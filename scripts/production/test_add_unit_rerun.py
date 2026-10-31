"""A stopped add-unit command finishes on the same row and session."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone
import add_unit
import herdr
import unit_lookup
import sessions
import resume_unit
import retire_unit
import atomic_file
from production_units import read_production


def setUpModule() -> None:
    configure_test_zone()


class AddUnitRerunTests(AddUnitTestCase):
    def test_retired_resume_finishes_the_live_sessions_pending_opening(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        arguments = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior))
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli(*arguments)
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", "session-123", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        unit = resume_unit.RetiredUnit(read_production(self.doc), "alpha-unit", self.root / "home/worktrees/alpha",
            "alpha", "docs/plans/given.md", "session-123", True, resume_unit.RecordedStartFolder(prior))
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        output = io.StringIO()

        def run_resume(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return self.cli(*argv[4:])

        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(1234)),
              patch.object(unit_lookup, "marked_unit", return_value=marked),
              patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(retire_unit, "run", side_effect=run_resume),
              redirect_stdout(output)):
            resume_unit.launch(unit)
        self.assertIn("alpha-unit started:", output.getvalue())
        self.assertIn("resumed: session session-123", output.getvalue())
        self.assertIn("Run /unit:direct", self.opening_prompt())
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())

    def test_live_resumed_session_finishes_its_pending_opening_without_second_start(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        arguments = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior),
                     "--autocompact", "200k")
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli(*arguments)
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", "session-123", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(1234)),
              patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked)):
            finished = self.successful(*arguments)
        self.assertIn("alpha-unit started:", finished.stdout)
        self.assertIn("Run /unit:direct", self.opening_prompt())
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())

    def test_closed_pending_session_does_not_block_a_different_marked_session(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", "replacement", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        before = len(self.herdr_calls())
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())):
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha-unit started:", finished.stdout)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())
        self.assertFalse(any(row[2:4] in (["agent", "start"], ["agent", "prompt"])
                             for row in self.herdr_calls()[before:]))

    def test_other_live_pending_session_is_refused_and_keeps_its_record(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", "replacement", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(5678))):
            refused = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("pending opening does not name the marked unit's live session", refused.stderr)
        self.assertTrue((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())

    def test_unsubmitted_opening_on_rerun_is_a_refusal_then_can_finish(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        arguments = self.agent_arguments()
        session_id = arguments[arguments.index("--session-id") + 1]
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", session_id, "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked)):
            with patch.object(herdr.Herdr, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("pane busy")):
                refused = self.cli("alpha", "--plan", "docs/plans/given.md")
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("add_unit: alpha-unit: opening prompt not submitted: pane busy", refused.stderr)
            self.assertTrue((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha-unit started:", finished.stdout)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha-unit.json").exists())

    def test_interrupted_brief_stub_publish_reruns_with_the_whole_source(self) -> None:
        write = atomic_file.write_text_atomic

        def interrupted_stub(path: Path, text: str) -> None:
            if "## Source\n\n" in text and "Keep every source word." in text:
                with patch("atomic_file.os.fsync", side_effect=OSError("full disk")):
                    write(path, text)
            else:
                write(path, text)

        with patch.object(add_unit, "write_text_atomic", side_effect=interrupted_stub):
            stopped = self.cli("alpha", "--brief", "Keep every source word.")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        # Locate by its content after the rerun, without relying on the stub naming rule.
        finished = self.successful("alpha", "--brief", "Keep every source word.")
        self.assertIn("alpha-unit started:", finished.stdout)
        stubs = [path for path in self.plan.parent.glob("*.md") if "Keep every source word." in path.read_text()]
        self.assertEqual(len(stubs), 1)
        self.assertIn("## Source\n\n", stubs[0].read_text())
        self.assertEqual(self.git("status", "--porcelain", "--", str(self.doc), str(stubs[0])), "")

    def test_interrupted_row_publish_preserves_other_units_and_rerun_commits_row(self) -> None:
        original = self.doc.read_bytes()
        write = atomic_file.write_text_atomic

        def interrupted_row(path: Path, text: str) -> None:
            if path == self.doc:
                with patch("atomic_file.os.replace", side_effect=OSError("stopped before publish")):
                    write(path, text)
            else:
                write(path, text)

        with patch.object(add_unit, "write_text_atomic", side_effect=interrupted_row):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        self.assertEqual(self.doc.read_bytes(), original)
        finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha-unit started:", finished.stdout)
        self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
        self.assertEqual(self.git("status", "--porcelain", "--", str(self.doc)), "")

    def test_started_session_without_opening_gets_its_prompt_on_rerun_without_second_start(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        arguments = self.agent_arguments()
        session_id = arguments[arguments.index("--session-id") + 1]
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude("alpha", session_id, "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"))
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        submitted: list[herdr.OpeningPrompt] = []

        def submit_prompt(_client: herdr.Herdr, _pane: str, opening: herdr.OpeningPrompt,
                          _at: datetime, _timeout: int) -> herdr.PromptSubmitted:
            submitted.append(opening)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(herdr.Herdr, "submit_opening_prompt",
                           submit_prompt)):
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha-unit started:", finished.stdout)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].text, "/unit:direct docs/plans/given.md")
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        pending = self.checkout / ".git/add-unit-openings/alpha-unit.json"
        self.assertFalse(pending.exists())
