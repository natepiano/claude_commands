"""Reproducible blinded judgments, with assignments kept off the user sheet."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import random
from typing import cast
from uuid import uuid4

from . import evaluate, questions, vault
from .records import BlindAssignment, BlindFiles, BlindItem, BlindKey, BlindScore, BlindSheet, PreferenceTally, STATE_DIR

Disagreement = tuple[vault.Issue, str | int, str | int]


def _spread_choices(candidates: dict[str, list[Disagreement]], count: int) -> list[tuple[str, vault.Issue, str | int, str | int]]:
    selected: list[tuple[str, vault.Issue, str | int, str | int]] = []
    used: set[Path] = set()
    while len(selected) < count:
        assignments: dict[str, Disagreement] = {}
        owners: dict[Path, str] = {}

        def assign(field: str, visited: set[Path]) -> bool:
            for choice in candidates[field]:
                path = choice[0].path
                if path in used or path in visited:
                    continue
                visited.add(path)
                # Move an earlier field to another issue when this issue is
                # needed to serve another field in the same round.
                if path not in owners or assign(owners[path], visited):
                    owners[path] = field
                    assignments[field] = choice
                    return True
            return False

        for field in candidates:
            if assign(field, set()) and len(assignments) == count - len(selected):
                break
        if not assignments:
            break
        for field, (issue, actual, predicted) in assignments.items():
            selected.append((field, issue, actual, predicted))
            used.add(issue.path)
    return selected


def _safe_directory(state_dir: Path, issues_dir: Path) -> Path:
    directory = state_dir.expanduser().resolve()
    if directory.is_relative_to(vault.VAULT_ROOT.resolve()) or directory.is_relative_to(issues_dir.resolve()):
        raise ValueError("Blind files must be outside the issue vault")
    return directory


def blind(run_id: str, count: int = 20, *, seed: int = 0,
          issues_dir: Path = vault.ISSUES_DIR, state_dir: Path = STATE_DIR) -> BlindFiles:
    if count <= 0:
        raise ValueError("--count must be positive")
    name = run_id.removesuffix(".jsonl")
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("run-id must be a run filename without a directory")
    state_dir = _safe_directory(state_dir, issues_dir)
    run = evaluate.load_run(state_dir / "runs" / f"{name}.jsonl")
    rated = vault.load_rated_issues(issues_dir)
    eligible = [issue for issue in rated.issues if not issue.is_style
                and issue.path.stem not in questions.HISTORICAL_EFFORT_EXAMPLE_STEMS
                and evaluate.in_split(issue.path, "holdout") and str(issue.path) in run.scored]
    rng = random.Random(seed)
    candidates: dict[str, list[Disagreement]] = {}
    for field in evaluate.FIELDS:
        choices: list[Disagreement] = []
        for issue in eligible:
            if not issue.labels.get(field, ""):
                continue
            actual: str | int = issue.labels[field]
            if field in vault.RATING_FIELDS:
                actual = actual.count("⭐")
            elif field == "area":
                actual = vault.normalize_area(actual)
            decision = _object(run.scored[str(issue.path)]["decided_fields"][field])
            predicted = decision.get("value")
            if not isinstance(predicted, (str, int)) or isinstance(predicted, bool):
                raise ValueError(f"Invalid saved value for {field}")
            if actual != predicted:
                choices.append((issue, actual, predicted))
        rng.shuffle(choices)
        candidates[field] = choices
    selected = _spread_choices(candidates, count)
    if not selected:
        raise ValueError("No eligible held-out disagreements for a blind sheet")
    rng.shuffle(selected)
    items: list[BlindItem] = []
    assignments: list[BlindAssignment] = []
    for index, (field, issue, actual, predicted) in enumerate(selected, 1):
        identifier = str(index)
        jev_option = rng.choice(("A", "B"))
        first, second = (predicted, actual) if jev_option == "A" else (actual, predicted)
        items.append({"id": identifier, "field": field, "title": issue.title,
                      "body": vault.clean_body(issue.body), "A": first, "B": second, "pick": ""})
        assignments.append({"id": identifier, "field": field, "issue_path": str(issue.path),
                            "jev_option": jev_option, "A": first, "B": second})
    sheet: BlindSheet = {"run_id": name, "instructions": "Set each pick to A, B, or same. Leave other values unchanged.", "items": items}
    key: BlindKey = {"seed": seed, "run_id": name, "entries": assignments}
    directory = state_dir / "blind"
    keys = directory / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{uuid4().hex[:8]}"
    sheet_path, key_path = directory / f"{stem}.json", keys / f"{stem}.json"
    # Create the assignment file privately; only the sheet path is printed by CLI.
    with key_path.open("x", encoding="utf-8") as output:
        key_path.chmod(0o600)
        _ = output.write(json.dumps(key, ensure_ascii=False, indent=2) + "\n")
    with sheet_path.open("x", encoding="utf-8") as output:
        _ = output.write(json.dumps(sheet, ensure_ascii=False, indent=2) + "\n")
    return BlindFiles(sheet_path, key_path)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object in blind files")
    return cast(dict[str, object], value)


def _entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Expected a list in blind files")
    return [_object(item) for item in cast(list[object], value)]


def blind_score(sheet: Path) -> BlindScore:
    sheet = sheet.expanduser().resolve()
    if sheet.is_relative_to(vault.VAULT_ROOT.resolve()):
        raise ValueError("Blind files must be outside the issue vault")
    submitted = _object(cast(object, json.loads(sheet.read_text(encoding="utf-8"))))
    key = _object(cast(object, json.loads((sheet.parent / "keys" / sheet.name).read_text(encoding="utf-8"))))
    if submitted.get("run_id") != key.get("run_id"):
        raise ValueError("Blind sheet does not match its key")
    assignments = {str(entry["id"]): entry for entry in _entries(key.get("entries"))}
    counts: dict[str, PreferenceTally] = {}
    unanswered = 0
    seen: set[str] = set()
    for item in _entries(submitted.get("items")):
        identifier = str(item.get("id"))
        if identifier not in assignments or identifier in seen:
            raise ValueError("Blind sheet has unknown or duplicate item IDs")
        seen.add(identifier)
        assignment = assignments[identifier]
        if any(item.get(name) != assignment.get(name) for name in ("field", "A", "B")):
            raise ValueError("Blind sheet options changed; restore the original sheet")
        pick = item.get("pick")
        if pick not in ("", "A", "B", "same"):
            raise ValueError(f"Invalid pick for item {identifier}: choose A, B, or same")
        field = str(assignment["field"])
        current = counts.setdefault(field, PreferenceTally(0, 0, 0))
        if pick == "":
            unanswered += 1
        elif pick == "same":
            counts[field] = replace(current, same=current.same + 1)
        elif pick == assignment["jev_option"]:
            counts[field] = replace(current, jev=current.jev + 1)
        else:
            counts[field] = replace(current, vault=current.vault + 1)
    if seen != assignments.keys():
        raise ValueError("Blind sheet is missing items; retain rows with an empty pick")
    lines = [f"# Blind preference results{' — partial' if unanswered else ''}", "",
             f"Answered: {len(seen) - unanswered}; unanswered: {unanswered}.", "",
             "| Field | N | Jev | Vault | Same | Jev share | Vault share | Same share |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for field, tally in sorted(counts.items()):
        total = tally.jev + tally.vault + tally.same
        shares = [f"{number / total:.4f}" if total else "N/A" for number in (tally.jev, tally.vault, tally.same)]
        lines.append(f"| {field} | {total} | {tally.jev} | {tally.vault} | {tally.same} | " + " | ".join(shares) + " |")
    result_path = sheet.with_suffix(".results.md")
    _ = result_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return BlindScore(result_path, counts, unanswered)
