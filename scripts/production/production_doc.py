"""Read fields and promotion destinations from staff-owned production documents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone
from scripts.production import staff, showrunners


WRAPPED = "> **Status: PRODUCTION — wrapped.**"


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


@dataclass(frozen=True)
class ProductionDocument:
    doc: Path
    name: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo

    def for_owner(self, owner: staff.StaffRow) -> Production:
        return Production(self.doc, self.name, self.merge_branch, self.checkout, self.log, self.zone, owner)


@dataclass(frozen=True)
class Production(ProductionDocument):
    owner: staff.StaffRow


def cell_value(value: str) -> str:
    """Read the value before its commentary."""
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


class FieldPresent(NamedTuple):
    text: str


class FieldAbsent(NamedTuple):
    pass


class NoPromotion(NamedTuple):
    pass


class NoMac(NamedTuple):
    pass


class MacCheckout(NamedTuple):
    path: PurePosixPath
    """As written in the doc; a leading ~ is the Mac's home, so the Mac's shell expands it."""


class PromoteTo(NamedTuple):
    checkouts: tuple[Path, ...]
    mac: NoMac | MacCheckout


def optional_field(lines: list[str], field: str) -> FieldPresent | FieldAbsent:
    prefix = f"- **{field}:**"
    for line in lines:
        if line.startswith(prefix):
            return FieldPresent(line[len(prefix):].removeprefix(" "))
    return FieldAbsent()


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without its commentary."""
    present = optional_field(lines, field)
    value = cell_value(present.text) if isinstance(present, FieldPresent) else ""
    if not value:
        raise Refusal(f"production doc lacks {field}")
    return value


def read_production(reference: str | staff.StaffRow, record: staff.StaffSnapshot | None = None) -> Production:
    """Read the document from the showrunner row."""
    try:
        row = reference if isinstance(reference, staff.StaffRow) else showrunners.owner_for(reference, record)
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            raise Refusal(f"{row.name}: no production document")
        document = read_new_production(row.doc.path, row.name)
        return document.for_owner(row)
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def read_new_production(doc: Path, name: str) -> ProductionDocument:
    """Read a new production document with its specified staff name."""
    try:
        doc = doc.expanduser().resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        return ProductionDocument(doc, name, merge_branch, checkout, checkout / log, user_zone())
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def promotion_from_doc(lines: list[str]) -> NoPromotion | PromoteTo:
    """Read the production's promotion destinations in their declared order."""
    field = optional_field(lines, "Promote")
    if isinstance(field, FieldAbsent):
        return NoPromotion()
    promote_raw = field.text.split(" — ", 1)[0].replace("`", "")
    if promote_raw.strip().casefold() == "none":
        return NoPromotion()
    local: list[Path] = []
    mac: NoMac | MacCheckout = NoMac()
    for item in promote_raw.split(","):
        item = item.strip()
        if item.startswith("mac "):
            mac = MacCheckout(PurePosixPath(item[4:].strip()))
        elif item:
            local.append(Path(item).expanduser())
    return PromoteTo(tuple(local), mac) if local or isinstance(mac, MacCheckout) else NoPromotion()


