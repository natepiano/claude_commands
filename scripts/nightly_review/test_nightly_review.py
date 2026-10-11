"""Launch, quota gate and morning offer of the nightly review."""

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
MORNING = datetime(2026, 9, 29, 7, 30)


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
            return done("/dev/pts/3: nightly-config\n" if name in self.attached else "")
        if verb == "list-panes":
            mode = name.removeprefix("nightly-")
            return done("zsh -ic \"claude --resume x\"\n" if name in self.foreign else f"zsh -ic \"claude '/nightly_review {mode}'\"\n")
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

    def write_shutdown_record(self, value: object) -> None:
        account = self.state / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def down_record(self) -> dict[str, object]:
        return {
            "login": "owner@example.com",
            "label": "claude 2",
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

    def night(self, log: str) -> Path:
        directory = self.root / NIGHT.isoformat()
        directory.mkdir()
        _ = (directory / "launch.log").write_text(log)
        return directory

    def test_launch_starts_both_agents_with_the_prompt_last(self) -> None:
        self.assertEqual(self.launched([]), ["nightly-config: started", "nightly-rust: started"])
        command = self.calls[0]
        self.assertEqual(command[command.index("-s") + 1], "nightly-config")
        self.assertEqual(command[command.index("-c") + 1], "/etc/nixos")
        self.assertTrue(command[-1].endswith("'/nightly_review config'; exec zsh"))
        self.assertTrue((self.root / NIGHT.isoformat() / "launch.log").read_text().startswith("nightly-config: started"))

    def test_last_night_is_replaced_unless_the_user_is_attached(self) -> None:
        self.live = {"nightly-config", "nightly-rust"}
        self.attached = {"nightly-rust"}
        lines = self.launched([])
        self.assertEqual(lines[0], "nightly-config: started")
        self.assertIn("left running", lines[1])
        self.assertEqual(len(self.calls), 1)

    def test_a_session_no_review_started_is_left_running(self) -> None:
        self.live = {"nightly-config", "nightly-rust"}
        self.foreign = {"nightly-config"}
        lines = self.launched([])
        self.assertIn("another session holds this name", lines[0])
        self.assertIn("nightly-config", self.live)
        self.assertEqual(lines[1], "nightly-rust: started")
        self.assertEqual(len(self.calls), 1)

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

    def test_low_unit_account_does_not_block_default_account_nightly_work(self) -> None:
        (Path.home() / ".claude3").mkdir()
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        notes = [
            FakeNote("claude 3", {"login": "unit@example.com", "state": "active",
                                  "weekly_remaining_usage": "2", "resets": resets}),
            FakeNote("claude 2", {"login": "OWNER@example.com", "state": "active",
                                  "weekly_remaining_usage": "80", "resets": resets}),
        ]
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude3")}):
            self.assertEqual(self.launched(notes), ["nightly-config: started", "nightly-rust: started"])

    def test_low_default_account_blocks_even_when_the_unit_has_quota(self) -> None:
        (Path.home() / ".claude3").mkdir()
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        notes = [
            FakeNote("claude 3", {"login": "unit@example.com", "state": "active",
                                  "weekly_remaining_usage": "80", "resets": resets}),
            FakeNote("claude 2", {"login": "owner@example.com", "state": "active",
                                  "weekly_remaining_usage": "4", "resets": resets}),
        ]
        self.assertEqual(self.launched(notes), [
            "skipped: claude 2 has 4% of its weekly usage left, under the 10% floor",
        ])
        self.assertEqual(self.calls, [])

    def test_nightly_gate_uses_the_supplied_default_identity_for_the_whole_launch(self) -> None:
        """Nightly does not independently resolve the default login after the snapshot."""
        (Path.home() / ".claude3").mkdir()
        selection = accounts.claude_account_selection()
        _ = (Path.home() / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "unit@example.com"},
        }), encoding="utf-8")
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        notes = [
            FakeNote("claude 3", {"login": "unit@example.com", "state": "active",
                                  "weekly_remaining_usage": "2", "resets": resets}),
            FakeNote("claude 2", {"login": "OWNER@example.com", "state": "active",
                                  "weekly_remaining_usage": "80", "resets": resets}),
        ]
        with mock.patch.object(accounts, "claude_account_selection", return_value=selection) as resolve:
            self.assertEqual(self.launched(notes), ["nightly-config: started", "nightly-rust: started"])
        resolve.assert_called_once_with()

    def test_named_caller_and_tmux_server_cannot_select_the_nightly_account(self) -> None:
        """The guard and launched Claude both use the default account."""
        named = Path.home() / ".claude3"
        named.mkdir()
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
            "CLAUDE_CONFIG_DIR": str(named),
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-named-token",
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
            self.assertEqual(nightly_review.launch(NIGHT),
                             ["nightly-config: started", "nightly-rust: started"])

        guard_environment = cast(dict[str, str], permission.call_args.args[0])
        self.assertNotIn("CLAUDE_CONFIG_DIR", guard_environment)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", guard_environment)
        for call in spawn.call_args_list:
            environment = cast(dict[str, str], call.kwargs["env"])
            self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        argv = cast(list[str], spawn.call_args_list[0].args[0])
        # An existing tmux server supplies its own environment, independently
        # of the sanitized environment used to invoke the tmux client.
        result = subprocess.run(
            [real_shell, "-ic", argv[-1]], env=caller_environment,
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        launched_environment = cast(dict[str, str], json.loads(log.read_text(encoding="utf-8")))
        self.assertNotIn("CLAUDE_CONFIG_DIR", launched_environment)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", launched_environment)

    def test_unconfigured_nightly_launch_keeps_its_command_and_environment(self) -> None:
        """Legacy launches retain the previous argv and inherited environment."""
        inherited = {**os.environ, "CLAUDE_TEST_MARKER": "fixture"}
        with (
            mock.patch.dict(os.environ, inherited),
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "launch_permission",
                              return_value=launch_permission.AllowedByShutdownState()) as permission,
            mock.patch("subprocess.run", return_value=done()) as spawn,
        ):
            self.assertEqual(nightly_review.launch(NIGHT),
                             ["nightly-config: started", "nightly-rust: started"])
            self.assertEqual(permission.call_args.args[0], inherited)
        for mode, call in zip(nightly_review.MODES, spawn.call_args_list):
            self.assertNotIn("env", call.kwargs)
            name = f"nightly-{mode}"
            claude = shlex.join([
                "claude", "--remote-control", name, "-n", name, "--add-dir",
                str(Path.home() / ".claude"), "--settings", str(nightly_review.SETTINGS),
                f"/nightly_review {mode}",
            ])
            argv = cast(list[str], call.args[0])
            self.assertEqual(argv[-1], f"ENABLE_TOOL_SEARCH=true command {claude}; exec zsh")

    def test_down_account_skips_without_starting_a_review(self) -> None:
        self.write_shutdown_record(self.down_record())
        starts: list[str] = []

        def start(mode: str, _selection: object) -> str:
            starts.append(mode)
            return f"nightly-{mode}: started"

        with (
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "start_for_selection", side_effect=start),
        ):
            lines = nightly_review.launch(NIGHT)

        self.assertEqual(
            lines,
            [
                "skipped: claude 2 is held by a shutdown "
                + "(down since 2026-10-09 14:49 PDT)"
            ],
        )
        self.assertEqual(starts, [])

    def test_unreadable_shutdown_state_skips_with_one_prefix(self) -> None:
        account = self.state / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{", encoding="utf-8")
        starts: list[str] = []

        def start(mode: str, _selection: object) -> str:
            starts.append(mode)
            return f"nightly-{mode}: started"

        with (
            mock.patch("note_file.read_notes", return_value=[]),
            mock.patch.object(nightly_review, "start_for_selection", side_effect=start),
        ):
            lines = nightly_review.launch(NIGHT)

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("skipped: shutdown state unreadable: "))
        self.assertEqual(lines[0].count("shutdown state unreadable: "), 1)
        self.assertEqual(starts, [])

    def test_both_starts_run_while_launch_barrier_is_held(self) -> None:
        starts: list[str] = []

        def start(mode: str, _selection: object) -> str:
            lock_path = self.state / "launch.lock"
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
            starts.append(mode)
            return f"nightly-{mode}: started"

        with (
            mock.patch.object(
                nightly_review,
                "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ),
            mock.patch.object(
                nightly_review,
                "quota_permission",
                return_value=nightly_review.QuotaClear(),
            ),
            mock.patch.object(nightly_review, "start_for_selection", side_effect=start),
        ):
            lines = nightly_review.launch(NIGHT)

        self.assertEqual(starts, ["config", "rust"])
        self.assertEqual(lines, ["nightly-config: started", "nightly-rust: started"])

    def test_unknown_or_reset_usage_does_not_skip(self) -> None:
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        notes = [FakeNote("claude 1", {"state": "active", "weekly_remaining_usage": "null"}),
                 FakeNote("claude 2", {"state": "active", "weekly_remaining_usage": "2", "resets": past})]
        self.assertEqual(len(self.launched(notes)), 2)

    def test_offer_waits_for_five_and_fires_once(self) -> None:
        directory = self.night("nightly-config: started\nnightly-rust: started\n")
        _ = (directory / "config.md").write_text("# proposal\n")
        self.live = {"nightly-rust"}
        self.assertIsNone(nightly_review.offer(MORNING.replace(hour=4, minute=59), lambda: "natedev"))
        context = nightly_review.offer(MORNING, lambda: "natedev")
        self.assertIsNotNone(context)
        self.assertIn("config.md is ready", context or "")
        self.assertIn("rust is still running (tmux session nightly-rust)", context or "")
        self.assertIn("digest.md is not written yet", context or "")
        self.assertIsNone(nightly_review.offer(MORNING, lambda: "natedev"))

    def test_offer_is_for_natedev_only(self) -> None:
        directory = self.night("nightly-config: started\n")
        self.assertIsNone(nightly_review.offer(MORNING, lambda: "boss of bosses"))
        self.assertFalse((directory / "offered").exists())

    def test_offer_asks_and_carries_deferred_proposals(self) -> None:
        _ = self.night("nightly-config: started\n")
        _ = (self.root / "ledger.md").write_text(
            "- 2026-09-27 config: Old one — accepted, applied\n- 2026-09-28 rust/hana: Warm runners — deferred\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("Deferred earlier: 2026-09-28 rust/hana: Warm runners.", context)
        self.assertNotIn("Old one", context)
        self.assertIn("see it and the deferred proposals now or defer it, and on yes present it with /nightly_next", context)
        self.assertIn("change its ledger line's `— proposed` to `— deferred`", context)

    def test_offer_asks_about_deferred_on_a_skipped_night(self) -> None:
        _ = self.night("skipped: claude 2 has 4% of its weekly usage left, under the 10% floor\n")
        _ = (self.root / "ledger.md").write_text("- 2026-09-28 config: Kwin keys — deferred\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("Deferred earlier: 2026-09-28 config: Kwin keys.", context)
        self.assertIn("ask the user once whether they want to see the deferred proposals now or defer", context)

    def test_offer_reports_a_skipped_night(self) -> None:
        _ = self.night("skipped: claude 2 has 4% of its weekly usage left, under the 10% floor\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("skipped: claude 2 has 4%", context)
        self.assertNotIn("ask the user", context)


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

        def review(name: str) -> None:
            _ = real_run([binary, "-f", "/dev/null", "new-session", "-d", "-s", name,
                          ": /nightly_review config; exec sleep 60"], check=True)

        def live(name: str) -> bool:
            return real_run([binary, "has-session", "-t", f"={name}"], capture_output=True,
                            check=False).returncode == 0

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "tmux":
                return real_run(command, capture_output=True, text=True, check=False)
            self.calls.append(command)
            return done()

        def start() -> str:
            with mock.patch.object(nightly_review, "tmux", REAL_TMUX), mock.patch("subprocess.run", run):
                return nightly_review.start("config")

        review("nightly-config-kept")
        self.assertEqual(start(), "nightly-config: started")
        self.assertTrue(live("nightly-config-kept"))
        review("nightly-config")
        self.assertEqual(start(), "nightly-config: started")
        self.assertFalse(live("nightly-config"))
        self.assertTrue(live("nightly-config-kept"))
        self.assertEqual(len(self.calls), 2)

if __name__ == "__main__":
    _ = unittest.main()
