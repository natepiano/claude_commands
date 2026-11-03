#!/usr/bin/env python3
"""The nightly review's open candidates, as one Markdown table in the order /next efficiency ranks them.

Usage: candidates.py                   print the table
       candidates.py next              print the one candidate /next efficiency shows, and pin it
       candidates.py record <report>   add a night's report to the ledger: its proposal and each
                                       Also found and Noted item, one line each

The ledger is the one record, and every candidate is a line of its own:

    - <night> <scope>: <title> [gain/week: <gain>] — <state>
    - <night> <scope>, also found: <title> [gain/week: <gain>] — <state>

A line is open while it ends `— proposed` or `— deferred`. The gain is measured user-visible time
(`38 min waiting`, `2.5 h red CI`) or a share of tokens (`3.1% of tokens`); a line with no tag is
unmeasured. Time ranks before tokens and unmeasured comes last, as /next efficiency ranks them;
candidates that rank alike go oldest night first. The table and `next` follow urgency.py under the
queue name `efficiency`: the candidate `next` showed leads until its outcome is written, and one the
user moved to the end goes last.

The note at ~/rust/hanadocs/nightly improvement list.md copies the ledger's table.
NIGHTLY_REVIEW_NOTE replaces that path; both commands rewrite the note.
"""
from __future__ import annotations

import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import urgency  # noqa: E402
from user_zone import user_zone  # noqa: E402

ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "nightly-review"
# A title longer than this is cut, so one long sentence cannot widen the whole table.
TITLE_WIDTH = 90

LINE = re.compile(
    r"^- (?P<night>\d{4}-\d{2}-\d{2}(?:-[a-z]+)?) (?P<scope>[^:,]+)(?:, (?P<kind>also found|noted))?: (?P<body>.+)$"
)
OPEN_END = re.compile(r" — (?P<state>proposed|deferred)\s*$")
TAG = re.compile(r"\s*\[gain/week: (?P<gain>[^\]]+)\]\s*$")
GAIN = re.compile(r"^(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?:(?P<minutes>min(?:utes?)?\b)|(?P<hours>h(?:ours?)?\b)|(?P<percent>%))")
REPORT_FIELD = re.compile(r"^\*\*(?P<name>[^*:]+):\*\*\s*(?P<value>.*)$")
# A night writes its Also found and Noted items as a bulleted or a numbered list.
ITEM_LINE = re.compile(r"^(?:- |\d+\. )(?P<text>.+)$")
ITEM_LEAD = re.compile(r"^\*\*(?P<title>.+?)\*\*")
ITEM_KINDS = {"Also found": "also found", "Noted": "noted"}
UNMEASURED_RANK = 2
# The name urgency.py pins the candidate /next showed under.
QUEUE = "efficiency"


class TimeGain(NamedTuple):
    """User-visible time a week: waiting, red CI, failed runs."""
    minutes: float


class TokenGain(NamedTuple):
    """A share of a week's tokens."""
    percent: float


class Unmeasured(NamedTuple):
    pass


Gain = TimeGain | TokenGain | Unmeasured


class Candidate(NamedTuple):
    night: str
    scope: str
    # "" for a night's proposal, else "also found" or "noted".
    kind: str
    title: str
    # The tag's words as written; empty when the line carries no tag.
    gain_text: str
    state: str

    @property
    def area(self) -> str:
        return f"{self.scope}, {self.kind}" if self.kind else self.scope

    @property
    def head(self) -> str:
        """The start of this candidate's ledger line, which no other candidate shares."""
        return f"- {self.night} {self.area}: {self.title}"

    @property
    def line(self) -> str:
        tag = f" [gain/week: {self.gain_text}]" if self.gain_text else ""
        return f"{self.head}{tag} — {self.state}"


def read_gain(text: str) -> Gain:
    found = GAIN.match(text.strip())
    if found is None:
        return Unmeasured()
    number = float(found["number"].replace(",", ""))
    if found["percent"]:
        return TokenGain(number)
    return TimeGain(number * 60 if found["hours"] else number)


def rank(candidate: Candidate) -> tuple[int, float]:
    """Sort key: user-visible time first, then share of tokens, each largest first; unmeasured last."""
    match read_gain(candidate.gain_text):
        case TimeGain(minutes):
            return (0, -minutes)
        case TokenGain(percent):
            return (1, -percent)
        case Unmeasured():
            return (UNMEASURED_RANK, 0.0)


def open_candidate(line: str) -> Candidate | None:
    """The candidate a ledger line holds while it is open, else None."""
    found = LINE.match(line)
    ending = OPEN_END.search(line)
    if found is None or ending is None:
        return None
    title = OPEN_END.sub("", found["body"])
    tag = TAG.search(title)
    gain_text = tag["gain"].strip() if tag else ""
    if tag is not None:
        title = title[: tag.start()]
    return Candidate(found["night"], found["scope"].strip(), found["kind"] or "", title.strip(), gain_text, ending["state"])


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []


def ranked(lines: list[str]) -> list[Candidate]:
    """Every open candidate, best first; candidates that rank alike go oldest night first, then in ledger order."""
    found = [candidate for line in lines if (candidate := open_candidate(line)) is not None]
    return sorted(found, key=lambda candidate: (rank(candidate), candidate.night[:10]))


def night_day(candidate: Candidate) -> datetime | None:
    try:
        return datetime.fromisoformat(candidate.night[:10])
    except ValueError:
        return None


def entries(lines: list[str]) -> list[urgency.Entry[Candidate]]:
    """Every open candidate as an urgency.py entry: its gain is the value, its night the age."""
    return [urgency.Entry(each.head, each, since=night_day(each), value=tuple(-part for part in rank(each)))
            for each in ranked(lines)]


def ordered(lines: list[str]) -> list[Candidate]:
    """Every open candidate in the order the table and /next show: the pinned one first, moved ones last."""
    return [entry.item for entry in urgency.queue_order(entries(lines), QUEUE)]


def escaped(cell: str) -> str:
    text = " ".join(cell.split()).replace("|", "\\|")
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1].rstrip() + "…"


def table(candidates: list[Candidate]) -> str:
    lines = ["| Pick | Night | Area | Candidate | Gain per week | State |", "| --- | --- | --- | --- | --- | --- |"]
    for index, candidate in enumerate(candidates):
        # With nothing measured there is no basis for a pick, so no row is marked.
        pick = "recommended" if index == 0 and rank(candidate)[0] < UNMEASURED_RANK else ""
        cells = (pick, candidate.night, candidate.area, candidate.title, candidate.gain_text, candidate.state)
        lines.append("| " + " | ".join(escaped(cell) for cell in cells) + " |")
    return "\n".join(lines)


def now() -> datetime:
    return datetime.now(user_zone())


def write_note(body: str) -> None:
    value = os.environ.get("NIGHTLY_REVIEW_NOTE")
    note = Path(value).expanduser() if value else Path.home() / "rust" / "hanadocs" / "nightly improvement list.md"
    try:
        if not note.parent.is_dir() or not (ROOT / "ledger.md").is_file():
            return
        moment = now().astimezone(user_zone())
        host = socket.gethostname().split(".", 1)[0]
        read_line = (
            f"Read {moment:%Y-%m-%d %H:%M} {moment:%Z} from the nightly review's ledger on {host}. "
            "The nightly review rewrites this note; edits made here are lost."
        )
        write_text_atomic(note, f"{read_line}\n\n{body}\n")
        note.chmod(0o644)
    except OSError as error:
        print(f"candidates: cannot write {note}: {error}", file=sys.stderr)


def gain_words(value: str) -> str:
    """A report's gain as a tag's words; empty when the report calls it unmeasured or leaves it out."""
    text = value.strip().rstrip(".")
    return "" if not text or text.lower().startswith("unmeasured") else text


def item_candidate(night: str, scope: str, kind: str, text: str) -> Candidate:
    """One Also found or Noted item: its bold lead is the title, and a closing tag is its gain."""
    tag = TAG.search(text)
    body = text[: tag.start()] if tag else text
    lead = ITEM_LEAD.match(body)
    title = lead["title"] if lead else re.split(r"[.:] ", body, maxsplit=1)[0]
    return Candidate(night, scope, kind, title.strip().rstrip(".:"), gain_words(tag["gain"]) if tag else "", "proposed")


def report_candidates(night: str, lines: list[str]) -> list[Candidate]:
    """A report's proposal, then its Also found and Noted items. Empty when it names no title or target."""
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
    fields: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    section = ""
    for line in lines:
        field = REPORT_FIELD.match(line)
        item = ITEM_LINE.match(line)
        if field is not None:
            section = field["name"].strip()
            _ = fields.setdefault(section, field["value"])
        elif item is not None and section in ITEM_KINDS:
            items.append((ITEM_KINDS[section], item["text"].strip()))
        elif line.strip() and not line[0].isspace():
            # A paragraph closes the item list, so a list below it (what was denied tonight) is no candidate.
            section = ""
    scope = fields.get("Target", "").strip()
    if not title or not scope:
        return []
    proposal = Candidate(night, scope, "", title, gain_words(fields.get("Gain per week", "")), "proposed")
    return [proposal, *(item_candidate(night, scope, kind, text) for kind, text in items)]


def record(report: Path) -> int:
    lines = read_lines(report)
    candidates = report_candidates(report.parent.name, lines)
    if not candidates:
        print(f"candidates: {report} has no `# <title>` and `**Target:**` to record", file=sys.stderr)
        return 1
    ledger = ROOT / "ledger.md"
    known = read_lines(ledger)
    new: list[str] = []
    for candidate in candidates:
        # The outcome follows this exact heading and its optional gain, never words in the title.
        complete_record = re.compile(re.escape(candidate.head)
                                     + r"(?: \[gain/week: [^\]]+\])? — (?:proposed|deferred|accepted|declined|done|relayed)(?:[ ,].*)?")
        if any(complete_record.fullmatch(line) is not None for line in known):
            continue
        # A stopped append can leave just the heading or part of its final state.
        known = [line for line in known if not candidate.line.startswith(line) or not line]
        known.append(candidate.line)
        new.append(candidate.line)
    if not new:
        print("nothing new to record")
    else:
        print("\n".join(new), flush=True)
        try:
            write_text_atomic(ledger, "\n".join(known) + "\n")
        except OSError as error:
            print(f"candidates: {error}", file=sys.stderr)
            return 1
    open_candidates = ordered(read_lines(ledger))
    write_note(table(open_candidates) if open_candidates else "no open candidates")
    return 0


def main(arguments: list[str]) -> int:
    match arguments:
        case []:
            candidates = ordered(read_lines(ROOT / "ledger.md"))
            body = table(candidates) if candidates else "no open candidates"
            print(body)
            write_note(body)
            return 0
        case ["next"]:
            # One candidate only: the user asks for the next one when they want it (user, 2026-10-20).
            shown = urgency.top(entries(read_lines(ROOT / "ledger.md")), QUEUE)
            print(shown.item.line if shown is not None else "no open candidates")
            return 0
        case ["record", report]:
            return record(Path(report).expanduser())
        case _:
            print("usage: candidates.py [next | record <report>]", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
