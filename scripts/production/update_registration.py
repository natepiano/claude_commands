#!/usr/bin/env python3
"""Register a showrunner's update jobs and write its production time and log."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, cast

from add_unit import Production, read_production
from production_units import Refusal, cell_value, unit_table
from merge_checkpoint import NoMerge, merge_branch_history, report

if TYPE_CHECKING:
    from ..hooks.showrunner_footer import (PendingQuestion, PendingReviewedQuestion, PendingReviewUnavailableQuestion, write_pending_reviewed,
        read_pending_reviewed, ReportItem, reports_path)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    from showrunner_footer import (PendingQuestion, PendingReviewedQuestion, PendingReviewUnavailableQuestion, write_pending_reviewed,
        read_pending_reviewed, ReportItem, reports_path)
import showrunners
from dailies_render import (UNREADABLE_LIST, InputError, plumbing, read_outstanding,
    DeferredUntil, Outstanding, OutstandingReminder, ReviewedOutstandingQuestion,
    ReviewUnavailableOutstandingQuestion)

if TYPE_CHECKING:
    from ..usage import agents_log
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.usage import agents_log


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


class QuestionRegistration(TypedDict):
    time: str
    text: str
    reviewed: NotRequired[str]
    question: NotRequired[str]


class UnitJudgment(NamedTuple):
    unit: str
    phase: str
    wait: str


class OpenReminder(NamedTuple):
    text: str
    after: str


class QuestionForReview(NamedTuple):
    text: str
    after: str
    question: str


class OpenQuestionWithoutText(NamedTuple):
    text: str
    after: str


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


def register(production: Production) -> None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        raise RegistrationFailure("register", "CLAUDE_CODE_SESSION_ID is required")
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
                if field not in {"text", "after", "question"}:
                    raise state_error(step, path, f"{key}.{field}", "a known key")
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
                items.append(QuestionForReview(cast(str, item["text"]), after, question))
            else:
                items.append(OpenQuestionWithoutText(cast(str, item["text"]), after))
        outstanding = ReplaceOutstanding(tuple(items))
    return ShowrunnerState(tuple(units), tuple(cast(list[str], raw_held)), outstanding,
                           report_items(fields, path, step), tuple(cast(list[str], raw_next)))


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


# An item can wait longer than a week, so the day is named in full.
WHEN = "%a %b %d %H:%M"


def open_items(production: Production) -> None:
    """Print in full what waits on the user: this production's items and, for a root, every lower one's.

    Replies show only the count, so this list is what the showrunner gives when the user asks what
    the open questions are. Each line is led by the production that owns the item.
    """
    now = datetime.now(production.zone).replace(tzinfo=None)
    due: list[tuple[datetime, str, str]] = []
    deferred: list[tuple[datetime, str, str]] = []
    unreadable: list[str] = []
    for slug in showrunners.user_facing(production.slug):
        try:
            items = read_outstanding(outstanding_file(slug))
        except (InputError, OSError, ValueError):
            unreadable.append(slug)
            continue
        for item in items:
            text = item.text if isinstance(item, OutstandingReminder) else item.question
            if isinstance(item.timing, DeferredUntil) and item.timing.at > now:
                deferred.append((item.timing.at, slug, text))
            else:
                due.append((item.since, slug, text))
    for number, (since, slug, text) in enumerate(sorted(due), 1):
        print(f"{number}. {slug}, since {since:{WHEN}}: {text}")
    for after, slug, text in sorted(deferred):
        print(f"deferred until {after:{WHEN}}: {slug}: {text}")
    for slug in unreadable:
        print(f"{slug}: {UNREADABLE_LIST}")
    report("open", "ok", f"{len(due)} due, {len(deferred)} deferred"
           + (f", {len(unreadable)} unreadable" if unreadable else ""))


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
        entry = QuestionRegistration(time=now.isoformat(), text=item["text"])
        if "question" in item:
            entry["question"] = item["question"]
        if "reviewed" in item:
            entry["reviewed"] = item["reviewed"]
        retained.append(entry)
    _ = history.write_text("".join(json.dumps(item) + "\n" for item in retained), encoding="utf-8")


def keep_question_fields(entry: OutstandingItem, previous: Outstanding) -> None:
    if not isinstance(previous, OutstandingReminder):
        entry["question"] = previous.question
    if isinstance(previous, ReviewedOutstandingQuestion):
        entry["reviewed"] = previous.reviewed
    elif isinstance(previous, ReviewUnavailableOutstandingQuestion):
        entry["review_unavailable"] = previous.reason


class HeldRegistrationQuestion(NamedTuple):
    details: str


def review_open_question(slug: str, item: QuestionForReview) -> PendingQuestion | HeldRegistrationQuestion:
    # Copied registration trees can run reminder-only commands without notify installed.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
    if TYPE_CHECKING:
        from ..notify import review
    else:
        import review
    outcome = review.review_question(review.QuestionKey(slug, item.text), item.question)
    if isinstance(outcome, review.QuestionAccepted):
        return PendingReviewedQuestion(item.text, outcome.question)
    if isinstance(outcome, review.QuestionReviewUnavailable):
        return PendingReviewUnavailableQuestion(item.text, outcome.question, outcome.reason)
    details = outcome.questions if isinstance(outcome, review.QuestionAwaitingFacts) else outcome.details
    return HeldRegistrationQuestion("; ".join(details + tuple(f"{failure.rule}: {failure.quote}" for failure in outcome.failures)))


def questions(production: Production, path: Path | None) -> None:
    """Record what waits on the user and review each new question before the reply asks it."""
    state = judgment_state(path, "questions")
    outstanding = outstanding_path(production)
    if isinstance(state.outstanding, ReplaceOutstanding):
        previous = {item.text: item for item in read_outstanding(outstanding)} if outstanding.exists() else {}
        now = datetime.now(production.zone)
        since = now.strftime("%Y-%m-%dT%H:%M")
        entries: list[OutstandingItem] = []
        pending: list[PendingQuestion] = []
        held: list[str] = []
        items_by_text: dict[str, OpenForUserItem] = {}
        for item in state.outstanding.items:
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
            if item.text in previous:
                keep_question_fields(entry, previous[item.text])
            elif isinstance(item, QuestionForReview):
                accepted = review_open_question(production.slug, item)
                if isinstance(accepted, PendingReviewedQuestion):
                    entry["question"], entry["reviewed"] = item.question, accepted.reviewed
                    pending.append(accepted)
                elif isinstance(accepted, PendingReviewUnavailableQuestion):
                    entry["question"], entry["review_unavailable"] = item.question, accepted.reason
                    pending.append(accepted)
                else:
                    held.append(f"{item.text}: {accepted.details}")
            entries.append(entry)
        if held:
            raise RegistrationFailure("questions", "question held for review: " + "; ".join(held))
        outstanding.parent.mkdir(parents=True, exist_ok=True)
        instance_dir = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{production.slug}"
        if (instance_dir / "conf").exists():
            existing_pending = read_pending_reviewed(instance_dir)
            retained_pending = [item for item in existing_pending if item.text in items_by_text]
            if retained_pending != existing_pending or pending:
                write_pending_reviewed(instance_dir, retained_pending + pending)
        record_question_history(outstanding, [entry for entry in entries if entry["text"] not in previous], now)
        _ = outstanding.write_text(json.dumps(entries) + "\n", encoding="utf-8")
        for item in pending:
            accepted_text = item.reviewed if isinstance(item, PendingReviewedQuestion) else item.question
            print("reviewed question: " + accepted_text)
    if isinstance(state.reports, ReplaceReports):
        reports = reports_path(production.slug)
        reports.parent.mkdir(parents=True, exist_ok=True)
        _ = reports.write_text(json.dumps(list(state.reports.items)) + "\n", encoding="utf-8")
    report("questions", "ok", "recorded")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("register", "time", "log", "questions", "open"):
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
    args = parser.parse_args()
    action = cast(str, args.action)
    try:
        production = read_production(cast(Path, args.production))
        if action == "register":
            register(production)
        elif action == "time":
            report("time", "ok", production_time(production, cast(str, args.stamp)))
        elif action == "log":
            append_log(production, cast(str, args.event), cast(Path | None, args.state), cast(bool, args.before_compaction))
        elif action == "open":
            open_items(production)
        else:
            questions(production, cast(Path | None, args.state))
        return 0
    except (RegistrationFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, RegistrationFailure) else action
        report(step, "failed", str(error).replace("\n", "; "))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
