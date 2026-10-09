"""
engine.py — client for the darktable-api engine.

One engine serves every front end that uses the same library: the web app,
the MCP server, anything else. It listens on a unix socket; this client
connects to it and starts it first if nobody has (the engine then stops by
itself once no client has been connected for a while and nothing is
unsaved).

Edit sessions belong to the engine, one per open image, so two clients that
open the same photo share its edit (unsaved changes included) and the engine
tells each client what the others changed: add_listener() receives those
events ({"type": "edit" | "saved" | "reset" | "reopened" | "closed" |
"image" | "library_released" | "library_acquired", "imgid", ...}).

Requests from this client go one at a time; edits and previews come before
thumbnails, and a preview overtaken by a newer one returns None.

Configuration (environment variables):
  DTAPI_BIN          the darktable-api binary
  DTAPI_CONFIGDIR    darktable config dir with the library to use (a copy!);
                     required
  DTAPI_CACHEDIR     darktable cache dir for the engine (default: next to it)
  DTAPI_SOCKET       the engine's socket (default: next to the config dir)
  DTAPI_MAX_SESSIONS images the engine keeps open at once (default 3)
  DTAPI_IDLE_EXIT    seconds the engine stays up with no client (default 600)
  DTAPI_GUI_BIN      the darktable GUI a takeover may quit (default:
                     "darktable" next to DTAPI_BIN, the same source tree)
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

BIN = Path(os.environ.get("DTAPI_BIN") or shutil.which("darktable-api") or "darktable-api")
CONFIG = Path(os.environ.get("DTAPI_CONFIGDIR") or "DTAPI_CONFIGDIR-not-set").expanduser().resolve()
CACHE = Path(os.environ.get("DTAPI_CACHEDIR", CONFIG.parent / "cache")).expanduser().resolve()
GUI_BIN = Path(os.environ.get("DTAPI_GUI_BIN") or BIN.parent / "darktable")
LOG = CONFIG.parent / "engine.log"
MAX_SESSIONS = int(os.environ.get("DTAPI_MAX_SESSIONS", "3"))
IDLE_EXIT_S = int(os.environ.get("DTAPI_IDLE_EXIT", "600"))
CALL_TIMEOUT_S = 120
START_TIMEOUT_S = 60
QUIT_TIMEOUT_S = 60


def _socket_path() -> Path:
    if os.environ.get("DTAPI_SOCKET"):
        return Path(os.environ["DTAPI_SOCKET"]).expanduser()
    p = CONFIG.parent / "darktable-api.sock"
    if len(str(p)) < 100:   # unix socket paths are limited (104 bytes on macOS)
        return p
    # a fixed folder, not tempfile.gettempdir(): that follows TMPDIR, which
    # differs between clients (launchd, an MCP host, a shell), and every
    # client of this library must find the same socket
    tag = hashlib.sha1(str(CONFIG).encode()).hexdigest()[:12]
    return Path("/tmp") / f"darktable-api-{os.getuid()}-{tag}.sock"


SOCKET = _socket_path()


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self):
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._render_seq = 0
        self._edits_waiting = 0
        self._listeners: list[Callable[[dict], None]] = []
        self.image_id: int | None = None      # the image this client last opened

    def available(self) -> tuple[bool, str]:
        if not BIN.exists():
            return False, f"darktable-api not found at {BIN} (set DTAPI_BIN)"
        if not (CONFIG / "library.db").exists():
            return False, f"no library.db in {CONFIG} (set DTAPI_CONFIGDIR to a library copy)"
        return True, ""

    # ── connection ──────────────────────────────────────────────────────────

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    async def _connect(self) -> None:
        async with self._connect_lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            try:
                await self._open()
            except OSError:
                ok, why = self.available()
                if not ok:
                    raise EngineError(why)
                self._spawn()
                t = time.time()
                while True:
                    try:
                        await self._open()
                        break
                    except OSError:
                        if time.time() - t > START_TIMEOUT_S:
                            raise EngineError(f"the engine didn't start (see {LOG})")
                        await asyncio.sleep(0.2)

    async def _open(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(SOCKET), limit=1 << 24)
        self._read_task = asyncio.create_task(self._read_loop())

    def _spawn(self) -> None:
        """Start the engine on its own (not a child of this process), so it
        keeps serving other clients when this one exits."""
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "ab") as log:
            subprocess.Popen(
                [str(BIN), "--listen", str(SOCKET), "--max-sessions", str(MAX_SESSIONS),
                 "--idle-exit", str(IDLE_EXIT_S),
                 "--core", "--configdir", str(CONFIG), "--cachedir", str(CACHE)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True)

    async def _read_loop(self) -> None:
        reader = self._reader
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("method") == "event":
                    for fn in list(self._listeners):
                        try:
                            fn(msg.get("params") or {})
                        except Exception:
                            pass
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(EngineError("lost the connection to the engine"))
            self._pending.clear()
            if self._writer is not None:
                self._writer.close()
            self._writer = None

    async def stop(self) -> None:
        """Disconnect; the engine keeps running for other clients."""
        if self._writer is not None:
            self._writer.close()
        if self._read_task is not None:
            self._read_task.cancel()
        self._writer = None

    async def _call(self, method: str, **params) -> dict:
        """Send one request; the caller holds the lock."""
        if self._writer is None or self._writer.is_closing():
            await self._connect()
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                        "params": params}) + "\n").encode())
        try:
            await self._writer.drain()
            msg = await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            self._pending.pop(rid, None)
            await self.stop()
            raise EngineError(f"the engine stopped responding ({type(exc).__name__})") from exc
        if "error" in msg:
            raise EngineError(msg["error"].get("message", "engine error"))
        return msg.get("result") or {}

    # ── requests ────────────────────────────────────────────────────────────

    async def call(self, method: str, **params) -> dict:
        """A request that needs no open image (browsing, ratings, library)."""
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            return await self._call(method, **params)

    async def _session_call(self, image_id: int, method: str, **params) -> dict:
        """A request on image_id's edit session; (re)opens it if the engine
        doesn't have it open (never opened, closed to make room, restarted)."""
        try:
            return await self._call(method, imgid=image_id, **params)
        except EngineError as exc:
            if "is not open" not in str(exc):
                raise
        await self._call("session_open", imgid=image_id)
        return await self._call(method, imgid=image_id, **params)

    async def edit(self, image_id: int, method: str, **params) -> dict:
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            return await self._session_call(image_id, method, **params)

    async def open(self, image_id: int, fresh: bool = False) -> dict:
        """Open the image for editing: joins the session another client may
        have open (with its unsaved changes) unless fresh, which reloads it as
        saved, for everyone."""
        self._edits_waiting += 1
        async with self._lock:
            self._edits_waiting -= 1
            r = await self._call("session_open", imgid=image_id, fresh=fresh)
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
            return await self._to_file(lambda **p: self._session_call(image_id, "render", **p),
                                       width=width, height=height, quality=85)

    async def thumbnail(self, image_id: int, size: int) -> bytes:
        """darktable's thumbnail for the image; yields to queued edits."""
        while self._edits_waiting:
            await asyncio.sleep(0.05)
        async with self._lock:
            data, _ = await self._to_file(lambda **p: self._call("thumbnail", **p),
                                          imgid=image_id, size=size, quality=80)
            return data

    async def _to_file(self, send, **params) -> tuple[bytes, dict]:
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="dtapi_")
        os.close(fd)
        try:
            t = time.time()
            r = await send(path=path, **params)
            r["total_ms"] = round((time.time() - t) * 1000)
            return Path(path).read_bytes(), r
        finally:
            os.unlink(path)

    # ── the library lock ────────────────────────────────────────────────────

    async def library(self, method: str) -> dict:
        """library_status / library_release / library_acquire."""
        async with self._lock:
            return await self._call(method)

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
