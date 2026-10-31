#!/usr/bin/env python3
"""The nightly review: one read-only unit, one improvement proposal per mode, queued for the user.

`launch` runs on demand on natedev; the 01:00 nightly-review timer that ran it was retired by
/etc/nixos cc9a0e9 (2026-10-19). The review is the permanent nightly-review-unit of one production:
wrap never retires it, so it keeps one conversation across nights. Each launch sends that unit the
night's job, `/nightly_review config rust messages` for NIGHT=<date>. When the unit is gone, launch
first starts it through add_unit.py --permanent, which uses the configured units account, checks shutdown holds and trust,
and keeps the unit's launch flags (its own settings, ~/rust, /etc/nixos and ~/.claude).
The unit does the modes in order; for each it writes NIGHT/<mode>.md and messages natedev, which
distills the proposals into NIGHT/digest.md and the ledger (/watcher). The night is skipped when no
running production holds the unit.

Nothing offers the proposals: the ledger's queue grows until the user asks for /next efficiency
(user, 2026-10-10).
"""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

# The account reader and the production scripts sit one directory over.
SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "whoami"))
sys.path.insert(0, str(SCRIPTS / "shutdown"))
sys.path.insert(0, str(SCRIPTS / "production"))
from scripts.lib.process_failure import failure_detail
from scripts.whoami import account  # noqa: E402

if TYPE_CHECKING:
    from scripts.production.unit_lookup import LiveClaude

ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "nightly-review"
MODES = ("config", "rust", "messages")
SESSION = "nightly-review"
PROMPT = "/nightly_review " + " ".join(MODES)
SETTINGS = Path(__file__).with_name("settings.json")
SESSIONS_DIR = Path.home() / ".claude/sessions"
REVIEW_SCRIPT = SCRIPTS / "notify/review.py"


class TranscriptSessionRecord(TypedDict, total=False):
    sessionId: str
    name: str
    pid: int
    cwd: str
    config_dir: str
    transcript_path: str
    transcriptPath: str


class DatedTranscriptRecord(TypedDict):
    type: str
    timestamp: str


class NightlyVerdictRecord(TypedDict):
    time: str
    verdict: str
    failed_rules: list[str]
    replay: NotRequired[bool]
    dry: NotRequired[bool]
    sample: NotRequired[bool]


@dataclass(frozen=True)
class LiveMessageCounts:
    messages: int
    failures: int
    unreviewed: int
    rules: tuple[tuple[str, int], ...]
    template_failures: tuple[tuple[str, int], ...] = ()


def json_record(line: str) -> dict[str, object]:
    value = cast(object, json.loads(line))
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def record_text(record: dict[str, object], key: str) -> str:
    value = record.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} is not text")
    return value


def record_time(text: str) -> datetime:
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp has no time zone")
    return stamp


def recent_replies(path: Path, since: datetime) -> bool:
    """A recent file write alone does not make an old reply part of tonight."""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                data = json_record(line)
                record = DatedTranscriptRecord(type=record_text(data, "type"), timestamp=record_text(data, "timestamp"))
                if record["type"] == "assistant" and record_time(record["timestamp"]) >= since:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def showrunner_transcripts(since: datetime) -> list[Path]:
    """Find dated replies in each showrunner's own account, including natedev."""
    from scripts.production import showrunners

    names = {runner["session"] for runner in showrunners.registered_showrunners() if runner["session"]}
    found: set[Path] = set()
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            data = json_record(path.read_text(encoding="utf-8"))
            pid = data.get("pid", 0)
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise ValueError("pid is not an integer")
            record = TranscriptSessionRecord(sessionId=record_text(data, "sessionId"), name=record_text(data, "name"),
                pid=pid, cwd=record_text(data, "cwd"), config_dir=record_text(data, "config_dir"),
                transcript_path=record_text(data, "transcript_path"), transcriptPath=record_text(data, "transcriptPath"))
            session, name = record.get("sessionId", ""), record.get("name", "")
            if not session or Path(session).name != session:
                continue
            if name != "natedev" and name not in names and showrunners.production_of(session) is None:
                continue
            recorded = record.get("transcript_path", "") or record.get("transcriptPath", "")
            config = record.get("config_dir", "")
            pid = record.get("pid", 0)
            if recorded:
                candidates = [Path(recorded)]
            else:
                setting = (account.ConfiguredClaudeFolder(Path(config)) if config else
                           account.process_account_directory(account.process_config_dir(pid)) if pid > 0 else
                           account.NoAccountSetting())
                if not isinstance(setting, account.ConfiguredClaudeFolder):
                    print(f"messages: account directory unavailable for {session}", file=sys.stderr)
                    continue
                directory = setting.path
                cwd = record.get("cwd", "")
                candidates = ([directory / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / f"{session}.jsonl"]
                              if cwd else list((directory / "projects").glob(f"*/{session}.jsonl")))
            for candidate in candidates:
                if candidate.is_file() and recent_replies(candidate, since):
                    found.add(candidate)
        except (OSError, ValueError, TypeError) as error:
            print(f"messages: {path}: {error}", file=sys.stderr)
    return sorted(found)


def live_message_counts(path: Path, since: datetime) -> LiveMessageCounts:
    messages = failures = unreviewed = 0
    rules: dict[str, int] = {}
    templates: dict[str, int] = {}
    if not path.exists():
        return LiveMessageCounts(0, 0, 0, ())
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json_record(line)
            if data.get("kind") == "question" or any(data.get(key) for key in ("replay", "dry", "sample")):
                continue
            raw_rules = data.get("failed_rules", [])
            if not isinstance(raw_rules, list) or not all(isinstance(rule, str) for rule in cast(list[object], raw_rules)):
                raise ValueError("failed_rules is not a list of text")
            record = NightlyVerdictRecord(time=record_text(data, "time"), verdict=record_text(data, "verdict"),
                                          failed_rules=cast(list[str], raw_rules))
            if record_time(record["time"]) < since:
                continue
            verdict = record["verdict"]
            text_id = data.get("text_id")
            detail = data.get("detail", "")
            if isinstance(text_id, str) and text_id and isinstance(detail, str) and detail.startswith("sent despite failed review") and data.get("sent") is not None:
                templates[text_id] = templates.get(text_id, 0) + 1
            messages += 1
            if verdict.startswith("unreviewed:") or verdict == "review: off":
                unreviewed += 1
            elif verdict in ("rewrite", "ask", "failed", "withhold"):
                failures += 1
                for rule in set(record["failed_rules"]):
                    rules[rule] = rules.get(rule, 0) + 1
        except (ValueError, TypeError):
            continue
    return LiveMessageCounts(messages, failures, unreviewed, tuple(sorted(rules.items())), tuple(sorted(templates.items())))


def messages(since: datetime) -> list[str]:
    sys.path.insert(0, str(SCRIPTS / "notify"))
    if TYPE_CHECKING:
        from scripts.notify import review
    else:
        import review

    counts = live_message_counts(review.state_path() / "review.jsonl", since)
    lines = [f"Live messages: {counts.messages}; failures: {counts.failures}; Unreviewed: {counts.unreviewed}."]
    lines.extend(f"Live rule {rule}: {count}" for rule, count in counts.rules)
    lines.extend(f"Template {text_id} sent despite failed review: {count}" for text_id, count in counts.template_failures)
    transcripts = showrunner_transcripts(since)
    if not transcripts:
        return [*lines, "No showrunner replies in this interval."]
    try:
        result = subprocess.run([sys.executable, str(REVIEW_SCRIPT), "sample", *(str(path) for path in transcripts),
                                 "--limit", "20", "--since", since.isoformat()],
                                capture_output=True, text=True, check=False, timeout=20 * review.user_send_seconds() + 15)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [*lines, f"Sample failed: {error}"]
    lines.extend(result.stdout.splitlines())
    if result.returncode:
        lines.append(f"Sample failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    return lines


NIGHTLY_UNIT = "nightly-review-unit"


def night_dir(day: date) -> Path:
    return ROOT / day.isoformat()


def job(day: date) -> str:
    """The night's work, sent to the permanent unit as a message."""
    return (f"Nightly review for NIGHT={day.isoformat()}: follow ~/.claude/commands/nightly_review.md "
            + f"with the modes {' '.join(MODES)}, in that order.")


@dataclass(frozen=True)
class NightlyProduction:
    doc: Path
    slug: str


def nightly_production() -> NightlyProduction | str:
    """The one production whose Units table holds the permanent nightly review unit, or why there is none."""
    from scripts.production import showrunners

    if TYPE_CHECKING:
        from scripts.production import production_units
    else:
        import production_units

    found: list[NightlyProduction] = []
    for runner in showrunners.registered_showrunners():
        try:
            rows = production_units.unit_table(Path(runner["doc"]).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError, production_units.Refusal):
            continue
        if any(production_units.cell_value(row.get("Unit", "")) == NIGHTLY_UNIT
               and production_units.cell_value(row.get("Plan", "")) == "permanent" for row in rows):
            found.append(NightlyProduction(Path(runner["doc"]), runner["slug"]))
    if not found:
        return f"no running production has a permanent {NIGHTLY_UNIT} row; add it with add_unit.py {SESSION} --permanent"
    if len(found) > 1:
        return f"{len(found)} productions hold {NIGHTLY_UNIT}: " + ", ".join(item.slug for item in found)
    return found[0]


def live_unit(slug: str) -> LiveClaude | None:
    if TYPE_CHECKING:
        from scripts.production import unit_lookup
    else:
        import unit_lookup

    unit = unit_lookup.marked_unit(slug, NIGHTLY_UNIT)
    return unit.claude if unit is not None and isinstance(unit.claude, unit_lookup.LiveClaude) else None


def tonight(day: date) -> list[str]:
    """Send the night's review to the live unit, starting it first through add_unit when it is gone."""
    production = nightly_production()
    if isinstance(production, str):
        return [f"skipped: {production}"]
    lines: list[str] = []
    try:
        claude = live_unit(production.slug)
        if claude is None:
            # add_unit picks the account, checks shutdown holds and trust, and keeps the unit's launch flags.
            started = subprocess.run([sys.executable, str(SCRIPTS / "production" / "add_unit.py"), "--production",
                                      str(production.doc), SESSION, "--permanent"], capture_output=True, text=True, check=False)
            if started.returncode != 0:
                return [f"{SESSION}: not started: {failure_detail(started)}"]
            lines.append(f"{SESSION}: started")
            claude = live_unit(production.slug)
    except OSError as error:
        return [f"skipped: units unreadable: {error}"]
    if claude is None:
        return [*lines, f"{SESSION}: no live Claude carries the unit's mark"]
    sent = subprocess.run([sys.executable, str(SCRIPTS / "message" / "send.py"), "--to", f"uds:{claude.socket}",
                           "--from", "nightly-review timer", "--key", f"nightly-{day.isoformat()}", "--text", job(day)],
                          capture_output=True, text=True, check=False)
    outcome = "tonight's review sent" if sent.returncode == 0 else f"tonight's review not sent: {sent.stderr.strip()}"
    return [*lines, f"{SESSION}: {outcome}"]


def launch(today: date) -> list[str]:
    directory = night_dir(today)
    directory.mkdir(parents=True, exist_ok=True)
    lines = tonight(today)
    _ = (directory / "launch.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main() -> None:
    match sys.argv[1:]:
        case ["messages"]:
            for line in messages(datetime.now(timezone.utc) - timedelta(hours=24)):
                print(line)
        case ["launch"]:
            for line in launch(date.today()):
                print(line)
        case _:
            sys.exit("usage: nightly_review.py launch")


if __name__ == "__main__":
    main()
