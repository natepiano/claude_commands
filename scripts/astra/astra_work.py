#!/usr/bin/env python3
"""Save Astra assignments with explicit run and phase identity."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import durable_state

AstraRole = Literal["astra-designer", "astra-reviewer"]


@dataclass(frozen=True)
class PhaseWork:
    run_directory: str
    run_id: str
    phase_id: str
    phase_instance_id: str
    history_file: str
    kind: Literal["phase"] = field(default="phase", init=False)


@dataclass(frozen=True)
class StandaloneWork:
    kind: Literal["standalone"] = field(default="standalone", init=False)


@dataclass(frozen=True)
class AstraWork:
    role: AstraRole
    member: str
    document: str
    assigned_at_utc: str
    reply_path: str
    work: PhaseWork | StandaloneWork
    schema: Literal[1] = field(default=1, init=False)


@dataclass(frozen=True)
class HeaderMissing:
    path: str


@dataclass(frozen=True)
class HeaderInvalid:
    path: str
    reason: str


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)


def _text(value: dict[str, object], key: str) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"missing or invalid {key}")
    return text


def phase_work(run_directory: str | Path, phase_id: str) -> PhaseWork:
    directory = Path(run_directory).expanduser().resolve()
    try:
        state = _object(cast(object, json.loads(
            (directory / "progress_history_state.json").read_text(encoding="utf-8"))))
        phases: list[dict[str, object]] = []
        parked = state.get("open_phases", [])
        if not isinstance(parked, list):
            raise ValueError("invalid open_phases")
        phases.extend(_object(item) for item in cast(list[object], parked))
        current = state.get("phase")
        if isinstance(current, dict):
            phase = cast(dict[str, object], current)
            if phase.get("status") == "active":
                phases.append(phase)
        matches = [phase for phase in phases if phase.get("id") == phase_id]
        if not phase_id or len(matches) != 1:
            raise ValueError(f"phase {phase_id} does not resolve to one open phase")
        return PhaseWork(str(directory), _text(state, "run_id"), phase_id,
                         _text(matches[0], "instance_id"), _text(state, "history_file"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"cannot resolve phase {phase_id} in {directory}: {error}") from error


def header_line(work: AstraWork) -> str:
    return "<!-- astra_work " + json.dumps(asdict(work), ensure_ascii=False, separators=(",", ":")) + " -->"


def read_header(path: str | Path) -> AstraWork | HeaderMissing | HeaderInvalid:
    source = Path(path)
    try:
        with source.open(encoding="utf-8") as stream:
            first = stream.readline().rstrip("\r\n")
        prefix = "<!-- astra_work "
        if not first.startswith(prefix):
            return HeaderMissing(str(source))
        if not first.endswith(" -->"):
            raise ValueError("header has no closing marker")
        record = _object(cast(object, json.loads(first[len(prefix):-4])))
        if set(record) != {"schema", "role", "member", "document", "assigned_at_utc", "reply_path", "work"}:
            raise ValueError("header has missing or extra fields")
        if type(record["schema"]) is not int or record["schema"] != 1:
            raise ValueError("unknown header schema")
        role = record["role"]
        if role not in ("astra-designer", "astra-reviewer"):
            raise ValueError("unknown Astra role")
        assignment = _object(record["work"])
        kind = assignment.get("kind")
        if kind == "standalone" and set(assignment) == {"kind"}:
            work = StandaloneWork()
        elif kind == "phase" and set(assignment) == {
            "kind", "run_directory", "run_id", "phase_id", "phase_instance_id", "history_file",
        }:
            work = PhaseWork(*(_text(assignment, key) for key in (
                "run_directory", "run_id", "phase_id", "phase_instance_id", "history_file")))
        else:
            raise ValueError("invalid work context")
        stamp = _text(record, "assigned_at_utc")
        assigned = datetime.fromisoformat(stamp)
        if assigned.utcoffset() != timezone.utc.utcoffset(assigned):
            raise ValueError("assigned_at_utc must be UTC")
        return AstraWork(role, _text(record, "member"), _text(record, "document"), stamp,
                         _text(record, "reply_path"), work)
    except FileNotFoundError:
        return HeaderMissing(str(source))
    except (OSError, UnicodeError, ValueError) as error:
        return HeaderInvalid(str(source), str(error))


@dataclass
class BriefArguments(argparse.Namespace):
    command: str = ""
    run_directory: str = ""
    phase: str = ""
    member: str = ""
    document: str = ""
    round: int = 0
    text_file: str = ""


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    brief = commands.add_parser("brief")
    for name in ("run-directory", "phase", "member", "document", "text-file"):
        _ = brief.add_argument(f"--{name}", required=True)
    _ = brief.add_argument("--round", type=int, required=True)
    args = parser.parse_args(arguments, namespace=BriefArguments())
    try:
        if args.round < 1 or args.phase in (".", "..") or "/" in args.phase or "\\" in args.phase:
            raise ValueError("brief needs a positive round and a phase file name")
        work = phase_work(args.run_directory, args.phase)
        text = Path(args.text_file).expanduser().read_text(encoding="utf-8")
        folder = Path(work.run_directory) / "design" / f"phase{args.phase}"
        destination = folder / f"round{args.round}_brief.md"
        reply = folder / f"round{args.round}_reply.md"
        if reply.exists():
            raise ValueError(f"reply already exists: {reply}")
        assignment = AstraWork("astra-designer", args.member, str(Path(args.document).expanduser().resolve()),
                               datetime.now(timezone.utc).isoformat(), str(reply), work)
        folder.mkdir(parents=True, exist_ok=True)
        with durable_state.file_lock(destination.with_name(destination.name + ".lock")):
            if destination.exists():
                saved = read_header(destination)
                if (not isinstance(saved, AstraWork) or saved.role != assignment.role
                        or saved.member != assignment.member or saved.document != assignment.document
                        or saved.reply_path != assignment.reply_path or saved.work != assignment.work
                        or destination.read_text(encoding="utf-8").split("\n", 1)[-1] != text):
                    raise ValueError(f"brief already exists with different or incomplete content: {destination}")
            else:
                durable_state.atomic_write_text(destination, header_line(assignment) + "\n" + text)
        print(destination)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"astra_work: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
