"""Choosing a texture by looking at it, rather than from a hand-written list."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

import articraft.agent.tools.find_texture as find_texture
from articraft.agent.tools import ToolContext, get
from articraft.agent.workspace.local import LocalWorkspace
from articraft.sdk.textures import Candidate


def _run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable)


def _context(tmp_path: Path) -> ToolContext:
    env = LocalWorkspace(output_dir=tmp_path)
    run_dir = env.create_run("textures")
    return ToolContext(env, run_dir, run_dir / "workspace")


def _preview(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.png"
    Image.new("RGB", (96, 96), (160, 120, 80)).save(path)
    return path


def test_search_returns_slugs_with_their_tags_and_previews(monkeypatch, tmp_path) -> None:
    def search(query: str, *, limit: int) -> list[Candidate]:
        assert (query, limit) == ("fine oak", 2)
        return [
            Candidate("Wood092", ("fine", "smooth"), _preview(tmp_path, "Wood092")),
            Candidate("Wood049", ("grain", "horizontal"), _preview(tmp_path, "Wood049")),
        ]

    monkeypatch.setattr(find_texture.textures, "search", search)

    result = _run(get("find_texture").run(_context(tmp_path), {"query": "fine oak", "limit": 2}))

    assert result.output["found"] == [
        {"texture": "Wood092", "tags": ["fine", "smooth"]},
        {"texture": "Wood049", "tags": ["grain", "horizontal"]},
    ]
    # The pictures come back too. Tags narrow the field, but only the preview
    # shows how coarse a grain is, or which way it runs.
    assert [item["type"] for item in result.content_items] == ["input_image", "input_image"]


def test_the_reference_goes_with_the_swatches(monkeypatch, tmp_path) -> None:
    """Swatch against photograph is the comparison a model can actually make.

    A studio render and a product shot differ in light, exposure, and camera,
    so only hue survives between them. Two flat samples of a material do not
    have that problem.
    """

    monkeypatch.setattr(
        find_texture.textures,
        "search",
        lambda query, *, limit: [Candidate("Wood092", ("fine",), _preview(tmp_path, "Wood092"))],
    )
    context = _context(tmp_path)
    Image.new("RGB", (96, 96), (190, 160, 130)).save(context.workspace / "reference.png")

    result = _run(get("find_texture").run(context, {"query": "oak"}))

    assert result.output["images"].endswith("then the run's reference photograph")
    assert [item["type"] for item in result.content_items] == ["input_image", "input_image"]
    # The reference is something to match against, not one of the hits.
    assert result.output["found"] == [{"texture": "Wood092", "tags": ["fine"]}]


def test_a_phrase_that_matches_nothing_is_widened_a_word_at_a_time(monkeypatch, tmp_path) -> None:
    """The catalogue matches every term at once, so a phrase finds nothing.

    A tool that returned nothing would push the run back to guessing slugs,
    which is what the search exists to replace.
    """

    tried: list[str] = []

    def search(query: str, *, limit: int):
        tried.append(query)
        if query != "wood fine":
            return []
        return [Candidate("Wood092", ("fine", "wood"), _preview(tmp_path, "Wood092"))]

    monkeypatch.setattr(find_texture.textures, "search", search)

    result = _run(get("find_texture").run(_context(tmp_path), {"query": "wood fine light"}))

    # Words go from the end, so what survives is the material class. Widening
    # toward a species instead reached "oak", which matches two assets and one
    # of them is tree bark.
    assert tried == ["wood fine light", "wood fine"]
    assert result.output["query"] == "wood fine light"
    assert result.output["searched"] == "wood fine"
    assert result.output["found"] == [{"texture": "Wood092", "tags": ["fine", "wood"]}]


def test_a_search_with_no_hits_says_so_rather_than_failing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(find_texture.textures, "search", lambda query, *, limit: [])

    result = _run(get("find_texture").run(_context(tmp_path), {"query": "unobtainium"}))

    assert result.output == {"query": "unobtainium", "found": []}
    assert result.content_items == []


def test_a_failed_search_comes_back_as_data(monkeypatch, tmp_path) -> None:
    """Being offline should not end a run over a texture that has a fallback."""

    def refuse(query: str, *, limit: int):
        raise RuntimeError("could not search ambientCG for 'oak'")

    monkeypatch.setattr(find_texture.textures, "search", refuse)

    result = _run(get("find_texture").run(_context(tmp_path), {"query": "oak"}))

    assert result.output == {"error": "could not search ambientCG for 'oak'"}


def test_search_needs_a_query_and_a_sane_limit(tmp_path) -> None:
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="query must name the surface"):
        _run(get("find_texture").run(context, {"query": "  "}))
    with pytest.raises(ValueError, match="limit must be a number from 1 to 8"):
        _run(get("find_texture").run(context, {"query": "oak", "limit": 40}))
