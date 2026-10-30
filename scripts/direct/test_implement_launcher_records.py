"""test implement launcher records integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct import implement_launcher_test_support as support
from scripts.direct.review_launcher_test_support import without_retired_pool
from scripts.direct.implement_launcher_test_support import DIRECT_DIR as DIRECT_DIR
from typing import cast, override
import json
import subprocess
import sys


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    @override
    def environment(self) -> dict[str, str]:
        # Successful and failed turns exercise finish-pass and landed/abandon
        # after the worker exits, with no retired module to import.
        return without_retired_pool(self.root, super().environment())

    def test_a_companion_fix_starts_after_the_resolver_lands_its_round(self) -> None:
        session = self.start_phase("landed-companion")
        self.open_repair_round(session)
        landed = subprocess.run(
            [sys.executable, str(DIRECT_DIR / "findings.py"), "landed",
             "--session-dir", str(session)],
            env=self.environment(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(landed.returncode, 0, landed.stderr)
        result = self.checked_launch(session, "--phase", "3", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / "worker-started").exists())
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], {"round": 1, "covered": ["F001"]})

    def test_every_worker_records_its_own_pass_and_stamps_its_role(self) -> None:
        session_dir = self.start_phase("team")
        for kind, slot in (("impl", "impl"), ("impl", "test")):
            result = self.launch(session_dir, kind, slot)
            self.assertEqual(result.returncode, 0, result.stderr)

        # Two records, keyed by worker rather than by what each is doing: the
        # `test` worker opened as a second writer and is still slot `test`.
        passes = self.open_passes(session_dir)
        self.assertEqual(
            {slot: record["kind"] for slot, record in passes.items()},
            {"impl": "impl", "test": "impl"},
        )
        self.assertEqual(
            {record["status"] for record in passes.values()}, {"completed"}
        )

        # The register line carries the role, which is what fills the progress
        # table's columns before any agent has posted, and what made the
        # incident legible from the board alone once it had gone wrong.
        register = [
            line
            for line in (session_dir / "board.log").read_text(encoding="utf-8").splitlines()
            if "register:" in line
        ]
        self.assertEqual(len(register), 2, register)
        for line, expected in zip(register, ("role=impl", "role=impl")):
            self.assertIn(expected, line)

    def test_failed_follow_up_abandons_round_and_posts_blocked(self) -> None:
        session_dir = self.start_phase("abandoned")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.open_repair_round(session_dir)

        result = self.mesh_launch(
            session_dir, to=name, kind="fix", fail=True, resolves_round=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.repair_outcome(session_dir), "abandoned", result.stderr)
        state = cast("dict[str, object]", json.loads(
            (session_dir / "findings_state.json").read_text(encoding="utf-8")
        ))
        rounds = cast("list[dict[str, object]]", state["rounds"])
        self.assertIs(rounds[-1]["edits_landed"], True)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    def test_failed_landed_record_leaves_error_status(self) -> None:
        session_dir = self.start_phase("status-landed-fails")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records(fail_landed=True)

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n"
        )
        self.assertIn("blocked:", (session_dir / "board.log").read_text(encoding="utf-8"))

    def test_failed_pass_write_posts_blocked_and_does_not_send(self) -> None:
        session_dir = self.start_phase("ledger-error")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        _ = (session_dir / "progress_history_state.json").write_text(
            "{}\n", encoding="utf-8"
        )

        result = self.mesh_launch(session_dir, to=name)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
            "error\n",
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    def test_follow_up_records_pass_and_lands_its_repair_round(self) -> None:
        session_dir = self.start_phase("landed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.open_repair_round(session_dir)

        result = self.mesh_launch(
            session_dir, to=name, kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repair_outcome(session_dir), "landed")
        self.assertEqual(self.open_passes(session_dir)["impl"]["kind"], "fix")
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")
        self.assertEqual(self.open_passes(session_dir)["impl"]["worker_name"], name)
        # A renamed unit's seat works in the worktree the launcher names, not the one its thread began in.
        self.assertEqual((session_dir / "follow_cwd.txt").read_text(encoding="utf-8"), str(self.working_dir))
        message = (session_dir / "follow_message.txt").read_text(encoding="utf-8")
        self.assertIn("Write the retry path.", message)
        self.assertIn("impl_summary_impl.txt", message)
        self.assertIn("post", message)
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"follow-up to {name}", board)
        self.assertIn("launcher:", board)
        self.assertIn("done:", board)

    def test_launcher_passes_the_explicit_failed_gate_cause_for_a_fix(self) -> None:
        session = self.start_phase("checked-fix-cause")
        result = self.checked_launch(session, "--phase", "3", "--fix-cause",
                                     "retry tests fail after integration", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], "retry tests fail after integration")

    def test_success_status_follows_pass_and_landed_records(self) -> None:
        session_dir = self.start_phase("status-after-landed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records()

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_worker_error_status_follows_pass_and_abandon_records(self) -> None:
        session_dir = self.start_phase("status-after-abandon")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records()

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", fail=True,
            resolves_round=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "abandon"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n"
        )


if __name__ == "__main__":
    _ = unittest.main()
