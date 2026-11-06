#!/usr/bin/env python3
"""Select staff workers and dispatch work through their native conversations."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
import json
import socket
import time
from pathlib import Path
import subprocess
import sys
from typing import cast, final

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.agents import codex_mesh
from scripts.direct import progress_history
from scripts.herdr import worker_panes
from scripts.lib import atomic_file
from scripts.lib.script_envelope import script_prompt
from scripts.production import staff, worker_admission

import herdr

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole
ExitCode = worker_panes.ExitCode


@dataclass(frozen=True)
class DispatchNotSubmitted:
    """The complete transcript contains no prompt for this dispatch."""


@dataclass(frozen=True)
class DispatchSubmitted:
    """The transcript contains the prompt without a matching final reply."""


@dataclass(frozen=True)
class DispatchComplete:
    reply: str


@dataclass(frozen=True)
class DispatchEvidenceUnknown:
    reason: str


DispatchReceipt = DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown


@dataclass(frozen=True)
class ReplyBesidePrompt:
    """Write the reply beside the prompt when no destination is supplied."""


@dataclass(frozen=True)
class SingleStageReview:
    """Complete the dispatch without a second review stage."""


REPLY_BESIDE_PROMPT = ReplyBesidePrompt()
SINGLE_STAGE_REVIEW = SingleStageReview()


def dispatch_receipt(row: staff.StaffRow, line: str, dispatch_id: str) -> DispatchReceipt:
    """Read only this worker's transcript through the existing transcript parser."""
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return DispatchEvidenceUnknown("worker transcript backend is unavailable")
    submitted = False
    incomplete = False
    try:
        for entry in worker_admission.transcript_entries(worker_admission.transcript_source(row, backend), set()):
            if isinstance(entry, worker_admission.TornTranscriptRecord):
                incomplete = True
                continue
            if entry.startswith("user:\n"):
                text = entry.removeprefix("user:\n").strip()
                if text in (line, script_prompt(line)):
                    submitted = True
            elif submitted and entry.startswith("assistant:\n"):
                reply = entry.removeprefix("assistant:\n")
                if reply.rstrip().splitlines()[-1:] == [f"DONE {dispatch_id}"]:
                    return DispatchComplete(reply)
    except (OSError, ValueError) as error:
        return DispatchEvidenceUnknown(str(error))
    if incomplete and not submitted:
        return DispatchEvidenceUnknown("worker transcript has incomplete records")
    return DispatchSubmitted() if submitted else DispatchNotSubmitted()


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _agent_command(client: herdr.Herdr, worker: worker_panes.OpenWorker, action: str, line: str = "") -> None:
    if not herdr.check_target(str(worker.row.current_session), action="keys",
            place=herdr.HerdrPlace(client.session, worker.pane), terminal=worker.terminal, text=line, client=client):
        raise herdr.HerdrFailure(f"{worker.row.name}: dispatch target is unproved", "wrong_target")
    arguments = ["agent", action, worker.pane]
    if action == "prompt":
        arguments.extend((script_prompt(line), "--wait"))
    result = subprocess.run(client.argv(*arguments), text=True, capture_output=True, check=False)
    herdr.require_success(result, f"herdr agent {action}")


def _startup_result(name: WorkerName, result: worker_panes.WorkerStart) -> ExitCode:
    if isinstance(result, worker_panes.OpenWorker):
        return ExitCode.OK
    if isinstance(result, worker_panes.AdmissionRefused):
        print(f"staff: {name.value}: not opened: {result.reason}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, worker_panes.StartRefused):
        print(f"{result.reason.value}: {result.detail}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, worker_panes.IncompleteStart):
        print(f"{name.value}: incomplete startup: {result.reason}", file=sys.stderr)
    elif isinstance(result, worker_panes.UnpublishedWorker):
        print(f"{name.value}: publication failed: {result.cause}; pane {result.pane} retained", file=sys.stderr)
    # The shared scan already reports unknown and structural evidence.
    return ExitCode.UNCERTAIN


def _save_reply(path: Path, reply: str) -> None:
    text = reply.rstrip() + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_file.write_text_atomic(path, text)


def dispatch(parent: StaffId, name: WorkerName, role: WorkerRole, prompt: Path, dispatch_id: str = "", *,
             session_dir: Path, slot: str, phase: str, kind: str, launcher_command: list[str],
             lens: str = "", reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, fix_cause: str = "",
             fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW) -> ExitCode:
    """Hold one worker through startup, pass recording, submission, and completion."""
    prompt = prompt.resolve()
    with worker_panes.worker_lock(parent, name) as hold:
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.BUSY
        started = worker_panes.start_or_resume(parent, name, role, lock=hold)
        if not isinstance(started, worker_panes.OpenWorker):
            return _startup_result(name, started)
        instance = progress_history.begin_dispatch(session_dir, slot=slot, phase=phase, role=role,
            worker_name=name.value, worker_id=started.row.id, launcher_command=launcher_command, prompt=prompt, kind=kind, lens=lens,
            fix_cause=fix_cause, fix_pass=fix_pass, activity=activity)
        if dispatch_id and dispatch_id not in (instance, worker_panes.stage_two_completion_id(instance)):
            print("dispatch id differs from the recorder pass", file=sys.stderr)
            return ExitCode.REFUSED
        dispatch_id = dispatch_id or instance
        line = f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {dispatch_id}."
        destination = reply if isinstance(reply, Path) else prompt.with_name(prompt.stem + "_reply.txt")
        client = _client()
        result = _dispatch_turn(parent, started, client, line, dispatch_id)
        if not isinstance(result, DispatchComplete):
            return result
        if isinstance(early_ready, Path):
            _wait_for_ready(early_ready)
            second_id = worker_panes.stage_two_completion_id(instance)
            second_line = (f"Stage 2: the final diff is at {json.dumps(str(early_ready).removesuffix('.ready') + '.diff')}. "
                f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {second_id}.")
            record = staff.snapshot()
            current = next((state for state in worker_panes.observe(parent, record) if state.row.id == started.row.id), None)
            if not isinstance(current, worker_panes.OpenWorker):
                return ExitCode.UNCERTAIN
            result = _dispatch_turn(parent, current, client, second_line, second_id)
            if not isinstance(result, DispatchComplete):
                return result
        _save_reply(destination, result.reply)
        return ExitCode.OK


def _wait_for_ready(ready: Path) -> None:
    watch = codex_mesh.LinuxFileChanges({ready}) if sys.platform.startswith("linux") else codex_mesh.KqueueFileChanges({ready})
    try:
        wake, cancel = socket.socketpair()
        with wake, cancel:
            while not ready.exists():
                _ = watch.wait(wake, time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS)
    finally:
        watch.close()


def _dispatch_turn(parent: StaffId, started: worker_panes.OpenWorker, client: herdr.Herdr,
                   line: str, dispatch_id: str) -> DispatchComplete | ExitCode:
    name = WorkerName.parse(started.row.name)
    current = started
    receipt = dispatch_receipt(current.row, line, dispatch_id)
    if isinstance(receipt, DispatchEvidenceUnknown):
        print(f"anomaly: {name.value}: {receipt.reason}; no prompt sent", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if isinstance(receipt, DispatchComplete):
        return receipt
    if isinstance(receipt, DispatchNotSubmitted):
        if current.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            print(f"{name.value}: agent is {current.status.value}", file=sys.stderr)
            return ExitCode.BUSY
        try:
            _agent_command(client, current, "prompt", line)
        except (herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
            return _delivery_failure(error)
    while True:
        record = staff.snapshot()
        row = record.by_id(started.row.id)
        if row is None:
            print(f"anomaly: {name.value}: worker row is unavailable", file=sys.stderr)
            return ExitCode.UNCERTAIN
        receipt = dispatch_receipt(row, line, dispatch_id)
        if isinstance(receipt, DispatchComplete):
            return receipt
        if isinstance(receipt, DispatchEvidenceUnknown):
            print(f"anomaly: {name.value}: {receipt.reason}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        state = next((item for item in worker_panes.observe(parent, record) if item.row.id == row.id), None)
        if isinstance(state, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            return ExitCode.NO_DONE
        if not isinstance(state, worker_panes.OpenWorker):
            if not isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                print(f"anomaly: {name.value}: live dispatch evidence is unavailable", file=sys.stderr)
            return ExitCode.UNCERTAIN
        current = state
        if current.status == herdr.AgentStatus.BLOCKED:
            return ExitCode.BLOCKED
        if current.status != herdr.AgentStatus.WORKING:
            return ExitCode.NO_DONE
        try:
            _agent_command(client, current, "wait")
        except (herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
            return _delivery_failure(error)

def _delivery_failure(error: herdr.HerdrFailure | subprocess.TimeoutExpired) -> ExitCode:
    print(str(error), file=sys.stderr)
    if isinstance(error, herdr.AgentBlocked):
        return ExitCode.BLOCKED
    if isinstance(error, subprocess.TimeoutExpired) or error.code in ("agent_prompt_stalled", "timeout"):
        return ExitCode.STALLED
    return ExitCode.UNCERTAIN


def _role_number(name: str, role: WorkerRole) -> int:
    if name == role:
        return 1
    suffix = name.removeprefix(role)
    return int(suffix) if name.startswith(role) and suffix.isdigit() and int(suffix) >= 2 else 0


def select_worker(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> (
        WorkerName | worker_panes.StartRefused | worker_panes.ProcessUnknownWorker | worker_panes.UncertainWorker):
    """Select a proved idle worker, a resumable worker, or the lowest free name."""
    states = worker_panes.observe(parent, record)
    candidates = sorted((item for item in states if _role_number(item.row.name, role)),
                        key=lambda item: _role_number(item.row.name, role))
    for item in candidates:
        if isinstance(item, worker_panes.OpenWorker) and item.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            return WorkerName.parse(item.row.name)
        if isinstance(item, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            return WorkerName.parse(item.row.name)
    for item in candidates:
        if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return item
    if candidates:
        names = ", ".join(item.row.name for item in candidates)
        return worker_panes.StartRefused(worker_panes.StartRefusal.BUSY_ROLE_CHANGE, f"workers unavailable: {names}")
    return _free_name(parent, role, record)


def _free_name(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> WorkerName:
    names = {row.name for row in record.find(kind="worker", parent=parent)}
    number = 1
    while (name := role + (str(number) if number > 1 else "")) in names:
        number += 1
    return WorkerName.parse(name)


def add(parent: StaffId, role: WorkerRole, *, name: str = "") -> worker_panes.WorkerStart:
    with staff.row_operation(parent):
        return worker_panes.start_or_resume(parent, WorkerName.parse(name) if name else _free_name(parent, role, staff.snapshot()), role)


def assign(parent: StaffId, role: WorkerRole, prompt: Path, *, session_dir: Path, slot: str,
           phase: str, kind: str, launcher_command: list[str], lens: str = "", name: str = "",
           reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, dispatch_id: str = "", fix_cause: str = "",
           fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW) -> ExitCode:
    if not name:
        matched = progress_history.dispatch_worker(session_dir, slot=slot, phase=phase, role=role,
            prompt=prompt.resolve(), launcher_command=launcher_command)
        if isinstance(matched, progress_history.MatchedDispatch):
            name = matched.worker_name
    record = staff.snapshot()
    rows = record.find(kind="worker", parent=parent)
    run = worker_panes.worker_run(record.rows_by_id[parent])
    if role in ("impl", "fix", "review", "test") and not any(
            isinstance(worker_panes.dispatch_role(row, run), str) for row in rows):
        names = {row.name for row in rows}
        for default_role in ("impl", "fix", "review", "test"):
            if default_role in names:
                continue
            with redirect_stderr(io.StringIO()) as diagnostics:
                result = worker_panes.start_or_resume(parent, WorkerName.parse(default_role), default_role)
            if isinstance(result, worker_panes.OpenWorker):
                print(diagnostics.getvalue(), end="", file=sys.stderr)
                continue
            detail = (result.detail if isinstance(result, worker_panes.StartRefused) else
                      result.cause if isinstance(result, worker_panes.UnpublishedWorker) else result.reason)
            print(f"anomaly: {default_role}: {detail}", file=sys.stderr)
        record = staff.snapshot()
    chosen = WorkerName.parse(name) if name else select_worker(parent, role, record)
    if not isinstance(chosen, WorkerName):
        return _startup_result(WorkerName.parse(role), chosen)
    return dispatch(parent, chosen, role, prompt, dispatch_id, session_dir=session_dir, slot=slot,
        phase=phase, kind=kind, lens=lens, launcher_command=launcher_command, reply=reply,
        fix_cause=fix_cause, fix_pass=fix_pass, activity=activity, early_ready=early_ready)


def end(parent: StaffId, name: WorkerName) -> ExitCode:
    with worker_panes.worker_lock(parent, name) as hold:
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.KEPT
        row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        if row is None:
            return ExitCode.OK
        result = worker_panes.close(parent, name, lock=hold)
        if isinstance(result, worker_panes.Kept):
            print(result.blocker, file=sys.stderr)
            return ExitCode.KEPT
        if isinstance(result, worker_panes.UncertainWorker):
            return ExitCode.UNCERTAIN
        _ = staff.retire(row.id)
        return ExitCode.OK


def restore(parent: StaffId) -> ExitCode:
    record = staff.snapshot()
    run = worker_panes.worker_run(record.rows_by_id[parent])
    changed = False
    outcome = ExitCode.OK
    for state in worker_panes.observe(parent, record):
        if isinstance(state, worker_panes.OpenWorker):
            continue
        if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            outcome = ExitCode.UNCERTAIN
            continue
        try:
            role = worker_panes.dispatch_role(state.row, run)
            result = worker_panes.start_or_resume(parent, WorkerName.parse(state.row.name), role)
            code = _startup_result(WorkerName.parse(state.row.name), result)
        except (herdr.HerdrFailure, OSError) as error:
            print(f"anomaly: {state.row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
            outcome = ExitCode.UNCERTAIN
            continue
        if code != ExitCode.OK:
            outcome = code
        else:
            changed = True
    if outcome == ExitCode.OK:
        print("done" if changed else "unchanged")
    return outcome


def list_workers(parent: StaffId, record: staff.StaffSnapshot) -> list[dict[str, object]]:
    listing: list[dict[str, object]] = []
    run = worker_panes.worker_run(record.rows_by_id[parent])
    for state in worker_panes.observe(parent, record):
        role = worker_panes.dispatch_role(state.row, run)
        fields: dict[str, object] = {"name": state.row.name, "id": str(state.row.id),
            "current_session": str(state.row.current_session), "state": type(state).__name__}
        fields["role"] = role if isinstance(role, str) else {"state": "WorkerRoleUnavailable", "reason": role.reason}
        if isinstance(state, worker_panes.OpenWorker):
            fields["activity"] = state.status.value
        if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            fields["reason"] = state.reason
        listing.append(fields)
    return listing


def _launcher_arguments(text: str) -> list[str]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("--launcher-command requires a nonempty JSON list of arguments")
    return cast(list[str], value)


@final
class Arguments(argparse.Namespace):
    command: str = ""
    role: str = ""
    name: str = ""
    prompt: str = ""
    session_dir: str = ""
    slot: str = ""
    phase: str = ""
    kind: str = ""
    lens: str = ""
    reply: str = ""
    dispatch_id: str = ""
    fix_cause: str = ""
    fix_pass: int = 0
    activity: str = ""
    early_ready: str = ""
    launcher_command: str = ""
    json: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "restore", "check"):
        child = commands.add_parser(command)
        if command == "list":
            _ = child.add_argument("--json", action="store_true")
    ending = commands.add_parser("end")
    _ = ending.add_argument("name")
    adding = commands.add_parser("add")
    _ = adding.add_argument("role", choices=staff.WORKER_ROLES)
    _ = adding.add_argument("--name", default="")
    _ = adding.add_argument("--json", action="store_true")
    _ = adding.add_argument("--session-dir", default="")
    assignment = commands.add_parser("assign")
    _ = assignment.add_argument("role", choices=staff.WORKER_ROLES)
    _ = assignment.add_argument("prompt")
    for flag in ("session-dir", "slot", "phase", "kind", "lens", "name", "reply", "dispatch-id", "fix-cause", "activity", "early-ready"):
        aliases = {"name": "--to", "reply": "--reply-file"}
        flags = ["--" + flag, *([aliases[flag]] if flag in aliases else [])]
        _ = assignment.add_argument(*flags, default="", required=flag in ("session-dir", "slot", "phase", "kind"))
    _ = assignment.add_argument("--fix-pass", "--pass", type=int, default=0)
    _ = assignment.add_argument("--launcher-command", required=True, help="JSON list of launcher arguments")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        record = staff.snapshot()
        owner = record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            print("staff: caller: not opened: caller is not a unit or showrunner", file=sys.stderr)
            return ExitCode.REFUSED
        if args.command == "list":
            listing = list_workers(owner.id, record)
            if args.json:
                print(json.dumps(listing))
            else:
                for fields in listing:
                    print(f"{fields['name']} {fields['role']} {fields['state']} {fields.get('activity', '')} {fields.get('reason', '')}".rstrip())
            return ExitCode.OK
        if args.command == "check":
            run = worker_panes.worker_run(owner)
            wrong = [state for state in worker_panes.observe(owner.id, record)
                     if not isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ClosedWorker))
                     or (not isinstance(state, worker_panes.OpenWorker)
                         and isinstance(worker_panes.dispatch_role(state.row, run), worker_panes.WorkerRoleUnavailable))]
            for state in wrong:
                print(f"{state.row.name}: {getattr(state, 'reason', 'dispatch role is unavailable')}")
            return int(bool(wrong))
        if args.command == "restore":
            return restore(owner.id)
        if args.command == "end":
            code = end(owner.id, WorkerName.parse(args.name))
        elif args.command == "add":
            result = add(owner.id, cast(WorkerRole, args.role), name=args.name)
            if args.json and isinstance(result, worker_panes.OpenWorker):
                print(json.dumps(result.row.to_json()))
                return ExitCode.OK
            code = _startup_result(WorkerName.parse(args.role), result)
        else:
            code = assign(owner.id, cast(WorkerRole, args.role), Path(args.prompt), session_dir=Path(args.session_dir),
                slot=args.slot, phase=args.phase, kind=args.kind, lens=args.lens, name=args.name,
                reply=Path(args.reply) if args.reply else REPLY_BESIDE_PROMPT, dispatch_id=args.dispatch_id,
                launcher_command=_launcher_arguments(args.launcher_command), fix_cause=args.fix_cause,
                fix_pass=args.fix_pass, activity=args.activity,
                early_ready=Path(args.early_ready) if args.early_ready else SINGLE_STAGE_REVIEW)
        if code == ExitCode.OK:
            print("done")
        return code
    except (staff.StaffUnreadable, staff.StaffRefused, herdr.HerdrFailure, OSError, ValueError) as error:
        print(f"anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
