#!/usr/bin/env python3
"""Read style rules and emit focused evaluation units without changing state."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

RUST_DIR = Path(os.environ.get("STYLE_HISTORY_RUST_DIR", str(Path.home() / "rust")))
NATE_STYLE_DIR = Path(os.environ.get("STYLE_HISTORY_NATE_STYLE_DIR", str(RUST_DIR / "nate_style")))
HISTORY_DIR = NATE_STYLE_DIR / ".history"
LOAD_STYLE_SCRIPT = Path(
    os.environ.get(
        "STYLE_HISTORY_LOAD_STYLE_SCRIPT",
        str(Path.home() / ".claude" / "scripts" / "rust_style" / "load-rust-style.sh"),
    )
)


class Outcome(TypedDict, total=False):
    status: str
    reason: str
    summary: str
    finding_source: str
    skipped_by: str


class ReviewedUnit(TypedDict, total=False):
    guideline_id: str
    outcome: Outcome
    finding_source: str


class HistoryRow(TypedDict, total=False):
    start_time: str
    end_time: str
    reviewed_units: list[ReviewedUnit]
    evaluation_summary: dict[str, object]
    fingerprint: str


@dataclass(frozen=True)
class Unit:
    unit_id: str
    budget_cost: int
    checklist_index: int
    display_name: str
    guideline_ids: tuple[str, ...]
    see_also_guideline_ids: tuple[str, ...]
    pre_filter: str = ""


def normalize_guideline_id(raw_path: str, project_root: Path | None = None) -> str:
    path = raw_path.strip().strip("`")
    if path.startswith("~"):
        path = str(Path(path).expanduser())
    path_obj = Path(path)
    try:
        return str(path_obj.relative_to(NATE_STYLE_DIR))
    except ValueError:
        pass
    if "/docs/style/" in path:
        return "docs/style/" + path.split("/docs/style/", 1)[1]
    if project_root is not None:
        try:
            return str(path_obj.relative_to(project_root))
        except ValueError:
            pass
    return path_obj.name


def _extract_wikilink(raw: str) -> str:
    value = raw.strip()
    if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) >= 2:
        value = value[1:-1]
    if value.startswith("[[") and value.endswith("]]"):
        return value[2:-2].strip()
    return ""


def parse_frontmatter(path: Path) -> dict[str, list[str]]:
    tags: list[str] = []
    see_also: list[str] = []
    metadata = {"tags": tags, "see_also": see_also}
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return metadata
    current_key = ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped:
            continue
        if stripped.startswith("-") and current_key:
            value = stripped.lstrip("-").strip()
            if current_key == "tags":
                tags.append(value)
            elif current_key == "see_also":
                extracted = _extract_wikilink(value)
                if extracted:
                    see_also.append(extracted)
            continue
        if ":" not in stripped:
            current_key = ""
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if line == line.lstrip() and key in ("tier", "family", "input_scope", "mechanism", "mode", "approval"):
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            metadata[key] = [value]
        if key == "tags":
            current_key = "tags" if not value else ""
        elif key == "see_also":
            if value:
                extracted = _extract_wikilink(value)
                if extracted:
                    see_also.append(extracted)
                current_key = ""
            else:
                current_key = "see_also"
        else:
            current_key = ""
    return metadata


def extract_title(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.name


def read_pre_filter(path: Path) -> str:
    """Extract a top-level scalar `pre_filter:` field from the frontmatter.

    Returns the regex string, or empty string when absent. Strips surrounding
    quotes (single or double) so the value can be passed directly to ripgrep.
    """
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not line.startswith("pre_filter:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        return value
    return ""


def list_style_files(project_root: Path) -> list[Path]:
    result = subprocess.run(
        ["zsh", str(LOAD_STYLE_SCRIPT), "--list-files", "--project-root", str(project_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def build_units_from_files(
    style_files: list[Path],
    project_root: Path,
    resolution_corpus: list[Path],
) -> list[Unit]:
    # Resolve see_also wikilink stems against the full style-file population,
    # not just the subset being built into units. This matters for focused evals
    # where only a handful of guidelines are requested but their see_alsos may
    # point anywhere in the guide.
    stem_to_guideline_id: dict[str, str] = {
        style_file.stem: normalize_guideline_id(str(style_file), project_root)
        for style_file in resolution_corpus
    }
    units: list[Unit] = []
    for index, style_file in enumerate(style_files, start=1):
        frontmatter = parse_frontmatter(style_file)
        tags = set(frontmatter["tags"])
        see_also_stems = list(frontmatter["see_also"])
        guideline_id = normalize_guideline_id(str(style_file), project_root)
        see_also_ids: list[str] = []
        for stem in see_also_stems:
            resolved = stem_to_guideline_id.get(stem)
            if resolved and resolved not in see_also_ids:
                see_also_ids.append(resolved)
        units.append(
            Unit(
                unit_id=guideline_id,
                budget_cost=0 if "non-negotiable" in tags else 1,
                checklist_index=index,
                display_name=extract_title(style_file),
                guideline_ids=(guideline_id,),
                see_also_guideline_ids=tuple(see_also_ids),
                pre_filter=read_pre_filter(style_file),
            )
        )
    return units


def resolve_focus_targets(requested: list[str], project_root: Path) -> list[Path]:
    """Resolve user-supplied guideline identifiers to absolute style-file paths.

    Accepts any mix of:
      - bare stems (`when-to-split-a-module`)
      - filenames (`when-to-split-a-module.md`)
      - guideline IDs (`rust/when-to-split-a-module.md`)
      - absolute paths
    Raises SystemExit with the first unresolvable entry so the caller can fix input.
    """
    style_files = list_style_files(project_root)
    by_stem: dict[str, Path] = {p.stem: p for p in style_files}
    by_name: dict[str, Path] = {p.name: p for p in style_files}
    by_guideline_id: dict[str, Path] = {
        normalize_guideline_id(str(p), project_root): p for p in style_files
    }
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in requested:
        value = raw.strip()
        if not value:
            continue
        candidates: list[Path | None] = [
            by_guideline_id.get(value),
            by_name.get(value),
            by_stem.get(value),
        ]
        if value.endswith(".md"):
            candidates.append(by_stem.get(value[:-3]))
        else:
            candidates.append(by_name.get(f"{value}.md"))
        absolute = Path(value).expanduser()
        if absolute.is_absolute() and absolute.exists():
            candidates.append(absolute)
        path = next((c for c in candidates if c is not None), None)
        if path is None:
            raise SystemExit(f"Could not resolve guideline: {raw}")
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def focused_units(project_root: Path, requested: list[str]) -> list[Unit]:
    """Build units for an explicit subset of guidelines — read-only, no state writes."""
    targets = resolve_focus_targets(requested, project_root)
    all_files = list_style_files(project_root)
    return build_units_from_files(targets, project_root, all_files)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    focused = subparsers.add_parser(
        "focused-eval",
        help="Read-only: emit one JSON unit per requested guideline (no pending/history state).",
    )
    _ = focused.add_argument("--project-root", required=True)
    _ = focused.add_argument(
        "--guideline",
        action="append",
        required=True,
        help="Guideline to include. Accepts stem, filename, or guideline id. Repeatable.",
    )
    return parser.parse_args()


def _arg_str(args: argparse.Namespace, name: str) -> str:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    if not isinstance(value, str):
        raise SystemExit(f"Argument --{name.replace('_', '-')} must be a string.")
    return value


def _arg_str_list(args: argparse.Namespace, name: str) -> list[str]:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    if not isinstance(value, list):
        raise SystemExit(f"Argument --{name.replace('_', '-')} must be a list.")
    items: list[str] = []
    for entry in cast(list[object], value):
        if not isinstance(entry, str):
            raise SystemExit(f"Argument --{name.replace('_', '-')} must contain strings.")
        items.append(entry)
    return items


def main() -> None:
    args = parse_args()
    command = _arg_str(args, "command")
    if command == "focused-eval":
        project_root = Path(_arg_str(args, "project_root")).expanduser().resolve()
        units = focused_units(project_root, _arg_str_list(args, "guideline"))
        for unit in units:
            print(json.dumps(
                {
                    "unit_id": unit.unit_id,
                    "display_name": unit.display_name,
                    "guideline_ids": list(unit.guideline_ids),
                    "see_also_guideline_ids": list(unit.see_also_guideline_ids),
                },
                sort_keys=True,
            ))
        return


if __name__ == "__main__":
    main()
