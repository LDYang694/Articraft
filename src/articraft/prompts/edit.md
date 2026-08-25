<task>
Requested change:

{{ prompt }}

The run workspace already holds a complete object from an earlier run, in
`main.py` and any helper modules beside it. This is a change to that object, not
a new one. Read the current `main.py` before you edit anything, and find the
named shapes, dimensions, and checks the request actually concerns.

Make the requested change and nothing else. Keep every other part, material,
joint, limit, and dimension as it stands, and leave the structure and naming of
code you were not asked to touch alone. Reuse the constants and helpers the
script already defines. Meet the four quality requirements from the system
prompt for the geometry you change, and read the SDK references you need for the
new form when the current approach does not extend to it.

Rename what you changed when its old name no longer describes it. A shape,
constant, or helper named for the form it used to have is now misleading, so give
it an accurate name and update every reference to it, including the checks. This
applies only to the things the request changed.

`run_tests()` describes the object as it was. A check, metric, or allowance that
asserts what you just changed is now wrong, so restate it to describe the new
intent and keep it as strong as it was. Do not delete or loosen a check to make
the compile pass. Checks about parts you did not touch must keep passing
unchanged.
<image_prompt>
Re-run `previews.py` when the workspace has one, and view every image the change
affects before you compile. A preview built from the old geometry is not
evidence about the new geometry.
</image_prompt>
Run `compile` and treat every signal as design evidence. Then return a short
visible summary of what changed and what it means for the object.
</task>
