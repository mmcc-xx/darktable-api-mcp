---
name: darktable-batch
description: Give several photos in darktable one consistent look through the darktable-api MCP tools - copying an edit or part of it from one photo to others (paste_edit), making and applying styles, then reviewing every photo and fixing the ones that need their own adjustment. Use when the user asks to apply an edit, look, style or black and white conversion to several photos, a film roll, a series or a selection, to "make these match", or to create or use a darktable style.
---

# One look across several photos

Sources: darktable's user manual (styles, copy and paste history,
selective paste) and darktable.info's notes on styles. Our own wording.

For editing the reference photo, follow darktable-edit (and darktable-mono
for black and white). This skill is about carrying an edit to other photos
and checking each one afterwards.

## Batch changes save at once

`paste_edit` and `apply_style` **save every photo they change**; there is
no unsaved stage to look at first. So:

- **Confirm the set of photos** with the user before applying: how many,
  which (ids and file names), and what will be copied.
- **Try one photo first**, show it, then do the rest.
- **Never use `mode: "overwrite"` without asking**: it replaces each
  photo's whole edit, and their own work on those photos is gone. Default to
  "append". If the user wants a fresh start on the copies, `duplicate_photo`
  them first.
- Undo, per photo: `set_history_end` to the step before the paste (each
  result lists where it started).
- A photo with unsaved changes is refused unless `save_first`; ask before
  saving someone's unsaved work.

## 1. The photos

Work out exactly which photos the user means:

- "this roll", "the photos from Sunday": `list_film_rolls`, then
  `list_images(film_roll_id=..., rating="all")` (the default hides
  rejected photos; ask whether to include them).
- "the ones I selected", "what I'm looking at": `get_collection` (darktable's
  current collection and selection).
- "the 4-star ones", "the red-labelled ones": `list_images(rating="4")`,
  `label="red"`.
- Numbers ("photos 30 to 36"): library ids.

Look at them before deciding anything: `get_thumbnail(id, size=256)` for
each (or a few, for a big set). Note the outliers: different light,
different subject, already heavily edited (`history_end`), a different
camera (`image_info`).

## 2. The reference and what to copy

Pick the reference: the photo the user named, or the best edited one;
finish its edit and save it first.

Then decide what is **shared** (the look) and what is **per photo**:

| Copy (the look) | Leave out (per photo) |
|---|---|
| tone mapper (`agx`, `filmicrgb`, `sigmoid`) look settings | `exposure` (each photo's brightness differs) |
| `colorbalancergb` (saturation, grading) | white balance: `temperature`, and the color calibration instance that does it |
| a B&W conversion instance (`channelmixerrgb` instance 1) | `crop`, `flip`, `ashift` (rotate and perspective), `clipping` |
| `colorequal`, curves (`rgbcurve`, `tonecurve`), `rgblevels` | `retouch` (spot positions belong to one frame) |
| `grain`, `vignette`, `bilat` (local contrast) | modules with drawn masks: shapes sit on one frame's content |
| `denoiseprofile` (same camera and ISO range), `demosaic` capture sharpening | `lens`, `rawprepare`, `highlights`, `demosaic` method (darktable sets these per photo) |

Exceptions: shoots in identical light (a studio, a fixed tripod series)
can share exposure and white balance; the user may want everything. Ask
when unsure.

Name the modules: `list_modules` on the reference for operations and
instances; use `{"operation": "channelmixerrgb", "instance": 1}` to copy
one instance.

## 3. Paste or style?

- **`paste_edit(from_image_id, image_ids, modules=[...])`**: one-off, this
  set of photos now. Copies the reference's saved settings of those
  modules.
- **A style** (`create_style(name, image_id, modules=[...], description)`,
  then `apply_style(name, image_ids)`): when the look should be reusable
  (other rolls, later, by the user in darktable's own styles menu). Name it
  plainly ("B&W orange filter, punchy"), describe it. `list_styles` first:
  the user may already have one that fits.

**Tone mappers**: a photo never edited before already has darktable's
default tone mapper (usually `agx`). Pasting a different one (`filmicrgb`
from the reference) leaves it with two: turn the default off
(`enable_module("agx", False)`) on those photos. A target whose own edit
uses another tone mapper (sigmoid on a finished edit): don't paste the
reference's tone mapper onto it; copy only the other modules and match
contrast in its own tone mapper.

Both append by default: a copied module replaces the target's matching
instance or is added as a new one. Check `list_modules` on the first
target: an unexpected second instance (two exposure modules, two tone
mappers) means the copy didn't line up; undo that photo and copy fewer
modules or different instances.

## 4. Apply, one first

1. Apply to one typical photo of the set.
2. `open_photo` it, `render_preview`, compare with the reference's render.
   Check `list_modules`: one tone mapper, the right instances.
3. Show the user, or if they asked you to just do it, carry on when it
   looks right.
4. Apply to the rest in one call.

## 5. Review every photo and fix the outliers

A shared look rarely fits every frame. For each photo:

- `get_thumbnail(id, size=384)` (or `open_photo` + `render_preview` for a
  closer look).
- `measure_photo` on the subject and on a key area (sky, skin): compare
  with the reference. Brightness off by more than about 0.3 EV
  (L differs by more than ~5): adjust that photo's `exposure`.
- White balance cast where the reference had none: fix that photo's color
  calibration.
- Highlights or shadows clipping (the measured clipped fractions): a
  pasted tone mapper can clip a brighter photo's sky; adjust that photo
  (exposure, or the tone mapper's highlight settings).
- Save each photo you fix.

Report the set as a short list: which photos got the look, which needed
their own fix and what, which you left out and why.

## Black and white series

Read the reference's grey mix (`get_module("channelmixerrgb", instance=1)`)
and name it from darktable-mono's filter table (e.g. [1.51, −0.30, −0.20]
is the orange filter, not red). Copy only the conversion instance
(`channelmixerrgb` instance 1 from darktable-mono) and, if the reference's contrast was raised for B&W, the
tone mapper; not the white balance instance. A target that is already
black and white has its own conversion: ask, or replace only its mix on a
duplicate. If the user wants to keep the
color versions, `duplicate_photo` each first and paste onto the duplicates.
Contrast often needs a per-photo touch afterwards (darktable-mono, "Tones
after the conversion").
