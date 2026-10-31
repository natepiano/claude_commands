"""Read the showrunner reporting tree from one staff snapshot."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production import staff, staff_sources
from scripts.production import showrunners


def parent_edges(*, record: staff.StaffSnapshot | None = None) -> dict[str, str]:
    """Join showrunner parent ids from one snapshot to their production slugs."""
    record = staff.snapshot() if record is None else record
    members = staff_sources.showrunner_members(record)
    names = {row.id: row.name for row in record.find(kind="showrunner")}
    names.update({row.id: slug for slug, row in members.items()})
    return {names[row.id]: names[row.parent] for row in record.find(kind="showrunner")
            if row.parent is not None and row.parent in names}


def edges(slugs: set[str] | None = None, *, record: staff.StaffSnapshot | None = None) -> dict[str, str]:
    """Each recorded showrunner's parent, optionally restricted to the displayed slugs."""
    tree = parent_edges(record=record)
    return tree if slugs is None else {child: parent for child, parent in tree.items()
                                      if child in slugs and parent in slugs}


def parent_of(slug: str, *, record: staff.StaffSnapshot | None = None) -> str:
    """The production `slug` reports to. Empty for a root."""
    return edges(record=record).get(slug, "")


def children_of(slug: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """The productions that report to `slug`, one level down."""
    return sorted(child for child, parent in edges(record=record).items() if parent == slug)


def _below(slug: str, tree: dict[str, str]) -> list[str]:
    level = [slug]
    found: list[str] = []
    while level:
        level = sorted(child for child, parent in tree.items() if parent in level)
        found.extend(level)
    return found


def descendants_of(slug: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Every production below `slug`, at any depth, the nearest level first."""
    return _below(slug, edges(record=record))


def _root_in(slug: str, tree: dict[str, str]) -> str:
    while slug in tree:
        slug = tree[slug]
    return slug


def user_facing(slug: str, below: bool = False, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """The productions whose items for the user `slug`'s showrunner counts and lists.

    Each showrunner answers for its own production only: the showrunner above it and the
    showrunners below it answer for theirs (user, 2026-10-27). `below` adds every production
    below it, at any depth (`/next all-below`). A production above or beside it never counts.
    """
    return [slug, *descendants_of(slug, record=record)] if below else [slug]


def root_of(slug: str, *, record: staff.StaffSnapshot | None = None) -> str:
    """The top of the tree `slug` is in, the showrunner that shows the user everything below it. A root is its own."""
    return _root_in(slug, edges(record=record))


def highest_running(runners: list[showrunners.Showrunner], *, record: staff.StaffSnapshot | None = None) -> list[showrunners.Showrunner]:
    """Each running showrunner with no running showrunner above it.

    These are the roots, and below a root that is down, the showrunners nearest it. What every
    production must hear once, a machine-wide fault, goes to these.
    """
    tree = edges(record=record)
    running = {runner["slug"] for runner in runners if runner["socket"]}

    def covered(slug: str) -> bool:
        while slug in tree:
            slug = tree[slug]
            if slug in running:
                return True
        return False

    return [runner for runner in runners if runner["socket"] and not covered(runner["slug"])]


def tree_lines(runners: list[showrunners.Showrunner], *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """One line per showrunner, each child indented under its parent."""
    tree = edges({runner["slug"] for runner in runners}, record=record)
    names = {runner["slug"]: runner["session"] or "<not running>" for runner in runners}
    lines: list[str] = []

    def add(slug: str, depth: int) -> None:
        lines.append(f"{'  ' * depth}{slug}\t{names[slug]}")
        for child in sorted(child for child, parent in tree.items() if parent == slug):
            add(child, depth + 1)

    for slug in sorted(names):
        if slug not in tree:
            add(slug, 0)
    return lines


class ParentCycle(ValueError):
    """The requested source edge would make a showrunner its own ancestor."""


def check_attach(child: str, parent: str, *, record: staff.StaffSnapshot | None = None) -> None:
    """Check the source-side attach rules before either record changes."""
    child = showrunners.canonical_slug(child)
    parent = showrunners.canonical_slug(parent)
    known = showrunners.timer_slugs()
    for slug in (child, parent):
        if slug not in known:
            raise ValueError(f"{slug} has no update timer, so it has no showrunner to attach")
    if child == parent:
        raise ValueError(f"{child} cannot report to itself")
    for runner in showrunners.registered_showrunners():
        if runner["slug"] in (child, parent) and runner["socket"] and (why := showrunners.name_refusal(runner["slug"], runner["session"])):
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


def stored_children(slug: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Every child in the staff record, including productions with no local timer."""
    return children_of(slug, record=record)


def edges_to_release(slug: str, *, record: staff.StaffSnapshot | None = None) -> WrapRelease:
    """Capture recorded edges before wrap retires any row."""
    tree = edges(record=record)
    parent = tree.get(slug, "")
    children = tuple(sorted(child for child, above in tree.items() if above == slug))
    return WrapRelease(ReportedTo(parent) if parent else Root(), children, ())


def release_edges(slug: str, released: WrapRelease, *, record: staff.StaffSnapshot | None = None) -> None:
    """Remove captured legacy files unless the record has a subsequent attachment."""
    tree = edges(record=record)
    captured = [(child, slug) for child in released.former_children]
    if isinstance(released.former_parent, ReportedTo):
        captured.insert(0, (slug, released.former_parent.parent))
    for child, parent in captured:
        current = tree.get(child, "")
        if not current or current == parent:
            (showrunners.SHOWRUNNER_STATE_DIR / child / showrunners.PARENT_FILE).unlink(missing_ok=True)
        else:
            print(f"showrunners: keep {child} -> {current}; captured parent was {parent}", file=sys.stderr)


def main(action: str, slug: str) -> int:
    """Answer the reporting-tree verbs parsed by the showrunner command."""
    if action == "tree":
        for line in tree_lines(showrunners.registered_showrunners()):
            print(line)
    else:
        slug = showrunners.canonical_slug(slug)
        if slug not in showrunners.timer_slugs():
            raise ValueError(f"{slug} has no update timer")
        # A root prints no parent and no children: an empty answer is the answer.
        answers = (children_of(slug) if action == "children"
                   else [parent_of(slug) if action == "parent" else root_of(slug)])
        for answer in answers:
            if answer:
                print(answer)
    return 0

