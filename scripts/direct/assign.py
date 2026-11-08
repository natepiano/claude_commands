#!/usr/bin/env python3
"""Claim a unit worker and watch one named dispatch through completion."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, NoReturn, cast, final, overload

from scripts.astra import astra_roles
from scripts.agents import worker_lifecycle
from scripts.lib.process_failure import failure_detail
from scripts.direct import assign_claude, phase_reservation, progress_history, simplify_daily
from scripts.production import staff, worker_admission
from scripts.lib import conf, models_registry, unit_runs
from scripts.herdr import herdr, side_pane, worker_panes
from scripts.production import worker as staff_worker
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import style_review
from scripts.usage.agents_log import PHASE_REVIEW_LENSES

SCRIPTS = Path(__file__).resolve().parent
PYTHON = str(SCRIPTS / "../lib/py")
PROGRESS = str(SCRIPTS / "progress_history.py")
RULES = """
Rules for this dispatch:
Run only commands that finish within one call. Never poll, sleep, call wait,
loop on a status file, or make an empty write_stdin poll. Run long commands
through detached_runs.py start; then end your turn and receive the result message.
We believe in three gods, Simple, Fast and Beautiful, with correctness as the floor beneath them.
"""


class AssignmentOperation(StrEnum):
    DISPATCH = "dispatch"
    ASTRA_REVIEW = "astra-review"




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


def object_map(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    return worker_lifecycle.read_worker_state(path)




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
                   "style-since", "style-until"):
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
    if not args.prompt and args.target == "staff-worker":
        parser.error("--prompt is required")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.new_worker:
        parser.error("astra-review requires --new-worker")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and args.role != "review":
        parser.error("astra-review requires --role review")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.prompt:
        parser.error("astra-review requires --prompt")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and args.target != "consultation-pane" and not args.session_dir:
        parser.error("astra-review requires --session-dir")
    if args.operation != AssignmentOperation.DISPATCH and (args.lens or args.early_ready or args.style_since):
        parser.error("review stages require the dispatch operation")
    for address in (args.to, args.new_worker):
        if address and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", address):
            parser.error(f"invalid worker name: {address}")
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
    if worker_lifecycle.finished_run_status(str(run)) or not stored_phase:
        identity = next((match for line in prompt.read_text().splitlines()
                         if (match := re.match(r"^Phase (\S+) — (.*)$", line))), None)
        title = identity.group(2).strip() if identity else text(stored_phase.get("title")) or "Assignment"
        _ = call([PYTHON, PROGRESS, "start-phase", "--session-dir", str(run), "--phase-id", phase,
                  "--phase-title", title], env)
    return run, phase






@dataclass
class NoStyleAssignment:
    """This dispatch has no phase style result to capture."""


@dataclass
class StyleAssignment:
    directory: Path
    summary: Path
    captured: bool = False








@dataclass
class AssignmentResult:
    args: Arguments
    run: Path
    phase: str
    role: str
    worker: str
    env: dict[str, str]
    pass_open: bool = False
    started_at: float = field(default_factory=time.time)
    style_assignment: NoStyleAssignment | StyleAssignment = field(default_factory=NoStyleAssignment)

    def finish_pass_command(self, status: str) -> list[str]:
        return [PYTHON, PROGRESS, "finish-pass", "--session-dir", str(self.run), "--status", status]

    def capture_style(self, code: int) -> None:
        assignment = self.style_assignment
        if isinstance(assignment, StyleAssignment) and not assignment.captured:
            style_review.finish(assignment.directory, assignment.summary, code)
            assignment.captured = True


def recorded_phase(run: Path, phase_id: str) -> tuple[dict[str, object], dict[str, object]]:
    state = read_object(run / "progress_history_state.json")
    phase = next((object_map(candidate) for candidate in cast(list[object], state.get("open_phases", []))
                  if object_map(candidate).get("id") == phase_id), object_map(state.get("phase")))
    return state, phase


def record_review(dispatch: AssignmentResult, completed: bool, summary: Path) -> None:
    """Keep each launcher's review evidence, including a recorded zero."""
    if dispatch.role != "review":
        return
    state, phase = recorded_phase(dispatch.run, dispatch.phase)
    record = staff.local_snapshot()
    parent = worker_admission.parent(record, run=dispatch.run)
    member = worker_admission.find_worker(record, parent.id, dispatch.worker)
    raised = len(re.findall(r"^\s*\d+[.)]\s+", summary.read_text(), re.MULTILINE))
    progress_history.write_review_turn(dispatch.run, {
        "unit": str(parent.id), "phase": dispatch.phase,
        "phase_instance_id": phase.get("instance_id", ""), "history_file": state.get("history_file", ""),
    }, reviewer=dispatch.worker, thread_id=str(member.current_session) if member is not None else "", lens=dispatch.args.lens,
       pass_number=dispatch.args.pass_number, started_at=int(dispatch.started_at),
       status="reviewed" if completed else "error", raised=raised,
       details={"pool": "", "lease_ordinal": 0, "prime_hash": ""})


def finish_repair(dispatch: AssignmentResult, code: int) -> bool:
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
            print(shlex.join(["env", f"UNIT_TEAM_ROLE={dispatch.env.get('UNIT_TEAM_ROLE', dispatch.worker)}", "UNIT_PASS_OWNER=launcher", *command]), file=sys.stderr)
    dispatch.pass_open = False
    return not failed


def settle_dispatch(dispatch: AssignmentResult, code: int, completed: bool, shown: str,
                    status: Path, summary: Path, reply: Path) -> int:
    """Completed work wins; otherwise ownership loss precedes refusal or failure."""
    if dispatch.args.early_ready:
        lens_suffix = f"_{dispatch.args.lens}" if dispatch.args.lens else ""
        _ = (status.parent / f"{dispatch.worker}_status{lens_suffix}").write_text("reviewed\n" if completed else "error\n")
    if completed and reply.exists():
        _ = summary.write_text(reply.read_text())
    dispatch.capture_style(0 if completed else code or 1)
    record_review(dispatch, completed, summary)
    if shown not in summary.read_text():
        _ = summary.write_text(shown + "\n\n" + summary.read_text())
    if dispatch.args.summary:
        _ = Path(dispatch.args.summary).write_text(summary.read_text())
    outcome = "completed" if completed else "error"
    if not 3 <= code <= 8:
        if dispatch.role == "fix" and dispatch.env.get("UNIT_RESOLVES_ROUND") == "1":
            if not finish_repair(dispatch, 0 if completed else code or 1):
                completed, code, outcome = False, 1, "error"
        elif dispatch.pass_open:
            _ = call(dispatch.finish_pass_command(outcome), dispatch.env)
            dispatch.pass_open = False
    _ = status.write_text("done\n" if completed else "error\n")
    return 0 if completed else code if 3 <= code <= 8 else 2 if code == 2 else 1


def resolve_model(args: Arguments, role: str, env: dict[str, str]) -> tuple[str, str, str, str]:
    task = role if "." in role else f"direct.{role}"
    result = call(["bash", "-c", '''source "$1"; models_launch_configuration || exit 2; agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
 AGENT_MODEL="${pair%%:*}"
 AGENT_EFFORT="${pair##*:}"
fi
printf '%s\\n' "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}" "$MODELS_LAUNCH_CONFIGURATION"
''', "assign", str(SCRIPTS / "../agents/models_config.sh"), task, args.model, args.effort], env, capture=True)
    family, model, effort, tier, configuration = result.stdout.strip().splitlines()[-5:]
    env["MODELS_LAUNCH_CONFIGURATION"] = configuration
    return family, model, effort, tier

def image_note(images: list[str]) -> str:
    """Name each attached image for a worker that cannot receive it as an option."""
    if not images:
        return ""
    return "\nRead these images:\n" + "\n".join(str(Path(image).resolve()) for image in images)




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
    if role == "review":
        content += "\nReturn the complete review result in your final reply.\n"
    if not message.is_file() or message.read_text() != content:
        write_text_atomic(message, content)
    end = style_review.CommitEnd(args.style_until) if args.style_until else style_review.WORKING_TREE
    style: NoStyleAssignment | StyleAssignment = NoStyleAssignment()
    if args.style_since:
        style = StyleAssignment(style_review.start(Path(args.cwd), args.style_since, directory, message, slot, end), summary)
        message = style.directory / "prompt.md"
    env["UNIT_RUN_DIR"] = str(run)
    env["UNIT_TEAM_ROLE"] = slot
    if role != "review" and not assign_claude.claim_phase(run, phase, Path(args.cwd).expanduser().resolve(), log, status, slot, env):
        return 1
    command = [PYTHON, str(SCRIPTS / "../production/worker.py"), "assign", role, str(message),
               "--session-dir", str(run), "--phase", phase, "--slot", slot, "--kind", "assign", "--reply", str(reply),
               "--lens", args.lens, "--fix-cause", args.fix_cause,
               "--fix-pass", str(args.pass_number if role == "fix" else 0)]
    if args.to or args.new_worker:
        command += ["--name", args.to or args.new_worker]
    if args.model or args.effort:
        selection = resolve_model(args, role, env)
        command += ["--model", selection[1], "--effort", selection[2]]
    if args.early_ready:
        command += ["--early-ready", str(Path(args.early_ready).resolve())]
    command += ["--launcher-command", json.dumps(["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]])]
    number = sum(entry.get("event_type") == "pass_started" for entry in recorded_passes(run)) + 1
    write_text_atomic(directory / f"dispatch_{number}_paths.json",
                      json.dumps({"worker": slot, "round": number, "paths": paths}))
    write_text_atomic(status, "running\n")
    _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(run), slot, "register",
              f"role={role}; status in {status}"], env)
    _ = call(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(run / "heartbeat.log"),
              "header", slot, role], env, check=False)
    with log.open("a") as output:
        result = subprocess.run(command, env=env, text=True, stdout=output, stderr=subprocess.STDOUT)
    env["UNIT_PASS_OWNER"] = "launcher"
    record = staff.local_snapshot()
    parent = worker_admission.parent(record, run=run)
    passed = object_map(object_map(read_object(run / unit_runs.STATE_FILE).get("pass")).get(slot))
    selected = text(passed.get("worker_name")) or slot
    if selected != slot:
        def result_path(path: Path) -> Path:
            target = directory / (selected + path.name.removeprefix(slot))
            if path.is_file():
                _ = path.replace(target)
            return target

        summary, status, reply, log = (result_path(path) for path in (summary, status, reply, log))
        write_text_atomic(directory / f"dispatch_{number}_paths.json",
                          json.dumps({"worker": selected, "round": number, "paths": paths}))
        if isinstance(style, StyleAssignment):
            style.summary = summary
    if not summary.exists():
        write_text_atomic(summary, "")
    member = worker_admission.find_worker(record, parent.id, selected)
    opened = (member is not None and isinstance(progress_history.active_dispatch(member.id, run),
                                               progress_history.ActiveDispatch))
    outcome = AssignmentResult(args, run, phase, role, selected, env, pass_open=opened,
                               style_assignment=style)
    if args.early_ready:
        if result.returncode == 0:
            write_text_atomic(summary, reply.read_text())
            if args.summary:
                write_text_atomic(Path(args.summary), summary.read_text())
            record_review(outcome, True, summary)
            _ = call(outcome.finish_pass_command("completed"), env)
        outcome.capture_style(result.returncode)
        write_text_atomic(status, "done\n" if result.returncode == 0 else "error\n")
        code = result.returncode
    else:
        code = settle_dispatch(outcome, result.returncode, result.returncode == 0,
                               f"{parent.name} · {role}", status, summary, reply)
    _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(run), selected,
              "done" if code == 0 else "blocked",
              f"role={role}; exit {code}; summary {summary}"], env)
    return result.returncode if result.returncode in (7, 8) else code



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
        slot=name.value, phase=args.phase, kind="astra",
        reply=Path(args.summary) if args.summary else staff_worker.REPLY_BESIDE_PROMPT, policy=policy,
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
    return execute_astra_review_herdr(args, run, cwd, prompt, env)


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
        names: set[str] = set()
        record = staff.local_snapshot()
        for path in run.glob("impl_worker_*.json"):
            row = record.by_id(staff.StaffId(text(read_object(path).get("staff_id"))))
            if row is not None:
                names.add(row.name)
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
    cwd, prompt = Path(args.cwd).expanduser().resolve(), Path(args.prompt).expanduser().resolve()
    if not cwd.is_dir() or not prompt.is_file():
        refuse("--cwd must be a directory and --prompt must be a readable file")
    if args.operation == AssignmentOperation.ASTRA_REVIEW:
        return execute_astra_review(args, cwd, prompt, env)
    paths = authorized_paths(args, cwd)
    run, phase = prepare_run(args, cwd, prompt, env)
    role = "impl" if args.role == "implement" else args.role
    check = [PYTHON, PROGRESS, "check-launch", "--session-dir", str(run), "--pass-kind", role,
             "--phase-id", phase, "--prompt-file", str(prompt), "--fix-cause", args.fix_cause]
    if args.lens:
        check += ["--lens", args.lens]
    checked = call(check, env, check=False)
    if checked.returncode:
        return 3 if checked.returncode == 3 else 2
    slot = args.to or args.new_worker or role
    before = object_map(read_object(run / unit_runs.STATE_FILE).get("pass")).get(slot)
    code = execute_herdr(args, run, phase, prompt, role, paths, env)
    if code == 2 or (code == 1 and env.get("UNIT_RESOLVES_ROUND") != "1" and object_map(read_object(run / unit_runs.STATE_FILE).get("pass")).get(slot) != before):
        end_failed_launch(run, slot, env)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
