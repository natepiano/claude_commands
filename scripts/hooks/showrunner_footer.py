"""Read a running production and compare a reply with its rendered footer."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
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


class FooterError(Exception):
    """The shared footer renderer could not provide an authoritative footer."""


@dataclass(frozen=True)
class Production:
    slug: str
    doc: Path
    zone: str
    next_due: int | None


@dataclass(frozen=True)
class FooterComparison:
    production: Production
    minute: datetime
    stamped: datetime | None
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
        next_due = int(state["NEXT_DUE"]) if state.get("ENABLED") == "1" else None
        return Production(instance_dir.name.removeprefix("showrunner-"), doc, zone, next_due)
    except (OSError, ValueError, ZoneInfoNotFoundError, KeyError):
        return None


def next_run_text(next_due: int | None, minute: datetime, zone: ZoneInfo) -> str | None:
    if next_due is None:
        return None
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
    next_run = next_run_text(production.next_due, minute, ZoneInfo(production.zone))
    if next_run is not None:
        command.extend(["--next-run", next_run])
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


def block_reason(instance_dirs: list[str], reply: str) -> str | None:
    productions = [production for path in instance_dirs if (production := read_production(Path(path))) is not None]
    if not productions:
        return None
    lines = reply_lines(reply)
    waiting_start = next((index for index in range(len(lines) - 1, -1, -1)
                          if lines[index] == "Waiting on:"), None)
    waiting_is_valid = (waiting_start is not None and waiting_start >= 2
                        and lines[waiting_start - 2:waiting_start] == ["", ""]
                        and len(lines) >= waiting_start + 3
                        and lines[waiting_start + 1] == ""
                        and all(line.startswith("* ") and line[2:].strip()
                                for line in lines[waiting_start + 2:]))
    footer_lines = lines[:waiting_start - 2] if waiting_is_valid and waiting_start is not None else []
    first_comparison: FooterComparison | None = None
    for production in productions:
        now = datetime.now(ZoneInfo(production.zone)).replace(second=0, microsecond=0)
        at, nothing_needed = stamp(lines, now)
        expected = render_footer(production, at or now, at, nothing_needed=nothing_needed)
        if waiting_is_valid and len(footer_lines) >= len(expected) and footer_lines[-len(expected):] == expected:
            return None
        if first_comparison is None:
            first_comparison = FooterComparison(production, now, at, nothing_needed, expected)
    if first_comparison is None:
        return None
    current = (first_comparison.expected if first_comparison.stamped is None else
               render_footer(first_comparison.production, first_comparison.minute, None,
                             nothing_needed=first_comparison.nothing_needed))
    return REASON_HEAD + "\n\n" + "\n".join(current) + "\n\n\nWaiting on:\n\n* <item>"
