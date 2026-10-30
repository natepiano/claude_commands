"""Completion evidence belongs to one living staff member's incarnation."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff

# Production tools still share this sibling module's exception identity.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_units import Refusal


@dataclass(frozen=True)
class OwnedReceipt:
    owner: staff.StaffRow
    fields: dict[str, object]


@dataclass(frozen=True)
class NoReceipt:
    pass


def read_receipt(path: Path, record: staff.StaffSnapshot) -> OwnedReceipt | NoReceipt:
    """A retired owner's receipt cannot complete a later incarnation's request."""
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoReceipt()
    if not isinstance(value, dict):
        raise Refusal(f"{path}: receipt must be an object")
    fields = cast(dict[str, object], value)
    owner = fields.get("owner")
    row = record.by_id(staff.StaffId(owner)) if isinstance(owner, str) else None
    return NoReceipt() if row is None else OwnedReceipt(row, fields)


def write_receipt(path: Path, owner: staff.StaffId, fields: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({**fields, "owner": owner}, sort_keys=True) + "\n")
