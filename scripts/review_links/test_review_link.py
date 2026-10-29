"""Exercise snapshot creation through the CLI without contacting Tailscale."""
from __future__ import annotations

import html
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import override
else:
    from typing import TypeVar

    OverriddenMethod = TypeVar("OverriddenMethod")

    def override(method: OverriddenMethod) -> OverriddenMethod:
        return method


SCRIPT = Path(__file__).with_name("review_link.py")
DNS_NAME = "review-host.tail.example.ts.net"


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
                "import json, sys",
                "if sys.argv[1:] != ['status', '--self', '--json']:",
                "    raise SystemExit('unexpected tailscale command: ' + repr(sys.argv[1:]))",
                "print(json.dumps(" + repr({"Self": {"DNSName": DNS_NAME + "."}}) + "))",
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
        self.assertEqual((address.scheme, address.hostname, address.port), ("http", DNS_NAME, 8765))
        self.assertEqual((address.query, address.fragment), ("", ""))
        destination = self.snapshots / unquote(address.path).lstrip("/")
        self.assertTrue(destination.resolve().is_relative_to(self.snapshots.resolve()))
        self.assertTrue(destination.is_file(), url)
        return destination

    def test_named_snapshot_uses_session_prefix_and_prints_a_magicdns_url(self) -> None:
        source = self.source("notes.txt", "ready for review\n")
        self.env["TZ"] = "Pacific/Honolulu"
        before = datetime.now(timezone.utc)
        result = self.run_link(str(source), "--name", "draft")
        after = datetime.now(timezone.utc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(snapshot.relative_to(self.snapshots).parts[0], "12345678")
        stamp, label = snapshot.parent.name.split("-", 1)
        published = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        self.assertLessEqual(before, published)
        self.assertLessEqual(published, after)
        self.assertRegex(label, r"^draft(?:-|$)")
        self.assertEqual(snapshot.name, source.name)
        self.assertEqual(snapshot.read_bytes(), source.read_bytes())

    def test_missing_session_id_places_the_snapshot_under_manual(self) -> None:
        _ = self.env.pop("CLAUDE_CODE_SESSION_ID")
        source = self.source("manual.txt", "manual launch")
        result = self.run_link(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.linked_snapshot(result.stdout.strip())
        self.assertEqual(snapshot.relative_to(self.snapshots).parts[0], "manual")

    def test_a_second_call_keeps_the_old_url_and_snapshot_content_unchanged(self) -> None:
        source = self.source("draft.txt", "first version")
        first = self.run_link(str(source), "--name", "draft")
        self.assertEqual(first.returncode, 0, first.stderr)
        old_snapshot = self.linked_snapshot(first.stdout.strip())
        _ = source.write_text("revised version", encoding="utf-8")
        second = self.run_link(str(source), "--name", "draft")
        self.assertEqual(second.returncode, 0, second.stderr)
        new_snapshot = self.linked_snapshot(second.stdout.strip())
        self.assertNotEqual(first.stdout.strip(), second.stdout.strip())
        self.assertNotEqual(old_snapshot.parent, new_snapshot.parent)
        self.assertEqual(old_snapshot.read_text(encoding="utf-8"), "first version")
        self.assertEqual(new_snapshot.read_text(encoding="utf-8"), "revised version")

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


if __name__ == "__main__":
    _ = unittest.main()
