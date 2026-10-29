"""Conservative test selection from a certified commit and tracked source dependencies.

No source is imported or evaluated. Unreadable git state or source always requests
the full suite; callers own certification, reporting and execution.
"""
from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from importlib.metadata import packages_distributions
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from typing import cast


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
    return (path.startswith("scripts/lib/")
            or name in {"conftest.py", "settings.json", "merge_gate.py", "affected_modules.py", "agents_config.sh"}
            or (Path(path).parent == Path("config") and name.endswith(".conf")))


# Bump the extractor version whenever source-fact semantics change.
CACHE_FORMAT_VERSION = 1
EXTRACTOR_VERSION = 1


@dataclass(frozen=True)
class SourceFacts:
    # Relative imports retain their level until resolved from the current path.
    imports: tuple[tuple[int, str, tuple[str, ...]], ...]
    text: str
    scans: bool


def extract_facts(text: str, path: str) -> SourceFacts:
    if path.endswith(".sh"):
        return SourceFacts((), text, False)
    tree = ast.parse(text, filename=path)
    imports: list[tuple[int, str, tuple[str, ...]]] = []
    strings: set[str] = set()
    recursive = {"walk", "rglob"}
    globbers = {"glob", "iglob"}
    calls: set[str] = set()
    recursive_globs: set[str] = set()
    # Each node is visited once. A ** string belongs to every enclosing call,
    # including when nested in another expression, as in the old subtree walk.
    pending: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
    while pending:
        node, enclosing = pending.pop()
        if isinstance(node, ast.Import):
            imports.extend((0, alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.level, node.module or "", tuple(alias.name for alias in node.names)))
            for alias in node.names:
                if node.module == "os" and alias.name == "walk":
                    recursive.add(alias.asname or alias.name)
                if node.module == "glob" and alias.name in {"glob", "iglob"}:
                    globbers.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            calls.add(name)
            enclosing = (*enclosing, name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
            if "**" in node.value:
                recursive_globs.update(enclosing)
        pending.extend((child, enclosing) for child in ast.iter_child_nodes(node))
    # Aliases apply regardless of where their import appeared in the AST.
    scans = bool(calls & recursive or recursive_globs & globbers or any("ls-files" in text for text in strings))
    return SourceFacts(tuple(imports), "\0".join(sorted(strings)), scans)


def indexed_files(repo: Path) -> tuple[set[str], dict[str, str]]:
    tracked: set[str] = set()
    blobs: dict[str, str] = {}
    for entry in git_output(repo, "ls-files", "--stage", "-z", "--", "scripts", "config").split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if stage != "0":
            raise GraphError(f"unmerged index entry: {path}")
        tracked.add(path)
        # A symlink's blob hashes its target name, not the text we read through it.
        if mode in {"100644", "100755"}:
            blobs[path] = blob
    return tracked, blobs


def decoded_facts(value: object) -> SourceFacts:
    if not isinstance(value, dict):
        raise ValueError("invalid source facts")
    row = cast(dict[str, object], value)
    imports, text, scans = row.get("imports"), row.get("text"), row.get("scans")
    if not isinstance(imports, list) or not isinstance(text, str) or not isinstance(scans, bool):
        raise ValueError("invalid source facts fields")
    decoded: list[tuple[int, str, tuple[str, ...]]] = []
    for item in cast(list[object], imports):
        if not isinstance(item, list) or len(cast(list[object], item)) != 3:
            raise ValueError("invalid source import")
        level, module, names = cast(list[object], item)
        if (not isinstance(level, int) or level < 0 or not isinstance(module, str)
                or not isinstance(names, list)
                or not all(isinstance(name, str) for name in cast(list[object], names))):
            raise ValueError("invalid source import fields")
        decoded.append((level, module, tuple(cast(list[str], names))))
    return SourceFacts(tuple(decoded), text, scans)


def read_fact_cache(path: Path) -> dict[str, dict[str, SourceFacts]]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            return {}
        row = cast(dict[str, object], value)
        if row.get("format") != CACHE_FORMAT_VERSION or row.get("extractor") != EXTRACTOR_VERSION:
            return {}
        blobs = row.get("blobs")
        if not isinstance(blobs, dict):
            return {}
        result: dict[str, dict[str, SourceFacts]] = {}
        for blob, kinds in cast(dict[str, object], blobs).items():
            if not isinstance(kinds, dict):
                return {}
            result[blob] = {}
            for kind, facts in cast(dict[str, object], kinds).items():
                if kind not in {".py", ".sh"}:
                    return {}
                result[blob][kind] = decoded_facts(facts)
        return result
    except (OSError, ValueError):
        return {}


def write_fact_cache(path: Path, facts: dict[str, dict[str, SourceFacts]]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"format": CACHE_FORMAT_VERSION, "extractor": EXTRACTOR_VERSION,
                 "blobs": {blob: {kind: asdict(entry) for kind, entry in kinds.items()}
                           for blob, kinds in facts.items()}}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output)
        os.replace(temporary, path)
    except OSError:
        # The cache is optional; inability to persist it cannot change selection.
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def module_dependencies(module: str, tracked: set[str]) -> set[str]:
    parts = module.split(".")
    prefix = "/".join(parts)
    candidates = {f"{prefix}.py", f"{prefix}/__init__.py"}
    candidates.update("/".join(parts[:index]) + "/__init__.py" for index in range(1, len(parts)))
    return candidates & tracked


def dependency_graph(repo: Path, tracked: set[str] | None = None, changed_files: Sequence[str] = (),
                     *, state_dir: Path | None = None) -> tuple[dict[str, set[str]], set[str]]:
    blobs: dict[str, str] = {}
    if tracked is None or state_dir is not None:
        indexed, blobs = indexed_files(repo)
        if tracked is None:
            tracked = indexed
    cache_path = state_dir / "affected-module-facts.json" if state_dir is not None else None
    cached = read_fact_cache(cache_path) if cache_path is not None else {}
    current: dict[str, dict[str, SourceFacts]] = {}
    cache_changed = False
    # Direct graph callers can inspect dirty trees; never cache their working
    # edits under the index blob. The gate itself already requires a clean tree.
    dirty: set[str] = set(git_output(repo, "diff-files", "--name-only", "-z", "--", "scripts", "config").split("\0")) if cache_path else set()
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
        kind = Path(path).suffix
        blob = blobs.get(path) if path not in dirty else None
        facts = cached.get(blob, {}).get(kind) if blob is not None else None
        if facts is None:
            cache_changed = True
            facts = extract_facts((repo / path).read_text(encoding="utf-8"), path)
            if blob is not None:
                cached.setdefault(blob, {})[kind] = facts
        if blob is not None:
            current.setdefault(blob, {})[kind] = facts
        if facts.scans:
            scanners.add(path)
        dependencies: set[str] = set()
        for level, module, names in facts.imports:
            if level:
                parents = Path(path).parent.parts
                module = ".".join((*parents[:len(parents) - level + 1], *module.split("."))).rstrip(".")
            for imported in (module, *(f"{module}.{name}" for name in names)):
                root = imported.split(".")[0]
                if root == "scripts":
                    dependencies.update(module_dependencies(imported, tracked))
                elif root and root not in external:
                    dependencies.update(bare_modules.get(root, set()))
        # These full-suite triggers hold selection data, not runtime file uses.
        # Their imports still contribute dependencies above.
        if path not in {"scripts/production/affected_modules.py", "scripts/production/merge_gate.py"}:
            dependencies.update(mentions.in_text(facts.text))
        dependencies.discard(path)
        graph[path] = dependencies
    if cache_path is not None and (cache_changed or current != cached):
        write_fact_cache(cache_path, current)
    return graph, scanners


def select_affected(repo: Path, base: CertifiedBase | NoCertifiedBase,
                    modules: Sequence[Path], *, state_dir: Path | None = None) -> AffectedSelection | FullSuite:
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
        graph, scanners = dependency_graph(repo, changed_files=tuple(changed), state_dir=state_dir)
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
