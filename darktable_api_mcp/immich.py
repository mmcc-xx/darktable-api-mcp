"""
immich.py — a small client for an Immich server's REST API (#56), shared
with the web app: albums, asset info, original downloads, uploads, stacks,
copying albums and favorite from one asset to another, and the trash.

Immich itself is not changed: an API key and the documented API only
(checked against Immich 3.2.4's own OpenAPI document). Nothing here
deletes for good: `trash` moves assets to Immich's trash, and the tools
only trash uploads they made themselves.

Configuration: the environment, else KEY=value lines in
~/.config/darktable-api-mcp/immich.conf (IMMICH_URL, IMMICH_INBOX,
DTAPI_IMMICH_DIR), and these files readable by the user only:
- IMMICH_URL: the server's API, e.g. https://immich.example.com/api
- IMMICH_API_KEY, else ~/.config/darktable-api-mcp/immich (the key alone)
- IMMICH_PANGOLIN: a file of KEY=value lines, default
  ~/.config/darktable-api-mcp/immich-pangolin, with P_ACCESS_TOKEN_ID and
  P_ACCESS_TOKEN: Pangolin's share-link headers for a server behind its
  sign-in (left out when the file doesn't exist)
- IMMICH_INBOX: the inbox album's name, default "Edit in darktable"
- DTAPI_IMMICH_DIR (server.py): where originals are downloaded to,
  default ~/Pictures/immich (a folder per year)
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

import httpx

CONFIG = Path.home() / ".config" / "darktable-api-mcp"
KEY_FILE = CONFIG / "immich"
CONF_FILE = CONFIG / "immich.conf"


def _kv(path: Path) -> dict:
    """KEY=value lines (shell style: export, quotes allowed)."""
    kv = {}
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().removeprefix("export ").partition("=")
            if sep and not k.startswith("#"):
                kv[k.strip()] = v.strip().strip("'\"")
    return kv


def setting(name: str, default: str = "") -> str:
    """An environment variable, else its line in immich.conf, else default."""
    return os.environ.get(name) or _kv(CONF_FILE).get(name) or default


PANGOLIN_FILE = Path(os.environ.get("IMMICH_PANGOLIN", str(CONFIG / "immich-pangolin")))
INBOX = setting("IMMICH_INBOX", "Edit in darktable")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class ImmichError(Exception):
    pass


def api_url() -> str:
    url = setting("IMMICH_URL").rstrip("/")
    if not url:
        raise ImmichError(f"no Immich server: set IMMICH_URL (its API, e.g. https://immich.example.com/api) "
                          f"or put an IMMICH_URL= line in {CONF_FILE}")
    return url


def asset_id(text: str) -> str:
    """The asset id in an id or an Immich URL (…/photos/<id>, …/albums/…/photos/<id>)."""
    ids = UUID.findall(text or "")
    if not ids:
        raise ImmichError(f"no Immich asset id in {text!r}: give the id or the photo's Immich URL")
    return ids[-1].lower()


def checksum(path: Path) -> str:
    """Immich's checksum of a file: base64 of its SHA-1."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode()


def _headers() -> dict:
    key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        raise ImmichError(f"no Immich API key: set IMMICH_API_KEY or put it in {KEY_FILE}")
    h = {"x-api-key": key, "Accept": "application/json"}
    if PANGOLIN_FILE.exists():
        kv = _kv(PANGOLIN_FILE)
        if kv.get("P_ACCESS_TOKEN_ID") and kv.get("P_ACCESS_TOKEN"):
            h["P-Access-Token-Id"] = kv["P_ACCESS_TOKEN_ID"]
            h["P-Access-Token"] = kv["P_ACCESS_TOKEN"]
    return h


class Immich:
    """async with Immich() as im: await im.albums() ..."""

    def __init__(self, timeout: float = 120):
        self.base = api_url()
        self.http = httpx.AsyncClient(headers=_headers(), timeout=timeout, follow_redirects=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.http.request(method, self.base + path, **kw)
        except httpx.HTTPError as exc:
            raise ImmichError(f"Immich at {self.base} can't be reached: {exc}") from exc
        if r.is_redirect:
            raise ImmichError(f"Immich answered with a redirect to {r.headers.get('location', '?')[:80]} "
                              "(a sign-in in front of it?): check the Pangolin share-link headers")
        if r.status_code >= 400:
            try:
                msg = r.json().get("message")
            except ValueError:
                msg = r.text[:200]
            raise ImmichError(f"Immich {method} {path}: {r.status_code} {msg}")
        return r

    async def _json(self, method: str, path: str, **kw):
        r = await self._req(method, path, **kw)
        return r.json() if r.content else None

    # ── server, albums ──────────────────────────────────────────────────
    async def version(self) -> str:
        v = await self._json("GET", "/server/version")
        return f"{v['major']}.{v['minor']}.{v['patch']}"

    async def albums(self, asset: str | None = None) -> list[dict]:
        """All albums (owned and shared), or those holding an asset."""
        return await self._json("GET", "/albums", params={"assetId": asset} if asset else None)

    async def album_named(self, name: str) -> dict | None:
        found = [a for a in await self.albums() if a["albumName"] == name]
        return found[0] if found else None

    async def create_album(self, name: str, assets: list[str] | None = None) -> dict:
        return await self._json("POST", "/albums", json={"albumName": name, "assetIds": assets or []})

    async def add_to_albums(self, albums: list[str], assets: list[str]) -> None:
        if albums and assets:
            await self._json("PUT", "/albums/assets", json={"albumIds": albums, "assetIds": assets})

    async def remove_from_album(self, album: str, assets: list[str]) -> None:
        await self._json("DELETE", f"/albums/{album}/assets", json={"ids": assets})

    async def album_assets(self, album: str) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page:
            r = await self._json("POST", "/search/metadata",
                                 json={"albumIds": [album], "size": 250, "page": page, "withExif": True})
            out += r["assets"]["items"]
            page = int(r["assets"]["nextPage"]) if r["assets"].get("nextPage") else 0
        return out

    # ── assets ──────────────────────────────────────────────────────────
    async def asset(self, aid: str) -> dict:
        return await self._json("GET", f"/assets/{aid}")

    async def download(self, aid: str, dest: Path) -> Path:
        """The original file as uploaded (not Immich's own edit of it)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with self.http.stream("GET", f"{self.base}/assets/{aid}/original",
                                        params={"edited": "false"}) as r:
                if r.status_code >= 300:
                    raise ImmichError(f"Immich download of {aid}: {r.status_code}")
                with open(tmp, "wb") as f:
                    async for block in r.aiter_bytes():
                        f.write(block)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise ImmichError(f"Immich download of {aid}: {exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)
        return dest

    async def upload(self, path: Path, created: str, favorite: bool = False) -> tuple[str, str]:
        """Upload a file; created: ISO date (Immich prefers the file's EXIF
        date when it has one). Returns (asset id, "created" or "duplicate":
        the same bytes are already there, under that id)."""
        data = {"fileCreatedAt": created, "fileModifiedAt": created, "filename": path.name,
                "isFavorite": "true" if favorite else "false"}
        with open(path, "rb") as f:
            r = await self._json("POST", "/assets", data=data,
                                 files={"assetData": (path.name, f, "image/jpeg")})
        return r["id"], r["status"]

    async def update(self, aid: str, **fields) -> dict:
        return await self._json("PUT", f"/assets/{aid}", json=fields)

    async def copy(self, source: str, target: str, albums=True, favorite=True, stack=True) -> None:
        """Albums, favorite and stack of source onto target (Immich's copy;
        stack: target joins source's stack)."""
        await self._json("PUT", "/assets/copy", json={"sourceId": source, "targetId": target, "albums": albums,
                                                      "favorite": favorite, "stack": stack,
                                                      "sharedLinks": False, "sidecar": False})

    async def trash(self, assets: list[str]) -> None:
        """To Immich's trash (not deleted: force is never set)."""
        await self._json("DELETE", "/assets", json={"ids": assets, "force": False})

    # ── stacks ──────────────────────────────────────────────────────────
    async def create_stack(self, assets: list[str]) -> dict:
        """The first asset becomes the primary (the one shown)."""
        return await self._json("POST", "/stacks", json={"assetIds": assets})

    async def set_primary(self, stack: str, aid: str) -> None:
        await self._json("PUT", f"/stacks/{stack}", json={"primaryAssetId": aid})

    async def remove_from_stack(self, stack: str, aid: str) -> None:
        await self._json("DELETE", f"/stacks/{stack}/assets/{aid}")
