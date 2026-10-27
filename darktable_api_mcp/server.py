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

from . import panorama
from .engine import EngineError, engine

INSTRUCTIONS = """\
Edits photos in a darktable library through darktable-api (a headless
darktable). Flow: open_photo(id) -> get_module (setting names and values;
full=True for every range and dropdown) -> set_module(..., preview=True) -> save. Edits stay
in memory until save; discard_changes reopens the saved state. "Photo 36"
means library id 36. One photo is current at a time (the last open_photo).

Scene-referred order: exposure (subject midtones), white balance in color
calibration (channelmixerrgb), one tone mapper (agx; sigmoid/filmicrgb on
older edits; never two), then toneequal and colorbalancergb.
Film emulation: spektrafilm (darktable 5.8+), a spectral film and print
simulation that is itself the tone mapper: switch agx/sigmoid/filmicrgb
off when using it. Choose a stock with its presets (list_presets).
Black and white: add_module_instance("channelmixerrgb") (new, not a copy),
apply_preset a "monochrome | ..." preset to it, keep the first instance for
white balance; for creative B&W move_module it after the tone mapper.
When the user asks what you changed, use compare_before_after (mark_before
starts a new "before"). For choices (crops, filters, looks) use
propose_alternatives. Measure with measure_photo / render_preview(zoom=...);
don't write analysis scripts or install packages.
Panoramas: make_panorama, two steps (first call without fixes returns the
prerequisites report: show it and ask; then call again with the answer's fixes).
Removing objects: ai_fill (an AI model fills a drawn shape, or an
add_ai_mask outline via like_mask_of, from its surroundings; a composite
instance, non-destructive). Small spots: retouch_spots. Red eye: fix_red_eye.
Markers: points and areas the user drew and named in the web app
(list_markers); tools take marker=name instead of coordinates.

The engine is shared with the user's web app and darktable's window:
open_photo joins an edit already open there (unsaved changes included),
your changes show up live, and save saves the whole edit. If darktable's
window serves the library, its darkroom photo (library_status:
darkroom_imgid; open_darkroom_photo) is usually "this photo"; most pickers
(use_picker) work only on it. While the library is released to darktable's
GUI, only library_status, acquire_library and takeover_library work. Tell
the user what you are doing.

Cost: every request re-reads the whole conversation, renders included.
Adjust with measure_photo and look once per finished step (preview=True on
the edit, default size; zoom for detail). After two failed tries at one
fix, change the approach or ask; don't push the strength or retry. Hand a
cheaper agent only edits whose values are already decided (paste_edit,
styles, batches); looks, local edits and object removal stay here. You
can't run /clear or /compact, so suggest them in one line at the end of a
reply, without holding up the request: /clear when the user moves to
another photo (the edit lives in the library, nothing here is needed);
/compact before a new round of changes on the same photo once the
conversation holds about 10 renders, naming what to keep (photo id, the
look wanted, what the user liked and rejected, approaches that failed)."""

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
                 "retouch_remove", "module_add", "module_remove", "module_move", "module_rename", "ai_fill", "capture_autotune"}
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


# ── markers ──────────────────────────────────────────────────────────────────

async def _markers_on_photo(image_id: int | None = None) -> list[dict]:
    """The photo's markers with their points as fractions of the photo as
    shown (the engine keeps them in raw space), plus box and center."""
    await _require("marker_list", "coords")
    if image_id is None:
        image_id = engine.image_id
    if image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    ms = (await _call("marker_list", imgid=image_id))["markers"]
    flat = [p for m in ms for p in m["points"]]
    shown = iter((await _edit(image_id, "coords", points=flat, **{"from": "raw", "to": "image"}))["points"]
                 if flat else [])
    out = []
    for m in ms:
        pts = [[round(v, 5) for v in next(shown)] for _ in m["points"]]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = {"left": min(xs), "top": min(ys), "right": max(xs), "bottom": max(ys)}
        out.append({"name": m["name"], "kind": m["kind"], "points": pts, "box": box,
                    "center": [round((box["left"] + box["right"]) / 2, 5), round((box["top"] + box["bottom"]) / 2, 5)],
                    "note": m.get("note", ""), "by": m.get("by", "")})
    return out


async def _marker(name: str) -> dict:
    """One of the open photo's markers by name (case ignored), as
    _markers_on_photo gives it; an unknown name lists the photo's."""
    ms = await _markers_on_photo()
    for m in ms:
        if m["name"].casefold() == name.strip().casefold():
            return m
    raise ToolError(f"no marker '{name}' on photo {engine.image_id}; it has: "
                    + (", ".join(f"'{m['name']}'" for m in ms) or "none"))


async def _marker_shape(name: str) -> tuple[str, float | None, float | None, list[list[float]] | None, str]:
    """A marker as add_mask's (shape, x, y, points, corners): an area a path
    through its points (a rect's with sharp corners), a point a circle at it."""
    m = await _marker(name)
    if m["kind"] == "point":
        return "circle", m["points"][0][0], m["points"][0][1], None, "smooth"
    return "path", None, None, m["points"], "sharp" if m["kind"] == "rect" else "smooth"


async def _marker_corners(corners: str) -> str:
    """A marker's corners, smooth on an engine without sharp ones."""
    return corners if corners != "sharp" or await engine.supports("mask_add.corners") else "smooth"


@mcp.tool()
async def list_markers(image_id: int | None = None) -> dict:
    """The points and areas the user drew and named on a photo in the web
    app (default: the open photo); refer to them by name, and pass the name
    as marker= to add_mask, ai_fill, retouch_spots, use_picker, or
    markers= to measure_photo. Each: name, kind (point, rect, ellipse,
    outline), points and box {left, top, right, bottom} and center [x, y]
    (fractions of the photo as shown), note, by (who set it)."""
    return {"image_id": image_id if image_id is not None else engine.image_id,
            "markers": await _markers_on_photo(image_id)}


@mcp.tool()
async def set_marker(name: str, kind: str, points: list[list[float]], note: str = "") -> dict:
    """Mark a spot or area on the open photo and name it, to show the user
    ("I marked 'dust 3'") and to refer to later; it appears in the web app
    and replaces a marker of the same name. kind: "point" (1 point), "rect"
    (4 corners), "ellipse" (8-128 points around it), "outline" (3-500
    points). points [[x, y], ...]: fractions of the photo as shown. Not
    part of the edit."""
    await _require("marker_set", "coords")
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    raw, _, _ = await _photo_to_raw(points)
    r = await _call("marker_set", imgid=engine.image_id, name=name, kind=kind, points=raw, note=note)
    return {"image_id": r["imgid"], "marker": next(m for m in await _markers_on_photo()
                                                   if m["name"] == r["marker"]["name"])}


@mcp.tool()
async def remove_marker(name: str) -> dict:
    """Remove one marker of the open photo by name."""
    await _require("marker_remove")
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    return await _call("marker_remove", imgid=engine.image_id, name=name)


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
                      label: str | None = None, tag: str | None = None, offset: int = 0,
                      limit: int = 50) -> dict:
    """Photos in folder/filename order: id, filename, folder, rating,
    rejected, color labels (0 red .. 4 purple), size, history_end (0 =
    unedited). film_roll_id: omit for all. rating: "visible" (default:
    not rejected), "all", "rejected", or "1".."5" (at least). label: a
    color name. tag: only photos with that tag or one under it ("places"
    also takes "places|paris"; case ignored; list_tags names them).
    offset, limit (up to 1000); "total" is the full count."""
    extra = {}
    if tag:
        await _require("images_list.tag")
        extra["tag"] = tag
    return await _call("images_list", offset=offset, limit=limit, **_filters(film_roll_id, rating, label), **extra)


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
    """Make darktable's thumbnails in bulk, from each photo's saved edit,
    into darktable's disk cache (for the lighttable, web app,
    get_thumbnail, photo_sheet). image_ids, film_roll_id or
    whole_library=True; outdated_only (default) skips current ones; size:
    the largest needed. A job (0.2-1 s a photo): wait=True returns
    generated, skipped, failed."""
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
    """Import photos, as darktable's import does. paths: files or folders on
    darktable's machine (recursive=True: subfolders too). copy=False adds
    them where they are; copy=True copies and renames them into
    darktable's import folders (jobcode names the folder), for a card or
    downloads: ask which when it isn't clear. skip_existing (default)
    leaves out photos already in the library. A job: wait=True returns
    imported, skipped, failed and film_ids; then
    make_thumbnails(film_roll_id=...)."""
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
    """Many photos at once, for tagging, culling or finding a subject: each
    thumbnail with its id, file name, date, camera, lens, location, title
    and tags. image_ids, or a film_roll_id (omit: the whole library) with
    offset/limit (up to 24 a page). untagged_only skips photos with tags of
    their own. To tag: list_tags first and reuse its hierarchy,
    make_thumbnails first for speed, then tag_photos."""
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
    """Open a photo for editing and make it the current photo; lists its
    modules in pipeline order ("operation instance 'name'", "off" when
    switched off; list_modules for labels) and history. A photo already open (by you, or by the user in
    another app) is joined with its unsaved changes ("unsaved": true);
    discard_unsaved=True reloads it as saved, dropping those changes for
    everyone (ask the user first)."""
    try:
        opened = await engine.open(image_id, fresh=discard_unsaved)
        mods = await engine.edit(image_id, "module_list")
        hist = await engine.edit(image_id, "history_list")
        info = await engine.call("image_info", imgid=image_id)
    except EngineError as exc:
        raise _err(exc)
    image = {k: v for k, v in (info.get("image") or {}).items()
             if k != "changed" and v not in ([], None) and not (k == "version" and v == 0)}
    return {"image": image, "unsaved": opened.get("unsaved"), "shared": opened.get("joined"),
            "modules": [_module_tag(m) for m in mods["modules"] if m["in_history"] or m["enabled"]],
            "history_end": hist["history_end"], "history_items": len(hist["items"])}


def _module_tag(m: dict) -> str:
    """A module in a line: "exposure", "exposure 1 'sky'", "lens off"."""
    tag = m["operation"] + (f" {m['instance']}" if m["instance"] else "")
    if m.get("name") and not m["name"].startswith("_builtin"):
        tag += f" '{m['name']}'"
    return tag + ("" if m["enabled"] else " off")


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


async def _and_look(reply: dict, preview: bool | dict, measure: dict | None) -> dict | list:
    """An edit tool's reply, with render_preview's image (preview: True or
    its arguments) and measure_photo's readout (measure: its arguments)
    after it: the edit and a look at it in one call."""
    if not preview and not measure:
        return reply
    out: list = [reply]
    try:
        if measure:
            out.append({"measure": await measure_photo(**measure)})
        if preview:
            out.append(await render_preview(**(preview if isinstance(preview, dict) else {})))
    except (TypeError, ToolError) as exc:
        out.append(f"the edit is done; the look failed: {exc}")
    return out


@mcp.tool()
async def list_modules() -> dict:
    """The open photo's modules in pipeline order: operation (e.g. exposure,
    agx, colorbalancergb, channelmixerrgb, toneequal), instance, name, label
    (as darktable shows the module, e.g. "local contrast" for bilat), enabled,
    whether it is in the history."""
    return await _edit(None, "module_list")


def _r(v: Any) -> Any:
    """Floats to 4 significant digits (darktable's are float32: 0.699999988)."""
    if isinstance(v, float):
        return float(f"{v:.4g}")
    if isinstance(v, list):
        return [_r(x) for x in v]
    return v


@mcp.tool()
async def get_module(operation: str, instance: int = 0, full: bool = False) -> dict:
    """A module's settings on the open photo. "settings": the ones changed
    from their default, each with value, default, range, label (darktable's
    name for it) and a dropdown's options; "at_default": the rest, name:
    value. List settings have nested values, e.g. color calibration's gray
    mix "grey" (R, G, B, normalization). Use these names in set_module.
    full=True: every setting's range, label, shape and options."""
    m = await _edit(None, "module_get", operation=operation, instance=instance)
    if full:
        return m
    out = {k: m[k] for k in ("operation", "instance", "enabled") if k in m}
    out["settings"], out["at_default"] = {}, {}
    for f in m.get("fields", []):
        if f.get("default") == f["value"]:
            out["at_default"][f["name"]] = _r(f["value"])
            continue
        e = {"value": _r(f["value"]), "default": _r(f.get("default"))}
        if "min" in f:
            e["range"] = [_r(f["min"]), _r(f["max"])]
        if f.get("description"):
            e["label"] = f["description"]
        if "values" in f:
            e["options"] = [o["name"] for o in f["values"] if o["label"]]
        out["settings"][f["name"]] = e
    return out


@mcp.tool(structured_output=False)
async def set_module(operation: str, values: dict[str, Any], instance: int = 0,
                     preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Change a module's settings on the open photo, e.g.
    set_module("exposure", {"exposure": 0.7}). Names and ranges from
    get_module; dropdowns by name, label or number; list settings whole or
    one element ({"grey[1]": 0.6}). One bad value changes nothing.
    Switches the module on, one history step, not saved until save.
    darktable adjusts related settings as when a control is moved
    (darktable_also_changed in the reply).
    preview=True (or render_preview's arguments, {"zoom": 1}) and
    measure={measure_photo's arguments} add the render and readout: no
    separate call to look."""
    if any("[" in k or isinstance(v, list) for k, v in values.items()):
        await _require("module_set.lists")
    return await _and_look(await _edit(None, "module_set", operation=operation, values=values, instance=instance),
                           preview, measure)


@mcp.tool()
async def list_presets(operation: str, instance: int = 0) -> dict:
    """A module's presets as its presets menu lists them: name (for
    apply_preset), label (also accepted), builtin, autoapply."""
    await _require("preset_list")
    return await _edit(None, "preset_list", operation=operation, instance=instance)


@mcp.tool(structured_output=False)
async def apply_preset(operation: str, name: str, instance: int = 0,
                       preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Apply a module preset on the open photo, as darktable's presets menu
    does: the preset's settings, on/off and blending replace the module's,
    as one history step. name: from list_presets (name or label). Not saved
    until save. preview, measure as set_module."""
    await _require("preset_apply")
    return await _and_look(await _edit(None, "preset_apply", operation=operation, name=name, instance=instance),
                           preview, measure)


@mcp.tool(structured_output=False)
async def enable_module(operation: str, enabled: bool, instance: int = 0,
                        preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Turn a module on or off on the open photo (keeps its settings).
    preview, measure as set_module."""
    return await _and_look(await _edit(None, "module_enable", operation=operation, enabled=enabled,
                                       instance=instance), preview, measure)


@mcp.tool()
async def auto_capture_sharpen() -> dict:
    """Turn on capture sharpening on the open photo with a radius and noise
    threshold measured on this photo. Capture sharpening is a deconvolution
    inside the demosaic module that restores detail lost to the lens and the
    sensor's anti-aliasing filter (darktable 5.4+). This measures the
    radius and threshold the way darktable's reset button by the radius does,
    sets cs_enabled, cs_radius and cs_thrs, and returns the values. Raw
    photos only, in color. About 1 s for 16 MP. One undoable edit, unsaved
    until save. Check the result at 100 % (render_preview zoom=1).
    Downsides: it sharpens noise too (look at high-ISO photos), too big a
    radius gives halos, it cannot fix missed focus or motion blur, and don't
    stack it with other strong sharpening (diffuse or sharpen "sharpen"
    presets, the sharpen module).
    Auto-apply setup: a demosaic preset with capture sharpening on and
    radius 0, auto-applied, is measured per photo on its first save."""
    await _require("capture_autotune")
    return await _edit(None, "capture_autotune")


@mcp.tool()
async def add_module_instance(operation: str, instance: int = 0, copy: bool = False) -> dict:
    """Add another instance of a module after the given one, as its "new
    instance" (copy=False: defaults) or "duplicate" (copy=True: its
    settings and blending) does, as one history step. Returns the new
    instance number (instance=... in other tools)."""
    await _require("module_add")
    return await _edit(None, "module_add", operation=operation, instance=instance, copy=copy)


@mcp.tool()
async def move_module(operation: str, instance: int = 0, after: str | None = None, after_instance: int = 0,
                      before: str | None = None, before_instance: int = 0) -> dict:
    """Move a module instance right after or before another in the pipe, as
    dragging it in the darkroom (darktable's rules apply), e.g.
    move_module("channelmixerrgb", 1, after="agx"). Returns its iop_order."""
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


@mcp.tool(structured_output=False)
async def set_curve(operation: str, points: list[list[float]], channel: str | None = None,
                    type: str | None = None, instance: int = 0,
                    preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Set one channel's curve of rgbcurve, tonecurve, colorzones or
    basecurve: points [[x, y], ...] in 0..1, x increasing (2-20). channel
    as get_curve lists them (default the first). type: "cubic spline",
    "centripetal spline", "monotonic spline". Switches the module on, one
    history step. rgbcurve is scene-linear: middle gray is near x = 0.18.
    preview, measure as set_module."""
    await _require("curve_set")
    params: dict[str, Any] = {"operation": operation, "instance": instance, "points": points}
    if channel is not None:
        params["channel"] = channel
    if type is not None:
        params["type"] = type
    return await _and_look(await _edit(None, "curve_set", **params), preview, measure)


@mcp.tool()
async def duplicate_photo(image_id: int | None = None, virgin: bool = False, save_first: bool = False) -> dict:
    """Make a duplicate (a virtual copy of the same raw), as darktable's
    lighttable does: with its saved edit, or virgin=True unedited. Unsaved
    changes: save_first=True saves them first (ask the user). Returns the
    new photo's library entry."""
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
    """Remove photos from the library, as the lighttable's "remove": their
    library entries and edits go; files on disk and other duplicates stay.
    Refused for the darkroom photo, unsaved changes, or an overlay. Can't
    be undone: name the photos (ids, file names) to the user and ask, then
    pass confirm=True."""
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
    """The open photo's orientation (clockwise degrees, mirrored; relative
    to the raw), angle (straightening), autocrop, crop (left, top, right,
    bottom, fractions of the uncropped photo), aspect,
    frame_width/frame_height (uncropped, pixels) and width/height."""
    return await _edit(None, "geometry_get")


@mcp.tool()
async def rotate_photo(degrees: int = 0, angle: float | None = None, mirror: str | None = None,
                       autocrop: str | None = None) -> dict:
    """Turn, mirror or straighten the open photo. degrees: quarter turns from
    now (90 = clockwise). angle: absolute straightening in degrees
    (positive = counter-clockwise); the frame shrinks and a crop shifts, so
    straighten before cropping. mirror: "horizontal" or "vertical".
    autocrop: "ASHIFT_CROP_LARGEST" (default), "ASHIFT_CROP_ASPECT",
    "ASHIFT_CROP_OFF". Returns the geometry. Not saved until save."""
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


@mcp.tool(structured_output=False)
async def crop_photo(left: float | None = None, top: float | None = None, right: float | None = None,
                     bottom: float | None = None, aspect: str | None = None, remove: bool = False,
                     preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Crop the open photo (crop module). left, top, right, bottom: edges as
    fractions of the uncropped photo (render_preview(uncropped=True));
    all four, or none to reshape the current crop with aspect. aspect:
    "free", "original", "square" or "W:H" ("3:2", "2:3" portrait), the
    largest such box centered in the given one. remove: no crop. Returns
    the geometry. Not saved until save. preview, measure as set_module."""
    edges = [left, top, right, bottom]
    if remove:
        if any(e is not None for e in edges) or aspect:
            raise ToolError("remove=True takes no edges or aspect")
        return await _and_look(await _edit(None, "geometry_set", crop=None), preview, measure)
    params: dict[str, Any] = {}
    if any(e is not None for e in edges):
        if any(e is None for e in edges):
            raise ToolError("give all four of left, top, right, bottom")
        params["crop"] = {"left": left, "top": top, "right": right, "bottom": bottom}
    if aspect is not None:
        params["aspect"] = aspect
    if not params:
        raise ToolError("give the crop's edges, an aspect, or remove=True")
    return await _and_look(await _edit(None, "geometry_set", **params), preview, measure)


@mcp.tool(structured_output=False)
async def render_preview(size: int = 800, uncropped: bool = False, zoom: float | None = None,
                         center_x: float = 0.5, center_y: float = 0.5, history_step: int | None = None,
                         show_mask: str | None = None, show_mask_instance: int = 0) -> Image:
    """darktable's render of the open photo with its current edit, fitted
    in size x size. Images are costly (every later request re-reads them):
    when a number answers the question (a color, a level, clipping), use
    measure_photo; for detail use zoom, not a bigger size.
    zoom: a size x size region at this scale (1 = 100%, up to 2) around
    center_x, center_y (fractions). uncropped=True: without the crop
    module's crop (the frame crop_photo uses). history_step: the edit
    after that many steps (0 = the original).
    show_mask: a module (operation, with show_mask_instance) whose mask to
    show, as the darkroom's "display mask" does: yellow where the module
    acts (its strength), over a gray image. One render settles a
    parametric range or a feather: use it after add_mask or set_blending
    instead of judging the effect, e.g. render_preview(show_mask="exposure",
    show_mask_instance=1). The module must be on and have a mask."""
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    region = None
    if show_mask is not None:
        await _require("render.show_mask")
        region = {"show_mask": {"operation": show_mask, "instance": show_mask_instance}}
    if zoom is not None:
        await _require("render.zoom")
        region = dict(region or {}, zoom=max(0.01, min(zoom, 2.0)), center_x=min(max(center_x, 0.0), 1.0),
                      center_y=min(max(center_y, 0.0), 1.0))
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
    if show_mask is not None and r[1].get("mask_shown") is False:
        raise ToolError(f"{show_mask} instance {show_mask_instance}'s mask is the same everywhere "
                        "(a drawn mask with no shapes, or a range that takes everything): nothing to show")
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
async def compare_before_after(before_step: int | None = None, show_user: bool = True, size: int = 640) -> list:
    """Before and after of the open photo: both renders (before first) for
    you, and with show_user for the user (the web app's slider; in
    darktable's darkroom a split snapshot). Nothing is undone.
    before_step: a history step (0 = the original); default: before your
    first edit of this photo, or where mark_before was."""
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
async def propose_alternatives(alternatives: list[dict], size: int = 640, show_user: bool = True) -> list:
    """Render 2-8 alternative versions of the open photo (crops, B&W filters,
    looks) for the user to choose from, without changing the edit; with
    show_user the web app shows them as a strip. Then apply_alternative,
    or keep_alternative (as a duplicate).
    alternatives: [{"label": "square", "changes": [{"tool": "crop_photo",
    "aspect": "square"}]}, ...]; a change is set_module, enable_module,
    apply_preset, set_blending, set_curve, crop_photo or rotate_photo with
    its arguments. Alternatives can't add instances: add one first,
    switched off, and let each alternative enable it."""
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
    """Keep one of the alternatives as a duplicate photo (the current saved
    edit plus that alternative, saved, named by its label); the open
    photo is unchanged. Unsaved changes: save_first=True saves them
    first (ask the user)."""
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
    """Export a photo (default: the open one) from its SAVED edit, as
    darktable's export does; what isn't given comes from the user's
    export settings. format "jpeg", "tiff", "png", "webp", "jpegxl", ...;
    size: longest side (0 = full); quality 1-100; high_quality: process at
    full size, then scale. path: a pattern without extension, darktable
    variables allowed, e.g. "$(FILE_FOLDER)/darktable_exported/$(FILE_NAME)";
    a folder ending in "/" gets the file name. on_conflict "unique",
    "overwrite", "overwrite_if_changed", "skip". style: applied on export.
    Unsaved changes are refused unless save_first (ask the user: they may
    be theirs). Returns the file written."""
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
    """Throw away unsaved changes: reopen the open photo as saved. Not
    for the photo open in darktable's darkroom (refused: darktable autosaves
    that edit itself); undo there with set_history_end."""
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
                        history_step: int | None = None, channels: bool = False,
                        markers: list[str] | None = None) -> dict:
    """Read the open photo as darktable renders it (display sRGB, as the
    darkroom shows it). points [[x, y], ...] and boxes [{left, top, right,
    bottom}], fractions of the photo: rgb (0-1), lab, luminance min/max
    (radius: the pixel square around a point); null outside the render.
    Also the luminance histogram (% of pixels per bin, dark to light;
    channels=True adds red, green, blue) and the fractions clipped in
    highlights and shadows. markers: names from list_markers (a point
    reads as a point, an area as its box; the results follow points and
    boxes). size, zoom, center_x, center_y, history_step as
    render_preview."""
    await _require("sample")
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    points, boxes = list(points or []), list(boxes or [])
    for name in markers or []:
        m = await _marker(name)
        if m["kind"] == "point":
            points.append(m["points"][0])
        else:
            boxes.append(m["box"])
    params: dict[str, Any] = {"width": max(64, min(size, 4096)), "height": max(64, min(size, 4096)),
                              "radius": max(0, min(radius, 200)), "bins": max(2, min(bins, 256)),
                              "points": points or [], "boxes": boxes or []}
    if zoom is not None:
        params.update(zoom=max(0.01, min(zoom, 2.0)), center_x=center_x, center_y=center_y)
    if history_step is not None:
        await _require("render.history_end")
        params["history_end"] = max(0, history_step)
    r = await _edit(None, "sample", **params)
    r.pop("process_ms", None)
    hist = r.get("histogram") or {}
    total = sum(hist.get("luminance") or []) or 1
    r["histogram"] = {ch: [round(100 * n / total, 1) for n in counts] for ch, counts in hist.items()
                      if channels or ch == "luminance"}
    if not channels:
        r["histogram"] = r["histogram"].get("luminance", [])
    return r


_ROMAN = ["0", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


@mcp.tool(structured_output=False)
async def zone_map(points: list[list[float]] | None = None, highlight: list[int] | None = None,
                   size: int = 800, history_step: int | None = None) -> list:
    """The open photo's render as Ansel Adams's zones 0-X (steps of 10 in
    Lab L; V = L 45-55), each pixel the flat gray of its zone. Returns the
    fraction of the photo in each zone, and the L and zone (e.g. 7.4) at
    points [[x, y], ...]. highlight: zones to mark in red, e.g. [0, 1]
    (blocked shadows) or [9, 10] (burnt highlights). history_step as
    render_preview. Meant for black and white."""
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
    """A module's blending: masks, blend_mode, opacity, feathering and
    the other values set_blending takes, drawn_shapes (count), the
    parametric ranges that are on, and the channels the module's color
    space offers (e.g. g, R, G, B, Jz, Cz, hz; or L, a, b, C, h)."""
    await _require("blend_get")
    return await _edit(None, "blend_get", operation=operation, instance=instance)


@mcp.tool(structured_output=False)
async def set_blending(operation: str, values: dict[str, Any], instance: int = 0,
                       preview: bool | dict = False, measure: dict | None = None) -> dict | list:
    """Change a module's blending as one history step (all or nothing).
    values (as get_blending): masks, blend_mode, reverse, opacity (0-100),
    blend_parameter, feathering_radius (0-250), blur_radius (0-100),
    brightness, contrast, details (-1..1), combine, feathering_guide,
    raster_source {"operation", "instance"} (another module's mask; null
    removes it), raster_inverted, and parametric: per channel and
    direction (e.g. "g_in") {"range": [low end, low full, high full, high
    end], "inverted": true} or null (off), in the darkroom's units; ranges
    switch the parametric mask on. g and R/G/B are linear on scene-referred
    modules (18 = middle gray, 9 = -1 EV), e.g. denoise only the shadows:
    set_blending("denoiseprofile", {"parametric": {"g_in": {"range": [0, 0, 6, 13]}}}).
    Ranges apply to this module's input, not the render: check with
    render_preview(zoom=...) or preview, measure as set_module."""
    await _require("blend_set")
    return await _and_look(await _edit(None, "blend_set", operation=operation, values=values, instance=instance),
                           preview, measure)


async def _photo_to_raw(points: list[list[float]]) -> tuple[list[list[float]], int, int]:
    r = await _edit(None, "coords", points=points, **{"from": "image", "to": "raw"})
    return r["points"], r["raw_width"], r["raw_height"]


async def _shape_on_raw(shape: str, x: float | None, y: float | None, radius: float,
                        radius_y: float | None, rotation: float, feather: float, compression: float,
                        points: list[list[float]] | None, width: float, hardness: float,
                        density: float, corners: str | None = None) -> dict:
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
        if corners not in (None, "smooth", "sharp"):
            raise ToolError('corners: "smooth" or "sharp"')
        if corners == "sharp":
            await _require("mask_add.corners")
            sh["corners"] = "sharp"
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
                   combine: str = "union", inverted: bool = False, instance: int = 0,
                   marker: str | None = None, corners: str | None = None) -> dict:
    """Draw a mask shape on a module so it acts only there (one history
    step). x, y: fractions 0-1 of the photo as shown; radius, feather:
    fractions of its shorter side. marker: a name from list_markers instead
    of shape/x/y/points: an area becomes a path through its points
    (feather), a point a circle (radius, feather).
    "circle": x, y, radius, feather. "ellipse": also radius_y, rotation
    (degrees). "gradient": a line through x, y at rotation (0 =
    horizontal), compression 0-1 (fade width). "path": a closed outline
    through points [[x, y], ...] (3 or more), feather; corners "smooth"
    (default: rounds off through the points) or "sharp" (a polygon; a rect
    marker's default). "brush": a stroke along points, width (half its
    thickness), hardness, density; corners as path (sharp: a polyline). combine with earlier shapes: union, intersection, difference,
    exclusion (darktable master also screen: a soft union, product, sum);
    inverted: act outside it."""
    await _require("mask_add", "coords")
    if marker:
        shape, x, y, points, mcorners = await _marker_shape(marker)
        corners = corners or await _marker_corners(mcorners)
    sh = await _shape_on_raw(shape, x, y, radius, radius_y, rotation, feather, compression, points,
                             width, hardness, density, corners)
    return await _edit(None, "mask_add", operation=operation, instance=instance, shape=sh,
                       combine=combine, inverted=inverted)


@mcp.tool()
async def add_ai_mask(operation: str, include: list[list[float]], exclude: list[list[float]] | None = None,
                      combine: str = "union", inverted: bool = False, instance: int = 0) -> dict:
    """Mask a module to an object with darktable's AI object mask (SAM),
    as one history step: include points [[x, y], ...] on it (fractions of
    the photo as shown; one is enough), exclude points beside it; its
    outline becomes path shapes. combine/inverted as add_mask. The first
    call on a photo takes a few seconds. Returns formid (for remove_mask),
    paths and area. Needs darktable with AI enabled."""
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
    type, combine, inverted, and where it is as add_mask takes it:
    on_photo (x, y), and for circles and ellipses radius (radius_y,
    rotation) and feather, so a shape can be redrawn."""
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
        raise ToolError(f"model: 'local' (LaMa, on this machine), 'flux-erase', 'flux' or 'flux3' (online), not '{model}'")
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
            fargs = dict(instance=inst, filled=str(work / "filled.png"),
                         model=f"{bfl.MODELS[model][1]} (Black Forest Labs)", context=context,
                         upscale=upscale, **extra)
            if await engine.supports("ai_fill.background"):
                # the upscale is minutes on a CPU: a job, which the engine
                # has read the PNG for already
                ev = await _run_job(lambda: _edit(image_id, "ai_fill", background=True, **fargs), True, 1800)
                state = ev.get("state")
                if state == "running":
                    return {**ev, "bfl": info,
                            "note": "the fill is still being upscaled: job_status(job) later; it places itself"}
                if state != "done":
                    raise ToolError(ev.get("error") or f"the fill job ended {state}")
                out = ev["result"]
            else:
                out = await _edit(image_id, "ai_fill", **fargs)
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
                  prompt: str = "", upscale: bool = False, online_size: int = 1024,
                  marker: str | None = None, corners: str | None = None) -> dict:
    """Remove an object or blemish: an AI inpainting model fills the area
    inside a shape from its surroundings, as a new "composite" instance
    called `name` masked by the shape (undo: enable_module or
    remove_module_instance; its mask edits like any other). The fill is a
    float TIFF beside the photo (darktable_fills/), in its group. Needs
    darktable master or 5.8 on, with AI and an inpaint model.

    shape as add_mask, on the photo as shown: "circle" (x, y, radius,
    feather), "ellipse" (+ radius_y, rotation), "path" (points, feather,
    corners "smooth" or "sharp"), "brush" (points, width, hardness,
    density). Outline a few pixels beyond the object.
    marker: fill a marker's area from list_markers (a path through its
    points, feather; a point marker a circle, radius) instead of a shape.
    like_mask_of: fill that module's mask shapes instead (instance
    like_mask_instance; like_mask_shapes: some of them, by name or formid).
    context 0.25-4: surroundings the model sees, in shape sizes (smaller
    keeps texture sharper; ~1 for sky).
    model "local" (LaMa, free, seconds), or online at Black Forest Labs,
    only when the user agreed to upload crops: "flux-erase", "flux"
    (prompt describes the scene), "flux3" (prompt: what to remove);
    online_size, upscale (the upscaler runs as a background job, minutes
    on a CPU; the tool waits for it). The darktable-cleanup skill says which model,
    and which shapes are too large to fill well.
    Show a "warning" in the reply to the user as it is; check the fill
    with render_preview(zoom=1)."""
    await _require("ai_fill", "coords")
    if marker:
        shape, x, y, points, mcorners = await _marker_shape(marker)
        corners = corners or await _marker_corners(mcorners)
        like_mask_of = None
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
                                 width, hardness, density, corners)
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
    """Add spots to the retouch module, as one history step. Each spot:
    {x, y, radius, tool, ...} on the photo as shown (radius a fraction of
    the shorter side, e.g. 0.01). tool "heal" (default) or "clone":
    source_x, source_y (required); "blur": blur_radius, blur_type
    ("gaussian", "bilateral"); "fill": fill_mode ("erase", "color"),
    fill_color [r, g, b], fill_brightness (-1..1). A spot may give
    "marker": a name from list_markers instead of x, y: its center (radius
    from the area's size when not given; 0.01 for a point). Returns the
    formids."""
    await _require("retouch_add", "coords")
    spots = [dict(sp) for sp in spots]
    for sp in spots:
        if sp.get("marker"):
            m = await _marker(sp.pop("marker"))
            sp["x"], sp["y"] = m["center"]
            if "radius" not in sp:
                if m["kind"] == "point":
                    sp["radius"] = 0.01
                else:
                    g = await _edit(None, "geometry_get")
                    b = m["box"]
                    sp["radius"] = round(max((b["right"] - b["left"]) * g["width"],
                                             (b["bottom"] - b["top"]) * g["height"]) / 2
                                         / min(g["width"], g["height"]), 5)
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


# ── red eye ──────────────────────────────────────────────────────────────────

async def _render_rgb(size: int = 2000):
    """The open photo as darktable renders it: float RGB 0-1, H x W x 3."""
    import cv2
    import numpy as np
    r = await engine.render(engine.image_id, size, size, False, None)
    if r is None:
        raise ToolError("overtaken by a newer render request")
    im = cv2.imdecode(np.frombuffer(r[0], np.uint8), cv2.IMREAD_COLOR)
    return im[:, :, ::-1].astype(np.float32) / 255.0


def _red_blobs(rgb, box: dict) -> list[dict]:
    """Round red blobs (r > 1.5 max(g, b)) in a box of the render: the two
    largest, as {x, y, radius} on the photo (radius: fraction of its
    shorter side)."""
    import cv2
    import numpy as np
    h, w = rgb.shape[:2]
    x0, x1 = int(max(0.0, box["left"]) * w), int(min(1.0, box["right"]) * w)
    y0, y1 = int(max(0.0, box["top"]) * h), int(min(1.0, box["bottom"]) * h)
    crop = rgb[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    red = (crop[..., 0] > 1.5 * np.maximum(crop[..., 1], crop[..., 2])) & (crop[..., 0] > 0.15)
    red = cv2.morphologyEx(red.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(red, connectivity=8)
    blobs = []
    for i in range(1, n):
        bw, bh, area = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT], stats[i, cv2.CC_STAT_AREA]
        if area < 6 or not 0.5 <= bw / bh <= 2.0 or area / (bw * bh) < 0.55:
            continue                          # a speck, a streak or a ragged patch: not a pupil
        r = (area / np.pi) ** 0.5
        blobs.append({"x": (x0 + cent[i][0]) / w, "y": (y0 + cent[i][1]) / h, "area_px": int(area),
                      "radius": float(r) * 1.25 / min(w, h)})
    blobs.sort(key=lambda b: -b["area_px"])
    return blobs[:2]


def _pupil_stats(rgb, x: float, y: float, radius: float):
    """(median red chroma r - max(g, b), brightest low-chroma point (x, y,
    luminance) or None) inside a disc of the render."""
    import numpy as np
    h, w = rgb.shape[:2]
    rr = max(2.0, radius * min(w, h))
    cx, cy = x * w, y * h
    ys, xs = np.mgrid[max(0, int(cy - rr)):min(h, int(cy + rr) + 1), max(0, int(cx - rr)):min(w, int(cx + rr) + 1)]
    inside = (xs - cx) ** 2 + (ys - cy) ** 2 <= rr * rr
    px = rgb[ys, xs][inside]
    if not len(px):
        return 0.0, None
    chroma = px[:, 0] - px[:, 1:].max(1)
    lum = px @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    low = chroma < 0.15
    catch = None
    if low.any():
        k = int(np.argmax(np.where(low, lum, -1)))
        catch = (float(xs[inside][k] / w), float(ys[inside][k] / h), float(lum[k]))
    return float(np.median(chroma)), catch


@mcp.tool()
async def fix_red_eye(marker: str | None = None, points: list[list[float]] | None = None,
                      method: str = "auto", radius: float | None = None) -> dict:
    """Remove red eye (a flash reflected from the retina) on the open
    photo. Name the eyes: points [[x, y], ...] one per eye (fractions of
    the photo as shown), or marker (list_markers): a point marker is one
    eye, an area marker a box to search: the tool finds the red round
    pupils in it (the two largest). radius: the pupil's radius as a
    fraction of the photo's shorter side (default 0.012, or what the
    search measured).

    method "desaturate": a new color balance rgb instance called "red eye"
    takes the colour out and darkens, masked by a circle on each pupil AND
    a parametric range on red hue and chroma, so a catchlight (bright,
    unsaturated) and the iris stay. "fill": the local AI model (LaMa)
    fills each pupil, then a dark spot restores the pupil and a small
    one the catchlight; for blown pink-white pupils with no red left.
    "flux": as fill with the online FLUX model (costs credits, sends the
    crop online: only when the user asked). "auto" (default): red pupils
    are desaturated, blown ones filled; the reply says which.

    A pet's eyeshine (green, yellow, white) is not red: use method="fill"
    (or "flux" when the user asks). Check with render_preview(zoom=1) at
    each eye. The edit is unsaved until save. Returns method, eyes (x, y,
    area, shape), instance, formids, hint."""
    import math
    if method not in ("auto", "desaturate", "fill", "flux"):
        raise ToolError('method: "auto", "desaturate", "fill" or "flux"')
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    await _require("mask_add", "coords")
    rgb = None
    eyes: list[dict] = []
    if points:
        for p in points:
            if len(p) != 2:
                raise ToolError("points: [[x, y], ...] fractions of the photo")
            eyes.append({"x": float(p[0]), "y": float(p[1]), "radius": radius})
    elif marker:
        m = await _marker(marker)
        if m["kind"] == "point":
            eyes.append({"x": m["points"][0][0], "y": m["points"][0][1], "radius": radius})
        else:
            rgb = await _render_rgb()
            b = m["box"]
            eyes = [dict(e, radius=radius or e["radius"]) for e in _red_blobs(rgb, b)]
            if not eyes:
                raise ToolError(f"no red round pupil found in marker '{marker}' (box {b['left']:.3f}-{b['right']:.3f} "
                                f"x {b['top']:.3f}-{b['bottom']:.3f}): give points=[[x, y], ...] on the eyes")
    else:
        raise ToolError("name the eyes: points=[[x, y], ...] (one per eye) or marker=<a point or an area to search>")
    for e in eyes:
        if not (0.0 <= e["x"] <= 1.0 and 0.0 <= e["y"] <= 1.0):
            raise ToolError("points are fractions 0-1 of the photo")
        e["radius"] = max(0.002, min(float(e["radius"] or 0.012), 0.2))
    chosen = method
    if method == "auto":
        rgb = rgb if rgb is not None else await _render_rgb()
        red = [_pupil_stats(rgb, e["x"], e["y"], e["radius"] * 0.8)[0] for e in eyes]
        chosen = "desaturate" if sum(red) / len(red) > 0.12 else "fill"
    g = await _edit(None, "geometry_get")
    W, H = g["width"], g["height"]
    short = min(W, H)
    result: dict[str, Any] = {"method": chosen, "eyes": [], "instance": None, "formids": []}

    if chosen == "desaturate":
        await _require("module_add", "module_rename", "blend_set")
        inst = (await _edit(None, "module_add", operation="colorbalancergb", instance=0, copy=False))["instance"]
        await _edit(None, "module_set", operation="colorbalancergb", instance=inst,
                    values={"chroma_global": -1.0, "saturation_global": -1.0, "brilliance_global": -0.5})
        await _edit(None, "module_rename", operation="colorbalancergb", instance=inst, name="red eye")
        for e in eyes:
            circle = math.pi * e["radius"] ** 2 * short * short / (W * H)
            shape, area, fid = "circle", circle, None
            if await engine.supports("mask_ai"):
                try:
                    r = await add_ai_mask("colorbalancergb", [[e["x"], e["y"]]], instance=inst)
                    if 0.4 * circle <= r.get("area", 0) <= 4 * circle:
                        shape, area, fid = "ai", r["area"], r.get("formid")
                    elif r.get("formid") is not None:       # a catchlight alone, or the whole face
                        await remove_mask("colorbalancergb", r["formid"], instance=inst)
                except ToolError:
                    pass                         # no AI model here: the circle
            if shape == "circle":
                r = await add_mask("colorbalancergb", "circle", e["x"], e["y"], radius=e["radius"],
                                   feather=e["radius"] * 0.3, instance=inst)
                fid = r.get("formid")
            result["eyes"].append({"x": round(e["x"], 4), "y": round(e["y"], 4), "area": round(area, 6), "shape": shape})
            if fid is not None:
                result["formids"].append(fid)
        # drawn AND parametric: darktable's "exclusive" combine; red is hue
        # ~ 0-70 deg in JzCzhz, so the range is the red arc through 0 (given
        # inverted: everything but the arc from 80 to 330); Cz is 0-100 there,
        # the floor of 3-8 leaves out a catchlight and gray
        await _edit(None, "blend_set", operation="colorbalancergb", instance=inst, values={
            "masks": "drawn & parametric", "combine": "exclusive", "feathering_radius": 2.0,
            "parametric": {"hz_in": {"range": [80, 100, 310, 330], "inverted": True},
                           "Cz_in": {"range": [3, 8, 100, 100]}}})
        result["instance"] = inst
        result["hint"] = ("render_preview(zoom=1) at an eye; a pupil still pink: widen it with radius, or "
                          "method='fill'; show_mask='colorbalancergb' with show_mask_instance shows the mask")
        return result

    # fill / flux: the local or online model fills each pupil, a dark spot
    # then makes it a pupil and a small bright one is the catchlight
    model = "flux" if chosen == "flux" else "local"
    prompt = "natural dark pupil with a small catchlight" if model == "flux" else ""
    await _require("ai_fill", "retouch_add")
    rgb = rgb if rgb is not None else await _render_rgb()
    spots = []
    for e in eyes:
        _c, catch = _pupil_stats(rgb, e["x"], e["y"], e["radius"])
        sh = await _shape_on_raw("circle", e["x"], e["y"], e["radius"], None, 0.0, e["radius"] * 0.3, 0.5,
                                 None, 0.03, 0.5, 1.0)
        r = await _ai_fill(None, sh, 2.0, "red eye", model, prompt, False, 1024)
        result["instance"] = r.get("instance", result["instance"])
        spots.append({"x": e["x"], "y": e["y"], "radius": e["radius"] * 0.85, "tool": "fill",
                      "fill_mode": "color", "fill_color": [0.03, 0.03, 0.03]})
        if catch and catch[2] > 0.75:
            spots.append({"x": catch[0], "y": catch[1], "radius": max(0.0015, e["radius"] * 0.18),
                          "tool": "fill", "fill_mode": "color", "fill_color": [0.95, 0.95, 0.95]})
        area = math.pi * e["radius"] ** 2 * short * short / (W * H)
        result["eyes"].append({"x": round(e["x"], 4), "y": round(e["y"], 4), "area": round(area, 6), "shape": "circle"})
    rr = await retouch_spots(spots)
    result["formids"] = rr.get("formids") or rr.get("spots") or []
    result["hint"] = ("render_preview(zoom=1) at each eye; the fill is an instance called 'red eye' "
                      "(enable_module off to undo), the pupil and catchlight are retouch spots")
    return result


# ── styles and copy/paste ────────────────────────────────────────────────────

@mcp.tool()
async def list_styles(filter: str = "", camera_styles: bool = False) -> dict:
    """darktable's styles: name (for apply_style), label, description, its
    modules, whether it sets the module order. filter matches names;
    camera_styles adds darktable's per-camera base curve styles."""
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
    """Copy one photo's saved edit (default: the open photo) onto others, as
    darktable's copy and paste: all, or only modules (names or
    {"operation", "instance"}). mode "append" or "overwrite". Saved; a
    target with unsaved changes is refused unless save_first. module_order
    copies the module order too."""
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
    """Find sensor dust in a photo (default: the open one; opens it).
    Read-only. Maps the soft dark blobs that recur across the photo's film
    roll (cached; the first call reads every raw, ~0.4 s each), then rates
    each in this photo: "obvious" (repair), "visible" (seen through some
    texture), "hidden" (leave it). this_photo=True also searches this
    photo's render alone (ids from 101, a sheet of their own: heal only the
    round soft shadows); left out, it is on for black and white edits.
    Returns a summary, per spot status, already_retouched, size and
    on_photo (x, y for render_preview(zoom=1)), and numbered crop sheets
    to look at. heal_dust_spots repairs them; the darktable-cleanup skill
    has the workflow."""
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
    """Heal the dust find_dust_spots found (on the same edit, so ids match;
    default: the open photo). Default: "obvious" spots not yet retouched;
    include_visible adds "visible" ones; spots=[ids] picks (ids from 101
    only this way, after looking at their sheet).
    method "auto" (heal circles; an AI fill where no clean source is
    nearby, skipped on 5.6), "heal", or "ai_fill" (smooths texture: not on
    ground or foliage). Heals are one retouch history step; fills are
    "dust #id" composite instances. Not saved until save; undo with
    set_history_end. Checks each spot at 100%: returns darkening
    before/after and a before/after sheet. dry_run=True: the plan only."""
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
    """Whether the sensor really clipped: photosites at the raw's white
    level per color, and each color's headroom (how far its brightest 0.1%
    is below clipping, in EV). Clipped on the sensor: only the highlights
    module can rebuild it; merely bright in the render: recoverable with
    exposure or the tone mapper. Also the share of 2x2 blocks fully clipped
    (all_clipped_pct) and blocked_shadows_pct: shadows more than ~12 EV below
    white at base ISO (less at higher ISO) are noise even after denoise, a
    +3 EV push won't bring detail back. Needs the [dust] extra (rawpy)."""
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
    reply = await _call("image_info", imgid=image_id)
    info = reply["image"]
    path = str(Path(info["folder"]) / info["filename"])
    iso = (reply.get("exif") or {}).get("iso")

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
    try:
        from . import clipping
        blocks = (await asyncio.to_thread(clipping.measure, path, iso))["shares"]
    except Exception:
        blocks = {}
    return {"image_id": image_id, "file": path, "channels": channels, "sensor_clipped": bool(clipped),
            **{k: blocks[k] for k in ("all_clipped_pct", "blocked_shadows_pct") if k in blocks},
            "verdict": (f"sensor saturated in {', '.join(clipped)} ({worst:.2f}% of photosites at worst): that "
                        "detail wasn't recorded; highlight reconstruction can only fill it in"
                        if clipped else
                        "no significant sensor saturation: bright areas in the render are recoverable with "
                        "the tone mapper, exposure or tone equalizer")}


@mcp.tool()
async def make_panorama(photo_ids: list[int], fixes: dict | None = None, projection: str = "auto",
                        match_edits_from: int | None = None, crop: bool = True, in_order: bool = False,
                        name: str | None = None, folder: str | None = None,
                        apply_to_originals: bool = False) -> dict:
    """Stitch photos into a panorama with Hugin. Two steps. First call
    without fixes: it returns the prerequisites report (missing lens
    correction or highlight reconstruction, white balance or exposure that
    differs, per-frame looks such as a tone mapper or tone equalizer that
    make seams show, masks) plus a question and the answers' fixes. Show it
    to the user, ask the question, then call again with the chosen answer's
    fixes ({"add_missing": bool, "remove_unwanted": bool, "move_to_panorama":
    bool}). Everything is done on virtual copies used only for the export and
    removed afterwards: the frames and their edits are not changed (unless
    apply_to_originals=True). match_edits_from: the frame whose white
    balance, exposure ... the others get. projection: auto (cylindrical),
    rectilinear, cylindrical, equirectangular. crop=True cuts to the filled
    rectangle. in_order=True when the frames are in shooting order along one
    row: the retry when matching fails on skies or water. Writes
    <first frame>-pano.tif beside the frames (or in folder), 16-bit, Deflate
    (lossless), roughly 200-400 MB for 3 frames before compression, plus a
    small .pto showing the alignment; imported, tagged panorama|from ...,
    grouped with its frames as leader. Takes a minute or a few. The result
    is a new photo to edit like any other (open_photo). Needs Hugin's
    command-line tools (DTAPI_HUGIN_BIN or ~/.local/hugin/bin)."""
    await _require("export.bpp", "image_duplicate", "history_paste")
    try:
        if fixes is None:
            return await panorama.check(engine, photo_ids, match_edits_from)
        return await panorama.stitch(engine, photo_ids, fixes=fixes, projection=projection,
                                     match_edits_from=match_edits_from, crop=crop, in_order=in_order, name=name,
                                     folder=folder, apply_to_originals=apply_to_originals)
    except panorama.PanoramaError as exc:
        raise ToolError(str(exc))
    except EngineError as exc:
        raise _err(exc)


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
    """Denoise a raw with darktable's AI raw denoise (RawNIND): writes a new
    DNG beside the original, imported into the photo's group with its
    rating, tags and metadata, unedited: open_photo(new_imgid) and edit
    that one. strength 0-1 blends with the original (default: darktable's
    setting). A job (seconds to minutes): wait=True returns new_imgid and
    file. Needs darktable with AI enabled."""
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
    """A module's color pickers and auto buttons (name, kind) for
    use_picker, e.g. exposure "exposure", channelmixerrgb "picker", agx
    "exposure range/auto tune levels", toneequal "mask exposure
    compensation". Pickers work on any photo, auto buttons only on the
    photo in darktable's darkroom."""
    await _require("picker_list")
    return await _edit(None, "picker_list", operation=operation, instance=instance)


@mcp.tool()
async def use_picker(operation: str, control: str, box: dict | None = None, point: list[float] | None = None,
                     instance: int = 0, wait: bool = True, timeout_s: float = 60,
                     marker: str | None = None) -> dict:
    """Use a module's picker or auto button (names from list_pickers) as
    clicking it in the darkroom: darktable samples the area and sets the
    module (switched on, one history step). Pickers work on any photo;
    auto buttons only on the photo in darktable's darkroom. Common:
    exposure "exposure" (the area to mid gray), channelmixerrgb "picker"
    (white balance making the area neutral; prefer it to temperature's
    "settings/from image area"), agx "exposure range/auto tune levels",
    monochrome "highlights" (filter color from the area).
    box {left, top, right, bottom} or point [x, y], fractions of the photo
    as shown; neither: darktable's default area; buttons take none.
    marker: a name from list_markers instead (an area gives its bounding
    box, a point the point).
    Returns result.changed and result.sample (the module's input,
    mean/min/max; result.output_sample: its output)."""
    await _require("picker_apply")
    if marker:
        m = await _marker(marker)
        box, point = (None, m["points"][0]) if m["kind"] == "point" else (m["box"], None)
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
    """Who serves the library ("server": "gui" or "engine"), whether it is
    released to darktable's GUI, darkroom_imgid (the photo in darktable's
    darkroom, 0 if none), the current photo and whether it has unsaved
    changes."""
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


def _slim_schema(node: Any) -> Any:
    """A parameter schema without pydantic's titles, and `X | None = None`
    as plain X (left out means None): the same arguments in fewer tokens
    (every request re-reads them). Arguments are still checked against the
    functions' own signatures."""
    if isinstance(node, list):
        return [_slim_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties":
            out[key] = {name: _slim_schema(prop) for name, prop in value.items()}
        else:
            out[key] = _slim_schema(value)
    options = out.get("anyOf")
    if options and len(options) == 2 and {"type": "null"} in options:
        other = next(o for o in options if o != {"type": "null"})
        if isinstance(other.get("type"), str) and out.get("default", 0) is None:
            del out["anyOf"], out["default"]
            out = {**other, **out}
    return out


for _tool in mcp._tool_manager.list_tools():
    _tool.parameters = _slim_schema(_tool.parameters)


def _compact_replies() -> None:
    """Replies as compact JSON: FastMCP writes a tool's dict as indented
    JSON text (one line per array element), and that text is what the
    model reads; compact, a reply costs about half the tokens. Lists
    (a reply and its preview) are converted item by item, as before."""
    import pydantic_core
    from mcp.server.fastmcp.utilities import func_metadata
    convert = getattr(func_metadata, "_convert_to_content", None)
    if convert is None or getattr(convert, "_compact", False):
        return    # another mcp version: its own output, still correct

    def compact(result: Any):
        if isinstance(result, dict):
            result = pydantic_core.to_json(result, fallback=str).decode()
        return convert(result)

    compact._compact = True
    func_metadata._convert_to_content = compact


_compact_replies()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
