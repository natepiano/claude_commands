"""Real page publishing, HTTP forms, verdict subprocesses and checkpoint routing."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import date
from http.client import HTTPConnection
from html.parser import HTMLParser
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, showrunners, staff
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

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.fields = {}
        attributes = dict(attrs)
        name, value = attributes.get("name"), attributes.get("value")
        if tag in ("input", "button") and name is not None and value is not None:
            self.fields[name] = value
        if tag == "button" and value == "retry":
            self.saved_answers[self.fields["row"]] = dict(self.fields)


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
        _ = staff.ensure("showrunner", name="demo")
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
        _ = self.production.write_text(f"""# Demo
## Production Context
- **Showrunner checkout:** `{self.repo}`
- **Showrunner session:** fallback-owner
## Units
| Unit | Plan | Worktree | Branch |
| --- | --- | --- | --- |
| builder-unit | plan.md | /unused/feature | feature |
""", encoding="utf-8")
        self.review_list = self.temporary / "change-review - page-owner.md"
        _ = change_review.append_rows(self.review_list, [item, item, change_review.Visible(
            "Internal comments clarified", "Read the diff", "The old and new comments agree with the tests")],
            date(2026, 11, 1), merged, self.production)
        for row, kind in (("C1", "See in action"), ("C2", "See in action"), ("C3", "Read only")):
            _ = change_review.set_row_kind(self.review_list, row, kind, self.production)
        self.page_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C3"])).path
        self.page = change_page.page_at(self.root, self.page_path)
        self.messages = []
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
        self.assertEqual(reply.status, 503)
        self.assertIn("Answer saved; delivery failed", reply.body)
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

    def test_a_missing_proposal_keeps_the_row_open_and_tells_the_showrunner(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        before = self.review_list.read_bytes()
        reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("no proposal for page-owner/C1", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 stays open", self.messages[0][2])
        self.assertIn("no proposal", self.messages[0][2])

    def test_a_saved_answer_retries_delivery_without_requiring_its_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 503)
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

    def test_the_project_policy_can_require_a_proposal_before_saving(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/nixos.git")
        project = feature_log.PROJECTS["natedev"]._replace(closes_by_change_review=True)
        before = self.review_list.read_bytes()
        with patch.dict(feature_log.PROJECTS, {"natedev": project}):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("no proposal for page-owner/C1", reply.body)
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
        root = staff.ensure("showrunner", name="root")
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
                _ = staff.ensure("showrunner", name=doc.stem.removesuffix("-production"), parent=root.id)
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
                             503)
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
        self.assertEqual(reply.status, 503)
        self.assertEqual(attempts, ["builder-unit", "page-owner"])
        parser = SavedAnswerForm()
        parser.feed(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, parser.saved_answers["C1"]).status, 303)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner", "builder-unit", "page-owner"])
        self.assertTrue(all("Use a smaller title" in message for _, _, message in self.messages))


if __name__ == "__main__":
    _ = unittest.main()
