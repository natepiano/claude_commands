"""Reservation availability is read from the board envelope, never inferred from failure."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock

from scripts.lib.berth_board import Board, BoardUnavailable, read_board


def snapshot(identity: str, status: str = "blocking") -> dict[str, object]:
    # The wire shapes are from cargo-berth's board/rows.rs and ledger/journal.rs.
    return {
        "reservation_id": identity,
        "target": {"ref": "refs/heads/main", "short_name": "main", "source": "repository_trunk", "commit": "abc"},
        "holder": {"worktree_id": "worker", "worktree_root": "/tmp/worker", "branch": {
            "kind": "attached", "reference": "refs/heads/product-worker",
        }, "liveness": "live"},
        "scopes": [{"kind": "file", "path": "Cargo.toml"}, {"kind": "tree", "path": "src/module"}],
        "lifecycle": {"stage": "active"}, "edit_blocking_status": status,
    }


def envelope(*, populated: bool = True) -> dict[str, object]:
    position = {"generation": 1, "journal_byte_offset": 123}
    blocking = snapshot("product-1")
    clear = snapshot("released-1", "clear")
    data: dict[str, object] = {
        "targets": [{"ref": "refs/heads/main", "commit": "abc", "reservations": ["product-1"]}] if populated else [],
        "ready_now": {"journal_position": position, "entries": [{"relation": "unordered", "reservation": blocking}] if populated else []},
        "waiting": {"journal_position": position, "entries": [{"hold": "ordering_edge", "reservation": blocking}] if populated else []},
        "unresolved_overlaps": {"journal_position": position, "entries": []},
        "unconstrained_reservations": {"journal_position": position, "entries": []},
        "resolved": {"journal_position": position, "entries": [clear] if populated else []},
    }
    return {
        "output_contract_version": 1, "verb": "board", "status": "ok", "exit_code": 0,
        "reservations": ["product-1"] if populated else [], "blocked_by": [], "message": "",
        "presentation": {}, "payload": {"kind": "board", "data": data},
    }


@final
class BerthBoardTests(unittest.TestCase):
    def read(self, value: object, *, exit_code: int = 0) -> Board | BoardUnavailable:
        completed = subprocess.CompletedProcess(["cargo-berth"], exit_code, json.dumps(value), "")
        with mock.patch("scripts.lib.berth_board.subprocess.run", return_value=completed) as run:
            result = read_board(Path("/tmp/repo"))
        run.assert_called_once_with(
            ["cargo-berth", "board", "--json"], cwd=Path("/tmp/repo"), capture_output=True, text=True, check=False,
        )
        return result

    def test_envelope_collects_deduplicates_and_respects_scope_boundaries_and_clear_status(self) -> None:
        result = self.read(envelope())
        self.assertIsInstance(result, Board)
        if not isinstance(result, Board):
            self.fail(str(result))
        reservations = {entry.reservation_id: entry for entry in result.reservations()}
        self.assertEqual(set(reservations), {"product-1", "released-1"})
        blocking = reservations["product-1"]
        self.assertEqual((blocking.target, blocking.holder_worktree_root, blocking.holder_branch, blocking.lifecycle),
                         ("refs/heads/main", Path("/tmp/worker"), "refs/heads/product-worker", "active"))
        for path in ("Cargo.toml", "src/module", "src/module/lib.rs"):
            self.assertTrue(blocking.blocks(path), path)
            self.assertFalse(reservations["released-1"].blocks(path), path)
        for path in ("Cargo.toml/child", "src/modules/lib.rs", "src/other.rs"):
            self.assertFalse(blocking.blocks(path), path)

    def test_empty_envelope_is_available_even_when_process_reports_another_status(self) -> None:
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                result = self.read(envelope(populated=False), exit_code=exit_code)
                self.assertIsInstance(result, Board)
                if isinstance(result, Board):
                    self.assertEqual(result.reservations(), ())

    def test_unconfigured_exit_four_is_an_available_empty_board(self) -> None:
        value = envelope(populated=False)
        value.update(status="unconfigured", exit_code=4, payload={"kind": "no_facts"},
                     message="this repository has no cargo-berth configuration")
        result = self.read(value, exit_code=4)
        self.assertIsInstance(result, Board)
        if isinstance(result, Board):
            self.assertEqual(result.reservations(), ())

    def test_unreadable_ledger_exit_four_is_unavailable_with_its_diagnostic(self) -> None:
        value = envelope(populated=False)
        value.update(status="ledger_unreadable", exit_code=4, payload={"kind": "no_facts"},
                     message="cannot read reservation journal")
        result = self.read(value, exit_code=4)
        self.assertIsInstance(result, BoardUnavailable)
        if isinstance(result, BoardUnavailable):
            self.assertIn("cannot read reservation journal", result.reason)

    def test_empty_malformed_and_non_envelope_output_are_unavailable(self) -> None:
        for stdout, code in (("", 0), ("", 2), ("", 4), ("not json", 1), ("{}", 0), ("[]", 0), ('{"reservations": []}', 0)):
            with self.subTest(stdout=stdout, code=code):
                completed = subprocess.CompletedProcess(["cargo-berth"], code, stdout, "")
                with mock.patch("scripts.lib.berth_board.subprocess.run", return_value=completed):
                    result = read_board(Path("/tmp/repo"))
                self.assertIsInstance(result, BoardUnavailable)

    def test_missing_command_is_unavailable(self) -> None:
        with mock.patch("scripts.lib.berth_board.subprocess.run", side_effect=FileNotFoundError("cargo-berth missing")):
            result = read_board(Path("/tmp/repo"))
        self.assertIsInstance(result, BoardUnavailable)
        if isinstance(result, BoardUnavailable):
            self.assertIn("cargo-berth missing", result.reason)

    def test_missing_or_unresolved_board_sections_do_not_look_empty(self) -> None:
        value = envelope(populated=False)
        payload = cast(dict[str, object], value["payload"])
        data = cast(dict[str, object], payload["data"])
        data["targets"] = [{"reservations": ["missing-snapshot"]}]
        self.assertIsInstance(self.read(value), BoardUnavailable)
        del data["waiting"]
        self.assertIsInstance(self.read(value), BoardUnavailable)

    def test_snapshots_in_all_sections_and_overlap_references_are_collected(self) -> None:
        value = envelope()
        payload = cast(dict[str, object], value["payload"])
        data = cast(dict[str, object], payload["data"])
        data["targets"] = [{"reservations": [snapshot("target-1")]}]
        section = cast(dict[str, object], data["unconstrained_reservations"])
        section["entries"] = [snapshot("free-1")]
        overlaps = cast(dict[str, object], data["unresolved_overlaps"])
        overlaps["entries"] = [{"deferred": "product-1", "blocker": "free-1"}]
        result = self.read(value)
        self.assertIsInstance(result, Board)
        if isinstance(result, Board):
            self.assertEqual({entry.reservation_id for entry in result.reservations()},
                             {"target-1", "product-1", "free-1", "released-1"})

    def test_unknown_status_and_incomplete_snapshot_are_unavailable(self) -> None:
        changes: tuple[dict[str, object], ...] = ({"edit_blocking_status": "unknown"}, {"scopes": []}, {"holder": {}})
        for changed in changes:
            with self.subTest(changed=changed):
                value = envelope()
                payload = cast(dict[str, object], value["payload"])
                data = cast(dict[str, object], payload["data"])
                section = cast(dict[str, object], data["ready_now"])
                section["entries"] = [{**snapshot("product-1"), **changed}]
                self.assertIsInstance(self.read(value), BoardUnavailable)
