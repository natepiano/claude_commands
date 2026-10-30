"""Exercise snapshot creation through the CLI without contacting Tailscale."""
from __future__ import annotations

import html
import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.review_links import review_link, serve


from typing import TYPE_CHECKING, cast


if TYPE_CHECKING:
    from typing import override
else:
    from typing import TypeVar

    OverriddenMethod = TypeVar("OverriddenMethod")

    def override(method: OverriddenMethod) -> OverriddenMethod:
        return method


SCRIPT = Path(__file__).with_name("review_link.py")
TAILSCALE_IP = "100.103.14.30"


class ReviewPage(HTMLParser):
    """Text a browser can display as the title and offline fallback."""

    def __init__(self, document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.fallback: list[str] = []
        self.in_title: bool = False
        self.in_pre: bool = False
        self.feed(document)

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag == "pre":
            self.in_pre = True

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "pre":
            self.in_pre = False

    @override
    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        if self.in_pre:
            self.fallback.append(data)


class PrivateNameTests(unittest.TestCase):
    def test_private_names_ignore_case_without_hiding_similar_public_names(self) -> None:
        for name, expected in (
            (".index.json", True),
            (".INDEX.JSON", True),
            (".Index.Json", True),
            (".index.lock", True),
            (".INDEX.LOCK", True),
            (".Index.Lock", True),
            (".review-link-", True),
            (".review-link-x", True),
            (".Review-Link-x", True),
            (".REVIEW-LINK-X", True),
            ("index.json", False),
            (".index.json.bak", False),
            (".index.locked", False),
            (".review-link", False),
            ("review-link-draft.txt", False),
            ("draft.txt", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(review_link.private_name(name), expected)


class ReviewLinkTests(unittest.TestCase):
    root: Path = Path()
    snapshots: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-link-test-")))
        home = self.root / "home"
        home.mkdir()
        self.snapshots = home / ".local/share/review-links"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        tailscale = bin_dir / "tailscale"
        _ = tailscale.write_text(
            "\n".join([
                f"#!{sys.executable}",
                "import sys",
                "if sys.argv[1:] != ['ip', '-4']:",
                "    raise SystemExit('unexpected tailscale command: ' + repr(sys.argv[1:]))",
                f"print({TAILSCALE_IP!r})",
                "",
            ]),
            encoding="utf-8",
        )
        tailscale.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(home),
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "CLAUDE_CODE_SESSION_ID": "12345678-abcd-4321-abcd-123456789abc",
        }
        _ = self.env.pop("REVIEW_LINKS_ROOT", None)
        _ = self.env.pop("REVIEW_LINKS_HOST", None)

    def source(self, name: str, contents: str) -> Path:
        path = self.root / name
        _ = path.write_text(contents, encoding="utf-8")
        return path

    def run_link(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], env=self.env,
            text=True, capture_output=True, check=False, timeout=10,
        )

    def linked_snapshot(self, url: str) -> Path:
        address = urlsplit(url)
        expected_host = self.env.get("REVIEW_LINKS_HOST", TAILSCALE_IP).rstrip(".")
        self.assertEqual((address.scheme, address.hostname, address.port), ("http", expected_host, 8765))
        self.assertEqual((address.query, address.fragment), ("", ""))
        destination = self.snapshots / unquote(address.path).lstrip("/")
        self.assertTrue(destination.resolve().is_relative_to(self.snapshots.resolve()))
        self.assertTrue(destination.is_file(), url)
        return destination

    def test_named_snapshot_uses_a_flat_id_and_tailscale_ipv4_url(self) -> None:
        source = self.source("notes.txt", "ready for review\n")
        self.env["TZ"] = "Pacific/Honolulu"
        result = self.run_link(str(source), "--name", "draft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(len(snapshot.relative_to(self.snapshots).parts), 2)
        self.assertRegex(snapshot.parent.name, r"^[a-z0-9]{6}$")
        self.assertEqual(snapshot.name, source.name)
        self.assertEqual(snapshot.read_bytes(), source.read_bytes())

    def test_missing_session_id_still_uses_a_flat_id(self) -> None:
        _ = self.env.pop("CLAUDE_CODE_SESSION_ID")
        source = self.source("manual.txt", "manual launch")
        result = self.run_link(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(len(snapshot.relative_to(self.snapshots).parts), 2)
        self.assertRegex(snapshot.parent.name, r"^[a-z0-9]{6}$")

    def test_ipv4_url_with_a_twenty_character_filename_fits_sixty_characters(self) -> None:
        source = self.source("1234567890123456.txt", "short link")
        result = self.run_link(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(snapshot.name, source.name)
        self.assertLessEqual(len(result.stdout.strip()), 60)

    def test_explicit_host_override_is_not_shortened(self) -> None:
        self.env["REVIEW_LINKS_HOST"] = "custom.tail.example.ts.net."
        self.env["PATH"] = str(self.root / "no-tailscale")
        source = self.source("notes.txt", "override")
        result = self.run_link(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.linked_snapshot(result.stdout.strip()).read_bytes(), source.read_bytes())

    def test_missing_or_invalid_tailscale_ipv4_refuses_without_creating_a_snapshot(self) -> None:
        source = self.source("notes.txt", "must not be copied")
        tailscale = self.root / "bin" / "tailscale"
        for output, exit_code in (("", 0), ("fd7a:115c:a1e0::1", 0), ("natedev", 0), ("", 1)):
            with self.subTest(output=output, exit_code=exit_code):
                _ = tailscale.write_text(
                    f"#!{sys.executable}\nprint({output!r})\nraise SystemExit({exit_code})\n", encoding="utf-8",
                )
                result = self.run_link(str(source))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertTrue(result.stderr.strip())
                self.assertFalse(self.snapshots.exists())

    def test_a_second_call_keeps_the_url_and_serves_the_edited_content(self) -> None:
        source = self.source("draft.txt", "first version")
        first = self.run_link(str(source), "--name", "draft")
        self.assertEqual(first.returncode, 0, first.stderr)
        old_snapshot = self.linked_snapshot(first.stdout.strip())
        _ = source.write_text("revised version", encoding="utf-8")
        second = self.run_link(str(source), "--name", "draft")
        self.assertEqual(second.returncode, 0, second.stderr)
        new_snapshot = self.linked_snapshot(second.stdout.strip())
        self.assertEqual(first.stdout.strip(), second.stdout.strip())
        self.assertEqual(old_snapshot.parent, new_snapshot.parent)
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "revised version")
        self.assertEqual(new_snapshot.read_text(encoding="utf-8"), "revised version")
        with serve.ReviewLinksServer(("127.0.0.1", 0), self.snapshots) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", urlsplit(first.stdout.strip()).path)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"revised version")
            finally:
                connection.close()
                server.shutdown()
                thread.join(5)

    def test_a_different_file_set_gets_a_new_id(self) -> None:
        first = self.source("first.txt", "first")
        second = self.source("second.txt", "second")
        single = self.run_link(str(first))
        bundle = self.run_link(str(first), str(second))
        self.assertEqual(single.returncode, 0, single.stderr)
        self.assertEqual(bundle.returncode, 0, bundle.stderr)
        self.assertNotEqual(self.linked_snapshot(single.stdout.strip()).parent,
                            self.linked_snapshot(bundle.stdout.splitlines()[0]).parent)

    def test_sorted_resolved_file_paths_reuse_the_id_and_remove_obsolete_outputs(self) -> None:
        first = self.source("first.txt", "first")
        second = self.source("second.md", "# second")
        initial = self.run_link(str(first), str(second))
        self.assertEqual(initial.returncode, 0, initial.stderr)
        folder = self.linked_snapshot(initial.stdout.splitlines()[0]).parent
        obsolete = folder / "obsolete.html"
        _ = obsolete.write_text("old output", encoding="utf-8")
        old = time.time() - 6 * 24 * 60 * 60
        os.utime(folder, (old, old))
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        repeated = self.run_link(str(alias / second.name), str(first))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout.splitlines(), list(reversed(initial.stdout.splitlines())))
        self.assertFalse(obsolete.exists())
        self.assertGreater(folder.stat().st_mtime, old + 24 * 60 * 60)
        self.assertEqual((folder / second.name).read_bytes(), second.read_bytes())
        self.assertEqual({item.name for item in folder.iterdir()}, {first.name, second.name, "second.html"})

    def test_an_expired_id_gets_a_new_id_and_stale_index_entries_are_dropped(self) -> None:
        expired_source = self.source("expired.txt", "expired")
        other_source = self.source("other.txt", "other")
        initial = self.run_link(str(expired_source))
        self.assertEqual(initial.returncode, 0, initial.stderr)
        expired = self.linked_snapshot(initial.stdout.strip()).parent
        old = time.time() - 8 * 24 * 60 * 60
        os.utime(expired, (old, old))
        self.assertEqual(serve.sweep_snapshots(self.snapshots, time.time()), 1)
        other = self.run_link(str(other_source))
        self.assertEqual(other.returncode, 0, other.stderr)
        index = (self.snapshots / ".index.json").read_text(encoding="utf-8")
        self.assertNotIn(str(expired_source.resolve()), index)
        self.assertNotIn(expired.name, index)
        repeated = self.run_link(str(expired_source))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertNotEqual(initial.stdout, repeated.stdout)
        self.assertEqual(self.linked_snapshot(repeated.stdout.strip()).read_bytes(), expired_source.read_bytes())

    def test_differently_named_source_alias_keeps_both_links_working(self) -> None:
        original = self.source("original.txt", "one source, two published names")
        alias = self.root / "alias.txt"
        alias.symlink_to(original)
        first = self.run_link(str(original))
        second = self.run_link(str(alias))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        original_url = first.stdout.strip()
        alias_url = second.stdout.strip()
        original_copy = self.linked_snapshot(original_url)
        alias_copy = self.linked_snapshot(alias_url)
        self.assertNotEqual(original_copy.parent, alias_copy.parent)
        self.assertEqual(original_copy.name, "original.txt")
        self.assertEqual(alias_copy.name, "alias.txt")
        with serve.ReviewLinksServer(("127.0.0.1", 0), self.snapshots) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                for url in (original_url, alias_url):
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.request("GET", urlsplit(url).path)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), original.read_bytes())
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                thread.join(5)
        repeated_original = self.run_link(str(original))
        repeated_alias = self.run_link(str(alias))
        self.assertEqual(repeated_original.returncode, 0, repeated_original.stderr)
        self.assertEqual(repeated_alias.returncode, 0, repeated_alias.stderr)
        self.assertEqual(repeated_original.stdout, first.stdout)
        self.assertEqual(repeated_alias.stdout, second.stdout)

    def test_reserved_names_refuse_the_whole_batch_before_writing(self) -> None:
        valid = self.source("valid.txt", "must not be copied")
        for name in (".index.json", ".index.lock", ".review-link-report.txt", ".review-link-draft.md",
                     ".INDEX.JSON", ".INDEX.LOCK", ".REVIEW-LINK-report.txt"):
            with self.subTest(name=name):
                reserved = self.source(name, "reserved input")
                result = self.run_link(str(valid), str(reserved))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("reserved snapshot filename", result.stderr)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.snapshots.exists())
                self.assertEqual(reserved.read_text(encoding="utf-8"), "reserved input")

    def test_new_forces_a_separate_id_and_leaves_the_old_content(self) -> None:
        source = self.source("draft.txt", "first version")
        initial = self.run_link(str(source))
        self.assertEqual(initial.returncode, 0, initial.stderr)
        old_snapshot = self.linked_snapshot(initial.stdout.strip())
        _ = source.write_text("second version", encoding="utf-8")
        separate = self.run_link(str(source), "--new")
        self.assertEqual(separate.returncode, 0, separate.stderr)
        self.assertNotEqual(initial.stdout, separate.stdout)
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "first version")
        self.assertEqual(self.linked_snapshot(separate.stdout.strip()).read_text(encoding="utf-8"), "second version")
        repeated = self.run_link(str(source))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, separate.stdout)

    def test_markdown_renders_an_escaped_page_with_the_original_text_as_fallback(self) -> None:
        text = '# Review\n\n<script>alert("draft")</script>\n</pre> & café\n'
        source = self.source("review & notes.md", text)
        result = self.run_link(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(snapshot.name, "review & notes.html")
        self.assertEqual((snapshot.parent / source.name).read_bytes(), source.read_bytes())
        document = snapshot.read_text(encoding="utf-8")
        page = ReviewPage(document)
        self.assertEqual("".join(page.title), source.name)
        self.assertEqual("".join(page.fallback), text)
        self.assertIn(html.escape(text), document)
        self.assertIn("viewport", document)
        self.assertIn("max-width", document)
        self.assertTrue("prefers-color-scheme" in document or "color-scheme: light dark" in document)

    def test_code_blocks_scroll_without_wrapping_and_raw_html_is_escaped_by_the_renderer(self) -> None:
        document = review_link.render_markdown("```\n" + "─" * 100 + "\n```\n\n<details>draft</details>", "draft.md")
        pre_style = document.split("pre {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space: pre;", pre_style)
        self.assertIn("overflow-x: auto;", pre_style)
        self.assertIn("overflow-wrap: normal;", pre_style)
        self.assertNotIn("pre-wrap", pre_style)
        self.assertIn("&lt;details&gt;draft&lt;/details&gt;", document)
        self.assertIn("marked.use({renderer: {html(text)", document)
        self.assertIn("text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')", document)

    def test_multiple_files_share_one_snapshot_and_preserve_html_image_and_text_bytes(self) -> None:
        page = self.source("page.html", "<!doctype html><title>Existing</title><p>café</p>")
        text = self.source("notes.txt", "review text — unchanged\n")
        image = self.root / "image.png"
        _ = image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        result = self.run_link(str(page), str(image), str(text), "--name", "bundle")
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = result.stdout.splitlines()
        self.assertEqual(len(urls), 3)
        copies = [self.linked_snapshot(url) for url in urls]
        self.assertEqual(len({copy.parent for copy in copies}), 1)
        for source, copy in zip((page, image, text), copies, strict=True):
            with self.subTest(source=source.name):
                self.assertEqual(copy.name, source.name)
                self.assertEqual(copy.read_bytes(), source.read_bytes())

    def test_a_directory_or_missing_file_refuses_the_whole_batch_without_copying(self) -> None:
        valid = self.source("valid.txt", "must not be copied")
        directory = self.root / "directory"
        directory.mkdir()
        for invalid in (directory, self.root / "missing.txt"):
            with self.subTest(invalid=invalid.name):
                result = self.run_link(str(valid), str(invalid))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertTrue(result.stderr.strip())
                self.assertFalse(self.snapshots.exists() and any(self.snapshots.iterdir()))

    def test_colliding_source_or_generated_filenames_refuse_without_copying(self) -> None:
        original = self.source("notes.md", "# Original Markdown\n")
        other_folder = self.root / "other"
        other_folder.mkdir()
        duplicate = other_folder / "notes.md"
        _ = duplicate.write_text("# A different document\n", encoding="utf-8")
        generated_collision = self.source("notes.html", "<title>Existing HTML</title>")
        for conflicting in (duplicate, generated_collision):
            with self.subTest(conflicting=conflicting):
                result = self.run_link(str(original), str(conflicting))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertTrue(result.stderr.strip())
                self.assertFalse(self.snapshots.exists() and any(self.snapshots.iterdir()))

    def test_casefold_collisions_refuse_both_sources_before_creating_a_snapshot(self) -> None:
        first_folder = self.root / "first"
        second_folder = self.root / "second"
        first_folder.mkdir()
        second_folder.mkdir()
        for first_name, second_name in (
            ("Report.txt", "report.txt"),
            ("Straße.txt", "STRASSE.txt"),
            ("Review.md", "review.HTML"),
        ):
            with self.subTest(first=first_name, second=second_name):
                first = first_folder / first_name
                second = second_folder / second_name
                _ = first.write_text("first report must not be replaced", encoding="utf-8")
                _ = second.write_text("different report", encoding="utf-8")
                result = self.run_link(str(first), str(second))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertIn("snapshot filename collision", result.stderr)
                self.assertIn(first.name, result.stderr)
                self.assertIn(second.name, result.stderr)
                self.assertFalse(self.snapshots.exists() and any(self.snapshots.iterdir()))


class SnapshotIdAllocationTests(unittest.TestCase):
    def test_concurrent_publishers_retain_both_index_entries_and_reuse_both_urls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-concurrent-test-") as directory:
            root = Path(directory)
            sources = [root / "first.txt", root / "second.txt"]
            for source in sources:
                _ = source.write_text(source.name, encoding="utf-8")
            snapshots = root / "snapshots"
            start = threading.Barrier(2)
            second_lock_attempt = threading.Event()
            observations = threading.Lock()
            lock_attempts = 0
            index_reads = 0
            flock = fcntl.flock
            read_index = review_link.read_snapshot_index

            def observe_lock(descriptor: int, operation: int) -> None:
                nonlocal lock_attempts
                with observations:
                    lock_attempts += 1
                    if lock_attempts == 2:
                        second_lock_attempt.set()
                flock(descriptor, operation)

            def hold_first_read(folder: Path) -> dict[str, str]:
                nonlocal index_reads
                index = read_index(folder)
                with observations:
                    index_reads += 1
                    first_read = index_reads == 1
                if first_read:
                    # Keep the first read open until the other publisher is
                    # contending for the real lock, with a finite failure guard.
                    self.assertTrue(second_lock_attempt.wait(2), "second publisher never attempted the lock")
                return index

            def publish(source: Path) -> list[str]:
                _ = start.wait(timeout=5)
                return review_link.snapshot([source])

            with patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(snapshots), "REVIEW_LINKS_HOST": "natedev"}):
                with (patch.object(fcntl, "flock", side_effect=observe_lock),
                      patch.object(review_link, "read_snapshot_index", side_effect=hold_first_read),
                      ThreadPoolExecutor(max_workers=2) as workers):
                    runs = [workers.submit(publish, source) for source in sources]
                    urls = [run.result(timeout=5) for run in runs]
                index = cast(dict[str, str], json.loads((snapshots / ".index.json").read_text(encoding="utf-8")))
                self.assertEqual(len(index), 2)
                self.assertEqual(lock_attempts, 2)
                self.assertEqual(index_reads, 2)
                for source, first_urls in zip(sources, urls, strict=True):
                    key = json.dumps([(str(source.resolve()), source.name)])
                    self.assertEqual(index[key], urlsplit(first_urls[0]).path.split("/")[1])
                    self.assertEqual(review_link.snapshot([source]), first_urls)

    def test_refresh_atomically_replaces_artifacts_and_the_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-atomic-test-") as directory:
            root = Path(directory)
            source = root / "draft.txt"
            _ = source.write_text("old", encoding="utf-8")
            snapshots = root / "snapshots"
            with patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(snapshots), "REVIEW_LINKS_HOST": "natedev"}):
                initial = review_link.snapshot([source])
                destination = snapshots / urlsplit(initial[0]).path.lstrip("/")
                _ = source.write_text("new", encoding="utf-8")
                replace = os.replace
                replaced: list[str] = []

                def observe(temporary: Path, target: Path) -> None:
                    self.assertEqual(temporary.parent, target.parent)
                    self.assertTrue(target.is_file())
                    if target == destination:
                        self.assertEqual(target.read_text(encoding="utf-8"), "old")
                        self.assertEqual(temporary.read_text(encoding="utf-8"), "new")
                    else:
                        self.assertEqual(target.name, ".index.json")
                        self.assertIsInstance(cast(object, json.loads(temporary.read_text(encoding="utf-8"))), dict)
                    replaced.append(target.name)
                    replace(temporary, target)

                with patch.object(os, "replace", side_effect=observe):
                    self.assertEqual(review_link.snapshot([source]), initial)
                self.assertEqual(replaced, [source.name, ".index.json"])
                self.assertEqual(destination.read_text(encoding="utf-8"), "new")
                self.assertEqual(list(snapshots.rglob(".review-link-*")), [])

    def test_failed_refresh_keeps_the_previous_file_and_cleans_the_temporary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-atomic-test-") as directory:
            root = Path(directory)
            source = root / "draft.txt"
            _ = source.write_text("old", encoding="utf-8")
            snapshots = root / "snapshots"
            with patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(snapshots), "REVIEW_LINKS_HOST": "natedev"}):
                initial = review_link.snapshot([source])
                index = (snapshots / ".index.json").read_bytes()
                _ = source.write_text("new", encoding="utf-8")
                with patch.object(os, "replace", side_effect=OSError("rename failed")):
                    with self.assertRaisesRegex(OSError, "rename failed"):
                        _ = review_link.snapshot([source])
                destination = snapshots / urlsplit(initial[0]).path.lstrip("/")
                self.assertEqual(destination.read_text(encoding="utf-8"), "old")
                self.assertEqual((snapshots / ".index.json").read_bytes(), index)
                self.assertEqual(list(snapshots.rglob(".review-link-*")), [])

    def test_taken_id_retries_without_overwriting_the_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-id-test-") as directory:
            root = Path(directory)
            source = root / "source.txt"
            _ = source.write_text("new content", encoding="utf-8")
            snapshots = root / "snapshots"
            old = snapshots / "taken1"
            old.mkdir(parents=True)
            _ = (old / source.name).write_text("old content", encoding="utf-8")
            with (patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(snapshots), "REVIEW_LINKS_HOST": "natedev"}),
                  patch.object(secrets, "choice", side_effect=list("taken1fresh2"))):
                urls = review_link.snapshot([source])
            self.assertEqual(urls, ["http://natedev:8765/fresh2/source.txt"])
            self.assertEqual((old / source.name).read_text(encoding="utf-8"), "old content")
            self.assertEqual((snapshots / "fresh2" / source.name).read_bytes(), source.read_bytes())

    def test_ten_taken_ids_fail_without_changing_the_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-id-test-") as directory:
            root = Path(directory)
            source = root / "source.txt"
            _ = source.write_text("new content", encoding="utf-8")
            snapshots = root / "snapshots"
            old = snapshots / "aaaaaa"
            old.mkdir(parents=True)
            _ = (old / source.name).write_text("old content", encoding="utf-8")
            with (patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(snapshots), "REVIEW_LINKS_HOST": "natedev"}),
                  patch.object(secrets, "choice", return_value="a") as choose):
                with self.assertRaisesRegex(FileExistsError, "after 10 tries"):
                    _ = review_link.snapshot([source])
                self.assertEqual(choose.call_count, 60)
            self.assertEqual(set(snapshots.iterdir()), {old, snapshots / ".index.lock"})
            self.assertEqual((old / source.name).read_text(encoding="utf-8"), "old content")


if __name__ == "__main__":
    _ = unittest.main()
