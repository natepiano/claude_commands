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
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TypeVar, cast
from urllib.parse import parse_qs, unquote, urlsplit

if __package__:
    from . import change_page
    from .review_link import private_name
else:
    import change_page  # pyright: ignore[reportImplicitRelativeImport]
    from review_link import private_name  # pyright: ignore[reportImplicitRelativeImport]

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
            page = change_page.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links page: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
            return None
        else:
            try:
                content = change_page.render(page, cast(ReviewLinksServer, self.server).delivery_status)
            except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as error:
                print(f"review-links render: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
                return None
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return BytesIO(content)
        try:
            decoded = unquote(urlsplit(self.path).path)
            if ".." in decoded.split("/"):
                raise FileNotFoundError("parent traversal")
            if any(private_name(part) for part in decoded.split("/")):
                raise FileNotFoundError("private publisher file")
            file = (root / decoded.lstrip("/")).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                raise FileNotFoundError("outside root or not a file")
            if any(private_name(part) for part in file.relative_to(root).parts):
                raise FileNotFoundError("private publisher file")
            stream = file.open("rb")
        except (OSError, ValueError, RuntimeError):
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
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

    def html_response(self, status: int, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        _ = self.wfile.write(content)

    def failed_answer(self, page: change_page.ReviewPage, body: str, status: int, reason: str,
                      error: Exception) -> None:
        print(f"review-links answer: {error}", file=sys.stderr)
        server = cast(ReviewLinksServer, self.server)
        try:
            content = change_page.render(page, server.delivery_status, change_page.answer_notice(page, body, reason))
        except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as render_error:
            print(f"review-links error page: {render_error}", file=sys.stderr)
            content = change_page.fallback_answer(page, change_page.answer_notice(page, body, reason))
        self.html_response(status, content)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()

    def do_POST(self) -> None:
        server = cast(ReviewLinksServer, self.server)
        try:
            if server.bound_address != tailscale_ip():
                self.html_response(403, change_page.document("<p>Open this page through this machine's Tailscale address.</p>"))
                return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"review-links address: {error}", file=sys.stderr)
            self.html_response(403, change_page.document("<p>The Tailscale address is not available.</p>"))
            return
        if urlsplit(self.path).query or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.html_response(400, change_page.document("<p>Use the answer form on the review page.</p>"))
            return
        try:
            page = change_page.page_at(server.root, self.path)
        except (OSError, ValueError) as error:
            print(f"review-links page: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return
        body = ""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("Empty answer")
            body = self.rfile.read(length).decode("utf-8")
            if parse_qs(body).get("outcome") == ["next"]:
                location = change_page.publish_next(page, body)
                self.redirect(location)
                return
            delivery = change_page.submit(page, body, server.delivery_status)
        except (ValueError, change_page.change_review.ChangeReviewError) as error:
            self.failed_answer(page, body, 400, "Check the answer and submit it again.", error)
            return
        except change_page.FeatureLogFailed as error:
            self.failed_answer(page, body, 503, "The feature log could not be updated.", error)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, change_page.Refusal) as error:
            self.failed_answer(page, body, 503, "The answer could not be saved. Try again.", error)
            return
        try:
            self.redirect(urlsplit(self.path).path)
        finally:
            # The request thread sends messages after it flushes the response.
            try:
                change_page.deliver(delivery, server.delivery_status)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)

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
        self.delivery_status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
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
