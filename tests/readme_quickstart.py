"""
readme_quickstart.py — run the README's Quick start as a new user would.

Takes the commands from README.md's Quick start (the steps for one platform)
and runs them in a fresh home folder: install the build dependencies, clone
and build the fork's branch, install the MCP server, copy a library, then
check the MCP server and the web app work. Only what a test can't do is
changed: `apt install` gets -y, `xcode-select --install` becomes a check,
`claude mcp add` becomes a check of the MCP server with the same command and
variables, and the web app runs in the background and is fetched. A library
with one raw (darktable's test image) stands in for the user's
~/.config/darktable. darktable's window (step 7) is not run.

    python3 tests/readme_quickstart.py --platform macos --branch darktable-api \\
        --home /tmp/qs-home [--mcp-src .] [--port 8020] [--print]

--mcp-src clones the MCP server from this checkout instead of GitHub (to test
a change before it is pushed). The `seed` and `smoke` subcommands are run by
the generated script with the venv's Python.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
README = HERE.parent.parent / "README.md"
RAW_URL = "https://raw.githubusercontent.com/darktable-org/darktable-tests/master/images/mire1.cr2"
MCP_URL = "https://github.com/mmcc-xx/darktable-api-mcp.git"
PLATFORMS = {"macos": "macOS:", "ubuntu": "Ubuntu 24.04:"}


def quickstart_steps(readme: str, platform: str) -> list[tuple[int, list[str]]]:
    """The Quick start's numbered steps and, for each, its code blocks for
    this platform (each block as one shell text, dedented)."""
    sec = readme.split("### Steps", 1)[1]
    sec = re.split(r"^(?:The AI mask|## )", sec, maxsplit=1, flags=re.M)[0]
    steps: list[tuple[int, list[str]]] = []
    only = None              # the platform label the current lines are under
    block: list[str] = []

    def flush():
        if block and steps and only in (None, PLATFORMS[platform]):
            steps[-1][1].append("\n".join(block).strip("\n"))
        block.clear()

    for line in sec.splitlines():
        if line.startswith("       ") or (block and not line.strip()):
            block.append(line[7:])
            continue
        flush()
        if m := re.match(r"^(\d+)\. ", line):
            steps.append((int(m[1]), []))
            only = None
        elif line.strip() in PLATFORMS.values():
            only = line.strip()
    flush()
    return steps


def commands(block: str) -> list[str]:
    """A code block's shell commands (backslash continuations joined)."""
    out, cur = [], ""
    for line in block.splitlines():
        cur += line
        if cur.endswith("\\"):
            cur = cur[:-1] + " "
            continue
        if cur.strip():
            out.append(cur.strip())
        cur = ""
    return out


def adapt(cmd: str, a, harness: str) -> list[str]:
    """One README command as the test runs it."""
    venv_py = "~/src/darktable-api-mcp/.venv/bin/python"
    if cmd.startswith("xcode-select --install"):
        return ["xcode-select -p   # README: xcode-select --install"]
    if re.match(r"(sudo )?apt(-get)? install", cmd):
        return [re.sub(r"apt(-get)? install", "DEBIAN_FRONTEND=noninteractive apt-get install -y", cmd, count=1)]
    if cmd.startswith("brew bundle"):
        return ["HOMEBREW_BUNDLE_NO_UPGRADE=1 HOMEBREW_NO_AUTO_UPDATE=1 " + cmd]
    if a.mcp_src and cmd.startswith(f"git clone {MCP_URL}"):
        return [f"git clone {shlex.quote(str(Path(a.mcp_src).resolve()))} darktable-api-mcp   # README: {cmd}"]
    if "make_library_copy.py" in cmd:
        # the user's darktable library: one raw imported by the engine
        return [f"{venv_py} {harness} seed", cmd]
    if cmd.startswith("claude mcp add"):
        argv = shlex.split(cmd)
        env = [argv[i + 1] for i, x in enumerate(argv) if x == "-e"]
        server = argv[argv.index("--") + 1]
        # double quotes: the README's $HOME expands as it would in the user's shell
        env_args = " ".join('"%s"' % e for e in env)
        return [f'env {env_args} {venv_py} {harness} smoke "{server}"'
                f"   # README: claude mcp add"]
    if "uvicorn" in cmd:
        cmd = cmd.replace("--port 8020", f"--port {a.port}")
        return [f"{cmd} > ~/webapp.log 2>&1 & WEB=$!",
                f"for i in $(seq 60); do curl -sf http://127.0.0.1:{a.port}/ >/dev/null && break; sleep 1; done",
                # grep reads it all: grep -q stops at the match and pipefail sees curl's SIGPIPE
                f"curl -sf http://127.0.0.1:{a.port}/ | grep -i '<html' >/dev/null || {{ cat ~/webapp.log; exit 1; }}",
                "kill $WEB"]
    return [cmd]


def script(a) -> str:
    readme = Path(a.readme).read_text()
    pick = readme.split("### Pick a branch", 1)[1].split("### Steps", 1)[0]
    if f"`{a.branch}`" not in pick:
        sys.exit(f"README's Pick a branch doesn't name {a.branch}")
    if not re.search(r"^    BRANCH=", pick, re.M):
        sys.exit("README's Pick a branch has no BRANCH= line")
    harness = shlex.quote(str(HERE))
    out = ["set -euo pipefail",
           f"export HOME={shlex.quote(str(Path(a.home).resolve()))}",
           'mkdir -p "$HOME"; cd "$HOME"',
           # the engines this test starts, stopped at the end
           'trap \'pkill -f "$HOME/src/darktable/build/bin/darktable-api" || true\' EXIT',
           f"BRANCH={shlex.quote(a.branch)}"]
    for n, blocks in quickstart_steps(readme, a.platform):
        if n >= 7:           # darktable's window
            continue
        out.append(f"echo '=== step {n} ==='")
        for b in blocks:
            for c in commands(b):
                for line in adapt(c, a, harness):
                    out += [f"echo '+ {line}'" if "'" not in line else f"echo {shlex.quote('+ ' + line)}", line]
    out.append("echo '=== quick start passed ==='")
    return "\n".join(out) + "\n"


async def _tools(server: str, env: dict, calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=server, args=[], env={**os.environ, **env})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await asyncio.wait_for(calls(s), 600)   # an engine that hangs fails the test


async def _call(s, name, **kw):
    res = await s.call_tool(name, kw)
    if res.isError:
        raise SystemExit(f"{name} failed: {res.content}")
    print(f"{name}: ok")
    return res


def seed() -> None:
    """Make ~/.config/darktable a library with one raw, as the user's
    darktable would have, by importing through the engine."""
    home = Path.home()
    pics = home / "Pictures"
    pics.mkdir(exist_ok=True)
    raw = pics / Path(RAW_URL).name
    if not raw.exists():
        urllib.request.urlretrieve(RAW_URL, raw)
    config = home / ".config/darktable"
    config.mkdir(parents=True, exist_ok=True)
    (config / "library.db").touch()   # empty: darktable creates its tables in it
    env = {"DTAPI_BIN": str(home / "src/darktable/build/bin/darktable-api"),
           "DTAPI_CONFIGDIR": str(config)}
    server = str(home / "src/darktable-api-mcp/.venv/bin/darktable-api-mcp")

    async def calls(s):
        res = await _call(s, "import_photos", paths=[str(raw)])
        print(res.structuredContent or res.content)
    asyncio.run(_tools(server, env, calls))


def smoke(server: str) -> None:
    """The MCP server, as the user's client would start it: browse, open,
    render."""
    async def calls(s):
        tools = await s.list_tools()
        print(f"{len(tools.tools)} tools")
        res = await _call(s, "list_images")
        ids = re.findall(r'"id":\s*(\d+)', str(res.structuredContent or res.content))
        if not ids:
            raise SystemExit(f"list_images: no photos in the library copy: {res.content}")
        await _call(s, "open_photo", image_id=int(ids[0]))
        res = await _call(s, "render_preview", size=400)
        if not any(getattr(c, "type", "") == "image" for c in res.content):
            raise SystemExit(f"render_preview: no image: {res.content}")
    asyncio.run(_tools(server, {}, calls))


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed()
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == "smoke":
        smoke(sys.argv[2])
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--platform", choices=PLATFORMS, required=True)
    ap.add_argument("--branch", required=True, help="the fork's branch, e.g. darktable-api")
    ap.add_argument("--home", required=True, help="a fresh folder to use as the home folder")
    ap.add_argument("--mcp-src", help="clone the MCP server from this checkout, not GitHub")
    ap.add_argument("--port", type=int, default=8020, help="the web app's port")
    ap.add_argument("--readme", default=str(README))
    ap.add_argument("--print", action="store_true", help="print the script, don't run it")
    a = ap.parse_args()
    text = script(a)
    if a.print:
        print(text)
        return 0
    return subprocess.run(["bash", "-c", text]).returncode


if __name__ == "__main__":
    sys.exit(main())
