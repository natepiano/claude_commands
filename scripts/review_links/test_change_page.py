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
from urllib.parse import urlencode, urlsplit

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import urgency
from scripts.production import change_review, next_item, showrunners
from scripts.production.staff_test_support import fixture_member
from scripts.feature_log import feature_log
from scripts.review_links import change_page, serve


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


class ChangePageTests(unittest.TestCase):
    temporary: Path = Path()
    root: Path = Path()
    repo: Path = Path()
    production: Path = Path()
    review_list: Path = Path()
    page_path: str = ""
    page: change_page.ReviewPage = change_page.ReviewPage(Path(), Path(), (), "")
    port: int = 0
    messages: list[tuple[str, str, str]] = []
    delivery_done: threading.Event = threading.Event()

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
        _ = change_review.append_rows(self.review_list, [item, item, change_review.Visible(
            "Internal comments clarified", "Read the diff", "The old and new comments agree with the tests")],
            date(2026, 11, 1), merged, self.production)
        for row, kind in (("C1", "See in action"), ("C2", "See in action"), ("C3", "Read only")):
            _ = change_review.set_row_kind(self.review_list, row, kind, self.production)
        self.page_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C3"])).path
        self.page = change_page.page_at(self.root, self.page_path)
        self.messages = []
        self.delivery_done = threading.Event()
        deliver = change_page.deliver

        def finish_delivery(delivery: change_page.MessageDelivery, status: change_page.DeliveryStatus) -> None:
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

    def capture_message(self, recipient: str, sender: str, message: str) -> None:
        self.messages.append((recipient, sender, message))

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
        self.assertEqual([target for target, _, _ in self.messages], ["fallback-owner", "page-owner"])

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
                self.assertIsInstance(after.validation if first == "validation" else after.verdict,
                                      change_page.NoDelivery)
                stored = status.path(self.page, "C1").read_text(encoding="utf-8")
                self.assertNotIn(f'"{first}"', stored)
                self.assertIn(f'"{other}"', stored)
                status.record(other_delivery, False)
                self.assertFalse(status.path(self.page, "C1").exists())

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
        unlink = Path.unlink

        def pause_before_delete(path: Path, missing_ok: bool = False) -> None:
            if path == status.path(self.page, "C1") and threading.current_thread().name.startswith("old-attempt"):
                checked.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise RuntimeError("the old attempt was not released")
            unlink(path, missing_ok=missing_ok)

        def newer_failure() -> str:
            entering.set()
            newer = status.begin(self.page, "C1", "validation", "new words")
            status.record(change_page.MessageDelivery(self.page, "C1", (), "", newer), True)
            return newer

        with patch.object(Path, "unlink", new=pause_before_delete), \
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
