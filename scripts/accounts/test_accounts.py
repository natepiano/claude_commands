"""Behavioral contract for isolated Claude account directories."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from collections.abc import Callable
from typing import Self, cast, final, override
from unittest.mock import call, patch

from scripts.accounts import accounts
import account
import agent_accounts


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


@final
class RoleWriteTime(datetime):
    """The fixed assignment time before this fixture's quota observations."""

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 8, tzinfo=timezone.utc).astimezone(tz)


@final
class AccountsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.main_dir = Path()

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
        }
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del environment_context

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
            if entry not in {"projects", "sessions"}:
                _ = source.mkdir()
            _ = (account_dir / entry).symlink_to(source)

    def logged_in(self) -> account.Account:
        return account.Account("claude", "owner@example.invalid", "claude 2")

    def login_directory(self, label: str, login: str, *, default: bool = False, note_label: str = "") -> None:
        config = self.home / ".claude.json" if default else accounts.account_dir(label) / ".claude.json"
        if not default:
            self.assertEqual(accounts.setup(label), [])
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
        notes = Path(os.environ["AGENT_NOTES_DIR"])
        _ = (notes / f"{note_label or label}.md").write_text(f"---\nlogin: {login}\n---\n", encoding="utf-8")

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
            self.assertEqual(argv[2:], ("down", argv[3], "--restart"))
            current = accounts.role_account("unit")
            if label == "default":
                self.assertIsInstance(current, accounts.DefaultAccount)
            else:
                self.assertEqual(current, accounts.NamedAccount(label, accounts.account_dir(label)))
            return subprocess.CompletedProcess(argv, down_code, down_stdout, down_stderr)

        with patch.object(subprocess, "run", side_effect=shutdown):
            result = self.run_cli("swap", "unit", label, *(["--dry-run"] if dry_run else []))
        return result, commands

    def test_swap_moves_local_units_and_owned_seats_with_an_empty_peer_inventory(self) -> None:
        local = self.prepare_swap()
        local["sessions"].insert(0, accounts.SwapSeatSession(
            kind="seat", session_id="seat-id", name="seat-one",
            owner=accounts.SwapDirectorOwner(kind="director", session_id="unit-id"),
        ))
        remote = accounts.SwapInventory(
            machine="macbook", login=local["login"], label=local["label"],
            sessions=[], unattributed=[],
        )

        result, commands = self.swap_cli([local, remote])

        self.assertEqual(result, (0, "shutdown started\nunits come back on claude 1 when claude 3 is down\n", ""))
        self.assertEqual([argv[2:] for argv in commands], [
            ("status", "claude 3", "--json"), ("down", "claude 3", "--restart"),
        ])
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude 1", self.home / ".claude1"))
        audit = cast(dict[str, object], json.loads(accounts.roles_history_file().read_text()))
        self.assertEqual((audit["label"], audit["account_label"]), ("claude 1", "claude 1"))

    def test_swap_refuses_a_peer_unit_before_writing_the_role_or_starting_shutdown(self) -> None:
        local = self.prepare_swap()
        remote = accounts.SwapInventory(
            machine="macbook", login=local["login"], label=local["label"],
            sessions=[accounts.SwapUnitSession(kind="unit", session_id="remote-id", name="unit-two")],
            unattributed=[],
        )
        before = self.role_files()

        result, commands = self.swap_cli([local, remote])

        self.assertEqual(result, (1, "", "accounts: cannot swap claude 3: macbook unit unit-two would not move\n"))
        self.assertEqual([argv[2:] for argv in commands], [("status", "claude 3", "--json")])
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_a_peer_owned_seat_before_writing_the_role_or_starting_shutdown(self) -> None:
        local = self.prepare_swap()
        remote = accounts.SwapInventory(
            machine="macbook", login=local["login"], label=local["label"],
            sessions=[
                accounts.SwapSeatSession(kind="seat", session_id="remote-seat", name="seat-two",
                                         owner=accounts.SwapDirectorOwner(kind="director", session_id="remote-id")),
                accounts.SwapUnitSession(kind="unit", session_id="remote-id", name="unit-two"),
            ], unattributed=[],
        )
        before = self.role_files()

        result, commands = self.swap_cli([local, remote])

        self.assertEqual(result, (1, "", "accounts: cannot swap claude 3: macbook seat seat-two would not move\n"))
        self.assertEqual([argv[2:] for argv in commands], [("status", "claude 3", "--json")])
        self.assertEqual(self.role_files(), before)

    def test_swap_inspects_and_stops_the_old_note_identity_instead_of_its_directory_label(self) -> None:
        found = self.prepare_swap(shared_default_login=True)

        result, commands = self.swap_cli([found])

        self.assertEqual(result[0], 0, result)
        self.assertEqual([argv[2:] for argv in commands], [
            ("status", "claude 2", "--json"), ("down", "claude 2", "--restart"),
        ])
        self.assertIn("when claude 2 is down", result[1])

    def test_swap_accepts_an_unnamed_unit_and_identifies_an_unnamed_blocker_by_session_id(self) -> None:
        found = self.prepare_swap()
        found["sessions"][0]["name"] = ""
        result, _ = self.swap_cli([found], dry_run=True)
        self.assertEqual(result[0], 0, result)
        found["sessions"].append(accounts.SwapOtherSession(kind="top-level", session_id="unnamed-terminal", name=""))
        before = self.role_files()
        result, _ = self.swap_cli([found])
        self.assertEqual(result[0], 1)
        self.assertIn("top-level unnamed-terminal", result[2])
        self.assertEqual(self.role_files(), before)

    def test_swap_default_returns_units_to_the_default_login(self) -> None:
        found = self.prepare_swap()

        result, commands = self.swap_cli([found], "default")

        self.assertEqual(result, (0, "shutdown started\nunits come back on claude 2 when claude 3 is down\n", ""))
        self.assertEqual(commands[-1][2:], ("down", "claude 3", "--restart"))
        self.assertIsInstance(accounts.role_account("unit"), accounts.DefaultAccount)
        audit = cast(dict[str, object], json.loads(accounts.roles_history_file().read_text()))
        self.assertEqual((audit["label"], audit["account_label"]), ("default", "claude 2"))

    def test_swap_dry_run_prints_both_commands_without_writing_or_stopping(self) -> None:
        found = self.prepare_swap()
        before = self.role_files()

        result, commands = self.swap_cli([found], dry_run=True)

        self.assertEqual((result[0], result[2]), (0, ""))
        role_argv, down_argv = [shlex.split(line) for line in result[1].splitlines()]
        self.assertEqual(role_argv[2:], ["set-role", "unit", "claude 1"])
        self.assertEqual(down_argv[2:], ["down", "claude 3", "--restart"])
        self.assertEqual([argv[2] for argv in commands], ["status"])
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_an_unreadable_old_identity_before_any_subprocess_or_write(self) -> None:
        _ = self.prepare_swap()
        (accounts.account_dir("claude 3") / ".claude.json").unlink()
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 1")
        self.assertEqual(result, (1, "", "accounts: current unit account identity is unreadable\n"))
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_an_unfit_target_before_any_subprocess_or_write(self) -> None:
        _ = self.prepare_swap()
        (accounts.account_dir("claude 1") / "projects").unlink()
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 1")
        self.assertEqual(result[0:2], (1, ""))
        self.assertIn("projects", result[2])
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_a_missing_target_before_any_subprocess_or_write(self) -> None:
        _ = self.prepare_swap()
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 9")
        self.assertEqual(result[0:2], (1, ""))
        self.assertIn(".claude9 is missing", result[2])
        self.assertEqual(self.role_files(), before)

    def test_swap_default_note_label_requires_default_even_without_a_named_directory(self) -> None:
        _ = self.prepare_swap()
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 2")
        self.assertEqual(result, (1, "", "accounts: claude 2 is the default account; use default\n"))
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_the_default_login_named_by_a_directory_with_an_undo_hint(self) -> None:
        _ = self.prepare_swap()
        self.login_directory("claude 4", "MAIN@example.invalid", note_label="claude 2")
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 4")
        self.assertEqual(result, (1, "", "accounts: claude 4 is the default account; use default\n"))
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_an_alias_of_the_current_unit_login(self) -> None:
        _ = self.prepare_swap()
        self.login_directory("claude 4", "OLD@example.invalid", note_label="claude 3")
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "claude 4")
        self.assertEqual(result, (1, "", "accounts: claude 3 is already the unit account\n"))
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_an_unreadable_default_undo_target(self) -> None:
        _ = self.prepare_swap()
        (self.home / ".claude.json").unlink()
        before = self.role_files()
        with patch.object(subprocess, "run", side_effect=AssertionError("shutdown called")):
            result = self.run_cli("swap", "unit", "default")
        self.assertEqual(result, (1, "", "accounts: default account identity is unreadable\n"))
        self.assertEqual(self.role_files(), before)

    def test_swap_names_every_kind_of_session_that_would_not_move(self) -> None:
        found = self.prepare_swap()
        before = self.role_files()
        blockers: list[accounts.SwapSession] = [
            accounts.SwapOtherSession(kind="showrunner", session_id="showrunner", name="showrunner-one"),
            accounts.SwapOtherSession(kind="top-level", session_id="terminal", name="terminal-one"),
            accounts.SwapSeatSession(kind="seat", session_id="orphan", name="orphan-seat", owner=accounts.SwapNoDirector(kind="no director")),
            accounts.SwapSeatSession(kind="seat", session_id="other-seat", name="other-seat", owner=accounts.SwapDirectorOwner(kind="director", session_id="other-director")),
        ]
        for blocked in blockers:
            with self.subTest(blocked=blocked):
                found["sessions"] = [accounts.SwapUnitSession(kind="unit", session_id="unit-id", name="unit-one"), blocked]
                result, commands = self.swap_cli([found])
                self.assertEqual(result[0:2], (1, ""))
                self.assertIn(f"{blocked['kind']} {blocked['name']}", result[2])
                self.assertEqual([argv[2] for argv in commands], ["status"])
                self.assertEqual(self.role_files(), before)

    def test_swap_refuses_and_names_unattributed_sessions(self) -> None:
        found = self.prepare_swap()
        found["unattributed"] = [accounts.SwapUnattributedSession(name="unreadable-session", pid=123, reason="account unreadable")]
        before = self.role_files()
        result, commands = self.swap_cli([found])
        self.assertEqual(result[0:2], (1, ""))
        self.assertIn("unattributed session unreadable-session (pid 123)", result[2])
        self.assertEqual(len(commands), 1)
        self.assertEqual(self.role_files(), before)

    def test_swap_refuses_missing_or_unavailable_machine_inventory(self) -> None:
        found = self.prepare_swap()
        before = self.role_files()
        invalid: list[object] = [
            [], found, [found, {"machine": "macbook", "unreachable": True}],
            [found, {"machine": "macbook", "status": "unavailable", "rc": 1}],
            [{**found, "login": "another@example.invalid"}],
            [{**found, "sessions": [{"kind": "unknown", "session_id": "x", "name": "x"}]}],
            [{**found, "sessions": "not a list"}],
            [{"machine": "natedev", "login": found["login"], "label": found["label"], "sessions": []}],
        ]
        for wire in invalid:
            with self.subTest(wire=wire):
                result, commands = self.swap_cli(wire)
                self.assertEqual(result[0:2], (1, ""))
                self.assertEqual(len(commands), 1)
                self.assertEqual(self.role_files(), before)

    def test_swap_refuses_failed_or_invalid_status_output_without_writing(self) -> None:
        _ = self.prepare_swap()
        before = self.role_files()
        for code, output, error in ((2, "", "status refused\n"), (0, "not JSON", "")):
            with self.subTest(code=code):
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, error)) as shutdown:
                    result = self.run_cli("swap", "unit", "claude 1")
                self.assertEqual(result[0:2], (1, ""))
                shutdown.assert_called_once()
                self.assertEqual(self.role_files(), before)

    def test_refused_down_restores_the_previous_role_and_can_be_retried_immediately(self) -> None:
        found = self.prepare_swap()
        before = accounts.accounts_config_file().read_text()

        result, _ = self.swap_cli([found], down_code=2, down_stdout="", down_stderr="shutdown: nothing was shut down\n")

        self.assertEqual(result, (2, "unit account unchanged: still claude 3\n", "shutdown: nothing was shut down\n"))
        self.assertEqual(accounts.accounts_config_file().read_text(), before)
        history = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([(line["label"], line["account_label"]) for line in history], [("claude 1", "claude 1"), ("claude 3", "claude 3")])
        retried, _ = self.swap_cli([found])
        self.assertEqual(retried[0], 0, retried)

    def test_refused_down_restores_an_absent_default_role_configuration(self) -> None:
        found = self.prepare_swap(shared_default_login=True)
        accounts.accounts_config_file().unlink()

        result, _ = self.swap_cli([found], down_code=1, down_stdout="nothing was shut down\n")

        self.assertEqual(result[0], 1)
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertIsInstance(accounts.role_account("unit"), accounts.DefaultAccount)
        self.assertIn("unit account unchanged: still claude 2", result[1])

    def test_failed_down_spawn_restores_the_old_role(self) -> None:
        found = self.prepare_swap()
        before = accounts.accounts_config_file().read_text()
        with patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps([found]), ""), OSError("missing executable"),
        ]):
            result = self.run_cli("swap", "unit", "claude 1")
        self.assertEqual(result[0], 1)
        self.assertIn("missing executable", result[2])
        self.assertEqual(accounts.accounts_config_file().read_text(), before)

    def test_accepted_shutdown_retains_the_new_role_and_partial_stop_retry_line(self) -> None:
        found = self.prepare_swap()
        notice = "shutdown: stop partial\nshutdown.py down claude 3 --restart\n"
        result, _ = self.swap_cli([found], down_stdout=notice)
        self.assertEqual(result[0], 0, result)
        self.assertTrue(result[1].startswith(notice))
        self.assertEqual(accounts.role_account("unit"), accounts.NamedAccount("claude 1", self.home / ".claude1"))

    def test_swap_uses_one_selection_through_role_write_and_shutdown(self) -> None:
        found = self.prepare_swap()

        def shutdown(argv: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            if argv[2] == "status":
                _ = self.enterContext(patch.object(accounts, "account_identity", side_effect=AssertionError("identity resolved again")))
                return subprocess.CompletedProcess(argv, 0, json.dumps([found]), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (patch.object(accounts, "claude_account_selection", wraps=accounts.claude_account_selection) as selection,
              patch.object(subprocess, "run", side_effect=shutdown)):
            result = self.run_cli("swap", "unit", "claude 1")
        self.assertEqual(result[0], 0, result)
        selection.assert_called_once_with()

    def test_burn_keeps_the_swapped_account_identity_after_logout_or_another_login(self) -> None:
        found = self.prepare_swap()
        notes = Path(os.environ["AGENT_NOTES_DIR"])
        _ = (notes / "claude 1.md").rename(notes / "claude 4.md")
        _ = (notes / "claude 5.md").write_text("---\nlogin: another@example.invalid\n---\n")
        with patch.object(accounts, "datetime", RoleWriteTime):
            result, _ = self.swap_cli([found])
        self.assertEqual(result[0], 0, result)
        audit = cast(dict[str, object], json.loads(accounts.roles_history_file().read_text()))
        self.assertEqual((audit["label"], audit["account_label"]), ("claude 1", "claude 4"))
        self.write_readings([
            accounts.QuotaReading(account="claude 4", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))
        ])
        config = accounts.account_dir("claude 1") / ".claude.json"
        for content in ({}, {"oauthAccount": {"emailAddress": "another@example.invalid"}}):
            with self.subTest(content=content):
                _ = config.write_text(json.dumps(content))
                results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
                self.assertEqual(results, [accounts.AccountRoleBurn("claude 4", ("unit directors",), 10, 1)])

    def test_set_role_refuses_unreadable_named_and_default_identities_without_writing(self) -> None:
        for label in ("claude 1", "default"):
            with self.subTest(label=label):
                result = self.run_cli("set-role", "unit", label)
                self.assertEqual(result, (1, "", f"accounts: account identity for {label} is unreadable\n"))
                self.assertEqual(self.role_files(), {})

    def test_new_default_role_history_keeps_its_identity_after_default_logout(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        with patch.object(accounts, "datetime", RoleWriteTime):
            accounts.set_role("unit", "default")
        (self.home / ".claude.json").unlink()
        self.write_readings([
            accounts.QuotaReading(account="claude 2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))
        ])
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)),
                         [accounts.AccountRoleBurn("claude 2", ("unit directors",), 10, 1)])

    def test_legacy_default_role_history_refuses_an_unreadable_identity(self) -> None:
        self.write_role_history([accounts.LegacyRoleChange(at="2026-10-08T00:00:00Z", role="unit", label="default")])
        with self.assertRaisesRegex(accounts.AccountsError, "account identity for default is unreadable"):
            _ = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))

    def test_single_default_account_serves_both_roles_without_multi_account_displays(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.assertFalse(accounts.multiple_accounts_configured())
        roles = accounts.claude_account_selection()
        self.assertFalse(roles.multiple_accounts)
        self.assertEqual(roles.roles_for_login("MAIN@example.invalid"), ("showrunners", "unit directors"))
        self.assertEqual(roles.roles_for_login("unknown@example.invalid"), ())
        self.assertEqual(roles.roles_for_login(""), ())

    def test_account_selection_uses_the_whoami_identity_types(self) -> None:
        """Factory values must pass the top-level whoami type checks."""
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")

        selection = accounts.claude_account_selection()

        self.assertIsInstance(selection, agent_accounts.ClaudeAccountSelection)
        self.assertIsInstance(selection.default_account, agent_accounts.KnownClaudeAccount)
        self.assertIsInstance(selection.unit_account, agent_accounts.KnownClaudeAccount)
        self.assertIsInstance(selection.named_accounts[0], agent_accounts.CheckedNamedClaudeAccount)
        self.assertIsInstance(selection.named_accounts[0].identity, agent_accounts.KnownClaudeAccount)
        self.assertTrue(agent_accounts.ClaudeAccountSelection.is_default_login(
            selection, "MAIN@example.invalid",
        ))
        self.assertEqual(agent_accounts.ClaudeAccountSelection.roles_for_login(
            selection, "unit@example.invalid",
        ), ("unit directors",))
        self.assertIsInstance(
            accounts.account_identity(self.home / "unreadable-account"),
            agent_accounts.UnreadableClaudeAccount,
        )

    def test_current_roles_distinguish_default_unit_and_spare_logins(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.login_directory("claude 1", "spare@example.invalid")
        self.select_named_unit("claude 3")
        self.assertTrue(accounts.multiple_accounts_configured())
        roles = accounts.claude_account_selection()
        self.assertEqual(roles.roles_for_login("main@example.invalid"), ("showrunners",))
        self.assertEqual(roles.roles_for_login("UNIT@example.invalid"), ("unit directors",))
        self.assertEqual(roles.roles_for_login("spare@example.invalid"), ())
        self.assertEqual([named.label for named in roles.named_accounts], ["claude 1", "claude 3"])

    def test_same_login_in_default_and_unit_directory_serves_both_roles(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "main@example.invalid")
        self.select_named_unit("claude 3")
        self.assertEqual(accounts.claude_account_selection().roles_for_login("main@example.invalid"), ("showrunners", "unit directors"))

    def test_unfit_named_accounts_are_excluded_from_reads_and_roles(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        (accounts.account_dir("claude 3") / "projects").unlink()
        self.assertEqual(accounts.checked_named_accounts(), [])
        self.assertEqual(accounts.claude_account_selection().roles_for_login("unit@example.invalid"), ())

    def test_selection_keeps_roles_fixed_when_the_unit_changes(self) -> None:
        """Comparisons within one run cannot activate both old and new units."""
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "old-unit@example.invalid")
        self.login_directory("claude 1", "new-unit@example.invalid")
        self.select_named_unit("claude 3")
        with patch.object(accounts, "role_account", wraps=accounts.role_account) as selected:
            roles = accounts.claude_account_selection()
            self.select_named_unit("claude 1")
            with patch.object(accounts, "account_identity", side_effect=AssertionError("comparison reads disk")):
                self.assertEqual(roles.roles_for_login("MAIN@example.invalid"), ("showrunners",))
                self.assertEqual(roles.roles_for_login("old-unit@example.invalid"), ("unit directors",))
                self.assertEqual(roles.roles_for_login("new-unit@example.invalid"), ())
                self.assertTrue(roles.is_default_login("MAIN@example.invalid"))
            selected.assert_called_once_with("unit")
        self.assertEqual(accounts.claude_account_selection().roles_for_login("new-unit@example.invalid"), ("unit directors",))

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
            "claude 1 (spare): 4 points/day (8 points over 2 observed days)",
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
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(account_dir))} claude\n/login\n",
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
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(account_dir))} claude\n/login\n",
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
        with patch.object(account, "claude_account", return_value=self.logged_in()):
            self.assertEqual(self.run_cli("check", "claude 2"), (0, "", ""))

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

        with patch.object(account, "claude_account", return_value=self.logged_in()):
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
        self.assertIn(str(self.main_dir), stdout)
        self.assertIn("claude 2", stdout)
        self.assertIn("owner@example.invalid", stdout)
        self.assertIn("unit", stdout)
        self.assertNotIn("unclassified:", stdout)
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
        self.assertIn("mcpServers: unavailable", stdout)
        self.assertEqual(
            login_read.call_args_list,
            [call(self.main_dir), call(self.main_dir)],
        )

    def test_status_shows_role_problems_unclassified_entries_and_mcp_difference(
        self,
    ) -> None:
        self.write_main_config()
        account_dir = self.home / ".claude2"
        self.correct_links(account_dir)
        _ = (account_dir / "mystery-cache").mkdir()
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
        self.assertIn("unit", stdout)
        self.assertIn("not logged in", stdout.lower())
        self.assertIn("unclassified: mystery-cache", stdout)
        self.assertIn("brp", stdout)
        self.assertIn("account-only", stdout)

    def test_absent_role_configuration_uses_default_account(self) -> None:
        resolved = accounts.role_account("unit")

        self.assertIsInstance(resolved, accounts.DefaultAccount)
        self.assertEqual(accounts.launch_prefix("unit"), "")
        self.assertEqual(self.run_cli("role", "unit"), (0, "default\n", ""))
        self.assertEqual(self.run_cli("env", "unit"), (0, "\n", ""))

    def test_account_prefix_keeps_the_selection_after_the_role_changes(self) -> None:
        """Rendering a selected account never resolves the role again."""
        self.write_main_config()
        self.login_directory("claude 3", "old@example.invalid")
        self.login_directory("claude 1", "new@example.invalid")
        accounts.set_role("unit", "claude 3")
        selected = accounts.role_account("unit")
        accounts.set_role("unit", "claude 1")

        self.assertEqual(
            accounts.account_prefix(selected),
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude3'))} ",
        )
        self.assertEqual(
            accounts.launch_prefix("unit"),
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude1'))} ",
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

    def test_named_role_resolves_label_directory_and_quoted_launch_prefix(self) -> None:
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        _ = (config_dir / "accounts.json").write_text(
            json.dumps({"unit": "claude 3"}), encoding="utf-8"
        )

        resolved = accounts.role_account("unit")

        self.assertIsInstance(resolved, accounts.NamedAccount)
        assert isinstance(resolved, accounts.NamedAccount)
        self.assertEqual(resolved.label, "claude 3")
        self.assertEqual(resolved.dir, self.home / ".claude3")
        expected = (
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude3'))} "
        )
        self.assertEqual(accounts.launch_prefix("unit"), expected)
        self.assertEqual(self.run_cli("role", "unit"), (0, "claude 3\n", ""))
        self.assertEqual(self.run_cli("env", "unit"), (0, expected + "\n", ""))

    def test_set_role_writes_atomically_and_appends_audit_line(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "selected@example.invalid")
        original_replace = os.replace
        with patch.object(os, "replace", wraps=original_replace) as replace:
            result, stdout, stderr = self.run_cli("set-role", "unit", "claude 2")

        self.assertEqual((result, stdout, stderr), (0, "", ""))
        config_path = self.main_dir / "config" / "accounts.json"
        self.assertEqual(json.loads(config_path.read_text()), {"unit": "claude 2"})
        replace.assert_called_once()
        replace_call = cast(tuple[object, ...], replace.call_args.args)
        self.assertEqual(Path(cast(str | os.PathLike[str], replace_call[1])), config_path)
        self.assertFalse(Path(cast(str | os.PathLike[str], replace_call[0])).exists())

        audit_path = self.home / ".local" / "state" / "accounts" / "roles.jsonl"
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        audit = cast(dict[str, object], json.loads(lines[0]))
        self.assertEqual(audit["role"], "unit")
        self.assertEqual(audit["label"], "claude 2")
        self.assertEqual(audit["account_label"], "claude 2")
        self.assertIsInstance(audit["at"], str)
        self.assertTrue(cast(str, audit["at"]))

    def test_set_role_default_removes_assignment_and_records_default(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        config_path = config_dir / "accounts.json"
        _ = config_path.write_text('{"unit": "claude 2"}', encoding="utf-8")

        result, stdout, stderr = self.run_cli("set-role", "unit", "default")

        self.assertEqual((result, stdout, stderr), (0, "", ""))
        self.assertEqual(json.loads(config_path.read_text()), {})
        audit_path = self.home / ".local" / "state" / "accounts" / "roles.jsonl"
        audit = cast(dict[str, object], json.loads(audit_path.read_text()))
        self.assertEqual(audit["label"], "default")
        self.assertEqual(audit["account_label"], "claude 2")

    def test_unknown_role_is_refused_by_every_role_command(self) -> None:
        for arguments in (
            ("role", "showrunner"),
            ("env", "showrunner"),
            ("set-role", "showrunner", "default"),
            ("swap", "showrunner", "default"),
        ):
            with self.subTest(arguments=arguments):
                result, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    "accounts: showrunner is not a role; only unit is supported\n",
                )

    def test_set_role_refuses_an_unknown_label_without_writing(self) -> None:
        result, stdout, stderr = self.run_cli("set-role", "unit", "claude2")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "accounts: claude2 is not a claude N label\n")
        self.assertFalse((self.main_dir / "config" / "accounts.json").exists())
        self.assertFalse((self.home / ".local" / "state" / "accounts").exists())

    def test_role_refuses_an_unknown_label_stored_in_configuration(self) -> None:
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        _ = (config_dir / "accounts.json").write_text(
            '{"unit": "claude2"}', encoding="utf-8"
        )

        result, stdout, stderr = self.run_cli("role", "unit")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "accounts: claude2 is not a claude N label\n")

    def test_role_refuses_a_null_unit_in_configuration(self) -> None:
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        config_path = config_dir / "accounts.json"
        _ = config_path.write_text('{"unit": null}', encoding="utf-8")

        result, stdout, stderr = self.run_cli("role", "unit")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            f"accounts: {config_path} has an invalid unit account\n",
        )

    def test_set_role_restores_existing_configuration_when_history_fails(
        self,
    ) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "selected@example.invalid")
        config_dir = self.main_dir / "config"
        _ = config_dir.mkdir()
        config_path = config_dir / "accounts.json"
        original = '{"unit": "claude 1"}\n'
        _ = config_path.write_text(original, encoding="utf-8")

        with patch.object(
            accounts, "_append_role_change", side_effect=OSError("disk full")
        ):
            result, stdout, stderr = self.run_cli("set-role", "unit", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("could not record role change: disk full", stderr)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_set_role_removes_new_configuration_when_history_fails(self) -> None:
        self.write_main_config()
        self.login_directory("claude 2", "selected@example.invalid")
        config_path = self.main_dir / "config" / "accounts.json"

        with patch.object(
            accounts, "_append_role_change", side_effect=OSError("disk full")
        ):
            result, stdout, stderr = self.run_cli("set-role", "unit", "claude 2")

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("could not record role change: disk full", stderr)
        self.assertFalse(config_path.exists())


if __name__ == "__main__":
    _ = unittest.main()
