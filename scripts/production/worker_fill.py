#!/usr/bin/env python3
"""Admit and publish workers through staff."""
from __future__ import annotations

import argparse
import json
from typing import cast
from pathlib import Path
import select
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, session_staff
from scripts.production import worker_admission
from scripts.lib import conf
from scripts.agents import worker_lifecycle


from scripts.production.worker_admission import AdmissionRefused, PublishedWorker, SwitchState


def coordinate_claude(name: str, run: Path, role: str, prompt: Path, reference: str = "") -> int:
    """Hold the row until the Bash parent confirms dispatch."""
    allowed = worker_admission.admit(name, run, role=role, reference=reference)
    request = worker_admission.assignment_id(prompt)
    started = False

    def receive() -> str:
        remaining = min(conf.read("direct").whole("CLAUDE_WORKER_REGISTER_WAIT_SECS"),
                        worker_lifecycle.effective_deadline() - time.monotonic())
        if remaining <= 0 or not select.select([sys.stdin], [], [], remaining)[0]:
            raise worker_lifecycle.OperationDeadlineExpired("Claude launch deadline expired")
        value = sys.stdin.readline().strip()
        if not value:
            raise AdmissionRefused("Claude launcher ended before dispatch")
        return value

    def start(handoff: worker_admission.FamilyHandoff) -> str:
        nonlocal started
        prepared = prompt
        if isinstance(handoff, worker_admission.TranscriptHandoff):
            prepared = run / f"{allowed.lock_reference}.opening.md"
            _ = prepared.write_text(handoff.opening(prompt.read_text()))
        print("launch" if not started else "attach", prepared, sep="\n", flush=True)
        started = True
        return receive()

    def dispatch(published: PublishedWorker) -> SwitchState:
        nonlocal started
        if not started:
            if worker_admission.dispatch_record(allowed).state is SwitchState.DISPATCH_DEFERRED:
                source = worker_admission.transcript_source(published.row, staff.ClaudeBackend())
                torn = False
                for line in source.read_text().splitlines():
                    try:
                        fields = cast(object, json.loads(line))
                    except json.JSONDecodeError:
                        torn = True
                        continue
                    if isinstance(fields, dict) and (entry := worker_admission.transcript_entry(cast(dict[str, object], fields), set())).startswith("user:\n") and entry.endswith(prompt.read_text()):
                        print("unchanged", flush=True)
                        return SwitchState.ASSIGNMENT_ACCEPTED
                if torn:
                    raise AdmissionRefused("Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused")
            started = True
            _ = start(published.handoff)
        if worker_admission.dispatch_record(allowed).assignment_id == request:
            worker_admission.record_dispatch(allowed, request, SwitchState.DISPATCH_DEFERRED)
        print("published", flush=True)
        if receive() != "dispatched":
            raise AdmissionRefused("Claude launcher ended before dispatch")
        return SwitchState.ASSIGNMENT_ACCEPTED

    def cleanup(identity: str) -> None:
        del identity
        print("failed", flush=True)

    _ = worker_admission.switch_worker(allowed, "claude", start=start, dispatch=dispatch, cleanup=cleanup, request=request)
    return 0


class _Arguments(argparse.Namespace):
    command: str = ""
    name: str = ""
    run: Path = Path(".")
    staff_id: str = ""
    prompt: Path = Path(".")
    role: str = ""
    allow_absent: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("member", "coordinate-claude"))
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--staff-id", default="")
    _ = parser.add_argument("--prompt", type=Path, default=Path("."))
    _ = parser.add_argument("--role")
    _ = parser.add_argument("--allow-absent", action="store_true")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "coordinate-claude":
            return coordinate_claude(args.name, args.run, args.role, args.prompt, args.staff_id)
        row = (staff.local_snapshot().by_id(staff.StaffId(args.staff_id)) if args.staff_id
               else worker_admission.member(args.run, args.name))
        if row is None:
            if args.allow_absent:
                print("gone")
                return 0
            raise AdmissionRefused(f"{args.name}: no worker staff row")
        print(session_staff.worker_family(staff.local_snapshot(), row.id), row.current_session, row.id)
        return 0
    except (AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
