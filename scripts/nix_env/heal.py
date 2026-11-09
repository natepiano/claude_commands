#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Restart idle Claude sessions that missed their worktree's nix environment.

Registry decoding and process identity belong to message/sessions.py. Build
holds belong to the build-target state readers. A locked journal reserves each
restart before invoking shutdown, including failed attempts, for one hour.
A five-minute notifier timer runs the sweep.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

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
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
from scripts.lib import atomic_file, conf, nix_env
from scripts.herdr import pane_launch
from scripts.production import staff
from scripts.message import send_result, sessions
from scripts.message import send_budget
if TYPE_CHECKING:
    from scripts.notify import push_texts, review
else:
    from scripts.notify import push_texts
    from scripts.notify import review


def state_directory() -> Path:
    return Path(conf.read("nix_env").text("heal_state_directory")).expanduser()


@dataclass(frozen=True)
class HealLimits:
    restart_seconds: float
    relaunch_seconds: float
    alert_seconds: float

    @classmethod
    def read(cls) -> HealLimits:
        settings = conf.read("nix_env")
        return cls(settings.number("restart_interval_seconds"),
                   2 * settings.number("command_timeout_seconds")
                   + settings.number("relaunch_launch_seconds") + settings.number("relaunch_confirmation_seconds"),
                   settings.number("alert_timeout_seconds"))


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
    config_dir: NotRequired[str]
    pid: int
    proc_start: str
    stop_request_sent: bool


@dataclass(frozen=True)
class StopComplete:
    """The exact process has exited."""


@dataclass(frozen=True)
class StopPending:
    """The original process still runs."""


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
    directories = set(Path.home().glob(conf.read("nix_env").text("session_directory_glob")))
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
    stop_request_sent = fields.get("stop_request_sent", True)
    if (kind not in ("stopping", "launching") or not isinstance(login, str) or not login
            or not isinstance(name, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or ("config_dir" in fields and (not isinstance(config_dir, str) or not Path(config_dir).is_absolute()))
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(proc_start, str) or not proc_start
            or not isinstance(stop_request_sent, bool)):
        raise ValueError("invalid nix-heal pending restart; refusing an unproven relaunch")
    pending = PendingRestart(kind="launching" if kind == "launching" else "stopping", login=login, name=name, cwd=cwd,
                             pid=pid, proc_start=proc_start, stop_request_sent=stop_request_sent)
    if isinstance(config_dir, str):
        pending["config_dir"] = config_dir
    return pending


def read_history(directory: Path) -> dict[str, HealHistory]:
    try:
        value = cast(object, json.loads((directory / "state.json").read_text(encoding="utf-8")))
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


def save_history(directory: Path, histories: dict[str, HealHistory]) -> None:
    atomic_file.write_text_atomic(directory / "state.json", json.dumps(histories))


def log(directory: Path, session: HealSession, history: HealHistory, action: str, reason: str) -> None:
    """Retain every restart and refusal; suppress unchanged skip reasons."""
    if action in {"skip", "pending"} and history["action"] == action and history["reason"] == reason:
        return
    entry = HealLog(time=datetime.now(timezone.utc).isoformat(), machine=socket.gethostname(),
        session_id=session.session_id, name=session.name, cwd=str(session.cwd), action=action, reason=reason)
    with (directory / "log.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(entry) + "\n")
    history["action"], history["reason"] = action, reason


def alert(directory: Path, limits: HealLimits, session: HealSession, history: HealHistory, reason: str) -> None:
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
    timeout = max(limits.alert_seconds, send_budget.user_seconds(review.user_send_seconds()))
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome) and outcome.header.outcome in {"sent", "skipped", "replaced", "queued", "kept"}:
        history["notified"] = True
    else:
        log(directory, session, history, "push_failed", (result.stderr or result.stdout).strip() or f"exit {result.returncode}")


def stop_completion(session: HealSession, login: str) -> StopComplete | StopPending | StopRefused:
    del login
    observed = sessions.process_liveness(session.pid, session.proc_start)
    if isinstance(observed, sessions.SessionLivenessUnknown):
        return StopRefused(observed.reason)
    return StopPending() if isinstance(observed, sessions.LiveSessionProcess) else StopComplete()


def confirm_relaunch(directory: Path, session: HealSession, history: HealHistory) -> bool:
    """A successful command is not yet proof of a live, healthy replacement."""
    replacement = next((item for item in list_sessions() if item.session_id == session.session_id), None)
    if (replacement is not None and (replacement.pid, replacement.proc_start) != (session.pid, session.proc_start)
            and replacement.cwd.resolve() == session.cwd.resolve()
            and replacement.config_dir.resolve() == session.config_dir.resolve()
            and isinstance(inspect(replacement), Healthy)):
        history["pending"] = NoPendingRestart(kind="none")
        history["healthy_since_restart"] = True
        history["notified"] = False
        log(directory, session, history, "restart", "nix shell missing; stopped session relaunched")
        return True
    return False


def request_stop(directory: Path, limits: HealLimits, session: HealSession, pending: PendingRestart, history: HealHistory,
                 histories: dict[str, HealHistory]) -> bool:
    from scripts.shutdown import stop_resume

    result = stop_resume.stop(session.session_id)
    if not isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
        reason = result.reason if isinstance(result, stop_resume.StopFailed) else "no local session tab"
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason)
        return False
    pending["stop_request_sent"] = True
    save_history(directory, histories)
    return True


def resume_pending(directory: Path, limits: HealLimits, session: HealSession, pending: PendingRestart, history: HealHistory,
                   histories: dict[str, HealHistory]) -> None:
    if pending["kind"] == "launching":
        if not confirm_relaunch(directory, session, history):
            replacement = next((item for item in list_sessions() if item.session_id == session.session_id), None)
            still_broken = replacement is not None and isinstance(inspect(replacement), Broken)
            reason = ("session still has no nix shell after its heal restart" if still_broken else
                      "waiting for a live replacement with a nix shell; relaunch will not be repeated")
            log(directory, session, history, "pending", reason)
            if still_broken or time.time() - history["restart_at"] >= limits.relaunch_seconds:
                alert(directory, limits, session, history, reason)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "pending", held)
        return
    completion = stop_completion(session, pending["login"])
    if isinstance(completion, StopPending):
        if not pending["stop_request_sent"]:
            if sessions.process_start(session.pid) != session.proc_start:
                history["pending"] = NoPendingRestart(kind="none")
                reason = "unsent shutdown request lost its original session process; restart cancelled"
                log(directory, session, history, "refused", reason)
                alert(directory, limits, session, history, reason)
                return
            if not request_stop(directory, limits, session, pending, history, histories):
                return
        log(directory, session, history, "pending", "waiting for shutdown stop completion")
        return
    if isinstance(completion, StopRefused):
        log(directory, session, history, "refused", completion.reason)
        alert(directory, limits, session, history, completion.reason)
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(directory, session, history, "refused", loaded.reason)
        alert(directory, limits, session, history, loaded.reason)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "pending", held)
        return
    # Keep the stopped session recoverable even if the command times out or the
    # healer dies. A launching reservation is observed, never blindly retried.
    pending["kind"] = "launching"
    history["restart_at"] = time.time()
    save_history(directory, histories)
    from scripts.shutdown import stop_resume
    try:
        result = stop_resume.resume(session.session_id, why="nix shell heal")
    except (OSError, subprocess.TimeoutExpired) as error:
        log(directory, session, history, "refused", str(error))
        alert(directory, limits, session, history, str(error))
        return
    if not isinstance(result, stop_resume.ClaudeUp):
        reason = (result.reason
                  if isinstance(result, stop_resume.ResumeFailed)
                  else "no local session tab")
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason)
        return
    if not confirm_relaunch(directory, session, history):
        log(directory, session, history, "pending", "restart command completed; waiting for a live replacement with a nix shell")


def heal_one(directory: Path, limits: HealLimits, session: HealSession, history: HealHistory, histories: dict[str, HealHistory]) -> None:
    observation = inspect(session)
    if isinstance(observation, (Healthy, Gone)):
        if isinstance(observation, Healthy):
            history["notified"] = False
            history["healthy_since_restart"] = True
        return
    if isinstance(observation, Unavailable):
        log(directory, session, history, "skip", observation.reason)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    if session.status not in {"idle", "shell"}:
        log(directory, session, history, "skip", "session is not idle")
        return
    now = time.time()
    interval = limits.restart_seconds
    if history["restart_at"] and not history["healthy_since_restart"]:
        replaced = (session.pid, session.proc_start) != (history["restart_pid"], history["restart_proc_start"])
        if replaced or now - history["restart_at"] >= interval:
            reason = "session still has no nix shell after its heal restart"
            log(directory, session, history, "refused", reason)
            alert(directory, limits, session, history, reason)
            return
        log(directory, session, history, "skip", f"heal restart already requested within {interval / 60:g} minutes")
        return
    if history["restart_at"] and now - history["restart_at"] < interval:
        log(directory, session, history, "skip", f"heal restart already requested within {interval / 60:g} minutes")
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(directory, session, history, "refused", loaded.reason)
        alert(directory, limits, session, history, loaded.reason)
        return
    # Loading can take minutes. Recheck both coordination and identity before
    # shutdown gets authority to stop a process.
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    fresh = next((item for item in list_sessions() if item.session_id == session.session_id), None)
    if (fresh is None or fresh.pid != session.pid or fresh.proc_start != session.proc_start
            or fresh.status not in {"idle", "shell"} or not broken(fresh)):
        log(directory, session, history, "skip", "session changed during environment load")
        return
    from scripts.whoami import account

    selected = account.claude_account(session.config_dir)
    if selected is None:
        reason = "cannot identify the session's Claude account"
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    history["restart_at"], history["restart_pid"] = time.time(), session.pid
    history["restart_proc_start"] = session.proc_start
    history["healthy_since_restart"] = False
    pending = PendingRestart(kind="stopping", login=selected.login, name=session.name, cwd=str(session.cwd),
                             pid=session.pid, proc_start=session.proc_start,
                             stop_request_sent=False)
    if staff.initial_snapshot().by_member_reference(session.session_id) is None:
        pending["config_dir"] = str(session.config_dir)
    history["pending"] = pending
    save_history(directory, histories)
    if not request_stop(directory, limits, session, pending, history, histories):
        return
    resume_pending(directory, limits, session, pending, history, histories)


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
        directory = state_directory()
        limits = HealLimits.read()
        needs_attention = [item for item in selected if not isinstance(inspect(item), (Healthy, Gone))]
        if not needs_attention and not (directory / "state.json").exists():
            return 0
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "state.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            histories = read_history(directory)
            pending_ids: set[str] = set()
            # A stopped session has left the registry. Recover its saved request
            # before considering any new live session, including after a hold.
            for session_id, history in histories.items():
                pending = history["pending"]
                if pending["kind"] == "none" or (only and only != session_id):
                    continue
                pending_ids.add(session_id)
                session = HealSession(session_id, pending["pid"], pending["proc_start"], pending["name"],
                                      Path(pending["cwd"]), "idle", Path(pane_launch.config_home({**pending, "session_id": session_id})))
                try:
                    resume_pending(directory, limits, session, pending, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(directory, session, history, "refused", str(error))
                finally:
                    save_history(directory, histories)
            for session in selected:
                if session.session_id in pending_ids:
                    continue
                if session not in needs_attention and session.session_id not in histories:
                    continue
                history = histories.setdefault(session.session_id, new_history())
                try:
                    heal_one(directory, limits, session, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(directory, session, history, "refused", str(error))
                finally:
                    save_history(directory, histories)
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
