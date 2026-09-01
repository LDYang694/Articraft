"""Measuring a color off an image, rather than authoring it by eye."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from articraft.agent.tools import ToolContext, get
from articraft.agent.workspace.local import LocalWorkspace


def _run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)


def _context(tmp_path: Path, slug: str = "sample") -> ToolContext:
    env = LocalWorkspace(output_dir=tmp_path)
    run_dir = env.create_run(slug)
    return ToolContext(env, run_dir, run_dir / "workspace")


def test_it_reads_the_reference_and_hands_back_a_material_color(tmp_path: Path) -> None:
    context = _context(tmp_path)
    Image.new("RGB", (100, 100), (163, 120, 79)).save(context.workspace / "reference.png")

    result = _run(get("sample_color").run(context, {}))

    assert result["path"] == "reference.png"
    assert result["rgb_255"] == [163, 120, 79]
    # Straight into base_color, which takes display values.
    assert result["color"] == [0.639, 0.471, 0.31]
    assert result["spread"] == 0.0
    assert "flat surface" in result["note"]


def test_the_median_survives_a_region_that_catches_a_handle(tmp_path: Path) -> None:
    """A mean would be dragged by the dark strip; the median stays on the wood."""

    context = _context(tmp_path)
    pixels = np.full((100, 100, 3), (180, 140, 100), dtype=np.uint8)
    pixels[40:60, :] = (10, 10, 10)
    Image.fromarray(pixels).save(context.workspace / "board.png")

    result = _run(get("sample_color").run(context, {"path": "board.png", "region": [0, 0, 1, 1]}))

    assert result["rgb_255"] == [180, 140, 100]
    # The spread reports that the region was not one surface.
    assert result["spread"] > 0.3
    assert "more than one surface" in result["note"]


def test_a_region_picks_out_one_part_of_the_image(tmp_path: Path) -> None:
    context = _context(tmp_path)
    pixels = np.full((100, 100, 3), (200, 200, 200), dtype=np.uint8)
    pixels[:, :50] = (40, 60, 90)
    Image.fromarray(pixels).save(context.workspace / "split.png")

    left = _run(get("sample_color").run(context, {"path": "split.png", "region": [0, 0, 0.4, 1]}))
    right = _run(get("sample_color").run(context, {"path": "split.png", "region": [0.6, 0, 1, 1]}))

    assert left["rgb_255"] == [40, 60, 90]
    assert right["rgb_255"] == [200, 200, 200]


def test_it_refuses_a_region_that_is_not_a_region(tmp_path: Path) -> None:
    context = _context(tmp_path)
    Image.new("RGB", (50, 50), (10, 10, 10)).save(context.workspace / "reference.png")

    with pytest.raises(ValueError, match="left < right"):
        _run(get("sample_color").run(context, {"region": [0.8, 0.0, 0.2, 1.0]}))
    with pytest.raises(ValueError, match="between 0 and 1"):
        _run(get("sample_color").run(context, {"region": [0.0, 0.0, 2.0, 1.0]}))


def test_it_says_so_when_the_run_has_no_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no reference image"):
        _run(get("sample_color").run(_context(tmp_path), {}))
