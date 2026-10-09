"""
darktable-api-mcp: an MCP server that browses a darktable library and edits
photos through darktable-api, a long-running headless darktable.

Everything goes through the engine: darktable's own code renders, applies
edits and writes history to the library. No darktable GUI, Lua or XMP
patching is involved.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .engine import EngineError, engine

INSTRUCTIONS = """\
Edits photos in a darktable library through darktable-api (a headless
darktable). Typical flow: list_images -> open_photo(id) -> get_module /
set_module (several times, checking render_preview) -> save. Edits stay in
memory until save; discard_changes reopens the saved state; start_over
applies darktable's defaults again.

darktable is scene-referred: set exposure first (midtones of the subject),
white balance in color calibration (channelmixerrgb), then the tone mapper
(agx, or sigmoid/filmicrgb on older edits; never two), then tone equalizer
(toneequal) and color balance rgb (colorbalancergb). Use get_module to learn a
module's setting names, ranges and dropdown values before set_module.
Orientation, straightening and cropping: get_geometry, rotate_photo,
crop_photo (render_preview(uncropped=True) shows the whole photo to choose a
crop on). export_photo writes a finished file with darktable's export.

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
    rating: "visible" (not rejected, default), "all", "rejected", or "1".."5"
            (at least that many stars).
    label: "red", "yellow", "green", "blue" or "purple" to filter by label.
    offset, limit: paging (limit up to 1000); "total" is the full count."""
    return await _call("images_list", offset=offset, limit=limit, **_filters(film_roll_id, rating, label))


@mcp.tool()
async def image_info(image_id: int) -> dict:
    """One photo's library entry (as in list_images)."""
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
    agx, colorbalancergb, channelmixerrgb, toneequal), instance, name, enabled,
    whether it is in the history."""
    return await _edit(None, "module_list")


@mcp.tool()
async def get_module(operation: str, instance: int = 0) -> dict:
    """A module's settings on the open photo: for each setting its name,
    current value, default, allowed range (min/max) and, for dropdowns, the
    possible values (name and label). Use these names in set_module."""
    return await _edit(None, "module_get", operation=operation, instance=instance)


@mcp.tool()
async def set_module(operation: str, values: dict[str, Any], instance: int = 0) -> dict:
    """Change settings of a module on the open photo, e.g.
    set_module("exposure", {"exposure": 0.7}). Names and ranges come from
    get_module; dropdown values by name, label or number. All values are
    checked first: one bad value changes nothing. Turns the module on and
    adds a history step, as darktable's darkroom does. Not saved until save."""
    return await _edit(None, "module_set", operation=operation, values=values, instance=instance)


@mcp.tool()
async def enable_module(operation: str, enabled: bool, instance: int = 0) -> dict:
    """Turn a module on or off on the open photo (keeps its settings)."""
    return await _edit(None, "module_enable", operation=operation, enabled=enabled, instance=instance)


@mcp.tool()
async def get_history() -> dict:
    """The open photo's history steps: num (0-based; darktable's history panel
    shows num + 1), operation, instance, enabled, applied; and history_end, the
    number of steps applied."""
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
async def render_preview(size: int = 1200, uncropped: bool = False) -> Image:
    """darktable's rendering of the open photo with its current (unsaved)
    edit, fitted inside size x size. Use it to check edits. uncropped=True
    shows the whole photo without the crop module's crop (still oriented
    and straightened), the frame crop_photo's fractions refer to."""
    if engine.image_id is None:
        raise ToolError("no photo is open: call open_photo(image_id) first")
    try:
        r = await engine.render(engine.image_id, max(64, min(size, 2560)), max(64, min(size, 2560)),
                                uncropped)
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
        return await engine.call("export", **params)
    except EngineError as exc:
        if "unsaved" in str(exc):
            raise ToolError("the photo has unsaved changes and export uses the saved edit: save, or pass "
                            "save_first=True (both save the whole shared edit: ask the user)")
        raise _err(exc)


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
