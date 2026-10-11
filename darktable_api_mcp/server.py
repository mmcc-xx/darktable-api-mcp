"""
darktable-api-mcp: an MCP server that browses a darktable library and edits
photos through darktable-api, a long-running headless darktable.

Everything goes through the engine: darktable's own code renders, applies
edits and writes history to the library. No darktable GUI, Lua or XMP
patching is involved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .engine import EngineError, engine

INSTRUCTIONS = """\
Edits photos in a darktable library through darktable-api (a headless
darktable). Typical flow: list_images -> open_photo(id) -> get_module /
set_module (several times, checking render_preview) -> save. Edits stay in
memory until save; discard_changes reopens the saved state; start_over
applies darktable's defaults again. When the user names a photo by number
("photo 36"), that is its library id: open_photo(36) or image_info(36)
directly. list_images hides rejected photos unless rating="all".

darktable is scene-referred: set exposure first (midtones of the subject),
white balance in color calibration (channelmixerrgb), then the tone mapper
(agx, or sigmoid/filmicrgb on older edits; never two), then tone equalizer
(toneequal) and color balance rgb (colorbalancergb). Use get_module to learn a
module's setting names, ranges and dropdown values before set_module.
Orientation, straightening and cropping: get_geometry, rotate_photo,
crop_photo (render_preview(uncropped=True) shows the whole photo to choose a
crop on). export_photo writes a finished file with darktable's export.
Presets: list_presets / apply_preset (e.g. denoise (profiled) presets; for
black and white see below).
render_preview(zoom=1) shows a region at 100% to judge noise, sharpness and
dust.
Local edits: get_blending / set_blending (blend mode, opacity, parametric
ranges, e.g. noise reduction only in the shadows), add_mask / list_masks /
remove_mask (drawn circle, ellipse, gradient, path, brush stroke), add_ai_mask (darktable's AI
object mask: click points on a subject, it is outlined). measure_photo reads values,
histogram and clipping of the rendered photo.
Instances: add_module_instance (e.g. a second color calibration for creative
B&W, a second denoise pass), rename_module_instance, remove_module_instance.
Black and white: add_module_instance("channelmixerrgb") (a new instance,
not a copy) and apply_preset a "monochrome | ..." preset to it; keep the
first instance, which does the white balance (the monochrome presets switch
white balance off). Creative B&W: then move_module(it, after the tone
mapper, e.g. "agx"). Curves:
get_curve / set_curve (rgbcurve, tonecurve, colorzones, basecurve).
duplicate_photo makes a version (e.g. a B&W one beside the color one).
Before/after: render_preview(history_step=0) shows the original without
undoing. compress_history tidies the history (and saves).
AI denoise: ai_denoise (darktable's neural restore on the raw: a new, cleaner
DNG photo beside the original, in its group; edit that one). It runs as a
background job: job_status / list_jobs / cancel_job.
Sensor dust: find_dust_spots (maps dust that recurs across the photo's film
roll, rates it in this photo), heal_dust_spots (heal circles in retouch, one
history step, checked at 100% before/after). Other retouching:
retouch_spots (clone, heal, blur, fill circles), list_retouch_spots,
edit_retouch_spot, remove_retouch_spots.
Library: get_collection (the photos the user's darktable shows),
open_in_darkroom (switch darktable's darkroom to a photo), image_info (with
camera data), check_sensor_clipping (from the raw: real sensor clipping vs
highlights only bright in the render), get_photo_metadata, list_tags, set_tags, set_metadata (title,
description, ...), set_location.
Styles and copy/paste: list_styles, create_style, apply_style (e.g. one B&W
look on a set of photos), delete_style, paste_edit (one photo's edit, or some
of its modules, onto others). These save the photos they change.
Pickers and auto buttons (exposure's picker, color calibration's white
balance picker, AgX's auto tune levels, tone equalizer's wands, ...):
list_pickers / use_picker, on the photo in darktable's darkroom only (with
darktable's window serving the library).

The engine is shared: the user may be looking at or editing the same photo
in a web app at the same time. open_photo joins an edit already open there,
including its unsaved changes; changes made here show up for the user right
away, and save saves the photo's whole edit (theirs too). darktable's own
window may be serving the library: then the photo in its darkroom is the
one the user is editing there (library_status: darkroom_imgid;
open_darkroom_photo opens it), and your changes move its sliders. When the
user talks about "this photo" or "the photo I have open", that's usually it. Tell the
user what you are doing. One photo is "current" for these tools at a time (the last
open_photo). While the library is released to darktable's GUI, only
library_status, acquire_library and takeover_library work."""

mcp = FastMCP("darktable-api", instructions=INSTRUCTIONS, log_level="WARNING")


def _err(exc: EngineError) -> ToolError:
    """Engine errors become MCP tool errors (isError), with the engine's message."""
    return ToolError(str(exc))


async def _call(method: str, **params) -> dict:
    try:
        return await engine.call(method, **params)
    except EngineError as exc:
        raise _err(exc)


async def _require(*names: str) -> None:
    """Refuse a feature the server doesn't have, rather than have an older
    one ignore an option or fail with "unknown method"."""
    for name in names:
        try:
            ok = await engine.supports(name)
        except EngineError as exc:
            raise _err(exc)
        if not ok:
            raise ToolError(f"the darktable serving the library is too old for this ('{name}'): "
                            "it needs a newer darktable-api build (restart darktable or the engine)")


async def _edit(image_id: int | None, method: str, **params) -> dict:
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    try:
        return await engine.edit(image_id, method, **params)
    except EngineError as exc:
        raise _err(exc)


def _filters(film_roll_id: int | None, rating: str, label: str | None) -> dict:
    labels = ["red", "yellow", "green", "blue", "purple"]
    return {"film_id": film_roll_id if film_roll_id is not None else -1, "rating": rating,
            "label": labels.index(label) if label in labels else -1}


# ── browsing ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_film_rolls() -> dict:
    """The library's film rolls (folders): id, folder, number of photos."""
    return await _call("film_rolls")


@mcp.tool()
async def list_images(film_roll_id: int | None = None, rating: str = "visible",
                      label: str | None = None, offset: int = 0, limit: int = 50) -> dict:
    """Photos in folder/filename order, with id, filename, folder, rating
    (0-5), rejected, color labels (0 red, 1 yellow, 2 green, 3 blue, 4 purple),
    size and history_end (0 = unedited).

    film_roll_id: from list_film_rolls; omit for all.
    rating: "visible" (not rejected, default; rejected photos are left
            out), "all", "rejected", or "1".."5"
            (at least that many stars).
    label: "red", "yellow", "green", "blue" or "purple" to filter by label.
    offset, limit: paging (limit up to 1000); "total" is the full count."""
    return await _call("images_list", offset=offset, limit=limit, **_filters(film_roll_id, rating, label))


@mcp.tool()
async def image_info(image_id: int) -> dict:
    """One photo's library entry (as in list_images), and its camera data
    (exif: maker, model, lens, aperture, exposure_time in seconds,
    exposure_bias, iso, focal_length, taken, raw, monochrome)."""
    return await _call("image_info", imgid=image_id)


@mcp.tool(structured_output=False)
async def get_thumbnail(image_id: int, size: int = 512) -> Image:
    """darktable's thumbnail of a photo with its current saved edit, fitted
    inside size x size (from darktable's thumbnail cache)."""
    try:
        return Image(data=await engine.thumbnail(image_id, max(64, min(size, 2048))), format="jpeg")
    except EngineError as exc:
        raise _err(exc)


@mcp.tool()
async def set_rating(image_id: int, rating: int | None = None, reject: bool = False) -> dict:
    """Set a photo's star rating (0-5), or reject it (reject=True), as
    darktable's lighttable does: rejecting keeps the stars, rating clears a
    reject. Written to the library immediately."""
    if reject:
        return await _call("set_rating", imgid=image_id, rating="reject")
    if rating is None:
        raise ToolError("give rating (0-5) or reject=True")
    return await _call("set_rating", imgid=image_id, rating=rating)


@mcp.tool()
async def set_color_label(image_id: int, label: str, on: bool = True) -> dict:
    """Turn a color label (red, yellow, green, blue, purple) on or off.
    Written to the library immediately."""
    labels = ["red", "yellow", "green", "blue", "purple"]
    if label not in labels:
        raise ToolError(f"label must be one of {', '.join(labels)}")
    return await _call("set_label", imgid=image_id, label=labels.index(label), on=on)


# ── editing ──────────────────────────────────────────────────────────────────

def _ids(image_ids: list[int] | None) -> list[int]:
    ids = image_ids or ([engine.image_id] if engine.image_id is not None else None)
    if not ids:
        raise ToolError("give image_ids, or open a photo first")
    return ids


@mcp.tool()
async def get_photo_metadata(image_id: int | None = None) -> dict:
    """A photo's tags (darktable's own "darktable|..." left out), metadata
    (title, description, creator, publisher, rights, notes, ... as
    darktable's metadata editor shows them) and location (latitude,
    longitude, elevation, or null)."""
    await _require("image_metadata")
    params = {"imgid": image_id if image_id is not None else engine.image_id}
    if params["imgid"] is None:
        raise ToolError("give image_id, or open a photo first")
    return await _call("image_metadata", **params)


@mcp.tool()
async def list_tags(filter: str = "") -> dict:
    """The library's tags (hierarchies with "|", e.g. "places|france|paris")
    and how many photos carry each; filter matches part of the name."""
    await _require("tag_list")
    return await _call("tag_list", filter=filter)


@mcp.tool()
async def set_tags(attach: list[str] | None = None, detach: list[str] | None = None,
                   image_ids: list[int] | None = None) -> dict:
    """Attach and/or detach tags on photos (default: the open one), as
    darktable's tagging module does; new tags are created. Use "|" for a
    hierarchy ("places|france|paris")."""
    await _require("set_tags")
    params: dict[str, Any] = {"imgids": _ids(image_ids)}
    if attach:
        params["attach"] = attach
    if detach:
        params["detach"] = detach
    return await _call("set_tags", **params)


@mcp.tool()
async def set_metadata(values: dict[str, str], image_ids: list[int] | None = None) -> dict:
    """Set metadata fields on photos (default: the open one), as darktable's
    metadata editor does: e.g. {"title": "...", "description": "...",
    "creator": "...", "rights": "..."}; "" clears a field. Field names as
    get_photo_metadata lists them."""
    await _require("set_metadata")
    return await _call("set_metadata", imgids=_ids(image_ids), values=values)


@mcp.tool()
async def set_location(latitude: float | None = None, longitude: float | None = None,
                       elevation: float | None = None, clear: bool = False,
                       image_ids: list[int] | None = None) -> dict:
    """Set photos' location (default: the open one), as darktable's
    geotagging does: latitude and longitude in degrees, elevation in metres
    (optional); clear=True removes it."""
    await _require("set_location")
    params: dict[str, Any] = {"imgids": _ids(image_ids)}
    if clear:
        params["clear"] = True
    else:
        if latitude is None or longitude is None:
            raise ToolError("give latitude and longitude, or clear=True")
        params.update(latitude=latitude, longitude=longitude)
        if elevation is not None:
            params["elevation"] = elevation
    return await _call("set_location", **params)


@mcp.tool()
async def open_photo(image_id: int, discard_unsaved: bool = False) -> dict:
    """Open a photo for editing and make it the current photo for the editing
    tools; lists its modules and history. If the photo is already open (by
    you earlier, or by the user in another app), this joins that edit with its
    unsaved changes ("unsaved": true); discard_unsaved=True reloads it as
    saved instead, dropping those changes for everyone (ask the user first).
    Photos you opened before stay open, with their unsaved changes, until
    saved; the engine keeps a few open at once."""
    try:
        opened = await engine.open(image_id, fresh=discard_unsaved)
        mods = await engine.edit(image_id, "module_list")
        hist = await engine.edit(image_id, "history_list")
        info = await engine.call("image_info", imgid=image_id)
    except EngineError as exc:
        raise _err(exc)
    return {"image": info.get("image"), "unsaved": opened.get("unsaved"),
            "shared": opened.get("joined"),
            "modules": [m for m in mods["modules"] if m["in_history"] or m["enabled"]],
            "history_end": hist["history_end"], "history_items": len(hist["items"])}


@mcp.tool()
async def get_collection(offset: int = 0, limit: int = 100) -> dict:
    """darktable's current collection: the photos the user's lighttable and
    filmstrip show, in their order (as list_images entries), the
    collection's rules, and which photos are selected."""
    await _require("collection")
    return await _call("collection", offset=offset, limit=limit)


@mcp.tool()
async def open_in_darkroom(image_id: int) -> dict:
    """Show a photo in darktable's darkroom (darktable's window must be
    serving the library), as clicking it in the filmstrip, or
    double-clicking it in the lighttable, does; then make it the current
    photo here. The darkroom saves the photo it leaves."""
    await _require("darkroom_open")
    await _call("darkroom_open", imgid=image_id)
    try:
        for _ in range(60):
            st = await engine.library("library_status")
            if st.get("darkroom_imgid") == image_id:
                break
            await asyncio.sleep(0.5)
        else:
            raise ToolError("darktable didn't switch to the photo within 30 s")
    except EngineError as exc:
        raise _err(exc)
    return await open_photo(image_id)


@mcp.tool()
async def open_darkroom_photo() -> dict:
    """Open the photo the user has open in darktable's darkroom (when
    darktable's window serves the library) and make it the current photo:
    edits then appear in darktable live, and the user's edits there are the
    same edit."""
    try:
        st = await engine.library("library_status")
    except EngineError as exc:
        raise _err(exc)
    if st.get("server") != "gui":
        raise ToolError("darktable's window isn't serving the library (the headless engine is): "
                        "ask the user which photo, or start darktable with --api-socket")
    if not st.get("darkroom_imgid"):
        raise ToolError("darktable's darkroom shows no photo right now")
    return await open_photo(st["darkroom_imgid"])


@mcp.tool()
async def list_modules() -> dict:
    """The open photo's modules in pipeline order: operation (e.g. exposure,
    agx, colorbalancergb, channelmixerrgb, toneequal), instance, name, label
    (as darktable shows the module, e.g. "local contrast" for bilat), enabled,
    whether it is in the history."""
    return await _edit(None, "module_list")


@mcp.tool()
async def get_module(operation: str, instance: int = 0) -> dict:
    """A module's settings on the open photo: for each setting its name,
    current value, default, allowed range (min/max) and, for dropdowns, the
    possible values (name and label). List settings have a "shape" (e.g.
    [4], or [6, 7]) and nested values, e.g. color calibration's gray mix
    "grey" (R, G, B, normalization). Use these names in set_module."""
    return await _edit(None, "module_get", operation=operation, instance=instance)


@mcp.tool()
async def set_module(operation: str, values: dict[str, Any], instance: int = 0) -> dict:
    """Change settings of a module on the open photo, e.g.
    set_module("exposure", {"exposure": 0.7}). Names and ranges come from
    get_module; dropdown values by name, label or number. List settings
    whole, as nested lists of their shape ({"grey": [0.3, 0.6, 0.1, 0]}), or
    one element by index ({"grey[1]": 0.6}, {"x[0][3]": 0.5}). All values
    are checked first: one bad value changes nothing. Turns the module on and
    adds a history step, as darktable's darkroom does. Not saved until save.
    On the photo in darktable's darkroom, darktable then adjusts related
    settings as it does when a control is moved (e.g. AgX keeps its pivot,
    exposure keeps black below white); darktable_also_changed in the reply
    lists them. Headless (no darktable window) that doesn't happen: check
    related settings yourself (e.g. exposure's mode must be manual for
    exposure to apply)."""
    if any("[" in k or isinstance(v, list) for k, v in values.items()):
        await _require("module_set.lists")
    return await _edit(None, "module_set", operation=operation, values=values, instance=instance)


@mcp.tool()
async def list_presets(operation: str, instance: int = 0) -> dict:
    """A module's presets, as its presets menu lists them: name (pass it to
    apply_preset), label (as darktable shows it, also accepted), builtin,
    autoapply. E.g. channelmixerrgb (color calibration) has "monochrome |
    luminance-based" and film-emulation B&W mixes (apply them to a new
    instance: they switch its white balance off); denoiseprofile has
    "wavelets: chroma only"."""
    await _require("preset_list")
    return await _edit(None, "preset_list", operation=operation, instance=instance)


@mcp.tool()
async def apply_preset(operation: str, name: str, instance: int = 0) -> dict:
    """Apply a module preset on the open photo, as darktable's presets menu
    does: the preset's settings, on/off and blending replace the module's,
    as one history step. name: from list_presets (name or label). Not saved
    until save."""
    await _require("preset_apply")
    return await _edit(None, "preset_apply", operation=operation, name=name, instance=instance)


@mcp.tool()
async def enable_module(operation: str, enabled: bool, instance: int = 0) -> dict:
    """Turn a module on or off on the open photo (keeps its settings)."""
    return await _edit(None, "module_enable", operation=operation, enabled=enabled, instance=instance)


@mcp.tool()
async def add_module_instance(operation: str, instance: int = 0, copy: bool = False) -> dict:
    """Add another instance of a module after the given one in the pipe, as
    the module's "new instance" (copy=False: default settings) or
    "duplicate" (copy=True: its settings and blending) does. Returns the new
    instance number for get_module/set_module/... (instance=...). E.g. a
    second channelmixerrgb for creative monochrome, a second denoiseprofile
    for a separate chroma pass. One history step."""
    await _require("module_add")
    return await _edit(None, "module_add", operation=operation, instance=instance, copy=copy)


@mcp.tool()
async def move_module(operation: str, instance: int = 0, after: str | None = None, after_instance: int = 0,
                      before: str | None = None, before_instance: int = 0) -> dict:
    """Move a module (instance) in the pipe, right after or before another,
    as dragging it in darktable's darkroom does. darktable's rules apply
    (some modules can't move). Typical: a second color calibration for
    creative B&W goes after the tone mapper: move_module("channelmixerrgb",
    1, after="agx"). Returns its new position (iop_order)."""
    await _require("module_move")
    if (after is None) == (before is None):
        raise ToolError("give after=<operation> or before=<operation>")
    ref = {"after": {"operation": after, "instance": after_instance}} if after else \
          {"before": {"operation": before, "instance": before_instance}}
    return await _edit(None, "module_move", operation=operation, instance=instance, **ref)


@mcp.tool()
async def get_curve(operation: str, instance: int = 0) -> dict:
    """The curves of a curve module on the open photo: rgbcurve (channels R,
    G, B; in its linked mode only R is used, for all), tonecurve (L, a, b),
    colorzones (lightness, chroma, hue: each a curve over the "select by"
    axis), basecurve (curve). Per channel: type and points [[x, y], ...]."""
    await _require("curve_get")
    return await _edit(None, "curve_get", operation=operation, instance=instance)


@mcp.tool()
async def set_curve(operation: str, points: list[list[float]], channel: str | None = None,
                    type: str | None = None, instance: int = 0) -> dict:
    """Set one channel's curve of a curve module (rgbcurve, tonecurve,
    colorzones, basecurve) as dragging its nodes does: points [[x, y], ...]
    in 0..1 with x increasing (2-20 points). channel as get_curve lists them
    (default the first). type: "cubic spline", "centripetal spline",
    "monotonic spline". Turns the module on; one history step. Note that
    rgbcurve works on scene-linear values: middle gray is near x = 0.18, so
    an S-curve drawn for display values darkens the photo a lot."""
    await _require("curve_set")
    params: dict[str, Any] = {"operation": operation, "instance": instance, "points": points}
    if channel is not None:
        params["channel"] = channel
    if type is not None:
        params["type"] = type
    return await _edit(None, "curve_set", **params)


@mcp.tool()
async def duplicate_photo(image_id: int | None = None, virgin: bool = False, save_first: bool = False) -> dict:
    """Make a duplicate (a new version, a virtual copy of the same raw file)
    of a photo, as darktable's lighttable does: with its saved edit, or
    virgin=True for an unedited one. E.g. a B&W version beside the color
    one: duplicate, open_photo(the new id), convert. If the photo has unsaved
    changes, save_first=True saves them first (ask the user; they may be
    theirs). Returns the new photo's library entry."""
    await _require("image_duplicate")
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("give image_id, or open a photo first")
    try:
        return await engine.call("image_duplicate", imgid=image_id, virgin=virgin, save=save_first)
    except EngineError as exc:
        if "unsaved" in str(exc):
            raise ToolError("the photo has unsaved changes and the duplicate gets the saved edit: save, or "
                            "pass save_first=True (both save the whole shared edit: ask the user)")
        raise _err(exc)


@mcp.tool()
async def remove_module_instance(operation: str, instance: int) -> dict:
    """Delete one instance of a module (not a module's only instance: switch
    that off with enable_module). Its history steps go with it, as in
    darktable; undoing to before the deletion doesn't bring it back."""
    await _require("module_remove")
    return await _edit(None, "module_remove", operation=operation, instance=instance)


@mcp.tool()
async def rename_module_instance(operation: str, instance: int, name: str) -> dict:
    """Label a module instance as darktable's module header does, e.g.
    "creative mono"; "" gives the label back to darktable."""
    await _require("module_rename")
    return await _edit(None, "module_rename", operation=operation, instance=instance, name=name)


@mcp.tool()
async def compress_history(truncate: bool = False, confirm: bool = False) -> dict:
    """Compress the open photo's history as darktable's history panel does:
    one step per module instance (truncate=True instead only drops the steps
    above the current one, i.e. undone ones). This SAVES the photo's whole
    edit first (darktable compresses the saved history), which may be the
    user's too: ask, then pass confirm=True."""
    if not confirm:
        raise ToolError("compress_history saves the edit first: ask the user, then pass confirm=True")
    await _require("history_compress")
    return await _edit(None, "history_compress", truncate=truncate)


@mcp.tool()
async def get_history() -> dict:
    """The open photo's history steps: num (0-based; darktable's history panel
    shows num + 1), operation, instance, label (the step's name as darktable
    shows it, e.g. "local contrast", "exposure • sky": use it when talking to
    the user), enabled, applied; and history_end, the number of steps
    applied."""
    return await _edit(None, "history_list")


@mcp.tool()
async def set_history_end(end: int) -> dict:
    """Undo/redo to a step, as clicking in darktable's history panel:
    end = number of steps applied (0 = original). The next edit drops the
    steps above it."""
    return await _edit(None, "history_end", end=end)


@mcp.tool()
async def get_geometry() -> dict:
    """The open photo's orientation, straightening and crop:
    orientation (rotation in clockwise degrees and mirrored, relative to the
    raw file as the camera wrote it), angle (straightening, degrees, rotate
    and perspective module), autocrop (that module's automatic crop),
    crop (left, top, right, bottom as fractions 0-1 of the uncropped photo,
    i.e. after orientation and straightening), aspect ("free", "original" or
    "W:H" of the crop), frame_width/frame_height (the uncropped photo in
    pixels) and width/height (the result)."""
    return await _edit(None, "geometry_get")


@mcp.tool()
async def rotate_photo(degrees: int = 0, angle: float | None = None, mirror: str | None = None,
                       autocrop: str | None = None) -> dict:
    """Turn, mirror or straighten the open photo, as darktable's darkroom does.

    degrees: turn by quarter turns, relative to now: 90 = clockwise,
             -90 = counter-clockwise, 180 (orientation module). An existing
             crop turns with the photo.
    angle: straighten to this absolute angle in degrees (rotate and
           perspective module, -180..180; small values like -3..3 level a
           horizon). Positive turns counter-clockwise. The automatic crop
           is refitted so no blank corners show.
    mirror: "horizontal" (left-right) or "vertical".
    autocrop: rotate and perspective's automatic crop: "ASHIFT_CROP_LARGEST"
              (largest area, default), "ASHIFT_CROP_ASPECT" (keep the
              aspect ratio) or "ASHIFT_CROP_OFF".
    Returns the new geometry (as get_geometry). Not saved until save."""
    params: dict[str, Any] = {}
    if degrees:
        params["rotate"] = degrees
    if angle is not None:
        params["angle"] = angle
    if mirror is not None:
        params["flip"] = mirror
    if autocrop is not None:
        params["autocrop"] = autocrop
    if not params:
        raise ToolError("give degrees, angle, mirror or autocrop")
    return await _edit(None, "geometry_set", **params)


@mcp.tool()
async def crop_photo(left: float | None = None, top: float | None = None, right: float | None = None,
                     bottom: float | None = None, aspect: str | None = None, remove: bool = False) -> dict:
    """Crop the open photo (crop module), as darktable's darkroom does.

    left, top, right, bottom: the crop's edges as fractions 0-1 of the
        uncropped photo (as render_preview(uncropped=True) shows it: after
        orientation and straightening); right/bottom are edges, not sizes.
        Give all four, or none to reshape the current crop with aspect.
    aspect: "free", "original" (the camera's ratio), "square", or "W:H" of
        the result, e.g. "3:2", "2:3" (portrait), "16:9", "4:5". The largest
        box of that aspect inside the given (or current) one is used,
        centered on it.
    remove: remove the crop.
    Returns the new geometry (as get_geometry). Not saved until save."""
    edges = [left, top, right, bottom]
    if remove:
        if any(e is not None for e in edges) or aspect:
            raise ToolError("remove=True takes no edges or aspect")
        return await _edit(None, "geometry_set", crop=None)
    params: dict[str, Any] = {}
    if any(e is not None for e in edges):
        if any(e is None for e in edges):
            raise ToolError("give all four of left, top, right, bottom")
        params["crop"] = {"left": left, "top": top, "right": right, "bottom": bottom}
    if aspect is not None:
        params["aspect"] = aspect
    if not params:
        raise ToolError("give the crop's edges, an aspect, or remove=True")
    return await _edit(None, "geometry_set", **params)


@mcp.tool(structured_output=False)
async def render_preview(size: int = 1200, uncropped: bool = False, zoom: float | None = None,
                         center_x: float = 0.5, center_y: float = 0.5, history_step: int | None = None) -> Image:
    """darktable's rendering of the open photo with its current (unsaved)
    edit, fitted inside size x size. Use it to check edits. uncropped=True
    shows the whole photo without the crop module's crop (still oriented
    and straightened), the frame crop_photo's fractions refer to.

    zoom: render a size x size region at this scale instead of the whole
          photo: 1 = 100% (each pixel of the photo, for judging noise,
          sharpness, dust), 0.5 = 50%, up to 2. center_x, center_y: the
          region's center as fractions 0-1 of the photo (0.5, 0.5 = middle;
          the region stays inside the photo).
    history_step: render the edit as it was after that many history steps
          (0 = the original, as get_history counts), for before/after,
          without undoing anything."""
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    region = None
    if zoom is not None:
        await _require("render.zoom")
        region = {"zoom": max(0.01, min(zoom, 2.0)), "center_x": min(max(center_x, 0.0), 1.0),
                  "center_y": min(max(center_y, 0.0), 1.0)}
    if history_step is not None:
        await _require("render.history_end")
        region = dict(region or {}, history_end=max(0, history_step))
    try:
        r = await engine.render(engine.image_id, max(64, min(size, 2560)), max(64, min(size, 2560)),
                                uncropped, region)
    except EngineError as exc:
        raise _err(exc)
    if r is None:
        raise ToolError("overtaken by a newer render request")
    return Image(data=r[0], format="jpeg")


@mcp.tool()
async def export_photo(image_id: int | None = None, format: str | None = None, size: int | None = None,
                       quality: int | None = None, high_quality: bool | None = None,
                       path: str | None = None, on_conflict: str | None = None, style: str | None = None,
                       save_first: bool = False) -> dict:
    """Export a photo to a file with darktable's export (as its export
    button does), from the photo's SAVED edit. Settings not given come from
    the user's darktable export settings.

    image_id: default the open photo.
    format: "jpeg", "tiff", "png", "webp", "jpegxl", ... (darktable's format
            modules; "jpg"/"tif"/"jxl" work too).
    size: longest side in pixels; 0 = full size.
    quality: 1-100 for JPEG, WebP, JPEG XL.
    high_quality: process at full resolution, then scale (slower, sharper).
    path: output pattern without extension, with darktable variables, e.g.
          "$(FILE_FOLDER)/darktable_exported/$(FILE_NAME)" (the usual
          default) or "/Users/me/Desktop/$(FILE_NAME)_web". A folder ending
          in "/" gets the file name.
    on_conflict: "unique" (add _01, ...), "overwrite", "overwrite_if_changed",
                 "skip".
    style: a darktable style to apply on export.
    save_first: if the photo has unsaved changes, save them first (they
                belong to whoever is editing it, so ask the user); without
                it, unsaved changes are refused.
    Returns the file written (or skipped: true)."""
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("give image_id, or open a photo first")
    params: dict[str, Any] = {"imgid": image_id, "save": save_first}
    if format:
        params["format"] = format
    if size is not None:
        params["max_width"] = params["max_height"] = max(0, size)
    for key, value in (("quality", quality), ("high_quality", high_quality), ("path", path),
                       ("on_conflict", on_conflict), ("style", style)):
        if value is not None:
            params[key] = value
    try:
        if not await engine.supports("export.background"):
            return await engine.call("export", **params)
    except EngineError as exc:
        if "unsaved" in str(exc):
            raise ToolError("the photo has unsaved changes and export uses the saved edit: save, or pass "
                            "save_first=True (both save the whole shared edit: ask the user)")
        raise _err(exc)
    # in the background, so other apps using the engine aren't held up
    try:
        r = await _run_job(lambda: _call("export", background=True, **params), True, 900)
    except ToolError as exc:
        if "unsaved" in str(exc):
            raise ToolError("the photo has unsaved changes and export uses the saved edit: save, or pass "
                            "save_first=True (both save the whole shared edit: ask the user)")
        raise
    if r.get("state") == "failed":
        raise ToolError(r.get("error") or "the export failed")
    return r.get("result") or r


@mcp.tool()
async def save() -> dict:
    """Write the open photo's edit to the library (as leaving darktable's
    darkroom does), so the thumbnail and darktable's GUI show it."""
    return await _edit(None, "save")


@mcp.tool()
async def discard_changes() -> dict:
    """Throw away unsaved changes: reopen the open photo as saved."""
    if engine.image_id is None:
        raise ToolError("no photo is open")
    return await open_photo(engine.image_id, discard_unsaved=True)


@mcp.tool()
async def start_over(confirm: bool = False) -> dict:
    """Delete the open photo's whole edit (saved and unsaved) and apply
    darktable's defaults again (the workflow's modules and auto-apply
    presets), saved immediately. Ask the user first; pass confirm=True."""
    if not confirm:
        raise ToolError("start_over deletes the whole edit: ask the user, then pass confirm=True")
    return await _edit(None, "reset")


# ── readouts ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def measure_photo(points: list[list[float]] | None = None, boxes: list[dict] | None = None,
                        radius: int = 2, size: int = 1024, zoom: float | None = None,
                        center_x: float = 0.5, center_y: float = 0.5, bins: int = 32,
                        history_step: int | None = None) -> dict:
    """Read the open photo as darktable renders it (display sRGB output, as
    the darkroom shows it; not values inside the pipe).

    points: [[x, y], ...] fractions of the photo: the color there, averaged
            over a (2*radius+1)^2 pixel square: rgb (0-1), lab, luminance
            min/max (linear 0-1). null if outside what was rendered.
    boxes: [{left, top, right, bottom}] fractions: the same over each box.
    Also histograms (red, green, blue, luminance; bins) and the fraction of
    pixels clipped in the highlights (a channel at 255) and shadows (all 0).
    size: the render's size; zoom/center_x/center_y as render_preview, to
    measure at 100% (points outside the region come back null).
    history_step: measure the edit as it was after that many steps (0 =
    the original), e.g. to compare before/after."""
    await _require("sample")
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    params: dict[str, Any] = {"width": max(64, min(size, 4096)), "height": max(64, min(size, 4096)),
                              "radius": max(0, min(radius, 200)), "bins": max(2, min(bins, 256)),
                              "points": points or [], "boxes": boxes or []}
    if zoom is not None:
        params.update(zoom=max(0.01, min(zoom, 2.0)), center_x=center_x, center_y=center_y)
    if history_step is not None:
        await _require("render.history_end")
        params["history_end"] = max(0, history_step)
    return await _edit(None, "sample", **params)


# ── blending and masks ───────────────────────────────────────────────────────

@mcp.tool()
async def get_blending(operation: str, instance: int = 0) -> dict:
    """A module's blending (the section under each module in the darkroom):
    masks (off, uniform, drawn, parametric, drawn & parametric, raster),
    blend_mode, reverse, opacity (0-100), blend_parameter, feathering_radius,
    blur_radius, brightness, contrast, details, combine, feathering_guide,
    drawn_shapes (count), the parametric ranges that are on, and the
    channels this module's color space offers (e.g. g, R, G, B, Jz, Cz, hz for
    scene-referred RGB modules; L, a, b, C, h for Lab ones)."""
    await _require("blend_get")
    return await _edit(None, "blend_get", operation=operation, instance=instance)


@mcp.tool()
async def set_blending(operation: str, values: dict[str, Any], instance: int = 0) -> dict:
    """Change a module's blending, all or nothing, as one history step.

    values: any of masks, blend_mode (e.g. "normal", "multiply", "lighten"),
    reverse, opacity (0-100), blend_parameter, feathering_radius (0-250),
    blur_radius (0-100), brightness, contrast, details (-1..1), combine
    ("exclusive", "inclusive", "exclusive & inverted", "inclusive &
    inverted"), feathering_guide, and parametric: ranges per channel and
    direction, in the darkroom's units (percent for gray/RGB/L/chroma,
    -128..128 for Lab a/b, degrees for hue), as four values [low end, low
    full, high full, high end], e.g. shadows only on a scene-referred module:
    {"parametric": {"g_in": {"range": [0, 0, 18, 40]}}}; "inverted": true
    flips a range; null switches a channel off. Ranges turn the parametric
    mask on. raster_source {"operation", "instance"} uses another module's
    mask (one earlier in the pipe that has a drawn or parametric mask) as
    this one's, as the raster mask menu does; null removes it;
    raster_inverted flips it. Example: denoise only the shadows:
    set_blending("denoiseprofile", {"parametric": {"g_in": {"range": [0, 0, 10, 30]}}})."""
    await _require("blend_set")
    return await _edit(None, "blend_set", operation=operation, values=values, instance=instance)


async def _photo_to_raw(points: list[list[float]]) -> tuple[list[list[float]], int, int]:
    r = await _edit(None, "coords", points=points, **{"from": "image", "to": "raw"})
    return r["points"], r["raw_width"], r["raw_height"]


@mcp.tool()
async def add_mask(operation: str, shape: str, x: float | None = None, y: float | None = None,
                   radius: float = 0.1, radius_y: float | None = None, rotation: float = 0.0,
                   feather: float = 0.05, compression: float = 0.5, points: list[list[float]] | None = None,
                   width: float = 0.03, hardness: float = 0.5, density: float = 1.0,
                   combine: str = "union", inverted: bool = False, instance: int = 0) -> dict:
    """Draw a mask shape on a module, so it only acts there (one history
    step; the module's drawn mask is switched on).

    Positions and sizes are on the photo as shown: x, y fractions 0-1 of the
    photo; radius and feather fractions of the photo's shorter side.
      shape "circle": x, y, radius, feather.
      shape "ellipse": x, y, radius (horizontal), radius_y (vertical),
        rotation (degrees), feather.
      shape "gradient": a line through x, y at rotation (degrees, 0 =
        horizontal line: the module acts on one side, fading across it);
        compression 0-1 (how wide the fade is).
      shape "path": a closed outline through points [[x, y], ...] (3 or
        more), smoothed as darktable's path tool draws it (corners round
        off: add points close to a corner to keep it); feather. Points
        outside the photo snap to its edge.
      shape "brush": a stroke along points (2 or more), width (half the
        stroke's thickness), hardness and density 0-1.
    combine: how it joins the module's earlier shapes (union, intersection,
    difference, exclusion); inverted: the module acts outside it."""
    await _require("mask_add", "coords")
    if shape in ("path", "brush"):
        await _require("mask_add.path")
        brush = shape == "brush"
        if not points or len(points) < (2 if brush else 3):
            raise ToolError("path: 3 or more points; brush: 2 or more")
        # points just outside the photo (to reach its edge) snap onto it
        points = [[min(max(p[0], 0.0), 1.0), min(max(p[1], 0.0), 1.0)] for p in points]
        raw, _, _ = await _photo_to_raw(points)
        _, size = await _photo_circle_to_raw(points[0][0], points[0][1], width if brush else feather)
        sh: dict[str, Any] = {"type": shape, "points": raw}
        if brush:
            sh.update(width=size, hardness=hardness, density=density)
        else:
            sh["border"] = size
        return await _edit(None, "mask_add", operation=operation, instance=instance, shape=sh,
                           combine=combine, inverted=inverted)
    if x is None or y is None:
        raise ToolError(f"shape {shape}: needs x and y")
    import math
    g = await _edit(None, "geometry_get")
    W, H = g["width"], g["height"]
    short = min(W, H)
    # map the center and points along each radius to raw space: sizes and
    # directions follow the photo's rotation, crop and lens correction
    a = math.radians(rotation)
    dx = [math.cos(a) * short / W, math.sin(a) * short / H]
    probes = [[x, y], [x + radius * dx[0], y + radius * dx[1]],
              [x - (radius_y or radius) * dx[1] * H / W, y + (radius_y or radius) * dx[0] * W / H],
              [x + feather * dx[0], y + feather * dx[1]]]
    raw, rw, rh = await _photo_to_raw(probes)
    rshort = min(rw, rh)
    def dist(p, q):
        return math.hypot((p[0] - q[0]) * rw, (p[1] - q[1]) * rh) / rshort
    rot_raw = math.degrees(math.atan2((raw[1][1] - raw[0][1]) * rh, (raw[1][0] - raw[0][0]) * rw))
    if shape == "circle":
        sh = {"type": "circle", "x": raw[0][0], "y": raw[0][1], "r": dist(raw[0], raw[1]),
              "border": dist(raw[0], raw[3])}
    elif shape == "ellipse":
        sh = {"type": "ellipse", "x": raw[0][0], "y": raw[0][1], "ra": dist(raw[0], raw[1]),
              "rb": dist(raw[0], raw[2]), "rotation": rot_raw, "border": dist(raw[0], raw[3])}
    elif shape == "gradient":
        sh = {"type": "gradient", "x": raw[0][0], "y": raw[0][1], "rotation": rot_raw,
              "compression": max(0.0, min(compression, 1.0))}
    else:
        raise ToolError("shape: circle, ellipse, gradient, path or brush")
    return await _edit(None, "mask_add", operation=operation, instance=instance, shape=sh,
                       combine=combine, inverted=inverted)


@mcp.tool()
async def add_ai_mask(operation: str, include: list[list[float]], exclude: list[list[float]] | None = None,
                      combine: str = "union", inverted: bool = False, instance: int = 0) -> dict:
    """Mask a module to an object with darktable's AI object mask (SAM), as
    clicking on it with the darkroom's object mask tool: darktable finds the
    object around the points and traces its outline into path shapes on the
    module's mask (holes subtracted). One history step.

    include: [[x, y], ...] points on the object, fractions 0-1 of the photo
             as shown (as render_preview shows it); one is enough.
    exclude: points that are not part of it (to separate a neighbour).
    combine/inverted: as add_mask. The first call on a photo takes a few
    seconds (the photo is encoded); later ones are quick. Returns the
    group's formid (for remove_mask), how many paths, and the area (fraction
    of the photo). Check the result with render_preview. Needs darktable
    built with AI and AI enabled in its preferences."""
    await _require("mask_ai")
    if await engine.supports("mask_ai_encode"):
        # the slow first step (encoding the photo) in the background
        enc = await _run_job(lambda: _edit(None, "mask_ai_encode"), True, 600)
        if enc.get("state") not in ("done", None):
            raise ToolError(enc.get("error") or f"encoding the photo for AI masks ended {enc.get('state')}")
    points = ([{"x": p[0], "y": p[1], "include": True} for p in include]
              + [{"x": p[0], "y": p[1], "include": False} for p in (exclude or [])])
    r = await _edit(None, "mask_ai", operation=operation, instance=instance, points=points,
                    combine=combine, inverted=inverted)
    if r.get("area", 0) > 0.8:
        r["hint"] = ("the mask covers most of the photo: if that isn't the object, remove it "
                     "(remove_mask) and try again with exclude points around the object")
    return r


@mcp.tool()
async def list_masks(operation: str, instance: int = 0) -> dict:
    """The drawn shapes on a module's mask: formid (for remove_mask), name,
    type, combine, inverted, and where it is on the photo (x, y fractions)."""
    await _require("mask_list", "coords")
    r = await _edit(None, "mask_list", operation=operation, instance=instance)
    pts = [[s["x"], s["y"]] for s in r["shapes"] if "x" in s]
    if pts:
        img = (await _edit(None, "coords", points=pts, **{"from": "raw", "to": "image"}))["points"]
        it = iter(img)
        for s in r["shapes"]:
            if "x" in s:
                u, v = next(it)
                s["on_photo"] = [round(u, 4), round(v, 4)]
                del s["x"], s["y"]
    return r


@mcp.tool()
async def remove_mask(operation: str, formid: int, instance: int = 0) -> dict:
    """Take a shape (formid from list_masks) off a module's mask, as one
    history step."""
    await _require("mask_remove")
    return await _edit(None, "mask_remove", operation=operation, formid=formid, instance=instance)


# ── retouch ──────────────────────────────────────────────────────────────────

_RT_OPTIONS = ("blur_type", "blur_radius", "fill_mode", "fill_color", "fill_brightness")


async def _photo_circle_to_raw(x: float, y: float, radius: float) -> tuple[list[float], float]:
    """A circle on the photo as shown (x, y fractions; radius a fraction of
    the shorter side) in darktable's raw space: center, radius relative to
    the raw's shorter side."""
    import math
    g = await _edit(None, "geometry_get")
    short = min(g["width"], g["height"])
    raw, rw, rh = await _photo_to_raw([[x, y], [x + radius * short / g["width"], y]])
    r = math.hypot((raw[1][0] - raw[0][0]) * rw, (raw[1][1] - raw[0][1]) * rh) / min(rw, rh)
    return raw[0], r


async def _retouch_spec(spot: dict, adding: bool) -> dict:
    spec: dict[str, Any] = {}
    if "tool" in spot:
        spec["algorithm"] = spot["tool"]
    for k in _RT_OPTIONS:
        if k in spot:
            spec[k] = spot[k]
    if "x" in spot or "y" in spot or "radius" in spot:
        if adding or ("x" in spot and "y" in spot and "radius" in spot):
            (spec["x"], spec["y"]), spec["r"] = await _photo_circle_to_raw(spot["x"], spot["y"], spot["radius"])
        else:
            raise ToolError("moving or resizing a spot takes x, y and radius together")
    if "source_x" in spot or "source_y" in spot:
        if "source_x" not in spot or "source_y" not in spot:
            raise ToolError("source_x and source_y go together")
        (spec["sx"], spec["sy"]), _ = await _photo_circle_to_raw(spot["source_x"], spot["source_y"], 0.01)
    return spec


@mcp.tool()
async def list_retouch_spots() -> dict:
    """The retouch module's spots: formid (for edit_retouch_spot and
    remove_retouch_spots), tool (clone, heal, blur, fill) with its options,
    and where it is on the photo (on_photo: x, y fractions; source_on_photo
    for clone and heal)."""
    await _require("retouch_list", "coords")
    r = await _edit(None, "retouch_list")
    pts = []
    for s in r["spots"]:
        if "x" in s:
            pts.append([s["x"], s["y"]])
        if s.get("algorithm") in ("DT_IOP_RETOUCH_CLONE", "DT_IOP_RETOUCH_HEAL"):
            pts.append([s["sx"], s["sy"]])
    img = iter((await _edit(None, "coords", points=pts, **{"from": "raw", "to": "image"}))["points"]
               if pts else [])
    for s in r["spots"]:
        if "x" in s:
            s["on_photo"] = [round(v, 4) for v in next(img)]
        if s.get("algorithm") in ("DT_IOP_RETOUCH_CLONE", "DT_IOP_RETOUCH_HEAL"):
            s["source_on_photo"] = [round(v, 4) for v in next(img)]
        s.pop("sx", None)
        s.pop("sy", None)
        if s.get("algorithm"):
            s["tool"] = s.pop("algorithm").removeprefix("DT_IOP_RETOUCH_").lower()
    return r


@mcp.tool()
async def retouch_spots(spots: list[dict]) -> dict:
    """Add spots to the retouch module, as its circle tool does, all in one
    history step. Each spot: {x, y, radius, tool, ...} on the photo as shown
    (x, y fractions 0-1; radius a fraction of the shorter side, e.g. 0.01).
      tool "heal" (default) or "clone": copies from source_x, source_y
        (required; heal blends the copy into its surroundings);
      tool "blur": blur_radius (0.1-200, default the module's), blur_type
        ("gaussian" or "bilateral");
      tool "fill": fill_mode ("erase" or "color"), fill_color [r, g, b]
        (0-1, the module's working RGB), fill_brightness (-1 to 1).
    Returns the new spots' formids. For sensor dust, heal_dust_spots finds
    and heals them for you."""
    await _require("retouch_add", "coords")
    specs = [await _retouch_spec(s, adding=True) for s in spots]
    return await _edit(None, "retouch_add", spots=specs)


@mcp.tool()
async def edit_retouch_spot(formid: int, x: float | None = None, y: float | None = None,
                            radius: float | None = None, source_x: float | None = None,
                            source_y: float | None = None, tool: str | None = None,
                            options: dict[str, Any] | None = None) -> dict:
    """Move, resize or change one retouch spot (formid from
    list_retouch_spots). x, y and radius go together (on the photo as shown,
    as retouch_spots); source_x/source_y move a clone or heal source. tool
    swaps clone and heal, or blur and fill (the darkroom allows the same).
    options: blur_radius, blur_type, fill_mode, fill_color, fill_brightness."""
    await _require("retouch_set", "coords")
    spot: dict[str, Any] = {k: v for k, v in (("x", x), ("y", y), ("radius", radius), ("source_x", source_x),
                                               ("source_y", source_y), ("tool", tool)) if v is not None}
    spot.update(options or {})
    spec = await _retouch_spec(spot, adding=False)
    return await _edit(None, "retouch_set", formid=formid, **spec)


@mcp.tool()
async def remove_retouch_spots(formids: list[int]) -> dict:
    """Delete retouch spots (formids from list_retouch_spots), as one
    history step."""
    await _require("retouch_remove")
    return await _edit(None, "retouch_remove", formids=formids)


# ── styles and copy/paste ────────────────────────────────────────────────────

@mcp.tool()
async def list_styles(filter: str = "", camera_styles: bool = False) -> dict:
    """darktable's styles (saved sets of module settings): name, description,
    the modules each holds, and whether it sets the module order. name is
    what apply_style takes; label is how darktable shows it. filter matches
    names. darktable's built-in camera styles (base curves per
    camera model, hundreds) are left out unless camera_styles."""
    await _require("style_list")
    r = await _call("style_list", filter=filter)
    styles = [{"name": st["name"], "label": st.get("label", st["name"]), "description": st["description"],
               "modules": [i["operation"] + (f" {i['instance']}" if i["instance"] else "") for i in st["items"]],
               "module_order": st["module_order"]}
              for st in r["styles"] if camera_styles or "_l10n_camera styles" not in st["name"]]
    return {"styles": styles, "camera_styles_left_out": len(r["styles"]) - len(styles)}


@mcp.tool()
async def create_style(name: str, image_id: int | None = None, modules: list | None = None,
                       description: str = "", module_order: bool = False) -> dict:
    """Make a style from a photo's saved edit (default: the open photo; save
    first). Without modules it takes those darktable's create style dialog
    ticks by default (the modules meant for styles); modules picks some:
    names ("channelmixerrgb") or {"operation", "instance"}. module_order
    also stores the photo's module order."""
    await _require("style_create")
    params: dict[str, Any] = {"name": name, "description": description, "module_order": module_order}
    if image_id is not None:
        params["imgid"] = image_id
    elif engine.image_id is not None:
        params["imgid"] = engine.image_id
    if modules:
        params["modules"] = modules
    return await _call("style_create", **params)


@mcp.tool()
async def apply_style(name: str, image_ids: list[int] | None = None, save_first: bool = False) -> dict:
    """Apply a style to photos (default: the open one), as darktable's
    lighttable and darkroom do: its modules are added to each edit as new
    history steps, and the result is saved. A photo with unsaved changes
    here is refused unless save_first (those changes are saved with it).
    Undo: set_history_end."""
    await _require("style_apply")
    ids = image_ids or ([engine.image_id] if engine.image_id is not None else None)
    if not ids:
        raise ToolError("give image_ids, or open a photo first")
    return await _call("style_apply", name=name, imgids=ids, save=save_first)


@mcp.tool()
async def delete_style(name: str) -> dict:
    """Delete a style from darktable."""
    await _require("style_delete")
    return await _call("style_delete", name=name)


@mcp.tool()
async def paste_edit(from_image_id: int, image_ids: list[int] | None = None, mode: str = "append",
                     modules: list | None = None, module_order: bool = False,
                     save_first: bool = False) -> dict:
    """Copy one photo's saved edit onto others (default: the open photo), as
    darktable's copy and paste: all of it, or only modules (names or
    {"operation", "instance"}, as selective copy). mode "append" adds to
    each edit, "overwrite" replaces it. The result is saved; a target with
    unsaved changes here is refused unless save_first. module_order also
    copies the module order."""
    await _require("history_paste")
    ids = image_ids or ([engine.image_id] if engine.image_id is not None else None)
    if not ids:
        raise ToolError("give image_ids, or open a photo first")
    params: dict[str, Any] = {"from": from_image_id, "imgids": ids, "mode": mode,
                              "module_order": module_order, "save": save_first}
    if modules:
        params["modules"] = modules
    return await _call("history_paste", **params)


# ── sensor dust ──────────────────────────────────────────────────────────────

def _dust_module():
    try:
        from . import dust
    except ImportError as exc:
        raise ToolError(f"the dust tools need numpy, opencv and rawpy: "
                        f"pip install 'darktable-api-mcp[dust]' ({exc})")
    return dust


async def _roll_frames(info: dict) -> list[tuple[int, Path]]:
    """The photo's film roll: frames of the same sensor size (one camera), one
    per raw file (duplicates share it)."""
    rolls = (await _call("film_rolls"))["film_rolls"]
    roll = next((r for r in rolls if r["folder"] == info["folder"]), None)
    if roll is None:
        raise ToolError(f"no film roll for {info['folder']}")
    frames, seen, offset = [], set(), 0
    while True:
        page = await _call("images_list", film_id=roll["id"], rating="all", offset=offset, limit=1000)
        for im in page["images"]:
            p = Path(im["folder"]) / im["filename"]
            if (im["width"], im["height"]) == (info["width"], info["height"]) and p not in seen:
                seen.add(p)
                frames.append((im["id"], p))
        offset += 1000
        if offset >= page["total"]:
            return frames


async def _dust_analysis(image_id: int | None, rebuild_map: bool = False) -> dict:
    """What find_dust_spots and heal_dust_spots need for one photo. Opens it
    (joining an edit already open) so retouch circles include unsaved ones."""
    dust = _dust_module()
    await _require("coords", "retouch_list")
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("give image_id, or open a photo first")
    info = (await _call("image_info", imgid=image_id))["image"]
    raw_path = Path(info["folder"]) / info["filename"]
    frames = await _roll_frames(info)
    loop = asyncio.get_running_loop()
    try:
        dust_map = await loop.run_in_executor(None, lambda: dust.build_map(
            frames, f"{info['width']}x{info['height']}", f"roll_{Path(info['folder']).name}", force=rebuild_map))
        lum, geom = await loop.run_in_executor(None, dust.render_sensor, raw_path)
    except Exception as exc:
        raise ToolError(f"dust detection failed: {exc}")
    if (geom["half_width"], geom["half_height"]) != (dust_map["geometry"]["half_width"],
                                                    dust_map["geometry"]["half_height"]):
        raise ToolError("this photo's sensor size differs from its roll's dust map")
    try:
        await engine.open(image_id)
        rl = await engine.edit(image_id, "retouch_list")
    except EngineError as exc:
        raise _err(exc)
    circles = []
    for c in rl["spots"]:
        if c.get("type") == "circle" and c.get("active"):
            x, y, r = dust.half_from_raw(c["x"], c["y"], c["r"], geom)
            circles.append({"x": x, "y": y, "r": r})
    spots = dust.spots_in_frame(lum, dust_map)
    for s in spots:
        s["already_retouched"] = any(((s["x"] - c["x"]) ** 2 + (s["y"] - c["y"]) ** 2) ** 0.5 <= c["r"]
                                     for c in circles)
        s["raw"] = dust.raw_norm(s["x"], s["y"], geom)
    # where each spot is on the photo as rendered (lens, rotation, crop...)
    if spots:
        try:
            pts = (await engine.edit(image_id, "coords", points=[[s["raw"]["raw_x"], s["raw"]["raw_y"]] for s in spots],
                                     **{"from": "raw", "to": "image"}))["points"]
        except EngineError as exc:
            raise _err(exc)
        for s, (u, v) in zip(spots, pts):
            s["on_photo"] = [round(u, 4), round(v, 4)] if 0 <= u <= 1 and 0 <= v <= 1 else None
    return {"image_id": image_id, "info": info, "dust_map": dust_map, "lum": lum, "geom": geom,
            "circles": circles, "spots": spots}


def _dust_spot_json(s: dict, geom: dict) -> dict:
    return {"id": s["id"], "status": s["status"], "already_retouched": s["already_retouched"],
            "darkening_pct": round(100 * s["depth"], 1), "contrast_vs_background": s["ratio"],
            "background": s["background"], "diameter_px": int(round(4 * s["r"])),
            "on_photo": s.get("on_photo"), "raw_x": s["raw"]["raw_x"], "raw_y": s["raw"]["raw_y"],
            "found_in_roll_frames": s["roll_frames"], "size": s.get("size", "small")}


@mcp.tool(structured_output=False)
async def find_dust_spots(image_id: int | None = None, rebuild_map: bool = False) -> list:
    """Find sensor dust in a photo (default: the open one). Read-only.

    Dust sits at the same sensor position in every frame, so this maps the
    soft dark round blobs that recur across the photo's film roll (cached; the
    first call for a roll reads every raw file, ~0.4 s each), small specks
    and large soft blobs (~50-300 px), then measures each mapped spot in this
    photo: dust shows against smooth, bright-ish backgrounds (sky), more at
    small apertures. Blobs seen in only this photo aren't mapped (one photo
    can't tell them from scene content): look at the sky yourself too.

    Returns a summary and a sheet of numbered crops (sensor orientation,
    contrast-stretched; green = visible here, red = hidden). Per spot: status
    ("obvious": repair it; "visible": seen through some texture; "hidden":
    texture or an edge hides it, leave it), already_retouched (an active
    retouch circle covers it, unsaved ones included), darkening %,
    contrast_vs_background, background, diameter_px (full size), on_photo
    (x, y as fractions of the photo as rendered; null if cropped out: use it
    for render_preview(zoom=1, center_x, center_y)), and in how many frames of
    the roll it recurs, size ("small" or "large"; a large one in a cloudy
    sky may be rated obvious where it hardly shows: check the sheet). Finds
    the obvious dust, not every speck. Opens the photo (joins its edit).
    heal_dust_spots repairs them."""
    from mcp.server.fastmcp import Image as McpImage
    a = await _dust_analysis(image_id, rebuild_map)
    dust = _dust_module()
    out = [_dust_spot_json(s, a["geom"]) for s in a["spots"]]
    m = a["dust_map"]
    summary = {
        "image_id": a["image_id"], "file": a["info"]["filename"],
        "obvious": [s["id"] for s in out if s["status"] == "obvious"],
        "visible": [s["id"] for s in out if s["status"] == "visible"],
        "hidden": [s["id"] for s in out if s["status"] == "hidden"],
        "already_retouched": [s["id"] for s in out if s["already_retouched"]],
        "spots": out,
        "dust_map": {"roll": a["info"]["folder"], "frames_scanned": m["frames"],
                     "dust_positions": len(m["dust"]), "recurrence_threshold_frames": m["threshold_frames"],
                     "unreadable_frames": [s["file"] for s in m["skipped"]]},
    }
    if m["frames"] < m["threshold_frames"]:
        summary["dust_map"]["note"] = (
            f"only {m['frames']} readable raw files in the roll; dust must recur in at least "
            f"{m['threshold_frames']:g} to be mapped, so this map can't find any. Check this photo "
            "by eye (render_preview zoom=1 on the sky) instead.")
    return [json.dumps(summary), McpImage(data=dust.crop_sheet(a["lum"], a["spots"]), format="png")]


async def _spot_crops(image_id: int, plan: list[dict], size: int = 240) -> dict[int, tuple]:
    """100% renders around each planned spot: (luminance crop, spot x, y in it)."""
    import cv2
    import numpy as np
    out = {}
    for p in plan:
        u, v = p["spot"]["on_photo"]
        r = await engine.render(image_id, size, size, False, {"zoom": 1.0, "center_x": u, "center_y": v})
        if r is None:
            continue
        data, info = r
        reg = info["region"]
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).astype(np.float32) / 255.0
        lum = im @ np.array([0.0722, 0.7152, 0.2126], np.float32)    # BGR
        h, w = lum.shape
        out[p["spot"]["id"]] = (lum, (u - reg["left"]) / (reg["right"] - reg["left"]) * w,
                                (v - reg["top"]) / (reg["bottom"] - reg["top"]) * h)
    return out


@mcp.tool(structured_output=False)
async def heal_dust_spots(image_id: int | None = None, spots: list[int] | None = None,
                          include_visible: bool = False, dry_run: bool = False) -> list:
    """Heal sensor dust in a photo (default: the open one) with darktable's
    retouch module.

    By default heals the spots find_dust_spots rates "obvious" that no active
    retouch circle covers yet; include_visible adds the "visible" ones, and
    spots=[ids] picks specific ones. Each gets a heal circle about twice the
    dust's radius, copying from a nearby patch chosen to be smooth and to
    match the brightness around it. All are added as one history step
    (undo: set_history_end). Not saved until save.

    Checks the result at 100% around each spot before and after and returns
    the darkening per spot before/after (healed when it mostly disappears),
    plus a before/after sheet. dry_run=True only returns the plan."""
    from mcp.server.fastmcp import Image as McpImage
    import cv2
    import numpy as np
    await _require("retouch_heal", "render.zoom")
    a = await _dust_analysis(image_id)
    dust = _dust_module()
    geom, lum, image_id = a["geom"], a["lum"], a["image_id"]
    if spots:
        targets = [s for s in a["spots"] if s["id"] in set(spots)]
    else:
        wanted = {"obvious", "visible"} if include_visible else {"obvious"}
        targets = [s for s in a["spots"] if s["status"] in wanted and not s["already_retouched"]]
    skipped = [{"id": s["id"], "why": "already retouched"} for s in a["spots"]
               if s["already_retouched"] and s not in targets]
    avoid = ([(d["x"], d["y"], d["r"]) for d in a["dust_map"]["dust"]]
             + [(c["x"], c["y"], c["r"] / 2.5) for c in a["circles"]])
    plan = []
    for s in targets:
        if s.get("on_photo") is None:
            skipped.append({"id": s["id"], "why": "outside the photo as cropped"})
            continue
        R = dust.heal_radius(s["r"], s.get("size", "small"))
        src = dust.choose_source(lum, s["x"], s["y"], R, [v for v in avoid if (v[0], v[1]) != (s["x"], s["y"])])
        if src is None:
            skipped.append({"id": s["id"], "why": "no clean source patch nearby"})
            continue
        plan.append({"spot": s, "R": R, "source": src, "spec": dust.heal_spec(s, src, R, geom)})
    summary = {"image_id": image_id, "file": a["info"]["filename"],
               "plan": [{"id": p["spot"]["id"], "status": p["spot"]["status"],
                         "heal_diameter_px": int(round(4 * p["R"])),
                         "source_offset_px": [int(round(2 * (p["source"]["x"] - p["spot"]["x"]))),
                                              int(round(2 * (p["source"]["y"] - p["spot"]["y"])))],
                         "source_brightness_diff_pct": p["source"]["brightness_diff_pct"],
                         "circle": p["spec"]} for p in plan],
               "skipped": skipped}
    if not plan:
        summary["note"] = "nothing to heal" + ("" if a["spots"] else ": no dust mapped for this roll")
        return [json.dumps(summary)]
    if dry_run:
        return [json.dumps(summary),
                McpImage(data=dust.crop_sheet(lum, [p["spot"] for p in plan]), format="png")]

    try:
        before = await _spot_crops(image_id, plan)
        r = await engine.edit(image_id, "retouch_heal", spots=[p["spec"] for p in plan])
        after = await _spot_crops(image_id, plan)
    except EngineError as exc:
        raise _err(exc)
    summary.update(added=r["added"], history_end=r["history_end"])
    checks, rows = [], []
    for p in plan:
        sid = p["spot"]["id"]
        if sid not in before or sid not in after:
            checks.append({"id": sid, "result": "not checked (render overtaken)"})
            continue
        (lb, x, y), (la, _, _) = before[sid], after[sid]
        rr = max(6.0, 2 * p["spot"]["r"])            # full-size pixels: a 100% render
        db, da = dust.darkening_at(lb, x, y, rr), dust.darkening_at(la, x, y, rr)
        checks.append({"id": sid, "darkening_before_pct": round(100 * db, 1),
                       "darkening_after_pct": round(100 * da, 1), "healed": da <= max(0.02, 0.4 * db)})
        pair = []
        for img in (lb, la):
            c = np.clip((img - img.mean()) / (6 * img.std() + 1e-6) + 0.5, 0, 1)
            t = cv2.cvtColor((c * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            cv2.putText(t, f"#{sid}", (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 200, 60), 1)
            pair.append(t)
        rows.append(np.hstack(pair))
    summary["check"] = checks
    out = [json.dumps(summary)]
    if rows:
        width = max(r.shape[1] for r in rows)
        rows = [np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0))) for r in rows]
        ok, png = cv2.imencode(".png", np.vstack(rows))
        if ok:
            out.append(McpImage(data=png.tobytes(), format="png"))
    return out


@mcp.tool()
async def check_sensor_clipping(image_id: int | None = None) -> dict:
    """Whether the sensor really clipped: counts photosites at the camera's
    white level in the raw file, per color. A render's histogram can't tell
    that (white balance, exposure and the tone mapper move it): highlights
    clipped on the sensor are gone and can only be reconstructed
    (highlights module), while highlights merely bright in the render are
    recoverable with the tone mapper, exposure or tone equalizer. Also gives
    each color's headroom: how far its brightest 0.1% sits below clipping,
    in EV. Needs pip install 'darktable-api-mcp[dust]' (rawpy)."""
    import math
    try:
        import numpy as np
        import rawpy
    except ImportError:
        raise ToolError("needs rawpy: pip install 'darktable-api-mcp[dust]'")
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("give image_id, or open a photo first")
    info = (await _call("image_info", imgid=image_id))["image"]
    path = str(Path(info["folder"]) / info["filename"])

    def measure() -> dict:
        with rawpy.imread(path) as raw:
            data = raw.raw_image_visible
            colors = raw.raw_colors_visible
            desc = raw.color_desc.decode()
            white = raw.camera_white_level_per_channel or [raw.white_level] * 4
            black = raw.black_level_per_channel or [0] * 4
            out: dict[str, dict] = {}
            for idx in range(len(desc)):
                name = desc[idx].lower()
                if name not in ("r", "g", "b"):
                    continue
                sites = data[colors == idx]
                c = out.setdefault(name, {"clipped": 0, "total": 0, "top": []})
                c["clipped"] += int(np.count_nonzero(sites >= white[idx] * 0.99))
                c["total"] += int(sites.size)
                c["top"].append((float(np.percentile(sites, 99.9)) - black[idx])
                                / max(1, white[idx] - black[idx]))
        res = {}
        for ch, c in out.items():
            top = max(c["top"])
            res[ch] = {"clipped_pct": round(100 * c["clipped"] / c["total"], 4) if c["total"] else 0.0,
                       "headroom_ev": round(-math.log2(top), 2) if top > 0 else None}
        return res

    try:
        channels = await asyncio.to_thread(measure)
    except Exception as exc:
        raise ToolError(f"couldn't read {path} as a raw file: {exc}")
    worst = max(v["clipped_pct"] for v in channels.values())
    clipped = [ch for ch, v in channels.items() if v["clipped_pct"] >= 0.01]
    return {"image_id": image_id, "file": path, "channels": channels, "sensor_clipped": bool(clipped),
            "verdict": (f"sensor saturated in {', '.join(clipped)} ({worst:.2f}% of photosites at worst): that "
                        "detail wasn't recorded; highlight reconstruction can only fill it in"
                        if clipped else
                        "no significant sensor saturation: bright areas in the render are recoverable with "
                        "the tone mapper, exposure or tone equalizer")}


# ── background jobs ──────────────────────────────────────────────────────────

async def _run_job(start, wait: bool, timeout_s: float) -> dict:
    """Starts a job (start() returns the engine's reply) and, with wait,
    waits for its "job" event."""
    done: dict = {}
    finished = asyncio.Event()

    def listener(ev: dict) -> None:
        if ev.get("type") == "job" and ev.get("job") == done.get("job"):
            done["event"] = ev
            finished.set()

    engine.add_listener(listener)          # before starting: it can end fast
    try:
        j = await start()
        done["job"] = j["job"]
        if not wait or j.get("state") != "running":
            return j
        if "event" not in done:
            try:
                await asyncio.wait_for(finished.wait(), timeout_s)
            except asyncio.TimeoutError:
                return {**await _call("job_status", job=j["job"]),
                        "note": "still running: job_status(job) later, or cancel_job(job)"}
        return done["event"]
    finally:
        engine.remove_listener(listener)


@mcp.tool()
async def ai_denoise(image_id: int | None = None, strength: float | None = None, wait: bool = True,
                     timeout_s: int = 600) -> dict:
    """Denoise a raw photo with darktable's AI raw denoise (neural restore,
    RawNIND): the model runs on the sensor data and writes a new DNG beside
    the original (darktable's output pattern, by default
    <name>_restore.dng), imported into the library in the photo's group with
    its rating, labels, tags and metadata. The new photo starts unedited:
    open_photo(new_imgid) and edit that one (no further denoise needed,
    usually). strength 0-1 blends the original and the denoised raw
    (default: darktable's setting, else 1).

    Runs as a background job (seconds with Apple's Neural Engine, can be
    minutes on CPU): wait=True waits for it and returns the result
    (new_imgid, file); wait=False returns the job at once (job_status,
    cancel_job). Needs darktable built with AI and AI enabled."""
    await _require("ai_denoise")
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("give image_id, or open a photo first")
    params: dict[str, Any] = {"imgid": image_id}
    if strength is not None:
        params["strength"] = max(0.0, min(strength, 1.0))
    return await _run_job(lambda: _call("ai_denoise", **params), wait, timeout_s)


# ── pickers and auto buttons (darktable's window) ────────────────────────────

@mcp.tool()
async def list_pickers(operation: str, instance: int = 0) -> dict:
    """A module's color pickers and auto buttons, as darktable's darkroom
    shows them: name (for use_picker; e.g. exposure "exposure", agx
    "exposure range/auto tune levels", channelmixerrgb "picker" (white
    balance from an area), colorbalancergb "white fulcrum", toneequal "mask
    exposure compensation" (its wand), rgblevels "auto levels") and kind
    (picker or button). Only for the photo in darktable's darkroom, with
    darktable's window serving the library (open_darkroom_photo)."""
    await _require("picker_list")
    return await _edit(None, "picker_list", operation=operation, instance=instance)


@mcp.tool()
async def use_picker(operation: str, control: str, box: dict | None = None, point: list[float] | None = None,
                     instance: int = 0, wait: bool = True, timeout_s: float = 60) -> dict:
    """Use a module's picker or auto button (name from list_pickers) on the
    photo in darktable's darkroom, exactly as clicking it there: darktable
    samples the area and sets the module (e.g. exposure from a midtone area,
    white balance from a neutral area, AgX's levels from the whole photo).
    box {left, top, right, bottom} or point [x, y]: fractions 0-1 of the
    photo as shown; without either, darktable's default area (nearly the
    whole photo). Buttons take no area. Returns which settings changed
    (result.changed). One history step in darktable's history. Runs as a
    job that ends when darktable has applied it (usually well under a
    second; the first use may wait for darktable to load the photo)."""
    await _require("picker_apply")
    params: dict[str, Any] = {"operation": operation, "instance": instance, "control": control}
    if box is not None:
        params["box"] = box
    if point is not None:
        params["point"] = point
    return await _run_job(lambda: _edit(None, "picker_apply", **params), wait, timeout_s)


@mcp.tool()
async def job_status(job: int) -> dict:
    """A background job (ai_denoise, use_picker): state (running, done,
    failed, cancelled), progress (when known), and its result (ai_denoise:
    new_imgid, file; use_picker: result.changed) or error."""
    await _require("job_status")
    return await _call("job_status", job=job)


@mcp.tool()
async def list_jobs() -> dict:
    """The server's background jobs, running and ended."""
    await _require("job_list")
    return await _call("job_list")


@mcp.tool()
async def cancel_job(job: int) -> dict:
    """Cancel a running background job; it ends "cancelled" and leaves no
    file. (In darktable's window the computation stops; headless it runs to
    its end and the result is discarded.)"""
    await _require("job_cancel")
    return await _call("job_cancel", job=job)


# ── sharing the library with darktable's GUI ─────────────────────────────────

@mcp.tool()
async def library_status() -> dict:
    """Who serves the library ("server": "gui" for darktable's window,
    "engine" for the headless engine), whether the engine has released it to
    darktable's GUI ("released", with the GUI's process id), darkroom_imgid
    (the photo open in darktable's darkroom, 0 if none; gui only), the
    current photo and whether it has unsaved changes."""
    try:
        return await engine.library("library_status")
    except EngineError as exc:
        raise _err(exc)


@mcp.tool()
async def release_library() -> dict:
    """Close the library so darktable's GUI can open it. Unsaved changes of
    the open photo are kept and restored by acquire_library if the photo
    wasn't changed in the GUI meanwhile."""
    try:
        return await engine.library("library_release")
    except EngineError as exc:
        raise _err(exc)


@mcp.tool()
async def acquire_library() -> dict:
    """Take the library back after release_library (refused while darktable's
    GUI still has it open)."""
    try:
        return await engine.library("library_acquire")
    except EngineError as exc:
        raise _err(exc)


@mcp.tool()
async def takeover_library(confirm: bool = False) -> dict:
    """Ask the darktable GUI that has the library open to quit the normal way
    (it saves its edits), wait, then take the library back. Never kills it.
    This closes the user's darktable window: ask first; pass confirm=True."""
    if not confirm:
        raise ToolError("takeover quits the user's darktable: ask, then pass confirm=True")
    try:
        return await engine.takeover()
    except EngineError as exc:
        raise _err(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
