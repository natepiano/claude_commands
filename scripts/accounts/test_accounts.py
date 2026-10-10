"""Behavioral contract for isolated Claude account directories."""

from __future__ import annotations

import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import cast, final, override
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

    def login_directory(self, label: str, login: str, *, default: bool = False) -> None:
        config = self.home / ".claude.json" if default else accounts.account_dir(label) / ".claude.json"
        if not default:
            self.assertEqual(accounts.setup(label), [])
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")
        notes = Path(os.environ["AGENT_NOTES_DIR"])
        _ = (notes / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n", encoding="utf-8")

    def write_readings(self, readings: list[accounts.QuotaReading]) -> None:
        log = accounts.readings_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ = log.write_text("".join(json.dumps(reading) + "\n" for reading in readings), encoding="utf-8")

    def write_role_history(self, changes: list[accounts.RoleChange]) -> None:
        log = accounts.roles_history_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ = log.write_text("".join(json.dumps(change) + "\n" for change in changes), encoding="utf-8")

    def select_named_unit(self, label: str) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps(accounts.AccountsConfiguration(unit=label)), encoding="utf-8")

    def test_single_default_account_serves_both_roles_without_multi_account_displays(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.assertFalse(accounts.multiple_accounts_configured())
        roles = accounts.claude_account_selection()
        self.assertFalse(roles.multiple_accounts)
        self.assertEqual(roles.roles_for_login("MAIN@example.invalid"), ("showrunners", "unit directors"))
        self.assertEqual(roles.roles_for_login("unknown@example.invalid"), ())
        self.assertEqual(roles.roles_for_login(""), ())

    def test_account_selection_uses_the_whoami_identity_types(self) -> None:
        """F007: factory values must pass the top-level whoami type checks."""
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
        """F002: comparisons within one run cannot activate both old and new units."""
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
        """F003: a directory label is not the quota account's note label."""
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "MAIN@example.invalid")
        self.select_named_unit("claude 3")
        self.write_role_history([
            accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
        ])
        self.write_readings([
            accounts.QuotaReading(account="claude 2", at=f"2026-10-{day:02d}T00:00:00Z", remaining=left)
            for day, left in ((9, 90.0), (10, 80.0))
        ])
        results = accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(results, [accounts.AccountRoleBurn("claude 2", ("showrunners", "unit directors"), 10, 1)])
        accounts.roles_history_file().unlink()
        self.assertEqual(accounts.burn(datetime(2026, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, tzinfo=timezone.utc)), results)

    def test_burn_refuses_to_guess_an_unreadable_historical_account_identity(self) -> None:
        """F003: missing identity data must not be replaced by a directory label."""
        self.write_role_history([
            accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
        ])
        result, stdout, stderr = self.run_cli("burn", "--since", "2026-10-09T00:00:00Z", "--until", "2026-10-10T00:00:00Z")
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("account identity for claude 3 is unreadable", stderr)

    def test_burn_reports_each_account_with_its_historical_role(self) -> None:
        self.login_directory("claude 2", "main@example.invalid", default=True)
        self.login_directory("claude 3", "unit@example.invalid")
        self.select_named_unit("claude 3")
        self.write_role_history([accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3")])
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
            accounts.RoleChange(at="2026-10-08T00:00:00Z", role="unit", label="claude 3"),
            accounts.RoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude 1"),
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
            accounts.RoleChange(at="2026-10-10T00:00:00Z", role="unit", label="claude 3"),
            accounts.RoleChange(at="2026-10-11T00:00:00Z", role="unit", label="default"),
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
        self.write_role_history([accounts.RoleChange(at="bad", role="unit", label="claude 3")])
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
        """F003: rendering a selected account never resolves the role again."""
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
        """F003: a default selection renders without another configuration lookup."""
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
        self.assertIsInstance(audit["at"], str)
        self.assertTrue(cast(str, audit["at"]))

    def test_set_role_default_removes_assignment_and_records_default(self) -> None:
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

    def test_unknown_role_is_refused_by_every_role_command(self) -> None:
        for arguments in (
            ("role", "showrunner"),
            ("env", "showrunner"),
            ("set-role", "showrunner", "default"),
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
