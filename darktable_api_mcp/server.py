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
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .engine import EngineError, engine

INSTRUCTIONS = """\
Edits photos in a darktable library through darktable-api (a headless
darktable). Flow: open_photo(id) -> get_module (setting names, ranges,
dropdown values) -> set_module, checking render_preview -> save. Edits stay
in memory until save; discard_changes reopens the saved state. "Photo 36"
means library id 36. One photo is current at a time (the last open_photo).

Scene-referred order: exposure (subject midtones), white balance in color
calibration (channelmixerrgb), one tone mapper (agx; sigmoid/filmicrgb on
older edits; never two), then toneequal and colorbalancergb.
Black and white: add_module_instance("channelmixerrgb") (new, not a copy),
apply_preset a "monochrome | ..." preset to it, keep the first instance for
white balance; for creative B&W move_module it after the tone mapper.
When the user asks what you changed, use compare_before_after (mark_before
starts a new "before"). For choices (crops, filters, looks) use
propose_alternatives. Measure with measure_photo / render_preview(zoom=...);
don't write analysis scripts or install packages.
Removing objects: ai_fill (an AI model fills a drawn shape, or an
add_ai_mask outline via like_mask_of, from its surroundings; a composite
instance, non-destructive). Small spots: retouch_spots.

The engine is shared with the user's web app and darktable's window:
open_photo joins an edit already open there (unsaved changes included),
your changes show up live, and save saves the whole edit. If darktable's
window serves the library, its darkroom photo (library_status:
darkroom_imgid; open_darkroom_photo) is usually "this photo"; pickers
(use_picker) work only on it. While the library is released to darktable's
GUI, only library_status, acquire_library and takeover_library work. Tell
the user what you are doing."""

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


# methods that change an edit: the first one on a photo marks compare_before_after's "before"
_EDIT_METHODS = {"module_set", "module_enable", "preset_apply", "blend_set", "curve_set", "geometry_set",
                 "mask_add", "mask_ai", "mask_remove", "retouch_heal", "retouch_add", "retouch_set",
                 "retouch_remove", "module_add", "module_remove", "module_move", "module_rename", "ai_fill"}
_before: dict[int, int] = {}    # photo -> history step before your first edit of it


async def _note_before(image_id: int) -> None:
    if image_id not in _before:
        _before[image_id] = (await engine.edit(image_id, "history_list"))["history_end"]


async def _edit(image_id: int | None, method: str, **params) -> dict:
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    try:
        if method in _EDIT_METHODS:
            await _note_before(image_id)
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
async def make_thumbnails(image_ids: list[int] | None = None, film_roll_id: int | None = None,
                          whole_library: bool = False, size: int = 720, outdated_only: bool = True,
                          wait: bool = True, timeout_s: int = 1800) -> dict:
    """Make darktable thumbnails in bulk (as darktable-generate-cache does):
    rendered with each photo's saved edit and written to darktable's disk
    cache, so darktable's lighttable, the web app's grid, get_thumbnail and
    photo_sheet show them at once. Give image_ids, a film_roll_id, or
    whole_library=True. outdated_only (default) skips photos whose
    thumbnails are on disk and match their edit. size: the largest
    thumbnail needed (pixels).

    A background job (about 0.2-1 s a photo): wait=True waits for it
    (result: generated, skipped, failed); wait=False returns the job at
    once (job_status, cancel_job)."""
    await _require("thumbnails")
    params: dict[str, Any] = {"size": max(64, min(size, 4096)), "outdated_only": outdated_only}
    if image_ids:
        params["imgids"] = image_ids
    elif film_roll_id is not None:
        params["film_id"] = film_roll_id
    elif not whole_library:
        raise ToolError("give image_ids, film_roll_id, or whole_library=True")
    return await _run_job(lambda: _call("thumbnails", **params), wait, timeout_s)


@mcp.tool()
async def import_photos(paths: list[str], copy: bool = False, recursive: bool = False,
                        jobcode: str | None = None, skip_existing: bool = True, wait: bool = True,
                        timeout_s: int = 1800) -> dict:
    """Import photos into the darktable library, as darktable's import does.
    paths: files or folders on the machine darktable runs on (a folder
    takes the photos in it; recursive=True also its subfolders, e.g. a
    card's DCIM).

    copy=False adds the files where they are ("add to library"): for photos
    already in their final place. copy=True copies them into the folders
    darktable's import settings name ("copy & import", by default
    ~/Pictures/Darktable/<date>_<jobcode>) and renames them by its file
    name pattern: for a memory card or a download folder. jobcode names
    that folder (default: darktable's last one). Ask the user which, and
    for a jobcode, when it isn't clear.

    skip_existing (default) leaves out photos already in the library (same
    file, the id darktable's copy & import stores, or the same file name
    and capture time), so importing a card again brings only what's new.
    darktable's "ignore non-raws" preference is followed.

    A background job: wait=True waits for it; the result lists imported
    (image ids and files), skipped (with the photo already there) and
    failed (with why), and the film_ids they went into. Afterwards
    make_thumbnails(film_roll_id=...) prepares their thumbnails."""
    await _require("import")
    if not paths:
        raise ToolError("give paths: the files or folders to import")
    params: dict[str, Any] = {"paths": [os.path.expanduser(p) for p in paths], "copy": copy,
                              "recursive": recursive, "skip_existing": skip_existing}
    if jobcode:
        params["jobcode"] = jobcode
    return await _run_job(lambda: _call("import", **params), wait, timeout_s)


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


def _sheet_line(img: dict, info: dict, md: dict) -> str:
    ex = info.get("exif") or {}
    parts = [f"id {img['id']}", img.get("filename", "")]
    if ex.get("taken"):
        parts.append(str(ex["taken"]))
    cam = " ".join(x for x in (ex.get("model"), ex.get("lens")) if x)
    if cam:
        parts.append(cam)
    loc = md.get("location")
    if loc:
        parts.append(f"at {loc['latitude']:.4f}, {loc['longitude']:.4f}")
    if md.get("metadata", {}).get("title"):
        parts.append(f"title \"{md['metadata']['title']}\"")
    parts.append("tags: " + (", ".join(md.get("tags") or []) or "none"))
    return " · ".join(parts)


@mcp.tool(structured_output=False)
async def photo_sheet(image_ids: list[int] | None = None, film_roll_id: int | None = None,
                      rating: str = "visible", untagged_only: bool = False, offset: int = 0,
                      limit: int = 12, size: int = 384) -> list:
    """Look at many photos at once, for tagging them by content (or culling,
    finding a subject): each photo's thumbnail with a line giving its id,
    filename, date, camera and lens, location, title and current tags. Give
    image_ids, or page through a film roll (film_roll_id; omit for the whole
    library) with offset/limit (up to 24 a page); the first line says where
    the next page starts. untagged_only skips photos that already have tags
    of their own (darktable's own "darktable|..." tags don't count).

    Auto-tagging: list_tags first and reuse the library's existing tags and
    hierarchy (e.g. "places|usa|washington", "animals|mammals|prairie dog");
    make_thumbnails for the roll first makes the pages fast; then
    tag_photos with what you see on each page."""
    await _require("image_metadata")
    limit = max(1, min(limit, 24))
    size = max(128, min(size, 768))
    imgs: list[dict] = []
    next_offset = None
    total = None
    if image_ids:
        for i in image_ids[offset:offset + limit]:
            imgs.append((await _call("image_info", imgid=i))["image"])
        total = len(image_ids)
        next_offset = offset + limit if offset + limit < total else None
    else:
        pos = offset
        while len(imgs) < limit:
            page = await _call("images_list", offset=pos, limit=100, **_filters(film_roll_id, rating, None))
            total = page["total"]
            for img in page["images"]:
                pos += 1
                if untagged_only and (await _call("image_metadata", imgid=img["id"]))["tags"]:
                    continue
                imgs.append(img)
                if len(imgs) == limit:
                    break
            if not page["images"] or pos >= total:
                break
        next_offset = pos if pos < (total or 0) else None
    out: list = [f"{len(imgs)} photos (of {total}); "
                 + (f"next page: offset={next_offset}" if next_offset is not None else "no more pages")]
    for img in imgs:
        info = await _call("image_info", imgid=img["id"])
        md = await _call("image_metadata", imgid=img["id"])
        out.append(_sheet_line(img, info, md))
        try:
            out.append(Image(data=await engine.thumbnail(img["id"], size), format="jpeg"))
        except EngineError as exc:
            out.append(f"(no thumbnail: {exc})")
    return out


@mcp.tool()
async def tag_photos(tags: dict[str, list[str]], detach: dict[str, list[str]] | None = None) -> dict:
    """Tag many photos, each with its own tags: {"<image id>": ["tag",
    "places|usa|washington|mt rainier", ...]}; detach takes the same form
    to remove tags. New tags are created ("|" for the hierarchy). Written to
    the library and sidecars at once, as darktable's tagging module does."""
    await _require("set_tags")
    detach = detach or {}
    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for key in sorted(set(tags) | set(detach), key=lambda k: int(k) if str(k).isdigit() else 0):
        try:
            imgid = int(key)
        except ValueError:
            failed[key] = "not an image id"
            continue
        params: dict[str, Any] = {"imgids": [imgid]}
        if tags.get(key):
            params["attach"] = tags[key]
        if detach.get(key):
            params["detach"] = detach[key]
        if len(params) == 1:
            continue
        try:
            await engine.call("set_tags", **params)
            done[key] = (await engine.call("image_metadata", imgid=imgid))["tags"]
        except EngineError as exc:
            failed[key] = str(exc)
    return {"tagged": done, **({"failed": failed} if failed else {})}


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
        if exc.is_("unsaved_changes", "unsaved"):
            raise ToolError("the photo has unsaved changes and the duplicate gets the saved edit: save, or "
                            "pass save_first=True (both save the whole shared edit: ask the user)")
        raise _err(exc)


@mcp.tool()
async def remove_photos(image_ids: list[int], confirm: bool = False) -> dict:
    """Remove photos from the darktable library, as the lighttable's
    "remove" does: each photo's library entry and edit are deleted; the raw
    file on disk stays, and so do other versions (duplicates) of it. E.g.
    a duplicate made for an experiment. Refused (nothing removed) for the
    photo open in darktable's darkroom, a photo with unsaved changes, or one
    used as an overlay. Can't be undone: say which photos (ids and file
    names, with list_images / image_info) and ask the user, then pass
    confirm=True. Returns the removed ids."""
    await _require("image_remove")
    if not confirm:
        raise ToolError("removing photos can't be undone: tell the user which ones (id, file name, version), "
                        "ask, then pass confirm=True")
    try:
        r = await engine.call("image_remove", imgids=image_ids)
    except EngineError as exc:
        raise _err(exc)
    if engine.image_id in r.get("removed", []):
        engine.image_id = None
    return r


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
           horizon). Positive turns the photo counter-clockwise (its top
           moves left), negative clockwise (its top moves right). The
           automatic crop is refitted so no blank corners show; the
           frame gets smaller, and a crop module crop keeps its fractions
           of that new frame, so its framing shifts and tightens: straighten
           before cropping, or check the framing and crop again after.
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


# ── comparing: before/after, alternatives ────────────────────────────────────

VARIANTS_DIR = Path(os.environ.get("DTAPI_VARIANTS_DIR") or Path.home() / ".cache/darktable-api-mcp/variants")
_proposals: dict[int, list[dict]] = {}   # photo -> the alternatives last proposed


async def _share(kind: str, image_id: int, data: dict) -> bool:
    """Tell the user's other apps (the web app) to show something; False if
    this darktable can't relay it."""
    try:
        if not await engine.supports("share"):
            return False
        await engine.call("share", kind=kind, imgid=image_id, data=data)
        return True
    except EngineError:
        return False


def _step_label(items: list[dict], step: int) -> str:
    if step == 0:
        return "the original"
    it = items[step - 1] if step - 1 < len(items) else {}
    return f"step {step} ({it.get('label') or it.get('operation', '?')})"


@mcp.tool()
async def mark_before() -> dict:
    """Make the open photo's current step the "before" of
    compare_before_after. Call it when you start on a new request, so the
    comparison shows what that request changed; otherwise "before" is the
    step before your first edit of the photo."""
    image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    _before[image_id] = (await _edit(image_id, "history_list"))["history_end"]
    return {"before_step": _before[image_id]}


@mcp.tool(structured_output=False)
async def compare_before_after(before_step: int | None = None, show_user: bool = True, size: int = 900) -> list:
    """Before and after of the open photo: both renders (before first) for
    you to judge, and with show_user the comparison in front of the user:
    the web app's photo page shows a before/after slider, and if the photo
    is in darktable's darkroom, darktable's snapshots panel gets a snapshot
    of the before step, shown split against the edit (drag the line to
    compare; click the snapshot to hide it). Nothing is undone.
    before_step: history step (0 = the original, as get_history counts);
    default: the step before your first edit of this photo (or the one
    mark_before set)."""
    image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    await _require("render.history_end")
    hist = await _edit(image_id, "history_list")
    end, items = hist["history_end"], hist["items"]
    step = before_step if before_step is not None else _before.get(image_id, 0)
    if not 0 <= step <= len(items):
        raise ToolError(f"before_step must be 0..{len(items)}")
    if step == end:
        raise ToolError(f"before and after are the same step ({end}): nothing to compare; "
                        "give before_step (0 = the original)")
    size = max(64, min(size, 2560))
    try:
        before = await engine.render(image_id, size, size, False, {"history_end": step})
        after = await engine.render(image_id, size, size, False)
    except EngineError as exc:
        raise _err(exc)
    if before is None or after is None:
        raise ToolError("overtaken by a newer render request: try again")
    shown = []
    if show_user:
        if await _share("compare", image_id, {"history_end": step, "after_end": end,
                                              "before_label": _step_label(items, step),
                                              "after_label": _step_label(items, end)}):
            shown.append("the web app's photo page (a before/after slider)")
        try:
            st = await engine.call("library_status")
            if st.get("darkroom_imgid") == image_id and await engine.supports("darkroom_snapshot"):
                await engine.call("darkroom_snapshot", history_end=step)
                shown.append("darktable's darkroom (a snapshot of the before step, split against the edit)")
        except EngineError as exc:
            shown.append(f"not darktable's darkroom: {exc}")
    note = (f"before: {_step_label(items, step)}; after: {_step_label(items, end)} (the current edit). "
            + (f"Shown to the user in {' and '.join(shown)}." if shown else "Not shown to the user."))
    return [f"BEFORE ({_step_label(items, step)}):", Image(data=before[0], format="jpeg"),
            f"AFTER ({_step_label(items, end)}):", Image(data=after[0], format="jpeg"), note]


# the tools an alternative's changes may use, and the engine method each is
_CHANGE_TOOLS = {"set_module": "module_set", "enable_module": "module_enable", "apply_preset": "preset_apply",
                 "set_blending": "blend_set", "set_curve": "curve_set", "crop_photo": "geometry_set",
                 "rotate_photo": "geometry_set"}


def _steps(changes: list[dict]) -> list[dict]:
    """An alternative's changes (tool calls) as engine steps."""
    steps = []
    for ch in changes:
        ch = dict(ch)
        tool = ch.pop("tool", None)
        if tool not in _CHANGE_TOOLS:
            raise ToolError(f"a change's tool must be one of {', '.join(_CHANGE_TOOLS)} (got {tool!r})")
        if tool == "crop_photo":
            p: dict[str, Any] = {}
            if ch.get("remove"):
                p["crop"] = None
            elif any(k in ch for k in ("left", "top", "right", "bottom")):
                p["crop"] = {k: ch[k] for k in ("left", "top", "right", "bottom")}
            if ch.get("aspect"):
                p["aspect"] = ch["aspect"]
        elif tool == "rotate_photo":
            p = {k2: ch[k1] for k1, k2 in (("degrees", "rotate"), ("angle", "angle"), ("mirror", "flip"),
                                            ("autocrop", "autocrop")) if ch.get(k1) is not None}
        else:
            p = {"instance": 0, **ch}
        if not p:
            raise ToolError(f"a {tool} change changes nothing: {ch}")
        steps.append({"method": _CHANGE_TOOLS[tool], "params": p})
    return steps


@mcp.tool(structured_output=False)
async def propose_alternatives(alternatives: list[dict], size: int = 800, show_user: bool = True) -> list:
    """Render alternative versions of the open photo for the user to choose
    from, without changing the edit: different crops, black and white
    filters, looks, ... Each is applied to a copy of the current edit,
    rendered, and dropped. With show_user, the web app's photo page shows
    them as a strip the user can pick from (its Apply button applies one).
    Then apply_alternative(choice) applies the one the user picks, or
    keep_alternative(choice) makes it a duplicate photo.

    alternatives: 2-8 of {"label": "B&W, red filter", "changes": [...]},
    each change a tool call as {"tool": name, ...its arguments}:
      set_module (operation, values, instance), enable_module (operation,
      enabled, instance), apply_preset (operation, name, instance),
      set_blending (operation, values, instance), set_curve (operation,
      points, channel, type, instance), crop_photo (left, top, right,
      bottom, aspect, remove), rotate_photo (degrees, angle, mirror).
    e.g. {"label": "square", "changes": [{"tool": "crop_photo", "aspect": "square"}]}
    Alternatives can't add module instances: add one first if they need it
    (e.g. add_module_instance("channelmixerrgb"), enable_module(it, False),
    then each alternative enables it with its own preset or gray mix)."""
    image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    await _require("render_variants")
    if not 2 <= len(alternatives) <= 8:
        raise ToolError("give 2 to 8 alternatives")
    folder = VARIANTS_DIR / f"{image_id}-{time.strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    props = []
    for k, alt in enumerate(alternatives):
        label = str(alt.get("label") or f"alternative {k + 1}")
        props.append({"label": label, "changes": alt.get("changes") or [],
                      "steps": _steps(alt.get("changes") or []), "path": str(folder / f"{k + 1}.jpg")})
    size = max(64, min(size, 2560))
    r = await _edit(image_id, "render_variants", width=size, height=size,
                    variants=[{"label": p["label"], "path": p["path"], "steps": p["steps"]} for p in props])
    out: list = []
    for k, (p, v) in enumerate(zip(props, r["variants"])):
        if v.get("error"):
            p["error"] = v["error"]
            out.append(f"{k + 1}. {p['label']}: FAILED: {v['error']}")
        else:
            out += [f"{k + 1}. {p['label']}:", Image(data=Path(p["path"]).read_bytes(), format="jpeg")]
    _proposals[image_id] = props
    good = [p for p in props if "error" not in p]
    shown = show_user and good and await _share(
        "variants", image_id, {"variants": [{"number": k + 1, "label": p["label"], "path": p["path"],
                                             "steps": p["steps"]} for k, p in enumerate(props) if "error" not in p]})
    out.append("Nothing was applied. " + ("The user sees them in the web app's photo page. " if shown else "")
               + "apply_alternative(number or label) applies one; keep_alternative makes one a duplicate photo.")
    return out


def _proposal(image_id: int, choice: int | str) -> dict:
    props = _proposals.get(image_id)
    if not props:
        raise ToolError("no alternatives proposed for this photo: propose_alternatives first")
    for k, p in enumerate(props):
        if choice == k + 1 or (isinstance(choice, str) and (choice.strip().lower() == p["label"].lower()
                                                            or choice.strip() == str(k + 1))):
            if "error" in p:
                raise ToolError(f"that alternative failed to render: {p['error']}")
            return p
    raise ToolError("no such alternative: " + "; ".join(f"{k + 1}. {p['label']}" for k, p in enumerate(props)))


@mcp.tool()
async def apply_alternative(choice: int | str) -> dict:
    """Apply one of the alternatives propose_alternatives showed (its number
    or label) to the open photo, as history steps. Not saved until save."""
    image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open")
    p = _proposal(image_id, choice)
    for st in p["steps"]:
        await _edit(image_id, st["method"], **st["params"])
    hist = await _edit(image_id, "history_list")
    await _share("variants", image_id, {"applied": p["label"]})
    return {"applied": p["label"], "history_end": hist["history_end"]}


@mcp.tool()
async def keep_alternative(choice: int | str, save_first: bool = False) -> dict:
    """Keep one of the alternatives as a duplicate photo (a new version of
    the same raw, as darktable's duplicate), with the current edit plus
    that alternative, saved, its version name set to the label. The open
    photo is unchanged. E.g. keep a B&W version beside the color one. The
    duplicate gets the photo's saved edit: if it has unsaved changes,
    save_first=True saves them first (ask the user; they may be theirs).
    The user can compare duplicates side by side in darktable's culling
    view (X in the lighttable)."""
    image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open")
    p = _proposal(image_id, choice)
    dup = await duplicate_photo(image_id, save_first=save_first)
    new_id = dup["image"]["id"]
    for st in p["steps"]:
        await _edit(new_id, st["method"], **st["params"])
    await _edit(new_id, "save")
    try:
        if await engine.supports("set_metadata"):
            await engine.call("set_metadata", imgids=[new_id], values={"version name": p["label"]})
    except EngineError:
        pass
    return {"kept": p["label"], "duplicate_id": new_id,
            "note": "the open photo is unchanged; open_photo(duplicate_id) to edit the duplicate"}


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
        if exc.is_("unsaved_changes", "unsaved"):
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
    _before.pop(engine.image_id, None)
    return await open_photo(engine.image_id, discard_unsaved=True)


@mcp.tool()
async def start_over(confirm: bool = False) -> dict:
    """Delete the open photo's whole edit (saved and unsaved) and apply
    darktable's defaults again (the workflow's modules and auto-apply
    presets), saved immediately. Ask the user first; pass confirm=True."""
    if not confirm:
        raise ToolError("start_over deletes the whole edit: ask the user, then pass confirm=True")
    if engine.image_id is not None:
        _before.pop(engine.image_id, None)
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


_ROMAN = ["0", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


@mcp.tool(structured_output=False)
async def zone_map(points: list[list[float]] | None = None, highlight: list[int] | None = None,
                   size: int = 800, history_step: int | None = None) -> list:
    """The open photo as darktable renders it, reduced to Ansel Adams's
    eleven print values (zones 0-X): each pixel is shown as the flat grey of
    its zone, with a zone scale underneath. Zones are steps of 10 in Lab L
    of the render (zone V = L 45-55, middle grey; 0 = L under 5, X = L 95
    and up), a print's 1:100 range from black to paper white in equal
    visual steps.

    Returns the fraction of the photo in each zone, and for points
    ([[x, y], ...] fractions of the photo) the L there and its zone as a
    decimal (7.4 = between VII and VIII). highlight: zones to mark in red
    on the map, e.g. [0, 1] to see where shadows block up, [9, 10] where
    highlights burn out. history_step as render_preview (0 = the original).
    For black and white edits; on a color photo it maps its brightness."""
    from mcp.server.fastmcp import Image as McpImage
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ToolError(f"zone_map needs numpy and opencv: pip install 'darktable-api-mcp[dust]' ({exc})")
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    size = max(128, min(size, 1600))
    region = None
    if history_step is not None:
        await _require("render.history_end")
        region = {"history_end": max(0, history_step)}
    try:
        r = await engine.render(engine.image_id, size, size, False, region)
    except EngineError as exc:
        raise _err(exc)
    if r is None:
        raise ToolError("overtaken by a newer render request")
    im = cv2.imdecode(np.frombuffer(r[0], np.uint8), cv2.IMREAD_COLOR).astype(np.float32) / 255.0
    lin = np.where(im <= 0.04045, im / 12.92, ((im + 0.055) / 1.055) ** 2.4)
    y = lin @ np.array([0.0722, 0.7152, 0.2126], np.float32)    # BGR
    lab_l = np.where(y > 216 / 24389, 116 * np.cbrt(y) - 16, y * 24389 / 27)
    zone = np.clip(np.floor(lab_l / 10 + 0.5), 0, 10).astype(np.uint8)

    def grey(z: int) -> int:    # sRGB 8-bit value of L = 10 z
        yy = ((10 * z + 16) / 116) ** 3 if z > 0.8 else 10 * z * 27 / 24389
        return int(round(255 * (12.92 * yy if yy <= 0.0031308 else 1.055 * yy ** (1 / 2.4) - 0.055)))

    lut = np.array([grey(z) for z in range(11)], np.uint8)
    out = cv2.cvtColor(lut[zone], cv2.COLOR_GRAY2BGR)
    for z in highlight or []:
        if 0 <= z <= 10:
            out[zone == z] = (40, 40, 230)
    h, w = zone.shape
    cell = max(w // 11, 24)
    scale = np.full((max(cell // 2, 28), cell * 11, 3), 255, np.uint8)
    for z in range(11):
        scale[:, z * cell:(z + 1) * cell] = lut[z]
        if z in (highlight or []):
            scale[-6:, z * cell:(z + 1) * cell] = (40, 40, 230)
        cv2.putText(scale, _ROMAN[z], (z * cell + 4, scale.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (255, 255, 255) if z < 5 else (0, 0, 0), 1, cv2.LINE_AA)
    pad = np.full((scale.shape[0], max(w, scale.shape[1]), 3), 255, np.uint8)
    pad[:, :scale.shape[1]] = scale
    body = np.full((h, pad.shape[1], 3), 255, np.uint8)
    body[:, :w] = out
    ok, png = cv2.imencode(".png", np.vstack([body, pad]))
    counts = np.bincount(zone.ravel(), minlength=11) / zone.size
    summary: dict[str, Any] = {"zones": {_ROMAN[z]: round(float(counts[z]), 4) for z in range(11)}}
    if points:
        pts = []
        for x, yv in points:
            px = min(max(int(x * w), 0), w - 1)
            py = min(max(int(yv * h), 0), h - 1)
            patch = lab_l[max(py - 2, 0):py + 3, max(px - 2, 0):px + 3]
            lv = float(patch.mean())
            pts.append({"x": x, "y": yv, "L": round(lv, 1), "zone": round(lv / 10, 1)})
        summary["points"] = pts
    return [json.dumps(summary), McpImage(data=png.tobytes(), format="png")]


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
    full, high full, high end]; "inverted": true flips a range; null
    switches a channel off. Ranges turn the parametric mask on.
    Gray (g) and R/G/B on scene-referred modules are linear: 18 is middle
    gray and each EV down halves it (9 = -1 EV, 4.5 = -2 EV, 2 = -3 EV).
    Dark shadows are a few percent, not 20-40: e.g. the darkest tones only
    {"parametric": {"g_in": {"range": [0, 0, 2, 6]}}}, below middle gray
    [0, 0, 6, 18]. The values are what reaches this module (after exposure
    and earlier modules), not what measure_photo reads from the rendered
    photo, so check a range's effect with render_preview(zoom=...) and
    narrow it if the change spreads.
    raster_source {"operation", "instance"} uses another module's
    mask (one earlier in the pipe that has a drawn or parametric mask) as
    this one's, as the raster mask menu does; null removes it;
    raster_inverted flips it. Example: denoise only the shadows (-1.5 EV
    and below, fading in from -0.5 EV):
    set_blending("denoiseprofile", {"parametric": {"g_in": {"range": [0, 0, 6, 13]}}})."""
    await _require("blend_set")
    return await _edit(None, "blend_set", operation=operation, values=values, instance=instance)


async def _photo_to_raw(points: list[list[float]]) -> tuple[list[list[float]], int, int]:
    r = await _edit(None, "coords", points=points, **{"from": "image", "to": "raw"})
    return r["points"], r["raw_width"], r["raw_height"]


async def _shape_on_raw(shape: str, x: float | None, y: float | None, radius: float,
                        radius_y: float | None, rotation: float, feather: float, compression: float,
                        points: list[list[float]] | None, width: float, hardness: float,
                        density: float) -> dict:
    """add_mask's arguments (positions and sizes on the photo as shown) as
    the engine's shape dict in raw space."""
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
        return sh
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
    return sh


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
    sh = await _shape_on_raw(shape, x, y, radius, radius_y, rotation, feather, compression, points,
                             width, hardness, density)
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
    type, combine (how it joins the shapes before it; with darktable's
    flexi masks, master and 5.8 on, its group's operator, which the user's
    masks panel can also set to screen, product or sum), inverted, and
    where it is on the photo as add_mask takes
    it: on_photo (x, y fractions of the photo as shown); circles and
    ellipses also radius (radius_y, rotation for an ellipse) and feather,
    fractions of the photo's shorter side, so a shape can be redrawn
    bigger or smaller with add_mask."""
    await _require("mask_list", "coords")
    import math
    r = await _edit(None, "mask_list", operation=operation, instance=instance)
    shapes = [s for s in r["shapes"] if "x" in s]
    if not shapes:
        return r
    # each center, and for circles and ellipses points along its radii and
    # border in raw space (the inverse of add_mask), mapped to the photo
    raw_size = await _edit(None, "coords", points=[[0.5, 0.5]], **{"from": "raw", "to": "image"})
    rw, rh = raw_size["raw_width"], raw_size["raw_height"]
    rshort = min(rw, rh)
    probes: list[list[float]] = []
    for s in shapes:
        x, y = s["x"], s["y"]
        probes.append([x, y])
        if s["type"] in ("circle", "ellipse"):
            a = math.radians(s.get("rotation", 0.0))
            ra, rb = (s["r"], s["r"]) if s["type"] == "circle" else (s["ra"], s["rb"])
            for length, (cx, cy) in ((ra, (math.cos(a), math.sin(a))), (rb, (-math.sin(a), math.cos(a))),
                                     (s.get("border", 0.0), (math.cos(a), math.sin(a)))):
                probes.append([x + length * rshort * cx / rw, y + length * rshort * cy / rh])
    img = (await _edit(None, "coords", points=probes, **{"from": "raw", "to": "image"}))["points"]
    g = await _edit(None, "geometry_get")
    W, H = g["width"], g["height"]
    short = min(W, H)
    def dist(p, q):
        return math.hypot((q[0] - p[0]) * W, (q[1] - p[1]) * H) / short
    it = iter(img)
    for s in shapes:
        c = next(it)
        s["on_photo"] = [round(c[0], 4), round(c[1], 4)]
        del s["x"], s["y"]
        if s["type"] in ("circle", "ellipse"):
            pa, pb, pf = next(it), next(it), next(it)
            # sizes as add_mask takes them, replacing the raw-space ones
            for k in ("r", "ra", "rb", "border"):
                s.pop(k, None)
            s["radius"] = round(dist(c, pa), 4)
            if s["type"] == "ellipse":
                s["radius_y"] = round(dist(c, pb), 4)
                s["rotation"] = round(math.degrees(math.atan2((pa[1] - c[1]) * H, (pa[0] - c[0]) * W)), 1)
            s["feather"] = round(dist(c, pf), 4)
    return r


@mcp.tool()
async def remove_mask(operation: str, formid: int, instance: int = 0) -> dict:
    """Take a shape (formid from list_masks) off a module's mask, as one
    history step."""
    await _require("mask_remove")
    return await _edit(None, "mask_remove", operation=operation, formid=formid, instance=instance)


# where ai_fill writes its patch TIFFs: beside the photo by default; set
# DTAPI_FILLS_DIR for a library copy, whose photos are someone's originals
FILLS_DIR = os.environ.get("DTAPI_FILLS_DIR")
DUST_FILL_NAME = "dust"     # heal_dust_spots' fill instances: "dust #3"


async def _ai_fill(image_id: int | None, shape: dict, context: float, name: str,
                   model: str = "local", prompt: str = "", upscale: bool = False,
                   online_size: int = 1024) -> dict:
    extra = {"path": str(Path(FILLS_DIR).expanduser() / "$(FILE.NAME)_fill")} if FILLS_DIR else {}
    if model == "local":
        return await _edit(image_id, "ai_fill", shape=shape, context=context, name=name, **extra)
    from . import bfl
    if model not in bfl.MODELS:
        raise ToolError(f"model: 'local' (LaMa, on this machine), 'flux-erase' or 'flux' (online), not '{model}'")
    # at Black Forest Labs: the engine writes the crop, BFL fills it, the
    # engine places the fill. FLUX works best near a megapixel
    await _require("ai_fill.external")
    import tempfile
    bfl.api_key()                               # no key: refuse before any edit
    work = Path(tempfile.mkdtemp(prefix="dtapi-flux-"))
    try:
        if not 512 <= online_size <= 2048 or online_size % 64:
            raise ToolError("online_size: 512-2048, a multiple of 64 (the crop's side sent online)")
        r = await _edit(image_id, "ai_fill", shape=shape, context=context, name=name,
                        crop_to=str(work / "crop"), size=online_size)
        inst = r["instance"]
        try:
            info = await bfl.fill(model, Path(r["image"]), Path(r["mask"]), work / "filled.png", prompt)
            out = await _edit(image_id, "ai_fill", instance=inst, filled=str(work / "filled.png"),
                              model=f"{bfl.MODELS[model][1]} (Black Forest Labs)", context=context,
                              upscale=upscale, **extra)
        except BaseException:
            # the instance crop_to set up has no patch: take it away again
            try:
                if inst:
                    await _edit(image_id, "module_remove", operation="overlay", instance=inst)
                else:
                    await _edit(image_id, "module_enable", operation="overlay", instance=0, enabled=False)
            except Exception:
                pass
            raise
        out["bfl"] = info
        return out
    except bfl.BflError as exc:
        raise ToolError(str(exc)) from None
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _member_from_listed(s: dict) -> dict:
    """A mask_list shape as a mask_add group member: its geometry (raw
    space) with combine, inverted, opacity and name; formid and active are
    dropped (a group's members are nested the same way)."""
    m = {k: v for k, v in s.items() if k not in ("formid", "active", "members")}
    if s.get("members"):
        m["members"] = [_member_from_listed(c) for c in s["members"] if c.get("active", True)]
    return m


def _pick_shapes(shapes: list[dict], wanted: list[str | int], op: str, instance: int) -> list[dict]:
    """The shapes of a mask_list reply named in `wanted`: a formid, or a
    name as listed ("circle eye") or without darktable's type word
    ("eye"), in any case; in the mask's order."""
    def keys(s: dict) -> set[str]:
        n = (s.get("name") or "").strip().lower()
        k = {str(s.get("formid")), n}
        t = (s.get("type") or "").lower()
        if t and n.startswith(t + " "):
            k.add(n[len(t) + 1:].strip())
        return k
    want = [str(w).strip().lower() for w in wanted]
    missing = [w for w, o in zip(want, wanted) if not any(w in keys(s) for s in shapes)]
    if missing:
        names = ", ".join(f"'{s.get('name')}' ({s.get('formid')})" for s in shapes) or "none"
        raise ToolError(f"no shape {', '.join(repr(str(m)) for m in missing)} on {op} (instance "
                        f"{instance}); its shapes: {names}")
    picked = [dict(s) for s in shapes if keys(s) & set(want)]
    if "combine" in picked[0]:
        # the first shape starts the mask: its operator joined it to a shape
        # that is not taken
        picked[0]["combine"] = "union"
    return picked


@mcp.tool()
async def ai_fill(shape: str = "circle", x: float | None = None, y: float | None = None,
                  radius: float = 0.05, radius_y: float | None = None, rotation: float = 0.0,
                  feather: float = 0.01, points: list[list[float]] | None = None, width: float = 0.03,
                  hardness: float = 0.5, density: float = 1.0, like_mask_of: str | None = None,
                  like_mask_instance: int = 0, like_mask_shapes: list[str | int] | None = None,
                  context: float = 0.5, name: str = "fill", model: str = "local",
                  prompt: str = "", upscale: bool = False, online_size: int = 1024) -> dict:
    """Remove an object or blemish: the area inside a shape is filled by an
    AI inpainting model (LaMa) from its surroundings. Non-destructive: a
    new "composite" module instance called `name` shows the fill, masked by
    the shape; switch it off (enable_module) or delete it
    (remove_module_instance) to undo. Its mask can be feathered with
    set_blending and edited like any mask (add_mask, list_masks,
    remove_mask). One call, a few seconds (about 6 on a Mac).

    The shape is drawn as add_mask does: positions as fractions 0-1 of the
    photo as shown, radius and feather fractions of its shorter side.
      shape "circle": x, y, radius, feather.
      shape "ellipse": x, y, radius, radius_y, rotation (degrees), feather.
      shape "path": a closed outline through points [[x, y], ...] (3 or
        more), feather.
      shape "brush": a stroke along points, width, hardness, density.
    Outline the object generously (a few pixels around it) and feather a
    little (the default is 0.01).

    like_mask_of: a module's operation name (e.g. one where add_ai_mask
    outlined the object): fill exactly the shapes of that module's mask
    (instance like_mask_instance); the shape, x, y and radius arguments are
    then ignored. Remove that helper module's mask afterwards (remove_mask)
    if it was only made for this.
    like_mask_shapes: only these shapes of that mask, by name (as
    list_masks shows it, e.g. "circle eye", or without darktable's type
    word, "eye"; any case) or formid, e.g. ["eye"] when the user drew
    "eye" and "nose" on one module and wants only the eye filled. The
    shape is used as drawn, feather included: the fill fades out across
    the feather, so a shape with a small core and a wide feather leaves
    the object partly showing.

    model: "local" (default): LaMa on this machine, free, a few seconds;
    smooth on fur, hair and fine detail. Online, at Black Forest Labs (the
    crop around the shape, about context x 2 + 1 shape sizes and nothing
    else, is uploaded; cents and 10-30 s a fill; needs the user's BFL key in
    BFL_API_KEY or ~/.config/darktable-api-mcp/bfl_api_key; use them when
    the user asks or agreed to send crops online):
      "flux-erase": FLUX Erase, made to remove an object. Best for small
        to medium objects within texture (an eye in fur: no trace). On a
        large area it re-renders with a look of its own (sharp close-up
        texture against a soft background), and the patch shows.
      "flux": FLUX.1 Fill [pro]: paints what belongs there, guided by
        prompt. Best for large areas and backgrounds with a prompt that
        describes the scene, e.g. "an empty field of tall dry grass" for
        a whole animal: it keeps the photo's pixels around the hole, so
        focus and color match. It may paint something unwanted (a zebra
        for "dry prairie grass"; on a face a new eye or nose); negatives
        such as "no eye" don't work.
    FLUX results differ from run to run: fill two or three times and
    compare to choose. A fill made online is color matched to the photo
    (what the model did to the pixels it should have kept is undone in the
    fill; patch.color_match in the reply).
    Online fills are made at online_size (default 1024, up to 2048; the
    models were trained near 1024): a crop larger than that comes back
    softer than the photo (patch.size vs patch.model_size in the reply).
    upscale=True (online only) runs such a fill through darktable's AI
    upscaler (RealPLKSR, 2x or 4x, local) before placing it: sharper, with
    detail the upscaler invents; patch.upscaled in the reply gives the
    factor, missing when no upscale model is installed. Seconds on a GPU,
    minutes on a CPU.

    context 0.25-4 (default 0.5): how much of the surroundings the model
    sees, in shape sizes. Smaller keeps texture sharper; larger (e.g. 1.0)
    suits big uniform areas such as sky. propose_alternatives cannot run
    fills: to choose, make a second fill with a different context and
    compare with render_preview(zoom=1), then remove the worse instance with
    remove_module_instance. Check the result at 100% with
    render_preview(zoom=1) before and after.

    The fill is written as a float TIFF beside the photo (in a
    darktable_fills folder, or in DTAPI_FILLS_DIR when the server sets it) and imported into the photo's group. Needs the
    inpaint model installed (inpaint-lama, or inpaint-migan, in darktable's
    models folder) and AI enabled.

    Size: small to medium shapes only. Shapes up to about 1300 px across
    at full size (a tail, a sign, a person in the distance; roughly a fifth
    of the photo's shorter side) fill with believable texture. Larger ones
    (a main subject) come out as a soft, flat smear, not texture: before
    filling a large shape, tell the user it will likely not look right.
    When the reply has a "warning", show it to the user in your answer, as
    it is, and check the fill at 100% with them. In darktable's window the
    user can mark the area themselves: a drawn mask on any module (e.g. a
    spare exposure instance), then like_mask_of that module
    (like_mask_shapes to take some of its shapes). Not available
    on darktable-api for 5.6 (it needs darktable master, or 5.8 on). Returns the
    instance, name, imgid of the patch photo, file, model, patch box,
    shape_px (the shape's longest side in pixels), process_ms, for a
    large shape warning, and with model "flux" bfl (task, cost in credits,
    seconds)."""
    await _require("ai_fill", "coords")
    if shape == "gradient":
        raise ToolError("a gradient cannot be filled: use circle, ellipse, path or brush")
    context = max(0.25, min(float(context), 4.0))
    if like_mask_of:
        await _require("mask_list")
        r = await _edit(None, "mask_list", operation=like_mask_of, instance=like_mask_instance)
        shapes = [s for s in r.get("shapes", []) if s.get("active", True) and s.get("type") != "other"]
        if like_mask_shapes:
            shapes = _pick_shapes(shapes, like_mask_shapes, like_mask_of, like_mask_instance)
        members = [_member_from_listed(s) for s in shapes]
        if not members:
            raise ToolError(f"module '{like_mask_of}' (instance {like_mask_instance}) has no mask shapes")
        sh: dict[str, Any] = {"type": "group", "members": members}
    else:
        sh = await _shape_on_raw(shape, x, y, radius, radius_y, rotation, feather, 0.5, points,
                                 width, hardness, density)
    r = await _ai_fill(None, sh, context, name, model, prompt, upscale, online_size)
    if r.get("warning"):
        r["warning"] = f"Tell the user: {r['warning']}"
    return r


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


PHOTO_SPOT_IDS = 101            # spots found in this photo alone are numbered from here


async def _photo_spots(image_id: int, geom: dict, mapped: list[dict]) -> tuple[list[dict], Any]:
    """Dust in this photo alone, found in darktable's render of its current
    edit (dust.detect_in_render), placed on the sensor with darktable's own
    coordinates so the rest of the dust tools treat them as mapped spots.
    Returns the spots and the render's luminance (for their crop sheet)."""
    import cv2
    import numpy as np
    dust = _dust_module()
    # JPEG quality 85's 8 px blocks look like faint blobs
    r = await engine.render(image_id, geom["half_width"], geom["half_height"], quality=97)
    if r is None:
        raise ToolError("the render was overtaken by a newer one: try again")
    im = cv2.imdecode(np.frombuffer(r[0], np.uint8), cv2.IMREAD_COLOR).astype(np.float32) / 255.0
    lum = im @ np.array([0.0722, 0.7152, 0.2126], np.float32)     # BGR
    h, w = lum.shape
    found = await asyncio.get_running_loop().run_in_executor(None, dust.detect_in_render, lum)
    if not found:
        return [], lum
    # each spot's centre and a point one radius right of it, photo -> raw
    pts = []
    for d in found:
        pts += [[d["x"] / w, d["y"] / h], [(d["x"] + d["r"]) / w, d["y"] / h]]
    try:
        raw = (await engine.edit(image_id, "coords", points=pts, **{"from": "image", "to": "raw"}))["points"]
    except EngineError as exc:
        raise _err(exc)
    out = []
    for i, d in enumerate(found):
        (rx, ry), (ex, ey) = raw[2 * i], raw[2 * i + 1]
        hx, hy = (rx * geom["raw_width"] - geom["left_margin"]) / 2, (ry * geom["raw_height"] - geom["top_margin"]) / 2
        hr = ((ex - rx) ** 2 * geom["raw_width"] ** 2 + (ey - ry) ** 2 * geom["raw_height"] ** 2) ** 0.5 / 2
        # already in the roll's map: reported there
        if any(((hx - m["x"]) ** 2 + (hy - m["y"]) ** 2) ** 0.5 <= max(hr, m["r"]) * 1.5 for m in mapped):
            continue
        out.append({**d, "id": PHOTO_SPOT_IDS + len(out), "x": hx, "y": hy, "r": hr, "roll_frames": None,
                    "found_in": "this photo", "render_xy": (d["x"], d["y"], d["r"]),
                    "render_extent": dust.blob_extent(lum, d["x"], d["y"], d["r"]), "half_per_render": hr / d["r"],
                    "on_photo": [round(d["x"] / w, 4), round(d["y"] / h, 4)]})
    return out, lum


async def _mono_edit(image_id: int, modules: list[dict]) -> str | None:
    """Whether the edit turns the photo black and white, and how: an enabled
    color calibration instance with a gray mix (what darktable applies when
    any of its R, G, B weights is nonzero) or the monochrome module."""
    for m in modules:
        if not m["enabled"]:
            continue
        if m["operation"] == "monochrome":
            return "monochrome module"
        if m["operation"] == "channelmixerrgb":
            g = await engine.edit(image_id, "module_get", operation="channelmixerrgb", instance=m["instance"])
            grey = next((f["value"] for f in g["fields"] if f["name"] == "grey"), None)
            if grey and any(v != 0 for v in grey[:3]):
                r, gr, b = grey[:3]
                kind = "red-weighted gray mix" if r > max(gr, b) else "gray mix"
                return f"color calibration {kind} (R {r:.2f}, G {gr:.2f}, B {b:.2f})"
    return None


async def _dust_analysis(image_id: int | None, rebuild_map: bool = False, this_photo: bool | None = False) -> dict:
    """What find_dust_spots and heal_dust_spots need for one photo. Opens it
    (joining an edit already open) so retouch circles include unsaved ones.
    this_photo adds the dust found in this photo alone (_photo_spots); None:
    when the edit is black and white (_mono_edit), where a filter's tones
    bring out dust the roll's map doesn't have."""
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
        # heal_dust_spots' AI fills cover their spots as heal circles do
        modules = (await engine.edit(image_id, "module_list"))["modules"]
        mono = await _mono_edit(image_id, modules) if this_photo is None else None
        fills = [m for m in modules
                 if m["operation"] == "overlay" and m["enabled"] and m["name"].startswith(DUST_FILL_NAME)]
        for m in fills:
            ml = await engine.edit(image_id, "mask_list", operation="overlay", instance=m["instance"])
            for s in ml.get("shapes", []):
                for c in s.get("members") or [s]:
                    if c.get("type") == "circle" and c.get("active", True):
                        rl["spots"].append({**c, "active": True})
    except EngineError as exc:
        raise _err(exc)
    circles = []
    for c in rl["spots"]:
        if c.get("type") == "circle" and c.get("active"):
            x, y, r = dust.half_from_raw(c["x"], c["y"], c["r"], geom)
            circles.append({"x": x, "y": y, "r": r})
    spots = dust.spots_in_frame(lum, dust_map)
    for s in spots:
        s["found_in"] = "roll"
    photo_lum = None
    if this_photo is None:
        this_photo = mono is not None
    if this_photo:
        photo, photo_lum = await _photo_spots(image_id, geom, dust_map["dust"])
        spots += photo
    for s in spots:
        s["already_retouched"] = any(((s["x"] - c["x"]) ** 2 + (s["y"] - c["y"]) ** 2) ** 0.5 <= c["r"]
                                     for c in circles)
        s["raw"] = dust.raw_norm(s["x"], s["y"], geom)
    # where each mapped spot is on the photo as rendered (lens, rotation, crop...)
    mapped = [s for s in spots if s["found_in"] == "roll"]
    if mapped:
        try:
            pts = (await engine.edit(image_id, "coords", points=[[s["raw"]["raw_x"], s["raw"]["raw_y"]] for s in mapped],
                                     **{"from": "raw", "to": "image"}))["points"]
        except EngineError as exc:
            raise _err(exc)
        for s, (u, v) in zip(mapped, pts):
            s["on_photo"] = [round(u, 4), round(v, 4)] if 0 <= u <= 1 and 0 <= v <= 1 else None
    return {"image_id": image_id, "info": info, "dust_map": dust_map, "lum": lum, "geom": geom,
            "circles": circles, "spots": spots, "photo_lum": photo_lum, "this_photo": this_photo, "mono": mono}


def _dust_spot_json(s: dict, geom: dict) -> dict:
    return {"id": s["id"], "status": s["status"], "already_retouched": s["already_retouched"],
            "darkening_pct": round(100 * s["depth"], 1), "contrast_vs_background": s["ratio"],
            "background": s["background"], "diameter_px": int(round(4 * s["r"])),
            "on_photo": s.get("on_photo"), "raw_x": s["raw"]["raw_x"], "raw_y": s["raw"]["raw_y"],
            "found_in": s["found_in"], "found_in_roll_frames": s["roll_frames"], "size": s.get("size", "small")}


@mcp.tool(structured_output=False)
async def find_dust_spots(image_id: int | None = None, rebuild_map: bool = False,
                          this_photo: bool | None = None) -> list:
    """Find sensor dust in a photo (default: the open one). Read-only.

    Dust sits at the same sensor position in every frame, so this maps the
    soft dark round blobs that recur across the photo's film roll (cached; the
    first call for a roll reads every raw file, ~0.4 s each), small specks
    and large soft blobs (~50-300 px), then measures each mapped spot in this
    photo: dust shows against smooth, bright-ish backgrounds (sky), more at
    small apertures.

    this_photo=True also looks for dust in this photo alone, in darktable's
    render of its current edit: one-off spots the roll's map doesn't have,
    and spots an edit brought out (a black and white red or orange filter
    darkens a blue sky and shows faint dust). These are round, even, soft
    blobs on smooth areas, numbered from 101, found_in "this photo", with a
    second crop sheet (the photo as rendered), at most 8, the strongest
    first. One photo can't prove a blob is dust (in a noisy sky, noise and
    soft cloud gradients look alike): look at that sheet and heal only the
    round soft shadows, heal_dust_spots(spots=[ids]). Left out (None), it
    is on when the edit is black and white (a color calibration gray mix or
    the monochrome module; the summary's this_photo_reason says which) and
    off otherwise; True when the user sees spots the map lacks. Converting
    a photo to black and white changes what dust shows: search again after
    the conversion, not only before it.

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
    a = await _dust_analysis(image_id, rebuild_map, this_photo)
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
    roll = [s for s in a["spots"] if s["found_in"] == "roll"]
    photo = [s for s in a["spots"] if s["found_in"] == "this photo"]
    if a["this_photo"]:
        summary["this_photo"] = ([s["id"] for s in photo] if photo else
                                 "no round dust-like blobs on smooth areas of this render")
        if this_photo is None:
            summary["this_photo_reason"] = f"black and white edit: {a['mono']}"
    result = [json.dumps(summary)]
    if roll or not photo:
        result += ["Crops of the roll's dust (sensor orientation):",
                   McpImage(data=dust.crop_sheet(a["lum"], roll), format="png")]
    if photo:
        tiles = [{**s, "x": s["render_xy"][0], "y": s["render_xy"][1], "r": s["render_xy"][2]} for s in photo]
        result += ["Crops of the dust found in this photo alone (the photo as rendered):",
                   McpImage(data=dust.crop_sheet(a["photo_lum"], tiles), format="png")]
    return result


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


async def _photo_heal_plan(a: dict, s: dict, need_source: bool = True) -> dict | None:
    """Heal circle for a spot found in this photo alone: sized to the blob's
    whole soft shadow and copying from a smooth patch chosen on the render
    (what the user sees, after any black and white filter), then placed on
    the raw with darktable's coordinates."""
    import numpy as np
    dust = _dust_module()
    lum = a["photo_lum"]
    x, y, _r = s["render_xy"]
    R = 1.1 * s["render_extent"]
    g = a["geom"]
    # darktable feathers a heal circle by a border it copies too (its default,
    # plugins/darkroom/spots/circle_border: 2 % of the raw's shorter side):
    # the source is judged, and kept clear, out to the border's edge
    border = 0.02 * min(g["raw_width"], g["raw_height"]) / 2 / s["half_per_render"]
    others = [(o["render_xy"][0], o["render_xy"][1], o["render_extent"]) for o in a["spots"]
              if o["found_in"] == "this photo" and o is not s]
    yy, xx = np.ogrid[:lum.shape[0], :lum.shape[1]]
    ring = lum[((xx - x) ** 2 + (yy - y) ** 2 <= (2.2 * R) ** 2) & ((xx - x) ** 2 + (yy - y) ** 2 > (1.2 * R) ** 2)]
    calm = float(np.std(ring) / (np.mean(ring) + 1e-3)) if ring.size else 0.02
    src = dust.choose_source(lum, x, y, R + border, others, max_texture=max(0.015, 2.5 * calm))
    if src is None:
        if need_source:
            return None
        src = {"x": x, "y": y}      # an AI fill needs no source
    h, w = lum.shape
    try:
        raw = (await engine.edit(a["image_id"], "coords", points=[[x / w, y / h], [src["x"] / w, src["y"] / h]],
                                 **{"from": "image", "to": "raw"}))["points"]
    except EngineError as exc:
        raise _err(exc)
    R_half = R * s["half_per_render"]
    spec = {"x": round(raw[0][0], 5), "y": round(raw[0][1], 5),
            "r": round(2 * R_half / min(g["raw_width"], g["raw_height"]), 5),
            "sx": round(raw[1][0], 5), "sy": round(raw[1][1], 5)}
    k = s["half_per_render"]
    return {"spot": s, "R": R_half, "spec": spec,
            "source": {**src, "x": s["x"] + (src["x"] - x) * k, "y": s["y"] + (src["y"] - y) * k}}


@mcp.tool(structured_output=False)
async def heal_dust_spots(image_id: int | None = None, spots: list[int] | None = None,
                          include_visible: bool = False, method: str = "auto",
                          dry_run: bool = False) -> list:
    """Heal sensor dust in a photo (default: the open one) with darktable's
    retouch module, or with an AI fill where healing leaves a trace.

    By default heals the spots find_dust_spots rates "obvious" that no active
    retouch circle or dust fill covers yet; include_visible adds the
    "visible" ones, and spots=[ids] picks specific ones. Dust
    find_dust_spots(this_photo=True) found in this photo alone (ids from
    101) is healed only when listed in spots, after you looked at its crop
    sheet; call this on the same edit as find_dust_spots, so the numbers
    match.

    method:
      "auto" (default): a heal circle, or an AI fill (ai_fill) where no
        clean source patch is nearby. A heal keeps the texture of ground
        and foliage; a fill smooths it (a soft, slightly lighter disk), but
        needs no source. Without ai_fill (darktable-api for 5.6) those
        spots are skipped.
      "heal": heal circles only.
      "ai_fill": AI fills only (e.g. dust on a gradient where heals show).
    A heal circle is about twice the dust's radius, copying from a nearby
    patch chosen to be smooth and to match the brightness around it; all
    heal circles are one history step in one retouch instance, and can be
    pasted to other photos of the roll. Each fill is its own "composite"
    instance named "dust #id" with a small TIFF in the photo's group (a few
    seconds each); it applies to this photo only. Undo: set_history_end.
    Not saved until save.

    Checks the result at 100% around each spot before and after and returns
    the darkening per spot before/after (healed when it mostly disappears;
    on textured ground the texture itself reads as a little darkening), plus a before/after sheet. dry_run=True only returns
    the plan."""
    from mcp.server.fastmcp import Image as McpImage
    import cv2
    import numpy as np
    await _require("retouch_heal", "render.zoom")
    if method not in ("auto", "heal", "ai_fill"):
        raise ToolError('method: "auto", "heal" or "ai_fill"')
    try:
        can_fill = await engine.supports("ai_fill")
    except EngineError as exc:
        raise _err(exc)
    if method == "ai_fill" and not can_fill:
        raise ToolError("ai_fill needs darktable-api on darktable master (5.8 on): use method \"heal\"")
    a = await _dust_analysis(image_id, this_photo=any(i >= PHOTO_SPOT_IDS for i in (spots or [])))
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
             + [(d["x"], d["y"], d["r"]) for d in a["spots"] if d["found_in"] == "this photo"]
             + [(c["x"], c["y"], c["r"] / 2.5) for c in a["circles"]])
    may_fill = can_fill and method != "heal"
    plan = []
    for s in targets:
        if s.get("on_photo") is None:
            skipped.append({"id": s["id"], "why": "outside the photo as cropped"})
            continue
        fill = method == "ai_fill"
        if s["found_in"] == "this photo":
            p = await _photo_heal_plan(a, s, need_source=not may_fill)
            if p is None:
                skipped.append({"id": s["id"], "why": "no clean source patch nearby"})
                continue
            if p["source"].get("brightness_diff_pct") is None:
                fill = True
            plan.append({**p, "method": "ai_fill" if fill else "heal"})
            continue
        # a blob found in one photo is measured at its most even radius,
        # inside its soft edge: twice that covers it
        R = dust.heal_radius(s["r"], s.get("size", "small"))
        # darktable feathers a heal circle by a border it copies too (as in
        # _photo_heal_plan): a source closer than that pastes the dust beside it
        border = 0.02 * min(geom["raw_width"], geom["raw_height"]) / 2
        src = None if fill else dust.choose_source(
            lum, s["x"], s["y"], R, [v for v in avoid if (v[0], v[1]) != (s["x"], s["y"])],
            min_dist=R + border + s["r"])
        if src is None and not fill:
            if not may_fill:
                skipped.append({"id": s["id"], "why": "no clean source patch nearby"})
                continue
            fill = True
        src = src or {"x": s["x"], "y": s["y"]}
        plan.append({"spot": s, "R": R, "source": src, "spec": dust.heal_spec(s, src, R, geom),
                     "method": "ai_fill" if fill else "heal"})

    def planned(p: dict) -> dict:
        e = {"id": p["spot"]["id"], "status": p["spot"]["status"], "background": p["spot"]["background"],
             "method": p["method"], "diameter_px": int(round(4 * p["R"]))}
        if p["method"] == "heal":
            e.update(source_offset_px=[int(round(2 * (p["source"]["x"] - p["spot"]["x"]))),
                                       int(round(2 * (p["source"]["y"] - p["spot"]["y"])))],
                     source_brightness_diff_pct=p["source"]["brightness_diff_pct"], circle=p["spec"])
        return e

    summary = {"image_id": image_id, "file": a["info"]["filename"], "plan": [planned(p) for p in plan],
               "skipped": skipped}
    if not plan:
        summary["note"] = "nothing to heal" + ("" if a["spots"] else ": no dust mapped for this roll")
        return [json.dumps(summary)]
    if dry_run:
        return [json.dumps(summary),
                McpImage(data=dust.crop_sheet(lum, [p["spot"] for p in plan]), format="png")]

    async def fill(p: dict) -> dict:
        c = p["spec"]
        sh = {"type": "circle", "x": c["x"], "y": c["y"], "r": c["r"], "border": round(0.45 * c["r"], 5)}
        r = await _ai_fill(image_id, sh, 0.5, f"{DUST_FILL_NAME} #{p['spot']['id']}")
        return {"id": p["spot"]["id"], "instance": r.get("instance"), "process_ms": r.get("process_ms")}

    heals = [p for p in plan if p["method"] == "heal"]
    fills = []
    try:
        before = await _spot_crops(image_id, plan)
        await _note_before(image_id)
        if heals:
            r = await engine.edit(image_id, "retouch_heal", spots=[p["spec"] for p in heals])
            summary["added"] = r["added"]
        for p in plan:
            if p["method"] == "ai_fill":
                fills.append(await fill(p))
        after = await _spot_crops(image_id, plan)
        summary["history_end"] = (await engine.edit(image_id, "history_list"))["history_end"]
    except EngineError as exc:
        raise _err(exc)
    if fills:
        summary["fills"] = fills
    checks, rows = [], []
    for p in plan:
        sid = p["spot"]["id"]
        if sid not in before or sid not in after:
            checks.append({"id": sid, "method": p["method"], "result": "not checked (render overtaken)"})
            continue
        (lb, x, y), (la, _, _) = before[sid], after[sid]
        rr = max(6.0, 2 * p["spot"]["r"])            # full-size pixels: a 100% render
        db, da = dust.darkening_at(lb, x, y, rr), dust.darkening_at(la, x, y, rr)
        c = {"id": sid, "method": p["method"], "darkening_before_pct": round(100 * db, 1),
             "darkening_after_pct": round(100 * da, 1), "healed": da <= max(0.02, 0.4 * db)}
        checks.append(c)
        pair = []
        for img in (lb, la):
            n = np.clip((img - img.mean()) / (6 * img.std() + 1e-6) + 0.5, 0, 1)
            t = cv2.cvtColor((n * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
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
