#!/usr/bin/env python3
"""Register a showrunner's update jobs and write its production time and log.

`open` lists what waits on the user and `next` prints the one item /next question shows, both in
urgency.py's order. `mark-important --item <words>` marks an item the user called important.
`ask` files one question without a reply to the user (/unit:question, /showrunner:question), and
`answer --item <words>` removes one item and names who asked it, so the answer goes back there.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from production_units import Production, read_production
from production_units import Refusal, cell_value, unit_table
from merge_checkpoint import NoMerge, merge_branch_history, report

if TYPE_CHECKING:
    from ..hooks.showrunner_footer import ReportItem, reports_path
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    from showrunner_footer import ReportItem, reports_path
from scripts.production import showrunners
import showrunner_tree
from dailies_render import (UNREADABLE_LIST, InputError, plumbing, read_outstanding,
    DeferredUntil, Outstanding, OutstandingReminder, Ranking, ReviewedOutstandingQuestion,
    ReviewUnavailableOutstandingQuestion)
import urgency
from atomic_file import write_text_atomic

if TYPE_CHECKING:
    from ..usage import agents_log
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.usage import agents_log

from scripts.lib import names


class InstancePresent(NamedTuple):
    output: str


class InstanceAbsent(NamedTuple):
    pass


class OutstandingItem(TypedDict):
    since: str
    text: str
    after: NotRequired[str]
    question: NotRequired[str]
    reviewed: NotRequired[str]
    review_unavailable: NotRequired[str]
    urgency: NotRequired[str]
    due: NotRequired[str]
    marked: NotRequired[str]
    follows: NotRequired[str]
    asked_by: NotRequired[str]


class QuestionRegistration(TypedDict):
    time: str
    text: str
    reviewed: NotRequired[str]
    question: NotRequired[str]


class AnsweredItem(TypedDict):
    time: str
    slug: str
    text: str
    question: NotRequired[str]
    asked_by: NotRequired[str]


@dataclass(frozen=True)
class UnitAsker:
    unit: str


@dataclass(frozen=True)
class ShowrunnerAsker:
    pass


@dataclass(frozen=True)
class AskerNotRecorded:
    reason: str


@dataclass(frozen=True)
class OpenItemWithoutAsker:
    pass


QuestionAsker = UnitAsker | ShowrunnerAsker | AskerNotRecorded | OpenItemWithoutAsker


def question_asker(fields: OutstandingItem | AnsweredItem,
                   missing: OpenItemWithoutAsker | AskerNotRecorded) -> QuestionAsker:
    name = fields.get("asked_by")
    if not isinstance(name, str) or not name.strip():
        return missing
    return ShowrunnerAsker() if name == SHOWRUNNER_ASKER else UnitAsker(name)


def relay_line(production: Production, slug: str, asker: QuestionAsker) -> str:
    owner = "this production" if slug == production.slug else f"{slug}, below this production"
    if isinstance(asker, OpenItemWithoutAsker):
        return f"relay: none; the item belongs to {owner}"
    if isinstance(asker, AskerNotRecorded):
        return f"relay: none; {asker.reason}; the item belongs to {owner}"
    if slug != production.slug:
        if isinstance(asker, UnitAsker):
            name = asker.unit
        else:
            loaded = names.runtime_names()
            name = loaded.job("showrunner") if isinstance(loaded, names.Names) else "requesting role (display name unavailable)"
        return f"relay: to the showrunner of {slug}, for {name}"
    if isinstance(asker, ShowrunnerAsker):
        return "relay: none; this showrunner asked it"
    return f"relay: to {asker.unit}"


def answered_items(outstanding: Path, now: datetime) -> list[AnsweredItem]:
    """Read the retained answers, converting JSON fields before replaying them."""
    history = outstanding.with_suffix(".answered.jsonl")
    if not history.exists():
        return []
    retained: list[AnsweredItem] = []
    for line in history.read_text(encoding="utf-8").splitlines():
        try:
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                continue
            fields = cast(dict[str, object], value)
            stamp, slug, text = fields.get("time"), fields.get("slug"), fields.get("text")
            if not isinstance(stamp, str) or not isinstance(slug, str) or not isinstance(text, str):
                continue
            dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dated.tzinfo is None or dated < now - timedelta(hours=48):
                continue
            entry = AnsweredItem(time=stamp, slug=slug, text=text)
            question = fields.get("question")
            if isinstance(question, str):
                entry["question"] = question
            asker = fields.get("asked_by")
            if isinstance(asker, str) and asker.strip():
                entry["asked_by"] = asker
            retained.append(entry)
        except ValueError:
            continue
    return retained


def record_answer(outstanding: Path, slug: str, item: OutstandingItem, now: datetime) -> None:
    retained = [entry for entry in answered_items(outstanding, now) if entry["text"] != item["text"]]
    entry = AnsweredItem(time=now.isoformat(), slug=slug, text=item["text"])
    if "question" in item:
        entry["question"] = item["question"]
    if "asked_by" in item:
        entry["asked_by"] = item["asked_by"]
    write_text_atomic(outstanding.with_suffix(".answered.jsonl"),
                      "".join(json.dumps(saved) + "\n" for saved in [*retained, entry]))


def replay_answer(production: Production, item: AnsweredItem) -> None:
    dated = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
    print(f"already answered {dated:%a %b %d %H:%M}")
    print(relay_line(production, item["slug"],
                     question_asker(item, AskerNotRecorded("asked_by not recorded in answered record"))))
    report("answer", "ok", f"{item['slug']}: {item['text']}")


class UnitJudgment(NamedTuple):
    unit: str
    phase: str
    wait: str


class OpenReminder(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


class QuestionForReview(NamedTuple):
    text: str
    after: str
    question: str
    rank: Ranking = Ranking()


class OpenQuestionWithoutText(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


OpenForUserItem = OpenReminder | QuestionForReview | OpenQuestionWithoutText


class KeepOutstanding(NamedTuple):
    pass


class ReplaceOutstanding(NamedTuple):
    items: tuple[OpenForUserItem, ...]


class KeepReports(NamedTuple):
    pass


class ReplaceReports(NamedTuple):
    items: tuple[ReportItem, ...]


class ShowrunnerState(NamedTuple):
    units: tuple[UnitJudgment, ...]
    merges_held: tuple[str, ...]
    outstanding: KeepOutstanding | ReplaceOutstanding
    reports: KeepReports | ReplaceReports = KeepReports()
    # What the showrunner owes and what sets each off; a resume acts on it first.
    owed: tuple[str, ...] = ()


class RegistrationFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def command(step: str, arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RegistrationFailure(step, result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def notifier(arguments: list[str]) -> str:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    return command("notifier", ["zsh", str(script), *arguments])


def instance(name: str) -> InstancePresent | InstanceAbsent:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    result = subprocess.run(["zsh", str(script), "status", name], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return InstancePresent(result.stdout.strip())
    if "no instance" in result.stdout + result.stderr or "no such instance" in result.stdout + result.stderr:
        return InstanceAbsent()
    raise RegistrationFailure("notifier", result.stderr.strip() or result.stdout.strip() or f"status exit {result.returncode}")


class ScheduledUpdates(NamedTuple):
    minutes: int
    aligned: bool


DEFAULT_MINUTES = 15


class UpdatesOnDemand(NamedTuple):
    """The doc's Updates line reads `on demand`, or there is none: a dailies runs only when the user asks for one."""


def update_schedule(lines: list[str]) -> ScheduledUpdates | UpdatesOnDemand:
    line = next((line for line in lines if line.startswith("- **Updates:**")), "")
    match = re.search(r"every (\d+) minutes", line)
    # Quiet by default (user, 2026-10-13): only a line asking for a schedule makes one.
    if match is None or re.search(r"\bon demand\b", line):
        return UpdatesOnDemand()
    minutes = int(match.group(1))
    if minutes <= 0:
        raise RegistrationFailure("prompt", "Updates interval must be positive")
    return ScheduledUpdates(minutes, "on the hour" in line)


def scheduled_prompt(minutes: int, zone: str, doc: Path, slug: str) -> str:
    source = Path(__file__).resolve().parents[2] / "commands/showrunner/produce.md"
    document = source.read_text(encoding="utf-8")
    if "The prompt:\n" not in document:
        raise RegistrationFailure("register", "The prompt: marker is missing")
    tail = document.split("The prompt:\n", 1)[1]
    if "\n**A tick**" not in tail:
        raise RegistrationFailure("register", "**A tick** marker is missing")
    template = tail.split("\n**A tick**", 1)[0]
    prompt = "\n".join(line.removeprefix("> ").removeprefix(">") for line in template.strip().splitlines())
    prompt = prompt.replace("<N>", str(minutes)).replace("<zone>", zone).replace("<production doc>", str(doc))
    prompt = prompt.replace("<STATE_DIR>", str(showrunners.session_state_dir(slug)))
    return prompt


def adopt_moved_state(production: Production) -> list[str]:
    """Copy the scratchpad state the state directory lacks and rewrite a tick prompt that differs.

    Every registration and dailies call runs this first, so a showrunner started before the state
    moved out of its scratchpad needs no step (showrunner, 2026-10-19). A failure only skips it.
    """
    done: list[str] = []
    try:
        copied = showrunners.adopt_session_state(production.slug)
        if copied:
            done.append(f"copied {', '.join(copied)} to {showrunners.session_state_dir(production.slug)}")
        prompt_file = showrunners.SHOWRUNNER_STATE_DIR / production.slug / "prompt.txt"
        if not prompt_file.exists():
            return done
        schedule = update_schedule(production.doc.read_text(encoding="utf-8").splitlines())
        minutes = schedule.minutes if isinstance(schedule, ScheduledUpdates) else DEFAULT_MINUTES
        prompt = scheduled_prompt(minutes, str(production.zone), production.doc, production.slug)
        if prompt_file.read_text(encoding="utf-8") != prompt:
            _ = prompt_file.write_text(prompt, encoding="utf-8")
            done.append(f"rewrote {prompt_file} from produce.md")
    except (OSError, UnicodeError, RegistrationFailure) as error:
        done.append(f"state move skipped: {error}")
    return done


def register(production: Production) -> None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        raise RegistrationFailure("register", "CLAUDE_CODE_SESSION_ID is required")
    # An account move restarts a showrunner on its own, so a name that differs only warns; attach refuses it
    # for a new production.
    if why := showrunners.name_refusal(production.slug, showrunners.session_name(session_id)):
        report("register", "warn", why)
    try:
        agents_log.append(session_id, "showrunner", None, datetime.now(timezone.utc))
    except Exception as exc:
        # Recording must never interrupt registration.
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    schedule = update_schedule(lines)
    # An on-demand timer is made at the default interval and stopped, so it stays the showrunner's record.
    minutes, aligned = ((schedule.minutes, schedule.aligned) if isinstance(schedule, ScheduledUpdates)
                        else (DEFAULT_MINUTES, False))
    prompt = scheduled_prompt(minutes, str(production.zone), production.doc, production.slug)
    relative = production.doc.relative_to(production.checkout)
    dirty = command("register", ["git", "status", "--porcelain", "--", str(relative)], cwd=production.checkout)
    if dirty:
        raise RegistrationFailure("register", "the production doc has uncommitted edits; commit or discard them first")
    # Nothing registers the showrunner by name: its update timer, made below, is its one record.
    state_root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    prompt_file = state_root / production.slug / "prompt.txt"
    updates = f"showrunner-{production.slug}"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    if not prompt_file.exists() or prompt_file.read_text(encoding="utf-8") != prompt:
        _ = prompt_file.write_text(prompt, encoding="utf-8")
    check = f"zsh {Path.home()}/.claude/scripts/production/production_check.sh {production.doc}"
    _ = notifier(["new", updates, "--to", f"session:{session_id}", "--every", str(minutes),
              "--prompt-file", str(prompt_file), "--from", f"showrunner-timer-{production.slug}",
              "--check", check, *(["--aligned"] if aligned else [])])
    if isinstance(schedule, UpdatesOnDemand):
        _ = notifier(["stop", updates])
    if isinstance(instance("stall-watch"), InstanceAbsent):
        run = f"{Path.home()}/.claude/scripts/lib/py {Path.home()}/.claude/scripts/production/stall_watch.py"
        _ = notifier(["new", "stall-watch", "--every", "1", "--run", run])
    if isinstance(schedule, UpdatesOnDemand):
        report("register", "ok", f"{updates} on demand: no scheduled dailies")
        return
    status = instance(updates)
    if isinstance(status, InstanceAbsent):
        raise RegistrationFailure("register", f"{updates} missing after new")
    next_due = next((line for line in status.output.splitlines() if line.startswith("next_due=")), "")
    if not next_due:
        raise RegistrationFailure("register", f"{updates} status lacks next_due")
    report("register", "ok", f"{updates} {next_due}")


def production_time(production: Production, stamp: str) -> str:
    zone = production.zone
    if re.fullmatch(r"\d{1,2}:\d{2}", stamp):
        today = datetime.now(zone).date()
        hour, minute = (int(part) for part in stamp.split(":"))
        moment = datetime(today.year, today.month, today.day, hour, minute, tzinfo=zone)
    else:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return moment.strftime("%H:%M %Z")


def state_error(step: str, path: Path, key: str, expected: str) -> RegistrationFailure:
    return RegistrationFailure(step, f"{path}: {key} must be {expected}")


def judgment_state(path: Path | None, step: str) -> ShowrunnerState:
    if path is None:
        return ShowrunnerState((), (), KeepOutstanding())
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise state_error(step, path, "root", "an object")
    fields = cast(dict[str, object], value)
    for key in fields:
        if key not in {"units", "merges_held", "open_for_user", "report_to_user", "next"}:
            raise state_error(step, path, key, "a known key")
    raw_units = fields.get("units", [])
    if not isinstance(raw_units, list):
        raise state_error(step, path, "units", "a list")
    units: list[UnitJudgment] = []
    for index, raw in enumerate(cast(list[object], raw_units)):
        key = f"units[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        unit = cast(dict[str, object], raw)
        for field in unit:
            if field not in {"unit", "phase", "wait"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        for field in ("unit", "phase", "wait"):
            if field in unit and not isinstance(unit[field], str):
                raise state_error(step, path, f"{key}.{field}", "text")
        units.append(UnitJudgment(cast(str, unit.get("unit", "")),
                                  cast(str, unit.get("phase", "not stated")),
                                  cast(str, unit.get("wait", "not stated"))))
    raw_held = fields.get("merges_held", [])
    if not isinstance(raw_held, list) or not all(isinstance(item, str) for item in cast(list[object], raw_held)):
        raise state_error(step, path, "merges_held", "a list of text")
    raw_next = fields.get("next", [])
    if not isinstance(raw_next, list) or not all(isinstance(item, str) and item.strip() and "\n" not in item
                                                 for item in cast(list[object], raw_next)):
        raise state_error(step, path, "next", "a list of one-line text")
    outstanding: KeepOutstanding | ReplaceOutstanding = KeepOutstanding()
    if "open_for_user" in fields:
        raw_open = fields["open_for_user"]
        if not isinstance(raw_open, list):
            raise state_error(step, path, "open_for_user", "a list")
        items: list[OpenForUserItem] = []
        for index, raw in enumerate(cast(list[object], raw_open)):
            key = f"open_for_user[{index}]"
            if isinstance(raw, str):
                if not raw.strip() or "\n" in raw:
                    raise state_error(step, path, key, "one nonempty line of text")
                items.append(OpenReminder(raw, ""))
                continue
            if not isinstance(raw, dict):
                raise state_error(step, path, key, "text or an object")
            item = cast(dict[str, object], raw)
            for field in item:
                if field not in {"text", "after", "question", "urgency", "due", "follows"}:
                    raise state_error(step, path, f"{key}.{field}", "a known key")
            rank = item_ranking(item, path, step, key)
            if not isinstance(item.get("text"), str) or not cast(str, item["text"]).strip() or "\n" in cast(str, item["text"]):
                raise state_error(step, path, f"{key}.text", "one nonempty line of text")
            after = item.get("after", "")
            if not isinstance(after, str) or (after and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", after) is None):
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM")
            try:
                if after:
                    _ = datetime.fromisoformat(after)
            except ValueError:
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM") from None
            if "question" in item:
                question = item["question"]
                if not isinstance(question, str) or not question.strip():
                    raise state_error(step, path, f"{key}.question", "nonempty text")
                items.append(QuestionForReview(cast(str, item["text"]), after, question, rank))
            else:
                items.append(OpenQuestionWithoutText(cast(str, item["text"]), after, rank))
        outstanding = ReplaceOutstanding(tuple(items))
    return ShowrunnerState(tuple(units), tuple(cast(list[str], raw_held)), outstanding,
                           report_items(fields, path, step), tuple(cast(list[str], raw_next)))


def item_ranking(item: dict[str, object], path: Path, step: str, key: str) -> Ranking:
    """An item's `urgency` (user, blocks, soon, normal or later), `due` (with soon) and `follows`
    (the text of the item it comes after); none of them is a normal item."""
    follows = item.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise state_error(step, path, f"{key}.follows", "one line of text")
    level, due = item.get("urgency", ""), item.get("due", "")
    if not isinstance(level, str) or not isinstance(due, str):
        raise state_error(step, path, f"{key}.urgency", "text")
    try:
        return Ranking(urgency.read(level, due), follows.strip())
    except urgency.UrgencyError:
        raise state_error(step, path, f"{key}.urgency", "user, blocks, soon with `due`, normal or later") from None


def report_items(fields: dict[str, object], path: Path, step: str) -> KeepReports | ReplaceReports:
    """`report_to_user`: what the user asked to be told about, each until an ISO time."""
    if "report_to_user" not in fields:
        return KeepReports()
    raw_reports = fields["report_to_user"]
    if not isinstance(raw_reports, list):
        raise state_error(step, path, "report_to_user", "a list")
    reports: list[ReportItem] = []
    for index, raw in enumerate(cast(list[object], raw_reports)):
        key = f"report_to_user[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        item = cast(dict[str, object], raw)
        for field in item:
            if field not in {"text", "until"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        text, until = item.get("text"), item.get("until")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise state_error(step, path, f"{key}.text", "one nonempty line of text")
        if not isinstance(until, str):
            raise state_error(step, path, f"{key}.until", "an ISO time")
        try:
            _ = datetime.fromisoformat(until)
        except ValueError:
            raise state_error(step, path, f"{key}.until", "an ISO time") from None
        reports.append(ReportItem(text=text, until=until))
    return ReplaceReports(tuple(reports))


def outstanding_file(slug: str) -> Path:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    return root / "outstanding" / f"{slug}.json"


def outstanding_path(production: Production) -> Path:
    return outstanding_file(production.slug)


@contextmanager
def outstanding_lock(outstanding: Path) -> Generator[None]:
    """Hold the list's lock while it is read and written: a unit's `ask` and the showrunner's
    `questions` can run at the same time. Never take it twice in one process: flock does not nest."""
    outstanding.parent.mkdir(parents=True, exist_ok=True)
    with outstanding.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def stored_entries(outstanding: Path) -> list[OutstandingItem]:
    """The list as stored, each entry kept whole; read_outstanding checks it first."""
    if not outstanding.exists():
        return []
    _ = read_outstanding(outstanding)
    return cast(list[OutstandingItem], json.loads(outstanding.read_text(encoding="utf-8")))


# An item can wait longer than a week, so the day is named in full.
WHEN = "%a %b %d %H:%M"


class OpenItem(NamedTuple):
    slug: str
    item: Outstanding

    def line(self) -> str:
        """The item in full, led by its production and any level it has."""
        text = self.item.text if isinstance(self.item, OutstandingReminder) else self.item.question
        level = urgency.cell(self.item.rank.urgency)
        asker = "" if isinstance(self.item, OutstandingReminder) or not self.item.asked_by else f", asked by {self.item.asked_by}"
        return f"{self.slug}{f' [{level}]' if level else ''}, since {self.item.since:{WHEN}}{asker}: {text}"


class OpenLists(NamedTuple):
    due: list[urgency.Entry[OpenItem]]
    deferred: list[OpenItem]
    unreadable: list[str]


def question_queue(production: Production, below: bool = False) -> str:
    """The name urgency.py pins the question /next showed under."""
    return urgency.scope_queue(f"question-{production.slug}", below)


def item_key(slug: str, text: str) -> str:
    return f"{slug}: {text}"


def open_lists(production: Production, below: bool = False) -> OpenLists:
    """What waits on the user now, in urgency.py's order, then what the user deferred and the lists not read.

    The production's own list, and with `below` the list of each production below it.
    """
    now = datetime.now(production.zone).replace(tzinfo=None)
    due: list[urgency.Entry[OpenItem]] = []
    deferred: list[OpenItem] = []
    unreadable: list[str] = []
    for slug in showrunner_tree.user_facing(production.slug, below):
        try:
            items = read_outstanding(outstanding_file(slug))
        except (InputError, OSError, ValueError):
            unreadable.append(slug)
            continue
        for item in items:
            if isinstance(item.timing, DeferredUntil) and item.timing.at > now:
                deferred.append(OpenItem(slug, item))
            else:
                due.append(urgency.Entry(item_key(slug, item.text), OpenItem(slug, item), item.rank.urgency,
                                         item.since, after=item_key(slug, item.rank.follows) if item.rank.follows else ""))
    deferred.sort(key=lambda found: (cast(DeferredUntil, found.item.timing).at, found.slug))
    return OpenLists(urgency.queue_order(due, question_queue(production, below)), deferred, unreadable)


def open_items(production: Production) -> None:
    """Print in full what waits on the user: this production's own items.

    Replies show only the count, so this list is what the showrunner gives when the user asks what
    the open questions are. Each line is led by the production that owns the item.
    """
    lists = open_lists(production)
    for number, entry in enumerate(lists.due, 1):
        print(f"{number}. {entry.item.line()}")
    for found in lists.deferred:
        print(f"deferred until {cast(DeferredUntil, found.item.timing).at:{WHEN}}: {found.slug}: "
              + (found.item.text if isinstance(found.item, OutstandingReminder) else found.item.question))
    for slug in lists.unreadable:
        print(f"{slug}: {UNREADABLE_LIST}")
    report("open", "ok", f"{len(lists.due)} due, {len(lists.deferred)} deferred"
           + (f", {len(lists.unreadable)} unreadable" if lists.unreadable else ""))


def next_item(production: Production) -> None:
    """Print the one question /next shows: the one shown last until it is answered, else the first due."""
    lists = open_lists(production)
    shown = urgency.top(lists.due, question_queue(production))
    print(shown.item.line() if shown is not None else "no question is open")
    report("next", "ok", f"{max(len(lists.due) - 1, 0)} more due")


def mark_important(production: Production, words: str) -> None:
    """Mark the one item whose text is `words`, or holds them, as the user's: it goes first after the shown one."""
    wanted = words.strip()
    exact: list[tuple[Path, str]] = []
    partial: list[tuple[Path, str]] = []
    # The item the user names may come from an all-below /next, so the lists below are searched too.
    for slug in showrunner_tree.user_facing(production.slug, below=True):
        for item in read_outstanding(outstanding_file(slug)):
            full = item.text if isinstance(item, OutstandingReminder) else item.question
            if item.text == wanted:
                exact.append((outstanding_file(slug), item.text))
            elif wanted and (wanted in item.text or wanted in full):
                partial.append((outstanding_file(slug), item.text))
    found = exact or partial
    if len(found) != 1:
        raise RegistrationFailure("mark-important", f"{len(found)} open items match {wanted!r}; name one by its text")
    path, text = found[0]
    with outstanding_lock(path):
        entries = cast(list[dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        entry = next(entry for entry in entries if entry.get("text") == text)
        for key in ("urgency", "due", "marked"):
            _ = entry.pop(key, None)
        entry.update(urgency.fields(urgency.mark(datetime.now(production.zone).replace(tzinfo=None))))
        write_text_atomic(path, json.dumps(entries) + "\n")
    report("mark-important", "ok", text)


# What `ask` records as the asker when the showrunner files its own question.
SHOWRUNNER_ASKER = "showrunner"


def production_units(production: Production) -> list[str]:
    return [cell_value(cells.get("Unit", "")) for cells in unit_table(production.doc.read_text(encoding="utf-8").splitlines())]


def ask(production: Production, text: str, question: str, unit: str, rank: Ranking) -> None:
    """File one question on the production's list without a reply to the user: the review runs as it
    does for `questions`, the status-line count and /next question show it, and `answer` names `unit`."""
    if unit and unit not in production_units(production):
        raise RegistrationFailure("ask", f"{unit} is not a unit of {production.slug}; name it as the Units table does")
    if not text.strip() or "\n" in text:
        raise RegistrationFailure("ask", "--text must be one nonempty line")
    if not question.strip():
        raise RegistrationFailure("ask", "--question must hold the full question")
    problem = plumbing(text)
    if problem is not None:
        raise RegistrationFailure("ask", f"{text}: {problem}")
    outstanding = outstanding_path(production)
    if any(entry["text"] == text for entry in stored_entries(outstanding)):
        raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
    # The review can take a minute, so it runs before the lock: `questions` is not held up by it.
    accepted = review_open_question(production.slug, QuestionForReview(text, "", question, rank))
    if isinstance(accepted, HeldRegistrationQuestion):
        raise RegistrationFailure("ask", f"question held for review: {text}: {accepted.details}")
    now = datetime.now(production.zone)
    entry = OutstandingItem(since=now.strftime("%Y-%m-%dT%H:%M"), text=text, question=question,
                            asked_by=unit or SHOWRUNNER_ASKER)
    if isinstance(accepted, ReviewedQuestion):
        entry["reviewed"] = accepted.reviewed
    else:
        entry["review_unavailable"] = accepted.reason
    store_ranking(entry, rank)
    with outstanding_lock(outstanding):
        entries = stored_entries(outstanding)
        if any(stored["text"] == text for stored in entries):
            raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
        record_question_history(outstanding, [entry], now)
        write_text_atomic(outstanding, json.dumps([*entries, entry]) + "\n")
    filed = accepted.reviewed if isinstance(accepted, ReviewedQuestion) else question
    print("filed question: " + filed)
    report("ask", "ok", f"{text}: on the open-questions list of {production.slug}, not shown to the user")


def answer(production: Production, words: str) -> None:
    """Remove the one open item whose text is `words`, or holds them, from this production's list or one
    below it, and name who asked it: the answer goes back to that unit through its production's showrunner."""
    wanted = words.strip()
    exact: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    answered_exact: list[AnsweredItem] = []
    answered_partial: list[AnsweredItem] = []
    now = datetime.now(production.zone)
    slugs = list(showrunner_tree.user_facing(production.slug, below=True))
    for slug in slugs:
        for item in read_outstanding(outstanding_file(slug)):
            full = item.text if isinstance(item, OutstandingReminder) else item.question
            if item.text == wanted:
                exact.append((slug, item.text))
            elif wanted and (wanted in item.text or wanted in full):
                partial.append((slug, item.text))
        for saved in answered_items(outstanding_file(slug), now):
            if saved["slug"] != slug:
                continue
            if saved["text"] == wanted:
                answered_exact.append(saved)
            elif wanted and (wanted in saved["text"] or wanted in saved.get("question", "")):
                answered_partial.append(saved)
    found = exact or answered_exact or partial or answered_partial
    if len(found) != 1:
        raise RegistrationFailure("answer", f"{len(found)} open items match {wanted!r}; name one by its text")
    selected = found[0]
    if not isinstance(selected, tuple):
        replay_answer(production, selected)
        return
    slug, text = selected
    path = outstanding_file(slug)
    with outstanding_lock(path):
        entries = stored_entries(path)
        removed = next((entry for entry in entries if entry["text"] == text), None)
        if removed is None:
            raise RegistrationFailure("answer", f"{text}: no longer open in {slug}")
        record_answer(path, slug, removed, datetime.now(production.zone))
        write_text_atomic(path, json.dumps([entry for entry in entries if entry["text"] != text]) + "\n")
    print(relay_line(production, slug, question_asker(removed, OpenItemWithoutAsker())))
    report("answer", "ok", f"{slug}: {text}")


def state_block(production: Production, state: ShowrunnerState, timestamp: str) -> str:
    history = merge_branch_history(production.checkout, production.merge_branch)
    judgments = {unit.unit: unit for unit in state.units}
    unit_lines: list[str] = []
    for cells in unit_table(production.doc.read_text(encoding="utf-8").splitlines()):
        name = cell_value(cells.get("Unit", ""))
        judgment = judgments.get(name)
        merged = history.last_code_for_unit(name)
        last = "none" if isinstance(merged, NoMerge) else f"phase {merged.phase} ({merged.short})"
        phase = judgment.phase if judgment is not None else "not stated"
        wait = judgment.wait if judgment is not None else "not stated"
        unit_lines.append(f"{name}: {phase}; last merged {last}; waits on {wait}")
    held_lines = ", ".join(state.merges_held)
    open_lines = ", ".join(item.text for item in read_outstanding(outstanding_path(production)))
    return "\n".join([f"### STATE {timestamp}", *unit_lines, f"Merges accepted but held: {held_lines or 'none'}",
                      f"Open for the user: {open_lines or 'none'}", f"Next: {'; '.join(state.owed) or 'none'}", ""])


def append_log(production: Production, event: str, path: Path | None, before_compaction: bool) -> None:
    if "\n" in event or not event.strip():
        raise RegistrationFailure("log", "event must be one nonempty line")
    state = judgment_state(path, "log")
    timestamp = production_time(production, datetime.now(production.zone).isoformat())
    previous = production.log.read_text(encoding="utf-8") if production.log.exists() else ""
    count = sum(bool(re.match(r"^- \d\d:\d\d \S+: ", line)) for line in previous.splitlines()) + 1
    block = state_block(production, state, timestamp) if count % 10 == 0 or before_compaction else ""
    production.log.parent.mkdir(parents=True, exist_ok=True)
    with production.log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {timestamp}: {event}\n")
        if block:
            _ = output.write(block)
    report("log", "ok", f"event {count}" + (" with STATE" if block else ""))


def record_question_history(outstanding: Path, items: list[OutstandingItem], now: datetime) -> None:
    """Retain two days of registrations, including questions that have closed."""
    history = outstanding.with_suffix(".history.jsonl")
    retained: list[QuestionRegistration] = []
    cutoff = now - timedelta(hours=48)
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    continue
                fields = cast(dict[str, object], value)
                stamp, text = fields.get("time"), fields.get("text")
                if not isinstance(stamp, str) or not isinstance(text, str):
                    continue
                dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if dated.tzinfo is not None and dated >= cutoff:
                    entry = QuestionRegistration(time=stamp, text=text)
                    for field in ("question", "reviewed"):
                        content = fields.get(field)
                        if isinstance(content, str):
                            if field == "question":
                                entry["question"] = content
                            else:
                                entry["reviewed"] = content
                    retained.append(entry)
            except ValueError:
                continue
    for item in items:
        if any(entry["text"] == item["text"] and entry.get("question") == item.get("question")
               for entry in retained):
            continue
        entry = QuestionRegistration(time=now.isoformat(), text=item["text"])
        if "question" in item:
            entry["question"] = item["question"]
        if "reviewed" in item:
            entry["reviewed"] = item["reviewed"]
        retained.append(entry)
    write_text_atomic(history, "".join(json.dumps(item) + "\n" for item in retained))


def store_ranking(entry: OutstandingItem, rank: Ranking) -> None:
    """Store only what is not normal: the level, its time, and the item this one comes after."""
    for key, value in urgency.fields(rank.urgency).items():
        if key == "urgency":
            entry["urgency"] = value
        elif key == "due":
            entry["due"] = value
        else:
            entry["marked"] = value
    if rank.follows:
        entry["follows"] = rank.follows


def keep_question_fields(entry: OutstandingItem, previous: Outstanding) -> None:
    if not isinstance(previous, OutstandingReminder):
        entry["question"] = previous.question
        if previous.asked_by:
            entry["asked_by"] = previous.asked_by
    if isinstance(previous, ReviewedOutstandingQuestion):
        entry["reviewed"] = previous.reviewed
    elif isinstance(previous, ReviewUnavailableOutstandingQuestion):
        entry["review_unavailable"] = previous.reason


class HeldRegistrationQuestion(NamedTuple):
    details: str


class ReviewedQuestion(NamedTuple):
    text: str
    reviewed: str


class ReviewUnavailableQuestion(NamedTuple):
    text: str
    question: str
    reason: str


AcceptedQuestion = ReviewedQuestion | ReviewUnavailableQuestion


def review_open_question(slug: str, item: QuestionForReview) -> AcceptedQuestion | HeldRegistrationQuestion:
    # Copied registration trees can run reminder-only commands without notify installed.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
    if TYPE_CHECKING:
        from ..notify import review
    else:
        import review
    outcome = review.review_question(review.QuestionKey(slug, item.text), item.question)
    if isinstance(outcome, review.QuestionAccepted):
        return ReviewedQuestion(item.text, outcome.question)
    if isinstance(outcome, review.QuestionReviewUnavailable):
        return ReviewUnavailableQuestion(item.text, outcome.question, outcome.reason)
    details = outcome.questions if isinstance(outcome, review.QuestionAwaitingFacts) else outcome.details
    return HeldRegistrationQuestion("; ".join(details + tuple(f"{failure.rule}: {failure.quote}" for failure in outcome.failures)))


def questions(production: Production, path: Path | None) -> None:
    """Record what waits on the user and review each new question before the reply asks it."""
    state = judgment_state(path, "questions")
    outstanding = outstanding_path(production)
    if isinstance(state.outstanding, ReplaceOutstanding):
        with outstanding_lock(outstanding):
            replace_outstanding(production, outstanding, state.outstanding)
    if isinstance(state.reports, ReplaceReports):
        reports = reports_path(production.slug)
        write_text_atomic(reports, json.dumps(list(state.reports.items)) + "\n")
    report("questions", "ok", "recorded")


def replace_outstanding(production: Production, outstanding: Path, replacement: ReplaceOutstanding) -> None:
    """Write the showrunner's new list; a question someone filed with `ask` leaves only through `answer`."""
    previous = {item.text: item for item in read_outstanding(outstanding)} if outstanding.exists() else {}
    now = datetime.now(production.zone)
    since = now.strftime("%Y-%m-%dT%H:%M")
    entries: list[OutstandingItem] = []
    pending: list[AcceptedQuestion] = []
    held: list[str] = []
    items_by_text: dict[str, OpenForUserItem] = {}
    for item in replacement.items:
        if item.text not in items_by_text:
            items_by_text[item.text] = item
    # Validate all new object questions before starting any review.
    for item in items_by_text.values():
        if item.text not in previous and isinstance(item, OpenQuestionWithoutText):
            raise RegistrationFailure("questions", f"{item.text}: a new question needs its full text in `question`")
    # A new or changed item must pass the plumbing check; a stored one that fails is only named.
    refused: list[str] = []
    for text in items_by_text:
        problem = plumbing(text)
        if problem is not None and text in previous:
            report("questions", "reword", f"{text}: {problem}")
        elif problem is not None:
            refused.append(f"{text}: {problem}")
    if refused:
        raise RegistrationFailure("questions", "; ".join(refused))
    for item in items_by_text.values():
        entry = OutstandingItem(since=previous[item.text].since.strftime("%Y-%m-%dT%H:%M")
                                if item.text in previous else since, text=item.text)
        if item.after:
            entry["after"] = item.after
        old = previous[item.text].rank.urgency if item.text in previous else urgency.Urgency()
        store_ranking(entry, Ranking(urgency.keep_mark(item.rank.urgency, old, now.replace(tzinfo=None)),
                                     item.rank.follows))
        if item.text in previous:
            keep_question_fields(entry, previous[item.text])
        elif isinstance(item, QuestionForReview):
            accepted = review_open_question(production.slug, item)
            if isinstance(accepted, ReviewedQuestion):
                entry["question"], entry["reviewed"] = item.question, accepted.reviewed
                pending.append(accepted)
            elif isinstance(accepted, ReviewUnavailableQuestion):
                entry["question"], entry["review_unavailable"] = item.question, accepted.reason
                pending.append(accepted)
            else:
                held.append(f"{item.text}: {accepted.details}")
        entries.append(entry)
    if held:
        raise RegistrationFailure("questions", "question held for review: " + "; ".join(held))
    for stored in stored_entries(outstanding):
        asker = stored.get("asked_by", "")
        if asker and stored["text"] not in items_by_text:
            entries.append(stored)
            report("questions", "kept", f"{stored['text']}: asked by {asker}; it leaves only through `answer`")
    record_question_history(outstanding, [entry for entry in entries if entry["text"] not in previous], now)
    write_text_atomic(outstanding, json.dumps(entries) + "\n")
    for item in pending:
        accepted_text = item.reviewed if isinstance(item, ReviewedQuestion) else item.question
        print("reviewed question: " + accepted_text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("register", "time", "log", "questions", "open", "next", "mark-important", "ask", "answer"):
        subcommand = commands.add_parser(action)
        _ = subcommand.add_argument("--production", type=Path, required=True)
        if action == "register":
            # Accepted and unused: a showrunner started before its name was looked up still passes it.
            _ = subcommand.add_argument("--session")
        elif action == "time":
            _ = subcommand.add_argument("stamp")
        elif action == "log":
            _ = subcommand.add_argument("event")
            _ = subcommand.add_argument("--state", type=Path)
            _ = subcommand.add_argument("--before-compaction", action="store_true")
        elif action == "questions":
            _ = subcommand.add_argument("--state", type=Path)
        elif action in ("mark-important", "answer"):
            _ = subcommand.add_argument("--item", required=True)
        elif action == "ask":
            _ = subcommand.add_argument("--text", required=True, help="the one-line item the count and lists name")
            _ = subcommand.add_argument("--question", required=True,
                                        help="what is asked, why, each choice and what it does, the recommended choice")
            _ = subcommand.add_argument("--unit", default="", help="the asking unit; absent, the showrunner asks")
            _ = subcommand.add_argument("--urgency", default="", help="user, blocks, soon (with --due), normal or later")
            _ = subcommand.add_argument("--due", default="")
    args = parser.parse_args()
    action = cast(str, args.action)
    try:
        production = read_production(cast(Path, args.production))
        for line in adopt_moved_state(production):
            report("state-dir", "ok", line)
        if action == "register":
            register(production)
        elif action == "time":
            report("time", "ok", production_time(production, cast(str, args.stamp)))
        elif action == "log":
            append_log(production, cast(str, args.event), cast(Path | None, args.state), cast(bool, args.before_compaction))
        elif action == "open":
            open_items(production)
        elif action == "next":
            next_item(production)
        elif action == "mark-important":
            mark_important(production, cast(str, args.item))
        elif action == "answer":
            answer(production, cast(str, args.item))
        elif action == "ask":
            try:
                level = urgency.read(cast(str, args.urgency), cast(str, args.due))
            except urgency.UrgencyError:
                raise RegistrationFailure("ask", "--urgency is user, blocks, soon with --due, normal or later") from None
            ask(production, cast(str, args.text), cast(str, args.question), cast(str, args.unit), Ranking(level))
        else:
            questions(production, cast(Path | None, args.state))
        return 0
    except (RegistrationFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, RegistrationFailure) else action
        report(step, "failed", str(error).replace("\n", "; "))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
