#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Identified serial crate batches, entered through bulk_run --crate.

Canonical Git and the ledger own claims and publication. Private checkouts own edits and
validation. The existing workspace validation and exact-tree test proof are unchanged.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import hashlib
import json
import posixpath
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast

from scripts.style import bulk_rename as bulk
from scripts.style import bulk_run as run
from scripts.style import coverage_report
from scripts.lib import copy_record, scratch
from scripts.style import rules
from scripts.style import style_ledger as ledger_api
from scripts.style import style_proposals
from scripts.style import style_footprint
from scripts.style import style_sync
from scripts.copies import copies
from scripts.style.style_ledger import _path_attributes  # pyright: ignore[reportPrivateUsage]


class CrateStage(StrEnum):
    SELECTED = "selected"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    WAITING_FOR_RESERVATIONS = "waiting for reservations"
    EDITED = "edited"
    COMMITTED = "committed"
    TESTED = "tested"
    IMPORTED = "imported"
    CHECKPOINT_PUBLISHED = "checkpoint published"
    INTEGRATED = "integrated"
    ABANDONED = "abandoned"


class IntegrationStage(StrEnum):
    PREPARED = "prepared"
    TESTED = "tested"


class IntegrationEvidence(TypedDict):
    commit: str
    destination_tip: str
    stage: str


class FrozenFinding(TypedDict):
    id: str
    path: str
    proposal: str
    rule_blob: str
    evidence: str
    proposal_sha256: str


class CommitEvidence(TypedDict):
    commit: str
    tree: str
    paths: list[str]
    findings: list[str]
    refusals: dict[str, str]


class FrozenMachineJudgment(TypedDict):
    finding_id: str
    commit: str
    path: str
    line: int
    finding_identity: str
    rule_blob: str
    inputs: dict[str, str]


def freeze_machine_judgment(judgment: run.FreshMachineJudgment) -> FrozenMachineJudgment:
    return {"finding_id": judgment.finding_id, "commit": judgment.anchor.commit,
            "path": judgment.anchor.path, "line": judgment.anchor.line,
            "finding_identity": judgment.finding_identity, "rule_blob": judgment.rule_blob,
            "inputs": dict(judgment.inputs)}


def thaw_machine_judgment(judgment: FrozenMachineJudgment) -> run.FreshMachineJudgment:
    return run.FreshMachineJudgment(judgment["finding_id"],
                                    ledger_api.FindingAnchor(judgment["commit"], judgment["path"], judgment["line"]),
                                    judgment["finding_identity"], judgment["rule_blob"],
                                    tuple(sorted(judgment["inputs"].items())))


class CrateRecord(TypedDict):
    directory: str
    checkout: str
    branch: str
    intent: str
    result_path: str
    selected: list[FrozenFinding]
    finding_holders: NotRequired[dict[str, list[str]]]
    machine_judgments: NotRequired[list[FrozenMachineJudgment]]
    judgment_planned_findings: NotRequired[list[str]]
    pending: NotRequired[list[bulk.PendingReservation]]
    terminal_refusals: NotRequired[dict[str, str]]
    stage: str
    owner: NotRequired[str]
    tickets: NotRequired[dict[str, str]]
    error: NotRequired[str]
    integration: NotRequired[IntegrationEvidence]


class Manifest(TypedDict):
    version: int
    run: str
    canonical: str
    database: str
    base: str
    base_tree: str
    rule: str
    rule_blob: str
    rule_text: str
    destination: str
    expected_tip: str
    integration_ref: str
    crates: dict[str, CrateRecord]
    refusals: list[dict[str, str]]


def evidence(finding: ledger_api.HeldFinding) -> str:
    """Mutable ownership is excluded; a resumed run cannot adopt a rejudged declaration."""
    fields = (finding.path, finding.line, finding.text, finding.kind, finding.before,
              finding.after, finding.rule, finding.rule_blob, asdict(finding.follow))
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


@contextmanager
def serial(folder: Path) -> Generator[None]:
    """One run lock also serializes different crates and publication retries."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def changed(repo: Path, base: str, tip: str) -> tuple[str, ...]:
    # --no-renames keeps both sides of moves, deletions, creations and mode-only changes.
    return tuple(filter(None, run.git(repo, "diff", "--no-renames", "--name-only", "-z",
                                      base, tip).split("\0")))


@dataclass(frozen=True)
class LoaderImportEvidence:
    """The import owner's frozen bindings and scopes for one source document."""
    words: tuple[str, ...]
    context: style_footprint.ImportContext
    bindings: dict[tuple[tuple[str, ...], str], list[tuple[tuple[str, ...], int]]]

    def module(self, site: int) -> tuple[str, ...]:
        return (tuple(name.split("@")[0] for name in self.context.scopes[site].module)
                if site >= 0 else ())


@dataclass(frozen=True)
class UnprovedLoaderAnchors:
    """An invocation's enclosing module instances cannot all be rooted safely."""
    reason: str


LoaderAnchors = tuple[tuple[str, tuple[str, ...]], ...] | UnprovedLoaderAnchors


class SourceLoaderImports:
    """Resolve invoked builtin loaders through frozen imports and proven module routes.

    BindingText owns tokens and macro boundaries, style_footprint owns import evidence,
    and bulk_rename owns file-module routes. Glob and named bindings share one resolver.
    """

    def __init__(self, sources: dict[str, bulk.BindingText],
                 external: Callable[[str, tuple[str, ...]], tuple[tuple[str, tuple[str, ...]], ...]],
                 anchored: Callable[[str, tuple[str, ...], tuple[str, ...]], LoaderAnchors]
                 ) -> None:
        self.sources: dict[str, bulk.BindingText] = sources
        self.external: Callable[[str, tuple[str, ...]],
                                tuple[tuple[str, tuple[str, ...]], ...]] = external
        self.anchored: Callable[[str, tuple[str, ...], tuple[str, ...]], LoaderAnchors] = anchored
        self.documents: dict[str, LoaderImportEvidence] = {}
        self.unproved: dict[str, set[str]] = defaultdict(set)
        self.anchor_failures: set[str] = set()

    def document(self, path: str) -> LoaderImportEvidence:
        if path not in self.documents:
            words = tuple(word.removeprefix("r#") for word in self.sources[path].words)
            trees = style_footprint.use_trees(words, style_footprint.code_item_starts(words))
            context = style_footprint.import_context(words)
            imports: dict[tuple[tuple[str, ...], str], list[tuple[tuple[str, ...], int]]]
            imports = defaultdict(list)
            evidence = LoaderImportEvidence(words, context, imports)
            for index, prefix in trees.prefixes.items():
                if words[index + 1:index + 2] == ("::",):
                    continue
                target = (*prefix, words[index])
                if words[index] == "self" and prefix:
                    target = prefix
                bound = words[index + 2] if words[index + 1:index + 2] == ("as",) else target[-1]
                keyword = trees.keywords[index]
                imports[evidence.module(keyword), bound].append((target, keyword))
            self.documents[path] = evidence
        return self.documents[path]

    def bound_loaders(self, path: str, owner: tuple[str, ...], name: str,
                      suffix: tuple[str, ...], site: int,
                      seen: frozenset[tuple[str, int, str]]) -> set[str]:
        document = self.document(path)
        found: set[str] = set()
        for binding in (name, "*"):
            for target, keyword in document.bindings.get((owner, binding), ()):
                block = document.context.scopes[keyword].block
                identity = (path, keyword, name)
                if (identity in seen
                        or (block >= 0 and block not in document.context.module_blocks
                            and not block < site < document.context.block_ends[block])):
                    continue
                parts = (*target[:-1], name, *suffix) if binding == "*" else (*target, *suffix)
                found.update(self.resolve(path, parts, keyword, seen | {identity}))
        return found

    def qualified_loaders(self, path: str, parts: tuple[str, ...], site: int,
                          seen: frozenset[tuple[str, int, str]]) -> set[str]:
        found: set[str] = set()
        for index, name in enumerate(parts):
            found.update(self.bound_loaders(
                path, parts[:index], name, parts[index + 1:], site, seen))
        for child, suffix in self.external(path, parts):
            # The file/route identity breaks mutually reexporting module cycles.
            identity = (child, -1, "::".join(suffix))
            if identity not in seen:
                found.update(self.resolve(child, suffix, -1, seen | {identity}))
        return found

    def resolve(self, path: str, parts: tuple[str, ...], site: int,
                seen: frozenset[tuple[str, int, str]]) -> set[str]:
        if not parts:
            return set()
        head, last = parts[0], parts[-1]
        loaders = {"include", "include_str", "include_bytes"}
        if len(parts) == 2 and head in {"std", "core"} and last in loaders:
            return {last}
        document = self.document(path)
        owner = document.module(site)
        found = {head} if len(parts) == 1 and head in loaders else set[str]()
        if head in style_footprint.PATH_ANCHORS:
            anchors = self.anchored(path, parts, owner)
            if isinstance(anchors, UnprovedLoaderAnchors):
                # It could be any builtin loader. Preserve the literal loading closure
                # until its module identity can be proved, rather than admitting it.
                self.anchor_failures.add(anchors.reason)
                return loaders
            for root, target in anchors:
                identity = (root, -1, "::".join(target))
                if identity not in seen:
                    found.update(self.qualified_loaders(root, target, -1, seen | {identity}))
            return found
        for depth in range(len(owner), -1, -1):
            found.update(self.qualified_loaders(path, (*owner[:depth], *parts), site, seen))
        return found

    def invoked(self, path: str) -> tuple[tuple[int, int, frozenset[str]], ...]:
        document = self.document(path)
        words = document.words
        result: list[tuple[int, int, frozenset[str]]] = []
        for bang, stop in self.sources[path].macros:
            # Real invocations only: fields, methods, imports, strings and comments are data.
            start = bang - 1
            while (start >= 2 and words[start - 1] == "::"
                   and style_footprint.IDENTIFIER.fullmatch(words[start - 2])):
                start -= 2
            if words[max(0, start - 1):start] == (".",):
                continue
            self.anchor_failures.clear()
            names = self.resolve(path, words[start:bang:2], bang, frozenset())
            if self.anchor_failures:
                self.unproved[path].update(self.anchor_failures)
            if names:
                result.append((bang, stop, frozenset(names)))
        return tuple(result)


class SourceOwnership:
    """Frozen package/target discovery plus conservative Rust source-loading evidence.

    The ledger owns path-attribute interpretation, including cfg and inline-module ambiguity.
    BindingText owns tokens and macro boundaries. Literal include loaders add reading edges;
    unreadable loaders make ownership unproved, rather than guessing from a directory.
    """

    def __init__(self, repo: Path, commit: str, members: tuple[rules.WorkspaceMember, ...]) -> None:
        self.directories: tuple[str, ...] = tuple(member.directory for member in members)
        self.owners: dict[str, set[str]] = {}
        self.sources: dict[str, bulk.BindingText] = {}
        self.uncertain: dict[str, list[str]] = defaultdict(list)
        self.inline_folders: dict[str, set[str]] = defaultdict(set)
        inventory = ledger_api.tree_entries(repo, commit)
        blobs = {path: entry.object for path, entry in inventory.items()
                 if (path.endswith(".rs") or Path(path).name == "Cargo.toml") and entry.kind == "blob"
                 and entry.mode in ledger_api.REGULAR_FILE_MODES}
        edges: dict[str, set[str]] = defaultdict(set)
        inline_contexts: set[tuple[str, bulk.ModuleLoadingDirectory]] = set()
        for path, entry in inventory.items():
            if path.endswith(".rs"):
                self.owners[path] = {self.home(path)}
                if entry.kind != "blob" or entry.mode not in ledger_api.REGULAR_FILE_MODES:
                    self.uncertain[path].append("source is not a regular file")
        manifests: dict[str, str] = {}
        for path, content in ledger_api.blob_contents(repo, blobs):
            if Path(path).name == "Cargo.toml":
                manifests[path] = content.decode()
                continue
            try:
                source = content.decode()
                parsed = bulk.BindingText(source)
            except (bulk.BulkRenameError, UnicodeError) as error:
                self.uncertain[path].append(str(error))
                continue
            self.sources[path] = parsed
            for mount in _path_attributes(path, source.encode()):
                if isinstance(mount, ledger_api.UnreadPathAttribute):
                    self.uncertain[path].append(mount.reason)
                elif isinstance(mount, ledger_api.MountedFolder):
                    inline_contexts.add((path, bulk.OwnedModuleDirectory(Path(path).parent)))
                    self.inline_folders[path].update(mount.readings)
                else:
                    # The ledger keeps alternative lexical readings. Only existing sources
                    # can be loaded; a missing alternative is not another ambiguous loader.
                    readings = set(mount.readings) & self.owners.keys()
                    edges[path].update(readings)
                    if not readings:
                        self.uncertain[path].append(
                            "missing or external source " + ", ".join(mount.readings))
        self.module_contexts: set[tuple[str, bulk.ModuleLoadingDirectory]] = (
            inline_contexts | self.mount_contexts(edges))
        self.route_evidence: dict[tuple[str, bulk.ModuleLoadingDirectory],
                                  tuple[bulk.ExternalModuleSource | bulk.InlineModuleSource
                                        | bulk.UnprovedModuleSource, ...]] = {}
        self.module_parents: dict[str, set[tuple[str, tuple[str, ...]]]] = defaultdict(set)
        self.crate_roots: set[str] = set()
        self.target_roots(manifests, frozenset(Path(path) for path in inventory), members)
        self.edges: dict[str, frozenset[str]] = {
            source: frozenset(targets & self.owners.keys()) for source, targets in edges.items()}
        # Prime the existing route graph with each document's known loading contexts.
        # Its normal propagation owns traversal; the reverse index preserves instances.
        for path in self.sources:
            for loading in self.loading_contexts(path):
                for route in self.routes(path, loading):
                    if isinstance(route, bulk.ExternalModuleSource):
                        child = posixpath.normpath(str(route.path))
                        if child in self.sources:
                            self.module_contexts.add((child, route.loading))
        self.propagate()
        imports = SourceLoaderImports(self.sources, self.external_modules, self.anchored_modules)
        for path, parsed in self.sources.items():
            for bang, stop, names in imports.invoked(path):
                word = "/".join(sorted(names))
                arguments = parsed.words[bang + 2:stop - 1]
                if arguments[-1:] == (",",):
                    arguments = arguments[:-1]
                literal = ledger_api.PLAIN_STRING.fullmatch(arguments[0]) if len(arguments) == 1 else None
                if literal is None:
                    self.uncertain[path].append(f"nonliteral {word} source loader")
                elif literal[1].endswith(".rs"):
                    edges[path].add(posixpath.normpath(str(Path(path).parent / literal[1])))
                elif "include" in names:
                    self.uncertain[path].append("include! code outside the Rust source inventory")
        for source, targets in edges.items():
            for target in targets:
                if target not in self.owners:
                    self.uncertain[source].append(f"missing or external source {target}")
        for source, targets in edges.items():
            self.edges[source] = (self.edges.get(source, frozenset())
                                  | frozenset(targets & self.owners.keys()))
        # Literal loaders choose their own directory; proven module routes already
        # carry theirs. Do not reinterpret a module edge as an include/path mount.
        self.module_contexts.update(self.mount_contexts(edges))
        self.propagate()
        # Unknown anchored instances may invoke a loader from any of their imports.
        # Protect its possible literal target and descendants, not only the importer.
        self.protect_loading_closures(imports.unproved)

    def protect_loading_closures(self, potential: dict[str, set[str]]) -> None:
        """Carry uncertainty over proven loading edges through the existing closure owner."""
        for path, reasons in potential.items():
            for target in self.reachable((path,)):
                for reason in sorted(reasons):
                    if reason not in self.uncertain[target]:
                        self.uncertain[target].append(reason)

    def mount_contexts(self, edges: dict[str, set[str]]
                       ) -> set[tuple[str, bulk.ModuleLoadingDirectory]]:
        return {(target, bulk.OwnedModuleDirectory(Path(target).parent))
                for source, targets in edges.items() for target in targets
                if target in self.sources and self.home(source) != self.home(target)}

    def routes(self, path: str, loading: bulk.ModuleLoadingDirectory
               ) -> tuple[bulk.ExternalModuleSource | bulk.InlineModuleSource
                          | bulk.UnprovedModuleSource, ...]:
        key = (path, loading)
        if key not in self.route_evidence:
            parsed = self.sources[path]
            explicit = bulk.module_file_path_evidence(
                Path(path), parsed, bulk.ModulePathUse.INCLUDING_INLINE)
            self.route_evidence[key] = bulk.module_source_routes(
                Path(path), parsed, loading, explicit.paths,
                lambda child: posixpath.normpath(str(child)) in self.owners, explicit.failures)
            for route in self.route_evidence[key]:
                if isinstance(route, bulk.ExternalModuleSource):
                    child = posixpath.normpath(str(route.path))
                    if child in self.sources:
                        self.module_parents[child].add((path, route.ancestry))
        return self.route_evidence[key]

    @staticmethod
    def native_loading(path: str) -> bulk.ModuleLoadingDirectory:
        source = Path(path)
        if source.name in {"lib.rs", "main.rs", "mod.rs"}:
            return bulk.OwnedModuleDirectory(source.parent)
        return bulk.FlatModuleDirectory(source.parent, source.stem)

    def loading_contexts(self, path: str) -> set[bulk.ModuleLoadingDirectory]:
        contexts = {loading for source, loading in self.module_contexts if source == path}
        return contexts or {self.native_loading(path)}

    def anchored_modules(self, path: str, parts: tuple[str, ...], inline: tuple[str, ...]
                         ) -> LoaderAnchors:
        """Resolve anchors in every frozen instance, rather than treating each file as a crate.

        Cargo targets establish roots; bulk_rename owns enclosing module routes. Reused
        documents retain every ancestry. Includes without a proved enclosing instance
        stay uncertain, regardless of their filename; cycles cannot manufacture a root.
        """
        pending = [(path, inline, frozenset[str]())]
        found: set[tuple[str, tuple[str, ...]]] = set()
        unproved = False
        while pending:
            source, ancestry, seen = pending.pop()
            if source in seen:
                unproved = True
                continue
            parents = self.module_parents.get(source, set())
            if parents:
                pending.extend((parent, (*prefix, *ancestry), seen | {source})
                               for parent, prefix in parents)
            if source in self.crate_roots:
                target = style_footprint.resolved_module(parts, ancestry)
                if not isinstance(target, style_footprint.UnresolvedPath) and target:
                    found.add((source, target))
                else:
                    unproved = True
            elif not parents:
                unproved = True
        if unproved:
            return UnprovedLoaderAnchors("unproved anchored loader module instance in " + path)
        return tuple(sorted(found))

    def external_modules(self, path: str, parts: tuple[str, ...]
                         ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Look up file-module prefixes using the single module-route owner."""
        found: set[tuple[str, tuple[str, ...]]] = set()
        for loading in self.loading_contexts(path):
            for route in self.routes(path, loading):
                match route:
                    case bulk.ExternalModuleSource():
                        child = posixpath.normpath(str(route.path))
                        if (parts[:len(route.ancestry)] == route.ancestry
                                and child in self.sources):
                            found.add((child, parts[len(route.ancestry):]))
                    case bulk.UnprovedModuleSource():
                        name = self.sources[path].words[route.declaration + 1]
                        if name in parts[:-1] and route.reason not in self.uncertain[path]:
                            self.uncertain[path].append(route.reason)
                    case bulk.InlineModuleSource():
                        pass  # Its bindings are already in this document's import evidence.
        return tuple(sorted(found))

    def propagate(self) -> None:
        """Share only mounted sources and their transitive module descendants, never a package.

        bulk_rename owns lexical child-module resolution, including explicit paths and inline
        modules. An unproved route makes only its declaring file uncertain; it does not guess
        at other sources in the package. An unproved inline mount also protects the sources
        in its literal loading folders and their loading closure, including paths outside
        those folders. All lookups use this frozen inventory.
        """
        pending = list(self.module_contexts)
        known_paths = {path for path, _ in self.module_contexts}
        visited: set[tuple[str, bulk.ModuleLoadingDirectory]] = set()
        potential: dict[str, set[str]] = defaultdict(set)
        while pending:
            path, loading = pending.pop()
            if (path, loading) in visited or path not in self.sources:
                continue
            visited.add((path, loading))
            parsed = self.sources[path]
            for target in self.edges.get(path, ()):
                if target not in known_paths:
                    context = (target, bulk.OwnedModuleDirectory(Path(target).parent))
                    self.module_contexts.add(context)
                    known_paths.add(target)
                    pending.append(context)
            routes = self.routes(path, loading)
            for route in routes:
                match route:
                    case bulk.UnprovedModuleSource():
                        if route.reason not in self.uncertain[path]:
                            self.uncertain[path].append(route.reason)
                        if parsed.words[route.declaration + 2:route.declaration + 3] == ("{",):
                            reason = f"unproved inline mount in {path}: {route.reason}"
                            for target in self.owners:
                                if any(Path(target).is_relative_to(folder)
                                       for folder in self.inline_folders.get(path, ())):
                                    if reason not in self.uncertain[target]:
                                        self.uncertain[target].append(reason)
                                    potential[target].add(reason)
                                    context = (target, self.native_loading(target))
                                    self.module_contexts.add(context)
                                    pending.append(context)
                    case bulk.ExternalModuleSource():
                        child = posixpath.normpath(str(route.path))
                        if child not in self.owners:
                            reason = f"missing or external source {child}"
                            if reason not in self.uncertain[path]:
                                self.uncertain[path].append(reason)
                            continue
                        self.edges[path] = self.edges.get(path, frozenset()) | {child}
                        known_paths.add(child)
                        self.module_contexts.add((child, route.loading))
                        pending.append((child, route.loading))
                    case bulk.InlineModuleSource():
                        pass  # Its out-of-line children are already resolved by the module owner.
        # Carry only conditional-mount uncertainty forward; ordinary per-file failures
        # must not erase proven sibling routes or taint unrelated sources.
        self.protect_loading_closures(potential)
        changed = True
        while changed:
            changed = False
            for source, targets in self.edges.items():
                for target in targets:
                    owners = self.owners[target]
                    previous = len(owners)
                    owners.update(self.owners[source])
                    changed |= len(owners) != previous

    def target_roots(self, manifests: dict[str, str], paths: frozenset[Path],
                     members: tuple[rules.WorkspaceMember, ...]) -> None:
        """Establish root identity from the same frozen inventory as module/import evidence."""
        for member in members:
            crate = Path(member.directory)
            try:
                targets = bulk.cargo_target_roots(
                    crate, frozen=bulk.FrozenCargoTargetInventory(
                        manifests[(crate / "Cargo.toml").as_posix()], paths))
            except bulk.BulkRenameError as error:
                # An unreadable target can share any source in its package. Keep the
                # other packages usable, but do not guess roots for this one.
                for path in self.owners:
                    if self.home(path) == member.directory:
                        self.uncertain[path].append(str(error))
                continue
            for target in targets:
                path = posixpath.normpath(str(target.path))
                if Path(path).is_absolute() or ".." in Path(path).parts:
                    self.uncertain[path].append("target outside checkout")
                    continue
                self.crate_roots.add(path)
                if path not in self.owners:
                    self.uncertain[path].append("target absent from frozen sources")
                else:
                    self.owners[path].add(member.directory)
                    self.module_contexts.add((path, bulk.OwnedModuleDirectory(Path(path).parent)))

    def home(self, path: str) -> str:
        return style_proposals.crate_for(self.directories, path)

    def reachable(self, paths: Sequence[str]) -> frozenset[str]:
        """Only loader edges from these sources establish reachability, never reverse edges."""
        return frozenset(run.dependent_files(self.edges, set(paths)))

    def require(self, directory: str, paths: tuple[str, ...]) -> None:
        for path in paths:
            for source in sorted(self.reachable((path,))):
                if source in self.uncertain:
                    raise bulk.BulkRenameError("ambiguous shared-source ownership: " + source
                                               + ": " + "; ".join(self.uncertain[source]))
            # An edited loader participates in the shared-source group even when its
            # cross-package target itself is unchanged in the final diff.
            if (Path(path).is_absolute() or ".." in Path(path).parts
                    or self.home(path) != directory
                    or self.owners.get(path, {self.home(path)}) != {directory}
                    or any(self.owners[target] != {directory}
                           for target in self.edges.get(path, ()))):
                raise bulk.BulkRenameError("shared or cross-crate source refused: " + path)
            if not path.endswith(".rs"):
                raise bulk.BulkRenameError("crate run edits only Rust source: " + path)


class Planner(Protocol):
    def __call__(self, checkout: Path, ledger: ledger_api.Ledger, base: str,
                 selected: run.Selection, availability: run.PlanningFileAvailability,
                 definition: rules.Rule, /) -> run.PlannedEdits: ...


def plan(checkout: Path, ledger: ledger_api.Ledger, base: str, selected: run.Selection,
         availability: run.PlanningFileAvailability, definition: rules.Rule) -> run.PlannedEdits:
    return run.plan_rule(checkout, ledger, base, selected, definition,
                         run.RenameService.ON_DEMAND, availability.available)


def admission_plan(checkout: Path, ledger: ledger_api.Ledger, base: str,
                   selected: run.Selection, availability: run.PlanningFileAvailability,
                   definition: rules.Rule, planner: Planner,
                   observe_refusal: Callable[[bulk.Refusal], None] = lambda _item: None) -> run.PlannedEdits:
    """The execution admission boundary, shared by a report and a claimed crate.

    Reservations precede site/semantic proof. The shared planner then checks whole
    dependent groups, including paths introduced by edits. This function writes nothing.
    """
    findings: list[ledger_api.HeldFinding] = []
    pending: list[bulk.PendingFinding] = []
    refused: list[bulk.Refusal] = []
    for finding in selected.findings:
        try:
            availability.require((finding.path,))
        except bulk.ReservationBlocked as blocked:
            pending.append(bulk.PendingFinding(finding.finding_id, blocked.path, blocked.holders))
        except bulk.BulkRenameError as error:
            refusal = bulk.Refusal(finding.finding_id, str(error))
            refused.append(refusal)
            observe_refusal(refusal)
        else:
            findings.append(finding)
    rebuilt = planner(checkout, ledger, base, replace(selected, findings=tuple(findings)), availability, definition)
    for refusal in rebuilt.refusals:
        observe_refusal(refusal)
    return run.available_constant_plan(replace(rebuilt,
        refusals=(*refused, *rebuilt.refusals), pending=(*pending, *rebuilt.pending)), availability)


class AdmissionOutcome(StrEnum):
    ADMITTED = "admitted"
    PENDING = "pending"
    REFUSED = "refused"
    NOT_CHECKED = "not checked"


CROSS_CRATE_PROPOSAL = "proposal spans crates; first form refuses the whole proposal"


def cross_crate_proposals(selected: run.Selection, directories: tuple[str, ...]) -> frozenset[Path]:
    """One frozen selection cannot split a decision's claim across crate runs."""
    proposals: dict[Path, set[str]] = defaultdict(set)
    for finding in selected.findings:
        if isinstance(finding.state, ledger_api.Filed):
            proposals[finding.state.proposal].add(style_proposals.crate_for(directories, finding.path))
    return frozenset(proposal for proposal, crates in proposals.items() if len(crates) != 1)


def dry_run_crates(repo: Path, packages: Sequence[str], rule: str, base_tree: str,
                   ledger: ledger_api.Ledger,
                   server: bulk.RenameServer | run.RenameService = run.RenameService.READ_ONLY,
                   available: Callable[[Path, str], None] = bulk.check_available) -> dict[str, object]:
    """Inspect the clean current base without creating a run, checkout, claim or result.

    Production semantic services may build, so their unavailable proof is explicit.
    Callers with a read-only analyzer may supply it to the same production planner.
    """
    if repo.resolve() != ledger.repo.resolve():
        raise bulk.BulkRenameError("ledger belongs to a different checkout")
    base = run.require_base(repo, base_tree)
    definition = next((item for item in rules.load_rules(repo)
                       if rule in {item.rule_id, item.name}), None)
    if definition is None or definition.family not in {"names", "values"}:
        raise bulk.BulkRenameError("dry run requires a names or values rule")
    members = coverage_report.workspace(ledger, base)
    if any(package not in {member.package_name for member in members} for package in packages):
        raise bulk.BulkRenameError("crate is not in the workspace")
    selected = run.select(ledger, definition)
    directories = tuple(member.directory for member in members)
    spanning = cross_crate_proposals(selected, directories)
    ownership = SourceOwnership(repo, base, members)
    reports: dict[str, list[dict[str, object]]] = {}
    for member in members:
        if member.package_name not in packages:
            continue
        def boundary(_repo: Path, path: str) -> None:
            ownership.require(member.directory, (path,))
            available(ledger.repo, path)
        findings = tuple(item for item in selected.findings
                         if style_proposals.crate_for(directories, item.path) == member.directory)
        cross_crate = tuple(bulk.Refusal(item.finding_id, CROSS_CRATE_PROPOSAL) for item in findings
            if isinstance(item.state, ledger_api.Filed) and item.state.proposal in spanning)
        excluded = {item.finding_id for item in cross_crate}
        def planner(checkout: Path, authority: ledger_api.Ledger, tip: str, selection: run.Selection,
                    availability: run.PlanningFileAvailability, pinned: rules.Rule) -> run.PlannedEdits:
            return run.plan_rule(checkout, authority, tip, selection, pinned, server, availability.available)
        planned = admission_plan(repo, ledger, base,
            run.Selection(tuple(item for item in findings if item.finding_id not in excluded), ()),
            run.PlanningFileAvailability(repo, boundary), definition, planner)
        outcomes: dict[str, dict[str, object]] = {}
        for file in planned.files:
            for item in file.kept:
                outcomes[item.finding.finding_id] = {"finding_id": item.finding.finding_id,
                    "outcome": AdmissionOutcome.ADMITTED}
        for unchecked in planned.not_checked:
            outcomes[unchecked.finding_id] = {"finding_id": unchecked.finding_id,
                "outcome": AdmissionOutcome.NOT_CHECKED, "reason": unchecked.reason}
        for pending in planned.pending:
            outcomes[pending.finding_id] = {**bulk.freeze_pending(pending), "outcome": AdmissionOutcome.PENDING}
        for refused in (*cross_crate, *planned.refusals, *selected.skipped):
            outcomes[refused.finding_id] = {"finding_id": refused.finding_id,
                "outcome": AdmissionOutcome.REFUSED, "reason": refused.reason}
        ids = {item.finding_id for item in findings} | {item.finding_id for item in ledger.named_held(
            item.finding_id for item in selected.skipped)
            if style_proposals.crate_for(directories, item.path) == member.directory}
        reports[member.package_name] = [outcomes.get(identity, {"finding_id": identity,
            "outcome": AdmissionOutcome.NOT_CHECKED, "reason": "planner produced no admission proof"})
            for identity in sorted(ids)]
    return {"base": base, "rule": definition.rule_id, "crates": reports,
            "not_checked": [{"stage": "execution validation and publication",
                "reason": "formatting, compiler checks, tests and final-tree proof require writes; no execution success is forecast"}]}


@dataclass(frozen=True)
class ValidatedEdits:
    """Survivors from bulk_run's execution owner, with its write-ahead journal refreshed."""
    paths: tuple[str, ...]
    findings: tuple[str, ...]
    refusals: dict[str, str]


class PlanExecutor(Protocol):
    def __call__(self, checkout: Path, base: str, planned: run.PlannedEdits,
                 intent: run.WorkspaceIntent, runner: run.Runner,
                 boundary: Callable[[Path, str], None], /) -> ValidatedEdits:
        """Install/preflight the whole plan, journal/write, format and validate via bulk_run."""
        ...


class ExecutionService(StrEnum):
    BULK_RUN = "shared bulk_run execution owner"


class CrateRun:
    """One identified frozen selection; each call advances only the named crate, serially.

    Planning compares only a private checkout at the frozen base.
    Historical evidence and reservations always use canonical authority. No copied ledger
    is opened in a worker checkout.
    """

    def __init__(self, ledger: ledger_api.Ledger, run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
            raise bulk.BulkRenameError("invalid run id")
        self.ledger: ledger_api.Ledger = ledger
        self.repo: Path = ledger.repo.resolve()
        self.folder: Path = ledger.database.parent / "crate_runs" / run_id
        self.path: Path = self.folder / "manifest.json"
        self.run_id: str = run_id
        self.owner: str = style_sync.gate_owner(self.repo)
        self.task: str = "gate:crate-" + hashlib.sha256(str(self.path).encode()).hexdigest()

    def read(self) -> Manifest:
        value = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        if (value.get("version") != 2 or value.get("run") != self.run_id
                or value.get("canonical") != str(self.repo)
                or value.get("database") != str(self.ledger.database.resolve())):
            raise bulk.BulkRenameError("run manifest belongs to another authority or version")
        return cast(Manifest, cast(object, value))

    def save(self, manifest: Manifest) -> None:
        bulk.atomic_json(self.path, manifest)

    def create(self, definition: rules.Rule, base_tree: str, destination: str) -> None:
        with serial(self.folder), ledger_api.refill_lock(self.repo):
            if self.path.exists():
                raise bulk.BulkRenameError("run already exists; resume by --crate and --run")
            base = run.require_base(self.repo, base_tree)
            if definition.family not in {"names", "values"}:
                raise bulk.BulkRenameError("crate run requires a names or values rule")
            if not destination.startswith("refs/heads/"):
                raise bulk.BulkRenameError("destination must be a canonical branch ref")
            _ = run.git(self.repo, "check-ref-format", destination)
            if run.git(self.repo, "rev-parse", destination) != base:
                raise bulk.BulkRenameError("destination must start at the frozen base")
            members = coverage_report.workspace(self.ledger, base)
            selected = run.select(self.ledger, definition)
            directories = tuple(member.directory for member in members)
            spanning = cross_crate_proposals(selected, directories)
            records: dict[str, CrateRecord] = {}
            refusals: list[dict[str, str]] = [
                {"finding_id": item.finding_id, "reason": item.reason} for item in selected.skipped
            ]
            held_by_crate: dict[str, dict[str, list[str]]] = defaultdict(dict)
            for skipped in selected.skipped:
                if isinstance(skipped, run.OpenTicketRefusal):
                    directory = style_proposals.crate_for(directories, skipped.path)
                    held_by_crate[directory][skipped.finding_id] = list(skipped.ticket_ids)
            for member in members:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", member.package_name):
                    raise bulk.BulkRenameError("unsafe package name")
                frozen: list[FrozenFinding] = []
                for finding in selected.findings:
                    if style_proposals.crate_for(directories, finding.path) != member.directory:
                        continue
                    if not isinstance(finding.state, ledger_api.Filed):
                        raise bulk.BulkRenameError("selection is no longer filed")
                    proposal = str(finding.state.proposal)
                    if finding.state.proposal in spanning:
                        refusals.append({"finding_id": finding.finding_id, "reason": CROSS_CRATE_PROPOSAL})
                        continue
                    frozen.append({"id": finding.finding_id, "path": finding.path, "proposal": proposal,
                                   "rule_blob": finding.rule_blob, "evidence": evidence(finding),
                                   "proposal_sha256": hashlib.sha256(Path(proposal).read_bytes()).hexdigest()})
                checkout = copies.workspace_path(self.owner, self.repo,
                                                 f"crate-{self.run_id}.{member.package_name}")
                records[member.package_name] = {
                    "directory": member.directory, "checkout": str(checkout),
                    "branch": f"refs/heads/style-crate/{self.run_id}/workers/{member.package_name}",
                    "intent": str(checkout / ".git/style-batch-intent.json"),
                    "result_path": str(self.folder / (member.package_name + ".result.json")),
                    "selected": frozen, "stage": CrateStage.SELECTED,
                    "finding_holders": held_by_crate.get(member.directory, {}),
                }
            self.save({"version": 2, "run": self.run_id, "canonical": str(self.repo),
                       "database": str(self.ledger.database.resolve()), "base": base,
                       "base_tree": base_tree, "rule": definition.rule_id, "rule_blob": definition.blob,
                       "rule_text": definition.path.read_text(), "destination": destination,
                       "expected_tip": base, "integration_ref": f"refs/heads/style-crate/{self.run_id}/integration",
                       "crates": records, "refusals": refusals})

    @contextmanager
    def frozen_checkout(self, manifest: Manifest) -> Generator[Path]:
        with copies.standing(self.owner, self.repo, copies.CommittedSource(manifest["base"])) as checkout:
            yield checkout

    def definition(self, manifest: Manifest) -> rules.Rule:
        """Read the pinned rule while the standing checkout stays locked."""
        with self.frozen_checkout(manifest) as checkout:
            return self.checked_definition(manifest, checkout)

    def checked_definition(self, manifest: Manifest, checkout: Path) -> rules.Rule:
        definition = next(item for item in rules.load_rules(checkout)
                          if item.rule_id == manifest["rule"])
        if (definition.blob != manifest["rule_blob"]
                or definition.path.read_text() != manifest["rule_text"]):
            raise bulk.BulkRenameError("pinned rule changed; run cannot select or repin")
        return definition

    def result(self, record: CrateRecord) -> CommitEvidence:
        """The atomic result file is the only durable commit evidence; never fall back to a copy."""
        try:
            value = bulk.object_fields(cast(object, json.loads(Path(record["result_path"]).read_text())))
            if set(value) != {"commit", "tree", "paths", "findings", "refusals"}:
                raise ValueError("unexpected result fields")
            for key in ("commit", "tree"):
                word = value[key]
                if not isinstance(word, str) or not re.fullmatch(r"[0-9a-f]{40}", word):
                    raise ValueError("invalid " + key)
            for key in ("paths", "findings"):
                words = bulk.object_list(value[key])
                if not words or any(not isinstance(word, str) or not word for word in words):
                    raise ValueError("invalid " + key)
                if len(set(cast(list[str], words))) != len(words):
                    raise ValueError("duplicate " + key)
            refusals = bulk.object_fields(value["refusals"])
            if any(not isinstance(reason, str) or not reason for reason in refusals.values()):
                raise ValueError("invalid refusals")
            result = cast(CommitEvidence, cast(object, value))
            tickets = record.get("tickets", {})
            if (set(result["findings"]) & refusals.keys()
                    or set(result["findings"]) | refusals.keys() != tickets.keys()):
                raise ValueError("result does not partition the frozen claims")
            checkout = Path(record["checkout"])
            base = self.read()["base"]
            if (run.git(checkout, "rev-parse", result["commit"] + "^1") != base
                    or run.git(checkout, "rev-parse", result["commit"] + "^{tree}") != result["tree"]
                    or list(changed(checkout, base, result["commit"])) != result["paths"]):
                raise ValueError("result differs from its commit")
            return result
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise bulk.BulkRenameError("invalid authoritative crate result: " + str(error)) from error

    def owned(self, record: CrateRecord) -> None:
        assert "owner" in record and "tickets" in record
        owner = ledger_api.BatchOwner(record["owner"])
        for ticket in set(record["tickets"].values()):
            if self.ledger.ticket_batch_owner(ticket) != owner:
                raise bulk.BulkRenameError("late result from a replaced owner: " + ticket)
        current = {finding.finding_id: finding for finding in self.ledger.named_held(record["tickets"])}
        built = (set(self.result(record)["findings"])
                 if CrateStage(record["stage"]) in {CrateStage.COMMITTED, CrateStage.TESTED,
                    CrateStage.IMPORTED, CrateStage.CHECKPOINT_PUBLISHED, CrateStage.INTEGRATED}
                 else set(record["tickets"]) - record.get("terminal_refusals", {}).keys())
        for frozen in record["selected"]:
            if frozen["id"] in built:
                finding = current[frozen["id"]]
                if (finding.build_ticket != ledger_api.BuildTicket(record["tickets"][frozen["id"]])
                        or evidence(finding) != frozen["evidence"]
                        or not isinstance(finding.state, ledger_api.Filed)
                        or str(finding.state.proposal) != frozen["proposal"]
                        or hashlib.sha256(Path(frozen["proposal"]).read_bytes()).hexdigest()
                        != frozen["proposal_sha256"]):
                    raise bulk.BulkRenameError("frozen finding or ownership changed")

    def publish_refusals(self, manifest: Manifest, record: CrateRecord,
                         refused: dict[str, str]) -> None:
        """Under the claim lock, save evidence before releasing membership; retries are idempotent."""
        self.owned(record)
        assert "tickets" in record
        recorded = {row["finding_id"]: row for row in manifest["refusals"]}
        for identity, reason in refused.items():
            recorded[identity] = {"finding_id": identity, "reason": reason}
        manifest["refusals"] = list(recorded.values())
        self.save(manifest)
        for ticket in sorted(set(record["tickets"].values())):
            published = {row.finding_id for row in self.ledger.bulk_refusals(ticket)}
            pending = {identity: reason for identity, reason in refused.items()
                       if record["tickets"][identity] == ticket and identity not in published}
            if pending:
                _ = self.ledger.bulk_release(ticket, pending)

    def abandon(self, manifest: Manifest, record: CrateRecord, reason: str,
                refused: dict[str, str]) -> None:
        # A tested result is pending publication, never abandoned or silently released.
        if CrateStage(record["stage"]) in {CrateStage.TESTED, CrateStage.IMPORTED,
                                          CrateStage.CHECKPOINT_PUBLISHED, CrateStage.INTEGRATED}:
            raise bulk.BulkRenameError("tested result remains pending: " + reason)
        with ledger_api.refill_lock(self.repo):
            if "tickets" in record:
                self.publish_refusals(manifest, record, refused)
            for ticket in set(record.get("tickets", {}).values()):
                assert "owner" in record
                self.ledger.ticket_drop_batch(ticket, ledger_api.BatchOwner(record["owner"]))
        record["stage"] = CrateStage.ABANDONED
        record["error"] = reason
        self.save(manifest)

    def advance(self, package: str, planner: Planner = plan, runner: run.Runner = run.run_command,
                available: Callable[[Path, str], None] = bulk.check_available,
                executor: PlanExecutor | ExecutionService = ExecutionService.BULK_RUN) -> Manifest:
        with serial(self.folder), self.complete_run():
            manifest = self.read()
            with (self.frozen_checkout(manifest) as frozen,
                  self.ledger.rules_from_checkout(frozen), ExitStack() as workers):
                if package not in manifest["crates"]:
                    raise bulk.BulkRenameError("crate is not in the frozen run")
                record = manifest["crates"][package]
                stage = CrateStage(record["stage"])
                if stage in {CrateStage.ABANDONED, CrateStage.INTEGRATED}:
                    return manifest
                checkout = Path(record["checkout"])
                base = manifest["base"]
                definition = self.checked_definition(manifest, frozen)
                if stage != CrateStage.SELECTED or record["selected"]:
                    legacy = checkout if checkout.exists() else None
                    worker = workers.enter_context(copies.workspace(
                        self.owner, self.repo, base,
                        name=checkout.name, branch=record["branch"], task=self.task, path=legacy,
                        run_manifest=self.path))
                    if worker != checkout and stage == CrateStage.SELECTED and not checkout.exists():
                        record["checkout"] = str(worker)
                        record["intent"] = str(worker / ".git/style-batch-intent.json")
                        self.save(manifest)
                        checkout = worker
                    if worker != checkout:
                        raise bulk.BulkRenameError("worker registration differs from the frozen run")
                intent = run.WorkspaceIntent(checkout, f"bulk_run --run {self.run_id} --crate {package} --repo {self.repo}") if checkout.exists() else None
                if stage == CrateStage.CLAIMING:
                    # Write-ahead intent owns independently committed claims even if claim() never returned.
                    assert intent is not None
                    if intent.path.exists():
                        journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
                        owner = ledger_api.BatchOwner(str(journal["batch"]))
                        with ledger_api.refill_lock(self.repo):
                            for ticket in bulk.object_list(journal["tickets"]):
                                self.ledger.ticket_drop_batch(str(ticket), owner)
                    self.abandon(manifest, record, "interrupted claim; its journal owns any released claims", {})
                    return manifest
                if stage == CrateStage.SELECTED:
                    if not record["selected"]:
                        holders = record.get("finding_holders", {})
                        reason = "no selected findings"
                        if holders:
                            ticket_ids = sorted({ticket for tickets in holders.values() for ticket in tickets})
                            noun = "finding" if len(holders) == 1 else "findings"
                            reason += f": {len(holders)} {noun} held by open tickets: " + ", ".join(ticket_ids)
                        self.abandon(manifest, record, reason, {})
                        return manifest
                    with ledger_api.refill_lock(self.repo):
                        # The canonical worker branch must precede ticket assignment and base recording.
                        existing = run.run_command(self.repo, ("git", "rev-parse", "--verify", record["branch"]))
                        if existing.code:
                            _ = run.git(self.repo, "update-ref", record["branch"], base, "0" * 40)
                        elif existing.stdout.strip() != base:
                            raise bulk.BulkRenameError("worker branch moved before claiming")
                        intent = run.WorkspaceIntent(checkout, f"bulk_run --run {self.run_id} --crate {package} --repo {self.repo}")
                        record["stage"] = CrateStage.CLAIMING
                        self.save(manifest)
                        findings = self.ledger.named_held(frozen["id"] for frozen in record["selected"])
                        frozen_by_id = {item["id"]: item for item in record["selected"]}
                        if any(evidence(finding) != frozen_by_id[finding.finding_id]["evidence"]
                               for finding in findings):
                            raise bulk.BulkRenameError("frozen selection changed before claiming")
                        record["tickets"] = run.claim(self.ledger, run.Selection(findings, ()),
                                                      str(definition.family), record["branch"], intent)
                        record["owner"] = intent.batch_id
                        if any(self.ledger.ticket_base(ticket) != base for ticket in set(record["tickets"].values())):
                            raise bulk.BulkRenameError("canonical ticket base differs from frozen base")
                        record["stage"] = CrateStage.CLAIMED
                        self.save(manifest)
                    stage = CrateStage.CLAIMED
                assert intent is not None
                self.owned(record)
                assert "owner" in record and "tickets" in record
                if stage == CrateStage.EDITED and Path(record["result_path"]).exists():
                    _ = self.result(record)
                    record["stage"] = CrateStage.COMMITTED
                    self.save(manifest)
                    stage = CrateStage.COMMITTED
                if stage == CrateStage.EDITED:
                    # Accept only the exact journaled staging; recover() itself deliberately refuses an index.
                    journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
                    files = bulk.object_fields(journal["files"])
                    created = bulk.object_fields(journal.get("created_files", {}))
                    for path in filter(None, run.git(checkout, "diff", "--cached", "--name-only", "-z").split("\0")):
                        after = (bulk.object_fields(files[path])["after"] if path in files else created[path])
                        staged = subprocess.run(("git", "show", ":" + path), cwd=checkout,
                                                capture_output=True, check=True).stdout
                        if staged != bytes.fromhex(str(after)):
                            raise bulk.BulkRenameError("interrupted index has unrecorded bytes")
                    _ = run.git(checkout, "reset", "--mixed", base)
                    _ = intent.recover()
                    record["stage"] = CrateStage.CLAIMED
                    self.save(manifest)
                    stage = CrateStage.CLAIMED
                if stage in {CrateStage.CLAIMED, CrateStage.WAITING_FOR_RESERVATIONS}:
                    # Reject unresolved recovery before any execution or abandonment transition.
                    if intent.retry_eligible():
                        _ = intent.recover()
                        intent.clear_admission()
                    refused: dict[str, str] = dict(record.get("terminal_refusals", {}))
                    if stage == CrateStage.WAITING_FOR_RESERVATIONS:
                        with ledger_api.refill_lock(self.repo):
                            self.publish_refusals(manifest, record, refused)
                    resume_record = deepcopy(record)
                    try:
                        with bulk.admission_attempt(intent.stop_admission, intent.clear_admission):
                            self.execute(manifest, record, intent, definition, planner, runner, available, executor, refused)
                    except (KeyboardInterrupt, SystemExit) as stopped:
                        if not intent.retry_eligible():
                            raise
                        try:
                            if not isinstance(stopped, bulk.StyleAdmissionStopped):
                                intent.restore_admission(stopped)
                        finally:
                            manifest["crates"][package] = resume_record
                            self.save(manifest)
                        raise
                    except bulk.ReservationBlocked as blocked:
                        # A writer may appear after planning. Undo journaled private edits,
                        # retain all claims, and retry normal proof on the next invocation.
                        _ = run.git(checkout, "reset", "--mixed", base)
                        _ = intent.recover()
                        self.wait_for_reservations(manifest, record,
                            tuple(bulk.PendingFinding(identity, blocked.path, blocked.holders)
                                  for identity in record["tickets"] if identity not in refused), refused)
                        return manifest
                    except Exception as error:
                        self.abandon(manifest, record, str(error), refused)
                        return manifest
                    stage = CrateStage(record["stage"])
                if stage == CrateStage.COMMITTED:
                    result_evidence = self.result(record)
                    commit = result_evidence["commit"]
                    actual = run.git(checkout, "rev-parse", "HEAD")
                    if actual == base:
                        if (run.git(checkout, "write-tree") != result_evidence["tree"]
                                or run.git(checkout, "diff", "--name-only")):
                            raise bulk.BulkRenameError("worker index or files changed after commit preparation")
                        _ = run.git(checkout, "update-ref", record["branch"], commit, base)
                    elif actual != commit:
                        raise bulk.BulkRenameError("worker checkout moved after commit")
                    if run.git(checkout, "status", "--porcelain", "--untracked-files=no"):
                        raise bulk.BulkRenameError("worker files changed after commit")
                    result = run.test_tree(checkout, commit, result_evidence["tree"], runner, {}, owner=self.owner)
                    if result.code:
                        self.abandon(manifest, record, "tests failed: " + run.first_failing_test(result),
                                     result_evidence["refusals"])
                        return manifest
                    record["stage"] = CrateStage.TESTED
                    self.save(manifest)
                    stage = CrateStage.TESTED
                if stage == CrateStage.TESTED:
                    self.import_result(manifest, record)
                    stage = CrateStage.IMPORTED
                if stage == CrateStage.IMPORTED:
                    result_evidence = self.result(record)
                    with ledger_api.refill_lock(self.repo):
                        self.publish_refusals(manifest, record, result_evidence["refusals"])
                        # Refusal decisions are durable in the tested result before releasing membership.
                        # A partial checkpoint resume must not call bulk_release on checkpointed tickets.
                        built_tickets = {record["tickets"][identity] for identity in result_evidence["findings"]}
                        for ticket in sorted(set(record["tickets"].values())):
                            if ticket not in built_tickets:
                                self.ledger.ticket_drop_batch(ticket, ledger_api.BatchOwner(record["owner"]))
                        for ticket in sorted(built_tickets):
                            self.ledger.ticket_checkpoint(ticket, result_evidence["commit"])
                        self.ledger.bulk_share_checkpoint(sorted(built_tickets))
                    record["stage"] = CrateStage.CHECKPOINT_PUBLISHED
                    self.save(manifest)
                    intent.finish()
                    stage = CrateStage.CHECKPOINT_PUBLISHED
                if stage == CrateStage.CHECKPOINT_PUBLISHED:
                    self.integrate(manifest, record, runner)
                return manifest

    @contextmanager
    def complete_run(self) -> Generator[None]:
        """End worker tasks after terminal manifest publication and worker lock release."""
        try:
            yield
        finally:
            manifest = self.read()
            if all(CrateStage(record["stage"]) in {CrateStage.INTEGRATED, CrateStage.ABANDONED}
                   for record in manifest["crates"].values()):
                paths = [Path(record["checkout"]) for record in manifest["crates"].values()]
                for record in manifest["crates"].values():
                    path = Path(record["checkout"])
                    if path.exists() and isinstance(scratch.pending_removal(path), copy_record.NotRegistered):
                        with copies.workspace(self.owner, self.repo, manifest["base"], name=path.name,
                                              branch=record["branch"], task=self.task, path=path, run_manifest=self.path):
                            pass
                copies.end_workspaces(self.task, paths)

    def wait_for_reservations(self, manifest: Manifest, record: CrateRecord,
                              pending: tuple[bulk.PendingFinding, ...], refused: dict[str, str]) -> None:
        record["pending"] = [bulk.freeze_pending(item) for item in pending]
        record["terminal_refusals"] = dict(refused)
        record["stage"] = CrateStage.WAITING_FOR_RESERVATIONS
        self.save(manifest)
        with ledger_api.refill_lock(self.repo):
            self.publish_refusals(manifest, record, refused)

    def execute(self, manifest: Manifest, record: CrateRecord, intent: run.WorkspaceIntent,
                definition: rules.Rule, planner: Planner, runner: run.Runner,
                available: Callable[[Path, str], None], executor: PlanExecutor | ExecutionService,
                refused: dict[str, str]) -> None:
        assert "tickets" in record and "owner" in record
        checkout, base = Path(record["checkout"]), manifest["base"]
        def unchanged_base() -> None:
            if run.require_base(checkout, manifest["base_tree"]) != base:
                raise bulk.BulkRenameError("planning requires the exact frozen private base")
        unchanged_base()
        members = coverage_report.workspace(self.ledger, base)
        ownership = SourceOwnership(checkout, base, members)
        def boundary(_repo: Path, path: str) -> None:
            ownership.require(record["directory"], (path,))
            available(self.repo, path)
        selected = run.Selection(self.ledger.named_held(
            identity for identity in record["tickets"] if identity not in refused), ())
        availability = run.PlanningFileAvailability(checkout, boundary)

        def remember_refusal(item: bulk.Refusal) -> None:
            # Preserve decisions even when later planning or group admission aborts.
            refused[item.finding_id] = item.reason

        def remember_refusals(planned: run.PlannedEdits) -> run.PlannedEdits:
            for item in planned.refusals:
                remember_refusal(item)
            return planned

        def admitted(identities: frozenset[str]) -> run.PlannedEdits:
            findings = tuple(item for item in selected.findings if item.finding_id in identities)
            frozen = tuple(thaw_machine_judgment(item) for item in record.get("machine_judgments", [])
                           if item["finding_id"] not in refused)
            for judgment in frozen:
                finding = next(item for item in selected.findings if item.finding_id == judgment.finding_id)
                reason = run.validate_machine_judgment(self.ledger, finding, base, checkout, judgment)
                if reason:
                    raise bulk.BulkRenameError(reason)
            previous = set(record.get("judgment_planned_findings", record["tickets"]
                                      if "machine_judgments" in record else ()))
            selection = run.Selection(findings, (), frozen,
                                      run.JudgmentPolicy.FROZEN_ONLY,
                                      frozenset(item.finding_id for item in findings) - previous)
            rebuilt = remember_refusals(admission_plan(
                checkout, self.ledger, base, selection, availability, definition, planner, remember_refusal))
            if any(item.finding_id in previous and item not in frozen for item in rebuilt.judgments):
                raise bulk.BulkRenameError("planner replaced a frozen machine judgment")
            judgments = {item.finding_id: item for item in (*frozen, *rebuilt.judgments)}
            record["machine_judgments"] = [freeze_machine_judgment(item) for item in judgments.values()]
            # The shared plan owner removes whole dependent edit groups, including files
            # introduced by a planner. No overlay may precede this boundary check.
            checked = rebuilt
            record["judgment_planned_findings"] = sorted(previous | ({item.finding_id for item in findings}
                                                       - {item.finding_id for item in checked.pending}))
            self.save(manifest)
            return checked

        planned = admitted(frozenset(record["tickets"]))
        if planned.pending:
            # One fresh availability retry after independent planning; never sleep or loop.
            still_pending: list[bulk.PendingFinding] = []
            for item in planned.pending:
                try:
                    boundary(checkout, item.path)
                except bulk.ReservationBlocked as blocked:
                    still_pending.append(bulk.PendingFinding(item.finding_id, blocked.path, blocked.holders))
            if still_pending:
                self.wait_for_reservations(manifest, record, tuple(still_pending), refused)
                return
            availability = run.PlanningFileAvailability(checkout, boundary)
            planned = admitted(frozenset(record["tickets"]) - refused.keys())
            if planned.pending:
                self.wait_for_reservations(manifest, record, planned.pending, refused)
                return
        record["pending"] = []
        unchanged_base()
        record["stage"] = CrateStage.EDITED
        self.save(manifest)
        while True:
            if not planned.files:
                validated = ValidatedEdits((), (), refused)
            elif isinstance(executor, ExecutionService):
                match executor:
                    case ExecutionService.BULK_RUN:
                        workspace = run.WorkspaceEdits(checkout, base, ())
                        kept_paths = run.execute_plan(
                            workspace, planned, definition, self.ledger, intent, runner, boundary,
                            admitted, dict.fromkeys(("rename", "fmt", "check_clippy"), 0.0), [], refused)
                        validated = ValidatedEdits(tuple(sorted(kept_paths)),
                                                  tuple(item.finding.finding_id for path in sorted(kept_paths)
                                                        for item in workspace.files[path].kept), refused)
            else:
                validated = executor(checkout, base, planned, intent, runner, boundary)
            refused.update(validated.refusals)
            kept, surviving = validated.paths, validated.findings
            if not surviving:
                refused.update((identity, "not retained by crate planner")
                               for identity in record["tickets"] if identity not in refused)
                raise bulk.BulkRenameError("no surviving findings: " + "; ".join(sorted(set(refused.values()))))
            _ = run.git(checkout, "add", "--", *sorted(kept))
            tree = run.git(checkout, "write-tree")
            final_ownership = SourceOwnership(checkout, tree, members)
            def final_boundary(_repo: Path, path: str) -> None:
                ownership.require(record["directory"], (path,))
                final_ownership.require(record["directory"], (path,))
            final_plan = run.available_constant_plan(
                planned, run.PlanningFileAvailability(checkout, final_boundary))
            refused.update((item.finding_id, item.reason) for item in final_plan.refusals)
            removed = {item.finding_id for item in final_plan.refusals} & set(surviving)
            if not removed:
                for path in changed(checkout, base, tree):
                    final_boundary(checkout, path)
                break
            # A newly introduced loader can invalidate one atomic group. Restore through
            # WorkspaceEdits, then let the existing planner prove only the survivors anew.
            rollback = run.WorkspaceEdits(checkout, base, ())
            rollback.set_plan(planned)
            rollback.restore(sorted(kept))
            planned = admitted(frozenset(surviving) - removed)
        refused.update((identity, "not retained by crate planner")
                       for identity in record["tickets"] if identity not in surviving and identity not in refused)
        for identity in surviving:
            _ = refused.pop(identity, "")
        commit = run.git(checkout, "commit-tree", tree, "-p", base, "-m",
                         f"style({definition.name}): crate batch\n\nTested-Tree: {tree}\nRun: {self.run_id}")
        result: CommitEvidence = {"commit": commit, "tree": tree, "paths": sorted(kept),
                                  "findings": list(surviving), "refusals": refused}
        bulk.atomic_json(Path(record["result_path"]), result)
        record["stage"] = CrateStage.COMMITTED
        self.save(manifest)

    def import_result(self, manifest: Manifest, record: CrateRecord) -> None:
        result_evidence = self.result(record)
        with ledger_api.refill_lock(self.repo):
            self.owned(record)
            commit = result_evidence["commit"]
            _ = run.git(self.repo, "fetch", "--no-tags", record["checkout"], commit)
            current = run.git(self.repo, "rev-parse", record["branch"])
            if current != commit:
                _ = run.git(self.repo, "update-ref", record["branch"], commit, manifest["base"])
            record["stage"] = CrateStage.IMPORTED
            self.save(manifest)

    def integrate(self, manifest: Manifest, record: CrateRecord, runner: run.Runner) -> None:
        result_evidence = self.result(record)
        self.owned(record)
        destination, expected = manifest["destination"], manifest["expected_tip"]
        integration = record.get("integration")
        candidate = integration["commit"] if integration is not None else None
        if (integration is not None and integration["stage"] == IntegrationStage.TESTED
                and run.git(self.repo, "rev-parse", destination) == candidate):
            # Crash after CAS but before the final manifest write: the exact proved result won.
            manifest["expected_tip"] = integration["commit"]
            record["stage"] = CrateStage.INTEGRATED
            self.save(manifest)
            return
        current = run.git(self.repo, "rev-parse", destination)
        if (current != expected
                or integration is not None and integration.get("destination_tip") != expected):
            # A candidate proves one pair of parents. Moving either invalidates that proof.
            manifest["expected_tip"] = expected = current
            candidate = None
            _ = record.pop("integration", None)
            self.save(manifest)
        if candidate is None:
            previous = ledger_api.tree_entries(self.repo, expected)
            base_entries = ledger_api.tree_entries(self.repo, manifest["base"])
            worker = result_evidence["commit"]
            worker_entries = ledger_api.tree_entries(self.repo, worker)
            paths = changed(self.repo, manifest["base"], worker)
            overlap = any(previous.get(path) != base_entries.get(path) for path in paths)
            try:
                tree = run.git(self.repo, "merge-tree", "--write-tree",
                               expected, worker).splitlines()[0]
            except bulk.BulkRenameError as error:
                raise bulk.BulkRenameError("crate integration overlaps already published paths: "
                                           + str(error)) from error
            union = dict(previous)
            for path in paths:
                if path in worker_entries:
                    union[path] = worker_entries[path]
                else:
                    _ = union.pop(path, None)
            if not overlap and ledger_api.tree_entries(self.repo, tree) != union:
                raise bulk.BulkRenameError("integration tree differs from the exact edit union")
            candidate = run.git(self.repo, "commit-tree", tree, "-p", expected, "-p", worker,
                                "-m", f"style(crate-run): integrate {self.run_id}")
            record["integration"] = {"commit": candidate, "destination_tip": expected,
                                     "stage": IntegrationStage.PREPARED}
            self.save(manifest)
        private = manifest["integration_ref"]
        actual = run.run_command(self.repo, ("git", "rev-parse", "--verify", private))
        if actual.code or actual.stdout.strip() != candidate:
            _ = run.git(self.repo, "update-ref", private, candidate,
                         actual.stdout.strip() if actual.code == 0 else "0" * 40)
        assert "integration" in record
        if IntegrationStage(record["integration"]["stage"]) == IntegrationStage.PREPARED:
            with copies.standing(self.owner, self.repo, copies.CommittedSource(candidate)) as checkout:
                tree = run.git(checkout, "rev-parse", "HEAD^{tree}")
                run.require_test_tree(checkout, tree)
                if run.git(checkout, "rev-parse", "HEAD") != candidate:
                    raise bulk.BulkRenameError("integration checkout moved")
                members = coverage_report.workspace(self.ledger, candidate)
                ownership = SourceOwnership(checkout, candidate, members)
                for path in changed(checkout, expected, candidate):
                    ownership.require(ownership.home(path), (path,))
                workspace = run.WorkspaceEdits(checkout, candidate, ())
                _ = run.validate(workspace, runner, {"check_clippy": 0.0}, [], lambda paths: paths)
                result = run.test_tree(checkout, candidate, tree, runner, {}, owner=self.owner)
                if result.code:
                    raise bulk.BulkRenameError("combined-tree proof failed; tested claims remain pending")
                record["integration"]["stage"] = IntegrationStage.TESTED
                self.save(manifest)
        with ledger_api.refill_lock(self.repo):
            self.owned(record)
            worktrees = run.git(self.repo, "worktree", "list", "--porcelain")
            if "branch " + destination in worktrees.splitlines():
                reason = ("destination is checked out; checkout owner must integrate validated candidate "
                          + candidate + " from " + private + "; tested claims remain pending")
                record["error"] = reason
                self.save(manifest)
                raise bulk.BulkRenameError(reason)
            _ = record.pop("error", None)
            _ = run.git(self.repo, "update-ref", destination, candidate,
                        record["integration"]["destination_tip"])
            manifest["expected_tip"] = candidate
            record["stage"] = CrateStage.INTEGRATED
            self.save(manifest)


def run_crates(repo: Path, packages: Sequence[str], run_id: str, rule: str, base_tree: str,
               destination: str, ledger: ledger_api.Ledger,
               server: bulk.RenameServer | run.RenameService,
               runner: run.Runner, available: Callable[[Path, str], None]) -> int:
    """Freeze once or resume by identity, then advance named crates serially."""
    if repo.resolve() != ledger.repo.resolve():
        raise bulk.BulkRenameError("ledger belongs to a different checkout")
    batch = CrateRun(ledger, run_id)
    if not batch.path.exists():
        if not rule or not base_tree or not destination:
            raise bulk.BulkRenameError("new crate run requires --rule, --base-tree and --destination")
        definition = next((item for item in rules.load_rules(repo)
                           if item.rule_id == rule or item.name == rule), None)
        if definition is None:
            raise bulk.BulkRenameError("unknown crate run rule")
        batch.create(definition, base_tree, destination)
    manifest = batch.read()
    frozen_rule = batch.definition(manifest)
    if ((rule and rule not in {manifest["rule"], frozen_rule.name})
            or (base_tree and base_tree != manifest["base_tree"])
            or (destination and destination != manifest["destination"])):
        raise bulk.BulkRenameError("arguments differ from the frozen run")
    if any(package not in manifest["crates"] for package in packages):
        raise bulk.BulkRenameError("crate is not in the frozen run")

    def planner(checkout: Path, authority: ledger_api.Ledger, base: str, selected: run.Selection,
                availability: run.PlanningFileAvailability, definition: rules.Rule) -> run.PlannedEdits:
        return run.plan_rule(checkout, authority, base, selected, definition, server, availability.available)

    status = 0
    for package in packages:
        manifest = batch.advance(package, planner, runner, available)
        record = manifest["crates"][package]
        print(json.dumps(record, indent=2))
        if record["stage"] != CrateStage.INTEGRATED:
            status = 1
    return status
