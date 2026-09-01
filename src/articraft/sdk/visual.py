from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast

import numpy as np
import trimesh
from PIL import Image, ImageDraw

from articraft.sdk._collision import MeshCollisionKernel
from articraft.sdk._mesh.core import geometry_to_trimesh
from articraft.sdk.assembly import WORLD, Joint, JointAxis, RigidBodyAssembly, _frame_matrix
from articraft.sdk.bodies import RigidBody
from articraft.sdk.errors import ValidationError
from articraft.sdk.materials import Material, to_linear
from articraft.sdk.testing import DEFAULT_MESH_TOLERANCE, PoseSample

Vec3: TypeAlias = tuple[float, float, float]
ColorMode: TypeAlias = Literal["part", "shape", "material"]
Projection: TypeAlias = Literal["orthographic", "perspective"]

# A two-light rig plus a constant ambient term. The key light is where the old
# single light was, so views keep roughly the same orientation as before.
_KEY_LIGHT: Vec3 = (0.4, -0.5, 1.0)
_FILL_LIGHT: Vec3 = (-0.7, -0.3, 0.35)
_KEY_STRENGTH = 0.72
_FILL_STRENGTH = 0.22
_AMBIENT_STRENGTH = 0.10


@dataclass(frozen=True)
class ImagePoint:
    """A normalized image position, with ``u`` right and ``v`` down."""

    u: float
    v: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.u) or not math.isfinite(self.v):
            raise ValidationError("image coordinates must be finite")
        if not 0.0 <= self.u <= 1.0 or not 0.0 <= self.v <= 1.0:
            raise ValidationError("image coordinates must be between 0 and 1")

    def pixel(self, width: int, height: int) -> tuple[float, float]:
        return self.u * width, self.v * height


@dataclass(frozen=True)
class Reticle:
    point: ImagePoint
    label: str = ""
    color: tuple[int, int, int] = (235, 55, 170)


@dataclass(frozen=True)
class PointOverlay:
    position: Vec3
    label: str = ""
    color: tuple[int, int, int] = (220, 45, 45)
    radius: int = 4


@dataclass(frozen=True)
class LineOverlay:
    start: Vec3
    end: Vec3
    label: str = ""
    color: tuple[int, int, int] = (220, 45, 45)
    width: int = 2


@dataclass(frozen=True)
class ModelView:
    direction: Vec3 = (1.0, -1.0, 0.7)
    up: Vec3 = (0.0, 0.0, 1.0)
    projection: Projection = "orthographic"
    width: int = 640
    height: int = 480
    color_by: ColorMode = "part"
    background: tuple[int, int, int] = (247, 248, 250)
    show_bounds: bool = False
    show_joints: bool = False
    selected_parts: tuple[str, ...] = ()
    selected_shapes: tuple[tuple[str, str], ...] = ()
    points: tuple[PointOverlay, ...] = ()
    lines: tuple[LineOverlay, ...] = ()

    @classmethod
    def front(cls, **kwargs) -> ModelView:
        return cls(direction=(0.0, -1.0, 0.15), **kwargs)

    @classmethod
    def side(cls, **kwargs) -> ModelView:
        return cls(direction=(1.0, 0.0, 0.15), **kwargs)

    @classmethod
    def top(cls, **kwargs) -> ModelView:
        return cls(direction=(0.0, 0.0, 1.0), up=(0.0, 1.0, 0.0), **kwargs)

    @classmethod
    def three_quarter(cls, **kwargs) -> ModelView:
        return cls(direction=(1.0, -1.0, 0.7), **kwargs)


@dataclass(frozen=True)
class SectionView:
    plane_origin: Vec3 = (0.0, 0.0, 0.0)
    plane_normal: Vec3 = (0.0, 1.0, 0.0)
    horizontal: Vec3 | None = None
    vertical: Vec3 | None = None
    width: int = 720
    height: int = 480
    color_by: ColorMode = "part"
    background: tuple[int, int, int] = (250, 250, 250)
    line_width: int = 3
    show_axes: bool = True
    selected_parts: tuple[str, ...] = ()
    selected_shapes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MeridionalSectionView:
    axis: Vec3 = (1.0, 0.0, 0.0)
    radial_direction: Vec3 = (0.0, 0.0, 1.0)
    origin: Vec3 = (0.0, 0.0, 0.0)
    width: int = 900
    height: int = 480
    color_by: ColorMode = "part"
    background: tuple[int, int, int] = (250, 250, 250)
    line_width: int = 3
    show_axes: bool = True
    selected_parts: tuple[str, ...] = ()
    selected_shapes: tuple[tuple[str, str], ...] = ()

    def section_view(self) -> SectionView:
        axis = _normalize(self.axis, "axis")
        radial = _normalize(self.radial_direction, "radial_direction")
        if abs(float(np.dot(axis, radial))) > 1e-6:
            raise ValidationError("axis and radial_direction must be perpendicular")
        normal = np.cross(axis, radial)
        return SectionView(
            plane_origin=self.origin,
            plane_normal=cast(Vec3, tuple(float(value) for value in normal)),
            horizontal=self.axis,
            vertical=self.radial_direction,
            width=self.width,
            height=self.height,
            color_by=self.color_by,
            background=self.background,
            line_width=self.line_width,
            show_axes=self.show_axes,
            selected_parts=self.selected_parts,
            selected_shapes=self.selected_shapes,
        )


@dataclass(frozen=True)
class MotionStripView:
    articulation: str | Joint
    positions: tuple[float, ...] = ()
    dof: JointAxis | str | None = None
    samples: int = 5
    view: ModelView = field(
        default_factory=lambda: ModelView.three_quarter(width=320, height=280, show_joints=True)
    )
    gap: int = 12
    background: tuple[int, int, int] = (247, 248, 250)


VisualSpec: TypeAlias = ModelView | SectionView | MeridionalSectionView | MotionStripView


@dataclass(frozen=True)
class _Surface:
    """How a shape responds to light, taken from its material.

    Separate from the shape's flat ``color`` because ``color_by`` decides which
    color a preview paints, while the light response always belongs to the
    material the shape is made of.
    """

    metallic: float = 0.0
    roughness: float = 0.6
    clearcoat: float = 0.0
    clearcoat_roughness: float = 0.1
    emissive: Vec3 | None = None
    opacity: float = 1.0


_DEFAULT_SURFACE = _Surface()


@dataclass(frozen=True)
class _RenderMesh:
    part: str
    shape: str
    mesh: trimesh.Trimesh
    color: tuple[int, int, int]
    surface: _Surface = _DEFAULT_SURFACE


def annotate_image(
    source: str | Path,
    reticles: tuple[Reticle, ...],
    output: str | Path,
) -> Path:
    """Copy an image to PNG and draw normalized, named reticles on it."""
    source_path = Path(source)
    output_path = Path(output)
    if output_path.suffix.lower() != ".png":
        raise ValidationError("annotated image output must use a .png suffix")
    try:
        with Image.open(source_path) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ValidationError(f"could not open image: {source_path}") from exc
    draw = ImageDraw.Draw(image)
    for reticle in reticles:
        _draw_reticle(draw, image.width, image.height, reticle)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=False)
    return output_path


def render_view(
    model: RigidBodyAssembly,
    view: VisualSpec,
    output: str | Path,
    *,
    pose: dict[str, float] | PoseSample | None = None,
    mesh_tolerance: float = DEFAULT_MESH_TOLERANCE,
) -> Path:
    """Render one deterministic PNG view and return its path."""
    output_path = Path(output)
    if output_path.suffix.lower() != ".png":
        raise ValidationError("visual output must use a .png suffix")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pose_values = pose.as_dict() if isinstance(pose, PoseSample) else dict(pose or {})
    if isinstance(view, ModelView):
        image = _render_model(model, view, pose_values, mesh_tolerance)
    elif isinstance(view, MeridionalSectionView):
        image = _render_section(model, view.section_view(), pose_values, mesh_tolerance)
    elif isinstance(view, SectionView):
        image = _render_section(model, view, pose_values, mesh_tolerance)
    elif isinstance(view, MotionStripView):
        image = _render_motion_strip(model, view, pose_values, mesh_tolerance)
    else:
        raise TypeError(f"unsupported visual specification: {type(view).__name__}")
    image.save(output_path, format="PNG", optimize=False)
    return output_path


def _render_model(
    model: RigidBodyAssembly,
    view: ModelView,
    pose: dict[str, float],
    mesh_tolerance: float,
    *,
    fit_vertices: np.ndarray | None = None,
) -> Image.Image:
    _validate_size(view.width, view.height)
    meshes = _world_meshes(
        model,
        pose,
        mesh_tolerance,
        color_by=view.color_by,
        selected_parts=view.selected_parts,
        selected_shapes=view.selected_shapes,
    )
    vertices = np.concatenate([np.asarray(item.mesh.vertices) for item in meshes], axis=0)
    fit = vertices if fit_vertices is None else fit_vertices
    center = (fit.min(axis=0) + fit.max(axis=0)) * 0.5
    forward, right, camera_up = _camera_basis(view.direction, view.up)
    combined = np.concatenate((vertices, fit), axis=0)
    combined_projection = _project_points(
        combined,
        center,
        right,
        camera_up,
        forward,
        projection=view.projection,
    )
    projected = combined_projection[: len(vertices)]
    fit_projection = combined_projection[len(vertices) :]
    scale, offset = _fit_projection(fit_projection[:, :2], view.width, view.height)
    image_array = np.empty((view.height, view.width, 3), dtype=np.uint8)
    image_array[:] = np.asarray(view.background, dtype=np.uint8)
    depth_buffer = np.full((view.height, view.width), np.inf, dtype=np.float64)

    vertex_offset = 0
    view_direction = -forward
    opaque: list[tuple[np.ndarray, np.ndarray]] = []
    translucent: list[tuple[np.ndarray, np.ndarray, float]] = []
    for item in meshes:
        mesh_vertices = np.asarray(item.mesh.vertices)
        count = len(mesh_vertices)
        screen = projected[vertex_offset : vertex_offset + count].copy()
        screen[:, 0] = screen[:, 0] * scale + offset[0]
        screen[:, 1] = -screen[:, 1] * scale + offset[1]
        vertex_offset += count
        triangles = screen[np.asarray(item.mesh.faces)]
        world_triangles = mesh_vertices[np.asarray(item.mesh.faces)]
        normals = np.cross(
            world_triangles[:, 1] - world_triangles[:, 0],
            world_triangles[:, 2] - world_triangles[:, 0],
        )
        lengths = np.linalg.norm(normals, axis=1)
        valid = lengths > 1e-14
        normals[valid] /= lengths[valid, None]
        colors = _shade_faces(normals, view_direction, item.color, item.surface)
        if item.surface.opacity >= 1.0:
            opaque.append((triangles[valid], colors[valid]))
        else:
            translucent.append((triangles[valid], colors[valid], item.surface.opacity))

    for shape_triangles, shape_colors in opaque:
        for triangle, color in zip(shape_triangles, shape_colors, strict=True):
            _raster_triangle(image_array, depth_buffer, triangle, color)
    # Translucent faces blend into whatever is already there, so they go last
    # and back to front. They read depth but do not write it, which keeps a
    # pane from hiding the parts behind it.
    for shape_triangles, shape_colors, opacity in translucent:
        for index in np.argsort(-shape_triangles[:, :, 2].mean(axis=1)):
            _raster_triangle(
                image_array,
                depth_buffer,
                shape_triangles[index],
                shape_colors[index],
                alpha=opacity,
            )

    image = Image.fromarray(image_array, mode="RGB")
    draw = ImageDraw.Draw(image)
    if view.show_bounds:
        corners = _bounds_corners(vertices.min(axis=0), vertices.max(axis=0))
        box = _project_points(
            corners, center, right, camera_up, forward, projection=view.projection
        )
        box[:, 0] = box[:, 0] * scale + offset[0]
        box[:, 1] = -box[:, 1] * scale + offset[1]
        _draw_bounds(draw, box)
    if view.show_joints:
        _draw_joints(
            draw,
            model,
            pose,
            center,
            right,
            camera_up,
            forward,
            view.projection,
            scale,
            offset,
            max(float(np.linalg.norm(np.ptp(fit, axis=0))), 1e-6),
        )
    _draw_overlays(
        draw,
        view,
        center,
        right,
        camera_up,
        forward,
        scale,
        offset,
        max(float(np.linalg.norm(np.ptp(fit, axis=0))), 1e-6),
    )
    return image


def _render_section(
    model: RigidBodyAssembly,
    view: SectionView,
    pose: dict[str, float],
    mesh_tolerance: float,
) -> Image.Image:
    _validate_size(view.width, view.height)
    normal = _normalize(view.plane_normal, "plane_normal")
    origin = np.asarray(view.plane_origin, dtype=np.float64)
    if view.horizontal is None:
        reference = np.asarray((0.0, 0.0, 1.0))
        if abs(float(reference @ normal)) > 0.9:
            reference = np.asarray((1.0, 0.0, 0.0))
        horizontal = _normalize(np.cross(reference, normal), "horizontal")
    else:
        horizontal = _normalize(view.horizontal, "horizontal")
    vertical = (
        _normalize(np.cross(normal, horizontal), "vertical")
        if view.vertical is None
        else _normalize(view.vertical, "vertical")
    )
    if abs(float(horizontal @ normal)) > 1e-6 or abs(float(vertical @ normal)) > 1e-6:
        raise ValidationError("section horizontal and vertical axes must lie in the plane")

    meshes = _world_meshes(
        model,
        pose,
        mesh_tolerance,
        color_by=view.color_by,
        selected_parts=view.selected_parts,
        selected_shapes=view.selected_shapes,
    )
    projected_segments: list[tuple[np.ndarray, tuple[int, int, int]]] = []
    for item in meshes:
        segments = trimesh.intersections.mesh_plane(
            item.mesh,
            plane_normal=normal,
            plane_origin=origin,
        )
        for segment in np.asarray(segments):
            relative = segment - origin
            points = np.column_stack((relative @ horizontal, relative @ vertical))
            projected_segments.append((points, item.color))

    image = Image.new("RGB", (view.width, view.height), view.background)
    draw = ImageDraw.Draw(image)
    if not projected_segments:
        draw.text((20, 20), "No geometry crosses this section plane.", fill=(70, 70, 70))
        return image
    points = np.concatenate([segment for segment, _color in projected_segments], axis=0)
    scale, offset = _fit_projection(points, view.width, view.height)
    for segment, color in projected_segments:
        screen = [
            (float(point[0] * scale + offset[0]), float(-point[1] * scale + offset[1]))
            for point in segment
        ]
        draw.line(screen, fill=color, width=max(1, int(view.line_width)))
    if view.show_axes:
        center = (offset[0], offset[1])
        draw.line((20, center[1], view.width - 20, center[1]), fill=(145, 145, 145), width=1)
        draw.line((center[0], 20, center[0], view.height - 20), fill=(145, 145, 145), width=1)
    return image


def _render_motion_strip(
    model: RigidBodyAssembly,
    view: MotionStripView,
    base_pose: dict[str, float],
    mesh_tolerance: float,
) -> Image.Image:
    joint = model.get_joint(view.articulation)
    if not joint.dofs:
        raise ValidationError(
            f"motion strip joint {joint.name!r} is fixed; it has no motion to sweep"
        )
    if view.dof is not None:
        swept = joint.get_dof(view.dof)
    else:
        rotational = [dof for dof in joint.dofs if cast(JointAxis, dof.axis).is_rotational]
        swept = rotational[0] if rotational else joint.dofs[0]
    # Pose by the swept DOF's qualified id: a bare joint name resolves only
    # for single-DOF joints, and this one may carry several.
    dof_id = joint.dof_id(swept)
    positions = list(view.positions)
    if not positions:
        if swept.limits is not None:
            low, high = swept.limits
        else:
            low, high = 0.0, math.pi
        count = max(2, int(view.samples))
        positions = [
            min(max(low + (high - low) * index / (count - 1), low), high) for index in range(count)
        ]
    frames: list[Image.Image] = []
    fit_vertices: list[np.ndarray] = []
    for value in positions:
        pose = {**base_pose, dof_id: float(value)}
        meshes = _world_meshes(
            model,
            pose,
            mesh_tolerance,
            color_by=view.view.color_by,
            selected_parts=view.view.selected_parts,
            selected_shapes=view.view.selected_shapes,
        )
        fit_vertices.extend(np.asarray(item.mesh.vertices) for item in meshes)
    shared_fit = np.concatenate(fit_vertices, axis=0)
    for value in positions:
        pose = {**base_pose, dof_id: float(value)}
        frame = _render_model(
            model,
            view.view,
            pose,
            mesh_tolerance,
            fit_vertices=shared_fit,
        )
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, frame.width, 25), fill=(255, 255, 255))
        draw.text((8, 6), f"{dof_id} = {value:.4g}", fill=(25, 25, 25))
        frames.append(frame)
    width = sum(frame.width for frame in frames) + view.gap * (len(frames) - 1)
    height = max(frame.height for frame in frames)
    strip = Image.new("RGB", (width, height), view.background)
    x = 0
    for frame in frames:
        strip.paste(frame, (x, 0))
        x += frame.width + view.gap
    return strip


def _world_meshes(
    model: RigidBodyAssembly,
    pose: dict[str, float],
    mesh_tolerance: float,
    *,
    color_by: ColorMode,
    selected_parts: tuple[str, ...],
    selected_shapes: tuple[tuple[str, str], ...],
) -> list[_RenderMesh]:
    model.validate()
    selected = set(selected_parts)
    selected_shape_set = set(selected_shapes)
    transforms = MeshCollisionKernel(model, mesh_tolerance=mesh_tolerance).world_transforms(pose)
    rendered: list[_RenderMesh] = []
    for part in model.resolve().rigid_bodies:
        if selected and part.name not in selected:
            continue
        for shape in part._iter_shapes():
            if selected_shape_set and (part.name, shape.name) not in selected_shape_set:
                continue
            mesh = geometry_to_trimesh(shape.geometry, mesh_tolerance).copy()
            mesh.apply_transform(transforms[part.name])
            appearance = shape.display_material
            surface = _DEFAULT_SURFACE if appearance is None else _surface_of(appearance)
            if color_by == "material" and appearance is not None:
                color = _material_color(appearance)
            elif color_by == "shape":
                color = _stable_color(f"{part.name}/{shape.name}")
            else:
                color = _stable_color(part.name)
            rendered.append(_RenderMesh(part.name, shape.name, mesh, color, surface))
    if not rendered:
        raise ValidationError("visual selection did not match any geometry")
    return rendered


def _project_points(
    points: np.ndarray,
    center: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    forward: np.ndarray,
    *,
    projection: Projection,
    perspective_radius: float | None = None,
) -> np.ndarray:
    relative = np.asarray(points, dtype=np.float64) - center
    x = relative @ right
    y = relative @ up
    forward_distance = relative @ forward
    if projection == "orthographic":
        return np.column_stack((x, y, -forward_distance))
    if projection != "perspective":
        raise ValidationError("projection must be orthographic or perspective")
    radius = (
        max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-6)
        if perspective_radius is None
        else perspective_radius
    )
    camera_distance = radius * 2.8
    depth = np.maximum(camera_distance - forward_distance, radius * 0.05)
    focal = camera_distance
    return np.column_stack((x * focal / depth, y * focal / depth, depth))


def _shade_faces(
    normals: np.ndarray,
    view_direction: np.ndarray,
    color: tuple[int, int, int],
    surface: _Surface,
) -> np.ndarray:
    """Shade each face from the material's light response.

    Not a physically based renderer, and deliberately not trying to be: enough
    of one that roughness, metallic, clearcoat, and emissive become visible
    differences instead of invisible numbers. Metals get a constant environment
    reflection term because there is no environment map to reflect, and without
    it a metal surface would be black everywhere the highlight misses.

    Dot products are absolute, as they were in the flat renderer, so a face
    whose winding points away still shades rather than going black.
    """

    albedo = np.asarray(to_linear(component / 255.0 for component in color), dtype=np.float64)
    gloss = (1.0 - surface.roughness) ** 2
    shininess = 4.0 + 250.0 * gloss
    coat_gloss = (1.0 - surface.clearcoat_roughness) ** 2
    coat_shininess = 8.0 + 400.0 * coat_gloss

    diffuse = np.full(len(normals), _AMBIENT_STRENGTH)
    specular = np.zeros(len(normals))
    for direction, strength in ((_KEY_LIGHT, _KEY_STRENGTH), (_FILL_LIGHT, _FILL_STRENGTH)):
        light = _normalize(direction, "light")
        diffuse += strength * np.abs(normals @ light)
        highlight = np.abs(normals @ _unit(light + view_direction))
        specular += strength * (0.35 + 0.65 * gloss) * highlight**shininess
        if surface.clearcoat > 0.0:
            specular += strength * surface.clearcoat * 0.5 * highlight**coat_shininess

    reflectance = 0.04 + 0.96 * surface.metallic
    environment = 0.18 + 0.22 * gloss
    tint = albedo * surface.metallic + (1.0 - surface.metallic)
    shaded = albedo * (1.0 - surface.metallic) * diffuse[:, None]
    shaded = shaded + tint * (reflectance * (specular + environment))[:, None]
    if surface.emissive is not None:
        shaded = shaded + np.asarray(to_linear(surface.emissive), dtype=np.float64)
    # Back to display. Colors come in encoded and are linearized above, shading
    # keeps them linear, and writing those numbers straight into a PNG would
    # report a surface far darker than the one that was authored.
    display = np.clip(shaded, 0.0, 1.0) ** (1.0 / 2.2)
    return (display * 255.0).astype(np.uint8)


def _raster_triangle(
    image: np.ndarray,
    depth_buffer: np.ndarray,
    triangle: np.ndarray,
    color: np.ndarray,
    *,
    alpha: float = 1.0,
) -> None:
    height, width = depth_buffer.shape
    minimum = np.floor(triangle[:, :2].min(axis=0)).astype(int)
    maximum = np.ceil(triangle[:, :2].max(axis=0)).astype(int)
    x0, y0 = max(0, minimum[0]), max(0, minimum[1])
    x1, y1 = min(width - 1, maximum[0]), min(height - 1, maximum[1])
    if x1 < x0 or y1 < y0:
        return
    a, b, c = triangle
    denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(float(denominator)) <= 1e-12:
        return
    yy, xx = np.mgrid[y0 : y1 + 1, x0 : x1 + 1]
    px = xx + 0.5
    py = yy + 0.5
    wa = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / denominator
    wb = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / denominator
    wc = 1.0 - wa - wb
    inside = (wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9)
    if not np.any(inside):
        return
    depth = wa * a[2] + wb * b[2] + wc * c[2]
    current = depth_buffer[y0 : y1 + 1, x0 : x1 + 1]
    update = inside & (depth < current)
    region = image[y0 : y1 + 1, x0 : x1 + 1]
    if alpha >= 1.0:
        current[update] = depth[update]
        region[update] = color
        return
    blended = region[update] * (1.0 - alpha) + color * alpha
    region[update] = blended.astype(np.uint8)


def _fit_projection(
    points: np.ndarray,
    width: int,
    height: int,
    *,
    margin: float = 0.08,
) -> tuple[float, tuple[float, float]]:
    minimum = points.min(axis=0)
    maximum = points.max(axis=0)
    span = np.maximum(maximum - minimum, 1e-9)
    usable_width = width * (1.0 - margin * 2.0)
    usable_height = height * (1.0 - margin * 2.0)
    scale = min(usable_width / span[0], usable_height / span[1])
    center = (minimum + maximum) * 0.5
    return float(scale), (width * 0.5 - center[0] * scale, height * 0.5 + center[1] * scale)


def _camera_basis(direction: Vec3, up: Vec3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward = _normalize(direction, "direction")
    up_vector = _normalize(up, "up")
    right = np.cross(up_vector, forward)
    if np.linalg.norm(right) <= 1e-9:
        raise ValidationError("view direction and up must not be parallel")
    right /= np.linalg.norm(right)
    camera_up = np.cross(forward, right)
    return forward, right, camera_up


def _draw_bounds(draw: ImageDraw.ImageDraw, corners: np.ndarray) -> None:
    edges = (
        (0, 1),
        (0, 2),
        (0, 4),
        (1, 3),
        (1, 5),
        (2, 3),
        (2, 6),
        (3, 7),
        (4, 5),
        (4, 6),
        (5, 7),
        (6, 7),
    )
    for start, end in edges:
        draw.line(
            (
                float(corners[start, 0]),
                float(corners[start, 1]),
                float(corners[end, 0]),
                float(corners[end, 1]),
            ),
            fill=(45, 45, 45),
            width=1,
        )


def _draw_joints(
    draw: ImageDraw.ImageDraw,
    model: RigidBodyAssembly,
    pose: dict[str, float],
    center: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    forward: np.ndarray,
    projection: Projection,
    scale: float,
    offset: tuple[float, float],
    perspective_radius: float,
) -> None:
    transforms = MeshCollisionKernel(model, mesh_tolerance=DEFAULT_MESH_TOLERANCE).world_transforms(
        pose
    )
    for item in model.resolve().joints:
        joint = item.joint
        if joint.body0 is WORLD:
            continue
        parent = transforms.get(cast(RigidBody, joint.body0).name)
        if parent is None:
            continue
        rotational = [dof for dof in joint.dofs if cast(JointAxis, dof.axis).is_rotational]
        if not rotational:
            continue
        frame = parent @ _frame_matrix(joint.frame0)
        point = frame[:3, 3]
        unit = [0.0, 0.0, 0.0]
        unit[cast(JointAxis, rotational[0].axis).component] = 1.0
        axis = frame[:3, :3] @ np.asarray(unit, dtype=np.float64)
        length = max(0.02, 16.0 / max(scale, 1e-9))
        world = np.asarray([point - axis * length, point + axis * length])
        screen = _project_points(
            world,
            center,
            right,
            up,
            forward,
            projection=projection,
            perspective_radius=perspective_radius,
        )
        screen[:, 0] = screen[:, 0] * scale + offset[0]
        screen[:, 1] = -screen[:, 1] * scale + offset[1]
        draw.line(tuple(screen[:, :2].reshape(-1)), fill=(210, 45, 45), width=3)
        x, y = float(screen[:, 0].mean()), float(screen[:, 1].mean())
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(210, 45, 45))


def _draw_overlays(
    draw: ImageDraw.ImageDraw,
    view: ModelView,
    center: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    forward: np.ndarray,
    scale: float,
    offset: tuple[float, float],
    perspective_radius: float,
) -> None:
    for line in view.lines:
        world = np.asarray((line.start, line.end), dtype=np.float64)
        screen = _project_points(
            world,
            center,
            right,
            up,
            forward,
            projection=view.projection,
            perspective_radius=perspective_radius,
        )
        screen[:, 0] = screen[:, 0] * scale + offset[0]
        screen[:, 1] = -screen[:, 1] * scale + offset[1]
        draw.line(
            tuple(screen[:, :2].reshape(-1)),
            fill=line.color,
            width=max(1, int(line.width)),
        )
        if line.label:
            draw.text(
                (float(screen[1, 0]) + 4, float(screen[1, 1]) + 4),
                line.label,
                fill=line.color,
            )
    for point in view.points:
        world = np.asarray((point.position,), dtype=np.float64)
        screen = _project_points(
            world,
            center,
            right,
            up,
            forward,
            projection=view.projection,
            perspective_radius=perspective_radius,
        )[0]
        x = float(screen[0] * scale + offset[0])
        y = float(-screen[1] * scale + offset[1])
        radius = max(1, int(point.radius))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=point.color)
        if point.label:
            draw.text((x + radius + 3, y - radius), point.label, fill=point.color)


def _draw_reticle(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    reticle: Reticle,
) -> None:
    x, y = reticle.point.pixel(width, height)
    radius = max(7, round(min(width, height) * 0.012))
    line = max(2, round(radius / 5))
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius), outline=reticle.color, width=line
    )
    draw.line((x - radius * 1.5, y, x - radius * 0.45, y), fill=reticle.color, width=line)
    draw.line((x + radius * 0.45, y, x + radius * 1.5, y), fill=reticle.color, width=line)
    draw.line((x, y - radius * 1.5, x, y - radius * 0.45), fill=reticle.color, width=line)
    draw.line((x, y + radius * 0.45, x, y + radius * 1.5), fill=reticle.color, width=line)
    if reticle.label:
        box = draw.textbbox((0, 0), reticle.label)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        text_x = min(max(2.0, x + radius + 5), max(2.0, width - text_width - 8.0))
        text_y = min(max(2.0, y + radius + 3), max(2.0, height - text_height - 8.0))
        draw.rectangle(
            (text_x - 3, text_y - 3, text_x + text_width + 3, text_y + text_height + 3),
            fill=(255, 255, 255),
            outline=reticle.color,
            width=1,
        )
        draw.text((text_x, text_y), reticle.label, fill=(20, 20, 20))


def _bounds_corners(minimum: np.ndarray, maximum: np.ndarray) -> np.ndarray:
    return np.asarray(
        [
            (x, y, z)
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        ],
        dtype=np.float64,
    )


def _stable_color(name: str) -> tuple[int, int, int]:
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return cast(tuple[int, int, int], tuple(75 + int(value) % 145 for value in digest[:3]))


def _surface_of(material: Material) -> _Surface:
    """The material's light response.

    The authored numbers are used as they are, because export recolors a
    texture set so its averages match them. A preview cannot show the grain a
    texture adds, but the color and finish it shades with are the ones the
    exported USDZ will average out to.
    """

    return _Surface(
        metallic=material.metallic,
        roughness=material.roughness,
        clearcoat=material.clearcoat,
        clearcoat_roughness=material.clearcoat_roughness,
        emissive=material.emissive,
        opacity=material.opacity,
    )


def _material_color(material: Material) -> tuple[int, int, int]:
    return cast(
        tuple[int, int, int], tuple(round(value * 255) for value in material.base_color[:3])
    )


def _normalize(value: Vec3 | np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise ValidationError(f"{name} must contain 3 finite values")
    length = float(np.linalg.norm(array))
    if length <= 0.0:
        raise ValidationError(f"{name} must be non-zero")
    return array / length


def _unit(value: np.ndarray) -> np.ndarray:
    """Normalize a vector that shading built, where a zero length is harmless."""

    length = float(np.linalg.norm(value))
    return value if length <= 1e-12 else value / length


def _validate_size(width: int, height: int) -> None:
    if not 64 <= int(width) <= 4096 or not 64 <= int(height) <= 4096:
        raise ValidationError("visual width and height must be between 64 and 4096 pixels")


__all__ = [
    "ImagePoint",
    "LineOverlay",
    "MeridionalSectionView",
    "ModelView",
    "MotionStripView",
    "PointOverlay",
    "Reticle",
    "SectionView",
    "VisualSpec",
    "annotate_image",
    "render_view",
]
