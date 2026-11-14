"""Give a producer role to the conversation that runs the command."""
from __future__ import annotations

import os
from pathlib import Path

from scripts.accounts import accounts
from scripts.agents import member_account, pane_record, remote, restart, shutdown_record, stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.lib import models_registry
from scripts.production import staff


def _producer(record: staff.StaffSnapshot, name: str) -> staff.StaffRow:
    owners = [row for row in record.rows if row.kind == "producer" and row.name == name]
    if len(owners) != 1:
        raise ValueError(f"{name}: {'several producer rows match' if owners else 'no producer row'}")
    return owners[0]


def _take(name: str, caller: str, pane: str, client: herdr.Herdr, model: str, effort: str) -> staff.StaffRow:
    """Run the transfer steps under the row lock. Each step is safe to run again."""
    record = staff.initial_snapshot()
    row = _producer(record, name)
    # The row id can be the caller's own id: the first conversation of a producer takes its role back.
    member = next((other for other in record.rows
                   if other.id != row.id and caller in (other.id, other.current_session)), None)
    if member is not None:
        raise ValueError(f"this conversation is the {member.kind} {member.name}; a staff member cannot take a producer role")
    machine, _folder = shutdown_record.row_folder(row, record)
    if machine != remote.local_machine():
        raise ValueError(f"{name} runs on {machine}; run the transfer there")
    entry = stop_resume.producer_adoption_launch(caller, pane, client)
    if not entry["pane_id"]:
        raise ValueError("this conversation has no herdr pane")
    native = staff.native_id(entry["session_id"])
    kind = pane_launch.saved_kind(entry)
    home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
    if not isinstance(home, staff.SessionHome):
        raise ValueError("the account of this conversation is unavailable")
    account = member_account.resume_account(kind, row)
    if isinstance(account, accounts.NamedAccount) and account.dir.resolve() != home.path.resolve():
        raise ValueError(f"{name} runs on {account.label}; first run: agents move {native} {account.label}")
    folder = Path(entry["cwd"])
    if model:
        settings = models_registry.producer_settings(name, kind, model, effort)
    else:
        settings = models_registry.launch_settings("producers", name)
        if settings.kind != kind:
            raise ValueError(f"{name} uses {settings.kind}; give --model and --effort for {kind}")
    if row.current_session != caller:
        old_panes = [old for _row, old in restart.row_panes([row], client)]
        if not restart.stop_row(row, client):
            raise ValueError(f"{name}: the old conversation {row.current_session} did not stop")
        print(f"{name}: the old conversation stopped; read it with: "
              + f"~/.claude/scripts/lib/py ~/.claude/scripts/agents/catch_up.py {row.current_session}")
        for old in old_panes:
            if not client.close_retained_pane(old):
                raise ValueError(f"{name}: the old pane {old} did not close")
    if model:
        _ = models_registry.store_producer(name, kind, model, effort)
        # The stored row replaces a launch configuration that this process inherited.
        _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
    row = staff.transfer_producer(row.id, native, home, staff.PresentWorktree(remote.local_machine(), folder))
    entry["name"] = name
    # The pane record is a producer publication after the staff write: the row names this conversation.
    with pane_launch.producer_launch_request(pane_launch.ProducerLaunchRequest(row.id, native, kind, folder, home.path)):
        pane_record.write(client.session, entry)
        row = pane_launch.staff_admitted(row)
    binding = pane_record.read(client.session).get(entry["pane_id"])
    if binding is None or binding.get("staff_id") != row.id or not binding.get("producer_binding", False):
        raise ValueError(f"{name}: the pane publication is unproved; run the transfer again")
    # The tab takes the producer name and the saved position of the producer's old tab.
    _ = client.retitle_row(str(row.id))
    client.remember_row_tab(str(row.id), entry["pane_id"])
    print(f"{name}: session {native} holds the role; {kind}:{settings.model}:{settings.effort}, folder {folder}")
    return row


def transfer(name: str, *, model: str = "", effort: str = "") -> int:
    """Move the producer row to the caller, then restart the caller as the producer."""
    if bool(model) != bool(effort):
        raise ValueError("give --model and --effort together")
    caller, pane = staff.runtime_id(), os.environ.get("HERDR_PANE_ID", "")
    if not caller or not pane:
        raise ValueError("run the transfer in the conversation that takes the role, in its herdr pane")
    client = herdr.Herdr(herdr.shared_session())
    with staff.slot_operation("producer-first-" + name, name):
        row = _producer(staff.initial_snapshot(), name)
        with staff.row_operation(str(row.id)):
            row = _take(name, caller, pane, client, model, effort)
    # The restart takes the row lock in a process outside this conversation.
    return restart.restart(stop_all=True, only=(str(row.id),), why=f"agents transfer {name} ran")
