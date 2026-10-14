"""A provider refusal moves only its live owner, once, through safe shutdown."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, Self, cast, final, override
from unittest.mock import patch

from scripts.accounts import accounts, moves, refusal_watch, refusals
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
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": os.defpath, "AGENT_NOTES_DIR": str(self.home / "notes"),
            "SHUTDOWN_STATE_DIR": str(self.home / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.home / "sessions"), "USER_ZONE": "America/New_York",
        }, clear=True)))
        (self.home / "notes").mkdir()
        (self.home / ".claude").mkdir()
        (self.home / "notifier").mkdir()
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.home / "notifier"))
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        for label, login in (("claude 1", SOURCE_LOGIN), ("claude 3", TARGET_LOGIN)):
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
            _ = (self.home / "notes" / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
        _ = (self.home / "notes/claude 2.md").write_text("---\nlogin: main@example.invalid\n---\n")
        self.owners = []
        self.live = []
        WatchTime.at = NOW
        _ = self.enterContext(patch.object(refusal_watch, "datetime", WatchTime))
        _ = self.enterContext(patch.object(sessions, "live_sessions", side_effect=lambda: self.live))
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.process_dir))
        _ = self.enterContext(patch.object(account, "account_of", side_effect=self.process_account))
        _ = self.enterContext(patch.object(inventory, "inventory", side_effect=self.inventory_for))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.down))
        self.notifications = []
        _ = self.enterContext(patch.object(refusal_watch, "_notify_no_room", side_effect=self.notified))
        self.commands = []
        self.reading("main@example.invalid", 80, reset=NOW + timedelta(days=6))
        self.reading(SOURCE_LOGIN, 0)
        self.reading(TARGET_LOGIN, 1, reset=NOW + timedelta(days=1))

    def process_dir(self, pid: int) -> Path | None:
        found = next((owner for owner in self.owners if owner["pid"] == pid), None)
        return Path(found["config_dir"]) if found is not None and "config_dir" in found else None

    def process_account(self, pid: int) -> account.Account | None:
        directory = self.process_dir(pid)
        if directory is None:
            return None
        identity = accounts.account_identity(directory)
        return account.Account("claude", identity.login, identity.label) if isinstance(identity, agent_accounts.KnownClaudeAccount) else None

    def inventory_for(self, login: str, only: frozenset[str] = NO_SESSION_FILTER, *,
                      refresh_desktops: bool = True) -> inventory.Inventory:
        self.assertFalse(refresh_desktops, "the watcher must read the desktop snapshot without invoking its timer")
        owners = [owner for owner in self.owners
                  if (identity := self.process_account(owner["pid"])) is not None
                  and identity.login.casefold() == login.casefold()
                  and (not only or owner["session_id"] in only)]
        return inventory.Inventory(machine="natedev", login=login,
            label="claude 1" if login == SOURCE_LOGIN else "claude 3", sessions=owners, unattributed=[])

    def owner(self, identity: str, *, host: str = "tmux", label: str = "claude 1") -> inventory.Session:
        fields = inventory.SessionFields(session_id=identity, pid=1000 + len(self.owners),
            proc_start="fixture-start", name="owner " + identity, cwd=str(self.home / "checkout"),
            status="idle", model=inventory.ModelName(kind="model", name="opus-real"),
            checkout=inventory.NotACheckout(kind="not a checkout"),
            run_dirs=[], codex_servers=[], timers=[], config_dir=str(accounts.account_dir(label)))
        if host == "unit":
            owner: inventory.Session = inventory.UnitSession(**fields, kind="unit", host=inventory.UnitHost(
                kind="unit", production="fixture", unit=identity, doc="/fixture.md", tmux_session=identity,
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
                for path in self.home.rglob("*") if path.is_file() and not path.is_symlink()}

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
        for identity in ids:
            owner = next(owner for owner in self.owners if owner["session_id"] == identity)
            source = self.process_account(owner["pid"])
            assert source is not None
            saved = record.SessionShutdown(login=source.login, label=source.label, machine="natedev",
                requested_at=WatchTime.at.isoformat(), request_id=f"batch-{len(self.commands)}",
                requested_by=record.FromTerminal(kind="terminal"), hold="session", then="restart",
                force="wait for ready", alerts="user", session=owner, seats=[], timers=[],
                settle_message=record.SettleMessageNotSent(kind="not sent"), where=record.WhereNotSaid(kind="not said"),
                progress=record.SessionWaiting(kind="waiting"), stop_issues=[], target=target,
                source_label=source.label, why=why)
            self.assertTrue(record.create_session(saved))
        return subprocess.CompletedProcess(command, 0, "added\n", "")

    def test_dry_run_detects_refusal_on_each_host_and_changes_nothing(self) -> None:
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
        self.assertIn("claude 3", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(moves.read_moves(), [])

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
        self.assertIn("claude 3", output)
        self.assertNotIn("large-healthy", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.commands, [])

    def test_refused_sessions_on_one_account_share_one_selected_shutdown(self) -> None:
        for identity, host in (("director", "unit"), ("showrunner", "tmux"), ("desktop", "ghostty")):
            _ = self.owner(identity, host=host)
            _ = self.transcript(identity)
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(set(command[command.index("--only") + 1].split(",")), {"director", "showrunner", "desktop"})
        self.assertIn("--here", command)
        self.assertIn("--restart", command)
        self.assertEqual(command[command.index("--to") + 1], "claude 3")
        self.assertIn("weekly limit until", command[command.index("--why") + 1])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested"] * 3)
        self.assertEqual({entry["move_id"] for entry in entries}, {"batch-1"})
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
        self.assertEqual(self.process_dir(self.owners[0]["pid"]), accounts.account_dir("claude 1"))
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
        _ = self.owner("second", label="claude 3")
        _ = self.transcript("first")
        _ = self.transcript("second")
        self.reading(SOURCE_LOGIN, 80, at=REFUSAL_AT - timedelta(seconds=1), reset=NOW + timedelta(hours=1))
        self.reading(TARGET_LOGIN, 80, at=REFUSAL_AT - timedelta(seconds=1), reset=NOW + timedelta(hours=2))
        before = self.snapshot()
        _, preview, _ = self.cli("--dry-run")
        self.assertIn("owner first: refused; move from claude 1 to default", preview)
        self.assertIn("owner second: refused; move from claude 3 to default", preview)
        self.assertEqual(self.snapshot(), before)
        _, actual, _ = self.cli()
        self.assertEqual(preview, actual)
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all(command[command.index("--to") + 1] == "default" for command in self.commands))

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
        _ = self.cli()
        _ = self.cli()
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("earliest reset", self.notifications[0])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["no room", "no room"])
        self.assertEqual(self.commands, [])
        self.reading(TARGET_LOGIN, 1, reset=NOW + timedelta(days=1))
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
                code = settle.down(account.Account("claude", SOURCE_LOGIN, "claude 1"), here=True,
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

    def test_open_shutdown_owner_and_director_seat_are_excluded(self) -> None:
        owner = self.owner("already-moving")
        _ = self.transcript(owner["session_id"])
        _ = self.cli()
        fields = inventory.SessionFields(session_id="seat-refused", pid=12345, proc_start="fixture-start",
            name="worker", cwd=owner["cwd"], status="idle", model=owner["model"], checkout=owner["checkout"],
            run_dirs=[], codex_servers=[], timers=[], config_dir=str(accounts.account_dir("claude 1")))
        worker = inventory.SeatSession(**fields, kind="seat", host=inventory.UnknownHost(kind="unknown"),
            owner=inventory.DirectorOwner(kind="director", session_id=owner["session_id"]))
        self.owners.append(worker)
        self.live.append(sessions.SessionRecord(pid=12345, sessionId="seat-refused", name="worker",
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))
        _ = self.transcript("seat-refused")
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
        _ = self.owner("first")
        _ = self.owner("second")
        _ = self.owner("unit", host="unit")
        requested = (accounts.SessionMoveRequested("first", "batch-id", "claude 3"),
                     accounts.SessionMoveRequested("second", "batch-id", "claude 3"))
        with patch.object(accounts, "move_sessions", return_value=requested) as move:
            result = accounts.move_showrunners("claude 1", "claude 3", why="weekly refusal")
        self.assertIsInstance(result, accounts.ShowrunnersMoveStarted)
        move.assert_called_once_with(("first", "second"), "claude 3", why="weekly refusal")

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
        self.assertEqual(accounts.setup("claude 2"), [])
        _ = (accounts.account_dir("claude 2") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.owner("alias-owner", label="claude 2")
        _ = self.transcript("alias-owner", "Usage limit reached")
        # Both positive windows after the refusal resolve an unnamed refusal immediately.
        # Default resets first, so exclusion must still prevent a return to the same login.
        self.reading("main@example.invalid", 80, reset=NOW + timedelta(hours=1))
        _ = self.cli()
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertEqual(command[command.index("--to") + 1], "claude 3")
        self.assertNotEqual(command[command.index("--to") + 1], "default")
        saved = record.all_session_records()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["login"], "main@example.invalid")


if __name__ == "__main__":
    _ = unittest.main()
