#!/usr/bin/env python3
"""Show or change the zone every time shown to the user is written in: the /zone command.

  zone.py                   the zone, its label and offset, and the time now; sends a queued Mac change
  zone.py set NAME          change it. NAME is an IANA zone (America/Curacao), the city of one
                            (Curacao, "New York"), or a label (AST) that only one zone uses
  zone.py drain [--pulled PATH]  send a change queued while the Mac was offline

`set` writes config/user_zone.conf, commits it on main and pushes it, pulls the Mac's ~/.claude,
and tells every session on both machines with broadcast.py --all. It runs on natedev, whose
~/.claude main the Mac pulls from. When the Mac cannot be reached, its pull and its sessions'
message wait in the queue (<state>/zone/mac.json); a bare `zone.py` and each promotion's Mac pull
(merge_checkpoint.promote) send them. A name that is not a zone, or that names more than one,
is refused with exit 2 and the zones it could mean.

ZONE_SSH, ZONE_BROADCAST and ZONE_STATE replace ssh, broadcast.py and the state folder in tests.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import platform
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, available_timezones

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf_reads, main_publish, user_zone  # noqa: E402
from scripts.lib.main_publish import Refusal as Refusal
from scripts.lib.main_publish import checkout_of, first_line as _first_line, git, queue, read_queue
from scripts.lib.main_publish import ready as _ready

MAC = "mac"
SENDER = "zone"
SUBJECT = "zone("
SHOWN_CANDIDATES = 3


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
    return user_zone.stored_zone(str(conf)) or user_zone.DEFAULT_ZONE


def write_zone(conf: Path, zone: str) -> None:
    """Set the zone= line of `conf`, keeping its comments; add the line when it has none."""
    lines = conf_reads.read_text(conf).splitlines() if conf.exists() else []
    written = [f"zone={zone}" if line.startswith("zone=") else line for line in lines]
    if written == lines and not any(line.startswith("zone=") for line in lines):
        written.append(f"zone={zone}")
    write_text_atomic(conf, "\n".join(written) + "\n")


def _commit(checkout: Path, conf: Path, zone: str, before_text: str | None) -> str:
    label = datetime.now(ZoneInfo(zone)).tzname() or zone
    before = main_publish.MissingFile() if before_text is None else main_publish.ExistingFile(before_text)
    return main_publish.commit(checkout, conf, f"{SUBJECT}{zone}): set with /zone ({label})", before,
                               subject_prefix=SUBJECT, unchanged="the zone is unchanged")


def _ssh(remote: str) -> subprocess.CompletedProcess[str] | str:
    return main_publish.ssh(remote, ssh_program=os.environ.get("ZONE_SSH") or "ssh", host=MAC)


def pull_mac(checkout: Path) -> main_publish.MacPulled | main_publish.MacPullFailed:
    return main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh")


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


def drain(checkout: Path | None, *, pulled: PurePosixPath | None) -> list[str]:
    """Pull each pending checkout, then send its queued zone notice."""
    change = read_queue()
    if isinstance(change, main_publish.NoPendingMacPull):
        return []
    for pull in change.pulls:
        if pull.mac != pulled:
            source = pull.source if isinstance(pull.source, Path) else checkout
            if source is None:
                return [f"mac: the {change.label} change waits since {change.since}; run /zone on natedev"]
            outcome = main_publish.pull_mac(source, mac=pull.mac, ssh_program=os.environ.get("ZONE_SSH") or "ssh")
            if isinstance(outcome, main_publish.MacPullFailed):
                return [f"mac: the {change.label} change still waits since {change.since}: {outcome.reason}"]
        main_publish.clear_pull_only(pull.mac)
    delivered, summary = broadcast_on_mac(change.text) if change.text else (True, "no message")
    if not delivered:
        return [f"mac: pulled the {change.label} change; its message still waits: {summary}"]
    main_publish.clear_pull_only(notice_delivered=True)
    return [f"mac: took the {change.label} change queued at {change.since}; message {summary}"]


def set_zone(name: str) -> int:
    if platform.system() == "Darwin":
        raise Refusal("run /zone on natedev: the Mac pulls ~/.claude main from there")
    zone = resolve(name)
    conf = Path(user_zone.CONF)
    checkout = checkout_of(conf)
    _ready(checkout)
    before = stored(conf)
    print(f"zone: {describe(zone)}")
    before_text = conf_reads.read_text(conf) if conf.exists() else None
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
    pushed, push_line = main_publish.push(checkout)
    print(f"main: {commit}, {push_line}")
    text = notice(zone, before, commit)
    if not pushed:
        print("mac: not pulled because main is not on origin")
        print(f"broadcast: {broadcast(text, here=True)[1]}")
        return 1
    outcome = pull_mac(checkout)
    if isinstance(outcome, main_publish.MacPulled):
        print("mac: pulled")
        main_publish.clear_pull_only(notice_delivered=True)
        delivered, summary = broadcast(text, here=False)
    else:
        since = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M %Z")
        queue(main_publish.PendingMacPull(label=zone, text=text, since=since, reason=outcome.reason))
        print(f"mac: queued ({outcome.reason}); a bare /zone or the next promotion sends it")
        delivered, summary = broadcast(text, here=True)
    print(f"broadcast: {summary}")
    return 0 if delivered else 1


def show() -> int:
    print(f"zone: {describe(user_zone.user_zone_name())}")
    checkout = None if platform.system() == "Darwin" else checkout_of(Path(user_zone.CONF))
    for line in drain(checkout, pulled=None):
        print(line)
    return 0


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case []:
                return show()
            case ["set", name]:
                return set_zone(name)
            case ["drain"] | ["drain", "--pulled", _]:
                checkout = None if platform.system() == "Darwin" else checkout_of(Path(user_zone.CONF))
                pulled = PurePosixPath(arguments[2]) if len(arguments) == 3 else None
                for line in drain(checkout, pulled=pulled):
                    print(line)
                return 0
            case _:
                print("usage: zone.py [set NAME | drain [--pulled PATH]]", file=sys.stderr)
                return 2
    except (Refusal, ValueError) as error:
        print(f"zone: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
