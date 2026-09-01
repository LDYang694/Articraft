"""The Poly Haven library, and the dispatch between it and ambientCG."""

from __future__ import annotations

import io
import json

import pytest

from articraft.sdk import polyhaven, textures
from articraft.sdk.materials import Material

CATALOGUE = {
    "wood_table_001": {
        "name": "Wood Table 001",
        "tags": ["wood", "table", "varnished", "fine"],
        "categories": ["wood", "furniture"],
        "dimensions": [1500, 1500.0001],
        "thumbnail_url": "https://example.invalid/wood_table_001.png",
    },
    "rough_plank_02": {
        "name": "Rough Plank 02",
        "tags": ["wood", "rough"],
        "categories": ["wood"],
        "dimensions": [2000, 2000],
        "thumbnail_url": "https://example.invalid/rough_plank_02.png",
    },
    "concrete_floor_03": {
        "name": "Concrete Floor 03",
        "tags": ["concrete"],
        "categories": ["floor"],
        "dimensions": [4000, 4000],
        "thumbnail_url": "https://example.invalid/concrete_floor_03.png",
    },
}

FILES = {
    "Diffuse": {"1k": {"jpg": {"url": "https://example.invalid/diff.jpg"}}},
    "Rough": {"1k": {"jpg": {"url": "https://example.invalid/rough.jpg"}}},
    "nor_gl": {"1k": {"jpg": {"url": "https://example.invalid/nor.jpg"}}},
    "AO": {"1k": {"jpg": {"url": "https://example.invalid/ao.jpg"}}},
    # Only offered at a resolution nobody asked for, so it is not fetched.
    "Metal": {"4k": {"jpg": {"url": "https://example.invalid/metal.jpg"}}},
    "Displacement": {"1k": {"jpg": {"url": "https://example.invalid/disp.jpg"}}},
}


@pytest.fixture
def network(monkeypatch):
    """Answer the two endpoints, and count what was asked for."""

    requested: list[str] = []

    def urlopen(request, *, timeout):
        url = request.full_url
        requested.append(url)
        if "assets?t=textures" in url:
            return io.BytesIO(json.dumps(CATALOGUE).encode())
        if "/files/" in url:
            return io.BytesIO(json.dumps(FILES).encode())
        return io.BytesIO(b"image bytes")

    monkeypatch.setattr(polyhaven.urllib.request, "urlopen", urlopen)
    return requested


def test_search_ranks_by_whole_word_overlap(network, tmp_path) -> None:
    found = polyhaven.search("wood fine", limit=6, cache_root=tmp_path)

    # Both are wood, and only one is also fine, so it comes first.
    assert [candidate.slug for candidate in found] == [
        "polyhaven:wood_table_001",
        "polyhaven:rough_plank_02",
    ]
    assert found[0].tags == ("wood", "table", "varnished", "fine")
    assert found[0].preview.read_bytes() == b"image bytes"
    # Poly Haven publishes the real size, which is what texture_scale wants.
    # ambientCG does not, so there it stays a guess.
    assert found[0].tile_meters == 1.5


def test_a_color_word_alone_never_picks_a_substance(network, tmp_path) -> None:
    """Substring matching sent "blue carpet" to a tree bark called bluegum."""

    assert polyhaven.search("blue carpet", limit=6, cache_root=tmp_path) == []
    # The substance has to be matched. Naming wood reaches the wood, and the
    # colour only orders what is left.
    found = polyhaven.search("varnished wood", limit=6, cache_root=tmp_path)
    assert found[0].slug == "polyhaven:wood_table_001"


def test_search_reuses_the_catalogue_and_the_thumbnails(network, tmp_path) -> None:
    polyhaven.search("wood", limit=6, cache_root=tmp_path)
    before = len(network)
    polyhaven.search("wood", limit=6, cache_root=tmp_path)

    assert len(network) == before, network[before:]


def test_fetch_takes_the_channels_a_preview_surface_reads(network, tmp_path) -> None:
    found = polyhaven.fetch_texture_set("wood_table_001", cache_root=tmp_path)

    assert found.slug == "polyhaven:wood_table_001"
    assert set(found.maps()) == {"base_color", "roughness", "normal", "occlusion"}
    assert found.base_color.read_bytes() == b"image bytes"
    # Displacement has no UsdPreviewSurface input, and Metal was not offered at
    # this resolution.
    assert found.metalness is None
    assert not any("disp" in url for url in network)


def test_a_prefixed_texture_goes_to_poly_haven_and_a_bare_one_to_ambientcg(
    network, monkeypatch, tmp_path
) -> None:
    """One field names both libraries, so the prefix is the whole dispatch."""

    asked: list[str] = []

    def ambient_fetch(asset, *, resolution, cache_root):
        asked.append(asset)
        return textures.TextureSet(asset, resolution, tmp_path / "color.jpg")

    monkeypatch.setattr("articraft.sdk.ambientcg.fetch_texture_set", ambient_fetch)

    bare = Material.HARDWOOD.but(name="bare", texture="Wood049")
    prefixed = Material.HARDWOOD.but(name="prefixed", texture="polyhaven:wood_table_001")

    assert textures.fetch_material(bare, cache_root=tmp_path)[0].slug == "Wood049"
    assert asked == ["Wood049"]
    assert (
        textures.fetch_material(prefixed, cache_root=tmp_path)[0].slug == "polyhaven:wood_table_001"
    )


def test_search_interleaves_the_two_libraries(monkeypatch, tmp_path) -> None:
    """Concatenating would let one library fill the whole answer."""

    def fake(prefix):
        def search(query, *, limit, cache_root=None):
            return [
                textures.Candidate(f"{prefix}{index}", (), tmp_path / "p.png")
                for index in range(limit)
            ]

        return search

    monkeypatch.setattr("articraft.sdk.polyhaven.search", fake("ph"))
    monkeypatch.setattr("articraft.sdk.ambientcg.search", fake("acg"))

    found = textures.search("wood", limit=4, cache_root=tmp_path)

    assert [candidate.slug for candidate in found] == ["ph0", "acg0", "ph1", "acg1"]


def test_one_library_being_down_does_not_lose_the_other(monkeypatch, tmp_path) -> None:
    def broken(query, *, limit, cache_root=None):
        raise RuntimeError("offline")

    def working(query, *, limit, cache_root=None):
        return [textures.Candidate("Wood049", (), tmp_path / "p.png")]

    monkeypatch.setattr("articraft.sdk.polyhaven.search", broken)
    monkeypatch.setattr("articraft.sdk.ambientcg.search", working)

    assert [c.slug for c in textures.search("wood", cache_root=tmp_path)] == ["Wood049"]

    monkeypatch.setattr("articraft.sdk.ambientcg.search", broken)
    with pytest.raises(RuntimeError, match="could not search either"):
        textures.search("wood", cache_root=tmp_path)
