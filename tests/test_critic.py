"""The appearance critic: what it renders, what it sends, and what it costs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from harness import GOOD_MAIN_PY, WarmEnvironment, calls, run_scenario, text
from harness import tool_call as call
from PIL import Image

import articraft.agent.tools.critic as critic_module
from articraft.agent.tools import ToolContext, get
from articraft.agent.tools._core import workspace_digest
from articraft.agent.workspace.local import LocalWorkspace
from articraft.render import RenderError


class FakeReviewer:
    """A reviewer that answers with canned text and remembers what it saw."""

    def __init__(self, *replies: str, cost: float = 0.25) -> None:
        self.replies = list(replies)
        self.cost = cost
        self.queries: list[list[dict[str, Any]]] = []
        self.close_calls = 0

    async def query(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.queries.append(messages)
        return {
            "text": self.replies.pop(0),
            "cost": self.cost,
            "token_usage": {"input_tokens": 400, "output_tokens": 20},
        }

    async def close(self) -> None:
        self.close_calls += 1


class FakeBlender:
    """Stands in for the render, and records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, usdz: Path, out_dir: Path, *, blender: Path, views, **kwargs) -> list[Path]:
        self.calls.append({"usdz": usdz, "out_dir": out_dir, "blender": blender, "views": views})
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for view in views:
            path = out_dir / f"{view}.png"
            Image.new("RGB", (64, 48), (120, 90, 60)).save(path)
            written.append(path)
        return written


def _run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)


@pytest.fixture
def blender(monkeypatch) -> FakeBlender:
    fake = FakeBlender()
    monkeypatch.setattr(critic_module, "render_usdz", fake)
    monkeypatch.setattr(critic_module, "find_blender", lambda configured: Path("/usr/bin/blender"))
    return fake


def _context(
    tmp_path: Path, reviewer: Any = None, *, compiled: bool = True, slug: str = "critic"
) -> ToolContext:
    env = LocalWorkspace(output_dir=tmp_path)
    run_dir = env.create_run(slug)
    usdz = run_dir / "result" / "usdz" / "0000.usdz"
    usdz.parent.mkdir(parents=True, exist_ok=True)
    usdz.write_bytes(b"exported")
    context = ToolContext(
        env,
        run_dir,
        run_dir / "workspace",
        new_reviewer=None if reviewer is None else lambda: reviewer,
    )
    if compiled:
        context.successful_compile_result = {"status": "success", "usdz": str(usdz)}  # type: ignore[typeddict-item]
        _mark_compiled(context)
    return context


def _mark_compiled(context: ToolContext) -> None:
    """Say the workspace is what the last compile saw, as the compile tool does."""

    context.successful_compile_digest = workspace_digest(context.workspace)


def _revise(context: ToolContext, note: str) -> None:
    """Author a new version and compile it, which is what earns another review."""

    (context.workspace / "main.py").write_text(f"# {note}\n")
    _mark_compiled(context)


def test_critic_renders_the_exported_model_and_returns_a_verdict(
    tmp_path: Path, blender: FakeBlender
) -> None:
    reviewer = FakeReviewer(
        json.dumps(
            {
                "pass": False,
                "score": 4,
                "issues": ["the handles are the same oak tint as the carcass"],
            }
        )
    )
    context = _context(tmp_path, reviewer)

    result = _run(get("critic").run(context, {"goal": "an oak cabinet with steel handles"}))

    # It grades the exported file, not a rasterized preview: the previews show
    # no texture and no real light, so they cannot answer the question asked.
    assert blender.calls[0]["usdz"] == Path(str(context.successful_compile_result["usdz"]))
    assert blender.calls[0]["out_dir"] == context.workspace / "review" / "renders" / "1"
    assert result.output == {
        "rendered": ["review/renders/1/hero.png", "review/renders/1/front.png"],
        "pass": False,
        "score": 4.0,
        "issues": ["the handles are the same oak tint as the carcass"],
        "scores_so_far": [4.0],
        "reviews_left": 2,
        "best_kept_at": "review/best_main.py",
    }
    content = reviewer.queries[0][0]["content"]
    assert content[0]["type"] == "input_text"
    assert "an oak cabinet with steel handles" in content[0]["text"]
    assert "Judge appearance, not shape" in content[0]["text"]
    assert [item["type"] for item in content[1:]] == ["input_image", "input_image"]
    # A review is part of what the run spent, whichever client paid for it.
    assert context.review_cost == 0.25
    assert context.review_token_usage == {"input_tokens": 400, "output_tokens": 20}


def test_critic_renders_the_views_it_is_asked_for(tmp_path: Path, blender: FakeBlender) -> None:
    context = _context(tmp_path, FakeReviewer(json.dumps({"pass": True, "score": 9})))

    result = _run(get("critic").run(context, {"goal": "steel", "views": ["side", "high"]}))

    assert blender.calls[0]["views"] == ("side", "high")
    assert result.output["rendered"] == ["review/renders/1/side.png", "review/renders/1/high.png"]


def test_critic_reads_a_verdict_out_of_a_fenced_reply(tmp_path: Path, blender: FakeBlender) -> None:
    reviewer = FakeReviewer('Here is my verdict:\n```json\n{"pass": true, "score": 12}\n```\n')
    context = _context(tmp_path, reviewer)

    result = _run(get("critic").run(context, {"goal": "brushed steel"}))

    # Score is clamped, and a reply without issues still reads as no issues.
    assert result.output["pass"] is True
    assert result.output["score"] == 10.0
    assert result.output["issues"] == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("the object looks fine to me", "critic did not return a verdict"),
        ('{"score": 9}', "critic did not return a verdict"),
    ],
)
def test_critic_returns_an_unusable_reply_as_data(
    tmp_path: Path, blender: FakeBlender, reply: str, expected: str
) -> None:
    """A judge that raises would end the run over a second opinion."""

    context = _context(tmp_path, FakeReviewer(reply))

    result = _run(get("critic").run(context, {"goal": "steel"}))

    assert result.output["error"] == expected


def test_critic_reports_a_failed_call_instead_of_raising(
    tmp_path: Path, blender: FakeBlender
) -> None:
    class BrokenReviewer:
        def __init__(self) -> None:
            self.close_calls = 0

        async def query(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
            raise TimeoutError("no answer")

        async def close(self) -> None:
            self.close_calls += 1

    reviewer = BrokenReviewer()
    context = _context(tmp_path, reviewer)

    result = _run(get("critic").run(context, {"goal": "steel"}))

    assert result.output == {"error": "critic call failed: TimeoutError: no answer"}
    assert context.review_cost == 0.0
    # A failed review still releases its client.
    assert reviewer.close_calls == 1


def test_a_failed_render_stops_the_review_rather_than_grading_something_else(
    tmp_path: Path, monkeypatch
) -> None:
    """There is no honest fallback: a preview is the image this tool moved off."""

    def refuse(*args, **kwargs):
        raise RenderError("no blender found")

    monkeypatch.setattr(critic_module, "find_blender", refuse)
    context = _context(tmp_path, FakeReviewer("{}"))

    with pytest.raises(RenderError, match="no blender found"):
        _run(get("critic").run(context, {"goal": "steel"}))


def test_each_review_gets_a_client_that_has_seen_nothing(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """Two reviews through one chained client repeated the first verdict.

    A provider that chains its requests carries the earlier review along, so the
    judge answers from what it already said instead of the images in front of it.
    """

    built: list[FakeReviewer] = []

    def new_reviewer() -> FakeReviewer:
        built.append(FakeReviewer(json.dumps({"pass": True, "score": 9, "issues": []})))
        return built[-1]

    context = _context(tmp_path)
    context.new_reviewer = new_reviewer

    for attempt in range(2):
        _revise(context, f"version {attempt}")
        _run(get("critic").run(context, {"goal": "steel"}))

    assert len(built) == 2
    assert [len(reviewer.queries) for reviewer in built] == [1, 1]
    assert [reviewer.close_calls for reviewer in built] == [1, 1]
    # Both reviews are charged to the run.
    assert context.review_cost == pytest.approx(0.5)


def test_critic_requires_a_goal_a_known_view_and_a_compiled_model(
    tmp_path: Path, blender: FakeBlender
) -> None:
    context = _context(tmp_path, FakeReviewer("{}"))

    with pytest.raises(ValueError, match="goal must say"):
        _run(get("critic").run(context, {"goal": "  "}))
    with pytest.raises(ValueError, match="unknown views"):
        _run(get("critic").run(context, {"goal": "steel", "views": ["underneath"]}))

    uncompiled = _context(tmp_path, FakeReviewer("{}"), compiled=False, slug="uncompiled")
    with pytest.raises(ValueError, match="compile the model successfully"):
        _run(get("critic").run(uncompiled, {"goal": "steel"}))


def test_the_reference_photograph_goes_to_the_critic_with_the_renders(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """Without it a review grades the renders against its own idea of the material.

    That is how an oak that already matched its reference came back as too warm,
    and drifted to weathered gray on the way to satisfying the idea instead.
    """

    reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    context = _context(tmp_path, reviewer)
    Image.new("RGB", (96, 72), (150, 110, 70)).save(context.workspace / "reference.png")
    _mark_compiled(context)

    result = _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    prompt = reviewer.queries[0][0]["content"][0]["text"]
    assert "the reference photograph" in prompt
    assert "the reference wins" in prompt
    # Two differently lit images cannot be compared on brightness, so the
    # rubric says which readings survive the difference and which do not.
    assert "Hue and saturation do" in prompt
    # It arrives last, after the two renders, which is the order the rubric states.
    assert [item["type"] for item in reviewer.queries[0][0]["content"][1:]] == ["input_image"] * 3
    # The reference is evidence for the critic, not one of the graded renders.
    assert result.output["rendered"] == ["review/renders/1/hero.png", "review/renders/1/front.png"]


def test_the_critic_is_told_the_colors_both_sides_measure(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """A judge reads a photograph's brighter exposure as a warmer surface.

    One run spent all three of its reviews moving an oak that measured the same
    hue as its reference to within half a degree. Measuring both sides costs
    nothing and settles the question the eye keeps getting wrong.
    """

    reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    context = _context(tmp_path, reviewer)
    # The same hue as the render the fake writes, two steps lighter.
    Image.new("RGB", (96, 72), (150, 110, 70)).save(context.workspace / "reference.png")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    prompt = reviewer.queries[0][0]["content"][0]["text"]
    renders, photograph = prompt.split("the photograph:\n")
    assert "100 percent: hue 30 degrees, saturation 50 percent, lightness 35 percent" in renders
    assert "100 percent: hue 30 degrees, saturation 53 percent, lightness 43 percent" in photograph


def test_each_material_gets_its_own_measured_group(tmp_path: Path, blender: FakeBlender) -> None:
    """One average over the frame is a color no part of the object has.

    Oak, black pulls, and a gray floor blend into a single number that answers
    nothing, so the colors are grouped and the judge matches the wood against
    the wood.
    """

    reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    context = _context(tmp_path, reviewer)
    reference = Image.new("RGB", (100, 100), (150, 110, 70))
    reference.paste(Image.new("RGB", (100, 25), (18, 18, 20)), (0, 75))
    reference.save(context.workspace / "reference.png")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet with black pulls"}))

    photograph = reviewer.queries[0][0]["content"][0]["text"].split("the photograph:\n")[1]
    assert "75 percent: hue 30 degrees" in photograph
    # A near-black pull carries no hue worth reading, so it is placed by lightness.
    assert "25 percent: neutral, lightness 7 percent" in photograph


def test_a_run_without_a_reference_photograph_measures_nothing(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """Colors read off the renders alone say nothing about what they should be."""

    reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    _run(get("critic").run(_context(tmp_path, reviewer), {"goal": "steel"}))

    assert "Colors measured" not in reviewer.queries[0][0]["content"][0]["text"]


def test_a_run_without_textures_is_not_asked_about_them(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """An untextured export has no grain to size or turn, so asking invents work."""

    plain = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    _run(get("critic").run(_context(tmp_path, plain, slug="plain"), {"goal": "steel"}))

    textured_reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9}))
    textured = _context(tmp_path, textured_reviewer, slug="textured")
    textured.textures = True
    _run(get("critic").run(textured, {"goal": "steel"}))

    assert "wrong way" not in plain.queries[0][0]["content"][0]["text"]
    assert "tile size in meters" not in plain.queries[0][0]["content"][0]["text"]
    assert "wrong way" in textured_reviewer.queries[0][0]["content"][0]["text"]


def test_a_review_refuses_a_compile_the_workspace_has_moved_past(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """Grading a stale USDZ reports a surface the script no longer authors."""

    context = _context(tmp_path, FakeReviewer(json.dumps({"pass": True, "score": 9})))
    (context.workspace / "main.py").write_text("# edited after the compile\n")

    with pytest.raises(ValueError, match="compile again before reviewing"):
        _run(get("critic").run(context, {"goal": "steel"}))
    assert blender.calls == []


def test_each_review_keeps_its_own_renders(tmp_path: Path, blender: FakeBlender) -> None:
    """Overwriting them left the best score with no images behind it."""

    context = _context(
        tmp_path,
        FakeReviewer(*[json.dumps({"pass": False, "score": 5})] * 2),
    )

    _revise(context, "first version")
    _run(get("critic").run(context, {"goal": "steel"}))
    _revise(context, "second version")
    _run(get("critic").run(context, {"goal": "steel"}))

    renders = context.workspace / "review" / "renders"
    assert (renders / "1" / "hero.png").is_file()
    assert (renders / "2" / "hero.png").is_file()


def test_a_run_gets_a_fixed_number_of_reviews(tmp_path: Path, blender: FakeBlender) -> None:
    """Four reviews of the same object repeated themselves and never converged.

    The scores went 4, 3, 4, 3 while the object drifted, because nothing said
    when to stop asking.
    """

    replies = [json.dumps({"pass": False, "score": score}) for score in (4, 3, 4)]
    context = _context(tmp_path, FakeReviewer(*replies))

    for attempt in range(3):
        _revise(context, f"version {attempt}")
        _run(get("critic").run(context, {"goal": "steel"}))
    _revise(context, "one more version")
    refused = _run(get("critic").run(context, {"goal": "steel"}))

    assert context.review_scores == [4.0, 3.0, 4.0]
    assert "3 reviews are used up" in refused.output["error"]
    assert "4, 3, 4" in refused.output["error"]
    assert len(blender.calls) == 3


def test_a_review_is_told_what_the_earlier_ones_asked_for(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """A review cannot see the ones before it, so it cannot tell it is undoing one.

    A run asked for a finer grain, then a coarser one, then a finer one again,
    and finished where it started with all three reviews spent.
    """

    reviewer = FakeReviewer(
        json.dumps({"pass": False, "score": 5, "issues": ["the grain is too coarse"]}),
        json.dumps({"pass": False, "score": 6, "issues": ["the pulls are flat"]}),
    )
    context = _context(tmp_path, reviewer)
    (context.workspace / "main.py").write_text("OAK = Material(pattern_strength=0.52)\n")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet"}))
    (context.workspace / "main.py").write_text("OAK = Material(pattern_strength=0.42)\n")
    _mark_compiled(context)
    _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    first, second = (query[0]["content"][0]["text"] for query in reviewer.queries)
    assert "Earlier reviews" not in first
    assert "Review 1 scored 5 and asked for:" in second
    assert "the grain is too coarse" in second
    # The dial it moved, so a review about to move it back can see that.
    assert "-OAK = Material(pattern_strength=0.52)" in second
    assert "+OAK = Material(pattern_strength=0.42)" in second


def test_a_second_look_at_an_unedited_script_is_refused(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """A run whose review passed asked for another without changing anything.

    The same USDZ came back a point higher, which is the judge moving and not
    the object, and it cost a path trace and a review to find that out.
    """

    reviewer = FakeReviewer(
        json.dumps({"pass": True, "score": 8, "issues": []}),
        json.dumps({"pass": True, "score": 9, "issues": []}),
    )
    context = _context(tmp_path, reviewer)
    (context.workspace / "main.py").write_text("OAK = Material(roughness=0.5)\n")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet"}))
    refused = _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    assert "has not changed since review 1" in refused.output["error"]
    assert "finish" in refused.output["error"]
    # It costs nothing: no second render, and the reviewer was never asked.
    assert len(blender.calls) == 1
    assert len(reviewer.queries) == 1
    assert context.review_scores == [8.0]


def test_an_edited_script_may_be_reviewed_again(tmp_path: Path, blender: FakeBlender) -> None:
    """The refusal is about an unchanged object, not about asking twice."""

    reviewer = FakeReviewer(
        json.dumps({"pass": False, "score": 6, "issues": ["the oak is flat"]}),
        json.dumps({"pass": True, "score": 8, "issues": []}),
    )
    context = _context(tmp_path, reviewer)
    (context.workspace / "main.py").write_text("OAK = Material(roughness=0.5)\n")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet"}))
    (context.workspace / "main.py").write_text("OAK = Material(roughness=0.3)\n")
    _mark_compiled(context)
    second = _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    assert second.output["score"] == 8.0
    assert context.review_scores == [6.0, 8.0]


def test_a_shape_edit_stays_out_of_the_record(tmp_path: Path, blender: FakeBlender) -> None:
    """This judge is told to say nothing about shape, so it is not shown any."""

    reviewer = FakeReviewer(
        json.dumps({"pass": False, "score": 5, "issues": ["the oak is pale"]}),
        json.dumps({"pass": True, "score": 8}),
    )
    context = _context(tmp_path, reviewer)
    (context.workspace / "main.py").write_text("DOOR_WIDTH = 0.40\nOAK = Material(roughness=0.5)\n")
    _mark_compiled(context)

    _run(get("critic").run(context, {"goal": "an oak cabinet"}))
    (context.workspace / "main.py").write_text("DOOR_WIDTH = 0.55\nOAK = Material(roughness=0.3)\n")
    _mark_compiled(context)
    _run(get("critic").run(context, {"goal": "an oak cabinet"}))

    second = reviewer.queries[1][0]["content"][0]["text"]
    assert "roughness=0.3" in second
    assert "DOOR_WIDTH" not in second


def test_the_script_behind_the_best_score_is_kept(tmp_path: Path, blender: FakeBlender) -> None:
    """Appearance work does not improve monotonically, so a worse pass needs a way back."""

    context = _context(
        tmp_path,
        FakeReviewer(
            json.dumps({"pass": False, "score": 7}), json.dumps({"pass": False, "score": 5})
        ),
    )
    best = context.workspace / "review" / "best_main.py"

    (context.workspace / "main.py").write_text("# the version that scored seven\n")
    _mark_compiled(context)
    _run(get("critic").run(context, {"goal": "steel"}))
    (context.workspace / "main.py").write_text("# the version that scored five\n")
    _mark_compiled(context)
    _run(get("critic").run(context, {"goal": "steel"}))

    assert best.read_text() == "# the version that scored seven\n"


def test_a_review_does_not_make_the_compile_it_graded_stale(
    tmp_path: Path, blender: FakeBlender
) -> None:
    """The renders come out of the compiled USDZ, so they cannot invalidate it."""

    context = _context(tmp_path, FakeReviewer(json.dumps({"pass": True, "score": 9})))
    (context.workspace / "main.py").write_text("# the model\n")
    _mark_compiled(context)
    before = workspace_digest(context.workspace)

    _run(get("critic").run(context, {"goal": "steel"}))

    assert (context.workspace / "review" / "renders" / "1" / "hero.png").is_file()
    assert workspace_digest(context.workspace) == before


def test_critic_says_so_when_no_reviewer_is_configured(
    tmp_path: Path, blender: FakeBlender
) -> None:
    context = _context(tmp_path)

    result = _run(get("critic").run(context, {"goal": "steel"}))

    assert result.output == {"error": "no critic model is configured for this run"}


def test_a_run_charges_its_reviews_and_closes_the_critic(
    tmp_path: Path, blender: FakeBlender
) -> None:
    reviewer = FakeReviewer(json.dumps({"pass": True, "score": 9, "issues": []}), cost=0.5)

    artifacts = run_scenario(
        "a box",
        [
            calls(call("write", {"path": "main.py", "content": GOOD_MAIN_PY})),
            calls(call("compile")),
            calls(call("critic", {"goal": "a plain plastic box"})),
            text("done"),
        ],
        env=WarmEnvironment(output_dir=tmp_path),
        new_reviewer=lambda: reviewer,
    )

    verdict = next(
        output["result"]
        for output in artifacts.tool_outputs()
        if isinstance(output.get("result"), dict) and "score" in output["result"]
    )
    assert verdict["pass"] is True
    assert artifacts.record.status == "success"
    assert artifacts.record.cost == pytest.approx(0.5)
    assert artifacts.record.token_usage["input_tokens"] == 400
    assert reviewer.close_calls == 1
