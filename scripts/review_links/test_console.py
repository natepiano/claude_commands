"""Test the console with private configuration and pages."""
from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlsplit

from scripts.build_queue import memory_feed
from scripts.review_links import change_page, console


class ConsoleMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[str] = []
        self.sources: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identity := values.get("data-page"):
            self.pages.append(identity)
        if tag in ("script", "link", "img", "iframe"):
            self.sources.extend(value for key in ("src", "href") if (value := values.get(key)))


class ConsoleTests(unittest.TestCase):
    root: Path = Path()
    configuration: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="console-test-")))
        self.configuration = self.root / "config"
        self.configuration.mkdir()
        for stem in ("console", "review_links", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{stem}.conf",
                                self.configuration / f"{stem}.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.configuration), "REVIEW_LINKS_ROOT": str(self.root / "snapshots"),
            "REVIEW_LINKS_HOST": "127.0.0.1", "STAFF_FILE": str(self.root / "staff.json")})))

    def test_publication_reuses_the_changes_link_and_inlines_only_local_icons(self) -> None:
        with patch.object(memory_feed, "page_url", return_value="/builds/memory_live.html", create=True):
            url = console.publish()
        parsed = urlsplit(url)
        self.assertEqual(parsed.query, "page=changes")
        folder = self.root / "snapshots" / parsed.path.split("/")[1]
        self.assertTrue((folder / change_page.BROWSER_MANIFEST).is_file())
        self.assertTrue((folder / change_page.PAGE_NAME).is_file())
        markup = ConsoleMarkup()
        source = (folder / console.PAGE_NAME).read_text()
        markup.feed(source)
        self.assertEqual(markup.pages, ["changes", "builds"])
        self.assertEqual(markup.sources, [])
        self.assertEqual(source.count('aria-hidden="true" focusable="false"'), 5)
        self.assertIn("/builds/memory_live.html", source)
        self.assertNotIn("Workspace", source)
        self.assertNotIn("sidebar-footer", source)
        self.assertEqual(source.count("<h1>Hana console</h1>"), 2)
        self.assertIn("<title>Hana console</title>", source)
        self.assertIn("document.title = 'Hana console · ' + title", source)
        self.assertIn('class="mobile-header"', source)
        self.assertIn('id="mobile-nav-toggle"', source)
        self.assertIn('aria-label="Open navigation"', source)
        self.assertIn('class="lucide lucide-menu"', source)
        self.assertIn('.sidebar .brand,.collapsed .sidebar,.page-header{display:none}', source)
        self.assertIn('.workspace{flex:1;width:100%}', source)
        self.assertIn("mobileToggle.setAttribute('aria-expanded', String(!value))", source)

    def test_builds_url_resolves_from_its_public_function(self) -> None:
        folder = self.root / "snapshots/builds"
        folder.mkdir(parents=True)
        page = folder / "memory_live.html"
        _ = page.write_text("<!doctype html><title>Builds</title><h1>Builds</h1>")
        _ = (self.configuration / "memory_feed.conf").write_text(f"output_folder={folder}\n")
        pages = console.read_pages()
        self.assertEqual(pages[1].destination, console.AvailablePage("/builds/memory_live.html"))
        self.assertIn('data-url="/builds/memory_live.html" data-title="Builds"', console.render(pages).decode())
        self.assertEqual(page.read_text(), "<!doctype html><title>Builds</title><h1>Builds</h1>")

    def test_config_order_and_unavailable_pages_remain_visible(self) -> None:
        source = (self.configuration / "console.conf").read_text()
        blocks = source.split("\n\n")
        _ = (self.configuration / "console.conf").write_text("\n\n".join(reversed(blocks)))
        with patch.object(memory_feed, "page_url", side_effect=FileNotFoundError("fixture page absent"), create=True):
            pages = console.read_pages()
        self.assertEqual([page.identity for page in pages], ["builds", "changes"])
        self.assertIsInstance(pages[0].destination, console.UnavailablePage)
        self.assertIsInstance(pages[1].destination, console.AvailablePage)
        markup = ConsoleMarkup()
        source = console.render(pages).decode()
        markup.feed(source)
        self.assertEqual(markup.pages, ["builds", "changes"])
        self.assertIn('aria-label="Builds — Not available"', source)
        self.assertNotIn("fixture page absent", source)

    def test_a_missing_url_function_keeps_the_entry_unavailable(self) -> None:
        configuration = self.configuration / "console.conf"
        source = configuration.read_text().replace(
            "python:scripts.build_queue.memory_feed:page_url",
            "python:scripts.build_queue.memory_feed:fixture_page_url_absent")
        _ = configuration.write_text(source)
        pages = console.read_pages()
        self.assertEqual([page.identity for page in pages], ["changes", "builds"])
        self.assertIsInstance(pages[0].destination, console.AvailablePage)
        self.assertIsInstance(pages[1].destination, console.UnavailablePage)
        self.assertIn('aria-label="Builds — Not available"', console.render(pages).decode())

    def test_url_functions_cannot_put_an_external_page_in_the_frame(self) -> None:
        for path in ("https://example.invalid/page", "//example.invalid/page", "/../outside", "/%2e%2e/outside",
                     "http://127.0.0.1:1/page", "https://127.0.0.1:8765/page"):
            with self.subTest(path=path), patch.object(memory_feed, "page_url", return_value=path, create=True):
                pages = console.read_pages()
                self.assertIsInstance(pages[1].destination, console.UnavailablePage)


if __name__ == "__main__":
    _ = unittest.main()
