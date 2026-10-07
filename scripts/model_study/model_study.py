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
from typing import TypedDict, cast
from zoneinfo import ZoneInfo

from compare import compare, load_turns, markdown as compare_markdown
from phases import markdown as phases_markdown, phases
import report as study_report
import results as study_results
from turns import Compaction, Dropped, Turn, load_roster, read_session, recompute_switch_turns, session_ids

PDT = ZoneInfo("America/Los_Angeles")
DEFAULT_STATE = Path.home() / ".local/state/model-study"
DEFAULT_ROSTER = Path(__file__).with_name("roster.json")


class SessionState(TypedDict):
    director: str
    requests: int
    dropped: dict[str, int]
    transcript_gone: bool
    director_from: str | None
    whole_session: bool


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


def extract(roster: Path, projects_dir: Path, registry_dir: Path, state_dir: Path, verbose: bool = True, now: datetime | None = None) -> dict[str, object]:
    all_turns: list[Turn] = []
    all_compactions: list[Compaction] = []
    summaries: list[dict[str, object]] = []
    states: dict[str, SessionState] = {}
    old_extract = state_dir / "extract.json"
    previous = cast(dict[str, object], json.loads(old_extract.read_text())) if old_extract.exists() else {}
    old_states = cast(dict[str, SessionState], previous.get("session_states", {}))
    migrating = "session_states" not in previous
    old_summaries = {cast(str, row["name"]): row for row in cast(list[dict[str, object]], previous.get("sessions", []))}
    old_turns = load_turns(state_dir / "turns.jsonl") if (state_dir / "turns.jsonl").exists() else []
    old_turn_counts = Counter((turn.name, turn.session) for turn in old_turns)
    old_compactions: list[Compaction] = []
    old_compaction_path = state_dir / "compactions.jsonl"
    if old_compaction_path.exists():
        for line in old_compaction_path.read_text().splitlines():
            row = cast(dict[str, object], json.loads(line))
            old_compactions.append(Compaction(
                session=cast(str, row["session"]), name=cast(str, row["name"]),
                at=cast(str, row["at"]), trigger=cast(str, row["trigger"]),
                pre_tokens=cast(int, row["pre_tokens"]), post_tokens=cast(int, row["post_tokens"]),
                duration_ms=cast(int | None, row["duration_ms"]), model=cast(str, row["model"]),
            ))
    if verbose:
        print("name | requests | model / effort | first Sonnet PDT | switched by PDT | difference min | drops")
    for entry in load_roster(roster):
        prior_ids = [session_id for session_id, state in old_states.items() if state["director"] == entry.name]
        if migrating:
            prior_summary = old_summaries.get(entry.name, {})
            prior_ids = list(dict.fromkeys([
                *cast(list[str], prior_summary.get("session_ids", [])),
                *(turn.session for turn in old_turns if turn.name == entry.name),
            ]))
        ids = list(dict.fromkeys([*session_ids(entry, registry_dir), *prior_ids]))
        files = {session_id: sorted(projects_dir.glob(f"*/{session_id}.jsonl")) for session_id in ids}
        found = {session_id for session_id, paths in files.items() if paths}
        reads = {session_id: read_session(paths[0], entry.name, entry.director_from_pdt)
                 for session_id, paths in files.items() if paths}
        carried = set(ids) - found
        if migrating:
            prior_summary = old_summaries.get(entry.name, {})
            old_drops = cast(dict[str, int], prior_summary.get("dropped", {}))
            live_drops = drop_sum([read.dropped for read in reads.values()])
            residual_drops = {field: max(0, old_drops.get(field, 0) - live_drops[field])
                              for field in Dropped().to_json()}
            first_carried = next((session_id for session_id in ids if session_id in carried and session_id in prior_ids), None)
            for session_id in prior_ids:
                old_states[session_id] = {
                    "director": entry.name,
                    "requests": old_turn_counts[entry.name, session_id],
                    "dropped": residual_drops if session_id == first_carried else drop_sum([]),
                    "transcript_gone": session_id in carried,
                    "director_from": cast(str | None, prior_summary.get("director_from")) if session_id == first_carried else None,
                    "whole_session": prior_summary.get("whole_session") is True if session_id == first_carried else False,
                }
        turns = recompute_switch_turns([
            *(turn for read in reads.values() for turn in read.turns),
            *(turn for turn in old_turns if turn.session in carried and turn.name == entry.name),
        ])
        all_turns.extend(turns)
        all_compactions.extend(compaction for read in reads.values() for compaction in read.compactions)
        all_compactions.extend(compaction for compaction in old_compactions if compaction.session in carried and compaction.name == entry.name)
        for session_id in ids:
            if session_id in reads:
                read = reads[session_id]
                states[session_id] = {"director": entry.name, "requests": len(read.turns),
                                      "dropped": drop_sum([read.dropped]), "transcript_gone": False,
                                      "director_from": read.director_from, "whole_session": read.whole_session}
            elif session_id in old_states:
                state = old_states[session_id]
                states[session_id] = {"director": state["director"],
                                      "requests": state["requests"], "dropped": state["dropped"],
                                      "transcript_gone": True, "director_from": state.get("director_from"),
                                      "whole_session": state.get("whole_session", False)}
            else:
                states[session_id] = {"director": entry.name,
                                      "requests": sum(turn.session == session_id for turn in turns),
                                      "dropped": drop_sum([]), "transcript_gone": True,
                                      "director_from": None, "whole_session": False}
        models = dict(Counter(turn.model for turn in turns))
        efforts = dict(Counter(turn.effort for turn in turns))
        first_sonnet = min((turn.started for turn in turns if "sonnet" in turn.model.lower()), default=None)
        first_pdt = pdt(first_sonnet)
        switch = entry.switched_by_pdt
        difference: float | None = None
        if switch is not None and first_sonnet is not None:
            switched_at = datetime.fromisoformat(switch).replace(tzinfo=PDT)
            difference = round((datetime.fromisoformat(first_sonnet) - switched_at).total_seconds() / 60, 1)
        dropped = {field: sum(states[session_id]["dropped"].get(field, 0) for session_id in ids)
                   for field in Dropped().to_json()}
        boundaries = [boundary for session_id in ids
                      if (boundary := states[session_id]["director_from"]) is not None]
        summary: dict[str, object] = {
            "name": entry.name,
            "session_ids": ids,
            "missing_ids": [session_id for session_id in ids if session_id not in found],
            "director_from": min(boundaries) if boundaries else None,
            "whole_session": any(states[session_id]["whole_session"] for session_id in ids),
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
        if verbose:
            carried_note = f" | carried {len(carried)}" if carried else ""
            print(f"{entry.name} | {len(turns)} | {breakdown}; {effort_breakdown} | {first_pdt or '-'} | {switch or '-'} | {difference if difference is not None else '-'} | {drop_text}{carried_note}")
    moment = now or datetime.now(timezone.utc)
    moment = moment.replace(tzinfo=PDT) if moment.tzinfo is None else moment
    result: dict[str, object] = {
        "extracted": moment.astimezone(timezone.utc).isoformat(),
        "sessions": summaries,
        "session_states": states,
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
    _ = extract_parser.add_argument("--now", type=datetime.fromisoformat)
    compare_parser = subcommands.add_parser("compare", help="compare Opus and Sonnet requests")
    _ = compare_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    phases_parser = subcommands.add_parser("phases", help="compare completed plan-delegate phases")
    _ = phases_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    _ = phases_parser.add_argument("--runs-dir", type=Path, default=Path.home() / ".local/state/plan-delegate/runs")
    _ = phases_parser.add_argument("--registry-dir", type=Path, default=Path.home() / ".claude/sessions")
    _ = phases_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    report_parser = subcommands.add_parser("report", help="render director numbers and the default recommendation")
    mode = report_parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--interim", action="store_true")
    _ = mode.add_argument("--final", action="store_true")
    _ = report_parser.add_argument("--message", action="store_true")
    _ = report_parser.add_argument("--no-extract", action="store_true")
    _ = report_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    _ = report_parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    _ = report_parser.add_argument("--registry-dir", type=Path, default=Path.home() / ".claude/sessions")
    _ = report_parser.add_argument("--runs-dir", type=Path, default=Path.home() / ".local/state/plan-delegate/runs")
    _ = report_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    _ = report_parser.add_argument("--now", type=datetime.fromisoformat)
    _ = report_parser.add_argument("--no-history", action="store_true")
    results_parser = subcommands.add_parser("results", help="generate final results documents")
    _ = results_parser.add_argument("--message", action="store_true")
    _ = results_parser.add_argument("--no-extract", action="store_true")
    _ = results_parser.add_argument("--no-history", action="store_true")
    _ = results_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    _ = results_parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    _ = results_parser.add_argument("--registry-dir", type=Path, default=Path.home() / ".claude/sessions")
    _ = results_parser.add_argument("--runs-dir", type=Path, default=Path.home() / ".local/state/plan-delegate/runs")
    _ = results_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    _ = results_parser.add_argument("--docs-dir", type=Path, default=Path(__file__).resolve().parents[2] / "docs/as-built")
    _ = results_parser.add_argument("--now", type=datetime.fromisoformat)
    ready_parser = subcommands.add_parser("ready", help="check the Sonnet sample gate")
    _ = ready_parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("MODEL_STUDY_STATE", DEFAULT_STATE)))
    _ = ready_parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    _ = ready_parser.add_argument("--registry-dir", type=Path, default=Path.home() / ".claude/sessions")
    _ = ready_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    _ = ready_parser.add_argument("--now", type=datetime.fromisoformat)
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
        now = args["now"]
        assert now is None or isinstance(now, datetime)
        _ = extract(roster, projects_dir, registry_dir, state_dir, now=now)
    elif args.get("command") == "compare":
        state_dir = args["state_dir"]
        assert isinstance(state_dir, Path)
        print(compare_markdown(compare(state_dir)))
    elif args.get("command") == "phases":
        state_dir = args["state_dir"]
        runs_dir = args["runs_dir"]
        registry_dir = args["registry_dir"]
        roster = args["roster"]
        assert isinstance(state_dir, Path)
        assert isinstance(runs_dir, Path)
        assert isinstance(registry_dir, Path)
        assert isinstance(roster, Path)
        print(phases_markdown(phases(state_dir, runs_dir, roster, registry_dir)))
    elif args.get("command") in ("report", "results"):
        roster = args["roster"]
        projects_dir = args["projects_dir"]
        registry_dir = args["registry_dir"]
        runs_dir = args["runs_dir"]
        state_dir = args["state_dir"]
        assert isinstance(roster, Path)
        assert isinstance(projects_dir, Path)
        assert isinstance(registry_dir, Path)
        assert isinstance(runs_dir, Path)
        assert isinstance(state_dir, Path)
        now = args["now"]
        assert now is None or isinstance(now, datetime)
        moment = now or datetime.now(PDT)
        if args["no_extract"] is True:
            extracted = cast(study_report.ExtractReport, json.loads((state_dir / "extract.json").read_text()))
        else:
            extracted = cast(study_report.ExtractReport, cast(object, extract(roster, projects_dir, registry_dir, state_dir, verbose=False, now=moment)))
        comparison = compare(state_dir)
        phase_data = phases(state_dir, runs_dir, roster, registry_dir)
        final = args.get("command") == "results" or args.get("final") is True
        current = study_report.history_row(comparison, phase_data, moment, final)
        history, run_history = study_report.record_history(state_dir / "history.jsonl", current, save=args["no_history"] is not True)
        path = state_dir / "report.md"
        atomic_text(path, study_report.render(comparison, phase_data, extracted, moment, final, run_history))
        output_path = path
        if args.get("command") == "results":
            docs_dir = args["docs_dir"]
            assert isinstance(docs_dir, Path)
            md_path, json_path = study_results.document_paths(docs_dir)
            markdown = study_results.render(comparison, phase_data, extracted, moment, run_history)
            compact = study_results.compact_json(comparison, phase_data, history)
            if len(compact.encode("utf-8")) >= study_results.MAX_JSON_BYTES:
                raise SystemExit(4)
            atomic_text(md_path, markdown)
            atomic_text(json_path, compact)
            output_path = md_path
        if args["message"] is True:
            print(study_report.message(comparison, phase_data, extracted, moment, final, path, run_history), end="")
        else:
            print(output_path)
    elif args.get("command") == "ready":
        roster = args["roster"]
        projects_dir = args["projects_dir"]
        registry_dir = args["registry_dir"]
        state_dir = args["state_dir"]
        assert isinstance(roster, Path)
        assert isinstance(projects_dir, Path)
        assert isinstance(registry_dir, Path)
        assert isinstance(state_dir, Path)
        now = args["now"]
        assert now is None or isinstance(now, datetime)
        _ = extract(roster, projects_dir, registry_dir, state_dir, verbose=False, now=now)
        comparison = compare(state_dir)
        gate, lines = study_report.ready(comparison, now)
        print("\n".join(lines))
        if not gate:
            raise SystemExit(3)


if __name__ == "__main__":
    main()
