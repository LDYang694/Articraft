# USDZ export

```python
from articraft.sdk.export import export_assembly


result = export_assembly(object_model, "output")
```

`export_assembly(...)` resolves and validates a `RigidBodyAssembly`, writes the
next numbered USDZ under `output/usdz/`, and atomically replaces
`output/model.json`. It returns `AssemblyExportResult` with paths, texture
results, and an `AssemblyExportAudit`.

## Stage layout

```text
/World/physicsScene
/World/<assembly>                         kind=assembly
/World/<assembly>/rigid_bodies/<body>
/World/<assembly>/rigid_bodies/<body>/shapes/<shape>
/World/<assembly>/joints/<joint>
```

Rigid bodies are siblings with their reference-state world transforms. Every
physical joint exports both local endpoint frames and targets its two bodies.

- zero DOFs use `UsdPhysics.FixedJoint`;
- one rotational DOF uses `UsdPhysics.RevoluteJoint`;
- one translational DOF uses `UsdPhysics.PrismaticJoint`;
- other combinations use `UsdPhysics.Joint` with per-axis `LimitAPI` schemas.

Rotational limits are converted from SDK radians to USD degrees. Linear limits
remain meters. Unlisted D6 axes are locked.

`UsdPhysics.ArticulationRootAPI` is applied to the selected root body or fixed
world-root joint, never to the assembly prim. A joint incident to an articulated
body but omitted from its tree is authored as a regular constraint with
`physics:excludeFromArticulation = true`.

## Manifest schema 2

`model.json` records:

- `rigid_bodies`, shapes, materials, mass, and body state;
- every joint endpoint, frame, D6 freedom, limit, articulation membership, and
  derived exclusion status;
- each articulation root and selected spanning-tree joints;
- the complete reference `PhysicsState`;
- the numbered USDZ path.

The manifest is descriptive output, not another authoring API.

## Validation and audit

Export runs OpenUSD validators and an internal topology audit. The audit checks
that articulation edges are trees, every physical joint was written, excluded
constraints carry the required USD flag, body targets exist, meshes have
normals, and authored material bindings survive packaging.

The exporter removes a partial USDZ if validation fails. Existing numbered
exports are not overwritten.

Closed loops are represented natively, but their numerical stability is a
property of the chosen USD physics backend, timestep, and solver settings.
