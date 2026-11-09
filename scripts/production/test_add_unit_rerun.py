"""A stopped add-unit command finishes on the same row and session."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone
from scripts.production import add_unit
from scripts.herdr import herdr
from scripts.production import unit_lookup
from scripts.message import sessions
from scripts.agents import shutdown
from scripts.production import unit_lookup as script_unit_lookup
from scripts.lib import atomic_file
from scripts.production.production_doc import Refusal, read_production
from scripts.production import staff, unit_config
from scripts.production.staff_test_support import unit_membership


def setUpModule() -> None:
    configure_test_zone()


class AddUnitRerunTests(AddUnitTestCase):
    def dead_pending_resume(self) -> tuple[add_unit.RequestedUnitLaunch, Path]:
        worktree = self.root / "project-alpha"
        owner = self.prepare_restored_worktree("alpha", worktree)
        request = add_unit.RequestedUnitLaunch(
            production=read_production(self.production_name), requested_name="alpha", unit="alpha",
            branch="build-followups-alpha", worktree=worktree,
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(owner.current_session, worktree), timeout=5,
        )
        pending = add_unit.opening_record(request.production, request.unit)
        pending.parent.mkdir(parents=True, exist_ok=True)
        _ = pending.write_text(json.dumps({
            "session_id": owner.current_session,
            "config_dir": str(Path(self.env["HOME"]) / ".claude2"),
            "prompt": "/unit:direct docs/plans/given.md",
            "launched_at": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
        return request, pending

    def test_resume_check_keeps_dead_pending_opening_without_a_recorded_model(self) -> None:
        request, pending = self.dead_pending_resume()
        assert isinstance(request.session, add_unit.ResumedSession)
        before = pending.read_bytes(), pending.stat().st_mtime_ns
        state = self.staff.staff_file.read_bytes(), self.repository_state()
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(unit_lookup, "labeled_session", return_value=None)):
            checked = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume",
                               request.session.session_id, "--cwd", str(request.worktree), "--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertTrue(pending.exists(), "--check removed the dead pending opening")
        self.assertEqual((pending.read_bytes(), pending.stat().st_mtime_ns), before)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.repository_state()), state)
        self.assertFalse(any(command[2:4] == ["agent", "start"] for command in self.herdr_calls()))

    def test_dead_pending_resume_does_not_bypass_the_recorded_worktree(self) -> None:
        request, pending = self.dead_pending_resume()
        assert isinstance(request.session, add_unit.ResumedSession)
        wrong = self.root / "prior-session"
        wrong.mkdir()
        request = replace(request, session=request.session._replace(cwd=wrong))
        before = pending.read_bytes()
        with (patch.dict(os.environ, self.env),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())):
            selected = add_unit.account_for_launch(request)
            with self.assertRaisesRegex(Refusal, "its staff row names"):
                _ = add_unit.preflight(request, selected)
        assert isinstance(request.session, add_unit.ResumedSession)
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(unit_lookup, "labeled_session", return_value=None)):
            checked = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume",
                               request.session.session_id, "--cwd", str(wrong), "--check")
        self.assertEqual(checked.returncode, 2, checked.stdout + checked.stderr)
        self.assertIn("its staff row names", checked.stderr)
        self.assertEqual(pending.read_bytes(), before)

    def test_dead_pending_lookup_keeps_the_labeled_session_fallback_and_evidence(self) -> None:
        request, pending = self.dead_pending_resume()
        before = pending.read_bytes()
        marked = unit_lookup.LabeledSession(
            "alpha", unit_lookup.LiveClaude("alpha", self.env["FAKE_CLAUDE_SESSION_ID"], "/fixture", 1234),
            herdr.HerdrPlace("agents", "w1:p1"),
            identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        with (patch.dict(os.environ, self.env),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(unit_lookup, "labeled_session", return_value=marked) as lookup):
            found = add_unit.launched_unit(request)
        self.assertEqual(found, add_unit.LabeledSessionFound(marked))
        lookup.assert_called_once_with(request.production.name, request.unit)
        self.assertTrue(pending.exists(), "read-only marked lookup removed the pending opening")
        self.assertEqual(pending.read_bytes(), before)

    def test_execution_cleans_dead_pending_opening_after_read_only_discovery(self) -> None:
        request, pending = self.dead_pending_resume()
        before = pending.read_bytes()
        with (patch.dict(os.environ, self.env),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(unit_lookup, "labeled_session", return_value=None)):
            self.assertEqual(add_unit.model_for_resume(request), "")
            self.assertTrue(pending.exists(), "model discovery removed the pending opening")
            self.assertEqual(pending.read_bytes(), before)
            with patch.object(add_unit, "launch_session", side_effect=RuntimeError("stopped at new backend")) as launch:
                result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("stopped at new backend", result.stderr)
        launch.assert_called_once()
        self.assertFalse(pending.exists(), "execution retained the dead pending opening")

    def test_retry_after_brief_commit_updates_the_worktree_before_launch(self) -> None:
        commit = add_unit.commit_unit

        def interrupted(request: add_unit.UnitLaunch, transition: add_unit.NoPlanTransition | add_unit.BriefToPlanTransition) -> None:
            commit(request, transition)
            raise OSError("stopped after brief commit")

        with patch.object(add_unit, "commit_unit", side_effect=interrupted):
            stopped = self.cli("alpha", "--brief", "Keep the committed brief in the unit worktree.")
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        worktree = self.root / "project-alpha"
        config = unit_config.read(worktree)
        assert isinstance(config, unit_config.UnitConfig)
        assert isinstance(config.plan, unit_config.PlanFile)
        self.assertFalse((worktree / config.plan.path).exists())
        _ = self.successful("alpha", "--brief", "Keep the committed brief in the unit worktree.")
        self.assertEqual((worktree / config.plan.path).read_bytes(), (self.checkout / config.plan.path).read_bytes())
        self.assertEqual(self.git("-C", str(worktree), "rev-parse", "HEAD"),
                         self.git("rev-parse", "build-followups"))
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        before = (self.staff.staff_file.read_bytes(), self.doc.read_bytes(), (worktree / ".unit.toml").read_bytes(),
                  self.git("-C", str(worktree), "rev-parse", "HEAD"))
        starts = sum(command[2:4] == ["agent", "start"] for command in self.herdr_calls())
        _ = self.successful("alpha", "--brief", "Keep the committed brief in the unit worktree.")
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes(), (worktree / ".unit.toml").read_bytes(),
                          self.git("-C", str(worktree), "rev-parse", "HEAD")), before)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(sum(command[2:4] == ["agent", "start"] for command in self.herdr_calls()), starts)

    def test_retry_after_config_publication_reuses_the_row_file_and_launch(self) -> None:
        publish = unit_config.write

        def interrupted(worktree: Path, config: unit_config.UnitConfig) -> None:
            _ = publish(worktree, config)
            raise OSError("stopped after config publication")

        arguments = ("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                     "--responsibility", "nightly")
        with patch.object(unit_config, "write", side_effect=interrupted):
            stopped = self.cli(*arguments)
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        self.assertEqual(staff.find(kind="unit"), [])
        path = self.root / "project-alpha/.unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertFalse(any(command[2:4] == ["agent", "start"] for command in self.herdr_calls()))
        submitted: list[str] = []

        def submit(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                   _at: datetime, _timeout: int) -> herdr.PromptSubmitted:
            assert isinstance(opening.transcript, herdr.SessionTranscript)
            row = staff.by_id(staff.native_id(opening.transcript.session_id))
            assert row is not None
            self.assertEqual((row.name, row.worktree), ("alpha", staff.PresentWorktree("natedev", path.parent)))
            submitted.append(row.id)
            client.prompt(pane, opening.text, target=opening.transcript.session_id)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        with patch.object(herdr.Herdr, "submit_opening_prompt", submit):
            _ = self.successful(*arguments)
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        self.assertEqual(submitted, [row.id])
        self.assertEqual(row.id, self.env["FAKE_CLAUDE_SESSION_ID"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertNotIn("STAFF_ID", self.pane_environment())
        calls = self.herdr_calls()
        _ = self.successful(*arguments)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual([command for command in self.herdr_calls()[len(calls):]
                          if command[2:4] in (["agent", "start"], ["pane", "close"], ["tab", "create"])], [])

    def test_retired_resume_finishes_the_live_sessions_pending_opening(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        arguments = ("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli(*arguments)
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        unit = shutdown.RetiredUnitRecovery(read_production(self.production_name), "alpha", self.root / "project-alpha",
            "alpha", unit_config.PlanFile(Path("docs/plans/given.md")), "62ccbd64-f22e-5647-b20e-7899aa39d766")
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        output = io.StringIO()

        def run_resume(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return self.cli(*argv[4:])

        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(1234)),
              patch.object(unit_lookup, "labeled_session", return_value=marked),
              patch.object(script_unit_lookup, "labeled_session", return_value=marked),
              patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(shutdown, "retirement_run", side_effect=run_resume),
              redirect_stdout(output)):
            shutdown.recovery_launch(unit)
        self.assertIn("alpha started:", output.getvalue())
        self.assertIn("resumed: session 62ccbd64-f22e-5647-b20e-7899aa39d766", output.getvalue())
        self.assertIn("Run /unit:direct", self.opening_prompt())
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha.json").exists())

    def test_live_resumed_session_finishes_its_pending_opening_without_second_start(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        arguments = ("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior),
                     "--autocompact", "200k")
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli(*arguments)
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(1234)),
              patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked)):
            finished = self.successful(*arguments)
        self.assertIn("alpha started:", finished.stdout)
        self.assertIn("Run /unit:direct", self.opening_prompt())
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha.json").exists())

    def test_closed_pending_session_does_not_block_a_different_marked_session(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", "replacement", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        before = len(self.herdr_calls())
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())):
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha started:", finished.stdout)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha.json").exists())
        self.assertFalse(any(row[2:4] in (["agent", "start"], ["agent", "prompt"])
                             for row in self.herdr_calls()[before:]))

    def test_other_live_pending_session_is_refused_and_keeps_its_record(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", "replacement", "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(5678))):
            refused = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("pending opening does not name the session that carries the unit's session label", refused.stderr)
        self.assertTrue((self.checkout / ".git/add-unit-openings/alpha.json").exists())

    def test_unsubmitted_opening_on_rerun_is_a_refusal_then_can_finish(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        session_id = self.env["FAKE_CLAUDE_SESSION_ID"]
        self.assertNotIn("--session-id", self.agent_arguments())
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", session_id, "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked)):
            with patch.object(herdr.Herdr, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("pane busy")):
                refused = self.cli("alpha", "--plan", "docs/plans/given.md")
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("add_unit: alpha: opening prompt not submitted: pane busy", refused.stderr)
            self.assertTrue((self.checkout / ".git/add-unit-openings/alpha.json").exists())
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha started:", finished.stdout)
        self.assertFalse((self.checkout / ".git/add-unit-openings/alpha.json").exists())

    def test_interrupted_brief_stub_publish_reruns_with_the_whole_source(self) -> None:
        write = atomic_file.write_text_atomic

        def interrupted_stub(path: Path, text: str) -> None:
            if "## Source\n\n" in text and "Keep every source word." in text:
                with patch("scripts.lib.atomic_file.os.fsync", side_effect=OSError("full disk")):
                    write(path, text)
            else:
                write(path, text)

        with patch.object(add_unit, "write_text_atomic", side_effect=interrupted_stub):
            stopped = self.cli("alpha", "--brief", "Keep every source word.")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        # Locate by its content after the rerun, without relying on the stub naming rule.
        finished = self.successful("alpha", "--brief", "Keep every source word.")
        self.assertIn("alpha started:", finished.stdout)
        stubs = [path for path in self.plan.parent.glob("*.md") if "Keep every source word." in path.read_text()]
        self.assertEqual(len(stubs), 1)
        self.assertIn("## Source\n\n", stubs[0].read_text())
        self.assertEqual(self.git("status", "--porcelain", "--", str(self.doc), str(stubs[0])), "")

    def test_started_session_without_opening_gets_its_prompt_on_rerun_without_second_start(self) -> None:
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()),
              patch.object(herdr.Herdr, "submit_opening_prompt", side_effect=RuntimeError("stopped before prompt"))):
            stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        session_id = self.env["FAKE_CLAUDE_SESSION_ID"]
        self.assertNotIn("--session-id", self.agent_arguments())
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", session_id, "/fixture", 1234),
                                       herdr.HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        before = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        submitted: list[herdr.OpeningPrompt] = []

        def submit_prompt(_client: herdr.Herdr, _pane: str, opening: herdr.OpeningPrompt,
                          _at: datetime, _timeout: int) -> herdr.PromptSubmitted:
            submitted.append(opening)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
              patch.object(add_unit, "wait_for_remote_control", return_value=marked),
              patch.object(herdr.Herdr, "submit_opening_prompt",
                           submit_prompt)):
            finished = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("alpha started:", finished.stdout)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].text, "/unit:direct docs/plans/given.md")
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), before)
        pending = self.checkout / ".git/add-unit-openings/alpha.json"
        self.assertFalse(pending.exists())
