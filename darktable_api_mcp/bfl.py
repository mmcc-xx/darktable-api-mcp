"""
bfl.py — fills made by Black Forest Labs (api.bfl.ai): FLUX.1 Fill [pro],
which paints what the prompt or the surroundings suggest, FLUX Erase,
which removes the object, and FLUX 3 Image, which edits the crop as an
instruction says (no mask: it re-renders the whole crop, and only the
hole is used).

The engine writes the crop around the shape and its hole as PNG files
(ai_fill crop_to); this sends them to BFL, waits for the fill and saves it
as a PNG for the engine to place (ai_fill filled). Only that crop leaves
the machine.

The API key: the BFL_API_KEY environment variable, else the file
~/.config/darktable-api-mcp/bfl_api_key (readable by the user only).
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path

import httpx

API = "https://api.bfl.ai/v1"
# the MCP's model names: BFL's endpoint and the name a fill is recorded with
MODELS = {"flux": ("flux-pro-1.0-fill", "FLUX.1 Fill [pro]"),
          "flux-erase": ("flux-tools/erase-v1", "FLUX Erase"),
          "flux3": ("flux-3-image", "FLUX 3 Image")}
# FLUX 3 takes no mask: an instruction says what to take away
FLUX3_PROMPT = ("Remove {what} in the middle of this photo, filling its place with the surroundings "
                "as if it had never been there, with the same texture, light and grain. "
                "Keep everything else in the image exactly unchanged.")
KEY_FILE = Path.home() / ".config" / "darktable-api-mcp" / "bfl_api_key"
WORKING = {"Pending", "Reasoning", "Generating", "Task not found"}  # not found: not queued yet


class BflError(Exception):
    pass


def _png_side(png: Path) -> int:
    """The larger side of a PNG, from its IHDR (no imaging library needed)."""
    head = png.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise BflError(f"{png} is not a PNG file")
    return max(int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def api_key() -> str:
    key = os.environ.get("BFL_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        raise BflError(f"no Black Forest Labs API key: set BFL_API_KEY or put it in {KEY_FILE}")
    return key


async def fill(model: str, image_png: Path, mask_png: Path, out_png: Path, prompt: str = "",
               timeout_s: float = 180.0) -> dict:
    """model ("flux", "flux-erase" or "flux3") on image_png where mask_png
    is white; the result is written to out_png. For "flux3" the prompt is
    what to remove ("the ring of stones"), or a whole instruction starting
    with "Remove ..."; empty: the object in the middle.
    Returns the task id, the cost in credits when BFL reports it, and the
    seconds it took."""
    key = api_key()
    endpoint = MODELS[model][0]
    image = base64.b64encode(image_png.read_bytes()).decode()
    if model == "flux3":
        p = prompt.strip()
        if not p.lower().startswith(("remove", "erase", "delete", "take away")):
            p = FLUX3_PROMPT.format(what=p or "the object")
        side = _png_side(image_png)
        res_tier = "1k" if side <= 1152 else "1.5k" if side <= 1664 else "2k"
        body = {"prompt": p, "images": [image], "aspect_ratio": "1:1", "resolution": res_tier,
                "grounding": False}
    else:
        body = {"image": image, "mask": base64.b64encode(mask_png.read_bytes()).decode(),
                "output_format": "png"}
        if model == "flux":
            body["prompt"] = prompt
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(f"{API}/{endpoint}", json=body, headers={"x-key": key, "accept": "application/json"})
        if r.status_code != 200:
            raise BflError(f"BFL refused the fill ({r.status_code}): {r.text[:300]}")
        task = r.json()
        poll = task.get("polling_url") or f"{API}/get_result?id={task['id']}"
        delay = 0.5
        while True:
            if time.monotonic() - t0 > timeout_s:
                raise BflError(f"BFL's fill took over {timeout_s:.0f} s (task {task.get('id')})")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 3.0)
            p = await http.get(poll, headers={"x-key": key, "accept": "application/json"})
            if p.status_code != 200:
                raise BflError(f"BFL's result could not be read ({p.status_code}): {p.text[:300]}")
            res = p.json()
            status = res.get("status")
            if status == "Ready":
                break
            if status not in WORKING:
                raise BflError(f"BFL's fill failed: {status} {res.get('details') or ''}".strip())
        url = (res.get("result") or {}).get("sample")
        if not url:
            raise BflError(f"BFL's reply has no image: {str(res)[:300]}")
        img = await http.get(url)          # a signed URL, valid for 10 minutes
        if img.status_code != 200:
            raise BflError(f"BFL's image could not be downloaded ({img.status_code})")
        if not img.content.startswith(b"\x89PNG"):
            raise BflError(f"BFL's {MODELS[model][1]} image is not a PNG ({img.headers.get('content-type')})")
        out_png.write_bytes(img.content)
    cost = res.get("cost") if res.get("cost") is not None else task.get("cost")
    return {"task": task.get("id"), "cost": cost, "seconds": round(time.monotonic() - t0, 1)}
