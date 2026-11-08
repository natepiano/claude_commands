#!/usr/bin/env python3
"""The nightly review: one read-only unit, one improvement proposal per mode, queued for the user.

`launch` runs on demand on natedev; the 01:00 nightly-review timer that ran it was retired by
/etc/nixos cc9a0e9 (2026-10-19). The review is the permanent nightly-review of one production:
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
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

# The account reader and the production scripts sit one directory over.
SCRIPTS = Path(__file__).resolve().parent.parent
from scripts.lib.process_failure import failure_detail
from scripts.whoami import account  # noqa: E402

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead
    from scripts.production.session_staff import LiveClaude, RowResolver
    from scripts.production.staff import StaffRow
    from scripts.production.unit_config import UnitConfig

SETTINGS = Path(__file__).with_name("settings.json")
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


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read("nightly_review")


def sessions_directory() -> Path:
    return Path(_settings().text("sessions_directory")).expanduser()


def showrunner_transcripts(since: datetime) -> list[Path]:
    """Find dated replies in each showrunner's own account, including natedev."""
    from scripts.production import staff

    primary = _settings().text("primary_showrunner_name")
    membership = staff.snapshot()
    owners = staff.active(membership, "showrunner")
    identities = {identity for row in owners for identity in (row.id, row.current_session)}
    names = {row.name for row in owners}
    found: set[Path] = set()
    for path in sorted(sessions_directory().glob("*.json")):
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
            if name != primary and session not in identities and name not in names:
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
                candidates = list((directory / "projects").glob(f"*/{session}.jsonl"))
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
    from scripts.notify import review

    counts = live_message_counts(review.state_path() / "review.jsonl", since)
    lines = [f"Live messages: {counts.messages}; failures: {counts.failures}; Unreviewed: {counts.unreviewed}."]
    lines.extend(f"Live rule {rule}: {count}" for rule, count in counts.rules)
    lines.extend(f"Template {text_id} sent despite failed review: {count}" for text_id, count in counts.template_failures)
    transcripts = showrunner_transcripts(since)
    if not transcripts:
        return [*lines, "No showrunner replies in this interval."]
    settings = _settings()
    sample_rows = settings.whole("message_sample_rows")
    try:
        result = subprocess.run([sys.executable, str(REVIEW_SCRIPT), "sample", *(str(path) for path in transcripts),
                                 "--limit", str(sample_rows), "--since", since.isoformat()],
                                capture_output=True, text=True, check=False, timeout=sample_rows * review.user_send_seconds() + settings.number("sample_timeout_overhead_seconds"))
    except (OSError, subprocess.TimeoutExpired) as error:
        return [*lines, f"Sample failed: {error}"]
    lines.extend(result.stdout.splitlines())
    if result.returncode:
        lines.append(f"Sample failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    return lines


def state_root() -> Path:
    from scripts.nightly_review.candidates import state_root as candidate_state_root

    return candidate_state_root()


def night_dir(day: date) -> Path:
    return state_root() / day.isoformat()


def job(day: date) -> str:
    """The night's work, sent to the permanent unit as a message."""
    settings = _settings()
    modes = settings.words("review_modes")
    command = settings.text("command_path")
    return (f"Nightly review for NIGHT={day.isoformat()}: follow {command} "
            + f"with the modes {' '.join(modes)}, in that order.")


@dataclass(frozen=True)
class NightlyUnit:
    unit: StaffRow
    config: UnitConfig


@dataclass(frozen=True)
class NightlyUnitNotSelected:
    """The production identity carries no launch selection."""


@dataclass(frozen=True)
class NightlyProduction:
    doc: Path
    name: str
    selection: NightlyUnit | NightlyUnitNotSelected = field(default_factory=NightlyUnitNotSelected, compare=False)


def nightly_production(*, resolver: RowResolver | None = None) -> NightlyProduction | str:
    """Find the active unit whose configuration assigns the nightly review."""
    from scripts.production import session_staff, staff

    try:
        resolver = resolver if resolver is not None else session_staff.RowResolver(staff.snapshot())
        record = resolver.record
        found: list[NightlyProduction] = []
        unknown: list[str] = []
        for row in staff.active(record, "unit"):
            parent = record.parent_of(row)
            if parent is None or parent.kind != "showrunner" or isinstance(parent.doc, staff.WrappedDoc):
                continue
            try:
                config = resolver.unit_config(row)
            except ValueError as error:
                config = session_staff.ConfigMissing(str(error))
            if not isinstance(config, session_staff.UnitConfig):
                reason = f"{row.name}: {getattr(config, 'reason', 'unit configuration is unavailable')}"
                print(f"nightly_review: {reason}", file=sys.stderr)
                unknown.append(reason)
                continue
            if "nightly-review" not in config.responsibilities:
                continue
            doc = resolver.production_doc(row)
            if not isinstance(doc, session_staff.ProductionDoc):
                reason = f"{row.name}: {doc.reason}"
                print(f"nightly_review: {reason}", file=sys.stderr)
                unknown.append(reason)
                continue
            found.append(NightlyProduction(doc.path, parent.name, NightlyUnit(row, config)))
    except ValueError as error:
        return f"nightly review membership is unavailable: {error}"
    if unknown:
        raise OSError("nightly review ownership is unknown: " + "; ".join(unknown))
    if not found:
        return "no running production has a unit configuration that assigns nightly-review"
    if len(found) > 1:
        return f"{len(found)} productions hold nightly-review: " + ", ".join(item.name for item in found)
    return found[0]


def live_unit(production: NightlyProduction, *, resolver: RowResolver) -> LiveClaude | None:
    """Find the configured unit through its native session identity."""
    from scripts.production import session_staff
    from scripts.message import sessions

    if isinstance(production.selection, NightlyUnitNotSelected):
        return None
    row = resolver.record.by_id(production.selection.unit.id)
    if row is None:
        return None
    for current in sessions.live_sessions():
        if current["sessionId"] == row.current_session:
            return session_staff.LiveClaude(current["name"], current["sessionId"],
                                           current["messagingSocketPath"], current["pid"])
    return None


def tonight(day: date) -> list[str]:
    """Send the night's review to the live unit, starting it first through add_unit when it is gone."""
    from scripts.message import session_send
    from scripts.production import session_staff, staff, unit_config

    resolver = session_staff.RowResolver(staff.snapshot())
    production = nightly_production(resolver=resolver)
    if isinstance(production, str):
        return [f"skipped: {production}"]
    selection = production.selection
    if isinstance(selection, NightlyUnitNotSelected):
        return [f"skipped: {production.name}: the nightly unit is not selected"]
    lines: list[str] = []
    try:
        claude = live_unit(production, resolver=resolver)
        if claude is None:
            plan = selection.config.plan
            launch = (["--permanent"] if isinstance(plan, unit_config.PermanentPlan) else
                      ["--standby"] if isinstance(plan, unit_config.StandbyPlan) else
                      ["--plan", plan.path.as_posix()])
            started = subprocess.run([sys.executable, str(SCRIPTS / "production" / "add_unit.py"), "--production",
                                      production.name, selection.unit.name, *launch], capture_output=True, text=True, check=False)
            if started.returncode != 0:
                return [f"{selection.unit.name}: not started: {failure_detail(started)}"]
            lines.append(f"{selection.unit.name}: started")
            resolver = session_staff.RowResolver(staff.snapshot())
            claude = live_unit(production, resolver=resolver)
    except OSError as error:
        return [f"skipped: units unreadable: {error}"]
    if claude is None:
        return [*lines, f"{selection.unit.name}: no live Claude has the unit's recorded identity"]
    sent = subprocess.run(session_send.send_argv("--to", f"uds:{claude.socket}", "--from", "nightly-review timer",
                                            "--key", f"nightly-{day.isoformat()}", "--text", job(day)),
                          capture_output=True, text=True, check=False)
    outcome = "tonight's review sent" if sent.returncode == 0 else f"tonight's review not sent: {sent.stderr.strip()}"
    return [*lines, f"{selection.unit.name}: {outcome}"]


def launch(today: date) -> list[str]:
    directory = night_dir(today)
    directory.mkdir(parents=True, exist_ok=True)
    lines = tonight(today)
    _ = (directory / "launch.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main() -> None:
    match sys.argv[1:]:
        case ["messages"]:
            hours = _settings().number("message_history_hours")
            for line in messages(datetime.now(timezone.utc) - timedelta(hours=hours)):
                print(line)
        case ["launch"]:
            for line in launch(date.today()):
                print(line)
        case _:
            sys.exit("usage: nightly_review.py launch")


if __name__ == "__main__":
    main()
