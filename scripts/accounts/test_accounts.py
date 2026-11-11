"""Behavioral contract for isolated Claude account directories."""

from __future__ import annotations

import sys
from pathlib import Path


from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.herdr import herdr_types
from scripts.whoami.note_file import note_name

import ast
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
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo
from collections.abc import Callable, Generator, Iterator
from typing import Literal, Self, TextIO, TypedDict, cast, final, override
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

from scripts.accounts.home_test_support import clear_notes_directory_cache, scratch_account_home, write_account_settings
from scripts.accounts import accounts, next_moves, refusal_watch, refusals, role_map
from scripts.accounts.nix_env_fake import FakeShellRefusal, fake_nix_loader, imports_with_nix_loader, installed_program
from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.test_models_registry import write_registry
from scripts.production import showrunners, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker
from scripts.agents import stop_resume
from scripts.agents import remote, stop_results
from scripts.accounts import pane_moves
from scripts.message import sessions
from scripts.herdr import herdr, worker_panes
from scripts.accounts import moves, worker_names
from scripts.whoami import account, note_file
from scripts.whoami import agent_accounts
from scripts.whoami import usage_store
from scripts.whoami.account_test_support import create_claude_account, stored_room
from scripts.whoami import quota_alert
from scripts.whoami import run_out
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

MODELS_RESOLVER_PATH = os.environ.get("PATH", os.defpath)

send_role_map_command = role_map.ssh_role_map
start_session_counts_fetch = cast(Callable[[], bool], getattr(accounts, "_start_session_counts_fetch"))
session_counts_cache = cast(Callable[[], Path], getattr(accounts, "_session_counts_cache"))
session_counts_document = cast(Callable[[dict[tuple[Path, str], dict[str, str]]], accounts.MachineSessionCounts],
    getattr(accounts, "_session_counts_document"))
decode_session_counts = cast(Callable[[str, str], accounts.MachineSessionCounts], getattr(accounts, "_decode_session_counts"))
machine_role_counts = cast(Callable[[tuple[accounts.StatusRow, ...], datetime],
    tuple[tuple[accounts.StatusRow, ...], tuple[str, ...]]], getattr(accounts, "_machine_role_counts"))
fetch_session_counts = cast(Callable[[], int], getattr(accounts, "_fetch_session_counts"))
local_codex_processes = cast(Callable[[], dict[str, accounts.CodexRoleProcess]],
    getattr(accounts, "_local_codex_processes"))


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
    install_signal_pid_guard()
    _ = unittest.enterModuleContext(patch.object(accounts, "_local_codex_processes", return_value={}))
    _ = unittest.enterModuleContext(patch.object(accounts, "_start_session_counts_fetch", return_value=True))
    _ = cast(object, unittest.enterModuleContext(patch.object(accounts, "import_module", side_effect=imports_with_nix_loader(fake_nix_loader()))))
    _ = unittest.enterModuleContext(patch.object(role_map, "is_role_map_host", return_value=True))


def accounts_reader_calls(source: str) -> dict[str, set[str]]:
    """Collect each method called on the accounts configuration."""
    readers: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        receiver = node.func.value
        configured = isinstance(receiver, ast.Name) and receiver.id == "account_config"
        if isinstance(receiver, ast.Call):
            configured = (isinstance(receiver.func, ast.Name) and receiver.func.id == "read_account_settings"
                or isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "read_account_settings"
                or isinstance(receiver.func, ast.Attribute) and isinstance(receiver.func.value, ast.Name)
                and receiver.func.value.id == "conf" and receiver.func.attr == "read"
                and bool(receiver.args) and isinstance(receiver.args[0], ast.Constant)
                and receiver.args[0].value == "accounts")
        if configured:
            readers.setdefault(node.args[0].value, set()).add(node.func.attr)
    return readers


class AccountsConfigurationTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        clear_notes_directory_cache()
        self.addCleanup(clear_notes_directory_cache)

    def test_each_accounts_key_has_a_typed_reader_and_parses(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "accounts.conf"
            _ = config.write_bytes((root / "config/accounts.conf").read_bytes())
            with patch.dict(os.environ, {"CONF_DIR": temporary}):
                account_config = conf.read("accounts")
                configured = account_config.values()
                readers: dict[str, set[str]] = {}
                sources = [*(root / "scripts/accounts").glob("*.py"), *(root / "scripts/whoami").glob("*.py")]
                for source in sources:
                    if source.name.startswith("test_"):
                        continue
                    for key, kinds in accounts_reader_calls(source.read_text()).items():
                        readers.setdefault(key, set()).update(kinds)
                self.assertEqual(set(configured), set(readers))
                for key, kinds in readers.items():
                    with self.subTest(key=key):
                        self.assertEqual(len(kinds), 1)
                        kind = next(iter(kinds))
                        self.assertIn(kind, ("whole", "number", "text"))
                        if kind == "whole":
                            self.assertEqual(account_config.whole(key), int(configured[key]))
                        elif kind == "number":
                            self.assertEqual(account_config.number(key), float(configured[key]))
                        else:
                            self.assertEqual(account_config.text(key), configured[key])

    def test_accounts_reader_scan_exposes_a_wrong_usage_age_method(self) -> None:
        source = Path(accounts.__file__).read_text()
        changed = source.replace('.whole("usage_stale_minutes")', '.words("usage_stale_minutes")', 1)
        readers = accounts_reader_calls(changed)
        self.assertEqual(readers["usage_stale_minutes"], {"whole", "words"})
        self.assertNotEqual(readers, accounts_reader_calls(source))
        self.assertNotIn("unrelated", accounts_reader_calls('other.words("unrelated")'))

        original_read = Path.read_text

        def mutated_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            return changed if path == Path(accounts.__file__) else original_read(path, encoding=encoding, errors=errors)

        result = unittest.TestResult()
        with patch.object(Path, "read_text", new=mutated_read):
            result = cast(unittest.TestResult, AccountsConfigurationTests("test_each_accounts_key_has_a_typed_reader_and_parses").run(result))
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn("usage_stale_minutes", str(result.failures[0][0]))

    def test_usage_age_changes_without_a_module_reload(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "accounts.conf"
            contents = (root / "config/accounts.conf").read_text()
            _ = config.write_text(contents)
            with patch.dict(os.environ, {"CONF_DIR": temporary}):
                now = datetime(2026, 11, 2, tzinfo=timezone.utc)
                self.assertTrue(cast(Callable[[datetime, datetime], bool], getattr(accounts, "_reading_is_old"))(now - timedelta(minutes=11), now))
                _ = config.write_text(contents.replace("usage_stale_minutes=10", "usage_stale_minutes=12"))
                self.assertFalse(cast(Callable[[datetime, datetime], bool], getattr(accounts, "_reading_is_old"))(now - timedelta(minutes=11), now))


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


class McpProjectFixture(TypedDict):
    disabledMcpServers: list[str]


class McpFixtureIdentity(TypedDict):
    emailAddress: str


class McpConfigurationFixture(TypedDict):
    oauthAccount: McpFixtureIdentity
    mcpServers: dict[str, object]
    projects: dict[str, McpProjectFixture]


@final
class AccountsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.main_dir = Path()
        self.trust_config = Path()
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
            "STAFF_FILE": str(self.root / "staff.json"),
            "PATH": os.defpath,
            # The wall times these tests pass are Pacific; the user's zone setting must not change them.
            "USER_ZONE": "America/Los_Angeles",
        }
        environment_context: object = cast(
            object,
            self.enterContext(scratch_account_home(self.home, environment)),
        )
        del environment_context
        counts_cache = session_counts_cache()
        counts_cache.parent.mkdir(parents=True, exist_ok=True)
        _ = counts_cache.write_text(json.dumps({"machine": remote.other_machine(),
            "at": datetime.now(timezone.utc).isoformat(), "accounts": {}}))
        _ = (self.root / "staff.json").write_text('{"schema": 2, "rows": []}\n')
        self.trust_config = accounts.trust_settings_file()
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.root / "quota_alert.json"))
        _ = self.enterContext(patch.object(run_out, "READINGS_LOG", self.root / "pace.jsonl"))
        self.live = {}
        self.live_reads = self.enterContext(patch.object(agent_accounts, "claude_live", side_effect=self.live_report))
        self.codex_reads = cast(AsyncMock, self.enterContext(patch.object(
            agent_accounts, "codex_live", return_value=agent_accounts.Report("Codex", problem="Not logged in"),
        )))
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home",
            return_value=agent_accounts.CodexHomePrepared()))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=[
            showrunners.Showrunner(session="fixture", socket="", name="fixture", zone="America/Los_Angeles", doc=""),
        ]))

    def live_report(self, config_dir: Path) -> agent_accounts.Report:
        return self.live.get(config_dir, agent_accounts.Report(
            "Claude", problem="Account unavailable (claude auth status failed)",
        ))

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
              patch.object(sessions, "running_sessions", return_value=[owner]),
              patch.object(import_module("scripts.message.sessions"), "live_sessions", return_value=[owner]),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.main_dir)),
              patch.object(accounts, "pick", side_effect=chosen),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.home / ".codex1")),
              patch.object(accounts, "read_role_map", return_value=role_map.RoleMap({}, "codex1")),
              patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                  "codex1", self.home / ".codex1", "fixture@notexample.com"))):
            code, output, error = self.run_cli("--detail")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("live-identity", output)
        self.assertIn("Live director", output)
        self.assertIn("claude2", output)
        self.assertIn("Codex workers: codex1", output)
        self.assertIn("on refusal: codex3", output)
        self.assertTrue(output.splitlines()[0].startswith("| Folder"))




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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_shows_sessions_workers_and_moves_only_with_detail(self) -> None:
        _ = self.workers_fixture()
        from scripts.message import sessions
        self.login_directory("claude2", "main@example.invalid", default=True)
        owner = sessions.SessionRecord(pid=12345, sessionId="live-identity", name="Live director",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
        moves.append_move(moves.FailedMove(at="2026-10-14T12:00:00+00:00", tool="claude",
            subject="moved-session", move_id="move-one", kind="failed", reason="still on claude1"))
        # Each run stores a usage reading at the current time; the two outputs must match.
        with (patch.object(accounts, "datetime", RoleWriteTime),
              patch.object(sessions, "live_sessions", return_value=[owner]),
              patch.object(sessions, "running_sessions", return_value=[owner]),
              patch.object(import_module("scripts.message.sessions"), "live_sessions", return_value=[owner]),
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

    def test_stored_json_counts_local_members_per_account_and_role_without_discovery(self) -> None:
        self.assert_stored_role_counts("unit marks")

    def test_stored_role_counts_follow_retained_unit_references_after_clear(self) -> None:
        self.assert_stored_role_counts("cleared unit marks")

    def test_stored_role_counts_follow_retained_unit_references_after_clear_without_marks(self) -> None:
        self.assert_stored_role_counts("cleared staff identity")

    def test_stored_role_counts_resolve_a_unit_with_only_its_staff_identity(self) -> None:
        self.assert_stored_role_counts("staff identity")

    def test_stored_role_counts_skip_damaged_launch_history_and_keep_loading(self) -> None:
        self.assert_stored_role_counts("damaged history")

    def test_stored_role_counts_keep_valid_siblings_of_an_empty_launch_entry(self) -> None:
        self.assert_stored_role_counts("damaged sibling")

    def test_stored_role_counts_skip_a_newer_launch_with_a_non_string_config_dir(self) -> None:
        self.assert_stored_role_counts("damaged folder")

    def test_stored_role_counts_keep_a_unit_without_legacy_launch_flags(self) -> None:
        self.assert_stored_role_counts("legacy flags")

    def test_stored_role_counts_treat_unknown_claude_activity_as_idle(self) -> None:
        self.assert_stored_role_counts("unknown status")

    def test_stored_role_counts_use_the_native_current_session_without_a_launch_record(self) -> None:
        self.assert_stored_role_counts("native session")

    def test_missing_or_unreadable_staff_supplies_no_group_members(self) -> None:
        from scripts.production import staff
        from scripts.herdr import pane_launch
        from scripts.agents import inventory
        read = cast(Callable[[], tuple[dict[tuple[Path, str], dict[str, str]], tuple[str, ...]]],
                    getattr(accounts, "_local_session_counts"))
        records = self.root / "private-records"
        records.mkdir()
        owner = sessions.SessionRecord(pid=991101, sessionId="runner", name="runner", status="busy",
            messagingSocketPath="/private.sock", updatedAt=1, tmux="", formerNames=[])
        for missing in (True, False):
            failure = staff.StaffUnreadable("private staff unavailable")
            if missing:
                failure.__cause__ = FileNotFoundError("private file absent")
            with (self.subTest(missing=missing),
                  patch.object(staff, "local_snapshot", side_effect=failure),
                  patch.object(pane_launch, "record_directory", return_value=records),
                  patch.object(sessions, "live_sessions", return_value=[owner]),
                  patch.object(sessions, "running_sessions", return_value=[owner]),
                  patch.object(inventory, "showrunner_registrations", return_value=inventory.ShowrunnerRegistrations((
                      inventory.ShowrunnerRegistration("runner", "runner", ""),))),
                  patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.main_dir)),
                  redirect_stderr(io.StringIO()) as errors):
                counts, _inputs = read()
            self.assertEqual(counts, {})
            self.assertEqual(errors.getvalue(), "" if missing else
                             "accounts: unit counts are incomplete: private staff unavailable\n")

    def assert_stored_role_counts(self, evidence: Literal[
        "unit marks", "staff identity", "damaged history", "damaged sibling", "damaged folder", "legacy flags",
        "cleared unit marks", "cleared staff identity", "unknown status", "native session",
    ]) -> None:
        from scripts.message import sessions
        from scripts.agents import inventory
        from scripts.herdr import pane_launch
        from scripts.production import staff
        from scripts.production.staff_test_support import unit_membership, fixture_runtime_id

        _ = cast(object, self.enterContext(patch.dict(os.environ, {"STAFF_FILE": str(self.root / "staff.json")})))
        members = {name: unit_membership("fixture", name) for name in ("a", "b", "c")}
        members["a"] = staff.set_worktree(members["a"].id,
            staff.PresentWorktree(remote.local_machine(), self.root), expected=members["a"].worktree)

        self.login_directory("claude1", "units@notexample.com")
        self.login_directory("claude2", "runners@notexample.com")
        self.write_note("codex1", "workers@notexample.com")
        self.codex_login("workers@notexample.com", self.home / ".codex1")
        self.write_note("codex2", "spare@notexample.com")
        self.codex_login("spare@notexample.com", self.home / ".codex2")
        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text('{"workers": "codex1"}')
        records = self.root / "pane-records"
        records.mkdir()
        owners: list[sessions.SessionRecord] = []
        launches: dict[str, pane_launch.PaneLaunch] = {}
        for pane, label, unit in (("unit-a", "claude1", "a"), ("unit-b", "claude1", "b"),
                                  ("runner-a", "claude2", ""), ("runner-b", "claude1", ""),
                                  ("stopped", "claude1", "c"), ("plain", "claude1", "")):
            identity = pane
            marked = {"SHOWRUNNER_UNIT_ID": unit} if unit else {}
            if unit:
                identity = members[unit].current_session
            if evidence == "staff identity" and unit:
                # No unit mark: the session id is the row's current session.
                identity, marked = members[unit].current_session, {}
            stored_identity = identity
            if evidence in ("cleared unit marks", "cleared staff identity") and unit:
                stored_identity = members[unit].id
                current = staff.update_current_session(members[unit].current_session,
                    staff.StaffId(fixture_runtime_id("account-clear-" + unit)))
                identity = current.current_session
                if evidence == "cleared staff identity":
                    marked = {}
            launches[identity] = pane_launch.PaneLaunch(pane_id=identity, session_id=stored_identity,
                config_dir=str(self.home / ("." + label)), cwd=str(self.root), name=identity,
                model="", remote_control=False, flags_read=True, written_at="2026-10-01T00:00:00Z",
                environment=marked)
            if pane != "stopped":
                owners.append(sessions.SessionRecord(pid=12345 + len(owners), sessionId=identity, name=identity,
                    messagingSocketPath="/fake.sock", updatedAt=1, tmux="", formerNames=[]))
                state = {"unit-a": "busy", "unit-b": "shell", "runner-a": "idle"}.get(pane)
                if state is not None:
                    owners[-1]["status"] = state
                elif evidence == "unknown status":
                    owners[-1]["status"] = "unrecognized"
        # A repeated registry identity is still one session.
        owners.append(owners[0])
        history: dict[str, object] = {key: launch for key, launch in launches.items()
            if evidence != "native session" or key not in {row.current_session for row in members.values()}}
        if evidence == "legacy flags":
            history[members["a"].current_session] = {key: value for key, value in launches[members["a"].current_session].items() if key != "flags_read"}
        elif evidence == "damaged sibling":
            history["broken-pane"] = {}
        elif evidence == "damaged folder":
            damaged: dict[str, object] = dict(launches[members["a"].current_session])
            damaged.update(config_dir=[], written_at="2026-10-02T00:00:00Z")
            history["broken-pane"] = damaged
        _ = (records / "fixture.json").write_text(json.dumps(history))
        if evidence == "damaged history":
            _ = (records / "damaged.json").write_text('{"broken-pane": {}}')
        from scripts.herdr import herdr, worker_panes
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.production.staff_test_support import fixture_worker
        world = PaneWorld()
        world.add(members["a"], "w1:p0")
        for index in range(4):
            child = fixture_worker(f"codex-{index}", members["a"].id,
                                   backend=staff.CodexBackend(self.home / ".codex1"))
            world.add(child, f"w1:p{index + 1}", family="codex")
            if index < 2:
                world.locations[f"w1:p{index + 1}"]["agent_status"] = "working"
        for identity in ("runner-a", "runner-b"):
            _ = fixture_member("showrunner", name=identity, runtime_id=fixture_runtime_id(identity))
        stored = staff.local_snapshot()
        membership = replace(stored, rows=tuple(
            replace(row, current_session=staff.StaffId(row.name)) if row.name in ("runner-a", "runner-b") else row
            for row in stored.rows))
        runners = inventory.ShowrunnerRegistrations((
            inventory.ShowrunnerRegistration("runner-a", "runner-a", ""),
            inventory.ShowrunnerRegistration("runner-b", "runner-b", ""),
        ))
        units = accounts.RoleSessionCountsDocument(busy=1, idle=1, paused=0, exited=1, total=3)
        runners_count = accounts.RoleSessionCountsDocument(busy=0, idle=1, paused=0, exited=0, total=1)
        selected_runners = accounts.RoleSessionCountsDocument(busy=0, idle=1, paused=0, exited=1, total=2)
        cases: tuple[tuple[str, dict[str, tuple[list[str], list[accounts.RoleSessionCountsDocument]]]], ...] = (
            ("claude2", {"claude1": (["units", "showrunners"], [units, runners_count]), "claude2": (["showrunners"], [selected_runners])}),
            ("claude1", {"claude1": (["units", "showrunners"], [units, selected_runners]), "claude2": (["showrunners"], [runners_count])}),
        )
        def live_directory(pid: int) -> account.ConfiguredClaudeFolder:
            identity = next(owner["sessionId"] for owner in owners if owner["pid"] == pid)
            entry = launches[identity]
            assert "config_dir" in entry
            return account.ConfiguredClaudeFolder(Path(entry["config_dir"]))

        retained_bytes = (records / "fixture.json").read_bytes()
        for runner_label, expected in cases:
            _ = accounts.set_group_account("units", "claude1", why="fixture")
            _ = accounts.set_group_account("showrunners", runner_label, why="fixture")
            with (self.subTest(runner_label=runner_label),
                  patch.object(sessions, "running_sessions", return_value=owners),
                  patch.object(staff, "local_snapshot", return_value=membership),
                  patch.object(pane_launch, "record_directory", return_value=records),
                  patch.object(accounts, "_session_config_dir", side_effect=live_directory),
                  # Even on a client machine, counts use only the retained local record.
                  patch.object(staff, "snapshot", side_effect=AssertionError("no owner fetch")),
                  patch.object(inventory, "showrunner_registrations", return_value=runners),
                  patch.object(worker_panes, "_client", return_value=world),
                  patch.object(herdr, "native_process", side_effect=world.native_process),
                  patch.object(herdr, "_startup_processes", side_effect=world.descendants),
                  patch.object(subprocess, "run", side_effect=AssertionError("no process or network discovery")),
                  patch.object(subprocess, "Popen", side_effect=AssertionError("no process or network discovery"))):
                code, text, error = self.run_cli("status", "--stored", "--json")
            self.assertEqual((code, error), (0, ""))
            document = cast(accounts.StatusDocument, json.loads(text))
            values = {row["label"]: (row["roles"], row["role_counts"]) for row in document["accounts"]}
            self.assertEqual(values, {**expected, "codex1": (["workers"], [{"busy": 2, "paused": 0, "exited": 0, "idle": 2, "total": 4}]), "codex2": (["spare"], [None])})
            self.assertIn(str(records / "fixture.json"), document["session_inputs"])
            self.assertEqual((records / "fixture.json").read_bytes(), retained_bytes)

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_board_and_group_move_share_staff_membership_and_the_live_process_account(self) -> None:
        from scripts.herdr import pane_launch
        from scripts.agents import inventory
        from scripts.production.staff_test_support import unit_membership

        for label in ("claude2", "claude4"):
            self.login_directory(label, label + "@example.invalid")
        registry = write_registry(self.root / "producer-models.conf", {"producers": {
            "producer-claude": "claude:opus:max", "producer-codex": "codex:gpt-test:high"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry), "PATH": MODELS_RESOLVER_PATH})))
        cases: tuple[tuple[staff.Kind, accounts.ClaudeGroup, str, str, str], ...] = (
            ("unit", "units", "claude", "", ""), ("producer", "showrunners", "claude", "", ""),
            ("producer", "showrunners", "codex", "", ""),
            ("unit", "units", "claude", "live", ""), ("producer", "showrunners", "claude", "live", ""),
            ("unit", "units", "claude", "stopped", ""), ("producer", "showrunners", "claude", "stopped", ""),
            ("unit", "units", "claude", "", "paused"),
            ("showrunner", "showrunners", "claude", "", "paused"),
            ("unit", "units", "claude", "", "wrapped"),
            ("showrunner", "showrunners", "claude", "", "retired"),
            ("unit", "units", "claude", "", "remote document"),
            ("showrunner", "showrunners", "claude", "", "remote worktree"),
            ("producer", "showrunners", "claude", "", "paused"))
        for kind, group, family, missing_settings, state in cases:
            with self.subTest(kind=kind, family=family, missing_settings=missing_settings, state=state):
                member = (unit_membership("fixture", "models") if kind == "unit" else
                    fixture_member(kind, name=kind + "-" + family))
                if state == "paused":
                    member = replace(member, paused=staff.Paused("now", "user", "hold"))
                elif state == "wrapped":
                    member = replace(member, doc=staff.WrappedDoc("natedev", self.root / "plan.md"))
                elif state == "retired":
                    member = replace(member, worktree=staff.RemovedWorktree("natedev", self.root))
                elif state == "remote document":
                    member = replace(member, doc=staff.LiveDoc("mac", self.root / "plan.md"))
                elif state == "remote worktree":
                    member = replace(member, worktree=staff.PresentWorktree("mac", self.root))
                accounts.groups_file().unlink(missing_ok=True)
                _ = accounts.set_group_account(group, "claude2", why="fixture")
                records = self.root / "member-launches"
                records.mkdir(exist_ok=True)
                launch = pane_launch.PaneLaunch(pane_id="models-pane", session_id=member.id,
                    config_dir=str(self.home / ".claude2"), cwd=str(self.root), name=member.name,
                    model="", remote_control=False, environment={}, flags_read=True, written_at="2026-11-05T06:00:00Z")
                _ = (records / "fixture.json").write_text(json.dumps({"models-pane": launch}))
                owner = sessions.SessionRecord(pid=990901, sessionId=member.current_session, name=member.name,
                    messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status="busy")
                members = [member]
                live = [owner]
                if missing_settings:
                    invalid = fixture_member("producer", name="missing-settings-producer")
                    members.append(invalid)
                    if missing_settings == "live":
                        live.append(sessions.SessionRecord(pid=990902, sessionId=invalid.current_session, name=invalid.name,
                            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status="busy"))
                membership = staff.StaffSnapshot(tuple(members))
                with (patch.object(staff, "local_snapshot", return_value=membership),
                      patch.object(sessions, "running_sessions", return_value=live),
                      patch.object(pane_launch, "record_directory", return_value=records),
                      patch.object(accounts, "_session_config_dir", return_value=account.ConfiguredClaudeFolder(self.home / ".claude4")),
                      patch.object(inventory, "listing", side_effect=AssertionError("timer membership was read")),
                      patch.object(accounts, "move_sessions", side_effect=AssertionError("preview moved a session"))):
                    reading = accounts.read_status(source=accounts.StoredUsageOnly())
                    with redirect_stderr(io.StringIO()) as errors:
                        result = accounts.move_group(group, "claude2", dry_run=True)
                    unproved = result
                    if state:
                        with patch.object(accounts, "_session_config_dir", return_value=account.ProcessEnvironmentUnread("fixture")):
                            unproved = accounts.move_group(group, "claude2", dry_run=True)
                if missing_settings:
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn("missing-settings-producer", errors.getvalue())
                    self.assertIn("invalid producer settings", errors.getvalue())
                else:
                    self.assertEqual(errors.getvalue(), "")
                document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
                rows = {row["label"]: row for row in document["accounts"]}
                self.assertEqual(rows["claude4"]["roles"], [group] if family == "claude" else [])
                if state == "paused":
                    self.assertEqual(rows["claude4"]["role_counts"], [{"busy": 0, "paused": 1, "exited": 0, "idle": 0, "total": 1}])
                else:
                    self.assertEqual(rows["claude4"]["role_counts"], [{"busy": 1, "paused": 0, "exited": 0, "idle": 0, "total": 1}] if family == "claude" else [])
                self.assertEqual(rows["claude2"]["role_counts"], [{"busy": 0, "paused": 0, "exited": 0, "idle": 0, "total": 0}])
                self.assertIsInstance(result, accounts.GroupMoveDryRun)
                assert isinstance(result, accounts.GroupMoveDryRun)
                if kind == "producer" and state == "paused":
                    self.assertEqual(result.session_ids, ())
                else:
                    self.assertEqual(result.session_ids, (member.current_session,) if family == "claude" else ())
                if family == "claude" and not (kind == "producer" and state == "paused"):
                    self.assertIn(f"{member.name}: {member.current_session} · stop, then resume on claude2 (from claude4)", result.lines)
                if state:
                    self.assertIsInstance(unproved, accounts.GroupMoveDryRun)
                    assert isinstance(unproved, accounts.GroupMoveDryRun)
                    self.assertEqual(unproved.session_ids, result.session_ids)

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_partial_move_counts_actual_accounts_and_stopped_members_once(self) -> None:
        from scripts.herdr import pane_launch
        from scripts.agents import inventory
        from scripts.production.staff_test_support import unit_membership

        for label in ("claude2", "claude4"):
            self.login_directory(label, label + "@example.invalid")
        members = [unit_membership("fixture", name) for name in ("a", "b", "c", "down")]
        _ = accounts.set_group_account("units", "claude2", why="fixture")
        _ = accounts.set_group_account("showrunners", "claude2", why="fixture")
        records = self.root / "private-launches"
        records.mkdir()
        launches = {member.current_session: pane_launch.PaneLaunch(
            pane_id=member.name, session_id=member.id, config_dir=str(self.home / ".claude4"),
            cwd=str(self.root), name=member.name, model="", remote_control=False,
            environment={}, flags_read=True, written_at="2026-11-05T06:00:00Z", ended=True)
            for member in members}
        _ = (records / "launches.json").write_text(json.dumps(launches))
        live = [sessions.SessionRecord(pid=900 + index, sessionId=member.current_session, name=member.name,
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status=state)
            for index, (member, state) in enumerate(zip(members, ("busy", "busy", "shell")))]
        def live_directory(pid: int) -> account.ConfiguredClaudeFolder:
            return account.ConfiguredClaudeFolder(self.home / (".claude2" if pid == 901 else ".claude4"))

        membership = staff.local_snapshot()
        with (patch.object(staff, "local_snapshot", return_value=membership) as staff_read,
              patch.object(sessions, "running_sessions", return_value=[*live, live[0]]) as registry_read,
              patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(pane_launch, "record_directory", return_value=records),
              patch.object(inventory, "showrunner_registrations", return_value=inventory.ShowrunnerRegistrations(())),
              patch.object(accounts, "_session_config_dir", side_effect=live_directory) as settings_read):
            reading = accounts.read_status(source=accounts.StoredUsageOnly(), detail=True)
        staff_read.assert_called_once_with()
        registry_read.assert_called_once_with()
        self.assertEqual(settings_read.call_count, 3)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        rows = {row["label"]: row for row in document["accounts"]}
        self.assertEqual(rows["claude2"]["roles"], ["units", "showrunners"])
        self.assertEqual(rows["claude2"]["selected_roles"], ["units", "showrunners"])
        self.assertEqual(rows["claude2"]["role_counts"], [
            {"busy": 1, "paused": 0, "exited": 1, "idle": 0, "total": 2},
            {"busy": 0, "paused": 0, "exited": 1, "idle": 0, "total": 1}])
        self.assertEqual(rows["claude4"]["roles"], ["units"])
        self.assertEqual(rows["claude4"]["selected_roles"], [])
        self.assertEqual(rows["claude4"]["role_counts"], [{"busy": 1, "idle": 1, "paused": 0, "exited": 0, "total": 2}])
        self.assertEqual(rows["claude4"]["sessions"], sorted([
            {"session": members[0].current_session, "state": "busy"},
            {"session": members[2].current_session, "state": "idle"},
        ], key=lambda entry: entry["session"]))
        self.assertIn({"session": members[3].current_session, "state": "exited"}, rows["claude2"]["sessions"])
        table = accounts.render_status_table(reading)
        for text in ("units ✓ 2 (busy 1, idle 0, paused 0, exited 1)", "units 2 (busy 1, idle 1, paused 0, exited 0)",
                     "showrunners ✓ 1 (busy 0, idle 0, paused 0, exited 1)"):
            self.assertIn(text, table)
            self.assertIn(text, "\n".join(reading.detail_lines))

        from scripts.production import live_units, unit_phases, unit_lookup

        self.enterContext(patch.dict(os.environ, {"UNIT_HISTORY_DIR": str(self.root / "status-history"),
                                                "UNIT_QUEUE_STATE_DIR": str(self.root / "unit-queues")}))
        states = [("busy", "", "busy"), ("idle", "", "idle"), ("shell", "", "idle"),
                  ("busy", "pause", "paused"), ("", "pause", "paused"),
                  ("busy", "exit", "exited"), ("", "exit", "exited"),
                  ("busy", "both", "exited"), ("", "both", "exited"), ("", "", "exited")]
        for kind in ("unit", "showrunner"):
            for activity, hold, expected in states:
                with self.subTest(kind=kind, activity=activity, hold=hold):
                    source = members[0] if kind == "unit" else next(row for row in membership.rows if row.kind == "showrunner")
                    member = replace(source, paused=staff.Paused("2026-11-08", "fixture", "hold") if hold in ("pause", "both") else staff.Active(),
                                     exited=staff.Exited("2026-11-08", "fixture", "stop") if hold in ("exit", "both") else staff.Active())
                    observed = [sessions.SessionRecord(pid=900, sessionId=member.current_session, name=member.name,
                        messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status=activity)] if activity else []
                    parent = membership.parent_of(member)
                    snapshot = staff.StaffSnapshot((member, parent) if parent is not None else (member,))
                    with (patch.object(staff, "local_snapshot", return_value=snapshot),
                          patch.object(sessions, "running_sessions", return_value=observed),
                          patch.object(pane_launch, "record_directory", return_value=records),
                          patch.object(accounts, "_session_config_dir", side_effect=live_directory)):
                        status = accounts.read_status(source=accounts.StoredUsageOnly())
                    role = "units" if kind == "unit" else "showrunners"
                    label = "claude4" if activity else "claude2"
                    document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(status)))
                    account_row = next(row for row in document["accounts"] if row["label"] == label)
                    self.assertEqual(account_row["role_counts"][account_row["roles"].index(role)],
                                     {"busy": int(expected == "busy"), "idle": int(expected == "idle"),
                                      "paused": int(expected == "paused"), "exited": int(expected == "exited"), "total": 1})
                    if kind == "unit":
                        assert parent is not None
                        listing = unit_lookup.LabeledProductions({}, {parent.name: (member.name,)})
                        with (patch.object(staff, "snapshot", return_value=snapshot),
                              patch.object(sessions, "running_sessions", return_value=observed)):
                            selected = live_units.units_in(parent.name, listing)
                            rendered = unit_phases.table(selected, {})
                        self.assertIn(f"| {member.name} | {expected} |", rendered)

    def test_native_codex_counts_share_one_conversation_and_activity_read(self) -> None:
        from scripts.herdr import herdr

        agents = [herdr.AgentOccupant(pane_id="w1:p1", agent="codex", agent_status=herdr.AgentStatus.WORKING,
                     agent_session=herdr.ReportedAgentSession("busy-thread")),
                  herdr.AgentOccupant(pane_id="w1:p2", agent="codex", agent_status=herdr.AgentStatus.IDLE,
                     agent_session=herdr.ReportedAgentSession("idle-thread"))]
        with (patch.object(herdr, "herdr_binary", return_value="/nonexistent/herdr"),
              patch.object(herdr.Herdr, "agents", return_value=agents) as activity,
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({
                  "busy-thread": (990,), "idle-thread": (991,)}, ())) as conversations):
            observed = local_codex_processes()
        activity.assert_called_once_with()
        conversations.assert_called_once_with(wanted="busy-thread")
        self.assertEqual(observed, {"busy-thread": accounts.CodexRoleProcess(990, "busy"),
                                    "idle-thread": accounts.CodexRoleProcess(991, "idle")})

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_exited_directors_use_selected_accounts_and_absent_codex_workers_do_not_count(self) -> None:
        from scripts.herdr import pane_launch
        from scripts.production.staff_test_support import unit_membership, fixture_worker

        for label in ("claude2", "claude4"):
            self.login_directory(label, label + "@example.invalid")
        for label in ("codex1", "codex2"):
            self.write_note(label, label + "@example.invalid")
            self.codex_login(label + "@example.invalid", self.home / ("." + label))
        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text('{"workers": "codex2"}')
        member = unit_membership("fixture", "down")
        worker = fixture_worker("down-worker", member.id, backend=staff.CodexBackend(self.home / ".codex1"))
        _ = fixture_worker("old-worker", member.id, backend=staff.CodexBackend(self.home / ".codex1"))
        _ = accounts.set_group_account("units", "claude2", why="fixture")
        _ = accounts.set_group_account("showrunners", "claude4", why="fixture")
        records = self.root / "no-launch-records"
        records.mkdir()
        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(pane_launch, "record_directory", return_value=records),
              patch.object(accounts, "_local_codex_processes", return_value={})):
            reading = accounts.read_status(source=accounts.StoredUsageOnly(), detail=True)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        rows = {row["label"]: row for row in document["accounts"]}
        exited = {"busy": 0, "idle": 0, "paused": 0, "exited": 1, "total": 1}
        for label in ("claude2", "claude4"):
            self.assertEqual(rows[label]["role_counts"], [exited])
        self.assertEqual(rows["codex2"]["role_counts"], [
            {"busy": 0, "idle": 0, "paused": 0, "exited": 0, "total": 0}])
        self.assertEqual(rows["codex1"]["roles"], ["spare"])
        self.assertNotIn("shell", accounts.render_status_table(reading))
        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(pane_launch, "record_directory", return_value=records),
              patch.object(accounts, "_local_codex_processes", return_value={
                  worker.current_session: accounts.CodexRoleProcess(990, "busy")}) as native,
              patch.object(account, "process_codex_home",
                           return_value=account.ProcessCodexHome(self.home / ".codex1")) as home):
            reading = accounts.read_status(source=accounts.StoredUsageOnly())
        native.assert_called_once_with()
        home.assert_called_once_with(990)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        rows = {row["label"]: row for row in document["accounts"]}
        self.assertEqual(rows["codex1"]["role_counts"], [
            {"busy": 1, "idle": 0, "paused": 0, "exited": 0, "total": 1}])
        self.assertEqual(rows["codex2"]["role_counts"], [
            {"busy": 0, "idle": 0, "paused": 0, "exited": 0, "total": 0}])

        claude_worker = fixture_worker("live-claude-worker", member.id)
        for activity, hold in (("busy", "pause"), ("idle", "exit")):
            with self.subTest(worker_activity=activity, staff_hold=hold):
                if hold == "pause":
                    _ = staff.pause(claude_worker.id, "hold worker")
                else:
                    _ = staff.exit(claude_worker.id, "stop worker")
                observed = sessions.SessionRecord(pid=991, sessionId=claude_worker.current_session, name=claude_worker.name,
                    messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status=activity)
                with (patch.object(sessions, "running_sessions", return_value=[observed]),
                      patch.object(pane_launch, "record_directory", return_value=records),
                      patch.object(accounts, "_session_config_dir", return_value=account.ConfiguredClaudeFolder(self.home / ".claude4"))):
                    reading = accounts.read_status(source=accounts.StoredUsageOnly())
                document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
                worker_account = next(row for row in document["accounts"] if row["label"] == "claude4")
                self.assertEqual(worker_account["role_counts"][worker_account["roles"].index("workers")],
                                 {"busy": int(activity == "busy"), "idle": int(activity == "idle"),
                                  "paused": 0, "exited": 0, "total": 1})



    def test_mac_account_counts_read_the_kernel_without_a_process_for_each_member(self) -> None:
        read_directory = cast(Callable[[int], account.ConfiguredClaudeFolder | account.NoAccountSetting |
            account.ProcessEnvironmentUnread], getattr(accounts, "_session_config_dir"))
        payload = ((1).to_bytes(4, sys.byteorder, signed=True)
            + b"/fixture/claude\0\0claude\0CLAUDE_CONFIG_DIR=/fixture/.claude4\0")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(account, "kern_procargs2", return_value=payload) as kernel,
              patch.object(subprocess, "run", side_effect=AssertionError("no process for each member"))):
            observed = read_directory(990001)
        self.assertEqual(observed, account.ConfiguredClaudeFolder(Path("/fixture/.claude4")))
        kernel.assert_called_once_with(990001)

    def test_peer_members_add_a_role_to_an_unselected_account(self) -> None:
        now = datetime.now(timezone.utc)
        row = replace(SharedStatusDocumentTests().row(), label="claude4", tool="claude", roles=(), role_counts=())
        peer = remote.other_machine()
        _ = session_counts_cache().write_text(json.dumps({"machine": peer, "at": now.isoformat(), "accounts": {
            "claude4": {"units": {"busy": 9, "paused": 0, "exited": 1, "idle": 20, "total": 30}}}}))
        rows, problems = machine_role_counts((row,), now)
        self.assertEqual(problems, ())
        self.assertEqual(rows[0].roles, ("units",))
        self.assertEqual(rows[0].selected_roles, ())
        self.assertIn("units 30 (busy 9, idle 20, paused 0, exited 1)", accounts.render_status_table(
            accounts.StatusReading(rows, (), (), timezone.utc, now)))

    def test_stored_status_starts_a_background_counts_fetch_without_waiting_for_ssh(self) -> None:
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        paths = set(self.home.rglob("*"))
        with (patch.object(accounts, "_start_session_counts_fetch", return_value=True) as fetch,
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("status waited on ssh"))):
            code, _text, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        fetch.assert_called_once_with()
        self.assertEqual(set(self.home.rglob("*")), paths)
        self.assertEqual({path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}, before)

    def test_machine_counts_publish_and_merge_on_either_machine_without_waiting_for_ssh(self) -> None:
        from scripts.message import sessions
        local = accounts.RoleSessionCounts(1, 2, 0, 1)
        row = replace(SharedStatusDocumentTests().row(), label="claude1", tool="claude",
            folder=str(self.home / ".claude1"), roles=("units",), role_counts=(local,))
        now = datetime.now(timezone.utc)
        count = accounts.RoleSessionCountsDocument(busy=3, paused=0, exited=2, idle=1, total=6)
        for here, peer in (("natedev", "mac"), ("mac", "natedev")):
            with (self.subTest(here=here), patch.object(remote, "local_machine", return_value=here),
                  patch.object(remote, "other_machine", return_value=peer),
                  patch.object(sessions, "running_sessions", return_value=[]),
                  patch.object(role_map, "ssh_role_map", side_effect=AssertionError("status waited on ssh"))):
                published = session_counts_document({(self.home / ".claude1", "units"):
                    {"one": "busy", "two": "exited", "three": "idle", "four": "idle"}})
                published = decode_session_counts(json.dumps(published), here)
                self.assertEqual(published["accounts"]["claude1"]["units"]["total"], 4)
                atomic_file.write_text_atomic(session_counts_cache(), json.dumps({
                    "machine": peer, "at": now.isoformat(), "accounts": {"claude1": {"units": count}}}))
                rows, problems = machine_role_counts((row,), now)
                document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(
                    accounts.StatusReading(rows, problems, (), timezone.utc, now))))
                self.assertEqual(document["problems"], [])
                machines = document["accounts"][0]["role_counts_by_machine"]
                self.assertEqual(machines[peer]["units"], count)
                self.assertEqual(machines[here]["units"], {"busy": 1, "paused": 0, "exited": 1, "idle": 2, "total": 4})
                self.assertEqual(document["accounts"][0]["role_counts"], [{"busy": 4, "paused": 0, "exited": 3, "idle": 3, "total": 10}])
                self.assertEqual(rows[0].role_counts, (accounts.RoleSessionCounts(4, 3, 0, 3),))
                self.assertIn("units 10 (busy 4, idle 3, paused 0, exited 3)", accounts.render_status_table(
                    accounts.StatusReading(rows, (), (), timezone.utc, now)))

    def test_combined_role_counts_preserve_role_order_for_codex(self) -> None:
        now = datetime.now(timezone.utc)
        row = replace(SharedStatusDocumentTests().row(), label="codex1", tool="codex",
            roles=("workers", "units"), role_counts=(accounts.RoleSessionCounts(1, 2, 0, 0),))
        peer = remote.other_machine()
        _ = session_counts_cache().write_text(json.dumps({"machine": peer, "at": now.isoformat(), "accounts": {
            "codex1": {"units": {"busy": 2, "paused": 0, "exited": 0, "idle": 1, "total": 3},
                       "workers": {"busy": 3, "paused": 0, "exited": 0, "idle": 4, "total": 7}}}}))
        rows, problems = machine_role_counts((row,), now)
        reading = accounts.StatusReading(rows, problems, (), timezone.utc, now)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        self.assertEqual(document["accounts"][0]["roles"], ["workers", "units"])
        self.assertEqual(document["accounts"][0]["role_counts"], [
            {"busy": 4, "paused": 0, "exited": 0, "idle": 6, "total": 10},
            {"busy": 2, "paused": 0, "exited": 0, "idle": 1, "total": 3}])
        table = accounts.render_status_table(reading)
        self.assertIn("workers 10 (busy 4, idle 6, paused 0, exited 0)", table)
        self.assertIn("units 3 (busy 2, idle 1, paused 0, exited 0)", table)
        self.assertNotIn("shell", table)

    def test_unreported_roles_have_zero_counts_and_spare_has_no_count(self) -> None:
        now = datetime.now(timezone.utc)
        peer = remote.other_machine()
        _ = session_counts_cache().write_text(json.dumps({"machine": peer, "at": now.isoformat(), "accounts": {}}))
        for tool in ("claude", "codex"):
            for role in ("units", "spare"):
                with self.subTest(tool=tool, role=role):
                    row = replace(SharedStatusDocumentTests().row(), tool=tool, roles=(role,), role_counts=())
                    rows, problems = machine_role_counts((row,), now)
                    reading = accounts.StatusReading(rows, problems, (), timezone.utc, now)
                    document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
                    expected = None if role == "spare" else {
                        "busy": 0, "paused": 0, "exited": 0, "idle": 0, "total": 0}
                    self.assertEqual(document["accounts"][0]["role_counts"], [expected])
                    text = "spare" if role == "spare" else (
                        "units 0 (busy 0, idle 0, paused 0, exited 0)" if tool == "claude" else "units 0 (busy 0, idle 0, paused 0, exited 0)")
                    self.assertIn(text, accounts.render_status_table(reading))

    def test_session_counts_use_aliases_when_hostnames_differ(self) -> None:
        now = datetime.now(timezone.utc)
        row = replace(SharedStatusDocumentTests().row(), label="claude1", tool="claude",
            roles=("units",), role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),))
        for hostname, here, peer in (("MacBook-Pro", "mac", "natedev"),
                                     ("development-box", "natedev", "mac")):
            with (self.subTest(hostname=hostname),
                  patch.object(remote, "local_machine", return_value=hostname),
                  patch.object(remote, "other_machine", return_value=peer)):
                published = session_counts_document({(self.home / ".claude1", "units"): {"unit": "busy"}})
                self.assertEqual(published["machine"], here)
                self.assertEqual(decode_session_counts(json.dumps(published), here), published)
                atomic_file.write_text_atomic(session_counts_cache(), json.dumps({
                    "machine": peer, "at": now.isoformat(), "accounts": {}}))
                rows, problems = machine_role_counts((row,), now)
                self.assertEqual(problems, ())
                self.assertEqual(set(rows[0].role_counts_by_machine), {here, peer})
            with (patch.object(remote, "other_machine", return_value=here),
                  patch.object(role_map, "ssh_role_map", return_value=json.dumps(published))):
                self.assertEqual(fetch_session_counts(), 0)
                self.assertEqual(decode_session_counts(session_counts_cache().read_text(), here), published)

    def test_published_worker_counts_resolve_the_legacy_codex_home(self) -> None:
        self.write_note("codex1", "fixture-worker")
        self.codex_login("fixture-worker")
        self.codex_login("fixture-worker", self.home / ".codex1")
        published = session_counts_document({
            (self.home / ".codex", "workers"): {"legacy-worker": "busy"},
            (self.home / ".codex1", "workers"): {"numbered-worker": "idle"},
        })
        self.assertEqual(published["accounts"], {
            "codex1": {"workers": {"busy": 1, "paused": 0, "exited": 0, "idle": 1, "total": 2}}})
        self.assertEqual(decode_session_counts(json.dumps(published), published["machine"]), published)

    def test_published_worker_counts_resolve_accounts_in_numbered_codex_homes(self) -> None:
        self.write_note("codex1", "fixture-first")
        self.write_note("codex2", "fixture-second")
        self.codex_login("fixture-second", self.home / ".codex1")
        self.codex_login("fixture-first", self.home / ".codex2")
        published = session_counts_document({
            (self.home / ".codex1", "workers"): {"first-worker": "busy"},
            (self.home / ".codex2", "workers"): {"second-worker": "idle"},
        })
        self.assertEqual(published["accounts"], {
            "codex1": {"workers": {"busy": 0, "paused": 0, "exited": 0, "idle": 1, "total": 1}},
            "codex2": {"workers": {"busy": 1, "paused": 0, "exited": 0, "idle": 0, "total": 1}},
        })
        self.assertEqual(decode_session_counts(json.dumps(published), published["machine"]), published)

    def test_open_worker_counts_use_the_recorded_home_with_shared_sessions(self) -> None:
        from scripts.herdr import worker_panes
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.production.staff_test_support import unit_membership

        numbered = self.home / ".codex1"
        shared = self.home / ".codex" / "sessions"
        shared.mkdir(parents=True)
        numbered.mkdir()
        (numbered / "sessions").symlink_to(shared, target_is_directory=True)
        owner = unit_membership("show", "unit")
        owner = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), self.root), expected=owner.worktree)
        row = fixture_worker("shared-worker", owner.id, backend=staff.CodexBackend(numbered))
        _ = staff.set_home(row.id, staff.SessionHome(numbered))
        world = PaneWorld()
        world.add(owner, "w1:p0")
        world.add(row, "w1:p99", family="codex")
        read_sessions = cast(Callable[..., accounts.LocalRoleSessions], getattr(accounts, "_read_local_role_sessions"))
        with (patch.dict(os.environ, {"CODEX_HOME": str(shared.parent)}),
              patch.object(worker_panes, "_client", return_value=world),
              patch.object(herdr, "native_process", side_effect=world.native_process),
              patch.object(herdr, "_startup_processes", side_effect=world.descendants)):
            observed = read_sessions(accounts.GroupAccounts())
        self.assertEqual(observed.counts[(numbered, "workers")], {row.current_session: "idle"})

    def test_peer_counts_age_failure_and_recovery_use_only_the_local_counts(self) -> None:
        row = SharedStatusDocumentTests().row()
        now = datetime.now(timezone.utc)
        peer = remote.other_machine()
        local = "mac" if peer == "natedev" else "natedev"
        for seconds, available in ((600, True), (601, False), (-1, True)):
            with self.subTest(seconds=seconds):
                atomic_file.write_text_atomic(session_counts_cache(), json.dumps({
                    "machine": peer, "at": (now - timedelta(seconds=seconds)).isoformat(), "accounts": {}}))
                rows, problems = machine_role_counts((row,), now)
                self.assertEqual(peer in rows[0].role_counts_by_machine, available)
                self.assertEqual(problems, () if available else
                    (f"{peer} unavailable: counts are {local} only",))
        for content in ('{"unavailable": true}', '{}', 'broken'):
            _ = session_counts_cache().write_text(content)
            rows, problems = machine_role_counts((row,), now)
            self.assertNotIn(peer, rows[0].role_counts_by_machine)
            self.assertEqual(len(problems), 1)
        reply = json.dumps({"machine": peer, "at": now.isoformat(), "accounts": {}})
        with patch.object(role_map, "ssh_role_map", return_value=reply) as fetch:
            self.assertEqual(fetch_session_counts(), 0)
        fetch.assert_called_once_with(("session-counts-read",), machine=peer)
        self.assertEqual(machine_role_counts((row,), now)[1], ())
        for failure in (OSError("offline"), subprocess.TimeoutExpired("fixture", 20), ValueError("bad reply")):
            with self.subTest(failure=type(failure).__name__), patch.object(role_map, "ssh_role_map", side_effect=failure):
                self.assertEqual(fetch_session_counts(), 1)
            self.assertEqual(len(machine_role_counts((row,), now)[1]), 1)

    def test_count_fetch_detaches_once_and_transfers_its_lock(self) -> None:
        held: list[int] = []
        def detached(arguments: list[str], **options: object) -> MagicMock:
            descriptor = int(arguments[-1])
            held.append(os.dup(descriptor))
            self.assertEqual(arguments[2:4], ["session-counts-fetch", "--counts-lock"])
            self.assertEqual(options, {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "start_new_session": True, "pass_fds": (descriptor,)})
            return MagicMock()
        with patch.object(subprocess, "Popen", side_effect=detached) as launch:
            try:
                self.assertTrue(start_session_counts_fetch())
                self.assertTrue(start_session_counts_fetch())
                launch.assert_called_once()
            finally:
                for descriptor in held:
                    os.close(descriptor)
        with patch.object(subprocess, "Popen", side_effect=OSError("cannot detach")):
            self.assertFalse(start_session_counts_fetch())

    def test_counts_read_uses_only_local_registries_and_shared_transport_selects_the_peer(self) -> None:
        from scripts.message import sessions
        peer = remote.other_machine()
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("role fetch")),
              patch.object(accounts, "account_statuses", side_effect=AssertionError("account read")),
              patch.object(subprocess, "run", side_effect=AssertionError("network or process probe"))):
            result, text, error = self.run_cli("session-counts-read")
        self.assertEqual((result, error), (0, ""))
        local = "mac" if peer == "natedev" else "natedev"
        self.assertEqual(decode_session_counts(text, local)["accounts"], {})
        self.assertEqual({path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}, before)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=text, stderr="")) as fetch:
            self.assertEqual(role_map.ssh_role_map(("session-counts-read",), machine=peer), text)
        fetch.assert_called_once_with(("ssh", "-o", "BatchMode=yes", peer,
            "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py session-counts-read"),
            capture_output=True, text=True, timeout=20)

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

    def write_note(self, label: str, login: str, *, use_credits: str = "") -> None:
        path = Path(os.environ["AGENT_NOTES_DIR"]) / note_name(label)
        credits = f"use_credits: {use_credits}\n" if use_credits else ""
        _ = path.write_text(f"---\nlogin: {login}\n{credits}---\n", encoding="utf-8")

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
                    self.assertEqual(row[2], "" if missing else f"{group} ✓ 0 (busy 0, idle 0, paused 0, exited 0)")
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
        create_claude_account(label, login)
        if default:
            _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
        if note_label:
            self.write_note(note_label, login)

    def seed_room(self, login: str, days: float = 2) -> None:
        stored_room(login, 80, datetime.now(timezone.utc) + timedelta(days=days))

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

        arguments = ["--resume", "conversation id", "--", "quoted argument"]
        for tty, probe in ((False, False), (True, False), (False, True)):
            with (self.subTest(tty=tty, probe=probe), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "inherited"),
                      "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / "inherited-memory"),
                      "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token", "KEEP": "yes",
                      "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                      "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")}), patch.object(accounts, "prepared_account") as prepare, patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")), patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected subprocess")), patch.object(sys.stdin, "isatty", return_value=tty), patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_cli("exec", *(["--probe"] if probe else []),
                                              "claude", *arguments), (0, "", ""))
                prepare.assert_not_called()
                launch.assert_called_once()
                program, argv, environment = cast(tuple[str, list[str], dict[str, str]], launch.call_args.args)
                claude = str(self.home / ".local/bin/claude")
                if tty:
                    self.assertEqual(program, str(accounts.SCRIPTS / "lib/py"))
                    self.assertEqual(argv, [str(accounts.SCRIPTS / "lib/py"), str(accounts.SCRIPTS / "lib/relaunch.py"),
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
        cases = ((False, ["claude2"]), (True, ["--probe", "claude2"]), (True, ["--probe", "claude"]))
        for probe, account_options in cases:
            with self.subTest(account_options=account_options), patch.dict(os.environ):
                _ = os.environ.pop("CLAUDE_PROBE_SESSION", None)
                os.environ.update(SHOWRUNNER_UNIT="show/demo-unit", SHOWRUNNER_UNIT_ID="id-demo")
                inherited = {"SHOWRUNNER_PRIVATE_TEST": "private", "CLAUDE_CODE_SESSION_ID": "old-claude",
                             "CODEX_THREAD_ID": "old-codex"}
                os.environ.update(inherited)
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
                    for name in inherited:
                        self.assertNotIn(name, environment)
                else:
                    self.assertNotIn("CLAUDE_PROBE_SESSION", environment)
                    self.assertFalse(any(name.startswith("SHOWRUNNER_") for name in environment))
                    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                        self.assertEqual(environment[name], inherited[name])

    def test_exec_probe_preserves_claude_options_that_share_accounts_option_names(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        registry = write_registry(self.main_dir / "config/models.conf")
        arguments = ["--model", "haiku", "--help", "--detail"]
        with (patch.dict(os.environ, {"PATH": str(self.home / ".local/bin") + os.pathsep + MODELS_RESOLVER_PATH,
                                     "MODELS_CONFIG_FILE": str(registry),
                                     "MODELS_CONFIG_PUBLISH": "0"}),
              patch.object(os, "execvpe") as launch):
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
            with worker_names.roles_lock():
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

    def test_move_reports_an_unreadable_login_and_continues(self) -> None:
        self.login_directory("claude3", "unit@example.invalid")
        directory = accounts.account_dir("claude3")
        with (patch.object(accounts, "account_identity", return_value=agent_accounts.UnreadableIdentity(directory, "fixture read failed")),
              redirect_stderr(io.StringIO()) as errors):
            result = accounts.check_move_target("claude3", datetime.now(timezone.utc))
        self.assertIsInstance(result, accounts.MoveTargetReady)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("anomaly:", errors.getvalue())
        self.assertIn("fixture read failed", errors.getvalue())

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

    def test_prepared_account_logged_out_named_login_is_allowed(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        result = accounts.prepared_account("claude3", resume=False)
        self.assertIsInstance(result, accounts.PreparedAccount)
        assert isinstance(result, accounts.PreparedAccount)
        self.assertEqual(result.variables, named_environment(accounts.account_dir("claude3")))
        self.assertEqual(result.remove, frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))

    def test_group_launch_prepares_configured_account_for_new_work_and_resumes(self) -> None:
        """The account resolver reports bad input without changing the group."""
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "expected@example.invalid")
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude3", "showrunners": "claude3"}))
        before = path.read_bytes()
        for role in ("unit", "showrunner"):
            for resume in (False, True):
                with self.subTest(role=role, resume=resume), patch.object(accounts, "prepared_account",
                        return_value=accounts.AccountRefused("unknown launch account")) as prepare:
                    with self.assertRaises(accounts.AccountsError) as refused:
                        _ = accounts.account_for_group_launch(role, resume=resume)
                    self.assertEqual(str(refused.exception), "unknown launch account")
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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_exec_keeps_the_selected_home_and_status_reports_its_identity(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "expected@example.invalid")
        self.write_identity("claude3", "other@example.invalid")
        line = ("accounts: ~/.claude3 holds a different login from claude3; run "
            + f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(accounts.account_dir('claude3')))} claude, then /login as claude3")
        self.assertIsInstance(accounts.account_directory_readiness("claude3"), accounts.AccountDirectoryWrongLogin)
        self.assertEqual(accounts.check("claude3"), [line])
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.run_cli("exec", "claude3"), (0, "", ""))
        launch.assert_called_once()
        result = self.run_cli("status")
        self.assertRegex(result[1], r"\| ~/\.claude3 +\| claude3 +\|")
        self.assertIn("claude3: wrong sign-in; " + line.removeprefix("accounts: "), result[1].splitlines())
        self.assertNotIn("other@example.invalid", result[1])
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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_reports_unreadable_notes_without_erasing_other_account_identities(self) -> None:
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "unit@example.invalid")
        self.login_directory("claude4", "spare@example.invalid")
        self.select_named_unit("claude3")
        _ = (Path(os.environ["AGENT_NOTES_DIR"]) / "claude3.md").write_bytes(b"\xff")
        result = self.run_cli("status")
        self.assertEqual((result[0], result[2]), (0, ""))
        self.assertIn("repair the account note for claude3 with its login", result[1])
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
                with (scratch_account_home(self.home, environment),
                      patch.object(sys.stdin, "isatty", return_value=tty),
                      patch.object(os, "execvpe") as launch):
                    _ = accounts.exec_account("claude3", ["--resume", "conversation-id"])
                executable, argv, child_environment = cast(tuple[str, list[str], dict[str, str]], launch.call_args.args)
                if tty and not supervised:
                    self.assertEqual(executable, str(accounts.SCRIPTS / "lib/py"))
                    self.assertEqual(argv, [str(accounts.SCRIPTS / "lib/py"),
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

    def test_codex_update_uses_the_shared_install_on_both_machines_without_account_checks(self) -> None:
        program = "/fixture/codex"
        for label in ("workers", "codex1", "codex2"):
            for machine, peer in (("natedev", "mac"), ("mac", "natedev")):
                with (self.subTest(label=label, machine=machine),
                      patch.object(accounts, "launch_shell_environment", return_value={"CODEX_HOME": "/unused"}),
                      patch.object(accounts, "resolve_program", return_value=program),
                      patch.object(accounts, "role_account") as role,
                      patch.object(accounts, "check_codex_target") as target,
                      patch.object(remote, "local_machine", return_value=machine),
                      patch.object(remote, "other_machine", return_value=peer),
                      patch.object(os, "execvpe") as launch,
                      patch.object(subprocess, "run", side_effect=[
                          subprocess.CompletedProcess[str]([], 0),
                          subprocess.CompletedProcess[str]([], 0, "codex-cli 0.166.0\n", ""),
                          subprocess.CompletedProcess[str]([], 0, "codex-cli 0.166.0\n"),
                      ]) as run):
                    self.assertEqual(self.run_cli("exec", label, "update", "--yes"),
                        (0, f"{machine}: codex-cli 0.166.0\n{peer}: codex-cli 0.166.0\n", ""))
                    environment = {"CODEX_HOME": str(self.home / ".codex")}
                    self.assertEqual(run.call_args_list, [
                        call([program, "update", "--yes"], env=environment, check=False),
                        call([program, "--version"], env=environment, capture_output=True, text=True, timeout=15, check=False),
                        call(["ssh", "-o", "BatchMode=yes", peer,
                            'CODEX_HOME="$HOME/.codex" "$HOME/.local/bin/codex" update >&2 && "$HOME/.local/bin/codex" --version'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=600, check=False),
                    ])
                    role.assert_not_called()
                    target.assert_not_called()
                    launch.assert_not_called()

    def test_codex_update_reports_remote_failures_without_remote_output(self) -> None:
        failures = (
            (subprocess.CompletedProcess[str]([], 255, "hidden"), "unreachable; run codex update there"),
            (subprocess.TimeoutExpired("ssh", 600), "unreachable; run codex update there"),
            (OSError("hidden"), "unreachable; run codex update there"),
            (subprocess.CompletedProcess[str]([], 7, "hidden"), "update failed (exit 7)"),
            (subprocess.CompletedProcess[str]([], 0, ""), "version read failed"),
            (subprocess.CompletedProcess[str]([], 0, " \n"), "version read failed"),
        )
        for failure, detail in failures:
            with (self.subTest(detail=detail, failure=type(failure).__name__),
                  patch.object(accounts, "launch_shell_environment", return_value={}),
                  patch.object(accounts, "resolve_program", return_value="/fixture/codex"),
                  patch.object(remote, "local_machine", return_value="natedev"),
                  patch.object(remote, "other_machine", return_value="mac"),
                  patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess[str]([], 0),
                      subprocess.CompletedProcess[str]([], 0, "codex-cli 0.166.0\n", ""), failure])):
                self.assertEqual(self.run_cli("exec", "workers", "update"),
                    (1, f"natedev: codex-cli 0.166.0\nmac: {detail}\n", ""))

    def test_codex_update_still_updates_the_peer_after_a_local_failure(self) -> None:
        for failure, detail in ((subprocess.CompletedProcess[str]([], 9), "update failed (exit 9)"),
                                (OSError("hidden"), "codex could not start")):
            with (self.subTest(detail=detail),
                  patch.object(accounts, "launch_shell_environment", return_value={}),
                  patch.object(accounts, "resolve_program", return_value="/fixture/codex"),
                  patch.object(remote, "local_machine", return_value="natedev"),
                  patch.object(remote, "other_machine", return_value="mac"),
                  patch.object(subprocess, "run", side_effect=[failure,
                      subprocess.CompletedProcess[str]([], 0, "codex-cli 0.166.0\n")]) as run):
                self.assertEqual(self.run_cli("exec", "codex1", "update"),
                    (1, f"natedev: {detail}\nmac: codex-cli 0.166.0\n", ""))
                self.assertEqual(run.call_count, 2)

    def test_codex_update_requires_a_successful_local_version_read(self) -> None:
        failures = (
            (subprocess.CompletedProcess[str]([], 4, "hidden", "hidden"), "version read failed (exit 4)"),
            (subprocess.CompletedProcess[str]([], 0, "", "hidden"), "version read failed (exit 0)"),
            (subprocess.TimeoutExpired("codex", 15), "version read timed out"),
        )
        for failure, detail in failures:
            with (self.subTest(detail=detail),
                  patch.object(accounts, "launch_shell_environment", return_value={}),
                  patch.object(accounts, "resolve_program", return_value="/fixture/codex"),
                  patch.object(remote, "local_machine", return_value="natedev"),
                  patch.object(remote, "other_machine", return_value="mac"),
                  patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess[str]([], 0), failure,
                      subprocess.CompletedProcess[str]([], 0, "codex-cli 0.166.0\n")])):
                self.assertEqual(self.run_cli("exec", "codex2", "update"),
                    (1, f"natedev: {detail}\nmac: codex-cli 0.166.0\n", ""))

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
        self.assertIn("codex99 has no account note", error)
        launch.assert_not_called()

    def test_exec_unknown_codex_account_beside_a_noted_home_names_only_the_missing_home(self) -> None:
        _ = self.workers_fixture()
        with patch.object(os, "execvpe") as launch:
            code, output, error = self.run_cli("exec", "codex99")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("codex99 has no account note", error)
        self.assertNotIn("@", error)
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
        self.assertIn("holds a different login from claude3", problems[2])
        self.assertNotIn("other@example.invalid", problems[2])

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

    def test_single_session_move_reads_the_inventory_listing_once(self) -> None:
        from scripts.agents.inventory import (
            AttributedSessionConfiguration, ListedSession, ShowrunnerRegistration, ShowrunnerRegistrations,
        )

        inventory = import_module("scripts.agents.inventory")
        owner = ListedSession("move-id", 998071, "fixture", "fixture session", "top-level",
                              self.home / ".claude1", "", "", "")
        target = accounts.MoveTargetReady(accounts.NamedAccount("claude2", self.home / ".claude2"))
        outcome = stop_results.ClaudeUp(owner.session_id, "w1:p1", "claude2")
        for live in ([owner], []):
            with (self.subTest(live=bool(live)),
                  patch.object(inventory, "listing", return_value=live) as listing,
                  patch.object(accounts, "check_move_target", return_value=target),
                  patch.object(stop_resume, "move_sessions", return_value=[outcome]) as moved):
                result = accounts.move(owner.session_id, "claude2", why="fixture move")
            self.assertIsInstance(result, accounts.MoveStarted)
            listing.assert_called_once_with()
            moved.assert_called_once_with([owner.session_id], "claude2", why="fixture move", cause="by hand",
                                          source_accounts={owner.session_id: "claude1"} if live else {})

        self.login_directory("claude1", "move@example.invalid")
        stored_room("move@example.invalid", 0, datetime.now(timezone.utc) + timedelta(days=1))
        for dry_run in (False, True):
            with (self.subTest(dry_run=dry_run),
                  patch.object(inventory, "listing", return_value=[owner]) as listing,
                  patch.object(accounts, "check_move_target", wraps=accounts.check_move_target) as checked,
                  patch.object(stop_resume, "move_sessions") as moved):
                result = self.run_cli("move", owner.name, "claude1", *(["--dry-run"] if dry_run else []))
                self.assertEqual(result, (0, "fixture session: already on claude1\n", ""))
                listing.assert_called_once_with()
                checked.assert_not_called()
                moved.assert_not_called()

        runner = fixture_member("showrunner", name="fixture-production",
                                doc=staff.LiveDoc("natedev", self.root / "production.md"))
        unit = fixture_member("unit", name="fixture-unit", parent=runner.id)
        registrations = ShowrunnerRegistrations((ShowrunnerRegistration(
            str(runner.current_session), runner.name, str(self.root / "production.md")),))
        producer = fixture_member("producer", name="fixture-producer")
        records = [sessions.SessionRecord(pid=998071 + index, sessionId=str(row.current_session),
            name=runner.name, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], procStart="fixture")
            for index, row in enumerate((runner, unit, producer))]
        attributed = AttributedSessionConfiguration(
            account.Account("claude", "move@example.invalid", "claude1"), self.home / ".claude1")
        with (patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "private-sessions"),
                                           "HERDR_PROC": str(self.root)}),
              patch.object(sessions, "live_sessions", return_value=records),
              patch.object(inventory, "showrunner_registrations", return_value=registrations) as timers,
              patch("scripts.agents.inventory._record_matches_process", return_value=True),
              patch("scripts.agents.inventory._configuration_for_session", return_value=attributed)):
            for registered in (registrations, ShowrunnerRegistrations(())):
                timers.return_value = registered
                for member, reference in ((unit, unit.name), (unit, str(unit.current_session)),
                                          (runner, runner.name), (producer, producer.name)):
                    for dry_run in (False, True):
                        with (self.subTest(registered=bool(registered.entries), reference=reference, dry_run=dry_run),
                              patch.object(accounts, "check_move_target", wraps=accounts.check_move_target) as checked,
                              patch.object(accounts, "move_sessions", return_value=()) as moved):
                            result = self.run_cli("move", reference, "claude1", *(["--dry-run"] if dry_run else []))
                            self.assertEqual(result, (0, f"{member.name}: already on claude1\n", ""))
                            checked.assert_not_called()
                            moved.assert_not_called()

    def test_showrunner_group_preview_names_unregistered_timers_without_configuring_or_requesting(self) -> None:
        from scripts.agents.inventory import ShowrunnerRegistration, ShowrunnerRegistrations

        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "other@example.invalid")
        inventory = import_module("scripts.agents.inventory")
        stale = showrunners.Showrunner(session="", socket="", name="missing", zone="America/New_York",
                                       doc="/w/missing-production.md")
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "showrunner_registrations", return_value=ShowrunnerRegistrations((
                  ShowrunnerRegistration("missing", stale["name"], stale["doc"]),))),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude3", "--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("showrunners: would select claude3", output)
        self.assertIn("showrunners: no live session moves", output)
        self.assertIn("missing: no session runs under the id", output)
        self.assertIn("in any Claude session on", output)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", output)
        self.assertIn("--production missing", output)
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())
        moved.assert_not_called()

    def test_showrunner_group_move_names_every_unregistered_timer_even_without_live_members(self) -> None:
        from scripts.agents.inventory import ShowrunnerRegistration, ShowrunnerRegistrations

        self.login_directory("claude2", "main@example.invalid", default=True)
        self.login_directory("claude3", "other@example.invalid")
        inventory = import_module("scripts.agents.inventory")
        stale = [showrunners.Showrunner(session="", socket="", name=name, zone="America/New_York",
                                       doc=f"/w/doc-{name}-production.md") for name in ("first", "second")]
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "showrunner_registrations", return_value=ShowrunnerRegistrations(tuple(
                  ShowrunnerRegistration(runner["name"], runner["name"], runner["doc"]) for runner in stale))),
              patch.object(accounts, "move_sessions") as moved):
            code, output, error = self.run_cli("move", "showrunners", "claude3")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        self.assertIn("showrunners: no live session moves", output)
        for runner in stale:
            line = next(line for line in output.splitlines() if line.startswith(runner["name"] + ":"))
            self.assertIn("no session runs under the id", line)
            self.assertIn("in any Claude session on", line)
            self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", line)
            self.assertIn(f"--production {runner['name']}", line)
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

    def write_trust_fixture(self) -> tuple[Path, ...]:
        write_account_settings(self.trust_config, folders=("~/worktrees", "~/other trees"))
        paths = [self.home / ".claude.json"]
        for number in (1, 2):
            home = self.home / f".claude{number}"
            home.mkdir()
            paths.append(home / ".claude.json")
        for path in paths:
            _ = path.write_text(json.dumps({"keep": [1, 2], "projects": {
                str(self.home / "worktrees"): {"hasTrustDialogAccepted": False, "keep": "here"},
                "/elsewhere": {"hasTrustDialogAccepted": False, "other": [1]},
            }}))
            path.chmod(0o640)
        return tuple(paths)

    def trust_home_snapshot(self) -> dict[Path, tuple[int, int, int, int, bytes]]:
        """Notice content, metadata, directory, link and temporary-file changes."""
        result: dict[Path, tuple[int, int, int, int, bytes]] = {}
        for path in (self.home, *self.home.rglob("*")):
            metadata = path.lstat()
            content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes() if path.is_file() else b""
            result[path] = (metadata.st_mode, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns, content)
        return result

    def assert_trust_locks_free(self, paths: tuple[Path, ...]) -> None:
        for path in paths:
            lock_path = path.with_name(path.name + ".lock")
            if lock_path.exists():
                with lock_path.open("a") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def start_trust_writer(self, path: Path) -> subprocess.Popen[str]:
        """An independent lock-respecting save, with only scratch config paths."""
        script = '''
import fcntl, json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
with path.with_name(path.name + ".lock").open("a") as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("blocked", flush=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    print("acquired", flush=True)
    sys.stdin.readline()
    current = json.loads(path.read_text())
    current["other writer"] = "retained"
    temporary = path.with_name(path.name + ".writer")
    temporary.write_text(json.dumps(current))
    temporary.chmod(path.stat().st_mode)
    os.replace(temporary, path)
'''
        process = subprocess.Popen([sys.executable, "-c", script, str(path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)

        def close_writer() -> None:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=5)

        self.addCleanup(close_writer)
        return process

    def test_trust_waiting_writer_saves_after_publication_and_preserves_both_changes(self) -> None:
        paths = self.write_trust_fixture()
        publish = os.replace
        writers: list[subprocess.Popen[str]] = []

        def publish_with_contender(temporary: Path, path: Path) -> None:
            if path == paths[0]:
                writer = self.start_trust_writer(path)
                writers.append(writer)
                assert writer.stdout is not None and writer.stdin is not None
                # This hook is after trust's compare and before its replace.
                self.assertEqual(writer.stdout.readline(), "blocked\n")
                _ = writer.stdin.write("save\n")
                writer.stdin.flush()
                self.assertIsNone(writer.poll())
            publish(temporary, path)

        with patch.object(os, "replace", side_effect=publish_with_contender):
            code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        writer = writers[0]
        stdout, stderr = writer.communicate(timeout=5)
        self.assertEqual((writer.returncode, stdout, stderr), (0, "acquired\n", ""))
        final = accounts.read_json_object(paths[0])
        self.assertEqual(final["other writer"], "retained")
        self.assertEqual(final["projects"], accounts.read_json_object(paths[1])["projects"])

    def test_trust_waits_for_held_lock_then_reads_the_holders_change(self) -> None:
        paths = self.write_trust_fixture()
        writer = self.start_trust_writer(paths[0])
        assert writer.stdout is not None and writer.stdin is not None
        self.assertEqual(writer.stdout.readline(), "acquired\n")
        flock = fcntl.flock
        lock_path = paths[0].with_name(paths[0].name + ".lock")
        waits: list[int] = []

        def release_holder(descriptor: int, operation: int) -> None:
            if os.fstat(descriptor).st_ino == lock_path.stat().st_ino:
                self.assertEqual(operation, fcntl.LOCK_EX)
                waits.append(descriptor)
                assert writer.stdin is not None
                _ = writer.stdin.write("save\n")
                writer.stdin.flush()
            flock(descriptor, operation)

        with patch.object(fcntl, "flock", side_effect=release_holder):
            code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assertEqual(len(waits), 1)
        stdout, stderr = writer.communicate(timeout=5)
        self.assertEqual((writer.returncode, stdout, stderr), (0, "", ""))
        final = accounts.read_json_object(paths[0])
        self.assertEqual(final["other writer"], "retained")
        self.assertEqual(final["projects"], accounts.read_json_object(paths[1])["projects"])

    def test_trust_acquires_the_kernel_lock_after_the_holder_dies(self) -> None:
        paths = self.write_trust_fixture()
        writer = self.start_trust_writer(paths[0])
        assert writer.stdout is not None
        self.assertEqual(writer.stdout.readline(), "acquired\n")
        writer.kill()
        _ = writer.communicate(timeout=5)
        code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assert_trust_locks_free(paths)
        self.assertEqual(accounts.read_json_object(paths[0])["projects"],
                         accounts.read_json_object(paths[1])["projects"])








    def test_trust_and_seed_read_one_validated_configuration_before_home_locks(self) -> None:
        self.write_main_config()
        read = Path.read_text
        flock = fcntl.flock
        for arguments in (("trust",), ("setup", "claude2")):
            with self.subTest(arguments=arguments):
                write_account_settings(self.trust_config, folders=("~/worktrees",))
                reads: list[Path] = []

                def read_settings(
                    path: Path, encoding: str | None = None, errors: str | None = None,
                    newline: str | None = None,
                ) -> str:
                    if path == self.trust_config:
                        reads.append(path)
                    return read(path, encoding=encoding, errors=errors, newline=newline)

                def change_settings_after_validation(descriptor: int, operation: int) -> None:
                    self.assertEqual(reads, [self.trust_config])
                    _ = self.trust_config.write_text("[trust]\nfolders = []\n")
                    flock(descriptor, operation)

                with (patch.object(Path, "read_text", new=read_settings),
                      patch.object(fcntl, "flock", side_effect=change_settings_after_validation)):
                    code, _output, error = self.run_cli(*arguments)
                self.assertEqual(code, 0, error)
                self.assertEqual(reads, [self.trust_config])
                seeded = self.home / ".claude2/.claude.json" if arguments[0] == "setup" else self.home / ".claude.json"
                projects = cast(dict[str, object], accounts.read_json_object(seeded)["projects"])
                self.assertEqual(projects[str(self.home / "worktrees")], {"hasTrustDialogAccepted": True})


    def test_trust_holds_one_publication_lock_through_preparation_and_sync(self) -> None:
        paths = self.write_trust_fixture()
        prepare = atomic_file.prepared_replacement
        lock_config = file_lock.claude_config_lock
        sync = os.fsync
        locks: list[Path] = []
        preparations: list[Path] = []
        held: set[Path] = set()

        def assert_lock_held(path: Path) -> None:
            self.assertIn(path, held)
            with path.with_name(path.name + ".lock").open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        @contextmanager
        def prepare_under_lock(path: Path, content: str | bytes, *, mode: int) -> Generator[Path]:
            assert_lock_held(path)
            preparations.append(path)
            with prepare(path, content, mode=mode) as temporary:
                assert_lock_held(path)
                self.assertEqual(temporary.stat().st_mode & 0o777, 0o640)
                yield temporary
                assert_lock_held(path)
            assert_lock_held(path)

        @contextmanager
        def count_lock(path: Path) -> Generator[None]:
            with lock_config(path):
                locks.append(path)
                held.add(path)
                try:
                    yield
                finally:
                    held.remove(path)

        def sync_under_lock(descriptor: int) -> None:
            self.assertEqual(len(held), 1)
            assert_lock_held(next(iter(held)))
            sync(descriptor)

        with (patch.object(atomic_file, "prepared_replacement", side_effect=prepare_under_lock),
              patch.object(file_lock, "claude_config_lock", side_effect=count_lock),
              patch.object(os, "fsync", side_effect=sync_under_lock)):
            code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assertEqual(locks, list(paths))
        self.assertEqual(preparations, list(paths))
        self.assert_trust_locks_free(paths)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o640 for path in paths))
        self.assertFalse(list(self.home.rglob("*.tmp")))

    def test_trust_rebuilds_when_permissions_tighten_during_preparation(self) -> None:
        paths = self.write_trust_fixture()
        paths[0].chmod(0o644)
        sync = os.fsync
        replace = os.replace
        prepared_modes: list[int] = []

        def tighten_during_sync(descriptor: int) -> None:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                prepared_modes.append(os.fstat(descriptor).st_mode & 0o777)
                if len(prepared_modes) == 1:
                    paths[0].chmod(0o600)
            sync(descriptor)

        def publish(temporary: Path, destination: Path) -> None:
            self.assertEqual(temporary.stat().st_mode & 0o777, destination.stat().st_mode & 0o777)
            replace(temporary, destination)

        with (patch.object(os, "fsync", side_effect=tighten_during_sync),
              patch.object(os, "replace", side_effect=publish)):
            code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assertEqual(prepared_modes, [0o644, 0o600, 0o640, 0o640])
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(accounts.read_json_object(paths[0])["projects"],
                         accounts.read_json_object(paths[1])["projects"])
        self.assertFalse(list(self.home.rglob("*.tmp")))

    def test_trust_changes_only_configured_project_bits_in_every_home_and_reruns_unchanged(self) -> None:
        paths = self.write_trust_fixture()
        originals = [accounts.read_json_object(path) for path in paths]
        code, output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assertEqual(output.splitlines(), ["~/.claude: trust updated", "claude1: trust updated", "claude2: trust updated"])
        self.assertEqual(error, "")
        for path, original in zip(paths, originals):
            with self.subTest(path=path.name, home=path.parent.name):
                expected_projects = cast(dict[str, object], original["projects"])
                expected_projects[str(self.home / "worktrees")] = {"hasTrustDialogAccepted": True, "keep": "here"}
                expected_projects[str(self.home / "other trees")] = {"hasTrustDialogAccepted": True}
                self.assertEqual(accounts.read_json_object(path), original)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.assertEqual(self.run_cli("trust")[:2], (0,
            "~/.claude: trust unchanged\nclaude1: trust unchanged\nclaude2: trust unchanged\n"))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_trust_retries_an_intervening_writer_before_publish_and_after_publish(self) -> None:
        paths = self.write_trust_fixture()
        apply = accounts.with_trusted_folders
        publish = os.replace
        before_publish = True
        after_publish = True

        def competing_change(source: dict[str, object], folders: tuple[str, ...]) -> dict[str, object]:
            nonlocal before_publish
            if before_publish:
                before_publish = False
                current = accounts.read_json_object(paths[0])
                current["concurrent before"] = "retained"
                _ = paths[0].write_text(json.dumps(current))
            return apply(source, folders)

        def competing_publish(temporary: Path, path: Path) -> None:
            nonlocal after_publish
            publish(temporary, path)
            if after_publish:
                after_publish = False
                # An unlocked writer uses its earlier snapshot after our replace.
                _ = path.write_text(json.dumps({"concurrent before": "retained", "concurrent after": True}))

        with (patch.object(accounts, "with_trusted_folders", side_effect=competing_change),
              patch.object(os, "replace", side_effect=competing_publish)):
            code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        final = accounts.read_json_object(paths[0])
        self.assertEqual(final["concurrent before"], "retained")
        self.assertIs(final["concurrent after"], True)
        self.assertEqual(final["projects"], {
            str(self.home / "worktrees"): {"hasTrustDialogAccepted": True},
            str(self.home / "other trees"): {"hasTrustDialogAccepted": True},
        })

    def test_trust_reports_missing_and_unreadable_files_and_continues_other_homes(self) -> None:
        paths = self.write_trust_fixture()
        paths[0].unlink()
        read = Path.read_bytes

        def unreadable(path: Path) -> bytes:
            if path == paths[1]:
                raise PermissionError("fixture unreadable")
            return read(path)

        with patch.object(Path, "read_bytes", new=unreadable):
            code, output, error = self.run_cli("trust")
        self.assertEqual((code, output), (1, "claude2: trust updated\n"))
        self.assertIn("~/.claude: trust failed:", error)
        self.assertIn("claude1: trust failed: fixture unreadable", error)
        self.assertFalse(paths[0].exists())
        self.assertEqual(accounts.read_json_object(paths[2])["projects"], {
            str(self.home / "worktrees"): {"hasTrustDialogAccepted": True, "keep": "here"},
            str(self.home / "other trees"): {"hasTrustDialogAccepted": True},
            "/elsewhere": {"hasTrustDialogAccepted": False, "other": [1]},
        })

    def test_without_project_entries_removes_exact_keys_and_preserves_other_settings(self) -> None:
        source: dict[str, object] = {"keep": [1, 2], "projects": {
            "/settings": {"hasTrustDialogAccepted": True, "lastCost": 1.5, "exampleFiles": ["file"]},
            "/empty": {"hasTrustDialogAccepted": False},
            "/without-trust": {"lastCost": 2.5},
            "/settings/child": {"hasTrustDialogAccepted": True},
            "/settings-neighbor": {"keep": "neighbor"},
            "/other": {"hasTrustDialogAccepted": True, "keep": "other"},
        }}
        before = json.dumps(source)
        result = accounts.without_project_entries(source, ("/settings", "/empty", "/without-trust", "/missing"))
        self.assertEqual(result, {"keep": [1, 2], "projects": {
            "/settings/child": {"hasTrustDialogAccepted": True},
            "/settings-neighbor": {"keep": "neighbor"},
            "/other": {"hasTrustDialogAccepted": True, "keep": "other"},
        }})
        self.assertEqual(json.dumps(source), before)
        self.assertIs(accounts.without_project_entries(result, ("/settings", "/empty")), result)
        self.assertIs(accounts.without_project_entries(source, ()), source)
        absent: dict[str, object] = {"keep": "no projects"}
        self.assertIs(accounts.without_project_entries(absent, ("/missing",)), absent)

    def test_folder_trust_reports_a_failed_home_and_continues_on_set_and_clear(self) -> None:
        paths = self.write_trust_fixture()
        folder = self.home / "clone"
        read = Path.read_bytes

        def unreadable(path: Path) -> bytes:
            if path == paths[1]:
                raise PermissionError("fixture unreadable")
            return read(path)

        for trusted in (True, False):
            with self.subTest(trusted=trusted):
                errors = io.StringIO()
                output = io.StringIO()
                with (patch.object(Path, "read_bytes", new=unreadable),
                      redirect_stderr(errors), redirect_stdout(output)):
                    accounts.set_claude_folder_trust(folder, trusted=trusted)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertTrue(errors.getvalue().startswith("accounts: anomaly:"))
                for path in (paths[0], paths[2]):
                    projects = cast(dict[str, object], accounts.read_json_object(path)["projects"])
                    self.assertEqual(str(folder.resolve()) in projects, trusted)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_folder_trust_skips_missing_files_without_output_or_file_creation(self) -> None:
        paths = self.write_trust_fixture()
        for path in paths:
            path.unlink()
        errors = io.StringIO()
        output = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(output):
            self.assertFalse(accounts.source_has_claude_trust(self.home / "source"))
            accounts.set_claude_folder_trust(self.home / "clone", trusted=True)
            accounts.set_claude_folder_trust(self.home / "clone", trusted=False)
        self.assertEqual((errors.getvalue(), output.getvalue()), ("", ""))
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(all(not path.with_name(path.name + ".lock").exists() for path in paths))

    def test_new_account_seed_uses_trusted_folders_from_configuration_at_use(self) -> None:
        write_account_settings(self.trust_config, folders=("~/configured root",))
        _ = (self.home / ".claude.json").write_text('{"hasCompletedOnboarding": true}')
        self.assertEqual(accounts.setup("claude2"), [])
        seeded = accounts.read_json_object(self.home / ".claude2/.claude.json")
        self.assertEqual(seeded, {"hasCompletedOnboarding": True, "projects": {
            str(self.home / "configured root"): {"hasTrustDialogAccepted": True},
        }})
        self.assertEqual((self.home / ".claude2/.claude.json").stat().st_mode & 0o777, 0o600)
        write_account_settings(self.trust_config, folders=("~/different root",))
        self.assertEqual(accounts.setup("claude3"), [])
        self.assertEqual(accounts.read_json_object(self.home / ".claude3/.claude.json")["projects"], {
            str(self.home / "different root"): {"hasTrustDialogAccepted": True},
        })

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
                    self.assertIsInstance(selected, accounts.NamedAccount)
                    self.assertTrue(folder.is_symlink())
                else:
                    self.assertIn("claude3", [named.label for named in accounts.checked_named_accounts()])
                if folder.is_symlink():
                    folder.unlink()
                folder.mkdir(exist_ok=True)
                _ = (folder / "collision").write_text("local")
                _ = (self.main_dir / "shell-snapshots/collision").write_text("shared")
                with redirect_stderr(io.StringIO()) as errors:
                    selected = accounts._move_target("claude3")  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(selected, accounts.NamedAccount)
                self.assertIn("anomaly:", errors.getvalue())
                self.assertIn("resolve clashing entries collision", errors.getvalue())
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
                with redirect_stderr(io.StringIO()) as errors:
                    if tool == "claude":
                        self.assertIsInstance(accounts.prepared_account("claude3", resume=True), accounts.PreparedAccount)
                    else:
                        self.assertEqual(accounts.check_codex_target("codex3", shared).home, destination)
                diagnostic = errors.getvalue()
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

    def test_launch_reports_a_file_conflict_and_keeps_both_files(self) -> None:
        for tool, name in (("claude", "settings.json"), ("codex", "config.toml")):
            with self.subTest(tool=tool):
                destination, shared = self.shared_repair_fixture(tool)
                _ = (shared / name).write_text("shared settings")
                _ = (destination / name).write_text("local settings")
                with redirect_stderr(io.StringIO()) as errors:
                    if tool == "claude":
                        self.assertIsInstance(accounts.prepared_account("claude3", resume=True), accounts.PreparedAccount)
                    else:
                        self.assertEqual(accounts.check_codex_target("codex3", shared).home, destination)
                reason = errors.getvalue()
                self.assertIn("anomaly:", reason)
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
            if name != ".claude.json":
                self.assertIn(f"accounts: {path} is a link; identity stays in each account dir\n", stderr)
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), str(target))

    def test_problem_helpers_return_empty_or_populated_lists(self) -> None:
        repair_link = cast(
            Callable[[Path, Path], list[str]],
            getattr(accounts, "_repair_link"),
        )
        seed_config = cast(
            Callable[[Path, accounts.TrustConfiguration], list[str]],
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
        self.assertEqual(seed_config(account_dir / ".claude.json", accounts.trust_configuration()), [])

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
        self.write_main_config()
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
        self.assertEqual(setup_result, 0)
        self.assertEqual(setup_stderr, "")
        self.assertEqual(check_result, 1)
        self.assertEqual(check_stdout, problem)
        login_read.assert_not_called()
        self.assertTrue((target / ".claude.json").is_file())
        self.assertTrue(account_dir.is_symlink())
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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_describes_accounts_without_creating_any(self) -> None:
        with patch.object(sys, "platform", "linux"):
            self.assert_status_describes_accounts_without_creating_any()

    def assert_status_describes_accounts_without_creating_any(self) -> None:
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
        self.assertNotIn("unit directors", stdout)
        self.assertNotIn("unclassified entries", stdout)
        self.assertNotIn("mcpServers differ", stdout)
        self.assertNotIn(" where ~/.claude does not: ", stdout)
        self.assertEqual(set(self.home.iterdir()), before)
        self.assertFalse(any(self.home.glob(".claude[0-9]*")))

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
        self.assertNotIn(" where ~/.claude does not: ", stdout)
        self.assertIn(call(account_dir), login_read.call_args_list)

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

        expected = (
            "claude2 turns brp off in 1 project where ~/.claude does not: "
            + "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp",
            "claude2 turns account-only on in 1 project where ~/.claude does not: "
            + "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 account-only",
        )
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
                    for line in expected:
                        self.assertIn(line, cast(list[str], document["problems"]))
                else:
                    for line in expected:
                        self.assertIn(line, stdout.getvalue().splitlines())

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
                fetch.assert_called_once_with(("role-map-read",))
                lines = (cast(list[str], json.loads(output)["problems"]) if flags else output.splitlines())
                problems = [line for line in lines if line.startswith("role map: using saved assignments;")]
                self.assertEqual(len(problems), 1)
                self.assertIn("2026-10-16", problems[0])
                self.assertIn(str(failure), problems[0])
                self.assertIn("restore access to natedev", problems[0])

    def test_stored_role_map_with_cached_assignments_lists_no_problem(self) -> None:
        cache = role_map.role_map_cache_file()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"at": "2026-10-16T15:24:00+00:00", "map": {
            "groups": {}, "workers": "codex1"}}))
        for flags in ((), ("--json",)):
            with (self.subTest(flags=flags), patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored status reached ssh")) as fetch):
                code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, error), (0, ""))
            lines = (cast(list[str], json.loads(output)["problems"]) if flags else output.splitlines())
            problems = [line for line in lines if line.startswith("role map: using saved assignments;")]
            self.assertEqual(problems, [])
            fetch.assert_not_called()

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
        def render(_reading: accounts.StatusReading, *, account_config: conf.ConfigRead) -> str:
            del account_config
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
        def render(reading: accounts.StatusReading, *, account_config: conf.ConfigRead) -> str:
            print("fixture renderer warning", file=sys.stderr)
            return original(reading, account_config=account_config)
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
                            if diagnostic == "stored map":
                                self.assertNotIn("role map: using saved assignments;", error)
                            else:
                                expected_map = ("role map: using saved assignments; map saved at 2026-10-16 08:24 PDT; "
                                    + "natedev fetch failed: fixture offline; restore access to natedev; run accounts.py status again")
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
                            else output.split("\n\n", 2)[2].strip().splitlines())
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
                    self.assertFalse(any(text in error for text in ("account note", "setup claude2")))
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

    def write_mcp_configuration(self, directory: Path, servers: tuple[str, ...],
                                projects: dict[str, list[str]]) -> Path:
        """Write the effective-server cases beside this test's scratch identity."""
        configuration = McpConfigurationFixture(
            oauthAccount=McpFixtureIdentity(emailAddress="fixture@notexample.com"),
            mcpServers={name: {} for name in servers},
            projects={path: McpProjectFixture(disabledMcpServers=disabled)
                      for path, disabled in projects.items()},
        )
        path = agent_accounts.claude_config_file(directory)
        _ = path.write_text(json.dumps(configuration), encoding="utf-8")
        return path

    def assert_mcp_readout(self, expected: list[str]) -> None:
        """Both readout forms preserve every independent server and direction."""
        for flags in ((), ("--json",)):
            with self.subTest(flags=flags):
                code, output, error = self.run_cli("status", "--stored", *flags)
                self.assertEqual((code, error), (0, ""))
                lines = (cast(accounts.StatusDocument, json.loads(output))["problems"]
                         if flags else output.splitlines())
                self.assertFalse(any("mcpServers differ" in line for line in lines))
                self.assertCountEqual([line for line in lines if line.startswith("claude2 turns ")], expected)

    def test_mcp_readout_counts_three_disabled_projects_and_lists_them_without_writing(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        projects = [str(self.root / name) for name in ("project one", "project-two", "project-three")]
        baseline = self.write_mcp_configuration(self.main_dir, ("brp",), {path: [] for path in projects})
        config = self.write_mcp_configuration(self.home / ".claude2", ("brp",),
                                              {path: ["brp"] for path in projects})
        before = (baseline.read_bytes(), config.read_bytes())
        command = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp"
        self.assert_mcp_readout([f"claude2 turns brp off in 3 projects where ~/.claude does not: {command}"])
        # The lister remains read-only; the printed repair is covered separately.
        code, output, error = self.run_cli("mcp-projects", "claude2", "brp", "off")
        self.assertEqual((code, error), (0, ""))
        self.assertCountEqual(output.splitlines(), projects)
        self.assertEqual((baseline.read_bytes(), config.read_bytes()), before)

    def test_mcp_printed_command_accepts_a_server_name_starting_with_a_dash(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "project with spaces")
        _ = self.write_mcp_configuration(self.main_dir, ("-brp",), {project: []})
        _ = self.write_mcp_configuration(self.home / ".claude2", ("-brp",), {project: ["-brp"]})
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        prefix = "claude2 turns -brp off in 1 project where ~/.claude does not: "
        lines = [line for line in output.splitlines() if line.startswith(prefix)]
        self.assertEqual(len(lines), 1)
        command = lines[0].removeprefix(prefix)
        self.assertEqual(self.run_cli(*shlex.split(command)[2:]), (0, "", ""))
        self.assertEqual(shlex.split(command)[2:], ["mcp-repair", "--", "claude2", "-brp"])
        self.assertEqual(self.run_cli("mcp-projects", "--", "claude2", "-brp", "off"), (0, "", ""))

    def test_matching_mcp_configuration_has_no_difference_line_or_project_output(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        projects = {str(self.root / "on"): [], str(self.root / "off"): ["brp"]}
        for directory in (self.main_dir, self.home / ".claude2"):
            _ = self.write_mcp_configuration(directory, ("brp",), projects)
        self.assert_mcp_readout([])
        for direction in ("off", "on"):
            self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", direction), (0, "", ""))

    def test_mcp_home_scope_ignores_the_numbered_folders_different_project(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        _ = self.write_mcp_configuration(self.main_dir, ("blender",), {str(self.main_dir): ["blender"]})
        _ = self.write_mcp_configuration(self.home / ".claude2", ("blender",), {
            str(self.main_dir): ["blender"], str(self.home / ".claude2"): [],
        })
        self.assert_mcp_readout([])

    def test_printed_mcp_repair_matches_both_directions_with_backup_and_mode(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        enabled, disabled, agreed = (str(self.root / name) for name in ("enabled", "disabled", "agreed"))
        baseline = self.write_mcp_configuration(self.main_dir, ("brp",), {
            str(self.main_dir): [], enabled: [], disabled: ["brp"], agreed: ["brp"],
        })
        config = self.write_mcp_configuration(self.home / ".claude2", ("brp", "keep"), {
            str(self.main_dir): ["brp"], enabled: ["brp"], disabled: [], agreed: ["brp"],
        })
        config.chmod(0o640)
        original, seed = config.read_bytes(), baseline.read_bytes()
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        lines = [line for line in output.splitlines() if line.startswith("claude2 turns brp ")]
        self.assertEqual(len(lines), 2)
        command = lines[0].split(": ", 1)[1]
        self.assertEqual(self.run_cli(*shlex.split(command)[2:]), (0, "", ""))
        for direction in ("off", "on"):
            self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", direction), (0, "", ""))
        backups = list(config.parent.glob(".claude.json.mcp-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o640)
        self.assertEqual(config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(baseline.read_bytes(), seed)
        document = cast(McpConfigurationFixture, json.loads(config.read_text()))
        self.assertEqual(document["oauthAccount"]["emailAddress"], "fixture@notexample.com")
        self.assertIn("keep", document["mcpServers"])
        self.assertEqual(document["projects"][agreed]["disabledMcpServers"], ["brp"])
        repaired = config.read_bytes()
        self.assertEqual(self.run_cli(*shlex.split(command)[2:]), (0, "", ""))
        self.assertEqual(config.read_bytes(), repaired)
        self.assertEqual(list(config.parent.glob(".claude.json.mcp-backup-*")), backups)

    def test_failed_atomic_mcp_repair_keeps_the_original_and_its_backup(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        _ = self.write_mcp_configuration(self.main_dir, ("brp",), {})
        config = self.write_mcp_configuration(self.home / ".claude2", (), {})
        before = config.read_bytes()
        with patch.object(atomic_file, "write_text_atomic", side_effect=OSError("fixture publish failure")):
            code, output, error = self.run_cli("mcp-repair", "claude2", "brp")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("fixture publish failure", error)
        self.assertEqual(config.read_bytes(), before)
        backups = list(config.parent.glob(".claude.json.mcp-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_printed_mcp_repair_creates_a_missing_configuration_privately(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "baseline-only")
        baseline = self.write_mcp_configuration(self.main_dir, ("brp",), {project: ["brp"]})
        seed = baseline.read_bytes()
        config = self.home / ".claude2/.claude.json"
        config.unlink()
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        line = next(line for line in output.splitlines() if line.startswith("claude2 turns brp off "))
        self.assertEqual(self.run_cli(*shlex.split(line.split(": ", 1)[1])[2:]), (0, "", ""))
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", "off"), (0, "", ""))
        self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", "on"), (0, "", ""))
        self.assertEqual(list(config.parent.glob(".claude.json.mcp-backup-*")), [])
        self.assertEqual(baseline.read_bytes(), seed)

    def test_missing_mcp_repair_preserves_a_configuration_created_during_publication(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        _ = self.write_mcp_configuration(self.main_dir, ("brp",), {})
        config = self.home / ".claude2/.claude.json"
        config.unlink()
        create = atomic_file.create_text_atomic

        def competing_create(path: Path, text: str) -> bool:
            _ = config.write_text('{"keep": true}\n')
            return create(path, text)

        with patch.object(atomic_file, "create_text_atomic", side_effect=competing_create):
            code, output, error = self.run_cli("mcp-repair", "claude2", "brp")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("changed during repair", error)
        self.assertEqual(config.read_text(), '{"keep": true}\n')

    def test_mcp_repair_refuses_a_linked_configuration_without_touching_its_target(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        _ = self.write_mcp_configuration(self.main_dir, ("brp",), {})
        config = self.home / ".claude2/.claude.json"
        target = self.root / "untouched.json"
        _ = config.rename(target)
        config.symlink_to(target)
        before = target.read_bytes()
        code, output, error = self.run_cli("mcp-repair", "claude2", "brp")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, "")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(config.is_symlink())
        self.assertIn("brp", cast(dict[str, object], accounts.read_json_object(config)["mcpServers"]))
        backups = list(config.parent.glob(".claude.json.mcp-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_mcp_project_in_one_file_compares_against_the_other_global_servers(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "one-file-only")
        for account_only, direction in ((True, "off"), (False, "on")):
            with self.subTest(account_only=account_only):
                _ = self.write_mcp_configuration(self.main_dir, ("brp",),
                                                  {} if account_only else {project: ["brp"]})
                _ = self.write_mcp_configuration(self.home / ".claude2", ("brp",),
                                                  {project: ["brp"]} if account_only else {})
                self.assert_mcp_readout([
                    f"claude2 turns brp {direction} in 1 project where ~/.claude does not: "
                    + f"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp"])
                self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", direction),
                                 (0, project + "\n", ""))

    def test_mcp_project_command_includes_baseline_only_paths_and_the_home_scope(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "baseline-only")
        _ = self.write_mcp_configuration(self.main_dir, ("brp",), {project: []})
        _ = self.write_mcp_configuration(self.home / ".claude2", (), {})
        self.assert_mcp_readout([
            "claude2 turns brp off in 2 projects where ~/.claude does not: "
            + "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp"])
        code, output, error = self.run_cli("mcp-projects", "claude2", "brp", "off")
        self.assertEqual((code, error), (0, ""))
        self.assertCountEqual(output.splitlines(), ["~", project])

    def test_mcp_project_disabled_in_both_files_is_not_reported(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "both-off")
        for directory in (self.main_dir, self.home / ".claude2"):
            _ = self.write_mcp_configuration(directory, ("brp",), {project: ["brp"]})
        self.assert_mcp_readout([])
        self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", "off"), (0, "", ""))

    def test_mcp_readout_keeps_a_separate_line_for_each_server(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        project = str(self.root / "two-servers")
        _ = self.write_mcp_configuration(self.main_dir, ("brp", "docs"), {project: []})
        _ = self.write_mcp_configuration(self.home / ".claude2", ("brp", "docs"), {project: ["brp", "docs"]})
        self.assert_mcp_readout([
            f"claude2 turns {server} off in 1 project where ~/.claude does not: "
            + f"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 {server}"
            for server in ("brp", "docs")])
        for server in ("brp", "docs"):
            self.assertEqual(self.run_cli("mcp-projects", "claude2", server, "off"), (0, project + "\n", ""))

    def test_mcp_readout_keeps_opposite_directions_for_the_same_server(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        turned_on, turned_off = (str(self.root / name) for name in ("turned-on", "turned-off"))
        _ = self.write_mcp_configuration(self.main_dir, ("brp",), {turned_on: ["brp"], turned_off: []})
        _ = self.write_mcp_configuration(self.home / ".claude2", ("brp",), {turned_on: [], turned_off: ["brp"]})
        self.assert_mcp_readout([
            f"claude2 turns brp {direction} in 1 project where ~/.claude does not: "
            + f"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp"
            for direction in ("off", "on")])
        for direction, project in (("off", turned_off), ("on", turned_on)):
            self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", direction), (0, project + "\n", ""))

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
                        self.assertIn("claude2 turns brp off in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp", lines)
                        self.assertIn("claude2 turns account-only on in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 account-only", lines)
                    else:
                        self.assertTrue(any("MCP configuration" in line and "~/.claude.json" in line
                                            and "Expecting property name" in line for line in lines))
                        self.assertNotIn("mcpServers differ", output)
                        self.assertNotIn("claude2 turns ", output)
                        status = next(item for item in accounts.account_statuses() if item.label == "claude2")
                        self.assertEqual(tuple(problem.path for problem in status.mcp_baseline_problems), (baseline,))

    def test_mcp_comparison_uses_the_same_home_project_scope(self) -> None:
        self.login_directory("claude2", "fixture@notexample.com")
        baseline = agent_accounts.claude_config_file(self.main_dir)
        config = self.home / ".claude2/.claude.json"
        _ = baseline.write_text(json.dumps({"mcpServers": {"brp": {}}, "projects": {
            str(self.main_dir): {"mcpServers": {"brp": {}}, "disabledMcpServers": ["brp"]}}}))
        for enabled in (False, True):
            _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@notexample.com"},
                "projects": {str(self.main_dir): {"mcpServers": {"brp": {}} if enabled else {}}}}))
            for flags in ((), ("--json",)):
                with self.subTest(enabled=enabled, flags=flags):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                    self.assertEqual((code, error), (0, ""))
                    lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags else output.splitlines())
                    if enabled:
                        self.assertIn("claude2 turns brp on in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp", lines)
                        self.assertEqual(self.run_cli("mcp-projects", "claude2", "brp", "on"), (0, "~\n", ""))
                    else:
                        self.assertFalse(any("mcpServers differ" in line for line in lines))
                        self.assertFalse(any(line.startswith("claude2 turns ") for line in lines))

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
                        self.assertFalse(any(line.startswith("claude2 turns ") for line in lines))
                    else:
                        self.assertIn("claude2 turns brp off in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp", lines)

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
                             else output.split("\n\n", 2)[2].strip().splitlines())
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
                    + f"add {note.parent}/claudeN.md with the login for claude2; run accounts.py setup claude2")
            else:
                _ = note.write_text("---\nname: no login\n---\n")
                expected_note = (f"claude2: cannot read {note}; fixture note denied; " if fault == "unreadable" else
                    f"claude2: not selectable; {note} has no readable login in its frontmatter; ")
                expected_note += "repair the account note for claude2 with its login; run accounts.py setup claude2"
            expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>", expected_note]
            for flags in ((), ("--json",)):
                with (self.subTest(fault=fault, flags=flags),
                      patch.object(Path, "read_text", new=read) if fault == "unreadable" else
                          patch.object(sys, "dont_write_bytecode", True)):
                    code, output, error = self.run_cli("status", "--stored", *flags)
                self.assertEqual((code, error), (0, ""))
                lines = (cast(accounts.StatusDocument, json.loads(output))["problems"] if flags
                         else output.split("\n\n", 2)[2].strip().splitlines())
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
            self.assertNotIn("claude2 turns ", output)
        _ = identity.write_text("{")
        for flags in ((), ("--json",)):
            code, output, error = self.run_cli("status", "--stored", *flags)
            self.assertEqual((code, error), (0, ""))
            self.assertIn("cannot read", output)
            self.assertNotIn("mcpServers differ", output)
            self.assertNotIn("claude2 turns ", output)

    def test_piped_status_keeps_command_errors_on_stderr(self) -> None:
        with patch.object(accounts, "_read_status", side_effect=accounts.AccountsError("accounts: fixture read failed")):
            result, stdout, stderr = self.run_cli("status", "--stored", "--json")
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("fixture read failed", stderr)

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
        self.assertIn("claude2 turns brp off in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp", stdout.splitlines())
        self.assertIn("claude2 turns account-only on in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 account-only", stdout.splitlines())

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
            ("~/.claude3", "claude3", "units ✓ 0 (busy 0, idle 0, paused 0, exited 0)",
             "12%", weekly_cell, "—", "—", read_cell),
            ("~/.claude4", "claude4", "", *("—",) * 5),
        ])
        self.assertRegex(read_cell, r"^\d\d:\d\d P[DS]T$")
        self.live_reads.assert_not_called()
        self.codex_reads.assert_not_awaited()
        self.assertFalse(accounts.account_dir("claude1").exists())
        self.assertTrue(accounts.account_dir("claude2").exists())

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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_no_action_means_status(self) -> None:
        self.write_main_config()
        self.login_directory("claude2", "main@example.invalid", default=True)

        result, stdout, stderr = self.run_cli()

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout, self.run_cli("status")[1])
        self.assertRegex(stdout, r"\| ~/\.claude2 +\| claude2 +\|")

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

                self.assertEqual(rows[-2][:3], ("~/.codex", "codex2", "workers ✓ 0 (busy 0, idle 0, paused 0, exited 0)"))
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
        anomaly = "accounts: anomaly: cannot read started holds:"
        requests: list[tuple[str, ...]] = []

        def remote(arguments: tuple[str, ...]) -> str:
            requests.append(arguments)
            with patch.object(role_map, "is_role_map_host", return_value=True):
                code, output, error = self.run_cli(*arguments)
            self.assertEqual(code, 0)
            self.assertTrue(error.startswith(anomaly))
            self.assertEqual(len(error.splitlines()), 1)
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
                        + "units 2: at 2% → claude3, hold half a point (≈5 min)\n")
            for mac_client in (False, True):
                with (self.subTest(mac_client=mac_client),
                      patch.object(role_map, "is_role_map_host", return_value=not mac_client),
                      patch.object(role_map, "ssh_role_map", side_effect=remote)):
                    code, listed, error = self.run_cli("next", "list")
                    self.assertEqual((code, listed), (0, expected))
                    if mac_client:
                        self.assertEqual(error, "")
                    else:
                        self.assertTrue(error.startswith(anomaly))
                        self.assertEqual(len(error.splitlines()), 1)
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

    def test_started_fixed_expired_and_no_pace_holds_list_exact_rows_without_state_changes(self) -> None:
        self.login_directory("claude1", "source@example.invalid")
        self.login_directory("claude2", "target@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"units": "claude1"}))
        at = datetime(2026, 10, 17, tzinfo=timezone.utc)
        cases = (
            (next_moves.FixedHold(30), next_moves.ResolvedHold(30, next_moves.FixedHold(30)),
             timedelta(minutes=18),
             "units 1: at 1.234567% → claude2, hold 30 min (12 min left)\n"),
            (next_moves.FixedHold(30), next_moves.ResolvedHold(30, next_moves.FixedHold(30)),
             timedelta(minutes=31),
             "units 1: at 1.234567% → claude2, hold 30 min (hold over)\n"),
            (next_moves.HalfPointHold(), next_moves.ResolvedHold(40, next_moves.NoUsablePace()),
             timedelta(minutes=22, seconds=1),
             "units 1: at 1.234567% → claude2, hold half a point (40 min default: no pace history, 18 min left)\n"),
        )
        for mode, duration, elapsed, expected in cases:
            with self.subTest(expected=expected):
                entry = next_moves.add_next_move("units", 1.234567, "claude2", mode)
                held = refusal_watch.HoldStarted("claude1", at, accounts.NoGroupTransition(),
                    entry.at_percent, duration)
                refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
                    auto_move_holds={entry.id: held}))
                state_path = refusal_watch.state_root() / "refusal_watch.json"
                before = state_path.read_bytes()
                with patch.object(next_moves, "render_time", return_value=at + elapsed):
                    self.assertEqual(self.run_cli("next", "list"), (0, expected, ""))
                self.assertEqual(state_path.read_bytes(), before)
                next_moves.remove_next_move("units", next_moves.FIRST_GROUP_POSITION)

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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_table_displays_both_groups_on_the_same_account_in_group_order(self) -> None:
        self.login_directory("claude2", "shared@example.invalid")
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text('{"showrunners": "claude2", "units": "claude2"}')
        code, output, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        lines = output.splitlines()
        index = next(index for index, line in enumerate(lines) if "| claude2 " in line)
        self.assertEqual([cell.strip() for cell in lines[index].strip("|").split("|")][2], "units ✓ 0 (busy 0, idle 0, paused 0, exited 0)")
        self.assertEqual([cell.strip() for cell in lines[index + 1].strip("|").split("|")],
                         ["", "", "showrunners ✓ 0 (busy 0, idle 0, paused 0, exited 0)", "", "", "", "", ""])

    def test_board_shows_upcoming_moves_between_the_table_and_problems(self) -> None:
        _ = next_moves.add_next_move("units", 1, "claude4")
        _ = next_moves.add_next_move("showrunners", 1, "claude3", fallback_targets=("claude2",))
        _ = next_moves.add_next_move("workers", 1, "codex2")
        expected = next_moves.list_next_moves()
        before = next_moves.next_moves_file().read_bytes()
        reading = accounts.StatusReading((), ("Fixture problem.",), (), timezone.utc, datetime.now(timezone.utc))
        with patch.object(accounts, "_read_status", return_value=reading):
            code, output, error = self.run_cli()
        self.assertEqual((code, error), (0, ""))
        table, upcoming, problems = output.rstrip().split("\n\n", 2)
        self.assertTrue(table.startswith("| Folder"))
        self.assertEqual(upcoming + "\n\n" + problems,
                         "Upcoming moves:\n" + expected + "\n\nFixture problem.")
        with patch.object(accounts, "_read_status", return_value=reading):
            code, document, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(document)["upcoming_moves"], ["Upcoming moves:", *expected.splitlines()])
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

    def test_board_shows_none_when_no_upcoming_moves_exist(self) -> None:
        reading = accounts.StatusReading((), ("Fixture problem.",), (), timezone.utc, datetime.now(timezone.utc))
        with patch.object(accounts, "_read_status", return_value=reading):
            code, output, error = self.run_cli()
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.endswith("\n\nUpcoming moves: none\n\nFixture problem.\n"))
        with patch.object(accounts, "_read_status", return_value=reading):
            code, document, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(document)["upcoming_moves"], ["Upcoming moves: none"])
        self.assertFalse(next_moves.next_moves_file().exists())

    def test_board_uses_upcoming_moves_from_the_role_map_without_another_fetch(self) -> None:
        reading = accounts.StatusReading((), ("Fixture problem.",), (), timezone.utc, datetime.now(timezone.utc))
        for lines in (("Upcoming moves:", "showrunners 1: at 1% → claude3, else claude2"),
                      ("Upcoming moves: none",), ()):
            for arguments in ((), ("status", "--stored", "--json")):
                with (self.subTest(lines=lines, arguments=arguments),
                      patch.object(accounts, "_read_status", return_value=reading),
                      patch.object(role_map, "is_role_map_host", return_value=False),
                      patch.object(role_map, "read_role_map", return_value=role_map.RoleMap({}, "codex1", upcoming_moves=lines)),
                      patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected second fetch")) as fetch):
                    code, output, error = self.run_cli(*arguments)
                self.assertEqual((code, error), (0, ""))
                fetch.assert_not_called()
                if arguments:
                    output = "\n".join(cast(accounts.StatusDocument, json.loads(output))["upcoming_moves"])
                expected = lines or ("Upcoming moves: unavailable; natedev unreachable",)
                self.assertIn("\n".join(expected), output)

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
        self.write_note("codex1", "workers@example.invalid", use_credits="off")
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
            f"claude3: not logged in; no readable sign-in is stored; run CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude3'))} claude and /login",
            "codex1: not set up; no folder holds this account; run accounts.py setup codex1",
        ]
        # Status must explain each empty row when the inventory has no diagnostics.
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
                         else output.split("\n\n", 2)[2].strip().splitlines())
                for line in expected:
                    self.assertEqual(notes.count(line), 1)
                self.assertFalse(any(note.startswith(("* claude2:", "* claude3:", "* codex1:")) for note in notes))

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_status_json_preserves_row_order_numeric_readings_roles_and_table_problems(self) -> None:
        read, reset = self.json_status_fixture()
        code, table, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, error), (0, ""))
        document = cast(dict[str, object], json.loads(output))
        self.assertEqual(set(document), {"accounts", "problems", "session_inputs", "upcoming_moves"})
        rows = cast(list[dict[str, object]], document["accounts"])
        keys = {"label", "tool", "roles", "selected_roles", "role_counts", "sessions", "role_counts_by_machine", "weekly_left_percent", "resets_at", "credits", "use_credits", "read_at",
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
        notes = table.split("\n\n", 2)[2].strip().splitlines()
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
            document = cast(accounts.StatusDocument, json.loads(output))
            self.assertEqual(sum("usage reading is old" in problem for problem in document["problems"]), 2)
            self.assertEqual(sum("credits reading is old" in problem for problem in document["problems"]), 1)
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
        self.assertIn(f"env CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login", stdout)
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
            ("~/.codex", "codex1", "workers ✓ 0 (busy 0, idle 0, paused 0, exited 0)"),
            ("~/.codex2", "codex2", "spare"),
            ("~/.codex4", "~/.codex4", "spare"),
            ("~/.codex10", "~/.codex10", "spare"),
            ("~/.codex3", "codex3", "—"),
        ])

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
        self.assertEqual([row[2] for row in rows], ["spare", "workers ✓ 0 (busy 0, idle 0, paused 0, exited 0)"])
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

    def test_stale_credits_are_named_and_refreshed_even_with_a_recent_weekly_reading(self) -> None:
        self.write_note("codex1", "credits@example.invalid")
        home = self.home / ".codex1"
        self.codex_login("credits@example.invalid", home)
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=15)
        reset = now + timedelta(days=3)
        usage_store.save([agent_accounts.Report("Codex", email="credits@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 100, reset)], credit_balance="12345")], old)
        stored = usage_store.load()
        usage = stored[usage_store.key("Codex", "credits@example.invalid")]
        if "weekly" not in usage:
            self.fail("fixture must save its weekly reading")
        usage["weekly"]["at"] = now.isoformat()
        _ = usage_store.store_file().write_text(json.dumps(stored))
        code, output, error = self.run_cli("status", "--stored", "--json")
        self.assertEqual((code, error), (0, ""))
        document = cast(accounts.StatusDocument, json.loads(output))
        self.assertTrue(any("credits reading is old" in problem for problem in document["problems"]))
        row = document["accounts"][0]
        self.assertIs(row["read_ok"], False)
        self.assertIsNone(row["credits"])
        self.assertFalse(row["credits_in_use"])
        code, table, error = self.run_cli("status", "--stored")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(f"stale since {old:%Y-%m-%d %H:%M %Z}", table)
        self.assertNotIn("12,345", table)
        self.codex_reads.assert_not_awaited()
        self.codex_reads.return_value = agent_accounts.Report("Codex", email="credits@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 100, reset)], credit_balance="9")
        code, output, error = self.run_cli("status", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(cast(accounts.StatusDocument, json.loads(output))["accounts"][0]["credits"], 9)
        self.codex_reads.assert_awaited_once_with(home)

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

    def test_usage_only_selection_reads_local_accounts_without_repairs_or_sessions(self) -> None:
        from scripts.message import sessions

        self.login_directory("claude2", "desktop@example.invalid")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with (patch.object(sessions, "live_sessions", side_effect=AssertionError("live session inventory")),
              patch.object(accounts, "_shared_repair_problems", side_effect=AssertionError("shared link repair")),
              patch.object(subprocess, "run", side_effect=AssertionError("external process"))):
            selection = accounts.claude_account_selection(usage_only=True)
        self.assertEqual([named.label for named in selection.named_accounts], ["claude2"])
        self.assertEqual(selection.live_directories, ())
        self.assertEqual(selection.live_logins, frozenset())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_a_refresh_without_session_credentials_keeps_a_good_reading_but_marks_its_age(self) -> None:
        self.login_directory("claude2", "desktop@example.invalid")
        at = datetime.now(timezone.utc) - timedelta(minutes=20)
        usage_store.save([agent_accounts.Report("Claude", email="desktop@example.invalid",
            source=agent_accounts.ClaudeDirectorySource(accounts.account_dir("claude2")),
            quotas=[agent_accounts.Quota("Weekly", 25, at + timedelta(days=1))],
            usage_read_outcome=agent_accounts.UsageReadOutcome.OK)], at)
        before = usage_store.store_file().read_bytes()
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude",
            problem="Login credentials unavailable in this session",
            usage_read_outcome=agent_accounts.UsageReadOutcome.CREDENTIALS_UNAVAILABLE)
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if line.startswith("| ~/.claude2"))
        self.assertIn("75%", row)
        self.assertNotIn("*", row)
        self.assertNotIn("the usage read failed", output)
        self.assertNotIn("next try", output)
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        code, document, error = self.run_cli("status", "--json")
        self.assertEqual((code, error), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(document)["accounts"])
        held = next(item for item in rows if item["label"] == "claude2")
        self.assertIs(held["read_ok"], False)
        self.assertEqual(held["weekly_left_percent"], 75)
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        self.assertEqual(self.live_reads.call_count, 2)

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_stored_status_read_failures_and_old_readings_reach_board_problems(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=15)
        cases = (
            (agent_accounts.UsageReadOutcome.STALE_TOKEN, "login token expired"),
            (agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN, "no login token"),
            (agent_accounts.UsageReadOutcome.REFUSED_401, "the usage service did not accept this login"),
            (agent_accounts.UsageReadOutcome.RATE_LIMITED_429, "the usage service is busy"),
            (agent_accounts.UsageReadOutcome.CREDENTIALS_UNAVAILABLE, "login credentials unavailable in this session"),
            (agent_accounts.UsageReadOutcome.ERROR, "the usage read failed"),
            (agent_accounts.UsageReadOutcome.OK, "usage reading is old"),
            (agent_accounts.UsageReadOutcome.HELD, "usage reading is old"),
        )
        for outcome, cause in cases:
            with self.subTest(outcome=outcome):
                usage = usage_store.StoredUsage(read_outcome=outcome, attempt_outcome=outcome,
                    attempted_at=now.isoformat(), weekly=usage_store.StoredWindow(left=75,
                        at=old.isoformat(), resets=(now + timedelta(days=1)).isoformat()))
                with usage_store.read_lock():
                    usage_store.write({"idle@example.invalid": usage})
                code, output, error = self.run_cli("status", "--stored", "--json")
                self.assertEqual((code, error), (0, ""))
                document = cast(accounts.StatusDocument, json.loads(output))
                self.assertTrue(any(cause in problem for problem in document["problems"]))
                self.assertIs(document["accounts"][0]["read_ok"], False)
                self.assertEqual(document["accounts"][0]["read_at"], old.isoformat())
                code, table, error = self.run_cli("status", "--stored")
                self.assertEqual((code, error), (0, ""))
                self.assertIn(cause, table)
        self.live_reads.assert_not_called()

    def test_login_renewal_uses_a_quiet_probe_and_keeps_the_new_reading(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        self.live[accounts.account_dir("claude2")] = agent_accounts.Report("Claude",
            email="idle@example.invalid", usage_read_outcome=agent_accounts.UsageReadOutcome.OK,
            quotas=[agent_accounts.Quota("Weekly", 10, datetime.now(timezone.utc) + timedelta(days=1))])
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"private", b"private")) as probe:
            self.assertEqual(accounts.renew_login("claude2"), accounts.LoginRenewed())
        probe.assert_called_once_with([str(accounts.SCRIPTS / "lib/py"), str(accounts.SCRIPTS / "accounts" / "accounts.py"),
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
        self.live[accounts.account_dir("claude2")].usage_read_outcome = agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(accounts.renew_login("claude2"),
                             accounts.LoginRenewalFailed("usage still shows no login token"))

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_missing_login_token_has_an_actionable_cause_in_table_and_json_on_both_machines(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        at = datetime(2026, 10, 22, 15, 11, tzinfo=timezone.utc)
        usage_store.save([agent_accounts.Report("Claude", email="idle@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN)], at)
        for platform in ("darwin", "linux"):
            for failed in (False, True):
                path = accounts.login_renewals_file()
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(json.dumps({"claude2": {"at": at.isoformat(), "outcome": "failed",
                    "detail": "probe exited with status 7"}} if failed else {}))
                cause = "no login token" + ("; a probe session at 08:11 failed" if failed else "")
                step = "log in on it by hand" if failed else "start a session on it"
                expected = f"claude2: usage unavailable; {cause}; {step}"
                for flags in ((), ("--json",)):
                    with self.subTest(platform=platform, failed=failed, flags=flags), patch.object(sys, "platform", platform):
                        code, output, error = self.run_cli("status", "--stored", *flags)
                        self.assertEqual((code, error), (0, ""))
                        self.assertIn(expected, output)
                        self.assertNotIn("next try", output)
                        self.assertNotIn("next notes pass", output)
        self.live_reads.assert_not_called()

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
                    self.assertIn("login token expired; start a session on it", output)
        self.live_reads.assert_not_called()

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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failed_read_causes_show_old_date_star_and_one_footnote_until_a_good_read(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        cases = (
            (agent_accounts.UsageReadOutcome.STALE_TOKEN,
             "login token expired; start a session on it"),
            (agent_accounts.UsageReadOutcome.REFUSED_401,
             "the usage service did not accept this login; start a session on it to check it"),
            (agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
             "the usage service is busy; this shows the reading from 01-02 05:29; the attempt time is not recorded"),
            (agent_accounts.UsageReadOutcome.ERROR,
             "the usage read failed; this shows the reading from 01-02 05:29; the attempt time is not recorded"),
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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_usage_failure_notes_name_the_service_and_retry_time_in_table_and_json(self) -> None:
        self.login_directory("claude2", "idle@example.invalid")
        at = datetime(2026, 10, 19, 15, 11, tzinfo=timezone.utc)
        cases = (
            (agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
             "the usage service is busy; this shows the reading from 10-19 08:09; last read attempted at 10-19 08:11",
             "the usage service is busy; no reading is stored yet; last read attempted at 10-19 08:11"),
            (agent_accounts.UsageReadOutcome.ERROR,
             "the usage read failed; this shows the reading from 10-19 08:09; last read attempted at 10-19 08:11",
             "the usage read failed; no reading is stored yet; last read attempted at 10-19 08:11"),
            (agent_accounts.UsageReadOutcome.REFUSED_401,
             "the usage service did not accept this login; start a session on it to check it",
             "the usage service did not accept this login; start a session on it to check it"),
            (agent_accounts.UsageReadOutcome.STALE_TOKEN,
             "login token expired; start a session on it",
             "login token expired; start a session on it"),
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
        self.assertIn("the usage service is busy; this shows the reading from 01-02 05:29; the attempt time is not recorded", output)
        self.assertEqual(usage_store.store_file().read_bytes(), before)

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
                         ["* claude2: usage unavailable; the usage read failed; this shows the reading from 01-02 05:29; the attempt time is not recorded"])
        self.assertEqual(usage_store.load()["idle@example.invalid"].get("weekly"), window)
        self.live_reads.assert_called_once_with(directory)

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
                         ["* claude2: usage unavailable; the usage read failed; this shows the reading from 01-02 05:29; the attempt time is not recorded"])
        self.live_reads.assert_not_called()

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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_failed_codex_start_keeps_its_last_good_read_time_and_shows_a_footnote(self) -> None:
        _, target = self.workers_fixture()
        old = datetime(2001, 1, 2, 13, 29, tzinfo=timezone.utc)
        usage_store.save([agent_accounts.Report("Codex", email="target@example.invalid",
            source=agent_accounts.CodexHomeSource(target),
            quotas=[agent_accounts.Quota("Weekly", 25, old + timedelta(days=1))])], old)
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem="Startup request timed out",
            source=agent_accounts.CodexHomeSource(target),
            codex_startup=agent_accounts.CodexStartupFailure(target, "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup codex1"))
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        row = next(line for line in output.splitlines() if "| codex1 " in line)
        self.assertEqual(row.split("|")[-2].strip(), "01-02 05:29 PST*")
        self.assertIn("* codex1: usage unavailable; Codex did not start at ~/.codex1; this shows the reading from 01-02 05:29; run ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup codex1 and try again; the attempt time is not recorded", output)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup codex1", row)

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
        report = self.reading("main@example.invalid", agent_accounts.Quota("5-hour", 50, resets),
                              agent_accounts.Quota("Weekly", 50, resets))
        usage_store.save([report], datetime.now(timezone.utc))
        setting = resets.astimezone(ZoneInfo("Europe/Paris")).strftime("%a %m-%d %H:%M %Z")

        with patch.dict(os.environ, {"USER_ZONE": "Europe/Paris"}):
            with patch.object(showrunners, "registered_showrunners", return_value=[]):
                self.assertEqual(self.status_rows()[0][4], setting)
            with patch.object(showrunners, "registered_showrunners", side_effect=OSError("session records unreadable")):
                self.assertEqual(self.status_rows()[0][4], setting)
            def failed_discovery() -> None:
                print("skipping fixture-showrunner: no showrunner staff row", file=sys.stderr)
                raise OSError("session records unreadable")

            saved = role_map.CachedAfterFetchFailure(datetime(2026, 10, 16, 15, 24, tzinfo=timezone.utc),
                                                     "fixture transport failed")
            with (patch.object(showrunners, "registered_showrunners", side_effect=failed_discovery) as discovery,
                  patch.object(role_map, "observed_map_provenance", return_value=saved)):
                code, output, errors = self.run_cli("status")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn(setting, output)
            self.assertIn(saved.at.astimezone(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M %Z"), output)
            self.assertNotIn("skipping fixture-showrunner", output)
            discovery.assert_not_called()

            usage_store.store_file().unlink()
            self.live[accounts.account_dir("claude2")] = replace(report,
                store_warnings=(agent_accounts.UsageClaimFailed("fixture claim denied"),))
            with (patch.object(showrunners, "registered_showrunners", side_effect=failed_discovery) as discovery,
                  patch.object(accounts, "datetime", RoleWriteTime)):
                code, output, errors = self.run_cli("status")
            self.assertEqual((code, errors), (0, ""))
            read_at = RoleWriteTime.now(timezone.utc).astimezone(ZoneInfo("Europe/Paris"))
            self.assertIn(f"fixture claim denied; read at {read_at:%Y-%m-%d %H:%M %Z}", output)
            self.assertIn("usage store read continued without a hold", output)
            self.assertIn(setting, output)
            self.assertNotIn("skipping fixture-showrunner", output)
            discovery.assert_not_called()

            self.live.clear()
            _ = self.workers_fixture()
            usage_store.save([report, *(agent_accounts.Report("Codex", email=login,
                five_hour=agent_accounts.Quota("5-hour", 100, resets),
                quotas=[agent_accounts.Quota("Weekly", 100, resets)], credit_balance="0")
                for login in ("target@example.invalid", "default@example.invalid"))], datetime.now(timezone.utc))
            refusals.write_refusal(refusals.AccountRefusal("codex", "target@example.invalid",
                refusals.KnownWindow("weekly"), refusals.KnownReset(resets), saved.at))
            with (patch.object(showrunners, "registered_showrunners", side_effect=failed_discovery) as discovery,
                  patch.object(accounts, "_check_codex_home", return_value=())):
                code, output, errors = self.run_cli("status", "--detail")
            self.assertEqual((code, errors), (0, ""))
            detail_reset = resets.astimezone(ZoneInfo("Europe/Paris")).strftime("%a %Y-%m-%d %H:%M %Z")
            self.assertIn("Codex workers: codex2; on refusal: no codex account has room; "
                          + f"the earliest reset is {detail_reset}", output)
            self.assertIn(setting, output)
            self.assertNotIn("skipping fixture-showrunner", output)
            discovery.assert_not_called()
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
        with patch.object(accounts, "user_zone", return_value=zone) as resolve:
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

        with scratch_account_home(later_home, {}, clear=False):
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

    def workers_member(self, name: str = "project-review") -> staff.StaffRow:
        _ = self.enterContext(patch.object(import_module("scripts.herdr.worker_panes"), "worker_run", return_value=self.root / "worker-run"))
        owner = fixture_member("showrunner", name="worker-owner")
        unit = fixture_member("unit", name="worker-unit", parent=owner.id,
                              worktree=staff.PresentWorktree("natedev", self.root))
        return fixture_worker(name, unit.id, backend=staff.CodexBackend(self.home / ".codex"))

    def test_workers_swap_records_the_callers_auto_reason_and_cause(self) -> None:
        _ = self.workers_fixture()
        _ = accounts.swap("workers", "codex1", why="auto: workers-entry", cause="auto")
        rows = [cast(dict[str, object], json.loads(line))
                for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual((rows[-1]["role"], rows[-1]["cause"], rows[-1]["why"]),
                         ("workers", "auto", "auto: workers-entry"))

    def test_detached_workers_swap_selects_the_target_before_starting_one_unit(self) -> None:
        default, target = self.workers_fixture()
        self.assertEqual(accounts.role_account("workers"), accounts.CodexWorkersAccount("codex2", default))
        commands: list[list[str]] = []
        expected_label, expected_home = "codex1", target

        def start(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            self.assertEqual(accounts.read_role_map().workers, expected_label)
            self.assertEqual(accounts.role_account("workers").home, expected_home)
            commands.append(command)
            inactive = command[0] == "systemctl" and not any(saved[0] == "systemd-run" for saved in commands)
            return subprocess.CompletedProcess(command, int(inactive), "", "")

        with (patch.object(shutil, "which", return_value="/fixture/systemd-run"),
              patch.object(subprocess, "run", side_effect=start),
              patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")),
              patch.object(accounts, "move_workers", side_effect=AssertionError("move must run outside the pass"))):
            result = accounts.swap("workers", "codex1", why="auto: workers-entry", cause="auto", worker_moves="detach")
            expected_label, expected_home = "codex2", default
            repeated = accounts.swap("workers", "codex2", why="newer selection", cause="auto", worker_moves="detach")
        self.assertIsInstance(result, accounts.WorkersSwapStarted)
        self.assertIsInstance(repeated, accounts.WorkersSwapStarted)
        self.assertEqual(commands, [
            ["systemctl", "--user", "is-active", "--quiet", "accounts-workers-move"],
            ["systemd-run", "--user", "--collect", "--quiet", "--unit=accounts-workers-move",
             str(accounts.SCRIPTS / "lib/py"), str(accounts.SCRIPTS / "accounts/accounts.py"),
             "move", "workers", "codex1", "--follow-role", "--why", "auto: workers-entry"],
            ["systemctl", "--user", "is-active", "--quiet", "accounts-workers-move"],
        ])

    def test_direct_codex_move_and_preview_refuse_a_target_outside_the_workers_role(self) -> None:
        _ = self.workers_fixture()
        before = self.role_files(), Path(os.environ["STAFF_FILE"]).read_bytes()
        for dry_run in (False, True):
            with (self.subTest(dry_run=dry_run),
                  patch.object(pane_moves, "move_worker", side_effect=AssertionError("mismatched move started")),
                  patch.object(pane_moves, "preview_worker", side_effect=AssertionError("mismatched preview started")),
                  patch.object(accounts, "_launch_worker_moves", side_effect=AssertionError("mismatched unit started"))):
                result = accounts.move_workers("codex1", dry_run=dry_run)
            self.assertEqual(result, accounts.MoveRefused(
                "workers role is codex2; run accounts.py swap workers codex1 to select it"))
            self.assertEqual((self.role_files(), Path(os.environ["STAFF_FILE"]).read_bytes()), before)

    def test_detached_move_finishes_on_the_latest_role_and_settles_each_group_request(self) -> None:
        _ = self.workers_fixture()
        for follow_role, change_after, old_snapshot in (
                (False, 1, False), (False, 2, False), (True, 1, False), (True, 2, False), (True, 1, True)):
            with self.subTest(follow_role=follow_role, change_after=change_after, old_snapshot=old_snapshot):
                case = f"{follow_role}-{change_after}-{old_snapshot}"
                workers = (self.workers_member("first-" + case), self.workers_member("second-" + case))
                earlier_id, latest_id = "earlier-" + case, "latest-" + case
                between_id = "between-" + case
                journal_snapshot: list[moves.MoveEntry] = []

                def request(identity: str, target_label: str) -> moves.RequestedMove:
                    entry = moves.RequestedMove(kind="requested", at="2026-11-10T22:00:00Z",
                        tool="codex", subject="workers", move_id=identity, cause="auto", why="auto: " + identity,
                        **{"from": "codex2", "to": target_label}, window="weekly")
                    moves.append_move(entry)
                    return entry

                calls: list[tuple[staff.StaffId, str]] = []

                def move(row: staff.StaffRow, record: staff.StaffSnapshot, target_label: str,
                         why: str, run: Path) -> accounts.SessionMoveOutcome:
                    del record, why, run
                    lock_path = moves.store_file().with_name("workers_move.run.lock")
                    with lock_path.open("a") as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    calls.append((row.id, target_label))
                    if len(calls) == change_after:
                        if old_snapshot:
                            _ = request(between_id, "codex1")
                            _ = accounts.swap("workers", "codex1", why="intermediate selection", cause="auto", worker_moves="detach")
                        latest = request(latest_id, "codex2")
                        _ = accounts.swap("workers", "codex2", why="newer selection", cause="auto", worker_moves="detach",
                            worker_move_journal=[*journal_snapshot, latest] if old_snapshot else moves.READ_CURRENT_MOVE_JOURNAL)
                        if old_snapshot:
                            pending = cast(dict[str, object], json.loads(moves.store_file().with_name("workers_move.json").read_text()))
                            self.assertEqual(pending["requests"], [earlier_id, between_id, latest_id])
                    return accounts.SessionMoveRefused(row.id, "fixture worker refused")

                with (patch.object(accounts, "_launch_worker_moves"),
                      patch.object(pane_moves, "move_worker", side_effect=move)):
                    _ = request(earlier_id, "codex1")
                    _ = accounts.swap("workers", "codex1", why="earlier selection", cause="auto", worker_moves="detach")
                    journal_snapshot = moves.read_moves()
                    result = accounts.move_workers("codex1", follow_role=follow_role)
                assert isinstance(result, accounts.GroupMoveStartFailed)
                self.assertEqual(result.target, "codex2")
                for row in workers:
                    self.assertEqual([target_label for identity, target_label in calls if identity == row.id][-1], "codex2")
                self.assertEqual(sum(saved[1] == "codex1" for saved in calls), change_after)
                expected_ids = [earlier_id, between_id, latest_id] if old_snapshot else [earlier_id, latest_id]
                outcomes = [row for row in moves.read_moves()
                            if row["kind"] != "requested" and row["move_id"] in expected_ids]
                self.assertEqual(outcomes[0]["kind"], "failed")
                assert outcomes[0]["kind"] == "failed"
                self.assertEqual(outcomes[0]["reason"], "superseded by codex2")
                if old_snapshot:
                    self.assertEqual(outcomes[1]["kind"], "failed")
                    assert outcomes[1]["kind"] == "failed"
                    self.assertEqual(outcomes[1]["reason"], "superseded by codex2")
                    self.assertEqual(outcomes[2]["kind"], "confirmed")
                else:
                    self.assertEqual(outcomes[1]["kind"], "confirmed")
                self.assertEqual([row["move_id"] for row in outcomes], expected_ids)
                self.assertFalse(moves.store_file().with_name("workers_move.json").exists())
                with patch.object(staff, "require_stopped", return_value=None):
                    for row in workers:
                        self.assertEqual(staff.retire(row.id), row)

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
        self.assertIn("codex1 has no account note", stderr)
        self.assertEqual(stderr.count("accounts: "), 1, stderr)

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

    def test_workers_swap_selects_the_target_before_stops_and_keeps_pending_and_refused_workers(self) -> None:
        for missing_settings in (False, True):
            with self.subTest(missing_settings=missing_settings):
                _, target = self.workers_fixture()
                failed = self.workers_member("project-review")
                pending = self.workers_member("project-test")
                machine = cast(Callable[[], str], getattr(accounts, "_session_counts_machine"))()
                producer = fixture_member("producer", name="codex-producer", backend=staff.CodexBackend(self.home / ".codex2"),
                    doc=staff.LiveDoc(machine, self.root / "codex-producer/plan.md"),
                    worktree=staff.PresentWorktree(machine, self.root / "codex-producer"))
                for name, paused, document_machine, worktree_machine in (
                        ("paused-producer", True, machine, machine),
                        ("remote-document-producer", False, remote.other_machine(), machine),
                        ("remote-worktree-producer", False, machine, remote.other_machine())):
                    excluded = fixture_member("producer", name=name,
                        doc=staff.LiveDoc(document_machine, self.root / name / "plan.md"),
                        worktree=staff.PresentWorktree(worktree_machine, self.root / name))
                    if paused:
                        _ = staff.set_hold(str(excluded.id), staff.Paused("now", "user", "hold"))
                _ = fixture_member("producer", name="claude-producer")
                if missing_settings:
                    _ = fixture_member("producer", name="missing-settings-producer",
                        doc=staff.LiveDoc(machine, self.root / "missing-settings-producer/plan.md"),
                        worktree=staff.PresentWorktree(machine, self.root / "missing-settings-producer"))
                registry = write_registry(self.root / "producer-models.conf", {"producers": {
                    "codex-producer": "codex:gpt-test:high", "claude-producer": "claude:opus:max",
                    "paused-producer": "codex:gpt-test:high", "remote-document-producer": "codex:gpt-test:high",
                    "remote-worktree-producer": "codex:gpt-test:high"}})
                _ = cast(object, self.enterContext(patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry), "PATH": MODELS_RESOLVER_PATH})))
                before = Path(os.environ["STAFF_FILE"]).read_bytes()

                observed: list[Path] = []
                def move(row: staff.StaffRow, record: staff.StaffSnapshot, target_label: str,
                         why: str, run: Path) -> accounts.SessionMoveOutcome:
                    del why
                    self.assertEqual(record.backends[row.id].family, "codex")
                    self.assertEqual((target_label, run), ("codex1", self.root / "worker-run"))
                    observed.append(agent_accounts.workers_home())
                    return (accounts.SessionMoveRefused(row.id, "source process could not stop") if row.id == failed.id
                            else stop_results.ResumePending(row.id, target_label, "destination is not ready"))

                with (patch.object(pane_moves, "move_worker", side_effect=move) as stopped,
                      patch.object(stop_resume, "move_sessions", return_value=[stop_results.ResumePending(
                          producer.current_session, "codex1", "destination is not ready")]) as producer_move):
                    code, output, errors = self.run_cli("swap", "workers", "codex1")
                if missing_settings:
                    self.assertEqual(code, 1)
                    self.assertEqual(len(errors.splitlines()), 1)
                    self.assertIn("missing-settings-producer", errors)
                    self.assertIn("invalid producer settings", errors)
                else:
                    self.assertEqual((code, errors), (1, ""), errors)
                self.assertEqual(agent_accounts.workers_home(), target)
                self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
                self.assertEqual({cast(staff.StaffRow, entry.args[0]).id for entry in stopped.call_args_list}, {failed.id, pending.id})
                self.assertEqual(stopped.call_count, 2)
                producer_move.assert_called_once_with([str(producer.current_session)], "codex1", why="by hand",
                                                     cause="by hand", source_accounts={})
                self.assertEqual(observed, [target, target])
                self.assertEqual(self.run_cli("role", "workers"), (0, "codex1\n", ""))
                lines = output.splitlines()
                self.assertEqual(len(lines), 3)
                failed_line = next(line for line in lines if failed.id in line)
                self.assertIn("source process could not stop", failed_line)
                self.assertNotIn("resumed", failed_line)
                pending_line = next(line for line in lines if pending.id in line)
                self.assertIn("pending", pending_line)
                self.assertIn("codex1", pending_line)
                self.assertNotIn("failed", pending_line)
                with (patch.object(pane_moves, "preview_worker", return_value=accounts.SessionMoveRefused(
                          failed.id, "preview refusal")) as preview,
                      patch.object(stop_resume, "move_sessions") as producer_move):
                    code, output, errors = self.run_cli("swap", "workers", "codex1", "--dry-run")
                if missing_settings:
                    self.assertEqual(code, 0)
                    self.assertEqual(len(errors.splitlines()), 1)
                    self.assertIn("missing-settings-producer", errors)
                    self.assertIn("invalid producer settings", errors)
                else:
                    self.assertEqual((code, errors), (0, ""))
                self.assertEqual({cast(staff.StaffRow, entry.args[0]).id for entry in preview.call_args_list}, {failed.id, pending.id})
                self.assertEqual(len(output.splitlines()), 3)
                self.assertIn(f"{producer.name} ({producer.id}): would stop and resume", output)
                producer_move.assert_not_called()

    def test_worker_release_refusals_name_the_reason_and_next_action(self) -> None:
        _ = self.workers_fixture()
        role_map.write_role_map(role_map.RoleMapChange("workers", "codex1", "fixture selection", datetime.now(timezone.utc).isoformat()))
        worker = self.workers_member()
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        for result, reason, action in (
            (accounts.SessionMoveRefused(worker.id, "source is absent; check the staff row and retry"),
             "source is absent", "check the staff row and retry"),
            (accounts.SessionMoveRefused(worker.id, "fixture stop failed; inspect the source session and retry"),
             "fixture stop failed", "inspect the source session and retry"),
        ):
            with self.subTest(reason=reason), patch.object(pane_moves, "move_worker", return_value=result) as stopped:
                code, output, errors = self.run_cli("move", "workers", "codex1")
                self.assertEqual((code, output), (1, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertIn(worker.id, errors)
                self.assertIn(reason, errors)
                self.assertIn(action, errors)
                self.assertEqual(cast(staff.StaffRow, stopped.call_args.args[0]).id, worker.id)
                self.assertEqual(stopped.call_count, 1)
                self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)

    def test_worker_release_when_not_running_leaves_resume_pending(self) -> None:
        _ = self.workers_fixture()
        role_map.write_role_map(role_map.RoleMapChange("workers", "codex1", "fixture selection", datetime.now(timezone.utc).isoformat()))
        worker = self.workers_member()
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        with patch.object(pane_moves, "move_worker",
                          return_value=stop_results.ResumePending(worker.id, "codex1", "destination is not ready")) as stopped:
            code, output, errors = self.run_cli("move", "workers", "codex1")
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertIn(f"{worker.id}: stopped, resume pending on codex1:", errors)
        self.assertEqual(cast(staff.StaffRow, stopped.call_args.args[0]).id, worker.id)
        self.assertEqual(stopped.call_count, 1)
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)


    def test_numbered_workers_targets_keep_the_selected_home_with_real_or_shared_entries(self) -> None:
        target = "codex2"
        for shared in (False, True):
            with self.subTest(shared=shared, target=target):
                default, _ = self.workers_fixture()
                if shared:
                    _ = (default / "config.toml").write_text("own config")
                    (default / "sessions").mkdir(exist_ok=True)
                    for name in ("config.toml", "sessions"):
                        _ = (self.home / ".codex1" / name).symlink_to(default / name)
                self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
                report = cast(agent_accounts.Report, self.codex_reads.return_value)
                report.email = "default@example.invalid"
                report.source = agent_accounts.CodexHomeSource(default)
                worker = self.workers_member()
                before = self.role_files(), Path(os.environ["STAFF_FILE"]).read_bytes()
                with (patch.object(pane_moves, "move_worker",
                                   return_value=stop_results.ResumePending(worker.id, target, "destination is not ready")) as stop,
                      patch.object(pane_moves, "preview_worker", return_value=pane_moves.WorkerResume(
                          worker_panes.StoppedWorker(worker, "fixture-pane", "fixture-terminal"),
                          "impl", "codex1"))):
                    code, stdout, stderr = self.run_cli("swap", "workers", target, *([] if shared else ["--dry-run"]))
                self.assertEqual((code, stderr), (1 if shared else 0, ""))
                if shared:
                    self.assertIn("pending", stdout)
                    self.assertEqual(cast(staff.StaffRow, stop.call_args.args[0]).id, worker.id)
                    self.assertEqual(stop.call_count, 1)
                else:
                    self.assertEqual(stdout, f"{worker.name} ({worker.id}): would stop and resume the same conversation on {target} in its worker pane\n")
                    self.assertEqual((self.role_files(), Path(os.environ["STAFF_FILE"]).read_bytes()), before)
                    stop.assert_not_called()
                    self.assertEqual(self.run_cli("set-role", "workers", target), (0, "", ""))
                self.assertEqual(agent_accounts.workers_home(), default)
                self.assertEqual(self.run_cli("role", "workers"), (0, target + "\n", ""))
                self.assertEqual(accounts.role_account("workers").home, default)
                self.assertEqual(json.loads(accounts.accounts_config_file().read_text())["workers"], target)

    def test_workers_swap_checks_usage_and_credit_policy_before_changing_roles(self) -> None:
        _, target = self.workers_fixture()
        now = datetime.now(timezone.utc)
        cases = (
            ("role-write-refused", "on", accounts.AccountsError("role write refused"), False, False,
                agent_accounts.Report("Codex", email="target@example.invalid")),
            ("credits-disabled", "off", None, True, False, agent_accounts.Report("Codex", email="target@example.invalid",
                quotas=[agent_accounts.Quota("Weekly", 100, now + timedelta(days=1))], credit_balance="6370")),
            ("exhausted", "on", None, True, False, agent_accounts.Report("Codex", email="target@example.invalid",
                five_hour=agent_accounts.Quota("5-hour", 100, now + timedelta(hours=1)),
                quotas=[agent_accounts.Quota("Weekly", 20, now + timedelta(days=1))], credit_balance="0")),
            ("credits", "on", None, False, False, agent_accounts.Report("Codex", email="target@example.invalid",
                five_hour=agent_accounts.Quota("5-hour", 100, now + timedelta(hours=1)), credit_balance="5")),
            ("unknown", "on", None, False, True, agent_accounts.Report("Codex", problem="read failed")),
            ("stale", "on", None, False, True, agent_accounts.Report("Codex", five_hour=agent_accounts.Quota("5-hour", 0, now - timedelta(seconds=1)),
                quotas=[agent_accounts.Quota("Weekly", 0, now + timedelta(days=1))], credit_balance="0")),
        )
        for case, credits, write_error, refusal, anomalies, report in cases:
            with (self.subTest(case=case), patch.object(stop_resume, "stop") as stop,
                  patch.object(accounts, "_write_role", wraps=getattr(accounts, "_write_role"), side_effect=write_error)):
                self.write_note("codex1", "target@example.invalid", use_credits=credits)
                report.source = agent_accounts.CodexHomeSource(target)
                self.codex_reads.return_value = report
                before = self.role_files()
                code, stdout, stderr = self.run_cli("swap", "workers", "codex1")
                stop.assert_not_called()
                if write_error:
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertIn("role write refused", stderr)
                    self.assertEqual(self.role_files(), before)
                elif refusal:
                    self.assertNotEqual(code, 0)
                    self.assertIn("codex1", stdout + stderr)
                    self.assertIn("~/.codex1", stdout + stderr)
                    self.assertEqual(self.role_files(), before)
                    if credits == "off":
                        self.assertIn("is used up and set not to use credits; choose another account or wait for reset", stdout + stderr)
                        self.assertIn("codex1 (~/.codex1)", stdout + stderr)
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(self.run_cli("role", "workers"), (0, "codex1\n", ""))
                    if anomalies:
                        self.assertEqual(len(stderr.splitlines()), 1)
                        self.assertIn("accounts: anomaly: codex1 (~/.codex1):", stderr)
                        self.assertEqual(stdout, "\n")

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
        command = stdout.index(f"env CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login")
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
        process = subprocess.Popen([str(accounts.SCRIPTS / "lib" / "py"), str(Path(accounts.__file__)), "setup", "codex1"],
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
        self.assertIn(f"env CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login", output)

    def test_table_workers_role_ignores_conflicting_process_home(self) -> None:
        default, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        self.assertEqual(self.run_cli("set-role", "workers", "codex1"), (0, "", ""))
        with patch.dict(os.environ, {"CODEX_HOME": str(default)}):
            rows = self.status_rows()[1:]
        self.assertEqual([(row[0], row[2]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "workers ✓ 0 (busy 0, idle 0, paused 0, exited 0)")])
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
        self.assertEqual([(row[0], row[2]) for row in rows], [("~/.codex", "spare"), ("~/.codex1", "workers ✓ 0 (busy 0, idle 0, paused 0, exited 0)")])
        self.assertEqual(agent_accounts.workers_home(), default)
        self.assertNotEqual(target, default)

    def test_numbered_home_start_failure_row_names_setup(self) -> None:
        _, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        problem = 'Codex did not start at ~/.codex1; run ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup "codex1" and try again'
        startup = agent_accounts.CodexStartupFailure(target, '~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup "codex1"')
        self.codex_reads.return_value = agent_accounts.Report("Codex", problem=problem,
            source=agent_accounts.CodexHomeSource(target), codex_startup=startup)
        rows = self.status_rows()[1:]
        target_row = next(row for row in rows if row[0] == "~/.codex1")
        self.assertIn('~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup "codex1"', " ".join(target_row))

    def test_table_uses_the_startup_result_command_independently_of_problem_text(self) -> None:
        _, target = self.workers_fixture()
        self.login_directory("claude2", "main@example.invalid", default=True)
        command = '~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup "codex1"'
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
        self.assertLess(stdout.index("initialize"), stdout.index(f"env CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login"))

    def test_legacy_unit_role_file_leaves_workers_unconfigured(self) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text('{"unit": "claude3"}')
        message = "accounts: workers has no mapped account; run accounts.py set-role workers <codexN>"
        self.assertEqual(self.run_cli("role", "workers"), (1, "", message + "\n"))
        with self.assertRaises(ValueError) as refused:
            _ = agent_accounts.workers_home()
        self.assertEqual(str(refused.exception), message)

    def test_trust_skips_a_relative_folder_and_continues_with_the_other_folders(self) -> None:
        paths = self.write_trust_fixture()
        write_account_settings(self.trust_config, folders=("relative", "~/worktrees"))
        code, _output, error = self.run_cli("trust")
        self.assertEqual(code, 0, error)
        self.assertIn("anomaly", error)
        for path in paths:
            saved = cast(dict[str, object], json.loads(path.read_text()))
            projects = cast(dict[str, dict[str, object]], saved["projects"])
            self.assertTrue(projects[str(self.home / "worktrees")]["hasTrustDialogAccepted"])
















@final
class PickerTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 13, 12, tzinfo=timezone.utc).astimezone(tz)


ZERO_AGE = timedelta()


NO_PICK_EXCLUSIONS: frozenset[str] = frozenset()
NO_PICK_KEEP = accounts.NoKeep()


@final
class PaneWorkerMoveTests(unittest.TestCase):
    """Move native workers with isolated staff, accounts, processes, and panes."""

    def __init__(self, methodName: str = "runTest") -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        super().__init__(methodName)
        self.fixture = StaffTestCase()
        self.root = Path()
        self.world = PaneWorld()
        self.run_dir = Path()
        self.owner_id = staff.StaffId("")
        self.stop = MagicMock()
        self.target_check = MagicMock()

    @property
    def owner(self) -> staff.StaffRow:
        row = staff.by_id(self.owner_id)
        assert row is not None
        return row

    def move(self, target: str, *, why: str = "") -> accounts.GroupMoveStarted | accounts.GroupMoveStartFailed:
        if target.startswith("codex"):
            role_map.write_role_map(role_map.RoleMapChange("workers", target, "fixture selection", datetime.now(timezone.utc).isoformat()))
        result = accounts.move_workers(target, why=why)
        assert isinstance(result, (accounts.GroupMoveStarted, accounts.GroupMoveStartFailed))
        return result

    @override
    def setUp(self) -> None:
        from scripts.production.staff_test_support import StaffTestCase

        self.fixture = StaffTestCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.agents import restart
        from scripts.lib.test_models_registry import write_registry
        from scripts.lib import unit_runs

        self.world = PaneWorld()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        runner = fixture_member("showrunner", name="showrunner")
        self.owner_id = fixture_member("unit", name="director", parent=runner.id,
                                       worktree=staff.PresentWorktree("natedev", self.root)).id
        self.world.add(self.owner, "w1:p0")
        config = self.root / "conf"
        config.mkdir()
        _ = (config / "accounts.conf").write_text(
            (Path(__file__).resolve().parents[2] / "config/accounts.conf").read_text())
        _ = (config / "direct.conf").write_text("# range: 0.1..0.9\nWORKER_SPLIT_RATIO=0.667\n"
                                               + "# whole: 1..\nUNIT_COMPACT_ABOVE_TOKENS=43000\n"
                                               + "# range: 1..600\nPANE_AGENT_START_SECONDS=90\n")
        _ = (config / "shutdown.conf").write_text("# whole: 1..300\nSTOP_GRACE_SECONDS=2\n# whole: 1..300\nKILL_GRACE_SECONDS=5\n# whole: 1..300\nMOVE_STOP_MARGIN_SECONDS=10\n")
        registry = write_registry(config / "models.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(config), "MODELS_CONFIG_FILE": str(registry), "MODELS_CONFIG_PUBLISH": "0",
            "UNIT_RUN_DIR": str(self.run_dir), "XDG_STATE_HOME": str(self.root / "state"),
        })))
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "runs"))
        _ = self.enterContext(patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.world))
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=self.world.native_process))
        _ = self.enterContext(patch.object(herdr, "_startup_processes", side_effect=self.world.descendants))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.world.launch))
        self.stop = cast(MagicMock, self.enterContext(patch.object(restart, "stop_row", side_effect=self.stop_worker)))
        _ = self.enterContext(patch.object(accounts, "_codex_move_target"))
        _ = self.enterContext(patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(accounts.NamedAccount("claude2", self.root / ".claude2"))))
        _ = self.enterContext(patch.object(accounts, "_codex_account_home", side_effect=self.account_home))
        _ = self.enterContext(patch.object(accounts, "codex_home_label", side_effect=self.account_label))
        _ = self.enterContext(patch.object(accounts, "claude_config_label", side_effect=self.account_label))
        _ = self.enterContext(patch.object(accounts, "claude_account_dir", side_effect=self.account_home))
        _ = self.enterContext(patch.object(accounts, "account_directory_readiness", return_value=accounts.AccountDirectoryReady("fixture")))
        _ = self.enterContext(patch.object(accounts, "prepare_claude_launch", side_effect=self.prepare_claude))
        _ = self.enterContext(patch.object(agent_accounts, "codex_account_home", side_effect=self.codex_home))
        _ = self.enterContext(patch.object(restart, "session_launch", return_value=("claude", {"CLAUDE_CONFIG_DIR": str(self.root / ".claude1")}, [])))
        _ = self.enterContext(patch.object(agent_accounts, "codex_home_role_label", side_effect=self.account_label))
        self.target_check = cast(MagicMock, self.enterContext(patch.object(herdr, "check_target", return_value=True)))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.claude_live))
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.claude_process_home))
        _ = self.enterContext(patch.object(account, "process_codex_home", side_effect=self.codex_process_home))
        _ = self.enterContext(patch.object(herdr, "read_conversation", side_effect=self.conversation))

    def stop_worker(self, row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
        self.assertAlmostEqual(deadline - time.monotonic(), 17, delta=0.5)
        return self.world.stop(row, client)

    def account_home(self, label: str) -> Path:
        return self.root / ("." + label)

    @staticmethod
    def account_label(home: Path) -> str:
        return home.name.removeprefix(".")

    def prepare_claude(self, label: str, *, resume: bool) -> accounts.PreparedAccount:
        self.assertTrue(resume)
        return accounts.PreparedAccount({"CLAUDE_CONFIG_DIR": str(self.account_home(label))}, frozenset())

    def codex_home(self, label: str) -> agent_accounts.CodexHomeHoldingLogin:
        return agent_accounts.CodexHomeHoldingLogin(label, self.account_home(label), "fixture")

    def claude_process_home(self, pid: int) -> account.ConfiguredClaudeFolder:
        return account.ConfiguredClaudeFolder(self.process_home(pid, "CLAUDE_CONFIG_DIR", ".claude1"))

    def codex_process_home(self, pid: int) -> account.ProcessCodexHome:
        return account.ProcessCodexHome(self.process_home(pid, "CODEX_HOME", ".codex1"))

    def process_home(self, pid: int, key: str, default: str) -> Path:
        identity = self.world.processes[pid].current_identity
        request = next((item for item in reversed(self.world.requests) if identity in item[1]), None)
        return Path(request[2][key]) if request is not None else self.root / default

    def claude_live(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        pid = next((pid for pid, kind, _ in self.world.foreground.values()
                    if kind == "claude" and self.world.processes[pid].current_identity == identity), None)
        return sessions.LiveSessionProcess(pid) if pid is not None else sessions.SessionNotLive()

    def conversation(self, identity: str, kind: str = "") -> herdr.LiveConversation | herdr.StoppedConversation:
        candidates = [(pid, family) for pid, family, _ in self.world.foreground.values()
                      if self.world.processes[pid].current_identity == identity and (not kind or family == kind)]
        return herdr.LiveConversation(cast(Literal["claude", "codex"], candidates[0][1]), (candidates[0][0],)) if candidates else herdr.StoppedConversation()

    def worker(self, name: str = "implementer", *, family: Literal["claude", "codex"] = "claude", live: bool = True) -> staff.StaffRow:
        from scripts.lib.test_models_registry import write_registry

        _ = write_registry(self.root / "conf/models.conf", {"assignments": {"direct": family}})
        row = fixture_worker(name, self.owner.id, backend=staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(self.root / ".codex1"))
        pane = "w1:p" + str(len(self.world.locations))
        self.world.add(row, pane, family=family, live=live)
        return row

    def dispatch(self, row: staff.StaffRow, role: str = "impl", status: str = "active") -> None:
        if role in staff.WORKER_ROLES:
            _ = staff.set_worker_role(row.id, staff.KnownWorkerRole(role))
        atomic_file.write_text_atomic(self.run_dir / "progress_history_state.json", json.dumps({
            "owner_session": self.owner.current_session, "history_file": str(self.run_dir / "history.jsonl"),
            "pass": {row.name: {"worker_name": row.name, "team_slot": row.name, "kind": role,
                                 "status": status, "started_at": time.time(), "instance_id": "dispatch-" + row.name,
                                 "launcher_command": ["assign.sh", "--to", row.name]}}}))
        if status == "active":
            from scripts.direct import progress_history
            from scripts.production import worker_admission

            instance = "dispatch-" + row.name
            line = (f'Read the unchanged prompt file "{self.root / "prompt.md"}". '
                    f"End your final reply with this line alone: DONE {instance}\n"
                    "A reply that waits on a detached run is not final.")
            progress_history.save_dispatch_delivery(self.run_dir, instance,
                progress_history.DispatchDelivery(line, instance, str(self.root / "reply.md")))
            backend = staff.backend(row)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            entry = ({"type": "event_msg", "payload": {"type": "user_message", "message": line}}
                     if isinstance(backend, staff.CodexBackend) else
                     {"type": "user", "message": {"role": "user", "content": line}})
            atomic_file.write_text_atomic(worker_admission.transcript_source(row, backend), json.dumps(entry) + "\n")
        if status == "completed":
            from scripts.production import worker_admission

            backend = staff.backend(row)
            assert isinstance(backend, staff.ClaudeBackend)
            atomic_file.write_text_atomic(worker_admission.transcript_source(row, backend), json.dumps({
                "type": "assistant", "message": {"content": [{"type": "text", "text": "DONE dispatch-" + row.name}]}}) + "\n")

    def test_busy_move_keeps_the_pane_and_conversation_and_rerun_changes_nothing(self) -> None:
        row = self.worker()
        _ = self.fixture.worker_run("a-first", "unit", self.owner.current_session, director_run=True,
            passes=({"worker_name": row.name, "kind": "review", "status": "active", "started_at": 0,
                     "instance_id": "old-dispatch", "launcher_command": ["assign.sh", "--old"]},))
        self.run_dir = self.fixture.worker_run("z-own", "unit", self.owner.current_session)
        self.dispatch(row)
        before = self.fixture.staff_state()
        result = self.move("claude2", why="quota")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.world.launches, [herdr.ExistingPane("w1:p1", "terminal-w1:p1")])
        self.assertIn(row.current_session, self.world.requests[0][1])
        prompt = next(command for command in self.world.commands if command[:2] == ("agent", "prompt"))
        self.assertIn("Continue pass dispatch-implementer", prompt[-1])
        self.assertIn("moved from claude1 to claude2", prompt[-1])
        self.assertEqual(self.target_check.call_args.args[0], row.current_session)
        self.assertEqual(cast(herdr.HerdrPlace, self.target_check.call_args.kwargs["place"]).pane, "w1:p1")
        journal = moves.store_file().read_bytes()
        with patch.object(moves, "append_move", wraps=moves.append_move) as append:
            rerun = self.move("claude2")
        append.assert_not_called()
        self.assertEqual(rerun.returncode, 0)
        self.assertEqual(moves.store_file().read_bytes(), journal)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.fixture.staff_state(), before)
        self.assertEqual(self.world.closed, [])
        self.assertIn("w1:p0", self.world.foreground)
        self.assertEqual([item["kind"] for item in moves.read_moves()], ["requested", "confirmed"])
        self.assertEqual(moves.read_moves()[0].get("terminal"), "terminal-w1:p1")

    def test_idle_move_sends_no_prompt_and_does_not_touch_sibling_or_director(self) -> None:
        row = self.worker()
        self.dispatch(row, status="completed")
        sibling = fixture_member("unit", name="sibling", parent=self.owner.parent)
        self.world.add(sibling, "w2:p8")
        processes = {pane: value for pane, value in self.world.foreground.items() if pane != "w1:p1"}
        before = self.fixture.staff_state()
        self.stop.side_effect = self.world.stop
        result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(command[:2] == ("agent", "prompt") for command in self.world.commands))
        self.assertEqual({pane: self.world.foreground[pane] for pane in processes}, processes)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.requests, [])
        self.assertNotIn("w1:p1", self.world.locations)
        self.assertIn("idle pane closed", result.stdout)
        self.stop.assert_not_called()
        self.assertEqual(self.fixture.staff_state(), before)
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertNotIn("nothing to move", result.stderr)
        self.assertEqual(moves.read_moves(), [])

    def test_move_elapsed_time_includes_stopping_the_worker(self) -> None:
        row = self.worker()
        self.dispatch(row)
        with patch.object(time, "monotonic", return_value=100.0) as clock:
            def stop(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
                stopped = self.stop_worker(row, client, deadline=deadline)
                clock.return_value = 102.5
                return stopped

            self.stop.side_effect = stop
            result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = result.outcomes[0]
        assert isinstance(outcome, stop_results.ClaudeUp)
        self.assertEqual(outcome.elapsed_seconds, 2.5)
        self.assertIn("Claude is up on claude2 in w1:p1 (2.5 s)", result.stdout)

    def test_codex_move_reads_the_process_account_and_resumes_the_same_thread(self) -> None:
        row = self.worker(family="codex")
        self.dispatch(row)
        result = self.move("codex2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex is up on codex2", result.stdout)
        self.assertEqual(self.world.requests[0][0], "codex")
        self.assertEqual(self.world.requests[0][1][:2], ["resume", row.current_session])
        self.assertEqual(self.world.requests[0][2]["CODEX_HOME"], str(self.root / ".codex2"))
        result = self.move("codex2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged; Codex runs on codex2", result.stdout)
        self.assertEqual(len(self.world.launches), 1)
        member = accounts.LiveGroupMember(str(row.current_session), row.name, "units", self.root / ".codex2")
        self.assertTrue(accounts.session_on_account(member, "codex2"))

    def test_stopped_worker_resumes_in_the_same_pane(self) -> None:
        row = self.worker(live=False)
        self.dispatch(row)
        result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stop.assert_not_called()
        self.assertEqual(self.world.launches, [herdr.ExistingPane("w1:p1", "terminal-w1:p1")])

        self.assertEqual(self.world.closed, [])

    def test_stopped_idle_worker_move_closes_the_pane_and_keeps_the_conversation(self) -> None:
        row = self.worker(live=False)
        self.dispatch(row, status="completed")
        before = self.fixture.staff_state()
        result = self.move("claude2")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("nothing to move", result.stderr)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.requests, [])
        self.assertNotIn("w1:p1", self.world.locations)
        self.assertIn("idle pane closed", result.stdout)
        self.assertEqual(self.fixture.staff_state(), before)
        self.stop.assert_not_called()
        self.assertEqual(moves.read_moves(), [])

    def test_idle_pane_close_refusals_keep_the_worker_and_record_no_move(self) -> None:
        row = self.worker()
        before = self.fixture.staff_state()
        for refusal in (worker_panes.Kept("close is held"),
                        worker_panes.UncertainWorker(row, "pane identity is unclear")):
            with self.subTest(refusal=refusal), patch.object(worker_panes, "close", return_value=refusal):
                result = self.move("claude2")
                reason = refusal.blocker if isinstance(refusal, worker_panes.Kept) else refusal.reason
                self.assertEqual(result.outcomes, (accounts.SessionMoveRefused(row.id, reason),))
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)
                self.assertEqual(self.world.closed, [])
                self.assertEqual(self.world.launches, [])
                self.assertEqual(self.world.requests, [])
                self.assertIn("w1:p1", self.world.locations)
                self.assertEqual(self.fixture.staff_state(), before)
                self.assertEqual(moves.read_moves(), [])
                self.stop.assert_not_called()

    def test_unreadable_active_dispatch_keeps_the_worker_and_reports_one_anomaly(self) -> None:
        row = self.worker()
        self.dispatch(row)
        atomic_file.write_text_atomic(self.run_dir / "progress_history_state.json", "{broken\n")
        before = self.fixture.staff_state()
        result = self.move("claude2")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count("anomaly:"), 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.fixture.staff_state(), before)
        self.stop.assert_not_called()

    def test_closed_worker_move_completes_without_starting_a_pane_or_recording_a_target(self) -> None:
        from scripts.lib.test_models_registry import write_registry

        _ = write_registry(self.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        row = fixture_worker("closed", self.owner.id, backend=staff.ClaudeBackend())
        before = self.fixture.staff_state()
        with patch.object(moves, "append_move") as append:
            result = self.move("claude2")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.outcomes, (accounts.SessionMoveRefused(row.id, "nothing to move", 0),))
        self.assertIn("nothing to move", result.stderr)
        append.assert_not_called()
        self.stop.assert_not_called()
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.requests, [])
        self.assertEqual(self.fixture.staff_state(), before)

    def test_closed_worker_without_a_conversation_has_nothing_to_move(self) -> None:
        from scripts.lib.test_models_registry import write_registry

        _ = write_registry(self.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        row = fixture_worker("closed", self.owner.id, backend=staff.ClaudeBackend())
        empty = replace(row, current_session=staff.StaffId(""))
        record = staff.local_snapshot()
        record = replace(record, rows=tuple(empty if item.id == row.id else item for item in record.rows))
        with patch.object(staff, "local_snapshot", return_value=record), patch.object(moves, "append_move") as append:
            result = self.move("claude2")
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to move", result.stderr)
        append.assert_not_called()
        self.assertEqual(self.world.launches, [])
        self.stop.assert_not_called()

    def test_worker_move_preview_lists_actions_and_refusals_and_counts_empty_rows(self) -> None:
        opened = self.worker("open", family="codex")
        stopped = self.worker("stopped", family="codex", live=False)
        unknown = self.worker("unassigned", family="codex", live=False)
        closed = fixture_worker("closed", self.owner.id, backend=staff.CodexBackend(self.root / ".codex1"))
        empty = fixture_worker("empty", self.owner.id, backend=staff.CodexBackend(self.root / ".codex1"))
        other = self.worker("other-family", family="claude")
        opened = staff.set_worker_role(opened.id, staff.KnownWorkerRole("impl"))
        stopped = staff.set_worker_role(stopped.id, staff.KnownWorkerRole("impl"))
        record = staff.local_snapshot()
        record = replace(record, rows=tuple(replace(row, current_session=staff.StaffId(""))
                                           if row.id == empty.id else row for row in record.rows))
        before = self.fixture.staff_state()
        role_map.write_role_map(role_map.RoleMapChange("workers", "codex2", "fixture selection", datetime.now(timezone.utc).isoformat()))
        with patch.object(staff, "local_snapshot", return_value=record):
            result = accounts.move_workers("codex2", dry_run=True)
        assert isinstance(result, accounts.GroupMoveDryRun)
        self.assertEqual(sorted(result.lines), sorted((
            f"{opened.name} ({opened.id}): would close its idle pane; the next assign resumes it on codex2",
            f"{stopped.name} ({stopped.id}): would close its idle pane; the next assign resumes it on codex2",
            f"{unknown.name} ({unknown.id}): would close its idle pane; the next assign resumes it on codex2",
            "2 workers: nothing to move",
        )))
        for row in (closed, empty, other):
            self.assertNotIn(str(row.id), "\n".join(result.lines))
        self.assertEqual(self.fixture.staff_state(), before)
        self.assertEqual(self.world.stops, [])
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.requests, [])

    def test_move_workers_catches_the_worker_panes_herdr_failure(self) -> None:
        from scripts.herdr import worker_panes

        row = self.worker()
        self.dispatch(row)
        with patch.object(worker_panes, "start_or_resume", side_effect=herdr_types.HerdrFailure("fixture start failed")):
            result = self.move("claude2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("fixture start failed", result.stderr)
        self.assertEqual(self.world.launches, [])

    def test_move_workers_reads_the_same_refusal_class_from_pane_moves(self) -> None:
        from scripts.accounts import pane_moves

        row = self.worker()
        self.dispatch(row, role="unknown-role")
        with patch.object(pane_moves, "run_for_worker", return_value=self.run_dir) as read_run:
            result = self.move("claude2")
        read_run.assert_called_once_with(row, staff.local_snapshot())
        self.assertEqual(len(result.outcomes), 1)
        self.assertIs(type(result.outcomes[0]), accounts.SessionMoveRefused)
        self.assertEqual(result.returncode, 1)
        self.assertIn("dispatch role is unavailable", result.stderr)

    def test_resume_message_continues_the_moved_workers_own_pass(self) -> None:
        from scripts.lib.resume_message import AccountMove, BusyPass, resume_message

        text = resume_message("quota", move=AccountMove("claude1", "claude2"), own_pass=BusyPass("dispatch-busy"))
        self.assertIn("Continue pass dispatch-busy", text)
        self.assertIn("moved from claude1 to claude2", text)
        self.assertNotIn("idle", text)

    def test_missing_role_starts_nothing(self) -> None:
        row = self.worker()
        self.dispatch(row, role="unknown-role")
        result = self.move("claude2")
        self.assertIn("dispatch role is unavailable", result.stderr)
        self.assertEqual(self.world.launches, [])
        self.stop.assert_not_called()

    def test_unknown_worker_variants_leave_the_worker_untouched(self) -> None:
        from scripts.herdr import worker_panes

        row = self.worker()
        self.dispatch(row)
        for state in (worker_panes.UncertainWorker(row, "ambiguous identity"),
                      worker_panes.ProcessUnknownWorker(row, "w1:p1", "terminal-w1:p1", "process unreadable")):
            with self.subTest(state=state), patch.object(worker_panes, "observe", return_value=[state]):
                result = self.move("claude2")
                self.assertIn("anomaly:", result.stderr)
                self.assertEqual(self.world.launches, [])
                self.stop.assert_not_called()

    def test_stop_requires_fresh_stopped_evidence_before_resuming(self) -> None:
        row = self.worker()
        self.dispatch(row)
        with patch.object(self.world, "stop", return_value=True):
            result = self.move("claude2")
        self.assertIn("stop did not prove the worker stopped", result.stderr)
        self.assertEqual(self.world.launches, [])

    def test_a_held_worker_lock_keeps_only_that_worker(self) -> None:
        from scripts.herdr import worker_panes

        first = self.worker("first")
        second = self.worker("second")
        self.dispatch(second)
        with worker_panes.worker_lock(self.owner.id, staff.WorkerName.parse(first.name)):
            result = self.move("claude2")
        self.assertIn("dispatch lock is held", result.stderr)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.stops, [second.current_session])

    def test_every_failed_start_variant_preserves_the_row_and_sends_no_prompt(self) -> None:
        from scripts.herdr import worker_panes

        row = self.worker(live=False)
        self.dispatch(row)
        states: list[worker_panes.WorkerStart] = [
            worker_panes.IncompleteStart(row, "w1:p1", "incomplete"),
            worker_panes.AdmissionRefused("exact admission refusal"),
            worker_panes.UnpublishedWorker("w1:p1", "publication failed"),
            worker_panes.ProcessUnknownWorker(row, "w1:p1", "terminal-w1:p1", "unknown process"),
            worker_panes.UncertainWorker(row, "uncertain"),
            worker_panes.StartRefused(worker_panes.StartRefusal.ACCOUNT_UNAVAILABLE, "exact account detail"),
        ]
        before = self.fixture.staff_state()
        for state in states:
            with self.subTest(state=state), patch.object(worker_panes, "start_or_resume", return_value=state):
                result = self.move("claude2")
                self.assertNotEqual(result.returncode, 0)
                if isinstance(state, worker_panes.StartRefused):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(state.reason.value + ": " + state.detail, result.stderr)
                if isinstance(state, worker_panes.AdmissionRefused):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(state.reason, result.stderr)
                self.assertFalse(any(command[:2] == ("agent", "prompt") for command in self.world.commands))
                self.assertEqual(self.fixture.staff_state(), before)

    def test_incomplete_target_process_prevents_a_source_start(self) -> None:
        row = self.worker()
        self.dispatch(row)
        self.world.outcome = herdr.PromptNotSubmitted("not ready")
        result = self.move("claude2")
        self.assertIn("source not started", result.stderr)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.requests[0][2]["CLAUDE_CONFIG_DIR"], str(self.root / ".claude2"))
        self.assertFalse(any(command[:2] == ("agent", "prompt") for command in self.world.commands))

    def test_worker_move_uses_the_known_row_role_without_history(self) -> None:
        from scripts.direct import progress_history

        row = self.worker()
        self.dispatch(row)
        with patch.object(progress_history, "last_dispatch", return_value=progress_history.NoDispatchRecorded()):
            result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.world.requests[-1][2]["CLAUDE_CONFIG_DIR"], str(self.root / ".claude2"))

    def test_rollback_and_destination_use_the_latest_dispatch_role(self) -> None:
        from scripts.herdr import worker_panes

        row = self.worker()
        self.dispatch(row, "impl")
        self.dispatch(row, "review")
        start = worker_panes.start_or_resume
        seen: list[tuple[str, str]] = []

        def fail_target(parent: staff.StaffId, name: staff.WorkerName, role: staff.WorkerRole | worker_panes.WorkerRoleUnavailable, *,
                        account: worker_panes.ConfiguredWorkerAccount | worker_panes.DestinationWorkerAccount,
                        lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse) -> worker_panes.WorkerStart:
            assert isinstance(account, worker_panes.DestinationWorkerAccount)
            assert isinstance(role, str)
            seen.append((role, account.label))
            return (worker_panes.StartRefused(worker_panes.StartRefusal.ACCOUNT_UNAVAILABLE, "target unavailable")
                    if account.label == "claude2" else start(parent, name, role, account=account, lock=lock))

        with patch.object(worker_panes, "start_or_resume", side_effect=fail_target):
            result = self.move("claude2")
        self.assertIn("source resumed on claude1", result.stderr)
        self.assertEqual(seen, [("review", "claude2"), ("review", "claude1")])
        self.assertEqual(len(self.world.launches), 1)
        result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plan", self.world.requests[-1][1])

    def test_transcript_refusal_marks_the_same_open_pass_before_the_move(self) -> None:
        from scripts.direct import progress_history

        row = self.worker()
        self.dispatch(row)
        transcript = Path.home() / ".claude" / "projects" / "project" / (str(row.current_session) + ".jsonl")
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
            "message": {"content": [{"type": "text", "text": "You've hit your limit"}]},
            "uuid": "quota-refusal", "timestamp": datetime.now(timezone.utc).isoformat()}) + "\n")
        self.assertIsInstance(refusal_watch.transcript_refusal(str(row.current_session)), refusal_watch.TranscriptRefusal)
        result = self.move("claude2")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        passed = cast(dict[str, dict[str, object]], state["pass"])[row.name]
        self.assertEqual((passed["instance_id"], passed["status"]), ("dispatch-implementer", "refused"))
        self.assertNotIn("finished_at", passed)
        before = (self.run_dir / "progress_history_state.json").read_bytes()
        progress_history.mark_pass_refused(self.run_dir, row.name)
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), before)



class PickerTests(unittest.TestCase):
    home: Path = Path()
    now: datetime = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(os, "execvpe", side_effect=AssertionError("unexpected exec in picker test")))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(scratch_account_home(self.home, {
            "AGENT_NOTES_DIR": str(self.home / "notes"), "USER_ZONE": "UTC",
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
        if label != "default":
            create_claude_account(label, login)
            return
        config = self.home / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        _ = (self.home / "notes" / note_name("claude2")).write_text(f"---\nlogin: {login}\n---\n")

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
        with usage_store.read_lock():
            saved = usage_store.load()
            saved[usage_store.key(tool, login)] = usage
            usage_store.write(saved)

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

    def test_shell_preconditions_report_one_anomaly_and_launch_continues(self) -> None:
        loader = fake_nix_loader()
        for result in (FakeShellRefusal("nix_env: IN_NIX_SHELL is not set"), object()):
            with (self.subTest(result=result), patch.object(loader, "ensure", return_value=result),
                  patch.object(accounts, "import_module", side_effect=imports_with_nix_loader(loader)),
                  patch.object(os, "execvpe") as launch):
                code, stdout, stderr = self.group_cli("exec", "claude2")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(len(stderr.splitlines()), 1)
            self.assertIn("accounts: anomaly:", stderr)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(self.home / ".claude2"))

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

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unnoted_claude_home_is_displayed_but_never_selected_or_a_destination(self) -> None:
        (self.home / "notes/claude2.md").unlink()
        self.assert_picked("claude3")
        picked = accounts.pick("claude", self.now, exclude=frozenset({"claude1", "claude3"}))
        self.assertIsInstance(picked, accounts.NoAccountWithRoom)
        self.assertEqual(accounts.placement("units", self.now, exclude=frozenset()).label, "claude3")
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"claude2: not selectable; claude2 has no account note at {self.home / 'notes/claude2.md'}; add ~/notes/claudeN.md with the login for claude2; run accounts.py setup claude2", stdout.splitlines())
        for command in (("move", "units", "claude2"), ("move", "some-session", "claude2"),
                        ("move", "units", "default")):
            with self.subTest(command=command):
                code, stdout, stderr = self.group_cli(*command)
                self.assertNotEqual(code, 0)
                self.assertIn("no account note", stdout + stderr)
        with patch.object(os, "execvpe") as launch:
            self.assertEqual(self.group_cli("exec", "claude2"), (0, "", ""))
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(self.home / ".claude2"))
        code, stdout, stderr = self.group_cli("move", "units", "claude1")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(accounts.role_account("unit").dir, self.home / ".claude1")

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_legacy_group_for_an_unnoted_home_is_display_only_and_cannot_launch(self) -> None:
        path = self.groups(units="default")
        original = path.read_bytes()
        (self.home / "notes/claude2.md").unlink()
        groups = accounts.group_accounts()
        self.assertEqual(accounts.group_label(groups, "units"), "home account unresolved")
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"claude2: not selectable; claude2 has no account note at {self.home / 'notes/claude2.md'}; add ~/notes/claudeN.md with the login for claude2; run accounts.py setup claude2", stdout.splitlines())
        with self.assertRaisesRegex(accounts.AccountsError, "home account unresolved"):
            _ = accounts.role_account("unit")
        self.assertEqual(path.read_bytes(), original)

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_unnoted_codex_home_is_displayed_but_never_picked_or_assigned_to_workers(self) -> None:
        self.codex("default", "unnoted@example.invalid")
        self.codex("codex2", "spare@example.invalid")
        (self.home / "notes/codex1.md").unlink()
        self.reading("Codex", "unnoted@example.invalid", 90, 1)
        self.reading("Codex", "spare@example.invalid", 50, 3)
        message = f"codex1 has no account note at {self.home / 'notes/codex1.md'}"
        self.assert_picked("codex2", tool="codex")
        picked = accounts.pick("codex", self.now, exclude=frozenset({"codex2"}))
        self.assertIsInstance(picked, accounts.NoAccountWithRoom)
        code, stdout, stderr = self.group_cli("status")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("~/.codex: not selectable; this login has no account note; add ~/notes/codexN.md with the login for ~/.codex", stdout.splitlines())
        for command in (("swap", "workers", "codex1"), ("set-role", "workers", "codex1")):
            with self.subTest(command=command):
                code, stdout, stderr = self.group_cli(*command)
                self.assertNotEqual(code, 0)
                self.assertIn(message, stdout + stderr)
        code, stdout, stderr = self.group_cli("swap", "workers", "default")
        self.assertNotEqual(code, 0)
        self.assertIn("name the account by number", stdout + stderr)
        self.assertEqual(self.group_cli("set-role", "workers", "codex2")[0], 0)
        self.assertEqual(accounts.role_account("workers").label, "codex2")

    @patch.object(accounts, "is_test_login", new=provider_fixture_login)
    def test_missing_note_repairs_name_the_configured_notes_folder(self) -> None:
        self.codex("default", "fixture-codex")
        notes = self.home / "configured-notes"
        _ = (self.home / "notes").rename(notes)
        (notes / "claude2.md").unlink()
        (notes / "codex1.md").unlink()
        config = Path(os.environ["CONF_DIR"]) / "accounts.conf"
        _ = config.write_text(config.read_text().replace(
            "notes_directory=~/rust/hanadocs/agents", f"notes_directory={notes}"))
        clear_notes_directory_cache()
        _ = os.environ.pop("AGENT_NOTES_DIR", None)
        self.assertEqual(note_file.notes_dir(), notes)
        code, stdout, stderr = self.group_cli("status", "--stored")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("add ~/configured-notes/claudeN.md with the login for claude2", stdout)
        self.assertIn("add ~/configured-notes/codexN.md with the login for ~/.codex", stdout)
        self.assertIn("add ~/configured-notes/codexN.md", accounts.unnoted_home_line(self.home / ".codex", "fixture"))
        self.assertNotIn("~/rust/hanadocs/agents", stdout)

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

    def test_picker_messages_use_the_configured_reading_age(self) -> None:
        config = conf.path("accounts")
        _ = config.write_text(config.read_text().replace("usage_stale_minutes=10", "usage_stale_minutes=5"))
        for login in ("main@example.invalid", "first@example.invalid", "third@example.invalid"):
            self.reading("Claude", login, 50, 2, age=timedelta(minutes=6))
        _, output, _ = self.selection(explain=True)
        self.assertIn("has room; no reading in the last 5 minutes", output)
        self.assertNotIn("last 10 minutes", output)
        self.codex("codex1", "worker@example.invalid")
        self.reading("Codex", "worker@example.invalid", 50, 2, age=timedelta(minutes=6))
        _, output, _ = self.selection(tool="codex", explain=True)
        self.assertIn("no reading in the last 5 minutes", output)
        self.assertNotIn("last 10 minutes", output)

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
        self.assertEqual([row[1] for row in rows if row[2].startswith("units ✓")], ["claude3"])
        self.assertEqual([row[1] for row in rows if row[2].startswith("showrunners ✓")], ["claude1"])

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
            roles=("workers",), role_counts=(accounts.RoleSessionCounts(0, 0, 0, 0),), weekly_left_percent=60, resets_at=self.at + timedelta(days=1),
            credits=13446, on_credits="no", use_credits=True, read_at=self.at,
            read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=self.at)

    def reading(self, row: accounts.StatusRow) -> accounts.StatusReading:
        return accounts.StatusReading(rows=(row,), notes=("* Fixture problem.",), detail_lines=(),
                                      zone=ZoneInfo("America/New_York"), checked_at=self.at)

    def document(self, row: accounts.StatusRow) -> dict[str, object]:
        status = cast(dict[str, object], json.loads(accounts.render_status_json(self.reading(row))))
        self.assertEqual(status["problems"], ["Fixture problem."])
        return cast(list[dict[str, object]], status["accounts"])[0]

    def test_multiple_roles_have_separate_rows_without_repeated_account_cells(self) -> None:
        row = replace(self.row(), roles=("units", "showrunners"),
                      role_counts=(accounts.RoleSessionCounts(1, 1, 0, 1), accounts.RoleSessionCounts(1, 1, 0, 0)))
        reading = self.reading(row)
        table = accounts.render_status_table(reading)
        cells = [tuple(cell.strip() for cell in line.strip("|").split("|"))
                 for line in table.split("\n\n", 1)[0].splitlines()[2:]]
        self.assertEqual(len(cells), 2)
        first_role = replace(row, roles=("units",), role_counts=row.role_counts[:1])
        single = accounts.render_status_table(self.reading(first_role))
        self.assertEqual(cells[0], tuple(cell.strip() for cell in single.splitlines()[2].strip("|").split("|")))
        self.assertEqual(cells[1], ("", "", "showrunners 2 (busy 1, idle 1, paused 0, exited 0)", "", "", "", "", ""))
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        self.assertEqual(len(document["accounts"]), 1)
        self.assertEqual(document["accounts"][0]["roles"], ["units", "showrunners"])
        self.assertEqual(document["accounts"][0]["role_counts"], [
            {"busy": 1, "paused": 0, "exited": 1, "idle": 1, "total": 3},
            {"busy": 1, "paused": 0, "exited": 0, "idle": 1, "total": 2}])

    def test_usage_failure_timing_names_the_last_attempt_without_promising_a_retry(self) -> None:
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        read = datetime(2026, 10, 16, 10, 58, tzinfo=timezone.utc)
        read_problem = cast(Callable[..., list[accounts.StatusProblem]], getattr(accounts, "_read_failure_problem"))
        for outcome in (agent_accounts.UsageReadOutcome.ERROR, agent_accounts.UsageReadOutcome.RATE_LIMITED_429):
            cases = (
                (now, "last read attempted at 12:00"),
                (now - timedelta(minutes=3), "last read attempted at 11:57"),
                (now - timedelta(minutes=3, seconds=1), "last read attempted at 11:56"),
                (read, "last read attempted at 10-16 10:58"),
                (accounts.StatusUnavailable(), "the attempt time is not recorded"),
                (now + timedelta(minutes=1), "last read attempted at 12:01"),
            )
            for attempted, step in cases:
                with self.subTest(outcome=outcome, attempted=attempted):
                    row = replace(self.row(), read_at=read, attempted_at=attempted, read_outcome=outcome)
                    problems = read_problem(row, timezone.utc, now=now)
                    cause = "the usage read failed" if outcome == agent_accounts.UsageReadOutcome.ERROR else "the usage service is busy"
                    line = f"* ~/.codex1: usage unavailable; {cause}; this shows the reading from 10-16 10:58; {step}"
                    self.assertEqual([accounts.render_status_problem(problem) for problem in problems], [line])
                    reading = replace(self.reading(row), notes=(line,), checked_at=now)
                    self.assertIn(line, accounts.render_status_table(reading))
                    self.assertEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"],
                        [line.removeprefix("* ")])
                    self.assertNotIn("next try", line)
        row = replace(self.row(), read_at=now, attempted_at=now, read_outcome=agent_accounts.UsageReadOutcome.ERROR)
        line = accounts.render_status_problem(read_problem(row, timezone.utc, now=now)[0])
        self.assertIn("this shows the reading from 12:00; last read attempted at 12:00", line)
        self.assertNotIn("11-01", line)

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

    def test_old_reading_dot_uses_the_existing_refresh_age_limit(self) -> None:
        for outcome in (agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD):
            for age, expected in ((timedelta(minutes=10), True), (timedelta(minutes=10, microseconds=1), False)):
                with self.subTest(outcome=outcome, age=age):
                    row = replace(self.row(), read_outcome=outcome, read_at=self.at - age)
                    self.assertIs(self.document(row)["read_ok"], expected)

    def test_usage_and_credit_readings_just_after_status_time_are_fresh(self) -> None:
        read_at = self.at + timedelta(seconds=0.1)
        credit_reading = cast(Callable[[usage_store.StoredUsage, datetime],
                                      tuple[str, float | Literal["unlimited"] | accounts.StatusUnavailable]],
                              getattr(accounts, "_credit_reading"))
        read_problem = cast(Callable[..., list[accounts.StatusProblem]], getattr(accounts, "_read_failure_problem"))
        usage = usage_store.StoredUsage(credits={"at": read_at.isoformat(), "balance": "13446"})
        on_credits, credits = credit_reading(usage, self.at)
        self.assertEqual(credits, 13446)
        self.assertNotEqual(on_credits, "stale")
        for outcome in (agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD):
            with self.subTest(outcome=outcome):
                row = replace(self.row(), read_outcome=outcome, read_at=read_at, on_credits=on_credits, credits=credits)
                problems = read_problem(row, timezone.utc, now=self.at)
                self.assertEqual(problems, [])
                reading = replace(self.reading(row), notes=tuple(accounts.render_status_problem(problem) for problem in problems))
                document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
                self.assertEqual(document["problems"], [])
                self.assertIs(document["accounts"][0]["read_ok"], True)
                self.assertEqual(document["accounts"][0]["read_at"], read_at.isoformat())
                self.assertEqual(document["accounts"][0]["credits"], 13446)

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

    def test_json_signals_and_text_table_show_the_same_zero_worker_counts(self) -> None:
        reading = self.reading(self.row())
        self.assertEqual(accounts.render_status_table(reading), (
            '| Folder    | Label   | Role                                           | Weekly left | Weekly resets       | On credits | Credits left | Read      |\n'
            '| --------- | ------- | ---------------------------------------------- | ----------- | ------------------- | ---------- | ------------ | --------- |\n'
            '| ~/.codex1 | fixture | workers 0 (busy 0, idle 0, paused 0, exited 0) | 60%         | Sat 10-17 14:00 EDT | no         | 13,446       | 14:00 EDT |\n'
            '\n'
            '* Fixture problem.'
        ))
        self.assertEqual(self.document(self.row()), {
            "label": "fixture", "tool": "codex", "roles": ["workers"], "selected_roles": [], "role_counts": [{"busy": 0, "paused": 0, "exited": 0, "idle": 0, "total": 0}],
            "weekly_left_percent": 60, "resets_at": "2026-10-17T18:00:00+00:00",
            "credits": 13446, "use_credits": True, "read_at": "2026-10-16T18:00:00+00:00",
            "read_ok": True, "credits_in_use": False, "role_counts_by_machine": {}, "sessions": [],
        })


class StatusProblemCauseTests(unittest.TestCase):
    """Status reports one cause and one repair for each home problem."""

    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(scratch_account_home(self.home, {
            "AGENT_NOTES_DIR": str(self.home / "notes"),
            "CODEX_HOME": str(self.home / ".codex"), "USER_ZONE": "UTC",
        }, clear=True)))
        cache = session_counts_cache()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps({"machine": remote.other_machine(),
            "at": datetime.now(timezone.utc).isoformat(), "accounts": {}}))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no real child")))
        _ = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("no real child")))

    def reading(self, login: str, *, noted: bool = False, diagnostics: tuple[str, ...] = ()) -> accounts.StatusReading:
        home = self.home / ".codex"
        home.mkdir(exist_ok=True)
        auth = home / "auth.json"
        _ = auth.write_text("fixture sign-in")
        at = datetime(2026, 10, 29, 1, 14, tzinfo=timezone.utc)
        os.utime(auth, (at.timestamp(), at.timestamp()))
        status = accounts.CodexStatus("~/.codex", home, True, True,
            "codex1" if noted else accounts.NO_VALUE, login, "workers",
            diagnostics if noted else (accounts.unnoted_home_line(home, login), *diagnostics), ())
        usage = {usage_store.key("Codex", login): usage_store.StoredUsage(
            read_outcome=agent_accounts.UsageReadOutcome.ERROR,
            attempt_outcome=agent_accounts.UsageReadOutcome.ERROR, attempted_at=at.isoformat())}
        with (patch.object(accounts, "account_statuses", return_value=[]),
              patch.object(accounts, "codex_statuses", return_value=[status]),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", home)),
              patch.object(accounts, "read_role_map", return_value=role_map.RoleMap({}, "codex1")),
              patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                  "codex1", home, login)),
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
              patch.object(accounts, "read_role_map", return_value=role_map.RoleMap({}, "codex1")),
              patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                  workers.label, workers.home, "fixture@notexample.com")),
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
        self.assertCountEqual(terminal[terminal.index("") + 1:], [*reading.upcoming_moves, "", *expected])
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
        self.assertFalse(any("account note" in line or "setup claude2" in line for line in reading.notes))

    def test_status_identity_and_note_repairs_never_print_the_login(self) -> None:
        for login in ("private-owner@provider.io", "private-account-id"):
            for fault in ("wrong login", "missing note", "unreadable note", "unnoted"):
                with self.subTest(identity=login.split("@")[-1], fault=fault):
                    directory = self.home / ".claude2"
                    note = self.home / "notes/claude2.md"
                    diagnostics = {
                        "wrong login": f"accounts: ~/.claude2 is logged in as {login}, not claude2",
                        "missing note": f"accounts: claude2 has no account note at {note}",
                        "unreadable note": f"accounts: {note} has no readable login in its frontmatter",
                        "unnoted": accounts.unnoted_home_line(directory, login),
                    }
                    status = accounts.AccountStatus("~/.claude2", directory, True, True, True,
                        "claude2", login, "", (diagnostics[fault],), (), accounts.McpServerDifferences())
                    reading = self.status_with([status], [], usage_store.StoredUsage())
                    self.assertTrue(any(line.startswith("claude2:") for line in reading.notes))
                    for line in reading.notes:
                        self.assertNotIn(login, line)
                    self.assertNotIn(login, accounts.render_status_json(reading))

    def test_duplicate_home_diagnostic_is_one_line_when_both_rows_have_one_account(self) -> None:
        statuses = self.codex_inventory(duplicate=True)
        self.assertEqual([status.label for status in statuses], ["codex1"])
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
            expected.extend((
                "codex1: login is stored in two homes; ~/.codex and ~/.codex1 store codex1's login; two homes on one login break token refresh; keep each login in one home",
                "codex1: login is stored in two homes; ~/.codex and ~/.codex2 store codex1's login; two homes on one login break token refresh; keep each login in one home",
                "codex1: login is stored in two homes; ~/.codex1 and ~/.codex2 store codex1's login; two homes on one login break token refresh; keep each login in one home",
            ))
            lines = list(reading.notes)
            observed.append(sorted(lines))
            self.assertCountEqual(lines, expected)
            self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].splitlines(),
                                  [*reading.upcoming_moves, "", *expected])
            self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)
        self.assertEqual(observed[0], observed[1])

    def test_startup_observation_does_not_own_another_homes_service_failure(self) -> None:
        statuses = self.codex_inventory(duplicate=True)
        failed = statuses[0]
        other = replace(failed, home=self.home / ".codex2", folder="~/.codex2")
        statuses = [failed, other]
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
                      patch.object(accounts, "read_role_map", return_value=role_map.RoleMap({}, "codex1")),
                      patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                          "codex1", failed.home, failed.login)),
                      patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
                      patch.object(accounts, "_codex_home_login", return_value=""),
                      patch.object(account, "claude_account", return_value=None)):
                    reading = accounts.read_status()
                at = reading.rows[0].read_at
                self.assertIsInstance(at, datetime)
                local = cast(datetime, at)
                previous = "this shows the reading from " + local.strftime(
                    "%H:%M" if local.date() == reading.checked_at.date() else "%m-%d %H:%M")
                expected = [
                    "units: not configured; the role map selects no account; run accounts.py move units <claudeN>",
                    "showrunners: not configured; the role map selects no account; run accounts.py move showrunners <claudeN>",
                    "* codex1: usage unavailable; Codex did not start at ~/.codex1; " + previous
                        + "; run accounts.py setup codex1 and try again"
                        + ("; the attempt time is not recorded" if outcome == agent_accounts.UsageReadOutcome.ERROR else
                           "; start a session on it to check it" if outcome == agent_accounts.UsageReadOutcome.REFUSED_401 else ""),
                    "codex1: login is stored in two homes; ~/.codex1 and ~/.codex2 store codex1's login; two homes on one login break token refresh; keep each login in one home",
                ]
                if outcome != agent_accounts.UsageReadOutcome.OK:
                    expected.append("* codex1: usage unavailable; " + (
                        "the usage read failed; " + previous + "; the attempt time is not recorded"
                        if outcome == agent_accounts.UsageReadOutcome.ERROR else
                        "the usage service did not accept this login; start a session on it to check it"))
                terminal = accounts.render_status_table(reading).splitlines()
                self.assertCountEqual(terminal[terminal.index("") + 1:], [*reading.upcoming_moves, "", *expected])
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
            "claude2", "owner@provider.io", "", (), ("mystery-cache",), accounts.McpServerDifferences((
                accounts.McpServerDifference("brp", "off", ("~",)),
                accounts.McpServerDifference("account-only", "on", ("~",)),
            )))
        reading = self.status_with([status], [], usage_store.StoredUsage(), detail=True)
        expected = (
            "claude2: unclassified entries; mystery-cache; check these entries in ~/.claude2; keep private files in this home",
            "claude2 turns brp off in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp",
            "claude2 turns account-only on in 1 project where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 account-only",
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
        line = "* claude2: usage unavailable; the usage read failed; no reading is stored yet; the attempt time is not recorded"
        self.assertIn(line, reading.notes)
        self.assertIn(line, accounts.render_status_table(reading).splitlines())
        self.assertIn(line.removeprefix("* "), cast(accounts.StatusDocument,
            json.loads(accounts.render_status_json(reading)))["problems"])

    def test_unreadable_note_keeps_independent_usage_failure_and_retry(self) -> None:
        directory = self.home / ".claude2"
        note = self.home / "notes/claude2.md"
        status = accounts.AccountStatus("~/.claude2", directory, True, True, True,
            "claude2", "owner@provider.io", "", (f"accounts: could not read {note}: denied",), (), accounts.McpServerDifferences())
        usage = usage_store.StoredUsage(read_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            attempt_outcome=agent_accounts.UsageReadOutcome.RATE_LIMITED_429,
            weekly=usage_store.StoredWindow(left=50, resets="2026-11-01T01:14:00+00:00", at="2026-10-29T01:14:00+00:00"), attempted_at="2026-10-29T01:20:00+00:00")
        lines = self.status_with([status], [], usage).notes
        self.assertTrue(any("cannot read" in line for line in lines))
        self.assertIn("* claude2: usage unavailable; the usage service is busy; this shows the reading from 10-29 01:14; last read attempted at 10-29 01:20", lines)

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
                                     for line in lines))
                expected = ["workers: not configured; the role map selects no account; run accounts.py set-role workers <codexN>",
                            "claude2: test sign-in is stored; a test wrote ~/.claude2/.claude.json at 2026-10-29 01:14 UTC; delete ~/.claude2/.claude.json"]
                self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(),
                                      [*reading.upcoming_moves, "", *expected])
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
            self.assertIn("reading from 10-29 01:14", failures[0])
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
            self.assertIn(f"* codex{number}: usage unavailable; the usage service is busy; this shows the reading from 10-29 01:14; last read attempted at 10-29 01:20", lines)
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
        self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(),
                              [*reading.upcoming_moves, "", *expected])
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
        self.assertIn("reading from 10-29 01:14", lines[0])
        self.assertIn("accounts.py setup codex1", lines[0])

    def test_invalid_existing_claude_identity_requires_login_repair(self) -> None:
        directory = self.home / ".claude2"
        directory.mkdir()
        identity = directory / ".claude.json"
        _ = identity.write_text('{"oauthAccount": {}}')
        status = accounts.AccountStatus("~/.claude2", directory, True, True, True,
            "claude2", "", "", (f"accounts: {identity} has an invalid oauthAccount identity",), (), accounts.McpServerDifferences(),
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
            "codex1: login is stored in two homes; ~/.codex and ~/.codex1 store codex1's login; two homes on one login break token refresh; keep each login in one home",
        ]
        self.assertCountEqual(accounts.render_status_table(reading).split("\n\n", 1)[1].strip().splitlines(),
                              [*reading.upcoming_moves, "", *expected])
        self.assertCountEqual(cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))["problems"], expected)

    def test_real_domains_keep_account_repairs_through_status(self) -> None:
        for domain in ("notexample.com", "latest.io", "proton.me"):
            with self.subTest(domain=domain):
                lines = self.reading("fixture@" + domain).notes
                self.assertFalse(any("test wrote" in line or "delete " in line for line in lines))
                self.assertTrue(any("add ~/notes/" in line for line in lines))

    def test_reserved_login_reports_the_test_file_and_write_time_without_account_steps(self) -> None:
        for domain in ("invalid", "test", "example", "localhost", "one.invalid", "one.test",
                       "one.example", "one.localhost", "example.com", "example.net", "example.org",
                       "sub.example.com", "EXAMPLE.ORG."):
            for noted in (False, True):
                with self.subTest(domain=domain, noted=noted):
                    reading = self.reading("account-fixture@" + domain, noted=noted)
                    lines = [line for line in reading.notes if "test wrote" in line]
                    self.assertEqual(len(lines), 1)
                    line = lines[0]
                    self.assertIn("auth.json", line)
                    self.assertIn("2026-10-29 01:14 UTC", line)
                    self.assertIn("delete ~/.codex/auth.json", line)
                    self.assertFalse(any("add ~/notes/" in note
                                         for note in reading.notes))
                    self.assertTrue(any("usage read failed" in note for note in reading.notes))
                    problems = cast(list[str], json.loads(accounts.render_status_json(reading))["problems"])
                    self.assertEqual(problems, [note.removeprefix("* ") for note in reading.notes])
                    self.assertIn(line, accounts.render_status_table(reading))

    def test_unnoted_legacy_login_keeps_read_failure_and_note_repair(self) -> None:
        reading = self.reading("fixture@notexample.com")
        lines = [line for line in reading.notes if "~/.codex" in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("* ~/.codex: usage unavailable; the usage read failed; no reading is stored yet; last read attempted at 10-29 01:14", lines)
        lines = [line for line in lines if "no account note" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("no account note", lines[0])
        self.assertIn("with the login for ~/.codex", lines[0])
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
            "claude2", "fixture@example.invalid", "", (), (), accounts.McpServerDifferences())
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
            ("accounts: ~/.claude2 is logged in as owner@example.invalid, not claude2", "wrong login", "holds a different login from claude2", "/login"),
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
            accounts.ProblemStep("last read attempted at 01:14"), footnote=True)
        self.assertEqual(accounts.render_status_problem(problem),
            "* codex1: usage unavailable; the service is busy; last read attempted at 01:14")
        self.assertEqual(accounts.render_status_problem(accounts.StatusProblem("codex1", "check failed",
            accounts.NoSeparateCause(), accounts.NoUserStep())), "codex1: check failed")


if __name__ == "__main__":
    _ = unittest.main()
