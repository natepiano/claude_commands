"""test implement launcher compaction integration coverage."""

from __future__ import annotations

import unittest
import socket
from concurrent.futures import ThreadPoolExecutor
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct import implement_launcher_test_support as support
from scripts.lint.memory_test_support import commit_config_fixture
from typing import cast
import json


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_compaction_does_not_block_another_worker_in_the_unit(self) -> None:
        session = self.start_phase("parallel-compaction")
        self.assertEqual(self.mesh_launch(session).returncode, 0)
        self.assertEqual(self.mesh_launch(session, slot="test").returncode, 0)
        roster = cast(dict[str, dict[str, object]], json.loads(
            (session / "mesh_roster.json").read_text()))
        first, second = roster
        self.set_mesh_context(session, first, 100001)
        mesh = self.root / "scripts/agents/codex_mesh.py"
        _ = mesh.write_text(mesh.read_text().replace(
            "elif verb == 'compact':",
            "elif verb == 'compact':\n"
            + "    import socket\n"
            + "    with socket.socket(socket.AF_UNIX) as gate:\n"
            + "        gate.connect(str(session / 'compact.sock'))\n"
            + "        gate.sendall(b'r')\n"
            + "        gate.recv(1)\n"
            + "    roster = json.loads(roster_path.read_text())"))
        with socket.socket(socket.AF_UNIX) as gate, ThreadPoolExecutor() as dispatches:
            gate.bind(str(session / "compact.sock"))
            gate.listen()
            gate.settimeout(TEST_HANG_GUARD_SECS)
            pending = dispatches.submit(self.mesh_launch, session, to=first)
            connection = gate.accept()[0]
            with connection:
                connection.settimeout(TEST_HANG_GUARD_SECS)
                try:
                    self.assertEqual(connection.recv(1), b"r")
                    claim = cast(dict[str, object], json.loads(
                        (session / "impl_claim_impl.json").read_text()))
                    self.assertEqual(claim["worker"], first)
                    refused = self.mesh_launch(session, to=first)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertIn("is claimed by launcher", refused.stderr)
                    result = self.mesh_launch(session, to=second, slot="test")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(pending.done())
                finally:
                    connection.sendall(b"x")
            result = pending.result(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session / "impl_claim_impl.json").exists())
        self.assertFalse((session / "impl_claim_test.json").exists())

    def test_worker_compaction_uses_the_private_config_instead_of_the_home_config(self) -> None:
        _ = self.config_file.write_text(
            self.config_file.read_text(encoding="utf-8").replace(
                "UNIT_COMPACT_ABOVE_TOKENS=100000", "UNIT_COMPACT_ABOVE_TOKENS=20000"
            ), encoding="utf-8",
        )
        home_config = self.root / ".claude/config/direct.conf"
        _ = home_config.write_text("UNIT_COMPACT_ABOVE_TOKENS=999999\n", encoding="utf-8")
        session_dir = self.start_phase("private-compaction-config")

        first = self.mesh_launch(session_dir)

        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast(dict[str, object], json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 20001)

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact", "can-follow", "follow"],
        )

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
        self.assertFalse((session_dir / "impl_claim_impl.json").exists())
        self.assertFalse((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)

    def test_compaction_threshold_must_be_configured_as_a_positive_integer(self) -> None:
        configured = self.config_file.read_text(encoding="utf-8")
        key = "UNIT_COMPACT_ABOVE_TOKENS"
        commit_config_fixture(self.config_file)
        for value in ("missing", "0", "zero", "1.5"):
            with self.subTest(value=value):
                _ = self.config_file.write_text(configured, encoding="utf-8")
                session_dir = self.start_phase(f"threshold-recovery-{value}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster = cast(dict[str, object], json.loads(
                    (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
                ))
                name = next(iter(roster))
                self.set_mesh_context(session_dir, name, 100001)
                replacement = "" if value == "missing" else f"{key}={value}\n"
                _ = self.config_file.write_text(
                    configured.replace(f"{key}=100000\n", replacement), encoding="utf-8"
                )

                result = self.mesh_launch(session_dir, to=name)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("warning:", result.stderr)
                self.assertIn(str(self.config_file), result.stderr)
                self.assertIn(key, result.stderr)
                self.assertEqual(
                    (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                    ["compact", "can-follow", "follow"],
                )

    def test_compaction_threshold_refuses_without_usable_history(self) -> None:
        configured = self.config_file.read_text(encoding="utf-8")
        key = "UNIT_COMPACT_ABOVE_TOKENS"
        without_key = "\n".join(
            line for line in configured.splitlines() if not line.startswith(f"{key}=")
        ) + "\n"
        for value in ("missing", "0", "zero", "1.5"):
            with self.subTest(value=value):
                damaged = without_key if value == "missing" else configured.replace(
                    f"{key}=100000", f"{key}={value}"
                )
                _ = self.config_file.write_text(damaged, encoding="utf-8")
                session_dir = self.start_phase(f"threshold-no-history-{value}")

                result = self.mesh_launch(session_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.config_file), result.stderr)
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

    def test_follow_up_compacts_above_the_context_threshold_before_following(self) -> None:
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
