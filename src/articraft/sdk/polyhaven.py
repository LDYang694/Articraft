"""Poly Haven's CC0 texture library.

Two things make it worth having beside ambientCG. It carries far more
furniture-grade wood, which is what an indoor object usually needs, and it
publishes each texture's real size, so a tile can be scaled to what it actually
is instead of guessed.

It also ships maps as individual files rather than one zip per resolution, so
only the channels UsdPreviewSurface reads get downloaded.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from pathlib import Path

from articraft.sdk.textures import (
    CACHE_ROOT,
    DOWNLOAD_TIMEOUT_SECONDS,
    POLYHAVEN,
    Candidate,
    TextureSet,
)

_ASSETS_URL = "https://api.polyhaven.com/assets?t=textures"

# Words that name what a surface is made of, as opposed to what color it is.
# A query naming one of these must be honored: a search for blue carpet that
# returns blue tree bark is worse than returning nothing.
SUBSTANCES = frozenset(
    {
        "asphalt",
        "bark",
        "brick",
        "cardboard",
        "carpet",
        "ceramic",
        "concrete",
        "denim",
        "fabric",
        "granite",
        "grass",
        "gravel",
        "laminate",
        "leather",
        "linoleum",
        "marble",
        "metal",
        "paint",
        "paper",
        "parquet",
        "plank",
        "planks",
        "plaster",
        "plastic",
        "rock",
        "roof",
        "rubber",
        "rug",
        "sand",
        "stone",
        "terrazzo",
        "tile",
        "tiles",
        "veneer",
        "vinyl",
        "wicker",
        "wood",
    }
)
_FILES_URL = "https://api.polyhaven.com/files/{asset}"

# Poly Haven map name -> TextureSet channel. It also ships Displacement and a
# packed "arm", which UsdPreviewSurface has no use for.
_CHANNELS = {
    "Diffuse": "base_color",
    "Rough": "roughness",
    "nor_gl": "normal",
    "Metal": "metalness",
    "AO": "occlusion",
}


def search(query: str, *, limit: int = 6, cache_root: Path | None = None) -> list[Candidate]:
    """Rank textures by whole-word overlap with their tags, categories, and name.

    Whole words, because substrings pick the wrong thing confidently: "blue
    carpet" matches the tree bark asset "bark_bluegum" through "blue". Naming a
    substance is also stronger evidence than naming a color, so a term that
    names one counts double, and an asset that matches no named substance is
    dropped however many color words it happens to carry.
    """

    cache = (cache_root or CACHE_ROOT) / "polyhaven"
    catalogue = _catalogue(cache)
    terms = [term for term in re.split(r"[^a-z0-9]+", query.lower()) if len(term) > 2]
    wanted = [term for term in terms if term in SUBSTANCES]

    ranked: list[tuple[int, str, dict]] = []
    for slug, asset in catalogue.items():
        labels = _labels(slug, asset)
        if wanted and not any(term in labels for term in wanted):
            continue
        score = sum((2 if term in SUBSTANCES else 1) * (term in labels) for term in terms)
        if score:
            ranked.append((score, slug, asset))

    found: list[Candidate] = []
    for _, slug, asset in sorted(ranked, key=lambda entry: -entry[0]):
        preview = _preview(slug, asset, cache)
        if preview is None:
            continue
        found.append(
            Candidate(
                slug=f"{POLYHAVEN}{slug}",
                tags=tuple(str(tag) for tag in asset.get("tags") or []),
                preview=preview,
                tile_meters=_tile_meters(asset),
            )
        )
        if len(found) >= limit:
            break
    return found


def _labels(slug: str, asset: dict) -> set[str]:
    """Every whole word describing one asset, from its slug, name, tags, and categories."""

    text = " ".join(
        [
            slug,
            str(asset.get("name") or ""),
            *(str(tag) for tag in asset.get("tags") or []),
            *(str(item) for item in asset.get("categories") or []),
        ]
    ).lower()
    return {word for word in re.split(r"[^a-z0-9]+", text) if word}


def fetch_texture_set(
    asset: str,
    *,
    resolution: str = "1K",
    cache_root: Path | None = None,
) -> TextureSet:
    """Download (or reuse cached) a Poly Haven texture set for ``asset``."""

    size = resolution.lower()
    cache = (cache_root or CACHE_ROOT) / "polyhaven" / asset / size
    cache.mkdir(parents=True, exist_ok=True)

    found = {
        channel: path
        for channel in _CHANNELS.values()
        if (path := next(iter(cache.glob(f"{channel}.*")), None)) is not None
    }
    if "base_color" not in found:
        found = _download(asset, size, cache)
    if "base_color" not in found:
        raise RuntimeError(f"Poly Haven asset {asset!r} has no color map at {size}")

    return TextureSet(
        slug=f"{POLYHAVEN}{asset}",
        resolution=size,
        base_color=found["base_color"],
        roughness=found.get("roughness"),
        normal=found.get("normal"),
        metalness=found.get("metalness"),
        occlusion=found.get("occlusion"),
    )


def _download(asset: str, size: str, cache: Path) -> dict[str, Path]:
    listing = _json(_FILES_URL.format(asset=asset), f"could not list Poly Haven asset {asset!r}")
    found: dict[str, Path] = {}
    for name, channel in _CHANNELS.items():
        url = (((listing.get(name) or {}).get(size) or {}).get("jpg") or {}).get("url")
        if not isinstance(url, str):
            continue
        destination = cache / f"{channel}.jpg"
        if _save(url, destination):
            found[channel] = destination
    return found


def _catalogue(cache: Path) -> dict[str, dict]:
    """The whole texture index, cached, because one search reads all of it."""

    cache.mkdir(parents=True, exist_ok=True)
    index = cache / "assets.json"
    if not index.is_file():
        payload = _json(_ASSETS_URL, "could not reach the Poly Haven catalogue")
        index.write_text(json.dumps(payload), encoding="utf-8")
        return payload
    try:
        return json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        index.unlink(missing_ok=True)
        raise RuntimeError("the cached Poly Haven catalogue is unreadable") from None


def _preview(slug: str, asset: dict, cache: Path) -> Path | None:
    url = asset.get("thumbnail_url")
    if not isinstance(url, str):
        return None
    destination = cache / "thumbnails" / f"{slug}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return destination
    return destination if _save(url, destination) else None


def _tile_meters(asset: dict) -> float | None:
    """How much of the world one tile covers. Poly Haven publishes it in millimetres."""

    dimensions = asset.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        return None
    try:
        return round(float(dimensions[0]) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _json(url: str, message: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(message) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(message)
    return payload


def _save(url: str, destination: Path) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    except Exception:
        destination.unlink(missing_ok=True)
        return False
    return True
