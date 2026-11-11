"""Run a Claude implementation pass inside assign."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import NoReturn, TypedDict, cast

from scripts.agents import worker_lifecycle
from scripts.direct import claude_worker, phase_reservation, worker_state
from scripts.lib import models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import session_staff, staff, worker_admission

SCRIPTS = Path(__file__).resolve().parent
PYTHON = str(SCRIPTS / "../lib/py")


@dataclass(frozen=True)
class NewClaudeWorker:
    """Start a worker through the admission coordinator."""


@dataclass(frozen=True)
class ClaudeFollowUp:
    """Continue the named worker after an idle claim."""
    name: str


@dataclass(frozen=True)
class ClaudeLaunch:
    """The resolved settings for one Claude pass."""
    run: Path
    phase: str
    cwd: Path
    prompt: Path
    role: str
    slot: str
    fix_cause: str
    fix_pass: int
    model: str
    effort: str
    worker: NewClaudeWorker | ClaudeFollowUp
    command: tuple[str, ...]
    env: dict[str, str]


class WorkerIdentityRecord(TypedDict):
    staff_id: str
    slot: str
    model: str


class ClaudeAgentRecord(TypedDict, total=False):
    sessionId: str
    status: str


def _read(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text()))
        return cast(dict[str, object], value) if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _fail(message: str) -> NoReturn:
    print(f"assign.sh: {message}", file=sys.stderr)
    raise subprocess.CalledProcessError(2, [])


def _identity(launch: ClaudeLaunch, name: str) -> tuple[Path, WorkerIdentityRecord, str]:
    path = worker_state.worker_record_path(launch.run, name, "fix" if launch.role == "fix" else launch.slot)
    member = worker_admission.member(launch.run, name, staff.local_snapshot())
    return (path, {"staff_id": member.id if member else "", "slot": launch.slot, "model": launch.model},
            member.current_session if member else "")


def _record(launch: ClaudeLaunch, name: str) -> bool:
    try:
        with worker_state.worker_claim_lock(launch.run):
            path, identity, session = _identity(launch, name)
            if not session:
                return False
            with worker_lifecycle.locked_state(path) as state:
                state.publish(json.dumps({**{key: value for key, value in state.read_object().items()
                                             if key not in ("name", "family")}, **identity}))
            return True
    except Exception:
        return False


def _claim(launch: ClaudeLaunch, name: str, env: dict[str, str]) -> None:
    with worker_state.worker_claim_lock(launch.run):
        path, expected, session = _identity(launch, name)
        if not _read(path) and expected["staff_id"]:
            _ = path.write_text(json.dumps(expected))
        status = worker_state.worker_status_path(launch.run, launch.slot)
        claim = launch.run / f"impl_claim_{launch.slot}.json"
        owner = worker_state.read_worker_claim(claim)
        if isinstance(owner, worker_state.HeldWorkerClaim):
            _fail(f"{name} is claimed by launcher {owner.pid}")
        stale = isinstance(owner, worker_state.EndedWorkerClaim)
        claim.unlink(missing_ok=True)
        if stale or (status.exists() and status.read_text().strip() == "implementing"):
            _ = status.write_text("error\n")
        claude = env.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
        try:
            result = subprocess.run([claude, "agents", "--json"], env=env, capture_output=True, text=True)
            rows = cast(list[ClaudeAgentRecord], json.loads(result.stdout))
        except (OSError, ValueError):
            _fail("cannot read claude agents")
        missing: ClaudeAgentRecord = {}
        row = next((row for row in rows if row.get("sessionId") == session), missing)
        if row.get("status") != "idle":
            _fail(f"{name} is busy or gone")
        worker_state.publish_worker_claim(claim, name)


def _release(launch: ClaudeLaunch) -> None:
    with worker_state.worker_claim_lock(launch.run):
        claim = launch.run / f"impl_claim_{launch.slot}.json"
        if _read(claim).get("pid") == os.getpid():
            claim.unlink(missing_ok=True)


def _switch(launch: ClaudeLaunch, name: str) -> bool:
    with worker_state.worker_claim_lock(launch.run):
        record = staff.local_snapshot()
        member = worker_admission.member(launch.run, name, record)
        return member is not None and (session_staff.worker_family(record, member.id) != "claude"
            or worker_admission.dispatch_pending(launch.run, name, launch.prompt))


def _interrupted(signum: int, frame: FrameType | None) -> NoReturn:
    del frame
    raise SystemExit(128 + signum)


def run(launch: ClaudeLaunch) -> int:
    """Keep the shell launch order and release the claim on exit."""
    handlers = {sig: signal.signal(sig, _interrupted) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        return _run(launch)
    except subprocess.CalledProcessError as error:
        return error.returncode
    finally:
        if isinstance(launch.worker, ClaudeFollowUp):
            _release(launch)
        for sig, handler in handlers.items():
            _ = signal.signal(sig, handler)


def claim_phase(run: Path, phase: str, cwd: Path, log: Path, status: Path,
                slot: str, env: dict[str, str]) -> bool:
    """Claim the phase files before the worker opens."""
    if phase:
        with log.open("a") as output:
            reserved = subprocess.run([PYTHON, str(SCRIPTS / "phase_reservation.py"), "claim",
                "--session-dir", str(run), "--phase", phase, "--repo", str(cwd)],
                env=env, stdout=output, stderr=subprocess.STDOUT)
        if reserved.returncode:
            print(f"ERROR: phase {phase} could not claim its files before its worker opens; see {log}.", file=sys.stderr)
            write_text_atomic(status, "error\n")
            _ = subprocess.run(["bash", str(SCRIPTS / "board.sh"), "post", str(run), slot, "blocked",
                f"launcher: phase {phase} could not claim its files; its worker did not open"],
                env=env, text=True, check=False)
            return False
    return True


def _run(launch: ClaudeLaunch) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", launch.slot):
        print(f"ERROR: team_role must be alphanumeric/_- and 1-32 chars; got '{launch.slot}'.", file=sys.stderr)
        return 2
    env = dict(launch.env)
    for key, value in launch.env.items():
        if key.startswith("PLAN_DELEGATE_"):
            name = "UNIT_RUN_DIR" if key == "PLAN_DELEGATE_BOARD_DIR" else key.replace("PLAN_DELEGATE_", "UNIT_", 1)
            _ = env.setdefault(name, value)
    env["UNIT_TEAM_ROLE"] = launch.slot
    env["MODELS_CONFIG_FILE"] = str(models_registry.launch_configuration(env).path)

    def child(arguments: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, env=env, text=True, check=check, capture_output=capture)

    def board(kind: str, message: str) -> None:
        _ = child(["bash", str(SCRIPTS / "board.sh"), "post", str(launch.run), launch.slot, kind, message], check=False)

    def beat(kind: str, message: str, *details: str) -> None:
        _ = child(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(launch.run / "heartbeat.log"),
                   kind, message, *details], check=False)

    directory = phase_reservation.phase_directory(launch.run, launch.phase) if launch.phase else launch.run
    summary = directory / f"impl_summary_{launch.slot}.txt"
    status = directory / f"impl_status_{launch.slot}"
    log = directory / f"impl_agent_{launch.slot}.log"
    task = f"direct.{launch.role}"
    tag = f"{launch.role}:{launch.slot}"
    worker = launch.worker
    if isinstance(worker, ClaudeFollowUp):
        name = worker.name
    else:
        board_log = launch.run / "board.log"
        try:
            prefix = launch.run.name if f"worker={launch.run.name}-" in board_log.read_text() else ""
        except OSError:
            prefix = ""
        name = child(["bash", str(SCRIPTS / "worker_name.sh"), str(launch.cwd), launch.slot, launch.role, prefix],
                     capture=True).stdout.strip()
    try:
        allowed = worker_admission.admit(name, launch.run)
    except worker_admission.AdmissionRefused as error:
        _fail(str(error))
    _ = child([PYTHON, str(SCRIPTS / "worker_state.py"), str(launch.run)])
    directory.mkdir(parents=True, exist_ok=True)
    slot = allowed.slot
    if isinstance(worker, ClaudeFollowUp) and _switch(launch, name):
        worker = NewClaudeWorker()
        slot = worker_admission.NewWorkerSlot()
    if isinstance(worker, ClaudeFollowUp):
        _claim(launch, name, env)
    if not claim_phase(launch.run, launch.phase, launch.cwd, log, status, launch.slot, env):
        return 1
    _ = summary.write_text("")
    _ = status.write_text("implementing\n")
    _ = (directory / f"impl_agent_{launch.slot}").write_text(
        f"task={task}\nfamily=claude\nagent={launch.model}\neffort={launch.effort}\n")
    progress = launch.run / "progress_history_state.json"
    phase_options = ["--phase-id", launch.phase] if launch.phase else []
    worker_options = ["--worker-id", slot.row.id] if isinstance(slot, worker_admission.ExistingWorkerSlot) else []
    env["UNIT_PASS_OWNER"] = "launcher"
    if progress.is_file():
        started = child([PYTHON, str(SCRIPTS / "progress_history.py"), "start-pass", "--session-dir", str(launch.run),
            "--pass-kind", launch.role, "--prompt-file", str(launch.prompt), "--fix-cause", launch.fix_cause,
            "--fix-pass", str(launch.fix_pass), "--activity", launch.role, "--called-task", task,
            "--worker-name", name, "--called-family", "claude", "--called-model", launch.model,
            "--called-effort", launch.effort or "unset", "--launcher-command", json.dumps(launch.command),
            "--launcher-pid", str(os.getpid()),
            *phase_options, *worker_options], check=False)
        if started.returncode:
            print(f"ERROR: unable to record the {launch.role} pass start.", file=sys.stderr)
            _ = status.write_text("error\n")
            board("blocked", f"launcher: unable to record the {launch.role} pass start")
            return 1
    beat("header", f"{tag} (claude/{launch.model}:{launch.effort or 'unset'})", launch.role)
    note = f"follow-up to {name}" if isinstance(worker, ClaudeFollowUp) else "launcher up"
    board("register", f"{launch.role} {note} (claude/{launch.model}:{launch.effort or 'unset'}); "
          + f"worker={name}; reach=SendMessage; role={launch.role}; status in impl_status_{launch.slot}")
    env["UNIT_RUN_DIR"] = str(launch.run)
    build_folders = str(SCRIPTS / "../lib/build_folders.py")
    env["TMPDIR"] = child([PYTHON, build_folders, "run-temp-path", str(launch.run)], capture=True).stdout.strip()
    if not Path(env["TMPDIR"]).is_dir():
        _ = child([PYTHON, build_folders, "stable", "--run-temp", str(launch.run)], capture=True)
    message = directory / f"impl_follow_message_{launch.slot}.{os.getpid()}.txt"
    if isinstance(worker, ClaudeFollowUp):
        _ = message.write_bytes(launch.prompt.read_bytes() +
            f"\n\nAs your last act, write impl_summary_{launch.slot}.txt, then post done on the board.\n".encode())
    turn = claude_worker.ClaudeTurn(name, launch.run, launch.cwd,
        message if isinstance(worker, ClaudeFollowUp) else launch.prompt, summary, log,
        launch.model, launch.effort, launch.role, env.get("WORKER_STAFF_ID", ""), True, env)
    try:
        try:
            if isinstance(worker, ClaudeFollowUp):
                member = worker_admission.member(launch.run, name, staff.local_snapshot())
                delivered = claude_worker.follow_up(turn, member.current_session if member else "")
            else:
                delivered = claude_worker.open_worker(turn)
        finally:
            _ = _record(launch, name)
        if isinstance(delivered, claude_worker.Delivered):
            watch = subprocess.Popen(["bash", str(SCRIPTS / "../agents/heartbeat_watch.sh"),
                str(launch.run / "heartbeat.log"), tag, str(os.getpid()), str(log), "60"], env=env)
            try:
                code = claude_worker.wait_for_turn(turn, delivered)
            finally:
                if watch.poll() is None:
                    watch.terminate()
                _ = watch.wait()
        else:
            code = delivered.code if isinstance(delivered, claude_worker.NotDelivered) else 0
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    except Exception as error:
        print(f"claude_worker: {name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
        code = 1
    finally:
        if isinstance(worker, ClaudeFollowUp):
            message.unlink(missing_ok=True)
    findings = launch.run / "findings_state.json"
    resolves = env.get("UNIT_RESOLVES_ROUND", "1" if launch.role == "fix" and launch.slot == "impl" else "0") == "1"
    repair_options = ["--phase", launch.phase] if launch.phase else []
    if code == 0:
        if progress.is_file() and child([PYTHON, str(SCRIPTS / "progress_history.py"), "finish-pass",
                "--session-dir", str(launch.run), "--status", "completed"], check=False).returncode:
            print(f"ERROR: unable to record the {launch.role} pass completion.", file=sys.stderr)
            _ = status.write_text("error\n")
            board("blocked", f"launcher: unable to record the {launch.role} pass completion")
            return 1
        if resolves and findings.is_file() and child([PYTHON, str(SCRIPTS / "findings.py"), "landed",
                "--session-dir", str(launch.run), *repair_options], check=False).returncode:
            print("ERROR: unable to record the repair round as landed.", file=sys.stderr)
            _ = status.write_text("error\n")
            board("blocked", "launcher: unable to record the repair round as landed")
            return 1
        _ = status.write_text("implemented\n")
        beat("wrapper", f"{tag} agent finished")
        board("done", f"launcher: {launch.role} finished; summary at impl_summary_{launch.slot}.txt")
    else:
        ledger_error = 0
        if progress.is_file() and child([PYTHON, str(SCRIPTS / "progress_history.py"), "finish-pass",
                "--session-dir", str(launch.run), "--status", "error"], check=False).returncode:
            print(f"ERROR: unable to record the {launch.role} pass error.", file=sys.stderr)
            ledger_error = 1
        if resolves and findings.is_file() and child([PYTHON, str(SCRIPTS / "findings.py"), "abandon",
                "--session-dir", str(launch.run), "--edits-landed", *repair_options,
                "--reason", f"the {launch.role} worker exited with code {code}"], check=False).returncode:
            print("ERROR: unable to record the repair round as abandoned.", file=sys.stderr)
            ledger_error = 1
        _ = status.write_text("error\n")
        beat("wrapper", f"{tag} agent exited with code {code}")
        board("blocked", f"launcher: {launch.role} exited with code {code}; this worker is down; ledger_error={ledger_error}")
    _ = child(["bash", str(SCRIPTS / "board.sh"), "release", str(launch.run), launch.slot, "cargo"],
              check=False, capture=True)
    return code
