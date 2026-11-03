"""Fill historical Producer cells once. Remove this module after the lists are filled."""
from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.production import change_review, change_routing, staff
from scripts.production.proposals_folder import owner_docs
from production_units import Refusal
import unit_names
from scripts.shutdown import remote


OLD_CHECKPOINT_PREFIX = "build-followups-"


@dataclass(frozen=True)
class UnmappedProducer:
    name: str


@dataclass(frozen=True)
class MergeCheckpoints:
    included: tuple[change_review.Commit, ...]
    own_name: str


class ProducerBackfillLists:
    """Use the list's locked editor and keep one backup before changing cells."""

    def read(self, path: Path) -> change_review.ReviewListSnapshot:
        return change_review.review_snapshot(path)

    def write(self, path: Path, filled: dict[str, staff.StaffId], production: Path) -> dict[str, staff.StaffId]:
        changed: dict[str, staff.StaffId] = {}

        def fill(lines: list[str]) -> None:
            sections = [change_review.SECTION]
            if any(line.strip() == change_review.REVIEWED for line in lines):
                sections.append(change_review.REVIEWED)
            if not filled:
                return
            values = {row: str(identity) for row, identity in filled.items()}
            for section in sections:
                rows = change_review._fill_column(path, lines, section, change_review.PRODUCER, values)  # pyright: ignore[reportPrivateUsage]
                changed.update((row, filled[row]) for row in rows)
            if changed:
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
                _ = shutil.copy2(path, path.with_name(path.name + ".backup-" + stamp))

        change_review._edit(path, production, change_review.today(), fill,  # pyright: ignore[reportPrivateUsage]
                            lambda _: f"producer cells for {len(changed)} rows")
        return changed


class ProducerHistory:
    """Read checkpoint history only for the one-time backfill."""

    def __init__(self, documents: Iterable[Path], record: staff.StaffSnapshot) -> None:
        self.tree: change_review.Tree = change_review.Tree(dict.fromkeys(
            nested for doc in documents for nested in owner_docs(doc)))
        self.record: staff.StaffSnapshot = record
        self.machine: str = remote.local_machine()
        self.commits: dict[str, MergeCheckpoints] = {}

    def checkpoints(self, merge: str) -> MergeCheckpoints:
        """Read each merge once across the production repositories."""
        if merge not in self.commits:
            repo = change_review._found_repo(self.tree.repos, merge)  # pyright: ignore[reportPrivateUsage]
            if repo is None:
                self.commits[merge] = MergeCheckpoints((), "")
            else:
                subject = change_review._git(repo, "show", "-s", "--format=%s", merge).strip()  # pyright: ignore[reportPrivateUsage]
                own = change_review._CHECKPOINT.match(subject)  # pyright: ignore[reportPrivateUsage]
                self.commits[merge] = MergeCheckpoints(tuple(change_review.merge_commits(repo, merge)),
                                                      own["branch"] if own is not None else "")
        return self.commits[merge]

    def producer(self, row: dict[str, str]) -> staff.StaffId | UnmappedProducer:
        match = change_review._HASH.search(row.get("Merged", ""))  # pyright: ignore[reportPrivateUsage]
        evidence = self.checkpoints(match[0]) if match is not None else MergeCheckpoints((), "")
        names = change_review.matching_checkpoint_names(list(evidence.included), row.get("Change", ""))
        if not names:
            names = (evidence.own_name,) if evidence.own_name else ()
        if len(names) != 1:
            return UnmappedProducer(", ".join(names) or "no checkpoint found")
        source = self.tree.branches.get(names[0])
        producer = (change_review.producer_for(source, self.record, self.machine)
                    if source is not None else change_review.UnknownProducer())
        if not isinstance(producer, change_review.UnknownProducer):
            return producer
        name = names[0]
        if name.startswith(OLD_CHECKPOINT_PREFIX):
            unit_name = name.removeprefix(OLD_CHECKPOINT_PREFIX)
            try:
                unit_name = unit_names.name(unit_name)
            except unit_names.BadName:
                return UnmappedProducer(name)
            matches = [row.id for row in self.record.rows if row.kind == "unit" and row.name == unit_name]
            if len(matches) == 1:
                return matches[0]
        return UnmappedProducer(name)


@dataclass(frozen=True)
class BackfillResult:
    filled: dict[str, staff.StaffId]
    unmapped: dict[str, str]

    def report(self, path: Path, write: bool) -> None:
        """Print the result of one list's backfill."""
        print(f"{path.name}: {len(self.filled)} {'filled' if write else 'to fill'}, {len(self.unmapped)} unmapped")
        if not self.filled:
            print(f"{path.name}: no change")
        for row, identity in self.filled.items():
            print(f"{path.name}/{row}: {identity}")
        for row, name in self.unmapped.items():
            print(f"{path.name}/{row}: {name}; Producer stays empty")


def run(path: Path, documents: Iterable[Path], record: staff.StaffSnapshot, write: bool) -> BackfillResult:
    """Report empty Producer cells and optionally fill them under the list lock."""
    lists = ProducerBackfillLists()
    snapshot = lists.read(path)
    history = ProducerHistory((*documents, *change_routing.list_documents(snapshot.text)), record)
    filled: dict[str, staff.StaffId] = {}
    unmapped: dict[str, str] = {}
    for row in (*snapshot.pending, *snapshot.reviewed):
        if row.get(change_review.PRODUCER) or change_review._HASH.search(row.get("Merged", "")) is None:  # pyright: ignore[reportPrivateUsage]
            continue
        producer = history.producer(row)
        if isinstance(producer, UnmappedProducer):
            unmapped[row["#"]] = producer.name
        else:
            filled[row["#"]] = producer
    changed = lists.write(path, filled, next(iter(history.tree.docs), path)) if write else {}
    return BackfillResult(changed if write else filled, unmapped)


def run_lists(paths: list[Path], documents: list[Path], record: staff.StaffSnapshot, write: bool) -> int:
    """Report each list and continue after a list error."""
    failed = False
    for path in paths:
        try:
            run(path, documents, record, write).report(path, write)
        except (change_review.ChangeReviewError, OSError, Refusal, UnicodeDecodeError) as error:
            if isinstance(error, staff.StaffUnreadable):
                raise
            print(f"change review: {path.name}: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0
