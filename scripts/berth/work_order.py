#!/usr/bin/env python3
"""Parse, validate, resolve, and compare cargo-berth Work Orders.

The module is intentionally independent of the cargo-berth ledger.  Work Order
authors and readers share this one lexical contract instead of teaching plan
commands subtly different Markdown grammars.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable


CONTRACT = "cargo-berth-work-order/v1"
FIELD_HEADING = re.compile(
    r"^\*\*(?P<label>[^*\n]+):\*\*(?P<inline>.*)$", re.MULTILINE
)
WORK_ORDER_HEADING = re.compile(r"^#### Work Order\s*$", re.MULTILINE)
PHASE_HEADING = re.compile(r"^### Phase\s+(?P<identifier>\d+)\b(?P<title>.*)$", re.MULTILINE)
RESERVATION_LINE = re.compile(r"^- (file|tree): `([^`]+)`$")
CODE_SPAN = re.compile(r"`([^`]+)`")
LINE_REFERENCE = re.compile(r"(?::\d+(?:-\d+)?|#L\d+(?:-L\d+)?)$")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


class ReservationCoverageMode(Enum):
    """Whether a caller permits or rejects a missing declaration."""

    ADVISORY = "advisory"
    REQUIRED = "required"

    def tagged(self) -> dict[str, str]:
        return {"kind": self.value}


class ScopeKind(Enum):
    """The path relationship promised by one reservation scope."""

    FILE = "file"
    TREE = "tree"


@dataclass(frozen=True)
class ReservationScope:
    """One validated lexical reservation scope."""

    kind: ScopeKind
    path: str

    def tagged(self) -> dict[str, str]:
        return {"kind": self.kind.value, "path": self.path}

    def argument(self) -> str:
        return f"{self.kind.value}:{self.path}"


@dataclass(frozen=True)
class DeclaredReservationDeclaration:
    """A present, non-empty, validated Reservations block."""

    scopes: tuple[ReservationScope, ...]

    def tagged(self) -> dict[str, object]:
        return {
            "kind": "declared",
            "scopes": [scope.tagged() for scope in self.scopes],
        }


@dataclass(frozen=True)
class MissingReservationDeclaration:
    """A Work Order with no Reservations heading."""

    def tagged(self) -> dict[str, str]:
        return {"kind": "missing"}


ReservationDeclaration = DeclaredReservationDeclaration | MissingReservationDeclaration


@dataclass(frozen=True)
class WorkOrderFile:
    """One expanded path named by Files and its reservation obligation."""

    path: str
    coverage: str

    @property
    def requires_reservation(self) -> bool:
        return self.coverage == "implementation"

    def tagged(self) -> dict[str, str]:
        return {"path": self.path, "coverage": self.coverage}


@dataclass(frozen=True)
class ValidatedWorkOrder:
    """A complete Work Order safe for plan writers and delegation readers."""

    phase: str
    phase_heading: str
    goal: str
    specification: str
    files: tuple[WorkOrderFile, ...]
    declaration: ReservationDeclaration

    def tagged(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "phase_heading": self.phase_heading,
            "goal": self.goal,
            "specification": self.specification,
            "files": [entry.tagged() for entry in self.files],
            "reservation_declaration": self.declaration.tagged(),
        }


@dataclass(frozen=True)
class WorkOrderSource:
    """The bounded Markdown and phase identity for one Work Order heading."""

    phase: str
    phase_heading: str
    markdown: str


@dataclass(frozen=True)
class EveryWorkOrderSelection:
    """Validation covers every Work Order in the document."""


@dataclass(frozen=True)
class ExactPhaseSelection:
    """Validation covers the sole Work Order matching one phase selector."""

    phase: str


WorkOrderSelection = EveryWorkOrderSelection | ExactPhaseSelection


class WorkOrderValidationError(Exception):
    """One or more deterministic Work Order contract violations."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _invalid(operation: str, errors: Iterable[str]) -> int:
    _emit(
        {
            "contract": CONTRACT,
            "operation": operation,
            "outcome": {"kind": "invalid", "errors": list(errors)},
        }
    )
    return 2


def _lexical_components(path: str, context: str) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(path, str) or not path:
        errors.append(f"{context}: path must be a non-empty string")
    elif "\\" in path:
        errors.append(f"{context}: path must use '/' separators: {path!r}")
    elif path.startswith("/") or WINDOWS_DRIVE.match(path):
        errors.append(f"{context}: path must be repository-relative: {path!r}")
    elif "{" in path or "}" in path:
        errors.append(f"{context}: brace expressions must be expanded: {path!r}")
    elif "\x00" in path or "\n" in path or "\r" in path:
        errors.append(f"{context}: path contains a forbidden control character")

    components = tuple(path.split("/")) if not errors else ()
    if components:
        if any(component == "" for component in components):
            errors.append(f"{context}: path has an empty component: {path!r}")
        if any(component in {".", ".."} for component in components):
            errors.append(f"{context}: path has a '.' or '..' component: {path!r}")
        if any(component.casefold() == ".git" for component in components):
            errors.append(f"{context}: path may not enter .git: {path!r}")
    if errors:
        raise WorkOrderValidationError(errors)
    return components


def validate_lexical_path(path: str, context: str) -> str:
    """Return an unchanged valid repository-relative path."""

    _lexical_components(path, context)
    return path


def _folded_components(path: str, ignore_case: bool) -> tuple[str, ...]:
    components = _lexical_components(path, "scope comparison")
    if ignore_case:
        # Match Rust `str::to_lowercase`, which is the engine's component rule.
        return tuple(component.lower() for component in components)
    return components


def _is_component_ancestor(parent: tuple[str, ...], child: tuple[str, ...]) -> bool:
    return len(parent) <= len(child) and child[: len(parent)] == parent


def scope_contains(
    container: ReservationScope, contained: ReservationScope, ignore_case: bool
) -> bool:
    """Whether every path in ``contained`` is covered by ``container``."""

    container_path = _folded_components(container.path, ignore_case)
    contained_path = _folded_components(contained.path, ignore_case)
    if container.kind is ScopeKind.FILE:
        return container_path == contained_path and contained.kind is ScopeKind.FILE
    return _is_component_ancestor(container_path, contained_path)


def scopes_overlap(
    left: ReservationScope, right: ReservationScope, ignore_case: bool
) -> bool:
    """Whether the two file/tree scope sets share at least one lexical path."""

    left_path = _folded_components(left.path, ignore_case)
    right_path = _folded_components(right.path, ignore_case)
    if left_path == right_path:
        return True
    if left.kind is ScopeKind.TREE and _is_component_ancestor(left_path, right_path):
        return True
    return right.kind is ScopeKind.TREE and _is_component_ancestor(right_path, left_path)


def _path_covered_by_scope(path: str, scope: ReservationScope, ignore_case: bool) -> bool:
    path_components = _folded_components(path, ignore_case)
    scope_components = _folded_components(scope.path, ignore_case)
    if scope.kind is ScopeKind.FILE:
        return path_components == scope_components
    return _is_component_ancestor(scope_components, path_components)


def _validate_minimal_antichain(
    scopes: tuple[ReservationScope, ...], ignore_case: bool, context: str
) -> list[str]:
    errors: list[str] = []
    for index, left in enumerate(scopes):
        for right in scopes[index + 1 :]:
            if scope_contains(left, right, ignore_case):
                errors.append(
                    f"{context}: {right.kind.value}:{right.path} is duplicate or contained by "
                    f"{left.kind.value}:{left.path}"
                )
            elif scope_contains(right, left, ignore_case):
                errors.append(
                    f"{context}: {left.kind.value}:{left.path} is duplicate or contained by "
                    f"{right.kind.value}:{right.path}"
                )
    return errors


def _expand_braces(expression: str) -> tuple[str, ...]:
    opening = expression.find("{")
    if opening < 0:
        if "}" in expression:
            raise WorkOrderValidationError(
                [f"Files: unmatched closing brace in {expression!r}"]
            )
        return (expression,)
    closing = expression.find("}", opening + 1)
    if closing < 0:
        raise WorkOrderValidationError([f"Files: unmatched opening brace in {expression!r}"])
    alternatives = expression[opening + 1 : closing].split(",")
    if not alternatives or any(not alternative for alternative in alternatives):
        raise WorkOrderValidationError([f"Files: empty brace alternative in {expression!r}"])
    expanded: list[str] = []
    for alternative in alternatives:
        substituted = expression[:opening] + alternative + expression[closing + 1 :]
        expanded.extend(_expand_braces(substituted))
    return tuple(expanded)


def _phase_for_offset(document: str, offset: int) -> tuple[str, str]:
    matches = [match for match in PHASE_HEADING.finditer(document, 0, offset)]
    if not matches:
        raise WorkOrderValidationError(
            ["Work Order heading is not contained by a numbered Phase heading"]
        )
    match = matches[-1]
    return match.group("identifier"), match.group(0)


def _bounded_work_orders(document: str) -> tuple[WorkOrderSource, ...]:
    headings = list(WORK_ORDER_HEADING.finditer(document))
    sources: list[WorkOrderSource] = []
    for index, heading in enumerate(headings):
        next_work_order = headings[index + 1].start() if index + 1 < len(headings) else len(document)
        next_phase_match = PHASE_HEADING.search(document, heading.end())
        next_phase = next_phase_match.start() if next_phase_match else len(document)
        end = min(next_work_order, next_phase)
        phase, phase_heading = _phase_for_offset(document, heading.start())
        sources.append(
            WorkOrderSource(
                phase=phase,
                phase_heading=phase_heading,
                markdown=document[heading.end() : end],
            )
        )
    if not sources:
        raise WorkOrderValidationError(["document contains no '#### Work Order' heading"])
    return tuple(sources)


def _select_work_orders(
    sources: tuple[WorkOrderSource, ...], selection: WorkOrderSelection
) -> tuple[WorkOrderSource, ...]:
    if isinstance(selection, EveryWorkOrderSelection):
        return sources
    phase_selection = selection.phase
    selected = tuple(
        source
        for source in sources
        if source.phase == phase_selection
        or phase_selection.casefold() in source.phase_heading.casefold()
    )
    if not selected:
        raise WorkOrderValidationError(
            [f"no Work Order matches phase selector {phase_selection!r}"]
        )
    if len(selected) > 1:
        raise WorkOrderValidationError(
            [f"phase selector {phase_selection!r} matches more than one Work Order"]
        )
    return selected


def _field_sections(markdown: str) -> dict[str, str]:
    matches = list(FIELD_HEADING.finditer(markdown))
    sections: dict[str, str] = {}
    duplicates: list[str] = []
    for index, match in enumerate(matches):
        label = match.group("label").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        content = (match.group("inline") + markdown[match.end() : end]).strip()
        if label in sections:
            duplicates.append(label)
        else:
            sections[label] = content
    if duplicates:
        raise WorkOrderValidationError(
            [f"Work Order repeats **{label}:**" for label in duplicates]
        )
    return sections


def _file_entries(files_content: str) -> tuple[WorkOrderFile, ...]:
    bullet_lines = [line for line in files_content.splitlines() if line.startswith("- ")]
    candidates = bullet_lines if bullet_lines else [files_content]
    entries: list[WorkOrderFile] = []
    errors: list[str] = []
    for candidate in candidates:
        path_side = re.split(r"\s+[—–]\s+", candidate, maxsplit=1)[0]
        description = candidate[len(path_side) :].casefold()
        coverage = (
            "verify_only"
            if "verify-only" in description or "verify only" in description
            else "implementation"
        )
        spans = CODE_SPAN.findall(path_side)
        if not spans:
            errors.append(f"Files: entry has no backticked path: {candidate.strip()!r}")
            continue
        residual = CODE_SPAN.sub("", path_side.removeprefix("- "))
        residual = re.sub(r"\band\b", "", residual)
        residual = re.sub(r"[\s,;]+", "", residual)
        if residual:
            errors.append(
                "Files: every path expression must be fully backticked; "
                f"unexpected text {residual!r} in {candidate.strip()!r}"
            )
            continue
        for span in spans:
            expression = LINE_REFERENCE.sub("", span.strip())
            try:
                for expanded in _expand_braces(expression):
                    path = validate_lexical_path(expanded, "Files")
                    entries.append(WorkOrderFile(path=path, coverage=coverage))
            except WorkOrderValidationError as error:
                errors.extend(error.errors)
    if not entries and not errors:
        errors.append("Files: section must name at least one backticked repository-relative path")
    seen: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            errors.append(f"Files: duplicate expanded path {entry.path!r}")
        seen.add(entry.path)
    if errors:
        raise WorkOrderValidationError(errors)
    return tuple(entries)


def _reservation_declaration(
    sections: dict[str, str], coverage: ReservationCoverageMode, ignore_case: bool
) -> ReservationDeclaration:
    if "Reservations" not in sections:
        if coverage is ReservationCoverageMode.REQUIRED:
            raise WorkOrderValidationError(
                ["Reservations: declaration is missing while coverage is required"]
            )
        return MissingReservationDeclaration()
    content = sections["Reservations"]
    if not content:
        raise WorkOrderValidationError(
            ["Reservations: a present declaration must contain at least one scope"]
        )
    scopes: list[ReservationScope] = []
    errors: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        match = RESERVATION_LINE.fullmatch(line)
        if not match:
            errors.append(f"Reservations: malformed line {line!r}")
            continue
        kind = ScopeKind(match.group(1))
        path = match.group(2)
        try:
            scopes.append(
                ReservationScope(kind=kind, path=validate_lexical_path(path, "Reservations"))
            )
        except WorkOrderValidationError as error:
            errors.extend(error.errors)
    if not scopes and not errors:
        errors.append("Reservations: declaration must contain at least one scope")
    scope_tuple = tuple(scopes)
    errors.extend(_validate_minimal_antichain(scope_tuple, ignore_case, "Reservations"))
    if errors:
        raise WorkOrderValidationError(errors)
    return DeclaredReservationDeclaration(scope_tuple)


def _validate_file_coverage(
    files: tuple[WorkOrderFile, ...],
    declaration: ReservationDeclaration,
    ignore_case: bool,
) -> list[str]:
    if isinstance(declaration, MissingReservationDeclaration):
        return []
    errors: list[str] = []
    implementation_paths = tuple(entry.path for entry in files if entry.requires_reservation)
    for path in implementation_paths:
        if not any(
            _path_covered_by_scope(path, scope, ignore_case)
            for scope in declaration.scopes
        ):
            errors.append(
                f"Files: implementation path {path!r} is not covered by Reservations"
            )
    for scope in declaration.scopes:
        if not any(
            _path_covered_by_scope(path, scope, ignore_case)
            for path in implementation_paths
        ):
            errors.append(
                f"Reservations: {scope.kind.value}:{scope.path} covers no implementation path in Files"
            )
    return errors


def validate_work_order(
    source: WorkOrderSource,
    coverage: ReservationCoverageMode,
    ignore_case: bool,
) -> ValidatedWorkOrder:
    """Validate one complete Work Order, including Files/Reservations agreement."""

    try:
        sections = _field_sections(source.markdown)
    except WorkOrderValidationError:
        raise
    errors: list[str] = []
    for field in ("Goal", "Spec", "Files"):
        if field not in sections:
            errors.append(f"Work Order is missing **{field}:**")
        elif not sections[field].strip():
            errors.append(f"Work Order has an empty **{field}:** section")
    if errors:
        raise WorkOrderValidationError(errors)

    try:
        files = _file_entries(sections["Files"])
        declaration = _reservation_declaration(sections, coverage, ignore_case)
    except WorkOrderValidationError as error:
        raise WorkOrderValidationError(
            [f"Phase {source.phase}: {message}" for message in error.errors]
        ) from error
    coverage_errors = _validate_file_coverage(files, declaration, ignore_case)
    if coverage_errors:
        raise WorkOrderValidationError(
            [f"Phase {source.phase}: {message}" for message in coverage_errors]
        )
    return ValidatedWorkOrder(
        phase=source.phase,
        phase_heading=source.phase_heading,
        goal=sections["Goal"].strip(),
        specification=sections["Spec"].strip(),
        files=files,
        declaration=declaration,
    )


def _repository_ignore_case(repository_root: Path, case_mode: str) -> bool:
    if case_mode == "insensitive":
        return True
    if case_mode == "sensitive":
        return False
    result = subprocess.run(
        ["git", "config", "--bool", "core.ignoreCase"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip().casefold() == "true"


def _read_document(document: str, repository_root: Path) -> tuple[Path, str, str]:
    requested = Path(document)
    absolute = requested if requested.is_absolute() else repository_root / requested
    absolute = absolute.resolve()
    root = repository_root.resolve()
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise WorkOrderValidationError(
            [f"plan document is outside the repository: {absolute}"]
        ) from error
    validate_lexical_path(relative, "plan document")
    try:
        contents = absolute.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkOrderValidationError(
            [f"could not read plan document {relative!r}: {error}"]
        ) from error
    return absolute, relative, contents


def _validated_orders(
    document: str,
    repository_root: Path,
    selection: WorkOrderSelection,
    coverage: ReservationCoverageMode,
    ignore_case: bool,
) -> tuple[str, tuple[ValidatedWorkOrder, ...]]:
    _, relative, contents = _read_document(document, repository_root)
    sources = _select_work_orders(_bounded_work_orders(contents), selection)
    orders = tuple(validate_work_order(source, coverage, ignore_case) for source in sources)
    return relative, orders


def _generated_declaration(
    order: ValidatedWorkOrder, ignore_case: bool
) -> DeclaredReservationDeclaration:
    scopes = tuple(
        ReservationScope(ScopeKind.FILE, entry.path)
        for entry in order.files
        if entry.requires_reservation
    )
    if not scopes:
        raise WorkOrderValidationError(
            [f"Phase {order.phase}: Files contains no implementation path to reserve"]
        )
    errors = _validate_minimal_antichain(scopes, ignore_case, "generated Reservations")
    if errors:
        raise WorkOrderValidationError(errors)
    return DeclaredReservationDeclaration(scopes)


def _reservations_markdown(declaration: DeclaredReservationDeclaration) -> str:
    lines = ["**Reservations:**", ""]
    lines.extend(f"- {scope.kind.value}: `{scope.path}`" for scope in declaration.scopes)
    return "\n".join(lines)


def _derived_next_items_path(plan_path: str) -> str:
    plan = Path(plan_path)
    normalized_stem = re.sub(r"[^a-z0-9]+", "-", plan.stem.casefold()).strip("-")
    if not normalized_stem:
        raise WorkOrderValidationError(
            [f"plan document stem cannot derive a next-items path: {plan_path!r}"]
        )
    return plan.with_name(f"{normalized_stem}-next{plan.suffix}").as_posix()


def _resolved_scopes(
    order: ValidatedWorkOrder, plan_path: str, ignore_case: bool
) -> tuple[ReservationScope, ...]:
    if isinstance(order.declaration, MissingReservationDeclaration):
        raise WorkOrderValidationError(
            [f"Phase {order.phase}: cannot resolve a missing Reservations declaration"]
        )
    scopes = order.declaration.scopes + (
        ReservationScope(ScopeKind.FILE, plan_path),
        ReservationScope(ScopeKind.FILE, _derived_next_items_path(plan_path)),
    )
    errors = _validate_minimal_antichain(scopes, ignore_case, "resolved claim footprint")
    if errors:
        raise WorkOrderValidationError(errors)
    return scopes


def _compare_declarations(
    left: ValidatedWorkOrder,
    right: ValidatedWorkOrder,
    ignore_case: bool,
) -> list[dict[str, object]]:
    if isinstance(left.declaration, MissingReservationDeclaration):
        raise WorkOrderValidationError(
            [f"Phase {left.phase}: comparison requires a declared Reservations block"]
        )
    if isinstance(right.declaration, MissingReservationDeclaration):
        raise WorkOrderValidationError(
            [f"Phase {right.phase}: comparison requires a declared Reservations block"]
        )
    collisions: list[dict[str, object]] = []
    for left_scope in left.declaration.scopes:
        for right_scope in right.declaration.scopes:
            if scopes_overlap(left_scope, right_scope, ignore_case):
                collisions.append(
                    {"left": left_scope.tagged(), "right": right_scope.tagged()}
                )
    return collisions


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        default=os.getcwd(),
        help="repository root used to resolve plan paths and core.ignoreCase",
    )
    parser.add_argument(
        "--case-mode",
        choices=("repository", "sensitive", "insensitive"),
        default="repository",
        help="path comparison policy; repository reads git core.ignoreCase",
    )
    return parser


def _coverage(value: str) -> ReservationCoverageMode:
    return ReservationCoverageMode(value)


def _selection(value: str) -> WorkOrderSelection:
    if value:
        return ExactPhaseSelection(value)
    return EveryWorkOrderSelection()


def _build_parser() -> argparse.ArgumentParser:
    parser = _common_parser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    validate = subparsers.add_parser("validate", help="validate complete Work Orders")
    validate.add_argument("--document", required=True)
    validate.add_argument("--phase", default="")
    validate.add_argument(
        "--coverage", choices=("advisory", "required"), required=True
    )

    emit = subparsers.add_parser(
        "emit-reservations", help="derive conservative exact-file Reservations Markdown"
    )
    emit.add_argument("--document", required=True)
    emit.add_argument("--phase", required=True)

    resolve = subparsers.add_parser(
        "resolve", help="resolve one declared Work Order to claim arguments"
    )
    resolve.add_argument("--document", required=True)
    resolve.add_argument("--phase", required=True)
    resolve.add_argument(
        "--coverage", choices=("advisory", "required"), required=True
    )

    compare = subparsers.add_parser(
        "compare", help="compare two declared Work Orders without the ledger"
    )
    compare.add_argument("--left-document", required=True)
    compare.add_argument("--left-phase", required=True)
    compare.add_argument("--right-document", required=True)
    compare.add_argument("--right-phase", required=True)

    return parser


def main(argv: list[str]) -> int:
    arguments = _build_parser().parse_args(argv)
    operation = arguments.operation
    repository_root = Path(arguments.repository_root).resolve()
    ignore_case = _repository_ignore_case(repository_root, arguments.case_mode)
    try:
        if operation == "validate":
            plan_path, orders = _validated_orders(
                arguments.document,
                repository_root,
                _selection(arguments.phase),
                _coverage(arguments.coverage),
                ignore_case,
            )
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "validate",
                    "coverage_mode": _coverage(arguments.coverage).tagged(),
                    "case_comparison": {
                        "kind": "insensitive" if ignore_case else "sensitive"
                    },
                    "document": plan_path,
                    "outcome": {"kind": "valid", "work_orders": [order.tagged() for order in orders]},
                }
            )
            return 0

        if operation == "emit-reservations":
            plan_path, orders = _validated_orders(
                arguments.document,
                repository_root,
                ExactPhaseSelection(arguments.phase),
                ReservationCoverageMode.ADVISORY,
                ignore_case,
            )
            declaration = _generated_declaration(orders[0], ignore_case)
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "emit_reservations",
                    "coverage_mode": ReservationCoverageMode.ADVISORY.tagged(),
                    "document": plan_path,
                    "phase": orders[0].phase,
                    "reservation_declaration": declaration.tagged(),
                    "markdown": _reservations_markdown(declaration),
                    "outcome": {"kind": "generated"},
                }
            )
            return 0

        if operation == "resolve":
            coverage = _coverage(arguments.coverage)
            plan_path, orders = _validated_orders(
                arguments.document,
                repository_root,
                ExactPhaseSelection(arguments.phase),
                coverage,
                ignore_case,
            )
            order = orders[0]
            scopes = _resolved_scopes(order, plan_path, ignore_case)
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "resolve",
                    "coverage_mode": coverage.tagged(),
                    "case_comparison": {
                        "kind": "insensitive" if ignore_case else "sensitive"
                    },
                    "document": plan_path,
                    "phase": order.phase,
                    "reservation_declaration": order.declaration.tagged(),
                    "resolved_claim": {
                        "kind": "declared_with_plan_scopes",
                        "scopes": [scope.tagged() for scope in scopes],
                        "arguments": [scope.argument() for scope in scopes],
                    },
                    "outcome": {"kind": "resolved"},
                }
            )
            return 0

        left_path, left_orders = _validated_orders(
            arguments.left_document,
            repository_root,
            ExactPhaseSelection(arguments.left_phase),
            ReservationCoverageMode.REQUIRED,
            ignore_case,
        )
        right_path, right_orders = _validated_orders(
            arguments.right_document,
            repository_root,
            ExactPhaseSelection(arguments.right_phase),
            ReservationCoverageMode.REQUIRED,
            ignore_case,
        )
        collisions = _compare_declarations(left_orders[0], right_orders[0], ignore_case)
        _emit(
            {
                "contract": CONTRACT,
                "operation": "compare",
                "case_comparison": {
                    "kind": "insensitive" if ignore_case else "sensitive"
                },
                "left": {"document": left_path, "phase": left_orders[0].phase},
                "right": {"document": right_path, "phase": right_orders[0].phase},
                "outcome": {
                    "kind": "collision" if collisions else "disjoint",
                    "collisions": collisions,
                },
            }
        )
        return 1 if collisions else 0
    except WorkOrderValidationError as error:
        return _invalid(operation, error.errors)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
