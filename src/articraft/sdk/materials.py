"""What a shape is made of.

A :class:`Material` answers every physical question about a shape at once: how
heavy it is, how it behaves on contact, and how it looks. Naming one from the
library is usually all a shape needs::

    body.add(shell, name="shell", material=Material.STEEL)

Derive a variant with :meth:`Material.but` when a shape differs from the library
entry, and assign it to a name to reuse it across shapes and parts::

    BRUSHED = Material.STEEL.but(roughness=0.75)

Build one directly when the library has nothing close. ``density`` is required
because mass cannot be measured without it. ``friction`` is optional, and a
material without it authors no contact behavior at all rather than inventing a
coefficient::

    CERAMIC = Material(name="ceramic", density=2400.0, friction=(0.45, 0.40))

Prefer the library. Its numbers were checked; an invented one is only as good as
the guess behind it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import ClassVar, TypeAlias

from articraft.sdk._values import _positive
from articraft.sdk.errors import ValidationError

Color: TypeAlias = tuple[float, float, float, float]
Rgb: TypeAlias = tuple[float, float, float]
Friction: TypeAlias = tuple[float, float]


class _Unspecified:
    pass


_UNSPECIFIED = _Unspecified()


@dataclass(frozen=True)
class Material:
    """What a shape is made of, and everything that follows from it.

    - ``density`` in kg/m^3 decides the shape's mass from its measured volume.
    - ``friction`` is ``(static, dynamic)``; ``None`` authors no contact
      behavior, leaving the simulator's own default in place.
    - ``restitution`` in ``[0, 1]`` is how much impact speed survives a bounce.
    - ``base_color`` ``(r, g, b, a)``, ``metallic``, ``roughness``, and
      ``emissive`` describe how the surface responds to light; alpha is
      opacity. Both colors are display colors, the numbers a color picker
      shows, and are linearized on the way into a renderer.
    - ``clearcoat`` adds a clear layer over the surface, which is what makes
      enamel, lacquer, and glazed ceramic read wet rather than chalky.
      ``clearcoat_roughness`` is how sharp that layer's reflection is.
    - ``ior`` is the index of refraction, used by a transparent surface.
    - ``texture`` names an ambientCG asset, used when textures are enabled,
      ``texture_scale`` is how many meters one tile of it covers, and
      ``texture_rotation`` in radians turns the tile on the surface. Grain runs
      along a board, so a rail cut from the same stock as a stile needs the
      quarter turn between them.
    - ``pattern_strength`` is how much of a texture's own variation survives.
      One keeps the asset as shot. Lower values settle it toward flat color,
      for a surface that is painted or veneered rather than figured.
    """

    name: str
    density: float
    friction: Friction | None = None
    restitution: float | None = None
    base_color: Color = (0.8, 0.8, 0.8, 1.0)
    metallic: float = 0.0
    roughness: float = 0.6
    clearcoat: float = 0.0
    clearcoat_roughness: float = 0.1
    ior: float | None = None
    emissive: Rgb | None = None
    texture: str | None = None
    texture_scale: float | None = None
    texture_rotation: float = 0.0
    pattern_strength: float = 1.0

    # Library entries, defined after the class body.
    STEEL: ClassVar[Material]
    ALUMINUM: ClassVar[Material]
    ABS_PLASTIC: ClassVar[Material]
    GLASS: ClassVar[Material]
    HARDWOOD: ClassVar[Material]
    RUBBER: ClassVar[Material]
    PAINTED_STEEL: ClassVar[Material]
    CERAMIC: ClassVar[Material]
    WOVEN_FABRIC: ClassVar[Material]

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValidationError("material name must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "density", _positive(self.density, field_name=f"material {name!r} density")
        )
        if self.friction is not None:
            object.__setattr__(self, "friction", _as_friction(self.friction, name=name))
        if self.restitution is not None:
            object.__setattr__(
                self,
                "restitution",
                _as_unit(self.restitution, field_name=f"material {name!r} restitution"),
            )
        object.__setattr__(
            self, "base_color", _as_color(self.base_color, field_name=f"material {name!r} color")
        )
        object.__setattr__(
            self, "metallic", _as_unit(self.metallic, field_name=f"material {name!r} metallic")
        )
        object.__setattr__(
            self, "roughness", _as_unit(self.roughness, field_name=f"material {name!r} roughness")
        )
        object.__setattr__(
            self, "clearcoat", _as_unit(self.clearcoat, field_name=f"material {name!r} clearcoat")
        )
        object.__setattr__(
            self,
            "clearcoat_roughness",
            _as_unit(self.clearcoat_roughness, field_name=f"material {name!r} clearcoat roughness"),
        )
        if self.ior is not None:
            object.__setattr__(
                self, "ior", _positive(self.ior, field_name=f"material {name!r} ior")
            )
        if self.texture_scale is not None:
            object.__setattr__(
                self,
                "texture_scale",
                _positive(self.texture_scale, field_name=f"material {name!r} texture scale"),
            )
        object.__setattr__(
            self,
            "texture_rotation",
            _as_angle(self.texture_rotation, field_name=f"material {name!r} texture rotation"),
        )
        object.__setattr__(
            self,
            "pattern_strength",
            _as_unit(self.pattern_strength, field_name=f"material {name!r} pattern strength"),
        )
        if self.emissive is not None:
            object.__setattr__(
                self, "emissive", _as_rgb(self.emissive, field_name=f"material {name!r} emissive")
            )

    def but(
        self,
        *,
        name: str | None = None,
        density: float | None = None,
        friction: Friction | None | _Unspecified = _UNSPECIFIED,
        restitution: float | None | _Unspecified = _UNSPECIFIED,
        color: Sequence[float] | None = None,
        metallic: float | None = None,
        roughness: float | None = None,
        clearcoat: float | None = None,
        clearcoat_roughness: float | None = None,
        ior: float | None | _Unspecified = _UNSPECIFIED,
        emissive: Rgb | None | _Unspecified = _UNSPECIFIED,
        texture: str | None | _Unspecified = _UNSPECIFIED,
        texture_scale: float | None | _Unspecified = _UNSPECIFIED,
        texture_rotation: float | None = None,
        pattern_strength: float | None = None,
    ) -> Material:
        """This material with some properties changed, keeping the rest.

        A derived material keeps its origin's texture unless you name another
        one, so brushed steel still looks like steel when textures are enabled.
        Pass ``None`` to clear friction, restitution, ior, emissive, or texture
        values.
        """

        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if density is not None:
            changes["density"] = density
        if friction is not _UNSPECIFIED:
            changes["friction"] = friction
        if restitution is not _UNSPECIFIED:
            changes["restitution"] = restitution
        if color is not None:
            changes["base_color"] = _as_color(color, field_name=f"material {self.name!r} color")
        if metallic is not None:
            changes["metallic"] = metallic
        if roughness is not None:
            changes["roughness"] = roughness
        if clearcoat is not None:
            changes["clearcoat"] = clearcoat
        if clearcoat_roughness is not None:
            changes["clearcoat_roughness"] = clearcoat_roughness
        if ior is not _UNSPECIFIED:
            changes["ior"] = ior
        if emissive is not _UNSPECIFIED:
            changes["emissive"] = emissive
        if texture is not _UNSPECIFIED:
            changes["texture"] = texture
        if texture_scale is not _UNSPECIFIED:
            changes["texture_scale"] = texture_scale
        if texture_rotation is not None:
            changes["texture_rotation"] = texture_rotation
        if pattern_strength is not None:
            changes["pattern_strength"] = pattern_strength
        return replace(self, **changes)  # pyright: ignore[reportArgumentType]

    @property
    def opacity(self) -> float:
        return self.base_color[3]

    @property
    def static_friction(self) -> float | None:
        return None if self.friction is None else self.friction[0]

    @property
    def dynamic_friction(self) -> float | None:
        return None if self.friction is None else self.friction[1]


def to_linear(color: Sequence[float]) -> tuple[float, ...]:
    """An authored display color as the linear values light transport needs.

    Colors are authored the way a color picker shows them, which is sRGB
    encoded. USD's ``diffuseColor``, the recolor that moves a texture's average
    onto an authored one, and any shading math all work in linear space.
    Handing them the encoded number made every surface about a stop too bright
    and washed out: oak's ``(0.62, 0.45, 0.24)`` rendered as bleached pine.

    2.2 is the usual stand-in for the sRGB curve. It is off by at most a couple
    of levels, and it matches what the texture averages are linearized with.
    """

    return tuple(float(component) ** 2.2 for component in color)


def is_library_material(material: Material) -> bool:
    """Whether this is a library entry rather than a derived or invented one.

    Recorded in the manifest so a reviewer can tell which numbers were checked.
    """
    return any(material == entry for entry in LIBRARY)


def _as_material(value: object, *, field_name: str) -> Material:
    if not isinstance(value, Material):
        names = ", ".join(f"Material.{entry.name.upper()}" for entry in LIBRARY)
        raise ValidationError(f"{field_name} must be a Material (library: {names})")
    return value


def _as_friction(value: object, *, name: str) -> Friction:
    try:
        numbers = tuple(float(item) for item in value)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"material {name!r} friction must be (static, dynamic)") from exc
    if len(numbers) != 2:
        raise ValidationError(f"material {name!r} friction must be (static, dynamic)")
    for coefficient in numbers:
        if not math.isfinite(coefficient) or coefficient < 0.0:
            raise ValidationError(f"material {name!r} friction must be non-negative and finite")
    return (numbers[0], numbers[1])


def _as_color(value: Sequence[float], *, field_name: str) -> Color:
    if isinstance(value, (str, bytes)):
        raise ValidationError(f"{field_name} must have 3 or 4 numeric values")
    try:
        raw = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field_name} must have 3 or 4 numeric values") from exc
    if len(raw) == 3:
        raw = (*raw, 1.0)
    if len(raw) != 4:
        raise ValidationError(f"{field_name} must have 3 or 4 numeric values")
    if any(not math.isfinite(component) for component in raw):
        raise ValidationError(f"{field_name} values must be finite")
    if any(component < 0.0 or component > 1.0 for component in raw):
        raise ValidationError(f"{field_name} values must be between 0.0 and 1.0")
    return raw


def _as_rgb(value: Sequence[float], *, field_name: str) -> Rgb:
    if isinstance(value, (str, bytes)):
        raise ValidationError(f"{field_name} must have 3 numeric values")
    try:
        raw = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field_name} must have 3 numeric values") from exc
    if len(raw) != 3:
        raise ValidationError(f"{field_name} must have 3 numeric values")
    if any(not math.isfinite(component) for component in raw):
        raise ValidationError(f"{field_name} values must be finite")
    if any(component < 0.0 or component > 1.0 for component in raw):
        raise ValidationError(f"{field_name} values must be between 0.0 and 1.0")
    return raw


def _as_angle(value: float, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field_name} must be an angle in radians") from exc
    if not math.isfinite(number):
        raise ValidationError(f"{field_name} must be finite")
    return number


def _as_unit(value: float, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field_name} must be a number between 0.0 and 1.0") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValidationError(f"{field_name} must be between 0.0 and 1.0")
    return number


# Densities are standard engineering values. Friction and restitution are
# per-material approximations, which a simulator combines with whatever the shape
# is actually touching. Texture scales are the real size of one tile of the
# named ambientCG asset, so a wood plank stays plank-sized on a cabinet door.
Material.STEEL = Material(
    name="steel",
    density=7850.0,
    friction=(0.42, 0.36),
    restitution=0.55,
    base_color=(0.72, 0.73, 0.76, 1.0),
    metallic=1.0,
    roughness=0.35,
    texture="Metal009",
    texture_scale=0.5,
)
Material.ALUMINUM = Material(
    name="aluminum",
    density=2700.0,
    friction=(0.45, 0.38),
    restitution=0.40,
    base_color=(0.85, 0.86, 0.88, 1.0),
    metallic=1.0,
    roughness=0.28,
    texture="Metal050A",
    texture_scale=0.5,
)
Material.ABS_PLASTIC = Material(
    name="abs_plastic",
    density=1050.0,
    friction=(0.40, 0.32),
    restitution=0.45,
    base_color=(0.80, 0.80, 0.82, 1.0),
    roughness=0.45,
    texture="Plastic010",
    texture_scale=0.3,
)
Material.GLASS = Material(
    name="glass",
    density=2500.0,
    friction=(0.40, 0.35),
    restitution=0.60,
    base_color=(0.90, 0.93, 0.96, 0.25),
    roughness=0.05,
    ior=1.45,
)
Material.HARDWOOD = Material(
    name="hardwood",
    density=700.0,
    friction=(0.50, 0.40),
    restitution=0.35,
    base_color=(0.62, 0.45, 0.24, 1.0),
    roughness=0.75,
    texture="Wood062",
    texture_scale=1.0,
)
Material.RUBBER = Material(
    name="rubber",
    density=1200.0,
    friction=(0.95, 0.85),
    restitution=0.75,
    base_color=(0.12, 0.12, 0.13, 1.0),
    roughness=0.90,
    texture="Rubber004",
    texture_scale=0.2,
)
# Painted steel is steel underneath, so it keeps steel's density and contact
# values. What differs is the surface: enamel is a dielectric clear layer over
# pigment, not bare metal.
Material.PAINTED_STEEL = Material(
    name="painted_steel",
    density=7850.0,
    friction=(0.40, 0.34),
    restitution=0.50,
    base_color=(0.92, 0.93, 0.93, 1.0),
    metallic=0.0,
    roughness=0.30,
    clearcoat=0.7,
    clearcoat_roughness=0.08,
)
Material.CERAMIC = Material(
    name="ceramic",
    density=2400.0,
    friction=(0.45, 0.40),
    restitution=0.30,
    base_color=(0.95, 0.95, 0.94, 1.0),
    roughness=0.15,
    clearcoat=0.8,
    clearcoat_roughness=0.05,
)
Material.WOVEN_FABRIC = Material(
    name="woven_fabric",
    density=400.0,
    friction=(0.60, 0.50),
    restitution=0.15,
    base_color=(0.55, 0.55, 0.58, 1.0),
    roughness=0.95,
    texture="Fabric004",
    texture_scale=0.25,
)

LIBRARY: tuple[Material, ...] = (
    Material.STEEL,
    Material.ALUMINUM,
    Material.ABS_PLASTIC,
    Material.GLASS,
    Material.HARDWOOD,
    Material.RUBBER,
    Material.PAINTED_STEEL,
    Material.CERAMIC,
    Material.WOVEN_FABRIC,
)
