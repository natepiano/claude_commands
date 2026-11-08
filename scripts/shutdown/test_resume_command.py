"""The `resume` command starts shutdown.py's resume verb from a link on PATH."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

COMMAND = Path(__file__).resolve().with_name("resume")


class ResumeCommandTests(unittest.TestCase):
    def test_a_linked_command_prints_the_resume_usage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / ".claude").symlink_to(Path(__file__).resolve().parents[2], target_is_directory=True)
            link = home / "resume"
            link.symlink_to(COMMAND)
            result = subprocess.run([str(link), "--help"], cwd=folder, capture_output=True, text=True,
                                    timeout=60, env={**os.environ, "HOME": str(home), "HERDR_BIN": "/nonexistent/herdr"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--why", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
