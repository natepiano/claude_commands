#!/usr/bin/env python3
"""Move a showrunner's saved report state from one unit key to another."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

import tidy_wrapped

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import atomic_file

if TYPE_CHECKING:
    from scripts.production.staff_sources import MemberSelector

JsonObject = dict[str, object]


class RenameRefused(ValueError):
    """A state store cannot be renamed safely."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


def read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise RenameRefused(f"cannot parse {path}: {error}") from error


def _fields(path: Path) -> JsonObject:
    value = read_json(path)
    if not isinstance(value, dict):
        raise RenameRefused(f"{path}: expected a JSON object")
    return cast(JsonObject, value)


def unfinished(path: Path) -> bool:
    """Whether a rename journal exists and its rename has not finished."""
    return path.exists() and _fields(path).get("done") is not True


def production_journal(slug: str) -> Path:
    return tidy_wrapped.owned_paths(slug)[-1] / "rename.json"


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def migrate_staff_ids(path: Path, fields: JsonObject,
                      members: dict[str, tuple[MemberSelector, ...]]) -> JsonObject:
    """Bind a legacy journal once, using members at either side of its saved changes."""
    missing = {name: selectors for name, selectors in members.items() if name not in fields}
    if not missing:
        return fields
    from scripts.production import staff, staff_sources

    record = staff.snapshot()
    migrated = fields.copy()
    for name, selectors in missing.items():
        for selector in selectors:
            found = staff_sources.member(selector, record)
            if isinstance(found, staff_sources.MemberFound):
                migrated[name] = found.row.id
                break
        else:
            raise staff.StaffRefused(f"{path}: no matching staff member for legacy {name}")
    write_json(path, migrated)
    return migrated


def _rename_json(path: Path, update: Callable[[object], bool]) -> bool:
    if not path.exists():
        return False
    value = read_json(path)
    if not update(value):
        return False
    write_json(path, value)
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


# Old and new names, the id first, rewritten as whole tokens in free text.
Words = Sequence[tuple[str, str]]


def _reworded(text: str, words: Words) -> str:
    """`text` with each old name as a whole token taking its new name: `per-unit` stays."""
    for old, new in words:
        text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])", new, text)
    return text


def _reword_items(items: object, fields: tuple[str, ...], words: Words) -> bool:
    """Reword a list of texts, or of objects whose `fields` are text."""
    if not isinstance(items, list):
        return False
    changed = False
    entries = cast(list[object], items)
    for index, item in enumerate(entries):
        if isinstance(item, str):
            if (updated := _reworded(item, words)) != item:
                entries[index], changed = updated, True
            continue
        if not isinstance(item, dict):
            continue
        entry = cast(JsonObject, item)
        for field in fields:
            value = entry.get(field)
            if isinstance(value, str) and (updated := _reworded(value, words)) != value:
                entry[field], changed = updated, True
    return changed


def _reword_lists(value: object, words: Words) -> bool:
    """Keep held merge names current, and reword the showrunner's pending notes for readability."""
    if not isinstance(value, dict):
        return False
    state = cast(JsonObject, value)
    changed = _reword_items(state.get("merges_held"), (), words)
    changed = _reword_items(state.get("next"), (), words) | changed
    return _reword_reminders(state.get("open_for_user"), words) | changed


def _rename_showrunner_state(value: object, words: Words) -> bool:
    """The unit rows take the new id, and the live lists the new names."""
    changed = bool(words) and _rename_unit_rows(value, words[0][0], words[0][1])
    return _reword_lists(value, words) | changed


def reword_showrunner_lists(path: Path, words: Words) -> bool:
    """Another production's showrunner state, such as the parent's: the unit is not one of its rows,
    but its live lists can name the unit."""
    try:
        return _rename_json(path, lambda value: _reword_lists(value, words))
    except (OSError, ValueError) as error:
        raise RenameRefused(str(error)) from error


def _reword_reminders(value: object, words: Words) -> bool:
    """Reword reminders without changing questions or their fixed asker references."""
    if not isinstance(value, list):
        return False
    reminders: list[object] = [cast(object, entry) for entry in cast(list[object], value)
                 if not isinstance(entry, dict) or "question" not in entry]
    changed = _reword_items(reminders, ("text",), words)
    replacements = iter(reminders)
    entries = cast(list[object], value)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "question" not in entry:
            entries[index] = next(replacements)
    return changed


def rename_outstanding(slug: str, words: Words) -> bool:
    """Reword reminder text; question records remain unchanged during a rename."""
    path = tidy_wrapped.outstanding_path(slug)
    try:
        return _rename_json(path, lambda value: _reword_reminders(value, words))
    except (OSError, ValueError) as error:
        raise RenameRefused(str(error)) from error


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


def write_text(path: Path, text: str) -> None:
    """Publish state through the shared durable file writer."""
    atomic_file.write_text_atomic(path, text)


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
    write_text(path, "".join(output))
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
    write_text(path, "".join(output))
    return True


# The showrunner state files (showrunners.session_state_dir) _rename_scratch rewrites; verify reads them for a stale unit id.
SCRATCH_FILES = ("dailies_input_state/eta_seen.json", "unit_status/decisions_seen", "unit_status/blocks_open",
                 "showrunner_state.json", "dailies_judgment.json", "dailies_state.json")


def _rename_scratch(old: str, new: str, scratch: Path, words: Words) -> list[str]:
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
    if _rename_json(showrunner, lambda value: _rename_showrunner_state(value, [(old, new), *words])):
        changed.append("showrunner_state.json")
    judgment = scratch / "dailies_judgment.json"
    if _rename_json(judgment, lambda value: _rename_unit_rows(value, old, new)):
        changed.append("dailies_judgment.json")
    rendered = scratch / "dailies_state.json"
    if _rename_json(rendered, lambda value: _rename_top_level_key(value, old, new)):
        changed.append("dailies_state.json")
    return changed


def rename_scratch(old: str, new: str, scratch: Path, words: Words = ()) -> list[str]:
    """Move a showrunner's saved report state from one unit key to another. `words` are more old and
    new names, such as the branch and the worktree, that free text takes too."""
    try:
        return _rename_scratch(old, new, scratch, words)
    except RenameRefused:
        raise
    except (OSError, ValueError) as error:
        raise RenameRefused(str(error)) from error
