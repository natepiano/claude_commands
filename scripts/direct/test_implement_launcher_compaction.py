"""test implement launcher compaction integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct import implement_launcher_test_support as support
from typing import cast
import json


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_a_worker_left_with_a_live_turn_by_compaction_gets_no_follow_up(self) -> None:
        session_dir = self.start_phase("compact-still-live")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name, compact_still_live=True)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the worker", result.stderr)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)

    def test_compaction_threshold_must_be_configured_as_a_positive_integer(self) -> None:
        configured = self.config_file.read_text(encoding="utf-8")
        key = "UNIT_COMPACT_ABOVE_TOKENS"
        without_key = "\n".join(
            line for line in configured.splitlines() if not line.startswith(f"{key}=")
        ) + "\n"
        for label, config in (("missing", without_key), ("zero", without_key + f"{key}=0\n")):
            with self.subTest(value=label):
                _ = self.config_file.write_text(config, encoding="utf-8")
                session_dir = self.start_phase(f"threshold-{label}")

                result = self.mesh_launch(session_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)

    def test_failed_compaction_is_reported_and_the_follow_up_still_runs(self) -> None:
        session_dir = self.start_phase("compact-fails")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name, compact_fail=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("compaction failed", board)
        self.assertIn("continuing follow-up", board)

    def test_follow_up_compacts_above_the_context_threshold_before_claiming(self) -> None:
        session_dir = self.start_phase("compact-above")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(events, ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(
            f"launcher: compacted {name}: 100001 -> 12000 tokens",
            board,
        )

    def test_follow_up_does_not_compact_at_or_below_the_context_threshold(self) -> None:
        for tokens in (99999, 100000):
            with self.subTest(tokens=tokens):
                session_dir = self.start_phase(f"compact-below-{tokens}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster = cast("dict[str, object]", json.loads(
                    (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
                ))
                name = next(iter(roster))
                self.set_mesh_context(session_dir, name, tokens)

                result = self.mesh_launch(session_dir, to=name)

                self.assertEqual(result.returncode, 0, result.stderr)
                events = (session_dir / "mesh_events.txt").read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertEqual(events, ["can-follow", "follow"])

    def test_follow_up_posts_the_unmeasured_compaction_line_unchanged(self) -> None:
        session_dir = self.start_phase("compact-unmeasured-line")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast(dict[str, object], json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)
        (session_dir / "compact_reports_zero").touch()

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher: compacted {name}: 100001 tokens, size not measured until its next turn", board)

    def test_follow_up_to_an_unmeasured_worker_does_not_compact_again(self) -> None:
        session_dir = self.start_phase("unmeasured-follow")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "mesh_roster.json"
        roster = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        name = next(iter(roster))
        roster[name]["context_tokens"] = "not_measured_since_compaction"
        _ = path.write_text(json.dumps(roster), encoding="utf-8")

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["can-follow", "follow"])
        stored = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(stored[name]["context_tokens"], "not_measured_since_compaction")


if __name__ == "__main__":
    _ = unittest.main()
