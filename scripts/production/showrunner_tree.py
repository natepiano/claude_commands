"""Read the showrunner reporting tree from one staff snapshot."""
from __future__ import annotations

from dataclasses import dataclass


from scripts.production import staff
from scripts.production import showrunners


def parent_edges(*, record: staff.StaffSnapshot | None = None) -> dict[str, str]:
    """Map showrunner parent ids to production names from one snapshot."""
    record = staff.snapshot() if record is None else record
    names = {row.id: row.name for row in staff.active(record, "showrunner")}
    return {names[row.id]: names[row.parent.id] for row in staff.active(record, "showrunner")
            if isinstance(row.parent, staff.ReportsTo) and row.parent.id in names}


def edges(production_names: set[str] | None = None, *, record: staff.StaffSnapshot | None = None) -> dict[str, str]:
    """Return each showrunner's parent, optionally limited to the displayed production names."""
    tree = parent_edges(record=record)
    return tree if production_names is None else {child: parent for child, parent in tree.items()
                                      if child in production_names and parent in production_names}


def parent_of(production_name: str, *, record: staff.StaffSnapshot | None = None) -> str:
    """The production `production_name` reports to. Empty for a root."""
    return edges(record=record).get(production_name, "")


def children_of(production_name: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """The productions that report to `production_name`, one level down."""
    return sorted(child for child, parent in edges(record=record).items() if parent == production_name)


def _below(production_name: str, tree: dict[str, str]) -> list[str]:
    level = [production_name]
    found: list[str] = []
    while level:
        level = sorted(child for child, parent in tree.items() if parent in level)
        found.extend(level)
    return found


def descendants_of(production_name: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Every production below `production_name`, at any depth, the nearest level first."""
    return _below(production_name, edges(record=record))


def _root_in(production_name: str, tree: dict[str, str]) -> str:
    while production_name in tree:
        production_name = tree[production_name]
    return production_name


def user_facing(production_name: str, below: bool = False, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """The productions whose items for the user `production_name`'s showrunner counts and lists.

    Each showrunner answers for its own production only: the showrunner above it and the
    showrunners below it answer for theirs (user, 2026-10-27). `below` adds every production
    below it, at any depth (`/next all-below`). A production above or beside it never counts.
    """
    return [production_name, *descendants_of(production_name, record=record)] if below else [production_name]


def root_of(production_name: str, *, record: staff.StaffSnapshot | None = None) -> str:
    """The top of the tree `production_name` is in, the showrunner that shows the user everything below it. A root is its own."""
    return _root_in(production_name, edges(record=record))


def highest_running(runners: list[showrunners.Showrunner], *, record: staff.StaffSnapshot | None = None) -> list[showrunners.Showrunner]:
    """Each running showrunner with no running showrunner above it.

    These are the roots, and below a root that is down, the showrunners nearest it. What every
    production must hear once, a machine-wide fault, goes to these.
    """
    tree = edges(record=record)
    running = {runner["name"] for runner in runners if runner["socket"]}

    def covered(production_name: str) -> bool:
        while production_name in tree:
            production_name = tree[production_name]
            if production_name in running:
                return True
        return False

    return [runner for runner in runners if runner["socket"] and not covered(runner["name"])]


def tree_lines(runners: list[showrunners.Showrunner], *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """One line per showrunner, each child indented under its parent."""
    tree = edges({runner["name"] for runner in runners}, record=record)
    names = {runner["name"]: runner["session"] or "<not running>" for runner in runners}
    lines: list[str] = []

    def add(production_name: str, depth: int) -> None:
        lines.append(f"{'  ' * depth}{production_name}\t{names[production_name]}")
        for child in sorted(child for child, parent in tree.items() if parent == production_name):
            add(child, depth + 1)

    for production_name in sorted(names):
        if production_name not in tree:
            add(production_name, 0)
    return lines


class ParentCycle(ValueError):
    """The requested source edge would make a showrunner its own ancestor."""


def check_attach(child: str, parent: str, *, record: staff.StaffSnapshot | None = None) -> None:
    """Check the source-side attach rules before either record changes."""
    known = showrunners.timer_names()
    for production_name in (child, parent):
        if production_name not in known:
            raise ValueError(f"{production_name} has no update timer, so it has no showrunner to attach")
    if child == parent:
        raise ValueError(f"{child} cannot report to itself")
    for runner in showrunners.require_complete(showrunners.registered_showrunners()):
        if runner["name"] in (child, parent) and runner["socket"] and (why := showrunners.name_refusal(runner["name"], runner["session"])):
            raise ValueError(why)
    tree = edges(record=record)
    if child in tree and tree[child] != parent:
        raise ValueError(f"{child} already reports to {tree[child]}; detach it first")
    above = parent
    while above in tree:
        above = tree[above]
        if above == child:
            raise ParentCycle(f"{parent} already reports to {child}, directly or through others")


@dataclass(frozen=True)
class ReportedTo:
    parent: str


@dataclass(frozen=True)
class Root:
    """The production had no parent when wrap captured its edges."""


@dataclass(frozen=True)
class WrapRelease:
    former_parent: ReportedTo | Root
    former_children: tuple[str, ...]
    told: tuple[str, ...]


def stored_children(production_name: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Every child in the staff record, including productions with no local timer."""
    return children_of(production_name, record=record)


def edges_to_release(production_name: str, *, record: staff.StaffSnapshot | None = None) -> WrapRelease:
    """Capture recorded edges before wrap retires any row."""
    tree = edges(record=record)
    parent = tree.get(production_name, "")
    children = tuple(sorted(child for child, above in tree.items() if above == production_name))
    return WrapRelease(ReportedTo(parent) if parent else Root(), children, ())


def main(action: str, production_name: str) -> int:
    """Answer the reporting-tree verbs parsed by the showrunner command."""
    if action == "tree":
        runners = showrunners.registered_showrunners()
        for line in tree_lines(runners):
            print(line)
        _ = showrunners.require_complete(runners)
    else:
        if production_name not in showrunners.timer_names():
            raise ValueError(f"{production_name} has no update timer")
        # A root prints no parent and no children: an empty answer is the answer.
        answers = (children_of(production_name) if action == "children"
                   else [parent_of(production_name) if action == "parent" else root_of(production_name)])
        for answer in answers:
            if answer:
                print(answer)
    return 0

