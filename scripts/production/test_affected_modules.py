"""Selection contracts against small committed source trees."""
from __future__ import annotations

import ast
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import conf
from scripts.production import affected_modules as affected
from scripts.test_times.gate_record_test_support import gate_run


class AffectedModulesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.repo: Path = Path()
        self.base: affected.CertifiedBase = affected.CertifiedBase("", "")
        self.modules: list[Path] = []

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="affected-test-")))
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "maintenance.auto", "false")
        _ = self.git("config", "user.name", "Selection Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        self.write(".git/test-config/merge_gate.conf", "# text:\nlanding_branches=main builds natedev sdlc\n"
                   + "# text:\nscan_roots=\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.repo / ".git/test-config")})))
        self.write(".git/test-config/buildlog.conf", (Path(__file__).resolve().parents[2] / "config/buildlog.conf").read_text())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.repo / ".git/buildlog")})))
        self.write(".gitignore", "__pycache__/\n")
        # Ten unrelated modules distinguish direct selection from a full selection.
        for index in range(10):
            self.add_test(f"other/test_unrelated_{index}.py", "VALUE = 1\n")
        self.certify()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text, encoding="utf-8")

    def add_test(self, path: str, text: str) -> None:
        self.write(f"scripts/{path}", text)
        self.modules.append(self.repo / "scripts" / path)

    def commit(self) -> str:
        _ = self.git("add", ".")
        _ = self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def certify(self) -> None:
        self.base = affected.CertifiedBase(self.commit(), datetime.now(UTC).isoformat())
        self.record_configs({})

    def record_configs(self, reads: dict[str, list[str]]) -> None:
        modules = [{"suite": path.parent.name, "module": path.name, "attempt": 1,
                    "config_reads": reads.get(path.name, [])} for path in self.modules]
        run = {**gate_run(), "kind": "gate_run", "v": 1, "id": "fixture-full", "host": "mac",
               "selection": "full", "aborted": False, "modules": modules}
        self.write(".git/buildlog/mac/fixture.jsonl", json.dumps(run) + "\n")

    def selection(self) -> affected.AffectedSelection:
        result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.AffectedSelection, result)
        assert isinstance(result, affected.AffectedSelection)
        return result

    def cache_dir(self) -> Path:
        return self.repo / ".git" / "gate-state"

    def test_config_selection_uses_the_newest_full_record_and_direct_names(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.write("scripts/reader.py", 'from scripts.lib import conf\nconf.read("limits")\n')
        self.add_test("checks/test_runtime.py", "import scripts.reader\n")
        self.add_test("checks/test_indirect.py", "import scripts.reader\n")
        self.add_test("checks/test_named.py", '# config/limits.conf\n')
        self.add_test("checks/test_missing.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_runtime.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        run = cast(dict[str, object], json.loads(log.read_text()))
        module_rows = cast(list[dict[str, object]], run["modules"])
        for row in module_rows:
            if row["module"] == "test_missing.py":
                del row["config_reads"]
        earlier = {**run, "id": "earlier", "started_at": "2020-01-01T00:00:00Z",
                   "modules": [{**row, "config_reads": ["limits"]} for row in module_rows]}
        aborted = {**earlier, "id": "aborted", "started_at": "2030-01-01T00:00:00Z", "aborted": True}
        affected_run = {**aborted, "id": "affected", "aborted": False, "selection": "affected"}
        _ = log.write_text("".join(json.dumps(row) + "\n" for row in (earlier, run, aborted, affected_run)))
        self.add_test("checks/test_new.py", "VALUE = 1\n")
        self.write("config/limits.conf", "value=two\n")
        from scripts.buildlog import index
        with mock.patch.object(index, "read_only", wraps=index.read_only) as reads:
            selected = self.selection()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual({path.name for path in selected.modules}, {"test_runtime.py", "test_named.py", "test_new.py", "test_missing.py"})
        with mock.patch.object(index, "read_only", side_effect=sqlite3.OperationalError("unreadable index")):
            result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("unreadable index", result.reason)
        self.write("config/limits.conf", "value=one\n")
        (self.repo / "scripts/checks/test_new.py").unlink()
        with mock.patch.object(affected, "latest_module_reads", side_effect=AssertionError("No config changed")):
            _ = affected.select_affected(self.repo, self.base, self.modules[:-1], set())

    def test_config_selection_skips_a_newer_full_gate_without_config_records(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.add_test("checks/test_reader.py", "VALUE = 1\n")
        self.add_test("checks/test_other.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_reader.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        recorded = cast(dict[str, object], json.loads(log.read_text()))
        module_rows = cast(list[dict[str, object]], recorded["modules"])
        unrecorded = {**recorded, "id": "newer-unrecorded", "started_at": "2030-01-01T00:00:00Z",
                      "modules": [{key: value for key, value in row.items() if key != "config_reads"}
                                  for row in module_rows]}
        _ = log.write_text("".join(json.dumps(row) + "\n" for row in (recorded, unrecorded)))
        self.write("config/limits.conf", "value=two\n")
        self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_reader.py",))

    def test_folder_records_select_added_changed_and_removed_paths(self) -> None:
        self.add_test("production/test_staff_accessors.py", "VALUE = 1\n")
        self.add_test("checks/test_folder.py", "VALUE = 1\n")
        self.write("scripts/component/seed.txt", "base\n")
        self.write("assets/kept.txt", "base\n")
        self.write("docs/kept.txt", "base\n")
        cases: tuple[tuple[str, str, object, bool, tuple[str, ...]], ...] = (
            ("added accessor", "scripts/component/new_accessor.py", [], False, ("test_staff_accessors.py",)),
            ("removed file", "assets/removed.txt", ["assets"], True, ("test_folder.py",)),
            ("removed directory", "assets/old/only.txt", ["assets"], True, ("test_folder.py",)),
            ("changed file", "assets/changed.txt", ["assets"], False, ()),
            ("changed recursive file", "scripts/component/seed.txt", ["scripts/"], False, ("test_folder.py",)),
            ("changed config", "config/limits.conf", ["config"], False, ()),
            ("new directory", "docs/new/deep/page.md", ["docs"], False, ("test_folder.py",)),
            ("unlisted directory", "other/place.txt", [], False, ()),
            ("recursive boundary", "scripts-extra/file.txt", ["scripts/"], False, ()),
            ("null record", "other/unknown.txt", None, False, ("test_folder.py",)),
            ("repository prefix", "other/all.txt", ["/"], False, ("test_folder.py",)),
        )
        for label, path, folders, removed, expected in cases:
            with self.subTest(case=label):
                if removed or label in {"changed file", "changed recursive file", "changed config"}:
                    self.write(path, "base\n")
                self.certify()
                log = self.repo / ".git/buildlog/mac/fixture.jsonl"
                run = cast(dict[str, object], json.loads(log.read_text()))
                module_rows = cast(list[dict[str, object]], run["modules"])
                for row in module_rows:
                    row["folder_scans"] = (["scripts/", "commands/", "docs/"]
                                           if row["module"] == "test_staff_accessors.py" and label == "added accessor"
                                           else folders if row["module"] == "test_folder.py" else [])
                _ = log.write_text(json.dumps(run) + "\n")
                if removed:
                    (self.repo / path).unlink()
                    if label == "removed directory":
                        (self.repo / path).parent.rmdir()
                else:
                    self.write(path, "from scripts.production import staff\n" if label == "added accessor" else "changed\n")
                _ = self.commit()
                self.assertEqual(tuple(module.name for module in self.selection().modules), expected)

    def test_source_audits_select_changed_file_kinds_without_scan_records(self) -> None:
        scans = (
            ('ROOT.rglob("*.py")', "scripts/component/subject.py", True),
            ('ROOT.rglob("*.py")', "config/limits.conf", False),
            ('ROOT.glob("**/*.py")', "scripts/component/subject.py", True),
            ('os.walk(ROOT)', "scripts/component/subject.py", True),
            ('subprocess.check_output(["git", "ls-files"], cwd=ROOT)', "commands/subject.md", True),
            ('scan(ROOT)', "scripts/component/subject.py", True),
            ('Audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('imported_scan(ROOT)', "scripts/component/subject.py", True),
            ('audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('audit', "scripts/component/subject.py", False),
            ('bare_audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('scan_imported_root()', "scripts/component/subject.py", True),
            ('IMPORTED_ROOT.rglob("*.py")', "scripts/component/subject.py", True),
            ('IMPORTED_ROOT.rglob("*.py")', "assets/unrelated.txt", False),
            ('imported_scan(IMPORTED_ROOT)', "scripts/component/subject.py", True),
            ('imported_scan(scratch)', "scripts/component/subject.py", False),
            ('scratch.rglob("*.py")', "scripts/component/subject.py", False),
            ('source_scan(ROOT)', "assets/unrelated.txt", False),
            ('audit.scan(ROOT)', "assets/unrelated.txt", False),
            ('audit.noop(ROOT)', "scripts/unrelated/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/unrelated/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/accounts/nested/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/accounts/subject.py", True),
            ('(ROOT / "scripts/accounts").rglob("*.py")', "scripts/accounts/nested/subject.py", True),
            ('(ROOT / "scripts/accounts").rglob("*.py")', "scripts/unrelated/subject.py", False),
            ("config_test_support", "assets/unrelated.conf", False),
            ("config_test_support", "config/limits.conf", True),
            ('declared_scan(ROOT)', "scripts/component/nested/subject.txt", True),
            ('declared_scan(ROOT)', "commands/component/nested/subject.txt", True),
            ('declared_scan(ROOT)', "outside/subject.md", False),
            ('(ROOT / os.environ.get("SCAN_FOLDER", "scripts")).rglob("*.py")', "scripts/component/subject.py", True),
            ('ROOT.glob(os.environ.get("SCAN_PATTERN", "**/*.py"))', "scripts/component/subject.py", True),
            ('(ROOT / os.environ.get("SCAN_FOLDER", "scripts/component")).glob("*.py")', "scripts/component/subject.py", True),
            (f"Path({str(self.repo)!r}).rglob('*.py')", "scripts/component/subject.py", True),
        )
        declaration_errors = {
            "checks/test_absent.py:scripts/component": "test module is not a live gate module",
            "checks/test_audit.py:scripts/stlye": "repository path does not exist",
            "checks/test_audit.py:scripts/component:extra": "expected exactly one ':'",
        }
        cases = [(expression, changed, selected,
                  "checks/test_audit.py:scripts/component checks/test_audit.py:commands/component"
                  if expression == "declared_scan(ROOT)" else "") for expression, changed, selected in scans]
        cases.extend(("declared_scan(ROOT)", "outside/subject.md", False, declaration)
                     for declaration in declaration_errors)
        self.write("commands/component/seed.txt", "fixture\n")
        for expression, changed, selected, declaration in cases:
            with self.subTest(scan=expression, changed=changed, declaration=declaration):
                self.write(".git/test-config/merge_gate.conf", "# text:\nlanding_branches=main builds natedev sdlc\n"
                           + f"# text:\nscan_roots={declaration}\n")
                self.write(changed, "# before\n")
                self.write("scripts/component/audit.py", "import os\nfrom scripts.component import directory_walk\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def noop(folder):\n    return None\n"
                    + "def unused_walk(folder):\n    return list(os.walk(folder))\n")
                self.write("scripts/component/directory_walk.py", "import os\n"
                    + "def scan(folder):\n    return list(os.walk(folder))\n")
                self.write("scripts/component/root_audit.py", "from pathlib import Path\n"
                    + "ROOT = Path(__file__).resolve().parents[2]\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n")
                self.write("scripts/component/source_root.py", "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n")
                self.write("scripts/component/config_test_support.py", "from pathlib import Path\n"
                    + "SOURCE = Path(__file__).resolve().parents[2] / 'config'\n"
                    + "def config_files():\n    return list(SOURCE.glob('*.conf'))\n")
                self.add_test("checks/test_audit.py", "from pathlib import Path\nimport os, subprocess\n"
                    + "from scripts.component.audit import scan as imported_scan\nfrom scripts.component import audit\n"
                    + "import audit as bare_audit\n"
                    + ("from scripts.component import config_test_support\n" if expression == "config_test_support" else "")
                    + ("from scripts.component.root_audit import scan as root_scan, ROOT as IMPORTED_ROOT\n"
                       + "def scan_imported_root():\n    return root_scan(IMPORTED_ROOT)\n" if expression == "scan_imported_root()" else "")
                    + ("from scripts.component.source_root import ROOT as IMPORTED_ROOT\n"
                       if expression in {'IMPORTED_ROOT.rglob("*.py")', "imported_scan(IMPORTED_ROOT)"} else "")
                    + "ROOT = Path(__file__).resolve().parents[2]\nscratch = Path('/fixture')\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def declared_scan(folder):\n    return list((folder / 'outside').glob('*.md'))\n"
                    + "def source_scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def scratch_scan(folder):\n    return list(os.walk(folder))\n"
                    + "class Audit:\n    @staticmethod\n    def scan(folder):\n        return list(folder.rglob('*.py'))\n"
                    + f"FILES = {expression}\n")
                self.certify()
                self.record_configs({})
                self.write(changed, "# after\n")
                _ = self.commit()
                for cached in (False, True):
                    with self.subTest(cached=cached):
                        result = affected.select_affected(self.repo, self.base, self.modules,
                            affected.dirty_paths(self.repo), state_dir=self.cache_dir())
                        if declaration in declaration_errors:
                            self.assertIsInstance(result, affected.FullSuite)
                            assert isinstance(result, affected.FullSuite)
                            self.assertIn("graph error:", result.reason)
                            self.assertIn(declaration, result.reason)
                            self.assertIn(declaration_errors[declaration], result.reason)
                        else:
                            self.assertIsInstance(result, affected.AffectedSelection)
                            assert isinstance(result, affected.AffectedSelection)
                            self.assertEqual(result.modules, (self.repo / "scripts/checks/test_audit.py",) if selected else ())
                self.modules.remove(self.repo / "scripts/checks/test_audit.py")

        words = conf.read(Path(__file__).resolve().parents[2] / "config/merge_gate.conf").text("scan_roots").split()
        data_inputs = (
            ("config/issue_score.conf", ("issue_score/test_experiment.py", "issue_score/test_score_issues.py")),
            ("config/agent_notes.toml", ("maintenance/test_vault_commit.py",)),
            ("scripts/lib/py", ("review_links/test_review_link.py", "usage/test_collect_mac.py")),
            ("outside/subject.md", ("agents/test_models_conf_publish.py", "lint/test_style_review.py")),
        )
        for changed, names in data_inputs:
            with self.subTest(changed=changed):
                self.write(changed, "before\n")
                declaration = " ".join(word for word in words if word.partition(":")[0] in names)
                for word in declaration.split():
                    path = self.repo / word.partition(":")[2]
                    if not path.exists():
                        path.mkdir(parents=True)
                self.write(".git/test-config/merge_gate.conf", "# text:\nlanding_branches=main builds natedev sdlc\n"
                           + f"# text:\nscan_roots={declaration}\n")
                for name in names:
                    self.add_test(name, "from pathlib import Path\n"
                                  + "ROOT = Path(__file__).resolve().parents[2]\nFILES = list(ROOT.rglob('*'))\n")
                self.certify()
                self.write(changed, "after\n")
                _ = self.commit()
                for cached in (False, True):
                    with self.subTest(cached=cached):
                        result = affected.select_affected(self.repo, self.base, self.modules,
                            affected.dirty_paths(self.repo), state_dir=self.cache_dir())
                        self.assertIsInstance(result, affected.AffectedSelection)
                        assert isinstance(result, affected.AffectedSelection)
                        self.assertEqual({path.relative_to(self.repo).as_posix() for path in result.modules},
                                         {f"scripts/{name}" for name in names})
                for name in names:
                    self.modules.remove(self.repo / "scripts" / name)

    def test_content_reads_select_read_files_and_keep_config_selection(self) -> None:
        self.add_test("checks/test_reader.py", "VALUE = 1\n")
        cases: tuple[tuple[str, str, object, bool, bool, tuple[str, ...]], ...] = (
            ("changed script", "scripts/component/subject.py", ["scripts/component/subject.py"], False, True, ("test_reader.py",)),
            ("removed script", "scripts/component/subject.py", ["scripts/component/subject.py"], True, True, ("test_reader.py",)),
            ("copied config", "config/limits.conf", ["config/limits.conf"], False, True, ("test_reader.py",)),
            ("null content", "scripts/component/subject.py", None, False, True, ("test_reader.py",)),
            ("empty content", "scripts/component/subject.py", [], False, True, ()),
            ("no content gate", "scripts/component/subject.py", [], False, False, ()),
        )
        for label, path, contents, removed, recorded, expected in cases:
            with self.subTest(case=label):
                self.write(path, "VALUE = 1\n")
                self.certify()
                log = self.repo / ".git/buildlog/mac/fixture.jsonl"
                run = cast(dict[str, object], json.loads(log.read_text()))
                module_rows = cast(list[dict[str, object]], run["modules"])
                for row in module_rows:
                    row["folder_scans"] = []
                    if recorded:
                        row["content_reads"] = contents if row["module"] == "test_reader.py" else []
                run.update(id="content-full", started_at="2028-01-01T00:00:00Z")
                newer = {**run, "id": "newer-no-content", "started_at": "2029-01-01T00:00:00Z",
                         "modules": [{key: value for key, value in row.items() if key != "content_reads"}
                                     for row in module_rows]}
                ignored: list[dict[str, object]] = [{**run, "id": selection, "selection": selection, "aborted": aborted,
                            "started_at": "2030-01-01T00:00:00Z",
                            "modules": [{**row, "content_reads": []} for row in module_rows]}
                           for selection, aborted in (("full", True), ("affected", False))]
                _ = log.write_text("".join(json.dumps(record) + "\n" for record in (run, newer, *ignored)))
                if removed:
                    (self.repo / path).unlink()
                else:
                    self.write(path, "# .astimezone()\nVALUE = 2\n")
                _ = self.commit()
                self.assertEqual(tuple(module.name for module in self.selection().modules), expected)

    def test_folder_and_config_records_use_independent_latest_full_gates(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.write("assets/kept.txt", "base\n")
        self.add_test("checks/test_config.py", "VALUE = 1\n")
        self.add_test("checks/test_folder.py", "VALUE = 1\n")
        self.add_test("checks/test_unrecorded.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_config.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        config_gate = cast(dict[str, object], json.loads(log.read_text()))
        config_rows = cast(list[dict[str, object]], config_gate["modules"])
        folder_rows = [{key: value for key, value in row.items() if key != "config_reads"} for row in config_rows]
        for row in folder_rows:
            row["folder_scans"] = ["assets"] if row["module"] == "test_folder.py" else []
            if row["module"] == "test_unrecorded.py":
                row["folder_scans"] = None
        folder_gate = {**config_gate, "id": "folder-full", "started_at": "2030-01-01T00:00:00Z", "modules": folder_rows}
        empty_gate = {**folder_gate, "id": "newer-no-records", "started_at": "2031-01-01T00:00:00Z",
                      "modules": [{key: value for key, value in row.items() if key != "folder_scans"} for row in folder_rows]}
        for selection, aborted in (("full", True), ("affected", False)):
            ignored = {**folder_gate, "id": "ignored", "started_at": "2032-01-01T00:00:00Z",
                       "selection": selection, "aborted": aborted,
                       "modules": [{**row, "folder_scans": ["/"]} for row in folder_rows]}
            _ = log.write_text("".join(json.dumps(run) + "\n" for run in (config_gate, folder_gate, empty_gate, ignored)))
            self.write("config/limits.conf", "value=two\n")
            self.write("assets/new.txt", "added\n")
            self.assertEqual({module.name for module in self.selection().modules},
                             {"test_config.py", "test_folder.py", "test_unrecorded.py"})

    def test_landing_bases_exclude_changes_that_already_passed_the_merge_gate(self) -> None:
        self.write("scripts/landed/subject.py", "VALUE = 1\n")
        self.write("scripts/own/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_landed.py", "import scripts.landed.subject\n")
        self.add_test("checks/test_own.py", "import scripts.own.subject\n")
        self.certify()
        self.write("scripts/landed/subject.py", "VALUE = 2\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        self.write("scripts/own/subject.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/own/subject.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base, affected.CertifiedBase(landing, self.git("show", "-s", "--format=%cI", landing)))
        without_certificate = affected.select_affected(self.repo, affected.NoCertifiedBase(), self.modules, set())
        self.assertEqual(without_certificate, result)

    def test_a_merge_with_a_landing_branch_keeps_the_units_earlier_changes(self) -> None:
        self.write("scripts/landed/subject.py", "VALUE = 1\n")
        self.write("scripts/own/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_landed.py", "import scripts.landed.subject\n")
        self.add_test("checks/test_own.py", "import scripts.own.subject\n")
        self.certify()
        _ = self.git("switch", "-c", "unit")
        self.write("scripts/own/subject.py", "VALUE = 2\n")
        _ = self.commit()
        _ = self.git("switch", "main")
        self.write("scripts/landed/subject.py", "VALUE = 2\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        _ = self.git("switch", "unit")
        _ = self.git("merge", "--no-ff", "main", "-m", "Merge the landing branch")
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/own/subject.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base.commit, landing)

    def test_a_dirty_file_at_the_landing_tip_is_the_only_changed_path(self) -> None:
        self.add_test("checks/test_own.py", "VALUE = 1\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        self.write("scripts/checks/test_own.py", "VALUE = 2\n")
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/checks/test_own.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base.commit, landing)

    def test_base_choice_counts_paths_and_preserves_candidate_priority(self) -> None:
        for name in ("first", "second", "own"):
            self.add_test(f"checks/test_{name}.py", "VALUE = 0\n")
        self.certify()
        self.write("scripts/checks/test_first.py", "VALUE = 1\n")
        first = self.commit()
        self.write("scripts/checks/test_second.py", "VALUE = 1\n")
        second = self.commit()
        self.write("scripts/checks/test_first.py", "VALUE = 0\n")
        landing = self.commit()
        self.write("scripts/checks/test_own.py", "VALUE = 1\n")
        _ = self.commit()
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "Unrelated history")
        for branch, commit in (("main", first), ("builds", second), ("natedev", landing),
                               ("sdlc", landing), ("stable", self.base.commit), ("unrelated", unrelated)):
            _ = self.git("update-ref", f"refs/remotes/origin/{branch}", commit)
        cases = (
            (self.base, "main", self.base.commit, ("own", "second")),
            (self.base, "builds", self.base.commit, ("own", "second")),
            (self.base, "main builds natedev sdlc", landing, ("own",)),
            (affected.NoCertifiedBase(), "builds stable", second, ("first", "own")),
            (affected.NoCertifiedBase(), "stable builds", self.base.commit, ("own", "second")),
            (self.base, "missing unrelated", self.base.commit, ("own", "second")),
        )
        for certificate, branches, expected_base, changed in cases:
            with self.subTest(branches=branches, certificate=certificate):
                self.write(".git/test-config/merge_gate.conf", f"# text:\nlanding_branches={branches}\n"
                           + "# text:\nscan_roots=\n")
                with mock.patch.object(affected, "git_output", wraps=affected.git_output) as git:
                    result = affected.select_affected(self.repo, certificate, self.modules, set())
                self.assertIsInstance(result, affected.AffectedSelection)
                assert isinstance(result, affected.AffectedSelection)
                self.assertEqual(result.base.commit, expected_base)
                expected_paths = tuple(f"scripts/checks/test_{name}.py" for name in changed)
                self.assertEqual(result.changed_files, expected_paths)
                self.assertCountEqual(result.modules, tuple(self.repo / path for path in expected_paths))
                compared = [call.args[-2] for call in git.call_args_list if call.args[1] == "diff"]
                self.assertEqual(len(compared), len(set(compared)))

    def test_cached_and_uncached_graphs_are_equal(self) -> None:
        sources = {
            "scripts/component/__init__.py": "VALUE = 1\n",
            "scripts/component/subject.py": "VALUE = 2\n",
            "scripts/duplicate/subject.py": "VALUE = 3\n",
            "scripts/component/test_adapter.py": "from . import subject\nfrom ..component import subject\n",
            "scripts/checks/test_imports.py": "import scripts.component.test_adapter, subject\nfrom subject import VALUE\nimport json\n",
        }
        for path, text in sources.items():
            self.write(path, text)
        _ = self.commit()
        expected = affected.dependency_graph(self.repo)
        cold = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        with mock.patch.object(ast, "parse", side_effect=AssertionError("warm cache parsed source")):
            warm = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(cold, expected)
        self.assertEqual(warm, expected)

    def test_new_blob_reparses_only_the_changed_file(self) -> None:
        self.write("scripts/component/test_subject.py", "VALUE = 20\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/test_subject.py", "VALUE = 21\n")
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/test_subject.py"])
        self.assertEqual(actual, affected.dependency_graph(self.repo))

    def test_invalid_cache_rebuilds_without_changing_the_graph(self) -> None:
        expected = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        cache = self.cache_dir() / "affected-module-facts.json"
        for contents in ("broken json", "[]",
                         json.dumps({"format": -1, "extractor": affected.EXTRACTOR_VERSION, "blobs": {}}),
                         json.dumps({"format": affected.CACHE_FORMAT_VERSION, "extractor": -1, "blobs": {}}),
                         json.dumps({"format": affected.CACHE_FORMAT_VERSION, "extractor": affected.EXTRACTOR_VERSION,
                                     "blobs": {"bad": {"imports": None, "tokens": []}}})):
            with self.subTest(contents=contents):
                _ = cache.write_text(contents)
                with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
                    actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
                self.assertGreater(parse.call_count, 0)
                self.assertEqual(actual, expected)

    def test_deleted_blob_is_pruned_and_new_targets_resolve_cached_facts(self) -> None:
        self.write("scripts/component/test_caller.py", "import test_future_subject\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/test_future_subject.py", "VALUE = 42\n")
        _ = self.commit()
        blob = self.git("rev-parse", "HEAD:scripts/component/test_future_subject.py")
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/test_future_subject.py"])
        self.assertEqual(graph["scripts/component/test_caller.py"],
                         {"scripts/component/test_future_subject.py"})
        (self.repo / "scripts/component/test_future_subject.py").unlink()
        _ = self.commit()
        with mock.patch.object(ast, "parse", side_effect=AssertionError("unchanged source parsed")):
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(graph["scripts/component/test_caller.py"], set())
        self.assertNotIn(blob, affected.read_fact_cache(self.cache_dir() / "affected-module-facts.json"))

    def test_shared_blob_resolves_relative_imports_from_each_current_path(self) -> None:
        for component in ("first", "second"):
            self.write(f"scripts/{component}/test_adapter.py", "from . import subject\n")
            self.write(f"scripts/{component}/subject.py", "VALUE = 42\n")
        _ = self.commit()
        expected = affected.dependency_graph(self.repo)
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(expected[0]["scripts/first/test_adapter.py"], {"scripts/first/subject.py"})
        self.assertEqual(expected[0]["scripts/second/test_adapter.py"], {"scripts/second/subject.py"})

    def test_unstaged_sources_and_symlinks_do_not_reuse_index_blob_facts(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 42\n")
        self.write("scripts/component/test_adapter.py", "VALUE = 43\n")
        self.write("scripts/component/target.txt", "VALUE = 44\n")
        (self.repo / "scripts/component/test_link.py").symlink_to("target.txt")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/test_adapter.py", "import scripts.component.subject\n")
        self.write("scripts/component/target.txt", "import scripts.component.subject\n")
        actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(actual, affected.dependency_graph(self.repo))
        self.assertEqual(actual[0]["scripts/component/test_adapter.py"], {"scripts/component/subject.py"})
        self.assertEqual(actual[0]["scripts/component/test_link.py"], {"scripts/component/subject.py"})

    def test_bare_imports_select_each_matching_source_without_transitive_selection(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/duplicate/subject.py", "VALUE = 1\n")
        self.write("scripts/component/adapter.py", "from scripts.component import subject\n")
        self.add_test("checks/test_qualified.py", "import scripts.component.adapter\n")
        self.add_test("checks/test_bare.py", "from subject import VALUE\n")
        self.add_test("checks/test_mention.py", 'SOURCE = "../component/subject.py"\n')
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertCountEqual([path.name for path in result.modules],
                              ["test_bare.py"])
        self.assertEqual(result.changed_files, ("scripts/component/subject.py",))
        # Bare imports conservatively cover every same-named source.
        self.certify()
        self.write("scripts/duplicate/subject.py", "VALUE = 3\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_bare.py"])

    def test_imported_test_module_selects_its_importer_without_requesting_full_suite(self) -> None:
        self.add_test("agents/test_native_worker.py", "VALUE = 1\n")
        self.add_test("direct/test_workers.py", "from scripts.agents.test_native_worker import VALUE\n")
        self.certify()
        self.write("scripts/agents/test_native_worker.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_native_worker.py", "test_workers.py"])

    def test_package_initializers_and_relative_imports_are_dependencies(self) -> None:
        self.write("scripts/component/__init__.py", "VALUE = 1\n")
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/adapter.py", "from . import subject\n")
        self.add_test("checks/test_package.py", "from scripts.component.adapter import subject\n")
        self.certify()
        self.write("scripts/component/__init__.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_package.py"])

    def test_configuration_names_without_complete_paths_select_their_readers(self) -> None:
        self.write("config/limits.conf", "amount=1\n")
        self.write("scripts/component/reader.py", 'import conf\nVALUE = conf.read("limits").whole("amount")\n')
        self.add_test("checks/test_limits.py", "import reader\n")
        self.certify()
        self.record_configs({"test_limits.py": ["limits"]})
        self.write("config/limits.conf", "amount=2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_limits.py",))

    def test_configuration_changes_select_recorded_readers(self) -> None:
        readers = (
            ("module", 'from scripts.lib import conf\nVALUE = conf.read("limits")\n'),
            ("module alias", 'from scripts.lib import conf as settings\nVALUE = settings.read("limits")\n'),
            ("qualified alias", 'import scripts.lib.conf as settings\nVALUE = settings.read("limits")\n'),
            ("qualified module", 'import scripts.lib.conf\nVALUE = scripts.lib.conf.read("limits")\n'),
            ("function", 'from scripts.lib.conf import read\nVALUE = read("limits")\n'),
            ("function alias", 'from scripts.lib.conf import read as load\nVALUE = load("settings/limits.conf")\n'),
            ("path token", 'SETTINGS = "settings/limits.conf"\n'),
            ("relative cycle", 'from . import adapter\nfrom scripts.lib import conf\nVALUE = conf.read("limits")\n'),
        )
        self.write("scripts/component/adapter.py", "from . import reader\n")
        self.add_test("checks/test_limits.py", "from scripts.component import adapter\n")
        for name, reader in readers:
            with self.subTest(reader=name):
                self.write("config/limits.conf", "amount=1\n")
                self.write("config/unrelated.conf", "amount=1\n")
                self.write("scripts/component/reader.py", reader)
                self.certify()
                self.record_configs({"test_limits.py": ["limits"]})
                self.write("config/limits.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))
                self.certify()
                self.record_configs({"test_limits.py": ["limits"]})
                self.write("config/unrelated.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, ())

        fixtures = (
            'from pathlib import Path\nVALUE = (Path(__file__).parents[2] / "config/limits.conf").read_text()\n',
            'from scripts.lib import conf\nVALUE = conf.read("limits")\n',
            'from pathlib import Path\nimport shutil\nshutil.copytree(Path(__file__).parents[2] / "config", "fixture")\n',
            'from pathlib import Path\nNAME = "limits"\nVALUE = (Path(__file__).parents[2] / "config" / f"{NAME}.conf").read_text()\n',
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.write("config/limits.conf", "amount=1\n")
                self.write("scripts/component/reader_test_support.py", fixture)
                self.write("scripts/component/test_fixture.py", "from scripts.component import reader_test_support\n")
                self.write("scripts/checks/test_limits.py", "from scripts.component import test_fixture\n")
                self.certify()
                self.record_configs({})
                self.write("config/limits.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))

        self.write("config/.conf", "amount=1\n")
        self.write("scripts/checks/test_limits.py", 'NAME = ".conf"\n')
        self.certify()
        self.write("config/.conf", "amount=2\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))

    def test_configuration_selection_reuses_all_source_facts(self) -> None:
        self.write("config/limits.conf", "amount=1\n")
        self.write("scripts/component/reader.py", 'from scripts.lib import conf\nVALUE = conf.read("limits")\n')
        self.write("scripts/component/adapter.py", "import reader\n")
        self.add_test("checks/test_limits.py", "from scripts.component import adapter\n")
        self.certify()
        self.record_configs({"test_limits.py": ["limits"]})
        _ = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.write("config/limits.conf", "amount=2\n")
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertIsInstance(result, affected.AffectedSelection)
        assert isinstance(result, affected.AffectedSelection)
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_limits.py",))
        self.assertEqual(parse.call_count, 0)
        self.write("scripts/component/reader.py", 'from scripts.lib import conf\nVALUE = conf.read("limits").whole("amount")\n')
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            changed = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertEqual(changed, affected.AffectedSelection(self.base,
            ("config/limits.conf", "scripts/component/reader.py"), result.modules))
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/reader.py"])

    def test_unreferenced_inputs_select_only_tests_in_a_changed_python_folder(self) -> None:
        paths = ["scripts/lib/new.py", "scripts/other/conftest.py", "settings.json",
                 "scripts/production/merge_gate.py", "scripts/production/affected_modules.py",
                 "scripts/agents/models_config.sh"]
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "# changed\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, ())
                self.certify()

    def test_deleted_and_renamed_python_sources_select_direct_importers(self) -> None:
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/component/renamed.py", "VALUE = 1\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.deleted\n")
        self.add_test("checks/test_old_name.py", "import scripts.component.renamed\n")
        self.add_test("checks/test_new_name.py", "import scripts.component.new\n")
        self.certify()
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_deleted.py"])
        self.certify()
        _ = self.git("mv", "scripts/component/renamed.py", "scripts/component/new.py")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_old_name.py", "test_new_name.py"])

    def test_unreferenced_shell_files_and_outside_files_select_no_modules(self) -> None:
        self.add_test("component/test_orphan.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/component/new.sh", "true\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())
        self.certify()
        self.write("notes/unrelated.md", "new notes\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())

    def test_a_changed_test_selects_itself(self) -> None:
        self.add_test("checks/test_changed.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/checks/test_changed.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual([path.name for path in result.modules], ["test_changed.py"])

    def test_selections_at_and_above_forty_percent_stay_affected(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        # Four of the ten original modules depend on this module.
        for index in range(4):
            self.write(f"scripts/other/test_unrelated_{index}.py", "import scripts.component.subject\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual(len(self.selection().modules), 4)
        self.write("scripts/other/test_unrelated_4.py", "import scripts.component.subject\n")
        _ = self.commit()
        self.assertEqual(len(self.selection().modules), 5)

    def test_dirty_paths_allow_selection_but_no_base_and_invalid_tests_require_full_runs(self) -> None:
        self.write("untracked.txt", "pending\n")
        self.assertEqual(self.selection().modules, ())
        _ = self.commit()
        self.assertEqual(affected.select_affected(self.repo, affected.NoCertifiedBase(), self.modules, affected.dirty_paths(self.repo)),
                         affected.FullSuite("no certified base"))
        self.write("scripts/component/invalid.py", "this is invalid (\n")
        self.write("scripts/component/test_broken.py", "this is invalid (\n")
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("graph error:", result.reason)
        self.assertIn("broken.py", result.reason)

    def test_direct_imports_and_same_folder_select_without_a_second_import_step(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/adapters/adapter.py", "import scripts.component.subject\n")
        self.add_test("checks/test_direct.py", "import scripts.component.subject\n")
        self.add_test("checks/test_indirect.py", "import scripts.adapters.adapter\n")
        self.add_test("component/test_neighbor.py", "VALUE = 1\n")
        self.add_test("component/test_named.py", "SOURCE = 'subject.py'\n")
        self.add_test("component/test_plain.py", "import subject\n")
        self.add_test("component/test_comment.py", "# subject\n")
        self.add_test("component/test_fragment.py", "SOURCE = 'subjects my_subject subject_helper'\n")
        self.add_test("checks/test_named.py", "SOURCE = 'subject.py'\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    (self.repo / "scripts/component/subject.py").unlink()
                    _ = self.commit()
                self.assertCountEqual(self.selection().modules,
                                      [self.repo / "scripts" / path for path in
                                       ("checks/test_direct.py", "component/test_named.py",
                                        "component/test_plain.py", "component/test_comment.py")])

    def test_complete_path_tokens_select_only_the_test_that_names_the_path(self) -> None:
        self.write("config/custom.json", "{}\n")
        self.write("verify.sh", "true\n")
        self.write("scripts/adapters/adapter.py", "# config/custom.json\n")
        self.add_test("checks/test_path.py", "# ./config/custom.json\n")
        self.add_test("checks/test_root_path.py", "SCRIPT = 'verify.sh'\n")
        self.add_test("checks/test_fragment.py", "PATHS = 'custom.json old/config/custom.json config/custom.json.bak'\n")
        self.add_test("checks/test_indirect_path.py", "import scripts.adapters.adapter\n")
        self.certify()
        self.write("config/custom.json", '{"changed": true}\n')
        self.write("verify.sh", "false\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_path.py", "test_root_path.py"])

        self.certify()
        self.write("scripts/component/tool.sh", "true\n")
        self.write("scripts/component/reader_test_support.py",
                   'from pathlib import Path\nTOOL = Path(__file__).with_name("tool.sh")\n'
                   + 'from scripts.component import relay_test_support\n')
        self.write("scripts/component/relay_test_support.py",
                   "from scripts.component import reader_test_support\n")
        self.write("scripts/component/test_fixture.py", "from scripts.component import relay_test_support\n")
        self.write("scripts/adapters/path_adapter.py",
                   "from scripts.component import reader_test_support\n")
        for path, text in (
            ("component/test_support_path.py", "from scripts.component import relay_test_support\n"),
            ("checks/test_folder_path.py", "PARTS = 'component tool.sh'\n"),
            ("checks/test_basename.py", "NAME = 'tool.sh'\n"),
            ("checks/test_inherited_fixture.py", "from scripts.component import test_fixture\n"),
            ("checks/test_support_through_production.py", "from scripts.adapters import path_adapter\n"),
        ):
            self.add_test(path, text)
        self.certify()
        self.write("scripts/component/tool.sh", "false\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_support_path.py", "test_folder_path.py", "test_inherited_fixture.py"])

    def test_a_changed_script_selects_tests_of_every_script_that_calls_it(self) -> None:
        self.write("scripts/component/tool.sh", "# runner.sh calls this file.\ntrue\n")
        self.write("scripts/component/runner.sh", 'source "$SCRIPT_DIR/tool.sh"\n')
        self.write("scripts/entry/front.sh", 'exec "$HOME/.claude/scripts/component/runner.sh"\n')
        self.write("scripts/entry/launch", 'exec "$SCRIPT_DIR/front.sh"\n')
        (self.repo / "scripts/entry/launch").chmod(0o755)
        self.write("scripts/other/elsewhere.sh", 'source "$SCRIPT_DIR/tool.sh"\n')
        self.write("scripts/other/fragment.sh", "cat old/component/tool.sh\n")
        self.write("scripts/component/helper.py", 'COMMAND = ["scripts/component/tool.sh"]\n')
        for name, text in (
            ("test_tool.py", "SCRIPT = 'scripts/component/tool.sh'\n"),
            ("test_runner.py", "PARTS = 'component runner.sh'\n"),
            ("test_front.py", "SCRIPT = 'scripts/entry/front.sh'\n"),
            ("test_launch.py", "SCRIPT = 'scripts/entry/launch'\n"),
            ("test_elsewhere.py", "SCRIPT = 'scripts/other/elsewhere.sh'\n"),
            ("test_fragment.py", "SCRIPT = 'scripts/other/fragment.sh'\n"),
            ("test_helper.py", "SCRIPT = 'scripts/component/helper.py'\n"),
            ("test_relative_front.py", "SCRIPT = 'SCRIPT_DIR/front.sh'\n"),
        ):
            self.add_test(f"checks/{name}", text)
        self.certify()
        expected = ["test_tool.py", "test_runner.py", "test_front.py", "test_launch.py"]
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    (self.repo / "scripts/component/tool.sh").unlink()
                else:
                    self.write("scripts/component/tool.sh", "# runner.sh calls this file.\nfalse\n")
                _ = self.commit()
                self.assertCountEqual([path.name for path in self.selection().modules], expected)
                self.certify()

    def test_dirty_paths_include_edits_untracked_files_deletions_and_renames(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/component/old.py", "VALUE = 1\n")
        self.add_test("checks/test_dirty.py", "import scripts.component.subject\n")
        self.add_test("checks/test_new.py", "import scripts.component.new\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.deleted\n")
        self.add_test("checks/test_old.py", "import scripts.component.old\n")
        self.add_test("checks/test_renamed.py", "import scripts.component.renamed\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        self.write("scripts/component/new.py", "VALUE = 3\n")
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.git("mv", "scripts/component/old.py", "scripts/component/renamed.py")
        result = self.selection()
        self.assertCountEqual([path.name for path in result.modules],
                              ["test_dirty.py", "test_new.py", "test_deleted.py", "test_old.py", "test_renamed.py"])
        self.assertEqual(result.changed_files,
                         ("scripts/component/deleted.py", "scripts/component/new.py", "scripts/component/old.py",
                          "scripts/component/renamed.py", "scripts/component/subject.py"))

    def test_removed_python_sources_reuse_both_graphs_without_parsing_or_cache_writes(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.subject\n")
        self.certify()
        (self.repo / "scripts/component/subject.py").unlink()
        self.write("scripts/checks/test_deleted.py", "import scripts.component.subject\n# Source removed.\n")
        _ = self.commit()
        first = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                         state_dir=self.cache_dir())
        self.assertIsInstance(first, affected.AffectedSelection)
        with (mock.patch.object(ast, "parse", wraps=ast.parse) as parse,
              mock.patch.object(affected, "write_fact_cache", wraps=affected.write_fact_cache) as write,
              mock.patch.object(affected, "blob_texts", wraps=affected.blob_texts) as read):
            second = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                              state_dir=self.cache_dir())
        self.assertEqual(second, first)
        self.assertEqual(parse.call_count, 0)
        write.assert_not_called()
        read.assert_not_called()

    def test_a_dirty_python_test_does_not_write_the_fact_cache(self) -> None:
        self.add_test("checks/test_dirty.py", "VALUE = 1\n")
        self.certify()
        expected = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                            state_dir=self.cache_dir())
        self.assertIsInstance(expected, affected.AffectedSelection)
        self.write("scripts/checks/test_dirty.py", "VALUE = 2\n")
        with mock.patch.object(affected, "write_fact_cache", wraps=affected.write_fact_cache) as write:
            result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                              state_dir=self.cache_dir())
        self.assertIsInstance(result, affected.AffectedSelection)
        assert isinstance(result, affected.AffectedSelection)
        self.assertEqual([path.name for path in result.modules], ["test_dirty.py"])
        write.assert_not_called()

    def test_invalid_non_test_python_sources_still_select_their_importers(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_importer.py", "import scripts.component.subject\n")
        self.certify()
        self.write("scripts/component/subject.py", "this is invalid (\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_importer.py"])

    def test_a_deleted_test_does_not_select_its_folder_neighbors(self) -> None:
        self.add_test("checks/test_deleted.py", "VALUE = 1\n")
        self.add_test("checks/test_neighbor.py", "VALUE = 2\n")
        self.certify()
        deleted = self.repo / "scripts/checks/test_deleted.py"
        deleted.unlink()
        self.modules.remove(deleted)
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())


if __name__ == "__main__":
    _ = unittest.main()
