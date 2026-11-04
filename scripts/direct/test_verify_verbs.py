"""Refuse unknown verbs before target queries or step records."""

from __future__ import annotations

import unittest

from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture


class VerifyVerbTests(VerifyLintCacheFixture):
    def test_unknown_verb_from_mesh_worker_prints_usage_without_creating_a_run(self) -> None:
        mesh_runs = self.root / "mesh_runs"
        mesh_script = self.root / "mesh-runs.py"
        self.write_executable(
            mesh_script,
            f"from pathlib import Path\nruns = Path({str(mesh_runs)!r})\n"
            + "runs.mkdir()\n(runs / 'unexpected.json').write_text('{}')\n",
        )
        self.environment.update({
            "CODEX_THREAD_ID": "test-mesh-worker",
            "VERIFY_MESH_RUNS_SCRIPT": str(mesh_script),
        })
        result = self.invoke_verify("fmt", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(list(mesh_runs.glob("*.json")), [])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assertEqual(self.cargo_calls(), [])

    def test_unknown_verb_prints_usage_before_target_query_or_step_record(self) -> None:
        target_query = self.root / "target-query"
        self.write_executable(
            self.root / "build-targets.py",
            f"from pathlib import Path\nPath({str(target_query)!r}).touch()\nraise SystemExit(2)\n",
        )
        result = self.invoke_verify("fmt", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(target_query.exists())
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assertEqual(self.cargo_calls(), [])


if __name__ == "__main__":
    _ = unittest.main()
