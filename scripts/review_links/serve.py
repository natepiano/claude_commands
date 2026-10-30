#!/usr/bin/env python3
"""Serve review snapshots only on this machine's Tailscale IPv4 address."""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TypeVar, cast
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from typing import override
else:
    OverriddenMethod = TypeVar("OverriddenMethod")

    def override(method: OverriddenMethod) -> OverriddenMethod:
        """Keep override annotations usable on Python 3.11."""
        return method

PORT = 8765
RETENTION_SECONDS = 7 * 24 * 60 * 60
SWEEP_SECONDS = 60 * 60
TAILSCALE_CHECK_SECONDS = 60


class TailscaleAddressLost(RuntimeError):
    """The server's bound address is no longer this machine's Tailscale address."""


def tailscale_ip() -> str:
    result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                            text=True, check=True, timeout=10)
    return str(ipaddress.IPv4Address(result.stdout.strip()))


def sweep_snapshots(root: Path, now: float) -> int:
    """Expire flat snapshots and legacy session snapshots without following symlinks."""
    removed = 0
    if not root.exists():
        return removed
    try:
        entries = list(root.iterdir())
    except OSError as error:
        print(f"review-links sweep: {root}: {error}", file=sys.stderr)
        return removed
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            children = list(entry.iterdir())
            # New snapshots contain regular files. Check symlinks before their
            # targets; a legacy session may contain only inaccessible links.
            flat = re.fullmatch(r"[a-z0-9]{6}", entry.name) is not None and not any(
                child.is_symlink() or child.is_dir() for child in children
            )
        except OSError as error:
            print(f"review-links sweep: {entry}: {error}", file=sys.stderr)
            continue
        for folder in [entry] if flat else children:
            try:
                if folder.is_symlink() or not folder.is_dir():
                    continue
                if folder.stat().st_mtime < now - RETENTION_SECONDS:
                    shutil.rmtree(folder)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as error:
                print(f"review-links sweep: {folder}: {error}", file=sys.stderr)
    return removed


class ReviewLinksHandler(SimpleHTTPRequestHandler):
    """Serve regular files within the snapshot root, without directory indexes."""

    @override
    def send_head(self) -> BinaryIO | None:
        root = cast(ReviewLinksServer, self.server).root
        try:
            decoded = unquote(urlsplit(self.path).path)
            if ".." in decoded.split("/"):
                raise FileNotFoundError("parent traversal")
            file = (root / decoded.lstrip("/")).resolve()
            if file == root / ".index.json" or file.name.startswith(".review-link-"):
                raise FileNotFoundError("private index or unfinished write")
            if not file.is_relative_to(root) or not file.is_file():
                raise FileNotFoundError("outside root or not a file")
            stream = file.open("rb")
        except (OSError, ValueError, RuntimeError):
            self.send_error(404, "File not found")
            return None
        try:
            stat = os.fstat(stream.fileno())
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(file)))
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        except BaseException:
            stream.close()
            raise
        return stream

    @override
    def guess_type(self, path: str | os.PathLike[str]) -> str:
        if Path(path).suffix.lower() in (".html", ".htm"):
            return "text/html; charset=utf-8"
        guessed = super().guess_type(path)
        if guessed.startswith("image/"):
            return guessed
        return "text/plain; charset=utf-8"


class ReviewLinksServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], root: Path) -> None:
        self.root: Path = root.resolve()
        self.bound_address: str = address[0]
        self.next_tailscale_check: float = time.monotonic() + TAILSCALE_CHECK_SECONDS
        self.next_sweep: float = time.monotonic() + SWEEP_SECONDS
        _ = sweep_snapshots(self.root, time.time())
        super().__init__(address, ReviewLinksHandler)

    @override
    def service_actions(self) -> None:
        now = time.monotonic()
        if now >= self.next_tailscale_check:
            self.next_tailscale_check = now + TAILSCALE_CHECK_SECONDS
            try:
                address = tailscale_ip()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise TailscaleAddressLost(f"tailscale address gone (was {self.bound_address})") from error
            if address != self.bound_address:
                raise TailscaleAddressLost(f"tailscale address changed (was {self.bound_address}, now {address})")
        if time.monotonic() >= self.next_sweep:
            self.next_sweep = time.monotonic() + SWEEP_SECONDS
            try:
                _ = sweep_snapshots(self.root, time.time())
            except OSError as error:
                print(f"review-links sweep: {error}", file=sys.stderr)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--port", type=int, default=PORT, help="test port; service uses 8765")
    args = parser.parse_args(arguments)
    try:
        address = tailscale_ip()
        root = Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()
        root.mkdir(parents=True, exist_ok=True)
        with ReviewLinksServer((address, cast(int, args.port)), root) as server:
            print(f"review-links: serving {root} on {address}:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, TailscaleAddressLost) as error:
        print(f"review-links: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
