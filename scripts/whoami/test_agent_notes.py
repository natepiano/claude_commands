"""Account attribution and stale-data behavior of the vault writer."""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Self, cast, override
from unittest.mock import patch

import agent_notes
import note_file
import agent_accounts
import codex_pacer
import five_hour
import quota_alert
import usage_store
import whoami
from agent_accounts import (ClaudeDirectorySource, CodexHomeSource, CodexRateLimits, Quota, Report,
                            codex_credits, reset_credits)
from agent_notes import apply, local_reset
from note_file import Note, read_note
from scripts.accounts import accounts


class AgentNotesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.future: datetime = self.now + timedelta(days=2)

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home_context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del home_context
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _ = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        _ = os.environ.pop("CODEX_HOME", None)
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.root))
        _ = self.enterContext(patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl", create=True))

    def note(self, name: str, email: str, extra: str = "") -> Note:
        path = self.root / name
        _ = path.write_text(f'---\nlogin: {email}\nstate: inactive\n{extra}---\nBody stays.\n')
        path.chmod(0o644)
        note = read_note(path)
        if note is None:
            self.fail(f"{name} did not parse as a note")
        return note

    def account_dir(self, label: str, login: str, token: str) -> Path:
        if label == "default":
            directory = self.root / ".claude"
            directory.mkdir(exist_ok=True)
            config = self.root / ".claude.json"
        else:
            self.assertEqual(accounts.setup(label), [])
            directory = accounts.account_dir(label)
            config = directory / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        _ = (directory / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": token},
        }))
        return directory

    def select_unit(self, label: str) -> None:
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps(accounts.AccountsConfiguration(unit=label)))

    def select_seats(self, label: str) -> None:
        self.assertEqual(accounts.main(["set-role", "seats", label]), 0)

    def codex_login(self, home: Path, login: str) -> None:
        home.mkdir(exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({
            "tokens": {"id_token": f"header.{claims}.signature"},
        }))

    def test_update_writes_each_codex_home_and_keeps_only_the_seats_note_active(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex2"
        self.codex_login(default, "seats@example.com")
        self.codex_login(spare, "spare@example.com")
        _ = self.note("codex 1.md", "SEATS@example.com")
        _ = self.note("codex 2.md", "spare@example.com")
        _ = self.note("codex 3.md", "missing@example.com")
        seats_report = Report("Codex", email="seats@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 20, self.future)], credit_balance="7", limit_reset_count=1,
            limit_reset_expirations=[self.future])
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(spare),
            quotas=[Quota("Weekly", 95, self.future)], credit_balance="23", limit_reset_count=2,
            limit_reset_expirations=[self.future])

        for reports in ([seats_report, spare_report], [spare_report, seats_report]):
            with (
                self.subTest(order=[report.email for report in reports]),
                patch.object(agent_notes, "live_reports", return_value=reports),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            notes = {note.path.stem: note for note in note_file.read_notes()}
            self.assertEqual({label: note.get("state") for label, note in notes.items()}, {
                "codex 1": "active", "codex 2": "inactive", "codex 3": "inactive",
            })
            self.assertEqual({label: note.get("weekly_remaining_usage") for label, note in notes.items()}, {
                "codex 1": "80", "codex 2": "5", "codex 3": "null",
            })
            self.assertEqual(notes["codex 1"].get("credit_balance"), "7")
            self.assertEqual(notes["codex 2"].get("credit_balance"), "23")
            self.assertEqual(notes["codex 1"].get("limit_reset_count"), "1")
            self.assertEqual(notes["codex 2"].get("limit_reset_count"), "2")
            self.assertNotIn(notes["codex 1"].get("weekly_usage_checked_at"), (None, "null"))
            self.assertNotIn(notes["codex 2"].get("weekly_usage_checked_at"), (None, "null"))
            watch.assert_called_once_with([seats_report])
        stored = usage_store.load()
        seats_usage = stored[usage_store.key("Codex", "seats@example.com")]
        spare_usage = stored[usage_store.key("Codex", "spare@example.com")]
        if "weekly" not in seats_usage or "weekly" not in spare_usage:
            self.fail("Both signed-in homes must save their weekly readings")
        self.assertEqual(seats_usage["weekly"]["left"], 80)
        self.assertEqual(spare_usage["weekly"]["left"], 5)
        readings = [cast(agent_notes.StoredReading, json.loads(line))
                    for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertEqual({reading["account"] for reading in readings}, {"codex 1", "codex 2"})

    def test_update_follows_the_codex_home_in_use_for_note_activity(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex2"
        self.codex_login(default, "default@example.com")
        self.codex_login(selected, "selected@example.com")
        _ = self.note("codex 1.md", "default@example.com")
        _ = self.note("codex 2.md", "selected@example.com")
        default_report = Report("Codex", email="default@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 10, self.future)])
        self.select_seats("codex 2")
        selected_report = Report("Codex", email="selected@example.com", source=CodexHomeSource(selected),
            quotas=[Quota("Weekly", 30, self.future)])
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(agent_notes, "live_reports", return_value=[selected_report, default_report]),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            _ = agent_notes.update()
        notes = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex 1"].get("state"), "inactive")
        self.assertEqual(notes["codex 2"].get("state"), "active")
        self.assertEqual(notes["codex 1"].get("weekly_remaining_usage"), "90")
        self.assertEqual(notes["codex 2"].get("weekly_remaining_usage"), "70")
        watch.assert_called_once_with([selected_report])

    def test_five_hour_watch_selects_codex_by_home_even_with_shared_or_absent_login(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex2"
        self.codex_login(default, "shared@example.com")
        self.codex_login(spare, "shared@example.com")
        _ = self.note("codex 1.md", "shared@example.com")
        seats_report = Report("Codex", email="shared@example.com", source=CodexHomeSource(default))
        spare_report = Report("Codex", email="shared@example.com", source=CodexHomeSource(spare))
        for current_login in ("shared@example.com", None):
            seats_report.email = current_login
            with (
                self.subTest(current_login=current_login),
                patch.object(agent_notes, "live_reports", return_value=[spare_report, seats_report]),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            watch.assert_called_once_with([seats_report])

    def test_failed_selected_codex_read_preserves_activity_and_alert_acknowledgement(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        self.codex_login(default, "spare@example.com")
        self.codex_login(selected, "SEATS@example.com")
        seats = self.note("codex 1.md", "seats@example.com")
        _ = self.note("codex 2.md", "spare@example.com")
        _ = apply([seats], Report("Codex", email="seats@example.com",
            quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.select_seats("codex 1")
        before = seats.path.read_text()
        stamp = self.now.isoformat(timespec="seconds")
        episode: quota_alert.Episode = {"since": stamp, "acknowledged": stamp, "last": {}}
        state: quota_alert.State = {"episodes": {"codex 1": episode.copy()}}
        seats_report = Report("Codex", problem="Account unavailable", source=CodexHomeSource(selected))
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 95, self.future)], credit_balance="23")
        selection = agent_accounts.ClaudeAccountSelection(
            agent_accounts.UnreadableClaudeAccount(), agent_accounts.UnreadableClaudeAccount(), False, (),
        )

        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(agent_notes, "live_reports", return_value=[spare_report, seats_report]),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            _ = agent_notes.update_for_selection(selection, agent_accounts.seats_home())

        notes = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex 1"].get("state"), "active")
        self.assertEqual(seats.path.read_text(), before)
        self.assertEqual(notes["codex 1"].get("weekly_remaining_usage"), "0")
        self.assertEqual(notes["codex 2"].get("state"), "inactive")
        self.assertEqual(notes["codex 2"].get("weekly_remaining_usage"), "5")
        self.assertEqual(notes["codex 2"].get("credit_balance"), "23")
        self.assertEqual(quota_alert.close_recovered(state, list(notes.values()), 5, selection, self.now), [])
        self.assertEqual(state["episodes"], {"codex 1": episode})
        watch.assert_called_once_with([seats_report])

    def test_unreadable_selected_codex_login_preserves_every_note_state(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        self.codex_login(default, "spare@example.com")
        self.codex_login(selected, "seats@example.com")
        seats = self.note("codex 1.md", "seats@example.com")
        spare = self.note("codex 2.md", "spare@example.com")
        _ = self.note("codex 3.md", "absent@example.com")
        _ = apply([seats], Report("Codex", email="seats@example.com",
            quotas=[Quota("Weekly", 100, self.future)]), self.now)
        _ = apply([spare], Report("Codex", email="spare@example.com",
            quotas=[Quota("Weekly", 90, self.future)]), self.now)
        self.select_seats("codex 1")
        _ = (selected / "auth.json").write_text("not json")
        before = {note.path.stem: note.get("state") for note in note_file.read_notes()}
        seats_report = Report("Codex", problem="Account unavailable", source=CodexHomeSource(selected))
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 95, self.future)])

        for reports in ([spare_report, seats_report], [seats_report, spare_report]):
            with (
                self.subTest(order=[report.source for report in reports]),
                patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
                patch.object(agent_notes, "live_reports", return_value=reports),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            notes = {note.path.stem: note for note in note_file.read_notes()}
            self.assertEqual({label: note.get("state") for label, note in notes.items()}, before)
            self.assertEqual(notes["codex 1"].get("weekly_remaining_usage"), "0")
            self.assertEqual(notes["codex 2"].get("weekly_remaining_usage"), "5")
            watch.assert_called_once_with([seats_report])

    def test_update_names_the_logged_out_codex_spare_home(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex1"
        self.codex_login(default, "seats@example.com")
        spare.mkdir()
        _ = self.note("codex 2.md", "seats@example.com")
        reports = [
            Report("Codex", email="seats@example.com", source=CodexHomeSource(default)),
            Report("Codex", problem="Not logged in", source=CodexHomeSource(spare)),
        ]
        with (
            patch.object(agent_notes, "live_reports", return_value=reports),
            patch.object(five_hour, "watch", return_value=[]),
        ):
            messages = agent_notes.update()
        self.assertIn("Codex (~/.codex1): Not logged in", messages)
        self.assertNotIn("Codex: Not logged in", messages)

    def test_update_with_one_logged_out_codex_home_keeps_the_original_message(self) -> None:
        _ = self.note("codex 1.md", "seats@example.com")
        report = Report("Codex", problem="Not logged in", source=CodexHomeSource(self.root / ".codex"))
        with (
            patch.object(agent_notes, "live_reports", return_value=[report]),
            patch.object(five_hour, "watch", return_value=[]),
        ):
            messages = agent_notes.update()
        self.assertEqual(messages[-1], "Codex: Not logged in")

    def test_update_with_one_codex_home_keeps_note_states_and_messages(self) -> None:
        home = self.root / ".codex"
        self.codex_login(home, "seats@example.com")
        current = self.note("codex 1.md", "seats@example.com")
        spare = self.note("codex 2.md", "spare@example.com")
        report = Report("Codex", email="seats@example.com", source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 20, self.future)], credit_balance="7")
        checked_at = self.now

        class FixedNoteCheckTime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(checked_at.timestamp(), tz)

        with (
            patch.object(agent_notes, "live_reports", return_value=[report]),
            patch.object(agent_notes, "datetime", FixedNoteCheckTime),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            messages = agent_notes.update()
        self.assertEqual(messages, [
            "codex 1.md: state=active, credit_balance=7, weekly_remaining_usage=80, "
            + f"resets={local_reset(self.future)}, weekly_usage_checked_at={self.now.isoformat(timespec='seconds')}",
            "codex 2.md: state=inactive, weekly_remaining_usage=null, weekly_usage_checked_at=null",
        ])
        updated = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual(updated[current.path.stem].get("state"), "active")
        self.assertEqual(updated[spare.path.stem].get("state"), "inactive")
        self.assertEqual(updated[current.path.stem].get("weekly_remaining_usage"), "80")
        self.assertTrue(all(note.path.read_text().endswith("---\nBody stays.\n") for note in (current, spare)))
        watch.assert_called_once_with([report])

    def test_notes_and_watch_keep_one_seats_role_through_a_role_change_during_read(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        next_home = self.root / ".codex3"
        for home, login, label in ((default, "default@example.com", "codex 2"),
            (selected, "selected@example.com", "codex 1"), (next_home, "next@example.com", "codex 3")):
            self.codex_login(home, login)
            _ = self.note(f"{label}.md", login)
        self.select_seats("codex 1")
        reports = [Report("Codex", email="selected@example.com", source=CodexHomeSource(selected),
            quotas=[Quota("Weekly", 100, self.future)]),
            Report("Codex", email="next@example.com", source=CodexHomeSource(next_home),
                quotas=[Quota("Weekly", 10, self.future)])]
        async def read(_selection: agent_accounts.ClaudeAccountSelection, seats_home: Path) -> list[Report]:
            self.assertEqual(seats_home, selected)
            self.select_seats("codex 3")
            return reports
        with patch.dict(os.environ, {"CODEX_HOME": str(default)}), patch.object(agent_notes, "live_reports", side_effect=read), patch.object(five_hour, "watch", return_value=[]) as watch:
            _ = agent_notes.update()
        notes = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex 1"].get("state"), "active")
        self.assertEqual(notes["codex 3"].get("state"), "inactive")
        self.assertEqual(notes["codex 3"].get("weekly_remaining_usage"), "90")
        watch.assert_called_once_with([reports[0]])

    def test_update_reads_two_active_accounts_and_an_inactive_spare(self) -> None:
        default = self.account_dir("default", "main@example.com", "main-fixture-token")
        unit = self.account_dir("claude 3", "unit@example.com", "unit-fixture-token")
        spare = self.account_dir("claude 1", "spare@example.com", "spare-fixture-token")
        (self.root / ".claude9").mkdir()
        self.select_unit("claude 3")
        _ = self.note("claude 2.md", "main@example.com")
        _ = self.note("claude 3.md", "unit@example.com")
        _ = self.note("claude 1.md", "spare@example.com")
        identities = {default: "main@example.com", unit: "unit@example.com", spare: "spare@example.com"}
        tokens = {"Bearer main-fixture-token": 20, "Bearer unit-fixture-token": 96, "Bearer spare-fixture-token": 10}
        observed: list[Path] = []

        def status(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command, ("fixture-claude", "auth", "status"))
            environment = cast(dict[str, str], kwargs["env"])
            directory = Path(environment.get("CLAUDE_CONFIG_DIR", str(default)))
            observed.append(directory)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "loggedIn": True, "email": identities[directory], "authMethod": "claude.ai",
            }), "")

        def usage(request: urllib.request.Request, timeout: int) -> io.BytesIO:
            self.assertEqual(timeout, 15)
            return io.BytesIO(json.dumps({"seven_day": {
                "utilization": tokens[request.get_header("Authorization") or ""],
                "resets_at": self.future.isoformat(),
            }}).encode())

        with (
            patch.object(agent_accounts, "executable", return_value="fixture-claude"),
            patch.object(subprocess, "run", side_effect=status),
            patch.object(urllib.request, "urlopen", side_effect=usage),
            patch.object(agent_accounts, "codex_live", return_value=Report("Codex", problem="fixture")),
            patch.object(five_hour, "watch", return_value=[]) as watch,
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(spare), "CLAUDE_CODE_OAUTH_TOKEN": "inherited-token"}),
        ):
            _ = agent_notes.update()

        self.assertCountEqual(observed, [default, unit, spare])
        notes = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual({label: note.get("state") for label, note in notes.items()}, {
            "claude 1": "inactive", "claude 2": "active", "claude 3": "active",
        })
        self.assertEqual({label: note.get("weekly_remaining_usage") for label, note in notes.items()}, {
            "claude 1": "90", "claude 2": "80", "claude 3": "4",
        })
        readings = [cast(agent_notes.StoredReading, json.loads(line))
                    for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertCountEqual([(reading["account"], reading["remaining"]) for reading in readings],
                              [("claude 2", 80), ("claude 3", 4), ("claude 1", 90)])
        watched = cast(list[Report], watch.call_args.args[0])
        self.assertEqual([(report.tool, report.email) for report in watched],
                         [("Claude", "main@example.com"), ("Codex", None)])
        self.assertEqual(watched[0].source, ClaudeDirectorySource(default))

    def test_two_account_fixture_writes_two_readings(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude 3", "unit@example.com", "unit-token")
        self.select_unit("claude 3")
        _ = self.note("claude 2.md", "main@example.com")
        _ = self.note("claude 3.md", "unit@example.com")
        reports = [Report("Claude", email=email, quotas=[Quota("Weekly", used, self.future)])
                   for email, used in (("main@example.com", 20), ("unit@example.com", 96))]
        with (patch.object(agent_notes, "live_reports", return_value=reports),
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
        self.assertEqual([note.get("state") for note in note_file.read_notes()], ["active", "active"])
        self.assertEqual(len((self.root / "readings.jsonl").read_text().splitlines()), 2)
        self.assertEqual(
            {login: usage.get("weekly", {}).get("left") for login, usage in usage_store.load().items()},
            {"main@example.com": 80.0, "unit@example.com": 4.0},
        )

    def test_role_swap_during_update_keeps_one_unit_login_active(self) -> None:
        """Every note uses the same role selection for the entire update."""
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude 1", "old-unit@example.com", "old-token")
        _ = self.account_dir("claude 3", "new-unit@example.com", "new-token")
        self.select_unit("claude 1")
        for label, login in (("claude 1", "old-unit@example.com"),
                             ("claude 2", "main@example.com"),
                             ("claude 3", "new-unit@example.com")):
            _ = self.note(f"{label}.md", login)
        resolve = accounts.role_account

        def swap_after_selection(role: str) -> accounts.AccountSelection:
            selected = resolve(role)
            self.select_unit("claude 3")
            return selected

        reports = [Report("Claude", email=login, quotas=[Quota("Weekly", 20, self.future)])
                   for login in ("old-unit@example.com", "main@example.com", "new-unit@example.com")]
        with (
            patch.object(accounts, "role_account", side_effect=swap_after_selection) as selection,
            patch.object(agent_notes, "live_reports", return_value=reports),
            patch.object(five_hour, "watch", return_value=[]),
        ):
            _ = agent_notes.update()
        selection.assert_called_once_with("unit")
        self.assertEqual({note.path.stem: note.get("state") for note in note_file.read_notes()}, {
            "claude 1": "active", "claude 2": "active", "claude 3": "inactive",
        })

    def test_five_hour_watch_uses_the_default_directory_independent_of_report_order(self) -> None:
        """Order, shared logins and missing emails cannot select the named reading."""
        default = self.account_dir("default", "shared@example.com", "main-token")
        named = self.account_dir("claude 3", "shared@example.com", "named-token")
        self.select_unit("claude 3")
        _ = self.note("claude 2.md", "shared@example.com")
        _ = self.note("claude 3.md", "shared@example.com")
        main = Report("Claude", email="shared@example.com", source=ClaudeDirectorySource(default))
        unit = Report("Claude", email="shared@example.com", source=ClaudeDirectorySource(named))
        codex = Report("Codex")
        for main_email in ("shared@example.com", None):
            main.email = main_email
            with (
                self.subTest(main_email=main_email),
                patch.object(agent_notes, "live_reports", return_value=[unit, codex, main]),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            watched = cast(list[Report], watch.call_args.args[0])
            self.assertEqual(watched, [codex, main])

    def test_updater_and_alert_share_the_same_role_snapshot(self) -> None:
        """The timer entry point resolves roles once for writes and notices."""
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude 3", "unit@example.com", "unit-token")
        self.select_unit("claude 3")
        _ = self.note("claude 2.md", "main@example.com")
        _ = self.note("claude 3.md", "unit@example.com")
        selection = accounts.claude_account_selection()
        reports = [Report("Claude", email=login, quotas=[Quota("Weekly", 20, self.future)])
                   for login in ("main@example.com", "unit@example.com")]
        with (
            patch.object(accounts, "claude_account_selection", return_value=selection) as resolve,
            patch.object(agent_notes, "live_reports", return_value=reports) as collect,
            patch.object(five_hour, "watch", return_value=[]),
            patch.object(agent_notes, "alert", return_value=[]) as alert,
            patch.object(codex_pacer, "tick", side_effect=ValueError("fixture")),
            patch.object(sys, "argv", ["agent_notes.py"]),
            redirect_stdout(io.StringIO()),
        ):
            agent_notes.main()
        resolve.assert_called_once_with()
        collect.assert_called_once_with(selection, agent_accounts.seats_home())
        notice_roles = cast(object, alert.call_args.kwargs["selection"])
        self.assertIs(notice_roles, selection)

    def test_legacy_live_collection_keeps_the_original_call_and_note_states(self) -> None:
        active = self.note("claude 2.md", "main@example.com")
        spare = self.note("claude 3.md", "other@example.com")
        report = Report("Claude", email="main@example.com", quotas=[Quota("Weekly", 20, self.future)])
        with (patch.object(agent_accounts, "claude_live", return_value=report) as claude,
              patch.object(agent_accounts, "codex_live", return_value=Report("Codex")),
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
        claude.assert_called_once_with()
        updated = {note.path.stem: note for note in note_file.read_notes()}
        self.assertEqual(updated[active.path.stem].get("state"), "active")
        self.assertEqual(updated[spare.path.stem].get("state"), "inactive")

    def test_implicit_live_read_keeps_inherited_oauth_token_and_subprocess_environment(self) -> None:
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "fixture-inherited"}),
            patch.object(agent_accounts, "executable", return_value="fixture-claude"),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, '{"loggedIn": true, "email": "main@example.com", "authMethod": "claude.ai"}', "")) as status,
            patch.object(agent_accounts, "claude_credentials", side_effect=RuntimeError("unused")) as credentials,
            patch.object(urllib.request, "urlopen", return_value=io.BytesIO(
                json.dumps({"seven_day": {"utilization": 20, "resets_at": self.future.isoformat()}}).encode())) as usage,
        ):
            report = agent_accounts.claude_live()
        self.assertEqual(report.weekly_remaining, 80)
        credentials.assert_not_called()
        self.assertNotIn("env", status.call_args.kwargs)
        request = cast(urllib.request.Request, usage.call_args.args[0])
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-inherited")
        self.assertEqual(report.source, ClaudeDirectorySource(self.root / ".claude"))

    def test_named_credentials_never_fall_back_to_the_default_keychain(self) -> None:
        named = self.root / ".claude3"
        named.mkdir()
        with (patch.object(sys, "platform", "darwin"),
              patch.object(agent_accounts, "command_json") as command):
            with self.assertRaisesRegex(RuntimeError, "usage credentials unavailable"):
                _ = agent_accounts.claude_credentials(named)
        command.assert_not_called()

    def test_explicit_default_credentials_use_the_keychain_even_from_a_named_shell(self) -> None:
        with (patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}),
              patch.object(sys, "platform", "darwin"),
              patch.object(agent_accounts, "command_json", return_value={"claudeAiOauth": {"accessToken": "fixture"}}) as command):
            self.assertEqual(agent_accounts.claude_credentials(self.root / ".claude")
                             .get("claudeAiOauth", {}).get("accessToken"), "fixture")
        command.assert_called_once_with("security", "find-generic-password", "-s", "Claude Code-credentials", "-w")

    def test_whoami_prints_both_active_roles_and_omits_the_spare(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude 3", "unit@example.com", "unit-token")
        _ = self.account_dir("claude 1", "spare@example.com", "spare-token")
        self.select_unit("claude 3")
        _ = self.note("claude 2.md", "main@example.com")
        _ = self.note("claude 3.md", "unit@example.com")
        _ = self.note("claude 1.md", "spare@example.com")
        output = io.StringIO()
        with (patch.object(whoami, "live_reports", return_value=[
            Report("Claude", email="main@example.com"), Report("Claude", email="unit@example.com"),
            Report("Claude", email="spare@example.com"), Report("Codex", problem="fixture"),
        ]), redirect_stdout(output)):
            asyncio.run(whoami.main())
        self.assertIn("Account: claude 2 (showrunners)", output.getvalue())
        self.assertIn("Account: claude 3 (unit directors)", output.getvalue())
        self.assertNotIn("spare@example.com", output.getvalue())

    def test_whoami_filter_and_labels_share_one_role_selection_during_a_swap(self) -> None:
        """One display cannot switch unit accounts between filtering and rendering."""
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude 1", "old-unit@example.com", "old-token")
        _ = self.account_dir("claude 3", "new-unit@example.com", "new-token")
        self.select_unit("claude 1")
        for label, login in (("claude 1", "old-unit@example.com"),
                             ("claude 2", "main@example.com"),
                             ("claude 3", "new-unit@example.com")):
            _ = self.note(f"{label}.md", login)
        resolve = accounts.role_account

        def swap_after_selection(role: str) -> accounts.AccountSelection:
            selected = resolve(role)
            self.select_unit("claude 3")
            return selected

        reports = [Report("Claude", email=login) for login in (
            "old-unit@example.com", "main@example.com", "new-unit@example.com",
        )]
        output = io.StringIO()
        with (
            patch.object(accounts, "role_account", side_effect=swap_after_selection) as selection,
            patch.object(whoami, "live_reports", return_value=reports),
            redirect_stdout(output),
        ):
            asyncio.run(whoami.main())
        selection.assert_called_once_with("unit")
        self.assertIn("Account: claude 1 (unit directors)", output.getvalue())
        self.assertIn("Account: claude 2 (showrunners)", output.getvalue())
        self.assertNotIn("new-unit@example.com", output.getvalue())

    def test_legacy_whoami_account_line_is_unchanged(self) -> None:
        _ = self.note("claude 2.md", "main@example.com")
        self.assertEqual(whoami.render(Report("Claude", email="main@example.com"),
                                      accounts.claude_account_selection(), agent_accounts.seats_home()), [
            "Claude", "  Account: claude 2 — main@example.com", "  Plan: unavailable",
        ])

    def test_live_percentage_is_written_only_to_matching_tool_and_email(self):
        active = self.note("claude 1.md", "a@example.com")
        other = self.note("claude 2.md", "b@example.com")
        codex = self.note("codex 1.md", "a@example.com")
        original_codex = codex.path.read_text()
        report = Report("Claude", email="A@example.com", quotas=[Quota("Weekly", 99, self.future)])
        _ = apply([active, other, codex], report, self.now)
        self.assertEqual(active.get("weekly_remaining_usage"), "1")
        self.assertEqual(active.get("state"), "active")
        self.assertEqual(active.get("resets"), local_reset(self.future))
        self.assertEqual(other.get("weekly_remaining_usage"), "null")
        self.assertEqual(codex.path.read_text(), original_codex)
        self.assertTrue(active.path.read_text().endswith("---\nBody stays.\n"))
        self.assertEqual(active.path.stat().st_mode & 0o777, 0o644)

    def test_switch_preserves_inactive_observation_until_window_expires(self):
        old = self.note("codex 1.md", "old@example.com")
        new = self.note("codex 2.md", "new@example.com")
        _ = apply([old, new], Report("Codex", email="old@example.com", quotas=[Quota("Weekly", 20, self.future)]), self.now)
        checked = old.get("weekly_usage_checked_at")
        _ = apply([old, new], Report("Codex", email="new@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        self.assertEqual(old.get("state"), "inactive")
        self.assertEqual(old.get("weekly_remaining_usage"), "80")
        self.assertEqual(old.get("weekly_usage_checked_at"), checked)
        self.assertEqual(new.get("weekly_remaining_usage"), "95")

    def test_expired_inactive_usage_is_unknown(self):
        past = local_reset(self.now - timedelta(days=1))
        note = self.note("claude 1.md", "old@example.com", f"resets: {past}\nweekly_remaining_usage: 30\n")
        _ = apply([note], Report("Claude", email="new@example.com"), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("resets"), past)

    def test_failed_usage_is_not_zero_and_does_not_get_a_fresh_timestamp(self):
        note = self.note("claude 1.md", "a@example.com")
        _ = apply([note], Report("Claude", email="a@example.com", quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "0")
        checked = note.get("weekly_usage_checked_at")
        _ = apply([note], Report("Claude", email="a@example.com", quota_problem="Unavailable"), self.now + timedelta(minutes=2))
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("weekly_usage_checked_at"), checked)

    def test_unknown_identity_does_not_mark_account_inactive(self):
        note = self.note("codex 1.md", "a@example.com")
        _ = apply([note], Report("Codex", email="a@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        original = note.path.read_text()
        _ = apply([note], Report("Codex", problem="Unavailable"), self.now)
        self.assertEqual(note.path.read_text(), original)

    def test_reset_expiration_uses_eastern_date_and_summary_count(self):
        note = self.note("codex 1.md", "a@example.com")
        report = Report("Codex", email="a@example.com")
        reset_credits(report, {"availableCount": 2, "credits": [
            {"status": "available", "expiresAt": "2026-10-23T01:00:00Z"},
            {"status": "redeemed", "expiresAt": "2026-10-01T01:00:00Z"},
        ]})
        _ = apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        self.assertEqual(note.get("limit_reset"), "2026-10-22T21:00:00-04:00")
        # An unavailable API answer must not erase a known reset.
        _ = apply([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        reset_credits(report, {"availableCount": 0, "credits": []})
        _ = apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "0")
        self.assertEqual(note.get("limit_reset"), "null")

    def test_credit_balance_is_read_from_the_main_bucket(self):
        def usage(credits: object) -> CodexRateLimits:
            return {"rateLimits": {"credits": credits}}  # pyright: ignore[reportReturnType]

        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False,
                                              "balance": "60498.0534770000"})), "60498")
        self.assertEqual(codex_credits(usage({"hasCredits": False, "unlimited": False, "balance": None})), "0")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": True, "balance": None})), "unlimited")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False, "balance": None})), "null")
        self.assertEqual(codex_credits(usage(None)), "null")
        self.assertEqual(codex_credits({}), "null")

    def test_credit_balance_is_written_and_an_unread_one_is_kept(self):
        note = self.note("codex 1.md", "a@example.com")
        _ = apply([note], Report("Codex", email="a@example.com", credit_balance="60498"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        # A failed read must not erase the last balance seen.
        _ = apply([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        _ = apply([note], Report("Codex", email="a@example.com", credit_balance="null"), self.now)
        self.assertEqual(note.get("credit_balance"), "null")

    def test_codex_reading_includes_numeric_credit_balance_from_the_note(self):
        note = self.note("codex 1.md", "a@example.com", "credit_balance: 60498\n")
        weekly = Quota("Weekly", 40, self.future)
        _ = apply([note], Report("Codex", email="a@example.com", quotas=[weekly]), self.now)
        records = [json.loads(line) for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertEqual(records, [{
            "account": "codex 1",
            "at": self.now.isoformat(timespec="seconds"),
            "remaining": 60,
            "credits": 60498.0,
        }])

    def test_codex_reading_omits_absent_or_non_numeric_credit_balance(self):
        absent = self.note("codex 1.md", "a@example.com")
        unlimited = self.note("codex 2.md", "a@example.com", "credit_balance: unlimited\n")
        weekly = Quota("Weekly", 40, self.future)
        _ = apply([absent, unlimited], Report("Codex", email="a@example.com", quotas=[weekly]), self.now)
        records = [json.loads(line) for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertCountEqual(records, [
            {"account": "codex 1", "at": self.now.isoformat(timespec="seconds"), "remaining": 60},
            {"account": "codex 2", "at": self.now.isoformat(timespec="seconds"), "remaining": 60},
        ])

    def test_claude_manual_reset_fields_are_preserved(self):
        note = self.note("claude 1.md", "a@example.com", "limit_reset: 2026-10-22\nlimit_reset_count: 1\n")
        _ = apply([note], Report("Claude", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset"), "2026-10-22")
        self.assertEqual(note.get("limit_reset_count"), "1")

    def test_reading_for_each_active_value_and_none_for_null_or_inactive(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        first = self.note("claude 1.md", "a@example.com")
        second = self.note("claude 2.md", "a@example.com")
        inactive = self.note("claude 3.md", "b@example.com")
        weekly = Quota("Weekly", 40, checked + timedelta(days=2))
        _ = apply([second, inactive, first], Report("Claude", email="a@example.com", quotas=[weekly]), checked)
        path = self.root / "readings.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertCountEqual(records, [
            {"account": "claude 1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
            {"account": "claude 2", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
        ])
        _ = apply([first, second, inactive], Report("Claude", email="a@example.com"), checked + timedelta(minutes=2))
        self.assertEqual(len(path.read_text().splitlines()), 2)
        self.assertEqual(inactive.get("state"), "inactive")

    def test_reading_log_keeps_only_the_last_eight_days(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude 1.md", "a@example.com")
        path = self.root / "readings.jsonl"
        old = {"account": "claude 1", "at": "2026-09-27T19:44:59+00:00", "remaining": 70}
        recent = {"account": "claude 1", "at": "2026-09-27T19:45:00+00:00", "remaining": 65}
        _ = path.write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
        _ = apply([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records, [recent, {
            "account": "claude 1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60,
        }])

    def test_unwritable_log_reports_once_and_keeps_note_write(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude 1.md", "a@example.com")
        (self.root / "readings.jsonl").mkdir()
        messages = apply([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        self.assertEqual(note.get("weekly_remaining_usage"), "60")
        self.assertEqual(note.get("weekly_usage_checked_at"), "2026-10-05T19:45:00+00:00")
        self.assertEqual(len(messages), 2)
        self.assertEqual(sum("log" in message.lower() or "readings" in message.lower() for message in messages), 1)


if __name__ == "__main__":
    _ = unittest.main()
