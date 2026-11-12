"""Test page publication, HTTP forms, verdict calls and checkpoint routing."""
from __future__ import annotations

import fcntl
import os
import re
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, replace
from datetime import date
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from io import StringIO
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Literal, cast, override
from collections.abc import Callable, Generator
from unittest.mock import patch
from urllib.parse import urlencode, urljoin, urlsplit

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import atomic_file, conf, urgency
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.production import change_routing, change_review, footer_counts, next_item, showrunners, staff, unrouted_alert, update_registration
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, production_staff_record
from scripts.feature_log import feature_log
from scripts.review_links import change_browser, change_page, serve
from scripts.rebuild import need
from scripts.agents.remote import local_machine
from scripts.production.production_doc import read_production


@dataclass(frozen=True)
class Reply:
    status: int
    body: str
    location: str


class SavedAnswerForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.validation_requests: dict[str, dict[str, str]] = {}
        self.feedback: dict[str, str] = {}
        self.selected_outcomes: dict[str, str] = {}
        self.textarea_row: str = ""
        self.textarea_text: str = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.fields = {}
        attributes = dict(attrs)
        name, value = attributes.get("name"), attributes.get("value")
        if tag in ("input", "button") and name is not None and value is not None:
            self.fields[name] = value
        if tag == "textarea" and name == "feedback":
            self.textarea_row = self.fields["row"]
            self.textarea_text = ""
        if tag == "button" and attributes.get("aria-pressed") == "true" and value is not None:
            self.selected_outcomes[self.fields["row"]] = value
        if tag == "button" and value == "validate":
            self.validation_requests[self.fields["row"]] = dict(self.fields)

    @override
    def handle_data(self, data: str) -> None:
        if self.textarea_row:
            self.textarea_text += data

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self.textarea_row:
            # HTML removes the first newline after a textarea's opening tag.
            self.feedback[self.textarea_row] = self.textarea_text.removeprefix("\n")
            self.textarea_row = ""


class BrowserForms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, str]] = {}
        self.tabs: dict[str, str] = {}
        self.save_urls: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.question_counts: dict[str, int] = {}
        self.count_kind: str = "changes"
        self.pending: dict[str, bool] = {}
        self.outcomes: dict[str, list[str]] = {}
        self.current: str = ""
        self.count_tab: str = ""
        self.read_count: bool = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section" and values.get("data-key"):
            self.current = values["data-key"] or ""
            self.rows[self.current] = {}
            self.tabs[self.current] = values.get("data-tab") or ""
            self.pending[self.current] = values.get("data-pending") == "true"
            self.outcomes[self.current] = []
        if tag in ("input", "textarea") and self.current and values.get("name"):
            self.rows[self.current][values["name"] or ""] = values.get("value") or ""
        if tag == "form" and self.current and values.get("method") == "post":
            self.save_urls[self.current] = values.get("action") or ""
        if tag == "button" and self.current and values.get("name") == "outcome":
            self.outcomes[self.current].append(values.get("value") or "")
        if tag == "button" and values.get("data-showrunner"):
            self.count_tab = values["data-showrunner"] or ""
            self.count_kind = values.get("data-type") or "changes"
        if tag == "span" and "data-count" in values:
            self.read_count = True

    @override
    def handle_data(self, data: str) -> None:
        if self.read_count:
            counts = self.question_counts if self.count_kind == "questions" else self.counts
            counts[self.count_tab] = int(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "section":
            self.current = ""
        if tag == "span":
            self.read_count = False


SEND_MESSAGE = change_page.send_message


@cache
def repository_seed() -> Path:
    """Copy private Git files from one merged fixture repository."""
    temporary = tempfile.TemporaryDirectory(prefix="change-page-seed-")
    unittest.addModuleCleanup(repository_seed.cache_clear)
    unittest.addModuleCleanup(temporary.cleanup)
    repo = Path(temporary.name)
    item = change_review.Visible("Numbered moves", "Run moves list", "Each group starts at 1")
    commands = [
        ["init", "-b", "main"],
        ["config", "user.name", "Review test"],
        ["config", "user.email", "review@example.invalid"],
        ["commit", "--allow-empty", "-m", "initial"],
        ["checkout", "-b", "feature"],
        ["commit", "--allow-empty", "-m",
         f"checkpoint(feature): numbered moves\n\nChange-Review: {item.change} | {item.check} | {item.see}"],
        ["checkout", "main"],
        ["merge", "--no-ff", "feature", "-m", "merge numbered moves"],
    ]
    for arguments in commands:
        _ = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True)
    return repo


class ReviewTextTests(unittest.TestCase):
    def test_button_borders_keep_three_to_one_contrast_in_both_schemes(self) -> None:
        body = change_page.document("").decode()
        colors = re.search(r'background:light-dark\((#[0-9a-f]{6}),(#[0-9a-f]{6})\);color:CanvasText;'
                           + r'border:1px solid light-dark\((#[0-9a-f]{6}),(#[0-9a-f]{6})\)', body)
        assert colors is not None

        def luminance(color: str) -> float:
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                      for channel in channels]
            return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for fill, border, background in ((colors[1], colors[3], "#ffffff"), (colors[2], colors[4], "#1c1b22")):
            for adjacent in (fill, background):
                with self.subTest(fill=fill, border=border, adjacent=adjacent):
                    high, low = sorted((luminance(border), luminance(adjacent)), reverse=True)
                    self.assertGreaterEqual((high + 0.05) / (low + 0.05), 3)

    def test_text_without_list_lines_keeps_all_whitespace_in_one_block(self) -> None:
        text = '\n First <line> \r\n\r Second & line\u2028Third\n'
        for block in ("p", "pre"):
            with self.subTest(block=block):
                self.assertEqual(change_page.review_text_html(text, block),
                    f'<{block}>\n First &lt;line&gt; \r\n\r Second &amp; line\u2028Third\n</{block}>')

    def test_question_and_answer_show_list_runs_and_text_blocks(self) -> None:
        question = change_page.AnsweredQuestion("q1", "\n  Steps\n continue\n\n  - First\n- Second\n1. Third\n2. Fourth\n\n Done\n", "- Yes\n- No")
        self.assertEqual(change_page.question_html(question),
            '<h2>Question</h2><p>Steps\ncontinue</p><ul><li>First</li><li>Second</li></ul>'
            + '<ol><li>Third</li><li>Fourth</li></ol><p>Done</p><h2>Answer</h2><ul><li>Yes</li><li>No</li></ul>')

    def test_review_text_escapes_text_blocks_and_each_list_item(self) -> None:
        text = '<script>& "text"\n\n- <a href="x"> & **bold**\n1. `code` | <img>'
        for block in ("p", "pre"):
            with self.subTest(block=block):
                self.assertEqual(change_page.review_text_html(text, block),
                    f'<{block}>&lt;script&gt;&amp; &quot;text&quot;</{block}>'
                    + '<ul><li>&lt;a href=&quot;x&quot;&gt; &amp; **bold**</li></ul>'
                    + '<ol><li>`code` | &lt;img&gt;</li></ol>')

    def test_each_numbered_run_starts_at_its_first_number(self) -> None:
        self.assertEqual(change_page.review_text_html('3. Third\n4. Fourth\n\n1. First\n- Bullet\n8. Eighth'),
            '<ol start="3"><li>Third</li><li>Fourth</li></ol><ol><li>First</li></ol>'
            + '<ul><li>Bullet</li></ul><ol start="8"><li>Eighth</li></ol>')


class ChangePageTests(unittest.TestCase):
    temporary: Path = Path()
    root: Path = Path()
    repo: Path = Path()
    production: Path = Path()
    review_list: Path = Path()
    page_path: str = ""
    page: change_page.ReviewPage = change_page.ReviewPage(Path(), Path(), (), "")
    port: int = 0
    unit: staff.StaffRow
    raw_messages: list[tuple[str, str, str]]
    messages: list[tuple[str, str, str]] = []
    delivery_done: threading.Event = threading.Event()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.raw_messages = []
        self.unit = staff.StaffRow(staff.StaffId(""), "unit", "", staff.StaffRoot(), current_session=staff.StaffId(""), unit_type=staff.UnitDetails("direct"))

    def command(self, module: ModuleType, main: Callable[[list[str]], int],
                arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"),
                                   str(module.__file__), *arguments],
                                  capture_output=True, text=True, check=False)
        return invoke_cli(module, lambda: main(arguments), arguments, dict(os.environ), Path.cwd())

    def test_both_pages_show_lists_in_the_question_answer_and_check(self) -> None:
        item = change_review.Visible('Numbered moves', 'Steps\n- Click <one>\n- Click two\n\nFinish', 'Result')
        _ = change_review.edit_row(self.review_list, 'C1', item, date(2026, 11, 5), self.production)
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, 'C1', 'Which steps?\n1. First\n2. Second')
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, 'C1', question.question_id, 'Use these:\n- One\n- Two'))
        for content in (change_page.render(self.page, status).decode(),
                        change_browser.render(change_page.ReviewBrowser('token'), status).decode()):
            with self.subTest(page=content[:80]):
                self.assertIn('<pre>Steps</pre><ul><li>Click &lt;one&gt;</li><li>Click two</li></ul>'
                              + '<pre>Finish. You see: Result</pre>', content)
                self.assertIn('<h2>Question</h2><p>Which steps?</p><ol><li>First</li><li>Second</li></ol>', content)
                self.assertIn('<h2>Answer</h2><p>Use these:</p><ul><li>One</li><li>Two</li></ul>', content)

    def test_answer_question_keeps_internal_line_breaks_and_an_identical_rerun_changes_nothing(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, 'C1', 'Which steps?')
        assert isinstance(question, change_page.QuestionDelivery)
        answer = '  \n1. First\n\n2.  Second | <result>\n  '
        arguments = ['answer-question', '--production', 'page-owner', '--list', str(self.review_list),
                     '--row', 'C1', '--question', question.question_id, '--text', answer]
        self.assertEqual(change_page.main(arguments), 0)
        saved = status.slots(self.page, 'C1').question
        self.assertEqual(saved, change_page.AnsweredQuestion(question.question_id, question.text, answer.strip()))
        record = status.path(self.page, 'C1')
        before, modified = record.read_bytes(), record.stat().st_mtime_ns
        self.assertEqual(change_page.main(arguments), 0)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), (before, modified))
        self.assertIn('<h2>Answer</h2><ol><li>First</li></ol><ol start="2"><li> Second | &lt;result&gt;</li></ol>',
                      change_page.render(self.page, status).decode())

    def test_a_row_without_a_production_document_shows_proof_lists(self) -> None:
        row = change_page.answer_row(self.page, 'C1') | {change_review.CHECK: 'Read\n- First\n- Second'}
        change = change_browser.WaitingChange('orphan/C1', 'orphan',
            change_routing.UnavailableProduction(self.review_list), row)
        self.assertIn('<h2>Proof</h2><pre>Read</pre><ul><li>First</li><li>Second</li></ul>',
                      change_browser.section(change, 5))

    def test_publications_keep_the_html_and_manifest_under_one_configured_root(self) -> None:
        from scripts.review_links import review_link

        settings = Path(os.environ["CONF_DIR"]) / "change_review.conf"
        original = settings.read_text()
        real_write = atomic_file.write_bytes_atomic
        publications: list[tuple[Callable[[], str], str]] = [
            (change_page.publish_browser, change_page.BROWSER_MANIFEST),
            (lambda: change_page.publish(self.production, self.review_list, ["C1"]), change_page.MANIFEST),
        ]
        for index, (publish, manifest) in enumerate(publications):
            first = self.temporary / f"first-{index}"
            second = self.temporary / f"second-{index}"
            configured = original.replace("review_links_directory=~/.local/share/review-links",
                                          f"review_links_directory={first}")
            _ = settings.write_text(configured)

            def write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
                real_write(path, content, mode=mode)
                if path.name == change_page.PAGE_NAME:
                    _ = settings.write_text(configured.replace(str(first), str(second)))

            with (self.subTest(manifest=manifest), patch.dict(os.environ),
                  patch.object(atomic_file, "write_bytes_atomic", side_effect=write)):
                _ = os.environ.pop("REVIEW_LINKS_ROOT", None)
                url = publish()
                folder = first / urlsplit(url).path.split("/")[1]
                self.assertTrue((folder / change_page.PAGE_NAME).is_file())
                self.assertTrue((folder / manifest).is_file())
                self.assertFalse(second.exists())
                self.assertEqual(review_link.review_root(), second)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_an_open_owner_cell_uses_the_current_staff_production_without_a_list_write(self) -> None:
        doc = self.temporary / "destination.md"
        _ = doc.write_text("# Destination\n")
        owner = fixture_member("showrunner", name="destination", doc=staff.LiveDoc(local_machine(), doc))
        before = self.review_list.read_bytes()
        row = change_review.pending(self.review_list)[0]
        _ = staff.move(self.unit.id, staff.ReportsTo(owner.id))
        self.assertEqual(change_page.row_production(self.page, row), doc)
        self.assertEqual(self.review_list.read_bytes(), before)

    @override
    def setUp(self) -> None:
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="change-page-test-")))
        cache_home = self.temporary / "cache"
        seed_interpreter_cache(cache_home)
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache_home), "CLAUDE_PYTHON": ""}))
        self.root = self.temporary / "snapshots"
        configuration = self.temporary / "config"
        configuration.mkdir()
        for stem in ("change_review", "nix_env"):
            source = Path(__file__).resolve().parents[2] / f"config/{stem}.conf"
            _ = (configuration / f"{stem}.conf").write_text(source.read_text())
        self.write_delivery_config()
        _ = self.enterContext(patch.object(unrouted_alert, "send_message"))
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)}))
        self.repo = self.temporary / "repo"
        _ = shutil.copytree(repository_seed(), self.repo)
        self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "NEXT_STATE_DIR": str(self.temporary / "next"),
            "SCRATCH_ROOT": str(self.temporary / "scratch"),
            "SCRATCH_HOLDS": str(self.temporary / "holds.json"),
            "SCRATCH_VISIBLE_ROOT": str(self.temporary / "repro"),
            "FEATURE_LOG_VAULT": str(self.temporary / "vault"),
            "XDG_STATE_HOME": str(self.temporary / "state"),
            "STAFF_FILE": str(self.temporary / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.temporary / "showrunner-state"),
            "REBUILD_NEEDS_DIR": str(self.temporary / "rebuild-needs"),
        }))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.temporary / "notifier"))
        _ = self.enterContext(patch.object(change_review, "lists_directory", return_value=self.temporary))
        _ = self.enterContext(patch("scripts.production.change_review.lists_directory", return_value=self.temporary))
        _ = fixture_member("showrunner", name="demo")
        item = change_review.Visible("Numbered moves", "Run moves list", "Each group starts at 1")
        merged = self.git("rev-parse", "HEAD")
        # A production file is configuration: neither a recipient nor a checkout
        # comes from the submitted form.
        self.production = self.temporary / "demo.md"
        self.review_list = self.temporary / "change-review - page-owner.md"
        _ = self.production.write_text(f"""# Demo
## Production Context
- **Showrunner checkout:** `{self.repo}`
- **Showrunner session:** fallback-owner
- **Change review:** `{self.review_list}`
""", encoding="utf-8")
        runner = fixture_member("showrunner", name="page-owner", doc=staff.LiveDoc(local_machine(), self.production))
        self.unit = fixture_member("unit", name="builder-unit", parent=runner.id,
                                   worktree=staff.PresentWorktree(local_machine(), Path("/unused/feature")))
        _ = change_review.append_rows(self.review_list, [item, item, change_review.Visible(
            "Internal comments clarified", "Read the diff", "The old and new comments agree with the tests")],
            date(2026, 11, 1), merged, self.production, producers=[self.unit.id] * 3, record=staff.snapshot())
        for row, kind in (("C1", "See in action"), ("C2", "See in action"), ("C3", "Read only")):
            _ = change_review.set_row_kind(self.review_list, row, kind, self.production)
        self.page_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C3"])).path
        self.page = change_page.page_at(self.root, self.page_path)
        self.messages = []
        self.raw_messages = []
        self.delivery_done = threading.Event()
        deliver = change_page.deliver

        def finish_delivery(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery, status: change_page.DeliveryStatus) -> None:
            try:
                deliver(delivery, status)
            finally:
                self.delivery_done.set()

        _ = self.enterContext(patch.object(change_page, "deliver", side_effect=finish_delivery))
        _ = self.enterContext(patch.object(change_page, "send_message", side_effect=self.capture_message))
        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        # Restart tests start recovery after they create the pending records.
        with patch.object(change_page, "recover_pending"):
            server = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
        self.port = server.server_port
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)

    def write_delivery_config(self, tries: int = 3, pause: int = 0) -> None:
        source = Path(__file__).resolve().parents[2] / "config/review_links.conf"
        content = source.read_text()
        content = re.sub(r"(?m)^delivery_tries=.*$", f"delivery_tries={tries}", content)
        content = re.sub(r"(?m)^delivery_pause_seconds=.*$", f"delivery_pause_seconds={pause}", content)
        _ = (self.temporary / "config/review_links.conf").write_text(content)

    def fail_all_but_alerts(self, calls: list[str]) -> Callable[[str, str, str], None]:
        def send(recipient: str, sender: str, message: str) -> None:
            calls.append(message)
            if "delivery needs attention" not in message:
                raise RuntimeError("socket unavailable")
            self.capture_message(recipient, sender, message)
        return send

    def assert_record_cleared(self, row: str = "C1") -> None:
        self.assertFalse(change_page.DeliveryStatus(self.root).path(self.page, row).exists())

    def assert_no_delivery_controls(self, content: str) -> None:
        for removed in ("Message needs attention:", "The message may not have sent",
                        "Saved, but the message did not send", "The question message did not send",
                        "The validation message did not send", "Send saved answer again",
                        "Send question again", "Send validation request again", "data-resend", 'value="retry"'):
            self.assertNotIn(removed, content)

    def test_recovery_does_not_erase_a_live_request_before_its_verdict_saves(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        began, release = threading.Event(), threading.Event()
        begin = status.begin
        def pause_begin(page: change_page.ReviewPage, row: str, kind: Literal["verdict", "validation"],
                        feedback: str = "", *, recipients: tuple[staff.StaffId, ...] = ()) -> str:
            attempt = begin(page, row, kind, feedback, recipients=recipients)
            began.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test did not release the save")
            return attempt
        def save() -> None:
            delivery = change_page.submit(self.page, urlencode(self.answer()), status)
            change_page.deliver(delivery, status)
        with patch.object(status, "begin", side_effect=pause_begin), ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(save)
            try:
                self.assertTrue(began.wait(TEST_HANG_GUARD_SECS))
                before = status.path(self.page, "C1").read_bytes()
                change_page.recover_pending(status)
                self.assertEqual(status.path(self.page, "C1").read_bytes(), before)
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.DeliveryIntent)
            finally:
                release.set()
            result.result(TEST_HANG_GUARD_SECS)
        self.assertEqual(len(self.messages), 1)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        self.assertEqual(change_page.answer_row(self.page, "C1")["Verdict"], "works")

    def test_a_live_request_and_recovery_send_the_same_row_once_per_recipient(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="not works")), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        started, release = threading.Event(), threading.Event()
        recipients: list[staff.StaffId] = []

        def send(target: staff.StaffId, sender: str, message: str) -> None:
            del sender, message
            recipients.append(target)
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release the send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            sent = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                change_page.recover_pending(status)
                self.assertEqual(recipients, [delivery.recipients[0]])
            finally:
                release.set()
            sent.result(10)
        self.assertCountEqual(recipients, delivery.recipients)
        self.assertEqual(len(recipients), 2)
        self.assertNotIn(delivery.attempt, status.request_attempts)
        self.assert_record_cleared()

    def test_a_live_request_and_recovery_send_different_rows_once_each(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="not works")), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        previous = change_page.DeliveryStatus(self.root)
        recovered = previous.begin(self.page, "C3", "validation", "Check C3", recipients=delivery.alert_recipients)
        started, release = threading.Event(), threading.Event()
        messages: list[tuple[staff.StaffId, str]] = []

        def send(target: staff.StaffId, sender: str, message: str) -> None:
            del sender
            messages.append((target, message))
            if "Change review C1:" in message:
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            sent = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                change_page.recover_pending(status)
                self.assertEqual([target for target, message in messages if "Change review C3:" in message],
                                 list(delivery.alert_recipients))
            finally:
                release.set()
            sent.result(10)
        self.assertCountEqual([target for target, message in messages if "Change review C1:" in message],
                              delivery.recipients)
        self.assertIsInstance(status.slots(self.page, "C3").validation, change_page.ValidationRequested)
        self.assertNotIn(delivery.attempt, status.request_attempts)
        self.assertNotIn(recovered, status.request_attempts)

    def test_concurrent_recovery_claims_every_intent_before_routing_and_sends_each_once(self) -> None:
        self.root = self.root / "ownership"
        previous = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        question = previous.ask(self.page, "C1", "Which groups?", recipients=(runner.id,))
        assert isinstance(question, change_page.QuestionDelivery)
        validation = previous.begin(self.page, "C1", "validation", "Check C1", recipients=(runner.id,))
        status = change_page.DeliveryStatus(self.root)
        started, release = threading.Event(), threading.Event()
        assign = change_routing.StaffRouting.owner_and_production

        def pause_assignment(router: change_routing.StaffRouting, listing: Path,
                             row: dict[str, str]) -> change_routing.ChangeAssignment:
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release routing")
            return assign(router, listing, row)

        with patch.object(change_routing.StaffRouting, "owner_and_production", autospec=True,
                          side_effect=pause_assignment) as routed, ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(change_page.recover_pending, status)
            try:
                self.assertTrue(started.wait(10))
                for attempt in (validation, question.attempt):
                    self.assertIn(attempt, status.request_attempts)
                    self.assertEqual(status.request_attempts[attempt].state, "claimed")
                change_page.recover_pending(status)
                self.assertEqual(routed.call_count, 1)
                self.assertEqual(self.messages, [])
            finally:
                release.set()
            first.result(10)
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(sum("the user asks you to validate this" in message for _, _, message in self.messages), 1)
        self.assertEqual(sum("the user asks a question" in message for _, _, message in self.messages), 1)
        self.assertEqual(status.request_attempts, {})

    def test_duplicate_deliver_calls_send_once_with_or_without_a_request_claim(self) -> None:
        self.root = self.root / "ownership"
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        for claimed in (True, False):
            with self.subTest(claimed=claimed):
                previous = change_page.DeliveryStatus(self.root)
                attempt = previous.begin(self.page, "C1", "verdict", recipients=(runner.id,))
                status = previous if claimed else change_page.DeliveryStatus(self.root)
                delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
                started, release = threading.Event(), threading.Event()
                recipients: list[staff.StaffId] = []

                def send(target: staff.StaffId, sender: str, message: str) -> None:
                    del sender, message
                    recipients.append(target)
                    started.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release the send")

                with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(change_page.deliver, delivery, status)
                    try:
                        self.assertTrue(started.wait(10))
                        change_page.deliver(delivery, status)
                        self.assertEqual(recipients, [runner.id])
                    finally:
                        release.set()
                    first.result(10)
                    change_page.deliver(delivery, status)
                self.assertEqual(recipients, [runner.id])
                self.assertNotIn(attempt, status.request_attempts)
                self.assert_record_cleared()

    def test_completed_and_cleared_attempts_release_the_owner_for_a_new_send(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        for index in range(2):
            attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
            delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
            change_page.deliver(delivery, status)
            self.assertNotIn(attempt, status.request_attempts)
            self.assertEqual(len(self.messages), index + 1)
        recorded = status.begin(self.page, "C1", "verdict")
        status.record(change_page.MessageDelivery(self.page, "C1", (), "", recorded))
        self.assertNotIn(recorded, status.request_attempts)
        cleared = status.begin(self.page, "C1", "verdict")
        status.clear(self.page, "C1", "verdict")
        self.assertNotIn(cleared, status.request_attempts)
        question = status.ask(self.page, "C3", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        with status.locked():
            status.clear_locked(self.page, "C3", status.slots(self.page, "C3"), "question")
        self.assertNotIn(question.attempt, status.request_attempts)
        self.assertEqual(status.request_attempts, {})
        self.assert_record_cleared()

    def test_deliver_releases_a_stale_claim_when_the_saved_intent_is_gone(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "verdict")
        status.path(self.page, "C1").unlink()
        change_page.deliver(change_page.MessageDelivery(self.page, "C1", (), "", attempt), status)
        self.assertNotIn(attempt, status.request_attempts)
        self.assertEqual(self.messages, [])

    def test_duplicate_delivery_keeps_the_sending_owner_when_the_record_disappears(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
        delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
        started, release = threading.Event(), threading.Event()
        with patch.object(change_page, "send_to_staff") as sender, ThreadPoolExecutor(max_workers=1) as executor:
            def send(target: staff.StaffId, name: str, message: str) -> None:
                del target, name, message
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the send")
            sender.side_effect = send
            first = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                status.path(self.page, "C1").unlink()
                change_page.deliver(delivery, status)
                self.assertIn(attempt, status.request_attempts)
                self.assertEqual(status.request_attempts[attempt].state, "sending")
                self.assertEqual(sender.call_count, 1)
            finally:
                release.set()
            first.result(10)
        self.assertNotIn(attempt, status.request_attempts)
        self.assertEqual(sender.call_count, 1)

    def test_replacing_then_clearing_a_verdict_releases_all_claims_and_allows_recovery(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "verdict")
        _ = status.begin(self.page, "C1", "verdict")
        status.clear(self.page, "C1", "verdict")
        self.assertEqual(status.request_attempts, {})
        previous = change_page.DeliveryStatus(self.root)
        pending = previous.begin(self.page, "C1", "validation", "Check this")
        change_page.recover_pending(status)
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(pending, "Check this"))
        self.assertEqual(status.request_attempts, {})

    def test_a_visible_replacement_releases_old_claims_when_directory_sync_fails(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        old = status.begin(self.page, "C1", "validation", "Old")
        with patch.object(atomic_file, "sync_folder", side_effect=OSError("directory sync failed")):
            with self.assertRaises(atomic_file.AtomicDurabilityUnconfirmed):
                _ = status.begin(self.page, "C1", "validation", "New")
        self.assertNotIn(old, status.request_attempts)
        self.assertEqual(status.request_attempts, {})
        current = status.slots(self.page, "C1").validation
        assert isinstance(current, change_page.DeliveryIntent)
        self.assertEqual(current.feedback, "New")
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(current.attempt, "New"))

    def test_answering_an_undelivered_question_releases_its_claim_and_allows_recovery(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, "C1", question.question_id, "All groups"))
        self.assertNotIn(question.attempt, status.request_attempts)
        previous = change_page.DeliveryStatus(self.root)
        pending = previous.begin(self.page, "C1", "validation", "Check this")
        change_page.recover_pending(status)
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        slots = status.slots(self.page, "C1")
        self.assertEqual(slots.validation, change_page.ValidationRequested(pending, "Check this"))
        self.assertEqual(slots.question, change_page.AnsweredQuestion(question.question_id, "Which groups?", "All groups"))
        self.assertEqual(status.request_attempts, {})

    def test_a_replaced_live_send_preserves_the_new_attempt_until_it_delivers_once(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        old = status.begin(self.page, "C1", "validation", "Old", recipients=(runner.id,))
        started, release = threading.Event(), threading.Event()
        messages: list[str] = []

        def send(target: staff.StaffId, sender: str, message: str) -> None:
            del target, sender
            messages.append(message)
            if message == "Old":
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the old send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(change_page.deliver,
                change_page.MessageDelivery(self.page, "C1", (runner.id,), "Old", old), status)
            try:
                self.assertTrue(started.wait(10))
                new = status.begin(self.page, "C1", "validation", "New", recipients=(runner.id,))
                self.assertNotIn(old, status.request_attempts)
                self.assertEqual(status.request_attempts[new].state, "claimed")
            finally:
                release.set()
            first.result(10)
            self.assertEqual(status.request_attempts[new].state, "claimed")
            current = change_page.MessageDelivery(self.page, "C1", (runner.id,), "New", new)
            change_page.deliver(current, status)
            change_page.deliver(current, status)
        self.assertEqual(messages, ["Old", "New"])
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(new, "New"))
        self.assertEqual(status.request_attempts, {})

    def test_recovery_reads_no_staff_or_documents_for_a_completed_question(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, "C1", question.question_id, "All groups"))
        with patch.object(change_page, "list_routing") as routing, patch.object(staff, "snapshot") as snapshot:
            change_page.recover_pending(status)
        routing.assert_not_called()
        snapshot.assert_not_called()
        self.assertEqual(self.messages, [])

    def test_an_unrouted_row_with_a_missing_alert_setting_still_renders(self) -> None:
        _ = self.enterContext(production_staff_record())
        _ = self.enterContext(patch.object(change_routing, "configured_lists_directory", return_value=self.temporary))
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Conflict", "Read", "Proof")],
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[staff.StaffId("missing-producer")], record=staff.snapshot())
        config = self.temporary / "config/review_links.conf"
        _ = config.write_text(config.read_text().replace("unrouted_alert_retry_seconds=60", ""))
        path = urlsplit(change_page.publish_browser()).path
        with redirect_stderr(StringIO()) as errors:
            reply = self.request(path, {})
        forms = BrowserForms()
        forms.feed(reply.body)
        self.assertEqual(reply.status, 200)
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")
        self.assertIn("review-links unrouted alert:", errors.getvalue())

        key = self.file_user_question("Route an unknown owner")
        browser = change_page.ReviewBrowser("private-owner")
        status = change_page.DeliveryStatus(self.root)
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        _ = self.production.write_text(self.production.read_text() + f"- **Proposals:** `{question.record.path.parent}`\n")
        record = staff.snapshot()
        without_owner = staff.StaffSnapshot(tuple(row for row in record.rows if row.id != question.owner))
        with patch.object(staff, "snapshot", return_value=without_owner):
            prepared = change_browser.prepare(browser, status)
        self.assertEqual(next(item.tab for item in prepared.questions if item.key == key), "unrouted")

        with self.subTest(owner="absent without Proposals"):
            _ = self.production.write_text("\n".join(line for line in self.production.read_text().splitlines()
                                                    if not line.startswith("- **Proposals:**")) + "\n")
            with patch.object(staff, "snapshot", return_value=without_owner):
                prepared = change_browser.prepare(browser, status)
                body = change_browser.render(browser, status).decode()
                forms = BrowserForms()
                forms.feed(body)
                self.assertIn(key, {item.key for item in prepared.questions})
                self.assertEqual(next(item.tab for item in prepared.questions if item.key == key), "unrouted")
                self.assertEqual(forms.question_counts["unrouted"], 1)
                self.assertIn('Owner unavailable (<span data-count>1</span>)', body)
                self.assertEqual(forms.outcomes[key], [])
                self.assertNotIn(key, forms.save_urls)
                self.assertIn("No showrunner owns this question.", body)
                self.assertIn("Which layout gives each review card enough space for its controls?", body)
                fields = {"row": key, "token": change_browser.row_token(browser, key),
                          "outcome": "answer", "feedback": "Use compact"}
                with self.assertRaisesRegex(change_page.QuestionAnswerRefused, "No showrunner owns"):
                    _ = change_browser.submit(browser, urlencode(fields), status)

    def test_recovery_clears_a_true_orphan_without_saving_or_sending_a_verdict(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "verdict")
        before = self.review_list.read_bytes()
        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertIsInstance(restarted.slots(self.page, "C1").verdict, change_page.NoDelivery)

    def test_a_send_that_fails_once_uses_two_tries_and_clears_its_record_without_an_alert(self) -> None:
        calls: list[str] = []
        def send(recipient: str, sender: str, message: str) -> None:
            calls.append(message)
            if len(calls) == 1:
                raise RuntimeError("socket unavailable")
            self.capture_message(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=send):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 2)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertTrue(all("delivery needs attention" not in message for message in calls))
        self.assert_record_cleared()

    def test_an_exhausted_send_alerts_once_and_the_page_and_footer_exclude_the_saved_row(self) -> None:
        calls: list[str] = []
        with patch.object(change_page, "send_message", side_effect=self.fail_all_but_alerts(calls)):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 4)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        alert = self.messages[0][2]
        self.assertIn("List: " + str(self.review_list), alert)
        self.assertIn("Row: C1", alert)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        self.assertIn("Recipient: " + str(runner.id), alert)
        self.assertIn("Tries: 3", alert)
        self.assertIn("Last error: socket unavailable", alert)
        self.assertIn("The user's answer is saved. Pass it on by hand.", alert)
        self.assert_record_cleared()
        path, forms, content = self.browser()
        del path
        self.assertEqual(forms.counts["page-owner"], 2)
        self.assertEqual(footer_counts.changes(self.production), forms.counts["page-owner"])
        self.assert_no_delivery_controls(content)

    def test_a_queued_result_finishes_delivery_without_an_alert(self) -> None:
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
        with patch.object(change_page, "send_message", new=SEND_MESSAGE), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "QUEUED: recipient absent", "")) as run:
            change_page.deliver(change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt), status)
        self.assertEqual(run.call_count, 1)
        self.assert_record_cleared()

    def test_server_start_delivers_a_legacy_pending_verdict_without_page_context(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text(json.dumps({
            "attempt": "legacy", "state": "pending", "kind": "verdict"}))
        self.messages.clear()
        self.delivery_done.clear()
        with serve.ReviewLinksServer(("127.0.0.1", 0), self.root):
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1: works", self.messages[0][2])
        self.assert_record_cleared()

    def test_restart_finds_pending_records_without_page_context_and_reads_each_list_once(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        for row in ("C1", "C3"):
            _ = status.begin(self.page, row, "validation", "Check this change")
            fields = cast(dict[str, object], json.loads(status.path(self.page, row).read_text()))
            self.assertNotIn("page", fields)
        with patch.object(change_review, "review_snapshot", wraps=change_review.review_snapshot) as read:
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args, (self.review_list,))
        self.assertEqual(len(self.messages), 2)
        for row in ("C1", "C3"):
            self.assertEqual(sum(f"Change review {row}:" in message for _, _, message in self.messages), 1)
            self.assertIsInstance(status.slots(self.page, row).validation, change_page.ValidationRequested)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_restart_ignores_a_legacy_records_stored_page_context(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Keep these words")
        path = status.path(self.page, "C1")
        fields = cast(dict[str, object], json.loads(path.read_text()))
        fields["page"] = {"review_list": "/missing-list", "production": "/wrong-production", "row": "C999"}
        _ = path.write_text(json.dumps(fields))
        before = self.review_list.read_bytes()
        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        change_page.recover_pending(restarted)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Change review C1: the user asks you to validate this", self.messages[0][2])
        self.assertIn(f"List: {self.review_list}", self.messages[0][2])
        self.assertIn("Production: page-owner", self.messages[0][2])
        self.assertIn("User feedback: Keep these words", self.messages[0][2])
        self.assertNotIn("/wrong-production", self.messages[0][2])
        self.assertNotIn("page", cast(dict[str, object], json.loads(path.read_text())))
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIsInstance(status.slots(self.page, "C1").validation, change_page.ValidationRequested)

    def test_recovered_messages_use_the_same_owner_production_as_live_messages(self) -> None:
        doc = self.temporary / "current-owner-production.md"
        _ = doc.write_text(self.production.read_text())
        runner = fixture_member("showrunner", name="current-owner", doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer()), status)
        change_page.deliver(delivery, status)
        self.assertEqual(len(self.messages), 1)
        _ = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), 2)
        production_lines = [next(line for line in message.splitlines() if line.startswith("Production:"))
                            for _, _, message in self.messages]
        self.assertEqual(production_lines, ["Production: current-owner"] * 2)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["current-owner"] * 2)
        self.assert_record_cleared()

    def test_browser_and_recovery_use_the_same_owner_and_production_function(self) -> None:
        doc = self.temporary / "current-owner-production.md"
        _ = doc.write_text(self.production.read_text())
        runner = fixture_member("showrunner", name="current-owner", doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        assignments: list[change_routing.ChangeAssignment] = []
        resolve = change_routing.StaffRouting.owner_and_production

        def capture(routing: change_routing.StaffRouting, listing: Path, row: dict[str, str]) -> change_routing.ChangeAssignment:
            assignment = resolve(routing, listing, row)
            if listing == self.review_list and row["#"] == "C1":
                assignments.append(assignment)
            return assignment

        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Check the proof")
        with patch.object(change_routing.StaffRouting, "owner_and_production", autospec=True, side_effect=capture):
            prepared = change_browser.prepare(change_page.ReviewBrowser("token"), status)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
        change = next(item for item in prepared.changes if item.key == "page-owner/C1")
        self.assertIsInstance(change.page, change_page.AssignedReviewPage)
        assert isinstance(change.page, change_page.AssignedReviewPage)
        self.assertEqual(assignments, [change_routing.ChangeAssignment(change.page.owner, change.page.production)] * 2)
        self.assertEqual(change.page.owner.showrunner_recipients, (runner.id,))
        self.assertEqual(change.page.production, doc)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Production: current-owner", self.messages[0][2])

    def test_a_row_without_a_staff_unit_uses_its_list_showrunner_and_production(self) -> None:
        row = change_page.answer_row(self.page, "C1") | {"Producer": "", "Merged": "", "Production": "another-production"}
        routing = change_page.list_routing({self.review_list: change_review.review_snapshot(self.review_list)})
        assignment = routing.owner_and_production(self.review_list, row)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        self.assertEqual(assignment.owner, change_routing.ShowrunnerOwnChange(runner.id, "page-owner"))
        self.assertEqual(assignment.production, self.production)

    def test_two_orphan_answers_save_and_reach_the_root_after_the_first_stamps_its_document(self) -> None:
        listing = self.temporary / "change-review - orphan.md"
        missing = self.temporary / "wrapped-production.md"
        _ = listing.write_text("\n".join((
            "---", "productions:", f'  - "{missing}"', "---", "## To review", "",
            "| # | Change | Merged | How to check |",
            "| --- | --- | --- | --- |",
            "| C1 | First orphan | | Read the first change |",
            "| C2 | Second orphan | | Read the second change |", "")))
        settings = Path(os.environ["CONF_DIR"]) / "review_links.conf"
        _ = settings.write_text(settings.read_text().replace(
            "unrouted_alert_recipient=natedev", "unrouted_alert_recipient=page-owner"))
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        browser = change_page.ReviewBrowser("orphan-token")
        status = change_page.DeliveryStatus(self.root)
        for number in (1, 2):
            with self.subTest(row=number):
                prepared = change_browser.prepare(browser, status)
                change = next(item for item in prepared.changes if item.key == f"orphan/C{number}")
                page = change.page
                self.assertIsInstance(page, change_page.AssignedReviewPage)
                assert isinstance(page, change_page.AssignedReviewPage)
                self.assertEqual(page.owner, change_routing.ShowrunnerOwnChange(runner.id, runner.name))
                self.assertEqual(change.tab, runner.name)
                self.assertEqual(page.production, self.production)
                self.assertEqual(page.owner.showrunner_recipients, (runner.id,))
                delivery = change_page.submit(page, urlencode({"row": f"C{number}", "token": page.token,
                    "outcome": "works", "feedback": ""}), status)
                self.assertIsInstance(delivery, change_page.MessageDelivery)
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertEqual(delivery.recipients, (runner.id,))
                change_page.deliver(delivery, status)
                self.assertEqual(change_review.review_snapshot(listing).reviewed[-1]["Verdict"], "works")
                self.assertIn(self.production, change_routing.list_documents(listing.read_text()))
                self.assertEqual([recipient for recipient, _, _ in self.messages], [runner.name] * number)
                self.assertIn(f"Change review C{number}: works", self.messages[-1][2])
        self.assertEqual({row["#"]: row["Verdict"] for row in change_review.review_snapshot(listing).reviewed},
                         {"C1": "works", "C2": "works"})

    def test_new_delivery_records_never_store_page_context(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        verdict = status.begin(self.page, "C1", "verdict")
        validation = status.begin(self.page, "C2", "validation", "Keep this feedback")
        question = status.ask(self.page, "C3", "Which comments?")
        assert isinstance(question, change_page.QuestionDelivery)
        for row, attempt in (("C1", verdict), ("C2", validation), ("C3", question.attempt)):
            with self.subTest(row=row):
                fields = cast(dict[str, object], json.loads(status.path(self.page, row).read_text()))
                self.assertNotIn("page", fields)
                self.assertEqual(status.slots(self.page, row).intents()[0].attempt, attempt)
        status.record(change_page.MessageDelivery(self.page, "C2", (), "", validation))
        self.assertIsInstance(status.slots(self.page, "C2").validation, change_page.ValidationRequested)
        self.assertNotIn("page", cast(dict[str, object], json.loads(status.path(self.page, "C2").read_text())))
        self.assertTrue(status.answer_question(self.page, "C3", question.question_id, "The comments below the change"))
        self.assertEqual(status.slots(self.page, "C3").question,
                         change_page.AnsweredQuestion(question.question_id, question.text, "The comments below the change"))
        self.assertNotIn("page", cast(dict[str, object], json.loads(status.path(self.page, "C3").read_text())))

    def test_delivery_uses_the_live_config_budget_and_pause(self) -> None:
        self.write_delivery_config(tries=2)
        calls: list[str] = []
        with patch.object(change_page, "send_message", side_effect=self.fail_all_but_alerts(calls)), patch.object(time, "sleep") as pause:
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 3)
        self.assertIn("Tries: 2", calls[-1])
        pause.assert_called_once_with(0)

    def test_pending_and_legacy_failed_records_have_no_delivery_controls_on_either_page(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        _ = change_review.record_verdict(self.review_list, "C1", "works", date(2026, 11, 2), self.production)
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                _ = status.path(self.page, "C1").write_text(json.dumps({"verdict": {"attempt": "old", "state": state}}))
                self.assert_no_delivery_controls(self.request(path, {}).body)
                content = self.request(self.page_path, {}).body
                self.assertIn("Saved: works", content)
                self.assert_no_delivery_controls(content)

    def test_every_message_kind_resolves_a_unit_staff_id_to_its_short_live_session(self) -> None:
        previous_session = staff.native_id(fixture_runtime_id("resources-previous"))
        new_session = staff.native_id(fixture_runtime_id("resources"))
        runner = fixture_member("showrunner", name="resources-unit", runtime_id=str(previous_session))
        owner = change_routing.StaffRoutedChange(self.unit.id, runner.id, runner.name)
        status = change_page.DeliveryStatus(self.root)
        def send_after_account_move(recipient: str, sender: str, message: str) -> None:
            if recipient == f"session:{previous_session}":
                _ = staff.update_current_session(previous_session, new_session)
                raise RuntimeError("the account moved")
            self.capture_message(recipient, sender, message)
        for row, outcome, feedback in (("C1", "not works", ""), ("C2", "validate", "Check"), ("C3", "question", "Why?")):
            with self.subTest(outcome=outcome), patch.object(change_page, "send_message", side_effect=send_after_account_move):
                current = staff.snapshot().by_id(runner.id)
                assert current is not None
                _ = staff.update_current_session(current.current_session, previous_session)
                page = change_page.AssignedReviewPage(self.review_list, self.production, (row,), self.page.token, owner)
                delivery = change_page.submit(page, urlencode(self.answer(row, outcome, feedback)), status)
                change_page.deliver(delivery, status)
        self.assertEqual(sum(recipient == f"session:{new_session}" for recipient, _, _ in self.raw_messages), 3)
        for detail in ("not works", "validate this", "user asks a question"):
            self.assertEqual(sum(detail in message for target, _, message in self.raw_messages
                                 if target == f"session:{new_session}"), 1)
        self.assertTrue(all(target != "resources-unit" and target != "session:resources-unit"
                            for target, _, _ in self.raw_messages))

    def recipient_name(self, recipient: str) -> str:
        if recipient.startswith("session:"):
            member = staff.snapshot().by_current_session(recipient.removeprefix("session:"))
            if member is None:
                raise AssertionError("the recipient must name a current staff session")
            return member.name
        return recipient

    def capture_message(self, recipient: str, sender: str, message: str) -> None:
        self.raw_messages.append((recipient, sender, message))
        self.messages.append((self.recipient_name(recipient), sender, message))

    def request(self, path: str, fields: dict[str, str]) -> Reply:
        self.delivery_done.clear()
        reply = self.http_request(path, fields)
        if reply.status == 303 and fields.get("outcome") != "next":
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS), "message delivery did not finish")
        return reply

    def http_request(self, path: str, fields: dict[str, str]) -> Reply:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            if fields:
                connection.request("POST", path, urlencode(fields),
                                   {"Content-Type": "application/x-www-form-urlencoded"})
            else:
                connection.request("GET", path)
            response = connection.getresponse()
            return Reply(response.status, response.read().decode("utf-8"), response.getheader("Location") or "")
        finally:
            connection.close()

    def answer(self, row: str = "C1", outcome: str = "works", feedback: str = "") -> dict[str, str]:
        return {"row": row, "outcome": outcome, "feedback": feedback, "token": self.page.token}

    def browser(self) -> tuple[str, BrowserForms, str]:
        path = urlsplit(change_browser.publish()).path
        reply = self.request(path, {})
        self.assertEqual(reply.status, 200)
        forms = BrowserForms()
        forms.feed(reply.body)
        return path, forms, reply.body

    def browser_fields(self, forms: BrowserForms, key: str, outcome: str = "works", feedback: str = "") -> dict[str, str]:
        return forms.rows[key] | {"row": key, "outcome": outcome, "feedback": feedback}

    def answer_question(self, text: str, row: str = "C1", *, question_id: str = "") -> subprocess.CompletedProcess[str]:
        if not question_id:
            question = change_page.DeliveryStatus(self.root).slots(self.page, row).question
            question_id = question.question_id if not isinstance(question, change_page.NoQuestion) else "unknown"
        return self.command(change_page, change_page.main,
                            ["answer-question", "--production", "page-owner", "--list", str(self.review_list),
                             "--row", row, "--question", question_id, "--text", text])

    def file_user_question(self, text: str = "Choose the layout", *, asker: staff.StaffRow | None = None) -> str:
        """File a canonical question without a reviewer or a real message sender."""
        document = self.production.read_text()
        if "- **Merge branch:**" not in document:
            _ = self.production.write_text(document + "- **Merge branch:** main\n- **Log:** progress.log\n")
        record = staff.snapshot()
        identities = update_registration.QuestionStaff.for_record(record)
        owner = next(row for row in record.rows if row.name == "page-owner")
        production = read_production(owner, record)
        items = [item.item for item in update_registration.question_records(production, identities) if item.status == "open"]
        item = update_registration.OutstandingItem(since="2026-11-12T12:00", text=text,
            question="Which layout gives each review card enough space for its controls?\n1. Compact <layout>\n2. Wide layout",
            asked_by=(asker or self.unit).id)
        update_registration.store_questions(production, [*items, item], identities)
        filed = next(item for item in update_registration.question_records(production, identities) if item.item["text"] == text)
        return "question/" + owner.id + "/" + filed.path.name

    def test_all_lists_load_once_and_the_browser_contains_the_whole_waiting_queue(self) -> None:
        other = self.temporary / "change-review - another.md"
        owner = fixture_member("showrunner", name="another", doc=staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read the proof", "It agrees")],
                                      date(2026, 11, 2), "", self.production, producers=[owner.id], record=staff.snapshot())
        path = urlsplit(change_browser.publish()).path
        with patch.object(change_review, "read", wraps=change_review.read) as reads:
            reply = self.request(path, {})
        self.assertEqual(reply.status, 200)
        for review_list in (self.review_list, other):
            self.assertEqual(sum(call.args == (review_list,) for call in reads.call_args_list), 1)
        forms = BrowserForms()
        forms.feed(reply.body)
        self.assertEqual(set(forms.rows), {"page-owner/C1", "page-owner/C2", "page-owner/C3", "another/C1"})
        self.assertIn('another (<span data-count>1</span>)', reply.body)
        self.assertIn('page-owner (<span data-count>3</span>)', reply.body)
        self.assertIn("Read only", reply.body)
        self.assertIn("The old and new comments agree with the tests", reply.body)
        self.assertNotIn('value="next"', reply.body)

        question = self.file_user_question()
        _, forms, body = self.browser()
        self.assertIn('data-review-type="questions"', body)
        self.assertIn('data-review-type="changes"', body)
        self.assertIn('data-review-type="rebuilds"', body)
        self.assertIn('data-type-count="questions">1</span>', body)
        self.assertIn('data-type-count="changes">4</span>', body)
        self.assertIn("Which layout gives each review card enough space for its controls?", body)
        self.assertEqual(body.count("Choose the layout"), 1)
        self.assertIn("questions:'Questions to answer',changes:'Changes to review',rebuilds:'Rebuilds needed'", body)
        self.assertIn('<title>Changes to review</title>', body)
        self.assertIn("Compact &lt;layout&gt;", body)
        self.assertIn("Asked by builder-unit", body)
        self.assertEqual(forms.tabs[question], "page-owner")
        self.assertEqual(forms.outcomes[question], ["answer"])
        self.assertEqual(forms.question_counts["page-owner"], 1)
        for review_type in ("questions", "changes", "rebuilds"):
            with self.subTest(review_type=review_type, total="absent"):
                self.assertNotIn('data-total', body)
                self.assertNotIn("total.textContent", body)
                self.assertNotIn("+' waiting'", body)

        with self.subTest(rebuilds="missing folder"):
            browser = change_page.ReviewBrowser("private-rebuilds")
            status = change_page.DeliveryStatus(self.root)
            with patch.object(need, "read_needs", wraps=need.read_needs) as reads:
                body = change_browser.render(browser, status).decode()
            reads.assert_called_once_with()
            self.assertIn('data-type-count="rebuilds">0</span>', body)
            empty = body.split('<div hidden data-rebuilds data-count="0">', 1)[1].split('</div>', 1)[0]
            self.assertEqual(empty, '<p>No rebuilds needed.</p>')
            self.assertFalse(Path(os.environ["REBUILD_NEEDS_DIR"]).exists())

        with self.subTest(rebuilds="machine reasons and current names"):
            owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
            second = fixture_member("unit", name="second-session", parent=owner.id)
            _ = staff.rename(self.unit.id, "renamed <unit>")
            rows: tuple[tuple[need.Machine, str, str, str, str, str], ...] = (
                ("natedev", "b" * 40, "Apply the second settings.", second.id, second.id, "2026-11-12T13:00:00+00:00"),
                ("natedev", "a" * 40, "Apply the first <settings>.", self.unit.id, owner.id, "2026-11-12T12:00:00+00:00"),
                ("mac", "c" * 40, "Apply the Mac settings.", "unknown-staff", "unknown-requester", "2026-11-12T11:00:00+00:00"),
            )
            for machine, commit, reason, concerns, requester, created in rows:
                need.add_need(machine, commit, reason, concerns, requester)
                path = Path(os.environ["REBUILD_NEEDS_DIR"]) / f"{machine}-{commit}.json"
                record = cast(need.NeedRecord, json.loads(path.read_text()))
                record["created_at"] = created
                _ = path.write_text(json.dumps(record))
            with patch.object(need, "read_needs", wraps=need.read_needs) as reads:
                body = change_browser.render(browser, status).decode()
            reads.assert_called_once_with()
            self.assertIn('data-type-count="rebuilds">2</span>', body)
            self.assertLess(body.index('data-review-type="changes"'), body.index('data-review-type="rebuilds"'))
            block = body.split('<div hidden data-rebuilds data-count="2">', 1)[1].split('</div>', 1)[0]
            self.assertLess(block.index('data-machine="natedev"'), block.index('data-machine="mac"'))
            self.assertIn('<h2>Rebuild natedev</h2>', block)
            self.assertIn('<h2>Rebuild the Mac</h2>', block)
            self.assertIn('<p>Run <code>rebuild</code> in a terminal on natedev.</p>', block)
            self.assertIn('<p>Run <code>rebuild</code> in Terminal on the Mac.</p>', block)
            self.assertIn('<p>Mac updates can take five minutes while the Mac is reachable.</p>', block)
            self.assertLess(block.index("Apply the second settings."), block.index("Apply the first &lt;settings&gt;."))
            self.assertEqual(block.count('<p>After the rebuild</p>'), 2)
            self.assertEqual(block.count('<details><summary>Details</summary>'), 2)
            self.assertNotIn('<details open', block)
            natedev, mac = block.split('</article>', 1)
            self.assertEqual(natedev.count('One rebuild applies all listed changes.'), 1)
            self.assertNotIn('One rebuild applies all listed changes.', mac)
            main, details = natedev.split('<details>', 1)
            self.assertNotIn('For ', main)
            self.assertNotIn('renamed', main)
            self.assertNotIn('Configuration commit:', main)
            self.assertIn('Configuration commit: <code>' + 'b' * 40 + '</code>', details)
            self.assertIn('Configuration commit: <code>' + 'a' * 40 + '</code>', details)
            self.assertLess(details.index('b' * 40), details.index('a' * 40))
            self.assertIn('Affected session: renamed &lt;unit&gt; (unit)', details)
            self.assertIn('Affected session: second-session (unit)', details)
            self.assertEqual(details.count('Requested by:'), 1)
            self.assertIn('Requested by: page-owner (showrunner)', details)
            self.assertNotIn('Requested by: second-session', details)
            self.assertIn('Unknown session: unknown-staff', mac)
            self.assertIn('Unknown session: unknown-requester', mac)
            self.assertIn('Configuration commit: <code>' + 'c' * 40 + '</code>', mac)
            self.assertTrue(block.endswith('<p>Completed requests clear automatically. Refresh this page after the rebuild.</p>'))
            for control in ('<form', '<textarea', 'data-showrunner', 'data-next', 'data-key'):
                self.assertNotIn(control, block)
            self.assertIn("showrunners.hidden=rebuildMode", body)
            self.assertIn("next.hidden=rebuildMode || !waiting().some(r=>r!==current)", body)
            self.assertNotIn('data-total', body)

            with self.subTest(rebuilds="need spacing"):
                self.assertIn('[data-rebuilds] li+li{margin-top:16px}', body)
                self.assertIn('[data-rebuilds] details p{margin:0}', body)
                self.assertIn('[data-rebuilds] details p+p{margin-top:8px}', body)
                self.assertIn('margin-top:16px;padding-top:16px', body)
                self.assertIn('flex-wrap:wrap;gap:8px', body)
                self.assertIn('section h1,h2{font-size:20px', body)

            with self.subTest(rebuilds="requester absent"):
                single: need.NeedRecord = {**need.read_needs()[0].newest, "requested_by": ""}
                summary = need.MachineNeeds(single, (single["reason"],), (single,))
                content = change_browser.rebuild_list([summary], staff.snapshot())
                self.assertNotIn('Requested by:', content)
                self.assertNotIn('One rebuild applies all listed changes.', content)

    def test_user_question_answers_follow_each_typed_relay(self) -> None:
        destination = fixture_member("showrunner", name="destination")
        owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
        moved = fixture_member("unit", name="moved-unit", parent=owner.id)
        for name, asker, recipient in (("unit", self.unit, self.unit.name), ("owner", owner, owner.name),
                                        ("moved", moved, destination.name)):
            with self.subTest(relay=name):
                key = self.file_user_question("Choose " + name, asker=asker)
                if name == "moved":
                    _ = staff.move(moved.id, staff.ReportsTo(destination.id))
                path, forms, _ = self.browser()
                self.delivery_done.clear()
                reply = self.http_request(path, self.browser_fields(forms, key, "answer", "Use compact\nKeep the choices"))
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(json.loads(reply.body), {"answer": "Use compact\nKeep the choices"})
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(self.messages[-1][0], recipient)
                self.assertTrue(self.messages[-1][2].startswith("From the user (via the review page): Choose " + name))
                self.assertIn("Use compact\nKeep the choices", self.messages[-1][2])
                if name == "moved":
                    self.assertIn("For: moved-unit", self.messages[-1][2])
                _, after, _ = self.browser()
                self.assertNotIn(key, after.rows)
                count = len(self.messages)
                duplicate = self.http_request(path, self.browser_fields(forms, key, "answer", "Use compact"))
                self.assertEqual(duplicate.status, 400)
                self.assertIn("no longer open", duplicate.body)
                self.assertEqual(len(self.messages), count)

        with self.subTest(requests="two prepared answers"):
            key = self.file_user_question("Keep one delivery")
            browser = change_page.ReviewBrowser("private-concurrent")
            first_status = change_page.DeliveryStatus(self.root)
            second_status = change_page.DeliveryStatus(self.root)
            first = next(item for item in change_browser.prepare(browser, first_status).questions if item.key == key)
            second = next(item for item in change_browser.prepare(browser, second_status).questions if item.key == key)
            delivery, _ = change_browser.submit_question(first, "First answer", first_status)
            pending = first_status.path(delivery.page, delivery.row).read_bytes()
            with self.assertRaises(change_page.QuestionAnswerRefused):
                _ = change_browser.submit_question(second, "Second answer", second_status)
            self.assertEqual(first_status.path(delivery.page, delivery.row).read_bytes(), pending)
            count = len(self.messages)
            change_page.deliver(delivery, first_status)
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count + 1)
            self.assertIn("First answer", self.messages[-1][2])
            self.assertNotIn("Second answer", self.messages[-1][2])

    def test_staff_moves_the_unit_changes_to_its_current_showrunner_tab(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        path, forms, body = self.browser()
        self.assertEqual(forms.tabs["page-owner/C1"], "sdlc")
        self.assertIn('sdlc (<span data-count>3</span>)', body)
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Check this yourself"))
        self.assertEqual(reply.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["sdlc"])
        self.assertIn("Check this yourself", self.messages[0][2])
        self.assertEqual(len(change_review.pending(self.review_list)), 3)
        refreshed = BrowserForms()
        refreshed.feed(self.request(path, {}).body)
        self.assertEqual(list(refreshed.rows), ["page-owner/C2", "page-owner/C3", "page-owner/C1"])

    def test_a_renamed_unit_gets_its_current_staff_session(self) -> None:
        from scripts.production.staff_test_support import fixture_runtime_id

        _ = staff.rename(self.unit.id, "x")
        current = staff.update_current_session(self.unit.id, staff.native_id(fixture_runtime_id("session-x")))
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        self.assertEqual(self.raw_messages[0][0], f"session:{current.current_session}")
        self.assertEqual(self.messages[0][0], "x")

    def test_a_delivery_resolves_the_staff_session_after_an_account_move(self) -> None:
        from scripts.production.staff_test_support import fixture_runtime_id

        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="validate")), status)
        owner = staff.snapshot().by_path("page-owner")
        if owner is None:
            self.fail("the showrunner must have a staff row")
        moved = staff.update_current_session(owner.id, staff.native_id(fixture_runtime_id("moved-showrunner")))
        record = cast(dict[str, object], json.loads(status.path(self.page, "C1").read_text()))
        validation = cast(dict[str, object], record["validation"])
        self.assertEqual(validation["recipients"], [owner.id])
        change_page.deliver(delivery, status)
        self.assertEqual(self.raw_messages[-1][0], f"session:{moved.current_session}")

    def test_tabs_with_waiting_changes_keep_their_order_before_empty_tabs(self) -> None:
        other = self.temporary / "change-review - zeta.md"
        owner = fixture_member("showrunner", name="zeta", doc=staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read", "Proof")],
                                      date(2026, 11, 2), "", self.production, producers=[owner.id], record=staff.snapshot())
        path, forms, body = self.browser()
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertEqual(list(prepared.queues), ["page-owner", "zeta", "demo"])
        self.assertEqual(list(forms.counts.items()), [("page-owner", 3), ("zeta", 1), ("demo", 0)])
        self.assertIn("tabs[0]?.dataset.showrunner", body)

    def test_sent_validation_and_question_tabs_sort_after_waiting_tabs(self) -> None:
        other = self.temporary / "change-review - alpha.md"
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read", "Proof")],
                                      date(2026, 11, 2), "", self.production, record=staff.snapshot())
        page = change_page.ReviewPage(other, self.production, ("C1",), "")
        status = change_page.DeliveryStatus(self.root)
        for kind in ("validation", "question"):
            with self.subTest(kind=kind):
                status.path(page, "C1").unlink(missing_ok=True)
                if kind == "validation":
                    attempt = status.begin(page, "C1", "validation")
                else:
                    question = status.ask(page, "C1", "Which change?")
                    if not isinstance(question, change_page.QuestionDelivery):
                        self.fail("a new question must create a message attempt")
                    attempt = question.attempt
                path, pending, body = self.browser()
                del body
                self.assertEqual(list(pending.counts.items()), [("page-owner", 3), ("alpha", 0), ("demo", 0)])
                status.record(change_page.MessageDelivery(page, "C1", (), "", attempt))
                forms = BrowserForms()
                forms.feed(self.request(path, {}).body)
                self.assertEqual(list(forms.counts.items()), [("page-owner", 3), ("alpha", 0), ("demo", 0)])
                prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
                self.assertEqual(list(prepared.queues), ["page-owner", "alpha", "demo"])

    def test_the_selected_tab_outline_stays_inside_its_scroll_container(self) -> None:
        path, forms, body = self.browser()
        del path, forms
        self.assertIn("nav button[aria-selected=true]{outline:2px solid currentColor;outline-offset:-2px}", body)

    def test_production_aliases_share_one_document_read_per_page_request(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        reads: list[Path] = []
        read_text = Path.read_text

        def read_document(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(path)
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", new=read_document):
            path, forms, body = self.browser()
        del path, body
        self.assertEqual(reads.count(self.production), 1)
        self.assertEqual(forms.counts["sdlc"], 3)
        self.assertEqual(set(forms.save_urls), {"page-owner/C1", "page-owner/C2", "page-owner/C3"})

    def test_an_unrouted_row_refuses_a_question_without_saving_and_still_accepts_a_verdict(self) -> None:
        assert isinstance(self.unit.parent, staff.ReportsTo)
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Unknown", "Read", "Proof")],
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[staff.StaffId("missing-producer")], record=staff.snapshot())
        path, forms, body = self.browser()
        self.assertIn('Owner unavailable (<span data-count>1</span>)', body)
        self.assertNotIn('>unrouted (<span', body)
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")
        self.assertNotIn("question", forms.outcomes["page-owner/C4"])
        with self.subTest(controls="unavailable owner"):
            self.assertEqual(forms.outcomes["page-owner/C4"], ["works", "not works"])
        before = self.review_list.read_bytes()
        status = change_page.DeliveryStatus(self.root)
        browser = change_browser.page_at(self.root, path)
        fields = self.browser_fields(forms, "page-owner/C4", "question", "Who checks this?")
        with self.assertRaisesRegex(change_page.AnswerRefused, "no showrunner to answer a question"):
            _ = change_browser.submit(browser, urlencode(fields), status)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertFalse(status.path(self.page, "C4").exists())
        self.assertEqual(self.http_request(path, fields).status, 400)
        self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C4")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(self.messages, [])
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C4")
        self.assertEqual(saved["Verdict"], "works")

    def test_a_question_without_recipients_alerts_its_showrunner_and_finishes(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Who checks this?")
        assert isinstance(question, change_page.QuestionDelivery)
        change_page.deliver(change_page.MessageDelivery(self.page, "C1", (), "Question", question.attempt, change_page.route_owner(self.page, change_page.answer_row(self.page, "C1")).showrunner_recipients), status)
        self.assertNotIn(question.attempt, status.request_attempts)
        saved = status.slots(self.page, "C1").question
        assert isinstance(saved, change_page.OpenQuestion)
        self.assertIsInstance(saved.delivery, change_page.NoDelivery)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("no showrunner to send to", self.messages[0][2])
        self.assertIn("Pass it on by hand", self.messages[0][2])

    def test_a_legacy_failed_verdict_shows_the_saved_answer_and_sends_nothing(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text(json.dumps({"attempt": "old", "state": "failed", "kind": "verdict"}))
        before = self.review_list.read_bytes()
        self.messages.clear()
        change_page.recover_pending(status)
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works", content)
        self.assert_no_delivery_controls(content)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_producer_keeps_its_list_showrunner_without_a_new_staff_parent(self) -> None:
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")

    def test_a_showrunner_own_change_stays_under_its_source_list(self) -> None:
        merged = self.git("rev-parse", "HEAD")
        _ = self.review_list.write_text(self.review_list.read_text().replace(merged, "").replace(self.unit.id, ""))
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")

    def test_an_unknown_unit_stays_in_its_list_even_when_production_names_another_showrunner(self) -> None:
        foreign = self.temporary / "sdlc-production.md"
        _ = foreign.write_text(self.production.read_text())
        _ = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), foreign))
        written = change_review.append_rows(self.review_list, [change_review.Visible("Unknown unit", "Read", "Proof")],
                                      date(2026, 11, 2), "abcdef12", foreign, record=staff.snapshot())
        owner = staff.snapshot().by_path("sdlc")
        assert owner is not None
        _ = self.review_list.write_text(self.review_list.read_text().replace(
            written[0], written[0].replace(str(owner.id), "sdlc")))
        row = next(row for row in change_review.pending(self.review_list) if row["#"] == "C4")
        self.assertEqual(change_page.row_production(self.page, row), self.production)
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "page-owner")

    def test_a_registered_production_saves_when_the_list_has_only_a_missing_document_reference(self) -> None:
        text = self.review_list.read_text().replace(str(self.production), str(self.temporary / "missing.md"))
        _ = self.review_list.write_text(text)
        with patch.object(showrunners, "timer_docs", return_value={"page-owner": self.production}):
            path, forms, body = self.browser()
            del body
            reply = self.http_request(urljoin(path, forms.save_urls["page-owner/C1"]),
                                      self.browser_fields(forms, "page-owner/C1"))
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(cast(object, json.loads(reply.body)), {"answer": "works"})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        row = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(row["Verdict"], "works")
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])

    def test_a_list_without_a_production_document_shows_its_changes_without_answer_controls(self) -> None:
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        _ = staff.set_doc(runner.id, staff.LiveDoc(local_machine(), self.temporary / "missing.md"))
        text = self.review_list.read_text().replace(str(self.production), str(self.temporary / "missing.md"))
        _ = self.review_list.write_text(text)
        path, forms, body = self.browser()
        self.assertEqual(set(forms.rows), {"page-owner/C1", "page-owner/C2", "page-owner/C3"})
        self.assertEqual(forms.save_urls, {})
        self.assertIn("The production document is not available. This change cannot accept an answer.", body)
        browser = change_browser.page_at(self.root, path)
        fields = {"row": "page-owner/C1", "token": change_browser.row_token(browser, "page-owner/C1"),
                  "outcome": "works", "feedback": ""}
        self.assertEqual(self.http_request(path, fields).status, 400)
        self.assertEqual(len(change_review.pending(self.review_list)), 3)

    def test_an_earlier_lists_prerequisites_keep_qualified_ids_in_the_embedded_tab_order(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        earlier = self.temporary / "change-review - earlier.md"
        _ = change_review.append_rows(earlier, [change_review.Visible("First", "Read", "Proof")],
                                      date(2026, 11, 2), "abcdef12", self.production,
                                      urgency.cell(urgency.read(), "C2"), producers=[self.unit.id], record=staff.snapshot())
        _ = change_review.append_rows(earlier, [change_review.Visible("Second", "Read", "Proof")],
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[self.unit.id], record=staff.snapshot())
        path, forms, body = self.browser()
        del body
        keys = list(forms.rows)
        self.assertLess(keys.index("earlier/C2"), keys.index("earlier/C1"))
        self.assertEqual(forms.tabs["earlier/C1"], "sdlc")
        self.assertEqual(forms.tabs["earlier/C2"], "sdlc")
        self.assertEqual(forms.counts["sdlc"], 5)
        self.assertEqual(forms.counts["earlier"], 0)
        self.assertEqual(forms.counts["page-owner"], 0)
        self.assertEqual(set(forms.save_urls), set(forms.rows))
        reply = self.http_request(urljoin(path, forms.save_urls["earlier/C2"]),
                                  self.browser_fields(forms, "earlier/C2", "not works", "Needs a correction"))
        self.assertEqual(reply.status, 200, reply.body)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        row = next(row for row in change_review.review_rows(earlier) if row["#"] == "C2")
        self.assertEqual(row["Verdict"], "not works: Needs a correction")

    def test_rows_from_two_units_use_their_producer_ids_for_their_staff_tabs(self) -> None:
        first = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        second = fixture_member("showrunner", name="other", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(first.id))
        second_unit = fixture_member("unit", name="second-unit", parent=second.id)
        _ = change_review.append_rows(self.review_list,
                                      [change_review.Visible("First mixed change", "Read", "First proof"),
                                       change_review.Visible("Second mixed change", "Read", "Second proof")],
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[self.unit.id, second_unit.id], record=staff.snapshot())
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "sdlc")
        self.assertEqual(forms.tabs["page-owner/C5"], "other")

    def test_the_browser_response_does_not_wait_for_a_message_or_show_delivery_controls(self) -> None:
        path, forms, body = self.browser()
        del body
        entered, release = threading.Event(), threading.Event()

        def slow_send(recipient: str, sender: str, message: str) -> None:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("send was not released")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=slow_send):
            try:
                reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1"))
                self.assertEqual(reply.status, 200)
                self.assertEqual(json.loads(reply.body), {"answer": "works"})
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                pending = self.request(path, {}).body
                refreshed = BrowserForms()
                refreshed.feed(pending)
                self.assertNotIn("page-owner/C1", refreshed.rows)
                self.assertNotIn("Saved: works", pending)
                self.assert_no_delivery_controls(pending)
                self.assertIn('page-owner (<span data-count>2</span>)', pending)
            finally:
                release.set()
                if entered.is_set():
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        landed = self.request(path, {}).body
        self.assert_no_delivery_controls(landed)

    def test_a_failed_browser_save_returns_plain_words_and_accepts_the_same_typed_answer_again(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "not works", "Keep <my> words & outcome")
        for refusal in ("bad proposal /private/file.json", "feature_log: bad proposal /private/file.json"):
            with (self.subTest(refusal=refusal),
                  patch.object(feature_log, "drop_review", side_effect=feature_log.FeatureLogError(refusal))):
                failed = self.http_request(path, fields)
                self.assertEqual(failed.status, 503)
                self.assertEqual(json.loads(failed.body), {"reason": "The feature log could not be updated."})
                self.assertNotIn("/private/", failed.body)
                self.assertEqual(len(change_review.pending(self.review_list)), 3)
                self.assertEqual(self.messages[-1][2].splitlines()[1], "feature_log: bad proposal /private/file.json")
        self.delivery_done.clear()
        saved = self.http_request(path, fields)
        self.assertEqual(saved.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        rows = change_review.review_rows(self.review_list)
        self.assertEqual(next(row["Verdict"] for row in rows if row["#"] == "C1"), "not works: Keep <my> words & outcome")

        key = self.file_user_question("Choose a safe save")
        path, forms, _ = self.browser()
        fields = self.browser_fields(forms, key, "answer", "Keep my typed answer")
        with patch.object(update_registration, "answer", side_effect=OSError("private save unavailable")):
            failed = self.http_request(path, fields)
        self.assertEqual(failed.status, 503)
        self.assertIn("could not be saved", failed.body)
        self.assertIn(key, self.browser()[1].rows)
        self.delivery_done.clear()
        saved = self.http_request(path, fields)
        self.assertEqual(saved.status, 200, saved.body)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(json.loads(saved.body)["answer"], "Keep my typed answer")

    def test_a_later_browser_delivery_failure_leaves_the_row_saved_without_controls(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "not works", "Please fix"))
            self.assertEqual(reply.status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works: Please fix", content)
        self.assert_no_delivery_controls(content)
        refreshed = BrowserForms()
        refreshed.feed(self.request(path, {}).body)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assert_record_cleared()

        for failures in (1, 3):
            with self.subTest(user_question_failures=failures):
                key = self.file_user_question("Choose retry " + str(failures))
                path, forms, _ = self.browser()
                calls: list[str] = []
                remaining = failures

                def fail_question(recipient: str, sender: str, text: str) -> None:
                    nonlocal remaining
                    calls.append(text)
                    if remaining > 0 and "delivery needs attention" not in text:
                        remaining -= 1
                        raise RuntimeError("private sender unavailable")
                    self.capture_message(recipient, sender, text)

                self.delivery_done.clear()
                with patch.object(change_page, "send_message", side_effect=fail_question):
                    saved = self.http_request(path, self.browser_fields(forms, key, "answer", "Keep it"))
                    self.assertEqual(saved.status, 200, saved.body)
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(len(calls), 2 if failures == 1 else 4)
                self.assertEqual(sum("delivery needs attention" in text for text in calls), int(failures == 3))
                self.assertNotIn(key, self.browser()[1].rows)

    def test_the_browser_refuses_extra_fields_bad_tokens_and_unshown_rows(self) -> None:
        path, forms, body = self.browser()
        del body
        before = self.review_list.read_bytes()
        valid = self.browser_fields(forms, "page-owner/C1")
        for fields in (valid | {"path": str(self.review_list)}, valid | {"command": "true"},
                       valid | {"token": "é"}, valid | {"row": "page-owner/C999"}, valid | {"row": "../C1"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.http_request(path, fields).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

        key = self.file_user_question("Choose elsewhere")
        path, forms, _ = self.browser()
        owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
        _ = update_registration.answer(read_production(owner, staff.snapshot()), "Choose elsewhere")
        count = len(self.messages)
        stale = self.http_request(path, self.browser_fields(forms, key, "answer", "Too late"))
        self.assertEqual(stale.status, 400)
        self.assertIn("no longer open", stale.body)
        self.assertEqual(len(self.messages), count)

    def test_the_browser_uses_the_existing_pin_and_move_order_without_publishing_next_pages(self) -> None:
        _ = next_item.move_to_end(next_item.CHANGE, self.production, key="C1")
        queue = next_item.change_queue(self.production)
        urgency.pin(queue.name, "C3")
        path, forms, body = self.browser()
        self.assertEqual(list(forms.rows), ["page-owner/C3", "page-owner/C2", "page-owner/C1"])
        before = set(self.root.iterdir())
        self.assertEqual(self.request(path, {}).status, 200)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIn('data-next hidden>Skip for now', body)

    def test_empty_lists_keep_their_zero_count_tabs(self) -> None:
        for row in ("C1", "C2", "C3"):
            _ = change_review.record_verdict(self.review_list, row, "works", date(2026, 11, 2), self.production)
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.rows, {})
        self.assertIn('page-owner (<span data-count>0</span>)', body)

    def test_validation_requests_leave_the_browser_queue_after_delivery_and_a_fresh_prepare(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Check the proof"))
        self.assertEqual(reply.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        browser = change_browser.page_at(self.root, path)
        status = change_page.DeliveryStatus(self.root)
        prepared = change_browser.prepare(browser, status)
        self.assertEqual([item.key for item in prepared.changes if item.waiting], ["page-owner/C2", "page-owner/C3"])
        self.assertEqual([entry.key for entry in prepared.queues["page-owner"].entries], ["C2", "C3"])
        self.assertFalse(any(row.get("Verdict") for row in change_review.review_rows(self.review_list)))
        fresh = BrowserForms()
        content = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        fresh.feed(content)
        self.assertEqual(fresh.counts["page-owner"], 2)
        self.assertFalse(fresh.pending["page-owner/C1"])
        self.assertEqual(fresh.outcomes["page-owner/C1"], [])
        self.assertNotIn("page-owner/C1", fresh.save_urls)
        self.assertIn("The showrunner will validate this", content)
        self.assertIn("C1: the user asks you to validate this", self.messages[0][2])
        queue = next_item.change_queue(self.production)
        self.assertNotIn("C1", [entry.key for entry in urgency.queue_order(queue.entries, queue.name)])

    def test_removing_a_validation_record_restores_waiting_until_a_verdict_saves(self) -> None:
        path, forms, body = self.browser()
        del body
        status = change_page.DeliveryStatus(self.root)
        browser = change_browser.page_at(self.root, path)
        delivery, answer = change_browser.submit(browser, urlencode(
            self.browser_fields(forms, "page-owner/C1", "validate")), status)
        self.assertEqual(answer, "")
        if isinstance(delivery, change_page.NoMessageDelivery):
            self.fail("the validation request must create a delivery attempt")
        status.record(delivery)
        self.assertNotIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        status.path(delivery.page, "C1").unlink()
        self.assertIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        _ = change_review.record_verdict(self.review_list, "C1", "works", date(2026, 11, 2), self.production)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        refreshed = BrowserForms()
        refreshed.feed(change_browser.render(browser, status).decode())
        self.assertEqual(refreshed.counts["page-owner"], 2)

    def test_unsent_validation_records_leave_the_waiting_count_without_delivery_controls(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                _ = status.path(self.page, "C1").write_text(json.dumps({"validation": {
                    "attempt": "old", "state": state, "feedback": "Keep these words"}}))
                prepared = change_browser.prepare(change_browser.page_at(self.root, path), status)
                self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
                self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
                content = self.request(path, {}).body
                refreshed = BrowserForms()
                refreshed.feed(content)
                self.assertEqual(refreshed.counts["page-owner"], 2)
                self.assertFalse(refreshed.pending["page-owner/C1"])
                self.assertIn("The showrunner will validate this", content)
                self.assert_no_delivery_controls(content)

    def test_failed_validation_delivery_stays_out_of_the_count_without_answer_buttons(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, self.browser_fields(
                forms, "page-owner/C1", "validate", "Keep <these> words")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertEqual(footer_counts.changes(self.production), refreshed.counts["page-owner"])
        self.assertIn("The showrunner will validate this", content)
        self.assert_no_delivery_controls(content)

    def test_a_failed_validation_save_keeps_the_waiting_count(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(next_item, "move_to_end", side_effect=OSError("queue unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Keep this answer"))
        self.assertEqual(reply.status, 503)
        refreshed = BrowserForms()
        refreshed.feed(self.request(path, {}).body)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertTrue(refreshed.pending["page-owner/C1"])

    def test_browser_feedback_removes_the_count_only_after_its_verdict_saves(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "feedback", "Needs a clearer proof"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.body), {"answer": "Needs a clearer proof"})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        row = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(row["Verdict"], "Needs a clearer proof")
        self.assertFalse(change_review.lands(row["Verdict"]))
        refreshed = BrowserForms()
        refreshed.feed(self.request(path, {}).body)
        self.assertEqual(refreshed.counts["page-owner"], 2)

    def test_an_open_question_leaves_waiting_rows_counts_and_next_after_a_fresh_prepare(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "What does <this> change?"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.body), {"answer": ""})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertEqual([item.key for item in prepared.changes if item.waiting], ["page-owner/C2", "page-owner/C3"])
        self.assertEqual([entry.key for entry in prepared.queues["page-owner"].entries], ["C2", "C3"])
        refreshed = BrowserForms()
        content = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertIn('<h2>Question</h2><p>What does &lt;this&gt; change?</p>', content)
        self.assertIn("The showrunner will answer this", content)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        message = self.messages[0][2]
        for text in ("C1: the user asks a question", f"List: {self.review_list}", "Production: page-owner",
                     "Change: Numbered moves", "Question: What does <this> change?"):
            self.assertIn(text, message)
        question = change_page.DeliveryStatus(self.root).slots(self.page, "C1").question
        if not isinstance(question, change_page.OpenQuestion):
            self.fail("the sent question must remain open")
        self.assertIn(f"Question id: {question.question_id}", message.splitlines())
        self.assertFalse(any(row.get("Verdict") for row in change_review.review_rows(self.review_list)))

    def test_answer_question_restores_the_row_and_shows_the_answer_before_another_question_or_verdict(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        result = self.answer_question("The <numbered> groups.")
        self.assertEqual(result.returncode, 0, result.stderr)
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertEqual(refreshed.outcomes["page-owner/C1"], ["works", "not works", "validate", "question"])
        start = content.index('data-key="page-owner/C1"')
        section = content[start:content.index('</section>', start)]
        self.assertIn('<h2>Question</h2><p>Which groups?</p><h2>Answer</h2><p>The &lt;numbered&gt; groups.</p>', section)
        self.assertLess(section.index('<h1>Numbered moves</h1>'), section.index('<h2>Question</h2>'))
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "question", "Does it include nested groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(self.answer_question("Yes, it includes nested groups.").returncode, 0)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "works")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(next(row["Verdict"] for row in change_review.review_rows(self.review_list) if row["#"] == "C1"), "works")

    @process_boundary
    def test_answer_question_refuses_missing_questions_and_changes_nothing_on_an_identical_rerun(self) -> None:
        result = self.answer_question("There is no question.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no open question", result.stderr)
        status = change_page.DeliveryStatus(self.root)
        _ = status.ask(self.page, "C1", "Which groups?")
        self.assertEqual(self.answer_question("All groups.").returncode, 0)
        record = status.path(self.page, "C1")
        before = (record.read_bytes(), record.stat().st_mtime_ns, self.review_list.read_bytes())
        result = self.answer_question("All groups.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns, self.review_list.read_bytes()), before)
        self.assertEqual(self.answer_question("Some groups.").returncode, 1)
        self.assertEqual(record.read_bytes(), before[0])

    def test_question_posts_refuse_empty_and_blank_text_without_saving_or_sending(self) -> None:
        path, forms, body = self.browser()
        del body
        before = self.review_list.read_bytes()
        for text in ("", "   ", "\n\n", " \r\n \u2028"):
            with self.subTest(text=text):
                reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", text))
                self.assertEqual(reply.status, 400)
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "feedback", "\n \u2028"))
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assert_record_cleared()

    def test_feedback_textareas_use_one_live_row_setting_per_page_render(self) -> None:
        settings = self.temporary / "config/review_links.conf"
        original = settings.read_text()
        browser = change_page.ReviewBrowser("test")
        status = change_page.DeliveryStatus(self.root)
        renderers: tuple[Callable[[], bytes], ...] = (
            lambda: change_page.render(self.page, status),
            lambda: change_browser.render(browser, status),
            lambda: change_page.fallback_answer(self.page, change_page.AnswerNotice("C1", "Try again")),
        )
        for height in (5, 8):
            _ = settings.write_text(original.replace("feedback_rows=5", f"feedback_rows={height}"))
            for render in renderers:
                with self.subTest(height=height, render=render), patch.object(conf, "read", wraps=conf.read) as read:
                    content = render().decode()
                    self.assertIn(f'<textarea id="feedback-C1" name="feedback" rows="{height}">\n</textarea>', content)
                    self.assertIn('label,textarea{display:block}', content)
                    self.assertIn('resize:vertical', content)
                    self.assertEqual(sum(call.args == ("review_links",) for call in read.call_args_list), 1)

    def test_a_missing_feedback_rows_setting_refuses_the_render(self) -> None:
        settings = self.temporary / "config/review_links.conf"
        _ = settings.write_text(settings.read_text().replace("feedback_rows=5", ""))
        with self.assertRaisesRegex(conf.Unusable, "feedback_rows.*key is missing"):
            _ = change_page.render(self.page, change_page.DeliveryStatus(self.root))

    def test_multiline_browser_answers_preserve_messages_and_repeat_without_changes(self) -> None:
        path, forms, body = self.browser()
        del body
        original = self.review_list.read_bytes()
        status = change_page.DeliveryStatus(self.root)
        record = status.path(self.page, "C1")
        words = "\n First <line> \r\n\r Second & line\u2028Third\n"
        normalized = "\n First <line> \n\n Second & line\nThird"
        joined = "First <line> Second & line Third"
        for outcome in ("works", "not works", "feedback", "validate", "question"):
            with self.subTest(outcome=outcome):
                _ = self.review_list.write_bytes(original)
                record.unlink(missing_ok=True)
                self.messages.clear()
                fields = self.browser_fields(forms, "page-owner/C1", outcome, words)
                self.delivery_done.clear()
                reply = self.http_request(path, fields)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                verdict = next(row.get("Verdict", "") for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
                expected = f"{outcome}: {joined}" if outcome in ("works", "not works") else joined if outcome == "feedback" else ""
                self.assertEqual(verdict, expected)
                self.assertEqual(json.loads(reply.body), {"answer": expected})
                message = next(message for recipient, _, message in self.messages if recipient == "page-owner")
                self.assertIn(("Question: " if outcome == "question" else "User feedback: ") + normalized, message)
                slots = status.slots(self.page, "C1")
                if outcome == "validate":
                    assert isinstance(slots.validation, change_page.ValidationRequested)
                    self.assertEqual(slots.validation, change_page.ValidationRequested(slots.validation.attempt, normalized))
                if outcome == "question":
                    self.assertIsInstance(slots.question, change_page.OpenQuestion)
                    assert isinstance(slots.question, change_page.OpenQuestion)
                    self.assertEqual(slots.question.text, normalized)
                    self.assertIn('<p>\n First &lt;line&gt; \n\n Second &amp; line\nThird</p>',
                                  self.http_request(path, {}).body)
                saved = self.review_list.read_bytes()
                delivery = record.read_bytes() if record.exists() else b""
                messages = list(self.messages)
                self.delivery_done.clear()
                self.assertEqual(self.http_request(path, fields).status, 200)
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(self.review_list.read_bytes(), saved)
                self.assertEqual(record.read_bytes() if record.exists() else b"", delivery)
                self.assertEqual(self.messages, messages)

    def test_a_failed_multiline_save_refills_the_textarea_with_its_leading_blank_line(self) -> None:
        words = "\nFirst <line>\n\nSecond & line"
        before = self.review_list.read_bytes()
        with patch.object(change_page, "shown_rows", side_effect=ValueError("render failed")):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIn('rows="5">\n\nFirst &lt;line&gt;\n\nSecond &amp; line</textarea>', reply.body)
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback=parser.feedback["C1"])).status, 303)
        row = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(row["Verdict"], "not works: First <line> Second & line")

    def test_question_sends_after_the_response_and_repeated_posts_do_not_write_or_send_twice(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        deliver = change_page.deliver

        def hold_delivery(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery, status: change_page.DeliveryStatus) -> None:
            if isinstance(delivery, change_page.NoMessageDelivery):
                return
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("the question delivery was not released")
            try:
                deliver(delivery, status)
            finally:
                finished.set()

        with patch.object(change_page, "deliver", side_effect=hold_delivery):
            try:
                self.assertEqual(self.http_request(path, fields).status, 200)
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                record = change_page.DeliveryStatus(self.root).path(self.page, "C1")
                pending = (record.read_bytes(), record.stat().st_mtime_ns)
                self.assertEqual(self.http_request(path, fields).status, 200)
                self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
                self.assertEqual(self.messages, [])
            finally:
                release.set()
                self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
        sent = (record.read_bytes(), record.stat().st_mtime_ns)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, fields).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), sent)
        self.assertEqual(len(self.messages), 1)

    def test_a_failed_question_delivery_leaves_the_count_and_preserves_the_question(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, self.browser_fields(
                forms, "page-owner/C1", "question", "Which <groups>?")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        self.assertIn("Which &lt;groups&gt;?", content)
        self.assertIn("The showrunner will answer this", content)
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertEqual(footer_counts.changes(self.production), refreshed.counts["page-owner"])
        self.assert_no_delivery_controls(content)

    def test_a_failed_question_save_keeps_the_row_waiting(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page.DeliveryStatus, "write_slots", side_effect=OSError("record unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?"))
        self.assertEqual(reply.status, 503)
        refreshed = BrowserForms()
        refreshed.feed(self.request(path, {}).body)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertTrue(refreshed.pending["page-owner/C1"])
        self.assertEqual(self.messages, [])

    def test_an_old_answer_cannot_answer_a_new_question(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        first = status.ask(self.page, "C1", "Which groups?")
        if not isinstance(first, change_page.QuestionDelivery):
            self.fail("a new question must create a message attempt")
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = status.path(self.page, "C1")
        answered = (record.read_bytes(), record.stat().st_mtime_ns)
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), answered)
        self.assertEqual(self.answer_question("Some groups.", question_id=first.question_id).returncode, 1)
        second = status.ask(self.page, "C1", "Does it include nested groups?")
        if not isinstance(second, change_page.QuestionDelivery):
            self.fail("an answered row must accept a new question")
        self.assertNotEqual(first.question_id, second.question_id)
        pending = (record.read_bytes(), record.stat().st_mtime_ns)
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("question id", result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
        question = status.slots(self.page, "C1").question
        if not isinstance(question, change_page.OpenQuestion):
            self.fail("the second question must remain open")
        self.assertEqual(question.question_id, second.question_id)
        self.assertEqual(question.text, "Does it include nested groups?")

    def test_a_stale_question_resend_cannot_reopen_an_answered_question(self) -> None:
        path, forms, body = self.browser()
        del body
        status = change_page.DeliveryStatus(self.root)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        record = status.path(self.page, "C1")
        sent = (record.read_bytes(), record.stat().st_mtime_ns)
        retry = self.browser_fields(forms, "page-owner/C1", "retry")
        self.assertEqual(self.http_request(path, retry).status, 400)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), sent)
        self.assertEqual(self.answer_question("All groups.").returncode, 0)
        answered = (record.read_bytes(), record.stat().st_mtime_ns)
        reply = self.http_request(path, retry)
        self.assertEqual(reply.status, 400)
        self.assertEqual(json.loads(reply.body), {"reason": "Check the answer and submit it again."})
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), answered)
        self.assertEqual(len(self.messages), 1)

    def test_a_pending_question_leaves_the_count_and_repeated_saves_change_nothing(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        first = status.ask(self.page, "C1", "Which groups?")
        self.assertIsInstance(first, change_page.QuestionDelivery)
        record = status.path(self.page, "C1")
        pending = (record.read_bytes(), record.stat().st_mtime_ns)
        self.assertIsInstance(status.ask(self.page, "C1", "Which groups?"), change_page.NoDelivery)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertNotIn("C1", [entry.key for entry in next_item.change_queue(self.production).entries])
        self.assert_no_delivery_controls(content)

    def test_a_fresh_question_post_cannot_retry_a_failed_question(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, fields).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        record = change_page.DeliveryStatus(self.root).path(self.page, "C1")
        failed = (record.read_bytes(), record.stat().st_mtime_ns)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, fields).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), failed)
        self.assertEqual(self.messages, [])

    def test_a_server_restart_delivers_a_pending_question_once(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        self.delivery_done.clear()
        with serve.ReviewLinksServer(("127.0.0.1", 0), self.root):
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        saved = status.slots(self.page, "C1").question
        self.assertEqual(saved, change_page.OpenQuestion(question.question_id, "Which groups?", change_page.NoDelivery()))
        self.assertIn("Question id: " + question.question_id, self.messages[0][2])
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)

        key = self.file_user_question("Keep the spacing")
        browser = change_page.ReviewBrowser("private-recovery")
        status = change_page.DeliveryStatus(self.root)
        fields = {"row": key, "token": change_browser.row_token(browser, key), "outcome": "answer", "feedback": "Keep it"}
        delivery, answer = change_browser.submit(browser, urlencode(fields), status)
        self.assertEqual(answer, "Keep it")
        self.assertIsInstance(delivery, change_page.MessageDelivery)
        recovered = change_page.DeliveryStatus(self.root)
        before = len(self.messages)
        change_page.recover_pending(recovered)
        self.assertEqual(len(self.messages), before + 1)
        self.assertIn("Keep the spacing", self.messages[-1][2])
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), before + 1)

        with self.subTest(answer="save error before another close"):
            key = self.file_user_question("Do not send a failed answer")
            pending_status = change_page.DeliveryStatus(self.root)
            question = next(item for item in change_browser.prepare(browser, pending_status).questions if item.key == key)
            with patch.object(update_registration, "answer", side_effect=OSError("answer disk unavailable")):
                with self.assertRaisesRegex(OSError, "answer disk unavailable"):
                    _ = change_browser.submit_question(question, "Failed answer", pending_status)
            page = change_page.ReviewPage(question.record.path, self.production, (question.record.path.stem,), "")
            self.assertIsInstance(pending_status.slots(page, page.rows[0]).verdict, change_page.NoDelivery)
            self.assertEqual(pending_status.request_attempts, {})
            update_registration.write_question_record(replace(question.record, status="closed"))
            count = len(self.messages)
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count)

        with self.subTest(answer="closed with unknown save state"):
            key = self.file_user_question("Keep an uncertain answer private")
            pending_status = change_page.DeliveryStatus(self.root)
            question = next(item for item in change_browser.prepare(browser, pending_status).questions if item.key == key)
            delivery, _ = change_browser.submit_question(question, "Unconfirmed answer", pending_status)
            path = pending_status.path(delivery.page, delivery.row)
            saved = cast(change_page.StoredDeliverySlots, json.loads(path.read_text()))
            saved_answer = saved.get("user_answer")
            assert saved_answer is not None
            saved_answer["saved"] = False
            _ = path.write_text(json.dumps(saved))
            count = len(self.messages)
            restarted = change_page.DeliveryStatus(self.root)
            with redirect_stderr(StringIO()) as errors:
                change_page.recover_question_answers(restarted)
            self.assertEqual(errors.getvalue(), f"review-links recovery {path.name}: answer save state unknown; not sent\n")
            self.assertEqual(len(self.messages), count)
            self.assertIsInstance(restarted.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)
            self.assertEqual(restarted.request_attempts, {})

    def test_browser_markup_supplies_waiting_rows_and_navigation_after_saves(self) -> None:
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.pending, dict.fromkeys(forms.rows, True))
        self.assertEqual(forms.outcomes["page-owner/C1"], ["works", "not works", "validate", "question"])
        with self.subTest(button="negative verdict label"):
            self.assertIn('name="outcome" value="not works">Does not work</button>', body)
            self.assertNotIn('>Not works</button>', body)
        self.assertIn('data-next hidden>Skip for now', body)
        self.assertNotIn('data-left', body)
        self.assertIn('<p hidden data-empty></p>', body)
        self.assertIn("empty.hidden=rebuildMode || current!==null", body)
        self.assertIn("No questions for this showrunner.", body)
        self.assertIn("No questions to answer.", body)
        self.assertIn("No changes for this showrunner.", body)
        self.assertIn("No changes to review.", body)
        self.assertIn("current===row && tab===row.dataset.tab && navigation===position", body)
        self.assertIn("row.dataset.pending='false';form.hidden=true", body)
        self.assertIn("go(following?.dataset.key || '')", body)
        self.assertNotIn("rows.push(row)", body)
        with self.subTest(controls="labels and action groups"):
            self.assertIn('<p>Showrunner</p><nav', body)
            self.assertIn('>Comment or question</label>', body)
            self.assertIn('value="validate">Ask showrunner to check</button>', body)
            self.assertIn('<div class="secondary-actions"><button name="outcome" value="validate"', body)
            self.assertNotIn('Send feedback', body)
            self.assertNotIn('value="feedback"', body)
        with self.subTest(card="title and metadata"):
            card = body.split('<section hidden data-type="changes"', 1)[1].split('</section>', 1)[0]
            title, metadata = card.split('<details><summary>Details</summary>', 1)
            details, proof = metadata.split('</details>', 1)
            self.assertIn('<h1>Numbered moves</h1>', title)
            self.assertNotIn('class="kind"', title)
            self.assertIn('page-owner/C1', details)
            self.assertIn('2026-11-01', details)
            self.assertIn(self.git('rev-parse', 'HEAD'), details)
            self.assertIn('See in action', details)
            self.assertIn('<h2>How to check</h2>', proof)
            self.assertNotIn('<details open', card)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_skip_save_and_empty_states_in_the_browser(self) -> None:
        _ = self.file_user_question("First question")
        _ = self.file_user_question("Second question")
        _, _, body = self.browser()
        probe = r'''
<script>
(async()=>{
 const failures=[], check=(value,label)=>{if(!value) failures.push(label);};
 const realFetch=window.fetch;
 try{
  const navigate=(type,runner='page-owner')=>{
   history.replaceState(null,'','#'+new URLSearchParams({type,showrunner:runner}));show();
  };
  navigate('changes');
  const first=current, pending=waiting().length;
  next.click();
  check(current!==first && waiting().length===pending && first.dataset.pending==='true','skip keeps the change open');
  for(const row of rows.filter(r=>r.dataset.type==='changes')) row.dataset.pending=String(row===first);
  go(first.dataset.key);check(next.hidden,'one waiting change hides skip');
  first.dataset.pending='false';
  const other=rows.find(r=>r.dataset.type==='changes' && r!==first);other.dataset.pending='true';show();
  check(!next.hidden,'a saved card can skip to another waiting change');
  for(const row of rows) row.dataset.pending='true';
  for(const [type,outcome,comment] of [
   ['changes','works',''],['changes','works','Clear numbers'],['changes','not works','Still small'],
   ['changes','validate','Check the number'],['changes','question','Which number?'],['questions','answer','Use compact']]){
   for(const row of rows){row.dataset.pending='true';const form=row.querySelector('form');if(form) form.hidden=false;}
   navigate(type);const row=current,form=row.querySelector('form'),field=form.querySelector('textarea');field.value=comment;
   let sent;
   window.fetch=async(url,request)=>{sent=request.body;return {ok:true,json:async()=>({answer:outcome})};};
   await save(row,form,outcome);
   check(sent.get('outcome')===outcome && sent.get('feedback')===comment,type+' '+outcome+' sends the shared field');
   check(current!==row && row.dataset.pending==='false' && form.hidden,type+' '+outcome+' advances after saving');
   check(!saveNotice.hidden && saveNotice.textContent===(type==='questions' ? 'Answer saved.' : 'Review saved.'),type+' save notice');
   next.click();check(saveNotice.hidden,'skip clears the previous save notice');
  }
  for(const row of rows){row.dataset.pending='true';const form=row.querySelector('form');if(form) form.hidden=false;}
  navigate('changes');const row=current,form=row.querySelector('form'),field=form.querySelector('textarea');
  field.value='Keep <these> words';
  window.fetch=async()=>({ok:false,json:async()=>({reason:'Fixture save failed'})});
  await save(row,form,'not works');
  check(current===row && row.dataset.pending==='true' && !form.hidden,'failed save keeps the card open');
  check(field.value==='Keep <these> words','failed save keeps the text');
  check(row.querySelector('[data-save-state]').textContent==='Not saved: Fixture save failed','failed save shows the reason');
  check(saveNotice.hidden,'failed save hides the previous success notice');
  for(const type of ['questions','changes']){
   navigate(type,'demo');check(!empty.hidden && empty.textContent==='No '+type+' for this showrunner.',type+' empty group');
   check(next.hidden,'empty group hides skip');
   for(const row of rows.filter(r=>r.dataset.type===type)) row.dataset.pending='false';
   navigate(type);check(!empty.hidden && empty.textContent===(type==='questions' ? 'No questions to answer.' : 'No changes to review.'),type+' empty type');
  }
  navigate('questions');
  const question=rows.find(r=>r.dataset.type==='questions');question.dataset.pending='true';go(question.dataset.key);
  check(!question.querySelector('.kind') && question.firstElementChild.tagName==='H1','question title replaces the Question label');
  check(question.textContent.includes('Asked by builder-unit'),'question keeps the asker');
  check(question.querySelector('label').textContent==='Answer','question keeps the Answer label');
  check(getComputedStyle(question.querySelector('h1')).fontSize==='20px','card title size');
  navigate('changes');first.dataset.pending='true';go(first.dataset.key);
  check(parseFloat(getComputedStyle(first.querySelector('h2')).fontSize)<20,'section labels are smaller than the title');
  const title=first.querySelector('h1'),details=first.querySelector('details');
  check(!details.open && details.getBoundingClientRect().top-title.getBoundingClientRect().bottom>=15.9,'closed metadata has space after the title');
  navigate('rebuilds');check(showrunners.hidden && document.querySelector('.navigation').hidden,'rebuild controls stay hidden');
 }catch(error){failures.push(error.stack || String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        content = body.replace('</body>', probe + '</body>').encode()
        results: list[list[str]] = []

        class BrowserProbe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                _ = self.wfile.write(content)

            def do_POST(self) -> None:
                results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(204)
                self.end_headers()

        with ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                profile = self.temporary / "browser-profile"
                profile.mkdir()
                run = subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                      "--window-size", "390,1200", "--screenshot", str(self.temporary / "browser.png"),
                                      f"http://127.0.0.1:{server.server_port}/"], capture_output=True, text=True, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(results, [[]], "The browser must complete every check without a failure.")
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)

    def test_submit_writes_through_verdict_and_a_fresh_get_reads_the_saved_record(self) -> None:
        reply = self.request(self.page_path, self.answer())
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        rows = {row["#"]: row for row in change_review.review_rows(self.review_list)}
        self.assertEqual(rows["C1"]["Verdict"], "works")
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])
        reloaded = self.request(self.page_path, {})
        self.assertEqual(reloaded.status, 200)
        self.assertIn("Saved: works", reloaded.body)
        self.assertIn("Each group starts at 1", reloaded.body)
        self.assertIn("Read only", reloaded.body)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 landed", self.messages[0][2])
        folder = self.root / self.page_path.split("/")[1]
        self.assertNotIn("works", (folder / change_page.MANIFEST).read_text())
        self.assertNotIn("Saved:", (folder / change_page.PAGE_NAME).read_text())
        self.assertEqual(self.request(self.page_path.rsplit("/", 1)[0] + "/" + change_page.MANIFEST, {}).status, 404)

    def test_only_the_rows_shown_on_this_page_can_be_answered(self) -> None:
        before = self.review_list.read_bytes()
        for row in ("C2", "C999", "../C1"):
            with self.subTest(row=row):
                self.assertEqual(self.request(self.page_path, self.answer(row)).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_validate_yourself_keeps_the_verdict_unset_and_leaves_the_next_order(self) -> None:
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        proposal = feature_log.state_dir() / "proposals/page-owner/C1.json"
        before_proposal = proposal.read_bytes()
        before = self.review_list.read_bytes()
        initial = self.request(self.page_path, {}).body
        self.assertEqual(initial.count('value="validate">Ask showrunner to check'), len(self.page.rows))
        words = "Please check this yourself"
        reply = self.request(self.page_path, self.answer(outcome="validate", feedback=words))
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(proposal.read_bytes(), before_proposal)
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C1", "C2", "C3"])
        queue = next_item.change_queue(self.production)
        self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries, queue.name)], ["C2", "C3"])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1: the user asks you to validate this", self.messages[0][2])
        self.assertIn(words, self.messages[0][2])
        page = self.request(self.page_path, {}).body
        self.assertNotIn("changes left", page)
        self.assertIn('value="next">Skip for now', page)
        self.assertNotIn("Saved:", page)
        self.assert_no_delivery_controls(page)
        following = self.request(self.page_path, {"outcome": "next", "token": self.page.token})
        self.assertEqual(following.status, 303)
        self.assertEqual(change_page.page_at(self.root, urlsplit(following.location).path).rows, ("C2",))

    def test_a_refused_validation_request_does_not_wait_for_delivery(self) -> None:
        with (patch.object(self, "http_request", return_value=Reply(400, "refused", "")),
              patch.object(threading.Event, "wait", side_effect=AssertionError("delivery did not start")) as wait):
            with self.assertRaisesRegex(AssertionError, "400 != 303"):
                self.test_a_failed_validation_message_alerts_with_the_saved_feedback()
            wait.assert_not_called()

    def test_a_failed_validation_message_alerts_with_the_saved_feedback(self) -> None:
        words = "Check <this> & keep the proof"
        sent: list[str] = []
        before = self.review_list.read_bytes()
        started, release = threading.Event(), threading.Event()
        send = self.fail_all_but_alerts(sent)
        def paused_send(recipient: str, sender: str, message: str) -> None:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test did not release delivery")
            send(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=paused_send):
            try:
                reply = self.http_request(self.page_path, self.answer(outcome="validate", feedback=words))
                self.assertEqual(reply.status, 303)
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                body = self.http_request(self.page_path, {}).body
                self.assertNotIn(str(self.temporary), body)
                self.assertNotIn("Saved:", body)
            finally:
                release.set()
                if started.is_set():
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(len(sent), 4)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn(words, self.messages[0][2])
        self.assertIn("Tries: 3", self.messages[0][2])
        content = self.request(self.page_path, {}).body
        self.assertIn("The showrunner will validate this", content)
        self.assert_no_delivery_controls(content)

    def test_validate_yourself_refuses_a_saved_row_without_moving_it(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        before = self.review_list.read_bytes()
        queue = next_item.change_queue(self.production)
        self.assertEqual(self.request(self.page_path, self.answer(outcome="validate")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(urgency.ended(queue.name), {})
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])

    def test_validate_yourself_moves_the_qualified_row_from_an_earlier_list(self) -> None:
        earlier = self.temporary / "change-review - demo.md"
        demo = fixture_member("showrunner", name="demo")
        _ = staff.set_doc(demo.id, staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(earlier, [change_review.Visible("Earlier change", "Run it", "Proof")],
                                      date(2026, 11, 1), "", self.production, producers=[demo.id], record=staff.snapshot())
        _ = change_review.set_row_kind(earlier, "C1", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, earlier, ["C1"])).path
        page = change_page.page_at(self.root, path)
        before = earlier.read_bytes()
        fields = dict(self.answer(outcome="validate"), token=page.token)
        self.assertEqual(self.request(path, fields).status, 303)
        self.assertEqual(earlier.read_bytes(), before)
        queue = next_item.change_queue(self.production)
        self.assertEqual(set(urgency.ended(queue.name)), {"demo/C1"})
        self.assertNotIn("demo/C1", [entry.key for entry in urgency.queue_order(queue.entries, queue.name)])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["demo"])
        self.assertNotIn("changes left", self.request(path, {}).body)

    def test_paths_commands_extra_fields_and_forged_page_tokens_are_refused(self) -> None:
        before = self.review_list.read_bytes()
        for fields in (dict(self.answer(), path=str(self.production)), dict(self.answer(), command="touch /tmp/no"),
                       dict(self.answer(), token="wrong"), dict(self.answer(), row=str(self.review_list))):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(self.page_path, fields).status, 400)
        self.assertEqual(self.request(self.page_path + "?path=/tmp/no", self.answer()).status, 400)
        self.assertEqual(self.request("/unpublished/changes.html", self.answer()).status, 404)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_write_path_refuses_a_non_tailnet_bind_and_a_lost_address(self) -> None:
        before = self.review_list.read_bytes()
        with patch.object(serve, "tailscale_ip", return_value="100.64.0.99"):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 403)
        with patch.object(serve, "tailscale_ip", side_effect=OSError("address lost")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 403)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_not_works_and_free_text_follow_the_stored_producer_route(self) -> None:
        for row, outcome, feedback, verdict in (("C1", "not works", "", "not works"),
                                               ("C2", "feedback", "works but café | <still tiny>",
                                                "works but café | <still tiny>")):
            with self.subTest(outcome=outcome):
                path = urlsplit(change_page.publish(self.production, self.review_list, [row])).path
                page = change_page.page_at(self.root, path)
                fields = dict(self.answer(row, outcome, feedback), token=page.token)
                self.assertEqual(self.request(path, fields).status, 303)
                self.assertEqual([recipient for recipient, _, _ in self.messages[-2:]],
                                 ["builder-unit", "page-owner"])
                for _, _, message in self.messages[-2:]:
                    self.assertIn(feedback or "not works", message)
                    self.assertIn("Numbered moves", message)
                    self.assertIn(str(self.review_list), message)
                saved = next(item for item in change_review.review_rows(self.review_list) if item["#"] == row)
                self.assertEqual(saved["Verdict"], verdict)
                self.assertIn("Saved: " + verdict.replace("<", "&lt;").replace(">", "&gt;"),
                              self.request(path, {}).body)

    def test_not_works_with_words_preserves_them_and_returns_to_the_unit(self) -> None:
        words = "I still see the internal ids"
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback=words)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        self.assertIn("C1 returns to its unit", self.messages[0][2])
        self.assertIn("User feedback: " + words, self.messages[0][2])
        self.assertIn("Saved: not works: " + words, self.request(self.page_path, {}).body)

    def test_a_change_outside_a_unit_returns_to_its_production_session(self) -> None:
        words = "The proof needs the actual test output"
        self.assertEqual(self.request(self.page_path, self.answer("C3", "feedback", words)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        # C3 has no matching trailer. Its merge names one unit, which receives the answer.
        self.messages.clear()
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Local docs", "Read", "Proof")],
                                      date(2026, 11, 1), "", self.production, record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "feedback", words), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

    def test_read_only_change_stays_visible_with_kind_change_proof_and_controls_after_reload(self) -> None:
        body = self.request(self.page_path, {}).body
        self.assertIn('<p class="kind">See in action</p>', body)
        self.assertIn('<p class="kind">Read only</p>', body)
        self.assertIn("Internal comments clarified", body)
        self.assertIn("The old and new comments agree with the tests", body)
        self.assertIn('id="feedback-C3"', body)
        self.assertIn('value="works">Works', body)
        self.assertEqual(self.request(self.page_path, self.answer("C3")).status, 303)
        reloaded = self.request(self.page_path, {}).body
        self.assertIn("Internal comments clarified", reloaded)
        self.assertIn("The old and new comments agree with the tests", reloaded)
        self.assertIn('<p class="kind">Read only</p>', reloaded)
        self.assertIn("Saved: works", reloaded)

    @process_boundary
    def test_command_publishes_one_row_and_a_batch_through_the_same_path(self) -> None:
        for rows in (["C1"], ["C1", "C2", "C3"]):
            with self.subTest(rows=rows):
                result = self.command(change_page, change_page.main,
                                      ["--production", "page-owner", "--list", str(self.review_list), *rows])
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                path = urlsplit(result.stdout.strip()).path
                reply = self.request(path, {})
                self.assertEqual(reply.status, 200)
                for row in ("C1", "C2", "C3"):
                    self.assertEqual(f'<section id="{row}">' in reply.body, row in rows)

    def test_delivery_failure_keeps_the_saved_answer_and_repeated_saves_send_nothing(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 303)
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], [])
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(len(self.messages), 0)

    def feature_command(self, *arguments: str) -> None:
        result = self.command(feature_log, feature_log.main, list(arguments))
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def note_arguments(self, project: str, title: str) -> list[str]:
        return ["--project", project, "--title", title, "--kind", "feature", "--summary", title,
                "--behavior", "Each group starts at 1", "--see", "Numbers start at 1",
                "--link", self.git("rev-parse", "HEAD"), "--evidence", "The page test passes",
                "--completed", "2026-11-01", *(["--crate", "hana"] if project == "hana" else [])]

    def proposal(self, review: str) -> dict[str, object]:
        path = feature_log.state_dir() / "proposals" / f"{review}.json"
        return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))

    def test_a_feature_log_project_saves_when_the_writer_skips_an_absent_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        with (patch.object(subprocess, "run", wraps=subprocess.run) as commands,
              patch.object(feature_log, "project_of", wraps=feature_log.project_of) as project,
              patch.object(feature_log, "land_review", wraps=feature_log.land_review) as land,
              patch.object(change_review, "save_verdict", wraps=change_review.save_verdict) as save):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        self.assertTrue(invocations)
        self.assertTrue(all(args[0] == "git" for args in invocations), invocations)
        project.assert_called_once_with(str(self.repo))
        land.assert_called_once_with("page-owner/C1", if_proposed=True)
        save.assert_called_once_with(self.review_list, "C1", "works", self.production)
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(saved["Verdict"], "works")
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 landed", self.messages[0][2])
        self.assertFalse((feature_log.state_dir() / "proposals/page-owner/C1.json").exists())

    def test_works_with_feedback_lands_the_proposal_before_closing_the_row(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer(feedback="The numbers are clear")).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "landed")
        self.assertTrue((feature_log.vault_dir() / "feature-log/claude/numbered moves.md").is_file())
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(saved["Verdict"], "works: The numbers are clear")
        self.assertTrue(change_review.lands(saved["Verdict"]))
        self.assertIn("C1 landed", self.messages[-1][2])
        self.assertIn("Saved: works: The numbers are clear", self.request(self.page_path, {}).body)

    def test_feedback_that_starts_with_dashes_drops_the_proposal_and_saves_the_words(self) -> None:
        _ = self.git("remote", "add", "origin", "git@github.com:natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer(outcome="feedback", feedback="--help")).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "dropped")
        self.assertEqual(self.proposal("page-owner/C1")["verdict"], "--help")
        self.assertIn("Saved: --help", self.request(self.page_path, {}).body)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])

    def test_a_landing_replacement_supersedes_the_old_note(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands")
        self.feature_command("add", *self.note_arguments("claude", "Old numbers"))
        self.feature_command("propose", "--review", "page-owner/C1", "--old", "Old numbers",
                             *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "landed")
        old = feature_log.parse_note(feature_log.vault_dir() / "feature-log/claude/old numbers.md")
        self.assertEqual((old["state"], old["replaced_by"]), ("superseded", ["numbered moves"]))

    def test_a_malformed_proposal_keeps_the_row_open_and_tells_the_showrunner(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        before = self.review_list.read_bytes()
        _ = (feature_log.state_dir() / "proposals/page-owner/C1.json").write_text("{", encoding="utf-8")
        reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 stays open", self.messages[0][2])
        self.assertIn("cannot read proposal", self.messages[0][2])

    def test_a_saved_answer_needs_no_delivery_control_or_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        (feature_log.state_dir() / "proposals/page-owner/C1.json").unlink()
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual([target for target, _, _ in self.messages], [])

    def test_other_projects_close_the_row_without_a_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/nixos.git")
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertFalse((feature_log.state_dir() / "proposals").exists())

    def test_the_project_policy_can_require_feature_log_closeout_before_saving(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/nixos.git")
        project = feature_log.PROJECTS["natedev"]._replace(closes_by_change_review=True)
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("natedev", "Numbered moves"))
        before = self.review_list.read_bytes()
        _ = (feature_log.state_dir() / "proposals/page-owner/C1.json").write_text("{", encoding="utf-8")
        with patch.dict(feature_log.PROJECTS, {"natedev": project}):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

    def test_the_project_policy_can_skip_the_feature_log_operation(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        project = feature_log.PROJECTS["claude"]._replace(closes_by_change_review=False)
        with patch.dict(feature_log.PROJECTS, {"claude": project}):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertFalse((feature_log.state_dir() / "proposals").exists())

    def test_the_hana_list_uses_a_bare_review_id(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/hana.git")
        review_list = self.review_list.with_name("change-review - hana.md")
        _ = self.review_list.rename(review_list)
        path = urlsplit(change_page.publish(self.production, review_list, ["C1"])).path
        page = change_page.page_at(self.root, path)
        self.feature_command("propose", "--review", "C1", *self.note_arguments("hana", "Numbered moves"))
        self.assertEqual(self.request(path, dict(self.answer(), token=page.token)).status, 303)
        self.assertEqual(self.proposal("C1")["status"], "landed")
        self.assertIn("Saved: works", self.request(path, {}).body)

    def test_a_producer_id_routes_feedback_to_its_unit(self) -> None:
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Direct checkpoint", "Run", "Proof")],
                                      date(2026, 11, 1), "abcdef12", self.production, producers=[self.unit.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "See in action", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works"), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])

    def test_a_child_page_routes_a_sibling_row_through_the_shared_root(self) -> None:
        notifier = self.temporary / "notifier"
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier))
        root = fixture_member("showrunner", name="root")
        child = self.temporary / "child-production.md"
        sibling = self.temporary / "sibling-production.md"
        root_doc = self.temporary / "root-production.md"
        _ = staff.set_doc(root.id, staff.LiveDoc(local_machine(), root_doc))
        for name, doc in (("root", root_doc), ("child", child), ("sibling", sibling)):

            _ = doc.write_text(self.production.read_text().replace("builder-unit", f"{doc.stem}-unit").replace(
                "fallback-owner", name))
            timer = notifier / f"showrunner-{name}"
            timer.mkdir(parents=True)
            _ = (timer / "conf").write_text(f"EVERY=15\nTARGET=session:test\nPROMPT_FILE={timer / 'prompt'}\n"
                                           + f"CHECK=zsh /unused/production_check.sh {name}\n")
            if doc != root_doc:
                _ = fixture_member("showrunner", name=name, parent=root.id, doc=staff.LiveDoc(local_machine(), doc))
        owner = staff.snapshot().by_path("page-owner")
        if owner is None:
            self.fail("the list showrunner must have a staff record")
        sibling_unit = fixture_member("unit", name="sibling-production-unit", parent=owner.id)
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Sibling change", "Run", "Proof")],
                                      date(2026, 11, 1), "abcdef12", sibling, producers=[sibling_unit.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", child)
        path = urlsplit(change_page.publish(child, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works"), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["sibling-production-unit", "page-owner"])

    def test_an_unknown_production_is_refused_before_saving(self) -> None:
        owner = fixture_member("showrunner", name="missing-owner",
                               doc=staff.LiveDoc(local_machine(), self.temporary / "missing-production.md"))
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Unknown owner", "Run", "Proof")],
                                      date(2026, 11, 1), "", self.production, producers=[owner.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(path, dict(self.answer("C4"), token=page.token)).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_a_saved_verdict_has_no_delivery_form_after_failure(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback="Still small")).status, 303)
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works: Still small", content)
        self.assert_no_delivery_controls(content)
        self.assertEqual(self.request(self.page_path, self.answer("C3", "retry")).status, 400)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_non_ascii_token_is_refused_without_changing_the_record(self) -> None:
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, dict(self.answer(), token="é")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_each_section_shows_its_change_text_once(self) -> None:
        body = self.request(self.page_path, {}).body
        self.assertEqual(body.count("Numbered moves"), 1)
        self.assertEqual(body.count("Internal comments clarified"), 1)

    def test_feedback_works_retries_on_the_server_and_alerts_only_the_showrunner(self) -> None:
        def report_delivery_failure(recipient: str, sender: str, message: str) -> None:
            self.capture_message(recipient, sender, message)
            raise RuntimeError("delivery receipt missing")
        with patch.object(change_page, "send_message", side_effect=report_delivery_failure), redirect_stderr(StringIO()) as error:
            self.assertEqual(self.request(self.page_path, self.answer(outcome="feedback", feedback="works")).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"] * 4)
        self.assertIn("Pass it on by hand", self.messages[-1][2])
        alerts = [line for line in error.getvalue().splitlines() if line.startswith("review-links delivery alert") ]
        self.assertEqual(len(alerts), 1)
        self.assertIn("delivery alert C1: delivery receipt missing", alerts[0])
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_failed_unit_delivery_uses_its_budget_and_alerts_the_showrunner_once(self) -> None:
        attempts: list[str] = []
        def fail_unit_delivery(recipient: str, sender: str, message: str) -> None:
            name = self.recipient_name(recipient)
            if "delivery needs attention" not in message:
                attempts.append(name)
            if name == "builder-unit":
                raise RuntimeError("unit socket unavailable")
            self.capture_message(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=fail_unit_delivery):
            reply = self.request(self.page_path, self.answer(outcome="feedback", feedback="Use a smaller title"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(attempts, ["builder-unit", "page-owner", "builder-unit", "builder-unit"])
        self.assertEqual(sum("delivery needs attention" in message for _, _, message in self.messages), 1)
        self.assertTrue(all("Use a smaller title" in message for _, _, message in self.messages))
        self.assertIn("Recipient: " + str(self.unit.id), self.messages[-1][2])
        self.assertIn("unit socket unavailable", self.messages[-1][2])
        self.assert_record_cleared()
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_the_showrunner_receives_its_first_attempt_before_the_unit_receives_its_second(self) -> None:
        events: list[str] = []

        def fail_the_unit_once(recipient: str, sender: str, message: str) -> None:
            name = self.recipient_name(recipient)
            events.append(name)
            if name == "builder-unit" and events.count(name) == 1:
                raise RuntimeError("unit session unavailable")
            self.capture_message(recipient, sender, message)

        def pause_round(seconds: float) -> None:
            self.assertEqual(seconds, 0)
            events.append("pause")

        with patch.object(change_page, "send_message", side_effect=fail_the_unit_once), patch.object(
                time, "sleep", side_effect=pause_round):
            reply = self.request(self.page_path, self.answer(outcome="not works"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(events, ["builder-unit", "page-owner", "pause", "builder-unit"])
        self.assert_record_cleared()

    def test_a_recipient_that_succeeds_in_the_first_round_receives_no_retry(self) -> None:
        attempts: list[str] = []

        def fail_the_showrunner_once(recipient: str, sender: str, message: str) -> None:
            name = self.recipient_name(recipient)
            attempts.append(name)
            if name == "page-owner" and attempts.count(name) == 1:
                raise RuntimeError("showrunner session unavailable")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=fail_the_showrunner_once), patch.object(time, "sleep") as pause:
            reply = self.request(self.page_path, self.answer(outcome="not works"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(attempts, ["builder-unit", "page-owner", "page-owner"])
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        pause.assert_called_once_with(0)
        self.assert_record_cleared()

    def test_a_failed_feature_log_save_keeps_the_form_and_hides_error_details(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        proposal = feature_log.state_dir() / "proposals/page-owner/C1.json"
        original = proposal.read_bytes()
        _ = proposal.write_text("{", encoding="utf-8")
        words = "Still <small> & 'awkward'"
        before = self.review_list.read_bytes()
        errors = StringIO()
        with redirect_stderr(errors):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 503)
        self.assertIn("<style>", reply.body)
        self.assertIn("<main>", reply.body)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertNotIn(str(self.temporary), reply.body)
        self.assertNotIn("Traceback", reply.body)
        self.assertNotIn("feature_log:", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIn(str(self.review_list), errors.getvalue())
        self.assertIn("cannot read proposal", errors.getvalue())
        self.assertIn("cannot read proposal", self.messages[0][2])
        self.assertIn(str(self.review_list), self.messages[0][2])
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        _ = proposal.write_bytes(original)
        self.assertEqual(self.request(self.page_path, self.answer(
            "C1", parser.selected_outcomes["C1"], parser.feedback["C1"])).status, 303)
        self.assertIn("Saved: not works: Still &lt;small&gt;", self.request(self.page_path, {}).body)

    def test_unknown_post_paths_use_the_review_page_style_without_error_details(self) -> None:
        reply = self.request("/missing/changes.html", self.answer())
        self.assertEqual(reply.status, 404)
        self.assertIn("<style>", reply.body)
        self.assertIn("<main><p>This review page is not available.</p></main>", reply.body)
        self.assertNotIn("Error response", reply.body)
        self.assertNotIn(str(self.root), reply.body)

    def test_the_response_and_saved_page_do_not_wait_for_a_slow_message(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_send(recipient: str, sender: str, message: str) -> None:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test message was not released")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=slow_send):
            try:
                reply = self.http_request(self.page_path, self.answer())
                self.assertEqual(reply.status, 303)
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                self.assertEqual(self.messages, [])
                page = self.http_request(self.page_path, {}).body
                self.assertIn("Saved: works", page)
                self.assert_no_delivery_controls(page)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)

    def test_a_later_message_failure_keeps_delivery_details_off_every_link(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("private failure")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        other_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1"])).path
        for path in (self.page_path, other_path):
            body = self.request(path, {}).body
            self.assertIn("Saved: works", body)
            self.assert_no_delivery_controls(body)
            self.assertNotIn("private failure", body)
            self.assertNotIn(str(self.temporary), body)
        self.assert_record_cleared()
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Keep private")
        self.assertTrue(status.path(self.page, "C1").exists())
        self.assertEqual(self.request("/" + status.path(self.page, "C1").name, {}).status, 404)

    def test_an_interruption_after_the_verdict_preserves_a_pending_message_for_server_recovery(self) -> None:
        save = change_review.save_verdict

        def interrupt_after_verdict(path: Path, row: str, verdict: str, production: Path) -> change_review.VerdictSaved | change_review.VerdictRefused:
            result = save(path, row, verdict, production)
            if isinstance(result, change_review.VerdictSaved):
                raise RuntimeError("interrupted after verdict")
            return result

        with patch.object(change_review, "save_verdict", side_effect=interrupt_after_verdict):
            reply = self.request(self.page_path, self.answer(feedback="Clear numbers"))
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.messages, [])
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works: Clear numbers", body)
        self.assert_no_delivery_controls(body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertIn("works: Clear numbers", self.messages[0][2])
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)

    def test_an_intent_write_failure_refuses_the_verdict_write(self) -> None:
        before = self.review_list.read_bytes()
        with patch.object(change_page.DeliveryStatus, "begin", side_effect=OSError("disk full")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertNotIn("Saved:", reply.body)

    def test_a_failed_or_interrupted_verdict_write_preserves_validation_ownership_and_feedback(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        self.assertEqual(self.request(self.page_path, self.answer(outcome="validate", feedback="Keep these words")).status, 303)
        validation = status.slots(self.page, "C1").validation
        before = self.review_list.read_bytes()
        for failure in (change_review.VerdictRefused([], "verdict write failed"),
                        change_review.ChangeReviewError("verdict write failed"), OSError("verdict write failed"),
                        RuntimeError("interrupted before verdict")):
            interruption = isinstance(failure, RuntimeError)
            with self.subTest(failure=failure):
                def fail_verdict(path: Path, row: str, verdict: str, production: Path) -> change_review.VerdictRefused:
                    del path, row, verdict, production
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch.object(change_review, "save_verdict", side_effect=fail_verdict):
                    reply = self.request(self.page_path, self.answer(feedback="New answer"))
                self.assertEqual(reply.status, 503 if interruption else 400)
                self.assertEqual(self.review_list.read_bytes(), before)
                self.assertEqual(status.slots(self.page, "C1").validation, validation)
                self.assertEqual(validation.feedback if isinstance(validation, change_page.ValidationRequested) else "", "Keep these words")
                self.assertIn("C1", [row["#"] for row in change_review.pending(self.review_list)])
                self.assertIsInstance(status.slots(self.page, "C1").verdict,
                                      change_page.DeliveryIntent if interruption else change_page.NoDelivery)
                body = self.request(self.page_path, {}).body
                self.assertIn("The showrunner will validate this", body)
                self.assertNotIn("Saved:", body)
                self.assert_no_delivery_controls(body)

    def test_an_interruption_after_saving_selects_the_verdict_slot_and_keeps_validation_feedback(self) -> None:
        words = "Check the small text"
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="validate", feedback=words)).status, 303)
        status = change_page.DeliveryStatus(self.root)
        validation = status.slots(self.page, "C1").validation
        save = change_review.save_verdict

        def interrupt_after_save(path: Path, row: str, verdict: str, production: Path) -> change_review.VerdictSaved | change_review.VerdictRefused:
            self.assertEqual(status.slots(self.page, "C1").validation, validation)
            self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.DeliveryIntent)
            result = save(path, row, verdict, production)
            if isinstance(result, change_review.VerdictSaved):
                raise RuntimeError("interrupted after verdict")
            return result

        with patch.object(change_review, "save_verdict", side_effect=interrupt_after_save):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 503)
        slots = status.slots(self.page, "C1")
        self.assertEqual(slots.validation, validation)
        self.assertEqual(status.slots(self.page, "C1").verdict, slots.verdict)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.DeliveryIntent)
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assert_no_delivery_controls(body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(status.slots(self.page, "C1").validation, validation)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        body = self.request(self.page_path, {}).body
        self.assert_no_delivery_controls(body)

    def test_completing_one_delivery_kind_preserves_the_other_slot(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        for first in ("validation", "verdict"):
            with self.subTest(first=first):
                validation = status.begin(self.page, "C1", "validation", "Keep these words")
                verdict = status.begin(self.page, "C1", "verdict")
                attempts = {"validation": validation, "verdict": verdict}
                other = "verdict" if first == "validation" else "validation"
                first_delivery = change_page.MessageDelivery(self.page, "C1", (), "", attempts[first])
                other_delivery = change_page.MessageDelivery(self.page, "C1", (), "", attempts[other])
                before = status.slots(self.page, "C1")
                remaining = before.verdict if other == "verdict" else before.validation
                status.record(first_delivery)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                status.record(first_delivery)
                status.record(first_delivery)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                if first == "validation":
                    self.assertEqual(after.validation, change_page.ValidationRequested(validation, "Keep these words"))
                else:
                    self.assertIsInstance(after.verdict, change_page.NoDelivery)
                stored = status.path(self.page, "C1").read_text(encoding="utf-8")
                self.assertEqual(f'"{first}"' in stored, first == "validation")
                self.assertIn(f'"{other}"', stored)
                status.record(other_delivery)
                self.assertEqual(status.slots(self.page, "C1").validation,
                                 change_page.ValidationRequested(validation, "Keep these words"))
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)

    def test_single_intent_records_migrate_to_their_kind_without_losing_feedback(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        kinds: tuple[Literal["validation", "verdict"], ...] = ("validation", "verdict")
        for kind in kinds:
            with self.subTest(kind=kind):
                _ = status.path(self.page, "C1").write_text(json.dumps({
                    "attempt": "old-attempt", "state": "pending", "kind": kind,
                    "feedback": "Keep these words" if kind == "validation" else "",
                }), encoding="utf-8")
                slots = status.slots(self.page, "C1")
                original = slots.validation if kind == "validation" else slots.verdict
                self.assertEqual(original, change_page.DeliveryIntent(
                    "old-attempt", kind,
                    "Keep these words" if kind == "validation" else ""))
                if kind == "validation":
                    _ = status.begin(self.page, "C1", "verdict")
                else:
                    _ = status.begin(self.page, "C1", "validation", "New words")
                migrated = status.slots(self.page, "C1")
                self.assertEqual(migrated.validation if kind == "validation" else migrated.verdict, original)

    def test_a_stopped_send_leaves_a_pending_record_without_delivery_controls(self) -> None:
        stopped = threading.Event()

        def stop_delivery(delivery: change_page.MessageDelivery, status: change_page.DeliveryStatus) -> None:
            del delivery, status
            stopped.set()
            raise OSError("server stopped")

        with patch.object(change_page, "deliver", side_effect=stop_delivery):
            self.assertEqual(self.http_request(self.page_path, self.answer()).status, 303)
            self.assertTrue(stopped.wait(TEST_HANG_GUARD_SECS))
        status = change_page.DeliveryStatus(self.root)
        intent = status.slots(self.page, "C1").verdict
        self.assertIsInstance(intent, change_page.DeliveryIntent)
        if not isinstance(intent, change_page.DeliveryIntent):
            self.fail("the delivery intent is missing")
        self.assertEqual((intent.kind, intent.feedback), ("verdict", ""))
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assert_no_delivery_controls(body)

    def test_an_older_success_cannot_clear_a_newer_validation_ownership_mark(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        older = status.begin(self.page, "C1", "validation", "old words")
        newer = status.begin(self.page, "C1", "validation", "new words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", older)
        new = change_page.MessageDelivery(self.page, "C1", (), "", newer)
        status.record(new)
        status.record(old)
        status.record(old)
        self.assertEqual(status.slots(self.page, "C1").validation,
                         change_page.ValidationRequested(newer, "new words"))
        body = self.request(self.page_path, {}).body
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertNotIn("C1", parser.validation_requests)
        self.assertNotIn("The validation message did not send", body)

    def test_attempt_comparison_and_update_hold_the_lock_against_a_new_attempt(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "validation", "old words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", attempt)
        checked = threading.Event()
        release = threading.Event()
        entering = threading.Event()
        write_bytes = atomic_file.write_bytes_atomic

        def pause_before_write(path: Path, content: bytes, *, mode: int | None = None) -> None:
            if path == status.path(self.page, "C1") and threading.current_thread().name.startswith("old-attempt"):
                checked.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise RuntimeError("the old attempt was not released")
            write_bytes(path, content, mode=mode)

        def newer_failure() -> str:
            entering.set()
            newer = status.begin(self.page, "C1", "validation", "new words")
            status.record(change_page.MessageDelivery(self.page, "C1", (), "", newer))
            return newer

        with patch.object(atomic_file, "write_bytes_atomic", new=pause_before_write), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-attempt") as old_worker, \
                ThreadPoolExecutor(max_workers=1) as new_worker:
            previous = old_worker.submit(status.record, old)
            try:
                self.assertTrue(checked.wait(TEST_HANG_GUARD_SECS))
                current = new_worker.submit(newer_failure)
                self.assertTrue(entering.wait(TEST_HANG_GUARD_SECS))
                with (self.root / ".review-link-delivery.lock").open("a+", encoding="utf-8") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                release.set()
            previous.result(TEST_HANG_GUARD_SECS)
            newest = current.result(TEST_HANG_GUARD_SECS)
        self.assertEqual(status.slots(self.page, "C1").validation,
                         change_page.ValidationRequested(newest, "new words"))

    def test_legacy_failed_records_offer_no_delivery_controls_and_send_nothing(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        status = change_page.DeliveryStatus(self.root)
        self.messages.clear()
        for content in ("failed\n", json.dumps({"feedback": "Check the proof"})):
            _ = status.path(self.page, "C1").write_text(content)
            change_page.recover_pending(status)
            body = self.request(self.page_path, {}).body
            self.assertIn("Saved: works", body)
            self.assert_no_delivery_controls(body)
            self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        self.assertEqual(self.messages, [])

    def test_a_render_failure_keeps_the_typed_answer_in_an_independent_form(self) -> None:
        words = "Still <small> & 'awkward'"
        before = self.review_list.read_bytes()
        with patch.object(change_page, "shown_rows", side_effect=ValueError(f"private path {self.temporary}")):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 400)
        self.assertIn("<style>", reply.body)
        self.assertIn('method="post"', reply.body)
        self.assertNotIn(str(self.temporary), reply.body)
        self.assertNotIn("<small>", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        self.assertEqual(self.request(self.page_path, self.answer(
            "C1", parser.selected_outcomes["C1"], parser.feedback["C1"])).status, 303)

    def test_after_a_save_next_publishes_the_queue_head_without_advancing_on_get(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        queue = next_item.change_queue(self.production)
        urgency.pin(queue.name, "C3")
        folders = set(self.root.iterdir())
        page = self.request(self.page_path, {}).body
        self.assertNotIn("changes left", page)
        self.assertIn('value="next">Skip for now', page)
        self.assertEqual(set(self.root.iterdir()), folders)
        for fields in ({"outcome": "next", "token": "wrong"},
                       {"outcome": "next", "token": self.page.token, "row": "C2"}):
            self.assertEqual(self.request(self.page_path, fields).status, 400)
        reply = self.request(self.page_path, {"outcome": "next", "token": self.page.token})
        self.assertEqual(reply.status, 303)
        path = urlsplit(reply.location).path
        self.assertNotEqual(path, self.page_path)
        self.assertEqual(change_page.page_at(self.root, path).rows, ("C3",))
        self.assertIn("Internal comments clarified", self.request(path, {}).body)
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])

    def test_an_empty_queue_shows_zero_without_a_next_control(self) -> None:
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C2", "C3"])).path
        page = change_page.page_at(self.root, path)
        for row in page.rows:
            self.assertEqual(self.request(path, dict(self.answer(row), token=page.token)).status, 303)
        body = self.request(path, {}).body
        self.assertIn("No changes to review.", body)
        self.assertNotIn('value="next"', body)
        self.assertEqual(self.request(path, {"outcome": "next", "token": page.token}).status, 400)


class FixtureStartupRecoveryTests(unittest.TestCase):
    @contextmanager
    def delayed_startup(self) -> Generator[tuple[ChangePageTests, threading.Thread], None, None]:
        fixture = ChangePageTests()
        deferred: list[threading.Thread] = []
        start = threading.Thread.start

        def defer_recovery(thread: threading.Thread) -> None:
            if not deferred:
                deferred.append(thread)
            else:
                start(thread)

        try:
            with patch.object(threading.Thread, "start", new=defer_recovery):
                fixture.setUp()
            self.assertEqual(len(deferred), 1)
            yield fixture, deferred[0]
        finally:
            fixture.doCleanups()

    def test_fixture_startup_cannot_clear_a_later_legacy_verdict(self) -> None:
        with self.delayed_startup() as (fixture, recovery):
            status = change_page.DeliveryStatus(fixture.root)
            _ = status.path(fixture.page, "C1").write_text(json.dumps({
                "attempt": "old-attempt", "state": "pending", "kind": "verdict",
            }), encoding="utf-8")
            recovery.run()
            _ = status.begin(fixture.page, "C1", "validation", "New words")
            self.assertEqual(status.slots(fixture.page, "C1").verdict,
                             change_page.DeliveryIntent("old-attempt", "verdict"))
            self.assertEqual(fixture.messages, [])

    def test_fixture_startup_cannot_send_during_a_legacy_context_rewrite(self) -> None:
        with self.delayed_startup() as (fixture, recovery):
            status = change_page.DeliveryStatus(fixture.root)
            _ = status.begin(fixture.page, "C1", "validation", "Keep these words")
            path = status.path(fixture.page, "C1")
            fields = cast(dict[str, object], json.loads(path.read_text()))
            fields["page"] = {"review_list": "/missing-list", "production": "/wrong-production", "row": "C999"}
            before = fixture.review_list.read_bytes()
            recovery.run()
            _ = path.write_text(json.dumps(fields), encoding="utf-8")
            restarted = change_page.DeliveryStatus(fixture.root)
            change_page.recover_pending(restarted)
            change_page.recover_pending(restarted)
            self.assertEqual(len(fixture.messages), 1)
            self.assertIn("User feedback: Keep these words", fixture.messages[0][2])
            self.assertIn("Production: page-owner", fixture.messages[0][2])
            self.assertNotIn("page", cast(dict[str, object], json.loads(path.read_text())))
            self.assertEqual(fixture.review_list.read_bytes(), before)
            self.assertIsInstance(status.slots(fixture.page, "C1").validation, change_page.ValidationRequested)


if __name__ == "__main__":
    _ = unittest.main()
