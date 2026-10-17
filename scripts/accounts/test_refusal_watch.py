"""A provider refusal moves only its live owner, once, through safe shutdown."""
from __future__ import annotations

from scripts.whoami.note_file import note_name

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator, Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Self, cast, final, override
from unittest.mock import patch

from scripts.accounts import accounts, moves, next_moves, refusal_watch, refusals, role_map
from scripts.production import showrunners
import account
import agent_accounts
import usage_store

if TYPE_CHECKING:
    from scripts.shutdown import inventory, record, settle
    from scripts.message import sessions
else:
    inventory = import_module("inventory")
    record = import_module("record")
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
    commands: list[list[str]] = []

    @override
    def setUp(self) -> None:
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
        (self.home / ".claude").mkdir()
        (self.home / "notifier").mkdir()
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.home / "notifier"))
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        for label, login in (("claude1", SOURCE_LOGIN), ("claude2", "main@example.invalid"), ("claude3", TARGET_LOGIN)):
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
            _ = (self.home / "notes" / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        _ = (self.home / "notes/claude2.md").write_text("---\nlogin: main@example.invalid\n---\n")
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
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.down))
        self.notifications = []
        _ = self.enterContext(patch.object(refusal_watch, "_notify_no_room", side_effect=self.notified))
        self.commands = []
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
                          owner["host"]["production"] if owner["host"]["kind"] == "unit" else "")
            doc = (owner["doc"] if owner["kind"] == "showrunner" else
                   owner["host"]["doc"] if owner["host"]["kind"] == "unit" else "")
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
            owner: inventory.Session = inventory.UnitSession(**fields, kind="unit", host=inventory.UnitHost(
                kind="unit", production="fixture", unit=identity, doc=str(self.home / "fixture-production.md"), session_name=identity, session_host="tmux",
                plan=inventory.NoRunRecord(kind="no run record")))
        elif host == "tmux":
            owner = inventory.ShowrunnerSession(**fields, kind="showrunner", production="fixture", doc="/fixture.md",
                host=inventory.TmuxHost(kind="tmux", tmux_session=identity,
                    server=inventory.TmuxServerNotRecorded(kind="not recorded"),
                    session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
                    pane=inventory.PaneNotRecorded(kind="not recorded")))
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

    def notified(self, text: str, _now: datetime) -> None:
        self.notifications.append(text)

    def down(self, command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.assertTrue(any(part.endswith("/shutdown.py") for part in command), command)
        environment = _options.get("env")
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        self.assertEqual(cast(dict[str, object], environment).get("REFUSAL_WATCH_INVENTORY"), "read existing")
        self.commands.append(command)
        ids = command[command.index("--only") + 1].split(",")
        target = command[command.index("--to") + 1]
        why = command[command.index("--why") + 1]
        if why.startswith("auto: "):
            self.assertIn("--auto", command)
        for identity in ids:
            owner = next(owner for owner in self.owners if owner["session_id"] == identity)
            source = self.process_account(owner["pid"], home_state=account.home_account_state())
            assert source is not None
            saved = record.SessionShutdown(login=source.login, label=source.label, machine="natedev",
                requested_at=WatchTime.at.isoformat(), request_id=f"batch-{len(self.commands)}",
                requested_by=record.FromTerminal(kind="terminal"), hold="session", then="restart",
                force="wait for ready", alerts="none" if "--no-alert" in command else "user", probe=False,
                session=owner, workers=[], timers=[],
                settle_message=record.SettleMessageNotSent(kind="not sent"), where=record.WhereNotSaid(kind="not said"),
                progress=record.SessionWaiting(kind="waiting"), trail=[], stop_issues=[], target=target,
                source_label=source.label, why=why, move_id=command[command.index("--move-id") + 1])
            self.assertTrue(record.create_session(saved))
        return subprocess.CompletedProcess(command, 0, "added\n", "")

    def groups(self, units: str = "claude1", showrunners: str = "claude3") -> Path:
        path = self.home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def next_entries(self, *entries: next_moves.NextMove) -> None:
        with next_moves.next_moves_lock():
            next_moves.write_next_moves(entries)

    def low_rule(self, name: str = "units-low", *, group: accounts.ClaudeGroup = "units",
                 target: str = "claude2", threshold: float = 1) -> next_moves.NextMove:
        return next_moves.NextMove(name, group, threshold, 0, target, NOW)

    def hold_rule(self, name: str = "units-low", *, target: str = "claude2",
                  minutes: float = 10) -> next_moves.NextMove:
        return next_moves.NextMove(name, "units", 1, minutes, target, NOW)

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
        read_text = Path.read_text
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

                def read_with_failure(file: Path, encoding: str | None = None,
                                      errors: str | None = None) -> str:
                    if file == history and problem == "permission":
                        raise PermissionError("fixture history unreadable")
                    return read_text(file, encoding=encoding, errors=errors)

                with patch.object(Path, "read_text", read_with_failure):
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

    def test_removing_an_entry_drops_only_its_hold_without_needing_another_timer_pass(self) -> None:
        _ = self.groups()
        first, second = self.hold_rule("entry-one"), self.hold_rule("entry-two")
        self.next_entries(first, second)
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        before = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(before.get("auto_move_holds", {})), {first.id, second.id})
        next_moves.remove_next_move(first.id)
        self.assertEqual(next_moves.read_next_moves(), (second,))
        after = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(after.get("auto_move_holds", {}),
                         {second.id: before.get("auto_move_holds", {})[second.id]})

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

    def test_held_chain_uses_a_later_target_when_the_first_is_unavailable_or_low(self) -> None:
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
                code, _, error = self.cli()
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(stored_group_labels(path)["units"], "claude2")
                self.assertEqual(next_moves.read_next_moves(), (first,))
                self.assertEqual(refusal_watch._load_state().get("auto_move_holds", {}), {})  # pyright: ignore[reportPrivateUsage]

    def test_a_group_moved_by_a_refusal_keeps_its_later_next_entry(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.transcript("refused-unit", at=NOW)
        entry = self.low_rule("later", target="claude3")
        self.next_entries(entry)
        self.reading(SOURCE_LOGIN, 0)
        self.reading("main@example.invalid", 1)
        self.reading(TARGET_LOGIN, 90)
        placed = accounts.GroupPlacement("units", "claude2", accounts.account_dir("claude2"), accounts.UnitsResetSoonest())
        with patch.object(accounts, "placement", return_value=placed):
            code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([entry["cause"] for entry in history], ["refusal"])

    def test_low_rule_persists_its_hold_and_moves_only_after_the_full_interval(self) -> None:
        path = self.groups()
        self.next_entries(self.hold_rule())
        self.reading(SOURCE_LOGIN, 1)
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        state_file = self.home / ".local/state/accounts/refusal_watch.json"
        document = cast(dict[str, object], json.loads(state_file.read_text()))
        self.assertEqual(document["auto_move_holds"], {"units-low": {
            "account": "claude1", "at": NOW.isoformat(), "move_id": "", "percent": 1}})
        loaded = refusal_watch._load_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(loaded.get("auto_move_holds", {})["units-low"],
                         refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1))
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
                                 refusal_watch.HoldStarted("claude1", NOW, accounts.NoGroupTransition(), 1))
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
                                 refusal_watch.HoldStarted("claude3", NOW + timedelta(minutes=10), transition.move_id, 1))

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
                    "claude1", WatchTime.at, returned.move_id, 1))
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
        self.next_entries(next_moves.NextMove("units-low", "units", 0, 10, "claude2", NOW))
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
                    "claude1", WatchTime.at, accounts.NoGroupTransition(), 0))
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

    def test_automatic_low_rule_holds_at_two_percent_and_moves_at_one_with_one_transition(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 2)
        _ = self.cli()
        self.assertEqual(self.commands, [])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.reading(SOURCE_LOGIN, 1)
        _, output, error = self.cli()
        self.assertEqual(error, "")
        self.assertIn("units-low", output)
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertIn("--no-alert", self.commands[0])
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(history), 1)
        self.assertEqual(len(requested), 1)
        self.assertEqual((history[0]["cause"], history[0]["why"], history[0]["label"]),
                         ("auto", "auto: units-low", "claude2"))
        self.assertEqual((requested[0].get("cause"), requested[0]["why"], requested[0]["to"]),
                         ("auto", "auto: units-low", "claude2"))
        self.assertEqual(requested[0]["move_id"], history[0]["move_id"])
        self.assertEqual(self.notifications, [])
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)

    def test_automatic_low_rule_requires_a_fresh_source_reading(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        self.reading(SOURCE_LOGIN, 1, at=NOW - timedelta(minutes=11))
        _ = self.cli()
        self.assertEqual(self.commands, [])
        self.assertEqual(stored_group_labels(path)["units"], "claude1")
        self.reading(SOURCE_LOGIN, 1)
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(len(self.commands), 1)

    def test_retained_stopped_unit_record_does_not_block_an_automatic_low_rule(self) -> None:
        path = self.groups()
        _ = self.owner("old-unit", host="unit")
        self.next_entries(self.low_rule())
        _ = self.down(["/fake/shutdown.py", "--only", "old-unit", "--to", "claude3",
                       "--why", "older stop", "--move-id", "older-transition"],
                      env={"REFUSAL_WATCH_INVENTORY": "read existing"})
        saved = record.all_session_records()[0]
        def finish(current: record.SessionShutdown) -> None:
            current["then"] = "stop"
            current["progress"] = record.SessionStopped(kind="stopped", at=NOW.isoformat())
        saved = record.update_session(saved["login"], "old-unit", finish)
        self.owners.clear()
        self.commands.clear()
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(record.all_session_records(), [saved])
        self.assertEqual(self.commands, [])

    def test_automatic_completion_retry_preserves_the_rule_reason_and_transition_in_the_journal(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        with patch.object(accounts, "move_sessions", return_value=(
                accounts.SessionMoveRefused("unit", "synthetic delivery refusal"),)):
            _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        self.assertEqual(record.all_session_records(), [])
        history = accounts.roles_history_file().read_bytes()
        transition = cast(dict[str, object], json.loads(path.read_text())["units"])
        decoded = accounts.group_transition("units")
        assert isinstance(decoded, accounts.GroupTransition)
        self.assertEqual(decoded.why, accounts.RecordedTransitionReason("auto: units-low"))
        code, _, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requested), 1)
        self.assertEqual((requested[0].get("cause"), requested[0]["why"], requested[0]["move_id"]),
                         ("auto", "auto: units-low", transition["move_id"]))
        self.assertEqual(accounts.roles_history_file().read_bytes(), history)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--auto", self.commands[0])

    def test_unavailable_automatic_target_is_reported_and_a_later_valid_rule_still_moves(self) -> None:
        path = self.groups()
        self.next_entries(self.low_rule("missing-target", target="claude99"), self.low_rule("valid-target"))
        code, output, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertIn("missing-target", output)
        self.assertIn("claude99", output)
        self.assertIn("valid-target", output)
        self.assertEqual(stored_group_labels(path)["units"], "claude2")
        history = [cast(dict[str, object], json.loads(line))
                   for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual([entry["why"] for entry in history], ["auto: valid-target"])

    def test_automatic_rule_refuses_low_stale_missing_and_unresolved_target_readings(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        for state in ("at threshold", "below threshold", "stale", "missing", "unresolved refusal"):
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
                self.assertEqual(self.commands, [])
                self.assertEqual(stored_group_labels(path)["units"], "claude1")
                self.assertFalse(accounts.roles_history_file().exists())
                self.assertEqual(next_moves.read_next_moves(), (self.low_rule(),))

    def test_automatic_rule_does_not_prepare_or_reselect_the_current_account(self) -> None:
        path = self.groups(units="claude2")
        _ = self.owner("unit", host="unit", label="claude2")
        self.next_entries(self.low_rule())
        self.reading("main@example.invalid", 1)
        before = path.read_bytes()
        with patch.object(accounts, "prepared_account", side_effect=AssertionError("same-account rule prepared a move")):
            _ = self.cli()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.commands, [])

    def test_automatic_rules_allow_both_groups_to_share_one_target_and_move_only_off_target_members(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.owner("arrived-unit", host="unit", label="claude2")
        _ = self.owner("runner", host="tmux", label="claude3")
        self.reading(SOURCE_LOGIN, 1)
        self.reading(TARGET_LOGIN, 1)
        self.next_entries(self.low_rule(), self.low_rule("showrunners-low", group="showrunners"))
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude2", "showrunners": "claude2"})
        self.assertEqual({identity for command in self.commands
                          for identity in command[command.index("--only") + 1].split(",")}, {"unit", "runner"})
        self.assertTrue(all("--no-alert" in command for command in self.commands))
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
        self.assertEqual(self.commands, [])

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
        self.assertEqual(self.commands, [])

    def test_automatic_rule_keeps_a_group_with_an_open_transition_on_its_committed_target(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = accounts.move_group("units", "claude3", why="pending hand move")
        self.assertEqual(len(record.all_session_records()), 1)
        history = accounts.roles_history_file().read_bytes()
        self.next_entries(self.low_rule())
        _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual(accounts.roles_history_file().read_bytes(), history)
        self.assertEqual(len(self.commands), 1)


    def test_automatic_move_uses_the_refusal_showrunner_notice_without_a_phone_alert(self) -> None:
        _ = self.groups()
        _ = self.owner("unit", host="unit")
        self.next_entries(self.low_rule())
        _ = self.cli()
        saved = record.all_session_records()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["alerts"], "none")
        with (patch.object(import_module("showrunners"), "current_name", return_value="fixture-showrunner"),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery,
              patch.object(settle, "alert_user") as phone):
            settle.tell_showrunner_stopping(saved[0])
            settle.tell_showrunner_stopping(record.all_session_records()[0])
        delivery.assert_called_once()
        self.assertEqual(delivery.call_args.args[0], "fixture-showrunner")
        self.assertIn("auto: units-low", cast(str, delivery.call_args.args[1]))
        self.assertIn("claude2", cast(str, delivery.call_args.args[2]))
        phone.assert_not_called()
        self.assertEqual(self.notifications, [])

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
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][self.commands[0].index("--only") + 1], "probe")
        self.assertNotEqual(self.commands[0][self.commands[0].index("--to") + 1], "claude1")
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
        self.assertEqual(self.commands, [])
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

    def test_malformed_request_ids_do_not_prevent_a_new_session_move(self) -> None:
        _ = self.owner("moving")
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        invalid_ids: tuple[object, ...] = ([], "")
        malformed: list[dict[str, object]] = [{"kind": "requested", "at": NOW.isoformat(), "tool": "claude",
            "subject": "old: old owner", "move_id": "old-transition", "request_id": value,
            "from": "claude1", "to": "claude3", "why": "old move", "window": "unknown"}
            for value in invalid_ids]
        _ = path.write_text("".join(json.dumps(entry) + "\n" for entry in malformed))
        result = accounts.move_sessions(("moving",), "claude3", why="operator move", origin=accounts.HandOrigin())
        self.assertIsInstance(result[0], accounts.SessionMoveRequested)
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requested), 1)
        self.assertTrue(requested[0]["subject"].startswith("moving: "))

    def test_hand_group_dispatch_keeps_its_committed_transition_when_another_selection_replaces_it(self) -> None:
        _ = self.groups()
        _ = self.owner("member", host="unit")
        select = accounts.set_group_account

        def superseded(group: accounts.ClaudeGroup, label: str, *, why: str,
                       now: datetime | accounts.AccountDecisionNow = accounts.ACCOUNT_DECISION_NOW,
                       origin: accounts.MoveOrigin = accounts.HAND_ORIGIN, move_id: str = "") -> accounts.GroupTransition:
            committed = select(group, label, why=why, now=now, origin=origin, move_id=move_id)
            _ = select(group, "claude1", why="newer selection", now=now,
                origin=accounts.RefusalOrigin("five_hour", refusals.KnownReset(FIVE_HOUR_RESET)), move_id="replacement")
            return committed

        with patch.object(accounts, "set_group_account", side_effect=superseded):
            result = accounts.move_group("units", "claude3", why="operator group move")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        history = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
        request = next(entry for entry in moves.read_moves() if entry["kind"] == "requested")
        self.assertEqual((request["to"], request["move_id"], request.get("cause"), request["window"]),
                         (history[0]["label"], history[0]["move_id"], history[0]["cause"], history[0]["window"]))
        self.assertNotIn("reset", request)
        self.assertEqual(accounts.group_label(accounts.group_accounts(), "units"), "claude1")
        self.assertNotEqual(request["move_id"], history[1]["move_id"])

    def test_refusal_group_dispatch_keeps_its_committed_transition_when_a_hand_selection_replaces_it(self) -> None:
        _ = self.groups()
        _ = self.owner("member", host="unit")
        _ = self.transcript("member")
        select = accounts.set_group_account

        def superseded(group: accounts.ClaudeGroup, label: str, *, why: str,
                       now: datetime | accounts.AccountDecisionNow = accounts.ACCOUNT_DECISION_NOW,
                       origin: accounts.MoveOrigin = accounts.HAND_ORIGIN, move_id: str = "") -> accounts.GroupTransition:
            committed = select(group, label, why=why, now=now, origin=origin, move_id=move_id)
            _ = select(group, "claude1", why="newer hand selection", now=now,
                origin=accounts.HandOrigin(), move_id="replacement")
            return committed

        with patch.object(accounts, "set_group_account", side_effect=superseded):
            _ = self.cli()
        history = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
        request = next(entry for entry in moves.read_moves() if entry["kind"] == "requested")
        self.assertEqual((request["to"], request["move_id"], request.get("cause"), request["window"], request.get("reset")),
                         (history[0]["label"], history[0]["move_id"], history[0]["cause"], history[0]["window"], history[0]["reset"]))
        self.assertEqual(accounts.group_label(accounts.group_accounts(), "units"), "claude1")
        self.assertNotEqual(request["move_id"], history[1]["move_id"])

    def test_transcript_refusal_writes_the_account_once_and_carries_its_window_and_reset(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        write = refusals.write_refusal
        with patch.object(refusals, "write_refusal", wraps=write) as saved:
            _ = self.cli()
            _ = self.cli()
        saved.assert_called_once()
        self.assertIsInstance(refusal_watch.session_refusal("refused"), refusal_watch.TranscriptRefusal)
        entries = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].get("cause"), entries[0]["window"], entries[0].get("reset")),
                         ("refusal", "weekly", WEEKLY_RESET.isoformat()))

    def test_assumed_refusal_rejects_both_real_group_kinds_without_changing_selections(self) -> None:
        groups = self.groups()
        for identity, host in (("unit", "unit"), ("runner", "tmux")):
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
        self.assertEqual(self.commands, [])

    def test_proof_wait_continuation_deletes_only_its_assumption_with_routing_entries_present(self) -> None:
        _ = self.owner("probe")
        _ = self.owner("other")
        _ = self.cli("--assume-refused", "probe", "five-hour", "--reset-in", "19")
        other = refusal_watch.assumed_file("other")
        _ = other.write_text(json.dumps({"uuid": "unrelated", "timestamp": NOW.isoformat(),
            "window": "weekly", "reset": ""}))
        routing: moves.RoutedToClaude = {"kind": "routed to claude", "at": NOW.isoformat(),
            "tool": "codex", "fallback_at": NOW.isoformat(), "owner": "codex1", "cause": "refusal", "moved": ["delegate"]}
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
        self.assertEqual(self.commands, [])

    def test_proof_group_moves_only_the_named_probes_and_never_reads_real_group_state(self) -> None:
        _ = self.owner("probe-one")
        _ = self.owner("probe-two", host="ghostty", label="claude3")
        _ = self.owner("unit", host="unit")
        _ = self.owner("runner", host="tmux", label="claude3")
        groups = self.groups()
        before = groups.read_bytes()
        with (patch.object(accounts, "group_accounts", side_effect=AssertionError("proof must not read real groups")),
              patch.object(accounts, "move_sessions", wraps=accounts.move_sessions) as move):
            _, _, error = self.cli("--assume-refused", "probe-one", "weekly", "--proof-group", "probe-one", "probe-two")
        self.assertEqual(error, "")
        move.assert_called_once()
        self.assertEqual(set(cast(tuple[str, ...], move.call_args.args[0])), {"probe-one", "probe-two"})
        self.assertEqual({command[command.index("--to") + 1] for command in self.commands}, {"claude2"})
        self.assertEqual({identity for command in self.commands for identity in command[command.index("--only") + 1].split(",")},
                         {"probe-one", "probe-two"})
        self.assertEqual({saved["session"]["session_id"] for saved in record.all_session_records()}, {"probe-one", "probe-two"})
        self.assertEqual(groups.read_bytes(), before)
        self.assertFalse(accounts.roles_history_file().exists())
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requested), 2)
        self.assertEqual(len({entry["move_id"] for entry in requested}), 1)
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as delivery:
            for saved in record.all_session_records():
                settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual({call.args[0] for call in delivery.call_args_list}, {"session:probe-one", "session:probe-two"})
        self.assertEqual(delivery.call_count, 2)
        self.assertTrue(all("Pause for move" in str(call.args) for call in delivery.call_args_list))

    def test_hand_move_does_not_inherit_an_account_refusal_and_status_names_its_cause(self) -> None:
        _ = self.owner("manual")
        refusals.write_refusal(refusals.AccountRefusal("claude", SOURCE_LOGIN,
            refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), REFUSAL_AT))
        result = accounts.move_sessions(("manual",), "claude3", why="operator move", origin=accounts.HandOrigin())
        self.assertIsInstance(result[0], accounts.SessionMoveRequested)
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requested), 1)
        self.assertEqual((requested[0].get("cause"), requested[0]["window"]), ("by hand", "unknown"))
        self.assertNotIn("reset", requested[0])
        self.assertIn("by hand", "\n".join(accounts._move_status_lines(moves.read_moves(), timezone.utc)))  # pyright: ignore[reportPrivateUsage]

    def test_group_reconciliation_after_an_updater_restart_reuses_the_durable_origin_and_transition(self) -> None:
        for refused in (False, True):
            with self.subTest(refused=refused):
                self.owners = []
                self.live = []
                self.commands = []
                for saved in record.all_session_records():
                    record.archive_session(saved["login"], saved["session"]["session_id"])
                accounts.roles_history_file().unlink(missing_ok=True)
                moves.store_file().unlink(missing_ok=True)
                (self.home / ".local/state/accounts/refusal_watch.json").unlink(missing_ok=True)
                _ = self.groups()
                first = self.owner("first-" + str(refused), host="unit")
                if refused:
                    _ = self.transcript(first["session_id"])
                    _ = self.cli()
                else:
                    _ = accounts.move_group("units", "claude3", why="operator group move")
                selection = cast(dict[str, object], json.loads(accounts.groups_file().read_text())["units"])
                first_request = [entry for entry in moves.read_moves() if entry["kind"] == "requested"][0]
                expected = "refusal" if refused else "by hand"
                self.assertEqual(first_request.get("cause"), expected)
                self.assertEqual(first_request["move_id"], selection["move_id"])
                rows = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["move_id"], rows[0]["cause"]), (selection["move_id"], expected))
                record.archive_session(SOURCE_LOGIN, first["session_id"])
                first["config_dir"] = str(accounts.account_dir("claude3"))
                late = self.owner("late-" + str(refused), host="unit")
                # Every run reopens groups.json and refusal_watch.json; no
                # in-memory selection is supplied to this later updater pass.
                WatchTime.at = NOW + timedelta(minutes=2)
                _ = self.cli()
                requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
                self.assertEqual(len(requested), 2)
                self.assertEqual({entry["move_id"] for entry in requested}, {selection["move_id"]})
                self.assertEqual([entry.get("cause") for entry in requested], [expected, expected])
                self.assertEqual([entry["window"] for entry in requested], ["weekly" if refused else "unknown"] * 2)
                self.assertEqual(requested[0].get("reset"), requested[1].get("reset"))
                self.assertNotEqual(requested[0].get("request_id"), requested[1].get("request_id"))
                self.assertTrue(requested[-1]["subject"].startswith(late["session_id"] + ":"))

    def test_hand_group_selection_without_live_members_is_reused_for_a_later_member(self) -> None:
        _ = self.groups()
        _ = accounts.move_group("units", "claude3", why="select before starting work")
        selection = cast(dict[str, object], json.loads(accounts.groups_file().read_text())["units"])
        self.assertTrue(selection["move_id"])
        self.assertEqual(moves.read_moves(), [])
        _ = self.owner("late", host="unit")
        _ = self.cli()
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]["move_id"], requests[0].get("cause")), (selection["move_id"], "by hand"))

    def test_fast_archived_move_returns_the_attempt_id_separately_from_the_transition_id(self) -> None:
        owner = self.owner("fast")
        def already_completed(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            request: moves.RequestedMove = {"kind": "requested", "at": NOW.isoformat(), "tool": "claude",
                "subject": "fast: " + owner["name"], "move_id": "group-transition", "request_id": "fast-attempt",
                "from": "claude1", "to": "claude3", "why": "hand move", "cause": "by hand", "window": "unknown"}
            moves.append_move(request)
            moves.append_move(moves.ConfirmedMove(kind="confirmed", at=NOW.isoformat(), tool="claude",
                subject=request["subject"], move_id="group-transition", config_dir=str(accounts.account_dir("claude3"))))
            return subprocess.CompletedProcess(command, 0, "already restarted", "")
        with patch.object(subprocess, "run", side_effect=already_completed):
            outcomes = accounts.move_sessions(("fast",), "claude3", why="hand move", origin=accounts.HandOrigin(), move_id="group-transition")
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertIsInstance(outcome, accounts.SessionMoveRequested)
        assert isinstance(outcome, accounts.SessionMoveRequested)
        self.assertEqual((outcome.request_id, outcome.move_id), ("fast-attempt", "group-transition"))
        self.assertEqual(record.all_session_records(), [])

    def test_units_group_preview_lists_exactly_off_target_members_without_writes(self) -> None:
        off = self.owner("off-target", host="unit")
        _ = self.owner("already-there", host="unit", label="claude3")
        _ = self.owner("other-group", host="tmux")
        before = self.snapshot()
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "units", "claude3", "--dry-run"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertIn("units: would select claude3", output.getvalue())
        self.assertIn(f"{off['name']}: {off['session_id']}", output.getvalue())
        self.assertIn("reap and restart now", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(accounts.groups_file().exists())
        self.assertFalse(accounts.roles_history_file().exists())
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual(record.all_session_records(), [])
        self.assertEqual(self.commands, [])

    def test_units_group_preview_on_current_account_prints_no_live_session_moves(self) -> None:
        _ = self.owner("already-there", host="unit")
        self.reading(SOURCE_LOGIN, 80)
        before = self.snapshot()
        result = accounts.move_group("units", "claude1", dry_run=True)
        self.assertIsInstance(result, accounts.GroupMoveDryRun)
        assert isinstance(result, accounts.GroupMoveDryRun)
        self.assertEqual(result.session_ids, ())
        self.assertEqual(result.lines, ("units: would select claude1", "units: no live session moves"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.commands, [])

    def test_units_group_move_returns_group_named_started_outcome(self) -> None:
        _ = self.owner("off-target", host="unit")
        _ = self.owner("already-there", host="unit", label="claude3")
        result = accounts.move_group("units", "claude3", why="checkpoint move")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        assert isinstance(result, accounts.GroupMoveStarted)
        self.assertEqual(result.session_ids, ("off-target",))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(tuple(self.commands[0][2:12]), ("down", "claude1", "--here", "--only", "off-target",
            "--restart", "--to", "claude3", "--why", "checkpoint move"))
        transition = accounts.group_transition("units")
        assert isinstance(transition, accounts.GroupTransition)
        self.assertEqual(tuple(self.commands[0][12:]), ("--move-id", transition.move_id))

    def test_units_group_failed_request_returns_group_named_failure(self) -> None:
        _ = self.owner("off-target", host="unit")
        refused = accounts.SessionMoveRefused("off-target", "stop request failed", returncode=2,
                                             stderr="stop request failed\n")
        with patch.object(accounts, "move_sessions", return_value=(refused,)):
            result = accounts.move_group("units", "claude3")
        self.assertIsInstance(result, accounts.GroupMoveStartFailed)
        assert isinstance(result, accounts.GroupMoveStartFailed)
        self.assertEqual(result.session_ids, ("off-target",))
        self.assertEqual(result.returncode, 2)
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
        self.assertEqual(self.commands, [])

    def test_example_groups_move_only_after_their_own_refusals(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        runner = self.owner("runner", host="tmux", label="claude3")
        self.reading("main@example.invalid", 0, reset=NOW + timedelta(days=2))
        self.reading(TARGET_LOGIN, 3, reset=NOW + timedelta(days=4))
        self.reading(SOURCE_LOGIN, 53, reset=NOW + timedelta(days=6))
        _ = self.cli()
        self.assertEqual(self.commands, [])
        _ = self.transcript("runner")
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude1"})
        command = self.commands[0]
        self.assertEqual(command[command.index("--only") + 1], "runner")
        self.assertEqual(command[command.index("--to") + 1], "claude1")
        record.archive_session(TARGET_LOGIN, "runner")
        runner["config_dir"] = str(accounts.account_dir("claude1"))
        _ = self.transcript("unit")
        _ = self.cli()
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude1"})
        self.assertEqual(len(self.commands), 1)

    def test_group_selection_is_stored_before_shutdown_requests_are_sent(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.transcript("unit")
        original = accounts.move_sessions
        def requested(ids: tuple[str, ...], target: str, *, why: str, origin: accounts.MoveOrigin, move_id: str = "") -> tuple[accounts.SessionMoveRequested | accounts.SessionMoveRefused, ...]:
            self.assertEqual(stored_group_labels(path)["units"], target)
            return original(ids, target, why=why, origin=origin, move_id=move_id)
        with patch.object(accounts, "move_sessions", side_effect=requested):
            _ = self.cli()
        history = [cast(dict[str, object], json.loads(line)) for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["group"], history[0]["label"]), ("units", "claude3"))
        self.assertIn("refused", str(history[0]["why"]))

    def test_manual_showrunner_group_command_needs_only_destination(self) -> None:
        path = self.groups(showrunners="claude1")
        _ = self.owner("runner-one", host="tmux")
        _ = self.owner("runner-two", host="tmux")
        _ = self.owner("unit", host="unit")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "showrunners", "claude3"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(stored_group_labels(path), {"units": "claude1", "showrunners": "claude3"})
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(set(command[command.index("--only") + 1].split(",")), {"runner-one", "runner-two"})

    def test_refusal_of_one_unit_moves_every_live_unit_and_leaves_showrunners(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.owner("healthy-unit", host="unit")
        _ = self.owner("healthy-showrunner", host="tmux", label="claude3")
        _ = self.transcript("refused-unit")
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(set(command[command.index("--only") + 1].split(",")), {"refused-unit", "healthy-unit"})
        self.assertEqual(tuple(command[2:4]), ("down", "claude1"))
        self.assertIn("--restart", command)
        self.assertIn("--here", command)
        self.assertNotIn("now", command)
        self.assertEqual(command[command.index("--to") + 1], "claude3")
        self.assertEqual(stored_group_labels(path), {"units": "claude3", "showrunners": "claude3"})

    def test_classified_showrunner_without_update_timer_moves_with_its_group(self) -> None:
        path = self.groups(showrunners="claude1")
        refused = self.owner("refused-runner", host="tmux")
        healthy = self.owner("runner-without-timer", host="tmux")
        _ = self.owner("unit", host="unit")
        self.assertEqual(healthy["kind"], "showrunner")
        self.assertEqual(healthy["timers"], [])
        _ = self.transcript(refused["session_id"])
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(set(command[command.index("--only") + 1].split(",")),
            {refused["session_id"], healthy["session_id"]})
        self.assertEqual(command[command.index("--to") + 1], "claude2")
        self.assertEqual(stored_group_labels(path)["showrunners"], "claude2")
        self.assertEqual(stored_group_labels(path)["units"], "claude1")

    def test_healthy_groups_stay_on_their_accounts_despite_better_placement(self) -> None:
        path = self.groups()
        _ = self.owner("unit", host="unit")
        _ = self.owner("runner", host="tmux", label="claude3")
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(self.commands, [])
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
            self.assertEqual(self.commands, [])
            WatchTime.at = NOW + timedelta(minutes=2)
            _ = self.cli()
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(self.snapshot(), before)

    def test_partial_group_move_retries_only_off_account_members_without_open_records(self) -> None:
        path = self.groups()
        first = self.owner("refused-unit", host="unit")
        second = self.owner("healthy-unit", host="unit")
        _ = self.transcript("refused-unit")
        original = self.down
        def partial(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            changed = list(command)
            changed[changed.index("--only") + 1] = first["session_id"]
            return original(changed, **options)
        with patch.object(subprocess, "run", side_effect=partial):
            _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual(len(record.all_session_records()), 1)
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        self.assertEqual(len(self.commands), 2)
        command = self.commands[1]
        self.assertEqual(command[command.index("--only") + 1], second["session_id"])
        requested = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requested), 2)
        self.assertEqual({entry["subject"].split(": ", 1)[0] for entry in requested}, {first["session_id"], second["session_id"]})
        _ = self.cli()
        self.assertEqual(len(self.commands), 2)

    def test_partial_move_keeps_committed_target_when_refusing_member_failed_before_record(self) -> None:
        path = self.groups()
        refused = self.owner("refused-unit", host="unit")
        healthy = self.owner("healthy-unit", host="unit")
        _ = self.transcript(refused["session_id"])
        original = self.down
        def healthy_only(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(set(command[command.index("--only") + 1].split(",")),
                {refused["session_id"], healthy["session_id"]})
            changed = list(command)
            changed[changed.index("--only") + 1] = healthy["session_id"]
            return original(changed, **options)
        with patch.object(subprocess, "run", side_effect=healthy_only):
            _ = self.cli()
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual({saved["session"]["session_id"] for saved in record.all_session_records()},
            {healthy["session_id"]})
        first = moves.read_moves()
        self.assertTrue(any(entry["kind"] == "failed" and entry["subject"].startswith("refused-unit: ") for entry in first))
        WatchTime.at = NOW + timedelta(minutes=2)
        self.reading("main@example.invalid", 80, at=WatchTime.at, reset=WatchTime.at + timedelta(hours=1))
        _ = self.cli()
        self.assertEqual(len(self.commands), 2)
        command = self.commands[1]
        self.assertEqual(command[command.index("--only") + 1], refused["session_id"])
        self.assertEqual(command[command.index("--to") + 1], "claude3")
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        requests = [entry["subject"].split(": ", 1)[0] for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertCountEqual(requests, [healthy["session_id"], refused["session_id"]])
        _ = self.cli()
        self.assertEqual(len(self.commands), 2)

    def test_five_hour_wait_holds_whole_group_without_reassigning_healthy_members(self) -> None:
        path = self.groups()
        _ = self.owner("refused-unit", host="unit")
        _ = self.owner("healthy-unit", host="unit")
        _ = self.transcript("refused-unit", "You've hit your session limit")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=NOW + timedelta(minutes=19))
        before = path.read_bytes()
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(self.commands, [])
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
        self.assertEqual(self.commands, [])
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
        self.assertEqual(self.commands, [])
        self.assertEqual(record.all_session_records(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_partial_group_reconciliation_skips_unresolved_target_refusal_without_new_transcript(self) -> None:
        """F007: reconciliation must check the selected target's existing refusal."""
        path = self.groups(units="claude3")
        _ = self.owner("remaining-unit", host="unit", label="claude1")
        refusals.write_refusal(refusals.AccountRefusal("claude", TARGET_LOGIN,
            refusals.KnownWindow("weekly"), refusals.KnownReset(WEEKLY_RESET), NOW))
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(self.commands, [])
        self.assertEqual(record.all_session_records(), [])
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
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(command[command.index("--to") + 1], "claude2")

    def test_status_without_group_file_does_not_infer_live_members_accounts(self) -> None:
        """F004: read-only status reports missing configuration despite live members."""
        _ = self.owner("unit-one", host="unit")
        _ = self.owner("runner", host="tmux", label="claude3")
        before = self.snapshot()
        self.assertIn("units: not configured", accounts.status_report())
        self.assertIn("showrunners: not configured", accounts.status_report())
        self.assertEqual(self.snapshot(), before)

    def test_refused_unconfigured_groups_report_setup_and_move_no_members(self) -> None:
        """F004: neither real nor preview ticks infer a missing group's account."""
        for identity, host in (("unit-one", "unit"), ("unit-two", "unit"), ("runner", "tmux")):
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
                self.assertEqual(self.commands, [])
                self.assertEqual(record.all_session_records(), [])

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
        _ = self.owner("runner", host="tmux", label="claude3")
        _ = self.transcript("scratch")
        before = path.read_bytes()
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(command[command.index("--only") + 1], "scratch")
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
        self.assertEqual(self.commands, [])

    def test_manual_group_move_sets_account_and_requests_all_members_once(self) -> None:
        path = self.groups()
        _ = self.owner("one", host="unit")
        _ = self.owner("two", host="unit")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["move", "units", "claude3"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(stored_group_labels(path)["units"], "claude3")
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(set(command[command.index("--only") + 1].split(",")), {"one", "two"})

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
        for host in ("tmux", "unit", "ghostty", "zed", "terminal"):
            for window, text in (("weekly", "weekly limit"), ("five", "You've hit your session limit")):
                identity = host + "-" + window
                _ = self.owner(identity, host=host)
                _ = self.transcript(identity, text)
        before = self.snapshot()
        code, output, error = self.cli("--dry-run")
        self.assertIn(code, (0, None))
        self.assertEqual(error, "")
        for host in ("tmux", "unit", "ghostty", "zed", "terminal"):
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
        self.assertEqual(self.commands, [])

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
        for host in ("tmux", "unit", "ghostty", "zed", "terminal"):
            _ = self.owner(host, host=host)
            _ = self.transcript(host, tail={"type": "assistant", "message": {"model": "opus-real", "content": "normal work"}})
        with patch.object(inventory, "inventory") as observe:
            _ = self.cli()
        observe.assert_not_called()
        self.assertEqual(self.commands, [])
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
        self.assertEqual(self.commands, [])

    def test_refused_sessions_on_one_account_move_by_group_and_scratch_boundary(self) -> None:
        _ = self.groups(showrunners="claude1")
        for identity, host in (("director", "unit"), ("showrunner", "tmux"), ("desktop", "ghostty")):
            _ = self.owner(identity, host=host)
            _ = self.transcript(identity)
        _ = self.cli()
        self.assertEqual(len(self.commands), 3)
        self.assertEqual({command[command.index("--only") + 1] for command in self.commands},
            {"director", "showrunner", "desktop"})
        command = self.commands[0]
        self.assertIn("--here", command)
        self.assertIn("--restart", command)
        self.assertEqual(command[command.index("--to") + 1], "claude3")
        self.assertIn("weekly limit until", command[command.index("--why") + 1])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested"] * 3)
        self.assertEqual(len({entry["move_id"] for entry in entries}), 3)
        self.assertEqual({entry.get("request_id") for entry in entries if entry["kind"] == "requested"},
                         {"batch-1", "batch-2", "batch-3"})
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, NOW - timedelta(seconds=1))

    def test_unchanged_transcript_rescan_keeps_refusal_time_and_never_repeats_move(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        _ = self.cli()
        record.archive_session(SOURCE_LOGIN, "refused")
        before = self.snapshot()
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, NOW - timedelta(seconds=1))

    def test_new_transcript_refusal_uuid_is_a_new_transition(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        _ = self.cli()
        record.archive_session(SOURCE_LOGIN, "refused")
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.transcript("refused", at=WatchTime.at, entry_id="refusal-two")
        _ = self.cli()
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].at, WatchTime.at)

    def test_unnamed_window_uses_only_a_post_refusal_reading_with_one_zero(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        held = refusals.load()[refusals.key("claude", SOURCE_LOGIN)]
        self.assertEqual(held.window, refusals.KnownWindow("five_hour"))
        self.assertEqual(held.reset, refusals.KnownReset(NOW + timedelta(minutes=19)))
        self.assertEqual(self.commands, [])
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_pre_refusal_reading_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 80, five=0, at=NOW - timedelta(minutes=1), five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.commands), 1)

    def test_both_zero_windows_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        self.reading(SOURCE_LOGIN, 0, five=0, five_reset=NOW + timedelta(minutes=19))
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.commands), 1)

    def test_missing_reading_cannot_infer_an_unnamed_window(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "Usage limit reached")
        saved = usage_store.load()
        _ = saved.pop(usage_store.key("Claude", SOURCE_LOGIN))
        usage_store.write(saved)
        _ = self.cli()
        self.assertEqual(refusals.load()[refusals.key("claude", SOURCE_LOGIN)].window, refusals.UnknownWindow())
        self.assertEqual(len(self.commands), 1)

    def test_waited_reset_continues_once_on_its_own_account_and_new_refusal_is_decided_fresh(self) -> None:
        _ = self.owner("refused", host="ghostty")
        _ = self.transcript("refused", "You've hit your session limit · resets 11:30pm (America/New_York)")
        reset = NOW + timedelta(minutes=19)
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(self.commands, [])
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
        self.assertEqual(self.commands, [])
        self.assertEqual(self.process_dir(self.owners[0]["pid"]), account.ConfiguredClaudeFolder(accounts.account_dir("claude1")))
        _ = self.transcript("refused", "weekly limit", at=reset, entry_id="later-refusal")
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)

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
        self.assertEqual(self.commands, [])
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
        self.assertEqual("\n".join(preview.splitlines()[1:]) + "\n", actual)
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all(command[command.index("--to") + 1] == "claude2" for command in self.commands))

    def test_transcript_lookup_ignores_deeper_folders_and_reads_each_live_owner_once_per_tick(self) -> None:
        _ = self.owner("refused")
        transcript = self.transcript("refused")
        nested = transcript.parent / "subagents" / transcript.name
        nested.parent.mkdir()
        _ = nested.write_text(json.dumps({"type": "assistant", "message": {"content": "normal reply"}}) + "\n")
        os.utime(nested, (transcript.stat().st_mtime + 60, transcript.stat().st_mtime + 60))
        with patch.object(refusal_watch, "session_refusal", wraps=refusal_watch.session_refusal) as detect:
            _, output, _ = self.cli("--dry-run")
        self.assertIn("owner refused: refused; move", output)
        detect.assert_called_once_with("refused")

    def assert_five_hour_moves(self, reset: datetime) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused", "You've hit your session limit")
        self.reading(SOURCE_LOGIN, 80, five=0, five_reset=reset)
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["requested"])

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
        self.assertEqual(self.commands, [])
        self.reading(TARGET_LOGIN, 1, at=NOW + timedelta(seconds=1), reset=NOW + timedelta(days=1))
        WatchTime.at = NOW + timedelta(seconds=1)
        _ = self.cli()
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["no room", "no room", "room again", "room again", "requested", "requested"])
        self.assertEqual(entries[0]["move_id"], entries[2]["move_id"])
        self.assertEqual(entries[1]["move_id"], entries[3]["move_id"])
        self.assertEqual(len(self.commands), 1)

    def test_pre_record_shutdown_refusal_is_failed_and_retried_on_next_tick(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "GitHub keys are cold")) as down:
            _ = self.cli()
            WatchTime.at = NOW + timedelta(minutes=2)
            _ = self.cli()
        self.assertEqual(down.call_count, 2)
        self.assertEqual(record.all_session_records(), [])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed", "failed"])
        for entry in entries:
            assert entry["kind"] == "failed"
            self.assertIn("cold", entry["reason"])

    def test_shutdown_subprocess_failure_is_failed_without_request(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        with patch.object(subprocess, "run", side_effect=OSError("conductor could not start")):
            _ = self.cli()
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed"])
        failed = entries[0]
        assert failed["kind"] == "failed"
        self.assertIn("conductor", failed["reason"])
        self.assertEqual(record.all_session_records(), [])

    def test_conductor_failure_with_created_records_withdraws_all_owners_and_records_only_failures(self) -> None:
        for identity in ("first", "second"):
            _ = self.owner(identity)
            _ = self.transcript(identity)
        report = self.inventory_for(SOURCE_LOGIN, refresh_desktops=False)
        def fail_conductor(_login: str, _label: str) -> None:
            self.assertEqual({saved["session"]["session_id"] for saved in record.all_session_records()}, {"first", "second"})
            raise RuntimeError("conductor could not start after records were created")
        def run_down(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            try:
                code = settle.down(account.Account("claude", SOURCE_LOGIN, "claude1"), here=True,
                    only=command[command.index("--only") + 1].split(","), then="restart",
                    target=command[command.index("--to") + 1], why=command[command.index("--why") + 1])
            except RuntimeError as error:
                return subprocess.CompletedProcess(command, 1, "", str(error))
            return subprocess.CompletedProcess(command, code, "", "")
        with (patch.object(subprocess, "run", side_effect=run_down),
              patch.object(settle, "_warm", return_value=True),
              patch.object(settle, "inventory", return_value=report),
              patch.object(settle, "run_inventory", return_value=report),
              patch.object(settle, "_session_timer_snapshot", return_value=[]),
              patch.object(settle, "_retire_shutdown_messages"),
              patch.object(settle, "_launch_conductor", side_effect=fail_conductor) as conductor,
              patch.object(record, "create_session", wraps=record.create_session) as created):
            _ = self.cli()
        conductor.assert_called_once()
        self.assertEqual(created.call_count, 2)
        self.assertEqual(record.all_session_records(), [])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed", "failed"])
        for entry in entries:
            assert entry["kind"] == "failed"
            self.assertIn("conductor could not start after records were created", entry["reason"])

    def test_settle_refusal_before_record_is_failed_without_request(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "settle refused")):
            _ = self.cli()
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed"])
        failed = entries[0]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["reason"], "settle refused")

    def test_cancelled_refusal_is_not_restarted_by_unchanged_transcript(self) -> None:
        _ = self.owner("refused")
        _ = self.transcript("refused")
        _ = self.cli()
        refusal_watch.cancelled("refused")
        record.archive_session(SOURCE_LOGIN, "refused")
        WatchTime.at = NOW + timedelta(minutes=2)
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)

    def test_open_shutdown_owner_and_director_worker_are_excluded(self) -> None:
        owner = self.owner("already-moving")
        _ = self.transcript(owner["session_id"])
        _ = self.cli()
        fields = inventory.SessionFields(session_id="worker-refused", pid=12345, proc_start="fixture-start",
            name="worker", cwd=owner["cwd"], status="idle", updated_at=0, probe=False, model=owner["model"], checkout=owner["checkout"],
            run_dirs=[], codex_servers=[], timers=[], config_dir=str(accounts.account_dir("claude1")))
        worker = inventory.WorkerSession(**fields, kind="worker", host=inventory.UnknownHost(kind="unknown"),
            owner=inventory.DirectorOwner(kind="director", session_id=owner["session_id"]))
        self.owners.append(worker)
        self.live.append(sessions.SessionRecord(pid=12345, sessionId="worker-refused", name="worker",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))
        _ = self.transcript("worker-refused")
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["requested"])

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
        self.assertEqual(self.commands, [])

    def test_assumed_dry_run_writes_neither_assumption_nor_transition(self) -> None:
        _ = self.owner("assumed", host="ghostty")
        before = self.snapshot()
        _ = self.cli("--dry-run", "--assume-refused", "assumed", "weekly")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.commands, [])

    def test_showrunner_batch_uses_the_same_move_sessions_path_as_watcher(self) -> None:
        _ = self.owner("first", host="tmux")
        _ = self.owner("second", host="tmux")
        _ = self.owner("unit", host="unit")
        requested = (accounts.SessionMoveRequested("first", "batch-id", "claude3", source_login=SOURCE_LOGIN),
                     accounts.SessionMoveRequested("second", "batch-id", "claude3", source_login=SOURCE_LOGIN))
        with patch.object(accounts, "move_sessions", return_value=requested) as move:
            result = accounts.move_group("showrunners", "claude3", why="weekly refusal")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        transition = accounts.group_transition("showrunners")
        assert isinstance(transition, accounts.GroupTransition)
        move.assert_called_once_with(("first", "second"), "claude3", why="weekly refusal",
                                     origin=accounts.HandOrigin(), move_id=transition.move_id, alerts="user")

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
        self.assertEqual(len(self.commands), 1)

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
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(command[command.index("--to") + 1], "claude3")
        self.assertNotEqual(command[command.index("--to") + 1], "claude2")
        saved = record.all_session_records()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["login"], "main@example.invalid")


if __name__ == "__main__":
    _ = unittest.main()
