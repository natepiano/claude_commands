#!/usr/bin/env python3
"""Load and validate global and repository style rule metadata."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

import style_rules
from candidate_generators import CandidatesSpec, CandidatesSpecError, read_candidates_spec

Tier = Literal["A", "B", "C", "D", "E"]
Family = Literal["names", "values", "types", "modules"]
InputScope = Literal["file", "crate", "manifest"]
RuleOrigin = Literal["global", "local"]


@dataclass(frozen=True)
class Rule:
    name: str
    path: Path
    tier: Tier | Literal["unclassified"]
    family: Family | None
    input_scope: InputScope
    mode_propose: bool
    candidates: CandidatesSpec | None
    pre_filter: str | None
    blob: str
    origin: RuleOrigin


class RuleValidationError(ValueError):
    """A named rule does not meet the metadata contract."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")


def _scalar(metadata: dict[str, list[str]], key: str, path: Path, default: str = "") -> str:
    values = metadata.get(key)
    if values is None:
        return default
    if len(values) != 1 or not values[0].strip():
        raise RuleValidationError(path, f"{key} must be a nonempty scalar")
    return values[0]


def _load_rule(path: Path, origin: RuleOrigin) -> Rule:
    metadata = style_rules.parse_frontmatter(path)
    try:
        candidates = read_candidates_spec(path)
    except CandidatesSpecError as error:
        raise RuleValidationError(path, str(error)) from error

    tier: Tier | Literal["unclassified"] = "unclassified"
    family: Family | None = None
    input_scope: InputScope = "crate"
    has_classification = any(key in metadata for key in ("tier", "family", "input_scope"))
    if origin == "local" or has_classification:
        declared_tier = _scalar(metadata, "tier", path)
        if declared_tier not in ("A", "B", "C", "D", "E"):
            raise RuleValidationError(path, "tier must be A, B, C, D or E")
        tier = declared_tier
        mechanism = _scalar(metadata, "mechanism", path)
        if (tier == "A") != (mechanism in ("clippy", "mend", "rustfmt")):
            raise RuleValidationError(path, "tier A requires a clippy, mend or rustfmt mechanism, and vice versa")

        declared_family = _scalar(metadata, "family", path)
        if (tier in ("B", "C", "D") or origin == "local") and not declared_family:
            raise RuleValidationError(path, "family is required for B, C, D and local rules")
        if tier in ("A", "E") and declared_family:
            raise RuleValidationError(path, "family is forbidden for tier A and E")
        if declared_family:
            if declared_family not in ("names", "values", "types", "modules"):
                raise RuleValidationError(path, "family must be names, values, types or modules")
            family = declared_family

        declared_scope = _scalar(metadata, "input_scope", path, "crate")
        if declared_scope not in ("file", "crate", "manifest"):
            raise RuleValidationError(path, "input_scope must be file, crate or manifest")
        input_scope = declared_scope

        if candidates is not None and candidates.surface == "unclassified":
            raise RuleValidationError(path, "classified rule's candidates.surface must be whole or partial")

    pre_filter = style_rules.read_pre_filter(path) or None
    if tier == "B" and candidates is None and pre_filter is None:
        raise RuleValidationError(path, "tier B requires candidates or pre_filter")

    blob = subprocess.run(
        ["git", "hash-object", "--", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Rule(
        name=path.stem,
        path=path,
        tier=tier,
        family=family,
        input_scope=input_scope,
        mode_propose=_scalar(metadata, "mode", path) == "propose",
        candidates=candidates,
        pre_filter=pre_filter,
        blob=blob,
        origin=origin,
    )


def load_rules(repo_root: Path | None) -> tuple[Rule, ...]:
    """Read every global rule, then root and member rules in a repository.

    Global rules predating classification metadata remain unclassified.
    Local rules and any declared classification metadata are validated.
    Use the loader's directory layout without its context-loading filters:
    this registry includes tool-enforced and Bevy rules for every repository.
    """
    global_dir = style_rules.NATE_STYLE_DIR / "rust"
    rules = [_load_rule(path.resolve(), "global") for path in sorted(global_dir.glob("*.md")) if path.is_file()]
    if repo_root is not None:
        docs = repo_root.expanduser().resolve() / "docs"
        directories = [docs / "style", *sorted(docs.glob("*/style"))]
        for directory in directories:
            rules.extend(
                _load_rule(path.resolve(), "local")
                for path in sorted(directory.glob("*.md")) if path.is_file()
            )
    return tuple(rules)


class RuleListing(TypedDict):
    name: str
    tier: Tier | Literal["unclassified"]
    family: Family | None
    input_scope: InputScope
    surface: Literal["whole", "partial", "unclassified"] | None


class ListArguments(argparse.Namespace):
    repo: str = ""
    json: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list", help="List rule tiers, families, scopes and generator surfaces.")
    _ = listing.add_argument("--repo", default="", help="Include this repository's local rules.")
    _ = listing.add_argument("--json", action="store_true", help="Emit a JSON array.")
    args = parser.parse_args(namespace=ListArguments())
    try:
        rules = load_rules(Path(args.repo) if args.repo else None)
    except (RuleValidationError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"rules: {error}\n")
    rows: list[RuleListing] = [
        {
            "name": rule.name,
            "tier": rule.tier,
            "family": rule.family,
            "input_scope": rule.input_scope,
            "surface": rule.candidates.surface if rule.candidates else None,
        }
        for rule in rules
    ]
    if args.json:
        print(json.dumps(rows, sort_keys=True))
    else:
        print("name\ttier\tfamily\tinput_scope\tsurface")
        for row in rows:
            print(f"{row['name']}\t{row['tier']}\t{row['family'] or '-'}\t{row['input_scope']}\t{row['surface'] or '-'}")


if __name__ == "__main__":
    main()
