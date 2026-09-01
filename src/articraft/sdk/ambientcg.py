"""ambientCG's CC0 texture library, cached after the first pull.

The broad, evenly shot half of the two libraries in `textures.py`. It ships one
zip per resolution holding the whole map set, so this downloads the zip once,
extracts it into the cache, and locates the channels by filename suffix.

Download scheme (no API key)::

    https://ambientcg.com/get?file=<asset>_<res>-JPG.zip
      -> <asset>_<res>-JPG_Color.jpg / _Roughness.jpg / _NormalGL.jpg / _Metalness.jpg
"""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from articraft.sdk.textures import (
    CACHE_ROOT,
    DOWNLOAD_TIMEOUT_SECONDS,
    Candidate,
    TextureSet,
)

_URL = "https://ambientcg.com/get?file={asset}_{res}-JPG.zip"
_SEARCH_URL = (
    "https://ambientcg.com/api/v2/full_json"
    "?type=Material&q={query}&limit={limit}&include=displayData,tagData,imageData"
)
_THUMBNAIL_SIZES = ("512-PNG", "256-PNG", "1024-PNG", "128-PNG")

# ambientCG filename suffix -> TextureSet channel. The zips also carry
# NormalDX and Displacement, which UsdPreviewSurface has no use for.
_CHANNELS = {
    "_Color.": "base_color",
    "_Roughness.": "roughness",
    "_NormalGL.": "normal",
    "_Metalness.": "metalness",
    "_AmbientOcclusion.": "occlusion",
}


def fetch_texture_set(
    asset: str,
    *,
    resolution: str = "1K",
    cache_root: Path | None = None,
) -> TextureSet:
    """Download (or reuse cached) an ambientCG material set for ``asset``.

    Raises ``RuntimeError`` if the zip cannot be fetched or has no color map, so
    callers can fall back to a parametric ``Material``.
    """

    cache = (cache_root or (CACHE_ROOT / "ambientcg")) / asset / resolution
    cache.mkdir(parents=True, exist_ok=True)

    found = _locate(cache)
    if "base_color" not in found:
        _download_and_extract(_URL.format(asset=asset, res=resolution), cache)
        found = _locate(cache)
    if "base_color" not in found:
        raise RuntimeError(f"ambientCG asset {asset!r} has no color map")

    return TextureSet(
        slug=asset,
        resolution=resolution,
        base_color=found["base_color"],
        roughness=found.get("roughness"),
        normal=found.get("normal"),
        metalness=found.get("metalness"),
        occlusion=found.get("occlusion"),
    )


def search(
    query: str,
    *,
    limit: int = 6,
    cache_root: Path | None = None,
) -> list[Candidate]:
    """Find ambientCG materials matching ``query``, newest and most popular first.

    A hand-written list of slugs is the wrong way to choose a surface. It ages,
    it is short, and a name says nothing about how coarse a grain is: one run
    spent three reviews fighting the grain direction of an asset whose own tags
    read "grain, horizontal". The catalogue holds hundreds of woods tagged
    fine, smooth, light, or plain, so the honest answer is to look.

    Each candidate carries a cached thumbnail, because the tags narrow the
    field but only the picture settles it.
    """

    url = _SEARCH_URL.format(query=urllib.parse.quote(query), limit=max(1, min(limit, 24)))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"could not search ambientCG for {query!r}") from exc

    cache = (cache_root or (CACHE_ROOT / "ambientcg")) / "_thumbnails"
    cache.mkdir(parents=True, exist_ok=True)
    candidates: list[Candidate] = []
    for asset in payload.get("foundAssets") or []:
        slug = str(asset.get("assetId") or "")
        thumbnail = _thumbnail(asset, cache)
        if not slug or thumbnail is None:
            continue
        candidates.append(
            Candidate(
                slug=slug,
                tags=tuple(str(tag) for tag in asset.get("tags") or []),
                preview=thumbnail,
            )
        )
    return candidates


def _thumbnail(asset: dict, cache: Path) -> Path | None:
    """Cache one preview image, preferring a size large enough to read a grain."""

    images = asset.get("previewImage")
    if not isinstance(images, dict):
        return None
    url = next((images[key] for key in _THUMBNAIL_SIZES if key in images), None)
    if not isinstance(url, str):
        return None
    destination = cache / f"{asset.get('assetId')}.png"
    if destination.is_file():
        return destination
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    except Exception:
        destination.unlink(missing_ok=True)
        return None
    return destination


def _locate(cache: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in cache.iterdir():
        for suffix, channel in _CHANNELS.items():
            if suffix in path.name:
                found[channel] = path
    return found


def _download_and_extract(url: str, cache: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with tempfile.NamedTemporaryFile(
        prefix="_download-", suffix=".zip", dir=cache, delete=False
    ) as file:
        zip_path = Path(file.name)
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            zip_path.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    except Exception as exc:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"could not download ambientCG asset: {url}") from exc
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                if any(suffix in member for suffix in _CHANNELS):
                    destination = cache / Path(member).name
                    with archive.open(member) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
    finally:
        zip_path.unlink(missing_ok=True)
