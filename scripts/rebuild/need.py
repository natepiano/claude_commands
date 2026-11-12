"""Record, list, and clear needed system rebuilds."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast

Machine = Literal["natedev", "mac"]


class NeedRecord(TypedDict):
    """The JSON fields of an open rebuild need."""

    id: str
    machine: Machine
    nixos_commit: str
    reason: str
    concerns: str
    requested_by: str
    created_at: str


class CompletedNeedRecord(NeedRecord):
    """A rebuild need covered by one logged switch."""

    cleared_by: str


@dataclass(frozen=True)
class MachineNeeds:
    """The newest open need and all open reasons for one machine."""

    newest: NeedRecord
    reasons: tuple[str, ...]

    def record(self) -> NeedRecord:
        """Return the newest fields with the joined open reasons."""
        return {**self.newest, "reason": " ".join(self.reasons)}


def needs_folder() -> Path:
    """Return the needs folder selected by REBUILD_NEEDS_DIR."""
    return Path(os.environ.get("REBUILD_NEEDS_DIR", "~/.local/state/rebuild/needs")).expanduser()


def _open_needs() -> list[NeedRecord]:
    records: list[NeedRecord] = []
    for path in sorted(needs_folder().glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        records.append(cast(NeedRecord, json.loads(text)))
    return records


@contextmanager
def _need_lock(folder: Path) -> Generator[None]:
    """Hold the folder lock while an add or clear changes a record."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_record(path: Path, record: NeedRecord, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(json.dumps(record, indent=2) + "\n")
            stream.flush()
            if create_only:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            else:
                _ = temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def add_need(machine: Machine, commit: str, reason: str, concerns: str,
             requested_by: str = "") -> None:
    """Create one need unless the same machine and commit already have a record."""
    identifier = f"{machine}-{commit}"
    folder = needs_folder()
    filename = f"{identifier}.json"
    with _need_lock(folder):
        if (folder / filename).exists() or (folder / "done" / filename).exists():
            return
        record: NeedRecord = {
            "id": identifier,
            "machine": machine,
            "nixos_commit": commit,
            "reason": reason,
            "concerns": concerns,
            "requested_by": requested_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_record(folder / filename, record, create_only=True)


def read_needs() -> list[MachineNeeds]:
    """Read the needs folder and return one summary per machine without subprocesses."""
    groups: dict[Machine, list[NeedRecord]] = {}
    for record in _open_needs():
        groups.setdefault(record["machine"], []).append(record)
    summaries: list[MachineNeeds] = []
    for machine in sorted(groups):
        records = sorted(groups[machine], key=lambda record: datetime.fromisoformat(record["created_at"]))
        summaries.append(MachineNeeds(records[-1], tuple(record["reason"] for record in records)))
    return summaries


def clear_needs(machine: Machine, generations: Path, nixos_repo: Path) -> None:
    """Move needs covered by logged switches to the done folder."""
    pending = [record for record in _open_needs() if record["machine"] == machine]
    unknown: set[str] = set()
    for line in generations.read_text(encoding="utf-8").splitlines():
        _time, action, _generation, commit, _store = line.split()
        if action != "switch":
            continue
        commit = commit.removesuffix("-dirty")
        for record in pending[:]:
            result = subprocess.run(
                ["git", "-C", str(nixos_repo), "merge-base", "--is-ancestor", record["nixos_commit"], commit],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                completed: CompletedNeedRecord = {**record, "cleared_by": line}
                source = needs_folder() / f"{record['id']}.json"
                done = needs_folder() / "done"
                with _need_lock(needs_folder()):
                    done.mkdir(exist_ok=True)
                    _write_record(source, completed)
                    _ = source.replace(done / source.name)
                pending.remove(record)
            elif result.returncode != 1:
                for candidate, label in (
                    (commit, f"logged commit {commit}"),
                    (record["nixos_commit"], f"need commit {record['nixos_commit']} (need {record['id']})"),
                ):
                    known = subprocess.run(
                        ["git", "-C", str(nixos_repo), "cat-file", "-e", f"{candidate}^{{commit}}"],
                        capture_output=True, text=True, check=False,
                    )
                    if known.returncode != 0 and candidate not in unknown:
                        print(f"anomaly: unknown {label}")
                        unknown.add(candidate)


class Arguments(argparse.Namespace):
    """The arguments selected by the command parser."""

    command: str = ""
    machine: Machine = "natedev"
    commit: str = ""
    reason: str = ""
    concerns: str = ""
    requested_by: str = ""
    json: bool = False
    generations: Path = Path()
    nixos_repo: Path = Path()


def main(argv: list[str]) -> int:
    """Run one command for the rebuild needs folder."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    _ = add.add_argument("--machine", choices=("natedev", "mac"), required=True)
    _ = add.add_argument("--commit", required=True)
    _ = add.add_argument("--reason", required=True)
    _ = add.add_argument("--concerns", required=True)
    _ = add.add_argument("--requested-by", default="")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    clear = commands.add_parser("clear")
    _ = clear.add_argument("--machine", choices=("natedev", "mac"), required=True)
    _ = clear.add_argument("--generations", type=Path, required=True)
    _ = clear.add_argument("--nixos-repo", type=Path, required=True)
    args = parser.parse_args(argv, namespace=Arguments())
    if args.command == "add":
        add_need(args.machine, args.commit, args.reason, args.concerns, args.requested_by)
    elif args.command == "clear":
        clear_needs(args.machine, args.generations, args.nixos_repo)
    else:
        records = [summary.record() for summary in read_needs()]
        if args.json:
            print(json.dumps(records))
        else:
            for record in records:
                print(f"{record['machine']} {record['nixos_commit']} {record['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
