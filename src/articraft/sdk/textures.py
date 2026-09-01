"""Where a surface's texture maps come from.

Two libraries, both CC0, so their maps can be bundled straight into an exported
USDZ. They cover different ground, which is the reason for having both:
ambientCG is broad and evenly shot, and Poly Haven has far more furniture-grade
wood and publishes each texture's real size, so a tile can be scaled to what it
actually is rather than guessed.

A material names one with its ``texture`` field. A bare name is ambientCG, and
``polyhaven:`` in front of it picks the other::

    OAK = Material.HARDWOOD.but(texture="Wood049")
    TABLE = Material.HARDWOOD.but(texture="polyhaven:wood_table_001")

This module owns the shapes both libraries return and the dispatch between
them. The libraries themselves live in `ambientcg.py` and `polyhaven.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from articraft.sdk.materials import Material

CACHE_ROOT = Path.home() / ".cache" / "articraft" / "textures"
DOWNLOAD_TIMEOUT_SECONDS = 30
POLYHAVEN = "polyhaven:"


@dataclass(frozen=True, slots=True)
class TextureSet:
    """Local paths to a fetched PBR texture set.

    Only ``base_color`` is guaranteed; the rest are present when the material
    provides them.
    """

    slug: str
    resolution: str
    base_color: Path
    roughness: Path | None = None
    normal: Path | None = None
    metalness: Path | None = None
    occlusion: Path | None = None

    def maps(self) -> dict[str, Path]:
        found = {"base_color": self.base_color}
        for channel in ("roughness", "normal", "metalness", "occlusion"):
            path = getattr(self, channel)
            if path is not None:
                found[channel] = path
        return found


@dataclass(frozen=True, slots=True)
class MaterialSpec:
    """Which library asset backs one surface."""

    asset: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """One search hit: what to name in ``texture``, and what it looks like.

    ``tile_meters`` is how much of the world one tile covers, when the library
    says. Poly Haven publishes it, ambientCG does not, and knowing it turns
    ``texture_scale`` from a guess into a reading.
    """

    slug: str
    tags: tuple[str, ...]
    preview: Path
    tile_meters: float | None = None


def search(query: str, *, limit: int = 6, cache_root: Path | None = None) -> list[Candidate]:
    """Search both libraries and interleave what they return.

    Interleaved rather than concatenated so one library cannot fill the whole
    answer. A search for wood that returned six ambientCG hits would hide the
    library that actually has the furniture-grade stock.
    """

    # Imported here because both libraries build the shapes defined above.
    from articraft.sdk import ambientcg, polyhaven

    half = max(1, (limit + 1) // 2)
    results = []
    for library in (polyhaven, ambientcg):
        try:
            results.append(library.search(query, limit=limit, cache_root=cache_root))
        except RuntimeError:
            results.append([])
    if not any(results):
        raise RuntimeError(f"could not search either texture library for {query!r}")

    merged: list[Candidate] = []
    for index in range(limit):
        for found in results:
            if index < len(found) and (index < half or len(merged) < limit):
                merged.append(found[index])
    return merged[:limit]


def fetch_material(
    kind: Material,
    *,
    resolution: str = "1K",
    cache_root: Path | None = None,
) -> tuple[TextureSet, MaterialSpec]:
    """Fetch the texture set for ``kind`` from whichever library it names.

    The asset comes from the material itself, so a derived material keeps the
    texture of whatever it was derived from.
    """

    from articraft.sdk import ambientcg, polyhaven

    if kind.texture is None:
        raise RuntimeError(f"material {kind.name!r} has no texture")
    spec = MaterialSpec(kind.texture)
    if kind.texture.startswith(POLYHAVEN):
        asset = kind.texture[len(POLYHAVEN) :]
        found = polyhaven.fetch_texture_set(asset, resolution=resolution, cache_root=cache_root)
    else:
        found = ambientcg.fetch_texture_set(
            kind.texture, resolution=resolution, cache_root=cache_root
        )
    return found, spec
