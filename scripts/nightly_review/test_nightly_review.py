"""The nightly review: its night's job reaches the permanent unit, and its message counts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nightly_review
from scripts.whoami import account
from scripts.accounts import accounts
from scripts.production import showrunners
# `messages` reads the message review from its own folder, as the timer does.
# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(nightly_review.__file__).resolve().parents[1] / "notify"))
if TYPE_CHECKING:
    from scripts.notify import review
    from scripts.production.unit_lookup import LiveClaude
else:
    import review
    from unit_lookup import LiveClaude

NIGHT = date(2026, 9, 29)
SESSION = "nightly-review"
DOC = Path("/work/docs/plans/build-followups-production.md")
UNIT = LiveClaude("nightly-review", "aaaaaaaa-0000-0000-0000-000000000001", "/run/cc-socks/42.sock", 42)
PRODUCTION = nightly_review.NightlyProduction(DOC, "build-followups")


def setUpModule() -> None:
    """The times these tests expect are Pacific; neither the user's zone setting nor a showrunner
    registered on the machine running them may change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))
    no_showrunners = unittest.enterModuleContext(tempfile.TemporaryDirectory())
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", Path(no_showrunners)))


def done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class NightlyReviewTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        environment_context: object = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(home)})))
        del environment_context
        _ = self.enterContext(mock.patch.object(nightly_review, "ROOT", self.root / "nights"))

    def test_timer_runs_messages_after_rust(self) -> None:
        self.assertEqual(nightly_review.MODES, ("config", "rust", "messages"))
        self.assertEqual(nightly_review.PROMPT, "/nightly_review config rust messages")


    def test_copied_nightly_library_import_needs_no_message_review_or_production_modules(self) -> None:
        copied = self.root / "copy/scripts/nightly_review/nightly_review.py"
        copied.parent.mkdir(parents=True)
        _ = shutil.copyfile(nightly_review.__file__, copied)
        library = copied.parents[1] / "lib"
        library.mkdir()
        _ = shutil.copyfile(Path(nightly_review.__file__).resolve().parents[1] / "lib/process_failure.py",
                            library / "process_failure.py")
        whoami = copied.parents[1] / "whoami"
        whoami.mkdir()
        _ = (whoami / "account.py").write_text("def process_config_dir(pid): pass\n")
        result = subprocess.run([sys.executable, "-I", "-c",
                                 "import runpy,sys; runpy.run_path(sys.argv[1], run_name='copied_library')", str(copied)],
                                cwd=self.root, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_transcripts_use_session_accounts_and_timer_identity_after_rename(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        paths: list[Path] = []
        for index, name in enumerate(("renamed coordinator", "natedev")):
            config = self.root / f"account-{index}"
            path = config / "projects/-work" / f"sid-{index}.jsonl"
            path.parent.mkdir(parents=True)
            _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
                type="assistant", timestamp=since.isoformat())) + "\n")
            paths.append(path)
            record = nightly_review.TranscriptSessionRecord(sessionId=f"sid-{index}", name=name,
                cwd="/work", config_dir=str(config))
            if index == 0:
                record["transcript_path"] = str(path)
                record["cwd"] = "/old-work"
            _ = (sessions / f"{index}.json").write_text(json.dumps(record))
        def production_of(sid: str) -> Path | None:
            return self.root if sid == "sid-0" else None
        with (mock.patch.object(nightly_review, "SESSIONS_DIR", sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
              mock.patch.object(showrunners, "production_of", side_effect=production_of),
              mock.patch.object(account, "process_config_dir", side_effect=AssertionError("recorded account lost"))):
            self.assertEqual(nightly_review.showrunner_transcripts(since), sorted(paths))

    def test_transcript_selection_uses_reply_time_instead_of_recent_file_write(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        path = self.root / "old.jsonl"
        _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=(since - timedelta(days=2)).isoformat())) + "\n")
        _ = (sessions / "1.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            name="natedev", sessionId="sid", transcriptPath=str(path))))
        with (mock.patch.object(nightly_review, "SESSIONS_DIR", sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[])):
            self.assertEqual(nightly_review.showrunner_transcripts(since), [])

    def test_transcript_lookup_uses_the_sessions_process_account(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        config = self.root / "showrunner-account"
        path = config / "projects/-work" / "sid.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=since.isoformat())) + "\n")
        _ = (sessions / "1.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            name="natedev", sessionId="sid", cwd="/work", pid=42)))
        with (mock.patch.object(nightly_review, "SESSIONS_DIR", sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
              mock.patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(config)) as directory):
            self.assertEqual(nightly_review.showrunner_transcripts(since), [path])
        directory.assert_called_once_with(42)

    def test_live_counts_exclude_dry_runs_and_keep_unreviewed_apart_from_failures(self) -> None:
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        records = [nightly_review.NightlyVerdictRecord(time=since.isoformat(), verdict=verdict,
                   failed_rules=["reader-starts-cold"]) for verdict in ("send", "rewrite", "ask", "unreviewed: timeout")]
        records += [nightly_review.NightlyVerdictRecord(time=since.isoformat(), verdict="rewrite", failed_rules=[], replay=True),
                    nightly_review.NightlyVerdictRecord(time=since.isoformat(), verdict="rewrite", failed_rules=[], dry=True),
                    nightly_review.NightlyVerdictRecord(time=since.isoformat(), verdict="rewrite", failed_rules=[], sample=True)]
        records.append(nightly_review.NightlyVerdictRecord(time=(since - timedelta(seconds=1)).isoformat(),
            verdict="rewrite", failed_rules=["reader-starts-cold"]))
        path = self.root / "review.jsonl"
        _ = path.write_text("\n".join(json.dumps(record) for record in records) + '\n{"time":{},"verdict":"ask"}\n')
        self.assertEqual(nightly_review.live_message_counts(path, since),
                         nightly_review.LiveMessageCounts(4, 2, 1, (("reader-starts-cold", 2),)))

    def test_question_review_records_leave_live_push_totals_unchanged(self) -> None:
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        path = self.root / "review.jsonl"
        push = nightly_review.NightlyVerdictRecord(time=since.isoformat(), verdict="send", failed_rules=[])
        _ = path.write_text(json.dumps(push) + "\n")
        before = nightly_review.live_message_counts(path, since)
        for verdict in ("send", "ask", "failed", "unreviewed: timeout"):
            with path.open("a") as output:
                _ = output.write(json.dumps(dict(push, kind="question", verdict=verdict,
                                                 failed_rules=["reader-starts-cold"])) + "\n")
        self.assertEqual(nightly_review.live_message_counts(path, since), before)

    def test_failed_and_withheld_live_pushes_count_and_registry_failures_are_attributed(self) -> None:
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        path = self.root / "review.jsonl"
        records: list[dict[str, object]] = [{"time": since.isoformat(), "verdict": verdict, "failed_rules": ["reader-starts-cold"],
                    "text_id": "quota", "detail": "sent despite failed review: reader-starts-cold", "sent": {"body": "original"}} for verdict in ("failed", "withhold")]
        records.append({"time": since.isoformat(), "verdict": "failed", "failed_rules": ["reader-starts-cold"], "text_id": "quota", "dry": True})
        _ = path.write_text("\n".join(json.dumps(record) for record in records))
        self.assertEqual(nightly_review.live_message_counts(path, since), nightly_review.LiveMessageCounts(2, 2, 0, (("reader-starts-cold", 2),), (("quota", 2),)))

    def test_messages_mode_runs_only_the_dry_sample_cli_with_a_derived_wait(self) -> None:
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        paths = [self.root / "one.jsonl", self.root / "two.jsonl"]
        with (mock.patch.object(nightly_review, "showrunner_transcripts", return_value=paths),
              mock.patch.object(nightly_review, "live_message_counts", return_value=nightly_review.LiveMessageCounts(0, 0, 0, ())),
              mock.patch.object(review, "user_send_seconds", return_value=115),
              mock.patch.object(subprocess, "run", return_value=done("Sampled: 1; failures: 0; Unreviewed: 0; unregistered questions: 1\n")) as run):
            lines = nightly_review.messages(since)
        self.assertIn("unregistered questions: 1", lines[-1])
        self.assertEqual(run.call_args.args[0], [sys.executable, str(nightly_review.REVIEW_SCRIPT), "sample",
                         *(str(path) for path in paths), "--limit", "20", "--since", since.isoformat()])
        self.assertEqual(run.call_args.kwargs["timeout"], 2315)

    def test_review_settings_deny_edits_under_the_main_folder_and_every_account_folder(self) -> None:
        settings = cast(dict[str, dict[str, list[str]]], json.loads(nightly_review.SETTINGS.read_text(encoding="utf-8")))
        denied = settings["permissions"]["deny"]

        self.assertIn("Edit(~/.claude/**)", denied)
        for label in ("claude1", "claude2", "claude3"):
            self.assertIn(f"Edit(~/{accounts.account_dir(label).name}/**)", denied)



class TonightTests(NightlyReviewTests):
    def tonight(self, units: list[LiveClaude | None], runs: list[subprocess.CompletedProcess[str]],
                production: nightly_review.NightlyProduction | str = PRODUCTION) -> tuple[list[str], list[list[str]]]:
        with (mock.patch.object(nightly_review, "nightly_production", return_value=production),
              mock.patch.object(nightly_review, "live_unit", side_effect=units),
              mock.patch.object(subprocess, "run", side_effect=runs) as run):
            lines = nightly_review.launch(NIGHT)
        return lines, [cast(list[str], call.args[0]) for call in run.call_args_list]

    def test_a_live_unit_is_sent_the_nights_job_under_the_nights_key(self) -> None:
        lines, calls = self.tonight([UNIT], [done()])
        self.assertEqual(lines, [f"{SESSION}: tonight's review sent"])
        self.assertEqual(calls, [[sys.executable, str(nightly_review.SCRIPTS / "message/send.py"), "--to", f"uds:{UNIT.socket}",
                                  "--from", "nightly-review timer", "--key", "nightly-2026-09-29",
                                  "--text", nightly_review.job(NIGHT)]])
        self.assertIn("NIGHT=2026-09-29", nightly_review.job(NIGHT))
        self.assertEqual((self.root / "nights/2026-09-29/launch.log").read_text(), f"{SESSION}: tonight's review sent\n")

    def test_a_gone_unit_is_started_through_add_unit_as_permanent_then_sent(self) -> None:
        lines, calls = self.tonight([None, UNIT], [done(), done()])
        self.assertEqual(lines, [f"{SESSION}: started", f"{SESSION}: tonight's review sent"])
        self.assertEqual(calls[0], [sys.executable, str(nightly_review.SCRIPTS / "production/add_unit.py"),
                                    "--production", str(DOC), SESSION, "--permanent"])
        self.assertEqual(calls[1][calls[1].index("--to") + 1], f"uds:{UNIT.socket}")

    def test_a_refused_start_names_add_units_reason_and_sends_nothing(self) -> None:
        lines, calls = self.tonight([None], [done(code=1, stderr="claude3 is shut down")])
        self.assertEqual(lines, [f"{SESSION}: not started: stderr: claude3 is shut down"])
        self.assertEqual(len(calls), 1)

    def test_a_night_with_no_production_holding_the_unit_is_skipped(self) -> None:
        lines, calls = self.tonight([], [], production="no running production has a permanent nightly-review-unit row")
        self.assertEqual(lines, ["skipped: no running production has a permanent nightly-review-unit row"])
        self.assertEqual(calls, [])

    def test_a_failed_send_is_reported(self) -> None:
        lines, _ = self.tonight([UNIT], [done(code=1, stderr="socket gone")])
        self.assertEqual(lines, [f"{SESSION}: tonight's review not sent: socket gone"])


class NightlyProductionTests(NightlyReviewTests):
    def doc(self, name: str, plan: str) -> str:
        path = self.root / f"{name}.md"
        _ = path.write_text("\n".join(["# production", "", "## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                       "| --- | --- | --- | --- | --- | --- |",
                                       f"| nightly-review-unit | {plan} | `/w/{name}` | `{name}` | 0 | nightly |", ""]))
        return str(path)

    def runners(self, *docs: tuple[str, str]) -> list[showrunners.Showrunner]:
        return [showrunners.Showrunner(session="", socket="", slug=slug, zone="", doc=doc) for slug, doc in docs]

    def test_the_production_with_the_permanent_row_is_found(self) -> None:
        listed = self.runners(("one", self.doc("one", "standby")), ("two", self.doc("two", "permanent")))
        with mock.patch.object(showrunners, "registered_showrunners", return_value=listed):
            self.assertEqual(nightly_review.nightly_production(),
                             nightly_review.NightlyProduction(self.root / "two.md", "two"))

    def test_no_permanent_row_and_two_permanent_rows_are_each_named(self) -> None:
        with mock.patch.object(showrunners, "registered_showrunners", return_value=self.runners(("one", self.doc("one", "standby")))):
            self.assertIn("no running production", cast(str, nightly_review.nightly_production()))
        twice = self.runners(("one", self.doc("one", "permanent")), ("two", self.doc("two", "permanent")))
        with mock.patch.object(showrunners, "registered_showrunners", return_value=twice):
            self.assertEqual(nightly_review.nightly_production(), "2 productions hold nightly-review-unit: one, two")


if __name__ == "__main__":
    _ = unittest.main()
