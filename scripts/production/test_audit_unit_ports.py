"""The port audit reports recorded evidence without changing local or remote state."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import cast, override
from unittest.mock import patch

from scripts.lib import atomic_file
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import audit_unit_ports as audit, production_units, staff, staff_sources, unit_config
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.shutdown import remote


class AuditUnitPortsTests(StaffTestCase):
    document: Path = Path()
    parent: staff.StaffId = staff.StaffId("")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.document = self.fill_production("sdlc", "web", "tools", "retired")
        text = self.document.read_text()
        for name in ("web", "tools", "retired"):
            text = text.replace(f"| `{name}` |", f"| `{name}-unit` |")
        atomic_file.write_text_atomic(self.document, text)
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.document))
        self.parent = runner.id
        _ = self.enterContext(patch.object(remote, "local_machine", return_value="natedev"))
        _ = self.enterContext(patch.object(staff_sources, "live", side_effect=AssertionError("audit must not read live staff")))

    def unit(self, name: str, *, removed: bool = False, machine: str = "natedev") -> staff.StaffRow:
        folder = self.source_root / "worktrees" / name
        location = staff.RemovedWorktree(machine, folder) if removed else staff.PresentWorktree(machine, folder)
        row = fixture_member("unit", name=name, parent=self.parent)
        return staff.set_worktree(row.id, location, expected=row.worktree)

    def write_configuration(self, name: str, port: unit_config.AppPort | unit_config.NoAppPort) -> staff.StaffRow:
        row = self.unit(name)
        value = unit_config.UnitConfig(unit_config.StandbyPlan(), port, ("build-check",))
        _ = unit_config.write(self.source_root / "worktrees" / name, value)
        return row

    def document_port(self, name: str, cell: str) -> None:
        lines = self.document.read_text().splitlines()
        for index, line in enumerate(lines):
            if line.startswith(f"| `{name}-unit` |"):
                cells = line.split("|")
                cells[-2] = f" {cell} "
                lines[index] = "|".join(cells)
        atomic_file.write_text_atomic(self.document, "\n".join(lines) + "\n")

    def document_unit(self, name: str, cell: str) -> None:
        text = self.document.read_text().replace(f"| `{name}-unit` |", f"| {cell} |")
        atomic_file.write_text_atomic(self.document, text)

    def other_document(self, name: str, *rows: tuple[str, int]) -> Path:
        document = self.source_root / f"{name}-production.md"
        lines = ["# Fixture production", "", "## Units", "",
                 "| Unit | Port |", "| --- | --- |"]
        lines.extend(f"| `{unit}-unit` | `{port}` |" for unit, port in rows)
        atomic_file.write_text_atomic(document, "\n".join(lines) + "\n")
        _ = fixture_member("showrunner", name=name, doc=staff.LiveDoc("natedev", document))
        return document

    def versions(self) -> dict[Path, tuple[bytes, int]]:
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def results(self) -> dict[staff.StaffId, audit.PortAudit]:
        with patch.object(staff, "snapshot", side_effect=AssertionError("audit must read the staff page")):
            return {result.row.id: result for result in audit.audit(staff.local_snapshot())}

    def cli_output(self, *arguments: str) -> tuple[int, str, str]:
        with (redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors,
              patch.object(staff, "snapshot", side_effect=AssertionError("audit must read the staff page"))):
            code = audit.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    @contextmanager
    def remote_files(self) -> Generator[list[list[str]]]:
        real_run = subprocess.run
        calls: list[list[str]] = []

        def read_on_fixture_machine(command: list[str], **_keywords: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:3], ["ssh", "-o", "BatchMode=yes"])
            self.assertIn(command[3], ("natedev", "mac"))
            arguments = shlex.split(command[4])
            self.assertIn("-c", arguments)
            script = arguments[arguments.index("-c") + 1]
            self.assertNotIn("staff.py", script)
            self.assertNotIn("staff_sources", script)
            calls.append(command)
            return real_run([sys.executable, *arguments[arguments.index("-c"):]], capture_output=True, text=True, check=False,
                            env={**os.environ, "HOME": str(self.root / command[3])},
                            timeout=TEST_HANG_GUARD_SECS)

        with patch.object(subprocess, "run", side_effect=read_on_fixture_machine):
            yield calls

    def test_app_port_no_app_port_and_missing_config_remain_distinct(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        tools = self.write_configuration("tools", unit_config.NoAppPort())
        missing = self.unit("retired")
        self.document_port("web", "`8211` — recorded server")
        before = self.versions()
        results = self.results()
        self.assertEqual(set(results), {web.id, tools.id, missing.id})
        self.assertEqual(results[web.id].config_port, unit_config.AppPort(8211))
        self.assertEqual(results[web.id].document_port, unit_config.AppPort(8211))
        self.assertEqual(results[web.id].document_cell, "`8211` — recorded server")
        self.assertEqual(results[web.id].gap, "")
        self.assertEqual(results[tools.id].config_port, unit_config.NoAppPort())
        self.assertEqual(results[tools.id].document_port, unit_config.NoAppPort())
        self.assertEqual(results[tools.id].gap, "")
        self.assertIsInstance(results[missing.id].config_port, unit_config.ConfigMissing)
        self.assertTrue(results[missing.id].gap)
        self.assertEqual(results[web.id].config_source,
                         production_units.DocumentSource("natedev", self.source_root / "worktrees/web/.unit.toml"))
        self.assertEqual(results[web.id].document_source, production_units.DocumentSource("natedev", self.document))
        code, output, errors = self.cli_output()
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertIn("AppPort(8211)", output)
        self.assertIn("NoAppPort", output)
        self.assertIn("ConfigMissing", output)
        self.assertIn("gap for sdlc", output)
        self.assertEqual(self.versions(), before)

    def test_audit_reads_use_no_cleanup_and_retain_abandoned_files(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        retired = self.write_configuration("retired", unit_config.AppPort(8212))
        folder = self.source_root / "worktrees" / "retired"
        facts = unit_config.RetiredSession("2026-10-31", "retired-session",
                                         unit_config.RecordedStartFolder(folder), unit_config.Branch("retired"))
        _ = unit_config.park(retired.id, folder, facts)
        retired = self.unit("retired", removed=True)
        parked = unit_config.parked_path(retired.id)
        for path in (self.source_root / "worktrees/web/.unit.toml", parked):
            atomic_file.write_text_atomic(path.with_name(f".{path.name}.999999999.abandoned.tmp"), "unfinished")
        before = self.versions()
        with (patch.object(unit_config, "read", wraps=unit_config.read) as active_reads,
              patch.object(unit_config, "read_parked", wraps=unit_config.read_parked) as parked_reads):
            results = self.results()
        self.assertEqual(results[web.id].config_port, unit_config.AppPort(8211))
        self.assertEqual(results[retired.id].config_port, unit_config.AppPort(8212))
        self.assertGreater(active_reads.call_count, 0)
        self.assertGreater(parked_reads.call_count, 0)
        for call in (*active_reads.call_args_list, *parked_reads.call_args_list):
            self.assertIs(call.kwargs.get("cleanup"), False)
        self.assertEqual(self.versions(), before)

    def test_retired_unit_uses_the_parked_file_instead_of_a_stray_worktree_file(self) -> None:
        retired = self.write_configuration("retired", unit_config.AppPort(8212))
        folder = self.source_root / "worktrees" / "retired"
        facts = unit_config.RetiredSession("2026-10-31", "retired-session",
                                         unit_config.RecordedStartFolder(folder), unit_config.Branch("retired"))
        _ = unit_config.park(retired.id, folder, facts)
        original = unit_config.read(folder, cleanup=False)
        assert isinstance(original, unit_config.UnitConfig)
        _ = unit_config.write(folder, replace(original, port=unit_config.AppPort(8999)))
        retired = self.unit("retired", removed=True)
        self.document_port("retired", "`8212`")
        before = self.versions()
        result = self.results()[retired.id]
        self.assertEqual(result.config_port, unit_config.AppPort(8212))
        self.assertEqual(result.config_source, production_units.DocumentSource("natedev", unit_config.parked_path(retired.id)))
        self.assertEqual(result.gap, "")
        self.assertEqual(self.versions(), before)

    def test_document_evidence_comes_only_from_the_bound_parent_document(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        other = self.source_root / "another-production.md"
        atomic_file.write_text_atomic(other, self.document.read_text().replace("| — |", "| `8211` |"))
        self.document_port("web", "`8219` — owner decision")
        result = self.results()[web.id]
        self.assertEqual(result.document_source, production_units.DocumentSource("natedev", self.document))
        self.assertEqual(result.document_cell, "`8219` — owner decision")
        self.assertEqual(result.document_port, unit_config.AppPort(8219))
        self.assertTrue(result.gap)
        current = unit_config.read(self.source_root / "worktrees/web", cleanup=False)
        assert isinstance(current, unit_config.UnitConfig)
        self.assertEqual(current.port, unit_config.AppPort(8211))

    def test_suffixed_unit_cell_matches_the_staff_name(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        self.assertIn("| `web-unit` |", self.document.read_text())
        before = self.versions()
        result = self.results()[web.id]
        self.assertEqual(result.document_port, unit_config.AppPort(8211))
        self.assertEqual(result.document_cell, "`8211`")
        self.assertEqual(result.gap, "")
        self.assertEqual(self.versions(), before)

    def test_invalid_unit_cell_does_not_hide_a_valid_unit_row(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211` — web server")
        original = self.document.read_text()
        valid = next(line for line in original.splitlines() if line.startswith("| `web-unit` |"))
        invalid = valid.replace("`web-unit`", "`bad unit`").replace("8211", "8999")
        for before_valid in (True, False):
            with self.subTest(before_valid=before_valid):
                pair = (invalid, valid) if before_valid else (valid, invalid)
                atomic_file.write_text_atomic(self.document, original.replace(valid, "\n".join(pair), 1))
                before = self.versions()
                result = self.results()[web.id]
                self.assertEqual(result.document_port, unit_config.AppPort(8211))
                self.assertEqual(result.document_cell, "`8211` — web server")
                self.assertEqual(result.document_source, production_units.DocumentSource("natedev", self.document))
                self.assertEqual(result.gap, "")
                self.assertEqual(result.note, "")
                self.assertEqual(self.versions(), before)

    def test_another_showrunner_document_supplies_the_missing_parent_row(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_unit("web", "`different-unit`")
        document = self.other_document("other", ("web", 8211))
        before = self.versions()
        result = self.results()[web.id]
        self.assertEqual(result.document_port, unit_config.AppPort(8211))
        self.assertEqual(result.document_cell, "`8211`")
        self.assertEqual(result.document_source, production_units.DocumentSource("natedev", document))
        self.assertEqual(result.gap, "")
        self.assertIn(f"natedev:{document}", result.note)
        self.assertIn("parent", result.note)
        self.assertIn("not", result.note)
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(f"note: {result.note}", output)
        code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["note"], result.note)
        self.assertEqual(self.versions(), before)

    def test_two_other_showrunner_documents_report_both_matching_sources(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_unit("web", "`different-unit`")
        first = self.other_document("first", ("web", 8211))
        second = self.other_document("second", ("web", 8211))
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
        assert isinstance(result.document_port, audit.DocumentPortMissing)
        for document in (first, second):
            self.assertIn(f"natedev:{document}", result.document_port.reason)
        self.assertTrue(result.gap)
        self.assertEqual(self.versions(), before)

    def test_parent_document_match_takes_priority_over_other_showrunner_documents(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211` — parent decision")
        _ = self.other_document("other", ("web", 8999))
        before = self.versions()
        result = self.results()[web.id]
        self.assertEqual(result.document_port, unit_config.AppPort(8211))
        self.assertEqual(result.document_cell, "`8211` — parent decision")
        self.assertEqual(result.document_source, production_units.DocumentSource("natedev", self.document))
        self.assertEqual(result.gap, "")
        self.assertEqual(result.note, "")
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertNotIn("note:", output)
        self.assertEqual(self.versions(), before)

    def test_each_bound_document_is_read_once_for_multiple_unit_searches(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        tools = self.write_configuration("tools", unit_config.AppPort(8212))
        self.document_unit("web", "`different-web-unit`")
        self.document_unit("tools", "`different-tools-unit`")
        document = self.other_document("other", ("web", 8211), ("tools", 8212))
        _ = fixture_member("showrunner", name="shared", doc=staff.LiveDoc("natedev", document))
        empty = self.other_document("empty", ("unrelated", 8999))
        real_read = Path.read_text
        reads: list[Path] = []

        def read_document(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path in (self.document, document, empty):
                reads.append(path)
            return real_read(path, encoding=encoding, errors=errors)

        before = self.versions()
        with patch.object(Path, "read_text", new=read_document):
            results = self.results()
        self.assertEqual(results[web.id].document_port, unit_config.AppPort(8211))
        self.assertEqual(results[tools.id].document_port, unit_config.AppPort(8212))
        for path in (self.document, document, empty):
            self.assertEqual(reads.count(path), 1, str(path))
        self.assertEqual(reads[0], self.document)
        self.assertEqual(self.versions(), before)

    def test_bare_unit_cell_still_matches_the_staff_name(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        self.document_unit("web", "`web`")
        before = self.versions()
        result = self.results()[web.id]
        self.assertEqual(result.document_port, unit_config.AppPort(8211))
        self.assertEqual(result.document_cell, "`8211`")
        self.assertEqual(result.gap, "")
        self.assertEqual(self.versions(), before)

    def test_rejected_unit_cell_keeps_its_raw_text_in_the_report(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        cell = "`web unit`"
        self.document_unit("web", cell)
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
        assert isinstance(result.document_port, audit.DocumentPortMissing)
        self.assertIn(cell, result.document_port.reason)
        self.assertEqual(result.document_cell, cell)
        self.assertTrue(result.gap)
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(cell, output)
        code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["document_cell"], cell)
        self.assertIn(cell, str(selected["document_port"]))
        self.assertEqual(self.versions(), before)

    def test_bare_and_suffixed_cells_for_one_unit_report_two_matches(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        text = self.document.read_text()
        line = next(line for line in text.splitlines() if line.startswith("| `web-unit` |"))
        duplicate = line.replace("| `web-unit` |", "| `web` |", 1)
        atomic_file.write_text_atomic(self.document, text.replace(line, line + "\n" + duplicate, 1))
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
        assert isinstance(result.document_port, audit.DocumentPortMissing)
        self.assertIn("found 2", result.document_port.reason)
        self.assertTrue(result.gap)
        self.assertEqual(self.versions(), before)

    def test_missing_or_invalid_document_port_does_not_count_as_no_app_port(self) -> None:
        web = self.write_configuration("web", unit_config.NoAppPort())
        for cell in ("unknown", "`70000`"):
            with self.subTest(cell=cell):
                self.document_port("web", cell)
                result = self.results()[web.id]
                self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
                self.assertTrue(result.gap)

    def test_empty_port_cell_is_no_app_port_but_an_absent_column_is_missing(self) -> None:
        web = self.write_configuration("web", unit_config.NoAppPort())
        self.document_port("web", "")
        result = self.results()[web.id]
        self.assertEqual(result.document_port, unit_config.NoAppPort())
        self.assertEqual(result.document_cell, "")
        self.assertEqual(result.gap, "")
        lines = self.document.read_text().splitlines()
        rows: list[str] = []
        for line in lines:
            if line.startswith("|"):
                line = "|".join(line.split("|")[:-2]) + "|"
            rows.append(line)
        atomic_file.write_text_atomic(self.document, "\n".join(rows) + "\n")
        result = self.results()[web.id]
        self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
        self.assertTrue(result.gap)

    def test_one_match_with_an_unavailable_document_is_an_incomplete_search(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(15733))
        self.document_unit("web", "`different-unit`")
        matched = self.other_document("matched", ("web", 15733))
        missing = self.other_document("unavailable", ("unrelated", 8123))
        missing.unlink()
        before = self.versions()
        result = self.results()[web.id]
        self.assertTrue(result.gap)
        self.assertNotEqual(result.document_port, unit_config.AppPort(15733))
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(str(missing), output)
        self.assertIn(str(matched), output)
        self.assertEqual(self.versions(), before)

    def test_missing_worktree_and_missing_bound_document_are_visible(self) -> None:
        web = fixture_member("unit", name="web", parent=self.parent)
        self.document.unlink()
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.config_port, audit.Unavailable)
        self.assertIsInstance(result.document_port, audit.Unavailable)
        self.assertTrue(result.gap)
        self.assertEqual(self.versions(), before)

    def test_invalid_numeric_port_keeps_the_raw_cell_in_text_and_json_reports(self) -> None:
        web = self.write_configuration("web", unit_config.NoAppPort())
        cell = "`70000` — recorded decision"
        self.document_port("web", cell)
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.document_port, audit.DocumentPortMissing)
        self.assertEqual(result.document_cell, cell)
        self.assertTrue(result.gap)
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(cell, output)
        code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["document_cell"], cell)
        self.assertEqual(self.versions(), before)

    def test_local_audit_process_creates_no_bytecode_or_interpreter_cache(self) -> None:
        _ = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        checkout = self.root / "private-checkout"
        _ = shutil.copytree(Path(__file__).resolve().parents[2] / "scripts", checkout / "scripts",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        home = self.root / "home"
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "",
                       "XDG_CACHE_HOME": str(home / ".cache"), "PYTHONPATH": str(checkout)}
        _ = environment.pop("PYTHONPYCACHEPREFIX", None)
        before = self.versions()
        result = subprocess.run([sys.executable, str(checkout / "scripts/production/audit_unit_ports.py"), "--json"],
                                capture_output=True, text=True, check=False, cwd=checkout, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AppPort(8211)", result.stdout)
        self.assertEqual(self.versions(), before)

    def test_remote_audit_processes_create_no_bytecode_or_interpreter_cache(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        home = self.root / "natedev"
        checkout = home / ".claude"
        _ = shutil.copytree(Path(__file__).resolve().parents[2] / "scripts", checkout / "scripts",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        atomic_file.write_bytes_atomic(home / ".local/state/staff/staff.json", self.staff_file.read_bytes())
        before = self.versions()
        real_run = subprocess.run
        calls: list[list[str]] = []

        def run_on_private_owner(command: list[str], **_keywords: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:4], ["ssh", "-o", "BatchMode=yes", "natedev"])
            calls.append(command)
            environment = {**os.environ, "HOME": str(home), "STAFF_FILE": "",
                           "PYTHONDONTWRITEBYTECODE": "",
                           "XDG_CACHE_HOME": str(home / ".cache"), "CLAUDE_PYTHON": "",
                           "PYTHONPATH": str(checkout)}
            _ = environment.pop("PYTHONPYCACHEPREFIX", None)
            return real_run(["bash", "-c", command[4]], capture_output=True, text=True, check=False,
                            cwd=checkout, env=environment, timeout=TEST_HANG_GUARD_SECS)

        with (patch.dict(os.environ, {"STAFF_FILE": ""}),
              patch.object(remote, "local_machine", return_value="mac"),
              patch.object(remote, "other_machine", return_value="natedev"),
              patch.object(subprocess, "run", side_effect=run_on_private_owner)):
            code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["config_port"], "AppPort(8211)")
        self.assertEqual(selected["document_cell"], "`8211`")
        self.assertEqual(selected["gap"], "")
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(self.versions(), before)

    def test_malformed_configuration_reports_unavailable_and_keeps_its_bytes(self) -> None:
        web = self.unit("web")
        path = self.source_root / "worktrees/web/.unit.toml"
        atomic_file.write_text_atomic(path, 'plan = "standby"\nport = "8211"\nresponsibilities = []\n')
        before = self.versions()
        result = self.results()[web.id]
        self.assertIsInstance(result.config_port, audit.Unavailable)
        self.assertTrue(result.gap)
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("unavailable:", output)
        self.assertEqual(self.versions(), before)

    def test_guarded_repair_removes_the_gap_and_the_finished_audit_changes_nothing(self) -> None:
        web = self.write_configuration("web", unit_config.NoAppPort())
        _ = self.write_configuration("tools", unit_config.NoAppPort())
        _ = self.write_configuration("retired", unit_config.NoAppPort())
        self.document_port("web", "`8211`")
        code, output, errors = self.cli_output()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("gap for sdlc", output)
        self.assertEqual(self.results()[web.id].config_port, unit_config.NoAppPort())
        _ = unit_config.repair_port(unit_config.InWorktree(self.source_root / "worktrees/web/.unit.toml"),
                                    unit_config.NoAppPort(), unit_config.AppPort(8211))
        before = self.versions()
        first = self.cli_output()
        self.assertEqual((first[0], first[2]), (0, ""))
        self.assertNotIn("gap for sdlc", first[1])
        self.assertEqual(self.results()[web.id].gap, "")
        self.assertEqual(self.cli_output(), first)
        self.assertEqual(self.versions(), before)
    def test_remote_active_and_parked_files_use_the_recorded_machine_and_keep_source_evidence(self) -> None:
        web = self.unit("web", machine="mac")
        retired = self.unit("retired", removed=True, machine="mac")
        active = self.source_root / "worktrees/web/.unit.toml"
        parked = self.root / "mac/.local/state/staff/units" / f"{retired.id}.toml"
        atomic_file.write_text_atomic(active, 'plan = "standby"\nport = 8211\nresponsibilities = []\n')
        atomic_file.write_text_atomic(parked, 'plan = "standby"\nport = 8212\nresponsibilities = []\n'
                                      + '[retired]\ndate = "2026-10-31"\nsession = "saved"\n'
                                      + f'folder = "{self.root / "removed"}"\nbranch = "retired"\n')
        document = self.root / "mac/production.md"
        self.document_port("web", "`8211`")
        self.document_port("retired", "`8212`")
        atomic_file.write_text_atomic(document, self.document.read_text())
        _ = staff.set_doc(self.parent, staff.LiveDoc("mac", document))
        before = self.versions()
        with self.remote_files() as calls:
            results = self.results()
        self.assertEqual(results[web.id].config_source, production_units.DocumentSource("mac", active))
        self.assertEqual(results[retired.id].config_source, production_units.DocumentSource("mac", parked))
        self.assertEqual(results[web.id].config_port, unit_config.AppPort(8211))
        self.assertEqual(results[retired.id].config_port, unit_config.AppPort(8212))
        self.assertEqual(results[web.id].document_source, production_units.DocumentSource("mac", document))
        self.assertEqual(results[retired.id].document_cell, "`8212`")
        self.assertEqual((results[web.id].gap, results[retired.id].gap), ("", ""))
        document_reads = [call for call in calls if shlex.split(call[4])[-1] == str(document)]
        self.assertEqual(len(document_reads), 1)
        self.assertTrue(all(call[3] == "mac" for call in calls))
        self.assertEqual(self.versions(), before)

    def test_unavailable_remote_configuration_reports_the_cause_without_local_fallback(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        web = self.unit("web", machine="mac")
        self.document_port("web", "`8211`")
        before = self.versions()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "fixture host offline")) as ssh:
            result = self.results()[web.id]
            code, output, errors = self.cli_output()
        self.assertIsInstance(result.config_port, audit.Unavailable)
        assert isinstance(result.config_port, audit.Unavailable)
        self.assertIn("fixture host offline", result.config_port.reason)
        self.assertEqual(result.config_source,
                         production_units.DocumentSource("mac", self.source_root / "worktrees/web/.unit.toml"))
        self.assertTrue(result.gap)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("unavailable:", output)
        self.assertIn("fixture host offline", output)
        self.assertTrue(all(call.args[0][3] == "mac" for call in ssh.call_args_list))
        self.assertEqual(self.versions(), before)

    def test_remote_missing_configuration_is_not_no_app_port(self) -> None:
        web = self.unit("web", machine="mac")
        before = self.versions()
        with self.remote_files():
            result = self.results()[web.id]
        self.assertIsInstance(result.config_port, unit_config.ConfigMissing)
        self.assertTrue(result.gap)
        self.assertEqual(result.document_port, unit_config.NoAppPort())
        self.assertEqual(self.versions(), before)

    def test_unavailable_remote_document_does_not_reuse_local_document_evidence(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        document = self.root / "mac/bound-production.md"
        _ = staff.set_doc(self.parent, staff.LiveDoc("mac", document))
        before = self.versions()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "fixture link unavailable")):
            result = self.results()[web.id]
        self.assertEqual(result.config_port, unit_config.AppPort(8211))
        self.assertEqual(result.document_source, production_units.DocumentSource("mac", document))
        self.assertIsInstance(result.document_port, audit.Unavailable)
        assert isinstance(result.document_port, audit.Unavailable)
        self.assertIn("fixture link unavailable", result.document_port.reason)
        self.assertEqual(result.document_cell, "")
        self.assertTrue(result.gap)
        self.assertEqual(self.versions(), before)

    def test_json_cli_reports_the_current_staff_page_without_a_live_staff_command(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        before = self.versions()
        with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as reads:
            code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(reads.call_count, 1)
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["config_port"], "AppPort(8211)")
        self.assertEqual(selected["document_source"], f"natedev:{self.document}")
        self.assertEqual(selected["document_cell"], "`8211`")
        self.assertEqual(self.versions(), before)

    def test_mac_cli_reads_the_owner_page_once_and_preserves_remote_file_evidence(self) -> None:
        web = self.write_configuration("web", unit_config.AppPort(8211))
        self.document_port("web", "`8211`")
        owner_page = self.root / "natedev/.local/state/staff/staff.json"
        atomic_file.write_bytes_atomic(owner_page, self.staff_file.read_bytes())
        self.write_json(self.mac_file, {"schema": 2, "rows": [], "pending_retire": []})
        before = self.versions()
        with (patch.dict(os.environ, {"HOME": str(self.root / "mac"), "STAFF_FILE": ""}),
              patch.object(remote, "other_machine", return_value="natedev"),
              patch.object(remote, "local_machine", return_value="mac"),
              patch.object(staff, "local_snapshot", side_effect=AssertionError("Mac must read the owner page")),
              self.remote_files() as calls):
            code, output, errors = self.cli_output("--json")
        self.assertEqual((code, errors), (0, ""))
        rows = cast(list[dict[str, object]], json.loads(output))
        selected = next(row for row in rows if row["id"] == web.id)
        self.assertEqual(selected["config_port"], "AppPort(8211)")
        self.assertEqual(selected["config_source"], f"natedev:{self.source_root / 'worktrees/web/.unit.toml'}")
        self.assertEqual(selected["document_source"], f"natedev:{self.document}")
        self.assertEqual(selected["document_cell"], "`8211`")
        self.assertEqual(selected["gap"], "")
        owner_reads = [call for call in calls if "staff.local_snapshot()" in call[4]]
        self.assertEqual(len(owner_reads), 1)
        self.assertTrue(all(call[3] == "natedev" for call in calls))
        self.assertEqual(self.versions(), before)

    def test_mac_cli_reports_an_unavailable_owner_page_and_exits_zero(self) -> None:
        before = self.versions()
        with (patch.dict(os.environ, {"HOME": str(self.root / "mac"), "STAFF_FILE": ""}),
              patch.object(remote, "other_machine", return_value="natedev"),
              patch.object(remote, "local_machine", return_value="mac"),
              patch.object(staff, "local_snapshot", side_effect=AssertionError("Mac must read the owner page")),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "owner unavailable"))):
            code, output, errors = self.cli_output()
        self.assertEqual((code, output), (0, ""))
        self.assertIn("unavailable:", errors)
        self.assertIn("owner unavailable", errors)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertEqual(self.versions(), before)
