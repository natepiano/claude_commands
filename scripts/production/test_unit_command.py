"""The unit command resolves staff identities without changing their records."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
import io
import os
from pathlib import Path
import subprocess
from typing import override
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, unit_config, unit_command
from scripts.shutdown import remote
from scripts.herdr import herdr
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase, fixture_runtime_id


@dataclass
class CommandHerdr:
    session: str = "agents"
    pane_records: list[dict[str, str]] = field(default_factory=list)
    agent_records: list[dict[str, str]] = field(default_factory=list)
    identities: dict[str, str] = field(default_factory=dict)
    unreadable_panes: dict[str, str] = field(default_factory=dict)
    pane_error: str = ""
    identity_error: str = ""

    def panes(self) -> list[dict[str, str]]:
        if self.pane_error:
            raise herdr.HerdrFailure(self.pane_error)
        return self.pane_records

    def agents(self) -> list[dict[str, str]]:
        return self.agent_records

    def native_session(self, pane: str, kind: str, timeout: float) -> str:
        del kind, timeout
        if self.identity_error:
            raise ValueError(self.identity_error)
        if pane in self.unreadable_panes:
            raise ValueError(self.unreadable_panes[pane])
        return self.identities[pane]


class UnitCommandTests(StaffTestCase):
    client: CommandHerdr = CommandHerdr()
    folder: Path = Path()
    plan: Path = Path("docs/plans/member.md")
    record: staff.StaffSnapshot = staff.StaffSnapshot(())

    @property
    def runner(self) -> staff.StaffRow:
        return self.record.rows[0]

    @property
    def unit(self) -> staff.StaffRow:
        return self.record.rows[1]

    @property
    def worker(self) -> staff.StaffRow:
        return self.record.rows[2]

    @property
    def runner_worker(self) -> staff.StaffRow:
        return self.record.rows[3]

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}))
        _ = self.enterContext(mock.patch.object(remote, "local_machine", return_value="natedev"))
        self.client = CommandHerdr()
        _ = self.enterContext(mock.patch.object(herdr, "Herdr", return_value=self.client))
        self.folder = self.root / "checkout"
        self.folder.mkdir()
        _ = self.fixture_git(self.folder, "init", "-q", "--initial-branch=topic/member")
        _ = self.fixture_git(self.folder, "-c", "user.name=Fixture", "-c",
                             "user.email=fixture@example.invalid", "commit", "-q",
                             "--allow-empty", "-m", "Fixture")
        write_text_atomic(self.folder / ".unit.toml",
                          f'plan = "{self.plan}"\nresponsibilities = ["member"]\n')
        runner = staff.StaffRow(
            staff.StaffId(fixture_runtime_id("command-root")), "showrunner", "tools", staff.StaffRoot(),
            doc=staff.LiveDoc("natedev", self.root / "production.md"),
            current_session=staff.StaffId(fixture_runtime_id("command-root-current")))
        unit = staff.StaffRow(
            staff.StaffId(fixture_runtime_id("command")), "unit", "panes", staff.ReportsTo(runner.id),
            worktree=staff.PresentWorktree("natedev", self.folder),
            current_session=staff.StaffId(fixture_runtime_id("command-unit-current")))
        worker = staff.StaffRow(
            staff.StaffId(fixture_runtime_id("command-worker")), "worker", "impl", staff.ReportsTo(unit.id),
            current_session=staff.StaffId(fixture_runtime_id("command-worker-current")))
        runner_worker = staff.StaffRow(
            staff.StaffId(fixture_runtime_id("command-runner-worker")), "worker", "review", staff.ReportsTo(runner.id),
            current_session=staff.StaffId(fixture_runtime_id("command-runner-worker-current")))
        self.record = staff.StaffSnapshot((runner, unit, worker, runner_worker))
        _ = self.write_document({"schema": 2, "rows": [row.to_json() for row in self.record.rows],
                                 "pending_retire": []})

    def invoke(self, identity: str, *, record: staff.StaffSnapshot | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = unit_command.main([identity], record=self.record if record is None else record)
        return code, stdout.getvalue(), stderr.getvalue()

    def fields(self, output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in output.splitlines():
            label, separator, value = line.partition(":")
            self.assertTrue(separator, line)
            self.assertTrue(value.strip(), line)
            self.assertNotIn(label.casefold(), fields, line)
            fields[label.casefold()] = value.strip()
        for label in ("name", "kind", "parent", "machine", "worktree", "branch", "plan", "live tab", "current session"):
            self.assertIn(label, fields, output)
        return fields

    def parked_unit(self) -> tuple[staff.StaffRow, Path]:
        removed = replace(self.unit, worktree=staff.RemovedWorktree("natedev", self.root / "gone"))
        parked = unit_config.parked_path(removed.id)
        parked.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(parked, (
            'plan = "docs/plans/retained.md"\nresponsibilities = ["member"]\n'
            '[retired]\ndate = "2026-11-01"\n'
            f'session = "{removed.current_session}"\nfolder = "{self.root / "original"}"\n'
            'branch = "parked/member"\n'))
        return removed, parked

    def test_unknown_id_reports_one_line_and_fails(self) -> None:
        code, stdout, stderr = self.invoke("does-not-exist")
        self.assertNotEqual(code, 0)
        self.assertEqual((stdout + stderr).strip(), "unknown id: does-not-exist")
        self.assertEqual(len((stdout + stderr).splitlines()), 1)

    def test_ambiguous_prefix_names_each_member_on_one_line_and_fails(self) -> None:
        unit = replace(self.unit, id=staff.StaffId("12345678-1111-4111-8111-111111111111"))
        worker = replace(self.worker, parent=unit.id,
                         current_session=staff.StaffId("12345678-2222-4222-8222-222222222222"))
        record = staff.StaffSnapshot((self.runner, unit, worker))
        code, stdout, stderr = self.invoke("12345678", record=record)
        self.assertNotEqual(code, 0)
        self.assertEqual(len((stdout + stderr).splitlines()), 1)
        self.assertIn(unit.name, stdout + stderr)
        self.assertIn(worker.name, stdout + stderr)

    def test_an_ambiguous_prefix_limits_sorted_matches_and_counts_remaining_members(self) -> None:
        rows = tuple(
            replace(self.unit, id=staff.StaffId(f"00000000-0000-4000-8000-{index:012d}"),
                    name=f"member-{9 - index}",
                    current_session=staff.StaffId(f"00000000-1111-4111-8111-{index:012d}"))
            for index in range(8, 0, -1))
        code, stdout, stderr = self.invoke("0", record=staff.StaffSnapshot(rows))
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, (
            "ambiguous id: 0: member-8 (00000000-0000-4000-8000-000000000001), "
            "member-7 (00000000-0000-4000-8000-000000000002), "
            "member-6 (00000000-0000-4000-8000-000000000003), "
            "member-5 (00000000-0000-4000-8000-000000000004), "
            "member-4 (00000000-0000-4000-8000-000000000005), and 3 more\n"))

    def test_full_fixed_id_wins_over_another_members_current_session_match(self) -> None:
        worker = replace(self.worker, current_session=self.unit.id)
        record = staff.StaffSnapshot((self.runner, self.unit, worker))
        code, stdout, stderr = self.invoke(self.unit.id, record=record)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], self.unit.name)
        self.assertEqual(fields["current session"], self.unit.current_session)

    def test_a_prefix_matching_fixed_and_current_ids_selects_the_member_once(self) -> None:
        unit = replace(self.unit, id=staff.StaffId("12345678-1111-4111-8111-111111111111"),
                       current_session=staff.StaffId("12345678-2222-4222-8222-222222222222"))
        record = staff.StaffSnapshot((self.runner, unit))
        code, stdout, stderr = self.invoke("12345678", record=record)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], unit.name)
        self.assertEqual(fields["current session"], unit.current_session)

    def test_a_unique_current_session_prefix_resolves_the_fixed_member(self) -> None:
        code, stdout, stderr = self.invoke(self.worker.current_session[:12])
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], self.worker.name)
        self.assertEqual(fields["parent"], self.unit.name)
        self.assertEqual(fields["current session"], self.worker.current_session)

    def test_a_root_showrunner_has_no_parent_and_shows_its_production_document(self) -> None:
        code, stdout, stderr = self.invoke(self.runner.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], "tools")
        self.assertEqual(fields["kind"], "showrunner")
        self.assertEqual(fields["parent"], "not applicable")
        self.assertEqual(fields["machine"], "natedev")
        self.assertIn(str(self.root / "production.md"), fields["plan"])
        self.assertEqual(fields["current session"], self.runner.current_session)

    def test_a_unit_shows_its_parent_worktree_git_branch_and_plan(self) -> None:
        code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], "panes")
        self.assertEqual(fields["kind"], "unit")
        self.assertEqual(fields["parent"], self.runner.name)
        self.assertEqual(fields["machine"], "natedev")
        self.assertEqual(fields["worktree"], str(self.folder))
        self.assertEqual(fields["branch"], "topic/member")
        self.assertIn(str(self.plan), fields["plan"])
        self.assertEqual(fields["current session"], self.unit.current_session)

    def test_a_worker_inherits_its_holder_units_worktree(self) -> None:
        code, stdout, stderr = self.invoke(self.worker.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], self.worker.name)
        self.assertEqual(fields["kind"], "worker")
        self.assertEqual(fields["parent"], self.unit.name)
        self.assertEqual(fields["machine"], "natedev")
        self.assertIn(str(self.folder), fields["worktree"])
        self.assertIn("inherited", fields["worktree"])
        self.assertEqual(fields["branch"], "topic/member")
        self.assertIn(str(self.plan), fields["plan"])

    def test_a_showrunner_held_worker_has_no_worktree(self) -> None:
        code, stdout, stderr = self.invoke(self.runner_worker.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["parent"], self.runner.name)
        self.assertEqual(fields["worktree"], "not applicable")
        self.assertEqual(fields["branch"], "not applicable")
        self.assertEqual(fields["current session"], self.runner_worker.current_session)

    def test_a_retired_unit_reads_parked_facts_without_a_live_tab_or_git_folder(self) -> None:
        removed, _ = self.parked_unit()
        record = staff.StaffSnapshot((self.runner, removed))
        self.assertFalse((self.root / "gone").exists())
        code, stdout, stderr = self.invoke(removed.current_session[:12], record=record)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["name"], removed.name)
        self.assertIn(str(self.root / "gone"), fields["worktree"])
        self.assertEqual(fields["branch"].removesuffix(" (parked)"), "parked/member")
        self.assertIn("docs/plans/retained.md", fields["plan"])
        self.assertEqual(fields["live tab"], "not applicable")

    def test_live_tabs_match_current_native_sessions_for_claude_and_codex(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                self.client.pane_records = [
                    {"pane_id": "w1:p1", "label": "old-conversation"},
                    {"pane_id": "w1:p2", "label": "panes"}]
                self.client.agent_records = [
                    {"pane_id": "w1:p1", "agent": kind}, {"pane_id": "w1:p2", "agent": kind}]
                self.client.identities = {"w1:p1": self.unit.id, "w1:p2": self.unit.current_session}
                code, stdout, stderr = self.invoke(self.unit.id)
                self.assertEqual(code, 0, stderr)
                fields = self.fields(stdout)
                self.assertIn("panes", fields["live tab"])
                self.assertIn("w1:p2", fields["live tab"])
                self.assertNotIn("old-conversation", fields["live tab"])
                self.assertNotIn("w1:p1", fields["live tab"])
                self.assertEqual(fields["current session"], self.unit.current_session)

    def test_a_failed_mac_read_preserves_known_fields_and_reports_the_cause(self) -> None:
        unit = replace(self.unit, worktree=staff.PresentWorktree("mac", self.root / "mac-worktree"))
        with mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1",
                                          "STAFF_TEST_SSH_REASON": "ssh: Mac link unavailable"}):
            code, stdout, stderr = self.invoke(unit.id, record=staff.StaffSnapshot((self.runner, unit)))
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["machine"], "mac")
        self.assertEqual(fields["worktree"], str(self.root / "mac-worktree"))
        self.assertEqual(fields["parent"], self.runner.name)
        self.assertEqual(fields["current session"], unit.current_session)
        for label in ("branch", "plan", "live tab"):
            self.assertTrue(fields[label].startswith("unavailable:"), fields[label])
            self.assertIn("Mac link unavailable", fields[label])

    def test_a_failed_git_read_reports_unavailable_without_losing_the_plan_or_identity(self) -> None:
        folder = self.root / "not-a-repository"
        folder.mkdir()
        write_text_atomic(folder / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        unit = replace(self.unit, worktree=staff.PresentWorktree("natedev", folder))
        code, stdout, stderr = self.invoke(unit.id, record=staff.StaffSnapshot((self.runner, unit)))
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertTrue(fields["branch"].startswith("unavailable:"), fields["branch"])
        self.assertIn("not a git repository", fields["branch"])
        self.assertEqual(fields["plan"], "standby")
        self.assertEqual(fields["current session"], unit.current_session)

    def test_the_configured_other_machine_supplies_remote_member_details(self) -> None:
        unit = replace(self.unit, worktree=staff.PresentWorktree("studio", self.root / "remote"))
        response = subprocess.CompletedProcess(
            ["ssh"], 0,
            stdout='{"branch":"topic/remote","plan":"docs/remote.md","tab":"remote-member"}',
            stderr="")
        with mock.patch.object(remote, "local_machine", return_value="workstation"), \
                mock.patch.object(remote, "other_machine", return_value="studio"), \
                mock.patch.object(subprocess, "run", return_value=response) as run:
            code, stdout, stderr = self.invoke(unit.id, record=staff.StaffSnapshot((self.runner, unit)))
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["machine"], "studio")
        self.assertEqual(fields["branch"], "topic/remote")
        self.assertEqual(fields["plan"], "docs/remote.md")
        self.assertEqual(fields["live tab"], "remote-member")
        self.assertEqual(fields["current session"], unit.current_session)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "ssh")
        self.assertEqual(run.call_args.args[0][-2], "studio")

    def test_an_unconfigured_machine_reports_unavailable_without_a_connection(self) -> None:
        unit = replace(self.unit, worktree=staff.PresentWorktree("mac", self.root / "remote"))
        with mock.patch.object(remote, "local_machine", return_value="workstation"), \
                mock.patch.object(remote, "other_machine", return_value="studio"), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("unconfigured connection")) as run:
            code, stdout, stderr = self.invoke(unit.id, record=staff.StaffSnapshot((self.runner, unit)))
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        for label in ("branch", "plan", "live tab"):
            self.assertEqual(fields[label], "unavailable: machine mac has no configured connection")
        self.assertEqual(fields["machine"], "mac")
        self.assertEqual(fields["current session"], unit.current_session)
        run.assert_not_called()

    def test_a_failed_herdr_read_reports_unavailable_without_losing_git_facts(self) -> None:
        self.client.pane_error = "fixture herdr did not answer"
        code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["live tab"], "unavailable: fixture herdr did not answer")
        self.assertEqual(fields["branch"], "topic/member")
        self.assertEqual(fields["current session"], self.unit.current_session)

    def test_unknown_native_evidence_reports_a_note_without_reconciling_the_staff_session(self) -> None:
        self.client.pane_records = [{"pane_id": "w1:p1", "label": "panes"}]
        self.client.agent_records = [{"pane_id": "w1:p1", "agent": "codex"}]
        self.client.identity_error = "Codex thread unknown"
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "update_current_session", side_effect=AssertionError("read-only command")):
            code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        self.assertIn("anomaly:", stderr)
        self.assertEqual(stderr, "anomaly: 1 panes unreadable (panes); not blocking\n")
        self.assertIn("not blocking", stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["live tab"], "none found (1 panes unreadable)")
        self.assertEqual(fields["current session"], self.unit.current_session)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreadable_unrelated_panes_produce_one_note_and_preserve_a_matching_tab(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                self.client.pane_records = [
                    {"pane_id": "w1:p1", "label": "first-unreadable"},
                    {"pane_id": "w1:p2", "label": "panes"},
                    {"pane_id": "w1:p3", "label": "second-unreadable"}]
                self.client.agent_records = [
                    {"pane_id": "w1:p1", "agent": "claude"},
                    {"pane_id": "w1:p2", "agent": kind},
                    {"pane_id": "w1:p3", "agent": "codex"}]
                self.client.unreadable_panes = {
                    "w1:p1": "native registry is unreadable", "w1:p3": "Codex thread unknown"}
                self.client.identities = {"w1:p2": self.unit.current_session}
                code, stdout, stderr = self.invoke(self.unit.id)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(stderr, (
                    "anomaly: 2 panes unreadable (first-unreadable, second-unreadable); not blocking\n"))
                self.assertEqual(self.fields(stdout)["live tab"], "panes (herdr:agents:w1:p2)")

    def test_unreadable_panes_with_no_match_report_a_count_without_an_unavailable_field(self) -> None:
        self.client.pane_records = [
            {"pane_id": "w1:p1", "label": "unrelated"}, {"pane_id": "w1:p2", "label": ""}]
        self.client.agent_records = [
            {"pane_id": "w1:p1", "agent": "claude"}, {"pane_id": "w1:p2", "agent": "codex"}]
        self.client.unreadable_panes = {"w1:p1": "registry unknown", "w1:p2": "thread unknown"}
        code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "anomaly: 2 panes unreadable (unrelated, w1:p2); not blocking\n")
        self.assertEqual(self.fields(stdout)["live tab"], "none found (2 panes unreadable)")

    def test_duplicate_live_panes_stay_unavailable_with_one_note_for_unreadable_panes(self) -> None:
        self.client.pane_records = [
            {"pane_id": "w1:p1", "label": "first"},
            {"pane_id": "w1:p2", "label": "second"},
            {"pane_id": "w1:p3", "label": "unrelated"}]
        self.client.agent_records = [
            {"pane_id": "w1:p1", "agent": "claude"},
            {"pane_id": "w1:p2", "agent": "codex"},
            {"pane_id": "w1:p3", "agent": "codex"}]
        self.client.identities = {"w1:p1": self.unit.current_session, "w1:p2": self.unit.current_session}
        self.client.unreadable_panes = {"w1:p3": "thread unknown"}
        code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "anomaly: 1 panes unreadable (unrelated); not blocking\n")
        self.assertEqual(self.fields(stdout)["live tab"], (
            "unavailable: duplicate live panes: first (herdr:agents:w1:p1), second (herdr:agents:w1:p2)"))

    def test_the_executable_entry_forwards_the_id_and_failure_status(self) -> None:
        shim = Path.home() / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.symlink_to(CHECKOUT / "scripts/lib/py")
        completed = subprocess.run(
            [str(CHECKOUT / "scripts/production/unit"), "missing-entry-id"],
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual((completed.stdout + completed.stderr).strip(), "unknown id: missing-entry-id")

    def test_a_changed_native_thread_never_reconciles_staff_or_cleans_the_live_configuration(self) -> None:
        self.client.pane_records = [{"pane_id": "w1:p1", "label": "panes"}]
        self.client.agent_records = [{"pane_id": "w1:p1", "agent": "codex"}]
        self.client.identities = {"w1:p1": fixture_runtime_id("unreconciled-thread")}
        config = self.folder / ".unit.toml"
        recovery = config.with_name(f".{config.name}.999999999.fixture.tmp")
        write_text_atomic(recovery, "unfinished live configuration\n")
        paths = (self.staff_file, config, recovery)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with mock.patch.object(staff, "update_current_session", side_effect=AssertionError("read-only command")), \
                mock.patch.object(unit_config, "read", wraps=unit_config.read) as read:
            code, stdout, stderr = self.invoke(self.unit.id)
        self.assertEqual(code, 0, stderr)
        fields = self.fields(stdout)
        self.assertEqual(fields["current session"], self.unit.current_session)
        self.assertEqual(fields["live tab"], "not applicable")
        read.assert_called_once_with(self.folder, cleanup=False)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

    def test_the_command_reads_one_staff_snapshot_and_changes_no_record_or_recovery_file(self) -> None:
        removed, parked = self.parked_unit()
        _ = self.write_document({"schema": 2, "rows": [self.runner.to_json(), removed.to_json()],
                                 "pending_retire": []})
        recovery = parked.with_name(f".{parked.name}.999999999.fixture.tmp")
        write_text_atomic(recovery, "unfinished publication\n")
        paths = (self.staff_file, parked, recovery, self.folder / ".unit.toml")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, \
                mock.patch.object(staff, "update_current_session", side_effect=AssertionError("read-only command")), \
                mock.patch.object(unit_config, "read_parked", wraps=unit_config.read_parked) as read_parked, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = unit_command.main([removed.id])
        self.assertEqual(code, 0, stderr.getvalue())
        snapshot.assert_called_once_with()
        read_parked.assert_called_once_with(removed.id, cleanup=False)
        self.assertEqual(self.fields(stdout.getvalue())["current session"], removed.current_session)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

