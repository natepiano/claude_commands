"""Resolve production store owners through the shared staff join."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import session_staff, staff
from scripts.shutdown.remote import local_machine


def document_id(document: Path, *, record: staff.StaffSnapshot | None = None) -> staff.StaffId:
    """Read a document owner's fixed id from staff."""
    record = staff.snapshot() if record is None else record
    row = record.by_doc(local_machine(), document)
    if row is None or row.kind != "showrunner":
        raise staff.StaffRefused(f"{document}: showrunner ownership evidence is unavailable")
    return row.id


def showrunner_id(reference: str, *, record: staff.StaffSnapshot | None = None,
                  sources: session_staff.MemberSources | None = None) -> staff.StaffId:
    """Return a fixed id from staff; refuse missing ownership evidence."""
    record = staff.snapshot() if record is None else record
    row = record.by_member_reference(reference)
    if row is None:
        sources = session_staff.member_sources() if sources is None else sources
        found = sources.member(session_staff.ShowrunnerMember(reference), record)
        if isinstance(found, session_staff.NoSuchMember):
            raise staff.StaffRefused(f"{reference}: showrunner ownership evidence is unavailable")
        row = found.row
    if row.kind != "showrunner":
        raise staff.StaffRefused(f"{reference}: expected a showrunner staff row")
    return row.id
