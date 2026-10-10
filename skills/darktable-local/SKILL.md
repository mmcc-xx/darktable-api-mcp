---
name: darktable-local
description: Make local edits in darktable through the darktable-api MCP tools - masks (drawn shapes, gradients, AI object masks, parametric ranges, reused raster masks), dodge and burn, darkening a sky, brightening a face, local color, vignettes - and check that the mask covers the right area. Use when the user wants a change in only part of a photo (the sky, the subject, a face, the background, the edges), or mentions masks, gradients, dodging, burning, or selective edits.
---

# Local edits in darktable

Sources: darktable's user manual (masking and blending, drawn, parametric
and raster masks, tone equalizer), darktable.info's masking articles, and
the tools here. Our own wording.

For finding the photo, the global edit, checking and saving, follow
darktable-edit. Do the global edit first: a local edit corrects what's left
after it, and parametric masks depend on what comes before them.

## First: is a mask needed at all?

- **Brightness by zone** (sky too bright, shadows too dark, backlit
  subject): the **tone equalizer** often does it with no mask, and its
  transitions are cleaner (darktable-edit step 4). Try it first.
- **One color** (only the greens, only the blue sky): **color equalizer**
  (`colorequal`) or color zones change one hue without a mask.
- Otherwise: a module instance with a mask.

## The pattern

1. **A new instance for the local change**, so the global settings stay:
   `add_module_instance("exposure")` (dodge, burn, vignette),
   `add_module_instance("colorbalancergb")` (local saturation, grading),
   `add_module_instance("channelmixerrgb")` (a second light source's white
   balance). Note the instance it returns. Rename it for the user:
   `rename_module_instance(op, instance, "sky")`.
2. **The mask** (below) on that instance.
3. **The change** on that instance: `set_module(op, values, instance=n)`.
   Start strong enough to see where it acts, check, then dial it to the
   final value.
4. **Check the mask** (below), then **soften the edges**.
5. One local change per instance. Several areas with the same change can
   share one instance (shapes combine with `union`).

## Choosing the mask

| Area | Mask | How |
|---|---|---|
| Sky fading into land, no hard horizon | gradient | `add_mask(op, "gradient", x, y, rotation, compression)` |
| Sky above a straight horizon, or above water | path + blue hue | a path around the sky down to the horizon (below), then `masks: "drawn & parametric"` with a sky hue range (below) |
| Sky above an irregular skyline (trees, buildings) | path or gradient + hue or brightness | the drawn part for the sky's area, the parametric part to leave out what rises into it |
| A person, an animal, a car, a building | AI object mask | `add_ai_mask(op, include=[[x, y]])`, add `exclude` points to separate a neighbour |
| Part of a landscape (a mountain, a hill, a field) | path | the AI mask works on distinct objects; on parts of a landscape it spills into neighbours or leaves holes. Draw a path instead |
| A face, a spot of light, a round area | ellipse or circle | `add_mask(op, "ellipse", x, y, radius, radius_y, rotation, feather)` |
| An irregular area you can outline | path | `add_mask(op, "path", points=[[x, y], ...], feather)` |
| A stroke along something (a road, a branch) | brush | `add_mask(op, "brush", points, width, hardness)` |
| Shadows, highlights, a color | parametric | `set_blending(op, {"parametric": {...}})` |
| The same area as another module's mask | raster | `set_blending(op, {"raster_source": {"operation", "instance"}})` |
| Edges and corners (vignette) | ellipse, inverted | `add_mask(op, "ellipse", 0.5, 0.5, radius, radius_y, feather=0.3, inverted=True)` |

Positions are fractions of the photo as `render_preview` shows it; radius
and feather are fractions of the shorter side. Pick them from a
`render_preview` (with `measure_photo` to confirm where something is).

**Gradient**: x, y is the **middle of the fade**, not where it ends: the
fade reaches both sides of it, so whatever is just below the line still
gets part of the change. Put the line above the boundary you want to keep
clean, by about half the fade. `compression` near 0 is a hard line, near 1
a long fade; skies usually want 0.2–0.5. Check which side it acts on with a
strong test value; if it's the wrong one, use `rotation` + 180 (or
`inverted`). Where something must not be touched right at the line (water
under a sea horizon), use a path instead.

**Path**: points outside the photo snap to its edge; put them on the edges
(0 or 1) to reach the frame. darktable smooths the outline through the
points, so corners round off and a straight edge bulges: add points close
to each corner (e.g. 0.99 and 1.0) and every ~quarter along a long straight
edge to keep the outline tight. Draw it just inside the outline you want
and soften with `blur_radius`, rather than outside it (a soft edge outside
glows onto the neighbour).

**AI object mask**: for distinct objects (people, animals, vehicles,
buildings, a boat on water). One include point in the middle of the object
is often enough. Check the outline; if it took too much, add an `exclude` point on
the wrong part; if too little, another include point. The first call on a
photo takes a few seconds. Needs darktable's AI enabled; if it fails, fall
back to a path.

**Parametric ranges** (`set_blending`): four values [low end, low full,
high full, high end] per channel, in the darkroom's units (percent for
brightness `g`). Examples on a scene-referred module:
- shadows only: `{"g_in": {"range": [0, 0, 10, 30]}}`
- highlights only: `{"g_in": {"range": [40, 70, 100, 100]}}`
- `"inverted": true` flips a range.
- blue sky by hue: `{"hz_in": {"range": [205, 225, 300, 320]}}` (hue in
  degrees; this kept a clear blue sky and left out green trees; widen the
  low end toward 195 for a pale band near the horizon, but shaded foliage
  turns bluish and gets caught)
`get_blending(op, instance)` lists the channels this module offers
(brightness `g`, R/G/B, and Jz/Cz/hz for lightness, chroma and hue). Wide
soft ramps (far apart low end / low full) avoid blotchy edges. A
parametric mask on a busy edge (tree tops against sky) gives speckles;
a small guided feathering (below) smooths them.

**Combining**: shapes on one module join with `combine` (union,
intersection, difference, exclusion). Drawn plus parametric:
`set_blending(op, {"masks": "drawn & parametric", "combine": "exclusive"})`
keeps only where both apply.

## Checking the mask

There's no mask overlay through the tools, so make it visible:

1. **Exaggerate**: set the instance to an obvious value (exposure +3 EV,
   or saturation −1 for black and white where the mask is) and
   `render_preview`. The changed area is the mask. Look for spill onto the
   subject, missed corners, halos along edges.
2. **Zoom on the edges**: `render_preview(zoom=1, center_x, center_y)` on
   the boundary (skyline, hair, the subject's outline).
3. **Measure**: `measure_photo(boxes=[inside, outside])` with the
   exaggerated value: inside changes a lot, outside not at all.
4. Fix the shape (`list_masks`, `remove_mask`, add again with new numbers),
   then set the real value.

## Softening edges

Hard mask edges are the most common local-edit artifact.

- Drawn shapes: `feather` (0.05–0.3 of the shorter side; more for a
  vignette or a soft dodge).
- `set_blending(op, {"feathering_radius": ..., "feathering_guide": ...}, instance=n)`:
  feathering guided by the photo's edges (guide: "input before blur",
  "input after blur", "output before blur", "output after blur"). Small
  radii (2–5) clean up speckles along a parametric mask's edge; large ones
  (10–20) can spread the change past the edge as a glow or halo. Check at
  `zoom=1` and turn it off (0) if it makes things worse.
- `blur_radius` softens the whole mask evenly; use for dodge and burn,
  not where an edge must stay crisp.
- `opacity` (0–100) weakens the whole local change: the easiest way to
  dial it in.

## Recipes

- **Darken a bright sky**: exposure instance; a path over the sky to the
  horizon (or a gradient if sky fades into land), with a blue hue range so
  trees and buildings rising into it stay out; −0.5 to −1.5 EV; guided
  feathering radius 2–5 if tree tops speckle. Check the skyline at 100%.
  Measure the water, trees and ground before and after: they shouldn't
  change.
- **Brighten a face or subject** (dodge): exposure instance, AI object mask
  or a feathered ellipse, +0.3 to +0.8 EV.
- **Darken distractions** (burn): exposure instance, brush or ellipse,
  −0.3 to −1 EV, large feather.
- **Vignette**: exposure instance, inverted ellipse with big feather
  (0.3+), −0.3 to −0.8 EV. More control than the vignette module.
- **Mixed light** (window daylight and warm lamps): a second color
  calibration instance masked to the lamp-lit area, white balanced from a
  neutral surface there.
- **Local color**: color balance rgb instance, mask, `vibrance` or
  `saturation_global` there (more on the subject, less on a busy
  background).
- **Noise only in the shadows**: see darktable-cleanup (a parametric
  shadows range on denoise).

## Reporting

Say which area each local change covers and how you made the mask, the
value, and what you checked ("sky −0.8 EV through a gradient on the
horizon, limited to the bright parts so the trees stay; checked the
skyline at 100%"). Each mask and change is a history step the user can
undo.
