#!/usr/bin/env python3
"""Claim a unit worker and watch one named dispatch through completion."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Literal, NoReturn, cast, final, overload

from scripts.astra import astra_roles
from scripts.agents import worker_lifecycle
from scripts.lib.process_failure import failure_detail
from scripts.agents import codex_mesh
from scripts.direct import assign_claude, phase_reservation, progress_history, simplify_daily, workers, worker_state
from scripts.direct.worker_state import ROSTER_LIVE, worker_claim_lock, worker_record_path, worker_status_path
from scripts.production import staff, session_staff, worker_admission
from scripts.lib import conf, models_registry, unit_runs
from scripts.herdr import herdr, side_pane, worker_panes
from scripts.production import worker as staff_worker
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import style_review
from scripts.usage.agents_log import PHASE_REVIEW_LENSES

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
    ADOPT = "adopt"
    RESUME_ACCOUNT_MOVE = "resume-account-move"
    ASTRA_REVIEW = "astra-review"


RECOVERY_OPERATIONS = (AssignmentOperation.ADOPT, AssignmentOperation.RESUME_ACCOUNT_MOVE)


@final
class Arguments(argparse.Namespace):
    role: str = ""
    target: str = "staff-worker"
    policy: str = ""
    parent: str = ""
    to: str = ""
    new_worker: str = ""
    model: str = ""
    effort: str = ""
    summary: str = ""
    log: str = ""
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
    pass_number: int = 1
    image: list[str] = []
    operation: AssignmentOperation = AssignmentOperation.DISPATCH
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
                            choices=("implement", "test", "fix", "review", "research", "propose", "simplify"))
    _ = parser.add_argument("--operation", type=AssignmentOperation, choices=tuple(AssignmentOperation),
                            default=AssignmentOperation.DISPATCH)
    _ = parser.add_argument("--target", choices=("staff-worker", "consultation-pane", "durable-design-role"), default="staff-worker")
    _ = parser.add_argument("--policy", choices=("friend", "astra-reviewer", "astra-designer"), default="")
    _ = parser.add_argument("--parent", default="")
    choice = parser.add_mutually_exclusive_group()
    _ = choice.add_argument("--to", default="")
    _ = choice.add_argument("--new-worker", default="")
    for option in ("model", "effort", "summary", "log", "fix-cause", "paths", "phase", "early-ready", "session-dir",
                   "style-since", "style-until", "from-session-dir"):
        _ = parser.add_argument("--" + option, default="")
    _ = parser.add_argument("--prompt", default="")
    _ = parser.add_argument("--cwd", default=str(Path.cwd()))
    _ = parser.add_argument("--lens", choices=PHASE_REVIEW_LENSES, default="")
    _ = parser.add_argument("--pass", dest="pass_number", type=int, default=1)
    _ = parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args(namespace=Arguments())
    if args.role == "simplify":
        if not args.session_dir:
            parser.error("simplify requires --session-dir")
        return args
    if args.role in ("research", "propose"):
        parser.error(f"{args.role} is not available until Phase 6")
    if args.target != "staff-worker" and not args.policy:
        parser.error("--policy is required for this target")
    if args.target == "consultation-pane" and not args.new_worker:
        parser.error("consultation-pane requires --new-worker")
    if args.target == "durable-design-role" and (not args.parent or args.policy == "friend"):
        parser.error("durable-design-role requires --parent and an Astra policy")
    if not args.prompt and args.operation not in RECOVERY_OPERATIONS and args.target == "staff-worker":
        parser.error("--prompt is required")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.new_worker:
        parser.error("astra-review requires --new-worker")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and args.role != "review":
        parser.error("astra-review requires --role review")
    if args.operation in RECOVERY_OPERATIONS and not args.to:
        parser.error("recovery requires --to")
    if args.operation == AssignmentOperation.ADOPT and not args.from_session_dir:
        parser.error("adopt requires --from-session-dir")
    if args.from_session_dir and args.operation != AssignmentOperation.ADOPT:
        parser.error("--from-session-dir requires adopt")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.prompt:
        parser.error("astra-review requires --prompt")
    if args.operation in (*RECOVERY_OPERATIONS, AssignmentOperation.ASTRA_REVIEW) and args.target != "consultation-pane" and not args.session_dir:
        parser.error("recovery and astra-review require --session-dir")
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
    task = role if "." in role else f"direct.{role}"
    result = call(["bash", "-c", '''source "$1"; models_launch_configuration || exit 2; agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}" "$MODELS_LAUNCH_CONFIGURATION"
''', "assign", str(SCRIPTS / "../agents/models_config.sh"), task, args.model, args.effort], env, capture=True)
    family, model, effort, tier, configuration = result.stdout.strip().splitlines()[-5:]
    env["MODELS_LAUNCH_CONFIGURATION"] = configuration
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
    switching: staff.StaffRow | None = None
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
                  "--called-task", f"direct.{self.role}", "--worker-name", self.worker,
                  "--worker-id", self.switching.id if self.switching else text(read_roster(self.run, self.worker)["staff_id"]),
                  "--called-family", self.family,
                  "--called-model", self.model, "--called-effort", self.effort or "unset",
                  "--launcher-command", json.dumps(["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]])], self.env)
        self.pass_open = True

    def finish_pass_command(self, status: str) -> list[str]:
        return [PYTHON, PROGRESS, "finish-pass", "--session-dir", str(self.run),
                "--status", status]

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
            _ = call(self.finish_pass_command("interrupted"), self.env, check=False, capture=True)
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
        owner = worker_state.read_worker_claim(claim_path)
        if isinstance(owner, worker_state.HeldWorkerClaim):
            refuse(f"{dispatch.worker} is claimed by launcher {owner.pid}")
        stale_claim = isinstance(owner, worker_state.EndedWorkerClaim)
        claim_path.unlink(missing_ok=True)
        status_path = worker_status_path(dispatch.run, slot)
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
        if record.get("status") == "ended" or (
                record.get("status") in ROSTER_LIVE | {codex_mesh.WAITING_CAPACITY}
                and not codex_mesh.worker_launcher_attached(record)):
            _ = dispatch.mesh_call("reopen", capture=True)
        dispatch.claim_follow()
        worker_state.publish_worker_claim(claim_path, dispatch.worker)
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


def recorded_phase(run: Path, phase_id: str) -> tuple[dict[str, object], dict[str, object]]:
    state = read_object(run / "progress_history_state.json")
    phase = next((object_map(candidate) for candidate in cast(list[object], state.get("open_phases", []))
                  if object_map(candidate).get("id") == phase_id), object_map(state.get("phase")))
    return state, phase


def record_review(dispatch: Dispatch, completed: bool, summary: Path) -> None:
    """Keep each launcher's review evidence, including a recorded zero."""
    if dispatch.role != "review":
        return
    state, phase = recorded_phase(dispatch.run, dispatch.phase)
    record = read_roster(dispatch.run, dispatch.worker)
    raised = len(re.findall(r"^\s*\d+[.)]\s+", summary.read_text(), re.MULTILINE))
    store = workers.ledger_for_run(dispatch.run)
    progress_history.write_review_turn(dispatch.run, {
        "unit": store.path.stem, "phase": dispatch.phase,
        "phase_instance_id": phase.get("instance_id", ""), "history_file": state.get("history_file", ""),
    }, reviewer=dispatch.worker, thread_id=session_staff.worker_session(record), lens=dispatch.args.lens,
       pass_number=dispatch.args.pass_number, started_at=int(dispatch.started_at),
       status="reviewed" if completed else "error", raised=raised,
       details={"pool": store.path.parent.name, "lease_ordinal": 0, "prime_hash": ""})


def finish_repair(dispatch: Dispatch, code: int) -> bool:
    ledger = [PYTHON, str(SCRIPTS / "findings.py"), "landed" if code == 0 else "abandon",
              "--session-dir", str(dispatch.run), "--phase", dispatch.phase]
    if code:
        ledger += ["--edits-landed", "--reason", f"the fix worker exited with code {code}"]
    failed = call(ledger, dispatch.env, check=False).returncode != 0
    commands = [ledger]
    if dispatch.pass_open:
        commands.append(dispatch.finish_pass_command("error" if code or failed else "completed"))
        failed = call(commands[-1], dispatch.env, check=False).returncode != 0 or failed
    if failed:
        print("assign.sh: repair resolution failed; run these commands:", file=sys.stderr)
        for command in commands:
            print(shlex.join(["env", f"UNIT_TEAM_ROLE={dispatch.worker}", "UNIT_PASS_OWNER=launcher", *command]), file=sys.stderr)
    dispatch.pass_open = False
    return not failed


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
    if dispatch.role == "fix" and dispatch.env.get("UNIT_RESOLVES_ROUND") == "1":
        if not finish_repair(dispatch, 0 if completed else code or 1):
            completed, code, outcome = False, 1, "error"
    elif dispatch.pass_open:
        _ = call(dispatch.finish_pass_command(outcome), dispatch.env)
        dispatch.pass_open = False
    _ = status.write_text("done\n" if completed else "error\n")
    if dispatch.claim is not None:
        dispatch.board(dispatch.claim.slot, "done" if completed else "blocked", f"{shown}: {outcome}; summary {summary}")
    return 0 if completed else 2 if code == 2 else 1


def image_note(images: list[str]) -> str:
    """Name each attached image for a worker that cannot receive it as an option."""
    if not images:
        return ""
    return "\nRead these images:\n" + "\n".join(str(Path(image).resolve()) for image in images)


def execute(dispatch: Dispatch, paths: list[str]) -> int:
    try:
        dispatch.claim = (WorkerClaim(worker_record_path(dispatch.run, dispatch.worker, dispatch.worker), dispatch.worker,
                                      dispatch.model, dispatch.effort)
                          if dispatch.switching else claim_worker(dispatch))
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
    assignment = dispatch.prompt
    message = phase_dir / f"dispatch_{number}_prompt.txt"
    receipts = phase_dir / f"dispatch_{number}_submissions.jsonl"
    output_instruction = ("Return the complete review result in your final reply; the runner saves the artifacts."
                          if dispatch.role == "review" else f"As your last act, write your summary to {summary}.")
    # `start` has no image option, so a switched worker opens each image by its path.
    _ = message.write_text(dispatch.prompt.read_text() + "\n" + RULES +
                           f"\nWorker: {shown}\n{output_instruction}\n" +
                           (image_note(args.image) if dispatch.switching else ""))
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
    launch = follow if dispatch.switching is None else mesh_command(
        args, dispatch.run, dispatch.worker, SwitchStart(
            dispatch.cwd, message, assignment, str(summary), reply, str(log),
            dispatch.model, dispatch.effort, dispatch.tier))
    code = dispatch.follow(launch, shown, log)
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


def execute_herdr(args: Arguments, run: Path, phase: str, prompt: Path,
                  role: str, paths: list[str], env: dict[str, str]) -> int:
    """Send the assignment through the shared worker command."""
    directory = phase_reservation.phase_directory(run, phase)
    directory.mkdir(parents=True, exist_ok=True)
    slot = args.to or args.new_worker or role
    suffix = f"_{args.lens}_{args.pass_number}" if args.lens else f"_{args.pass_number}" if role == "review" else ""
    summary = directory / f"{slot}_summary{suffix}.txt"
    status = directory / f"{slot}_status{suffix}"
    message = directory / f"{slot}_prompt{suffix}.txt"
    reply = directory / f"{slot}_reply{suffix}.txt"
    log = directory / f"{slot}_agent{suffix}.log"
    content = prompt.read_text() + "\n" + RULES + "\nAuthorized paths:\n" + "\n".join(paths)
    content += image_note(args.image)
    if not message.is_file() or message.read_text() != content:
        write_text_atomic(message, content)
    end = style_review.CommitEnd(args.style_until) if args.style_until else style_review.WORKING_TREE
    style: NoStyleAssignment | StyleAssignment = NoStyleAssignment()
    if args.style_since:
        style = StyleAssignment(style_review.start(Path(args.cwd), args.style_since, directory, message, slot, end), summary)
        message = style.directory / "prompt.md"
    env["UNIT_RUN_DIR"] = str(run)
    env["UNIT_TEAM_ROLE"] = slot
    command = [PYTHON, str(SCRIPTS / "../production/worker.py"), "assign", role, str(message),
               "--session-dir", str(run), "--phase", phase, "--slot", slot, "--kind", "assign", "--reply", str(reply),
               "--lens", args.lens, "--fix-cause", args.fix_cause,
               "--fix-pass", str(args.pass_number if role == "fix" else 0)]
    if args.to or args.new_worker:
        command += ["--name", args.to or args.new_worker]
    if args.early_ready:
        command += ["--early-ready", str(Path(args.early_ready).resolve())]
    command += ["--launcher-command", json.dumps(["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]])]
    write_text_atomic(status, "running\n")
    _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(run), slot, "register",
              f"role={role}; transport=herdr; status in {status}"], env)
    _ = call(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(run / "heartbeat.log"),
              "header", slot, role], env, check=False)
    with log.open("a") as output:
        result = subprocess.run(command, env=env, text=True, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode == 0:
        env["UNIT_PASS_OWNER"] = "launcher"
        _ = call([PYTHON, PROGRESS, "finish-pass", "--session-dir", str(run), "--status", "completed"], env)
        write_text_atomic(summary, reply.read_text())
        if args.summary:
            write_text_atomic(Path(args.summary), summary.read_text())
    write_text_atomic(status, "done\n" if result.returncode == 0 else "error\n")
    if isinstance(style, StyleAssignment):
        style_review.finish(style.directory, style.summary, result.returncode)
    _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(run), slot,
              "done" if result.returncode == 0 else "blocked",
              f"role={role}; exit {result.returncode}; summary {summary}"], env)
    return result.returncode



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
                   env: dict[str, str], model: str, effort: str) -> int:
    """Run the Claude pass inside the assign process."""
    options = ["--phase", phase]
    role = "impl" if args.role == "implement" else args.role
    slot = args.to or args.new_worker or role
    if args.role == "review":
        for flag, value in (("--style-since", args.style_since),
                            ("--style-until", args.style_until), ("--summary", args.summary)):
            if value:
                options += [flag, value]
        return subprocess.run(["bash", str(SCRIPTS / "review.sh"), *options, str(run), str(cwd), str(prompt),
                   "review", "review", "review", str(args.pass_number), args.lens, args.early_ready],
                   env=env, check=False).returncode
    else:
        worker = assign_claude.ClaudeFollowUp(args.to) if args.to else assign_claude.NewClaudeWorker()
        result = assign_claude.run(assign_claude.ClaudeLaunch(run, phase, cwd, prompt, role, slot,
            args.fix_cause, args.pass_number, model, effort, worker,
            ("bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]), env))
    if args.summary:
        artifact = run / "phases" / phase / f"impl_summary_{slot}.txt"
        if artifact.is_file():
            _ = Path(args.summary).write_text(artifact.read_text())
    return result


@dataclass(frozen=True)
class SwitchStart:
    """The values that only the start of a family switch reads."""
    cwd: Path
    prompt: Path
    assignment: Path
    summary: str
    reply: Path
    log: str
    model: str
    effort: str
    tier: str


def mesh_command(args: Arguments, run: Path, worker: str, switch: SwitchStart | None = None) -> list[str]:
    """Build the one codex_mesh command of an admitted assignment."""
    command = [PYTHON, MESH]
    context = ["--session-dir", str(run)]
    if switch is not None:
        role = "impl" if args.role == "implement" else args.role
        command += ["start", *context, "--name", worker, "--role", role, "--cwd", str(switch.cwd),
                    "--prompt-file", str(switch.prompt), "--assignment-file", str(switch.assignment),
                    "--summary-file", switch.summary, "--reply-file", str(switch.reply), "--log-file", switch.log,
                    "--task", f"direct.{role}", "--model", switch.model, "--effort", switch.effort,
                    "--service-tier", switch.tier]
        if role == "review":
            command += ["--sandbox", "read-only"]
        if args.lens:
            command += ["--lens", args.lens]
    elif args.operation == AssignmentOperation.ADOPT:
        command += ["adopt", *context, "--name", worker, "--from", str(Path(args.from_session_dir).resolve())]
    else:
        command += ["resume-account-move", *context, "--to", worker]
    return command


def execute_operation(args: Arguments, admitted: worker_admission.WorkerStartAllowed,
                      env: dict[str, str]) -> int:
    """Translate an admitted assignment into its recovery operation."""
    return call(mesh_command(args, admitted.run, admitted.name), env, check=False).returncode


def pane_policy(name: str, *, model: str = "", effort: str = "") -> worker_panes.LaunchPolicy:
    """Resolve one explicit launch policy and its model overrides."""
    try:
        if name == "friend":
            task = "ask_a_friend.consultation"
            policy = worker_panes.worker_policy(models_registry.launch_settings("ask_a_friend", "consultation"))
        else:
            if name not in ("astra-reviewer", "astra-designer"):
                refuse(f"unknown pane policy: {name}")
            task = "design_roles." + name.removeprefix("astra-")
            policy = worker_panes.launch_policy(cast(staff.WorkerRole, name))
        if model or effort:
            family, selected_model, selected_effort, tier = resolve_model(
                Arguments(model=model, effort=effort), task, dict(os.environ))
            settings = models_registry.LaunchSettings(cast(Literal["claude", "codex"], family),
                selected_model, selected_effort, tier)
            policy = replace(policy, settings=settings)
        return policy
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        refuse(f"{worker_panes.StartRefusal.INVALID_ROLE_CONFIGURATION.value}: {error}")


@overload
def launch(target: worker_panes.ConsultationPane, policy: worker_panes.LaunchPolicy) -> side_pane.SidePane: ...


@overload
def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
           policy: worker_panes.LaunchPolicy) -> worker_panes.WorkerStart: ...


def launch(target: worker_panes.PaneTarget, policy: worker_panes.LaunchPolicy) -> side_pane.SidePane | worker_panes.WorkerStart:
    """Start the named target with the supplied policy."""
    if isinstance(target, worker_panes.ConsultationPane):
        return side_pane.open(target.label, policy, question=target.question, cwd=target.cwd, delivery=target.delivery)
    return staff_worker.launch(target, policy)


def execute_consultation(args: Arguments, cwd: Path) -> int:
    """Start a consultation without a staff row."""
    opened: side_pane.AbsentSidePane | side_pane.OpenSidePane = side_pane.AbsentSidePane()
    result = 1
    try:
        question = ""
        if args.prompt:
            prompt = Path(args.prompt).expanduser().resolve()
            question = f"Read {prompt}.\n{prompt.read_text()}"
        review = args.operation == AssignmentOperation.ASTRA_REVIEW
        policy = (worker_panes.astra_review_policy(args.model, args.effort) if review else
                  pane_policy(args.policy, model=args.model, effort=args.effort))
        delivery = worker_panes.TranscriptReply() if review else worker_panes.ReplyToCaller()
        current = launch(worker_panes.ConsultationPane(args.new_worker, cwd, question, delivery), policy)
        if isinstance(current, side_pane.UnknownSidePane):
            return worker_panes.ExitCode.UNCERTAIN
        if not isinstance(current, side_pane.OpenSidePane):
            raise side_pane.SidePaneRefused("consultation is not running")
        if args.operation != AssignmentOperation.ASTRA_REVIEW:
            print("done")
            return 0
        opened = current
        client = herdr.Herdr(herdr.shared_session())
        process = client.pane_agent_process(current.pane, current.terminal)
        source = herdr.codex_transcript(process.pid)
        reply = staff_worker.consultation_reply(client, current.pane, source, question)
        write_text_atomic(Path(args.summary), reply + "\n")
        result = 0
    except side_pane.SidePaneRefused as error:
        print(str(error), file=sys.stderr)
        result = worker_panes.ExitCode.REFUSED
    except (herdr.HerdrFailure, conf.Unusable, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"assign.sh: consultation unavailable: {error}", file=sys.stderr)
        result = worker_panes.ExitCode.UNCERTAIN
    finally:
        if isinstance(opened, side_pane.OpenSidePane):
            try:
                closed = side_pane.close(args.new_worker)
                if isinstance(closed, side_pane.SidePaneCloseUnavailable):
                    print(f"assign.sh: consultation close unproved: {closed.reason}; "
                          + f"pane {opened.pane} ({args.new_worker}) stays open", file=sys.stderr)
                    result = 1
            except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"assign.sh: consultation close failed: {error}", file=sys.stderr)
                result = 1
    return result


def execute_durable(args: Arguments, env: dict[str, str]) -> int:
    """Start or dispatch a durable role through its explicit target."""
    name = staff.WorkerName.parse(args.to or args.new_worker)
    role = cast(staff.WorkerRole, args.policy)
    target = worker_panes.DurableDesignRole(staff.StaffId(args.parent), name, role)
    policy = pane_policy(args.policy, model=args.model, effort=args.effort)
    if not args.prompt:
        current = launch(target, policy)
        if not isinstance(current, worker_panes.OpenWorker):
            return staff_worker.startup_result(name, current)
        print(json.dumps(current.row.to_json()))
        return 0
    prompt = Path(args.prompt).expanduser().resolve()
    if not prompt.is_file() or not args.session_dir or not args.phase:
        refuse("durable dispatch requires --prompt, --session-dir, and --phase")
    result = staff_worker.dispatch(target.parent, name, role, prompt, session_dir=Path(args.session_dir),
        slot=name.value, phase=args.phase, kind="astra", reply=Path(args.summary), policy=policy,
        launcher_command=["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]])
    if result:
        return result
    env.update(UNIT_TEAM_ROLE=name.value, UNIT_PASS_OWNER="launcher")
    return call([PYTHON, PROGRESS, "finish-pass", "--session-dir", args.session_dir,
                 "--status", "completed"], env, check=False).returncode


def execute_astra_review_herdr(args: Arguments, run: Path, cwd: Path, prompt: Path,
                              env: dict[str, str]) -> int:
    """Hold one pane reviewer until the worker command confirms its end."""
    for arguments in (["start-run", "--session-dir", str(run), "--working-dir", str(cwd)],
                      ["start-phase", "--session-dir", str(run), "--phase-id", "astra-review",
                       "--phase-title", "Astra review"]):
        recorded = subprocess.run([PYTHON, PROGRESS, *arguments], env=env, text=True,
                                  stderr=subprocess.STDOUT, check=False)
        if recorded.returncode:
            return recorded.returncode
    command = [PYTHON, str(SCRIPTS / "../production/worker.py")]
    parent = worker_admission.parent(staff.snapshot(), run=run)
    target = worker_panes.StaffWorker(parent.id, staff.WorkerName.parse(args.new_worker), "astra-reviewer")
    policy = pane_policy(args.policy or "astra-reviewer", model=args.model, effort=args.effort)
    current = launch(target, policy)
    if not isinstance(current, worker_panes.OpenWorker):
        return staff_worker.startup_result(target.name, current)
    held, result = "", 1
    try:
        role = astra_roles.hold("reviewer", astra_roles.ExecProcess(os.getpid()), str(current.row.id))
        if isinstance(role, astra_roles.HoldWorkerUnavailable):
            return 1
        held = role["staff_id"]
        result = staff_worker.dispatch(target.parent, target.name, target.role, prompt,
            session_dir=run, slot=args.new_worker, phase="astra-review", kind="astra",
            reply=Path(args.summary), policy=policy, started=current,
            launcher_command=["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]])
        env.update(UNIT_PASS_OWNER="launcher", UNIT_TEAM_ROLE=args.new_worker)
        finished = subprocess.run([PYTHON, PROGRESS, "finish-pass", "--session-dir", str(run),
                       "--status", "error" if result else "completed"], env=env, text=True,
                       stderr=subprocess.STDOUT, check=False).returncode
        if result:
            print(f"assign.sh: reviewer dispatch exited {result}; pending input is retained", flush=True)
        else:
            result = finished
    except (OSError, ValueError) as error:
        print(f"assign.sh: reviewer role unavailable: {error}", file=sys.stderr)
        result = 1
    finally:
        ended = subprocess.run([*command, "end", args.new_worker, "--session-dir", str(run)],
                               env=env, text=True, stderr=subprocess.STDOUT, check=False).returncode
        if ended:
            result = 1
        elif held:
            astra_roles.release(held)
    return result


def execute_astra_review(args: Arguments, cwd: Path, prompt: Path, env: dict[str, str]) -> int:
    """Publish, hold, review, and close one managed Astra worker."""
    run = Path(args.session_dir).expanduser().resolve()
    if unit_runs.worker_transport(run) == "herdr":
        return execute_astra_review_herdr(args, run, cwd, prompt, env)
    context = ["--session-dir", str(run)]
    settings = ["--model", args.model, "--effort", args.effort, "--cwd", str(cwd)]
    opened, held, result = False, "", 1
    try:
        result = call([PYTHON, MESH, "prime", *context, "--name", args.new_worker,
                       "--instructions-file", str(prompt),
                       "--sandbox", "read-only", *settings], env, check=False).returncode
        if result:
            return result
        opened = True
        role = astra_roles.hold("reviewer", astra_roles.MeshWorker(str(run), args.new_worker))
        if isinstance(role, astra_roles.HoldWorkerUnavailable):
            return 1
        held = role["staff_id"]
        result = call([PYTHON, MESH, "follow", *context, "--to", args.new_worker, "--role", "review",
                       "--message-file", str(prompt), "--summary-file", args.summary,
                       "--log-file", args.log, "--read-only", *settings], env, check=False).returncode
    except (OSError, ValueError) as error:
        print(f"assign.sh: reviewer role unavailable: {error}", file=sys.stderr)
        result = 1
    finally:
        if opened:
            ended = call([PYTHON, MESH, "end", *context, "--to", args.new_worker], env, check=False).returncode
            if ended:
                result = 1
            else:
                if held:
                    astra_roles.release(held)
                row = session_staff.worker_row(read_roster(run, args.new_worker))
                if row is not None and row.kind == "worker":
                    try:
                        _ = staff.retire(row.id)
                    except staff.StaffRefused as error:
                        print(f"codex_mesh: {args.new_worker}: {error}", file=sys.stderr)
                        result = 1
    return result


def recorded_passes(run: Path) -> list[dict[str, object]]:
    state = read_object(run / unit_runs.STATE_FILE)
    passes = [{**object_map(value), "team_slot": slot}
              for slot, value in object_map(state.get("pass")).items()]
    history = text(state.get("history_file"))
    if history:
        for line in Path(history).read_text().splitlines():
            entry = object_map(cast(object, json.loads(line)))
            if entry.get("event_type") in ("pass_started", "pass_finished"):
                passes.append(entry)
    return passes


def pass_start_count(run: Path, slot: str) -> int:
    return sum(entry.get("event_type") == "pass_started" and entry.get("team_slot") == slot
               for entry in recorded_passes(run))


def end_failed_launch(run: Path, slot: str, env: dict[str, str]) -> None:
    """End a sole failed dispatch without changing its exit code."""
    try:
        passes = recorded_passes(run)
        if any(entry.get("status") == "completed" for entry in passes):
            return
        if any(entry.get("team_slot") != slot for entry in passes):
            return
        names = set(read_object(run / "mesh_roster.json"))
        record = staff.local_snapshot()
        for path in run.glob("impl_worker_*.json"):
            row = record.by_id(staff.StaffId(text(read_object(path).get("staff_id"))))
            if row is not None:
                names.add(row.name)
        if unit_runs.worker_transport(run) == "herdr":
            parent = worker_admission.parent(record, run=run)
            names.update(row.name for row in record.find(kind="worker", parent=parent.id))
        own_names = {slot, *(text(entry.get("worker_name")) for entry in passes if entry.get("worker_name"))}
        if names - own_names:
            return
        held = unit_runs.held(staff.runtime_id(), env.get("UNIT_RUN_ROOT"), env.get("UNIT_ACTIVE_DIR"), record=record)
        if {Path(folder).resolve() for folder in held} != {run}:
            return
        commands = ([PYTHON, PROGRESS, "finish-run", "--session-dir", str(run), "--status", "error"],
                    ["bash", str(SCRIPTS / "end_session.sh")])
        ended = True
        for command in commands:
            result = call(command, env, capture=True, check=False)
            if result.returncode:
                print(f"assign.sh: {failure_detail(result)}", file=sys.stderr)
                ended = False
        if ended:
            print(f"assign.sh: run ended: {run}; its only worker failed at launch without a completed pass", file=sys.stderr)
    except Exception as error:
        print(f"assign.sh: unable to end failed run {run}: {error}", file=sys.stderr)


def main() -> int:
    require_live_director()
    args = parse_arguments()
    if args.role == "simplify":
        return simplify_daily.main(["--session-dir", args.session_dir, "--cwd", args.cwd])
    env = dict(os.environ)
    if args.target == "consultation-pane":
        cwd = Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir():
            refuse("--cwd must be a directory")
        return execute_consultation(args, cwd)
    if args.target == "durable-design-role":
        return execute_durable(args, env)
    if args.operation in RECOVERY_OPERATIONS:
        try:
            admitted = worker_admission.admit(args.to, Path(args.session_dir).expanduser().resolve())
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        env.update(UNIT_TEAM_ROLE=admitted.name, UNIT_PASS_OWNER="launcher")
        return execute_operation(args, admitted, env)
    cwd, prompt = Path(args.cwd).expanduser().resolve(), Path(args.prompt).expanduser().resolve()
    if not cwd.is_dir() or not prompt.is_file():
        refuse("--cwd must be a directory and --prompt must be a readable file")
    if args.operation == AssignmentOperation.ASTRA_REVIEW:
        return execute_astra_review(args, cwd, prompt, env)
    paths = authorized_paths(args, cwd)
    run, phase = prepare_run(args, cwd, prompt, env)
    transport = unit_runs.worker_transport(run)
    role = "impl" if args.role == "implement" else args.role
    check = [PYTHON, PROGRESS, "check-launch", "--session-dir", str(run), "--pass-kind", role,
             "--phase-id", phase, "--prompt-file", str(prompt), "--fix-cause", args.fix_cause]
    if args.lens:
        check += ["--lens", args.lens]
    checked = call(check, env, check=False)
    if checked.returncode:
        return 3 if checked.returncode == 3 else 2
    if transport == "herdr":
        code = execute_herdr(args, run, phase, prompt, role, paths, env)
        if code == 2:
            end_failed_launch(run, args.to or args.new_worker or role, env)
        return code
    family, model, effort, tier = resolve_model(args, role, env)
    worker = args.to
    reservation: dict[str, object] = {}
    if family == "claude":
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
        _ = worker_admission.admit(worker, run)
    except ValueError as error:
        workers.release_allocation(run, worker, reservation)
        print(str(error), file=sys.stderr)
        return 2
    env["UNIT_TEAM_ROLE"] = worker
    env["UNIT_PASS_OWNER"] = "launcher"
    membership = staff.local_snapshot()
    member = worker_admission.member(run, worker, membership)
    switches = member is not None and (session_staff.worker_family(membership, member.id) != family
        or (family == "codex" and worker_admission.dispatch_pending(run, worker, prompt)))
    if family == "claude":
        if switches:
            args.to = worker
        slot = ({"adversary": "test", "contract": "impl", "": ""}.get(args.lens, "review")
                if args.role == "review" else worker)
        started_before = pass_start_count(run, slot)
        try:
            code = execute_claude(args, run, phase, cwd, prompt, env, model, effort)
        finally:
            workers.release_allocation(run, worker, reservation)
        try:
            if code and pass_start_count(run, slot) > started_before:
                end_failed_launch(run, slot, env)
        except Exception as error:
            print(f"assign.sh: unable to end failed run {run}: {error}", file=sys.stderr)
        return code
    dispatch = Dispatch(args, run, cwd, prompt, phase, role, worker, family, model, effort, tier, env, reservation,
                        switching=member if switches else None)
    try:
        code = execute(dispatch, paths)
    finally:
        dispatch.cleanup()
    if code and dispatch.agent is not None and dispatch.agent.returncode:
        end_failed_launch(run, worker, env)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
