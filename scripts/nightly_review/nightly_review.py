#!/usr/bin/env python3
"""The nightly review: one read-only session, one improvement proposal per mode, queued for the user.

`launch` runs from the nightly-review timer on natedev (/etc/nixos/modules/linux/nightly-review.nix).
It starts `/nightly_review config rust messages` in one named, detached tmux session with Remote Control under
the same name, replacing the previous night's unless the user is attached to it. A session under that
name that no nightly review started is left running, and the review skips the night. The session runs
on the account with room whose weekly allowance resets soonest.
It does the modes in order; for each it writes NIGHT/<mode>.md and messages natedev, which distills
the proposals into NIGHT/digest.md and the ledger (/watcher). The night is skipped while that account
is shut down, is not fit to launch, or no account has room.

Nothing offers the proposals: the ledger's queue grows until the user asks for /nightly_next
(user, 2026-10-10).
"""

from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

# The quota readers live with the agent notes, one directory over.
SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "whoami"))
sys.path.insert(0, str(SCRIPTS / "shutdown"))
from launch_permission import (  # noqa: E402
    BlockedByShutdown,
    NewWorkLaunchPurpose,
    ShutdownStateUnreadable,
    launch_barrier,
    launch_permission,
)
from account import NAMED_ACCOUNT_VARIABLES  # noqa: E402
from agent_accounts import ClaudeAccountsInUse  # noqa: E402
import account  # noqa: E402

ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "nightly-review"
MODES = ("config", "rust", "messages")
SESSION = "nightly-review"
# The two sessions a night had before one session ran every mode.
LEGACY_SESSIONS = ("nightly-config", "nightly-rust")
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
                directory = Path(config) if config else (account.process_config_dir(pid) if pid > 0 else None)
                if directory is None:
                    print(f"messages: account directory unavailable for {session}", file=sys.stderr)
                    continue
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
            if any(data.get(key) for key in ("replay", "dry", "sample")):
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


@dataclass(frozen=True)
class QuotaClear:
    """The account the review launches on has enough weekly quota to launch work."""


@dataclass(frozen=True)
class ReviewAccountUnavailable:
    """Tonight's run must wait for weekly quota."""

    reason: str


QuotaPermission = QuotaClear | ReviewAccountUnavailable


def night_dir(day: date) -> Path:
    return ROOT / day.isoformat()


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux is not installed")


def running(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}").returncode == 0


def quota_permission() -> QuotaPermission:
    from scripts.accounts import accounts

    return quota_for_selection(accounts.claude_account_selection())


def quota_for_selection(_selection: ClaudeAccountsInUse) -> QuotaPermission:
    """The picker decides room without a minimum percentage."""
    from scripts.accounts import accounts

    chosen = accounts.pick("claude", datetime.now(timezone.utc), keep=accounts.NoKeep())
    return ReviewAccountUnavailable(accounts.no_room_line("claude", chosen)) if isinstance(chosen, accounts.NoAccountWithRoom) else QuotaClear()


@dataclass(frozen=True)
class LeftRunning:
    """A session under a review name stays, with the line that says why."""

    line: str


@dataclass(frozen=True)
class NameFree:
    """No session holds the name now."""


def retire(name: str, attached: str, foreign: str) -> LeftRunning | NameFree:
    """End a past night's session unless the user is attached or no review started it."""
    if not running(name):
        return NameFree()
    if tmux("list-clients", "-t", f"={name}").stdout.strip():
        return LeftRunning(f"{name}: {attached}")
    # A session promoted out of a nightly review keeps its tmux name.
    if "/nightly_review " not in tmux("list-panes", "-F", "#{pane_start_command}", "-t", f"={name}:").stdout:
        return LeftRunning(f"{name}: {foreign}")
    _ = tmux("kill-session", "-t", f"={name}")
    return NameFree()


def start(_selection: ClaudeAccountsInUse, prefix: str, environment: dict[str, str]) -> list[str]:
    """Replace last night's session with tonight's; `prefix` selects a named account."""
    lines = [
        left.line for name in LEGACY_SESSIONS
        if isinstance(left := retire(
            name, "you are attached, so it was left running",
            "another session holds this name, so it was left running",
        ), LeftRunning)
    ]
    last_night = retire(
        SESSION, "you are attached, so last night's session was left running",
        "another session holds this name, so tonight's review did not start",
    )
    if isinstance(last_night, LeftRunning):
        return [*lines, last_night.line]
    # The session works in ~/rust; the config mode also reads /etc/nixos and ~/.claude.
    home = Path.home()
    argv = ["claude", "--remote-control", SESSION, "-n", SESSION,
            "--add-dir", "/etc/nixos", "--add-dir", str(home / ".claude"),
            "--settings", str(SETTINGS), PROMPT]
    # A scope of its own, so a tmux server this starts outlives the timer's oneshot unit.
    command = f"{prefix}ENABLE_TOOL_SEARCH=true command {shlex.join(argv)}; exec zsh"
    command = f"unset {' '.join(NAMED_ACCOUNT_VARIABLES)} CLAUDE_CODE_OAUTH_TOKEN; " + command
    spawn = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "tmux", "new-session", "-d", "-s", SESSION,
             "-c", str(home / "rust"), "zsh", "-ic", command]
    started = subprocess.run(spawn, capture_output=True, text=True, check=False, env=environment)
    outcome = "started" if started.returncode == 0 else f"failed to start: {started.stderr.strip()}"
    return [*lines, f"{SESSION}: {outcome}"]


def launch_environment(_selection: ClaudeAccountsInUse) -> dict[str, str]:
    """The launch environment without an inherited Claude account."""
    return {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}


def tonight(selection: ClaudeAccountsInUse) -> list[str]:
    """Start the review on the picked account, or give the one line that says why not."""
    from scripts.accounts import accounts
    from scripts.herdr.herdr import Untrusted, UnreadableTrustFile, claude_trust, trust_command

    try:
        selected = accounts.role_account("unit")
    except accounts.AccountsError as error:
        return [str(error)]
    environment = launch_environment(selection)
    permission_environment = dict(environment)
    if isinstance(selected, accounts.NamedAccount):
        problems = accounts.check(selected.label)
        if problems:
            return [f"skipped: unit account {selected.label}: {problems[0]}"]
        # The shutdown check reads the account the review will run on.
        permission_environment["CLAUDE_CONFIG_DIR"] = str(selected.dir)
    permission = launch_permission(permission_environment, NewWorkLaunchPurpose())
    if isinstance(permission, BlockedByShutdown):
        return [f"skipped: {permission.reason}"]
    if isinstance(permission, ShutdownStateUnreadable):
        return [f"skipped: shutdown state unreadable: {permission.detail}"]
    folder = Path.home() / "rust"
    trust_account = selected
    inherited_config = environment.get("CLAUDE_CONFIG_DIR", "")
    if isinstance(selected, accounts.DefaultAccount) and inherited_config:
        trust_account = accounts.NamedAccount(inherited_config, Path(inherited_config))
    account_file = (trust_account.dir / ".claude.json" if isinstance(trust_account, accounts.NamedAccount)
                    else Path.home() / ".claude.json")
    trust = claude_trust(folder, account_file)
    if isinstance(trust, UnreadableTrustFile):
        return [f"{SESSION}: {trust}"]
    if isinstance(trust, Untrusted):
        label = trust_account.label if isinstance(trust_account, accounts.NamedAccount) else "default account"
        return [f"{SESSION}: Claude is not trusted in {folder} on {label}. Run: {trust_command(folder, trust_account)}"]
    return start(selection, accounts.account_prefix(selected), environment)


def launch(today: date) -> list[str]:
    from scripts.accounts import accounts

    selection = accounts.claude_account_selection()
    directory = night_dir(today)
    directory.mkdir(parents=True, exist_ok=True)
    barrier = launch_barrier()
    try:
        _ = barrier.__enter__()
    except OSError as error:
        lines = [
            f"skipped: shutdown state unreadable: launch barrier: {error}"
        ]
    else:
        try:
            lines = tonight(selection)
        finally:
            _ = barrier.__exit__(None, None, None)
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
