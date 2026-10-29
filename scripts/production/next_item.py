#!/usr/bin/env python3
"""The one item /next shows from the four lists the user works through, and moving an item to the end.

    next_item.py show [--production DOC] [--kind change|proposal|efficiency|question] [--order urgent|oldest|newest] [--all-below]
    next_item.py move-to-end --kind change|proposal|efficiency|question [--production DOC] [--all-below]

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

import argparse
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar, cast

import change_review
import proposals_folder
import proposals_table
import showrunners
from footer_counts import efficiency_ledger
from production_units import read_production
from production_units import Refusal
from proposals_folder import slug_of
from update_registration import ALL_BELOW, RegistrationFailure, open_lists, question_queue, scope_queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import urgency  # noqa: E402
from user_zone import user_zone  # noqa: E402

if TYPE_CHECKING:
    from ..nightly_review import candidates
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nightly_review"))
    import candidates

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


def change_queue(doc: Path, below: bool = False) -> Queue:
    """The production's current and earlier change rows, and with `below` each lower one's."""
    path = change_review.scope_list(doc)
    entries = change_review.scope_entries(path, doc, below)
    # The root keeps the list's own pin; a lower showrunner pins its own row.
    slug = proposals_folder.slug_of(doc)
    name = change_review.queue(path) if slug == showrunners.root_of(slug) else f"{change_review.queue(path)}-{slug}"
    return Queue(scope_queue(name, below), _as_text(entries,
                                                lambda entry: change_review.describe(entry.item)))


def efficiency_queue(doc: Path, below: bool = False) -> Queue:
    """The open candidates of the production's Efficiency ledger, and with `below` of each one below it."""
    ledgers = dict.fromkeys(ledger for each in proposals_folder.scope_docs(doc, below)
                            if (ledger := efficiency_ledger(each)) is not None)
    lines = [line for ledger in ledgers for line in candidates.read_lines(ledger)]
    return Queue(scope_queue(candidates.QUEUE, below), _as_text(candidates.entries(lines), lambda entry: entry.item.line))


def read_queue(kind: str, production: Path | None, below: bool = False) -> Queue:
    """The waiting items of one list."""
    try:
        match kind:
            case "change":
                return change_queue(_doc(production), below)
            case "proposal":
                doc = _doc(production)
                waiting, _ = proposals_table.waiting_entries(doc, below)
                return Queue(scope_queue(proposals_table.queue(doc), below), _as_text(waiting, proposals_table.proposal_text))
            case "question":
                found = read_production(_doc(production))
                due = open_lists(found, below).due
                return Queue(question_queue(found, below), _as_text(due, lambda entry: entry.item.line()))
            case _:
                return efficiency_queue(_doc(production), below)
    except (change_review.ChangeReviewError, RegistrationFailure, Refusal, OSError, ValueError) as error:
        raise NextError(f"{kind}: {error}") from None


def show(kind: str, order: str, production: Path | None, below: bool = False) -> str:
    """The one item /next shows, led by its kind; it is pinned."""
    if kind:
        queue = read_queue(kind, production, below)
        shown = urgency.top(queue.entries, queue.name, order)
        return f"{kind}\n{shown.item}" if shown is not None else f"no {kind} is waiting for you"
    doc = _doc(production)
    queues: dict[str, list[urgency.Entry[str]]] = {}
    kinds: dict[str, str] = {}
    for each in KINDS:
        try:
            queue = read_queue(each, doc, below)
        except NextError as error:
            print(f"next_item: {error}", file=sys.stderr)
            continue
        queues[queue.name] = queue.entries
        kinds[queue.name] = each
    found = urgency.top_across(queues, scope_queue(f"next-{slug_of(doc)}", below), order)
    if found is None:
        return "nothing is waiting for you"
    name, shown = found
    return f"{kinds[name]}\n{shown.item}"


def move_to_end(kind: str, production: Path | None, below: bool = False) -> str:
    """Move the item /next last showed from the `kind` list to its end."""
    queue = read_queue(kind, production, below)
    key = urgency.pinned(queue.name)
    if not key or key not in {entry.key for entry in queue.entries}:
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
        _ = action.add_argument("--production", type=Path)
        _ = action.add_argument(f"--{ALL_BELOW}", action="store_true", dest="below")
    args = parser.parse_args(arguments)
    production = cast(Path | None, args.production)
    below = cast(bool, args.below)
    try:
        if cast(str, args.action) == "show":
            print(show(cast(str, args.kind), cast(str, args.order), production, below))
        else:
            print(move_to_end(cast(str, args.kind), production, below))
        return 0
    except NextError as error:
        print(f"next_item: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
