"""Exercise durable design identity with local stores and private pane fixtures."""
from __future__ import annotations

from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import (
    fixture_member, fixture_runtime_id, fixture_unit_location, fixture_session,
    showrunner_relationship, unit_membership,
)

from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import write_registry

import io
import json
import os
import shutil
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from scripts.astra import astra_roles  # noqa: E402
from scripts.accounts import role_map  # noqa: E402
from scripts.agents import worker_lifecycle  # noqa: E402
from scripts.astra import astra_review, astra_work, design_roles  # noqa: E402
from scripts.direct import progress_history, recorder_state
from scripts.astra.design_role_test_support import HeldRolePaneCase, write_empty_binding_record
from scripts.herdr import worker_panes
from scripts.herdr.test_worker_panes import PaneWorld, idle_transcript
from scripts.whoami import agent_accounts  # noqa: E402
from scripts.lib.stop_injection_test_support import InjectedStop, stop_once

Role = Literal["designer", "reviewer"]


def damage_binding_repository(design: design_roles.HeldDesign) -> Path:
    """Plant the historical writer defect in an otherwise valid private record."""
    path = design_roles.design_directory(design.placement.design_id) / "design.json"
    fields = cast(dict[str, object], json.loads(path.read_text()))
    binding = cast(dict[str, object], fields["binding"])
    del binding["repository_owner"]
    binding["repository"] = ""
    _ = path.write_text(json.dumps(fields))
    return path


def setUpModule() -> None:
    install_signal_pid_guard()


@final
class HeldRolePaneTests(HeldRolePaneCase):

    def test_configured_checkout_at_a_unit_path_keeps_independent_ownership(self) -> None:
        parent = self.panes.command_case.fixture.parent
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", self.panes.root),
                               expected=parent.worktree)
        design = self.panes.open("designer")
        self.assertEqual(design.checkout, design_roles.INDEPENDENT_CHECKOUT)
        current_parent = staff.by_id(parent.id)
        assert current_parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", self.panes.root / "moved-parent"),
                               expected=current_parent.worktree)
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(current.checkout, design_roles.INDEPENDENT_CHECKOUT)
        self.assertEqual(current.repository, str(self.panes.root))
        self.assertEqual(current.document, str(self.panes.root / "design.md"))
        assert isinstance(current.binding, design_roles.DesignBoundToPhase)
        self.assertEqual(current.binding.checkout, design_roles.INDEPENDENT_CHECKOUT)
        self.assertEqual(design_roles.binding_repository(current.binding), str(self.panes.root))
        reopened = self.panes.open("designer")
        self.assertEqual(reopened, current)
        self.assertEqual(len(self.panes.world.launches), 1)

    def test_held_role_opens_and_dispatches_when_the_design_is_outside_the_parent_worktree(self) -> None:
        parent = self.panes.command_case.fixture.parent
        worktree = self.panes.root / "parent-checkout"
        worktree.mkdir()
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", worktree), expected=parent.worktree)
        design = self.panes.open("designer")
        reply = self.panes.resume(design, "designer")
        self.assertEqual(design.repository, str(self.panes.root))
        fields = cast(dict[str, object], json.loads(
            (design_roles.design_directory(design.placement.design_id) / "design.json").read_text()))
        self.assertEqual(fields["repository"], str(self.panes.root))
        self.assertEqual(fields["document"], str(self.panes.root / "design.md"))
        self.assertNotIn("repository_owner", fields)
        binding = cast(dict[str, object], fields["binding"])
        self.assertEqual(binding["repository"], str(self.panes.root))
        self.assertNotIn("repository_owner", binding)
        current_parent = staff.by_id(parent.id)
        assert current_parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", self.panes.root / "moved-parent"),
                               expected=current_parent.worktree)
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(current.document, str(self.panes.root / "design.md"))
        self.assertEqual(design_roles.design_repository(current), str(self.panes.root))
        assert isinstance(current.binding, design_roles.DesignBoundToPhase)
        self.assertEqual(design_roles.binding_repository(current.binding), str(self.panes.root))
        self.assertIn("The work is complete.", reply.read_text())
        self.assertEqual(len(self.panes.world.launches), 1)
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
        self.assertEqual(self.panes.world.launch_directories, [worktree])

    def test_submitted_held_review_keeps_the_phase_open_until_the_hook_completes(self) -> None:
        design = self.panes.open("reviewer", bound=False)
        directory = design_roles.design_directory(design.placement.design_id)
        self.panes.command_case.agent.finish_after = 1
        submitted = design_roles.resume(design.placement.design_id + "/reviewer", self.panes.command_case.prompt)
        self.assertIsInstance(submitted, design_roles.SubmittedRoleTurn)
        self.assertEqual(submitted.run, directory)
        self.assertFalse(submitted.reply.exists())
        self.assertFalse(progress_history.reply_completed(submitted.run, submitted.reply))
        self.assertEqual(recorder_state.read_open_phase_ids(directory), ["astra-review"])
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(astra_review.read_review(submitted.reply, submitted.run), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("not completed", errors.getvalue())
        pane = self.panes.command_case.agent.submitted[-1][0]
        self.panes.command_case.agent.complete(pane)
        self.panes.finish(design, "reviewer")
        self.assertEqual(recorder_state.read_open_phase_ids(directory), [], self.panes.command_case.agent.notices)
        self.assertTrue(progress_history.reply_completed(submitted.run, submitted.reply))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(astra_review.read_review(submitted.reply, submitted.run), 0)
        self.assertIn("The work is complete.", output.getvalue())
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)

    def test_rebind_without_context_does_not_report_unrelated_hold_damage(self) -> None:
        design = self.panes.open("designer")
        write_text_atomic(self.panes.root / "holds/unrelated.json", "{")
        with redirect_stderr(io.StringIO()) as errors:
            rebound = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                           reason="publish context", binding=self.panes.binding)
        self.assertEqual(rebound, design)
        self.assertEqual(errors.getvalue(), "")

    def test_another_holder_reuses_dispatches_and_ends_the_recorded_worker(self) -> None:
        parent = self.panes.command_case.fixture.parent
        owner = staff.snapshot().parent_of(parent)
        if owner is None:
            self.fail("fixture has no showrunner")
        other = fixture_member("unit", name="another-holder", parent=owner.id)
        self.panes.world.add(other, "w1:p-other")
        roles: tuple[tuple[Role, bool], ...] = (("designer", True), ("reviewer", False))
        for role, bound in roles:
            with self.subTest(role=role):
                design = self.panes.open(role, bound=bound)
                instance = next(item for item in design.roles if item.role == role)
                if not isinstance(instance.lifecycle, design_roles.RoleOpen):
                    self.fail("role did not publish its worker")
                row = staff.by_id(instance.lifecycle.staff_id)
                self.assertIsNotNone(row)
                self.assertEqual(staff.snapshot().find(kind="worker", parent=parent.id), [row])
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(other.current_session)}):
                    reopened = self.panes.open(role, bound=bound)
                    self.assertEqual(reopened, design)
                    self.assertEqual(staff.snapshot().find(kind="worker", parent=other.id), [])
                    self.assertEqual(len(self.panes.world.launches), len(self.panes.world.closed) + 1)
                    reply = self.panes.resume(reopened, role)
                    self.assertIn("The work is complete.", reply.read_text())
                    ended = design_roles.end(design.placement.design_id + "/" + role)
                    if bound:
                        self.assertEqual(self.panes.recorder_commands, [])
                self.assertIsInstance(next(item for item in ended.roles if item.role == role).lifecycle,
                                      design_roles.RoleEnded)
                self.assertIsNone(staff.by_id(instance.lifecycle.staff_id))
                self.assertEqual(staff.snapshot().find(kind="worker"), [])

    def test_durable_dispatch_records_both_astra_policies_through_the_recorder(self) -> None:
        from scripts.direct import progress_history

        for role in ("designer", "reviewer"):
            with self.subTest(role=role):
                design = self.panes.open(role)
                instance = next(item for item in design.roles if item.role == role)
                assert isinstance(instance.lifecycle, design_roles.RoleOpen)
                row = staff.by_id(instance.lifecycle.staff_id)
                assert row is not None
                member_id = row.id
                command = self.panes.assign

                def assigned(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                    self.assertEqual(staff.by_id(member_id), row)
                    return command(arguments)

                with (patch.object(design_roles, "_assign_command", side_effect=assigned),
                      patch.object(progress_history, "begin_dispatch", wraps=progress_history.begin_dispatch) as dispatch,
                      patch.object(staff, "set_worker_role", wraps=staff.set_worker_role) as writer):
                    _ = self.panes.resume(design, role)
                dispatch.assert_called_once()
                self.assertEqual(dispatch.call_args.kwargs["role"], "astra-" + role)
                writer.assert_not_called()
                saved = staff.by_id(row.id)
                assert saved is not None
                self.assertEqual(saved.role, staff.KnownWorkerRole(cast(staff.WorkerRole, "astra-" + role)))

    def test_held_designer_dispatches_through_worker_and_keeps_one_identity(self) -> None:
        design = self.panes.open("designer")
        instance = design.roles[0]
        self.assertIsInstance(instance.lifecycle, design_roles.RoleOpen)
        if not isinstance(instance.lifecycle, design_roles.RoleOpen):
            self.fail("designer did not publish its worker")
        before = staff.by_id(instance.lifecycle.staff_id)
        assert before is not None
        first = self.panes.resume(design, "designer")
        second = self.panes.resume(design, "designer")
        self.assertIn("The work is complete.", first.read_text())
        self.assertIn("The work is complete.", second.read_text())
        self.assertEqual(staff.by_id(instance.lifecycle.staff_id), replace(before, role=staff.KnownWorkerRole("astra-designer")))
        self.assertEqual(self.panes.world.requests[-1][1][:2], ["resume", str(before.current_session)])
        first_pane, second_pane = [submission[0] for submission in self.panes.command_case.agent.submitted]
        self.assertNotEqual(first_pane, second_pane)
        self.assertEqual(self.panes.command_case.agent.row(first_pane).current_session,
                         self.panes.command_case.agent.row(second_pane).current_session)
        self.assertEqual(len(self.panes.world.launches), 2)
        self.assertEqual([command[command.index("--target") + 1] for command in self.panes.commands],
                         ["durable-design-role"] * 3)
        self.assertEqual([command[command.index("--policy") + 1] for command in self.panes.commands],
                         ["astra-designer"] * 3)
        self.assertEqual(["--new-worker" in command for command in self.panes.commands], [True, False, False])
        self.assertEqual([command[command.index("--to") + 1] for command in self.panes.commands[1:]],
                         [instance.name] * 2)
        self.assertEqual(len(self.panes.command_case.agent.submitted), 2)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id)), 1)

    def test_unbound_showrunner_reviewer_dispatches_in_its_design_folder(self) -> None:
        parent = staff.snapshot().parent_of(self.panes.command_case.fixture.parent)
        if parent is None:
            self.fail("fixture has no showrunner")
        transcript = Path.home() / ".claude/projects/fixture" / f"{parent.current_session}.jsonl"
        write_text_atomic(transcript, json.dumps({"type": "assistant", "message": {"model": "fixture"},
                                                 "effort": "high"}) + "\n")
        with patch.object(staff, "runtime_id", return_value=str(parent.current_session)), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": str(parent.current_session)}):
            self.panes.world.add(parent, "w1:p-showrunner")
            design = self.panes.open("reviewer", bound=False)
            directory = design_roles.design_directory(design.placement.design_id)
            open_during_turn: list[list[str]] = []
            self.panes.command_case.agent.during_prompt = lambda: open_during_turn.append(
                recorder_state.read_open_phase_ids(directory))
            reply = self.panes.resume(design, "reviewer")
            first_row = self.panes.command_case.agent.row(self.panes.command_case.agent.submitted[-1][0])
            self.assertEqual(recorder_state.read_open_phase_ids(directory), [])
            second_reply = self.panes.resume(design, "reviewer")
            self.assertEqual(recorder_state.read_open_phase_ids(directory), [])
        self.assertEqual(open_during_turn, [["astra-review"], ["astra-review"]])
        self.assertEqual([command[0] for command in self.panes.recorder_commands],
                         ["start-run", "start-phase", "finish-phase", "start-phase", "finish-phase"])
        state = worker_lifecycle.read_worker_state(directory / "progress_history_state.json")
        history = Path(cast(str, state["history_file"]))
        events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([event["status"] for event in events if event["event_type"] == "phase_finished"],
                         ["completed", "completed"])
        self.assertEqual(sum(event["event_type"] == "run_started" for event in events), 1)
        self.assertNotIn("status", state)
        self.assertIn("The work is complete.", second_reply.read_text())
        instance = design.roles[1]
        if not isinstance(instance.lifecycle, design_roles.RoleOpen):
            self.fail("reviewer did not publish its worker")
        row = staff.by_id(instance.lifecycle.staff_id)
        self.assertIsNotNone(row)
        if row is None:
            self.fail("reviewer row is unavailable")
        self.assertEqual(row.parent, staff.ReportsTo(parent.id))
        command = self.panes.commands[-1]
        self.assertEqual(command[command.index("--session-dir") + 1],
                         str(design_roles.design_directory(design.placement.design_id)))
        self.assertEqual(command[command.index("--phase") + 1], "astra-review")
        self.assertIn("The work is complete.", reply.read_text())
        self.assertIsInstance(astra_work.read_header(next(reply.parent.parent.glob("prompts/*.md"))), astra_work.AstraWork)
        self.assertEqual(row, first_row)
        self.assertEqual(self.panes.world.requests[-1][1][:2], ["resume", str(first_row.current_session)])
        first_pane, second_pane = [submission[0] for submission in self.panes.command_case.agent.submitted]
        self.assertNotEqual(first_pane, second_pane)
        self.assertEqual(self.panes.command_case.agent.row(first_pane), row)
        self.assertEqual(self.panes.command_case.agent.row(second_pane), row)
        self.assertEqual(len(self.panes.world.launches), 2)
        with stop_once(design_roles, "_recorder_command", when="before"), self.assertRaises(InjectedStop):
            _ = design_roles.end(design.placement.design_id + "/reviewer")
        self.assertNotIn("status", worker_lifecycle.read_worker_state(directory / "progress_history_state.json"))
        ended = design_roles.end(design.placement.design_id + "/reviewer")
        self.assertIsInstance(ended.roles[1].lifecycle, design_roles.RoleEnded)
        self.assertIsNone(staff.by_id(instance.lifecycle.staff_id))
        self.assertEqual(len(self.panes.world.closed), 2)
        finished = worker_lifecycle.read_worker_state(directory / "progress_history_state.json")
        self.assertEqual(finished["status"], "completed")
        events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([event["status"] for event in events if event["event_type"] == "run_finished"],
                         ["completed"])
        saved_history = history.read_bytes()
        _ = design_roles.end(design.placement.design_id + "/reviewer")
        self.assertEqual(history.read_bytes(), saved_history)
        self.assertEqual(worker_lifecycle.read_worker_state(directory / "progress_history_state.json"), finished)

    def test_refused_unbound_review_closes_its_phase_without_a_hook(self) -> None:
        design = self.panes.open("reviewer", bound=False)
        directory = design_roles.design_directory(design.placement.design_id)
        refusal = subprocess.CompletedProcess(["assign.sh"], 2, "", "fixture refuses before submission")
        with patch.object(design_roles, "_assign_command", return_value=refusal), \
                self.assertRaisesRegex(design_roles.RoleRefused, "fixture refuses before submission"):
            _ = self.panes.resume(design, "reviewer")
        self.assertEqual(self.panes.command_case.agent.submitted, [])
        self.assertEqual(recorder_state.read_open_phase_ids(directory), [])
        state = worker_lifecycle.read_worker_state(directory / "progress_history_state.json")
        self.assertNotIn("status", state)
        history = Path(cast(str, state["history_file"]))
        events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([event["status"] for event in events if event["event_type"] == "phase_finished"], ["error"])
        self.assertEqual([command[0] for command in self.panes.recorder_commands],
                         ["start-run", "start-phase", "finish-phase"])

    def test_astra_open_rerun_after_split_reuses_the_same_terminal(self) -> None:
        self.panes.world.interrupt = "split"
        with self.assertRaisesRegex(design_roles.RoleRefused, "interrupted after split"):
            _ = self.panes.open("designer")
        locations = {pane: value["terminal_id"] for pane, value in self.panes.world.locations.items()}
        _ = self.panes.assign(self.panes.commands[-1])
        design_path, = design_roles.state_root().glob("*/design.json")
        _ = design_roles.recover(design_path.parent.name + "/designer")
        opened = self.panes.open("designer")
        repeated = self.panes.open("designer")
        self.assertEqual(repeated, opened)
        self.assertEqual({pane: value["terminal_id"] for pane, value in self.panes.world.locations.items()}, locations)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id)), 1)
        self.assertEqual(self.panes.world.closed, [])

    def assert_held_role_family(self, family: Literal["claude", "codex"],
                                models: tuple[str, str], efforts: tuple[str, str],
                                permission_flag: str, permissions: tuple[str, str]) -> None:
        _ = write_registry(Path(os.environ["MODELS_CONFIG_FILE"]), {
            "assignments": {"design_roles": family},
            "design_roles." + family: dict(zip(("designer", "reviewer"),
                (model + ":" + effort for model, effort in zip(models, efforts, strict=True)), strict=True)),
        })
        designer = self.panes.open("designer")
        reviewer = self.panes.open("reviewer")
        self.assertEqual(designer.placement.design_id, reviewer.placement.design_id)
        for request, model, effort, permission in zip(self.panes.world.requests,
                models, efforts, permissions, strict=True):
            launched_family, arguments, environment = request
            self.assertEqual(launched_family, family)
            self.assertEqual(arguments[arguments.index("--model") + 1], model)
            self.assertEqual(arguments[arguments.index(permission_flag) + 1], permission)
            if family == "codex":
                self.assertIn(f'model_reasoning_effort="{effort}"', arguments)
                self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))
            self.assertNotIn("STAFF_ID", environment)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id)), 2)

    def test_codex_held_roles_use_their_selected_models_and_sandboxes(self) -> None:
        self.assert_held_role_family("codex", ("gpt-test", "gpt-test"), ("medium", "high"),
                                     "--sandbox", ("danger-full-access", "read-only"))

    def test_claude_held_roles_use_the_same_worker_admission_path(self) -> None:
        self.assert_held_role_family("claude", ("sonnet", "opus"), ("high", "high"),
                                     "--permission-mode", ("bypassPermissions", "plan"))

    def test_two_held_roles_use_one_pane_scan(self) -> None:
        _ = self.panes.open("designer")
        _ = self.panes.open("reviewer")
        membership = staff.snapshot()
        with patch.object(self.panes.world, "panes", wraps=self.panes.world.panes) as panes:
            roles = astra_roles.live_roles(membership)
        self.assertCountEqual([role["role"] for role in roles], ["designer", "reviewer"])
        self.assertEqual(panes.call_count, 1)

    def test_durable_dispatch_reuses_the_worker_with_one_start_scan(self) -> None:
        design = self.panes.open("designer")
        instance = design.roles[0]
        if not isinstance(instance.lifecycle, design_roles.RoleOpen):
            self.fail("designer did not publish its worker")
        row = staff.by_id(instance.lifecycle.staff_id)
        assert row is not None
        self.panes.world.reads.clear()
        launches = len(self.panes.world.launches)
        before_prompt: list[tuple[str, ...]] = []
        self.panes.command_case.agent.during_prompt = lambda: before_prompt.extend(self.panes.world.reads)
        reply = self.panes.root / "dispatch-review.md"
        result = self.panes.assign([
            "--role", "implement", "--target", "durable-design-role", "--policy", "astra-designer",
            "--parent", str(self.panes.command_case.fixture.parent.id), "--to", instance.name,
            "--session-dir", str(self.panes.command_case.run_dir), "--phase", "8",
            "--prompt", str(self.panes.command_case.prompt), "--reply-file", str(reply),
            "--cwd", str(self.panes.root),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.panes.finish(design, "designer")
        self.assertIn("The work is complete.", reply.read_text())
        self.assertEqual(staff.by_id(instance.lifecycle.staff_id), replace(row, role=staff.KnownWorkerRole("astra-designer")))
        self.assertEqual(len(self.panes.world.launches), launches)
        self.assertEqual(sum(request[:2] == ("agent", "list") for request in before_prompt), 1)
        inventory = next(index for index, request in enumerate(before_prompt) if request[:2] == ("agent", "list"))
        self.assertEqual(sum(request[:2] == ("pane", "list") for request in before_prompt[:inventory]), 1)
        # Startup, layout and prompt delivery each read the pane list once.
        self.assertEqual(sum(request[:2] == ("pane", "list") for request in before_prompt), 3)

    def test_census_keeps_unknown_worker_evidence_and_the_known_sibling(self) -> None:
        design = self.panes.open("designer")
        design = self.panes.open("reviewer")
        record = staff.snapshot()
        reviewer = cast(design_roles.RoleOpen, design.roles[1].lifecycle)
        designer = cast(design_roles.RoleOpen, design.roles[0].lifecycle)
        for structural in (False, True):
            with self.subTest(structural=structural):
                if structural:
                    self.panes.world.unknown.clear()
                    self.panes.world.locations["w1:p1"]["terminal_id"] = ""
                else:
                    self.panes.world.unknown["w1:p1"] = "native process evidence is unavailable"
                with redirect_stderr(io.StringIO()) as errors:
                    live = astra_roles.live_roles(record)
                states = {item["staff_id"]: item for item in live}
                self.assertEqual(states[designer.staff_id]["state"], "unknown")
                self.assertIn("terminal identity" if structural else "native process evidence is unavailable",
                              errors.getvalue())
                self.assertEqual(states[reviewer.staff_id]["state"], "idle")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id)), 2)
                self.assertEqual(self.panes.world.closed, [])


@final
class DurableRolesTests(HeldRolePaneCase):
    root: Path = Path()
    repository: Path = Path()
    run_a: Path = Path()
    run_b: Path = Path()
    launches: list[tuple[str, str]] = []
    dispatches: list[tuple[str, str, str]] = []
    holder_session: str = '17759e5d-1377-577b-b283-19888bbfc1f5'
    lose_creation_ack: bool = False
    fail_dispatch: bool = False

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = self.panes.root
        from scripts.production.staff_test_support import production_membership, recorded_showrunner
        membership = self.panes.command_case.fixture
        document = membership.production("demo", "design-unit")
        _ = recorded_showrunner("demo", document)
        production_membership("demo", "design")
        unit = unit_membership("demo", "design")
        _ = staff.update_current_session(unit.current_session, staff.native_id(self.holder_session))
        self.repository = self.root / "checkout"
        self.repository.mkdir()
        _ = document.write_text(document.read_text()
            + f"- **Merge branch:** main\n- **Showrunner checkout:** {self.repository}\n- **Log:** log.md\n")
        _ = (self.repository / "design.md").write_text("# Design\n")
        self.launches, self.dispatches = [], []
        self.holder_session, self.lose_creation_ack, self.fail_dispatch = '17759e5d-1377-577b-b283-19888bbfc1f5', False, False
        unit = staff.by_path("demo/design")
        assert unit is not None
        _ = staff.update_current_session(unit.current_session, staff.native_id(self.holder_session))
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", self.repository), expected=unit.worktree)
        config = self.root / "models.conf"
        _ = write_registry(config, {'assignments': {'design_roles': 'codex'}, 'design_roles.codex': {'designer': 'gpt-fixture:xhigh', 'reviewer': 'gpt-review-fixture:high'}, 'codex.agents': {'gpt-fixture': 'xhigh', 'gpt-review-fixture': 'high'}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "DESIGN_ROLES_STATE": str(self.root / "roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "ownership"), "DESIGN_ROLES_HOST": "natedev",
            "ASTRA_ROLES_STATE": str(self.root / "holds"), "CODEX_THREAD_ID": self.holder_session,
            "USAGE_STATE_DIR": str(self.root / "usage"),
            "MODELS_CONFIG_FILE": str(config),
        })))
        _ = self.enterContext(patch.object(design_roles, "authority_request", side_effect=self.authority))
        _ = self.enterContext(patch.object(design_roles, "dispatch_turn", side_effect=self.dispatch))
        _ = self.enterContext(patch.object(role_map, "read_role_map", return_value=role_map.RoleMap({}, "codex1")))
        _ = self.enterContext(patch.object(agent_accounts, "codex_account_home", return_value=
            agent_accounts.CodexHomeHoldingLogin("codex1", self.root / "codex-home", "fixture@example.invalid")))
        design_roles.register_repository("fixture-repo", {"natedev": design_roles.UnitCheckout(unit.id),
                                                    "macbook": str(self.root / "mac-checkout")})
        self.run_a = self.phase("run-a", "instance-a")
        self.run_b = self.phase("run-b", "instance-b")
        self.panes.world.locations.clear()
        self.panes.world.foreground.clear()
        self.panes.world.layouts.clear()
        parent = staff.by_path("demo/design")
        assert parent is not None
        self.panes.command_case.fixture.parent_id = parent.id
        self.panes.world.add(parent, "w1:p0", family="codex")
        fixture_session(parent.current_session, staff.CodexBackend(self.root / "codex-home"))
        activate = self.panes.world.activate

        def native_start(identity: str, pane: str, family: Literal["claude", "codex"]) -> None:
            label = str(self.panes.world.locations[pane]["label"])
            role: Role = "reviewer" if "reviewer" in label else "designer"
            membership = staff.snapshot()
            if any(label.startswith(worker_panes.agent_name(owner, "astra-"))
                   for owner in membership.rows if owner.kind in ("unit", "showrunner")):
                existing = membership.by_current_session(identity)
                if existing is None:
                    identity = fixture_runtime_id("astra-" + role if not any(
                        item[0] == role for item in self.launches) else label)
                    self.launches.append((role, identity))
                fixture_session(identity, staff.CodexBackend(self.root / "codex-home"))
            activate(identity, pane, family)

        _ = self.enterContext(patch.object(self.panes.world, "activate", side_effect=native_start))
        _ = self.enterContext(patch.object(design_roles, "_assign_command", side_effect=self.assign_command))

    def test_herdr_role_start_keeps_its_creation_claim_when_pane_evidence_is_unknown(self) -> None:
        self.panes.world.unreadable = True
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        with self.assertRaisesRegex(design_roles.RoleRefused, "parent pane is unavailable or duplicated"):
            _ = self.open_designer()
        self.assertEqual(self.launches, [])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        designs = list(design_roles.state_root().glob("*/design.json"))
        self.assertEqual(len(designs), 1)
        lifecycle = design_roles.read_design(designs[0].parent.name).roles[0].lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleNotOpened)
        assert isinstance(lifecycle, design_roles.RoleNotOpened)
        self.assertEqual(lifecycle.claim, "pane-start")


    def test_worker_state_accepts_holder_free_design_and_checks_its_structure(self) -> None:
        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / "design.json"
        fields = cast(dict[str, object], json.loads(path.read_text()))
        self.assertNotIn("holder", fields)
        for instance in cast(list[dict[str, object]], fields["roles"]):
            lifecycle = cast(dict[str, object], instance["lifecycle"])
            if lifecycle.get("staff_id"):
                self.assertNotIn("name", instance)
            else:
                role = cast(Role, instance["role"])
                self.assertEqual(instance["name"], self.instance(design, role).name)
        self.assertIs(worker_lifecycle.validate_worker_state(path, fields), fields)
        invalid: tuple[tuple[str, object], ...] = (
            ("placement", None), ("placement", []), ("binding", None), ("binding", []),
            ("roles", None), ("roles", [None]), ("roles", [{"lifecycle": None}]),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(worker_lifecycle.WorkerStateUnreadable) as error:
                    _ = worker_lifecycle.validate_worker_state(path, {**fields, field: value})
                self.assertIn(str(path), str(error.exception))

    def test_rebind_rerun_after_generation_save_finishes_context_publication(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.rebind(identifier, expected_generation=1, reason="next phase",
                                    binding=self.binding(self.run_b))
        retried = design_roles.rebind(identifier, expected_generation=1, reason="next phase",
                                      binding=self.binding(self.run_b))
        self.assertEqual(retried.binding, self.binding(self.run_b))
        self.assertEqual(retried.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(retried, "designer")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.dispatches[-1][1], self.launches[0][1])

    def test_rebind_rerun_with_current_binding_finishes_the_committed_generation(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        path = self.repository / "next-design.md"
        _ = path.write_text("# Next design\n")
        document = str(path)
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.rebind(identifier, expected_generation=1, reason="next document", document=document)
        retried = design_roles.rebind(identifier, expected_generation=1, reason="next document", document=document)
        self.assertEqual(retried.binding, design.binding)
        self.assertEqual(retried.document, document)
        self.assertEqual(retried.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(retried, "designer")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.dispatches[-1][1], self.launches[0][1])

    def test_checkout_move_rerun_after_generation_save_finishes_routes(self) -> None:
        design = self.open_designer()
        destination = self.root / "moved-checkout"
        _ = self.repository.rename(destination)
        identifier = design.placement.design_id
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        moved = design_roles.read_design(identifier)
        self.assertEqual(moved.repository, str(destination))
        self.assertEqual(moved.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(moved, "designer")
        self.assertEqual(len(self.launches), 1)

    def test_checkout_move_before_save_resumes_its_transition_after_staff_moves(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        directory = design_roles.design_directory(identifier)
        transition = directory / "transition.json"
        destination = self.root / "move-before-save"
        _ = self.repository.rename(destination)
        with stop_once(design_roles, "_save", when="before"), self.assertRaises(InjectedStop):
            design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        self.assertTrue(transition.exists())
        self.assertEqual(design_roles.read_design(identifier).placement.generation, 1)
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        moved = design_roles.read_design(identifier)
        self.assertEqual(moved.placement.generation, 2)
        self.assertFalse(transition.exists())
        self.assertEqual(moved.checkout, design_roles.UnitCheckout(parent.id))
        self.assertEqual(moved.repository, str(destination))
        self.assertEqual(moved.roles, design.roles)
        before = (directory / "design.json").read_bytes()
        design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        self.assertEqual((directory / "design.json").read_bytes(), before)
        self.assertEqual(len(self.launches), 1)

    def test_path_only_registration_at_a_unit_path_stays_independent(self) -> None:
        design_roles.register_repository("independent-path", {"natedev": str(self.repository)})
        placement = self.authority("reserve", {"repository_id": "independent-path", "host": "natedev",
                                               "document": "design.md", "request_id": "path-only"})
        self.assertNotIn("repository_owner", placement)
        self.assertEqual(placement["checkout"], str(self.repository))
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", self.root / "another-unit-path"),
                               expected=parent.worktree)
        resolved = self.authority("resolve", {"repository_id": "independent-path", "host": "natedev",
                                              "document": "design.md"})
        self.assertEqual(resolved, placement)

    def test_cli_registers_an_explicit_unit_owner(self) -> None:
        parent = staff.by_path("demo/design")
        assert parent is not None
        self.assertEqual(design_roles.main(["register", "--repository", "explicit-unit", "--host", "natedev",
                                          "--repository-owner", parent.id]), 0)
        registry = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "ownership/repositories.json").read_text()))
        self.assertEqual(registry["explicit-unit"]["checkouts"], {"natedev": {"repository_owner": parent.id}})
        destination = self.root / "explicit-owner-moved"
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        placement = self.authority("reserve", {"repository_id": "explicit-unit", "host": "natedev",
                                               "document": "design.md", "request_id": "explicit-owner"})
        self.assertEqual(placement["repository_owner"], parent.id)
        self.assertNotIn("checkout", placement)

    def authority(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        return design_roles._authority_local(operation, payload)  # pyright: ignore[reportPrivateUsage]


    def phase(self, name: str, instance: str) -> Path:
        directory = self.root / name
        phase = directory / "phases/1"
        phase.mkdir(parents=True)
        owner = fixture_unit_location(showrunner_relationship("demo"), "design", self.repository)
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "1"}))
        _ = (phase / "files").write_text("design.md\n")
        _ = (directory / "progress_history_state.json").write_text(json.dumps({
            "run_id": name, "owner_staff_id": owner.id, "history_file": str(self.root / (name + ".jsonl")),
            "phase": {"id": "1", "instance_id": instance, "status": "open"},
            "open_phases": [{"id": "1", "instance_id": instance, "status": "open"}],
        }))
        return directory

    def binding(self, directory: Path) -> design_roles.DesignBoundToPhase:
        parent = staff.by_path("demo/design")
        assert parent is not None
        return design_roles.DesignBoundToPhase(str(directory), "1", str(self.repository), ("design.md",),
                                               checkout=design_roles.UnitCheckout(parent.id))

    def instance(self, design: design_roles.HeldDesign, role: Role) -> design_roles.RoleInstance:
        return next(instance for instance in design.roles if instance.role == role)

    def assign_command(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        result = self.panes.assign(arguments)
        if "--new-worker" in arguments and result.returncode == 0:
            for row in staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id):
                for pane, (pid, family, _) in self.panes.world.foreground.items():
                    if self.panes.world.processes[pid].identity.id == row.current_session:
                        self.panes.world.record(row, pane, cast(Literal["claude", "codex"], family))
                idle_transcript(row)
            if self.lose_creation_ack:
                self.lose_creation_ack = False
                raise TimeoutError("creation acknowledgement lost")
        return result

    def dispatch(self, design: design_roles.HeldDesign, role: Role, prompt: Path, reply: Path) -> design_roles.SubmittedRoleTurn:
        lifecycle = self.instance(design, role).lifecycle
        self.assertIsInstance(lifecycle, (design_roles.RoleOpen, design_roles.RolePausedForMove))
        thread = cast(design_roles.RoleOpen, lifecycle).thread_id
        self.dispatches.append((role, thread, prompt.read_text()))
        if self.fail_dispatch:
            raise design_roles.RoleRefused("turn acceptance unknown")
        write_text_atomic(reply, "Saved reply from " + thread)
        run = Path(design.binding.run_directory) if isinstance(design.binding, design_roles.DesignBoundToPhase) else design_roles.design_directory(design.placement.design_id)
        return design_roles.SubmittedRoleTurn(reply, run)

    def open_designer(self, *, document: str = "design.md", request_id: str = "open-one") -> design_roles.HeldDesign:
        return design_roles.open_role("fixture-repo", document, "designer", "design", binding=self.binding(self.run_a), request_id=request_id)

    def resume(self, design: design_roles.HeldDesign, role: Role, text: str = "Revise with cited evidence.") -> Path:
        prompt = self.root / "assignment.md"
        _ = prompt.write_text(text)
        return design_roles.resume(design.placement.design_id + "/" + role, prompt).reply

    def pane_start(self) -> PaneWorld:
        return self.panes.world

    def test_pane_open_rerun_before_unknown_save_creates_one_worker(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_begin_creation", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "not_started"))
        opened = self.open_designer()
        self.assertIsInstance(self.instance(opened, "designer").lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual([command[command.index("--new-worker") + 1] for command in self.panes.commands],
                         [self.instance(opened, "designer").name])

    def test_pane_open_rerun_after_add_recovers_the_idle_worker_without_another_add(self) -> None:
        self.recover_after_add(close_pane=False)

    def test_pane_open_rerun_after_add_recovers_the_closed_worker_without_another_add(self) -> None:
        self.recover_after_add(close_pane=True)

    def recover_after_add(self, *, close_pane: bool) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_assign_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        if close_pane:
            world.remove_pane("w1:p1")
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        parent = staff.by_path("demo/design")
        assert parent is not None
        row = worker_admission.find_worker(staff.snapshot(), parent.id, self.instance(saved, "designer").name)
        assert row is not None
        recovered = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(self.instance(recovered, "designer").lifecycle, design_roles.RoleOpen(row.id))
        self.assertEqual(self.open_designer(), recovered)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(sum("--new-worker" in command for command in self.panes.commands), 1)
        self.assertTrue((self.root / "holds" / (row.id + ".json")).exists())

    def test_pane_open_rerun_without_worker_evidence_retains_the_unknown_claim(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_assign_command", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        with self.assertRaisesRegex(design_roles.RoleRefused, "no recorded worker; replacement refused"):
            _ = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(design_roles.read_design(saved.placement.design_id), saved)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        self.assertEqual(self.panes.commands, [])
        self.assertEqual(world.launches, [])

    def test_pane_open_rerun_with_unknown_process_evidence_retains_the_claim(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_assign_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        world.unknown["w1:p1"] = "worker process evidence is unreadable"
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        with self.assertRaisesRegex(design_roles.RoleRefused, "process evidence is unavailable; worker is retained"):
            _ = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(design_roles.read_design(saved.placement.design_id), saved)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(sum("--new-worker" in command for command in self.panes.commands), 1)

    def test_pane_end_clears_unknown_creation_before_the_worker_command(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_assign_command", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        identifier = path.parent.name + "/designer"
        with self.assertRaises(design_roles.RoleRefused) as refused:
            _ = design_roles.recover(identifier)
        ended = design_roles.end(identifier)
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertEqual(design_roles.end(identifier), ended)
        self.assertIn("no recorded worker; replacement refused", str(refused.exception))
        self.assertIn("end", str(refused.exception))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(self.panes.commands, [["end", self.instance(ended, "designer").name,
                                                "--session-dir", str(self.run_a)]])
        self.assertEqual(world.launches, [])

    def test_end_unknown_creation_refuses_another_roles_creation_claim(self) -> None:
        with stop_once(design_roles, "_assign_command", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with stop_once(design_roles, "_assign_command", when="before"), self.assertRaises(InjectedStop):
            _ = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                       binding=self.binding(self.run_a))
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        with self.assertRaisesRegex(design_roles.RoleRefused, "role is busy; an assignment claim is active"):
            _ = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(design_roles.read_design(path.parent.name), saved)
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(self.launches, [])

    def test_pane_end_unknown_creation_finishes_the_existing_worker_cleanup(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_assign_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        parent = staff.by_path("demo/design")
        assert parent is not None
        row = worker_admission.find_worker(staff.snapshot(), parent.id, self.instance(saved, "designer").name)
        assert row is not None
        _ = world.foreground.pop("w1:p1")
        with patch.object(world, "close_terminal_pane", return_value=False):
            with self.assertRaisesRegex(design_roles.RoleRefused, "pane close is incomplete"):
                _ = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(design_roles.read_design(path.parent.name), saved)
        self.assertIsNotNone(staff.by_id(row.id))
        ended = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(row.id))
        self.assertEqual(design_roles.end(path.parent.name + "/designer"), ended)
        self.assertIsNone(staff.by_id(row.id))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(len(world.launches), 1)

    def test_pane_rebind_rerun_before_transition_clear_finishes_publication(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        document = self.repository / "next-design.md"
        _ = document.write_text("# Next design\n")

        def rebind() -> design_roles.HeldDesign:
            return design_roles.rebind(identifier, expected_generation=1,
                                       reason="next document", document=str(document))

        with (stop_once(Path, "unlink", when="before", matches=lambda path: path == transition),
              self.assertRaises(InjectedStop)):
            _ = rebind()
        self.assertTrue(transition.exists())
        saved = design_roles.read_design(identifier)
        rebound = rebind()
        self.assertEqual(rebound, saved)
        self.assertEqual(rebound.document, str(document))
        self.assertEqual(rebound.placement.generation, 2)
        self.assertEqual(rebound.roles, design.roles)
        self.assertFalse(transition.exists())
        self.assertEqual(len(world.launches), 1)

    def test_pane_end_rerun_after_transport_failure_keeps_then_finishes_its_transition(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        _ = world.foreground.pop("w1:p1")
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        lifecycle = self.instance(design, "designer").lifecycle
        assert isinstance(lifecycle, design_roles.RoleOpen)
        with patch.object(world, "close_terminal_pane", return_value=False):
            with self.assertRaisesRegex(design_roles.RoleRefused, "pane close is incomplete"):
                _ = design_roles.end(identifier + "/designer")
        self.assertTrue(transition.exists())
        self.assertEqual(design_roles.read_design(identifier), design)
        self.assertIsNotNone(staff.by_id(lifecycle.staff_id))
        ended = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(lifecycle.staff_id))
        self.assertFalse(transition.exists())
        self.assertIsNone(staff.by_id(lifecycle.staff_id))
        self.assertFalse((self.root / "holds" / (lifecycle.staff_id + ".json")).exists())

    def test_pane_end_rerun_after_terminal_save_finishes_census_and_staff_cleanup(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        _ = world.foreground.pop("w1:p1")
        identifier = design.placement.design_id
        lifecycle = self.instance(design, "designer").lifecycle
        assert isinstance(lifecycle, design_roles.RoleOpen)
        transition = design_roles.design_directory(identifier) / "transition.json"
        census = self.root / "holds" / (lifecycle.staff_id + ".json")
        with stop_once(astra_roles, "release", when="before"), self.assertRaises(InjectedStop):
            _ = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(design_roles.read_design(identifier), "designer").lifecycle,
                         design_roles.RoleEnded(lifecycle.staff_id))
        self.assertTrue(transition.exists())
        self.assertTrue(census.exists())
        ended = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(lifecycle.staff_id))
        self.assertFalse(transition.exists())
        self.assertFalse(census.exists())
        self.assertIsNone(staff.by_id(lifecycle.staff_id))
        self.assertEqual(sum(command[0] == "end" for command in self.panes.commands), 1)
        self.assertEqual(len(world.closed), 1)

    def test_pane_checkout_move_rerun_before_transition_clear_keeps_generation_and_thread(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        destination = self.root / "moved-checkout"
        _ = self.repository.rename(destination)

        def move() -> None:
            design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")

        with (stop_once(Path, "unlink", when="before", matches=lambda path: path == transition),
              self.assertRaises(InjectedStop)):
            move()
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        saved = design_roles.read_design(identifier)
        self.assertTrue(transition.exists())
        move()
        moved = design_roles.read_design(identifier)
        self.assertEqual(moved, saved)
        self.assertEqual(moved.repository, str(destination))
        self.assertEqual(moved.placement.generation, 2)
        self.assertEqual(moved.roles, design.roles)
        self.assertFalse(transition.exists())
        self.assertEqual(len(world.launches), 1)


    def test_busy_sibling_does_not_stop_selected_role_dispatch(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                        binding=self.binding(self.run_a))
        world = self.panes.world
        world.locations["w1:p2"]["agent_status"] = "working"
        reply = self.resume(design, "designer")
        self.assertEqual(reply.read_text(), "Saved reply from " + self.launches[0][1])
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(self.instance(current, "reviewer"), self.instance(design, "reviewer"))
        self.assertEqual([role for role, _, _ in self.dispatches], ["designer"])

    def test_unknown_sibling_does_not_stop_selected_role_rebind(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                        binding=self.binding(self.run_a))
        world = self.panes.world
        world.unknown["w1:p2"] = "sibling process evidence is unreadable"
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                      reason="next phase", binding=self.binding(self.run_b))
        self.assertEqual(design_roles.read_design(design.placement.design_id).binding, self.binding(self.run_b))
        self.assertEqual(rebound.roles, design.roles)
        self.assertEqual(world.stops, [])
        self.assertEqual(world.closed, [])

    def test_astra_role_publishes_the_returned_thread_as_its_staff_identity(self) -> None:
        self.check_native_role("demo/design")

    def test_showrunner_held_astra_role_keeps_its_worker_row_and_backend(self) -> None:
        self.check_native_role("demo")

    def check_native_role(self, parent_path: str) -> None:
        parent = staff.by_path(parent_path)
        assert parent is not None
        os.environ["CODEX_THREAD_ID"] = parent.current_session
        self.holder_session = parent.current_session
        if parent_path == "demo":
            self.panes.command_directory = self.repository
            self.panes.world.add(parent, "w1:p-holder", family="codex", tab="w1:t-holder")
        design = self.open_designer()
        instance = self.instance(design, "designer")
        self.assertIsInstance(instance.lifecycle, design_roles.RoleOpen)
        assert isinstance(instance.lifecycle, design_roles.RoleOpen)
        row = staff.by_id(instance.lifecycle.staff_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row.id, row.name), (fixture_runtime_id("astra-designer"), instance.name))
        self.assertEqual(row.parent, staff.ReportsTo(parent.id))
        self.assertEqual(staff.snapshot().backends[row.id], staff.CodexBackend(self.root / "codex-home"))
        self.assertEqual(len(self.panes.world.launches), 1)
        command = self.panes.commands[-1]
        self.assertEqual(command[command.index("--target") + 1], "durable-design-role")
        self.assertEqual(command[command.index("--policy") + 1], "astra-designer")
        self.assertEqual(command[command.index("--new-worker") + 1], instance.name)
        self.assertEqual(self.panes.command_case.agent.submitted, [])
        self.assertEqual(self.dispatches, [])

    def test_a_phase_binding_without_a_run_owner_names_the_run_directory(self) -> None:
        from scripts.direct import phase_reservation

        path = self.run_a / "progress_history_state.json"
        state = cast(dict[str, object], json.loads(path.read_text()))
        _ = state.pop("owner_staff_id")
        _ = path.write_text(json.dumps(state))
        for contents in (None, "", "\n"):
            with self.subTest(director=contents):
                if contents is not None:
                    _ = (self.run_a / "director").write_text(contents)
                with self.assertRaises(phase_reservation.StateRefused) as refusal:
                    _ = self.open_designer()
                self.assertEqual(str(refusal.exception), f"{self.run_a}: run owner is unavailable")
                self.assertEqual(self.launches, [])

    def test_staff_refusal_retains_the_creation_claim_and_launches_no_astra_role(self) -> None:
        caller = self.panes.command_case.fixture.worker("caller")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(caller.current_session)}):
            with self.assertRaisesRegex(design_roles.RoleRefused,
                                        "staff: caller: not opened: caller is not a unit or showrunner"):
                _ = self.open_designer()
        self.assertEqual(self.launches, [])
        self.assertTrue(all(isinstance(role.lifecycle, design_roles.RoleNotOpened) and role.lifecycle.claim == "pane-start"
                            for path in design_roles.state_root().glob("*/design.json")
                            for role in design_roles.read_design(path.parent.name).roles if role.role == "designer"))


    def test_design_ownership_read_names_damaged_state_instead_of_decoding_it_as_empty(self) -> None:
        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / "design.json"
        for damaged in ("", '{"placement":', "not JSON", "[]", '{"roles": [42]}'):
            with self.subTest(damaged=damaged):
                _ = path.write_text(damaged)
                with self.assertRaises(Exception) as refused:
                    _ = design_roles.read_design(design.placement.design_id)
                self.assertEqual(type(refused.exception).__name__, "WorkerStateUnreadable")
                self.assertIn(str(path), str(refused.exception))
                self.assertEqual(path.read_text(), damaged)

    def test_old_repository_record_opens_and_retries_keep_one_pair_and_thread(self) -> None:
        store = self.root / "ownership/repositories.json"
        record = {"repository_id": "fixture-repo", "checkouts": {"natedev": str(self.repository)}}
        _ = store.write_text(json.dumps({"fixture-repo": record}))
        design = self.open_designer()
        for complete in (False, True):
            _ = store.write_text(json.dumps({"fixture-repo": {**record, "inventory_complete": complete}}))
            self.assertEqual(self.open_designer(), design)
        self.assertIn('"inventory_complete": true', store.read_text())
        retried = self.open_designer(document=str(self.repository / "sub/../design.md"), request_id="retry")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(self.launches, [("designer", "9309c903-5894-501a-8aa7-f33ea912e074")])
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_cli_open_without_checkout_saves_the_resolved_binding_repository(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = design_roles.main(['open', '--repository', 'fixture-repo', '--document', 'design.md',
                '--role', 'designer', '--area', 'design',
                '--run-directory', str(self.run_a), '--phase', '1', '--output-path', 'design.md'])
        self.assertEqual(code, 0, output.getvalue())
        design_id = output.getvalue().strip().removesuffix('/designer')
        design = design_roles.read_design(design_id)
        self.assertEqual(design.binding, self.binding(self.run_a))
        self.assertEqual(self.instance(design, 'designer').lifecycle, design_roles.RoleOpen(staff.StaffId('9309c903-5894-501a-8aa7-f33ea912e074')))

    def test_save_refuses_an_empty_binding_repository_without_changing_the_record(self) -> None:
        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / 'design.json'
        before = path.read_bytes()
        with self.assertRaisesRegex(design_roles.RoleRefused, 'repository'):
            design_roles._save(replace(design, binding=replace(self.binding(self.run_a), repository='', checkout=design_roles.IndependentCheckout())))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(path.read_bytes(), before)

    def test_sweeps_skip_an_unrelated_bad_binding_and_keep_processing_valid_roles(self) -> None:

        bad = write_empty_binding_record(self.repository)
        design = self.open_designer()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(design_roles.held_document(Path(design.document)), design)
            design_roles.unbind_run(str(self.run_a))
        unbound = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(unbound.binding, design_roles.DesignUnbound)
        self.assertEqual(unbound.roles, design.roles)
        self.assertEqual(errors.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'] * 2)
        self.assertTrue(bad.is_file())


    def test_sweeps_refuse_a_matching_bad_record_with_a_live_role(self) -> None:

        operations: tuple[Callable[[], object], ...] = (
            lambda: design_roles.unbind_run(str(self.run_a)),
            lambda: design_roles.held_document(self.repository / 'broken-design.md'))
        for lifecycle in (design_roles.RoleOpen(staff.StaffId('held-thread')), design_roles.RolePausedForMove(staff.StaffId('held-thread')),
                          design_roles.RoleNeedsRecovery(staff.StaffId('held-thread'), 'lost acknowledgement')):
            for operation in operations:
                with self.subTest(lifecycle=lifecycle, operation=operation):
                    bad = write_empty_binding_record(self.repository, lifecycle=lifecycle,
                                                     run_directory=str(self.run_a))
                    before = bad.read_bytes()
                    with self.assertRaisesRegex(design_roles.RoleRefused, 'live held role needs recovery'):
                        _ = operation()
                    self.assertEqual(bad.read_bytes(), before)
                    bad.unlink()

    def test_run_cleanup_skips_another_runs_damaged_record_and_a_retry_writes_nothing(self) -> None:
        design = self.open_designer()
        bad = write_empty_binding_record(self.repository,
                                        lifecycle=design_roles.RoleOpen(staff.StaffId('other-thread')), run_directory=str(self.run_b))
        for attempt in range(2):
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob('*.json')}
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = design_roles.main(['unbind-run', str(self.run_a)])
            self.assertEqual(code, 0, error.getvalue())
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(error.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'])
            current = design_roles.read_design(design.placement.design_id)
            self.assertIsInstance(current.binding, design_roles.DesignUnbound)
            self.assertEqual(current.placement.generation, 2)
            self.assertEqual(current.roles, design.roles)
            self.assertEqual((bad.read_bytes(), bad.stat().st_mtime_ns), before[bad])
            if attempt:
                self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob('*.json')}, before)
        self.assertEqual(self.panes.world.stops, [])
        self.assertEqual(self.panes.world.closed, [])

    def test_run_cleanup_refuses_matching_damage_without_a_live_role(self) -> None:
        for lifecycle in (design_roles.RoleNotOpened(), design_roles.RoleEnded(staff.StaffId('ended-thread'))):
            with self.subTest(lifecycle=lifecycle):
                bad = write_empty_binding_record(self.repository,
                                                lifecycle=lifecycle, run_directory=str(self.run_a))
                before = bad.read_bytes(), bad.stat().st_mtime_ns
                error = io.StringIO()
                with redirect_stderr(error):
                    code = design_roles.main(['unbind-run', str(self.run_a)])
                self.assertEqual(code, 2)
                self.assertEqual(error.getvalue().splitlines(), [f'design_roles: {bad}: missing or invalid repository'])
                self.assertEqual((bad.read_bytes(), bad.stat().st_mtime_ns), before)
                bad.unlink()
        self.assertEqual(self.panes.world.closed, [])

    def test_document_lookup_warns_once_for_invalid_json_and_keeps_the_valid_design(self) -> None:
        design = self.open_designer()
        bad = write_empty_binding_record(self.repository)
        _ = bad.write_text('{"holder":\n')
        errors = io.StringIO()
        with redirect_stderr(errors):
            found = design_roles.held_document(Path(design.document))
        self.assertEqual(found, design)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(f'warning: {bad}: ', errors.getvalue())

    def test_holder_checks_skip_bad_unrelated_records_and_keep_valid_failures(self) -> None:

        bad = write_empty_binding_record(self.repository, lifecycle=design_roles.RoleOpen(staff.StaffId('unrelated-thread')))
        design = self.open_designer()
        report = design_roles.list_roles()
        entries = cast(list[dict[str, object]], report['roles'])
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry['design_id'] for entry in entries}, {design.placement.design_id})
        self.assertIn(str(bad), str(report['problems']))
        self.assertTrue(all('holder' not in entry and 'holder_reach' not in entry for entry in entries))


    def test_holder_checks_refuse_only_matching_bad_live_roles(self) -> None:

        for lifecycle in (design_roles.RoleNotOpened(), design_roles.RoleEnded(staff.StaffId('ended-thread')),
                          design_roles.RoleOpen(staff.StaffId('held-thread')), design_roles.RolePausedForMove(staff.StaffId('held-thread')),
                          design_roles.RoleNeedsRecovery(staff.StaffId('held-thread'), 'lost acknowledgement')):
            with self.subTest(lifecycle=lifecycle):
                bad = write_empty_binding_record(self.repository, lifecycle=lifecycle)
                before = bad.read_bytes()
                report = design_roles.list_roles()
                self.assertIn(str(bad), str(report['problems']))
                self.assertEqual(bad.read_bytes(), before)
                bad.unlink()


    def test_record_operation_warns_once_across_holders_and_warns_again_on_retry(self) -> None:

        bad = write_empty_binding_record(self.repository)
        design = self.open_designer()
        for _attempt in range(2):
            errors = io.StringIO()
            with redirect_stderr(errors), design_roles.record_operation():
                with design_roles.record_operation():
                    for _ in range(3):
                        self.assertEqual(design_roles.held_document(Path(design.document)), design)
            self.assertEqual(errors.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'])
        self.assertEqual(design_roles.read_design(design.placement.design_id), design)


    def test_unregistered_repository_refusal_names_the_register_command(self) -> None:
        store = self.root / "ownership/repositories.json"
        for contents in (None, "{}"):
            if contents is None:
                store.unlink()
            else:
                _ = store.write_text(contents)
            for operation in ("open", "resolve", "checkout"):
                for host in ("natedev", "macbook", ""):
                    if operation == "open" and host != "natedev":
                        continue
                    with self.subTest(contents=contents, operation=operation, host=host):
                        payload: dict[str, object] = {
                            "repository_id": "fixture-repo", "document": "design.md",
                            "old_checkout": str(self.repository), "new_checkout": str(self.root / "new-checkout"),
                        }
                        if host:
                            payload["host"] = host
                        with self.assertRaises(design_roles.RoleRefused) as refused:
                            if operation == "open":
                                _ = self.open_designer()
                            else:
                                _ = self.authority(operation, payload)
                        self.assertEqual(str(refused.exception),
                                         "repository fixture-repo is not registered; run design_roles.py register "
                                         + f"--repository fixture-repo --host {host or '<host>'} "
                                         + "--checkout <absolute path>")
        self.assertEqual(self.launches, [])

    def test_malformed_repository_record_keeps_the_object_refusal(self) -> None:
        store = self.root / "ownership/repositories.json"
        records: tuple[object, ...] = ([], "invalid", 42, False)
        for record in records:
            _ = store.write_text(json.dumps({"fixture-repo": record}))
            for operation in ("reserve", "resolve", "checkout"):
                with self.subTest(record=record, operation=operation):
                    with self.assertRaises(design_roles.RoleRefused) as refused:
                        _ = self.authority(operation, {"repository_id": "fixture-repo", "host": "natedev"})
                    self.assertEqual(str(refused.exception), "expected a JSON object")

    def test_registered_document_alias_reuses_the_original_pair(self) -> None:
        design = self.open_designer()
        _ = self.authority("alias", {"repository_id": "fixture-repo", "host": "natedev",
                                     "document": "design.md", "alias": "renamed/design.md"})
        retried = self.open_designer(document="renamed/design.md")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unknown_thread_creation_retains_the_claim_instead_of_launching_again(self) -> None:
        self.panes.world.interrupt = "agent"
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.open_designer()
        count = len(self.panes.world.launches)
        _ = self.panes.assign(self.panes.commands[-1])
        design_path, = design_roles.state_root().glob("*/design.json")
        _ = design_roles.recover(design_path.parent.name + "/designer")
        retry = self.open_designer(request_id="another-attempt")
        self.assertEqual(len(self.panes.world.launches), count)
        self.assertIsInstance(self.instance(retry, "designer").lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)


    def test_simultaneous_hosts_reserve_only_one_document_placement(self) -> None:
        def reserve(host: str) -> str:
            try:
                placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": "race.md",
                                                       "host": host, "request_id": host})
                return str(placement["design_id"])
            except design_roles.RoleRefused:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("natedev", "macbook")))
        self.assertEqual(results.count("refused"), 1)
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(self.launches, [])

    def test_lost_creation_acknowledgement_never_launches_a_second_thread(self) -> None:
        self.lose_creation_ack = True
        with self.assertRaises(TimeoutError):
            _ = self.open_designer()
        design_path, = design_roles.state_root().glob("*/design.json")
        _ = design_roles.recover(design_path.parent.name + "/designer")
        retried = self.open_designer()
        lifecycle = self.instance(retried, "designer").lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unreachable_authority_and_host_mismatch_cannot_create_roles(self) -> None:
        with patch.object(design_roles, "authority_request", side_effect=design_roles.RoleRefused("authority unavailable")):
            with self.assertRaisesRegex(design_roles.RoleRefused, "authority unavailable"):
                _ = self.open_designer()
        with self.assertRaisesRegex(design_roles.RoleRefused, "host"):
            _ = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design", host="macbook")
        self.assertEqual(self.launches, [])
        self.assertEqual(list((self.root / "roles").glob("*/design.json")), [])

    def test_invalid_model_registry_refuses_before_thread_creation(self) -> None:
        write_text_atomic(Path(os.environ["MODELS_CONFIG_FILE"]), "[design_roles.codex\ndesigner=gpt-fixture:xhigh\n")
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.open_designer()
        self.assertEqual(self.launches, [])

    def test_missing_or_disallowed_configured_model_never_uses_a_fallback(self) -> None:
        config = Path(os.environ["MODELS_CONFIG_FILE"])
        for index, model in enumerate((None, "unlisted-model:xhigh", "gpt-fixture:invalid-effort")):
            document = f"configuration-{index}.md"
            write_text_atomic(self.repository / document, "# Private design\n")
            with self.subTest(model=model):
                _ = write_registry(config, {"design_roles.codex": {"designer": model},
                                            "codex.agents": {"gpt-fixture": "xhigh"}})
                with self.assertRaisesRegex(design_roles.RoleRefused, "invalid role configuration"):
                    _ = self.open_designer(document=document)
        self.assertEqual(self.launches, [])

    def test_showrunner_cannot_open_unbound_writer_but_reviewer_is_read_only(self) -> None:
        with self.assertRaisesRegex(design_roles.RoleRefused, "binding"):
            _ = design_roles.open_role("fixture-repo", "design.md", "designer", "design")
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design")
        self.assertIsInstance(design_roles.access_policy(design, "reviewer"), design_roles.ReadOnlyReviewer)
        self.assertEqual(self.launches, [("reviewer", "364898ad-f177-5514-80f3-b82001fa95d9")])

    def test_prompts_and_replies_are_saved_with_phase_assignment_headers(self) -> None:
        design = self.open_designer()
        lifecycle = self.instance(design, "designer").lifecycle
        assert isinstance(lifecycle, design_roles.RoleOpen)
        worker = staff.by_id(lifecycle.staff_id)
        assert worker is not None
        for turn, name in enumerate((worker.name, "renamed-design-worker"), start=1):
            with self.subTest(name=name):
                _ = staff.rename(worker.id, name)
                reply = self.resume(design, "designer")
                prompts = list(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
                self.assertEqual(len(prompts), turn)
                prompt = design_roles.design_directory(design.placement.design_id) / "prompts" / reply.name
                header = astra_work.read_header(prompt)
                self.assertIsInstance(header, astra_work.AstraWork)
                if not isinstance(header, astra_work.AstraWork):
                    self.fail("saved assignment has no work header")
                self.assertEqual((header.member, header.role, header.reply_path),
                                 (name, "astra-designer", str(reply)))
                self.assertEqual(header.work, astra_work.phase_work(self.run_a, "1"))
                entries = cast(list[dict[str, object]], design_roles.list_roles()["roles"])
                listed = next(entry for entry in entries if entry["role"] == "designer")
                self.assertEqual(listed["name"], name)

    def test_rebind_to_an_existing_second_run_keeps_thread_and_changes_next_header(self) -> None:
        design = self.open_designer()
        first_reply = self.resume(design, "designer")
        previous = first_reply.read_bytes()
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="next phase",
                               binding=self.binding(self.run_b))
        next_reply = self.resume(rebound, "designer")
        self.assertTrue(self.run_a.is_dir())
        self.assertEqual(first_reply.read_bytes(), previous)
        self.assertNotEqual(next_reply, first_reply)
        self.assertEqual(self.launches, [("designer", "9309c903-5894-501a-8aa7-f33ea912e074")])
        self.assertEqual([entry[1] for entry in self.dispatches], ["9309c903-5894-501a-8aa7-f33ea912e074", "9309c903-5894-501a-8aa7-f33ea912e074"])
        header = astra_work.read_header(sorted(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))[-1])
        self.assertIsInstance(header, astra_work.AstraWork)
        if isinstance(header, astra_work.AstraWork):
            self.assertEqual(header.work, astra_work.phase_work(self.run_b, "1"))
        self.assertEqual(self.panes.world.stops, [])
        self.assertEqual(self.panes.world.closed, [])

    def test_open_with_a_phase_and_no_checkout_saves_the_design_repository(self) -> None:
        parent = staff.by_path("demo/design")
        assert parent is not None
        with self.subTest(checkout="another caller"), patch.dict(os.environ, {"CODEX_THREAD_ID": fixture_runtime_id("another-design-caller")}):
            self.assertEqual(design_roles._registered_path(  # pyright: ignore[reportPrivateUsage]
                {"repository_owner": parent.id}, "natedev", staff.snapshot()), str(self.repository))
        output = io.StringIO()
        with redirect_stdout(output):
            code = design_roles.main(["open", "--repository", "fixture-repo", "--document", "design.md",
                "--role", "designer", "--area", "design",
                "--run-directory", str(self.run_a), "--phase", "1", "--output-path", "design.md"])
        self.assertEqual(code, 0, output.getvalue())
        design = design_roles.read_design(output.getvalue().strip().split("/")[0])
        self.assertEqual(design.binding, self.binding(self.run_a))
        parent = staff.by_path("demo/design")
        assert parent is not None
        path = design_roles.design_directory(design.placement.design_id) / "design.json"
        before = path.read_bytes()
        fields = cast(dict[str, object], json.loads(before))
        self.assertEqual(fields["repository_owner"], parent.id)
        self.assertNotIn("repository", fields)
        binding = cast(dict[str, object], fields["binding"])
        self.assertEqual(binding["repository_owner"], parent.id)
        self.assertNotIn("repository", binding)
        ownership = self.root / "ownership/repositories.json"
        registered = cast(dict[str, dict[str, object]], json.loads(ownership.read_text()))
        self.assertEqual(registered["fixture-repo"]["checkouts"],
                         {"natedev": {"repository_owner": parent.id}, "macbook": str(self.root / "mac-checkout")})
        placement, = (self.root / "ownership/placements").glob("*.json")
        placed = cast(dict[str, object], json.loads(placement.read_text()))
        self.assertEqual(placed["repository_owner"], parent.id)
        self.assertNotIn("checkout", placed)
        authority_before = ownership.read_bytes(), placement.read_bytes()
        for row, destination in (("original-document", self.repository),
                                 ("moved-document", self.root / "staff-moved-checkout")):
            with self.subTest(row=row):
                if destination != self.repository:
                    _ = self.repository.rename(destination)
                    _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination),
                                           expected=parent.worktree)
                self.assertEqual(design_roles.design_repository(design), str(destination))
                assert isinstance(design.binding, design_roles.DesignBoundToPhase)
                self.assertEqual(design_roles.binding_repository(design.binding), str(destination))
                current = design_roles.read_design(design.placement.design_id)
                self.assertEqual(current.repository, str(destination))
                assert isinstance(current.binding, design_roles.DesignBoundToPhase)
                self.assertEqual(current.binding.repository, str(destination))
                self.assertEqual(current.document, str(destination / "design.md"))
                self.assertEqual(design_roles.held_document(destination / "design.md"), current)
                self.assertEqual(path.read_bytes(), before)
                reopened = self.open_designer()
                self.assertEqual(reopened.placement.design_id, design.placement.design_id)
                self.assertEqual((ownership.read_bytes(), placement.read_bytes()), authority_before)
                reply = self.resume(current, "designer")
                prompt = design_roles.design_directory(design.placement.design_id) / "prompts" / reply.name
                header = astra_work.read_header(prompt)
                assert isinstance(header, astra_work.AstraWork)
                self.assertEqual(header.document, str(destination / "design.md"))
                fields = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(fields["document"], "design.md")
                before = path.read_bytes()

    def test_unbind_repairs_an_empty_repository_and_a_retry_writes_nothing(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        command = ["rebind", design.placement.design_id, "--expected-generation", "1",
                   "--unbind", "--reason", "repair historical binding"]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(repaired.binding, design_roles.DesignUnbound)
        self.assertEqual(repaired.placement.generation, 2)
        self.assertEqual(repaired.roles, design.roles)
        before = path.read_bytes(), path.stat().st_mtime_ns
        closed = list(self.panes.world.closed)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.panes.world.closed, closed)

    def test_rebind_to_a_checkout_repairs_an_empty_binding_repository(self) -> None:
        design = self.open_designer()
        _ = damage_binding_repository(design)
        parent = staff.by_path("demo/design")
        showrunner = staff.by_path("demo")
        assert parent is not None and showrunner is not None
        for row, caller, generation in (("unit-repair", parent, "1"),
                                        ("showrunner-same-checkout", showrunner, "2")):
            with self.subTest(row=row), patch.dict(os.environ, {"CODEX_THREAD_ID": caller.current_session}):
                with redirect_stdout(io.StringIO()):
                    code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", generation,
                        "--checkout", str(self.repository), "--run-directory", str(self.run_b), "--phase", "1",
                        "--output-path", "design.md", "--reason", "repair phase binding"])
                self.assertEqual(code, 0)
                repaired = design_roles.read_design(design.placement.design_id)
                self.assertEqual(repaired.binding, self.binding(self.run_b))
                self.assertEqual(repaired.placement.generation, 2)
                self.assertEqual(repaired.roles, design.roles)
                fields = cast(dict[str, object], json.loads(
                    (design_roles.design_directory(design.placement.design_id) / "design.json").read_text()))
                self.assertEqual(fields["repository_owner"], parent.id)
                self.assertNotIn("repository", fields)
                binding = cast(dict[str, object], fields["binding"])
                self.assertEqual(binding["repository_owner"], parent.id)
                self.assertNotIn("repository", binding)

    def test_binding_repair_still_refuses_a_stale_generation(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        error = io.StringIO()
        with redirect_stderr(error):
            code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "0",
                                      "--unbind", "--reason", "stale repair"])
        self.assertEqual(code, 2)
        self.assertIn("stale binding generation", error.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.panes.world.closed, [])

    def test_checkout_repair_without_output_paths_keeps_the_existing_paths(self) -> None:
        design = self.open_designer()
        _ = damage_binding_repository(design)
        with redirect_stdout(io.StringIO()):
            code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "1",
                "--checkout", str(self.repository), "--run-directory", str(self.run_b), "--phase", "1",
                "--reason", "repair without replacing output paths"])
        self.assertEqual(code, 0)
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertEqual(repaired.binding, self.binding(self.run_b))
        self.assertEqual(repaired.placement.generation, 2)
        self.assertEqual(repaired.roles, design.roles)

    def test_invalid_rebind_target_is_refused_before_any_lifecycle_effect(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes(), path.stat().st_mtime_ns
        invalid_bindings = (
            design_roles.DesignBoundToPhase(str(self.run_b), "1", str(self.repository), ()),
            design_roles.DesignBoundToPhase("", "1", str(self.repository), ("design.md",)),
            design_roles.DesignBoundToPhase(str(self.run_b), "", str(self.repository), ("design.md",)),
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding):
                operations: list[str] = []

                def authority(operation: str, payload: dict[str, object]) -> dict[str, object]:
                    operations.append(operation)
                    return self.authority(operation, payload)

                with patch.object(design_roles, "authority_request", side_effect=authority), \
                        self.assertRaises(design_roles.RoleRefused):
                    _ = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                           reason="invalid target", binding=binding)
                self.assertNotIn("advance", operations)
                self.assertEqual(self.panes.world.closed, [])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                self.assertFalse((path.parent / "transition.json").exists())

    def assert_interrupted_repair_finishes_publication(self, stop_at: str) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        directory = path.parent
        command = ["rebind", design.placement.design_id, "--expected-generation", "1",
                   "--unbind", "--reason", "finish interrupted repair"]
        atomic = design_roles._atomic  # pyright: ignore[reportPrivateUsage]

        def interrupt(target: Path, value: object) -> None:
            if target.name == stop_at and target.name != "design.json":
                raise OSError("publication interrupted")
            atomic(target, value)
            if target.name == stop_at:
                raise OSError("publication interrupted")

        with patch.object(design_roles, "_atomic", side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 2)
        committed = design_roles.read_design(design.placement.design_id)
        self.assertEqual(committed.placement.generation, 2)
        self.assertIsInstance(committed.binding, design_roles.DesignUnbound)
        # Force recovery to prove it republishes the census, rather than reading an old hold.
        (self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").unlink(missing_ok=True)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = design_roles.main(command)
        self.assertEqual(code, 0, error.getvalue())
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{design.placement.design_id} rebound at generation 2"])
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertEqual(repaired, committed)
        self.assertEqual(self.panes.world.stops, [])
        self.assertEqual(self.panes.world.closed, [])
        self.assertEqual([record["thread_id"] for record in astra_roles.live_roles(staff.snapshot())], ["9309c903-5894-501a-8aa7-f33ea912e074"])
        self.assertFalse((directory / "transition.json").exists())
        unchanged = {item: (item.read_bytes(), item.stat().st_mtime_ns)
                     for item in (path, self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json")}
        closed = list(self.panes.world.closed)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual({item: (item.read_bytes(), item.stat().st_mtime_ns) for item in unchanged}, unchanged)
        self.assertEqual(self.panes.world.closed, closed)

    def test_repair_retry_finishes_publication_after_the_design_save(self) -> None:
        self.assert_interrupted_repair_finishes_publication("design.json")


    def test_markerless_repair_retry_finishes_census_before_reporting_success(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        command = ['rebind', design.placement.design_id, '--expected-generation', '1',
                   '--unbind', '--reason', 'finish census publication']
        def interrupt(design: design_roles.HeldDesign, role: Role, record: staff.StaffSnapshot) -> None:
            del design, role, record
            raise OSError('census publication interrupted')

        with patch.object(design_roles, '_project_hold', side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 2)
        (path.parent / 'transition.json').unlink()
        hold = self.root / 'holds/9309c903-5894-501a-8aa7-f33ea912e074.json'
        hold.unlink()
        output = io.StringIO()
        with patch.object(design_roles, '_project_hold', side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 2)
        self.assertEqual(output.getvalue(), '')
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual(output.getvalue().splitlines(), [f'{design.placement.design_id} rebound at generation 2'])
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual([role['thread_id'] for role in astra_roles.live_roles(staff.snapshot())],
                         ['9309c903-5894-501a-8aa7-f33ea912e074'])
        self.assertEqual(self.panes.world.stops, [])
        self.assertEqual(self.panes.world.closed, [])

    def test_binding_repair_does_not_accept_other_record_damage(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        original = cast(dict[str, object], json.loads(path.read_text()))
        for defect in ("missing repository", "null repository", "invalid schema", "invalid role"):
            with self.subTest(defect=defect):
                fields = cast(dict[str, object], json.loads(json.dumps(original)))
                binding = cast(dict[str, object], fields["binding"])
                if defect == "missing repository":
                    del binding["repository"]
                elif defect == "null repository":
                    binding["repository"] = None
                elif defect == "invalid schema":
                    fields["schema"] = 999
                else:
                    cast(list[dict[str, object]], fields["roles"])[0]["role"] = "invalid"
                _ = path.write_text(json.dumps(fields))
                before = path.read_bytes()
                with redirect_stderr(io.StringIO()):
                    code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "1",
                                              "--unbind", "--reason", "must not mask other damage"])
                self.assertEqual(code, 2)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.panes.world.closed, [])

    def test_other_record_verbs_refuse_the_damaged_path_and_binding_field(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        instance = design.placement.design_id + "/designer"
        prompt = self.root / "repair-prompt.md"
        _ = prompt.write_text("Keep the design identity.\n")
        common = ["--repository", "fixture-repo", "--document", "design.md", "--role", "designer",
                  "--area", "design"]
        commands = [
            ["open", *common],
            ["resume", instance, "--prompt-file", str(prompt)], ["end", instance], ["recover", instance],
            ["unbind-run", str(self.run_a)],
            ["rebind", design.placement.design_id, "--expected-generation", "1", "--reason", "no repair target"],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                error = io.StringIO()
                with redirect_stderr(error), redirect_stdout(io.StringIO()):
                    code = design_roles.main(command)
                self.assertEqual(code, 2, error.getvalue())
                expected = (f"design_roles: {path}: missing or invalid repository; live held role needs recovery"
                            if command[0] == "unbind-run" else
                            f"design_roles: {path}: missing or invalid binding.repository")
                self.assertEqual(error.getvalue().splitlines(), [expected])
                self.assertEqual(path.read_bytes(), before)

    def test_other_unit_scans_skip_a_damaged_record_with_one_named_diagnostic(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertIsInstance(design_roles.held_document(self.repository / 'other-design.md'),
                                  design_roles.NotHeldDocument)
        self.assertEqual(errors.getvalue().splitlines(), [f'warning: {path}: missing or invalid repository'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.panes.world.closed, [])


    def test_holding_unit_scans_keep_the_damaged_record_as_a_named_refusal(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        with self.assertRaises(design_roles.RoleRefused) as error:
            _ = design_roles.held_document(Path(design.document))
        self.assertEqual(str(error.exception), f'{path}: missing or invalid repository; live held role needs recovery')
        self.assertEqual(path.read_bytes(), before)


    def test_pending_messages_clear_only_after_successful_turn_acceptance(self) -> None:
        design = self.open_designer()
        message = design_roles.queue_message(design.placement.design_id, "designer", "holder", "Keep evidence.")
        _ = design_roles.queue_message(design.placement.design_id, "designer", "holder", "Keep evidence.", message_id=message.id)
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.fail_dispatch = True
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.resume(design, "designer")
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.assertIn(message.id, self.dispatches[-1][2])
        lifecycle = self.instance(design_roles.read_design(design.placement.design_id), "designer").lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleNeedsRecovery)

    def test_successful_assignment_consumes_its_pending_ids_without_waking_on_send(self) -> None:
        design = self.open_designer()
        message = design_roles.queue_message(design.placement.design_id, "designer", "peer", "Cite the file.")
        self.assertEqual(self.dispatches, [])
        _ = self.resume(design, "designer")
        self.assertIn(message.id, self.dispatches[0][2])
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_message_queued_during_a_turn_is_kept_for_the_next_assignment(self) -> None:
        design = self.open_designer()
        first = design_roles.queue_message(design.placement.design_id, "designer", "peer", "First evidence.")
        during_turn: list[design_roles.PendingMessage] = []

        def dispatch_with_message(current: design_roles.HeldDesign, role: Role, prompt: Path,
                                  reply: Path) -> design_roles.SubmittedRoleTurn:
            during_turn.append(design_roles.queue_message(design.placement.design_id, role, "peer", "Later evidence."))
            return self.dispatch(current, role, prompt, reply)

        with patch.object(design_roles, "dispatch_turn", side_effect=dispatch_with_message):
            _ = self.resume(design, "designer")
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), tuple(during_turn))
        self.assertIn(first.id, self.dispatches[-1][2])
        self.assertNotIn(during_turn[0].id, self.dispatches[-1][2])
        _ = self.resume(design, "designer")
        self.assertIn(during_turn[0].id, self.dispatches[-1][2])
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_holder_transfer_fences_the_old_session_and_keeps_the_thread(self) -> None:

        design = self.open_designer()
        changed = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                      reason='phase change', binding=self.binding(self.run_b))
        with self.assertRaises(design_roles.RoleRefused):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                    reason='stale generation', binding=self.binding(self.run_a))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': fixture_runtime_id('different-caller')}):
            _ = self.resume(changed, 'designer')
        self.assertEqual(changed.placement.generation, 2)
        self.assertEqual(changed.roles, design.roles)
        self.assertEqual(self.launches, [('designer', '9309c903-5894-501a-8aa7-f33ea912e074')])
        self.assertEqual([entry[1] for entry in self.dispatches], ['9309c903-5894-501a-8aa7-f33ea912e074'])


    def test_holder_restart_repairs_a_missing_hold_without_replacing_the_thread(self) -> None:
        design = self.open_designer()
        (self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").unlink()
        self.holder_session = '5dea413e-cf4a-5eba-b1b4-0b19c7e0d86e'
        parent = design_roles.worker_parent(design, "designer", staff.snapshot())
        _ = staff.update_current_session(parent.current_session, staff.native_id(self.holder_session))
        os.environ["CODEX_THREAD_ID"] = self.holder_session
        _ = self.resume(design, "designer")
        records = astra_roles.live_roles(staff.snapshot())
        self.assertEqual([(record["thread_id"], record["manager"]) for record in records],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", '5dea413e-cf4a-5eba-b1b4-0b19c7e0d86e')])
        self.assertEqual(len(self.launches), 1)

    def test_role_becomes_idle_after_its_process_exits_without_a_status_write(self) -> None:
        design = self.open_designer()
        row_id = cast(design_roles.RoleOpen, self.instance(design, "designer").lifecycle).staff_id
        pane = next(pane for pane, process in self.panes.world.foreground.items()
                    if self.panes.world.processes[process[0]].current_identity == row_id)
        self.panes.world.locations[pane]["agent_status"] = "working"
        self.assertEqual([role["state"] for role in astra_roles.live_roles(staff.snapshot())], ["busy"])
        _ = self.panes.world.foreground.pop(pane)
        self.assertEqual([role["state"] for role in astra_roles.live_roles(staff.snapshot())], ["idle"])


    def test_durable_hold_survives_run_removal_and_keeps_one_census_edge(self) -> None:
        design = self.open_designer()
        shutil.rmtree(self.run_a)
        records = astra_roles.live_roles(staff.snapshot())
        self.assertEqual([(record["thread_id"], record["state"]) for record in records],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", "idle")])
        self.assertIsInstance(records[0]["lifetime"], astra_roles.HeldDesignRole)
        record = staff.snapshot()
        parent = design_roles.worker_parent(design, "designer", record)
        workers = record.find(kind="worker", parent=parent.id)
        self.assertEqual([(worker.current_session, worker.parent) for worker in workers],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", staff.ReportsTo(parent.id))])
        self.assertEqual([(role["manager"], role["thread_id"], role["role"], role["state"])
                          for role in records],
                         [('17759e5d-1377-577b-b283-19888bbfc1f5', "9309c903-5894-501a-8aa7-f33ea912e074",
                           "designer", "idle")])
        self.assertEqual(design_roles.read_design(design.placement.design_id).placement.design_id, design.placement.design_id)

    def test_rebind_keeps_the_pane_and_account_and_uses_the_current_phase(self) -> None:
        design = self.open_designer()
        before = self.panes.world.locations.copy()
        backend = staff.snapshot().backends[cast(design_roles.RoleOpen, design.roles[0].lifecycle).staff_id]
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="run change",
                                      binding=self.binding(self.run_b))
        reply = self.resume(rebound, "designer")
        self.assertTrue(reply.read_text().startswith("Saved reply from "))
        self.assertEqual(self.panes.world.locations, before)
        self.assertEqual(staff.snapshot().backends[cast(design_roles.RoleOpen, rebound.roles[0].lifecycle).staff_id], backend)
        self.assertEqual(len(self.panes.world.launches), 1)
        prompt = next(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
        header = astra_work.read_header(prompt)
        self.assertIsInstance(header, astra_work.AstraWork)
        if isinstance(header, astra_work.AstraWork):
            self.assertEqual(header.work, astra_work.phase_work(self.run_b, "1"))

    def test_stale_rebind_and_busy_rebind_preserve_the_current_binding(self) -> None:
        design = self.open_designer()
        with self.assertRaisesRegex(design_roles.RoleRefused, "generation"):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=2, reason="stale",
                             binding=self.binding(self.run_b))
        busy = replace(design, roles=(replace(design.roles[0], assignment_claim="active-dispatch"), design.roles[1]))
        design_roles._save(busy)  # pyright: ignore[reportPrivateUsage]
        with self.assertRaisesRegex(design_roles.RoleRefused, "busy"):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="busy",
                             binding=self.binding(self.run_b))
        self.assertEqual(design_roles.read_design(design.placement.design_id).binding, design.binding)
        self.assertEqual(self.panes.world.closed, [])

    def list_output(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(["list", *arguments]), 0)
        return output.getvalue()

    def test_list_prints_each_role_with_its_holder_session_thread_and_document(self) -> None:
        design = self.open_designer()
        lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2, lines)
        for role in ("designer", "reviewer"):
            instance_id = design.placement.design_id + "/" + role
            line = next(line for line in lines if instance_id in line)
            self.assertNotIn("holder", line)
            self.assertIn(design.document, line)
            self.assertNotRegex(line, r"[{}]")
            if role == "designer":
                self.assertIn("RoleOpen", line)
                self.assertIn("9309c903-5894-501a-8aa7-f33ea912e074", line)
            else:
                self.assertIn("RoleNotOpened", line)
                self.assertNotIn("thread", line)

    def test_list_names_showrunner_and_unavailable_holder_reason(self) -> None:

        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / 'design.json'
        fields = cast(dict[str, object], json.loads(path.read_text()))
        fields['holder'] = {'kind': 'invalid legacy holder'}
        _ = path.write_text(json.dumps(fields))
        before = path.read_bytes(), path.stat().st_mtime_ns
        lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(design.placement.design_id, line)
            self.assertIn(design.document, line)
            self.assertNotIn('invalid legacy holder', line)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


    def test_list_prints_empty_inventory_and_record_problems(self) -> None:
        self.assertEqual(self.list_output(), "no held roles\n")
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir(parents=True)
        _ = bad.write_text('{"schema": 999}')
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(self.list_output(), "no held roles\n")
        lines = error.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"problem: {bad}: "), lines)
        self.assertGreater(len(lines[0]), len(f"problem: {bad}: "))
        with redirect_stderr(io.StringIO()):
            design = self.open_designer()
        error = io.StringIO()
        with redirect_stderr(error):
            lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith(design.placement.design_id + '/') for line in lines), lines)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertIn(f"problem: {bad}: ", error.getvalue())

    def test_list_json_preserves_full_inventory_and_problems(self) -> None:
        _ = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        report = cast(dict[str, object], json.loads(self.list_output("--json")))
        inventory = cast(object, json.loads(json.dumps(design_roles.list_roles())))
        self.assertEqual(report, inventory)
        entries = cast(list[dict[str, object]], report["roles"])
        self.assertEqual(len(entries), 2)
        self.assertNotIn("holder_reach", entries[0])
        self.assertIn(str(bad), str(report["problems"]))

    def test_inventory_lists_bad_records_without_network_and_keeps_valid_roles(self) -> None:
        design = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        with patch.object(design_roles, "authority_request", side_effect=AssertionError("inventory called authority")), \
                patch.object(subprocess, "run", side_effect=AssertionError("inventory ran a command")):
            inventory = design_roles.list_roles()
        entries = cast(list[dict[str, object]], inventory["roles"])
        self.assertIn(design.placement.design_id, [entry["design_id"] for entry in entries])
        self.assertIn(str(bad), str(inventory["problems"]))


    def test_patch_checks_use_the_current_run_and_refuse_stale_or_mixed_paths(self) -> None:
        design = self.open_designer()
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="next run",
                               binding=self.binding(self.run_b))
        self.assertTrue(self.run_a.is_dir())
        accepted = design_roles.check_patch(design.placement.design_id, 2, self.repository, ["./design.md"])
        self.assertEqual(accepted, rebound.binding)
        for generation, paths in ((1, ["design.md"]), (2, ["design.md", "unowned.md"]),
                                  (2, ["design.md", "renamed.md"])):
            with self.subTest(generation=generation, paths=paths), self.assertRaises(design_roles.RoleRefused):
                _ = design_roles.check_patch(design.placement.design_id, generation, self.repository, paths)
        _ = (self.run_b / "phases/1/delegated_phase_reservation_state.json").write_bytes(b"\xff")
        with self.assertRaises((ValueError, OSError)):
            _ = design_roles.check_patch(design.placement.design_id, 2, self.repository, ["design.md"])

    def test_a_binding_to_an_ad_hoc_phase_open_beside_another_accepts_its_patch(self) -> None:
        phase = self.run_a / "phases/adhoc"
        phase.mkdir()
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "ad hoc"}))
        _ = (phase / "files").write_text("design.md\n")
        state = self.run_a / "progress_history_state.json"
        recorded = cast(dict[str, object], json.loads(state.read_text()))
        ad_hoc = {"id": "ad hoc", "instance_id": "instance-ad-hoc", "status": "active"}
        recorded["open_phases"] = [*cast(list[object], recorded["open_phases"]), ad_hoc]
        _ = state.write_text(json.dumps(recorded))
        binding = replace(self.binding(self.run_a), phase="ad hoc")
        design = design_roles.open_role("fixture-repo", "design.md", "designer", "design", binding=binding, request_id="open-ad-hoc")
        self.assertEqual(design_roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"]), binding)

    def test_patch_cannot_follow_a_symlink_outside_the_reserved_repository(self) -> None:
        design = self.open_designer()
        outside = self.root / "outside.md"
        _ = outside.write_text("Private evidence")
        (self.repository / "design.md").unlink()
        (self.repository / "design.md").symlink_to(outside)
        with self.assertRaises(design_roles.RoleRefused):
            _ = design_roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"])
        self.assertEqual(outside.read_text(), "Private evidence")

    def test_unbind_keeps_evidence_and_threads_but_refuses_the_next_writer_turn(self) -> None:
        design = self.open_designer()
        reply = self.resume(design, "designer")
        saved = {path: path.read_bytes() for path in design_roles.design_directory(design.placement.design_id).glob("prompts/*.md")}
        design_roles.unbind_run(str(self.run_a))
        unbound = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(unbound.binding, design_roles.DesignUnbound)
        self.assertEqual(self.instance(unbound, "designer").lifecycle, design_roles.RoleOpen(staff.StaffId("9309c903-5894-501a-8aa7-f33ea912e074")))
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.resume(unbound, "designer")
        for path, contents in saved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(reply.is_file())
        self.assertEqual(len(self.launches), 1)

    def test_unbind_run_rerun_with_a_trailing_slash_finishes_the_saved_transition(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        arguments = ["unbind-run", str(self.run_a) + "/"]
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.main(arguments)
        saved = design_roles.read_design(identifier)
        self.assertIsInstance(saved.binding, design_roles.DesignUnbound)
        self.assertEqual(design_roles.main(arguments), 0)
        self.assertEqual(design_roles.read_design(identifier), saved)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        self.assertEqual(saved.roles, design.roles)
        self.assertEqual(len(self.launches), 1)

    def test_end_is_terminal_and_keeps_saved_prompts(self) -> None:
        design = self.open_designer()
        folder = design_roles.design_directory(design.placement.design_id)
        saved = {path: path.read_bytes() for path in folder.glob("prompts/*.md")}
        _ = self.resume(design, "designer")
        _ = self.panes.world.foreground.pop("w1:p1")
        ended = design_roles.end(design.placement.design_id + "/designer")
        retried = design_roles.end(design.placement.design_id + "/designer")
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertIsInstance(self.instance(retried, "designer").lifecycle, design_roles.RoleEnded)
        with self.assertRaisesRegex(design_roles.RoleRefused, "role ended; this design identity cannot be reopened"):
            _ = self.open_designer()
        self.assertEqual(len(self.launches), 1)
        self.assertFalse((self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").exists())
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_unit_rename_retry_keeps_both_original_threads_and_updates_live_paths(self) -> None:

        design = self.open_designer()
        design = design_roles.open_role('fixture-repo', 'design.md', 'reviewer', 'design', binding=self.binding(self.run_a))
        initial_prompts = {path: path.read_bytes() for path in design_roles.design_directory(design.placement.design_id).glob('prompts/*.md')}
        destination = self.root / 'renamed-checkout'
        _ = self.repository.rename(destination)
        current = design_roles.rebind(design.placement.design_id, expected_generation=1, reason='checkout move',
                                      repository=str(destination), document=str(destination / 'design.md'),
                                      binding=replace(self.binding(self.run_a), repository=str(destination)))
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(current.repository, str(destination))
        self.assertEqual(current.document, str(destination / 'design.md'))
        self.assertEqual(current.placement.generation, 2)
        self.assertEqual(current.roles, design.roles)
        self.assertEqual({path: path.read_bytes() for path in initial_prompts}, initial_prompts)
        for role in ('designer', 'reviewer'):
            _ = self.resume(current, role)
        self.assertEqual([(role, thread) for role, thread, _text in self.dispatches],
                         [('designer', '9309c903-5894-501a-8aa7-f33ea912e074'), ('reviewer', '364898ad-f177-5514-80f3-b82001fa95d9')])
        self.assertEqual(len(self.launches), 2)

    def assert_checkout_ownership_survives_move(self, *, independent: bool, operation: str) -> None:
        parent = staff.by_path("demo/design")
        assert parent is not None
        expected = design_roles.INDEPENDENT_CHECKOUT if independent else design_roles.UnitCheckout(parent.id)
        if independent:
            registry = self.root / "ownership/repositories.json"
            records = cast(dict[str, dict[str, object]], json.loads(registry.read_text()))
            records["fixture-repo"]["checkouts"] = {"natedev": {"configured_checkout": str(self.repository)}}
            write_text_atomic(registry, json.dumps(records))
        design = self.open_designer()
        self.assertEqual(design.checkout, expected)
        destination = self.root / "ownership-destination"
        _ = self.repository.rename(destination)
        destination_owner = parent
        if independent:
            destination_owner = fixture_member("unit", name="destination-owner", parent=parent.parent,
                                               worktree=staff.PresentWorktree("natedev", destination))
        if operation == "move":
            design_roles.move_checkout(str(self.repository), str(destination), reason="ownership test")
            current = design_roles.read_design(design.placement.design_id)
        else:
            current = design_roles.rebind(design.placement.design_id, expected_generation=1,
                reason="ownership test", repository=str(destination), document=str(destination / "design.md"),
                binding=replace(self.binding(self.run_a), repository=str(destination), checkout=expected))
        self.assertEqual(current.checkout, expected)
        assert isinstance(current.binding, design_roles.DesignBoundToPhase)
        self.assertEqual(current.binding.checkout, expected)
        path = design_roles.design_directory(design.placement.design_id) / "design.json"
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(saved.get("repository_owner"), None if independent else parent.id)
        if independent:
            _ = staff.set_worktree(destination_owner.id, staff.PresentWorktree("natedev", self.root / "other-unit-moved"),
                                   expected=destination_owner.worktree)
        else:
            _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", destination), expected=parent.worktree)
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(current.repository, str(destination))
        self.assertEqual(current.document, str(destination / "design.md"))
        self.assertEqual(current.roles, design.roles)
        self.assertEqual(len(self.launches), 1)

    def test_checkout_move_preserves_the_unit_owner_before_staff_moves(self) -> None:
        self.assert_checkout_ownership_survives_move(independent=False, operation="move")

    def test_checkout_move_preserves_independence_at_another_unit_path(self) -> None:
        self.assert_checkout_ownership_survives_move(independent=True, operation="move")

    def test_rebind_preserves_the_unit_owner_before_staff_moves(self) -> None:
        self.assert_checkout_ownership_survives_move(independent=False, operation="rebind")

    def test_rebind_preserves_independence_at_another_unit_path(self) -> None:
        self.assert_checkout_ownership_survives_move(independent=True, operation="rebind")


    def test_a_unit_transfer_moves_the_holder_to_the_receiving_production(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        receiver = fixture_member('showrunner', name='receiving-production')
        _ = staff.move(parent.id, staff.ReportsTo(receiver.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_unit_rename_skips_another_units_damaged_record(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        _ = staff.rename(parent.id, 'renamed-parent')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_unit_transfer_skips_another_units_damaged_record(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        receiver = fixture_member('showrunner', name='receiving-production')
        _ = staff.move(parent.id, staff.ReportsTo(receiver.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_holder_move_refuses_its_own_damaged_record_by_path_and_field(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        with self.assertRaises(worker_lifecycle.WorkerStateUnreadable) as error:
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1, reason='phase change')
        self.assertIn(str(path), str(error.exception))
        self.assertIn('repository', str(error.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.panes.world.closed, [])


    def test_a_production_rename_moves_the_showrunner_holder(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        _ = staff.rename(parent.id, 'renamed-parent')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_rebind_preserves_a_worker_with_unknown_process_evidence(self) -> None:
        design = self.open_designer()
        row_id = cast(design_roles.RoleOpen, design.roles[0].lifecycle).staff_id
        before = staff.by_id(row_id)
        locations = self.panes.world.locations.copy()
        launches = len(self.panes.world.launches)
        self.panes.world.unknown["w1:p1"] = "native process evidence is unavailable"
        with redirect_stderr(io.StringIO()):
            rebound = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                          reason="phase change", binding=self.binding(self.run_b))
        self.assertEqual(rebound.binding, self.binding(self.run_b))
        self.assertEqual(rebound.roles, design.roles)
        self.assertEqual(staff.by_id(row_id), before)
        self.assertEqual(self.panes.world.locations, locations)
        self.assertEqual(len(self.panes.world.launches), launches)
        self.assertEqual(self.panes.world.stops, [])
        self.assertEqual(self.panes.world.closed, [])

    def test_last_role_end_closes_only_its_pane_and_preserves_design_evidence(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                       binding=self.binding(self.run_a))
        design_id = design.placement.design_id
        directory = design_roles.design_directory(design_id)
        _ = self.resume(design, "reviewer")
        message = design_roles.queue_message(design_id, "reviewer", "fixture", "Keep this evidence after end.")
        evidence = {path: path.read_bytes() for folder in ("prompts", "replies", "pending")
                    for path in (directory / folder).rglob("*") if path.is_file()}
        authority_before = self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                   "host": "natedev"})
        _ = self.panes.world.foreground.pop("w1:p1")
        _ = self.panes.world.foreground.pop("w1:p2")
        first = design_roles.end(design_id + "/designer")
        self.assertIsInstance(self.instance(first, "designer").lifecycle, design_roles.RoleEnded)
        self.assertIsInstance(self.instance(first, "reviewer").lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(self.panes.world.closed), 1)
        reviewer = cast(design_roles.RoleOpen, self.instance(first, "reviewer").lifecycle)
        self.assertIsNotNone(staff.by_id(reviewer.staff_id))
        ended = design_roles.end(design_id + "/reviewer")
        self.assertTrue(all(isinstance(instance.lifecycle, design_roles.RoleEnded) for instance in ended.roles))
        self.assertEqual(len(self.panes.world.closed), 2)
        self.assertEqual(set(self.panes.world.locations), {"w1:p0"})
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.panes.command_case.fixture.parent.id), [])
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertEqual(design_roles.pending_messages(design_id, "reviewer"), (message,))
        self.assertEqual(self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                 "host": "natedev"}), authority_before)


    def test_end_closes_the_worker_while_its_agent_status_is_working(self) -> None:
        design = self.open_designer()
        instance_id = design.placement.design_id + "/designer"
        self.panes.world.locations["w1:p1"]["agent_status"] = "working"
        ended = design_roles.end(instance_id)
        self.assertEqual(self.instance(ended, "designer").lifecycle,
                         design_roles.RoleEnded(staff.StaffId("9309c903-5894-501a-8aa7-f33ea912e074")))
        self.assertFalse((self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").exists())
        self.assertEqual(self.panes.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(design_roles.end(instance_id), ended)


    def test_showrunner_notifier_target_requires_a_live_local_session(self) -> None:

        design = self.open_designer()
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        self.assertEqual(parent.current_session, self.holder_session)
        with patch.object(staff.StaffSnapshot, 'by_id', return_value=None):
            with self.assertRaisesRegex(design_roles.RoleRefused, 'parent is unavailable'):
                _ = design_roles.worker_parent(design, 'designer', staff.snapshot())


if __name__ == "__main__":
    _ = unittest.main()
