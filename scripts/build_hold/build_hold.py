#!/usr/bin/env python3
"""One holder file per session; decide when builds are quiet and may resume."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time as ClockTime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BUILD_COMMANDS = frozenset({"cargo", "rustc", "cargo-nextest"})
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class KnownRelease:
    at: datetime


@dataclass(frozen=True)
class UnknownRelease:
    pass


ReleaseEta = KnownRelease | UnknownRelease


@dataclass(frozen=True)
class NoReleaseEta:
    pass


@dataclass(frozen=True)
class ClockReleaseEta:
    clock: ClockTime
    zone: ZoneInfo


ReleaseRequest = NoReleaseEta | ClockReleaseEta


@dataclass(frozen=True)
class Holder:
    name: str
    since: datetime
    purpose: str
    release: ReleaseEta


@dataclass(frozen=True)
class NoHolders:
    pass


@dataclass(frozen=True)
class ActiveHolders:
    holders: tuple[Holder, ...]

    def __post_init__(self) -> None:
        if not self.holders:
            raise ValueError("active holders cannot be empty")
        if tuple(sorted(self.holders, key=lambda holder: holder.since)) != self.holders:
            raise ValueError("active holders must be ordered by since")


HoldState = NoHolders | ActiveHolders


@dataclass(frozen=True)
class Process:
    pid: int
    user: str
    command: str
    cpu_percent: float


@dataclass(frozen=True)
class Quiet:
    pass


@dataclass(frozen=True)
class Busy:
    reasons: tuple[str, ...]


def holder_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))


def holder_path(directory: Path, name: str) -> Path:
    safe = SAFE.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("holder name must contain a file-name character")
    return directory / safe


def aware_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("instant needs an offset")
    return instant


def read_holder(path: Path) -> Holder:
    """Even an unreadable or malformed regular file remains an active hold."""
    fallback = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        return Holder(path.name, fallback, "unreadable holder file", UnknownRelease())
    try:
        raw = cast(object, json.loads(content))
        if isinstance(raw, dict):
            fields = cast(dict[str, object], raw)
            name = fields.get("holder")
            since = fields.get("since")
            purpose = fields.get("for")
            release = fields.get("release_eta")
            if isinstance(name, str) and isinstance(since, str) and isinstance(purpose, str) and isinstance(release, str):
                eta: ReleaseEta = UnknownRelease() if release == "unknown" else KnownRelease(aware_instant(release))
                return Holder(name, aware_instant(since), purpose, eta)
    except (ValueError, TypeError):
        pass
    match = INSTANT.search(content)
    if match is None:
        return Holder(path.name, fallback, content, UnknownRelease())
    try:
        since = aware_instant(match.group())
    except ValueError:
        since = fallback
    return Holder(path.name, since, content[match.end():].lstrip(" ,;:-").strip(), UnknownRelease())


def read_holders(directory: Path) -> HoldState:
    try:
        paths = [path for path in directory.iterdir() if path.is_file()]
    except FileNotFoundError:
        return NoHolders()
    found: list[Holder] = []
    for path in paths:
        try:
            found.append(read_holder(path))
        except FileNotFoundError:
            continue
    holders = tuple(sorted(found, key=lambda holder: (holder.since, holder.name)))
    return ActiveHolders(holders) if holders else NoHolders()


def release_eta_text(release: ReleaseEta) -> str:
    if isinstance(release, UnknownRelease):
        return "unknown"
    return release.at.astimezone().strftime("%H:%M %Z")


def holder_text(holder: Holder) -> str:
    return f"{holder.name} (for {holder.purpose}, release eta {release_eta_text(holder.release)})"


def release_request(release_time: str | None, zone_name: str | None) -> ReleaseRequest:
    if release_time is None:
        if zone_name is not None:
            raise ValueError("--zone goes with --release-eta")
        return NoReleaseEta()
    if zone_name is None:
        raise ValueError("--zone goes with --release-eta")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", release_time):
        raise ValueError("release ETA must be HH:MM")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown IANA zone: {zone_name}") from None
    hour, minute = map(int, release_time.split(":"))
    return ClockReleaseEta(ClockTime(hour, minute), zone)


def write_hold(directory: Path, name: str, purpose: str, request: ReleaseRequest, now: datetime) -> str:
    if not purpose.strip() or "\n" in purpose:
        raise ValueError("hold purpose must be one line of text")
    if isinstance(request, NoReleaseEta):
        release_eta = "unknown"
    else:
        release = datetime.combine(now.astimezone(request.zone).date(), request.clock, request.zone)
        if release <= now:
            raise ValueError("release ETA has already passed today")
        release_eta = release.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    record = {"holder": name, "since": now.isoformat(), "for": purpose.strip(), "release_eta": release_eta}
    _ = holder_path(directory, name).write_text(json.dumps(record, ensure_ascii=False) + "\n")
    return f"/build_hold from {name}: stop any cargo or verify.sh you are running and start none until I release. No release after 2 h: ask me. For: {purpose.strip()}"


def release_hold(directory: Path, name: str) -> str:
    path = holder_path(directory, name)
    try:
        path.unlink()
    except FileNotFoundError:
        raise ValueError(f"{name} held nothing") from None
    state = read_holders(directory)
    if isinstance(state, NoHolders):
        return "released, builds may resume."
    return "released; still held by " + "; ".join(holder_text(holder) for holder in state.holders)


def quiet_verdict(load_1m: float, cores: int, processes: Sequence[Process], user: str) -> Quiet | Busy:
    own = Counter(process.command for process in processes if process.user == user and process.command in BUILD_COMMANDS)
    reasons: list[str] = []
    if own:
        reasons.append("your builds still running: " + ", ".join(f"{count} {command}" for command, count in sorted(own.items(), key=lambda item: (-item[1], item[0]))))
    if load_1m >= cores / 4:
        heaviest = sorted(processes, key=lambda process: process.cpu_percent, reverse=True)[:5]
        running = ", ".join(f"{process.user} {process.command} {process.cpu_percent:g}%" for process in heaviest)
        reasons.append(f"1-minute load {load_1m:g} is at or above {cores / 4:g}; heaviest: {running or 'none visible'}")
    return Busy(tuple(reasons)) if reasons else Quiet()


def read_processes() -> list[Process]:
    output = subprocess.check_output(["ps", "-eo", "pid=,user:32=,pcpu=,comm="], text=True)
    processes: list[Process] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4:
            pid, user, cpu, command = parts
            try:
                processes.append(Process(int(pid), user, Path(command).name, float(cpu)))
            except ValueError:
                continue
    return processes


def wait_for_quiet(
    max_wait: float = 600,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    cores: Callable[[], int | None] = os.cpu_count,
    processes: Callable[[], Sequence[Process]] = read_processes,
    user: str | None = None,
) -> Quiet | Busy:
    if max_wait < 0:
        raise ValueError("max wait must be non-negative")
    current_user = user or os.environ.get("USER", "")
    deadline = clock() + max_wait
    while True:
        result = quiet_verdict(load(), cores() or 1, processes(), current_user)
        if isinstance(result, Quiet) or clock() >= deadline:
            return result
        sleep(min(10, max(0, deadline - clock())))


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    hold = commands.add_parser("hold")
    _ = hold.add_argument("--holder", required=True)
    _ = hold.add_argument("--for", dest="purpose", required=True)
    _ = hold.add_argument("--release-eta")
    _ = hold.add_argument("--zone")
    quiet = commands.add_parser("quiet")
    _ = quiet.add_argument("--max-wait", type=float, default=600)
    release = commands.add_parser("release")
    _ = release.add_argument("--holder", required=True)
    _ = commands.add_parser("status")
    options = parser.parse_args(arguments)
    try:
        action = cast(str, options.action)
        if action == "hold":
            request = release_request(cast(str | None, options.release_eta), cast(str | None, options.zone))
            print(write_hold(holder_directory(), cast(str, options.holder), cast(str, options.purpose), request, datetime.now().astimezone()))
        elif action == "quiet":
            verdict = wait_for_quiet(cast(float, options.max_wait))
            if isinstance(verdict, Busy):
                print("; ".join(verdict.reasons))
                return 1
            print("builds are quiet; load is below the limit")
        elif action == "release":
            print(release_hold(holder_directory(), cast(str, options.holder)))
        else:
            state = read_holders(holder_directory())
            if isinstance(state, NoHolders):
                print("no build hold")
            else:
                for holder in state.holders:
                    print(f"{holder.name} since {holder.since.astimezone():%H:%M %Z}, for {holder.purpose} - release eta: {release_eta_text(holder.release)}")
    except (ValueError, OSError) as error:
        print(f"build_hold: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
