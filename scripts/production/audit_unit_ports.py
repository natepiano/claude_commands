#!/usr/bin/env python3
"""Compare each unit's configuration port with its bound production document."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import staff, unit_config, production_units  # noqa: E402
from scripts.shutdown import remote  # noqa: E402


from scripts.production.production_units import DocumentSource as SourceLocation  # noqa: E402


@dataclass(frozen=True)
class Unavailable:
    reason: str


@dataclass(frozen=True)
class DocumentPortMissing:
    reason: str


@dataclass(frozen=True)
class PortAudit:
    row: staff.StaffRow
    config_source: SourceLocation | Unavailable
    config_port: unit_config.AppPort | unit_config.NoAppPort | unit_config.ConfigMissing | Unavailable
    document_source: SourceLocation | Unavailable
    document_cell: str
    document_port: unit_config.AppPort | unit_config.NoAppPort | DocumentPortMissing | Unavailable
    gap: str
    note: str


@dataclass(frozen=True)
class FileEvidence:
    source: SourceLocation
    text: str


@dataclass(frozen=True)
class FileMissing:
    source: SourceLocation


def _remote_python(machine: str, script: str, *arguments: str) -> str:
    command = ["env", "PYTHONDONTWRITEBYTECODE=1", 'CLAUDE_PYTHON="${CLAUDE_PYTHON:-python3}"',
               '"$HOME/.claude/scripts/lib/py"', "-c", shlex.quote(script),
               *(shlex.quote(argument) for argument in arguments)]
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", machine, " ".join(command)],
        capture_output=True, text=True, check=False,
        timeout=remote.STANDARD_TIME_LIMIT["seconds"],
    )
    if result.returncode:
        raise OSError(result.stderr.strip() or f"{machine}: remote read failed")
    return result.stdout


def read_snapshot() -> staff.StaffSnapshot:
    """Read the owner's stored staff page through its existing file reader."""
    if staff.is_owner():
        return staff.local_snapshot()
    script = (
        "import json, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path.home() / '.claude'))\n"
        "from scripts.production import staff\n"
        "print(json.dumps([row.to_json() for row in staff.local_snapshot().rows]))\n"
    )
    try:
        value = cast(object, json.loads(_remote_python(staff.OWNER_MACHINE, script)))
        if not isinstance(value, list):
            raise ValueError("remote staff response is not a row list")
        return staff.StaffSnapshot(tuple(staff.StaffRow.from_json(row) for row in cast(list[object], value)))
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as error:
        raise staff.StaffUnreadable(f"{staff.OWNER_MACHINE}: {error}") from error


def read_remote(source: SourceLocation) -> FileEvidence | FileMissing | Unavailable:
    """Read one recorded path without executing a staff command or changing remote files."""
    return _remote_file(source.machine, (
        "import json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1]).expanduser()\n"
    ), str(source.path))


def read_remote_parked(machine: str, unit: staff.StaffId) -> FileEvidence | FileMissing | Unavailable:
    """Resolve the retained path through the configuration owner on the recorded machine."""
    return _remote_file(machine, (
        "import json, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path.home() / '.claude'))\n"
        "from scripts.production import unit_config\n"
        "from scripts.production.staff import StaffId\n"
        "path = unit_config.parked_path(StaffId(sys.argv[1]))\n"
    ), unit)


def _remote_file(machine: str, resolve_path: str, argument: str) -> FileEvidence | FileMissing | Unavailable:
    script = resolve_path + (
        "try:\n"
        "    result = {'path': str(path), 'text': path.read_text(encoding='utf-8')}\n"
        "except FileNotFoundError:\n"
        "    result = {'path': str(path), 'missing': True}\n"
        "except (OSError, UnicodeError) as error:\n"
        "    result = {'error': str(error)}\n"
        "print(json.dumps(result))\n"
    )
    try:
        value = cast(object, json.loads(_remote_python(machine, script, argument)))
        if not isinstance(value, dict):
            raise ValueError("remote file response is not an object")
        fields = cast(dict[str, object], value)
        if isinstance(fields.get("error"), str):
            return Unavailable(f"{machine}:{argument}: {fields['error']}")
        path = fields.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("remote file response lacks an absolute source path")
        location = SourceLocation(machine, Path(path))
        if fields.get("missing") is True:
            return FileMissing(location)
        text = fields.get("text")
        if not isinstance(text, str):
            raise ValueError("remote file response lacks file text")
        return FileEvidence(location, text)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as error:
        return Unavailable(f"{machine}:{argument}: {error}")


def _file(source: SourceLocation) -> FileEvidence | FileMissing | Unavailable:
    if source.machine != remote.local_machine():
        return read_remote(source)
    try:
        return FileEvidence(source, source.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return FileMissing(source)
    except (OSError, UnicodeError) as error:
        return Unavailable(f"{source.machine}:{source.path}: {error}")


def _configuration(row: staff.StaffRow) -> tuple[
    SourceLocation | Unavailable,
    unit_config.AppPort | unit_config.NoAppPort | unit_config.ConfigMissing | Unavailable,
]:
    worktree = row.worktree
    if isinstance(worktree, staff.WorktreeUnfilled):
        problem = Unavailable("staff worktree is unfilled")
        return problem, problem
    parked = isinstance(worktree, staff.RemovedWorktree)
    local = worktree.machine == remote.local_machine()
    source: SourceLocation | Unavailable = Unavailable("remote parked path is unavailable")
    if local or not parked:
        path = unit_config.parked_path(row.id) if parked else worktree.path / ".unit.toml"
        source = SourceLocation(worktree.machine, path)
    try:
        if local:
            config = (unit_config.read_parked(row.id, cleanup=False) if parked
                      else unit_config.read(worktree.path, cleanup=False))
        else:
            if parked:
                evidence = read_remote_parked(worktree.machine, row.id)
            else:
                assert isinstance(source, SourceLocation)
                evidence = read_remote(source)
            if isinstance(evidence, Unavailable):
                return source, evidence
            source = evidence.source
            if isinstance(evidence, FileMissing):
                return source, unit_config.ConfigMissing(f"{source.machine}:{source.path}: configuration is missing")
            config = unit_config.parse(evidence.text)
        return source, config if isinstance(config, unit_config.ConfigMissing) else config.port
    except (OSError, UnicodeError, ValueError) as error:
        return source, Unavailable(str(error))


@dataclass(frozen=True)
class DocumentPortEvidence:
    source: SourceLocation | Unavailable
    cell: str
    port: unit_config.AppPort | unit_config.NoAppPort | DocumentPortMissing | Unavailable
    note: str = ""


def _document_port(source: SourceLocation, cells: dict[str, str], note: str = "") -> DocumentPortEvidence:
    if "Port" not in cells:
        return DocumentPortEvidence(source, "", DocumentPortMissing("Units row has no Port cell"), note)
    cell = cells["Port"]
    try:
        port = unit_config.parse_port_cell(cell)
    except unit_config.ConfigRefused as error:
        return DocumentPortEvidence(source, cell, DocumentPortMissing(str(error)), note)
    return DocumentPortEvidence(source, cell, port, note)


def _document(row: staff.StaffRow, record: staff.StaffSnapshot,
              files: dict[SourceLocation, FileEvidence | FileMissing | Unavailable]) -> DocumentPortEvidence:
    owner = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
    if owner is None or owner.kind != "showrunner" or isinstance(owner.doc, staff.DocUnfilled):
        problem = Unavailable("parent has no bound production document")
        return DocumentPortEvidence(problem, "", problem)
    source = SourceLocation(owner.doc.machine, owner.doc.path)

    def load(location: SourceLocation) -> str | production_units.DocumentUnavailable:
        if location not in files:
            files[location] = _file(location)
        evidence = files[location]
        if isinstance(evidence, FileEvidence):
            return evidence.text
        reason = evidence.reason if isinstance(evidence, Unavailable) else "bound production document is missing"
        return production_units.DocumentUnavailable(reason)

    documents = tuple(SourceLocation(runner.doc.machine, runner.doc.path)
                      for runner in record.find(kind="showrunner") if not isinstance(runner.doc, staff.DocUnfilled))
    try:
        result = production_units.lookup_unit(row.name, source, documents, load)
    except ValueError as error:
        return DocumentPortEvidence(source, row.name, DocumentPortMissing(str(error)))
    if isinstance(result, production_units.UnitRowFound):
        return _document_port(result.source, result.cells, result.note)
    if isinstance(result, production_units.UnitRowMissing):
        return DocumentPortEvidence(source, result.cell, DocumentPortMissing(result.reason))
    if isinstance(result, production_units.UnitSearchIncomplete):
        return DocumentPortEvidence(source, "", Unavailable("incomplete document search: " + result.reason))
    return DocumentPortEvidence(source, "", DocumentPortMissing(result.reason))


def port_text(port: unit_config.AppPort | unit_config.NoAppPort | unit_config.ConfigMissing
              | DocumentPortMissing | Unavailable) -> str:
    if isinstance(port, unit_config.AppPort):
        return f"AppPort({port.number})"
    if isinstance(port, unit_config.NoAppPort):
        return "NoAppPort"
    if isinstance(port, unit_config.ConfigMissing):
        return f"ConfigMissing: {port.reason}"
    if isinstance(port, DocumentPortMissing):
        return f"DocumentPortMissing: {port.reason}"
    return f"unavailable: {port.reason}"


def audit(record: staff.StaffSnapshot) -> tuple[PortAudit, ...]:
    """Collect configuration ports and search the recorded production documents."""
    files: dict[SourceLocation, FileEvidence | FileMissing | Unavailable] = {}
    results: list[PortAudit] = []
    for row in record.find(kind="unit"):
        if isinstance(row.worktree, staff.RemovedWorktree):
            continue
        config_source, config_port = _configuration(row)
        document = _document(row, record, files)
        valid = isinstance(config_port, (unit_config.AppPort, unit_config.NoAppPort))
        gap = "" if valid and config_port == document.port else (
            f"configuration {port_text(config_port)}; document {port_text(document.port)}")
        results.append(PortAudit(row, config_source, config_port, document.source, document.cell,
                                 document.port, gap, document.note))
    return tuple(results)


def _source_text(source: SourceLocation | Unavailable) -> str:
    return f"{source.machine}:{source.path}" if isinstance(source, SourceLocation) else f"unavailable: {source.reason}"


class _Arguments(argparse.Namespace):
    json: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", help="print the evidence as JSON")
    arguments = parser.parse_args(argv, namespace=_Arguments())
    try:
        record = read_snapshot()
        results = audit(record)
    except staff.StaffUnreadable as error:
        print(f"unavailable: {error}", file=sys.stderr)
        return 0
    output = [{"id": result.row.id, "name": result.row.name,
               "config_source": _source_text(result.config_source), "config_port": port_text(result.config_port),
               "document_source": _source_text(result.document_source), "document_cell": result.document_cell,
               "document_port": port_text(result.document_port), "gap": result.gap, "note": result.note} for result in results]
    if arguments.json:
        print(json.dumps(output, indent=2))
    else:
        for evidence, result in zip(results, output):
            print(f"{result['id']} {result['name']}: {result['config_port']} [{result['config_source']}]; "
                  + f"Port={result['document_cell']!r} [{result['document_source']}]")
            if result["note"]:
                print(f"  note: {result['note']}")
            if result["gap"]:
                parent = record.parent_of(evidence.row)
                production = parent.name if parent is not None else "unknown"
                print(f"  gap for {production}: {result['gap']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
