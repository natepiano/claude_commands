"""Transfer worker routes between runs with the same unit staff owner."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import time
from typing import cast

from scripts.agents import codex_mesh, worker_lifecycle
from scripts.direct import workers as registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import session_staff, staff, worker_admission


def object_fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def text_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def worker_record(run: Path, name: str) -> dict[str, object]:
    entry = object_fields(session_staff.read_worker_roster(run / "mesh_roster.json").get(name))
    if not entry:
        raise registry.WorkerUnavailable(f"{name}: no source worker")
    return entry


def adopt_unit_worker(source: Path, destination: Path, name: str) -> str:
    """Move an idle worker route and retain its fixed staff inbox."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise registry.WorkerUnavailable("the source and destination runs must differ")
    initial = worker_record(source, name)
    reference = staff.StaffId(text_field(initial.get("staff_id")))
    allowed = worker_admission.admit(name, destination, reference=reference)
    store = registry.ledger_for_run(source)
    if store.path != registry.ledger_for_run(destination).path:
        raise registry.WorkerUnavailable("adoption requires the same unit staff owner")
    with staff.row_operation(allowed.lock_reference), store.locked() as ledger:
        with worker_lifecycle.membership_operation(source, destination), ExitStack() as locks:
            for folder in sorted((source, destination)):
                _ = locks.enter_context(worker_lifecycle.lock_worker(folder, name))
            before = dict(worker_record(source, name))
            target_path = destination / "mesh_roster.json"
            target = object_fields(session_staff.read_worker_roster(target_path).get(name))
            if before.get("adopted_to"):
                if (before["adopted_to"] == str(destination) and target.get("staff_id") == reference
                        and target.get("adoption_id") == before.get("adoption_id")):
                    return name
                raise registry.WorkerUnavailable(f"{name}: already adopted to {before['adopted_to']}")
            if before.get("staff_id") != reference:
                raise registry.WorkerUnavailable(f"{name}: source identity changed")
            if target:
                raise registry.WorkerUnavailable(f"{name}: destination already has a worker")
            worker = ledger.workers.get(name)
            paused = worker is not None and isinstance(worker.ownership, registry.Paused)
            membership = staff.local_snapshot()
            reason = codex_mesh.worker_idle_reason(str(source), name, before, paused=paused, adopting=True,
                                                   membership=membership)
            if reason:
                raise registry.WorkerUnavailable(f"{name}: {reason}")
            row = membership.by_id(reference)
            if row is None or row.parent != staff.ReportsTo(allowed.parent):
                raise registry.WorkerUnavailable(f"{name}: worker staff owner changed")
            thread_id = row.current_session
            identity = f"{reference}:{source}:{destination}"
            after = dict(before)
            registry.clear_allocation(after)
            for key in ("launcher_pid", "settled_launcher_pid", "turn_id", "context_tokens", "adopted_to"):
                _ = after.pop(key, None)
            after.update(status="paused", adopted_from=str(source), adoption_id=identity,
                         unit_ledger=str(store.path))
            codex_mesh.advance_worker_roster_revision(after)
            with worker_lifecycle.locked_state(target_path) as handle:
                roster = handle.read_object()
                if name in roster:
                    raise registry.WorkerUnavailable(f"{name}: destination changed")
                roster[name] = after
                session_staff.publish_worker_roster(handle, roster)
            with worker_lifecycle.locked_state(source / "mesh_roster.json") as handle:
                roster = handle.read_object()
                before.update(status="adopted", adopted_to=str(destination), adoption_id=identity)
                codex_mesh.advance_worker_roster_revision(before)
                roster[name] = before
                session_staff.publish_worker_roster(handle, roster)
            move = (source / f"{name}.account-move.json")
            if move.exists():
                write_text_atomic((destination / f"{name}.account-move.json"), move.read_text())
            role = codex_mesh.task_role(thread_id, codex_mesh.task_assignments())
            task = role.role if isinstance(role, codex_mesh.AssignedWorkerRole) else "impl"
            if worker is None:
                worker = registry.UnitWorker(name, task, "codex", text_field(before.get("model")),
                    text_field(before.get("effort")), object_fields(before.get("access_policy")),
                    registry.CodexThread(thread_id, session_staff.worker_home(before, membership)),
                    registry.HeldBy(str(source)),
                    [registry.WorkerEvent("opened", "source run", str(source), time.time(), {"thread_id": thread_id})],
                    staff_id=reference)
                ledger.workers[name] = worker
            worker.events.append(registry.WorkerEvent("opened", "adopted", str(destination), time.time(),
                                                      {"thread_id": thread_id}))
            worker.ownership = registry.HeldBy(str(destination))
    return name

