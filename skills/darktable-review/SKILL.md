---
name: darktable-review
description: Review a photo and its darktable edit through the darktable-api MCP tools without changing anything - technical quality (exposure, clipping, white balance, noise, sharpness, dust), tones, color, composition and the edit itself - and give ranked, concrete suggestions with the tool calls and values that would make them. Use when the user asks for feedback, a critique, a second opinion, "what would you change", "is this edit any good", "what's wrong with this photo", or wants suggestions before editing.
---

# Reviewing a photo and its edit

A review **changes nothing**. No `set_module`, `enable_module`,
`apply_preset`, masks, retouch, `save`, or anything else that edits or
saves. The user may have unsaved work in this photo (in darktable's window
or the web app); a review must leave it exactly as it was. Only offer to
apply suggestions afterwards; when asked, follow darktable-edit (and the
other skills) to do it.

Read-only tools: `library_status`, `open_photo` (joins the edit without
changing it), `image_info`, `get_history`, `list_modules`, `get_module`,
`get_blending`, `list_masks`, `list_retouch_spots`, `get_geometry`,
`render_preview` (also with `history_step`, `zoom`, `uncropped`),
`measure_photo`, `check_sensor_clipping`, `find_dust_spots`,
`get_thumbnail`.

## 1. Gather the evidence

1. Find the photo as darktable-edit says (darkroom photo for "this
   photo"; a number is a library id).
2. `image_info`: camera, lens, ISO, aperture, shutter, focal length.
   `get_history` and `list_modules`: is there an edit, what does it use.
3. Look: `render_preview(size=1200)` (as edited) and
   `render_preview(history_step=0)` (the original, for what the edit did).
4. Measure: `measure_photo` with boxes on the subject, the brightest and
   darkest important areas, and something that should be neutral; read
   the histogram and the clipped fractions.
5. At 100%: `render_preview(zoom=1)` on the subject (focus, sharpness,
   noise), a shadow area (noise), and the sky or plain areas (dust).
6. `check_sensor_clipping` if anything is near white; `find_dust_spots`
   if there's sky or plain background at a small aperture.

Base every point on something you saw or measured, and say which.

## 2. What to look at

**Technical**
- Exposure of the subject: Lab L around 50–60 for a midtone subject
  (faces a bit brighter); darker or brighter only if it's the intent
  (low key, high key, silhouette).
- Clipping: highlights at 255 (`measure_photo` clipped fraction) and
  whether the sensor really clipped (`check_sensor_clipping`): rendered
  clipping with sensor headroom is recoverable; sensor clipping isn't.
  Crushed shadows with lost detail.
- White balance: neutral things (grey stone, white paper, clouds) near
  a = b = 0; a cast that isn't the mood of the light.
- Noise for the ISO, at 100%; overdone denoise (waxy skin, smeared
  foliage).
- Sharpness: focus on the subject (eyes for people); motion blur; soft
  overall (capture sharpening off); oversharpening halos.
- Dust, hot pixels, color fringes on high-contrast edges.

**Tones**
- Global contrast: does it use the range from deep shadows to bright
  highlights, or is it flat or harsh?
- Local: dark shadows hiding the subject, a sky that dominates,
  backlight; the tone equalizer's job.
- Does the brightest, most contrasty area hold the subject? The eye goes
  there first.

**Color**
- Saturation: dull, natural, or overdone (neon greens, orange skin,
  posterized blue skies).
- Skin: plausible hue and saturation.
- Harmony: a palette that holds together, or a stray color pulling the
  eye.

**Composition**
- Horizon level (`get_geometry` for current rotation); verticals in
  architecture.
- Crop: room around the subject, edges cutting through things,
  distractions near the edges (bright spots, cut-off objects).
- Subject placement and what the eye does.
- Note crop suggestions as fractions for `crop_photo`, judged on
  `render_preview(uncropped=True)`.

**The edit itself**
- One tone mapper (agx, filmicrgb, sigmoid or basecurve, not two).
- White balance done in color calibration, with white balance on camera
  reference; not both doing it.
- Legacy display-referred modules where a scene-referred one does better
  (monochrome module, shadows and highlights, old sharpen, base curve on a
  new edit).
- Masks that leak (an exaggerated edge, a halo at a skyline at 100%).
- Black and white: the grey mix (`get_module("channelmixerrgb",
  instance=...)`) against darktable-mono's filter table; contrast after
  the conversion.

## 3. The review

Lead with a one-line verdict, then what works (briefly, and specifically),
then the suggestions **ranked by impact**, at most 5–7:

For each suggestion:
- **What**: the change in plain words.
- **Why**: what you saw or measured (numbers).
- **How**: the module and values, as the tool call that would do it
  (`set_module("exposure", {"exposure": +0.3})`, a mask from
  darktable-local, a retouch from darktable-cleanup).
- **Effect**: what will look different.

Separate fixes (something is wrong) from taste (a different look the user
may or may not want). Say what you'd leave alone and why. If the photo is
good as it is, say so; don't invent work.

Then offer: apply all, some (by number), or none. Applying is a separate
step the user chooses, done with the editing skills, as new history steps
they can undo.

## Reviewing several photos

For a set (a roll, a selection), `get_thumbnail` each, rank them, and
review the best few in detail; for the rest, a line each (keep, needs
work and what, reject and why). Ratings and rejects are the user's call:
suggest, don't set.
