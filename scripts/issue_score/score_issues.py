#!/usr/bin/env python3
"""Score open issues; keep payloads and decisions in local JSONL run files."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def content_hash(issue: vault.Issue, areas: Sequence[str] = ()) -> str:
    """Hash content plus area context, excluding all current field labels."""
    text = json.dumps({"title": issue.title, "project": issue.project,
                       "body": issue.body, "areas": areas}, sort_keys=True, ensure_ascii=False)
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
                    and record.get("model") == model and isinstance(issue_path, str)
                    and isinstance(digest, str) and isinstance(record.get("payloads"), dict)
                    and isinstance(record.get("answers"), dict) and isinstance(record.get("decided_fields"), dict)):
                cache[(issue_path, digest)] = cast(ScoredIssue, cast(object, record))
    return cache


def _score_issue(issue: vault.Issue, areas: list[str], model: str, state_dir: Path,
                 budget_exhausted: Event) -> ScoredIssue:
    state = vault.build_state(issue)
    area_state: questions.AreaState = {"issue": state["issue"], "areas": list(areas)}
    main_payload: RequestPayload = {"model": model, "state": state, "questions": questions.main_questions()}
    area_payload: RequestPayload = {"model": model, "state": area_state, "questions": questions.area_questions(areas)}

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
    decisions = decide.decide_answers(main["answers"], area["answers"])
    return {
        "issue_path": str(issue.path), "content_hash": content_hash(issue, areas),
        "question_set_version": questions.QUESTION_SET_VERSION, "model": main["model"],
        "payloads": {"main": main_payload, "area": area_payload},
        "answers": {"main": main, "area": area},
        "decided_fields": {name: asdict(decision) for name, decision in decisions.items()},
    }


def score(only: str = "*", limit: int = 0, *, issues_dir: Path = vault.ISSUES_DIR,
          state_dir: Path = STATE_DIR, model: str = questions.MODEL,
          split: Split = "all", fresh: bool = False) -> ScoreRun:
    if limit < 0:
        raise ValueError("--limit must be nonnegative")
    state_dir = state_dir.expanduser().resolve()
    if state_dir.is_relative_to(vault.VAULT_ROOT.resolve()) or state_dir.is_relative_to(issues_dir.resolve()):
        raise ValueError("Result files must be outside the issue vault")
    issues = vault.load_open_issues(issues_dir)
    areas = vault.area_vocabulary(issues)
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
        key = (str(issue.path), content_hash(issue, areas))
        if key in cache:
            return cache[key]
        return _score_issue(issue, areas, model, state_dir, budget_exhausted)

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
    args = parser.parse_args(argv, namespace=CommandArguments())
    try:
        if args.command == "compare":
            print(compare(args.run_id, split=args.split, repeat_run_id=args.repeat))
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
