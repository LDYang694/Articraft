"""Realistic renders of an exported USDZ, for judging how a model looks.

The loop already has a renderer, the rasterizer in `sdk/visual.py`, and it is
the wrong tool for this question. It draws no texture, no bounced light, and no
real reflection, so an oak door and a beige door come out as the same flat
rectangle. Grading appearance from that image is grading a proxy.

So appearance is judged on the real thing. This hands the exported USDZ to
Blender, which path-traces it on a small studio set and writes PNGs. Blender
runs as a subprocess with `worker.py`, because it brings its own Python.

Rendering is not part of every compile. It costs seconds and answers only one
question, so it happens when that question is asked: the `critic` tool renders
what it is about to grade.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "worker.py"
VIEWS = ("hero", "front", "side", "high")
DEFAULT_VIEWS = ("hero", "front")
DEFAULT_SAMPLES = 128
DEFAULT_RESOLUTION = (1024, 768)
DEFAULT_TIMEOUT_SECONDS = 600.0


class RenderError(RuntimeError):
    """Blender is missing, or it could not render the model."""


def find_blender(configured: str | None = None) -> Path:
    """Locate Blender, or say plainly how to point at it.

    Appearance has no cheaper honest answer than a real render, so a missing
    Blender is an error rather than a quiet fall back to the rasterizer. A
    silent downgrade would hand the critic the very image it was moved off.
    """

    candidate = configured or os.environ.get("ARTICRAFT_BLENDER") or shutil.which("blender")
    if not candidate:
        raise RenderError(
            "no blender found: install it, or set ARTICRAFT_BLENDER to the executable"
        )
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RenderError(f"blender at {path} is not an executable file")
    return path


def render_usdz(
    usdz: Path,
    out_dir: Path,
    *,
    blender: Path,
    views: tuple[str, ...] = DEFAULT_VIEWS,
    samples: int = DEFAULT_SAMPLES,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Path]:
    """Path-trace ``usdz`` into ``out_dir`` and return the images written."""

    unknown = [view for view in views if view not in VIEWS]
    if unknown:
        raise RenderError(f"unknown views {unknown}, pick from {list(VIEWS)}")
    if not views:
        raise RenderError("render at least one view")
    if not usdz.is_file():
        raise RenderError(f"no usdz to render at {usdz}")

    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(blender),
        "-b",
        "-P",
        str(WORKER),
        "--",
        str(usdz),
        str(out_dir),
        "--views",
        *views,
        "--samples",
        str(samples),
        "--resolution",
        str(resolution[0]),
        str(resolution[1]),
    ]
    try:
        finished = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"blender did not finish within {timeout:.0f}s") from exc
    except OSError as exc:
        raise RenderError(f"could not start blender: {exc}") from exc

    written = [out_dir / f"{view}.png" for view in views]
    missing = [path.name for path in written if not path.is_file()]
    if finished.returncode != 0 or missing:
        raise RenderError(_failure(finished, missing))
    return written


def _failure(finished: subprocess.CompletedProcess[str], missing: list[str]) -> str:
    """Blender is chatty, so a failure quotes only its last few lines."""

    tail = "\n".join((finished.stderr or finished.stdout or "").strip().splitlines()[-8:])
    reason = f"blender exited {finished.returncode}" if finished.returncode else "no image written"
    if missing:
        reason = f"{reason} (missing {', '.join(missing)})"
    return f"{reason}\n{tail}" if tail else reason
