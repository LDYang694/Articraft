"""The edit loop: a new run that starts from a finished run's code.

Seeding and prompt choice are the only things that differ from generation, so
these cover the seeded workspace, both sides of the prompt choice, and the fact
that the seeded code is what the agent actually compiles.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import (
    GOOD_MAIN_PY,
    ModelQuery,
    Response,
    WarmEnvironment,
    calls,
    run_scenario,
    text,
    tool_call,
)

from articraft import package_dir
from articraft.agent.record import Record
from articraft.agent.workspace.local import LocalWorkspace


def compile_workspace() -> Response:
    return calls(tool_call("compile"))


def seed_workspace(run_dir: Path, main_py: str = GOOD_MAIN_PY) -> Path:
    """A finished run's workspace, which is what a seed always is."""
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    workspace.joinpath("main.py").write_text(main_py, encoding="utf-8")
    return workspace


def test_create_run_copies_the_seed_workspace_and_relinks_docs(tmp_path: Path) -> None:
    seed = seed_workspace(tmp_path / "source", "# seeded\n")
    seed.joinpath("previews.py").write_text("# previews\n", encoding="utf-8")
    seed.joinpath("qa", "previews").mkdir(parents=True)
    seed.joinpath("qa", "previews", "front.png").write_bytes(b"png")
    seed.joinpath("__pycache__").mkdir()
    seed.joinpath("__pycache__", "main.pyc").write_bytes(b"stale")
    seed.joinpath("docs").mkdir()
    seed.joinpath("docs", "sdk").symlink_to(tmp_path, target_is_directory=True)

    env = LocalWorkspace(output_dir=tmp_path / "runs")
    run_dir = env.create_run("edited", seed_workspace=seed)

    workspace = run_dir / "workspace"
    assert workspace.joinpath("main.py").read_text() == "# seeded\n"
    assert workspace.joinpath("previews.py").read_text() == "# previews\n"
    assert workspace.joinpath("qa", "previews", "front.png").read_bytes() == b"png"
    assert not workspace.joinpath("__pycache__").exists()
    # The seed's stale docs symlink is replaced, not copied.
    assert workspace.joinpath("docs", "sdk").resolve() == (package_dir / "sdk" / "docs")
    assert list(run_dir.joinpath("result").iterdir()) == []
    assert Record.load(run_dir / "record.json") == Record(run_id="edited", source_run="source")


def test_create_run_rejects_a_seed_without_main_py(tmp_path: Path) -> None:
    empty = tmp_path / "source" / "workspace"
    empty.mkdir(parents=True)
    env = LocalWorkspace(output_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match=r"no main\.py"):
        env.create_run("edited", seed_workspace=empty)

    assert not (tmp_path / "runs" / "edited").exists()


def test_edit_loop_asks_the_model_to_change_the_seeded_object(tmp_path: Path) -> None:
    env = WarmEnvironment(output_dir=tmp_path / "runs")
    seed = seed_workspace(tmp_path / "source")

    def first_turn(query: ModelQuery) -> Response:
        assert query.contains("Requested change:", "make the box taller")
        assert not query.contains("build the requested object")
        return compile_workspace()

    artifacts = run_scenario(
        "make the box taller",
        [first_turn, text("the box is taller")],
        env=env,
        seed_workspace=seed,
        max_turns=4,
    )

    # The compile passed on code the script never wrote, so the seed is what ran.
    assert artifacts.record.status == "success"
    assert artifacts.record.source_run == "source"
    assert env.compile_count == 1


def test_generation_keeps_the_from_scratch_task_prompt(tmp_path: Path) -> None:
    env = WarmEnvironment(output_dir=tmp_path / "runs")

    def first_turn(query: ModelQuery) -> Response:
        assert query.contains("build the requested object")
        assert not query.contains("Requested change:")
        return compile_workspace()

    artifacts = run_scenario(
        "a box",
        [first_turn, text("a box")],
        env=env,
        max_turns=4,
    )

    assert artifacts.record.status == "success"
    assert artifacts.record.source_run == ""
