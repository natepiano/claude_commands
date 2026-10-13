#!/usr/bin/env python3
"""The nightly review's open candidates, as one Markdown table in the order /nightly_next ranks them.

Usage: candidates.py                   print the table
       candidates.py record <report>   add a night's report to the ledger: its proposal and each
                                       Also found and Noted item, one line each

The ledger is the one record, and every candidate is a line of its own:

    - <night> <scope>: <title> [gain/week: <gain>] — <state>
    - <night> <scope>, also found: <title> [gain/week: <gain>] — <state>

A line is open while it ends `— proposed` or `— deferred`. The gain is measured user-visible time
(`38 min waiting`, `2.5 h red CI`) or a share of tokens (`3.1% of tokens`); a line with no tag is
unmeasured. Time ranks before tokens and unmeasured comes last, as /nightly_next ranks them.

The note at ~/rust/hanadocs/nightly improvement list.md copies the ledger's table.
NIGHTLY_REVIEW_NOTE replaces that path; both commands rewrite the note.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
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
    """Every open candidate, best first; candidates that rank alike keep the ledger's order."""
    found = [candidate for line in lines if (candidate := open_candidate(line)) is not None]
    return sorted(found, key=rank)


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
    temporary: Path | None = None
    try:
        if not note.parent.is_dir() or not (ROOT / "ledger.md").is_file():
            return
        moment = now().astimezone(user_zone())
        host = socket.gethostname().split(".", 1)[0]
        read_line = (
            f"Read {moment:%Y-%m-%d %H:%M} {moment:%Z} from the nightly review's ledger on {host}. "
            "The nightly review rewrites this note; edits made here are lost."
        )
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=note.parent, prefix=f".{note.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            _ = handle.write(f"{read_line}\n\n{body}\n")
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, note)
    except OSError as error:
        print(f"candidates: cannot write {note}: {error}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


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
    new = [each.line for each in candidates if not any(line.startswith(each.head) for line in known)]
    if not new:
        print("nothing new to record")
    else:
        try:
            with ledger.open("a", encoding="utf-8") as handle:
                _ = handle.write("".join(f"{line}\n" for line in new))
        except OSError as error:
            print(f"candidates: {error}", file=sys.stderr)
            return 1
        print("\n".join(new))
    open_candidates = ranked(read_lines(ledger))
    write_note(table(open_candidates) if open_candidates else "no open candidates")
    return 0


def main(arguments: list[str]) -> int:
    match arguments:
        case []:
            candidates = ranked(read_lines(ROOT / "ledger.md"))
            body = table(candidates) if candidates else "no open candidates"
            print(body)
            write_note(body)
            return 0
        case ["record", report]:
            return record(Path(report).expanduser())
        case _:
            print("usage: candidates.py [record <report>]", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
