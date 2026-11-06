#!/usr/bin/env python3
"""Claim a unit worker and watch one named dispatch through completion."""
from __future__ import annotations

import argparse
import fcntl
import json
import itertools
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast, final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import design_roles
from scripts.agents import worker_lifecycle
from scripts.lib.process_failure import failure_detail
from scripts.agents import codex_mesh
from scripts.direct import phase_reservation, workers
from scripts.direct.worker_state import worker_claim_lock, worker_record_path
from scripts.production import staff, session_staff, worker_admission
from scripts.lib import conf, unit_runs
from scripts.lint import style_review
from scripts.usage.agents_log import LENSES

SCRIPTS = Path(__file__).resolve().parent
PYTHON = str(SCRIPTS / "../lib/py")
MESH = str(SCRIPTS / "../agents/codex_mesh.py")
PROGRESS = str(SCRIPTS / "progress_history.py")
RULES = """
Rules for this dispatch:
Run only commands that finish within one call. Never poll, sleep, call wait,
loop on a status file, or make an empty write_stdin poll. Run long commands
through mesh_runs.py start; then end your turn and receive the result message.
We believe in three gods, Simple, Fast and Beautiful, with correctness as the floor beneath them.
"""


class AssignmentOperation(StrEnum):
    DISPATCH = "dispatch"
    PRIME = "prime"
    FOLLOW = "follow"
    RESIDENT = "resident"
    ADOPT = "adopt"
    RESUME_ACCOUNT_MOVE = "resume-account-move"


RECOVERY_OPERATIONS = (AssignmentOperation.ADOPT, AssignmentOperation.RESUME_ACCOUNT_MOVE)


@dataclass(frozen=True)
class NoHeldAssignment:
    """The assignment uses the normal worker policy."""


@dataclass(frozen=True)
class ReservedHeldCreation:
    """The holder reserved this token before thread creation."""
    token: str


@dataclass(frozen=True)
class ExistingHeldThread:
    """The holder retains this recorded thread identity."""
    thread_id: str


@dataclass(frozen=True)
class HeldAssignment:
    """The design store supplies the identity, claim, and access policy."""
    design: design_roles.HeldDesign
    member: design_roles.RoleInstance
    claim: ReservedHeldCreation | ExistingHeldThread
    access: design_roles.ThreadAccessPolicy


@final
class Arguments(argparse.Namespace):
    role: str = ""
    to: str = ""
    new_worker: str = ""
    model: str = ""
    effort: str = ""
    summary: str = ""
    fix_cause: str = ""
    paths: str = ""
    phase: str = ""
    early_ready: str = ""
    style_since: str = ""
    style_until: str = ""
    session_dir: str = ""
    prompt: str = ""
    cwd: str = ""
    lens: str = ""
    diff: str = ""
    pass_number: int = 1
    image: list[str] = []
    operation: AssignmentOperation = AssignmentOperation.DISPATCH
    held_design: str = ""
    held_role: str = ""
    family: str = ""
    log: str = ""
    id_file: str = ""
    from_session_dir: str = ""


def object_map(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    return worker_lifecycle.read_worker_state(path)


def read_roster(run: Path, name: str) -> dict[str, object]:
    with worker_lifecycle.locked_state(run / "mesh_roster.json", mode=fcntl.LOCK_SH) as state:
        entry = object_map(state.read_object().get(name))
        return entry if entry else {}


def refuse(message: str) -> NoReturn:
    print(f"assign.sh: {message}", file=sys.stderr)
    raise SystemExit(2)


def call(command: list[str], env: dict[str, str], *, capture: bool = False,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, env=env, text=True, capture_output=capture)
    if check and result.returncode:
        if capture:
            print(result.stderr or result.stdout, file=sys.stderr, end="")
        raise SystemExit(2)
    return result


def parse_arguments() -> Arguments:
    parser = argparse.ArgumentParser(prog="assign.sh", allow_abbrev=False)
    _ = parser.add_argument("--role", required=True,
                            choices=("implement", "test", "fix", "review", "research", "propose"))
    _ = parser.add_argument("--operation", type=AssignmentOperation, choices=tuple(AssignmentOperation),
                            default=AssignmentOperation.DISPATCH)
    choice = parser.add_mutually_exclusive_group()
    _ = choice.add_argument("--to", default="")
    _ = choice.add_argument("--new-worker", default="")
    for option in ("model", "effort", "summary", "fix-cause", "paths", "phase", "early-ready", "session-dir",
                   "style-since", "style-until", "held-design", "log", "id-file", "from-session-dir"):
        _ = parser.add_argument("--" + option, default="")
    _ = parser.add_argument("--held-role", choices=("designer", "reviewer"), default="")
    _ = parser.add_argument("--family", choices=("codex", "claude"), default="")
    _ = parser.add_argument("--prompt", default="")
    _ = parser.add_argument("--cwd", default=str(Path.cwd()))
    _ = parser.add_argument("--lens", choices=LENSES, default="")
    _ = parser.add_argument("--diff", default="", help="saved phase diff for the simplify lens")
    _ = parser.add_argument("--pass", dest="pass_number", type=int, default=1)
    _ = parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args(namespace=Arguments())
    if args.role in ("research", "propose"):
        parser.error(f"{args.role} is not available until Phase 6")
    if not args.prompt and args.operation not in RECOVERY_OPERATIONS:
        parser.error("--prompt is required")
    if bool(args.held_design) != bool(args.held_role):
        parser.error("--held-design and --held-role are required together")
    if args.operation in (AssignmentOperation.PRIME, AssignmentOperation.FOLLOW) and not args.held_design:
        parser.error("prime and follow require a held design")
    if args.held_design and args.operation not in (AssignmentOperation.PRIME, AssignmentOperation.FOLLOW):
        parser.error("held designs require prime or follow")
    if args.operation in (AssignmentOperation.PRIME, AssignmentOperation.RESIDENT) and not args.new_worker:
        parser.error("prime and resident require --new-worker")
    if args.operation in (*RECOVERY_OPERATIONS, AssignmentOperation.FOLLOW) and not args.to:
        parser.error("follow and recovery require --to")
    if args.operation == AssignmentOperation.RESIDENT and args.role != "review":
        parser.error("resident requires --role review")
    if args.operation != AssignmentOperation.RESIDENT and (args.family or args.id_file):
        parser.error("--family and --id-file require resident")
    if args.operation == AssignmentOperation.ADOPT and not args.from_session_dir:
        parser.error("adopt requires --from-session-dir")
    if args.from_session_dir and args.operation != AssignmentOperation.ADOPT:
        parser.error("--from-session-dir requires adopt")
    if args.operation in RECOVERY_OPERATIONS and not args.session_dir:
        parser.error("recovery requires --session-dir")
    if args.operation != AssignmentOperation.DISPATCH and (args.lens or args.early_ready or args.style_since):
        parser.error("review stages require the dispatch operation")
    for address in (args.to, args.new_worker):
        if address and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", address):
            parser.error(f"invalid worker address: {address}")
    if args.pass_number < 1:
        parser.error("--pass must be positive")
    if args.early_ready and args.role != "review":
        parser.error("--early-ready requires review")
    if args.style_since and (args.role != "review" or args.early_ready):
        parser.error("--style-since requires a final-diff review")
    if args.style_until and not args.style_since:
        parser.error("--style-until requires --style-since")
    if args.lens == "simplify" and (args.role != "review" or args.early_ready or args.style_since):
        parser.error("simplify requires a broad review of the completed phase diff")
    if args.lens == "simplify" and not args.diff:
        parser.error("--diff is required with --lens simplify")
    if args.diff and args.lens != "simplify":
        parser.error("--diff requires --lens simplify")
    return args


def authorized_paths(args: Arguments, cwd: Path) -> list[str]:
    if args.role == "review":
        if args.paths:
            refuse("--paths is refused for review")
        return []
    if not args.paths or not Path(args.paths).is_file():
        refuse(f"--paths is required for {args.role}")
    paths: list[str] = []
    for line in Path(args.paths).read_text().splitlines():
        value = line.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not (cwd / path).resolve().is_relative_to(cwd):
            refuse(f"invalid authorized path: {value}")
        paths.append(value)
    if not paths:
        refuse("--paths must contain at least one path")
    return paths


def held_assignment(args: Arguments) -> NoHeldAssignment | HeldAssignment:
    if not args.held_design:
        return NoHeldAssignment()
    try:
        design = design_roles.read_design(args.held_design)
    except (OSError, ValueError) as error:
        refuse(str(error))
    if design.placement.host != design_roles.local_host():
        refuse("the held design belongs to another host")
    member = next((member for member in design.roles if member.role == args.held_role), None)
    if member is None or member.name != (args.to or args.new_worker):
        refuse("the held design does not reserve this worker")
    if not isinstance(member.lifecycle, design_roles.RoleNotOpened):
        try:
            record = staff.snapshot()
            owner = design_roles.worker_parent(design, member.role, record)
            caller = worker_admission.parent(record, staff_id=staff.runtime_id())
        except (OSError, ValueError) as error:
            refuse(str(error))
        if owner.id != caller.id:
            refuse("only the current design worker parent may assign a held worker")
    if args.role != ("implement" if member.role == "designer" else "review"):
        refuse("--role differs from the held role")
    if (Path(args.cwd).resolve() != Path(design.repository).resolve()
            or Path(args.session_dir).resolve() != design_roles.mesh_directory(args.held_design).resolve()):
        refuse("the held design requires its recorded worktree and mesh")
    if args.paths:
        refuse("the held design supplies its access policy; --paths is refused")
    claim: ReservedHeldCreation | ExistingHeldThread
    if args.operation == AssignmentOperation.PRIME:
        if not isinstance(member.lifecycle, design_roles.RoleNotOpened) or not member.lifecycle.claim:
            refuse("held thread creation requires its reserved creation claim")
        claim = ReservedHeldCreation(member.lifecycle.claim)
    else:
        if isinstance(member.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded)):
            refuse("the held design has no active thread")
        claim = ExistingHeldThread(member.lifecycle.thread_id)
        if session_staff.worker_session(read_roster(Path(args.session_dir), member.name)) != claim.thread_id:
            refuse("the held thread differs from the mesh identity")
    return HeldAssignment(design, member, claim, design_roles.access_policy(design, member.role))


def recover_arguments(args: Arguments) -> None:
    """Use the recorded worker context for lifecycle recovery."""
    if args.operation not in RECOVERY_OPERATIONS:
        return
    source = Path(args.from_session_dir or args.session_dir).expanduser().resolve()
    record = read_roster(source, args.to)
    role = "impl" if args.role == "implement" else args.role
    session = session_staff.worker_session(record)
    task = codex_mesh.task_role(session, codex_mesh.task_assignments())
    if not session or not isinstance(task, codex_mesh.AssignedWorkerRole) or task.role != role:
        refuse("recovery requires an existing worker with the recorded role")
    if not args.prompt:
        saved = text(record.get("instructions_file"))
        if saved and Path(saved).is_file():
            args.prompt = saved
        else:
            run = Path(args.session_dir).expanduser().resolve()
            run.mkdir(parents=True, exist_ok=True)
            prompt = run / f"{args.to}.assign-recovery.txt"
            _ = prompt.write_text(codex_mesh.ACCOUNT_MOVE_CONTINUATION, encoding="utf-8")
            args.prompt = str(prompt)
    state = read_object(source / "progress_history_state.json")
    if args.cwd == str(Path.cwd()):
        args.cwd = text(record.get("cwd")) or text(state.get("working_dir")) or args.cwd
    if not args.phase:
        args.phase = text(object_map(state.get("phase")).get("id"))


def prepare_run(args: Arguments, cwd: Path, prompt: Path, env: dict[str, str]) -> tuple[Path, str]:
    director = staff.runtime_id()
    session = args.session_dir
    if not session:
        marker = unit_runs.marker(director, os.environ.get("UNIT_ACTIVE_DIR"))
        if marker.is_file():
            session = marker.read_text().strip()
        elif unit_runs.environ("RUN_DIR"):
            session = unit_runs.environ("RUN_DIR")
        else:
            result = call(["bash", str(SCRIPTS / "prepare_session.sh")], env, capture=True)
            session = result.stdout.strip().splitlines()[-1].removeprefix("Session ready at ")
            print(session, flush=True)
    run = Path(session).expanduser().resolve()
    run.mkdir(parents=True, exist_ok=True)
    env["UNIT_RUN_DIR"] = str(run)
    state_path = run / "progress_history_state.json"
    if not state_path.exists():
        _ = call([PYTHON, PROGRESS, "start-run", "--session-dir", str(run), "--working-dir", str(cwd)], env)
    state = read_object(state_path)
    stored_phase = object_map(state.get("phase"))
    phase = args.phase or text(stored_phase.get("id")) or "1"
    if phase != "ad hoc" and (not re.fullmatch(r"[A-Za-z0-9_.-]+", phase) or phase in (".", "..")):
        refuse(f"invalid phase id: {phase}")
    if codex_mesh.finished_run_status(str(run)) or not stored_phase:
        identity = next((match for line in prompt.read_text().splitlines()
                         if (match := re.match(r"^Phase (\S+) — (.*)$", line))), None)
        title = identity.group(2).strip() if identity else text(stored_phase.get("title")) or "Assignment"
        _ = call([PYTHON, PROGRESS, "start-phase", "--session-dir", str(run), "--phase-id", phase,
                  "--phase-title", title], env)
    return run, phase


def resolve_model(args: Arguments, role: str, env: dict[str, str]) -> tuple[str, str, str, str]:
    task = (f"design_roles.{args.held_role}" if args.held_design else
            "ask_a_friend.consultation" if args.operation == AssignmentOperation.RESIDENT else f"direct.{role}")
    result = call(["bash", "-c", '''source "$1"; agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}"
''', "assign", str(SCRIPTS / "../agents/models_config.sh"), task, args.model, args.effort], env, capture=True)
    family, model, effort, tier = result.stdout.strip().splitlines()[-4:]
    return family, model, effort, tier


@dataclass(frozen=True)
class WorkerClaim:
    """The durable worker identity verified under the dispatch lock."""
    identity_path: Path
    slot: str
    model: str
    effort: str


@dataclass
class NoStyleAssignment:
    """This dispatch has no phase style result to capture."""


@dataclass
class StyleAssignment:
    directory: Path
    summary: Path
    captured: bool = False


@dataclass
class Dispatch:
    args: Arguments
    run: Path
    cwd: Path
    prompt: Path
    phase: str
    role: str
    worker: str
    family: str
    model: str
    effort: str
    tier: str
    env: dict[str, str]
    reservation: dict[str, object]
    claim: WorkerClaim | None = None
    pass_open: bool = False
    agent: subprocess.Popen[str] | None = None
    beat: subprocess.Popen[str] | None = None
    stage_thread: threading.Thread | None = None
    stage_cancel: socket.socket | None = None
    stage_errors: list[str] = field(default_factory=list)
    stage_message: str = ""
    started_at: float = field(default_factory=time.time)
    style_assignment: NoStyleAssignment | StyleAssignment = field(default_factory=NoStyleAssignment)

    def capture_style(self, code: int) -> None:
        assignment = self.style_assignment
        if isinstance(assignment, StyleAssignment) and not assignment.captured:
            style_review.finish(assignment.directory, assignment.summary, code)
            assignment.captured = True

    def mesh_call(self, verb: str, *options: str, capture: bool = False,
                  check: bool = True) -> subprocess.CompletedProcess[str]:
        return call([PYTHON, MESH, verb, "--session-dir", str(self.run), "--to", self.worker, *options],
                    self.env, capture=capture, check=check)

    def board(self, slot: str, kind: str, message: str) -> None:
        _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(self.run), slot, kind, message], self.env)

    def claim_follow(self) -> None:
        options = ["--claim-pid", str(os.getpid()), "--role", self.role]
        if self.args.lens:
            options += ["--lens", self.args.lens]
        checked = self.mesh_call("can-follow", *options, capture=True, check=False)
        if checked.returncode:
            refuse(checked.stderr.strip() or f"{self.worker} is busy or not followable")

    def start_pass(self, shown: str) -> None:
        _ = call([PYTHON, PROGRESS, "start-pass", "--session-dir", str(self.run), "--pass-kind", self.role,
                  "--phase-id", self.phase, "--prompt-file", str(self.prompt), "--fix-cause", self.args.fix_cause,
                  "--fix-pass", str(self.args.pass_number if self.role == "fix" else 0), "--activity", shown,
                  "--called-task", f"direct.{self.role}", "--worker-name", self.worker, "--called-family", self.family,
                  "--called-model", self.model, "--called-effort", self.effort or "unset"], self.env)
        self.pass_open = True

    def watch_stage_two(self, shown: str) -> None:
        ready = Path(self.args.early_ready).resolve()
        watch = (codex_mesh.LinuxFileChanges({ready}) if sys.platform.startswith("linux")
                 else codex_mesh.KqueueFileChanges({ready}))
        stage_read, self.stage_cancel = socket.socketpair()
        deadline = time.monotonic() + 86400
        self.stage_message = (f"Stage 2: the final diff is at {str(ready).removesuffix('.ready')}.diff; "
                              "review it per your prompt's Stage 2.")

        def deliver() -> None:
            try:
                while not ready.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("final diff sentinel deadline expired")
                    if watch.wait(stage_read, deadline) == codex_mesh.SocketReady.READY:
                        return
                self.start_pass(shown)
                sent = self.mesh_call("send", "--message", self.stage_message, capture=True, check=False)
                if sent.returncode:
                    self.stage_errors.append(sent.stderr or sent.stdout or "Stage 2 could not be delivered")
            except (OSError, SystemExit) as error:
                self.stage_errors.append(str(error))
            finally:
                watch.close()
                stage_read.close()

        self.stage_thread = threading.Thread(target=deliver, name="final-diff")
        self.stage_thread.start()

    def interrupted(self, signum: int, frame: FrameType | None) -> NoReturn:
        del signum, frame
        if self.agent is not None and self.agent.poll() is None:
            self.agent.terminate()
        raise SystemExit(1)

    def follow(self, command: list[str], shown: str, log: Path) -> int:
        self.agent = subprocess.Popen(command, env=self.env, text=True)
        self.beat = subprocess.Popen(["bash", str(SCRIPTS / "../agents/heartbeat_watch.sh"),
                                     str(self.run / "heartbeat.log"), shown, str(self.agent.pid), str(log), "60"],
                                    env=self.env, text=True)
        try:
            return self.agent.wait()
        finally:
            self.beat.terminate()
            _ = self.beat.wait()
            self.beat = None

    def cleanup(self) -> None:
        if self.agent is not None and self.agent.poll() is None:
            self.agent.terminate()
            _ = self.agent.wait()
        if self.beat is not None:
            self.beat.terminate()
            _ = self.beat.wait()
        if self.stage_cancel is not None:
            try:
                _ = self.stage_cancel.send(b"x")
            except BrokenPipeError:
                pass
            self.stage_cancel.close()
        if self.stage_thread is not None:
            self.stage_thread.join()
        if self.pass_open:
            _ = call([PYTHON, PROGRESS, "finish-pass", "--session-dir", str(self.run), "--status", "interrupted"],
                     self.env, check=False, capture=True)
        if self.claim is not None:
            _ = self.mesh_call("release-follow", "--claim-pid", str(os.getpid()), check=False, capture=True)
            with worker_claim_lock(self.run):
                claim_path = self.run / f"impl_claim_{self.claim.slot}.json"
                if read_object(claim_path).get("pid") == os.getpid():
                    claim_path.unlink(missing_ok=True)
                record = read_roster(self.run, self.worker)
                if session_staff.worker_session(record):
                    identity = read_object(self.claim.identity_path)
                    identity["staff_id"] = record["staff_id"]
                    worker_lifecycle.WorkerStateFile(self.claim.identity_path).publish(json.dumps(identity))
        workers.release_allocation(self.run, self.worker, self.reservation)
        self.capture_style(1)  # An interrupted dispatch never settles an assigned style pass.


def claim_worker(dispatch: Dispatch) -> WorkerClaim:
    """Port the durable identity, stale-claim, role and compaction checks of worker_record claim."""
    with worker_claim_lock(dispatch.run):
        record = read_roster(dispatch.run, dispatch.worker)
        if not record:
            refuse(f"{dispatch.worker}: worker is missing")
        identity_path = worker_record_path(dispatch.run, dispatch.worker, dispatch.worker)
        identity = read_object(identity_path)
        slot = text(identity.get("slot")) or dispatch.worker
        model = text(identity.get("model")) or dispatch.model
        expected = {"staff_id": record.get("staff_id"), "slot": slot, "model": model}
        if not identity:
            worker_lifecycle.WorkerStateFile(identity_path).publish(json.dumps(expected))
        claim_path = dispatch.run / f"impl_claim_{slot}.json"
        previous_claim = read_object(claim_path)
        pid = previous_claim.get("pid")
        stale_claim = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                stale_claim = True
                claim_path.unlink(missing_ok=True)
            else:
                refuse(f"{dispatch.worker} is claimed by launcher {pid}")
        status_path = dispatch.run / f"impl_status_{slot}"
        if stale_claim or (status_path.exists() and status_path.read_text().strip() == "implementing"):
            launcher = record.get("launcher_pid")
            if isinstance(launcher, int) and launcher > 0:
                try:
                    os.kill(launcher, 0)
                except ProcessLookupError:
                    pass
                else:
                    refuse(f"{dispatch.worker} is already implementing")
            _ = status_path.write_text("error\n")
        if record.get("held_design_id"):
            refuse(f"{dispatch.worker}: held roles resume through design_roles.py")
        try:
            threshold = conf.read("direct").whole("UNIT_COMPACT_ABOVE_TOKENS")
        except conf.Unusable as error:
            refuse(str(error))
        context_tokens = record.get("context_tokens")
        if isinstance(context_tokens, int) and context_tokens > threshold:
            compact = dispatch.mesh_call("compact", capture=True, check=False)
            if compact.returncode:
                detail = failure_detail(compact, lines=1).replace("\n", "; ")
                if compact.returncode == 3:
                    dispatch.board(slot, "status", f"launcher: {dispatch.worker} still has a live turn after compaction: {detail}; follow-up not sent")
                    refuse(f"{dispatch.worker} still has a live turn after compaction; end the worker "
                           + f"(codex_mesh.py end --to {dispatch.worker}) and start a fresh one")
                if compact.returncode == codex_mesh.COMPACT_ROSTER_CHANGED:
                    dispatch.board(slot, "status", f"launcher: {detail}; follow-up not sent")
                    refuse(f"{dispatch.worker}: roster changed during compaction; follow-up not sent; "
                           + f"inspect the worker and use codex_mesh.py reopen --to {dispatch.worker} if idle")
                dispatch.board(slot, "status", f"launcher: compaction failed for {dispatch.worker}: {detail}; continuing follow-up")
            else:
                dispatch.board(slot, "status", f"launcher: {compact.stdout.strip() or f'compacted {dispatch.worker}'}")
        if record.get("status") == "ended":
            _ = dispatch.mesh_call("reopen", capture=True)
        dispatch.claim_follow()
        worker_lifecycle.WorkerStateFile(claim_path).publish(json.dumps({"pid": os.getpid(), "worker": dispatch.worker, "turn_id": ""}))
        # Each turn runs at the caller's model:effort (models.conf default or explicit override).
        return WorkerClaim(identity_path, slot, dispatch.model, dispatch.effort)


def stage_two_submitted(receipts: Path, message: str) -> bool:
    """A successful follow must have submitted this dispatch's final-review input."""
    if not receipts.exists():
        return False
    for line in receipts.read_text().splitlines():
        receipt = object_map(cast(object, json.loads(line)))
        if receipt.get("turn_id") and message in cast(list[object], receipt.get("messages", [])):
            return True
    return False


def record_review(dispatch: Dispatch, completed: bool, summary: Path) -> None:
    """Keep each launcher's review evidence, including a recorded zero."""
    if dispatch.role != "review":
        return
    state = object_map(cast(object, json.loads((dispatch.run / "progress_history_state.json").read_text())))
    phase = object_map(state.get("phase"))
    for candidate in cast(list[object], state.get("open_phases", [])):
        item = object_map(candidate)
        if item.get("id") == dispatch.phase:
            phase = item
            break
    record = read_roster(dispatch.run, dispatch.worker)
    raised = len(re.findall(r"^\s*\d+[.)]\s+", summary.read_text(), re.MULTILINE))
    store = workers.ledger_for_run(dispatch.run)
    evidence: dict[str, object] = {
        "arm": "own", "pool": store.path.parent.name, "unit": store.path.stem,
        "phase": dispatch.phase, "phase_instance_id": phase.get("instance_id", ""),
        "history_file": state.get("history_file", ""), "reviewer": dispatch.worker,
        "thread_id": session_staff.worker_session(record), "lens": dispatch.args.lens,
        "kind": "ux" if dispatch.args.lens == "ux" else "broad" if dispatch.args.pass_number == 1 and dispatch.args.lens else "closure",
        "pass": dispatch.args.pass_number, "started_at": int(dispatch.started_at),
        "finished_at": time.time(), "status": "reviewed" if completed else "error",
        "raised": raised, "lease_ordinal": 0, "prime_hash": "",
    }
    root = unit_runs.history_root() / "review_turns" / dispatch.run.name
    root.mkdir(parents=True, exist_ok=True)
    for sequence in itertools.count():
        try:
            output = (root / f"{int(dispatch.started_at)}-{os.getpid()}-{sequence}.json").open("x", encoding="utf-8")
        except FileExistsError:
            continue
        with output:
            json.dump(evidence, output)
            _ = output.write("\n")
        break


def settle_dispatch(dispatch: Dispatch, code: int, completed: bool, shown: str,
                    status: Path, summary: Path, reply: Path) -> int:
    """Completed work wins; otherwise ownership loss precedes refusal or failure."""
    if dispatch.args.early_ready:
        lens_suffix = f"_{dispatch.args.lens}" if dispatch.args.lens else ""
        _ = (status.parent / f"{dispatch.worker}_status{lens_suffix}").write_text("reviewed\n" if completed else "error\n")
    if not completed and code == 0:
        print("assign.sh: Stage 2 was not submitted by this launcher; review incomplete", file=sys.stderr)
        code = 1
    if not summary.read_text().strip() and reply.exists():
        _ = summary.write_text(reply.read_text())
    dispatch.capture_style(0 if completed else code or 1)
    record_review(dispatch, completed, summary)
    if shown not in summary.read_text():
        _ = summary.write_text(shown + "\n\n" + summary.read_text())
    if dispatch.args.summary:
        _ = Path(dispatch.args.summary).write_text(summary.read_text())
    outcome = "completed" if completed else "error"
    _ = status.write_text("done\n" if completed else "error\n")
    if dispatch.pass_open:
        _ = call([PYTHON, PROGRESS, "finish-pass", "--session-dir", str(dispatch.run), "--status", outcome], dispatch.env)
        dispatch.pass_open = False
    if dispatch.role == "fix" and dispatch.env.get("UNIT_RESOLVES_ROUND") != "0":
        ledger = [PYTHON, str(SCRIPTS / "findings.py")]
        phase = ["--session-dir", str(dispatch.run), "--phase", dispatch.phase]
        state = object_map(cast(object, json.loads(call([*ledger, "status", *phase], dispatch.env, capture=True).stdout)))
        if state.get("in_flight"):
            result = ["landed"] if completed else [
                "abandon", "--edits-landed", "--reason", f"the {dispatch.worker} worker exited with code {code}",
            ]
            _ = call([*ledger, *result, *phase], dispatch.env)
    if dispatch.claim is not None:
        dispatch.board(dispatch.claim.slot, "done" if completed else "blocked", f"{shown}: {outcome}; summary {summary}")
    return 0 if completed else 2 if code == 2 else 1


def execute(dispatch: Dispatch, paths: list[str]) -> int:
    try:
        dispatch.claim = claim_worker(dispatch)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    shown = workers.shown_name(dispatch.run, dispatch.worker)
    args = dispatch.args
    phase_dir = phase_reservation.phase_directory(dispatch.run, dispatch.phase)
    phase_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if dispatch.role != "review" else f"_{args.lens}_{args.pass_number}" if args.lens else f"_{args.pass_number}"
    status = phase_dir / f"{dispatch.worker}_status{suffix}"
    summary = phase_dir / f"{dispatch.worker}_summary{suffix}.txt"
    log = phase_dir / f"{dispatch.worker}_agent{suffix}.log"
    reply = phase_dir / f"{dispatch.worker}_reply{suffix}.txt"
    _ = summary.write_text("")
    _ = status.write_text("running\n")
    if args.early_ready:
        lens_suffix = f"_{args.lens}" if args.lens else ""
        _ = (phase_dir / f"{dispatch.worker}_status{lens_suffix}").write_text("reviewing\n")
        _ = (phase_dir / f"{dispatch.worker}_pid{lens_suffix}").write_text(f"{os.getpid()}\n")
    dispatch.model, dispatch.effort = dispatch.claim.model, dispatch.claim.effort
    shown = workers.shown_name(dispatch.run, dispatch.worker)
    with (phase_dir / "dispatch.lock").open("a+") as lock:
        worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_EX)
        numbers = [int(path.stem.split("_")[1]) for path in phase_dir.glob("dispatch_*_paths.json")]
        number = max(numbers, default=0) + 1
        _ = (phase_dir / f"dispatch_{number}_paths.json").write_text(json.dumps(
            {"worker": dispatch.worker, "round": number, "paths": paths}) + "\n")
    message = phase_dir / f"dispatch_{number}_prompt.txt"
    receipts = phase_dir / f"dispatch_{number}_submissions.jsonl"
    output_instruction = ("Return the complete review result in your final reply; the runner saves the artifacts."
                          if dispatch.role == "review" else f"As your last act, write your summary to {summary}.")
    _ = message.write_text(dispatch.prompt.read_text() + "\n" + RULES +
                           f"\nWorker: {shown}\n{output_instruction}\n")
    if args.style_since:
        end = style_review.CommitEnd(args.style_until) if args.style_until else style_review.WORKING_TREE
        try:
            directory = style_review.start(dispatch.cwd, args.style_since, phase_dir, message, f"dispatch-{number}", end)
        except ValueError as error:
            print(f"assign: {error}", file=sys.stderr)
            return settle_dispatch(dispatch, 2, False, shown, status, summary, reply)
        dispatch.style_assignment = StyleAssignment(directory, summary)
        message = directory / "prompt.md"
    dispatch.prompt = message
    dispatch.board(dispatch.claim.slot, "register", f"{shown}; mesh={dispatch.worker}; role={dispatch.role}; status in {status}")
    _ = call(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(dispatch.run / "heartbeat.log"),
              "header", shown, f"{shown}: {dispatch.role}"], dispatch.env, check=False)
    if not args.early_ready:
        dispatch.start_pass(shown)
    follow = [PYTHON, MESH, "follow", "--session-dir", str(dispatch.run), "--to", dispatch.worker,
              "--claim-pid", str(os.getpid()), "--role", dispatch.role, "--cwd", str(dispatch.cwd),
              "--message-file", str(message), "--summary-file", str(summary), "--reply-file", str(reply),
              "--log-file", str(log),
              "--task", f"direct.{dispatch.role}", "--service-tier", dispatch.tier]
    if args.model:
        follow += ["--model", args.model]
    if args.effort:
        follow += ["--effort", args.effort]
    if dispatch.role == "review":
        follow += ["--read-only"]
    if args.lens:
        follow += ["--lens", args.lens]
    for path in args.image:
        follow += ["--image", path]
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        _ = signal.signal(sig, dispatch.interrupted)
    if args.early_ready:
        follow += ["--submission-receipt-file", str(receipts)]
        dispatch.watch_stage_two(shown)
    code = dispatch.follow(follow, shown, log)
    if code == 0 and dispatch.stage_thread is not None:
        # Stage 1 may finish before the sentinel. Keep the dispatch claim while
        # the native file watcher waits, then follow any still-held Stage 2.
        dispatch.stage_thread.join()
        # A successful first follow may already have consumed both stages.
        if stage_two_submitted(receipts, dispatch.stage_message):
            return settle_dispatch(dispatch, code, True, shown, status, summary, reply)
        needs_follow = False
        try:
            with worker_claim_lock(dispatch.run):
                if dispatch.stage_errors:
                    print("assign.sh: " + dispatch.stage_errors[0], file=sys.stderr)
                    code = 1
                else:
                    pending_path = session_staff.worker_inbox_path(dispatch.run, dispatch.worker)
                    with worker_lifecycle.locked_state(pending_path, mode=fcntl.LOCK_SH) as inbox:
                        pending = inbox.read_object()
                    needs_follow = dispatch.stage_message in cast(list[object], pending.get("messages", []))
                    if needs_follow:
                        dispatch.claim_follow()
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 2
            return settle_dispatch(dispatch, code, False, shown, status, summary, reply)
        if needs_follow:
            # send already placed Stage 2 in the inbox; submit no second prompt.
            empty = phase_dir / f"dispatch_{number}_resume.txt"
            _ = empty.write_text("")
            # The previous follow settled provisional findings. The final
            # turn must settle its own summary, including its reply fallback.
            _ = summary.write_text("")
            second_follow = list(follow)
            second_follow[second_follow.index("--message-file") + 1] = str(empty)
            code = dispatch.follow(second_follow, shown, log)
    completed = code == 0 and (not args.early_ready or stage_two_submitted(receipts, dispatch.stage_message))
    return settle_dispatch(dispatch, code, completed, shown, status, summary, reply)


def execute_simplify(args: Arguments, run: Path, phase: str, cwd: Path, source_prompt: Path,
                     env: dict[str, str]) -> int:
    if conf.read("direct").text("SIMPLIFY_LENS") == "off":
        print("simplify: off")
        return 0
    if args.pass_number != 1:
        print("simplify: skipped; broad review only")
        return 0
    command = ["bash", str(SCRIPTS / "review.sh"), "--phase", phase, "--diff", args.diff,
               "--summary", str(Path(args.summary).resolve()) if args.summary else "", str(run), str(cwd),
               str(source_prompt), "simplify", "simplify review", "reviewing the completed phase diff", "1", "simplify"]
    return subprocess.run(command, env=env, check=False).returncode


def require_live_director() -> None:
    """Refuse launchers without a live Claude or Codex ancestor."""
    try:
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=,stat=,comm="],
                                    capture_output=True, text=True, check=True)
            parent, state, command = result.stdout.strip().split(None, 2)
            if state.startswith("Z"):
                break
            if Path(command).name in {"claude", "codex"}:
                os.kill(pid, 0)
                return
            pid = int(parent)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    refuse("assign.sh requires a live Claude or Codex ancestor; run it from the director's session shell")


def execute_claude(args: Arguments, run: Path, phase: str, cwd: Path, prompt: Path,
                   env: dict[str, str]) -> int:
    """Use the Claude launcher under the same assign parent."""
    options = ["--phase", phase]
    if args.to:
        options += ["--to", args.to]
    role = "impl" if args.role == "implement" else args.role
    slot = args.to or args.new_worker or role
    if args.role == "review":
        for flag, value in (("--style-since", args.style_since),
                            ("--style-until", args.style_until), ("--summary", args.summary)):
            if value:
                options += [flag, value]
        command = ["bash", str(SCRIPTS / "review.sh"), *options, str(run), str(cwd), str(prompt),
                   "review", "review", "review", str(args.pass_number), args.lens, args.early_ready]
    else:
        if args.fix_cause:
            options += ["--fix-cause", args.fix_cause]
        command = ["bash", str(SCRIPTS / "implement.sh"), *options, str(run), str(cwd), str(prompt),
                   role, role, role, role, str(args.pass_number), slot]
    result = subprocess.run(command, env=env, check=False).returncode
    if args.summary and args.role != "review":
        artifact = run / "phases" / phase / f"impl_summary_{slot}.txt"
        if artifact.is_file():
            _ = Path(args.summary).write_text(artifact.read_text())
    return result


def admit_assignment(worker: str, run: Path, role: str,
                     held: NoHeldAssignment | HeldAssignment) -> worker_admission.WorkerStartAllowed:
    """Apply the same admission rule to every assignment operation."""
    if isinstance(held, HeldAssignment):
        lifecycle = held.member.lifecycle
        return worker_admission.admit(
            worker, run, role=held.member.role,
            reference=str(lifecycle.staff_id) if isinstance(lifecycle, design_roles.HeldWorker) else "")
    return worker_admission.admit(worker, run, role=role)


def execute_operation(args: Arguments, admitted: worker_admission.WorkerStartAllowed,
                      held: NoHeldAssignment | HeldAssignment, cwd: Path, prompt: Path,
                      family: str, model: str, effort: str, tier: str, env: dict[str, str]) -> int:
    """Translate a checked assignment into its backend operation."""
    run, worker = admitted.run, admitted.name
    summary = str(Path(args.summary).resolve()) if args.summary else str(run / f"{worker}.reply.txt")
    log = str(Path(args.log).resolve()) if args.log else str(run / f"{worker}.log")
    if family == "claude":
        if args.operation != AssignmentOperation.RESIDENT:
            refuse("this operation requires a Codex worker")
        env.update(AGENT_BG_ROLE="review", AGENT_BG_DETACH="1", AGENT_BG_EFFORT=effort)
        return call(["bash", str(SCRIPTS / "../agents/agent_bg.sh"), "--run", str(run), worker, str(cwd), str(prompt), summary, log,
                     str(Path(args.id_file).resolve()) if args.id_file else str(run / "friend_id"), model],
                    env, check=False).returncode
    command = [PYTHON, MESH]
    context = ["--session-dir", str(run)]
    settings = ["--model", model, "--effort", effort, "--service-tier", tier]
    role = "impl" if args.role == "implement" else args.role
    if args.operation == AssignmentOperation.PRIME:
        if not isinstance(held, HeldAssignment) or not isinstance(held.claim, ReservedHeldCreation):
            refuse("prime requires the holder's reserved creation claim")
        sandbox = "read-only" if isinstance(held.access, design_roles.ReadOnlyReviewer) else held.access.launch_sandbox
        command += ["prime", *context, "--name", worker, "--cwd", str(cwd), "--instructions-file", str(prompt),
                    *settings, "--sandbox", sandbox, "--held-design", held.design.placement.design_id,
                    "--held-role", held.member.role]
    elif args.operation == AssignmentOperation.FOLLOW:
        if not isinstance(held, HeldAssignment) or not isinstance(held.claim, ExistingHeldThread):
            refuse("follow requires the holder's existing thread identity")
        command += ["follow", *context, "--to", worker, "--role", role, "--cwd", str(cwd),
                    "--message-file", str(prompt), "--summary-file", summary, "--log-file", log, *settings]
        if isinstance(held.access, design_roles.ReadOnlyReviewer):
            command += ["--read-only"]
    elif args.operation == AssignmentOperation.RESIDENT:
        command += ["start", "--resident", *context, "--name", worker, "--role", role,
                    "--cwd", str(cwd), "--prompt-file", str(prompt), "--summary-file", summary,
                    "--log-file", log, "--task", "ask_a_friend.consultation", *settings]
    elif args.operation == AssignmentOperation.ADOPT:
        command += ["adopt", *context, "--name", worker, "--from", str(Path(args.from_session_dir).resolve())]
    elif args.operation == AssignmentOperation.RESUME_ACCOUNT_MOVE:
        command += ["resume-account-move", *context, "--to", worker]
    else:
        refuse("the dispatch operation requires the normal worker coordinator")
    return call(command, env, check=False).returncode


def main() -> int:
    require_live_director()
    args = parse_arguments()
    recover_arguments(args)
    held = held_assignment(args)
    cwd, prompt = Path(args.cwd).expanduser().resolve(), Path(args.prompt).expanduser().resolve()
    if not cwd.is_dir() or not prompt.is_file():
        refuse("--cwd must be a directory and --prompt must be a readable file")
    paths = [] if isinstance(held, HeldAssignment) or args.operation in RECOVERY_OPERATIONS else authorized_paths(args, cwd)
    env = dict(os.environ)
    run, phase = prepare_run(args, cwd, prompt, env)
    if args.lens == "simplify":
        return execute_simplify(args, run, phase, cwd, prompt, env)
    role = "impl" if args.role == "implement" else args.role
    check = [PYTHON, PROGRESS, "check-launch", "--session-dir", str(run), "--pass-kind", role,
             "--phase-id", phase, "--prompt-file", str(prompt), "--fix-cause", args.fix_cause]
    if args.lens:
        check += ["--lens", args.lens]
    checked = call(check, env, check=False)
    if checked.returncode:
        return 3 if checked.returncode == 3 else 2
    family, model, effort, tier = (("codex", "", "", "") if args.operation in RECOVERY_OPERATIONS
                                   else resolve_model(args, role, env))
    if args.family and args.family != family:
        refuse("the resident family differs from the caller's configured family")
    if isinstance(held, HeldAssignment) and family != "codex":
        refuse("held designs require Codex")
    worker = args.to
    reservation: dict[str, object] = {}
    if args.operation != AssignmentOperation.DISPATCH or family == "claude":
        worker = args.to or args.new_worker or role
    elif not worker:
        command = [PYTHON, str(SCRIPTS / "workers.py"), "resolve", "--session-dir", str(run), "--role", role,
                   "--owner-pid", str(os.getpid()), "--working-dir", str(cwd)]
        if args.model or args.effort:
            command += ["--model", model, "--effort", effort]
        if args.new_worker:
            command += ["--new-worker", args.new_worker]
        resolved = call(command, env, capture=True, check=False)
        if resolved.returncode:
            print(resolved.stderr or resolved.stdout, file=sys.stderr, end="")
            return 3
        worker = resolved.stdout.strip()
        reservation = workers.allocation_for_follow(run, worker)
    try:
        admitted = admit_assignment(worker, run, role, held)
    except ValueError as error:
        workers.release_allocation(run, worker, reservation)
        print(str(error), file=sys.stderr)
        return 2
    env["UNIT_TEAM_ROLE"] = worker
    env["UNIT_PASS_OWNER"] = "launcher"
    if args.operation != AssignmentOperation.DISPATCH:
        return execute_operation(args, admitted, held, cwd, prompt, family, model, effort, tier, env)
    if family == "claude":
        return execute_claude(args, run, phase, cwd, prompt, env)
    dispatch = Dispatch(args, run, cwd, prompt, phase, role, worker, family, model, effort, tier, env, reservation)
    try:
        return execute(dispatch, paths)
    finally:
        dispatch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
