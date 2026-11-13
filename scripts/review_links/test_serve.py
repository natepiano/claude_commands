"""HTTP behavior on localhost and retention in disposable snapshot folders."""
from __future__ import annotations

from scripts.lib.checkout_test_support import python_tool_source

import importlib.util
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from unittest.mock import patch


from typing import cast, override


from scripts.review_links import change_page, serve
from scripts.lib import atomic_file, conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@dataclass(frozen=True)
class HttpReply:
    status: int
    content_type: str
    body: bytes


def setUpModule() -> None:
    configuration = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    for stem in ("review_links", "change_review"):
        _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{stem}.conf", configuration / f"{stem}.conf")
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))


class ReviewLinksServerTests(unittest.TestCase):
    root: Path = Path()
    outside: Path = Path()
    port: int = 0

    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-server-test-")))
        self.root = temporary / "root"
        self.root.mkdir()
        self.outside = temporary / "outside.txt"
        _ = self.outside.write_text("outside the served root", encoding="utf-8")
        server = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
        self.port = server.server_port
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

    def request(self, path: str) -> HttpReply:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return HttpReply(response.status, headers.get("content-type", ""), response.read())
        finally:
            connection.close()

    def test_files_are_served_with_the_required_utf8_content_types(self) -> None:
        text = "review café — ready\n"
        for filename, content_type in (
            ("notes.md", "text/plain; charset=utf-8"),
            ("notes.txt", "text/plain; charset=utf-8"),
            ("notes.html", "text/html; charset=utf-8"),
        ):
            with self.subTest(filename=filename):
                _ = (self.root / filename).write_text(text, encoding="utf-8")
                reply = self.request("/" + filename)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.content_type, content_type)
                self.assertEqual(reply.body.decode("utf-8"), text)

    def test_other_text_files_are_readable_as_utf8(self) -> None:
        text = "a report with no standard text extension — café\n"
        _ = (self.root / "report.log").write_text(text, encoding="utf-8")
        reply = self.request("/report.log")
        self.assertEqual(reply.status, 200)
        self.assertTrue(reply.content_type.startswith("text/"), reply.content_type)
        self.assertIn("charset=utf-8", reply.content_type)
        self.assertEqual(reply.body.decode("utf-8"), text)

    def test_undeletable_expired_snapshot_does_not_prevent_bind_or_other_links(self) -> None:
        expired = self.root / "aaaaaa"
        expired.mkdir()
        os.utime(expired, (0, 0))
        other = self.root / "bbbbbb"
        other.mkdir()
        _ = (other / "draft.txt").write_text("still served", encoding="utf-8")
        errors = StringIO()
        with patch.object(shutil, "rmtree", side_effect=PermissionError("cannot delete snapshot")), redirect_stderr(errors):
            with serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server:
                # handle_request serves one disposable connection without a background service.
                thread = threading.Thread(target=server.handle_request)
                thread.start()
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("GET", "/bbbbbb/draft.txt")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"still served")
                finally:
                    connection.close()
                    thread.join(TEST_HANG_GUARD_SECS)
        self.assertTrue(expired.exists())
        self.assertIn("cannot delete snapshot", errors.getvalue())
        self.assertIn("aaaaaa", errors.getvalue())

    def test_images_keep_their_image_content_type_and_binary_bytes(self) -> None:
        image = b"\x89PNG\r\n\x1a\n\x00\xff"
        _ = (self.root / "diagram.png").write_bytes(image)
        reply = self.request("/diagram.png")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.content_type, "image/png")
        self.assertEqual(reply.body, image)

    def test_inaccessible_sweep_entries_do_not_prevent_bind_or_other_links(self) -> None:
        session = self.root / "manual"
        session.mkdir()
        target = self.root.parent / "unsearchable" / "snapshot"
        target.mkdir(parents=True)
        symlink = session / "link"
        symlink.symlink_to(target, target_is_directory=True)
        unreadable = session / "unreadable"
        other = self.root / "bbbbbb"
        other.mkdir()
        _ = (other / "draft.txt").write_text("still served", encoding="utf-8")
        is_dir = Path.is_dir

        for inaccessible in (symlink, unreadable):
            with self.subTest(entry=inaccessible.name):
                if inaccessible == unreadable:
                    unreadable.mkdir()

                def checked_is_dir(path: Path) -> bool:
                    if path == inaccessible:
                        raise PermissionError("cannot inspect entry")
                    return is_dir(path)

                errors = StringIO()
                with patch.object(Path, "is_dir", new=checked_is_dir), redirect_stderr(errors):
                    with serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server:
                        self.port = server.server_port
                        thread = threading.Thread(target=server.handle_request)
                        thread.start()
                        try:
                            reply = self.request("/bbbbbb/draft.txt")
                            self.assertEqual(reply.status, 200)
                            self.assertEqual(reply.body, b"still served")
                        finally:
                            thread.join(TEST_HANG_GUARD_SECS)
                self.assertTrue(symlink.is_symlink())
                self.assertTrue(target.is_dir())
                if inaccessible == unreadable:
                    self.assertIn("cannot inspect entry", errors.getvalue())

    def test_directories_return_404_even_when_an_index_page_exists(self) -> None:
        snapshot = self.root / "session" / "snapshot"
        snapshot.mkdir(parents=True)
        _ = (snapshot / "index.html").write_text("<title>Not a directory endpoint</title>", encoding="utf-8")
        for path in ("/", "/session", "/session/", "/session/snapshot", "/session/snapshot/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).status, 404)
        self.assertEqual(self.request("/session/snapshot/index.html").status, 200)

    def test_plain_and_encoded_traversal_cannot_read_a_file_outside_the_root(self) -> None:
        for path in ("/../outside.txt", "/%2e%2e/outside.txt", "/%2e%2e%2foutside.txt"):
            with self.subTest(path=path):
                reply = self.request(path)
                self.assertEqual(reply.status, 404)
                self.assertNotIn(self.outside.read_bytes(), reply.body)

    def test_a_symlink_to_an_outside_file_or_directory_returns_404(self) -> None:
        (self.root / "shortcut.txt").symlink_to(self.outside)
        (self.root / "shortcut").symlink_to(self.outside.parent, target_is_directory=True)
        for path in ("/shortcut.txt", "/shortcut/outside.txt"):
            with self.subTest(path=path):
                reply = self.request(path)
                self.assertEqual(reply.status, 404)
                self.assertNotIn(self.outside.read_bytes(), reply.body)

    def test_url_encoded_filename_with_spaces_and_unicode_is_served(self) -> None:
        payload = b"unchanged snapshot bytes"
        _ = (self.root / "draft café.txt").write_bytes(payload)
        reply = self.request("/draft%20caf%C3%A9.txt")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, payload)

    def test_flat_and_legacy_snapshot_urls_both_keep_serving_their_files(self) -> None:
        for folder in ("k3x9qa", "da928300/20261029T151117462923Z-review-46806d56e97e"):
            with self.subTest(folder=folder):
                snapshot = self.root / folder
                snapshot.mkdir(parents=True)
                _ = (snapshot / "draft.html").write_bytes(b"<title>Review snapshot</title>")
                reply = self.request(f"/{folder}/draft.html")
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.body, b"<title>Review snapshot</title>")

    def test_an_atomic_index_temporary_is_not_served_before_publication(self) -> None:
        private = b"private index source paths and page tokens"
        publish = os.replace
        requested: list[str] = []

        def replace_file(source: Path, destination: Path) -> None:
            requested.append(source.name)
            self.assertEqual(source.read_bytes(), private)
            reply = self.request("/" + source.name)
            self.assertEqual(reply.status, 404)
            self.assertNotIn(private, reply.body)
            publish(source, destination)

        with patch.object(os, "replace", side_effect=replace_file):
            atomic_file.write_bytes_atomic(self.root / ".index.json", private, mode=0o600)
        self.assertEqual(len(requested), 1)
        self.assertEqual((self.root / ".index.json").read_bytes(), private)

    def test_index_and_unfinished_writes_are_not_served(self) -> None:
        for filename in (".index.json", ".review-link-unfinished"):
            _ = (self.root / filename).write_text("private source paths or incomplete bytes", encoding="utf-8")
            self.assertEqual(self.request("/" + filename).status, 404)
            self.assertEqual(self.request("/./" + filename).status, 404)
        (self.root / "index-alias.txt").symlink_to(self.root / ".index.json")
        self.assertEqual(self.request("/index-alias.txt").status, 404)

    def test_private_files_in_any_letter_case_return_404(self) -> None:
        private = "private source paths or incomplete bytes"
        for folder in (self.root, self.root / "session"):
            folder.mkdir(exist_ok=True)
            for filename in (
                ".index.json", ".INDEX.JSON", ".Index.Json",
                ".index.lock", ".INDEX.LOCK", ".Index.Lock",
                ".review-link-x", ".REVIEW-LINK-X", ".Review-Link-x",
                ".review-link-delivery-user-answer.json",
            ):
                with self.subTest(folder=folder.name, filename=filename):
                    _ = (folder / filename).write_text(private, encoding="utf-8")
                    path = (folder / filename).relative_to(self.root).as_posix()
                    for request_path in ("/" + path, "/" + path.replace(".", "%2e", 1)):
                        reply = self.request(request_path)
                        self.assertEqual(reply.status, 404)
                        self.assertNotIn(private.encode("utf-8"), reply.body)
        snapshot = self.root / "abc123"
        snapshot.mkdir()
        _ = (snapshot / "draft.txt").write_bytes(b"review content")
        reply = self.request("/abc123/draft.txt")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, b"review content")

    def test_private_path_parts_are_blocked_before_symlink_resolution(self) -> None:
        snapshot = self.root / "abc123"
        snapshot.mkdir()
        _ = (snapshot / "draft.txt").write_bytes(b"public snapshot through a private alias")
        for name in (".INDEX.JSON", ".Index.Lock", ".Review-Link-x"):
            (self.root / name).symlink_to(snapshot, target_is_directory=True)
            for prefix in (name, "%2e" + name[1:]):
                with self.subTest(prefix=prefix):
                    self.assertEqual(self.request(f"/{prefix}/draft.txt").status, 404)
        self.assertEqual(self.request("/abc123/draft.txt").status, 200)


class SnapshotSweepTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-sweep-test-")))

    def snapshot(self, session: str, name: str, modified: float) -> Path:
        directory = self.root / session / name
        directory.mkdir(parents=True)
        artifact = directory / "review.html"
        _ = artifact.write_text("<title>Snapshot</title>", encoding="utf-8")
        os.utime(artifact, (modified, modified))
        os.utime(directory, (modified, modified))
        return directory

    def test_sweep_removes_eight_day_snapshots_and_keeps_six_day_snapshots_across_sessions(self) -> None:
        now = 1_800_000_000.0
        six_days_ago = now - 6 * 24 * 60 * 60
        eight_days_ago = now - 8 * 24 * 60 * 60
        kept = self.snapshot("session-a", "recent-review", six_days_ago)
        removed = self.snapshot("session-a", "expired-review", eight_days_ago)
        other_removed = self.snapshot("session-b", "expired-review", eight_days_ago)
        _ = serve.sweep_snapshots(self.root, now)
        self.assertTrue((kept / "review.html").is_file())
        self.assertFalse(removed.exists())
        self.assertFalse(other_removed.exists())

    def test_sweep_tolerates_a_root_that_has_not_been_created(self) -> None:
        missing = self.root / "not-created-yet"
        _ = serve.sweep_snapshots(missing, 1_800_000_000.0)
        self.assertFalse(missing.exists())

    def test_sweep_expires_flat_ids_and_legacy_snapshots_in_the_same_root(self) -> None:
        now = 1_800_000_000.0
        recent = now - 6 * 24 * 60 * 60
        expired = now - 8 * 24 * 60 * 60
        flat_kept = self.snapshot("", "abc123", recent)
        flat_removed = self.snapshot("", "def456", expired)
        legacy_kept = self.snapshot("da928300", "20261029T151117462923Z-review-46806d56e97e", recent)
        legacy_removed = self.snapshot("da928300", "20261020T151117462923Z-review-46806d56e97e", expired)
        manual_kept = self.snapshot("manual", "20261029T151117462923Z-review-46806d56e97e", recent)
        manual_removed = self.snapshot("manual", "20261020T151117462923Z-review-46806d56e97e", expired)
        os.utime(self.root / "manual", (expired, expired))
        self.assertEqual(serve.sweep_snapshots(self.root, now), 3)
        for kept in (flat_kept, legacy_kept, manual_kept):
            self.assertTrue((kept / "review.html").is_file())
        for removed in (flat_removed, legacy_removed, manual_removed):
            self.assertFalse(removed.exists())

    def test_flat_snapshot_id_manual_expires_when_it_contains_files(self) -> None:
        now = 1_800_000_000.0
        folder = self.snapshot("", "manual", now - 8 * 24 * 60 * 60)
        self.assertEqual(serve.sweep_snapshots(self.root, now), 1)
        self.assertFalse(folder.exists())

    def test_expired_manual_session_with_only_a_symlink_keeps_link_and_target(self) -> None:
        now = 1_800_000_000.0
        expired = now - 8 * 24 * 60 * 60
        target = self.root / "outside" / "snapshot"
        target.mkdir(parents=True)
        artifact = target / "keep.txt"
        _ = artifact.write_text("outside the snapshot root", encoding="utf-8")
        os.utime(target, (expired, expired))
        root = self.root / "served"
        manual = root / "manual"
        manual.mkdir(parents=True)
        link = manual / "old-snapshot"
        link.symlink_to(target, target_is_directory=True)
        os.utime(manual, (expired, expired))
        self.assertEqual(serve.sweep_snapshots(root, now), 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "outside the snapshot root")

    def test_sweep_ignores_index_and_temporary_files(self) -> None:
        now = 1_800_000_000.0
        expired = now - 8 * 24 * 60 * 60
        for name in (".index.json", ".review-link-unfinished"):
            file = self.root / name
            _ = file.write_text("keep", encoding="utf-8")
            os.utime(file, (expired, expired))
        self.assertEqual(serve.sweep_snapshots(self.root, now), 0)
        self.assertEqual({file.name for file in self.root.iterdir()}, {".index.json", ".review-link-unfinished"})

    def test_sweep_skips_flat_and_legacy_symlinks_and_non_directories(self) -> None:
        now = 1_800_000_000.0
        outside = self.root / "outside"
        outside.mkdir()
        kept_file = outside / "keep.txt"
        _ = kept_file.write_text("outside snapshot storage", encoding="utf-8")
        os.utime(outside, (now - 8 * 24 * 60 * 60, now - 8 * 24 * 60 * 60))
        served = self.root / "served"
        served.mkdir()
        (served / "abc123").symlink_to(outside, target_is_directory=True)
        legacy = served / "da928300"
        legacy.mkdir()
        (legacy / "old-snapshot").symlink_to(outside, target_is_directory=True)
        plain = served / "def456"
        _ = plain.write_text("not a directory", encoding="utf-8")
        _ = serve.sweep_snapshots(served, now)
        self.assertTrue(kept_file.is_file())
        self.assertTrue((served / "abc123").is_symlink())
        self.assertTrue((legacy / "old-snapshot").is_symlink())
        self.assertTrue(plain.is_file())

    def test_server_sweeps_on_startup_and_again_after_an_hour(self) -> None:
        now = 1_800_000_000.0
        expired_at_start = self.snapshot("session", "expired-at-start", now - 8 * 24 * 60 * 60)
        flat_at_start = self.snapshot("", "abc123", now - 8 * 24 * 60 * 60)
        with (patch.object(time, "time", return_value=now),
              patch.object(time, "monotonic", return_value=100.0) as elapsed,
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server):
            self.assertFalse(expired_at_start.exists())
            self.assertFalse(flat_at_start.exists())
            expires_later = self.snapshot("session", "expires-later", now - 8 * 24 * 60 * 60)
            flat_later = self.snapshot("", "def456", now - 8 * 24 * 60 * 60)
            elapsed.return_value = 100.0 + 3599.0
            server.service_actions()
            self.assertTrue(expires_later.exists())
            self.assertTrue(flat_later.exists())
            elapsed.return_value = 100.0 + 3600.0
            server.service_actions()
            self.assertFalse(expires_later.exists())
            self.assertFalse(flat_later.exists())

    def test_sweep_does_not_follow_a_session_symlink_outside_the_root(self) -> None:
        outside = self.root / "outside"
        root = self.root / "served"
        root.mkdir()
        expired = outside / "expired-review"
        expired.mkdir(parents=True)
        _ = (expired / "keep.txt").write_text("not a snapshot owned by the service", encoding="utf-8")
        old = 1_800_000_000.0 - 8 * 24 * 60 * 60
        os.utime(expired, (old, old))
        (root / "session-symlink").symlink_to(outside, target_is_directory=True)
        _ = serve.sweep_snapshots(root, 1_800_000_000.0)
        self.assertTrue((expired / "keep.txt").is_file())


class ServingObservationComplete(Exception):
    """The healthy server continued through two scheduled address checks."""


class ServerAddressHealthTests(unittest.TestCase):
    elapsed: float = 100.0
    address_checks: list[float] = []
    next_address: Callable[[], str] = staticmethod(lambda: "127.0.0.1")
    stderr: StringIO = StringIO()

    @override
    def setUp(self) -> None:
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="review-address-test-"))
        self.elapsed = 100.0
        self.address_checks = []
        self.next_address = lambda: "127.0.0.1"
        self.stderr = self.enterContext(redirect_stderr(StringIO()))
        _ = self.enterContext(redirect_stdout(StringIO()))
        _ = cast(dict[str, str], self.enterContext(patch.dict(os.environ, {"REVIEW_LINKS_ROOT": root})))
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        _ = self.enterContext(patch.object(serve, "tailscale_ip", side_effect=self.read_address))
        observations = self

        class ClockDrivenServer(serve.ReviewLinksServer):
            @override
            def serve_forever(self, poll_interval: float = 0.5) -> None:
                super().serve_forever(poll_interval=0)

            @override
            def service_actions(self) -> None:
                observations.elapsed += 1
                if observations.elapsed > 221:
                    raise ServingObservationComplete()
                super().service_actions()

        _ = self.enterContext(patch.object(serve, "ReviewLinksServer", ClockDrivenServer))

    def read_address(self) -> str:
        self.address_checks.append(self.elapsed)
        if len(self.address_checks) == 1:
            return "127.0.0.1"
        return self.next_address()

    def test_tailscale_failure_after_startup_leaves_the_serve_loop_and_main_returns_one(self) -> None:
        def unavailable() -> str:
            raise subprocess.CalledProcessError(1, ["tailscale", "ip", "-4"])

        self.next_address = unavailable
        self.assertEqual(serve.main(["--port", "0"]), 1)
        self.assertEqual(self.address_checks, [100.0, 160.0])
        diagnostic = self.stderr.getvalue().splitlines()
        self.assertEqual(len(diagnostic), 1)
        self.assertIn("tailscale", diagnostic[0].lower())
        self.assertIn("gone", diagnostic[0].lower())
        self.assertIn("127.0.0.1", diagnostic[0])

    def test_changed_tailscale_address_leaves_the_serve_loop_and_main_returns_one(self) -> None:
        self.next_address = lambda: "127.0.0.2"
        self.assertEqual(serve.main(["--port", "0"]), 1)
        self.assertEqual(self.address_checks, [100.0, 160.0])
        diagnostic = self.stderr.getvalue().splitlines()
        self.assertEqual(len(diagnostic), 1)
        self.assertIn("tailscale", diagnostic[0].lower())
        self.assertIn("127.0.0.1", diagnostic[0])
        self.assertTrue("changed" in diagnostic[0].lower() or "gone" in diagnostic[0].lower())

    def test_unchanged_tailscale_address_keeps_serving_and_checks_again_after_sixty_seconds(self) -> None:
        with self.assertRaises(ServingObservationComplete):
            _ = serve.main(["--port", "0"])
        self.assertEqual(self.address_checks, [100.0, 160.0, 220.0])
        self.assertEqual(self.stderr.getvalue(), "")


class ServerCodeHealthTests(unittest.TestCase):
    checkout: Path = Path()
    elapsed: float = 100.0
    module_file: Path = Path()

    @override
    def setUp(self) -> None:
        self.checkout = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-code-test-")))
        server_file = self.checkout / "scripts/review_links/serve.py"
        server_file.parent.mkdir(parents=True)
        _ = server_file.write_text("# The server uses this temporary checkout.\n")
        os.utime(server_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        _ = self.enterContext(patch.object(serve, "__file__", str(server_file)))
        self.elapsed = 100.0
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        self.module_file = self.checkout / "review_code_fixture.py"
        _ = self.module_file.write_text("VALUE = 'before'\n")
        os.utime(self.module_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.load_module(self.module_file)

    def start_server(self) -> serve.ReviewLinksServer:
        return self.enterContext(serve.ReviewLinksServer(("127.0.0.1", 0), self.checkout / "snapshots"))

    def load_module(self, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _ = cast(object, self.enterContext(patch.dict(sys.modules, {path.stem: module})))
        spec.loader.exec_module(module)

    def test_unchanged_code_keeps_serving_and_reads_settings_only_when_the_check_is_due(self) -> None:
        server = self.start_server()
        with patch.object(conf, "read", wraps=conf.read) as read:
            self.elapsed = 104.0
            server.service_actions()
            server.service_actions()
            read.assert_not_called()
            self.elapsed = 105.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            self.elapsed = 109.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            self.elapsed = 110.0
            server.service_actions()
            self.assertEqual(read.call_count, 2)

    def test_a_deleted_loaded_module_requires_a_restart(self) -> None:
        server = self.start_server()
        self.module_file.unlink()
        self.elapsed = 105.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
        self.assertIn(str(self.module_file), str(changed.exception))

    def test_a_module_loaded_after_start_joins_the_checks_without_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'first'\n")
        os.utime(late_module, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        server.service_actions()
        _ = late_module.write_text("VALUE = 'changed later'\n")
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
        self.assertIn(str(late_module), str(changed.exception))

    def test_a_newly_seen_module_modified_after_process_start_requires_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'promoted before its first check'\n")
        os.utime(late_module, ns=(serve.STARTED_NS + 1_000_000_000, serve.STARTED_NS + 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
        self.assertIn(str(late_module), str(changed.exception))

    def test_an_active_verdict_defers_restart_and_keeps_the_recorded_stamps(self) -> None:
        server = self.start_server()
        stamps = dict(server.module_stamps)
        page = change_page.ReviewPage(self.checkout / "changes.md", self.checkout / "production.md", ("C1",), "token")
        status = server.delivery_status
        attempt = status.begin(page, "C1", "verdict")
        _ = self.module_file.write_text("VALUE = 'changed during the verdict'\n")
        os.utime(self.module_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.elapsed = 105.0
        server.service_actions()
        self.assertEqual(server.module_stamps, stamps)
        status.record(change_page.MessageDelivery(page, "C1", (), "", attempt))
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
        self.assertIn(str(self.module_file), str(changed.exception))


class ServerCodeRestartTests(unittest.TestCase):
    def test_a_changed_module_restarts_the_same_process_and_keeps_serving_http(self) -> None:
        checkout = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-restart-test-")))
        server_file = checkout / "scripts/review_links/serve.py"
        server_file.parent.mkdir(parents=True)
        _ = server_file.write_text("# The server uses this temporary checkout.\n")
        module_file = checkout / "review_restart_fixture.py"
        _ = module_file.write_text("VALUE = 'before'\n")
        settings = (Path(__file__).resolve().parents[2] / "config/review_links.conf").read_text()
        _ = (checkout / "review_links.conf").write_text(settings.replace("code_check_seconds=5", "code_check_seconds=0.05"))
        root = checkout / "snapshots"
        root.mkdir()
        _ = (root / "answer.txt").write_text("The server answers after the restart.\n")
        driver = checkout / "driver.py"
        _ = driver.write_text(python_tool_source('''import importlib.util
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from scripts.review_links import serve

checkout = Path(__file__).parent
serve.__file__ = str(checkout / "scripts/review_links/serve.py")
serve.tailscale_ip = lambda: "127.0.0.1"
spec = importlib.util.spec_from_file_location("review_restart_fixture", checkout / "review_restart_fixture.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class ObservedServer(serve.ReviewLinksServer):
    def __init__(self, address, root):
        super().__init__(address, root)
        os.environ["REVIEW_TEST_PORT"] = str(self.server_port)
        line = f"{os.getpid()} {module.VALUE} {self.server_port}"
        with (checkout / "starts.txt").open("a") as output:
            output.write(line + "\\n")
        print(line, file=sys.__stdout__, flush=True)

serve.ReviewLinksServer = ObservedServer
with redirect_stdout(sys.stderr):
    raise SystemExit(serve.main(["--port", os.environ.get("REVIEW_TEST_PORT", "0")]))
'''))
        errors_path = checkout / "errors.txt"
        errors = self.enterContext(errors_path.open("w"))
        child: subprocess.Popen[str] = subprocess.Popen(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), "-P", "-B", str(driver)],
            cwd=checkout, env={**os.environ, "CONF_DIR": str(checkout), "REVIEW_LINKS_ROOT": str(root),
                               "CLAUDE_PYTHON": sys.executable},
            stdout=subprocess.PIPE, stderr=errors, text=True,
        )

        def stop_child() -> None:
            child.terminate()
            try:
                _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                child.kill()
                _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(stop_child)

        def read_start() -> tuple[int, str, int]:
            assert child.stdout is not None
            ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
            self.assertEqual(ready, [child.stdout], errors_path.read_text())
            line = cast(str, child.stdout.readline())
            self.assertTrue(line, errors_path.read_text())
            pid, value, port = line.split()
            return int(pid), value, int(port)

        first = read_start()
        self.assertEqual(first[0], child.pid)
        self.assertEqual(first[1], "before")
        _ = module_file.write_text("VALUE = 'after_code_change'\n")
        second = read_start()
        self.assertEqual(second, (first[0], "after_code_change", first[2]))
        self.assertEqual((checkout / "starts.txt").read_text().splitlines(),
                         [f"{pid} {value} {port}" for pid, value, port in (first, second)])
        connection = HTTPConnection("127.0.0.1", second[2], timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", "/answer.txt")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode(), (root / "answer.txt").read_text())
        finally:
            connection.close()
        self.assertIn(f"review-links: code changed: {module_file}", errors_path.read_text())


class ServerStartupTests(unittest.TestCase):
    def test_server_exits_nonzero_when_tailscale_ipv4_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-startup-test-") as directory:
            root = Path(directory)
            tailscale = root / "tailscale"
            _ = tailscale.write_text("#!/bin/sh\necho 'Tailscale is down' >&2\nexit 1\n", encoding="utf-8")
            tailscale.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).with_name("serve.py")), "--port", "0"],
                env={**os.environ, "PATH": str(root), "CLAUDE_PYTHON": sys.executable,
                     "REVIEW_LINKS_ROOT": str(root / "snapshots")},
                text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tailscale", result.stderr.lower())


class ReviewLinksConfigTests(unittest.TestCase):
    def test_service_polls_read_settings_only_when_a_check_is_due(self) -> None:
        server = serve.ReviewLinksServer.__new__(serve.ReviewLinksServer)
        server.next_tailscale_check = 200.0
        server.next_sweep = 300.0
        server.next_code_check = 400.0
        server.bound_address = "127.0.0.1"
        server.root = Path("unused")
        with (patch.object(time, "monotonic", return_value=100.0) as elapsed,
              patch.object(conf, "read", wraps=conf.read) as read,
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1") as address,
              patch.object(serve, "sweep_snapshots", return_value=0) as sweep):
            server.service_actions()
            server.service_actions()
            read.assert_not_called()
            address.assert_not_called()
            sweep.assert_not_called()
            elapsed.return_value = 200.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            address.assert_called_once_with()
            sweep.assert_not_called()
            self.assertEqual(server.next_tailscale_check, 260.0)
            server.next_tailscale_check = 1000.0
            elapsed.return_value = 300.0
            server.service_actions()
            self.assertEqual(read.call_count, 2)
            sweep.assert_called_once()
            self.assertEqual(server.next_sweep, 3900.0)

    def test_snapshot_sweeps_read_the_retention_setting_at_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "review_links.conf"
            snapshot = root / "abc123"
            snapshot.mkdir()
            _ = (snapshot / "draft.txt").write_text("keep me")
            os.utime(snapshot, (10, 10))
            with patch.dict(os.environ, {"CONF_DIR": str(root)}):
                _ = settings.write_text("# range: 0..\nsnapshot_retention_seconds=100\n")
                self.assertEqual(serve.sweep_snapshots(root, 50), 0)
                self.assertTrue(snapshot.is_dir())
                _ = settings.write_text("# range: 0..\nsnapshot_retention_seconds=1\n")
                self.assertEqual(serve.sweep_snapshots(root, 50), 1)
                self.assertFalse(snapshot.exists())

    def test_config_keeps_the_existing_values(self) -> None:
        from scripts.lib import conf

        settings = conf.read(Path(__file__).resolve().parents[2] / "config/review_links.conf")
        for key, expected in {'unrouted_alert_recipient': 'natedev'}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.text(key), expected)
        for key, expected in {'delivery_tries': 3, 'server_port': 8765, 'snapshot_allocation_tries': 10}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.whole(key), expected)
        for key, expected in {'delivery_pause_seconds': 5.0, 'unrouted_alert_retry_seconds': 60.0, 'tailscale_timeout_seconds': 10.0, 'snapshot_retention_seconds': 604800.0, 'snapshot_sweep_seconds': 3600.0, 'tailscale_check_seconds': 60.0}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.number(key), expected)

if __name__ == "__main__":
    _ = unittest.main()
