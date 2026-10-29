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
    decision: decide.FieldDecision
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


def load_run(path: Path) -> RunJudgments:
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
    current_decision = decide.decide_answers(main, area)[field]
    actual: FieldValue = issue.labels.get(field, "")
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
        if field == "backlog_urgency":
            if current_decision.origin == "urgency_evidence":
                probabilities = ExactMatchOnly("urgency from evidence")
                return FieldObservation(issue, actual, predicted, current_decision, probabilities)
            name = "pressure"
        answer = _score(main, name)
        probabilities = {str(int(level) + 1): p for level, p in answer["probabilities"].items()}
    else:
        answers, name = (area, "area") if field == "area" else (main, "goal" if field == "backlog_goal" else field)
        if field == "area":
            actual = vault.normalize_area(str(actual))
            if current_decision.origin == "area_fallback":
                return FieldObservation(issue, actual, predicted, current_decision, ExactMatchOnly("unfiled area"))
        probabilities = decide.average_choice(_choice(answers, name), _choice(answers, name + "_reverse"))
        if field == "backlog_goal":
            probabilities = {questions.GOAL_LABELS[name]: p for name, p in probabilities.items()}
    return FieldObservation(issue, actual, predicted, current_decision, probabilities)


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


def _covered(field: str, observations: list[FieldObservation], threshold: float) -> list[FieldObservation]:
    thresholds = dict(questions.THRESHOLDS)
    # Sweep this field even when production currently leaves it agent-only.
    thresholds[field] = decide.Supported(threshold)
    return [item for item in observations if decide.is_sure(field, item.decision, thresholds)]


def _threshold_outcome(field: str, observations: list[FieldObservation], *, complete: bool) -> decide.ThresholdOutcome:
    points: list[decide.RiskPoint] = []
    for threshold in questions.RISK_THRESHOLDS:
        covered = _covered(field, observations, threshold)
        if not covered:
            continue
        if field in ("backlog_impact", "backlog_alignment"):
            error = sum(abs(int(item.actual) - int(item.predicted)) > 1 for item in covered) / len(covered)
        else:
            error = sum(item.actual != item.predicted for item in covered) / len(covered)
        points.append(decide.RiskPoint(threshold, len(covered) / len(observations), error))
    return decide.select_threshold(field, points, complete=complete)


def _outcome_text(outcome: decide.ThresholdOutcome) -> str:
    if isinstance(outcome, decide.Supported):
        return f"supported({outcome.threshold:.2f}); Jev proposes, pending held-out validation and user verdict"
    if isinstance(outcome, decide.Unsupported):
        return f"unsupported ({outcome.reason}); agent-only"
    return "report-only; agent proposes pending user verdict"


def _heldout_validation(field: str, observations: list[FieldObservation], *, complete: bool) -> str:
    policy = questions.THRESHOLDS[field]
    if not isinstance(policy, decide.Supported):
        return _outcome_text(policy)
    threshold = policy.threshold
    covered = _covered(field, observations, threshold)
    if field in ("backlog_impact", "backlog_alignment"):
        errors = [float(abs(int(item.actual) - int(item.predicted)) > 1) for item in covered]
        measure = "beyond-one error"
    else:
        errors = [float(item.actual != item.predicted) for item in covered]
        measure = "exact error"
    error = _mean(errors)
    target = questions.ERROR_TARGETS[field]
    passed = bool(covered) and isinstance(error, float) and error <= target + 1e-12
    status = "partial" if not complete else "pass" if passed else "fail"
    coverage = float(len(covered) / len(observations)) if observations else 0.0
    recommendation = "Jev proposes, pending user verdict" if status == "pass" else "agent-only"
    return (f"{status} at configured threshold {threshold:.2f}; Sure N: {len(covered)}; "
            f"coverage: {coverage:.4f}; {measure}: {_metric(error)}; target: ≤ {target:.4f}; {recommendation}")


def _judgment_text(field: str, observations: list[FieldObservation], *, split: Split, complete: bool) -> str:
    if split == "holdout":
        return _heldout_validation(field, observations, complete=complete)
    return _outcome_text(_threshold_outcome(field, observations, complete=complete and split == "tune"))


def _routing_summary(field: str, observations: list[FieldObservation]) -> str:
    policy = questions.THRESHOLDS[field]
    if isinstance(policy, decide.Supported):
        configured = f"at configured threshold {policy.threshold:.2f}"
    else:
        configured = f"under configured {'unsupported' if isinstance(policy, decide.Unsupported) else 'report-only'} policy"
    taken = _mean([float(decide.is_sure(field, item.decision)) for item in observations])
    routed = 1.0 - taken if isinstance(taken, float) else taken
    return (f"Routing {configured}: taken (sure): {_metric(taken)}; "
            f"routed to the user (unsure): {_metric(routed)}.")


def _risk_report(field: str, observations: list[FieldObservation], *, complete: bool, split: Split) -> list[str]:
    rows: list[list[object]] = []
    for threshold in questions.RISK_THRESHOLDS:
        covered = _covered(field, observations, threshold)
        taken = float(len(covered) / len(observations)) if observations else UndefinedMetric("no observations")
        routed = 1.0 - taken if isinstance(taken, float) else taken
        row: list[object] = [f"{threshold:.2f}", len(covered), _metric(taken), _metric(routed),
                             _metric(_mean([float(item.actual != item.predicted) for item in covered]))]
        if field in ORDINAL_FIELDS:
            ordinal = [item for item in covered if isinstance(item.probabilities, dict)]
            row.append(_metric(_mean([float(abs(int(item.actual) - int(item.predicted)) > 1) for item in ordinal])))
        rows.append(row)
    headers = ["Threshold", "Sure N", "Coverage", "Routed to user", "Exact error"]
    if field in ORDINAL_FIELDS:
        headers.append("Beyond-one error")
    recommendation = _judgment_text(field, observations, split=split, complete=complete)
    label = "Held-out validation" if split == "holdout" else "Threshold outcome"
    return [f"### {field}", "", f"{label}: {recommendation}.", "",
            _routing_summary(field, observations), "",
            "Coverage is the share taken (sure); Routed to user is the share unsure.", "", *_table(headers, rows)]


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


def _same_questions(first: ScoredIssue, second: ScoredIssue) -> bool:
    first_variant = first.get("variant_set", "production")
    second_variant = second.get("variant_set", "production")
    if first_variant != second_variant:
        return False
    # Choice criteria order is part of the request, including in nested objects.
    return json.dumps(first.get("variant_definitions", {})) == json.dumps(second.get("variant_definitions", {}))


def _high_priority(issues: Sequence[vault.Issue]) -> list[vault.Issue]:
    result: list[vault.Issue] = []
    for issue in issues:
        try:
            if float(issue.labels.get("backlog_score", "")) >= 20:
                result.append(issue)
        except ValueError:
            continue
    return result


def _recommendations(observations: Mapping[str, list[FieldObservation]], high: Sequence[vault.Issue], *, complete: bool, split: Split) -> list[str]:
    paths = {issue.path for issue in high}
    rows: list[list[object]] = []
    for field, items in observations.items():
        band = [item for item in items if item.issue.path in paths]
        exact = _mean([float(item.actual == item.predicted) for item in band])
        ordinal = [item for item in band if isinstance(item.probabilities, dict)] if field in ORDINAL_FIELDS else []
        within = _mean([float(abs(int(item.actual) - int(item.predicted)) <= 1) for item in ordinal])
        rows.append([field, len(band), _metric(exact), _metric(within) if field in ORDINAL_FIELDS else "N/A",
                     _judgment_text(field, items, split=split, complete=complete)])
    urgency = observations["backlog_urgency"]
    rows_45 = [item for item in urgency if item.actual in (4, 5)]
    return ["## Per-field recommendations and score ≥ 20 results", "",
            "These are measured candidates for review. A global pass supplies no evidence for the high-priority group; its results below must be assessed separately.", "",
            *_table(["Field", "Score ≥ 20 N", "Exact", "Within one", "Recommendation"], rows),
            "Category review must inspect feature/enhancement/research confusion, shown in each group's confusion matrix.", "",
            f"Urgency 4–5 current-label rows: N={len(rows_45)}, exact={_metric(_mean([float(item.actual == item.predicted) for item in rows_45]))}. Judge urgency by kappa and these rows, not majority-class accuracy.", ""]


def compare(run_id: str, *, split: Split = "all", repeat_run_id: str = "",
            issues_dir: Path = vault.ISSUES_DIR, state_dir: Path = STATE_DIR,
            goals_path: Path = vault.VAULT_ROOT / "prioritization goals.md", baseline_run_id: str = "") -> Path:
    """Write a report from saved answers; this function never contacts the API."""
    state_dir = state_dir.expanduser().resolve()
    if state_dir.is_relative_to(vault.VAULT_ROOT.resolve()) or state_dir.is_relative_to(issues_dir.resolve()):
        raise ValueError("Reports must be outside the issue vault")
    run_path = _run_path(run_id, state_dir)
    run = load_run(run_path)
    rated = vault.load_rated_issues(issues_dir)
    in_scope = [issue for issue in rated.issues if in_split(issue.path, split)]
    styles = [issue for issue in in_scope if issue.is_style]
    examples = [issue for issue in in_scope if not issue.is_style and issue.path.stem in questions.HISTORICAL_EFFORT_EXAMPLE_STEMS]
    eligible = [issue for issue in in_scope if not issue.is_style and issue.path.stem not in questions.HISTORICAL_EFFORT_EXAMPLE_STEMS]
    scored = [issue for issue in eligible if str(issue.path) in run.scored]
    failed = [issue for issue in eligible if str(issue.path) in run.failed]
    unscored = [issue for issue in eligible if str(issue.path) not in run.scored and str(issue.path) not in run.failed]
    partial = bool(failed or unscored)
    candidate_count = len(scored)
    baseline = RunJudgments({}, {})
    if baseline_run_id:
        baseline = load_run(_run_path(baseline_run_id, state_dir))
        partial = partial or any(str(issue.path) not in baseline.scored for issue in eligible)
        scored = [issue for issue in scored if str(issue.path) in baseline.scored]
    skipped = [item for item in rated.skipped if in_split(item.path, split)]
    observations = {field: [_observation(issue, run.scored[str(issue.path)], field) for issue in scored
                             if issue.labels.get(field, "")] for field in FIELDS}
    lines = [f"# Issue score comparison{' — partial' if partial else ''}: {run_path.stem} ({split})", "",
             f"Scored: {candidate_count}; failed: {len(failed)}; unscored: {len(unscored)}; eligible rated issues: {len(eligible)}.", "",
             f"Excluded effort examples: {len(examples)}; admitted style issues excluded: {len(styles)}; loader skips: {len(skipped)}.", "",
             "Labels and backlog_score are read from the current vault, never from the run. All metrics exclude effort examples and style issues.", "",
             "RPS is normalized by K−1 on the five-star rubric. Pressure mass is zero at stars 4 and 5. Evidence-based urgency 4 and derived unfiled fallback areas receive exact-match metrics only. Brier is the sum over classes.", ""]
    models = sorted({run.scored[str(issue.path)]["model"] for issue in scored})
    versions = sorted({run.scored[str(issue.path)]["question_set_version"] for issue in scored})
    lines += [f"Models: {', '.join(models) or 'none'}; question versions: {', '.join(versions) or 'none'}.", ""]
    variants = sorted({run.scored[str(issue.path)].get("variant_set", "production") for issue in scored})
    lines += [f"Variant sets: {', '.join(variants) or 'none'}.", ""]
    goals = _goals(goals_path)
    if baseline_run_id:
        baseline_failed = [issue for issue in eligible if str(issue.path) in baseline.failed]
        baseline_unscored = [issue for issue in eligible if str(issue.path) not in baseline.failed and str(issue.path) not in baseline.scored]
        changed_content = sum(run.scored[str(issue.path)]["content_hash"] != baseline.scored[str(issue.path)]["content_hash"] for issue in scored)
        lines += [f"Shared cohort with baseline {baseline_run_id}: {len(scored)} issues. Both runs use these same current labels, read once.", "",
                  f"Shared issues whose content/state hash changed between runs: {changed_content}. Interpret wording effects separately from state changes.", "",
                  f"Baseline scored: {sum(str(issue.path) in baseline.scored for issue in eligible)}; failed: {len(baseline_failed)}; unscored: {len(baseline_unscored)}.", "",
                  *(f"- Baseline missing: {_cell(issue.path)}" for issue in [*baseline_failed, *baseline_unscored]), ""]
    groups = [("all", scored), ("title-only", [issue for issue in scored if len(vault.clean_body(issue.body)) < 200])]
    high = _high_priority(scored)
    groups.append(("score ≥ 20", high))
    for name, issues in groups:
        lines += [f"## Group: {name} (N={len(issues)})", "", *_ordering(issues, run, goals)]
        paths = {issue.path for issue in issues}
        for field in FIELDS:
            lines += _field_report(field, [item for item in observations[field] if item.issue.path in paths])
    lines += ["## Risk-coverage", "", "The production sure rule sweeps only this field's threshold; every other field keeps its configured threshold. Structural uncertainty remains unsure. Threshold selection requires a complete tuning run.", ""]
    for field in FIELDS:
        lines += _risk_report(field, observations[field], complete=not partial, split=split)
    lines += _recommendations(observations, high, complete=not partial, split=split)
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
    if baseline_run_id:
        lines += [f"## Baseline: {baseline_run_id}", "", "All baseline metrics use the identical shared cohort above.", ""]
        baseline_observations = {field: [_observation(issue, baseline.scored[str(issue.path)], field) for issue in scored
                                        if issue.labels.get(field, "")] for field in FIELDS}
        for name, issues in groups:
            lines += [f"## Baseline group: {name} (N={len(issues)})", "", *_ordering(issues, baseline, goals)]
            paths = {issue.path for issue in issues}
            for field in FIELDS:
                lines += _field_report(field, [item for item in baseline_observations[field] if item.issue.path in paths])
        lines += ["## Baseline risk-coverage", ""]
        for field in FIELDS:
            lines += _risk_report(field, baseline_observations[field], complete=not partial, split=split)
        lines += _recommendations(baseline_observations, high, complete=not partial, split=split)
    if repeat_run_id:
        second = load_run(_run_path(repeat_run_id, state_dir))
        common = [issue for issue in scored if str(issue.path) in second.scored]
        unchanged = [issue for issue in common if all(run.scored[str(issue.path)][key] == second.scored[str(issue.path)][key]
                                                     for key in ("content_hash", "question_set_version", "model"))]
        matched = [issue for issue in unchanged
                   if _same_questions(run.scored[str(issue.path)], second.scored[str(issue.path)])]
        rows: list[list[object]] = []
        for field in FIELDS:
            changed = sum(_object(run.scored[str(issue.path)]["decided_fields"][field])["value"]
                          != _object(second.scored[str(issue.path)]["decided_fields"][field])["value"] for issue in matched)
            rows.append([field, len(matched), changed, _metric(float(changed / len(matched)) if matched else UndefinedMetric("no comparable pairs"))])
        lines += ["## Repeat check", "", f"Second run: {repeat_run_id}. Comparable issues: {len(matched)}; changed content/model/version: {len(common) - len(unchanged)}; missing second successes: {len(scored) - len(common)}; changed question definitions: {len(unchanged) - len(matched)}; not comparable: {len(common) - len(matched)}.", "",
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
