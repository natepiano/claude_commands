"""Resolve production store owners through the shared staff join."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, staff_sources
from scripts.production import unit_lookup


def document_id(document: Path, *, record: staff.StaffSnapshot | None = None) -> staff.StaffId:
    """Read a document owner's fixed id from staff."""
    record = staff.snapshot() if record is None else record
    return unit_lookup.doc_owner(document, record).id


def showrunner_id(reference: str, *, record: staff.StaffSnapshot | None = None) -> staff.StaffId:
    """Return a fixed id from staff; refuse missing ownership evidence."""
    record = staff.snapshot() if record is None else record
    row = record.by_member_reference(reference)
    if row is None:
        found = staff_sources.member(staff_sources.ShowrunnerMember(reference), record)
        if isinstance(found, staff_sources.NoSuchMember):
            raise staff.StaffRefused(f"{reference}: showrunner ownership evidence is unavailable")
        row = found.row
    if row.kind != "showrunner":
        raise staff.StaffRefused(f"{reference}: expected a showrunner staff row")
    return row.id
