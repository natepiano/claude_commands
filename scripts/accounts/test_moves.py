"""Move requests, outcomes and waits remain distinct in an append-only journal."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        return {**self.common(), "kind": "requested", "from": "codex 1", "to": "codex 2",
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
            moves.RoomAgainMove(**self.common(), kind="room again", label="codex 2"),
        ]
        for entry in entries:
            moves.append_move(entry)
        self.assertEqual(moves.read_moves(), entries)
        path = self.home / ".local/state/accounts/moves.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), len(entries))
        self.assertEqual({entry["move_id"] for entry in moves.read_moves()}, {"transition-1"})

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
