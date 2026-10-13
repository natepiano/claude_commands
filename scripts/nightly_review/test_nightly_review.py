"""Launch and quota gate of the nightly review."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

import nightly_review
import launch_permission
import agent_notes
import note_file
from scripts.accounts import accounts

REAL_TMUX = nightly_review.tmux
NIGHT = date(2026, 9, 29)
SESSION = "nightly-review"
STARTED = [f"{SESSION}: started"]
UNIT_LOGIN = "unit@example.com"


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


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
            json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"}}),
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
        for name, value in (("ROOT", self.root), ("tmux", self.tmux)):
            patcher = mock.patch.object(nightly_review, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)

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
            json.dumps({"oauthAccount": {"emailAddress": UNIT_LOGIN}}), encoding="utf-8",
        )
        _ = (self.notes / f"{label}.md").write_text(
            f"---\nlogin: {UNIT_LOGIN}\nstate: active\n---\n", encoding="utf-8",
        )
        return directory

    def unit_role(self, label: str) -> Path:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(
            json.dumps(accounts.AccountsConfiguration(unit=label)), encoding="utf-8",
        )
        return accounts.account_dir(label)

    def write_shutdown_record(self, value: dict[str, object]) -> None:
        account = self.state / cast(str, value["login"])
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

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
            "--settings", str(nightly_review.SETTINGS), "/nightly_review config rust",
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
        self.assertEqual(command[-1], f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh")
        self.assertTrue(command[-1].endswith("'/nightly_review config rust'; exec zsh"))
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

    def test_low_quota_skips_the_night(self) -> None:
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        low = FakeNote("claude 2", {"login": "owner@example.com", "state": "active", "weekly_remaining_usage": "4", "resets": resets})
        lines = self.launched([low])
        self.assertEqual(lines, ["skipped: claude 2 has 4% of its weekly usage left, under the 10% floor"])
        self.assertEqual(self.calls, [])

    def test_quota_permission_names_clear_and_below_floor_states(self) -> None:
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        low = FakeNote(
            "claude 2",
            {
                "login": "owner@example.com",
                "state": "active",
                "weekly_remaining_usage": "4",
                "resets": resets,
            },
        )
        with mock.patch("note_file.read_notes", return_value=[low]):
            below = nightly_review.quota_permission()
        self.assertEqual(
            below,
            nightly_review.QuotaBelowFloor(
                "claude 2 has 4% of its weekly usage left, under the 10% floor"
            ),
        )

        with mock.patch("note_file.read_notes", return_value=[]):
            clear = nightly_review.quota_permission()
        self.assertIsInstance(clear, nightly_review.QuotaClear)

    def account_notes(self, default_left: str, unit_left: str) -> list[FakeNote]:
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        return [
            FakeNote("claude 3", {"login": UNIT_LOGIN, "state": "active",
                                  "weekly_remaining_usage": unit_left, "resets": resets}),
            FakeNote("claude 2", {"login": "OWNER@example.com", "state": "active",
                                  "weekly_remaining_usage": default_left, "resets": resets}),
        ]

    def test_quota_floor_reads_the_unit_account_the_review_launches_on(self) -> None:
        _ = self.unit_account("claude 3")
        self.assertEqual(self.launched(self.account_notes(default_left="80", unit_left="2")), [
            "skipped: claude 3 has 2% of its weekly usage left, under the 10% floor",
        ])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.launched(self.account_notes(default_left="4", unit_left="80")), STARTED)

    def test_without_a_unit_role_the_quota_floor_reads_the_default_login(self) -> None:
        (Path.home() / ".claude3").mkdir()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude3")}):
            self.assertEqual(self.launched(self.account_notes(default_left="80", unit_left="2")), STARTED)
        self.calls.clear()
        self.assertEqual(self.launched(self.account_notes(default_left="4", unit_left="80")), [
            "skipped: claude 2 has 4% of its weekly usage left, under the 10% floor",
        ])
        self.assertEqual(self.calls, [])

    def test_nightly_gate_uses_the_supplied_identities_for_the_whole_launch(self) -> None:
        """Nightly does not independently resolve a login after the snapshot."""
        (Path.home() / ".claude3").mkdir()
        selection = accounts.claude_account_selection()
        _ = (Path.home() / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": UNIT_LOGIN},
        }), encoding="utf-8")
        notes = self.account_notes(default_left="80", unit_left="2")
        with mock.patch.object(accounts, "claude_account_selection", return_value=selection) as resolve:
            self.assertEqual(self.launched(notes), STARTED)
        resolve.assert_called_once_with()

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

    def test_unit_role_launches_the_review_on_that_account(self) -> None:
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

    def test_a_unit_account_not_fit_to_launch_skips_the_night(self) -> None:
        missing = self.unit_role("claude 3")
        self.assertEqual(self.launched([]), [
            f"skipped: unit account claude 3: accounts: {missing} is missing",
        ])
        self.assertEqual(self.calls, [])

    def test_unconfigured_nightly_launch_keeps_its_command_and_environment(self) -> None:
        """Legacy launches retain the plain command and inherited environment."""
        inherited = {**os.environ, "CLAUDE_TEST_MARKER": "fixture"}
        with (
            mock.patch.dict(os.environ, inherited),
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "launch_permission",
                              return_value=launch_permission.AllowedByShutdownState()) as permission,
            mock.patch("subprocess.run", return_value=done()) as spawn,
        ):
            self.assertEqual(nightly_review.launch(NIGHT), STARTED)
            self.assertEqual(permission.call_args.args[0], inherited)
        spawn.assert_called_once()
        self.assertNotIn("env", spawn.call_args.kwargs)
        argv = cast(list[str], spawn.call_args.args[0])
        self.assertEqual(argv[-1], f"ENABLE_TOOL_SEARCH=true command {self.review_command()}; exec zsh")

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

        def start(_selection: object, prefix: str) -> list[str]:
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
        binary = shutil.which("tmux")
        if binary is None:
            self.skipTest("tmux is required to check its session matching")
        server_dir = self.root / "tmux"
        server_dir.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(server_dir)}))
        _ = os.environ.pop("TMUX", None)
        self.addCleanup(subprocess.run, [binary, "kill-server"], capture_output=True, check=False)
        real_run = subprocess.run
        selection = accounts.claude_account_selection()

        def review(name: str) -> None:
            _ = real_run([binary, "-f", "/dev/null", "new-session", "-d", "-s", name,
                          ": /nightly_review config rust; exec sleep 60"], check=True)

        def live(name: str) -> bool:
            return real_run([binary, "has-session", "-t", f"={name}"], capture_output=True,
                            check=False).returncode == 0

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "tmux":
                return real_run(command, capture_output=True, text=True, check=False)
            self.calls.append(command)
            return done()

        def start() -> list[str]:
            with mock.patch.object(nightly_review, "tmux", REAL_TMUX), mock.patch("subprocess.run", run):
                return nightly_review.start(selection, "")

        review(f"{SESSION}-kept")
        self.assertEqual(start(), STARTED)
        self.assertTrue(live(f"{SESSION}-kept"))
        review(SESSION)
        self.assertEqual(start(), STARTED)
        self.assertFalse(live(SESSION))
        self.assertTrue(live(f"{SESSION}-kept"))
        self.assertEqual(len(self.calls), 2)

if __name__ == "__main__":
    _ = unittest.main()
