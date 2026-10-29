"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
import production_units
import autocompact
import conversation_pause
import launch_permission
import unit_lookup
import sessions
from herdr import HerdrPlace
from scripts.accounts import accounts
import herdr
from scripts.herdr.fake_herdr import CommandResponse


from add_unit_test_support import AddUnitTestCase, configure_test_zone, SHARED, SUBMITTED_PROMPT


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitRestoreTests(AddUnitTestCase):
    def test_restore_reports_not_submitted_to_its_parent_and_does_not_report_launched(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr")
        output = io.StringIO()
        with redirect_stdout(output):
            code, error, _commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), already_marked=False,
                outcome=herdr.PromptNotSubmitted("fixture prompt remains in input"))
        self.assertEqual(code, 1)
        self.assertEqual(launches, 1)
        self.assertIn("alpha-unit", error)
        self.assertIn("restart prompt not submitted", error)
        self.assertNotIn("restore launched", output.getvalue())
        line, = output.getvalue().splitlines()
        self.assertTrue(line.startswith("restart-prompt-outcome: "))
        self.assertEqual(herdr.decode_prompt_outcome(line.removeprefix("restart-prompt-outcome: ")),
                         herdr.PromptNotSubmitted("fixture prompt remains in input"))

    def test_restore_returns_zero_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "build-followups-recorded-herdr"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "restart-prompt-outcome: " + herdr.encode_prompt_outcome(SUBMITTED_PROMPT) + "\nalpha-unit restore launched: herdr session attach agents, workspace director, tab recorded-name\n")

    def test_restore_already_running_reports_missing_submit_proof_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)
            )
        self.assertEqual(result, 1)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertNotIn("restore launched", output.getvalue())
        self.assertIn("restart-prompt-outcome: ", output.getvalue())

    def test_restore_replaces_stopped_herdr_unit_by_name_by_herdr_name(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        with patch("herdr.stop_session") as stopped:
            result, error, commands, launches, writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), place=HerdrPlace("native-unit", "w1:p1")
            )
        self.assertEqual(result, 0, error)
        stopped.assert_called_once_with("native-unit")
        self.assertEqual(commands, [])
        self.assertEqual((launches, writes), (1, 1))

    def test_restore_already_live_in_herdr_launches_nothing_and_reports_missing_submit_proof(self) -> None:
        kind = add_unit.UnitRestoreLaunch("session-123", self.root / "restart-note", "recorded-name", "native-unit")
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(kind,
                unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123),
                place=HerdrPlace("native-unit", "w1:p1"))
        self.assertEqual(result, 1)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertNotIn("restore launched", output.getvalue())
        self.assertIn("restart-prompt-outcome: ", output.getvalue())

    def test_each_production_names_its_unit_session_and_a_restore_keeps_the_recorded_name(self) -> None:
        production = production_units.read_production(self.doc)
        request = add_unit.UnitLaunch(
            production=production, identity=add_unit.UnitIdentity("review-unit", "review"),
            branch="build-followups-review", worktree=self.root / "project-review",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")), port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(), session=add_unit.NewSession(), launch_kind=add_unit.NewWorkLaunch(),
            timeout=90)
        hana = dataclasses.replace(request, production=production._replace(slug="hana"))
        self.assertEqual(add_unit.host_session_name(request), "build-followups-review")
        self.assertEqual(add_unit.host_session_name(hana), "hana-review")
        named = dataclasses.replace(request, identity=add_unit.UnitIdentity("review-unit", "build-followups-review"))
        self.assertEqual(add_unit.host_session_name(named), "build-followups-review")
        style = dataclasses.replace(request, production=production._replace(slug="cargo-liner-style"),
                                    identity=add_unit.UnitIdentity("cargo-liner-style-names-unit", "cargo-liner-style-names"))
        self.assertEqual(add_unit.host_session_name(style), "cargo-liner-style-names")
        restore = add_unit.UnitRestoreLaunch("session-123", self.root / "note", "review", "build-followups-review")
        self.assertEqual(add_unit.host_session_name(dataclasses.replace(request, launch_kind=restore)),
                         "build-followups-review")
        # A record from before the slug led: its bare name may be another production's live session.
        before = add_unit.UnitRestoreLaunch("session-123", self.root / "note", "review", "review")
        self.assertEqual(add_unit.host_session_name(dataclasses.replace(hana, launch_kind=before)), "hana-review")

    def test_failed_unit_restore_closes_its_tab_after_agent_exit(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note"
        _ = note.write_text("Resume this unit.")
        with (patch.dict(os.environ, self.env),
              patch.object(herdr, "start_agent", side_effect=herdr.AgentExited("restored-unit", "w1:p1")),
              patch.object(herdr, "close_agent") as closed):
            request = add_unit.UnitLaunch(
                production=production_units.read_production(self.doc),
                identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
                branch="build-followups-alpha", worktree=self.root / "project-alpha",
                plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
                port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(),
                session=add_unit.ResumedSession("session-123", prior),
                launch_kind=add_unit.UnitRestoreLaunch("session-123", note, "recorded-name", "build-followups-alpha"),
                timeout=90,
            )
            self.configure_herdr(request)
            with self.assertRaises(herdr.AgentExited):
                _ = add_unit.launch_session(request, add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
                                        "Resume this unit.", accounts.role_account("unit"))
        closed.assert_called_once_with(SHARED, "w1:p1")
        self.assertEqual(self.servers_started(), [["--user", "start", "herdr-agents.service"]])
        self.assertFalse(any(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()))

    def test_running_restore_refuses_even_without_herdr_marks(self) -> None:
        self.commit_prepared_row(self.unit_row())
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.restore("alpha", prior)
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"session session-123 already runs as pid {os.getpid()}", result.stderr)
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_restart_restore_is_allowed_for_its_restarting_unit(self) -> None:
        self.commit_prepared_row(self.unit_row())
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restart_from_a_record_before_the_slug_led_skips_another_productions_live_session(self) -> None:
        self.commit_prepared_row(self.unit_row("review"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        # hana's review-unit holds the bare name this record was written under.
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(
            herdr.SessionListResult(sessions=[herdr.SessionEntry(name="review", running=True, default=False,
                                                                 session_dir="/fixture", socket_path="/fixture/socket")])))

        result = self.successful(
            "review", "--plan", "docs/plans/given.md", "--resume", "session-review", "--cwd", str(prior),
            "--restart-note", str(note), "--session-name", "review", "--host-session", "review",
        )

        self.assertIn("review-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual([row[4] for row in starts], ["build-followups-review"])

    def test_restart_restore_marks_only_the_probe_pane_despite_inherited_marker(self) -> None:
        self.env["CLAUDE_PROBE_SESSION"] = "1"
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        cases = ((False, ""), (True, ""), (False, "claude2"), (True, "claude2"))
        for probe, target in cases:
            with self.subTest(probe=probe, target=target):
                name = ("probe" if probe else "ordinary") + ("-move" if target else "-restore")
                self.commit_prepared_row(self.unit_row(name))
                result = self.successful(
                    name, "--plan", "docs/plans/given.md", "--resume", f"session-{name}",
                    "--cwd", str(prior), "--restart-note", str(note),
                    "--session-name", name, "--host-session", f"build-followups-{name}",
                    *(["--probe"] if probe else []),
                    *(["--account", target] if target else []),
                )
                self.assertIn(f"{name}-unit restore launched", result.stdout)
                # The first restore starts the shared server; each later one opens its tab there.
                self.assertEqual(len(self.servers_started()), 1)
                opened = self.opened()
                self.assertEqual(opened[opened.index("--cwd") + 1], str(prior))
                starts = [row for row in self.herdr_calls()
                          if row[:5] == ["--session", SHARED, "agent", "start", f"build-followups-{name}"]]
                self.assertEqual(len(starts), 1)
                arguments = starts[0][starts[0].index("--") + 1:]
                self.assertEqual(arguments[arguments.index("--resume") + 1], f"session-{name}")
                environment = self.pane_environment()
                self.assertEqual(environment.get("CLAUDE_PROBE_SESSION", ""), "1" if probe else "")
                self.assertNotIn("CLAUDE_TEST_SECRET", environment)

    def test_restart_restore_adopts_a_row_added_from_a_brief_whose_plan_is_now_its_as_built(self) -> None:
        checkout = self.doc.parents[2]
        (checkout / "docs/as-built").mkdir(parents=True, exist_ok=True)
        _ = (checkout / "docs/as-built/alpha.md").write_text("# Alpha\n", encoding="utf-8")
        row = self.unit_row(plan="docs/as-built/alpha.md")
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md", "docs/as-built/alpha.md")
        _ = self.git("commit", "-m", "production(build-followups): add unit alpha-unit (brief)")
        _ = self.git("push", "origin", "build-followups")
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/as-built/alpha.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restore_without_a_units_row_is_refused_and_adds_nothing(self) -> None:
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")

        result = self.cli(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr, "add_unit: unit alpha-unit has no Units row; a restore never adds one\n"
        )
        self.assert_no_launch_change(document, head)

    def test_restore_adopts_a_row_whose_worktree_and_branch_are_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()

        result = self.restore("trunk", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)

    def test_restore_in_a_cwd_other_than_the_row_worktree_launches_there(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        recorded = self.root / "prior-session"
        _ = self.git("worktree", "add", str(recorded), "-b", "build-followups-alpha")
        before = self.repository_state()

        result = self.restore("alpha", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)
        self.assertFalse(worktree.exists())

    def test_restore_in_the_row_worktree_on_another_branch_launches(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "other-branch")
        before = self.repository_state()

        result = self.restore("alpha", worktree)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.repository_state(), before)

    def test_restart_restore_is_refused_when_record_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_restart_restore_is_refused_when_record_does_not_hold_it(self) -> None:
        self.write_shutdown_record(
            self.shutdown_record("restarting", session_id="other-session")
        )
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_unreadable_shutdown_state_refuses_a_restore_with_one_prefix(self) -> None:
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{", encoding="utf-8")
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertTrue(
            result.stderr.startswith("add_unit: shutdown state unreadable: "),
            result.stderr,
        )
        self.assertEqual(result.stderr.count("shutdown state unreadable: "), 1)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_refused_marked_restore_does_not_stop_herdr_or_record_prompt(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123",
            self.root / "restart-note",
            "recorded-name",
            "recorded-herdr",
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeNotRunning()
        )

        self.assertEqual(result, 2)
        self.assertIn("/shutdown restart first", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_launch_barrier_covers_preflight_through_spawn(self) -> None:
        note = self.root / "restart-note"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        request = add_unit.UnitLaunch(
            production=production_units.read_production(self.doc),
            identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(
                "session-123", self.root / "prior-session"
            ),
            launch_kind=add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded-name", "recorded-herdr"
            ),
            timeout=1.0,
        )
        ready = add_unit.ReadyToLaunch(
            request,
            add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        old = self.shutdown_record("down")
        owner = cast(list[dict[str, object]], old["entries"])[0]
        session_shutdown: dict[str, object] = {
            "login": old["login"],
            "label": old["label"],
            "machine": old["machine"],
            "requested_at": old["requested_at"],
            "requested_by": old["requested_by"],
            "request_id": "launch-barrier-request",
            "hold": "account",
            "then": "stop",
            "force": old["force"],
            "alerts": "user",
            "workers": [],
            **owner,
        }
        creator: subprocess.Popen[str] | None = None

        def start_shutdown(_request: add_unit.UnitLaunch) -> None:
            nonlocal creator
            creator = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, record; assert record.create_session(record.parse_session_records(os.environ['TEST_RECORD'])[0])",
                ],
                cwd=Path(__file__).parents[1] / "shutdown",
                env={
                    **self.env,
                    "TEST_RECORD": json.dumps([session_shutdown]),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        def launch(
            _request: add_unit.UnitLaunch,
            _director: add_unit.DirectorAgent,
            _prompt: str,
            _selected: accounts.ClaudeLaunchAccount,
            *, target: str = "", model: str = "", autocompact_window: str = "",
        ) -> herdr.PromptSubmitted:
            self.assertEqual(target, "claude2" if isinstance(_request.session, add_unit.ResumedSession) else "")
            self.assertEqual((model, autocompact_window), ("", ""))
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            self.assertIsNone(creator.poll(), "shutdown record appeared before spawn")
            lock_path = Path(self.env["SHUTDOWN_STATE_DIR"]) / "launch.lock"
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )

            return SUBMITTED_PROMPT

        def finish_shutdown() -> None:
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            stdout, stderr = creator.communicate(timeout=300)
            self.assertEqual(creator.returncode, 0, (stdout, stderr))

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=add_unit.NoMarkedUnit(),
            ),
            patch.object(herdr, "live_sessions", return_value=[]),
            patch.object(add_unit, "write_stub", side_effect=start_shutdown),
            patch.object(add_unit, "append_row"),
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "record_scheduled_restore_prompt"),
            patch.object(add_unit, "launch_session", side_effect=launch),
            patch.object(add_unit, "wait_for_remote_control") as wait,
            patch.object(add_unit, "record"),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            result = add_unit.main(
                ["--production", "unused", "alpha", "--plan", "unused",
                 "--config-dir", str(Path(self.env["HOME"]) / ".claude2")]
            )
            finish_shutdown()

        self.assertEqual(result, 0, error.getvalue())
        wait.assert_not_called()
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        self.assertTrue((account / "sessions" / "session-123.json").is_file())

    def test_restore_recording_failure_uses_one_stderr_line_and_does_not_launch(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )

        with patch.object(
            conversation_pause,
            "record_scheduled_prompts",
            side_effect=OSError("first line\nsecond line"),
        ):
            result, error, _commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning()
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            error,
            "add_unit: restart prompt not recorded: first line\n",
        )
        self.assertEqual(launches, 0)

    def test_restore_keeps_marked_herdr_with_live_claude_and_reports_missing_submit_proof(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        live = unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)

        result, error, commands, launches, writes = self.run_with_marked_unit(launch_kind, live)

        self.assertEqual(result, 1, error)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)

    def test_restore_refuses_marked_herdr_when_claude_state_is_unknown(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeUnknown("session registry unreadable")
        )

        self.assertEqual(result, 2)
        self.assertIn(
            "cannot tell whether Claude runs in session marked-herdr: session registry unreadable",
            error,
        )
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)

    def test_restart_restore_of_a_standby_unit_moved_to_another_account(self) -> None:
        directory = self.prepare_unit_account("claude3")
        self.commit_prepared_row(self.unit_row(plan="standby"))
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha", "--standby", "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note), "--session-name", "recorded-name",
            "--host-session", "recorded-herdr", "--account", "claude3",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")

    def test_restart_restore_carries_the_recorded_autocompact_window(self) -> None:
        """A /shutdown restart, account move or rename relaunches through a restore with no flag of its own."""
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        with patch.dict(os.environ, self.env):
            autocompact.record("session-compact", "200k")
        for name, flags, expected in (("compact", (), "200k"), ("reverted", ("--autocompact", "auto"), None)):
            with self.subTest(name=name):
                self.commit_prepared_row(self.unit_row(name))
                _ = self.successful(name, "--plan", "docs/plans/given.md", "--resume", "session-compact",
                                    "--cwd", str(prior), "--restart-note", str(note), "--session-name", name,
                                    "--host-session", f"build-followups-{name}", *flags)
                start = next(row for row in self.herdr_calls()
                             if row[:5] == ["--session", SHARED, "agent", "start", f"build-followups-{name}"])
                arguments = start[start.index("--") + 1:]
                if expected is None:
                    self.assertNotIn("--autocompact", arguments)
                else:
                    self.assertEqual(arguments[arguments.index("--autocompact") + 1], expected)
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-compact"), "")

    def test_registered_unit_autocompact_reaches_its_restore_launch(self) -> None:
        worktree = self.commit_row_off_the_naming_pattern()
        _ = self.set_unit_autocompact("200k")
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        _ = self.successful("trunk", "--plan", "docs/plans/given.md", "--resume", "session-trunk",
                            "--cwd", str(worktree), "--restart-note", str(note), "--session-name", "trunk",
                            "--host-session", "build-followups-trunk")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--autocompact") + 1], "200k")

    def test_recorded_source_fallback_uses_add_unit_in_the_shared_host(self) -> None:
        from test_restart import entry, unit
        import record

        self.commit_prepared_row(self.unit_row())
        directory = self.prepare_unit_account("claude1")
        _ = self.prepare_unit_account("claude2")
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume on the source after the target refused.", encoding="utf-8")
        saved = entry(unit("session-123"))
        saved.update(target="claude1", source_label="claude1")
        flags = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior),
                 "--restart-note", str(note), "--session-name", "recorded-name", "--host-session", "shared-herdr",
                 "--account", "claude1")
        with (patch.object(record, "all_session_records", return_value=[saved]),
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
              patch.object(herdr, "shared_session", return_value="shared-herdr"),
              patch.object(herdr, "start_server") as start,
              patch.object(herdr, "open_tab", return_value="w1:p1") as tab,
              patch.object(herdr, "start_agent", return_value=SUBMITTED_PROMPT) as agent):
            refused = self.cli(*flags)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("units account is claude2", refused.stderr)
            _ = self.successful(*flags, "--source-fallback")
        start.assert_called_once_with("shared-herdr", 90_000)
        environment = cast(dict[str, str], tab.call_args.args[4])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(environment["SHOWRUNNER_UNIT_ID"], "alpha-unit")
        self.assertEqual(environment["SHOWRUNNER_UNIT"], "build-followups")
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[:2], ("shared-herdr", "w1:p1"))

    def test_source_fallback_requires_a_restore_of_its_recorded_source(self) -> None:
        import record

        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume.", encoding="utf-8")
        with (patch.object(record, "all_session_records", return_value=[]),
              patch.object(herdr, "start_agent", side_effect=AssertionError("invalid fallback launched"))):
            new = self.cli("alpha", "--plan", "docs/plans/given.md", "--source-fallback", "--account", "claude2")
            self.assertEqual(new.returncode, 2)
            self.assertIn("requires a recorded unit restore", new.stderr)
            restore = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior),
                "--restart-note", str(note), "--session-name", "recorded-name", "--host-session", "shared-herdr",
                "--account", "claude2", "--source-fallback")
            self.assertEqual(restore.returncode, 2)
            self.assertIn("must name the shutdown record's source account", restore.stderr)

    def test_all_restore_flags_parse_as_a_unit_restore_and_other_launches_do_not(self) -> None:
        note = self.root / "restart note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        common = {
            "production": str(self.doc),
            "name": "alpha",
            "plan": "docs/plans/given.md",
            "brief": None,
            "standby": False,
            "permanent": False,
            "port": None,
            "owns": None,
            "timeout": 90.0,
            "check": False,
            "probe": False,
        }
        restored = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=str(note),
            session_name="recorded name",
            host_session="recorded herdr",
        ))
        ordinary_resume = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=None,
            session_name=None,
            host_session=None,
        ))
        new_work = add_unit.launch_request(argparse.Namespace(
            **common,
            resume=None,
            cwd=None,
            restart_note=None,
            session_name=None,
            host_session=None,
        ))

        self.assertEqual(
            restored.launch_kind,
            add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded name", "recorded herdr"
            ),
        )
        self.assertIsInstance(ordinary_resume.launch_kind, add_unit.NewWorkLaunch)
        self.assertIsInstance(new_work.launch_kind, add_unit.NewWorkLaunch)

    def test_guilds_designer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("designers")

    def test_guilds_reviewer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("reviewers")

    def test_guilds_showrunner_head_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("showrunners")

    def test_guilds_unit_head_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("units")

    def test_restore_flags_are_atomic_and_require_resume(self) -> None:
        note = self.root / "restart-note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        for flags in (
            ("--restart-note", str(note)),
            ("--session-name", "recorded-name"),
            ("--host-session", "recorded-herdr"),
            (
                "--restart-note", str(note),
                "--session-name", "recorded-name",
                "--host-session", "recorded-herdr",
            ),
        ):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.servers_started(), [])

    def test_restore_keeps_claude_name_and_sanitizes_herdr_agent_name(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior session"
        prior.mkdir()
        note = self.root / "restart note.txt"
        restart_note = "Restarted with spaces, 'quotes', and $() unchanged."
        _ = note.write_text(restart_note, encoding="utf-8")
        recorded_session = "restored 'session' $()"
        recorded_host = "build-followups restore $()"
        existing_prompt = "Keep this previously scheduled prompt."
        with patch.dict(os.environ, self.env):
            conversation_pause.record_scheduled_prompts(
                "session-123", (existing_prompt,)
            )

        _ = self.successful(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", recorded_session,
            "--host-session", recorded_host,
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(self.opened()[1], SHARED)
        self.assertEqual(self.opened()[7], str(prior))
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1", recorded_session], self.herdr_calls())
        claude = self.agent_arguments()
        prompt = self.opening_prompt()
        self.assertEqual(claude[claude.index("--remote-control") + 1], recorded_session)
        self.assertEqual(claude[claude.index("-n") + 1], recorded_session)
        self.assertNotIn("claude", claude)
        self.assertIn("You are now alpha-unit in production build-followups", prompt)
        self.assertTrue(prompt.endswith(restart_note), prompt)
        self.assertNotIn("Run /unit:direct", prompt)
        recorded = self.recorded_prompts("session-123")
        self.assertEqual(recorded, (existing_prompt, prompt))
        self.assertIs(conversation_pause.prompt_source(prompt, frozenset, lambda: recorded),
                      conversation_pause.PromptSource.SCHEDULED)
        start, = (row for row in self.herdr_calls() if row[2:4] == ["agent", "start"])
        self.assertEqual(start[4], "build-followups-restore----")

    def test_restore_refuses_launch_when_prompt_cannot_be_recorded(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        _ = self.pause_state.write_text("not a directory", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name",
            "--host-session", "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("add_unit: restart prompt not recorded:", result.stderr)
        self.assertEqual(self.servers_started(), [])

    def test_exact_table_row_is_adopted_without_suffix_in_another_production(self) -> None:
        row = self.unit_row("designers", branch="build-followups-designers",
                            worktree=self.root / "project-designers").replace("| designers-unit |", "| `designers` |")
        self.commit_prepared_row(row)
        request = add_unit.launch_request(argparse.Namespace(
            production=str(self.doc), name="designers", plan="docs/plans/given.md", brief=None,
            standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
            resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
        ))
        self.assertEqual(request.unit, "designers")
        self.assertIsInstance(add_unit.row_is_present(request, self.doc.read_text().splitlines()),
                              add_unit.ExistingUnitRow)

    def test_a_name_ending_in_unit_launches_as_the_name_without_it(self) -> None:
        def request(name: str) -> tuple[str, str, Path, str]:
            launched = add_unit.launch_request(argparse.Namespace(
                production=str(self.doc), name=name, plan="docs/plans/given.md", brief=None,
                standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
                resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
            ))
            return launched.unit, launched.branch, launched.worktree, launched.requested_name

        expected = request("feature-log")
        self.assertEqual(expected[0], "feature-log-unit")
        self.assertEqual(request("feature-log-unit"), expected)
        self.assertEqual(request("feature_log_unit"), request("feature_log"))
        self.assertEqual(request("unit")[0], "unit-unit")


if __name__ == "__main__":
    _ = unittest.main()
