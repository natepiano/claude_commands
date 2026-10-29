"""Selection contracts against small committed source trees."""
from __future__ import annotations

import ast
from collections import defaultdict
from datetime import UTC, datetime
from importlib.metadata import packages_distributions
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import override
from unittest import mock

from scripts.production import affected_modules as affected


# Independent pre-cache traversal, retained as the graph-equivalence oracle.
def reference_scans_tree(tree: ast.AST) -> bool:
    """Include scans of fixture trees too: treating those as repository scans is safe."""
    recursive = {"walk", "rglob"}
    globbers = {"glob", "iglob"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module == "os" and alias.name == "walk":
                    recursive.add(alias.asname or alias.name)
                if node.module == "glob" and alias.name in {"glob", "iglob"}:
                    globbers.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            if name in recursive:
                return True
            if name in globbers and any(
                    isinstance(value, ast.Constant) and isinstance(value.value, str) and "**" in value.value
                    for value in ast.walk(node)):
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "ls-files" in node.value:
            return True
    return False


def reference_graph(repo: Path, tracked: set[str], changed_files: tuple[str, ...] = ()) -> tuple[dict[str, set[str]], set[str]]:
    by_name: dict[str, set[str]] = defaultdict(set)
    bare_modules: dict[str, set[str]] = defaultdict(set)
    # Keep removed data/shell files as dependency targets, so their old names
    # still connect callers across suites. Deleted Python already forces full.
    targets = tracked | {path for path in changed_files if path.startswith(("scripts/", "config/"))}
    for path in targets:
        by_name[Path(path).name].add(path)
        if path.startswith("scripts/") and path.endswith(".py"):
            name = Path(path).parent.name if Path(path).name == "__init__.py" else Path(path).stem
            bare_modules[name].add(path)
    mentions = affected.FilenameDependencies(by_name)
    external = set(sys.stdlib_module_names) | set(packages_distributions())
    graph: dict[str, set[str]] = {}
    scanners: set[str] = set()
    for path in sorted(tracked):
        if not path.startswith("scripts/") or Path(path).suffix not in {".py", ".sh"}:
            continue
        text = (repo / path).read_text(encoding="utf-8")
        dependencies: set[str] = set()
        if path.endswith(".py"):
            tree = ast.parse(text, filename=path)
            if reference_scans_tree(tree):
                scanners.add(path)
            strings: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    strings.append(node.value)
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        parents = Path(path).parent.parts
                        module = ".".join((*parents[:len(parents) - node.level + 1], *module.split("."))).rstrip(".")
                    imports = [module, *(f"{module}.{alias.name}" for alias in node.names)]
                else:
                    continue
                for module in imports:
                    root = module.split(".")[0]
                    if root == "scripts":
                        dependencies.update(affected.module_dependencies(module, tracked))
                    elif root and root not in external:
                        dependencies.update(bare_modules.get(root, set()))
            text = "\0".join(strings)
        # These full-suite triggers hold selection data, not runtime file uses.
        # Their imports still contribute dependencies above.
        if path not in {"scripts/production/affected_modules.py", "scripts/production/merge_gate.py"}:
            dependencies.update(mentions.in_text(text))
        dependencies.discard(path)
        graph[path] = dependencies
    return graph, scanners



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

    def cache_dir(self) -> Path:
        return self.repo / ".git" / "gate-state"

    def tracked(self) -> set[str]:
        return set(self.git("ls-files", "-z", "--", "scripts", "config").split("\0")) - {""}

    def test_single_pass_and_cached_graphs_equal_the_reference_graph(self) -> None:
        sources = {
            "scripts/component/__init__.py": "VALUE = 1\n",
            "scripts/component/subject.py": "VALUE = 2\n",
            "scripts/duplicate/subject.py": "VALUE = 3\n",
            "scripts/component/adapter.py": "from . import subject\nfrom ..component import subject\n",
            "scripts/checks/imports.py": "import scripts.component.adapter, subject\nfrom subject import VALUE\nimport json\n",
            "scripts/checks/recursive.py": "scan('.')\nfrom os import walk as scan\n",
            "scripts/checks/recursive_glob.py": "scan(wrap('**/*.py'))\nfrom glob import iglob as scan\n",
            "scripts/checks/attribute_glob.py": "other.glob(pattern=wrap('**'))\n",
            "scripts/checks/nested_glob.py": "glob((lambda: '**')())\n",
            "scripts/checks/plain_glob.py": "glob('*')\nUNRELATED = '**'\n",
            "scripts/checks/alias_plain_glob.py": "scan('*')\nfrom glob import glob as scan\n",
            "scripts/checks/path_walk.py": "other.walk()\n",
            "scripts/checks/rglob.py": "other.rglob('*')\n",
            "scripts/checks/index_scan.py": "COMMAND = 'git ls-files'\n",
            "scripts/checks/runner.sh": "cat custom.json removed.json\n",
            "scripts/checks/mention.py": "SOURCE = 'subject.py runner.sh'\n",
            "scripts/production/affected_modules.py": "FILES = ['subject.py']\nimport scripts.component.adapter\n",
            "scripts/production/merge_gate.py": "FILES = ['subject.py']\nimport scripts.component.adapter\n",
            "config/custom.json": "{}\n",
        }
        for path, text in sources.items():
            self.write(path, text)
        _ = self.commit()
        tracked = self.tracked()
        removed = ("config/removed.json",)
        expected = reference_graph(self.repo, tracked, removed)
        cold = affected.dependency_graph(self.repo, tracked, removed, state_dir=self.cache_dir())
        with mock.patch.object(ast, "parse", side_effect=AssertionError("warm cache parsed source")):
            warm = affected.dependency_graph(self.repo, tracked, removed, state_dir=self.cache_dir())
        self.assertEqual(cold, expected)
        self.assertEqual(warm, expected)

    def test_new_blob_reparses_only_the_changed_file(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 20\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/subject.py", "VALUE = 21\n")
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/subject.py"])
        self.assertEqual(actual, reference_graph(self.repo, self.tracked()))

    def test_invalid_cache_rebuilds_without_changing_the_graph(self) -> None:
        expected = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        cache = self.cache_dir() / "affected-module-facts.json"
        for contents in ("broken json", "[]",
                         json.dumps({"format": -1, "extractor": affected.EXTRACTOR_VERSION, "blobs": {}}),
                         json.dumps({"format": affected.CACHE_FORMAT_VERSION, "extractor": -1, "blobs": {}}),
                         json.dumps({"format": affected.CACHE_FORMAT_VERSION, "extractor": affected.EXTRACTOR_VERSION,
                                     "blobs": {"bad": {".py": {"imports": None}}}})):
            with self.subTest(contents=contents):
                _ = cache.write_text(contents)
                with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
                    actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
                self.assertGreater(parse.call_count, 0)
                self.assertEqual(actual, expected)

    def test_deleted_blob_is_pruned_and_new_targets_resolve_cached_facts(self) -> None:
        self.write("scripts/component/caller.py", "import future_subject\nNAME = 'future.json'\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/future_subject.py", "VALUE = 42\n")
        self.write("config/future.json", "{}\n")
        _ = self.commit()
        blob = self.git("rev-parse", "HEAD:scripts/component/future_subject.py")
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/future_subject.py"])
        self.assertEqual(graph["scripts/component/caller.py"],
                         {"scripts/component/future_subject.py", "config/future.json"})
        (self.repo / "scripts/component/future_subject.py").unlink()
        (self.repo / "config/future.json").unlink()
        _ = self.commit()
        with mock.patch.object(ast, "parse", side_effect=AssertionError("unchanged source parsed")):
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(graph["scripts/component/caller.py"], set())
        self.assertNotIn(blob, affected.read_fact_cache(self.cache_dir() / "affected-module-facts.json"))

    def test_shared_blob_resolves_relative_imports_from_each_current_path(self) -> None:
        for component in ("first", "second"):
            self.write(f"scripts/{component}/adapter.py", "from . import subject\n")
            self.write(f"scripts/{component}/subject.py", "VALUE = 42\n")
        # Identical source in a shell file must retain its full text as mentions.
        self.write("scripts/first/runner.py", "# subject.py\n")
        self.write("scripts/first/runner.sh", "# subject.py\n")
        _ = self.commit()
        expected = reference_graph(self.repo, self.tracked())
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(expected[0]["scripts/first/adapter.py"], {"scripts/first/subject.py"})
        self.assertEqual(expected[0]["scripts/second/adapter.py"], {"scripts/second/subject.py"})

    def test_unstaged_sources_and_symlinks_do_not_reuse_index_blob_facts(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 42\n")
        self.write("scripts/component/adapter.py", "VALUE = 43\n")
        self.write("scripts/component/target.txt", "VALUE = 44\n")
        (self.repo / "scripts/component/link.py").symlink_to("target.txt")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/adapter.py", "import scripts.component.subject\n")
        self.write("scripts/component/target.txt", "import scripts.component.subject\n")
        actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(actual, reference_graph(self.repo, self.tracked()))
        self.assertEqual(actual[0]["scripts/component/adapter.py"], {"scripts/component/subject.py"})
        self.assertEqual(actual[0]["scripts/component/link.py"], {"scripts/component/subject.py"})

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

    def test_imported_test_module_selects_its_importer_without_requesting_full_suite(self) -> None:
        self.add_test("agents/test_codex_mesh.py", "VALUE = 1\n")
        self.add_test("direct/test_workers.py", "from scripts.agents.test_codex_mesh import VALUE\n")
        self.certify()
        self.write("scripts/agents/test_codex_mesh.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_codex_mesh.py", "test_workers.py"])

    def test_selection_file_strings_do_not_connect_dependents_but_imports_do(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/imported.py", "VALUE = 1\n")
        for name in ("affected_modules", "merge_gate"):
            self.write(f"scripts/production/{name}.py",
                       'FILES = ["subject.py"]\nimport scripts.component.imported\n')
            self.add_test(f"checks/test_{name}.py", f"import scripts.production.{name}\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())
        self.certify()
        self.write("scripts/component/imported.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_affected_modules.py", "test_merge_gate.py"])

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
                 "config/agents.conf", "scripts/agents/agents_config.sh"]
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
