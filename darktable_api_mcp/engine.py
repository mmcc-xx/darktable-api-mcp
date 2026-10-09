"""
engine.py — client for the darktable-api engine.

Owns one long-running `darktable-api` process (started on first use) and
talks JSON-RPC to it over stdin/stdout. The engine is single-threaded, so
requests go one at a time:

- edits and previews come first: thumbnail requests wait while an edit is
  queued, so a grid full of thumbnails never delays a slider;
- previews are "latest wins": a render request overtaken by a newer one
  returns None instead of rendering.

The engine is restarted if it dies and stopped after IDLE_S without use.

Configuration (environment variables):
  DTAPI_BIN       path to the darktable-api binary
  DTAPI_CONFIGDIR darktable config dir with the library to use (a copy!);
                  required
  DTAPI_CACHEDIR  darktable cache dir for the engine (default: <configdir>/../cache)
  DTAPI_GUI_BIN   the darktable GUI a takeover may quit (default: "darktable"
                  next to DTAPI_BIN, i.e. built from the same source tree)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(os.environ.get("DTAPI_BIN") or shutil.which("darktable-api") or "darktable-api")
CONFIG = Path(os.environ.get("DTAPI_CONFIGDIR") or "DTAPI_CONFIGDIR-not-set").expanduser().resolve()
CACHE = Path(os.environ.get("DTAPI_CACHEDIR", CONFIG.parent / "cache")).expanduser().resolve()
GUI_BIN = Path(os.environ.get("DTAPI_GUI_BIN") or BIN.parent / "darktable")
LOG = CONFIG.parent / "engine.log"
IDLE_S = 600
CALL_TIMEOUT_S = 120
QUIT_TIMEOUT_S = 60


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._render_seq = 0
        self._edits_waiting = 0
        self._last_use = 0.0
        self._idle_task: asyncio.Task | None = None
        self.image_id: int | None = None      # the engine's open edit session

    def available(self) -> tuple[bool, str]:
        if not BIN.exists():
            return False, f"darktable-api not found at {BIN} (set DTAPI_BIN)"
        if not (CONFIG / "library.db").exists():
            return False, (f"no library.db in {CONFIG}: set DTAPI_CONFIGDIR to a darktable config dir "
                           "(a copy made with make_library_copy.py)")
        return True, ""

    # ── process ─────────────────────────────────────────────────────────────

    async def _start(self) -> None:
        ok, why = self.available()
        if not ok:
            raise EngineError(why)
        CACHE.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            str(BIN), "--core", "--configdir", str(CONFIG), "--cachedir", str(CACHE),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=open(LOG, "ab"), limit=1 << 24)
        self.image_id = None
        await self._call("ping")
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        while self._proc is not None:
            await asyncio.sleep(30)
            if time.time() - self._last_use > IDLE_S and not self._lock.locked():
                await self.stop()

    async def stop(self) -> None:
        async with self._lock:
            proc, self._proc, self.image_id = self._proc, None, None
            if proc is None or proc.returncode is not None:
                return
            try:
                await self._send(proc, "shutdown", {})
                await asyncio.wait_for(proc.wait(), 30)
            except Exception:
                proc.terminate()   # the engine shuts down cleanly on SIGTERM

    async def _send(self, proc, method: str, params: dict) -> dict:
        rid = next(self._ids)
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                      "params": params}) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), CALL_TIMEOUT_S)
        if not line:
            raise EngineError("the engine exited (see engine.log)")
        msg = json.loads(line)
        if "error" in msg:
            raise EngineError(msg["error"].get("message", "engine error"))
        return msg.get("result") or {}

    async def _call(self, method: str, **params) -> dict:
        """Send one request; the caller holds the lock (or is _start)."""
        if self._proc is None or self._proc.returncode is not None:
            await self._start()
        self._last_use = time.time()
        try:
            return await self._send(self._proc, method, params)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as exc:
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
            self._proc, self.image_id = None, None
            raise EngineError(f"the engine stopped responding ({type(exc).__name__})") from exc
        except EngineError as exc:
            if "exited" in str(exc):
                self._proc, self.image_id = None, None
            raise

    # ── requests ────────────────────────────────────────────────────────────

    async def call(self, method: str, **params) -> dict:
        """A request that needs no open photo (browsing, ratings, library)."""
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            return await self._call(method, **params)

    async def edit(self, image_id: int, method: str, **params) -> dict:
        """A request on the edit session for image_id, opening it if needed."""
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            await self._ensure_open(image_id)
            return await self._call(method, **params)

    async def _ensure_open(self, image_id: int) -> None:
        if self.image_id != image_id:
            await self._call("session_open", imgid=image_id)
            self.image_id = image_id

    async def open(self, image_id: int) -> dict:
        """(Re)open the photo, discarding unsaved changes."""
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            r = await self._call("session_open", imgid=image_id)
            self.image_id = image_id
            return r

    async def render(self, image_id: int, width: int, height: int) -> tuple[bytes, dict] | None:
        """JPEG bytes and timings, or None if a newer render request arrived
        while this one waited."""
        self._render_seq += 1
        seq = self._render_seq
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            if seq != self._render_seq:
                return None
            await self._ensure_open(image_id)
            return await self._to_file("render", width=width, height=height, quality=85)

    async def thumbnail(self, image_id: int, size: int) -> bytes:
        """darktable's thumbnail for the photo; yields to queued edits."""
        while self._edits_waiting:
            await asyncio.sleep(0.05)
        async with self._lock:
            data, _ = await self._to_file("thumbnail", imgid=image_id, size=size, quality=80)
            return data

    async def _to_file(self, method: str, **params) -> tuple[bytes, dict]:
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="dtapi_")
        os.close(fd)
        try:
            t = time.time()
            r = await self._call(method, path=path, **params)
            r["total_ms"] = round((time.time() - t) * 1000)
            return Path(path).read_bytes(), r
        finally:
            os.unlink(path)

    # ── the library lock ────────────────────────────────────────────────────

    async def library(self, method: str) -> dict:
        """library_status / library_release / library_acquire."""
        async with self._lock:
            r = await self._call(method)
            if method == "library_release":
                self.image_id = None
            elif method == "library_acquire":
                self.image_id = r.get("draft_imgid") if r.get("draft") else None
            return r

    async def takeover(self) -> dict:
        """Quit the darktable GUI holding the library the normal way (as its
        quit menu item: it saves its edits), wait for it to exit, then take
        the library back. Never kills it: if it doesn't quit (a dialog open,
        a job running), report that and leave it running."""
        st = await self.library("library_status")
        if st.get("state") != "released":
            raise EngineError("the engine already has the library")
        pid = st.get("holder_pid") or 0
        if pid:
            exe = (await _run("ps", "-o", "comm=", "-p", str(pid))).strip()
            if not exe or Path(exe).resolve() != GUI_BIN.resolve():
                raise EngineError(f"the library is held by {exe or 'process ' + str(pid)}, not by "
                                  f"{GUI_BIN} (DTAPI_GUI_BIN); not quitting it")
            asked = await _ask_to_quit(pid)
            t = time.time()
            while _alive(pid) and time.time() - t < QUIT_TIMEOUT_S:
                await asyncio.sleep(0.5)
            if _alive(pid):
                raise EngineError(f"darktable (process {pid}) didn't quit within {QUIT_TIMEOUT_S} s "
                                  f"({asked}); a dialog may be open there. It was left running.")
        r = await self.library("library_acquire")
        r["quit_pid"] = pid
        return r


async def _ask_to_quit(pid: int) -> str:
    """darktable's own quit path: on macOS the application terminate request
    (as Cmd+Q), elsewhere its D-Bus Quit method (src/common/dbus.c)."""
    if sys.platform == "darwin":
        out = await _run("osascript", "-l", "JavaScript", "-e",
                         'ObjC.import("AppKit");var a=$.NSRunningApplication.'
                         f'runningApplicationWithProcessIdentifier({pid});'
                         'a.isNil()?"not an application":"asked: "+String(a.terminate)')
        return out.strip()
    out = await _run("gdbus", "call", "--session", "--dest", "org.darktable.service",
                     "--object-path", "/darktable", "--method", "org.darktable.service.Remote.Quit")
    return "asked over D-Bus" + (f" ({out.strip()})" if out.strip() else "")


async def _run(*cmd: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        return f"{cmd[0]} not found"
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


engine = Engine()
