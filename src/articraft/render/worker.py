"""Path-trace an exported USDZ. Runs inside Blender, not inside this package.

Blender ships its own Python, so this module is executed by ``blender -b -P``
and can import nothing from articraft. Everything it needs arrives on argv.

The agent's own previews come from the rasterizer in `sdk/visual.py`, which is
fast and flat on purpose. This is the other end of that trade: the surface a
material actually produces, with its texture, its roughness, and light that
bounces. It is what the appearance critic grades.

    blender -b -P articraft/render/worker.py -- <usdz> <out-dir> [options]
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import bpy
from mathutils import Vector

# Azimuth and elevation in degrees, measured from the front of the object.
VIEWS: dict[str, tuple[float, float]] = {
    "hero": (35.0, 18.0),
    "front": (0.0, 8.0),
    "side": (90.0, 12.0),
    "high": (-40.0, 42.0),
}


@dataclass
class Bounds:
    center: Vector
    size: Vector

    @property
    def radius(self) -> float:
        return self.size.length / 2.0

    def corners(self) -> list[Vector]:
        half = self.size / 2.0
        return [
            self.center + Vector((x * half.x, y * half.y, z * half.z))
            for x in (-1, 1)
            for y in (-1, 1)
            for z in (-1, 1)
        ]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="render a usdz with cycles")
    parser.add_argument("usdz", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--views", nargs="+", default=["hero", "front"], choices=list(VIEWS))
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--resolution", type=int, nargs=2, default=(1024, 768))
    parser.add_argument("--device", default="OPTIX", choices=("OPTIX", "CUDA", "CPU"))
    return parser.parse_args(argv)


def unpack(usdz: Path, into: Path) -> Path:
    """Extract the package so the stage resolves its textures as plain files."""
    with zipfile.ZipFile(usdz) as package:
        package.extractall(into)
    stages = sorted(into.glob("*.usdc")) + sorted(into.glob("*.usda")) + sorted(into.glob("*.usd"))
    if not stages:
        raise SystemExit(f"{usdz} holds no usd stage")
    return stages[0]


def mesh_bounds() -> Bounds:
    lo = Vector((math.inf,) * 3)
    hi = Vector((-math.inf,) * 3)
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            lo = Vector(map(min, lo, world))
            hi = Vector(map(max, hi, world))
    if not all(map(math.isfinite, lo)):
        raise SystemExit("the stage imported no meshes")
    return Bounds(center=(lo + hi) / 2.0, size=hi - lo)


def add_backdrop(bounds: Bounds) -> None:
    """A large soft floor, so the object sits on something and casts shadows."""
    span = max(bounds.radius * 20.0, 4.0)
    bpy.ops.mesh.primitive_plane_add(
        size=span, location=(bounds.center.x, bounds.center.y, bounds.center.z - bounds.size.z / 2)
    )
    floor = bpy.context.active_object
    floor.name = "backdrop"

    material = bpy.data.materials.new("backdrop")
    material.use_nodes = True
    surface = material.node_tree.nodes["Principled BSDF"]
    surface.inputs["Base Color"].default_value = (0.62, 0.62, 0.63, 1.0)
    surface.inputs["Roughness"].default_value = 0.6
    floor.data.materials.append(material)


def add_world() -> None:
    world = bpy.data.worlds.new("studio")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (0.24, 0.245, 0.25, 1.0)
    background.inputs["Strength"].default_value = 1.0
    bpy.context.scene.world = world


def aim(obj: bpy.types.Object, target: Vector) -> None:
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def add_light(name: str, offset: Vector, bounds: Bounds, size: float, power: float) -> None:
    """Place an area light relative to the object and aim it at the center.

    Power scales with the object's size because an area light falls off with
    distance, and the camera backs off for a large object. Without the scaling
    a cabinet renders black and a bolt renders blown out.
    """

    reach = max(bounds.radius, 0.2)
    light_data = bpy.data.lights.new(name, type="AREA")
    light_data.shape = "RECTANGLE"
    light_data.size = size * reach
    light_data.size_y = light_data.size * 0.7
    light_data.energy = power * reach**2
    light = bpy.data.objects.new(name, light_data)
    light.location = bounds.center + offset * reach
    bpy.context.scene.collection.objects.link(light)
    aim(light, bounds.center)


def add_lights(bounds: Bounds) -> None:
    add_light("key", Vector((2.4, -3.0, 3.2)), bounds, size=6.0, power=226.0)
    add_light("fill", Vector((-3.4, -1.6, 1.2)), bounds, size=8.0, power=96.0)
    add_light("rim", Vector((-1.2, 3.6, 2.4)), bounds, size=5.0, power=104.0)


def add_camera() -> bpy.types.Object:
    camera_data = bpy.data.cameras.new("camera")
    camera_data.lens = 55.0
    camera = bpy.data.objects.new("camera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    return camera


def frame(camera: bpy.types.Object, bounds: Bounds, azimuth: float, elevation: float) -> None:
    """Put the camera on the given orbit angle and back it off until all of it fits."""
    yaw, pitch = math.radians(azimuth), math.radians(elevation)
    direction = Vector(
        (math.sin(yaw) * math.cos(pitch), -math.cos(yaw) * math.cos(pitch), math.sin(pitch))
    ).normalized()

    camera.location = bounds.center + direction
    aim(camera, bounds.center)
    forward = -(camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0)))
    right = camera.matrix_world.to_quaternion() @ Vector((1.0, 0.0, 0.0))
    up = camera.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))

    scene = bpy.context.scene
    aspect = scene.render.resolution_x / scene.render.resolution_y
    half_v = math.atan(math.tan(camera.data.angle / 2.0) / max(aspect, 1.0))
    half_h = math.atan(math.tan(half_v) * aspect)

    distance = 0.0
    for corner in bounds.corners():
        offset = corner - bounds.center
        depth = offset.dot(forward)
        for extent, half_angle in ((abs(offset.dot(right)), half_h), (abs(offset.dot(up)), half_v)):
            distance = max(distance, extent / math.tan(half_angle) - depth)

    camera.location = bounds.center + direction * distance * 1.12
    aim(camera, bounds.center)


def configure_render(args: argparse.Namespace) -> None:
    scene = bpy.context.scene
    # Blender defaults to AgX, a filmic transform that lifts shadows, rolls off
    # highlights, and desaturates. It flatters a picture and ruins a
    # measurement: a 0.20 surface came back as 0.34 and a 0.80 one as 0.81, so
    # every dark material read lighter and less saturated than it was authored.
    # These renders are read as evidence about a material, so they go out
    # untone-mapped.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.engine = "CYCLES"
    scene.render.resolution_x, scene.render.resolution_y = args.resolution
    scene.render.image_settings.file_format = "PNG"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.cycles.device = "CPU" if args.device == "CPU" else "GPU"
    if args.device == "CPU":
        return

    preferences = bpy.context.preferences.addons["cycles"].preferences
    preferences.compute_device_type = args.device
    preferences.get_devices()
    found = [device for device in preferences.devices if device.type == args.device]
    for device in preferences.devices:
        device.use = device.type == args.device
    if not found:
        print(f"no {args.device} device found, falling back to CPU")
        scene.cycles.device = "CPU"


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="articraft-render-"))
    try:
        stage = unpack(args.usdz, staging)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.usd_import(filepath=str(stage), import_textures_mode="IMPORT_PACK")

        bounds = mesh_bounds()
        add_world()
        add_backdrop(bounds)
        add_lights(bounds)
        camera = add_camera()
        configure_render(args)

        for name in args.views:
            azimuth, elevation = VIEWS[name]
            frame(camera, bounds, azimuth, elevation)
            bpy.context.scene.render.filepath = str(args.out / f"{name}.png")
            bpy.ops.render.render(write_still=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
