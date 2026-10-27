"""
Panoramas from photos in the library, stitched with Hugin's command-line tools.

check() reads the frames' edits and reports what would make the seams show
(no lens correction, white balance that differs, a tone mapper that adapts to
each frame ...); stitch() makes virtual copies of the frames, fixes them as
asked, exports them as 16-bit TIFFs, runs Hugin, imports the result beside the
frames, tags and groups it, and removes the copies. The frames and their edits
are not changed (unless apply_to_originals).

Plain functions on an engine client (engine.py), no import of server.py: the
MCP tool (server.make_panorama) and the web app's worker call them directly.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .engine import EngineError


class PanoramaError(Exception):
    """A problem with a message to show as it is."""


HUGIN_TOOLS = ("pto_gen", "cpfind", "cpclean", "linefind", "autooptimiser", "checkpto", "pano_modify",
               "hugin_executor")
PROJECTIONS = {"rectilinear": 0, "cylindrical": 1, "equirectangular": 2}
STEP_TIMEOUT_S = 900
SPAN_WARN_S = 300
CLIP_WARN_PCT = 0.5

# modules that adapt to each frame's own content or change the look: left on, the seams show
UNWANTED = {
    "agx": "AgX is a tone mapper that adapts to each frame's brightness, so frames won't match at the seams",
    "sigmoid": "sigmoid is a tone mapper that adapts to each frame's brightness, so frames won't match at the seams",
    "filmicrgb": "filmic rgb is a tone mapper that adapts to each frame's brightness, so frames won't match at the seams",
    "filmic": "filmic is a tone mapper that adapts to each frame's brightness, so frames won't match at the seams",
    "spektrafilm": "the film simulation is a tone mapper that adapts to each frame, so frames won't match at the seams",
    "toneequal": "tone equalizer adjusts each frame to its own content, so frames won't match at the seams",
    "bilat": "local contrast works from each frame's own content, so frames won't match at the seams",
    "hazeremoval": "haze removal estimates the haze per frame, so frames won't match at the seams",
    "diffuse": "diffuse or sharpen works per frame, so the seams can show",
    "crop": "a crop cuts the frame before stitching and removes overlap",
    "clipping": "a crop cuts the frame before stitching and removes overlap",
    "ashift": "perspective correction bends each frame differently; Hugin does the geometry",
    "vignette": "a vignette darkens every frame's edges, which shows at the seams",
    "grain": "grain is added per frame; add it to the panorama instead",
    "borders": "a border frames each photo; add it to the panorama instead",
    "sharpen": "sharpening each frame shows at the seams; sharpen the panorama instead",
    "liquify": "liquify moves pixels in one frame, so it won't match its neighbor",
    "overlay": "a composite (ai_fill) patch belongs to its own frame's content and position",
    "retouch": "fine if it is dust healing; other retouching is better done on the panorama",
}
KEEP_BY_DEFAULT = {"retouch"}
# of those, the ones that make the look: carried over to the panorama
LOOK = ("agx", "sigmoid", "filmicrgb", "filmic", "spektrafilm", "toneequal", "bilat", "hazeremoval", "diffuse",
        "sharpen")
# modules whose blending says nothing about the frame's content
NO_BLEND = {"rawprepare", "temperature", "highlights", "demosaic", "demosaicscale", "lens", "flip", "colorin",
            "colorout", "gamma", "finalscale", "overexposed", "rawoverexposed", "denoiseprofile", "rawdenoise",
            "nlmeans", "exposure", "channelmixerrgb"}
BETTER_AFTER = {"colorbalancergb": "color balance rgb", "rgbcurve": "rgb curve", "rgblevels": "rgb levels",
                "colorzones": "color zones"}
DENOISE = ("denoiseprofile", "rawdenoise", "nlmeans")
DIFFERS = {"temperature": "white balance differs, so the seams show as color steps",
           "channelmixerrgb": "color calibration differs, so the seams show as color steps",
           "exposure": "exposure differs, so the seams show as brightness steps"}
MASKED = ("drawn", "parametric", "drawn & parametric", "raster")
LABELS = {"lens": "lens correction", "highlights": "highlight reconstruction", "denoise": "denoise",
          "agx": "AgX", "sigmoid": "sigmoid", "filmicrgb": "filmic rgb", "filmic": "filmic", "spektrafilm": "film simulation",
          "toneequal": "tone equalizer", "bilat": "local contrast", "hazeremoval": "haze removal", "diffuse": "diffuse or sharpen",
          "crop": "crop", "clipping": "crop", "ashift": "perspective", "vignette": "vignette", "grain": "grain",
          "borders": "borders", "sharpen": "sharpen", "liquify": "liquify", "overlay": "composite (ai_fill)",
          "retouch": "retouch", "temperature": "white balance", "channelmixerrgb": "color calibration",
          "exposure": "exposure"}


def _label(op: str) -> str:
    return LABELS.get(op, op)


# ── Hugin ────────────────────────────────────────────────────────────────────

def find_hugin() -> Path:
    """The folder with Hugin's command-line tools: DTAPI_HUGIN_BIN if set (no
    fallback), else ~/.local/hugin/bin, else wherever pto_gen is on the PATH."""
    msg = ("Hugin's command-line tools aren't in {where}; install Hugin "
           "(" + ", ".join(HUGIN_TOOLS) + ")")
    env = os.environ.get("DTAPI_HUGIN_BIN")
    if env:
        d = Path(env).expanduser()
        if (d / "pto_gen").is_file():
            return d
        raise PanoramaError(msg.format(where=f"DTAPI_HUGIN_BIN={env}"))
    d = Path.home() / ".local/hugin/bin"
    if (d / "pto_gen").is_file():
        return d
    found = shutil.which("pto_gen")
    if found:
        return Path(found).resolve().parent
    raise PanoramaError(msg.format(where="~/.local/hugin/bin or on the PATH"))


async def _run(cmd: list[str], cwd: Path, hugin: Path, timeout: float = STEP_TIMEOUT_S) -> str:
    """Runs a tool; its stdout and stderr together. PanoramaError with the
    stderr's tail if it fails or takes too long."""
    env = {**os.environ, "PATH": f"{hugin}{os.pathsep}{os.environ.get('PATH', '')}"}
    name = Path(cmd[0]).name
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        raise PanoramaError(f"{name} can't run: {exc}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PanoramaError(f"{name} took more than {int(timeout // 60)} minutes and was stopped")
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = "\n".join(text.strip().splitlines()[-12:])
        raise PanoramaError(f"{name} failed (exit {proc.returncode}): {tail}")
    return text


# ── the frames' edits ────────────────────────────────────────────────────────

def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-4
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _values(reply: dict) -> dict:
    """A module_get reply as {field name: value}, plus its on/off."""
    out: dict = {"enabled": bool(reply.get("enabled"))}
    for f in reply.get("fields") or []:
        if "name" in f:
            out[f["name"]] = f.get("value")
    return out


def _seconds(taken: str | None) -> float | None:
    try:
        return datetime.strptime(taken or "", "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _ids_text(ids: list[int]) -> str:
    """442-444 for a run of ids, else 442, 450, 451."""
    ids = list(ids)
    if len(ids) > 1 and ids == list(range(ids[0], ids[0] + len(ids))):
        return f"{ids[0]}-{ids[-1]}"
    return ", ".join(str(i) for i in ids)


async def _frame(engine, imgid: int) -> dict:
    try:
        info = await engine.call("image_info", imgid=imgid)
    except EngineError as exc:
        raise PanoramaError(f"photo {imgid}: {exc}")
    img, ex = info.get("image") or {}, info.get("exif") or {}
    mods = (await engine.edit(imgid, "module_list"))["modules"]
    frame = {"id": imgid, "file": img.get("filename"), "folder": img.get("folder"),
             "width": img.get("width"), "height": img.get("height"),
             "camera": " ".join(x for x in (ex.get("maker"), ex.get("model")) if x) or None,
             "lens": ex.get("lens") or None, "focal_length": ex.get("focal_length") or None,
             "crop_factor": ex.get("crop_factor") or None, "taken": ex.get("taken"),
             "modules": mods, "values": {}, "masked": {}}
    on = {(m["operation"], m["instance"]): m for m in mods if m.get("enabled")}
    for op in DIFFERS:
        inst = 0
        if (op, inst) in on or any(m["operation"] == op and m["instance"] == inst for m in mods):
            try:
                frame["values"][op] = _values(await engine.edit(imgid, "module_get", operation=op, instance=inst))
            except EngineError:
                pass
    for (op, inst), m in on.items():
        if op in NO_BLEND or op in UNWANTED or not m.get("in_history"):
            continue
        try:
            masks = (await engine.edit(imgid, "blend_get", operation=op, instance=inst)).get("masks")
        except EngineError:
            continue
        if masks in MASKED:
            frame["masked"][(op, inst)] = masks
    return frame


def _enabled(frame: dict, op: str, inst: int | None = None) -> bool:
    return any(m["operation"] == op and m.get("enabled") and (inst is None or m["instance"] == inst)
               for m in frame["modules"])


def _is_masked(frame: dict, m: dict) -> bool:
    return (m["operation"], m["instance"]) in frame["masked"]


def _unwanted(frames: list[dict]) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for fr in frames:
        for m in fr["modules"]:
            if not m.get("enabled") or m["operation"] == "flip":
                continue
            op, inst = m["operation"], m["instance"]
            if op in UNWANTED:
                why = UNWANTED[op]
            elif _is_masked(fr, m):
                why = "a mask follows each frame's own content"
            else:
                continue
            row = rows.setdefault((op, inst), {"module": op, "instance": inst, "frames": [], "why": why})
            if op in KEEP_BY_DEFAULT:
                row["keep_by_default"] = True
            row["frames"].append(fr["id"])
    return list(rows.values())


def _missing(frames: list[dict]) -> tuple[list[dict], list[dict]]:
    missing = []
    for op, why in (("lens", "no lens correction: the frames' edges and distortion won't line up"),
                    ("highlights", "no highlight reconstruction: clipped highlights show pink or flat at the seams")):
        ids = [f["id"] for f in frames if not _enabled(f, op)]
        if ids:
            missing.append({"module": op, "frames": ids, "why": why})
    ids = [f["id"] for f in frames if not any(_enabled(f, op) for op in DENOISE)]
    if ids:
        missing.append({"module": "denoise", "frames": ids,
                        "why": "no denoise: the noise differs from frame to frame and the seams show it"})
    differs = []
    for op, why in DIFFERS.items():
        have = [f for f in frames if op in f["values"]]
        if len(have) < 2:
            continue
        names: dict[str, dict] = {}
        for key in {k for f in have for k in f["values"][op]}:
            vals = {f["id"]: f["values"][op].get(key) for f in have}
            if not all(_same(vals[have[0]["id"]], v) for v in vals.values()):
                names[key] = vals
        if names:
            differs.append({"module": op, "fields": names, "why": why})
    return missing, differs


async def _analyze(engine, photo_ids: list[int], match_edits_from: int | None) -> tuple[dict, list[dict]]:
    ids = list(photo_ids or [])
    if len(ids) < 2:
        raise PanoramaError("a panorama needs at least two photos")
    if len(set(ids)) != len(ids):
        raise PanoramaError("the same photo is listed twice")
    if match_edits_from is not None and match_edits_from not in ids:
        raise PanoramaError(f"match_edits_from={match_edits_from} is not one of the frames")
    frames = [await _frame(engine, i) for i in ids]
    ref = match_edits_from if match_edits_from is not None else ids[0]

    warnings = []
    for key, what in (("camera", "cameras"), ("lens", "lenses"), ("focal_length", "focal lengths")):
        vals = {f["id"]: f[key] for f in frames}
        if len(set(vals.values())) > 1:
            warnings.append(f"the frames have different {what}: {vals}")
    times = [t for t in (_seconds(f["taken"]) for f in frames) if t is not None]
    if times and max(times) - min(times) > SPAN_WARN_S:
        warnings.append(f"the frames were taken over {(max(times) - min(times)) / 60:.0f} minutes: "
                        "light and moving things may have changed")

    missing, differs = _missing(frames)
    unwanted = _unwanted(frames)
    better = []
    for fr in frames:
        for m in fr["modules"]:
            op, inst = m["operation"], m["instance"]
            if not m.get("enabled") or _is_masked(fr, m):
                continue
            if op in BETTER_AFTER or (op == "channelmixerrgb" and inst > 0):
                row = next((r for r in better if r["module"] == op and r["instance"] == inst), None)
                if row is None:
                    row = {"module": op, "instance": inst, "frames": [], "why": "better on the panorama"}
                    better.append(row)
                row["frames"].append(fr["id"])

    report: dict = {
        "frames": [{k: f[k] for k in ("id", "file", "camera", "lens", "focal_length", "taken")} for f in frames],
        "warnings": warnings, "missing": missing, "differs": differs, "unwanted": unwanted,
        "better_after": better,
        "dust": "not checked here: find_dust_spots(image_id) per frame if the frames have sky",
        "note": ("Fixes and removals are made on virtual copies used only for the export and removed afterwards; "
                 "your frames and their edits are not changed (unless apply_to_originals=True)."),
        "match_edits_from": ref,
    }
    if differs or missing:
        report["match_note"] = f"the prerequisite values (white balance, color calibration, exposure ...) come from frame {ref}"

    parts = []
    if missing:
        parts.append("missing: " + "; ".join(f"{_label(r['module'])} on {_ids_text(r['frames'])}" for r in missing))
    if differs:
        parts.append("differing: " + ", ".join(_label(r["module"]) for r in differs))
    removable = [r for r in unwanted if not r.get("keep_by_default")]
    if removable:
        parts.append("per-frame looks: " + "; ".join(
            f"{_label(r['module'])}{'' if r['instance'] == 0 else ' ' + str(r['instance'])} on {_ids_text(r['frames'])}"
            for r in removable))
    if parts:
        report["question"] = (" | ".join(parts) + f". Make the frames match frame {ref}'s prerequisites and remove the "
                              "per-frame looks (carrying the look over to the panorama), or fix the missing ones only, "
                              "or stitch as is?")
    else:
        report["question"] = "The frames look ready: stitch them as they are?"
    report["answers"] = {
        "fix both": {"add_missing": True, "remove_unwanted": True, "move_to_panorama": True},
        "fix missing only": {"add_missing": True, "remove_unwanted": False},
        "stitch as is": {"add_missing": False, "remove_unwanted": False},
    }
    return report, frames


async def check(engine, photo_ids: list[int], match_edits_from: int | None = None) -> dict:
    """What would make the seams of a panorama of these photos show, from
    their edits. Reads only."""
    report, _ = await _analyze(engine, photo_ids, match_edits_from)
    return report


# ── jobs ─────────────────────────────────────────────────────────────────────

async def _job(engine, start, timeout_s: float) -> dict:
    """Starts a job (start() returns the engine's reply) and waits for its
    "job" event; the event's result."""
    done: dict = {}
    finished = asyncio.Event()

    def listener(ev: dict) -> None:
        if ev.get("type") == "job" and ev.get("job") == done.get("job"):
            done["event"] = ev
            finished.set()

    engine.add_listener(listener)          # before starting: it can end fast
    try:
        j = await start()
        if j.get("state") not in (None, "running") and "job" not in j:
            return j
        done["job"] = j.get("job")
        if j.get("job") is None:
            return j                       # not a job: the reply itself
        if "event" not in done:
            try:
                await asyncio.wait_for(finished.wait(), timeout_s)
            except asyncio.TimeoutError:
                raise PanoramaError(f"the darktable job took more than {int(timeout_s // 60)} minutes")
        ev = done["event"]
        if ev.get("state") in ("failed", "error") or ev.get("error"):
            raise PanoramaError(f"the darktable job failed: {ev.get('error') or ev}")
        return ev.get("result") if isinstance(ev.get("result"), dict) else ev
    finally:
        engine.remove_listener(listener)


# ── the stitch ───────────────────────────────────────────────────────────────

def _read_tiff(path: Path):
    """The pixels of a 16-bit RGB TIFF as an array, or None if nothing here reads it."""
    try:
        import tifffile
        return tifffile.imread(str(path))
    except Exception:
        pass
    try:
        import cv2
        return cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    except Exception:
        return None


def _clipped_pct(path: Path) -> float | None:
    """The share of pixels at 65535 in any channel, None if it can't be read."""
    try:
        a = _read_tiff(path)
        if a is None or a.dtype.name != "uint16" or a.ndim != 3:
            return None
        return 100.0 * float((a[..., :3] >= 65535).any(axis=-1).mean())
    except Exception:
        return None


async def _tiff_size(path: Path) -> tuple[int, int] | None:
    tool = shutil.which("tiffinfo")
    if not tool:
        return None
    try:
        out = await _run([tool, str(path)], path.parent, path.parent, 60)
    except PanoramaError:
        return None
    m = re.search(r"Image Width:\s*(\d+)\s+Image Length:\s*(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _free_name(folder: Path, stem: str) -> tuple[Path, str]:
    n = 1
    name = stem
    while (folder / f"{name}.tif").exists() or (folder / f"{name}.pto").exists():
        n += 1
        name = f"{stem}-{n}"
    return folder / f"{name}.tif", name


def _pto_points(pto: Path, ids: list[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in pto.read_text(errors="replace").splitlines():
        m = re.match(r"c n(\d+) N(\d+) ", line)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a != b and a < len(ids) and b < len(ids):
                key = f"{ids[a]}-{ids[b]}"
                out[key] = out.get(key, 0) + 1
    return out


def _errors(text: str) -> str | None:
    """checkpto's mean and maximum control point error, as "mean 8.9 px, max 26.9 px";
    else the optimiser's own lines as they were printed."""
    mean = re.search(r"Mean error\s*:\s*([\d.]+)", text)
    mx = re.search(r"Maximum\s*:\s*([\d.]+)", text)
    if mean and mx:
        return f"mean {float(mean.group(1)):.1f} px, max {float(mx.group(1)):.1f} px"
    lines = [l.strip() for l in text.splitlines() if re.search(r"\d+ units|error", l, re.I)]
    return "; ".join(lines[-2:]) or None


def _not_connected(check_out: str, ids: list[int]) -> list[int] | None:
    """The photo ids that are not in the first image group, None if connected."""
    groups = re.findall(r"^\s*Image group \d+:\s*(.*)$", check_out, re.M)
    if not groups:
        groups = re.findall(r"^\s*Group \d+:?\s*(.*)$", check_out, re.M)
    sets = [{int(x) for x in re.findall(r"\d+", g)} for g in groups]
    if len(sets) > 1:
        main = max(sets, key=len)
        return [ids[i] for i in sorted({i for s in sets for i in s} - main) if i < len(ids)]
    m = re.search(r"unconnected image[^\d]*([\d ,]+)", check_out, re.I)
    if m:
        return [ids[int(x)] for x in re.findall(r"\d+", m.group(1)) if int(x) < len(ids)]
    n = re.search(r"Number of image groups:\s*(\d+)", check_out)
    if n and int(n.group(1)) > 1:
        return []
    return None


async def stitch(engine, photo_ids: list[int], fixes: dict | None = None, projection: str = "auto",
                 match_edits_from: int | None = None, crop: bool = True, in_order: bool = False,
                 name: str | None = None, folder: str | None = None, apply_to_originals: bool = False,
                 log: Callable[[str], None] | None = None) -> dict:
    t0 = time.time()
    say = log or (lambda msg: None)
    fixes = fixes or {}
    add_missing = bool(fixes.get("add_missing"))
    remove_unwanted = bool(fixes.get("remove_unwanted"))
    move_back = bool(fixes.get("move_to_panorama", remove_unwanted))
    proj_name = "cylindrical" if projection in (None, "", "auto") else str(projection).lower()
    if proj_name not in PROJECTIONS:
        raise PanoramaError(f"projection '{projection}' is not one of auto, " + ", ".join(PROJECTIONS))

    say("looking for Hugin")
    hugin = find_hugin()
    say("checking the frames")
    report, frames = await _analyze(engine, photo_ids, match_edits_from)
    ids = [f["id"] for f in frames]
    ref = report["match_edits_from"]
    warnings: list[str] = list(report["warnings"])
    applied: list[str] = []

    out_dir = Path(folder).expanduser() if folder else Path(frames[0]["folder"] or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    first_stem = Path(frames[0]["file"] or f"photo{ids[0]}").stem
    work = out_dir / ".panorama-work" / first_stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    # the prerequisite modules the reference frame has on
    ref_frame = next(f for f in frames if f["id"] == ref)
    paste_mods: list = []
    for op in ("temperature", "exposure", "highlights", "lens", *DENOISE):
        if _enabled(ref_frame, op):
            paste_mods.append(op)
    if _enabled(ref_frame, "channelmixerrgb", 0):
        paste_mods.append({"operation": "channelmixerrgb", "instance": 0})
    others = [i for i in ids if i != ref]

    async def prerequisites(targets: list[int], ref_target: int) -> None:
        """Pastes the reference's prerequisites onto targets, then switches
        lens correction and highlight reconstruction on where still off,
        the reference (ref_target: its copy, or itself) included."""
        if targets and paste_mods:
            await engine.call("history_paste", **{"from": ref}, imgids=targets, mode="append", modules=paste_mods)
        for t in [ref_target, *targets]:
            for op in ("lens", "highlights"):
                mods = [m for m in await _mods(t) if m["operation"] == op]
                if mods and not any(m.get("enabled") for m in mods):
                    await engine.edit(t, "module_enable", operation=op, instance=mods[0]["instance"], enabled=True)
                    await engine.edit(t, "save")

    async def _mods(i: int) -> list[dict]:
        return (await engine.edit(i, "module_list"))["modules"]

    copies: dict[int, int] = {}                 # frame -> its virtual copy
    exported: list[Path] = []
    result_tif = result_pto = None
    keep = bool(os.environ.get("DTAPI_PANORAMA_KEEP"))
    try:
        if add_missing and apply_to_originals:
            say("fixing the frames themselves")
            await prerequisites(others, ref)
            applied.append("prerequisites added to the frames")
        for i in ids:
            say(f"making a virtual copy of frame {i}")
            try:
                r = await engine.call("image_duplicate", imgid=i)
            except EngineError as exc:
                if exc.is_("unsaved_changes", "unsaved"):
                    raise PanoramaError(f"save or discard frame {i} first: {exc}")
                raise
            copies[i] = r["image"]["id"]
        if add_missing and not apply_to_originals:
            say("adding the missing prerequisites to the copies")
            await prerequisites([copies[i] for i in others], copies[ref])
            applied.append(f"prerequisites from frame {ref} added")
        elif add_missing:
            applied.append(f"prerequisites from frame {ref} added to the frames")
        if remove_unwanted:
            say("removing per-frame looks on the copies")
            for i in ids:
                c = copies[i]
                n = 0
                for m in await _mods(c):
                    op, inst = m["operation"], m["instance"]
                    if not m.get("enabled") or op == "flip" or op in KEEP_BY_DEFAULT:
                        continue
                    if op in UNWANTED or ((op, inst) in frames[ids.index(i)]["masked"]):
                        await engine.edit(c, "module_enable", operation=op, instance=inst, enabled=False)
                        n += 1
                if n:
                    await engine.edit(c, "save")
            applied.append("per-frame looks and masked modules switched off on the copies")
        if add_missing or remove_unwanted:
            for i in ids:
                await engine.edit(copies[i], "save")

        # export
        stems: dict[int, str] = {}
        for i, f in zip(ids, frames):
            stem = Path(f["file"] or f"photo{i}").stem
            stems[i] = stem if stem not in stems.values() else f"{stem}-{i}"
        for n, i in enumerate(ids, 1):
            say(f"exporting frame {i} ({n} of {len(ids)}) as a 16-bit TIFF")
            params = dict(imgid=copies[i], format="tiff", bpp=16, max_width=0, max_height=0, upscale=False,
                          style="", high_quality=True, path=str(work / stems[i]), on_conflict="overwrite")
            if await engine.supports("export.background"):
                r = await _job(engine, lambda: engine.call("export", background=True, **params), 900)
            else:
                r = await engine.call("export", **params)
            file = Path(r.get("file") or "")
            if not file.is_file():
                raise PanoramaError(f"darktable didn't write the export of frame {i}: {r}")
            exported.append(file)
            pct = await asyncio.to_thread(_clipped_pct, file)
            if pct is not None and pct > CLIP_WARN_PCT:
                warnings.append(f"frame {i} clips {pct:.1f}% at white in the 16-bit export (the tone mapper was "
                                "removed); lower exposure on the frames or keep it")
    finally:
        # the copies go, success or not
        gone = list(copies.values())
        if gone:
            try:
                await engine.call("image_remove", imgids=gone)
            except Exception as exc:
                warnings.append(f"the virtual copies {gone} couldn't be removed: {exc}")

    try:
        # Hugin
        pto = work / "p.pto"
        tifs = [str(p) for p in exported]
        gen = [str(hugin / "pto_gen"), "-o", str(pto)]
        f0 = frames[0]
        size = await _tiff_size(exported[0])
        if f0["focal_length"]:
            w, h = size or (f0["width"] or 4, f0["height"] or 3)
            sensor = 36.0 if w >= h else 24.0
            hfov = math.degrees(2 * math.atan(sensor / (2 * f0["focal_length"] * (f0["crop_factor"] or 1.0))))
            gen += ["-f", f"{hfov:.4f}"]
        else:
            warnings.append("focal length unknown: Hugin guesses the field of view")
        say("Hugin: project file")
        await _run(gen + tifs, work, hugin)
        say("Hugin: finding control points")
        await _run([str(hugin / "cpfind"), "--celeste", "--linearmatch" if in_order else "--multirow",
                    "-o", str(pto), str(pto)], work, hugin)
        say("Hugin: cleaning control points")
        await _run([str(hugin / "cpclean"), "-o", str(pto), str(pto)], work, hugin)
        await _run([str(hugin / "linefind"), "-o", str(pto), str(pto)], work, hugin)
        say("Hugin: optimising")
        opt = await _run([str(hugin / "autooptimiser"), "-a", "-m", "-l", "-s", "-o", str(pto), str(pto)], work, hugin)
        chk = await _run([str(hugin / "checkpto"), str(pto)], work, hugin)
        lost = _not_connected(chk, ids)
        if lost is not None:
            raise PanoramaError(
                "the frames don't connect: "
                + (f"photos {', '.join(map(str, lost))} share no control points with the others"
                   if lost else "Hugin found more than one group of images")
                + ". They need to overlap by about a third; "
                  + ("" if in_order else "if they do, and the scene is mostly sky or water, try in_order=True. ")
                + "Nothing was imported.")
        say("Hugin: canvas and projection")
        mod = [str(hugin / "pano_modify"), "-o", str(pto), "--canvas=AUTO"]
        if crop:
            mod.append("--crop=AUTO")
        mod += ["--fov=AUTO", "--straighten", "--center", "--ldr-file=TIF", "--ldr-compression=DEFLATE",
                "--blender=INTERNAL", f"--projection={PROJECTIONS[proj_name]}", str(pto)]
        await _run(mod, work, hugin)
        say("Hugin: stitching")
        await _run([str(hugin / "hugin_executor"), "--stitching", f"--prefix={work / 'pano'}", str(pto)], work, hugin)
        made = work / "pano.tif"
        if not made.is_file():
            cands = sorted(p for p in work.glob("pano*.tif") if not re.search(r"\d{4}\.tif$", p.name))
            if not cands:
                raise PanoramaError("Hugin finished but wrote no panorama file")
            made = cands[0]
        points = _pto_points(pto, ids)
        errs = _errors(chk) or _errors(opt)

        # the result beside the frames
        result_tif, stem = _free_name(out_dir, name or f"{first_stem}-pano")
        shutil.move(str(made), str(result_tif))
        tiffinfo, tiffcp = shutil.which("tiffinfo"), shutil.which("tiffcp")
        if tiffinfo and tiffcp:
            info = await _run([tiffinfo, str(result_tif)], out_dir, hugin, 120)
            if not re.search(r"Compression Scheme:\s*(Deflate|AdobeDeflate|LZW)", info, re.I):
                tmp = result_tif.with_suffix(".tmp.tif")
                await _run([tiffcp, "-c", "zip:2", str(result_tif), str(tmp)], out_dir, hugin, 600)
                tmp.replace(result_tif)
        else:
            warnings.append("tiffinfo/tiffcp not found: the compression of the result wasn't checked")
        exiv2 = shutil.which("exiv2")
        if exiv2:
            try:
                src = subprocess.run([exiv2, "-ea-", str(exported[0])], capture_output=True, timeout=120)
                if src.returncode == 0 and src.stdout:
                    dst = subprocess.run([exiv2, "-ia-", str(result_tif)], input=src.stdout, capture_output=True,
                                         timeout=120)
                    if dst.returncode != 0:
                        warnings.append("EXIF not copied to the panorama: " + dst.stderr.decode(errors="replace")[-200:])
                else:
                    warnings.append("EXIF not copied to the panorama: " + src.stderr.decode(errors="replace")[-200:])
            except Exception as exc:
                warnings.append(f"EXIF not copied to the panorama: {exc}")
        else:
            warnings.append("exiv2 not found: the panorama has no EXIF")
        result_pto = result_tif.with_suffix(".pto")
        shutil.move(str(pto), str(result_pto))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
            try:
                work.parent.rmdir()
            except OSError:
                pass

    # into the library
    say("importing the panorama")
    r = await _job(engine, lambda: engine.call("import", paths=[str(result_tif)], copy=False, skip_existing=False), 600)
    imp = (r.get("imported") or [])
    if not imp:
        raise PanoramaError(f"darktable didn't import {result_tif}: {r}")
    new = imp[0]["imgid"]
    await engine.call("set_tags", imgids=[new], attach=[f"panorama|from {_ids_text(ids)}"])
    grouped = False
    if await engine.supports("group"):
        await engine.call("group", imgids=ids + [new], leader=new)
        grouped = True
    else:
        warnings.append("not grouped: engine too old")
    if move_back:
        look = [op for op in LOOK if _enabled(ref_frame, op)
                and not any(m["operation"] == op and m.get("enabled") and _is_masked(ref_frame, m)
                            for m in ref_frame["modules"])]
        if look:
            say("carrying the look over to the panorama")
            try:
                await engine.call("history_paste", **{"from": ref}, imgids=[new], mode="append", modules=look)
                applied.append(f"look ({', '.join(look)}) from frame {ref} moved to the panorama")
            except EngineError as exc:
                warnings.append(f"the look couldn't be carried over to the panorama: {exc}")

    info = (await engine.call("image_info", imgid=new)).get("image") or {}
    return {"image_id": new, "file": str(result_tif), "pto": str(result_pto),
            "width": info.get("width"), "height": info.get("height"),
            "file_mb": round(result_tif.stat().st_size / 1e6, 1), "frames": ids,
            "control_points": points, "optimiser_error": errs, "projection": proj_name,
            "grouped": grouped, "fixes": applied, "warnings": warnings, "seconds": round(time.time() - t0)}
