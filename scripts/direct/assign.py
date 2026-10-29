#!/usr/bin/env python3
"""Claim a unit worker and watch one named dispatch through completion."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast, final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents import codex_mesh
from scripts.direct import workers
from scripts.direct.worker_state import worker_claim_lock, worker_record_path
from scripts.guilds import roles as guild_roles
from scripts.lib import unit_runs

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
    session_dir: str = ""
    prompt: str = ""
    cwd: str = ""
    lens: str = ""
    pass_number: int = 1
    image: list[str] = []


def object_map(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    try:
        return object_map(cast(object, json.loads(path.read_text())))
    except (OSError, ValueError):
        return {}


def read_roster(run: Path, name: str) -> dict[str, object]:
    with (run / "mesh_roster.json").open() as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        return object_map(object_map(cast(object, json.load(handle))).get(name))


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
    choice = parser.add_mutually_exclusive_group()
    _ = choice.add_argument("--to", default="")
    _ = choice.add_argument("--new-worker", default="")
    for option in ("model", "effort", "summary", "fix-cause", "paths", "phase", "early-ready", "session-dir"):
        _ = parser.add_argument("--" + option, default="")
    _ = parser.add_argument("--prompt", required=True)
    _ = parser.add_argument("--cwd", default=str(Path.cwd()))
    _ = parser.add_argument("--lens", choices=("adversary", "contract", "craft", "ux"), default="")
    _ = parser.add_argument("--pass", dest="pass_number", type=int, default=1)
    _ = parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args(namespace=Arguments())
    if args.role in ("research", "propose"):
        parser.error(f"{args.role} is not available until Phase 6")
    for address in (args.to, args.new_worker):
        if address and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", address):
            parser.error(f"invalid worker address: {address}")
    if args.pass_number < 1:
        parser.error("--pass must be positive")
    if args.early_ready and args.role != "review":
        parser.error("--early-ready requires review")
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
    session = args.session_dir
    if not session:
        marker = unit_runs.marker(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), os.environ.get("UNIT_ACTIVE_DIR"))
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
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", phase) or phase in (".", ".."):
        refuse(f"invalid phase id: {phase}")
    if codex_mesh.finished_run_status(str(run)) or not stored_phase:
        identity = next((match for line in prompt.read_text().splitlines()
                         if (match := re.match(r"^Phase (\S+) — (.*)$", line))), None)
        title = identity.group(2).strip() if identity else text(stored_phase.get("title")) or "Assignment"
        _ = call([PYTHON, PROGRESS, "start-phase", "--session-dir", str(run), "--phase-id", phase,
                  "--phase-title", title], env)
    unit_runs.record_director(run, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    return run, phase


def resolve_model(args: Arguments, role: str, env: dict[str, str]) -> tuple[str, str, str, str]:
    result = call(["bash", "-c", '''source "$1"; agents_resolve "direct.$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "direct.$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}"
''', "assign", str(SCRIPTS / "../agents/agents_config.sh"), role, args.model, args.effort], env, capture=True)
    family, model, effort, tier = result.stdout.strip().splitlines()[-4:]
    if family != "codex":
        print("assign.sh: persistent unit workers require a Codex assignment", file=sys.stderr)
        raise SystemExit(3)
    return family, model, effort, tier


@dataclass(frozen=True)
class WorkerClaim:
    """The durable worker identity verified under the dispatch lock."""
    identity_path: Path
    slot: str
    model: str
    effort: str


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
                if not record.get("adopted_to") and record.get("thread_id"):
                    identity = read_object(self.claim.identity_path)
                    identity["worker_id"] = record["thread_id"]
                    _ = self.claim.identity_path.write_text(json.dumps(identity))
        workers.release_allocation(self.run, self.worker, self.reservation)


def claim_worker(dispatch: Dispatch) -> WorkerClaim:
    """Port the durable identity, stale-claim, role and compaction checks of worker_record claim."""
    with worker_claim_lock(dispatch.run):
        record = read_roster(dispatch.run, dispatch.worker)
        if not record or record.get("adopted_to"):
            refuse(f"{dispatch.worker}: worker is missing or adopted")
        args = dispatch.args
        identity_path = worker_record_path(dispatch.run, dispatch.worker, dispatch.worker, role=dispatch.role)
        identity = read_object(identity_path)
        slot = text(identity.get("slot")) or dispatch.worker
        model = text(identity.get("model")) or text(record.get("model")) or dispatch.model
        effort = text(record.get("effort")) or dispatch.effort
        expected = {"name": dispatch.worker, "slot": slot, "family": dispatch.family,
                    "model": model, "worker_id": record.get("thread_id")}
        _ = identity.pop("role", None)
        if (identity and identity != expected) or not record.get("thread_id"):
            refuse(f"{dispatch.worker} has no matching durable worker identity for slot {slot}")
        if (args.model and model != dispatch.model
                or (args.effort or ":" in args.model) and effort != dispatch.effort):
            refuse(f"{dispatch.worker}: requested {dispatch.model}:{dispatch.effort}, "
                   + f"worker identity is {model}:{effort}")
        if not identity:
            _ = identity_path.write_text(json.dumps(expected))
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
        if record.get("transferring_role") or record.get("role_redirect") or guild_roles.transfer_route(
                text(record.get("thread_id")), text(record.get("held_design_id"))):
            refuse(f"{dispatch.worker}: TransferringRole; use its destination holder")
        if record.get("held_design_id"):
            refuse(f"{dispatch.worker}: held roles resume through roles.py")
        config = Path(os.environ.get("UNIT_CONFIG", str(Path.home() / ".claude/config/direct.conf")))
        match = re.search(r"^UNIT_COMPACT_ABOVE_TOKENS=(\d+)", config.read_text(), re.M) if config.is_file() else None
        threshold = int(match.group(1)) if match else 0
        if threshold <= 0:
            refuse(f"UNIT_COMPACT_ABOVE_TOKENS in {config} must be a positive integer")
        context_tokens = record.get("context_tokens")
        if isinstance(context_tokens, int) and context_tokens > threshold:
            compact = dispatch.mesh_call("compact", capture=True, check=False)
            if compact.returncode:
                detail = (compact.stderr.strip() or compact.stdout.strip() or f"exit {compact.returncode}").splitlines()[-1]
                if compact.returncode == 3:
                    dispatch.board(slot, "status", f"launcher: {dispatch.worker} still has a live turn after compaction: {detail}; follow-up not sent")
                    refuse(f"{dispatch.worker} still has a live turn after compaction; end the worker "
                           + f"(codex_mesh.py end --to {dispatch.worker}) and start a fresh one")
                dispatch.board(slot, "status", f"launcher: compaction failed for {dispatch.worker}: {detail}; continuing follow-up")
            else:
                dispatch.board(slot, "status", f"launcher: {compact.stdout.strip() or f'compacted {dispatch.worker}'}")
        if record.get("status") == "ended":
            _ = dispatch.mesh_call("reopen", capture=True)
        dispatch.claim_follow()
        _ = claim_path.write_text(json.dumps({"pid": os.getpid(), "worker": dispatch.worker, "turn_id": ""}))
        return WorkerClaim(identity_path, slot, model, effort)


def stage_two_submitted(receipts: Path, message: str) -> bool:
    """A successful follow must have submitted this dispatch's final-review input."""
    if not receipts.exists():
        return False
    for line in receipts.read_text().splitlines():
        receipt = object_map(cast(object, json.loads(line)))
        if receipt.get("turn_id") and message in cast(list[object], receipt.get("messages", [])):
            return True
    return False


def settle_dispatch(dispatch: Dispatch, code: int, completed: bool, shown: str,
                    status: Path, summary: Path, reply: Path) -> int:
    """Completed work wins; otherwise ownership loss precedes refusal or failure."""
    record = read_roster(dispatch.run, dispatch.worker)
    if not completed and record.get("adopted_to"):
        destination = text(record["adopted_to"])
        _ = status.write_text("handed_off\n")
        _ = summary.write_text(f"{shown}: handed off to {destination}; completion unknown\n")
        print(f"handed off: {dispatch.worker} to {destination}; completion unknown")
        return 4
    if not completed and code == 0:
        print("assign.sh: Stage 2 was not submitted by this launcher; review incomplete", file=sys.stderr)
        code = 1
    if not summary.read_text().strip() and reply.exists():
        _ = summary.write_text(reply.read_text())
    if shown not in summary.read_text():
        _ = summary.write_text(shown + "\n\n" + summary.read_text())
    if dispatch.args.summary:
        _ = Path(dispatch.args.summary).write_text(summary.read_text())
    outcome = "completed" if completed else "error"
    _ = status.write_text("done\n" if completed else "error\n")
    if dispatch.pass_open:
        _ = call([PYTHON, PROGRESS, "finish-pass", "--session-dir", str(dispatch.run), "--status", outcome], dispatch.env)
        dispatch.pass_open = False
    if dispatch.claim is not None:
        dispatch.board(dispatch.claim.slot, "done" if completed else "blocked", f"{shown}: {outcome}; summary {summary}")
    return 0 if completed else 2 if code == 2 else 1


def execute(dispatch: Dispatch, paths: list[str]) -> int:
    shown = workers.shown_name(dispatch.run, dispatch.worker)
    args = dispatch.args
    phase_dir = dispatch.run / "phases" / dispatch.phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if dispatch.role != "review" else f"_{args.lens}_{args.pass_number}" if args.lens else f"_{args.pass_number}"
    status = phase_dir / f"{dispatch.role}_status{suffix}"
    summary = phase_dir / f"{dispatch.role}_summary{suffix}.txt"
    log = phase_dir / f"{dispatch.role}_agent{suffix}.log"
    reply = phase_dir / f"{dispatch.role}_reply{suffix}.txt"
    _ = summary.write_text("")
    _ = status.write_text("running\n")
    try:
        dispatch.claim = claim_worker(dispatch)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 2
        return settle_dispatch(dispatch, code, False, shown, status, summary, reply)
    dispatch.model, dispatch.effort = dispatch.claim.model, dispatch.claim.effort
    shown = workers.shown_name(dispatch.run, dispatch.worker)
    with (phase_dir / "dispatch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        numbers = [int(path.stem.split("_")[1]) for path in phase_dir.glob("dispatch_*_paths.json")]
        number = max(numbers, default=0) + 1
        _ = (phase_dir / f"dispatch_{number}_paths.json").write_text(json.dumps(
            {"worker": dispatch.worker, "round": number, "paths": paths}) + "\n")
    message = phase_dir / f"dispatch_{number}_prompt.txt"
    receipts = phase_dir / f"dispatch_{number}_submissions.jsonl"
    _ = message.write_text(dispatch.prompt.read_text() + "\n" + RULES +
                           f"\nWorker: {shown}\nAs your last act, write your summary to {summary}.\n")
    dispatch.board(dispatch.claim.slot, "register", f"{shown}; mesh={dispatch.worker}; role={dispatch.role}; status in {status}")
    _ = call(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(dispatch.run / "heartbeat.log"),
              "header", shown, f"{shown}: {dispatch.role}"], dispatch.env, check=False)
    if not args.early_ready:
        dispatch.start_pass(shown)
    follow = [PYTHON, MESH, "follow", "--session-dir", str(dispatch.run), "--to", dispatch.worker,
              "--claim-pid", str(os.getpid()), "--role", dispatch.role, "--cwd", str(dispatch.cwd),
              "--message-file", str(message), "--summary-file", str(summary), "--reply-file", str(reply),
              "--log-file", str(log), "--model", dispatch.model, "--effort", dispatch.effort,
              "--task", f"direct.{dispatch.role}", "--service-tier", dispatch.tier]
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
                record = read_roster(dispatch.run, dispatch.worker)
                if record.get("adopted_to"):
                    code = 2
                elif dispatch.stage_errors:
                    print("assign.sh: " + dispatch.stage_errors[0], file=sys.stderr)
                    code = 1
                else:
                    pending_path = dispatch.run / f"{dispatch.worker}.pending.json"
                    with pending_path.open() as inbox:
                        fcntl.flock(inbox, fcntl.LOCK_SH)
                        pending = object_map(cast(object, json.load(inbox)))
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


def main() -> int:
    args = parse_arguments()
    cwd, prompt = Path(args.cwd).expanduser().resolve(), Path(args.prompt).expanduser().resolve()
    if not cwd.is_dir() or not prompt.is_file():
        refuse("--cwd must be a directory and --prompt must be a readable file")
    paths = authorized_paths(args, cwd)
    env = dict(os.environ)
    run, phase = prepare_run(args, cwd, prompt, env)
    role = "impl" if args.role == "implement" else args.role
    check = [PYTHON, PROGRESS, "check-launch", "--session-dir", str(run), "--pass-kind", role,
             "--phase-id", phase, "--prompt-file", str(prompt), "--fix-cause", args.fix_cause]
    if args.lens:
        check += ["--lens", args.lens]
    _ = call(check, env)
    family, model, effort, tier = resolve_model(args, role, env)
    worker = args.to
    reservation: dict[str, object] = {}
    if not worker:
        command = [PYTHON, str(SCRIPTS / "workers.py"), "resolve", "--session-dir", str(run), "--role", role,
                   "--owner-pid", str(os.getpid())]
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
    env["UNIT_TEAM_ROLE"] = worker
    env["UNIT_PASS_OWNER"] = "launcher"
    dispatch = Dispatch(args, run, cwd, prompt, phase, role, worker, family, model, effort, tier, env, reservation)
    try:
        return execute(dispatch, paths)
    finally:
        dispatch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
