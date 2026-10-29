#!/usr/bin/env python3
"""Restart idle Claude sessions that missed their worktree's nix environment.

Registry decoding and process identity belong to message/sessions.py. Build
holds belong to the build-target state readers. A locked journal reserves each
restart before invoking shutdown, including failed attempts, for one hour.
A five-minute notifier timer runs the sweep.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
from scripts.lib import nix_env
from scripts.message import sessions
sys.path.insert(0, str(SCRIPTS / "notify"))
if TYPE_CHECKING:
    from scripts.notify import push_texts
else:
    import push_texts

STATE_DIR = Path.home() / ".local/state/nix-heal"
RESTART_INTERVAL_SECONDS = 3600
# A fresh allow and export can each consume the helper's full budget, followed
# by herdr's 90-second launch and command startup/record confirmation overhead.
RELAUNCH_TIMEOUT_SECONDS = 2 * nix_env.TIMEOUT_SECONDS + 90 + 30


@dataclass(frozen=True)
class HealSession:
    session_id: str
    pid: int
    proc_start: str
    name: str
    cwd: Path
    status: str
    config_dir: Path


@dataclass(frozen=True)
class Healthy:
    pass


@dataclass(frozen=True)
class Broken:
    pass


@dataclass(frozen=True)
class Unavailable:
    reason: str


@dataclass(frozen=True)
class Gone:
    pass


class NoPendingRestart(TypedDict):
    kind: Literal["none"]


class PendingRestart(TypedDict):
    kind: Literal["stopping", "launching"]
    login: str
    name: str
    cwd: str
    config_dir: str
    pid: int
    proc_start: str


@dataclass(frozen=True)
class StopComplete:
    """The conductor recorded a completed stop for this exact process."""


@dataclass(frozen=True)
class StopPending:
    """The conductor has not yet confirmed the stop."""


@dataclass(frozen=True)
class StopRefused:
    reason: str


class HealHistory(TypedDict):
    restart_at: float
    restart_pid: int
    restart_proc_start: str
    healthy_since_restart: bool
    notified: bool
    action: str
    reason: str
    pending: NoPendingRestart | PendingRestart


class HealLog(TypedDict):
    time: str
    machine: str
    session_id: str
    name: str
    cwd: str
    action: str
    reason: str


def registry_sessions() -> list[HealSession]:
    """Decode all registries without probing unrelated processes."""
    directories = set(Path.home().glob(".claude*/sessions"))
    configured = os.environ.get("NOTIFIER_SESSIONS_DIR")
    if configured:
        directories.add(Path(configured))
    found: dict[tuple[str, int, str], HealSession] = {}
    previous = os.environ.get("NOTIFIER_SESSIONS_DIR")
    try:
        for directory in sorted(directories):
            os.environ["NOTIFIER_SESSIONS_DIR"] = str(directory)
            for observation in sessions.read_registry().entries:
                record, details = observation.record, observation.details
                if sessions.headless(record):
                    continue
                cwd, status = details.get("cwd"), details.get("status")
                if (not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute()
                        or not isinstance(status, str) or not record["sessionId"]):
                    continue
                started = record.get("procStart", "")
                if not started:
                    continue
                found[(record["sessionId"], record["pid"], started)] = HealSession(record["sessionId"], record["pid"], started,
                    record["name"], Path(cwd), status, directory.parent)
    finally:
        if previous is None:
            if "NOTIFIER_SESSIONS_DIR" in os.environ:
                del os.environ["NOTIFIER_SESSIONS_DIR"]
        else:
            os.environ["NOTIFIER_SESSIONS_DIR"] = previous
    return list(found.values())


def list_sessions() -> list[HealSession]:
    """List confirmed live processes, including those without messaging sockets."""
    return [item for item in registry_sessions() if sessions.process_start(item.pid) == item.proc_start]


def find_session(session_id: str) -> HealSession | Gone:
    for item in registry_sessions():
        if item.session_id == session_id and sessions.process_start(item.pid) == item.proc_start:
            return item
    return Gone()


def inspect(session: HealSession) -> Healthy | Broken | Unavailable | Gone:
    """Only a confirmed process with readable environment can be broken."""
    try:
        if nix_env.nearest_envrc(session.cwd) is None:
            return Healthy()
        if not session.proc_start or sessions.process_start(session.pid) != session.proc_start:
            return Gone()
        if sys.platform == "darwin":
            from scripts.whoami import account

            parts = account.procargs2_parts(account.kern_procargs2(session.pid), lambda name: name == "IN_NIX_SHELL")
            loaded = bool(parts.environment.get("IN_NIX_SHELL"))
        else:
            process = Path(f"/proc/{session.pid}")
            if process.stat().st_uid != os.getuid():
                return Unavailable("process belongs to another user")
            environment = (process / "environ").read_bytes().split(b"\0")
            loaded = any(value.startswith(b"IN_NIX_SHELL=") and value != b"IN_NIX_SHELL=" for value in environment)
        if sessions.process_start(session.pid) != session.proc_start:
            return Gone()
        return Healthy() if loaded else Broken()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return Unavailable(str(error))


def broken(session: HealSession) -> bool:
    return isinstance(inspect(session), Broken)


def hold_reason() -> str:
    """Return a local hold or unreadable-state reason, without reading hold files."""
    try:
        from scripts.build_targets import targets

        local_platform = "macos" if sys.platform == "darwin" else "linux"
        matched = False
        for target in targets.read_targets():
            if target.platform != local_platform:
                continue
            matched = True
            state = targets.target_state(target)
            if isinstance(state, targets.Held):
                return f"build hold: {state.holder}: {state.reason}"
            if isinstance(state, targets.StateUnreadable):
                return f"build hold state unavailable: {state.reason}"
        return "" if matched else "build hold state unavailable: this machine has no configured target"
    except (OSError, ValueError, ImportError) as error:
        return f"build hold state unavailable: {error}"


def new_history() -> HealHistory:
    return HealHistory(restart_at=0.0, restart_pid=0, restart_proc_start="", healthy_since_restart=False,
                       notified=False, action="", reason="", pending=NoPendingRestart(kind="none"))


def read_pending(value: object) -> NoPendingRestart | PendingRestart:
    if not isinstance(value, dict):
        raise ValueError("invalid nix-heal pending restart")
    fields = cast(dict[str, object], value)
    if fields.get("kind") == "none":
        return NoPendingRestart(kind="none")
    login, name, cwd = fields.get("login"), fields.get("name"), fields.get("cwd")
    config_dir, pid, proc_start = fields.get("config_dir"), fields.get("pid"), fields.get("proc_start")
    kind = fields.get("kind")
    if (kind not in ("stopping", "launching") or not isinstance(login, str) or not login
            or not isinstance(name, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or not isinstance(config_dir, str) or not Path(config_dir).is_absolute()
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(proc_start, str) or not proc_start):
        raise ValueError("invalid nix-heal pending restart; refusing an unproven relaunch")
    return PendingRestart(kind="launching" if kind == "launching" else "stopping", login=login, name=name, cwd=cwd,
                          config_dir=config_dir, pid=pid, proc_start=proc_start)


def read_history() -> dict[str, HealHistory]:
    try:
        value = cast(object, json.loads((STATE_DIR / "state.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("nix-heal state is not an object; refusing to lose the restart guard")
    histories: dict[str, HealHistory] = {}
    for key, raw in cast(dict[str, object], value).items():
        if not isinstance(raw, dict):
            raise ValueError("invalid nix-heal session state")
        fields = cast(dict[str, object], raw)
        restarted, pid = fields.get("restart_at"), fields.get("restart_pid")
        start, notified = fields.get("restart_proc_start"), fields.get("notified")
        healthy = fields.get("healthy_since_restart")
        action, reason = fields.get("action"), fields.get("reason")
        if (not isinstance(restarted, (int, float)) or isinstance(restarted, bool) or not math.isfinite(restarted)
                or not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(start, str)
                or not isinstance(healthy, bool) or not isinstance(notified, bool)
                or not isinstance(action, str) or not isinstance(reason, str)):
            raise ValueError("invalid nix-heal session state; refusing to lose the restart guard")
        histories[key] = HealHistory(restart_at=float(restarted), restart_pid=pid,
            restart_proc_start=start, healthy_since_restart=healthy, notified=notified, action=action, reason=reason,
            pending=read_pending(fields.get("pending", {"kind": "none"})))
    return histories


def save_history(histories: dict[str, HealHistory]) -> None:
    temporary = STATE_DIR / f"state.{os.getpid()}.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(histories, stream)
        stream.flush()
        os.fsync(stream.fileno())
    _ = temporary.replace(STATE_DIR / "state.json")


def log(session: HealSession, history: HealHistory, action: str, reason: str) -> None:
    """Retain every restart and refusal; suppress unchanged skip reasons."""
    if action in {"skip", "pending"} and history["action"] == action and history["reason"] == reason:
        return
    entry = HealLog(time=datetime.now(timezone.utc).isoformat(), machine=socket.gethostname(),
        session_id=session.session_id, name=session.name, cwd=str(session.cwd), action=action, reason=reason)
    with (STATE_DIR / "log.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(entry) + "\n")
    history["action"], history["reason"] = action, reason


def alert(session: HealSession, history: HealHistory, reason: str) -> None:
    if history["notified"]:
        return
    if reason.endswith(".envrc loaded but no nix shell (IN_NIX_SHELL unset)"):
        cause = "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."
    elif reason == "session still has no nix shell after its heal restart":
        cause = "An automatic restart did not fix it."
    else:
        cause = "The automatic restart could not fix it. The nix shell setup refused to load."
    check = shlex.join([str(SCRIPTS / "lib/py"), str(Path(__file__).resolve()), "sweep",
                        "--only", session.session_id, "--check"])
    push = push_texts.render("nix_heal.failed", session=session.name,
                             machine=socket.gethostname(), cwd=str(session.cwd), cause=cause)
    command = push_texts.user_send_argv(push, key=f"nix-heal-{session.session_id}")
    command.extend(["--clears-when", check])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode == 0 or (result.returncode == 1 and "QUEUED:" in result.stdout + result.stderr):
        history["notified"] = True
    else:
        log(session, history, "push_failed", (result.stderr or result.stdout).strip() or f"exit {result.returncode}")


def stop_completion(session: HealSession, login: str) -> StopComplete | StopPending | StopRefused:
    """Read the conductor's typed completion signal; down itself is asynchronous."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    from scripts.shutdown import record

    saved = record.find_session(login, session.session_id)
    if saved is None:
        return StopPending()
    owner = saved["session"]
    if owner["pid"] != session.pid or owner["proc_start"] != session.proc_start or saved["then"] != "stop":
        return StopRefused("shutdown record no longer belongs to this heal stop")
    progress = saved["progress"]["kind"]
    if progress == "stop failed" or record.stop_incomplete(saved):
        return StopRefused("shutdown did not complete the session stop")
    if progress in {"stopped", "already gone"} and sessions.process_start(session.pid) != session.proc_start:
        return StopComplete()
    if progress == "process identity lost":
        return StopRefused("session process identity changed during shutdown")
    return StopPending()


def confirm_relaunch(session: HealSession, history: HealHistory) -> bool:
    """A successful command is not yet proof of a live, healthy replacement."""
    replacement = next((item for item in list_sessions() if item.session_id == session.session_id), None)
    if (replacement is not None and (replacement.pid, replacement.proc_start) != (session.pid, session.proc_start)
            and replacement.cwd.resolve() == session.cwd.resolve()
            and replacement.config_dir.resolve() == session.config_dir.resolve()
            and isinstance(inspect(replacement), Healthy)):
        history["pending"] = NoPendingRestart(kind="none")
        history["healthy_since_restart"] = True
        history["notified"] = False
        log(session, history, "restart", "nix shell missing; stopped session relaunched")
        return True
    return False


def resume_pending(session: HealSession, pending: PendingRestart, history: HealHistory,
                   histories: dict[str, HealHistory]) -> None:
    if pending["kind"] == "launching":
        if not confirm_relaunch(session, history):
            replacement = next((item for item in list_sessions() if item.session_id == session.session_id), None)
            still_broken = replacement is not None and isinstance(inspect(replacement), Broken)
            reason = ("session still has no nix shell after its heal restart" if still_broken else
                      "waiting for a live replacement with a nix shell; relaunch will not be repeated")
            log(session, history, "pending", reason)
            if still_broken or time.time() - history["restart_at"] >= RELAUNCH_TIMEOUT_SECONDS:
                alert(session, history, reason)
        return
    held = hold_reason()
    if held:
        log(session, history, "pending", held)
        return
    completion = stop_completion(session, pending["login"])
    if isinstance(completion, StopPending):
        log(session, history, "pending", "waiting for shutdown stop completion")
        return
    if isinstance(completion, StopRefused):
        log(session, history, "refused", completion.reason)
        alert(session, history, completion.reason)
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(session, history, "refused", loaded.reason)
        alert(session, history, loaded.reason)
        return
    held = hold_reason()
    if held:
        log(session, history, "pending", held)
        return
    # Keep the stopped session recoverable even if the command times out or the
    # healer dies. A launching reservation is observed, never blindly retried.
    pending["kind"] = "launching"
    history["restart_at"] = time.time()
    save_history(histories)
    command = [str(SCRIPTS / "lib/py"), str(SCRIPTS / "shutdown/shutdown.py"),
               "restart", pending["login"], "--only", session.session_id, "--here"]
    environment = dict(loaded.env)
    environment[nix_env.HEAL_SESSION_ENV] = session.session_id
    environment[nix_env.HEAL_DIRECTORY_ENV] = str(session.cwd.resolve())
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                timeout=RELAUNCH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        reason = f"nix shell relaunch failed: {error}"
        log(session, history, "refused", reason)
        alert(session, history, reason)
        return
    if result.returncode:
        reason = (result.stderr or result.stdout).strip() or f"restart exit {result.returncode}"
        log(session, history, "refused", reason)
        alert(session, history, reason)
        return
    if not confirm_relaunch(session, history):
        log(session, history, "pending", "restart command completed; waiting for a live replacement with a nix shell")


def heal_one(session: HealSession, history: HealHistory, histories: dict[str, HealHistory]) -> None:
    observation = inspect(session)
    if isinstance(observation, (Healthy, Gone)):
        if isinstance(observation, Healthy):
            history["notified"] = False
            history["healthy_since_restart"] = True
        return
    if isinstance(observation, Unavailable):
        log(session, history, "skip", observation.reason)
        return
    held = hold_reason()
    if held:
        log(session, history, "skip", held)
        return
    if session.status not in {"idle", "shell"}:
        log(session, history, "skip", "session is not idle")
        return
    now = time.time()
    if history["restart_at"] and not history["healthy_since_restart"]:
        replaced = (session.pid, session.proc_start) != (history["restart_pid"], history["restart_proc_start"])
        if replaced or now - history["restart_at"] >= RESTART_INTERVAL_SECONDS:
            reason = "session still has no nix shell after its heal restart"
            log(session, history, "refused", reason)
            alert(session, history, reason)
            return
        log(session, history, "skip", "heal restart already requested within 60 minutes")
        return
    if history["restart_at"] and now - history["restart_at"] < RESTART_INTERVAL_SECONDS:
        log(session, history, "skip", "heal restart already requested within 60 minutes")
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(session, history, "refused", loaded.reason)
        alert(session, history, loaded.reason)
        return
    # Loading can take minutes. Recheck both coordination and identity before
    # shutdown gets authority to stop a process.
    held = hold_reason()
    if held:
        log(session, history, "skip", held)
        return
    fresh = next((item for item in list_sessions() if item.session_id == session.session_id), None)
    if (fresh is None or fresh.pid != session.pid or fresh.proc_start != session.proc_start
            or fresh.status not in {"idle", "shell"} or not broken(fresh)):
        log(session, history, "skip", "session changed during environment load")
        return
    from scripts.whoami import account

    selected = account.claude_account(session.config_dir)
    if selected is None:
        reason = "cannot identify the session's Claude account"
        log(session, history, "refused", reason)
        alert(session, history, reason)
        return
    held = hold_reason()
    if held:
        log(session, history, "skip", held)
        return
    history["restart_at"], history["restart_pid"] = time.time(), session.pid
    history["restart_proc_start"] = session.proc_start
    history["healthy_since_restart"] = False
    pending = PendingRestart(kind="stopping", login=selected.login, name=session.name, cwd=str(session.cwd),
                             config_dir=str(session.config_dir), pid=session.pid, proc_start=session.proc_start)
    history["pending"] = pending
    save_history(histories)
    command = [str(SCRIPTS / "lib/py"), str(SCRIPTS / "shutdown/shutdown.py"), "down", selected.login,
               "--here", "--only", session.session_id]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        reason = (result.stderr or result.stdout).strip() or f"shutdown exit {result.returncode}"
        log(session, history, "refused", reason)
        alert(session, history, reason)
        return
    resume_pending(session, pending, history, histories)


def sweep(only: str = "", check: bool = False) -> int:
    """Keep one bad record or launch from preventing other sessions' recovery."""
    try:
        selected = [item for item in list_sessions() if not only or item.session_id == only]
        if check:
            # Absence is not healthy proof: a registry write may be in progress
            # during restart. Keep the push until a live replacement proves it.
            if only and not selected:
                return 1
            return 0 if all(isinstance(inspect(item), Healthy) for item in selected) else 1
        needs_attention = [item for item in selected if not isinstance(inspect(item), (Healthy, Gone))]
        if not needs_attention and not (STATE_DIR / "state.json").exists():
            return 0
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "state.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            histories = read_history()
            pending_ids: set[str] = set()
            # A stopped session has left the registry. Recover its saved request
            # before considering any new live session, including after a hold.
            for session_id, history in histories.items():
                pending = history["pending"]
                if pending["kind"] == "none" or (only and only != session_id):
                    continue
                pending_ids.add(session_id)
                session = HealSession(session_id, pending["pid"], pending["proc_start"], pending["name"],
                                      Path(pending["cwd"]), "idle", Path(pending["config_dir"]))
                try:
                    resume_pending(session, pending, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(session, history, "refused", str(error))
                finally:
                    save_history(histories)
            for session in selected:
                if session.session_id in pending_ids:
                    continue
                if session not in needs_attention and session.session_id not in histories:
                    continue
                history = histories.setdefault(session.session_id, new_history())
                try:
                    heal_one(session, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(session, history, "refused", str(error))
                finally:
                    save_history(histories)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"nix-heal: {error}", file=sys.stderr)
        return 1 if check else 0


class SweepArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.only: str = ""
        self.check: bool = False


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("sweep",))
    _ = parser.add_argument("--only", default="")
    _ = parser.add_argument("--check", action="store_true")
    parsed = parser.parse_args(arguments, namespace=SweepArguments())
    return sweep(parsed.only, parsed.check)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
