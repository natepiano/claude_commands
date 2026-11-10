"""Test demotion with private staff files, Git worktrees, and fake launchers."""
from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.accounts import accounts
from scripts.herdr import herdr
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import add_unit, demote_showrunner as demote, promote_showrunner, session_staff, showrunners, staff, transfer_unit, unit_config, unit_effort, unit_lookup, update_registration
from scripts.production.production_doc import Production, read_production
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.agents import pane_record, remote, stop_resume


class DemoteShowrunnerTests(StaffTestCase):
    parent_name: str = "cargo-liner"
    child_name: str = "cargo-liner-style"
    child_branch: str = "cargo-liner-style"
    plan_relative: Path = Path("plan.md")
    port: unit_config.AppPort | unit_config.NoAppPort = unit_config.NoAppPort()
    style: bool = True
    checkout: Path = Path()
    child_tree: Path = Path()
    doc: Path = Path()
    child_doc: Path = Path()
    plan: Path = Path()
    events: list[str] = []
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.detach: mock.Mock = mock.Mock()
        self.timer: mock.Mock = mock.Mock()
        self.stop: mock.Mock = mock.Mock()
        self.start: mock.Mock = mock.Mock()
        self.place: mock.Mock = mock.Mock()
        self.account: mock.Mock = mock.Mock()
        self.observed: mock.Mock = mock.Mock()

    @cached_property
    def parent(self) -> staff.StaffRow:
        return fixture_member("showrunner", name=self.parent_name, doc=staff.LiveDoc(remote.local_machine(), self.doc))

    @cached_property
    def child(self) -> staff.StaffRow:
        row = fixture_member("showrunner", name=self.child_name, parent=self.parent.id,
                             doc=staff.LiveDoc(remote.local_machine(), self.child_doc))
        return staff.set_home(row.id, staff.SessionHome(self.root / "showrunner-account"))

    @cached_property
    def worker(self) -> staff.StaffRow:
        return fixture_member("worker", name="impl", parent=self.child.id)

    @cached_property
    def claude(self) -> unit_lookup.LiveClaude:
        return unit_lookup.LiveClaude("child-session", self.child.current_session, "fixture.sock", 4242)

    @cached_property
    def marked(self) -> unit_lookup.LabeledSession:
        return unit_lookup.LabeledSession(self.child_name, self.claude, herdr.HerdrPlace("fixture", "w1:p1"))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.root / "parent"
        self.checkout.mkdir()
        self.child_tree = self.root / self.child_branch
        self.doc = self.checkout / "production.md"
        self.child_doc = self.checkout / "child.md"
        self.plan = self.checkout / self.plan_relative
        self.plan.parent.mkdir(parents=True, exist_ok=True)
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "test@example.invalid"),
                     ("config", "user.name", "test"), ("config", "commit.gpgsign", "false")):
            _ = self.git(*args)
        for path, text in ((self.doc, self.document(self.checkout) +
                            "\n## Units\n\n| Unit | Owns |\n| --- | --- |\n| existing | retained |\n"),
                           (self.child_doc, self.document(self.child_tree, self.child_branch) +
                            ("- **Kind:** style\n" if self.style else "")), (self.plan, "# Unit plan\n")):
            write_text_atomic(path, text)
        _ = self.git("add", ".")
        _ = self.git("commit", "-q", "-m", "fixture")
        _ = self.git("worktree", "add", "-q", "-b", self.child_branch, str(self.child_tree))
        self.plan = self.child_tree / self.plan_relative
        _ = self.git("init", "--bare", "-q", str(self.root / "origin.git"))
        _ = self.git("remote", "add", "origin", str(self.root / "origin.git"))
        _ = self.worker
        self.events = []
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "state")})))
        _ = self.enterContext(mock.patch.object(showrunners, "own_name", return_value=self.parent_name))
        _ = self.enterContext(mock.patch.object(update_registration, "local_machine", side_effect=lambda: remote.local_machine()))
        self.observed = self.enterContext(mock.patch.object(unit_lookup, "observed_session_labels", side_effect=self.session_labels))
        _ = self.enterContext(mock.patch.object(promote_showrunner, "_session_status", return_value="idle"))
        _ = self.enterContext(mock.patch.object(promote_showrunner, "run_marker", return_value=self.root / "run-marker"))
        _ = self.enterContext(mock.patch.object(promote_showrunner, "read_place", return_value="fixture pane"))
        _ = self.enterContext(mock.patch.object(unit_effort, "verdict", return_value=unit_effort.Ready()))
        self.detach = self.enterContext(mock.patch.object(showrunners, "main", side_effect=self.detach_child))
        self.timer = self.enterContext(mock.patch.object(update_registration, "notifier", side_effect=self.timer_action))
        self.stop = self.enterContext(mock.patch.object(promote_showrunner, "stop_director", side_effect=self.stop_child))
        self.start = self.enterContext(mock.patch.object(add_unit, "main", side_effect=self.start_unit))
        self.place = self.enterContext(mock.patch.object(transfer_unit, "place_unit", side_effect=self.place_unit))
        self.account = self.enterContext(mock.patch.object(accounts, "account_for_group_launch",
                           return_value=accounts.NamedAccount("claude2", self.root / "units-account")))
        _ = self.enterContext(mock.patch.object(pane_record, "publication_for_session", return_value=None))
        _ = self.enterContext(mock.patch.object(update_registration.QuestionStaff, "read", side_effect=self.question_staff))

    @staticmethod
    def document(checkout: Path, branch: str = "main") -> str:
        return f"# Production\n\n- **Merge branch:** {branch}\n- **Showrunner checkout:** {checkout}\n- **Log:** log.md\n"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.checkout), *args], capture_output=True, text=True, check=True).stdout.strip()

    def session_labels(self, *, record: staff.StaffSnapshot) -> tuple[unit_lookup.LabeledSession, ...]:
        row = record.by_id(self.child.id)
        return (self.marked,) if row is not None and row.kind == "showrunner" else ()

    def retired_unit(self, name: str, branch: str) -> staff.StaffRow:
        tree = self.root / name
        _ = self.git("worktree", "add", "-q", "--detach", str(tree), self.child_branch)
        row = fixture_member("unit", name=name, parent=self.child.id,
                             worktree=staff.PresentWorktree(remote.local_machine(), tree))
        _ = unit_config.write(tree, unit_config.UnitConfig(unit_config.PlanFile(Path("old.md")), unit_config.AppPort(9000), ()))
        _ = unit_config.park(row.id, tree, unit_config.RetiredSession("2026-11-09", row.current_session, unit_config.Branch(branch)))
        _ = self.git("worktree", "remove", "--force", str(tree))
        return staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), tree), expected=row.worktree)

    def question_staff(self) -> update_registration.QuestionStaff:
        return update_registration.QuestionStaff.for_record(staff.snapshot(), session_staff.MemberSources(()))

    def detach_child(self, args: list[str]) -> int:
        self.assertEqual(args, ["detach", self.child_name])
        self.events.append("detach")
        _ = staff.move(self.child.id, staff.STAFF_ROOT)
        return 0

    def timer_action(self, args: list[str]) -> str:
        self.assertEqual(args, ["stop", f"showrunner-{self.child.id}"])
        self.events.append("timer")
        return ""

    def stop_child(self, name: str, marked: unit_lookup.LabeledSession, claude: unit_lookup.LiveClaude) -> bool:
        self.assertEqual((name, marked, claude), (self.child_name, self.marked, self.claude))
        self.events.append("stop")
        return True

    def start_unit(self, args: list[str]) -> int:
        self.events.append("start")
        self.assertEqual(args, ["--production", self.parent_name, self.child_name, "--plan", str(self.plan_relative),
                               "--resume", self.child.current_session, "--cwd", str(self.child_tree),
                               "--config-dir", str(self.root / "units-account"),
                               *(["--port", str(self.port.number)] if isinstance(self.port, unit_config.AppPort) else [])])
        self.assertEqual(staff.by_id(self.child.id), replace(self.child, kind="unit", parent=staff.ReportsTo(self.parent.id),
                         doc=staff.DOC_UNFILLED, worktree=staff.PresentWorktree(remote.local_machine(), self.child_tree)))
        config = unit_config.read(self.child_tree)
        assert isinstance(config, unit_config.UnitConfig)
        self.assertEqual(config.plan, unit_config.PlanFile(self.plan_relative))
        self.assertEqual(config.port, self.port)
        self.assertEqual(self.git("-C", str(self.child_tree), "branch", "--show-current"), self.child_branch)
        return 0

    def place_unit(self, source: str, request: transfer_unit.UnitTransfer) -> None:
        self.events.append("place")
        self.assertEqual((source, request.member.id, request.parent.id), (self.child_name, self.child.id, self.parent.id))

    def run_command(self, *extra: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            result = demote.main([self.child_name, "--plan", str(self.plan),
                                  *(["--port", str(self.port.number)] if isinstance(self.port, unit_config.AppPort) else []), *extra])
        return result, output.getvalue(), errors.getvalue()

    def journal(self) -> Path:
        return demote.record_path(read_production(self.parent), self.child_name)

    def assert_no_actions(self, before: bytes) -> None:
        self.assertEqual(self.events, [])
        self.account.assert_not_called()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertFalse(self.journal().exists())

    def test_name_only_uses_the_child_plan_and_keeps_explicit_values(self) -> None:
        original_doc = self.child_doc
        for source in ("configuration", "production document"):
            if source == "configuration":
                _ = unit_config.write(self.child_tree, unit_config.UnitConfig(
                    unit_config.PlanFile(self.plan_relative), self.port, ()))
                expected = self.plan
            else:
                (self.child_tree / ".unit.toml").unlink()
                self.child_doc = self.child_tree / "child.md"
                _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), self.child_doc))
                expected = self.child_doc
            for flags in ((), ("--plan", str(self.plan), "--port", "8124")):
                requests: list[demote.Demotion] = []
                checked = demote.checked
                def capture(parent: Production, name: str, plan: Path | unit_config.PlanDefault,
                            record: staff.StaffSnapshot, port: unit_config.AppPort | unit_config.NoAppPort) -> demote.Demotion:
                    request = checked(parent, name, plan, record, port)
                    requests.append(request)
                    return request
                with (self.subTest(source=source, flags=flags),
                      mock.patch.object(demote, "checked", side_effect=capture),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    code = demote.main([self.child_name, "--check", *flags])
                    self.assertEqual(code, 0, errors.getvalue())
                    self.assertEqual(requests[0].plan, self.plan if flags else expected)
                    self.assertEqual(requests[0].port, unit_config.AppPort(8124) if flags else
                                     self.port if source == "configuration" else unit_config.NoAppPort())
        self.child_doc = original_doc
        _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), original_doc))
        _ = unit_config.write(self.child_tree, unit_config.UnitConfig(
            unit_config.PlanFile(self.plan_relative), self.port, ()))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = demote.main([self.child_name])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(self.events, ["detach", "timer", "stop", "start", "place"])

    def test_name_only_without_a_plan_reports_one_refusal_line(self) -> None:
        with redirect_stderr(io.StringIO()) as errors:
            code = demote.main([self.child_name, "--check"])
        self.assertEqual(code, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("plan", errors.getvalue())
        self.assertIn(str(self.child_doc), errors.getvalue())
        self.assertEqual(self.events, [])

    def test_check_stops_nothing_and_writes_nothing(self) -> None:
        before = self.staff_file.read_bytes()
        _ = self.git("rm", str(self.plan_relative))
        _ = self.git("commit", "-q", "-m", "keep the unit plan on the child branch")
        for current_has_plan in (True, False):
            if not current_has_plan:
                _ = self.git("-C", str(self.child_tree), "switch", "-c", "without-plan")
                _ = self.git("-C", str(self.child_tree), "rm", "--", str(self.plan_relative))
                _ = self.git("-C", str(self.child_tree), "commit", "-q", "-m", "remove the plan from the current branch")
            for plan in (self.plan, self.plan_relative):
                with self.subTest(current_has_plan=current_has_plan, plan=plan):
                    self.assertEqual(self.run_command("--check", "--plan", str(plan))[0], 0)
                    self.assert_no_actions(before)

    def test_refuses_a_child_outside_the_callers_production(self) -> None:
        _ = staff.move(self.child.id, staff.STAFF_ROOT)
        before = self.staff_file.read_bytes()
        code, _, errors = self.run_command()
        self.assertEqual(code, 2)
        self.assertIn("not attached", errors)
        self.assert_no_actions(before)

    def test_refuses_a_dirty_child_worktree(self) -> None:
        write_text_atomic(self.child_tree / "dirty.md", "unfinished\n")
        before = self.staff_file.read_bytes()
        self.assertEqual(self.run_command()[0], 2)
        self.assert_no_actions(before)

    def test_refuses_busy_forms_prompts_and_drafts(self) -> None:
        before = self.staff_file.read_bytes()
        with mock.patch.object(promote_showrunner, "_session_status", return_value="busy"):
            self.assertEqual(self.run_command()[0], 2)
        for reason in ("form", "prompt", "draft"):
            with self.subTest(reason=reason), mock.patch.object(unit_effort, "verdict", return_value=unit_effort.Skipped(reason)):
                self.assertEqual(self.run_command()[0], 2)
        self.assert_no_actions(before)

    def test_refuses_an_uncommitted_plan(self) -> None:
        before = self.staff_file.read_bytes()
        original = self.plan.read_text()
        untracked = self.child_tree / "untracked-plan.md"
        branch_only = self.child_tree / "temporary-plan.md"
        _ = self.git("-C", str(self.child_tree), "switch", "-c", "temporary-plan-branch")
        write_text_atomic(branch_only, "# Temporary plan\n")
        _ = self.git("-C", str(self.child_tree), "add", "--", branch_only.name)
        _ = self.git("-C", str(self.child_tree), "commit", "-q", "-m", "commit the temporary plan")
        for plan in (self.plan, untracked, self.checkout / self.plan_relative,
                     self.child_tree / ".." / "parent" / self.plan_relative, branch_only):
            with self.subTest(plan=plan):
                if plan in (self.plan, untracked):
                    write_text_atomic(plan, "changed\n")
                try:
                    for flags in ((), ("--check",)):
                        with self.subTest(flags=flags):
                            code, _, errors = self.run_command(*flags, "--plan", str(plan))
                            self.assertEqual(code, 2)
                            if plan == branch_only:
                                self.assertIn(f"plan {branch_only.name} is missing on {self.child_branch}", errors)
                            elif plan not in (self.plan, untracked):
                                self.assertIn("plan must be inside the child checkout", errors)
                            self.assert_no_actions(before)
                finally:
                    if plan == self.plan:
                        write_text_atomic(self.plan, original)
                    elif plan == untracked:
                        untracked.unlink()

    def test_refuses_live_units_and_accepts_retired_units(self) -> None:
        rows = [fixture_member("unit", name=name, parent=self.child.id) for name in ("alpha", "beta")]
        before = self.staff_file.read_bytes()
        code, _, errors = self.run_command("--check")
        self.assertEqual(code, 2, errors)
        for row in rows:
            self.assertIn(row.name, errors)
        self.assert_no_actions(before)
        for row in rows:
            _ = self.retired_unit(row.name, "missing-" + row.name)
        before = self.staff_file.read_bytes()
        code, _, errors = self.run_command("--check")
        self.assertEqual(code, 0, errors)
        self.assert_no_actions(before)

    def test_refuses_unmerged_branches_and_accepts_merged_or_missing_branches(self) -> None:
        commit = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "unmerged work")
        for name in ("handoff-a", "handoff-b"):
            _ = self.git("branch", name, commit)
            _ = self.retired_unit(name, name)
        before = self.staff_file.read_bytes()
        for remote in (False, True):
            with self.subTest(remote=remote):
                if remote:
                    _ = self.git("push", "origin", "handoff-a", "handoff-b")
                    for name in ("handoff-a", "handoff-b"):
                        _ = self.git("branch", "-D", name)
                code, _, errors = self.run_command("--check")
                self.assertEqual(code, 2, errors)
                for name in ("handoff-a", "handoff-b"):
                    self.assertIn(name, errors)
                self.assert_no_actions(before)
        _ = self.git("-C", str(self.child_tree), "merge", "--ff-only", commit)
        self.assertEqual(self.run_command("--check")[0], 0)
        _ = self.git("push", "origin", "--delete", "handoff-a", "handoff-b")
        self.assertEqual(self.run_command("--check")[0], 0)
        self.assert_no_actions(before)

    def test_unknown_retirement_facts_print_one_anomaly_without_refusal(self) -> None:
        row = fixture_member("unit", name="unknown", parent=self.child.id,
                             worktree=staff.RemovedWorktree(remote.local_machine(), self.root / "missing"))
        before = self.staff_file.read_bytes()
        code, _, errors = self.run_command("--check")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertIn(f"anomaly: {row.name}:", errors)
        self.assert_no_actions(before)

    def test_unknown_origin_prints_one_anomaly_without_refusal(self) -> None:
        _ = self.retired_unit("unknown", "missing-branch")
        _ = self.git("remote", "set-url", "origin", str(self.root / "unavailable.git"))
        before = self.staff_file.read_bytes()
        code, _, errors = self.run_command("--check")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertIn("anomaly: unknown:", errors)
        self.assert_no_actions(before)

    def test_absent_update_timer_counts_as_stopped(self) -> None:
        self.timer.side_effect = update_registration.RegistrationFailure("notifier", "fixture missing timer")
        with mock.patch.object(update_registration, "instance", return_value=update_registration.InstanceAbsent()) as instance:
            self.assertEqual(self.run_command()[0], 0)
        instance.assert_called_once_with(f"showrunner-{self.child.id}")
        self.start.assert_called_once()

    def test_other_timer_failures_keep_the_demotion_pending(self) -> None:
        self.timer.side_effect = update_registration.RegistrationFailure("notifier", "fixture stop failed")
        for status in (update_registration.InstancePresent("running"),
                       update_registration.RegistrationFailure("notifier", "fixture status failed")):
            with self.subTest(status=status), mock.patch.object(update_registration, "instance") as instance:
                if isinstance(status, update_registration.RegistrationFailure):
                    instance.side_effect = status
                else:
                    instance.return_value = status
                self.assertEqual(self.run_command()[0], 1)
                self.assertEqual(cast(dict[str, object], json.loads(self.journal().read_text()))["step"], demote.Step.DETACHED)
                self.stop.assert_not_called()
                self.start.assert_not_called()
        self.detach.assert_called_once()

    def test_refuses_the_parent_branch_before_any_change(self) -> None:
        _ = self.git("switch", "-c", "wrong-parent-branch")
        before = self.staff_file.read_bytes()
        for flags in (("--check",), ()):
            self.assertEqual(self.run_command(*flags)[0], 2)
            self.assert_no_actions(before)

    def test_pending_demotion_checks_the_parent_branch_before_any_remaining_step(self) -> None:
        self.stop.side_effect = [False, True]
        self.assertEqual(self.run_command()[0], 1)
        before = self.staff_file.read_bytes(), self.journal().read_bytes(), list(self.events)
        _ = self.git("switch", "-c", "wrong-parent-branch")
        for flags in (("--check",), ()):
            self.assertEqual(self.run_command(*flags)[0], 2)
            self.assertEqual((self.staff_file.read_bytes(), self.journal().read_bytes(), self.events), before)
        self.assertEqual(self.stop.call_count, 1)

    def test_demotes_the_same_staff_row_and_commits_the_unit_row(self) -> None:
        _ = self.git("rm", str(self.plan_relative))
        _ = self.git("commit", "-q", "-m", "keep the unit plan on the child branch")
        _ = self.retired_unit("old", "deleted-branch")
        _ = self.git("-C", str(self.child_tree), "switch", "-c", "temporary-branch")
        fields: dict[str, object] = {}
        for count, saved_completion in enumerate((False, True), start=1):
            with self.subTest(saved_completion=saved_completion):
                if saved_completion:
                    _ = staff.change_director(self.child.id, staff.ShowrunnerRole(self.child_name, self.parent.id, staff.LiveDoc(remote.local_machine(), self.child_doc)))
                    demote.save(self.journal(), fields, demote.Step.PLACED)
                with (mock.patch.object(demote, "save", wraps=demote.save) as save,
                      mock.patch.object(demote, "checked", wraps=demote.checked) as checked):
                    plan = self.plan_relative if saved_completion else self.plan
                    code, _, errors = self.run_command("--plan", str(plan))
                self.assertEqual(code, 0, errors)
                checked.assert_called_once()
                fields = cast(dict[str, object], save.call_args.args[1])
                self.assertEqual(self.events, ["detach", "timer", "stop", "start", "place"] * count)
                self.assertEqual(self.account.call_count, count)
                self.account.assert_called_with("unit", resume=True)
                self.assertEqual(staff.by_id(self.worker.id), self.worker)
                document = self.doc.read_text()
                self.assertIn(f"| {self.child_name} |  | {self.child_tree} | {self.child_branch} | {self.plan_relative} |", document)
                if isinstance(self.port, unit_config.AppPort):
                    self.assertIn(f"| {self.port.number} |", document)
                else:
                    self.assertNotIn("Port", document)
                self.assertIn("| existing | retained |", document)
                self.assertEqual(self.git("show", "main:production.md"), document.strip())
                self.assertEqual(self.git("status", "--porcelain"), "")
                self.assertFalse(self.journal().exists())
                self.assertEqual(self.run_command()[0], 2)
                self.assertEqual(self.events, ["detach", "timer", "stop", "start", "place"] * count)

    def test_moves_open_questions_once_and_preserves_reminders(self) -> None:
        child = read_production(self.child)
        parent = read_production(self.parent)
        identities = self.question_staff()
        question: update_registration.OutstandingItem = {"since": "2026-11-09T10:00", "text": "Choose a value",
            "question": "Which value?", "asked_by": self.child.id, "urgency": "normal"}
        reminder: update_registration.OutstandingItem = {"since": "2026-11-09T10:01", "text": "Keep this reminder"}
        update_registration.write_open_entries(child, [question, reminder], identities)
        self.stop.return_value = False
        self.stop.side_effect = None
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(update_registration.open_entries(child, identities), [reminder])
        moved = update_registration.open_entries(parent, identities)
        self.assertEqual(moved, [question])
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(update_registration.open_entries(parent, identities), moved)
        self.detach.assert_called_once()
        self.timer.assert_called_once()

    def test_rerun_resumes_each_completed_step(self) -> None:
        self.stop.side_effect = [False, True]
        self.assertEqual(self.run_command("--plan", str(self.plan_relative))[0], 1)
        self.assertEqual(cast(dict[str, object], json.loads(self.journal().read_text()))["step"], demote.Step.QUESTIONS_MOVED)
        self.assertEqual(self.run_command()[0], 0)
        self.detach.assert_called_once()
        self.timer.assert_called_once()
        self.assertEqual(self.stop.call_count, 2)
        self.start.assert_called_once()

    def test_document_failure_keeps_the_converted_row_for_a_rerun(self) -> None:
        with mock.patch.object(demote, "unit_document", side_effect=OSError("fixture commit failed")):
            self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(cast(dict[str, object], json.loads(self.journal().read_text()))["step"], demote.Step.CONVERTED)
        self.assertEqual(self.run_command()[0], 0)
        self.stop.assert_called_once()
        self.start.assert_called_once()

    def test_restart_failure_prints_one_restore_command_and_retries_only_the_restart(self) -> None:
        self.start.side_effect = None
        self.start.return_value = 2
        code, _, errors = self.run_command()
        self.assertEqual(code, 1)
        self.assertEqual(errors.count(demote.restore_line(self.journal())), 1)
        self.assertEqual(cast(dict[str, object], json.loads(self.journal().read_text()))["step"], demote.Step.DOCUMENTED)
        pending = self.journal().read_text()
        self.account.return_value = accounts.NamedAccount("claude3", self.root / "current-units-account")
        self.start.return_value = 0
        self.observed.side_effect = None
        for count, (live, opening, flag) in enumerate(((False, False, "--resume"),
                                                     (True, True, "--resume"),
                                                     (True, False, "--attach")), start=2):
            with self.subTest(live=live, opening=opening):
                write_text_atomic(self.journal(), pending)
                self.observed.return_value = (self.marked,) if live else ()
                with mock.patch.object(add_unit, "pending_opening", return_value=opening):
                    self.assertEqual(self.run_command()[0], 0)
                self.assertEqual(self.start.call_count, count)
                arguments = cast(list[str], self.start.call_args.args[0])
                self.assertEqual(arguments[arguments.index(flag) + 1], self.child.current_session)
                self.assertEqual(arguments[arguments.index("--config-dir") + 1], str(self.root / "current-units-account"))
                self.assertFalse(self.journal().exists())
        self.stop.assert_called_once()
        self.detach.assert_called_once()

    def test_pane_placement_failure_does_not_repeat_the_restart(self) -> None:
        self.place.side_effect = OSError("fixture placement failed")
        self.assertEqual(self.run_command()[0], 1)
        self.place.side_effect = self.place_unit
        self.assertEqual(self.run_command()[0], 0)
        self.start.assert_called_once()

    def test_restore_reuses_the_original_role_account_and_conversation(self) -> None:
        self.start.side_effect = None
        self.start.return_value = 2
        self.assertEqual(self.run_command()[0], 1)
        for outcome in (herdr.AgentNotStarted("fixture start failed"), herdr.PromptSubmissionNotRequired()):
            with (self.subTest(outcome=outcome),
                  mock.patch.object(stop_resume, "stop", return_value=stop_resume.NotRunning(self.child.current_session)) as stop,
                  mock.patch.object(herdr, "shared_session", return_value="fixture"),
                  mock.patch.object(herdr, "launch_agent", return_value=herdr.AgentLaunched("w1:p2", outcome)) as start,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(demote.restore(self.journal()), 1 if isinstance(outcome, herdr.AgentNotStarted) else 0)
                stop.assert_called_once_with(self.child.current_session)
                self.assertEqual(staff.by_id(self.child.id), self.child)
                args = start.call_args.args
                self.assertEqual(args[:4], ("fixture", self.child_name, "child-session", str(self.child_tree)))
                self.assertEqual(cast(dict[str, str], args[4])["CLAUDE_CONFIG_DIR"], str(self.root / "showrunner-account"))
                self.assertIn(self.child.current_session, cast(list[str], args[6]))
                if isinstance(outcome, herdr.AgentNotStarted):
                    self.assertIn("fixture start failed", errors.getvalue())
                    self.assertTrue(self.journal().exists())
                    self.assertEqual(self.timer.call_count, 1)
                else:
                    self.assertFalse(self.journal().exists())


@final
class PerformanceDemotionTests(DemoteShowrunnerTests):
    parent_name: str = "hana"
    child_name: str = "fps"
    child_branch: str = "tool-based-ui-frame-time"
    plan_relative: Path = Path("docs/hana/performance-plan.md")
    port: unit_config.AppPort | unit_config.NoAppPort = unit_config.AppPort(8123)
    style: bool = False


if __name__ == "__main__":
    _ = unittest.main()
