"""Test assignment compaction through assign."""
from __future__ import annotations


from scripts.agents import test_codex_mesh as mesh_fixture
from scripts.direct.assign_launcher_test_support import AssignLauncherFixture


setUpModule = mesh_fixture.setUpModule


class AssignCompactionTests(AssignLauncherFixture):
    def large_worker(self) -> str:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record["context_tokens"] = 200_000
        self.write_pool_roster({name: record})
        return name

    def compact_result(self, code: int, output: str, error: str) -> None:
        self.instrument_launcher(
            "import subprocess\nfrom scripts.direct import assign\n"
            + "real_call = assign.Dispatch.mesh_call\n"
            + "def mesh_call(dispatch, verb, *options, **kwargs):\n"
            + "    if verb == 'compact':\n"
            + f"        return subprocess.CompletedProcess(['compact'], {code}, {output!r}, {error!r})\n"
            + "    return real_call(dispatch, verb, *options, **kwargs)\n"
            + "assign.Dispatch.mesh_call = mesh_call\n")

    def test_live_turn_after_compaction_receives_no_assignment(self) -> None:
        name = self.large_worker()
        before = self.turn_starts
        self.compact_result(3, "", "the turn is still live")
        result = self.launch("implement", "--to", name)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("still has a live turn after compaction", result.stderr)
        self.assertEqual(self.turn_starts, before)
        self.assertFalse(list(self.session_dir.glob("impl_claim_*.json")))
        self.assertIn("follow-up not sent", (self.session_dir / "board.log").read_text())

    def test_failed_compaction_reports_the_failure_and_continues(self) -> None:
        name = self.large_worker()
        before = self.turn_starts
        self.compact_result(1, "", "stub compaction failed")
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.turn_starts, before + 1)
        board = (self.session_dir / "board.log").read_text()
        self.assertIn("stub compaction failed", board)
        self.assertIn("continuing follow-up", board)

    def test_unmeasured_compaction_message_remains_unchanged(self) -> None:
        name = self.large_worker()
        message = f"compacted {name}: 200000 tokens, size not measured until its next turn"
        self.compact_result(0, message, "")
        _ = self.successful("implement", "--to", name)
        self.assertIn(message, (self.session_dir / "board.log").read_text())
        self.assertEqual(self.turn_starts, 2)

    def test_context_at_the_threshold_receives_no_compaction(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record["context_tokens"] = 100_000
        self.write_pool_roster({name: record})
        self.context_sizes: list[int] = [100_000]
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.methods("thread/compact/start"), [])
        self.assertEqual(self.turn_starts, 2)
