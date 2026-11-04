"""test implement launcher follow integration coverage."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.lib.test_models_registry import write_registry
from scripts.direct.implement_launcher_claims_test_support import CompletedWorkerFixture
from scripts.direct.implement_launcher_test_support import MODELS_REGISTRY as MODELS_REGISTRY
from typing import cast
import json
import subprocess


class ImplementLauncherWorkerTests(CompletedWorkerFixture):
    def test_a_repair_pass_leaves_the_impl_worker_followable(self) -> None:
        session_dir, first = self.completed_worker()
        self.assertEqual(first.returncode, 0, first.stderr)
        impl = cast("dict[str, object]", json.loads((session_dir / "impl_worker_impl.json").read_text()))
        repair = self.mesh_launch(session_dir, kind="fix", fix_cause="retry test failed after integration")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual(json.loads((session_dir / "impl_worker_impl.json").read_text()), impl)
        self.assertTrue(self.recorded_worker(session_dir, "fix").name.endswith("-fix"))
        result = self.mesh_launch(session_dir, to=self.recorded_worker(session_dir).name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_impl_worker_keeps_one_identity_when_followed_as_a_repair_worker(self) -> None:
        run, opened = self.completed_worker()
        self.assertEqual(opened.returncode, 0, opened.stderr)
        path = run / "impl_worker_impl.json"
        original = cast(dict[str, object], json.loads(path.read_text()))
        name = self.recorded_worker(run).name

        followed = self.mesh_launch(run, to=name, kind="fix", fix_cause="repair identity after implementation")

        self.assertEqual(followed.returncode, 0, followed.stderr)
        records = [cast(dict[str, object], json.loads(item.read_text()))
                   for item in run.glob("impl_worker_*.json")]
        identities = [record for record in records if record.get("staff_id") == original["staff_id"]]
        self.assertEqual(len(identities), 1, records)
        self.assertEqual(identities[0]["staff_id"], original["staff_id"])
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["staff_id"], original["staff_id"])
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster[name]["role"], "fix")
        self.assertFalse((run / "impl_worker_fix.json").exists())

    def test_follow_up_of_a_worker_with_no_identity_record_resumes_its_roster_thread(self) -> None:
        session_dir, first = self.completed_worker()
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "impl_worker_impl.json"
        identity = cast("dict[str, object]", json.loads(path.read_text()))
        name = self.recorded_worker(session_dir).name
        path.unlink()
        resumed = self.mesh_launch(session_dir, to=name)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(path.read_text()), identity)

    def test_follow_up_uses_the_current_registry_model(self) -> None:
        session_dir, first = self.completed_worker()
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        registry = self.root / ".claude" / "config" / "models.conf"
        _ = write_registry(registry, {"direct.codex": {"impl": "gpt-blind:max"}}, base_text=MODELS_REGISTRY)
        # The next turn follows the edited registry; the saved identity remains history.
        changed_model = self.mesh_launch(session_dir, to=name)
        self.assertEqual(changed_model.returncode, 0, changed_model.stderr)
        self.assertIn("agent=gpt-blind\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        self.assertIn("effort=max\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        identity = cast("dict[str, object]", json.loads((session_dir / "impl_worker_impl.json").read_text()))
        self.assertEqual(identity["model"], "gpt-called")

    def test_follow_up_resumes_a_worker_recorded_before_the_rename(self) -> None:
        # A historical identity may name a model the current registry no longer selects.
        session_dir, first = self.completed_worker()
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "impl_worker_impl.json"
        identity = cast("dict[str, object]", json.loads(path.read_text()))
        _ = path.write_text(json.dumps({**identity, "model": "gpt-blind"}))
        resumed = self.mesh_launch(session_dir, to=self.recorded_worker(session_dir).name)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("agent=gpt-called\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        self.assertIn("effort=xhigh\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text()), {**identity, "model": "gpt-blind"})

    def test_unreadable_staff_follow_refuses_before_recording_a_pass(self) -> None:
        session_dir, first = self.completed_worker()
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        status_path = session_dir / "impl_status_impl"
        before = status_path.read_text(encoding="utf-8")
        passes = self.open_passes(session_dir)
        environment = self.environment()
        _ = (self.root / "staff.json").write_text("{invalid staff")
        args = ["bash", str(self.implement_script), "--to", name,
                str(session_dir), str(self.working_dir), str(self.prompt_file),
                "impl", "the retry path", "impl", "writing the retry path", "0", "impl"]

        refused = subprocess.run(args, env=environment, capture_output=True,
                                 text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("not opened:", refused.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.open_passes(session_dir), passes)
        self.assertNotIn("follow-up to", (session_dir / "board.log").read_text())


if __name__ == "__main__":
    _ = unittest.main()
