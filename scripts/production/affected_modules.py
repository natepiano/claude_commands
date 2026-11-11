"""Select tests from imports, recorded reads, folder scans, and script calls.

No source is imported or evaluated. Callers own certification and execution.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from io import BytesIO
from pathlib import Path
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import cast

from scripts.lib import conf
from scripts.test_times.gate_record import (
    ModuleReadRecord, NoConfigReadRecord, NoFolderScanRecord, RecordedConfigReads,
    RecordedFolderScans, module_read_record,
)
from scripts.production.gate_modules import is_test_module
from scripts.style.git_process import GitBlob, parse_status_z, parse_tree_listing, read_batch_blob


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


@dataclass(frozen=True)
class FullSuite:
    reason: str


class GraphError(Exception):
    """Git or source cannot establish a safe dependency graph."""


def git_output(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, errors="replace", check=False)
    if result.returncode:
        raise GraphError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


# Bump the extractor version whenever source-fact semantics change.
CACHE_FORMAT_VERSION = 2
EXTRACTOR_VERSION = 6
PATH_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")


@dataclass(frozen=True)
class ValidPython:
    pass


@dataclass(frozen=True)
class InvalidPython:
    message: str


@dataclass(frozen=True)
class SourceFacts:
    # Relative imports retain their level until resolved from the current path.
    imports: tuple[tuple[int, str, tuple[str, ...]], ...]
    tokens: frozenset[str]
    config_reads: frozenset[str]
    syntax: ValidPython | InvalidPython = ValidPython()


@dataclass(frozen=True)
class SourceGraph:
    imports: dict[str, set[str]]
    tokens: dict[str, frozenset[str]]
    config_reads: dict[str, frozenset[str]]


def path_tokens(text: str) -> frozenset[str]:
    return frozenset(match.group().removeprefix("./") for match in PATH_TOKEN.finditer(text))


def extract_facts(text: str, path: str) -> SourceFacts:
    tokens = path_tokens(text)
    config_reads = {Path(token).name.removesuffix(".conf") for token in tokens if token.endswith(".conf")}
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as error:
        return SourceFacts((), tokens, frozenset(config_reads), InvalidPython(error.msg))
    imports: list[tuple[int, str, tuple[str, ...]]] = []
    conf_modules: set[str] = set()
    conf_functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((0, alias.name, ()) for alias in node.names)
            conf_modules.update(alias.asname or alias.name for alias in node.names
                                if alias.name in {"conf", "scripts.lib.conf"})
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.level, node.module or "", tuple(alias.name for alias in node.names)))
            if not node.level and node.module == "scripts.lib":
                conf_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "conf")
            elif not node.level and node.module == "scripts.lib.conf":
                conf_functions.update(alias.asname or alias.name for alias in node.names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        reader = isinstance(function, ast.Name) and function.id in conf_functions
        if isinstance(function, ast.Attribute):
            parts: list[str] = []
            module = function.value
            while isinstance(module, ast.Attribute):
                parts.append(module.attr)
                module = module.value
            if isinstance(module, ast.Name):
                reader = ".".join((module.id, *reversed(parts))) in conf_modules
        first = node.args[0]
        if reader and isinstance(first, ast.Constant) and isinstance(first.value, str):
            config_reads.add(Path(first.value).name.removesuffix(".conf"))
    return SourceFacts(tuple(imports), tokens, frozenset(config_reads))


def indexed_files(repo: Path) -> tuple[set[str], dict[str, str]]:
    tracked: set[str] = set()
    blobs: dict[str, str] = {}
    for entry in git_output(repo, "ls-files", "--stage", "-z").split("\0"):
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
    imports, tokens, config_reads = row.get("imports"), row.get("tokens"), row.get("config_reads")
    syntax_error = row.get("syntax_error")
    if (not isinstance(imports, list) or not isinstance(tokens, list)
            or not all(isinstance(token, str) for token in cast(list[object], tokens))
            or not isinstance(config_reads, list)
            or not all(isinstance(name, str) for name in cast(list[object], config_reads))
            or syntax_error is not None and not isinstance(syntax_error, str)):
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
    syntax = InvalidPython(syntax_error) if isinstance(syntax_error, str) else ValidPython()
    return SourceFacts(tuple(decoded), frozenset(cast(list[str], tokens)), frozenset(cast(list[str], config_reads)), syntax)


def read_fact_cache(path: Path) -> dict[str, SourceFacts]:
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
        result: dict[str, SourceFacts] = {}
        for blob, facts in cast(dict[str, object], blobs).items():
            result[blob] = decoded_facts(facts)
        return result
    except (OSError, ValueError):
        return {}


def write_fact_cache(path: Path, facts: dict[str, SourceFacts]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"format": CACHE_FORMAT_VERSION, "extractor": EXTRACTOR_VERSION,
                 "blobs": {blob: {"imports": entry.imports, "tokens": sorted(entry.tokens),
                                  "config_reads": sorted(entry.config_reads),
                                  "syntax_error": entry.syntax.message if isinstance(entry.syntax, InvalidPython) else None}
                           for blob, entry in facts.items()}}
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


def revision_files(repo: Path, revision: str) -> dict[str, str]:
    return {path: entry.object for path, entry in parse_tree_listing(git_output(repo, "ls-tree", "-rz", revision)).items()
            if entry.kind == "blob"}


def blob_texts(repo: Path, blobs: set[str]) -> dict[str, str]:
    try:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                                input=("\n".join(sorted(blobs)) + "\n").encode(), capture_output=True, check=False)
    except OSError as error:
        raise GraphError(str(error)) from error
    if result.returncode:
        raise GraphError(result.stderr.decode("utf-8", errors="replace").strip() or "git cat-file failed")
    replies = BytesIO(result.stdout)
    texts: dict[str, str] = {}
    for blob in sorted(blobs):
        reply = read_batch_blob(replies)
        if not isinstance(reply, GitBlob):
            raise GraphError(f"{blob}: invalid Git blob response")
        texts[blob] = reply.content.decode("utf-8")
    return texts


def dirty_paths(repo: Path) -> set[str]:
    return parse_status_z(git_output(repo, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"))


def worktree_files(repo: Path, dirty: set[str]) -> tuple[set[str], dict[str, str]]:
    indexed, blobs = indexed_files(repo)
    paths = {path for path in indexed | dirty if path.endswith(".py")}
    return {path for path in paths if (repo / path).is_file()}, blobs


def graph_for_files(repo: Path, paths: set[str], blobs: dict[str, str], dirty: set[str],
                    cached: dict[str, SourceFacts], *, worktree: bool
                    ) -> SourceGraph:
    bare_modules: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        name = Path(path).parent.name if Path(path).name == "__init__.py" else Path(path).stem
        bare_modules[name].add(path)
    sources = sorted(paths)
    missing = {blobs[path] for path in sources if path in blobs and blobs[path] not in cached}
    texts = blob_texts(repo, missing) if not worktree and missing else {}
    external = set(sys.stdlib_module_names) | set(packages_distributions())
    graph: dict[str, set[str]] = {}
    tokens: dict[str, frozenset[str]] = {}
    config_reads: dict[str, frozenset[str]] = {}
    for path in sources:
        blob = blobs.get(path) if path not in dirty else None
        facts = cached.get(blob) if blob is not None else None
        if facts is None:
            text = (repo / path).read_text(encoding="utf-8") if worktree else texts[blobs[path]]
            facts = extract_facts(text, path)
            if blob is not None:
                cached[blob] = facts
        if isinstance(facts.syntax, InvalidPython) and is_test_module(path):
            raise SyntaxError(f"{facts.syntax.message} ({path})")
        tokens[path] = facts.tokens
        config_reads[path] = facts.config_reads
        dependencies: set[str] = set()
        for level, module, names in facts.imports:
            if level:
                parents = Path(path).parent.parts
                module = ".".join((*parents[:len(parents) - level + 1], *module.split("."))).rstrip(".")
            for imported in (module, *(f"{module}.{name}" for name in names)):
                root = imported.split(".")[0]
                if root == "scripts" or root and root not in external:
                    dependencies.update(module_dependencies(imported, paths))
                    if root != "scripts":
                        dependencies.update(bare_modules.get(root, set()))
        dependencies.discard(path)
        graph[path] = dependencies
    return SourceGraph(graph, tokens, config_reads)


def latest_module_reads() -> dict[str, ModuleReadRecord]:
    """Read each field from its newest full, non-aborted gate."""
    from scripts.buildlog import index

    _ = index.update()
    with closing(index.read_only()) as connection:
        rows = cast(list[tuple[str, str, str | None, str | None]], connection.execute(
            "WITH latest AS (SELECT "
            + "(SELECT id FROM gate_runs WHERE selection = 'full' AND aborted = 0 "
            + "AND EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = gate_runs.id "
            + "AND recorded.config_reads IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS config_gate, "
            + "(SELECT id FROM gate_runs WHERE selection = 'full' AND aborted = 0 "
            + "AND EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = gate_runs.id "
            + "AND recorded.folder_scans IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS folder_gate) "
            + "SELECT suite, module, CASE WHEN run_id = config_gate THEN config_reads END, "
            + "CASE WHEN run_id = folder_gate THEN folder_scans END FROM gate_modules CROSS JOIN latest "
            + "WHERE run_id = config_gate OR run_id = folder_gate ORDER BY suite, module, attempt"
        ).fetchall())
    recorded: dict[str, ModuleReadRecord] = {}
    for suite, module, config_json, folder_json in rows:
        path = f"scripts/{suite}/{module}"
        reads = module_read_record(json.loads(config_json) if config_json is not None else None,
                                   json.loads(folder_json) if folder_json is not None else None)
        previous = recorded.get(path, ModuleReadRecord(NoConfigReadRecord(), NoFolderScanRecord()))
        configs = previous.config_reads
        if isinstance(reads.config_reads, RecordedConfigReads):
            stems = reads.config_reads.stems
            if isinstance(configs, RecordedConfigReads):
                stems |= configs.stems
            configs = RecordedConfigReads(stems)
        folders = previous.folder_scans
        if isinstance(reads.folder_scans, RecordedFolderScans):
            scans = reads.folder_scans.folders
            if isinstance(folders, RecordedFolderScans):
                scans |= folders.folders
            folders = RecordedFolderScans(scans)
        recorded[path] = ModuleReadRecord(configs, folders)
    return recorded


def changed_path_parents(repo: Path, changed: set[str], base_paths: set[str]) -> set[str]:
    """Include parents only for added and removed paths."""
    directories = {"."}
    for path in base_paths:
        directories.update(parent.as_posix() for parent in Path(path).parents)
    parents: set[str] = set()
    for path in changed:
        if path in base_paths and os.path.lexists(repo / path):
            continue
        parent = Path(path).parent
        while parent.as_posix() not in directories:
            parent = parent.parent
        parents.add(parent.as_posix())
        parent = Path(path).parent
        while parent != Path(".") and not (repo / parent).is_dir():
            parent = parent.parent
        parents.add(parent.as_posix())
    return parents


def scans_changed_paths(scans: RecordedFolderScans, changed: set[str], parents: set[str]) -> bool:
    for folder in scans.folders:
        if folder == "/" or folder in parents:
            return True
        if folder.endswith("/") and any(path == folder[:-1] or path.startswith(folder) for path in changed):
            return True
    return False


def retain_facts(cache_path: Path | None, cached: dict[str, SourceFacts],
                 before: set[str], blobs: set[str]) -> None:
    current = {blob: facts for blob, facts in cached.items() if blob in blobs}
    if cache_path is not None and current.keys() != before:
        write_fact_cache(cache_path, current)


def source_blobs(paths: set[str], blobs: dict[str, str]) -> set[str]:
    return {blob for path, blob in blobs.items() if path in paths}


def dependency_graph(repo: Path, *, state_dir: Path | None = None
                     ) -> tuple[dict[str, set[str]], dict[str, frozenset[str]]]:
    dirty = dirty_paths(repo)
    paths, blobs = worktree_files(repo, dirty)
    cache_path = state_dir / "affected-module-facts.json" if state_dir is not None else None
    cached = read_fact_cache(cache_path) if cache_path is not None else {}
    before = set(cached)
    result = graph_for_files(repo, paths, blobs, dirty, cached, worktree=True)
    retain_facts(cache_path, cached, before, source_blobs(paths | dirty, blobs))
    return result.imports, result.tokens


def folder_names_of(paths: set[str]) -> dict[str, set[str]]:
    folder_names: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        parent = str(Path(path).parent)
        if parent != ".":
            folder_names[parent].add(Path(path).name)
    return folder_names


def names_path(tokens: frozenset[str], source: str, paths: set[str],
               folder_names: dict[str, set[str]]) -> bool:
    """Match a complete path, or a file name with its folder."""
    if tokens & paths:
        return True
    parent = str(Path(source).parent)
    return any(tokens & names and (parent == folder or Path(folder).name in tokens)
               for folder, names in folder_names.items())


def names_changed_file(graph: SourceGraph, path: str, changed: set[str],
                       folder_names: dict[str, set[str]]) -> bool:
    """Match file tokens in the test and its imported test-support modules."""
    pending = [path]
    seen: set[str] = set()
    while pending:
        source = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        if names_path(graph.tokens.get(source, frozenset()), source, changed, folder_names):
            return True
        pending.extend(dependency for dependency in graph.imports.get(source, set())
                       if dependency.endswith("_test_support.py"))
    return False


def is_script(repo: Path, path: str) -> bool:
    """A non-Python file under scripts/ that ends in .sh or has an execute bit."""
    return (path.startswith("scripts/") and not path.endswith(".py")
            and (path.endswith(".sh") or os.access(repo / path, os.X_OK)))


def calls_script(tokens: frozenset[str], caller: str, targets: set[str],
                 folder_names: dict[str, set[str]]) -> bool:
    """Shell calls also prefix a full path or a sibling name with a variable."""
    if names_path(tokens, caller, targets, folder_names):
        return True
    siblings = folder_names.get(str(Path(caller).parent), set())
    names = {Path(target).name for target in targets}
    for token in tokens:
        _, slash, name = token.rpartition("/")
        if slash and name in names and (name in siblings
                                        or any(token.endswith(f"/{target}") for target in targets)):
            return True
    return False


def script_callers(repo: Path, candidates: set[str], changed: set[str]) -> set[str]:
    """Return the changed scripts and every script that calls one, directly or through others."""
    found = {path for path in changed if is_script(repo, path)}
    if not found:
        return found
    scripts = {path: path_tokens((repo / path).read_text(encoding="utf-8", errors="replace"))
               for path in candidates if is_script(repo, path) and (repo / path).is_file()}
    pending = set(found)
    while pending:
        folder_names = folder_names_of(pending)
        pending = {caller for caller, tokens in scripts.items()
                   if caller not in found and calls_script(tokens, caller, pending, folder_names)}
        found |= pending
    return found


def select_affected(repo: Path, base: CertifiedBase | NoCertifiedBase,
                    modules: Sequence[Path], dirty: set[str], *,
                    state_dir: Path | None = None) -> AffectedSelection | FullSuite:
    try:
        candidates = {base.commit: base} if isinstance(base, CertifiedBase) else {}
        for branch in conf.read("merge_gate").words("landing_branches"):
            ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD",
                                       f"refs/remotes/origin/{branch}"],
                                      capture_output=True, text=True, check=False)
            if ancestor.returncode:
                continue
            commit = ancestor.stdout.strip()
            if commit not in candidates:
                at = git_output(repo, "show", "-s", "--format=%cI", commit).strip()
                candidates[commit] = CertifiedBase(commit, at)
        if not candidates:
            return FullSuite("no certified base")
        differences: list[tuple[CertifiedBase, set[str]]] = []
        for candidate in candidates.values():
            changes = git_output(repo, "diff", "--name-only", "-z", "--no-renames", candidate.commit, "HEAD")
            differences.append((candidate, set(changes.split("\0")) - {""}))
        base, committed_changes = min(differences, key=lambda item: len(item[1]))
        changed = committed_changes | dirty
        paths, blobs = worktree_files(repo, dirty)
        cache_path = state_dir / "affected-module-facts.json" if state_dir is not None else None
        cached = read_fact_cache(cache_path) if cache_path is not None else {}
        before = set(cached)
        graph = graph_for_files(repo, paths, blobs, dirty, cached, worktree=True)
        configs = {Path(path).name.removesuffix(".conf") for path in changed
                   if path.startswith("config/") and path.endswith(".conf")}
        recorded = latest_module_reads() if changed else {}
        has_folder_records = any(isinstance(reads.folder_scans, RecordedFolderScans) for reads in recorded.values())
        base_blobs = revision_files(repo, base.commit) if has_folder_records else {}
        scanned_parents: set[str] = changed_path_parents(repo, changed, set(base_blobs)) if has_folder_records else set()
        retained = source_blobs(paths | dirty, blobs)
        python = {path for path in changed if path.endswith(".py")}
        removed = {path for path in python if not (repo / path).is_file()}
        old_graph: dict[str, set[str]] = {}
        if removed:
            old_blobs = base_blobs if has_folder_records else revision_files(repo, base.commit)
            old_paths = {path for path in old_blobs if path.endswith(".py")}
            old_graph = graph_for_files(repo, old_paths, old_blobs, set(), cached, worktree=False).imports
            retained.update(source_blobs(old_paths, old_blobs))
        retain_facts(cache_path, cached, before, retained)
        folder_stems: dict[str, set[str]] = defaultdict(set)
        for source in python:
            if not is_test_module(source):
                folder_stems[str(Path(source).parent)].add(Path(source).stem)
        named = changed | script_callers(repo, set(blobs) | dirty, changed)
        folder_names = folder_names_of(named)
        selected: list[Path] = []
        for module in modules:
            path = module.relative_to(repo).as_posix()
            reads = recorded.get(path, ModuleReadRecord(NoConfigReadRecord(), NoFolderScanRecord()))
            config_reads = reads.config_reads
            config_changed = bool(configs) and (isinstance(config_reads, NoConfigReadRecord)
                or bool(config_reads.stems & configs) or bool(graph.config_reads.get(path, frozenset()) & configs))
            folder_changed = has_folder_records and (isinstance(reads.folder_scans, NoFolderScanRecord)
                or scans_changed_paths(reads.folder_scans, changed, scanned_parents))
            if (path in changed or graph.imports.get(path, set()) & python
                    or old_graph.get(path, set()) & removed or names_changed_file(graph, path, named, folder_names)
                    or config_changed or folder_changed):
                selected.append(module)
                continue
            stems = folder_stems.get(str(Path(path).parent), set())
            if stems:
                text = module.read_text(encoding="utf-8")
                if any(re.search(rf"\b{re.escape(stem)}\b", text) for stem in stems):
                    selected.append(module)
        return AffectedSelection(base, tuple(sorted(changed)), tuple(selected))
    except (GraphError, OSError, ValueError, SyntaxError, sqlite3.Error) as error:
        return FullSuite(f"graph error: {error}")
