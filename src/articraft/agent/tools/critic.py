"""The appearance judge: path-trace the model, then grade how it looks.

`view_image` puts a preview in front of the agent that authored it, which makes
it the same judgement by the same party. This asks a separate model, which sees
only the images and the goal, and answers with a verdict the loop can act on:
``{pass, score, issues}``.

It renders what it grades. The rasterized previews show no texture and no real
light, so an oak door and a beige door reach a judge as the same flat
rectangle, and the only honest answer it could give was about color. Here the
exported USDZ goes through Blender first, so the grain, the finish, and the
metal are the ones the delivered file actually carries.

It grades appearance only. Geometry already has compile checks; what a material
looks like has none, so this is the one signal for it.

Each review gets its own client, and no review sees another. A provider that
chains its requests would otherwise carry the previous verdict into the next
one, and the judge repeats itself instead of looking again.

A failed review comes back as data rather than an exception, because ending a
run over a second opinion is never the right trade. A failed render does not:
without it there is nothing to grade, and falling back to a rasterized preview
would quietly restore the problem this tool exists to solve.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from articraft import package_dir
from articraft.agent.images import prepare_image
from articraft.agent.tools._core import (
    REVIEW_DIR,
    Tool,
    ToolContext,
    ToolResult,
    display_path,
    reference_image,
    schema,
)
from articraft.render import DEFAULT_VIEWS, VIEWS, find_blender, render_usdz

MAX_REVIEWS = 3
RENDER_DIR = REVIEW_DIR / "renders"
BEST_COPY = REVIEW_DIR / "best_main.py"


async def run(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal must say what the object is supposed to be made of")
    views = _views(args.get("views"))
    if context.new_reviewer is None:
        return ToolResult({"error": "no critic model is configured for this run"}, [])
    if len(context.review_scores) >= MAX_REVIEWS:
        return ToolResult({"error": _exhausted(context)}, [])

    usdz = _latest_usdz(context)
    out_dir = context.workspace / RENDER_DIR / str(len(context.review_scores) + 1)
    rendered = render_usdz(usdz, out_dir, blender=find_blender(context.blender), views=views)
    reference = reference_image(context.workspace)

    labels = [display_path(context.workspace, path) for path in rendered]
    shown = [*rendered, reference] if reference is not None else rendered
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": _prompt(goal, labels, context, reference)},
        *(prepare_image(path).content_item() for path in shown),
    ]

    reviewer = context.new_reviewer()
    try:
        response = await reviewer.query([{"role": "user", "content": content}])
    except Exception as exc:
        return ToolResult({"error": f"critic call failed: {type(exc).__name__}: {exc}"}, [])
    finally:
        with contextlib.suppress(Exception):
            await reviewer.close()
    context.add_review_usage(response)

    verdict = _verdict(str(response.get("text") or ""))
    if verdict is None:
        return ToolResult({"error": "critic did not return a verdict", "rendered": labels}, [])

    best_so_far = max(context.review_scores, default=-1.0)
    context.review_scores.append(verdict["score"])
    if verdict["score"] > best_so_far:
        _keep_best(context)
    return ToolResult(
        {
            "rendered": labels,
            **verdict,
            "scores_so_far": list(context.review_scores),
            "reviews_left": MAX_REVIEWS - len(context.review_scores),
            "best_kept_at": BEST_COPY.as_posix(),
        },
        [],
    )


def _keep_best(context: ToolContext) -> None:
    """Save the script behind the best score, so a worse pass can be undone.

    Appearance work does not improve monotonically. A run can chase one issue
    into a material that scores lower than where it started, and without a copy
    of the better version the only way back is to remember what changed.
    """

    source = context.workspace / "main.py"
    if not source.is_file():
        return
    destination = context.workspace / BEST_COPY
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _exhausted(context: ToolContext) -> str:
    scores = ", ".join(f"{score:g}" for score in context.review_scores)
    return (
        f"the run's {MAX_REVIEWS} reviews are used up (scores: {scores}). "
        f"Settle on the best version -- {BEST_COPY.as_posix()} holds the script "
        "behind the highest score -- and finish."
    )


def _views(raw: object) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_VIEWS
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"views must be a non-empty list from {list(VIEWS)}")
    chosen = tuple(str(item) for item in raw)
    unknown = [view for view in chosen if view not in VIEWS]
    if unknown:
        raise ValueError(f"unknown views {unknown}, pick from {list(VIEWS)}")
    return chosen


def _latest_usdz(context: ToolContext) -> Path:
    """The file the last good compile exported, which is what ships.

    The compile has to still describe the workspace. Grading a USDZ built
    before the last edit reports a surface the script no longer authors, and
    the run answers a review of code it already replaced.
    """

    payload = context.successful_compile_result
    path = Path(str((payload or {}).get("usdz") or ""))
    if not payload or not path.is_file():
        raise ValueError("compile the model successfully before asking for a review")
    if not context.refresh_compile_freshness():
        raise ValueError("main.py changed since the last compile; compile again before reviewing")
    return path


def _prompt(goal: str, labels: list[str], context: ToolContext, reference: Path | None) -> str:
    """The rubric, cut down to the questions this run can actually answer.

    Texture criteria only apply when the run exports texture maps. Asking about
    grain size on an untextured object produces issues with no control behind
    them, which is the failure the fixable-only rule exists to prevent.
    """

    rubric = (package_dir / "prompts" / "critic.md").read_text(encoding="utf-8")
    rubric = _section(rubric, "reference", keep=reference is not None)
    rubric = _section(rubric, "textures", keep=context.textures)
    listing = "\n".join(f"- {label}" for label in labels)
    if reference is not None:
        listing += f"\n- {display_path(context.workspace, reference)} (the reference photograph)"
    return f"{rubric.replace('{{ goal }}', goal)}\nThe images, in order:\n{listing}\n"


def _section(rubric: str, name: str, *, keep: bool) -> str:
    pattern = re.compile(rf"<!-- {name} -->\n(.*?)<!-- /{name} -->\n", flags=re.DOTALL)
    return pattern.sub((lambda match: match.group(1)) if keep else "", rubric)


def _verdict(text: str) -> dict[str, Any] | None:
    """Read the judge's JSON, tolerating a fenced or prefixed reply.

    The verdict is normalized so the agent always reads the same three keys,
    whatever shape the reply arrived in.
    """

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or "pass" not in parsed:
        return None
    issues = parsed.get("issues")
    return {
        "pass": bool(parsed.get("pass")),
        "score": _score(parsed.get("score")),
        "issues": [str(issue) for issue in issues] if isinstance(issues, list) else [],
    }


def _score(value: Any) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


TOOL = Tool(
    "critic",
    schema(
        "critic",
        "Ask a separate model to grade how the model looks. It path-traces the USDZ from "
        "your last successful compile with Blender, writes the images to review/renders/, "
        "and grades those: color, finish, texture scale and direction, and whether each "
        "part reads as the material it claims to be. Returns {pass, score, issues}. It "
        "never judges shape, size, or placement. Compile first; view the renders yourself "
        f"after. A run gets {MAX_REVIEWS} reviews, and the script behind the best score is "
        f"kept at {BEST_COPY.as_posix()} so a worse pass can be undone.",
        {
            "goal": {
                "type": "string",
                "description": (
                    "What the object is supposed to be made of, in a sentence: "
                    '"an oak cabinet with blackened steel handles and glass doors".'
                ),
            },
            "views": {
                "type": "array",
                "items": {"type": "string", "enum": list(VIEWS)},
                "description": (
                    f"Which views to render, default {list(DEFAULT_VIEWS)}. Each one "
                    "costs a few seconds."
                ),
            },
        },
        ["goal"],
    ),
    run,
    supports_parallel=False,
)
