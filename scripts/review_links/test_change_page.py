"""Real page publishing, HTTP forms, verdict subprocesses and checkpoint routing."""
from __future__ import annotations

import fcntl
import os
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import dataclass
from datetime import date
from http.client import HTTPConnection
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch
from urllib.parse import urlencode, urljoin, urlsplit

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import urgency
from scripts.production import change_routing, change_review, next_item, showrunners, staff
from scripts.production.staff_test_support import fixture_member
from scripts.feature_log import feature_log
from scripts.review_links import change_browser, change_page, review_link, serve


@dataclass(frozen=True)
class Reply:
    status: int
    body: str
    location: str


class SavedAnswerForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.saved_answers: dict[str, dict[str, str]] = {}
        self.validation_requests: dict[str, dict[str, str]] = {}
        self.feedback: dict[str, str] = {}
        self.selected_outcomes: dict[str, str] = {}

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.fields = {}
        attributes = dict(attrs)
        name, value = attributes.get("name"), attributes.get("value")
        if tag in ("input", "button") and name is not None and value is not None:
            self.fields[name] = value
        if tag == "input" and name == "feedback" and value is not None:
            self.feedback[self.fields["row"]] = value
        if tag == "button" and attributes.get("aria-pressed") == "true" and value is not None:
            self.selected_outcomes[self.fields["row"]] = value
        if tag == "button" and value == "retry":
            self.saved_answers[self.fields["row"]] = dict(self.fields)
        if tag == "button" and value == "validate":
            self.validation_requests[self.fields["row"]] = dict(self.fields)


class BrowserForms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, str]] = {}
        self.tabs: dict[str, str] = {}
        self.save_urls: dict[str, str] = {}
        self.counts: dict[str, int] = {}
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
        if tag == "input" and self.current and values.get("name"):
            self.rows[self.current][values["name"] or ""] = values.get("value") or ""
        if tag == "form" and self.current and values.get("method") == "post":
            self.save_urls[self.current] = values.get("action") or ""
        if tag == "button" and self.current and values.get("name") == "outcome":
            self.outcomes[self.current].append(values.get("value") or "")
        if tag == "button" and values.get("data-showrunner"):
            self.count_tab = values["data-showrunner"] or ""
        if tag == "span" and "data-count" in values:
            self.read_count = True

    @override
    def handle_data(self, data: str) -> None:
        if self.read_count:
            self.counts[self.count_tab] = int(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "section":
            self.current = ""
        if tag == "span":
            self.read_count = False


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
        self.unit = staff.StaffRow(staff.StaffId(""), "unit", "", None, current_session=staff.StaffId(""))

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    @override
    def setUp(self) -> None:
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="change-page-test-")))
        self.root = self.temporary / "snapshots"
        self.repo = self.temporary / "repo"
        self.repo.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "NEXT_STATE_DIR": str(self.temporary / "next"),
            "SCRATCH_ROOT": str(self.temporary / "scratch"),
            "SCRATCH_HOLDS": str(self.temporary / "holds.json"),
            "SCRATCH_VISIBLE_ROOT": str(self.temporary / "repro"),
            "FEATURE_LOG_VAULT": str(self.temporary / "vault"),
            "XDG_STATE_HOME": str(self.temporary / "state"),
            "STAFF_FILE": str(self.temporary / "staff.json"),
            "XDG_CACHE_HOME": str(self.temporary / "cache"),
        }))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.temporary / "notifier"))
        _ = self.enterContext(patch.object(change_review, "LISTS", self.temporary))
        _ = self.enterContext(patch("change_review.LISTS", self.temporary))
        _ = fixture_member("showrunner", name="demo")
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "Review test")
        _ = self.git("config", "user.email", "review@example.invalid")
        _ = self.git("commit", "--allow-empty", "-m", "initial")
        _ = self.git("checkout", "-b", "feature")
        item = change_review.Visible("Numbered moves", "Run moves list", "Each group starts at 1")
        _ = self.git("commit", "--allow-empty", "-m",
                     f"checkpoint(feature): numbered moves\n\nChange-Review: {item.change} | {item.check} | {item.see}")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "feature", "-m", "merge numbered moves")
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
## Units
| Unit | Plan | Worktree | Branch |
| --- | --- | --- | --- |
| builder-unit | plan.md | /unused/feature | feature |
""", encoding="utf-8")
        runner = fixture_member("showrunner", name="page-owner", doc=staff.LiveDoc("natedev", self.production))
        self.unit = fixture_member("unit", name="builder-unit", parent=runner.id,
                                   worktree=staff.PresentWorktree("natedev", Path("/unused/feature")))
        _ = change_review.append_rows(self.review_list, [item, item, change_review.Visible(
            "Internal comments clarified", "Read the diff", "The old and new comments agree with the tests")],
            date(2026, 11, 1), merged, self.production)
        for row, kind in (("C1", "See in action"), ("C2", "See in action"), ("C3", "Read only")):
            _ = change_review.set_row_kind(self.review_list, row, kind, self.production)
        self.page_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C3"])).path
        self.page = change_page.page_at(self.root, self.page_path)
        router = change_routing.StaffRouting(staff.snapshot(), {"page-owner": self.production}, change_review)
        _ = router.graph(self.repo / ".git")
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
        server = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
        self.port = server.server_port
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)

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
        return subprocess.run([sys.executable, change_page.__file__, "answer-question", "--production", str(self.production),
                               "--list", str(self.review_list), "--row", row, "--question", question_id, "--text", text],
                              capture_output=True, text=True, check=False)

    def test_all_lists_load_once_and_the_browser_contains_the_whole_waiting_queue(self) -> None:
        other = self.temporary / "change-review - another.md"
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read the proof", "It agrees")],
                                      date(2026, 11, 2), "", self.production)
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

    def test_staff_moves_the_unit_changes_to_its_current_showrunner_tab(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.production))
        _ = staff.move(self.unit.id, runner.id)
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

    def test_a_units_table_name_does_not_replace_its_current_staff_session(self) -> None:
        from scripts.production.staff_test_support import fixture_runtime_id

        _ = staff.rename(self.unit.id, "x")
        current = staff.update_current_session(self.unit.id, staff.native_id(fixture_runtime_id("session-x")))
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        self.assertEqual(self.raw_messages[0][0], f"session:{current.current_session}")
        self.assertIn("builder-unit", self.production.read_text())
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
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read", "Proof")],
                                      date(2026, 11, 2), "", self.production)
        path, forms, body = self.browser()
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertEqual(list(prepared.queues), ["page-owner", "zeta", "demo"])
        self.assertEqual(list(forms.counts.items()), [("page-owner", 3), ("zeta", 1), ("demo", 0)])
        self.assertIn("tabs[0]?.dataset.showrunner", body)

    def test_sent_validation_and_question_tabs_sort_after_waiting_tabs(self) -> None:
        other = self.temporary / "change-review - alpha.md"
        _ = change_review.append_rows(other, [change_review.Visible("Other change", "Read", "Proof")],
                                      date(2026, 11, 2), "", self.production)
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
                self.assertEqual(list(pending.counts.items()), [("alpha", 1), ("page-owner", 3), ("demo", 0)])
                status.record(change_page.MessageDelivery(page, "C1", (), "", attempt), False)
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
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.production))
        _ = staff.move(self.unit.id, runner.id)
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

    def test_a_page_reads_all_commit_parents_once_per_repository(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.production))
        _ = staff.move(self.unit.id, runner.id)
        other_repo = self.temporary / "other-repo"
        _ = subprocess.run(["git", "clone", "--quiet", str(self.repo), str(other_repo)], check=True)
        other_doc = self.temporary / "other-production.md"
        other_list = self.temporary / "change-review - other.md"
        _ = other_doc.write_text(self.production.read_text().replace(str(self.repo), str(other_repo))
                                .replace(str(self.review_list), str(other_list)))
        _ = change_review.append_rows(other_list, [change_review.Visible("Other change", "Read", "Proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), other_doc)
        router = change_routing.StaffRouting(staff.snapshot(), {}, change_review)
        for repo in (self.repo, other_repo):
            router.history_path(repo / ".git").unlink(missing_ok=True)
        path = urlsplit(change_browser.publish()).path
        with patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            reply = self.request(path, {})
        self.assertEqual(reply.status, 200, reply.body)
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        parent_reads = [command for command in invocations if "rev-list" in command and "--parents" in command]
        self.assertCountEqual([command[2] for command in parent_reads],
                              [str(self.repo / ".git"), str(other_repo / ".git")])
        self.assertTrue(all("--stdin" in command and "--no-walk" in command for command in parent_reads))
        forms = BrowserForms()
        forms.feed(reply.body)
        self.assertEqual(set(forms.rows), {"page-owner/C1", "page-owner/C2", "page-owner/C3", "other/C1"})
        self.assertEqual(set(forms.tabs.values()), {"sdlc"})

    def test_an_unrouted_row_refuses_a_question_without_saving_and_still_accepts_a_verdict(self) -> None:
        _ = self.git("checkout", "-b", "aws-unit")
        _ = self.git("commit", "--allow-empty", "-m", "checkpoint(aws-unit): unknown owner")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "aws-unit", "-m", "Merge unknown unit")
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Unknown", "Read", "Proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), self.production)
        path, forms, body = self.browser()
        del body
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")
        self.assertNotIn("question", forms.outcomes["page-owner/C4"])
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

    def test_a_question_delivery_without_recipients_fails_and_can_retry_after_staff_is_fixed(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Who checks this?")
        assert isinstance(question, change_page.QuestionDelivery)
        delivery = change_page.MessageDelivery(self.page, "C1", (), "Question", question.attempt)
        with redirect_stderr(StringIO()) as error:
            change_page.deliver(delivery, status)
        self.assertIn("no showrunner to send to", error.getvalue())
        self.assertNotIn(question.attempt, status.question_messages_in_flight)
        failed = status.slots(self.page, "C1").question
        assert isinstance(failed, change_page.OpenQuestion) and isinstance(failed.delivery, change_page.DeliveryIntent)
        self.assertEqual(failed.delivery.state, "failed")
        self.assertEqual(failed.delivery.failure_note, "no showrunner to send to")
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        retried = status.retry_question(self.page, "C1", recipients=(runner.id,))
        slots = status.slots(self.page, "C1").question
        assert isinstance(slots, change_page.OpenQuestion) and isinstance(slots.delivery, change_page.DeliveryIntent)
        change_page.deliver(change_page.MessageDelivery(self.page, "C1", slots.delivery.recipients,
                                                       "Question", retried.attempt), status)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        sent = status.slots(self.page, "C1").question
        assert isinstance(sent, change_page.OpenQuestion)
        self.assertTrue(sent.awaiting_answer)
        self.assertNotIn(retried.attempt, status.question_messages_in_flight)

    def test_an_older_failed_verdict_without_recipients_retries_to_the_unit_and_showrunner(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text(json.dumps({"attempt": "old", "state": "failed", "kind": "verdict"}))
        before = self.review_list.read_bytes()
        self.messages.clear()
        self.assertEqual(self.request(self.page_path, self.answer(outcome="retry")).status, 303)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["builder-unit", "page-owner"])
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_unknown_staff_keeps_a_change_under_its_source_list(self) -> None:
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")

    def test_a_showrunner_own_change_stays_under_its_source_list(self) -> None:
        merged = self.git("rev-parse", "HEAD")
        _ = self.review_list.write_text(self.review_list.read_text().replace(merged, ""))
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")

    def test_an_unknown_unit_stays_in_its_list_even_when_production_names_another_showrunner(self) -> None:
        foreign = self.temporary / "sdlc-production.md"
        _ = foreign.write_text(self.production.read_text())
        _ = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", foreign))
        _ = self.git("checkout", "-b", "aws-unit")
        _ = self.git("commit", "--allow-empty", "-m", "checkpoint(aws-unit): unknown unit")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "aws-unit", "-m", "Merge aws-unit phase 1 into builds")
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Unknown unit", "Read", "Proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), foreign)
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")

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
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.production))
        _ = staff.move(self.unit.id, runner.id)
        earlier = self.temporary / "change-review - earlier.md"
        _ = change_review.append_rows(earlier, [change_review.Visible("First", "Read", "Proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), self.production,
                                      urgency.cell(urgency.read(), "C2"))
        _ = change_review.append_rows(earlier, [change_review.Visible("Second", "Read", "Proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), self.production)
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

    def test_a_merge_with_two_units_uses_each_change_trailer_for_its_staff_tab(self) -> None:
        first = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.production))
        second = fixture_member("showrunner", name="other", doc=staff.LiveDoc("natedev", self.production))
        _ = staff.move(self.unit.id, first.id)
        _ = fixture_member("unit", name="second-unit", parent=second.id)
        _ = self.git("checkout", "-b", "mixed")
        _ = self.git("commit", "--allow-empty", "-m",
                     "checkpoint(builder-unit): first change\n\nChange-Review: First mixed change | Read | First proof")
        _ = self.git("commit", "--allow-empty", "-m",
                     "checkpoint(second-unit): second change\n\nChange-Review: Second mixed change | Read | Second proof")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "mixed", "-m", "merge both units")
        _ = change_review.append_rows(self.review_list,
                                      [change_review.Visible("First mixed change", "Read", "First proof"),
                                       change_review.Visible("Second mixed change", "Read", "Second proof")],
                                      date(2026, 11, 2), self.git("rev-parse", "HEAD"), self.production)
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "sdlc")
        self.assertEqual(forms.tabs["page-owner/C5"], "other")

    def test_the_browser_response_does_not_wait_for_a_message_and_a_reload_shows_pending_delivery(self) -> None:
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
                self.assertIn("Saved: works", pending)
                self.assertIn("The message may not have sent", pending)
                self.assertIn("Send saved answer again", pending)
                self.assertIn('page-owner (<span data-count>2</span>)', pending)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        landed = self.request(path, {}).body
        self.assertNotIn("Send saved answer again", landed)

    def test_a_failed_browser_save_returns_plain_words_and_accepts_the_same_typed_answer_again(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "not works", "Keep <my> words & outcome")
        with patch.object(change_page, "close_feature_log", side_effect=RuntimeError("bad proposal /private/file.json")):
            failed = self.http_request(path, fields)
        self.assertEqual(failed.status, 503)
        self.assertEqual(json.loads(failed.body), {"reason": "The feature log could not be updated."})
        self.assertNotIn("/private/", failed.body)
        self.assertEqual(len(change_review.pending(self.review_list)), 3)
        self.delivery_done.clear()
        saved = self.http_request(path, fields)
        self.assertEqual(saved.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        rows = change_review.review_rows(self.review_list)
        self.assertEqual(next(row["Verdict"] for row in rows if row["#"] == "C1"), "not works: Keep <my> words & outcome")

    def test_a_later_browser_delivery_failure_remains_retryable_after_a_reload(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "not works", "Please fix"))
            self.assertEqual(reply.status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        failed = self.request(path, {}).body
        self.assertIn("Saved: not works: Please fix", failed)
        self.assertIn("Saved, but the message did not send", failed)
        self.assertIn("Send saved answer again", failed)
        retry = BrowserForms()
        retry.feed(failed)
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(retry, "page-owner/C1", "retry"))
        self.assertEqual(reply.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["builder-unit", "page-owner"])
        self.assertNotIn("Send saved answer again", self.request(path, {}).body)

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

    def test_the_browser_uses_the_existing_pin_and_move_order_without_publishing_next_pages(self) -> None:
        _ = next_item.move_to_end(next_item.CHANGE, self.production, key="C1")
        queue = next_item.change_queue(self.production)
        urgency.pin(queue.name, "C3")
        path, forms, body = self.browser()
        self.assertEqual(list(forms.rows), ["page-owner/C3", "page-owner/C2", "page-owner/C1"])
        before = set(self.root.iterdir())
        self.assertEqual(self.request(path, {}).status, 200)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIn('data-next hidden>Next', body)

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
        self.assertEqual(urgency.queue_order(queue.entries, queue.name)[-1].key, "C1")

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
        status.record(delivery, False)
        self.assertNotIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        status.path(delivery.page, "C1").unlink()
        self.assertIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        _ = change_review.record_verdict(self.review_list, "C1", "works", date(2026, 11, 2), self.production)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        refreshed = BrowserForms()
        refreshed.feed(change_browser.render(browser, status).decode())
        self.assertEqual(refreshed.counts["page-owner"], 2)

    def test_unsent_validation_records_keep_the_waiting_count_and_resend_control(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        browser = change_browser.page_at(self.root, path)
        status = change_page.DeliveryStatus(self.root)
        for failed in (False, True):
            with self.subTest(state="failed" if failed else "pending"):
                attempt = status.begin(self.page, "C1", "validation", "Keep these words")
                if failed:
                    status.record(change_page.MessageDelivery(self.page, "C1", (), "", attempt), True)
                prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
                self.assertIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
                self.assertIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
                content = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
                refreshed = BrowserForms()
                refreshed.feed(content)
                self.assertEqual(refreshed.counts["page-owner"], 3)
                self.assertTrue(refreshed.pending["page-owner/C1"])
                self.assertIn('data-resend>Send validation request again</button>', content)
                self.assertIn("The validation message did not send" if failed else "The message may not have sent", content)

    def test_failed_validation_delivery_stays_in_the_count_and_can_retry(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "validate", "Keep <these> words")
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, fields).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        failed = BrowserForms()
        failed.feed(self.request(path, {}).body)
        self.assertEqual(failed.counts["page-owner"], 3)
        self.assertTrue(failed.pending["page-owner/C1"])
        self.assertEqual(failed.outcomes["page-owner/C1"], ["works", "not works", "validate", "feedback", "question"])
        self.assertEqual(failed.rows["page-owner/C1"]["feedback"], "Keep <these> words")
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(
            failed, "page-owner/C1", "validate", failed.rows["page-owner/C1"]["feedback"])).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertIn("Keep <these> words", self.messages[-1][2])
        self.assertNotIn("Send validation request again", self.request(path, {}).body)

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
        for text in ("C1: the user asks a question", f"List: {self.review_list}", f"Production: {self.production}",
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
        self.assertEqual(refreshed.outcomes["page-owner/C1"], ["works", "not works", "validate", "feedback", "question"])
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

    def test_question_posts_refuse_empty_text_and_line_breaks_without_saving_or_sending(self) -> None:
        path, forms, body = self.browser()
        del body
        before = self.review_list.read_bytes()
        for text in ("", "   ", "First\nSecond", "First\rSecond", "First\r\nSecond", "First\u2028Second", "Which groups?\u2028"):
            with self.subTest(text=text):
                reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", text))
                self.assertEqual(reply.status, 400)
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "feedback", "First\u2028Second"))
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertFalse(change_page.DeliveryStatus(self.root).path(self.page, "C1").exists())

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

    def test_a_failed_question_delivery_has_a_visible_retry_with_the_saved_question(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "question", "Which <groups>?")
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, fields).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
        self.assertIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        self.assertIn('Message needs attention: page-owner/C1', content)
        self.assertIn('data-resend data-outcome="retry">Send question again</button>', content)
        self.assertIn("The question message did not send", content)
        self.assertEqual(refreshed.rows["page-owner/C1"]["feedback"], "")
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "retry",
                                                                  refreshed.rows["page-owner/C1"]["feedback"])).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Question: Which <groups>?", self.messages[0][2])
        self.assertNotIn("Send question again", self.request(path, {}).body)

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

    def test_a_pending_question_stays_waiting_and_refuses_a_retry_during_delivery(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        first = status.ask(self.page, "C1", "Which groups?")
        self.assertIsInstance(first, change_page.QuestionDelivery)
        record = status.path(self.page, "C1")
        pending = (record.read_bytes(), record.stat().st_mtime_ns)
        with self.assertRaisesRegex(change_page.AnswerRefused, "no question to send again"):
            _ = status.retry_question(self.page, "C1")
        self.assertIsInstance(status.ask(self.page, "C1", "Which groups?"), change_page.NoDelivery)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        content = self.request(path, {}).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertTrue(refreshed.pending["page-owner/C1"])
        self.assertIn("The message may not have sent", content)
        self.assertIn('data-resend data-outcome="retry">Send question again</button>', content)

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

    def test_a_server_restart_can_retry_a_pending_question_without_an_old_result_changing_the_retry(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        old = status.ask(self.page, "C1", "Which groups?")
        restarted = change_page.DeliveryStatus(self.root)
        new = restarted.retry_question(self.page, "C1")
        if not isinstance(old, change_page.QuestionDelivery):
            self.fail("a restart must allow recovery of an unfinished message")
        newer = change_page.MessageDelivery(self.page, "C1", (), "", new.attempt)
        restarted.record(newer, True)
        status.record(change_page.MessageDelivery(self.page, "C1", (), "", old.attempt), False)
        self.assertEqual(restarted.slots(self.page, "C1").question,
                         change_page.OpenQuestion(old.question_id, "Which groups?", change_page.DeliveryIntent(new.attempt, "failed", "question")))

    def test_browser_markup_supplies_waiting_rows_and_navigation_after_saves(self) -> None:
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.pending, dict.fromkeys(forms.rows, True))
        self.assertEqual(forms.outcomes["page-owner/C1"], ["works", "not works", "validate", "feedback", "question"])
        self.assertIn('data-next hidden>Next', body)
        self.assertIn('<p data-left></p>', body)
        self.assertIn("current===row && tab===row.dataset.tab && navigation===position", body)
        self.assertIn("row.dataset.pending='false';form.hidden=true", body)
        self.assertIn("go(following?.dataset.key || '')", body)
        self.assertNotIn("rows.push(row)", body)

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

    def test_validate_yourself_keeps_the_row_pending_and_moves_it_before_showing_next(self) -> None:
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        proposal = feature_log.state_dir() / "proposals/page-owner/C1.json"
        before_proposal = proposal.read_bytes()
        before = self.review_list.read_bytes()
        initial = self.request(self.page_path, {}).body
        self.assertEqual(initial.count('value="validate">Validate yourself'), len(self.page.rows))
        words = "Please check this yourself"
        reply = self.request(self.page_path, self.answer(outcome="validate", feedback=words))
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(proposal.read_bytes(), before_proposal)
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C1", "C2", "C3"])
        queue = next_item.change_queue(self.production)
        self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries, queue.name)], ["C2", "C3", "C1"])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1: the user asks you to validate this", self.messages[0][2])
        self.assertIn(words, self.messages[0][2])
        page = self.request(self.page_path, {}).body
        self.assertIn("3 changes left", page)
        self.assertIn('value="next">Next', page)
        self.assertNotIn("Saved:", page)
        self.assertNotIn("Send validation request again", page)
        following = self.request(self.page_path, {"outcome": "next", "token": self.page.token})
        self.assertEqual(following.status, 303)
        self.assertEqual(change_page.page_at(self.root, urlsplit(following.location).path).rows, ("C2",))

    def test_a_failed_validation_message_keeps_feedback_for_a_retry_after_the_response(self) -> None:
        release = threading.Event()
        words = "Check <this> & keep the proof"

        def failed_send(recipient: str, sender: str, message: str) -> None:
            del recipient, sender, message
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test message was not released")
            raise RuntimeError(f"private failure at {self.temporary}/socket")

        before = self.review_list.read_bytes()
        self.delivery_done.clear()
        with patch.object(change_page, "send_message", side_effect=failed_send):
            try:
                reply = self.http_request(self.page_path, self.answer(outcome="validate", feedback=words))
                self.assertEqual(reply.status, 303)
                self.assertFalse(self.delivery_done.is_set())
                initial = self.http_request(self.page_path, {}).body
                self.assertIn("3 changes left", initial)
                self.assertIn("Send validation request again", initial)
                self.assertIn("The message may not have sent", initial)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        other_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1"])).path
        body = self.request(other_path, {}).body
        self.assertIn("The validation message did not send", body)
        self.assertIn("Send validation request again", body)
        self.assertNotIn(str(self.temporary), body)
        self.assertNotIn("Saved:", body)
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.validation_requests["C1"]["feedback"], words)
        self.assertEqual(self.request(other_path, parser.validation_requests["C1"]).status, 303)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn(words, self.messages[0][2])
        self.assertNotIn("Send validation request again", self.request(self.page_path, {}).body)
        self.assertFalse(change_page.DeliveryStatus(self.root).failed(self.page, "C1"))

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
        _ = change_review.append_rows(earlier, [change_review.Visible("Earlier change", "Run it", "Proof")],
                                      date(2026, 11, 1), "", self.production)
        _ = change_review.set_row_kind(earlier, "C1", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, earlier, ["C1"])).path
        page = change_page.page_at(self.root, path)
        before = earlier.read_bytes()
        fields = dict(self.answer(outcome="validate"), token=page.token)
        self.assertEqual(self.request(path, fields).status, 303)
        self.assertEqual(earlier.read_bytes(), before)
        queue = next_item.change_queue(self.production)
        self.assertEqual(set(urgency.ended(queue.name)), {"demo/C1"})
        self.assertEqual(urgency.queue_order(queue.entries, queue.name)[-1].key, "demo/C1")
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["demo"])
        self.assertIn("4 changes left", self.request(path, {}).body)

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

    def test_not_works_and_free_text_take_the_existing_checkpoint_unit_route(self) -> None:
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
                                      date(2026, 11, 1), "", self.production)
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

    def test_command_publishes_one_row_and_a_batch_through_the_same_path(self) -> None:
        for rows in (["C1"], ["C1", "C2", "C3"]):
            with self.subTest(rows=rows):
                result = subprocess.run([sys.executable, change_page.__file__, "--production", str(self.production),
                                         "--list", str(self.review_list), *rows],
                                        capture_output=True, text=True, check=False)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                path = urlsplit(result.stdout.strip()).path
                reply = self.request(path, {})
                self.assertEqual(reply.status, 200)
                for row in ("C1", "C2", "C3"):
                    self.assertEqual(f'<section id="{row}">' in reply.body, row in rows)

    def test_delivery_failure_keeps_the_saved_answer_and_allows_the_same_submission_to_retry(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 303)
        self.assertIn("Saved, but the message did not send", self.request(self.page_path, {}).body)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(len(self.messages), 1)

    def feature_command(self, *arguments: str) -> None:
        result = subprocess.run([sys.executable, feature_log.__file__, *arguments],
                                capture_output=True, text=True, check=False)
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
        with patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            reply = self.request(self.page_path, self.answer())
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        self.assertEqual([args[2] for args in invocations], ["project-of", "add", "verdict"])
        self.assertEqual(invocations[1], [sys.executable, feature_log.__file__, "add", "--review",
                                          "page-owner/C1", "--if-proposed"])
        self.assertEqual(invocations[2],
                         [sys.executable, str(Path(change_review.__file__)), "verdict", "--production",
                          str(self.production), "--list", str(self.review_list), "--row", "C1", "--verdict=works"])
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

    def test_a_saved_answer_retries_delivery_without_requiring_its_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        (feature_log.state_dir() / "proposals/page-owner/C1.json").unlink()
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        self.assertIn("C1", parser.saved_answers)
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

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

    def test_a_checkpoint_hash_routes_feedback_to_its_unit(self) -> None:
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Direct checkpoint", "Run", "Proof")],
                                      date(2026, 11, 1), self.git("rev-parse", "feature"), self.production)
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
        for doc in (root_doc, child, sibling):
            slug = doc.stem.removesuffix("-production")
            _ = doc.write_text(self.production.read_text().replace("builder-unit", f"{doc.stem}-unit").replace(
                "fallback-owner", slug))
            timer = notifier / f"showrunner-{doc.stem.removesuffix('-production')}"
            timer.mkdir(parents=True)
            _ = (timer / "conf").write_text(f"EVERY=15\nTARGET=session:test\nPROMPT_FILE={timer / 'prompt'}\n"
                                           + f"CHECK=zsh /unused/production_check.sh {doc}\n")
            if doc != root_doc:
                _ = fixture_member("showrunner", name=doc.stem.removesuffix("-production"), parent=root.id)
        owner = staff.snapshot().by_path("page-owner")
        if owner is None:
            self.fail("the list showrunner must have a staff record")
        _ = fixture_member("unit", name="sibling-production-unit", parent=owner.id)
        _ = self.git("commit", "--allow-empty", "-m", "checkpoint(sibling-production-unit): sibling change")
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Sibling change", "Run", "Proof")],
                                      date(2026, 11, 1), self.git("rev-parse", "HEAD"), sibling)
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", child)
        path = urlsplit(change_page.publish(child, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works"), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["sibling-production-unit", "page-owner"])

    def test_an_unknown_production_is_refused_before_saving(self) -> None:
        _ = change_review.append_rows(self.review_list, [change_review.Visible("Unknown owner", "Run", "Proof")],
                                      date(2026, 11, 1), "", self.temporary / "missing-production.md")
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(path, dict(self.answer("C4"), token=page.token)).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_the_reloaded_saved_form_retries_failed_delivery_with_the_saved_words(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback="Still small")).status,
                             303)
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        self.assertIn("C1", parser.saved_answers)
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        self.assertTrue(all("not works: Still small" in message for _, _, message in self.messages))
        self.assertEqual(self.request(self.page_path, self.answer("C3", "retry")).status, 400)

    def test_a_non_ascii_token_is_refused_without_changing_the_record(self) -> None:
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, dict(self.answer(), token="é")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_each_section_shows_its_change_text_once(self) -> None:
        body = self.request(self.page_path, {}).body
        self.assertEqual(body.count("Numbered moves"), 1)
        self.assertEqual(body.count("Internal comments clarified"), 1)

    def test_feedback_works_and_its_retry_notify_only_the_showrunner(self) -> None:
        def report_delivery_failure(recipient: str, sender: str, message: str) -> None:
            self.capture_message(recipient, sender, message)
            raise RuntimeError("delivery receipt missing")

        with patch.object(change_page, "send_message", side_effect=report_delivery_failure):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="feedback", feedback="works")).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner", "page-owner"])

    def test_a_retry_repeats_a_failed_unit_delivery_for_the_saved_verdict(self) -> None:
        attempts: list[str] = []

        def fail_unit_delivery(recipient: str, sender: str, message: str) -> None:
            recipient = self.recipient_name(recipient)
            attempts.append(recipient)
            if recipient == "builder-unit":
                raise RuntimeError("unit socket unavailable")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=fail_unit_delivery):
            reply = self.request(self.page_path, self.answer(outcome="feedback", feedback="Use a smaller title"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(attempts, ["builder-unit", "page-owner"])
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner", "builder-unit", "page-owner"])
        self.assertTrue(all("Use a smaller title" in message for _, _, message in self.messages))

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
                self.assertIn("Send saved answer again", page)
                self.assertIn("The message may not have sent", page)
                self.assertNotIn("Saved, but the message did not send", page)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertNotIn("Send saved answer again", self.request(self.page_path, {}).body)

    def test_a_later_message_failure_persists_across_links_until_a_successful_retry(self) -> None:
        release = threading.Event()

        def failed_send(recipient: str, sender: str, message: str) -> None:
            del recipient, sender, message
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test message was not released")
            raise RuntimeError(f"private failure at {self.temporary}/socket")

        self.delivery_done.clear()
        with patch.object(change_page, "send_message", side_effect=failed_send):
            try:
                self.assertEqual(self.http_request(self.page_path, self.answer()).status, 303)
                initial = self.http_request(self.page_path, {}).body
                self.assertIn("Saved: works", initial)
                self.assertIn("Send saved answer again", initial)
                self.assertIn("The message may not have sent", initial)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        other_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1"])).path
        page = self.request(other_path, {}).body
        self.assertIn("Saved: works", page)
        self.assertIn("Saved, but the message did not send", page)
        self.assertIn("Send saved answer again", page)
        self.assertNotIn("private failure", page)
        self.assertNotIn(str(self.temporary), page)
        status = change_page.DeliveryStatus(self.root)
        self.assertTrue(status.failed(self.page, "C1"))
        self.assertEqual(self.request("/" + status.path(self.page, "C1").name, {}).status, 404)
        parser = SavedAnswerForm()
        parser.feed(page)
        self.assertEqual(self.request(other_path, parser.saved_answers["C1"]).status, 303)
        self.assertFalse(status.failed(self.page, "C1"))
        self.assertNotIn("Send saved answer again", self.request(self.page_path, {}).body)

    def test_an_interruption_after_the_verdict_preserves_a_retryable_delivery_intent(self) -> None:
        run = subprocess.run

        def interrupt_after_verdict(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            result = run(arguments, capture_output=True, text=True, check=False)
            if arguments[2] == "verdict" and result.returncode == 0:
                raise RuntimeError("interrupted after verdict")
            return result

        with patch.object(subprocess, "run", side_effect=interrupt_after_verdict):
            reply = self.request(self.page_path, self.answer(feedback="Clear numbers"))
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.messages, [])
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works: Clear numbers", body)
        self.assertIn("The message may not have sent", body)
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertIn("C1", parser.saved_answers)
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertIn("works: Clear numbers", self.messages[0][2])
        self.assertNotIn("Send saved answer again", self.request(self.page_path, {}).body)

    def test_an_intent_write_failure_refuses_the_verdict_write(self) -> None:
        before = self.review_list.read_bytes()
        with patch.object(change_page.DeliveryStatus, "begin", side_effect=OSError("disk full")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertNotIn("Saved:", reply.body)

    def test_a_failed_or_interrupted_verdict_preserves_the_failed_validation_request(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        run = subprocess.run
        for row, interruption in (("C1", False), ("C3", True)):
            with self.subTest(interruption=interruption):
                words = f"Check the small text on {row}"
                with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
                    self.assertEqual(self.request(self.page_path, self.answer(row, "validate", words)).status, 303)
                validation = status.slots(self.page, row).validation
                before = self.review_list.read_bytes()

                def fail_verdict(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    del options
                    if arguments[2] != "verdict":
                        return run(arguments, capture_output=True, text=True, check=False)
                    slots = status.slots(self.page, row)
                    self.assertEqual(slots.validation, validation)
                    self.assertIsInstance(slots.verdict, change_page.DeliveryIntent)
                    if interruption:
                        raise RuntimeError("interrupted before verdict")
                    return subprocess.CompletedProcess(arguments, 1, "", "verdict write failed")

                with patch.object(subprocess, "run", side_effect=fail_verdict):
                    reply = self.request(self.page_path, self.answer(row, feedback="New answer"))
                self.assertEqual(reply.status, 503 if interruption else 400)
                self.assertEqual(self.review_list.read_bytes(), before)
                self.assertEqual(status.read(self.page, row), validation)
                self.assertTrue(status.failed(self.page, row))
                body = self.request(self.page_path, {}).body
                parser = SavedAnswerForm()
                parser.feed(body)
                self.assertEqual(parser.validation_requests[row]["feedback"], words)
                self.assertIn("The validation message did not send", body)
                self.assertNotIn("Saved:", body)
                self.assertEqual(self.request(self.page_path, parser.validation_requests[row]).status, 303)
                self.assertIn(words, self.messages[-1][2])
                self.assertIsInstance(status.read(self.page, row), change_page.NoDelivery)
                self.assertIn(row, [item["#"] for item in change_review.pending(self.review_list)])

    def test_an_interruption_after_saving_selects_the_verdict_slot_and_keeps_validation_feedback(self) -> None:
        words = "Check the small text"
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="validate", feedback=words)).status, 303)
        status = change_page.DeliveryStatus(self.root)
        validation = status.slots(self.page, "C1").validation
        run = subprocess.run

        def interrupt_after_save(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            if arguments[2] == "verdict":
                self.assertEqual(status.slots(self.page, "C1").validation, validation)
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.DeliveryIntent)
            result = run(arguments, capture_output=True, text=True, check=False)
            if arguments[2] == "verdict" and result.returncode == 0:
                raise RuntimeError("interrupted after verdict")
            return result

        with patch.object(subprocess, "run", side_effect=interrupt_after_save):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 503)
        slots = status.slots(self.page, "C1")
        self.assertEqual(slots.validation, validation)
        self.assertEqual(status.read(self.page, "C1"), slots.verdict)
        self.assertFalse(status.failed(self.page, "C1"))
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assertIn("The message may not have sent", body)
        self.assertNotIn("Send validation request again", body)
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual(status.slots(self.page, "C1").validation, validation)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        self.assertIsInstance(status.read(self.page, "C1"), change_page.NoDelivery)
        self.assertFalse(status.failed(self.page, "C1"))
        body = self.request(self.page_path, {}).body
        self.assertNotIn("Send saved answer again", body)
        self.assertNotIn("Send validation request again", body)

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
                status.record(other_delivery, True)
                before = status.slots(self.page, "C1")
                remaining = before.verdict if other == "verdict" else before.validation
                status.record(first_delivery, True)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                status.record(first_delivery, False)
                status.record(first_delivery, True)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                if first == "validation":
                    self.assertEqual(after.validation, change_page.ValidationRequested(validation, "Keep these words"))
                else:
                    self.assertIsInstance(after.verdict, change_page.NoDelivery)
                stored = status.path(self.page, "C1").read_text(encoding="utf-8")
                self.assertEqual(f'"{first}"' in stored, first == "validation")
                self.assertIn(f'"{other}"', stored)
                status.record(other_delivery, False)
                self.assertEqual(status.slots(self.page, "C1").validation,
                                 change_page.ValidationRequested(validation, "Keep these words"))
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)

    def test_single_intent_records_migrate_to_their_kind_without_losing_feedback(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        kinds: tuple[Literal["validation", "verdict"], ...] = ("validation", "verdict")
        for kind in kinds:
            with self.subTest(kind=kind):
                _ = status.path(self.page, "C1").write_text(json.dumps({
                    "attempt": "old-attempt", "state": "failed", "kind": kind,
                    "feedback": "Keep these words" if kind == "validation" else "",
                }), encoding="utf-8")
                slots = status.slots(self.page, "C1")
                original = slots.validation if kind == "validation" else slots.verdict
                self.assertEqual(original, change_page.DeliveryIntent(
                    "old-attempt", "failed", kind,
                    "Keep these words" if kind == "validation" else ""))
                if kind == "validation":
                    _ = status.begin(self.page, "C1", "verdict")
                else:
                    _ = status.begin(self.page, "C1", "validation", "New words")
                migrated = status.slots(self.page, "C1")
                self.assertEqual(migrated.validation if kind == "validation" else migrated.verdict, original)

    def test_a_stopped_send_leaves_the_pending_record_and_resend_control(self) -> None:
        stopped = threading.Event()

        def stop_delivery(delivery: change_page.MessageDelivery, status: change_page.DeliveryStatus) -> None:
            del delivery, status
            stopped.set()
            raise OSError("server stopped")

        with patch.object(change_page, "deliver", side_effect=stop_delivery):
            self.assertEqual(self.http_request(self.page_path, self.answer()).status, 303)
            self.assertTrue(stopped.wait(TEST_HANG_GUARD_SECS))
        status = change_page.DeliveryStatus(self.root)
        intent = status.read(self.page, "C1")
        self.assertIsInstance(intent, change_page.DeliveryIntent)
        if not isinstance(intent, change_page.DeliveryIntent):
            self.fail("the delivery intent is missing")
        self.assertEqual((intent.state, intent.kind, intent.feedback), ("pending", "verdict", ""))
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assertIn("The message may not have sent", body)
        self.assertIn("Send saved answer again", body)

    def test_an_older_success_cannot_clear_a_newer_failed_validation_attempt(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        older = status.begin(self.page, "C1", "validation", "old words")
        newer = status.begin(self.page, "C1", "validation", "new words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", older)
        new = change_page.MessageDelivery(self.page, "C1", (), "", newer)
        status.record(new, True)
        status.record(old, False)
        status.record(old, True)
        self.assertEqual(status.read(self.page, "C1"),
                         change_page.DeliveryIntent(newer, "failed", "validation", "new words"))
        body = self.request(self.page_path, {}).body
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertEqual(parser.validation_requests["C1"]["feedback"], "new words")
        self.assertIn("The validation message did not send", body)

    def test_attempt_comparison_and_update_hold_the_lock_against_a_new_attempt(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "validation", "old words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", attempt)
        checked = threading.Event()
        release = threading.Event()
        entering = threading.Event()
        replace_file = review_link.replace_file

        def pause_before_write(path: Path, content: bytes) -> None:
            if path == status.path(self.page, "C1") and threading.current_thread().name.startswith("old-attempt"):
                checked.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise RuntimeError("the old attempt was not released")
            replace_file(path, content)

        def newer_failure() -> str:
            entering.set()
            newer = status.begin(self.page, "C1", "validation", "new words")
            status.record(change_page.MessageDelivery(self.page, "C1", (), "", newer), True)
            return newer

        with patch.object(review_link, "replace_file", new=pause_before_write), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-attempt") as old_worker, \
                ThreadPoolExecutor(max_workers=1) as new_worker:
            previous = old_worker.submit(status.record, old, False)
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
        self.assertEqual(status.read(self.page, "C1"),
                         change_page.DeliveryIntent(newest, "failed", "validation", "new words"))

    def test_legacy_delivery_records_keep_saved_and_validation_answers_retryable(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text("failed\n", encoding="utf-8")
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertFalse(status.failed(self.page, "C1"))
        _ = status.path(self.page, "C3").write_text(json.dumps({"feedback": "Check the proof"}), encoding="utf-8")
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        self.assertEqual(parser.validation_requests["C3"]["feedback"], "Check the proof")
        self.assertEqual(self.request(self.page_path, parser.validation_requests["C3"]).status, 303)
        self.assertIn("Check the proof", self.messages[-1][2])
        self.assertFalse(status.failed(self.page, "C3"))

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
        self.assertIn("2 changes left", page)
        self.assertIn('value="next">Next', page)
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
        self.assertIn("0 changes left", body)
        self.assertNotIn('value="next"', body)
        self.assertEqual(self.request(path, {"outcome": "next", "token": page.token}).status, 400)


if __name__ == "__main__":
    _ = unittest.main()
