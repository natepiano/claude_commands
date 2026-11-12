"""The nightly review: its night's job reaches the permanent unit, and its message counts."""

from __future__ import annotations

from scripts.agents import remote

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from contextlib import redirect_stderr
from pathlib import Path
from scripts.lib.transcript_test_support import compressed_copy
from typing import cast, override
from unittest import mock



from scripts.nightly_review import nightly_review
from scripts.whoami import account
from scripts.accounts import accounts
from scripts.production import showrunners, staff, fake_showrunner, session_staff, unit_config
from scripts.message import sessions
from scripts.production.staff_test_support import (isolated_staff_file, isolated_source_readers,
                                                    source_environment, recorded_unit, fixture_member,
                                                    fixture_runtime_id)
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.notify import review
from scripts.production.unit_lookup import LiveClaude

NIGHT = date(2026, 9, 29)
SESSION = "nightly-review"
DOC = Path("/work/docs/plans/build-followups-production.md")
UNIT = LiveClaude("nightly-review", "aaaaaaaa-0000-0000-0000-000000000001", "/run/cc-socks/42.sock", 42)
PRODUCTION = nightly_review.NightlyProduction(DOC, "build-followups", nightly_review.NightlyUnit(
    staff.StaffRow(staff.StaffId(UNIT.session_id), "unit", SESSION, staff.StaffRoot(),
                   current_session=staff.StaffId(UNIT.session_id),
                   worktree=staff.PresentWorktree(remote.local_machine(), DOC.parents[1]), unit_type=staff.UnitDetails("direct")),
    unit_config.UnitConfig(unit_config.PermanentPlan(), unit_config.NoAppPort(), ("nightly-review",)),
))


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
        configuration = self.root / "config"
        configuration.mkdir()
        for stem in ("nightly_review", "accounts", "nix_env"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{stem}.conf", configuration / f"{stem}.conf")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        environment_context: object = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(home)})))
        del environment_context
        _ = self.enterContext(mock.patch.object(nightly_review, "state_root", return_value=self.root / "nights"))
        environment = source_environment(self.root / "sources")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", environment))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 2, "rows": []}))
        _ = self.enterContext(isolated_source_readers())

    def transcript_owner(self, name: str) -> str:
        doc = self.root / (name + ".md")
        _ = doc.write_text("# Transcript owner\n")
        row = fixture_member("showrunner", name=name, runtime_id=fixture_runtime_id(name),
                             doc=staff.LiveDoc(remote.local_machine(), doc))
        return row.current_session

    def test_timer_runs_messages_after_rust(self) -> None:
        self.assertEqual(conf.read("nightly_review").words("review_modes"), ("config", "rust", "messages"))
        self.assertEqual(nightly_review.job(NIGHT), "Nightly review for NIGHT=2026-09-29: follow ~/.claude/commands/nightly_review.md with the modes config rust messages, in that order.")


    def test_copied_nightly_library_import_needs_no_message_review_or_production_modules(self) -> None:
        copied = self.root / "copy/scripts/nightly_review/nightly_review.py"
        copied.parent.mkdir(parents=True)
        _ = shutil.copyfile(nightly_review.__file__, copied)
        library = copied.parents[1] / "lib"
        library.mkdir()
        for name in ("process_failure.py", "package_root.py", "transcripts.py"):
            _ = shutil.copyfile(Path(nightly_review.__file__).resolve().parents[1] / "lib" / name, library / name)
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
            identity = self.transcript_owner(name)
            config = self.root / f"account-{index}"
            path = config / "projects/-work" / f"{identity}.jsonl"
            path.parent.mkdir(parents=True)
            _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
                type="assistant", timestamp=since.isoformat())) + "\n")
            paths.append(path)
            record = nightly_review.TranscriptSessionRecord(sessionId=identity, name=name,
                cwd="/work", config_dir=str(config))
            if index == 0:
                record["transcript_path"] = str(path)
                record["cwd"] = "/old-work"
            _ = (sessions / f"{index}.json").write_text(json.dumps(record))
        def production_of(sid: str) -> Path | None:
            return self.root if sid == "sid-0" else None
        with (mock.patch.object(nightly_review, "sessions_directory", return_value=sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
              mock.patch.object(showrunners, "production_of", side_effect=production_of),
              mock.patch.object(account, "process_config_dir", side_effect=AssertionError("recorded account lost"))):
            self.assertEqual(nightly_review.showrunner_transcripts(since), sorted(paths))
            compressed = [compressed_copy(path) for path in paths]
            self.assertEqual(nightly_review.showrunner_transcripts(since), sorted(compressed))

    def test_transcripts_keep_the_showrunners_earlier_native_session_in_the_window(self) -> None:
        registry = self.root / "sessions"
        registry.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        original = staff.StaffId(self.transcript_owner("review-owner"))
        earlier = staff.StaffId(fixture_runtime_id("review-owner-middle-session"))
        _ = staff.update_current_session(original, earlier)
        _ = staff.update_current_session(earlier, staff.StaffId(fixture_runtime_id("review-owner-restarted")))
        transcript = self.root / "earlier.jsonl"
        _ = transcript.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=since.isoformat())) + "\n")
        _ = (registry / "earlier.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            sessionId=earlier, name="review-owner", transcript_path=str(transcript))))
        unrelated = self.root / "unrelated.jsonl"
        _ = unrelated.write_text(transcript.read_text())
        _ = (registry / "unrelated.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            sessionId=fixture_runtime_id("unrelated"), name="unrelated", transcript_path=str(unrelated))))
        with mock.patch.object(nightly_review, "sessions_directory", return_value=registry):
            found = nightly_review.showrunner_transcripts(since)
        self.assertEqual(found, [transcript])

    def test_transcript_selection_uses_reply_time_instead_of_recent_file_write(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        identity = self.transcript_owner("natedev")
        path = self.root / "old.jsonl"
        _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=(since - timedelta(days=2)).isoformat())) + "\n")
        _ = (sessions / "1.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            name="natedev", sessionId=identity, transcriptPath=str(path))))
        with (mock.patch.object(nightly_review, "sessions_directory", return_value=sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[])):
            self.assertEqual(nightly_review.showrunner_transcripts(since), [])

    def test_transcript_lookup_uses_the_sessions_process_account(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        identity = self.transcript_owner("natedev")
        config = self.root / "showrunner-account"
        path = config / "projects/-work" / f"{identity}.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=since.isoformat())) + "\n")
        _ = (sessions / "1.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            name="natedev", sessionId=identity, cwd="/work", pid=42)))
        with (mock.patch.object(nightly_review, "sessions_directory", return_value=sessions),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
              mock.patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(config)) as directory):
            self.assertEqual(nightly_review.showrunner_transcripts(since), [path])
        directory.assert_called_once_with(42)

    def test_transcript_lookup_uses_session_id_after_the_folder_changes(self) -> None:
        records = self.root / "sessions"
        records.mkdir()
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        identity = self.transcript_owner("natedev")
        config = self.root / "account"
        path = config / "projects/-previous-folder" / f"{identity}.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(nightly_review.DatedTranscriptRecord(
            type="assistant", timestamp=since.isoformat())) + "\n")
        _ = (records / "probe.json").write_text(json.dumps(nightly_review.TranscriptSessionRecord(
            name="natedev", sessionId=identity, cwd="/current/folder", config_dir=str(config))))
        unrelated = path.with_name(f"{fixture_runtime_id('unrelated-transcript')}.jsonl")
        _ = unrelated.write_text(path.read_text())
        with (mock.patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}),
              mock.patch.object(nightly_review, "sessions_directory", return_value=records),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[])):
            self.assertEqual(nightly_review.showrunner_transcripts(since), [path])

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
        _ = path.write_text(json.dumps(records[0]) + "\n")
        self.assertEqual(nightly_review.live_message_counts(path, since),
                         nightly_review.LiveMessageCounts(1, 0, 0, ()))
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
                                    "--production", "build-followups", SESSION, "--permanent"])
        self.assertEqual(calls[1][calls[1].index("--to") + 1], f"uds:{UNIT.socket}")

    def test_a_refused_start_names_add_units_reason_and_sends_nothing(self) -> None:
        lines, calls = self.tonight([None], [done(code=1, stderr="claude3 is shut down")])
        self.assertEqual(lines, [f"{SESSION}: not started: stderr: claude3 is shut down"])
        self.assertEqual(len(calls), 1)

    def test_a_night_with_no_production_holding_the_unit_is_skipped(self) -> None:
        lines, calls = self.tonight([], [], production="no running production has a permanent nightly-review row")
        self.assertEqual(lines, ["skipped: no running production has a permanent nightly-review row"])
        self.assertEqual(calls, [])

    def test_a_failed_send_is_reported(self) -> None:
        lines, _ = self.tonight([UNIT], [done(code=1, stderr="socket gone")])
        self.assertEqual(lines, [f"{SESSION}: tonight's review not sent: socket gone"])


class NightlyProductionTests(NightlyReviewTests):
    def doc(self, name: str, plan: str) -> str:
        path = self.root / f"{name}.md"
        _ = path.write_text("# production\n")
        tree = self.root / (name + "-unit")
        unit = recorded_unit(name + "-owner", "nightly-review", tree, path, plan=plan)
        responsibilities = '["nightly-review"]' if plan == "permanent" else "[]"
        _ = (tree / ".unit.toml").write_text(f'plan = "{plan}"\nresponsibilities = {responsibilities}\n')
        parent = staff.snapshot().parent_of(unit)
        if parent is None:
            self.fail("the nightly fixture unit lost its parent")
        _ = fake_showrunner.write_timer(Path(os.environ["NOTIFIER_STATE_DIR"]), parent.name,
                                         parent.current_session, "UTC", path)
        return str(path)

    def runners(self, *docs: tuple[str, str]) -> list[showrunners.Showrunner]:
        return [showrunners.Showrunner(session="", socket="", name=name, zone="", doc=doc) for name, doc in docs]

    def test_configuration_responsibility_wins_over_the_permanent_plan(self) -> None:
        _ = self.doc("old-alias", "permanent")
        destination = Path(self.doc("new-alias", "standby"))
        _ = (self.root / "old-alias-unit/.unit.toml").write_text('plan = "permanent"\nresponsibilities = []\n')
        _ = (self.root / "new-alias-unit/.unit.toml").write_text('plan = "standby"\nresponsibilities = ["nightly-review"]\n')
        self.assertEqual(nightly_review.nightly_production(),
                         nightly_review.NightlyProduction(destination, "new-alias-owner"))

    def test_stopped_responsibility_holder_starts_its_own_name_and_configured_launch_mode(self) -> None:
        _ = self.doc("review-alias", "standby")
        member = next(row for row in staff.snapshot().find(kind="unit"))
        member = staff.rename(member.id, "auditor")
        live: sessions.SessionRecord = {
            "pid": 42, "sessionId": member.current_session, "name": "review-auditor",
            "messagingSocketPath": "/private/auditor.sock", "updatedAt": 1, "tmux": "", "formerNames": [],
        }
        for plan, launch in (("standby", ["--standby"]), ("permanent", ["--permanent"]),
                             ("docs/review.md", ["--plan", "docs/review.md"])):
            with self.subTest(plan=plan):
                _ = (self.root / "review-alias-unit/.unit.toml").write_text(
                    f'plan = {json.dumps(plan)}\nresponsibilities = ["nightly-review"]\n')
                with (mock.patch.object(sessions, "live_sessions", side_effect=[[], [live]]),
                      mock.patch.object(subprocess, "run", return_value=done()) as run):
                    lines = nightly_review.tonight(NIGHT)
                calls = [cast(list[str], call.args[0]) for call in run.call_args_list]
                self.assertEqual(calls[0], [sys.executable, str(nightly_review.SCRIPTS / "production/add_unit.py"),
                                           "--production", "review-alias-owner", "auditor", *launch])
                self.assertEqual(len(calls), 2, calls)
                self.assertEqual(calls[1][calls[1].index("--to") + 1], "uds:/private/auditor.sock")
                self.assertIn("tonight's review sent", lines[-1])

    def test_missing_candidate_configuration_prevents_a_false_unique_nightly_owner(self) -> None:
        _ = self.doc("one", "permanent")
        _ = self.doc("two", "standby")
        (self.root / "two-unit/.unit.toml").unlink()
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(OSError):
            _ = nightly_review.nightly_production()
        self.assertIn("nightly-review", errors.getvalue())
        self.assertIn(".unit.toml", errors.getvalue())

    def test_unreadable_candidate_document_prevents_a_false_unique_nightly_owner(self) -> None:
        _ = self.doc("one", "permanent")
        Path(self.doc("two", "permanent")).unlink()
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(OSError):
            _ = nightly_review.nightly_production()
        self.assertIn("nightly-review", errors.getvalue())
        self.assertIn("two.md", errors.getvalue())

    def test_malformed_unrelated_configuration_reports_once_and_keeps_ownership_unknown(self) -> None:
        _ = self.doc("one", "permanent")
        _ = self.doc("other", "standby")
        unrelated = next(row for row in staff.snapshot().find(kind="unit")
                         if isinstance(row.worktree, staff.PresentWorktree)
                         and row.worktree.path == self.root / "other-unit")
        _ = staff.rename(unrelated.id, "unrelated")
        _ = (self.root / "other-unit/.unit.toml").write_text('plan = 42\nresponsibilities = []\n')
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(OSError):
            _ = nightly_review.nightly_production()
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("unrelated", lines[0])

    def test_delivery_uses_the_selection_without_rereading_unrelated_configurations(self) -> None:
        _ = self.doc("other", "standby")
        _ = self.doc("review-alias", "permanent")
        snapshot = staff.snapshot()
        selected = next(row for row in snapshot.find(kind="unit")
                        if isinstance(row.worktree, staff.PresentWorktree)
                        and row.worktree.path == self.root / "review-alias-unit")
        live: sessions.SessionRecord = {
            "pid": 42, "sessionId": selected.current_session, "name": "review-worker",
            "messagingSocketPath": "/private/review.sock", "updatedAt": 1, "tmux": "", "formerNames": [],
        }
        reads: dict[staff.StaffId, int] = {}
        configuration = session_staff.RowResolver.unit_config

        def read_configuration(resolver: session_staff.RowResolver, row: staff.StaffRow) -> object:
            reads[row.id] = reads.get(row.id, 0) + 1
            if row.id != selected.id and reads[row.id] > 1:
                raise unit_config.ConfigRefused("unrelated configuration changed after selection")
            return configuration(resolver, row)

        with (mock.patch.object(session_staff.RowResolver, "unit_config", autospec=True, side_effect=read_configuration),
              mock.patch.object(sessions, "live_sessions", return_value=[live]),
              mock.patch.object(subprocess, "run", return_value=done()) as run):
            lines = nightly_review.tonight(NIGHT)
        calls = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][calls[0].index("--to") + 1], "uds:/private/review.sock")
        self.assertIn("tonight's review sent", lines[-1])
        self.assertEqual(set(reads.values()), {1})

    def test_unknown_production_cannot_make_the_nightly_owner_appear_unique(self) -> None:
        healthy = self.runners(("one", self.doc("one", "permanent")))
        partial = showrunners.ShowrunnerDiscovery(healthy, (
            showrunners.ShowrunnerLookupFailure("missing", "fixture lookup failed"),))
        with (mock.patch.object(showrunners, "registered_showrunners", return_value=partial),
              mock.patch.object(staff, "snapshot", side_effect=OSError("fixture lookup failed"))):
            with self.assertRaisesRegex(OSError, "fixture lookup failed"):
                _ = nightly_review.nightly_production()

    def test_the_production_with_the_permanent_row_is_found(self) -> None:
        listed = self.runners(("one", self.doc("one", "standby")), ("two", self.doc("two", "permanent")))
        with mock.patch.object(showrunners, "registered_showrunners", return_value=listed):
            self.assertEqual(nightly_review.nightly_production(),
                             nightly_review.NightlyProduction(self.root / "two.md", "two-owner"))

    def test_no_permanent_row_and_two_permanent_rows_are_each_named(self) -> None:
        with mock.patch.object(showrunners, "registered_showrunners", return_value=self.runners(("one", self.doc("one", "standby")))):
            self.assertIn("no running production", cast(str, nightly_review.nightly_production()))
        twice = self.runners(("one", self.doc("one", "permanent")), ("two", self.doc("two", "permanent")))
        with mock.patch.object(showrunners, "registered_showrunners", return_value=twice):
            self.assertEqual(nightly_review.nightly_production(), "2 productions hold nightly-review: one-owner, two-owner")


if __name__ == "__main__":
    _ = unittest.main()
