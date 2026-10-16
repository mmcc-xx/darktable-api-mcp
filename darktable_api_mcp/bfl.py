"""
bfl.py — fills made by Black Forest Labs (api.bfl.ai): FLUX.1 Fill [pro],
which paints what the prompt or the surroundings suggest, and FLUX Erase,
which removes the object.

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
          "flux-erase": ("flux-tools/erase-v1", "FLUX Erase")}
KEY_FILE = Path.home() / ".config" / "darktable-api-mcp" / "bfl_api_key"
WORKING = {"Pending", "Reasoning", "Generating", "Task not found"}  # not found: not queued yet


class BflError(Exception):
    pass


def api_key() -> str:
    key = os.environ.get("BFL_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        raise BflError(f"no Black Forest Labs API key: set BFL_API_KEY or put it in {KEY_FILE}")
    return key


async def fill(model: str, image_png: Path, mask_png: Path, out_png: Path, prompt: str = "",
               timeout_s: float = 180.0) -> dict:
    """model ("flux" or "flux-erase") on image_png where mask_png is white;
    the result is written to out_png. Returns the task id, the cost in
    credits when BFL reports it, and the seconds it took."""
    key = api_key()
    endpoint = MODELS[model][0]
    body = {
        "image": base64.b64encode(image_png.read_bytes()).decode(),
        "mask": base64.b64encode(mask_png.read_bytes()).decode(),
        "output_format": "png",
    }
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
        out_png.write_bytes(img.content)
    return {"task": task.get("id"), "cost": res.get("cost"), "seconds": round(time.monotonic() - t0, 1)}
