"""Final gate compiler checks."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_query_refuses_a_changed_compiler(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stderr)
        compiler = Path(self.environment["HOME"]) / "stubs/rustc"
        _ = compiler.write_text('#!/bin/sh\nprintf "different compiler\\n"\n')
        answer = self.verify("final-pass")
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("rustc differs", answer.stderr)


if __name__ == "__main__":
    _ = unittest.main()
