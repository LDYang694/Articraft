<task>
User request:

{{ prompt }}

Edit `main.py` in the run workspace and build the requested object. Meet the five
quality requirements from the system prompt. Use realistic geometry, model only
the motion the design requires, support retained parts, seat removable parts,
avoid unintended overlap, and say what every visible surface is made of.

Start with the preloaded SDK quickstart. Before choosing a representation, use
`read` to survey the relevant SDK references and compare plausible build123d and
mesh approaches. Do not stop at the first workable API. Research enough to form
an internal geometry strategy for the major visible forms. Then implement the
object with `RigidBody.add(shape, name=..., material=...)` for known physical surfaces
or `color=...` for plain matte ones, add prompt-specific checks, and run `compile`.
Read `docs/sdk/common/37_materials.md` before you choose the materials: the
color, roughness, metalness, and clear coat of each part are as much a part of
the object as its shape.

Treat every compile signal as design evidence. Preserve prompt-critical visible
geometry while you repair named defects.
<image_prompt>
After a successful compile, review the visual representation separately and
improve any major form that uses a crude substitute when a public authoring
method would fit it better. Review appearance in the same pass with `critic`,
which path-traces the compiled USDZ, and act on the issues it names.
</image_prompt>
Then return a short visible summary of the object. Include its main motion only
when independent motion is part of the design.
</task>
