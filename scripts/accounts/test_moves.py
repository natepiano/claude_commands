"""Move requests, outcomes and waits remain distinct in an append-only journal."""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TextIO, cast, override
from unittest.mock import patch

from scripts.accounts.home_test_support import scratch_account_home
from scripts.accounts import accounts, moves, pane_moves, role_map, worker_names
from scripts.herdr import worker_panes
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import unit_runs
from scripts.production import staff, session_staff
from scripts.production.staff_test_support import (
    StaffTestCase, fixture_member, fixture_worker, fixture_runtime_id,
)
from scripts.shutdown import inventory, stop_results
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.account_test_support import create_claude_account, write_codex_auth


class SingleSessionMoveNoticeTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(scratch_account_home(self.home, {}, clear=True))

    def move_lines(self, kind: Literal["unit", "showrunner", "top-level"], target: str, *, dry_run: bool) -> list[str]:
        owner = inventory.ListedSession("session-alpha", 123, "fixture-start", "alpha", kind,
                                        self.home / ".claude1", "fixture-login", "fixture", "")
        checked = accounts.MoveTargetReady(accounts.NamedAccount(target, self.home / ("." + target)))
        with (patch.object(accounts, "import_module", return_value=inventory),
              patch.object(inventory, "listing", return_value=[owner]),
              patch.object(accounts, "check_move_target", return_value=checked),
              patch.object(accounts, "session_account_label", return_value="claude1"),
              patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts(
                  units="claude4", showrunners="claude4")),
              patch.object(accounts, "move_sessions", return_value=(stop_results.ClaudeUp(
                  owner.session_id, "w1:p1", target),)) as move):
            result = accounts.move(owner.name, target, dry_run=dry_run)
        if dry_run:
            self.assertIsInstance(result, accounts.MoveDryRun)
            assert isinstance(result, accounts.MoveDryRun)
            move.assert_not_called()
            return list(result.lines)
        self.assertIsInstance(result, accounts.MoveStarted)
        assert isinstance(result, accounts.MoveStarted)
        move.assert_called_once()
        return result.stdout.splitlines()

    def test_move_outside_the_group_account_names_the_return_account_and_group_command(self) -> None:
        for kind in ("unit", "showrunner"):
            group = "units" if kind == "unit" else "showrunners"
            for dry_run in (False, True):
                with self.subTest(kind=kind, dry_run=dry_run):
                    lines = self.move_lines(kind, "claude3", dry_run=dry_run)
                    self.assertEqual(lines[-1], "alpha returns to claude4 at its next resume; "
                                     + f"to keep claude3, run: accounts.py move {group} claude3")
                    self.assertEqual(sum("returns to" in line for line in lines), 1)

    def test_move_to_the_group_account_has_no_return_notice(self) -> None:
        for kind in ("unit", "showrunner"):
            for dry_run in (False, True):
                with self.subTest(kind=kind, dry_run=dry_run):
                    lines = self.move_lines(kind, "claude4", dry_run=dry_run)
                    self.assertFalse(any("returns to" in line for line in lines))

    def test_ad_hoc_move_has_no_return_notice(self) -> None:
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                lines = self.move_lines("top-level", "claude3", dry_run=dry_run)
                self.assertFalse(any("returns to" in line for line in lines))


class MoveJournalTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(scratch_account_home(self.home, {}, clear=True)))

    def common(self, *, tool: str = "codex", subject: str = "workers") -> moves.MoveCommon:
        self.assertIn(tool, ("claude", "codex"))
        return moves.MoveCommon(at="2026-10-13T12:00:00+00:00",
            tool="claude" if tool == "claude" else "codex", subject=subject, move_id="transition-1")

    def requested(self) -> moves.RequestedMove:
        return {**self.common(), "kind": "requested", "from": "codex1", "to": "codex2",
            "why": "quota refusal", "window": "unknown", "cause": "refusal"}

    def test_replay_evidence_excludes_failed_requests_and_includes_scheduled_claude_routes(self) -> None:
        first: moves.RequestedMove = {**self.requested(), "cause": "auto", "why": "auto: workers-first"}
        failed: moves.RequestedMove = {**first, "move_id": "failed", "why": "auto: workers-retry"}
        unrelated: moves.RequestedMove = {**first, "subject": "units", "move_id": "units", "why": "auto: units"}
        moves.append_move(first)
        moves.append_move(failed)
        failed_common: moves.MoveCommon = {**self.common(), "move_id": "failed"}
        moves.append_move(moves.FailedMove(**failed_common, kind="failed", reason="refused"))
        moves.append_move(unrelated)
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=self.common()["at"],
            tool="codex", fallback_at=self.common()["at"], owner="codex1", cause="auto",
            why="auto: workers-claude", moved=["direct"]))
        evidence = moves.recorded_auto_entries()
        self.assertIsInstance(evidence, moves.RecordedAutoEntries)
        assert isinstance(evidence, moves.RecordedAutoEntries)
        self.assertEqual(evidence.entry_ids, frozenset({"workers-first", "workers-claude"}))

    def test_replay_reader_skips_only_a_torn_final_line_and_holds_the_journal_lock(self) -> None:
        moves.append_move({**self.requested(), "cause": "auto", "why": "auto: first"})
        path = moves.store_file()
        valid = path.read_bytes()
        for suffix, torn in ((b'{"kind":', True), (b'\xff', True),
                             (b'{broken\n', False), (b'{broken\n' + valid, False)):
            with self.subTest(suffix=suffix):
                _ = path.write_bytes(valid + suffix)
                output = io.StringIO()
                with redirect_stdout(output), patch.object(fcntl, "flock", wraps=fcntl.flock) as flock:
                    evidence = moves.recorded_auto_entries()
                self.assertTrue(flock.called)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                if torn:
                    self.assertIsInstance(evidence, moves.RecordedAutoEntries)
                    assert isinstance(evidence, moves.RecordedAutoEntries)
                    self.assertEqual(evidence.entry_ids, frozenset({"first"}))
                    self.assertIn("moves.jsonl", output.getvalue())
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(len(moves.read_journal()), 1)
                else:
                    self.assertIsInstance(evidence, moves.UnreadableMoveJournal)
                self.assertEqual(path.read_bytes(), valid + suffix)

    def test_an_auto_entry_read_recovers_interrupted_worker_name_conversion(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        row = {**self.requested(), "cause": "auto", "why": "auto: recovered"}
        complete = json.dumps(row) + "\n"
        _ = path.write_text(complete[:70] + "damaged old bytes\n")
        recovery = path.with_name(f".{path.name}.workers")
        _ = recovery.write_text(complete)
        with patch.object(fcntl, "flock", wraps=fcntl.flock) as flock:
            evidence = moves.recorded_auto_entries()
        self.assertEqual(evidence, moves.RecordedAutoEntries(frozenset({"recovered"})))
        self.assertEqual(path.read_text(), complete)
        self.assertFalse(recovery.exists())
        self.assertTrue(any(call.args[1] == fcntl.LOCK_EX for call in flock.call_args_list))

    def test_move_and_routing_appends_write_one_complete_encoded_line(self) -> None:
        with patch.object(os, "write", wraps=os.write) as write:
            moves.append_move(self.requested())
        write.assert_called_once()
        line = cast(bytes, write.call_args.args[1])
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(json.loads(line)["move_id"], "transition-1")
        with patch.object(os, "write", wraps=os.write) as write:
            moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=self.common()["at"],
                tool="codex", fallback_at=self.common()["at"], owner="codex1", cause="auto",
                why="auto: route", moved=["direct"]))
        write.assert_called_once()
        self.assertTrue(cast(bytes, write.call_args.args[1]).endswith(b"\n"))

    def test_append_after_schema_invalid_torn_tail_preserves_readable_replay_history(self) -> None:
        """F003: syntactically valid JSON can still be an interrupted journal row."""
        first: moves.RequestedMove = {**self.requested(), "cause": "auto", "why": "auto: first"}
        second: moves.RequestedMove = {**first, "move_id": "second", "why": "auto: second"}
        moves.append_move(first)
        path = moves.store_file()
        valid = path.read_bytes()
        _ = path.write_bytes(valid + b"{}")
        with redirect_stdout(io.StringIO()):
            before = moves.recorded_auto_entries()
        self.assertEqual(before, moves.RecordedAutoEntries(frozenset({"first"})))
        moves.append_move(second)
        self.assertEqual(moves.recorded_auto_entries(), moves.RecordedAutoEntries(frozenset({"first", "second"})))
        self.assertEqual(moves.read_moves(), [first, second])
        self.assertTrue(path.read_bytes().startswith(valid))

    def test_move_append_finishes_a_short_write_without_repeating_the_prefix(self) -> None:
        write = os.write
        calls: list[bytes] = []

        def short_write(descriptor: int, data: bytes) -> int:
            calls.append(data)
            return write(descriptor, data[:7] if len(calls) == 1 else data)

        with patch.object(os, "write", short_write):
            moves.append_move(self.requested())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], calls[0][7:])
        self.assertEqual(moves.read_moves(), [self.requested()])

    def test_legacy_claude_routing_fills_reason_from_cause_at_read_boundary(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"kind": "routed to claude", "at": self.common()["at"],
            "tool": "codex", "fallback_at": self.common()["at"], "owner": "codex1",
            "cause": "refusal", "moved": ["direct"]}) + "\n")
        rows = moves.read_journal()
        self.assertEqual(len(rows), 1)
        route = rows[0]
        assert route["kind"] == "routed to claude"
        self.assertEqual(route["why"], "refusal")

    def test_every_transition_kind_round_trips_without_losing_its_semantics(self) -> None:
        entries: list[moves.MoveEntry] = [
            self.requested(),
            moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", worker="worker-a"),
            moves.ConfirmedMove(**self.common(tool="claude", subject="conversation: unit"),
                kind="confirmed", config_dir="/temporary/claude3"),
            moves.FailedMove(**self.common(), kind="failed", reason="old writer still live"),
            moves.WaitedMove(**self.common(), kind="waited", window="five_hour", reset="2026-10-13T12:19:00+00:00"),
            moves.WaitEndedMove(**self.common(), kind="wait ended"),
            moves.NoRoomMove(**self.common(), kind="no room", earliest_reset="2026-10-15T12:00:00+00:00"),
            moves.RoomAgainMove(**self.common(), kind="room again", label="codex2"),
        ]
        for entry in entries:
            moves.append_move(entry)
        self.assertEqual(moves.read_moves(), entries)
        path = self.home / ".local/state/accounts/moves.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), len(entries))
        self.assertEqual({entry["move_id"] for entry in moves.read_moves()}, {"transition-1"})

    def test_legacy_labels_are_normalized_without_rewriting_the_journal(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {**self.requested(), "from": "codex 1", "to": "claude 2"},
            {**self.common(), "kind": "room again", "label": "codex 1"},
        ]
        original = "".join(json.dumps(entry) + "\n" for entry in records).encode()
        _ = path.write_bytes(original)
        loaded = moves.read_moves()
        self.assertEqual(loaded[0].get("from"), "codex1")
        self.assertEqual(loaded[0].get("to"), "claude2")
        self.assertEqual(loaded[1].get("label"), "codex1")
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_home_labels_use_the_role_evidence_in_force_at_each_move(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        roles = path.with_name("roles.jsonl")
        evidence = [
            {"at": "2026-10-12T00:00:00Z", "role": "unit", "label": "default", "account_label": "claude2"},
            {"at": "2026-10-12T00:00:00Z", "role": "workers", "label": "default", "account_label": "codex2"},
            {"at": "2026-10-14T00:00:00Z", "role": "unit", "label": "default", "account_label": "claude3"},
            {"at": "2026-10-14T00:00:00Z", "role": "workers", "label": "default", "account_label": "codex3"},
        ]
        _ = roles.write_text("".join(json.dumps(entry) + "\n" for entry in evidence))
        records = [
            {**self.common(tool="claude", subject="units"), "kind": "requested", "from": "claude1",
             "to": "default", "why": "quota", "window": "unknown"},
            {**self.requested(), "from": "default", "to": "codex1"},
            {**self.common(), "kind": "room again", "label": "default"},
            {**self.common(), "at": "2026-10-14T00:00:00Z", "kind": "room again", "label": "default"},
        ]
        original = "".join(json.dumps(entry) + "\n" for entry in records).encode()
        _ = path.write_bytes(original)
        before_roles = roles.read_bytes()

        loaded = moves.read_moves()

        self.assertEqual([entry.get("to") for entry in loaded[:2]], ["claude2", "codex1"])
        self.assertEqual(loaded[1].get("from"), "codex2")
        self.assertEqual([entry.get("label") for entry in loaded[2:]], ["codex2", "codex3"])
        # Today's login must never change the meaning of a historical home alias.
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "new@example.invalid"}}))
        notes = self.home / "rust/hanadocs/agents"
        notes.mkdir(parents=True)
        _ = (notes / "claude9.md").write_text("---\nlogin: new@example.invalid\n---\n")
        self.assertEqual(moves.read_moves(), loaded)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(roles.read_bytes(), before_roles)

    def test_legacy_home_without_historical_evidence_is_never_resolved_from_todays_login(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {**self.common(tool="claude", subject="units"), "kind": "requested", "from": "claude1",
             "to": "default", "why": "quota", "window": "unknown"},
            {**self.requested(), "from": "default", "to": "codex1"},
            {**self.common(), "kind": "room again", "label": "default"},
        ]
        original = "".join(json.dumps(entry) + "\n" for entry in records).encode()
        _ = path.write_bytes(original)
        notes = self.home / "rust/hanadocs/agents"
        notes.mkdir(parents=True)
        (self.home / ".claude").mkdir()
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "today@example.invalid"}}))
        _ = (notes / "claude9.md").write_text("---\nlogin: today@example.invalid\n---\n")
        codex = self.home / ".codex"
        codex.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({"email": "today@example.invalid"}).encode()).decode().rstrip("=")
        _ = (codex / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (notes / "codex9.md").write_text("---\nlogin: today@example.invalid\n---\n")
        # Later role evidence cannot establish which login a home held yesterday.
        _ = path.with_name("roles.jsonl").write_text(json.dumps({
            "at": "2026-10-14T00:00:00Z", "role": "unit", "label": "default", "account_label": "claude9"}) + "\n")
        loaded = moves.read_moves()
        for entry, field, home in ((loaded[0], "to", self.home / ".claude"),
                                   (loaded[1], "from", self.home / ".codex"),
                                   (loaded[2], "label", self.home / ".codex")):
            value = entry.get(field)
            self.assertIsInstance(value, moves.HomeAccountUnresolved)
            assert isinstance(value, moves.HomeAccountUnresolved)
            self.assertEqual(value.home, home)
        from scripts.accounts import accounts
        rendered = "\n".join(accounts._move_status_lines(loaded, timezone.utc))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("home account unresolved", rendered)
        self.assertNotIn("default", rendered)
        self.assertEqual(path.read_bytes(), original)

    def test_historical_source_uses_the_move_login_and_ignores_stale_shutdown_state(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        notes = self.home / "rust/hanadocs/agents"
        notes.mkdir(parents=True)
        _ = (notes / "claude2.md").write_text("---\nlogin: source@example.invalid\n---\n")
        _ = (notes / "claude3.md").write_text("---\nlogin: later@example.invalid\n---\n")
        configuration = self.home / ".claude/config/accounts.json"
        configuration.parent.mkdir(parents=True)
        _ = configuration.write_text(json.dumps({"home_cutovers": [{"at": "2026-10-14T00:00:00Z",
            "label": "claude3", "login": "later@example.invalid"}]}))
        record = self.home / ".local/state/shutdown/session.json"
        record.parent.mkdir(parents=True)
        _ = record.write_text(json.dumps({"request_id": "attempt", "login": "later@example.invalid",
            "source_label": "default", "session": {"config_dir": str(self.home / ".claude")}, "target": "default"}))
        original = json.dumps({**self.common(tool="claude"), "request_id": "attempt", "kind": "requested",
            "from": "default", "to": "default", "login": "source@example.invalid", "window": "unknown", "why": "fixture"}) + "\n"
        _ = path.write_text(original)
        loaded = moves.read_moves()
        self.assertEqual(loaded[0].get("from"), "claude2")
        self.assertEqual(loaded[0].get("to"), "claude3")
        self.assertEqual(path.read_text(), original)

    def test_superseded_home_role_evidence_leaves_later_legacy_moves_unresolved(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        roles = path.with_name("roles.jsonl")
        assignments = [
            {"at": "2026-10-11T00:00:00Z", "role": "workers", "label": "default", "account_label": "codex2"},
            {"at": "2026-10-12T00:00:00Z", "role": "workers", "label": "codex3"},
        ]
        _ = roles.write_text("".join(json.dumps(row) + "\n" for row in assignments))
        entries = [{**self.requested(), "to": "default"},
                   {**self.common(), "kind": "room again", "label": "default"}]
        original = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
        _ = path.write_bytes(original)
        role_bytes = roles.read_bytes()
        loaded = moves.read_moves()
        for entry, field in ((loaded[0], "to"), (loaded[1], "label")):
            value = entry.get(field)
            self.assertIsInstance(value, moves.HomeAccountUnresolved)
            assert isinstance(value, moves.HomeAccountUnresolved)
            self.assertEqual(value.home, self.home / ".codex")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(roles.read_bytes(), role_bytes)

    def test_codex_home_cutover_resolves_only_earlier_moves_and_never_uses_claude_evidence(self) -> None:
        configuration = self.home / ".claude/config/accounts.json"
        configuration.parent.mkdir(parents=True)
        evidence = {"home_cutovers": [
            {"at": "2026-10-12T00:00:00Z", "label": "claude4", "login": "claude@example.invalid"},
            {"at": "2026-10-14T00:00:00Z", "label": "codex2", "login": "codex@example.invalid"},
        ]}
        _ = configuration.write_text(json.dumps(evidence))
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        records: list[dict[str, object]] = [
            {**self.requested(), "from": "~/.codex", "to": "codex1"},
            {**self.requested(), "move_id": "absolute-home", "from": "codex1", "to": str(self.home / ".codex")},
            {**self.common(), "kind": "room again", "label": "~/.codex"},
            {**self.common(), "at": "2026-10-14T00:00:00Z", "kind": "room again", "label": "~/.codex"},
            {**self.common(), "at": "2026-10-15T00:00:00Z", "kind": "room again", "label": "~/.codex"},
            {**self.common(tool="claude"), "kind": "room again", "label": "~/.claude"},
        ]
        original = "".join(json.dumps(entry) + "\n" for entry in records)
        _ = path.write_text(original)
        config_before = configuration.read_bytes()
        loaded = moves.read_journal()
        self.assertEqual(loaded[0].get("from"), "codex2")
        self.assertEqual(loaded[1].get("to"), "codex2")
        self.assertEqual(loaded[2].get("label"), "codex2")
        for entry, home in ((loaded[3], self.home / ".codex"), (loaded[4], self.home / ".codex"),
                            (loaded[5], self.home / ".claude")):
            unresolved = entry.get("label")
            self.assertIsInstance(unresolved, moves.HomeAccountUnresolved)
            assert isinstance(unresolved, moves.HomeAccountUnresolved)
            self.assertEqual(unresolved.home, home)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(configuration.read_bytes(), config_before)

    def test_codex_home_spelling_without_a_cutover_remains_unresolved_despite_todays_login(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        original = json.dumps({**self.requested(), "from": "~/.codex"}) + "\n"
        _ = path.write_text(original)
        notes = self.home / "notes"
        notes.mkdir()
        _ = (notes / "codex9.md").write_text("---\nlogin: current@example.invalid\n---\n")
        home = self.home / ".codex"
        home.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({"email": "current@example.invalid"}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}):
            loaded = moves.read_moves()
        unresolved = loaded[0].get("from")
        self.assertIsInstance(unresolved, moves.HomeAccountUnresolved)
        assert isinstance(unresolved, moves.HomeAccountUnresolved)
        self.assertEqual(unresolved.home, home)
        self.assertEqual(path.read_text(), original)

    def test_request_alone_never_implies_confirmation(self) -> None:
        requested = self.requested()
        moves.append_move(requested)
        self.assertEqual(moves.read_moves(), [requested])

    def test_automatic_requests_preserve_rule_reason_and_cause_alongside_older_causes(self) -> None:
        legacy = self.requested()
        _ = legacy.pop("cause")
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(legacy) + "\n")
        automatic = cast(moves.RequestedMove, cast(object, {
            **self.requested(), "move_id": "automatic-transition", "cause": "auto", "why": "auto: units-low"}))
        moves.append_move(automatic)
        self.assertEqual([(entry.get("cause"), entry.get("why")) for entry in moves.read_moves()],
                         [("refusal", "quota refusal"), ("auto", "auto: units-low")])
        self.assertEqual(json.loads(path.read_text().splitlines()[1])["cause"], "auto")


    def test_hand_and_refusal_causes_survive_storage_and_legacy_requests_read_as_refusals(self) -> None:
        legacy = self.requested()
        _ = legacy.pop("cause")
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(legacy) + "\n")
        for cause in ("by hand", "refusal", "credits off"):
            entry = {**legacy, "move_id": cause, "cause": cause}
            moves.append_move(cast(moves.RequestedMove, cast(object, entry)))
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual([entry.get("cause") for entry in requests],
                         ["refusal", "by hand", "refusal", "credits off"])

    def test_malformed_optional_request_ids_are_skipped_while_legacy_and_numbered_attempts_remain_readable(self) -> None:
        legacy = self.requested()
        attempt: moves.RequestedMove = {**legacy, "move_id": "group-transition", "request_id": "attempt-one"}
        invalid_ids: tuple[object, ...] = ([], "", None, 7)
        entries: list[object] = [legacy, *({**legacy, "request_id": value} for value in invalid_ids), attempt]
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
        _ = path.write_bytes(original)
        self.assertEqual(moves.read_moves(), [legacy, attempt])
        self.assertEqual(path.read_bytes(), original)

    def test_routing_variants_validate_without_account_move_identity(self) -> None:
        common: dict[str, object] = {"at": "2026-10-13T12:00:00Z", "tool": "codex",
            "fallback_at": "2026-10-13T11:00:00+00:00", "owner": "codex2"}
        variants: list[dict[str, object]] = [
            {**common, "kind": "routed to claude", "cause": "refusal", "why": "Codex refused work for quota", "moved": ["direct"]},
            {**common, "kind": "routed back", "restored": ["direct"], "kept": []},
            {**common, "kind": "routing not restored", "outcome": "partial",
             "restored": ["direct"], "kept": [], "failed": ["fix (unreadable)"]},
        ]
        for entry in variants:
            with self.subTest(kind=entry["kind"]):
                self.assertTrue(moves._valid(entry))  # pyright: ignore[reportPrivateUsage]
                for extra in ({"move_id": "not-a-move"}, {"subject": "workers"}):
                    self.assertFalse(moves._valid({**entry, **extra}))  # pyright: ignore[reportPrivateUsage]
                missing = dict(entry)
                _ = missing.pop("fallback_at")
                self.assertFalse(moves._valid(missing))  # pyright: ignore[reportPrivateUsage]
        missing_move_id = dict(self.requested())
        _ = missing_move_id.pop("move_id")
        self.assertFalse(moves._valid(missing_move_id))  # pyright: ignore[reportPrivateUsage]

    def test_mixed_journal_separates_routing_and_preserves_unresolved_historical_accounts(self) -> None:
        routing: moves.RoutedToClaude = {"kind": "routed to claude", "at": self.common()["at"],
            "tool": "codex", "fallback_at": "2026-10-13T11:00:00Z", "owner": "codex2",
            "cause": "refusal", "why": "Codex refused work for quota", "moved": ["direct"]}
        accounts: list[moves.MoveEntry] = [self.requested(),
            moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2"),
            moves.WaitedMove(**self.common(), kind="waited", window="five_hour", reset="2026-10-13T12:19:00Z"),
            moves.NoRoomMove(**self.common(), kind="no room", earliest_reset="2026-10-15T12:00:00Z")]
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw: list[object] = []
        for entry in accounts:
            raw.extend((entry, routing))
        raw.append({**self.requested(), "move_id": "old-home", "from": "default"})
        original = "".join(json.dumps(entry) + "\n" for entry in raw).encode()
        _ = path.write_bytes(original)
        loaded = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in loaded],
                         ["requested", "confirmed", "waited", "no room", "requested"])
        self.assertIsInstance(loaded[-1].get("from"), moves.HomeAccountUnresolved)
        self.assertEqual(len(moves.read_journal()), len(raw))
        self.assertEqual(path.read_bytes(), original)

    def test_routing_writes_once_per_kind_and_fallback_without_resolving_unknown_owner(self) -> None:
        entry: moves.RoutedToClaude = {"kind": "routed to claude", "at": self.common()["at"],
            "tool": "codex", "fallback_at": "2026-10-13T11:00:00Z", "owner": "unknown",
            "cause": "credits off", "why": "credits off", "moved": ["direct", "fix"]}
        moves.append_routing(entry)
        moves.append_routing(entry)
        recovery: moves.RoutedBack = {"kind": "routed back", "at": "2026-10-13T13:00:00Z",
            "tool": "codex", "fallback_at": entry["fallback_at"], "owner": "unknown",
            "restored": ["direct"], "kept": ["fix"]}
        moves.append_routing(recovery)
        moves.append_routing(recovery)
        self.assertEqual(len(moves.store_file().read_text().splitlines()), 2)
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual([entry["kind"] for entry in moves.read_journal()], ["routed to claude", "routed back"])
        with self.assertRaises(ValueError):
            moves.append_routing({**entry, "owner": "default"})

    def test_reader_skips_malformed_lines_and_keeps_later_valid_transitions(self) -> None:
        first = moves.WaitedMove(**self.common(), kind="waited", window="five_hour", reset="2026-10-13T12:19:00+00:00")
        last = moves.WaitEndedMove(**self.common(), kind="wait ended")
        moves.append_move(first)
        path = self.home / ".local/state/accounts/moves.jsonl"
        with path.open("a", encoding="utf-8") as output:
            for invalid in ("{broken", "null", "[]", "{}", '{"kind":"imaginary"}',
                            json.dumps({**first, "at": 7}), json.dumps({**first, "reset": []})):
                _ = output.write(invalid + "\n")
        moves.append_move(last)
        self.assertEqual(moves.read_moves(), [first, last])

    def test_unknown_request_reset_is_absent_and_empty_reset_is_malformed(self) -> None:
        requested = self.requested()
        self.assertNotIn("reset", requested)
        moves.append_move(requested)
        self.assertEqual(moves.read_moves(), [requested])
        with moves.store_file().open("a", encoding="utf-8") as output:
            _ = output.write(json.dumps({**requested, "reset": ""}) + "\n")
        self.assertEqual(moves.read_moves(), [requested])

    def test_malformed_confirmation_does_not_suppress_valid_confirmation(self) -> None:
        entry = moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", worker="worker-a")
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({**entry, "at": "invalid"}) + "\n")
        moves.append_move(entry)
        self.assertEqual(moves.read_moves(), [entry])
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_missing_journal_is_empty_and_reading_creates_nothing(self) -> None:
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_concurrent_writers_keep_complete_entries_for_all_workers(self) -> None:
        entries = [moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", worker=f"worker-{index}")
                   for index in range(40)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            _ = list(workers.map(moves.append_move, entries))
        observed = moves.read_moves()
        self.assertEqual(len(observed), len(entries))
        self.assertCountEqual(observed, entries)
        path = self.home / ".local/state/accounts/moves.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), len(entries))

    def test_legacy_move_names_keep_identity_and_later_appends_on_the_same_inode(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        requested = {**self.requested(), "subject": "seats"}
        waited = {**self.common(), "subject": "seats", "kind": "waited", "window": "weekly",
                  "reset": "2026-10-14T12:00:00+00:00"}
        confirmed = {**self.common(), "subject": "seats", "kind": "confirmed",
                     "home": "/temporary/codex2", "seat": "impl", "run": "run-42"}
        _ = path.write_text(json.dumps(requested) + "\ninvalid JSON\n" + json.dumps(waited) + "\n")
        inode = path.stat().st_ino
        with path.open("a", encoding="utf-8") as earlier_writer:
            loaded = moves.read_moves()
            self.assertEqual([entry["subject"] for entry in loaded], ["workers", "workers"])
            self.assertEqual([entry["move_id"] for entry in loaded], ["transition-1", "transition-1"])
            self.assertEqual(loaded[0]["at"], requested["at"])
            self.assertEqual(loaded[1].get("reset"), waited["reset"])
            fcntl.flock(earlier_writer.fileno(), fcntl.LOCK_EX)
            _ = earlier_writer.write(json.dumps(confirmed) + "\n")
            earlier_writer.flush()
            fcntl.flock(earlier_writer.fileno(), fcntl.LOCK_UN)
            loaded = moves.read_moves()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(loaded[-1].get("worker"), "impl")
        self.assertEqual(loaded[-1].get("run"), "run-42")
        self.assertNotIn("seat", loaded[-1])
        self.assertIn("invalid JSON\n", path.read_text())
        unchanged = path.stat().st_mtime_ns
        self.assertEqual(moves.read_moves(), loaded)
        self.assertEqual(path.stat().st_mtime_ns, unchanged)

    def test_a_partial_worker_name_conversion_recovers_all_history_on_rerun(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        original = (json.dumps({**self.requested(), "subject": "seats"}) + "\ninvalid JSON\n"
                    + json.dumps({"role": "seats", "label": "codex2"}) + "\n")
        migrate = worker_names.migrate_locked_journal
        for failure in (OSError("disk full"), KeyboardInterrupt("stopped during write")):
            with self.subTest(failure=type(failure).__name__):
                _ = path.write_text(original)
                inode = path.stat().st_ino

                def interrupted(source: TextIO, journal: Path) -> list[str]:
                    write = source.write

                    def partial_write(text: str) -> int:
                        _ = write(text[:70])
                        source.flush()
                        raise failure

                    with patch.object(source, "write", side_effect=partial_write):
                        return migrate(source, journal)

                with patch.object(worker_names, "migrate_locked_journal", side_effect=interrupted):
                    with self.assertRaises(type(failure)):
                        _ = worker_names.role_history_lines(path)
                self.assertNotEqual(path.read_text(), original)
                lines = worker_names.role_history_lines(path)
                self.assertEqual(len(lines), 3)
                self.assertEqual(json.loads(lines[0]), {**self.requested(), "subject": "workers"})
                self.assertEqual(lines[1], "invalid JSON\n")
                self.assertEqual(json.loads(lines[2]), {"role": "workers", "label": "codex2"})
                self.assertEqual(path.read_text(), "".join(lines))
                self.assertEqual(path.stat().st_ino, inode)
                self.assertFalse(path.with_name(f".{path.name}.workers").exists())
                self.assertFalse(path.with_name(f".{path.name}.tmp").exists())

    def test_confirmation_deduplicates_old_names_before_validation_and_keeps_other_workers(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        confirmed = {**self.common(), "subject": "seats", "kind": "confirmed",
                     "home": "/temporary/codex2", "seat": "impl", "run": "run-42"}
        _ = path.write_text(json.dumps(confirmed) + "\n")
        current = moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2",
                                      worker="impl", run="run-42")
        moves.append_move(current)
        moves.append_move({**current, "worker": "test"})
        moves.append_move({**current, "run": "run-43"})
        self.assertEqual(moves.read_moves(), [current, {**current, "worker": "test"}, {**current, "run": "run-43"}])

    def test_old_role_history_keeps_historical_login_evidence_and_reads_later_old_appends(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True)
        roles = path.with_name("roles.jsonl")
        evidence = {"at": "2026-10-12T00:00:00Z", "role": "seats", "label": "default", "account_label": "codex2"}
        _ = roles.write_text(json.dumps(evidence) + "\n")
        _ = path.write_text(json.dumps({**self.requested(), "from": "default"}) + "\n")
        inode = roles.stat().st_ino
        with roles.open("a", encoding="utf-8") as earlier_writer:
            self.assertEqual(moves.read_moves()[0].get("from"), "codex2")
            self.assertEqual(json.loads(roles.read_text())["role"], "workers")
            fcntl.flock(earlier_writer.fileno(), fcntl.LOCK_EX)
            _ = earlier_writer.write(json.dumps({**evidence, "at": "2026-10-13T00:00:00Z", "account_label": "codex3"}) + "\n")
            earlier_writer.flush()
            fcntl.flock(earlier_writer.fileno(), fcntl.LOCK_UN)
            self.assertEqual(moves.read_moves()[0].get("from"), "codex3")
        self.assertEqual(roles.stat().st_ino, inode)
        unchanged = roles.stat().st_mtime_ns
        self.assertEqual(moves.read_moves()[0].get("from"), "codex3")
        self.assertEqual(roles.stat().st_mtime_ns, unchanged)


class WorkerGroupPendingTests(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.stop_requests: list[str] = []
        self.failed_worker: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "runs"))
        _ = self.enterContext(patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former-runs"))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})))
        home = Path(os.environ["HOME"])
        home.mkdir(parents=True, exist_ok=True)
        _ = self.enterContext(scratch_account_home(home, {}, clear=False))
        Path(os.environ["NOTIFIER_SESSIONS_DIR"]).mkdir(parents=True, exist_ok=True)
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
        create_claude_account("claude1", "claude@example.invalid")
        notes = note_file.notes_dir()
        _ = (notes / note_file.note_name("codex1")).write_text("---\nlogin: codex@example.invalid\n---\n")
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        codex = binary / "codex"
        _ = codex.write_text("#!" + sys.executable + "\n" +
            "import json, sys\nfor line in sys.stdin:\n"
            + "    request = json.loads(line)\n"
            + "    if 'id' in request:\n"
            + "        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n")
        codex.chmod(0o700)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")})))
        self.assertEqual(accounts.setup("codex1"), [])
        write_codex_auth(home / ".codex1", "codex@example.invalid")
        report = agent_accounts.Report("Codex", email="codex@example.invalid",
            quotas=[agent_accounts.Quota("Weekly", 80, datetime.now(timezone.utc) + timedelta(days=1))])
        _ = self.enterContext(patch.object(accounts, "_codex_live", return_value=report))
        self.stop_requests.clear()
        self.failed_worker = ""
        patches = [
            cast(object, self.enterContext(patch.object(pane_moves, "_move_locked", side_effect=self.stop_worker))),
            cast(object, self.enterContext(patch.object(worker_panes, "worker_run", return_value=self.root / "pane-run"))),
        ]
        self.addCleanup(patches.clear)
        _ = self.enterContext(patch.object(accounts, "SCRIPTS", self.root / "no-launchers"))

    def stop_worker(self, move: pane_moves.WorkerMove,
                    hold: worker_panes.WorkerLockHold) -> accounts.SessionMoveOutcome:
        del hold
        self.stop_requests.append(move.row.id)
        if move.row.id == self.failed_worker:
            return accounts.SessionMoveRefused(move.row.id, "source process could not stop")
        return stop_results.ResumePending(move.row.id, move.target, "destination is not ready")

    def worker(self, name: str, family: Literal["claude", "codex"]) -> tuple[staff.StaffRow, Path]:
        owner = fixture_member("showrunner", name="history-owner")
        unit = fixture_member("unit", name="history-unit", parent=owner.id,
                              worktree=staff.PresentWorktree("natedev", self.root))
        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(Path(os.environ["HOME"]) / ".codex")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude")}):
            row = fixture_worker(name, unit.id, backend=backend)
        run = self.root / "old-runs" / name
        run.mkdir(parents=True)
        write_text_atomic(run / "progress_history_state.json", json.dumps({"owner_staff_id": unit.id}))
        _ = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("replacement director")))
        return row, run


    def test_account_moves_stop_fixed_workers_and_keep_pending_rows_in_both_families(self) -> None:
        for family in ("claude", "codex"):
            for target in ("codex1", "claude1"):
                with self.subTest(family=family, target=target):
                    self.stop_requests.clear()
                    row, run = self.worker("worker-" + family + target, family)
                    before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                    result = accounts.move_workers(target, dry_run=True)
                    assert isinstance(result, accounts.GroupMoveDryRun)
                    same_family = target.startswith(family)
                    preview = (f"would stop and resume the same conversation on {target} in its worker pane" if same_family
                               else f"not moved: cannot restart on {target} through assign.sh")
                    self.assertEqual(result.lines, (f"{row.name} ({row.id}): {preview}",))
                    self.assertIn(str(row.id), "\n".join(result.lines))
                    self.assertIn(target, "\n".join(result.lines))
                    self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(self.stop_requests, [])
                    inbox = session_staff.inbox_path(row.id)
                    inbox.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(inbox, '{"messages": ["held assignment"], "accepting_messages": true}')
                    reference = (run / "progress_history_state.json").read_bytes(), inbox.read_bytes()
                    saved = self.staff_file.read_bytes()
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        code = accounts.main(["move", "workers", target])
                    self.assertEqual(code, 1, output.getvalue() + errors.getvalue())
                    self.assertEqual(self.stop_requests, [row.id] if same_family else [])
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(self.staff_file.read_bytes(), saved)
                    self.assertEqual(((run / "progress_history_state.json").read_bytes(), inbox.read_bytes()), reference)
                    self.assertEqual(session_staff.worker_directory(staff.local_snapshot(), row.id), self.root)
                    lines = (output.getvalue() + errors.getvalue()).splitlines()
                    self.assertEqual(len(lines), 1, lines)
                    self.assertIn(str(row.id), lines[0])
                    self.assertIn(target, lines[0])
                    if same_family:
                        self.assertIn("pending", lines[0])
                    else:
                        self.assertIn(f"cannot restart on {target} through assign.sh", lines[0])
                    self.assertEqual(staff.retire(row.id), row)

    def test_one_failed_stop_reports_the_worker_and_keeps_other_moves_pending(self) -> None:
        for failure in ("stop", "locked"):
            with self.subTest(failure=failure):
                self.stop_requests.clear()
                failed, failed_run = self.worker("first-fails-" + failure, "claude")
                healthy, healthy_run = self.worker("second-works-" + failure, "claude")
                saved = {path: path.read_bytes() for run in (failed_run, healthy_run) for path in run.iterdir()}
                before = self.staff_file.read_bytes()
                self.failed_worker = failed.id
                lock_path = self.staff_file.parent / "locks" / f"{failed.id}.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a") as lock:
                    if failure == "locked":
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        code = accounts.main(["move", "workers", "claude1"])
                self.assertEqual(code, 1)
                self.assertEqual(set(self.stop_requests),
                                 {failed.id, healthy.id} if failure == "stop" else {healthy.id})
                self.assertEqual(len(self.stop_requests), 2 if failure == "stop" else 1)
                self.assertEqual(staff.by_id(failed.id), failed)
                self.assertEqual(staff.by_id(healthy.id), healthy)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual({path: path.read_bytes() for run in (failed_run, healthy_run) for path in run.iterdir()}, saved)
                lines = (output.getvalue() + errors.getvalue()).splitlines()
                self.assertEqual(len(lines), 2, lines)
                failed_line = next(line for line in lines if str(failed.id) in line)
                healthy_line = next(line for line in lines if str(healthy.id) in line)
                self.assertIn("source process could not stop" if failure == "stop"
                              else "dispatch lock is held", failed_line)
                self.assertIn("pending", healthy_line)
                self.assertIn("claude1", healthy_line)
                _ = staff.retire(failed.id)
                _ = staff.retire(healthy.id)


if __name__ == "__main__":
    _ = unittest.main()
