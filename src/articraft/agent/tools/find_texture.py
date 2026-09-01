"""Search ambientCG for a surface, and look at what came back.

Texture assets used to come from a short list written into the material docs.
That list aged, it covered a fraction of the catalogue, and a slug says nothing
about how coarse a grain is or which way it runs. One run spent all three of
its appearance reviews fighting the grain direction of an asset whose own tags
read "grain, horizontal", because the list offered nothing better.

So the choice is made by looking. This searches the catalogue, returns the tags
each hit carries, and puts the preview images in front of the model, which is
the only way to tell fine oak from rippled oak.
"""

from __future__ import annotations

from typing import Any

from articraft.agent.images import prepare_image
from articraft.agent.tools._core import (
    Tool,
    ToolContext,
    ToolResult,
    reference_image,
    schema,
)
from articraft.sdk import textures

MAX_RESULTS = 8


async def run(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query must name the surface to look for, such as 'fine oak'")
    limit = _limit(args.get("limit"))

    try:
        searched, candidates = _widen(query, limit)
    except RuntimeError as exc:
        return ToolResult({"error": str(exc)}, [])
    if not candidates:
        return ToolResult({"query": query, "found": []}, [])

    # The run's reference goes last when it has one. Choosing between a swatch
    # and a crop of a photograph is the better-posed comparison: both are flat
    # samples of a material, while a studio render and a product shot differ in
    # light, exposure, and camera, and only hue survives that.
    reference = reference_image(context.workspace)
    images = [candidate.preview for candidate in candidates]
    shown = "the previews follow in the same order"
    if reference is not None:
        images.append(reference)
        shown += ", then the run's reference photograph"

    return ToolResult(
        {
            "query": query,
            **({} if searched == query else {"searched": searched}),
            "found": [
                {"texture": candidate.slug, "tags": list(candidate.tags)}
                for candidate in candidates
            ],
            "images": shown,
        },
        [prepare_image(path, detail="high").content_item() for path in images],
    )


def _widen(query: str, limit: int) -> tuple[str, list[textures.Candidate]]:
    """Search, dropping trailing words until something comes back.

    The catalogue matches every term at once, so a phrase finds nothing:
    "fine light oak" has no asset carrying all three tags. Words go from the
    end because the tags describe appearance rather than species, and the
    material class is the term worth keeping. "oak" matches two assets, one of
    them tree bark, while "wood fine" matches thirty. The query that actually
    ran is reported, since the caller asked for something narrower.
    """

    terms = query.split()
    for count in range(len(terms), 0, -1):
        attempt = " ".join(terms[:count])
        candidates = textures.search(attempt, limit=limit)
        if candidates:
            return attempt, candidates
    return query, []


def _limit(raw: object) -> int:
    if raw is None:
        return 6
    try:
        limit = int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limit must be a number from 1 to {MAX_RESULTS}") from exc
    if not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be a number from 1 to {MAX_RESULTS}")
    return limit


TOOL = Tool(
    "find_texture",
    schema(
        "find_texture",
        "Search the ambientCG catalogue for a texture and see the previews. Returns each "
        "hit's slug, which is what a Material's `texture` takes, plus the tags it carries "
        "and its preview image. Search before naming a texture: tags like fine, smooth, "
        "light, plain, and rough separate surfaces that a slug alone does not. Every term "
        "has to match, so one or two words find more than a phrase does.",
        {
            "query": {
                "type": "string",
                "description": (
                    "Tags describing the surface, material class first: 'wood fine "
                    "light', 'metal brushed', 'fabric woven'. The tags describe how a "
                    "surface looks, not what species or alloy it is, so 'oak' matches "
                    "two assets and 'wood fine' matches thirty."
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"How many hits to see, 1 to {MAX_RESULTS}. Defaults to 6.",
            },
        },
        ["query"],
    ),
    run,
    supports_parallel=True,
)
