#!/usr/bin/env python3
"""Record a short MP4 of one X11 window selected by title."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import NamedTuple


MAX_SECONDS = 60
MAX_BYTES = 500_000_000
DIRECTORY_LIMIT = 2_147_483_648
MAX_AGE_SECONDS = 604_800
WINDOW_IDS: re.Pattern[str] = re.compile(r"0x[0-9a-fA-F]+")
WINDOW_NAME = re.compile(r'WM_NAME\((?:STRING|UTF8_STRING)\)\s*=\s*"((?:\\.|[^"\\])*)"')
CLIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ScreenRecordArguments(argparse.Namespace):
    """Requested window, duration, and clip label."""

    window: str | None = None
    seconds: str | None = None
    name: str = "clip"


class FoundWindow(NamedTuple):
    """The X11 window selected for recording and its displayed title."""

    window_id: str
    title: str


class WindowLookupFailure(Exception):
    """A window lookup that cannot select one window."""


def arguments() -> ScreenRecordArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--window", default=None)
    _ = parser.add_argument("--seconds", default=None)
    _ = parser.add_argument("--name", default="clip")
    return parser.parse_args(namespace=ScreenRecordArguments())


def error(message: str, code: int) -> int:
    print(f"screen_record: {message}", file=sys.stderr)
    return code


def prune(directory: Path) -> None:
    """Remove expired clips, then oldest clips until stored bytes fall below 2 GiB."""
    now = time.time()
    candidates: list[tuple[Path, float, int]] = []
    for path in directory.glob("*.mp4"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            details = path.stat()
        except OSError as exc:
            print(f"screen_record: cannot inspect {path}: {exc}", file=sys.stderr)
            continue
        candidates.append((path, details.st_mtime, details.st_size))

    deleted_bytes = 0
    expired_count = 0
    size_count = 0
    remaining: list[tuple[Path, float, int]] = []

    def remove(path: Path, size: int) -> bool:
        nonlocal deleted_bytes
        try:
            path.unlink()
        except OSError as exc:
            print(f"screen_record: cannot delete {path}: {exc}", file=sys.stderr)
            return False
        deleted_bytes += size
        return True

    for path, modified, size in candidates:
        if now - modified > MAX_AGE_SECONDS and remove(path, size):
            expired_count += 1
        else:
            remaining.append((path, modified, size))

    remaining.sort(key=lambda clip: (clip[1], clip[0].name))
    total = sum(size for _, _, size in remaining)
    for path, _, size in remaining:
        if total < DIRECTORY_LIMIT:
            break
        if remove(path, size):
            size_count += 1
            total -= size

    if expired_count or size_count:
        print(
            f"screen_record: pruned {expired_count + size_count} clips "
            + f"({round(deleted_bytes / 1_000_000)} MB): "
            + f"{expired_count} older than 7 days, "
            + f"{size_count} to bring the directory under 2 GiB",
            file=sys.stderr,
        )


def window_title(window_id: str, deadline: float) -> str | None:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["xprop", "-id", window_id, "WM_NAME"],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(0.1, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    match = WINDOW_NAME.search(result.stdout)
    if match is None:
        return None
    return re.sub(r'\\([\\"])', r"\1", match.group(1))


def find_window(substring: str) -> FoundWindow:
    deadline = time.monotonic() + 10
    seen: list[str] = []

    def no_match() -> WindowLookupFailure:
        titles = ", ".join(repr(title) for title in seen) if seen else "no windows"
        return WindowLookupFailure(
            f"screen_record: no window title contains {substring!r} after 10 s; titles seen: {titles}"
        )

    while True:
        if time.monotonic() >= deadline:
            raise no_match()
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["xprop", "-root", "_NET_CLIENT_LIST"],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as exc:
            raise no_match() from exc
        if result.returncode != 0:
            if result.stderr:
                raise WindowLookupFailure(result.stderr.rstrip("\n"))
            raise WindowLookupFailure("screen_record: xprop -root failed")

        ids: list[str] = WINDOW_IDS.findall(result.stdout.split("#", 1)[1]) if "#" in result.stdout else []
        matches: list[FoundWindow] = []
        for window_id in ids:
            if time.monotonic() >= deadline:
                raise no_match()
            title = window_title(window_id, deadline)
            if title is None:
                continue
            if title not in seen:
                seen.append(title)
            if substring in title:
                matches.append(FoundWindow(window_id, title))

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            titles = ", ".join(repr(window.title) for window in matches)
            raise WindowLookupFailure(
                f"screen_record: {len(matches)} windows match {substring!r}: "
                + f"{titles}; pass a longer substring"
            )
        if time.monotonic() >= deadline:
            raise no_match()
        time.sleep(min(0.025, max(0, deadline - time.monotonic())))


def clip_path(directory: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{label}-{stamp}"
    path = directory / f"{base}.mp4"
    suffix = 2
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            path = directory / f"{base}-{suffix}.mp4"
            suffix += 1
            continue
        os.close(descriptor)
        return path


def record(window_id: str, title: str, seconds: int, display: str, path: Path) -> int:
    duration = seconds + 15
    command = [
        "timeout", "--kill-after=5", str(duration),
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "x11grab", "-framerate", "60", "-window_id", str(int(window_id, 16)),
        "-i", display, "-t", str(seconds), "-fs", str(MAX_BYTES),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", str(path),
    ]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=False)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    if result.returncode in (124, 137, -9):
        path.unlink(missing_ok=True)
        return error(
            f"stopped by the outer timeout at {duration} s ({seconds} s + 15 s): "
            + "ffmpeg did not stop at -t; partial clip deleted",
            3,
        )
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        return error(f"ffmpeg exited {result.returncode}; partial clip deleted", 1)
    try:
        size = path.stat().st_size
    except OSError as exc:
        path.unlink(missing_ok=True)
        return error(f"ffmpeg produced no readable clip: {exc}", 1)
    if size >= MAX_BYTES:
        print(path)
        return error(
            f"stopped at the 500 MB file cap (ffmpeg -fs); the clip is shorter than {seconds} s",
            3,
        )
    print(path)
    print(f"screen_record: recorded {seconds} s of '{title}' ({round(size / 1_000)} KB)", file=sys.stderr)
    return 0


def main() -> int:
    args = arguments()
    if sys.platform != "linux":
        return error(f"refused: Linux only; this machine is {sys.platform}", 2)
    if args.seconds is None:
        return error("refused: --seconds is required (1 to 60 s; there is no default)", 2)
    if re.fullmatch(r"[0-9]+", args.seconds) is None:
        return error(
            f"refused: --seconds must be a whole number from 1 to 60, got {args.seconds!r}",
            2,
        )
    numeric_seconds = args.seconds.lstrip("0") or "0"
    if numeric_seconds == "0":
        return error(
            f"refused: --seconds must be a whole number from 1 to 60, got {args.seconds!r}",
            2,
        )
    if len(numeric_seconds) > 2 or int(numeric_seconds) > MAX_SECONDS:
        return error(
            f"refused: --seconds {numeric_seconds} is over the 60 s ceiling, which has no override; "
            + "record at most 60 s per clip",
            2,
        )
    seconds = int(numeric_seconds)
    if not args.window:
        return error("refused: --window needs a non-empty window title substring", 2)
    if CLIP_NAME.fullmatch(args.name) is None:
        return error(
            f"refused: --name must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}, got {args.name!r}",
            2,
        )

    missing = [tool for tool in ("xprop", "timeout", "ffmpeg") if shutil.which(tool) is None]
    if missing:
        return error(f"missing on PATH: {', '.join(missing)}", 1)
    display = os.environ.get("DISPLAY")
    if not display:
        return error("DISPLAY is not set: no X server (Xwayland) to record from", 1)

    directory = Path(os.environ.get("SCREEN_RECORD_DIR") or Path.home() / ".cache" / "screen-record").expanduser().resolve()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        prune(directory)
        found = find_window(args.window)
        return record(found.window_id, found.title, seconds, display, clip_path(directory, args.name))
    except WindowLookupFailure as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        return error(f"could not record: {exc}", 1)


if __name__ == "__main__":
    sys.exit(main())
