"""Read a color off an image instead of arguing about it.

A run used to author a surface color by eye, and a review used to grade it by
eye, and neither could settle the question: one cabinet went warm, then gray,
then yellow across three reviews while the color it needed sat in the reference
photograph the whole time, measurable to within a few levels.

So it gets measured. Name a region of an image, get back the color it averages
to, in the same 0..1 display values a ``Material`` takes.

The median is used rather than the mean, and the spread is reported beside it.
A region that catches a handle, a shadow, or a highlight has a wide spread and a
median that still lands on the surface, so the number stays usable and the
caller can see when it should not be trusted.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image

from articraft.agent.tools._core import (
    Tool,
    ToolContext,
    display_path,
    readable_path,
    reference_image,
    schema,
)


async def run(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = _path(context, args.get("path"))
    region = _region(args.get("region"))

    with Image.open(path) as opened:
        pixels = np.asarray(opened.convert("RGB"), dtype=np.float64)
    height, width, _ = pixels.shape
    left, top, right, bottom = (
        int(region[0] * width),
        int(region[1] * height),
        max(int(region[2] * width), int(region[0] * width) + 1),
        max(int(region[3] * height), int(region[1] * height) + 1),
    )
    sample = pixels[top:bottom, left:right].reshape(-1, 3)

    median = np.median(sample, axis=0)
    # How far the region's outer tenth sits from the reported color. The median
    # absolute deviation is the usual robust spread and the wrong one here: it
    # reads zero when a tenth of the region is a dark handle, which is exactly
    # the case worth reporting.
    spread = float(np.percentile(np.abs(sample - median).mean(axis=1), 90))
    return {
        "path": display_path(context.workspace, path),
        "region": list(region),
        "pixels": len(sample),
        # Ready to paste into base_color, which takes display values.
        "color": [round(float(value) / 255.0, 3) for value in median],
        "rgb_255": [round(float(value)) for value in median],
        "spread": round(spread / 255.0, 3),
        "note": _note(spread / 255.0),
    }


def _path(context: ToolContext, raw: object) -> Any:
    if raw is None:
        found = reference_image(context.workspace)
        if found is None:
            raise ValueError("this run has no reference image, so name a path to sample")
        return found
    return readable_path(context.workspace, str(raw))


def _region(raw: object) -> tuple[float, float, float, float]:
    if raw is None:
        return (0.4, 0.4, 0.6, 0.6)
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("region must be [left, top, right, bottom] as fractions from 0 to 1")
    try:
        box = tuple(float(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("region values must be numbers from 0 to 1") from exc
    if not all(0.0 <= value <= 1.0 for value in box):
        raise ValueError("region values must be between 0 and 1")
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("region must have left < right and top < bottom")
    return box  # pyright: ignore[reportReturnType]


def _note(spread: float) -> str:
    if spread < 0.04:
        return "one flat surface, so the color is solid evidence"
    if spread < 0.10:
        return "some variation, normal for a patterned or lit surface"
    return "wide spread: the region may span more than one surface, so check what it covers"


TOOL = Tool(
    "sample_color",
    schema(
        "sample_color",
        "Measure the color of a region of an image, so a surface color is read rather than "
        "guessed. Returns the median as 0..1 display values ready for a Material's color, "
        "plus how much the region varies. Defaults to the run's reference photograph. Use "
        "it on the reference before authoring a color, and view the image first so the "
        "region lands on the surface you mean.",
        {
            "path": {
                "type": "string",
                "description": "Workspace image to sample. Defaults to the run's reference.",
            },
            "region": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "[left, top, right, bottom] as fractions of width and height, from 0 "
                    "to 1. Defaults to the middle fifth of the image."
                ),
            },
        },
        [],
    ),
    run,
    supports_parallel=True,
)
