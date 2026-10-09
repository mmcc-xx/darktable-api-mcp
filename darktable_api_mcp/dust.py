"""
Sensor dust detection.

Dust on the sensor casts a soft, round, slightly dark shadow at the same
sensor position in every frame, most visibly on smooth backgrounds (sky)
and at small apertures. Detection therefore works in sensor coordinates,
straight from the RAW (rawpy, half size, no flip, no lens correction):

1. detect(): in one frame, dark round blobs well above the local noise
   (relative darkening vs. a wide blur, measured against a noise estimate
   that a spot can't inflate). Texture (sand, foliage) produces many of these.
2. build_map(): over a film roll, count in how many frames each sensor
   position has a candidate. Dust recurs in tens of frames; texture hits are
   random (≈10 per position in 199 frames of the 2026-03 dunes roll, versus
   88 and 41 for its two dust spots). Positions far above that background
   are dust. Cached per roll and camera.
3. spots_in_frame(): for one photo, which mapped dust spots are visible
   there (measured signal-to-noise at that position).

Coordinates: "half" = pixels of the half-size sensor rendering;
`raw_norm` = normalized to darktable's full raw buffer (what drawn masks and
retouch shapes use: darktable-api's "raw" space). Where a spot is on the
photo as shown, and which retouch circles exist, come from darktable-api
(coords, retouch_list).
Tuned on a Panasonic GX85; dust ~20–30 px across at full size.

From darktableluamcp's dust.py (same author).
"""

import hashlib
import json
import os
from pathlib import Path

import cv2
import numpy as np

CACHE = Path(os.environ.get("DTAPI_DUST_CACHE") or Path.home() / ".cache/darktable-api-mcp/dust")
MAP_VERSION = 1


# ── rendering ─────────────────────────────────────────────────────────────────

def render_sensor(path: str | Path) -> tuple[np.ndarray, dict]:
    """Luminance 0–1 of the RAW at half size in sensor orientation, plus geometry."""
    import rawpy
    with rawpy.imread(str(path)) as raw:
        # the camera's orientation flag: read it before postprocess(user_flip=0),
        # after which raw.sizes.flip reports 0 (found 2026-10-04 on portrait frames)
        flip = raw.sizes.flip
        rgb = raw.postprocess(half_size=True, user_flip=0, use_camera_wb=True, no_auto_bright=True,
                              output_bps=16, gamma=(2.222, 4.5))
        s = raw.sizes
        geom = {"raw_width": s.raw_width, "raw_height": s.raw_height, "left_margin": s.left_margin,
                "top_margin": s.top_margin, "width": s.width, "height": s.height, "flip": flip}
    lum = (rgb.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)) / 65535.0
    geom["half_width"], geom["half_height"] = lum.shape[1], lum.shape[0]
    return lum, geom


# ── single-frame detection ────────────────────────────────────────────────────

def _maps(lum: np.ndarray, s_small: float = 2.5, s_bg: float = 16, clip: float = 0.015):
    bg = cv2.GaussianBlur(lum, (0, 0), s_bg)
    rel = cv2.GaussianBlur((lum - bg) / (bg + 1e-3), (0, 0), s_small)       # relative darkening
    # local noise, robust to the spot itself: RMS of rel clipped at ±clip
    noise = np.sqrt(cv2.blur(np.minimum(rel * rel, clip * clip), (61, 61)) + 1e-10)
    return bg, rel, noise


def detect(lum: np.ndarray, k: float = 6.0, min_depth: float = 0.02, max_noise: float = 0.014,
           r_range: tuple[float, float] = (2, 30)) -> list[dict]:
    """Dark, round, soft blobs at least k× the local noise. Positions in half-size pixels."""
    bg, rel, noise = _maps(lum)
    cand = (rel < -k * noise) & (rel < -min_depth) & (noise < max_noise) & (bg > 0.04) & (bg < 0.97)
    n, labels, stats, cent = cv2.connectedComponentsWithStats(cand.astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        r = max(w, h) / 2
        if not (r_range[0] <= r <= r_range[1]) or max(w, h) > 2.0 * min(w, h) or area < 0.45 * w * h:
            continue
        sl = labels[y:y + h, x:x + w] == i
        depth = float(-rel[y:y + h, x:x + w][sl].min())
        nz = float(noise[y:y + h, x:x + w][sl].mean())
        out.append({"x": float(cent[i][0]), "y": float(cent[i][1]), "r": float(r),
                    "depth": round(depth, 4), "snr": round(depth / nz, 1)})
    out.sort(key=lambda d: -d["snr"])
    return out


def measure(lum: np.ndarray, x: float, y: float, r: float, search: float = 10, maps=None) -> dict:
    """Is there a dark blob near (x, y) in this frame? Depth and SNR at the darkest point."""
    bg, rel, noise = maps or _maps(lum)
    h, w = lum.shape
    x0, x1 = max(0, int(x - search)), min(w, int(x + search) + 1)
    y0, y1 = max(0, int(y - search)), min(h, int(y + search) + 1)
    win = rel[y0:y1, x0:x1]
    yy, xx = np.unravel_index(np.argmin(win), win.shape)
    cy, cx = y0 + yy, x0 + xx
    depth = float(-rel[cy, cx])
    return {"x": float(cx), "y": float(cy), "depth": round(depth, 4),
            "snr": round(depth / float(noise[cy, cx]), 1), "background_noise": round(float(noise[cy, cx]), 4),
            "brightness": round(float(bg[cy, cx]), 3)}


# ── roll-wide dust map ────────────────────────────────────────────────────────

def _file_key(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"


def build_map(frames: list[tuple[int, Path]], camera: str, roll_key: str,
              min_frames: int = 15, background_factor: float = 3.0, force: bool = False) -> dict:
    """Recurring dark blobs over frames [(image_id, raw path)] of one camera.

    A position counts as dust when its candidates come from at least
    max(min_frames, background_factor × the typical count of the strongest
    non-dust positions) different frames. Cached in cache/dust/.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    sig = hashlib.sha1("|".join(sorted(_file_key(p) for _, p in frames)).encode()).hexdigest()[:12]
    cache = CACHE / f"{roll_key}_{camera.replace(' ', '_')}.json"
    if cache.exists() and not force:
        m = json.loads(cache.read_text())
        if m.get("version") == MAP_VERSION and m.get("signature") == sig:
            return m
    cands, geom, used, skipped = [], None, 0, []
    for imgid, path in frames:
        try:
            lum, g = render_sensor(path)
        except Exception as exc:
            skipped.append({"image_id": imgid, "file": Path(path).name, "error": str(exc)[:120]})
            continue
        if geom is None:
            geom = g
        elif (g["half_width"], g["half_height"]) != (geom["half_width"], geom["half_height"]):
            skipped.append({"image_id": imgid, "file": Path(path).name, "error": "different sensor size"})
            continue
        used += 1
        cands += [dict(c, id=imgid) for c in detect(lum)]
    if geom is None:
        raise ValueError("no readable RAW frames")
    H, W = geom["half_height"], geom["half_width"]
    votes = np.zeros((H, W), np.float32)
    by_frame: dict[int, list] = {}
    for c in cands:
        by_frame.setdefault(c["id"], []).append(c)
    for cs in by_frame.values():
        m = np.zeros((H, W), np.uint8)
        for c in cs:
            cv2.circle(m, (int(round(c["x"])), int(round(c["y"]))), 5, 1, -1)
        votes += m
    peaks, v = [], votes.copy()
    while len(peaks) < 200:
        y, x = np.unravel_index(np.argmax(v), v.shape)
        if v[y, x] < 3:
            break
        near = [c for c in cands if abs(c["x"] - x) <= 6 and abs(c["y"] - y) <= 6]
        ids = sorted({c["id"] for c in near})
        peaks.append({"x": round(float(np.mean([c["x"] for c in near])), 1),
                      "y": round(float(np.mean([c["y"] for c in near])), 1),
                      "frames": len(ids), "image_ids": ids,
                      "r": round(float(np.median([c["r"] for c in near])), 1),
                      "depth": round(float(np.median([c["depth"] for c in near])), 3)})
        cv2.circle(v, (int(x), int(y)), 14, 0, -1)
    counts = sorted((p["frames"] for p in peaks), reverse=True)
    background = float(np.median(counts[2:40])) if len(counts) > 5 else 3.0
    threshold = max(min_frames, background_factor * background)
    dust = [p for p in peaks if p["frames"] >= threshold]
    for i, p in enumerate(dust, 1):
        p["id"] = i
        p.update(raw_norm(p["x"], p["y"], geom))
        p["radius_raw_norm"] = round(p["r"] * 2 / geom["raw_width"], 5)
    m = {"version": MAP_VERSION, "signature": sig, "camera": camera, "roll": roll_key,
         "frames": used, "skipped": skipped, "geometry": geom, "background_frames": background,
         "threshold_frames": threshold, "dust": dust}
    cache.write_text(json.dumps(m))
    return m


# ── coordinates ───────────────────────────────────────────────────────────────

def raw_norm(x_half: float, y_half: float, geom: dict) -> dict:
    """Half-size sensor pixel → darktable's normalized full-raw-buffer coordinates."""
    return {"raw_x": round((geom["left_margin"] + 2 * x_half) / geom["raw_width"], 5),
            "raw_y": round((geom["top_margin"] + 2 * y_half) / geom["raw_height"], 5)}


# ── one photo ─────────────────────────────────────────────────────────────────

def ring_texture(rel: np.ndarray, x: float, y: float, outer: int = 30, inner: int = 10) -> float:
    """Std of the relative-darkening map in a ring around (x, y): the background's texture,
    measured without the spot itself (uncapped, unlike the noise map)."""
    h, w = rel.shape
    x, y = int(round(x)), int(round(y))
    ys, xs = np.mgrid[max(0, y - outer):min(h, y + outer + 1), max(0, x - outer):min(w, x + outer + 1)]
    d2 = (xs - x) ** 2 + (ys - y) ** 2
    sel = (d2 <= outer * outer) & (d2 > inner * inner)
    return float(rel[ys[sel], xs[sel]].std()) if sel.any() else 1.0


def spots_in_frame(lum: np.ndarray, dust_map: dict) -> list[dict]:
    """The map's dust spots, measured in this frame.

    Measured at the mapped position (±4 half-px; dust doesn't move) against the
    texture in a ring around it. Calibrated on the 2026-03 dunes roll against
    contact sheets: clean sky gives ratios ~10, a spot seen through diagonal
    sand texture ~6, spots lost in texture or next to an edge 1.4–4.7.
      obvious  — ratio >= 8 on a smooth background (ring texture < 1.2 %)
      visible  — ratio >= 5 and ring texture < 2 %
      hidden   — otherwise (texture or an edge hides it; no repair needed)
    """
    out = []
    _bg, rel, _noise = _maps(lum)
    h, w = rel.shape
    for d in dust_map["dust"]:
        x, y = int(round(d["x"])), int(round(d["y"]))
        win = rel[max(0, y - 4):min(h, y + 5), max(0, x - 4):min(w, x + 5)]
        yy, xx = np.unravel_index(np.argmin(win), win.shape)
        depth = float(-win.min())
        tex = ring_texture(rel, x, y)
        ratio = depth / max(tex, 1e-4)
        status = ("obvious" if ratio >= 8 and tex < 0.012 else
                  "visible" if ratio >= 5 and tex < 0.02 else "hidden")
        out.append({"id": d["id"], "x": float(max(0, x - 4) + xx), "y": float(max(0, y - 4) + yy),
                    "r": d["r"], "depth": round(depth, 4), "ratio": round(ratio, 1),
                    "texture": round(tex, 4), "status": status, "visible": status != "hidden",
                    "roll_frames": d["frames"],
                    "background": "smooth" if tex < 0.012 else "moderate" if tex < 0.02 else "textured"})
    return out


def crop_sheet(lum: np.ndarray, spots: list[dict], tile: int = 180, half_window: int = 40) -> bytes:
    """PNG of numbered, contrast-stretched crops around each spot (sensor orientation)."""
    tiles = []
    for s in spots:
        x, y = int(round(s["x"])), int(round(s["y"]))
        h, w = lum.shape
        x0, y0 = min(max(0, x - half_window), w - 2 * half_window), min(max(0, y - half_window), h - 2 * half_window)
        c = lum[y0:y0 + 2 * half_window, x0:x0 + 2 * half_window]
        c = np.clip((c - c.mean()) / (6 * c.std() + 1e-6) + 0.5, 0, 1)
        t = cv2.cvtColor(cv2.resize((c * 255).astype(np.uint8), (tile, tile), interpolation=cv2.INTER_NEAREST),
                         cv2.COLOR_GRAY2BGR)
        k = tile / (2 * half_window)
        col = (60, 200, 60) if s.get("visible", True) else (60, 60, 220)
        cv2.circle(t, (int((x - x0) * k), int((y - y0) * k)), int(max(4, s["r"] * 1.8 * k)), col, 1)
        cv2.putText(t, f"#{s['id']}", (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        tiles.append(t)
    if not tiles:
        tiles = [np.zeros((tile, tile, 3), np.uint8)]
    ok, png = cv2.imencode(".png", np.hstack(tiles))
    return png.tobytes()


# ── repair planning ───────────────────────────────────────────────────────────

def heal_radius(r_dust: float) -> float:
    """Heal circle radius (half-size px) for a dust spot of radius r_dust: twice
    the detected radius covers the soft edge of the shadow."""
    return max(6.0, 2.0 * r_dust)


def choose_source(lum: np.ndarray, x: float, y: float, R: float, avoid: list[tuple[float, float, float]]) -> dict | None:
    """Where to copy from: a nearby patch of radius R that is smooth and matches the
    brightness around the spot. avoid: other dust (x, y, r) to stay clear of.
    Returns {"x", "y", "score"} in half-size pixels, or None."""
    h, w = lum.shape
    yy, xx = np.mgrid[0:h, 0:w]
    def disk(cx, cy, rr):
        x0, x1, y0, y1 = int(max(0, cx - rr)), int(min(w, cx + rr + 1)), int(max(0, cy - rr)), int(min(h, cy + rr + 1))
        sub = lum[y0:y1, x0:x1]
        m = (xx[y0:y1, x0:x1] - cx) ** 2 + (yy[y0:y1, x0:x1] - cy) ** 2 <= rr * rr
        return sub[m]
    ring = disk(x, y, 2.2 * R)
    target = float(np.median(ring))
    best = None
    for dist in (2.5, 3.5, 4.5):
        for k in range(16):
            a = 2 * np.pi * k / 16
            sx, sy = x + dist * R * np.cos(a), y + dist * R * np.sin(a)
            if not (R + 2 <= sx <= w - R - 3 and R + 2 <= sy <= h - R - 3):
                continue
            if any(np.hypot(sx - ax, sy - ay) < R + 2.5 * ar for ax, ay, ar in avoid):
                continue
            p = disk(sx, sy, R)
            if p.size < 10:
                continue
            tex = float(np.std(p) / (np.mean(p) + 1e-3))
            diff = abs(float(np.mean(p)) - target) / (target + 1e-3)
            score = tex + 2.0 * diff + 0.01 * dist
            if best is None or score < best["score"]:
                best = {"x": float(sx), "y": float(sy), "score": round(score, 4),
                        "brightness_diff_pct": round(100 * diff, 1), "texture_pct": round(100 * tex, 1)}
    return best


def heal_spec(spot: dict, src: dict, R: float, geom: dict) -> dict:
    """darktable-api retouch_heal entry: center and source in drawn-mask (raw)
    coordinates, radius relative to the shorter side of the raw buffer."""
    c, s = raw_norm(spot["x"], spot["y"], geom), raw_norm(src["x"], src["y"], geom)
    return {"x": c["raw_x"], "y": c["raw_y"], "r": round(2 * R / min(geom["raw_width"], geom["raw_height"]), 5),
            "sx": s["raw_x"], "sy": s["raw_y"]}


def darkening_at(img_lum: np.ndarray, x: float, y: float, r: float) -> float:
    """Relative darkening of a soft blob of radius r (pixels) at (x, y) in any image,
    e.g. a full-size rendering: local version of _maps, used before/after healing."""
    h, w = img_lum.shape
    half = int(max(24, 8 * r))
    x0, x1, y0, y1 = int(max(0, x - half)), int(min(w, x + half)), int(max(0, y - half)), int(min(h, y + half))
    win = img_lum[y0:y1, x0:x1].astype(np.float32)
    bg = cv2.GaussianBlur(win, (0, 0), max(4.0, 2.2 * r))
    rel = cv2.GaussianBlur((win - bg) / (bg + 1e-3), (0, 0), max(1.0, r / 3))
    cx, cy = x - x0, y - y0
    m = (np.mgrid[0:win.shape[0], 0:win.shape[1]][1] - cx) ** 2 + (np.mgrid[0:win.shape[0], 0:win.shape[1]][0] - cy) ** 2 <= (r * 1.2) ** 2
    return round(float(-rel[m].min()), 4) if m.any() else 0.0


def half_from_raw(x: float, y: float, r: float, geom: dict) -> tuple[float, float, float]:
    """A circle in raw space (darktable-api retouch_list) in half-size sensor pixels."""
    short = min(geom["raw_width"], geom["raw_height"])
    return ((x * geom["raw_width"] - geom["left_margin"]) / 2,
            (y * geom["raw_height"] - geom["top_margin"]) / 2, r * short / 2)
