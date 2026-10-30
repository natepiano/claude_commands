#!/usr/bin/env python3
"""Show or change the zone every time shown to the user is written in: the /zone command.

  zone.py                   the zone, its label and offset, and the time now; sends a queued Mac change
  zone.py set NAME          change it. NAME is an IANA zone (America/Curacao), the city of one
                            (Curacao, "New York"), or a label (AST) that only one zone uses
  zone.py drain [--pulled]  send a change queued while the Mac was offline

`set` writes config/user_zone.conf, commits it on main and pushes it, pulls the Mac's ~/.claude,
and tells every session on both machines with broadcast.py --all. It runs on natedev, whose
~/.claude main the Mac pulls from. When the Mac cannot be reached, its pull and its sessions'
message wait in the queue (<state>/zone/mac.json); a bare `zone.py` and each promotion's Mac pull
(merge_checkpoint.promote) send them. A name that is not a zone, or that names more than one,
is refused with exit 2 and the zones it could mean.

ZONE_SSH, ZONE_BROADCAST and ZONE_STATE replace ssh, broadcast.py and the state folder in tests.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast
from zoneinfo import ZoneInfo, available_timezones

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import user_zone  # noqa: E402

MAC = "mac"
SENDER = "zone"
SUBJECT = "user zone: "
SHOWN_CANDIDATES = 3


class Refusal(Exception):
    """A name or a checkout state that /zone cannot act on; nothing was changed."""


class QueuedChange(TypedDict):
    zone: str
    text: str
    since: str
    reason: str


def _words(text: str) -> list[str]:
    """`text` as case-folded words with no accents: "Curaçao", "curacao" and "CURACAO" match."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return re.sub(r"[_\-\s]+", " ", plain).casefold().split()


def _offset(moment: datetime) -> str:
    """The moment's UTC offset as UTC-04:00."""
    text = moment.strftime("%z")
    return f"UTC{text[:3]}:{text[3:]}"


def _candidates(zones: list[str], now: datetime) -> str:
    """Up to SHOWN_CANDIDATES examples per UTC offset, so a reader sees which places differ."""
    groups: dict[str, list[str]] = {}
    for zone in zones:
        groups.setdefault(_offset(now.astimezone(ZoneInfo(zone))), []).append(zone)
    return "; ".join(f"{len(names)} at {offset} (such as {', '.join(names[:SHOWN_CANDIDATES])})"
                     for offset, names in sorted(groups.items()))


def resolve(name: str, now: datetime | None = None) -> str:
    """The one IANA zone `name` means. Raises Refusal when no zone or more than one matches."""
    wanted = name.strip()
    zones = available_timezones()
    exact = sorted(zone for zone in zones if zone.casefold() == wanted.casefold())
    if exact:
        return exact[0]
    at = now or datetime.now(timezone.utc)
    regional = sorted(zone for zone in zones if "/" in zone and not zone.startswith("Etc/"))
    by_city = [zone for zone in regional if _words(zone.rsplit("/", 1)[1]) == _words(wanted)]
    if len(by_city) == 1:
        return by_city[0]
    if by_city:
        raise Refusal(f"{wanted!r} is the city of {len(by_city)} zones: {_candidates(by_city, at)}; "
                      + "give the IANA zone")
    label = wanted.upper()
    by_label = [zone for zone in regional
                if any((moment.astimezone(ZoneInfo(zone)).tzname() or "") == label
                       for moment in (at, at + timedelta(days=182)))]
    if len(by_label) == 1:
        return by_label[0]
    if by_label:
        raise Refusal(f"{wanted!r} is the label of {len(by_label)} zones: {_candidates(by_label, at)}; "
                      + "name the place or the IANA zone")
    raise Refusal(f"no zone is named {wanted!r}; give the IANA zone of the place, such as America/Los_Angeles")


def describe(zone: str) -> str:
    """America/Curacao (AST, UTC-04:00); now 18:40 AST."""
    now = datetime.now(ZoneInfo(zone))
    label = now.tzname() or zone
    return f"{zone} ({label}, {_offset(now)}); now {now:%H:%M} {label}"


def stored(conf: Path) -> str:
    """The file's zone= value, or the default when the file or the line is missing."""
    try:
        lines = conf.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return user_zone.DEFAULT_ZONE
    return next((line.split("=", 1)[1].strip() for line in lines if line.startswith("zone=")),
                user_zone.DEFAULT_ZONE)


def write_zone(conf: Path, zone: str) -> None:
    """Set the zone= line of `conf`, keeping its comments; add the line when it has none."""
    lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
    written = [f"zone={zone}" if line.startswith("zone=") else line for line in lines]
    if written == lines and not any(line.startswith("zone=") for line in lines):
        written.append(f"zone={zone}")
    write_text_atomic(conf, "\n".join(written) + "\n")


def git(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True, text=True, check=False)


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout).strip()
    return text.splitlines()[0] if text else f"exit {result.returncode}"


def checkout_of(conf: Path) -> Path:
    found = git(conf.parent, "rev-parse", "--show-toplevel")
    if found.returncode:
        raise Refusal(f"{conf.parent} is not in a git checkout: {_first_line(found)}")
    return Path(found.stdout.strip())


def _ready(checkout: Path) -> None:
    """Refuse unless the checkout is on main and holds origin/main."""
    branch = git(checkout, "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise Refusal(f"{checkout} is on {branch or 'no branch'}, not main")
    fetched = git(checkout, "fetch", "-q", "origin", "main")
    if fetched.returncode:
        raise Refusal(f"git fetch origin main: {_first_line(fetched)}")
    merged = git(checkout, "merge", "-q", "--ff-only", "origin/main")
    if merged.returncode:
        raise Refusal(f"{checkout} main does not take origin/main as a fast-forward: {_first_line(merged)}")


def _commit(checkout: Path, conf: Path, zone: str, before_text: str | None) -> str:
    """Commit the new conf; on a failure put the old text back and refuse."""
    label = datetime.now(ZoneInfo(zone)).tzname() or zone
    committed = git(checkout, "commit", "-q", "--only", "-m", f"{SUBJECT}{zone} ({label}), set with /zone",
                    "--", str(conf))
    if committed.returncode:
        if before_text is None:
            conf.unlink(missing_ok=True)
        else:
            write_text_atomic(conf, before_text)
        raise Refusal(f"git commit: {_first_line(committed)}; the zone is unchanged")
    return git(checkout, "rev-parse", "--short", "HEAD").stdout.strip()


def _push(checkout: Path) -> tuple[bool, str]:
    """Push main when every commit origin lacks is a zone commit."""
    unpushed = git(checkout, "log", "--format=%s", "origin/main..main").stdout.splitlines()
    if not unpushed:
        return True, "origin has it"
    others = [subject for subject in unpushed if not subject.startswith(SUBJECT)]
    if others:
        return False, f"not pushed: main holds {len(others)} other commit(s) origin does not have"
    pushed = git(checkout, "push", "-q", "origin", "main")
    if pushed.returncode:
        return False, f"not pushed: {_first_line(pushed)}"
    return True, "pushed"


def _ssh(remote: str) -> subprocess.CompletedProcess[str] | str:
    """Run `remote` on the Mac; a string says why it could not run."""
    command = [os.environ.get("ZONE_SSH") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", MAC, remote]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return "timed out"
    except OSError as error:
        return str(error)


def pull_mac(checkout: Path) -> str | None:
    """Pull natedev's main into the Mac's ~/.claude, as a promotion does. None when it worked, else why not."""
    done = _ssh(f'cd "$HOME"/.claude && git pull -q --ff-only natedev:{shlex.quote(str(checkout))} main; '
                + "printf 'rc=%s\\n' \"$?\"")
    if isinstance(done, str):
        return done
    match = re.search(r"(?:^|\n)rc=(\d+)", done.stdout)
    if done.returncode or match is None or int(match.group(1)):
        return _first_line(done)
    return None


def _counted(output: str) -> tuple[bool, str]:
    """broadcast.py's recipient lines as counts, with each line that did not arrive."""
    lines = output.splitlines()
    sent = sum(1 for line in lines if re.search(r" - sent\b", line))
    missed = [line for line in lines if "NOT sent" in line]
    return not missed, f"{sent} sent" + "".join(f"\n  {line}" for line in missed)


def broadcast(text: str, *, here: bool) -> tuple[bool, str]:
    """Send `text` to every session, on this machine only when `here`."""
    program = os.environ.get("ZONE_BROADCAST") or str(Path(__file__).resolve().parents[1] / "production/broadcast.py")
    done = subprocess.run([sys.executable, program, *(["--here"] if here else []), "--from", SENDER, "--all", text],
                          capture_output=True, text=True, check=False)
    delivered, summary = _counted(done.stdout)
    if done.returncode and delivered:
        return False, f"{summary}; broadcast.py: {_first_line(done)}"
    return delivered, summary


def broadcast_on_mac(text: str) -> tuple[bool, str]:
    done = _ssh('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/broadcast.py" --here '
                + f"--from {SENDER} --all {shlex.quote(text)}")
    if isinstance(done, str):
        return False, f"not sent: {done}"
    delivered, summary = _counted(done.stdout)
    return delivered and done.returncode == 0, summary


def notice(zone: str, before: str, commit: str) -> str:
    now = datetime.now(ZoneInfo(zone))
    label = now.tzname() or zone
    return (f"From /zone ({now:%H:%M} {label}): the user's time zone is now {zone} ({label}, {_offset(now)}); "
            + f"it was {before}. config/user_zone.conf says so ({commit}). Write every time you give the user "
            + f"in {label}, with its label. Read the clock with TZ=$(python3 ~/.claude/scripts/lib/user_zone.py) "
            + "date '+%H:%M %Z', never with bare date, because each machine's clock keeps its own zone. "
            + "Scripts that read user_zone.py change by themselves. Change any note of yours that names the old zone.")


def queue_path() -> Path:
    state = os.environ.get("ZONE_STATE") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "zone")
    return Path(state) / "mac.json"


def read_queue() -> QueuedChange | None:
    try:
        value = cast(object, json.loads(queue_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    fields = cast(dict[str, object], value)
    if not all(isinstance(fields.get(key), str) for key in ("zone", "text", "since", "reason")):
        return None
    return cast(QueuedChange, cast(object, fields))


def queue(change: QueuedChange) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(change, indent=2) + "\n")


def drain(checkout: Path | None, *, pulled: bool) -> list[str]:
    """Send the queued Mac change: pull (unless `pulled`), then tell the Mac's sessions."""
    change = read_queue()
    if change is None:
        return []
    if not pulled:
        if checkout is None:
            return [f"mac: the {change['zone']} change waits since {change['since']}; run /zone on natedev"]
        reason = pull_mac(checkout)
        if reason is not None:
            return [f"mac: the {change['zone']} change still waits since {change['since']}: {reason}"]
    delivered, summary = broadcast_on_mac(change["text"])
    if not delivered:
        return [f"mac: pulled the {change['zone']} change; its message still waits: {summary}"]
    queue_path().unlink(missing_ok=True)
    return [f"mac: took the {change['zone']} change queued at {change['since']}; message {summary}"]


def set_zone(name: str) -> int:
    if platform.system() == "Darwin":
        raise Refusal("run /zone on natedev: the Mac pulls ~/.claude main from there")
    zone = resolve(name)
    conf = user_zone.CONF
    checkout = checkout_of(conf)
    _ready(checkout)
    before = stored(conf)
    print(f"zone: {describe(zone)}")
    before_text = conf.read_text(encoding="utf-8") if conf.exists() else None
    if before != zone:
        print(f"was: {before}")
        write_zone(conf, zone)
    changed = git(checkout, "diff", "HEAD", "--", str(conf))
    if changed.returncode:
        raise Refusal(f"git diff: {_first_line(changed)}")
    if changed.stdout:
        commit = _commit(checkout, conf, zone, before_text)
    else:
        print("was: the same; nothing to commit")
        commit = git(checkout, "rev-parse", "--short", "HEAD").stdout.strip()
    pushed, push_line = _push(checkout)
    print(f"main: {commit}, {push_line}")
    text = notice(zone, before, commit)
    reason = pull_mac(checkout)
    if reason is None:
        print("mac: pulled")
        queue_path().unlink(missing_ok=True)
        delivered, summary = broadcast(text, here=False)
    else:
        since = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M %Z")
        queue(QueuedChange(zone=zone, text=text, since=since, reason=reason))
        print(f"mac: queued ({reason}); a bare /zone or the next promotion sends it")
        delivered, summary = broadcast(text, here=True)
    print(f"broadcast: {summary}")
    return 0 if pushed and delivered else 1


def show() -> int:
    print(f"zone: {describe(user_zone.user_zone_name())}")
    checkout = None if platform.system() == "Darwin" else checkout_of(user_zone.CONF)
    for line in drain(checkout, pulled=False):
        print(line)
    return 0


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case []:
                return show()
            case ["set", name]:
                return set_zone(name)
            case ["drain", *pulled] if pulled in ([], ["--pulled"]):
                checkout = None if pulled or platform.system() == "Darwin" else checkout_of(user_zone.CONF)
                for line in drain(checkout, pulled=bool(pulled)):
                    print(line)
                return 0
            case _:
                print("usage: zone.py [set NAME | drain [--pulled]]", file=sys.stderr)
                return 2
    except (Refusal, ValueError) as error:
        print(f"zone: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
