#!/usr/bin/env python3
"""Quietly enable Remote Control for one proven restarted process."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
from scripts.lib import process_start as process_identity
for dependency in ("message", "production", "notify", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import detached
from launch_permission import shutdown_state_root
import session_pane
import sessions
from herdr import AgentBlocked
from record import SessionShutdown

FLAG_WAIT = 60.0
ATTEMPT_WAIT = 30.0
ATTEMPTS = 3
DEADLINE = 15 * 60.0
POLL_SECONDS = 0.5


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_SYSTEM_CLOCK = SystemClock()


@dataclass(frozen=True)
class SessionGone:
    """The requested process no longer owns this session."""


@dataclass(frozen=True)
class AlreadyOn:
    """The launch flag enabled Remote Control without pane input."""


@dataclass(frozen=True)
class TurnedOn:
    """Remote Control was confirmed after the fixer typed its command."""


@dataclass(frozen=True)
class Failed:
    reason: str


RemoteControlOutcome = SessionGone | AlreadyOn | TurnedOn | Failed


@dataclass(frozen=True)
class ConfirmedSession:
    record: sessions.SessionRecord
    idle: bool
    active: bool


@dataclass(frozen=True)
class RegistryUnavailable:
    """The process still matches, but its registry cannot authorize pane input."""


def remote_control_who(saved: SessionShutdown) -> str:
    session = saved["session"]
    host = session["host"]
    if host["kind"] == "unit":
        return host["unit"].removesuffix("-unit") + " unit"
    if session["kind"] == "showrunner":
        return session["production"] + " showrunner"
    return session["name"]


def launch_fixer(session_id: str, pid: int, proc_start: str, who: str, session: str) -> None:
    """Start the local fixer independently of the conductor or restart command."""
    if pid <= 0 or not proc_start:
        raise ValueError("Remote Control fixer requires a process identity")
    prefix = re.sub(r"[^A-Za-z0-9_-]", "-", session_id[:12])
    service = f"shutdown-remote-control-{prefix}-{pid}"
    command = ["env", "PYTHONUNBUFFERED=1", sys.executable, "-u",
               str(Path(__file__).with_name("shutdown.py")), "remote-control", session_id,
               "--pid", str(pid), "--proc-start", proc_start, "--who", who, "--session", session]
    if sys.platform == "darwin":
        try:
            logs = shutdown_state_root() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=logs, prefix=service + "-", suffix=".log", delete=False) as output:
                log = Path(output.name)
        except OSError as error:
            raise RuntimeError(f"could not start Remote Control fixer {service}: {error}") from error
        try:
            _ = detached.start_detached(command, within=15,
                ready=lambda: "Remote Control fixer started\n" in log.read_text(encoding="utf-8"),
                output=detached.AppendLog(log),
                environment=detached.ExplicitEnvironment({**os.environ, detached.STARTUP_SIGNAL_ENV: "1"}))
        except (detached.DetachedStartFailure, OSError) as error:
            raise RuntimeError(f"could not start Remote Control fixer {service}: {error}; log: {log}") from error
        return
    launch = ["systemd-run", "--user", "--collect", "--quiet", "--no-block", "--unit", service, *command]
    result = subprocess.run(launch, check=False, timeout=15, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"could not start Remote Control fixer {service}")


def _observe(session_id: str, pid: int, proc_start: str) -> ConfirmedSession | RegistryUnavailable | SessionGone:
    if not proc_start:
        return SessionGone()
    state = process_identity.check_owner(pid, process_identity.parse_start(proc_start))
    if state in (process_identity.OwnerCheck.GONE, process_identity.OwnerCheck.REPLACED):
        return SessionGone()
    if state is process_identity.OwnerCheck.UNKNOWN:
        return RegistryUnavailable()
    path = sessions.sessions_dir() / f"{pid}.json"
    try:
        value = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return RegistryUnavailable()
    if not isinstance(value, dict):
        return RegistryUnavailable()
    fields = cast(dict[str, object], value)
    record = sessions.parse_session(fields)
    if isinstance(record, sessions.UnreadableSessionRecord):
        return RegistryUnavailable()
    # Status and identity must belong to the same registry write.
    if fields.get("pid") != pid or fields.get("sessionId") != session_id or fields.get("procStart") != proc_start:
        return SessionGone()
    bridge = fields.get("bridgeSessionId")
    return ConfirmedSession(record, fields.get("status") == "idle", isinstance(bridge, str) and bool(bridge))


def _fix(session_id: str, pid: int, proc_start: str, clock: Clock) -> RemoteControlOutcome:
    started = clock.monotonic()
    deadline = started + DEADLINE
    flag_until = started + FLAG_WAIT
    attempt_until = flag_until
    attempts = 0
    last_error = ""

    def failed(reason: str) -> RemoteControlOutcome:
        fresh = _observe(session_id, pid, proc_start)
        if isinstance(fresh, SessionGone):
            return fresh
        if isinstance(fresh, ConfirmedSession) and fresh.active:
            return TurnedOn() if attempts else AlreadyOn()
        return Failed(f"{reason}: {last_error}" if last_error else reason)

    while True:
        observed = _observe(session_id, pid, proc_start)
        if isinstance(observed, SessionGone):
            return observed
        if isinstance(observed, ConfirmedSession) and observed.active:
            return TurnedOn() if attempts else AlreadyOn()
        now = clock.monotonic()
        if now >= deadline:
            return failed("Remote Control did not become active before the deadline")
        if now >= flag_until and isinstance(observed, ConfirmedSession):
            try:
                place = session_pane.record_place(observed.record)
            except session_pane.NoSuchTarget:
                return failed("no pane to type into")
            except (OSError, session_pane.HerdrFailure):
                clock.sleep(min(POLL_SECONDS, deadline - now))
                continue
            if attempts:
                try:
                    if "/remote-control is active" in session_pane.read_place(place):
                        return TurnedOn()
                except (OSError, session_pane.HerdrFailure):
                    pass
            if now >= attempt_until:
                if attempts >= ATTEMPTS:
                    return failed(f"Remote Control did not become active after {ATTEMPTS} attempts")
                # Resolve the session's own pane, then recheck identity and idle
                # status immediately before the only authorized input.
                observed = _observe(session_id, pid, proc_start)
                if isinstance(observed, SessionGone):
                    return observed
                if isinstance(observed, ConfirmedSession) and observed.active:
                    return TurnedOn() if attempts else AlreadyOn()
                if isinstance(observed, ConfirmedSession) and observed.idle:
                    if clock.monotonic() >= deadline:
                        return failed("Remote Control did not become active before the deadline")
                    try:
                        session_pane.type_place(place, "/remote-control")
                    except AgentBlocked:
                        pass
                    except (OSError, session_pane.HerdrFailure) as error:
                        last_error = str(error)
                        attempts += 1
                        attempt_until = clock.monotonic() + ATTEMPT_WAIT
                    else:
                        attempts += 1
                        attempt_until = clock.monotonic() + ATTEMPT_WAIT
        remaining = deadline - clock.monotonic()
        if remaining > 0:
            clock.sleep(min(POLL_SECONDS, remaining))


def fix(session_id: str, pid: int, proc_start: str, who: str, session: str, *, clock: Clock = _SYSTEM_CLOCK) -> RemoteControlOutcome:
    if os.environ.pop(detached.STARTUP_SIGNAL_ENV, "") == "1":
        print("Remote Control fixer started", file=sys.stderr, flush=True)
    outcome = _fix(session_id, pid, proc_start, clock)
    if isinstance(outcome, SessionGone):
        text = "session gone; Remote Control fixer finished"
    elif isinstance(outcome, AlreadyOn):
        text = "Remote Control already on"
    elif isinstance(outcome, TurnedOn):
        text = "Remote Control on after typing its command"
    else:
        text = f"Remote Control failed: {outcome.reason}"
    print(f"{who or session}: {text}", flush=True)
    return outcome
