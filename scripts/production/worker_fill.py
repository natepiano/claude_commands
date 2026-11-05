#!/usr/bin/env python3
"""Admit and publish workers through staff."""
from __future__ import annotations

import argparse
from pathlib import Path
import select
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, worker_admission
from scripts.message import sessions
from scripts.lib import conf
from scripts.agents import worker_lifecycle


def coordinate_claude(name: str, run: Path, prompt: Path, reference: str = "") -> int:
    """Hold the row until the Bash parent confirms dispatch."""
    wait_seconds = conf.read("direct").whole("CLAUDE_WORKER_REGISTER_WAIT_SECS")
    allowed = worker_admission.admit(name, run, role="", reference=reference)
    request = worker_admission.assignment_id(prompt)
    started = False

    def receive(stage: str = "dispatch") -> str:
        remaining = worker_lifecycle.effective_deadline(time.monotonic() + wait_seconds) - time.monotonic()
        if remaining <= 0 or not select.select([sys.stdin], [], [], remaining)[0]:
            raise worker_lifecycle.OperationDeadlineExpired("Claude launch deadline expired")
        value = sys.stdin.readline().strip()
        if not value:
            raise worker_admission.AdmissionRefused(f"Claude launcher ended before {stage}; retry the assignment")
        return value

    def start(handoff: worker_admission.FamilyHandoff, action: str = "launch") -> str:
        nonlocal started
        prepared = prompt
        if isinstance(handoff, worker_admission.TranscriptHandoff):
            prepared = run / f"{allowed.lock_reference}.opening.md"
            _ = prepared.write_text(handoff.opening(prompt.read_text()))
        print(action, prepared, sep="\n", flush=True)
        started = True
        identity = receive("native identity proof")
        if action == "attach":
            return ""
        deadline = time.monotonic() + wait_seconds
        while True:
            registry = sessions.read_registry()
            if registry.unreadable:
                raise worker_admission.AdmissionRefused("staff: registry is incomplete; native identity not published")
            found = [item.record for item in registry.entries
                     if item.record["sessionId"].startswith(identity) and sessions.running_session(item.record)]
            remaining = worker_lifecycle.effective_deadline(deadline) - time.monotonic()
            if found or remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
        if len(found) != 1:
            raise worker_admission.AdmissionRefused("staff: backend has no unique live registry identity")
        if staff.runtime_id(found[0]["pid"]) != found[0]["sessionId"]:
            raise worker_admission.AdmissionRefused("staff: Claude process changed during publication")
        return found[0]["sessionId"]

    def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
        if not started:
            if worker_admission.dispatch_record(allowed, report_anomaly=False).state is worker_admission.SwitchState.DISPATCH_IN_FLIGHT:
                source = worker_admission.transcript_source(published.row, staff.ClaudeBackend())
                torn = False
                text = prompt.read_text()
                for entry in worker_admission.transcript_entries(source, set()):
                    if isinstance(entry, worker_admission.TornTranscriptRecord):
                        torn = True
                    elif entry.startswith("user:\n") and entry.endswith(text):
                        print("unchanged", flush=True)
                        return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED
                if torn:
                    raise worker_admission.AdmissionRefused("Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused")
            _ = start(published.handoff, "attach")
        worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT, expected_request=request)
        print("published", published.row.current_session, flush=True)
        if receive() != "dispatched":
            raise worker_admission.AdmissionRefused("Claude launcher ended before dispatch")
        return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

    _ = worker_admission.switch_worker(allowed, "claude", start=start, dispatch=dispatch,
                                       cleanup=lambda identity: None, request=request)
    return 0


def end_friend(run: Path) -> int:
    """Stop the friend before recording the consultation as ended."""
    if not (run / "consult_agent").exists():
        print(f"end-friend: no friend was launched in {run}")
        return 0
    row = worker_admission.member(run, (run / "friend_name").read_text().strip())
    backend = staff.backend(row) if row is not None else staff.BACKEND_UNAVAILABLE
    family = "" if isinstance(backend, staff.BackendUnavailable) else backend.family
    if family == "codex" or row is None:
        from scripts.agents import codex_mesh
        if row is not None:
            try:
                _ = codex_mesh.command_end(argparse.Namespace(session_dir=str(run), to=row.name))
            except SystemExit as error:
                print(str(error), file=sys.stderr)
        if (row is not None or (run / "mesh_server.json").exists()) and (stopped := codex_mesh.command_stop(argparse.Namespace(session_dir=str(run)))):
            return stopped
    elif family == "claude":
        from scripts.shutdown import stop_resume, stop_results
        result = stop_resume.stop(row.id)
        if not isinstance(result, (stop_results.Stopped, stop_results.NotRunning)):
            raise worker_admission.AdmissionRefused(f"{row.id}: friend stop is unconfirmed; inspect the session and retry")
    else:
        raise worker_admission.AdmissionRefused(f"{row.id}: friend family is unknown; inspect the session and retry")
    _ = (run / "status").write_text("ended\n")
    return 0


class _Arguments(argparse.Namespace):
    command: str = ""
    name: str = ""
    run: Path = Path(".")
    staff_id: str = ""
    prompt: Path = Path(".")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("member", "coordinate-claude", "end-friend"))
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--staff-id", default="")
    _ = parser.add_argument("--prompt", type=Path, default=Path("."))
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "coordinate-claude":
            return coordinate_claude(args.name, args.run, args.prompt, args.staff_id)
        if args.command == "end-friend":
            return end_friend(args.run)
        row = (staff.by_id(staff.StaffId(args.staff_id)) if args.staff_id
               else worker_admission.member(args.run, args.name))
        if row is None:
            raise worker_admission.AdmissionRefused(f"{args.name}: no worker staff row")
        backend = staff.backend(row)
        print("" if isinstance(backend, staff.BackendUnavailable) else backend.family, row.current_session)
        return 0
    except (worker_admission.AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
