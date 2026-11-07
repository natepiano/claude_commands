#!/usr/bin/env python3
"""Silence pushes while the user is away: `auto_silence.py [on | SPAN | off] | active | check`.

`on` sets the idle interval to DEFAULT_IDLE (2 h). A SPAN sets another interval.
`check` starts automatic silence after that interval without user activity.
`active` receives the classified prompt source and its session identity.
Only UserTypedPrompt changes last_active or ends automatic silence.
Other sources preserve silence and append an ignored activity event.
An end event precedes the return notice and the release of held pushes.
`off` disables automatic silence. Manual silence remains separate.

The idle length and the last prompt's time are in one file next to silence.json. `check` runs every
minute as the notifier job `auto-silence`, which `on` or a SPAN makes and `off` removes. natedev holds the
state: on the Mac every subcommand runs there over ssh.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import socket
import uuid
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.notify import push_texts  # noqa: E402
from scripts.notify import silence  # noqa: E402
from scripts.notify.silence import Deliver, Run  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

from scripts.lib.prompt_activity import PromptActivity, UserTypedPrompt, UnknownPrompt, SOURCES
from scripts.lib.session_paths import sessions_directory

SENDER = "natedev"
JOB = "auto-silence"
NOTIFIER = Path.home() / ".claude/scripts/message/notifier.sh"
SCRIPT = Path.home() / ".claude/scripts/notify/auto_silence.py"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/auto_silence.py"'
ON_TEXT = "silence.auto_on"
OFF_TEXT = "silence.auto_off"
# `on` sets this idle length.
DEFAULT_IDLE = 2 * 3600
# The job's run may wait on a reviewed push; the notifier's own default cuts it at 120 s.
JOB_TIMEOUT_SECONDS = 300

Send = Callable[[Sequence[str]], int]
Clock = Callable[[], float]
UNKNOWN_ACTIVITY = UnknownPrompt()


class AutoRecord(TypedDict):
    last_session: NotRequired[str]
    last_name: NotRequired[str]
    idle_seconds: int | None
    last_active: float
    # The last_active a silence was already started for: one auto silence per absence.
    started_for: float | None


def config_file() -> Path:
    return silence.state_dir() / "auto-silence.json"


def read_config() -> AutoRecord:
    off: AutoRecord = {"idle_seconds": None, "last_active": 0.0, "started_for": None}
    try:
        value = cast(object, json.loads(config_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return off
    if not isinstance(value, dict):
        return off
    record = cast(dict[str, object], value)
    idle, last, started = record.get("idle_seconds"), record.get("last_active"), record.get("started_for")
    return {"idle_seconds": idle if isinstance(idle, int) and idle > 0 else None,
            "last_active": float(last) if isinstance(last, int | float) else 0.0,
            "started_for": float(started) if isinstance(started, int | float) else None,
            "last_session": str(record.get("last_session", "")), "last_name": str(record.get("last_name", ""))}


def write_config(record: AutoRecord) -> None:
    """The caller holds silence's lock."""
    scratch = config_file().with_suffix(".tmp")
    _ = scratch.write_text(json.dumps(record), encoding="utf-8")
    _ = scratch.replace(config_file())


def send(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def tell(text_id: str, sender: Send, *, session_name: str = SENDER, **values: str) -> int:
    rendered = push_texts.render(text_id, sender=session_name, **values)
    code = sender(push_texts.user_send_argv(rendered))
    if code != 0:
        print(f"auto silence: the {text_id} push was not sent (exit {code})", file=sys.stderr)
    return code


def minutes(seconds: float) -> str:
    return str(int(seconds // 60))


def job_argv() -> list[str]:
    run = shlex.join([str(silence.PY), str(SCRIPT), "check"])
    return ["zsh", str(NOTIFIER), "new", JOB, "--every", "1", "--timeout", str(JOB_TIMEOUT_SECONDS), "--run", run]


def set_length(seconds: int, runner: Run, clock: Clock) -> int:
    """Start counting from now: the user is typing this."""
    if runner(job_argv()) != 0:
        print("auto silence: the notifier job could not be made; nothing changed", file=sys.stderr)
        return 1
    with silence.locked():
        write_config({"idle_seconds": seconds, "last_active": clock(), "started_for": None})
    print(f"auto silence after {minutes(seconds)} min idle")
    return 0


def session_name(session_id: str) -> str:
    """Use the activity session's registry name, or its identity when unavailable."""
    for path in sessions_directory().glob("*.json"):
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            record = cast(dict[str, object], raw)
            if record.get("sessionId") == session_id:
                name = record.get("name")
                if isinstance(name, str) and name:
                    return name
    return session_id or "manual command"


def append_event(event: str, cause: str, activity: PromptActivity, record: AutoRecord,
                 previous: silence.SilenceRecord | None, next_state: str, now: float) -> None:
    """Append the cause under the silence lock before changing silence or releasing pushes."""
    state = "automatic" if previous and previous["auto"] else "manual" if previous else "off"
    entry = {"event_id": uuid.uuid4().hex, "timestamp_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
             "event": event, "cause": cause, "silence_id": previous["since"] if previous else "",
             "previous_state": "off" if event in {"start", "cancelled_start"} else state, "next_state": next_state, "session_id": activity.session_id,
             "pane_id": activity.pane_id, "machine": socket.gethostname(),
             "prompt_source": type(activity).__name__, "evidence_kind": activity.evidence_kind,
             "prompt_hash": activity.prompt_hash, "idle_seconds": record["idle_seconds"],
             "last_active": record["last_active"], "held_count": len(silence.read_kept()), "result": event}
    with (silence.state_dir() / "auto-silence-events.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(entry) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def record_command_event(event: str, cause: str, record: silence.SilenceRecord, next_state: str) -> None:
    """Record an automatic transition requested by the silence command."""
    append_event(event, cause, UnknownPrompt(evidence_kind="silence_command"),
                 read_config(), record, next_state, time.time())


def come_back(sender: Send, runner: Run, deliver: Deliver) -> int:
    """An explicit off command ends automatic silence, but preserves manual silence."""
    with silence.locked():
        previous = silence.read_state()
        if previous is None or not previous["auto"]:
            return 0
        try:
            append_event("end", "manual_off", UnknownPrompt(evidence_kind="manual_command"),
                         read_config(), previous, "off", time.time())
        except OSError as error:
            print(f"auto silence: event append failed: {error}", file=sys.stderr)
            return 1
        kept = silence.read_kept()
        silence.state_file().unlink()
    code = tell(OFF_TEXT, sender, held=held(len(kept)))
    return max(code, silence.release(previous, kept, runner, deliver, "silence off"))


def held(count: int) -> str:
    """The silence.auto_off sentence: what silence kept, which comes next."""
    if count == 0:
        return "No pushes were held while you were away."
    if count == 1:
        return "The push held while you were away comes next."
    return f"The {count} pushes held while you were away come next."


def turn_off(runner: Run, sender: Send, deliver: Deliver) -> int:
    with silence.locked():
        record = read_config()
        write_config({**record, "idle_seconds": None})
    _ = runner(["zsh", str(NOTIFIER), "remove", JOB])
    print("auto silence off")
    return come_back(sender, runner, deliver)


def show(clock: Clock) -> int:
    record = read_config()
    if record["idle_seconds"] is None:
        print("auto silence off")
        return 0
    last = record["last_active"]
    print(f"auto silence after {minutes(record['idle_seconds'])} min idle; "
          + f"last prompt {silence.shown(datetime.fromtimestamp(last, user_zone()))}, "
          + f"{minutes(clock() - last)} min ago")
    return 0


def active(clock: Clock, sender: Send, runner: Run, deliver: Deliver,
           activity: PromptActivity = UNKNOWN_ACTIVITY) -> int:
    with silence.locked():
        record = read_config()
        previous = silence.read_state()
        state = "automatic" if previous and previous["auto"] else "manual" if previous else "off"
        typed = isinstance(activity, UserTypedPrompt) and bool(activity.session_id)
        ending = typed and previous is not None and previous["auto"]
        try:
            append_event("end" if ending else "activity" if typed else "ignored_activity",
                         "user_prompt" if typed else type(activity).__name__, activity, record, previous,
                         "off" if ending else state, clock())
        except OSError as error:
            print(f"auto silence: event append failed: {error}", file=sys.stderr)
            return 1
        if not typed:
            return 0
        name = session_name(activity.session_id)
        write_config({**record, "last_active": clock(), "last_session": activity.session_id, "last_name": name})
        if previous is None or not previous["auto"]:
            return 0
        kept = silence.read_kept()
        silence.state_file().unlink()
    code = tell(OFF_TEXT, sender, session_name=name, held=held(len(kept)))
    return max(code, silence.release(previous, kept, runner, deliver, "silence off"))


def idle_for(record: AutoRecord, now: float) -> float | None:
    """How long the user has been idle, when that starts an auto silence now."""
    length = record["idle_seconds"]
    if length is None or record["started_for"] == record["last_active"]:
        return None
    idle = now - record["last_active"]
    return idle if idle >= length else None


def check(clock: Clock, sender: Send) -> int:
    """Tell the user first, so the push is not kept; then start silence unless they came back."""
    with silence.locked():
        record = read_config()
        idle = idle_for(record, clock())
        if idle is None or silence.read_state() is not None:
            return 0
    code = tell(ON_TEXT, sender, minutes=minutes(idle))
    with silence.locked():
        now = read_config()
        returned = now["last_active"] != record["last_active"]
        previous = silence.read_state()
        if previous is not None:
            return code
        started: silence.SilenceRecord = {"since": datetime.fromtimestamp(clock(), timezone.utc).isoformat(),
                                          "until": None, "timer": None, "auto": True}
        activity = UserTypedPrompt(now.get("last_session", "")) if returned else UnknownPrompt(evidence_kind="idle_timer")
        try:
            append_event("cancelled_start" if returned else "start",
                         "start_cancelled_by_user_prompt" if returned else "idle_threshold", activity,
                         now, started, "off" if returned else "automatic", clock())
        except OSError as error:
            silence.write_state(started)
            print(f"auto silence: event append failed: {error}", file=sys.stderr)
            return 1
        if not returned:
            silence.write_state(started)
            print(f"silence on after {minutes(idle)} min idle {silence.UNTIL_BACK}")
        write_config({**now, "started_for": record["last_active"]})
    if returned:
        return max(code, tell(OFF_TEXT, sender, session_name=now.get("last_name", "") or now.get("last_session", ""), held=held(0)))
    return code


def main(argv: Sequence[str] | None = None, runner: Run = silence.run, system: str | None = None,
         remote: Run = silence.passthrough, sender: Send = send, deliver: Deliver | None = None,
         clock: Clock = time.time) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) > 1 and arguments[0] != "active":
        print(__doc__, file=sys.stderr)
        return 2
    if silence.on_mac(system):
        return remote([*silence.SSH, f"{REMOTE} {shlex.join(arguments)}"])
    word = arguments[0] if arguments else ""
    kept_poster = deliver or silence.post_kept
    if word == "":
        return show(clock)
    if word == "active":
        parser = argparse.ArgumentParser(prog="auto_silence.py active")
        _ = parser.add_argument("--source", choices=list(SOURCES), default="UnknownPrompt")
        _ = parser.add_argument("--session-id", default="")
        _ = parser.add_argument("--pane-id", default="")
        _ = parser.add_argument("--prompt-hash", default="")
        parsed = parser.parse_args(arguments[1:])
        source = SOURCES[cast(str, parsed.source)]
        activity = source(cast(str, parsed.session_id), cast(str, parsed.pane_id), cast(str, parsed.prompt_hash))
        return active(clock, sender, runner, kept_poster, activity)
    if word == "check":
        return check(clock, sender)
    if word == "off":
        return turn_off(runner, sender, kept_poster)
    seconds = DEFAULT_IDLE if word == "on" else silence.span_seconds(word)
    if not seconds:
        print(f"auto silence: {word!r} is not on, off, nor a span such as 45min or 1h", file=sys.stderr)
        return 2
    return set_length(seconds, runner, clock)


if __name__ == "__main__":
    sys.exit(main())
