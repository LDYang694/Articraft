# Assemblies and rigid bodies

`RigidBodyAssembly` is the authored physical asset. It owns `RigidBody` values,
physical joints, and optional articulation trees.

```python
from articraft.sdk import Material, RigidBodyAssembly


model = RigidBodyAssembly("small_table")
body = model.rigid_body("body")
body.add(Box(0.8, 0.5, 0.04), name="top", material=Material.HARDWOOD)
```

All geometry and linear physics values use meters.

## `RigidBodyAssembly`

```python
RigidBodyAssembly(name: str, *, scene: PhysicsScene = PhysicsScene())
```

Assembly, rigid body, joint, and articulation names must be identifiers --
letters, digits, and underscores, not starting with a digit -- because they
travel verbatim into USD prim paths, DOF ids, and the manifest. Shape names
are display labels and may be any nonempty string. The assembly exposes its
authored `rigid_bodies`, `joints`, and `articulations` for inspection. Create
entries through `rigid_body(...)`, `joint(...)`, and `articulation(...)` so
references are resolved immediately and duplicate names fail where they are
authored.

```python
model.rigid_body(
    name: str,
    *,
    mass_properties: MassProperties | None = None,
    body_state: BodyState | None = None,
) -> RigidBody
```

`PhysicsScene` sets gravity for the exported stage. `MassProperties` overrides
mass inferred from shape geometry and `Material`. A body's optional `BodyState`
sets its initial rigid-body flags and velocity; see
[simulation settings](38_simulation_settings.md).

## `RigidBody` and `body.add(...)`

One `RigidBody` contains all geometry that moves together. Use another body
only when the geometry needs independent rigid motion.

```python
body.add(
    shape: build123d.Shape | MeshGeometry,
    *,
    name: str,
    material: Material | None = None,
    coating: Material | None = None,
    color: Sequence[float] | None = None,
) -> build123d.Shape | MeshGeometry
```

Shape names are unique within one body. Build123d and mesh geometry both use
the body's local coordinates; apply `Pos`, `Rot`, `Location`, or a mesh
transform before calling `add(...)`. There is no second per-shape transform.

`material` supplies density, contact properties, and appearance. `coating`
changes the surface material without replacing the core density. `color`
tints the displayed surface.

```python
from build123d import Box, Pos

housing = model.rigid_body("housing")
housing.add(Box(0.30, 0.22, 0.08), name="shell", material=Material.ALUMINUM)
housing.add(
    Pos(X=0.12) * Box(0.08, 0.02, 0.02),
    name="handle",
    material=Material.ABS_PLASTIC,
)
```

Overlapping shapes within one body are allowed and count as connected. Extend
a protrusion's own end slightly into the surface it meets instead of adding a
decorative patch to hide a gap.

Use `body.shape(name)` to retrieve an authored shape and
`model.get_rigid_body(body_or_name)` to retrieve a body.

## Geometry-anchored frames

```python
body.at(source=None) -> BodyFrame
```

`body.at(...)` binds a local frame to that body. `source` may be a three-number
point, `JointFrame`, build123d `Location`, `Plane`, `Axis`, `Face`, `Edge`,
`Vertex`, or `Shape`, or a `MeshGeometry`. Faces use their center and normal;
edges use their center and tangent; axes use their position and direction. The
feature direction becomes frame-local Z. Whole shapes and meshes use their
bounding-box center. Build123d locations are converted from their exact
transform, including their degree-based rotation convention.

Use build123d's ordinary topology queries instead of copying coordinates:

```python
from build123d import Axis

top = housing_shape.faces().sort_by(Axis.Z)[-1]
top_frame = housing_body.at(top)
hinge_axis = lid_body.at(lid_shape.edges().filter_by(Axis.X)[0])
```

`WORLD.at(...)` creates the world endpoint. A raw `JointFrame` is the compact
USD-local value used when exact numbers are already the clearest input.

```python
model.frame_in(source: BodyFrame, body: RigidBody | WORLD = WORLD) -> BodyFrame
```

`frame_in(...)` expresses the same reference-state frame in another body's
coordinates. It is useful for deriving the second endpoint of a closure or
fixed mount without repeating transform arithmetic.

## Resolution

```python
resolved = model.resolve()
```

`resolve()` validates the authored graph and returns an immutable
`ResolvedRigidBodyAssembly`. Export, collision, testing, rendering, and
kinematics consume this same resolved view. It contains:

- every rigid body and physical joint;
- each validated articulation tree;
- the derived `exclude_from_articulation` status of every joint;
- the validated zero-configuration `reference_state`;
- the assembly `PhysicsScene`.

Do not author `exclude_from_articulation` yourself. A selected articulation
joint is included; another joint incident to an articulated body is exported as
an excluded regular constraint.

## Physics states

`PhysicsState` stores one world transform per body. Body poses are authoritative;
DOF positions are optional metadata checked against those poses.

```python
state = model.physics_state(
    {
        base: JointFrame(xyz=(0.0, 0.0, 0.0)),
        lid: lid_world_matrix,
    }
)
```

`resolved.forward_kinematics({...})` is the convenience path for an articulation
tree. In a closed loop, supplied coordinates drive and unspecified coordinates
are solved to keep excluded constraints closed. Use a complete `PhysicsState`
for maximal-coordinate graphs, multiple trees without one spanning kinematic
tree, or poses supplied by a physics backend.

## Validation

`model.validate()` calls `resolve()` and returns `None` on success. Validation
requires:

- at least one body, with a unique nonempty name and one valid named shape;
- unique joint and articulation names;
- finite joint frames and valid endpoints;
- one connected physical joint graph;
- each selected articulation to be a connected acyclic tree;
- each body and selected joint to belong to at most one articulation;
- every joint constraint and DOF limit to hold in the reference state.

Zero articulations are valid for a maximal-coordinate USD assembly. Multiple
non-overlapping articulations are also valid; consumers that cannot derive one
complete pose from their trees require a complete `PhysicsState`.

## USD layout

Each body is a sibling rigid-body prim. Named shapes remain children of their
body:

```text
/World/<assembly>/rigid_bodies/<body>/shapes/<shape>
```

Joints live under `/World/<assembly>/joints`. This flat physical layout does
not encode a parent/child hierarchy; an articulation is solver configuration,
not asset ownership.
