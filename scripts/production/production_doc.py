"""Read names, fields and promotion destinations from production documents."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone


WRAPPED = "> **Status: PRODUCTION — wrapped.**"


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


class Production(NamedTuple):
    doc: Path
    slug: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo


def cell_value(value: str) -> str:
    """Read the value before its commentary."""
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


class ProductionName(NamedTuple):
    value: str


class ProductionNameNotRecorded(NamedTuple):
    reason: str


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


def production_name(lines: list[str]) -> ProductionName | ProductionNameNotRecorded:
    for line in lines:
        if line.startswith("# Production — "):
            return ProductionName(line.removeprefix("# Production — ").strip())
    return ProductionNameNotRecorded("production doc lacks its name")


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


def read_production(path: Path) -> Production:
    """Read the production fields shared with merge_checkpoint.py."""
    try:
        doc = path.resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        slug = doc.name.removesuffix("-production.md")
        if slug == doc.name:
            raise Refusal("production doc name must end in -production.md")
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        return Production(doc, slug, merge_branch, checkout, checkout / log, user_zone())
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


