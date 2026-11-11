#!/usr/bin/env python3
"""Resume a child showrunner as a unit of its parent."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import shlex
import sys
from dataclasses import dataclass, field, replace
from enum import IntEnum
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from scripts.accounts import accounts
from scripts.herdr import herdr_types
from scripts.herdr import herdr, pane_launch
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import add_unit, production_lifecycle, promote_showrunner, session_staff, showrunners, staff, transfer_unit, unit_config, unit_lookup, update_registration
from scripts.production.production_doc import Production, Refusal, read_production
from scripts.agents import pane_record, remote, stop_resume
from scripts.whoami.account import named_account_environment


class Step(IntEnum):
    PREPARED = 0
    DETACHED = 1
    TIMER_STOPPED = 2
    QUESTIONS_MOVED = 3
    STOPPED = 4
    CONVERTED = 5
    DOCUMENTED = 6
    STARTED = 7
    PLACED = 8


@dataclass(frozen=True)
class Demotion:
    parent: Production
    child: Production
    marked: unit_lookup.LabeledSession
    claude: unit_lookup.LiveClaude
    worktree: Path
    branch: str
    plan: Path
    port: unit_config.AppPort | unit_config.NoAppPort = field(default_factory=unit_config.NoAppPort)


def record_path(parent: Production, name: str) -> Path:
    return promote_showrunner.director_record(parent, name, "demotions")


def check_units(child: Production, record: staff.StaffSnapshot) -> None:
    """Require retired staff units whose branches reached the child's merge branch."""
    rows = record.find(kind="unit", parent=child.owner.id)
    live = [row.name for row in rows if not isinstance(row.worktree, staff.RemovedWorktree)]
    if live:
        raise Refusal(f"{child.name}: live units: {', '.join(live)}")
    resolver = session_staff.RowResolver(record)
    unmerged: list[str] = []
    for row in rows:
        try:
            branch = production_lifecycle.unit_branch(row, resolver)
            if not isinstance(branch, session_staff.Branch):
                print(f"anomaly: {row.name}: {' '.join(branch.reason.splitlines())}", file=sys.stderr)
                continue
            if production_lifecycle.branch_merge_state(child, branch.name) is production_lifecycle.BranchMergeState.UNMERGED:
                unmerged.append(branch.name)
        except (OSError, ValueError, production_lifecycle.LifecycleStop) as error:
            print(f"anomaly: {row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
    if unmerged:
        raise Refusal(f"{child.name}: unmerged branches: {', '.join(unmerged)}")


def live_session(session: str, record: staff.StaffSnapshot) -> unit_lookup.LabeledSession | unit_lookup.NoSession:
    """Read the live pane for the exact retained conversation."""
    return next((item for item in unit_lookup.observed_session_labels(record=record)
                 if isinstance(item.claude, unit_lookup.LiveClaude) and item.claude.session_id == session), unit_lookup.NoSession())


def child_plan(child: Production, plan: Path) -> Path:
    """Resolve the plan inside the child's checkout."""
    plan = plan.expanduser()
    plan = (plan if plan.is_absolute() else child.checkout / plan).resolve()
    try:
        _ = plan.relative_to(child.checkout)
    except ValueError as error:
        raise Refusal("plan must be inside the child checkout") from error
    return plan


def checked(parent: Production, name: str, plan: Path | unit_config.PlanDefault, record: staff.StaffSnapshot,
            port: unit_config.AppPort | unit_config.NoAppPort) -> Demotion:
    try:
        row = showrunners.owner_for(name, record)
    except ValueError as error:
        raise Refusal(f"{name} is not attached to {parent.name}") from error
    if row.parent != staff.ReportsTo(parent.owner.id):
        raise Refusal(f"{name} is not attached to {parent.name}")
    child = read_production(row, record=record)
    if isinstance(plan, unit_config.PlanDefault):
        saved_plan = unit_config.default_plan(child.checkout, child.doc)
        if not isinstance(saved_plan, unit_config.PlanFile):
            raise Refusal(f"plan default is missing; looked in {child.checkout / '.unit.toml'} and {child.doc}")
        plan = saved_plan.path
    plan = child_plan(child, plan)
    if isinstance(port, unit_config.NoAppPort):
        saved = unit_config.read(child.checkout, cleanup=False)
        port = unit_config.default_port(child.checkout, saved.port if isinstance(saved, unit_config.UnitConfig) else port)
    add_unit.require_merge_branch(parent)
    marked = live_session(row.current_session, record)
    if isinstance(marked, unit_lookup.NoSession) or not isinstance(marked.claude, unit_lookup.LiveClaude):
        raise Refusal(f"{name}: cannot read the live pane")
    worktree = child.checkout
    relative_plan = plan.relative_to(child.checkout).as_posix()
    if not plan.is_file():
        raise Refusal(f"plan {relative_plan} is missing in {child.checkout}")
    tracked = add_unit.git(child, "ls-files", "--error-unmatch", "--", relative_plan, check=False)
    if (tracked.returncode == 0
            and add_unit.git(child, "cat-file", "-e", f"{child.merge_branch}:{relative_plan}", check=False).returncode != 0):
        branch = add_unit.git(child, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        raise Refusal(f"plan {relative_plan} is committed only on {branch}; "
                      + f"commit it on {child.merge_branch} or keep it untracked")
    promote_showrunner.check_director(name, marked, marked.claude, worktree, skip_path=plan)
    check_units(child, record)
    return Demotion(parent, child, marked, marked.claude, worktree, child.merge_branch, plan, port)


def saved_request(parent: Production, fields: dict[str, object]) -> Demotion:
    row = staff.StaffRow.from_json(fields["source"])
    child = read_production(row)
    claude = unit_lookup.LiveClaude(str(fields["session"]), row.current_session, str(fields["socket"]), int(str(fields["pid"])))
    marked = unit_lookup.LabeledSession(row.name, claude, herdr.HerdrPlace(str(fields["host"]), str(fields["pane"])))
    return Demotion(parent, child, marked, claude, Path(str(fields["worktree"])), str(fields["branch"]), Path(str(fields["plan"])),
                    unit_config.parse_port_cell(str(fields.get("port", ""))))


def save(path: Path, fields: dict[str, object], step: Step) -> None:
    fields["step"] = step.value
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(fields) + "\n")


def move_questions(request: Demotion) -> None:
    identities = update_registration.QuestionStaff.read()
    child, parent = request.child, request.parent
    update_registration.migrate_questions(child, identities=identities)
    update_registration.migrate_questions(parent, identities=identities)
    paths = sorted([update_registration.outstanding_path(child), update_registration.outstanding_path(parent)])
    with update_registration.outstanding_lock(paths[0]), update_registration.outstanding_lock(paths[1]):
        source = update_registration.open_entries(child, identities)
        destination = update_registration.open_entries(parent, identities)
        keys = {(item.get("asked_by"), item["text"]) for item in destination if "question" in item}
        for item in source:
            if "question" not in item:
                continue
            moved = item.copy()
            moved["asked_by"] = child.owner.id
            key = (child.owner.id, moved["text"])
            if key not in keys:
                destination.append(moved)
                keys.add(key)
        update_registration.write_open_entries(parent, destination, identities)
        update_registration.write_open_entries(child, [item for item in source if "question" not in item], identities)


def unit_document(request: Demotion) -> None:
    """Commit the unit row on the parent's merge branch."""
    parent = request.parent
    lines = parent.doc.read_text(encoding="utf-8").splitlines()
    heading = next((i for i, line in enumerate(lines) if line.strip() == "## Units"), len(lines))
    if heading == len(lines):
        lines.extend(["", "## Units", "", "| Unit | Worktree | Branch | Plan |", "| --- | --- | --- | --- |"])
        heading += 1
    end = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    table = [i for i in range(heading + 1, end) if lines[i].startswith("|")]
    if not table:
        lines[end:end] = ["", "| Unit | Worktree | Branch | Plan |", "| --- | --- | --- | --- |", ""]
        table = [end + 1, end + 2]
    headers = [cell.strip() for cell in lines[table[0]].strip("|").split("|")]
    rows = [[cell.strip() for cell in lines[i].strip("|").split("|")] for i in table[2:]]
    for title in ("Unit", "Worktree", "Branch", "Plan"):
        if title not in headers:
            headers.append(title)
    values = {"Unit": request.child.name, "Worktree": str(request.worktree), "Branch": request.branch,
              "Plan": str(request.plan.relative_to(request.worktree))}
    if isinstance(request.port, unit_config.AppPort):
        if "Port" not in headers:
            headers.append("Port")
        values["Port"] = str(request.port.number)
    elif "Port" in headers:
        values["Port"] = ""
    unit_index = headers.index("Unit")
    selected = next((row for row in rows if len(row) > unit_index and row[unit_index].strip("`") == request.child.name), None)
    if selected is None:
        selected = list[str]()
        rows.append(selected)
    selected.extend([""] * (len(headers) - len(selected)))
    for title, value in values.items():
        selected[headers.index(title)] = value
    rendered = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                *("| " + " | ".join(row + [""] * (len(headers) - len(row))) + " |" for row in rows)]
    lines[table[0]:table[-1] + 1] = rendered
    text = "\n".join(lines) + "\n"
    if text != parent.doc.read_text(encoding="utf-8"):
        write_text_atomic(parent.doc, text)
    _ = add_unit.commit_paths(parent, [parent.doc], f"production({parent.name}): add unit {request.child.name}")


def launch(request: Demotion) -> int:
    plan = request.plan.relative_to(request.worktree)
    config = unit_config.read(request.worktree, cleanup=False)
    desired = (unit_config.UnitConfig(unit_config.PlanFile(plan), request.port, ())
               if isinstance(config, unit_config.ConfigMissing)
               else replace(config, plan=unit_config.PlanFile(plan), port=request.port))
    _ = unit_config.write(request.worktree, desired)
    target = accounts.account_for_group_launch("unit", resume=True)
    port = ["--port", str(request.port.number)] if isinstance(request.port, unit_config.AppPort) else []
    session = request.claude.session_id
    flag = "--resume" if (isinstance(live_session(session, staff.snapshot()), unit_lookup.NoSession)
                         or add_unit.pending_opening(request.parent, request.child.name, session)) else "--attach"
    return add_unit.main(["--production", request.parent.name, request.child.name, "--plan", str(plan),
                          flag, session, "--cwd", str(request.worktree), "--config-dir", str(target.dir), *port])


def restore_line(path: Path) -> str:
    code = "from pathlib import Path; from scripts.production.demote_showrunner import restore; raise SystemExit(restore(Path(" + repr(str(path)) + ")))"
    return "~/.claude/scripts/lib/py -c " + shlex.quote(code)


def restore(path: Path) -> int:
    """Restore the saved showrunner role and its retained launch."""
    fields = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    source = staff.StaffRow.from_json(fields["source"])
    assert isinstance(source.parent, staff.ReportsTo) and isinstance(source.doc, staff.LiveDoc)
    with staff.operation():
        result = stop_resume.stop(source.current_session)
        if not isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
            return 1
        _ = staff.change_director(source.id, staff.ShowrunnerRole(source.name, source.parent.id, source.doc))
        for identity in cast(list[str], fields["directors"]):
            _ = staff.move(staff.StaffId(identity), staff.ReportsTo(source.id))
        entry = cast(pane_launch.PaneLaunch, fields["launch"])
        environment = entry["environment"].copy()
        if isinstance(source.home, staff.SessionHome):
            environment.update(named_account_environment(source.home.path))
        outcome = herdr.launch_agent(herdr.shared_session(), source.name, entry["name"] or source.name,
                                     entry["cwd"], environment, "claude", pane_record.resume_arguments(entry),
                                     herdr.NoOpeningPrompt(), 90_000, agent=source.name)
        started = outcome if isinstance(outcome, herdr.nix_env.Refused) else outcome.outcome
        if isinstance(started, (herdr.nix_env.Refused, herdr.PromptNotSubmitted)):
            print(started.reason, file=sys.stderr)
            return 1
        production = read_production(source)
        if isinstance(update_registration.update_schedule(production.doc.read_text().splitlines()), update_registration.ScheduledUpdates):
            _ = update_registration.notifier(["start", f"showrunner-{source.id}"])
        path.unlink()
    return 0


def apply(request: Demotion, path: Path, fields: dict[str, object]) -> int:
    completed = Step(int(str(fields["step"])))
    if completed < Step.DETACHED:
        if showrunners.main(["detach", request.child.name]) != 0:
            return 1
        save(path, fields, Step.DETACHED)
    if completed < Step.TIMER_STOPPED:
        timer = f"showrunner-{request.child.owner.id}"
        try:
            _ = update_registration.notifier(["stop", timer])
        except update_registration.RegistrationFailure:
            if not isinstance(update_registration.instance(timer), update_registration.InstanceAbsent):
                raise
        save(path, fields, Step.TIMER_STOPPED)
    if completed < Step.QUESTIONS_MOVED:
        move_questions(request)
        save(path, fields, Step.QUESTIONS_MOVED)
    if completed < Step.STOPPED:
        if not promote_showrunner.stop_director(request.child.name, request.marked, request.claude):
            print(f"{request.child.name}: stop is not proved; demotion remains pending", file=sys.stderr)
            return 1
        save(path, fields, Step.STOPPED)
    if completed < Step.CONVERTED:
        _ = add_unit.git(request.child, "switch", request.branch)
        _ = staff.change_director(request.child.owner.id, staff.UnitRole(request.child.name, request.parent.owner.id,
                                 staff.PresentWorktree(remote.local_machine(), request.worktree)))
        save(path, fields, Step.CONVERTED)
    if completed < Step.DOCUMENTED:
        unit_document(request)
        save(path, fields, Step.DOCUMENTED)
    if completed < Step.STARTED:
        try:
            started = launch(request)
        except (OSError, ValueError, Refusal, accounts.AccountsError, herdr_types.HerdrFailure, staff.StaffRefused, staff.StaffUnreadable) as error:
            print(f"{request.child.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
            started = 1
        if started:
            print(f"{request.child.name}: unit restart failed. Restore the showrunner with: {restore_line(path)}", file=sys.stderr)
            return 1
        save(path, fields, Step.STARTED)
    if completed < Step.PLACED:
        row = staff.by_id(request.child.owner.id)
        assert row is not None
        transfer_unit.place_unit(request.child.name, transfer_unit.UnitTransfer(row, request.parent.owner, request.parent, row.name))
        save(path, fields, Step.PLACED)
    path.unlink()
    print(f"{request.child.name} is now a unit of {request.parent.name}")
    return 0


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("child")
    _ = parser.add_argument("--plan", default=unit_config.PlanDefault(), type=Path)
    _ = parser.add_argument("--check", action="store_true")
    _ = parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    record = staff.snapshot()
    parent = read_production(showrunners.own_name(), record=record)
    name = cast(str, args.child)
    path = record_path(parent, name)
    plan = cast(Path | unit_config.PlanDefault, args.plan)
    raw_port = cast(int | None, args.port)
    port = unit_config.NoAppPort() if raw_port is None else unit_config.AppPort(raw_port)
    fields: dict[str, object] = {}
    if path.exists():
        add_unit.require_merge_branch(parent)
        fields = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        if Step(int(str(fields["step"]))) == Step.PLACED:
            path.unlink()
            fields = {}
    if fields:
        request = saved_request(parent, fields)
        if ((isinstance(plan, Path) and child_plan(request.child, plan) != request.plan)
                or (raw_port is not None and port != request.port)):
            raise Refusal(f"{name}: the pending demotion uses {request.plan} and port {request.port}")
    else:
        request = checked(parent, name, plan, record, port)
    port = request.port
    if cast(bool, args.check):
        print(f"check: {name} can become a unit of {parent.name}")
        return 0
    if not fields:
        entry = pane_record.publication_for_session(request.claude.session_id, record=record)
        retained: pane_launch.PaneLaunch = entry.entry if entry is not None else {
            "kind": "claude", "pane_id": request.marked.place.pane, "session_id": request.claude.session_id,
            "cwd": str(request.worktree), "name": request.claude.name, "model": "", "remote_control": False,
            "environment": {}, "flags_read": False, "written_at": datetime.now(UTC).isoformat(),
        }
        fields = {"source": request.child.owner.to_json(), "session": request.claude.name, "socket": request.claude.socket,
                  "pid": request.claude.pid, "host": request.marked.place.session, "pane": request.marked.place.pane,
                  "worktree": str(request.worktree), "branch": request.branch, "plan": str(request.plan), "port": str(port.number) if isinstance(port, unit_config.AppPort) else "",
                  "launch": retained, "directors": [row.id for row in record.rows
                      if row.parent == staff.ReportsTo(request.child.owner.id) and row.kind in {"unit", "showrunner"}]}
        save(path, fields, Step.PREPARED)
    return apply(request, path, fields)


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            try:
                return _main(argv)
            except (Refusal, unit_config.ConfigRefused) as error:
                print(f"demote_showrunner: {error}", file=sys.stderr)
                return 2
            except (OSError, ValueError, accounts.AccountsError, update_registration.RegistrationFailure, herdr_types.HerdrFailure) as error:
                print(f"demote_showrunner: {' '.join(str(error).splitlines())}", file=sys.stderr)
                return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
