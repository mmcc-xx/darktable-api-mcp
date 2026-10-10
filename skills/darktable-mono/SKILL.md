---
name: darktable-mono
description: Convert a photo to black and white in darktable through the darktable-api MCP tools, scene-referred, with color calibration's gray mix (luminance or film presets), then contrast, dodge and burn, and optional toning. Use when the user asks for black and white, B&W, monochrome, greyscale, a film look like HP5 or Acros, or split toning.
---

# Black and white in darktable

Sources: darktable.info "Monochrome from color images"
(https://darktable.info/en/darkroom/in-depth/monochrome-from-color-images/),
the darktable manual's monochrome guide
(https://docs.darktable.org/usermanual/development/en/guides-tutorials/monochrome/),
and the presets in darktable's color calibration source. Our own wording.

For exposure, checking and saving, follow the darktable-edit skill; this
one covers what changes for black and white. A photo named by number
("photo 6") is its library id: `open_photo(6)` directly (`list_images`
hides rejected photos by default).

## The idea

Mix the color channels into grey **in linear light, before the tone
mapper**, like film does: each color's contribution to the grey decides how
bright red lips, green leaves or blue sky end up. Then shape the tones with
the tone mapper and dodge and burn, exactly as for a color photo.

Methods, best first:

1. **Color calibration, gray tab** (`channelmixerrgb`): the default.
2. **A second color calibration after the tone mapper**: creative,
   display-referred; for a punchier or graphic look.
3. Color balance rgb with chroma/saturation at −100%: works, slightly
   tinted, less control.
4. Color equalizer with all saturation nodes down, brightness nodes to
   shape hues: flexible but fiddly.
5. **Never the monochrome module** (display-referred, artifacts).

## Before converting

- Do steps 1–2 of darktable-edit first: exposure on the subject, white
  balance in color calibration. The grey mix sees the white-balanced
  colors, so a color cast changes the grey tones.
- **Make a version if the user may want the color one too**:
  `duplicate_photo()` and convert the duplicate. Ask if it isn't clear;
  don't destroy a finished color edit.
- If the photo is already monochrome (camera B&W mode, a flagged mono
  image, `image_info`), skip the conversion.

## Converting (method 1)

Keep the existing color calibration instance: it does the white balance.
darktable's monochrome presets switch adaptation off, so applying one to
that instance would throw the white balance away.

1. `add_module_instance("channelmixerrgb")` (copy=False: a fresh
   instance), note the instance it returns.
2. `list_presets("channelmixerrgb", instance=<new>)` and
   `apply_preset("channelmixerrgb", "<name>", instance=<new>)`:
   - "monochrome | luminance-based": neutral, like the eye's brightness.
     The default choice.
   - "monochrome | ILFORD HP5+", "DELTA 100", "DELTA 400 - 3200", "FP4+":
     film responses, more weight on blue: blue sky and blue shadows come
     out lighter, reds and skin a little darker. "Fuji Acros 100": an
     almost even mix.
3. `get_module("channelmixerrgb", instance=<new>)`: the preset sets
   `illuminant` to "same as pipeline (D50)" (no second white balance); look at
   `grey`. The first instance should still have its white balance.
4. `render_preview`.

## Shaping the mix

`grey` is [R, G, B, normalization] inputs; `normalize_grey` (normalize
channels) keeps overall brightness steady while you move them. Change one
element: `set_module("channelmixerrgb", {"grey[2]": 0.3}, instance=<new>)`.

- Raising an input brightens things of that color in the grey; lowering it
  darkens them. Approximate rules from the sources:
  - more **R**: smoother skin, lighter lips and blemishes, darker blue sky
    (like a red filter on film);
  - more **G**: more detail and texture, lighter foliage;
  - less **B**: less texture, darker sky (stronger clouds), less haze.
- The sum of the three must not be zero; keep each input within about
  −0.5 to 1.5 or noise and odd edges appear.
- Work from what's in the color photo: `render_preview` of the color
  version (`history_step` before the conversion) tells you which colors
  separate subject and background. **Two different colors with the same
  brightness turn into one grey**: pick the input that pulls them apart.
- Check with numbers: `measure_photo(boxes=[subject, background])`; their
  luminance should differ clearly. The rgb values should be equal (a
  neutral grey); if not, something downstream adds color.
- Move in steps of 0.1–0.2 and look after each.

## Color filters (as on black and white film)

Photographers shooting black and white film put a colored filter on the
lens: it passes its own color and holds back the opposite one, so things of
its color come out lighter and the rest darker. The same mix can be set as
the grey mix. Use these when the user asks for a filter by name, or when
the photo needs what a filter does (darker sky, less haze, lighter
foliage, smoother skin).

Set on the conversion instance, with normalize channels on (only the
ratios matter, so the red row is at half scale to fit the −2..2 range):
`set_module("channelmixerrgb", {"grey": [R, G, B, 0], "normalize_grey": true}, instance=<new>)`

| Filter | grey [R, G, B] | What it does | Use for |
|---|---|---|---|
| none (panchromatic film) | [0.38, 0.28, 0.34] | film's own response: blue a bit lighter than the eye sees it | a neutral film look; base for comparing |
| yellow (Wratten 8) | [0.53, 0.44, 0.03] | sky slightly darker, clouds show; close to the eye | the everyday filter, landscapes, street |
| deep yellow (Wratten 15) | [0.88, 0.26, −0.13] | sky clearly darker, cuts some haze | landscapes with clouds |
| orange (Wratten 21) | [1.51, −0.30, −0.20] | dark sky, strong clouds, haze cut, skin smoother and lighter | architecture against sky, distant views, portraits with blemishes |
| red (Wratten 25) | [1.24, −0.64, −0.10] | darkest sky, dramatic clouds, foliage darker, red and skin pale, shadows (lit by blue sky) darker | drama, graphic skies; not for portraits unless pale skin is wanted |
| yellow-green (Wratten 11) | [−0.02, 0.89, 0.13] | foliage lighter and separated, natural skin | landscapes with greenery, outdoor portraits |
| green (Wratten 58) | [−0.60, 1.49, 0.11] | foliage much lighter, reds and lips darker, skin darker and more textured | forests, gardens; men's portraits with character |
| blue (Wratten 47) | [−0.02, 0.04, 0.98] | sky light, haze and fog stronger, reds and skin dark | mood, mist, atmosphere |

How strong it looks depends on the photo: a deep blue sky darkens far more
than a pale or hazy one. On a pale summer sky (b* around −28), the sky
went from L 58 (no filter) to 54 yellow, 51 deep yellow, 48 orange, 45 red
and 65 blue. For more drama than the red filter gives, add contrast after
the conversion (AgX contrast, tone equalizer on the sky) rather than
pushing the mix further; extreme mixes bring noise and edge artifacts.

Neutral greys keep their brightness to about 0.1 EV (blue: 0.3 EV darker;
correct with exposure if it matters), as if the filter factor had been
compensated when shooting.

These mixes were computed, not measured from real filters: modelled
filter transmissions (smooth cut-on edges at about 485, 520, 555 and
600 nm for yellow, deep yellow, orange and red; pass bands for green and
blue) on a generic panchromatic film, fitted to darktable's XYZ grey mix
over the 24 ColorChecker patches under D50 (`filters.py` beside this
file). Deep red (Wratten 29) is left out: a three-input mix can't
reproduce it. Check the result with `measure_photo` as always.

## Tones after the conversion (always do this)

Black and white lives on contrast and local light. A fresh conversion of a
color edit almost always looks flat, because the color contrast that
separated things is gone. Don't stop at the conversion:

1. `measure_photo` (the histogram and a few boxes): does the photo use the
   range from deep shadows to bright highlights? Is the subject clearly
   lighter or darker than its surroundings?
2. Raise global contrast until it does, in the tone mapper in use: AgX
   `curve_contrast_around_pivot` (and toe / shoulder power), or filmic rgb
   `contrast`. Small steps; check that highlights don't clip and shadows
   keep some detail.
3. Then dodge and burn where the subject needs it (below).

If the user asked for a soft or low-contrast look, keep it soft and say so.

- **Global contrast**: see step 2. Mono usually wants a bit more
  contrast than color.
- **Dodge and burn**: tone equalizer (presets "compress
  shadows/highlights | ..." for flat light or high dynamic range), or an
  exposure instance with a drawn mask (`add_module_instance("exposure")`,
  `add_mask`, small exposure changes, big feathering).
- **Darker edges**: exposure instance with an inverted ellipse mask.
- **Grain** (`grain`) only if the user wants a film look; it goes after
  the tone mapper by default.

## Toning (optional)

Only if asked (sepia, selenium, split toning). Use color balance rgb, which
works after the grey conversion in the pipe:

- Sepia / warm: `highlights_H` and `midtones_H` around 40–60° (orange),
  chroma 0.03–0.08.
- Split tone: warm highlights, cool shadows (`shadows_H` around 200–230°),
  low chroma on both.
- Check `measure_photo`: the tint should be visible but faint.

## Creative variant (method 2)

For a graphic, high-contrast look: put the grey conversion after the tone
mapper. `add_module_instance("channelmixerrgb")`, apply a monochrome
preset to it, then `move_module("channelmixerrgb", instance=<new>,
after="agx")` (or the tone mapper in use). The mix now works on display
values, so channel changes act harder; color tools before the tone
mapper (color equalizer, color balance rgb) now shape which hues become
light or dark.

## Finishing

Before saving, check the sky and plain areas for sensor dust at
`render_preview(zoom=0.5)` (darktable-edit, "Before saving"): in black and
white, dust spots stand out more.

Describe what changed from your measurements (e.g. "sky from L 53 to 50,
darker"), and make the words match the numbers.

Before saving, show the user the B&W next to the color original
(`render_preview(history_step=<step before the conversion>)`) and say
which mix you used and why (e.g. "HP5+ for the lighter sky, R raised to
smooth the skin").

For the same look on several photos, see darktable-batch (a style made
from the conversion instance).
