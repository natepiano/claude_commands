"""Deterministic decisions from Jev's independent probability distributions."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from .client import Answer, ChoiceAnswer, ScoreAnswer, TypeSafeError
from .questions import ERROR_TARGETS, EVIDENCE_ABSENT, EVIDENCE_PRESENT, GOAL_LABELS, SPLIT_PROBABILITY, THRESHOLDS, URGENCY_EVIDENCE
from .questions import MinimumGap as MinimumGap, ReportOnly as ReportOnly, Supported as Supported, ThresholdOutcome as ThresholdOutcome, Unsupported as Unsupported


@dataclass(frozen=True)
class FieldDecision:
    value: str | int
    confidence: float
    certainty: Literal["sure", "unsure"]
    split: bool = False
    check: bool = False
    possible_five: bool = False
    probabilities: dict[str, float] = field(default_factory=dict)
    hint: str = ""
    structurally_eligible: bool = True
    threshold_dependencies: dict[str, float] = field(default_factory=dict)
    origin: Literal["score", "choice", "pressure", "urgency_evidence", "area_fallback"] = "score"


@dataclass(frozen=True)
class RiskPoint:
    threshold: float
    coverage: float
    error_rate: float


def select_threshold(field_name: str, points: Sequence[RiskPoint], *, complete: bool = True) -> ThresholdOutcome:
    if field_name not in THRESHOLDS:
        raise ValueError(f"Unknown threshold field: {field_name}")
    if isinstance(THRESHOLDS[field_name], ReportOnly):
        return ReportOnly()
    if not complete:
        return Unsupported("incomplete or non-tuning cohort; excluded from threshold selection")
    for point in sorted(points, key=lambda item: item.threshold):
        if point.coverage > 0 and point.error_rate <= ERROR_TARGETS[field_name] + 1e-12:
            return MinimumGap(point.threshold) if isinstance(THRESHOLDS[field_name], MinimumGap) else Supported(point.threshold)
    return Unsupported()


def is_sure(field_name: str, decision: FieldDecision, thresholds: Mapping[str, ThresholdOutcome] = THRESHOLDS) -> bool:
    """Apply the configured probability or gap gate after structural guards."""
    policy = thresholds[field_name]
    if not decision.structurally_eligible or decision.split:
        return False
    if isinstance(policy, Supported):
        if decision.confidence < policy.threshold:
            return False
    elif isinstance(policy, MinimumGap):
        if decision.origin == "area_fallback":
            gap = decision.confidence - (1 - decision.confidence)
        else:
            probabilities = sorted(decision.probabilities.values(), reverse=True)
            if not probabilities:
                return False
            gap = probabilities[0] - (probabilities[1] if len(probabilities) > 1 else 0.0)
        if gap + 1e-12 < policy.gap:
            return False
    else:
        return False
    for name, confidence in decision.threshold_dependencies.items():
        dependency = thresholds[name]
        # Dependencies carry confidence only, so a gap dependency cannot be certified.
        if not isinstance(dependency, Supported) or confidence < dependency.threshold:
            return False
    return True


def _certify(field_name: str, decision: FieldDecision) -> FieldDecision:
    return replace(decision, certainty="sure" if is_sure(field_name, decision) else "unsure")


def median_level(probabilities: Mapping[str, float]) -> int:
    """Return the lower median in API level coordinates (zero-based)."""
    if not probabilities or sum(probabilities.values()) <= 0:
        raise TypeSafeError("Cannot decide an empty score distribution")
    cumulative = 0.0
    for level, probability in sorted((int(key), value) for key, value in probabilities.items()):
        cumulative += probability
        if cumulative >= sum(probabilities.values()) / 2:
            return level
    raise TypeSafeError("Cannot find score median")


def is_split(probabilities: Mapping[str, float]) -> bool:
    levels = sorted(int(level) for level, probability in probabilities.items() if probability >= SPLIT_PROBABILITY)
    return bool(levels and levels[-1] - levels[0] > 1)


def average_choice(first: ChoiceAnswer, second: ChoiceAnswer) -> dict[str, float]:
    if first["probabilities"].keys() != second["probabilities"].keys():
        raise TypeSafeError("Choice orders have different options")
    return {key: (value + second["probabilities"][key]) / 2 for key, value in first["probabilities"].items()}


def _top(probabilities: Mapping[str, float]) -> str:
    if not probabilities:
        raise TypeSafeError("Cannot decide an empty choice distribution")
    return min(probabilities, key=lambda name: (-probabilities[name], name))


def decide_choice(first: ChoiceAnswer, second: ChoiceAnswer, field_name: str) -> FieldDecision:
    probabilities = average_choice(first, second)
    winner = _top(probabilities)
    confidence = probabilities[winner]
    agree = _top(first["probabilities"]) == _top(second["probabilities"])
    return _certify(field_name, FieldDecision(winner, confidence, "unsure",
                    probabilities=probabilities, structurally_eligible=agree, origin="choice"))


def decide_score(answer: ScoreAnswer, field_name: str) -> FieldDecision:
    level = median_level(answer["probabilities"])
    split = is_split(answer["probabilities"])
    confidence = answer["probabilities"][str(level)]
    return _certify(field_name, FieldDecision(level + 1, confidence, "unsure",
                         split=split, check=field_name == "backlog_effort",
                         probabilities={str(int(key) + 1): value for key, value in answer["probabilities"].items()}))


def decide_urgency(evidence: Mapping[str, float], pressure: ScoreAnswer) -> FieldDecision:
    if set(evidence) != set(URGENCY_EVIDENCE):
        raise TypeSafeError("Urgency evidence questions are missing or unexpected")
    ambiguous = any(EVIDENCE_ABSENT <= value <= EVIDENCE_PRESENT for value in evidence.values())
    present = any(value > EVIDENCE_PRESENT for value in evidence.values())
    pressure_decision = decide_score(pressure, "backlog_urgency")
    if present:
        confidence = max(evidence.values())
        return _certify("backlog_urgency", FieldDecision(4, confidence, "unsure",
                             split=pressure_decision.split, possible_five=True,
                             probabilities={"4": confidence}, structurally_eligible=not ambiguous, origin="urgency_evidence"))
    confidence = min(pressure_decision.confidence, *(1 - value for value in evidence.values()))
    return _certify("backlog_urgency", replace(pressure_decision, confidence=confidence,
                    structurally_eligible=not ambiguous, origin="pressure"))


def _choice(answers: Mapping[str, Answer], name: str) -> ChoiceAnswer:
    answer = answers[name]
    if answer["type"] != "choice":
        raise TypeSafeError(f"Expected Choice answer: {name}")
    return answer


def _score(answers: Mapping[str, Answer], name: str) -> ScoreAnswer:
    answer = answers[name]
    if answer["type"] != "score":
        raise TypeSafeError(f"Expected Score answer: {name}")
    return answer


def _noul(answers: Mapping[str, Answer], name: str) -> float:
    answer = answers[name]
    if answer["type"] != "noul":
        raise TypeSafeError(f"Expected Noul answer: {name}")
    return answer["noul"]


def decide_answers(main: Mapping[str, Answer], area: Mapping[str, Answer]) -> dict[str, FieldDecision]:
    goal = decide_choice(_choice(main, "goal"), _choice(main, "goal_reverse"), "backlog_goal")
    goal_name = str(goal.value)
    if goal_name not in GOAL_LABELS:
        raise TypeSafeError("Unknown goal in response")
    alignment = decide_score(_score(main, f"alignment_{goal_name}"), "backlog_alignment")
    alignment = _certify("backlog_alignment", replace(alignment,
        structurally_eligible=goal.structurally_eligible,
        threshold_dependencies={"backlog_goal": goal.confidence}))
    area_decision = decide_choice(_choice(area, "area"), _choice(area, "area_reverse"), "area")
    fits = _noul(area, "area_fits")
    if fits < EVIDENCE_ABSENT:
        area_decision = _certify("area", replace(area_decision, value="unfiled", hint=str(area_decision.value),
                                confidence=1 - fits, structurally_eligible=True, origin="area_fallback"))
    elif fits <= EVIDENCE_PRESENT:
        area_decision = replace(area_decision, certainty="unsure", structurally_eligible=False)
    return {
        "backlog_goal": replace(goal, value=GOAL_LABELS[goal_name]),
        "backlog_alignment": alignment,
        "backlog_impact": decide_score(_score(main, "impact"), "backlog_impact"),
        "backlog_urgency": decide_urgency({name: _noul(main, name) for name in URGENCY_EVIDENCE}, _score(main, "pressure")),
        "backlog_effort": decide_score(_score(main, "effort"), "backlog_effort"),
        "category": decide_choice(_choice(main, "category"), _choice(main, "category_reverse"), "category"),
        "area": area_decision,
    }
