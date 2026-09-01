# Repository guidelines

## Project purpose

articraft is a small reference version of
[articraft](https://github.com/mattzh72/articraft).

Keep this repo simple. The goal is to preserve the useful core idea from
articraft while leaving behind the larger repo's messy code, broad feature set,
viewer, data library, provenance system, and heavy storage flows.

The core loop is:

```text
prompt -> model -> environment -> record
```

When you need behavior from articraft, read the source first and bring over
only the smallest idea needed for this repo. Prefer writing a clear new version
over copying a large module.

## Project shape

The Python package lives in `src/articraft/`.

Use these areas as the main boundaries:

- `api.py` owns the public sync/async generation functions and shared provider routing.
- `agent/` owns the generate and compile loop.
- `models/` owns model adapters.
- `agent/workspace/` owns run creation and compile execution: where the agent works.
- `compiler/` owns the compile itself, plus its result and the signals the agent reads.
- `render/` owns the Blender subprocess that path-traces an exported USDZ.
- `sdk/` owns the small build123d object API, joints, and export helpers.
- `prompts/` owns the agent prompts.
- `settings.py` owns default runtime settings.
- `record.py` owns the small JSON record and conversation helpers.
- `tests/` owns pytest coverage for the package.

Do not add a viewer, full local library, category system, record manifest,
paper tooling, or broad provider matrix unless the user asks for it directly.

## Development commands

Use `uv` for local work.

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format .
```

Use `OPENAI_API_KEY` in `.env` when testing the OpenAI model adapter.

The CLI entry point is:

```bash
uv run articraft
```

`articraft edit "<change>" <run>` runs the same loop against a finished run's
code: it seeds a new run from that run's workspace and uses `prompts/edit.md`
instead of `prompts/task.md`.

`--textures` turns texture maps on for the whole run, not for a pass after it,
so the agent compiles and judges the textured object.

Appearance is judged on a real render. The `critic` tool path-traces the USDZ
from the last successful compile with Blender and grades those images, because
the rasterizer in `sdk/visual.py` draws no texture and no bounced light, and a
judge looking at it can only answer about color. Blender is a hard dependency
of that tool: point at it with `ARTICRAFT_BLENDER`, and let a missing one raise
rather than falling back to a preview. Everything a review writes lands in the
workspace's `review/`, which `workspace_digest` skips on purpose so a review
does not make the compile it graded look stale.

A run gets a fixed number of reviews, and the critic may only raise issues the
author can act on. Both rules exist because a run without them did not
converge: the rubric asked for texture offsets and bump strength, which no
field controls, so every review returned the same list and the object drifted
while chasing it. When adding to `prompts/critic.md`, check the request against
the fields on `Material` first. The texture criteria live in a `<!-- textures -->`
section that is cut out when a run exports no maps, for the same reason.

A review sees the run's reference photograph when it has one, and grades the
renders against it on material alone. It also refuses a compile the workspace
has moved past, because a USDZ built before the last edit shows a surface the
script no longer authors.

The renders are an instrument, so the rig is calibrated rather than tuned by
eye: a surface renders at the brightness it was authored with. Two settings
broke that silently. Blender's default AgX view transform lifted a 0.20 surface
to 0.34 while leaving a 0.80 one at 0.81, so the worker sets `Standard`. A rig
matched by eye to one product photograph then over-exposed everything. Run
`uv run pytest -m blender` after touching the lighting or the view transform.

Nothing else about a render is comparable to a photograph. Light, exposure, and
camera all differ, so `prompts/critic.md` says which readings survive that and
which do not: hue, saturation, finish, and the ordering between parts do, and
overall brightness does not. Texture identity is settled by `find_texture`
instead, which shows flat swatches beside the reference, because two flat
samples of a material compare cleanly and a render and a photograph do not.

The critic answers through a model client it builds per review; it must never
share the run's client or reuse one across reviews, because a chained provider
keeps conversation state and the judge would repeat itself.

Authored colors are display colors. Anything that feeds a renderer, USD's
`diffuseColor` included, linearizes them first with `materials.to_linear`.

A textured surface gets its color by moving the map's own average onto the
authored one, in LAB, per channel. Every pixel keeps its distance from that
mean, which is what the pattern is; a multiply would stretch the bright end and
flatten the dark one. Pillow stores LAB's a and b signed in unsigned bytes, so
`_signed_lab` undoes that before any arithmetic -- reading them raw exported
black handles as green.

Appearance work does not need a generation. `scripts/material_lab.py` takes a
finished run, compiles it with textures, path-traces it, and prints the color a
region reports beside the color the reference reports for the same region:

```bash
uv run python scripts/material_lab.py runs/<run-id> --region 0.42,0.40,0.56,0.60
```

It works on a scratch copy, costs nothing without `--critic`, and turns a
material question into two rows of numbers. Use it instead of re-running a
generation to look at a material change.

The compile worker entry point is:

```bash
uv run python -m articraft.compiler.worker <run_dir>
```

Prefer calling the compile worker through `LocalWorkspace` unless you are
debugging the worker itself.

## Coding style

Target Python 3.11 and keep the current style.

Use `from __future__ import annotations` in Python modules. Use explicit type
hints for public functions and helpers. Keep dataclasses and Pydantic models
small. Prefer plain functions and simple classes over new frameworks.

Write code in the articraft style: small, direct, and easy to fork. Favor
clear data shapes, compact helpers, and obvious control flow over defensive
frameworks, plugin systems, policy objects, registries, and broad fallback
machinery. Extensible should mean that a reader can understand the core idea and
edit it by hand, not that the repo grows a configurable abstraction layer.

When porting an Articraft idea, keep the useful behavior and drop the ceremony.
Prefer one readable module with a few plain dataclasses and functions over a
large subsystem split across many files. Avoid over-engineering for inputs this
repo does not produce. Keep tests focused on behavior and avoid repeating the
same assertions at every integration layer.

Ruff is configured with a line length of 100, Python 3.11 syntax, import
sorting, and double quotes.

## Change policy

Make narrow changes. Keep each module easy to read on its own.

Avoid hidden global state. Avoid background services. Avoid adding caches,
registries, database layers, or file layouts that are not needed for the small
reference flow.

Generated runs, result files, local secrets, virtual environments, and caches
should stay out of commits. If a new workflow writes generated files, either
write them under a clearly ignored folder or update `.gitignore` in the same
change.

## Testing

Add or update tests when behavior changes. Keep tests close to the code they
cover and name new files `test_<feature>.py`.

Prefer fast pytest tests that exercise the package directly. Use temporary
directories for compile and record tests. Do not require real model calls unless
the test is explicitly about a live adapter.

`tests/harness.py` is the modular test environment for agent-loop behavior:
scripted models, warm-worker compiles (`WarmEnvironment`), and tape replay
(`ReplayHarness`) cover the full loop without paid model calls. See
`tests/README.md` for the four lanes and when to use each. Keep the subprocess
worker contract covered in `test_compile.py`. A few exec-output timing tests
are known to flake on macOS; do not weaken them locally.

Run this before handing off a code change:

```bash
uv run pytest -q
uv run ruff check .
```
