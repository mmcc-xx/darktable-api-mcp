# darktable-api-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant
(e.g. Claude, ChatGPT) browse a [darktable](https://www.darktable.org/)
library and edit photos. All the work is done by **darktable-api**, darktable's
own code running as a long-lived headless engine. The engine renders previews,
applies module settings and writes the history to the library exactly as
darktable does. darktable's own window (built from the same fork) can serve
the API as well instead, sharing the photo open in its darkroom live. A few
tools require running darktable with the GUI (the color pickers and auto buttons, and switching
darktable's darkroom to a photo), but most edits can be done headlessly. No Lua script or XMP editing is involved.

[Demo Video](https://www.youtube.com/watch?v=VDfZbxvvniY)

**What's included:**

- **78 MCP tools** ([list](#tools)): browse the library, rate, label and tag
  photos; open a photo and change any module's settings, with previews to
  check each step; presets and styles; crop and rotate; masks (drawn shapes,
  parametric ranges, AI object masks); curves; finding and healing sensor
  dust and other retouching; AI denoise; before/after comparisons and
  alternative versions to choose from; export.
- **6 [skills](#skills)** that teach the AI whole tasks:
  `darktable-edit` (a full develop in darktable's scene-referred
  workflow), `darktable-mono` (black and white), `darktable-local` (masks,
  dodge and burn), `darktable-cleanup` (dust, noise, sharpening),
  `darktable-batch` (one look across several photos) and
  `darktable-review` (a critique with concrete suggestions, without
  changing anything).

**This is experimental.** I recommend using it on a copy of your library (the copy script below
does that). Saving writes darktable's history in the engine's module
versions, which an older darktable may not be able to read.

Written with AI assistance (Claude), directed and reviewed by the repository
owner.

## Quick start: engine, MCP server and web app

These instructions will get the fork of darktable with the API, a web app, and the MCP server built and running.
This has been tested on macOS (Apple Silicon) and Ubuntu 24.04; other Linux
distributions need the equivalent packages (darktable's README links build
instructions for Fedora and others). Python 3.10 or later.

1. **Install the build dependencies.**

   macOS:

   Install [Homebrew](https://brew.sh), then:

       xcode-select --install
       mkdir -p ~/src && cd ~/src
       git clone -b darktable-api-5.6 --recurse-submodules https://github.com/mmcc-xx/darktable.git
       brew bundle --file=darktable/.ci/Brewfile

   Ubuntu 24.04:

       sudo apt install git build-essential python3-venv cmake ninja-build \
         intltool xsltproc libgtk-3-dev libjson-glib-dev liblcms2-dev \
         libexiv2-dev liblensfun-dev libcurl4-openssl-dev libsqlite3-dev \
         libxml2-dev libpugixml-dev librsvg2-dev libjpeg-dev libtiff-dev \
         libopenexr-dev libwebp-dev libavif-dev libheif-dev libjxl-dev \
         liblua5.4-dev libcolord-gtk-dev libgmic-dev libportmidi-dev \
         libsecret-1-dev libraw-dev libgphoto2-dev libgraphicsmagick1-dev \
         libopenjp2-7-dev libosmesa6-dev ocl-icd-opencl-dev libsdl2-dev \
         libcups2-dev libpotrace-dev libarchive-dev
       mkdir -p ~/src && cd ~/src
       git clone -b darktable-api-5.6 --recurse-submodules https://github.com/mmcc-xx/darktable.git

   `darktable-api-5.6` is darktable 5.6 with the API: its edits open in the
   darktable 5.6 you already use. To follow darktable's development
   version instead, clone `-b darktable-api` (see [Requirements](#requirements)).

2. **Build the engine** (darktable-api and a darktable from the same fork;
   10–20 minutes the first time, the same on both systems):

       cd ~/src/darktable
       cmake -B build -G Ninja -DUSE_API=ON -DUSE_AI=ON
       cmake --build build --target darktable-api darktable

3. **Install the MCP server and copy your library.** The copy is the
   library everything below works on; your own library and XMP files are
   never written:

       cd ~/src
       git clone https://github.com/mmcc-xx/darktable-api-mcp.git
       cd darktable-api-mcp
       python3 -m venv .venv && .venv/bin/pip install -e '.[dust]'
       .venv/bin/python make_library_copy.py    # ~/.config/darktable -> ./library-copy

4. **Connect your MCP client.** It runs the server's command
   (`.venv/bin/darktable-api-mcp`) with two environment variables:
   `DTAPI_BIN` (the engine) and `DTAPI_CONFIGDIR` (the library copy's
   `config` folder); [Install](#install) has a JSON example for clients
   configured that way. With Claude Code, for example, which also loads
   the skills (link them wherever your client looks for skills):

       claude mcp add darktable-api \
         -e DTAPI_BIN=$HOME/src/darktable/build/bin/darktable-api \
         -e DTAPI_CONFIGDIR=$HOME/src/darktable-api-mcp/library-copy/config \
         -- $HOME/src/darktable-api-mcp/.venv/bin/darktable-api-mcp
       mkdir -p ~/.claude/skills
       for s in ~/src/darktable-api-mcp/skills/*/; do ln -s "$s" ~/.claude/skills/; done

5. **Run the web app on the same library copy**, so the browser and the AI
   share one engine and one edit:

       cd ~/src
       git clone https://github.com/mmcc-xx/darktable-api-web.git
       cd darktable-api-web
       python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
       DTAPI_BIN=$HOME/src/darktable/build/bin/darktable-api \
       DTAPI_CONFIGDIR=$HOME/src/darktable-api-mcp/library-copy/config \
           .venv/bin/uvicorn dtweb.main:app --port 8020 --timeout-graceful-shutdown 3

   Open http://127.0.0.1:8020

6. **Try it.** Start your client, check it lists the darktable-api server
   (in Claude Code: `/mcp`), and ask e.g. "list my last film roll" or
   "open photo 12 and do a basic edit". The web page follows along live.

7. **Optional: darktable's own window** on the same library, shared live
   with the browser and the AI:

       ~/src/darktable/build/bin/darktable \
         --configdir ~/src/darktable-api-mcp/library-copy/config \
         --cachedir ~/src/darktable-api-mcp/library-copy/cache \
         --api-socket ~/src/darktable-api-mcp/library-copy/darktable-api.sock

The AI mask and AI denoise tools need darktable's AI models: download them
in that darktable's preferences (AI tab).

**Linux:** darktable run from the build folder can't find ONNX Runtime (it
loads it by file name), so the AI tools fail until you point the library
copy at it, with darktable's window closed:

    echo "plugins/ai/ort_library_path=$(ls ~/src/darktable/build/_deps/onnxruntime/lib/libonnxruntime.so.*.*.*)" \
      >> ~/src/darktable-api-mcp/library-copy/config/darktablerc

(or `sudo cmake --install build` in `~/src/darktable`, which installs this
darktable over any packaged one). darktable's window on Linux needs a
desktop session; the engine, MCP server and web app run headless, e.g. on
a server over SSH, with the web app reached through an SSH tunnel
(`ssh -L 8020:127.0.0.1:8020 server`).

## Tools

| Tool | Does |
|---|---|
| `list_film_rolls`, `list_images`, `image_info` | browse: film rolls; photos filtered by film roll, rating, color label, with paging; a photo's camera data |
| `get_thumbnail` | darktable's thumbnail of a photo (image) |
| `import_photos` | import files or folders (e.g. a card's DCIM) into the library, in place or copied into darktable's import folders; photos already there skipped; a background job |
| `make_thumbnails` | thumbnails in bulk (photos, a film roll, the whole library) rendered into darktable's thumbnail cache, as a background job |
| `get_photo_metadata`, `list_tags`, `set_tags`, `set_metadata`, `set_location` | tags, metadata (title, description, creator, ...) and location, as darktable's tagging, metadata editor and geotagging |
| `photo_sheet`, `tag_photos` | tagging by content: pages of thumbnails with each photo's id, date, camera, place and tags for the AI to look at, then each photo's own tags applied in one call |
| `set_rating`, `set_color_label` | rate (0–5), reject, color labels, as darktable's lighttable |
| `open_photo` | open a photo for editing (joins an edit already open in another app, unsaved changes included) |
| `open_in_darkroom`, `get_collection` | switch darktable's darkroom to a photo (needs darktable's window); the photos darktable's lighttable shows, and the selection |
| `check_sensor_clipping` | real sensor clipping, from the raw file, per color, with each color's headroom (needs the `[dust]` extra) |
| `open_darkroom_photo` | open the photo the user has open in darktable's darkroom (needs darktable's window) |
| `list_modules`, `get_module` | the photo's modules; a module's settings with values, defaults, ranges and dropdown values |
| `set_module`, `enable_module` | change settings by name (checked, all or nothing), lists whole or by element (`grey[1]`), module on/off |
| `list_presets`, `apply_preset` | a module's presets, applied as darktable's presets menu does (e.g. "monochrome \| luminance-based") |
| `get_history`, `set_history_end` | history steps; undo/redo to a step |
| `add_module_instance`, `rename_module_instance`, `remove_module_instance` | module instances (e.g. a second color calibration for creative B&W) |
| `move_module` | move a module in the pipe (e.g. a second color calibration after the tone mapper) |
| `get_curve`, `set_curve` | curves of rgbcurve, tonecurve, colorzones, basecurve |
| `duplicate_photo`, `remove_photos` | a duplicate (version) of a photo, with its edit or virgin; remove photos from the library as the lighttable's remove does (the files stay; asks for `confirm`) |
| `compress_history` | compress (or truncate) the history as darktable's history panel does; saves first |
| `get_geometry`, `rotate_photo`, `crop_photo` | orientation, straightening (with the automatic crop) and crop, through darktable's flip, rotate and perspective, and crop modules |
| `export_photo` | darktable's export of the saved edit to a file (format, size, quality, output pattern, style; the user's export settings otherwise) |
| `get_blending`, `set_blending` | a module's blending: blend mode, opacity, parametric ranges per channel in the darkroom's units (e.g. denoise only the shadows) |
| `add_ai_mask` | darktable's AI object mask: points on a subject, outlined into path shapes on a module (needs darktable built with AI, AI enabled) |
| `add_mask`, `list_masks`, `remove_mask` | drawn shapes (circle, ellipse, gradient, path, brush stroke) on a module, placed on the photo as shown; `set_blending(raster_source=...)` reuses another module's mask |
| `measure_photo` | values at points and in boxes, histograms and clipping of the rendered photo |
| `zone_map` | the rendered photo reduced to Ansel Adams's eleven print values (zones 0-X, steps of 10 in Lab L), with the share of the photo in each zone, zones at points, and chosen zones marked in red (needs the `[dust]` extra) |
| `ai_denoise`, `job_status`, `list_jobs`, `cancel_job` | darktable's AI raw denoise as a background job: a new DNG photo beside the original, in its group (needs darktable built with AI) |
| `ai_fill` | remove an object or blemish: an AI inpainting model (LaMa, or MI-GAN) fills a drawn shape, or the outline of an `add_ai_mask` module (`like_mask_of`), from its surroundings; shown by a new composite instance (non-destructive; needs the inpaint model installed) Small to medium shapes (up to about 1300 px across); larger ones are warned about (a soft smear). In darktable's window too: draw the area as a mask on any module, then `like_mask_of`. darktable-api on master now, the release branch from 5.8 on (not 5.6). |
| `find_dust_spots`, `heal_dust_spots` | sensor dust: a map of dust recurring across the photo's film roll (read from the raw files, cached; small specks and large soft blobs), rated in this photo (raw files that are missing or unreadable are skipped and listed; with too few left the result says the map can't find dust); `this_photo=True` (on by default for a black and white edit) also finds dust in this photo alone, in darktable's render of the current edit (one-off spots, or faint ones a black and white red filter brings out; numbered from 101, with their own crop sheet, healed only when picked by id); heal circles in retouch as one history step (each source kept clear of the dust by the circle's feathered border), or an AI fill (`ai_fill`, darktable master) where no clean source is nearby (`method`: auto, heal, ai_fill), checked at 100% before and after. Needs `pip install 'darktable-api-mcp[dust]'` |
| `retouch_spots`, `list_retouch_spots`, `edit_retouch_spot`, `remove_retouch_spots` | retouch circles that clone, heal, blur or fill, placed on the photo as shown; move, resize, change or delete them |
| `list_styles`, `create_style`, `apply_style`, `delete_style` | darktable's styles: make one from a photo's edit, apply one to photos (e.g. one B&W look on a set), as the lighttable does |
| `paste_edit` | one photo's edit, or some of its modules, onto other photos (append or overwrite), as darktable's copy and paste |
| `list_pickers`, `use_picker` | a module's color pickers and auto buttons (exposure's picker, color calibration's white balance picker, AgX's auto tune levels, tone equalizer's wands, ...), used as clicking them in darktable: on the photo in darktable's darkroom (needs darktable's window) |
| `render_preview` | darktable's rendering of the current, unsaved edit (image); `uncropped=True` shows the whole photo to choose a crop on; `zoom=1` a region at 100%; `history_step` an earlier step (before/after) |
| `compare_before_after`, `mark_before` | before and after of the edit (both renders, for the AI), shown to the user as a before/after slider in the web app and, for the photo in darktable's darkroom, a snapshot in darktable's snapshots panel split against the edit (when darktable's window serves the library); "before" is the step before the AI's first edit of the photo, or where `mark_before` put it |
| `propose_alternatives`, `apply_alternative`, `keep_alternative` | labeled alternatives (crops, B&W filters, looks: changes as calls of the editing tools) rendered without touching the edit, shown to the user as a strip in the web app; apply the one picked, or keep it as a duplicate photo with its version name set |
| `save`, `discard_changes`, `start_over` | write the edit to the library; reopen as saved; delete the edit and apply darktable's defaults again (asks for `confirm`) |
| `library_status`, `release_library`, `acquire_library`, `takeover_library` | hand the library to darktable's GUI and take it back; `takeover_library` asks a running darktable to quit the normal way (asks for `confirm`, never kills it) |

Edits stay in memory until `save`. Errors come back as MCP tool errors with
darktable's message, e.g. `'exposure': 99 is outside -18..18`.
The engine also sends an error code and name (`not_found`,
`invalid_params`, `busy`, `unsaved_changes`, ...; see its `src/api/README.md`):
the server retries `busy` errors a few times (0.5, 1 and 2 s apart) and
reopens a photo the engine had closed (`not_open`).

Measured on an Apple M1 (CPU only) with a 20 MP raw: `render_preview` after a
change 0.15–0.4 s at 1200 px; browsing calls a few milliseconds; the engine
starts in about 3 s on the first call.

## Requirements

1. **darktable-api**, from the darktable fork
   https://github.com/mmcc-xx/darktable (see `src/api/README.md` there). Build darktable with the API enabled
   (`-DUSE_API=ON`, off by default), which builds `darktable-api` alongside,
   and with AI for the AI mask and AI denoise tools:

   Which branch: the one matching the darktable you edit the library with
   (a newer darktable writes module versions an older one can't open):

   | Your darktable | Branch | What it is |
   |---|---|---|
   | 5.6.x (5.6.2 on) | `darktable-api-5.6` | darktable's 5.6 release with the API; darktable's point releases are merged in as they come out. Most people want this one |
   | built from master | `darktable-api` (the fork's default branch) | darktable's development version with the API; darktable master is merged in weekly once the tests pass. Its edits use module versions darktable 5.6 can't open |

   When darktable releases a new version (5.8, ...), a `darktable-api-5.8`
   branch starts from it and the previous release branch is frozen (tagged
   `api-dt<version>-final`, no more changes).

       git clone -b darktable-api-5.6 --recurse-submodules https://github.com/mmcc-xx/darktable.git
       cd darktable
       cmake -B build -G Ninja -DUSE_API=ON -DUSE_AI=ON
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

### Updating the engine

The branches are only ever added to (darktable's changes come in as
merges, never by rewriting history), so a pull updates a clone:

    cd ~/src/darktable
    git pull --recurse-submodules
    cmake --build build --target darktable-api darktable

Then reconnect the MCP server (or restart the web app). A clone of
`darktable-api` made before 2026-10-13 (the branch was rebased until then)
may refuse to pull; reset it once to the published branch (this drops
local commits): `git fetch origin && git reset --hard origin/darktable-api`.

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
| `DTAPI_VARIANTS_DIR` | default: `~/.cache/darktable-api-mcp/variants` | where `propose_alternatives` renders the alternatives (the web app serves them from there: set it the same for both) |
| `DTAPI_FILLS_DIR` | default: unset, a `darktable_fills` folder beside each photo | where `ai_fill` (and `heal_dust_spots`' fills) write their patch TIFFs: set it for a library copy, whose photos are someone's originals |
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

`skills/` holds [Agent Skills](https://agentskills.io) that teach the AI how
to use the tools for whole tasks (for clients that load skills, such as
Claude Code): `darktable-edit` (darktable's standard scene-referred
workflow), `darktable-mono` (black and white, with film filter mixes) and
`darktable-cleanup` (dust, retouching, noise, sharpening, fringes),
`darktable-local` (masks and local edits), `darktable-batch` (one look
across several photos) and `darktable-review` (critique and suggestions
without changing anything).
Link them where your client finds skills; for Claude Code, that is
`~/.claude/skills/` (all projects) or a project's `.claude/skills/`:

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
