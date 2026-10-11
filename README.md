# darktable-api-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant
(Claude, or any MCP client) browse a [darktable](https://www.darktable.org/)
library and edit photos. All the work is done by **darktable-api**, a
long-running headless darktable built from darktable's own code: it renders
previews, applies module settings and writes the history to the library
exactly as darktable does. No darktable window, Lua script or XMP editing is
involved.

**Experimental.** Use it on a copy of your library (the copy script below
does that). Saving writes darktable's history in the engine's module
versions, which an older darktable may not be able to read.

Written with AI assistance (Claude), directed and reviewed by the repository
owner.

## Tools

| Tool | Does |
|---|---|
| `list_film_rolls`, `list_images`, `image_info` | browse: film rolls; photos filtered by film roll, rating, color label, with paging; a photo's camera data |
| `get_thumbnail` | darktable's thumbnail of a photo (image) |
| `get_photo_metadata`, `list_tags`, `set_tags`, `set_metadata`, `set_location` | tags, metadata (title, description, creator, ...) and location, as darktable's tagging, metadata editor and geotagging |
| `set_rating`, `set_color_label` | rate (0–5), reject, color labels, as darktable's lighttable |
| `open_photo` | open a photo for editing (joins an edit already open in another app, unsaved changes included) |
| `open_in_darkroom`, `get_collection` | switch darktable's darkroom to a photo; the photos darktable's lighttable shows, and the selection |
| `check_sensor_clipping` | real sensor clipping, from the raw file, per color, with each color's headroom (needs the `[dust]` extra) |
| `open_darkroom_photo` | open the photo the user has open in darktable's darkroom (when darktable's window serves the library) |
| `list_modules`, `get_module` | the photo's modules; a module's settings with values, defaults, ranges and dropdown values |
| `set_module`, `enable_module` | change settings by name (checked, all or nothing), lists whole or by element (`grey[1]`), module on/off |
| `list_presets`, `apply_preset` | a module's presets, applied as darktable's presets menu does (e.g. "monochrome \| luminance-based") |
| `get_history`, `set_history_end` | history steps; undo/redo to a step |
| `add_module_instance`, `rename_module_instance`, `remove_module_instance` | module instances (e.g. a second color calibration for creative B&W) |
| `move_module` | move a module in the pipe (e.g. a second color calibration after the tone mapper) |
| `get_curve`, `set_curve` | curves of rgbcurve, tonecurve, colorzones, basecurve |
| `duplicate_photo` | a duplicate (version) of a photo, with its edit or virgin |
| `compress_history` | compress (or truncate) the history as darktable's history panel does; saves first |
| `get_geometry`, `rotate_photo`, `crop_photo` | orientation, straightening (with the automatic crop) and crop, through darktable's flip, rotate and perspective, and crop modules |
| `export_photo` | darktable's export of the saved edit to a file (format, size, quality, output pattern, style; the user's export settings otherwise) |
| `get_blending`, `set_blending` | a module's blending: blend mode, opacity, parametric ranges per channel in the darkroom's units (e.g. denoise only the shadows) |
| `add_ai_mask` | darktable's AI object mask: points on a subject, outlined into path shapes on a module (needs darktable built with AI, AI enabled) |
| `add_mask`, `list_masks`, `remove_mask` | drawn shapes (circle, ellipse, gradient, path, brush stroke) on a module, placed on the photo as shown; `set_blending(raster_source=...)` reuses another module's mask |
| `measure_photo` | values at points and in boxes, histograms and clipping of the rendered photo |
| `ai_denoise`, `job_status`, `list_jobs`, `cancel_job` | darktable's AI raw denoise as a background job: a new DNG photo beside the original, in its group (needs darktable built with AI) |
| `find_dust_spots`, `heal_dust_spots` | sensor dust: a map of dust recurring across the photo's film roll (read from the raw files, cached; small specks and large soft blobs), rated in this photo (raw files that are missing or unreadable are skipped and listed; with too few left the result says the map can't find dust); heal circles in retouch as one history step, checked at 100% before and after. Needs `pip install 'darktable-api-mcp[dust]'` |
| `retouch_spots`, `list_retouch_spots`, `edit_retouch_spot`, `remove_retouch_spots` | retouch circles that clone, heal, blur or fill, placed on the photo as shown; move, resize, change or delete them |
| `list_styles`, `create_style`, `apply_style`, `delete_style` | darktable's styles: make one from a photo's edit, apply one to photos (e.g. one B&W look on a set), as the lighttable does |
| `paste_edit` | one photo's edit, or some of its modules, onto other photos (append or overwrite), as darktable's copy and paste |
| `list_pickers`, `use_picker` | a module's color pickers and auto buttons (exposure's picker, color calibration's white balance picker, AgX's auto tune levels, tone equalizer's wands, ...), used as clicking them in darktable: on the photo in darktable's darkroom, with darktable's window serving the library |
| `render_preview` | darktable's rendering of the current, unsaved edit (image); `uncropped=True` shows the whole photo to choose a crop on; `zoom=1` a region at 100%; `history_step` an earlier step (before/after) |
| `save`, `discard_changes`, `start_over` | write the edit to the library; reopen as saved; delete the edit and apply darktable's defaults again (asks for `confirm`) |
| `library_status`, `release_library`, `acquire_library`, `takeover_library` | hand the library to darktable's GUI and take it back; `takeover_library` asks a running darktable to quit the normal way (asks for `confirm`, never kills it) |

Edits stay in memory until `save`. Errors come back as MCP tool errors with
darktable's message, e.g. `'exposure': 99 is outside -18..18`.

Measured on an Apple M1 (CPU only) with a 20 MP raw: `render_preview` after a
change 0.15–0.4 s at 1200 px; browsing calls a few milliseconds; the engine
starts in about 3 s on the first call.

## Requirements

1. **darktable-api**, from the `darktable-api` branch of the darktable fork:
   https://github.com/mmcc-xx/darktable/tree/darktable-api
   (see `src/api/README.md` there). Build darktable with the MCP server
   (on by default on this branch), which builds `darktable-api` alongside,
   and with AI for the AI mask and AI denoise tools:

       git clone -b darktable-api --recurse-submodules https://github.com/mmcc-xx/darktable.git
       cd darktable
       cmake -B build -G Ninja -DUSE_MCP=ON -DUSE_AI=ON
       cmake --build build --target darktable-api darktable

   darktable's README lists the build dependencies (on macOS:
   `brew bundle --file=.ci/Brewfile`; on Ubuntu 24.04 also `libpotrace-dev`
   and `libarchive-dev`). The AI build downloads ONNX Runtime itself; on
   Linux, darktable run from the build folder can't find it (it loads it by
   file name): `cmake --install build`, or set `plugins/ai/ort_library_path`
   in the library copy's darktablerc to
   `build/_deps/onnxruntime/lib/libonnxruntime.so.<version>`. The AI models
   are downloaded from darktable's preferences (AI tab).
2. Python 3.10 or later.

## Install

    git clone https://github.com/mmcc-xx/darktable-api-mcp.git
    cd darktable-api-mcp
    python -m venv .venv && .venv/bin/pip install -e .
    .venv/bin/python make_library_copy.py      # ~/.config/darktable -> ./library-copy

`make_library_copy.py` copies `library.db`, `data.db` and `darktablerc`
(safe while darktable is running) and sets `write_sidecar_files=never` in the
copy, so nothing done here touches the XMP files next to your photos. Your
photos are only read.

| Variable | | |
|---|---|---|
| `DTAPI_BIN` | required unless on the PATH | the `darktable-api` binary |
| `DTAPI_CONFIGDIR` | required | the darktable config dir to use: the copy's `config` folder |
| `DTAPI_CACHEDIR` | default: next to it, `cache` | the engine's darktable cache |
| `DTAPI_SOCKET` | default: `darktable-api.sock` next to the config dir (or `/tmp/darktable-api-<uid>-<hash>.sock` if that path is too long) | where the engine listens; every app using the library must use the same one |
| `DTAPI_GUI_BIN` | default: `darktable` next to `DTAPI_BIN` | the only darktable `takeover_library` may quit |

### Claude Code

    claude mcp add darktable-api \
      -e DTAPI_BIN=/path/to/darktable/build/bin/darktable-api \
      -e DTAPI_CONFIGDIR=/path/to/darktable-api-mcp/library-copy/config \
      -- /path/to/darktable-api-mcp/.venv/bin/darktable-api-mcp

### Claude Desktop (or another MCP client)

```json
{
  "mcpServers": {
    "darktable-api": {
      "command": "/path/to/darktable-api-mcp/.venv/bin/darktable-api-mcp",
      "env": {
        "DTAPI_BIN": "/path/to/darktable/build/bin/darktable-api",
        "DTAPI_CONFIGDIR": "/path/to/darktable-api-mcp/library-copy/config"
      }
    }
  }
}
```

### Skills

`skills/` holds Agent Skills that teach Claude how to use the tools for
whole tasks: `darktable-edit` (darktable's standard scene-referred
workflow), `darktable-mono` (black and white, with film filter mixes) and
`darktable-cleanup` (dust, retouching, noise, sharpening, fringes),
`darktable-local` (masks and local edits), `darktable-batch` (one look
across several photos) and `darktable-review` (critique and suggestions
without changing anything).
For Claude Code, link them into `~/.claude/skills/` (all projects) or a
project's `.claude/skills/`:

    ln -s /path/to/darktable-api-mcp/skills/darktable-edit ~/.claude/skills/

## Sharing the engine with a web app

The server doesn't run darktable itself: it connects to a darktable-api
engine on a unix socket next to the library copy, and starts one if none is
running. [darktable-api-web](https://github.com/mmcc-xx/darktable-api-web)
pointed at the same library copy uses the same engine, so you can watch in
the browser while the AI edits, and both work on the same photos: a photo
open in both is one shared edit (`open_photo` reports `"shared": true`), the
web page updates live, and `save` saves the whole edit. The engine keeps up
to 3 photos open and stops 10 minutes after the last client disconnected,
unless something is unsaved.

darktable's own window can serve the library as well: started (from the
same fork) with `--api-socket` pointing at the same socket, it takes over
from the engine automatically, unsaved edits included, and the photo open in
its darkroom is shared live: the AI's changes move darktable's sliders, and
the user's changes in darktable reach the AI. When darktable quits, the
server goes back to the engine. (`release_library` / `acquire_library` are for
a darktable started without `--api-socket`.)

Tested on macOS (Apple M1) and Linux (Ubuntu 24.04): the engine's test
suite, headless and with darktable's window, and Claude Code sessions with
the skills. On Linux, `takeover_library` uses darktable's D-Bus `Quit`
method (untested).

## License

GPL-3.0, like darktable.
