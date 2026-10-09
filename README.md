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
| `list_film_rolls`, `list_images`, `image_info` | browse: film rolls; photos filtered by film roll, rating, color label, with paging |
| `get_thumbnail` | darktable's thumbnail of a photo (image) |
| `set_rating`, `set_color_label` | rate (0–5), reject, color labels, as darktable's lighttable |
| `open_photo` | open a photo for editing (joins an edit already open in another app, unsaved changes included) |
| `open_darkroom_photo` | open the photo the user has open in darktable's darkroom (when darktable's window serves the library) |
| `list_modules`, `get_module` | the photo's modules; a module's settings with values, defaults, ranges and dropdown values |
| `set_module`, `enable_module` | change settings by name (checked, all or nothing), lists whole or by element (`grey[1]`), module on/off |
| `list_presets`, `apply_preset` | a module's presets, applied as darktable's presets menu does (e.g. "monochrome \| luminance-based") |
| `get_history`, `set_history_end` | history steps; undo/redo to a step |
| `get_geometry`, `rotate_photo`, `crop_photo` | orientation, straightening (with the automatic crop) and crop, through darktable's flip, rotate and perspective, and crop modules |
| `export_photo` | darktable's export of the saved edit to a file (format, size, quality, output pattern, style; the user's export settings otherwise) |
| `get_blending`, `set_blending` | a module's blending: blend mode, opacity, parametric ranges per channel in the darkroom's units (e.g. denoise only the shadows) |
| `add_mask`, `list_masks`, `remove_mask` | drawn shapes (circle, ellipse, gradient) on a module, placed on the photo as shown |
| `measure_photo` | values at points and in boxes, histograms and clipping of the rendered photo |
| `find_dust_spots`, `heal_dust_spots` | sensor dust: a map of dust recurring across the photo's film roll (read from the raw files, cached), rated in this photo; heal circles in retouch as one history step, checked at 100% before and after. Needs `pip install 'darktable-api-mcp[dust]'` |
| `render_preview` | darktable's rendering of the current, unsaved edit (image); `uncropped=True` shows the whole photo to choose a crop on; `zoom=1` a region at 100% |
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
   enabled, which builds `darktable-api` alongside:

       git clone -b darktable-api --recurse-submodules https://github.com/mmcc-xx/darktable.git
       cd darktable
       cmake -B build -G Ninja -DUSE_MCP=ON
       cmake --build build --target darktable-api darktable

   darktable's README lists the build dependencies (on macOS:
   `brew bundle --file=.ci/Brewfile`).
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

Tested on macOS. On Linux, `takeover_library` uses darktable's D-Bus `Quit`
method (untested).

## License

GPL-3.0, like darktable.
