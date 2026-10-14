"""Behavioral contract for isolated Claude account directories."""

from __future__ import annotations

from scripts.whoami.note_file import note_name

import io
from importlib import import_module
import base64
import json
import os
import selectors
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from collections.abc import Callable
from typing import Self, TextIO, cast, final, override
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

from scripts.accounts import accounts, refusals
from scripts.production import showrunners
from scripts.agents import codex_mesh
from scripts.accounts import moves
import account
import agent_accounts
import usage_store


def named_environment(config_dir: Path) -> dict[str, str]:
    """What a session on a named account's directory starts with."""
    return {
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path.home() / ".claude"),
    }


def setUpModule() -> None:
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))
    _ = unittest.enterModuleContext(patch.object(accounts, "SEATS_RUN_ROOT", root / "seat-runs"))


def named_prefix(config_dir: Path) -> str:
    return "".join(
        f"{name}={shlex.quote(value)} "
        for name, value in named_environment(config_dir).items()
    )


EXPECTED_SHARED = (
    "settings.json",
    "CLAUDE.md",
    "keybindings.json",
    "history.jsonl",
    "commands",
    "skills",
    "agents",
    "output-styles",
    "plugins",
    "projects",
    "sessions",
    "plans",
    "todos",
    "tasks",
    "teams",
    "file-history",
    "shell-snapshots",
    "session-env",
    "paste-cache",
    "uploads",
)


EXPECTED_CODEX_SHARED = (
    "config.toml",
    "hooks.json",
    "AGENTS.md",
    "skills",
    "rules",
    "plugins",
    "sessions",
    "archived_sessions",
    "session_index.jsonl",
    "history.jsonl",
    "models_cache.json",
)


@final
class RoleWriteTime(datetime):
    """The fixed assignment time before this fixture's quota observations."""

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 8, tzinfo=timezone.utc).astimezone(tz)


# The Codex home's row when nothing is signed in to it.
CODEX_HOME_ROW = ("~/.codex", "~/.codex", "not logged in", "seats", *("—",) * 7)


@final
class AccountsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.main_dir = Path()
        # The live reading the status table gets for a folder; a folder without one cannot be read.
        self.live: dict[Path, agent_accounts.Report] = {}
        self.live_reads = MagicMock()
        self.codex_reads = AsyncMock()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home with spaces"
        _ = self.home.mkdir()
        self.main_dir = self.home / ".claude"
        _ = self.main_dir.mkdir(mode=0o700)
        notes = self.root / "notes"
        _ = notes.mkdir()
        environment = {
            "AGENT_NOTES_DIR": str(notes),
            "HOME": str(self.home),
            "PATH": os.defpath,
            # The wall times these tests pass are Pacific; the user's zone setting must not change them.
            "USER_ZONE": "America/Los_Angeles",
        }
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del environment_context
        self.live = {}
        self.live_reads = self.enterContext(patch.object(agent_accounts, "claude_live", side_effect=self.live_report))
        self.codex_reads = cast(AsyncMock, self.enterContext(patch.object(
            agent_accounts, "codex_live", return_value=agent_accounts.Report("Codex", problem="Not logged in"),
        )))
        _ = self.enterContext(patch.object(accounts, "SEATS_RUN_ROOT", self.root / "seat-runs"))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home",
            return_value=agent_accounts.CodexHomePrepared()))
        _ = self.enterContext(patch.object(codex_mesh, "retire_seats_servers", return_value=[]))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=[
            showrunners.Showrunner(session="fixture", socket="", slug="fixture", zone="America/Los_Angeles", doc=""),
        ]))

    def live_report(self, config_dir: Path) -> agent_accounts.Report:
        return self.live.get(config_dir, agent_accounts.Report(
            "Claude", problem="Account unavailable (claude auth status failed)",
        ))

    def test_status_names_live_account_and_codex_refusal_destination_separately(self) -> None:
        from scripts.message import sessions
        self.login_directory("claude 2", "main@example.invalid", default=True)
        owner = sessions.SessionRecord(pid=12345, sessionId="live-identity", name="Live director",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
        now = datetime.now(timezone.utc)
        def chosen(tool: str, _now: datetime, **_options: object) -> accounts.PickedAccount:
            return accounts.PickedAccount("default" if tool == "claude" else "codex 3",
                self.main_dir if tool == "claude" else self.home / ".codex3", 1,
                accounts.KnownReset(now + timedelta(days=1)))
        with (patch.object(sessions, "live_sessions", return_value=[owner]),
              patch.object(import_module("sessions"), "live_sessions", return_value=[owner]),
              patch.object(account, "process_config_dir", return_value=self.main_dir),
              patch.object(accounts, "pick", side_effect=chosen),
              patch.object(accounts, "role_account", return_value=accounts.CodexSeatsAccount("codex 1", self.home / ".codex1"))):
            code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("live-identity", output)
        self.assertIn("Live director", output)
        self.assertIn("claude 2", output)
        self.assertIn("Codex seats: codex 1", output)
        self.assertIn("on refusal: codex 3", output)
        self.assertIn("units:", output.splitlines()[0])

    def test_status_shows_each_owed_continuation_and_its_hold_reason(self) -> None:
        run = self.root / "seat-runs/run-one"
        run.mkdir(parents=True)
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}))
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            "blocked": {"status": "starting", "continuation_owed": True,
                "continuation_reason": "destination account refused again"},
            "capacity": {"status": "waiting_capacity", "continuation_owed": True},
            "done": {"status": "idle", "continuation_owed": False},
        }))
        _, output, _ = self.run_cli("status")
        self.assertIn("run-one/blocked: continuation owed", output)
        self.assertIn("destination account refused again", output)
        self.assertIn("run-one/capacity: continuation owed", output)
        self.assertIn("waiting for capacity", output)
        self.assertNotIn("run-one/done", output)

    def test_status_omits_owed_continuations_from_runs_without_a_live_mesh_server(self) -> None:
        for name, server in (("live", {"pid": 901}), ("dead", {"pid": 902}),
                             ("missing", None), ("malformed", {"pid": "901"}),
                             ("invalid-pid", {"pid": True})):
            run = self.root / "seat-runs" / name
            run.mkdir(parents=True)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "worker": {"continuation_owed": True, "status": "waiting_capacity"}}))
            if server is not None:
                _ = (run / "mesh_server.json").write_text(json.dumps(server))
        def check(pid: int, signal: int) -> None:
            self.assertEqual(signal, 0)
            if pid != 901:
                raise ProcessLookupError("mesh server gone")
        with patch.object(os, "kill", side_effect=check):
            _, output, _ = self.run_cli("status")
        self.assertIn("live/worker: continuation owed", output)
        for name in ("dead", "missing", "malformed", "invalid-pid"):
            self.assertNotIn(name + "/worker", output)

    def test_status_renders_requested_pending_confirmed_per_seat_and_failed_reason(self) -> None:
        common = moves.MoveCommon(at="2026-10-14T12:00:00+00:00", tool="codex", subject="seats", move_id="move-one")
        request: moves.RequestedMove = {**common, "kind": "requested", "from": "codex 1", "to": "codex 3",
            "why": "weekly refusal", "window": "weekly", "reset": "2026-10-16T12:00:00+00:00"}
        moves.append_move(request)
        moves.append_move(moves.ConfirmedMove(**common, kind="confirmed", home=str(self.home / ".codex3"), seat="worker-a", run="run-one"))
        moves.append_move(moves.ConfirmedMove(**common, kind="confirmed", home=str(self.home / ".codex3"), seat="worker-b", run="run-one"))
        failed: moves.RequestedMove = {**request, "move_id": "failed-move", "subject": "claude-session: Director", "tool": "claude"}
        moves.append_move(failed)
        moves.append_move(moves.FailedMove(at=common["at"], move_id="failed-move", subject="claude-session: Director", tool="claude",
            kind="failed", reason="still on claude 1"))
        moves.append_move({**request, "move_id": "pending-move", "subject": "waiting-session: Waiting"})
        _, output, _ = self.run_cli("status")
        self.assertIn("confirmed", output)
        self.assertIn("worker-a", output)
        self.assertIn("worker-b", output)
        self.assertIn("still on claude 1", output)
        self.assertIn("pending", output)
        self.assertIn("2026-10-14 05:00", output)

    def test_status_limits_move_readout_to_last_ten_entries(self) -> None:
        for index in range(12):
            moves.append_move(moves.FailedMove(at="2026-10-14T12:00:00+00:00", tool="claude",
                subject=f"session-{index}", move_id=f"move-{index}", kind="failed", reason=f"failure-{index}"))
        _, output, _ = self.run_cli("status")
        self.assertNotIn("session-0:", output)
        self.assertNotIn("session-1:", output)
        for index in range(2, 12):
            self.assertIn(f"session-{index}", output)

    def test_status_renders_wait_end_no_room_and_room_again_in_user_zone(self) -> None:
        common = moves.MoveCommon(at="2026-10-14T12:00:00+00:00", tool="claude", subject="director: Director", move_id="wait-one")
        moves.append_move(moves.WaitedMove(**common, kind="waited", window="five_hour", reset="2026-10-14T12:19:00+00:00"))
        moves.append_move(moves.WaitEndedMove(**common, kind="wait ended"))
        room: moves.MoveCommon = {**common, "move_id": "room-one"}
        moves.append_move(moves.NoRoomMove(**room, kind="no room", earliest_reset="2026-10-15T12:00:00+00:00"))
        moves.append_move(moves.RoomAgainMove(**room, kind="room again", label="claude 3"))
        _, output, _ = self.run_cli("status")
        self.assertIn("waited until 2026-10-14T05:19", output)
        self.assertIn("wait ended", output)
        self.assertIn("no room until 2026-10-15T05:00", output)
        self.assertIn("room again: claude 3", output)

    def status_rows(self) -> list[tuple[str, ...]]:
        """The status table's rows after its heading, each a tuple of its cells, then nothing else."""
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        table = [line for line in stdout.split("\n\n")[0].splitlines() if line.startswith("|")]
        self.assertEqual(
            [cell.strip() for cell in table[0].strip("|").split("|")], list(accounts.TABLE_HEADINGS),
        )
        return [tuple(cell.strip() for cell in line.strip("|").split("|")) for line in table[2:]]

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = accounts.main(list(arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def write_main_config(self) -> None:
        source = {
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": "2.1.296",
            "installMethod": "native",
            "autoUpdates": False,
            "shiftEnterKeyBindingInstalled": True,
            "mcpServers": {"brp": {"command": "brp"}},
            "remoteDialogSeen": True,
            "hasUsedRemoteControl": True,
            "hasSeenAutoModeEntryWarning": True,
            "hasSeenAutoModeOutsideReadPrompt": False,
            "hasResetAutoModeOptInForDefaultOffer": True,
            "oauthAccount": {"emailAddress": "must-not-copy@example.invalid"},
            "cachedGrowthBookFeatures": {"secret-cache": True},
            "projects": {
                "/trusted": {
                    "hasTrustDialogAccepted": True,
                    "allowedTools": ["Bash(git status)", "Read"],
                    "enabledMcpjsonServers": ["one"],
                    "disabledMcpjsonServers": ["two"],
                    "mcpServers": {"project": {"command": "project"}},
                    "hasClaudeMdExternalIncludesApproved": True,
                    "hasClaudeMdExternalIncludesWarningShown": False,
                    "lastCost": 19.5,
                    "lastSessionId": "must-not-copy",
                },
                "/not-a-project-object": "discard this entry",
            },
        }
        _ = (self.home / ".claude.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    def correct_links(self, account_dir: Path) -> None:
        _ = account_dir.mkdir(mode=0o700)
        for entry in EXPECTED_SHARED:
            source = self.main_dir / entry
            _ = source.mkdir(exist_ok=True)
            _ = (account_dir / entry).symlink_to(source)

    def write_note(self, label: str, login: str) -> None:
        path = Path(os.environ["AGENT_NOTES_DIR"]) / note_name(label)
        _ = path.write_text(f"---\nlogin: {login}\n---\n", encoding="utf-8")

    def test_group_note_labels_map_each_configured_group_and_omit_unconfigured_groups(self) -> None:
        self.write_note("claude 1", "unit@example.invalid")
        self.write_note("claude 3", "runner@example.invalid")
        accounts.set_group_account("units", "claude 1", why="fixture", now=datetime.now(timezone.utc))
        self.assertEqual(accounts.group_note_labels(), {"units": "claude 1"})
        accounts.set_group_account("showrunners", "claude 3", why="fixture", now=datetime.now(timezone.utc))
        self.assertEqual(accounts.group_note_labels(), {"units": "claude 1", "showrunners": "claude 3"})

    def test_group_note_labels_resolve_a_stored_default_from_its_actual_login(self) -> None:
        self.write_note("claude 2", "home@example.invalid")
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "home@example.invalid"}}))
        accounts.set_group_account("units", "default", why="fixture", now=datetime.now(timezone.utc))
        self.assertEqual(accounts.group_note_labels(), {"units": "claude 2"})
        _ = (self.home / ".claude.json").write_text("not json")
        self.assertEqual(accounts.group_note_labels(), {})

    def codex_login(self, login: str, home: Path | None = None) -> None:
        """Sign the Codex home in the way Codex stores it: the login is a claim of its id token."""
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        codex_home = self.home / ".codex" if home is None else home
        codex_home.mkdir(exist_ok=True)
        _ = (codex_home / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}), encoding="utf-8",
        )

    def write_identity(self, label: str, login: str) -> None:
        _ = (accounts.account_dir(label) / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8",
        )

    def logged_in(self) -> account.Account:
        return account.Account("claude", "owner@example.invalid", "claude 2")

    def login_directory(self, label: str, login: str, *, default: bool = False, note_label: str = "") -> None:
        config = self.home / ".claude.json" if default else accounts.account_dir(label) / ".claude.json"
        if not default:
            for name in ("projects", "sessions"):
                (self.main_dir / name).mkdir(exist_ok=True)
            self.assertEqual(accounts.setup(label), [])
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
        self.write_note(label, login)
        if note_label:
            self.write_note(note_label, login)

    def seed_room(self, login: str, days: float = 2) -> None:
        now = datetime.now(timezone.utc)
        file = usage_store.store_file()
        saved = usage_store.load()
        saved[usage_store.key("Claude", login)] = usage_store.StoredUsage(weekly=usage_store.StoredWindow(
            left=80, at=now.isoformat(), resets=(now + timedelta(days=days)).isoformat()))
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

    def write_readings(self, readings: list[accounts.QuotaReading]) -> None:
        log = accounts.readings_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ = log.write_text("".join(json.dumps(reading) + "\n" for reading in readings), encoding="utf-8")

    def write_role_history(self, changes: list[accounts.HistoricalRoleChange]) -> None:
        log = accounts.roles_history_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ = log.write_text("".join(json.dumps(change) + "\n" for change in changes), encoding="utf-8")

    def select_named_unit(self, label: str) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps(accounts.AccountsConfiguration(unit=label)), encoding="utf-8")
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": label}))

    def test_exec_default_login_without_named_dirs_preserves_arguments_and_removes_config_override(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        before = set(self.home.iterdir())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "inherited")}), patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 2", "--version", "--", "quoted argument")
        self.assertEqual(result, (0, "", ""))
        environment = dict(os.environ)
        launch.assert_called_once_with("claude", ["claude", "--version", "--", "quoted argument"], environment)
        self.assertEqual(set(self.home.iterdir()), before)
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_exec_default_removes_inherited_config_directory_and_oauth_token(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "inherited", "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token"}),
            patch.object(os, "execvpe") as launch,
        ):
            expected = dict(os.environ)
            del expected["CLAUDE_CONFIG_DIR"]
            del expected["CLAUDE_CODE_OAUTH_TOKEN"]
            self.assertEqual(self.run_cli("exec", "default", "--resume", "same-id"), (0, "", ""))
        launch.assert_called_once_with("claude", ["claude", "--resume", "same-id"], expected)

    def test_exec_missing_directory_prints_setup_refusal_without_launching(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 3", "--version")
        self.assertEqual(result, (2, "", 'accounts: ~/.claude3 is not set up; /showrunner:accounts setup "claude 3" first\n'))
        launch.assert_not_called()
        self.assertFalse(accounts.account_dir("claude 3").exists())

    def test_prepared_account_named_ready_returns_only_launch_changes_without_exec(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        with (patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": "inherited", "CLAUDE_CODE_REMOTE_MEMORY_DIR": "inherited",
                "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token", "UNCHANGED": "caller-owned",
              }), patch.object(os, "execvpe") as launch):
            before = dict(os.environ)
            result = accounts.prepared_account("claude 3", resume=True)
            self.assertEqual(dict(os.environ), before)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude 3")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        self.assertNotIn("UNCHANGED", result.variables)
        launch.assert_not_called()
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "variables", {})

    def test_prepared_account_default_login_alias_removes_named_variables_without_setup(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("claude 3", "MAIN@example.invalid")
        result = accounts.prepared_account("claude 3", resume=True)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, {})
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN", *account.NAMED_ACCOUNT_VARIABLES}))
        self.assertFalse(accounts.account_dir("claude 3").exists())

    def test_prepared_account_missing_named_directory_refuses_with_setup_instruction(self) -> None:
        self.write_note("claude 3", "unit@example.invalid")
        result = accounts.prepared_account("claude 3", resume=False)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason,
            'accounts: ~/.claude3 is not set up; /showrunner:accounts setup "claude 3" first')
        self.assertFalse(accounts.account_dir("claude 3").exists())
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "reason", "changed")

    def test_prepared_account_logged_out_named_resume_refuses(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        result = accounts.prepared_account("claude 3", resume=True)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason, f"accounts: {accounts.account_dir('claude 3')} is not logged in")

    def test_prepared_account_logged_out_named_login_is_allowed(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        result = accounts.prepared_account("claude 3", resume=False)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude 3")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_prepared_account_wrong_login_refuses_with_existing_diagnostic(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "expected@example.invalid")
        self.write_identity("claude 3", "other@example.invalid")
        result = accounts.prepared_account("claude 3", resume=False)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason,
            "accounts: ~/.claude3 is logged in as other@example.invalid, not claude 3; see Log out and in in ~/.claude/docs/accounts.md")

    def test_group_launch_prepares_configured_account_for_new_work_and_resumes(self) -> None:
        """F006: every launch checks readiness without selecting another account."""
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "expected@example.invalid")
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude 3", "showrunners": "claude 3"}))
        before = path.read_bytes()
        for role in ("unit", "showrunner"):
            for resume in (False, True):
                with self.subTest(role=role, resume=resume), patch.object(accounts, "prepared_account",
                        return_value=accounts.AccountRefused("configured account is not ready")) as prepare:
                    with self.assertRaises(accounts.AccountsError) as refused:
                        _ = accounts.account_for_group_launch(role, resume=resume)
                    self.assertEqual(str(refused.exception), "configured account is not ready")
                    prepare.assert_called_once_with("claude 3", resume=resume)
        self.assertEqual(path.read_bytes(), before)

    def test_prepared_account_broken_links_refuse_even_when_logged_out(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        (self.main_dir / "projects").rmdir()
        result = accounts.prepared_account("claude 3", resume=False)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason, f"accounts: {accounts.account_dir('claude 3') / 'projects'} is broken")

    def test_prepared_account_default_resume_refuses_first_check_problem(self) -> None:
        problems = ["accounts: default identity unavailable", "accounts: another problem"]
        with patch.object(accounts, "check", return_value=problems) as checked:
            result = accounts.prepared_account("default", resume=True)
        checked.assert_called_once_with("default")
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason, problems[0])

    def test_prepared_account_default_without_resume_removes_named_variables_without_check(self) -> None:
        with patch.object(accounts, "check") as checked:
            result = accounts.prepared_account("default", resume=False)
        checked.assert_not_called()
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, {})
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN", *account.NAMED_ACCOUNT_VARIABLES}))

    def test_prepared_account_default_resume_with_no_problems_is_allowed(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        result = accounts.prepared_account("default", resume=True)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, {})
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN", *account.NAMED_ACCOUNT_VARIABLES}))

    def test_prepared_account_unit_uses_resolved_launch_account(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        for selected in (accounts.DefaultAccount(), accounts.NamedAccount("claude 3", accounts.account_dir("claude 3"))):
            with self.subTest(selected=selected), patch.object(accounts, "role_account", return_value=selected) as resolve:
                result = accounts.prepared_account("unit", resume=True)
                resolve.assert_called_once_with("unit")
                self.assertIsInstance(result, accounts.PreparedAccount)
                assert isinstance(result, accounts.PreparedAccount)
                if isinstance(selected, accounts.NamedAccount):
                    self.assertEqual(result.variables, named_environment(selected.dir))
                    self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
                else:
                    self.assertEqual(result.variables, {})
                    self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN", *account.NAMED_ACCOUNT_VARIABLES}))

    def test_exec_named_account_sets_its_config_directory_and_preserves_claude_arguments(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "inherited", "UNCHANGED": "value"}), patch.object(os, "execvpe") as launch:
            environment = {**os.environ, **named_environment(accounts.account_dir("claude 3"))}
            result = self.run_cli("exec", "claude 3", "--resume", "conversation id", "-p", "a prompt")
        self.assertEqual(result, (0, "", ""))
        launch.assert_called_once_with("claude", ["claude", "--resume", "conversation id", "-p", "a prompt"], environment)
        self.assertIsInstance(accounts.account_directory_readiness("claude 3"), accounts.AccountDirectoryReady)

    def test_exec_uses_one_expected_login_through_resolution_and_readiness(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md"
        reads: list[str] = []
        original_read = Path.read_text

        def change_after_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            text = original_read(path, encoding=encoding, errors=errors)
            if path == note:
                reads.append(text)
                _ = note.write_text("---\nlogin: next@example.invalid\n---\n")
            return text

        with patch.object(Path, "read_text", change_after_read), patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 3")
        self.assertEqual(result, (0, "", ""))
        self.assertEqual(len(reads), 1)
        launch.assert_called_once()


    def test_exec_named_alias_of_default_login_uses_plain_claude(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("claude 3", "MAIN@example.invalid")
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude 3"), (0, "", ""))
        launch.assert_called_once_with("claude", ["claude"], dict(os.environ))
        self.assertFalse(accounts.account_dir("claude 3").exists())

    def test_exec_discards_an_inherited_oauth_token_for_every_account_selection(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.seed_room("main@example.invalid", 7)
        self.seed_room("unit@example.invalid", 2)
        selections = (
            ("named account", "claude 3", False, str(accounts.account_dir("claude 3"))),
            ("default login alias", "claude 2", False, ""),
            ("named unit role", "unit", True, str(accounts.account_dir("claude 3"))),
            ("legacy unit key absent", "unit", False, str(accounts.account_dir("claude 3"))),
        )
        for selection, label, named_unit, config_dir in selections:
            with self.subTest(selection=selection):
                if named_unit:
                    self.select_named_unit("claude 3")
                else:
                    accounts.accounts_config_file().unlink(missing_ok=True)
                with (
                    patch.dict(os.environ, {
                        "CLAUDE_CODE_OAUTH_TOKEN": "another-account-token",
                        "CLAUDE_CONFIG_DIR": "inherited",
                        "CLAUDE_CODE_REMOTE_MEMORY_DIR": "inherited",
                        "UNCHANGED": "value",
                    }),
                    patch.object(os, "execvpe") as launch,
                ):
                    expected_environment = dict(os.environ)
                    del expected_environment["CLAUDE_CODE_OAUTH_TOKEN"]
                    if config_dir:
                        expected_environment.update(named_environment(Path(config_dir)))
                    else:
                        del expected_environment["CLAUDE_CONFIG_DIR"]
                        del expected_environment["CLAUDE_CODE_REMOTE_MEMORY_DIR"]
                    self.assertEqual(self.run_cli("exec", label, "--version"), (0, "", ""))
                    launch.assert_called_once_with(
                        "claude", ["claude", "--version"], expected_environment,
                    )
                    self.assertEqual(os.environ["CLAUDE_CODE_OAUTH_TOKEN"], "another-account-token")

    def test_exec_allows_a_set_up_logged_out_directory_to_log_in_but_check_refuses_it(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.write_identity("claude 3", "unit@example.invalid")
        config = accounts.account_dir("claude 3") / ".claude.json"
        for content in ("{}", "missing"):
            with self.subTest(content=content):
                if content == "missing":
                    config.unlink()
                else:
                    _ = config.write_text(content)
                readiness = accounts.account_directory_readiness("claude 3")
                self.assertIsInstance(readiness, accounts.AccountDirectoryLoggedOut)
                self.assertIn("not logged in", "\n".join(accounts.check("claude 3")))
                with patch.object(os, "execvpe") as launch:
                    self.assertEqual(self.run_cli("exec", "claude 3"), (0, "", ""))
                launch.assert_called_once_with("claude", ["claude"], {**os.environ, **named_environment(accounts.account_dir("claude 3"))})

    def test_exec_allows_login_before_a_new_label_has_a_note(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude 3"), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(len(accounts.check("claude 3")), 2)

    def test_exec_refuses_wrong_login_and_status_keeps_the_actual_identity(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "expected@example.invalid")
        self.write_identity("claude 3", "other@example.invalid")
        line = "accounts: ~/.claude3 is logged in as other@example.invalid, not claude 3; see Log out and in in ~/.claude/docs/accounts.md"
        self.assertIsInstance(accounts.account_directory_readiness("claude 3"), accounts.AccountDirectoryWrongLogin)
        self.assertEqual(accounts.check("claude 3"), [line])
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude 3"), (2, "", line + "\n"))
        launch.assert_not_called()
        result = self.run_cli("status")
        self.assertIn("| claude 3 | other@example.invalid ", result[1])
        self.assertIn(line, result[1])
        self.assertEqual(accounts.account_identity(accounts.account_dir("claude 3")), agent_accounts.KnownClaudeAccount("other@example.invalid", "other@example.invalid"))

    def test_exec_refuses_broken_shared_links_even_when_logged_out(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        (self.main_dir / "projects").rmdir()
        readiness = accounts.account_directory_readiness("claude 3")
        self.assertIsInstance(readiness, accounts.AccountDirectoryBrokenLinks)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 3")
        self.assertEqual(result, (2, "", f"accounts: {accounts.account_dir('claude 3') / 'projects'} is broken\n"))
        launch.assert_not_called()
        self.assertIn("not logged in", "\n".join(readiness.problems))

    def test_removed_optional_shared_source_refuses_check_and_exec(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        self.login_directory("claude 3", "unit@example.invalid")
        source.unlink()
        linked = accounts.account_dir("claude 3") / "settings.json"
        problem = f"accounts: {linked} is broken"
        self.assertEqual(accounts.check("claude 3"), [problem])
        self.assertIsInstance(accounts.account_directory_readiness("claude 3"), accounts.AccountDirectoryBrokenLinks)
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude 3"), (2, "", problem + "\n"))
        launch.assert_not_called()
        self.assertEqual(accounts.checked_named_accounts(), [])

    def test_exec_refuses_linked_directory_and_identity_files(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        for name in accounts.IDENTITY_ENTRIES:
            with self.subTest(name=name):
                path = accounts.account_dir("claude 3") / name
                original = path.read_bytes() if path.exists() else b""
                path.unlink(missing_ok=True)
                path.symlink_to(self.root / "external")
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude 3")
                self.assertEqual(result[0], 2)
                self.assertIn("identity stays in each account dir", result[2])
                launch.assert_not_called()
                path.unlink()
                if original:
                    _ = path.write_bytes(original)
        linked = accounts.account_dir("claude 4")
        linked.symlink_to(accounts.account_dir("claude 3"), target_is_directory=True)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 4")
        self.assertEqual(result[0], 2)
        self.assertIn("an account dir must be a real directory", result[2])
        launch.assert_not_called()

    def test_exec_refuses_malformed_existing_identity_instead_of_treating_it_as_logged_out(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        path = accounts.account_dir("claude 3") / ".claude.json"
        for content in ("not JSON", "[]", '{"oauthAccount": []}', '{"oauthAccount": {"emailAddress": 1}}', '{"oauthAccount": {}}'):
            with self.subTest(content=content):
                _ = path.write_text(content)
                self.assertIsInstance(accounts.account_directory_readiness("claude 3"), accounts.AccountDirectoryUnreadable)
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude 3")
                self.assertEqual(result[0], 2)
                self.assertIn(".claude.json", result[2])
                self.assertNotIn("not logged in", result[2])
                launch.assert_not_called()

    def test_exec_refuses_each_unreadable_identity_file(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        original_read = Path.read_text
        for name in accounts.IDENTITY_ENTRIES:
            with self.subTest(name=name):
                blocked = accounts.account_dir("claude 3") / name
                _ = blocked.touch(exist_ok=True)

                def deny(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                    if path == blocked:
                        raise PermissionError("fixture read denied")
                    return original_read(path, encoding=encoding, errors=errors)

                with patch.object(Path, "read_text", deny), patch.object(os, "execvpe") as launch:
                    readiness = accounts.account_directory_readiness("claude 3")
                    result = self.run_cli("exec", "claude 3")
                self.assertIsInstance(readiness, accounts.AccountDirectoryUnreadable)
                self.assertEqual(result[0], 2)
                self.assertIn("fixture read denied", result[2])
                launch.assert_not_called()
                if name != ".claude.json":
                    blocked.unlink()

    def test_exec_refuses_missing_or_unreadable_account_notes_for_a_logged_in_directory(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md"
        for content in (b"missing", b"body without frontmatter", b"---\nlogin: null\n---\n", b"\xff"):
            with self.subTest(content=content):
                if content == b"missing":
                    note.unlink()
                else:
                    _ = note.write_bytes(content)
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude 3")
                self.assertEqual(result[0], 2)
                self.assertIn("claude3.md", result[2])
                launch.assert_not_called()

    def test_exec_refuses_an_unreadable_note_even_when_the_directory_is_logged_out(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        _ = (accounts.account_dir("claude 3") / ".claude.json").write_text("{}")
        _ = (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").write_text("not frontmatter")
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude 3")
        self.assertEqual(result[0], 2)
        self.assertIn("has no readable login", result[2])
        self.assertNotIn("not logged in", result[2])
        self.assertEqual(len(accounts.check("claude 3")), 2)
        launch.assert_not_called()

    def test_status_reports_unreadable_notes_without_erasing_other_account_identities(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.login_directory("claude 4", "spare@example.invalid")
        self.select_named_unit("claude 3")
        _ = (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").write_bytes(b"\xff")
        result = self.run_cli("status")
        self.assertEqual((result[0], result[2]), (0, ""))
        self.assertIn("| claude 3 | unit@example.invalid ", result[1])
        self.assertIn("could not read", result[1])
        self.assertIn("| claude 4 | spare@example.invalid ", result[1])
        selection = accounts.claude_account_selection()
        self.assertEqual([named.identity.label for named in selection.named_accounts], ["claude 4"])
        self.assertTrue(selection.is_default_login("main@example.invalid"))

    def test_exec_reports_an_unavailable_claude_executable_with_exit_two(self) -> None:
        with patch.object(os, "execvpe", side_effect=FileNotFoundError("fixture executable missing")):
            result = self.run_cli("exec", "default")
        self.assertEqual(result, (2, "", "accounts: could not start claude: fixture executable missing\n"))

    def test_check_wrong_login_or_missing_note_removes_the_named_account_and_unit_role(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        self.write_identity("claude 3", "other@example.invalid")
        for missing_note in (False, True):
            with self.subTest(missing_note=missing_note):
                if missing_note:
                    (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
                self.assertTrue(accounts.check("claude 3"))
                self.assertEqual(accounts.checked_named_accounts(), [])
                selection = accounts.claude_account_selection()
                self.assertEqual(selection.named_accounts, ())
                self.assertFalse(selection.multiple_accounts)

    def test_check_retains_link_identity_and_note_failures_in_one_readiness_value(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        (accounts.account_dir("claude 3") / "projects").unlink()
        _ = (accounts.account_dir("claude 3") / ".credentials.json").write_text("invalid JSON")
        self.write_identity("claude 3", "other@example.invalid")
        problems = accounts.check("claude 3")
        self.assertEqual(len(problems), 3)
        self.assertIn("projects", problems[0])
        self.assertIn(".credentials.json", problems[1])
        self.assertIn("logged in as other@example.invalid", problems[2])

    def test_check_missing_note_alone_excludes_an_otherwise_matching_account(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
        self.assertEqual(accounts.check("claude 3"), [f"accounts: claude 3 has no account note at {Path(os.environ['AGENT_NOTES_DIR']) / 'claude3.md'}"])
        self.assertEqual(accounts.claude_account_selection().named_accounts, ())
        self.assertTrue(accounts.multiple_accounts_configured())

    def test_check_matches_the_requested_note_case_insensitively_even_when_identity_label_differs(self) -> None:
        self.login_directory("claude 2", "MAIN@example.invalid", default=True)
        self.login_directory("claude 3", "main@example.invalid")
        self.assertEqual(accounts.check("claude 3"), [])
        self.assertEqual(accounts.account_identity(accounts.account_dir("claude 3")), agent_accounts.KnownClaudeAccount("main@example.invalid", "claude 2"))



    def prepare_swap(self, *, shared_default_login: bool = False) -> accounts.SwapInventory:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory(
            "claude 3", "main@example.invalid" if shared_default_login else "old@example.invalid",
            note_label="claude 2" if shared_default_login else "claude 3",
        )
        self.login_directory("claude 1", "new@example.invalid")
        self.select_named_unit("claude 3")
        return accounts.SwapInventory(
            machine="natedev", login="main@example.invalid" if shared_default_login else "old@example.invalid",
            label="claude 2" if shared_default_login else "claude 3",
            sessions=[accounts.SwapUnitSession(kind="unit", session_id="unit-id", name="unit-one")],
            unattributed=[],
        )

    def role_files(self) -> dict[str, str]:
        return {str(path): path.read_text(encoding="utf-8")
                for path in (accounts.accounts_config_file(), accounts.roles_history_file()) if path.exists()}

    def swap_cli(
        self, inventory: object, label: str = "claude 1", *, dry_run: bool = False,
        down_code: int = 0, down_stdout: str = "shutdown started\n", down_stderr: str = "",
    ) -> tuple[tuple[int, str, str], list[tuple[str, ...]]]:
        commands: list[tuple[str, ...]] = []

        def shutdown(argv: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            if argv[2] == "status":
                self.assertEqual(argv[4:], ("--json",))
                return subprocess.CompletedProcess(argv, 0, json.dumps(inventory), "")
            self.assertEqual(argv[2:], ("down", argv[3], "--restart", "--to", label))
            current = accounts.role_account("unit")
            if label == "default":
                self.assertIsInstance(current, accounts.DefaultAccount)
            else:
                self.assertEqual(current, accounts.NamedAccount(label, accounts.account_dir(label)))
            return subprocess.CompletedProcess(argv, down_code, down_stdout, down_stderr)

        with patch.object(subprocess, "run", side_effect=shutdown):
            result = self.run_cli("swap", "unit", label, *(["--dry-run"] if dry_run else []))
        return result, commands
























    def test_burn_keeps_historical_account_identity_after_logout_or_another_login(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 1", account_label="claude 1")])
        self.write_readings([accounts.QuotaReading(account="claude 1", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
                            for day, left in ((9, 90.0), (10, 80.0))])
        result = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertIn(accounts.AccountRoleBurn("claude 1", ("unit directors",), 10, 1), result)

    def test_legacy_default_role_history_refuses_an_unreadable_identity(self) -> None:
        self.write_role_history([accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="default")])
        with self.assertRaisesRegex(accounts.AccountsError, "account identity for default is unreadable"):
            _ = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))

    def test_account_selection_uses_whoami_accounts_in_use_type_and_checked_directories(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        selection = accounts.claude_account_selection()
        self.assertIsInstance(selection, agent_accounts.ClaudeAccountsInUse)
        self.assertIsInstance(selection.default_account, agent_accounts.KnownClaudeAccount)
        self.assertIsInstance(selection.named_accounts[0], agent_accounts.CheckedNamedClaudeAccount)
        self.assertTrue(selection.is_default_login("MAIN@example.invalid"))
        self.assertFalse(hasattr(selection, "unit_account"))
        self.assertFalse(hasattr(selection, "roles_for_login"))

    def test_unfit_named_accounts_are_excluded_from_checked_directories(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        (accounts.account_dir("claude 3") / "projects").unlink()
        self.assertEqual(accounts.checked_named_accounts(), [])
        self.assertEqual(accounts.claude_account_selection().named_accounts, ())

    def test_burn_maps_a_unit_directory_to_its_shared_login_note_identity(self) -> None:
        """A directory label is not the quota account's note label."""
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "MAIN@example.invalid")
        self.select_named_unit("claude 3")
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
        ])
        self.write_readings([
            accounts.QuotaReading(account="claude 2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(results, [accounts.AccountRoleBurn("claude 2", ("showrunners", "unit directors"), 10, 1)])
        accounts.roles_history_file().unlink()
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)), results)

    def test_burn_refuses_an_unreadable_historical_account_identity(self) -> None:
        """Missing identity data must not be replaced by a directory label."""
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
        ])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-10T00:00:00Z")
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("account identity for claude 3 is unreadable", stderr)

    def test_burn_after_unit_role_retirement_labels_default_and_old_unit_by_account_only(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
                                                     label="claude 3", account_label="claude 3")])
        self.write_readings([accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude 2", "claude 3")
            for stamp, left in (("2030-01-01T00:00:00Z", 90), ("2030-01-02T00:00:00Z", 80))])
        results = accounts.burn(datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(results, [accounts.AccountRoleBurn(label, (), 10, 1) for label in ("claude 2", "claude 3")])
        code, output, error = self.run_cli("burn", "--since", "2030-01-01T00:00:00Z", "--until", "2030-01-02T00:00:00Z")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines(), [f"{label}: 10 points/day (10 points over 1 observed days)"
                                              for label in ("claude 2", "claude 3")])

    def test_burn_splits_observed_spend_at_unit_role_retirement(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
                                                     label="claude 3", account_label="claude 3")])
        self.write_readings([accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude 2", "claude 3")
            for stamp, left in (("2026-10-12T00:00:00Z", 90), ("2026-10-14T00:00:00Z", 70))])
        results = accounts.burn(datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc))
        self.assertCountEqual(results, [accounts.AccountRoleBurn("claude 2", ("showrunners",), 10, 1),
                                       accounts.AccountRoleBurn("claude 3", ("unit directors",), 10, 1),
                                       accounts.AccountRoleBurn("claude 2", (), 10, 1),
                                       accounts.AccountRoleBurn("claude 3", (), 10, 1)])

    def test_showrunner_group_move_names_every_unregistered_timer_even_without_live_members(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "other@example.invalid")
        inventory = import_module("inventory")
        stale = [showrunners.Showrunner(session="", socket="", slug=slug, zone="America/New_York",
                                       doc=f"/w/{slug}-production.md") for slug in ("first", "second")]
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "unregistered_showrunners", return_value=stale),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude 3")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude 3")
        self.assertEqual(output.replace("`", "").splitlines(), ["showrunners: no live session moves", *(
            f"{runner['slug']}: no session runs under the id its update timer names; "
            + "a showrunner running under another id is not moved until it runs "
            + f"update_registration.py register --production {runner['doc']}" for runner in stale)])
        moved.assert_not_called()

    def test_showrunner_group_preview_names_unregistered_timers_without_configuring_or_requesting(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "other@example.invalid")
        inventory = import_module("inventory")
        stale = showrunners.Showrunner(session="", socket="", slug="missing", zone="America/New_York",
                                       doc="/w/missing-production.md")
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "unregistered_showrunners", return_value=[stale]),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude 3", "--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.replace("`", "").splitlines(), ["showrunners: would select claude 3",
            "showrunners: no live session moves",
            "missing: no session runs under the id its update timer names; a showrunner running under "
            + "another id is not moved until it runs update_registration.py register --production /w/missing-production.md"])
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())
        moved.assert_not_called()

    def test_burn_after_role_retirement_without_history_never_defaults_to_showrunners(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_readings([accounts.QuotaReading(account="claude 2", at=stamp, remaining=left)
                            for stamp, left in (("2030-01-01T00:00:00Z", 90), ("2030-01-02T00:00:00Z", 80))])
        self.assertEqual(accounts.burn(datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 2, tzinfo=timezone.utc)),
                         [accounts.AccountRoleBurn("claude 2", (), 10, 1)])

    def test_burn_reports_each_account_with_its_historical_role(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        self.write_role_history([accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3")])
        self.write_readings([
            accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label, values in (("claude 1", (98.0, 94.0, 90.0)),
                                  ("claude 2", (80.0, 70.0, 60.0)),
                                  ("claude 3", (90.0, 80.0, 70.0)))
            for stamp, left in zip(("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z"), values)
        ])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-11T00:00:00Z")
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout.splitlines(), [
            "claude 1: 4 points/day (8 points over 2 observed days)",
            "claude 2 (showrunners): 10 points/day (20 points over 2 observed days)",
            "claude 3 (unit directors): 10 points/day (20 points over 2 observed days)",
        ])

    def test_burn_splits_a_reading_interval_at_a_role_swap(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "old-unit@example.invalid")
        self.login_directory("claude 1", "new-unit@example.invalid")
        self.select_named_unit("claude 1")
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
            accounts.LegacyRoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude 1"),
        ])
        self.write_readings([
            accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude 1", "claude 3")
            for stamp, left in (("2026-10-09T00:00:00Z", 90.0), ("2026-10-11T00:00:00Z", 70.0))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertEqual(len(results), 4)
        self.assertEqual({(result.label, result.roles) for result in results}, {
            ("claude 1", ()), ("claude 1", ("unit directors",)),
            ("claude 3", ()), ("claude 3", ("unit directors",)),
        })
        for result in results:
            self.assertEqual((result.points, result.observed_days, result.points_per_day), (10.0, 1.0, 10.0))

    def test_burn_default_assignment_uses_default_login_note_label(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude 3"),
            accounts.LegacyRoleChange(at="2026-10-11T00:00:00Z", role="unit", label="default"),
        ])
        self.write_readings([
            accounts.QuotaReading(account="claude 2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=float(left))
            for day, left in ((9, 90), (10, 80), (11, 70), (12, 60))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))
        by_roles = {result.roles: result for result in results}
        self.assertEqual(by_roles[("showrunners", "unit directors")].points, 20.0)
        self.assertEqual(by_roles[("showrunners",)].points, 10.0)

    def test_burn_weekly_refill_starts_a_new_observed_span(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude 3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=float(left))
            for day, left in ((9, 10), (10, 0), (11, 100), (12, 80))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].points, results[0].observed_days, results[0].points_per_day), (30.0, 2.0, 15.0))

    def test_burn_clips_observed_intervals_to_the_requested_window(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude 3", at="2026-10-09T00:00:00Z", remaining=90),
            accounts.QuotaReading(account="claude 3", at="2026-10-11T00:00:00Z", remaining=50),
        ])
        results = accounts.burn(datetime(2026, 10, 9, 12, tzinfo=timezone.utc), datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        self.assertEqual((results[0].points, results[0].observed_days), (20.0, 1.0))

    def test_burn_skips_invalid_readings_and_orders_observations(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude 3", at="2026-10-11T00:00:00Z", remaining=50),
            accounts.QuotaReading(account="claude 3", at="2026-10-09T00:00:00Z", remaining=90),
        ])
        with accounts.readings_file().open("a", encoding="utf-8") as output:
            for item in (False, {"account": "claude 3", "at": "bad", "remaining": 9},
                         {"account": "claude 3", "at": "2026-10-10T00:00:00Z", "remaining": True},
                         {"account": "claude 3", "at": "2026-10-10T00:00:00Z", "remaining": float("nan")},
                         {"account": "claude 3", "at": "2026-10-10T00:00:00", "remaining": 60},
                         {"account": "claude 3", "at": "2026-10-10T00:00:00Z", "remaining": 101}):
                _ = output.write(json.dumps(item) + "\n")
            _ = output.write("not JSON\n")
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertEqual((results[0].points, results[0].points_per_day), (40.0, 20.0))

    def test_burn_needs_paired_observations_and_does_not_write_state(self) -> None:
        self.write_readings([accounts.QuotaReading(account="claude 3", at="2026-10-09T00:00:00Z", remaining=90)])
        before = accounts.readings_file().read_text(encoding="utf-8")
        self.assertEqual(self.run_cli("burn", "--since", "2026-10-09T00:00:00Z"),
                         (0, "No paired quota readings in this window.\n", ""))
        self.assertEqual(accounts.readings_file().read_text(encoding="utf-8"), before)
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertFalse(accounts.accounts_config_file().exists())

    def test_burn_cli_accepts_los_angeles_wall_times(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude 3", at="2026-10-09T07:00:00Z", remaining=90),
            accounts.QuotaReading(account="claude 3", at="2026-10-10T07:00:00Z", remaining=70),
        ])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00", "--until", "2026-10-10T00:00:00")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("20 points/day (20 points over 1 observed days)", stdout)

    def test_burn_refuses_invalid_windows_and_corrupt_role_history(self) -> None:
        for since, until in (("bad", "2026-10-10T00:00:00Z"),
                             ("2026-10-10T00:00:00Z", "2026-10-09T00:00:00Z")):
            with self.subTest(since=since):
                result, stdout, stderr = self.run_cli("burn", "--since", since, "--until", until)
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn("accounts:", stderr)
        self.write_role_history([accounts.LegacyRoleChange(at="bad", role="unit", label="claude 3")])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-10T00:00:00Z")
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("invalid role change", stderr)

    def test_shared_entries_are_the_documented_complete_set(self) -> None:
        self.assertEqual(tuple(accounts.SHARED), EXPECTED_SHARED)

    def test_setup_creates_real_symlinks_and_seed_with_private_modes(self) -> None:
        self.write_main_config()
        _ = (self.main_dir / "settings.json").write_text("{}", encoding="utf-8")
        _ = (self.main_dir / "commands").mkdir()

        result, stdout, stderr = self.run_cli("setup", "claude 2")

        account_dir = self.home / ".claude2"
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(account_dir))} claude\n/login\n",
        )
        self.assertEqual(account_dir.stat().st_mode & 0o777, 0o700)
        for entry in ("settings.json", "commands", "projects", "sessions"):
            link = account_dir / entry
            self.assertTrue(link.is_symlink(), entry)
            self.assertEqual(os.readlink(link), str(self.main_dir / entry))
        self.assertFalse((account_dir / "skills").exists())

        seeded_path = account_dir / ".claude.json"
        self.assertEqual(seeded_path.stat().st_mode & 0o777, 0o600)
        seeded = cast(dict[str, object], json.loads(seeded_path.read_text()))
        self.assertEqual(
            set(seeded),
            {
                "hasCompletedOnboarding",
                "lastOnboardingVersion",
                "installMethod",
                "autoUpdates",
                "shiftEnterKeyBindingInstalled",
                "mcpServers",
                "remoteDialogSeen",
                "hasUsedRemoteControl",
                "hasSeenAutoModeEntryWarning",
                "hasSeenAutoModeOutsideReadPrompt",
                "hasResetAutoModeOptInForDefaultOffer",
                "projects",
            },
        )
        self.assertNotIn("oauthAccount", seeded)
        projects = cast(dict[str, object], seeded["projects"])
        self.assertEqual(set(projects), {"/trusted"})
        trusted = cast(dict[str, object], projects["/trusted"])
        self.assertEqual(
            set(trusted),
            {
                "hasTrustDialogAccepted",
                "allowedTools",
                "enabledMcpjsonServers",
                "disabledMcpjsonServers",
                "mcpServers",
                "hasClaudeMdExternalIncludesApproved",
                "hasClaudeMdExternalIncludesWarningShown",
            },
        )

    def test_setup_creates_missing_required_shared_targets_before_linking(self) -> None:
        self.write_main_config()
        for name in accounts.ALWAYS_LINKED:
            self.assertFalse((self.main_dir / name).exists())
        self.assertEqual(accounts.setup("claude 3"), [])
        for name in accounts.ALWAYS_LINKED:
            target = self.main_dir / name
            linked = accounts.account_dir("claude 3") / name
            self.assertTrue(target.is_dir())
            self.assertTrue(linked.is_symlink())
            self.assertTrue(linked.is_dir())
            self.assertEqual(linked.resolve(), target)
        readiness = accounts.account_directory_readiness("claude 3")
        self.assertIsInstance(readiness, accounts.AccountDirectoryLoggedOut)
        self.assertFalse(any("broken" in problem for problem in readiness.problems))

    def test_setup_removes_an_obsolete_expected_shared_link_and_restores_readiness(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        self.login_directory("claude 3", "unit@example.invalid")
        source.unlink()
        linked = accounts.account_dir("claude 3") / "settings.json"
        self.assertTrue(linked.is_symlink())
        self.assertEqual(accounts.setup("claude 3"), [])
        self.assertFalse(os.path.lexists(linked))
        self.assertFalse(source.exists())
        self.assertEqual(accounts.check("claude 3"), [])
        self.assertIsInstance(accounts.account_directory_readiness("claude 3"), accounts.AccountDirectoryReady)

    def test_missing_optional_sources_report_wrong_links_and_real_entries_without_removing_them(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        destination = accounts.account_dir("claude 3")
        outside = self.root / "outside-settings"
        _ = outside.write_text("outside", encoding="utf-8")
        linked = destination / "settings.json"
        linked.symlink_to(outside)
        local = destination / "CLAUDE.md"
        _ = local.write_text("private content", encoding="utf-8")
        self.assertEqual(accounts.check("claude 3"), [
            f"accounts: {linked} points to {outside}, not {self.main_dir / 'settings.json'}",
            f"accounts: {local} is a real file; move it aside or merge it into ~/.claude",
        ])
        _ = accounts.setup("claude 3")
        self.assertEqual(os.readlink(linked), str(outside))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        self.assertEqual(local.read_text(encoding="utf-8"), "private content")

    def test_setup_leaves_correct_link_and_existing_seed_untouched(self) -> None:
        self.write_main_config()
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        link = account_dir / "settings.json"
        _ = link.symlink_to(source)
        original_inode = link.lstat().st_ino
        seed = account_dir / ".claude.json"
        _ = seed.write_text('{"keep": true}', encoding="utf-8")

        result, _, _ = self.run_cli("setup", "claude 2")

        self.assertEqual(result, 0)
        self.assertEqual(link.lstat().st_ino, original_inode)
        self.assertEqual(seed.read_text(encoding="utf-8"), '{"keep": true}')

    def test_setup_refuses_each_linked_identity_entry_without_replacing_it(self) -> None:
        self.write_main_config()
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        targets: dict[str, Path] = {}
        for name in accounts.IDENTITY_ENTRIES:
            target = self.root / f"target-{name.removeprefix('.')}"
            _ = target.write_text("identity", encoding="utf-8")
            _ = (account_dir / name).symlink_to(target)
            targets[name] = target

        result, _, stderr = self.run_cli("setup", "claude 2")

        self.assertEqual(result, 1)
        for name, target in targets.items():
            path = account_dir / name
            self.assertIn(
                f"accounts: {path} is a link; identity stays in each account dir\n",
                stderr,
            )
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), str(target))

    def test_problem_helpers_return_empty_or_populated_lists(self) -> None:
        repair_link = cast(
            Callable[[Path, Path], list[str]],
            getattr(accounts, "_repair_link"),
        )
        seed_config = cast(
            Callable[[Path], list[str]],
            getattr(accounts, "_seed_config"),
        )
        login_problem = cast(
            Callable[[Path], list[str]],
            getattr(accounts, "_login_problem"),
        )
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        link = account_dir / "settings.json"

        self.assertEqual(repair_link(link, source), [])
        self.assertEqual(repair_link(link, source), [])
        self.write_main_config()
        self.assertEqual(seed_config(account_dir / ".claude.json"), [])
        with patch.object(account, "claude_account", return_value=self.logged_in()):
            self.assertEqual(login_problem(account_dir), [])

    def test_setup_replaces_a_wrong_target_link(self) -> None:
        self.write_main_config()
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        wrong = self.root / "wrong"
        _ = (account_dir / "settings.json").symlink_to(wrong)

        result, _, _ = self.run_cli("setup", "claude 2")

        self.assertEqual(result, 0)
        self.assertEqual(os.readlink(account_dir / "settings.json"), str(source))

    def test_setup_reports_each_real_entry_and_finishes_the_rest(self) -> None:
        self.write_main_config()
        _ = (self.main_dir / "settings.json").write_text("{}", encoding="utf-8")
        _ = (self.main_dir / "commands").mkdir()
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        _ = (account_dir / "settings.json").write_text("mine", encoding="utf-8")
        _ = (account_dir / "commands").mkdir()

        result, stdout, stderr = self.run_cli("setup", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(
            stdout,
            f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(account_dir))} claude\n/login\n",
        )
        self.assertEqual(
            stderr,
            "".join(
                f"accounts: {account_dir}/{entry} is a real file; move it aside or merge it into ~/.claude\n"
                for entry in ("settings.json", "commands")
            ),
        )
        self.assertTrue((account_dir / "projects").is_symlink())
        self.assertTrue((account_dir / "sessions").is_symlink())
        self.assertTrue((account_dir / ".claude.json").is_file())

    def test_setup_and_check_refuse_an_account_directory_link_without_writes(
        self,
    ) -> None:
        target = self.root / "must stay untouched"
        _ = target.mkdir()
        marker = target / "marker"
        _ = marker.write_text("unchanged", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.symlink_to(target, target_is_directory=True)

        setup_result, _, setup_stderr = self.run_cli("setup", "claude 2")
        with patch.object(account, "claude_account") as login_read:
            check_result, check_stdout, _ = self.run_cli("check", "claude 2")

        problem = (
            f"accounts: {account_dir} is a link; "
            "an account dir must be a real directory\n"
        )
        self.assertEqual(setup_result, 1)
        self.assertEqual(setup_stderr, problem)
        self.assertEqual(check_result, 1)
        self.assertEqual(check_stdout, problem)
        login_read.assert_not_called()
        self.assertEqual(set(target.iterdir()), {marker})
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_setup_and_check_include_a_dangling_shared_entry(self) -> None:
        self.write_main_config()
        source = self.main_dir / "settings.json"
        _ = source.symlink_to(self.root / "missing-target")

        setup_result, _, setup_stderr = self.run_cli("setup", "claude 2")

        account_dir = self.home / ".claude2"
        linked = account_dir / "settings.json"
        self.assertEqual(setup_result, 0)
        self.assertEqual(setup_stderr, "")
        self.assertTrue(linked.is_symlink())
        self.assertEqual(os.readlink(linked), str(source))
        self.write_note("claude 2", "owner@example.invalid")
        self.write_identity("claude 2", "owner@example.invalid")
        problems = accounts.check("claude 2")
        self.assertIn(f"accounts: {linked} is broken", problems)

    def test_invalid_label_is_refused_without_creating_a_directory(self) -> None:
        for command in ("setup", "check"):
            with self.subTest(command=command):
                result, stdout, stderr = self.run_cli(command, "claude2")
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "accounts: claude2 is not a claude N label\n")
                self.assertEqual(set(self.home.iterdir()), {self.main_dir})

    def test_check_is_silent_for_a_complete_logged_in_account(self) -> None:
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        self.write_identity("claude 2", "owner@example.invalid")
        self.write_note("claude 2", "owner@example.invalid")
        result, stdout, stderr = self.run_cli("check", "claude 2")

        self.assertEqual((result, stdout, stderr), (0, "", ""))

    def test_check_reports_missing_directory_without_reading_login(self) -> None:
        with patch.object(account, "claude_account") as login_read:
            result, stdout, stderr = self.run_cli("check", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, f"accounts: {self.home / '.claude2'} is missing\n")
        self.assertEqual(stderr, "")
        login_read.assert_not_called()

    def test_check_reports_every_link_problem_and_not_logged_in(self) -> None:
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        self.write_note("claude 2", "owner@example.invalid")
        _ = (account_dir / "settings.json").unlink()
        _ = (account_dir / "CLAUDE.md").unlink()
        _ = (account_dir / "CLAUDE.md").symlink_to(self.root / "wrong")
        _ = (account_dir / "keybindings.json").unlink()
        _ = (account_dir / "keybindings.json").write_text("real", encoding="utf-8")

        with patch.object(account, "claude_account", return_value=None):
            result, stdout, stderr = self.run_cli("check", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "".join(
                (
                    f"accounts: {account_dir / 'settings.json'} is missing\n",
                    f"accounts: {account_dir / 'CLAUDE.md'} points to {self.root / 'wrong'}, not {self.main_dir / 'CLAUDE.md'}\n",
                    f"accounts: {account_dir / 'keybindings.json'} is a real file; move it aside or merge it into ~/.claude\n",
                    f"accounts: {account_dir} is not logged in\n",
                )
            ),
        )

    def test_check_rejects_a_linked_identity_without_reading_its_login(self) -> None:
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        self.write_note("claude 2", "owner@example.invalid")
        _ = (account_dir / ".claude.json").symlink_to(self.home / ".claude.json")

        with patch.object(account, "claude_account") as login_read:
            result, stdout, stderr = self.run_cli("check", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"accounts: {account_dir / '.claude.json'} is a link; "
            + "identity stays in each account dir\n",
        )
        login_read.assert_not_called()

    def test_status_describes_accounts_without_creating_any(self) -> None:
        _ = (self.main_dir / "README.md").write_text("repo entry", encoding="utf-8")
        self.write_main_config()
        before = set(self.home.iterdir())
        login = self.logged_in()

        with (
            patch.object(account, "claude_account", return_value=login),
            patch.object(account, "label_for", return_value="claude 2"),
        ):
            result, stdout, stderr = self.run_cli("status")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("| ~/.claude | claude 2 | owner@example.invalid |", stdout)
        self.assertNotIn("unit directors", stdout)
        self.assertNotIn("unclassified:", stdout)
        self.assertNotIn("mcpServers:", stdout)
        self.assertEqual(set(self.home.iterdir()), before)
        self.assertFalse(any(self.home.glob(".claude[0-9]*")))

    def test_status_reports_an_account_directory_link_without_inspecting_it(
        self,
    ) -> None:
        target = self.root / "linked-account-target"
        _ = target.mkdir()
        account_dir = self.home / ".claude2"
        _ = account_dir.symlink_to(target, target_is_directory=True)

        with patch.object(
            account, "claude_account", return_value=self.logged_in()
        ) as login_read:
            result, stdout, stderr = self.run_cli("status")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn(
            f"accounts: {account_dir} is a link; "
            + "an account dir must be a real directory",
            stdout,
        )
        self.assertIn("| ~/.claude2 | claude 2 | unavailable ", stdout)
        self.assertNotIn("mcpServers:", stdout)
        self.assertEqual(
            login_read.call_args_list,
            [call(self.main_dir)] * login_read.call_count,
        )

    def test_status_shows_role_problems_unclassified_entries_and_mcp_difference(
        self,
    ) -> None:
        self.write_main_config()
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        _ = (account_dir / "mystery-cache").mkdir()
        # Claude Code's own entries in each folder are not reported.
        _ = (account_dir / "cache").mkdir()
        _ = (account_dir / ".last-cleanup").write_text("2026-10-12T00:00:00Z", encoding="utf-8")
        _ = (account_dir / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "account-only": {"command": "account"},
                    }
                }
            ),
            encoding="utf-8",
        )
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        _ = (config_dir / "accounts.json").write_text(
            json.dumps({"unit": "claude 2"}), encoding="utf-8"
        )

        with (
            patch.object(account, "claude_account", return_value=None),
            patch.object(account, "label_for", return_value="unknown"),
        ):
            result, stdout, stderr = self.run_cli("status")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("| ~/.claude2 | claude 2 | not logged in |", stdout)
        self.assertIn("\n~/.claude2: unclassified: mystery-cache\n", stdout)
        self.assertIn("\n~/.claude2: mcpServers: - brp, + account-only\n", stdout)

    def reading(
        self, login: str, five_hour: agent_accounts.Quota | None, weekly: agent_accounts.Quota,
    ) -> agent_accounts.Report:
        return agent_accounts.Report("Claude", email=login, quotas=[weekly], five_hour=five_hour)

    def test_status_table_has_a_row_for_every_folder_logged_in_or_not(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        # A folder set up and never logged in, and claude 1: a note, and no folder holds it.
        # Claude 2 has no folder of its own and gets no second row: the default folder holds it.
        self.assertEqual(accounts.setup("claude 4"), [])
        self.write_note("claude 1", "spare@example.invalid")
        now = datetime.now(timezone.utc)
        read = now - timedelta(minutes=1)
        soon, later = now + timedelta(hours=2), now + timedelta(days=3)
        usage_store.save([
            self.reading("Main@example.invalid", agent_accounts.Quota("5-hour", 36.0, soon),
                         agent_accounts.Quota("Weekly", 29.4, later)),
            # No 5-hour window is open on this one.
            self.reading("unit@example.invalid", None, agent_accounts.Quota("Weekly", 88.0, later)),
        ], read)
        zone = ZoneInfo("America/Los_Angeles")
        read_cell = read.astimezone(zone).strftime("%H:%M %Z")
        weekly_cell = later.astimezone(zone).strftime("%a %m-%d %H:%M %Z")

        self.assertEqual(self.status_rows(), [
            ("~/.claude", "claude 2", "main@example.invalid", "",
             "64%", soon.astimezone(zone).strftime("%H:%M %Z"), "71%", weekly_cell, "—", "—", read_cell),
            ("~/.claude1", "claude 1", "not set up", "", *("—",) * 7),
            ("~/.claude3", "claude 3", "unit@example.invalid", "units",
             "100%", "—", "12%", weekly_cell, "—", "—", read_cell),
            ("~/.claude4", "claude 4", "not logged in", "", *("—",) * 7),
            CODEX_HOME_ROW,
        ])
        self.assertIn("PDT", read_cell + weekly_cell)
        self.live_reads.assert_not_called()
        self.codex_reads.assert_not_awaited()
        self.assertFalse(accounts.account_dir("claude 1").exists())
        self.assertFalse(accounts.account_dir("claude 2").exists())

    def test_no_action_means_status(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)

        result, stdout, stderr = self.run_cli()

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout, self.run_cli("status")[1])
        self.assertIn("| ~/.claude | claude 2 | main@example.invalid |", stdout)

    def test_status_table_is_padded_markdown_and_a_folder_in_order_adds_no_line(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.seed_room("main@example.invalid")

        result, stdout, stderr = self.run_cli("status")

        self.assertEqual((result, stderr), (0, ""))
        lines = [line for line in stdout.split("\n\n")[0].splitlines() if line.startswith("|")]
        # The heading, the rule, the default folder, and the Codex home.
        self.assertEqual(len(lines), 4)
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertEqual(set(lines[1]), {"|", " ", "-"})
        self.assertEqual([cell.strip() for cell in lines[2].strip("|").split("|")][:4], ["~/.claude", "claude 2", "main@example.invalid", ""])

    def test_status_reads_live_only_when_no_reading_is_recent_and_keeps_what_it_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.live[self.main_dir] = self.reading(
            "main@example.invalid", agent_accounts.Quota("5-hour", 10.0, later), agent_accounts.Quota("Weekly", 40.0, later),
        )

        first = self.status_rows()[0]
        second = self.status_rows()[0]

        self.assertEqual((first[4], first[6]), ("90%", "60%"))
        self.assertNotEqual(first[10], "—")
        self.assertEqual(second, first)
        self.live_reads.assert_called_once_with(self.main_dir)

    def test_status_table_shows_each_codex_account_and_whether_it_is_on_credits(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.codex_login("Seats@example.invalid")
        self.write_note("codex 1", "other@example.invalid")
        self.write_note("codex 2", "seats@example.invalid")
        now = datetime.now(timezone.utc)
        read = now - timedelta(minutes=1)
        later = now + timedelta(days=3)
        zone = ZoneInfo("America/Los_Angeles")
        used_up = agent_accounts.Quota("5-hour", 100.0, now + timedelta(hours=1))
        # Weekly percent used, the 5-hour window if the account has one, and the balance as Codex reports it.
        cases: list[tuple[float, agent_accounts.Quota | None, str, tuple[str, str]]] = [
            (100.0, None, "13446", ("yes", "13,446")),
            (81.0, None, "13446", ("no", "13,446")),
            (100.0, None, "0", ("no", "0")),
            (81.0, used_up, "unlimited", ("yes", "unlimited")),
            (100.0, None, "null", ("unavailable", "unavailable")),
        ]
        for weekly_used, five_hour, balance, credit_cells in cases:
            with self.subTest(weekly_used=weekly_used, balance=balance):
                usage_store.save([agent_accounts.Report(
                    "Codex", email="seats@example.invalid", quotas=[agent_accounts.Quota("Weekly", weekly_used, later)],
                    five_hour=five_hour, credit_balance=balance,
                )], read)

                rows = self.status_rows()

                self.assertEqual(rows[-2][:4], ("~/.codex", "codex 2", "Seats@example.invalid", "seats"))
                # An account with no 5-hour window shows none.
                self.assertEqual(rows[-2][4:6], ("—", "—") if five_hour is None else ("0%", rows[-2][5]))
                self.assertEqual(rows[-2][6:], (
                    f"{100 - weekly_used:.0f}%", later.astimezone(zone).strftime("%a %m-%d %H:%M %Z"),
                    *credit_cells, read.astimezone(zone).strftime("%H:%M %Z"),
                ))
                self.assertEqual(rows[-1], ("~/.codex1", "codex 1", "not set up", "—", *("—",) * 7))
        self.codex_reads.assert_not_awaited()

    def test_status_reads_codex_live_only_when_no_reading_is_recent_and_keeps_what_it_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.codex_login("seats@example.invalid")
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.codex_reads.return_value = agent_accounts.Report(
            "Codex", email="seats@example.invalid", quotas=[agent_accounts.Quota("Weekly", 30.0, later)],
            credit_balance="5",
        )

        first = self.status_rows()[-1]
        second = self.status_rows()[-1]

        # A login with no account note is identified by its home folder.
        self.assertEqual(first[:4], ("~/.codex", "~/.codex", "seats@example.invalid", "seats"))
        self.assertEqual((first[6], first[8], first[9]), ("70%", "no", "5"))
        self.assertNotEqual(first[10], "—")
        self.assertEqual(second, first)
        self.codex_reads.assert_awaited_once_with(self.home / ".codex")

    def test_codex_setup_requires_its_note_before_creating_a_home(self) -> None:
        result, stdout, stderr = self.run_cli("setup", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex 1", stderr)
        self.assertIn("note", stderr.lower())
        self.assertEqual(stdout, "")
        self.assertFalse((self.home / ".codex1").exists())
        self.codex_reads.assert_not_awaited()

    def test_codex_setup_refuses_a_login_already_held_by_the_default_home(self) -> None:
        self.write_note("codex 2", "seats@example.invalid")
        self.codex_login("SEATS@example.invalid")
        default = self.home / ".codex"
        authentication = (default / "auth.json").read_bytes()
        entries = set(self.home.iterdir())

        problems = accounts.setup("codex 2")

        self.assertEqual(len(problems), 1)
        self.assertIn("codex 2", problems[0])
        self.assertIn("~/.codex", problems[0])
        self.assertIn("two homes on one login break token refresh", problems[0])
        self.assertEqual(self.run_cli("setup", "codex 2"), (1, "", problems[0] + "\n"))
        self.assertFalse((self.home / ".codex2").exists())
        self.assertEqual(set(self.home.iterdir()), entries)
        self.assertEqual((default / "auth.json").read_bytes(), authentication)
        self.codex_reads.assert_not_awaited()

    def test_codex_setup_duplicate_login_refusal_preserves_an_existing_home(self) -> None:
        self.write_note("codex 2", "seats@example.invalid")
        self.codex_login("seats@example.invalid")
        default = self.home / ".codex"
        _ = (default / "config.toml").write_text("shared config")
        home = self.home / ".codex2"
        home.mkdir(mode=0o755)
        _ = (home / "config.toml").write_text("own config")
        before = home.stat().st_mode

        result, stdout, stderr = self.run_cli("setup", "codex 2")

        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("~/.codex already holds", stderr)
        self.assertEqual(home.stat().st_mode, before)
        self.assertEqual({entry.name for entry in home.iterdir()}, {"config.toml"})
        self.assertFalse((home / "config.toml").is_symlink())
        self.assertEqual((home / "config.toml").read_text(), "own config")
        self.assertEqual((default / "config.toml").read_text(), "shared config")

    def test_codex_setup_refuses_a_login_held_by_a_numbered_or_custom_home(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        for name in (".codex3", "custom-codex"):
            with self.subTest(home=name):
                other = self.home / name
                self.codex_login("spare@example.invalid", other)
                with patch.dict(os.environ, {"CODEX_HOME": str(other)}):
                    result, stdout, stderr = self.run_cli("setup", "codex 1")
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn(f"~/{name} already holds", stderr)
                self.assertFalse((self.home / ".codex1").exists())
                (other / "auth.json").unlink()
                other.rmdir()

    def test_codex_setup_links_existing_shared_entries_and_preserves_default_identity(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        self.codex_login("seats@example.invalid")
        default = self.home / ".codex"
        directories = {"skills", "rules", "plugins", "sessions", "archived_sessions"}
        for name in EXPECTED_CODEX_SHARED:
            source = default / name
            if name in directories:
                source.mkdir()
                _ = (source / "entry").write_text(name)
            else:
                _ = source.write_text(name)
        for name in ("state_5.sqlite", "installation_id", "version.json"):
            _ = (default / name).write_text("own state")
        before = (default / "auth.json").read_bytes()

        result, stdout, stderr = self.run_cli("setup", "codex 1")

        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("CODEX_HOME=~/.codex1 codex login", stdout)
        home = self.home / ".codex1"
        self.assertTrue(home.is_dir())
        self.assertFalse(home.is_symlink())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual({entry.name for entry in home.iterdir()}, set(EXPECTED_CODEX_SHARED))
        for name in EXPECTED_CODEX_SHARED:
            self.assertTrue((home / name).is_symlink(), name)
            self.assertEqual((home / name).resolve(), default / name)
        self.assertEqual((default / "auth.json").read_bytes(), before)
        self.codex_reads.assert_not_awaited()

    def test_codex_setup_omits_missing_shared_targets_and_keeps_existing_links(self) -> None:
        self.write_note("codex 2", "spare@example.invalid")
        default = self.home / ".codex"
        default.mkdir()
        _ = (default / "config.toml").write_text("config")
        self.assertEqual(self.run_cli("setup", "codex 2")[0], 0)
        home = self.home / ".codex2"
        linked = home / "config.toml"
        before = linked.lstat()
        self.codex_login("spare@example.invalid", home)
        authentication = (home / "auth.json").read_bytes()

        self.assertEqual(self.run_cli("setup", "codex 2")[0], 0)

        self.assertEqual(linked.lstat().st_ino, before.st_ino)
        self.assertEqual(linked.lstat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((home / "auth.json").read_bytes(), authentication)
        self.assertEqual({entry.name for entry in home.iterdir()}, {"config.toml", "auth.json"})
        self.assertEqual({entry.name for entry in default.iterdir()}, {"config.toml"})

    def test_codex_setup_reports_real_entries_without_replacing_them(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        default = self.home / ".codex"
        default.mkdir()
        _ = (default / "config.toml").write_text("shared")
        (default / "skills").mkdir()
        _ = (default / "hooks.json").write_text("shared hooks")
        home = self.home / ".codex1"
        home.mkdir(mode=0o700)
        _ = (home / "config.toml").write_text("own config")
        (home / "skills").mkdir()
        _ = (home / "skills" / "own").write_text("own skills")

        result, _, stderr = self.run_cli("setup", "codex 1")

        self.assertNotEqual(result, 0)
        self.assertIn("config.toml", stderr)
        self.assertIn("skills", stderr)
        self.assertEqual((home / "config.toml").read_text(), "own config")
        self.assertFalse((home / "skills").is_symlink())
        self.assertEqual((home / "skills" / "own").read_text(), "own skills")
        self.assertEqual((home / "hooks.json").resolve(), default / "hooks.json")

    def test_codex_check_accepts_own_state_files_and_matching_login(self) -> None:
        self.write_note("codex 1", "SPARE@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex1")
        home = self.home / ".codex1"
        for stem in ("state_5", "thread_history_1", "goals_1", "logs_2", "queue_1", "memories_1"):
            for suffix in (".sqlite", ".sqlite-wal", ".sqlite-shm"):
                _ = (home / f"{stem}{suffix}").write_text("own database")
        for name in ("app-server-control", "app-server-daemon", "cache", "tmp", "shell_snapshots", "packages"):
            (home / name).mkdir()
        for name in ("installation_id", "version.json"):
            _ = (home / name).write_text("own state")

        self.assertEqual(self.run_cli("check", "codex 1"), (0, "", ""))
        self.codex_reads.assert_not_awaited()

    def test_codex_check_reports_links_unknown_entry_and_login_mismatch_together(self) -> None:
        self.write_note("codex 1", "expected@example.invalid")
        self.codex_login("other@example.invalid", self.home / ".codex1")
        default = self.home / ".codex"
        default.mkdir()
        for name in ("config.toml", "hooks.json", "AGENTS.md"):
            _ = (default / name).write_text("shared")
        home = self.home / ".codex1"
        _ = (home / "config.toml").symlink_to(self.root / "missing")
        _ = (home / "hooks.json").write_text("replacement")
        wrong = self.root / "other.md"
        _ = wrong.write_text("other")
        _ = (home / "AGENTS.md").symlink_to(wrong)
        _ = (home / "unknown-entry").write_text("own")

        result, stdout, stderr = self.run_cli("check", "codex 1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        for detail in ("config.toml", "hooks.json", "AGENTS.md", "unknown-entry", "other@example.invalid", "codex 1"):
            self.assertIn(detail, stdout)
        self.assertEqual((home / "hooks.json").read_text(), "replacement")
        self.codex_reads.assert_not_awaited()

    def test_codex_check_reports_one_login_in_default_and_another_numbered_home(self) -> None:
        self.write_note("codex 1", "same@example.invalid")
        self.write_note("codex 2", "same@example.invalid")
        for name in (".codex", ".codex1", ".codex2"):
            self.codex_login("SAME@example.invalid", self.home / name)

        result, stdout, stderr = self.run_cli("check", "codex 1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("~/.codex", stdout)
        self.assertIn("~/.codex2", stdout)
        self.assertIn("same@example.invalid", stdout.lower())

    def test_codex_check_reports_missing_home_and_logged_out_home(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        result, stdout, stderr = self.run_cli("check", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("~/.codex1", stdout)
        self.assertFalse((self.home / ".codex1").exists())
        (self.home / ".codex1").mkdir(mode=0o700)

        result, stdout, stderr = self.run_cli("check", "codex 1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("not logged in", stdout.lower())

    def test_status_has_one_codex_row_per_home_then_only_unheld_missing_notes(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        for label, login in (("codex 1", "main-codex@example.invalid"), ("codex 2", "spare@example.invalid"),
                             ("codex 3", "missing@example.invalid"), ("codex 4", "unheld@example.invalid")):
            self.write_note(label, login)
        self.codex_login("MAIN-CODEX@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex2")
        self.codex_login("unknown@example.invalid", self.home / ".codex4")
        (self.home / ".codex10").mkdir()

        rows = self.status_rows()[1:]

        self.assertEqual([row[:4] for row in rows], [
            ("~/.codex", "codex 1", "MAIN-CODEX@example.invalid", "seats"),
            ("~/.codex2", "codex 2", "spare@example.invalid", "spare"),
            ("~/.codex4", "~/.codex4", "unknown@example.invalid", "spare"),
            ("~/.codex10", "~/.codex10", "not logged in", "spare"),
            ("~/.codex3", "codex 3", "not set up", "—"),
        ])

    def test_status_reports_a_not_set_up_codex_account_when_its_home_is_a_file(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        home = self.home / ".codex1"
        _ = home.write_text("obstruction")

        self.assertIn(("~/.codex1", "codex 1", "not set up", "—"),
                      [row[:4] for row in self.status_rows()])
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        problems = stdout.split("\n\n", 1)[1]
        self.assertIn("~/.codex1 is not a directory", problems)
        self.assertEqual(home.read_text(), "obstruction")
        self.codex_reads.assert_not_awaited()

    def test_status_reports_a_not_set_up_codex_account_when_its_home_is_a_dangling_link(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        home = self.home / ".codex1"
        target = self.root / "missing-home"
        home.symlink_to(target)

        self.assertIn(("~/.codex1", "codex 1", "not set up", "—"),
                      [row[:4] for row in self.status_rows()])
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        problems = stdout.split("\n\n", 1)[1]
        self.assertIn("~/.codex1 is a link", problems)
        self.assertEqual(os.readlink(home), str(target))
        self.assertFalse(target.exists())
        self.codex_reads.assert_not_awaited()

    def test_logged_out_numbered_home_names_the_account_to_sign_in(self) -> None:
        self.write_note("codex 1", "spare@example.invalid")
        (self.home / ".codex1").mkdir(mode=0o700)
        rows = self.status_rows()
        row = next(row for row in rows if row[0] == "~/.codex1")
        self.assertEqual(row[:4], ("~/.codex1", "~/.codex1", "not logged in", "spare"))
        report = self.run_cli("status")[1]
        self.assertIn("codex 1", report.split("\n\n", 1)[1])
        self.assertIn("sign in", report.split("\n\n", 1)[1].lower())
        self.assertEqual(accounts.codex_home_label(self.home / ".codex1"), "~/.codex1")

    def test_status_assigns_seats_to_the_selected_codex_home(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("codex 1", "spare@example.invalid")
        self.codex_login("default@example.invalid")
        selected = self.home / ".codex1"
        self.codex_login("spare@example.invalid", selected)
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        with patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
            rows = self.status_rows()[1:]
        self.assertEqual([row[3] for row in rows], ["spare", "seats"])
        self.assertEqual([row[0] for row in rows], ["~/.codex", "~/.codex1"])

    def test_status_reads_each_stale_codex_home_and_saves_both_readings(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("codex 2", "spare@example.invalid")
        default = self.home / ".codex"
        spare = self.home / ".codex2"
        self.codex_login("seats@example.invalid", default)
        self.codex_login("spare@example.invalid", spare)
        later = datetime.now(timezone.utc) + timedelta(days=3)
        reports = {
            default: agent_accounts.Report("Codex", email="seats@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 20, later)], credit_balance="7"),
            spare: agent_accounts.Report("Codex", email="spare@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 95, later)], credit_balance="11"),
        }
        self.codex_reads.side_effect = reports.__getitem__

        first = self.status_rows()[1:]
        second = self.status_rows()[1:]

        self.assertEqual([row[6] for row in first], ["80%", "5%"])
        self.assertEqual([row[9] for row in first], ["7", "11"])
        self.assertEqual(first, second)
        self.assertCountEqual(self.codex_reads.await_args_list, [call(default), call(spare)])
        stored = usage_store.load()
        seats_usage = stored[usage_store.key("Codex", "seats@example.invalid")]
        spare_usage = stored[usage_store.key("Codex", "spare@example.invalid")]
        if "weekly" not in seats_usage or "weekly" not in spare_usage:
            self.fail("Both signed-in homes must save their weekly readings")
        self.assertEqual(seats_usage["weekly"]["left"], 80)
        self.assertEqual(spare_usage["weekly"]["left"], 5)

    def test_status_reads_only_the_codex_home_with_an_old_reading(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("codex 1", "spare@example.invalid")
        default = self.home / ".codex"
        spare = self.home / ".codex1"
        self.codex_login("seats@example.invalid", default)
        self.codex_login("spare@example.invalid", spare)
        now = datetime.now(timezone.utc)
        later = now + timedelta(days=3)
        fresh = agent_accounts.Report("Codex", email="seats@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 20, later)], credit_balance="7")
        old = agent_accounts.Report("Codex", email="spare@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 70, later)], credit_balance="11")
        usage_store.save([old], now - timedelta(minutes=11))
        usage_store.save([fresh], now - timedelta(minutes=1))
        self.codex_reads.return_value = agent_accounts.Report("Codex", email="spare@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 95, later)], credit_balance="13")

        rows = self.status_rows()[1:]

        self.codex_reads.assert_awaited_once_with(spare)
        self.assertEqual([row[6] for row in rows], ["80%", "5%"])
        self.assertEqual([row[9] for row in rows], ["7", "13"])

    def test_status_prints_codex_check_problems_below_the_table(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_note("codex 1", "spare@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex1")
        self.assertNotIn("unclassified:", self.run_cli("status")[1])
        _ = (self.home / ".codex1" / "unclassified-item").write_text("own")

        result, stdout, stderr = self.run_cli("status")

        self.assertEqual((result, stderr), (0, ""))
        table, problems = stdout.split("\n\n", 1)
        self.assertIn("~/.codex1", table)
        self.assertNotIn("unclassified-item", table)
        self.assertIn("~/.codex1", problems)
        self.assertIn("unclassified-item", problems)

    def test_status_keeps_the_last_good_reading_when_the_service_refuses_a_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        now = datetime.now(timezone.utc)
        read = now - timedelta(minutes=45)
        usage_store.save([self.reading(
            "main@example.invalid",
            # This 5-hour window has ended since it was read.
            agent_accounts.Quota("5-hour", 80.0, now - timedelta(minutes=5)),
            agent_accounts.Quota("Weekly", 25.0, now + timedelta(days=2)),
        )], read)
        self.live[self.main_dir] = agent_accounts.Report(
            "Claude", email="main@example.invalid",
            quota_problem="Weekly quota: unavailable (HTTP 429; try Claude /usage)",
        )

        rows = self.status_rows()

        self.assertEqual(rows[0][4:7], ("unavailable", "unavailable", "75%"))
        self.assertEqual(rows[0][10], read.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M %Z"))
        # claude 3 is logged in and has never been read.
        self.assertEqual(rows[1][2:], ("unit@example.invalid", "", *("unavailable",) * 4, "—", "—", "—"))
        self.assertEqual(self.live_reads.call_count, 2)

    def test_status_times_use_the_users_zone_setting_when_no_production_runs_or_none_can_be_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        resets = datetime.now(timezone.utc) + timedelta(days=3)
        usage_store.save([self.reading(
            "main@example.invalid", agent_accounts.Quota("5-hour", 50.0, resets), agent_accounts.Quota("Weekly", 50.0, resets),
        )], datetime.now(timezone.utc))
        setting = resets.astimezone(ZoneInfo("Europe/Paris")).strftime("%a %m-%d %H:%M %Z")

        with patch.dict(os.environ, {"USER_ZONE": "Europe/Paris"}):
            with patch.object(showrunners, "registered_showrunners", return_value=[]):
                self.assertEqual(self.status_rows()[0][7], setting)
            with patch.object(showrunners, "registered_showrunners", side_effect=OSError("session records unreadable")):
                self.assertEqual(self.status_rows()[0][7], setting)
        self.assertEqual(
            self.status_rows()[0][7],
            resets.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %m-%d %H:%M %Z"),
        )

    def test_table_times_use_the_shared_user_zone_rule(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        resets = datetime.now(timezone.utc) + timedelta(days=3)
        usage_store.save([self.reading("main@example.invalid",
            agent_accounts.Quota("5-hour", 50, resets), agent_accounts.Quota("Weekly", 50, resets),
        )], datetime.now(timezone.utc))
        zone = ZoneInfo("Asia/Tokyo")
        with patch.object(agent_accounts, "user_zone", return_value=zone) as resolve:
            self.assertEqual(self.status_rows()[0][7], resets.astimezone(zone).strftime("%a %m-%d %H:%M %Z"))
        resolve.assert_called_once_with()


    def test_account_prefix_keeps_the_selection_after_the_role_changes(self) -> None:
        """Rendering a selected account never resolves the role again."""
        self.write_main_config()
        self.login_directory("claude 3", "old@example.invalid")
        self.login_directory("claude 1", "new@example.invalid")
        selected = accounts.NamedAccount("claude 3", accounts.account_dir("claude 3"))
        self.seed_room("old@example.invalid", 7)
        self.seed_room("new@example.invalid", 2)
        self.select_named_unit("claude 1")

        self.assertEqual(
            accounts.account_prefix(selected),
            named_prefix(self.home / ".claude3"),
        )
        self.assertEqual(
            accounts.launch_prefix("unit"),
            named_prefix(self.home / ".claude1"),
        )

    def test_default_account_prefix_is_empty_without_reading_role_configuration(self) -> None:
        """A default selection renders without another configuration lookup."""
        config = accounts.accounts_config_file()
        config.parent.mkdir()
        _ = config.write_text("{invalid", encoding="utf-8")

        self.assertEqual(accounts.account_prefix(accounts.DefaultAccount()), "")

    def test_paths_read_home_at_call_time_without_an_import_time_constant(self) -> None:
        later_home = self.root / "later home"
        _ = later_home.mkdir()

        with patch.dict(os.environ, {"HOME": str(later_home)}):
            self.assertEqual(accounts.accounts_home(), later_home)
            self.assertEqual(accounts.main_config_dir(), later_home / ".claude")

        self.assertFalse(hasattr(accounts, "HOME"))




    def test_unknown_role_is_refused_by_every_role_command(self) -> None:
        for arguments in (
            ("role", "unknown"),
            ("env", "unknown"),
            ("set-role", "unknown", "default"),
            ("swap", "unknown", "default"),
        ):
            with self.subTest(arguments=arguments):
                result, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    "accounts: unknown is not a role; only unit, showrunner and seats are supported\n",
                )








    def seats_fixture(self) -> tuple[Path, Path]:
        default = self.home / ".codex"
        target = self.home / ".codex1"
        self.write_note("codex 1", "target@example.invalid")
        self.write_note("codex 2", "default@example.invalid")
        self.codex_login("default@example.invalid", default)
        self.codex_login("target@example.invalid", target)
        now = datetime.now(timezone.utc)
        self.codex_reads.return_value = agent_accounts.Report("Codex", email="target@example.invalid",
            source=agent_accounts.CodexHomeSource(target), five_hour=agent_accounts.Quota("5-hour", 20, now + timedelta(hours=3)),
            quotas=[agent_accounts.Quota("Weekly", 30, now + timedelta(days=3))], credit_balance="0")
        return default, target

    def test_seats_role_defaults_independently_of_the_callers_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            self.assertEqual(self.run_cli("role", "seats"), (0, "~/.codex\n", ""))
            self.assertEqual(agent_accounts.seats_home(), self.home / ".codex")
        self.assertEqual(self.role_files(), {})

    def test_seats_environment_overrides_the_caller_for_default_and_numbered_roles(self) -> None:
        default, target = self.seats_fixture()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            for label, home in (("codex 1", target), ("default", default)):
                with self.subTest(label=label):
                    self.assertEqual(self.run_cli("set-role", "seats", label), (0, "", ""))
                    result, stdout, stderr = self.run_cli("env", "seats")
                    self.assertEqual((result, stderr), (0, ""))
                    self.assertEqual(shlex.split(stdout), [f"CODEX_HOME={home}"])

    def test_trust_command_default_and_named_accounts_quote_paths(self) -> None:
        folder = self.root / "folder ' with $spaces"
        clear_variables = " ".join(f"-u {name}" for name in account.NAMED_ACCOUNT_VARIABLES)
        self.assertEqual(accounts.trust_command(folder, accounts.DefaultAccount()),
                         f"cd {shlex.quote(str(folder))} && env {clear_variables} claude")
        config_dir = self.root / "account ' directory"
        expected = (f"cd {shlex.quote(str(folder))} && CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))} "
                    + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(self.root / '.claude'))} claude")
        with patch.object(Path, "home", return_value=self.root):
            self.assertEqual(accounts.trust_command(folder, accounts.NamedAccount("fixture", config_dir)), expected)

    def test_default_trust_command_clears_named_account_variables_only_in_child(self) -> None:
        folder = self.root / "folder ' with $spaces"
        folder.mkdir()
        fake_claude = self.root / "claude"
        _ = fake_claude.write_text(
            f"#!{sys.executable}\nimport json, os\n"
            + f"print(json.dumps({{name: os.environ.get(name) for name in {account.NAMED_ACCOUNT_VARIABLES!r}}}))\n")
        fake_claude.chmod(0o755)
        inherited = {name: "named account value" for name in account.NAMED_ACCOUNT_VARIABLES}
        with patch.dict(os.environ, {**inherited, "PATH": f"{self.root}{os.pathsep}{os.defpath}"}):
            result = subprocess.run(["/bin/sh", "-c", accounts.trust_command(folder, accounts.DefaultAccount())],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(cast(object, json.loads(result.stdout)),
                             {name: None for name in account.NAMED_ACCOUNT_VARIABLES})
            self.assertEqual({name: os.environ[name] for name in account.NAMED_ACCOUNT_VARIABLES}, inherited)

    def test_default_prefixes_keep_claude_unassigned_and_codex_explicit(self) -> None:
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "default"}))
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.seed_room("main@example.invalid")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            unit = accounts.role_account("unit")
            seats = accounts.role_account("seats")
            self.assertIsInstance(seats, accounts.CodexSeatsAccount)
            self.assertEqual((seats.home, seats.label), (self.home / ".codex", "~/.codex"))
            self.assertEqual(accounts.account_prefix(unit), "")
            self.assertEqual(shlex.split(accounts.account_prefix(seats)), [f"CODEX_HOME={self.home / '.codex'}"])
            self.assertEqual(accounts.launch_prefix("seats"), accounts.account_prefix(seats))

    def test_seats_prefix_retains_its_home_when_the_role_changes(self) -> None:
        default, target = self.seats_fixture()
        before = accounts.role_account("seats")
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        selected = accounts.role_account("seats")
        self.assertEqual(self.run_cli("set-role", "seats", "default"), (0, "", ""))
        with patch.object(agent_accounts, "seats_home", side_effect=AssertionError("A prefix must keep its selection")):
            self.assertEqual(shlex.split(accounts.account_prefix(before)), [f"CODEX_HOME={default}"])
            self.assertEqual(shlex.split(accounts.account_prefix(selected)), [f"CODEX_HOME={target}"])
        self.assertEqual((selected.home, selected.label), (target, "codex 1"))

    def test_unreadable_seats_role_becomes_an_accounts_error_at_resolution(self) -> None:
        for error in (ValueError("invalid seats account"), OSError("role file unreadable")):
            with self.subTest(error=error), patch.object(agent_accounts, "seats_home", side_effect=error):
                result, stdout, stderr = self.run_cli("env", "seats")
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn(str(error), stderr)
                with self.assertRaises(accounts.AccountsError):
                    _ = accounts.role_account("seats")

    def test_refused_seats_selection_names_the_tool_once(self) -> None:
        (self.home / ".codex1").mkdir(exist_ok=True)
        result, stdout, stderr = self.run_cli("set-role", "seats", "codex 1")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.startswith("accounts: cannot select codex 1 (~/.codex1): "), stderr)
        self.assertEqual(stderr.count("accounts: "), 1, stderr)

    def test_unrelated_io_and_value_errors_propagate_from_the_command(self) -> None:
        for error in (OSError("table write failed"), ValueError("invalid table data")):
            with self.subTest(error=error), patch.object(accounts, "status_report", side_effect=error):
                with self.assertRaises(type(error)):
                    _ = self.run_cli("status")


    def test_seats_only_configuration_preserves_claude_selection(self) -> None:
        _ = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        before = accounts.claude_account_selection()
        for label in ("codex 1", "default"):
            with self.subTest(label=label):
                self.assertEqual(self.run_cli("set-role", "seats", label), (0, "", ""))
                self.assertEqual(accounts.claude_account_selection(), before)
                self.assertFalse(accounts.multiple_accounts_configured())


    def test_failed_seats_history_write_restores_both_roles(self) -> None:
        _ = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        before = self.role_files()
        with patch.object(accounts, "_append_role_change", side_effect=OSError("disk full")):
            result, _, stderr = self.run_cli("set-role", "seats", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertIn("disk full", stderr)
        self.assertEqual(self.role_files(), before)

    def test_burn_skips_group_history_between_legacy_unit_and_seats_entries(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
            label="claude 3", account_label="claude 3")])
        with accounts.roles_history_file().open("a") as history:
            for entry in ({"at": "2026-10-09T00:00:00Z", "role": "seats", "label": "default"},
                          {"at": "2026-10-09T00:00:00Z", "group": "units", "label": "claude 1", "why": "refused"},
                          {"at": "2026-10-09T00:00:00Z", "group": "showrunners", "label": "claude 3", "why": "refused"}):
                _ = history.write(json.dumps(entry) + "\n")
        self.write_readings([accounts.QuotaReading(account="claude 3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))])
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)),
            [accounts.AccountRoleBurn("claude 3", ("unit directors",), 10, 1)])

    def test_unit_burn_ignores_seats_history_records(self) -> None:
        _ = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        with patch.object(accounts, "datetime", RoleWriteTime):
            self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3", account_label="claude 3")])
            self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
            self.assertEqual(self.run_cli("set-role", "seats", "default"), (0, "", ""))
        self.write_readings([accounts.QuotaReading(account="claude 3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))])
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)),
            [accounts.AccountRoleBurn("claude 3", ("unit directors",), 10, 1)])

    def test_seats_swap_dry_run_names_target_and_preserves_roles_and_history(self) -> None:
        _, target = self.seats_fixture()
        before = self.role_files()
        with patch.object(codex_mesh, "retire_seats_servers", return_value=["unit-one would move"]) as retire:
            result, stdout, stderr = self.run_cli("swap", "seats", "codex 1", "--dry-run")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("codex 1", stdout)
        self.assertIn("~/.codex1", stdout)
        self.assertEqual(self.role_files(), before)
        retire.assert_called_once_with(target, "codex 1", dry_run=True)

    def test_numbered_seats_target_selects_the_login_in_the_home_directory(self) -> None:
        default, _ = self.seats_fixture()
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.email = "default@example.invalid"
        report.source = agent_accounts.CodexHomeSource(default)
        before = self.role_files()
        with patch.object(codex_mesh, "retire_seats_servers", return_value=[]) as retire:
            code, stdout, stderr = self.run_cli("swap", "seats", "codex 2", "--dry-run")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("codex 2 (~/.codex)", stdout)
        self.assertEqual(self.role_files(), before)
        retire.assert_called_once_with(default, "codex 2", dry_run=True)
        self.assertEqual(self.run_cli("set-role", "seats", "codex 2"), (0, "", ""))
        self.assertEqual(agent_accounts.seats_home(), default)
        self.assertEqual(self.run_cli("role", "seats"), (0, "codex 2\n", ""))
        self.assertEqual(accounts.role_account("seats").home, default)
        self.assertEqual(json.loads(accounts.accounts_config_file().read_text())["seats"], "codex 2")

    def test_seats_swap_refuses_paid_credits_when_the_target_setting_is_off(self) -> None:
        _, target = self.seats_fixture()
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "codex1.md"
        _ = note.write_text("---\nlogin: target@example.invalid\nuse_credits: off\n---\n")
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.quotas = [agent_accounts.Quota("Weekly", 100, datetime.now(timezone.utc) + timedelta(days=1))]
        report.credit_balance = "6370"
        before = self.role_files()
        with patch.object(codex_mesh, "retire_seats_servers") as retire:
            code, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
        self.assertNotEqual(code, 0)
        self.assertIn("is used up and set not to use credits; choose another account or wait for reset", stdout + stderr)
        self.assertIn("codex 1 (~/.codex1)", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        self.assertEqual(target, self.home / ".codex1")
        retire.assert_not_called()

    def test_seats_swap_writes_role_before_retiring_servers(self) -> None:
        _, target = self.seats_fixture()
        observed: list[Path] = []
        def retire(home: Path, label: str, dry_run: bool = False) -> list[str]:
            self.assertFalse(dry_run)
            self.assertEqual((home, label), (target, "codex 1"))
            observed.append(agent_accounts.seats_home())
            return ["unit-one: codex 1 (~/.codex1); current turn finishes, next turn moves"]
        with patch.object(codex_mesh, "retire_seats_servers", side_effect=retire):
            result, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(observed, [target])
        self.assertIn("unit-one", stdout)
        self.assertEqual(self.run_cli("role", "seats"), (0, "codex 1\n", ""))

    def test_seats_swap_refuses_current_role_without_mutation(self) -> None:
        _ = self.seats_fixture()
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        before = self.role_files()
        with patch.object(codex_mesh, "retire_seats_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex 1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_seats_swap_accepts_default_real_shared_entries(self) -> None:
        default, _ = self.seats_fixture()
        _ = (default / "config.toml").write_text("own config")
        (default / "sessions").mkdir()
        for name in ("config.toml", "sessions"):
            _ = (self.home / ".codex1" / name).symlink_to(default / name)
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.email = "default@example.invalid"
        report.source = agent_accounts.CodexHomeSource(default)
        with patch.object(codex_mesh, "retire_seats_servers", return_value=[]) as retire:
            result, _, stderr = self.run_cli("swap", "seats", "default")
        self.assertEqual((result, stderr), (0, ""))
        retire.assert_called_once_with(default, "codex 2", dry_run=False)
        self.assertEqual(self.run_cli("role", "seats"), (0, "codex 2\n", ""))

    def test_seats_swap_default_refuses_unreadable_or_duplicate_login_without_mutation(self) -> None:
        default, _ = self.seats_fixture()
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        before = self.role_files()
        for condition in ("unreadable", "duplicate"):
            with self.subTest(condition=condition):
                if condition == "unreadable":
                    _ = (default / "auth.json").write_text("not json")
                else:
                    self.codex_login("default@example.invalid", default)
                    self.codex_login("default@example.invalid", self.home / ".codex3")
                with patch.object(codex_mesh, "retire_seats_servers") as retire:
                    result, stdout, stderr = self.run_cli("swap", "seats", "default")
                self.assertNotEqual(result, 0)
                self.assertIn("~/.codex", stdout + stderr)
                self.assertEqual(self.role_files(), before)
                retire.assert_not_called()

    def test_seats_swap_refuses_exhausted_unknown_and_expired_usage(self) -> None:
        _, target = self.seats_fixture()
        before = self.role_files()
        now = datetime.now(timezone.utc)
        for report in (
            agent_accounts.Report("Codex", email="target@example.invalid", five_hour=agent_accounts.Quota("5-hour", 100, now + timedelta(hours=1)),
                quotas=[agent_accounts.Quota("Weekly", 20, now + timedelta(days=1))], credit_balance="0"),
            agent_accounts.Report("Codex", email="target@example.invalid", problem="read failed"),
            agent_accounts.Report("Codex", email="target@example.invalid", five_hour=agent_accounts.Quota("5-hour", 0, now - timedelta(seconds=1)),
                quotas=[agent_accounts.Quota("Weekly", 0, now + timedelta(days=1))], credit_balance="0"),
        ):
            with self.subTest(report=report), patch.object(codex_mesh, "retire_seats_servers") as retire:
                report.source = agent_accounts.CodexHomeSource(target)
                self.codex_reads.return_value = report
                result, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
                self.assertNotEqual(result, 0)
                self.assertIn("codex 1", stdout + stderr)
                self.assertIn("~/.codex1", stdout + stderr)
                self.assertEqual(self.role_files(), before)
                retire.assert_not_called()

    def test_seats_swap_accepts_credits_when_a_window_is_used_up(self) -> None:
        _ = self.seats_fixture()
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.five_hour = agent_accounts.Quota("5-hour", 100, datetime.now(timezone.utc) + timedelta(hours=1))
        report.credit_balance = "5"
        self.assertEqual(self.run_cli("swap", "seats", "codex 1")[0], 0)

    def test_seats_swap_start_failure_names_setup_without_mutating_role_or_history(self) -> None:
        _, target = self.seats_fixture()
        before = self.role_files()
        command = '/showrunner:accounts setup "codex 1"'
        self.codex_reads.return_value = agent_accounts.Report("Codex", source=agent_accounts.CodexHomeSource(target),
            problem=f"Codex did not start at ~/.codex1; run {command} and try again")
        with patch.object(codex_mesh, "retire_seats_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertIn(command, stdout + stderr)
        self.assertIn("~/.codex1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_seats_swap_refuses_a_login_change_during_the_usage_read(self) -> None:
        _, target = self.seats_fixture()
        before = self.role_files()
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        def read(home: Path) -> agent_accounts.Report:
            self.assertEqual(home, target)
            self.codex_login("different@example.invalid", target)
            return report
        self.codex_reads.side_effect = read
        with patch.object(codex_mesh, "retire_seats_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "seats", "codex 1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex 1", stdout + stderr)
        self.assertIn("~/.codex1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_codex_log_directory_is_own_state(self) -> None:
        _, target = self.seats_fixture()
        (target / "log").mkdir()
        self.assertEqual(self.run_cli("check", "codex 1"), (0, "", ""))

    def test_codex_setup_reports_preparation_failure_without_a_login_command(self) -> None:
        self.write_note("codex 1", "target@example.invalid")
        with patch.object(agent_accounts, "prepare_codex_home",
            return_value=agent_accounts.CodexHomePreparationFailed("first start reached its limit")) as prepare:
            result, stdout, stderr = self.run_cli("setup", "codex 1")
        self.assertNotEqual(result, 0)
        prepare.assert_called_once_with(self.home / ".codex1")
        self.assertNotIn("codex login", stdout)
        self.assertIn("limit", stderr + stdout)

    def test_codex_setup_warns_that_login_without_home_changes_the_default_account(self) -> None:
        self.write_note("codex 1", "target@example.invalid")
        result, stdout, stderr = self.run_cli("setup", "codex 1")
        self.assertEqual((result, stderr), (0, ""))
        command = stdout.index("CODEX_HOME=~/.codex1 codex login")
        warning = stdout[command:].splitlines()[1:]
        self.assertTrue(warning)
        self.assertIn("~/.codex", " ".join(warning))
        self.assertIn("CODEX_HOME", " ".join(warning))

    def test_codex_setup_wait_line_reaches_a_pipe_before_initialize_answers(self) -> None:
        self.write_note("codex 1", "target@example.invalid")
        ready = self.root / "server-ready"
        release = self.root / "server-release"
        binary = self.root / "bin"
        binary.mkdir()
        server = binary / "codex"
        _ = server.write_text(f"#!{sys.executable}\n" + """import json
import os
import sys
import time
from pathlib import Path
request = json.loads(sys.stdin.readline())
Path(os.environ["TEST_SERVER_READY"]).touch()
while not Path(os.environ["TEST_SERVER_RELEASE"]).exists():
    time.sleep(0.01)
print(json.dumps({"id": request["id"], "result": {}}), flush=True)
_ = sys.stdin.read()
""")
        server.chmod(0o700)
        environment = {**os.environ, "PATH": f"{binary}{os.pathsep}{os.defpath}",
            "PYTHONDONTWRITEBYTECODE": "1", "TEST_SERVER_READY": str(ready), "TEST_SERVER_RELEASE": str(release)}
        process = subprocess.Popen([sys.executable, str(Path(accounts.__file__)), "setup", "codex 1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "The fake server must wait for its startup answer")
            self.assertIsNone(process.poll())
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            output_pipe = cast(TextIO, process.stdout)
            with selectors.DefaultSelector() as selector:
                _ = selector.register(output_pipe, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=1), "The setup wait line must be flushed to its pipe")
            line = output_pipe.readline()
            self.assertIn("initialize", line)
            self.assertIn("limit", line)
            self.assertNotIn("codex login", line)
        finally:
            release.touch()
            try:
                output, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate()
        self.assertEqual((process.returncode, errors), (0, ""))
        self.assertIn("CODEX_HOME=~/.codex1 codex login", output)

    def test_table_seats_role_ignores_conflicting_process_home(self) -> None:
        default, target = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        with patch.dict(os.environ, {"CODEX_HOME": str(default)}):
            rows = self.status_rows()[1:]
        self.assertEqual([(row[0], row[3]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "seats")])
        self.assertEqual(agent_accounts.seats_home(), target)

    def test_table_keeps_seats_selection_when_a_live_read_changes_the_role(self) -> None:
        default, target = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.assertEqual(self.run_cli("set-role", "seats", "codex 1"), (0, "", ""))
        def read(home: Path) -> agent_accounts.Report:
            if home == default:
                self.assertEqual(self.run_cli("set-role", "seats", "default"), (0, "", ""))
            return agent_accounts.Report("Codex", email="default@example.invalid" if home == default else "target@example.invalid",
                source=agent_accounts.CodexHomeSource(home))
        self.codex_reads.side_effect = read
        rows = self.status_rows()[1:]
        self.assertEqual([(row[0], row[3]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "seats")])
        self.assertEqual(agent_accounts.seats_home(), default)
        self.assertNotEqual(target, default)

    def test_numbered_home_start_failure_row_names_setup(self) -> None:
        _, target = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        problem = 'Codex did not start at ~/.codex1; run /showrunner:accounts setup "codex 1" and try again'
        startup = agent_accounts.CodexStartupFailure(target, '/showrunner:accounts setup "codex 1"')
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem=problem,
            source=agent_accounts.CodexHomeSource(target), codex_startup=startup)
        rows = self.status_rows()[1:]
        target_row = next(row for row in rows if row[0] == "~/.codex1")
        self.assertIn('/showrunner:accounts setup "codex 1"', " ".join(target_row))

    def test_table_uses_the_startup_result_command_independently_of_problem_text(self) -> None:
        _, target = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        command = '/showrunner:accounts setup "codex 1"'
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Startup request timed out",
            codex_startup=agent_accounts.CodexStartupFailure(target, command))
        row = next(row for row in self.status_rows() if row[0] == "~/.codex1")
        self.assertEqual(row[4], command)

    def test_table_does_not_treat_similar_error_text_as_a_startup_failure(self) -> None:
        _, target = self.seats_fixture()
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Codex did not start another service",
            source=agent_accounts.CodexHomeSource(target))
        row = next(row for row in self.status_rows() if row[0] == "~/.codex1")
        self.assertEqual(row[4], "unavailable")

    def test_codex_setup_rerun_finishes_preparation_without_changing_identity(self) -> None:
        self.write_note("codex 1", "target@example.invalid")
        home = self.home / ".codex1"
        self.codex_login("target@example.invalid", home)
        before = (home / "auth.json").read_bytes()
        with patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()) as prepare:
            result, stdout, stderr = self.run_cli("setup", "codex 1")
        self.assertEqual((result, stderr), (0, ""))
        _ = prepare.assert_awaited_once_with(home)
        self.assertEqual((home / "auth.json").read_bytes(), before)
        lines = stdout.splitlines()
        self.assertIn("initialize", lines[0])
        self.assertIn("limit", lines[0].lower())
        self.assertLess(stdout.index("initialize"), stdout.index("CODEX_HOME=~/.codex1 codex login"))

    def test_legacy_unit_role_file_still_resolves_seats_to_default(self) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude 3"}')
        self.assertEqual(self.run_cli("role", "seats"), (0, "~/.codex\n", ""))
        self.assertEqual(agent_accounts.seats_home(), self.home / ".codex")


@final
class PickerTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 13, 12, tzinfo=timezone.utc).astimezone(tz)


ZERO_AGE = timedelta()


class PickerTests(unittest.TestCase):
    home: Path = Path()
    now: datetime = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes"), "USER_ZONE": "UTC",
        }, clear=True)))
        (self.home / "notes").mkdir()
        (self.home / ".claude").mkdir()
        self.claude("default", "main@example.invalid")
        self.claude("claude 1", "first@example.invalid")
        self.claude("claude 3", "third@example.invalid")
        self.reading("Claude", "main@example.invalid", 50, 7)
        self.reading("Claude", "first@example.invalid", 90, 5)
        self.reading("Claude", "third@example.invalid", 3, 2)
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()))

    def claude(self, label: str, login: str) -> None:
        if label == "default":
            config = self.home / ".claude.json"
            note = "claude 2"
        else:
            self.assertEqual(accounts.setup(label), [])
            config = accounts.account_dir(label) / ".claude.json"
            note = label
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        _ = (self.home / "notes" / note_name(note)).write_text(f"---\nlogin: {login}\n---\n")

    def codex(self, label: str, login: str) -> None:
        home = self.home / (".codex" if label == "default" else "." + label.replace(" ", ""))
        note = "codex 1" if label == "default" else label
        _ = (self.home / "notes" / note_name(note)).write_text(f"---\nlogin: {login}\n---\n")
        if label != "default":
            self.assertEqual(accounts.setup(label), [])
        home.mkdir(exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        note = "codex 1" if label == "default" else label
        _ = (self.home / "notes" / note_name(note)).write_text(f"---\nlogin: {login}\n---\n")

    def reading(self, tool: str, login: str, left: float, reset_days: float, *,
                age: timedelta = ZERO_AGE, five_left: float = 50, five: bool = True,
                reset: bool = True, credits: str = "0") -> None:
        at = (self.now - age).isoformat()
        usage = usage_store.StoredUsage(weekly=usage_store.StoredWindow(left=left,
            resets=(self.now + timedelta(days=reset_days)).isoformat() if reset else "", at=at))
        if five:
            usage["five_hour"] = usage_store.StoredWindow(left=five_left,
                resets=(self.now + timedelta(hours=3)).isoformat(), at=at)
        if tool == "Codex":
            usage["credits"] = usage_store.StoredCredits(balance=credits, at=at)
        saved = usage_store.load()
        saved[usage_store.key(tool, login)] = usage
        file = usage_store.store_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

    def cli(self, *options: str, tool: str = "claude") -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with patch.object(accounts, "datetime", PickerTime), redirect_stdout(output), redirect_stderr(error):
            result = accounts.main(["pick", tool, *options])
        return result, output.getvalue(), error.getvalue()

    def assert_picked(self, label: str, *options: str, tool: str = "claude") -> None:
        self.assertEqual(self.cli(*options, tool=tool), (0, label + "\n", ""))

    def groups(self, units: str = "claude 1", showrunners: str = "claude 3") -> Path:
        path = self.home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def group_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with patch.object(accounts, "datetime", PickerTime), redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def test_placement_zero_percent_without_refusal_has_room_for_units(self) -> None:
        self.reading("Claude", "third@example.invalid", 0, 2, five_left=0)
        result = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude 3")
        self.assertIsInstance(result.reason, accounts.UnitsResetSoonest)

    def test_placement_unresolved_refusal_excludes_positive_room(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        result = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude 1")

    def test_placement_units_choose_soonest_reset_and_break_ties_by_most_left(self) -> None:
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude 3")
        self.reading("Claude", "first@example.invalid", 4, 2)
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude 1")

    def test_placement_showrunners_choose_most_weekly_left(self) -> None:
        result = accounts.placement("showrunners", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude 1")
        self.assertIsInstance(result.reason, accounts.ShowrunnersMostLeft)

    def test_placement_no_room_stays_on_stored_group_account(self) -> None:
        path = self.groups()
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        before = path.read_bytes()
        unit = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(unit.label, "claude 1")
        self.assertIsInstance(unit.reason, accounts.GroupNoRoom)
        self.assertEqual(accounts.placement("showrunners", self.now, exclude=frozenset()).label, "claude 3")
        self.assertEqual(path.read_bytes(), before)

    def test_placement_example_ranks_zero_percent_units_and_high_room_showrunners(self) -> None:
        _ = self.groups()
        self.reading("Claude", "main@example.invalid", 0, 2)
        self.reading("Claude", "third@example.invalid", 3, 4)
        self.reading("Claude", "first@example.invalid", 53, 6)
        self.assertEqual(accounts.placement("showrunners", self.now, exclude=frozenset({"claude 3"})).label, "claude 1")
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset({"claude 1"})).label, "default")
        with patch.object(accounts, "datetime", PickerTime):
            self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude 1", accounts.account_dir("claude 1")))
            self.assertEqual(accounts.role_account("showrunner"), accounts.NamedAccount("claude 3", accounts.account_dir("claude 3")))

    def test_new_launches_keep_each_stored_group_even_when_readings_rank_better_elsewhere(self) -> None:
        path = self.groups()
        before = path.read_bytes()
        with patch.object(accounts, "datetime", PickerTime):
            unit = accounts.prepared_account("unit", resume=True)
            runner = accounts.role_account("showrunner")
        self.assertIsInstance(unit, accounts.PreparedAccount)
        assert isinstance(unit, accounts.PreparedAccount)
        self.assertEqual(unit.variables, named_environment(accounts.account_dir("claude 1")))
        self.assertEqual(runner, accounts.NamedAccount("claude 3", accounts.account_dir("claude 3")))
        self.assertEqual(path.read_bytes(), before)

    def test_read_only_placement_and_status_leave_absent_group_file_absent(self) -> None:
        path = self.home / ".local/state/accounts/groups.json"
        for arguments in (("placement", "--explain"), ("status",)):
            with self.subTest(arguments=arguments):
                code, output, error = self.group_cli(*arguments)
                self.assertEqual((code, error), (0, ""))
                self.assertIn("units", output)
                self.assertIn("showrunners", output)
                self.assertIn("not configured", output)
                self.assertFalse(path.exists())
                self.assertFalse(accounts.roles_history_file().exists())

    def test_unconfigured_launch_roles_refuse_without_inference_or_group_writes(self) -> None:
        """F004: configuration alone decides a launch account."""
        with patch.object(accounts, "pick", side_effect=AssertionError("launch inferred from readings")):
            for role, group in (("unit", "units"), ("showrunner", "showrunners")):
                with self.subTest(role=role), self.assertRaises(accounts.AccountsError) as refused:
                    _ = accounts.role_account(role)
                self.assertIn(f"accounts.py move {group} <claude N>", str(refused.exception))
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_configured_launch_roles_ignore_room_and_allow_one_shared_account(self) -> None:
        """F004: even a refused configured account is returned without a picker call."""
        path = self.groups(units="claude 1", showrunners="claude 1")
        before = path.read_bytes()
        refusals.write_refusal(refusals.AccountRefusal("claude", "first@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        with patch.object(accounts, "pick", side_effect=AssertionError("launch checked room")):
            for role in ("unit", "showrunner"):
                self.assertEqual(accounts.role_account(role), accounts.NamedAccount("claude 1", accounts.account_dir("claude 1")))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())

    def test_manual_group_move_creates_only_requested_configuration_when_file_absent(self) -> None:
        """F004: an explicit move can configure one group without bootstrapping the other."""
        with patch.object(accounts, "move_sessions", return_value=()) as moved:
            code, _, error = self.group_cli("move", "units", "claude 1")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude 1", accounts.account_dir("claude 1")))
        with self.assertRaises(accounts.AccountsError) as refused:
            _ = accounts.role_account("showrunner")
        self.assertIn("accounts.py move showrunners <claude N>", str(refused.exception))
        self.assertEqual(json.loads(accounts.groups_file().read_text()), {"units": "claude 1"})
        self.assertFalse(moved.called)

    def test_status_opens_with_groups_and_role_cells_replace_next(self) -> None:
        _ = self.groups()
        code, output, error = self.group_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "units: claude 1, showrunners: claude 3")
        rows = [[cell.strip() for cell in line.strip("|").split("|")]
                for line in output.splitlines() if line.startswith("| ~/.claude")]
        self.assertIn("units", next(row[3] for row in rows if row[1] == "claude 1"))
        self.assertIn("showrunners", next(row[3] for row in rows if row[1] == "claude 3"))
        self.assertFalse(any("next" in row[3] for row in rows))

    def test_picker_without_saved_readings_keeps_unrefused_accounts_eligible(self) -> None:
        usage_store.store_file().unlink()
        code, output, error = self.cli("--explain")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "default")
        self.assertIn("no fresh readings", output)

    def test_picker_without_fresh_readings_keeps_candidate_despite_stale_zero_room(self) -> None:
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            self.reading("Claude", login, 0, 2, age=timedelta(minutes=11), five_left=0)
        self.assert_picked("claude 3", "--keep", "claude 3")
        self.assert_picked("default", "--keep", "claude 3", "--exclude", "claude 3")

    def test_picker_without_fresh_readings_skips_refused_keep(self) -> None:
        usage_store.store_file().unlink()
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("default", "--keep", "claude 3")
        _, output, _ = self.cli("--keep", "claude 3", "--explain")
        self.assertIn("refused at", output)

    def test_picker_without_fresh_readings_never_falls_back_to_refused_default(self) -> None:
        usage_store.store_file().unlink()
        refusals.write_refusal(refusals.AccountRefusal("claude", "main@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("claude 1")

    def test_picker_fresh_zero_candidate_remains_eligible_with_missing_other_readings(self) -> None:
        usage_store.store_file().unlink()
        self.reading("Claude", "third@example.invalid", 0, 2)
        self.assert_picked("default", "--keep", "default")
        self.assert_picked("claude 3")

    def test_picker_prefers_soonest_weekly_reset_over_most_room(self) -> None:
        self.assert_picked("claude 3")

    def test_picker_breaks_equal_weekly_resets_by_most_weekly_left(self) -> None:
        self.reading("Claude", "first@example.invalid", 4, 2)
        self.assert_picked("claude 1")

    def test_picker_breaks_equal_reset_and_room_by_label(self) -> None:
        self.reading("Claude", "first@example.invalid", 3, 2)
        self.assert_picked("claude 1")

    def test_picker_keeps_current_account_with_room_despite_a_sooner_reset(self) -> None:
        self.assert_picked("claude 1", "--keep", "claude 1")

    def test_picker_keeps_zero_percent_account_without_a_refusal(self) -> None:
        self.reading("Claude", "first@example.invalid", 0, 2)
        self.assert_picked("claude 1", "--keep", "claude 1")

    def test_picker_has_no_minimum_remaining_percentage(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, five_left=1)
        self.assert_picked("claude 3")

    def test_picker_excludes_each_explicit_label(self) -> None:
        self.assert_picked("default", "--exclude", "claude 3", "--exclude", "claude 1")

    def test_picker_exclude_overrides_keep(self) -> None:
        self.assert_picked("claude 1", "--keep", "claude 3", "--exclude", "claude 3")

    def test_picker_zero_five_hour_window_without_refusal_still_has_room(self) -> None:
        self.reading("Claude", "third@example.invalid", 90, 2, five_left=0)
        self.assert_picked("claude 3")

    def test_picker_accepts_weekly_room_when_claude_has_no_five_hour_window(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, five=False)
        self.assert_picked("claude 3")

    def test_picker_skips_readings_older_than_ten_minutes_with_the_cause(self) -> None:
        self.reading("Claude", "third@example.invalid", 90, 2, age=timedelta(minutes=10, microseconds=1))
        code, output, error = self.cli("--explain")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("has room", output)
        self.assertEqual(output.splitlines()[0], "claude 1")

    def test_picker_accepts_a_reading_exactly_ten_minutes_old(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, age=timedelta(minutes=10))
        self.assert_picked("claude 3")

    def test_picker_skips_an_account_with_no_saved_reading(self) -> None:
        self.claude("claude 4", "unread@example.invalid")
        _, output, _ = self.cli("--explain")
        self.assertIn("claude 4", output)
        self.assertIn("has room", output)
        self.assert_picked("claude 3")

    def test_picker_skips_a_named_directory_that_fails_check(self) -> None:
        (accounts.account_dir("claude 3") / "projects").unlink()
        self.assert_picked("claude 1")

    def test_picker_deduplicates_logins_in_favor_of_default(self) -> None:
        self.claude("claude 3", "main@example.invalid")
        self.reading("Claude", "main@example.invalid", 3, 1)
        self.assert_picked("default")

    def test_picker_deduplicates_named_logins_in_favor_of_lowest_number(self) -> None:
        self.claude("claude 10", "shared@example.invalid")
        self.claude("claude 4", "shared@example.invalid")
        self.reading("Claude", "shared@example.invalid", 3, 1)
        self.assert_picked("claude 4")

    def test_picker_reads_codex_namespace_independently_of_claude(self) -> None:
        self.codex("default", "main@example.invalid")
        self.codex("codex 2", "second@example.invalid")
        self.reading("Codex", "main@example.invalid", 0, 1)
        self.reading("Codex", "second@example.invalid", 1, 2)
        self.assert_picked("codex 2", tool="codex")

    def test_unnoted_home_pick_is_accepted_as_a_seats_swap_destination(self) -> None:
        """F002: a picker destination must be usable by its swap consumer."""
        self.codex("default", "unnamed@example.invalid")
        self.codex("codex 2", "seats@example.invalid")
        (self.home / "notes/codex1.md").unlink()
        self.assertEqual(accounts.main(["set-role", "seats", "codex 2"]), 0)
        self.reading("Codex", "unnamed@example.invalid", 80, 1)
        self.reading("Codex", "seats@example.invalid", 50, 3)
        chosen = accounts.pick("codex", self.now)
        self.assertIsInstance(chosen, accounts.PickedAccount)
        assert isinstance(chosen, accounts.PickedAccount)
        self.assertEqual((chosen.label, chosen.dir), ("default", self.home / ".codex"))
        self.assertEqual(accounts.check_codex_target(chosen.label, agent_accounts.seats_home()).home, chosen.dir)
        report = agent_accounts.Report("Codex", email="unnamed@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 20, self.now + timedelta(days=1))],
            five_hour=agent_accounts.Quota("5-hour", 50, self.now + timedelta(hours=3)))
        with (patch.object(accounts, "_codex_live", return_value=report),
              patch.object(accounts, "datetime", PickerTime),
              patch.object(accounts, "_retire_seats_servers", return_value=[]) as retire):
            result = accounts.swap("seats", chosen.label, dry_run=True)
        self.assertIsInstance(result, accounts.SeatsSwapDryRun)
        retire.assert_called_once_with(chosen.dir, "unnamed@example.invalid", dry_run=True)

    def test_codex_exclude_default_excludes_the_numbered_home_account(self) -> None:
        """F003: the legacy home alias excludes its current numbered login."""
        self.codex("default", "main@example.invalid")
        self.codex("codex 2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        self.assert_picked("codex 2", "--exclude", "default", tool="codex")

    def test_codex_keep_default_retains_the_numbered_home_account(self) -> None:
        """F003: keeping the home alias outranks the spare's earlier reset."""
        self.codex("default", "main@example.invalid")
        self.codex("codex 2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 3)
        self.reading("Codex", "spare@example.invalid", 50, 1)
        self.assert_picked("codex 1", "--keep", "default", tool="codex")
        self.assert_picked("codex 2", "--keep", "default", "--exclude", "codex 1", tool="codex")

    def test_codex_exclude_default_cannot_reselect_a_duplicate_home_login(self) -> None:
        """F002/F003: a duplicate folder cannot bypass exclusion of the canonical destination."""
        self.codex("default", "main@example.invalid")
        self.codex("codex 1", "original@example.invalid")
        _ = (self.home / ".codex1/auth.json").write_text((self.home / ".codex/auth.json").read_text())
        _ = (self.home / "notes/codex1.md").write_text("---\nlogin: main@example.invalid\n---\n")
        self.codex("codex 2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        self.assert_picked("codex 2", "--exclude", "default", tool="codex")

    def test_codex_picker_without_fresh_readings_keeps_candidate_or_uses_the_home_account(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.codex("codex 2", "other@example.invalid")
        self.assert_picked("codex 1", tool="codex")
        self.assert_picked("codex 2", "--keep", "codex 2", tool="codex")
        refusals.write_refusal(refusals.AccountRefusal("codex", "other@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("codex 1", "--keep", "codex 2", tool="codex")

    def test_picker_codex_credits_allow_work_after_both_windows_are_exhausted(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.reading("Codex", "seats@example.invalid", 0, 2, five_left=0, credits="1")
        self.assert_picked("codex 1", tool="codex")

    def test_picker_skips_paid_room_on_a_credits_off_account(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.codex("codex 2", "spare@example.invalid")
        note = self.home / "notes/codex1.md"
        _ = note.write_text("---\nlogin: seats@example.invalid\nuse_credits: off\n---\n")
        self.reading("Codex", "seats@example.invalid", 0, 1, credits="6370")
        self.reading("Codex", "spare@example.invalid", 1, 3)
        self.assert_picked("codex 2", tool="codex")

    def test_picker_codex_unlimited_credits_allow_work(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.reading("Codex", "seats@example.invalid", 0, 2, five_left=0, credits="unlimited")
        self.assert_picked("codex 1", tool="codex")

    def test_picker_codex_without_credits_requires_both_windows_with_room(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.reading("Codex", "seats@example.invalid", 80, 2, five_left=0)
        code, output, error = self.cli(tool="codex")
        self.assertEqual(code, 1)
        self.assertIn("no codex account has room", output + error)

    def test_picker_no_room_reports_earliest_known_reset(self) -> None:
        for login, days in (("main@example.invalid", 7), ("first@example.invalid", 5), ("third@example.invalid", 2)):
            self.reading("Claude", login, 0, days, five_left=0)
            refusals.write_refusal(refusals.AccountRefusal("claude", login, refusals.UnknownWindow(),
                refusals.NoResetKnown(), self.now))
        selected = accounts.pick("claude", self.now, keep=accounts.NoKeep())
        self.assertIsInstance(selected, accounts.NoAccountWithRoom)
        assert isinstance(selected, accounts.NoAccountWithRoom)
        self.assertEqual(selected.earliest_reset, accounts.EarliestReset(self.now + timedelta(hours=3)))
        self.assertEqual(len(selected.reasons), 3)
        self.assertEqual(self.cli()[0], 1)
        self.assertIn("earliest reset", "".join(self.cli()[1:]))

    def test_picker_no_room_without_any_reset_uses_no_reset_known(self) -> None:
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            self.reading("Claude", login, 0, 2, reset=False, five=False)
            refusals.write_refusal(refusals.AccountRefusal("claude", login, refusals.UnknownWindow(),
                refusals.NoResetKnown(), self.now))
        selected = accounts.pick("claude", self.now, keep=accounts.NoKeep())
        self.assertIsInstance(selected, accounts.NoAccountWithRoom)
        assert isinstance(selected, accounts.NoAccountWithRoom)
        self.assertIsInstance(selected.earliest_reset, refusals.NoResetKnown)
        self.assertEqual(len(selected.reasons), 3)
        self.assertEqual(self.cli()[0], 1)
        self.assertIn("no claude account has room", "".join(self.cli()[1:]))

    def test_picker_unresolved_refusal_beats_positive_saved_room_and_keep(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        self.assert_picked("claude 1", "--keep", "claude 3")
        code, output, error = self.cli("--explain")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("refused at", output)

    def test_picker_codex_unresolved_refusal_beats_positive_windows_and_credits(self) -> None:
        self.codex("default", "seats@example.invalid")
        self.reading("Codex", "seats@example.invalid", 90, 2, five_left=90, credits="100")
        refusals.write_refusal(refusals.AccountRefusal("codex", "seats@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        selected = accounts.pick("codex", self.now, keep=accounts.KeepAccount("default"))
        self.assertIsInstance(selected, accounts.NoAccountWithRoom)
        code, output, error = self.cli("--explain", tool="codex")
        self.assertEqual(code, 1)
        self.assertIn("refused at", output + error)

    def test_picker_accepts_account_after_its_refusal_reset_passes(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now), self.now - timedelta(days=7)))
        self.assert_picked("claude 3")

    def test_stored_reading_adapter_returns_fresh_stale_and_missing_states(self) -> None:
        self.assertIsInstance(refusals.stored_reading("claude", "third@example.invalid", self.now), accounts.FreshReading)
        self.assertIsInstance(refusals.stored_reading("claude", "unknown@example.invalid", self.now), accounts.NoReading)
        self.reading("Claude", "third@example.invalid", 1, 2, age=timedelta(minutes=11))
        self.assertIsInstance(refusals.stored_reading("claude", "third@example.invalid", self.now), accounts.StaleReading)

    def test_stored_reading_adapter_represents_missing_reset_by_name(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, reset=False, five=False)
        reading = refusals.stored_reading("claude", "third@example.invalid", self.now)
        self.assertIsInstance(reading, accounts.FreshReading)
        assert isinstance(reading, accounts.FreshReading)
        self.assertIsInstance(reading.weekly.reset, refusals.NoResetKnown)
        self.assertIsInstance(reading.five_hour, accounts.NoWindow)

    def test_status_marks_configured_groups_instead_of_picker_next(self) -> None:
        _ = self.groups(units="claude 3", showrunners="claude 1")
        with patch.object(accounts, "datetime", PickerTime):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(accounts.main(["status"]), 0)
        rows = [[cell.strip() for cell in line.strip("|").split("|")]
                for line in output.getvalue().splitlines() if line.startswith("| ~/.claude")]
        self.assertEqual([row[1] for row in rows if row[3] == "units"], ["claude 3"])
        self.assertEqual([row[1] for row in rows if row[3] == "showrunners"], ["claude 1"])

    def test_unit_role_reads_group_and_ignores_legacy_saved_unit_key(self) -> None:
        _ = self.groups(units="claude 3")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude 1"}')
        before = config.read_bytes()
        with patch.object(accounts, "datetime", PickerTime):
            selected = accounts.role_account("unit")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(accounts.main(["role", "unit"]), 0)
        self.assertEqual(selected, accounts.NamedAccount("claude 3", accounts.account_dir("claude 3")))
        self.assertEqual(output.getvalue(), "claude 3\n")
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())

    def test_env_unit_and_exec_unit_use_configured_units_account(self) -> None:
        _ = self.groups(units="claude 3")
        with patch.object(accounts, "datetime", PickerTime):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(accounts.main(["env", "unit"]), 0)
            environment = shlex.split(output.getvalue())
            self.assertIn(f"CLAUDE_CONFIG_DIR={accounts.account_dir('claude 3')}", environment)
            with patch.object(os, "execvpe") as launch:
                self.assertEqual(accounts.main(["exec", "unit", "--version"]), 0)
        self.assertEqual(launch.call_args.args[:2], ("claude", ["claude", "--version"]))
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(accounts.account_dir("claude 3")))
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_picked_account_exposes_its_directory_weekly_room_and_reset(self) -> None:
        picked = accounts.pick("claude", self.now, keep=accounts.NoKeep())
        self.assertIsInstance(picked, accounts.PickedAccount)
        assert isinstance(picked, accounts.PickedAccount)
        self.assertEqual(picked.label, "claude 3")
        self.assertEqual(picked.dir, accounts.account_dir("claude 3"))
        self.assertEqual(picked.weekly_left, 3)
        self.assertEqual(picked.weekly_reset, refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual({reason.label for reason in picked.reasons}, {"default", "claude 1", "claude 3"})
        self.assertEqual([reason.label for reason in picked.reasons if reason.status == "picked"], ["claude 3"])

    def test_retired_unit_mutations_refuse_before_disk_or_process_changes(self) -> None:
        cause = "unit launches use the configured units account; select and move the group with accounts.py move units <claude N>"
        for verb in ("set-role", "swap"):
            with self.subTest(verb=verb), patch.object(subprocess, "run") as process:
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    result = accounts.main([verb, "unit", "claude 3"])
                self.assertEqual(result, 1)
                self.assertEqual(len(error.getvalue().splitlines()), 1)
                self.assertIn(cause, error.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertFalse(accounts.accounts_config_file().exists())
                self.assertFalse(accounts.roles_history_file().exists())
                process.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
