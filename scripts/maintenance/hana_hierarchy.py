#!/usr/bin/env python3
"""Write the vault note "hana hierarchy.md": the hana showrunner's tree as a Mermaid diagram.

An org chart, top down. Each box reads name -- role: the showrunner, its unit
directors, and the Codex workers each director launched, with the session folder
from home on the showrunner and director boxes and the state on each worker box,
in words and in color. A link to the production file sits above the diagram, and
a table counts each director's workers. The layout is the contract the
hanadocs-info diagrams unit wrote (productions/hanadocs-info-diagrams.md, Phase 2
and its Generator spec), so the hand-written note and this one read the same.

Sources, and nothing else: `roster.py --here --json`, which already joins the
showrunners, the staff rows and each unit's live mesh_roster.json, and
`showrunners.py list` and `children <production>` for the production file and
the showrunners below hana. --here because every hana session runs on natedev:
reading the Mac adds nothing to this tree, and a sleeping Mac would report a
problem on every run and freeze the note.

When the roster reports a problem, has no hana showrunner or no unit director
under it, git cannot run, or a showrunners.py call fails, the note is left as it
stands and the reason goes to stderr: a stale note says when it was taken, an
empty diagram says nothing is running. The roster reads staff and live sessions;
it does not require tmux. A missing git would quietly turn the production link
into a file:// URI. Source-command failures still fail the job.

Run by agent-notes; its vault part commits the generated note.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.maintenance.settings import settings
from scripts.lib.user_zone import user_zone

NOTE = os.path.expanduser("~/rust/hanadocs/hana hierarchy.md")
SCRIPTS = str(Path(__file__).resolve().parents[1])
ROSTER = os.path.join(SCRIPTS, "message", "roster.py")
SHOWRUNNERS = os.path.join(SCRIPTS, "production", "showrunners.py")
HOME = os.path.expanduser("~")
SHOWRUNNER = "hana"
SOURCE = "scripts/maintenance/hana_hierarchy.py"

# A worker's slot_role, as the job it was given; any other value is shown as it is.
ROLES = {"fix": "fixer", "impl": "implementer", "test": "tester", "review": "reviewer"}

# A worker's roster status, as the state its box shows; any other value is shown as it is.
STATES = {"running": "working", "idle": "idle"}

# Each state's box color, in legend order. A state is colored by its first word, so
# roster.py's "waiting for capacity" is amber; a state outside these is left plain.
CLASSES = {
    "working": "fill:#d3f2d8,stroke:#2e7d32,color:#1b3d20",
    "idle": "fill:#ececec,stroke:#8a8a8a,color:#3a3a3a",
    "waiting": "fill:#fff1cc,stroke:#b8860b,color:#4a3b00",
}

INTRO = (
    "The boxes run from the showrunner to its unit directors to the workers each one launched. "
    + "Each box reads name — role. Showrunner and director boxes add their folder; "
    + "worker boxes add their state."
)

NOTES = (
    "- A worker keeps its opening name when its job changes. The role shown is its current job.",
    "- Closed workers and workers from a director's earlier run are left out.",
    "- The workers shown are Codex workers. A director's short-lived Claude helpers are not recorded.",
)


class Entry(TypedDict, total=False):
    """The fields of one roster.py --json entry this reads; it carries more."""

    role: str
    name: str
    cwd: str
    production: str
    showrunner: str
    unit: str
    director: str
    slot_role: str
    status: str


class Roster(TypedDict):
    entries: list[Entry]
    problems: list[str]


class Worker(NamedTuple):
    name: str
    role: str
    status: str  # roster.py's: running, idle, ...; state() words it


class Director(NamedTuple):
    name: str
    cwd: str
    unit: str
    workers: list[Worker]


class Tree(NamedTuple):
    """One showrunner, its directors and the showrunners below it."""

    name: str
    cwd: str
    directors: list[Director]
    lower: list[Tree]


class Listed(NamedTuple):
    """One `showrunners.py list` row: the session running the production, and its doc."""

    session: str
    doc: str


class Snapshot(NamedTuple):
    """Everything the note shows, gathered and checked; render() lays it out."""

    tree: Tree
    lower: list[str]  # each production directly under the top one, as "session (slug)"
    production_name: str  # the production doc's file name
    link: str  # production_file_link's answer
    created: str  # date_created, kept from the note on disk
    now: datetime


class Refused(Exception):
    """The sources cannot be trusted this run; the note stays as it is."""


class ReadFailed(Refused):
    """A source command failed or timed out; preserve the note but fail the job."""


def tidy(path: str) -> str:
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + os.sep) else path


def node(name: str) -> str:
    """A Mermaid id: the name with every character outside [A-Za-z0-9_] made `_`."""
    return re.sub(r"\W", "_", name, flags=re.ASCII)


def unique(entries: list[Entry]) -> list[Entry]:
    """The entries, first of each name kept, sorted by name."""
    seen: dict[str, Entry] = {}
    for entry in entries:
        _ = seen.setdefault(entry.get("name", ""), entry)
    return [seen[name] for name in sorted(seen)]


def tree(roster: Roster, showrunner: Entry, lower: list[Tree]) -> Tree:
    """`showrunner` with its unit directors and their workers, from the roster alone."""
    name = showrunner.get("name", "")
    entries = roster["entries"]
    workers = unique([
        e for e in entries
        if e.get("role") == "worker" and e.get("showrunner") == name and e.get("director")
    ])
    directors = [
        Director(
            d.get("name", ""), d.get("cwd", ""), d.get("unit", ""),
            [Worker(w.get("name", ""), ROLES.get(w.get("slot_role", ""), w.get("slot_role", "")),
                    w.get("status", ""))
             for w in workers if w.get("director") == d.get("name")],
        )
        for d in unique([e for e in entries if e.get("role") == "unit director" and e.get("showrunner") == name])
    ]
    return Tree(name, showrunner.get("cwd", ""), directors, lower)


def showrunner_entry(roster: Roster, name: str) -> Entry | None:
    return next((e for e in roster["entries"] if e.get("role") == "showrunner" and e.get("name") == name), None)


def state(status: str) -> str:
    return STATES.get(status, status)


def color(worker_state: str) -> str | None:
    """The class a worker box with this state is drawn in, or None to leave it plain."""
    word = worker_state.split(" ", 1)[0]
    return word if word in CLASSES else None


def all_directors(top: Tree) -> list[Director]:
    """Every unit director in the tree, below every showrunner in it."""
    return [*top.directors, *(d for below in top.lower for d in all_directors(below))]


def all_workers(top: Tree) -> list[Worker]:
    """Every worker in the tree, below every showrunner in it."""
    return [w for d in all_directors(top) for w in d.workers]


def drawn_classes(top: Tree) -> dict[str, list[str]]:
    """Each colored state with a worker box, in legend order, to its box ids, sorted."""
    ids: dict[str, list[str]] = {name: [] for name in CLASSES}
    for worker in all_workers(top):
        if (name := color(state(worker.status))) is not None:
            ids[name].append(node(worker.name))
    return {name: sorted(found) for name, found in ids.items() if found}


def legend(top: Tree) -> str:
    waiting = ", amber is waiting" if "waiting" in drawn_classes(top) else ""
    return f"Workers: green is working, grey is idle{waiting}."


def diagram(top: Tree) -> list[str]:
    lines = ["```mermaid", "flowchart TD", f'  {node(top.name)}["{top.name} — showrunner<br/>{tidy(top.cwd)}"]']

    def add(current: Tree) -> None:
        parent = node(current.name)
        for director in current.directors:
            lines.extend([
                "",
                f'  {node(director.name)}["{director.name} — unit director<br/>{tidy(director.cwd)}"]',
                f"  {parent} --> {node(director.name)}",
            ])
            for worker in director.workers:
                lines.extend([
                    "",
                    f'  {node(worker.name)}["{worker.name} — {worker.role}<br/>{state(worker.status)}"]',
                    f"  {node(director.name)} --> {node(worker.name)}",
                ])
        for below in current.lower:
            lines.extend([
                "",
                f'  {node(below.name)}["{below.name} — showrunner<br/>{tidy(below.cwd)}"]',
                f"  {parent} --> {node(below.name)}",
            ])
            add(below)

    add(top)
    lines.append("")
    lines.extend(f"  classDef {name} {style}" for name, style in CLASSES.items())
    lines.extend(f"  class {','.join(ids)} {name}" for name, ids in drawn_classes(top).items())
    return [*lines, "```"]


def table(top: Tree) -> list[str]:
    rows = ["| Unit director | Unit | Workers |", "| --- | --- | --- |"]

    def add(current: Tree) -> None:
        rows.extend(f"| {d.name} | {d.unit} | {len(d.workers)} |" for d in current.directors)
        for below in current.lower:
            add(below)

    add(top)
    return rows


def lower_names(children: list[str], listed: dict[str, Listed]) -> list[str]:
    return [
        f"{listed[slug].session} ({slug})" if slug in listed and listed[slug].session != "<not running>"
        else f"{slug} (not running)"
        for slug in children
    ]


def render(snapshot: Snapshot) -> str:
    """The whole note. The one place the layout lives: swap this to change it."""
    top, now = snapshot.tree, snapshot.now
    link = snapshot.link
    link_text = link if link.startswith("[[") else f"[{snapshot.production_name}]({link})"
    lower = (
        f"- Showrunners under {top.name}: {', '.join(snapshot.lower)}."
        if snapshot.lower else f"- The {top.name} showrunner has no showrunner under it today."
    )
    lines = [
        "---",
        f"date_created: {snapshot.created}",
        f'date_modified: "[[{now:%Y-%m-%d}]]"',
        "---",
        "",
        "# Hana hierarchy",
        "",
        f"Snapshot taken {now:%Y-%m-%d %H:%M %Z}.",
        "",
        f"Showrunner: {top.name} · Production file: {link_text}",
        "",
        INTRO,
        "",
        *diagram(top),
        "",
        legend(top),
        "",
        *table(top),
        "",
        *NOTES,
        lower,
        "",
        "## Rebuilding it",
        "",
        f"The agent-notes job rewrites this note from `{SOURCE}`, "
        + "and commits it when a box, state, link or count changes.",
        "",
    ]
    return "\n".join(lines)


def git(directory: Path, *args: str) -> str:
    """git's answer, or empty when it fails; Refused when git cannot run at all.

    A failure means "not a checkout" or "no such remote", and the link falls back.
    git missing or hung means nothing, so the link it would cost is never written.
    """
    try:
        done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=settings().hierarchy_git_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReadFailed(f"git {' '.join(args)}: {error}") from error
    return done.stdout.strip() if done.returncode == 0 else ""


def first_promote_checkout(lines: list[str]) -> Path | None:
    """The first local checkout in the doc's `- **Promote:**` line, as merge_checkpoint.py reads it."""
    prefix = "- **Promote:**"
    field = next((line[len(prefix):].removeprefix(" ") for line in lines if line.startswith(prefix)), None)
    if field is None:
        return None
    raw = field.split(" — ", 1)[0].replace("`", "")
    if raw.strip().casefold() == "none":
        return None
    for item in raw.split(","):
        item = item.strip()
        if item and not item.startswith("mac "):
            return Path(item).expanduser()
    return None


def github_blob_url(checkout: Path, relative: Path) -> str:
    """The GitHub page of `relative` on the checkout's branch, or empty off GitHub."""
    remote, branch = git(checkout, "remote", "get-url", "origin"), git(checkout, "symbolic-ref", "--short", "HEAD")
    match = re.fullmatch(
        r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)(?P<repository>[^/]+/[^/]+?)(?:\.git)?/?",
        remote,
    )
    if match is None or not branch:
        return ""
    return f"https://github.com/{match.group('repository')}/blob/{branch}/{relative.as_posix()}"


def production_file_link(document: Path, vault: Path) -> str:
    """The production doc's main-branch copy, linked as the vault's phase notes link it.

    The same form as `production_file` in ~/.claude/scripts/delegate/phase_table.py: a
    [[wiki link]] when the copy is in the vault, else its GitHub page on the first
    Promote checkout's branch, else a file:// URI.
    """
    top = git(document.parent, "rev-parse", "--show-toplevel")
    try:
        if not top:
            raise ValueError(top)
        root = Path(top).resolve()
        relative = document.resolve().relative_to(root)
        lines = document.read_text(encoding="utf-8").splitlines()
    except (ValueError, OSError, UnicodeError):
        return document.as_uri()
    promoted = first_promote_checkout(lines)
    checkout = promoted.resolve() if promoted else root
    main_copy = checkout / relative
    try:
        in_vault = main_copy.relative_to(vault.resolve())
    except ValueError:
        return github_blob_url(checkout, relative) or main_copy.as_uri()
    return f"[[{in_vault.with_suffix('').as_posix()}]]"


def call(script: str, *args: str) -> str:
    """A script's stdout; Refused with its stderr when it fails."""
    try:
        done = subprocess.run([sys.executable, "-I", script, *args], capture_output=True, text=True, timeout=settings().hierarchy_read_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReadFailed(f"{os.path.basename(script)} {' '.join(args)}: {error}") from error
    if done.returncode != 0:
        reason = (done.stderr or done.stdout).strip().splitlines()
        raise ReadFailed(f"{os.path.basename(script)} {' '.join(args)}: {reason[0] if reason else done.returncode}")
    return done.stdout


def parse_roster(text: str) -> Roster:
    try:
        data = cast(object, json.loads(text))
    except ValueError as error:
        raise Refused(f"roster.py: not JSON: {error}") from error
    fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
    if not isinstance(fields.get("entries"), list) or not isinstance(fields.get("problems"), list):
        raise Refused("roster.py: no entries or problems list")
    roster = cast(Roster, cast(object, fields))
    if roster["problems"]:
        raise Refused(f"roster.py reports problems: {'; '.join(roster['problems'])}")
    return roster


def parse_list(text: str) -> dict[str, Listed]:
    """slug -> (session, doc) from `showrunners.py list`'s tab-separated rows."""
    listed: dict[str, Listed] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) >= 4:
            listed[cells[0]] = Listed(cells[1], cells[3])
    return listed


def subtree(roster: Roster, slug: str, listed: dict[str, Listed], children: dict[str, list[str]]) -> Tree | None:
    """The tree of the showrunner running production `slug`, or None while it is not in the roster."""
    session = listed[slug].session if slug in listed else ""
    entry = showrunner_entry(roster, session)
    if entry is None:
        return None
    below = [t for child in children.get(slug, []) if (t := subtree(roster, child, listed, children)) is not None]
    return tree(roster, entry, below)


def kept_created(path: str, today: str) -> str:
    """date_created from the note on disk, or today for a new note."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("date_created: "):
                    return line.removeprefix("date_created: ").rstrip("\n")
    except OSError:
        pass
    return f'"[[{today}]]"'


def all_children(top_slug: str) -> dict[str, list[str]]:
    """Each production below `top_slug`, at any depth, with the ones directly under it."""
    found: dict[str, list[str]] = {}
    pending = [top_slug]
    while pending:
        slug = pending.pop()
        found[slug] = [c for c in call(SHOWRUNNERS, "children", slug).splitlines() if c.strip()]
        pending.extend(c for c in found[slug] if c not in found)
    return found


def gather(note: str, now: datetime) -> Snapshot:
    """What the sources say the note should show now; Refused when they cannot be trusted."""
    roster = parse_roster(call(ROSTER, "--here", "--json"))
    top = showrunner_entry(roster, SHOWRUNNER)
    if top is None:
        raise Refused(f"roster.py lists no {SHOWRUNNER} showrunner")
    listed = parse_list(call(SHOWRUNNERS, "list"))
    slug = top.get("production", "")
    if slug not in listed:
        raise Refused(f"showrunners.py list has no row for {SHOWRUNNER}'s production {slug or '(none)'}")
    children = all_children(slug)
    lower = [t for child in children[slug] if (t := subtree(roster, child, listed, children)) is not None]
    hierarchy = tree(roster, top, lower)
    if not all_directors(hierarchy):
        raise Refused(f"roster.py lists no unit director under {SHOWRUNNER}")
    link = production_file_link(Path(listed[slug].doc), Path(os.path.dirname(note)))
    created = kept_created(note, f"{now:%Y-%m-%d}")
    name = Path(listed[slug].doc).name
    return Snapshot(hierarchy, lower_names(children[slug], listed), name, link, created, now)


def main(argv: list[str], *, refusal_exit: int = 1) -> int:
    note = argv[0] if argv else NOTE
    try:
        text = render(gather(note, datetime.now(user_zone())))
    except Refused as reason:
        print(f"hana hierarchy: left as it stands: {reason}", file=sys.stderr)
        return 1 if isinstance(reason, ReadFailed) else refusal_exit
    tmp = note + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        _ = handle.write(text)
    os.replace(tmp, note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
