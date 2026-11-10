"""Session labels use the native runtime id and never infer a row from a folder."""
from __future__ import annotations

from dataclasses import replace
import io
import json
from contextlib import redirect_stderr
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.agents import remote

from scripts.production import session_labels, session_staff, staff, unit_config
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class NativeSessionLabelTests(StaffTestCase):
    def test_labels_use_staff_without_reading_an_unreadable_timer_document(self) -> None:
        fixture = self.row_production()
        fixture.doc.unlink()
        errors = io.StringIO()
        with redirect_stderr(errors):
            labels = session_labels.StaffLabels.for_record(staff.snapshot())
        self.assertEqual(labels.environment({}, fixture.unit.current_session),
                         {"CLAUDE_CODE_SESSION_ID": fixture.unit.current_session})
        self.assertEqual(errors.getvalue(), "")

    def test_runtime_identity_selects_the_current_member_and_removes_copied_labels(self) -> None:
        _, unit, worker = self.tree()
        current = staff.native_id(fixture_runtime_id("label-unit-after-clear"))
        unit = staff.update_current_session(unit.current_session, current)
        self.assertNotEqual(unit.id, unit.current_session)
        labels = session_labels.StaffLabels(staff.snapshot())
        environment = {"STAFF_ID": worker.id, "SHOWRUNNER_" + "UNIT_ID": "models"}
        selected = labels.environment(environment, unit.current_session)
        self.assertEqual(selected, {"CLAUDE_CODE_SESSION_ID": unit.current_session})
        self.assertEqual(labels.environment(environment, worker.current_session),
                         {"CLAUDE_CODE_SESSION_ID": worker.current_session})
        self.assertEqual(labels.environment(environment, unit.id), {})

    def test_retained_reference_projects_the_current_session_without_copied_identity(self) -> None:
        from scripts.agents import pane_record
        from scripts.agents.launch_names import LaunchNames

        fixture = self.row_production()
        self.assertNotEqual(fixture.unit.id, fixture.unit.current_session)
        names = LaunchNames(staff.snapshot())
        launch = pane_record.launch("w1:p1", fixture.unit.id, str(fixture.worktree),
            {"STAFF_ID": fixture.unit.id, "SHOWRUNNER_" + "UNIT": "old", "ENABLE_TOOL_SEARCH": "true"},
            ["claude"])
        before = self.staff_file.read_bytes()
        selected = names.environment(launch)
        self.assertEqual(selected, {"CLAUDE_CODE_SESSION_ID": fixture.unit.current_session,
                                    "ENABLE_TOOL_SEARCH": "true"})
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unknown_runtime_is_not_assigned_by_its_unique_folder_or_display_name(self) -> None:
        runner, unit, _ = self.tree()
        located = replace(unit, worktree=staff.PresentWorktree("natedev", self.root))
        labels = session_labels.StaffLabels(staff.StaffSnapshot((runner, located)))
        before = self.staff_file.read_bytes()
        selected = labels.environment({"STAFF_ID": unit.id}, fixture_runtime_id("unpublished"))
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", selected)
        self.assertNotIn("STAFF_ID", selected)
        self.assertFalse(any(key.startswith("SHOWRUNNER_") for key in selected))
        self.assertEqual(self.staff_file.read_bytes(), before)


class RowResolverTests(StaffTestCase):
    def test_retired_worker_keeps_its_fixed_inbox_path_for_cleanup_retries(self) -> None:
        worker = self.tree()[2]
        before = session_staff.inbox_path(worker.id)
        before.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(before, json.dumps({"messages": ["cleanup evidence"]}))
        evidence = before.read_bytes()
        retired = staff.retire(worker.id)
        self.assertEqual(retired, worker)
        self.assertIsNone(staff.by_id(worker.id))
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                after = session_staff.inbox_path(worker.id)
                self.assertEqual(after, before)
                self.assertEqual(after.read_bytes(), evidence)

    def test_unit_and_worker_read_the_row_location_config_and_git_from_one_snapshot(self) -> None:
        fixture = self.row_production()
        record = staff.snapshot()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        resolver = session_staff.RowResolver(record)
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("unexpected staff read")):
            self.assertEqual(resolver.unit_worktree(fixture.unit), fixture.unit.worktree)
            self.assertEqual(resolver.unit_worktree(fixture.worker), fixture.unit.worktree)
            self.assertEqual(resolver.unit_branch(fixture.unit), session_staff.Branch("branch-from-git"))
            self.assertEqual(resolver.unit_config(fixture.unit), unit_config.UnitConfig(
                unit_config.PlanFile(Path("docs/plans/from-config.md")), unit_config.AppPort(4317),
                ("shared-fixture",)))
            self.assertEqual(resolver.unit_config(fixture.worker), resolver.unit_config(fixture.unit))
            self.assertEqual(resolver.production_doc(fixture.unit),
                             session_staff.ProductionDoc("natedev", fixture.doc, fixture.runner, "# Production\n"))
            self.assertEqual(resolver.merge_branch(fixture.unit), session_staff.Branch("merge-from-git"))
            self.assertIsInstance(resolver.unit_state(fixture.unit), session_staff.ActiveUnit)
            self.assertEqual(resolver.member_by_folder("natedev", fixture.worktree),
                             session_staff.MemberFound(fixture.unit))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_a_showrunner_held_worker_has_no_unit_configuration(self) -> None:
        fixture = self.row_production()
        worker = replace(fixture.worker, parent=staff.ReportsTo(fixture.runner.id))
        resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, fixture.unit, worker)))
        self.assertIsInstance(resolver.unit_config(worker), session_staff.NoUnitConfig)
        self.assertEqual(resolver.production_doc(worker),
                         session_staff.ProductionDoc("natedev", fixture.doc, fixture.runner, "# Production\n"))

    def test_unfilled_locations_keep_explicit_incomplete_outcomes(self) -> None:
        runner, unit, worker = self.tree()
        resolver = session_staff.RowResolver(staff.snapshot())
        self.assertIsInstance(resolver.unit_worktree(unit), staff.WorktreeUnfilled)
        self.assertIsInstance(resolver.unit_branch(unit), session_staff.LocationUnfilled)
        self.assertIsInstance(resolver.production_doc(runner), session_staff.LocationUnfilled)
        self.assertIsInstance(resolver.production_doc(unit), session_staff.LocationUnfilled)
        self.assertIsInstance(resolver.production_doc(worker), session_staff.LocationUnfilled)

    def test_removed_worktree_reads_only_parked_branch_and_configuration_without_cleanup(self) -> None:
        fixture = self.row_production()
        removed = replace(fixture.unit, worktree=staff.RemovedWorktree("natedev", fixture.worktree))
        parked = unit_config.parked_path(removed.id)
        parked.parent.mkdir(parents=True)
        write_text_atomic(parked, 'plan = "permanent"\nport = 8712\nresponsibilities = ["parked"]\n'
                          + '[retired]\ndate = "2026-11-01"\n'
                          + f'session = "{removed.current_session}"\nfolder = "{fixture.worktree}"\n'
                          + 'branch = "parked-branch"\n')
        abandoned = parked.with_name(f".{parked.name}.99999999.fixture.tmp")
        write_text_atomic(abandoned, "retained recovery bytes")
        before = parked.read_bytes(), parked.stat().st_mtime_ns, abandoned.read_bytes()
        resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, removed)))
        with mock.patch.object(unit_config, "read", side_effect=AssertionError("removed worktree read")):
            self.assertEqual(resolver.unit_branch(removed), session_staff.Branch("parked-branch"))
            config = resolver.unit_config(removed)
        self.assertIsInstance(config, unit_config.UnitConfig)
        assert isinstance(config, unit_config.UnitConfig)
        self.assertEqual(config.plan, unit_config.PermanentPlan())
        self.assertEqual(config.port, unit_config.AppPort(8712))
        self.assertEqual(config.responsibilities, ("parked",))
        self.assertIsInstance(resolver.unit_state(removed), session_staff.RetiredUnit)
        self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns, abandoned.read_bytes()), before)

    def test_missing_or_malformed_parked_facts_never_use_the_present_folder(self) -> None:
        fixture = self.row_production()
        removed = replace(fixture.unit, worktree=staff.RemovedWorktree("natedev", fixture.worktree))
        path = unit_config.parked_path(removed.id)
        path.parent.mkdir(parents=True)
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                if malformed:
                    write_text_atomic(path, "plan = [invalid")
                resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, removed)))
                branch = resolver.unit_branch(removed)
                self.assertIsInstance(branch, session_staff.RetiredFactsUnavailable)
                assert isinstance(branch, session_staff.RetiredFactsUnavailable)
                self.assertIn(str(path), branch.reason)
                self.assertNotEqual(branch, session_staff.Branch("branch-from-git"))

    def test_an_unreadable_git_worktree_is_not_a_confirmed_retirement(self) -> None:
        fixture = self.row_production()
        missing = self.root / "missing-repository"
        unit = replace(fixture.unit, worktree=staff.PresentWorktree("natedev", missing))
        resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, unit)))
        result = resolver.unit_branch(unit)
        self.assertIsInstance(result, session_staff.BranchUnreadable)
        assert isinstance(result, session_staff.BranchUnreadable)
        self.assertIn(str(missing), result.reason)
        self.assertIsInstance(resolver.unit_state(unit), session_staff.ActiveUnit)

    def test_a_missing_production_doc_reports_unavailable_instead_of_using_a_timer_doc(self) -> None:
        fixture = self.row_production()
        missing = self.root / "missing-production.md"
        runner = replace(fixture.runner, doc=staff.LiveDoc("natedev", missing))
        resolver = session_staff.RowResolver(staff.StaffSnapshot((runner, fixture.unit)))
        result = resolver.production_doc(fixture.unit)
        self.assertIsInstance(result, session_staff.DocumentUnavailable)
        assert isinstance(result, session_staff.DocumentUnavailable)
        self.assertIn(str(missing), result.reason)

    def test_folder_membership_never_matches_another_machine_or_a_similar_name(self) -> None:
        fixture = self.row_production()
        resolver = session_staff.RowResolver(staff.snapshot())
        self.assertIsInstance(resolver.member_by_folder("mac", fixture.worktree), session_staff.NoSuchMember)
        self.assertIsInstance(resolver.member_by_folder("natedev", fixture.worktree.parent / fixture.unit.name),
                              session_staff.NoSuchMember)

    def test_two_mac_units_share_one_transport_read_for_branches_config_and_document(self) -> None:
        fixture = self.row_production()
        second_folder = fixture.worktree.parent / "second-folder"
        _ = self.fixture_git(fixture.worktree, "worktree", "add", "-b", "second-git-branch", str(second_folder))
        write_text_atomic(second_folder / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        first = replace(fixture.unit, worktree=staff.PresentWorktree("mac", fixture.worktree))
        second = replace(first, id=staff.StaffId(fixture_runtime_id("second-fixed")),
                         current_session=staff.native_id(fixture_runtime_id("second-current")), name="second",
                         worktree=staff.PresentWorktree("mac", second_folder))
        runner = replace(fixture.runner, doc=staff.LiveDoc("mac", fixture.doc))
        real_run = subprocess.run
        transports: list[list[str]] = []

        def transport(arguments: list[str], *, input: str, capture_output: bool,
                      text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac"])
            transports.append(arguments)
            command = shlex.split(arguments[6])
            return real_run([sys.executable, *command[1:]], input=input, capture_output=capture_output,
                            text=text, check=check)

        resolver = session_staff.RowResolver(staff.StaffSnapshot((runner, first, second)))
        with (mock.patch.object(remote, "local_machine", return_value="natedev"),
              mock.patch.object(remote, "other_machine", return_value="mac"),
              mock.patch.object(subprocess, "run", side_effect=transport)):
            self.assertEqual(resolver.unit_branch(first), session_staff.Branch("branch-from-git"))
            self.assertEqual(resolver.unit_branch(second), session_staff.Branch("second-git-branch"))
            config = resolver.unit_config(second)
            self.assertIsInstance(config, unit_config.UnitConfig)
            assert isinstance(config, unit_config.UnitConfig)
            self.assertEqual(config.plan, unit_config.StandbyPlan())
            self.assertEqual(resolver.production_doc(first),
                             session_staff.ProductionDoc("mac", fixture.doc, runner, "# Production\n"))
            self.assertEqual(resolver.merge_branch(first), session_staff.Branch("merge-from-git"))
        self.assertEqual(len(transports), 1)

    def test_failed_mac_transport_keeps_uncertainty_distinct_from_absent_files(self) -> None:
        fixture = self.row_production()
        runner = replace(fixture.runner, doc=staff.LiveDoc("mac", fixture.doc))
        unit = replace(fixture.unit, worktree=staff.PresentWorktree("mac", fixture.worktree))
        resolver = session_staff.RowResolver(staff.StaffSnapshot((runner, unit)))
        failed = subprocess.CompletedProcess(["ssh"], 255, "", "connection refused")
        with (mock.patch.object(remote, "local_machine", return_value="natedev"),
              mock.patch.object(remote, "other_machine", return_value="mac"),
              mock.patch.object(subprocess, "run", return_value=failed) as calls):
            branch = resolver.unit_branch(unit)
            doc = resolver.production_doc(unit)
            with self.assertRaisesRegex(unit_config.ConfigRefused, "connection refused"):
                _ = resolver.unit_config(unit)
        self.assertIsInstance(branch, session_staff.BranchUnreadable)
        assert isinstance(branch, session_staff.BranchUnreadable)
        self.assertIn("connection refused", branch.reason)
        self.assertIsInstance(doc, session_staff.DocumentUnavailable)
        assert isinstance(doc, session_staff.DocumentUnavailable)
        self.assertIn("connection refused", doc.reason)
        self.assertEqual(calls.call_count, 1)

    def test_stored_references_keep_the_member_after_a_session_change_without_a_name_match(self) -> None:
        fixture = self.row_production()
        record = staff.snapshot()
        selector = session_staff.ObservedUnit("unrelated", session_staff.NoSession())
        self.assertEqual(session_staff.unit_match(selector, record,
                         session_staff.StoredReference(fixture.unit.id)), session_staff.MemberFound(fixture.unit))
        self.assertEqual(session_staff.unit_match(selector, record,
                         session_staff.StoredReference(fixture.unit.current_session)),
                         session_staff.MemberFound(fixture.unit))
        self.assertEqual(session_staff.unit_match(selector, record,
                         session_staff.LiveSession(fixture.unit.id)), session_staff.NoSuchMember(selector))
        self.assertEqual(session_staff.unit_match(selector, record,
                         session_staff.LiveSession(fixture.unit.current_session)),
                         session_staff.MemberFound(fixture.unit))

    def test_missing_or_malformed_live_configuration_never_uses_legacy_table_metadata(self) -> None:
        fixture = self.row_production()
        path = fixture.worktree / ".unit.toml"
        path.unlink()
        resolver = session_staff.RowResolver(staff.snapshot())
        missing = resolver.unit_config(fixture.unit)
        self.assertIsInstance(missing, unit_config.ConfigMissing)
        assert isinstance(missing, unit_config.ConfigMissing)
        self.assertIn(str(path), missing.reason)
        write_text_atomic(path, 'plan = "standby"\nport = "invalid"\nresponsibilities = []\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        with self.assertRaisesRegex(unit_config.ConfigRefused, "port"):
            _ = resolver.unit_config(fixture.unit)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_parked_configuration_without_retirement_facts_never_reads_the_removed_folder_branch(self) -> None:
        fixture = self.row_production()
        removed = replace(fixture.unit, worktree=staff.RemovedWorktree("natedev", fixture.worktree))
        path = unit_config.parked_path(removed.id)
        path.parent.mkdir(parents=True)
        write_text_atomic(path, 'plan = "standby"\nresponsibilities = []\n')
        resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, removed)))
        self.assertIsInstance(resolver.unit_branch(removed), session_staff.RetiredFactsUnavailable)

    def test_removed_configuration_requires_parked_retirement_facts(self) -> None:
        fixture = self.row_production()
        removed = replace(fixture.unit, worktree=staff.RemovedWorktree("natedev", fixture.worktree))
        path = unit_config.parked_path(removed.id)
        path.parent.mkdir(parents=True)
        write_text_atomic(path, 'plan = "standby"\nresponsibilities = []\n')
        before = path.read_bytes(), self.staff_file.read_bytes()
        resolver = session_staff.RowResolver(staff.StaffSnapshot((fixture.runner, removed)))
        with mock.patch.object(unit_config, "read", side_effect=AssertionError("removed folder read")):
            result = resolver.unit_config(removed)
        self.assertIsInstance(result, session_staff.RetiredFactsUnavailable)
        assert isinstance(result, session_staff.RetiredFactsUnavailable)
        self.assertIn("retirement facts", result.reason)
        self.assertEqual((path.read_bytes(), self.staff_file.read_bytes()), before)
