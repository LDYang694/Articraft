"""Iterate on a finished model's materials without paying to build it again.

Geometry is the expensive part of a run and it is already done. Appearance work
does not need the agent at all: edit the materials in a run's `main.py`, then
compile, path-trace, and measure. This is that loop, with no model calls unless
`--critic` asks for one.

    uv run python scripts/material_lab.py runs/<run-id>
    uv run python scripts/material_lab.py runs/<run-id> --region 0.42 0.40 0.56 0.60
    uv run python scripts/material_lab.py runs/<run-id> --critic --goal "a light oak cabinet"

It works on a scratch copy by default, so the run it reads stays as it was.
Point `--in-place` at a scratch copy to keep editing the same one across runs.

What it prints is the point: the color the render reports for a region, beside
the color the reference photograph reports for the same region. A material is
right when those agree, and the numbers say so in a way that arguing about two
images does not.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from PIL import Image

from articraft.agent.provider import create_model
from articraft.agent.tools import ToolContext, get
from articraft.agent.tools._core import workspace_digest
from articraft.agent.workspace.local import LocalWorkspace
from articraft.compiler.worker import compile_run
from articraft.render import find_blender, render_usdz
from articraft.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "runs" / "material-lab"
DEFAULT_REGION = (0.42, 0.40, 0.56, 0.60)

RUN = typer.Argument(help="A finished run directory, or a workspace to work on directly.")
REGION = typer.Option(
    "--region",
    help="left,top,right,bottom as fractions, sampled in both images. Defaults to the middle.",
)
VIEWS = typer.Option("--views", help="Comma separated views to render.")
SAMPLES = typer.Option("--samples", help="Cycles samples per view.")
IN_PLACE = typer.Option("--in-place", help="Work in the run itself, not a scratch copy.")
CRITIC = typer.Option("--critic", help="Also ask the appearance critic. Costs a model call.")
GOAL = typer.Option("--goal", help="What the object is made of, for the critic.")


def main(
    run: Annotated[Path, RUN],
    region: Annotated[str, REGION] = "",
    views: Annotated[str, VIEWS] = "hero,front",
    samples: Annotated[int, SAMPLES] = 128,
    in_place: Annotated[bool, IN_PLACE] = False,
    critic: Annotated[bool, CRITIC] = False,
    goal: Annotated[str, GOAL] = "",
) -> None:
    run_dir = _run_dir(run, in_place=in_place)
    box = _box(region)
    chosen = tuple(view.strip() for view in views.split(",") if view.strip())
    print(f"working in {run_dir}")

    payload = compile_run(run_dir, include_report=False, textures_enabled=True)
    if payload["status"] != "success":
        print(f"compile failed: {payload['error'] or payload['stderr'][-400:]}")
        raise typer.Exit(1)
    usdz = Path(payload["usdz"])
    print(f"compiled {usdz.relative_to(run_dir)}")

    out = run_dir / "workspace" / "review" / "renders" / "lab"
    settings = get_settings()
    render_usdz(
        usdz,
        out,
        blender=find_blender(settings.blender_path),
        views=chosen,
        samples=samples,
    )
    print(f"rendered {', '.join(chosen)} into {out.relative_to(run_dir)}")

    _report(run_dir, out, chosen, box)
    if critic:
        _review(run_dir, goal or "the object in the reference photograph")


def _box(region: str) -> tuple[float, float, float, float]:
    if not region.strip():
        return DEFAULT_REGION
    try:
        values = tuple(float(part) for part in region.split(","))
    except ValueError as exc:
        raise typer.BadParameter("region must be four numbers, comma separated") from exc
    if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
        raise typer.BadParameter("region must be left,top,right,bottom with left<right, top<bottom")
    return values  # pyright: ignore[reportReturnType]


def _run_dir(run: Path, *, in_place: bool) -> Path:
    # Absolute, because the compile worker runs the script from inside the
    # workspace and a relative run directory resolves against that instead.
    run = run.resolve()
    source = run if (run / "workspace").is_dir() else run.parent
    if not (source / "workspace" / "main.py").is_file():
        raise typer.BadParameter(f"no workspace with a main.py under {run}")
    if in_place:
        return source
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    shutil.copytree(source, SCRATCH)
    return SCRATCH


def _measure(path: Path, box: tuple[float, float, float, float]) -> tuple[list[int], float]:
    with Image.open(path) as opened:
        pixels = np.asarray(opened.convert("RGB"), dtype=np.float64)
    height, width, _ = pixels.shape
    sample = pixels[
        int(box[1] * height) : int(box[3] * height), int(box[0] * width) : int(box[2] * width)
    ].reshape(-1, 3)
    median = np.median(sample, axis=0)
    spread = float(np.percentile(np.abs(sample - median).mean(axis=1), 90))
    return [round(float(value)) for value in median], round(spread, 1)


def _saturation(rgb: list[int]) -> float:
    """How much color the surface has, independent of how brightly it was lit.

    Reported instead of a plain red-minus-blue, which is the same number for a
    dim saturated brown and a bright muted one. Saturation survives a change of
    light and is therefore comparable between a render and a photograph.
    """

    high = max(rgb)
    return 0.0 if high == 0 else round((high - min(rgb)) / high, 3)


def _report(
    run_dir: Path, out: Path, views: tuple[str, ...], box: tuple[float, float, float, float]
) -> None:
    print(f"\nregion {list(box)}")
    print(f"{'image':<22} {'R':>4} {'G':>4} {'B':>4} {'light':>6} {'sat':>6} {'spread':>7}")
    rows: list[tuple[str, Path]] = [(f"render {view}", out / f"{view}.png") for view in views]
    reference = next(
        (
            path
            for suffix in (".png", ".jpg", ".jpeg", ".webp")
            if (path := run_dir / "workspace" / f"reference{suffix}").is_file()
        ),
        None,
    )
    if reference is not None:
        rows.append(("reference", reference))
    for label, path in rows:
        if not path.is_file():
            continue
        rgb, spread = _measure(path, box)
        red, green, blue = rgb
        light = round((red + green + blue) / 3, 1)
        saturation = _saturation(rgb)
        print(f"{label:<22} {red:>4} {green:>4} {blue:>4} {light:>6} {saturation:>6} {spread:>7}")
    if reference is None:
        print("(this run has no reference image, so there is nothing to compare against)")


def _review(run_dir: Path, goal: str) -> None:
    settings = get_settings()
    workspace = LocalWorkspace(output_dir=run_dir.parent, textures_enabled=True)
    context = ToolContext(
        workspace,
        run_dir,
        run_dir / "workspace",
        new_reviewer=lambda: create_model(settings),
        blender=settings.blender_path,
        textures=True,
    )
    usdz = sorted((run_dir / "result" / "usdz").glob("*.usdz"))[-1]
    context.successful_compile_result = {"status": "success", "usdz": str(usdz)}  # pyright: ignore[reportArgumentType]
    context.successful_compile_digest = workspace_digest(context.workspace)

    result = asyncio.run(get("critic").run(context, {"goal": goal}))
    print("\ncritic:", json.dumps(result.output, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    typer.run(main)
