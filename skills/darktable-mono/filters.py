"""Grey mixes for darktable's color calibration that emulate colored lens
filters on panchromatic black and white film (the table in SKILL.md).

With a monochrome preset, color calibration mixes grey from CIE XYZ (D50):
grey = R*X + G*Y + B*Z. For each filter, the target is the film's response
through the filter, sum(reflectance * D50 * film * filter), scaled so a
perfect white stays 1 (the filter factor compensated). The three weights
are a least-squares fit of that target over the 24 ColorChecker patches,
with white weighted strongly. Filters are modelled as smooth edges, not
measured spectra.

pip install colour-science; python filters.py
"""

import warnings

import numpy as np

warnings.filterwarnings("ignore")
import colour  # noqa: E402

shape = colour.SpectralShape(380, 730, 5)
lam = np.arange(380, 731, 5.0)
cmf = colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"].copy().align(shape).values
d50 = colour.SDS_ILLUMINANTS["D50"].copy().align(shape).values
checker = colour.SDS_COLOURCHECKERS["BabelColor Average"]
refl = np.array([sd.copy().align(shape).values for sd in checker.values()])


def edge(center, width=9):
    return 1 / (1 + np.exp(-(lam - center) / width))


def band(low, high, width=9):
    return edge(low, width) * (1 - edge(high, width))


film = edge(395, 8) * (1 - edge(655, 10))  # generic panchromatic film
FILTERS = {
    "none (panchromatic film)": np.ones_like(lam),
    "yellow (Wratten 8)": edge(485),
    "deep yellow (Wratten 15)": edge(520),
    "orange (Wratten 21)": edge(555),
    "red (Wratten 25)": edge(600),
    "yellow-green (Wratten 11)": edge(480) * (1 - 0.6 * edge(590)),
    "green (Wratten 58)": band(495, 590),
    "blue (Wratten 47)": band(380, 495),
}

white = (cmf * d50[:, None]).sum(0)
white /= white[1]
xyz = (refl[:, :, None] * cmf[None] * d50[None, :, None]).sum(1) / (cmf[:, 1] * d50).sum()

for name, filt in FILTERS.items():
    response = film * filt * d50
    target = (refl * response[None]).sum(1) / response.sum()
    a = np.vstack([xyz, 10 * white])
    b = np.append(target, 10.0)
    w, *_ = np.linalg.lstsq(a, b, rcond=None)
    err = np.abs(xyz @ w - target).max()
    v = w / w.sum()  # normalize channels on: only the ratios matter
    if np.abs(v).max() > 2:  # darktable's range is -2..2
        v /= 2
    print(f"{name:27s} grey [{v[0]:.2f}, {v[1]:.2f}, {v[2]:.2f}]  "
          f"neutral {np.log2(1 / w.sum()):+.2f} EV  fit error {err:.3f}")
