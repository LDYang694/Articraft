# Joints and articulations

A `Joint` is a physical constraint. An `Articulation` is a reduced-coordinate
tree selected from those joints. Keeping them separate is what lets one physical
assembly contain closed loops.

```python
from articraft.sdk import WORLD, BodyFrame, JointAxis, JointDOF, JointFrame
```

## `JointFrame`

```python
JointFrame(
    xyz: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0),
)
```

Every joint has one local frame per endpoint. The two frames coincide in the
authored zero configuration. `xyz` is meters and `rpy` is extrinsic XYZ roll,
pitch, and yaw in radians.

Normally create a `BodyFrame` with `body.at(...)` or `WORLD.at(...)`. This binds
the local `JointFrame` to its endpoint before the joint is authored. Points and
build123d locations, planes, axes, faces, edges, and vertices are accepted. An
axis direction, face normal, or edge tangent becomes frame-local Z; use
`ROT_Z` or `TRANS_Z` to move along that selected feature.

Rotating a frame expresses an axis that is not aligned with the body's axes. To
hinge about a diagonal in the XY plane, yaw the frame and use its local X axis.

## `JointDOF` and `JointAxis`

```python
JointDOF(axis: JointAxis, limits: tuple[float, float] | None = None)
```

`JointAxis` names the six USD D6 axes: `TRANS_X`, `TRANS_Y`, `TRANS_Z`,
`ROT_X`, `ROT_Y`, and `ROT_Z`. Listed axes are free or limited; every unlisted
axis is locked.

Limits use meters for translation and radians for rotation. Every range must
contain zero because the joint frames define the zero configuration. Omit
limits for an unbounded axis.

## `model.joint(...)`

```python
model.joint(
    name: str,
    endpoint0: BodyFrame,
    endpoint1: BodyFrame,
    *,
    dofs: Iterable[JointDOF] = (),
) -> Joint
```

The endpoints are symmetric; neither means parent. One may come from
`WORLD.at(...)`. A joint cannot connect `WORLD` to itself.

| DOFs | Physical joint |
| --- | --- |
| none | fixed |
| one rotational axis | revolute |
| one translational axis | prismatic |
| any other combination | generic D6 |

```python
lid_hinge = model.joint(
    "lid_hinge",
    base.at((0.0, -0.04, 0.02)),
    lid.at((0.0, -0.04, 0.0)),
    dofs=(JointDOF(JointAxis.ROT_X, limits=(0.0, 1.57)),),
)
```

## `model.articulation(...)`

```python
model.articulation(
    name: str,
    *,
    root: RigidBody | Joint | str,
    joints: Iterable[Joint | str] | None = None,
) -> Articulation
```

A floating articulation roots at a body. A fixed articulation roots at a
selected joint connecting one body to `WORLD`. A floating articulation may
not *select* a joint that touches `WORLD`: to anchor an assembly, make that
joint the articulation root instead of listing it in `joints`.

The selected joints must form one connected acyclic tree. Selection can be
inferred only when the assembly has one articulation and one unambiguous
acyclic physical graph. Cycles and multiple articulations require an explicit
joint list.

Bodies may belong to at most one articulation. A physical joint may be selected
by at most one articulation.

## Closed loops

A four-bar has four physical joints but its reduced-coordinate tree has three.
Author all four constraints and select three:

```python
swing = (JointDOF(JointAxis.ROT_Y, limits=(-1.5, 1.5)),)

ground_crank = model.joint(
    "ground_crank",
    ground.at(),
    crank.at(),
    dofs=swing,
)
crank_coupler = model.joint(
    "crank_coupler",
    crank.at((CRANK_LENGTH, 0.0, 0.0)),
    coupler.at(),
    dofs=swing,
)
ground_rocker = model.joint(
    "ground_rocker",
    ground.at((GROUND_SPAN, 0.0, 0.0)),
    rocker.at(),
    dofs=swing,
)

model.articulation(
    "main",
    root=ground,
    joints=(ground_crank, crank_coupler, ground_rocker),
)

coupler_tip = coupler.at((COUPLER_LENGTH, 0.0, 0.0))
model.joint(
    "closing_pin",
    coupler_tip,
    model.frame_in(coupler_tip, rocker),
    dofs=swing,
)
```

`closing_pin` remains a physical USD constraint. Resolution derives
`exclude_from_articulation=True`, and export authors
`physics:excludeFromArticulation = true`.

Do not pose the closing joint directly; its value follows from the body poses.
Supply a tree DOF to `forward_kinematics(...)` or `TestContext.pose(...)` and
the graph solver determines unspecified coordinates needed to close the ring.
An unreachable pose raises `LoopClosureError`.

When the intended rocker pin is a build123d edge or axis, also check it with
`TestContext.expect_coaxial(...)`. This proves that the exact derived closure
still lands on the authored geometry feature.

```python
state = model.resolve().forward_kinematics({"ground_crank.rotY": 0.4})
```

Positions are keyed by DOF id: the joint name, a dot, and the `JointAxis`
value (`"ground_crank.rotY"`, `"slide.transX"`). The same ids appear in
`PhysicsState.dof_positions`, including the derived values of loop-closing
joints -- a free check that the ring stayed closed.

The solver uses independent D6 coordinates and the authored constraints. It is
a convenience for posing and geometry checks, not a dynamics backend. Closed-
loop simulation stability still depends on the USD physics backend, timestep,
and solver configuration.

## Authoritative states

```python
PhysicsState(body_poses, *, dof_positions=None)
```

`body_poses` maps every body name to a 4x4 world transform. These transforms are
authoritative. Resolution decomposes each endpoint-relative transform in
canonical D6 order, rejects locked-axis or limit violations, and derives DOF
metadata.

Use a complete state when poses come from a simulator or when one articulation
tree cannot determine the whole assembly:

```python
state = model.physics_state(body_world_transforms)
with TestContext(model).state(state):
    ...
```

Maximal-coordinate assemblies need no articulation. They export every joint as
an ordinary constraint; their arbitrary runtime pose is naturally represented
as a complete `PhysicsState`.
