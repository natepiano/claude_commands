"""Route changes through staff identities for the page, the status line, and /next."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import cast

from scripts.lib import conf
from scripts.production import delivery_state, staff, unrouted_alert
from scripts.production.issues import source_paths
from scripts.production.production_doc import Refusal, production_field

PREFIX = "change-review - "
PRODUCER = "Producer"


def routing_setting(file: str, key: str, *, sources: list[Path] | None = None) -> str:
    """Read a routing setting. A collector rejects recovery from an unreadable or invalid source."""
    settings = conf.read(file)
    if sources is None:
        return settings.text(key)
    sources.append(settings.source)
    result = settings.origin(key, str)
    if isinstance(result, conf.UsableLiveValue):
        return result.value
    if isinstance(result, conf.RecoveredValue) and not settings.source.exists():
        return result.value
    raise ValueError(result.reason)


def configured_lists_directory(*, sources: list[Path] | None = None) -> Path:
    return Path(routing_setting("change_review", "lists_directory", sources=sources)).expanduser()


def lists_directory(*, sources: list[Path] | None = None) -> Path:
    override = os.environ.get("CHANGE_REVIEW_DIR")
    if override:
        return Path(override)
    return configured_lists_directory() if sources is None else configured_lists_directory(sources=sources)


def _is_real_list(review_list: Path, configured_folder: Path) -> bool:
    return review_list.resolve().parent == configured_folder


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
        if docs:
            _ = documents.setdefault(owner, docs[0])
    return documents


class StaffRouting:
    """Read fixed producer identities and staff parent edges."""

    def __init__(self, record: staff.StaffSnapshot, documents: dict[str, Path], *,
                 sources: list[Path] | None = None) -> None:
        self.sources: list[Path] | None = sources
        self.record: staff.StaffSnapshot = record
        self.runners: dict[staff.StaffId, staff.StaffRow] = {
            row.id: row for row in record.rows if row.kind == "showrunner"}
        self.documents: dict[str, Path] = dict(documents)
        self.runners_by_name: dict[str, staff.StaffRow] = {row.name: row for row in self.runners.values()}
        self.production_by_runner: dict[staff.StaffId, Path] = {}
        exists: dict[Path, bool] = {}
        for runner in self.runners.values():
            candidates: list[Path] = []
            if isinstance(runner.doc, (staff.LiveDoc, staff.WrappedDoc)):
                candidates.append(runner.doc.path)
            if runner.name in self.documents:
                candidates.append(self.documents[runner.name])
            for path in candidates:
                if path not in exists:
                    exists[path] = path.is_file()
                if exists[path]:
                    self.production_by_runner[runner.id] = path
                    self.documents[runner.name] = path
                    break
        self.production_by_name: dict[str, Path] = {}
        for name, path in self.documents.items():
            if path not in exists:
                exists[path] = path.is_file()
            if exists[path]:
                self.production_by_name[name] = path

    @cached_property
    def _configured_folder(self) -> Path:
        return configured_lists_directory().resolve()

    @cached_property
    def _review_root(self) -> Path:
        return review_root()

    @cached_property
    def _root(self) -> staff.StaffRow | None:
        name = routing_setting("review_links", "unrouted_alert_recipient", sources=self.sources)
        return self.runners_by_name.get(name)

    @cached_property
    def document_lines(self) -> dict[Path, list[str]]:
        """Share one document read across list and question routing."""
        documents: dict[Path, list[str]] = {}
        for doc in dict.fromkeys(self.documents.values()):
            if self.sources is not None:
                self.sources.append(doc)
            try:
                documents[doc] = doc.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                if self.sources is not None:
                    raise
                continue
        return documents

    @cached_property
    def list_paths(self) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for doc in dict.fromkeys(self.documents.values()):
            if self.sources is not None:
                self.sources.append(doc)
            try:
                lines = self.document_lines.get(doc, [])
                listing = Path(production_field(lines, "Change review")).expanduser()
                for name, known in self.documents.items():
                    if known == doc:
                        paths[name] = listing
            except (OSError, UnicodeError):
                if self.sources is not None:
                    raise
                continue
            except Refusal:
                continue
        return paths

    def list_owner(self, review_list: Path) -> str:
        """Use the list's name, or its production reference for an older list name."""
        name = review_list.stem.removeprefix(PREFIX)
        if review_list.stem.startswith(PREFIX):
            return name
        return next((name for name, path in self.list_paths.items()
                     if path == review_list and name in self.runners_by_name), name)

    def owner(self, row: dict[str, str], review_list: Path) -> ChangeOwner:
        return self.owner_and_production(review_list, row).owner

    def _staff_owner(self, row: dict[str, str], review_list: Path) -> ChangeOwner:
        list_owner = self.list_owner(review_list)
        identity = row.get(PRODUCER, "").strip()
        recorded_owner = row.get("Production", "").strip()
        if not identity and not row.get("Verdict") and self.record.by_id(staff.StaffId(recorded_owner)) is not None:
            identity = recorded_owner
        if not identity:
            runner = self.runners_by_name.get(list_owner)
            if runner is not None and runner.kind == "showrunner":
                return ShowrunnerOwnChange(runner.id, runner.name)
            return ShowrunnerOwnChange(MissingShowrunner(list_owner), list_owner)
        producer = self.record.by_id(staff.StaffId(identity))
        if producer is None:
            return self.unrouted(review_list, row, f"No staff record for {identity}", identity)
        if producer.kind == "showrunner":
            return ShowrunnerOwnChange(producer.id, producer.name)
        if producer.kind != "unit":
            return self.unrouted(review_list, row, f"Producer {identity} is not a staff unit or showrunner", identity)
        parent = producer
        seen: set[staff.StaffId] = set()
        while parent.kind != "showrunner" and isinstance(parent.parent, staff.ReportsTo) and parent.id not in seen:
            seen.add(parent.id)
            ancestor = self.record.by_id(parent.parent.id)
            if ancestor is None:
                break
            parent = ancestor
        if parent.kind != "showrunner":
            return self.unrouted(review_list, row, f"No staff showrunner for {producer.name}", identity)
        return StaffRoutedChange(producer.id, parent.id, parent.name)

    def owner_and_production(self, review_list: Path, row: dict[str, str]) -> ChangeAssignment:
        """Read the producer and its showrunner's current production document."""
        name = self.list_owner(review_list)
        owner = self._staff_owner(row, review_list)
        if isinstance(owner, UnroutedChange) or isinstance(owner.showrunner_id, MissingShowrunner):
            runner = self.runners_by_name.get(name)
            document = self.production_by_runner.get(runner.id) if runner is not None else self.production_by_name.get(name)
        else:
            document = self.production_by_runner.get(owner.showrunner_id)
        missing_owner = isinstance(owner, ShowrunnerOwnChange) and isinstance(owner.showrunner_id, MissingShowrunner)
        if missing_owner or document is None:
            root = self._root
            if root is not None:
                root_document = document if document is not None else self.production_by_runner.get(root.id)
                if root_document is not None:
                    return ChangeAssignment(owner if isinstance(owner, UnroutedChange)
                                            else ShowrunnerOwnChange(root.id, root.name), root_document)
        return ChangeAssignment(owner, document if document is not None else UnavailableProduction(review_list))

    def unrouted(self, review_list: Path, row: dict[str, str], note: str, producer: str) -> UnroutedChange:
        list_owner = self.list_owner(review_list)
        print(f"staff: {note[0].lower() + note[1:]} (change {list_owner}/{row['#']})", file=sys.stderr)
        if self.sources is None and not row.get("Verdict") and _is_real_list(review_list, self._configured_folder) and self.record.production:
            root = self._review_root
            if not waits_for_user(review_list, row, root):
                return UnroutedChange(note + "; no message goes out")
            member = self.record.by_id(staff.StaffId(producer))
            names = (member.name if member is not None else producer,)
            unrouted_alert.notify(root, list_owner, row, note, names)
        return UnroutedChange(note + "; no message goes out")


def delivery_name(resolved_list: Path, row: str) -> str:
    """Name one row record from its resolved list path."""
    identity = json.dumps([str(resolved_list), row]).encode("utf-8")
    return ".review-link-delivery-" + hashlib.sha256(identity).hexdigest()


def delivery_path(root: Path, review_list: Path, row: str) -> Path:
    return root / delivery_name(review_list.resolve(), row)


def review_root(*, sources: list[Path] | None = None) -> Path:
    return Path(os.environ.get("REVIEW_LINKS_ROOT") or routing_setting(
        "change_review", "review_links_directory", sources=sources)).expanduser()


def delivery_slots(path: Path, *, sources: list[Path] | None = None) -> delivery_state.DeliverySlots:
    """Read one delivery record. A collector requires valid JSON and complete reads."""
    if sources is not None:
        sources.append(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    except OSError:
        if sources is not None:
            raise
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    return (delivery_state.decode_value(cast(object, json.loads(text))) if sources is not None
            else delivery_state.decode(text))


def waits_for_user(review_list: Path, row: dict[str, str], root: Path, *,
                   sources: list[Path] | None = None) -> bool:
    """Use the shared typed waiting rule."""
    slots = delivery_slots(delivery_path(root, review_list, row["#"]), sources=sources)
    return slots.waits_for_user(bool(row.get("Verdict")))


def waiting_rows(review_list: Path, rows: list[dict[str, str]],
                 names: set[str] | None = None, *, sources: list[Path] | None = None) -> list[dict[str, str]]:
    """Scan delivery names once and open only records that exist."""
    root = review_root(sources=sources)
    if names is None:
        names = {path.name for path in source_paths(root, ".review-link-delivery-*", sources=sources)}
    resolved = review_list.resolve()
    empty = delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    found: list[dict[str, str]] = []
    for row in rows:
        if row.get("Verdict"):
            continue
        name = delivery_name(resolved, row["#"])
        slots = empty
        if name in names:
            slots = delivery_slots(root / name, sources=sources)
        if slots.waits_for_user(False):
            found.append(row)
    return found
