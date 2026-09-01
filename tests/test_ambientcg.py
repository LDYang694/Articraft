from __future__ import annotations

import io
import json
import zipfile

from articraft.sdk import ambientcg


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("nested/Metal009_1K-JPG_Color.jpg", b"color")
        archive.writestr("nested/Metal009_1K-JPG_Roughness.jpg", b"roughness")
        archive.writestr("nested/unrelated.txt", b"ignored")
    return output.getvalue()


def _search_payload() -> bytes:
    return json.dumps(
        {
            "foundAssets": [
                {
                    "assetId": "Wood092",
                    "tags": ["fine", "smooth", "orange", "wood"],
                    "previewImage": {
                        "256-PNG": "https://example.invalid/small.png",
                        "512-PNG": "https://example.invalid/Wood092.png",
                    },
                },
                {
                    "assetId": "Wood049",
                    "tags": ["grain", "horizontal", "wood"],
                    "previewImage": {"512-PNG": "https://example.invalid/Wood049.png"},
                },
                # No preview, so nothing to judge it by and nothing to return.
                {"assetId": "Wood001", "tags": ["wood"]},
            ]
        }
    ).encode()


def test_search_returns_tags_and_caches_one_preview_each(monkeypatch, tmp_path) -> None:
    """A slug says nothing about a grain, so a search that cannot be seen is useless."""

    requested: list[str] = []

    def urlopen(request, *, timeout):
        requested.append(request.full_url)
        if "full_json" in request.full_url:
            return io.BytesIO(_search_payload())
        return io.BytesIO(b"preview bytes")

    monkeypatch.setattr(ambientcg.urllib.request, "urlopen", urlopen)

    found = ambientcg.search("fine oak", limit=4, cache_root=tmp_path)
    again = ambientcg.search("fine oak", limit=4, cache_root=tmp_path)

    assert [candidate.slug for candidate in found] == ["Wood092", "Wood049"]
    assert found[0].tags == ("fine", "smooth", "orange", "wood")
    assert found[0].preview.read_bytes() == b"preview bytes"
    # The larger preview wins, because a small one cannot show a grain.
    assert "https://example.invalid/Wood092.png" in requested
    assert "q=fine%20oak" in requested[0]
    # Previews are cached, so a repeated search only re-runs the query itself.
    assert requested.count("https://example.invalid/Wood092.png") == 1
    assert [candidate.slug for candidate in again] == ["Wood092", "Wood049"]


def test_fetch_texture_set_downloads_once_and_reuses_cache(monkeypatch, tmp_path) -> None:
    downloads = 0

    def urlopen(_request, *, timeout):
        nonlocal downloads
        downloads += 1
        assert timeout == 30
        return io.BytesIO(_archive())

    monkeypatch.setattr(ambientcg.urllib.request, "urlopen", urlopen)

    first = ambientcg.fetch_texture_set("Metal009", cache_root=tmp_path)
    second = ambientcg.fetch_texture_set("Metal009", cache_root=tmp_path)

    assert downloads == 1
    assert first == second
    assert first.base_color.read_bytes() == b"color"
    assert first.roughness is not None
    assert first.roughness.read_bytes() == b"roughness"
    assert not list(tmp_path.rglob("*.zip"))
