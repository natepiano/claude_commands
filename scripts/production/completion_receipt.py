"""Completion evidence belongs to one living staff member's incarnation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.production_doc import Refusal


@dataclass(frozen=True)
class OwnedReceipt:
    owner: staff.StaffRow
    fields: dict[str, object]


@dataclass(frozen=True)
class UnpublishedReceipt:
    """A backend was observed, but its native staff row was not published."""

    identity: staff.StaffId
    fields: dict[str, object]


@dataclass(frozen=True)
class NoReceipt:
    pass


def read_receipt(path: Path, record: staff.StaffSnapshot) -> OwnedReceipt | NoReceipt:
    """A retired owner's receipt cannot complete a later incarnation's request."""
    receipt = read_launch_receipt(path, record)
    return receipt if isinstance(receipt, OwnedReceipt) else NoReceipt()


def read_launch_receipt(path: Path, record: staff.StaffSnapshot) -> OwnedReceipt | UnpublishedReceipt | NoReceipt:
    """An unpublished native launch requires fresh runtime proof before use."""
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoReceipt()
    if not isinstance(value, dict):
        raise Refusal(f"{path}: receipt must be an object")
    fields = cast(dict[str, object], value)
    owner = fields.get("owner")
    row = record.by_id(staff.StaffId(owner)) if isinstance(owner, str) else None
    if row is not None:
        return OwnedReceipt(row, fields)
    if isinstance(owner, str) and owner == fields.get("session_id"):
        try:
            return UnpublishedReceipt(staff.native_id(owner), fields)
        except staff.StaffRefused:
            pass
    return NoReceipt()


def write_receipt(path: Path, owner: staff.StaffId, fields: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({**fields, "owner": owner}, sort_keys=True) + "\n")
