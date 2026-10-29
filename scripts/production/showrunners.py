#!/usr/bin/env python3
"""Read this machine's showrunners from their update timers, and its showrunner settings.

A showrunner may report to another: `attach <child> [<parent>]` stores the one fact, the parent's
slug, in the child's `parent` file under the showrunner state dir. Children, the root and names are
looked up from the update timers at each call, so the tree holds no name and no list.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SHOWRUNNER_STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
# Where Claude Code keeps each session scratchpad, as /tmp/claude-<uid>/<project>/<session id>/scratchpad.
SCRATCH_ROOT = Path(os.environ.get("SHOWRUNNER_SCRATCH_ROOT") or "/tmp")
SEND = Path(os.environ.get("SHOWRUNNERS_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
PARENT_FILE = "parent"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone_name  # noqa: E402

RULES = "Read the Nested showrunners rules in ~/.claude/commands/showrunner/produce.md."


class Showrunner(TypedDict):
    """One showrunner, read from its update timer each time. Nothing here is stored in the config."""

    # The session's name now, looked up by its Claude session id. Empty while it is not running.
    session: str
    # Where a message reaches it now. Empty while it is not running.
    socket: str
    # The production's slug, which names the showrunner whatever its session is called.
    slug: str
    # The user's zone setting. A production doc's own User zone line is no longer read.
    zone: str
    # The production doc, whose Units table says which units the showrunner has.
    doc: str


class ShowrunnerSettings(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    stall_minutes: float
    faults_to: str
    always: list[str]


class Timer(NamedTuple):
    """One complete update timer. It is what makes a production's showrunner exist, running or not."""

    slug: str
    target: str
    doc: Path
    zone: str


class CheckedDoc(NamedTuple):
    path: Path


class NoCheckedDoc(NamedTuple):
    reason: str


def defaults() -> ShowrunnerSettings:
    return ShowrunnerSettings(threshold_percent=2, repeat_minutes=30, stall_minutes=5,
                              faults_to="natedev", always=["natedev"])


def load_settings() -> ShowrunnerSettings:
    return load_settings_from(CONFIG)


def load_settings_from(path: Path) -> ShowrunnerSettings:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("expected an object")
        data = cast(dict[str, object], raw)
        for key in ("threshold_percent", "repeat_minutes", "stall_minutes"):
            if not isinstance(data.get(key), (int, float)) or isinstance(data[key], bool):
                raise ValueError(f"invalid {key}")
        if not isinstance(data.get("faults_to"), str) or not data["faults_to"]:
            raise ValueError("invalid faults_to")
        always = data.get("always")
        if not isinstance(always, list) or not all(isinstance(item, str) for item in cast(list[object], always)):
            raise ValueError("invalid always")
        # A file written while showrunners were listed here still has the list; it is not read.
        return ShowrunnerSettings(threshold_percent=cast(float, data["threshold_percent"]),
                                  repeat_minutes=cast(float, data["repeat_minutes"]),
                                  stall_minutes=cast(float, data["stall_minutes"]),
                                  faults_to=cast(str, data["faults_to"]), always=cast(list[str], always))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{path}: {error}") from error


def socket_for(target: str) -> str | None:
    """The session's socket, or None when it is not running. Raises OSError when the session
    records cannot be read, which says nothing about whether it runs."""
    result = subprocess.run([sys.executable, str(SESSIONS), "socket", target], capture_output=True,
                            text=True, check=False)
    # Exit 1 is no such live session. Any other failure is the lookup itself failing.
    if result.returncode not in (0, 1):
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
        raise OSError(f"cannot tell whether {target} is running: {reason}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _fields(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def checked_doc(instance: Path) -> CheckedDoc | NoCheckedDoc:
    """Read the production doc passed to an update instance's production check."""
    try:
        check = _fields(instance / "conf").get("CHECK")
    except (OSError, UnicodeError) as error:
        return NoCheckedDoc(f"conf could not be read: {error}")
    if check is None:
        return NoCheckedDoc("CHECK is missing")
    try:
        command = shlex.split(check)
    except ValueError as error:
        return NoCheckedDoc(f"CHECK could not be parsed: {error}")
    try:
        script_index = next(index for index, token in enumerate(command)
                            if Path(token).name == "production_check.sh")
    except StopIteration:
        return NoCheckedDoc("CHECK has no production_check.sh token")
    if script_index + 1 >= len(command):
        return NoCheckedDoc("CHECK has no production doc token")
    path = Path(command[script_index + 1])
    if not path.is_absolute():
        return NoCheckedDoc("production doc path is relative")
    return CheckedDoc(path)


def session_name(session_id: str) -> str:
    """The live Claude session's name, or empty when it has none or is not running."""
    return _session_name(session_id)


def _session_name(session_id: str) -> str:
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
            try:
                os.kill(pid, 0)
            except PermissionError:
                # Another user's process is still a live one.
                pass
            data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            name = data.get("name")
            if data.get("sessionId") == session_id and isinstance(name, str) and name:
                return name
        except (OSError, ValueError, TypeError):
            continue
    raise ValueError(f"session {session_id} has no live named process")


def _readable(doc: Path) -> None:
    """Refuse a production doc that cannot be read."""
    try:
        _ = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"production doc could not be read: {error}") from error


def _timer(instance: Path) -> Timer:
    fields = _fields(instance / "conf")
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    doc = checked_doc(instance)
    if isinstance(doc, NoCheckedDoc):
        raise ValueError(doc.reason)
    _readable(doc.path)
    return Timer(instance.name.removeprefix("showrunner-"), target, doc.path, user_zone_name())


def _registered(instance: Path) -> Showrunner:
    timer = _timer(instance)
    socket = socket_for(timer.target)
    name = _session_name(timer.target[8:]) if socket is not None else ""
    return Showrunner(session=name, socket=socket or "", slug=timer.slug, zone=timer.zone, doc=str(timer.doc))


def registered_showrunners(state_dir: Path | None = None) -> list[Showrunner]:
    """Every showrunner that has an update timer, running or not.

    The timer is addressed to the Claude session id and holds the production doc, so it is the one
    record of a showrunner and holds no name: the name is looked up here, at each call. Raises OSError
    when the session records cannot say whether a showrunner runs; a timer that is not a complete
    showrunner's is left out, with the reason on stderr.
    """
    found: list[Showrunner] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        try:
            found.append(_registered(instance))
        except (KeyError, ValueError, UnicodeError) as error:
            print(f"showrunners: skipping {instance.name}: {error}", file=sys.stderr)
    return found


def production_of(session_id: str, state_dir: Path | None = None) -> Path | None:
    """The production doc of the showrunner whose update timer is addressed to `session_id`."""
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        try:
            target = _fields(instance / "conf").get("TARGET")
        except (OSError, UnicodeError):
            continue
        doc = checked_doc(instance)
        if session_id and target == f"session:{session_id}" and isinstance(doc, CheckedDoc):
            return doc.path
    return None


def production_slug(doc: str) -> str:
    """The slug a production's units are marked with: its doc's name without the suffix."""
    slug = Path(doc).name.removesuffix("-production.md")
    if not doc or slug == Path(doc).name:
        raise ValueError(f"not a production doc: {doc or 'none registered'}")
    return slug


def session_state_dir(slug: str) -> Path:
    """The showrunner's own state, which a new session id, a restart and a reboot keep; a wrap deletes it."""
    return SHOWRUNNER_STATE_DIR / slug / "session"


# The showrunner state a scratchpad held before session_state_dir.
SESSION_STATE = ("dailies_input_state", "unit_status", "showrunner_state.json", "dailies_judgment.json",
                 "dailies_state.json")


def copy_session_state(slug: str, scratch: Path) -> list[str]:
    """Copy each state file the scratchpad holds and the state directory does not; a copy never replaces one."""
    target = session_state_dir(slug)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in SESSION_STATE:
        source, destination = scratch / name, target / name
        if destination.exists() or not source.exists():
            continue
        if source.is_dir():
            _ = shutil.copytree(source, destination)
        else:
            _ = shutil.copy2(source, destination)
        copied.append(name)
    return copied


def timer_scratch(slug: str) -> Path | None:
    """The scratchpad of the session the production's update timer is addressed to, when it exists."""
    try:
        target = _fields(NOTIFIER_STATE_DIR / f"showrunner-{slug}" / "conf").get("TARGET", "")
    except (OSError, UnicodeError):
        return None
    session_id = target.removeprefix("session:")
    if not session_id or session_id == target:
        return None
    return next((path for path in sorted(SCRATCH_ROOT.glob(f"claude-*/*/{session_id}/scratchpad"))
                 if path.is_dir()), None)


def adopt_session_state(slug: str) -> list[str]:
    """Bring a showrunner's scratchpad state into its state directory once, with no step by anyone.

    Only the state files the directory lacks are looked for, so a showrunner that has moved costs one
    stat per file.
    """
    target = session_state_dir(slug)
    if all((target / name).exists() for name in SESSION_STATE):
        return []
    scratch = timer_scratch(slug)
    return copy_session_state(slug, scratch) if scratch is not None else []


def former_slugs(text: str) -> tuple[str, ...]:
    """Read former names only from the production's Production Context section."""
    context = re.search(r"^## Production Context\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    formerly = re.search(r"^- \*\*Formerly:\*\*(.*)$", context[1] if context else "", re.MULTILINE)
    return tuple(re.findall(r"`([^`]+)`", formerly[1])) if formerly else ()


def canonical_slug(slug: str, state_dir: Path | None = None) -> str:
    """Map a former production name to its current slug; leave other slugs unchanged."""
    timers: list[Timer] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        try:
            timer = _timer(instance)
        except (OSError, KeyError, ValueError, UnicodeError):
            continue
        if timer.slug == slug:
            return slug
        timers.append(timer)
    for timer in timers:
        try:
            text = timer.doc.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if slug in former_slugs(text):
            return timer.slug
    return slug


def current_name(slug: str) -> str:
    """The name now of the production's showrunner. Empty while it is not running or has no update timer."""
    slug = canonical_slug(slug)
    return next((runner["session"] for runner in registered_showrunners() if runner["slug"] == slug), "")


def herdr_space(slug: str, doc: str) -> str:
    """The shared herdr session's workspace for a production, named for its showrunner.

    The doc's `Showrunner session:` line names it, else the slug. The name never depends on whether
    the showrunner runs, so a unit restarted before its showrunner finds the same workspace.
    """
    try:
        text = Path(doc).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
    line = re.search(r"^- \*\*Showrunner session:\*\*\s*`?([^`\s]+)`?\s*$", text, re.MULTILINE)
    return line[1] if line else slug


def current_address(slug: str) -> str:
    """Where `SendMessage` reaches the production's showrunner now: its session on this machine.
    Empty while it is not running or has no update timer."""
    slug = canonical_slug(slug)
    socket = next((runner["socket"] for runner in registered_showrunners() if runner["slug"] == slug), "")
    return f"uds:{socket}" if socket else ""


def named(showrunner: str) -> Showrunner:
    """The showrunner called `showrunner` now, or whose production has that slug."""
    slug = canonical_slug(showrunner)
    runners = registered_showrunners()
    # The production a slug resolves to wins over a session that happens to share the name.
    found = (next((runner for runner in runners if runner["slug"] == slug), None)
             or next((runner for runner in runners if runner["session"] == showrunner), None))
    if showrunner and found:
        return found
    raise ValueError(f"no showrunner with an update timer is called {showrunner} or runs that production")


def timer_docs(state_dir: Path | None = None) -> dict[str, Path]:
    """Each production with a complete update timer, by slug, with its doc, whether or not its showrunner runs."""
    found: dict[str, Path] = {}
    for instance in (state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*"):
        try:
            timer = _timer(instance)
        except (OSError, KeyError, ValueError, UnicodeError):
            continue
        found[timer.slug] = timer.doc
    return found


def timer_slugs(state_dir: Path | None = None) -> set[str]:
    """Every production with a complete update timer, whether or not its showrunner runs."""
    return set(timer_docs(state_dir))


def _stored_parent(child: str) -> str:
    """What the child's parent file says, whether or not that edge counts. Empty when it has none."""
    try:
        return (SHOWRUNNER_STATE_DIR / child / PARENT_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _on_cycle(slug: str, stored: dict[str, str]) -> bool:
    seen = {slug}
    current = stored.get(slug, "")
    while current and current not in seen:
        seen.add(current)
        current = stored.get(current, "")
    return current == slug


def edges(slugs: set[str] | None = None) -> dict[str, str]:
    """Each child's parent, among `slugs` or every production with an update timer.

    An edge counts only while both ends have an update timer, so a wrapped production leaves no
    broken tree. `attach` writes no cycle; one written by hand counts for none of its members, so
    each of them is a root.
    """
    known = timer_slugs() if slugs is None else slugs
    stored = {child: parent for child in known if (parent := _stored_parent(child)) in known and parent != child}
    return {child: parent for child, parent in stored.items() if not _on_cycle(child, stored)}


def parent_of(slug: str) -> str:
    """The production `slug` reports to. Empty for a root."""
    return edges().get(slug, "")


def children_of(slug: str) -> list[str]:
    """The productions that report to `slug`, one level down."""
    return sorted(child for child, parent in edges().items() if parent == slug)


def _below(slug: str, tree: dict[str, str]) -> list[str]:
    level = [slug]
    found: list[str] = []
    while level:
        level = sorted(child for child, parent in tree.items() if parent in level)
        found.extend(level)
    return found


def descendants_of(slug: str) -> list[str]:
    """Every production below `slug`, at any depth, the nearest level first."""
    return _below(slug, edges())


def _root_in(slug: str, tree: dict[str, str]) -> str:
    while slug in tree:
        slug = tree[slug]
    return slug


def user_facing(slug: str, below: bool = False) -> list[str]:
    """The productions whose items for the user `slug`'s showrunner counts and lists.

    Each showrunner answers for its own production only: the showrunner above it and the
    showrunners below it answer for theirs (user, 2026-10-27). `below` adds every production
    below it, at any depth (`/next all-below`). A production above or beside it never counts.
    """
    return [slug, *descendants_of(slug)] if below else [slug]


def root_of(slug: str) -> str:
    """The top of the tree `slug` is in, the showrunner that shows the user everything below it. A root is its own."""
    return _root_in(slug, edges())


def highest_running(runners: list[Showrunner]) -> list[Showrunner]:
    """Each running showrunner with no running showrunner above it.

    These are the roots, and below a root that is down, the showrunners nearest it. What every
    production must hear once, a machine-wide fault, goes to these.
    """
    tree = edges({runner["slug"] for runner in runners})
    running = {runner["slug"] for runner in runners if runner["socket"]}

    def covered(slug: str) -> bool:
        while slug in tree:
            slug = tree[slug]
            if slug in running:
                return True
        return False

    return [runner for runner in runners if runner["socket"] and not covered(runner["slug"])]


def tree_lines(runners: list[Showrunner]) -> list[str]:
    """One line per showrunner, each child indented under its parent."""
    tree = edges({runner["slug"] for runner in runners})
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


def own_slug() -> str:
    """The production this session runs as showrunner. Empty when it runs none."""
    doc = production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    return production_slug(str(doc)) if doc is not None else ""


def name_refusal(slug: str, name: str) -> str:
    """Why a showrunner session called `name` cannot run production `slug`; empty when it can.

    One name per production (user, 2026-10-27): the slug is the showrunner's name, so a production
    whose showrunner has another name is renamed first.
    """
    if name == slug:
        return ""
    if not name:
        return f"the showrunner of {slug} has no session name; name the session {slug}"
    return (f"production {slug} has showrunner {name}, and a production's slug must be its showrunner's name; "
            + f"rename the production first: `/showrunner:rename plan {slug} {name}`")


def attach(child: str, parent: str) -> None:
    """Store that `child` reports to `parent`. Raises ValueError naming why it cannot."""
    child = canonical_slug(child)
    parent = canonical_slug(parent)
    known = timer_slugs()
    for slug in (child, parent):
        if slug not in known:
            raise ValueError(f"{slug} has no update timer, so it has no showrunner to attach")
    if child == parent:
        raise ValueError(f"{child} cannot report to itself")
    for runner in registered_showrunners():
        if runner["slug"] in (child, parent) and runner["socket"] and (why := name_refusal(runner["slug"], runner["session"])):
            raise ValueError(why)
    tree = edges(known)
    if child in tree:
        raise ValueError(f"{child} already reports to {tree[child]}; detach it first")
    above = parent
    while above in tree:
        above = tree[above]
        if above == child:
            raise ValueError(f"{parent} already reports to {child}, directly or through others")
    path = SHOWRUNNER_STATE_DIR / child / PARENT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the file and moved over it, so a reader never sees half a slug.
    temporary = path.with_name(f".{PARENT_FILE}.{os.getpid()}")
    _ = temporary.write_text(f"{parent}\n", encoding="utf-8")
    _ = temporary.replace(path)


def detach(child: str) -> str:
    """Remove `child`'s parent file and return the parent it named. Raises ValueError when it has none."""
    child = canonical_slug(child)
    parent = _stored_parent(child)
    if not parent:
        raise ValueError(f"{child} reports to no one")
    (SHOWRUNNER_STATE_DIR / child / PARENT_FILE).unlink()
    return parent


class Released(NamedTuple):
    """The edges a wrapped production left: each one's parent file is gone."""

    # The production it reported to. Empty when it had none.
    parent: str
    # The productions that reported to it, each a root now.
    orphans: list[str]


def stored_children(slug: str) -> list[str]:
    """Every production whose parent file names `slug`, whether or not that edge counts."""
    return sorted(path.parent.name for path in SHOWRUNNER_STATE_DIR.glob(f"*/{PARENT_FILE}")
                  if path.parent.name != slug and _stored_parent(path.parent.name) == slug)


def release_edges(slug: str) -> Released:
    """Remove `slug`'s parent file and every parent file that names it, so a wrap leaves no edge behind.

    An edge to a wrapped production already counts for nothing; its file would come back to life if a
    production of the same slug ran again.
    """
    released = Released(_stored_parent(slug), stored_children(slug))
    for child in ([slug] if released.parent else []) + released.orphans:
        (SHOWRUNNER_STATE_DIR / child / PARENT_FILE).unlink(missing_ok=True)
    return released


def tell(slug: str, key: str, text: str) -> str:
    """Send one line to the production's showrunner, and say in a few words whether it got there."""
    address = current_address(slug)
    if not address:
        return f"{slug} is not running: tell it when it starts"
    sent = subprocess.run([sys.executable, str(SEND), "--to", address, "--from", "showrunners", "--key", key,
                           "--text", text], capture_output=True, text=True, check=False)
    if sent.returncode != 0:
        return f"could not tell {slug}: {sent.stderr.strip() or f'send.py exited {sent.returncode}'}"
    return f"told {slug}"


def tell_others(texts: dict[str, str], key: str) -> list[str]:
    """Tell each production its line, but for the one this session runs, which reads the command's own output."""
    own = own_slug()
    return [tell(slug, key, text) for slug, text in texts.items() if slug != own]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session", nargs="?")
    _ = making_ready.add_argument("--unit", action="append")
    naming = commands.add_parser("name")
    _ = naming.add_argument("slug")
    addressing = commands.add_parser("address")
    _ = addressing.add_argument("slug")
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    setting_status = commands.add_parser("status")
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
    _ = setting_status.add_argument("--state")
    _ = commands.add_parser("list")
    for lookup in ("parent", "children", "root"):
        looking_up = commands.add_parser(lookup)
        _ = looking_up.add_argument("slug")
    _ = commands.add_parser("tree")
    attaching = commands.add_parser("attach")
    _ = attaching.add_argument("child")
    _ = attaching.add_argument("parent", nargs="?")
    detaching = commands.add_parser("detach")
    _ = detaching.add_argument("child")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "list":
            for runner in registered_showrunners():
                print(f"{runner['slug']}\t{runner['session'] or '<not running>'}\t{runner['zone']}"
                      + f"\t{runner['doc']}")
        elif action in ("name", "address"):
            slug = cast(str, args.slug)
            found = current_name(slug) if action == "name" else current_address(slug)
            if not found:
                raise ValueError(f"the showrunner of {slug} is not running")
            print(found)
        elif action in ("parent", "children", "root"):
            slug = canonical_slug(cast(str, args.slug))
            if slug not in timer_slugs():
                raise ValueError(f"{slug} has no update timer")
            # A root prints no parent and no children: an empty answer is the answer.
            answers = children_of(slug) if action == "children" else [parent_of(slug) if action == "parent" else root_of(slug)]
            for answer in answers:
                if answer:
                    print(answer)
        elif action == "tree":
            for line in tree_lines(registered_showrunners()):
                print(line)
        elif action == "attach":
            child = canonical_slug(cast(str, args.child))
            parent = cast(str | None, args.parent) or own_slug()
            if not parent:
                raise ValueError("name the parent: this session runs no production")
            parent = canonical_slug(parent)
            attach(child, parent)
            top = root_of(parent)
            print(f"{child} now reports to {parent}")
            for line in tell_others({
                child: f"{child} now reports to {parent}. The root is {top}: send it each item for the user. {RULES}",
                parent: f"{child} now reports to {parent}. It is a child showrunner, never a unit: it has no Units "
                        + f"row, and no broadcast is passed on to it. {RULES}",
            }, f"showrunner-tree:{child}"):
                print(line)
        elif action == "detach":
            child = canonical_slug(cast(str, args.child))
            parent = detach(child)
            print(f"{child} no longer reports to {parent}")
            text = f"{child} no longer reports to {parent}. {child} is a root again."
            for line in tell_others({child: text, parent: text}, f"showrunner-tree:{child}"):
                print(line)
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
