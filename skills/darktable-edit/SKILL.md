---
name: darktable-edit
description: Develop a photo in darktable through the darktable-api MCP tools, following darktable's standard scene-referred workflow (exposure, color calibration, AgX, tone equalizer, color balance rgb, then crop, sharpness and noise). Use when the user asks to edit, develop, fix, brighten, warm up, or "make this photo look good" in darktable, or asks for a general edit of a photo rather than one specific setting.
---

# Developing a photo in darktable

The structure follows darktable.info's "standard workflow in 5 + 1 steps"
(https://darktable.info/en/getting-started/quick-start/darktable-first-steps/understand/standard-workflow-2/,
darktable 5.4) and the darktable user manual. What follows is our own
translation of it into tool calls and checks.

## Before you start

1. **Find the photo.** If the user says "this photo" or "the one I have
   open", call `library_status`; if `darkroom_imgid` is set, use
   `open_darkroom_photo`. A number ("photo 36", "id 36") is the library
   id: `open_photo(36)` directly. A file name: search `list_images`
   (`rating="all"`: by default it hides rejected photos) or
   `get_collection` (what darktable's lighttable shows). Ask only if
   these don't settle it.
2. **Know where you are running.**
   - *darktable's window serves the library* (the photo is in its darkroom):
     pickers and auto buttons work (`list_pickers`, `use_picker`), and
     darktable adjusts related settings after `set_module`
     (`darktable_also_changed`). The user sees every change live.
   - *Headless*: no pickers. Measure with `measure_photo` and set values
     with `set_module`. darktable won't fix related settings for you (see
     each step).
3. **Read the starting point.** `list_modules` (what's on), `get_history`
   (has the user edited it already?), `image_info` (camera, lens, ISO,
   exposure), and look at `render_preview`. If the photo already has an
   edit, build on it; don't reset it unless asked (`start_over` needs the
   user's say-so).
4. **Check for real clipping** on anything with bright skies, lamps or
   specular highlights: `check_sensor_clipping`. Clipped on the sensor =
   only the highlights module can reconstruct it; merely bright in the
   render = recoverable with exposure, AgX or tone equalizer.

Always `get_module(op)` before the first `set_module(op, ...)` on a module:
names, ranges and dropdown values come from there, not from memory.

## The five core steps

Do them in this order. The pipeline order is fixed by darktable, but each
step's right value depends on the ones before it (tone mapping sees what
exposure gives it; parametric masks see what's upstream).

### 1. Exposure: the subject's midtones

Goal: the main subject (face, central object) at a sensible mid brightness.
Ignore clipped highlights and black shadows at this point; AgX and tone
equalizer deal with them.

- Window: `use_picker("exposure", "exposure", box=<subject box>)`.
- Headless: `measure_photo(boxes=[<subject box>])` and change
  `exposure` in steps (+1 EV ≈ twice as bright in linear light). A typical
  midtone subject lands around Lab L 50–60 in the render; faces a bit
  brighter, dark subjects (black dog, night) lower. Check `mode` is
  manual ("manual" in `get_module`), or exposure won't apply.
- Leave `black` (black level correction) alone.

### 2. Color calibration: white balance

The **white balance** module (`temperature`) stays on camera reference.
White balance is done in **color calibration** (`channelmixerrgb`), which
handles highlights and mixed light better.

- Window: `use_picker("channelmixerrgb", "picker", box=<neutral area>)`
  on something that should be grey or white (wall, shirt, cloud, paper).
- Headless: `measure_photo(boxes=[<neutral area>])`; a neutral area should
  have Lab a and b near 0. Adjust `temperature` (for a temperature-based
  illuminant) in a few hundred K steps and re-measure. Raising
  `temperature` warms the result (it tells darktable the light was bluer);
  verify the direction by measuring rather than trusting this.
- Don't neutralise light that is part of the mood (sunset, candlelight,
  blue hour) unless the user asks; reduce the cast instead of removing it.
- Mixed light: one color calibration instance per light source, each with
  a mask (`add_module_instance`, `add_mask` / `set_blending`).

### 3. AgX: tone mapping and global contrast

AgX maps the scene's dynamic range to the screen. **Only one tone mapper**:
if the edit already uses filmic rgb or sigmoid, keep it and adjust that
one; never add AgX on top. Don't use base curve for new edits.

- Start from a preset if AgX is fresh: `list_presets("agx")`, e.g.
  "blender-like|base" (neutral) or "blender-like|punchy".
- Window: `use_picker("agx", "exposure range/auto tune levels")` sets
  the white and black relative exposure from the photo.
- Headless: leave `range_white_relative_ev` / `range_black_relative_ev`
  unless highlights clip in the render (raise white) or shadows are crushed
  (lower black).
- Then shape the look:
  - `curve_contrast_around_pivot` (contrast; default 3): main global contrast.
  - `auto_gamma` ("keep the pivot on the diagonal") keeps midtones still
    while contrast changes.
  - `curve_shoulder_power` up = softer highlight roll-off;
    `curve_toe_power` for the shadows.
  - `curve_target_display_black_ratio` 0.01–0.02 for a matte look.
  - `look_saturation`, `look_original_hue_mix_ratio` (preserve hue).
  - Leave `curve_gamma` (2.2) and the primaries settings alone.

### 4. Tone equalizer: dodge and burn by brightness

Brightens or darkens zones of the photo by their luminance, keeping local
contrast. Use it for sky vs. foreground, backlit faces, deep shadows.

- Easiest: a preset (`list_presets("toneequal")`): "compress
  shadows/highlights | EIGF | soft / medium / strong" for high dynamic
  range scenes, "relight: fill-in" for backlit people.
- Manual: the nine bands are EV zones of the mask. Their setting names
  don't match their labels: `noise` = −8 EV ("blacks"),
  `ultra_deep_blacks` −7, `deep_blacks` −6, `blacks` −5, `shadows` −4,
  `midtones` −3, `highlights` −2, `whites` −1, `speculars` 0 EV. Values
  are EV (±2). Move neighbouring bands together, in small steps
  (0.3–1 EV), or you get halos.
- The mask must spread over the bands. Window: run the wands,
  `use_picker("toneequal", "mask exposure compensation")` then
  `"mask contrast compensation"`. Headless: set `exposure_boost` /
  `contrast_boost` by hand if most of the photo falls in one band.
- `blending` (smoothing diameter): higher = softer transitions, fewer
  halos.

### 5. Color balance rgb: saturation and grading

- `vibrance` (global vibrance) is the first choice: it boosts muted colors
  and spares already saturated ones and skin. 0.1–0.3 is usually enough.
- `saturation_global` / `chroma_global` for stronger changes; chroma is
  harsher.
- Presets: "basic colorfulness | natural skin / standard / vibrant colors".
- Window: set the white fulcrum with `use_picker("colorbalancergb",
  "white fulcrum")` before using the 4 ways tab.
- Grading (4 ways: `shadows_*`, `midtones_*`, `highlights_*`, `global_*`
  with _Y luminance, _C chroma, _H hue in degrees): subtle, chroma
  0.02–0.1. Only when the user wants a look.
- `contrast` here acts after tone mapping and is strong; prefer AgX.

## +1: framing, sharpness, lens, noise (as needed)

- **Crop and straighten**: `get_geometry`, `rotate_photo`, `crop_photo`;
  `render_preview(uncropped=True)` to choose. Crop before judging
  exposure and tones if it removes a bright or dark edge.
- **Lens correction** (`lens`): usually auto-applied; check it's on if
  the lens is known. Don't combine its TCA correction with raw chromatic
  aberrations.
- **Capture sharpening**: `set_module("demosaic", {"cs_enabled": true})`;
  `cs_radius` 0.8–1.3 if the default looks soft. Judge at
  `render_preview(zoom=1)`.
- **Detail / clarity**: contrast equalizer (`atrous`) or diffuse or
  sharpen (`diffuse`) presets ("local contrast", "lens deblur").
- **Noise** (high ISO, lifted shadows): denoise (profiled)
  (`denoiseprofile`), strength around camera default, lower (~0.7) if it
  looks plastic; shadows only via
  `set_blending("denoiseprofile", {"parametric": {"g_in": {"range": [0, 0, 10, 30]}}})`.
  Or `ai_denoise` for a cleaner raw (makes a new photo). Denoise before
  sharpening.

## Checking your work

- After each step: `render_preview` (the whole photo) and say in a sentence
  what changed. For noise, sharpness and fine detail: `render_preview(zoom=1,
  center_x, center_y)` on the subject.
- Numbers back up your eyes: `measure_photo` for the subject's brightness,
  neutral areas, and the clipped fractions.
- Before/after: `render_preview(history_step=0)` shows the original.
- If a step made things worse, `set_history_end` back, or set the value
  back; don't stack corrections on top of a mistake.
- Headless, after changing a setting, check settings that darktable's
  window would have adjusted (exposure `mode`, AgX pivot with `auto_gamma`).

## Skipping a step

Not every photo needs all five steps. When you leave one out (white
balance already right, no zones to dodge or burn), say so in one line with
the reason, from what you saw or measured.

## Before saving: dust and defects

Look for sensor dust: dark soft blobs in sky, water, walls and other plain
areas. `render_preview(zoom=0.5)` over the sky's corners and edges shows
them; at the whole-photo size they're easy to miss. Heal what you find with
`retouch_spots` (`tool: "heal"`, source a nearby clean patch of the same
tone) or, for dust that recurs across a film roll, `find_dust_spots` /
`heal_dust_spots`, and check the result zoomed in.

## Finishing

- `save` only when the user is happy, or when they asked for a finished
  edit. Unsaved edits are visible in the web app and darktable's window
  but not in exports.
- Summarise the edit in plain words: what you changed and why, with the
  values, so the user can undo any of it. Describe effects from what you
  measured (darker, lighter, from L 59 to 56), and check the words match
  the numbers.
- Export only when asked (`export_photo`; web: JPEG, sRGB, 2048 px,
  quality 90–95).

## Rules of thumb

- One tone mapper. Exposure before tone mapping. Denoise before sharpening.
  Grain and vignette after the tone mapper.
- Small steps, look after each. A strong edit made of many gentle moves
  beats one big slider.
- For a vignette, an exposure instance with an inverted ellipse mask gives
  more control than the vignette module.
- Black and white: see the darktable-mono skill. Dust and retouching: see
  darktable-cleanup.
