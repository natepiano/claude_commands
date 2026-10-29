"""Read optional fields and promotion destinations from production documents."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import NamedTuple


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


