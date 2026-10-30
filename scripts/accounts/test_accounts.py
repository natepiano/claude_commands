"""Behavioral contract for isolated Claude account directories."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami.note_file import note_name

import io
from importlib import import_module
import base64
import ctypes
import errno
import fcntl
import json
import os
import selectors
import shlex
import subprocess
import tempfile
import time
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo
from collections.abc import Callable, Generator, Iterator
from typing import Literal, Self, TextIO, cast, final, override
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

from scripts.accounts import accounts, next_moves, refusal_watch, refusals, role_map
from scripts.accounts.nix_env_fake import fake_nix_loader, imports_with_nix_loader, installed_program
from scripts.production import showrunners
from scripts.agents import codex_mesh
from scripts.message import roster
from scripts.accounts import moves, worker_names
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.whoami import usage_store
import quota_alert
from scripts.whoami import run_out
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

send_role_map_command = role_map.ssh_role_map


def stored_group_labels(path: Path) -> dict[str, str]:
    raw = cast(dict[str, object], json.loads(path.read_text()))
    return {group: value if isinstance(value, str) else cast(str, cast(dict[str, object], value)["label"])
            for group, value in raw.items()}


def named_environment(config_dir: Path) -> dict[str, str]:
    """What a session on a named account's directory starts with."""
    return {
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path.home() / ".claude"),
    }


def setUpModule() -> None:
    _ = cast(object, unittest.enterModuleContext(patch.object(accounts, "import_module", side_effect=imports_with_nix_loader(fake_nix_loader()))))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory())).resolve()
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))
    _ = unittest.enterModuleContext(patch.object(accounts, "WORKERS_RUN_ROOTS", (root / "worker-runs",)))
    _ = unittest.enterModuleContext(patch.object(role_map, "is_role_map_host", return_value=True))


def failed_read_cell(read: datetime) -> str:
    """The Read cell after a failed attempt: a read from an earlier day in the status zone also shows its date.

    A read some minutes old falls on the day before just after midnight, so a fixed form fails then.
    """
    zone = ZoneInfo("America/Los_Angeles")
    local = read.astimezone(zone)
    form = "%H:%M %Z" if local.date() == datetime.now(zone).date() else "%m-%d %H:%M %Z"
    return local.strftime(form) + "*"


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


def provider_fixture_login(_login: str) -> bool:
    """Keep provider-account fixtures distinct from test-contamination cases."""
    return False


@final
class AccountsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.main_dir = Path()
        # The live reading the status table gets for a folder; a folder without one cannot be read.
        self.live: dict[Path, agent_accounts.Report] = {}
        self.read_claude_live = agent_accounts.claude_live
        self.live_reads = MagicMock()
        self.codex_reads = AsyncMock()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home with spaces"
        _ = self.home.mkdir()
        _ = installed_program(self.home, "claude")
        _ = installed_program(self.home, "codex")
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
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.root / "quota_alert.json"))
        _ = self.enterContext(patch.object(run_out, "READINGS_LOG", self.root / "pace.jsonl"))
        self.live = {}
        self.live_reads = self.enterContext(patch.object(agent_accounts, "claude_live", side_effect=self.live_report))
        self.codex_reads = cast(AsyncMock, self.enterContext(patch.object(
            agent_accounts, "codex_live", return_value=agent_accounts.Report("Codex", problem="Not logged in"),
        )))
        _ = self.enterContext(patch.object(accounts, "WORKERS_RUN_ROOTS", (self.root / "worker-runs",)))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home",
            return_value=agent_accounts.CodexHomePrepared()))
        _ = self.enterContext(patch.object(codex_mesh, "retire_workers_servers", return_value=[]))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=[
            showrunners.Showrunner(session="fixture", socket="", slug="fixture", zone="America/Los_Angeles", doc=""),
        ]))

    def live_report(self, config_dir: Path) -> agent_accounts.Report:
        return self.live.get(config_dir, agent_accounts.Report(
            "Claude", problem="Account unavailable (claude auth status failed)",
        ))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_names_live_account_and_codex_refusal_destination_separately(self) -> None:
        from scripts.message import sessions
        self.login_directory("claude2", "main@example.invalid", default=True)
        owner = sessions.SessionRecord(pid=12345, sessionId="live-identity", name="Live director",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
        now = datetime.now(timezone.utc)
        def chosen(tool: str, _now: datetime, **_options: object) -> accounts.PickedAccount:
            return accounts.PickedAccount("claude2" if tool == "claude" else "codex3",
                self.main_dir if tool == "claude" else self.home / ".codex3", 1,
                accounts.KnownReset(now + timedelta(days=1)))
        with (patch.object(sessions, "live_sessions", return_value=[owner]),
              patch.object(import_module("sessions"), "live_sessions", return_value=[owner]),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.main_dir)),
              patch.object(accounts, "pick", side_effect=chosen),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.home / ".codex1"))):
            code, output, error = self.run_cli("--detail")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("live-identity", output)
        self.assertIn("Live director", output)
        self.assertIn("claude2", output)
        self.assertIn("Codex workers: codex1", output)
        self.assertIn("on refusal: codex3", output)
        self.assertTrue(output.splitlines()[0].startswith("| Folder"))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_shows_each_owed_continuation_and_its_hold_reason(self) -> None:
        _ = self.workers_fixture()
        run = self.root / "worker-runs/run-one"
        run.mkdir(parents=True)
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}))
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            "blocked": {"status": "starting", "continuation_owed": True,
                "continuation_reason": "destination account refused again"},
            "capacity": {"status": "waiting_capacity", "continuation_owed": True},
            "done": {"status": "idle", "continuation_owed": False},
        }))
        _, output, _ = self.run_cli("--detail")
        self.assertIn("run-one/blocked: continuation owed", output)
        self.assertIn("destination account refused again", output)
        self.assertIn("run-one/capacity: continuation owed", output)
        self.assertIn("waiting for capacity", output)
        self.assertNotIn("run-one/done", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_shows_owed_continuations_under_either_run_folder_name(self) -> None:
        _ = self.workers_fixture()
        roots = (self.root / "worker-runs", self.root / "former-worker-runs")
        for root, name in zip(roots, ("run-new", "run-old")):
            run = root / name
            run.mkdir(parents=True)
            _ = (run / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}))
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "worker": {"status": "waiting_capacity", "continuation_owed": True}}))
        with patch.object(accounts, "WORKERS_RUN_ROOTS", roots):
            _, output, _ = self.run_cli("--detail")
        self.assertIn("run-new/worker: continuation owed", output)
        self.assertIn("run-old/worker: continuation owed", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_omits_owed_continuations_from_runs_without_a_live_mesh_server(self) -> None:
        _ = self.workers_fixture()
        for name, server in (("live", {"pid": 901}), ("dead", {"pid": 902}),
                             ("missing", None), ("malformed", {"pid": "901"}),
                             ("invalid-pid", {"pid": True})):
            run = self.root / "worker-runs" / name
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
            _, output, _ = self.run_cli("--detail")
        self.assertIn("live/worker: continuation owed", output)
        for name in ("dead", "missing", "malformed", "invalid-pid"):
            self.assertNotIn(name + "/worker", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_renders_requested_pending_confirmed_per_worker_and_failed_reason(self) -> None:
        common = moves.MoveCommon(at="2026-10-14T12:00:00+00:00", tool="codex", subject="workers", move_id="move-one")
        request: moves.RequestedMove = {**common, "kind": "requested", "from": "codex1", "to": "codex3",
            "why": "weekly refusal", "window": "weekly", "reset": "2026-10-16T12:00:00+00:00"}
        moves.append_move(request)
        moves.append_move(moves.ConfirmedMove(**common, kind="confirmed", home=str(self.home / ".codex3"), worker="worker-a", run="run-one"))
        moves.append_move(moves.ConfirmedMove(**common, kind="confirmed", home=str(self.home / ".codex3"), worker="worker-b", run="run-one"))
        failed: moves.RequestedMove = {**request, "move_id": "failed-move", "subject": "claude-session: Director", "tool": "claude"}
        moves.append_move(failed)
        moves.append_move(moves.FailedMove(at=common["at"], move_id="failed-move", subject="claude-session: Director", tool="claude",
            kind="failed", reason="still on claude1"))
        moves.append_move({**request, "move_id": "pending-move", "subject": "waiting-session: Waiting"})
        _, output, _ = self.run_cli("--detail")
        self.assertIn("confirmed", output)
        self.assertIn("worker-a", output)
        self.assertIn("worker-b", output)
        self.assertIn("still on claude1", output)
        self.assertIn("pending", output)
        self.assertIn("2026-10-14 05:00", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_limits_move_readout_to_last_ten_entries(self) -> None:
        for index in range(12):
            moves.append_move(moves.FailedMove(at="2026-10-14T12:00:00+00:00", tool="claude",
                subject=f"session-{index}", move_id=f"move-{index}", kind="failed", reason=f"failure-{index}"))
        _, output, _ = self.run_cli("--detail")
        self.assertNotIn("session-0:", output)
        self.assertNotIn("session-1:", output)
        for index in range(2, 12):
            self.assertIn(f"session-{index}", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_renders_wait_end_no_room_and_room_again_in_user_zone(self) -> None:
        common = moves.MoveCommon(at="2026-10-14T12:00:00+00:00", tool="claude", subject="director: Director", move_id="wait-one")
        moves.append_move(moves.WaitedMove(**common, kind="waited", window="five_hour", reset="2026-10-14T12:19:00+00:00"))
        moves.append_move(moves.WaitEndedMove(**common, kind="wait ended"))
        room: moves.MoveCommon = {**common, "move_id": "room-one"}
        moves.append_move(moves.NoRoomMove(**room, kind="no room", earliest_reset="2026-10-15T12:00:00+00:00"))
        moves.append_move(moves.RoomAgainMove(**room, kind="room again", label="claude3"))
        _, output, _ = self.run_cli("--detail")
        self.assertIn("waited until 2026-10-14T05:19", output)
        self.assertIn("wait ended", output)
        self.assertIn("no room until 2026-10-15T05:00", output)
        self.assertIn("room again: claude3", output)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_shows_sessions_workers_and_moves_only_with_detail(self) -> None:
        _ = self.workers_fixture()
        from scripts.message import sessions
        self.login_directory("claude2", "main@example.invalid", default=True)
        owner = sessions.SessionRecord(pid=12345, sessionId="live-identity", name="Live director",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
        moves.append_move(moves.FailedMove(at="2026-10-14T12:00:00+00:00", tool="claude",
            subject="moved-session", move_id="move-one", kind="failed", reason="still on claude1"))
        with (patch.object(sessions, "live_sessions", return_value=[owner]),
              patch.object(import_module("sessions"), "live_sessions", return_value=[owner]),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.main_dir))):
            summary = self.run_cli()
            detail = self.run_cli("--detail")
        self.assertEqual((summary[0], summary[2], detail[0], detail[2]), (0, "", 0, ""))
        for line in ("session live-identity (Live director)", "Codex workers:", "moved-session: failed"):
            self.assertNotIn(line, summary[1])
            self.assertIn(line, detail[1])
        self.assertEqual(detail[1].splitlines()[:len(summary[1].splitlines())], summary[1].splitlines())
        self.assertEqual(self.run_cli("--detail", "placement"), (1, "", "--detail goes only with no action\n"))

    def status_rows(self) -> list[tuple[str, ...]]:
        """The status table's rows after its heading, each a tuple of its cells, then nothing else."""
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        table = [line for line in stdout.split("\n\n")[0].splitlines() if line.startswith("|")]
        self.assertEqual(
            [cell.strip() for cell in table[0].strip("|").split("|")],
            ["Folder", "Label", "Role", "Weekly left", "Weekly resets", "On credits", "Credits left", "Read"],
        )
        self.assertEqual(stdout.splitlines()[0], table[0])
        rows = [tuple(cell.strip() for cell in line.strip("|").split("|")) for line in table[2:]]
        for row in rows:
            self.assertEqual(len(row), len(accounts.TABLE_HEADINGS))
        return rows

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

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_notes_each_missing_group_and_uses_roles_for_configured_groups(self) -> None:
        self.login_directory("claude1", "unit@example.invalid")
        self.login_directory("claude3", "runner@example.invalid")
        for configured in ((), ("units",), ("units", "showrunners")):
            with self.subTest(configured=configured):
                for group in accounts.CLAUDE_GROUPS:
                    if group in configured:
                        _ = accounts.set_group_account(group, "claude1" if group == "units" else "claude3", why="fixture")
                code, output, error = self.run_cli("status")
                self.assertEqual((code, error), (0, ""))
                json_code, document, json_error = self.run_cli("status", "--json")
                self.assertEqual((json_code, json_error), (0, ""))
                problems = cast(list[str], json.loads(document)["problems"])
                json_rows = cast(list[dict[str, object]], json.loads(document)["accounts"])
                rows = [tuple(cell.strip() for cell in line.strip("|").split("|"))
                        for line in output.splitlines() if line.startswith("|")][2:]
                for group in accounts.CLAUDE_GROUPS:
                    note = f"{group}: not configured; the role map selects no account; run accounts.py move {group} <claudeN>"
                    missing = group not in configured
                    self.assertEqual(output.splitlines().count(note), int(missing))
                    self.assertEqual(problems.count(note), int(missing))
                    label = "claude1" if group == "units" else "claude3"
                    row = next(row for row in rows if row[1] == label)
                    json_row = next(row for row in json_rows if row["label"] == label)
                    self.assertEqual(row[2], "" if missing else group)
                    self.assertEqual(json_row["roles"], [] if missing else [group])
                if not configured:
                    self.assertFalse(accounts.groups_file().exists())

    def test_group_note_labels_map_each_configured_group_and_omit_unconfigured_groups(self) -> None:
        self.write_note("claude1", "unit@example.invalid")
        self.write_note("claude3", "runner@example.invalid")
        _ = accounts.set_group_account("units", "claude1", why="fixture", now=datetime.now(timezone.utc))
        self.assertEqual(accounts.group_note_labels(), {"units": "claude1"})
        _ = accounts.set_group_account("showrunners", "claude3", why="fixture", now=datetime.now(timezone.utc))
        self.assertEqual(accounts.group_note_labels(), {"units": "claude1", "showrunners": "claude3"})

    def test_group_note_labels_resolve_a_stored_default_from_its_actual_login(self) -> None:
        self.write_note("claude2", "home@example.invalid")
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "home@example.invalid"}}))
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"units": "default"}')
        self.assertEqual(accounts.group_note_labels(), {})
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="default", account_label="claude2")])
        self.assertEqual(accounts.group_note_labels(), {"units": "claude2"})
        _ = (self.home / ".claude.json").write_text("not json")
        self.assertEqual(accounts.group_note_labels(), {"units": "claude2"})

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
        return account.Account("claude", "owner@example.invalid", "claude2")

    def login_directory(self, label: str, login: str, *, default: bool = False, note_label: str = "") -> None:
        for name in ("projects", "sessions"):
            (self.main_dir / name).mkdir(exist_ok=True)
        if not (self.home / ".claude.json").exists():
            _ = (self.home / ".claude.json").write_text("{}")
        self.assertEqual(accounts.setup(label), [])
        config = accounts.account_dir(label) / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
        if default:
            _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
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

        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        saved = cast(dict[str, object], json.loads(configuration.read_text())) if configuration.exists() else {}
        saved["home_cutovers"] = [{"at": "2026-10-13T00:00:00Z", "label": "claude2", "login": "main@example.invalid"}]
        _ = configuration.write_text(json.dumps(saved))

    def select_named_unit(self, label: str) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps(accounts.AccountsConfiguration(unit=label)), encoding="utf-8")
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": label}))

    def test_exec_claude_uses_default_home_and_removes_inherited_account_settings(self) -> None:
        from scripts.shutdown import launch_permission

        arguments = ["--resume", "conversation id", "--", "quoted argument"]
        for tty, probe in ((False, False), (True, False), (False, True)):
            with (self.subTest(tty=tty, probe=probe),
                  patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "inherited"),
                      "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / "inherited-memory"),
                      "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token", "KEEP": "yes",
                      "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                      "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")}),
                  patch.object(accounts, "prepared_account") as prepare,
                  patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
                  patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
                  patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected subprocess")),
                  patch.object(sys.stdin, "isatty", return_value=tty),
                  patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_cli("exec", *(["--probe"] if probe else []),
                                              "claude", *arguments), (0, "", ""))
                prepare.assert_not_called()
                launch.assert_called_once()
                program, argv, environment = cast(tuple[str, list[str], dict[str, str]], launch.call_args.args)
                claude = str(self.home / ".local/bin/claude")
                if tty:
                    self.assertEqual(program, sys.executable)
                    self.assertEqual(argv, [sys.executable, str(accounts.SCRIPTS / "lib/relaunch.py"),
                                           "run", "--", claude, *arguments])
                else:
                    self.assertEqual((program, argv), (claude, [claude, *arguments]))
                self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
                self.assertNotIn("CLAUDE_CODE_REMOTE_MEMORY_DIR", environment)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
                self.assertEqual(environment["KEEP"], "yes")
                self.assertEqual(environment.get("CLAUDE_PROBE_SESSION"), "1" if probe else None)

    def test_exec_default_login_without_named_dirs_preserves_arguments_and_removes_config_override(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        before = set(self.home.iterdir())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "inherited")}), patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude2", "--version", "--", "quoted argument")
        self.assertEqual(result, (0, "", ""))
        environment = {**os.environ, **named_environment(accounts.account_dir("claude2"))}
        launch.assert_called_once_with(str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--version", "--", "quoted argument"], environment)
        self.assertEqual(set(self.home.iterdir()), before)
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_exec_probe_marks_the_launched_environment_and_consumes_its_option(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        cases = ((False, ["claude2"]), (True, ["--probe", "claude2"]))
        for probe, account_options in cases:
            with self.subTest(account_options=account_options), patch.dict(os.environ):
                _ = os.environ.pop("CLAUDE_PROBE_SESSION", None)
                os.environ.update(SHOWRUNNER_UNIT="show/demo-unit", SHOWRUNNER_UNIT_ID="id-demo")
                with patch.object(os, "execvpe") as launch:
                    self.assertEqual(self.run_cli("exec", *account_options,
                                                  "-n", "throwaway", "--version"), (0, "", ""))
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[:2], (str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "-n", "throwaway", "--version"]))
                environment = cast(dict[str, str], launch.call_args.args[2])
                if probe:
                    self.assertEqual(environment["CLAUDE_PROBE_SESSION"], "1")
                    self.assertNotIn("SHOWRUNNER_UNIT", environment)
                    self.assertNotIn("SHOWRUNNER_UNIT_ID", environment)
                else:
                    self.assertNotIn("CLAUDE_PROBE_SESSION", environment)
                    self.assertEqual(environment["SHOWRUNNER_UNIT"], "show/demo-unit")

    def test_exec_probe_preserves_claude_options_that_share_accounts_option_names(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        arguments = ["--model", "haiku", "--help", "--detail"]
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "--probe", "claude2", *arguments), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[:2], (str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), *arguments]))
        self.assertEqual(launch.call_args.args[2]["CLAUDE_PROBE_SESSION"], "1")

    def test_exec_probe_after_the_account_passes_through_to_claude_unmarked(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        with patch.dict(os.environ):
            _ = os.environ.pop("CLAUDE_PROBE_SESSION", None)
            with patch.object(os, "execvpe") as launch:
                self.assertEqual(self.run_cli("exec", "claude2", "--probe"), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[:2], (str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--probe"]))
        self.assertNotIn("CLAUDE_PROBE_SESSION", cast(dict[str, str], launch.call_args.args[2]))

    def test_legacy_exec_alias_selects_the_numbered_configuration(self) -> None:
        self.login_directory("claude1", "main@example.invalid", default=True)
        self.login_directory("claude2", "two@example.invalid")
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude 2", "--version"), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(self.home / ".claude2"))

    def test_move_units_accepts_both_spellings_and_selects_the_same_directory(self) -> None:
        self.login_directory("claude1", "main@example.invalid", default=True)
        self.login_directory("claude2", "two@example.invalid")
        self.seed_room("two@example.invalid")
        for label in ("claude2", "claude 2"):
            with self.subTest(label=label):
                code, stdout, stderr = self.run_cli("move", "units", label)
                self.assertEqual((code, stderr), (0, ""))
                self.assertNotRegex(stdout, r"(?:claude|codex) [1-9][0-9]*")
                self.assertEqual(accounts.role_account("unit"),
                                 accounts.NamedAccount("claude2", self.home / ".claude2"))

    def test_legacy_configuration_reads_are_canonical_and_do_not_write(self) -> None:
        self.login_directory("claude1", "main@example.invalid", default=True)
        self.login_directory("claude2", "two@example.invalid")
        self.write_note("codex2", "worker@example.invalid")
        self.codex_login("worker@example.invalid", self.home / ".codex2")
        groups, config, history = accounts.groups_file(), accounts.accounts_config_file(), accounts.roles_history_file()
        for path in (groups, config, history):
            path.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text('{"units": "claude 2", "showrunners": "claude 2"}\n')
        _ = config.write_text('{"workers": "codex 2"}\n')
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
            label="claude 2", account_label="claude 2")])
        before = {path: path.read_bytes() for path in (groups, config, history)}
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude2", self.home / ".claude2"))
        self.assertEqual(agent_accounts.workers_home(), self.home / ".codex2")
        self.assertIn("codex2", self.run_cli("role", "workers")[1])
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)
        _ = accounts.set_group_account("units", "claude 2", why="canonical write")
        self.assertEqual(stored_group_labels(groups), {"units": "claude2", "showrunners": "claude 2"})
        self.assertEqual(self.run_cli("set-role", "workers", "codex 2")[0], 0)
        self.assertNotIn("codex 2", config.read_text())
        self.assertEqual(history.read_bytes()[:len(before[history])], before[history])

    def test_burn_combines_legacy_and_canonical_history_without_rewriting_it(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
            label="claude 2", account_label="claude 2"),
            accounts.RoleChange(at="2026-10-09T12:00:00Z", role="unit", label="claude2", account_label="claude2")])
        self.write_readings([accounts.QuotaReading(account=label, at=at, remaining=left)
            for label, at, left in (("claude 2", "2026-10-09T00:00:00Z", 90),
                                    ("claude2", "2026-10-10T00:00:00Z", 70))])
        paths = (accounts.roles_history_file(), accounts.readings_file())
        before = [path.read_bytes() for path in paths]
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual({result.label for result in results}, {"claude2"})
        self.assertEqual(sum(result.points for result in results), 20)
        self.assertEqual(sum(result.observed_days for result in results), 1)
        self.assertTrue(any("unit directors" in result.roles for result in results))
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_burn_preserves_unit_spend_while_migrating_legacy_worker_role_history(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        _ = self.workers_fixture()
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
            label="claude2", account_label="claude2")])
        path = accounts.roles_history_file()
        with path.open("a", encoding="utf-8") as output:
            _ = output.write(json.dumps({"at": "2026-10-09T12:00:00Z", "role": "seats",
                                        "label": "codex1", "account_label": "codex1"}) + "\n")
        self.write_readings([accounts.QuotaReading(account="claude2", at=at, remaining=left)
            for at, left in (("2026-10-09T00:00:00Z", 90), ("2026-10-10T00:00:00Z", 70))])
        inode = path.stat().st_ino
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual([(result.label, result.points, result.observed_days) for result in results], [("claude2", 20, 1)])
        self.assertIn("unit directors", results[0].roles)
        self.assertEqual(json.loads(path.read_text().splitlines()[1])["role"], "workers")
        self.assertEqual(path.stat().st_ino, inode)
        unchanged = path.stat().st_mtime_ns
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)), results)
        self.assertEqual(path.stat().st_mtime_ns, unchanged)

    def test_role_history_conversion_waits_for_the_writers_lock_at_the_selected_config_path(self) -> None:
        self.write_role_history([])
        history = accounts.roles_history_file()
        original = json.dumps({"role": "seats", "label": "codex1"}) + "\n"
        _ = history.write_text(original)
        config = self.root / "selected-config" / "accounts.json"
        flock = fcntl.flock

        def acquire_without_waiting(descriptor: int, operation: int) -> None:
            flock(descriptor, operation | fcntl.LOCK_NB)

        with patch.object(worker_names, "accounts_config_file", return_value=config):
            with accounts._roles_lock():  # pyright: ignore[reportPrivateUsage]
                with patch.object(fcntl, "flock", side_effect=acquire_without_waiting):
                    with self.assertRaises(BlockingIOError):
                        _ = worker_names.role_history_lines(history)
                self.assertEqual(history.read_text(), original)
            lines = worker_names.role_history_lines(history)
        self.assertEqual(json.loads(lines[0])["role"], "workers")

    def test_exec_default_removes_inherited_config_directory_and_oauth_token(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "inherited", "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token"}),
            patch.object(os, "execvpe") as launch,
        ):
            expected = dict(os.environ)
            expected.update(named_environment(accounts.account_dir("claude2")))
            del expected["CLAUDE_CODE_OAUTH_TOKEN"]
            self.assertEqual(self.run_cli("exec", "claude2", "--resume", "same-id"), (0, "", ""))
        launch.assert_called_once_with(str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--resume", "same-id"], expected)

    def test_exec_missing_directory_prints_setup_refusal_without_launching(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude3", "--version")
        self.assertEqual(result, (2, "", 'accounts: ~/.claude3 is not set up; /showrunner:accounts setup "claude3" first\n'))
        launch.assert_not_called()
        self.assertFalse(accounts.account_dir("claude3").exists())

    def test_prepared_account_named_ready_returns_only_launch_changes_without_exec(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        with (patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": "inherited", "CLAUDE_CODE_REMOTE_MEMORY_DIR": "inherited",
                "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token", "UNCHANGED": "caller-owned",
              }), patch.object(os, "execvpe") as launch):
            before = dict(os.environ)
            result = accounts.prepared_account("claude3", resume=True)
            self.assertEqual(dict(os.environ), before)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude3")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        self.assertNotIn("UNCHANGED", result.variables)
        launch.assert_not_called()
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "variables", {})

    def test_prepared_account_does_not_substitute_the_home_for_an_unset_up_number(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("claude3", "MAIN@example.invalid")
        result = accounts.prepared_account("claude3", resume=True)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertIn("~/.claude3 is not set up", result.reason)
        self.assertFalse(accounts.account_dir("claude3").exists())

    def test_prepared_account_missing_named_directory_refuses_with_setup_instruction(self) -> None:
        self.write_note("claude3", "unit@example.invalid")
        result = accounts.prepared_account("claude3", resume=False)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason,
            'accounts: ~/.claude3 is not set up; /showrunner:accounts setup "claude3" first')
        self.assertFalse(accounts.account_dir("claude3").exists())
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "reason", "changed")

    def test_prepared_account_logged_out_named_resume_refuses(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        result = accounts.prepared_account("claude3", resume=True)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason, f"accounts: {accounts.account_dir('claude3')} is not logged in")

    def test_prepared_account_logged_out_named_login_is_allowed(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        result = accounts.prepared_account("claude3", resume=False)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude3")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_prepared_account_wrong_login_refuses_with_existing_diagnostic(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "expected@example.invalid")
        self.write_identity("claude3", "other@example.invalid")
        result = accounts.prepared_account("claude3", resume=False)
        self.assertIsInstance(result, accounts.AccountRefused)
        assert isinstance(result, accounts.AccountRefused)
        self.assertEqual(result.reason,
            "accounts: ~/.claude3 is logged in as other@example.invalid, not claude3; see Log out and in in ~/.claude/docs/accounts.md")

    def test_group_launch_prepares_configured_account_for_new_work_and_resumes(self) -> None:
        """F006: every launch checks readiness without selecting another account."""
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "expected@example.invalid")
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude3", "showrunners": "claude3"}))
        before = path.read_bytes()
        for role in ("unit", "showrunner"):
            for resume in (False, True):
                with self.subTest(role=role, resume=resume), patch.object(accounts, "prepared_account",
                        return_value=accounts.AccountRefused("configured account is not ready")) as prepare:
                    with self.assertRaises(accounts.AccountsError) as refused:
                        _ = accounts.account_for_group_launch(role, resume=resume)
                    self.assertEqual(str(refused.exception), "configured account is not ready")
                    prepare.assert_called_once_with("claude3", resume=resume)
        self.assertEqual(path.read_bytes(), before)

    def test_prepared_account_repairs_broken_folder_links_even_when_logged_out(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        (self.main_dir / "projects").rmdir()
        result = accounts.prepared_account("claude3", resume=False)
        self.assertIsInstance(result, accounts.PreparedAccount)
        self.assertTrue((self.main_dir / "projects").is_dir())
        self.assertEqual((accounts.account_dir("claude3") / "projects").resolve(), self.main_dir / "projects")

    def test_prepared_account_former_home_number_repairs_its_own_directory_on_resume(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        (accounts.account_dir("claude2") / "projects").unlink()
        result = accounts.prepared_account("claude2", resume=True)
        self.assertIsInstance(result, accounts.PreparedAccount)
        self.assertTrue((accounts.account_dir("claude2") / "projects").is_symlink())
        self.assertEqual((accounts.account_dir("claude2") / "projects").resolve(), self.main_dir / "projects")

    def test_prepared_account_default_without_resume_removes_named_variables_without_check(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        with patch.object(accounts, "check") as checked:
            result = accounts.prepared_account("claude2", resume=False)
        checked.assert_not_called()
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude2")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_prepared_account_default_resume_with_no_problems_is_allowed(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        result = accounts.prepared_account("claude2", resume=True)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude2")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_prepared_account_unit_uses_resolved_launch_account(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        for selected in (accounts.NamedAccount("claude2", accounts.account_dir("claude2")), accounts.NamedAccount("claude3", accounts.account_dir("claude3"))):
            with self.subTest(selected=selected), patch.object(accounts, "role_account", return_value=selected) as resolve:
                result = accounts.prepared_account("unit", resume=True)
                resolve.assert_called_once_with("unit")
                self.assertIsInstance(result, accounts.PreparedAccount)
                assert isinstance(result, accounts.PreparedAccount)
                self.assertEqual(result.variables, named_environment(selected.dir))
                self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_exec_named_account_sets_its_config_directory_and_preserves_claude_arguments(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "inherited", "UNCHANGED": "value"}), patch.object(os, "execvpe") as launch:
            environment = {**os.environ, **named_environment(accounts.account_dir("claude3"))}
            result = self.run_cli("exec", "claude3", "--resume", "conversation id", "-p", "a prompt")
        self.assertEqual(result, (0, "", ""))
        launch.assert_called_once_with(str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--resume", "conversation id", "-p", "a prompt"], environment)
        self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryReady)

    def test_exec_uses_one_expected_login_through_resolution_and_readiness(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
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
            result = self.run_cli("exec", "claude3")
        self.assertEqual(result, (0, "", ""))
        self.assertEqual(len(reads), 1)
        launch.assert_called_once()


    def test_exec_refuses_a_note_whose_numbered_directory_is_missing_even_when_home_holds_its_login(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("claude3", "MAIN@example.invalid")
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude3")
        self.assertEqual(result[0], 2)
        self.assertIn("not set up", result[2])
        launch.assert_not_called()

    def test_exec_discards_an_inherited_oauth_token_for_every_account_selection(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.seed_room("main@example.invalid", 7)
        self.seed_room("unit@example.invalid", 2)
        selections = (
            ("named account", "claude3", False, str(accounts.account_dir("claude3"))),
            ("numbered former home account", "claude2", False, str(accounts.account_dir("claude2"))),
            ("named unit role", "unit", True, str(accounts.account_dir("claude3"))),
            ("legacy unit key absent", "unit", False, str(accounts.account_dir("claude3"))),
        )
        for selection, label, named_unit, config_dir in selections:
            with self.subTest(selection=selection):
                if named_unit:
                    self.select_named_unit("claude3")
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
                        str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--version"], expected_environment,
                    )
                    self.assertEqual(os.environ["CLAUDE_CODE_OAUTH_TOKEN"], "another-account-token")

    def test_exec_allows_a_set_up_logged_out_directory_to_log_in_but_check_refuses_it(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.write_identity("claude3", "unit@example.invalid")
        config = accounts.account_dir("claude3") / ".claude.json"
        for content in ("{}", "missing"):
            with self.subTest(content=content):
                if content == "missing":
                    config.unlink()
                else:
                    _ = config.write_text(content)
                readiness = accounts.account_directory_readiness("claude3")
                self.assertIsInstance(readiness, accounts.AccountDirectoryLoggedOut)
                self.assertIn("not logged in", "\n".join(accounts.check("claude3")))
                with patch.object(os, "execvpe") as launch:
                    self.assertEqual(self.run_cli("exec", "claude3"), (0, "", ""))
                launch.assert_called_once_with(str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude")], {**os.environ, **named_environment(accounts.account_dir("claude3"))})

    def test_exec_allows_login_before_a_new_label_has_a_note(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude3"), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(len(accounts.check("claude3")), 2)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_exec_refuses_wrong_login_and_status_keeps_the_actual_identity(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "expected@example.invalid")
        self.write_identity("claude3", "other@example.invalid")
        line = "accounts: ~/.claude3 is logged in as other@example.invalid, not claude3; see Log out and in in ~/.claude/docs/accounts.md"
        self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryWrongLogin)
        self.assertEqual(accounts.check("claude3"), [line])
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude3"), (2, "", line + "\n"))
        launch.assert_not_called()
        result = self.run_cli("status")
        self.assertRegex(result[1], r"\| ~/\.claude3 +\| claude3 +\|")
        self.assertIn("claude3: wrong sign-in; " + line.removeprefix("accounts: "), result[1].splitlines())
        self.assertEqual(accounts.account_identity(accounts.account_dir("claude3")), agent_accounts.UnnotedLogin(self.home / ".claude3", "other@example.invalid"))

    def test_exec_repairs_broken_shared_links_before_launching_a_logged_out_account(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        (accounts.account_dir("claude3") / "projects").unlink()
        readiness = accounts.account_directory_readiness("claude3")
        self.assertIsInstance(readiness, accounts.AccountDirectoryBrokenLinks)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude3")
        self.assertEqual(result, (0, "", ""))
        launch.assert_called_once()
        self.assertTrue((self.main_dir / "projects").is_dir())
        self.assertEqual((accounts.account_dir("claude3") / "projects").resolve(), self.main_dir / "projects")
        self.assertIn("not logged in", "\n".join(readiness.problems))

    def test_removed_optional_shared_source_is_reported_by_check_and_repaired_by_exec(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        self.login_directory("claude3", "unit@example.invalid")
        source.unlink()
        linked = accounts.account_dir("claude3") / "settings.json"
        problem = f"accounts: {linked} is broken"
        self.assertEqual(accounts.check("claude3"), [problem])
        self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryBrokenLinks)
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude3"), (0, "", ""))
        launch.assert_called_once()
        self.assertFalse(os.path.lexists(linked))
        self.assertFalse(source.exists())
        self.assertEqual([selected.label for selected in accounts.checked_named_accounts()], ["claude2", "claude3"])

    def test_exec_refuses_linked_directory_and_identity_files(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        for name in accounts.IDENTITY_ENTRIES:
            with self.subTest(name=name):
                path = accounts.account_dir("claude3") / name
                original = path.read_bytes() if path.exists() else b""
                path.unlink(missing_ok=True)
                path.symlink_to(self.root / "external")
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude3")
                self.assertEqual(result[0], 2)
                self.assertIn("identity stays in each account dir", result[2])
                launch.assert_not_called()
                path.unlink()
                if original:
                    _ = path.write_bytes(original)
        linked = accounts.account_dir("claude4")
        linked.symlink_to(accounts.account_dir("claude3"), target_is_directory=True)
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude4")
        self.assertEqual(result[0], 2)
        self.assertIn("an account dir must be a real directory", result[2])
        launch.assert_not_called()

    def test_exec_refuses_malformed_existing_identity_instead_of_treating_it_as_logged_out(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        path = accounts.account_dir("claude3") / ".claude.json"
        for content in ("not JSON", "[]", '{"oauthAccount": []}', '{"oauthAccount": {"emailAddress": 1}}', '{"oauthAccount": {}}'):
            with self.subTest(content=content):
                _ = path.write_text(content)
                self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryUnreadable)
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude3")
                self.assertEqual(result[0], 2)
                self.assertIn(".claude.json", result[2])
                self.assertNotIn("not logged in", result[2])
                launch.assert_not_called()

    def test_exec_refuses_each_unreadable_identity_file(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        original_read = Path.read_text
        for name in accounts.IDENTITY_ENTRIES:
            with self.subTest(name=name):
                blocked = accounts.account_dir("claude3") / name
                _ = blocked.touch(exist_ok=True)

                def deny(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                    if path == blocked:
                        raise PermissionError("fixture read denied")
                    return original_read(path, encoding=encoding, errors=errors)

                with patch.object(Path, "read_text", deny), patch.object(os, "execvpe") as launch:
                    readiness = accounts.account_directory_readiness("claude3")
                    result = self.run_cli("exec", "claude3")
                self.assertIsInstance(readiness, accounts.AccountDirectoryUnreadable)
                self.assertEqual(result[0], 2)
                self.assertIn("fixture read denied", result[2])
                launch.assert_not_called()
                if name != ".claude.json":
                    blocked.unlink()

    def test_exec_refuses_missing_or_unreadable_account_notes_for_a_logged_in_directory(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md"
        for content in (b"missing", b"body without frontmatter", b"---\nlogin: null\n---\n", b"\xff"):
            with self.subTest(content=content):
                if content == b"missing":
                    note.unlink()
                else:
                    _ = note.write_bytes(content)
                with patch.object(os, "execvpe") as launch:
                    result = self.run_cli("exec", "claude3")
                self.assertEqual(result[0], 2)
                self.assertIn("claude3.md", result[2])
                launch.assert_not_called()

    def test_exec_refuses_an_unreadable_note_even_when_the_directory_is_logged_out(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        _ = (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").write_text("not frontmatter")
        with patch.object(os, "execvpe") as launch:
            result = self.run_cli("exec", "claude3")
        self.assertEqual(result[0], 2)
        self.assertIn("has no readable login", result[2])
        self.assertNotIn("not logged in", result[2])
        self.assertEqual(len(accounts.check("claude3")), 2)
        launch.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_reports_unreadable_notes_without_erasing_other_account_identities(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.login_directory("claude4", "spare@example.invalid")
        self.select_named_unit("claude3")
        _ = (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").write_bytes(b"\xff")
        result = self.run_cli("status")
        self.assertEqual((result[0], result[2]), (0, ""))
        self.assertIn("with login: unit@example.invalid", result[1])
        self.assertIn("cannot read", result[1])
        self.assertRegex(result[1], r"\| ~/\.claude4 +\| claude4 +\|")
        selection = accounts.claude_account_selection()
        self.assertEqual([named.identity.label for named in selection.named_accounts], ["claude2", "claude4"])
        self.assertTrue(selection.is_home_directory_login("main@example.invalid"))

    def test_exec_supervises_only_an_unsupervised_terminal(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        for tty, supervised in ((True, False), (True, True), (False, False)):
            with self.subTest(tty=tty, supervised=supervised):
                environment = {**os.environ, "PRESERVED_MARKER": "keep"}
                if supervised:
                    environment["RELAUNCH_SUPERVISED"] = "1"
                else:
                    _ = environment.pop("RELAUNCH_SUPERVISED", None)
                with (patch.dict(os.environ, environment, clear=True),
                      patch.object(sys.stdin, "isatty", return_value=tty),
                      patch.object(os, "execvpe") as launch):
                    accounts.exec_account("claude3", ["--resume", "conversation-id"])
                executable, argv, child_environment = cast(tuple[str, list[str], dict[str, str]], launch.call_args.args)
                if tty and not supervised:
                    self.assertEqual(executable, sys.executable)
                    self.assertEqual(argv, [sys.executable,
                        str(accounts.SCRIPTS / "lib" / "relaunch.py"), "run", "--",
                        str(self.home / ".local/bin/claude"), "--resume", "conversation-id"])
                else:
                    self.assertEqual((executable, argv), (str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--resume", "conversation-id"]))
                self.assertEqual(child_environment["CLAUDE_CONFIG_DIR"], str(accounts.account_dir("claude3")))
                self.assertEqual(child_environment["PRESERVED_MARKER"], "keep")
                launch.assert_called_once()

    def test_exec_reports_an_unavailable_claude_executable_with_exit_two(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        with patch.object(os, "execvpe", side_effect=FileNotFoundError("fixture executable missing")):
            result = self.run_cli("exec", "claude2")
        self.assertEqual(result, (2, "", "accounts: could not start claude: fixture executable missing\n"))

    def test_program_resolver_uses_path_before_the_installer_directory(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                program = Path(installed_program(self.root, tool))
                with patch.dict(os.environ, {"PATH": str(program.parent)}):
                    self.assertEqual(accounts.resolve_program("claude" if tool == "claude" else "codex"), str(program))

    def test_probe_exec_finds_the_installed_claude_with_a_service_path(self) -> None:
        self.login_directory("claude2", "main@example.invalid")
        program = str(self.home / ".local/bin/claude")
        with (patch.dict(os.environ, {"PATH": str(self.root / "empty-path")}),
              patch.object(os, "execvpe") as launch):
            self.assertEqual(self.run_cli("exec", "--probe", "claude2", "-p", "hello"), (0, "", ""))
        self.assertEqual(launch.call_args.args[:2], (program, [program, "-p", "hello"]))
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(accounts.account_dir("claude2")))
        self.assertEqual(launch.call_args.args[2]["CLAUDE_PROBE_SESSION"], "1")

    def test_program_resolver_rejects_missing_and_non_executable_installs(self) -> None:
        program = self.home / ".local/bin/claude"
        for mode in ("missing", "not executable", "directory"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"PATH": str(self.root / "empty-path")}):
                program.unlink(missing_ok=True)
                if mode == "not executable":
                    _ = program.write_text("fixture")
                    program.chmod(0o600)
                elif mode == "directory":
                    program.mkdir()
                with self.assertRaisesRegex(FileNotFoundError, "claude program not found ") as failure:
                    _ = accounts.resolve_program("claude")
                self.assertIn(f"looked in PATH and {program}", str(failure.exception))

    def test_login_renewal_reports_a_missing_program_without_probe_output(self) -> None:
        self.login_directory("claude2", "main@example.invalid")
        program = self.home / ".local/bin/claude"
        program.unlink()
        with (patch.dict(os.environ, {"PATH": str(self.root / "empty-path")}),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2, b"private", b"private"))):
            self.assertEqual(accounts.renew_login("claude2"), accounts.LoginRenewalFailed(
                f"claude program not found (looked in PATH and {program})"))
        self.live_reads.assert_not_called()

    def test_exec_codex_labels_use_the_home_holding_each_login_and_preserve_arguments(self) -> None:
        home, numbered = self.workers_fixture()
        arguments = ["resume", "conversation id", "--model", "model-name", "--", "quoted argument"]
        for label, directory in (("codex1", numbered), ("codex2", home)):
            with self.subTest(label=label), patch.dict(os.environ, {
                "CODEX_HOME": str(self.root / "inherited"), "PRESERVED_MARKER": "keep",
            }), patch.object(sys.stdin, "isatty", return_value=True), patch.object(os, "execvpe") as launch:
                self.assertEqual(self.run_cli("exec", label, *arguments), (0, "", ""))
                launch.assert_called_once_with(str(self.home / ".local/bin/codex"), [str(self.home / ".local/bin/codex"), *arguments],
                    {**os.environ, "CODEX_HOME": str(directory)})

    def test_exec_workers_follows_the_stored_selection_instead_of_the_callers_home(self) -> None:
        home, numbered = self.workers_fixture()
        for label, directory in (("codex1", numbered), ("codex2", home)):
            with self.subTest(label=label):
                self.assertEqual(self.run_cli("set-role", "workers", label), (0, "", ""))
                with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}), patch.object(os, "execvpe") as launch:
                    self.assertEqual(self.run_cli("exec", "workers", "--version"), (0, "", ""))
                    launch.assert_called_once_with(str(self.home / ".local/bin/codex"), [str(self.home / ".local/bin/codex"), "--version"],
                        {**os.environ, "CODEX_HOME": str(directory)})

    def test_exec_unknown_codex_account_refuses_without_starting_a_process(self) -> None:
        with patch.object(os, "execvpe") as launch:
            code, output, error = self.run_cli("exec", "codex99")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("accounts: cannot select codex99 (~/.codex99): ~/.codex99 is missing", error)
        launch.assert_not_called()

    def test_exec_unknown_codex_account_beside_a_noted_home_names_only_the_missing_home(self) -> None:
        _ = self.workers_fixture()
        with patch.object(os, "execvpe") as launch:
            code, output, error = self.run_cli("exec", "codex99")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("accounts: cannot select codex99 (~/.codex99): ~/.codex99 is missing", error)
        self.assertNotIn("@", error)
        launch.assert_not_called()

    def test_exec_signed_out_codex_home_uses_the_selection_refusal_without_exec(self) -> None:
        _, numbered = self.workers_fixture()
        (numbered / "auth.json").unlink()
        with patch.object(os, "execvpe") as launch:
            code, output, error = self.run_cli("exec", "codex1")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("accounts: cannot select codex1 (~/.codex1): ~/.codex1 is not logged in", error)
        launch.assert_not_called()

    def test_exec_codex_rejects_the_claude_probe_option(self) -> None:
        _ = self.workers_fixture()
        for label in ("codex1", "workers"):
            with self.subTest(label=label), patch.object(os, "execvpe") as launch:
                code, output, error = self.run_cli("exec", "--probe", label)
                self.assertEqual((code, output), (2, ""))
                self.assertIn("--probe", error)
                self.assertIn("Claude", error)
                launch.assert_not_called()

    def test_exec_reports_an_unavailable_codex_executable_with_exit_two(self) -> None:
        _ = self.workers_fixture()
        with patch.object(os, "execvpe", side_effect=FileNotFoundError("fixture executable missing")):
            result = self.run_cli("exec", "codex1")
        self.assertEqual(result, (2, "", "accounts: could not start codex: fixture executable missing\n"))

    def test_check_wrong_login_or_missing_note_removes_the_named_account_and_unit_role(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.select_named_unit("claude3")
        self.write_identity("claude3", "other@example.invalid")
        for missing_note in (False, True):
            with self.subTest(missing_note=missing_note):
                if missing_note:
                    (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
                self.assertTrue(accounts.check("claude3"))
                self.assertEqual([selected.label for selected in accounts.checked_named_accounts()], ["claude2"])
                selection = accounts.claude_account_selection()
                self.assertEqual([selected.label for selected in selection.named_accounts], ["claude2"])
                self.assertTrue(selection.multiple_accounts)

    def test_check_retains_link_identity_and_note_failures_in_one_readiness_value(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        (accounts.account_dir("claude3") / "projects").unlink()
        _ = (accounts.account_dir("claude3") / ".credentials.json").write_text("invalid JSON")
        self.write_identity("claude3", "other@example.invalid")
        problems = accounts.check("claude3")
        self.assertEqual(len(problems), 3)
        self.assertIn("projects", problems[0])
        self.assertIn(".credentials.json", problems[1])
        self.assertIn("logged in as other@example.invalid", problems[2])

    def test_check_missing_note_alone_excludes_an_otherwise_matching_account(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").unlink()
        self.assertEqual(accounts.check("claude3"), [f"accounts: claude3 has no account note at {Path(os.environ['AGENT_NOTES_DIR']) / 'claude3.md'}"])
        self.assertEqual([selected.label for selected in accounts.claude_account_selection().named_accounts], ["claude2"])
        self.assertTrue(accounts.multiple_accounts_configured())

    def test_check_matches_the_requested_note_case_insensitively_even_when_identity_label_differs(self) -> None:
        self.login_directory("claude2", "MAIN@example.invalid", default=True)
        self.login_directory("claude3", "main@example.invalid")
        self.assertEqual(accounts.check("claude3"), [])
        self.assertEqual(accounts.account_identity(accounts.account_dir("claude3")), agent_accounts.NumberedAccount("claude2", self.home / ".claude3", "main@example.invalid"))



    def prepare_swap(self, *, shared_default_login: bool = False) -> accounts.SwapInventory:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory(
            "claude3", "main@example.invalid" if shared_default_login else "old@example.invalid",
            note_label="claude2" if shared_default_login else "claude3",
        )
        self.login_directory("claude1", "new@example.invalid")
        self.select_named_unit("claude3")
        return accounts.SwapInventory(
            machine="natedev", login="main@example.invalid" if shared_default_login else "old@example.invalid",
            label="claude2" if shared_default_login else "claude3",
            sessions=[accounts.SwapUnitSession(kind="unit", session_id="unit-id", name="unit-one")],
            unattributed=[],
        )

    def role_files(self) -> dict[str, str]:
        return {str(path): path.read_text(encoding="utf-8")
                for path in (accounts.accounts_config_file(), accounts.roles_history_file()) if path.exists()}

    def swap_cli(
        self, inventory: object, label: str = "claude1", *, dry_run: bool = False,
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
                self.assertIsInstance(current, accounts.NamedAccount)
            else:
                self.assertEqual(current, accounts.NamedAccount(label, accounts.account_dir(label)))
            return subprocess.CompletedProcess(argv, down_code, down_stdout, down_stderr)

        with patch.object(subprocess, "run", side_effect=shutdown):
            result = self.run_cli("swap", "unit", label, *(["--dry-run"] if dry_run else []))
        return result, commands
























    def test_burn_keeps_historical_account_identity_after_logout_or_another_login(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude1", account_label="claude1")])
        self.write_readings([accounts.QuotaReading(account="claude1", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
                            for day, left in ((9, 90.0), (10, 80.0))])
        result = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertIn(accounts.AccountRoleBurn("claude1", ("unit directors",), 10, 1), result)

    def test_legacy_default_role_history_without_evidence_remains_unresolved(self) -> None:
        log = accounts.roles_history_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ = log.write_text('{"at":"2026-10-08T00:00:00Z","role":"unit","label":"default"}\n')
        before = log.read_bytes()
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc),
                                       datetime(2026, 10, 10, tzinfo=timezone.utc)), [])
        self.assertEqual(log.read_bytes(), before)

    def test_account_selection_uses_whoami_accounts_in_use_type_and_checked_directories(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        selection = accounts.claude_account_selection()
        self.assertIsInstance(selection, agent_accounts.ClaudeAccountsInUse)
        self.assertIsInstance(selection.home_directory_account, agent_accounts.NumberedAccount)
        self.assertIsInstance(selection.named_accounts[0], agent_accounts.CheckedNamedClaudeAccount)
        self.assertTrue(selection.is_home_directory_login("MAIN@example.invalid"))
        self.assertFalse(hasattr(selection, "unit_account"))
        self.assertFalse(hasattr(selection, "roles_for_login"))

    def test_unfit_named_accounts_are_excluded_from_checked_directories(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        (accounts.account_dir("claude3") / "projects").unlink()
        _ = (accounts.account_dir("claude3") / "projects").write_text("cannot replace existing shared folder")
        self.assertEqual([selected.label for selected in accounts.checked_named_accounts()], ["claude2"])
        self.assertEqual([selected.label for selected in accounts.claude_account_selection().named_accounts], ["claude2"])

    def test_burn_maps_a_unit_directory_to_its_shared_login_note_identity(self) -> None:
        """A directory label is not the quota account's note label."""
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "MAIN@example.invalid")
        self.select_named_unit("claude3")
        self.write_role_history([
            accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude3", account_label="claude2"),
        ])
        self.write_readings([
            accounts.QuotaReading(account="claude2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(results, [accounts.AccountRoleBurn("claude2", ("showrunners", "unit directors"), 10, 1)])
        accounts.roles_history_file().unlink()
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)), results)

    def test_burn_keeps_a_numbered_historical_identity_after_its_folder_disappears(self) -> None:
        self.write_role_history([accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude3")])
        self.write_readings([accounts.QuotaReading(account="claude3", at=stamp, remaining=left)
                             for stamp, left in (("2026-10-09T00:00:00Z", 90), ("2026-10-10T00:00:00Z", 80))])
        result = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(result, [accounts.AccountRoleBurn("claude3", ("unit directors",), 10, 1)])

    def test_burn_after_unit_role_retirement_labels_default_and_old_unit_by_account_only(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
                                                     label="claude3", account_label="claude3")])
        self.write_readings([accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude2", "claude3")
            for stamp, left in (("2030-01-01T00:00:00Z", 90), ("2030-01-02T00:00:00Z", 80))])
        results = accounts.burn(datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(results, [accounts.AccountRoleBurn(label, (), 10, 1) for label in ("claude2", "claude3")])
        code, output, error = self.run_cli("burn", "--since", "2030-01-01T00:00:00Z", "--until", "2030-01-02T00:00:00Z")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines(), [f"{label}: 10 points/day (10 points over 1 observed days)"
                                              for label in ("claude2", "claude3")])

    def test_burn_splits_observed_spend_at_unit_role_retirement(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
                                                     label="claude3", account_label="claude3")])
        self.write_readings([accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude2", "claude3")
            for stamp, left in (("2026-10-12T00:00:00Z", 90), ("2026-10-14T00:00:00Z", 70))])
        results = accounts.burn(datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc))
        self.assertCountEqual(results, [accounts.AccountRoleBurn("claude2", ("showrunners",), 10, 1),
                                       accounts.AccountRoleBurn("claude3", ("unit directors",), 10, 1),
                                       accounts.AccountRoleBurn("claude2", (), 10, 1),
                                       accounts.AccountRoleBurn("claude3", (), 10, 1)])

    def test_showrunner_group_move_names_every_unregistered_timer_even_without_live_members(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "other@example.invalid")
        sys.path.insert(0, str(accounts.SCRIPTS / "shutdown"))
        inventory = import_module("inventory")
        stale = [showrunners.Showrunner(session="", socket="", slug=slug, zone="America/New_York",
                                       doc=f"/w/{slug}-production.md") for slug in ("first", "second")]
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "unregistered_showrunners", return_value=stale),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude3")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        self.assertIn("showrunners: no live session moves", output)
        for runner in stale:
            line = next(line for line in output.splitlines() if line.startswith(runner["slug"] + ":"))
            self.assertIn("no session runs under the id", line)
            self.assertIn("in any Claude session on", line)
            self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", line)
            self.assertIn(f"--production {runner['doc']}", line)
        moved.assert_not_called()

    def test_showrunner_group_preview_names_unregistered_timers_without_configuring_or_requesting(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "other@example.invalid")
        sys.path.insert(0, str(accounts.SCRIPTS / "shutdown"))
        inventory = import_module("inventory")
        stale = showrunners.Showrunner(session="", socket="", slug="missing", zone="America/New_York",
                                       doc="/w/missing-production.md")
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "unregistered_showrunners", return_value=[stale]),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude3", "--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("showrunners: would select claude3", output)
        self.assertIn("showrunners: no live session moves", output)
        self.assertIn("missing: no session runs under the id", output)
        self.assertIn("in any Claude session on", output)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", output)
        self.assertIn("--production /w/missing-production.md", output)
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())
        moved.assert_not_called()

    def test_burn_after_role_retirement_without_history_never_defaults_to_showrunners(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_readings([accounts.QuotaReading(account="claude2", at=stamp, remaining=left)
                            for stamp, left in (("2030-01-01T00:00:00Z", 90), ("2030-01-02T00:00:00Z", 80))])
        self.assertEqual(accounts.burn(datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 2, tzinfo=timezone.utc)),
                         [accounts.AccountRoleBurn("claude2", (), 10, 1)])

    def test_burn_reports_each_account_with_its_historical_role(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.select_named_unit("claude3")
        self.write_role_history([accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude3")])
        self.write_readings([
            accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label, values in (("claude1", (98.0, 94.0, 90.0)),
                                  ("claude2", (80.0, 70.0, 60.0)),
                                  ("claude3", (90.0, 80.0, 70.0)))
            for stamp, left in zip(("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z"), values)
        ])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-11T00:00:00Z")
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout.splitlines(), [
            "claude1: 4 points/day (8 points over 2 observed days)",
            "claude2 (showrunners): 10 points/day (20 points over 2 observed days)",
            "claude3 (unit directors): 10 points/day (20 points over 2 observed days)",
        ])

    def test_burn_splits_a_reading_interval_at_a_role_swap(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "old-unit@example.invalid")
        self.login_directory("claude1", "new-unit@example.invalid")
        self.select_named_unit("claude1")
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude3"),
            accounts.LegacyRoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude1"),
        ])
        self.write_readings([
            accounts.QuotaReading(account=label, at=stamp, remaining=left)
            for label in ("claude1", "claude3")
            for stamp, left in (("2026-10-09T00:00:00Z", 90.0), ("2026-10-11T00:00:00Z", 70.0))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertEqual(len(results), 4)
        self.assertEqual({(result.label, result.roles) for result in results}, {
            ("claude1", ()), ("claude1", ("unit directors",)),
            ("claude3", ()), ("claude3", ("unit directors",)),
        })
        for result in results:
            self.assertEqual((result.points, result.observed_days, result.points_per_day), (10.0, 1.0, 10.0))

    def test_burn_default_assignment_uses_default_login_note_label(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.select_named_unit("claude3")
        self.codex_login("worker@example.invalid")
        self.write_note("codex2", "worker@example.invalid")
        self.write_role_history([
            accounts.LegacyRoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude3"),
            accounts.LegacyRoleChange(at="2026-10-11T00:00:00Z", role="unit", label="default"),
            accounts.LegacyRoleChange(at="2026-10-11T00:00:00Z", role="workers", label="default"),
        ])
        self.write_readings([
            accounts.QuotaReading(account="claude2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=float(left))
            for day, left in ((9, 90), (10, 80), (11, 70), (12, 60))
        ])
        original = accounts.roles_history_file().read_bytes()
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))
        by_roles = {result.roles: result for result in results}
        self.assertEqual(by_roles[("showrunners", "unit directors")].points, 20.0)
        self.assertEqual(by_roles[("showrunners",)].points, 10.0)
        self.assertEqual({result.label for result in results}, {"claude2"})
        self.assertEqual(accounts.roles_history_file().read_bytes(), original)

    def test_burn_weekly_refill_starts_a_new_observed_span(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=float(left))
            for day, left in ((9, 10), (10, 0), (11, 100), (12, 80))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].points, results[0].observed_days, results[0].points_per_day), (30.0, 2.0, 15.0))

    def test_burn_clips_observed_intervals_to_the_requested_window(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude3", at="2026-10-09T00:00:00Z", remaining=90),
            accounts.QuotaReading(account="claude3", at="2026-10-11T00:00:00Z", remaining=50),
        ])
        results = accounts.burn(datetime(2026, 10, 9, 12, tzinfo=timezone.utc), datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        self.assertEqual((results[0].points, results[0].observed_days), (20.0, 1.0))

    def test_burn_skips_invalid_readings_and_orders_observations(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude3", at="2026-10-11T00:00:00Z", remaining=50),
            accounts.QuotaReading(account="claude3", at="2026-10-09T00:00:00Z", remaining=90),
        ])
        with accounts.readings_file().open("a", encoding="utf-8") as output:
            for item in (False, {"account": "claude3", "at": "bad", "remaining": 9},
                         {"account": "claude3", "at": "2026-10-10T00:00:00Z", "remaining": True},
                         {"account": "claude3", "at": "2026-10-10T00:00:00Z", "remaining": float("nan")},
                         {"account": "claude3", "at": "2026-10-10T00:00:00", "remaining": 60},
                         {"account": "claude3", "at": "2026-10-10T00:00:00Z", "remaining": 101}):
                _ = output.write(json.dumps(item) + "\n")
            _ = output.write("not JSON\n")
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertEqual((results[0].points, results[0].points_per_day), (40.0, 20.0))

    def test_burn_needs_paired_observations_and_does_not_write_state(self) -> None:
        self.write_readings([accounts.QuotaReading(account="claude3", at="2026-10-09T00:00:00Z", remaining=90)])
        before = accounts.readings_file().read_text(encoding="utf-8")
        self.assertEqual(self.run_cli("burn", "--since", "2026-10-09T00:00:00Z"),
                         (0, "No paired quota readings in this window.\n", ""))
        self.assertEqual(accounts.readings_file().read_text(encoding="utf-8"), before)
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertFalse(accounts.accounts_config_file().exists())

    def test_burn_cli_accepts_los_angeles_wall_times(self) -> None:
        self.write_readings([
            accounts.QuotaReading(account="claude3", at="2026-10-09T07:00:00Z", remaining=90),
            accounts.QuotaReading(account="claude3", at="2026-10-10T07:00:00Z", remaining=70),
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
        self.write_role_history([accounts.LegacyRoleChange(at="bad", role="unit", label="claude3")])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-10T00:00:00Z")
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("invalid role change", stderr)

    def test_shared_entries_are_the_documented_complete_set(self) -> None:
        self.assertEqual(tuple(accounts.SHARED), EXPECTED_SHARED)

    def test_setup_creates_real_symlinks_and_seed_with_private_modes(self) -> None:
        self.write_main_config()
        _ = (self.main_dir / "settings.json").write_text("{}", encoding="utf-8")
        _ = (self.main_dir / "commands").mkdir()

        result, stdout, stderr = self.run_cli("setup", "claude2")

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
        self.assertTrue((account_dir / "skills").is_symlink())
        self.assertEqual((self.main_dir / "skills").stat().st_mode & 0o777, 0o700)

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

    def shared_repair_fixture(self, tool: str) -> tuple[Path, Path]:
        if tool == "claude":
            self.login_directory("claude3", "repair@example.invalid")
            (accounts.account_dir("claude3") / "skills").unlink()
            (self.main_dir / "skills").rmdir()
            return accounts.account_dir("claude3"), self.main_dir
        shared = self.home / ".codex"
        shared.mkdir()
        destination = self.home / ".codex3"
        self.write_note("codex3", "repair@example.invalid")
        self.codex_login("repair@example.invalid", destination)
        return destination, shared

    def assert_shared_launch_ready(self, tool: str) -> None:
        if tool == "claude":
            self.assertIsInstance(accounts.prepared_account("claude3", resume=True), accounts.PreparedAccount)
        else:
            selected = accounts.check_codex_target("codex3", self.home / ".codex")
            self.assertEqual(selected.home, self.home / ".codex3")

    def test_launch_repairs_missing_and_wrong_links_after_another_account_creates_a_shared_target(self) -> None:
        for tool, name in (("claude", "settings.json"), ("codex", "config.toml")):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                first = self.home / f".{tool}4"
                self.assertFalse((shared / "skills").exists())
                self.assertFalse(os.path.lexists(destination / "skills"))
                if tool == "claude":
                    self.login_directory("claude4", "first@example.invalid")
                else:
                    self.write_note("codex4", "first@example.invalid")
                    self.codex_login("first@example.invalid", first)
                source = shared / name
                _ = source.write_text("shared settings")
                if tool == "claude":
                    self.assertIsInstance(accounts.prepared_account("claude4", resume=True), accounts.PreparedAccount)
                else:
                    self.assertEqual(accounts.check_codex_target("codex4", shared).home, first)
                self.assertTrue((first / name).is_symlink())
                self.assertTrue((shared / "skills").is_dir())
                self.assertFalse((destination / name).exists())
                self.assert_shared_launch_ready(tool)
                self.assertEqual((destination / name).resolve(), source)
                self.assertTrue((destination / "skills").is_symlink())
                self.assertEqual((destination / "skills").resolve(), shared / "skills")
                (destination / name).unlink()
                wrong = self.root / f"wrong-{tool}"
                _ = wrong.write_text("outside data")
                (destination / name).symlink_to(wrong)
                self.assert_shared_launch_ready(tool)
                self.assertEqual((destination / name).resolve(), source)
                self.assertEqual(wrong.read_text(), "outside data")

    def test_adoption_keeps_a_shared_file_created_after_the_absence_check(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        local = destination / "settings.json"
        target = shared / "settings.json"
        _ = local.write_bytes(b"local settings\x00kept")
        concurrent = b"concurrent shared settings\x00kept"
        lexists = os.path.lexists

        def appeared(path: str | os.PathLike[str]) -> bool:
            exists = lexists(path)
            if Path(path) == target and not exists:
                _ = target.write_bytes(concurrent)
            return exists

        with patch.object(os.path, "lexists", side_effect=appeared):
            problems = accounts._repair_link(local, target)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(target.read_bytes(), concurrent)
        self.assertEqual(local.read_bytes(), b"local settings\x00kept")
        self.assertFalse(local.is_symlink())
        self.assertEqual(len(problems), 1)
        self.assertIn("resolve clashing entries settings.json", problems[0])
        self.assertIn(str(local), problems[0])

    def test_merge_keeps_a_shared_entry_created_after_the_clash_check(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        local = destination / "skills"
        target = shared / "skills"
        local.mkdir()
        target.mkdir()
        _ = (local / "new-skill").write_bytes(b"local skill\x00kept")
        concurrent = b"concurrent shared skill\x00kept"
        check = accounts._folder_merge_problem  # pyright: ignore[reportPrivateUsage]

        def appeared(path: Path, shared_target: Path) -> str:
            problem = check(path, shared_target)
            _ = (shared_target / "new-skill").write_bytes(concurrent)
            return problem

        with patch.object(accounts, "_folder_merge_problem", side_effect=appeared):
            problems = accounts._repair_link(local, target)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((target / "new-skill").read_bytes(), concurrent)
        self.assertEqual((local / "new-skill").read_bytes(), b"local skill\x00kept")
        self.assertFalse(local.is_symlink())
        self.assertEqual(len(problems), 1)
        self.assertIn("resolve clashing entries new-skill", problems[0])

    def test_atomic_shared_move_refuses_existing_files_and_empty_folders(self) -> None:
        for kind in ("file", "folder"):
            with self.subTest(kind=kind):
                local = self.root / f"local-{kind}"
                target = self.root / f"shared-{kind}"
                if kind == "file":
                    _ = local.write_bytes(b"local bytes\x00")
                    _ = target.write_bytes(b"shared bytes\x00")
                else:
                    local.mkdir()
                    _ = (local / "kept").write_bytes(b"local folder entry")
                    target.mkdir()
                before = (local.stat().st_ino, target.stat().st_ino)
                with self.assertRaises(FileExistsError):
                    accounts._move_shared_entry(local, target)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual((local.stat().st_ino, target.stat().st_ino), before)
                if kind == "file":
                    self.assertEqual(local.read_bytes(), b"local bytes\x00")
                    self.assertEqual(target.read_bytes(), b"shared bytes\x00")
                else:
                    self.assertEqual((local / "kept").read_bytes(), b"local folder entry")
                    self.assertEqual(list(target.iterdir()), [])

    def test_partial_merge_preserves_moved_entries_and_can_finish_after_the_clash_is_resolved(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        local, target = destination / "skills", shared / "skills"
        local.mkdir()
        target.mkdir()
        for name in ("a-first", "b-second"):
            _ = (local / name).write_text(name)
        move = accounts._move_shared_entry  # pyright: ignore[reportPrivateUsage]

        def appeared(source: Path, shared_target: Path) -> None:
            if source.name == "b-second":
                _ = shared_target.write_text("concurrent shared entry")
            move(source, shared_target)

        with patch.object(accounts, "_move_shared_entry", side_effect=appeared):
            problems = accounts._repair_link(local, target)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((target / "a-first").read_text(), "a-first")
        self.assertFalse((local / "a-first").exists())
        self.assertEqual((target / "b-second").read_text(), "concurrent shared entry")
        self.assertEqual((local / "b-second").read_text(), "b-second")
        self.assertEqual(len(problems), 1)
        self.assertIn("already moved a-first", problems[0])
        self.assertIn("remaining", problems[0])
        self.assertIn("b-second", problems[0])
        self.assertIn("rerun", problems[0])
        (target / "b-second").unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(accounts._repair_link(local, target), [])  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(local.is_symlink())
        self.assertEqual((target / "b-second").read_text(), "b-second")

    def test_mac_atomic_move_uses_exclusive_rename_and_preserves_the_collision(self) -> None:
        local, target = self.root / "local entry", self.root / "shared entry"
        _ = local.write_bytes(b"local bytes")
        calls: list[tuple[bytes, bytes, int]] = []

        class MacLibc:
            def renamex_np(self, source: bytes, destination: bytes, flags: int) -> int:
                calls.append((source, destination, flags))
                _ = target.write_bytes(b"concurrent shared bytes")
                return -1

        with (patch.object(sys, "platform", "darwin"),
              patch.object(ctypes, "CDLL", return_value=MacLibc()),
              patch.object(ctypes, "get_errno", return_value=errno.EEXIST)):
            with self.assertRaises(FileExistsError):
                accounts._move_shared_entry(local, target)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(calls, [(os.fsencode(local), os.fsencode(target), 4)])
        self.assertEqual(local.read_bytes(), b"local bytes")
        self.assertEqual(target.read_bytes(), b"concurrent shared bytes")

    def test_unavailable_atomic_move_keeps_the_local_entry_and_returns_a_repair_step(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        local, target = destination / "settings.json", shared / "settings.json"
        _ = local.write_bytes(b"local settings")
        with patch.object(accounts, "_move_shared_entry", side_effect=OSError(errno.ENOTSUP, "not supported")):
            problems = accounts._repair_link(local, target)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(local.read_bytes(), b"local settings")
        self.assertFalse(local.is_symlink())
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(len(problems), 1)
        self.assertIn("repair", problems[0])
        self.assertIn("rerun", problems[0])

    def test_move_and_selection_accept_empty_local_folders_but_refuse_merge_clashes(self) -> None:
        self.login_directory("claude3", "repair@example.invalid")
        destination = accounts.account_dir("claude3")
        folder = destination / "shell-snapshots"
        for operation in ("move", "selection"):
            with self.subTest(operation=operation):
                if folder.is_symlink():
                    folder.unlink()
                folder.mkdir(exist_ok=True)
                self.assertTrue(accounts.check("claude3"))
                if operation == "move":
                    selected = accounts._move_target("claude3")  # pyright: ignore[reportPrivateUsage]
                    self.assertIsInstance(selected, agent_accounts.NumberedAccount)
                    self.assertTrue(folder.is_symlink())
                else:
                    self.assertIn("claude3", [named.label for named in accounts.checked_named_accounts()])
                if folder.is_symlink():
                    folder.unlink()
                folder.mkdir(exist_ok=True)
                _ = (folder / "collision").write_text("local")
                _ = (self.main_dir / "shell-snapshots/collision").write_text("shared")
                refused = accounts._move_target("claude3")  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(refused, accounts.MoveRefused)
                assert isinstance(refused, accounts.MoveRefused)
                self.assertIn("resolve clashing entries collision", refused.reason)
                self.assertNotIn("claude3", [named.label for named in accounts.checked_named_accounts()])
                self.assertEqual((folder / "collision").read_text(), "local")
                self.assertEqual((self.main_dir / "shell-snapshots/collision").read_text(), "shared")
                (folder / "collision").unlink()
                (self.main_dir / "shell-snapshots/collision").unlink()

    def test_repair_accepts_a_concurrent_launch_that_already_linked_or_removed_the_entry(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        local = destination / "skills"
        target = shared / "skills"
        target.mkdir()
        rmdir = Path.rmdir
        symlink = Path.symlink_to
        for operation in ("rmdir", "symlink_to"):
            with self.subTest(operation=operation):
                if local.is_symlink():
                    local.unlink()
                local.mkdir()

                def won_rmdir(path: Path) -> None:
                    rmdir(path)
                    if path == local:
                        symlink(local, target, target_is_directory=True)
                        raise FileNotFoundError("another launch linked this entry")

                def won_link(path: Path, link_target: str | os.PathLike[str], target_is_directory: bool = False) -> None:
                    symlink(path, link_target, target_is_directory=target_is_directory)
                    if path == local:
                        raise FileExistsError("another launch linked this entry")

                with patch.object(Path, operation, won_rmdir if operation == "rmdir" else won_link):
                    problems = accounts._repair_shared_links(  # pyright: ignore[reportPrivateUsage]
                        destination, shared, ("skills",), frozenset({"skills"}))
                self.assertEqual(problems, [])
                self.assertTrue(local.is_symlink())
                self.assertEqual(local.resolve(), target)

        removed = destination / "settings.json"
        missing_target = shared / "settings.json"
        removed.symlink_to(missing_target)
        unlink = Path.unlink

        def won_removal(path: Path, missing_ok: bool = False) -> None:
            unlink(path, missing_ok=missing_ok)
            if path == removed:
                raise FileNotFoundError("another launch already removed the stale link")

        with patch.object(Path, "unlink", won_removal):
            problems = accounts._repair_shared_links(  # pyright: ignore[reportPrivateUsage]
                destination, shared, ("settings.json",), frozenset())
        self.assertEqual(problems, [])
        self.assertFalse(os.path.lexists(removed))
        self.assertFalse(os.path.lexists(missing_target))

    def test_codex_launch_repair_does_not_create_an_absent_shared_home(self) -> None:
        shared = self.home / ".codex"
        destination = self.home / ".codex3"
        self.write_note("codex3", "repair@example.invalid")
        self.codex_login("repair@example.invalid", destination)
        entries = {entry.name for entry in destination.iterdir()}
        self.assertFalse(os.path.lexists(shared))

        selected = accounts.check_codex_target("codex3", shared)

        self.assertEqual(selected.home, destination)
        self.assertFalse(os.path.lexists(shared))
        self.assertEqual({entry.name for entry in destination.iterdir()}, entries)

    def test_check_requires_a_declared_folder_link_only_once_its_shared_target_exists(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                self.assertFalse(os.path.lexists(destination / "skills"))
                self.assertFalse(os.path.lexists(shared / "skills"))
                self.assertEqual(accounts.check(tool + "3"), [])
                self.assertFalse(os.path.lexists(shared / "skills"))
                (shared / "skills").mkdir()
                self.assertEqual(accounts.check(tool + "3"), [f"accounts: {destination / 'skills'} is missing"])
                self.assertFalse(os.path.lexists(destination / "skills"))

    def test_check_reports_a_wrong_folder_link_while_its_shared_target_is_absent(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                elsewhere = destination.parent / f"{tool}-other-skills"
                elsewhere.mkdir()
                (destination / "skills").symlink_to(elsewhere, target_is_directory=True)
                self.assertFalse(os.path.lexists(shared / "skills"))
                self.assertEqual(accounts.check(tool + "3"), [
                    f"accounts: {destination / 'skills'} points to {elsewhere}, not {shared / 'skills'}",
                ])

    def test_launch_creates_shell_snapshots_as_a_private_shared_folder(self) -> None:
        destination, shared = self.shared_repair_fixture("claude")
        linked = destination / "shell-snapshots"
        linked.unlink(missing_ok=True)
        target = shared / "shell-snapshots"
        if target.exists():
            target.rmdir()
        previous_umask = os.umask(0)
        try:
            self.assert_shared_launch_ready("claude")
        finally:
            _ = os.umask(previous_umask)
        self.assertTrue(target.is_dir())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), target)

    def test_launch_never_creates_empty_missing_shared_files(self) -> None:
        for tool, names in (("claude", ("settings.json", "keybindings.json", "history.jsonl")),
                            ("codex", ("config.toml", "hooks.json", "models_cache.json", "session_index.jsonl"))):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                self.assert_shared_launch_ready(tool)
                for name in names:
                    self.assertFalse(os.path.lexists(shared / name), name)
                    self.assertFalse(os.path.lexists(destination / name), name)

    def test_launch_adopts_missing_shared_targets_and_reports_only_on_stderr(self) -> None:
        for tool, file_name in (("claude", "settings.json"), ("codex", "config.toml")):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                local_file = destination / file_name
                _ = local_file.write_text("adopted settings")
                local_folder = destination / "skills"
                local_folder.mkdir()
                _ = (local_folder / "local-skill").write_text("adopted skill")
                file_inode = local_file.stat().st_ino
                folder_inode = local_folder.stat().st_ino
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    self.assert_shared_launch_ready(tool)
                self.assertTrue(local_file.is_symlink())
                self.assertTrue(local_folder.is_symlink())
                self.assertEqual((shared / file_name).stat().st_ino, file_inode)
                self.assertEqual((shared / "skills").stat().st_ino, folder_inode)
                self.assertEqual((shared / file_name).read_text(), "adopted settings")
                self.assertEqual((shared / "skills/local-skill").read_text(), "adopted skill")
                self.assertEqual(output.getvalue(), "")
                lines = errors.getvalue().splitlines()
                self.assertEqual(len(lines), 2)
                for name in (file_name, "skills"):
                    self.assertTrue(any(str(destination / name) in line and str(shared / name) in line
                                        for line in lines), lines)

    def test_launch_silently_replaces_an_empty_local_folder_with_the_shared_link(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                (shared / "skills").mkdir()
                _ = (shared / "skills/kept").write_text("shared skill")
                (destination / "skills").mkdir()
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    self.assert_shared_launch_ready(tool)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "")
                self.assertTrue((destination / "skills").is_symlink())
                self.assertEqual((destination / "skills").resolve(), shared / "skills")
                self.assertEqual((shared / "skills/kept").read_text(), "shared skill")

    def test_launch_merges_disjoint_entries_preserving_inodes_and_reports_only_on_stderr(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                target = shared / "skills"
                target.mkdir()
                _ = (target / "shared-skill").write_text("shared")
                local = destination / "skills"
                local.mkdir()
                (local / "nested").mkdir()
                _ = (local / "nested/kept").write_text("nested local")
                _ = (local / "local-skill").write_text("local")
                original = {name: (local / name).stat().st_ino for name in ("nested", "local-skill")}
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    self.assert_shared_launch_ready(tool)
                self.assertTrue(local.is_symlink())
                self.assertEqual(local.resolve(), target)
                self.assertEqual({path.name for path in target.iterdir()}, {"shared-skill", "nested", "local-skill"})
                self.assertEqual((target / "shared-skill").read_text(), "shared")
                self.assertEqual((target / "nested/kept").read_text(), "nested local")
                for name, inode in original.items():
                    self.assertEqual((target / name).stat().st_ino, inode)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("merged", errors.getvalue().lower())
                self.assertIn(str(local), errors.getvalue())
                self.assertIn(str(target), errors.getvalue())

    def test_launch_keeps_both_folders_and_all_entries_when_a_merge_has_name_clashes(self) -> None:
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                target = shared / "skills"
                local = destination / "skills"
                target.mkdir()
                local.mkdir()
                for name in ("collision-a", "collision-b"):
                    _ = (target / name).write_text("shared " + name)
                    _ = (local / name).write_text("local " + name)
                _ = (local / "movable").write_text("must stay local too")
                before = {path: (path.stat().st_ino, path.read_text())
                          for folder in (local, target) for path in folder.iterdir()}
                with redirect_stdout(io.StringIO()) as output:
                    if tool == "claude":
                        refused = accounts.prepared_account("claude3", resume=True)
                        self.assertIsInstance(refused, accounts.AccountRefused)
                        assert isinstance(refused, accounts.AccountRefused)
                        reason = refused.reason
                    else:
                        with self.assertRaises(accounts.AccountsError) as refused_codex:
                            _ = accounts.check_codex_target("codex3", shared)
                        reason = str(refused_codex.exception)
                diagnostic = output.getvalue() + reason
                self.assertIn("collision-a", diagnostic)
                self.assertIn("collision-b", diagnostic)
                self.assertIn(str(local), diagnostic)
                self.assertIn(str(target), diagnostic)
                self.assertRegex(diagnostic.lower(), r"move|merge|resolve")
                self.assertNotIn("is a real file", diagnostic)
                self.assertFalse(local.is_symlink())
                after = {path: (path.stat().st_ino, path.read_text())
                         for folder in (local, target) for path in folder.iterdir()}
                self.assertEqual(after, before)

    def test_launch_refuses_a_real_file_when_the_shared_target_already_exists(self) -> None:
        for tool, name in (("claude", "settings.json"), ("codex", "config.toml")):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                _ = (shared / name).write_text("shared settings")
                _ = (destination / name).write_text("local settings")
                if tool == "claude":
                    result = accounts.prepared_account("claude3", resume=True)
                    self.assertIsInstance(result, accounts.AccountRefused)
                    assert isinstance(result, accounts.AccountRefused)
                    reason = result.reason
                else:
                    with self.assertRaises(accounts.AccountsError) as refused:
                        _ = accounts.check_codex_target("codex3", shared)
                    reason = str(refused.exception)
                self.assertIn(f"{destination / name} is a real file;", reason)
                self.assertEqual((shared / name).read_text(), "shared settings")
                self.assertEqual((destination / name).read_text(), "local settings")
                self.assertFalse((destination / name).is_symlink())

    def test_setup_creates_missing_required_shared_targets_before_linking(self) -> None:
        self.write_main_config()
        for name in accounts.ALWAYS_LINKED:
            self.assertFalse((self.main_dir / name).exists())
        self.assertEqual(accounts.setup("claude3"), [])
        for name in accounts.ALWAYS_LINKED:
            target = self.main_dir / name
            linked = accounts.account_dir("claude3") / name
            self.assertTrue(target.is_dir())
            self.assertTrue(linked.is_symlink())
            self.assertTrue(linked.is_dir())
            self.assertEqual(linked.resolve(), target)
        readiness = accounts.account_directory_readiness("claude3")
        self.assertIsInstance(readiness, accounts.AccountDirectoryLoggedOut)
        self.assertFalse(any("broken" in problem for problem in readiness.problems))

    def test_setup_removes_an_obsolete_expected_shared_link_and_restores_readiness(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        self.login_directory("claude3", "unit@example.invalid")
        source.unlink()
        linked = accounts.account_dir("claude3") / "settings.json"
        self.assertTrue(linked.is_symlink())
        self.assertEqual(accounts.setup("claude3"), [])
        self.assertFalse(os.path.lexists(linked))
        self.assertFalse(source.exists())
        self.assertEqual(accounts.check("claude3"), [])
        self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryReady)

    def test_missing_optional_sources_keep_wrong_links_but_adopt_real_entries_on_setup(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        destination = accounts.account_dir("claude3")
        outside = self.root / "outside-settings"
        _ = outside.write_text("outside", encoding="utf-8")
        linked = destination / "settings.json"
        linked.symlink_to(outside)
        local = destination / "CLAUDE.md"
        _ = local.write_text("private content", encoding="utf-8")
        self.assertEqual(accounts.check("claude3"), [
            f"accounts: {linked} points to {outside}, not {self.main_dir / 'settings.json'}",
            f"accounts: {local} is a real file; move it aside or merge it into ~/.claude",
        ])
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            _ = accounts.setup("claude3")
        self.assertEqual(os.readlink(linked), str(outside))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        self.assertEqual(local.read_text(encoding="utf-8"), "private content")
        self.assertTrue(local.is_symlink())
        self.assertEqual(local.resolve(), self.main_dir / "CLAUDE.md")
        self.assertEqual(output.getvalue(), "")
        self.assertIn(str(local), errors.getvalue())

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

        result, _, _ = self.run_cli("setup", "claude2")

        self.assertEqual(result, 0)
        self.assertEqual(link.lstat().st_ino, original_inode)
        self.assertEqual(seed.read_text(encoding="utf-8"), '{"keep": true}')

    def test_setup_rerun_finishes_an_interrupted_seed_without_copying_identity(self) -> None:
        class Killed(BaseException):
            pass

        _ = (self.home / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
            "projects": {"/fixture": {"hasTrustDialogAccepted": True, "lastSessionId": "private"}},
        }))
        expected = {"hasCompletedOnboarding": True,
                    "projects": {"/fixture": {"hasTrustDialogAccepted": True}}}
        destination = self.home / ".claude2" / ".claude.json"
        with patch("scripts.lib.atomic_file.os.fsync", side_effect=Killed):
            with self.assertRaises(Killed):
                _ = self.run_cli("setup", "claude2")
        dead_pid = 2**22 + 1  # above every system's process id ceiling
        abandoned = destination.with_name(f".{destination.name}.{dead_pid}.abandoned.tmp")
        _ = abandoned.write_text('{"hasCompletedOnboarding":')
        abandoned.chmod(0o600)
        self.assertTrue(abandoned.exists())

        code, _, error = self.run_cli("setup", "claude2")

        self.assertEqual((code, error), (0, ""))
        self.assertFalse(abandoned.exists())
        self.assertEqual(json.loads(destination.read_text()), expected)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path for path in destination.parent.iterdir()
                          if path.is_file() and not path.is_symlink()], [destination])
        _ = destination.write_bytes(b"")
        self.assertEqual(self.run_cli("setup", "claude2")[0], 0)
        self.assertEqual(json.loads(destination.read_text()), expected)
        original = '{"oauthAccount": {"emailAddress": "keep@example.invalid"}}'
        _ = destination.write_text(original)
        self.assertEqual(self.run_cli("setup", "claude2")[0], 0)
        self.assertEqual(destination.read_text(), original)
        _ = abandoned.write_text("{}")
        self.assertEqual(self.run_cli("setup", "claude2")[0], 0)
        self.assertFalse(abandoned.exists())
        self.assertEqual(destination.read_text(), original)

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

        result, _, stderr = self.run_cli("setup", "claude2")

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
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        link = account_dir / "settings.json"

        self.assertEqual(repair_link(link, source), [])
        self.assertEqual(repair_link(link, source), [])
        self.write_main_config()
        self.assertEqual(seed_config(account_dir / ".claude.json"), [])

    def test_setup_replaces_a_wrong_target_link(self) -> None:
        self.write_main_config()
        source = self.main_dir / "settings.json"
        _ = source.write_text("{}", encoding="utf-8")
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        wrong = self.root / "wrong"
        _ = (account_dir / "settings.json").symlink_to(wrong)

        result, _, _ = self.run_cli("setup", "claude2")

        self.assertEqual(result, 0)
        self.assertEqual(os.readlink(account_dir / "settings.json"), str(source))

    def test_setup_refuses_a_real_file_but_repairs_an_empty_folder_and_finishes_the_rest(self) -> None:
        self.write_main_config()
        _ = (self.main_dir / "settings.json").write_text("{}", encoding="utf-8")
        _ = (self.main_dir / "commands").mkdir()
        account_dir = self.home / ".claude2"
        _ = account_dir.mkdir()
        _ = (account_dir / "settings.json").write_text("mine", encoding="utf-8")
        _ = (account_dir / "commands").mkdir()

        result, stdout, stderr = self.run_cli("setup", "claude2")

        self.assertEqual(result, 1)
        self.assertEqual(
            stdout,
            f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(account_dir))} claude\n/login\n",
        )
        self.assertEqual(
            stderr,
            f"accounts: {account_dir}/settings.json is a real file; move it aside or merge it into ~/.claude\n",
        )
        self.assertTrue((account_dir / "commands").is_symlink())
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

        setup_result, _, setup_stderr = self.run_cli("setup", "claude2")
        with patch.object(account, "claude_account") as login_read:
            check_result, check_stdout, _ = self.run_cli("check", "claude2")

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

        setup_result, _, setup_stderr = self.run_cli("setup", "claude2")

        account_dir = self.home / ".claude2"
        linked = account_dir / "settings.json"
        self.assertEqual(setup_result, 0)
        self.assertEqual(setup_stderr, "")
        self.assertTrue(linked.is_symlink())
        self.assertEqual(os.readlink(linked), str(source))
        self.write_note("claude2", "owner@example.invalid")
        self.write_identity("claude2", "owner@example.invalid")
        problems = accounts.check("claude2")
        self.assertIn(f"accounts: {linked} is broken", problems)

    def test_invalid_label_is_refused_without_creating_a_directory(self) -> None:
        before = set(self.home.iterdir())
        for command in ("setup", "check"):
            with self.subTest(command=command):
                result, stdout, stderr = self.run_cli(command, "claude0")
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                expected = ("accounts: claude0 is not a claudeN label\n" if command == "setup" else
                            "accounts: 'claude0' is not a numbered claude account; use claudeN\n")
                self.assertEqual(stderr, expected)
                self.assertEqual(set(self.home.iterdir()), before)

    def test_check_is_silent_for_a_complete_logged_in_account(self) -> None:
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        self.write_identity("claude2", "owner@example.invalid")
        self.write_note("claude2", "owner@example.invalid")
        result, stdout, stderr = self.run_cli("check", "claude2")

        self.assertEqual((result, stdout, stderr), (0, "", ""))

    def test_check_reports_missing_directory_without_reading_login(self) -> None:
        with patch.object(account, "claude_account") as login_read:
            result, stdout, stderr = self.run_cli("check", "claude2")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, f"accounts: {self.home / '.claude2'} is missing\n")
        self.assertEqual(stderr, "")
        login_read.assert_not_called()

    def test_check_reports_every_link_problem_and_not_logged_in(self) -> None:
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        self.write_note("claude2", "owner@example.invalid")
        _ = (account_dir / "settings.json").unlink()
        _ = (account_dir / "CLAUDE.md").unlink()
        _ = (account_dir / "CLAUDE.md").symlink_to(self.root / "wrong")
        _ = (account_dir / "keybindings.json").unlink()
        _ = (account_dir / "keybindings.json").write_text("real", encoding="utf-8")

        with patch.object(account, "claude_account", return_value=None):
            result, stdout, stderr = self.run_cli("check", "claude2")

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
        self.write_note("claude2", "owner@example.invalid")
        _ = (account_dir / ".claude.json").symlink_to(self.home / ".claude.json")

        with patch.object(account, "claude_account") as login_read:
            result, stdout, stderr = self.run_cli("check", "claude2")

        self.assertEqual(result, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"accounts: {account_dir / '.claude.json'} is a link; "
            + "identity stays in each account dir\n",
        )
        login_read.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_describes_accounts_without_creating_any(self) -> None:
        with patch.object(sys, "platform", "linux"):
            self.assert_status_describes_accounts_without_creating_any("run accounts.py cutover-home")

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_mac_status_describes_accounts_without_creating_any(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            self.assert_status_describes_accounts_without_creating_any(accounts.MAC_HOME_LOGIN_STEP)

    def assert_status_describes_accounts_without_creating_any(self, home_step: str) -> None:
        _ = (self.main_dir / "README.md").write_text("repo entry", encoding="utf-8")
        self.write_main_config()
        before = set(self.home.iterdir())
        login = self.logged_in()

        with (
            patch.object(account, "claude_account", return_value=login),
            patch.object(account, "label_for", return_value="claude2"),
        ):
            result, stdout, stderr = self.run_cli("status")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertNotRegex(stdout, r"\| ~/\.claude +\|")
        self.assertIn(f"~/.claude: legacy login is stored; the home still holds a login; {home_step}", stdout)
        self.assertNotIn("unit directors", stdout)
        self.assertNotIn("unclassified entries", stdout)
        self.assertNotIn("mcpServers differ", stdout)
        self.assertEqual(set(self.home.iterdir()), before)
        self.assertFalse(any(self.home.glob(".claude[0-9]*")))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
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
            f"claude2: cannot use {account_dir}; {account_dir} is a link; use a real private directory; run accounts.py setup claude2",
            stdout,
        )
        self.assertRegex(stdout, r"\| ~/\.claude2 +\| claude2 +\|")
        self.assertNotIn("mcpServers differ", stdout)
        self.assertEqual(
            login_read.call_args_list,
            [call(self.main_dir)] * login_read.call_count,
        )

    def assert_mcp_status_streams(self, *, tty: bool, helper_note: bool) -> None:
        _ = agent_accounts.claude_config_file(self.main_dir).write_text(json.dumps({"mcpServers": {"brp": {}}}))
        directory = self.home / ".claude2"
        self.correct_links(directory)
        self.write_note("claude2", "owner@notexample.com")
        identity = directory / ".claude.json"
        _ = identity.write_text(json.dumps({"oauthAccount": {"emailAddress": "owner@notexample.com"},
                                          "mcpServers": {"account-only": {}}}))
        names = cast(Callable[[Path, Path], accounts.ReadMcpConfiguration | accounts.UnreadableMcpConfiguration],
                     getattr(accounts, "_mcp_server_names"))
        note = "~/.claude2: mcpServers: - brp"

        def read_names(path: Path, project: Path) -> accounts.ReadMcpConfiguration | accounts.UnreadableMcpConfiguration:
            result = names(path, project)
            # A nested reader can emit an advisory, as older MCP readers did.
            if helper_note and path == identity:
                print(note, file=sys.stderr)
            return result

        expected = ("claude2: mcpServers differ from ~/.claude; - brp, + account-only; "
                    + "check mcpServers in ~/.claude2/.claude.json against ~/.claude.json")
        for arguments in ((), ("status", "--json"), ("status", "--stored"), ("status", "--stored", "--json")):
            with self.subTest(arguments=arguments, tty=tty, helper_note=helper_note):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (redirect_stdout(stdout), redirect_stderr(stderr),
                      patch.object(stderr, "isatty", return_value=tty),
                      patch.object(accounts, "_mcp_server_names", side_effect=read_names),
                      patch.object(subprocess, "run", side_effect=AssertionError("no real process")),
                      patch.object(subprocess, "Popen", side_effect=AssertionError("no real process"))):
                    result = accounts.main(list(arguments))
                self.assertEqual(result, 0)
                self.assertEqual(stderr.getvalue(), note + "\n" if tty and helper_note else "")
                if "--json" in arguments:
                    document = cast(dict[str, object], json.loads(stdout.getvalue()))
                    self.assertIn(expected, cast(list[str], document["problems"]))
                else:
                    self.assertIn(expected, stdout.getvalue().splitlines())

    def test_piped_status_keeps_mcp_problem_in_table_and_json(self) -> None:
        self.assert_mcp_status_streams(tty=False, helper_note=False)

    def test_piped_status_suppresses_nested_account_notes(self) -> None:
        self.assert_mcp_status_streams(tty=False, helper_note=True)

    def test_terminal_status_keeps_nested_account_notes(self) -> None:
        self.assert_mcp_status_streams(tty=True, helper_note=True)

    def test_cached_role_map_problem_keeps_time_and_actual_fetch_failure(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        for failure in (OSError("fixture transport failed"), role_map.RemoteCommandRefused("fixture refused"),
                        ValueError("fixture bad answer")):
            for flags in ((), ("--json",)):
                with (self.subTest(failure=type(failure).__name__, flags=flags),
                      patch.object(role_map, "is_role_map_host", return_value=False),
                      patch.object(role_map, "ssh_role_map", side_effect=failure) as fetch):
                    code, output, error = self.run_cli("status", *flags)
                self.assertEqual((code, error), (0, ""))
                fetch.assert_called_once()
                lines = (cast(list[str], json.loads(output)["problems"]) if flags else output.splitlines())
                problems = [line for line in lines if line.startswith("role map: using saved assignments;")]
                self.assertEqual(len(problems), 1)
                self.assertIn("2026-10-16", problems[0])
                self.assertIn(str(failure), problems[0])
                self.assertIn("restore access to natedev", problems[0])

    def test_stored_role_map_problem_says_no_fetch_was_attempted(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        for flags in ((), ("--json",)):
            with (self.subTest(flags=flags), patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored read fetched"))):
                code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, error), (0, ""))
            lines = (cast(list[str], json.loads(output)["problems"]) if flags else output.splitlines())
            problems = [line for line in lines if line.startswith("role map: using saved assignments;")]
            self.assertEqual(len(problems), 1)
            self.assertIn("no fetch was attempted", problems[0])
            self.assertIn("2026-10-16", problems[0])
            self.assertNotIn("unreachable", problems[0])

    def test_piped_status_replays_held_warning_before_the_final_error(self) -> None:
        def failed_read(*_args: object, **_kwargs: object) -> accounts.StatusReading:
            print("fixture warning before failure", file=sys.stderr)
            raise accounts.AccountsError("accounts: fixture read failed")
        with patch.object(accounts, "_read_status", side_effect=failed_read):
            code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("fixture warning before failure", error)
        self.assertIn("fixture read failed", error)
        self.assertLess(error.index("fixture warning"), error.index("fixture read failed"))

    def test_piped_status_keeps_cache_warning_when_a_later_read_fails(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline")),
              patch.object(accounts, "group_accounts", side_effect=accounts.AccountsError("accounts: fixture later failure"))):
            code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("2026-10-16", error)
        self.assertIn("fixture offline", error)
        self.assertIn("fixture later failure", error)

    def test_piped_status_keeps_read_diagnostics_when_rendering_fails(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline")),
              patch.object(accounts, "render_status_json", side_effect=accounts.AccountsError("accounts: fixture render failed"))):
            code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("2026-10-16", error)
        self.assertIn("fixture offline", error)
        self.assertIn("fixture render failed", error)

    def test_piped_status_replays_render_warning_when_rendering_fails(self) -> None:
        def render(_reading: accounts.StatusReading) -> str:
            print("fixture render warning", file=sys.stderr)
            raise accounts.AccountsError("accounts: fixture render failed")
        with patch.object(accounts, "render_status_json", side_effect=render):
            code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("fixture render warning", error)
        self.assertIn("fixture render failed", error)

    def test_piped_status_keeps_an_unrepresented_helper_warning(self) -> None:
        original = accounts.account_statuses
        def inspect() -> list[accounts.AccountStatus]:
            print("usage store: could not claim the read; read without a hold", file=sys.stderr)
            return original()
        with patch.object(accounts, "account_statuses", side_effect=inspect):
            code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        lines = cast(list[str], json.loads(output)["problems"])
        self.assertIn("accounts: read warning; usage store: could not claim the read; read without a hold", lines)

    def test_piped_status_preserves_unknown_reader_and_renderer_warnings(self) -> None:
        original = accounts.render_status_json
        def render(reading: accounts.StatusReading) -> str:
            print("fixture renderer warning", file=sys.stderr)
            return original(reading)
        with patch.object(accounts, "render_status_json", side_effect=render):
            code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual(code, 0)
        self.assertIn("fixture renderer warning", error)
        self.assertIsInstance(cast(object, json.loads(output)), dict)

    def test_usage_store_warnings_stay_beside_good_readings(self) -> None:
        def response(_request: object, **_options: object) -> io.BytesIO:
            return io.BytesIO(b'{"seven_day":{"utilization":25,"resets_at":"2099-10-25T12:00:00Z"}}')

        self.login_directory("claude2", "owner@notexample.com")
        for failure in ("claim", "finish", "final", "recovered"):
            for flags in ((), ("--json",)):
                with self.subTest(failure=failure, flags=flags):
                    usage_store.store_file().unlink(missing_ok=True)
                    error = OSError("fixture store denied")
                    with (patch.object(agent_accounts, "claude_live", new=self.read_claude_live),
                          patch.object(agent_accounts, "command_json", return_value={"loggedIn": True,
                              "email": "owner@notexample.com", "authMethod": "claude.ai"}),
                          patch.object(agent_accounts, "claude_credentials", return_value={
                              "claudeAiOauth": {"accessToken": "fixture"}}),
                          patch.object(urllib.request, "urlopen", side_effect=response),
                          patch.object(usage_store, "claim_read", side_effect=error) if failure == "claim" else
                              patch.object(usage_store, "finish_read", side_effect=error) if failure in ("finish", "recovered") else
                              patch.object(usage_store, "save", side_effect=error),
                          patch.object(usage_store, "save", side_effect=error) if failure == "finish" else
                              patch.object(sys, "dont_write_bytecode", True)):
                        code, output, stderr = self.run_cli("status", *flags)
                    self.assertEqual((code, stderr), (0, ""))
                    if flags:
                        document = cast(accounts.StatusDocument, json.loads(output))
                        lines = document["problems"]
                        self.assertTrue(any(row["weekly_left_percent"] == 75 for row in document["accounts"]))
                    else:
                        lines = output.splitlines()
                        self.assertIn("75%", output)
                    warnings = [line for line in lines if "usage store" in line]
                    if failure == "recovered":
                        self.assertEqual(warnings, [])
                    else:
                        self.assertTrue(warnings)
                        self.assertTrue(any("fixture store denied" in line for line in warnings))
                        self.assertTrue(any("without a hold" in line if failure == "claim" else "not saved" in line
                                            for line in warnings))

    def test_observed_warnings_survive_later_reads_and_render_failures(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        for diagnostic in ("cached map", "stored map", "claim", "save", "final save", "recovered save"):
            for later in ("read", "render"):
                for flags in ((), ("--json",)):
                    with self.subTest(diagnostic=diagnostic, later=later, flags=flags):
                        usage_store.store_file().unlink(missing_ok=True)
                        cache = role_map.role_map_cache_file()
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
                            "groups": {}, "workers": "codex1"}}))
                        warning = (agent_accounts.UsageClaimFailed("fixture claim denied") if diagnostic == "claim"
                                   else agent_accounts.UsageSaveFailed("fixture per-read denied"))
                        report = agent_accounts.Report("Claude", email="fixture@notexample.com",
                            quotas=[agent_accounts.Quota("Weekly", 25, datetime(2099, 1, 1, tzinfo=timezone.utc))],
                            usage_read_outcome=agent_accounts.UsageReadOutcome.OK,
                            store_warnings=(warning,) if diagnostic in ("claim", "save", "recovered save") else ())
                        def live(_directory: Path) -> agent_accounts.Report:
                            if report.store_warnings:
                                print(agent_accounts.usage_store_warning_line(warning), file=sys.stderr)
                            print("fixture unmatched warning", file=sys.stderr)
                            return report
                        renderer = "render_status_json" if flags else "render_status_table"
                        failure = accounts.AccountsError("accounts: fixture later failure")
                        with (patch.object(agent_accounts, "claude_live", side_effect=live),
                              patch.object(role_map, "is_role_map_host", return_value=False) if diagnostic.endswith("map") else
                                  patch.object(role_map, "is_role_map_host", return_value=True),
                              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline")),
                              patch.object(usage_store, "save", side_effect=PermissionError("fixture final denied"))
                                  if diagnostic in ("save", "final save") else patch.object(sys, "dont_write_bytecode", True),
                              patch.object(accounts, "_status_row", side_effect=failure) if later == "read" and str(diagnostic) == "stored map" else
                              patch.object(accounts, "group_accounts", side_effect=failure) if later == "read" else
                                  patch.object(accounts, renderer, side_effect=failure)):
                            code, output, error = self.run_cli("status", *(('--stored',) if str(diagnostic) == "stored map" else ()), *flags)
                        self.assertEqual((code, output), (1, ""))
                        self.assertTrue(error.rstrip().endswith("fixture later failure; check the account folders and role map"))
                        if diagnostic.endswith("map"):
                            expected_map = ("role map: using saved assignments; map saved at 2026-10-16 08:24 PDT; "
                                + ("stored mode; no fetch was attempted; run accounts.py status to fetch current assignments"
                                   if diagnostic == "stored map" else
                                   "natedev fetch failed: fixture offline; restore access to natedev; run accounts.py status again"))
                            self.assertIn(expected_map, error.splitlines())
                        else:
                            self.assertIn("fixture unmatched warning", error)
                            if diagnostic == "recovered save":
                                self.assertNotIn("not saved", error)
                                self.assertNotIn("fixture per-read denied", error)
                            else:
                                reason = ("fixture claim denied" if diagnostic == "claim" else
                                          "fixture per-read denied; fixture final denied" if diagnostic == "save" else
                                          "fixture final denied")
                                wrong = "usage store read continued without a hold" if diagnostic == "claim" else "usage store reading not saved"
                                self.assertRegex(error, "claude2: " + wrong + "; " + reason
                                    + r"; read at \d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Z]+; check the usage store file and its permissions; run accounts.py status again")
                                self.assertEqual(error.count("claude2: usage store"), 1)

    def test_terminal_warning_does_not_hide_a_different_final_save_failure(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        warning = agent_accounts.UsageSaveFailed("fixture per-read denied")
        report = agent_accounts.Report("Claude", email="fixture@notexample.com",
            usage_read_outcome=agent_accounts.UsageReadOutcome.OK, store_warnings=(warning,))
        def live(_directory: Path) -> agent_accounts.Report:
            print(agent_accounts.usage_store_warning_line(warning), file=sys.stderr)
            return report
        for tty in (False, True):
            for later in ("merge", "render"):
                for flags in ((), ("--json",)):
                    output, error = io.StringIO(), io.StringIO()
                    with (self.subTest(tty=tty, later=later, flags=flags), redirect_stdout(output), redirect_stderr(error),
                          patch.object(error, "isatty", return_value=tty),
                          patch.object(agent_accounts, "claude_live", side_effect=live),
                          patch.object(usage_store, "save", side_effect=PermissionError("fixture final denied")),
                          patch.object(usage_store, "merge", side_effect=accounts.AccountsError("accounts: fixture render failed"))
                              if later == "merge" else patch.object(accounts, "render_status_json" if flags else "render_status_table",
                                  side_effect=accounts.AccountsError("accounts: fixture render failed"))):
                        code = accounts.main(["status", *flags])
                    self.assertEqual((code, output.getvalue()), (1, ""))
                    self.assertEqual(error.getvalue().splitlines().count(agent_accounts.usage_store_warning_line(warning)), int(tty))
                    self.assertIn("fixture final denied; read at ", error.getvalue())
                    self.assertTrue(error.getvalue().splitlines()[-1].startswith("accounts: fixture render failed;"))

    def test_observed_role_problems_survive_the_next_role_inspection_failure(self) -> None:
        self.write_note("claude3", "fixture@notexample.com")
        path = role_map.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        for mapped in (False, True):
            _ = path.write_text(json.dumps({"units": "claude3"} if mapped else {}))
            expected = ("claude3: not set up; no folder holds this account; affected roles: units; run accounts.py setup claude3"
                        if mapped else "units: not configured; the role map selects no account; run accounts.py move units <claudeN>")
            original = accounts.group_label
            def selected(groups: accounts.GroupAccounts, group: accounts.ClaudeGroup) -> str | accounts.GroupNotConfigured:
                if group == "showrunners":
                    raise accounts.AccountsError("accounts: fixture next role failed")
                return original(groups, group)
            for flags in ((), ("--json",)):
                with self.subTest(mapped=mapped, flags=flags), patch.object(accounts, "group_label", side_effect=selected):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(expected, [line.removeprefix("* ") for line in error.splitlines()])
                self.assertTrue(error.splitlines()[-1].startswith("accounts: fixture next role failed;"))

    def test_failed_status_keeps_test_signin_advice_filtered(self) -> None:
        self.login_directory("claude2", "fixture@example.invalid")
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "claude2.md"
        _ = note.write_text("---\nname: fixture\n---\n")
        groups = role_map.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude2", "showrunners": "claude2"}))
        original = accounts.group_label

        def selected(groups: accounts.GroupAccounts, group: accounts.ClaudeGroup) -> str | accounts.GroupNotConfigured:
            if group == "showrunners":
                raise accounts.AccountsError("accounts: fixture later failure")
            return original(groups, group)

        for flags in ((), ("--json",)):
            with self.subTest(flags=flags), patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli("status", "--stored", *flags)
                self.assertEqual((code, error), (0, ""))
                expected = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags
                            else output.split("\n\n", 1)[1].strip().splitlines())
                self.assertEqual(len(expected), 2)
                self.assertTrue(any("test wrote" in line for line in expected))
                for later in ("read", "render"):
                    with (self.subTest(later=later),
                          patch.object(accounts, "group_label", side_effect=selected) if later == "read" else
                              patch.object(accounts, "render_status_json" if flags else "render_status_table",
                                  side_effect=accounts.AccountsError("accounts: fixture later failure"))):
                        code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, output), (1, ""))
                    self.assertCountEqual([line.removeprefix("* ") for line in error.splitlines()[:-1]], expected)
                    self.assertFalse(any(text in error for text in ("account note", "setup claude2", "cutover-home")))
                    self.assertEqual(error.splitlines()[-1],
                        "accounts: fixture later failure; check the account folders and role map")

    def test_failure_replay_keeps_the_workers_warning_before_the_later_codex_home_problem(self) -> None:
        self.write_note("codex1", "fixture@example.invalid")
        expected = [
            "workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>",
            "* codex1: not set up; no folder holds this account; run accounts.py setup codex1",
            "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
            "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
            "accounts: fixture render failed; check the account folders and role map",
        ]
        for flags in ((), ("--json",)):
            with (self.subTest(flags=flags), patch.object(role_map, "is_role_map_host", return_value=True),
                  patch.object(accounts, "render_status_json" if flags else "render_status_table",
                      side_effect=accounts.AccountsError("accounts: fixture render failed"))):
                code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, output), (1, ""))
            self.assertEqual(error.splitlines(), expected)

    def test_default_mcp_configuration_and_unreadable_baseline_are_reported(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        config = self.home / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"},
                                        "mcpServers": {"account-only": {}}}))
        baseline = agent_accounts.claude_config_file(self.main_dir)
        self.assertFalse((self.main_dir / ".claude.json").exists())
        for readable in (True, False):
            _ = baseline.write_text(json.dumps({"mcpServers": {"brp": {}}}) if readable else "{")
            for flags in ((), ("--json",)):
                with self.subTest(readable=readable, flags=flags):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, error), (0, ""))
                    lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags else output.splitlines())
                    if readable:
                        self.assertIn("claude2: mcpServers differ from ~/.claude; - brp, + account-only; check mcpServers in ~/.claude2/.claude.json against ~/.claude.json", lines)
                    else:
                        self.assertTrue(any("MCP configuration" in line and "~/.claude.json" in line
                                            and "Expecting property name" in line for line in lines))
                        self.assertNotIn("mcpServers differ", output)

    def test_mcp_comparison_uses_each_home_project_scope(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        baseline = agent_accounts.claude_config_file(self.main_dir)
        config = self.home / ".claude2/.claude.json"
        _ = baseline.write_text(json.dumps({"mcpServers": {"brp": {}}, "projects": {
            str(self.main_dir): {"mcpServers": {"brp": {}}, "disabledMcpServers": ["brp"]}}}))
        for enabled in (False, True):
            _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"},
                "projects": {str(config.parent): {"mcpServers": {"brp": {}} if enabled else {}}}}))
            for flags in ((), ("--json",)):
                with self.subTest(enabled=enabled, flags=flags):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, error), (0, ""))
                    lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags else output.splitlines())
                    if enabled:
                        self.assertIn("claude2: mcpServers differ from ~/.claude; + brp; check mcpServers in ~/.claude2/.claude.json against ~/.claude.json", lines)
                    else:
                        self.assertFalse(any("mcpServers differ" in line for line in lines))

    def test_mcp_comparison_includes_a_session_project_outside_the_home(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = self.root / "session-project"
        project.mkdir()
        _ = agent_accounts.claude_config_file(self.main_dir).write_text(json.dumps({
            "projects": {str(project): {"mcpServers": {"brp": {}}}}}))
        identity = self.home / ".claude2/.claude.json"
        for matching in (False, True):
            _ = identity.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"},
                "projects": {str(project): {"mcpServers": {"brp": {}} if matching else {}}}}))
            for flags in ((), ("--json",)):
                with self.subTest(matching=matching, flags=flags):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, error), (0, ""))
                    lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags else output.splitlines())
                    if matching:
                        self.assertFalse(any("mcpServers differ" in line for line in lines))
                    else:
                        self.assertIn("claude2: mcpServers differ from ~/.claude; - brp; check mcpServers in ~/.claude2/.claude.json against ~/.claude.json", lines)

    def test_selected_roles_name_the_missing_account_cause_once(self) -> None:
        for label in ("claude2", "claude3"):
            self.write_note(label, "fixture@notexample.com")
        path = role_map.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        for different in (False, True):
            _ = path.write_text(json.dumps({"units": "claude2", "showrunners": "claude3" if different else "claude2"}))
            for flags in ((), ("--json",)):
                with self.subTest(different=different, flags=flags):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, error), (0, ""))
                    lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags
                             else output.split("\n\n", 1)[1].strip().splitlines())
                    expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>"]
                    for label in ("claude2", "claude3"):
                        roles = "units" if label == "claude2" and different else "showrunners" if different else "units, showrunners" if label == "claude2" else ""
                        expected.append(("" if flags else "* ") + f"{label}: not set up; no folder holds this account; "
                            + (f"affected roles: {roles}; " if roles else "") + f"run accounts.py setup {label}")
                    self.assertCountEqual(lines, expected)

    def test_note_observation_is_shared_by_home_advice_and_real_role_selection(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        path = role_map.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude2", "showrunners": "claude2"}))
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "claude2.md"
        read_text = Path.read_text
        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == note:
                raise PermissionError("fixture note denied")
            return read_text(path, encoding=encoding, errors=errors)

        for fault in ("malformed", "missing", "unreadable"):
            if fault == "missing":
                note.unlink(missing_ok=True)
                expected_note = (f"claude2: not selectable; claude2 has no account note at {note}; "
                    + "add ~/rust/hanadocs/agents/claudeN.md with login: fixture@notexample.com; run accounts.py setup claude2")
            else:
                _ = note.write_text("---\nname: no login\n---\n")
                expected_note = (f"claude2: cannot read {note}; fixture note denied; " if fault == "unreadable" else
                    f"claude2: not selectable; {note} has no readable login in its frontmatter; ")
                expected_note += "repair the account note with login: fixture@notexample.com; run accounts.py setup claude2"
            expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>", expected_note]
            for flags in ((), ("--json",)):
                with (self.subTest(fault=fault, flags=flags),
                      patch.object(Path, "read_text", new=read) if fault == "unreadable" else
                          patch.object(sys, "dont_write_bytecode", True)):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                self.assertEqual((code, error), (0, ""))
                lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags
                         else output.split("\n\n", 1)[1].strip().splitlines())
                self.assertCountEqual(lines, expected)

    def test_failed_codex_save_and_render_keep_the_good_reading_diagnostic(self) -> None:
        home = self.home / ".codex1"
        home.mkdir()
        self.write_note("codex1", "fixture@notexample.com")
        report = agent_accounts.Report("Codex", email="fixture@notexample.com",
            quotas=[agent_accounts.Quota("Weekly", 20, datetime(2099, 1, 1, tzinfo=timezone.utc))],
            usage_read_outcome=agent_accounts.UsageReadOutcome.OK)
        for flags in ((), ("--json",)):
            with (patch.object(agent_accounts, "codex_email_on_disk", return_value="fixture@notexample.com"),
                  patch.object(accounts, "_codex_home_login", return_value="fixture@notexample.com"),
                  patch.object(accounts, "_codex_live", return_value=report),
                  patch.object(usage_store, "save", side_effect=PermissionError("fixture final Codex save denied")),
                  patch.object(accounts, "render_status_json" if flags else "render_status_table",
                               side_effect=accounts.AccountsError("accounts: fixture render failed"))):
                code, output, error = self.run_cli("status", *flags)
            self.assertEqual((code, output), (1, ""))
            self.assertRegex(error, r"codex1: usage store reading not saved; fixture final Codex save denied; read at [^\n]+; check the usage store file and its permissions; run accounts.py status again")
            self.assertTrue(error.splitlines()[-1].startswith("accounts: fixture render failed;"))

    def test_failed_status_restores_terminal_delivery_and_does_not_leak_to_next_command(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        output, error = io.StringIO(), io.StringIO()
        with (redirect_stdout(output), redirect_stderr(error), patch.object(error, "isatty", return_value=True),
              patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture transport denied")),
              patch.object(accounts, "render_status_json", side_effect=accounts.AccountsError("accounts: fixture render failed"))):
            code = accounts.main(["status", "--json"])
        self.assertEqual((code, output.getvalue()), (1, ""))
        self.assertEqual(error.getvalue().count("fixture transport denied"), 1)
        self.assertTrue(error.getvalue().splitlines()[-1].startswith("accounts: fixture render failed;"))
        code, output_text, error_text = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error_text), (0, ""))
        self.assertNotIn("fixture transport denied", output_text)
        self.assertNotIn("fixture render failed", output_text)

    def test_mcp_compares_the_active_home_configuration_and_preserves_unreadability(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        _ = (self.main_dir / ".claude.json").write_text(json.dumps({"mcpServers": {"stale": {}}}))
        _ = agent_accounts.claude_config_file(self.main_dir).write_text(json.dumps({"mcpServers": {"current": {}}}))
        identity = self.home / ".claude2/.claude.json"
        _ = identity.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"},
                                          "mcpServers": {"current": {}}}))
        for flags in ((), ("--json",)):
            code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, error), (0, ""))
            self.assertNotIn("mcpServers differ", output)
        _ = identity.write_text("{")
        for flags in ((), ("--json",)):
            code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, error), (0, ""))
            self.assertIn("cannot read", output)
            self.assertNotIn("mcpServers differ", output)

    def test_piped_status_keeps_command_errors_on_stderr(self) -> None:
        with patch.object(accounts, "_read_status", side_effect=accounts.AccountsError("accounts: fixture read failed")):
            result, stdout, stderr = self.run_cli("status", "--stored", "--json")
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("fixture read failed", stderr)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_detail_shows_unclassified_entries_and_mcp_difference(
        self,
    ) -> None:
        self.write_main_config()
        _ = agent_accounts.claude_config_file(self.main_dir).write_text(json.dumps({"mcpServers": {"brp": {}}}))
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
            json.dumps({"unit": "claude2"}), encoding="utf-8"
        )

        with (
            patch.object(account, "claude_account", return_value=None),
            patch.object(account, "label_for", return_value="unknown"),
        ):
            result, stdout, stderr = self.run_cli("--detail")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertRegex(stdout, r"\| ~/\.claude2 +\| claude2 +\|")
        self.assertIn("claude2: unclassified entries; mystery-cache; check these entries in ~/.claude2; keep private files in this home", stdout.splitlines())
        self.assertIn("claude2: mcpServers differ from ~/.claude; - brp, + account-only; check mcpServers in ~/.claude2/.claude.json against ~/.claude.json", stdout.splitlines())

    def test_claude_feedback_telemetry_and_atomic_write_temps_stay_private_after_setup(self) -> None:
        self.write_main_config()
        directory = self.home / ".claude2"
        directory.mkdir()
        for name in ("feedback", "telemetry"):
            (directory / name).mkdir()
        temporary = directory / ".claude.json.tmp.1234.abcdef"
        _ = temporary.write_text("unfinished config", encoding="utf-8")
        _ = (directory / "unexpected-entry").write_text("keep visible in detail", encoding="utf-8")

        self.assertEqual(accounts.setup("claude2"), [])

        self.assertEqual(accounts._unclassified(directory), ("unexpected-entry",))  # pyright: ignore[reportPrivateUsage]
        for name in ("feedback", "telemetry", temporary.name):
            self.assertFalse((directory / name).is_symlink())
        self.assertEqual(temporary.read_text(encoding="utf-8"), "unfinished config")

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unknown_claude_and_codex_entries_show_only_with_detail(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "spare@example.invalid")
        self.write_note("codex1", "worker@example.invalid")
        codex_home = self.home / ".codex1"
        self.codex_login("worker@example.invalid", codex_home)
        _ = (self.home / ".claude3" / "unknown-claude-entry").write_text("own", encoding="utf-8")
        _ = (codex_home / "unknown-codex-entry").write_text("own", encoding="utf-8")

        for arguments in ((), ("status",)):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli(*arguments)
                self.assertEqual((code, error), (0, ""))
                self.assertNotIn("unclassified entries", output)
                self.assertNotIn("unknown-claude-entry", output)
                self.assertNotIn("unknown-codex-entry", output)

        code, output, error = self.run_cli("--detail")

        self.assertEqual((code, error), (0, ""))
        self.assertIn("claude3: unclassified entries; unknown-claude-entry; check these entries in ~/.claude3; keep private files in this home", output.splitlines())
        self.assertIn("codex1: unclassified entries; unknown-codex-entry; check these entries in ~/.codex1; keep private files in this home", output.splitlines())

    def test_temp_sweep_deletes_only_old_regular_claude_write_temps_in_home_and_numbered_folders(self) -> None:
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        old = (now - timedelta(days=2)).timestamp()
        fresh = (now - timedelta(hours=1)).timestamp()
        cutoff = (now - timedelta(days=1)).timestamp()
        deleted: list[Path] = []
        retained: list[Path] = []
        target = self.root / "symlink-target"
        _ = target.write_text("outside the sweep", encoding="utf-8")
        os.utime(target, (old, old))
        links: list[Path] = []
        for folder in (self.home, self.home / ".claude2", self.home / ".claude17"):
            folder.mkdir(exist_ok=True)
            stale = folder / ".claude.json.tmp.1234.old"
            _ = stale.write_text("stale", encoding="utf-8")
            os.utime(stale, (old, old))
            deleted.append(stale)
            for name, modified in ((".claude.json.tmp.1234.fresh", fresh),
                                   (".claude.json.tmp.1234.exactly-one-day", cutoff),
                                   (".claude.json.tmpx.1234.other", old)):
                path = folder / name
                _ = path.write_text("keep", encoding="utf-8")
                os.utime(path, (modified, modified))
                retained.append(path)
            link = folder / ".claude.json.tmp.1234.link"
            link.symlink_to(target)
            links.append(link)
            nested = folder / ".claude.json.tmp.1234.directory"
            nested.mkdir()
            child = nested / ".claude.json.tmp.1234.nested"
            _ = child.write_text("keep", encoding="utf-8")
            os.utime(child, (old, old))
            retained.append(child)
        linked_folder = self.root / "linked-account"
        linked_folder.mkdir()
        (self.home / ".claude99").symlink_to(linked_folder, target_is_directory=True)
        for folder in (linked_folder, self.main_dir, self.home / ".claude-not-numbered"):
            folder.mkdir(exist_ok=True)
            path = folder / ".claude.json.tmp.1234.out-of-scope"
            _ = path.write_text("keep", encoding="utf-8")
            os.utime(path, (old, old))
            retained.append(path)

        accounts.sweep_claude_temp_files(now)

        for path in deleted:
            with self.subTest(deleted=path):
                self.assertFalse(path.exists())
        for path in retained:
            with self.subTest(retained=path):
                self.assertEqual(path.read_text(encoding="utf-8"), "keep")
        for link in links:
            self.assertTrue(link.is_symlink())
        self.assertTrue((self.home / ".claude99").is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "outside the sweep")

    def test_temp_sweep_silently_skips_vanished_and_undeletable_files_and_continues(self) -> None:
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=2)).timestamp()
        vanished, blocked, removable = (
            self.home / f".claude.json.tmp.1234.{name}" for name in ("vanished", "blocked", "removable")
        )
        for path in (vanished, blocked, removable):
            _ = path.write_text("stale", encoding="utf-8")
            os.utime(path, (old, old))
        unlink = os.unlink

        def delete(path: str, *, dir_fd: int) -> None:
            if path == blocked.name:
                raise PermissionError("cannot delete this file")
            unlink(path, dir_fd=dir_fd)
            if path == vanished.name:
                raise FileNotFoundError("another process already deleted this file")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(os, "unlink", side_effect=delete), redirect_stdout(stdout), redirect_stderr(stderr):
            accounts.sweep_claude_temp_files(now)

        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertFalse(vanished.exists())
        self.assertFalse(removable.exists())
        self.assertEqual(blocked.read_text(encoding="utf-8"), "stale")

    def test_temp_sweep_keeps_outside_files_when_discovered_folder_becomes_a_link(self) -> None:
        now = datetime.now(timezone.utc)
        folder = self.home / ".claude17"
        folder.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        temporary = outside / ".claude.json.tmp.1234.old"
        _ = temporary.write_text("outside the sweep", encoding="utf-8")
        old = (now - timedelta(days=2)).timestamp()
        os.utime(temporary, (old, old))
        scandir = os.scandir
        listings = 0

        @contextmanager
        def swap_before_listing(path: int | Path) -> Generator[Iterator[os.DirEntry[str]]]:
            nonlocal listings
            listings += 1
            if listings == 2:
                _ = folder.rename(self.root / "original-folder")
                folder.symlink_to(outside, target_is_directory=True)
            with scandir(path) as entries:
                yield entries

        with patch.object(os, "scandir", side_effect=swap_before_listing):
            accounts.sweep_claude_temp_files(now)

        self.assertEqual(listings, 2)
        self.assertTrue(folder.is_symlink())
        self.assertEqual(temporary.read_text(encoding="utf-8"), "outside the sweep")

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_codex_unknown_entry_does_not_block_check_and_is_only_shown_in_detail(self) -> None:
        self.write_main_config()
        self.write_note("codex1", "worker@example.invalid")
        self.codex_login("worker@example.invalid", self.home / ".codex1")
        self.assertEqual(accounts.setup("codex1"), [])
        _ = (self.home / ".codex1" / "unknown-codex-entry").write_text("own", encoding="utf-8")

        self.assertEqual(self.run_cli("check", "codex1"), (0, "", ""))
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("unknown-codex-entry", output)
        code, output, error = self.run_cli("--detail")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("codex1: unclassified entries; unknown-codex-entry; check these entries in ~/.codex1; keep private files in this home", output.splitlines())

    def reading(
        self, login: str, five_hour: agent_accounts.Quota | None, weekly: agent_accounts.Quota,
    ) -> agent_accounts.Report:
        return agent_accounts.Report("Claude", email=login, quotas=[weekly], five_hour=five_hour)

    def test_status_table_lists_numbered_folders_but_omits_the_signed_out_shared_codex_home(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.select_named_unit("claude3")
        # A folder set up and never logged in, and claude1: a note, and no folder holds it.
        # The former home login has its own numbered folder as well.
        self.assertEqual(accounts.setup("claude4"), [])
        self.write_note("claude1", "spare@example.invalid")
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
            ("~/.claude1", "claude1", "", *("—",) * 5),
            ("~/.claude2", "claude2", "",
             "71%", weekly_cell, "—", "—", read_cell),
            ("~/.claude3", "claude3", "units",
             "12%", weekly_cell, "—", "—", read_cell),
            ("~/.claude4", "claude4", "", *("—",) * 5),
        ])
        self.assertIn("PDT", read_cell + weekly_cell)
        self.live_reads.assert_not_called()
        self.codex_reads.assert_not_awaited()
        self.assertFalse(accounts.account_dir("claude1").exists())
        self.assertTrue(accounts.account_dir("claude2").exists())

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_hides_five_hour_cells_and_keeps_weekly_and_read(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        now = datetime.now(timezone.utc)
        read = now - timedelta(minutes=1)
        five_reset = now + timedelta(hours=2)
        weekly_reset = now + timedelta(days=3)
        usage_store.save([self.reading(
            "main@example.invalid", agent_accounts.Quota("5-hour", 37, five_reset),
            agent_accounts.Quota("Weekly", 44, weekly_reset),
        )], read)

        result, output, error = self.run_cli("status")

        self.assertEqual((result, error), (0, ""))
        table = output.split("\n\n")[0]
        self.assertNotIn("5-hour", table)
        row = next(row for row in self.status_rows() if row[0] == "~/.claude2")
        zone = ZoneInfo("America/Los_Angeles")
        self.assertNotIn("63%", row)
        self.assertNotIn(five_reset.astimezone(zone).strftime("%H:%M %Z"), row)
        self.assertEqual(row[3:], (
            "56%", weekly_reset.astimezone(zone).strftime("%a %m-%d %H:%M %Z"),
            "—", "—", read.astimezone(zone).strftime("%H:%M %Z"),
        ))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_no_action_means_status(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)

        result, stdout, stderr = self.run_cli()

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout, self.run_cli("status")[1])
        self.assertRegex(stdout, r"\| ~/\.claude2 +\| claude2 +\|")

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_table_is_padded_markdown_and_a_folder_in_order_adds_no_line(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.seed_room("main@example.invalid")

        result, stdout, stderr = self.run_cli("status")

        self.assertEqual((result, stderr), (0, ""))
        lines = [line for line in stdout.split("\n\n")[0].splitlines() if line.startswith("|")]
        # The heading, the rule, and the numbered Claude folder.
        self.assertEqual(len(lines), 3)
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertEqual(set(lines[1]), {"|", " ", "-"})
        self.assertEqual([cell.strip() for cell in lines[2].strip("|").split("|")][:3], ["~/.claude2", "claude2", ""])

    def test_status_reads_live_only_when_no_reading_is_recent_and_keeps_what_it_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.live[accounts.account_dir("claude2")] = self.reading(
            "main@example.invalid", agent_accounts.Quota("5-hour", 10.0, later), agent_accounts.Quota("Weekly", 40.0, later),
        )

        first = self.status_rows()[0]
        second = self.status_rows()[0]

        self.assertEqual(first[3], "60%")
        self.assertNotEqual(first[7], "—")
        self.assertEqual(second, first)
        self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))

    def test_status_table_shows_each_codex_account_and_whether_it_is_on_credits(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.codex_login("Workers@example.invalid")
        self.write_note("codex1", "other@example.invalid")
        self.write_note("codex2", "workers@example.invalid")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"workers": "codex2"}')
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
                    "Codex", email="workers@example.invalid", quotas=[agent_accounts.Quota("Weekly", weekly_used, later)],
                    five_hour=five_hour, credit_balance=balance,
                )], read)

                rows = self.status_rows()

                self.assertEqual(rows[-2][:3], ("~/.codex", "codex2", "workers"))
                self.assertEqual(rows[-2][3:], (
                    f"{100 - weekly_used:.0f}%", later.astimezone(zone).strftime("%a %m-%d %H:%M %Z"),
                    *credit_cells, read.astimezone(zone).strftime("%H:%M %Z"),
                ))
                self.assertEqual(rows[-1], ("~/.codex1", "codex1", "—", *("—",) * 5))
        self.codex_reads.assert_not_awaited()

    def test_status_reads_codex_live_only_when_no_reading_is_recent_and_keeps_what_it_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.codex_login("workers@example.invalid")
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.codex_reads.return_value = agent_accounts.Report(
            "Codex", email="workers@example.invalid", quotas=[agent_accounts.Quota("Weekly", 30.0, later)],
            credit_balance="5",
        )

        first = self.status_rows()[-1]
        second = self.status_rows()[-1]

        # A login with no account note is identified by its home folder.
        self.assertEqual(first[:3], ("~/.codex", "~/.codex", "spare"))
        self.assertEqual((first[3], first[5], first[6]), ("70%", "no", "5"))
        self.assertNotEqual(first[7], "—")
        self.assertEqual(second, first)
        self.codex_reads.assert_awaited_once_with(self.home / ".codex")

    def test_next_add_list_and_remove_print_the_stored_plan(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        code, output, error = self.run_cli("next", "list")
        self.assertEqual((code, output.strip(), error), (0, "nothing set up", ""))
        self.assertFalse(next_moves.next_moves_file().exists())
        code, output, error = self.run_cli("next", "add", "units", "1", "claude2", "--hold-minutes", "5")
        self.assertEqual((code, error), (0, ""))
        entries = next_moves.read_next_moves()
        self.assertEqual(len(entries), 1)
        self.assertIsInstance(entries[0], next_moves.ClaudeGroupMove)
        assert isinstance(entries[0], next_moves.ClaudeGroupMove)
        self.assertEqual((entries[0].group, entries[0].at_percent, entries[0].hold_minutes, entries[0].to),
                         ("units", 1, 5, "claude2"))
        self.assertNotIn(entries[0].id, output)
        self.assertIn("units 1:", output)
        self.assertIn("→ claude2", output)
        code, listed, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(listed, output)
        code, _, error = self.run_cli("next", "remove", "units", "1")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(next_moves.read_next_moves(), ())
        code, output, error = self.run_cli("next", "list")
        self.assertEqual((code, output.strip(), error), (0, "nothing set up", ""))

    def test_next_list_numbers_each_group_in_display_order_without_exposing_ids(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        units = next_moves.add_next_move("units", 1, "claude2", 40)
        worker = next_moves.add_next_move("workers", 0, "claude")
        runner = next_moves.add_next_move("showrunners", 2, "claude2")
        second = next_moves.add_next_move("units", 3, "claude3")
        with next_moves.next_moves_lock():
            next_moves.write_next_moves((units, worker, runner, second))
        code, output, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        lines = [line for line in output.splitlines() if "→" in line]
        self.assertEqual([line.split(":", 1)[0] for line in lines],
                         ["showrunners 1", "units 1", "units 2", "workers 1"])
        for entry in (units, worker, runner, second):
            self.assertNotIn(entry.id, output)
        self.assertIn("hold 40", lines[1])
        self.assertEqual(next_moves.read_next_moves(), (units, worker, runner, second))

    def test_next_half_point_add_and_hold_update_preserve_ids_order_and_fixed_minutes(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        code, output, error = self.run_cli("next", "add", "units", "1", "claude2", "--hold", "half-point")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("hold half a point", output)
        first, = next_moves.read_next_moves()
        worker = next_moves.add_next_move("workers", 0, "claude")
        second = next_moves.add_next_move("units", 2, "claude3", 7)
        with next_moves.next_moves_lock():
            next_moves.write_next_moves((first, worker, second))
        code, output, error = self.run_cli("next", "hold", "units", "1", "12", "--expect", "1", "claude2")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("units 1: at 1% → claude2, hold 12 min", output)
        changed, saved_worker, saved_second = next_moves.read_next_moves()
        self.assertEqual((changed.id, changed.added_at, changed.at_percent),
                         (first.id, first.added_at, first.at_percent))
        self.assertEqual((saved_worker, saved_second), (worker, second))
        code, output, error = self.run_cli("next", "hold", "units", "1", "half-point")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("units 1: at 1% → claude2, hold half a point", output)
        self.assertEqual(next_moves.read_next_moves(), (first, worker, second))

    def test_mac_half_point_add_and_hold_resolve_positions_on_the_host(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        first = next_moves.add_next_move("units", 1, "claude2", 5)
        second = next_moves.add_next_move("units", 2, "claude3", 7)
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            self.assertNotEqual(arguments[0], "next-read")
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            if code:
                raise role_map.RemoteCommandRefused(error.strip())
            self.assertEqual(error, "")
            return output

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote)):
            code, output, error = self.run_cli("next", "hold", "units", "2", "half-point",
                                              "--expect", "2", "claude3")
            self.assertEqual((code, error), (0, ""))
            self.assertIn("units 2: at 2% → claude3, hold half a point", output)
            code, output, error = self.run_cli("next", "add", "units", "3", "claude2",
                                              "--hold", "half-point", "--at", "2")
            self.assertEqual((code, error), (0, ""))
        self.assertEqual(requests[0][:6], ("next", "hold", "units", "2", "half-point", "--expect"))
        self.assertEqual(float(requests[0][6]), 2)
        self.assertEqual(requests[0][7:], ("claude3",))
        self.assertEqual(requests[1][0], "next-write")
        self.assertEqual(requests[1][2:], ("--at", "2"))
        entries = next_moves.read_next_moves()
        self.assertEqual((entries[0], entries[2].id), (first, second.id))
        self.assertIn("hold half a point", next_moves.render_next_move(entries[1]))
        self.assertIn("hold half a point", next_moves.render_next_move(entries[2]))

    def test_empty_list_does_not_read_corrupt_watch_state_locally_or_through_the_mac(self) -> None:
        state_path = refusal_watch.state_root() / "refusal_watch.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _ = state_path.write_text("{")
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            self.assertEqual((code, error), (0, ""))
            return output

        for mac_client in (False, True):
            with (self.subTest(mac_client=mac_client),
                  patch.object(role_map, "is_role_map_host", return_value=not mac_client),
                  patch.object(role_map, "ssh_role_map", side_effect=remote),
                  patch.object(refusal_watch, "_load_state", side_effect=AssertionError("empty list read watch state")) as read):
                self.assertEqual(self.run_cli("next", "list"), (0, "nothing set up\n", ""))
                read.assert_not_called()
        self.assertEqual(requests, [("next", "list")])
        self.assertEqual(state_path.read_text(), "{")

    def test_corrupt_started_hold_state_lists_estimates_and_one_notice_on_both_machines(self) -> None:
        self.login_directory("claude1", "source@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude1"}))
        at = datetime(2026, 10, 17, tzinfo=timezone.utc)
        head = next_moves.add_next_move("units", 1, "claude2", next_moves.HalfPointHold())
        _ = next_moves.add_next_move("units", 2, "claude3", next_moves.HalfPointHold())
        held = refusal_watch.HoldStarted("claude1", at, accounts.NoGroupTransition(), 1,
            next_moves.ResolvedHold(34, next_moves.PaceEstimate(0.9)))
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={head.id: held}))
        notice = "started holds not shown: watch state unreadable"
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            self.assertEqual((code, error), (0, ""))
            return output

        with (patch.object(next_moves, "render_time", return_value=at + timedelta(minutes=22)),
              patch.object(accounts, "group_half_point_hold",
                           return_value=next_moves.ResolvedHold(5, next_moves.PaceEstimate(6)))):
            code, started, error = self.run_cli("next", "list")
            self.assertEqual((code, error), (0, ""))
            self.assertIn("34 min at 0.9 points/hour, 12 min left", started)
            state_path = refusal_watch.state_root() / "refusal_watch.json"
            _ = state_path.write_text("{")
            before_log = next_moves.next_moves_file().read_bytes()
            expected = ("units 1: at 1% → claude2, hold half a point (≈5 min)\n"
                        + "units 2: at 2% → claude3, hold half a point (≈5 min)\n"
                        + notice + "\n")
            for mac_client in (False, True):
                with (self.subTest(mac_client=mac_client),
                      patch.object(role_map, "is_role_map_host", return_value=not mac_client),
                      patch.object(role_map, "ssh_role_map", side_effect=remote)):
                    code, listed, error = self.run_cli("next", "list")
                    self.assertEqual((code, listed, error), (0, expected, ""))
                    self.assertEqual(listed.count(notice), 1)
                    self.assertEqual(state_path.read_text(), "{")
                    self.assertEqual(next_moves.next_moves_file().read_bytes(), before_log)
        self.assertEqual(requests, [("next", "list")])

    def test_started_hold_row_is_shared_by_list_add_hold_reply_and_the_mac(self) -> None:
        self.login_directory("claude1", "source@example.invalid")
        self.login_directory("claude2", "target@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude1"}))
        at = datetime(2026, 10, 17, tzinfo=timezone.utc)
        entry = next_moves.add_next_move("units", 1.234567, "claude2", next_moves.HalfPointHold())
        held = refusal_watch.HoldStarted("claude1", at, accounts.NoGroupTransition(), entry.at_percent,
            next_moves.ResolvedHold(34, next_moves.PaceEstimate(0.9)))
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={entry.id: held}))
        before = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        expected = "units 1: at 1.234567% → claude2, hold half a point (34 min at 0.9 points/hour, 12 min left)\n"
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            if code:
                raise role_map.RemoteCommandRefused(error.strip())
            self.assertEqual(error, "")
            return output

        with (patch.object(next_moves, "render_time", return_value=at + timedelta(minutes=22, seconds=1)),
              patch.object(run_out, "weighted_rate", side_effect=AssertionError("started row read fresh pace"))):
            code, listed, error = self.run_cli("next", "list")
            self.assertEqual((code, listed, error), (0, expected, ""))
            code, added, error = self.run_cli("next", "add", "workers", "1", "claude")
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(added, expected + "\nworkers 1: at 1% → claude\n")
            code, reply, error = self.run_cli("next", "hold", "units", "1", "half-point",
                                              "--expect", "1.234567", "claude2")
            self.assertEqual((code, reply, error), (0, expected, ""))
            with (patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=remote)):
                code, mac_listed, error = self.run_cli("next", "list")
                self.assertEqual((code, mac_listed, error), (0, added, ""))
                code, mac_reply, error = self.run_cli("next", "hold", "units", "1", "half-point",
                                                      "--expect", "1.234567", "claude2")
                self.assertEqual((code, mac_reply, error), (0, expected, ""))
            self.assertEqual(refusal_watch._load_state(), before)  # pyright: ignore[reportPrivateUsage]
            code, changed, error = self.run_cli("next", "hold", "units", "1", "10")
            self.assertEqual((code, changed, error), (0, "units 1: at 1.234567% → claude2, hold 10 min\n", ""))
        self.assertEqual(requests, [("next", "list"),
            ("next", "hold", "units", "1", "half-point", "--expect", "1.234567", "claude2")])
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]

    def test_listed_percent_guards_its_hold_update_locally_and_through_the_mac(self) -> None:
        entry = next_moves.add_next_move("units", 1.234567, "claude2", 5)
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            if code:
                raise role_map.RemoteCommandRefused(error.strip())
            self.assertEqual(error, "")
            return output

        for mac_client, minutes in ((False, "12"), (True, "17")):
            with (self.subTest(mac_client=mac_client),
                  patch.object(role_map, "is_role_map_host", return_value=not mac_client),
                  patch.object(role_map, "ssh_role_map", side_effect=remote)):
                code, listed, error = self.run_cli("next", "list")
                self.assertEqual((code, error), (0, ""))
                displayed = listed.split("at ", 1)[1].split("%", 1)[0]
                self.assertEqual(displayed, "1.234567")
                code, reply, error = self.run_cli("next", "hold", "units", "1", minutes,
                                                 "--expect", displayed, "claude2")
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(reply, f"units 1: at {displayed}% → claude2, hold {minutes} min\n")
            changed, = next_moves.read_next_moves()
            assert isinstance(changed, next_moves.ClaudeGroupMove)
            self.assertEqual((changed.id, changed.at_percent, changed.hold_mode),
                             (entry.id, entry.at_percent, next_moves.FixedHold(float(minutes))))
        self.assertEqual(requests, [("next", "list"),
            ("next", "hold", "units", "1", "17.0", "--expect", "1.234567", "claude2")])

    def test_mac_hold_preserves_fractional_minutes_and_checks_the_exact_expected_percent(self) -> None:
        entry = next_moves.add_next_move("units", 1.234567, "claude2", 5)
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            if code:
                raise role_map.RemoteCommandRefused(error.strip())
            self.assertEqual(error, "")
            return output

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote)):
            code, output, error = self.run_cli("next", "hold", "units", "1", "10.1234567",
                                              "--expect", "1.234567", "claude2")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("units 1:", output)
        changed, = next_moves.read_next_moves()
        assert isinstance(changed, next_moves.ClaudeGroupMove)
        self.assertEqual((changed.id, changed.at_percent, changed.hold_mode),
                         (entry.id, entry.at_percent, next_moves.FixedHold(10.1234567)))
        self.assertEqual(requests, [("next", "hold", "units", "1", "10.1234567",
                                     "--expect", "1.234567", "claude2")])
        different = next_moves.ClaudeGroupMove(changed.id, changed.group, 1.23457,
                                              changed.hold_mode, changed.to, changed.added_at)
        with next_moves.next_moves_lock():
            next_moves.write_next_moves((different,))
        before = next_moves.next_moves_file().read_bytes()
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote)):
            code, output, error = self.run_cli("next", "hold", "units", "1", "20.1234567",
                                              "--expect", "1.234568", "claude2")
        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("nothing changed", error)
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(next_moves.read_next_moves(), (different,))
        self.assertEqual(requests[-1], ("next", "hold", "units", "1", "20.1234567",
                                        "--expect", "1.234568", "claude2"))

    def test_mac_hold_refusal_is_verbatim_and_transport_failure_keeps_the_verb(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude2", 5)
        before = next_moves.next_moves_file().read_bytes()
        refusal = "next: units has 1 entries; position 3 is out of range"

        def remote(arguments: tuple[str, ...]) -> str:
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            self.assertNotEqual(code, 0)
            self.assertEqual(output, "")
            raise role_map.RemoteCommandRefused(error.strip())

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote)):
            code, output, error = self.run_cli("next", "hold", "units", "3", "half-point")
        self.assertNotEqual(code, 0)
        self.assertEqual((output, error), ("", refusal + "\n"))
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture cannot connect"))):
            code, output, error = self.run_cli("next", "hold", "units", "1", "half-point")
        self.assertNotEqual(code, 0)
        self.assertEqual((output, error), ("", "next: remote hold failed: fixture cannot connect\n"))
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_workers_half_point_hold_is_refused_without_changing_the_queue(self) -> None:
        entry = next_moves.add_next_move("workers", 1, "claude")
        before = next_moves.next_moves_file().read_bytes()
        for arguments in (("add", "workers", "1", "claude", "--hold", "half-point"),
                          ("hold", "workers", "1", "half-point")):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli("next", *arguments)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn("workers", error)
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
                self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_mac_lists_the_hosts_half_point_estimate_despite_different_local_pace(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        self.login_directory("claude3", "next-target@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude2"}))
        code, _, error = self.run_cli("next", "add", "units", "1", "claude3", "--hold", "half-point")
        self.assertEqual((code, error), (0, ""))
        host_history, mac_history = self.root / "host-pace.jsonl", self.root / "mac-pace.jsonl"
        at = datetime.now(timezone.utc)
        clock = self.enterContext(patch.object(next_moves, "datetime", wraps=datetime))
        cast(MagicMock, clock.now).return_value = at
        for path, per_hour in ((host_history, 1), (mac_history, 6)):
            _ = path.write_text("\n".join(json.dumps({"account": "claude2",
                "at": (at - timedelta(minutes=180 - index * 10)).isoformat(),
                "remaining": 10 - index * per_hour / 6}) for index in range(19)))
        with patch.object(run_out, "READINGS_LOG", host_history):
            code, host_output, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("hold half a point (≈", host_output)
        with patch.object(run_out, "READINGS_LOG", mac_history):
            code, local_estimate, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertNotEqual(local_estimate, host_output)

        def remote(arguments: tuple[str, ...]) -> str:
            self.assertEqual(arguments, ("next", "list"))
            with (patch.object(role_map, "is_role_map_host", return_value=True),
                  patch.object(run_out, "READINGS_LOG", host_history)):
                code, output, error = self.run_cli(*arguments)
            self.assertEqual((code, error), (0, ""))
            return output

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote) as transport,
              patch.object(run_out, "READINGS_LOG", mac_history)):
            code, mac_output, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(mac_output, host_output)
        transport.assert_called_once()

    def test_half_point_list_explains_the_default_when_history_is_missing(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude2"}))
        with patch.object(run_out, "READINGS_LOG", self.root / "no-history"):
            code, output, error = self.run_cli("next", "add", "units", "1", "claude2", "--hold", "half-point")
            self.assertEqual((code, error), (0, ""))
            self.assertIn("hold half a point (40 min default: no pace history)", output)
            code, listed, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(listed, output)

    def test_next_cli_moves_removes_and_inserts_by_group_position_and_preserves_ids(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        first = next_moves.add_next_move("units", 1, "claude2")
        worker = next_moves.add_next_move("workers", 0, "claude")
        second = next_moves.add_next_move("units", 2, "claude3")
        runner = next_moves.add_next_move("showrunners", 3, "claude2")
        code, output, error = self.run_cli("next", "move", "units", "2", "1")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group == "units"],
                         [second, first])
        code, added, error = self.run_cli("next", "add", "units", "4", "claude2", "--at", "2")
        self.assertEqual((code, error), (0, ""))
        unit_entries = [entry for entry in next_moves.read_next_moves() if entry.group == "units"]
        self.assertEqual((unit_entries[0], unit_entries[2]), (second, first))
        inserted = unit_entries[1]
        self.assertEqual(inserted.at_percent, 4)
        code, removed, error = self.run_cli("next", "remove", "units", "2")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group == "units"],
                         [second, first])
        self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group != "units"],
                         [worker, runner])
        for entry in (first, second, worker, runner, inserted):
            for reply in (output, added, removed):
                self.assertNotIn(entry.id, reply)

    def test_remote_positional_edits_resolve_on_the_host_after_its_log_changes(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        first = next_moves.add_next_move("units", 1, "claude2")
        second = next_moves.add_next_move("units", 2, "claude3")
        worker = next_moves.add_next_move("workers", 0, "claude")
        inserted_on_host = next_moves.ClaudeGroupMove("host-added-head", "units", 5, 0, "claude2",
                                                     datetime.now(timezone.utc))
        for verb in ("move", "remove", "add"):
            with self.subTest(verb=verb):
                with next_moves.next_moves_lock():
                    next_moves.write_next_moves((first, worker, second))
                requests: list[tuple[str, ...]] = []

                def remote(arguments: tuple[str, ...]) -> str:
                    requests.append(arguments)
                    self.assertNotEqual(arguments[0], "next-read", "a client must not resolve a position")
                    with patch.object(role_map, "is_role_map_host", return_value=True):
                        with next_moves.next_moves_lock():
                            next_moves.write_next_moves((inserted_on_host, first, worker, second))
                        code, output, error = self.run_cli(*arguments)
                        self.assertEqual((code, error), (0, ""))
                        return output

                args = (["move", "units", "2", "1"] if verb == "move" else
                        ["remove", "units", "2"] if verb == "remove" else
                        ["add", "units", "9", "claude2", "--at", "2"])
                with (patch.object(role_map, "is_role_map_host", return_value=False),
                      patch.object(role_map, "ssh_role_map", side_effect=remote)):
                    code, output, error = self.run_cli("next", *args)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(len(requests), 1)
                units = [entry for entry in next_moves.read_next_moves() if entry.group == "units"]
                if verb == "move":
                    self.assertEqual(units, [first, inserted_on_host, second])
                elif verb == "remove":
                    self.assertEqual(units, [inserted_on_host, second])
                else:
                    self.assertEqual((units[0], units[2], units[3]), (inserted_on_host, first, second))
                    self.assertEqual(units[1].at_percent, 9)
                self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group == "workers"],
                                 [worker])
                for entry in next_moves.read_next_moves():
                    self.assertNotIn(entry.id, output)

    def test_mac_positional_refusals_print_the_hosts_last_error_line_without_a_transport_wrapper(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        refusal = "next: units has 1 entries; position 3 is out of range"
        for arguments in (("move", "units", "1", "3"), ("remove", "units", "3"),
                          ("add", "units", "1", "claude2", "--at", "3")):
            with (self.subTest(arguments=arguments),
                  patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=send_role_map_command),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                      ["fixture ssh"], 1, stdout="", stderr=f"fixture diagnostic\n{refusal}\n\n")) as transport):
                code, output, error = self.run_cli("next", *arguments)
            self.assertNotEqual(code, 0)
            self.assertEqual((output, error), ("", refusal + "\n"))
            transport.assert_called_once()
            self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
            self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_mac_positional_transport_failures_keep_the_remote_verb_and_connection_error(self) -> None:
        for verb, arguments in (("move", ("move", "units", "1", "3")),
                                ("remove", ("remove", "units", "3")),
                                ("add", ("add", "units", "1", "claude2", "--at", "3"))):
            with (self.subTest(verb=verb),
                  patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=send_role_map_command),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                      ["fixture ssh"], 255, stdout="", stderr="fixture cannot connect\n")) as transport):
                code, output, error = self.run_cli("next", *arguments)
            self.assertNotEqual(code, 0)
            self.assertEqual((output, error), ("", f"next: remote {verb} failed: fixture cannot connect\n"))
            transport.assert_called_once()

    def test_next_cli_out_of_range_names_group_size_and_does_not_write(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        _ = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        for arguments in (("move", "units", "2", "1"), ("move", "units", "1", "2"),
                          ("remove", "units", "2"), ("remove", "workers", "1"),
                          ("add", "units", "1", "claude2", "--at", "3")):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli("next", *arguments)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                group = "workers" if "workers" in arguments else "units"
                self.assertIn(group, error)
                self.assertIn("0" if group == "workers" else "1", error)
                self.assertEqual(len(error.strip().splitlines()), 1)
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

    def test_next_cli_refuses_ids_and_nonpositive_positions_without_changing_the_log(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        for arguments in (("remove", entry.id), ("remove", "units", entry.id),
                          ("move", "units", entry.id, "1"), ("remove", "units", "0"),
                          ("move", "units", "1", "-1"),
                          ("add", "units", "1", "claude2", "--at", "0")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()) as error:
                try:
                    code = accounts.main(["next", *arguments])
                except SystemExit as refused:
                    code = refused.code
                self.assertNotEqual(code, 0)
                self.assertTrue(error.getvalue())
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

    def test_next_refuses_unknown_accounts_groups_and_invalid_percent_without_storing_an_entry(self) -> None:
        self.login_directory("claude2", "target@example.invalid")
        with redirect_stderr(io.StringIO()) as parser_error:
            with self.assertRaises(SystemExit) as refused:
                _ = accounts.main(["next", "add", "unknown-group", "1", "claude2"])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("unknown-group", parser_error.getvalue())
        self.assertEqual(next_moves.read_next_moves(), ())
        for arguments in (("units", "1", "claude99"),
                          ("units", "-1", "claude2"), ("units", "101", "claude2"),
                          ("units", "nan", "claude2")):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli("next", "add", *arguments)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertTrue(error)
                self.assertEqual(next_moves.read_next_moves(), ())

    def test_workers_next_cli_validates_codex_targets_and_lists_and_removes_both_destinations(self) -> None:
        self.write_note("codex2", "target@example.invalid")
        self.codex_login("target@example.invalid", self.home / ".codex2")
        with patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check:
            code, output, error = self.run_cli("next", "add", "workers", "1", "codex2")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(check.called)
        self.assertIs(cast(object, check.call_args.kwargs["repair"]), False)
        self.assertIn("workers 1:", output)
        self.assertIn("→ codex2", output)
        with patch.object(accounts, "check_codex_target", side_effect=AssertionError("Claude is not a Codex login")):
            code, output, error = self.run_cli("next", "add", "workers", "0", "claude")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("workers 2:", output)
        self.assertIn("→ claude", output)
        entries = next_moves.read_next_moves()
        code, listed, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("workers 1:", listed)
        self.assertIn("→ codex2", listed)
        self.assertIn("workers 2:", listed)
        self.assertIn("→ claude", listed)
        for entry in entries:
            self.assertNotIn(entry.id, listed)
            self.assertEqual(self.run_cli("next", "remove", "workers", "1")[0], 0)
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_workers_queue_checks_numbered_claude_setup_and_login_before_storing(self) -> None:
        self.write_note("claude4", "workers@example.invalid")
        code, output, error = self.run_cli("next", "add", "workers", "1", "claude4")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("accounts.py setup claude4", error)
        self.assertEqual(next_moves.read_next_moves(), ())
        _ = (self.home / ".claude.json").write_text("{}")
        self.assertEqual(accounts.setup("claude4"), [])
        code, _, error = self.run_cli("next", "add", "workers", "1", "claude4")
        self.assertEqual(code, 1)
        self.assertIn("claude and /login", error)
        self.assertEqual(next_moves.read_next_moves(), ())
        self.login_directory("claude4", "workers@example.invalid")
        code, output, error = self.run_cli("next", "add", "workers", "1", "claude4")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("→ claude4", output)
        code, listed, error = self.run_cli("next", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("→ claude4", listed)

    def test_workers_next_cli_rejects_hold_unknown_account_and_remote_invalid_entry(self) -> None:
        for arguments in (("workers", "1", "claude", "--hold-minutes", "5"),
                          ("workers", "1", "codex99"), ("workers", "1", "claude2")):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli("next", "add", *arguments)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertTrue(error)
                self.assertEqual(next_moves.read_next_moves(), ())
        entry = next_moves.WorkersMove("remote-invalid", 1, next_moves.CodexWorkersTarget("codex99"),
                                      datetime.now(timezone.utc))
        code, output, error = self.run_cli("next-write", next_moves.encode_next_moves((entry,)))
        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertTrue(error)
        self.assertEqual(next_moves.read_next_moves(), ())

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_table_displays_both_groups_on_the_same_account_in_group_order(self) -> None:
        self.login_directory("claude2", "shared@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text('{"showrunners": "claude2", "units": "claude2"}')
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| claude2 " in line)
        self.assertEqual([cell.strip() for cell in row.split("|")][3], "units, showrunners")

    def json_status_fixture(self) -> tuple[datetime, datetime]:
        self.login_directory("claude2", "main@example.invalid")
        self.login_directory("claude3", "unit@example.invalid")
        self.write_note("claude1", "missing@example.invalid")
        self.assertEqual(accounts.setup("claude4"), [])
        for label, login in (("codex1", "workers@example.invalid"),
                             ("codex2", "startup@example.invalid"),
                             ("codex3", "unlimited@example.invalid")):
            self.write_note(label, login)
            self.codex_login(login, self.home / f".{label}")
        note = Path(os.environ["AGENT_NOTES_DIR"]) / note_name("codex1")
        _ = note.write_text("---\nlogin: workers@example.invalid\nuse_credits: off\n---\n")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text('{"units": "claude2", "showrunners": "claude3"}')
        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text('{"workers": "codex1"}')
        read = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
        reset = read + timedelta(days=3)
        usage_store.save([
            self.reading("main@example.invalid", None, agent_accounts.Quota("Weekly", 29.4, reset)),
            self.reading("unit@example.invalid", None, agent_accounts.Quota("Weekly", 88, reset)),
            agent_accounts.Report("Codex", email="workers@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 100, reset)], credit_balance="13446.5"),
            agent_accounts.Report("Codex", email="unlimited@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 81, reset)], credit_balance="unlimited"),
        ], read)
        failed_home = self.home / ".codex2"
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="synthetic initialize timeout",
            codex_startup=agent_accounts.CodexStartupFailure(failed_home, "accounts.py setup codex2"))
        return read, reset

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_explains_empty_account_rows_in_the_table_and_json_without_login_addresses(self) -> None:
        self.write_note("claude1", "absent@example.invalid")
        self.write_note("claude2", "signed-out@example.invalid")
        self.correct_links(self.home / ".claude2")
        self.write_note("claude3", "unsafe@example.invalid")
        target = self.root / "unsafe-account"
        target.mkdir()
        (self.home / ".claude3").symlink_to(target, target_is_directory=True)
        self.write_note("codex1", "missing-codex@example.invalid")
        expected = [
            "claude1: not set up; no folder holds this account; run accounts.py setup claude1",
            f"claude2: not logged in; no readable sign-in is stored; run CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude2'))} claude and /login",
            "claude3: folder unavailable; its folder could not be read; run accounts.py setup claude3",
            "codex1: not set up; no folder holds this account; run accounts.py setup codex1",
        ]
        # Some inventory callers have no check diagnostics. Status must still
        # explain each empty row, without reading the linked account folder.
        readiness = accounts.account_directory_readiness
        def without_diagnostics(label: str) -> accounts.AccountDirectoryReadiness:
            return replace(readiness(label), problems=(), state_source=accounts.NoObservedStateCause())

        with patch.object(accounts, "account_directory_readiness", side_effect=without_diagnostics):
            code, table, error = self.run_cli("status", "--stored")
            self.assertEqual((code, error), (0, ""))
            code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        problems = cast(list[str], json.loads(output)["problems"])
        for line in expected:
            self.assertEqual(problems.count(line), 1)
            self.assertEqual(table.splitlines().count("* " + line), 1)
        self.assertTrue(all(not problem.startswith("* ") for problem in problems))
        self.assertTrue(all("@" not in problem for problem in problems))

    def test_status_keeps_existing_folder_and_sign_in_reasons_without_duplicate_notes(self) -> None:
        self.write_note("claude2", "signed-out@example.invalid")
        directory = self.home / ".claude2"
        self.correct_links(directory)
        target = self.root / "unsafe-account"
        target.mkdir()
        (self.home / ".claude3").symlink_to(target, target_is_directory=True)
        self.write_note("codex1", "blocked@example.invalid")
        _ = (self.home / ".codex1").write_text("not a folder")
        expected = [
            f"claude2: not logged in; {directory} has no readable sign-in; run CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} claude and /login",
            f"claude3: cannot use {self.home / '.claude3'}; {self.home / '.claude3'} is a link; use a real private directory; run accounts.py setup claude3",
            "codex1: cannot use ~/.codex1; ~/.codex1 is not a directory; use a real private directory; run accounts.py setup codex1",
        ]
        for arguments in (("status", "--stored"), ("status", "--stored", "--json")):
            with self.subTest(arguments=arguments):
                code, output, error = self.run_cli(*arguments)
                self.assertEqual((code, error), (0, ""))
                notes = (cast(list[str], json.loads(output)["problems"]) if "--json" in arguments
                         else output.split("\n\n", 1)[1].strip().splitlines())
                for line in expected:
                    self.assertEqual(notes.count(line), 1)
                self.assertFalse(any(note.startswith(("* claude2:", "* claude3:", "* codex1:")) for note in notes))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_json_preserves_row_order_numeric_readings_roles_and_table_problems(self) -> None:
        read, reset = self.json_status_fixture()
        code, table, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, error), (0, ""))
        document = cast(dict[str, object], json.loads(output))
        self.assertEqual(set(document), {"accounts", "problems"})
        rows = cast(list[dict[str, object]], document["accounts"])
        keys = {"label", "tool", "roles", "weekly_left_percent", "resets_at", "credits", "use_credits", "read_at",
                "read_ok", "credits_in_use"}
        self.assertTrue(all(set(row) == keys for row in rows))
        expected: list[tuple[str, str, list[str], float | None, str | None, float | str | None, bool, str | None]] = [
            ("claude1", "claude", [], None, None, None, False, None),
            ("claude2", "claude", ["units"], 70.6, reset.isoformat(), None, False, read.isoformat()),
            ("claude3", "claude", ["showrunners"], 12, reset.isoformat(), None, False, read.isoformat()),
            ("claude4", "claude", [], None, None, None, False, None),
            ("codex1", "codex", ["workers"], 0, reset.isoformat(), 13446.5, False, read.isoformat()),
            ("codex2", "codex", ["spare"], None, None, None, True, None),
            ("codex3", "codex", ["spare"], 19, reset.isoformat(), "unlimited", True, read.isoformat()),
        ]
        fields = ("label", "tool", "roles", "weekly_left_percent", "resets_at", "credits", "use_credits", "read_at")
        self.assertEqual([tuple(row[field] for field in fields) for row in rows], expected)
        self.assertEqual([row["read_ok"] for row in rows], [False, True, True, False, True, False, True])
        self.assertEqual([row["credits_in_use"] for row in rows], [False, False, False, False, True, False, False])
        notes = table.split("\n\n", 1)[1].strip().splitlines()
        self.assertIn("accounts.py setup codex2", "\n".join(notes))
        self.assertEqual(document["problems"], [note.removeprefix("* ") for note in notes])
        self.assertTrue(any(note.startswith("* ") for note in notes))
        for row in rows:
            self.assertNotIn("login", row)
            self.assertNotIn("@", json.dumps(row))
            self.assertNotIn(str(self.home), json.dumps(row))
            self.assertNotIn("~/.", json.dumps(row))

        _ = accounts.groups_file().write_text('{"showrunners": "claude3", "units": "claude3"}')
        code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, error), (0, ""))
        changed = cast(list[dict[str, object]], json.loads(output)["accounts"])
        self.assertEqual([row["roles"] for row in changed[:4]], [[], [], ["units", "showrunners"], []])

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_json_and_table_refresh_the_same_stale_logins_once_each(self) -> None:
        self.login_directory("claude2", "stale@example.invalid")
        self.login_directory("claude3", "fresh@example.invalid")
        self.write_note("codex1", "codex@example.invalid")
        codex_home = self.home / ".codex1"
        self.codex_login("codex@example.invalid", codex_home)
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=3)
        stale = self.reading("stale@example.invalid", None, agent_accounts.Quota("Weekly", 90, reset))
        codex = agent_accounts.Report("Codex", email="codex@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 70, reset)], credit_balance="9")
        usage_store.save([stale, codex], now - timedelta(hours=2))
        usage_store.save([self.reading("fresh@example.invalid", None, agent_accounts.Quota("Weekly", 20, reset))], now)
        original = usage_store.store_file().read_bytes()
        self.live[accounts.account_dir("claude2")] = stale
        self.codex_reads.return_value = codex
        for flags in ((), ("--json",)):
            with self.subTest(flags=flags):
                _ = usage_store.store_file().write_bytes(original)
                self.live_reads.reset_mock()
                self.codex_reads.reset_mock()
                code, _, error = self.run_cli("status", *flags)
                self.assertEqual((code, error), (0, ""))
                self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))
                self.codex_reads.assert_awaited_once_with(codex_home)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_stored_status_shows_old_readings_without_live_reads_subprocesses_or_network(self) -> None:
        self.login_directory("claude2", "old@example.invalid")
        self.write_note("codex1", "old-codex@example.invalid")
        self.codex_login("old-codex@example.invalid", self.home / ".codex1")
        read = datetime(2001, 1, 2, 3, 4, tzinfo=timezone.utc)
        reset = datetime(2040, 1, 1, tzinfo=timezone.utc)
        usage_store.save([
            self.reading("old@example.invalid", None, agent_accounts.Quota("Weekly", 35, reset)),
            agent_accounts.Report("Codex", email="old-codex@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 75, reset)], credit_balance="12"),
        ], read)
        before = usage_store.store_file().read_bytes()
        with (patch.object(agent_accounts, "claude_live", side_effect=AssertionError("stored Claude live read")),
              patch.object(accounts, "_codex_live", side_effect=AssertionError("stored Codex live read")),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored ssh")),
              patch.object(subprocess, "run", side_effect=AssertionError("stored subprocess")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("stored Popen"))):
            code, output, error = self.run_cli("status", "--json", "--stored")
            self.assertEqual((code, error), (0, ""))
            rows = cast(list[dict[str, object]], json.loads(output)["accounts"])
            self.assertEqual([row["weekly_left_percent"] for row in rows], [65, 25])
            self.assertEqual([row["read_at"] for row in rows], [read.isoformat(), read.isoformat()])
            code, table, error = self.run_cli("status", "--stored")
            self.assertEqual((code, error), (0, ""))
        self.assertIn("65%", table)
        self.assertIn("25%", table)
        self.assertIn(read.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M %Z"), table)
        self.assertEqual(usage_store.store_file().read_bytes(), before)

    def test_status_rejects_json_detail_and_the_removed_login_option(self) -> None:
        for flags in (("--json", "--detail"), ("--with-login",), ("--json", "--with-login")):
            with (self.subTest(flags=flags), redirect_stdout(io.StringIO()) as output,
                  redirect_stderr(io.StringIO()) as error):
                with self.assertRaises(SystemExit) as refused:
                    _ = accounts.main(["status", *flags])
            self.assertEqual(refused.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("error:", error.getvalue())
            if "--with-login" in flags:
                self.assertIn("unrecognized arguments: --with-login", error.getvalue())

    def test_codex_setup_requires_its_note_before_creating_a_home(self) -> None:
        result, stdout, stderr = self.run_cli("setup", "codex1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex1", stderr)
        self.assertIn("note", stderr.lower())
        self.assertEqual(stdout, "")
        self.assertFalse((self.home / ".codex1").exists())
        self.codex_reads.assert_not_awaited()

    def test_codex_setup_refuses_a_login_already_held_by_the_default_home(self) -> None:
        self.write_note("codex2", "workers@example.invalid")
        self.codex_login("WORKERS@example.invalid")
        default = self.home / ".codex"
        authentication = (default / "auth.json").read_bytes()
        entries = set(self.home.iterdir())

        problems = accounts.setup("codex2")

        self.assertEqual(len(problems), 1)
        self.assertIn("codex2", problems[0])
        self.assertIn("~/.codex", problems[0])
        self.assertIn("two homes on one login break token refresh", problems[0])
        self.assertEqual(self.run_cli("setup", "codex2"), (1, "", problems[0] + "\n"))
        self.assertFalse((self.home / ".codex2").exists())
        self.assertEqual(set(self.home.iterdir()), entries)
        self.assertEqual((default / "auth.json").read_bytes(), authentication)
        self.codex_reads.assert_not_awaited()

    def test_codex_setup_duplicate_login_refusal_preserves_an_existing_home(self) -> None:
        self.write_note("codex2", "workers@example.invalid")
        self.codex_login("workers@example.invalid")
        default = self.home / ".codex"
        _ = (default / "config.toml").write_text("shared config")
        home = self.home / ".codex2"
        home.mkdir(mode=0o755)
        _ = (home / "config.toml").write_text("own config")
        before = home.stat().st_mode

        result, stdout, stderr = self.run_cli("setup", "codex2")

        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("~/.codex already holds", stderr)
        self.assertEqual(home.stat().st_mode, before)
        self.assertEqual({entry.name for entry in home.iterdir()}, {"config.toml"})
        self.assertFalse((home / "config.toml").is_symlink())
        self.assertEqual((home / "config.toml").read_text(), "own config")
        self.assertEqual((default / "config.toml").read_text(), "shared config")

    def test_codex_setup_refuses_a_login_held_by_a_numbered_or_custom_home(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        for name in (".codex3", "custom-codex"):
            with self.subTest(home=name):
                other = self.home / name
                self.codex_login("spare@example.invalid", other)
                with patch.dict(os.environ, {"CODEX_HOME": str(other)}):
                    result, stdout, stderr = self.run_cli("setup", "codex1")
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn(f"~/{name} already holds", stderr)
                self.assertFalse((self.home / ".codex1").exists())
                (other / "auth.json").unlink()
                other.rmdir()

    def test_codex_setup_links_existing_shared_entries_and_preserves_default_identity(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        self.codex_login("workers@example.invalid")
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

        result, stdout, stderr = self.run_cli("setup", "codex1")

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
        self.write_note("codex2", "spare@example.invalid")
        default = self.home / ".codex"
        default.mkdir()
        _ = (default / "config.toml").write_text("config")
        self.assertEqual(self.run_cli("setup", "codex2")[0], 0)
        home = self.home / ".codex2"
        linked = home / "config.toml"
        before = linked.lstat()
        self.codex_login("spare@example.invalid", home)
        authentication = (home / "auth.json").read_bytes()

        self.assertEqual(self.run_cli("setup", "codex2")[0], 0)

        self.assertEqual(linked.lstat().st_ino, before.st_ino)
        self.assertEqual(linked.lstat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((home / "auth.json").read_bytes(), authentication)
        folders = {"skills", "rules", "plugins", "sessions", "archived_sessions"}
        self.assertEqual({entry.name for entry in home.iterdir()}, {"config.toml", "auth.json", *folders})
        self.assertEqual({entry.name for entry in default.iterdir()}, {"config.toml", *folders})

    def test_codex_setup_refuses_an_occupied_real_file_but_merges_a_disjoint_folder(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
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

        result, stdout, stderr = self.run_cli("setup", "codex1")

        self.assertNotEqual(result, 0)
        self.assertIn("config.toml", stderr)
        self.assertNotIn(f"{home / 'skills'} is a real file", stderr)
        self.assertIn("merged", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual((home / "config.toml").read_text(), "own config")
        self.assertTrue((home / "skills").is_symlink())
        self.assertEqual((home / "skills" / "own").read_text(), "own skills")
        self.assertEqual((home / "hooks.json").resolve(), default / "hooks.json")

    def test_codex_check_accepts_own_state_files_and_matching_login(self) -> None:
        self.write_note("codex1", "SPARE@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex1")
        home = self.home / ".codex1"
        for stem in ("state_5", "thread_history_1", "goals_1", "logs_2", "queue_1", "memories_1"):
            for suffix in (".sqlite", ".sqlite-wal", ".sqlite-shm"):
                _ = (home / f"{stem}{suffix}").write_text("own database")
        for name in ("app-server-control", "app-server-daemon", "cache", "tmp", "shell_snapshots", "packages"):
            (home / name).mkdir()
        for name in ("installation_id", "version.json"):
            _ = (home / name).write_text("own state")

        self.assertEqual(self.run_cli("check", "codex1"), (0, "", ""))
        self.codex_reads.assert_not_awaited()

    def test_codex_check_reports_links_and_login_mismatch_but_not_unknown_entries(self) -> None:
        self.write_note("codex1", "expected@example.invalid")
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

        result, stdout, stderr = self.run_cli("check", "codex1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        for detail in ("config.toml", "hooks.json", "AGENTS.md", "other@example.invalid", "codex1"):
            self.assertIn(detail, stdout)
        self.assertNotIn("unknown-entry", stdout)
        self.assertEqual((home / "hooks.json").read_text(), "replacement")
        self.codex_reads.assert_not_awaited()

    def test_codex_check_reports_one_login_in_default_and_another_numbered_home(self) -> None:
        self.write_note("codex1", "same@example.invalid")
        self.write_note("codex2", "same@example.invalid")
        for name in (".codex", ".codex1", ".codex2"):
            self.codex_login("SAME@example.invalid", self.home / name)

        result, stdout, stderr = self.run_cli("check", "codex1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("~/.codex", stdout)
        self.assertIn("~/.codex2", stdout)
        self.assertIn("same@example.invalid", stdout.lower())

    def test_codex_check_reports_missing_home_and_logged_out_home(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        result, stdout, stderr = self.run_cli("check", "codex1")
        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("~/.codex1", stdout)
        self.assertFalse((self.home / ".codex1").exists())
        (self.home / ".codex1").mkdir(mode=0o700)

        result, stdout, stderr = self.run_cli("check", "codex1")

        self.assertNotEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("not logged in", stdout.lower())

    def test_status_has_one_codex_row_per_home_then_only_unheld_missing_notes(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        for label, login in (("codex1", "main-codex@example.invalid"), ("codex2", "spare@example.invalid"),
                             ("codex3", "missing@example.invalid"), ("codex4", "unheld@example.invalid")):
            self.write_note(label, login)
        self.codex_login("MAIN-CODEX@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex2")
        self.codex_login("unknown@example.invalid", self.home / ".codex4")
        (self.home / ".codex10").mkdir()

        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"workers": "codex1"}')
        rows = self.status_rows()[1:]

        self.assertEqual([row[:3] for row in rows], [
            ("~/.codex", "codex1", "workers"),
            ("~/.codex2", "codex2", "spare"),
            ("~/.codex4", "~/.codex4", "spare"),
            ("~/.codex10", "~/.codex10", "spare"),
            ("~/.codex3", "codex3", "—"),
        ])

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_reports_a_not_set_up_codex_account_when_its_home_is_a_file(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        home = self.home / ".codex1"
        _ = home.write_text("obstruction")

        self.assertIn(("~/.codex1", "codex1", "—"),
                      [row[:3] for row in self.status_rows()])
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        problems = stdout.split("\n\n", 1)[1]
        self.assertIn("~/.codex1 is not a directory", problems)
        self.assertEqual(home.read_text(), "obstruction")
        self.codex_reads.assert_not_awaited()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_reports_a_not_set_up_codex_account_when_its_home_is_a_dangling_link(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        home = self.home / ".codex1"
        target = self.root / "missing-home"
        home.symlink_to(target)

        self.assertIn(("~/.codex1", "codex1", "—"),
                      [row[:3] for row in self.status_rows()])
        result, stdout, stderr = self.run_cli("status")
        self.assertEqual((result, stderr), (0, ""))
        problems = stdout.split("\n\n", 1)[1]
        self.assertIn("~/.codex1 is a link", problems)
        self.assertEqual(os.readlink(home), str(target))
        self.assertFalse(target.exists())
        self.codex_reads.assert_not_awaited()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_logged_out_numbered_home_names_the_account_to_sign_in(self) -> None:
        self.write_note("codex1", "spare@example.invalid")
        (self.home / ".codex1").mkdir(mode=0o700)
        rows = self.status_rows()
        row = next(row for row in rows if row[0] == "~/.codex1")
        self.assertEqual(row[:3], ("~/.codex1", "~/.codex1", "spare"))
        report = self.run_cli("status")[1]
        self.assertIn("codex1", report.split("\n\n", 1)[1])
        self.assertIn("sign in", report.split("\n\n", 1)[1].lower())
        self.assertEqual(accounts.codex_home_label(self.home / ".codex1"), "~/.codex1")

    def test_status_assigns_workers_to_the_selected_codex_home(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("codex1", "spare@example.invalid")
        self.codex_login("default@example.invalid")
        selected = self.home / ".codex1"
        self.codex_login("spare@example.invalid", selected)
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        with patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
            rows = self.status_rows()[1:]
        self.assertEqual([row[2] for row in rows], ["spare", "workers"])
        self.assertEqual([row[0] for row in rows], ["~/.codex", "~/.codex1"])

    def test_status_reads_each_stale_codex_home_and_saves_both_readings(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("codex2", "spare@example.invalid")
        default = self.home / ".codex"
        spare = self.home / ".codex2"
        self.codex_login("workers@example.invalid", default)
        self.codex_login("spare@example.invalid", spare)
        later = datetime.now(timezone.utc) + timedelta(days=3)
        reports = {
            default: agent_accounts.Report("Codex", email="workers@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 20, later)], credit_balance="7"),
            spare: agent_accounts.Report("Codex", email="spare@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 95, later)], credit_balance="11"),
        }
        self.codex_reads.side_effect = reports.__getitem__

        first = self.status_rows()[1:]
        second = self.status_rows()[1:]

        self.assertEqual([row[3] for row in first], ["80%", "5%"])
        self.assertEqual([row[6] for row in first], ["7", "11"])
        self.assertEqual(first, second)
        self.assertCountEqual(self.codex_reads.await_args_list, [call(default), call(spare)])
        stored = usage_store.load()
        workers_usage = stored[usage_store.key("Codex", "workers@example.invalid")]
        spare_usage = stored[usage_store.key("Codex", "spare@example.invalid")]
        if "weekly" not in workers_usage or "weekly" not in spare_usage:
            self.fail("Both signed-in homes must save their weekly readings")
        self.assertEqual(workers_usage["weekly"]["left"], 80)
        self.assertEqual(spare_usage["weekly"]["left"], 5)

    def test_status_reads_only_the_codex_home_with_an_old_reading(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("codex1", "spare@example.invalid")
        default = self.home / ".codex"
        spare = self.home / ".codex1"
        self.codex_login("workers@example.invalid", default)
        self.codex_login("spare@example.invalid", spare)
        now = datetime.now(timezone.utc)
        later = now + timedelta(days=3)
        fresh = agent_accounts.Report("Codex", email="workers@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 20, later)], credit_balance="7")
        old = agent_accounts.Report("Codex", email="spare@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 70, later)], credit_balance="11")
        usage_store.save([old], now - timedelta(minutes=11))
        usage_store.save([fresh], now - timedelta(minutes=1))
        self.codex_reads.return_value = agent_accounts.Report("Codex", email="spare@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 95, later)], credit_balance="13")

        rows = self.status_rows()[1:]

        self.codex_reads.assert_awaited_once_with(spare)
        self.assertEqual([row[3] for row in rows], ["80%", "5%"])
        self.assertEqual([row[6] for row in rows], ["7", "13"])

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_detail_prints_codex_check_problems_below_the_table(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_note("codex1", "spare@example.invalid")
        self.codex_login("spare@example.invalid", self.home / ".codex1")
        self.assertNotIn("unclassified entries", self.run_cli("status")[1])
        _ = (self.home / ".codex1" / "unclassified-item").write_text("own")

        result, stdout, stderr = self.run_cli("--detail")

        self.assertEqual((result, stderr), (0, ""))
        table, problems = stdout.split("\n\n", 1)
        self.assertIn("~/.codex1", table)
        self.assertNotIn("unclassified-item", table)
        self.assertIn("~/.codex1", problems)
        self.assertIn("unclassified-item", problems)

    def test_status_keeps_the_last_good_reading_when_the_service_refuses_a_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        now = datetime.now(timezone.utc)
        read = now - timedelta(minutes=45)
        usage_store.save([self.reading(
            "main@example.invalid",
            # This 5-hour window has ended since it was read.
            agent_accounts.Quota("5-hour", 80.0, now - timedelta(minutes=5)),
            agent_accounts.Quota("Weekly", 25.0, now + timedelta(days=2)),
        )], read)
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report(
            "Claude", email="main@example.invalid",
            quota_problem="Weekly quota: unavailable (HTTP 429; try Claude /usage)",
        )

        rows = self.status_rows()

        self.assertEqual(rows[0][3], "75%")
        self.assertEqual(rows[0][7], failed_read_cell(read))
        # claude3 has no good reading. Its failed attempt still gets a star.
        self.assertEqual(rows[1][2:], ("", *("unavailable",) * 2, "—", "—", "—*"))
        self.assertEqual(self.live_reads.call_count, 2)

    def test_login_renewal_uses_a_quiet_probe_and_keeps_the_new_reading(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude",
            email="idle@example.invalid", usage_read_outcome=agent_accounts.UsageReadOutcome.OK,
            quotas=[agent_accounts.Quota("Weekly", 10, datetime.now(timezone.utc) + timedelta(days=1))])
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"private", b"private")) as probe:
            self.assertEqual(accounts.renew_login("claude2"), accounts.LoginRenewed())
        probe.assert_called_once_with([sys.executable, str(accounts.SCRIPTS / "accounts" / "accounts.py"),
            "exec", "--probe", "claude2", "-p", "hello"], cwd="/tmp", stdin=subprocess.DEVNULL,
            timeout=120, capture_output=True)
        self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))
        self.assertEqual(usage_store.latest_read_outcome(usage_store.load()["idle@example.invalid"]),
                         agent_accounts.UsageReadOutcome.OK)

    def test_login_renewal_fails_without_exposing_probe_output(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        failures = (
            (subprocess.TimeoutExpired("probe", 120, output=b"private"), "timed out after 120 s"),
            (OSError("private"), "probe could not start"),
        )
        for error, detail in failures:
            with self.subTest(detail=detail), patch.object(subprocess, "run", side_effect=error):
                self.assertEqual(accounts.renew_login("claude2"), accounts.LoginRenewalFailed(detail))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 7, b"private", b"private")):
            self.assertEqual(accounts.renew_login("claude2"), accounts.LoginRenewalFailed("probe exited with status 7"))
        self.live_reads.assert_not_called()
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude",
            email="idle@example.invalid", usage_read_outcome=agent_accounts.UsageReadOutcome.STALE_TOKEN)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(accounts.renew_login("claude2"),
                             accounts.LoginRenewalFailed("usage still shows an expired login"))

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_damaged_login_renewal_state_reads_empty_and_status_renders(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.STALE_TOKEN)], datetime.now(timezone.utc))
        path = accounts.login_renewals_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        for damaged in ('{"claude2":', '[]', '{"claude2": {}}',
                        '{"claude2": {"at": "bad", "outcome": "failed", "detail": "failed"}}',
                        '{"claude2": {"at": "2026-10-22T12:00:00", "outcome": "failed", "detail": "failed"}}'):
            with self.subTest(damaged=damaged):
                _ = path.write_text(damaged)
                self.assertEqual(accounts.login_renewal_attempts(), {})
                for arguments in (("status", "--stored"), ("status", "--stored", "--json")):
                    code, output, error = self.run_cli(*arguments)
                    self.assertEqual((code, error), (0, ""))
                    self.assertIn("login token expired; the next notes pass renews it", output)
        self.live_reads.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_expired_login_note_names_the_failed_probe_in_table_and_json(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        at = datetime(2026, 10, 22, 15, 11, tzinfo=timezone.utc)
        usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.STALE_TOKEN)], at)
        path = accounts.login_renewals_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"claude2": {"at": at.isoformat(), "outcome": "failed", "detail": "probe exited with status 7"}}))
        note = "claude2: usage unavailable; login token expired; a probe session at 08:11 failed; log in on it by hand"
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("* " + note, output)
        code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(note, cast(list[str], cast(dict[str, object], json.loads(output))["problems"]))
        self.live_reads.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failed_read_causes_show_old_date_star_and_one_footnote_until_a_good_read(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        cases = (
            (agent_accounts.UsageReadOutcome.STALE_TOKEN,
             "login token expired; the next notes pass renews it"),
            (agent_accounts.UsageReadOutcome.REFUSED_401,
             "the usage service did not accept this login; start a session on it to check it"),
            (agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
             "the usage service is busy; this shows the reading from 05:29; next try in 2 min"),
            (agent_accounts.UsageReadOutcome.ERROR,
             "the usage read failed; this shows the reading from 05:29; next try in 2 min"),
        )
        for outcome, reason in cases:
            with self.subTest(outcome=outcome):
                usage_store.save([self.reading("idle@example.invalid", None,
                    agent_accounts.Quota("Weekly", 25, old + timedelta(days=1)))], old)
                usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
                    usage_read_outcome=outcome)], old + timedelta(minutes=2))
                code, output, error = self.run_cli("status", "--stored")
                self.assertEqual((code, error), (0, ""))
                row = next(line for line in output.splitlines() if "| claude2 " in line)
                self.assertEqual(row.split("|")[-2].strip(), "01-02 05:29 PST*")
                self.assertEqual([line for line in output.splitlines() if line.startswith("* ")],
                                 [f"* claude2: usage unavailable; {reason}"])
                now = datetime.now(timezone.utc)
                usage_store.save([self.reading("idle@example.invalid", None,
                    agent_accounts.Quota("Weekly", 10, now + timedelta(days=1)))], now)
                code, output, error = self.run_cli("status", "--stored")
                self.assertEqual((code, error), (0, ""))
                row = next(line for line in output.splitlines() if "| claude2 " in line)
                self.assertEqual(row.split("|")[-2].strip(),
                                 now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M %Z"))
                self.assertFalse(any(line.startswith("* ") for line in output.splitlines()))
        self.live_reads.assert_not_called()
        self.codex_reads.assert_not_awaited()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_usage_failure_notes_name_the_service_and_retry_time_in_table_and_json(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        at = datetime(2026, 10, 19, 15, 11, tzinfo=timezone.utc)
        cases = (
            (agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
             "the usage service is busy; this shows the reading from 08:09; next try after 08:21",
             "the usage service is busy; no reading is stored yet; next try after 08:21"),
            (agent_accounts.UsageReadOutcome.ERROR,
             "the usage read failed; this shows the reading from 08:09; next try in 2 min",
             "the usage read failed; no reading is stored yet; next try in 2 min"),
            (agent_accounts.UsageReadOutcome.REFUSED_401,
             "the usage service did not accept this login; start a session on it to check it",
             "the usage service did not accept this login; start a session on it to check it"),
            (agent_accounts.UsageReadOutcome.STALE_TOKEN,
             "login token expired; the next notes pass renews it",
             "login token expired; the next notes pass renews it"),
        )
        for outcome, with_reading, without_reading in cases:
            for has_reading, reason in ((True, with_reading), (False, without_reading)):
                with self.subTest(outcome=outcome, reading=has_reading):
                    usage = usage_store.StoredUsage(read_outcome=outcome, attempted_at=at.isoformat(),
                        attempt_outcome=outcome)
                    if has_reading:
                        usage["weekly"] = usage_store.StoredWindow(left=75,
                            at=(at - timedelta(minutes=2)).isoformat(),
                            resets=(at + timedelta(days=1)).isoformat())
                        usage["five_hour"] = usage["weekly"]
                    with usage_store.read_lock():
                        usage_store.write({"idle@example.invalid": usage})
                    code, output, error = self.run_cli("status", "--stored")
                    self.assertEqual((code, error), (0, ""))
                    note = f"* claude2: usage unavailable; {reason}"
                    self.assertEqual([line for line in output.splitlines() if line.startswith("* ")], [note])
                    code, document, error = self.run_cli("status", "--json", "--stored")
                    self.assertEqual((code, error), (0, ""))
                    problems = cast(dict[str, object], json.loads(document))["problems"]
                    self.assertIn(note.removeprefix("* "), cast(list[str], problems))
                    self.assertFalse(any(problem.startswith("* ") for problem in cast(list[str], problems)))
                    self.assertNotIn("refused", note)
        self.live_reads.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_a_held_refresh_keeps_the_stored_reading_and_failure_note(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        at = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([self.reading("idle@example.invalid", None,
            agent_accounts.Quota("Weekly", 25, at + timedelta(days=1)))], at)
        usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429)], at)
        before = usage_store.store_file().read_bytes()
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude",
            email="idle@example.invalid", usage_read_outcome=agent_accounts.UsageReadOutcome.HELD)
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("01-02 05:29 PST*", output)
        self.assertIn("the usage service is busy; this shows the reading from 05:29; next try in 2 min", output)
        self.assertEqual(usage_store.store_file().read_bytes(), before)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_first_identity_failure_after_upgrade_marks_the_old_reading(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        path = usage_store.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        window = {"left": 75, "resets": (old + timedelta(days=1)).isoformat(), "at": old.isoformat()}
        _ = path.write_text(json.dumps({"idle@example.invalid": {"weekly": window}}))
        directory = accounts.account_dir("claude2")
        self.live[directory] = agent_accounts.Report("Claude", problem="identity unavailable")
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| claude2 " in line)
        self.assertEqual(row.split("|")[-2].strip(), "01-02 05:29 PST*")
        self.assertEqual([line for line in output.splitlines() if line.startswith("* ")],
                         ["* claude2: usage unavailable; the usage read failed; this shows the reading from 05:29; next try in 2 min"])
        self.assertEqual(usage_store.load()["idle@example.invalid"].get("weekly"), window)
        self.live_reads.assert_called_once_with(directory)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failure_from_the_first_of_two_directories_marks_their_shared_login(self) -> None:
        login = "shared@example.invalid"
        self.login_directory("claude2", login)
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        first, second = self.home / ".claude2", self.home / ".claude3"
        for directory in (first, second):
            usage_store.save([agent_accounts.Report("Claude", email=login,
                source=agent_accounts.ClaudeDirectorySource(directory),
                quotas=[agent_accounts.Quota("Weekly", 25, old + timedelta(days=1))])], old)
        # The failure has no identity hint. Only the persisted directory link can name its login.
        usage_store.save([agent_accounts.Report("Claude",
            source=agent_accounts.ClaudeDirectorySource(first), problem="identity unavailable")],
            old + timedelta(minutes=2))
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| claude2 " in line)
        self.assertEqual(row.split("|")[-2].strip(), "01-02 05:29 PST*")
        self.assertEqual([line for line in output.splitlines() if line.startswith("* ")],
                         ["* claude2: usage unavailable; the usage read failed; this shows the reading from 05:29; next try in 2 min"])
        self.live_reads.assert_not_called()

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failed_read_today_keeps_the_short_time_and_marks_a_never_read_account(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        self.login_directory("claude3", "never@example.invalid")
        now = datetime.now(timezone.utc)
        usage_store.save([self.reading("idle@example.invalid", None,
            agent_accounts.Quota("Weekly", 25, now + timedelta(days=1)))], now)
        usage_store.save([agent_accounts.Report("Claude", email=login,
            usage_read_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429)
            for login in ("idle@example.invalid", "never@example.invalid")], now)
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        rows = [line for line in output.splitlines() if "| claude2 " in line or "| claude3 " in line]
        self.assertEqual([line.split("|")[-2].strip() for line in rows],
            [now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M %Z") + "*", "—*"])
        self.assertEqual(len([line for line in output.splitlines() if line.startswith("* ")]), 2)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failed_codex_start_keeps_its_last_good_read_time_and_shows_a_footnote(self) -> None:
        _, target = self.workers_fixture()
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([agent_accounts.Report("Codex", email="target@example.invalid",
            source=agent_accounts.CodexHomeSource(target),
            quotas=[agent_accounts.Quota("Weekly", 25, old + timedelta(days=1))])], old)
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Startup request timed out",
            source=agent_accounts.CodexHomeSource(target),
            codex_startup=agent_accounts.CodexStartupFailure(target, "/showrunner:accounts setup codex1"))
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| codex1 " in line)
        self.assertEqual(row.split("|")[-2].strip(), "01-02 05:29 PST*")
        self.assertIn("* codex1: usage unavailable; Codex did not start at ~/.codex1; this shows the reading from 05:29; run /showrunner:accounts setup codex1 and try again; next try in 2 min", output)
        self.assertIn("/showrunner:accounts setup codex1", row)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_after_weekly_reset_explains_a_stale_login_without_reading_it_again(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        read = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([self.reading("idle@example.invalid", None,
            agent_accounts.Quota("Weekly", 90, read + timedelta(days=1)))], read)
        usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
            quota_problem="wording is deliberately unrelated to the skip reason",
            weekly_read_skip=agent_accounts.StaleTokenSkip())], read + timedelta(days=2))
        before = usage_store.store_file().read_bytes()
        with (patch.object(subprocess, "run", side_effect=AssertionError("display launched a process")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("display launched Claude"))):
            code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| claude2 " in line)
        self.assertEqual(row.split("|")[4].strip(),
                         "not read since 05:29: no session to refresh its login")
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        self.live_reads.assert_not_called()
        self.codex_reads.assert_not_awaited()

    def test_status_after_weekly_reset_with_another_read_failure_shows_only_last_read_time(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        read = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([self.reading("idle@example.invalid", None,
            agent_accounts.Quota("Weekly", 90, read + timedelta(days=1)))], read)
        # Text alone must never classify a failure as a stale-token skip.
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude", email="idle@example.invalid",
            quota_problem="Weekly quota: not read (stale token; a Claude session on this account refreshes it)")
        row = self.status_rows()[0]
        self.assertEqual(row[3], "not read since 05:29")
        self.assertEqual(row[4], "unavailable")
        self.assertEqual(row[7], "01-02 05:29 PST*")
        self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))

    def test_status_auth_failure_after_stale_login_skip_clears_its_reason_and_keeps_last_good_reading(self) -> None:
        login = "idle@example.invalid"
        self.login_directory("claude2", login)
        directory = accounts.account_dir("claude2")
        read = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([self.reading(login, None,
            agent_accounts.Quota("Weekly", 90, read + timedelta(days=1)))], read)
        before = usage_store.load()[login].copy()
        credentials = directory / ".credentials.json"
        _ = credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "fixture-stale", "expiresAt": 1}}))
        credential_bytes = credentials.read_bytes()
        with (patch.object(agent_accounts, "claude_live", side_effect=self.read_claude_live) as reads,
              patch.object(agent_accounts, "command_json", side_effect=[
                  {"loggedIn": True, "email": login, "authMethod": "claude.ai"},
                  RuntimeError("fixture auth failed")]) as auth,
              patch.object(subprocess, "run", side_effect=AssertionError("read launched a process")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("token refresh launched")),
              patch.object(urllib.request, "urlopen",
                  side_effect=AssertionError("stale token used for a read"))):
            skipped = self.status_rows()[0]
            self.assertEqual(skipped[3], "not read since 05:29: no session to refresh its login")
            self.assertIsInstance(usage_store.weekly_read_skip(usage_store.load()[login]),
                                  agent_accounts.StaleTokenSkip)
            failed = self.status_rows()[0]
        self.assertEqual(failed[3], "not read since 05:29")
        self.assertEqual((failed[4], failed[7]), ("unavailable", "01-02 05:29 PST*"))
        held = usage_store.load()[login]
        self.assertEqual(held.get("weekly"), before.get("weekly"))
        self.assertEqual(held.get("five_hour"), before.get("five_hour"))
        self.assertEqual(usage_store.latest_read_outcome(held), agent_accounts.UsageReadOutcome.ERROR)
        self.assertEqual(credentials.read_bytes(), credential_bytes)
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(reads.call_args_list, [call(directory), call(directory)])
        self.codex_reads.assert_not_awaited()

    def test_status_stale_token_skip_keeps_a_current_weekly_window_unchanged(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        read = datetime.now(timezone.utc) - timedelta(minutes=45)
        reset = read + timedelta(days=2)
        usage_store.save([self.reading("idle@example.invalid", None,
            agent_accounts.Quota("Weekly", 25, reset))], read)
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude", email="idle@example.invalid",
            weekly_read_skip=agent_accounts.StaleTokenSkip())
        row = self.status_rows()[0]
        self.assertEqual(row[3], "75%")
        self.assertEqual(row[4], reset.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %m-%d %H:%M %Z"))
        self.assertEqual(row[7], failed_read_cell(read))
        self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))

    def test_status_stale_token_skip_without_any_stored_reading_stays_unavailable(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude", email="idle@example.invalid",
            weekly_read_skip=agent_accounts.StaleTokenSkip())
        row = self.status_rows()[0]
        self.assertEqual((row[3], row[4], row[7]), ("unavailable", "unavailable", "—*"))
        self.live_reads.assert_called_once_with(accounts.account_dir("claude2"))

    def test_status_times_use_the_users_zone_setting_when_no_production_runs_or_none_can_be_read(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)
        resets = datetime.now(timezone.utc) + timedelta(days=3)
        usage_store.save([self.reading(
            "main@example.invalid", agent_accounts.Quota("5-hour", 50.0, resets), agent_accounts.Quota("Weekly", 50.0, resets),
        )], datetime.now(timezone.utc))
        setting = resets.astimezone(ZoneInfo("Europe/Paris")).strftime("%a %m-%d %H:%M %Z")

        with patch.dict(os.environ, {"USER_ZONE": "Europe/Paris"}):
            with patch.object(showrunners, "registered_showrunners", return_value=[]):
                self.assertEqual(self.status_rows()[0][4], setting)
            with patch.object(showrunners, "registered_showrunners", side_effect=OSError("session records unreadable")):
                self.assertEqual(self.status_rows()[0][4], setting)
        self.assertEqual(
            self.status_rows()[0][4],
            resets.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %m-%d %H:%M %Z"),
        )

    def test_table_times_use_the_shared_user_zone_rule(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        resets = datetime.now(timezone.utc) + timedelta(days=3)
        usage_store.save([self.reading("main@example.invalid",
            agent_accounts.Quota("5-hour", 50, resets), agent_accounts.Quota("Weekly", 50, resets),
        )], datetime.now(timezone.utc))
        zone = ZoneInfo("Asia/Tokyo")
        with patch.object(agent_accounts, "user_zone", return_value=zone) as resolve:
            self.assertEqual(self.status_rows()[0][4], resets.astimezone(zone).strftime("%a %m-%d %H:%M %Z"))
        resolve.assert_called_once_with()


    def test_account_prefix_keeps_the_selection_after_the_role_changes(self) -> None:
        """Rendering a selected account never resolves the role again."""
        self.write_main_config()
        self.login_directory("claude3", "old@example.invalid")
        self.login_directory("claude1", "new@example.invalid")
        selected = accounts.NamedAccount("claude3", accounts.account_dir("claude3"))
        self.seed_room("old@example.invalid", 7)
        self.seed_room("new@example.invalid", 2)
        self.select_named_unit("claude1")

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

        self.assertEqual(accounts.account_prefix(accounts.NamedAccount("claude2", accounts.account_dir("claude2"))), named_prefix(accounts.account_dir("claude2")))

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
            ("set-role", "unknown", "claude2"),
            ("swap", "unknown", "claude2"),
        ):
            with self.subTest(arguments=arguments):
                result, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    "accounts: unknown is not a role; only unit, showrunner and workers are supported\n",
                )








    def workers_fixture(self) -> tuple[Path, Path]:
        default = self.home / ".codex"
        target = self.home / ".codex1"
        self.write_note("codex1", "target@example.invalid")
        self.write_note("codex2", "default@example.invalid")
        self.codex_login("default@example.invalid", default)
        self.codex_login("target@example.invalid", target)
        now = datetime.now(timezone.utc)
        self.codex_reads.return_value = agent_accounts.Report("Codex", email="target@example.invalid",
            source=agent_accounts.CodexHomeSource(target), five_hour=agent_accounts.Quota("5-hour", 20, now + timedelta(hours=3)),
            quotas=[agent_accounts.Quota("Weekly", 30, now + timedelta(days=3))], credit_balance="0")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"workers": "codex2"}\n')
        return default, target

    def test_workers_swap_records_the_callers_auto_reason_and_cause(self) -> None:
        _ = self.workers_fixture()
        with patch.object(accounts, "_retire_workers_servers", return_value=[]):
            _ = accounts.swap("workers", "codex1", why="auto: workers-entry", cause="auto")
        rows = [cast(dict[str, object], json.loads(line))
                for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual((rows[-1]["role"], rows[-1]["cause"], rows[-1]["why"]),
                         ("workers", "auto", "auto: workers-entry"))

    def test_unset_workers_role_refuses_independently_of_the_callers_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            message = "accounts: workers has no mapped account; run accounts.py set-role workers <codexN>"
            self.assertEqual(self.run_cli("role", "workers"), (1, "", message + "\n"))
            with self.assertRaises(ValueError) as refused:
                _ = agent_accounts.workers_home()
            self.assertEqual(str(refused.exception), message)
        self.assertEqual(self.role_files(), {})

    def test_old_selected_worker_account_is_shared_by_accounts_and_whoami_until_normal_write(self) -> None:
        _, target = self.workers_fixture()
        path = accounts.accounts_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"seats": "codex1"}')
        original = path.read_bytes()
        for _ in range(2):
            self.assertEqual(agent_accounts.workers_home(), target)
            selected = accounts.role_account("workers")
            self.assertEqual((selected.label, selected.home), ("codex1", target))
            self.assertEqual(self.run_cli("role", "workers"), (0, "codex1\n", ""))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.run_cli("set-role", "workers", "codex2"), (0, "", ""))
        self.assertEqual(json.loads(path.read_text()), {"workers": "codex2"})

    def test_worker_swap_rejects_the_old_role_without_changing_selection(self) -> None:
        _ = self.workers_fixture()
        before = self.role_files()
        for verb in ("swap", "set-role", "role", "env"):
            arguments = (verb, "seats", "codex1") if verb in ("swap", "set-role") else (verb, "seats")
            code, stdout, stderr = self.run_cli(*arguments)
            self.assertNotEqual(code, 0)
            self.assertIn("not a role", stdout + stderr)
        self.assertEqual(self.role_files(), before)

    def test_workers_environment_overrides_the_caller_for_default_and_numbered_roles(self) -> None:
        default, target = self.workers_fixture()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            for label, home in (("codex1", target), ("codex2", default)):
                with self.subTest(label=label):
                    self.assertEqual(self.run_cli("set-role", "workers", label), (0, "", ""))
                    result, stdout, stderr = self.run_cli("env", "workers")
                    self.assertEqual((result, stderr), (0, ""))
                    self.assertEqual(shlex.split(stdout), [f"CODEX_HOME={home}"])

    def test_trust_command_default_and_named_accounts_quote_paths(self) -> None:
        folder = self.root / "folder ' with $spaces"
        self.assertEqual(accounts.trust_command(folder, accounts.NamedAccount("claude2", accounts.account_dir("claude2"))),
                         f"cd {shlex.quote(str(folder))} && {named_prefix(accounts.account_dir('claude2'))}claude")
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
            result = subprocess.run(["/bin/sh", "-c", accounts.trust_command(folder, accounts.NamedAccount("claude2", accounts.account_dir("claude2")))],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(cast(object, json.loads(result.stdout)),
                             named_environment(accounts.account_dir("claude2")))
            self.assertEqual({name: os.environ[name] for name in account.NAMED_ACCOUNT_VARIABLES}, inherited)

    def test_role_prefixes_use_the_explicit_claude_and_codex_selections(self) -> None:
        default, _ = self.workers_fixture()
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude2"}))
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.seed_room("main@example.invalid")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller")}):
            unit = accounts.role_account("unit")
            workers = accounts.role_account("workers")
            self.assertIsInstance(workers, accounts.CodexWorkersAccount)
            self.assertEqual((workers.home, workers.label), (default, "codex2"))
            self.assertEqual(accounts.account_prefix(unit), named_prefix(accounts.account_dir("claude2")))
            self.assertEqual(shlex.split(accounts.account_prefix(workers)), [f"CODEX_HOME={self.home / '.codex'}"])
            self.assertEqual(accounts.launch_prefix("workers"), accounts.account_prefix(workers))

    def test_workers_prefix_retains_its_home_when_the_role_changes(self) -> None:
        default, target = self.workers_fixture()
        before = accounts.role_account("workers")
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        selected = accounts.role_account("workers")
        self.assertEqual(self.run_cli("set-role", "workers", "codex2"), (0, "", ""))
        with patch.object(agent_accounts, "workers_home", side_effect=AssertionError("A prefix must keep its selection")):
            self.assertEqual(shlex.split(accounts.account_prefix(before)), [f"CODEX_HOME={default}"])
            self.assertEqual(shlex.split(accounts.account_prefix(selected)), [f"CODEX_HOME={target}"])
        self.assertEqual((selected.home, selected.label), (target, "codex1"))

    def test_unreadable_workers_role_becomes_an_accounts_error_at_resolution(self) -> None:
        for error in (ValueError("invalid workers account"), OSError("role file unreadable")):
            with self.subTest(error=error), patch.object(agent_accounts, "workers_home", side_effect=error):
                result, stdout, stderr = self.run_cli("env", "workers")
                self.assertEqual((result, stdout), (1, ""))
                self.assertIn(str(error), stderr)
                with self.assertRaises(accounts.AccountsError):
                    _ = accounts.role_account("workers")

    def test_refused_workers_selection_names_the_tool_once(self) -> None:
        (self.home / ".codex1").mkdir(exist_ok=True)
        result, stdout, stderr = self.run_cli("set-role", "workers", "codex1")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.startswith("accounts: cannot select codex1 (~/.codex1): "), stderr)
        self.assertEqual(stderr.count("accounts: "), 1, stderr)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unrelated_io_and_value_errors_propagate_from_the_command(self) -> None:
        for error in (OSError("table write failed"), ValueError("invalid table data")):
            with self.subTest(error=error), patch.object(accounts, "status_report", side_effect=error):
                with self.assertRaises(type(error)):
                    _ = self.run_cli("status")


    def test_workers_only_configuration_preserves_claude_selection(self) -> None:
        _ = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        before = accounts.claude_account_selection()
        for label in ("codex1", "codex2"):
            with self.subTest(label=label):
                self.assertEqual(self.run_cli("set-role", "workers", label), (0, "", ""))
                self.assertEqual(accounts.claude_account_selection(), before)
                self.assertTrue(accounts.multiple_accounts_configured())


    def test_failed_workers_history_write_restores_both_roles(self) -> None:
        _ = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.select_named_unit("claude3")
        before = self.role_files()
        open_file = os.open
        def open_without_history(path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                                 flags: int, mode: int = 0o777) -> int:
            if path == accounts.roles_history_file():
                raise OSError("disk full")
            return open_file(path, flags, mode)
        with patch.object(os, "open", side_effect=open_without_history):
            result, _, stderr = self.run_cli("set-role", "workers", "codex1")
        self.assertNotEqual(result, 0)
        self.assertIn("could not write workers", stderr)
        self.assertEqual(self.role_files(), before)

    def test_burn_skips_group_history_between_legacy_unit_and_workers_entries(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit",
            label="claude3", account_label="claude3")])
        with accounts.roles_history_file().open("a") as history:
            for entry in ({"at": "2026-10-09T00:00:00Z", "role": "workers", "label": "default"},
                          {"at": "2026-10-09T00:00:00Z", "group": "units", "label": "claude1", "why": "refused"},
                          {"at": "2026-10-09T00:00:00Z", "group": "showrunners", "label": "claude3", "why": "refused"}):
                _ = history.write(json.dumps(entry) + "\n")
        self.write_readings([accounts.QuotaReading(account="claude3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))])
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)),
            [accounts.AccountRoleBurn("claude3", ("unit directors",), 10, 1)])

    def test_unit_burn_ignores_workers_history_records(self) -> None:
        _ = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        with patch.object(accounts, "datetime", RoleWriteTime):
            self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude3", account_label="claude3")])
            self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
            self.assertEqual(self.run_cli("set-role", "workers", "codex2"), (0, "", ""))
        self.write_readings([accounts.QuotaReading(account="claude3", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))])
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)),
            [accounts.AccountRoleBurn("claude3", ("unit directors",), 10, 1)])

    def test_workers_swap_dry_run_names_target_and_preserves_roles_and_history(self) -> None:
        _, target = self.workers_fixture()
        before = self.role_files()
        with patch.object(codex_mesh, "retire_workers_servers", return_value=[]) as retire:
            result, stdout, stderr = self.run_cli("swap", "workers", "codex1", "--dry-run")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("codex1", stdout)
        self.assertEqual(stdout, "Workers would move codex2 → codex1:\n")
        self.assertEqual(self.role_files(), before)
        retire.assert_called_once_with(target, "codex1", dry_run=True)

    def test_workers_swap_names_each_switching_worker_and_its_production_for_both_modes(self) -> None:
        def holder(session: str, machine: str, unit: str, name: str) -> roster.RosterEntry:
            return roster.RosterEntry(
                machine=machine, kind="claude", role="unit director", name=name, status="running",
                reach=roster.Messageable(f"session:{session}"), cwd="",
                showrunner=roster.UnderShowrunner("build", "natedev", "session:showrunner"),
                director=roster.DirectsUnit(unit) if unit else roster.NoDirector(),
                assignment=roster.NotAWorker(), session=roster.ClaudeSession(session))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                _ = self.workers_fixture()
                switches = (
                    codex_mesh.WorkersRunSwitch(self.root / "private-run-a", ("director-a",), (
                        codex_mesh.SwitchingWorker("claude-build-two-accounts-impl", False),)),
                    codex_mesh.WorkersRunSwitch(self.root / "private-run-b", ("director-b",), (
                        codex_mesh.SwitchingWorker("claude-build-herdr-trial-review", True),)),
                    codex_mesh.WorkersRunSwitch(self.root / "private-run-c", ("freestanding",), (
                        codex_mesh.SwitchingWorker("project-test-worker", False),)),
                    codex_mesh.WorkersRunSwitch(self.root / "orphan-run", ("missing",), (
                        codex_mesh.SwitchingWorker("project-fix", False),)),
                )
                snapshot = roster.Roster([
                    holder("director-a", "here", "two-accounts", "director-a"),
                    holder("director-b", "macbook", "herdr-trial", "director-b"),
                    holder("freestanding", "here", "", "scratch session"),
                ], [])
                with (patch.object(codex_mesh, "retire_workers_servers", return_value=switches),
                      patch.object(roster, "roster", return_value=snapshot),
                      patch.object(roster, "machine_name", return_value="here")):
                    code, stdout, stderr = self.run_cli("swap", "workers", "codex1", *(["--dry-run"] if dry_run else []))
                self.assertEqual((code, stderr), (0, ""))
                header = "Workers would move" if dry_run else "Workers moving"
                self.assertEqual(stdout, f"{header} codex2 → codex1:\n" +
                    "  natedev · two-accounts · impl-worker → codex1\n" +
                    "  macbook · natedev · herdr-trial · review-worker → codex1, interrupted and resumed\n" +
                    "  natedev · scratch session · test-worker → codex1\n" +
                    "  unknown session (orphan-run) · fix-worker → codex1\n")
                self.assertNotIn("private-run", stdout)

    def test_workers_swap_reports_each_failed_worker_with_manual_resume_and_keeps_new_role(self) -> None:
        _, target = self.workers_fixture()
        run = self.root / "run with spaces"
        interrupt_command = f"python3 mesh.py resume-account-move --session-dir '{run}' --to project-impl"
        resume_command = f"python3 mesh.py resume-account-move --session-dir '{run}' --to project-review"
        switches = (codex_mesh.WorkersRunSwitch(run, (), (
            codex_mesh.SwitchingWorker("project-impl", True,
                codex_mesh.WorkerMoveFailed("interrupt failed: access denied", interrupt_command)),
            codex_mesh.SwitchingWorker("project-review", True,
                codex_mesh.WorkerMoveFailed("resume failed: server unavailable", resume_command)),
            codex_mesh.SwitchingWorker("project-test", False),
        )),)
        with (patch.object(codex_mesh, "retire_workers_servers", return_value=switches),
              patch.object(roster, "roster", return_value=roster.Roster([], []))):
            code, output, errors = self.run_cli("swap", "workers", "codex1")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(agent_accounts.workers_home(), target)
        lines = output.splitlines()
        self.assertEqual(len(lines), 4)
        for worker, reason, command in (("impl-worker", "interrupt failed: access denied", interrupt_command),
                                         ("review-worker", "resume failed: server unavailable", resume_command)):
            line = next(line for line in lines if f" · {worker} → codex1" in line)
            self.assertIn(reason, line)
            self.assertIn(command, line)
            self.assertNotIn("interrupted and resumed", line)
        idle = next(line for line in lines if " · test-worker → codex1" in line)
        self.assertNotIn("failed", idle)
        self.assertNotIn("interrupted", idle)

    def test_workers_swap_role_write_failure_exits_one_before_interrupting_workers(self) -> None:
        _ = self.workers_fixture()
        before = self.role_files()
        with (patch.object(accounts, "_write_role", side_effect=accounts.AccountsError("role write refused")),
              patch.object(codex_mesh, "retire_workers_servers") as retire):
            code, output, errors = self.run_cli("swap", "workers", "codex1")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("role write refused", errors)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_workers_swap_folds_same_holder_slot_across_runs_without_hiding_failures_or_other_holders(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                _ = self.workers_fixture()
                commands = ("mesh resume-account-move --to plan-c-review", "mesh resume-account-move --to plan-d-review")
                switches = (
                    codex_mesh.WorkersRunSwitch(self.root / "plan-a", ("holder-a",), (
                        codex_mesh.SwitchingWorker("plan-a-review", False),
                        codex_mesh.SwitchingWorker("plan-a-impl", False))),
                    codex_mesh.WorkersRunSwitch(self.root / "plan-b", ("holder-a",), (
                        codex_mesh.SwitchingWorker("plan-b-review-worker", True, codex_mesh.WorkerMoveResumed()),
                        codex_mesh.SwitchingWorker("plan-b-impl", False))),
                    codex_mesh.WorkersRunSwitch(self.root / "failures", ("holder-a",), (
                        codex_mesh.SwitchingWorker("plan-c-review", True,
                            codex_mesh.WorkerMoveFailed("interrupt failed: denied", commands[0])),
                        codex_mesh.SwitchingWorker("plan-d-review", True,
                            codex_mesh.WorkerMoveFailed("resume failed: refused", commands[1])))),
                    codex_mesh.WorkersRunSwitch(self.root / "different-holder", ("holder-b",), (
                        codex_mesh.SwitchingWorker("plan-e-review", False),)),
                )
                # Identical display names still describe two distinct holder identities.
                listing = roster.Roster([roster.RosterEntry(
                    machine="here", kind="claude", role="unit director", name="director", status="running",
                    reach=roster.Messageable(f"session:{holder}"), cwd="",
                    showrunner=roster.UnderShowrunner("build", "natedev", "session:showrunner"),
                    director=roster.DirectsUnit("two-accounts"), assignment=roster.NotAWorker(),
                    session=roster.ClaudeSession(holder)) for holder in ("holder-a", "holder-b")], [])
                with (patch.object(codex_mesh, "retire_workers_servers", return_value=switches),
                      patch.object(roster, "roster", return_value=listing),
                      patch.object(roster, "machine_name", return_value="here")):
                    code, output, errors = self.run_cli("swap", "workers", "codex1", *(["--dry-run"] if dry_run else []))
                self.assertEqual((code, errors), (0, ""))
                rows = output.splitlines()[1:]
                self.assertEqual(len(rows), 5)
                self.assertIn("  natedev · two-accounts · review-worker → codex1 ×2, 1 interrupted and resumed", rows)
                self.assertIn("  natedev · two-accounts · impl-worker → codex1 ×2", rows)
                self.assertIn("  natedev · two-accounts · review-worker → codex1", rows)
                for reason, command in zip(("interrupt failed: denied", "resume failed: refused"), commands, strict=True):
                    failed = next(row for row in rows if reason in row)
                    self.assertIn(command, failed)
                    self.assertNotIn("×", failed)

    def test_workers_swap_fold_counts_mid_turn_entries_and_preserves_single_rows_in_both_modes(self) -> None:
        previous, _ = self.workers_fixture()
        holder = roster.RosterEntry(
            machine="here", kind="claude", role="unit director", name="director", status="running",
            reach=roster.Messageable("session:holder"), cwd="",
            showrunner=roster.UnderShowrunner("build", "natedev", "session:showrunner"),
            director=roster.DirectsUnit("two-accounts"), assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("holder"))
        cases = (
            ((False,) * 11 + (True,), " ×12, 1 interrupted and resumed"),
            ((False, True, True, False), " ×4, 2 interrupted and resumed"),
            ((True, True, True), " ×3, 3 interrupted and resumed"),
            ((False, False, False), " ×3"),
            ((True,), ", interrupted and resumed"),
            ((False,), ""),
        )
        for dry_run in (True, False):
            for mid_turns, suffix in cases:
                with self.subTest(dry_run=dry_run, mid_turns=mid_turns):
                    switches = tuple(codex_mesh.WorkersRunSwitch(
                        self.root / f"plan-{index}", ("holder",), (
                            codex_mesh.SwitchingWorker(f"plan-{index}-test", mid_turn),))
                        for index, mid_turn in enumerate(mid_turns))
                    with (patch.object(roster, "roster", return_value=roster.Roster([holder], [])),
                          patch.object(roster, "machine_name", return_value="here")):
                        lines = accounts._workers_swap_lines(  # pyright: ignore[reportPrivateUsage]
                            switches, previous, "codex2", dry_run=dry_run)
                    self.assertEqual(lines[1:], (
                        f"  natedev · two-accounts · test-worker → codex2{suffix}",))

    def test_workers_swap_names_launcher_session_and_folds_with_that_sessions_holder_rows(self) -> None:
        holder = roster.RosterEntry(
            machine="here", kind="claude", role="unit director", name="director", status="running",
            reach=roster.Messageable("session:launching-director"), cwd="",
            showrunner=roster.UnderShowrunner("build", "natedev", "session:showrunner"),
            director=roster.DirectsUnit("two-accounts"), assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("launching-director"))
        switches = (
            codex_mesh.WorkersRunSwitch(self.root / "holderless-run", (), (
                codex_mesh.SwitchingWorker("plan-a-test", False),),
                launcher_sessions=("launching-director",)),
            codex_mesh.WorkersRunSwitch(self.root / "marked-run", ("launching-director",), (
                codex_mesh.SwitchingWorker("plan-b-test", True, codex_mesh.WorkerMoveResumed()),)),
            codex_mesh.WorkersRunSwitch(self.root / "unresolved-run", (), (
                codex_mesh.SwitchingWorker("plan-c-impl", False),)),
        )
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                _ = self.workers_fixture()
                with (patch.object(codex_mesh, "retire_workers_servers", return_value=switches),
                      patch.object(roster, "roster", return_value=roster.Roster([holder], [])),
                      patch.object(roster, "machine_name", return_value="here")):
                    code, output, errors = self.run_cli("swap", "workers", "codex1", *(["--dry-run"] if dry_run else []))
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(output.splitlines()[1:], [
                    "  natedev · two-accounts · test-worker → codex1 ×2, 1 interrupted and resumed",
                    "  unknown session (unresolved-run) · impl-worker → codex1",
                ])

    def test_workers_swap_uses_saved_launcher_session_name_when_roster_cannot_name_it(self) -> None:
        records = self.home / ".claude/sessions"
        records.mkdir(parents=True, exist_ok=True)
        _ = (records / "321.json").write_text(json.dumps({
            "pid": 321, "sessionId": "launching-director", "name": "saved launching director",
            "messagingSocketPath": "", "updatedAt": 10}))
        switches = (codex_mesh.WorkersRunSwitch(self.root / "holderless-run", (), (
            codex_mesh.SwitchingWorker("project-impl", False),), launcher_sessions=("launching-director",)),)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                previous, _ = self.workers_fixture()
                with patch.object(roster, "roster", return_value=roster.Roster([], [])):
                    lines = accounts._workers_swap_lines(  # pyright: ignore[reportPrivateUsage]
                        switches, previous, "codex1", dry_run=dry_run)
                self.assertEqual(lines[1:], ("  saved launching director · impl-worker → codex1",))

    def test_workers_swap_prints_ended_server_totals_and_each_busy_reason_in_both_modes(self) -> None:
        run = self.root / "ended-run"
        reason = "a conversation is not idle: unread-worker: ThreadLive"
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                _ = self.workers_fixture()
                before = self.role_files()
                switches = (codex_mesh.WorkersRunSwitch(run, (), (), leftover_servers=3,
                    stopped_servers=0 if dry_run else 1, kept_servers=(
                        codex_mesh.KeptWorkersServer(101, 4001, reason),
                        codex_mesh.KeptWorkersServer(102, 4002, "1 client connection(s) on its port"))),)
                with (patch.object(codex_mesh, "retire_workers_servers", return_value=switches),
                      patch.object(roster, "roster", side_effect=AssertionError("ended runs need no worker roster"))):
                    code, output, errors = self.run_cli("swap", "workers", "codex1", *(["--dry-run"] if dry_run else []))
                self.assertEqual((code, errors), (0, ""))
                summary = ("3 leftover servers from ended sessions: 1 can be stopped, 2 would stay" if dry_run
                           else "3 leftover servers from ended sessions, 1 stopped")
                prefix = "would stay" if dry_run else "left running"
                self.assertEqual(output.splitlines()[1:], [summary,
                    f"{prefix}: 101 on port 4001 ({run}): {reason}",
                    f"{prefix}: 102 on port 4002 ({run}): 1 client connection(s) on its port"])
                self.assertNotIn("unknown session", output)
                if dry_run:
                    self.assertEqual(self.role_files(), before)

    def test_workers_swap_uses_saved_holder_name_when_roster_cannot_list_it(self) -> None:
        records = self.home / ".claude/sessions"
        records.mkdir(parents=True, exist_ok=True)
        _ = (records / "321.json").write_text(json.dumps({
            "pid": 321, "sessionId": "missing-holder", "name": "saved director",
            "messagingSocketPath": "", "updatedAt": 10}))
        switches = (codex_mesh.WorkersRunSwitch(self.root / "private-run", ("missing-holder",), (
            codex_mesh.SwitchingWorker("project-impl", False),)),)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                _ = self.workers_fixture()
                with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                      patch.object(codex_mesh, "retire_workers_servers", return_value=switches)):
                    code, output, errors = self.run_cli("swap", "workers", "codex1", *(["--dry-run"] if dry_run else []))
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(output.splitlines()[1:], ["  saved director · impl-worker → codex1"])
                self.assertNotIn("private-run", output)

    def test_numbered_workers_target_selects_the_login_in_the_home_directory(self) -> None:
        default, _ = self.workers_fixture()
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.email = "default@example.invalid"
        report.source = agent_accounts.CodexHomeSource(default)
        before = self.role_files()
        with patch.object(codex_mesh, "retire_workers_servers", return_value=[]) as retire:
            code, stdout, stderr = self.run_cli("swap", "workers", "codex2", "--dry-run")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, "Workers would move codex1 → codex2:\n")
        self.assertEqual(self.role_files(), before)
        retire.assert_called_once_with(default, "codex2", dry_run=True)
        self.assertEqual(self.run_cli("set-role", "workers", "codex2"), (0, "", ""))
        self.assertEqual(agent_accounts.workers_home(), default)
        self.assertEqual(self.run_cli("role", "workers"), (0, "codex2\n", ""))
        self.assertEqual(accounts.role_account("workers").home, default)
        self.assertEqual(json.loads(accounts.accounts_config_file().read_text())["workers"], "codex2")

    def test_workers_swap_refuses_paid_credits_when_the_target_setting_is_off(self) -> None:
        _, target = self.workers_fixture()
        note = Path(os.environ["AGENT_NOTES_DIR"]) / "codex1.md"
        _ = note.write_text("---\nlogin: target@example.invalid\nuse_credits: off\n---\n")
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.quotas = [agent_accounts.Quota("Weekly", 100, datetime.now(timezone.utc) + timedelta(days=1))]
        report.credit_balance = "6370"
        before = self.role_files()
        with patch.object(codex_mesh, "retire_workers_servers") as retire:
            code, stdout, stderr = self.run_cli("swap", "workers", "codex1")
        self.assertNotEqual(code, 0)
        self.assertIn("is used up and set not to use credits; choose another account or wait for reset", stdout + stderr)
        self.assertIn("codex1 (~/.codex1)", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        self.assertEqual(target, self.home / ".codex1")
        retire.assert_not_called()

    def test_workers_swap_writes_role_before_retiring_servers(self) -> None:
        _, target = self.workers_fixture()
        observed: list[Path] = []
        def retire(home: Path, label: str, dry_run: bool = False) -> tuple[codex_mesh.WorkersRunSwitch, ...]:
            self.assertFalse(dry_run)
            self.assertEqual((home, label), (target, "codex1"))
            observed.append(agent_accounts.workers_home())
            return ()
        with patch.object(codex_mesh, "retire_workers_servers", side_effect=retire):
            result, stdout, stderr = self.run_cli("swap", "workers", "codex1")
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(observed, [target])
        self.assertEqual(stdout, "Workers moving codex2 → codex1:\n")
        self.assertEqual(self.run_cli("role", "workers"), (0, "codex1\n", ""))

    def test_workers_swap_refuses_current_role_without_mutation(self) -> None:
        _ = self.workers_fixture()
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        before = self.role_files()
        with patch.object(codex_mesh, "retire_workers_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "workers", "codex1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_workers_swap_accepts_default_real_shared_entries(self) -> None:
        default, _ = self.workers_fixture()
        _ = (default / "config.toml").write_text("own config")
        (default / "sessions").mkdir()
        for name in ("config.toml", "sessions"):
            _ = (self.home / ".codex1" / name).symlink_to(default / name)
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.email = "default@example.invalid"
        report.source = agent_accounts.CodexHomeSource(default)
        with patch.object(codex_mesh, "retire_workers_servers", return_value=[]) as retire:
            result, _, stderr = self.run_cli("swap", "workers", "codex2")
        self.assertEqual((result, stderr), (0, ""))
        retire.assert_called_once_with(default, "codex2", dry_run=False)
        self.assertEqual(self.run_cli("role", "workers"), (0, "codex2\n", ""))

    def test_workers_swap_default_refuses_unreadable_or_duplicate_login_without_mutation(self) -> None:
        default, _ = self.workers_fixture()
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        before = self.role_files()
        for condition in ("unreadable", "duplicate"):
            with self.subTest(condition=condition):
                if condition == "unreadable":
                    _ = (default / "auth.json").write_text("not json")
                else:
                    self.codex_login("default@example.invalid", default)
                    self.codex_login("default@example.invalid", self.home / ".codex3")
                with patch.object(codex_mesh, "retire_workers_servers") as retire:
                    result, stdout, stderr = self.run_cli("swap", "workers", "codex2")
                self.assertNotEqual(result, 0)
                self.assertIn("~/.codex", stdout + stderr)
                self.assertEqual(self.role_files(), before)
                retire.assert_not_called()

    def test_workers_swap_refuses_exhausted_unknown_and_expired_usage(self) -> None:
        _, target = self.workers_fixture()
        before = self.role_files()
        now = datetime.now(timezone.utc)
        for report in (
            agent_accounts.Report("Codex", email="target@example.invalid", five_hour=agent_accounts.Quota("5-hour", 100, now + timedelta(hours=1)),
                quotas=[agent_accounts.Quota("Weekly", 20, now + timedelta(days=1))], credit_balance="0"),
            agent_accounts.Report("Codex", email="target@example.invalid", problem="read failed"),
            agent_accounts.Report("Codex", email="target@example.invalid", five_hour=agent_accounts.Quota("5-hour", 0, now - timedelta(seconds=1)),
                quotas=[agent_accounts.Quota("Weekly", 0, now + timedelta(days=1))], credit_balance="0"),
        ):
            with self.subTest(report=report), patch.object(codex_mesh, "retire_workers_servers") as retire:
                report.source = agent_accounts.CodexHomeSource(target)
                self.codex_reads.return_value = report
                result, stdout, stderr = self.run_cli("swap", "workers", "codex1")
                self.assertNotEqual(result, 0)
                self.assertIn("codex1", stdout + stderr)
                self.assertIn("~/.codex1", stdout + stderr)
                self.assertEqual(self.role_files(), before)
                retire.assert_not_called()

    def test_workers_swap_accepts_credits_when_a_window_is_used_up(self) -> None:
        _ = self.workers_fixture()
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        report.five_hour = agent_accounts.Quota("5-hour", 100, datetime.now(timezone.utc) + timedelta(hours=1))
        report.credit_balance = "5"
        self.assertEqual(self.run_cli("swap", "workers", "codex1")[0], 0)

    def test_workers_swap_start_failure_names_setup_without_mutating_role_or_history(self) -> None:
        _, target = self.workers_fixture()
        before = self.role_files()
        command = '/showrunner:accounts setup "codex1"'
        self.codex_reads.return_value = agent_accounts.Report("Codex", source=agent_accounts.CodexHomeSource(target),
            problem=f"Codex did not start at ~/.codex1; run {command} and try again")
        with patch.object(codex_mesh, "retire_workers_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "workers", "codex1")
        self.assertNotEqual(result, 0)
        self.assertIn(command, stdout + stderr)
        self.assertIn("~/.codex1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_workers_swap_refuses_a_login_change_during_the_usage_read(self) -> None:
        _, target = self.workers_fixture()
        before = self.role_files()
        report = cast(agent_accounts.Report, self.codex_reads.return_value)
        def read(home: Path) -> agent_accounts.Report:
            self.assertEqual(home, target)
            self.codex_login("different@example.invalid", target)
            return report
        self.codex_reads.side_effect = read
        with patch.object(codex_mesh, "retire_workers_servers") as retire:
            result, stdout, stderr = self.run_cli("swap", "workers", "codex1")
        self.assertNotEqual(result, 0)
        self.assertIn("codex1", stdout + stderr)
        self.assertIn("~/.codex1", stdout + stderr)
        self.assertEqual(self.role_files(), before)
        retire.assert_not_called()

    def test_codex_log_directory_is_own_state(self) -> None:
        _, target = self.workers_fixture()
        (target / "log").mkdir()
        self.assertEqual(self.run_cli("check", "codex1"), (0, "", ""))

    def test_codex_setup_reports_preparation_failure_without_a_login_command(self) -> None:
        self.write_note("codex1", "target@example.invalid")
        with patch.object(agent_accounts, "prepare_codex_home",
            return_value=agent_accounts.CodexHomePreparationFailed("first start reached its limit")) as prepare:
            result, stdout, stderr = self.run_cli("setup", "codex1")
        self.assertNotEqual(result, 0)
        prepare.assert_called_once_with(self.home / ".codex1")
        self.assertNotIn("codex login", stdout)
        self.assertIn("limit", stderr + stdout)

    def test_codex_setup_warns_that_login_without_home_changes_the_default_account(self) -> None:
        self.write_note("codex1", "target@example.invalid")
        result, stdout, stderr = self.run_cli("setup", "codex1")
        self.assertEqual((result, stderr), (0, ""))
        command = stdout.index("CODEX_HOME=~/.codex1 codex login")
        warning = stdout[command:].splitlines()[1:]
        self.assertTrue(warning)
        self.assertIn("~/.codex", " ".join(warning))
        self.assertIn("CODEX_HOME", " ".join(warning))

    def test_codex_setup_wait_line_reaches_a_pipe_before_initialize_answers(self) -> None:
        self.write_note("codex1", "target@example.invalid")
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
        process = subprocess.Popen([sys.executable, str(Path(accounts.__file__)), "setup", "codex1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "The fake server must wait for its startup answer")
            self.assertIsNone(process.poll())
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            output_pipe = cast(TextIO, process.stdout)
            with selectors.DefaultSelector() as selector:
                _ = selector.register(output_pipe, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=TEST_HANG_GUARD_SECS), "The setup wait line must be flushed to its pipe")
            line = output_pipe.readline()
            self.assertIn("initialize", line)
            self.assertIn("limit", line)
            self.assertNotIn("codex login", line)
        finally:
            release.touch()
            try:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate()
        self.assertEqual((process.returncode, errors), (0, ""))
        self.assertIn("CODEX_HOME=~/.codex1 codex login", output)

    def test_table_workers_role_ignores_conflicting_process_home(self) -> None:
        default, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        with patch.dict(os.environ, {"CODEX_HOME": str(default)}):
            rows = self.status_rows()[1:]
        self.assertEqual([(row[0], row[2]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "workers")])
        self.assertEqual(agent_accounts.workers_home(), target)

    def test_table_keeps_workers_selection_when_a_live_read_changes_the_role(self) -> None:
        default, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        def read(home: Path) -> agent_accounts.Report:
            if home == default:
                self.assertEqual(self.run_cli("set-role", "workers", "codex2"), (0, "", ""))
            return agent_accounts.Report("Codex", email="default@example.invalid" if home == default else "target@example.invalid",
                source=agent_accounts.CodexHomeSource(home))
        self.codex_reads.side_effect = read
        rows = self.status_rows()[1:]
        self.assertEqual([(row[0], row[2]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "workers")])
        self.assertEqual(agent_accounts.workers_home(), default)
        self.assertNotEqual(target, default)

    def test_numbered_home_start_failure_row_names_setup(self) -> None:
        _, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        problem = 'Codex did not start at ~/.codex1; run /showrunner:accounts setup "codex1" and try again'
        startup = agent_accounts.CodexStartupFailure(target, '/showrunner:accounts setup "codex1"')
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem=problem,
            source=agent_accounts.CodexHomeSource(target), codex_startup=startup)
        rows = self.status_rows()[1:]
        target_row = next(row for row in rows if row[0] == "~/.codex1")
        self.assertIn('/showrunner:accounts setup "codex1"', " ".join(target_row))

    def test_table_uses_the_startup_result_command_independently_of_problem_text(self) -> None:
        _, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        command = '/showrunner:accounts setup "codex1"'
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Startup request timed out",
            codex_startup=agent_accounts.CodexStartupFailure(target, command))
        row = next(row for row in self.status_rows() if row[0] == "~/.codex1")
        self.assertEqual(row[3], command)
        self.assertEqual(len(row), len(accounts.TABLE_HEADINGS))

    def test_table_does_not_treat_similar_error_text_as_a_startup_failure(self) -> None:
        _, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Codex did not start another service",
            source=agent_accounts.CodexHomeSource(target))
        row = next(row for row in self.status_rows() if row[0] == "~/.codex1")
        self.assertEqual(row[3], "unavailable")

    def test_codex_setup_rerun_finishes_preparation_without_changing_identity(self) -> None:
        self.write_note("codex1", "target@example.invalid")
        home = self.home / ".codex1"
        self.codex_login("target@example.invalid", home)
        before = (home / "auth.json").read_bytes()
        with patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()) as prepare:
            result, stdout, stderr = self.run_cli("setup", "codex1")
        self.assertEqual((result, stderr), (0, ""))
        _ = prepare.assert_awaited_once_with(home)
        self.assertEqual((home / "auth.json").read_bytes(), before)
        lines = stdout.splitlines()
        self.assertIn("initialize", lines[0])
        self.assertIn("limit", lines[0].lower())
        self.assertLess(stdout.index("initialize"), stdout.index("CODEX_HOME=~/.codex1 codex login"))

    def test_legacy_unit_role_file_leaves_workers_unconfigured(self) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude3"}')
        message = "accounts: workers has no mapped account; run accounts.py set-role workers <codexN>"
        self.assertEqual(self.run_cli("role", "workers"), (1, "", message + "\n"))
        with self.assertRaises(ValueError) as refused:
            _ = agent_accounts.workers_home()
        self.assertEqual(str(refused.exception), message)


@final
class PickerTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 13, 12, tzinfo=timezone.utc).astimezone(tz)


ZERO_AGE = timedelta()


NO_PICK_EXCLUSIONS: frozenset[str] = frozenset()
NO_PICK_KEEP = accounts.NoKeep()


class PickerTests(unittest.TestCase):
    home: Path = Path()
    now: datetime = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes"), "USER_ZONE": "UTC",
        }, clear=True)))
        (self.home / "notes").mkdir()
        _ = installed_program(self.home, "claude")
        _ = installed_program(self.home, "codex")
        (self.home / ".claude").mkdir()
        self.claude("default", "main@example.invalid")
        self.claude("claude2", "main@example.invalid")
        self.claude("claude1", "first@example.invalid")
        self.claude("claude3", "third@example.invalid")
        self.reading("Claude", "main@example.invalid", 50, 7)
        self.reading("Claude", "first@example.invalid", 90, 5)
        self.reading("Claude", "third@example.invalid", 3, 2)
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()))

    def claude(self, label: str, login: str) -> None:
        if label == "default":
            config = self.home / ".claude.json"
            note = "claude2"
        else:
            self.assertEqual(accounts.setup(label), [])
            config = accounts.account_dir(label) / ".claude.json"
            note = label
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        _ = (self.home / "notes" / note_name(note)).write_text(f"---\nlogin: {login}\n---\n")

    def codex(self, label: str, login: str) -> None:
        home = self.home / (".codex" if label == "default" else "." + label.replace(" ", ""))
        note = "codex1" if label == "default" else label
        _ = (self.home / "notes" / note_name(note)).write_text(f"---\nlogin: {login}\n---\n")
        if label != "default":
            self.assertEqual(accounts.setup(label), [])
        home.mkdir(exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        note = "codex1" if label == "default" else label
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

    def selection(self, *, tool: Literal["claude", "codex"] = "claude",
                  exclude: frozenset[str] = NO_PICK_EXCLUSIONS,
                  keep: accounts.KeepAccount | accounts.NoKeep = NO_PICK_KEEP,
                  explain: bool = False) -> tuple[int, str, str]:
        chosen = accounts.pick(tool, self.now, exclude=exclude, keep=keep)
        lines = [chosen.label if isinstance(chosen, accounts.PickedAccount) else accounts.no_room_line(tool, chosen)]
        if explain:
            lines.extend(accounts._pick_explanation(reason) for reason in chosen.reasons)  # pyright: ignore[reportPrivateUsage]
        return (0 if isinstance(chosen, accounts.PickedAccount) else 1), "\n".join(lines) + "\n", ""

    def assert_picked(self, label: str, *, tool: Literal["claude", "codex"] = "claude",
                      exclude: frozenset[str] = NO_PICK_EXCLUSIONS,
                      keep: accounts.KeepAccount | accounts.NoKeep = NO_PICK_KEEP) -> None:
        chosen = accounts.pick(tool, self.now, exclude=exclude, keep=keep)
        self.assertIsInstance(chosen, accounts.PickedAccount)
        assert isinstance(chosen, accounts.PickedAccount)
        self.assertEqual(chosen.label, label)

    def test_pick_is_not_a_command(self) -> None:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error), self.assertRaises(SystemExit) as rejected:
            _ = accounts.main(["pick", "claude"])
        self.assertEqual(rejected.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("invalid choice: 'pick'", error.getvalue())

    def groups(self, units: str = "claude1", showrunners: str = "claude3") -> Path:
        path = self.home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def group_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with patch.object(accounts, "datetime", PickerTime), redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def test_string_groups_read_without_a_transition_and_next_selection_stores_one_object(self) -> None:
        path = self.groups()
        original = path.read_bytes()
        groups = accounts.group_accounts()
        self.assertEqual(accounts.group_label(groups, "units"), "claude1")
        self.assertEqual(accounts.group_note_labels(), {"units": "claude1", "showrunners": "claude3"})
        self.assertIsInstance(accounts.group_transition("units"), accounts.NoGroupTransition)
        self.assertEqual(path.read_bytes(), original)
        transition = accounts.set_group_account("units", "claude2", why="operator choice", now=self.now)
        self.assertEqual(transition.why, accounts.RecordedTransitionReason("operator choice"))
        stored = cast(dict[str, object], json.loads(path.read_text()))
        selection = cast(dict[str, object], stored["units"])
        self.assertEqual((selection["label"], selection["cause"], selection["window"]), ("claude2", "by hand", "unknown"))
        self.assertNotIn("reset", selection)
        self.assertTrue(selection["move_id"])
        self.assertEqual(stored["showrunners"], "claude3")
        self.assertEqual(accounts.group_transition("units"), transition)
        self.assertEqual(accounts.group_note_labels(), {"units": "claude2", "showrunners": "claude3"})

    def test_historical_transition_reason_is_unavailable_only_when_the_field_is_absent(self) -> None:
        path = self.groups()
        selection: dict[str, object] = {"label": "claude1", "move_id": "historical-transition",
                                      "cause": "by hand", "window": "unknown"}
        cases: tuple[tuple[dict[str, object], accounts.TransitionReason], ...] = (
            (selection, accounts.UnavailableTransitionReason()),
            ({**selection, "why": ""}, accounts.RecordedTransitionReason("")),
            ({**selection, "why": "operator selection"}, accounts.RecordedTransitionReason("operator selection")),
        )
        for stored, expected in cases:
            with self.subTest(selection=stored):
                _ = path.write_text(json.dumps({"units": stored, "showrunners": "claude3"}))
                before = path.read_bytes()
                transition = accounts.group_transition("units")
                assert isinstance(transition, accounts.GroupTransition)
                self.assertEqual(transition.move_id, "historical-transition")
                self.assertEqual(transition.why, expected)
                self.assertEqual(path.read_bytes(), before)

    def test_mixed_group_writes_keep_every_sibling_field_and_log_only_the_changed_group(self) -> None:
        transition = {"label": "claude1", "move_id": "original-transition", "cause": "refusal",
                      "window": "weekly", "reset": "2026-10-20T12:00:00+00:00"}
        cases: tuple[tuple[dict[str, object], accounts.ClaudeGroup, str, accounts.ClaudeGroup], ...] = (
            ({"units": "default", "showrunners": transition}, "showrunners", "claude3", "units"),
            ({"units": transition, "showrunners": "claude3"}, "showrunners", "claude2", "units"),
            ({"units": transition, "showrunners": "claude3"}, "units", "claude2", "showrunners"))
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        for selections, changed, target, sibling in cases:
            with self.subTest(changed=changed, sibling=selections[sibling]):
                _ = path.write_text(json.dumps(selections))
                history = accounts.roles_history_file()
                history.unlink(missing_ok=True)
                note = self.home / "notes/claude2.md"
                content = note.read_bytes()
                if selections["units"] == "default":
                    note.unlink()
                try:
                    _ = accounts.set_group_account(changed, target, why="one group only", now=self.now)
                finally:
                    _ = note.write_bytes(content)
                stored = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(stored[sibling], selections[sibling])
                self.assertIsInstance(stored[changed], dict)
                self.assertNotIn("~/.claude", path.read_text())
                rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["group"], changed)
                selection = cast(dict[str, object], stored[changed])
                self.assertEqual(rows[0]["move_id"], selection["move_id"])
                self.assertEqual(rows[0]["cause"], selection["cause"])

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_lists_work_routing_separately_from_account_moves(self) -> None:
        self.codex("default", "worker@example.invalid")
        common = moves.MoveCommon(at=self.now.isoformat(), tool="codex", subject="workers", move_id="account-move")
        request: moves.RequestedMove = {**common, "kind": "requested", "from": "codex1", "to": "codex2",
            "why": "manual", "window": "unknown", "cause": "by hand"}
        moves.append_move(request)
        stamp = self.now.isoformat()
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=stamp, tool="codex",
            fallback_at=stamp, owner="codex1", cause="credits off", why="credits off", moved=["direct", "fix"]))
        with patch.object(accounts, "datetime", PickerTime):
            text = accounts.status_report(detail=True)
        self.assertIn("by hand", text)
        self.assertIn("Codex work routing:", text)
        routing = text.split("Codex work routing:", 1)[1]
        self.assertIn("routed to claude", routing)
        self.assertIn("credits off", routing)
        self.assertIn("direct", routing)
        self.assertIn("— open", routing)
        self.assertNotIn("routed to claude", text.split("Codex work routing:", 1)[0])
        moves.append_routing(moves.RoutedBack(kind="routed back", at=stamp, tool="codex",
            fallback_at=stamp, owner="codex1", restored=["direct"], kept=["fix"]))
        failed_at = (self.now + timedelta(minutes=1)).isoformat()
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=failed_at, tool="codex",
            fallback_at=failed_at, owner="unknown", cause="refusal", why="Codex refused work for quota", moved=["direct"]))
        moves.append_routing(moves.RoutingNotRestored(kind="routing not restored", at=failed_at, tool="codex",
            fallback_at=failed_at, owner="unknown", outcome="failed", restored=[], kept=[], failed=["worker (unreadable)"]))
        with patch.object(accounts, "datetime", PickerTime):
            routing = accounts.status_report(detail=True).split("Codex work routing:", 1)[1]
        self.assertEqual(routing.count("routed to claude"), 2)
        self.assertIn("— routed back", routing)
        self.assertIn("— not restored (failed)", routing)
        self.assertIn("worker (unreadable)", routing)
        self.assertIn("owner unknown", routing)
        self.assertNotIn("— open", routing)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_detail_shows_scheduled_claude_routing_as_auto(self) -> None:
        self.codex("default", "worker@example.invalid")
        stamp = self.now.isoformat()
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=stamp, tool="codex",
            fallback_at=stamp, owner="codex1", cause="auto", why="auto: workers-once", moved=["direct"]))
        with patch.object(accounts, "datetime", PickerTime):
            text = accounts.status_report(detail=True)
        routing = text.split("Codex work routing:", 1)[1]
        self.assertIn("auto", routing)
        self.assertIn("routed to claude", routing)
        self.assertIn("direct", routing)
        self.assertIn("— open", routing)

    def test_default_input_is_refused_before_any_session_or_home_matcher(self) -> None:
        self.codex("default", "worker@example.invalid")
        expected = "accounts: name the account by number (~/.claude is claude2; ~/.codex is codex1)"
        commands = (("move", "some-session", "default"), ("move", "units", "default"),
                    ("move", "showrunners", "default"), ("set-role", "workers", "default"),
                    ("swap", "workers", "default"), ("exec", "default"), ("check", "default"))
        with (patch.object(agent_accounts, "codex_home_matches_label",
                           side_effect=AssertionError("matcher receives forbidden default")),
              patch.object(os, "execvpe") as launch):
            for command in commands:
                with self.subTest(command=command):
                    code, stdout, stderr = self.group_cli(*command)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr.strip(), expected)
        launch.assert_not_called()
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_move_units_to_the_home_number_uses_the_home_and_writes_only_numbers(self) -> None:
        code, stdout, stderr = self.group_cli("move", "units", "claude2")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude2", self.home / ".claude2"))
        self.assertEqual(stored_group_labels(accounts.groups_file()), {"units": "claude2"})
        self.assertNotIn('"default"', accounts.roles_history_file().read_text())
        self.assertTrue((self.home / ".claude2").exists())
        self.assertEqual(accounts.group_note_labels(), {"units": "claude2"})

    def test_legacy_groups_resolve_home_number_without_rewriting_until_next_group_write(self) -> None:
        path = self.groups(units="default", showrunners="claude3")
        history = accounts.roles_history_file()
        _ = history.write_text('{"at":"2026-10-08T00:00:00Z","role":"unit","label":"default","account_label":"claude2"}\n')
        original = path.read_bytes()
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude2", self.home / ".claude2"))
        for command in (("status",), ("placement", "--explain")):
            code, stdout, stderr = self.group_cli(*command)
            self.assertEqual((code, stderr), (0, ""))
            self.assertIn("claude2", stdout)
            self.assertNotRegex(stdout, r"\bdefault\b")
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(accounts.group_note_labels(), {"units": "claude2", "showrunners": "claude3"})
        _ = accounts.set_group_account("showrunners", "claude1", why="numbered selection", now=self.now)
        self.assertEqual(stored_group_labels(path), {"units": "default", "showrunners": "claude1"})

    def test_missing_or_unreadable_note_refuses_local_destination_and_preserves_group_storage(self) -> None:
        groups = self.groups()
        original = groups.read_bytes()
        note = self.home / "notes/claude3.md"
        note.unlink()
        for problem, message in (("missing", "no account note"), ("unreadable", "could not read")):
            with self.subTest(problem=problem):
                if problem == "unreadable":
                    note.mkdir()
                with self.assertRaises(accounts.AccountsError) as resolution:
                    _ = accounts.claude_account_dir("claude3")
                self.assertIn("claude3", str(resolution.exception))
                self.assertIn(message, str(resolution.exception))
                with self.assertRaises(accounts.AccountsError) as selection:
                    _ = accounts.set_group_account("units", "claude3", why="manual selection", now=self.now)
                self.assertIn("claude3", str(selection.exception))
                self.assertIn(message, str(selection.exception))
                self.assertEqual(groups.read_bytes(), original)
                self.assertFalse(accounts.roles_history_file().exists())

    def test_picker_rejects_non_numbered_and_wrong_tool_filters_before_reading_candidates(self) -> None:
        for tool, invalid in (("claude", ("default", "~/.codex", "claude", "codex1")),
                              ("codex", ("default", "~/.codex", "claude", "claude1"))):
            for option in ("--exclude", "--keep"):
                for label in invalid:
                    with (self.subTest(tool=tool, option=option, label=label),
                          patch.object(accounts, "checked_named_accounts") as named,
                          patch.object(agent_accounts, "codex_homes") as homes,
                          patch.object(agent_accounts, "codex_home_matches_label") as matcher):
                        with self.assertRaises(accounts.AccountsError) as rejected:
                            _ = accounts.pick(cast(Literal["claude", "codex"], tool), self.now,
                                exclude=frozenset({label}) if option == "--exclude" else frozenset(),
                                keep=accounts.KeepAccount(label) if option == "--keep" else accounts.NoKeep())
                        message = "name the account by number" if label == "default" else label
                        self.assertIn(message, str(rejected.exception))
                        named.assert_not_called()
                        homes.assert_not_called()
                        matcher.assert_not_called()

    def test_every_pick_uses_the_preparable_destination_when_a_home_alias_directory_holds_another_login(self) -> None:
        self.assertEqual(accounts.setup("claude2"), [])
        alias = self.home / ".claude2"
        _ = (alias / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "third@example.invalid"}}))
        excluded: set[str] = set()
        selected: list[str] = []
        while True:
            picked = accounts.pick("claude", self.now, exclude=frozenset(excluded))
            if isinstance(picked, accounts.NoAccountWithRoom):
                break
            self.assertNotIn(picked.label, excluded)
            selected.append(picked.label)
            excluded.add(picked.label)
            self.assertNotEqual(picked.dir, alias)
            self.assertEqual(picked.dir, accounts.claude_account_dir(picked.label))
            identity = accounts.account_identity(picked.dir)
            self.assertIsInstance(identity, agent_accounts.NumberedAccount)
            assert isinstance(identity, agent_accounts.NumberedAccount)
            self.assertEqual(identity.label, picked.label)
            prepared = accounts.prepared_account(picked.label, resume=True)
            self.assertIsInstance(prepared, accounts.PreparedAccount)
        self.assertCountEqual(selected, ["claude1", "claude3"])

    def test_writing_another_group_preserves_a_legacy_home_selection_until_its_note_returns(self) -> None:
        path = self.groups(units="default", showrunners="claude3")
        history = accounts.roles_history_file()
        _ = history.write_text('{"at":"2026-10-08T00:00:00Z","role":"unit","label":"default","account_label":"claude2"}\n')
        note = self.home / "notes/claude2.md"
        contents = note.read_bytes()
        note.unlink()
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        _ = accounts.set_group_account("showrunners", "claude1", why="manual selection", now=self.now)
        self.assertEqual(stored_group_labels(path), {"units": "default", "showrunners": "claude1"})
        self.assertNotIn("~/.claude", path.read_text())
        _ = note.write_bytes(contents)
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude2", self.home / ".claude2"))

    def test_legacy_workers_selection_resolves_its_number_until_the_next_normal_write(self) -> None:
        self.codex("default", "worker@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"workers": "default"}')
        original = config.read_bytes()
        self.assertEqual(self.group_cli("role", "workers"), (0, "codex1\n", ""))
        self.assertEqual(accounts.role_account("workers"), accounts.CodexWorkersAccount("codex1", self.home / ".codex"))
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(self.group_cli("set-role", "workers", "codex2"), (0, "", ""))
        self.assertEqual(json.loads(config.read_text()), {"workers": "codex2"})
        self.assertNotIn('"default"', accounts.roles_history_file().read_text())

    def test_absent_workers_key_refuses_without_using_the_signed_in_home_or_writing(self) -> None:
        self.codex("default", "worker@example.invalid")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude3"}')
        original = config.read_bytes()
        self.assertEqual(self.group_cli("role", "workers"), (1, "",
            "accounts: workers has no mapped account; run accounts.py set-role workers <codexN>\n"))
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse(accounts.roles_history_file().exists())

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unnoted_claude_home_is_displayed_but_never_selected_or_a_destination(self) -> None:
        (self.home / "notes/claude2.md").unlink()
        self.assert_picked("claude3")
        picked = accounts.pick("claude", self.now, exclude=frozenset({"claude1", "claude3"}))
        self.assertIsInstance(picked, accounts.NoAccountWithRoom)
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude3")
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"claude2: not selectable; claude2 has no account note at {self.home / 'notes/claude2.md'}; add ~/rust/hanadocs/agents/claudeN.md with login: main@example.invalid; run accounts.py setup claude2", stdout.splitlines())
        for command in (("move", "units", "claude2"), ("move", "some-session", "claude2"),
                        ("exec", "claude2"), ("move", "units", "default")):
            with self.subTest(command=command):
                code, stdout, stderr = self.group_cli(*command)
                self.assertNotEqual(code, 0)
                self.assertIn("no account note", stdout + stderr)
        code, stdout, stderr = self.group_cli("move", "units", "claude1")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(accounts.role_account("unit").dir, self.home / ".claude1")

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_legacy_group_for_an_unnoted_home_is_display_only_and_cannot_launch(self) -> None:
        path = self.groups(units="default")
        original = path.read_bytes()
        (self.home / "notes/claude2.md").unlink()
        groups = accounts.group_accounts()
        self.assertEqual(accounts.group_label(groups, "units"), "home account unresolved")
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"claude2: not selectable; claude2 has no account note at {self.home / 'notes/claude2.md'}; add ~/rust/hanadocs/agents/claudeN.md with login: main@example.invalid; run accounts.py setup claude2", stdout.splitlines())
        with self.assertRaisesRegex(accounts.AccountsError, "home account unresolved"):
            _ = accounts.role_account("unit")
        self.assertEqual(path.read_bytes(), original)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unnoted_codex_home_is_displayed_but_never_picked_or_assigned_to_workers(self) -> None:
        self.codex("default", "unnoted@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        (self.home / "notes/codex1.md").unlink()
        self.reading("Codex", "unnoted@example.invalid", 90, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        message = ("~/.codex — no account note, not selectable: add "
                   "~/rust/hanadocs/agents/codexN.md with login: unnoted@example.invalid")
        self.assert_picked("codex2", tool="codex")
        picked = accounts.pick("codex", self.now, exclude=frozenset({"codex2"}))
        self.assertIsInstance(picked, accounts.NoAccountWithRoom)
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("~/.codex: not selectable; this login has no account note; add ~/rust/hanadocs/agents/codexN.md with login: unnoted@example.invalid; then run accounts.py cutover-home <codexN>", stdout.splitlines())
        for command in (("swap", "workers", "codex1"), ("set-role", "workers", "codex1"),
                        ("swap", "workers", "default")):
            with self.subTest(command=command):
                code, stdout, stderr = self.group_cli(*command)
                self.assertNotEqual(code, 0)
                self.assertIn(message, stdout + stderr)
        self.assertEqual(self.group_cli("set-role", "workers", "codex2")[0], 0)
        self.assertEqual(accounts.role_account("workers").label, "codex2")

    def test_reports_and_refusals_never_name_an_account_default_even_with_legacy_journal(self) -> None:
        self.codex("default", "worker@example.invalid")
        self.reading("Codex", "worker@example.invalid", 60, 3)
        _ = self.groups(units="default")
        history = accounts.roles_history_file()
        history.parent.mkdir(parents=True, exist_ok=True)
        _ = history.write_text(''.join(json.dumps(entry) + "\n" for entry in (
            {"at": "2026-10-12T00:00:00Z", "role": "unit", "label": "default", "account_label": "claude2"},
            {"at": "2026-10-12T00:00:00Z", "role": "workers", "label": "default", "account_label": "codex1"})))
        journal = moves.store_file()
        _ = journal.write_text(''.join(json.dumps(entry) + "\n" for entry in (
            {"at": "2026-10-13T00:00:00Z", "tool": "claude", "kind": "requested", "subject": "units",
             "move_id": "old-claude", "from": "claude1", "to": "default", "why": "quota", "window": "unknown"},
            {"at": "2026-10-13T00:00:00Z", "tool": "codex", "kind": "requested", "subject": "workers",
             "move_id": "old-codex", "from": "default", "to": "codex2", "why": "quota", "window": "unknown"})))
        original = journal.read_bytes()
        for command in (("--detail", "status"), ("placement", "--explain"), ("move", "units", "claude99"),
                        ("swap", "workers", "codex99")):
            with self.subTest(command=command):
                _, stdout, stderr = self.group_cli(*command)
                self.assertTrue(stdout or stderr)
                self.assertNotRegex(stdout + stderr, r"\bdefault\b")
                if "status" in command:
                    self.assertIn("claude1 → claude2", stdout)
                    self.assertIn("codex1 → codex2", stdout)
        self.assertEqual(journal.read_bytes(), original)

    def test_user_reports_and_move_refusals_never_print_spaced_labels(self) -> None:
        self.codex("codex2", "worker@example.invalid")
        self.reading("Codex", "worker@example.invalid", 60, 3)
        _ = self.groups(units="claude 1", showrunners="claude 3")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"workers": "codex 2"}')
        for arguments in (("status",), ("placement", "--explain"), ("role", "workers"),
                          ("move", "units", "claude 99"), ("swap", "workers", "codex 99")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.group_cli(*arguments)
                self.assertTrue(stdout or stderr)
                self.assertNotRegex(stdout + stderr, r"(?:claude|codex) [1-9][0-9]*")
                if arguments[0] not in ("move", "swap"):
                    self.assertEqual(code, 0, stderr)
                else:
                    self.assertNotEqual(code, 0)

    def test_placement_fresh_zero_percent_without_refusal_selects_an_account_with_room(self) -> None:
        self.reading("Claude", "third@example.invalid", 0, 2, five_left=0)
        result = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude1")
        self.assertIsInstance(result.reason, accounts.UnitsResetSoonest)

    def test_placement_unresolved_refusal_excludes_positive_room(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        result = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude1")

    def test_placement_units_choose_soonest_reset_and_break_ties_by_most_left(self) -> None:
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude3")
        self.reading("Claude", "first@example.invalid", 4, 2)
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude1")

    def test_placement_showrunners_choose_most_weekly_left(self) -> None:
        result = accounts.placement("showrunners", self.now, exclude=frozenset())
        self.assertEqual(result.label, "claude1")
        self.assertIsInstance(result.reason, accounts.ShowrunnersMostLeft)

    def test_placement_no_room_stays_on_stored_group_account(self) -> None:
        path = self.groups()
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        before = path.read_bytes()
        unit = accounts.placement("units", self.now, exclude=frozenset())
        self.assertEqual(unit.label, "claude1")
        self.assertIsInstance(unit.reason, accounts.GroupNoRoom)
        self.assertEqual(accounts.placement("showrunners", self.now, exclude=frozenset()).label, "claude3")
        self.assertEqual(path.read_bytes(), before)

    def test_placement_example_skips_zero_percent_units_and_ranks_high_room_showrunners(self) -> None:
        _ = self.groups()
        self.reading("Claude", "main@example.invalid", 0, 2)
        self.reading("Claude", "third@example.invalid", 3, 4)
        self.reading("Claude", "first@example.invalid", 53, 6)
        self.assertEqual(accounts.placement("showrunners", self.now, exclude=frozenset({"claude3"})).label, "claude1")
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset({"claude1"})).label, "claude3")
        with patch.object(accounts, "datetime", PickerTime):
            self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude1", accounts.account_dir("claude1")))
            self.assertEqual(accounts.role_account("showrunner"), accounts.NamedAccount("claude3", accounts.account_dir("claude3")))

    def test_new_launches_keep_each_stored_group_even_when_readings_rank_better_elsewhere(self) -> None:
        path = self.groups()
        before = path.read_bytes()
        with patch.object(accounts, "datetime", PickerTime):
            unit = accounts.prepared_account("unit", resume=True)
            runner = accounts.role_account("showrunner")
        self.assertIsInstance(unit, accounts.PreparedAccount)
        assert isinstance(unit, accounts.PreparedAccount)
        self.assertEqual(unit.variables, named_environment(accounts.account_dir("claude1")))
        self.assertEqual(runner, accounts.NamedAccount("claude3", accounts.account_dir("claude3")))
        self.assertEqual(path.read_bytes(), before)

    def test_read_only_placement_and_status_leave_absent_group_file_absent(self) -> None:
        path = self.home / ".local/state/accounts/groups.json"
        for arguments in (("placement", "--explain"), ("status",)):
            with self.subTest(arguments=arguments):
                code, output, error = self.group_cli(*arguments)
                self.assertEqual((code, error), (0, ""))
                if arguments[0] == "placement":
                    self.assertIn("units", output)
                    self.assertIn("showrunners", output)
                    self.assertIn("not configured", output)
                else:
                    rows = [line for line in output.splitlines() if line.startswith("| ~/.")]
                    self.assertTrue(rows)
                    self.assertTrue(all(not line.split("|")[3].strip() for line in rows))
                self.assertFalse(path.exists())
                self.assertFalse(accounts.roles_history_file().exists())

    def test_unconfigured_launch_roles_refuse_without_inference_or_group_writes(self) -> None:
        """F004: configuration alone decides a launch account."""
        with patch.object(accounts, "pick", side_effect=AssertionError("launch inferred from readings")):
            for role, group in (("unit", "units"), ("showrunner", "showrunners")):
                with self.subTest(role=role), self.assertRaises(accounts.AccountsError) as refused:
                    _ = accounts.role_account(role)
                self.assertIn(f"accounts.py move {group} <claudeN>", str(refused.exception))
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_configured_launch_roles_ignore_room_and_allow_one_shared_account(self) -> None:
        """F004: even a refused configured account is returned without a picker call."""
        path = self.groups(units="claude1", showrunners="claude1")
        before = path.read_bytes()
        refusals.write_refusal(refusals.AccountRefusal("claude", "first@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        with patch.object(accounts, "pick", side_effect=AssertionError("launch checked room")):
            for role in ("unit", "showrunner"):
                self.assertEqual(accounts.role_account(role), accounts.NamedAccount("claude1", accounts.account_dir("claude1")))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())

    def test_manual_group_move_creates_only_requested_configuration_when_file_absent(self) -> None:
        """F004: an explicit move can configure one group without bootstrapping the other."""
        with patch.object(accounts, "move_sessions", return_value=()) as moved:
            code, _, error = self.group_cli("move", "units", "claude1")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude1", accounts.account_dir("claude1")))
        with self.assertRaises(accounts.AccountsError) as refused:
            _ = accounts.role_account("showrunner")
        self.assertIn("accounts.py move showrunners <claudeN>", str(refused.exception))
        self.assertEqual(stored_group_labels(accounts.groups_file()), {"units": "claude1"})
        self.assertFalse(moved.called)

    # Reserved fixtures model provider accounts in this status test.
    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_starts_with_the_table_and_keeps_groups_in_role_cells(self) -> None:
        _ = self.groups()
        code, output, error = self.group_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual([cell.strip() for cell in output.splitlines()[0].strip("|").split("|")],
            ["Folder", "Label", "Role", "Weekly left", "Weekly resets", "On credits", "Credits left", "Read"])
        self.assertFalse(any(line.startswith("units:") for line in output.splitlines()))
        rows = [[cell.strip() for cell in line.strip("|").split("|")]
                for line in output.splitlines() if line.startswith("| ~/.claude")]
        self.assertIn("units", next(row[2] for row in rows if row[1] == "claude1"))
        self.assertIn("showrunners", next(row[2] for row in rows if row[1] == "claude3"))
        self.assertFalse(any("next" in row[2] for row in rows))

    def test_picker_without_saved_readings_keeps_unrefused_accounts_eligible(self) -> None:
        usage_store.store_file().unlink()
        code, output, error = self.selection(explain=True)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "claude1")
        self.assertIn("no fresh readings", output)

    def test_picker_without_fresh_readings_keeps_candidate_despite_stale_zero_room(self) -> None:
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            self.reading("Claude", login, 0, 2, age=timedelta(minutes=11), five_left=0)
        self.assert_picked("claude3", keep=accounts.KeepAccount("claude3"))
        self.assert_picked("claude1", keep=accounts.KeepAccount("claude3"), exclude=frozenset({"claude3"}))
        self.reading("Claude", "main@example.invalid", 50, 7)
        self.reading("Claude", "first@example.invalid", 90, 5)
        code, output, error = self.selection(explain=True)
        self.assertEqual((code, error), (0, ""))
        explanation = next(line for line in output.splitlines() if line.startswith("claude3:"))
        self.assertTrue(explanation.endswith("; has room; no reading in the last 10 minutes"), explanation)
        for group in accounts.CLAUDE_GROUPS:
            with self.subTest(group=group):
                result = accounts.placement(group, self.now, exclude=frozenset({"claude1", "claude2"}))
                self.assertEqual(result.label, "claude3")
                self.assertNotIsInstance(result.reason, accounts.GroupNoRoom)

    def test_picker_without_fresh_readings_skips_refused_keep(self) -> None:
        usage_store.store_file().unlink()
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("claude1", keep=accounts.KeepAccount("claude3"))
        _, output, _ = self.selection(keep=accounts.KeepAccount("claude3"), explain=True)
        self.assertIn("refused at", output)

    def test_picker_without_fresh_readings_never_falls_back_to_refused_default(self) -> None:
        usage_store.store_file().unlink()
        refusals.write_refusal(refusals.AccountRefusal("claude", "main@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("claude1")

    def test_picker_fresh_zero_candidate_is_ineligible_even_with_missing_other_readings(self) -> None:
        usage_store.store_file().unlink()
        self.reading("Claude", "third@example.invalid", 0, 2)
        self.assert_picked("claude2", keep=accounts.KeepAccount("claude2"))
        self.assert_picked("claude1")

    def test_picker_prefers_soonest_weekly_reset_over_most_room(self) -> None:
        self.assert_picked("claude3")

    def test_picker_breaks_equal_weekly_resets_by_most_weekly_left(self) -> None:
        self.reading("Claude", "first@example.invalid", 4, 2)
        self.assert_picked("claude1")

    def test_picker_breaks_equal_reset_and_room_by_label(self) -> None:
        self.reading("Claude", "first@example.invalid", 3, 2)
        self.assert_picked("claude1")

    def test_picker_keeps_current_account_with_room_despite_a_sooner_reset(self) -> None:
        self.assert_picked("claude1", keep=accounts.KeepAccount("claude1"))

    def test_picker_does_not_keep_a_fresh_zero_percent_account_without_a_refusal(self) -> None:
        self.reading("Claude", "first@example.invalid", 0, 2)
        self.assert_picked("claude3", keep=accounts.KeepAccount("claude1"))

    def test_picker_has_no_minimum_remaining_percentage(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, five_left=1)
        self.assert_picked("claude3")

    def test_picker_excludes_each_explicit_label(self) -> None:
        self.assert_picked("claude2", exclude=frozenset({"claude3", "claude1"}))

    def test_picker_exclude_overrides_keep(self) -> None:
        self.assert_picked("claude1", keep=accounts.KeepAccount("claude3"), exclude=frozenset({"claude3"}))

    def test_picker_explains_fresh_zero_weekly_or_five_hour_as_no_room_and_placement_agrees(self) -> None:
        for weekly, five_hour in ((0, 90), (90, 0)):
            with self.subTest(weekly=weekly, five_hour=five_hour):
                self.reading("Claude", "third@example.invalid", weekly, 2, five_left=five_hour)
                code, output, error = self.selection(explain=True)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(output.splitlines()[0], "claude1")
                explanation = next(line for line in output.splitlines() if line.startswith("claude3:"))
                self.assertTrue(explanation.endswith("; no room"), explanation)
                self.assert_picked("claude1", keep=accounts.KeepAccount("claude3"))
                for group in accounts.CLAUDE_GROUPS:
                    with self.subTest(group=group):
                        result = accounts.placement(group, self.now)
                        self.assertEqual(result.label, "claude1")
                        reason = next(reason for reason in result.reasons if reason.label == "claude3")
                        self.assertEqual(reason.status, "no room")

    def test_picker_all_fresh_zero_candidates_refuses_with_the_soonest_reset_and_placement_agrees(self) -> None:
        _ = self.groups()
        cases = ((0, 50, False, self.now + timedelta(days=2), "Thu 2026-10-15 12:00 UTC"),
                 (50, 0, True, self.now + timedelta(hours=3), "Tue 2026-10-13 15:00 UTC"))
        for weekly, five_hour, five, earliest, reset_text in cases:
            with self.subTest(weekly=weekly, five_hour=five_hour):
                for login, days in (("main@example.invalid", 7), ("first@example.invalid", 5),
                                    ("third@example.invalid", 2)):
                    self.reading("Claude", login, weekly, days, five_left=five_hour, five=five)
                code, output, error = self.selection(explain=True)
                self.assertEqual(code, 1, output + error)
                self.assertIn("no claude account has room; the earliest reset is " + reset_text, output + error)
                self.assertEqual(sum(line.endswith("; no room") for line in output.splitlines()), 3)
                for group in accounts.CLAUDE_GROUPS:
                    result = accounts.placement(group, self.now)
                    self.assertEqual(result.label, "claude1" if group == "units" else "claude3")
                    self.assertIsInstance(result.reason, accounts.GroupNoRoom)
                    assert isinstance(result.reason, accounts.GroupNoRoom)
                    self.assertEqual(result.reason.outcome.earliest_reset, accounts.EarliestReset(earliest))

    def test_picker_accepts_weekly_room_when_claude_has_no_five_hour_window(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, five=False)
        self.assert_picked("claude3")

    def test_picker_skips_readings_older_than_ten_minutes_with_the_cause(self) -> None:
        self.reading("Claude", "third@example.invalid", 90, 2, age=timedelta(minutes=10, microseconds=1))
        code, output, error = self.selection(explain=True)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("has room", output)
        self.assertEqual(output.splitlines()[0], "claude1")

    def test_picker_accepts_a_reading_exactly_ten_minutes_old(self) -> None:
        self.reading("Claude", "third@example.invalid", 1, 2, age=timedelta(minutes=10))
        self.assert_picked("claude3")

    def test_picker_skips_an_account_with_no_saved_reading(self) -> None:
        self.claude("claude4", "unread@example.invalid")
        _, output, _ = self.selection(explain=True)
        self.assertIn("claude4", output)
        self.assertIn("has room", output)
        self.assert_picked("claude3")

    def test_picker_accepts_a_repairable_missing_link_but_skips_a_real_file_collision(self) -> None:
        projects = accounts.account_dir("claude3") / "projects"
        projects.unlink()
        self.assert_picked("claude3")
        self.assertFalse(os.path.lexists(projects))
        _ = projects.write_text("local file against existing shared folder")
        self.assert_picked("claude1")
        self.assertEqual(projects.read_text(), "local file against existing shared folder")

    def test_picker_skips_accounts_whose_shared_target_is_a_dangling_link(self) -> None:
        target = self.home / ".claude/projects"
        target.rmdir()
        missing = self.home / "missing-shared-projects"
        target.symlink_to(missing, target_is_directory=True)

        code, output, error = self.selection(keep=accounts.KeepAccount("claude3"))

        self.assertEqual(code, 1)
        self.assertIn("no claude account has room", output)
        self.assertEqual(error, "")
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(missing))
        self.assertFalse(missing.exists())
        target.unlink()
        target.mkdir()
        self.assert_picked("claude3")

    def test_picker_deduplicates_logins_in_favor_of_default(self) -> None:
        self.claude("claude3", "main@example.invalid")
        self.reading("Claude", "main@example.invalid", 3, 1)
        self.assert_picked("claude2")

    def test_picker_deduplicates_named_logins_in_favor_of_lowest_number(self) -> None:
        self.claude("claude10", "shared@example.invalid")
        self.claude("claude4", "shared@example.invalid")
        self.reading("Claude", "shared@example.invalid", 3, 1)
        self.assert_picked("claude4")

    def test_picker_reads_codex_namespace_independently_of_claude(self) -> None:
        self.codex("default", "main@example.invalid")
        self.codex("codex2", "second@example.invalid")
        self.reading("Codex", "main@example.invalid", 0, 1)
        self.reading("Codex", "second@example.invalid", 1, 2)
        self.assert_picked("codex2", tool="codex")

    def test_codex_exclude_home_number_excludes_the_numbered_home_account(self) -> None:
        """F003: the home number excludes its current numbered login."""
        self.codex("default", "main@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        self.assert_picked("codex2", exclude=frozenset({"codex1"}), tool="codex")

    def test_codex_keep_home_number_retains_the_numbered_home_account(self) -> None:
        """F003: keeping the home number outranks the spare's earlier reset."""
        self.codex("default", "main@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 3)
        self.reading("Codex", "spare@example.invalid", 50, 1)
        self.assert_picked("codex1", keep=accounts.KeepAccount("codex1"), tool="codex")
        self.assert_picked("codex2", keep=accounts.KeepAccount("codex1"), exclude=frozenset({"codex1"}), tool="codex")

    def test_codex_exclude_home_number_cannot_reselect_a_duplicate_home_login(self) -> None:
        """F002/F003: a duplicate folder cannot bypass exclusion of the canonical destination."""
        self.codex("default", "main@example.invalid")
        self.codex("codex1", "original@example.invalid")
        _ = (self.home / ".codex1/auth.json").write_text((self.home / ".codex/auth.json").read_text())
        _ = (self.home / "notes/codex1.md").write_text("---\nlogin: main@example.invalid\n---\n")
        self.codex("codex2", "spare@example.invalid")
        self.reading("Codex", "main@example.invalid", 80, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        self.assert_picked("codex2", exclude=frozenset({"codex1"}), tool="codex")

    def test_codex_picker_without_fresh_readings_keeps_candidate_or_uses_the_home_account(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.codex("codex2", "other@example.invalid")
        self.assert_picked("codex1", tool="codex")
        self.assert_picked("codex2", keep=accounts.KeepAccount("codex2"), tool="codex")
        refusals.write_refusal(refusals.AccountRefusal("codex", "other@example.invalid",
            refusals.UnknownWindow(), refusals.NoResetKnown(), self.now))
        self.assert_picked("codex1", keep=accounts.KeepAccount("codex2"), tool="codex")

    def test_picker_codex_credits_allow_work_after_both_windows_are_exhausted(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.reading("Codex", "workers@example.invalid", 0, 2, five_left=0, credits="1")
        self.assert_picked("codex1", tool="codex")

    def test_picker_skips_paid_room_on_a_credits_off_account(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        note = self.home / "notes/codex1.md"
        _ = note.write_text("---\nlogin: workers@example.invalid\nuse_credits: off\n---\n")
        self.reading("Codex", "workers@example.invalid", 0, 1, credits="6370")
        self.reading("Codex", "spare@example.invalid", 1, 3)
        self.assert_picked("codex2", tool="codex")

    def test_picker_codex_unlimited_credits_allow_work(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.reading("Codex", "workers@example.invalid", 0, 2, five_left=0, credits="unlimited")
        self.assert_picked("codex1", tool="codex")

    def test_picker_codex_without_credits_requires_both_windows_with_room(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.reading("Codex", "workers@example.invalid", 80, 2, five_left=0)
        code, output, error = self.selection(tool="codex")
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
        self.assertEqual(self.selection()[0], 1)
        self.assertIn("earliest reset", "".join(self.selection()[1:]))

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
        self.assertEqual(self.selection()[0], 1)
        self.assertIn("no claude account has room", "".join(self.selection()[1:]))

    def test_picker_unresolved_refusal_beats_positive_saved_room_and_keep(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        self.assert_picked("claude1", keep=accounts.KeepAccount("claude3"))
        code, output, error = self.selection(explain=True)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("refused at", output)

    def test_picker_codex_unresolved_refusal_beats_positive_windows_and_credits(self) -> None:
        self.codex("default", "workers@example.invalid")
        self.reading("Codex", "workers@example.invalid", 90, 2, five_left=90, credits="100")
        refusals.write_refusal(refusals.AccountRefusal("codex", "workers@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
        selected = accounts.pick("codex", self.now, keep=accounts.KeepAccount("codex1"))
        self.assertIsInstance(selected, accounts.NoAccountWithRoom)
        code, output, error = self.selection(explain=True, tool="codex")
        self.assertEqual(code, 1)
        self.assertIn("refused at", output + error)

    def test_picker_accepts_account_after_its_refusal_reset_passes(self) -> None:
        refusals.write_refusal(refusals.AccountRefusal("claude", "third@example.invalid",
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now), self.now - timedelta(days=7)))
        self.assert_picked("claude3")

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
        _ = self.groups(units="claude3", showrunners="claude1")
        with patch.object(accounts, "datetime", PickerTime):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(accounts.main(["status"]), 0)
        rows = [[cell.strip() for cell in line.strip("|").split("|")]
                for line in output.getvalue().splitlines() if line.startswith("| ~/.claude")]
        self.assertEqual([row[1] for row in rows if row[2] == "units"], ["claude3"])
        self.assertEqual([row[1] for row in rows if row[2] == "showrunners"], ["claude1"])

    def test_unit_role_reads_group_and_ignores_legacy_saved_unit_key(self) -> None:
        _ = self.groups(units="claude3")
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude1"}')
        before = config.read_bytes()
        with patch.object(accounts, "datetime", PickerTime):
            selected = accounts.role_account("unit")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(accounts.main(["role", "unit"]), 0)
        self.assertEqual(selected, accounts.NamedAccount("claude3", accounts.account_dir("claude3")))
        self.assertEqual(output.getvalue(), "claude3\n")
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())

    def test_env_unit_and_exec_unit_use_configured_units_account(self) -> None:
        _ = self.groups(units="claude3")
        with patch.object(accounts, "datetime", PickerTime):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(accounts.main(["env", "unit"]), 0)
            environment = shlex.split(output.getvalue())
            self.assertIn(f"CLAUDE_CONFIG_DIR={accounts.account_dir('claude3')}", environment)
            with patch.object(os, "execvpe") as launch:
                self.assertEqual(accounts.main(["exec", "unit", "--version"]), 0)
        self.assertEqual(launch.call_args.args[:2], (str(self.home / ".local/bin/claude"), [str(self.home / ".local/bin/claude"), "--version"]))
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(accounts.account_dir("claude3")))
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())

    def test_picked_account_exposes_its_directory_weekly_room_and_reset(self) -> None:
        picked = accounts.pick("claude", self.now, keep=accounts.NoKeep())
        self.assertIsInstance(picked, accounts.PickedAccount)
        assert isinstance(picked, accounts.PickedAccount)
        self.assertEqual(picked.label, "claude3")
        self.assertEqual(picked.dir, accounts.account_dir("claude3"))
        self.assertEqual(picked.weekly_left, 3)
        self.assertEqual(picked.weekly_reset, refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual({reason.label for reason in picked.reasons}, {"claude2", "claude1", "claude3"})
        self.assertEqual([reason.label for reason in picked.reasons if reason.status == "picked"], ["claude3"])

    def test_retired_unit_mutations_refuse_before_disk_or_process_changes(self) -> None:
        cause = "unit launches use the configured units account; select and move the group with accounts.py move units <claudeN>"
        for verb in ("set-role", "swap"):
            with self.subTest(verb=verb), patch.object(subprocess, "run") as process:
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    result = accounts.main([verb, "unit", "claude3"])
                self.assertEqual(result, 1)
                self.assertEqual(len(error.getvalue().splitlines()), 1)
                self.assertIn(cause, error.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertFalse(accounts.accounts_config_file().exists())
                self.assertFalse(accounts.roles_history_file().exists())
                process.assert_not_called()


class SharedStatusDocumentTests(unittest.TestCase):
    """The stored JSON adds access and spending signals without changing its text view."""

    at: datetime = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)

    def row(self) -> accounts.StatusRow:
        return accounts.StatusRow(
            label="fixture", tool="codex", folder="~/.codex1", login="fixture@example.invalid",
            roles=("workers",), weekly_left_percent=60, resets_at=self.at + timedelta(days=1),
            credits=13446, on_credits="no", use_credits=True, read_at=self.at,
            read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=self.at)

    def reading(self, row: accounts.StatusRow) -> accounts.StatusReading:
        return accounts.StatusReading(rows=(row,), notes=("* Fixture problem.",), detail_lines=(),
                                      zone=ZoneInfo("America/New_York"), checked_at=self.at)

    def document(self, row: accounts.StatusRow) -> dict[str, object]:
        status = cast(dict[str, object], json.loads(accounts.render_status_json(self.reading(row))))
        self.assertEqual(status["problems"], ["Fixture problem."])
        return cast(list[dict[str, object]], status["accounts"])[0]

    def test_access_requires_a_stored_reading_and_an_ok_or_held_latest_outcome(self) -> None:
        for outcome in agent_accounts.UsageReadOutcome:
            for has_reading in (True, False):
                with self.subTest(outcome=outcome, has_reading=has_reading):
                    row = replace(self.row(), read_outcome=outcome,
                                  read_at=self.at if has_reading else accounts.StatusUnavailable())
                    document = self.document(row)
                    expected = has_reading and outcome in (
                        agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD)
                    self.assertIs(document.get("read_ok"), expected)
                    self.assertIs(document.get("credits_in_use"), False)
                    self.assertEqual(document["read_at"], self.at.isoformat() if has_reading else None)

    def test_credit_spending_is_independent_of_enabled_balance_and_access(self) -> None:
        for enabled in (True, False):
            for spending in ("yes", "no", "—"):
                with self.subTest(enabled=enabled, spending=spending):
                    row = replace(self.row(), on_credits=spending, use_credits=enabled,
                                  credits=accounts.StatusUnavailable())
                    document = self.document(row)
                    self.assertIs(document.get("credits_in_use"), spending == "yes")
                    self.assertIs(document.get("read_ok"), True)
                    self.assertIs(document["use_credits"], enabled)
                    self.assertIsNone(document["credits"])

    def test_new_json_signals_leave_the_existing_text_table_exactly_unchanged(self) -> None:
        reading = self.reading(self.row())
        self.assertEqual(accounts.render_status_table(reading), (
            '| Folder    | Label   | Role    | Weekly left | Weekly resets       | On credits | Credits left | Read      |\n'
            '| --------- | ------- | ------- | ----------- | ------------------- | ---------- | ------------ | --------- |\n'
            '| ~/.codex1 | fixture | workers | 60%         | Sat 10-17 14:00 EDT | no         | 13,446       | 14:00 EDT |\n'
            '\n'
            '* Fixture problem.'
        ))
        self.assertEqual(self.document(self.row()), {
            "label": "fixture", "tool": "codex", "roles": ["workers"],
            "weekly_left_percent": 60, "resets_at": "2026-10-17T18:00:00+00:00",
            "credits": 13446, "use_credits": True, "read_at": "2026-10-16T18:00:00+00:00",
            "read_ok": True, "credits_in_use": False,
        })


class StatusProblemCauseTests(unittest.TestCase):
    """Status reports one cause and one repair for each home problem."""

    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes"),
            "CODEX_HOME": str(self.home / ".codex"), "USER_ZONE": "UTC",
        }, clear=True)))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no real child")))
        _ = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("no real child")))

    def reading(self, login: str, *, noted: bool = False) -> accounts.StatusReading:
        home = self.home / ".codex"
        home.mkdir(exist_ok=True)
        auth = home / "auth.json"
        _ = auth.write_text("fixture sign-in")
        at = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc)
        os.utime(auth, (at.timestamp(), at.timestamp()))
        status = accounts.CodexStatus("~/.codex", home, True, True,
            "codex1" if noted else accounts.NO_VALUE, login, "workers",
            () if noted else (accounts.unnoted_home_line(home, login),), ())
        usage = {usage_store.key("Codex", login): usage_store.StoredUsage(
            read_outcome=agent_accounts.UsageReadOutcome.ERROR,
            attempt_outcome=agent_accounts.UsageReadOutcome.ERROR, attempted_at=at.isoformat())}
        with (patch.object(accounts, "account_statuses", return_value=[]),
              patch.object(accounts, "codex_statuses", return_value=[status]),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", home)),
              patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
              patch.object(accounts, "_stored_usage", return_value=usage),
              patch.object(accounts, "_codex_home_login", return_value=login),
              patch.object(accounts, "home_account_label", return_value="codex1"),
              patch.object(account, "claude_account", return_value=None)):
            return accounts.read_status(source=accounts.StoredUsageOnly())

    def status_with(self, statuses: list[accounts.AccountStatus], codex: list[accounts.CodexStatus],
                    usage: usage_store.StoredUsage, *, role_error: str = "", detail: bool = False) -> accounts.StatusReading:
        workers = accounts.CodexWorkersAccount("codex1", self.home / ".codex1")
        with (patch.object(accounts, "account_statuses", return_value=statuses),
              patch.object(accounts, "codex_statuses", return_value=codex),
              patch.object(accounts, "role_account", side_effect=accounts.AccountsError(role_error) if role_error else None,
                           return_value=workers),
              patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
              patch.object(accounts, "_stored_usage", return_value={
                  usage_store.key("Claude", status.login): usage for status in statuses} | {
                  usage_store.key("Codex", status.login): usage for status in codex}),
              patch.object(accounts, "_codex_home_login", return_value=""),
              patch.object(account, "claude_account", return_value=None)):
            return accounts.read_status(source=accounts.StoredUsageOnly(), detail=detail)

    def codex_inventory(self, *, linked: tuple[str, ...] = (), duplicate: bool = False) -> list[accounts.CodexStatus]:
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "codex1.md").write_text("---\nlogin: fixture@notexample.com\n---\n")
        homes = [self.home / ".codex1", *([self.home / ".codex2"] if duplicate else [])]
        for home in homes:
            home.mkdir()
        for name in linked:
            (homes[0] / name).symlink_to(self.home / "private-target")
        def login_at(home: Path) -> str:
            return "fixture@notexample.com" if home in homes else ""

        def listed_homes(_workers_home: Path | agent_accounts.NoWorkersHome) -> list[Path]:
            return list(homes)

        with (patch.object(accounts, "_codex_home_login", side_effect=login_at),
              patch.object(agent_accounts, "codex_homes", side_effect=listed_homes),
              patch.object(agent_accounts, "codex_email_on_disk", side_effect=login_at),
              patch.object(accounts, "_shared_link_problems", return_value=[])):
            return accounts.codex_statuses(homes[0])

    def assert_inventory_state_problems(self, fault: str, *, omit_direct: bool = False) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        main = self.home / ".claude"
        main.mkdir()
        for name in accounts.SHARED:
            target = main / name
            target.mkdir()
            (directory / name).symlink_to(target, target_is_directory=True)
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: fixture@notexample.com\n---\n")
        identity = directory / ".claude.json"
        credentials = directory / ".credentials.json"
        credentials.symlink_to(self.home / "private-target")
        setup = "run accounts.py setup claude2"
        login_step = f"run CLAUDE_CONFIG_DIR={directory} claude and /login"
        private = (f"claude2: private file is linked; {credentials} is a link; "
                   + f"identity stays in each account dir; use a private file in this home; {setup}")
        expected = [private]
        if fault == "malformed":
            _ = identity.write_text("{")
            expected.append(f"claude2: cannot read {identity}; Expecting property name enclosed in double quotes: line 1 column 2 (char 1); check the file and its permissions; {setup}")
        elif fault == "invalid identity":
            _ = identity.write_text('{"oauthAccount": []}')
            expected.append(f"claude2: invalid sign-in; {identity} has an invalid oauthAccount identity; {login_step}")
        elif fault == "linked identity":
            identity.symlink_to(self.home / "identity-target")
            expected.append(f"claude2: cannot use {identity}; {identity} is a link; use a real private directory; {setup}")
        elif fault == "inaccessible folder":
            expected = [f"claude2: cannot inspect {directory}; fixture denied; check the file and its permissions; {setup}"]
        else:
            _ = identity.write_text("{}")
            expected.append(f"claude2: not logged in; {directory} has no readable sign-in; {login_step}")
        original_stat = Path.stat

        def stat_path(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if fault == "inaccessible folder" and path == directory and follow_symlinks:
                raise PermissionError("fixture denied")
            return original_stat(path, follow_symlinks=follow_symlinks)

        original_inspect = accounts._inspect_account_directory  # pyright: ignore[reportPrivateUsage]

        def inspect(selected: accounts.NamedAccount, expected: accounts.AccountNote) -> accounts.AccountDirectoryReadiness:
            with patch.object(Path, "stat", new=stat_path):
                return original_inspect(selected, expected)

        with patch.object(accounts, "_inspect_account_directory", side_effect=inspect):
            statuses = accounts.account_statuses()
        if omit_direct:
            direct = statuses[0].state_source
            diagnostics = tuple(text for text in statuses[0].problems if text != (
                direct.diagnostic if isinstance(direct, accounts.ObservedStateCause) else f"accounts: {directory} is not logged in"))
            statuses = [replace(statuses[0], problems=diagnostics)]
            expected[-1] = f"* claude2: not logged in; no readable sign-in is stored; {login_step}"
        reading = self.status_with(statuses, [], usage_store.StoredUsage())
        expected.extend((
            "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
            "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
        ))
        self.assertCountEqual(reading.notes, expected)
        terminal = accounts.render_status_table(reading).splitlines()
        self.assertCountEqual(terminal[terminal.index("") + 1:], expected)
        self.assertCountEqual(cast(list[str], json.loads(accounts.render_status_json(reading))["problems"]),
            [line.removeprefix("* ") for line in expected])

    def test_state_summary_stays_when_its_observed_diagnostic_is_absent(self) -> None:
        self.assert_inventory_state_problems("malformed", omit_direct=True)

    def test_logout_summary_without_a_source_stays_visible(self) -> None:
        self.assert_inventory_state_problems("logout", omit_direct=True)

    def test_unlinked_logout_summary_is_not_merged_with_a_direct_diagnostic(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        main = self.home / ".claude"
        main.mkdir()
        for name in accounts.SHARED:
            (main / name).mkdir()
            (directory / name).symlink_to(main / name, target_is_directory=True)
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: fixture@notexample.com\n---\n")
        _ = (directory / ".claude.json").write_text("{}")
        statuses = [replace(status, state_source=accounts.NoObservedStateCause()) for status in accounts.account_statuses()]
        reading = self.status_with(statuses, [], usage_store.StoredUsage())
        step = f"run CLAUDE_CONFIG_DIR={directory} claude and /login"
        expected = [f"claude2: not logged in; {directory} has no readable sign-in; {step}",
                    f"* claude2: not logged in; no readable sign-in is stored; {step}",
                    "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
                    "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>"]
        self.assertCountEqual(reading.notes, expected)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        self.assertCountEqual(document["problems"], [line.removeprefix("* ") for line in expected])

    def test_malformed_identity_has_one_cause_and_keeps_private_file_fault(self) -> None:
        self.assert_inventory_state_problems("malformed")

    def test_invalid_identity_has_one_cause_and_keeps_private_file_fault(self) -> None:
        self.assert_inventory_state_problems("invalid identity")

    def test_linked_identity_has_one_cause_and_keeps_private_file_fault(self) -> None:
        self.assert_inventory_state_problems("linked identity")

    def test_inaccessible_folder_has_one_cause(self) -> None:
        self.assert_inventory_state_problems("inaccessible folder")

    def test_ordinary_logout_keeps_its_problem_and_private_file_fault(self) -> None:
        self.assert_inventory_state_problems("logout")

    def test_each_linked_private_file_keeps_its_own_status_repair(self) -> None:
        statuses = self.codex_inventory(linked=("installation_id", "version.json"))
        reading = self.status_with([], statuses, usage_store.StoredUsage())
        for name in ("installation_id", "version.json"):
            line = (f"codex1: private file is linked; {self.home / '.codex1' / name} is a link; "
                    + "sign-in and home state stay in each account home; use a private file in this home; run accounts.py setup codex1")
            self.assertIn(line, reading.notes)
            self.assertIn(line, cast(list[str], json.loads(accounts.render_status_json(reading))["problems"]))

    def test_test_signin_keeps_an_independent_linked_credentials_repair(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
        credentials = directory / ".credentials.json"
        credentials.symlink_to(self.home / "private-target")
        statuses = accounts.account_statuses()
        reading = self.status_with(statuses, [], usage_store.StoredUsage())
        linked = [line for line in reading.notes if "private file is linked" in line and str(credentials) in line]
        self.assertEqual(len(linked), 1)
        self.assertIn("use a private file in this home", linked[0])
        self.assertTrue(any("test wrote" in line for line in reading.notes))
        self.assertFalse(any("account note" in line or "setup claude2" in line or "cutover-home" in line for line in reading.notes))

    def test_duplicate_home_diagnostic_is_one_line_when_both_rows_have_one_account(self) -> None:
        statuses = self.codex_inventory(duplicate=True)
        self.assertEqual([status.label for status in statuses], ["codex1", "codex1"])
        reading = self.status_with([], statuses, usage_store.StoredUsage())
        lines = [line for line in reading.notes if "login is stored in two homes" in line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(reading.notes), len(set(reading.notes)))

    def test_duplicate_pairs_are_complete_and_stable_across_three_homes_and_both_producers(self) -> None:
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "codex1.md").write_text("---\nlogin: fixture@notexample.com\n---\n")
        homes = [self.home / name for name in (".codex", ".codex1", ".codex2")]
        for home in homes:
            home.mkdir()
        def login(home: Path) -> str:
            return "fixture@notexample.com" if home in homes else ""
        observed: list[list[str]] = []
        for order in (homes, list(reversed(homes))):
            with (patch.object(accounts, "_codex_home_login", side_effect=login),
                  patch.object(agent_accounts, "codex_email_on_disk", side_effect=login),
                  patch.object(agent_accounts, "codex_homes", return_value=order),
                  patch.object(accounts, "_codex_identity_homes", return_value=order),
                  patch.object(accounts, "_shared_link_problems", return_value=[])):
                inventory = accounts.codex_statuses(homes[0])
            reading = self.status_with([], inventory, usage_store.StoredUsage())
            expected = [
                "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
                "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
            ]
            for first, second in (("~/.codex", "~/.codex1"), ("~/.codex", "~/.codex2"), ("~/.codex1", "~/.codex2")):
                expected.append(f"codex1: login is stored in two homes; {first} and {second} are logged in as fixture@notexample.com; two homes on one login break token refresh; keep each login in one home")
            lines = list(reading.notes)
            observed.append(sorted(lines))
            self.assertCountEqual(lines, expected)
            self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].splitlines(), expected)
            self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)
        self.assertEqual(observed[0], observed[1])

    def test_startup_observation_does_not_own_another_homes_service_failure(self) -> None:
        statuses = self.codex_inventory(duplicate=True)
        failed, other = statuses
        read = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc)
        startup = agent_accounts.CodexStartupFailure(failed.home, "accounts.py setup codex1")
        for outcome in (agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.ERROR,
                        agent_accounts.UsageReadOutcome.REFUSED_401):
            with self.subTest(outcome=outcome):
                usage_store.save([agent_accounts.Report("Codex", email=failed.login,
                    quotas=[agent_accounts.Quota("Weekly", 50, read + timedelta(days=3))])], read)
                reports = [agent_accounts.Report("Codex", codex_startup=startup, problem=startup.problem),
                    agent_accounts.Report("Codex", email=other.login, usage_read_outcome=outcome,
                        quotas=[agent_accounts.Quota("Weekly", 50, read + timedelta(days=3))]
                        if outcome == agent_accounts.UsageReadOutcome.OK else [])]
                with (patch.object(accounts, "account_statuses", return_value=[]),
                      patch.object(accounts, "codex_statuses", return_value=list(statuses)),
                      patch.object(accounts, "_codex_live", side_effect=reports),
                      patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", failed.home)),
                      patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
                      patch.object(accounts, "_codex_home_login", return_value=""),
                      patch.object(account, "claude_account", return_value=None)):
                    reading = accounts.read_status()
                at = reading.rows[0].read_at
                self.assertIsInstance(at, datetime)
                previous = f"this shows the reading from {cast(datetime, at):%H:%M}"
                expected = [
                    "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
                    "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
                    "* codex1: usage unavailable; Codex did not start at ~/.codex1; " + previous
                        + "; run accounts.py setup codex1 and try again"
                        + ("; next try in 2 min" if outcome == agent_accounts.UsageReadOutcome.ERROR else
                           "; start a session on it to check it" if outcome == agent_accounts.UsageReadOutcome.REFUSED_401 else ""),
                    "codex1: login is stored in two homes; ~/.codex1 and ~/.codex2 are logged in as fixture@notexample.com; two homes on one login break token refresh; keep each login in one home",
                ]
                if outcome != agent_accounts.UsageReadOutcome.OK:
                    expected.append("* codex1: usage unavailable; " + (
                        "the usage read failed; " + previous + "; next try in 2 min"
                        if outcome == agent_accounts.UsageReadOutcome.ERROR else
                        "the usage service did not accept this login; start a session on it to check it"))
                terminal = accounts.render_status_table(reading).splitlines()
                self.assertCountEqual(terminal[terminal.index("") + 1:], expected)
                document = cast(dict[str, object], json.loads(accounts.render_status_json(reading)))
                self.assertCountEqual(cast(list[str], document["problems"]),
                    [line.removeprefix("* ") for line in expected])

    def test_check_causes_merge_only_for_the_same_specific_file(self) -> None:
        for kind in ("check", "usage", "identity"):
            for same_object in (False, True):
                with self.subTest(kind=kind, same_object=same_object):
                    first = accounts.StatusProblem("codex1", "first fault",
                        accounts.ProblemCause(cast(accounts.ProblemKind, kind), "first cause", accounts.CauseFile("/fixture/installation_id"), accounts.CauseAccount("codex1")),
                        accounts.ProblemStep("first repair"))
                    second = accounts.StatusProblem("codex1", "second fault",
                        accounts.ProblemCause(cast(accounts.ProblemKind, kind), "second cause",
                            accounts.CauseFile("/fixture/installation_id" if same_object else "/fixture/version.json"), accounts.CauseAccount("codex1")),
                        accounts.ProblemStep("second repair"))
                    self.assertEqual(len(accounts.merge_status_problems([first, second])), 1 if same_object else 2)

    def test_independent_configuration_problems_each_keep_their_repair(self) -> None:
        status = accounts.AccountStatus("~/.claude2", self.home / ".claude2", True, True, True,
            "claude2", "owner@provider.io", "", (), ("mystery-cache",), ("brp",), ("account-only",))
        reading = self.status_with([status], [], usage_store.StoredUsage(), detail=True)
        expected = (
            "claude2: unclassified entries; mystery-cache; check these entries in ~/.claude2; keep private files in this home",
            "claude2: mcpServers differ from ~/.claude; - brp, + account-only; check mcpServers in ~/.claude2/.claude.json against ~/.claude.json",
        )
        problems = cast(list[str], json.loads(accounts.render_status_json(reading))["problems"])
        for line in expected:
            self.assertIn(line, reading.notes)
            self.assertIn(line, problems)

    def test_generic_service_error_is_independent_of_local_account_faults(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"}}))
        statuses = accounts.account_statuses()
        reading = self.status_with(statuses, [], usage_store.StoredUsage(
            read_outcome=agent_accounts.UsageReadOutcome.ERROR,
            attempt_outcome=agent_accounts.UsageReadOutcome.ERROR))
        line = "* claude2: usage unavailable; the usage read failed; no reading is stored yet; next try in 2 min"
        self.assertIn(line, reading.notes)
        self.assertIn(line, accounts.render_status_table(reading).splitlines())
        self.assertIn(line.removeprefix("* "), cast(accounts.StatusDocument,
            json.loads(accounts.render_status_json(reading)))["problems"])

    def test_unreadable_note_keeps_independent_usage_failure_and_retry(self) -> None:
        directory = self.home / ".claude2"
        note = self.home / "notes/claude2.md"
        status = accounts.AccountStatus("~/.claude2", directory, True, True, True,
            "claude2", "owner@provider.io", "", (f"accounts: could not read {note}: denied",), (), (), ())
        usage = usage_store.StoredUsage(read_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            attempt_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            weekly=usage_store.StoredWindow(left=50, resets="2026-11-01T01:14:00+00:00", at="2026-10-29T01:14:00+00:00"), attempted_at="2026-10-29T01:20:00+00:00")
        lines = self.status_with([status], [], usage).notes
        self.assertTrue(any("cannot read" in line for line in lines))
        self.assertIn("* claude2: usage unavailable; the usage service is busy; this shows the reading from 01:14; next try after 01:30", lines)

    def test_test_signin_with_malformed_or_unreadable_note_has_only_delete_advice(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        main = self.home / ".claude"
        main.mkdir()
        for name in accounts.SHARED:
            (main / name).mkdir()
            (directory / name).symlink_to(main / name, target_is_directory=True)
        identity = directory / ".claude.json"
        _ = identity.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
        stamp = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc).timestamp()
        os.utime(identity, (stamp, stamp))
        note = self.home / "notes/claude2.md"
        note.parent.mkdir()
        groups = role_map.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude2", "showrunners": "claude2"}))
        for contents in (b"---\nname: fixture\n---\n", b"\xff"):
            with self.subTest(contents=contents):
                _ = note.write_bytes(contents)
                diagnostic = accounts._account_note("claude2")  # pyright: ignore[reportPrivateUsage]
                assert isinstance(diagnostic, accounts.UnreadableAccountNote)
                reading = accounts.read_status(source=accounts.StoredUsageOnly())
                lines = reading.notes
                self.assertEqual(len([line for line in lines if "test wrote" in line]), 1)
                self.assertFalse(any("account note" in line or "setup claude2" in line
                                     or "cutover-home" in line for line in lines))
                expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>",
                            "claude2: test sign-in is stored; a test wrote ~/.claude2/.claude.json at 2026-10-29 01:14 UTC; delete ~/.claude2/.claude.json"]
                self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(), expected)
                self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)

    def test_startup_failure_stays_visible_when_another_home_reads_the_same_login(self) -> None:
        first = self.home / ".codex1"
        startup = agent_accounts.CodexStartupFailure(first, "accounts.py setup codex1")
        statuses = [
            accounts.CodexStatus("~/.codex1", first, True, True, "codex1", "owner@provider.io", "spare", (startup.problem,), (), startup),
            accounts.CodexStatus("~/.codex2", self.home / ".codex2", True, True, "codex2", "owner@provider.io", "workers", (), ()),
        ]
        usage = usage_store.StoredUsage(read_outcome=agent_accounts.UsageReadOutcome.OK,
            weekly=usage_store.StoredWindow(left=50, resets="2026-11-01T01:14:00+00:00", at="2026-10-29T01:14:00+00:00"))
        reading = self.status_with([], statuses, usage)
        for lines in (reading.notes, cast(list[str], json.loads(accounts.render_status_json(reading))["problems"])):
            failures = [line for line in lines if "Codex did not start" in line]
            self.assertEqual(len(failures), 1)
            self.assertIn("~/.codex1", failures[0])
            self.assertIn("reading from 01:14", failures[0])
            self.assertIn("accounts.py setup codex1", failures[0])

    def test_duplicate_identity_keeps_independent_rate_limit_and_its_times(self) -> None:
        statuses: list[accounts.CodexStatus] = []
        for number in (1, 2):
            directory = self.home / f".codex{number}"
            directory.mkdir()
            note = self.home / f"notes/codex{number}.md"
            note.parent.mkdir(exist_ok=True)
            _ = note.write_text("---\nlogin: owner@provider.io\n---\n")
        with (patch.object(accounts, "_codex_home_login", return_value="owner@provider.io"),
              patch.object(accounts, "_codex_identity_homes", return_value=[self.home / ".codex1", self.home / ".codex2"]),
              patch.object(accounts, "_shared_link_problems", return_value=[])):
            for number in (1, 2):
                label = f"codex{number}"
                diagnostic = accounts._check_codex_home(label, self.home / ".codex1")  # pyright: ignore[reportPrivateUsage]
                statuses.append(accounts.CodexStatus(f"~/.codex{number}", self.home / f".codex{number}",
                    True, True, label, "owner@provider.io", "spare", tuple(diagnostic), ()))
        usage = usage_store.StoredUsage(read_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            attempt_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            weekly=usage_store.StoredWindow(left=50, resets="2026-11-01T01:14:00+00:00", at="2026-10-29T01:14:00+00:00"),
            attempted_at="2026-10-29T01:20:00+00:00")
        lines = self.status_with([], statuses, usage).notes
        for number in (1, 2):
            self.assertIn(f"* codex{number}: usage unavailable; the usage service is busy; this shows the reading from 01:14; next try after 01:30", lines)
        self.assertEqual(len([line for line in lines if "login is stored in two homes;" in line]), 1)
        duplicate = next(line for line in lines if "login is stored in two homes;" in line)
        self.assertIn("~/.codex1", duplicate)
        self.assertIn("~/.codex2", duplicate)

    def test_merge_drops_only_the_same_typed_cause_on_the_same_owner(self) -> None:
        kinds: tuple[accounts.ProblemKind, ...] = ("usage", "access", "identity", "wrong login",
            "signed out", "unreadable note", "unnoted", "startup", "test login", "role", "unclassified", "mcp servers", "check")
        for first_kind in kinds:
            for second_kind in kinds:
                for same_account, same_home in ((False, False), (False, True), (True, False), (True, True)):
                    with self.subTest(first=first_kind, second=second_kind, same_account=same_account, same_home=same_home):
                        first = accounts.StatusProblem("claude2", "first diagnostic",
                            accounts.ProblemCause(first_kind, "first cause", accounts.CauseFile("/fixture/first"), accounts.CauseAccount("claude2")),
                            accounts.ProblemStep("first repair"))
                        second = accounts.StatusProblem("roles", "second diagnostic",
                            accounts.ProblemCause(second_kind, "second cause", accounts.CauseFile("/fixture/first" if same_home else "/fixture/second"),
                                accounts.CauseAccount("claude2" if same_account else "claude3")),
                            accounts.ProblemStep("second repair"), origin="role")
                        merged = accounts.merge_status_problems([first, second])
                        self.assertEqual(len(merged), 1 if first_kind == second_kind and same_account and same_home else 2)

    def test_mapped_test_identity_never_gets_account_note_advice(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        main = self.home / ".claude"
        main.mkdir()
        for name in accounts.SHARED:
            (main / name).mkdir()
            (directory / name).symlink_to(main / name, target_is_directory=True)
        identity = directory / ".claude.json"
        _ = identity.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
        stamp = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc).timestamp()
        os.utime(identity, (stamp, stamp))
        groups = role_map.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude2", "showrunners": "claude2"}))
        reading = accounts.read_status(source=accounts.StoredUsageOnly())
        lines = reading.notes
        self.assertEqual(len([line for line in lines if "test wrote" in line]), 1)
        self.assertFalse(any("account note" in line for line in lines))
        expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>",
                    "claude2: test sign-in is stored; a test wrote ~/.claude2/.claude.json at 2026-10-29 01:14 UTC; delete ~/.claude2/.claude.json"]
        self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(), expected)
        self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)

    def test_codex_startup_cause_keeps_reading_and_setup_in_one_line(self) -> None:
        directory = self.home / ".codex1"
        startup = agent_accounts.CodexStartupFailure(directory, "accounts.py setup codex1")
        status = accounts.CodexStatus("~/.codex1", directory, True, True,
            "codex1", "owner@provider.io", "workers", (startup.problem,), (), startup)
        usage = usage_store.StoredUsage(read_outcome=agent_accounts.UsageReadOutcome.ERROR,
            attempt_outcome=agent_accounts.UsageReadOutcome.ERROR, weekly=usage_store.StoredWindow(left=50, resets="2026-11-01T01:14:00+00:00", at="2026-10-29T01:14:00+00:00"))
        lines = [line for line in self.status_with([], [status], usage).notes if "codex1:" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("Codex did not start", lines[0])
        self.assertIn("reading from 01:14", lines[0])
        self.assertIn("accounts.py setup codex1", lines[0])

    def test_invalid_existing_claude_identity_requires_login_repair(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        identity = directory / ".claude.json"
        _ = identity.write_text('{"oauthAccount": {}}')
        status = accounts.AccountStatus("~/.claude2", directory, True, True, True,
            "claude2", "", "", (f"accounts: {identity} has an invalid oauthAccount identity",), (), (), (),
            state_source=accounts.account_directory_readiness("claude2").state_source)
        lines = [line for line in self.status_with([status], [], usage_store.StoredUsage()).notes if "claude2:" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("/login", lines[0])
        self.assertNotIn("accounts.py setup", lines[0])

    def test_same_test_identity_in_two_codex_homes_keeps_both_delete_steps(self) -> None:
        for index, name in enumerate((".codex", ".codex1")):
            directory = self.home / name
            directory.mkdir()
            identity = directory / "auth.json"
            payload = base64.urlsafe_b64encode(json.dumps({"email": "fixture@example.invalid"}).encode()).decode().rstrip("=")
            _ = identity.write_text(json.dumps({"tokens": {"id_token": f"fixture.{payload}.fixture"}}))
            stamp = datetime(2026, 10, 29, 1, 14 + index, tzinfo=timezone.utc).timestamp()
            os.utime(identity, (stamp, stamp))
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "codex1.md").write_text("---\nlogin: fixture@example.invalid\n---\n")
        reading = accounts.read_status(source=accounts.StoredUsageOnly())
        lines = [line for line in reading.notes if "test wrote" in line]
        self.assertEqual(len(lines), 2)
        for index, name in enumerate((".codex", ".codex1")):
            self.assertTrue(any(f"delete ~/{name}/auth.json" in line and f"01:{14 + index} UTC" in line for line in lines))
        expected = [
            "workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>",
            "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
            "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
            "codex1: test sign-in is stored; a test wrote ~/.codex/auth.json at 2026-10-29 01:14 UTC; delete ~/.codex/auth.json",
            "codex1: test sign-in is stored; a test wrote ~/.codex1/auth.json at 2026-10-29 01:15 UTC; delete ~/.codex1/auth.json",
            "codex1: login is stored in two homes; ~/.codex and ~/.codex1 are logged in as fixture@example.invalid; two homes on one login break token refresh; keep each login in one home",
        ]
        self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(), expected)
        self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)

    def test_real_domains_keep_account_repairs_through_status(self) -> None:
        for domain in ("notexample.com", "latest.io", "proton.me"):
            with self.subTest(domain=domain):
                lines = self.reading("fixture@" + domain).notes
                self.assertFalse(any("test wrote" in line or "delete " in line for line in lines))
                self.assertTrue(any("accounts.py cutover-home" in line and "add ~/rust" in line for line in lines))

    def test_reserved_login_reports_the_test_file_and_write_time_without_account_steps(self) -> None:
        for domain in ("invalid", "test", "example", "localhost", "one.invalid", "one.test",
                       "one.example", "one.localhost", "example.com", "example.net", "example.org",
                       "sub.example.com", "EXAMPLE.ORG."):
            for noted in (False, True):
                with self.subTest(domain=domain, noted=noted):
                    reading = self.reading("mesh-fixture@" + domain, noted=noted)
                    lines = [line for line in reading.notes if "test wrote" in line]
                    self.assertEqual(len(lines), 1)
                    line = lines[0]
                    self.assertIn("auth.json", line)
                    self.assertIn("2026-10-29 01:14 UTC", line)
                    self.assertIn("delete ~/.codex/auth.json", line)
                    self.assertFalse(any("cutover-home" in note or "add ~/rust" in note
                                         for note in reading.notes))
                    self.assertTrue(any("usage read failed" in note for note in reading.notes))
                    problems = cast(list[str], json.loads(accounts.render_status_json(reading))["problems"])
                    self.assertEqual(problems, [note.removeprefix("* ") for note in reading.notes])
                    self.assertIn(line, accounts.render_status_table(reading))

    def test_unnoted_legacy_login_merges_read_failure_and_keeps_both_repairs(self) -> None:
        reading = self.reading("fixture@notexample.com")
        lines = [line for line in reading.notes if "~/.codex" in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("* ~/.codex: usage unavailable; the usage read failed; no reading is stored yet; next try in 2 min", lines)
        lines = [line for line in lines if "no account note" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("no account note", lines[0])
        self.assertIn("login: fixture@notexample.com", lines[0])
        self.assertIn("accounts.py cutover-home", lines[0])
        self.assertNotIn("usage read failed", lines[0])

    def test_claude_test_signins_name_the_identity_file_in_each_home(self) -> None:
        home = self.home / ".claude"
        home.mkdir()
        numbered = self.home / ".claude2"
        numbered.mkdir()
        identity = numbered / ".claude.json"
        seed = self.home / ".claude.json"
        for path in (identity, seed):
            _ = path.write_text("fixture identity")
            at = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc).timestamp()
            os.utime(path, (at, at))
        status = accounts.AccountStatus("~/.claude2", numbered, True, True, True,
            "claude2", "fixture@example.invalid", "", (), (), (), ())
        with (patch.object(accounts, "account_statuses", return_value=[status]),
              patch.object(accounts, "codex_statuses", return_value=[]),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.home / ".codex1")),
              patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
              patch.object(accounts, "_stored_usage", return_value={}),
              patch.object(accounts, "_codex_home_login", return_value=""),
              patch.object(account, "claude_account", return_value=account.Account("claude", "fixture@example.invalid", "claude2"))):
            reading = accounts.read_status(source=accounts.StoredUsageOnly())
        lines = [line for line in reading.notes if "test wrote" in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("delete ~/.claude.json" in line for line in lines))
        self.assertTrue(any("delete ~/.claude2/.claude.json" in line for line in lines))
        self.assertTrue(all("2026-10-29 01:14 UTC" in line for line in lines))
        self.assertFalse(any("cutover-home" in line for line in reading.notes))

    def test_unreadable_test_file_time_keeps_the_deletion_step(self) -> None:
        with patch.object(Path, "stat", side_effect=PermissionError("fixture denied")):
            problem = accounts.test_login_problem("~/.codex", self.home / ".codex/auth.json", timezone.utc)
        line = accounts.render_status_problem(problem)
        self.assertIn("file time could not be read", line)
        self.assertIn("delete ~/.codex/auth.json", line)

    def test_status_failure_uses_the_problem_renderer_without_changing_launch_errors(self) -> None:
        reason = "accounts: cannot read role map from natedev; no usable map"
        for args in (("status",), ("status", "--json")):
            output, error = io.StringIO(), io.StringIO()
            with (patch.object(accounts, "read_status", side_effect=accounts.AccountsError(reason)),
                  redirect_stdout(output), redirect_stderr(error)):
                self.assertEqual(accounts.main(list(args)), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertTrue(error.getvalue().startswith("role map: unavailable;"))
            self.assertIn("no usable map", error.getvalue())
            self.assertIn("restore access", error.getvalue())
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(accounts, "exec_account", side_effect=accounts.AccountsError(reason)),
              redirect_stdout(output), redirect_stderr(error)):
            self.assertEqual(accounts.main(["exec", "claude2"]), 2)
        self.assertEqual(error.getvalue(), reason + "\n")

    def test_non_address_values_are_not_test_logins(self) -> None:
        for login in ("", "example.com", "owner", "owner@"):
            self.assertFalse(accounts.is_test_login(login))

    def test_check_problem_parts_keep_the_facts_and_an_action(self) -> None:
        directory = self.home / ".claude2"
        cases = (
            (f"accounts: {directory} is missing", "access", "missing", "accounts.py setup claude2"),
            (f"accounts: {directory} is not logged in", "signed out", "no readable sign-in", "/login"),
            (f"accounts: could not read {directory}: permission denied", "access", "permission denied", "permissions"),
            ("accounts: claude2 has no account note at /fixture/claude2.md", "note", "/fixture/claude2.md", "add the account note"),
            ("accounts: ~/.claude2 is logged in as owner@example.invalid, not claude2", "wrong login", "owner@example.invalid", "/login"),
            ("accounts: units has no account; run accounts.py move units <claudeN>", "role", "selects no account", "accounts.py move units"),
            ("accounts: units is on claude2, which is not set up on this machine; run accounts.py setup claude2", "role", "claude2", "accounts.py setup claude2"),
            ("accounts: cannot read role map from natedev; no usable map", "role", "no usable map", "restore access"),
        )
        for text, kind, fact, step in cases:
            with self.subTest(kind=kind, fact=fact):
                problem = accounts.status_check_problem(text, "claude2",
                    setup="run accounts.py setup claude2", login="run claude and /login",
                    access_paths=(str(directory),))
                self.assertIsInstance(problem.cause, accounts.ProblemCause)
                assert isinstance(problem.cause, accounts.ProblemCause)
                self.assertEqual(problem.cause.kind, kind)
                self.assertIn(fact, problem.cause.explanation)
                self.assertIsInstance(problem.step, accounts.ProblemStep)
                assert isinstance(problem.step, accounts.ProblemStep)
                self.assertIn(step, problem.step.instruction)
                self.assertEqual(accounts.render_status_problem(problem).split(":", 1)[0], problem.subject)

    def test_account_state_merge_uses_the_cause_after_wording_changes(self) -> None:
        directory = self.home / ".claude2"
        directory.symlink_to(self.home / "missing-folder")
        observed = accounts.account_directory_readiness("claude2").state_source
        assert isinstance(observed, accounts.ObservedStateCause)
        direct = accounts.StatusProblem("claude2", "new wording for the fault",
            accounts.ProblemCause("access", "missing folder", key=observed.key), accounts.ProblemStep("run accounts.py setup claude2"))
        summary = accounts.StatusProblem("claude2", "not set up", accounts.ProblemCause("access", "no folder"),
            accounts.ProblemStep("run accounts.py setup claude2"), True, origin="account state", state_source=observed)
        self.assertEqual(accounts.merge_status_problems([direct, summary]), [direct])

    def test_all_problem_parts_use_one_renderer_and_named_absence_variants(self) -> None:
        problem = accounts.StatusProblem("codex1", "usage unavailable",
            accounts.ProblemCause("usage", "the service is busy"),
            accounts.ProblemStep("next try after 01:24"), footnote=True)
        self.assertEqual(accounts.render_status_problem(problem),
            "* codex1: usage unavailable; the service is busy; next try after 01:24")
        self.assertEqual(accounts.render_status_problem(accounts.StatusProblem("codex1", "check failed",
            accounts.NoSeparateCause(), accounts.NoUserStep())), "codex1: check failed")


if __name__ == "__main__":
    _ = unittest.main()
