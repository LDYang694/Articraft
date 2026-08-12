# Errors

The public SDK exports `SDKError`, `ValidationError`, and `LoopClosureError`.

```python
from articraft.sdk import LoopClosureError, SDKError, ValidationError
```

## `SDKError`

Base class for errors defined by the Articraft SDK. Catch it only when a caller
can handle any SDK validation failure in one place.

## `ValidationError`

Raised when an assembly, rigid body, shape, joint, articulation, physics state,
or named test selector violates the public contract. Examples include:

- an empty or duplicate name;
- invalid build123d or mesh geometry;
- a joint endpoint outside its assembly;
- a disconnected physical graph;
- a cyclic or disconnected selected articulation tree;
- a joint limit that excludes the frame-defined zero configuration;
- a `PhysicsState` that violates a locked D6 axis or limit.

Value objects validate when they are constructed. `RigidBody.add(...)` validates
the shape it stores, and `model.joint(...)` resolves its endpoints immediately.
`model.validate()` calls `resolve()` and checks the whole graph, every body and
shape, every articulation tree, and the reference state.

```python
try:
    model.validate()
except ValidationError as exc:
    print(exc)
```

Authored assertions do not raise. `TestContext.check(...)` records failures in
its `TestReport`.

## `LoopClosureError`

A `ValidationError` raised when supplied tree coordinates cannot satisfy every
closed-loop constraint. The message says which of two causes applies. When it
names solved joint positions *pinned at their limits*, those limits are what
stop the loop: widen them if the pose should be reachable, or tighten the
driving DOF's limits so the unreachable pose is never requested. Otherwise the
linkage geometry itself cannot reach the pose: shorten the drive's range, or
fix the link lengths and joint frames that decide it.

## Built-in Python errors

Public helpers may use ordinary errors where appropriate:

- `TypeError` for a missing required argument or incompatible Python value;
- `ValueError` for an impossible dimension, zero transform axis, invalid
  profile, failed mesh boolean, or empty allowance reason;
- `FileNotFoundError` and other `OSError` subclasses for external assets.

Catch the narrow error an operation documents. Do not catch an error merely to
continue with geometry that did not build correctly.

## Lookups

Lookup helpers raise `ValidationError` when a name cannot be resolved:

```python
body = model.get_rigid_body("body")
shape = body.get_shape("housing")
joint = model.get_joint("body_to_lid")
```

Body and joint names are assembly-scoped. Shape names are body-scoped.

Import public values from `articraft.sdk`; documentation paths such as
`docs/sdk/common/20_core_types.md` are not Python modules.
