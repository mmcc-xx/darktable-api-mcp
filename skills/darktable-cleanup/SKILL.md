---
name: darktable-cleanup
description: Clean up a photo in darktable through the darktable-api MCP tools - sensor dust, blemishes and distractions (retouch heal, clone, blur, fill), noise (denoise profiled, AI denoise), sharpening and detail, chromatic aberration fringes and hot pixels - judged at 100%. Use when the user mentions dust, spots, specks, blemishes, removing something, noise, grain they don't want, high ISO, soft or unsharp photos, color fringes, or asks to clean up a photo.
---

# Cleaning up a photo in darktable

Sources: darktable's user manual (retouch, denoise (profiled), diffuse or
sharpen, demosaic, chromatic aberrations), darktable.info's "5 + 1" workflow
(sharpness, detail and noise as the optional "+1"), and what the tools here
do. Our own wording.

For finding and opening the photo, the main edit, checking and saving,
follow darktable-edit. This skill covers the defects you see at 100%.

## Look first, at 100%

Defects hide at whole-photo size. Before changing anything:

1. `render_preview(size=1200)` for the whole photo; note plain areas (sky,
   water, walls, skin) and dark areas.
2. `render_preview(zoom=1, center_x, center_y, size=600)` on:
   - the subject (sharpness, noise on it);
   - a dark shadow area (noise shows there first);
   - plain bright areas, especially the sky's corners (dust);
   - high-contrast edges near the frame's corners (color fringes).
3. `image_info`: ISO, aperture (small apertures like f/11–f/22 show dust
   sharply), camera.

Then fix only what you saw, in this order. Say what you saw and what you
left alone.

## 1. Sensor dust

Dust sits at the same place on the sensor in every frame: soft dark round
blobs, most visible on smooth bright areas at small apertures.

1. `find_dust_spots()`: maps the dust that recurs across the photo's film
   roll, then rates each spot in this photo: "obvious" (repair), "visible"
   (seen through some texture), "hidden" (leave it). It returns a crop
   sheet: look at it. The first call for a roll reads every raw in it
   (~0.4 s each).
2. `heal_dust_spots()` heals the obvious ones not already retouched, as one
   history step, and checks each before/after at 100%. Add
   `include_visible=True` when the visible ones show in plain areas, or
   `spots=[ids]` for a choice. `dry_run=True` shows the plan first.
3. Check its before/after sheet and the "healed" per spot. A spot that
   didn't heal: `render_preview(zoom=1)` there, then fix it by hand (below).
4. Dust it can't map (one-off specks, faint blobs that only show in this
   photo, a roll with few frames): heal by hand with `retouch_spots` as
   below.

Spots come in two sizes (`size`): "small" specks (~20–60 px) and "large"
soft blobs (~50–300 px, dust further from the sensor). A large spot in a
cloudy sky can be rated "obvious" where it's hard to see: check it on the
crop sheet before healing.

`find_dust_spots` finds the obvious dust, not every speck: after it, still
look at the sky at `zoom=0.5`.

## 2. Blemishes and distractions (retouch)

`retouch_spots([{x, y, radius, tool, ...}])`, all spots as one history
step. x, y are fractions of the photo as shown; radius is a fraction of the
shorter side (0.005–0.01 for a speck, 0.02–0.04 for a dust blob).

| Tool | For | Notes |
|---|---|---|
| `heal` (default) | dust, skin blemishes, small spots on texture | copies a source patch and blends its brightness and color into the surroundings |
| `clone` | removing things where an exact copy works (a wire across plain sky, a sign) | copies exactly; needs a source with matching texture |
| `blur` | softening without removing (skin texture, a busy patch) | `blur_radius`, `blur_type` gaussian or bilateral |
| `fill` | covering with a flat color | `fill_mode` erase or color; rarely the right choice |

- **Choosing the source** (`source_x`, `source_y`): nearby, same brightness
  and texture, not crossing an edge, not containing another spot. In sky,
  along the same height (sky brightness changes from top to horizon).
- **Size**: the circle about 1.5–2× the blemish; too big copies visible
  texture, too small leaves a dark ring.
- **Check every spot** at `render_preview(zoom=1)`: no ring, no repeated
  texture, no copied edge. Fix with `edit_retouch_spot(formid, ...)` (move
  the source, resize), or `remove_retouch_spots`. `list_retouch_spots`
  shows what's there, with formids.
- Long or large objects (power lines, people): several overlapping
  circles, or ask the user; this is slow work with circles and may look
  worse than leaving it.

## 3. Noise

Judge it at 100% on the subject and in the shadows, after the main edit
(exposure and tone equalizer lift shadows and their noise). Low ISO
(≤ 800 on most cameras) usually needs nothing; don't denoise by habit.

**Denoise (profiled)** (`denoiseprofile`): uses a noise profile measured
for the camera and ISO.

- `get_module("denoiseprofile")`; `enable_module` or set a value to turn it
  on. Default mode "wavelets auto" is a good start.
- `strength` 1.0 is the profile's estimate; lower (0.6–0.8) if the result
  looks plastic or loses fine texture; raise for heavy noise.
- Color speckles only (blotchy colored noise, luminance grain acceptable):
  `apply_preset("denoiseprofile", "wavelets: chroma only")`.
- Noise only in the shadows: keep the highlights crisp with a parametric
  mask: `set_blending("denoiseprofile", {"parametric": {"g_in": {"range":
  [0, 0, 10, 30]}}})`.
- Severe noise: mode "non-local means auto" is smoother but slower.
- A second instance (`add_module_instance`) can do chroma only while the
  first does luminance at a lower strength.

**AI denoise** (`ai_denoise`): darktable's neural denoise on the raw.
Cleaner than profiled denoise on high-ISO photos, but it **writes a new
DNG file next to the original** and imports it as a new photo in the same
group. Ask the user before running it unless they asked for AI denoise.
Then edit the new photo (`open_photo(new_imgid)`), which starts unedited;
it needs no further denoise. `strength` below 1 keeps some of the
original grain.

Check at `render_preview(zoom=1)` against `history_step` before the
denoise: noise gone, detail kept, no waxy skin or smeared foliage.

## 4. Sharpness and detail

After denoise, never before it (sharpening amplifies noise).

- **Capture sharpening** (in demosaic): restores what the sensor and lens
  blur; the first choice. `set_module("demosaic", {"cs_enabled": true})`;
  `cs_radius` 0.5 by default, 0.8–1.3 if still soft; `cs_boost` for soft
  corners. Judge at `zoom=1`; halos around edges mean too much.
- **Lens blur, deeper softness**: diffuse or sharpen presets
  (`list_presets("diffuse")`): "lens deblur | soft / medium / hard",
  "sharpness | fast / normal / strong".
- **Local contrast / clarity** (looks crisper at normal size): diffuse or
  sharpen "local contrast | normal / fine", or contrast equalizer
  (`atrous`). This is a look, not a repair; keep it modest on faces.
- Avoid the old **sharpen** module for new edits; capture sharpening and
  diffuse or sharpen do better.
- Out-of-focus or motion blur can't be repaired; say so instead of
  over-sharpening.

## 5. Color fringes and hot pixels

- **Purple/green fringes** on high-contrast edges, worst in corners:
  - raw chromatic aberrations (`cacorrect`): enable; Bayer sensors only,
    fast, fixes most lateral CA;
  - chromatic aberrations (`cacorrectrgb`): if fringes remain, or non-Bayer
    sensors;
  - don't also enable TCA in lens correction (`lens`): double correction.
- **Hot pixels** (single bright colored dots, long exposures, night):
  hot pixels module (`hotpixels`), `strength` and `threshold`; check at
  `zoom=1` that stars and fine highlights survive.

## Checking and reporting

- Before/after at the same spot: `render_preview(zoom=1, center_x,
  center_y, history_step=<before>)` and the same without `history_step`.
- Report per defect what you saw, what you did (tool, values, how many
  spots), and the checked result; mention defects you chose to leave and
  why.
- Each change is its own history step: the user can undo one with
  `set_history_end`.
- Save as darktable-edit says (when the user asked, or is happy).
