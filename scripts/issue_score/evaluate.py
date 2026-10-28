"""Evaluate saved judgments against current, confirmed vault labels."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import stat
import sys
from typing import Literal, cast

from . import client, decide, questions, vault
from .records import ScoredIssue, STATE_DIR

# renumber.py imports writer_lock by bare name, so its directory must be on sys.path.
PRIORITIZE_DIR = str(Path(__file__).resolve().parents[1] / "prioritize")
if PRIORITIZE_DIR not in sys.path:
    sys.path.insert(0, PRIORITIZE_DIR)
from scripts.prioritize import renumber

Split = Literal["tune", "holdout", "all"]
FieldValue = str | int
ORDINAL_FIELDS = vault.RATING_FIELDS
CHOICE_FIELDS = ("backlog_goal", "category", "area")
FIELDS = (*ORDINAL_FIELDS, *CHOICE_FIELDS)


@dataclass(frozen=True)
class UndefinedMetric:
    reason: str


Metric = float | UndefinedMetric


@dataclass(frozen=True)
class ExactMatchOnly:
    reason: str


@dataclass(frozen=True)
class FieldObservation:
    issue: vault.Issue
    actual: FieldValue
    predicted: FieldValue
    confidence: float
    can_be_sure: bool
    probabilities: dict[str, float] | ExactMatchOnly


@dataclass(frozen=True)
class RunJudgments:
    scored: dict[str, ScoredIssue]
    failed: dict[str, str]


def split_for(filename: str | Path) -> Literal["tune", "holdout"]:
    digest = hashlib.sha1(Path(filename).name.encode("utf-8")).hexdigest()
    return "tune" if int(digest, 16) % 10 < 7 else "holdout"


def in_split(filename: str | Path, split: Split) -> bool:
    return split == "all" or split_for(filename) == split


def _paired(actual: Sequence[object], predicted: Sequence[object]) -> None:
    if len(actual) != len(predicted):
        raise ValueError("Actual and predicted values must have equal lengths")


def confusion_matrix(actual: Sequence[FieldValue], predicted: Sequence[FieldValue],
                     labels: Sequence[FieldValue]) -> list[list[int]]:
    _paired(actual, predicted)
    index = {value: i for i, value in enumerate(labels)}
    if len(index) != len(labels):
        raise ValueError("Confusion labels must be unique")
    result = [[0 for _ in labels] for _ in labels]
    for observed, proposed in zip(actual, predicted):
        if observed not in index or proposed not in index:
            raise ValueError("Value outside confusion labels")
        result[index[observed]][index[proposed]] += 1
    return result


def quadratic_weighted_kappa(actual: Sequence[int], predicted: Sequence[int], levels: int = 5) -> Metric:
    """Quadratic disagreement relative to independent empirical marginals.

    Ordinal labels may be zero-based or one-based; only their distances matter.
    """
    _paired(actual, predicted)
    if levels < 2:
        raise ValueError("Kappa requires at least two levels")
    if not actual:
        return UndefinedMetric("no pairs")
    observed = sum((a - p) ** 2 for a, p in zip(actual, predicted)) / len(actual)
    actual_counts, predicted_counts = Counter(actual), Counter(predicted)
    expected = sum((a - p) ** 2 * a_count * p_count
                   for a, a_count in actual_counts.items()
                   for p, p_count in predicted_counts.items()) / len(actual) ** 2
    if expected == 0:
        return UndefinedMetric("constant identical ratings")
    return 1 - observed / expected


def ranked_probability_score(probabilities: Sequence[float], actual: int) -> float:
    """Normalized ordinal RPS; actual is a zero-based class index."""
    _distribution(probabilities)
    if len(probabilities) < 2 or not 0 <= actual < len(probabilities):
        raise ValueError("RPS requires a valid class among at least two levels")
    cumulative = 0.0
    error = 0.0
    for level, probability in enumerate(probabilities[:-1]):
        cumulative += probability
        error += (cumulative - int(actual <= level)) ** 2
    return error / (len(probabilities) - 1)


def _distribution(probabilities: Sequence[float]) -> None:
    if (not probabilities or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities)
            or abs(sum(probabilities) - 1) > 0.02):
        raise ValueError("Expected a probability distribution")


def brier_score(probabilities: Mapping[str, float], actual: str) -> float:
    """Multiclass Brier score, summed over classes without division by K."""
    _distribution(list(probabilities.values()))
    # Labels outside a run's vocabulary have zero predicted mass.
    return sum((probability - int(option == actual)) ** 2
               for option, probability in probabilities.items()) + int(actual not in probabilities)


def _midranks(values: Sequence[float]) -> list[float]:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Ranks require finite numbers")
    counts = Counter(values)
    ranks: dict[float, float] = {}
    preceding = 0
    for value in sorted(counts):
        ranks[value] = preceding + (counts[value] + 1) / 2
        preceding += counts[value]
    return [ranks[value] for value in values]


def spearman(actual: Sequence[float], predicted: Sequence[float]) -> Metric:
    """Pearson correlation of ranks, assigning midranks to ties."""
    _paired(actual, predicted)
    if len(actual) < 2:
        return UndefinedMetric("fewer than two pairs")
    a_ranks, p_ranks = _midranks(actual), _midranks(predicted)
    center = (len(actual) + 1) / 2
    covariance = sum((a - center) * (p - center) for a, p in zip(a_ranks, p_ranks))
    denominator = math.sqrt(sum((a - center) ** 2 for a in a_ranks)
                            * sum((p - center) ** 2 for p in p_ranks))
    if denominator == 0:
        return UndefinedMetric("constant ranks")
    return covariance / denominator


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in run")
    return cast(dict[str, object], value)


def _load_run(path: Path) -> RunJudgments:
    scored: dict[str, ScoredIssue] = {}
    failed: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = _object(cast(object, json.loads(line)))
        issue_path = record.get("issue_path")
        if not isinstance(issue_path, str):
            raise ValueError(f"{path}:{number}: run line has no issue_path")
        if "error" in record:
            failed[issue_path] = str(record["error"])
            _ = scored.pop(issue_path, None)
        elif all(key in record for key in ("content_hash", "question_set_version", "model", "payloads", "answers", "decided_fields")):
            scored[issue_path] = cast("ScoredIssue", cast(object, record))
            _ = failed.pop(issue_path, None)
        else:
            raise ValueError(f"{path}:{number}: incomplete result line")
    return RunJudgments(scored, failed)


def _choice(answers: Mapping[str, client.Answer], name: str) -> client.ChoiceAnswer:
    answer = answers[name]
    if answer["type"] != "choice":
        raise ValueError(f"Expected Choice answer {name}")
    return answer


def _score(answers: Mapping[str, client.Answer], name: str) -> client.ScoreAnswer:
    answer = answers[name]
    if answer["type"] != "score":
        raise ValueError(f"Expected Score answer {name}")
    return answer


def _noul(answers: Mapping[str, client.Answer], name: str) -> float:
    answer = answers[name]
    if answer["type"] != "noul":
        raise ValueError(f"Expected Noul answer {name}")
    return answer["noul"]


def _agree(answers: Mapping[str, client.Answer], name: str) -> bool:
    first, reverse = _choice(answers, name), _choice(answers, name + "_reverse")

    def winner(distribution: Mapping[str, float]) -> str:
        return min(distribution, key=lambda key: (-distribution[key], key))

    return winner(first["probabilities"]) == winner(reverse["probabilities"])


def _observation(issue: vault.Issue, record: ScoredIssue, field: str) -> FieldObservation:
    decision = _object(record["decided_fields"][field])
    predicted = decision["value"]
    if not isinstance(predicted, (str, int)) or isinstance(predicted, bool):
        raise ValueError(f"Invalid decision for {field}")
    confidence = decision["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError(f"Invalid confidence for {field}")
    main = record["answers"]["main"]["answers"]
    area = record["answers"]["area"]["answers"]
    actual: FieldValue = issue.labels.get(field, "")
    can_be_sure = not decision.get("split", False)
    probabilities: dict[str, float] | ExactMatchOnly
    if field in ORDINAL_FIELDS:
        actual = str(actual).count("⭐")
        if not 1 <= actual <= 5 or not isinstance(predicted, int) or not 1 <= predicted <= 5:
            raise ValueError(f"Invalid stars for {field}")
        name = field.removeprefix("backlog_")
        if field == "backlog_alignment":
            goal_decision = _object(record["decided_fields"]["backlog_goal"])
            goal_value = goal_decision["value"]
            goal_names = {label: name for name, label in questions.GOAL_LABELS.items()}
            if not isinstance(goal_value, str) or goal_value not in goal_names:
                raise ValueError(f"Invalid backlog_goal value: {goal_value!r}")
            goal_name = goal_names[goal_value]
            name = "alignment_" + goal_name
            goal_confidence = goal_decision["confidence"]
            if not isinstance(goal_confidence, (float, int)):
                raise ValueError("Invalid goal confidence")
            confidence = min(confidence, goal_confidence)
            can_be_sure = can_be_sure and _agree(main, "goal")
        if field == "backlog_urgency":
            evidence = [_noul(main, name) for name in questions.URGENCY_EVIDENCE]
            can_be_sure = can_be_sure and not any(questions.EVIDENCE_ABSENT <= p <= questions.EVIDENCE_PRESENT for p in evidence)
            if predicted >= 4:
                probabilities = ExactMatchOnly("urgency from evidence")
                return FieldObservation(issue, actual, predicted, confidence, can_be_sure, probabilities)
            name = "pressure"
        answer = _score(main, name)
        probabilities = {str(int(level) + 1): p for level, p in answer["probabilities"].items()}
    else:
        answers, name = (area, "area") if field == "area" else (main, "goal" if field == "backlog_goal" else field)
        if field == "area":
            actual = vault.normalize_area(str(actual))
            if predicted == "unfiled" and _noul(area, "area_fits") < questions.EVIDENCE_ABSENT:
                return FieldObservation(issue, actual, predicted, confidence, True, ExactMatchOnly("unfiled area"))
            can_be_sure = can_be_sure and _noul(area, "area_fits") > questions.EVIDENCE_PRESENT
        can_be_sure = can_be_sure and _agree(answers, name)
        probabilities = decide.average_choice(_choice(answers, name), _choice(answers, name + "_reverse"))
        if field == "backlog_goal":
            probabilities = {questions.GOAL_LABELS[name]: p for name, p in probabilities.items()}
    return FieldObservation(issue, actual, predicted, confidence, can_be_sure, probabilities)


def _mean(values: Sequence[float]) -> Metric:
    return sum(values) / len(values) if values else UndefinedMetric("no observations")


def _metric(value: Metric) -> str:
    return f"N/A ({value.reason})" if isinstance(value, UndefinedMetric) else f"{value:.4f}"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[object], rows: Sequence[Sequence[object]]) -> list[str]:
    return ["| " + " | ".join(_cell(value) for value in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows), ""]


def _field_report(field: str, observations: list[FieldObservation]) -> list[str]:
    ordinal = field in ORDINAL_FIELDS
    probabilistic = [item for item in observations if isinstance(item.probabilities, dict)]
    exact = _mean([float(item.actual == item.predicted) for item in observations])
    lines = [f"#### {field}", ""]
    if ordinal:
        actual = [int(item.actual) for item in probabilistic]
        predicted = [int(item.predicted) for item in probabilistic]
        rps = [ranked_probability_score([cast(dict[str, float], item.probabilities).get(str(level), 0.0)
                                        for level in range(1, 6)], int(item.actual) - 1) for item in probabilistic]
        lines += _table(["N", "Exact", "Ordinal N", "Within one", "MAE", "Quadratic-weighted kappa", "RPS"], [[
            len(observations), _metric(exact), len(probabilistic),
            _metric(_mean([float(abs(a - p) <= 1) for a, p in zip(actual, predicted)])),
            _metric(_mean([float(abs(a - p)) for a, p in zip(actual, predicted)])),
            _metric(quadratic_weighted_kappa(actual, predicted)), _metric(_mean(rps)),
        ]])
        labels: list[FieldValue] = list(range(1, 6))
    else:
        brier = [brier_score(cast(dict[str, float], item.probabilities), str(item.actual)) for item in probabilistic]
        lines += _table(["N", "Accuracy", "Probability N", "Brier"], [[len(observations), _metric(exact), len(probabilistic), _metric(_mean(brier))]])
        labels = sorted({str(item.actual) for item in observations} | {str(item.predicted) for item in observations})
    lines += ["Confusion matrix (rows: current label; columns: Jev):", ""]
    matrix = confusion_matrix([item.actual for item in observations], [item.predicted for item in observations], labels)
    lines += _table(["Label / Jev", *labels], [[label, *row] for label, row in zip(labels, matrix)])
    if not ordinal:
        rows: list[list[object]] = []
        for bucket in range(5):
            low, high = bucket / 5, (bucket + 1) / 5
            items = [item for item in probabilistic
                     if low <= cast(dict[str, float], item.probabilities).get(str(item.predicted), 0.0)
                     and (cast(dict[str, float], item.probabilities).get(str(item.predicted), 0.0) < high or bucket == 4)]
            # Confidence is read from the averaged API distribution, not from a
            # potentially incomplete synthetic decision distribution.
            confidences = [cast(dict[str, float], item.probabilities).get(str(item.predicted), 0.0) for item in items]
            rows.append([f"{low:.2f}–{high:.2f}" + (" (inclusive)" if bucket == 4 else ""), len(items),
                         _metric(_mean(confidences)), _metric(_mean([float(item.actual == item.predicted) for item in items]))])
        lines += ["Reliability (five confidence buckets):", "", *_table(["Confidence", "N", "Mean confidence", "Accuracy"], rows)]
    return lines


def _risk_report(field: str, observations: list[FieldObservation]) -> list[str]:
    rows: list[list[object]] = []
    for percent in range(40, 100, 5):
        threshold = percent / 100
        covered = [item for item in observations if item.can_be_sure and item.confidence >= threshold]
        row: list[object] = [f"{threshold:.2f}", len(covered), _metric(float(len(covered) / len(observations))
                                                                   if observations else UndefinedMetric("no observations")),
                             _metric(_mean([float(item.actual != item.predicted) for item in covered]))]
        if field in ORDINAL_FIELDS:
            ordinal = [item for item in covered if isinstance(item.probabilities, dict)]
            row.append(_metric(_mean([float(abs(int(item.actual) - int(item.predicted)) > 1) for item in ordinal])))
        rows.append(row)
    headers = ["Threshold", "Sure N", "Coverage", "Exact error"]
    if field in ORDINAL_FIELDS:
        headers.append("Beyond-one error")
    return [f"### {field}", "", *_table(headers, rows)]


def _goals(path: Path) -> dict[str, renumber.Goal]:
    content = path.read_bytes()
    metadata = path.stat()
    source = renumber.SourceFile(path, content, stat.S_IMODE(metadata.st_mode),
                                 (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns),
                                 hashlib.sha256(content).hexdigest())
    try:
        return {goal.value: goal for goal in renumber.parse_goals(source)}
    except renumber.PlanningError as error:
        raise ValueError(str(error)) from None


def _ordering(issues: Sequence[vault.Issue], run: RunJudgments, goals: Mapping[str, renumber.Goal]) -> list[str]:
    actual: list[float] = []
    predicted: list[float] = []
    for issue in issues:
        record = run.scored[str(issue.path)]
        fields = {field: _object(value)["value"] for field, value in record["decided_fields"].items()}
        goal = str(fields["backlog_goal"])
        try:
            score = float(issue.labels.get("backlog_score", ""))
        except ValueError:
            continue
        if not math.isfinite(score) or goal not in goals:
            continue
        actual.append(score)
        predicted.append(float(renumber.calculate_score(goals[goal], {field: int(cast(int, fields[field])) for field in ORDINAL_FIELDS})))
    return [f"Ordering pairs: {len(actual)}; missing/invalid current backlog_score or goal: {len(issues) - len(actual)}.", "",
            f"Spearman correlation: {_metric(spearman(actual, predicted))}.", ""]


def _run_path(run_id: str, state_dir: Path) -> Path:
    name = run_id.removesuffix(".jsonl")
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("run-id must be a run filename without a directory")
    return state_dir / "runs" / (name + ".jsonl")


def compare(run_id: str, *, split: Split = "all", repeat_run_id: str = "",
            issues_dir: Path = vault.ISSUES_DIR, state_dir: Path = STATE_DIR,
            goals_path: Path = vault.VAULT_ROOT / "prioritization goals.md") -> Path:
    """Write a report from saved answers; this function never contacts the API."""
    state_dir = state_dir.expanduser().resolve()
    if state_dir.is_relative_to(vault.VAULT_ROOT.resolve()) or state_dir.is_relative_to(issues_dir.resolve()):
        raise ValueError("Reports must be outside the issue vault")
    run_path = _run_path(run_id, state_dir)
    run = _load_run(run_path)
    rated = vault.load_rated_issues(issues_dir)
    in_scope = [issue for issue in rated.issues if in_split(issue.path, split)]
    styles = [issue for issue in in_scope if issue.is_style]
    examples = [issue for issue in in_scope if not issue.is_style and issue.path.stem in questions.EFFORT_EXAMPLE_STEMS]
    eligible = [issue for issue in in_scope if not issue.is_style and issue.path.stem not in questions.EFFORT_EXAMPLE_STEMS]
    scored = [issue for issue in eligible if str(issue.path) in run.scored]
    failed = [issue for issue in eligible if str(issue.path) in run.failed]
    unscored = [issue for issue in eligible if str(issue.path) not in run.scored and str(issue.path) not in run.failed]
    partial = bool(failed or unscored)
    skipped = [item for item in rated.skipped if in_split(item.path, split)]
    observations = {field: [_observation(issue, run.scored[str(issue.path)], field) for issue in scored
                             if issue.labels.get(field, "")] for field in FIELDS}
    lines = [f"# Issue score comparison{' — partial' if partial else ''}: {run_path.stem} ({split})", "",
             f"Scored: {len(scored)}; failed: {len(failed)}; unscored: {len(unscored)}; eligible rated issues: {len(eligible)}.", "",
             f"Excluded effort examples: {len(examples)}; admitted style issues excluded: {len(styles)}; loader skips: {len(skipped)}.", "",
             "Labels and backlog_score are read from the current vault, never from the run. All metrics exclude effort examples and style issues.", "",
             "RPS is normalized by K−1 on the five-star rubric. Pressure mass is zero at stars 4 and 5. Evidence-based urgency 4 and derived unfiled fallback areas receive exact-match metrics only. Brier is the sum over classes.", ""]
    models = sorted({run.scored[str(issue.path)]["model"] for issue in scored})
    versions = sorted({run.scored[str(issue.path)]["question_set_version"] for issue in scored})
    lines += [f"Models: {', '.join(models) or 'none'}; question versions: {', '.join(versions) or 'none'}.", ""]
    goals = _goals(goals_path)
    groups = [("all", scored), ("title-only", [issue for issue in scored if len(vault.clean_body(issue.body)) < 200])]
    high: list[vault.Issue] = []
    for issue in scored:
        try:
            if float(issue.labels.get("backlog_score", "")) >= 20:
                high.append(issue)
        except ValueError:
            continue
    groups.append(("score ≥ 20", high))
    for name, issues in groups:
        lines += [f"## Group: {name} (N={len(issues)})", "", *_ordering(issues, run, goals)]
        paths = {issue.path for issue in issues}
        for field in FIELDS:
            lines += _field_report(field, [item for item in observations[field] if item.issue.path in paths])
    lines += ["## Risk-coverage", "", "Structural uncertainty (split answers, order disagreement, ambiguous evidence) remains unsure at every threshold. Alignment also requires the chosen goal's confidence to meet the threshold.", ""]
    for field in FIELDS:
        lines += _risk_report(field, observations[field])
    lines += ["## Largest disagreements (up to 20 per field)", ""]
    for field in FIELDS:
        def gap(item: FieldObservation) -> float:
            if field in ORDINAL_FIELDS:
                return float(abs(int(item.actual) - int(item.predicted)))
            if isinstance(item.probabilities, ExactMatchOnly):
                return float(item.actual != item.predicted)
            return 1 - item.probabilities.get(str(item.actual), 0.0)

        mismatches = sorted((item for item in observations[field] if item.actual != item.predicted),
                            key=lambda item: (-gap(item), str(item.issue.path)))[:20]
        lines += [f"### {field}", "", *_table(["Issue", "Current", "Jev", "Gap"],
                  [[item.issue.title, item.actual, item.predicted, f"{gap(item):.4f}"] for item in mismatches])]
    if repeat_run_id:
        second = _load_run(_run_path(repeat_run_id, state_dir))
        common = [issue for issue in scored if str(issue.path) in second.scored]
        matched = [issue for issue in common if all(run.scored[str(issue.path)][key] == second.scored[str(issue.path)][key]
                                                   for key in ("content_hash", "question_set_version", "model"))]
        rows: list[list[object]] = []
        for field in FIELDS:
            changed = sum(_object(run.scored[str(issue.path)]["decided_fields"][field])["value"]
                          != _object(second.scored[str(issue.path)]["decided_fields"][field])["value"] for issue in matched)
            rows.append([field, len(matched), changed, _metric(float(changed / len(matched)) if matched else UndefinedMetric("no comparable pairs"))])
        lines += ["## Repeat check", "", f"Second run: {repeat_run_id}. Comparable issues: {len(matched)}; changed content/model/version: {len(common) - len(matched)}; missing second successes: {len(scored) - len(common)}.", "",
                  "Use score --fresh for each repeat so cached responses do not masquerade as independent judgments.", "",
                  *_table(["Field", "N", "Changed", "Change rate"], rows)]
    lines += ["## Exclusions and incomplete coverage", "", "### Effort examples", "",
              *(f"- {_cell(issue.title)} ({_cell(issue.path)})" for issue in examples), "", "### Admitted style issues for review", "",
              *(f"- {_cell(issue.title)} ({_cell(issue.path)})" for issue in styles), "", "### Failed issues", "",
              *(f"- {_cell(issue.title)}: {_cell(run.failed[str(issue.path)])}" for issue in failed), "", "### Unscored issues", "",
              *(f"- {_cell(issue.title)} ({_cell(issue.path)})" for issue in unscored), "", "### Loader skips by reason", "",
              *_table(["Reason", "Count"], list(Counter(item.reason for item in skipped).items()))]
    reports = state_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{run_path.stem}-{split}.md"
    _ = report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
