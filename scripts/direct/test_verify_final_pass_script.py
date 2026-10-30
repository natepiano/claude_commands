"""Final gate script checks."""
from __future__ import annotations

from pathlib import Path
import shutil
import signal
import subprocess
from scripts.direct.test_verify_untested_examples import VERIFY
from scripts.direct.verify_final_pass_test_support import TEST_HANG_GUARD_SECS
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_query_refuses_a_changed_verify_script(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = Path(self.environment["HOME"]) / "changed/direct"
        folder.mkdir(parents=True)
        (folder.parent / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        _ = shutil.copyfile(VERIFY.parent / "final_pass.py", folder / "final_pass.py")
        changed = folder / "verify.sh"
        _ = changed.write_text(VERIFY.read_text() + "\n# Test script change.\n")
        answer = subprocess.run(["bash", str(changed), "final-pass"], cwd=self.root,
                                env=self.environment, text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("verify.sh differs", answer.stderr)

    def test_python_trace_records_final_helpers_and_query_without_changing_output(self) -> None:
        trace = self.root.parent / "python-starts.tsv"
        self.environment["VERIFY_PY_TRACE"] = str(trace)
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(trace.is_file(), result.stdout + result.stderr)
        rows = trace.read_text().splitlines()
        self.assertTrue(rows)
        names: list[str] = []
        for row in rows:
            with self.subTest(row=row):
                self.assertRegex(row, r"^[A-Za-z_][A-Za-z0-9_.-]*\t[0-9]+\t[0-9]+$")
                name, wall_ms, cpu_ms = row.split("\t")
                names.append(name)
                self.assertGreaterEqual(int(wall_ms), 0)
                self.assertGreaterEqual(int(cpu_ms), 0)
        # A full final starts its proof before the gate and finishes it on exit.
        self.assertEqual(names.count("final_pass.py"), 2, rows)

        query = self.verify("final-pass")
        self.assertEqual(query.returncode, 0, query.stdout + query.stderr)
        appended = trace.read_text().splitlines()
        self.assertEqual(appended[:len(rows)], rows)
        query_rows = appended[len(rows):]
        # The read-only path starts exactly its query helper, without metadata.
        self.assertEqual(len(query_rows), 1, query_rows)
        self.assertRegex(query_rows[0], r"^final_pass\.py\t[0-9]+\t[0-9]+$")

        trace.unlink()
        del self.environment["VERIFY_PY_TRACE"]
        untraced = self.verify("final-pass")
        self.assertEqual(untraced.returncode, 0, untraced.stdout + untraced.stderr)
        self.assertEqual(untraced.stdout, query.stdout)
        self.assertEqual(untraced.stderr, query.stderr)
        self.assertFalse(trace.exists())

    def test_python_trace_records_sigterm_and_preserves_signal_status(self) -> None:
        folder = self.root.parent / "terminated/direct"
        folder.mkdir(parents=True)
        (folder.parent / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        changed = folder / "verify.sh"
        _ = shutil.copyfile(VERIFY, changed)
        _ = (folder / "final_pass.py").write_text(
            "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
        trace = self.root.parent / "terminated.tsv"
        environment = {**self.environment, "VERIFY_PY_TRACE": str(trace)}
        result = subprocess.run(["bash", str(changed), "final-pass"], cwd=self.root,
                                env=environment, text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertTrue(trace.exists(), "SIGTERM lost the helper trace")
        self.assertRegex(trace.read_text(), r"\Afinal_pass\.py\t[0-9]+\t[0-9]+\n\Z")


if __name__ == "__main__":
    _ = unittest.main()
