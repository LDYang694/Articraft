# Shared units and types

## Units and coordinates

Articraft geometry uses meters. Build123d dimensions, mesh vertices,
`JointFrame.xyz`, translational DOFs, bounds, distances, and tolerances all use
meters. The SDK and exported USD stages use right-handed XYZ coordinates with Z
up.

`JointFrame.rpy`, rotational DOFs, rotational limits, and mesh rotations use
radians. Build123d `Rot(...)` uses degrees; convert when moving angles between
the APIs.

## Geometry

`RigidBody.add(...)` accepts a build123d `Shape` or `MeshGeometry`. Both use the
body-local frame. Every shape has a unique name within its body and may carry a
`Material`, `coating`, and RGB/RGBA `color`.

Read [assemblies and rigid bodies](30_assembly.md) for structure and
[mesh geometry](../mesh/00_mesh_geometry.md) for procedural mesh authoring.

## Physics graphs

`BodyFrame`, `JointFrame`, `JointDOF`, `JointAxis`, and `Joint` describe physical
constraints. A `BodyFrame` binds local coordinates to a `RigidBody` or `WORLD`,
so a joint endpoint cannot accidentally pair one body's frame with another body.
`Articulation` independently selects a reduced-coordinate tree. `PhysicsState`
stores authoritative world-space body poses.

Read [joints and articulations](35_joints.md) for frames, D6 axes, loops, and
kinematics.

## Testing and errors

`TestContext` records authored checks and `TestReport` contains their failures,
warnings, metrics, and justified allowances. A context can inspect either a
tree pose or a complete `PhysicsState`.

The public errors are `SDKError`, `ValidationError`, and `LoopClosureError`.
Read [errors](10_errors.md) and [testing](40_testing.md).

## Export

`export_assembly(...)` writes a validated USDZ plus manifest schema version 2.
Read [USDZ export](50_usdz_export.md) for the body, joint, articulation, and
state layout.
