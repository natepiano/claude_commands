#!/usr/bin/env python3
"""Show one staff member without changing its records or its session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.dont_write_bytecode = True
from scripts.production import staff, unit_config
from scripts.agents import remote
from scripts.herdr import herdr


NOT_APPLICABLE = "not applicable"


def unavailable(cause: object) -> str:
    """Keep each field and each error on one line."""
    return "unavailable: " + (" ".join(str(cause).split()) or "evidence is unreadable")


@dataclass(frozen=True)
class MemberDetails:
    """Machine-local facts with an explicit result for each field."""

    branch: str
    plan: str
    tab: str


def read_branch(worktree: Path) -> str:
    """Read the branch from git without refreshing the index."""
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False, timeout=herdr.PROGRAM_TIMEOUT_SECONDS,
    )
    if result.returncode or not result.stdout.strip():
        return unavailable(result.stderr.strip() or "git did not return a branch")
    return result.stdout.strip()


def read_config(row: staff.StaffRow) -> unit_config.UnitConfig | unit_config.ConfigMissing:
    """Read the unit configuration without removing temporary files."""
    if isinstance(row.worktree, staff.RemovedWorktree):
        return unit_config.read_parked(row.id, cleanup=False)
    if isinstance(row.worktree, staff.PresentWorktree):
        return unit_config.read(row.worktree.path, cleanup=False)
    return unit_config.ConfigMissing("the worktree is not recorded")


def read_tab(row: staff.StaffRow) -> str:
    """Use the shared native identity reader instead of retained pane labels."""
    if isinstance(row.worktree, staff.RemovedWorktree) or isinstance(row.doc, staff.WrappedDoc):
        return NOT_APPLICABLE
    client = herdr.Herdr(herdr.shared_session())
    panes = {pane["pane_id"]: pane for pane in client.panes()}
    matches: list[str] = []
    unknown: list[str] = []
    agents = {agent["pane_id"]: agent.get("agent") for agent in client.agents()}
    for pane_id, pane in panes.items():
        kind = agents.get(pane_id)
        name = pane.get("label") or pane_id
        try:
            if kind not in ("claude", "codex"):
                if isinstance(client.pane_activity(pane_id), herdr.PaneAtShell):
                    continue
                raise herdr.HerdrFailure("the foreground process identity is unknown")
            identity = client.native_session(pane_id, kind, herdr.PROGRAM_TIMEOUT_SECONDS)
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError):
            unknown.append(" ".join(name.split()))
            continue
        if identity == row.current_session:
            matches.append(f"{name} (herdr:{client.session}:{pane_id})")
    if unknown:
        print(f"anomaly: {len(unknown)} panes unreadable ({', '.join(unknown)}); not blocking", file=sys.stderr)
    if len(matches) == 1:
        return matches[0]
    if matches:
        return unavailable("duplicate live panes: " + ", ".join(matches))
    return f"none found ({len(unknown)} panes unreadable)" if unknown else NOT_APPLICABLE


def local_details(row: staff.StaffRow, holder: staff.StaffRow) -> MemberDetails:
    """Read facts on the machine that holds the member."""
    branch, plan = NOT_APPLICABLE, NOT_APPLICABLE
    if holder.kind == "unit":
        try:
            config = read_config(holder)
        except (OSError, ValueError) as error:
            config = unit_config.ConfigMissing(str(error))
        if isinstance(config, unit_config.ConfigMissing):
            plan = unavailable(config.reason)
        elif isinstance(config.plan, unit_config.PlanFile):
            plan = str(config.plan.path)
        else:
            plan = "standby" if isinstance(config.plan, unit_config.StandbyPlan) else "permanent"
        if isinstance(holder.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            if (isinstance(holder.worktree, staff.RemovedWorktree) and not holder.worktree.path.exists()
                    and isinstance(config, unit_config.UnitConfig)
                    and isinstance(config.retired, unit_config.RetiredSession)):
                branch = config.retired.branch.value + " (parked)"
            else:
                try:
                    branch = read_branch(holder.worktree.path)
                except (OSError, subprocess.SubprocessError) as error:
                    branch = unavailable(error)
        else:
            branch = unavailable("the worktree is not recorded")
    elif isinstance(holder.doc, (staff.LiveDoc, staff.WrappedDoc)):
        plan = str(holder.doc.path)
    else:
        plan = unavailable("the plan document is not recorded")
    try:
        tab = read_tab(row)
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        tab = unavailable(error)
    return MemberDetails(branch, plan, tab)


def unavailable_details(row: staff.StaffRow, holder: staff.StaffRow, cause: object) -> MemberDetails:
    reason = unavailable(cause)
    retired = isinstance(row.worktree, staff.RemovedWorktree) or isinstance(row.doc, staff.WrappedDoc)
    return MemberDetails(reason if holder.kind == "unit" else NOT_APPLICABLE,
                         str(holder.doc.path) if isinstance(holder.doc, (staff.LiveDoc, staff.WrappedDoc)) else reason,
                         NOT_APPLICABLE if retired else reason)


def read_details(row: staff.StaffRow, holder: staff.StaffRow, machine: str) -> MemberDetails:
    """Read remote facts through the same readers without another staff snapshot."""
    if machine == remote.local_machine():
        return local_details(row, holder)
    destination = remote.other_machine()
    if machine != destination:
        return unavailable_details(row, holder, f"machine {machine} has no configured connection")
    code = (
        "import sys,json; from dataclasses import asdict; "
        "from scripts.production.unit_command import staff,local_details; "
        "rows=json.load(sys.stdin); "
        "print(json.dumps(asdict(local_details(*(staff.StaffRow.from_json(row) for row in rows)))))"
    )
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", destination,
               "~/.claude/scripts/lib/py -B -c " + shlex.quote(code)]
    try:
        result = subprocess.run(command, input=json.dumps([row.to_json(), holder.to_json()]),
                                capture_output=True, text=True, check=False,
                                timeout=herdr.PROGRAM_TIMEOUT_SECONDS)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode:
            raise ValueError(f"{machine} did not answer: {result.stderr.strip() or result.returncode}")
        value = cast(object, json.loads(result.stdout))
        if not isinstance(value, dict):
            raise ValueError(f"{machine} returned invalid member details")
        fields = cast(dict[str, object], value)
        values = [fields.get(key) for key in ("branch", "plan", "tab")]
        if not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"{machine} returned incomplete member details")
        return MemberDetails(*cast(list[str], values))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return unavailable_details(row, holder, error)


def describe(row: staff.StaffRow, record: staff.StaffSnapshot) -> str:
    """Resolve ownership through the supplied snapshot."""
    parent = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
    parent_name = parent.name if parent is not None else (
        NOT_APPLICABLE if isinstance(row.parent, staff.StaffRoot) else unavailable(f"parent {row.parent} is missing"))
    holder = parent if row.kind == "worker" and parent is not None else row
    machine, worktree = unavailable("the machine is not recorded"), NOT_APPLICABLE
    if isinstance(holder.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
        machine, worktree = holder.worktree.machine, str(holder.worktree.path)
        if row.kind == "worker":
            worktree += " (inherited)"
    elif isinstance(holder.doc, (staff.LiveDoc, staff.WrappedDoc)):
        machine = holder.doc.machine
    elif holder.kind == "unit":
        worktree = unavailable("the worktree is not recorded")
    if machine.startswith("unavailable:"):
        details = unavailable_details(row, holder, machine.removeprefix("unavailable: "))
    else:
        details = read_details(row, holder, machine)
    fields = (
        ("Name", row.name), ("Kind", row.kind), ("Id", row.id), ("Parent", parent_name),
        ("Machine", machine), ("Worktree", worktree), ("Branch", details.branch),
        ("Plan", details.plan), ("Live tab", details.tab), ("Current session", row.current_session),
    )
    return "\n".join(f"{label}: {' '.join(value.split()) or unavailable('the value is empty')}"
                     for label, value in fields)


def main(argv: list[str] | None = None, *, record: staff.StaffSnapshot | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments in (["--help"], ["-h"]):
        print("usage: unit <id or unique prefix>")
        return 0
    if len(arguments) != 1 or not arguments[0].strip() or arguments[0].startswith("-"):
        print("usage: unit <id or unique prefix>", file=sys.stderr)
        return 2
    target = arguments[0]
    try:
        record = staff.snapshot() if record is None else record
        exact = record.by_member_reference(target)
        matches = [exact] if exact is not None else [
            row for row in record.rows if row.id.startswith(target) or row.current_session.startswith(target)
        ]
        if not matches:
            print(f"unknown id: {target}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            names = ", ".join(f"{' '.join(row.name.split())} ({row.id})"
                              for row in sorted(matches, key=lambda item: item.id)[:5])
            if len(matches) > 5:
                names += f", and {len(matches) - 5} more"
            print(f"ambiguous id: {target}: {names}", file=sys.stderr)
            return 1
        print(describe(matches[0], record))
    except staff.StaffUnreadable as error:
        print(unavailable(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
