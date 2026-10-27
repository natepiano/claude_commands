#!/usr/bin/env python3
"""Find loose folders in ~/rust, /tmp, /tmp/claude and $TMPDIR once a day, and tell their makers.

The scratch guard refuses most loose folders before they exist and the hourly tmp sweep removes
idle ones under /tmp; this audit catches the rest, ~/rust above all, where nothing is swept. A
folder is loose when it is none of these:

  * in ~/rust: a name in config/rust_roots.txt (one per line, `#` starts a comment), or the
    visible scratch root (~/rust/repro);
  * in /tmp, /tmp/claude and $TMPDIR: a name the system or a tool owns (KEPT_NAMES,
    KEPT_PREFIXES), a session scratchpad (/tmp/claude-<uid>), a run folder root, a scratch root,
    or a Rust tempfile folder, which the sweep judges.

Only folders this user owns that did not change for MIN_AGE_SECONDS are named. The maker is the
earliest-started session whose transcript (~/.claude*/projects/*/<session>.jsonl) holds a command
that names the folder; a listing in a command's output does not count.
`folder_audit.py [--send]` prints one line per loose folder; with --send, each maker session also
gets one message that lists its folders and names scratch.py.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tmp_sweep import TEMPFILE_NAME

RUST_ROOTS_FILE = Path(__file__).resolve().parents[2] / "config" / "rust_roots.txt"
MIN_AGE_SECONDS = 24 * 3600
KEPT_NAMES = frozenset({"claude", "unit", "delegate", "scratch", ".X11-unix", ".ICE-unix", ".XIM-unix",
                        ".font-unix", ".Test-unix", "repro"})
KEPT_PREFIXES = ("systemd-private-", "tmux-", "nix-", "ssh-", "claude-", "com.apple.", "powerlog")
RG_FILES_PER_CALL = 500
# A session counts as a maker only where one of its own commands names the folder. ripgrep returns
# each whole JSON "command" string that names a loose folder (the path, then a boundary or the
# string's end), and every folder in it is read from there. A listing in a tool result names every
# folder it shows, so it does not count.
COMMAND_START = r'"command":"(?:[^"\\]|\\.)*?'
COMMAND_REST = r'(?:(?:[^A-Za-z0-9_.\-"\\]|\\.)(?:[^"\\]|\\.)*)?"'
TRANSCRIPT_TIME = re.compile(rb'"timestamp":"([^"]+)"')
START_LINES = 20
START_BYTES = 1 << 20
SEND = Path(__file__).resolve().parents[1] / "message" / "send.py"


@dataclass(frozen=True)
class Loose:
    path: Path
    changed: float
    maker: str


def expected_rust_names(path: Path = RUST_ROOTS_FILE) -> frozenset[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(name for line in lines if (name := line.split("#", 1)[0].strip()))


def kept_in_temp(name: str) -> bool:
    return name in KEPT_NAMES or name.startswith(KEPT_PREFIXES) or TEMPFILE_NAME.fullmatch(name) is not None


def candidates(rust: Path, temps: list[Path], expected: frozenset[str]) -> list[Path]:
    """Every loose folder this user owns, before the age rule."""
    def kept_in_rust(name: str) -> bool:
        return name in expected or name in KEPT_NAMES

    roots: list[tuple[Path, Callable[[str], bool]]] = [(rust, kept_in_rust), *((temp, kept_in_temp) for temp in temps)]
    found: list[Path] = []
    for root, keep in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir() or entry.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            if not keep(entry.name):
                found.append(entry)
    return found


def transcripts() -> list[Path]:
    """Every session transcript once; the account homes link the same projects folder."""
    folders = dict.fromkeys(folder.resolve() for folder in sorted(Path.home().glob(".claude*/projects")))
    return [path for folder in folders for path in folder.glob("*/*.jsonl")]


def makers(paths: list[Path], files: list[Path]) -> dict[Path, str]:
    """The earliest-started session whose own commands name each path, since a folder's maker named it
    before any later session that only talked about it; a path no command names is left out.
    One ripgrep pass reads every transcript: a Python scan of 22 GB took 4 h 24 min (2026-10-26). A
    name must end at a path boundary, so ~/rust/hana is not named by ~/rust/hana_catalyst."""
    if not paths or not files:
        return {}
    names = {str(path) for path in paths}
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = (COMMAND_START + "(?:" + alternatives + ")" + COMMAND_REST + "\n").encode()
    in_command = re.compile(f"(?:{alternatives})(?![A-Za-z0-9_.-])")
    earliest: dict[str, tuple[float, str]] = {}
    started: dict[str, float] = {}
    for start in range(0, len(files), RG_FILES_PER_CALL):
        chunk = [str(file) for file in files[start:start + RG_FILES_PER_CALL]]
        result = subprocess.run(["rg", "--only-matching", "--with-filename", "--null", "--no-line-number",
                                 "--no-messages", "--no-config", "--file", "-", "--", *chunk],
                                input=pattern, capture_output=True, check=False)
        for line in result.stdout.decode(errors="replace").splitlines():
            file, _, command = line.partition("\0")
            for name in {found.group(0) for found in in_command.finditer(command)}:
                if file not in started:
                    started[file] = _started(Path(file))
                if started[file] < earliest.get(name, (float("inf"), ""))[0]:
                    earliest[name] = (started[file], Path(file).stem)
    return {path: earliest[str(path)][1] for path in paths if str(path) in earliest}


def _started(path: Path) -> float:
    """The first timestamp in a transcript's opening lines, else its file time."""
    try:
        with path.open("rb") as handle:
            for _ in range(START_LINES):
                found = TRANSCRIPT_TIME.search(handle.readline(START_BYTES))
                if found:
                    return datetime.fromisoformat(found.group(1).decode().replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        pass
    return _mtime(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def audit(rust: Path, temps: list[Path], expected: frozenset[str], now: float,
          files: list[Path] | None = None) -> list[Loose]:
    changed = {entry: _mtime(entry) for entry in candidates(rust, temps, expected)}
    found = [entry for entry, when in changed.items() if when and now - when >= MIN_AGE_SECONDS]
    sessions = transcripts() if files is None and found else (files or [])
    named = makers(found, sessions)
    return [Loose(entry, changed[entry], named.get(entry, "")) for entry in found]


def message(folders: list[Loose]) -> str:
    shown = ", ".join(str(folder.path) for folder in folders)
    return (f"folder audit: you made loose folder(s) nothing removes: {shown}. Move each into a task's scratch "
            + "(`~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py adopt <path> --task <task>`) or remove "
            + "it when nothing in it is needed. Make new scratch only with `scratch.py new`.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="folder_audit.py", description="Find loose folders and tell their makers.")
    _ = parser.add_argument("--send", action="store_true")
    send = bool(parser.parse_args(argv).send)  # pyright: ignore[reportAny]
    temps = list(dict.fromkeys(Path(os.path.realpath(path)) for path in
                               ("/tmp", "/tmp/claude", os.environ.get("TMPDIR", "")) if path))
    loose = audit(Path(os.path.realpath(Path.home() / "rust")), temps, expected_rust_names(), time.time())
    by_maker: dict[str, list[Loose]] = {}
    for folder in loose:
        age = (time.time() - folder.changed) / 86400
        print(f"loose: {folder.path} ({age:.1f} d idle) maker {folder.maker or 'unknown'}")
        by_maker.setdefault(folder.maker, []).append(folder)
    print(f"folder audit: {len(loose)} loose folder(s)")
    if send:
        for maker, folders in by_maker.items():
            if maker:
                _ = subprocess.run([sys.executable, str(SEND), "--to", f"session:{maker}", "--from", "folder-audit",
                                    "--text", message(folders)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
