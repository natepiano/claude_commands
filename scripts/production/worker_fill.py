#!/usr/bin/env python3
"""Admit and publish workers through staff."""
from __future__ import annotations

import argparse
import os
import json
from typing import cast
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, session_staff
from scripts.production import worker_admission
from scripts.lib import conf
from scripts.agents import worker_lifecycle


from scripts.production.worker_admission import (
    AdmissionRefused as AdmissionRefused, WorkerStartAllowed as WorkerStartAllowed, PublishedWorker as PublishedWorker, admit as admit, publish as publish,
)


def fill(*, dry_run: bool = False) -> int:
    """Staff already holds every published worker."""
    del dry_run
    print("unchanged")
    return 0


def launch_claude(name: str, run: Path, role: str, command: list[str], prompt: Path, reference: str = "") -> int:
    """Keep the row lock in the parent until the Claude launcher dispatches."""
    allowed = admit(name, run, role=role, reference=reference)
    request = worker_admission.assignment_id(prompt)
    processes: list[subprocess.Popen[bytes]] = []
    parent, child = socket.socketpair()
    with parent, child:
        with parent.makefile("rwb", buffering=0) as control:
            def receive() -> bytes:
                remaining = worker_lifecycle.effective_deadline() - time.monotonic()
                if remaining <= 0:
                    raise worker_lifecycle.OperationDeadlineExpired("Claude launch deadline expired")
                parent.settimeout(min(conf.read("direct").whole("CLAUDE_WORKER_REGISTER_WAIT_SECS"), remaining))
                return control.readline()

            def start(handoff: worker_admission.FamilyHandoff) -> str:
                environment = staff.child_environment(os.environ)
                environment["WORKER_SWITCH_FD"] = str(child.fileno())
                if isinstance(handoff, worker_admission.TranscriptHandoff):
                    prepared = run / f"{allowed.lock_reference}.opening.md"
                    _ = prepared.write_text(handoff.opening(prompt.read_text()))
                    environment["WORKER_SWITCH_PROMPT"] = str(prepared)
                processes.append(subprocess.Popen(command, env=environment, pass_fds=(child.fileno(),)))
                child.close()
                identity = receive().decode().strip()
                if not identity:
                    raise worker_admission.AdmissionRefused("Claude launcher ended before native identity proof")
                return identity

            def dispatch(published: PublishedWorker) -> None:
                if not processes:
                    previous = worker_admission.dispatch_record(allowed)
                    if previous.get("dispatch_state") == "sending":
                        source = worker_admission.transcript_source(published.row, staff.ClaudeBackend())
                        torn = False
                        for line in source.read_text().splitlines():
                            try:
                                fields = cast(object, json.loads(line))
                            except json.JSONDecodeError:
                                torn = True
                                continue
                            if isinstance(fields, dict) and (entry := worker_admission.transcript_entry(cast(dict[str, object], fields), set())).startswith("user:\n") and entry.endswith(prompt.read_text()):
                                return
                        if torn:
                            raise worker_admission.AdmissionRefused("Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused")
                    command.insert(2, "--attach")
                    _ = start(published.handoff)
                if worker_admission.dispatch_record(allowed).get("assignment_id") == request:
                    worker_admission.record_dispatch(allowed, request, "sending")
                _ = control.write(b"published\n")
                if receive() != b"dispatched\n":
                    raise worker_admission.AdmissionRefused("Claude launcher ended before dispatch")

            def cleanup(identity: str) -> None:
                del identity
                _ = control.write(b"failed\n")

            _ = worker_admission.switch_worker(allowed, "claude", start=start, dispatch=dispatch, cleanup=cleanup, request=request)
    return processes[0].wait() if processes else 0


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False
    name: str = ""
    run: Path = Path(".")
    staff_id: str = ""
    prompt: Path = Path(".")
    role: str = ""
    launcher: list[str] = []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("fill", "admit", "member", "launch-claude"))
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--staff-id", default="")
    _ = parser.add_argument("--prompt", type=Path, default=Path("."))
    _ = parser.add_argument("--role")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    _ = parser.add_argument("--launcher", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "launch-claude":
            return launch_claude(args.name, args.run, args.role, args.launcher, args.prompt, args.staff_id)
        if args.command == "member":
            row = (staff.local_snapshot().by_id(staff.StaffId(args.staff_id)) if args.staff_id
                   else worker_admission.member(args.run, args.name))
            if row is None:
                raise AdmissionRefused(f"{args.name}: no worker staff row")
            print(session_staff.worker_family(staff.local_snapshot(), row.id), row.current_session, row.id)
            return 0
        if args.command == "admit":
            _ = admit(args.name, args.run, role=args.role or "", reference=args.staff_id)
            return 0
        return fill(dry_run=args.dry_run)
    except (AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
