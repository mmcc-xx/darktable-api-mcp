"""
Sensor clipping and blocked shadows, from the RAW (rawpy), for culling.

A photosite at the raw's white level recorded nothing above it; a shadow far
enough below white is noise whatever the denoiser does. On the Bayer mosaic
the unit is a 2x2 block (one red, two green, one blue photosite, about one
pixel of the photo):

- all_clipped (red): all four photosites at white; nothing to rebuild.
- some_clipped (orange): one to three; highlight reconstruction can usually
  fill it in from the others.
- blocked_shadows (blue): the block's brightest photosite is below floor():
  more than FLOOR_EV below white at base ISO, less at higher ISO (the noise
  floor rises with the gain); a +3 EV push won't bring detail back.

classify() works on arrays, measure() on a file, optionally with a
small RGBA mask (PNG) of the blocks, turned like the photo as shown.
Non-Bayer sensors (X-Trans) get the shares from single photosites and no mask.
FLOOR_EV is tuned by eye on library photos.

check_sensor_clipping (server.py) builds on the same function.
"""

import math

import numpy as np

FLOOR_EV = 12.0       # F: shadows more than F EV below white, at base ISO, are mush (2026-10-26: 11 marked bushes a +3 EV push brings back)
BASE_ISO = 200.0
CLIPPED = 0.99        # of a photosite's white level

NONE, BLUE, ORANGE, RED = 0, 1, 2, 3            # a block's class; a pooled pixel takes the max
COLORS = {BLUE: (40, 110, 255, 170), ORANGE: (255, 150, 0, 170), RED: (255, 0, 0, 200)}   # RGBA


def floor(iso: float | None, base_iso: float = BASE_ISO, f: float = FLOOR_EV) -> float:
    """2^-(F - log2(ISO / base)): the normalized level under which a block is noise."""
    gain = math.log2(iso / base_iso) if iso and iso > 0 else 0.0
    return 2.0 ** -(f - gain)


def _levels(colors, white, black) -> tuple[np.ndarray, np.ndarray]:
    return (np.asarray(white, np.float32)[colors], np.asarray(black, np.float32)[colors])


def _shares(red, orange, blue) -> dict:
    n = red.size
    return {"all_clipped_pct": round(100 * float(np.count_nonzero(red)) / n, 4),
            "some_clipped_pct": round(100 * float(np.count_nonzero(orange)) / n, 4),
            "blocked_shadows_pct": round(100 * float(np.count_nonzero(blue)) / n, 4)}


def classify(data, colors, white, black, iso=None, f=FLOOR_EV, bayer=True):
    """(shares, maps): the percentages of blocks (Bayer: 2x2, odd edges
    cropped) or, with bayer=False, of photosites; maps is the class of
    each block (NONE/BLUE/ORANGE/RED), None when not Bayer. data and colors
    are the visible raw and each photosite's color index into white/black."""
    if bayer:
        h, w = data.shape[0] // 2 * 2, data.shape[1] // 2 * 2
        data, colors = data[:h, :w], colors[:h, :w]
    wl, bl = _levels(colors, white, black)
    v = data.astype(np.float32)
    clip = v >= CLIPPED * wl
    norm = (v - bl) / np.maximum(1.0, wl - bl)
    fl = floor(iso, f=f)
    if not bayer:
        red, blue = clip, norm < fl
        return _shares(red, np.zeros_like(red), blue), None

    def blocks(a):
        return a.reshape(h // 2, 2, w // 2, 2)
    n = blocks(clip).sum(axis=(1, 3))
    red, orange = n == 4, (n > 0) & (n < 4)
    blue = blocks(norm).max(axis=(1, 3)) < fl
    blue &= ~(red | orange)
    cls = np.where(red, RED, np.where(orange, ORANGE, np.where(blue, BLUE, NONE))).astype(np.uint8)
    return _shares(red, orange, blue), cls


def _png(cls: np.ndarray, size: int, flip: int) -> bytes:
    """The block classes as an RGBA PNG whose long side is at most `size`
    (a pooled pixel takes the worst class in it), turned like the photo."""
    import cv2
    k = max(1, math.ceil(max(cls.shape) / size))
    h, w = -(-cls.shape[0] // k) * k, -(-cls.shape[1] // k) * k
    cls = np.pad(cls, ((0, h - cls.shape[0]), (0, w - cls.shape[1])))
    cls = cls.reshape(h // k, k, w // k, k).max(axis=(1, 3))
    if flip == 3:
        cls = np.rot90(cls, 2)
    elif flip == 5:
        cls = np.rot90(cls, 1)         # 90 degrees counter-clockwise
    elif flip == 6:
        cls = np.rot90(cls, -1)        # clockwise
    rgba = np.zeros(cls.shape + (4,), np.uint8)
    for c, col in COLORS.items():
        rgba[cls == c] = col
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgba[..., [2, 1, 0, 3]]))     # BGRA
    if not ok:
        raise RuntimeError("couldn't encode the mask")
    return buf.tobytes()


def measure(path, iso=None, mask_size=None, f=FLOOR_EV) -> dict:
    """{"shares", "png" (bytes or None), "bayer", "flip", "floor"} for a raw
    file; raises when rawpy can't read it (JPEG, TIFF...)."""
    import rawpy
    with rawpy.imread(str(path)) as raw:
        data = raw.raw_image_visible
        if data is None:
            raise RuntimeError("no visible raw image")
        colors = raw.raw_colors_visible
        white = raw.camera_white_level_per_channel or [raw.white_level] * 4
        black = raw.black_level_per_channel or [0] * 4
        flip = int(raw.sizes.flip)
        pat = raw.raw_pattern
        bayer = (pat is not None and pat.shape == (2, 2)
                 and len(set(raw.color_desc.decode())) == 3)      # RGBG: G twice
        shares, cls = classify(data, colors, white, black, iso, f, bayer)
    png = _png(cls, int(mask_size), flip) if cls is not None and mask_size else None
    return {"shares": shares, "png": png, "bayer": bayer, "flip": flip, "floor": floor(iso, f=f)}
