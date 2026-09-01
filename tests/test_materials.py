from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from PIL import Image
from pxr import Usd, UsdGeom, UsdShade  # pyright: ignore[reportAttributeAccessIssue]

from articraft.sdk import (
    BoxGeometry,
    JointAxis,
    JointDOF,
    JointFrame,
    Material,
    RigidBodyAssembly,
    textures,
)
from articraft.sdk.errors import ValidationError
from articraft.sdk.export import export_assembly
from articraft.sdk.materials import is_library_material, to_linear
from articraft.viewer import _read_version

# A derived material, defined once and reused -- the pattern the SDK expects.
BRONZE = Material.STEEL.but(name="bronze", color=(0.8, 0.5, 0.2, 1.0), roughness=0.3)


def _model() -> RigidBodyAssembly:
    model = RigidBodyAssembly("materialed")
    base = model.rigid_body("base")
    base.add(
        BoxGeometry([0.2, 0.2, 0.1]),
        name="body",
        material=BRONZE,
    )
    lid = model.rigid_body("lid")
    lid.add(BoxGeometry([0.2, 0.2, 0.02]), name="cap", color=(0.2, 0.3, 0.8))
    model.joint(
        "hinge",
        base.at(JointFrame()),
        lid.at(JointFrame()),
        dofs=(JointDOF(JointAxis.ROT_Z, limits=(0.0, 1.5)),),
    )
    return model


def test_material_validates_its_values() -> None:
    assert Material.STEEL.metallic == 1.0
    assert Material.GLASS.opacity < 1.0
    # Glass without an index of refraction renders as a flat film.
    assert Material.GLASS.ior == pytest.approx(1.45)
    assert Material.PAINTED_STEEL.clearcoat > 0.0

    with pytest.raises(ValidationError, match="metallic"):
        Material(name="bad", density=1000.0, metallic=1.5)
    with pytest.raises(ValidationError, match="roughness"):
        Material(name="bad", density=1000.0, roughness=-0.1)
    with pytest.raises(ValidationError, match="clearcoat"):
        Material(name="bad", density=1000.0, clearcoat=1.5)
    with pytest.raises(ValidationError, match="ior"):
        Material(name="bad", density=1000.0, ior=0.0)
    with pytest.raises(ValidationError, match="texture scale"):
        Material(name="bad", density=1000.0, texture="Wood062", texture_scale=-1.0)
    with pytest.raises(ValidationError, match="color"):
        Material(name="bad", density=1000.0, base_color=(2.0, 0.0, 0.0, 1.0))
    with pytest.raises(ValidationError, match="density"):
        Material(name="bad", density=-1.0)
    with pytest.raises(ValidationError, match="friction"):
        Material(name="bad", density=1000.0, friction=(0.5,))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="name"):
        Material(name="  ", density=1000.0)


def test_a_derived_material_keeps_what_it_did_not_change() -> None:
    brushed = Material.STEEL.but(roughness=0.75)

    assert brushed.roughness == 0.75
    assert brushed.density == Material.STEEL.density
    assert brushed.friction == Material.STEEL.friction
    # Lineage: brushed steel still looks like steel when textures are enabled.
    assert brushed.texture == Material.STEEL.texture
    assert brushed.texture_scale == Material.STEEL.texture_scale
    # Frozen and hashable, so one value can be attached to many shapes.
    assert brushed == Material.STEEL.but(roughness=0.75)


def test_a_derived_material_can_name_its_own_texture() -> None:
    """Oak derived from hardwood was unable to reach a wood texture at all."""

    oak = Material.HARDWOOD.but(name="oak", texture="Wood049", texture_scale=0.8)

    assert oak.texture == "Wood049"
    assert oak.texture_scale == 0.8
    assert oak.density == Material.HARDWOOD.density
    assert Material.STEEL.but(texture=None).texture is None


def test_a_derived_material_can_clear_optional_properties() -> None:
    glowing = Material(
        name="glowing",
        density=1000.0,
        friction=(0.5, 0.4),
        restitution=0.3,
        emissive=(1.0, 0.5, 0.0),
    )

    cleared = glowing.but(friction=None, restitution=None, emissive=None)

    assert cleared.friction is None
    assert cleared.restitution is None
    assert cleared.emissive is None


def test_an_invented_material_authors_no_friction_it_does_not_have() -> None:
    foam = Material(name="foam", density=60.0)

    assert foam.friction is None
    assert foam.texture is None
    assert not is_library_material(foam)
    assert is_library_material(Material.STEEL)


def test_color_alone_tints_a_shape_with_no_material() -> None:
    part = RigidBodyAssembly("o").rigid_body("p")
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="s", color=(0.2, 0.3, 0.8))
    shape = next(part._iter_shapes())

    assert shape.material is None
    assert shape.color == (0.2, 0.3, 0.8, 1.0)


def test_a_material_supplies_its_own_look() -> None:
    part = RigidBodyAssembly("o").rigid_body("p")
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="s", material=Material.RUBBER)
    shape = next(part._iter_shapes())
    assert shape.tint is None
    assert shape.display_material == Material.RUBBER


def test_color_recolors_a_material_without_changing_what_it_is() -> None:
    part = RigidBodyAssembly("o").rigid_body("p")
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="s", material=Material.STEEL, color=(0.2, 0.3, 0.8))
    shape = next(part._iter_shapes())

    assert shape.material is Material.STEEL
    assert shape.color == (0.2, 0.3, 0.8, 1.0)
    # Still reads as metal, and still weighs like steel.
    display = shape.display_material
    assert display is not None
    assert display.metallic == Material.STEEL.metallic
    assert display.density == Material.STEEL.density


def test_a_coating_looks_and_grips_like_itself_but_weighs_like_the_bulk() -> None:
    part = RigidBodyAssembly("o").rigid_body("p")
    part.add(
        BoxGeometry([0.1, 0.1, 0.1]),
        name="s",
        material=Material.ABS_PLASTIC,
        coating=Material.STEEL,
    )
    shape = next(part._iter_shapes())

    # Chrome-plated plastic: weighs like plastic, looks and slides like metal.
    assert shape.material is Material.ABS_PLASTIC
    assert shape.surface_material is Material.STEEL
    assert shape.display_material is not None
    assert shape.display_material.metallic == 1.0
    assert shape.display_material.density == Material.STEEL.density


def test_export_binds_usd_preview_surface(tmp_path) -> None:
    result = export_assembly(_model(), tmp_path)
    stage = Usd.Stage.Open(str(result.usdz))

    mesh = stage.GetPrimAtPath("/World/materialed/rigid_bodies/base/shapes/body")
    assert mesh.IsA(UsdGeom.Mesh)
    binding = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding()
    material = UsdShade.Material(stage.GetPrimAtPath(binding.GetMaterialPath()))
    assert material

    shader = UsdShade.Shader(stage.GetPrimAtPath(f"{binding.GetMaterialPath()}/surface"))
    assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
    assert shader.GetInput("metallic").Get() == pytest.approx(1.0)
    assert shader.GetInput("roughness").Get() == pytest.approx(0.3)
    # USD reads diffuseColor as linear, so the authored display color is
    # linearized on the way out. Writing (0.8, 0.5, 0.2) straight through
    # rendered a stop brighter and washed out.
    diffuse = shader.GetInput("diffuseColor").Get()
    assert tuple(float(value) for value in diffuse) == pytest.approx(to_linear((0.8, 0.5, 0.2)))

    # displayColor fallback is preserved for renderers that ignore UsdShade.
    assert mesh.GetAttribute("primvars:displayColor").Get() is not None

    # The manifest and the prim attrs keep the authored color, because the
    # viewer reads them as display colors.
    assert mesh.GetAttribute("articraft:material:baseColor").Get() == pytest.approx((0.8, 0.5, 0.2))


def test_export_writes_the_clear_coat_and_refraction_a_surface_declares(tmp_path) -> None:
    model = RigidBodyAssembly("finishes")
    part = model.rigid_body("base")
    part.add(BoxGeometry([0.2, 0.2, 0.1]), name="panel", material=Material.PAINTED_STEEL)
    part.add(BoxGeometry([0.1, 0.1, 0.002]), name="pane", material=Material.GLASS)

    result = export_assembly(model, tmp_path)
    stage = Usd.Stage.Open(str(result.usdz))

    def surface(shape: str) -> UsdShade.Shader:
        mesh = stage.GetPrimAtPath(f"/World/finishes/rigid_bodies/base/shapes/{shape}")
        binding = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding()
        return UsdShade.Shader(stage.GetPrimAtPath(f"{binding.GetMaterialPath()}/surface"))

    panel = surface("panel")
    assert panel.GetInput("clearcoat").Get() == pytest.approx(Material.PAINTED_STEEL.clearcoat)
    assert panel.GetInput("clearcoatRoughness").Get() == pytest.approx(
        Material.PAINTED_STEEL.clearcoat_roughness
    )
    # An opaque surface has no refraction to author.
    assert panel.GetInput("ior").Get() is None

    pane = surface("pane")
    assert pane.GetInput("ior").Get() == pytest.approx(1.45)
    assert pane.GetInput("opacity").Get() == pytest.approx(Material.GLASS.opacity)


def test_export_payload_carries_material_and_appearance(tmp_path) -> None:
    model = RigidBodyAssembly("materialed")
    part = model.rigid_body("base")
    part.add(BoxGeometry([0.2, 0.2, 0.1]), name="body", material=Material.STEEL)

    result = export_assembly(model, tmp_path)
    manifest = json.loads(result.manifest.read_text())
    body = manifest["rigid_bodies"][0]["shapes"][0]

    assert body["material"]["name"] == "steel"
    assert body["material"]["library"] is True
    assert body["material"]["density"] == 7850.0
    assert body["material"]["friction"] == list(Material.STEEL.friction or ())
    assert body["material"]["texture"] == "Metal009"
    assert body["material"]["texture_scale"] == 0.5


def test_textured_export_resolves_each_explicit_kind_once(monkeypatch, tmp_path) -> None:
    model = RigidBodyAssembly("textures")
    part = model.rigid_body("part")
    # The material is the texture key, so two steel shapes share one fetch and a
    # shape with only a color asks for nothing.
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="warm_light", material=Material.STEEL)
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="showcase", material=Material.STEEL)
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="steel_by_name_only", color=(0.5, 0.5, 0.5))
    attempts = 0

    def fail_fetch(_kind):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("offline")

    monkeypatch.setattr("articraft.sdk.textures.fetch_material", fail_fetch)
    result = export_assembly(model, tmp_path, textured=True)

    assert attempts == 1
    assert result.textures.requested_shapes == 2
    assert result.textures.textured_shapes == 0
    assert len(result.textures.errors) == 1


def _read_from_usdz(usdz, name: str) -> Image.Image:
    with zipfile.ZipFile(usdz) as package, package.open(name) as member:
        return Image.open(io.BytesIO(member.read())).convert("RGB")


def _mean_rgb(image: Image.Image) -> list[float]:
    return [float(value) for value in np.asarray(image, dtype=np.float64).mean(axis=(0, 1))]


def _fake_texture_set(directory, *, gray: int = 128, roughness_gray: int = 128):
    """A texture set of flat gray maps, so their averages are known exactly."""

    directory.mkdir(exist_ok=True)
    color = directory / "Metal009_1K-JPG_Color.jpg"
    Image.new("RGB", (32, 32), (gray, gray, gray)).save(color)
    roughness_map = directory / "Metal009_1K-JPG_Roughness.jpg"
    Image.new("L", (32, 32), roughness_gray).save(roughness_map)
    occlusion = directory / "Metal009_1K-JPG_AmbientOcclusion.jpg"
    Image.new("L", (32, 32), 230).save(occlusion)
    return textures.TextureSet(
        "Metal009",
        "1K",
        color,
        roughness=roughness_map,
        occlusion=occlusion,
    )


def test_textured_export_applies_explicit_texture(monkeypatch, tmp_path) -> None:
    texture_set = _fake_texture_set(tmp_path / "maps")
    spec = textures.MaterialSpec("Metal009")
    monkeypatch.setattr(
        textures,
        "fetch_material",
        lambda kind: (texture_set, spec),
    )
    model = RigidBodyAssembly("textured")
    model.rigid_body("part").add(
        BoxGeometry([0.1, 0.1, 0.1]),
        name="name_has_no_material_semantics",
        material=Material.STEEL,
    )

    result = export_assembly(model, tmp_path / "result", textured=True)
    stage = Usd.Stage.Open(str(result.usdz))
    mesh = stage.GetPrimAtPath(
        "/World/textured/rigid_bodies/part/shapes/name_has_no_material_semantics"
    )

    assert result.textures.requested_shapes == 1
    assert result.textures.textured_shapes == 1
    assert result.textures.errors == ()
    assert mesh.GetAttribute("articraft:material:textured").Get() == pytest.approx(1.0)
    points = mesh.GetAttribute("points").Get()
    uvs = UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st").Get()  # pyright: ignore[reportAttributeAccessIssue]
    assert len(points) == len(uvs)
    assert all(0.0 <= float(component) <= 1.0 for uv in uvs for component in uv)

    binding = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding()
    material = UsdShade.Material(stage.GetPrimAtPath(binding.GetMaterialPath()))
    shader = UsdShade.Shader(stage.GetPrimAtPath(f"{material.GetPath()}/surface"))
    assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
    diffuse_source = shader.GetInput("diffuseColor").GetConnectedSource()
    assert diffuse_source is not None
    assert UsdShade.Shader(diffuse_source[0].GetPrim()).GetIdAttr().Get() == "UsdUVTexture"

    # The authored color and roughness are what the map averages to, not a
    # filter over it: an oak tint over an oak map used to come out mud. Color
    # is moved onto the map itself rather than scaled in the shader, so the
    # exported image is a recolored copy and the shader has nothing to scale.
    diffuse = UsdShade.Shader(diffuse_source[0].GetPrim())
    written = diffuse.GetInput("file").Get().path
    assert diffuse.GetInput("scale").Get() is None
    assert "Metal009_1K-JPG_Color-" in written
    recolored = _read_from_usdz(result.usdz, Path(written).name)
    assert _mean_rgb(recolored) == pytest.approx(
        [value * 255 for value in Material.STEEL.base_color[:3]], abs=3.0
    )

    roughness_source = shader.GetInput("roughness").GetConnectedSource()
    assert roughness_source is not None
    roughness = UsdShade.Shader(roughness_source[0].GetPrim())
    assert float(roughness.GetInput("scale").Get()[0]) * (128 / 255) == pytest.approx(
        Material.STEEL.roughness, rel=1e-3
    )
    assert shader.GetInput("occlusion").GetConnectedSource() is not None


def test_recoloring_moves_the_average_and_keeps_the_pattern(monkeypatch, tmp_path) -> None:
    """A multiply stretched a dark map's bright end past white and flattened its dark end.

    The grain is the spread around the mean, so scaling to reach a lighter
    color destroyed the thing that made it read as grain. Translating the mean
    leaves every pixel's distance from it alone.
    """

    maps = tmp_path / "maps"
    maps.mkdir()
    source = maps / "Wood049_1K-JPG_Color.jpg"
    # A dark map with real variation in it, which is what a grain is.
    grain = np.tile(np.linspace(20, 90, 64, dtype=np.uint8)[:, None], (1, 64))
    Image.fromarray(np.dstack([grain, grain // 2, grain // 3])).save(source)
    monkeypatch.setattr(
        textures,
        "fetch_material",
        lambda kind: (textures.TextureSet("Wood049", "1K", source), textures.MaterialSpec("W")),
    )
    before = _spread(Image.open(source))

    pale = Material.HARDWOOD.but(name="pale", color=(0.78, 0.66, 0.53), texture="Wood049")
    flat = pale.but(name="flat", pattern_strength=0.2)
    model = RigidBodyAssembly("boards")
    part = model.rigid_body("part")
    part.add(BoxGeometry([0.4, 0.4, 0.02]), name="figured", material=pale)
    part.add(BoxGeometry([0.4, 0.4, 0.02]).translate(0.5, 0, 0), name="painted", material=flat)

    result = export_assembly(model, tmp_path / "result", textured=True)
    stage = Usd.Stage.Open(str(result.usdz))

    def written(shape: str) -> Image.Image:
        mesh = stage.GetPrimAtPath(f"/World/boards/rigid_bodies/part/shapes/{shape}")
        binding = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding()
        node = UsdShade.Shader(stage.GetPrimAtPath(f"{binding.GetMaterialPath()}/diffuseTex"))
        return _read_from_usdz(result.usdz, Path(node.GetInput("file").Get().path).name)

    figured, painted = written("figured"), written("painted")
    # Both average to the authored color, dark map or not.
    for image in (figured, painted):
        assert _mean_rgb(image) == pytest.approx([199, 168, 135], abs=6.0)
    # The full-strength one keeps the map's variation; the settled one does not.
    assert _spread(figured) == pytest.approx(before, rel=0.15)
    assert _spread(painted) < before * 0.4


def _spread(image: Image.Image) -> float:
    """Lightness variation, which is the contrast the move is meant to preserve.

    Measured in RGB it would look like it grew: equal perceptual steps span
    more RGB at a light color than a dark one, so lifting a dark map onto a
    pale one widens the numbers while the contrast a viewer sees holds.
    """

    lab = np.asarray(image.convert("RGB").convert("LAB"), dtype=np.float64)
    return float(lab[..., 0].std())


def test_texture_repeats_at_its_real_size(monkeypatch, tmp_path) -> None:
    """One tile stretched over a whole shape is the wrong size at any scale."""

    texture_set = _fake_texture_set(tmp_path / "maps")
    monkeypatch.setattr(
        textures,
        "fetch_material",
        lambda kind: (texture_set, textures.MaterialSpec("Metal009")),
    )
    plank = Material.HARDWOOD.but(name="plank", texture="Metal009", texture_scale=1.0)
    model = RigidBodyAssembly("tiled")
    part = model.rigid_body("part")
    part.add(BoxGeometry([2.0, 0.6, 0.02]), name="door", material=plank)
    part.add(BoxGeometry([0.006, 0.006, 0.05]), name="pin", material=plank)

    result = export_assembly(model, tmp_path / "result", textured=True)
    stage = Usd.Stage.Open(str(result.usdz))

    def uv_span(shape: str) -> float:
        prim = stage.GetPrimAtPath(f"/World/tiled/rigid_bodies/part/shapes/{shape}")
        uvs = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st").Get()  # pyright: ignore[reportAttributeAccessIssue]
        return max(float(component) for uv in uvs for component in uv)

    # A 2 m door wears two 1 m tiles; a 6 mm pin wears a sliver of one.
    assert uv_span("door") == pytest.approx(2.0, rel=0.05)
    assert uv_span("pin") < 0.05


def test_texture_rotation_turns_the_grain_without_sliding_it(monkeypatch, tmp_path) -> None:
    """A rail cut across a stile needs the quarter turn, or its grain runs wrong.

    Without this the critic kept naming grain direction, the author had no
    control that could answer it, and the same issue came back every review.
    """

    texture_set = _fake_texture_set(tmp_path / "maps")
    monkeypatch.setattr(
        textures,
        "fetch_material",
        lambda kind: (texture_set, textures.MaterialSpec("Metal009")),
    )
    oak = Material.HARDWOOD.but(name="oak", texture="Metal009")
    model = RigidBodyAssembly("frame")
    part = model.rigid_body("part")
    # The same board twice, so only the rotation differs between them.
    part.add(BoxGeometry([0.6, 0.1, 0.02]), name="flat", material=oak)
    part.add(
        BoxGeometry([0.6, 0.1, 0.02]).translate(0.0, 0.3, 0.0),
        name="turned",
        material=oak.but(name="oak_turned", texture_rotation=math.pi / 2),
    )

    result = export_assembly(model, tmp_path / "result", textured=True)
    stage = Usd.Stage.Open(str(result.usdz))

    def uvs(shape: str) -> list[tuple[float, float]]:
        prim = stage.GetPrimAtPath(f"/World/frame/rigid_bodies/part/shapes/{shape}")
        return [
            (float(uv[0]), float(uv[1]))
            for uv in UsdGeom.PrimvarsAPI(prim).GetPrimvar("st").Get()  # pyright: ignore[reportAttributeAccessIssue]
        ]

    def centre(points: list[tuple[float, float]]) -> tuple[float, float]:
        return (
            sum(u for u, _ in points) / len(points),
            sum(v for _, v in points) / len(points),
        )

    def span(points: list[tuple[float, float]]) -> tuple[float, float]:
        return (
            max(u for u, _ in points) - min(u for u, _ in points),
            max(v for _, v in points) - min(v for _, v in points),
        )

    flat, turned = uvs("flat"), uvs("turned")
    assert turned != flat
    # The turn is about the chart's own middle, so the tile rotates in place
    # rather than sliding off the part.
    assert centre(turned) == pytest.approx(centre(flat), abs=1e-5)
    # A quarter turn swaps the chart's width and height.
    assert span(turned) == pytest.approx(tuple(reversed(span(flat))), abs=1e-5)


def test_viewer_readback_exposes_shape_materials(tmp_path) -> None:
    result = export_assembly(_model(), tmp_path)
    version = _read_version(result.usdz)
    model = cast(dict[str, Any], version["model"])
    parts = {part["name"]: part for part in cast(list[dict[str, Any]], model["parts"])}

    body = parts["base"]["shapes"][0]
    assert body["usd_name"] == "body"
    assert body["appearance"]["metallic"] == pytest.approx(1.0)
    assert body["appearance"]["roughness"] == pytest.approx(0.3)
    assert body["appearance"]["base_color"] == pytest.approx([0.8, 0.5, 0.2])

    cap = parts["lid"]["shapes"][0]
    assert cap["appearance"]["metallic"] == pytest.approx(0.0)


def test_viewer_receives_the_mass_it_displays(tmp_path) -> None:
    """The parts panel was added in #68 but the viewer was never given the data."""
    model = RigidBodyAssembly("weighed")
    part = model.rigid_body("body")
    part.add(BoxGeometry([0.1, 0.1, 0.1]), name="shell", material=Material.STEEL)
    part.add(
        BoxGeometry([0.05, 0.02, 0.01]).translate(0.06, 0.0, 0.0),
        name="foot",
        material=Material.RUBBER,
    )

    result = export_assembly(model, tmp_path)
    version = _read_version(result.usdz)
    mass = cast(dict[str, Any], version["model"])["parts"][0]["mass"]

    assert mass is not None
    assert mass["kilograms"] == pytest.approx(
        0.1**3 * 7850.0 + 0.05 * 0.02 * 0.01 * 1200.0, rel=1e-3
    )
    assert mass["materials"] == ["rubber", "steel"]
    assert len(mass["center_of_mass"]) == 3
