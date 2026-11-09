"""Select tests from direct Python imports and transitive configuration reads.

No source is imported or evaluated. Callers own certification and execution.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from io import BytesIO
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import cast

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


def extract_facts(text: str, path: str) -> SourceFacts:
    tokens = frozenset(match.group().removeprefix("./") for match in PATH_TOKEN.finditer(text))
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
    return parse_status_z(git_output(repo, "status", "--porcelain", "-z", "--untracked-files=all"))


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


def transitive_config_reads(graph: SourceGraph) -> dict[str, set[str]]:
    importers: dict[str, set[str]] = defaultdict(set)
    for path, dependencies in graph.imports.items():
        for dependency in dependencies:
            importers[dependency].add(path)
    reads = {path: set(names) for path, names in graph.config_reads.items()}
    pending = [path for path, names in reads.items() if names]
    while pending:
        source = pending.pop()
        for importer in importers[source]:
            added = reads[source] - reads[importer]
            if added:
                reads[importer].update(added)
                pending.append(importer)
    return reads


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


def select_affected(repo: Path, base: CertifiedBase | NoCertifiedBase,
                    modules: Sequence[Path], dirty: set[str], *,
                    state_dir: Path | None = None) -> AffectedSelection | FullSuite:
    try:
        if isinstance(base, NoCertifiedBase):
            return FullSuite("no certified base")
        changes = git_output(repo, "diff", "--name-only", "-z", "--no-renames", base.commit, "HEAD")
        changed = (set(changes.split("\0")) - {""}) | dirty
        paths, blobs = worktree_files(repo, dirty)
        cache_path = state_dir / "affected-module-facts.json" if state_dir is not None else None
        cached = read_fact_cache(cache_path) if cache_path is not None else {}
        before = set(cached)
        graph = graph_for_files(repo, paths, blobs, dirty, cached, worktree=True)
        config_reads = transitive_config_reads(graph)
        configs = {Path(path).name.removesuffix(".conf") for path in changed
                   if path.startswith("config/") and path.endswith(".conf")}
        retained = source_blobs(paths | dirty, blobs)
        python = {path for path in changed if path.endswith(".py")}
        removed = {path for path in python if not (repo / path).is_file()}
        old_graph: dict[str, set[str]] = {}
        if removed:
            old_blobs = revision_files(repo, base.commit)
            old_paths = {path for path in old_blobs if path.endswith(".py")}
            old_graph = graph_for_files(repo, old_paths, old_blobs, set(), cached, worktree=False).imports
            retained.update(source_blobs(old_paths, old_blobs))
        retain_facts(cache_path, cached, before, retained)
        folders = {str(Path(path).parent) for path in python if not is_test_module(path)}
        selected: list[Path] = []
        for module in modules:
            path = module.relative_to(repo).as_posix()
            if (path in changed or graph.imports.get(path, set()) & python
                    or old_graph.get(path, set()) & removed or graph.tokens.get(path, frozenset()) & changed
                    or config_reads.get(path, set()) & configs
                    or str(Path(path).parent) in folders):
                selected.append(module)
        return AffectedSelection(base, tuple(sorted(changed)), tuple(selected))
    except (GraphError, OSError, ValueError, SyntaxError) as error:
        return FullSuite(f"graph error: {error}")
