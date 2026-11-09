"""Selection contracts against small committed source trees."""
from __future__ import annotations

import ast
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import override
from unittest import mock

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
        _ = self.git("config", "maintenance.auto", "false")
        _ = self.git("config", "user.name", "Selection Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
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

    def selection(self) -> affected.AffectedSelection:
        result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.AffectedSelection, result)
        assert isinstance(result, affected.AffectedSelection)
        return result

    def cache_dir(self) -> Path:
        return self.repo / ".git" / "gate-state"

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
        self.write("config/limits.conf", "amount=2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_limits.py",))

    def test_configuration_changes_select_transitive_readers(self) -> None:
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
                self.write("config/limits.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))
                self.certify()
                self.write("config/unrelated.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, ())

    def test_configuration_selection_reuses_all_source_facts(self) -> None:
        self.write("config/limits.conf", "amount=1\n")
        self.write("scripts/component/reader.py", 'from scripts.lib import conf\nVALUE = conf.read("limits")\n')
        self.write("scripts/component/adapter.py", "import reader\n")
        self.add_test("checks/test_limits.py", "from scripts.component import adapter\n")
        self.certify()
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
                expected = tuple(self.modules) if path == "scripts/other/conftest.py" else ()
                self.assertEqual(self.selection().modules, expected)
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
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_direct.py", "test_neighbor.py"])

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
