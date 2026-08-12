from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias, cast

import numpy as np
import trimesh
from build123d import Axis, Location, Plane, Vector
from build123d.topology import Edge, Face, Shape, Vertex

from articraft.sdk._mesh.core import MeshGeometry
from articraft.sdk.errors import ValidationError

if TYPE_CHECKING:
    from articraft.sdk.bodies import RigidBody

Vec3: TypeAlias = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class JointFrame:
    """A USD joint frame local to one rigid-body endpoint, in metres and radians."""

    xyz: Vec3 = (0.0, 0.0, 0.0)
    rpy: Vec3 = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "xyz", _vec3(self.xyz, field_name="joint frame xyz"))
        object.__setattr__(self, "rpy", _vec3(self.rpy, field_name="joint frame rpy"))


FrameSource: TypeAlias = (
    JointFrame | Location | Plane | Axis | Vector | Shape | MeshGeometry | Sequence[float] | None
)


@dataclass(frozen=True, slots=True)
class _WorldEndpoint:
    def at(self, source: FrameSource = None) -> BodyFrame:
        """Bind a frame to the USD world endpoint."""

        return BodyFrame(self, _as_joint_frame(source))

    def __repr__(self) -> str:
        return "WORLD"


WORLD = _WorldEndpoint()
"""The USD world endpoint. A joint may connect one rigid body to ``WORLD``."""


@dataclass(frozen=True, slots=True)
class BodyFrame:
    """A joint frame bound to the rigid body whose coordinates it uses."""

    body: RigidBody | _WorldEndpoint
    frame: JointFrame = field(default_factory=JointFrame)

    def __post_init__(self) -> None:
        from articraft.sdk.bodies import RigidBody

        if self.body is not WORLD and not isinstance(self.body, RigidBody):
            raise ValidationError("body frame must belong to a RigidBody or WORLD")
        if not isinstance(self.frame, JointFrame):
            raise ValidationError("body frame must contain a JointFrame")

    @property
    def xyz(self) -> Vec3:
        return self.frame.xyz

    @property
    def rpy(self) -> Vec3:
        return self.frame.rpy


def _as_joint_frame(source: FrameSource) -> JointFrame:
    if source is None:
        return JointFrame()
    if isinstance(source, JointFrame):
        return source
    if isinstance(source, Plane | Axis):
        return _location_frame(source.location)
    if isinstance(source, Location):
        return _location_frame(source)
    if isinstance(source, Face):
        return _location_frame(Plane(origin=source.center(), z_dir=source.normal_at()).location)
    if isinstance(source, Edge):
        return _location_frame(Axis(source.center(), source.tangent_at()).location)
    if isinstance(source, Vertex):
        return JointFrame(xyz=_vec3(source.center(), field_name="vertex center"))
    if isinstance(source, Shape):
        return JointFrame(
            xyz=_vec3(source.bounding_box().center(), field_name="shape bounds center")
        )
    if isinstance(source, MeshGeometry):
        source.validate()
        if not source.vertices:
            raise ValidationError("mesh frame source must contain vertices")
        vertices = np.asarray(source.vertices, dtype=np.float64)
        center = (vertices.min(axis=0) + vertices.max(axis=0)) * 0.5
        return JointFrame(xyz=_vec3(center, field_name="mesh bounds center"))
    return JointFrame(xyz=_vec3(source, field_name="body frame point"))


def _location_frame(location: Location) -> JointFrame:
    transform = location.wrapped.Transformation()
    matrix = np.identity(4, dtype=np.float64)
    for row in range(3):
        for column in range(4):
            matrix[row, column] = transform.Value(row + 1, column + 1)
    orientation = trimesh.transformations.euler_from_matrix(matrix, axes="sxyz")
    return JointFrame(
        xyz=(float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3])),
        rpy=(
            float(orientation[0]),
            float(orientation[1]),
            float(orientation[2]),
        ),
    )


def _vec3(value: Iterable[float], *, field_name: str) -> Vec3:
    if isinstance(value, (str, bytes)):
        raise ValidationError(f"{field_name} must have 3 numeric values")
    try:
        values = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field_name} must have 3 numeric values") from exc
    if len(values) != 3 or any(not math.isfinite(component) for component in values):
        raise ValidationError(f"{field_name} must have 3 finite numeric values")
    return cast(Vec3, values)


__all__ = ["WORLD", "BodyFrame", "FrameSource", "JointFrame"]
