"""Measure requests in named unit-director transcripts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from compare import compare
from turns import Compaction, Dropped, Turn, director_turns, load_roster, read_session, resolve_session_files, session_ids

PDT = ZoneInfo("America/Los_Angeles")
DEFAULT_STATE = Path.home() / ".local/state/model-study"
DEFAULT_ROSTER = Path(__file__).with_name("roster.json")


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = output.name
            _ = output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def json_lines(rows: Sequence[Turn] | Sequence[Compaction]) -> str:
    return "".join(json.dumps(row.to_json(), separators=(",", ":")) + "\n" for row in rows)


def pdt(instant: str | None) -> str | None:
    if instant is None:
        return None
    return datetime.fromisoformat(instant).astimezone(PDT).strftime("%Y-%m-%d %H:%M PDT")


def drop_sum(values: list[Dropped]) -> dict[str, int]:
    return {field: sum(getattr(value, field) for value in values) for field in (
        "before_director", "sidechain", "no_trigger", "negative", "over_limit", "synthetic"
    )}


def extract(roster: Path, projects_dir: Path, registry_dir: Path, state_dir: Path) -> dict[str, object]:
    all_turns: list[Turn] = []
    all_compactions: list[Compaction] = []
    summaries: list[dict[str, object]] = []
    print("name | requests | model / effort | first Sonnet PDT | switched by PDT | difference min | drops")
    for entry in load_roster(roster):
        files = resolve_session_files(entry, projects_dir, registry_dir)
        ids = session_ids(entry, registry_dir)
        found = {path.stem for path in files}
        reads = sorted(
            (read_session(path, entry.name, entry.director_from_pdt) for path in files),
            key=lambda read: read.turns[0].started if read.turns else "\uffff",
        )
        turns = director_turns(reads)
        all_turns.extend(turns)
        all_compactions.extend(compaction for read in reads for compaction in read.compactions)
        models = dict(Counter(turn.model for turn in turns))
        efforts = dict(Counter(turn.effort for turn in turns))
        first_sonnet = min((turn.started for turn in turns if "sonnet" in turn.model.lower()), default=None)
        first_pdt = pdt(first_sonnet)
        switch = entry.switched_by_pdt
        difference: float | None = None
        if switch is not None and first_sonnet is not None:
            switched_at = datetime.fromisoformat(switch).replace(tzinfo=PDT)
            difference = round((datetime.fromisoformat(first_sonnet) - switched_at).total_seconds() / 60, 1)
        dropped = drop_sum([read.dropped for read in reads])
        boundaries = [read.director_from for read in reads if read.director_from is not None]
        summary: dict[str, object] = {
            "name": entry.name,
            "session_ids": ids,
            "missing_ids": [session_id for session_id in ids if session_id not in found],
            "director_from": min(boundaries) if boundaries else None,
            "whole_session": any(read.whole_session for read in reads),
            "requests": len(turns),
            "by_model": models,
            "by_effort": efforts,
            "first_sonnet": first_sonnet,
            "first_sonnet_pdt": first_pdt,
            "dropped": dropped,
        }
        summaries.append(summary)
        breakdown = ", ".join(f"{model}:{count}" for model, count in sorted(models.items()))
        effort_breakdown = ", ".join(f"{effort}:{count}" for effort, count in sorted(efforts.items()))
        drop_text = ", ".join(f"{key}:{value}" for key, value in dropped.items())
        print(f"{entry.name} | {len(turns)} | {breakdown}; {effort_breakdown} | {first_pdt or '-'} | {switch or '-'} | {difference if difference is not None else '-'} | {drop_text}")
    result: dict[str, object] = {
        "extracted": datetime.now(timezone.utc).isoformat(),
        "sessions": summaries,
    }
    atomic_text(state_dir / "turns.jsonl", json_lines(all_turns))
    atomic_text(state_dir / "compactions.jsonl", json_lines(all_compactions))
    atomic_text(state_dir / "extract.json", json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    extract_parser = subcommands.add_parser("extract", help="measure director requests")
    _ = extract_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    _ = extract_parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    _ = extract_parser.add_argument("--registry-dir", type=Path, default=Path.home() / ".claude/sessions")
    _ = extract_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    compare_parser = subcommands.add_parser("compare", help="compare Opus and Sonnet requests")
    _ = compare_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    args = cast(dict[str, object], vars(parser.parse_args()))
    if args.get("command") == "extract":
        roster = args["roster"]
        projects_dir = args["projects_dir"]
        registry_dir = args["registry_dir"]
        state_dir = args["state_dir"]
        assert isinstance(roster, Path)
        assert isinstance(projects_dir, Path)
        assert isinstance(registry_dir, Path)
        assert isinstance(state_dir, Path)
        _ = extract(roster, projects_dir, registry_dir, state_dir)
    elif args.get("command") == "compare":
        state_dir = args["state_dir"]
        assert isinstance(state_dir, Path)
        _ = compare(state_dir)


if __name__ == "__main__":
    main()
