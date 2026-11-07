#!/usr/bin/env python3
"""The one item /next shows from the four lists the user works through, and moving an item to the end.

    next_item.py show [--production NAME] [--kind change|proposal|efficiency|question] [--order urgent|oldest|newest] [--all-below]
    next_item.py move-to-end --kind change|proposal|efficiency|question [--production NAME] [--all-below]

`show` prints the item's kind on the first line, then the item as its list prints it, and pins it
(urgency.py). With a kind it takes that list's first item; with none, the first across the four
lists (`urgency.top_across`). The order is urgency.py's: `urgent` (the default) or `oldest` or
`newest`. It prints one item and never the one after it.

`move-to-end` moves the item /next last showed from that list to the end of the list, so the next
`show` takes another item. It shows nothing after it.

Every kind needs the production: --production, else the calling showrunner's. Each kind shows the
items of that production only: never of the production above it, and not of the productions below
it, whose showrunners show their own (user, 2026-10-27). `--all-below` adds every production below
it, at any depth, and pins under its own names, so it never moves the item the default shows. A
change row belongs to the production its Production cell names, and a row without one to its
list's production. Earlier lists stay in the queue after attachment. Current-list rows keep their
IDs and pins; other rows name their list in their IDs. Efficiency reads the Efficiency ledger line of each of those
docs; a production with none has no efficiency items. A list that cannot be read is left out, with
one line on stderr.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar, cast

from scripts.production import change_review
from scripts.production import proposals_folder
from scripts.production import proposals_table
from scripts.production.footer_counts import efficiency_ledger
from scripts.production.store_owner import document_id
from scripts.production import staff
from scripts.production.production_doc import Refusal, read_production
from scripts.production.update_registration import QuestionStaff, RegistrationFailure, open_lists, question_queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from scripts.lib import urgency  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

if TYPE_CHECKING:
    from ..nightly_review import candidates
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nightly_review"))
    from scripts.nightly_review import candidates

CHANGE = "change"
PROPOSAL = "proposal"
EFFICIENCY = "efficiency"
QUESTION = "question"
# The order a tie across lists keeps.
KINDS = (CHANGE, PROPOSAL, EFFICIENCY, QUESTION)
Item = TypeVar("Item")


class NextError(ValueError):
    """A list that cannot be read, or nothing to move."""


class Queue(NamedTuple):
    """One list: the name urgency.py pins it under, and its waiting items as the text /next prints."""

    name: str
    entries: list[urgency.Entry[str]]


def _as_text(entries: Iterable[urgency.Entry[Item]], text: Callable[[urgency.Entry[Item]], str]) -> list[urgency.Entry[str]]:
    return [urgency.Entry(entry.key, text(entry), entry.urgency, entry.since, entry.value, entry.after)
            for entry in entries]


def _doc(production: Path | None) -> Path:
    if production is None:
        return change_review.production_doc("")
    return production


@dataclass(frozen=True)
class ReadChangeLists:
    """Read the current lists when no request snapshot exists."""


READ_CHANGE_LISTS = ReadChangeLists()


def change_queue(doc: Path, below: bool = False, *,
                 prepared: change_review.ChangeQueue | ReadChangeLists = READ_CHANGE_LISTS) -> Queue:
    """The production's current and earlier change rows, and with `below` each lower one's."""
    queue = (change_review.change_queue(change_review.scope_list(doc), doc, below)
             if isinstance(prepared, ReadChangeLists) else prepared)
    return Queue(queue.name, _as_text(queue.entries, lambda entry: change_review.describe(entry.item)))


def efficiency_queue(doc: Path, below: bool = False) -> Queue:
    """The open candidates of the production's Efficiency ledger, and with `below` of each one below it."""
    ledgers = dict.fromkeys(ledger for each in proposals_folder.scope_docs(doc, below)
                            if (ledger := efficiency_ledger(each)) is not None)
    lines = [line for ledger in ledgers for line in candidates.read_lines(ledger)]
    return Queue(urgency.scope_queue(candidates.QUEUE, below), _as_text(candidates.entries(lines), lambda entry: entry.item.line))


def read_queue(kind: str, production: Path | None, below: bool = False, *,
               record: staff.StaffSnapshot | None = None) -> Queue:
    """The waiting items of one list."""
    try:
        match kind:
            case "change":
                return change_queue(_doc(production), below)
            case "proposal":
                doc = _doc(production)
                waiting, _ = proposals_table.waiting_entries(doc, below)
                return Queue(urgency.scope_queue(proposals_table.queue(doc, record=record), below), _as_text(waiting, proposals_table.proposal_text))
            case "question":
                doc = _doc(production)
                identities = QuestionStaff.read() if record is None else QuestionStaff.for_record(record)
                owner = identities.owner(doc)
                found = read_production(owner)
                due = open_lists(found, below, identities=identities).due
                return Queue(question_queue(found, below), _as_text(due, lambda entry: entry.item.line()))
            case _:
                return efficiency_queue(_doc(production), below)
    except (change_review.ChangeReviewError, RegistrationFailure, Refusal, OSError, ValueError) as error:
        raise NextError(f"{kind}: {error}") from None


def show(kind: str, order: str, production: Path | None, below: bool = False) -> str:
    """The one item /next shows, led by its kind; it is pinned."""
    record = None if kind == EFFICIENCY else staff.snapshot()
    if kind:
        queue = read_queue(kind, production, below, record=record)
        shown = urgency.top(queue.entries, queue.name, order)
        return f"{kind}\n{shown.item}" if shown is not None else f"no {kind} is waiting for you"
    doc = _doc(production)
    queues: dict[str, list[urgency.Entry[str]]] = {}
    kinds: dict[str, str] = {}
    for each in KINDS:
        try:
            queue = read_queue(each, doc, below, record=record)
        except NextError as error:
            print(f"next_item: {error}", file=sys.stderr)
            continue
        queues[queue.name] = queue.entries
        kinds[queue.name] = each
    found = urgency.top_across(queues, urgency.scope_queue(f"next-{document_id(doc, record=record)}", below), order)
    if found is None:
        return "nothing is waiting for you"
    name, shown = found
    return f"{kinds[name]}\n{shown.item}"


def move_to_end(kind: str, production: Path | None, below: bool = False, *, key: str = "",
                prepared: change_review.ChangeQueue | ReadChangeLists = READ_CHANGE_LISTS) -> str:
    """Move the given key, or the item /next last showed, to the list's end."""
    queue = (change_queue(_doc(production), below, prepared=prepared)
             if kind == CHANGE and not isinstance(prepared, ReadChangeLists)
             else read_queue(kind, production, below))
    present = {entry.key for entry in queue.entries}
    if key and key not in present:
        raise NextError(f"no {kind} with key {key} is waiting")
    key = key or urgency.pinned(queue.name)
    if not key or key not in present:
        raise NextError(f"no {kind} that /next showed is waiting; show one first")
    urgency.move_to_end(queue.name, key, datetime.now(user_zone()).replace(tzinfo=None))
    return f"{kind}: moved {key} to the end"


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    shown = actions.add_parser("show")
    _ = shown.add_argument("--kind", choices=KINDS, default="")
    _ = shown.add_argument("--order", choices=urgency.ORDERS, default=urgency.URGENT)
    moved = actions.add_parser("move-to-end")
    _ = moved.add_argument("--kind", choices=KINDS, required=True)
    for action in (shown, moved):
        _ = action.add_argument("--production", default="")
        _ = action.add_argument(f"--{urgency.ALL_BELOW}", action="store_true", dest="below")
    args = parser.parse_args(arguments)
    production_name = cast(str, args.production)
    below = cast(bool, args.below)
    try:
        production = change_review.production_doc(production_name) if production_name else None
        if cast(str, args.action) == "show":
            print(show(cast(str, args.kind), cast(str, args.order), production, below))
        else:
            print(move_to_end(cast(str, args.kind), production, below))
        return 0
    except (NextError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"next_item: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
