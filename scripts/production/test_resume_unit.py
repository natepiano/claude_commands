#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""resume_unit.py in process. Git runs in temporary repositories; add_unit.py is a recording stand-in
and the transcripts are files the test writes: no Claude starts."""
from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

from scripts.shutdown import remote

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, MutableMapping
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from multiprocessing.connection import Connection
from typing import cast, final, override
from unittest import mock


from scripts.production import resume_unit
from scripts.production import add_unit
from scripts.production import retire_unit
from scripts.message import sessions as retirement_sessions
from scripts.production import unit_lookup
from scripts.production import unit_move
from scripts.production import test_retire_unit
from scripts.production.production_doc import Production, read_production
from scripts.shutdown import pane_record
from scripts.herdr.herdr import HerdrPlace

from scripts.production.staff_test_support import CHECKOUT, StaffTestCase
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import sessions
from scripts.production import completion_receipt, staff, unit_config, worker
from scripts.production.staff_test_support import unit_membership

RUN_COMMAND = retire_unit.run

DIRECTOR = "11111111-1111-1111-1111-111111111111"


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


@final
class ResumeUnitTests(unittest.TestCase):
    def test_resume_restores_retained_workers(self) -> None:
        from scripts.production import worker

        with mock.patch.object(worker, "restore", return_value=worker.ExitCode.OK) as restore:
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        restore.assert_called_once()

    def test_uncertain_worker_restore_keeps_the_director_resume_complete(self) -> None:
        row = unit_membership("alpha", "alpha")
        with mock.patch.object(worker, "restore", side_effect=[
                worker.ExitCode.UNCERTAIN, worker.ExitCode.OK]) as restore:
            code, output = self.resume()
            self.assertEqual(code, 8, output)
            self.assertIn("alpha resumed", output)
            self.assertFalse(unit_config.parked_path(row.id).exists())
            unit = resume_unit.find_unit("alpha", "alpha")
            self.assertIsInstance(unit, resume_unit.NoRecoveryRequired)
            receipt = completion_receipt.read_receipt(
                resume_unit.resume_record(unit.production, unit.name).with_suffix(".done"), staff.snapshot())
            self.assertIsInstance(receipt, completion_receipt.OwnedReceipt)
            assert isinstance(receipt, completion_receipt.OwnedReceipt)
            self.assertEqual(receipt.owner.id, row.id)
            self.assertEqual(receipt.fields["session_id"], row.current_session)
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(output, "alpha already resumed\n")
        self.assertEqual(restore.call_args_list, [mock.call(row.id), mock.call(row.id)])
        self.assertEqual(len(self.calls), 1)

    def test_completed_resume_dry_run_does_not_restore_workers(self) -> None:
        from scripts.production import worker

        with mock.patch.object(worker, "restore", return_value=worker.ExitCode.OK) as restore:
            code, output = self.resume()
            self.assertEqual(code, 0, output)
            restore.reset_mock()
            code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(output, f"{unit_membership('alpha', 'alpha').name} already resumed\n")
        restore.assert_not_called()

    def test_resume_restores_the_staff_location(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.worktree / ".git").is_file())
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        row = unit_membership("alpha", "alpha")
        self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.worktree))
        self.assertIsInstance(resume_unit.find_unit("alpha", "alpha"), resume_unit.NoRecoveryRequired)


    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    configs: list[Path] = []
    calls: list[list[str]] = []
    launch_code: int = 0

    def test_resume_command_uses_the_row_name_and_current_native_session(self) -> None:
        row = unit_membership("alpha", "alpha")
        self.assertNotEqual(row.id, row.current_session)
        unit = resume_unit.find_unit("alpha", "alpha")
        assert isinstance(unit, resume_unit.RetiredUnitRecovery)
        self.assertEqual(unit.branch, row.name)
        command = resume_unit.launch_command(replace(unit, name="stale-table"))
        self.assertEqual(command[command.index("--production") + 2], row.name)
        self.assertEqual(command[command.index("--resume") + 1], row.current_session)
        self.assertNotIn("STAFF_ID", " ".join(command))

    def test_recovery_refuses_an_empty_session_id(self) -> None:
        unit = resume_unit.find_unit("alpha", "alpha")
        assert isinstance(unit, resume_unit.RetiredUnitRecovery)
        with self.assertRaisesRegex(resume_unit.Refused, "^alpha: recovery requires a session id$"):
            _ = replace(unit, session_id=" ")

    def test_recovery_requires_each_retirement_fact(self) -> None:
        unit = resume_unit.find_unit("alpha", "alpha")
        assert isinstance(unit, resume_unit.RetiredUnitRecovery)
        for fact in ("branch", "session_id", "plan"):
            fields = dict(vars(unit))
            del fields[fact]
            with self.subTest(fact=fact), self.assertRaises(TypeError):
                _ = cast(Callable[..., object], resume_unit.RetiredUnitRecovery)(**fields)

    def test_neither_config_refuses_with_the_backfill_instruction_before_any_change(self) -> None:
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        unit_config.discard_parked(row.id)
        before = self.doc.read_bytes(), self.staff.staff_file.read_bytes(), git(self.checkout, "rev-parse", "HEAD")
        for flags in ((), ("--dry-run",)):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertEqual(output, f"alpha: no .unit.toml in {self.worktree} or "
                                 + f"~/.local/state/staff/units/{row.id}.toml; ask the showrunner\n")
                self.assertFalse(self.worktree.exists())
                self.assertEqual(unit_config.place(row.id, self.worktree), unit_config.Nowhere())
                self.assertEqual(self.calls, [])
                self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                                  git(self.checkout, "rev-parse", "HEAD")), before)


    @override
    def setUp(self) -> None:
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        for name in ("shutdown", "production", "direct"):
            write_text_atomic(self.root / f"{name}.conf", (CHECKOUT / f"config/{name}.conf").read_text())
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}))
        origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(origin), str(self.checkout)], check=True,
                           capture_output=True)
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        _ = self.doc.write_text("# Production\n\n- **Merge branch:** main\n"
                                + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n", encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "pane-launches"))
        self.configs = [self.root / "claude1", self.root / "claude2"]
        self.transcript(self.configs[0])

        self.calls = []
        self.launch_code = 0
        _ = self.staff.record_production("alpha", self.doc)
        production = read_production("alpha")

        def production_of(_production_name: str, *, record: staff.StaffSnapshot) -> Production:
            del record
            return production

        def not_marked(_production_name: str, _name: str) -> unit_lookup.LabeledSession | None:
            # The real pane writer retains a fixed owner; the live process resumes
            # that owner's current conversation, which can differ after /clear.
            owner = staff.by_current_session(DIRECTOR)
            entries = [entry for entry in pane_record.launches()
                       if owner is not None and entry["session_id"] == owner.id]
            return (unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123),
                                          HerdrPlace("agents", entries[-1]["pane_id"]))
                    if entries else None)

        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", not_marked))
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: self.configs))
        _ = self.enterContext(mock.patch.object(retirement_sessions, "config_dirs", lambda: self.configs))
        _ = cast(MutableMapping[str, str], self.enterContext(
            mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                                       "HERDR_PANE_RECORD_DIR": str(self.root / "pane-launches")})))
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        self.retained_config()

    def retained_config(self, *, plan: str = "docs/plans/alpha.md") -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        unit_config.discard_parked(row.id)
        _ = unit_config.write_parked(row.id, unit_config.UnitConfig(unit_config.PlanFile(Path(plan)),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", row.current_session, unit_config.Branch(row.name))))

    def transcript(self, config: Path, cwd: Path | None = None) -> None:
        cwd = self.worktree if cwd is None else cwd
        folder = config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n", encoding="utf-8")

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; add_unit.py is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append([Path(argv[1]).name, *argv[2:]])
        if self.launch_code == 0:
            pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
                {}, ["claude", "--resume", DIRECTOR]))
        return subprocess.CompletedProcess(argv, self.launch_code, "", "launch failed" if self.launch_code else "")

    def resume(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = resume_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def assert_resumed(self, code: int, output: str) -> None:
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[-1], ["add_unit.py", "--production", "alpha", "alpha", "--resume", DIRECTOR,
                                          "--cwd", str(self.worktree), "--plan", "docs/plans/alpha.md"])
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "main"))
        self.assertIn("alpha resumed", output)

    def test_resume_unit_rerun(self) -> None:
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        saved = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", DIRECTOR, unit_config.Branch("alpha")))
        _ = unit_config.write_parked(row.id, saved)
        parked = unit_config.parked_path(row.id)
        retained = parked.read_bytes(), parked.stat().st_mtime_ns
        original = staff.restore_unit
        for after in (False, True):
            def interrupted(identity: staff.StaffId,
                            worktree: staff.PresentWorktree) -> staff.StaffRow:
                if after:
                    _ = original(identity, worktree)
                raise RuntimeError("stopped at staff step")
            with mock.patch.object(staff, "restore_unit", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff step"):
                    _ = self.resume()
            self.assertTrue(self.worktree.is_dir())
            self.assertEqual(unit_config.read(self.worktree),
                             unit_config.UnitConfig(saved.plan, saved.port, saved.responsibilities))
            self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), retained)
            self.assertEqual(self.calls, [])
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        rows = staff.find(kind="unit")
        self.assertEqual([row.name for row in rows], ["alpha"])
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn("already resumed", output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))
        self.assertEqual(len(self.calls), 1)

    def test_retire_then_resume_reuses_the_staff_identity_at_the_add_unit_boundary(self) -> None:
        retirement = test_retire_unit.RetireUnitTests()
        self.addCleanup(retirement.doCleanups)
        retirement.setUp()
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: [retirement.config]))
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        tip = git(retirement.checkout, "rev-parse", "main")
        code, output = retirement.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(remote.local_machine(), retirement.worktree)))
        restore = self.enterContext(mock.patch.object(
            worker, "restore", return_value=worker.ExitCode.OK))
        launched_ids: list[staff.StaffId] = []
        commands: list[list[str]] = []

        def launch(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            if len(argv) > 1 and Path(argv[1]).name == "add_unit.py":
                commands.append(argv)
                production = read_production(argv[argv.index("--production") + 1])
                row = add_unit.ensure_staff_unit(production, argv[4])
                launched_ids.append(row.id)
                retirement.marked = unit_lookup.LabeledSession("alpha",
                    unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
                pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(retirement.worktree),
                    {}, ["claude", "--resume", DIRECTOR]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return retirement.fake_run(argv, cwd)

        resumed = io.StringIO()
        with mock.patch.object(retire_unit, "run", side_effect=launch), redirect_stdout(resumed):
            code = resume_unit.main(["alpha", "alpha"])
        self.assertEqual(code, 0, resumed.getvalue())
        restore.assert_called_once_with(unit.id)
        self.assertEqual(launched_ids, [unit.id])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][commands[0].index("--resume") + 1], DIRECTOR)
        self.assertEqual(staff.by_id(unit.id), unit)
        assert isinstance(unit.parent, staff.ReportsTo)
        self.assertEqual(staff.find(kind="unit", parent=unit.parent.id), [unit])
        self.assertEqual(git(retirement.worktree, "rev-parse", "HEAD"), tip)
        before = (retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                  git(retirement.checkout, "for-each-ref"))
        with mock.patch.object(retire_unit, "run", side_effect=launch), redirect_stdout(io.StringIO()):
            code = resume_unit.main(["alpha", "alpha"])
        self.assertEqual(code, 0)
        self.assertEqual(restore.call_args_list, [mock.call(unit.id), mock.call(unit.id)])
        self.assertEqual(len(commands), 1)
        self.assertEqual((retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                          git(retirement.checkout, "for-each-ref")), before)


    def parked_facts(self) -> Path:
        return unit_config.parked_path(next(row for row in staff.find(kind="unit") if row.name == "alpha").id)


    def test_successful_launch_keeps_parked_facts_until_its_staff_pane_is_proven(self) -> None:
        row = staff.find(kind="unit")[0]
        saved = unit_config.parked_path(row.id).read_bytes()
        document = self.doc.read_bytes()
        with mock.patch.object(resume_unit, "resumed_session_proven", return_value=False):
            code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("resumed native session identity is not proven", output)
        self.assertEqual(unit_config.parked_path(row.id).read_bytes(), saved)
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(len(self.calls), 1)
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_resume_proof_requires_the_exact_pane_owner_and_current_conversation(self) -> None:
        row = staff.find(kind="unit")[0]
        pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {}, ["claude", "--resume", DIRECTOR]))
        before = (self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                  unit_config.parked_path(row.id).read_bytes())
        with self.subTest(evidence="fixed owner for the live current conversation"):
            self.assertTrue(resume_unit.resumed_session_proven("alpha", "alpha", DIRECTOR, row))
        current = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
        other_session = fixture_runtime_id("unrelated-resume-conversation")
        for marked, expected_session in (
            (replace(current, place=HerdrPlace("agents", "w1:other")), DIRECTOR),
            (replace(current, claude=unit_lookup.LiveClaude("alpha", other_session, "fixture", 123)), DIRECTOR),
            (current, row.id),
        ):
            with self.subTest(marked=marked, expected_session=expected_session), mock.patch.object(
                    unit_lookup, "labeled_session", return_value=marked):
                self.assertFalse(resume_unit.resumed_session_proven("alpha", "alpha", expected_session, row))
        self.assertEqual((self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                          unit_config.parked_path(row.id).read_bytes()), before)

    def test_retired_unit_receipt_cannot_complete_a_new_incarnation(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        unit = staff.find(kind="unit")[0]
        self.staff.forget_rows(unit.id)
        replacement = fixture_member("unit", name=unit.name, parent=unit.parent,
                                     runtime_id=fixture_runtime_id("replacement-unit-backend"))
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertNotIn("already resumed", output)
        self.assertEqual(staff.by_id(replacement.id), replacement)
        self.assertEqual(len(self.calls), 1)

    def test_resume_child_joins_the_operation_until_the_parked_facts_are_discarded(self) -> None:
        source = self.staff.production("alpha", "alpha")
        source.unlink()
        source.symlink_to(self.doc)
        _ = self.staff.record_production("alpha", source)
        row = unit_membership("alpha", "alpha")
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        wrapper = self.root / "resume-launch.py"
        # The inner launcher boundary supplies no live pane. Its public main and
        # staff step are real, including the inherited operation lock.
        write_text_atomic(wrapper, "\n".join([
            "import sys", "from unittest.mock import patch",
            f"sys.path.insert(0, {str(CHECKOUT / 'scripts/production')!r})",
            f"sys.path.insert(0, {str(CHECKOUT)!r})",
            "from scripts.production import add_unit", "from scripts.production.production_doc import read_production",
            "def launch(arguments):",
            "    name = arguments[arguments.index('--production') + 1]",
            "    row = add_unit.ensure_staff_unit(read_production(name), arguments[2])",
            "    import pane_record",
            f"    pane_record.write('agents', pane_record.launch('w1:p1', {DIRECTOR!r}, {str(self.worktree)!r}, {{}}, ['claude']))",
            "    print(row.id)", "    return 0",
            "with patch.object(add_unit, '_main', side_effect=launch):",
            "    raise SystemExit(add_unit.main(sys.argv[1:]))", "",
        ]))
        context = multiprocessing.get_context("fork")
        first, second = context.Pipe()
        parent = cast(Connection, cast(object, first))
        child = cast(Connection, cast(object, second))
        original_discard = unit_config.discard_parked
        def finish(unit: staff.StaffId) -> None:
            child.send((staff.snapshot().find(kind="unit")[0].id, bool(staff.operation_fds())))
            _ = cast(object, child.recv())
            original_discard(unit)
        def run() -> None:
            parent.close()
            try:
                with mock.patch.object(retire_unit, "run", RUN_COMMAND), mock.patch.object(
                        resume_unit, "ADD_UNIT", wrapper), mock.patch.object(unit_config, "discard_parked", side_effect=finish):
                    child.send(self.resume())
            finally:
                child.close()
        process = context.Process(target=run)
        writer: subprocess.Popen[str] | None = None
        try:
            with self.staff.lock_notice(self.staff.staff_file.parent / "operation.lock") as listener:
                process.start()
                child.close()
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "nested add_unit did not join resume's lock")
                identity, lock_held = cast(tuple[str, bool], parent.recv())
                self.assertIsInstance(identity, str, f"resume exited before discard: {(identity, lock_held)!r}")
                self.assertIsInstance(lock_held, bool)
                staff_id = staff.StaffId(identity)
                self.assertTrue(lock_held, "resume released its operation lock before discarding the parked facts")
                self.assertTrue(unit_config.parked_path(staff_id).exists())
                writer = self.staff.start_script(CHECKOUT / "scripts/production/staff.py", "rename", identity, "--name", "alpha")
                self.staff.assert_operation_blocks(writer, listener)
                parent.send("finish")
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "resume did not discard the parked facts")
                code, output = cast(tuple[int, str], parent.recv())
                self.assertEqual(code, 0, output)
                process.join(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.exitcode, 0)
                output, errors = writer.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(writer.returncode, 0, output + errors)
            self.assertFalse(unit_config.parked_path(staff_id).exists())
            row = staff.by_id(staff_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual((row.kind, row.name), ("unit", "alpha"))
            self.assertEqual(staff.find(kind="unit"), [row])
        finally:
            parent.close()
            child.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=TEST_HANG_GUARD_SECS)
            process.close()
            if writer is not None and writer.returncode is None:
                writer.kill()
                _ = writer.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_resumes_the_recorded_session_in_a_worktree_made_from_the_merge_branch(self) -> None:
        self.assert_resumed(*self.resume())

    def test_retired_as_built_without_a_header_is_passed_to_the_resume_launch(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        self.retained_config(plan=plan)
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "condensed plan")

        code, output = self.resume()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls, [["add_unit.py", "--production", "alpha", "alpha",
                                      "--resume", DIRECTOR, "--cwd", str(self.worktree), "--plan", plan]])

    def test_a_transcript_under_another_account_resumes(self) -> None:
        # The units account moved since the retire: the transcript is under the other config dir.
        self.configs.reverse()
        self.assert_resumed(*self.resume())

    def test_a_failed_launch_keeps_the_parked_facts_and_a_rerun_resumes(self) -> None:
        document = self.doc.read_bytes()
        head = git(self.checkout, "rev-parse", "HEAD")
        self.launch_code = 1
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("refused: add_unit.py --resume: launch failed", output)
        self.assertTrue(unit_config.parked_path(staff.find(kind="unit")[0].id).exists())
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(len(self.calls), 1)
        worktree_head = git(self.worktree, "rev-parse", "HEAD")
        self.launch_code = 0
        code, output = self.resume()
        self.assert_resumed(code, output)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), worktree_head)
        self.assertNotIn("made: worktree", output)

    def test_transcript_and_branch_refusals_change_nothing(self) -> None:
        self.configs = [self.root / "claude3"]
        _ = git(self.checkout, "branch", "alpha", "main")
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"refused: alpha: no account has the transcript of session {DIRECTOR}", output)
        self.assertIn("refused: branch alpha still exists; retire did not finish", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production")

    def test_dry_run_changes_nothing(self) -> None:
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would resume: session {DIRECTOR} through add_unit.py --resume", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())

    def test_missing_session_refuses_both_dry_run_and_launch_when_no_evidence_exists(self) -> None:
        self.configs = [self.root / "empty-config"]
        unit_config.discard_parked(staff.find(kind="unit")[0].id)
        before = self.doc.read_bytes()
        for flags in (("--dry-run",), ()):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertIn("; ask the showrunner", output)
                self.assertNotIn("would resume:", output)
                self.assertNotIn("resumed:", output)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.worktree.exists())
                self.assertEqual(self.doc.read_bytes(), before)

    def test_parked_session_survives_newer_worker_transcripts(self) -> None:
        older = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / "older.jsonl"
        _ = older.write_text(json.dumps({"entrypoint": "cli", "cwd": str(self.worktree)}) + "\n")
        os.utime(older, (1, 1))
        worker = older.with_name("worker.jsonl")
        _ = worker.write_text(json.dumps({"entrypoint": "sdk-py", "cwd": str(self.worktree)}) + "\n")
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would resume: session {DIRECTOR} through", output)
        self.assertEqual(self.calls, [])
        self.assert_resumed(*self.resume())

    def test_parked_session_survives_stale_retained_pane_launches(self) -> None:
        for pane, identity, at in (("w1:p1", "older", "2026-10-27T23:00:00+00:00"),
                                    ("w1:p2", DIRECTOR, "2026-10-28T23:00:00+00:00")):
            launch = pane_record.launch(pane, identity, str(self.worktree),
                {('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha", "CLAUDE_CONFIG_DIR": str(self.configs[0])}, ["--resume", identity])
            launch["written_at"] = at
            pane_record.write("agents", launch)
        current_backend = unit_lookup.labeled_session

        def observed_backend(production_name: str, name: str) -> unit_lookup.LabeledSession | None:
            # Retained pane files are historical evidence. Only the fake launch
            # establishes a live backend for this test.
            return current_backend(production_name, name) if self.calls else None

        with mock.patch.object(unit_lookup, "labeled_session", side_effect=observed_backend):
            self.assert_resumed(*self.resume())

    def external_transcript(self) -> Path:
        for transcript in (self.configs[0] / "projects").glob(f"*/{DIRECTOR}.jsonl"):
            transcript.unlink()
        folder = self.root / "original-checkout"
        folder.mkdir()
        self.transcript(self.configs[0], folder)
        return folder

    def test_resume_finds_the_session_transcript_outside_the_staff_worktree(self) -> None:
        _ = self.external_transcript()
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would make: worktree {self.worktree} on alpha", output)
        self.assertIn(f"--cwd {self.worktree}", output)
        self.assertEqual(self.calls, [])
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn(["--cwd", str(self.worktree)], [call[index:index + 2] for call in self.calls for index in range(len(call) - 1)])
        self.assertTrue(self.worktree.is_dir())

    def test_renamed_transcript_keeps_the_staff_resume_folder(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old = self.root / "alpha-old-wt"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, self.worktree, self.worktree)) + "\n")
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"--cwd {self.worktree}", output)
        self.assertNotIn(str(old), output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assert_resumed(*self.resume())
        self.assertEqual(len(self.calls), 1)

    def test_resume_ignores_the_transcript_folder_and_recorded_cwd(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        _ = transcript.write_text(json.dumps({"cwd": str(self.root / "alpha-old-wt")}) + "\n")
        before = transcript.read_bytes(), transcript.stat().st_mtime_ns
        self.assert_resumed(*self.resume())
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns), before)

    def test_duplicate_transcript_locations_do_not_change_the_staff_resume_folder(self) -> None:
        _ = self.external_transcript()
        self.transcript(self.configs[1], self.root / "another-checkout")
        self.assert_resumed(*self.resume())

    def test_missing_session_transcript_keeps_the_retirement_record(self) -> None:
        for config in self.configs:
            for transcript in (config / "projects").glob(f"*/{DIRECTOR}.jsonl"):
                transcript.unlink()
        before = self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"no account has the transcript of session {DIRECTOR}", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.doc.read_bytes(), before)

    def test_resume_uses_a_changed_staff_folder_without_rewriting_retirement_facts(self) -> None:
        row = unit_membership("alpha", "alpha")
        parked = unit_config.parked_path(row.id)
        before = parked.read_bytes(), parked.stat().st_mtime_ns
        destination = self.root / "changed-staff-folder"
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), destination), expected=row.worktree)
        unit = resume_unit.find_unit("alpha", "alpha")
        assert isinstance(unit, resume_unit.RetiredUnitRecovery)
        command = resume_unit.launch_command(unit)
        self.assertEqual(command[command.index("--cwd") + 1], str(destination))
        self.assertTrue(resume_unit.has_transcript(unit))
        self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), before)

    def test_shared_projects_symlink_is_one_transcript_location(self) -> None:
        self.configs[1].mkdir()
        (self.configs[1] / "projects").symlink_to(self.configs[0] / "projects", target_is_directory=True)
        self.assert_resumed(*self.resume())


if __name__ == "__main__":
    _ = unittest.main(argv=sys.argv)
