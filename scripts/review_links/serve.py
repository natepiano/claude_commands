#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Serve review snapshots only on this machine's Tailscale IPv4 address."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from enum import Enum
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast, override
from urllib.parse import parse_qs, unquote, urlsplit

# Detect file changes before the first stamp, including changes during imports.
STARTED_NS = time.time_ns()

from scripts.lib import conf
from scripts.production import change_routing
from scripts.review_links import branch_page, change_page, change_browser
from scripts.review_links.review_link import private_name


class TailscaleAddressLost(RuntimeError):
    """The server's bound address is no longer this machine's Tailscale address."""


class CodeChanged(RuntimeError):
    """A loaded module changed and the server must restart."""


class ModuleStampUnavailable(Enum):
    UNREADABLE = "unreadable"


def loaded_module_stamps() -> dict[Path, tuple[int, int] | ModuleStampUnavailable]:
    checkout = Path(__file__).resolve().parents[2]
    stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(checkout):
            try:
                stat = path.stat()
                stamps[path] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamps[path] = ModuleStampUnavailable.UNREADABLE
    return stamps


def tailscale_ip() -> str:
    result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                            text=True, check=True, timeout=conf.read("review_links").number("tailscale_timeout_seconds"))
    return str(ipaddress.IPv4Address(result.stdout.strip()))


def sweep_snapshots(root: Path, now: float) -> int:
    """Expire flat snapshots and legacy session snapshots without following symlinks."""
    retention = conf.read("review_links").number("snapshot_retention_seconds")
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
                if folder.stat().st_mtime < now - retention:
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
            branches = branch_page.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This branch page is not available.</p>"))
            return None
        else:
            self.html_response(200, branch_page.render(branches))
            return None
        try:
            browser = change_browser.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links browser: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
            return None
        else:
            try:
                content = change_browser.render(browser, cast(ReviewLinksServer, self.server).delivery_status)
            except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as error:
                print(f"review-links browser: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
                return None
            self.html_response(200, content)
            return None
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
            branches = branch_page.page_at(server.root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This branch page is not available.</p>"))
            return
        else:
            self.delete_branches(branches)
            return
        try:
            browser = change_browser.page_at(server.root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links browser: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return
        else:
            self.browser_answer(browser)
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

    def delete_branches(self, page: branch_page.BranchPage) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty branch selection")
            body = self.rfile.read(length).decode("utf-8")
            results = branch_page.submit(page, body)
        except ValueError as error:
            self.html_response(400, change_page.document(f"<p>{html.escape(str(error))}</p>"))
            return
        except OSError as error:
            print(f"review-links branch delete: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>The request could not be read. No branches were deleted.</p>"))
            return
        completed = {result.name for result in results if result.message == "deleted"}
        selected = frozenset(result.name for result in results if result.message != "deleted")
        removed = {row.name for row in page.rows if row.name in completed
                   or (isinstance(row, branch_page.MergedUnownedBranch) and row.name not in selected)}
        unowned_removed = sum(isinstance(row, branch_page.MergedUnownedBranch)
                              and row.name in removed for row in page.rows)
        remaining = branch_page.BranchPage(page.repository, tuple(row for row in page.rows if row.name not in removed),
                                           page.token, page.unowned_total - unowned_removed)
        self.html_response(200, branch_page.render(remaining, results, selected=selected))

    def browser_answer(self, browser: change_browser.ReviewBrowser) -> None:
        server = cast(ReviewLinksServer, self.server)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty answer")
            body = self.rfile.read(length).decode("utf-8")
            delivery, answer = change_browser.submit(browser, body, server.delivery_status)
        except (ValueError, change_page.change_review.ChangeReviewError) as error:
            self.browser_failure(400, "Check the answer and submit it again.", error)
            return
        except change_page.FeatureLogFailed as error:
            self.browser_failure(503, "The feature log could not be updated.", error)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, change_page.Refusal) as error:
            self.browser_failure(503, "The answer could not be saved. Try again.", error)
            return
        try:
            self.json_response(200, {"answer": answer})
        finally:
            try:
                change_page.deliver(delivery, server.delivery_status)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)

    def browser_failure(self, status: int, reason: str, error: Exception) -> None:
        print(f"review-links browser answer: {error}", file=sys.stderr)
        self.json_response(status, {"reason": reason})

    def json_response(self, status: int, fields: dict[str, str]) -> None:
        content = json.dumps(fields).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        _ = self.wfile.write(content)
        self.wfile.flush()

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
        settings = conf.read("review_links")
        self.root: Path = root.resolve()
        self.bound_address: str = address[0]
        self.delivery_status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
        self.next_tailscale_check: float = time.monotonic() + settings.number("tailscale_check_seconds")
        self.next_sweep: float = time.monotonic() + settings.number("snapshot_sweep_seconds")
        self.next_code_check: float = time.monotonic() + settings.number("code_check_seconds")
        self.module_stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = loaded_module_stamps()
        _ = sweep_snapshots(self.root, time.time())
        super().__init__(address, ReviewLinksHandler)
        threading.Thread(target=change_page.recover_pending, args=(self.delivery_status,), daemon=True).start()

    @override
    def service_actions(self) -> None:
        now = time.monotonic()
        if now >= self.next_tailscale_check:
            settings = conf.read("review_links")
            self.next_tailscale_check = now + settings.number("tailscale_check_seconds")
            try:
                address = tailscale_ip()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise TailscaleAddressLost(f"tailscale address gone (was {self.bound_address})") from error
            if address != self.bound_address:
                raise TailscaleAddressLost(f"tailscale address changed (was {self.bound_address}, now {address})")
        if time.monotonic() >= self.next_sweep:
            settings = conf.read("review_links")
            self.next_sweep = time.monotonic() + settings.number("snapshot_sweep_seconds")
            try:
                _ = sweep_snapshots(self.root, time.time())
            except OSError as error:
                print(f"review-links sweep: {error}", file=sys.stderr)
        if now >= self.next_code_check:
            settings = conf.read("review_links")
            self.next_code_check = now + settings.number("code_check_seconds")
            stamps = loaded_module_stamps()
            for path, stamp in stamps.items():
                if (stamp is ModuleStampUnavailable.UNREADABLE or stamp[0] >= STARTED_NS
                        or (path in self.module_stamps and self.module_stamps[path] != stamp)):
                    with self.delivery_status.locked():
                        if self.delivery_status.request_attempts:
                            return
                    raise CodeChanged(f"code changed: {path}")
            self.module_stamps.update(stamps)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--port", type=int, default=conf.read("review_links").whole("server_port"), help="override the configured server port")
    args = parser.parse_args(arguments)
    try:
        address = tailscale_ip()
        root = change_routing.review_root()
        root.mkdir(parents=True, exist_ok=True)
        try:
            with ReviewLinksServer((address, cast(int, args.port)), root) as server:
                print(f"review-links: serving {root} on {address}:{server.server_port}", flush=True)
                server.serve_forever()
        except CodeChanged as error:
            print(f"review-links: {error}", file=sys.stderr)
            os.execv(sys.executable, sys.orig_argv)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, TailscaleAddressLost) as error:
        print(f"review-links: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
