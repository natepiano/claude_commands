"""A provider refusal moves only its live owner, once, through safe shutdown."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami.note_file import note_name

from scripts.production import staff_sources
import io
import fcntl
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from collections.abc import Generator, Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Self, cast, final, override
from unittest.mock import call, patch

from scripts.accounts import accounts, moves, next_moves, refusal_watch, refusals, role_map
from scripts.accounts.move_test_support import FakeSessionMoves, MoveCall
from scripts.shutdown import stop_resume
from scripts.production import showrunners
from scripts.message import send as message_send
from scripts.whoami import run_out
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.whoami import usage_store
import quota_alert

send_no_room_notice = refusal_watch._notify_no_room  # pyright: ignore[reportPrivateUsage]
deliver_notice_jobs = quota_alert.deliver_and_record

if TYPE_CHECKING:
    from scripts.shutdown.unit_staff_test_support import isolated_parents, unit_host
    from scripts.shutdown import inventory, launch_permission, shutdown_record, settle
    from scripts.message import sessions
else:
    from unit_staff_test_support import isolated_parents, unit_host
    launch_permission = import_module("launch_permission")
    inventory = import_module("inventory")
    shutdown_record = import_module("shutdown_record")
    settle = import_module("settle")
    sessions = import_module("sessions")


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
SOURCE_LOGIN = "source@example.invalid"
TARGET_LOGIN = "target@example.invalid"
def stored_group_labels(path: Path) -> dict[str, str]:
    raw = cast(dict[str, object], json.loads(path.read_text()))
    return {group: value if isinstance(value, str) else cast(str, cast(dict[str, object], value)["label"])
            for group, value in raw.items()}


NO_SESSION_FILTER: frozenset[str] = frozenset()
REFUSAL_AT = NOW - timedelta(seconds=1)
WEEKLY_RESET = NOW + timedelta(days=2)
FIVE_HOUR_RESET = NOW + timedelta(hours=3)


class WatchTime(datetime):
    at: datetime = NOW

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.at.timestamp(), tz or timezone.utc)


@final
class RefusalWatchTests(unittest.TestCase):
    home: Path = Path()
    owners: list[inventory.Session] = []
    live: list[sessions.SessionRecord] = []
    notifications: list[str] = []
    moves: FakeSessionMoves = FakeSessionMoves()
    head_deliveries: list[list[quota_alert.Job]] = []

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": os.defpath, "AGENT_NOTES_DIR": str(self.home / "notes"),
            "SHUTDOWN_STATE_DIR": str(self.home / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.home / "sessions"), "USER_ZONE": "America/New_York",
        }, clear=True)))
        (self.home / "notes").mkdir()
        _ = self.enterContext(patch.object(socket, "gethostname", return_value="natedev"))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("watcher reached ssh")))
        _ = self.enterContext(patch.object(run_out, "READINGS_LOG", self.home / "pace.jsonl"))
        (self.home / ".claude").mkdir()
        (self.home / "notifier").mkdir()
        # Production scripts import the registry under both spellings, each with paths fixed at import.
        for registry in (showrunners, import_module("showrunners")):
            for field, value in (("NOTIFIER_STATE_DIR", self.home / "notifier"), ("SESSIONS_DIR", self.home / "sessions"),
                                 ("SHOWRUNNER_STATE_DIR", self.home / "showrunner")):
                _ = self.enterContext(patch.object(registry, field, value))
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        for label, login in (("claude1", SOURCE_LOGIN), ("claude2", "main@example.invalid"), ("claude3", TARGET_LOGIN)):
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
            _ = (self.home / "notes" / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        _ = (self.home / "notes/claude2.md").write_text("---\nlogin: main@example.invalid\n---\n")
        _ = self.enterContext(isolated_parents())
        self.owners = []
        self.live = []
        WatchTime.at = NOW
        _ = self.enterContext(patch.object(refusal_watch, "datetime", WatchTime))
        # Readings are stored at NOW; status re-reads a login live once its reading is older than STALE_AFTER.
        _ = self.enterContext(patch.object(accounts, "datetime", WatchTime))
        _ = self.enterContext(patch.object(sessions, "live_sessions", side_effect=lambda: self.live))
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.process_dir))
        _ = self.enterContext(patch.object(account, "account_of", side_effect=self.process_account))
        _ = self.enterContext(patch.object(inventory, "inventory", side_effect=self.inventory_for))
        _ = self.enterContext(patch.object(inventory, "listing", side_effect=self.listing))
        _ = self.enterContext(patch.object(inventory, "unregistered_showrunners", return_value=[]))
        self.moves = FakeSessionMoves(self.moved)
        _ = self.enterContext(patch.object(stop_resume, "move_sessions", side_effect=self.moves))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("unfaked subprocess")))
        self.notifications = []
        _ = self.enterContext(patch.object(refusal_watch, "_notify_no_room", side_effect=self.notified))
        self.head_deliveries = []
        _ = self.enterContext(patch.object(quota_alert, "load_config", return_value={"always": ["fixture-showrunner"]}))
        _ = self.enterContext(patch.object(quota_alert, "recipients", return_value=["fixture-showrunner"]))
        _ = self.enterContext(patch.object(quota_alert, "deliver_and_record", side_effect=self.deliver_head_notice))
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.home / "quota_alert.json"))
        self.moves.calls = []
        self.reading("main@example.invalid", 80, reset=NOW + timedelta(days=6))
        self.reading(SOURCE_LOGIN, 0)
        self.reading(TARGET_LOGIN, 1, reset=NOW + timedelta(days=1))

    def process_dir(self, pid: int) -> account.ConfiguredClaudeFolder | account.ProcessEnvironmentUnread:
        found = next((owner for owner in self.owners if owner["pid"] == pid), None)
        return account.ConfiguredClaudeFolder(Path(found["config_dir"])) if found is not None and "config_dir" in found else account.ProcessEnvironmentUnread("fixture")

    def process_account(self, pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
        directory = self.process_dir(pid)
        if not isinstance(directory, account.ConfiguredClaudeFolder):
            return None
        if directory.path == self.home / ".claude" and isinstance(home_state, account.LegacyHomeLogin):
            return home_state.account
        identity = accounts.account_identity(directory.path)
        return account.Account("claude", identity.login, identity.label) if isinstance(identity, agent_accounts.NumberedAccount) else None

    def inventory_for(self, login: str, only: frozenset[str] = NO_SESSION_FILTER, *,
                      refresh_desktops: bool = True) -> inventory.Inventory:
        self.assertFalse(refresh_desktops, "the watcher must read the desktop snapshot without invoking its timer")
        owners = [owner for owner in self.owners
                  if (identity := self.process_account(owner["pid"], home_state=account.home_account_state())) is not None
                  and identity.login.casefold() == login.casefold()
                  and (not only or owner["session_id"] in only)]
        return inventory.Inventory(machine="natedev", login=login,
            label="claude1" if login == SOURCE_LOGIN else "claude3", sessions=owners, unattributed=[])

    def listing(self, login: str | None = None, *,
                registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
        del registrations
        result: list[inventory.ListedSession] = []
        for owner in self.owners:
            identity = self.process_account(owner["pid"], home_state=account.home_account_state())
            if identity is None or login is not None and identity.login != login:
                continue
            production = (owner["production"] if owner["kind"] == "showrunner" else
                          staff_sources.unit_parent(owner["host"]["unit"]).slug if owner["host"]["kind"] == "unit" else "")
            doc = (owner["doc"] if owner["kind"] == "showrunner" else
                   staff_sources.unit_parent(owner["host"]["unit"]).doc if owner["host"]["kind"] == "unit" else "")
            result.append(inventory.ListedSession(owner["session_id"], owner["pid"], owner["proc_start"],
                owner["name"], owner["kind"], Path(owner.get("config_dir", str(self.home / ".claude"))), identity.login, production, doc))
        return result

    def owner(self, identity: str, *, host: str = "terminal", label: str = "claude1") -> inventory.Session:
        fields = inventory.SessionFields(session_id=identity, pid=1000 + len(self.owners),
            proc_start="fixture-start", name="owner " + identity, cwd=str(self.home / "checkout"), probe=False,
            status="idle", updated_at=0, model=inventory.ModelName(kind="model", name="opus-real"),
            checkout=inventory.NotACheckout(kind="not a checkout"),
            run_dirs=[], codex_servers=[], timers=[], config_dir=str(accounts.account_dir(label)))
        if host == "unit":
            owner: inventory.Session = inventory.UnitSession(**fields, kind="unit", host=unit_host(
                kind="unit", production="fixture", unit=identity, doc=str(self.home / "fixture-production.md"), session_name=identity, session_host="herdr",
                plan=inventory.NoRunRecord(kind="no run record")))
        elif host == "herdr":
            owner = inventory.ShowrunnerSession(**fields, kind="showrunner", production="fixture", doc="/fixture.md",
                host=inventory.HerdrHost(kind="herdr", herdr_session=identity,
                    pane_id="w1:p1", presentation="detached"))
        else:
            where: inventory.SessionHost = (inventory.TerminalHost(kind="terminal") if host == "terminal" else
                inventory.WindowHost(kind="ghostty" if host == "ghostty" else "zed", window_shell=2000,
                    desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
            owner = inventory.TopLevelSession(**fields, kind="top-level", host=where)
        self.owners.append(owner)
        self.live.append(sessions.SessionRecord(pid=owner["pid"], sessionId=identity, name=owner["name"],
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))
        return owner

    def transcript(self, identity: str, text: str = "weekly limit", *, at: datetime = REFUSAL_AT,
                   entry_id: str = "refusal-one", tail: Mapping[str, object] | None = None) -> Path:
        path = self.home / ".claude/projects/fixture" / f"{identity}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "assistant", "message": {"model": "opus-real", "content": []}},
            {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
             "timestamp": at.isoformat(), "uuid": entry_id,
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}},
        ]
        if tail is not None:
            entries.append(dict(tail))
        _ = path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        return path

    def reading(self, login: str, weekly: float, *, five: float = 50, at: datetime = NOW,
                reset: datetime = WEEKLY_RESET, five_reset: datetime = FIVE_HOUR_RESET) -> None:
        saved = usage_store.load()
        saved[usage_store.key("Claude", login)] = usage_store.StoredUsage(
            weekly=usage_store.StoredWindow(left=weekly, resets=reset.isoformat(), at=at.isoformat()),
            five_hour=usage_store.StoredWindow(left=five, resets=five_reset.isoformat(), at=at.isoformat()))
        usage_store.write(saved)

    def cli(self, *arguments: str) -> tuple[int | None, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = refusal_watch.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix != ".lock"}

    def notified(self, text: str, _now: datetime) -> bool:
        self.notifications.append(text)
        return True

    def deliver_head_notice(self, jobs: list[quota_alert.Job], _stamp: str) -> list[str]:
        self.head_deliveries.append(jobs)
        return [f"quota {job.kind} {job.name} -> {job.recipient}: sent" for job in jobs]

    def moved(self, call: MoveCall) -> None:
        owner = next(owner for owner in self.owners if owner["session_id"] == call.session_id)
        owner["config_dir"] = str(accounts.account_dir(call.target))

    def groups(self, units: str = "claude1", showrunners: str = "claude3") -> Path:
        path = self.home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def next_entries(self, *entries: next_moves.NextMove) -> None:
        with next_moves.next_moves_lock():
            next_moves.write_next_moves(entries)

    def low_rule(self, name: str = "units-low", *, group: accounts.ClaudeGroup = "units",
                 target: str = "claude2", threshold: float = 1) -> next_moves.ClaudeGroupMove:
        return next_moves.ClaudeGroupMove(name, group, threshold, 0, target, NOW)

    def hold_rule(self, name: str = "units-low", *, target: str = "claude2",
                  minutes: float = 10) -> next_moves.ClaudeGroupMove:
        return next_moves.ClaudeGroupMove(name, "units", 1, minutes, target, NOW)

    def expired_login(self, login: str = "main@example.invalid") -> None:
        usage_store.save([agent_accounts.Report("Claude", email=login,
            usage_read_outcome=agent_accounts.UsageReadOutcome.STALE_TOKEN)], NOW)

    def test_expired_target_renews_before_the_queue_lock_and_moves_in_the_same_pass(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule())
        self.expired_login()
        report = agent_accounts.Report("Claude", email="main@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.OK,
            quotas=[agent_accounts.Quota("Weekly", 10, NOW + timedelta(days=6))])

        def probe(_command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            # A second descriptor must take the queue lock without blocking.
            with next_moves.next_moves_file().with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            WatchTime.at = NOW + timedelta(seconds=2)
            return subprocess.CompletedProcess([], 0, "private", "private")

        with (patch.object(subprocess, "run", side_effect=probe) as started,
              patch.object(agent_accounts, "claude_live", return_value=report) as read):
            lines = refusal_watch.run(NOW)
        self.assertIn("claude2 login renewed", lines)
        renewed_usage = usage_store.load()[usage_store.key("Claude", "main@example.invalid")]
        assert "weekly" in renewed_usage
        self.assertEqual(renewed_usage["weekly"]["at"], (NOW + timedelta(seconds=2)).isoformat())
        self.assertTrue(any("move from claude1 to claude2" in line for line in lines))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), ())
        started.assert_called_once()
        read.assert_called_once_with(accounts.account_dir("claude2"))
        self.assertEqual(self.head_deliveries, [])
        attempt = accounts.login_renewal_attempts()["claude2"]
        self.assertEqual(attempt, accounts.LoginRenewalAttempt(NOW, accounts.LoginRenewed()))

    def test_damaged_login_renewal_state_does_not_stop_the_pass(self) -> None:
        self.expired_login()
        path = accounts.login_renewals_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"claude2":')
        with (patch.object(accounts, "renew_login", return_value=accounts.LoginRenewed()) as probe,
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("unexpected live usage"))):
            self.assertIn("claude2 login renewed", refusal_watch.run(NOW))
        probe.assert_called_once_with("claude2")
        self.assertEqual(accounts.login_renewal_attempts(),
                         {"claude2": accounts.LoginRenewalAttempt(NOW, accounts.LoginRenewed())})

    def test_expired_login_wait_keeps_the_last_reading_time(self) -> None:
        _ = self.groups()
        self.next_entries(self.low_rule())
        self.reading("main@example.invalid", 80, at=NOW - timedelta(hours=3, minutes=55))
        self.expired_login()
        lines = refusal_watch.run_auto_moves(NOW)
        self.assertIn("units 1 → claude2 due, claude2 has no fresh reading since 04:05", lines)

    def test_failed_login_renewal_blocks_once_and_does_not_probe_again_within_an_hour(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule())
        self.expired_login()
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 7, "private", "private")) as probe,
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("failed probe read usage"))):
            first = refusal_watch.run(NOW)
            later = refusal_watch.run(NOW + timedelta(minutes=59))
        probe.assert_called_once()
        self.assertIn("claude2 login renewal failed: probe exited with status 7", first)
        blocked = ("units 1 → claude2 due, claude2's login expired, and a probe session on it failed "
                   "(probe exited with status 7). Log in on claude2 by hand.")
        self.assertIn(blocked, first)
        self.assertNotIn("login renewal failed:", "\n".join(later))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(len(self.head_deliveries), 1)
        self.assertEqual(self.head_deliveries[0][0].text, blocked)
        self.assertEqual(accounts.login_renewal_attempts()["claude2"].result,
                         accounts.LoginRenewalFailed("probe exited with status 7"))

    def test_expired_login_dry_run_and_fresh_account_do_not_probe(self) -> None:
        self.expired_login()
        before = self.snapshot()
        with (patch.object(accounts, "renew_login", side_effect=AssertionError("dry run started a probe")),
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("dry run read usage"))):
            self.assertEqual(refusal_watch.renew_expired_logins(NOW, True), ["would renew claude2"])
        self.assertEqual(self.snapshot(), before)
        self.reading("main@example.invalid", 80)
        with (patch.object(accounts, "renew_login", side_effect=AssertionError("fresh account started a probe")),
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("fresh account read usage"))):
            self.assertEqual(refusal_watch.renew_expired_logins(NOW, False), [])
        self.assertFalse(accounts.login_renewals_file().exists())

    def test_login_renewal_retries_at_an_hour_on_the_other_machine_too(self) -> None:
        self.expired_login()
        with (patch.object(accounts, "renew_login", return_value=accounts.LoginRenewalFailed("probe could not start")) as probe,
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("unexpected live usage")),
              patch.object(socket, "gethostname", return_value="macbook")):
            self.assertEqual(refusal_watch.renew_expired_logins(NOW, False),
                             ["claude2 login renewal failed: probe could not start"])
            self.assertEqual(refusal_watch.renew_expired_logins(NOW + timedelta(minutes=59), False), [])
            self.assertEqual(refusal_watch.renew_expired_logins(NOW + timedelta(minutes=60), False),
                             ["claude2 login renewal failed: probe could not start"])
        self.assertEqual(probe.call_count, 2)

    def test_list_and_runner_share_the_current_hold_verdict_for_every_selection_state(self) -> None:
        entry = self.hold_rule("shared-verdict", minutes=30)
        self.next_entries(entry)
        # Without a fresh reading, the runner can only retain or discard this hold.
        usage_store.write({})
        duration = next_moves.ResolvedHold(30, next_moves.FixedHold(30))
        recorded = accounts.GroupTransition("current-move", accounts.HandOrigin())
        no_transition = accounts.NoGroupTransition()
        cases: tuple[tuple[str, str | accounts.GroupNotConfigured,
                           accounts.GroupTransition | accounts.NoGroupTransition,
                           str, str | accounts.NoGroupTransition, float, bool], ...] = (
            ("current recorded move", "claude1", recorded, "claude1", "current-move", 1, True),
            ("account differs", "claude3", recorded, "claude1", "current-move", 1, False),
            ("group unconfigured", accounts.GroupNotConfigured(), recorded, "claude1", "current-move", 1, False),
            ("move id differs", "claude1", recorded, "claude1", "previous-move", 1, False),
            ("legacy hold against recorded move", "claude1", recorded, "claude1", no_transition, 1, False),
            ("recorded hold against legacy selection", "claude1", no_transition, "claude1", "current-move", 1, False),
            ("both legacy transitions", "claude1", no_transition, "claude1", no_transition, 1, True),
            ("percent differs", "claude1", recorded, "claude1", "current-move", 2, False))
        for reason, selected_account, transition, held_account, held_move, percent, current in cases:
            with self.subTest(reason=reason):
                selected: accounts.GroupAccounts = {}
                if isinstance(selected_account, str):
                    selected["units"] = selected_account
                held = refusal_watch.HoldStarted(held_account, NOW, held_move, percent, duration)
                refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
                    auto_move_holds={entry.id: held}))
                with (patch.object(accounts, "group_accounts", return_value=selected),
                      patch.object(accounts, "group_transition", return_value=transition)):
                    shown = refusal_watch.started_next_move_holds((entry,))
                    self.assertEqual(refusal_watch.run_auto_moves(NOW), [])
                expected_shown = {entry.id: next_moves.StartedHold(NOW, duration)} if current else {}
                self.assertEqual(shown, expected_shown)
                saved = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(saved, {entry.id: held} if current else {})
                self.assertEqual(bool(shown), entry.id in saved)
                self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_list_and_runner_both_follow_the_shared_predicate_when_its_verdict_is_forced(self) -> None:
        entry = self.hold_rule("forced-verdict", minutes=30)
        self.next_entries(entry)
        usage_store.write({})
        selected: accounts.GroupAccounts = {"units": "claude1"}
        transition = accounts.GroupTransition("current-move", accounts.HandOrigin())
        duration = next_moves.ResolvedHold(30, next_moves.FixedHold(30))
        for verdict in (False, True):
            with self.subTest(verdict=verdict):
                # Force rejection of a current hold and acceptance of a stale one.
                held = refusal_watch.HoldStarted("claude3" if verdict else "claude1", NOW,
                    "previous-move" if verdict else "current-move", 2 if verdict else 1, duration)
                refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
                    auto_move_holds={entry.id: held}))
                with (patch.object(accounts, "group_accounts", return_value=selected),
                      patch.object(accounts, "group_transition", return_value=transition),
                      patch.object(refusal_watch.HoldStarted, "is_current", autospec=True,
                                   return_value=verdict) as predicate):
                    shown = refusal_watch.started_next_move_holds((entry,))
                    self.assertEqual(refusal_watch.run_auto_moves(NOW), [])
                expected_call = call(held, refusal_watch.GroupSelection("claude1", "current-move"), entry.at_percent)
                self.assertEqual(predicate.call_args_list, [expected_call, expected_call])
                self.assertEqual(shown, {entry.id: next_moves.StartedHold(NOW, duration)} if verdict else {})
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}),  # pyright: ignore[reportPrivateUsage]
                                 {entry.id: held} if verdict else {})
                self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_list_keeps_the_runners_started_minutes_after_restart_and_changed_pace(self) -> None:
        _ = self.groups()
        entry = next_moves.ClaudeGroupMove("display-half", "units", 1.234567,
                                           next_moves.HalfPointHold(), "claude2", NOW)
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 1)
        with patch.object(run_out, "weighted_rate", return_value=1 / 3600):
            self.assertEqual(self.cli()[0], 0)
        before = (self.home / ".local/state/accounts/refusal_watch.json").read_bytes()
        WatchTime.at = NOW + timedelta(minutes=18, seconds=1)
        self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
        # A new runner invocation reloads the receipt, even with a different pace.
        with patch.object(run_out, "weighted_rate", return_value=6 / 3600) as changed_pace:
            self.assertEqual(self.cli()[0], 0)
            with (patch.object(next_moves, "render_time", return_value=WatchTime.at) as clock,
                  patch.object(refusal_watch, "_load_state", wraps=refusal_watch._load_state) as read,  # pyright: ignore[reportPrivateUsage]
                  patch.object(refusal_watch, "_watch_lock", side_effect=AssertionError("render took watch lock"))):
                self.assertEqual(next_moves.list_next_moves(),
                    "units 1: at 1.234567% → claude2, hold half a point (30 min at 1 points/hour, 12 min left)")
            clock.assert_called_once_with()
            read.assert_called_once_with()
            changed_pace.assert_not_called()
        self.assertEqual((self.home / ".local/state/accounts/refusal_watch.json").read_bytes(), before)
        with patch.object(next_moves, "render_time", return_value=NOW + timedelta(minutes=30)):
            self.assertIn("30 min at 1 points/hour, hold over", next_moves.list_next_moves())

    def test_same_mode_hold_reply_keeps_the_started_row_without_reentering_the_watch_lock(self) -> None:
        _ = self.groups()
        entry = next_moves.ClaudeGroupMove("same-mode", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        self.next_entries(entry)
        held = refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1,
            next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)))
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={entry.id: held}))
        before = (self.home / ".local/state/accounts/refusal_watch.json").read_bytes()
        entries = 0

        @contextmanager
        def one_watch_lock() -> Generator[None]:
            nonlocal entries
            self.assertEqual(entries, 0, "render must not reacquire the editing lock")
            entries += 1
            yield

        with (patch.object(next_moves, "render_time", return_value=NOW + timedelta(minutes=18, seconds=1)),
              patch.object(refusal_watch, "_watch_lock", side_effect=one_watch_lock)):
            reply = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.HalfPointHold())
        self.assertEqual(entries, 1)
        self.assertEqual(reply,
            "units 1: at 1% → claude2, hold half a point (30 min at 1 points/hour, 12 min left)")
        self.assertEqual((self.home / ".local/state/accounts/refusal_watch.json").read_bytes(), before)
        reply = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.FixedHold(10))
        self.assertEqual(reply, "units 1: at 1% → claude2, hold 10 min")
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(next_moves.read_next_moves()[0].id, entry.id)

    def test_only_current_resolved_head_holds_are_shown_and_render_does_not_write_state(self) -> None:
        groups = self.groups()
        head = next_moves.ClaudeGroupMove("head", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        later = next_moves.ClaudeGroupMove("later", "units", 1, next_moves.HalfPointHold(), "claude3", NOW)
        duration = next_moves.ResolvedHold(30, next_moves.PaceEstimate(1))
        held = refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1, duration)
        self.next_entries(head, later)
        # Only the current head is eligible, even if the queued entry has a receipt.
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={head.id: held, later.id: held}))
        estimate = next_moves.ResolvedHold(5, next_moves.PaceEstimate(6))
        with (patch.object(next_moves, "render_time", return_value=NOW + timedelta(minutes=18)),
              patch.object(accounts, "group_half_point_hold", return_value=estimate)):
            self.assertEqual(next_moves.list_next_moves(),
                "units 1: at 1% → claude2, hold half a point (30 min at 1 points/hour, 12 min left)\n"
                + "units 2: at 1% → claude3, hold half a point (≈5 min)")
        cases = (
            ("account moved", replace(held, account="claude3")),
            ("transition moved away and back", replace(held, move_id="previous-selection")),
            ("threshold changed", replace(held, percent=2)),
            ("legacy duration", replace(held, duration=refusal_watch.LegacyHoldDuration())))
        for reason, stale in cases:
            with self.subTest(reason=reason):
                refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
                    auto_move_holds={head.id: stale, later.id: held}))
                before = (self.home / ".local/state/accounts/refusal_watch.json").read_bytes()
                with (patch.object(next_moves, "render_time", return_value=NOW),
                      patch.object(accounts, "group_half_point_hold", return_value=estimate),
                      patch.object(refusal_watch, "_watch_lock", side_effect=AssertionError("render took watch lock"))):
                    self.assertEqual(next_moves.list_next_moves(),
                        "units 1: at 1% → claude2, hold half a point (≈5 min)\n"
                        + "units 2: at 1% → claude3, hold half a point (≈5 min)")
                self.assertEqual((self.home / ".local/state/accounts/refusal_watch.json").read_bytes(), before)
        # A changed current selection, rather than a changed saved receipt, is stale too.
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={head.id: held}))
        _ = groups.write_text(json.dumps({"units": "claude3", "showrunners": "claude3"}))
        with patch.object(accounts, "group_half_point_hold", return_value=estimate):
            self.assertEqual(next_moves.list_next_moves(),
                "units 1: at 1% → claude2, hold half a point (≈5 min)\n"
                + "units 2: at 1% → claude3, hold half a point (≈5 min)")

    def test_missing_state_and_zero_minute_heads_do_not_claim_a_started_hold(self) -> None:
        _ = self.groups()
        entry = next_moves.ClaudeGroupMove("missing", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        self.next_entries(entry)
        state_path = self.home / ".local/state/accounts/refusal_watch.json"
        self.assertFalse(state_path.exists())
        with patch.object(accounts, "group_half_point_hold", return_value=next_moves.ResolvedHold(5, next_moves.PaceEstimate(6))):
            self.assertEqual(next_moves.list_next_moves(), "units 1: at 1% → claude2, hold half a point (≈5 min)")
        self.assertFalse(state_path.exists())
        zero = replace(entry, hold_mode=next_moves.FixedHold(0))
        self.next_entries(zero)
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={entry.id: refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1,
                next_moves.ResolvedHold(30, next_moves.FixedHold(30)))}))
        self.assertEqual(next_moves.list_next_moves(), "units 1: at 1% → claude2")

    def test_full_list_uses_one_state_read_and_one_clock_for_all_group_heads(self) -> None:
        _ = self.groups()
        unit = self.hold_rule("units", minutes=30)
        runner = next_moves.ClaudeGroupMove("runners", "showrunners", 1, 30, "claude2", NOW)
        worker = next_moves.WorkersMove("workers", 1, next_moves.WorkersToClaude(), NOW)
        self.next_entries(unit, runner, worker)
        duration = next_moves.ResolvedHold(30, next_moves.FixedHold(30))
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={unit.id: refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1, duration),
                             runner.id: refusal_watch.HoldStarted("claude3", NOW, accounts.NoGroupTransition(), 1, duration)}))
        with (patch.object(next_moves, "render_time", return_value=NOW + timedelta(minutes=18, seconds=1)) as clock,
              patch.object(refusal_watch, "_load_state", wraps=refusal_watch._load_state) as read,  # pyright: ignore[reportPrivateUsage]
              patch.object(refusal_watch, "_watch_lock", side_effect=AssertionError("render took watch lock"))):
            self.assertEqual(next_moves.list_next_moves(),
                "showrunners 1: at 1% → claude2, hold 30 min (12 min left)\n\n"
                + "units 1: at 1% → claude2, hold 30 min (12 min left)\n\nworkers 1: at 1% → claude")
        clock.assert_called_once_with()
        read.assert_called_once_with()

    def test_half_point_hold_keeps_its_start_pace_and_duration_after_a_restart(self) -> None:
        path = self.groups()
        entry = next_moves.ClaudeGroupMove("half-point", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 1)
        with (patch.object(run_out, "READINGS_LOG", self.home / "pace.jsonl"),
              patch.object(run_out, "weighted_rate", return_value=1 / 3600) as pace):
            self.assertEqual(self.cli()[0], 0)
        pace.assert_called_once()
        started = refusal_watch._load_state().get("auto_move_holds", {})[entry.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(started.duration, next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)))
        self.assertEqual((started.account, started.at, started.move_id, started.percent),
                         ("claude1", NOW, accounts.NoGroupTransition(), 1))
        for elapsed in (timedelta(minutes=29, seconds=59), timedelta(minutes=30)):
            WatchTime.at = NOW + elapsed
            self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            # Each CLI pass reloads the saved state, as a restarted runner does.
            with patch.object(run_out, "weighted_rate", side_effect=AssertionError("started hold recalculated pace")):
                code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"],
                             "claude1" if elapsed.total_seconds() < 1800 else "claude2")
            if elapsed.total_seconds() < 1800:
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {})[entry.id], started)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_half_point_without_positive_pace_persists_the_forty_minute_default(self) -> None:
        for rate in (None, 0):
            with self.subTest(rate=rate):
                WatchTime.at = NOW
                path = self.groups()
                entry = next_moves.ClaudeGroupMove(f"default-half-{rate}", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
                self.next_entries(entry)
                self.reading(SOURCE_LOGIN, 1)
                with (patch.object(run_out, "READINGS_LOG", self.home / "missing-pace"),
                      patch.object(run_out, "weighted_rate", return_value=rate)):
                    self.assertEqual(self.cli()[0], 0)
                held = refusal_watch._load_state().get("auto_move_holds", {})[entry.id]  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(held.duration, next_moves.ResolvedHold(40, next_moves.NoUsablePace()))
                for minutes in (39, 40):
                    WatchTime.at = NOW + timedelta(minutes=minutes)
                    self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
                    self.reading("main@example.invalid", 80, at=WatchTime.at)
                    with patch.object(run_out, "weighted_rate", side_effect=AssertionError("default changed after start")):
                        self.assertEqual(self.cli()[0], 0)
                    self.assertEqual(stored_group_labels(path)["units"], "claude1" if minutes == 39 else "claude2")

    def test_legacy_fixed_hold_decodes_explicitly_and_elapses_from_its_original_start(self) -> None:
        path = self.groups()
        entry = self.hold_rule("legacy-fixed", minutes=10)
        self.next_entries(entry)
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        _ = state_file.write_text(json.dumps({"sessions": {}, "notices": {}, "auto_move_holds": {
            entry.id: {"account": "claude1", "at": (NOW - timedelta(minutes=9)).isoformat(),
                       "move_id": "", "percent": 1}}}))
        held = refusal_watch._load_state().get("auto_move_holds", {})[entry.id]  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(held.duration, refusal_watch.LegacyHoldDuration)
        self.reading(SOURCE_LOGIN, 1)
        self.assertEqual(self.cli()[0], 0)
        held = refusal_watch._load_state().get("auto_move_holds", {})[entry.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(held.duration, next_moves.ResolvedHold(10, next_moves.FixedHold(10)))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        WatchTime.at = NOW + timedelta(minutes=1)
        self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
        self.reading("main@example.invalid", 80, at=WatchTime.at)
        self.assertEqual(self.cli()[0], 0)
        self.assertEqual(stored_group_labels(path)["units"], "claude2")

    def test_source_recovery_and_head_reorder_drop_a_half_point_hold(self) -> None:
        _ = self.groups()
        head = next_moves.ClaudeGroupMove("half-head", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        later = self.hold_rule("later")
        self.next_entries(head, later)
        self.reading(SOURCE_LOGIN, 1)
        with (patch.object(run_out, "READINGS_LOG", self.home / "missing-pace"),
              patch.object(run_out, "weighted_rate", return_value=1 / 3600)):
            self.assertEqual(self.cli()[0], 0)
            self.reading(SOURCE_LOGIN, 2)
            self.assertEqual(self.cli()[0], 0)
            self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
            self.reading(SOURCE_LOGIN, 1)
            self.assertEqual(self.cli()[0], 0)
        self.assertIn(head.id, refusal_watch._load_state().get("auto_move_holds", {}))  # pyright: ignore[reportPrivateUsage]
        next_moves.move_next_move("units", next_moves.GroupPosition(2), next_moves.GroupPosition(1))
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(next_moves.read_next_moves(), (later, head))

    def test_hold_mode_update_clears_only_that_entry_and_restarts_its_elapsed_hold(self) -> None:
        _ = self.groups()
        head, later = self.hold_rule("elapsed-head"), self.hold_rule("later")
        self.next_entries(head, later)
        old = refusal_watch.HoldStarted("claude1", NOW - timedelta(minutes=50), accounts.NoGroupTransition(), 1)
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={head.id: old, later.id: old}))
        _ = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.FixedHold(10))
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}),  # pyright: ignore[reportPrivateUsage]
                         {head.id: old, later.id: old})
        reply = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.HalfPointHold())
        self.assertIn("units 1: at 1% → claude2, hold half a point", reply)
        changed, unchanged = next_moves.read_next_moves()
        self.assertEqual((changed.id, changed.added_at, unchanged), (head.id, head.added_at, later))
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {later.id: old})  # pyright: ignore[reportPrivateUsage]
        self.reading(SOURCE_LOGIN, 1)
        with (patch.object(run_out, "READINGS_LOG", self.home / "missing-pace"),
              patch.object(run_out, "weighted_rate", return_value=1 / 3600)):
            self.assertEqual(self.cli()[0], 0)
        restarted = refusal_watch._load_state().get("auto_move_holds", {})[head.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(restarted.at, NOW)
        self.assertEqual(restarted.duration, next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)))
        self.assertEqual(next_moves.read_next_moves()[0], changed)

    def test_expected_hold_update_refuses_a_consumed_head_locally_and_through_the_mac(self) -> None:
        _ = self.groups()
        first, second = self.hold_rule("consumed"), self.hold_rule("survivor", target="claude3")
        for remote_client in (False, True):
            with self.subTest(remote_client=remote_client):
                self.next_entries(first, second)
                # The list is read before the runner consumes the head.
                self.assertIn("units 1: at 1% → claude2", next_moves.list_next_moves())
                next_moves.remove_next_move("units", next_moves.GroupPosition(1))
                held = refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1)
                refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
                    auto_move_holds={second.id: held}, auto_move_notices={second.id: "delivered"}))
                before_log = next_moves.next_moves_file().read_bytes()
                before_state = (self.home / ".local/state/accounts/refusal_watch.json").read_bytes()

                def transport(arguments: tuple[str, ...]) -> str:
                    self.assertEqual(arguments, ("next", "hold", "units", "1", "half-point", "--expect", "1.0", "claude2"))
                    with patch.object(role_map, "is_role_map_host", return_value=True):
                        output, error = io.StringIO(), io.StringIO()
                        with redirect_stdout(output), redirect_stderr(error):
                            code = accounts.main(list(arguments))
                    self.assertNotEqual(code, 0)
                    self.assertEqual(output.getvalue(), "")
                    raise role_map.RemoteCommandRefused(error.getvalue().strip())

                with (patch.object(role_map, "is_role_map_host", return_value=not remote_client),
                      patch.object(role_map, "ssh_role_map", side_effect=transport)):
                    with self.assertRaisesRegex(next_moves.NextMoveError,
                            "next: units 1 is now at 1% → claude3; nothing changed"):
                        _ = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1),
                            next_moves.HalfPointHold(), next_moves.ExpectedNextMove(1, "claude2"))
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before_log)
                self.assertEqual((self.home / ".local/state/accounts/refusal_watch.json").read_bytes(), before_state)

    def test_hold_update_save_failure_preserves_log_and_log_failure_only_restarts_the_hold(self) -> None:
        _ = self.groups()
        head, later = self.hold_rule("elapsed-head"), self.hold_rule("later")
        self.next_entries(head, later)
        old = refusal_watch.HoldStarted("claude1", NOW - timedelta(minutes=50), accounts.NoGroupTransition(), 1)
        original = refusal_watch.WatchState(sessions={}, notices={}, auto_move_holds={head.id: old, later.id: old})
        refusal_watch._save_state(original)  # pyright: ignore[reportPrivateUsage]
        before = next_moves.next_moves_file().read_bytes()
        with patch.object(refusal_watch, "_save_state", side_effect=OSError("fixture cleanup save failed")):
            with self.assertRaisesRegex(next_moves.NextMoveError, "cleanup save failed"):
                _ = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.HalfPointHold())
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(refusal_watch._load_state(), original)  # pyright: ignore[reportPrivateUsage]
        with patch.object(next_moves, "write_next_moves", side_effect=OSError("fixture log write failed")):
            with self.assertRaisesRegex(next_moves.NextMoveError, "log write failed"):
                _ = next_moves.update_next_move_hold("units", next_moves.GroupPosition(1), next_moves.HalfPointHold())
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {later.id: old})  # pyright: ignore[reportPrivateUsage]
        self.reading(SOURCE_LOGIN, 1)
        self.assertEqual(self.cli()[0], 0)
        restarted = refusal_watch._load_state().get("auto_move_holds", {})[head.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(restarted.at, NOW)
        self.assertEqual(restarted.duration, next_moves.ResolvedHold(10, next_moves.FixedHold(10)))

    def test_positional_head_changes_clear_the_displaced_hold_and_both_notices_under_the_watch_lock(self) -> None:
        _ = self.groups()
        watch_lock = refusal_watch._watch_lock  # pyright: ignore[reportPrivateUsage]
        save_state = refusal_watch._save_state  # pyright: ignore[reportPrivateUsage]
        write_log = next_moves.write_next_moves
        log_lock = next_moves.next_moves_lock
        quota_state = quota_alert.state_file
        locked = False

        @contextmanager
        def tracked_watch_lock() -> Generator[None]:
            nonlocal locked
            with watch_lock():
                self.assertFalse(locked)
                locked = True
                try:
                    yield
                finally:
                    locked = False

        @contextmanager
        def tracked_log_lock() -> Generator[None]:
            self.assertTrue(locked, "log lock acquired before the watch lock")
            with log_lock():
                yield

        @contextmanager
        def tracked_quota_state() -> Generator[quota_alert.State]:
            self.assertTrue(locked, "workers notice cleared outside the watch lock")
            with quota_state() as state:
                yield state

        def checked_write(entries: tuple[next_moves.NextMove, ...]) -> None:
            self.assertTrue(locked, "log changed outside the watch lock")
            write_log(entries)

        def checked_save(state: refusal_watch.WatchState) -> None:
            self.assertTrue(locked, "head state cleared outside the watch lock")
            save_state(state)

        groups: tuple[next_moves.NextMoveGroup, ...] = ("units", "showrunners", "workers")
        for group in groups:
            for edit in ("move", "remove", "insert"):
                with self.subTest(group=group, edit=edit):
                    if group == "workers":
                        head: next_moves.NextMove = next_moves.WorkersMove("old-worker-head", 1,
                            next_moves.WorkersToClaude(), NOW)
                        later: next_moves.NextMove = next_moves.WorkersMove("next-worker-head", 1,
                            next_moves.WorkersToClaude(), NOW)
                    else:
                        head = self.low_rule("old-head", group=group)
                        later = self.low_rule("next-head", group=group, target="claude3")
                    other = self.low_rule("unrelated-head", group="showrunners" if group == "units" else "units")
                    self.next_entries(head, other, later)
                    hold = refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1)
                    save_state(refusal_watch.WatchState(sessions={}, notices={},
                        auto_move_holds={head.id: hold, other.id: hold},
                        auto_move_notices={head.id: "delivered", other.id: "keep"}))
                    with quota_alert.state_file() as state:
                        state["workers_move_notices"] = {head.id: "delivered", other.id: "keep"}
                    args = (["move", group, "2", "1"] if edit == "move" else
                            ["remove", group, "1"] if edit == "remove" else
                            ["add", group, "1", "claude" if group == "workers" else "claude2", "--at", "1"])
                    with (patch.object(refusal_watch, "_watch_lock", side_effect=tracked_watch_lock),
                          patch.object(refusal_watch, "_save_state", side_effect=checked_save),
                          patch.object(next_moves, "next_moves_lock", side_effect=tracked_log_lock),
                          patch.object(quota_alert, "state_file", side_effect=tracked_quota_state),
                          patch.object(next_moves, "write_next_moves", side_effect=checked_write),
                          redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                        self.assertEqual(accounts.main(["next", *args]), 0)
                    saved = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(saved.get("auto_move_holds"), {other.id: hold})
                    self.assertEqual(saved.get("auto_move_notices"), {other.id: "keep"})
                    with quota_alert.state_file() as state:
                        self.assertEqual(state.get("workers_move_notices"), {other.id: "keep"})
                    self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group == other.group],
                                     [other])

    def test_positional_edits_that_leave_the_head_keep_its_hold_and_notice_keys(self) -> None:
        _ = self.groups()
        head, second, third = self.hold_rule("head"), self.hold_rule("second"), self.hold_rule("third")
        for args in (["move", "units", "2", "3"], ["remove", "units", "2"],
                     ["add", "units", "1", "claude2", "--at", "2"], ["move", "units", "1", "1"]):
            with self.subTest(args=args):
                self.next_entries(head, second, third)
                original = refusal_watch.WatchState(sessions={}, notices={},
                    auto_move_holds={head.id: refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1)},
                    auto_move_notices={head.id: "keep"})
                refusal_watch._save_state(original)  # pyright: ignore[reportPrivateUsage]
                with quota_alert.state_file() as state:
                    state["workers_move_notices"] = {head.id: "keep"}
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(accounts.main(["next", *args]), 0)
                self.assertEqual(refusal_watch._load_state(), original)  # pyright: ignore[reportPrivateUsage]
                with quota_alert.state_file() as state:
                    self.assertEqual(state.get("workers_move_notices"), {head.id: "keep"})

    def test_failed_head_cleanup_keeps_the_log_unchanged_before_a_fresh_hold_can_start(self) -> None:
        _ = self.groups()
        head, later = self.hold_rule("elapsed-head"), self.hold_rule("successor")
        self.reading(SOURCE_LOGIN, 1)
        for failure in ("watch save", "workers notice save"):
            with self.subTest(failure=failure):
                self.next_entries(head, later)
                original = refusal_watch.WatchState(sessions={}, notices={},
                    auto_move_holds={head.id: refusal_watch.HoldStarted(
                        "claude1", NOW - timedelta(minutes=20), accounts.NoGroupTransition(), 1)},
                    auto_move_notices={head.id: "delivered"})
                refusal_watch._save_state(original)  # pyright: ignore[reportPrivateUsage]
                with quota_alert.state_file() as state:
                    state["workers_move_notices"] = {head.id: "delivered"}
                before = next_moves.next_moves_file().read_bytes()

                @contextmanager
                def failed_workers_notice_save() -> Generator[quota_alert.State]:
                    state = quota_alert.State(episodes={}, fallback=quota_alert.NoCodexQuotaFallback(),
                        workers_move_notices={head.id: "delivered"})
                    yield state
                    raise OSError("fixture workers notice save failed")

                with (patch.object(refusal_watch, "_save_state", side_effect=OSError("fixture watch save failed"))
                      if failure == "watch save" else
                      patch.object(quota_alert, "state_file", side_effect=failed_workers_notice_save)):
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                        code = accounts.main(["next", "move", "units", "1", "2"])
                self.assertNotEqual(code, 0)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("save failed", error.getvalue())
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
                with quota_alert.state_file() as state:
                    self.assertEqual(state.get("workers_move_notices"), {head.id: "delivered"})
                if failure == "watch save":
                    self.assertEqual(refusal_watch._load_state(), original)  # pyright: ignore[reportPrivateUsage]
                next_moves.move_next_move("units", next_moves.GroupPosition(1), next_moves.GroupPosition(2))
                next_moves.move_next_move("units", next_moves.GroupPosition(2), next_moves.GroupPosition(1))
                self.assertEqual(next_moves.read_next_moves(), (head, later))
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
                WatchTime.at = NOW + timedelta(minutes=2)
                _ = self.cli()
                hold = refusal_watch._load_state().get("auto_move_holds", {})[head.id]  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(hold.at, WatchTime.at)
                self.assertEqual(next_moves.read_next_moves(), (head, later))
                self.assertEqual(self.moves.calls, [])
                WatchTime.at = NOW

    def test_failed_log_write_leaves_displaced_head_state_cleared_so_the_hold_restarts(self) -> None:
        _ = self.groups()
        head, later = self.hold_rule("elapsed-head"), self.hold_rule("successor")
        self.next_entries(head, later)
        self.reading(SOURCE_LOGIN, 1)
        refusal_watch._save_state(refusal_watch.WatchState(sessions={}, notices={},  # pyright: ignore[reportPrivateUsage]
            auto_move_holds={head.id: refusal_watch.HoldStarted(
                "claude1", NOW - timedelta(minutes=20), accounts.NoGroupTransition(), 1)},
            auto_move_notices={head.id: "delivered"}))
        with quota_alert.state_file() as state:
            state["workers_move_notices"] = {head.id: "delivered"}
        before = next_moves.next_moves_file().read_bytes()
        with patch.object(next_moves, "write_next_moves", side_effect=OSError("fixture log write failed")):
            with self.assertRaisesRegex(next_moves.NextMoveError, "log write failed"):
                next_moves.move_next_move("units", next_moves.GroupPosition(1), next_moves.GroupPosition(2))
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        state = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(state.get("auto_move_holds", {}), {})
        self.assertEqual(state.get("auto_move_notices", {}), {})
        with quota_alert.state_file() as quota_state:
            self.assertEqual(quota_state.get("workers_move_notices", {}), {})
        _ = self.cli()
        hold = refusal_watch._load_state().get("auto_move_holds", {})[head.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(hold.at, NOW)
        self.assertEqual(self.moves.calls, [])

    def test_demoting_and_promoting_a_head_before_the_next_pass_restarts_its_hold(self) -> None:
        _ = self.groups()
        head, second = self.hold_rule("original-head"), self.hold_rule("successor")
        self.next_entries(head, second)
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        first_hold = refusal_watch._load_state().get("auto_move_holds", {})[head.id]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(first_hold.at, NOW)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(accounts.main(["next", "move", "units", "1", "2"]), 0)
            self.assertEqual(accounts.main(["next", "move", "units", "2", "1"]), 0)
        self.assertEqual(next_moves.read_next_moves(), (head, second))
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(holds), {head.id})
        self.assertEqual(holds[head.id].at, WatchTime.at)
        self.assertEqual(self.moves.calls, [])

    def test_stale_due_head_blocks_the_fresh_second_entry_and_reports_once_per_reason(self) -> None:
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "four@example.invalid"}}))
        _ = (self.home / "notes" / note_name("claude4")).write_text("---\nlogin: four@example.invalid\n---\n")
        path = self.groups(showrunners="claude1")
        first = next_moves.ClaudeGroupMove("stale-head", "showrunners", 1, 40, "claude4", NOW)
        second = next_moves.ClaudeGroupMove("fresh-second", "showrunners", 1, 40, "claude2", NOW)
        self.next_entries(first, second)
        self.reading(SOURCE_LOGIN, 1)
        last_read = NOW - timedelta(hours=12, minutes=17)
        self.reading("four@example.invalid", 80, at=last_read)
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        hold = {"account": "claude1", "at": (NOW - timedelta(minutes=40)).isoformat(),
                "move_id": "", "percent": 1}
        _ = state_file.write_text(json.dumps({"sessions": {}, "notices": {},
            "auto_move_holds": {first.id: hold, second.id: hold}}))
        with patch.object(accounts, "prepared_account", side_effect=AssertionError("blocked head prepared a target")):
            code, output, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["showrunners"], "claude1")
            self.assertEqual(next_moves.read_next_moves(), (first, second))
            self.assertEqual(set(refusal_watch._load_state().get("auto_move_holds", {})), {first.id})  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(self.moves.calls, [])
            self.assertFalse(accounts.roles_history_file().exists())
            self.assertEqual(len(self.head_deliveries), 1)
            job, = self.head_deliveries[0]
            self.assertEqual(job.recipient, "fixture-showrunner")
            self.assertNotIn(first.id, job.text)
            for text in ("showrunners 1", "claude4", "due", "19:43"):
                self.assertIn(text, job.text)
            self.assertIn(job.text, output)
            _ = self.cli()
            self.assertEqual(len(self.head_deliveries), 1)
            saved = usage_store.load()
            del saved[usage_store.key("Claude", "four@example.invalid")]
            usage_store.write(saved)
            _ = self.cli()
            self.assertEqual(len(self.head_deliveries), 2)
            self.assertIn("no reading", self.head_deliveries[-1][0].text)
            _ = self.cli()
            self.assertEqual(len(self.head_deliveries), 2)
        self.assertEqual(next_moves.read_next_moves(), (first, second))

    def test_unchanged_stale_target_notice_survives_a_source_recovery_and_relapse(self) -> None:
        _ = self.groups()
        entry = self.low_rule(target="claude3")
        self.next_entries(entry)
        self.reading(TARGET_LOGIN, 80, at=NOW - timedelta(minutes=11))
        for minute, left in ((0, 1), (2, 50), (4, 1)):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, left, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(self.head_deliveries), 1)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual(self.moves.calls, [])

    def test_target_allowance_changes_below_the_same_head_threshold_do_not_repeat_its_notice(self) -> None:
        _ = self.groups()
        entry = next_moves.ClaudeGroupMove("low-target", "units", 20, 0, "claude3", NOW)
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 1)
        for left in (10, 9):
            self.reading(TARGET_LOGIN, left)
            code, output, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertIn(f"{left}% left, at or below 20%", output)
        self.assertEqual(len(self.head_deliveries), 1)
        self.assertIn("10% left", self.head_deliveries[0][0].text)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual(self.moves.calls, [])

    def test_ready_next_move_reads_new_unit_members_after_report_snapshot(self) -> None:
        if TYPE_CHECKING:
            from scripts.herdr import herdr
        else:
            herdr = import_module("herdr")

        _ = self.groups()
        _ = self.owner("new-unit", host="unit")
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 1)
        fresh = herdr.HerdrProcess(1000, herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "new-unit"}, "claude", str(self.home))
        def observed(login: str | None = None, *,
                     registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            return self.listing(login, registrations=registrations) if herdr.herdr_processes() else []
        processes: list[herdr.HerdrProcess] = []
        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)),
              patch.object(inventory, "listing", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            processes.append(fresh)
            _ = refusal_watch.run_auto_moves(NOW)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual("new-unit", self.moves.calls[0].session_id)

    def test_committed_group_completion_sees_unit_started_after_report_snapshot(self) -> None:
        if TYPE_CHECKING:
            from scripts.herdr import herdr
        else:
            herdr = import_module("herdr")

        _ = self.groups(units="claude3")
        processes: list[herdr.HerdrProcess] = []

        def observed(login: str | None = None, *,
                     registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            return self.listing(login, registrations=registrations) if herdr.herdr_processes() else []

        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)) as scans,
              patch.object(inventory, "listing", side_effect=observed),
              refusal_watch.session_observation(), herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            owner = self.owner("new-unit", host="unit")
            processes.append(herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
                {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "new-unit"}, "claude", str(self.home)))
            _ = refusal_watch.run(NOW)
        self.assertGreater(scans.call_count, 1)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual("new-unit", self.moves.calls[0].session_id)
        self.assertEqual(self.moves.calls[0].target, "claude3")

    def test_idle_completion_and_later_watchdog_report_share_one_scan(self) -> None:
        if TYPE_CHECKING:
            from scripts.herdr import herdr
        else:
            herdr = import_module("herdr")

        _ = self.groups(units="claude1")
        _ = self.owner("unit", host="unit")

        def observed(login: str | None = None, *,
                     registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            _ = herdr.herdr_processes()
            return self.listing(login, registrations=registrations)

        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]) as scans,
              patch.object(inventory, "listing", side_effect=observed),
              refusal_watch.session_observation(), herdr.process_snapshot()):
            _ = herdr.herdr_processes()
            _ = herdr.herdr_processes()
            self.assertEqual(scans.call_count, 1)
            _ = refusal_watch.run(NOW)
            _ = herdr.herdr_processes()  # The watchdog report follows completion.
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.moves.calls, [])

    def test_changed_registered_process_identity_confirms_completion_fresh(self) -> None:
        if TYPE_CHECKING:
            from scripts.herdr import herdr
        else:
            herdr = import_module("herdr")
        _ = self.groups(units="claude1")
        _ = self.owner("unit", host="unit")
        def observed(login: str | None = None, *,
                     registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            _ = herdr.herdr_processes()
            return self.listing(login, registrations=registrations)
        for changed in ("pid", "procStart"):
            with (self.subTest(identity_field=changed),
                  patch.object(herdr, "_scan_herdr_processes", return_value=[]) as scans,
                  patch.object(inventory, "listing", side_effect=observed),
                  refusal_watch.session_observation(), herdr.process_snapshot()):
                _ = herdr.herdr_processes()
                if changed == "pid":
                    self.live[0]["pid"] += 1
                else:
                    self.live[0]["procStart"] = "new-process-start"
                _ = refusal_watch.run(NOW)
                self.assertEqual(scans.call_count, 2)
        self.assertEqual(self.moves.calls, [])

    def test_refusal_placement_sees_unit_pane_started_after_report_snapshot(self) -> None:
        if TYPE_CHECKING:
            from scripts.herdr import herdr
        else:
            herdr = import_module("herdr")

        path = self.groups()
        owner = self.owner("new-unit", host="unit")
        _ = self.transcript("new-unit", "weekly limit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "new-unit"}, "claude", str(self.home))
        original_inventory = self.inventory_for
        def observed(login: str, only: frozenset[str] = NO_SESSION_FILTER, *,
                     refresh_desktops: bool = True) -> inventory.Inventory:
            rows = herdr.herdr_processes()
            found = original_inventory(login, only, refresh_desktops=refresh_desktops)
            if not rows:
                # The report predates the new marked pane, so this is top-level.
                ungrouped: list[inventory.Session] = []
                for item in found["sessions"]:
                    fields: dict[str, object] = dict(item)
                    fields.update(kind="top-level", host=inventory.TerminalHost(kind="terminal"))
                    ungrouped.append(cast(inventory.TopLevelSession, cast(object, fields)))
                found["sessions"] = ungrouped
            return found
        processes: list[herdr.HerdrProcess] = []
        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)),
              patch.object(inventory, "inventory", side_effect=observed),
              patch.object(accounts, "placement", wraps=accounts.placement) as placement,
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            processes.append(fresh)
            _ = refusal_watch.run(NOW)
        self.assertTrue(placement.called)
        self.assertEqual(placement.call_args.args[0], "units")
        self.assertEqual(stored_group_labels(path)["units"], "claude3")

    def test_pending_head_preserves_a_waited_sessions_continuation_on_its_current_account(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.transcript("refused-unit", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        self.assertEqual([row["kind"] for row in moves.read_moves()], ["waited"])
        entry = self.hold_rule()
        self.next_entries(entry)
        WatchTime.at = reset
        with (patch.object(accounts, "placement", side_effect=AssertionError("wait chose an account")),
              patch.object(accounts, "pick", side_effect=AssertionError("wait picked an account")),
              patch.object(accounts, "set_group_account", side_effect=AssertionError("wait selected an account")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
            _ = self.cli()
            _ = self.cli()
        delivery.assert_called_once_with("session:refused-unit", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-refused-unit-refusal-one")
        self.assertEqual([row["kind"] for row in moves.read_moves()], ["waited", "wait ended"])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(self.notifications, [])

    def test_pending_head_waits_for_a_known_refusal_reset_and_continues_without_choosing_an_account(self) -> None:
        for window in ("five_hour", "weekly"):
            with self.subTest(window=window):
                path = self.groups()
                identity = "refused-" + window
                _ = self.owner(identity, host="unit")
                _ = self.transcript(identity, "You've hit your session limit" if window == "five_hour" else "weekly limit")
                entry = self.hold_rule()
                self.next_entries(entry)
                reset = NOW + timedelta(hours=3)
                self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset, reset=reset)
                with (patch.object(accounts, "placement", side_effect=AssertionError("pending head placed an account")),
                      patch.object(accounts, "pick", side_effect=AssertionError("pending head picked an account")),
                      patch.object(accounts, "set_group_account", side_effect=AssertionError("pending head selected an account")),
                      patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
                    WatchTime.at = NOW
                    _ = self.cli()
                    _ = self.cli()
                    waits = [row for row in moves.read_moves() if row["kind"] == "waited" and row["subject"].startswith(identity + ":")]
                    self.assertEqual(len(waits), 1)
                    self.assertEqual(waits[0]["window"], window)
                    self.assertEqual(waits[0]["reset"], reset.isoformat())
                    delivery.assert_not_called()
                    WatchTime.at = reset
                    _ = self.cli()
                    _ = self.cli()
                    delivery.assert_called_once_with("session:" + identity, "Quota wait ended", "Continue where you stopped.",
                        key="quota-continue-" + identity + "-refusal-one")
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertEqual(next_moves.read_next_moves(), (entry,))
                self.assertEqual(self.moves.calls, [])
                self.assertEqual(self.notifications, [])

    def test_group_head_targeting_current_account_blocks_successors_and_reports_once(self) -> None:
        path = self.groups()
        head = self.low_rule("current-head", target="claude1")
        later = self.hold_rule("later-target", target="claude2", minutes=10)
        self.next_entries(head, later)
        self.reading(SOURCE_LOGIN, 1)
        with (patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepare,
              patch.object(accounts, "set_group_account", wraps=accounts.set_group_account) as select,
              patch.object(refusal_watch, "notify_blocked_next_move", wraps=quota_alert.notify_blocked_next_move) as notice):
            for at in (NOW, NOW + timedelta(minutes=2)):
                WatchTime.at = at
                code, output, error = self.cli()
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertEqual(next_moves.read_next_moves(), (head, later))
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(len(self.head_deliveries), 1)
                job, = self.head_deliveries[0]
                self.assertEqual(job.recipient, "fixture-showrunner")
                self.assertIn(job.text, output)
            prepare.assert_not_called()
            select.assert_not_called()
            notice.assert_called_once_with("units", "claude1", head.id,
                                           quota_alert.TargetIsCurrentAccount(), NOW)
        self.assertNotIn(head.id, self.head_deliveries[0][0].text)
        for text in ("units 1", "claude1", "due", "already the current account"):
            self.assertIn(text, self.head_deliveries[0][0].text)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(self.notifications, [])
        self.assertFalse(accounts.roles_history_file().exists())

    def test_second_entry_starts_its_own_hold_on_the_pass_after_the_head_is_consumed(self) -> None:
        path = self.groups()
        first, second = self.hold_rule("first", target="claude3", minutes=5), self.hold_rule("second", minutes=5)
        self.next_entries(first, second)
        self.reading(SOURCE_LOGIN, 1)
        self.reading(TARGET_LOGIN, 80)
        _ = self.cli()
        holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(holds), {first.id})
        self.assertEqual(holds[first.id].at, NOW)
        WatchTime.at = NOW + timedelta(minutes=5)
        self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
        self.reading(TARGET_LOGIN, 80, at=WatchTime.at)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual(next_moves.read_next_moves(), (second,))
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
        WatchTime.at = NOW + timedelta(minutes=6)
        self.reading(TARGET_LOGIN, 1, at=WatchTime.at)
        _ = self.cli()
        holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(holds), {second.id})
        self.assertEqual(holds[second.id].at, WatchTime.at)
        self.assertEqual(holds[second.id].account, "claude3")
        WatchTime.at = NOW + timedelta(minutes=10)
        self.reading(TARGET_LOGIN, 1, at=WatchTime.at)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        WatchTime.at = NOW + timedelta(minutes=11)
        self.reading(TARGET_LOGIN, 1, at=WatchTime.at)
        self.reading("main@example.invalid", 80, at=WatchTime.at)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_pending_head_allows_off_target_members_to_complete_an_existing_group_choice(self) -> None:
        path = self.groups()
        _ = self.owner("off-target", host="unit", label="claude3")
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        with patch.object(accounts, "placement", side_effect=AssertionError("reconciliation selected a fallback")):
            code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(self.moves.calls[0].target, "claude1")

    def test_due_blocked_head_dry_run_prints_its_notice_without_delivery_or_state_changes(self) -> None:
        _ = self.groups()
        self.next_entries(self.low_rule(target="claude3"))
        self.reading(SOURCE_LOGIN, 1)
        self.reading(TARGET_LOGIN, 80, at=NOW - timedelta(minutes=11))
        before = self.snapshot()
        code, output, error = self.cli("--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("units-low", output)
        for text in ("units 1", "claude3", "due", "07:49"):
            self.assertIn(text, output)
        self.assertEqual(self.head_deliveries, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.moves.calls, [])

    def test_removing_a_blocked_entry_clears_its_notice_deduplication(self) -> None:
        _ = self.groups()
        entry = self.low_rule(target="claude3")
        self.next_entries(entry)
        self.reading(TARGET_LOGIN, 80, at=NOW - timedelta(minutes=11))
        _ = self.cli()
        self.assertEqual(len(self.head_deliveries), 1)
        next_moves.remove_next_move("units", next_moves.GroupPosition(1))
        state = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn(entry.id, state.get("auto_move_notices", {}))
        self.next_entries(entry)
        _ = self.cli()
        self.assertEqual(len(self.head_deliveries), 2)

    def test_failed_group_blocked_head_delivery_retries_until_it_is_delivered(self) -> None:
        _ = self.groups()
        head = self.low_rule(target="claude1")
        self.next_entries(head)
        with (patch.object(quota_alert, "deliver_and_record", wraps=deliver_notice_jobs) as delivery,
              patch.object(quota_alert, "relay", side_effect=["fixture recipient unavailable", None]) as relay):
            for minute in (0, 2, 4):
                WatchTime.at = NOW + timedelta(minutes=minute)
                _ = self.cli()
                state = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(head.id in state.get("auto_move_notices", {}), minute > 0)
        self.assertEqual(delivery.call_count, 2)
        self.assertEqual(relay.call_count, 2)
        self.assertEqual(relay.call_args_list[0], relay.call_args_list[1])
        self.assertEqual(self.moves.calls, [])

    def test_failed_no_room_delivery_retries_until_it_is_delivered(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.transcript("unit")
        for login in ("main@example.invalid", TARGET_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        before = path.read_bytes()
        with (patch.object(refusal_watch, "_notify_no_room", side_effect=send_no_room_notice),
              patch.object(quota_alert, "deliver_and_record", wraps=deliver_notice_jobs) as delivery,
              patch.object(quota_alert, "relay", side_effect=["fixture recipient unavailable", None]) as relay):
            for minute in (0, 2, 4):
                WatchTime.at = NOW + timedelta(minutes=minute)
                _ = self.cli()
                state = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(SOURCE_LOGIN in state["notices"], minute > 0)
        self.assertEqual(delivery.call_count, 2)
        self.assertEqual(relay.call_count, 2)
        self.assertEqual(relay.call_args_list[0], relay.call_args_list[1])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room"])


    def test_due_head_reports_a_target_without_a_readable_login_once(self) -> None:
        path = self.groups()
        _ = (accounts.account_dir("claude3") / ".claude.json").write_text("{}")
        entry = self.low_rule(target="claude3")
        self.next_entries(entry)
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(len(self.head_deliveries), 1)
        self.assertIn("claude3", self.head_deliveries[0][0].text)
        self.assertIn("login", self.head_deliveries[0][0].text)
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(self.moves.calls, [])

    def test_consumed_entry_does_not_move_a_group_again_after_an_operator_returns_it(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), ())
        _ = accounts.set_group_account("units", "claude1", why="operator return", now=NOW)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([entry["why"] for entry in history], ["auto: units-low", "operator return"])

    def test_workers_entries_are_never_claude_group_rules(self) -> None:
        entry = self.low_rule()
        self.next_entries(entry)
        worker = next_moves.add_next_move("workers", 1, "claude")
        rules = refusal_watch.read_auto_move_rules()
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        assert isinstance(rule, refusal_watch.AutoMoveRule)
        self.assertEqual((rule.entry_id, rule.group), (entry.id, "units"))
        self.assertEqual(next_moves.read_next_moves(), (entry, worker))

    def test_torn_last_role_history_line_does_not_block_a_runnable_group_entry(self) -> None:
        for suffix in (b'{"cause":', b'\xff'):
            with self.subTest(suffix=suffix):
                path = self.groups()
                entry = self.low_rule(f"units-torn-{suffix.hex()}")
                self.next_entries(entry)
                self.reading(SOURCE_LOGIN, 1)
                history = accounts.roles_history_file()
                history.parent.mkdir(parents=True, exist_ok=True)
                _ = history.write_bytes(b'{}\n' + suffix)
                code, output, error = self.cli()
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(stored_group_labels(path)["units"], "claude2")
                self.assertEqual(next_moves.read_next_moves(), ())
                lines = [line for line in output.splitlines() if "torn final line" in line]
                self.assertEqual(len(lines), 1)
                self.assertIn("roles.jsonl", lines[0])

    def test_failed_consumption_cannot_replay_a_recorded_move_after_an_operator_returns_the_group(self) -> None:
        path = self.groups()
        entry = self.low_rule()
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 1)
        with patch.object(next_moves, "write_next_moves", side_effect=OSError("injected log replacement failure")) as write:
            try:
                _ = self.cli()
            except OSError:
                pass
        write.assert_called_once()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        # Return before the watcher has a chance to reconcile the failed write.
        _ = accounts.set_group_account("units", "claude1", why="operator return", now=NOW)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(next_moves.read_next_moves(), ())
        _ = self.cli()
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([saved["why"] for saved in history], ["auto: units-low", "operator return"])

    def test_auto_move_relays_a_cleared_hold_once_to_showrunners(self) -> None:
        for released in (None, launch_permission.ReleasedHold(label="claude2", then="stop",
                requested_at=NOW.isoformat(), session_id="b1f5ca98-rest")):
            with self.subTest(released=released):
                _ = self.groups()
                self.next_entries(self.low_rule("cleared-hold" if released else "no-hold"))
                self.reading(SOURCE_LOGIN, 1)
                self.head_deliveries.clear()
                with patch.object(launch_permission, "launch_permission",
                        return_value=launch_permission.AllowedByShutdownState(released)):
                    code, _, error = self.cli()
                    _ = self.cli()
                self.assertEqual((code, error), (0, ""))
                notices = [job for jobs in self.head_deliveries for job in jobs
                           if "cleared a finished" in job.text]
                self.assertEqual(len(notices), int(released is not None))
                if notices:
                    self.assertEqual(notices[0].recipient, "fixture-showrunner")
                    self.assertIn("units move to claude2: cleared a finished stop hold on claude2", notices[0].text)
                    self.assertIn("session b1f5ca98", notices[0].text)

    def test_committed_auto_group_move_relays_its_cleared_hold(self) -> None:
        _ = self.groups()
        _ = self.owner("committed-unit", host="unit")
        self.reading(TARGET_LOGIN, 80)
        _ = accounts.set_group_account("units", "claude3", why="auto: committed", now=NOW, origin=accounts.AutoOrigin())
        released = launch_permission.ReleasedHold(label="claude3", then="stop",
            requested_at=NOW.isoformat(), session_id="b1f5ca98-rest")
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState(released)):
            code, _, error = self.cli()
            _ = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(self.moves.calls), 1)
        notices = [job for jobs in self.head_deliveries for job in jobs if "cleared a finished" in job.text]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].recipient, "fixture-showrunner")
        self.assertIn("units move to claude3: cleared a finished stop hold on claude3", notices[0].text)
        self.assertIn("session b1f5ca98", notices[0].text)
        self.assertEqual(self.moves.calls[0].why, "auto: committed")

    def test_refusal_group_move_relays_its_cleared_hold(self) -> None:
        _ = self.groups()
        owner = self.owner("refused-unit", host="unit")
        _ = self.transcript(owner["session_id"])
        self.reading(TARGET_LOGIN, 0)
        released = launch_permission.ReleasedHold(label="claude2", then="stop",
            requested_at=NOW.isoformat(), session_id=None)
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState(released)):
            code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(self.moves.calls), 1)
        notices = [job for jobs in self.head_deliveries for job in jobs if "cleared a finished" in job.text]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].recipient, "fixture-showrunner")
        self.assertIn("units move to claude2: cleared a finished stop hold on claude2", notices[0].text)

    def test_cleared_hold_retries_only_failed_recipients_after_the_move_is_consumed(self) -> None:
        _ = self.groups()
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 1)
        released = launch_permission.ReleasedHold(label="claude2", then="stop",
            requested_at=NOW.isoformat(), session_id="b1f5ca98-rest")
        deliveries = 0
        def attempt(jobs: list[quota_alert.Job], stamp: str) -> list[str]:
            nonlocal deliveries
            deliveries += 1
            if deliveries == 2:
                return [f"quota {job.kind} {job.name} -> {job.recipient}: not delivered: fixture failed" for job in jobs]
            return self.deliver_head_notice(jobs, stamp)
        with (patch.object(quota_alert, "recipients", return_value=["first-showrunner", "second-showrunner"]),
              patch.object(quota_alert, "deliver_and_record", side_effect=attempt) as deliver):
            with patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState(released)):
                code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(next_moves.read_next_moves(), ())
            path = self.home / ".local/state/accounts/pending_cleared_holds.json"
            pending = cast(dict[str, dict[str, object]], json.loads(path.read_text()))
            self.assertEqual([notice["recipients"] for notice in pending.values()], [["second-showrunner"]])
            _ = self.cli("--dry-run")
            self.assertEqual(deliver.call_count, 2)
            _ = self.cli()
            _ = self.cli()
        jobs = [cast(list[quota_alert.Job], call.args[0])[0] for call in deliver.call_args_list]
        self.assertEqual([job.recipient for job in jobs], ["first-showrunner", "second-showrunner", "second-showrunner"])
        self.assertEqual(jobs[1].text, jobs[2].text)
        self.assertTrue(jobs[1].name)
        self.assertEqual(jobs[1].name, jobs[2].name)
        self.assertEqual(json.loads(path.read_text()), {})

    def test_cleared_hold_queue_handoff_uses_real_delivery_and_enqueue_once(self) -> None:
        from scripts.message import send

        queued: list[send.QueuedMessage] = []
        commands: list[list[str]] = []
        deliveries: list[str] = []
        def deliver(jobs: list[quota_alert.Job], stamp: str) -> list[str]:
            result = deliver_notice_jobs(jobs, stamp)
            deliveries.extend(result)
            return result
        def store(_path: Path, contents: str) -> None:
            queued[:] = cast(list[send.QueuedMessage], [json.loads(line) for line in contents.splitlines()])
        def read_queue(_path: Path) -> list[send.QueuedMessage]:
            return list(queued)
        def relay(command: list[str], *, input: str, **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if len(commands) == 1:
                return subprocess.CompletedProcess(command, 3, "FAILED: fixture offline", "")
            recipient = command[command.index("--to") + 1]
            key = command[command.index("--key") + 1]
            send.enqueue(send.Message(recipient, "quota_alert", "", input, key), "fixture queued", NOW)
            return subprocess.CompletedProcess(command, 1, "QUEUED: fixture offline", "")

        notify = refusal_watch._notify_cleared_hold  # pyright: ignore[reportPrivateUsage]
        retry = refusal_watch._retry_cleared_hold_notices  # pyright: ignore[reportPrivateUsage]
        with (patch.object(quota_alert, "deliver_and_record", side_effect=deliver),
              patch.object(subprocess, "run", side_effect=relay),
              patch.object(send, "queue_for", return_value=self.home / "memory/queue"),
              patch.object(send, "locked", side_effect=nullcontext),
              patch.object(send, "read_queue", side_effect=read_queue),
              patch.object(send, "write_atomic", side_effect=store)):
            notify("units", "claude2", ("cleared a finished stop hold",), NOW)
            retry(NOW + timedelta(minutes=2))
            retry(NOW + timedelta(minutes=4))
        self.assertEqual(len(commands), 2, deliveries)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["text"], "units move to claude2: cleared a finished stop hold")
        self.assertTrue(queued[0]["key"])
        self.assertEqual(commands[0][commands[0].index("--key") + 1], queued[0]["key"])
        self.assertEqual(json.loads((self.home / ".local/state/accounts/pending_cleared_holds.json").read_text()), {})

    def test_released_hold_does_not_change_the_auto_id_used_after_interrupted_consumption(self) -> None:
        path = self.groups()
        head = self.low_rule()
        self.next_entries(head)
        self.reading(SOURCE_LOGIN, 1)
        released = launch_permission.ReleasedHold(label="claude2", then="stop",
            requested_at=NOW.isoformat(), session_id="b1f5ca98-rest")
        with (patch.object(launch_permission, "launch_permission",
                           return_value=launch_permission.AllowedByShutdownState(released)),
              patch.object(next_moves, "write_next_moves", side_effect=OSError("interrupted consumption"))):
            with self.assertRaises(OSError):
                _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (head,))
        _ = accounts.set_group_account("units", "claude1", why="operator return", now=NOW)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([item["why"] for item in history], ["auto: units-low", "operator return"])

    def test_failed_consumption_cannot_replay_a_recorded_move_when_role_history_becomes_malformed(self) -> None:
        path = self.groups()
        entry = self.low_rule()
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 1)
        with patch.object(next_moves, "write_next_moves", side_effect=OSError("injected log replacement failure")) as write:
            try:
                _ = self.cli()
            except OSError:
                pass
        write.assert_called_once()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        _ = accounts.set_group_account("units", "claude1", why="operator return", now=NOW)
        history = accounts.roles_history_file()
        recorded = history.read_text()
        malformed = recorded + "not JSON\n"
        _ = history.write_text(malformed)
        for _ in range(2):
            code, output, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(len([line for line in output.splitlines()
                                  if "cannot read recorded auto moves" in line]), 1)
            self.assertEqual(stored_group_labels(path)["units"], "claude1")
            self.assertEqual(next_moves.read_next_moves(), (entry,))
            self.assertEqual(history.read_text(), malformed)
        rows = [cast(dict[str, object], json.loads(line)) for line in recorded.splitlines()]
        self.assertEqual([row["why"] for row in rows], [f"auto: {entry.id}", "operator return"])
        # Once the journal is readable, reconcile consumption without replaying the move.
        _ = history.write_text(recorded)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(history.read_text(), recorded)

    def test_unreadable_role_history_reports_once_and_leaves_all_log_entries_untouched(self) -> None:
        read_bytes = Path.read_bytes
        for problem in ("permission", "malformed", "encoding"):
            with self.subTest(problem=problem):
                path = self.groups()
                entry = self.low_rule(f"units-{problem}")
                skipped = self.low_rule(f"runner-{problem}", group="showrunners", target="claude3")
                self.next_entries(entry, skipped)
                self.reading(SOURCE_LOGIN, 1)
                history = accounts.roles_history_file()
                # A valid prefix is insufficient evidence when the rest cannot be decoded.
                prefix = json.dumps({"cause": "auto", "group": "showrunners", "why": f"auto: {skipped.id}"}) + "\n"
                original = prefix.encode() + (b"not JSON\n" if problem == "malformed" else
                                              b"\xff\n" if problem == "encoding" else b"")
                _ = history.write_bytes(original)

                def read_with_failure(file: Path) -> bytes:
                    if file == history and problem == "permission":
                        raise PermissionError("fixture history unreadable")
                    return read_bytes(file)

                with patch.object(Path, "read_bytes", read_with_failure):
                    code, output, error = self.cli()
                    self.assertEqual((code, error), (0, ""))
                    self.assertEqual(len([line for line in output.splitlines()
                                          if "cannot read recorded auto moves" in line]), 1)
                    self.assertEqual(stored_group_labels(path)["units"], "claude1")
                    self.assertEqual(next_moves.read_next_moves(), (entry, skipped))
                self.assertEqual(history.read_bytes(), original)

    def test_legacy_reset_state_is_ignored_and_dropped_on_the_next_watch_write(self) -> None:
        path = self.groups()
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        _ = state_file.write_text(json.dumps({"sessions": {}, "notices": {}, "auto_moves": {
            "old-reset": {"observed_reset": "malformed", "acted_reset": ""}}}))
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        document = cast(dict[str, object], json.loads(state_file.read_text()))
        self.assertNotIn("auto_moves", document)
        self.assertIn("units-low", cast(dict[str, object], document["auto_move_holds"]))

    def test_old_config_rules_do_not_move_a_group_without_a_log_entry(self) -> None:
        path = self.groups()
        config = accounts.accounts_config_file()
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps({"auto_moves": [{"name": "retired", "group": "units",
            "to": "claude2", "when": {"left_at_most": 1}}]}))
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertFalse(accounts.roles_history_file().exists())

    def test_removing_the_head_drops_its_hold_and_leaves_the_next_one_unstarted(self) -> None:
        _ = self.groups()
        first, second = self.hold_rule("entry-one"), self.hold_rule("entry-two")
        self.next_entries(first, second)
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        before = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(before.get("auto_move_holds", {})), {first.id})
        next_moves.remove_next_move("units", next_moves.GroupPosition(1))
        self.assertEqual(next_moves.read_next_moves(), (second,))
        after = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(after.get("auto_move_holds", {}), {})

    def test_legacy_config_named_hold_loads_without_starting_a_new_entry_early(self) -> None:
        path = self.groups()
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        _ = state_file.write_text(json.dumps({"sessions": {}, "notices": {}, "auto_move_holds": {
            "units-low": {"account": "claude1", "at": (NOW - timedelta(hours=1)).isoformat(),
                          "move_id": "", "percent": 1}}}))
        self.next_entries(self.hold_rule("new-entry-id"))
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        loaded = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(loaded.get("auto_move_holds", {})["new-entry-id"].at, NOW)
        self.assertFalse(accounts.roles_history_file().exists())

    def test_due_blocked_head_keeps_the_later_entry_untried_and_unheld(self) -> None:
        for target in ("claude99", "claude3"):
            with self.subTest(first_target=target):
                path = self.groups()
                first = self.hold_rule(f"first-{target}", target=target)
                second = self.hold_rule(f"second-{target}")
                self.next_entries(first, second)
                WatchTime.at = NOW
                self.reading(SOURCE_LOGIN, 1)
                _ = self.cli()
                WatchTime.at = NOW + timedelta(minutes=10)
                self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
                self.reading(TARGET_LOGIN, 1, at=WatchTime.at)
                self.reading("main@example.invalid", 80, at=WatchTime.at)
                with patch.object(accounts, "prepared_account", side_effect=AssertionError("later entry tried")):
                    code, _, error = self.cli()
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertEqual(next_moves.read_next_moves(), (first, second))
                holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(set(holds), {first.id})

    def test_pending_head_blocks_refusal_selection_while_an_unqueued_group_still_moves(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.owner("refused-runner", host="herdr", label="claude3")
        _ = self.transcript("refused-unit", at=NOW)
        _ = self.transcript("refused-runner", at=NOW)
        entry = self.hold_rule("pending", target="claude3")
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 0)
        self.reading(TARGET_LOGIN, 0)
        self.reading("main@example.invalid", 90)
        with (patch.object(accounts, "placement", wraps=accounts.placement) as placement,
              patch.object(accounts, "set_group_account", wraps=accounts.set_group_account) as selected):
            code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(placement.called)
        self.assertEqual({call.args[0] for call in placement.call_args_list}, {"showrunners"})
        self.assertEqual([call.args[0] for call in selected.call_args_list], ["showrunners"])
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude2"})
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual([command.session_id for command in self.moves.calls], ["refused-runner"])
        self.assertFalse(any(row["kind"] == "no room" for row in moves.read_moves()))
        self.assertEqual(self.notifications, [])

    def test_pending_head_refusal_does_not_record_no_room_or_choose_a_fallback(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.transcript("refused-unit")
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 0)
        self.reading(TARGET_LOGIN, 0)
        self.reading("main@example.invalid", 0)
        with (patch.object(accounts, "placement", side_effect=AssertionError("pending group chose fallback")),
              patch.object(accounts, "set_group_account", side_effect=AssertionError("pending group selected fallback")),
              patch.object(accounts, "move_sessions", side_effect=AssertionError("pending group moved members"))):
            code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertFalse(any(row["kind"] == "no room" for row in moves.read_moves()))
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.head_deliveries, [])

    def test_low_rule_persists_its_hold_and_moves_only_after_the_full_interval(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        document = cast(dict[str, object], json.loads(state_file.read_text()))
        self.assertEqual(document["auto_move_holds"], {"units-low": {
            "account": "claude1", "at": NOW.isoformat(), "move_id": "", "percent": 1,
            "minutes": 10, "source": {"kind": "fixed"}}})
        loaded = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(loaded.get("auto_move_holds", {})["units-low"],
                         refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1,
                             next_moves.ResolvedHold(10, next_moves.FixedHold(10))))
        for elapsed in (timedelta(minutes=9, seconds=59), timedelta(minutes=10)):
            WatchTime.at = NOW + elapsed
            self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], "claude1" if elapsed.total_seconds() < 600 else "claude2")
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([(entry["cause"], entry["why"]) for entry in history], [("auto", "auto: units-low")])
        self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]

    def test_fresh_reading_above_the_threshold_restarts_the_hold_clock(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        for minute, left in ((5, 2), (6, 1), (15, 1), (16, 1)):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, left, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], "claude2" if minute == 16 else "claude1")
            holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
            if minute == 5:
                self.assertEqual(holds, {})
            elif minute in (6, 15):
                self.assertEqual(holds["units-low"].at, NOW + timedelta(minutes=6))

    def test_stale_and_missing_readings_neither_start_nor_clear_a_hold(self) -> None:
        for unavailable in ("stale", "missing"):
            with self.subTest(reading=unavailable):
                path = self.groups()
                entry = self.hold_rule(f"units-low-{unavailable}")
                self.next_entries(entry)
                WatchTime.at = NOW
                self.reading(SOURCE_LOGIN, 1, at=NOW - timedelta(minutes=11))
                if unavailable == "missing":
                    saved = usage_store.load()
                    del saved[usage_store.key("Claude", SOURCE_LOGIN)]
                    usage_store.write(saved)
                _ = self.cli()
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
                self.reading(SOURCE_LOGIN, 1)
                _ = self.cli()
                WatchTime.at = NOW + timedelta(minutes=12)
                # Even a stale high reading must not clear the saved low episode.
                self.reading(SOURCE_LOGIN, 80)
                if unavailable == "missing":
                    saved = usage_store.load()
                    del saved[usage_store.key("Claude", SOURCE_LOGIN)]
                    usage_store.write(saved)
                self.reading("main@example.invalid", 80, at=WatchTime.at)
                _ = self.cli()
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {})[entry.id],  # pyright: ignore[reportPrivateUsage]
                                 refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1,
                                     next_moves.ResolvedHold(10, next_moves.FixedHold(10))))
                self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
                _ = self.cli()
                self.assertEqual(stored_group_labels(path)["units"], "claude2")

    def test_changing_the_group_account_requires_a_new_full_hold(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        transition = accounts.set_group_account("units", "claude3", why="operator move", now=NOW + timedelta(minutes=5))
        for minute in (10, 19, 20):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(TARGET_LOGIN, 1, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], "claude2" if minute == 20 else "claude3")
            if minute != 20:
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {})["units-low"],  # pyright: ignore[reportPrivateUsage]
                                 refusal_watch.HoldStarted("claude3", NOW + timedelta(minutes=10), transition.move_id, 1,
                                     next_moves.ResolvedHold(10, next_moves.FixedHold(10))))

    def test_round_trip_group_moves_between_passes_require_a_new_full_hold(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        _ = accounts.set_group_account("units", "claude3", why="operator outward move",
                                       now=NOW + timedelta(minutes=4))
        returned = accounts.set_group_account("units", "claude1", why="operator return move",
                                              now=NOW + timedelta(minutes=5))
        for minute in (10, 19, 20):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, 1, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], "claude2" if minute == 20 else "claude1")
            if minute == 10:
                holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(holds["units-low"], refusal_watch.HoldStarted(
                    "claude1", WatchTime.at, returned.move_id, 1, next_moves.ResolvedHold(10, next_moves.FixedHold(10))))
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([entry["why"] for entry in history],
                         ["operator outward move", "operator return move", "auto: units-low"])

    def test_changing_the_rule_percent_requires_a_new_full_hold(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.next_entries(next_moves.ClaudeGroupMove("units-low", "units", 0, 10, "claude2", NOW))
        for minute in (10, 19, 20):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, 0, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], "claude2" if minute == 20 else "claude1")
            if minute == 10:
                holds = refusal_watch._load_state().get("auto_move_holds", {})  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(holds["units-low"], refusal_watch.HoldStarted(
                    "claude1", WatchTime.at, accounts.NoGroupTransition(), 0, next_moves.ResolvedHold(10, next_moves.FixedHold(10))))
        history = accounts.roles_history_file().read_text().splitlines()
        self.assertEqual(len(history), 1)


    def test_chained_rules_choose_the_first_target_then_hold_again_on_that_account(self) -> None:
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "four@example.invalid"}}))
        _ = (self.home / "notes" / note_name("claude4")).write_text("---\nlogin: four@example.invalid\n---\n")
        path = self.groups()
        self.next_entries(self.hold_rule("first", target="claude4", minutes=5), self.hold_rule("second", minutes=5))
        for minute, source_left, fourth_left, expected in ((0, 1, 80, "claude1"), (5, 1, 80, "claude4"),
                                                          (6, 80, 1, "claude4"), (10, 80, 1, "claude4"),
                                                          (11, 80, 1, "claude2")):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, source_left, at=WatchTime.at)
            self.reading("four@example.invalid", fourth_left, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(stored_group_labels(path)["units"], expected)
            if minute == 5:
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([(entry["label"], entry["why"]) for entry in history],
                         [("claude4", "auto: first"), ("claude2", "auto: second")])

    def test_zero_hold_moves_immediately_like_a_rule_without_a_hold(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule(minutes=0))
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")


    def test_hold_dry_runs_do_not_start_clear_or_consume_the_saved_episode(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        before = self.snapshot()
        _ = self.cli("--dry-run")
        self.assertEqual(self.snapshot(), before)
        _ = self.cli()
        for minute, left in ((5, 2), (10, 1)):
            WatchTime.at = NOW + timedelta(minutes=minute)
            self.reading(SOURCE_LOGIN, left, at=WatchTime.at)
            self.reading("main@example.invalid", 80, at=WatchTime.at)
            before = self.snapshot()
            code, output, error = self.cli("--dry-run")
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(self.snapshot(), before)
            if minute == 10:
                self.assertIn("would move from claude1 to claude2", output)
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")

    def test_auto_move_held_target_keeps_group_and_head_and_names_the_hold(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        head = self.low_rule()
        self.next_entries(head)
        reason = "claude2 is held by a shutdown, requested by session b1f5ca98"
        for permission in (launch_permission.BlockedByShutdown(reason),
                           launch_permission.ShutdownStateUnreadable("broken state")):
            with (self.subTest(permission=permission),
                  patch.object(launch_permission, "launch_permission", return_value=permission)):
                lines = refusal_watch.run_auto_moves(NOW)
            self.assertEqual(stored_group_labels(path)["units"], "claude1")
            self.assertEqual(next_moves.read_next_moves(), (head,))
            self.assertEqual(self.moves.calls, [])
            self.assertTrue(any("no session was stopped" in line for line in lines), lines)
        self.assertTrue(self.head_deliveries)
        self.assertIn(reason, self.head_deliveries[0][0].text)

    def test_refusal_and_committed_group_paths_check_the_shutdown_hold(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.owners.clear()
                self.live.clear()
                _ = self.groups(units="claude3" if committed else "claude1")
                owner = self.owner("held-unit", host="unit")
                self.reading(TARGET_LOGIN, 80)
                if not committed:
                    _ = self.transcript(owner["session_id"])
                with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("target held")):
                    lines = refusal_watch.run(NOW)
                self.assertEqual(self.moves.calls, [])
                self.assertTrue(any("target held" in line for line in lines), lines)
                self.assertTrue(any("target held" in text for text in self.notifications))

    def test_automatic_low_rule_holds_at_two_percent_and_moves_at_one_with_one_transition(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 2)
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.reading(SOURCE_LOGIN, 1)
        _, output, error = self.cli()
        self.assertEqual(error, "")
        self.assertIn("units-low", output)
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(next_moves.read_next_moves(), ())
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["cause"], history[0]["why"], history[0]["label"]),
                         ("auto", "auto: units-low", "claude2"))
        self.assertEqual(self.moves.calls, [MoveCall("unit", "claude2", "auto: units-low")])
        self.assertEqual(self.notifications, [])
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)

    def test_automatic_low_rule_requires_a_fresh_source_reading(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 1, at=NOW - timedelta(minutes=11))
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(len(self.moves.calls), 1)



    def test_unavailable_head_is_reported_without_trying_a_later_valid_entry(self) -> None:
        path = self.groups()
        first, second = self.low_rule("missing-target", target="claude99"), self.low_rule("valid-target")
        self.next_entries(first, second)
        code, output, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("missing-target", output)
        self.assertIn("units 1", output)
        self.assertIn("claude99", output)
        self.assertNotIn("valid-target", output)
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(next_moves.read_next_moves(), (first, second))
        self.assertFalse(accounts.roles_history_file().exists())

    def test_automatic_rule_refuses_low_stale_missing_and_unresolved_target_readings(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        for state, count in (("at threshold", 1), ("below threshold", 1), ("stale", 2),
                             ("missing", 3), ("unresolved refusal", 4)):
            with self.subTest(state=state):
                self.reading("main@example.invalid", 1 if state == "at threshold" else 0 if state == "below threshold" else 80,
                             at=NOW - timedelta(minutes=11) if state == "stale" else NOW)
                if state == "missing":
                    saved = usage_store.load()
                    del saved[usage_store.key("Claude", "main@example.invalid")]
                    usage_store.write(saved)
                if state == "unresolved refusal":
                    refusals.write_refusal(refusals.AccountRefusal("claude", "main@example.invalid",
                        refusals.UnknownWindow(), refusals.NoResetKnown(), NOW))
                _ = self.cli()
                self.assertEqual(self.moves.calls, [])
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertFalse(accounts.roles_history_file().exists())
                self.assertEqual(next_moves.read_next_moves(), (self.low_rule(),))
                self.assertEqual(len(self.head_deliveries), count)
                text = self.head_deliveries[-1][0].text
                expected = {"at threshold": "1%", "below threshold": "1%", "stale": "07:49",
                            "missing": "no reading", "unresolved refusal": "unresolved refusal"}[state]
                self.assertIn(expected, text)
                _ = self.cli()
                self.assertEqual(len(self.head_deliveries), count)

    def test_automatic_rule_does_not_prepare_or_reselect_the_current_account(self) -> None:
        path = self.groups(units="claude2")
        _ = self.owner("unit", host="unit", label="claude2")
        self.next_entries(self.low_rule())
        self.reading("main@example.invalid", 1)
        before = path.read_bytes()
        with patch.object(accounts, "prepared_account", side_effect=AssertionError("same-account rule prepared a move")):
            _ = self.cli()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.moves.calls, [])

    def test_automatic_rules_allow_both_groups_to_share_one_target_and_move_only_off_target_members(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.owner("arrived-unit", host="unit", label="claude2")
        _ = self.owner("runner", host="herdr", label="claude3")
        self.reading(SOURCE_LOGIN, 1)
        self.reading(TARGET_LOGIN, 1)
        self.next_entries(self.low_rule(), self.low_rule("showrunners-low", group="showrunners"))
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude2", "showrunners": "claude2"})
        self.assertEqual({identity for command in self.moves.calls
                          for identity in command.session_id.split(",")}, {"unit", "runner"})
        self.assertEqual({command.target for command in self.moves.calls}, {"claude2"})
        self.assertEqual(self.notifications, [])


    def test_automatic_move_commits_while_the_refusal_watch_lock_is_held(self) -> None:
        _ = self.groups()
        self.next_entries(self.low_rule())
        lock_held = False
        select = accounts.set_group_account
        @contextmanager
        def locked() -> Generator[None]:
            nonlocal lock_held
            self.assertFalse(lock_held)
            lock_held = True
            try:
                yield
            finally:
                lock_held = False
        def select_while_locked(group: accounts.ClaudeGroup, label: str, *, why: str,
                                now: datetime | accounts.AccountDecisionNow = accounts.ACCOUNT_DECISION_NOW,
                                origin: accounts.MoveOrigin = accounts.HAND_ORIGIN,
                                move_id: str = "") -> accounts.GroupTransition:
            self.assertTrue(lock_held, "automatic group selection escaped the refusal lock")
            return select(group, label, why=why, now=now, origin=origin, move_id=move_id)
        with (patch.object(refusal_watch, "_watch_lock", side_effect=locked),
              patch.object(accounts, "set_group_account", side_effect=select_while_locked) as selected):
            _ = self.cli()
        selected.assert_called_once()
        self.assertFalse(lock_held)

    def test_automatic_rules_do_not_run_off_natedev(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule())
        before = path.read_bytes()
        with patch.object(socket, "gethostname", return_value="macbook"):
            _ = self.cli()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.moves.calls, [])

    def test_automatic_rule_keeps_selection_when_target_preparation_refuses(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule())
        with patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("target clash")):
            _, output, error = self.cli()
        self.assertEqual(error, "")
        self.assertIn("target clash", output)
        self.assertEqual(next_moves.read_next_moves(), (self.low_rule(),))
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(len(self.head_deliveries), 1)
        self.assertIn("target clash", self.head_deliveries[0][0].text)




    def test_proof_moves_its_probe_without_changing_account_refusals_or_other_sessions_picker(self) -> None:
        _ = self.owner("probe")
        _ = self.owner("other")
        self.reading(SOURCE_LOGIN, 90, reset=NOW + timedelta(hours=1))
        held = refusals.AccountRefusal("claude", SOURCE_LOGIN, refusals.KnownWindow("weekly"),
            refusals.KnownReset(NOW - timedelta(minutes=1)), NOW - timedelta(hours=1))
        refusals.write_refusal(held)
        before_refusals = refusals.load()
        before_file = refusals.store_file().read_bytes()
        reading = refusals.stored_reading("claude", SOURCE_LOGIN, NOW)
        before_resolved = refusals.resolved(held, reading, NOW)
        self.assertTrue(before_resolved)
        before_pick = accounts.pick("claude", NOW)
        self.assertIsInstance(before_pick, accounts.PickedAccount)
        assert isinstance(before_pick, accounts.PickedAccount)
        self.assertEqual(before_pick.label, "claude1")
        _, _, error = self.cli("--assume-refused", "probe", "weekly")
        self.assertEqual(error, "")
        self.assertEqual(refusals.load(), before_refusals)
        self.assertEqual(refusals.store_file().read_bytes(), before_file)
        self.assertEqual(refusals.resolved(refusals.load()[refusals.key("claude", SOURCE_LOGIN)], reading, NOW), before_resolved)
        after_pick = accounts.pick("claude", NOW)
        self.assertEqual(after_pick, before_pick)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(self.moves.calls[0].session_id, "probe")
        self.assertNotEqual(self.moves.calls[0].target, "claude1")
        self.assertIsInstance(refusal_watch.session_refusal("probe"), refusal_watch.ProofRefusal)
        self.assertIsInstance(refusal_watch.session_refusal("other"), refusal_watch.NoSessionRefusal)

    def test_proof_without_room_does_not_suppress_a_real_refusals_notice_on_the_same_login(self) -> None:
        _ = self.owner("probe")
        _ = self.owner("real")
        for login in ("main@example.invalid", TARGET_LOGIN):
            self.reading(login, 0)
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.UnknownWindow(), refusals.NoResetKnown(), NOW))
        readings = usage_store.store_file().read_bytes()
        _ = self.cli("--assume-refused", "probe", "weekly")
        self.assertEqual(self.moves.calls, [])
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room"])
        proof_notices = len(self.notifications)
        refusal_watch.clear_assumed("probe")
        self.live = [session for session in self.live if session["sessionId"] != "probe"]
        _ = self.transcript("real")
        _ = self.cli()
        self.assertEqual(usage_store.store_file().read_bytes(), readings)
        self.assertEqual(len(self.notifications), proof_notices + 1)
        _ = self.cli()
        self.assertEqual(len(self.notifications), proof_notices + 1)




    def test_transcript_refusal_writes_the_account_once_and_carries_its_window_and_reset(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        write = refusals.write_refusal
        with patch.object(refusals, "write_refusal", wraps=write) as saved:
            _ = self.cli()
            _ = self.cli()
        saved.assert_called_once()
        self.assertIsInstance(refusal_watch.session_refusal("refused"), refusal_watch.TranscriptRefusal)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertIn("weekly limit until", self.moves.calls[0].why)
        refusal = refusals.load()[refusals.key("claude", SOURCE_LOGIN)]
        self.assertEqual((refusal.window, refusal.reset), (refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET)))

    def test_refusal_refreshes_usage_before_placement_in_the_same_pass(self) -> None:
        _ = self.groups()
        _ = self.owner("refused", host="unit")
        _ = self.transcript("refused")
        events: list[str] = []
        original = accounts.placement

        def place(group: accounts.ClaudeGroup, now: datetime, *, exclude: frozenset[str] = NO_SESSION_FILTER) -> accounts.GroupPlacement:
            events.append("placement")
            return original(group, now, exclude=exclude)

        with patch.object(accounts, "placement", side_effect=place):
            _ = refusal_watch.run(NOW, refresh_usage=lambda: events.append("fresh usage"))
        self.assertEqual(events[0], "fresh usage")
        self.assertIn("placement", events)

    def test_quiet_watcher_does_not_request_a_live_usage_read(self) -> None:
        events: list[str] = []
        _ = refusal_watch.run(NOW, refresh_usage=lambda: events.append("fresh usage"))
        self.assertEqual(events, [])

    def test_terminal_refusal_does_not_refresh_unchanged_usage(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        for terminal in ("requested", "cancelled", "continued", "group selected"):
            with self.subTest(terminal=terminal):
                path = self.home / ".local/state/accounts/refusal_watch.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(json.dumps({"sessions": {"refused": {
                    "entry_id": "refusal-one", "state": terminal, "move_id": "fixture"}},
                    "notices": {}, "auto_move_holds": {}}))
                with patch.object(accounts, "placement") as placement, \
                     patch.object(accounts, "pick") as pick, \
                     patch.object(refusal_watch, "renew_expired_logins", return_value=[]):
                    events: list[str] = []
                    _ = refusal_watch.run(NOW, refresh_usage=lambda: events.append("fresh usage"))
                self.assertEqual(events, [])
                placement.assert_not_called()
                pick.assert_not_called()

    def test_failed_timer_refresh_keeps_refusal_placement_and_watchdog(self) -> None:
        from scripts.accounts import unit_watchdog
        from scripts.whoami import agent_notes, codex_pacer

        selection = accounts.claude_account_selection()
        readings = quota_alert.codex_alert_pass(agent_accounts.NoWorkersHome("fixture"), [], NOW)
        for leaf in ("live_reports", "save"):
            for error in (OSError("fixture failure\nsecond line"), RuntimeError("fixture failure")):
                with self.subTest(leaf=leaf, error=type(error).__name__):
                    session_id = f"refused-{leaf}-{type(error).__name__}"
                    _ = self.owner(session_id)
                    _ = self.transcript(session_id)
                    before = len(self.moves.calls)
                    output = io.StringIO()
                    with (patch.object(agent_notes, "update_alert_pass", return_value=agent_notes.UpdatedAgentNotes((), readings, True)),
                          patch.object(accounts, "claude_account_selection", return_value=selection),
                          patch.object(agent_notes, "resolve_workers_home", return_value=agent_accounts.NoWorkersHome("fixture")),
                          patch.object(agent_notes, "live_reports", return_value=[]) as live,
                          patch.object(usage_store, "save") as save,
                          patch.object(agent_notes, "alert", return_value=[]),
                          patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"),
                          patch.object(unit_watchdog, "run", return_value=[]) as watchdog,
                          patch.object(sys, "argv", ["agent_notes.py"]),
                          redirect_stderr(output), redirect_stdout(io.StringIO())):
                        (live if leaf == "live_reports" else save).side_effect = error
                        agent_notes.main()
                    watchdog.assert_called_once()
                    self.assertEqual(len(output.getvalue().splitlines()), 1)
                    self.assertIn("refusal usage refresh:", output.getvalue())
                    self.assertEqual(len(self.moves.calls), before + 1)

    def test_assumed_refusal_rejects_both_real_group_kinds_without_changing_selections(self) -> None:
        groups = self.groups()
        for identity, host in (("unit", "unit"), ("runner", "herdr")):
            _ = self.owner(identity, host=host)
            before = self.snapshot()
            with self.subTest(host=host), redirect_stderr(io.StringIO()):
                try:
                    code, _, error = self.cli("--assume-refused", identity, "weekly")
                except SystemExit as refused:
                    code, error = refused.code, "grouped"
                self.assertNotEqual(code, 0)
                self.assertIn("group", error)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(refusal_watch.assumed_file(identity).exists())
        self.assertEqual(json.loads(groups.read_text()), {"units": "claude1", "showrunners": "claude3"})
        self.assertEqual(self.moves.calls, [])

    def test_proof_wait_continuation_deletes_only_its_assumption_with_routing_entries_present(self) -> None:
        _ = self.owner("probe")
        _ = self.owner("other")
        _ = self.cli("--assume-refused", "probe", "five-hour", "--reset-in", "19")
        other = refusal_watch.assumed_file("other")
        _ = other.write_text(json.dumps({"uuid": "unrelated", "timestamp": NOW.isoformat(),
            "window": "weekly", "reset": ""}))
        routing: moves.RoutedToClaude = {"kind": "routed to claude", "at": NOW.isoformat(),
            "tool": "codex", "fallback_at": NOW.isoformat(), "owner": "codex1", "cause": "refusal", "why": "Codex refused work for quota", "moved": ["direct"]}
        moves.append_routing(routing)
        WatchTime.at = NOW + timedelta(minutes=19)
        # Other is not live on the continuation tick; its proof file must survive.
        self.live = [live for live in self.live if live["sessionId"] == "probe"]
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery:
            _ = self.cli()
            _ = self.cli()
        delivery.assert_called_once()
        self.assertIn("Continue where you stopped.", delivery.call_args.args)
        self.assertFalse(refusal_watch.assumed_file("probe").exists())
        self.assertTrue(other.exists())
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited", "wait ended"])
        self.assertEqual(self.moves.calls, [])

    def test_proof_group_moves_only_the_named_probes_and_never_reads_real_group_state(self) -> None:
        _ = self.owner("probe-one")
        _ = self.owner("probe-two", host="ghostty", label="claude3")
        _ = self.owner("unit", host="unit")
        _ = self.owner("runner", host="herdr", label="claude3")
        groups = self.groups()
        before = groups.read_bytes()
        with (patch.object(accounts, "group_accounts", side_effect=AssertionError("proof must not read real groups")),
              patch.object(accounts, "move_sessions", wraps=accounts.move_sessions) as move):
            _, _, error = self.cli("--assume-refused", "probe-one", "weekly", "--proof-group", "probe-one", "probe-two")
        self.assertEqual(error, "")
        move.assert_called_once()
        self.assertEqual(set(cast(tuple[str, ...], move.call_args.args[0])), {"probe-one", "probe-two"})
        self.assertEqual({command.target for command in self.moves.calls}, {"claude2"})
        self.assertEqual({identity for command in self.moves.calls for identity in command.session_id.split(",")},
                         {"probe-one", "probe-two"})
        self.assertEqual(shutdown_record.all_session_records(), [])
        self.assertEqual(groups.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertEqual(len(self.moves.calls), 2)
        self.assertTrue(all("weekly limit" in command.why for command in self.moves.calls))



    def test_hand_group_selection_without_live_members_is_reused_for_a_later_member(self) -> None:
        _ = self.groups()
        _ = accounts.move_group("units", "claude3", why="select before starting work")
        selection = cast(dict[str, object], json.loads(accounts.groups_file().read_text())["units"])
        self.assertTrue(selection["move_id"])
        self.assertEqual(moves.read_moves(), [])
        _ = self.owner("late", host="unit")
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual((self.moves.calls[0].session_id, self.moves.calls[0].target), ("late", "claude3"))
        self.assertEqual(cast(dict[str, object], json.loads(accounts.groups_file().read_text())["units"])["move_id"], selection["move_id"])


    def test_units_group_preview_lists_exactly_off_target_members_without_writes(self) -> None:
        off = self.owner("off-target", host="unit")
        _ = self.owner("already-there", host="unit", label="claude3")
        _ = self.owner("other-group", host="herdr")
        before = self.snapshot()
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "units", "claude3", "--dry-run"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertIn("units: would select claude3", output.getvalue())
        self.assertIn(f"{off['name']}: {off['session_id']}", output.getvalue())
        self.assertIn("stop, then resume on claude3", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual(shutdown_record.all_session_records(), [])
        self.assertEqual(self.moves.calls, [])

    def test_units_group_preview_on_current_account_prints_no_live_session_moves(self) -> None:
        _ = self.owner("already-there", host="unit")
        self.reading(SOURCE_LOGIN, 80)
        before = self.snapshot()
        result = accounts.move_group("units", "claude1", dry_run=True)
        self.assertIsInstance(result, accounts.GroupMoveDryRun)
        assert isinstance(result, accounts.GroupMoveDryRun)
        self.assertEqual(result.session_ids, ())
        self.assertEqual(result.lines, ("claude1: would check the shutdown hold",
            "units: would select claude1", "units: no live session moves"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.moves.calls, [])

    def test_units_group_move_returns_group_named_started_outcome(self) -> None:
        _ = self.owner("off-target", host="unit")
        _ = self.owner("already-there", host="unit", label="claude3")
        result = accounts.move_group("units", "claude3", why="checkpoint move")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        assert isinstance(result, accounts.GroupMoveStarted)
        self.assertEqual(result.session_ids, ("off-target",))
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(self.moves.calls, [MoveCall("off-target", "claude3", "checkpoint move")])
        self.assertIn("off-target: resumed on claude3", result.stdout)

    def test_units_group_failed_request_returns_group_named_failure(self) -> None:
        _ = self.owner("off-target", host="unit")
        refused = accounts.SessionMoveRefused("off-target", "stop request failed")
        with patch.object(accounts, "move_sessions", return_value=(refused,)):
            result = accounts.move_group("units", "claude3")
        self.assertIsInstance(result, accounts.GroupMoveStartFailed)
        assert isinstance(result, accounts.GroupMoveStartFailed)
        self.assertEqual(result.session_ids, ("off-target",))
        self.assertEqual(result.returncode, 1)
        self.assertIn("stop request failed", result.stderr)

    def test_partially_moved_group_preview_uses_only_the_new_refusal_destination(self) -> None:
        path = self.groups(units="claude3")
        _ = self.owner("refused", host="unit")
        _ = self.owner("partial", host="unit")
        _ = self.transcript("refused")
        self.reading("main@example.invalid", 80, reset=NOW + timedelta(hours=1))
        before = self.snapshot()
        _, output, error = self.cli("--dry-run")
        self.assertEqual(error, "")
        self.assertIn("move from claude1 to claude2", output)
        self.assertIn("owner partial: refused group units; move to claude2", output)
        self.assertNotIn("moving to claude3", output)
        self.assertNotIn("move to claude3", output)
        self.assertEqual(path.read_bytes(), before[str(path.relative_to(self.home))])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.moves.calls, [])

    def test_example_groups_move_only_after_their_own_refusals(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        runner = self.owner("runner", host="herdr", label="claude3")
        self.reading("main@example.invalid", 0, reset=NOW + timedelta(days=2))
        self.reading(TARGET_LOGIN, 3, reset=NOW + timedelta(days=4))
        self.reading(SOURCE_LOGIN, 53, reset=NOW + timedelta(days=6))
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        _ = self.transcript("runner")
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude1"})
        command = self.moves.calls[0]
        self.assertEqual(command.session_id, "runner")
        self.assertEqual(command.target, "claude1")
        runner["config_dir"] = str(accounts.account_dir("claude1"))
        _ = self.transcript("unit")
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude1"})
        self.assertEqual(len(self.moves.calls), 1)

    def test_group_selection_is_stored_before_shutdown_requests_are_sent(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.transcript("unit")
        original = accounts.move_sessions
        def requested(ids: tuple[str, ...], target: str, *, why: str) -> tuple[stop_resume.Resumed | stop_resume.MoveHandedOff | accounts.SessionMoveRefused, ...]:
            self.assertEqual(stored_group_labels(path)["units"], target)
            return original(ids, target, why=why)
        with patch.object(accounts, "move_sessions", side_effect=requested):
            _ = self.cli()
        history = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["group"], history[0]["label"]), ("units", "claude3"))
        self.assertIn("refused", str(history[0]["why"]))

    def test_manual_showrunner_group_command_needs_only_destination(self) -> None:
        path = self.groups(showrunners="claude1")
        _ = self.owner("runner-one", host="herdr")
        _ = self.owner("runner-two", host="herdr")
        _ = self.owner("unit", host="unit")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "showrunners", "claude3"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude3"})
        self.assertEqual(len(self.moves.calls), 2)
        self.assertEqual({command.session_id for command in self.moves.calls}, {"runner-one", "runner-two"})

    def test_refusal_of_one_unit_moves_every_live_unit_and_leaves_showrunners(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.owner("healthy-unit", host="unit")
        _ = self.owner("healthy-showrunner", host="herdr", label="claude3")
        _ = self.transcript("refused-unit")
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 2)
        self.assertEqual({command.session_id for command in self.moves.calls}, {"refused-unit", "healthy-unit"})
        self.assertEqual({command.target for command in self.moves.calls}, {"claude3"})
        self.assertEqual(stored_group_labels(path), {"units": "claude3", "showrunners": "claude3"})

    def test_classified_showrunner_without_update_timer_moves_with_its_group(self) -> None:
        path = self.groups(showrunners="claude1")
        refused = self.owner("refused-runner", host="herdr")
        healthy = self.owner("runner-without-timer", host="herdr")
        _ = self.owner("unit", host="unit")
        self.assertEqual(healthy["kind"], "showrunner")
        self.assertEqual(healthy["timers"], [])
        _ = self.transcript(refused["session_id"])
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 2)
        self.assertEqual({command.session_id for command in self.moves.calls},
            {refused["session_id"], healthy["session_id"]})
        self.assertEqual({command.target for command in self.moves.calls}, {"claude2"})
        self.assertEqual(stored_group_labels(path)["showrunners"], "claude2")
        self.assertEqual(stored_group_labels(path)["units"], "claude1")

    def test_healthy_groups_stay_on_their_accounts_despite_better_placement(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.owner("runner", host="herdr", label="claude3")
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(moves.read_moves(), [])

    def test_group_target_preparation_refusal_changes_no_state_and_retries_next_tick(self) -> None:
        _ = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.owner("healthy", host="unit")
        _ = self.transcript("unit")
        before = self.snapshot()
        with patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("target is not ready")) as prepare:
            _ = self.cli()
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.moves.calls, [])
            WatchTime.at = NOW + timedelta(minutes=2)
            _ = self.cli()
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(self.snapshot(), before)



    def test_five_hour_wait_holds_whole_group_without_reassigning_healthy_members(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.owner("healthy-unit", host="unit")
        _ = self.transcript("refused-unit", "You've hit your session limit")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=NOW + timedelta(minutes=19))
        before = path.read_bytes()
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_no_room_keeps_group_account_and_alerts_once(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.transcript("unit")
        for login in ("main@example.invalid", TARGET_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        before = path.read_bytes()
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room"])

    def test_partial_group_no_room_refusal_stops_reconciliation_onto_refused_target(self) -> None:
        """F007: a new target refusal cannot pull remaining members onto that account."""
        path = self.groups(units="claude3")
        _ = self.owner("arrived-unit", host="unit", label="claude3")
        _ = self.owner("remaining-unit", host="unit", label="claude1")
        _ = self.transcript("arrived-unit")
        for login in ("main@example.invalid", SOURCE_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        before = path.read_bytes()
        _, output, error = self.cli()
        self.assertEqual(error, "")
        self.assertIn("no claude account has room", output)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(shutdown_record.all_session_records(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_partial_group_reconciliation_skips_unresolved_target_refusal_without_new_transcript(self) -> None:
        """F007: reconciliation must check the selected target's existing refusal."""
        path = self.groups(units="claude3")
        _ = self.owner("remaining-unit", host="unit", label="claude1")
        refusals.write_refusal(refusals.AccountRefusal("claude", TARGET_LOGIN,
            refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(shutdown_record.all_session_records(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_unconfigured_group_does_not_adopt_live_default_login_aliases(self) -> None:
        """F004: live membership cannot configure a launch group."""
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.owner("unit-alias-one", host="unit", label="claude2")
        _ = self.owner("unit-alias-two", host="unit", label="claude2")
        with self.assertRaises(accounts.AccountsError) as refused:
            _ = accounts.role_account("unit")
        self.assertIn("accounts.py move units <claudeN>", str(refused.exception))
        self.assertFalse(accounts.groups_file().exists())

    def test_manual_group_move_default_login_alias_stores_default_and_moves_once(self) -> None:
        path = self.groups()
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.owner("unit", host="unit")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "units", "claude2"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(len(self.moves.calls), 1)
        command = self.moves.calls[0]
        self.assertEqual(command.target, "claude2")

    def test_status_without_group_file_does_not_infer_live_members_accounts(self) -> None:
        """Status reports missing group accounts even when their sessions are live."""
        _ = self.owner("unit-one", host="unit")
        _ = self.owner("runner", host="herdr", label="claude3")
        before = self.snapshot()
        output = accounts.status_report()
        for group in ("units", "showrunners"):
            self.assertEqual(output.splitlines().count(
                f"{group}: not configured; the role map selects no account; run accounts.py move {group} <claudeN>"), 1)
        rows = [line for line in output.splitlines() if line.startswith("|")][2:]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.split("|")[3].strip(), "")
        self.assertEqual(self.snapshot(), before)

    def test_refused_unconfigured_groups_report_setup_and_move_no_members(self) -> None:
        """F004: neither real nor preview ticks infer a missing group's account."""
        for identity, host in (("unit-one", "unit"), ("unit-two", "unit"), ("runner", "herdr")):
            _ = self.owner(identity, host=host)
            _ = self.transcript(identity)
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                _, output, error = self.cli(*arguments)
                self.assertEqual(error, "")
                for group in ("units", "showrunners"):
                    self.assertIn(f"{group}: not configured", output)
                    self.assertIn(f"accounts.py move {group} <claudeN>", output)
                self.assertFalse(accounts.groups_file().exists())
                self.assertEqual(self.moves.calls, [])
                self.assertEqual(shutdown_record.all_session_records(), [])

    def test_refusal_dry_run_keeps_missing_group_file_absent(self) -> None:
        _ = self.owner("refused", host="unit")
        _ = self.owner("healthy", host="unit")
        _ = self.transcript("refused")
        before = self.snapshot()
        _, output, error = self.cli("--dry-run")
        self.assertEqual(error, "")
        self.assertIn("units: not configured", output)
        self.assertIn("accounts.py move units <claudeN>", output)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / ".local/state/accounts/groups.json").exists())

    def test_ungrouped_scratch_refusal_moves_alone_without_changing_group_accounts(self) -> None:
        path = self.groups()
        _ = self.owner("scratch", host="terminal")
        _ = self.owner("unit", host="unit")
        _ = self.owner("runner", host="herdr", label="claude3")
        _ = self.transcript("scratch")
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)
        command = self.moves.calls[0]
        self.assertEqual(command.session_id, "scratch")
        self.assertEqual(path.read_bytes(), before)

    def test_grouped_session_manual_move_refuses_with_group_command(self) -> None:
        _ = self.groups()
        _ = self.owner("unit", host="unit")
        def manual_inventory(login: str, only: frozenset[str] = NO_SESSION_FILTER, **_options: object) -> inventory.Inventory:
            return self.inventory_for(login, only, refresh_desktops=False)
        with patch.object(inventory, "inventory", side_effect=manual_inventory):
            result = accounts.move("unit", "claude3")
        self.assertIsInstance(result, accounts.MoveRefused)
        assert isinstance(result, accounts.MoveRefused)
        self.assertIn("move units", result.reason)
        self.assertEqual(self.moves.calls, [])

    def test_manual_group_move_sets_account_and_requests_all_members_once(self) -> None:
        path = self.groups()
        _ = self.owner("one", host="unit")
        _ = self.owner("two", host="unit")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "units", "claude3"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual(len(self.moves.calls), 2)
        self.assertEqual({command.session_id for command in self.moves.calls}, {"one", "two"})

    def test_status_names_off_account_group_member_as_moving(self) -> None:
        _ = self.groups(units="claude3")
        _ = self.owner("unit", host="unit")
        output, error = io.StringIO(), io.StringIO()
        from scripts.message import sessions as status_sessions
        with patch.object(status_sessions, "live_sessions", return_value=self.live), redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["--detail"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        line = next(line for line in output.getvalue().splitlines() if "owner unit" in line)
        self.assertIn("moving", line)
        self.assertIn("claude3", line)

    def test_dry_run_detects_refusal_on_each_host_and_changes_nothing(self) -> None:
        _ = self.groups(showrunners="claude1")
        for host in ("herdr", "unit", "ghostty", "zed", "terminal"):
            for window, text in (("weekly", "weekly limit"), ("five", "You've hit your session limit")):
                identity = host + "-" + window
                _ = self.owner(identity, host=host)
                _ = self.transcript(identity, text)
        before = self.snapshot()
        code, output, error = self.cli("--dry-run")
        self.assertIn(code, (0, None))
        self.assertEqual(error, "")
        for host in ("herdr", "unit", "ghostty", "zed", "terminal"):
            self.assertIn(host, output)
        self.assertIn("claude3", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(moves.read_moves(), [])

    def test_preview_accepts_repairable_destinations_without_changing_any_home_entry(self) -> None:
        _ = self.groups(units="claude3")
        _ = self.owner("off-target-unit", host="unit")
        _ = self.transcript("off-target-unit")
        _ = self.owner("scratch-refusal", host="terminal")
        _ = self.transcript("scratch-refusal")
        destination = accounts.account_dir("claude3")
        shared = self.home / ".claude"
        (destination / "shell-snapshots").unlink()
        (destination / "shell-snapshots").mkdir()
        for label in ("claude1", "claude2", "claude3"):
            (accounts.account_dir(label) / "skills").unlink()
        (shared / "skills").rmdir()
        _ = (destination / "settings.json").write_bytes(b"adoptable local settings")

        def listing() -> dict[str, tuple[int, int, int, int, str]]:
            entries: dict[str, tuple[int, int, int, int, str]] = {}
            for path in self.home.rglob("*"):
                stat = path.lstat()
                entries[str(path.relative_to(self.home))] = (
                    stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns,
                    os.readlink(path) if path.is_symlink() else "")
            return entries

        before = listing()
        code, output, error = self.cli("--dry-run")
        self.assertIn(code, (0, None))
        self.assertEqual(error, "")
        self.assertIn("off-target-unit", output)
        self.assertIn("scratch-refusal", output)
        self.assertIn("claude3", output)
        self.assertNotIn("refused:", output)
        self.assertEqual(listing(), before)
        self.assertEqual((destination / "settings.json").read_bytes(), b"adoptable local settings")
        self.assertEqual(self.moves.calls, [])

    def test_only_last_entry_with_all_refusal_markers_is_detected(self) -> None:
        for identity, tail in (
            ("user-last", {"type": "user", "message": {"content": "hello"}}),
            ("ordinary-error", {"type": "assistant", "isApiErrorMessage": True, "error": "overloaded"}),
            ("ordinary-reply", {"type": "assistant", "message": {"content": "weekly limit"}}),
        ):
            _ = self.owner(identity)
            _ = self.transcript(identity, tail=tail)
        _, output, _ = self.cli("--dry-run")
        for owner in self.owners:
            self.assertNotIn(owner["session_id"], output)

    def test_refusal_survives_metadata_after_the_newest_exchange(self) -> None:
        path = self.transcript("metadata-refusal")
        with path.open("a") as stream:
            for kind in ("system", "bridge-session", "cost-state", "last-prompt",
                         "queue-operation", "history-suppression", "file-history-snapshot"):
                _ = stream.write(json.dumps({"type": kind, "subtype": "turn_duration"}) + "\n")
            _ = stream.write("invalid-json\n\n")
        found = refusal_watch.transcript_refusal("metadata-refusal")
        self.assertIsInstance(found, refusal_watch.TranscriptRefusal)
        if isinstance(found, refusal_watch.TranscriptRefusal):
            self.assertEqual(found.entry_id, "refusal-one")
            self.assertEqual(found.window, refusals.KnownWindow("weekly"))
            self.assertEqual(found.refused_at.at, REFUSAL_AT)

    def test_new_user_exchange_clears_refusal_until_another_refusal_arrives(self) -> None:
        path = self.transcript("continue-refusal")
        refusal_line = path.read_text().splitlines()[-1]
        with path.open("a") as stream:
            _ = stream.write(json.dumps({"type": "user", "message": {"content": "continue"}}) + "\n")
            _ = stream.write('{"type":"last-prompt"}\n')
        self.assertIsInstance(refusal_watch.transcript_refusal("continue-refusal"), refusal_watch.NoSessionRefusal)
        with path.open("a") as stream:
            _ = stream.write(refusal_line + '\n{"type":"cost-state"}\n')
        self.assertIsInstance(refusal_watch.transcript_refusal("continue-refusal"), refusal_watch.TranscriptRefusal)
        with path.open("a") as stream:
            _ = stream.write('{"type":"assistant","message":{"content":"done"}}\n{"type":"system"}\n')
        self.assertIsInstance(refusal_watch.transcript_refusal("continue-refusal"), refusal_watch.NoSessionRefusal)

    def test_refusal_quota_limits_override_text_window_and_supply_reset(self) -> None:
        for name, window, reset in (("seven_day", "weekly", WEEKLY_RESET),
                                    ("five_hour", "five_hour", FIVE_HOUR_RESET)):
            with self.subTest(rate_limit_type=name):
                path = self.transcript(name, "hit your session limit" if window == "weekly" else "weekly limit")
                entries = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
                entries[-1]["quotaLimits"] = {"rateLimitType": name, "resetsAt": int(reset.timestamp())}
                _ = path.write_text("\n".join(json.dumps(entry) for entry in entries) + '\n{"type":"system"}\n')
                found = refusal_watch.transcript_refusal(name)
                self.assertIsInstance(found, refusal_watch.TranscriptRefusal)
                if isinstance(found, refusal_watch.TranscriptRefusal):
                    self.assertEqual(found.window, refusals.KnownWindow(cast(Literal["weekly", "five_hour"], window)))
                    self.assertEqual(found.reset, refusals.KnownReset(reset))

    def test_newest_exchange_reassembles_large_utf8_lines_and_skips_long_metadata(self) -> None:
        path = self.transcript("split-refusal", "weekly limit " + "é" * 70_000)
        entries = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        _ = path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n", encoding="utf-8")
        with path.open("a") as stream:
            _ = stream.write(json.dumps({"type": "cost-state", "padding": "x" * 70_000}) + "\n")
        found = refusal_watch.transcript_refusal("split-refusal")
        self.assertIsInstance(found, refusal_watch.TranscriptRefusal)
        if isinstance(found, refusal_watch.TranscriptRefusal):
            self.assertIn("é" * 70_000, found.text)

    def test_healthy_live_owners_never_invoke_inventory_or_start_a_move(self) -> None:
        for host in ("herdr", "unit", "ghostty", "zed", "terminal"):
            _ = self.owner(host, host=host)
            _ = self.transcript(host, tail={"type": "assistant", "message": {"model": "opus-real", "content": "normal work"}})
        with patch.object(inventory, "inventory") as observe:
            _ = self.cli()
        observe.assert_not_called()
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(self.notifications, [])
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual(refusals.load(), {})

    def test_large_last_entries_and_trailing_blanks_preserve_refusal_detection(self) -> None:
        _ = self.owner("large-refusal")
        refused = self.transcript("large-refusal", "weekly limit " + "detail " * 1000)
        _ = self.owner("large-healthy", host="ghostty")
        healthy = self.transcript("large-healthy", tail={"type": "assistant",
            "message": {"model": "opus-real", "content": "normal work " * 1000}})
        for path in (refused, healthy):
            self.assertGreater(path.stat().st_size, 4096)
            with path.open("a") as output:
                _ = output.write("\n\n\n")
        before = self.snapshot()
        _, output, error = self.cli("--dry-run")
        self.assertEqual(error, "")
        self.assertIn("large-refusal", output)
        self.assertIn("claude3", output)
        self.assertNotIn("large-healthy", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.moves.calls, [])

    def test_refused_sessions_on_one_account_move_by_group_and_scratch_boundary(self) -> None:
        _ = self.groups(showrunners="claude1")
        for identity, host in (("director", "unit"), ("showrunner", "herdr"), ("desktop", "ghostty")):
            _ = self.owner(identity, host=host)
            _ = self.transcript(identity)
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 3)
        self.assertEqual({command.session_id for command in self.moves.calls},
            {"director", "showrunner", "desktop"})
        command = self.moves.calls[0]
        self.assertEqual(command.target, "claude3")
        self.assertIn("weekly limit until", command.why)
        self.assertEqual(shutdown_record.all_session_records(), [])
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, NOW - timedelta(seconds=1))

    def test_unchanged_transcript_rescan_keeps_refusal_time_and_never_repeats_move(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        _ = self.cli()
        before = self.snapshot()
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, NOW - timedelta(seconds=1))

    def test_new_transcript_refusal_uuid_is_a_new_transition(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        _ = self.cli()
        WatchTime.at = NOW + timedelta(minutes=2)
        self.owners[0]["config_dir"] = str(accounts.account_dir("claude1"))
        _ = self.transcript("refused", at=WatchTime.at, entry_id="refusal-two")
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 2)
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, WatchTime.at)

    def test_unnamed_window_uses_only_a_post_refusal_reading_with_one_zero(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        held = refusals.load()[refusals.key("claude", SOURCE_LOGIN)]
        self.assertEqual(held.window, refusals.KnownWindow("five_hour"))
        self.assertEqual(held.reset, refusals.KnownReset(NOW + timedelta(minutes=19)))
        self.assertEqual(self.moves.calls, [])
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_pre_refusal_reading_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 80, five=0, at=NOW - timedelta(minutes=1), five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.moves.calls), 1)

    def test_both_zero_windows_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 0, five=0, five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.moves.calls), 1)

    def test_missing_reading_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        saved = usage_store.load()
        _ = saved.pop(usage_store.key("Claude", SOURCE_LOGIN))
        usage_store.write(saved)
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.moves.calls), 1)

    def test_wait_rerun_restores_its_journaled_move_and_continues_without_moving(self) -> None:
        class Killed(BaseException):
            pass

        _ = self.owner("interrupted-wait", host="ghostty")
        _ = self.transcript("interrupted-wait", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        set_transition = refusal_watch._set_transition  # pyright: ignore[reportPrivateUsage]

        def stop_after_wait(state: refusal_watch.WatchState, session_id: str, entry_id: str,
                            transition: refusal_watch.WatchTransition, move_id: str) -> None:
            if transition == "waited":
                raise Killed
            set_transition(state, session_id, entry_id, transition, move_id)

        with patch.object(refusal_watch, "_set_transition", side_effect=stop_after_wait):
            with self.assertRaises(Killed):
                _ = self.cli()
        waited, = moves.read_moves()
        self.assertEqual(waited["kind"], "waited")
        self.assertEqual(self.moves.calls, [])

        code, _, error = self.cli()

        self.assertEqual((code, error), (0, ""))
        saved = refusal_watch._load_state()["sessions"]["interrupted-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"], saved["move_id"]),
                         ("refusal-one", "waited", waited["move_id"]))
        self.assertEqual(moves.read_moves(), [waited])
        WatchTime.at = reset
        with (patch.object(accounts, "pick", side_effect=AssertionError("waited session chose another account")),
              patch.object(accounts, "move_sessions", side_effect=AssertionError("waited session moved")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
            _, output, _ = self.cli()
            _ = self.cli()
        self.assertIn("continued on its current account", output)
        delivery.assert_called_once_with("session:interrupted-wait", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-interrupted-wait-refusal-one")
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual(entries[1]["move_id"], waited["move_id"])
        self.assertEqual(self.moves.calls, [])

    def test_grouped_wait_rerun_continues_after_reset_without_preparing_a_refused_destination(self) -> None:
        class Killed(BaseException):
            pass

        path = self.groups()
        _ = self.owner("interrupted-unit-wait", host="unit")
        _ = self.transcript("interrupted-unit-wait", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        set_transition = refusal_watch._set_transition  # pyright: ignore[reportPrivateUsage]

        def stop_after_wait(state: refusal_watch.WatchState, session_id: str, entry_id: str,
                            transition: refusal_watch.WatchTransition, move_id: str) -> None:
            if transition == "waited":
                raise Killed
            set_transition(state, session_id, entry_id, transition, move_id)

        with patch.object(refusal_watch, "_set_transition", side_effect=stop_after_wait):
            with self.assertRaises(Killed):
                _ = self.cli()
        waited, = moves.read_moves()
        self.assertEqual(waited["kind"], "waited")
        saved = refusal_watch._load_state()["sessions"]["interrupted-unit-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"]), ("refusal-one", "retry"))

        WatchTime.at = reset
        with (patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("fixture destination refused")) as prepared,
              patch.object(accounts, "placement", wraps=accounts.placement) as placement,
              patch.object(accounts, "pick", wraps=accounts.pick) as pick,
              patch.object(accounts, "move_sessions", side_effect=AssertionError("wait moved")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
            code, output, error = self.cli()
            _ = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines(), ["owner interrupted-unit-wait: continued on its current account"])
        delivery.assert_called_once_with("session:interrupted-unit-wait", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-interrupted-unit-wait-refusal-one")
        prepared.assert_not_called()
        placement.assert_not_called()
        pick.assert_not_called()
        saved = refusal_watch._load_state()["sessions"]["interrupted-unit-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"], saved["move_id"]),
                         ("refusal-one", "continued", waited["move_id"]))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual(entries[1]["move_id"], waited["move_id"])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(self.moves.calls, [])

    def test_grouped_no_room_wait_rerun_reuses_its_journal_and_continues_without_a_destination(self) -> None:
        class Killed(BaseException):
            pass

        path = self.groups()
        _ = self.owner("no-room-wait", host="unit")
        _ = self.transcript("no-room-wait", "You've hit your session limit")
        reset = NOW + timedelta(minutes=30)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        for label in ("claude2", "claude3"):
            identity = accounts.account_identity(accounts.account_dir(label))
            assert isinstance(identity, agent_accounts.NumberedAccount)
            refusals.write_refusal(refusals.AccountRefusal("claude", identity.login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        saved = refusal_watch._load_state()["sessions"]["no-room-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"]), ("refusal-one", "no room"))
        no_room, = moves.read_moves()
        self.assertEqual(no_room["kind"], "no room")
        self.assertEqual(self.moves.calls, [])

        WatchTime.at = NOW + timedelta(minutes=11)
        self.reading(SOURCE_LOGIN, 80, five=0, at=WatchTime.at, five_reset=reset)
        set_transition = refusal_watch._set_transition  # pyright: ignore[reportPrivateUsage]

        def stop_after_wait(state: refusal_watch.WatchState, session_id: str, entry_id: str,
                            transition: refusal_watch.WatchTransition, move_id: str) -> None:
            if transition == "waited":
                raise Killed
            set_transition(state, session_id, entry_id, transition, move_id)

        with patch.object(refusal_watch, "_set_transition", side_effect=stop_after_wait):
            with self.assertRaises(Killed):
                _ = self.cli()
        _, waited = moves.read_moves()
        self.assertEqual(waited["kind"], "waited")
        saved = refusal_watch._load_state()["sessions"]["no-room-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"]), ("refusal-one", "no room"))

        with (patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("fixture destination refused")) as prepared,
              patch.object(accounts, "placement", wraps=accounts.placement) as placement,
              patch.object(accounts, "pick", wraps=accounts.pick) as pick,
              patch.object(accounts, "move_sessions", side_effect=AssertionError("wait moved")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
            code, _, error = self.cli()
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(moves.read_moves(), [no_room, waited])
            saved = refusal_watch._load_state()["sessions"]["no-room-wait"]  # pyright: ignore[reportPrivateUsage]
            self.assertEqual((saved["entry_id"], saved["state"], saved["move_id"]),
                             ("refusal-one", "waited", waited["move_id"]))
            delivery.assert_not_called()

            WatchTime.at = reset
            code, output, error = self.cli()
            _ = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines(), ["owner no-room-wait: continued on its current account"])
        delivery.assert_called_once_with("session:no-room-wait", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-no-room-wait-refusal-one")
        prepared.assert_not_called()
        placement.assert_not_called()
        pick.assert_not_called()
        saved = refusal_watch._load_state()["sessions"]["no-room-wait"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"], saved["move_id"]),
                         ("refusal-one", "continued", waited["move_id"]))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["no room", "waited", "wait ended"])
        self.assertEqual(entries[-1]["move_id"], waited["move_id"])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.assertEqual(self.moves.calls, [])

    def test_new_refusal_with_the_previous_wait_reset_moves_instead_of_continuing(self) -> None:
        _ = self.owner("same-reset", host="ghostty")
        _ = self.transcript("same-reset", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        waited, = moves.read_moves()
        self.assertEqual(waited["kind"], "waited")
        WatchTime.at = reset
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as first_delivery:
            _ = self.cli()
        first_delivery.assert_called_once_with("session:same-reset", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-same-reset-refusal-one")

        WatchTime.at = reset + timedelta(seconds=1)
        self.reading(SOURCE_LOGIN, 80, five=0, at=WatchTime.at, five_reset=reset)
        _ = self.transcript("same-reset", "You've hit your session limit", at=WatchTime.at, entry_id="refusal-two")
        with (patch.object(accounts, "pick", wraps=accounts.pick) as pick,
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery):
            code, output, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        delivery.assert_not_called()
        pick.assert_called_once()
        self.assertNotIn("continued on its current account", output)
        self.assertEqual(len(self.moves.calls), 1)
        saved = refusal_watch._load_state()["sessions"]["same-reset"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["entry_id"], saved["state"]), ("refusal-two", "requested"))
        self.assertNotEqual(saved["move_id"], waited["move_id"])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual(self.moves.calls[0].session_id, "same-reset")

    def test_waited_reset_continues_once_on_its_own_account_and_new_refusal_is_decided_fresh(self) -> None:
        _ = self.owner("refused", host="ghostty")
        _ = self.transcript("refused", "You've hit your session limit · resets 11:30pm (America/New_York)")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(self.moves.calls, [])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited"])
        held = entries[0]
        assert held["kind"] == "waited"
        self.assertEqual(held["reset"], reset.isoformat())
        WatchTime.at = reset
        self.reading(TARGET_LOGIN, 1, at=reset, reset=reset + timedelta(days=1))
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery:
            _ = self.cli()
            _ = self.cli()
        delivery.assert_called_once_with("session:refused", "Quota wait ended", "Continue where you stopped.",
            key="quota-continue-refused-refusal-one")
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(self.process_dir(self.owners[0]["pid"]), account.ConfiguredClaudeFolder(accounts.account_dir("claude1")))
        _ = self.transcript("refused", "weekly limit", at=reset, entry_id="later-refusal")
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)

    def test_expired_wait_retries_a_window_skip_until_the_continuation_is_acknowledged(self) -> None:
        _ = self.owner("refused", host="ghostty")
        _ = self.transcript("refused", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        waited = moves.read_moves()[0]
        WatchTime.at = reset
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "SKIPPED: inside the key window\n", "")) as first:
            _, output, _ = self.cli()
        self.assertEqual(output.splitlines(), ["owner refused: continuation not delivered: inside the key window"])
        saved = refusal_watch._load_state()["sessions"]["refused"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["state"], saved["move_id"]), ("waited", waited["move_id"]))
        acknowledged = message_send.acknowledged_skip_reason("quota-continue-refused-refusal-one", reset.isoformat())
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, f"SKIPPED: {acknowledged}\n", "")) as retry:
            _, output, _ = self.cli()
            _ = self.cli()
        self.assertEqual(output.splitlines(), ["owner refused: continued on its current account"])
        saved = refusal_watch._load_state()["sessions"]["refused"]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((saved["state"], saved["move_id"]), ("continued", waited["move_id"]))
        self.assertEqual((first.call_count, retry.call_count), (1, 1))
        self.assertEqual(first.call_args.args, retry.call_args.args)
        self.assertEqual(self.moves.calls, [])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual([entry["move_id"] for entry in entries], [waited["move_id"], waited["move_id"]])

    def test_expired_wait_retries_failed_continuation_without_moving_or_repeating_wait_end(self) -> None:
        _ = self.owner("refused", host="ghostty")
        _ = self.transcript("refused", "You've hit your session limit")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        WatchTime.at = reset
        with patch.object(settle, "send_message", side_effect=[
            RuntimeError("socket unavailable\nextra detail"), settle.MessageQueued(kind="queued", reason="socket still closed"),
            settle.MessageSent(kind="sent")]) as delivery:
            _, output, _ = self.cli()
            self.assertEqual(output.splitlines(), ["owner refused: continuation not delivered: socket unavailable"])
            _, output, _ = self.cli()
            self.assertEqual(output.splitlines(), ["owner refused: continuation not delivered: socket still closed"])
            _ = self.cli()
            _ = self.cli()
        self.assertEqual(delivery.call_count, 3)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited", "wait ended"])

    def test_no_room_notice_ignores_check_times_but_tracks_allowance_reset_and_credit_values(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        self.reading("main@example.invalid", 0)
        self.reading(TARGET_LOGIN, 0)
        for login in ("main@example.invalid", TARGET_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.UnknownWindow(), refusals.NoResetKnown(), NOW))
        saved = usage_store.load()
        saved[usage_store.key("Codex", "credit@example.invalid")] = usage_store.StoredUsage(
            credits=usage_store.StoredCredits(balance="10", at=NOW.isoformat()))
        usage_store.write(saved)
        _ = self.cli()
        for reading in saved.values():
            for window in (reading.get("weekly"), reading.get("five_hour")):
                if window is not None:
                    window["at"] = (NOW + timedelta(minutes=2)).isoformat()
            if "credits" in reading:
                reading["credits"]["at"] = (NOW + timedelta(minutes=2)).isoformat()
        usage_store.write(saved)
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        self.assertEqual(len(self.notifications), 1)
        source = saved[usage_store.key("Claude", SOURCE_LOGIN)]
        assert "weekly" in source
        source["weekly"]["left"] = 1
        usage_store.write(saved)
        _ = self.cli()
        self.assertEqual(len(self.notifications), 2)
        source["weekly"]["resets"] = (WEEKLY_RESET + timedelta(days=7)).isoformat()
        usage_store.write(saved)
        _ = self.cli()
        self.assertEqual(len(self.notifications), 3)
        credit = saved[usage_store.key("Codex", "credit@example.invalid")]
        assert "credits" in credit
        credit["credits"]["balance"] = "9"
        usage_store.write(saved)
        _ = self.cli()
        self.assertEqual(len(self.notifications), 4)

    def test_rescan_preserves_first_refusal_reset_after_allowance_renews(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        reset = NOW + timedelta(minutes=5)
        self.reading(SOURCE_LOGIN, 0, reset=reset)
        self.reading("main@example.invalid", 0)
        self.reading(TARGET_LOGIN, 0)
        for login in ("main@example.invalid", TARGET_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.UnknownWindow(), refusals.NoResetKnown(), NOW))
        with patch.object(refusals, "write_refusal", wraps=refusals.write_refusal) as write:
            _ = self.cli()
            original = refusals.store_file().read_bytes()
            WatchTime.at = reset
            self.reading(SOURCE_LOGIN, 100, at=reset, reset=reset + timedelta(days=7))
            _ = self.cli()
            self.assertEqual(write.call_count, 1)
            self.assertEqual(refusals.store_file().read_bytes(), original)
            held = refusals.load()[refusals.key("claude", SOURCE_LOGIN)]
            self.assertEqual(held.reset, refusals.KnownReset(reset))
            self.assertTrue(refusals.resolved(held, refusals.stored_reading("claude", SOURCE_LOGIN, reset), reset))
            _ = self.transcript("refused", at=reset, entry_id="new-refusal")
            _ = self.cli()
            self.assertEqual(write.call_count, 2)
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].reset,
            refusals.KnownReset(reset + timedelta(days=7)))

    def test_dry_run_and_real_run_exclude_all_accounts_refused_in_this_tick(self) -> None:
        _ = self.owner("first")
        _ = self.owner("second", label="claude3")
        _ = self.transcript("first")
        _ = self.transcript("second")
        self.reading(SOURCE_LOGIN, 80, at=REFUSAL_AT - timedelta(seconds=1), reset=NOW + timedelta(hours=1))
        self.reading(TARGET_LOGIN, 80, at=REFUSAL_AT - timedelta(seconds=1), reset=NOW + timedelta(hours=2))
        before = self.snapshot()
        _, preview, _ = self.cli("--dry-run")
        self.assertIn("owner first: refused; move from claude1 to claude2", preview)
        self.assertIn("owner second: refused; move from claude3 to claude2", preview)
        self.assertEqual(self.snapshot(), before)
        _, actual, _ = self.cli()
        self.assertEqual("\n".join(line for line in preview.splitlines()[1:]
            if ": would check the shutdown hold" not in line) + "\n",
            "\n".join(line for line in actual.splitlines() if ": resumed on " not in line) + "\n")
        self.assertEqual(len([line for line in actual.splitlines() if ": resumed on " in line]), 2)
        self.assertEqual(len(self.moves.calls), 2)
        self.assertTrue(all(command.target == "claude2" for command in self.moves.calls))

    def test_transcript_lookup_ignores_deeper_folders_and_reads_each_live_owner_once_per_tick(self) -> None:
        _ = self.owner("refused")
        transcript = self.transcript("refused")
        nested = transcript.parent / "subagents" / transcript.name
        nested.parent.mkdir()
        _ = nested.write_text(json.dumps({"type": "assistant", "message": {"content": "normal reply"}}) + "\n")
        os.utime(nested, (transcript.stat().st_mtime + 60, transcript.stat().st_mtime + 60))
        with patch.object(refusal_watch, "_newest_exchange", wraps=refusal_watch._newest_exchange) as detect:  # pyright: ignore[reportPrivateUsage]
            _, output, _ = self.cli("--dry-run")
        self.assertIn("owner refused: refused; move", output)
        detect.assert_called_once_with(transcript)

    def test_refusal_pass_searches_projects_once_and_keeps_each_latest_exchange(self) -> None:
        for identity in ("first", "second", "continued"):
            _ = self.owner(identity)
            _ = self.transcript(identity)
        continued = self.transcript("continued", tail={"type": "user", "message": {"content": "continue"}})
        # A resumed copy in another project must still win by modification time.
        newer = continued.parent.parent / "resumed" / "first.jsonl"
        newer.parent.mkdir()
        _ = newer.write_text(json.dumps({"type": "user", "message": {"content": "continue"}}) + "\n")
        original = continued.parent / "first.jsonl"
        os.utime(newer, (original.stat().st_mtime + 60, original.stat().st_mtime + 60))
        expected = {identity: refusal_watch.session_refusal(identity)
                    for identity in ("first", "second", "continued")}
        def read_individually(identities: list[str]) -> dict[str, refusal_watch.TranscriptRefusal | refusal_watch.ProofRefusal | refusal_watch.NoSessionRefusal]:
            return {identity: refusal_watch.session_refusal(identity) for identity in identities}

        with patch.object(refusal_watch, "session_refusals", side_effect=read_individually):
            expected_code, expected_output, expected_error = self.cli("--dry-run")
        glob = Path.glob
        with (patch.object(Path, "glob", autospec=True, side_effect=glob) as search,
              patch.object(refusal_watch, "_newest_exchange", wraps=refusal_watch._newest_exchange) as read):  # pyright: ignore[reportPrivateUsage]
            code, output, error = self.cli("--dry-run")
        self.assertEqual((code, output, error), (expected_code, expected_output, expected_error))
        self.assertIn("owner second: refused; move", output)
        self.assertNotIn("owner first: refused", output)
        self.assertNotIn("owner continued: refused", output)
        self.assertEqual(read.call_count, 3)
        project_searches = [item for item in search.call_args_list
                            if item.args[0] == self.home / ".claude/projects"]
        self.assertLessEqual(len(project_searches), 1)
        self.assertEqual(expected, {identity: refusal_watch.session_refusal(identity)
                                   for identity in expected})
        # A later exchange must be read on the next pass, without a persistent cache.
        _ = self.transcript("continued", entry_id="new-refusal")
        _, next_output, _ = self.cli("--dry-run")
        self.assertIn("owner continued: refused; move", next_output)

    def test_refusal_pass_reads_known_transcripts_in_an_unlistable_project(self) -> None:
        _ = self.owner("refused")
        transcript = self.transcript("refused")
        scan = os.scandir

        def project_entries(path: str | Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if Path(path) == transcript.parent:
                raise PermissionError("project permits traversal only")
            return scan(path)

        with patch.object(os, "scandir", side_effect=project_entries):
            code, output, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertIn("owner refused: refused; move", output)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(shutdown_record.all_session_records(), [])

    def assert_five_hour_moves(self, reset: datetime) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "You've hit your session limit")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(shutdown_record.all_session_records(), [])

    def test_exactly_twenty_minutes_remaining_moves_instead_of_waiting(self) -> None:
        self.assert_five_hour_moves(NOW + timedelta(minutes=20))

    def test_past_five_hour_reset_moves_instead_of_waiting(self) -> None:
        self.assert_five_hour_moves(NOW - timedelta(seconds=1))

    def test_no_room_alerts_once_per_reading_then_records_room_again(self) -> None:
        _ = self.owner("first")
        _ = self.owner("second", host="terminal")
        _ = self.transcript("first")
        _ = self.transcript("second")
        self.reading("main@example.invalid", 0)
        self.reading(TARGET_LOGIN, 0)
        for login in ("main@example.invalid", TARGET_LOGIN):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.UnknownWindow(), refusals.NoResetKnown(), NOW))
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("earliest reset", self.notifications[0])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["no room", "no room"])
        self.assertEqual(self.moves.calls, [])
        self.reading(TARGET_LOGIN, 1, at=NOW + timedelta(seconds=1), reset=NOW + timedelta(days=1))
        WatchTime.at = NOW + timedelta(seconds=1)
        _ = self.cli()
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["no room", "no room", "room again", "room again"])
        self.assertEqual(entries[0]["move_id"], entries[2]["move_id"])
        self.assertEqual(entries[1]["move_id"], entries[3]["move_id"])
        self.assertEqual(len(self.moves.calls), 2)










    def test_assumed_wait_overrides_only_its_decision_and_never_stored_usage(self) -> None:
        _ = self.owner("assumed", host="ghostty")
        _ = self.owner("unrelated", host="terminal")
        before = usage_store.store_file().read_bytes()
        _, output, error = self.cli("--assume-refused", "owner assumed", "five-hour", "--reset-in", "19")
        self.assertEqual(error, "")
        self.assertIn("wait", output)
        self.assertNotIn("unrelated", output)
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        self.assertTrue(refusal_watch.assumed_file("assumed").exists())
        self.assertFalse(refusal_watch.assumed_file("unrelated").exists())
        self.assertEqual(self.moves.calls, [])

    def test_assumed_dry_run_writes_neither_assumption_nor_transition(self) -> None:
        _ = self.owner("assumed", host="ghostty")
        before = self.snapshot()
        _ = self.cli("--dry-run", "--assume-refused", "assumed", "weekly")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.moves.calls, [])

    def test_showrunner_batch_uses_the_same_move_sessions_path_as_watcher(self) -> None:
        _ = self.owner("first", host="herdr")
        _ = self.owner("second", host="herdr")
        _ = self.owner("unit", host="unit")
        requested = (stop_resume.Resumed("first", "w1:p1", "claude3"),
                     stop_resume.Resumed("second", "w1:p2", "claude3"))
        with patch.object(accounts, "move_sessions", return_value=requested) as move:
            result = accounts.move_group("showrunners", "claude3", why="weekly refusal")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        transition = accounts.group_transition("showrunners")
        assert isinstance(transition, accounts.GroupTransition)
        move.assert_called_once_with(("first", "second"), "claude3", why="weekly refusal")

    def test_named_window_without_known_reset_moves(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "You've hit your session limit")
        saved = usage_store.load()
        usage = saved[usage_store.key("Claude", SOURCE_LOGIN)]
        assert "five_hour" in usage
        usage["five_hour"]["resets"] = ""
        usage_store.write(saved)
        _ = self.cli()
        self.assertIsInstance(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].reset, refusals.NoResetKnown)
        self.assertEqual(len(self.moves.calls), 1)

    def test_refused_default_login_alias_excludes_canonical_default_candidate(self) -> None:
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.owner("alias-owner", label="claude2")
        _ = self.transcript("alias-owner", "Usage limit reached")
        # Both positive windows after the refusal resolve an unnamed refusal immediately.
        # Default resets first, so exclusion must still prevent a return to the same login.
        self.reading("main@example.invalid", 80, reset=NOW + timedelta(hours=1))
        _ = self.cli()
        self.assertEqual(len(self.moves.calls), 1)
        command = self.moves.calls[0]
        self.assertEqual(command.target, "claude3")
        self.assertNotEqual(command.target, "claude2")
        self.assertEqual(self.moves.calls[0].session_id, "alias-owner")
        self.assertEqual(self.owners[0].get("config_dir"), str(accounts.account_dir("claude3")))


if __name__ == "__main__":
    _ = unittest.main()
