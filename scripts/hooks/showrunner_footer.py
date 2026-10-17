"""Read a running production and compare a reply with its rendered footer."""

from __future__ import annotations

import re
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone_name  # noqa: E402

if TYPE_CHECKING:
    from showrunners import Place
    from turn_input import TurnInput


STAMP_WINDOW = timedelta(minutes=5)
NOTHING_NEEDED = " - nothing needed"
DAILIES = Path(__file__).resolve().parent.parent / "production" / "dailies_render.py"
REASON_HEAD = ("Showrunner footer missing or out of date. End this reply with these footer lines, word for word, "
               "then two empty lines, `Waiting on:`, an empty line, and one `* ` bullet per item. A question for the user "
               "is never restated there; while any is open, the first bullet is its count, as below:")
STATUS = re.compile(r"Status: PRODUCTION — ([a-z]*)")
TIME_LINE = re.compile(r"^(\d{2}):(\d{2}) (\S+) update:$")
RENDER_TIMEOUT_SECONDS = 10
TIME = r"(?:[01]\d|2[0-3]):[0-5]\d(?:\+1)?"
ETA_LEAD = re.compile(
    rf"^(?:(?P<day>Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?(?P<time>{TIME})"
    + rf"(?: \((?P<low>{TIME})–(?P<high>{TIME})\))?(?P<zone> [A-Z]{{2,5}})? - "
)
MIDLINE_ETA = re.compile(rf"\bETA\s*:?[ ]*{TIME}\b|{TIME} \({TIME}–{TIME}\)", re.IGNORECASE)
LEADING_TIME = re.compile(rf"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?{TIME}(?:\s|$)")
WAITING_EXAMPLE = ("Waiting on items: the open-questions count first while any is open, then `quota: ` lines, "
                   + "then ETA items soonest first, then no-ETA items. Use `19:45 (18:20–23:55) - startup Phase 16` "
                   + "or `no ETA measured - <item>`; omit the zone.")
# A held quota alert is a status, not a question, so it keeps a line of its own.
QUOTA_LEAD = "quota: "
# User, 2026-10-12: "your messages should just say 'there are {n} open questions for you' - that's it".
ASK_ONCE = ("A question for the user is asked once, in full, when it first comes up. After that the Waiting on "
            + "block never restates it or its choices; list the questions in full only when the user asks what they are. ")


# A turn the user did not start passes with exactly this reply when nothing needs the user.
NOTHING_FOR_USER = "nothing needs the user: reply with -"


# The switch file's content when a conversation pause turned the footer off. A switch turned off
# on purpose is empty, so the pause's resume leaves it off.
PAUSE_MARK = "paused by a conversation pause\n"


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
class PendingReviewedQuestion:
    text: str
    reviewed: str


@dataclass(frozen=True)
class PendingReviewUnavailableQuestion:
    text: str
    question: str
    reason: str


PendingQuestion = PendingReviewedQuestion | PendingReviewUnavailableQuestion


@dataclass(frozen=True)
class QuestionReplyReady:
    pass


@dataclass(frozen=True)
class QuestionReplyBlocked:
    reason: str


class PendingReviewedQuestionRecord(TypedDict):
    kind: Literal["reviewed"]
    text: str
    reviewed: str


class PendingReviewUnavailableQuestionRecord(TypedDict):
    kind: Literal["review unavailable"]
    text: str
    question: str
    reason: str


class PendingReviewedRecord(TypedDict):
    items: list[PendingReviewedQuestionRecord | PendingReviewUnavailableQuestionRecord]


def pending_reviewed_path(instance_dir: Path) -> Path:
    return instance_dir / "pending_reviewed.json"


def read_pending_reviewed(instance_dir: Path) -> list[PendingQuestion]:
    path = pending_reviewed_path(instance_dir)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(raw, dict):
        raise FooterError(f"{path}: expected an object")
    entries = cast(dict[str, object], raw).get("items")
    if not isinstance(entries, list):
        raise FooterError(f"{path}: expected items")
    items: list[PendingQuestion] = []
    for raw_item in cast(list[object], entries):
        if not isinstance(raw_item, dict):
            raise FooterError(f"{path}: expected a question object")
        item = cast(dict[str, object], raw_item)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise FooterError(f"{path}: expected item text")
        if item.get("kind") == "reviewed":
            reviewed = item.get("reviewed")
            if not isinstance(reviewed, str) or not reviewed.strip():
                raise FooterError(f"{path}: expected reviewed question")
            items.append(PendingReviewedQuestion(text, reviewed))
        elif item.get("kind") == "review unavailable":
            question, reason = item.get("question"), item.get("reason")
            if not isinstance(question, str) or not question.strip() or not isinstance(reason, str):
                raise FooterError(f"{path}: expected unavailable question and reason")
            items.append(PendingReviewUnavailableQuestion(text, question, reason))
        else:
            raise FooterError(f"{path}: unknown question kind")
    return items


def add_pending_reviewed(instance_dir: Path, items: list[PendingQuestion]) -> None:
    combined = read_pending_reviewed(instance_dir)
    for item in items:
        if item not in combined:
            combined.append(item)
    if not combined:
        return
    record: PendingReviewedRecord = {"items": []}
    for item in combined:
        if isinstance(item, PendingReviewedQuestion):
            record["items"].append({"kind": "reviewed", "text": item.text, "reviewed": item.reviewed})
        else:
            record["items"].append({"kind": "review unavailable", "text": item.text,
                                    "question": item.question, "reason": item.reason})
    path = pending_reviewed_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    _ = scratch.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    _ = scratch.replace(path)


def delete_pending_reviewed(instance_dir: Path) -> None:
    pending_reviewed_path(instance_dir).unlink(missing_ok=True)


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
    return state_root() / "outstanding" / f"{slug}.json"


class ReportItem(TypedDict):
    """Something the user asked to be told about, such as "tell me when hanadocs is back"."""
    text: str
    until: str


def reports_path(slug: str) -> Path:
    return state_root() / "reports" / f"{slug}.json"


def live_reports(slug: str, now: datetime) -> list[ReportItem]:
    """The production's reports whose `until` has not passed; a naive `until` is in the user's zone."""
    try:
        raw = cast(object, json.loads(reports_path(slug).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise FooterError(f"{slug}: reports unreadable: {error}") from error
    if not isinstance(raw, list):
        raise FooterError(f"{slug}: expected a reports list")
    live: list[ReportItem] = []
    for entry in cast(list[object], raw):
        fields = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        text, until = fields.get("text"), fields.get("until")
        if not isinstance(text, str) or not isinstance(until, str):
            raise FooterError(f"{slug}: a report needs text and until")
        try:
            ends = datetime.fromisoformat(until)
        except ValueError as error:
            raise FooterError(f"{slug}: report until {until!r} is not ISO") from error
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=ZoneInfo(user_zone_name()))
        if ends > now:
            live.append(ReportItem(text=text, until=until))
    return live


def state_root() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def switch_path(slug: str) -> Path:
    return state_root() / "footers-off" / slug


def review_pause_path(slug: str) -> Path:
    return state_root() / "review-paused" / f"{slug}.json"


def footer_state(slug: str) -> FooterState:
    return FooterState.OFF if switch_path(slug).exists() else FooterState.ON


def set_footer_state(slug: str, state: FooterState) -> None:
    """Switch the footer on purpose; `off` writes the switch empty, which clears any pause mark."""
    path = switch_path(slug)
    if state is FooterState.OFF:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def pause_footer(slug: str) -> None:
    """Turn the footer off for a conversation pause, with the mark `unpause_footer` looks for."""
    path = switch_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(PAUSE_MARK, encoding="utf-8")


def footer_paused(slug: str) -> bool:
    """Whether the footer is off because a conversation pause turned it off, and only that."""
    try:
        return switch_path(slug).read_text(encoding="utf-8") == PAUSE_MARK
    except FileNotFoundError:
        return False


def unpause_footer(slug: str) -> bool:
    """Turn the footer back on if the pause's mark is still there; whether it came back."""
    if not footer_paused(slug):
        return False
    switch_path(slug).unlink(missing_ok=True)
    return True


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
        # The user's zone setting; the doc's own User zone line is no longer read.
        zone = user_zone_name()
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


def place_of(slug: str) -> Place:
    """Where `slug` sits among the showrunners: the root above it, or the productions below it."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The tree lives with the production scripts. They import this module, so this one imports late.
    import showrunners
    return showrunners.place_of(slug)


def render_footer(production: Production, minute: datetime, at: datetime | None, place: Place, *,
                  nothing_needed: bool) -> list[str]:
    command = [sys.executable, str(DAILIES), "--footer", "--zone", production.zone,
               "--outstanding", str(outstanding_path(production.slug)), "--doc", str(production.doc)]
    for slug in place.below:
        command.extend(["--below", str(outstanding_path(slug))])
    if place.root:
        command.extend(["--reports-to", place.root])
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


def count_sentence(count: int) -> str:
    return "There is 1 open question for you." if count == 1 else f"There are {count} open questions for you."


def open_questions(productions: list[Production], below: dict[str, list[str]]) -> int | None:
    """How many items now wait on the user across these productions; None when a list cannot be read.

    `below` names the productions under each root showrunner. A root's count covers them, since
    the root is where the user sees everything below it. An item the user deferred is not counted until its `after`
    time, as the footer's own `nothing needed` is decided.
    """
    # One user, one zone: a lower production's `after` is judged in the zone of the one that counts it.
    zones: dict[str, str] = {}
    for production in productions:
        for slug in (production.slug, *below.get(production.slug, [])):
            _ = zones.setdefault(slug, production.zone)
    count = 0
    for slug, zone in zones.items():
        path = outstanding_path(slug)
        if not path.exists():
            continue
        try:
            entries = cast(object, json.loads(path.read_text(encoding="utf-8")))
            now = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")
        except (OSError, ValueError, KeyError):
            return None
        if not isinstance(entries, list):
            return None
        for entry in cast(list[object], entries):
            after = cast(dict[str, object], entry).get("after") if isinstance(entry, dict) else None
            # Both are `YYYY-MM-DDTHH:MM` in that zone, so they compare as text.
            if not isinstance(after, str) or after <= now:
                count += 1
    return count


def open_questions_error(bullets: list[str], expected: int | None) -> str | None:
    """Why the block's lines for the user break the count-only rule; None when they keep it.

    An unreadable list has no count to check, and the footer render reports that fault itself.
    """
    if expected is None:
        return None
    user_lines = [bullet for bullet in bullets
                  if ETA_LEAD.match(bullet) is None and not bullet.startswith(("no ETA measured - ", QUOTA_LEAD))]
    wanted = [count_sentence(expected)] if expected else []
    if user_lines == wanted and (not wanted or bullets[0] == wanted[0]):
        return None
    if not wanted:
        return (ASK_ONCE + f"Nothing waits on the user now, so remove `* {user_lines[0]}`. "
                + f"A held quota alert begins `{QUOTA_LEAD}`.")
    return (ASK_ONCE + f"Write exactly `* {wanted[0]}` as the first bullet, once, and no other line for the user. "
            + f"A held quota alert begins `{QUOTA_LEAD}`.")


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
            return "Waiting on kinds are out of order: the count and `quota: ` lines first. " + WAITING_EXAMPLE
        last_kind = kind
    return None


def normalised(text: str) -> str:
    return " ".join(text.split())


def choice_comparison_text(text: str) -> str:
    return normalised(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text))


def pending_question_text(item: PendingQuestion) -> str:
    return item.reviewed if isinstance(item, PendingReviewedQuestion) else item.question


def instance_slugs(instance_dirs: list[str]) -> set[str]:
    return {Path(instance).name.removeprefix("showrunner-") for instance in instance_dirs}


def with_below(slugs: set[str]) -> set[str]:
    """These productions and every one below them, as a root's count covers them."""
    return slugs.union(*(place_of(slug).below for slug in slugs))


def outstanding_texts(slugs: set[str]) -> list[str]:
    """Every text, question and reviewed question in these productions' open_for_user lists."""
    texts: list[str] = []
    for slug in sorted(slugs):
        try:
            raw = cast(object, json.loads(outstanding_path(slug).read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        if not isinstance(raw, list):
            raise FooterError(f"{slug}: expected outstanding list")
        for entry in cast(list[object], raw):
            if isinstance(entry, dict):
                fields = cast(dict[str, object], entry)
                texts.extend(value for key in ("text", "question", "reviewed")
                             if isinstance(value := fields.get(key), str))
    return texts


def carries_registered(instance_dirs: list[str], reply: str) -> bool:
    """Whether the reply names an item registered for the user: an open item or a report."""
    slugs = with_below(instance_slugs(instance_dirs))
    now = datetime.now(ZoneInfo(user_zone_name()))
    texts = [*(pending_question_text(item) for instance in instance_dirs
               for item in read_pending_reviewed(Path(instance))),
             *outstanding_texts(slugs),
             *(report["text"] for slug in sorted(slugs) for report in live_reports(slug, now))]
    compared = normalised(reply)
    return any(text.strip() and normalised(text) in compared for text in texts)


def check_reviewed_text(instance_dirs: list[str], reply: str) -> QuestionReplyReady | QuestionReplyBlocked:
    accepted: list[str] = []
    slugs: set[str] = set()
    for instance in instance_dirs:
        for item in read_pending_reviewed(Path(instance)):
            question = pending_question_text(item)
            accepted.append(question)
            if normalised(question) not in normalised(reply):
                return QuestionReplyBlocked(f"Question {item.text!r}: paste its reviewed question into this reply:\n{question}")
        slugs.add(Path(instance).name.removeprefix("showrunner-"))
    notify_scripts = str(Path(__file__).resolve().parent.parent / "notify")
    if notify_scripts not in sys.path:
        sys.path.insert(0, notify_scripts)
    if TYPE_CHECKING:
        from ..notify import reply_asks
    else:
        import reply_asks
    asks = reply_asks.user_reply_questions(reply)
    if not asks.sentences and not asks.has_choices:
        return QuestionReplyReady()
    accepted.extend(outstanding_texts(with_below(slugs)))
    registered = [reply_asks.question_prose(choice_comparison_text(question)) for question in accepted]
    for sentence in (*asks.sentences, *asks.choice_lines):
        compared = reply_asks.question_prose(choice_comparison_text(sentence))
        if compared and not any(item and (compared in item or item in compared) for item in registered):
            return QuestionReplyBlocked(f"Unregistered ask {sentence!r}: record the item with "
                                        + "update_registration.py footer --state and paste its reviewed question "
                                        + "into this reply.")
    return QuestionReplyReady()


def block_reason(instance_dirs: list[str], reply: str,
                 states: dict[str, FooterState], *, skip_footer: bool = False,
                 turn: TurnInput | None = None) -> str | None:
    if not instance_dirs:
        return None
    checked = check_reviewed_text(instance_dirs, reply)
    if isinstance(checked, QuestionReplyBlocked):
        return checked.reason
    # A turn the user did not start reaches the user only with an auto-mode denial or a registered
    # item; otherwise the reply is exactly "-", which carries no footer.
    if turn is not None and turn.machine_only:
        if reply.strip() == "-":
            return None
        if not turn.denied and not carries_registered(instance_dirs, reply):
            return NOTHING_FOR_USER
    reason = footer_block_reason(instance_dirs, reply, states, skip_footer=skip_footer)
    if reason is None:
        for instance in instance_dirs:
            delete_pending_reviewed(Path(instance))
    return reason


def counts_line(productions: list[Production], questions: int) -> str | None:
    """The counts line a footers-off reply ends with: each production's counts and the open questions."""
    production_scripts = str(DAILIES.parent)
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    # The counts read the production scripts, which import this module, so this one imports late.
    import footer_counts

    counts = footer_counts.total([footer_counts.read_counts(production.doc) for production in productions])
    return footer_counts.counts_line(counts._replace(questions=questions))


def counts_block_reason(productions: list[Production], reply: str) -> str | None:
    """With footers off, the reply's last line is the counts line, and none when nothing is counted."""
    if reply.strip() == "-":
        return None
    counted = open_questions(productions, {production.slug: place_of(production.slug).below
                                           for production in productions})
    # A list that cannot be read leaves the question count unknown, so the line is not checked.
    if counted is None:
        return None
    expected = counts_line(productions, counted)
    import footer_counts

    lines = [line for line in reply_lines(reply) if line.strip()]
    last = lines[-1] if lines else ""
    if expected is None:
        return (f"Footers are off and nothing waits on the user: remove the last line `{last}`."
                if last.startswith(footer_counts.COUNTS_HEAD) else None)
    return None if last == expected else f"Footers are off: end the reply with this line, on its own:\n\n{expected}"


def footer_block_reason(instance_dirs: list[str], reply: str,
                       states: dict[str, FooterState], *, skip_footer: bool) -> str | None:
    if skip_footer:
        return None
    productions = [production for path in instance_dirs if (production := read_production(Path(path))) is not None]
    if not productions:
        return None
    if all(states[production.slug] is FooterState.OFF for production in productions):
        return counts_block_reason(productions, reply)
    lines = reply_lines(reply)
    waiting = waiting_bullets(lines)
    footer_lines, bullets = waiting if waiting is not None else ([], [])
    places = {production.slug: place_of(production.slug) for production in productions}
    counted = open_questions(productions, {slug: place.below for slug, place in places.items()})
    if waiting is not None:
        for production in productions:
            now = datetime.now(ZoneInfo(production.zone)).replace(second=0, microsecond=0)
            if (error := waiting_format_error(bullets, now)) is not None:
                return error
        if (error := open_questions_error(bullets, counted)) is not None:
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
        place = places[production.slug]
        expected = render_footer(production, at or now, at, place, nothing_needed=nothing_needed)
        if waiting is not None:
            found = unoccupied_match(expected)
            if found is None and len(sections) > 1:
                for section in sections:
                    section_at, section_quiet = stamp(section, now)
                    section_state = StampState.MISSING if section_at is None else StampState.PRESENT
                    if section_state is StampState.MISSING or (section_at, section_quiet) == (at, nothing_needed):
                        continue
                    assert section_at is not None
                    candidate = render_footer(production, section_at, section_at, place,
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
                             places[first_comparison.production.slug],
                             nothing_needed=first_comparison.nothing_needed))
    sample = f"* {count_sentence(counted)}\n* <item>" if counted else "* <item>"
    return REASON_HEAD + "\n\n" + "\n".join(current) + "\n\n\nWaiting on:\n\n" + sample


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
