"""clipping.classify, floor and the mask's rotation on synthetic RGGB arrays
(no rawpy). Run: python tests/test_clipping.py (or pytest)."""

import numpy as np

from darktable_api_mcp import clipping

WHITE, BLACK = [16000] * 4, [0] * 4
RED, ORANGE, BLUE, NONE = clipping.RED, clipping.ORANGE, clipping.BLUE, clipping.NONE


def _raw(blocks: list[int]):
    """A mosaic of 2x2 blocks in a row, each filled with one value (RGGB)."""
    data = np.kron(np.array([blocks]), np.ones((2, 2), int))
    colors = np.tile(np.array([[0, 1], [3, 2]]), (1, len(blocks)))
    return data, colors


def test_classes():
    data, colors = _raw([16000, 8000, 2, 8000])
    data[0, 6] = 16000                     # one white photosite in the last block
    shares, cls = clipping.classify(data, colors, WHITE, BLACK, iso=200)
    assert cls.tolist() == [[RED, NONE, BLUE, ORANGE]]
    assert shares == {"all_clipped_pct": 25.0, "some_clipped_pct": 25.0, "blocked_shadows_pct": 25.0}


def test_floor_param():
    data, colors = _raw([2, 8000])         # 2/16000 = 2^-13
    assert clipping.classify(data, colors, WHITE, BLACK, iso=200)[1].tolist() == [[BLUE, NONE]]
    assert clipping.classify(data, colors, WHITE, BLACK, iso=200, f=13.5)[1].tolist() == [[NONE, NONE]]
    # a higher ISO raises the floor: 2^-13 is noise at ISO 800 even with F = 13
    assert clipping.classify(data, colors, WHITE, BLACK, iso=800, f=13.0)[1].tolist() == [[BLUE, NONE]]


def test_floor_scales_with_iso():
    assert clipping.floor(400) == 2 * clipping.floor(200)
    assert clipping.floor(None) == clipping.floor(200)


def test_non_bayer():
    data, colors = _raw([16000, 8000, 2, 8000])
    shares, cls = clipping.classify(data, colors, WHITE, BLACK, iso=200, bayer=False)
    assert cls is None and shares["all_clipped_pct"] == 25.0 and shares["blocked_shadows_pct"] == 25.0


def test_png_flip():
    import cv2
    cls = np.zeros((2, 8), np.uint8)       # a wide block grid
    cls[0, 0] = RED
    sizes = {}
    for flip in (0, 3, 5, 6):
        im = cv2.imdecode(np.frombuffer(clipping._png(cls, 1024, flip), np.uint8), cv2.IMREAD_UNCHANGED)
        sizes[flip] = im.shape[:2]
        assert im.shape[2] == 4
    assert sizes[0] == sizes[3] == (2, 8) and sizes[5] == sizes[6] == (8, 2)
    # the top-left block: bottom-right after 180, bottom-left after 90 CCW, top-right after 90 CW
    im = lambda flip: cv2.imdecode(np.frombuffer(clipping._png(cls, 1024, flip), np.uint8), cv2.IMREAD_UNCHANGED)
    assert tuple(im(0)[0, 0]) == (0, 0, 255, 200)                        # BGRA red
    assert im(3)[1, 7, 3] == 200 and im(5)[7, 0, 3] == 200 and im(6)[0, 1, 3] == 200
    # pooled down: the long side is at most the size, the worst class wins
    assert clipping._png(cls, 4, 0) and cv2.imdecode(np.frombuffer(clipping._png(cls, 4, 0), np.uint8), cv2.IMREAD_UNCHANGED).shape[:2] == (1, 4)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
