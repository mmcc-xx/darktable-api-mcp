"""
make_library_copy.py — copy a darktable library for darktable-api to use.

Copies library.db and data.db (SQLite's online backup: consistent even while
darktable is running) and darktablerc from a darktable config dir into
<dest>/config, with write_sidecar_files=never so nothing done through the
copy reaches the XMP files next to your photos. Photos stay where they are
and are only read.

    python make_library_copy.py                      # ~/.config/darktable -> ./library-copy
    python make_library_copy.py --source DIR --dest DIR [--force]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

OVERRIDES = {"write_sidecar_files": "never"}


def _backup(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _darktablerc(src: Path, dst: Path) -> None:
    lines = src.read_text().splitlines() if src.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in OVERRIDES:
            lines[i] = f"{key}={OVERRIDES[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in OVERRIDES.items() if k not in seen]
    dst.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--source", type=Path, default=Path.home() / ".config" / "darktable",
                    help="darktable config dir to copy from (default: ~/.config/darktable)")
    ap.add_argument("--dest", type=Path, default=Path("library-copy"),
                    help="where to put the copy (default: ./library-copy)")
    ap.add_argument("--force", action="store_true", help="replace an existing copy")
    a = ap.parse_args()

    src, config = a.source.expanduser(), a.dest.expanduser() / "config"
    if not (src / "library.db").exists():
        print(f"no library.db in {src}", file=sys.stderr)
        return 1
    if config.exists():
        if not a.force:
            print(f"{config} exists; pass --force to replace it", file=sys.stderr)
            return 1
        shutil.rmtree(config)
    config.mkdir(parents=True)
    (a.dest / "cache").mkdir(exist_ok=True)
    for name in ("library.db", "data.db"):
        if (src / name).exists():
            _backup(src / name, config / name)
    _darktablerc(src / "darktablerc", config / "darktablerc")

    with sqlite3.connect(f"file:{config / 'library.db'}?mode=ro", uri=True) as con:
        images = con.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    print(f"copied {images} photos' library to {config.resolve()}")
    print(f"set DTAPI_CONFIGDIR={config.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
