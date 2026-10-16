#!/usr/bin/env python3
"""A phase's launcher files live in its own directory, and each reader and cleaner finds them there.

Two phases open at once each launch with `--phase <id>`, so their status, findings and prompts
sit under `${SESSION_DIR}/phases/<id>/` and never overwrite each other.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.hooks import delegate_run

CLEAR = Path(__file__).with_name("clear_phase_review.sh")


class PhaseArtifactTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session = Path(self.temporary.name) / "run"
        for phase in ("3", "4"):
            (self.session / "phases" / phase).mkdir(parents=True)
        _ = (self.session / "progress_history_state.json").write_text("{}", encoding="utf-8")

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str = "") -> Path:
        path = self.session / relative
        _ = path.write_text(text, encoding="utf-8")
        return path

    def test_clearing_a_phase_review_leaves_the_phase_beside_it(self) -> None:
        cleared = [self.write(f"phases/3/{name}") for name in ("review_prompt_1_adversary.md",
                                                                "review_findings_1_adversary.txt",
                                                                "review_status_adversary", "ux_shots_1.txt")]
        kept = [self.write(f"phases/4/{name}") for name in ("review_prompt_1_adversary.md",
                                                             "review_findings_1_adversary.txt")]
        kept.append(self.write("phases/3/implementation_prompt.md"))
        result = subprocess.run(["bash", str(CLEAR), str(self.session), "3"], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path for path in cleared if path.exists()], [])
        self.assertEqual([path for path in kept if not path.exists()], [])

    def test_a_phase_reviewing_counts_as_running_work(self) -> None:
        _ = self.write("phases/4/review_status_adversary", "reviewing\n")
        self.assertEqual(delegate_run.running_work(self.session), "a review pass")

    def test_a_phase_implementing_counts_as_running_work(self) -> None:
        _ = self.write("phases/3/impl_status_impl", "implementing\n")
        self.assertEqual(delegate_run.running_work(self.session), "an implementation or fix pass")


if __name__ == "__main__":
    _ = unittest.main()
