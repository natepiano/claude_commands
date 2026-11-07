"""Read the classes of change that a style unit applies without the user, from `mechanical_classes.toml`."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypedDict, cast

import rules
from approved_renames import AppliedInCode, BindingRename, binding_rename_check
from style_footprint import FileChange, FileStatus, RevisionTree, lint_removal_files, moved_files

CLASSES = Path(__file__).with_name("mechanical_classes.toml")
NO_BINDING_RENAMES: Mapping[str, tuple[BindingRename, ...]] = MappingProxyType({})
ProofKind = Literal["item_move", "lint_removal", "binding_rename"]
Proof = Callable[[tuple[FileChange, ...], RevisionTree, Mapping[str, tuple[BindingRename, ...]]], frozenset[str]]


def binding_rename_files(changes: tuple[FileChange, ...], _before: RevisionTree,
                         renames: Mapping[str, tuple[BindingRename, ...]]) -> frozenset[str]:
    """Files whose entire token change is explained by at least one approved binding rename."""
    explained: set[str] = set()
    for change in changes:
        pairs = renames.get(change.path, ())
        if not pairs or change.status != FileStatus.MODIFIED or not change.path.endswith(".rs"):
            continue
        proof = binding_rename_check(change.path, change.before, change.after, pairs)
        if proof.explained and any(isinstance(check, AppliedInCode) for check in proof.checks.values()):
            explained.add(change.path)
    return frozenset(explained)


# Each proof returns only files whose edits it fully accounts for.
PROOFS: Mapping[ProofKind, Proof] = {
    "item_move": lambda changes, before, _renames: moved_files(changes, before),
    "lint_removal": lambda changes, before, _renames: lint_removal_files(changes, before),
    "binding_rename": binding_rename_files,
}


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
                    ticket_rules: frozenset[str],
                    renames: Mapping[str, tuple[BindingRename, ...]] = NO_BINDING_RENAMES) -> frozenset[str]:
    """The files of the change that the proof of a listed class explains, for a ticket whose findings name the rules
    ticket_rules (catalog rule names). Only a class with one of those rules counts. Each returned file's entire edit
    must pass its class's proof; binding proofs also require approved pairs for that path."""
    kinds: dict[ProofKind, None] = dict.fromkeys(entry.proof for entry in classes() if ticket_rules & set(entry.rules))
    return frozenset(path for kind in kinds for path in PROOFS[kind](changes, before, renames))
