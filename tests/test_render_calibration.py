"""The studio as an instrument: what it is given is what it reports back.

Opt in with `uv run pytest -m blender`, because this drives a real Blender.

These renders are read as evidence about a material, so the rig has to be
unbiased. Two settings broke that and neither announced itself. Blender's
default AgX view transform lifted a 0.20 surface to 0.34 while leaving a 0.80
one at 0.81, so every dark material reported lighter and flatter than authored.
A rig tuned by eye against one product photograph then over-exposed everything
by about 1.4x. Both were only visible by rendering a known value and measuring.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from articraft.render import RenderError, find_blender, render_usdz
from articraft.sdk import BoxGeometry, Material, RigidBodyAssembly
from articraft.sdk.export import export_assembly

pytestmark = pytest.mark.blender

# Authored display value -> how far the render may drift from it. The darkest
# patch has the loosest bound: ambient light lifts a dark surface, and no rig
# with a visible environment reports 0.20 exactly.
PATCHES = ((0.20, 0.22), (0.35, 0.10), (0.46, 0.08), (0.60, 0.08), (0.80, 0.08))


def _rendered_value(tmp_path, blender, authored: float) -> float:
    """Render one flat patch alone and read the middle of its front face."""

    model = RigidBodyAssembly("patch")
    model.rigid_body("body").add(
        BoxGeometry([0.4, 0.05, 0.4]),
        name="patch",
        material=Material(
            name="swatch",
            density=1000.0,
            base_color=(authored, authored, authored),
            roughness=0.9,
        ),
    )
    usdz = export_assembly(model, tmp_path / f"export{authored}").usdz
    out = tmp_path / f"render{authored}"
    render_usdz(usdz, out, blender=blender, views=("front",), samples=48)

    pixels = np.asarray(Image.open(out / "front.png").convert("RGB"), dtype=float)
    height, width, _ = pixels.shape
    middle = pixels[int(height * 0.45) : int(height * 0.55), int(width * 0.45) : int(width * 0.55)]
    return float(middle.mean()) / 255.0


def test_the_studio_reports_the_albedo_it_was_given(tmp_path) -> None:
    try:
        blender = find_blender()
    except RenderError as exc:
        pytest.skip(str(exc))

    drift = []
    for authored, tolerance in PATCHES:
        rendered = _rendered_value(tmp_path, blender, authored)
        drift.append((authored, round(rendered, 3), round(rendered / authored, 2)))
        assert abs(rendered - authored) <= tolerance, f"authored/rendered/ratio: {drift}"

    # Monotonic, so two materials never swap order on the way through the rig.
    values = [rendered for _, rendered, _ in drift]
    assert values == sorted(values), drift
