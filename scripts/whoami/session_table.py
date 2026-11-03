"""Stable account-note tables from locally attributed session rosters."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami.note_file import canonical_label

SessionRole = Literal["showrunner", "unit", "worker", "freestanding"]
ROLE_ORDER: tuple[SessionRole, ...] = ("showrunner", "unit", "worker", "freestanding")
HEADING = "## Live sessions"
SECTION = re.compile(r"^## Live sessions[ \t]*(?:\n|$)", re.MULTILINE)
NEXT_HEADING = re.compile(r"^#{1,2} ", re.MULTILINE)


class SessionRow(NamedTuple):
    name: str
    role: SessionRole
    machine: str
    folder: str
    session_id: str
    last_seen: str = ""
    # Read-only compatibility with notes written before sessions_seen.json.
    observed_at: str = ""


@dataclass(frozen=True)
class SessionsSection:
    section: str


@dataclass(frozen=True)
class KeepSections:
    """This pass cannot replace the note's owned body section."""


KEEP_SECTIONS = KeepSections()


@dataclass(frozen=True)
class ReadSessions:
    rows_by_label: dict[str, tuple[SessionRow, ...]]
    rows_kept: frozenset[str]
    unattributed: int = 0
    machines_read: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SessionsUnread:
    reason: str


def _short_folder(folder: str) -> str:
    return re.sub(r"^/(?:home|Users)/[^/]+(?=/|$)", "~", folder)


def render_section(rows: Sequence[SessionRow], unattributed: int = 0) -> str:
    lines = [HEADING, ""]
    if not rows and not unattributed:
        return "\n".join([*lines, "No live sessions.", ""])
    if rows:
        lines += ["| Session | Role | Machine | Folder | Session id |",
                  "| --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda item: (item.machine, ROLE_ORDER.index(item.role), item.name, item.session_id)):
        identity = row.session_id + (f" (last seen {row.last_seen})" if row.last_seen else "")
        cells = (row.name, row.role, row.machine, _short_folder(row.folder), identity)
        lines.append("| " + " | ".join(cell.replace("\n", " ").replace("|", r"\|") for cell in cells) + " |")
    if unattributed:
        lines += ["", f"{unattributed} live session(s) not attributed to an account"]
    return "\n".join([*lines, ""])


def _section_bounds(body: str) -> tuple[int, int]:
    match = SECTION.search(body)
    if match is None:
        return len(body), len(body)
    following = NEXT_HEADING.search(body, match.end())
    return match.start(), following.start() if following else len(body)


def section_rows(body: str) -> tuple[SessionRow, ...]:
    start, end = _section_bounds(body)
    rows: list[SessionRow] = []
    for line in body[start:end].splitlines():
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = tuple(cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line[1:-1]))
        if len(cells) != 5 or cells[1] not in ROLE_ORDER:
            continue
        role: SessionRole = ROLE_ORDER[ROLE_ORDER.index(cells[1])]
        saved_identity, observation, observed_at = cells[4].partition(" <!-- observed-at: ")
        identity, marker, seen = saved_identity.partition(" (last seen ")
        rows.append(SessionRow(cells[0], role, cells[2], cells[3], identity,
                               seen.removesuffix(")") if marker else "",
                               observed_at.removesuffix(" -->") if observation else ""))
    return tuple(rows)


def replace_section(body: str, section: str) -> str:
    start, end = _section_bounds(body)
    if start == len(body):
        return body.rstrip("\n") + ("\n\n" if body.rstrip("\n") else "") + section
    return body[:start] + section.rstrip("\n") + ("\n\n" if end < len(body) else "\n") + body[end:]


def live_sessions(environment: Mapping[str, str]) -> ReadSessions | SessionsUnread:
    from scripts.message import roster

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            peer_future = pool.submit(roster.peer_snapshot, environment)
            local = roster.roster(environment, roster.ThisMachine())
            peer = peer_future.result()
    except Exception as error:
        return SessionsUnread(str(error) or type(error).__name__)
    kept: set[str] = set()
    machines_read = {roster.machine_name(), *(entry.machine for entry in local.entries)}
    entries = list(local.entries)
    if isinstance(peer, roster.PeerNotReached):
        kept.add(peer.host)
        # SSH aliases (such as "mac") need not equal the hostname in saved rows.
        from scripts.whoami.note_file import read_notes

        kept.update(row.machine for note in read_notes() for row in section_rows(note.text)
                    if row.machine not in machines_read)
    else:
        unavailable = {entry.machine for entry in peer.entries
                       if isinstance(entry.account, roster.AccountAttributionUnavailable)}
        kept.update(unavailable)
        machines_read.update(entry.machine for entry in peer.entries if entry.machine not in unavailable)
        if not peer.entries:
            machines_read.add(roster.remote.other_machine())
        entries += [entry for entry in peer.entries if entry.machine not in unavailable]
    rows: dict[str, list[SessionRow]] = {}
    roles: dict[str, SessionRole] = {"showrunner": "showrunner", "unit director": "unit",
                                    "worker": "worker", "freestanding": "freestanding"}
    unattributed = 0
    for entry in entries:
        if isinstance(entry.account, roster.AccountUnread):
            unattributed += 1
        if not isinstance(entry.account, roster.AccountLabel) or entry.role not in roles:
            continue
        session_id = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                      else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "—")
        rows.setdefault(canonical_label(entry.account.label), []).append(
            SessionRow(entry.name, roles[entry.role], entry.machine, _short_folder(entry.cwd), session_id))
    return ReadSessions({label: tuple(items) for label, items in rows.items()}, frozenset(kept),
                        unattributed, frozenset(machines_read))


def last_seen(row: SessionRow, machines_seen: Mapping[str, str]) -> SessionRow:
    """Freeze a kept row's time, preferring its saved marker over outside observations."""
    return row._replace(last_seen=row.last_seen or machines_seen.get(row.machine) or row.observed_at or "unknown",
                        observed_at="")
