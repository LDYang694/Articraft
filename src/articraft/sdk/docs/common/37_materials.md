# Materials and mass

Use `Material` to define the substance of a shape. A material provides mass, contact values,
and appearance.

When physics mode is on, each body must have measurable mass. Give every shape a material or
set `MassProperties` on the body.

Use these units:

- Use kilograms for mass.
- Use kilograms per cubic meter for density.
- Use meters for lengths.
- Use kilogram square meters for inertia.

## Use a library material

Pass a material when you add a shape:

```python
from articraft.sdk import Material, RigidBodyAssembly

model = RigidBodyAssembly("kettle")
base = model.rigid_body("base")
base.add(shell, name="shell", material=Material.STEEL)
```

The library provides these values:

| Material | Density | Static and dynamic friction | Restitution |
| --- | --- | --- | --- |
| `Material.STEEL` | 7850 | 0.42 and 0.36 | 0.55 |
| `Material.ALUMINUM` | 2700 | 0.45 and 0.38 | 0.40 |
| `Material.ABS_PLASTIC` | 1050 | 0.40 and 0.32 | 0.45 |
| `Material.GLASS` | 2500 | 0.40 and 0.35 | 0.60 |
| `Material.HARDWOOD` | 700 | 0.50 and 0.40 | 0.35 |
| `Material.RUBBER` | 1200 | 0.95 and 0.85 | 0.75 |
| `Material.PAINTED_STEEL` | 7850 | 0.40 and 0.34 | 0.50 |
| `Material.CERAMIC` | 2400 | 0.45 and 0.40 | 0.30 |
| `Material.WOVEN_FABRIC` | 400 | 0.60 and 0.50 | 0.15 |

## Choose how a surface looks

Appearance is not decoration. A cabinet with oak density and flat gray surfaces is not a
wooden cabinet. Four fields describe a surface, and each has a plain physical meaning:

- `base_color` is `(r, g, b)` or `(r, g, b, a)`. Alpha below one makes the surface
  transparent. Pick it the way a color picker shows it. Export converts it for the
  renderer, so a mid brown stays a mid brown rather than turning pale.
- `metallic` is 1.0 for bare metal and 0.0 for everything else. There is no middle: paint on
  steel is paint, so it is 0.0.
- `roughness` is 0.0 for a mirror and 1.0 for chalk.
- `clearcoat` adds a clear layer over the surface, with `clearcoat_roughness` for how sharp
  its reflection is. This is what makes enamel, lacquer, and glaze read wet rather than
  chalky.

Use these values as starting points:

| Surface | metallic | roughness | Other |
| --- | --- | --- | --- |
| Mirror or chrome | 1.0 | 0.0 to 0.05 | near-white `base_color` |
| Polished metal | 1.0 | 0.1 to 0.2 | |
| Brushed or satin metal | 1.0 | 0.25 to 0.4 | |
| Cast or bead-blasted metal | 1.0 | 0.5 to 0.7 | |
| Appliance enamel, painted trim | 0.0 | 0.25 to 0.35 | `clearcoat=0.7` |
| Glazed ceramic, porcelain | 0.0 | 0.1 to 0.2 | `clearcoat=0.8` |
| Gloss plastic | 0.0 | 0.2 to 0.35 | |
| Matte plastic, ABS | 0.0 | 0.4 to 0.6 | |
| Oiled or satin wood | 0.0 | 0.4 to 0.6 | |
| Bare or sawn wood | 0.0 | 0.7 to 0.85 | |
| Glass | 0.0 | 0.0 to 0.06 | alpha 0.1 to 0.3, `ior=1.45` |
| Rubber, seals, feet | 0.0 | 0.85 to 0.95 | |
| Fabric, webbing, woven baskets | 0.0 | 0.9 to 1.0 | |

A transparent surface needs an `ior` as well as an alpha. Without one it renders as a flat
film rather than glass. Set `emissive` only for a part that emits light itself.

Vary the surfaces of one object. A body, its trim, its handles, and its controls are rarely
the same substance. When they are, they are rarely the same tint. An object whose every
surface reflects identically reads as one lump of plastic.

## Create a material variant

Use `Material.but(...)` to copy a material and replace selected values. The result keeps the
texture of its source material unless you name another one.

```python
BRUSHED = Material.STEEL.but(roughness=0.75)
GRIPPY = Material.RUBBER.but(name="tacky", friction=(1.1, 0.95))
OAK = Material.HARDWOOD.but(name="light_oak", color=(0.66, 0.48, 0.28), roughness=0.55)
ENAMEL = Material.PAINTED_STEEL.but(name="oven_enamel", color=(0.08, 0.08, 0.09))
```

The `color` you pass is the color the surface ends up, textured or not. With a texture, the
map supplies the grain and your color moves its average. An oak tint therefore stays oak
instead of darkening the map it multiplies.

Measure that color rather than guessing it. When the run has a reference photograph,
`sample_color` reads the median of a region and hands back values ready for `color`.

`pattern_strength` is how much of the texture's own variation survives the move. One keeps
the asset as it was shot. Lower values settle it toward flat color, for a surface that reads
as painted or veneered rather than figured.

## Give a surface a texture

A `texture` names an ambientCG asset, and `texture_scale` is how many meters one tile of it
covers. The scale matters. Without it one tile stretches over the whole shape, so a cabinet
door and a hinge pin each wear one entire copy of the grain.

```python
OAK_PANEL = Material.HARDWOOD.but(name="oak_panel", texture="Wood049", texture_scale=0.8)
```

`texture_rotation` in radians turns the tile on the surface. Grain runs along a board, so
parts cut across each other need the quarter turn between them. A door frame is the usual
case. Its stiles stand up and its rails lie across. One material on both makes the rails
read as the wrong piece of wood.

```python
STILE = OAK_PANEL.but(name="oak_stile")
RAIL = OAK_PANEL.but(name="oak_rail", texture_rotation=math.pi / 2)
```

Rotation and scale are the only placement controls. A texture cannot be offset, and two
shapes sharing one material wear the same part of the tile.

Texture maps are fetched and cached on first use, and only when a run enables them with
`articraft generate --textures`. A surface whose asset cannot be fetched keeps its authored
color and roughness, and the compile reports which one failed.

Find the asset with the `find_texture` tool rather than guessing a slug. A guessed name is
usually wrong, and the surface then falls back to its authored numbers. Search returns each
hit's tags and its preview image, and both matter. Tags like `fine`, `smooth`, `light`,
`plain`, and `rough` separate surfaces the slug does not. The preview settles the rest: it
is the only way to see how coarse a grain is, and which way it runs.

Search by how a surface looks, starting with the material class: `wood fine light`, not
`white oak`. The tags carry appearance, not species or alloy, and every term has to match.
`oak` finds two assets and one of them is tree bark.

That direction is worth checking before you commit. An asset whose grain runs across its
tile needs `texture_rotation` on every part that uses it. Picking one that already runs the
right way is simpler.

Create a new `Material` only when the library has no suitable substance:

```python
BRASS = Material(
    name="brass",
    density=8500.0,
    friction=(0.40, 0.34),
    base_color=(0.72, 0.58, 0.28),
    metallic=1.0,
    roughness=0.3,
)
```

`density` is required. `friction` is optional. If you omit friction, export does not add
contact values.

Use a library material when possible. The manifest records the library source.

## Use multiple materials in one body

Material belongs to a shape, so one body can contain several materials:

```python
box = model.rigid_body("toolbox")
box.add(shell, name="shell", material=Material.STEEL)
box.add(grip, name="grip", material=Material.HARDWOOD)
box.add(pad, name="foot", material=Material.RUBBER)
```

The SDK measures each shape with its material. It combines the results for body mass, center of
mass, and inertia.

## Understand mass measurement

The SDK measures volume, center of mass, and the complete inertia tensor from meshes. Each
shape must be a closed solid.

The SDK unites shapes with the same material before measurement. Shared volume from intentional
overlap is counted once.

A hollow shell has the mass of its walls. A shape that is not closed cannot have measured
mass, so compilation fails.

The SDK repairs inverted face winding before measurement.

If you set `center_of_mass`, the SDK shifts a measured inertia tensor to that point. It uses
the parallel axis theorem.

If you set `diagonal_inertia`, the SDK uses the tensor without changes.

## Override mass values

Use `MassProperties` when geometry cannot provide the correct physical values. Each field is
optional, and the SDK measures omitted fields.

```python
model.rigid_body(
    "stone",
    mass_properties=MassProperties(density=2600.0),
)
model.rigid_body(
    "motor",
    mass_properties=MassProperties(
        mass=0.85,
        center_of_mass=(0.0, 0.0, 0.04),
    ),
)
```

An explicit body `mass` or `density` replaces material based mass measurement. Shape materials
still control contact values and appearance.

## Use physics mode

Enable physics requirements with `articraft generate --physics ...`. Compilation fails if any
body does not have measurable mass.

The error names each body that the SDK cannot measure. Add shape materials or explicit mass
properties to fix it.

When physics mode is off, the exporter can write a body without mass.

## Model hollow objects

Mass comes from the authored geometry. A solid block has the mass of a solid block.

Create walls and cavities for objects that are hollow. Dense materials make an incorrect solid
model much heavier than the intended object.

```python
shell = boolean_difference(outer, inner)
```

Use measured dimensions when you have them. The following values can help with an initial
model:

| Object | Typical wall thickness |
| --- | --- |
| Sheet metal panel or appliance body | 0.5 to 1.5 mm |
| Cast metal housing or cookware | 2 to 4 mm |
| Injection molded plastic shell | 1.5 to 3 mm |
| Wooden board or panel | 10 to 20 mm |
| Glass bottle or window | 2 to 5 mm |

Compare the final mass with a known object of the same type. If the mass is too large, check
for geometry that must be hollow.

## Export materials and mass

The exporter applies `UsdPhysics.MassAPI` to each body with measurable mass. It writes these
attributes:

- `physics:mass`.
- `physics:centerOfMass`.
- `physics:diagonalInertia`.
- `physics:principalAxes`.

Each surface binds a `UsdPreviewSurface` carrying its color, metalness, roughness, opacity,
clear coat, index of refraction, and emission. With textures enabled it also binds the
asset's color, roughness, normal, ambient occlusion, and metalness maps.

The exporter applies `UsdPhysics.MaterialAPI` to each collider with contact values. A coating
replaces the surface material, but it does not replace core density.

Friction belongs to each shape. For example, rubber feet can provide friction for a steel
frame.

Read `docs/sdk/common/50_usdz_export.md` for the complete USD structure.
