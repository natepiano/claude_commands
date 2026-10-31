#!/usr/bin/env python3
"""Refuse new Claude work while an account is held by shutdown state."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
from contextvars import ContextVar
from uuid import uuid4
from typing import Protocol, cast
from importlib import import_module


if __name__ == "launch_permission":
    _ = sys.modules.setdefault("scripts.shutdown.launch_permission", sys.modules[__name__])
elif __name__ == "scripts.shutdown.launch_permission":
    _ = sys.modules.setdefault("launch_permission", sys.modules[__name__])


@dataclass(frozen=True)
class NewWorkLaunchPurpose:
    """A launch unrelated to restoring a recorded shutdown session."""


@dataclass(frozen=True)
class RecordedSessionRestoreLaunchPurpose:
    """Restore any session held in a shutdown record.

    Stopped, already gone, process identity lost, restarting, restart failed
    and manual restart permit launch even under an account hold. Other
    progress blocks it. Without a record, the account hold alone decides.
    This purpose by itself does not authorize a restore."""

    session_id: str


LaunchPurpose = NewWorkLaunchPurpose | RecordedSessionRestoreLaunchPurpose


@dataclass(frozen=True)
class ReleasedHold:
    """A finished account hold the launch check removed."""

    label: str
    then: str
    requested_at: str
    # The requesting session's id; None for a terminal or a recovery snapshot.
    session_id: str | None


@dataclass(frozen=True)
class AllowedByShutdownState:
    """The current shutdown state permits this launch."""

    released: ReleasedHold | None = None


@dataclass(frozen=True)
class BlockedByShutdown:
    """A readable shutdown record holds this launch."""

    reason: str


@dataclass(frozen=True)
class ShutdownStateUnreadable:
    """Shutdown state could not safely decide whether to permit a launch."""

    detail: str


LaunchPermission = (
    AllowedByShutdownState | BlockedByShutdown | ShutdownStateUnreadable
)

_TIMEOUT_SECONDS = 30
# launch-blocked prints this before the JSON of a finished hold it removed.
RELEASED_HOLD_PREFIX = "released account hold: "
_UNREADABLE_PREFIX = "shutdown state unreadable: "


def shutdown_state_root() -> Path:
    """The machine-local root shared by shutdown records and launch locking."""
    return Path(
        os.environ.get(
            "SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")
        )
    )


@contextmanager
def _held_launch_barrier() -> Generator[None, None, None]:
    root = shutdown_state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "launch.lock").open("a+", encoding="utf-8") as lock_file:
        os.set_inheritable(lock_file.fileno(), False)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def launch_barrier() -> AbstractContextManager[None]:
    """Hold the barrier that serializes launch decisions with shutdown creation."""
    return _held_launch_barrier()



class LaunchAdmissionRefused(OSError):
    """A shutdown hold or unreadable state denies launching, including fallback."""


class LaunchRevoked(OSError):
    """A shutdown begun after admission withdrew permission to finish this launch."""


@contextmanager
def block_launches_during_shutdown() -> Generator[None, None, None]:
    """Fence all accounts for a bounded shutdown; a dead caller releases the fence."""
    root = shutdown_state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".record.shutdown-admission.tmp").open("a+") as fence:
        with launch_barrier():
            try:
                fcntl.flock(fence.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LaunchAdmissionRefused("machine-wide shutdown already in progress") from error
        try:
            yield
        finally:
            fcntl.flock(fence.fileno(), fcntl.LOCK_UN)


def _require_open_launch_admission() -> None:
    # The caller holds launch_barrier, so shutdown cannot install the fence
    # between this check and publishing the new revocable claim.
    with (shutdown_state_root() / ".record.shutdown-admission.tmp").open("a+") as fence:
        try:
            fcntl.flock(fence.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LaunchAdmissionRefused("machine-wide shutdown in progress") from error
        fcntl.flock(fence.fileno(), fcntl.LOCK_UN)


class LaunchPaneHost(Protocol):
    def close_agent(self, session: str, pane: str) -> None: ...


@dataclass(frozen=True)
class PendingSessionLaunch:
    """An admitted launch that shutdown can revoke before it finishes."""

    session_id: str
    login: str
    name: str
    pid: int
    herdr_session: str = ""
    pane: str = ""


@dataclass(frozen=True)
class NoLaunchClaim:
    """This caller is outside a shutdown-aware launch."""


_CURRENT_LAUNCH: ContextVar[Path | NoLaunchClaim] = ContextVar("shutdown_launch", default=NoLaunchClaim())


def _launch_fields(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"unreadable launch claim: {path}")
    return cast(dict[str, object], value)


def _pending_launch(fields: dict[str, object]) -> PendingSessionLaunch:
    texts = [fields.get(key, "") for key in ("session_id", "login", "name", "herdr_session", "pane")]
    pid = fields.get("pid")
    if not all(isinstance(value, str) for value in texts) or not isinstance(pid, int):
        raise ValueError("unreadable pending session launch")
    identity, login, name, session, pane = cast(list[str], texts)
    return PendingSessionLaunch(identity, login, name, pid, session, pane)


def pending_launches() -> tuple[PendingSessionLaunch, ...]:
    """Read launch claims while the caller holds launch_barrier()."""
    return tuple(_pending_launch(_launch_fields(path))
                 for path in shutdown_state_root().glob(".record.launch-*.tmp"))


def revoke_pending_launches(login: str, only: tuple[str, ...] = ()) -> tuple[PendingSessionLaunch, ...]:
    """Revoke this account's launches; return exact panes for shutdown to close.

    Call before shutdown's final inventory. A launch without a pane yet will
    refuse when it publishes its pane, and close that pane itself.
    """
    revoked: list[PendingSessionLaunch] = []
    with launch_barrier():
        for path in shutdown_state_root().glob(".record.launch-*.tmp"):
            fields = _launch_fields(path)
            claim = _pending_launch(fields)
            if claim.login.casefold() == login.casefold() and (not only or claim.session_id in only):
                fields["revoked"] = True
                _ = path.write_text(json.dumps(fields), encoding="utf-8")
                revoked.append(claim)
    return tuple(revoked)


@contextmanager
def admitted_launch(login: str, session_id: str, name: str,
                    admit: Callable[[], None]) -> Generator[None, None, None]:
    """Serialize only admission and its revocable claim, never agent startup."""
    path = shutdown_state_root() / f".record.launch-{uuid4().hex}.tmp"
    with launch_barrier():
        _require_open_launch_admission()
        parent = os.environ.get("SHUTDOWN_PARENT_LAUNCH_CLAIM", "")
        if parent and _launch_fields(Path(parent)).get("revoked") is True:
            raise LaunchRevoked("launch revoked by a newer shutdown")
        admit()
        _ = path.write_text(json.dumps(dict(login=login, session_id=session_id,
            name=name, pid=os.getpid(), revoked=False)), encoding="utf-8")
    token = _CURRENT_LAUNCH.set(path)
    try:
        yield
    finally:
        _CURRENT_LAUNCH.reset(token)
        with launch_barrier():
            fields = _launch_fields(path)
            path.unlink()
        if fields.get("revoked") is True:
            claim = _pending_launch(fields)
            if claim.pane:
                host = cast(LaunchPaneHost, cast(object, import_module("herdr")))
                host.close_agent(claim.herdr_session, claim.pane)
            raise LaunchRevoked(f"{name}: launch revoked by a newer shutdown")



def inherited_launch_claim() -> dict[str, str]:
    """Carry a unit subprocess's admission across its account/preflight work."""
    path = _CURRENT_LAUNCH.get()
    return {} if isinstance(path, NoLaunchClaim) else {"SHUTDOWN_PARENT_LAUNCH_CLAIM": str(path)}

def claim_launch_pane(session: str, pane: str) -> None:
    """Expose only the new pane to a down begun during startup."""
    path = _CURRENT_LAUNCH.get()
    if isinstance(path, NoLaunchClaim):
        return
    with launch_barrier():
        fields = _launch_fields(path)
        fields.update(herdr_session=session, pane=pane)
        _ = path.write_text(json.dumps(fields), encoding="utf-8")
        revoked = fields.get("revoked") is True
    if revoked:
        raise LaunchRevoked("launch revoked by a newer shutdown")

def _first_line(text: str) -> str:
    lines = text.splitlines()
    return lines[0] if lines else ""


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _unreadable_detail(status: int, stderr: str) -> str:
    detail = _first_line(stderr)
    if detail.startswith(_UNREADABLE_PREFIX):
        detail = detail.removeprefix(_UNREADABLE_PREFIX)
    return detail or f"launch-blocked exited {status}"


def _released_hold(text: str) -> ReleasedHold | None:
    try:
        value = cast(object, json.loads(text))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    fields = cast(dict[str, object], value)
    label, then, requested_at, origin = (fields.get(key) for key in ("label", "then", "requested_at", "requested_by"))
    if not (isinstance(label, str) and isinstance(then, str) and isinstance(requested_at, str) and isinstance(origin, dict)):
        return None
    session_id = cast(dict[str, object], origin).get("session_id")
    return ReleasedHold(label, then, requested_at, session_id if isinstance(session_id, str) else None)


def launch_permission(
    environment: Mapping[str, str], purpose: LaunchPurpose
) -> LaunchPermission:
    """Ask shutdown.py whether `purpose` may launch in `environment`."""
    command = [
        sys.executable,
        str(Path(__file__).with_name("shutdown.py")),
        "launch-blocked",
    ]
    if isinstance(purpose, RecordedSessionRestoreLaunchPurpose):
        command.extend(("--restart-of", purpose.session_id))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
            env=environment,
        )
    except OSError as error:
        return ShutdownStateUnreadable(str(error))
    except subprocess.TimeoutExpired:
        return ShutdownStateUnreadable(
            f"launch-blocked timed out after {_TIMEOUT_SECONDS} s"
        )
    if completed.returncode == 1:
        if not completed.stdout and not completed.stderr:
            return AllowedByShutdownState()
        released = _first_line(completed.stdout)
        if released.startswith(RELEASED_HOLD_PREFIX) and not completed.stderr:
            hold = _released_hold(released.removeprefix(RELEASED_HOLD_PREFIX))
            return (AllowedByShutdownState(hold) if hold is not None
                    else ShutdownStateUnreadable(f"launch-blocked printed an unreadable hold: {released}"))
        detail = _last_nonempty_line(completed.stderr) or _first_line(
            completed.stdout
        )
        return ShutdownStateUnreadable(f"launch-blocked exited 1: {detail}")
    if completed.returncode == 0:
        return BlockedByShutdown(_first_line(completed.stdout))
    return ShutdownStateUnreadable(
        _unreadable_detail(completed.returncode, completed.stderr)
    )


class CommandLine(argparse.Namespace):
    command_name: str = ""
    name: str = ""
    launched_command: list[str] = []


def _exec_new_work(name: str, command: list[str]) -> int:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("exec-new-work requires a command after --")
    try:
        barrier = launch_barrier()
        _ = barrier.__enter__()
    except OSError as error:
        print(
            f"{name}: skipped: shutdown state unreadable: launch barrier: {error}"
        )
        return 0
    try:
        permission = launch_permission(os.environ, NewWorkLaunchPurpose())
        if isinstance(permission, BlockedByShutdown):
            print(f"{name}: skipped: {permission.reason}")
            return 0
        if isinstance(permission, ShutdownStateUnreadable):
            print(
                f"{name}: skipped: shutdown state unreadable: {permission.detail}"
            )
            return 0
        os.execv(command[0], command)
    finally:
        _ = barrier.__exit__(None, None, None)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command_name", required=True)
    execute = commands.add_parser("exec-new-work")
    _ = execute.add_argument("--name", required=True)
    _ = execute.add_argument("launched_command", nargs=argparse.REMAINDER)
    options = cast(CommandLine, parser.parse_args(arguments))
    try:
        return _exec_new_work(options.name, options.launched_command)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
