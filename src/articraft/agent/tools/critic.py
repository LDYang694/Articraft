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
import difflib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

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
# Below this, a color carries no hue worth reading: a backdrop, a white wall, a
# black pull. It is reported by lightness instead.
CHROMA_FLOOR = 0.15
THUMBNAIL = 160
CLUSTERS = 12
MIN_SHARE = 0.03
MAX_GROUPS = 6
VERDICT_FILE = "verdict.json"
AUTHORED_FILE = "authored.py"
MAX_DIFF_LINES = 24
# What counts as a surface change worth telling the next review about. A shape
# edit is not one: this judge is told to say nothing about shape.
MATERIAL_FIELDS = (
    "material",
    "color",
    "roughness",
    "metallic",
    "clearcoat",
    "emissive",
    "opacity",
    "ior",
    "texture",
    "pattern_strength",
)


async def run(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal must say what the object is supposed to be made of")
    views = _views(args.get("views"))
    if context.new_reviewer is None:
        return ToolResult({"error": "no critic model is configured for this run"}, [])
    if len(context.review_scores) >= MAX_REVIEWS:
        return ToolResult({"error": _exhausted(context)}, [])
    settled = _unchanged_since_last_review(context)
    if settled:
        return ToolResult({"error": settled}, [])

    usdz = _latest_usdz(context)
    out_dir = context.workspace / RENDER_DIR / str(len(context.review_scores) + 1)
    rendered = render_usdz(usdz, out_dir, blender=find_blender(context.blender), views=views)
    reference = reference_image(context.workspace)

    labels = [display_path(context.workspace, path) for path in rendered]
    shown = [*rendered, reference] if reference is not None else rendered
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": _prompt(goal, labels, context, reference, rendered)},
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
    _record(out_dir, context.workspace / "main.py", verdict)
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


def _record(out_dir: Path, main: Path, verdict: dict[str, Any]) -> None:
    """Leave this review's verdict and the script it graded beside its renders.

    The next review reads them. It is the only way it learns this one happened,
    and it lives on disk rather than in the context so a finished run still
    shows how its appearance was argued.
    """

    (out_dir / VERDICT_FILE).write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    if main.is_file():
        shutil.copyfile(main, out_dir / AUTHORED_FILE)


def _unchanged_since_last_review(context: ToolContext) -> str:
    """Refuse to grade a script the author has not touched since the last review.

    A run whose second review passed asked for a third without editing
    anything. The same USDZ came back a point higher, which is the judge moving
    and not the object, and it cost a path trace and a review to find that out.
    """

    done = len(context.review_scores)
    if not done:
        return ""
    graded = context.workspace / RENDER_DIR / str(done) / AUTHORED_FILE
    main = context.workspace / "main.py"
    if not graded.is_file() or not main.is_file():
        return ""
    if graded.read_bytes() != main.read_bytes():
        return ""

    verdict = _past_verdict(context.workspace / RENDER_DIR / str(done) / VERDICT_FILE) or {}
    issues = verdict.get("issues") or []
    nothing_to_do = "finish" if verdict.get("pass") and not issues else "act on what it named"
    return (
        f"main.py has not changed since review {done}, so a new review would grade "
        f"the same object and report the same surfaces, give or take the judge's "
        f"own noise. Either {nothing_to_do}, or edit and compile first."
    )


def _history(context: ToolContext) -> str:
    """What the earlier reviews asked for, and which surfaces moved after them.

    Each review gets a client that has seen nothing, so no verdict can be
    carried forward and repeated. That keeps every look independent and leaves
    one gap: a review cannot tell it is undoing the last one. A run went to a
    finer grain, then back to a coarser one, then finer again through that gap,
    and finished where it started with its three reviews spent.

    So the record arrives as text instead. Only the material lines of the diff
    are shown. An appearance judge is told not to speak about shape, and handing
    it a geometry edit invites exactly that.
    """

    done = len(context.review_scores)
    if not done:
        return ""

    parts = []
    for number in range(1, done + 1):
        verdict = _past_verdict(context.workspace / RENDER_DIR / str(number) / VERDICT_FILE)
        if verdict is None:
            continue
        issues = "".join(f"  - {issue}\n" for issue in verdict.get("issues") or [])
        asked = issues or "  - nothing\n"
        parts.append(
            f"Review {number} scored {_score(verdict.get('score')):g} and asked for:\n{asked}"
        )
    if not parts:
        return ""
    return (
        "Earlier reviews of this object, which you did not see. This is the "
        "whole record of them.\n\n" + "\n".join(parts) + f"\n{_material_changes(context, done)}"
    )


def _past_verdict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _material_changes(context: ToolContext, number: int) -> str:
    """The appearance lines of `main.py` that moved since the last review."""

    before = context.workspace / RENDER_DIR / str(number) / AUTHORED_FILE
    after = context.workspace / "main.py"
    if not before.is_file() or not after.is_file():
        return ""

    changed = [
        line
        for line in difflib.unified_diff(
            before.read_text(encoding="utf-8").splitlines(),
            after.read_text(encoding="utf-8").splitlines(),
            n=0,
            lineterm="",
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and _is_material(line)
    ]
    if not changed:
        return "No surface has changed since that review.\n"

    shown = changed[:MAX_DIFF_LINES]
    omitted = len(changed) - len(shown)
    tail = f"\n[{omitted} more changed lines]" if omitted else ""
    return "The author then changed these surfaces:\n" + "\n".join(shown) + tail + "\n"


def _is_material(line: str) -> bool:
    lowered = line.lower()
    return any(field in lowered for field in MATERIAL_FIELDS)


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


def _prompt(
    goal: str,
    labels: list[str],
    context: ToolContext,
    reference: Path | None,
    rendered: Sequence[Path],
) -> str:
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
    parts = [f"{rubric.replace('{{ goal }}', goal)}\nThe images, in order:\n{listing}\n"]
    if reference is not None:
        parts.append(_measured(rendered, reference))
    parts.append(_history(context))
    return "\n".join(part for part in parts if part)


def _measured(rendered: Sequence[Path], reference: Path | None) -> str:
    """What colors each side is actually made of, and how much of it they cover.

    A judge comparing a render with a photograph reads the photograph's brighter
    exposure as a warmer surface, and asks for a hue that is already there. One
    run spent all three reviews moving an oak that measured the same hue as its
    reference to within half a degree.

    One average over the whole frame would not have caught it either: oak, black
    pulls, and a gray floor blend into a color no part of the object has. So the
    colors are grouped and listed with their share, which keeps the wood
    comparable to the wood and leaves the judge to say which group is which.
    """

    if reference is None:
        return ""
    renders = _swatches(rendered)
    photograph = _swatches([reference])
    if not renders or not photograph:
        return ""
    return (
        "Colors measured in each image, grouped and ordered by how much of the "
        "frame each covers. A neutral group carries no hue worth reading and is "
        "named by its lightness alone.\n\n"
        f"the renders:\n{_listing(renders)}\n"
        f"the photograph:\n{_listing(photograph)}"
    )


@dataclass(frozen=True)
class _Swatch:
    share: float
    hue: float
    saturation: float
    lightness: float

    @property
    def neutral(self) -> bool:
        return self.saturation < CHROMA_FLOOR


def _listing(swatches: Sequence[_Swatch]) -> str:
    return "".join(f"- {_describe(swatch)}\n" for swatch in swatches)


def _describe(swatch: _Swatch) -> str:
    share = f"{swatch.share * 100:.0f} percent"
    if swatch.neutral:
        return f"{share}: neutral, lightness {swatch.lightness * 100:.0f} percent"
    return (
        f"{share}: hue {swatch.hue:.0f} degrees, "
        f"saturation {swatch.saturation * 100:.0f} percent, "
        f"lightness {swatch.lightness * 100:.0f} percent"
    )


def _swatches(paths: Sequence[Path]) -> list[_Swatch]:
    """The colors some images are built from, largest share first.

    Median cut does the grouping, on thumbnails: full resolution would spend
    seconds resolving grain that averages away anyway. It splits by population,
    so one flat wall can come back as several near-identical groups, and those
    are folded back together afterwards. Groups too small to argue about are
    dropped rather than padding the list.
    """

    samples = []
    for path in paths:
        with Image.open(path) as opened:
            thumbnail = opened.convert("RGB")
            thumbnail.thumbnail((THUMBNAIL, THUMBNAIL))
            samples.append(np.asarray(thumbnail, dtype=np.uint8).reshape(-1, 1, 3))
    if not samples:
        return []

    flat = np.concatenate(samples)
    quantized = Image.fromarray(flat, "RGB").quantize(
        colors=CLUSTERS, method=Image.Quantize.MEDIANCUT
    )
    palette = quantized.getpalette() or []
    counts = quantized.getcolors(CLUSTERS) or []
    total = sum(count for count, _ in counts)
    if not total:
        return []

    found = [
        _swatch(count / total, palette[index * 3 : index * 3 + 3])
        for count, index in sorted(counts, reverse=True)
    ]
    merged = _folded(found)
    return [swatch for swatch in merged if swatch.share >= MIN_SHARE][:MAX_GROUPS]


def _swatch(share: float, rgb: Sequence[int]) -> _Swatch:
    """One group's color, as how colorful it is and how light it is.

    Saturation is measured against the brightest channel rather than against
    the distance to mid gray. The latter climbs as a color approaches white, so
    a warm off-white wall reports as a third saturated and takes a place in the
    list that belongs to a real surface.
    """

    color = np.asarray(rgb, dtype=np.float64).reshape(1, 3) / 255.0
    high = float(color.max())
    low = float(color.min())
    return _Swatch(
        share=share,
        hue=float(_hue_degrees(color, np.asarray([high - low]))[0]),
        saturation=(high - low) / high if high > 1e-6 else 0.0,
        lightness=(high + low) / 2.0,
    )


def _folded(swatches: Sequence[_Swatch]) -> list[_Swatch]:
    kept: list[_Swatch] = []
    for swatch in swatches:
        for index, existing in enumerate(kept):
            if _reads_the_same(existing, swatch):
                kept[index] = _blended(existing, swatch)
                break
        else:
            kept.append(swatch)
    return sorted(kept, key=lambda swatch: swatch.share, reverse=True)


def _reads_the_same(one: _Swatch, other: _Swatch) -> bool:
    if one.neutral != other.neutral:
        return False
    if one.neutral:
        return abs(one.lightness - other.lightness) < 0.08
    return (
        _hue_gap(one.hue, other.hue) < 10.0
        and abs(one.saturation - other.saturation) < 0.10
        and abs(one.lightness - other.lightness) < 0.12
    )


def _hue_gap(one: float, other: float) -> float:
    gap = abs(one - other) % 360.0
    return min(gap, 360.0 - gap)


def _blended(one: _Swatch, other: _Swatch) -> _Swatch:
    share = one.share + other.share
    weight = other.share / share
    hue = other.hue if abs(other.hue - one.hue) <= 180.0 else other.hue - 360.0
    return _Swatch(
        share=share,
        hue=(one.hue + (hue - one.hue) * weight) % 360.0,
        saturation=one.saturation + (other.saturation - one.saturation) * weight,
        lightness=one.lightness + (other.lightness - one.lightness) * weight,
    )


def _hue_degrees(rgb: np.ndarray, delta: np.ndarray) -> np.ndarray:
    span = np.where(delta > 1e-6, delta, 1.0)
    red, green, blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    brightest = rgb.argmax(axis=1)
    sextant = np.where(
        brightest == 0,
        ((green - blue) / span) % 6.0,
        np.where(brightest == 1, (blue - red) / span + 2.0, (red - green) / span + 4.0),
    )
    return sextant * 60.0


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
        f"after. A run gets {MAX_REVIEWS} reviews, a passing one ends the appearance work, "
        "and asking again without editing is refused. The script behind the best score is "
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
