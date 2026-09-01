"""Driving Blender: finding it, calling it, and reporting when it fails.

Blender itself is not a test dependency. A stub executable stands in for it and
records the command line, which is the whole contract this module owns.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from articraft.render import RenderError, find_blender, render_usdz

STUB = """#!/usr/bin/env python3
import pathlib, sys

argv = sys.argv[sys.argv.index("--") + 1 :]
out = pathlib.Path(argv[1])
out.mkdir(parents=True, exist_ok=True)
(out / "command.txt").write_text(" ".join(argv))
index = argv.index("--views") + 1
while index < len(argv) and not argv[index].startswith("--"):
    (out / f"{argv[index]}.png").write_bytes(b"png")
    index += 1
"""


def _stub(tmp_path: Path, body: str = STUB) -> Path:
    path = tmp_path / "blender"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _usdz(tmp_path: Path) -> Path:
    path = tmp_path / "model.usdz"
    path.write_bytes(b"not really a usdz, the stub never opens it")
    return path


def test_render_writes_one_image_per_view_and_passes_the_settings(tmp_path: Path) -> None:
    out = tmp_path / "renders"

    written = render_usdz(
        _usdz(tmp_path),
        out,
        blender=_stub(tmp_path),
        views=("hero", "side"),
        samples=64,
        resolution=(800, 600),
    )

    assert written == [out / "hero.png", out / "side.png"]
    assert all(path.is_file() for path in written)
    command = (out / "command.txt").read_text()
    assert "--views hero side" in command
    assert "--samples 64" in command
    assert "--resolution 800 600" in command


def test_render_reports_what_blender_said_when_it_fails(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "#!/bin/sh\necho 'no usd importer' >&2\nexit 3\n")

    with pytest.raises(RenderError, match="blender exited 3"):
        render_usdz(_usdz(tmp_path), tmp_path / "renders", blender=stub)


def test_render_fails_when_blender_leaves_an_image_out(tmp_path: Path) -> None:
    """A clean exit with no file is still a failed render."""

    stub = _stub(tmp_path, "#!/bin/sh\nexit 0\n")

    with pytest.raises(RenderError, match="no image written"):
        render_usdz(_usdz(tmp_path), tmp_path / "renders", blender=stub)


def test_render_refuses_an_unknown_view_or_a_missing_model(tmp_path: Path) -> None:
    stub = _stub(tmp_path)

    with pytest.raises(RenderError, match="unknown views"):
        render_usdz(_usdz(tmp_path), tmp_path / "renders", blender=stub, views=("underneath",))
    with pytest.raises(RenderError, match="no usdz to render"):
        render_usdz(tmp_path / "absent.usdz", tmp_path / "renders", blender=stub)


def test_blender_is_found_by_setting_then_environment(tmp_path: Path, monkeypatch) -> None:
    stub = _stub(tmp_path)
    monkeypatch.delenv("ARTICRAFT_BLENDER", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert find_blender(str(stub)) == stub
    monkeypatch.setenv("ARTICRAFT_BLENDER", str(stub))
    assert find_blender(None) == stub


def test_a_missing_blender_is_an_error_rather_than_a_quiet_downgrade(
    tmp_path: Path, monkeypatch
) -> None:
    """Falling back to the rasterizer would hand the critic the wrong image."""

    monkeypatch.delenv("ARTICRAFT_BLENDER", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(RenderError, match="set ARTICRAFT_BLENDER"):
        find_blender(None)

    plain = tmp_path / "not-executable"
    plain.write_text("")
    plain.chmod(0o644)
    with pytest.raises(RenderError, match="not an executable file"):
        find_blender(str(plain))
