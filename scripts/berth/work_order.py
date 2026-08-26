#!/usr/bin/env python3
"""Parse and validate cargo-berth Work Order structure.

The module is intentionally independent of the cargo-berth ledger.  Work Order
authors and readers share this one lexical contract instead of teaching plan
commands subtly different Markdown grammars.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


CONTRACT = "cargo-berth-work-order/v1"
FIELD_HEADING = re.compile(
    r"^\*\*(?P<label>[^*\n]+):\*\*(?P<inline>.*)$", re.MULTILINE
)
PENDING_DECISION_HEADING = re.compile(
    r"^\*\*Pending decision:\s*(?P<subject>[^*\n]+)\*\*\s*$", re.MULTILINE
)
WORK_ORDER_HEADING = re.compile(r"^#### Work Order\s*$", re.MULTILINE)
PHASE_HEADING = re.compile(r"^### Phase\s+(?P<identifier>\d+)\b(?P<title>.*)$", re.MULTILINE)
SECTION_HEADING = re.compile(r"^## ", re.MULTILINE)
CODE_SPAN = re.compile(r"`([^`]+)`")
LINE_REFERENCE = re.compile(r"(?::\d+(?:-\d+)?|#L\d+(?:-L\d+)?)$")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")
SHELL_VARIABLE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
WORK_ORDER_FIELD_LABELS = frozenset(
    {
        "Goal",
        "Spec",
        "Files",
        "Acceptance gate",
        "Constraints from prior phases",
        "Pending decision",
        "Style",
        "Binds later work",
        "Gotchas",
        "Ruled out",
    }
)


@dataclass(frozen=True)
class WorkOrderFile:
    """One validated expanded path named by Files."""

    path: str

    def tagged(self) -> dict[str, str]:
        return {"path": self.path}


@dataclass(frozen=True)
class ValidatedWorkOrder:
    """A complete Work Order safe for plan writers and delegation readers."""

    phase: str
    phase_heading: str
    goal: str
    specification: str
    files: tuple[WorkOrderFile, ...]

    def tagged(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "phase_heading": self.phase_heading,
            "goal": self.goal,
            "specification": self.specification,
            "files": [entry.tagged() for entry in self.files],
        }


@dataclass(frozen=True)
class WorkOrderSource:
    """The bounded Markdown and phase identity for one Work Order heading."""

    phase: str
    phase_heading: str
    markdown: str


@dataclass(frozen=True)
class WorkOrderFieldBoundary:
    """One Markdown field boundary within a Work Order."""

    start: int
    end: int
    label: str
    inline: str


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
        self.errors: tuple[str, ...] = tuple(errors)
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
    if not path:
        errors.append(f"{context}: path must be a non-empty string")
    elif "\\" in path:
        errors.append(f"{context}: path must use '/' separators: {path!r}")
    elif path.split("/", maxsplit=1)[0].startswith("~"):
        errors.append(
            f"{context}: home-relative path rule rejects a first component beginning with '~': {path!r}"
        )
    elif SHELL_VARIABLE.search(path):
        errors.append(
            f"{context}: shell-variable path rule rejects $NAME and ${{NAME}} references: {path!r}"
        )
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

    _ = _lexical_components(path, context)
    return path


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
        next_section_match = SECTION_HEADING.search(document, heading.end())
        next_section = next_section_match.start() if next_section_match else len(document)
        end = min(next_work_order, next_phase, next_section)
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
    # An exact phase identifier wins outright. The heading substring match is a
    # convenience for selecting by title, and on its own it makes every
    # single-digit selector ambiguous once a plan reaches ten phases ("1" is a
    # substring of "Phase 10"). Falling back to it only when nothing matched
    # exactly keeps both spellings usable.
    selected = tuple(source for source in sources if source.phase == phase_selection)
    if not selected:
        selected = tuple(
            source
            for source in sources
            if phase_selection.casefold() in source.phase_heading.casefold()
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


def _field_boundaries(markdown: str) -> tuple[WorkOrderFieldBoundary, ...]:
    headings = [
        WorkOrderFieldBoundary(
            start=match.start(),
            end=match.end(),
            label=match.group("label").strip(),
            inline=match.group("inline"),
        )
        for match in FIELD_HEADING.finditer(markdown)
        if not _inside_fenced_code(markdown, match.start())
    ]
    headings.extend(
        WorkOrderFieldBoundary(
            start=match.start(),
            end=match.end(),
            label="Pending decision",
            inline=match.group("subject"),
        )
        for match in PENDING_DECISION_HEADING.finditer(markdown)
        if not _inside_fenced_code(markdown, match.start())
    )
    headings.sort(key=lambda heading: heading.start)
    return tuple(headings)


def _inside_fenced_code(markdown: str, offset: int) -> bool:
    fence_count = sum(
        1
        for line in markdown[:offset].splitlines()
        if line.lstrip().startswith("```")
    )
    return fence_count % 2 == 1


def _field_sections(markdown: str) -> dict[str, str]:
    matches = _field_boundaries(markdown)
    sections: dict[str, str] = {}
    duplicates: list[str] = []
    for index, match in enumerate(matches):
        label = match.label
        end = matches[index + 1].start if index + 1 < len(matches) else len(markdown)
        content = (match.inline + markdown[match.end : end]).strip()
        if label not in WORK_ORDER_FIELD_LABELS:
            continue
        if label in sections:
            duplicates.append(label)
        else:
            sections[label] = content
    if duplicates:
        raise WorkOrderValidationError(
            [f"Work Order repeats **{label}:**" for label in duplicates]
        )
    return sections


def _logical_file_entries(files_content: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    candidates: list[str] = []
    errors: list[str] = []
    current: list[str] = []
    for line_number, line in enumerate(files_content.splitlines(), start=1):
        if line.startswith("- `"):
            if current:
                candidates.append(" ".join(current))
            current = [line]
        elif line.startswith("- **") and current:
            current.append(line)
        elif line.startswith("- "):
            if current:
                candidates.append(" ".join(current))
            current = [line]
        elif not line.strip():
            continue
        elif current:
            current.append(line.strip())
        else:
            errors.append(
                f"Files: line {line_number} is neither a '- ' bullet nor an indented continuation: {line!r}"
            )
    if current:
        candidates.append(" ".join(current))
    if not candidates and not errors:
        errors.append("Files: section must contain at least one '- ' bullet")
    return tuple(candidates), tuple(errors)


def _absolute_path_components(path: str, context: str) -> tuple[str, ...]:
    errors: list[str] = []
    if "\\" in path:
        errors.append(f"{context}: absolute path must use '/' separators: {path!r}")
    if SHELL_VARIABLE.search(path):
        errors.append(
            f"{context}: shell-variable path rule rejects $NAME and ${{NAME}} references: {path!r}"
        )
    if "{" in path or "}" in path:
        errors.append(f"{context}: brace expressions must be expanded: {path!r}")
    if "\x00" in path or "\n" in path or "\r" in path:
        errors.append(f"{context}: path contains a forbidden control character")
    without_one_trailing_slash = path[:-1] if path.endswith("/") else path
    components = tuple(without_one_trailing_slash.split("/")[1:])
    if not components or any(component == "" for component in components):
        errors.append(f"{context}: absolute path has an empty component: {path!r}")
    if any(component in {".", ".."} for component in components):
        errors.append(f"{context}: absolute path has a '.' or '..' component: {path!r}")
    if any(component.casefold() == ".git" for component in components):
        errors.append(f"{context}: path may not enter .git: {path!r}")
    if errors:
        raise WorkOrderValidationError(errors)
    return components


def _work_order_file(
    expression: str,
    repository_root: Path,
) -> WorkOrderFile:
    explicit_directory = expression.endswith("/")
    path_without_trailing_slash = expression[:-1] if explicit_directory else expression
    if path_without_trailing_slash.startswith("~/"):
        path_without_trailing_slash = (
            Path.home() / path_without_trailing_slash.removeprefix("~/")
        ).as_posix()
    if path_without_trailing_slash.startswith("/"):
        absolute_expression = path_without_trailing_slash + ("/" if explicit_directory else "")
        _ = _absolute_path_components(absolute_expression, "Files")
        absolute = Path(path_without_trailing_slash)
        try:
            relative = absolute.relative_to(repository_root).as_posix()
        except ValueError:
            return WorkOrderFile(path=absolute.as_posix())
        path = validate_lexical_path(relative, "Files")
    else:
        path = validate_lexical_path(path_without_trailing_slash, "Files")
    return WorkOrderFile(path=path)


def _file_entries(files_content: str, repository_root: Path) -> tuple[WorkOrderFile, ...]:
    candidates, logical_errors = _logical_file_entries(files_content)
    entries: list[WorkOrderFile] = []
    errors = list(logical_errors)
    for candidate in candidates:
        path_side = re.split(r"\s+[—–]\s+", candidate, maxsplit=1)[0]
        spans = [match.group(1) for match in CODE_SPAN.finditer(path_side)]
        if not spans:
            errors.append(f"Files: entry has no backticked path: {candidate.strip()!r}")
            continue
        residual = CODE_SPAN.sub("", path_side.removeprefix("- "))
        residual = re.sub(r"\band\b", "", residual)
        residual = re.sub(r"[\s,;]+", "", residual)
        if residual:
            errors.append(
                "Files: every path expression must be fully backticked; "
                + f"unexpected text {residual!r} in {candidate.strip()!r}"
            )
            continue
        for span in spans:
            expression = LINE_REFERENCE.sub("", span.strip())
            try:
                for expanded in _expand_braces(expression):
                    entries.append(_work_order_file(expanded, repository_root))
            except WorkOrderValidationError as error:
                errors.extend(error.errors)
    if not entries and not errors:
        errors.append("Files: section must name at least one backticked path")
    seen: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            errors.append(f"Files: duplicate expanded path {entry.path!r}")
        seen.add(entry.path)
    if errors:
        raise WorkOrderValidationError(errors)
    return tuple(entries)


def validate_work_order(
    source: WorkOrderSource,
    repository_root: Path,
) -> ValidatedWorkOrder:
    """Validate one complete Work Order's Goal, Spec, and Files structure."""

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
        files = _file_entries(sections["Files"], repository_root)
    except WorkOrderValidationError as error:
        raise WorkOrderValidationError(
            [f"Phase {source.phase}: {message}" for message in error.errors]
        ) from error
    return ValidatedWorkOrder(
        phase=source.phase,
        phase_heading=source.phase_heading,
        goal=sections["Goal"].strip(),
        specification=sections["Spec"].strip(),
        files=files,
    )


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
    _ = validate_lexical_path(relative, "plan document")
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
) -> tuple[str, tuple[ValidatedWorkOrder, ...]]:
    _, relative, contents = _read_document(document, repository_root)
    sources = _select_work_orders(_bounded_work_orders(contents), selection)
    orders = tuple(
        validate_work_order(source, repository_root) for source in sources
    )
    return relative, orders


class WorkOrderArguments(argparse.Namespace):
    """Fully typed command-line values for every Work Order operation."""

    repository_root: str = ""
    operation: str = ""
    document: str = ""
    phase: str = ""


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--repository-root",
        default=os.getcwd(),
        help="repository root used to resolve plan paths",
    )
    return parser


def _selection(value: str) -> WorkOrderSelection:
    if value:
        return ExactPhaseSelection(value)
    return EveryWorkOrderSelection()


def _build_parser() -> argparse.ArgumentParser:
    parser = _common_parser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    validate = subparsers.add_parser("validate", help="validate complete Work Orders")
    _ = validate.add_argument("--document", required=True)
    _ = validate.add_argument("--phase", default="")

    resolve = subparsers.add_parser(
        "resolve", help="validate and select one complete Work Order"
    )
    _ = resolve.add_argument("--document", required=True)
    _ = resolve.add_argument("--phase", required=True)

    return parser


def main(argv: list[str]) -> int:
    arguments = _build_parser().parse_args(argv, namespace=WorkOrderArguments())
    operation = arguments.operation
    repository_root = Path(arguments.repository_root).resolve()
    try:
        if operation == "validate":
            plan_path, orders = _validated_orders(
                arguments.document,
                repository_root,
                _selection(arguments.phase),
            )
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "validate",
                    "document": plan_path,
                    "outcome": {"kind": "valid", "work_orders": [order.tagged() for order in orders]},
                }
            )
            return 0

        plan_path, orders = _validated_orders(
            arguments.document,
            repository_root,
            ExactPhaseSelection(arguments.phase),
        )
        _emit(
            {
                "contract": CONTRACT,
                "operation": "resolve",
                "document": plan_path,
                "phase": orders[0].phase,
                "work_order": orders[0].tagged(),
                "outcome": {"kind": "resolved"},
            }
        )
        return 0
    except WorkOrderValidationError as error:
        return _invalid(operation, error.errors)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
