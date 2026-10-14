"""Launch and quota gate of the nightly review."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

import nightly_review
import account
import launch_permission
import agent_notes
import note_file
from scripts.accounts import accounts, refusals
from scripts.herdr import herdr
import usage_store
from scripts.production import showrunners
if TYPE_CHECKING:
    from scripts.notify import review
else:
    import review

REAL_TMUX = nightly_review.tmux
NIGHT = date(2026, 9, 29)
SESSION = "nightly-review"
STARTED = [f"{SESSION}: started"]
UNIT_LOGIN = "unit@example.com"


def setUpModule() -> None:
    """The times these tests expect are Pacific; neither the user's zone setting nor a showrunner
    registered on the machine running them may change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))
    no_showrunners = unittest.enterModuleContext(tempfile.TemporaryDirectory())
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", Path(no_showrunners)))


class FakeNote:
    def __init__(self, stem: str, fields: dict[str, str]) -> None:
        self.path: Path = Path(f"/vault/agents/{stem}.md")
        self.tool: str = stem.split()[0]
        self.fields: dict[str, str] = fields

    def get(self, key: str) -> str | None:
        return self.fields.get(key)


def done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


class NightlyReviewTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.live: set[str] = set()
        self.attached: set[str] = set()
        self.foreign: set[str] = set()
        self.calls: list[list[str]] = []
        self.state: Path = Path()
        self.notes: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home = self.root / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        _ = (home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                        "projects": {str(home / "rust"): {"hasTrustDialogAccepted": True}}}),
            encoding="utf-8",
        )
        notes = self.root / "notes"
        notes.mkdir()
        self.notes = notes
        _ = self.enterContext(mock.patch.object(note_file, "AGENTS_DIR", notes))
        _ = self.enterContext(mock.patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl"))
        _ = (notes / "claude 2.md").write_text(
            "---\nlogin: owner@example.com\nstate: active\n---\n",
            encoding="utf-8",
        )
        self.state = self.root / "shutdown"
        environment_context: object = cast(
            object,
            self.enterContext(
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "AGENT_NOTES_DIR": str(notes),
                        "SHUTDOWN_STATE_DIR": str(self.state),
                    },
                )
            ),
        )
        del environment_context
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _ = self.enterContext(mock.patch.object(herdr, "run_program", return_value=done(code=1)))
        self.quota_reading("owner@example.com", 80, 7)
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "default"}))
        for name, value in (("ROOT", self.root), ("tmux", self.tmux)):
            patcher = mock.patch.object(nightly_review, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)

    def quota_reading(self, login: str, left: float, reset_days: float) -> None:
        file = Path.home() / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text())) if file.exists() else {}
        now = datetime.now(timezone.utc)
        saved[login] = {"weekly": {"left": left, "at": now.isoformat(),
                                  "resets": (now + timedelta(days=reset_days)).isoformat()}}
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

    def test_picked_nightly_account_with_one_percent_room_launches(self) -> None:
        self.quota_reading("owner@example.com", 1, 7)
        self.assertEqual(self.launched(self.account_notes("1", "90")), STARTED)
        self.assertTrue(self.calls)

    def test_nightly_refuses_unconfigured_units_without_fallback_or_group_write(self) -> None:
        """F004: nightly launch requires the same configured units account."""
        accounts.groups_file().unlink()
        lines = self.launched(self.account_notes("80", "90"))
        self.assertEqual(len(lines), 1)
        self.assertIn("accounts.py move units <claude N>", lines[0])
        self.assertEqual(self.calls, [])
        self.assertFalse(accounts.groups_file().exists())

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        verb, name = args[0], args[-1].removeprefix("=").removesuffix(":")
        if verb == "has-session":
            return done(code=0 if name in self.live else 1)
        if verb == "list-clients":
            return done(f"/dev/pts/3: {name}\n" if name in self.attached else "")
        if verb == "list-panes":
            return done("zsh -ic \"claude --resume x\"\n" if name in self.foreign
                        else f"zsh -ic \"claude '{nightly_review.PROMPT}'\"\n")
        self.live.discard(name)
        return done()

    def launched(self, notes: list[FakeNote]) -> list[str]:
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(command)
            return done()

        with (
            mock.patch("note_file.read_notes", return_value=notes),
            mock.patch.object(
                nightly_review,
                "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ),
            mock.patch("subprocess.run", run),
        ):
            return nightly_review.launch(NIGHT)

    def unit_account(self, label: str) -> Path:
        """Name `label` as the unit role's account, with a directory fit to launch."""
        directory = self.unit_role(label)
        self.assertEqual(accounts.setup(label), [])
        _ = (directory / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": UNIT_LOGIN},
                        "projects": {str(Path.home() / "rust"): {"hasTrustDialogAccepted": True}}}),
            encoding="utf-8",
        )
        _ = (self.notes / f"{label}.md").write_text(
            f"---\nlogin: {UNIT_LOGIN}\nstate: active\n---\n", encoding="utf-8",
        )
        self.quota_reading(UNIT_LOGIN, 1, 2)
        return directory

    def unit_role(self, label: str) -> Path:
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": label}))
        return accounts.account_dir(label)

    def write_shutdown_record(self, value: dict[str, object]) -> None:
        account = self.state / cast(str, value["login"])
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_timer_runs_messages_after_rust(self) -> None:
        self.assertEqual(nightly_review.MODES, ("config", "rust", "messages"))
        self.assertEqual(nightly_review.PROMPT, "/nightly_review config rust messages")

    def test_untrusted_default_folder_refuses_before_retiring_or_spawning(self) -> None:
        _ = (Path.home() / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": {str(Path.home()): {"hasTrustDialogAccepted": True}}}))
        self.live = {SESSION, *nightly_review.LEGACY_SESSIONS}
        folder = Path.home() / "rust"
        self.assertEqual(self.launched([]), [
            f"{SESSION}: Claude is not trusted in {folder} on default account. "
            + f"Run: cd {shlex.quote(str(folder))} && "
            + "env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR claude",
        ])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.live, {SESSION, *nightly_review.LEGACY_SESSIONS})

    def test_trust_reads_the_named_launch_account(self) -> None:
        directory = self.unit_account("claude 3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": UNIT_LOGIN}, "projects": {}}))
        lines = self.launched([])
        self.assertEqual(len(lines), 1)
        self.assertIn(f"{SESSION}: Claude is not trusted in {Path.home() / 'rust'} on claude 3. Run:", lines[0])
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", lines[0])
        self.assertEqual(self.calls, [])

    def test_picked_launch_checks_default_trust_despite_a_trusted_inherited_account(self) -> None:
        directory = self.root / "inherited account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "projects": {str(Path.home() / "rust"): {"hasTrustDialogAccepted": True}}}))
        _ = (Path.home() / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"}, "projects": {}}))
        self.live = {SESSION, *nightly_review.LEGACY_SESSIONS}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory)}):
            lines = self.launched([])
        folder = Path.home() / "rust"
        self.assertEqual(lines, [
            f"{SESSION}: Claude is not trusted in {folder} on default account. "
            + f"Run: cd {shlex.quote(str(folder))} && "
            + "env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR claude",
        ])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.live, {SESSION, *nightly_review.LEGACY_SESSIONS})

    def test_picked_launch_ignores_untrusted_inherited_account_and_preserves_checked_environment(self) -> None:
        directory = self.root / "inherited account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({"projects": {}}))
        with (
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory)}),
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "launch_permission",
                              return_value=launch_permission.AllowedByShutdownState()) as permission,
            mock.patch("subprocess.run", return_value=done()) as spawn,
        ):
            self.assertEqual(nightly_review.launch(NIGHT), STARTED)
        spawn.assert_called_once()
        environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        self.assertEqual(environment, permission.call_args.args[0])
        self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
        command = cast(list[str], spawn.call_args.args[0])[-1]
        self.assertEqual(command, "unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; "
                         + f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh")

    def test_unreadable_trust_file_refuses_without_contents(self) -> None:
        selected = accounts.role_account("unit")
        config = Path.home() / ".claude.json"
        _ = config.write_text('{"oauthAccount": "SECRET-EMAIL", ')
        with mock.patch.object(accounts, "role_account", return_value=selected):
            lines = self.launched([])
        self.assertEqual(lines, [f"{SESSION}: cannot read {config} to check Claude trust (JSONDecodeError)"])
        self.assertNotIn("SECRET", lines[0])
        self.assertEqual(self.calls, [])

    def test_copied_nightly_library_import_needs_no_message_review_or_production_modules(self) -> None:
        copied = self.root / "copy/scripts/nightly_review/nightly_review.py"
        copied.parent.mkdir(parents=True)
        _ = shutil.copyfile(nightly_review.__file__, copied)
        whoami = copied.parents[1] / "whoami"
        shutdown = copied.parents[1] / "shutdown"
        whoami.mkdir()
        shutdown.mkdir()
        _ = (whoami / "account.py").write_text("NAMED_ACCOUNT_VARIABLES = []\n")
        _ = (whoami / "agent_accounts.py").write_text("class ClaudeAccountsInUse: pass\n")
        _ = (shutdown / "launch_permission.py").write_text(
            "class BlockedByShutdown: pass\nclass NewWorkLaunchPurpose: pass\nclass ShutdownStateUnreadable: pass\n"
            + "def launch_barrier(): pass\ndef launch_permission(*args): pass\n")
        result = subprocess.run([sys.executable, "-I", "-c",
                                 "import runpy,sys; runpy.run_path(sys.argv[1], run_name='copied_library')", str(copied)],
                                cwd=self.root, capture_output=True, text=True, check=False, timeout=10)
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
              mock.patch.object(account, "process_config_dir", return_value=config) as directory):
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

    def down_record(self, login: str = "owner@example.com", label: str = "claude 2") -> dict[str, object]:
        return {
            "login": login,
            "label": label,
            "machine": "natedev",
            "state": "down",
            "requested_at": "2026-10-09T21:49:10+00:00",
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"},
            "force": "wait for ready",
            "entries": [],
            "stop_issues": [],
        }

    def test_review_settings_deny_edits_under_the_main_folder_and_every_account_folder(self) -> None:
        settings = cast(dict[str, dict[str, list[str]]], json.loads(nightly_review.SETTINGS.read_text(encoding="utf-8")))
        denied = settings["permissions"]["deny"]

        self.assertIn("Edit(~/.claude/**)", denied)
        for label in ("claude 1", "claude 2", "claude 3"):
            self.assertIn(f"Edit(~/{accounts.account_dir(label).name}/**)", denied)

    def review_command(self) -> str:
        """The Claude command line every launch ends with."""
        return shlex.join([
            "claude", "--remote-control", SESSION, "-n", SESSION,
            "--add-dir", "/etc/nixos", "--add-dir", str(Path.home() / ".claude"),
            "--settings", str(nightly_review.SETTINGS), "/nightly_review config rust messages",
        ])

    def launched_environment(self, caller: dict[str, str]) -> tuple[dict[str, str], mock.MagicMock, mock.MagicMock]:
        """Launch as `caller`, then run the launched command as a tmux server holding `caller` would."""
        binary = self.root / "bin"
        binary.mkdir()
        log = self.root / "claude-environment.json"
        claude = binary / "claude"
        _ = claude.write_text(
            "#!/usr/bin/env python3\n"
            + "import json, os\nfrom pathlib import Path\n"
            + "Path(os.environ['NIGHTLY_ENV_LOG']).write_text(json.dumps(dict(os.environ)))\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        shell = binary / "zsh"
        _ = shell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shell.chmod(0o755)
        real_shell = shutil.which("zsh")
        if real_shell is None:
            self.skipTest("zsh is required to check the launched command")
        caller_environment = {
            **os.environ,
            **caller,
            "NIGHTLY_ENV_LOG": str(log),
            "PATH": f"{binary}:/usr/bin:/bin",
            "ZDOTDIR": str(Path.home()),
        }
        with (
            mock.patch.dict(os.environ, caller_environment),
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "launch_permission",
                              return_value=launch_permission.AllowedByShutdownState()) as permission,
            mock.patch("subprocess.run", return_value=done()) as spawn,
        ):
            self.assertEqual(nightly_review.launch(NIGHT), STARTED)
        spawn.assert_called_once()
        argv = cast(list[str], spawn.call_args.args[0])
        # An existing tmux server supplies its own environment, independently
        # of the sanitized environment used to invoke the tmux client.
        result = subprocess.run(
            [real_shell, "-ic", argv[-1]], env=caller_environment,
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        launched = cast(dict[str, str], json.loads(log.read_text(encoding="utf-8")))
        return launched, permission, spawn

    def test_launch_starts_one_session_with_both_modes_in_the_prompt(self) -> None:
        self.assertEqual(self.launched([]), STARTED)
        self.assertEqual(len(self.calls), 1)
        command = self.calls[0]
        self.assertEqual(command[command.index("-s") + 1], SESSION)
        self.assertEqual(command[command.index("-c") + 1], str(Path.home() / "rust"))
        self.assertEqual(command[-1], "unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; " + f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh")
        self.assertTrue(command[-1].endswith("'/nightly_review config rust messages'; exec zsh"))
        self.assertEqual((self.root / NIGHT.isoformat() / "launch.log").read_text(), f"{SESSION}: started\n")

    def test_last_night_is_replaced_unless_the_user_is_attached(self) -> None:
        self.live = {SESSION}
        self.assertEqual(self.launched([]), STARTED)
        self.assertNotIn(SESSION, self.live)
        self.assertEqual(len(self.calls), 1)

        self.live = {SESSION}
        self.attached = {SESSION}
        self.assertEqual(self.launched([]), [
            f"{SESSION}: you are attached, so last night's session was left running",
        ])
        self.assertIn(SESSION, self.live)
        self.assertEqual(len(self.calls), 1)

    def test_a_session_no_review_started_is_left_running(self) -> None:
        self.live = {SESSION}
        self.foreign = {SESSION}
        self.assertEqual(self.launched([]), [
            f"{SESSION}: another session holds this name, so tonight's review did not start",
        ])
        self.assertIn(SESSION, self.live)
        self.assertEqual(self.calls, [])

    def test_sessions_of_the_two_session_nights_are_retired_under_the_same_guards(self) -> None:
        self.live = {"nightly-config", "nightly-rust"}
        self.assertEqual(self.launched([]), STARTED)
        self.assertEqual(self.live, set())

        self.live = {"nightly-config", "nightly-rust"}
        self.attached = {"nightly-rust"}
        self.foreign = {"nightly-config"}
        self.assertEqual(self.launched([]), [
            "nightly-config: another session holds this name, so it was left running",
            "nightly-rust: you are attached, so it was left running",
            f"{SESSION}: started",
        ])
        self.assertEqual(self.live, {"nightly-config", "nightly-rust"})
        self.assertEqual(len(self.calls), 2)

    def test_low_note_reading_does_not_override_picked_room(self) -> None:
        self.assertEqual(self.launched(self.account_notes("4", "90")), STARTED)
        self.assertTrue(self.calls)

    def test_quota_permission_allows_zero_reading_until_account_refuses(self) -> None:
        self.quota_reading("owner@example.com", 1, 7)
        self.assertIsInstance(nightly_review.quota_permission(), nightly_review.QuotaClear)
        self.quota_reading("owner@example.com", 0, 7)
        self.assertIsInstance(nightly_review.quota_permission(), nightly_review.QuotaClear)
        now = datetime.now(timezone.utc)
        refusals.write_refusal(refusals.AccountRefusal("claude", "owner@example.com",
            refusals.KnownWindow("weekly"), refusals.KnownReset(now + timedelta(days=7)), now))
        refused = nightly_review.quota_permission()
        self.assertIsInstance(refused, nightly_review.ReviewAccountUnavailable)
        assert isinstance(refused, nightly_review.ReviewAccountUnavailable)
        self.assertIn("no claude account has room", refused.reason)

    def account_notes(self, default_left: str, unit_left: str) -> list[FakeNote]:
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        return [
            FakeNote("claude 3", {"login": UNIT_LOGIN, "state": "active",
                                  "weekly_remaining_usage": unit_left, "resets": resets}),
            FakeNote("claude 2", {"login": "OWNER@example.com", "state": "active",
                                  "weekly_remaining_usage": default_left, "resets": resets}),
        ]

    def test_nightly_launch_picks_low_room_account_resetting_before_default(self) -> None:
        _ = self.unit_account("claude 3")
        self.assertEqual(self.launched(self.account_notes("80", "1")), STARTED)
        self.assertIn(str(accounts.account_dir("claude 3")), self.calls[-1][-1])

    def test_nightly_ignores_named_callers_low_note_and_uses_picked_default(self) -> None:
        named = Path.home() / ".claude3"
        named.mkdir()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(named)}):
            self.assertEqual(self.launched(self.account_notes("1", "0")), STARTED)
        self.assertNotIn(str(named), self.calls[-1][-1])

    def test_nightly_gate_uses_the_supplied_identities_for_the_whole_launch(self) -> None:
        """Nightly does not independently resolve a login after the snapshot."""
        (Path.home() / ".claude3").mkdir()
        selection = accounts.claude_account_selection()
        _ = (Path.home() / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": UNIT_LOGIN},
            "projects": {str(Path.home() / "rust"): {"hasTrustDialogAccepted": True}},
        }), encoding="utf-8")
        notes = self.account_notes(default_left="80", unit_left="2")
        with mock.patch.object(accounts, "claude_account_selection", return_value=selection) as resolve:
            self.assertEqual(self.launched(notes), STARTED)
        resolve.assert_called_once_with()

    def test_nightly_refuses_unread_configured_account_without_fallback(self) -> None:
        """F004: launch configuration cannot be replaced by another readable account."""
        _ = self.unit_role("claude 3")
        lines = self.launched(self.account_notes("80", "0"))
        self.assertNotEqual(lines, STARTED)
        self.assertEqual(self.calls, [])

    def test_named_caller_and_tmux_server_cannot_select_the_nightly_account(self) -> None:
        """With no unit role, the guard and launched Claude both use the default account."""
        named = Path.home() / ".claude3"
        named.mkdir()
        launched, permission, spawn = self.launched_environment({
            "CLAUDE_CONFIG_DIR": str(named),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path.home() / ".claude"),
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-named-token",
        })
        guard_environment = cast(dict[str, str], permission.call_args.args[0])
        spawn_environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        for environment in (guard_environment, spawn_environment, launched):
            self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_REMOTE_MEMORY_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

    def test_nightly_picked_named_account_reaches_guard_and_launch_despite_caller(self) -> None:
        """The guard and launched Claude both use the unit role's account, whatever the caller holds."""
        unit = self.unit_account("claude 3")
        launched, permission, spawn = self.launched_environment({
            "CLAUDE_CONFIG_DIR": str(Path.home() / ".claude7"),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": "/caller/memory",
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-named-token",
        })
        guard_environment = cast(dict[str, str], permission.call_args.args[0])
        self.assertEqual(guard_environment["CLAUDE_CONFIG_DIR"], str(unit))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", guard_environment)
        spawn_environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        self.assertEqual([name for name in spawn_environment if name.startswith("CLAUDE_")], [])
        argv = cast(list[str], spawn.call_args.args[0])
        self.assertEqual(
            argv[-1],
            "unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; "
            + f"CLAUDE_CONFIG_DIR={shlex.quote(str(unit))} "
            + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path.home() / '.claude'))} "
            + f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh",
        )
        self.assertEqual(launched["CLAUDE_CONFIG_DIR"], str(unit))
        self.assertEqual(launched["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(Path.home() / ".claude"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", launched)

    def test_nightly_refuses_unfit_configured_account_without_using_default(self) -> None:
        """F004: an unfit configured directory is a refusal, not a selection hint."""
        _ = self.unit_role("claude 3")
        lines = self.launched([])
        self.assertNotEqual(lines, STARTED)
        self.assertEqual(self.calls, [])

    def test_picked_default_nightly_launch_drops_inherited_claude_variables(self) -> None:
        with (mock.patch.dict(os.environ, {"CLAUDE_TEST_MARKER": "fixture"}),
              mock.patch.object(nightly_review, "launch_permission", return_value=launch_permission.AllowedByShutdownState()) as permission,
              mock.patch("subprocess.run", return_value=done()) as spawn):
            self.assertEqual(nightly_review.launch(NIGHT), STARTED)
        guard = cast(dict[str, str], permission.call_args.args[0])
        self.assertFalse(any(key.startswith("CLAUDE_") for key in guard))
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["env"], guard)
        argv = cast(list[str], spawn.call_args.args[0])
        self.assertIn("unset CLAUDE_CONFIG_DIR", argv[-1])
        self.assertIn(f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh", argv[-1])

    def skipped_by_shutdown(self) -> list[str]:
        """Launch against the real shutdown check, with a start that must not run when it blocks."""
        with (
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "start", return_value=STARTED),
        ):
            return nightly_review.launch(NIGHT)

    def test_down_account_skips_without_starting_a_review(self) -> None:
        self.write_shutdown_record(self.down_record())
        self.assertEqual(
            self.skipped_by_shutdown(),
            [
                "skipped: claude 2 is held by a shutdown "
                + "(since 2026-10-09 14:49 PDT)"
            ],
        )

    def test_a_shutdown_of_the_unit_account_skips_and_one_of_the_default_does_not(self) -> None:
        _ = self.unit_account("claude 3")
        self.write_shutdown_record(self.down_record())
        self.assertEqual(self.skipped_by_shutdown(), STARTED)

        self.write_shutdown_record(self.down_record(UNIT_LOGIN, "claude 3"))
        self.assertEqual(
            self.skipped_by_shutdown(),
            [
                "skipped: claude 3 is held by a shutdown "
                + "(since 2026-10-09 14:49 PDT)"
            ],
        )

    def test_unreadable_shutdown_state_skips_with_one_prefix(self) -> None:
        account = self.state / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{", encoding="utf-8")
        lines = self.skipped_by_shutdown()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("skipped: shutdown state unreadable: "))
        self.assertEqual(lines[0].count("shutdown state unreadable: "), 1)

    def test_the_start_runs_while_the_launch_barrier_is_held(self) -> None:
        starts: list[str] = []

        def start(_selection: object, prefix: str, _environment: dict[str, str]) -> list[str]:
            lock_path = self.state / "launch.lock"
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
            starts.append(prefix)
            return STARTED

        with (
            mock.patch.object(
                nightly_review,
                "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ),
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "start", side_effect=start),
        ):
            lines = nightly_review.launch(NIGHT)

        self.assertEqual(starts, [""])
        self.assertEqual(lines, STARTED)

    def test_unknown_or_reset_usage_does_not_skip(self) -> None:
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        notes = [FakeNote("claude 1", {"state": "active", "weekly_remaining_usage": "null"}),
                 FakeNote("claude 2", {"state": "active", "weekly_remaining_usage": "2", "resets": past})]
        self.assertEqual(self.launched(notes), STARTED)

    def test_a_session_the_review_name_prefixes_is_never_taken_for_it(self) -> None:
        selection = accounts.claude_account_selection()
        targets = {f"={name}{suffix}" for name in (SESSION, *nightly_review.LEGACY_SESSIONS)
                   for suffix in ("", ":")}

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "tmux":
                self.assertIn(command[-1], targets)
                return self.tmux(*command[1:])
            self.calls.append(command)
            return done()

        def start() -> list[str]:
            with mock.patch.object(nightly_review, "tmux", REAL_TMUX), mock.patch("subprocess.run", run):
                return nightly_review.start(selection, "", nightly_review.launch_environment(selection))

        self.live = {f"{SESSION}-kept"}
        self.assertEqual(start(), STARTED)
        self.assertIn(f"{SESSION}-kept", self.live)
        self.live.add(SESSION)
        self.assertEqual(start(), STARTED)
        self.assertNotIn(SESSION, self.live)
        self.assertIn(f"{SESSION}-kept", self.live)
        self.assertEqual(len(self.calls), 2)

if __name__ == "__main__":
    _ = unittest.main()
