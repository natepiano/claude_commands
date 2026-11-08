#!/usr/bin/env python3
"""Admit and publish workers through staff."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from pathlib import Path
import sys

from scripts.production import staff, worker_admission


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
        retirement_code = 0
        if row is not None:
            try:
                if codex_mesh.command_end(argparse.Namespace(session_dir=str(run), to=row.name)) == 0:
                    try:
                        _ = staff.retire(row.id)
                    except staff.StaffRefused as error:
                        print(f"codex_mesh: {row.name}: {error}", file=sys.stderr)
                        retirement_code = 1
            except SystemExit as error:
                print(str(error), file=sys.stderr)
        if (row is not None or (run / "mesh_server.json").exists()) and (stopped := codex_mesh.command_stop(argparse.Namespace(session_dir=str(run)))):
            return retirement_code or stopped
        if retirement_code:
            return retirement_code
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("member", "end-friend"))
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--staff-id", default="")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
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
