"""Saved assignment headers retain phase identity without consulting live sessions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.guilds import astra_work  # noqa: E402

SCRIPT = Path(__file__).with_name("astra_work.py")


class AstraWorkTests(unittest.TestCase):
    root: Path = Path()
    run_directory: Path = Path()
    history: Path = Path()
    document: Path = Path()
    text_file: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_directory = self.root / "fixture-run"
        self.run_directory.mkdir()
        self.history = self.root / "history.jsonl"
        self.document = self.root / "design.md"
        _ = self.document.write_text("# Design\n", encoding="utf-8")
        self.text_file = self.root / "brief-source.md"
        _ = self.text_file.write_text("Read the design.\nAnswer each finding.\n", encoding="utf-8")
        self.write_progress()

    def write_progress(self) -> None:
        _ = (self.run_directory / "progress_history_state.json").write_text(json.dumps({
            "run_id": "durable-run-id", "history_file": str(self.history),
            "open_phases": [{"id": "3", "instance_id": "instance-three", "status": "open"}],
            "phase": {"id": "4", "instance_id": "instance-four", "status": "active"},
        }), encoding="utf-8")

    def brief(self, phase: str = "4") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "brief", "--run-directory", str(self.run_directory),
                               "--phase", phase, "--member", "fixture-designer", "--document", str(self.document),
                               "--round", "2", "--text-file", str(self.text_file)],
                              env={**os.environ, "HOME": str(self.root)}, capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def header_payload(self, path: Path) -> dict[str, object]:
        first = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("<!-- astra_work "), first)
        self.assertTrue(first.endswith(" -->"), first)
        return cast(dict[str, object], json.loads(first[len("<!-- astra_work "):-len(" -->")]))

    def test_phase_resolution_uses_the_named_open_instance_and_durable_run_fields(self) -> None:
        for phase, instance in (("3", "instance-three"), ("4", "instance-four")):
            with self.subTest(phase=phase):
                work = astra_work.phase_work(self.run_directory, phase)
                self.assertIsInstance(work, astra_work.PhaseWork)
                self.assertEqual(work.run_id, "durable-run-id")
                self.assertEqual(str(work.run_directory), str(self.run_directory))
                self.assertEqual(str(work.history_file), str(self.history))
                self.assertEqual(work.phase_id, phase)
                self.assertEqual(work.phase_instance_id, instance)

    def test_unresolved_phase_and_missing_or_invalid_progress_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            _ = astra_work.phase_work(self.run_directory, "99")
        state = self.run_directory / "progress_history_state.json"
        for contents in ("{broken", "{}", '{"run_id":"x","history_file":"h","phase":{"id":"4","status":"closed","instance_id":"old"}}'):
            with self.subTest(contents=contents):
                _ = state.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    _ = astra_work.phase_work(self.run_directory, "4")
        state.unlink()
        with self.assertRaises(ValueError):
            _ = astra_work.phase_work(self.run_directory, "4")

    def test_brief_header_round_trip_keeps_assignment_and_body(self) -> None:
        result = self.brief()
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.run_directory / "design/phase4/round2_brief.md"
        payload = self.header_payload(path)
        self.assertEqual({key: value for key, value in payload.items() if key != "assigned_at_utc"}, {
            "schema": 1, "role": "astra-designer", "member": "fixture-designer",
            "document": str(self.document), "reply_path": str(path.with_name("round2_reply.md")),
            "work": {"kind": "phase", "run_directory": str(self.run_directory), "run_id": "durable-run-id",
                     "phase_id": "4", "phase_instance_id": "instance-four", "history_file": str(self.history)},
        })
        stamp = datetime.fromisoformat(cast(str, payload["assigned_at_utc"]).replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertEqual(path.read_text().split("\n", 1)[1], self.text_file.read_text())
        saved = astra_work.read_header(path)
        self.assertIsInstance(saved, astra_work.AstraWork)
        if isinstance(saved, astra_work.AstraWork):
            rewritten = self.root / "round-trip.md"
            _ = rewritten.write_text(astra_work.header_line(saved) + "\n", encoding="utf-8")
            self.assertEqual(astra_work.read_header(rewritten), saved)

    def test_brief_refuses_overwrite_and_does_not_touch_an_existing_reply(self) -> None:
        first = self.brief()
        self.assertEqual(first.returncode, 0, first.stderr)
        brief = self.run_directory / "design/phase4/round2_brief.md"
        original = brief.read_bytes()
        reply = brief.with_name("round2_reply.md")
        _ = reply.write_text("Saved designer reply.\n", encoding="utf-8")
        _ = self.text_file.write_text("Replacement brief.\n", encoding="utf-8")
        second = self.brief()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(brief.read_bytes(), original)
        self.assertEqual(reply.read_text(), "Saved designer reply.\n")

    def test_unresolved_cli_phase_creates_no_brief(self) -> None:
        result = self.brief("99")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertFalse((self.run_directory / "design").exists())

    def test_reader_distinguishes_missing_headers_from_invalid_assignment_metadata(self) -> None:
        path = self.root / "prompt.md"
        _ = path.write_text("# Legacy prompt\n", encoding="utf-8")
        self.assertIsInstance(astra_work.read_header(path), astra_work.HeaderMissing)
        for first in ('<!-- astra_work {broken} -->', '<!-- astra_work {"schema": 2} -->',
                      '<!-- astra_work {"schema":1,"work":{"kind":"phase"}} -->'):
            with self.subTest(first=first):
                _ = path.write_text(first + "\nbody\n", encoding="utf-8")
                self.assertIsInstance(astra_work.read_header(path), astra_work.HeaderInvalid)

    def test_standalone_header_round_trip_has_no_phase_context(self) -> None:
        work = astra_work.AstraWork(
            role="astra-reviewer", member="fixture-review", document=str(self.document),
            assigned_at_utc="2026-10-21T10:00:00+00:00", reply_path=str(self.root / "review.md"),
            work=astra_work.StandaloneWork(),
        )
        path = self.root / "standalone.md"
        _ = path.write_text(astra_work.header_line(work) + "\nReview body.\n", encoding="utf-8")
        self.assertEqual(astra_work.read_header(path), work)
        self.assertEqual(self.header_payload(path)["work"], {"kind": "standalone"})

    def test_reader_rejects_unknown_roles_and_incomplete_phase_ids(self) -> None:
        result = self.brief()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.header_payload(self.run_directory / "design/phase4/round2_brief.md")
        path = self.root / "invalid.md"
        phase = cast(dict[str, object], payload["work"])
        for changed in ({**payload, "role": "impl"}, {**payload, "schema": True},
                        {**payload, "work": {**phase, "phase_instance_id": ""}},
                        {**payload, "assigned_at_utc": "2026-10-21T10:00:00"}):
            with self.subTest(changed=changed):
                _ = path.write_text("<!-- astra_work " + json.dumps(changed) + " -->\n", encoding="utf-8")
                self.assertIsInstance(astra_work.read_header(path), astra_work.HeaderInvalid)


if __name__ == "__main__":
    _ = unittest.main()
