#!/usr/bin/env python3
"""Keep Mac claim admission locked while the floor sweep removes idle output."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from importlib import import_module
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
from typing import Protocol, cast


class ClaimState(Protocol):
    lock: Path
    claims: Path


class HeldMacClaim(Protocol):
    record: Mapping[str, object]
    scope: object


class ClaimReader(Protocol):
    """The existing claim decoder and admission lock used by the lazy loader."""

    def state_paths(self) -> ClaimState: ...
    def locked(self, path: Path) -> AbstractContextManager[None]: ...
    def read_claims(self, paths: ClaimState) -> Sequence[HeldMacClaim]: ...


MIRROR_MARK = ".mac-test-mirror"


@dataclass(frozen=True)
class LockedClaims:
    """Claims remain valid while every admission lock process remains alive."""

    repositories: frozenset[str]
    processes: tuple[subprocess.Popen[str], ...] = ()

    def holds(self, target: str, mirror: Path) -> bool:
        if "*" in self.repositories or any(process.poll() is not None for process in self.processes):
            return True
        try:
            path = Path(target).resolve(strict=True)
            return any(path.is_relative_to((mirror / name).resolve(strict=True)) for name in self.repositories)
        except (OSError, RuntimeError):
            return True


@contextmanager
def locked_claims(timeout: float) -> Generator[LockedClaims]:
    """Lock local and natedev claim admission; unreadable ownership keeps every target."""
    processes: list[subprocess.Popen[str]] = []
    names: set[str] = set()
    remote_source = Path(__file__).read_text().replace(
        "from __future__ import annotations",
        "from __future__ import annotations\n"
        + "__file__ = __import__('os').path.expanduser('~/.claude/scripts/mac_test/floor_claims.py')",
        1,
    )
    commands = (
        [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "--serve"],
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}",
         "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "natedev",
         "~/.claude/scripts/lib/py -c " + shlex.quote(remote_source)],
    )
    try:
        try:
            for command in commands:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True)
                processes.append(process)
                if process.stdout is None or not select.select([process.stdout], [], [], timeout)[0]:
                    raise ValueError("claim reply unavailable")
                value = cast(object, json.loads(cast(str, process.stdout.readline())))
                if not isinstance(value, list):
                    raise ValueError("claim reply invalid")
                for name in cast(list[object], value):
                    if not isinstance(name, str) or (name != "*" and (Path(name).name != name or name in (".", ".."))):
                        raise ValueError("claim scope invalid")
                    names.add(name)
        except (OSError, ValueError):
            names = {"*"}
        yield LockedClaims(frozenset(names), tuple(processes))
    finally:
        for process in processes:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            try:
                _ = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                _ = process.wait()
            if process.stdout is not None:
                process.stdout.close()


def serve() -> int:
    """Use the claim writer's lock and decoder without deleting stale records."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    mac_test = cast(ClaimReader, cast(object, import_module("mac_test")))

    paths = mac_test.state_paths()
    with mac_test.locked(paths.lock):
        if paths.claims.exists():
            with os.scandir(paths.claims) as entries:
                _ = list(entries)
        names: set[str] = set()
        for claim in mac_test.read_claims(paths):
            record = claim.record
            # A kept remote job can outlive its owner. Unreadable local liveness also keeps it.
            live = "remote_host" in record
            if not live:
                try:
                    os.kill(int(cast(int, record["pid"])), 0)
                    live = True
                except ProcessLookupError:
                    pass
                except OSError:
                    live = True
            if live:
                name: object = getattr(claim.scope, "name", "*")
                names.add(name if isinstance(name, str) else "*")
        print(json.dumps(sorted(names)), flush=True)
        _ = sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
