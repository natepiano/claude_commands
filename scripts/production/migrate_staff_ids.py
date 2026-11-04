#!/usr/bin/env python3
"""One-time native identity migration; references finish before the atomic staff write."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import redirect_stderr
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import atomic_file, unit_runs
from scripts.production import staff, staff_sources, unit_config, showrunners, rename_state, unit_move
from scripts.shutdown import pane_record, remote
from scripts.message import sessions


@dataclass(frozen=True)
class ParkedPublication:
    identity: staff.StaffId
    configuration: unit_config.UnitConfig


@dataclass(frozen=True)
class MigrationPlan:
    identities: dict[staff.StaffId, staff.StaffId]
    additions: tuple[staff.StaffRow, ...]
    missing: tuple[str, ...]
    snapshot: staff.StaffSnapshot = field(default_factory=lambda: staff.StaffSnapshot(()))
    parked: tuple[ParkedPublication, ...] = ()


def historical_workers(record: staff.StaffSnapshot, evidence: dict[staff.StaffId, set[str]],
                       missing: list[str]) -> None:
    """A completed roster proves identity only within its recorded director's ownership."""
    claims: dict[str, set[staff.StaffId]] = {}
    for row in record.rows:
        if row.kind in ("unit", "showrunner"):
            for native in evidence[row.id]:
                claims.setdefault(native, set()).add(row.id)
    owners = {native: next(iter(rows)) for native, rows in claims.items() if len(rows) == 1}
    candidates: dict[staff.StaffId, dict[str, list[str]]] = {}
    parent_entries: dict[staff.StaffId, list[str]] = {}
    for root in unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT")):
        for path in sorted(root.glob("*/mesh_roster.json")):
            try:
                parent = owners.get(unit_runs.director(path.parent))
                if parent is None:
                    continue
                value = cast(object, json.loads(path.read_text(encoding="utf-8")))
                if not isinstance(value, dict):
                    raise ValueError("roster is not an object")
                entries = cast(dict[str, object], value)
                parent_entries.setdefault(parent, []).extend(
                    f"{name}={fields.get('thread_id')} in {path}" for name, entry in entries.items()
                    if isinstance(entry, dict) and (fields := cast(dict[str, object], entry)).get("thread_id"))
                for row in record.find(kind="worker", parent=parent):
                    # A current backend observation takes precedence over older runs.
                    if evidence[row.id]:
                        continue
                    entry = entries.get(row.name)
                    if not isinstance(entry, dict):
                        continue
                    thread = cast(dict[str, object], entry).get("thread_id")
                    if not isinstance(thread, str) or not thread:
                        missing.append(f"{row.id} {row.name}: {path}: missing recorded thread id")
                        continue
                    native = staff.native_id(thread)
                    candidates.setdefault(row.id, {}).setdefault(native, []).append(str(path))
            except (OSError, UnicodeError, ValueError, staff.StaffRefused) as error:
                missing.append(f"{path}: historical worker evidence unavailable: {error}")
    for identity, threads in candidates.items():
        evidence[identity].update(threads)
        if len(threads) > 1:
            detail = "; ".join(f"{native} in {', '.join(paths)}" for native, paths in sorted(threads.items()))
            missing.append(f"{identity}: conflicting parent-owned roster candidates: {detail}")
    for row in record.find(kind="worker"):
        if not evidence[row.id] and row.parent is not None and row.parent in parent_entries:
            missing.append(f"{row.id} {row.name}: no exact worker name in parent-owned rosters; candidates: "
                           + "; ".join(parent_entries[row.parent]))


def worktree_transcript(row: staff.StaffRow, evidence: set[str], missing: list[str]) -> None:
    """One transcript in the exact retained worktree supplies otherwise missing history."""
    if not isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
        return
    if row.worktree.machine != remote.local_machine():
        return
    if not staff_sources.source_allowed(row.worktree.path):
        missing.append(f"{row.id} {row.name}: scratch transcript source refused: {row.worktree.path}")
        return
    folder = unit_move.project_folder(str(row.worktree.path))
    try:
        paths = {path.resolve() for directory in unit_move.config_dirs()
                 for path in (directory / "projects" / folder).glob("*.jsonl")}
        if len(paths) != 1:
            if paths and not evidence:
                missing.append(f"{row.id} {row.name}: multiple worktree transcripts: {', '.join(map(str, sorted(paths)))}")
            return
        path = next(iter(paths))
        native = staff.native_id(path.stem)
        observed: set[str] = set()
        with path.open("rb") as lines:
            for line in lines:
                # A live writer may still be completing its last JSON object.
                if not line.endswith(b"\n"):
                    break
                try:
                    value = cast(object, json.loads(line.decode("utf-8")))
                except (UnicodeError, json.JSONDecodeError):
                    # Damaged history does not invalidate intact identity records.
                    continue
                if isinstance(value, dict):
                    fields = cast(dict[str, object], value)
                    session = fields.get("sessionId")
                    if isinstance(session, str):
                        observed.add(session)
        if observed != {native}:
            raise ValueError(f"{path}: transcript session ids disagree with its filename")
        evidence.add(native)
    except (OSError, UnicodeError, ValueError, staff.StaffRefused) as error:
        missing.append(f"{row.id} {row.name}: worktree transcript unavailable: {error}")


def live_launch_sessions(record: staff.StaffSnapshot, launches: Sequence[pane_record.PaneLaunch]) -> set[str]:
    """Confirm a launch through its exact registry process and start time."""
    confirmed: set[str] = set()
    for launch in launches:
        pid, started = launch.get("pid"), launch.get("proc_start")
        if launch.get("ended") or not isinstance(pid, int) or not started:
            continue
        try:
            native = staff.runtime_id(pid)
        except staff.StaffRefused:
            continue
        if not native or sessions.process_start(pid) != started:
            continue
        reference = launch["session_id"]
        if reference == native or record.by_member_reference(reference) is not None:
            confirmed.add(native)
    return confirmed


def plan(record: staff.StaffSnapshot) -> MigrationPlan:
    """Prove identities from recorded owners; missing history stays unproved."""
    missing: list[str] = []
    sources, source_problems = staff_sources.migration_sources(record)
    missing.extend(source_problems)
    live_sessions = live_launch_sessions(record, sources.launches)
    evidence: dict[staff.StaffId, set[str]] = {row.id: set() for row in record.rows}
    peer_identities, peer_problems = staff_sources.migration_peer_identities(record)
    missing.extend(peer_problems)
    for identity, candidates in peer_identities.items():
        evidence[identity].update(candidates)
    for launch in sources.launches:
        old = launch["environment"].get("STAFF_ID", "")
        if old and staff.StaffId(old) in evidence:
            evidence[staff.StaffId(old)].add(launch["session_id"])
        native = staff.StaffId(launch["session_id"])
        if native in evidence:
            evidence[native].add(native)
    try:
        workers = staff_sources.observe_workers(record, sources,
                                               unavailable=staff_sources.UnavailableWorkerPolicy.LIST)
    except (OSError, ValueError, staff.StaffUnreadable) as error:
        missing.append(f"worker runtime evidence unavailable: {error}")
    else:
        # Migration needs historical identity, not unrelated dead workers' admission facts.
        missing.extend(line for line in workers.diagnostics
                       if "missing or invalid recorded worker role" not in line
                       or any(f" worker {row.name}:" in line for row in record.find(kind="worker")))
        for identity, backend in workers.identities.items():
            if identity in evidence:
                evidence[identity].update(backend.values())
        for observed in workers.workers:
            if isinstance(observed.identity, staff_sources.BackendUnavailable):
                continue
            matches = [row for row in record.find(kind="worker", parent=observed.owner.match.id)
                       if row.name == observed.name]
            if len(matches) == 1:
                evidence[matches[0].id].update(observed.identity.values())
            elif matches:
                missing.append(f"{observed.name}: no unique worker row under {observed.owner.match.id}")
    parked: dict[staff.StaffId, unit_config.UnitConfig] = {}
    for row in record.rows:
        try:
            identity = staff.native_id(row.id)
            evidence[row.id].add(identity)
        except staff.StaffRefused:
            pass
        if row.kind == "unit":
            saved = unit_config.read_parked(row.id, cleanup=False)
            if isinstance(saved, unit_config.UnitConfig):
                parked[row.id] = saved
                if isinstance(saved.retired, unit_config.RetiredSession):
                    evidence[row.id].add(saved.retired.session_id)
            elif unit_config.parked_path(row.id).exists():
                missing.append(f"{row.id} {row.name}: unreadable parked configuration: {saved.reason}")
            # Folder history cannot replace an identity proved by its live process.
            if not evidence[row.id].intersection(live_sessions):
                worktree_transcript(row, evidence[row.id], missing)
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            # A timer binds this stored doc, not another discovered copy.
            for source in sources.productions.values():
                if source.timer.doc.resolve() == row.doc.path.resolve():
                    target = source.timer.target.removeprefix("session:")
                    if target:
                        evidence[row.id].add(target)
    historical_workers(record, evidence, missing)
    # A recorded handoff is conversation evidence, never a new member identity.
    for row in record.rows:
        if row.current_session != row.id and not row.id.startswith("s"):
            evidence[row.id] = {row.id}
    identities: dict[staff.StaffId, staff.StaffId] = {}
    destinations: dict[staff.StaffId, staff.StaffId] = {}
    for row in record.rows:
        try:
            candidates = {staff.native_id(value) for value in evidence[row.id]}
            if len(candidates) != 1:
                raise staff.StaffRefused("missing native session evidence" if not candidates
                                        else "conflicting native session evidence: " + ", ".join(sorted(candidates)))
            native = next(iter(candidates))
            other = destinations.get(native)
            if other is not None and other != row.id:
                raise staff.StaffRefused(f"native destination also claimed by {other}")
            destinations[native] = row.id
            identities[row.id] = native
        except (ValueError, staff.StaffRefused) as error:
            if not any(line.startswith(f"{row.id} ") or line.startswith(f"{row.id}:") for line in missing):
                missing.append(f"{row.id} {row.name}: {error}")
    # Do not publish either side of a disputed identity.
    for identity, destination in list(identities.items()):
        if sum(destination in values for values in evidence.values()) > 1:
            _ = identities.pop(identity)
            missing.append(f"{identity}: conflicting native destination {destination}")
    # A proved child cannot finish before its owner's native identity is proved.
    while True:
        unowned = [row for row in record.rows if row.id in identities
                   and row.parent is not None and row.parent not in identities]
        if not unowned:
            break
        for row in unowned:
            del identities[row.id]
            missing.append(f"{row.id} {row.name}: missing proved native parent {row.parent}")
    publications: list[ParkedPublication] = [ParkedPublication(identities.get(identity, identity), config)
                                           for identity, config in parked.items() if identity in identities]
    # Optional historical sources can be unreadable while independent runtime
    # evidence proves a row. Conflicting identities have already been withheld
    # above; report each proved row only as proved.
    unresolved = [line for line in missing
                  if staff.StaffId(line.split(maxsplit=1)[0].removesuffix(":")) not in identities]
    return MigrationPlan(identities, (), tuple(dict.fromkeys(unresolved)), record, tuple(publications))


def _replace_references(value: object, identities: dict[staff.StaffId, staff.StaffId]) -> object:
    if isinstance(value, str):
        return identities.get(staff.StaffId(value), value)
    if isinstance(value, list):
        return [_replace_references(item, identities) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {key: _replace_references(item, identities) for key, item in cast(dict[str, object], value).items()}
    return value


def _reference_files(migration: MigrationPlan) -> set[Path]:
    """Enumerate the existing owner stores, without adding a second store registry."""
    paths: set[Path] = set()
    for directory in showrunners.SHOWRUNNER_STATE_DIR.iterdir() if showrunners.SHOWRUNNER_STATE_DIR.exists() else ():
        if directory.is_dir():
            paths.update(directory.glob("*.json"))
    for row in migration.snapshot.rows:
        if (isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.machine == remote.local_machine()
                and staff_sources.source_allowed(row.doc.path)):
            result = subprocess.run(["git", "-C", str(row.doc.path.parent), "rev-parse", "--absolute-git-dir"],
                                    capture_output=True, text=True, check=False)
            if result.returncode == 0:
                git = Path(result.stdout.strip())
                for directory in (git / "promotions", git / "add-unit-openings"):
                    paths.update(directory.glob("*.json"))
    return paths


def publish_references(migration: MigrationPlan) -> None:
    """Each destination is equal or absent; interruption leaves its own replay proof."""
    for old, native in migration.identities.items():
        source, target = unit_config.parked_path(old), unit_config.parked_path(native)
        if old == native or not source.exists():
            continue
        config = unit_config.read_parked(old, cleanup=False)
        if not isinstance(config, unit_config.UnitConfig):
            raise staff.StaffRefused(f"{source}: {config.reason}")
        if target.exists() and unit_config.read_parked(native, cleanup=False) != config:
            raise staff.StaffRefused(f"{target}: unequal migration destination")
        _ = unit_config.write_parked(native, config)
        # Keep the old source until staff is durable, so a stopped migration can prove it again.
    for item in migration.parked:
        target = unit_config.parked_path(item.identity)
        if target.exists() and unit_config.read_parked(item.identity, cleanup=False) != item.configuration:
            raise staff.StaffRefused(f"{target}: unequal parked destination")
        _ = unit_config.write_parked(item.identity, item.configuration)
    for path in sorted(_reference_files(migration)):
        value = rename_state.read_json(path)
        changed = _replace_references(value, migration.identities)
        if value != changed:
            rename_state.write_json(path, changed)
    if not any(old != native for old, native in migration.identities.items()):
        return
    # Retired refs live on origin; local mirrors are supporting copies.
    repositories = {row.doc.path.parent for row in migration.snapshot.rows
        if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
        and row.doc.machine == remote.local_machine() and staff_sources.source_allowed(row.doc.path)}
    for repository in sorted(repositories):
        command = ["git", "-C", str(repository)]
        configured = subprocess.run([*command, "remote"], capture_output=True, text=True, check=True)
        tips: dict[str, str] = {}
        if "origin" in configured.stdout.splitlines():
            remote_refs = subprocess.run([*command, "ls-remote", "--exit-code", "origin", "refs/retired/*"],
                capture_output=True, text=True)
            if remote_refs.returncode not in (0, 2):
                raise staff.StaffRefused(f"{repository}: origin retired refs unavailable: {remote_refs.stderr.strip()}")
            tips = {fields[1]: fields[0] for line in remote_refs.stdout.splitlines()
                    if len(fields := line.split()) == 2}
        for old, native in migration.identities.items():
            if old == native:
                continue
            source, target = ("refs/retired/" + value for value in (old, native))
            if source in tips:
                tip = tips[source]
                if target in tips and tips[target] != tip:
                    raise staff.StaffRefused(f"origin {target}: unequal retired ref destination")
                if target not in tips:
                    _ = subprocess.run([*command, "fetch", "--no-tags", "origin", source],
                        capture_output=True, text=True, check=True)
                    _ = subprocess.run([*command, "push", f"--force-with-lease={target}:",
                        "origin", f"{tip}:{target}"], capture_output=True, text=True, check=True)
                    verified = subprocess.run([*command, "ls-remote", "--exit-code", "origin", target],
                        capture_output=True, text=True, check=True)
                    if verified.stdout.split() != [tip, target]:
                        raise staff.StaffRefused(f"origin {target}: retired ref publication unproved")
                    tips[target] = tip
            local = subprocess.run([*command, "rev-parse", "--verify", source], capture_output=True, text=True)
            if local.returncode:
                continue
            tip = local.stdout.strip()
            destination = subprocess.run([*command, "rev-parse", "--verify", target], capture_output=True, text=True)
            if destination.returncode == 0 and destination.stdout.strip() != tip:
                raise staff.StaffRefused(f"{target}: unequal retired ref destination")
            if destination.returncode:
                _ = subprocess.run([*command, "update-ref", target, tip, ""], capture_output=True, text=True, check=True)


def migrate(*, dry_run: bool = False) -> int:
    """Preview is read only; real replay takes the operation lock and a byte backup."""
    def run() -> int:
        record = staff.snapshot()
        migration = plan(record)
        changed = {old: native for old, native in migration.identities.items() if old != native}
        for old, native in sorted(changed.items()):
            print(f"proved {old} -> {native}")
        for row in migration.additions:
            print(f"{'would add' if dry_run else 'add'} retired {row.id} {row.name} parent {row.parent}")
        for line in migration.missing:
            print("missing " + line.replace("\n", " "))
        if not dry_run:
            if not staff.is_owner():
                raise staff.StaffRefused("run the migration on the staff owner's machine")
            source = staff._file()  # pyright: ignore[reportPrivateUsage]
            backup = source.with_name(source.name + ".before-native-ids")
            if not backup.exists():
                atomic_file.write_bytes_atomic(backup, source.read_bytes())
            # A leftover backup from an earlier interrupted implementation is not proof.
            saved = cast(object, json.loads(backup.read_bytes()))
            if not isinstance(saved, dict):
                raise staff.StaffRefused(f"{backup}: invalid migration backup")
            fields = cast(dict[str, object], saved)
            rows = fields.get("rows")
            if fields.get("schema") not in (1, 2) or not isinstance(rows, list):
                raise staff.StaffRefused(f"{backup}: invalid migration backup")
            for value in cast(list[object], rows):
                _ = staff.StaffRow.from_json(value)
            publish_references(migration)
            with redirect_stderr(io.StringIO()):
                _ = staff.publish_native_ids(record, changed, migration.additions)
        if not changed and not migration.additions:
            print("unchanged")
        if dry_run or changed or migration.additions or migration.missing:
            print(f"proved {len(migration.identities)}; retired {len(migration.additions)}; missing {len(migration.missing)}")
        return 1 if migration.missing else 0
    if dry_run:
        return run()
    with staff.operation():
        return run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return migrate(dry_run=cast(bool, args.dry_run))
    except (OSError, ValueError, staff.StaffRefused, rename_state.RenameRefused, subprocess.SubprocessError) as error:
        print(f"staff migration: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
