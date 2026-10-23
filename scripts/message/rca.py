#!/usr/bin/env python3
"""Find and fix why a session stopped for a permission, so the same command never stops it again.

  rca.py open --session S --machine M --kind prompt|auto_mode [--command C ...] [--transcript P]
              [--production SLUG]
  rca.py start ID --owner UNIT
  rca.py fixed ID --cause TEXT --fix TEXT --commit SHA
  rca.py list
  rca.py remind

A permission alert to the user opens a case: stall_watch.py opens one after its prompt push, and send.py
after an auto_mode.refused push. A new case sends its owner one message with the steps. An open or started
case for the same session and the same commands counts one more alert instead. The owner is the showrunner
of the session's production (--production, else the production mark the session's processes carry), or the
natedev session when there is none or it is not running. The case's transcript is the stopped session's:
--transcript, else the newest transcript of the live session on this machine that --session names, else none.

`start` and `fixed` each send the user one push, permission.rca_started and permission.rca_fixed, with the
key rca-<id>: while silence is on, the fixed push replaces the started one. `remind`, which stall_watch.py
runs on each pass, sends the owner one reminder for a case still open 30 minutes after it opened.

The owner message and the reminder start send.py detached and do not wait for it, as a relay can take
minutes; its output goes to STATE/owner.log. The start and fixed pushes wait for send.py and print its outcome.

Each case is one JSON file in STATE, which is $XDG_STATE_HOME/message/rca or ~/.local/state/message/rca.
`open` never fails its caller: the alert already went out, so an error prints to stderr and it exits 0.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "production"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
import push_texts  # noqa: E402
import sessions  # noqa: E402
import showrunners  # noqa: E402
from user_zone import user_zone  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "message" / "rca"
SEND = Path(os.environ.get("RCA_SEND") or Path(__file__).resolve().parent / "send.py")
CALL = "~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py"
PROJECTS = Path.home() / ".claude" / "projects"
# unit_lookup.PRODUCTION_MARK: the production slug every process of a unit's session inherits.
# unit_lookup itself is not imported here, as its herdr import costs 160 ms.
PRODUCTION_MARK = "SHOWRUNNER_UNIT"
# The owner when the session has no production, or its showrunner does not run.
NATEDEV = "natedev"
SENDER = "rca"
# The push sender when the session has no production.
NO_PRODUCTION = "messaging"
STARTED_TEXT = "permission.rca_started"
FIXED_TEXT = "permission.rca_fixed"
REMIND_AFTER = timedelta(minutes=30)
# send.py's REFUSED_COMMAND_LENGTH: a longer command shows its first 89 characters and "…".
COMMAND_LENGTH = 90
# A user push waits for its review; a Claude recipient falls back to a relay.
SEND_SECONDS = 300
# The output of each detached owner message and reminder, after a line with its time and key.
OWNER_LOG = "owner.log"
SHORT_SHA = re.compile(r"[0-9a-f]{9,40}")

Kind = Literal["prompt", "auto_mode"]
Status = Literal["open", "started", "fixed"]


class Case(TypedDict):
    id: str
    session: str
    machine: str
    production: str
    commands: list[str]
    kind: Kind
    transcript: str
    opened: str
    status: Status
    owner: str
    cause: str
    fix: str
    commit: str
    reminded: bool
    alerts: int


class Report(NamedTuple):
    """One permission alert that went to the user: the facts a case opens with."""
    session: str
    machine: str
    kind: Kind
    commands: tuple[str, ...]
    transcript: str = ""
    production: str = ""


def now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def locked() -> Generator[None]:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def case_path(case_id: str) -> Path:
    return STATE / f"{case_id}.json"


def read_case(path: Path) -> Case | None:
    try:
        return cast(Case, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def cases() -> list[Case]:
    found = [case for path in sorted(STATE.glob("*.json")) if (case := read_case(path)) is not None]
    return sorted(found, key=lambda case: case["opened"])


def save(case: Case) -> None:
    path = case_path(case["id"])
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(case, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def one_line(text: str) -> str:
    return " ".join(text.split())


def new_id(at: datetime) -> str:
    """The date in the user's zone and a counter: 261023-1 is the first case of 2026-10-23."""
    day = at.astimezone(user_zone()).strftime("%y%m%d")
    return f"{day}-{len(list(STATE.glob(f'{day}-*.json'))) + 1}"


def transcript_of(report: Report) -> str:
    """The given transcript, else the newest transcript of the live session the report names, else "".

    Never the caller's own: a showrunner or a session that sends auto_mode.refused reports another session."""
    if report.transcript:
        return report.transcript
    session = sessions.named(report.session, sessions.live_sessions())
    found = sessions.latest_transcript(session["sessionId"], PROJECTS) if session is not None else None
    return str(found or "")


def production_of(report: Report) -> str:
    slug = report.production or os.environ.get(PRODUCTION_MARK, "")
    return showrunners.canonical_slug(slug) if slug else ""


def owner_address(production: str) -> str:
    """Where the case's owner gets messages: the production's showrunner, else the natedev session."""
    if not production:
        return NATEDEV
    try:
        return showrunners.current_address(production) or NATEDEV
    except (OSError, ValueError):
        return NATEDEV


def shown_commands(commands: list[str]) -> str:
    """One command, "a and b", or "a, b and c", each cut to fit the phone."""
    shown = [command if len(command) <= COMMAND_LENGTH else command[:COMMAND_LENGTH - 1] + "…" for command in commands]
    if not shown:
        return "permission"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def owner_message(case: Case, *, reminder: bool = False) -> str:
    stop = "stopped at a permission prompt" if case["kind"] == "prompt" else "was stopped by auto mode"
    first = (f"rca {case['id']} is still open after 30 minutes. " if reminder else f"rca {case['id']}: ")
    transcript = ["Transcript: " + case["transcript"]] if case["transcript"] else []
    lines = [first + f"{case['session']} on {case['machine']} {stop}.",
             "Commands: " + ("; ".join(case["commands"]) or "none recorded"),
             *transcript,
             f"Find why it stopped and fix the source. Run the rca.py commands on {case['machine']}:",
             f"1. Run `{CALL} start {case['id']} --owner <unit you route the fix to>`.",
             "2. Find where the command came from: a work order, a helper prompt, a skill or a script.",
             "3. Route the fix to the unit that owns that source, so the command never needs a prompt "
             + "(an allow rule only when the command is safe and expected).",
             f"4. After the fix merges, run `{CALL} fixed {case['id']} --cause \"<cause>\" --fix \"<fix>\" --commit <sha>`.",
             "Steps 1 and 4 each send the user a push."]
    return "\n".join(lines)


def send(argv: list[str]) -> tuple[bool, str]:
    """Run send.py; whether it exited 0, and what it printed."""
    try:
        result = subprocess.run([sys.executable, str(SEND), *argv], capture_output=True, text=True,
                                timeout=SEND_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def tell_owner(case: Case, *, reminder: bool = False) -> str:
    """Start the owner message detached, so the caller does not wait for its relay: "" once it started, else why
    it did not start. A message that fails after it starts goes to OWNER_LOG; the reminder covers a first message."""
    key = f"rca-{case['id']}" + ("-remind" if reminder else "")
    argv = [sys.executable, str(SEND), "--to", owner_address(case["production"]), "--from", SENDER, "--key", key,
            "--text", owner_message(case, reminder=reminder)]
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / OWNER_LOG, "a", encoding="utf-8") as log:
            _ = log.write(f"{now().isoformat()} {key}\n")
            log.flush()
            _ = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as error:
        return str(error)
    return ""


def push(case: Case, text_id: str, values: dict[str, str]) -> tuple[bool, str]:
    sender = case["production"] or NO_PRODUCTION
    rendered = push_texts.render(text_id, sender=sender, session=case["session"], **values)
    command = push_texts.user_send_argv(rendered, sender=sender)
    # The send script is the one this module is set to use, so a test's stands in for it.
    return send([*command[2:], "--key", f"rca-{case['id']}"])


def open_case(report: Report, at: datetime | None = None) -> str:
    """Open a case for the alert, or join the open one for the same session and commands.

    It never raises: the alert already went out, so an error is only printed."""
    try:
        commands = list(dict.fromkeys(line for command in report.commands if (line := one_line(command))))
        with locked():
            for case in cases():
                if (case["status"] != "fixed" and case["session"] == report.session
                        and set(case["commands"]) == set(commands)):
                    case["alerts"] += 1
                    save(case)
                    return f"rca: joined {case['id']}"
            opened = at or now()
            case = Case(id=new_id(opened), session=report.session, machine=report.machine,
                        production=production_of(report), commands=commands, kind=report.kind,
                        transcript=transcript_of(report), opened=opened.isoformat(), status="open", owner="",
                        cause="", fix="", commit="", reminded=False, alerts=1)
            save(case)
        failure = tell_owner(case)
        if failure:
            print(f"rca: {case['id']}: the owner message did not start, a reminder follows: {failure}", file=sys.stderr)
        return f"rca: opened {case['id']}"
    except Exception as error:  # noqa: BLE001  the caller's alert went out; a case is extra
        message = f"rca: no case opened: {type(error).__name__}: {error}"
        print(message, file=sys.stderr)
        return message


def loaded(case_id: str) -> Case:
    case = read_case(case_path(case_id))
    if case is None:
        raise ValueError(f"no case {case_id} on this machine; `rca.py list` shows the open ones")
    return case


def start(case_id: str, owner: str) -> tuple[bool, str]:
    with locked():
        case = loaded(case_id)
        if case["status"] == "fixed":
            raise ValueError(f"case {case_id} is fixed")
        case["status"], case["owner"] = "started", owner
        save(case)
    return push(case, STARTED_TEXT, {"owner": owner, "commands": shown_commands(case["commands"])})


def fixed(case_id: str, cause: str, fix: str, commit: str) -> tuple[bool, str]:
    """Close the case. The template ends cause and fix with its own period."""
    commit = commit.strip()
    with locked():
        case = loaded(case_id)
        case["status"], case["commit"] = "fixed", commit
        case["cause"], case["fix"] = one_line(cause).rstrip("."), one_line(fix).rstrip(".")
        save(case)
    shown = commit[:8] if SHORT_SHA.fullmatch(commit) else commit
    return push(case, FIXED_TEXT, {"cause": case["cause"], "fix": case["fix"], "commit": shown})


def remind(at: datetime | None = None) -> None:
    """Tell the owner once of each case still open 30 minutes after it opened. It never raises."""
    try:
        late = [case for case in cases() if case["status"] == "open" and not case["reminded"]
                and (at or now()) - datetime.fromisoformat(case["opened"]) >= REMIND_AFTER]
        for case in late:
            failure = tell_owner(case, reminder=True)
            if failure:
                print(f"rca: {case['id']}: the reminder did not start: {failure}", file=sys.stderr)
                continue
            with locked():
                current = read_case(case_path(case["id"]))
                if current is not None:
                    current["reminded"] = True
                    save(current)
            print(f"rca: reminded {case['id']}")
    except Exception as error:  # noqa: BLE001  a pass of the caller must not end here
        print(f"rca: no reminders sent: {type(error).__name__}: {error}", file=sys.stderr)


def listing() -> str:
    lines: list[str] = []
    for case in cases():
        if case["status"] == "fixed":
            continue
        opened = datetime.fromisoformat(case["opened"]).astimezone(user_zone()).strftime("%m-%d %H:%M %Z")
        owner = f" owner {case['owner']}" if case["owner"] else ""
        lines.append(f"{case['id']}  {case['status']}{owner}  {case['session']} on {case['machine']}  {opened}"
                     + f"  alerts {case['alerts']}  {'; '.join(case['commands']) or 'none recorded'}")
    return "\n".join(lines) or "no open cases"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rca.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="action", required=True)
    opening = commands.add_parser("open", help="open a case for a permission alert, or join its open case")
    _ = opening.add_argument("--session", required=True)
    _ = opening.add_argument("--machine", required=True)
    _ = opening.add_argument("--kind", required=True, choices=("prompt", "auto_mode"))
    _ = opening.add_argument("--command", action="append", default=[], help="a command that needed permission")
    _ = opening.add_argument("--transcript", default="", help="default: the transcript of the session --session names")
    _ = opening.add_argument("--production", default="", help="default: the session's production mark")
    starting = commands.add_parser("start", help="name the unit that fixes the case; the user gets a push")
    _ = starting.add_argument("id")
    _ = starting.add_argument("--owner", required=True)
    fixing = commands.add_parser("fixed", help="close the case after its fix merges; the user gets a push")
    _ = fixing.add_argument("id")
    _ = fixing.add_argument("--cause", required=True)
    _ = fixing.add_argument("--fix", required=True)
    _ = fixing.add_argument("--commit", required=True)
    _ = commands.add_parser("list", help="the open and started cases")
    _ = commands.add_parser("remind", help="remind the owner of each case still open after 30 minutes")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    if action == "open":
        print(open_case(Report(cast(str, args.session), cast(str, args.machine), cast(Kind, args.kind),
                               tuple(cast(list[str], args.command)), cast(str, args.transcript),
                               cast(str, args.production))))
        return 0
    if action == "list":
        print(listing())
        return 0
    if action == "remind":
        remind()
        return 0
    try:
        sent, detail = (start(cast(str, args.id), cast(str, args.owner)) if action == "start"
                        else fixed(cast(str, args.id), cast(str, args.cause), cast(str, args.fix), cast(str, args.commit)))
    except ValueError as error:
        print(f"rca: {error}", file=sys.stderr)
        return 2
    print(detail)
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
