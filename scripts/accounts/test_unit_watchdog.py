"""Recover production units with scratch accounts and fake launch and send leaves."""
from __future__ import annotations

import json
import io
import os
import subprocess
import time
import unittest
from datetime import timedelta
from contextlib import redirect_stdout
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, Literal, cast, final, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.accounts import accounts, next_moves, outage_alerts, refusal_watch, role_map, unit_watchdog
from scripts.accounts import test_refusal_watch as fixtures
from scripts.production import showrunners
from scripts.notify.push_texts import RenderedPush

if TYPE_CHECKING:
    from scripts.shutdown import record, inventory, launch_permission, restart, settle
    from scripts.herdr import herdr
    from scripts.message import sessions
    from scripts.production import unit_lookup
else:
    record = import_module("record")
    settle = import_module("settle")
    restart = import_module("restart")
    herdr = import_module("herdr")
    inventory = import_module("inventory")
    sessions = import_module("sessions")
    launch_permission = import_module("launch_permission")
    unit_lookup = import_module("unit_lookup")
import quota_alert
import agent_accounts
NOW = fixtures.NOW


@final
class UnitWatchdogTests(unittest.TestCase):
    fixture: fixtures.RefusalWatchTests = fixtures.RefusalWatchTests()
    home: Path = Path()
    doc: Path = Path()
    runs: Path = Path()
    units: dict[str, inventory.Session] = {}
    finished: set[str] = set()
    retired: set[str] = set()
    commands: list[list[str]] = []
    notices: list[str] = []
    refused: set[str] = set()
    pending: list[list[str]] = []
    finish_jobs: bool = True

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.RefusalWatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.doc = self.home / "fixture-production.md"
        self.units = {}
        self.runs = self.home / "runs"
        self.runs.mkdir()
        self.finished = set()
        self.retired = set()
        self.commands = []
        self.notices = []
        self.refused = set()
        self.pending = []
        self.finish_jobs = True
        _ = self.enterContext(patch.object(unit_lookup, "runs_dir", return_value=self.runs))
        _ = self.enterContext(patch.object(unit_lookup, "every_marked_unit", side_effect=self.marked))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=[
            showrunners.Showrunner(session="fixture-showrunner", socket="/fixture.sock", slug="fixture", zone="UTC", doc=str(self.doc))]))
        _ = self.enterContext(patch.object(showrunners, "tell", side_effect=self.tell))
        _ = self.enterContext(patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.command))
        _ = self.enterContext(patch.object(herdr, "herdr_binary", return_value="/nonexistent/herdr"))
        _ = self.enterContext(patch.object(herdr.Herdr, "agents", return_value=[]))
        _ = self.enterContext(patch.object(restart, "_prepare_restore_server"))
        _ = self.enterContext(patch.object(herdr.Herdr, "close_restore_placeholders"))
        _ = self.fixture.groups(units="claude2")
        # Every account read, launch permission and message leaf is provided by the fixture.
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "four@example.invalid"}}))
        _ = (self.home / "notes/claude4.md").write_text("---\nlogin: four@example.invalid\n---\n")
        self.fixture.reading("four@example.invalid", 80)
        self.write_doc()

    def write_doc(self) -> None:
        rows = ["# Fixture", "- **Production slug:** fixture", "## Units", "| Unit | Plan | Worktree |", "| --- | --- | --- |"]
        for name in self.units:
            tree = self.home / name
            tree.mkdir(exist_ok=True)
            _ = (tree / ".git").write_text("gitdir: scratch\n")
            events: list[dict[str, object]] = [{"event_type": "run_started", "working_dir": str(tree), "run_started_at": 1}]
            if name in self.finished:
                events.append({"event_type": "run_finished"})
            _ = (self.runs / (name + ".jsonl")).write_text("\n".join(json.dumps(event) for event in events))
            plan = "(retired)" if name in self.retired else "docs/plan.md"
            rows.append(f"| {name} | {plan} | {tree} |")
        _ = self.doc.write_text("\n".join(rows) + "\n")

    def add_unit(self, name: str, *, label: str = "claude4") -> inventory.Session:
        owner = self.fixture.owner(name, host="unit", label=label)
        Path(owner["cwd"]).mkdir(parents=True, exist_ok=True)
        self.units[name] = owner
        self.write_doc()
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        record.write_snapshot(record.RecoverySnapshot(machine="natedev", login=identity.login, label=label,
            taken_at=NOW.isoformat(), sessions=[record.SnapshotClaudeSession(agent="claude", session=item,
                workers=[], timers=[], last_seen=NOW.isoformat(), state="unresolved", crash="host gone")
                for item in self.units.values() if item.get("config_dir") == str(accounts.account_dir(label))]))
        return owner

    def marked(self) -> unit_lookup.MarkedProductions:
        known = {entry["sessionId"] for entry in self.fixture.live}
        found: dict[str, unit_lookup.MarkedUnit] = {}
        for name, owner in self.units.items():
            claude = (unit_lookup.LiveClaude(owner["name"], owner["session_id"], "/fixture.sock", owner["pid"])
                      if owner["session_id"] in known else unit_lookup.ClaudeNotRunning())
            found[name] = unit_lookup.MarkedUnit(name, claude,
                unit_lookup.Place("shared", name))
        return unit_lookup.MarkedProductions({"fixture": found}, {})

    def die(self, *names: str) -> None:
        self.fixture.live[:] = [entry for entry in self.fixture.live if entry["sessionId"] not in names]

    def revive(self, identity: str, label: str) -> None:
        owner = self.units[identity]
        owner["config_dir"] = str(accounts.account_dir(label))
        self.die(identity)
        self.fixture.live.append(sessions.SessionRecord(pid=owner["pid"], sessionId=identity, name=owner["name"],
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))

    def tell(self, _slug: str, _key: str, text: str) -> str:
        self.notices.append(text)
        return "told fixture"

    def command(self, argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if "--recover" in argv:
            self.assertLessEqual(cast(float, options["timeout"]), 5)
            self.assertIn("--no-block", argv)
            self.assertIn("--user", argv)
            self.pending.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.assertTrue(any(part.endswith("/shutdown.py") for part in argv), argv)
        if "down" in argv:
            result = self.fixture.down(argv, **options)
            for saved in record.all_session_records():
                def stopped(current: record.SessionShutdown) -> None:
                    current["progress"] = record.SessionStopped(kind="stopped", at=NOW.isoformat())
                _ = record.update_session(saved["login"], saved["session"]["session_id"], stopped)
                self.die(saved["session"]["session_id"])
            return result
        self.assertEqual(argv[2], "restart")
        self.assertIn("--here", argv)
        self.assertIn("--no-alert", argv)
        self.commands.append(argv)
        label = argv[3]
        if label in self.refused:
            return subprocess.CompletedProcess(argv, 1, "", "fixture refused")
        identity = argv[argv.index("--only") + 1]
        saved = next((item for item in record.all_session_records() if item["session"]["session_id"] == identity), None)
        self.revive(identity, saved.get("target", label) if saved else label)
        if saved:
            def restored(current: record.SessionShutdown) -> None:
                current["progress"] = record.SessionRestarted(kind="restarted", at=NOW.isoformat())
            _ = record.update_session(saved["login"], identity, restored)
        return subprocess.CompletedProcess(argv, 0, "fixture restarted", "")

    def complete_jobs(self) -> None:
        if self.finish_jobs:
            pending, self.pending = self.pending, []
            for argv in pending:
                offset = argv.index("--recover")
                unit_watchdog._finish_recovery(argv[offset + 1], argv[offset + 2], Path(argv[offset + 3]), argv[offset + 4])  # pyright: ignore[reportPrivateUsage]

    def pass_at(self, minute: int, *, dry_run: bool = False) -> list[str]:
        if not dry_run:
            self.complete_jobs()
        return unit_watchdog.run(NOW + timedelta(minutes=minute), dry_run=dry_run)

    def test_several_units_read_run_records_once_per_pass(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        with patch.object(unit_lookup, "read_run_states", wraps=unit_lookup.read_run_states) as reads:
            self.assertEqual(self.pass_at(0, dry_run=True), [])
            reads.assert_called_once_with()

    def test_recovery_rechecks_a_reported_outage_and_sees_the_new_unit_pane(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            if herdr.herdr_processes():
                return self.marked()
            return unit_lookup.MarkedProductions({"fixture": {"unit": unit_lookup.MarkedUnit(
                "unit", unit_lookup.ClaudeNotRunning(), unit_lookup.Place("shared", "unit"))}}, {})
        with (patch.object(herdr, "_scan_herdr_processes", side_effect=[[], [fresh]]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(unit_watchdog.run(NOW, dry_run=False), [])
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pending, [])

    def test_recovery_completion_reads_new_unit_pane_after_report_snapshot(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        processes: list[herdr.HerdrProcess] = []

        def observed() -> unit_lookup.MarkedProductions:
            return self.marked() if herdr.herdr_processes() else unit_lookup.MarkedProductions({}, {})

        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)),
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            processes.append(fresh)
            self.assertEqual(unit_watchdog._live_account(self.doc, "unit"), "claude4")  # pyright: ignore[reportPrivateUsage]

    def test_live_unit_report_reuses_the_scan_without_starting_recovery(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"), {}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            self.assertEqual(herdr.herdr_processes(), [fresh])
            return self.marked()
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            _ = herdr.herdr_processes()
            self.assertEqual(unit_watchdog.run(NOW, dry_run=False), [])
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.pending, [])

    def test_idle_real_watcher_and_watchdog_share_the_report_scan(self) -> None:
        owner = self.add_unit("unit", label="claude2")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            _ = herdr.herdr_processes()
            return self.marked()
        def listing(login: str | None = None, *,
                    registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            _ = herdr.herdr_processes()
            return self.fixture.listing(login, registrations=registrations)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              patch.object(inventory, "listing", side_effect=listing),
              refusal_watch.session_observation(), herdr.process_snapshot()):
            _ = herdr.herdr_processes()  # The earlier session-table report.
            self.assertEqual(refusal_watch.run(NOW), [])
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.pending, [])
        self.assertEqual(self.fixture.commands, [])

    def test_live_report_without_fresh_confirmation_keeps_recovery_evidence(self) -> None:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        result = Path(outage["job"]["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        live = self.marked()
        self.die("unit")
        absent = self.marked()
        with (patch.object(unit_lookup, "every_marked_unit", side_effect=[live, absent]),
              patch.object(unit_watchdog, "_recovery_result", return_value=unit_watchdog.RecoveryStillRunning()),
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertTrue(result.exists())
        self.assertIn("job", unit_watchdog._read_state()["unit"])  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any("restarted on" in line for line in [*lines, *self.notices]))
        cleared.assert_not_called()

    def test_unreadable_fresh_confirmation_preserves_recovery_result_and_job(self) -> None:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        result = Path(outage["job"]["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        with (patch.object(unit_watchdog, "_live_account", side_effect=OSError("fixture unreadable")),
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertEqual(result.read_text(), '"fixture evidence"')
        self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("fixture unreadable" in line for line in lines))
        self.assertFalse(any("restarted on" in line for line in self.notices))
        cleared.assert_not_called()

    def finished_recovery(self) -> tuple[dict[str, unit_watchdog.DownOutage], Path]:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        job = outage["job"]
        result = Path(job["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        return state, result

    def test_finished_recovery_keeps_evidence_when_the_second_fresh_read_is_unreadable(self) -> None:
        state, result = self.finished_recovery()
        with (patch.object(unit_watchdog, "_live_account", side_effect=["", OSError("fixture unreadable")]) as live,
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertEqual(live.call_count, 2)
        self.assertEqual(result.read_text(), '"fixture evidence"')
        self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("watchdog: fixture: fixture unreadable", lines)
        self.assertFalse(any("restarted on" in line or "retry from" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 1)
        cleared.assert_not_called()

    def test_finished_recovery_consumes_evidence_only_after_live_confirmation(self) -> None:
        state, result = self.finished_recovery()
        checks: list[str] = []

        def confirm(_doc: Path, _unit: str) -> str:
            checks.append("checked")
            self.assertEqual(result.read_text(), '"fixture evidence"')
            self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
            return "" if len(checks) == 1 else "claude4"

        with patch.object(unit_watchdog, "_live_account", side_effect=confirm):
            lines = self.pass_at(3)
        self.assertEqual(len(checks), 2)
        self.assertFalse(result.exists())
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("restarted on claude4" in line for line in lines))
        self.assertEqual(len(self.pending), 1)

    def test_finished_recovery_consumes_evidence_after_absent_confirmation_and_retries(self) -> None:
        state, result = self.finished_recovery()
        checks: list[str] = []

        def confirm(_doc: Path, _unit: str) -> str:
            checks.append("checked")
            self.assertEqual(result.read_text(), '"fixture evidence"')
            return ""

        with patch.object(unit_watchdog, "_live_account", side_effect=confirm):
            lines = self.pass_at(3)
        self.assertEqual(len(checks), 2)
        self.assertFalse(result.exists())
        after = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
        before = state["unit"]
        assert "job" in after and "job" in before
        self.assertNotEqual(after["job"], before["job"])
        self.assertTrue(any("fixture evidence; retry from claude4's recovery record" in line for line in lines))
        self.assertFalse(any("restarted on" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 2)

    def test_held_unit_discards_finished_recovery_evidence_without_retry(self) -> None:
        _, result = self.finished_recovery()
        _ = self.hold("claude4", terminal=True)
        with (patch.object(unit_watchdog, "_live_account", return_value="") as live,
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("terminal stop"))):
            lines = self.pass_at(3)
        live.assert_called_once()
        self.assertFalse(result.exists())
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any("restarted on" in line or "retry from" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 1)

    def test_blocked_unit_discards_finished_recovery_evidence_without_retry(self) -> None:
        _, result = self.finished_recovery()
        saved = record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=NOW.isoformat(), request_id="terminal-stop", requested_by=record.FromTerminal(kind="terminal"),
            hold="session", then="stop", force="wait for ready", alerts="none", probe=False,
            session=self.units["unit"], workers=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
            where=record.WhereNotSaid(kind="not said"), progress=record.SessionStopped(kind="stopped", at=NOW.isoformat()),
            stop_issues=[], trail=[])
        self.assertTrue(record.create_session(saved))
        with patch.object(unit_watchdog, "_live_account", return_value="") as live:
            lines = self.pass_at(3)
        live.assert_called_once()
        self.assertFalse(result.exists())
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any("restarted on" in line or "retry from" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 1)

    def test_finished_recovery_without_a_source_records_failure_and_consumes_the_result(self) -> None:
        state, result = self.finished_recovery()
        del state["unit"]["recovery"]
        unit_watchdog._write_state(state)  # pyright: ignore[reportPrivateUsage]
        for snapshot in record.all_snapshots():
            snapshot["sessions"] = []
            record.write_snapshot(snapshot)
        self.die("unit")
        lines = self.pass_at(3)
        outage = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
        assert "dropped" in outage
        self.assertEqual(outage["dropped"], "fixture evidence")
        self.assertNotIn("job", outage)
        self.assertFalse(result.exists())
        self.assertIn("watchdog: unit: fixture evidence", lines)
        self.assertIn("watchdog: unit: no recovery record or snapshot", lines)
        self.assertEqual(len(self.pending), 1)

    def test_failed_result_cleanup_retries_when_a_recovery_source_returns(self) -> None:
        state, result = self.finished_recovery()
        self.pending.clear()
        del state["unit"]["recovery"]
        unit_watchdog._write_state(state)  # pyright: ignore[reportPrivateUsage]
        snapshots = record.all_snapshots()
        for snapshot in snapshots:
            empty = snapshot.copy()
            empty["sessions"] = []
            record.write_snapshot(empty)
        self.die("unit")
        unlink = Path.unlink
        failures: list[Path] = []

        def remove(path: Path, *, missing_ok: bool = False) -> None:
            if path == result and not failures:
                failures.append(path)
                raise PermissionError("fixture result cleanup denied")
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", remove):
            lines = self.pass_at(3)
            self.assertIn("watchdog: fixture: fixture result cleanup denied", lines)
            self.assertTrue(result.exists())
            after_failure = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
            self.assertIn("job", after_failure)
            for snapshot in snapshots:
                record.write_snapshot(snapshot)
            lines = self.pass_at(4)
        self.assertFalse(result.exists())
        self.assertTrue(any("fixture evidence; retry from claude4's recovery record" in line for line in lines))
        self.assertFalse(any("no longer exists" in line for line in lines))
        self.assertEqual(len(self.pending), 1)
        self.finish_jobs = True
        lines = self.pass_at(5)
        self.assertTrue(any("restarted on claude4" in line for line in lines))
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertIn("unit", {entry["sessionId"] for entry in self.fixture.live})

    def test_10_26_auto_move_clears_a_stale_hold_and_every_unit_recovers(self) -> None:
        _ = self.fixture.groups(units="claude4")
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("10-26-outage"))
        held = self.hold("claude2", terminal=False)
        def permission(_environment: object, _purpose: object) -> launch_permission.LaunchPermission:
            if record.account_hold(held["login"]) is not None:
                record.clear_account_hold(held["login"])
                return launch_permission.AllowedByShutdownState(launch_permission.ReleasedHold(
                    label="claude2", then="stop", requested_at=NOW.isoformat(), session_id="b1f5ca98-gone"))
            return launch_permission.AllowedByShutdownState()
        with patch.object(launch_permission, "launch_permission", side_effect=permission):
            first = refusal_watch.run(NOW)
            self.assertTrue(any("cleared a finished stop hold" in line for line in first))
            self.assertEqual(self.commands, [])
            _ = refusal_watch.run(NOW + timedelta(minutes=2))
            self.complete_jobs()
            _ = refusal_watch.run(NOW + timedelta(minutes=4))
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, set(self.units))
        for owner in self.units.values():
            self.assertEqual(owner.get("config_dir"), str(accounts.account_dir("claude2")))
        notices = [job.text for batch in self.fixture.head_deliveries for job in batch if "cleared a finished" in job.text]
        self.assertEqual(len(notices), 1)
        self.assertIn("session b1f5ca98", notices[0])
        self.assertEqual(len(self.commands), 3)

    def hold(self, label: str, *, terminal: bool) -> record.AccountHold:
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        held = record.AccountHold(login=identity.login, label=label, machine="natedev", requested_at=NOW.isoformat(),
            request_id="hold-" + label, requested_by=record.FromTerminal(kind="terminal") if terminal else
            record.FromSession(kind="session", session_id="b1f5ca98-gone"), then="stop")
        record.set_account_hold(held)
        return held

    def test_active_target_hold_stops_no_unit_then_crash_recovers_after_grace_on_source(self) -> None:
        _ = self.fixture.groups(units="claude4")
        _ = self.add_unit("alpha")
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("held-target"))
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("claude2 is held")):
            lines = refusal_watch.run(NOW)
        self.assertTrue(any("claude2 is held" in line for line in lines))
        self.assertEqual(self.fixture.commands, [])
        self.assertEqual(accounts.group_accounts().get("units"), "claude4")
        self.die("alpha")
        _ = self.pass_at(2)
        self.assertEqual(self.commands, [])
        _ = self.pass_at(4)
        _ = self.pass_at(6)
        self.assertEqual(self.commands[0][3], "claude4")
        self.assertIn("restarted on claude4", self.notices[0])

    def test_crash_command_uses_recovery_owner_and_reports_actual_account(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.add("claude2")
        self.die("alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual([argv[3] for argv in self.commands], ["claude4"])
        self.assertIn("restarted on claude4", self.notices[0])

    def test_user_stop_retired_finished_and_in_progress_units_are_not_restarted(self) -> None:
        for name in ("user-stop", "retired", "finished", "waiting"):
            _ = self.add_unit(name)
        self.retired.add("retired")
        self.finished.add("finished")
        self.write_doc()
        # A terminal stop and a move still in progress each hold their own session.
        for name, progress, then in (("user-stop", record.SessionStopped(kind="stopped", at=NOW.isoformat()), "stop"),
                                     ("waiting", record.SessionWaiting(kind="waiting"), "restart")):
            saved = record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
                requested_at=NOW.isoformat(), request_id=name, requested_by=record.FromTerminal(kind="terminal"),
                hold="session", then=cast(Literal["stop", "restart"], then), force="wait for ready", alerts="none", probe=False,
                session=self.units[name], workers=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
                where=record.WhereNotSaid(kind="not said"), progress=progress, stop_issues=[], trail=[])
            self.assertTrue(record.create_session(saved))
        self.die(*self.units)
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [])

    def test_account_stop_still_in_use_holds_a_crashed_unit(self) -> None:
        _ = self.add_unit("held")
        _ = self.hold("claude4", terminal=True)
        self.die("held")
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("terminal stop")):
            for minute in (0, 2, 4):
                _ = self.pass_at(minute)
        self.assertEqual(self.commands, [])

    def test_both_crash_accounts_refuse_one_push_per_outage_and_showrunner_each_pass(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.update(("claude2", "claude4"))
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4, 6, 8):
                _ = self.pass_at(minute)
            phone.assert_called_once()
            push = cast(RenderedPush, phone.call_args.args[0])
            self.assertEqual(push.id, "shutdown.restart_failed")
            self.assertEqual(push.values["account"], "claude4")
            self.assertIn("retry from claude4's recovery record", self.notices[0])
            self.assertEqual(len(self.notices), 3)
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.die("alpha")
            _ = self.pass_at(12)
            _ = self.pass_at(14)
            _ = self.pass_at(16)
            self.assertEqual(phone.call_count, 2)

    def test_move_record_owns_its_phone_alert_and_restart_uses_its_login(self) -> None:
        _ = self.fixture.groups(units="claude4")
        _ = self.add_unit("alpha")
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("moved"))
        _ = refusal_watch.run(NOW)
        self.refused.update(("claude4", "claude2"))
        for saved in record.all_session_records():
            def alerted(current: record.SessionShutdown) -> None:
                current["restore_alerted"] = "fixture worker sent move_failed"
            _ = record.update_session(saved["login"], saved["session"]["session_id"], alerted)
            claimed = outage_alerts.claim(saved["session"]["session_id"])
            assert isinstance(claimed, outage_alerts.AlertClaim)
            outage_alerts.delivered(claimed)
        with patch.object(quota_alert, "tell_user") as phone:
            for minute in (2, 4, 6, 8):
                _ = self.pass_at(minute)
        self.assertEqual([argv[3] for argv in self.commands], ["claude4"] * 3)
        phone.assert_not_called()

    def test_pending_detached_restarts_do_not_wait_or_start_duplicate_commands(self) -> None:
        self.finish_jobs = False
        for index in range(30):
            _ = self.add_unit("down-" + str(index))
        self.die(*self.units)
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 30)
        self.assertEqual(self.commands, [])
        state = cast(object, json.loads((self.home / ".local/state/accounts/unit_watchdog.json").read_text()))
        assert isinstance(state, dict)
        self.assertEqual(len(cast(dict[str, object], state)), 30)
        _ = self.pass_at(4)
        self.assertEqual(len(self.pending), 30)
        self.assertEqual(self.commands, [])

    def test_gone_folder_ends_recovery_and_alerts_once(self) -> None:
        owner = self.add_unit("alpha")
        Path(owner["cwd"]).rmdir()
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4):
                _ = self.pass_at(minute)
            Path(owner["cwd"]).mkdir()
            _ = self.pass_at(6)
            phone.assert_called_once()
        self.assertEqual(self.pending, [])
        self.assertEqual(self.commands, [])
        self.assertTrue(any("no longer exists; restore dropped" in line for line in self.notices))

    def test_folder_lost_after_dispatch_retains_record_and_retries_its_move_alert(self) -> None:
        saved = self.moved_outage()
        # This recovery exists only in its shutdown record.
        record.write_snapshot(record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 1)
        Path(saved["session"]["cwd"]).rmdir()
        command = self.pending[0]
        offset = command.index("--recover")

        def restart_command(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertIn("--no-alert", argv)
            output = io.StringIO()
            with redirect_stdout(output):
                status = restart.up(saved["login"], only=(saved["session"]["session_id"],))
            self.assertIn("no longer exists; restore the folder", output.getvalue())
            return subprocess.CompletedProcess(argv, status, output.getvalue(), "")

        with patch.object(subprocess, "run", side_effect=restart_command):
            unit_watchdog._finish_recovery(command[offset + 1], command[offset + 2],  # pyright: ignore[reportPrivateUsage]
                Path(command[offset + 3]), command[offset + 4])
        retained = record.find_session(saved["login"], saved["session"]["session_id"])
        assert retained is not None
        self.assertEqual(retained["progress"]["kind"], "restart failed")
        self.assertEqual(record.all_snapshots()[0]["sessions"], [])
        result = Path(command[offset + 3])
        finished = result.read_text()
        result.unlink()
        # The retained move record owns its alert even before the job result arrives.
        with patch.object(quota_alert, "tell_user", side_effect=[
                quota_alert.PushFailed("fixture delivery failed"), quota_alert.PushSent()]) as phone:
            _ = self.pass_at(3)
            phone.assert_called_once()
            push = cast(RenderedPush, phone.call_args.args[0])
            self.assertEqual(push.id, "shutdown.move_failed")
            self.assertEqual(push.values["source"], "claude4")
            self.assertEqual(push.values["target"], "claude2")
            self.assertEqual(push.values["session"], saved["session"]["name"])
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            self.assertIn("job", state["alpha"])
            _ = result.write_text(finished)
            _ = self.pass_at(4)
            self.assertEqual(phone.call_count, 2)
            _ = self.pass_at(6)
            self.assertEqual(phone.call_count, 2)
            _ = self.pass_at(8)
            self.assertEqual(phone.call_count, 2)
        self.assertTrue(any("no longer exists" in line for line in self.notices))

    def fail_moved_restart(self, saved: record.SessionShutdown) -> None:
        with (patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
              patch.object(restart, "_launch_session", side_effect=OSError("both accounts refused")) as launch,
              redirect_stdout(io.StringIO())):
            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=restart.DesktopLaunchState())
        self.assertEqual(launch.call_count, 2)

    def fail_pending_restore_with_missing_folder(self, saved: record.SessionShutdown) -> None:
        command = self.pending[-1]
        offset = command.index("--recover")
        Path(saved["session"]["cwd"]).rmdir()

        def restart_command(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertIn("--no-alert", argv)
            output = io.StringIO()
            with redirect_stdout(output):
                status = restart.up(saved["login"], only=(saved["session"]["session_id"],))
            self.assertIn("no longer exists; restore the folder", output.getvalue())
            return subprocess.CompletedProcess(argv, status, output.getvalue(), "")

        with patch.object(subprocess, "run", side_effect=restart_command):
            unit_watchdog._finish_recovery(command[offset + 1], command[offset + 2],  # pyright: ignore[reportPrivateUsage]
                Path(command[offset + 3]), command[offset + 4])
        retained = record.find_session(saved["login"], saved["session"]["session_id"])
        assert retained is not None
        self.assertEqual(retained["progress"]["kind"], "restart failed")

    def test_clear_removes_only_claims_before_the_live_observation(self) -> None:
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                identity = "delivered" if delivered else "claimed"
                with patch.object(time, "time", return_value=1000.0):
                    claimed = outage_alerts.claim(identity)
                assert isinstance(claimed, outage_alerts.AlertClaim)
                if delivered:
                    outage_alerts.delivered(claimed)
                self.assertFalse(outage_alerts.clear(identity, 999.0))
                self.assertFalse(outage_alerts.clear(identity, 1000.0))
                with patch.object(time, "time", return_value=1001.0):
                    self.assertIsInstance(outage_alerts.claim(identity),
                                          outage_alerts.AlertDelivered if delivered else outage_alerts.AlertInFlight)
                self.assertTrue(outage_alerts.clear(identity, time.time() + 1))
                self.assertIsInstance(outage_alerts.claim(identity), outage_alerts.AlertClaim)

    def test_old_live_observation_preserves_a_new_record_alert_mark(self) -> None:
        saved = self.moved_outage()
        with patch.object(time, "time", return_value=1000.0):
            claimed = outage_alerts.claim("alpha")
            assert isinstance(claimed, outage_alerts.AlertClaim)
            self.assertTrue(record.claim_restore_alert(saved, "new outage"))
            outage_alerts.delivered(claimed)
        restart._clear_live_outage(saved, 999.0)  # pyright: ignore[reportPrivateUsage]
        current = record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertEqual(current.get("restore_alerted"), "new outage")
        restart._clear_live_outage(saved, 1001.0)  # pyright: ignore[reportPrivateUsage]
        current = record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertNotIn("restore_alerted", current)

    def test_slow_snapshot_probe_preserves_a_new_watchdog_alert(self) -> None:
        beta = self.add_unit("beta")
        _ = self.add_unit("alpha")
        self.die("beta")
        self.finish_jobs = False
        sessions.sessions_dir().mkdir(parents=True, exist_ok=True)
        clock = [1000.0]

        delayed = [False]

        def slow_probe(pid: int, _start: str) -> restart.SessionNotLive:
            if pid == beta["pid"] and not delayed[0]:
                delayed[0] = True
                clock[0] += 10
                self.die("alpha")
                for minute in (0, 2, 8):
                    _ = self.pass_at(minute)
                self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            return restart.SessionNotLive()

        with (patch.object(time, "time", side_effect=lambda: clock[0]),
              patch.object(restart, "_process_liveness", side_effect=slow_probe) as probe,
              patch.object(restart, "_launch_session", side_effect=OSError("fixture refused")),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              redirect_stdout(io.StringIO())):
            _ = restart.up("four@example.invalid", only=("beta", "alpha"))
            self.assertGreater(probe.call_count, 0)
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            _ = self.pass_at(14)
            alpha_pushes = [call for call in phone.call_args_list
                           if cast(RenderedPush, call.args[0]).values.get("stopped") == "owner alpha"]
            self.assertEqual(len(alpha_pushes), 1)

    def test_slow_batch_archive_preserves_a_new_outage_alert(self) -> None:
        self.slow_confirmation_outage(conductor=False)

    def test_slow_conductor_archive_preserves_a_new_outage_alert(self) -> None:
        self.slow_confirmation_outage(conductor=True)

    def slow_confirmation_outage(self, *, conductor: bool) -> None:
        alpha = self.moved_outage()
        beta_owner = self.add_unit("beta")
        beta = alpha.copy()
        beta.update(session=beta_owner, request_id="beta-move",
            timers=[record.TimerRestore(instance="slow-beta", was_enabled=True)])
        self.assertTrue(record.create_session(beta))
        self.die("beta")
        clock = [time.time() + 10]
        old = outage_alerts.claim("alpha")
        assert isinstance(old, outage_alerts.AlertClaim)
        outage_alerts.delivered(old)

        def recipient(identity: str) -> herdr.PromptRecipientProcess:
            owner = self.units[identity]
            return herdr.PromptRecipientProcess(owner["pid"], owner["proc_start"])

        def launch(saved: record.SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState,
                   *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            identity = saved["session"]["session_id"]
            self.revive(identity, "claude2")
            return restart.SessionLaunched(herdr.PromptSubmitted(NOW, recipient(identity)))

        def slow_timer(_verb: str, instance: str) -> None:
            self.assertEqual(instance, "slow-beta")
            current = record.find_session(alpha["login"], "alpha")
            assert current is not None
            self.assertEqual(current["progress"]["kind"], "restarted")
            clock[0] += 10
            self.die("alpha")
            for minute in (0, 2, 8):
                _ = self.pass_at(minute)
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)

        with (patch.object(time, "time", side_effect=lambda: clock[0]),
              patch.object(restart, "_retire"),
              patch.object(restart, "_launch_session", side_effect=launch),
              patch.object(herdr, "prompt_recipient_process", side_effect=recipient),
              patch.object(sessions, "remote_control", return_value="active"),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              patch.object(settle, "run_notifier", side_effect=slow_timer)):
            launched = [restart.relaunch_session(saved, dry_run=False,
                desktop_state=restart.DesktopLaunchState()) for saved in (alpha, beta)]
            if conductor:
                for saved in launched:
                    progress = saved["progress"]
                    assert progress["kind"] == "restarting"
                    restart.confirm_entry(saved, progress["at"], True, observed_at=time.time())
                for saved in launched:
                    self.assertTrue(settle._archive_finished(saved))  # pyright: ignore[reportPrivateUsage]
            else:
                restart._confirm_entries(launched)  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(record.find_session(alpha["login"], "alpha"))
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            _ = self.pass_at(14)
            phone.assert_called_once()
            clock[0] += 10
            self.revive("alpha", "claude2")
            _ = self.pass_at(16)
            # The live snapshot pass provides recovery for the next outage.
            seen_at = (NOW + timedelta(minutes=16)).isoformat()
            record.write_snapshot(record.RecoverySnapshot(machine="natedev", login="main@example.invalid",
                label="claude2", taken_at=seen_at, sessions=[record.SnapshotClaudeSession(agent="claude",
                    session=self.units["alpha"], workers=[], timers=[], last_seen=seen_at,
                    state="unresolved", crash="agent exited")]))
            self.die("alpha")
            for minute in (18, 20, 26):
                _ = self.pass_at(minute)
            self.assertEqual(phone.call_count, 2)

    def test_dead_alert_owner_is_replaced_and_its_old_token_cannot_finish(self) -> None:
        first = outage_alerts.claim("dead-owner")
        assert isinstance(first, outage_alerts.AlertClaim)
        with patch.object(os, "kill", side_effect=ProcessLookupError):
            second = outage_alerts.claim("dead-owner")
        self.assertIsInstance(second, outage_alerts.AlertClaim)
        assert isinstance(second, outage_alerts.AlertClaim)
        self.assertNotEqual(first.token, second.token)
        outage_alerts.delivered(first)
        outage_alerts.release(first)
        self.assertIsInstance(outage_alerts.claim("dead-owner"), outage_alerts.AlertInFlight)
        outage_alerts.delivered(second)
        self.assertIsInstance(outage_alerts.claim("dead-owner"), outage_alerts.AlertDelivered)

    def test_live_alert_owner_keeps_its_claim_inside_the_lease(self) -> None:
        first = outage_alerts.claim("live-owner")
        self.assertIsInstance(first, outage_alerts.AlertClaim)
        self.assertIsInstance(outage_alerts.claim("live-owner"), outage_alerts.AlertInFlight)

    def test_alert_claim_past_the_lease_is_replaced(self) -> None:
        with patch.object(time, "time", return_value=1000):
            first = outage_alerts.claim("expired-owner")
        with patch.object(time, "time", return_value=2801):
            second = outage_alerts.claim("expired-owner")
        self.assertIsInstance(second, outage_alerts.AlertClaim)
        assert isinstance(first, outage_alerts.AlertClaim) and isinstance(second, outage_alerts.AlertClaim)
        self.assertNotEqual(first.token, second.token)

    def test_conductor_delivery_and_archive_share_the_watchdogs_alert(self) -> None:
        for archived in (False, True):
            with self.subTest(archived=archived):
                saved = self.moved_outage()
                if archived:
                    saved["requested_at"] = (NOW + timedelta(seconds=1)).isoformat()
                    _ = record.update_session(saved["login"], "alpha", lambda current: current.update(
                        requested_at=saved["requested_at"]))
                _ = self.pass_at(0)
                _ = self.pass_at(2)
                saved["alerts"] = "user"
                saved["progress"] = record.SessionRestartFailed(kind="restart failed", at=NOW.isoformat(), reason="both refused")
                _ = record.update_session(saved["login"], "alpha", lambda current: current.update(
                    alerts=saved["alerts"], progress=saved["progress"]))
                with (patch.object(restart, "restart_failed_liveness", return_value=restart.SessionNotLive()),
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                      patch.object(settle, "tell_showrunners_outcome"),
                      patch.object(settle, "_deliver_message", return_value=settle.MessageSent(kind="sent")) as report_phone,
                      patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as watchdog_phone,
                      redirect_stdout(io.StringIO())):
                    settle.report_stop([saved])
                    report_phone.assert_called_once()
                    if archived:
                        self.fail_pending_restore_with_missing_folder(saved)
                    _ = self.pass_at(10)
                    _ = self.pass_at(12)
                    watchdog_phone.assert_not_called()
                self.revive("alpha", "claude4")
                _ = self.pass_at(14)
                if archived:
                    Path(saved["session"]["cwd"]).mkdir(exist_ok=True)
                else:
                    record.archive_session(saved["login"], "alpha")

    def test_failed_conductor_push_allows_one_later_watchdog_delivery(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        saved["alerts"] = "user"
        saved["progress"] = record.SessionRestartFailed(kind="restart failed", at=NOW.isoformat(), reason="both refused")
        _ = record.update_session(saved["login"], "alpha", lambda current: current.update(
                    alerts=saved["alerts"], progress=saved["progress"]))
        with (patch.object(restart, "restart_failed_liveness", return_value=restart.SessionNotLive()),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
              patch.object(settle, "tell_showrunners_outcome"),
              patch.object(settle, "_deliver_message", side_effect=RuntimeError("fixture push failed")),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              redirect_stdout(io.StringIO())):
            settle.report_stop([saved])
            _ = self.pass_at(8)
            _ = self.pass_at(14)
            phone.assert_called_once()

    def test_worker_delivery_survives_a_later_retained_missing_folder(self) -> None:
        saved = self.moved_outage()
        record.write_snapshot(record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            self.fail_moved_restart(saved)
            phone.assert_called_once()
            self.assertEqual(cast(RenderedPush, phone.call_args.args[0]).id, "shutdown.move_failed")
            self.fail_pending_restore_with_missing_folder(saved)
            _ = self.pass_at(10)
            _ = self.pass_at(12)
            phone.assert_called_once()
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(state["alpha"]["alerted"])

    def test_watchdog_during_failed_worker_delivery_does_not_remember_a_claim_as_sent(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        attempts: list[RenderedPush] = []

        def deliver(push: RenderedPush) -> quota_alert.PushSent | quota_alert.PushFailed:
            attempts.append(push)
            if len(attempts) == 1:
                current = record.find_session(saved["login"], "alpha")
                assert current is not None
                self.assertIn("restore_alerted", current)
                # Run the real watcher while the worker's delivery is unfinished.
                _ = self.pass_at(8)
                return quota_alert.PushFailed("fixture delivery failed")
            return quota_alert.PushSent()

        with patch.object(quota_alert, "tell_user", side_effect=deliver):
            self.fail_moved_restart(saved)
            self.assertEqual(len(attempts), 1)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            _ = self.pass_at(14)
            _ = self.pass_at(20)
        self.assertEqual(len(attempts), 2)

    def test_failed_worker_delivery_allows_one_move_alert_after_retained_failure(self) -> None:
        saved = self.moved_outage()
        record.write_snapshot(record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushFailed("delivery failed")) as worker_phone:
            self.fail_moved_restart(saved)
            worker_phone.assert_called_once()
        # The pass observes the failed delivery while the worker is still pending.
        _ = self.pass_at(3)
        self.fail_pending_restore_with_missing_folder(saved)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            _ = self.pass_at(4)
            _ = self.pass_at(6)
            phone.assert_called_once()
            self.assertEqual(cast(RenderedPush, phone.call_args.args[0]).id, "shutdown.move_failed")

    def test_worker_delivery_then_live_unit_allows_one_new_outage_alert(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            self.fail_moved_restart(saved)
            _ = self.pass_at(8)
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
            current = record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertNotIn("restore_alerted", current)
            self.die("alpha")
            _ = self.pass_at(12)
            _ = self.pass_at(14)
            self.fail_moved_restart(current)
            _ = self.pass_at(20)
            _ = self.pass_at(26)
            self.assertEqual(phone.call_count, 2)

    def test_watchdog_notice_uses_the_users_zone(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        with patch.object(unit_watchdog, "user_zone", return_value=ZoneInfo("Asia/Tokyo")):
            _ = self.pass_at(0)
            _ = self.pass_at(2)
            _ = self.pass_at(4)
        self.assertIn("down since 21:00 JST", self.notices[0])

    def test_grace_marks_an_agent_exit_before_a_real_snapshot_merge_and_restart(self) -> None:
        self.grace_snapshot_restart(probe_unreadable=False)

    def test_unreadable_grace_probe_keeps_recovery_until_a_later_readable_probe(self) -> None:
        self.grace_snapshot_restart(probe_unreadable=True)

    def test_newer_store_snapshot_replaces_an_unreadable_grace_cache(self) -> None:
        self.grace_snapshot_restart(probe_unreadable=True, newer_snapshot=True)

    def grace_snapshot_restart(self, *, probe_unreadable: bool, newer_snapshot: bool = False) -> None:
        owner = self.add_unit("alpha")
        host = owner["host"]
        assert host["kind"] == "unit"
        host["session_name"] = "shared"
        snapshot = record.read_snapshot("four@example.invalid")
        assert not isinstance(snapshot, record.NoRecoverySnapshot)
        candidate = snapshot["sessions"][0]
        assert candidate["agent"] == "claude"
        candidate["session"] = owner
        _ = snapshot["sessions"][0].pop("crash", None)
        record.write_snapshot(snapshot)
        self.die("alpha")
        self.finish_jobs = False
        with patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()):
            self.assertIsInstance(restart.watchdog_crash_signal(owner), restart.AgentCrashConfirmed)
            if probe_unreadable:
                with patch.object(herdr.Herdr, "agents", side_effect=OSError("pane unavailable")):
                    _ = self.pass_at(0)
            else:
                lines = self.pass_at(0)
                self.assertEqual(lines, [])
        retained = record.read_snapshot("four@example.invalid")
        assert not isinstance(retained, record.NoRecoverySnapshot)
        self.assertEqual(retained["sessions"][0].get("crash"), None if probe_unreadable else "agent exited")
        merged = record.merge_snapshots(inventory.CompleteInventory("natedev", {}, {}),
                                         (NOW + timedelta(minutes=1)).isoformat())
        if probe_unreadable:
            self.assertEqual(merged[0]["sessions"], [])
            with (patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
                  patch.object(herdr.Herdr, "agents", side_effect=OSError("still unavailable"))):
                _ = self.pass_at(2)
            self.assertEqual(self.pending, [])
        else:
            self.assertEqual(merged[0]["sessions"][0].get("crash"), "agent exited")
        if newer_snapshot:
            candidate["last_seen"] = (NOW + timedelta(minutes=3)).isoformat()
            candidate["crash"] = "agent exited"
            snapshot["taken_at"] = candidate["last_seen"]
            record.write_snapshot(snapshot)
        with patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()):
            _ = self.pass_at(4 if probe_unreadable else 2)
        self.assertEqual(len(self.pending), 1)
        command = self.pending[0]
        offset = command.index("--recover")
        recipient = herdr.PromptRecipientProcess(owner["pid"], owner["proc_start"])
        submitted = herdr.PromptSubmitted(NOW, recipient)
        def launch(argv: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
            self.assertFalse(dry_run)
            self.assertFalse(check)
            self.assertTrue(any("add_unit" in part for part in argv))
            self.revive("alpha", "claude2")
            return subprocess.CompletedProcess(argv, 0,
                "restart-prompt-outcome: " + herdr.encode_prompt_outcome(submitted) + "\n", "")
        def confirmation(_ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
            return restart.RegistryConfirmation(tuple(self.fixture.live))
        with (patch.dict(os.environ, {"UNIT_WATCHDOG_SESSION": "alpha", "UNIT_WATCHDOG_SOURCE": command[offset + 4]}),
              patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "watchdog_crash_signal", side_effect=AssertionError("stored exit needs no second pane probe")),
              patch.object(restart, "_retire"),
              patch.object(herdr, "prompt_recipient_process", return_value=recipient),
              patch.object(sessions, "remote_control", return_value="active"),
              patch.object(restart, "_run", side_effect=launch),
              patch.object(restart, "wait_for_confirmation", side_effect=confirmation)):
            self.assertEqual(restart.up("four@example.invalid", only=("alpha",)), 0)
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, {"alpha"})

    def test_all_detach_calls_share_one_deadline_and_leave_the_rest_due(self) -> None:
        self.finish_jobs = False
        for index in range(20):
            _ = self.add_unit("down-" + str(index))
        self.die(*self.units)
        _ = self.pass_at(0)
        elapsed = [0.0]
        def slow(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            elapsed[0] += cast(float, options["timeout"])
            return self.command(argv, **options)
        with (patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
              patch.object(subprocess, "run", side_effect=slow)):
            _ = self.pass_at(2)
            self.assertLessEqual(elapsed[0], 30)
            self.assertEqual(len(self.pending), 6)
            _ = self.pass_at(4)
            self.assertEqual(len(self.pending), 12)
        self.assertEqual(self.commands, [])

    def test_expired_job_consumed_before_its_stale_restarting_record_blocks(self) -> None:
        owner = self.add_unit("alpha")
        self.die("alpha")
        self.finish_jobs = False
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        snapshot = record.read_snapshot("four@example.invalid")
        assert not isinstance(snapshot, record.NoRecoverySnapshot)
        entry = snapshot["sessions"][0]
        assert entry["agent"] == "claude"
        saved = record.materialize_snapshot(snapshot, entry)
        def interrupted(current: record.SessionShutdown) -> None:
            prior = current["progress"]
            assert prior["kind"] != "restarting"
            current["progress"] = record.SessionRestarting(kind="restarting", at=NOW.isoformat(), prior=prior)
        _ = record.update_session(saved["login"], owner["session_id"], interrupted)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            lines = self.pass_at(8)
            self.assertTrue(any("did not finish within 5 minutes" in line for line in lines))
            self.assertEqual(len(self.pending), 2)
            phone.assert_called_once()
            _ = self.pass_at(14)
            self.assertEqual(phone.call_count, 1)
            self.assertEqual(len(self.pending), 3)

    def test_expired_move_worker_alerts_once_unless_restart_already_pushed(self) -> None:
        for marked in (False, True):
            with self.subTest(restore_alerted=marked):
                name = "marked" if marked else "unmarked"
                owner = self.add_unit(name)
                saved = record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
                    requested_at=NOW.isoformat(), request_id=name, requested_by=record.FromTerminal(kind="terminal"),
                    hold="session", then="restart", force="now", alerts="none", probe=False,
                    session=owner, workers=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
                    where=record.WhereNotSaid(kind="not said"), progress=record.SessionStopped(kind="stopped", at=NOW.isoformat()),
                    stop_issues=[], trail=[], target="claude2", source_label="claude4")
                self.assertTrue(record.create_session(saved))
                if marked:
                    claimed = outage_alerts.claim(name)
                    assert isinstance(claimed, outage_alerts.AlertClaim)
                    outage_alerts.delivered(claimed)
                self.die(name)
                self.finish_jobs = False
                _ = self.pass_at(0)
                _ = self.pass_at(2)
                def interrupted(current: record.SessionShutdown) -> None:
                    prior = current["progress"]
                    assert prior["kind"] != "restarting"
                    current["progress"] = record.SessionRestarting(kind="restarting", at=NOW.isoformat(), prior=prior)
                _ = record.update_session(saved["login"], name, interrupted)
                with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
                    _ = self.pass_at(8)
                    self.assertEqual(phone.call_count, 0 if marked else 1)
                    if not marked:
                        push = cast(RenderedPush, phone.call_args.args[0])
                        self.assertEqual(push.id, "shutdown.move_failed")
                    _ = self.pass_at(14)
                    self.assertEqual(phone.call_count, 0 if marked else 1)
                self.revive(name, "claude4")
                _ = self.pass_at(16)

    def moved_outage(self) -> record.SessionShutdown:
        owner = self.add_unit("alpha")
        saved = record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=NOW.isoformat(), request_id="alpha-move", requested_by=record.FromTerminal(kind="terminal"),
            hold="session", then="restart", force="now", alerts="none", probe=False,
            session=owner, workers=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
            where=record.WhereNotSaid(kind="not said"), progress=record.SessionStopped(kind="stopped", at=NOW.isoformat()),
            stop_issues=[], trail=[], target="claude2", source_label="claude4")
        self.assertTrue(record.create_session(saved))
        self.die("alpha")
        self.finish_jobs = False
        return saved

    def test_worker_alert_after_source_read_prevents_watchdog_duplicate(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        def worker_alert(slug: str, key: str, text: str) -> str:
            # The worker finishes after the watcher has read sources, before its alert.
            current = record.find_session(saved["login"], "alpha")
            assert current is not None
            self.fail_moved_restart(current)
            return self.tell(slug, key, text)
        with (patch.object(showrunners, "tell", side_effect=worker_alert),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone):
            _ = self.pass_at(8)
            phone.assert_called_once()

    def test_failed_move_push_releases_claim_and_retries_until_sent(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", side_effect=[
                quota_alert.PushFailed("fixture delivery failed"), quota_alert.PushSent()]) as phone:
            _ = self.pass_at(8)
            current = record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertNotIn("restore_alerted", current)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            _ = self.pass_at(14)
            self.assertEqual(phone.call_count, 2)
            current = record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertIn("restore_alerted", current)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(state["alpha"]["alerted"])
            _ = self.pass_at(20)
            self.assertEqual(phone.call_count, 2)

    def test_watchdog_live_observation_clears_move_alert_for_the_next_outage(self) -> None:
        saved = self.moved_outage()
        restart.settle.mark_not_back_pushed(saved)
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.revive("alpha", "claude4")
        _ = self.pass_at(4)
        current = record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertNotIn("restore_alerted", current)
        self.die("alpha")
        _ = self.pass_at(6)
        _ = self.pass_at(8)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            _ = self.pass_at(14)
            _ = self.pass_at(20)
            phone.assert_called_once()

    def test_old_terminal_stop_does_not_hold_a_newer_session_for_the_same_unit(self) -> None:
        old = self.fixture.owner("old-alpha", host="unit", label="claude4")
        host = old["host"]
        assert host["kind"] == "unit"
        host["unit"] = "alpha"
        earlier = (NOW - timedelta(minutes=10)).isoformat()
        saved = record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=earlier, request_id="old-stop", requested_by=record.FromTerminal(kind="terminal"),
            hold="session", then="stop", force="wait for ready", alerts="none", probe=False,
            session=old, workers=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
            where=record.WhereNotSaid(kind="not said"), progress=record.SessionStopped(kind="stopped", at=earlier),
            stop_issues=[], trail=[])
        self.assertTrue(record.create_session(saved))
        _ = self.add_unit("alpha")
        self.die("alpha", "old-alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][self.commands[0].index("--only") + 1], "alpha")
        self.assertIn("restarted on claude4", self.notices[0])

    def test_dry_run_and_mac_write_nothing_and_launch_nothing(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        _ = self.pass_at(0)
        before = self.fixture.snapshot()
        lines = self.pass_at(2, dry_run=True)
        self.assertIn("would restart alpha from claude4's recovery record", "\n".join(lines))
        self.assertEqual(self.fixture.snapshot(), before)
        with patch.object(role_map, "is_role_map_host", return_value=False):
            self.assertEqual(self.pass_at(2), [])
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    _ = unittest.main()
