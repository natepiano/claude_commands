"""Read the classes of change that a style unit applies without the user, from `mechanical_classes.toml`."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal, TypedDict, cast

import rules
from style_footprint import FileChange, RevisionTree, lint_removal_files, moved_files

CLASSES = Path(__file__).with_name("mechanical_classes.toml")
ProofKind = Literal["item_move", "lint_removal"]
Proof = Callable[[tuple[FileChange, ...], RevisionTree], frozenset[str]]
# Each proof gives the files of a change that it explains, and no file unless `judge` finds the whole change mechanical.
PROOFS: Mapping[ProofKind, Proof] = {"item_move": moved_files, "lint_removal": lint_removal_files}


class ClassRow(TypedDict):
    name: str
    proof: str
    rules: list[str]
    summary: str


@dataclass(frozen=True)
class MechanicalClass:
    name: str
    proof: ProofKind
    rules: tuple[str, ...]
    summary: str


class ClassListError(ValueError):
    """The class list cannot be read, or it has an entry that the loader refuses."""


def _row(path: Path, index: int, value: object) -> ClassRow:
    """The entry when it has exactly the four fields, each of the right type and not empty."""
    if not isinstance(value, dict):
        raise ClassListError(f"{path}: class {index} is not a table")
    entry = cast(dict[str, object], value)
    if set(entry) != set(ClassRow.__annotations__):
        raise ClassListError(f"{path}: class {index} must have exactly the fields name, proof, rules and summary")
    name, proof, names, summary = entry["name"], entry["proof"], entry["rules"], entry["summary"]
    if not (isinstance(name, str) and name and isinstance(proof, str) and isinstance(summary, str) and summary):
        raise ClassListError(f"{path}: class {index} needs a name, a proof and a summary as text")
    if not isinstance(names, list) or not names:
        raise ClassListError(f"{path}: class {name} needs a list of rules")
    rule_names = cast(list[object], names)
    if not all(isinstance(rule, str) for rule in rule_names):
        raise ClassListError(f"{path}: class {name} has a rule that is not text")
    return ClassRow(name=name, proof=proof, rules=cast(list[str], rule_names), summary=summary)


def _proof_kind(proof: str) -> ProofKind | None:
    kinds: tuple[ProofKind, ...] = tuple(PROOFS)
    return next((kind for kind in kinds if kind == proof), None)


def load(path: Path, catalog: frozenset[str]) -> tuple[MechanicalClass, ...]:
    """The classes of the list at path. Refuse an entry that is malformed, names a proof that is not in `PROOFS` or
    names a rule that is not in the catalog, and a list with no entry or with a name twice."""
    try:
        data: dict[str, object] = tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ClassListError(f"{path}: {error}") from error
    values = data.get("class")
    if set(data) != {"class"} or not isinstance(values, list) or not values:
        raise ClassListError(f"{path}: the list must hold only [[class]] entries, at least one")
    result: list[MechanicalClass] = []
    for index, value in enumerate(cast(list[object], values), 1):
        row = _row(path, index, value)
        proof = _proof_kind(row["proof"])
        if proof is None:
            raise ClassListError(f"{path}: class {row['name']} names the unknown proof {row['proof']}")
        unknown = [rule for rule in row["rules"] if rule not in catalog]
        if unknown:
            raise ClassListError(f"{path}: class {row['name']} names a rule not in the catalog: {', '.join(unknown)}")
        if any(entry.name == row["name"] for entry in result):
            raise ClassListError(f"{path}: class {row['name']} is listed twice")
        result.append(MechanicalClass(row["name"], proof, tuple(row["rules"]), row["summary"]))
    return tuple(result)


@cache
def classes() -> tuple[MechanicalClass, ...]:
    """The classes of `mechanical_classes.toml`, read once and checked against the global rule catalog."""
    return load(CLASSES, frozenset(path.stem for path in rules.global_rule_paths()))


def explained_files(changes: tuple[FileChange, ...], before: RevisionTree,
                    ticket_rules: frozenset[str]) -> frozenset[str]:
    """The files of the change that the proof of a listed class explains, for a ticket whose findings name the rules
    ticket_rules (catalog rule names). Only a class with one of those rules counts. A proof gives files only when it
    passes on the whole change, so a change that adds an allow or changes code beyond the proof gives none."""
    kinds: dict[ProofKind, None] = dict.fromkeys(entry.proof for entry in classes() if ticket_rules & set(entry.rules))
    return frozenset(path for kind in kinds for path in PROOFS[kind](changes, before))
