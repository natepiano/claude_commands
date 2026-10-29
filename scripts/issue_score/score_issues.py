#!/usr/bin/env python3
"""Score open issues; keep payloads and decisions in local JSONL run files."""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
from pathlib import Path
import sys
from threading import Event
from typing import cast
from uuid import uuid4

# Support the documented direct script invocation and package imports in tests.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.issue_score import client, decide, questions, vault
from scripts.issue_score import experiment as wording
from scripts.issue_score.blind import blind, blind_score
from scripts.issue_score.evaluate import Split, compare, in_split
from scripts.issue_score.records import FailedIssue, RequestPayload, ScoredIssue, ScoreRun, STATE_DIR

WORKERS = 8


class CommandArguments(argparse.Namespace):
    command: str = "score"
    only: str = "*"
    limit: int = 0
    split: Split = "all"
    fresh: bool = False
    run_id: str = ""
    repeat: str = ""
    baseline: str = ""
    variants: str = ""
    count: int = 20
    seed: int = 0
    sheet: str = ""


def content_hash(issue: vault.Issue, areas: Sequence[str], definitions: Mapping[str, str]) -> str:
    """Hash content plus area context, excluding all current field labels."""
    text = json.dumps({"title": issue.title, "project": issue.project,
                       "body": issue.body, "areas": areas,
                       "area_definitions": {area: definitions[area] for area in areas if area in definitions}},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cache(state_dir: Path, model: str = questions.MODEL) -> dict[tuple[str, str], ScoredIssue]:
    cache: dict[tuple[str, str], ScoredIssue] = {}
    for path in sorted((state_dir / "runs").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                # A killed previous run can leave a partial last line.
                continue
            if not isinstance(value, dict):
                continue
            record = cast(dict[str, object], value)
            issue_path = record.get("issue_path")
            digest = record.get("content_hash")
            if (record.get("question_set_version") == questions.QUESTION_SET_VERSION
                    and "variant_set" not in record
                    and record.get("model") == model and isinstance(issue_path, str)
                    and isinstance(digest, str) and isinstance(record.get("payloads"), dict)
                    and isinstance(record.get("answers"), dict) and isinstance(record.get("decided_fields"), dict)):
                cache[(issue_path, digest)] = cast(ScoredIssue, cast(object, record))
    return cache


def _decided_fields(main: client.Response, area: client.Response) -> dict[str, object]:
    decisions = decide.decide_answers(main["answers"], area["answers"])
    return {name: asdict(decision) for name, decision in decisions.items()}


def _score_issue(issue: vault.Issue, areas: list[str], model: str, state_dir: Path,
                 budget_exhausted: Event, definitions: Mapping[str, str]) -> ScoredIssue:
    return _issue_judgments(issue, areas, model, state_dir, budget_exhausted, {}, "", definitions)["production"]


def _issue_judgments(issue: vault.Issue, areas: list[str], model: str, state_dir: Path,
                     budget_exhausted: Event, variants: wording.VariantSets, experiment_id: str,
                     definitions: Mapping[str, str]) -> dict[str, ScoredIssue]:
    state = vault.build_state(issue)
    area_state: questions.AreaState = {"issue": state["issue"], "areas": list(areas)}
    main_payload: RequestPayload = {"model": model, "state": state, "questions": wording.build_questions(questions.main_questions(), variants)}
    area_payload: RequestPayload = {"model": model, "state": area_state, "questions": wording.build_questions(questions.area_questions(areas, definitions), variants)}

    def request(payload: RequestPayload) -> client.Response:
        if budget_exhausted.is_set():
            raise client.BudgetExceeded("TypeSafe run stopped because its budget was exhausted")
        try:
            return client.system_one(payload["state"], payload["questions"], model,
                                     ledger_path=state_dir / "spend.jsonl")
        except client.BudgetExceeded:
            budget_exhausted.set()
            raise

    main = request(main_payload)
    area = request(area_payload)
    if main["model"] != area["model"]:
        raise client.TypeSafeError("Main and area requests were answered by different models")
    results: dict[str, ScoredIssue] = {}
    for variant in ("production", *variants):
        selected_main = wording.project_response(main, variant, variants)
        selected_area = wording.project_response(area, variant, variants)
        record: ScoredIssue = {
            "issue_path": str(issue.path), "content_hash": content_hash(issue, areas, definitions),
            "question_set_version": questions.QUESTION_SET_VERSION, "model": main["model"],
            "payloads": {"main": main_payload, "area": area_payload},
            "answers": {"main": selected_main, "area": selected_area},
            "decided_fields": _decided_fields(selected_main, selected_area),
        }
        if variants:
            record["variant_set"] = variant
            record["variant_definitions"] = variants.get(variant, {})
            record["experiment_id"] = experiment_id
            record["experiment_answers"] = {"main": main, "area": area}
        results[variant] = record
    return results


def experiment(variants: wording.VariantSets, only: str = "*", limit: int = 0, *,
               issues_dir: Path = vault.ISSUES_DIR, state_dir: Path = STATE_DIR,
               model: str = questions.MODEL,
               area_definitions_path: Path = vault.AREA_DEFINITIONS_PATH) -> dict[str, ScoreRun]:
    """Ask production and variants together, freshly, on tuning issues only."""
    if limit < 0 or not variants:
        raise ValueError("Experiments require variants and a nonnegative limit")
    state_dir = state_dir.expanduser().resolve()
    if state_dir.is_relative_to(vault.VAULT_ROOT.resolve()) or state_dir.is_relative_to(issues_dir.resolve()):
        raise ValueError("Result files must be outside the issue vault")
    issues = vault.load_open_issues(issues_dir)
    areas = vault.area_vocabulary(issues)
    definitions = vault.load_area_definitions(area_definitions_path)
    known = questions.main_questions() | questions.area_questions(areas, definitions)
    if any(name not in known for replacements in variants.values() for name in replacements):
        raise ValueError("Variant references an unknown production question")
    # Validate every replacement before incurring a request or opening result files.
    _ = wording.build_questions(known, variants)
    selected = [issue for issue in issues if in_split(issue.path, "tune") and
                (fnmatch.fnmatch(issue.path.name, only) or fnmatch.fnmatch(issue.path.stem, only)
                 or fnmatch.fnmatch(str(issue.path), only))]
    if limit:
        selected = selected[:limit]
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:8]
    runs = state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    paths = {variant: runs / f"{identifier}-{variant}.jsonl" for variant in ("production", *variants)}
    failures = dict.fromkeys(paths, 0)
    budget_exhausted = Event()
    # Open all run files before requests so even an empty experiment has artifacts.
    with ExitStack() as stack:
        outputs = {variant: stack.enter_context(path.open("x", encoding="utf-8")) for variant, path in paths.items()}
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for start in range(0, len(selected), WORKERS):
                if budget_exhausted.is_set():
                    break
                pending = {executor.submit(_issue_judgments, issue, areas, model, state_dir,
                                           budget_exhausted, variants, identifier, definitions): issue for issue in selected[start:start + WORKERS]}
                for future in as_completed(pending):
                    try:
                        results = future.result()
                    except Exception as error:
                        for variant, output in outputs.items():
                            failures[variant] += 1
                            _ = output.write(json.dumps({"issue_path": str(pending[future].path), "error": str(error)}, ensure_ascii=False) + "\n")
                            output.flush()
                    else:
                        for variant, record in results.items():
                            _ = outputs[variant].write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                            outputs[variant].flush()
    return {variant: ScoreRun(path, failures[variant]) for variant, path in paths.items()}


def score(only: str = "*", limit: int = 0, *, issues_dir: Path = vault.ISSUES_DIR,
          state_dir: Path = STATE_DIR, model: str = questions.MODEL,
          split: Split = "all", fresh: bool = False,
          area_definitions_path: Path = vault.AREA_DEFINITIONS_PATH) -> ScoreRun:
    if limit < 0:
        raise ValueError("--limit must be nonnegative")
    state_dir = state_dir.expanduser().resolve()
    if state_dir.is_relative_to(vault.VAULT_ROOT.resolve()) or state_dir.is_relative_to(issues_dir.resolve()):
        raise ValueError("Result files must be outside the issue vault")
    issues = vault.load_open_issues(issues_dir)
    areas = vault.area_vocabulary(issues)
    definitions = vault.load_area_definitions(area_definitions_path)
    selected = [issue for issue in issues if in_split(issue.path, split) and (fnmatch.fnmatch(issue.path.name, only)
                or fnmatch.fnmatch(issue.path.stem, only) or fnmatch.fnmatch(str(issue.path), only))]
    if limit:
        selected = selected[:limit]
    cache = {} if fresh else load_cache(state_dir, model)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:8]
    runs = state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    result_path = runs / f"{run_id}.jsonl"
    budget_exhausted = Event()
    failure_count = 0

    def result(issue: vault.Issue) -> ScoredIssue:
        key = (str(issue.path), content_hash(issue, areas, definitions))
        if key in cache:
            record = cache[key].copy()
            record["decided_fields"] = _decided_fields(record["answers"]["main"], record["answers"]["area"])
            return record
        return _score_issue(issue, areas, model, state_dir, budget_exhausted, definitions)

    with result_path.open("x", encoding="utf-8") as output:
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            # Submit bounded batches rather than queuing the entire vault.
            for start in range(0, len(selected), WORKERS):
                if budget_exhausted.is_set():
                    break
                pending = {executor.submit(result, issue): issue for issue in selected[start:start + WORKERS]}
                for future in as_completed(pending):
                    record: ScoredIssue | FailedIssue
                    try:
                        record = future.result()
                    except Exception as error:
                        failure_count += 1
                        record = {"issue_path": str(pending[future].path), "error": str(error)}
                    _ = output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    output.flush()
    return ScoreRun(result_path, failure_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    score_command = commands.add_parser("score", help="Score open non-style issues")
    _ = score_command.add_argument("--only", default="*", help="Issue filename or path glob")
    _ = score_command.add_argument("--limit", type=int, default=0, help="Maximum issues; 0 means all")
    _ = score_command.add_argument("--split", choices=("tune", "holdout", "all"), default="all")
    _ = score_command.add_argument("--fresh", action="store_true", help="Skip cached responses for a repeat run")
    compare_command = commands.add_parser("compare", help="Compare a saved run with current confirmed labels")
    _ = compare_command.add_argument("run_id")
    _ = compare_command.add_argument("--split", choices=("tune", "holdout", "all"), default="all")
    _ = compare_command.add_argument("--repeat", default="", help="Second run ID for the repeat check")
    _ = compare_command.add_argument("--baseline", default="", help="Compare both runs on one shared cohort")
    experiment_command = commands.add_parser("experiment", help="Fresh paired wording questions on tuning issues")
    _ = experiment_command.add_argument("--variants", required=True, help="JSON variant sets")
    _ = experiment_command.add_argument("--only", default="*")
    _ = experiment_command.add_argument("--limit", type=int, default=0)
    blind_command = commands.add_parser("blind", help="Write a held-out A/B preference sheet")
    _ = blind_command.add_argument("run_id")
    _ = blind_command.add_argument("--count", type=int, default=20)
    _ = blind_command.add_argument("--seed", type=int, default=0)
    blind_score_command = commands.add_parser("blind-score", help="Tally a completed A/B preference sheet")
    _ = blind_score_command.add_argument("sheet")
    args = parser.parse_args(argv, namespace=CommandArguments())
    try:
        if args.command == "compare":
            print(compare(args.run_id, split=args.split, repeat_run_id=args.repeat, baseline_run_id=args.baseline))
            return 0
        if args.command == "experiment":
            runs = experiment(wording.load_variants(Path(args.variants)), args.only, args.limit)
            for variant, result in runs.items():
                print(f"{variant}: {result.result_path}")
            failed = sum(result.failure_count for result in runs.values())
            if failed:
                print(f"issue-score: {failed} variant issue results failed", file=sys.stderr)
            return int(bool(failed))
        if args.command == "blind":
            print(blind(args.run_id, args.count, seed=args.seed).sheet_path)
            return 0
        if args.command == "blind-score":
            print(blind_score(Path(args.sheet)).result_path)
            return 0
        run = score(args.only, args.limit, split=args.split, fresh=args.fresh)
    except (client.TypeSafeError, OSError, ValueError) as error:
        print(f"issue-score: {error}", file=sys.stderr)
        return 1
    print(run.result_path)
    if run.failure_count:
        noun = "issue" if run.failure_count == 1 else "issues"
        print(f"issue-score: {run.failure_count} {noun} failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
