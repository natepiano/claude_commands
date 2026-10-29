"""Conservative test selection from a certified commit and tracked source dependencies.

No source is imported or evaluated. Unreadable git state or source always requests
the full suite; callers own certification, reporting and execution.
"""
from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.metadata import packages_distributions
from pathlib import Path
import subprocess
import sys


# Shared fixture providers, including test modules imported by other modules.
# Changing their setup/patch boundaries requires a full isolation check.
SHARED_TEST_HELPERS = frozenset({
    "scripts/shutdown/test_support.py", "scripts/shutdown/test_account_move.py",
    "scripts/shutdown/test_inventory.py", "scripts/production/cli_fixture.py",
    "scripts/production/add_unit_test_support.py",
    "scripts/usage/fixtures/collect/mac/fake_ssh.py",
    "scripts/direct/recorder_fixture.py", "scripts/direct/wait_for_test_state.py",
    "scripts/direct/test_verify_mac_offload.py", "scripts/direct/test_verify_doc.py",
    "scripts/direct/test_verify_lint_scope.py", "scripts/direct/test_verify_token_wait.py",
    "scripts/direct/test_verify_untested_examples.py", "scripts/accounts/nix_env_fake.py",
    "scripts/accounts/test_refusal_watch.py", "scripts/agents/test_codex_mesh.py",
    "scripts/build_hold/test_build_hold.py", "scripts/build_hold/test_hold_applies.py",
    "scripts/buildlog/test_index.py", "scripts/model_study/test_report.py",
    "scripts/lib/nix_env_test_support.py", "scripts/lib/test_claude_launch_scan.py",
})


@dataclass(frozen=True)
class CertifiedBase:
    commit: str
    at: str


@dataclass(frozen=True)
class NoCertifiedBase:
    pass


@dataclass(frozen=True)
class AffectedSelection:
    base: CertifiedBase
    changed_files: tuple[str, ...]
    modules: tuple[Path, ...]
    tree_scanners: tuple[Path, ...]


@dataclass(frozen=True)
class FullSuite:
    reason: str


class GraphError(Exception):
    """Git or source cannot establish a safe dependency graph."""


@dataclass
class FilenameSearchState:
    next_character: dict[str, int] = field(default_factory=dict)
    fallback: int = 0
    dependencies: set[str] = field(default_factory=set)


class FilenameDependencies:
    """Match all tracked basenames, including overlaps, in one text pass.

    Failure links reuse the longest remaining prefix after a mismatch. This
    avoids trying every filename at every character in large fixture strings.
    """

    def __init__(self, by_name: dict[str, set[str]]) -> None:
        self.states: list[FilenameSearchState] = [FilenameSearchState()]
        for name, paths in by_name.items():
            state = 0
            for character in name:
                transitions = self.states[state].next_character
                if character not in transitions:
                    transitions[character] = len(self.states)
                    self.states.append(FilenameSearchState())
                state = transitions[character]
            self.states[state].dependencies.update(paths)
        queue = deque(self.states[0].next_character.values())
        while queue:
            state = queue.popleft()
            for character, following in self.states[state].next_character.items():
                queue.append(following)
                fallback = self.states[state].fallback
                while fallback and character not in self.states[fallback].next_character:
                    fallback = self.states[fallback].fallback
                fallback = self.states[fallback].next_character.get(character, 0)
                self.states[following].fallback = fallback
                self.states[following].dependencies.update(self.states[fallback].dependencies)

    def in_text(self, text: str) -> set[str]:
        dependencies: set[str] = set()
        state = 0
        for character in text:
            while state and character not in self.states[state].next_character:
                state = self.states[state].fallback
            state = self.states[state].next_character.get(character, 0)
            dependencies.update(self.states[state].dependencies)
        return dependencies


def git_output(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise GraphError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def full_suite_trigger(path: str) -> bool:
    name = Path(path).name
    return (path.startswith("scripts/lib/") or path in SHARED_TEST_HELPERS
            or name in {"conftest.py", "settings.json", "merge_gate.py", "affected_modules.py", "agents_config.sh"}
            or (Path(path).parent == Path("config") and name.endswith(".conf")))


def scans_tree(tree: ast.AST) -> bool:
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


def module_dependencies(module: str, tracked: set[str]) -> set[str]:
    parts = module.split(".")
    prefix = "/".join(parts)
    candidates = {f"{prefix}.py", f"{prefix}/__init__.py"}
    candidates.update("/".join(parts[:index]) + "/__init__.py" for index in range(1, len(parts)))
    return candidates & tracked


def dependency_graph(repo: Path, tracked: set[str], changed_files: Sequence[str] = ()) -> tuple[dict[str, set[str]], set[str]]:
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
    mentions = FilenameDependencies(by_name)
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
            if scans_tree(tree):
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
                        dependencies.update(module_dependencies(module, tracked))
                    elif root and root not in external:
                        dependencies.update(bare_modules.get(root, set()))
            text = "\0".join(strings)
        dependencies.update(mentions.in_text(text))
        dependencies.discard(path)
        graph[path] = dependencies
    return graph, scanners


def select_affected(repo: Path, base: CertifiedBase | NoCertifiedBase,
                    modules: Sequence[Path]) -> AffectedSelection | FullSuite:
    try:
        if git_output(repo, "status", "--porcelain", "--untracked-files=all").strip():
            return FullSuite("tree not clean")
        if isinstance(base, NoCertifiedBase):
            return FullSuite("no certified base")
        changes = git_output(repo, "diff", "--name-status", "-z", "-M", base.commit, "HEAD").split("\0")
        changed: set[str] = set()
        index = 0
        while index < len(changes) and changes[index]:
            status, path = changes[index:index + 2]
            index += 2
            changed.add(path)
            if status.startswith(("R", "C")):
                target = changes[index]
                index += 1
                changed.add(target)
                if status.startswith("R") and (path.endswith(".py") or target.endswith(".py")):
                    return FullSuite(f"Python file renamed: {path} -> {target}")
            if status == "D" and path.endswith(".py"):
                return FullSuite(f"Python file deleted: {path}")
        for path in sorted(changed):
            if full_suite_trigger(path):
                return FullSuite(f"shared gate input changed: {path}")
        tracked = {path for path in git_output(repo, "ls-files", "-z", "--", "scripts", "config").split("\0") if path}
        graph, scanners = dependency_graph(repo, tracked, tuple(changed))
        dependents: dict[str, set[str]] = defaultdict(set)
        for path, dependencies in graph.items():
            for dependency in dependencies:
                dependents[dependency].add(path)
        reached = set(changed)
        queue = deque(changed)
        while queue:
            for path in dependents[queue.popleft()] - reached:
                reached.add(path)
                queue.append(path)
        orphan_suites = {Path(path).parts[1] for path in changed
                         if path.startswith("scripts/") and len(Path(path).parts) >= 3 and not dependents[path]}
        selected = tuple(path for path in modules if path.relative_to(repo).as_posix() in reached | scanners
                         or path.relative_to(repo).parts[1] in orphan_suites)
        if len(selected) * 5 > len(modules) * 2:
            return FullSuite(f"more than 40% of modules affected ({len(selected)} of {len(modules)})")
        return AffectedSelection(base, tuple(sorted(changed)), selected,
                                 tuple(path for path in modules if path.relative_to(repo).as_posix() in scanners))
    except (GraphError, OSError, ValueError, SyntaxError) as error:
        return FullSuite(f"graph error: {error}")
