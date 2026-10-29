"""Wording variants asked together over exactly the same issue state."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import cast

from . import client, questions

VariantSets = dict[str, dict[str, questions.Question]]


def load_variants(path: Path) -> VariantSets:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Variant file must map variant-set names to question replacements")
    result: VariantSets = {}
    known = questions.main_questions() | questions.area_questions([])
    for variant, replacements in cast(dict[object, object], raw).items():
        if (not isinstance(variant, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", variant)
                or "__" in variant or variant == "production"):
            raise ValueError("Variant names must be identifiers other than production")
        if not isinstance(replacements, dict) or not replacements:
            raise ValueError(f"Variant {variant} must replace at least one question")
        selected: dict[str, questions.Question] = {}
        for name, value in cast(dict[object, object], replacements).items():
            if not isinstance(name, str) or name not in known or not isinstance(value, dict):
                raise ValueError(f"Unknown production question in variant {variant}: {name}")
            question = cast(dict[str, object], value)
            criteria = question.get("criteria")
            if question.get("type") != known[name]["type"] or not isinstance(question.get("instructions"), str):
                raise ValueError(f"Invalid question type or instructions for {name}__{variant}")
            if question["type"] == "score":
                valid = isinstance(criteria, list) and all(isinstance(item, str) for item in cast(list[object], criteria))
            else:
                valid = (isinstance(criteria, dict) and len(cast(dict[object, object], criteria)) > 0
                         and all(isinstance(key, str) and isinstance(text, str)
                                 for key, text in cast(dict[object, object], criteria).items()))
            if not valid:
                raise ValueError(f"Invalid criteria for {name}__{variant}")
            selected[name] = cast(questions.Question, cast(object, question))
        result[variant] = selected
    return result


def build_questions(production: Mapping[str, questions.Question], variants: VariantSets) -> dict[str, questions.Question]:
    """Keep the production questions and append namespaced replacements."""
    result = deepcopy(dict(production))
    for variant, replacements in variants.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", variant) or "__" in variant or variant == "production":
            raise ValueError(f"Invalid variant name: {variant}")
        if not replacements:
            raise ValueError(f"Variant {variant} must replace at least one question")
        for name, question in replacements.items():
            if name not in production:
                continue  # Main and area variants share a file, but have separate states.
            original = production[name]
            if question["type"] != original["type"]:
                raise ValueError(f"Variant changes the answer type for {name}")
            if question["type"] == "score" and original["type"] == "score":
                compatible = len(question["criteria"]) == len(original["criteria"])
            elif question["type"] != "score" and original["type"] != "score":
                compatible = question["criteria"].keys() == original["criteria"].keys()
            else:
                compatible = False
            if not compatible:
                raise ValueError(f"Variant changes the decision options for {name}")
            result[f"{name}__{variant}"] = deepcopy(question)
    return result


def project_response(response: client.Response, variant: str, variants: VariantSets) -> client.Response:
    """Restore production names, falling back only for unchanged questions."""
    if variant != "production" and variant not in variants:
        raise ValueError(f"Unknown variant set: {variant}")
    replacements = variants.get(variant, {})
    answers: dict[str, client.Answer] = {}
    for name, answer in response["answers"].items():
        if "__" in name:
            continue
        if name in replacements:
            replacement = f"{name}__{variant}"
            if replacement not in response["answers"]:
                raise client.TypeSafeError(f"Missing variant answer: {replacement}")
            answer = response["answers"][replacement]
        answers[name] = deepcopy(answer)
    if not answers:
        raise client.TypeSafeError("Variant response has no production answers")
    return {"model": response["model"], "usage": deepcopy(response["usage"]), "answers": answers}
