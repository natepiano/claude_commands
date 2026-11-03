"""Route changes through staff identities for the page, the status line, and /next."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Protocol, cast

from scripts.production import delivery_state, staff, unit_names, unrouted_alert
from scripts.production.production_units import Refusal, production_field
from scripts.production.proposals_folder import slug_of

PREFIX = "change-review - "
_MERGE_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")


class ReviewHistory(Protocol):
    """Use the existing Git parent reader and checkpoint trailer parser."""

    def read_commit_parents(self, repo: Path, revisions: Iterable[str]) -> dict[str, tuple[str, ...]]: ...
    def checkpoint_changes(self, message: str) -> tuple[str, ...]: ...


class RoutingError(ValueError):
    """The checkpoint history could not be read."""


@dataclass(frozen=True)
class StaffRoutedChange:
    unit_id: staff.StaffId
    showrunner_id: staff.StaffId
    tab: str

    @property
    def showrunner_recipients(self) -> tuple[staff.StaffId, ...]:
        return (self.showrunner_id,)


@dataclass(frozen=True)
class MissingShowrunner:
    name: str


@dataclass(frozen=True)
class ShowrunnerOwnChange:
    showrunner_id: staff.StaffId | MissingShowrunner
    tab: str

    @property
    def showrunner_recipients(self) -> tuple[staff.StaffId, ...]:
        return (self.showrunner_id,) if isinstance(self.showrunner_id, str) else ()


@dataclass(frozen=True)
class UnroutedChange:
    note: str
    tab: str = "unrouted"

    @property
    def showrunner_recipients(self) -> tuple[staff.StaffId, ...]:
        return ()


ChangeOwner = StaffRoutedChange | ShowrunnerOwnChange | UnroutedChange


@dataclass(frozen=True)
class UnavailableProduction:
    review_list: Path
    reason: str = "The production document is not available. This change cannot accept an answer."


@dataclass(frozen=True)
class ChangeAssignment:
    """The current staff owner and production document for one review row."""

    owner: ChangeOwner
    production: Path | UnavailableProduction


@dataclass(frozen=True)
class CommitNode:
    parents: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class CheckpointSelection:
    repository: Path
    commits: tuple[str, ...]


@dataclass(frozen=True)
class NoCheckpoints:
    """No checkpoint identifies a unit for this row."""


def list_documents(text: str) -> list[Path]:
    front = text.split("---", 2)
    if len(front) != 3:
        return []
    documents: list[Path] = []
    productions = False
    for line in front[1].splitlines():
        if line and not line[0].isspace():
            productions = line.strip() == "productions" + ":"
        if productions and (match := re.match(r"\s+-\s+(.+)$", line)):
            path = Path(match[1].strip('"\'')).expanduser()
            if path.is_absolute() and path.is_file():
                documents.append(path)
    return documents


def production_documents(initial: dict[str, Path], lists: Iterable[tuple[str, str]]) -> dict[str, Path]:
    """Add each list's production references without replacing initial documents."""
    documents = dict(initial)
    for owner, text in lists:
        docs = list_documents(text)
        for doc in docs:
            _ = documents.setdefault(slug_of(doc), doc)
        if docs:
            _ = documents.setdefault(owner, docs[0])
    return documents

class StaffRouting:
    """Read staff edges and commit messages once, without a live session probe."""

    def __init__(self, record: staff.StaffSnapshot, documents: dict[str, Path], history: ReviewHistory) -> None:
        self.record: staff.StaffSnapshot = record
        self.history: ReviewHistory = history
        self.runners: dict[staff.StaffId, staff.StaffRow] = {r.id: r for r in record.rows if r.kind == "showrunner"}
        self.units: tuple[staff.StaffRow, ...] = tuple(r for r in record.rows if r.kind == "unit")
        self.answer_documents: dict[str, Path] = dict(documents)
        self.documents: dict[str, Path] = dict(documents)
        self.graphs: dict[Path, dict[str, CommitNode]] = {}
        self.hashes: dict[Path, dict[str, list[str]]] = {}
        self.ancestors: dict[Path, dict[str, int]] = {}
        self.checkpoints: dict[Path, int] = {}
        self.commit_order: dict[Path, list[str]] = {}
        self.loaded_messages: dict[Path, set[str]] = defaultdict(set)
        self.unit_cache: dict[str, set[str]] = {}
        self.list_paths: dict[str, Path] = {}
        self.repositories: dict[str, Path] = {}
        self.repository_keys: dict[Path, Path] = {}
        self.selections: dict[tuple[str, str, str], CheckpointSelection | NoCheckpoints] = {}
        for runner in self.runners.values():
            if isinstance(runner.doc, (staff.LiveDoc, staff.WrappedDoc)) and runner.doc.path.is_file():
                self.documents[runner.name] = runner.doc.path
                self.documents[slug_of(runner.doc.path)] = runner.doc.path
        for doc in dict.fromkeys(self.documents.values()):
            names = [name for name, known in self.documents.items() if known == doc]
            try:
                lines = doc.read_text(encoding="utf-8").splitlines()
                repository = Path(production_field(lines, "Showrunner checkout")).expanduser()
                self.repositories.update(dict.fromkeys(names, repository))
                try:
                    review_list = Path(production_field(lines, "Change review")).expanduser()
                    self.list_paths.update(dict.fromkeys(names, review_list))
                except Refusal:
                    pass
            except (OSError, Refusal):
                continue

    def prepare(self, rows_by_list: Iterable[tuple[str, list[dict[str, str]]]]) -> None:
        """Read each repository and each required checkpoint message once."""
        sources = tuple(rows_by_list)
        repositories = {repo
                        for fallback, rows in sources for row in rows
                        if _MERGE_HASH.search(row.get("Merged", ""))
                        for repo in self.repository_choices(row, fallback)}
        if len(repositories) > 1:
            with ThreadPoolExecutor() as executor:
                _ = tuple(executor.map(self.graph, repositories))
        messages: dict[Path, set[str]] = defaultdict(set)
        for fallback, rows in sources:
            for row in rows:
                selection = self.selection(row, fallback)
                if isinstance(selection, CheckpointSelection) and len(self.candidate_units(selection)) > 1:
                    messages[selection.repository].update(selection.commits)
        if messages:
            with ThreadPoolExecutor() as executor:
                _ = tuple(executor.map(self.read_messages,
                                       (CheckpointSelection(repo, tuple(commits)) for repo, commits in messages.items())))

    def repository_choices(self, row: dict[str, str], list_owner: str) -> tuple[Path, ...]:
        """Search every staff repository without using the row's Production cell."""
        del row, list_owner
        return self.search_repositories

    @cached_property
    def search_repositories(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((*self.staff_repositories, *(
            self.repository_key(repo) for repo in self.repositories.values() if (repo / ".git").exists()))))

    @cached_property
    def staff_repositories(self) -> tuple[Path, ...]:
        """Locate Git repositories from staff paths without opening production documents."""
        locations = [unit.worktree.path for unit in self.units
                     if isinstance(unit.worktree, (staff.PresentWorktree, staff.RemovedWorktree))]
        locations.extend(runner.doc.path.parent for runner in self.runners.values()
                         if isinstance(runner.doc, (staff.LiveDoc, staff.WrappedDoc)))
        return tuple(dict.fromkeys(self.repository_key(parent) for location in locations
                                   for parent in (location, *location.parents) if (parent / ".git").exists()))

    def repository_key(self, repo: Path) -> Path:
        if repo not in self.repository_keys:
            directory = repo / ".git"
            if directory.is_file():
                value = directory.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
                directory = repo / value
            common = directory / "commondir"
            if common.is_file():
                directory /= common.read_text(encoding="utf-8").strip()
            self.repository_keys[repo] = directory.resolve()
        return self.repository_keys[repo]

    def history_key(self, repo: Path) -> str:
        marks = [(str(path.relative_to(repo)), status.st_mtime_ns, status.st_size)
                 for path, status in _history_marks(repo)]
        return hashlib.sha256(json.dumps(sorted(marks)).encode()).hexdigest()

    def history_path(self, repo: Path) -> Path:
        root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        return root / "change-review-history" / (hashlib.sha256(str(repo).encode()).hexdigest() + ".json")

    def history_records(self, repo: Path) -> dict[str, CommitNode]:
        key = self.history_key(repo)
        path = self.history_path(repo)
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if isinstance(value, dict):
                fields = cast(dict[str, object], value)
                records = fields.get("commits")
                if fields.get("key") == key and isinstance(records, dict):
                    found: dict[str, CommitNode] = {}
                    for sha, record in cast(dict[str, object], records).items():
                        if not isinstance(record, list):
                            break
                        parts = cast(list[object], record)
                        if (len(parts) != 2 or not isinstance(parts[0], list) or not isinstance(parts[1], str)
                                or not all(isinstance(parent, str) for parent in cast(list[object], parts[0]))):
                            break
                        found[sha] = CommitNode(tuple(cast(list[str], parts[0])), parts[1])
                    else:
                        return found
        except (OSError, ValueError):
            pass
        result = subprocess.run(["git", "--git-dir", str(repo), "log", "--all", "--reverse", "--topo-order",
                                 "--format=%H%x1f%s", "--"], capture_output=True, text=True, check=False)
        if result.returncode:
            return {}
        records = [line.split("\x1f", 1) for line in result.stdout.splitlines()]
        parents = self.history.read_commit_parents(repo, (record[0] for record in records))
        found = {sha: CommitNode(parents[sha], subject) for sha, subject in records}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix="." + path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    _ = stream.write(json.dumps({"key": key, "commits": {
                        sha: [node.parents, node.message] for sha, node in found.items()}}))
                    stream.close()
                    _ = temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return found

    def graph(self, repo: Path) -> dict[str, CommitNode]:
        if repo not in self.graphs:
            graph = self.history_records(repo)
            ancestors: dict[str, int] = {}
            checkpoints = 0
            order: list[str] = []
            for sha, node in graph.items():
                inherited = 0
                for parent in node.parents:
                    inherited |= ancestors.get(parent, 0)
                if node.message.startswith("checkpoint("):
                    bit = 1 << len(order)
                    order.append(sha)
                    inherited |= bit
                    checkpoints |= bit
                ancestors[sha] = inherited
            self.graphs[repo] = graph
            self.ancestors[repo] = ancestors
            self.checkpoints[repo] = checkpoints
            self.commit_order[repo] = order
            prefixes: dict[str, list[str]] = defaultdict(list)
            for sha in graph:
                prefixes[sha[:7]].append(sha)
            self.hashes[repo] = prefixes
        return self.graphs[repo]

    def checkpoint_unit(self, message: str) -> set[str]:
        match = re.match(r"checkpoint\(([^)]+)\):", message)
        if match is None:
            return set()
        name = match[1]
        if name in self.unit_cache:
            return self.unit_cache[name]
        named = self.record.by_path(name)
        if named is not None:
            if named.kind == "unit":
                self.unit_cache[name] = {str(named.id)}
                return self.unit_cache[name]
            if named.kind == "showrunner":
                self.unit_cache[name] = set()
                return self.unit_cache[name]
        matches = [unit for unit in self.units if name == unit.name or name.endswith("-" + unit.name)
                   or unit_names.stem(name) == unit_names.stem(unit.name)
                   or unit_names.stem(name).endswith("-" + unit_names.stem(unit.name))
                   or isinstance(unit.worktree, (staff.PresentWorktree, staff.RemovedWorktree))
                   and unit.worktree.path.name == name]
        longest = max((len(unit.name) for unit in matches), default=0)
        found = {str(unit.id) for unit in matches if len(unit.name) == longest} or {name}
        self.unit_cache[name] = found
        return found

    def selection(self, row: dict[str, str], fallback: str) -> CheckpointSelection | NoCheckpoints:
        key = ("", row.get("Merged", ""), fallback)
        if key in self.selections:
            return self.selections[key]
        selected: CheckpointSelection | NoCheckpoints = NoCheckpoints()
        match = _MERGE_HASH.search(row.get("Merged", ""))
        if match is not None:
            for repo in self.repository_choices(row, fallback):
                graph = self.graph(repo)
                hashes = [sha for sha in self.hashes[repo].get(match[0][:7], []) if sha.startswith(match[0])]
                if len(hashes) != 1:
                    continue
                commit = graph[hashes[0]]
                ancestors = self.ancestors[repo]
                if len(commit.parents) > 1:
                    included = ancestors.get(commit.parents[1], 0) & ~ancestors.get(commit.parents[0], 0)
                elif graph[hashes[0]].message.startswith("checkpoint("):
                    included = 1 << self.commit_order[repo].index(hashes[0])
                else:
                    included = 0
                included &= self.checkpoints[repo]
                commits: list[str] = []
                while included:
                    bit = included & -included
                    included ^= bit
                    sha = self.commit_order[repo][bit.bit_length() - 1]
                    if self.checkpoint_unit(graph[sha].message):
                        commits.append(sha)
                selected = CheckpointSelection(repo, tuple(commits))
                break
        self.selections[key] = selected
        return selected

    def read_messages(self, selection: CheckpointSelection) -> None:
        repo = selection.repository
        missing = set(selection.commits) - self.loaded_messages[repo]
        if not missing:
            return
        result = subprocess.run(["git", "--git-dir", str(repo), "log", "--no-walk", "--stdin",
                                 "--format=%x1e%H%x1f%B", "--"], input="\n".join(sorted(missing)) + "\n",
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RoutingError("cannot read checkpoint messages")
        graph = self.graphs[repo]
        for part in result.stdout.split("\x1e")[1:]:
            sha, message = part.split("\x1f", 1)
            graph[sha] = CommitNode(graph[sha].parents, message)
        self.loaded_messages[repo].update(missing)

    def candidate_units(self, selection: CheckpointSelection) -> set[str]:
        return {unit for sha in selection.commits
                for unit in self.checkpoint_unit(self.graphs[selection.repository][sha].message)}

    def owner(self, row: dict[str, str], list_owner: str) -> ChangeOwner:
        selection = self.selection(row, list_owner)
        candidates: set[str] = set()
        if isinstance(selection, CheckpointSelection):
            candidates = self.candidate_units(selection)
            if len(candidates) > 1:
                self.read_messages(selection)
                for sha in selection.commits:
                    message = self.graphs[selection.repository][sha].message
                    if row.get("Change", "") in self.history.checkpoint_changes(message):
                        candidates = self.checkpoint_unit(message)
                        break
        units = {unit.id: unit for name in candidates
                 if (unit := self.record.by_id(staff.StaffId(name)) or self.record.by_path(name)) is not None
                 and unit.kind == "unit"}
        if not units and candidates:
            for old in candidates:
                identity = self.renamed_units.get(old)
                unit = self.record.by_id(identity) if identity is not None else None
                if unit is not None and unit.kind == "unit":
                    units[unit.id] = unit
        if not units:
            runner = self.record.by_path(list_owner)
            if runner is not None and runner.kind == "showrunner":
                return ShowrunnerOwnChange(runner.id, runner.name)
            return ShowrunnerOwnChange(MissingShowrunner(list_owner), list_owner)
        parents: dict[staff.StaffId, staff.StaffRow] = {}
        for unit in units.values():
            parent = unit
            seen: set[staff.StaffId] = set()
            while parent.kind != "showrunner" and parent.parent is not None and parent.id not in seen:
                seen.add(parent.id)
                ancestor = self.record.by_id(parent.parent)
                if ancestor is None:
                    break
                parent = ancestor
            if parent.kind != "showrunner":
                return self.unrouted(list_owner, row, f"No staff showrunner for {unit.name}", candidates)
            parents[parent.id] = parent
        if len(parents) > 1:
            return self.unrouted(list_owner, row, "More than one staff unit owns this change", candidates)
        parent = next(iter(parents.values()))
        if len(units) == 1:
            return StaffRoutedChange(next(iter(units)), parent.id, parent.name)
        return ShowrunnerOwnChange(parent.id, parent.name)

    # Phase 17i reads the staff id stored on the row.
    # It changes owner, which this function, the footer and /next all call, and the document step in this function.
    def owner_and_production(self, review_list: Path, row: dict[str, str]) -> ChangeAssignment:
        """Resolve the current owner and its production through one rule."""
        list_owner = review_list.stem.removeprefix(PREFIX)
        owner = self.owner(row, list_owner)
        source_doc = self.production_for_list(review_list)
        doc = (self.documents.get(owner.tab) if isinstance(source_doc, Path)
               and owner.tab in self.repositories else None)
        return ChangeAssignment(owner, doc or source_doc)

    @cached_property
    def renamed_units(self) -> dict[str, staff.StaffId]:
        """Read unit rename journals in the existing showrunner state folder."""
        root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner").expanduser()
        found: dict[str, staff.StaffId] = {}
        for path in root.glob("*/rename_unit.json"):
            try:
                value = cast(object, json.loads(path.read_text(encoding="utf-8")))
                fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
                old, new, identity = (fields.get(key) for key in ("old", "new", "staff_id"))
                member = self.record.by_id(staff.StaffId(identity)) if isinstance(identity, str) else None
                if member is None and isinstance(new, str):
                    member = self.record.by_path(new)
                if isinstance(old, str) and member is not None:
                    found[old] = member.id
                    found[unit_names.stem(old)] = member.id
            except (OSError, ValueError):
                continue
        return found

    def unrouted(self, list_owner: str, row: dict[str, str], note: str, candidates: set[str]) -> UnroutedChange:
        print(f"staff: {note[0].lower() + note[1:]} (change {list_owner}/{row['#']})", file=sys.stderr)
        if not row.get("Verdict") and waits_for_user(self.list_paths.get(list_owner, Path(list_owner)), row, review_root()):
            names = tuple(sorted(member.name if (member := self.record.by_id(staff.StaffId(name))) else name
                                 for name in candidates))
            unrouted_alert.notify(review_root(), list_owner, row, note, names)
        return UnroutedChange(note + "; no message goes out")

    def production_for_list(self, path: Path) -> Path | UnavailableProduction:
        """Use the registered production or the list's existing production references."""
        name = path.stem.removeprefix(PREFIX)
        candidates = ([self.answer_documents[name]] if name in self.answer_documents else [])
        candidates.extend(doc for key, doc in self.answer_documents.items() if self.list_paths.get(key) == path)
        for doc in candidates:
            if any(repo_name in self.repositories and known == doc
                   for repo_name, known in self.documents.items()):
                return doc
        return UnavailableProduction(path)



def history_sources(repo: Path) -> list[Path]:
    """Use these paths for the graph key and status count invalidation."""
    return [repo / "HEAD", repo / "packed-refs", repo / "refs",
            *(path for path, _ in _ref_marks(repo / "refs"))]


def _history_marks(repo: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Read each ref's metadata once for the history key."""
    for name in ("HEAD", "packed-refs", "refs"):
        path = repo / name
        try:
            yield path, path.stat()
        except FileNotFoundError:
            continue
    yield from _ref_marks(repo / "refs")


def _ref_marks(directory: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Read paths and metadata together during one directory scan."""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    status = entry.stat()
                except FileNotFoundError:
                    continue
                path = Path(entry.path)
                yield path, status
                if entry.is_dir(follow_symlinks=False):
                    yield from _ref_marks(path)
    except FileNotFoundError:
        return


def delivery_name(resolved_list: Path, row: str) -> str:
    """Name one row record from its resolved list path."""
    identity = json.dumps([str(resolved_list), row]).encode("utf-8")
    return ".review-link-delivery-" + hashlib.sha256(identity).hexdigest()


def delivery_path(root: Path, review_list: Path, row: str) -> Path:
    return root / delivery_name(review_list.resolve(), row)


def review_root() -> Path:
    return Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()


def waits_for_user(review_list: Path, row: dict[str, str], root: Path) -> bool:
    """Use the shared typed waiting rule."""
    try:
        slots = delivery_state.decode(delivery_path(root, review_list, row["#"]).read_text(encoding="utf-8"))
    except OSError:
        slots = delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    return slots.waits_for_user(bool(row.get("Verdict")))


def waiting_rows(review_list: Path, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Scan delivery names once and open only records that exist."""
    root = review_root()
    names = {path.name for path in root.glob(".review-link-delivery-*")}
    resolved = review_list.resolve()
    empty = delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    found: list[dict[str, str]] = []
    for row in rows:
        if row.get("Verdict"):
            continue
        name = delivery_name(resolved, row["#"])
        slots = empty
        if name in names:
            try:
                slots = delivery_state.decode((root / name).read_text(encoding="utf-8"))
            except OSError:
                pass
        if slots.waits_for_user(False):
            found.append(row)
    return found
