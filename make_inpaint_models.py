"""Builds darktable's inpaint model folders for darktable-api's ai_fill.

    python3 -m venv /tmp/onnxvenv && /tmp/onnxvenv/bin/pip install onnx onnxruntime numpy
    /tmp/onnxvenv/bin/python -I make_inpaint_models.py OUT_DIR [--download-dir DIR] [--only lama|migan]

inpaint-lama (the default in ai_fill): LaMa, Carve/LaMa-ONNX's lama_fp32.onnx
(Apache-2.0; ~208 MB), used as it is: inputs image [1,3,512,512] 0..1 and mask
[1,1,512,512] 1 = to fill, output 0..255. Fills fine texture (grass) well.

inpaint-migan (smaller, faster, weaker on fine texture):

Downloads migan.onnx (andraniksargsyan/migan on Hugging Face, the MI-GAN
author's; MIT, Picsart AI Research) and cuts out the float generator: the
official file is a uint8 pipeline (resize to 512, normalize, blend, back to
uint8). darktable fills scene-linear float patches, so it does that work
itself and runs only the generator:

    input  [1, 4, 512, 512] float: [mask - 0.5, image * mask], image in -1..1
           (RGB, encoded for display), mask 1 = known, 0 = to fill
    output [1, 3, 512, 512] float: the image in -1..1 (use it inside the hole)

Writes OUT_DIR/inpaint-lama/ and OUT_DIR/inpaint-migan/ (model.onnx,
config.json, LICENSE); put those folders in darktable's models folder
(~/.local/share/darktable/models/ or plugins/ai/models_path). Checks the cut
MI-GAN generator against its full pipeline, and LaMa's inputs and outputs.
"""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import utils

REPO = "https://huggingface.co/andraniksargsyan/migan/resolve/main/"
LAMA_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
SHA256 = {"migan.onnx": "593eba0b7e04730f1b61c0a3cbca68d97d8d6a7ff5c6a44a7b9d7fcd880fc5ae",
          "lama_fp32.onnx": "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"}
LAMA_CONFIG = {
    "id": "inpaint-lama",
    "name": "inpaint lama",
    "description": "LaMa 512 (big-lama, Places2) inpainting, ONNX by Carve. Apache-2.0",
    "task": "inpaint",
    "arch": "lama",
    "backend": "onnx",
}
LAMA_LICENSE = """LaMa: Resolution-robust Large Mask Inpainting with Fourier Convolutions
https://github.com/advimman/lama - Apache License 2.0
(https://www.apache.org/licenses/LICENSE-2.0). ONNX export:
https://huggingface.co/Carve/LaMa-ONNX (Apache-2.0). Trained on Places2.
"""
CUT_IN, CUT_OUT = "222", "1299"   # Concat([mask - 0.5, image * mask]) .. generator output

CONFIG = {
    "id": "inpaint-migan",
    "name": "inpaint mi-gan",
    "description": "MI-GAN 512 (Places2) inpainting generator, float in/out. Picsart AI Research, MIT",
    "task": "inpaint",
    "arch": "migan",
    "backend": "onnx",
}


def fetch(name: str, dl: Path, url: str | None = None) -> Path:
    path = dl / name
    if not path.exists():
        urllib.request.urlretrieve(url or REPO + name, path)
    if name in SHA256 and hashlib.sha256(path.read_bytes()).hexdigest() != SHA256[name]:
        raise SystemExit(f"{path}: unexpected checksum")
    return path


def cut_generator(src: Path, dst: Path):
    utils.extract_model(str(src), str(dst), [CUT_IN], [CUT_OUT])
    m = onnx.load(dst)
    g = m.graph

    def fix(v, dims):
        for d, n in zip(v.type.tensor_type.shape.dim, dims):
            d.ClearField("dim_param")
            d.dim_value = n

    fix(g.input[0], [1, 4, 512, 512])
    fix(g.output[0], [1, 3, 512, 512])
    for n in g.node:
        n.input[:] = ["input" if i == CUT_IN else i for i in n.input]
        n.output[:] = ["output" if o == CUT_OUT else o for o in n.output]
    g.input[0].name, g.output[0].name = "input", "output"
    del g.value_info[:]
    m = onnx.shape_inference.infer_shapes(m)
    onnx.checker.check_model(m)
    onnx.save(m, dst)


def check(pipeline: Path, gen: Path):
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:512, 0:512]
    img = np.stack([x / 2, y / 2, (x + y) / 4], -1) % 256
    img = np.clip(img + rng.normal(0, 8, img.shape), 0, 255).astype(np.uint8)
    mask = np.full((512, 512), 255, np.uint8)
    mask[200:330, 180:300] = 0
    full = ort.InferenceSession(str(pipeline), providers=["CPUExecutionProvider"])
    ref = full.run(None, {"image": img.transpose(2, 0, 1)[None], "mask": mask[None, None]})[0][0].transpose(1, 2, 0)
    g = ort.InferenceSession(str(gen), providers=["CPUExecutionProvider"])
    xi = img.astype(np.float32).transpose(2, 0, 1)[None] / 255 * 2 - 1
    mk = (mask.astype(np.float32) / 255)[None, None]
    out = g.run(None, {"input": np.concatenate([mk - 0.5, xi * mk], 1)})[0][0].transpose(1, 2, 0)
    out8 = np.clip((out * 0.5 + 0.5) * 255, 0, 255)
    d = np.abs(out8[mask == 0] - ref[mask == 0]).max()
    if d > 1.0:
        raise SystemExit(f"generator differs from the official pipeline by {d} levels")
    print(f"generator matches the official pipeline (max {d:.1f} 8-bit levels in the hole)")


def make_lama(out: Path, dl: Path):
    src = fetch("lama_fp32.onnx", dl, LAMA_URL)
    folder = out / LAMA_CONFIG["id"]
    folder.mkdir(parents=True, exist_ok=True)
    s = ort.InferenceSession(str(src), providers=["CPUExecutionProvider"])
    ins = [(i.name, i.shape[1:]) for i in s.get_inputs()]
    if ins != [("image", [3, 512, 512]), ("mask", [1, 512, 512])]:
        raise SystemExit(f"unexpected LaMa inputs {ins}")
    y = s.run(None, {"image": np.full((1, 3, 512, 512), 0.5, np.float32),
                     "mask": np.zeros((1, 1, 512, 512), np.float32)})[0]
    if not (y.shape == (1, 3, 512, 512) and 100 < y.mean() < 155):
        raise SystemExit(f"unexpected LaMa output {y.shape} mean {y.mean()}")
    print("LaMa: inputs and output as expected (0..255)")
    dst = folder / "model.onnx"
    if not dst.exists():
        dst.write_bytes(src.read_bytes())
    (folder / "config.json").write_text(json.dumps(LAMA_CONFIG, indent=2) + "\n")
    (folder / "LICENSE").write_text(LAMA_LICENSE)
    print(f"wrote {folder}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--download-dir", type=Path)
    ap.add_argument("--only", choices=("lama", "migan"))
    a = ap.parse_args()
    dl = a.download_dir or a.out / "download"
    dl.mkdir(parents=True, exist_ok=True)
    if a.only != "migan":
        make_lama(a.out, dl)
    if a.only == "lama":
        return
    folder = a.out / CONFIG["id"]
    folder.mkdir(parents=True, exist_ok=True)
    src = fetch("migan.onnx", dl)
    fetch("LICENSE", dl)
    cut_generator(src, folder / "model.onnx")
    check(src, folder / "model.onnx")
    (folder / "config.json").write_text(json.dumps(CONFIG, indent=2) + "\n")
    (folder / "LICENSE").write_bytes((dl / "LICENSE").read_bytes())
    print(f"wrote {folder}")


if __name__ == "__main__":
    main()
