from __future__ import annotations

from articraft.settings import DEFAULT_MAX_TURNS, DEFAULT_OPENAI_REASONING_EFFORT, Settings


def test_settings_ignore_local_dotenv_by_default_in_tests(tmp_path, monkeypatch) -> None:
    tmp_path.joinpath(".env").write_text("ARTICRAFT_MAX_TURNS=999\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().max_turns == DEFAULT_MAX_TURNS  # pyright: ignore[reportCallIssue]


def test_settings_default_openai_reasoning_effort_is_xhigh(monkeypatch) -> None:
    monkeypatch.delenv("ARTICRAFT_REASONING_EFFORT", raising=False)

    assert DEFAULT_OPENAI_REASONING_EFFORT == "xhigh"
    assert Settings().openai_reasoning_effort == "xhigh"  # pyright: ignore[reportCallIssue]
