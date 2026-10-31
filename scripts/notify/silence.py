#!/usr/bin/env python3
"""Keep the user's pushes for a while: `silence.py on [--until "YYYY-MM-DD HH:MM" | --for SPAN] | off | status`.

While silence is on, pushover.py keeps each push here instead of posting it. A priority 2
(blocked) push always goes out: power and UPS alerts are safety. `off` ends silence and posts
the kept pushes, oldest first, as they would have gone out, and prints how many. A kept push
with the same non-empty key from the same source replaces the older kept one, so `off` posts
only the newest. `on` with no end lasts until `off`; --until (a time in the user's zone,
user_zone.py) or --for (a span: 1h, 45min, 1h30min) makes a transient systemd user timer that
runs `off --timer UNIT` then, ending only the silence that still names it. `on` again replaces the
end. If the end passes without the timer (natedev was off), the next push or silence.py call ends
silence as `off` does.

`drop --source S --key K` removes the kept pushes with that source and non-empty key, by the rule
that makes a newer kept push replace an older one, and prints how many it removed. `send.py retire
--to user` runs it: a retired alert, such as the alert of a fixed rca.py case, never goes out at
silence end.

auto_silence.py turns silence on with the auto marker when the user is idle, and ends only that
silence when they come back; `on` replaces the marker, so a silence set by hand stays until `off`.

natedev holds the state: on the Mac every subcommand, and pushover.py's check, runs there over
ssh. NOTIFY_STATE_DIR, when set, names this host's state directory and keeps everything here.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone, zone_label  # noqa: E402

PY = Path.home() / ".claude/scripts/lib/py"
SILENCE = Path.home() / ".claude/scripts/notify/silence.py"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/silence.py"'
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "natedev"]
UNTIL_OFF = "until off"
UNTIL_BACK = "until you are back"
SPAN_UNITS = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hour": 3600, "hours": 3600,
              "m": 60, "min": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1, "second": 1, "seconds": 1}
SPAN_PART = re.compile(r"\s*(\d+)\s*([a-z]+)")

Run = Callable[[Sequence[str]], int]
Ask = Callable[[Sequence[str], str], tuple[int, str]]
Deliver = Callable[["KeptPush"], int]


class SilenceRecord(TypedDict):
    since: str
    until: str | None
    timer: str | None
    auto: bool


class KeptPush(TypedDict):
    source: str
    priority: str
    title: str
    message: str
    detail: str
    key: str
    text_id: str


@dataclass(frozen=True)
class Kept:
    until: str


@dataclass(frozen=True)
class NotSilent:
    pass


def state_dir() -> Path:
    return Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify")))


def state_file() -> Path:
    return state_dir() / "silence.json"


def kept_file() -> Path:
    return state_dir() / "silence-kept.jsonl"


def on_mac(system: str | None = None) -> bool:
    return (system or platform.system()) == "Darwin" and "NOTIFY_STATE_DIR" not in os.environ


@contextmanager
def locked() -> Generator[None]:
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / "silence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_state() -> SilenceRecord | None:
    try:
        value = cast(object, json.loads(state_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    record = cast(dict[str, object], value)
    since, until, timer = record.get("since"), record.get("until"), record.get("timer")
    if not isinstance(since, str) or not isinstance(until, str | None) or not isinstance(timer, str | None):
        return None
    return {"since": since, "until": until, "timer": timer, "auto": record.get("auto") is True}


def write_state(record: SilenceRecord) -> None:
    """The caller holds the lock."""
    _ = state_file().write_text(json.dumps(record), encoding="utf-8")


def read_kept() -> list[KeptPush]:
    try:
        lines = kept_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    kept: list[KeptPush] = []
    for line in lines:
        push = parse_push(line)
        if push is not None:
            kept.append(push)
    return kept


def parse_push(text: str) -> KeptPush | None:
    try:
        value = cast(object, json.loads(text))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    record = cast(dict[str, object], value)
    # A line kept before pushes had a key or a text id reads as key "" and text id "" (free text).
    found = {name: record.get(name) for name in KeptPush.__annotations__} | {
        "key": record.get("key", ""), "text_id": record.get("text_id", "")}
    if not all(isinstance(item, str) for item in found.values()):
        return None
    return cast(KeptPush, cast(object, found))


def shown(moment: datetime) -> str:
    """HH:MM and zone in the user's zone; the date too when it is not today."""
    local = moment.astimezone(user_zone())
    day = "" if local.date() == datetime.now(user_zone()).date() else local.strftime("%Y-%m-%d ")
    return f"{day}{local:%H:%M} {zone_label(local)}"


def ends(record: SilenceRecord) -> str:
    if record["auto"]:
        return UNTIL_BACK
    return f"until {shown(datetime.fromisoformat(record['until']))}" if record["until"] else UNTIL_OFF


def span_seconds(span: str) -> int | None:
    """A span such as 1h, 45min or 1h30min in seconds; None when it does not parse."""
    parts = cast(list[tuple[str, str]], SPAN_PART.findall(span.lower()))
    if not parts or "".join(f"{count}{unit}" for count, unit in parts) != re.sub(r"\s+", "", span.lower()):
        return None
    if any(unit not in SPAN_UNITS for _count, unit in parts):
        return None
    return sum(int(count) * SPAN_UNITS[unit] for count, unit in parts)


def timer_argv(until: datetime, unit: str) -> list[str]:
    """The systemd-run command that ends this timer's silence once, at `until`."""
    return ["systemd-run", "--user", "--collect", f"--unit={unit}",
            f"--on-calendar={until.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
            "--timer-property=AccuracySec=1s", str(PY), str(SILENCE), "off", "--timer", unit]


def stop_timer(unit: str | None, runner: Run) -> int:
    return runner(["systemctl", "--user", "stop", f"{unit}.timer"]) if unit else 0


def past_end(record: SilenceRecord) -> bool:
    until = record["until"]
    return until is not None and datetime.fromisoformat(until) <= datetime.now(user_zone())


def keep_here(push: KeptPush, runner: Run | None = None, deliver: Deliver | None = None) -> Kept | NotSilent:
    _ = end_if_past(runner, deliver)
    with locked():
        record = read_state()
        if record is None or past_end(record):
            return NotSilent()
        older = read_kept() if push["key"] else []
        newer = [old for old in older if (old["source"], old["key"]) != (push["source"], push["key"])]
        if len(newer) != len(older):
            temporary = kept_file().with_suffix(".tmp")
            _ = temporary.write_text("".join(json.dumps(old) + "\n" for old in newer), encoding="utf-8")
            _ = temporary.replace(kept_file())
        with kept_file().open("a", encoding="utf-8") as out:
            _ = out.write(json.dumps(push) + "\n")
        return Kept(ends(record))


def drop_here(source: str, key: str) -> int:
    """Remove the kept pushes with this source and key, as a newer kept push replaces them; how many."""
    if not key:
        return 0
    with locked():
        kept = read_kept()
        left = [push for push in kept if (push["source"], push["key"]) != (source, key)]
        if len(left) != len(kept):
            temporary = kept_file().with_suffix(".tmp")
            _ = temporary.write_text("".join(json.dumps(push) + "\n" for push in left), encoding="utf-8")
            _ = temporary.replace(kept_file())
    return len(kept) - len(left)


def ask(argv: Sequence[str], text: str) -> tuple[int, str]:
    done = subprocess.run(list(argv), input=text, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def keep(push: KeptPush, system: str | None = None, asker: Ask = ask, deliver: Deliver | None = None) -> Kept | NotSilent:
    """Keep the push when silence is on. On the Mac natedev decides; if it cannot be asked, send."""
    if not on_mac(system):
        return keep_here(push, deliver=deliver)
    code, out = asker([*SSH, f"{REMOTE} keep"], json.dumps(push))
    if code == 0 and out.strip():
        return Kept(out.strip())
    if code != 1:
        print(f"silence: natedev could not be asked (exit {code}); sending", file=sys.stderr)
    return NotSilent()


def drop(source: str, key: str, system: str | None = None, asker: Ask = ask) -> int:
    """Remove the kept pushes with this source and key; how many. On the Mac natedev removes them."""
    if not on_mac(system):
        return drop_here(source, key)
    code, out = asker([*SSH, f"{REMOTE} {shlex.join(['drop', '--source', source, '--key', key])}"], "")
    if code != 0 or not out.strip().isdigit():
        print(f"silence: natedev could not be asked to drop {key} (exit {code})", file=sys.stderr)
        return 0
    return int(out.strip())


def on(until: datetime | None, runner: Run) -> int:
    unit = f"silence-off-{time.time_ns()}" if until else None
    if until and unit and runner(timer_argv(until, unit)) != 0:
        print("silence: the end timer could not be made; silence not changed", file=sys.stderr)
        return 1
    with locked():
        old = read_state()
        record: SilenceRecord = {"since": old["since"] if old else datetime.now(user_zone()).isoformat(),
                                 "until": until.isoformat() if until else None, "timer": unit, "auto": False}
        if stop_timer(old["timer"] if old else None, runner) != 0:
            _ = stop_timer(unit, runner)
            print("silence: the old end timer could not be stopped; silence not changed", file=sys.stderr)
            return 1
        write_state(record)
    print(f"silence on from {shown(datetime.fromisoformat(record['since']))} {ends(record)}")
    return 0


class PushChannel(Protocol):
    def post_kept(self, push: KeptPush) -> int: ...


def post_kept(push: KeptPush) -> int:
    """pushover.py posts it; imported here, as pushover.py imports this module."""
    return cast(PushChannel, cast(object, importlib.import_module("pushover"))).post_kept(push)


def claim(when: Callable[[SilenceRecord], bool] | None = None, *,
          release_if_off: bool = False) -> tuple[SilenceRecord | None, list[KeptPush]] | None:
    """End silence and read the kept pushes, leaving them saved until release posts them.

    With `when`, only a silence it accepts ends; otherwise nothing changes and the result is None.
    `release_if_off` also claims leftover pushes when no silence is saved.
    """
    with locked():
        record = read_state()
        if when is not None and ((record is None and not release_if_off)
                                 or (record is not None and not when(record))):
            return None
        kept = read_kept()
        state_file().unlink(missing_ok=True)
    return record, kept


def release(record: SilenceRecord | None, kept: list[KeptPush], runner: Run, deliver: Deliver, opening: str) -> int:
    _ = stop_timer(record["timer"] if record else None, runner)
    failed = attempted = 0
    with locked():
        pending = read_kept()
        for push in kept:
            # Another release may already have posted this push since claim read it.
            if push not in pending:
                continue
            attempted += 1
            if deliver(push) != 0:
                failed += 1
                continue
            pending.remove(push)
            temporary = kept_file().with_suffix(".tmp")
            _ = temporary.write_text("".join(json.dumps(left) + "\n" for left in pending), encoding="utf-8")
            _ = temporary.replace(kept_file())
        if not pending:
            kept_file().unlink(missing_ok=True)
    plural = "" if attempted == 1 else "es"
    note = f"; {failed} failed, see ~/.local/state/notify/pushover.jsonl" if failed else ""
    print(f"{opening}; {attempted} kept push{plural} delivered{note}", file=sys.stdout if opening == "silence off" else sys.stderr)
    return 1 if failed else 0


def off(runner: Run, deliver: Deliver | None = None, timer: str = "") -> int:
    claimed = claim(lambda record: record["timer"] == timer, release_if_off=True) if timer else claim()
    if claimed is None:
        return 0
    record, kept = claimed
    return release(record, kept, runner, deliver or post_kept, "silence off")


def end_if_past(runner: Run | None = None, deliver: Deliver | None = None) -> int:
    """End a silence whose end passed without its timer firing, as `off` does."""
    claimed = claim(past_end)
    if claimed is None:
        return 0
    return release(*claimed, runner or run, deliver or post_kept, "silence ended at its end time")


def status() -> int:
    record = read_state()
    count = len(read_kept())
    plural = "" if count == 1 else "es"
    if record is None:
        print("silence off" + (f"; {count} kept push{plural} not yet delivered: run off" if count else ""))
    else:
        print(f"silence on from {shown(datetime.fromisoformat(record['since']))} {ends(record)}; {count} push{plural} kept")
    return 0


def run(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False, stdout=subprocess.DEVNULL).returncode


def passthrough(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def main(argv: Sequence[str] | None = None, runner: Run = run, system: str | None = None,
         remote: Run = passthrough) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("on", help="keep pushes from now")
    end = start.add_mutually_exclusive_group()
    _ = end.add_argument("--until", help='a time in the user\'s zone: "YYYY-MM-DD HH:MM"')
    _ = end.add_argument("--for", dest="span", help="a span: 1h, 45min, 1h30min")
    stop = commands.add_parser("off", help="end silence and post the kept pushes")
    _ = stop.add_argument("--timer", default="", help="end only the silence that names this timer")
    _ = commands.add_parser("status", help="say whether silence is on")
    _ = commands.add_parser("keep", help="pushover.py: keep the push JSON on stdin when silence is on")
    dropping = commands.add_parser("drop", help="send.py retire: remove the kept pushes with this source and key")
    _ = dropping.add_argument("--source", required=True)
    _ = dropping.add_argument("--key", required=True)
    options = parser.parse_args(arguments)
    if on_mac(system):
        return remote([*SSH, f"{REMOTE} {shlex.join(arguments)}"])
    command = cast(str, options.command)
    if command not in ("off", "keep", "drop"):
        _ = end_if_past(runner)
    if command == "on":
        until_text, span = cast(str | None, options.until), cast(str | None, options.span)
        until: datetime | None = None
        if until_text:
            try:
                until = datetime.strptime(until_text, "%Y-%m-%d %H:%M").replace(tzinfo=user_zone())
            except ValueError:
                parser.error('--until takes "YYYY-MM-DD HH:MM"')
        elif span:
            seconds = span_seconds(span)
            if not seconds:
                parser.error("--for takes a span such as 1h, 45min or 1h30min")
            until = datetime.now(user_zone()) + timedelta(seconds=seconds)
        if until and until <= datetime.now(user_zone()):
            parser.error(f"{until_text} has already passed")
        return on(until, runner)
    if command == "off":
        return off(runner, timer=cast(str, options.timer))
    if command == "keep":
        push = parse_push(sys.stdin.read())
        if push is None:
            parser.error("keep reads one push as JSON on stdin")
        kept = keep_here(push)
        if isinstance(kept, Kept):
            print(kept.until)
            return 0
        return 1
    if command == "drop":
        print(drop_here(cast(str, options.source), cast(str, options.key)))
        return 0
    return status()


if __name__ == "__main__":
    sys.exit(main())
