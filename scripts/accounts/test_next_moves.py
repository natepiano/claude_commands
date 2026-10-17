"""Ordered next moves use an isolated log and the role-map host transport."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import next_moves, role_map

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


class NextMovesTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected ssh")))

    def test_add_and_remove_keep_entry_order_and_preserve_the_other_groups_plan(self) -> None:
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertFalse(next_moves.next_moves_file().exists())
        first = next_moves.add_next_move("units", 1, "claude4", 5)
        second = next_moves.add_next_move("units", 1, "claude2", 5)
        runner = next_moves.add_next_move("showrunners", 2, "claude2")
        self.assertEqual(next_moves.read_next_moves(), (first, second, runner))
        self.assertNotEqual(first.id, second.id)
        next_moves.remove_next_move(second.id)
        self.assertEqual(next_moves.read_next_moves(), (first, runner))
        before = next_moves.next_moves_file().read_bytes()
        with self.assertRaises(next_moves.NextMoveError):
            next_moves.remove_next_move("absent")
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        next_moves.remove_next_move(first.id)
        next_moves.remove_next_move(runner.id)
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_remote_read_uses_host_log_without_creating_a_local_copy(self) -> None:
        document = json.dumps([{"id": "remote-entry", "group": "units", "at_percent": 1,
            "hold_minutes": 5, "to": "claude2", "added_at": NOW.isoformat()}])
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", return_value=document) as remote):
            entries = next_moves.read_next_moves()
        self.assertEqual(entries, (next_moves.NextMove("remote-entry", "units", 1, 5, "claude2", NOW),))
        remote.assert_called_once_with(("next-read",))
        self.assertFalse(next_moves.next_moves_file().exists())

    def test_remote_failure_refuses_instead_of_reading_a_different_local_plan(self) -> None:
        local = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=role_map.RoleMapUnavailable("host unavailable"))):
            with self.assertRaises((next_moves.NextMoveError, role_map.RoleMapUnavailable)):
                _ = next_moves.read_next_moves()
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(next_moves.read_next_moves(), (local,))

    def test_invalid_log_is_refused_without_rewriting_it(self) -> None:
        entry = {"id": "entry", "group": "units", "at_percent": 1, "hold_minutes": 10,
                 "to": "claude2", "added_at": NOW.isoformat()}
        cases: tuple[object, ...] = (
            {}, [{**entry, "group": "workers"}], [{**entry, "at_percent": True}],
            [{**entry, "at_percent": 101}], [{**entry, "hold_minutes": -1}],
            [{**entry, "hold_minutes": 10 ** 400}], [{**entry, "added_at": "2026-10-17T00:00:00"}],
            [entry, entry],
        )
        path = next_moves.next_moves_file()
        path.parent.mkdir(parents=True)
        for value in cases:
            with self.subTest(value=value):
                original = json.dumps(value)
                _ = path.write_text(original)
                with self.assertRaises(next_moves.NextMoveError):
                    _ = next_moves.read_next_moves()
                self.assertEqual(path.read_text(), original)
