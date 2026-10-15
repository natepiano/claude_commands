"""Move requests, outcomes and waits remain distinct in an append-only journal."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import moves


class MoveJournalTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)))

    def common(self, *, tool: str = "codex", subject: str = "seats") -> moves.MoveCommon:
        self.assertIn(tool, ("claude", "codex"))
        return moves.MoveCommon(at="2026-10-13T12:00:00+00:00",
            tool="claude" if tool == "claude" else "codex", subject=subject, move_id="transition-1")

    def requested(self) -> moves.RequestedMove:
        return {**self.common(), "kind": "requested", "from": "codex1", "to": "codex2",
            "why": "quota refusal", "window": "unknown"}

    def test_every_transition_kind_round_trips_without_losing_its_semantics(self) -> None:
        entries: list[moves.MoveEntry] = [
            self.requested(),
            moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", seat="seat-a"),
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
            {"at": "2026-10-12T00:00:00Z", "role": "seats", "label": "default", "account_label": "codex2"},
            {"at": "2026-10-14T00:00:00Z", "role": "unit", "label": "default", "account_label": "claude3"},
            {"at": "2026-10-14T00:00:00Z", "role": "seats", "label": "default", "account_label": "codex3"},
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

    def test_superseded_home_role_evidence_leaves_later_legacy_moves_unresolved(self) -> None:
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        roles = path.with_name("roles.jsonl")
        assignments = [
            {"at": "2026-10-11T00:00:00Z", "role": "seats", "label": "default", "account_label": "codex2"},
            {"at": "2026-10-12T00:00:00Z", "role": "seats", "label": "codex3"},
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

    def test_request_alone_never_implies_confirmation(self) -> None:
        requested = self.requested()
        moves.append_move(requested)
        self.assertEqual(moves.read_moves(), [requested])

    def test_credits_off_cause_round_trips_and_legacy_requests_remain_refusals(self) -> None:
        legacy = self.requested()
        policy: moves.RequestedMove = {**legacy, "cause": "credits off", "move_id": "policy-1"}
        moves.append_move(legacy)
        moves.append_move(policy)
        entries = moves.read_moves()
        self.assertEqual(entries, [legacy, policy])
        self.assertEqual([entry.get("cause", "refusal") for entry in entries if entry["kind"] == "requested"],
                         ["refusal", "credits off"])

    def test_invalid_move_cause_is_refused_by_writer_and_skipped_by_reader(self) -> None:
        valid = self.requested()
        moves.append_move(valid)
        for cause in ("refusal", "credits on", "", None, 7, ["credits off"]):
            with self.subTest(cause=cause):
                malformed = {**valid, "cause": cause}
                with self.assertRaisesRegex(ValueError, "invalid account move"):
                    moves.append_move(cast(moves.RequestedMove, cast(object, malformed)))
                with moves.store_file().open("a", encoding="utf-8") as output:
                    _ = output.write(json.dumps(malformed) + "\n")
        self.assertEqual(moves.read_moves(), [valid])

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
        entry = moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", seat="seat-a")
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({**entry, "at": "invalid"}) + "\n")
        moves.append_move(entry)
        self.assertEqual(moves.read_moves(), [entry])
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_missing_journal_is_empty_and_reading_creates_nothing(self) -> None:
        self.assertEqual(moves.read_moves(), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_concurrent_writers_keep_complete_entries_for_all_seats(self) -> None:
        entries = [moves.ConfirmedMove(**self.common(), kind="confirmed", home="/temporary/codex2", seat=f"seat-{index}")
                   for index in range(40)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            _ = list(workers.map(moves.append_move, entries))
        observed = moves.read_moves()
        self.assertEqual(len(observed), len(entries))
        self.assertCountEqual(observed, entries)
        path = self.home / ".local/state/accounts/moves.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), len(entries))


if __name__ == "__main__":
    _ = unittest.main()
