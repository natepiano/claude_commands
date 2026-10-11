#!/usr/bin/env python3
"""Move a showrunner's saved report state from one unit key to another."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

JsonObject = dict[str, object]


class RenameRefused(ValueError):
    """A state store cannot be renamed safely."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


def _read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise RenameRefused(f"cannot parse {path}: {error}") from error


def _write_json(path: Path, value: object) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            _ = temporary.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _rename_json(path: Path, update: Callable[[object], bool]) -> bool:
    if not path.exists():
        return False
    value = _read_json(path)
    if not update(value):
        return False
    _write_json(path, value)
    return True


def _rename_eta_keys(value: object, old: str, new: str) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast(JsonObject, value)
    prefix = f"{old}|"
    replacements = [(key, f"{new}|{key[len(prefix):]}") for key in fields if key.startswith(prefix)]
    if not replacements:
        return False
    for source, destination in replacements:
        entry = fields.pop(source)
        if destination not in fields:
            fields[destination] = entry
    return True


def _rename_unit_rows(value: object, old: str, new: str) -> bool:
    if not isinstance(value, dict):
        return False
    units = cast(JsonObject, value).get("units")
    if not isinstance(units, list):
        return False
    rows = cast(list[object], units)
    has_new = any(isinstance(row, dict) and cast(JsonObject, row).get("unit") == new for row in rows)
    changed = False
    updated: list[object] = []
    for row in rows:
        if not isinstance(row, dict) or cast(JsonObject, row).get("unit") != old:
            updated.append(cast(object, row))
            continue
        changed = True
        if not has_new:
            cast(JsonObject, row)["unit"] = new
            updated.append(cast(object, row))
    if changed:
        cast(JsonObject, value)["units"] = updated
    return changed


def _rename_top_level_key(value: object, old: str, new: str) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast(JsonObject, value)
    if old not in fields:
        return False
    entry = fields.pop(old)
    if new not in fields:
        fields[new] = entry
    return True


def _write_lines(path: Path, lines: list[str]) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            _ = temporary.write("".join(lines))
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _rename_lines(path: Path, old_prefix: str, new_prefix: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return False
    renamed = {new_prefix + line[len(old_prefix):] for line in lines if line.startswith(old_prefix)}
    if not renamed:
        return False
    existing = {line for line in lines if line.startswith(new_prefix)}
    output: list[str] = []
    emitted = set(existing)
    for line in lines:
        if not line.startswith(old_prefix):
            output.append(line)
            continue
        replacement = new_prefix + line[len(old_prefix):]
        if replacement not in emitted:
            output.append(replacement)
            emitted.add(replacement)
    _write_lines(path, output)
    return True


def _rename_blocks_open(path: Path, old: str, new: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return False
    old_prefix = f"{old}\t"
    if not any(line.startswith(old_prefix) for line in lines):
        return False
    new_prefix = f"{new}\t"
    if any(line.startswith(new_prefix) for line in lines):
        output = [line for line in lines if not line.startswith(old_prefix)]
    else:
        output = [new_prefix + line[len(old_prefix):] if line.startswith(old_prefix) else line for line in lines]
    _write_lines(path, output)
    return True


def _rename_scratch(old: str, new: str, scratch: Path) -> list[str]:
    changed: list[str] = []
    eta = scratch / "dailies_input_state" / "eta_seen.json"
    if _rename_json(eta, lambda value: _rename_eta_keys(value, old, new)):
        changed.append("dailies_input_state/eta_seen.json")

    decisions = scratch / "unit_status" / "decisions_seen"
    if _rename_lines(decisions, f"{old}|", f"{new}|"):
        changed.append("unit_status/decisions_seen")
    blocks = scratch / "unit_status" / "blocks_open"
    if _rename_blocks_open(blocks, old, new):
        changed.append("unit_status/blocks_open")

    showrunner = scratch / "showrunner_state.json"
    if _rename_json(showrunner, lambda value: _rename_unit_rows(value, old, new)):
        changed.append("showrunner_state.json")
    judgment = scratch / "dailies_judgment.json"
    if _rename_json(judgment, lambda value: _rename_unit_rows(value, old, new)):
        changed.append("dailies_judgment.json")
    rendered = scratch / "dailies_state.json"
    if _rename_json(rendered, lambda value: _rename_top_level_key(value, old, new)):
        changed.append("dailies_state.json")
    return changed


def rename_scratch(old: str, new: str, scratch: Path) -> list[str]:
    """Move a showrunner's saved report state from one unit key to another."""
    try:
        return _rename_scratch(old, new, scratch)
    except RenameRefused:
        raise
    except (OSError, ValueError) as error:
        raise RenameRefused(str(error)) from error
