"""How much detail a render holds (#158, print_size): the usable-detail
factor of a region rendered at 100%, by the downscale-and-re-upscale test,
and the width of its hard edges. numpy and OpenCV (the `dust` extra)."""

from __future__ import annotations

import math

import numpy as np
import cv2

SCALES = np.round(np.arange(1.0, 0.199, -0.025), 3)
LOSS = 0.10              # detail lost at the factor, as a share of what a quarter-size round trip loses
CRISP = 0.4              # a tile shrunk to this is sharp at its own pixels: the factor-1 reference
CAL = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)
MARGIN = 6


def gray(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0


def round_trip(g: np.ndarray, s: float) -> np.ndarray:
    """g shrunk to s of its size and scaled back: what printing it s times as large keeps."""
    h, w = g.shape
    d = cv2.resize(g, (max(8, round(w * s)), max(8, round(h * s))), interpolation=cv2.INTER_AREA)
    return cv2.resize(d, (w, h), interpolation=cv2.INTER_CUBIC)


def _knee(g: np.ndarray) -> float:
    """The smallest scale whose round trip loses at most LOSS of the detail
    (relative to a 0.2 round trip), interpolated between SCALES."""
    m = MARGIN
    loss = np.array([float(((round_trip(g, s) - g)[m:-m, m:-m] ** 2).mean()) for s in SCALES])
    if loss[-1] <= 0:
        return float(SCALES[-1])                 # flat: nothing to lose
    loss /= loss[-1]
    for i in range(1, len(SCALES)):
        if loss[i] > LOSS:
            a, b = loss[i - 1], loss[i]
            return float(SCALES[i - 1] + (SCALES[i] - SCALES[i - 1]) * (LOSS - a) / max(b - a, 1e-9))
    return float(SCALES[-1])


def detail_factor(g: np.ndarray) -> float:
    """The usable-detail factor of a 100% render (gray, 0-1): 1 = sharp at
    every pixel, 0.5 = as sharp as the same photo at half its size. The
    tile's knee is read against knees of a sharp copy of itself (shrunk to
    CRISP) blurred by known round trips, so the content's own texture
    cancels out."""
    h, w = g.shape
    crisp = cv2.resize(g, (max(32, round(w * CRISP)), max(32, round(h * CRISP))), interpolation=cv2.INTER_AREA)
    knees = [_knee(crisp if f >= 1 else round_trip(crisp, f)) for f in CAL]
    k = _knee(g)
    # knees fall as f falls; interpolate f at k (clamped to the calibrated range)
    if k >= knees[0]:
        return 1.0
    for i in range(1, len(CAL)):
        if k >= knees[i]:
            a, b = knees[i - 1], knees[i]
            return float(CAL[i - 1] + (CAL[i] - CAL[i - 1]) * (k - a) / (b - a)) if a != b else float(CAL[i])
    return float(CAL[-1])


def edge_width(g: np.ndarray, n: int = 40, half: int = 8) -> float | None:
    """Median 10-90% rise, in pixels, across the strongest isolated edges
    (a sharp edge rises in 1-2 px). None without a usable edge."""
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    h, w = g.shape
    m = half + 2
    mag[:m], mag[-m:], mag[:, :m], mag[:, -m:] = 0, 0, 0, 0
    # local maxima only, spread out: one per 9 x 9 neighbourhood
    peaks = (mag == cv2.dilate(mag, np.ones((9, 9), np.uint8))) & (mag > 0.05)
    ys, xs = np.nonzero(peaks)
    if not len(ys):
        return None
    order = np.argsort(-mag[ys, xs])[: n * 3]
    t = np.arange(-half, half + 0.01, 0.25, dtype=np.float32)
    widths = []
    for y, x in zip(ys[order], xs[order]):
        dx, dy = gx[y, x], gy[y, x]
        norm = math.hypot(dx, dy)
        dx, dy = dx / norm, dy / norm
        mx = (x + t * dx).astype(np.float32)[None, :]
        my = (y + t * dy).astype(np.float32)[None, :]
        p = cv2.remap(g, mx, my, cv2.INTER_LINEAR)[0]
        lo, hi = p[: len(t) // 4].mean(), p[-len(t) // 4:].mean()
        span = hi - lo
        if span < 0.08:
            continue                              # too faint
        q = (p - lo) / span
        # an isolated edge: flat ends, one rise (no second edge within half px)
        if q[: len(t) // 4].std() > 0.08 or q[-len(t) // 4:].std() > 0.08:
            continue
        i10 = np.argmax(q >= 0.1)
        i90 = np.argmax(q >= 0.9)
        if i90 <= i10:
            continue
        widths.append((t[i90] - t[i10]))
        if len(widths) >= n:
            break
    if len(widths) < 3:
        return None
    return float(np.median(widths))


def pick_regions(fit_bgr: np.ndarray, count: int = 3, grid: int = 8) -> list[dict]:
    """The `count` most detailed cells of a grid over a fitted render (Laplacian
    energy: sky and other smooth areas score low), at least two cells apart.
    [{x, y}] centers as fractions of the photo."""
    g = gray(fit_bgr)
    lap = np.abs(cv2.Laplacian(g, cv2.CV_32F))
    h, w = g.shape
    e = np.array([[lap[i * h // grid:(i + 1) * h // grid, j * w // grid:(j + 1) * w // grid].mean()
                   for j in range(grid)] for i in range(grid)])
    chosen: list[tuple[int, int]] = []
    for idx in np.argsort(-e.ravel()):
        i, j = divmod(int(idx), grid)
        if all(max(abs(i - a), abs(j - b)) >= 2 for a, b in chosen):
            chosen.append((i, j))
        if len(chosen) == count:
            break
    return [{"x": (j + 0.5) / grid, "y": (i + 0.5) / grid} for i, j in chosen]


def pixel_pitch_um(crop_factor: float | None, raw_w: int | None, raw_h: int | None) -> float | None:
    """The sensor's pixel pitch from the crop factor and the raw's pixel size."""
    if not crop_factor or crop_factor <= 0 or not raw_w or not raw_h:
        return None
    diag_mm = math.hypot(36.0, 24.0) / crop_factor
    width_mm = diag_mm * raw_w / math.hypot(raw_w, raw_h)
    return 1000.0 * width_mm / raw_w


def airy_px(f_number: float | None, pitch_um: float | None) -> float | None:
    """Diffraction blur: Airy disk diameter (2.44 x 0.55 um x N) in pixels."""
    if not f_number or not pitch_um:
        return None
    return 2.44 * 0.55 * f_number / pitch_um
