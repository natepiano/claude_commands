"""Read a running production and compare a reply with its rendered footer."""

from __future__ import annotations

import re
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STAMP_WINDOW = timedelta(minutes=5)
NOTHING_NEEDED = " - nothing needed"
DAILIES = Path(__file__).resolve().parent.parent / "production" / "dailies_render.py"
REASON_HEAD = ("Showrunner footer missing or out of date. End this reply with these footer lines, word for word, "
               "then two empty lines, `Waiting on:`, an empty line, and one `* ` bullet per item, the user's first:")
STATUS = re.compile(r"Status: PRODUCTION — ([a-z]*)")
ZONE_MARKER = "- **User zone:**"
TIME_LINE = re.compile(r"^(\d{2}):(\d{2}) (\S+) update:$")
RENDER_TIMEOUT_SECONDS = 10
TIME = r"(?:[01]\d|2[0-3]):[0-5]\d(?:\+1)?"
ETA_LEAD = re.compile(
    rf"^(?:(?P<day>Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?(?P<time>{TIME})"
    + rf"(?: \((?P<low>{TIME})–(?P<high>{TIME})\))?(?P<zone> [A-Z]{{2,5}})? - "
)
MIDLINE_ETA = re.compile(rf"\bETA\s*:?[ ]*{TIME}\b|{TIME} \({TIME}–{TIME}\)", re.IGNORECASE)
LEADING_TIME = re.compile(rf"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?{TIME}(?:\s|$)")
WAITING_EXAMPLE = ("Waiting on items: user's items first, then ETA items soonest first, "
                   + "then no-ETA items. Use `19:45 (18:20–23:55) - startup Phase 16` "
                   + "or `no ETA measured - <item>`; omit the zone.")


class FooterState(Enum):
    ON = "on"
    OFF = "off"


class StampState(Enum):
    PRESENT = "present"
    MISSING = "missing"


class NoDailiesSchedule(Enum):
    PAUSED_OR_UNSCHEDULED = "paused or unscheduled"


class FooterError(Exception):
    """The shared footer renderer could not provide an authoritative footer."""


@dataclass(frozen=True)
class ScheduledDailies:
    next_due: int


@dataclass(frozen=True)
class Production:
    slug: str
    doc: Path
    zone: str
    schedule: ScheduledDailies | NoDailiesSchedule


@dataclass(frozen=True)
class FooterComparison:
    production: Production
    minute: datetime
    stamp_state: StampState
    nothing_needed: bool
    expected: list[str]


def key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def outstanding_path(slug: str) -> Path:
    return Path.home() / ".local/state/showrunner/outstanding" / f"{slug}.json"


def state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def switch_path(slug: str) -> Path:
    return state_root() / "footers-off" / slug


def review_pause_path(slug: str) -> Path:
    return state_root() / "review-paused" / f"{slug}.json"


def footer_state(slug: str) -> FooterState:
    return FooterState.OFF if switch_path(slug).exists() else FooterState.ON


def set_footer_state(slug: str, state: FooterState) -> None:
    path = switch_path(slug)
    if state is FooterState.OFF:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    else:
        path.unlink(missing_ok=True)


def targeted_instances(session_id: str) -> list[Path]:
    root = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    matches: list[Path] = []
    try:
        for instance in root.iterdir():
            if not instance.name.startswith("showrunner-") or not instance.is_dir():
                continue
            try:
                if f"TARGET=session:{session_id}" in (instance / "conf").read_text().splitlines():
                    matches.append(instance)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(matches)


def read_production(instance_dir: Path) -> Production | None:
    try:
        conf = key_values(instance_dir / "conf")
        check = shlex.split(conf.get("CHECK", ""))
        if not check:
            return None
        doc = Path(check[-1])
        lines = doc.read_text().splitlines()
        status = next((match.group(1) for line in lines if (match := STATUS.search(line))), None)
        if status != "running":
            return None
        zone_line = next((line for line in lines if line.startswith(ZONE_MARKER)), None)
        if zone_line is None:
            return None
        zone_words = zone_line[len(ZONE_MARKER):].strip().split()
        if not zone_words:
            return None
        zone = zone_words[0].strip("`")
        _ = ZoneInfo(zone)
        try:
            state = key_values(instance_dir / "state")
        except OSError:
            state = {}
        schedule = (ScheduledDailies(int(state["NEXT_DUE"])) if state.get("ENABLED") == "1"
                    else NoDailiesSchedule.PAUSED_OR_UNSCHEDULED)
        return Production(instance_dir.name.removeprefix("showrunner-"), doc, zone, schedule)
    except (OSError, ValueError, ZoneInfoNotFoundError, KeyError):
        return None


def next_run_text(next_due: int, minute: datetime, zone: ZoneInfo) -> str:
    due = datetime.fromtimestamp(next_due, zone)
    days = (due.date() - minute.date()).days
    return f"{due:%H:%M}" + (f"+{days}" if days > 0 else "")


def reply_lines(reply: str) -> list[str]:
    return reply.rstrip().splitlines()


def stamp(lines: list[str], now: datetime) -> tuple[datetime | None, bool]:
    update = next(((index, match) for index in range(len(lines) - 1, -1, -1)
                   if (match := TIME_LINE.fullmatch(lines[index])) is not None), None)
    if update is None:
        return None, False
    update_index, match = update
    hour, minute, abbreviation = int(match.group(1)), int(match.group(2)), match.group(3)
    footer_end = next((index for index in range(update_index + 1, len(lines))
                       if lines[index] == "Waiting on:"), len(lines))
    last_bullet = next((line for line in reversed(lines[update_index + 1:footer_end])
                        if line.startswith("* ")), "")
    nothing_needed = last_bullet.endswith(NOTHING_NEEDED)
    for day in (now.date(), now.date() - timedelta(days=1)):
        for fold in (0, 1):
            try:
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo, fold=fold)
            except ValueError:
                return None, nothing_needed
            age = now.timestamp() - candidate.timestamp()
            if candidate.strftime("%Z") == abbreviation and 0 <= age <= STAMP_WINDOW.total_seconds():
                return candidate, nothing_needed
    return None, nothing_needed


def render_footer(production: Production, minute: datetime, at: datetime | None, *, nothing_needed: bool) -> list[str]:
    command = [sys.executable, str(DAILIES), "--footer", "--zone", production.zone,
               "--outstanding", str(outstanding_path(production.slug))]
    if isinstance(production.schedule, ScheduledDailies):
        command.extend(["--next-run", next_run_text(production.schedule.next_due, minute,
                                                    ZoneInfo(production.zone))])
    if nothing_needed:
        command.append("--nothing-needed")
    if at is not None:
        command.extend(["--at", at.isoformat(timespec="minutes")])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=RENDER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise FooterError("footer render timed out") from error
    if result.returncode != 0:
        message = result.stderr.splitlines()
        raise FooterError(message[0] if message else f"footer render exited {result.returncode}")
    return result.stdout.splitlines()


def waiting_bullets(lines: list[str]) -> tuple[list[str], list[str]] | None:
    waiting_start = next((index for index in range(len(lines) - 1, -1, -1)
                          if lines[index] == "Waiting on:"), None)
    if (waiting_start is None or waiting_start < 2
            or lines[waiting_start - 2:waiting_start] != ["", ""]
            or len(lines) < waiting_start + 3 or lines[waiting_start + 1] != ""
            or not all(line.startswith("* ") and line[2:].strip()
                       for line in lines[waiting_start + 2:])):
        return None
    return lines[:waiting_start - 2], [line[2:] for line in lines[waiting_start + 2:]]


def waiting_format_error(bullets: list[str], now: datetime) -> str | None:
    last_kind = 0
    last_eta = (-1, -1)
    for bullet in bullets:
        match = ETA_LEAD.match(bullet)
        if match is None and MIDLINE_ETA.search(bullet):
            return "An ETA appears mid-line. " + WAITING_EXAMPLE
        if match is None and LEADING_TIME.match(bullet):
            return "A leading ETA has a zone or invalid format. " + WAITING_EXAMPLE
        if bullet.startswith("no ETA measured - "):
            kind = 2
        else:
            if match is None:
                kind = 0
            else:
                kind = 1
                if match.group("zone"):
                    return "A leading ETA has a zone. " + WAITING_EXAMPLE
                lead = match.group("time")
                day = match.group("day")
                offset = (("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun").index(day)
                          - now.weekday()) % 7 if day else int(lead.endswith("+1"))
                hour, minute = (int(part) for part in lead.removesuffix("+1").split(":"))
                eta = (offset, hour * 60 + minute)
                if eta < last_eta:
                    return "ETA items are out of time order. " + WAITING_EXAMPLE
                last_eta = eta
        if kind < last_kind:
            return "Waiting on kinds are out of order: user's items first. " + WAITING_EXAMPLE
        last_kind = kind
    return None


def block_reason(instance_dirs: list[str], reply: str,
                 states: dict[str, FooterState]) -> str | None:
    productions = [production for path in instance_dirs if (production := read_production(Path(path))) is not None]
    if not productions or all(states[production.slug] is FooterState.OFF for production in productions):
        return None
    lines = reply_lines(reply)
    waiting = waiting_bullets(lines)
    footer_lines, bullets = waiting if waiting is not None else ([], [])
    if waiting is not None:
        for production in productions:
            now = datetime.now(ZoneInfo(production.zone)).replace(second=0, microsecond=0)
            if (error := waiting_format_error(bullets, now)) is not None:
                return error
    first_comparison: FooterComparison | None = None
    occupied: list[tuple[int, int]] = []
    stamp_positions = [index for index, line in enumerate(footer_lines)
                       if TIME_LINE.fullmatch(line) is not None]
    sections = [footer_lines[start:end] for start, end in zip(
        stamp_positions, [*stamp_positions[1:], len(footer_lines)])]

    def unoccupied_match(candidate: list[str]) -> int | None:
        return next((index for index in range(len(footer_lines) - len(candidate) + 1)
                     if footer_lines[index:index + len(candidate)] == candidate
                     and all(index + len(candidate) <= start or index >= end
                             for start, end in occupied)), None)

    for production in productions:
        if states[production.slug] is FooterState.OFF:
            continue
        now = datetime.now(ZoneInfo(production.zone)).replace(second=0, microsecond=0)
        at, nothing_needed = stamp(lines, now)
        stamp_state = StampState.MISSING if at is None else StampState.PRESENT
        expected = render_footer(production, at or now, at, nothing_needed=nothing_needed)
        if waiting is not None:
            found = unoccupied_match(expected)
            if found is None and len(sections) > 1:
                for section in sections:
                    section_at, section_quiet = stamp(section, now)
                    section_state = StampState.MISSING if section_at is None else StampState.PRESENT
                    if section_state is StampState.MISSING or (section_at, section_quiet) == (at, nothing_needed):
                        continue
                    assert section_at is not None
                    candidate = render_footer(production, section_at, section_at,
                                              nothing_needed=section_quiet)
                    found = unoccupied_match(candidate)
                    if found is not None:
                        expected = candidate
                        break
            if found is not None:
                occupied.append((found, found + len(expected)))
                continue
        if first_comparison is None:
            first_comparison = FooterComparison(production, now, stamp_state, nothing_needed, expected)
    if first_comparison is None:
        return None
    current = (first_comparison.expected if first_comparison.stamp_state is StampState.MISSING else
               render_footer(first_comparison.production, first_comparison.minute, None,
                             nothing_needed=first_comparison.nothing_needed))
    return REASON_HEAD + "\n\n" + "\n".join(current) + "\n\n\nWaiting on:\n\n* <item>"


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in {"on", "off", "status"}:
        print("usage: showrunner_footer.py on|off|status", file=sys.stderr)
        return 2
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    instances = targeted_instances(session_id) if session_id else []
    if not instances:
        print("no production targets this session", file=sys.stderr)
        return 1
    for instance in instances:
        slug = instance.name.removeprefix("showrunner-")
        if argv[0] != "status":
            set_footer_state(slug, FooterState(argv[0]))
        print(f"{slug} footers {footer_state(slug).value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
