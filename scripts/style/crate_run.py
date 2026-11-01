#!/usr/bin/env python3
"""Identified serial crate batches, entered through bulk_run --crate.

Canonical Git and the ledger own claims and publication. Private checkouts own edits and
validation. The existing workspace validation and exact-tree test proof are unchanged.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import posixpath
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast

import bulk_rename as bulk
import bulk_run as run
import coverage_report
import rules
import style_ledger as ledger_api
import style_proposals
import style_footprint
from style_ledger import _path_attributes  # pyright: ignore[reportPrivateUsage]


class CrateStage(StrEnum):
    SELECTED = "selected"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
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


class CrateRecord(TypedDict):
    directory: str
    checkout: str
    branch: str
    intent: str
    result_path: str
    selected: list[FrozenFinding]
    finding_holders: NotRequired[dict[str, list[str]]]
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


def invoked_source_loaders(parsed: bulk.BindingText) -> tuple[tuple[int, int, frozenset[str]], ...]:
    """Resolve invoked builtin paths through the import owner's use trees and lexical scopes.

    Qualified reexports and alias chains retain literal loading edges. Multiple possible
    bindings contribute all their loader identities rather than silently admitting a target.
    Tokenization, macro boundaries, grouped imports and scope extents stay with their owners.
    """
    words = tuple(word.removeprefix("r#") for word in parsed.words)
    trees = style_footprint.use_trees(words, style_footprint.code_item_starts(words))
    context = style_footprint.import_context(words)
    imports: dict[tuple[tuple[str, ...], str], list[tuple[tuple[str, ...], int]]]
    imports = defaultdict(list)
    loaders = {"include", "include_str", "include_bytes"}

    def module(index: int) -> tuple[str, ...]:
        return tuple(name.split("@")[0] for name in context.scopes[index].module)

    for index, prefix in trees.prefixes.items():
        if words[index] == "*" or words[index + 1:index + 2] == ("::",):
            continue
        target = (*prefix, words[index])
        if words[index] == "self" and prefix:
            target = prefix
        bound = words[index + 2] if words[index + 1:index + 2] == ("as",) else target[-1]
        keyword = trees.keywords[index]
        imports[module(keyword), bound].append((target, keyword))

    def bound_loaders(owner: tuple[str, ...], name: str, suffix: tuple[str, ...], site: int,
                      seen: frozenset[tuple[int, str]]) -> set[str]:
        found: set[str] = set()
        for target, keyword in imports.get((owner, name), ()):
            block = context.scopes[keyword].block
            if ((keyword, name) in seen
                    or (block >= 0 and block not in context.module_blocks
                        and not block < site < context.block_ends[block])):
                continue
            found.update(resolve((*target, *suffix), keyword, seen | {(keyword, name)}))
        return found

    def qualified_loaders(parts: tuple[str, ...], site: int,
                          seen: frozenset[tuple[int, str]]) -> set[str]:
        found: set[str] = set()
        for index, name in enumerate(parts):
            found.update(bound_loaders(parts[:index], name, parts[index + 1:], site, seen))
        return found

    def resolve(parts: tuple[str, ...], site: int,
                seen: frozenset[tuple[int, str]]) -> set[str]:
        if not parts:
            return set()
        head, last = parts[0], parts[-1]
        if len(parts) == 2 and head in {"std", "core"} and last in loaders:
            return {last}
        owner = module(site)
        found = {head} if len(parts) == 1 and head in loaders else set[str]()
        if head in style_footprint.PATH_ANCHORS:
            target = style_footprint.resolved_module(parts, owner)
            if not isinstance(target, style_footprint.UnresolvedPath) and target:
                found.update(qualified_loaders(target, site, seen))
            return found
        for depth in range(len(owner), -1, -1):
            found.update(qualified_loaders((*owner[:depth], *parts), site, seen))
        return found

    result: list[tuple[int, int, frozenset[str]]] = []
    for bang, stop in parsed.macros:
        # BindingText owns real macro boundaries. Field/method names, imports,
        # strings and comments do not establish a loader invocation.
        start = bang - 1
        while (start >= 2 and words[start - 1] == "::"
               and style_footprint.IDENTIFIER.fullmatch(words[start - 2])):
            start -= 2
        if words[max(0, start - 1):start] == (".",):
            continue
        names = resolve(words[start:bang:2], bang, frozenset())
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
                 if path.endswith(".rs") and entry.kind == "blob"
                 and entry.mode in ledger_api.REGULAR_FILE_MODES}
        edges: dict[str, set[str]] = defaultdict(set)
        inline_contexts: set[tuple[str, bulk.ModuleLoadingDirectory]] = set()
        for path, entry in inventory.items():
            if path.endswith(".rs"):
                self.owners[path] = {self.home(path)}
                if entry.kind != "blob" or entry.mode not in ledger_api.REGULAR_FILE_MODES:
                    self.uncertain[path].append("source is not a regular file")
        for path, content in ledger_api.blob_contents(repo, blobs):
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
            for bang, stop, names in invoked_source_loaders(parsed):
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
        # Target roots can be configured outside their package directory.
        # Inspect the exact frozen manifests and paths in a read-only exported checkout.
        for source, targets in edges.items():
            for target in targets:
                if target not in self.owners:
                    self.uncertain[source].append(f"missing or external source {target}")
        self.edges: dict[str, frozenset[str]] = {
            source: frozenset(targets & self.owners.keys()) for source, targets in edges.items()}
        self.module_contexts: set[tuple[str, bulk.ModuleLoadingDirectory]] = {
            (target, bulk.OwnedModuleDirectory(Path(target).parent))
            for source, targets in self.edges.items() for target in targets
            if self.home(source) != self.home(target)} | inline_contexts
        self.propagate()

    def propagate(self) -> None:
        """Share only mounted sources and their transitive module descendants, never a package.

        bulk_rename owns lexical child-module resolution, including explicit paths and inline
        modules. An unproved route makes only its declaring file uncertain; it does not guess
        at other sources in the package. An unproved inline mount also protects the sources
        in its literal loading folders until the module owner can prove the child routes.
        All lookups use this frozen inventory.
        """
        pending = list(self.module_contexts)
        known_paths = {path for path, _ in self.module_contexts}
        visited: set[tuple[str, bulk.ModuleLoadingDirectory]] = set()
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
            explicit = bulk.module_file_path_evidence(
                Path(path), parsed, bulk.ModulePathUse.INCLUDING_INLINE)
            routes = bulk.module_source_routes(Path(path), parsed, loading, explicit.paths,
                                               lambda child: posixpath.normpath(str(child)) in self.owners,
                                               explicit.failures)
            for route in routes:
                match route:
                    case bulk.UnprovedModuleSource():
                        if route.reason not in self.uncertain[path]:
                            self.uncertain[path].append(route.reason)
                        if parsed.words[route.declaration + 2:route.declaration + 3] == ("{",):
                            reason = f"unproved inline mount in {path}: {route.reason}"
                            for target in self.owners:
                                if (any(Path(target).is_relative_to(folder)
                                        for folder in self.inline_folders.get(path, ()))
                                        and reason not in self.uncertain[target]):
                                    self.uncertain[target].append(reason)
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
        changed = True
        while changed:
            changed = False
            for source, targets in self.edges.items():
                for target in targets:
                    owners = self.owners[target]
                    previous = len(owners)
                    owners.update(self.owners[source])
                    changed |= len(owners) != previous

    def target_roots(self, checkout: Path, members: tuple[rules.WorkspaceMember, ...]) -> None:
        for member in members:
            for target in bulk.cargo_target_roots(checkout / member.directory):
                try:
                    path = target.path.resolve().relative_to(checkout).as_posix()
                except ValueError:
                    self.uncertain[str(target.path)].append("target outside checkout")
                    continue
                if path not in self.owners:
                    self.uncertain[path].append("target absent from frozen sources")
                else:
                    self.owners[path].add(member.directory)
                    if self.home(path) != member.directory:
                        self.module_contexts.add((path, bulk.OwnedModuleDirectory(Path(path).parent)))
        self.propagate()

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

    Planning requires the canonical checkout to remain clean at the frozen base.
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
            proposals: dict[str, set[str]] = defaultdict(set)
            for finding in selected.findings:
                if isinstance(finding.state, ledger_api.Filed):
                    proposals[str(finding.state.proposal)].add(style_proposals.crate_for(directories, finding.path))
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
                    if len(proposals[proposal]) != 1:
                        refusals.append({"finding_id": finding.finding_id,
                                         "reason": "proposal spans crates; first form refuses the whole proposal"})
                        continue
                    frozen.append({"id": finding.finding_id, "path": finding.path, "proposal": proposal,
                                   "rule_blob": finding.rule_blob, "evidence": evidence(finding),
                                   "proposal_sha256": hashlib.sha256(Path(proposal).read_bytes()).hexdigest()})
                checkout = self.folder / member.package_name
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
                 else set(record["tickets"]))
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

    def clone(self, checkout: Path, branch: str, base: str) -> None:
        if not checkout.exists():
            _ = run.git(self.repo, "clone", "--shared", "--no-checkout", str(self.repo), str(checkout))
            _ = run.git(checkout, "checkout", "-b", branch.removeprefix("refs/heads/"), base)
            for key in ("user.name", "user.email"):
                _ = run.git(checkout, "config", key, run.git(self.repo, "config", key))
        if run.git(checkout, "remote", "get-url", "origin") != str(self.repo):
            raise bulk.BulkRenameError("unrelated execution checkout")

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
        with serial(self.folder):
            manifest = self.read()
            if package not in manifest["crates"]:
                raise bulk.BulkRenameError("crate is not in the frozen run")
            record = manifest["crates"][package]
            stage = CrateStage(record["stage"])
            if stage in {CrateStage.ABANDONED, CrateStage.INTEGRATED}:
                return manifest
            checkout = Path(record["checkout"])
            base = manifest["base"]
            definition = next(item for item in rules.load_rules(self.repo) if item.rule_id == manifest["rule"])
            if definition.blob != manifest["rule_blob"] or definition.path.read_text() != manifest["rule_text"]:
                raise bulk.BulkRenameError("pinned rule changed; run cannot select or repin")
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
                    self.clone(checkout, record["branch"], base)
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
            if stage == CrateStage.CLAIMED:
                refused: dict[str, str] = {}
                try:
                    self.execute(manifest, record, intent, definition, planner, runner, available, executor, refused)
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
                result = run.test_tree(checkout, commit, result_evidence["tree"], runner, {})
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

    def execute(self, manifest: Manifest, record: CrateRecord, intent: run.WorkspaceIntent,
                definition: rules.Rule, planner: Planner, runner: run.Runner,
                available: Callable[[Path, str], None], executor: PlanExecutor | ExecutionService,
                refused: dict[str, str]) -> None:
        assert "tickets" in record and "owner" in record
        checkout, base = Path(record["checkout"]), manifest["base"]
        def unchanged_base() -> None:
            if (run.require_base(self.repo, manifest["base_tree"]) != base
                    or run.require_base(checkout, manifest["base_tree"]) != base):
                raise bulk.BulkRenameError("planning requires the exact frozen base in both checkouts")
        unchanged_base()
        members = coverage_report.workspace(self.ledger, base)
        ownership = SourceOwnership(checkout, base, members)
        ownership.target_roots(checkout, members)
        def boundary(_repo: Path, path: str) -> None:
            ownership.require(record["directory"], (path,))
            available(self.repo, path)
        selected = run.Selection(self.ledger.named_held(record["tickets"]), ())
        availability = run.PlanningFileAvailability(checkout, boundary)

        def remember_refusals(planned: run.PlannedEdits) -> run.PlannedEdits:
            refused.update((item.finding_id, item.reason) for item in planned.refusals)
            return planned

        def admitted(identities: frozenset[str]) -> run.PlannedEdits:
            findings: list[ledger_api.HeldFinding] = []
            for finding in selected.findings:
                if finding.finding_id not in identities:
                    continue
                try:
                    availability.require((finding.path,))
                except bulk.BulkRenameError as error:
                    refused[finding.finding_id] = str(error)
                else:
                    findings.append(finding)
            rebuilt = remember_refusals(planner(
                checkout, self.ledger, base, run.Selection(tuple(findings), ()),
                availability, definition))
            # The shared plan owner removes whole dependent edit groups, including files
            # introduced by a planner. No overlay may precede this boundary check.
            return remember_refusals(run.available_constant_plan(rebuilt, availability))

        planned = admitted(frozenset(record["tickets"]))
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
            final_ownership.target_roots(checkout, members)
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
        if run.git(self.repo, "rev-parse", destination) != expected:
            raise bulk.BulkRenameError("destination tip moved; tested claims remain pending")
        if candidate is None:
            previous = ledger_api.tree_entries(self.repo, expected)
            base_entries = ledger_api.tree_entries(self.repo, manifest["base"])
            worker = result_evidence["commit"]
            worker_entries = ledger_api.tree_entries(self.repo, worker)
            paths = changed(self.repo, manifest["base"], worker)
            if any(previous.get(path) != base_entries.get(path) for path in paths):
                raise bulk.BulkRenameError("crate integration overlaps already published paths")
            tree = run.git(self.repo, "merge-tree", "--write-tree", expected, worker).splitlines()[0]
            union = dict(previous)
            for path in paths:
                if path in worker_entries:
                    union[path] = worker_entries[path]
                else:
                    _ = union.pop(path, None)
            if ledger_api.tree_entries(self.repo, tree) != union:
                raise bulk.BulkRenameError("integration tree differs from the exact edit union")
            candidate = run.git(self.repo, "commit-tree", tree, "-p", expected, "-p", worker,
                                "-m", f"style(crate-run): integrate {self.run_id}")
            record["integration"] = {"commit": candidate, "stage": IntegrationStage.PREPARED}
            self.save(manifest)
        private = manifest["integration_ref"]
        actual = run.run_command(self.repo, ("git", "rev-parse", "--verify", private))
        if actual.code or actual.stdout.strip() != candidate:
            _ = run.git(self.repo, "update-ref", private, candidate,
                         expected if actual.code == 0 else "0" * 40)
        assert "integration" in record
        if IntegrationStage(record["integration"]["stage"]) == IntegrationStage.PREPARED:
            checkout = self.folder / ("integration-" + candidate)
            self.clone(checkout, private, candidate)
            tree = run.git(checkout, "rev-parse", "HEAD^{tree}")
            if run.require_base(checkout, tree) != candidate:
                raise bulk.BulkRenameError("integration checkout moved")
            members = coverage_report.workspace(self.ledger, manifest["base"])
            ownership = SourceOwnership(checkout, candidate, members)
            ownership.target_roots(checkout, members)
            for path in changed(checkout, manifest["base"], candidate):
                ownership.require(ownership.home(path), (path,))
            workspace = run.WorkspaceEdits(checkout, candidate, ())
            _ = run.validate(workspace, runner, {"check_clippy": 0.0}, [], lambda paths: paths)
            result = run.test_tree(checkout, candidate, tree, runner, {})
            if result.code:
                raise bulk.BulkRenameError("combined-tree proof failed; tested claims remain pending")
            record["integration"]["stage"] = IntegrationStage.TESTED
            self.save(manifest)
        with ledger_api.refill_lock(self.repo):
            self.owned(record)
            worktrees = run.git(self.repo, "worktree", "list", "--porcelain")
            if "branch " + destination in worktrees.splitlines():
                raise bulk.BulkRenameError("destination is checked out; publication requires an inactive branch")
            _ = run.git(self.repo, "update-ref", destination, candidate, expected)
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
    frozen_rule = next(item for item in rules.load_rules(repo) if item.rule_id == manifest["rule"])
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
