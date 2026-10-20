#!/usr/bin/env python3
"""Silence pushes while the user is away: `auto_silence.py [SPAN | off] | active | check`.

A SPAN (45min, 1h) is the idle length: after that long with no prompt typed in a showrunner or unit
director session, `check` tells the user, then turns silence on with the auto marker. `active`, the
prompt hook's call, records the prompt; when the silence is an auto silence it ends it as
`silence.py off` does and tells the user. A silence set by hand is never ended here. `off` stops
auto silence; no argument shows it. Off until set.

The idle length and the last prompt's time are in one file next to silence.json. `check` runs every
minute as the notifier job `auto-silence`, which a SPAN makes and `off` removes. natedev holds the
state: on the Mac every subcommand runs there over ssh.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import push_texts  # noqa: E402
import silence  # noqa: E402
from silence import Deliver, Run  # noqa: E402
from user_zone import user_zone  # noqa: E402

SENDER = "natedev"
JOB = "auto-silence"
NOTIFIER = Path.home() / ".claude/scripts/message/notifier.sh"
SCRIPT = Path.home() / ".claude/scripts/notify/auto_silence.py"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/auto_silence.py"'
ON_TEXT = "silence.auto_on"
OFF_TEXT = "silence.auto_off"
# The job's run may wait on a reviewed push; the notifier's own default cuts it at 120 s.
JOB_TIMEOUT_SECONDS = 300

Send = Callable[[Sequence[str]], int]
Clock = Callable[[], float]


class AutoRecord(TypedDict):
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
            "started_for": float(started) if isinstance(started, int | float) else None}


def write_config(record: AutoRecord) -> None:
    """The caller holds silence's lock."""
    scratch = config_file().with_suffix(".tmp")
    _ = scratch.write_text(json.dumps(record), encoding="utf-8")
    _ = scratch.replace(config_file())


def send(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def tell(text_id: str, sender: Send, **values: str) -> int:
    rendered = push_texts.render(text_id, sender=SENDER, **values)
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


def come_back(sender: Send, runner: Run, deliver: Deliver) -> int:
    """End an auto silence as `off` does and tell the user; any other silence stays."""
    claimed = silence.claim(lambda record: record["auto"])
    if claimed is None:
        return 0
    record, kept = claimed
    code = silence.release(record, kept, runner, deliver, "silence off")
    return max(code, tell(OFF_TEXT, sender, kept=str(len(kept))))


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


def active(clock: Clock, sender: Send, runner: Run, deliver: Deliver) -> int:
    with silence.locked():
        record = read_config()
        write_config({**record, "last_active": clock()})
    return come_back(sender, runner, deliver)


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
        if not returned and silence.read_state() is None:
            silence.write_state({"since": datetime.now(user_zone()).isoformat(), "until": None,
                                 "timer": None, "auto": True})
            print(f"silence on after {minutes(idle)} min idle {silence.UNTIL_BACK}")
        write_config({**now, "started_for": record["last_active"]})
    if returned:
        return max(code, tell(OFF_TEXT, sender, kept="0"))
    return code


def main(argv: Sequence[str] | None = None, runner: Run = silence.run, system: str | None = None,
         remote: Run = silence.passthrough, sender: Send = send, deliver: Deliver | None = None,
         clock: Clock = time.time) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) > 1:
        print(__doc__, file=sys.stderr)
        return 2
    if silence.on_mac(system):
        return remote([*silence.SSH, f"{REMOTE} {shlex.join(arguments)}"])
    word = arguments[0] if arguments else ""
    kept_poster = deliver or silence.post_kept
    if word == "":
        return show(clock)
    if word == "active":
        return active(clock, sender, runner, kept_poster)
    if word == "check":
        return check(clock, sender)
    if word == "off":
        return turn_off(runner, sender, kept_poster)
    seconds = silence.span_seconds(word)
    if not seconds:
        print(f"auto silence: {word!r} is not a span such as 45min or 1h, nor off", file=sys.stderr)
        return 2
    return set_length(seconds, runner, clock)


if __name__ == "__main__":
    sys.exit(main())
