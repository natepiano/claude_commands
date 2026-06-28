#!/usr/bin/env python3
"""Rename a clean-fix project key and migrate its history state."""

from __future__ import annotations

import argparse
import json
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from project_add import (  # pyright: ignore[reportImplicitRelativeImport]
    DEFAULT_CONF,
    DEFAULT_RUST_DIR,
    MARKER,
    Project,
    project_key,
    relative_posix,
    resolve_project,
)

DEFAULT_HISTORY_DIR = Path.home() / "rust" / "nate_style" / ".history"

RenameStatus = Literal["change", "noop"]


@dataclass(frozen=True)
class ConfigEntry:
    entry: str
    key: str
    index: int
    skipped: bool


@dataclass(frozen=True)
class PlannedMove:
    old: Path
    new: Path
    label: str
    exists: bool


@dataclass(frozen=True)
class PlannedMarker:
    path: Path


@dataclass(frozen=True)
class Plan:
    old_entry: str
    old_key: str
    new_project: Project
    config_lines: list[str]
    config_changes: list[str]
    moves: list[PlannedMove]
    marker_updates: list[PlannedMarker]
    pending_path: Path | None
    pending_project_root_changed: bool


class CliArgs(argparse.Namespace):
    old: str = ""
    new: str = ""
    conf: Path = DEFAULT_CONF
    rust_dir: Path = DEFAULT_RUST_DIR
    history_dir: Path = DEFAULT_HISTORY_DIR
    dry_run: bool = False


def section_for_lines(lines: list[str]) -> list[str | None]:
    sections: list[str | None] = []
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        sections.append(current)
    return sections


def section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = -1
    for index, raw in enumerate(lines):
        if raw.strip() == f"[{section}]":
            start = index
            break
    if start < 0:
        raise ValueError(f"section [{section}] not found")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def uncommented_body(line: str) -> str:
    body = line.strip()
    if body.startswith(MARKER):
        body = body[len(MARKER):].strip()
    return body.split("#", 1)[0].strip()


def is_tagged(line: str) -> bool:
    return line.strip().startswith(MARKER)


def replace_entry_line(line: str, new_entry: str) -> str:
    leading = line[: len(line) - len(line.lstrip())]
    body = line.lstrip()
    marker = ""
    if body.startswith(MARKER):
        marker = f"{MARKER} "
        body = body[len(MARKER):].lstrip()
    comment = ""
    if "#" in body:
        before, _, after = body.partition("#")
        body = before.rstrip()
        comment = f" #{after}"
    if not body:
        return line
    return f"{leading}{marker}{new_entry}{comment}"


def replace_kv_line(line: str, key: str, value: str) -> str:
    leading = line[: len(line) - len(line.lstrip())]
    body = line.lstrip()
    comment = ""
    if "#" in body:
        before, _, after = body.partition("#")
        body = before.rstrip()
        comment = f" #{after}"
    if "=" not in body:
        return line
    return f"{leading}{key} = {value}{comment}"


def body_path_replace(value: str, old_entry: str, new_entry: str) -> str:
    if value == old_entry:
        return new_entry
    prefix = f"{old_entry}/"
    if value.startswith(prefix):
        return f"{new_entry}/{value[len(prefix):]}"
    return value


def old_selectors(raw_old: str, rust_dir: Path) -> set[str]:
    selectors = {raw_old}
    raw_path = Path(raw_old).expanduser()
    if raw_path.name == "Cargo.toml":
        raw_path = raw_path.parent
    path = raw_path if raw_path.is_absolute() else rust_dir / raw_path
    try:
        selectors.add(relative_posix(path.resolve(), rust_dir.resolve()))
    except ValueError:
        pass
    selectors.add(Path(raw_old).name)
    return {selector for selector in selectors if selector}


def project_entries(lines: list[str]) -> list[ConfigEntry]:
    out: list[ConfigEntry] = []
    sections = section_for_lines(lines)
    for index, section in enumerate(sections):
        if section != "projects":
            continue
        body = uncommented_body(lines[index])
        if not body:
            continue
        out.append(
            ConfigEntry(
                entry=body,
                key=project_key(body),
                index=index,
                skipped=is_tagged(lines[index]),
            )
        )
    return out


def find_old_project(lines: list[str], raw_old: str, rust_dir: Path) -> ConfigEntry:
    selectors = old_selectors(raw_old, rust_dir)
    matches = [
        entry
        for entry in project_entries(lines)
        if entry.entry in selectors or entry.key in selectors
    ]
    unique: dict[str, ConfigEntry] = {entry.entry: entry for entry in matches}
    if not unique:
        raise ValueError(f"unknown project: {raw_old}")
    if len(unique) > 1:
        rendered = ", ".join(sorted(unique))
        raise ValueError(f"ambiguous project {raw_old}: {rendered}")
    return next(iter(unique.values()))


def last_content_index(lines: list[str], section: str) -> int:
    start, end = section_bounds(lines, section)
    last = start
    for index in range(start + 1, end):
        if lines[index].strip():
            last = index
    return last


def ensure_no_project_collision(
    entries: list[ConfigEntry],
    old_entry: ConfigEntry,
    new_project: Project,
) -> None:
    for entry in entries:
        if entry.index == old_entry.index:
            continue
        if entry.entry == new_project.entry:
            raise ValueError(f"[projects] already contains {new_project.entry}")
        if entry.key == new_project.key:
            raise ValueError(
                f"[projects] key {new_project.key} already maps to {entry.entry}"
            )


def replace_project_entry(
    lines: list[str],
    old_project: ConfigEntry,
    new_project: Project,
) -> tuple[list[str], list[str]]:
    out = list(lines)
    changes: list[str] = []
    if old_project.entry != new_project.entry:
        out[old_project.index] = replace_entry_line(out[old_project.index], new_project.entry)
        status = "skipped " if old_project.skipped else ""
        changes.append(f"[projects] {status}{old_project.entry} -> {new_project.entry}")
    return out, changes


def replace_build_entries(
    lines: list[str],
    old_entry: str,
    old_key: str,
    new_entry: str,
) -> tuple[list[str], list[str]]:
    out = list(lines)
    changes: list[str] = []
    sections = section_for_lines(out)
    build_indices = [index for index, section in enumerate(sections) if section == "build"]
    old_indices: list[int] = []
    new_present = False

    for index in build_indices:
        body = uncommented_body(out[index])
        if not body:
            continue
        if body == new_entry:
            new_present = True
        if body == old_entry or project_key(body) == old_key:
            old_indices.append(index)

    if old_indices:
        first = old_indices[0]
        if not new_present:
            out[first] = replace_entry_line(out[first], new_entry)
            changes.append(f"[build] {old_entry} -> {new_entry}")
        for index in reversed(old_indices[1:] if not new_present else old_indices):
            removed = uncommented_body(out[index])
            del out[index]
            changes.append(f"[build] removed duplicate old entry {removed}")
        return out, changes

    if not new_present:
        out.insert(last_content_index(out, "build") + 1, new_entry)
        changes.append(f"[build] added {new_entry}")
    return out, changes


def kv_lines(lines: list[str], section: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for index, current in enumerate(section_for_lines(lines)):
        if current != section:
            continue
        body = uncommented_body(lines[index])
        if not body or "=" not in body:
            continue
        key, _, value = body.partition("=")
        out.append((index, key.strip(), value.strip()))
    return out


def update_active_checkout(
    lines: list[str],
    old_entry: str,
    new_entry: str,
) -> tuple[list[str], list[str]]:
    out = list(lines)
    changes: list[str] = []
    for index, key, value in kv_lines(out, "active_checkout"):
        if key == new_entry and old_entry != new_entry:
            raise ValueError(f"[active_checkout] already contains {new_entry}")

    for index, key, value in kv_lines(out, "active_checkout"):
        new_key = new_entry if key == old_entry else key
        new_value = body_path_replace(value, old_entry, new_entry)
        if new_key != key or new_value != value:
            out[index] = replace_kv_line(out[index], new_key, new_value)
            changes.append(f"[active_checkout] {key} = {value} -> {new_key} = {new_value}")
    return out, changes


def update_keyed_sections(
    lines: list[str],
    old_key: str,
    new_key: str,
    old_entry: str,
    new_entry: str,
) -> tuple[list[str], list[str]]:
    out = list(lines)
    changes: list[str] = []
    for section in ("project_env", "cargo_run", "examples"):
        for _, key, _ in kv_lines(out, section):
            if key == new_key and old_key != new_key:
                raise ValueError(f"[{section}] already contains {new_key}")
        for index, key, value in kv_lines(out, section):
            new_line_key = new_key if key == old_key else key
            new_value = body_path_replace(value, old_entry, new_entry)
            if new_line_key != key or new_value != value:
                out[index] = replace_kv_line(out[index], new_line_key, new_value)
                changes.append(f"[{section}] {key} -> {new_line_key}")
    return out, changes


def planned_moves(history_dir: Path, old_key: str, new_key: str) -> list[PlannedMove]:
    pending_dir = history_dir / ".pending"
    failures_dir = history_dir / ".failures"
    raw_moves = [
        (history_dir / f"{old_key}.jsonl", history_dir / f"{new_key}.jsonl", "history"),
        (pending_dir / f"{old_key}.json", pending_dir / f"{new_key}.json", "pending"),
        (
            pending_dir / f"{old_key}.json.lock",
            pending_dir / f"{new_key}.json.lock",
            "pending lock",
        ),
    ]
    moves = [
        PlannedMove(old=old, new=new, label=label, exists=old.exists())
        for old, new, label in raw_moves
    ]
    if failures_dir.is_dir():
        for old in sorted(failures_dir.glob(f"*_{old_key}.md")):
            new = old.with_name(old.name.removesuffix(f"_{old_key}.md") + f"_{new_key}.md")
            moves.append(
                PlannedMove(old=old, new=new, label="failure log", exists=old.exists())
            )
    return moves


def ensure_no_move_collisions(moves: list[PlannedMove]) -> None:
    for move in moves:
        if move.old == move.new or not move.exists:
            continue
        if move.new.exists():
            raise ValueError(f"{move.label} target already exists: {move.new}")


def marker_updates(rust_dir: Path, old_key: str) -> list[PlannedMarker]:
    markers: list[PlannedMarker] = []
    for marker in sorted(rust_dir.glob("*_style_fix/.clean-fix-project")):
        try:
            key = marker.read_text().strip()
        except OSError:
            continue
        if key == old_key:
            markers.append(PlannedMarker(marker))
    return markers


def pending_json_path(moves: list[PlannedMove], old_key: str, new_key: str) -> Path | None:
    for move in moves:
        if move.label == "pending":
            if move.exists:
                return move.new
            if move.new.exists() and old_key == new_key:
                return move.new
    return None


def pending_root_would_change(path: Path, new_root: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = cast(object, json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    data = cast(dict[str, object], payload)
    return data.get("project_root") != str(new_root)


def pending_root_would_change_after_move(
    moves: list[PlannedMove], new_root: Path
) -> bool:
    for move in moves:
        if move.label != "pending":
            continue
        path = move.old if move.exists else move.new
        return pending_root_would_change(path, new_root)
    return False


def build_plan(args: CliArgs) -> Plan:
    lines = args.conf.read_text().splitlines()
    old_project = find_old_project(lines, args.old, args.rust_dir)
    new_project = resolve_project(args.new, args.rust_dir)
    ensure_no_project_collision(project_entries(lines), old_project, new_project)

    out, changes = replace_project_entry(lines, old_project, new_project)
    out, build_changes = replace_build_entries(
        out, old_project.entry, old_project.key, new_project.entry
    )
    changes.extend(build_changes)
    out, active_changes = update_active_checkout(out, old_project.entry, new_project.entry)
    changes.extend(active_changes)
    out, keyed_changes = update_keyed_sections(
        out,
        old_project.key,
        new_project.key,
        old_project.entry,
        new_project.entry,
    )
    changes.extend(keyed_changes)

    moves = planned_moves(args.history_dir, old_project.key, new_project.key)
    ensure_no_move_collisions(moves)
    markers = marker_updates(args.rust_dir, old_project.key)
    pending_path = pending_json_path(moves, old_project.key, new_project.key)
    pending_changed = pending_root_would_change_after_move(moves, new_project.target)

    return Plan(
        old_entry=old_project.entry,
        old_key=old_project.key,
        new_project=new_project,
        config_lines=out,
        config_changes=changes,
        moves=moves,
        marker_updates=markers,
        pending_path=pending_path,
        pending_project_root_changed=pending_changed,
    )


def move_files(moves: list[PlannedMove]) -> list[str]:
    moved: list[str] = []
    for move in moves:
        if move.old == move.new or not move.exists:
            continue
        _ = move.new.parent.mkdir(parents=True, exist_ok=True)
        _ = move.old.rename(move.new)
        moved.append(f"{move.label}: {move.old} -> {move.new}")
    return moved


def update_pending_project_root(path: Path | None, new_root: Path) -> bool:
    if path is None or not path.exists():
        return False
    payload = cast(object, json.loads(path.read_text()))
    if not isinstance(payload, dict):
        return False
    data = cast(dict[str, object], payload)
    if data.get("project_root") == str(new_root):
        return False
    data["project_root"] = str(new_root)
    _ = path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True


def update_markers(markers: list[PlannedMarker], new_key: str) -> list[str]:
    updated: list[str] = []
    for marker in markers:
        _ = marker.path.write_text(f"{new_key}\n")
        updated.append(str(marker.path))
    return updated


def print_plan(plan: Plan, *, dry_run: bool, applied: bool) -> None:
    print(f"Project: {plan.old_key} -> {plan.new_project.key}")
    print(f"Entry: {plan.old_entry} -> {plan.new_project.entry}")
    if dry_run:
        print("Dry run; no file written.")
    elif applied:
        print("Applied.")
    else:
        print("No changes.")

    for change in plan.config_changes:
        print(change)
    for move in plan.moves:
        if move.old == move.new:
            continue
        status: RenameStatus = "change" if move.exists else "noop"
        print(f"{move.label}: {status} {move.old} -> {move.new}")
    if plan.pending_project_root_changed:
        print(f"pending project_root: {plan.pending_path} -> {plan.new_project.target}")
    for marker in plan.marker_updates:
        print(f"marker: {marker.path} -> {plan.new_project.key}")


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(description="Rename a clean-fix project key.")
    _ = parser.add_argument("old", help="current clean-fix project key or [projects] entry")
    _ = parser.add_argument("new", help="new project name/path under ~/rust, or absolute path")
    _ = parser.add_argument("--conf", type=Path, default=DEFAULT_CONF)
    _ = parser.add_argument("--rust-dir", type=Path, default=DEFAULT_RUST_DIR)
    _ = parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    _ = parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv, namespace=CliArgs())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    changed = bool(
        plan.config_changes
        or any(move.exists and move.old != move.new for move in plan.moves)
        or plan.pending_project_root_changed
        or plan.marker_updates
    )
    if args.dry_run or not changed:
        print_plan(plan, dry_run=args.dry_run, applied=False)
        return 0

    _ = args.conf.write_text("\n".join(plan.config_lines) + "\n")
    _ = move_files(plan.moves)
    _ = update_pending_project_root(plan.pending_path, plan.new_project.target)
    _ = update_markers(plan.marker_updates, plan.new_project.key)
    print_plan(plan, dry_run=False, applied=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
