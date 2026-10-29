"""Selection contracts against small committed source trees."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import override

from scripts.production import affected_modules as affected


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
        _ = self.git("config", "user.name", "Selection Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        self.write(".gitignore", "__pycache__/\n")
        # Ten unrelated modules leave space below the 40% fail-closed limit.
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

    def selection(self) -> affected.AffectedSelection:
        result = affected.select_affected(self.repo, self.base, self.modules)
        self.assertIsInstance(result, affected.AffectedSelection, result)
        assert isinstance(result, affected.AffectedSelection)
        return result

    def test_imports_bare_import_collisions_and_filename_mentions_follow_transitive_dependencies(self) -> None:
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
                              ["test_qualified.py", "test_bare.py", "test_mention.py"])
        self.assertEqual(result.changed_files, ("scripts/component/subject.py",))
        # Bare imports conservatively cover every same-named source.
        self.certify()
        self.write("scripts/duplicate/subject.py", "VALUE = 3\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_bare.py", "test_mention.py"])

    def test_package_initializers_and_relative_imports_are_dependencies(self) -> None:
        self.write("scripts/component/__init__.py", "VALUE = 1\n")
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/adapter.py", "from . import subject\n")
        self.add_test("checks/test_package.py", "from scripts.component.adapter import subject\n")
        self.certify()
        self.write("scripts/component/__init__.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_package.py"])

    def test_overlapping_filename_mentions_preserve_both_dependencies(self) -> None:
        self.write("config/aba", "first\n")
        self.write("config/bab", "second\n")
        self.add_test("checks/test_overlap.py", 'TEXT = "abab"\n')
        self.certify()
        self.write("config/bab", "changed\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_overlap.py"])

    def test_shell_and_config_filename_dependencies_are_transitive(self) -> None:
        self.write("config/custom.json", "{}\n")
        self.write("scripts/component/runner.sh", 'cat ../config/custom.json\n')
        self.add_test("checks/test_shell.py", 'SCRIPT = "runner.sh"\n')
        self.certify()
        self.write("config/custom.json", '{"changed": true}\n')
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_shell.py"])
        self.certify()
        (self.repo / "scripts/component/runner.sh").unlink()
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_shell.py"])

    def test_every_shared_input_trigger_requests_full_validation(self) -> None:
        paths = ["scripts/lib/new.py", "scripts/other/conftest.py", "settings.json",
                 "scripts/production/merge_gate.py", "scripts/production/affected_modules.py",
                 "config/agents.conf", "scripts/agents/agents_config.sh",
                 *sorted(affected.SHARED_TEST_HELPERS)]
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "# changed\n")
                _ = self.commit()
                result = affected.select_affected(self.repo, self.base, self.modules)
                self.assertIsInstance(result, affected.FullSuite)
                assert isinstance(result, affected.FullSuite)
                self.assertIn(path, result.reason)
                self.certify()

    def test_deleted_and_renamed_python_sources_request_full_validation(self) -> None:
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/component/renamed.py", "VALUE = 1\n")
        self.certify()
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules)
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("Python file deleted", result.reason)
        self.certify()
        _ = self.git("mv", "scripts/component/renamed.py", "scripts/component/new.py")
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules)
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("Python file renamed", result.reason)

    def test_unreferenced_suite_files_select_the_suite_and_outside_files_select_nothing(self) -> None:
        self.add_test("component/test_orphan.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/component/new.sh", "true\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_orphan.py"])
        self.certify()
        self.write("notes/unrelated.md", "new notes\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())

    def test_changed_test_and_all_tree_scan_forms_are_always_selected(self) -> None:
        scans = ["import os\nos.walk('.')\n", "from pathlib import Path\nPath('.').rglob('*')\n",
                 "from glob import glob\nglob('**/*.py')\n", "COMMAND = ['git', 'ls-files']\n",
                 "from glob import glob as scan\nscan('**/*.sh')\n"]
        for index, source in enumerate(scans):
            self.add_test(f"scanners/test_scan_{index}.py", source)
        self.add_test("checks/test_changed.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/checks/test_changed.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(len(result.tree_scanners), 5)
        self.assertCountEqual([path.name for path in result.modules],
                              ["test_changed.py", *(f"test_scan_{index}.py" for index in range(5))])

    def test_over_forty_percent_fails_closed_but_exactly_forty_does_not(self) -> None:
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
        result = affected.select_affected(self.repo, self.base, self.modules)
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("more than 40%", result.reason)

    def test_dirty_tree_no_base_and_unparsable_source_fail_closed(self) -> None:
        self.write("untracked.txt", "pending\n")
        self.assertEqual(affected.select_affected(self.repo, self.base, self.modules), affected.FullSuite("tree not clean"))
        _ = self.commit()
        self.assertEqual(affected.select_affected(self.repo, affected.NoCertifiedBase(), self.modules),
                         affected.FullSuite("no certified base"))
        self.write("scripts/component/broken.py", "this is invalid (\n")
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules)
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("graph error:", result.reason)
        self.assertIn("broken.py", result.reason)


if __name__ == "__main__":
    _ = unittest.main()
