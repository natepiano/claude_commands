#!/usr/bin/env python3
"""Keep Mac claim admission locked while the floor sweep removes idle output."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Mapping, Sequence
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
MIRROR_RECORD = ".copy-record.json"
MIRROR_SESSION = ".copy-session"
MIRROR_METADATA = (MIRROR_SESSION, MIRROR_MARK, MIRROR_RECORD)
MIRROR_REMOVAL_SUFFIX = ".copy-removing.json"


def read_mirror_record(path: Path) -> dict[str, object]:
    """Read mirror metadata or removal authority through the same decoder."""
    return cast(dict[str, object], json.loads(path.read_text()))


def mirror_local_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.home() / candidate
    return candidate.absolute()


def mirror_lock_path(tree: Path) -> Path:
    # A sibling lock survives replacement or removal of the mirror itself.
    return tree.parent / ("." + tree.name + ".copy-lease")


def removal_authority_path(tree: Path) -> Path:
    return tree.parent / ("." + tree.name + MIRROR_REMOVAL_SUFFIX)


def record_lock_path(tree: Path) -> Path:
    return tree.parent / ("." + tree.name + ".copy-record.lock")


@dataclass(frozen=True)
class MirrorIdentity:
    """The host and configured path identify one mirror lease."""

    host: str
    path: str


@dataclass(frozen=True)
class HeldMirror:
    identity: MirrorIdentity
    session: str


@dataclass(frozen=True)
class NoMirrorExemption:
    """Every active mirror lease protects its target."""


NO_MIRROR_EXEMPTION = NoMirrorExemption()


@dataclass(frozen=True)
class LockedClaims:
    """Claims remain valid while every admission lock process remains alive."""

    repositories: frozenset[str]
    processes: tuple[subprocess.Popen[str], ...] = ()
    mirrors: tuple[HeldMirror, ...] = ()

    def holds(self, target: str, mirror: Path, *,
              exemption: MirrorIdentity | NoMirrorExemption = NO_MIRROR_EXEMPTION) -> bool:
        if "*" in self.repositories or any(process.poll() is not None for process in self.processes):
            return True
        try:
            path = Path(target).resolve(strict=True)
            for held in self.mirrors:
                if held.identity == exemption:
                    continue
                if isinstance(exemption, MirrorIdentity) and held.identity.host != exemption.host:
                    continue
                remote = mirror_local_path(held.identity.path)
                if path.is_relative_to(remote.resolve()):
                    return True
            for parent in (path, *path.parents):
                record = parent / MIRROR_RECORD
                if record.exists():
                    try:
                        fields = read_mirror_record(record)
                        return fields.get("repo") in self.repositories
                    except (OSError, ValueError):
                        return True
            for name in self.repositories:
                try:
                    claimed = (mirror / name).resolve(strict=True)
                except FileNotFoundError:
                    continue
                if path.is_relative_to(claimed):
                    return True
            return False
        except (OSError, RuntimeError):
            return True


@contextmanager
def locked_claims(timeout: float) -> Generator[LockedClaims]:
    """Lock local and natedev claim admission; unreadable ownership keeps every target."""
    processes: list[subprocess.Popen[str]] = []
    names: set[str] = set()
    mirrors: list[HeldMirror] = []
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
                if not isinstance(value, dict):
                    raise ValueError("claim reply invalid")
                reply = cast(dict[str, object], value)
                repositories = reply.get("repositories")
                leases = reply.get("mirrors")
                if not isinstance(repositories, list) or not isinstance(leases, list):
                    raise ValueError("claim reply invalid")
                for lease in cast(list[object], leases):
                    if not isinstance(lease, dict):
                        raise ValueError("mirror lease invalid")
                    fields = cast(dict[str, object], lease)
                    host, path, session = fields.get("host"), fields.get("path"), fields.get("session")
                    if not isinstance(host, str) or not isinstance(path, str) or not isinstance(session, str):
                        raise ValueError("mirror lease invalid")
                    mirrors.append(HeldMirror(MirrorIdentity(host, path), session))
                for name in cast(list[object], repositories):
                    if not isinstance(name, str) or (name != "*" and (Path(name).name != name or name in (".", ".."))):
                        raise ValueError("claim scope invalid")
                    names.add(name)
        except (OSError, ValueError):
            names = {"*"}
        yield LockedClaims(frozenset(names), tuple(processes), tuple(mirrors))
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
    mac_test = cast(ClaimReader, cast(object, import_module('scripts.mac_test.mac_test')))

    paths = mac_test.state_paths()
    with mac_test.locked(paths.lock):
        if paths.claims.exists():
            with os.scandir(paths.claims) as entries:
                _ = list(entries)
        names: set[str] = set()
        held_remote_mirrors = cast(Callable[[], tuple[HeldMirror, ...]],
            import_module("scripts.copies.copies").held_remote_mirrors)
        mirrors = [{"host": held.identity.host, "path": held.identity.path, "session": held.session}
                   for held in held_remote_mirrors()]
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
        print(json.dumps({"repositories": sorted(names), "mirrors": mirrors}), flush=True)
        _ = sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
