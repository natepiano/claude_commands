"""Supply staff parentage at the API boundary for synthetic shutdown sessions."""
from __future__ import annotations

from typing import Literal
from contextlib import contextmanager
from collections.abc import Generator
from unittest.mock import patch

from scripts.production import staff, staff_sources
from session_types import LegacyTmuxUnitHost, UnitHost, UnitPlan

PARENTS: dict[str, staff_sources.UnitProduction] = {}


def parent(unit: str, _session_id: str = "") -> staff_sources.UnitProduction:
    try:
        return PARENTS[unit]
    except KeyError:
        raise staff.StaffUnreadable(f"{unit}: no unique staff entry") from None


@contextmanager
def isolated_parents() -> Generator[None]:
    """Keep synthetic staff answers within one test and at the shared read API."""
    with patch.dict(PARENTS, clear=True), patch.object(staff_sources, "unit_parent", side_effect=parent):
        yield


def unit_host(*, kind: Literal["unit"], production: str, unit: str, doc: str,
              session_name: str, session_host: Literal["herdr"], plan: UnitPlan) -> UnitHost:
    PARENTS[unit] = staff_sources.UnitProduction(production, doc)
    return UnitHost(kind=kind, unit=unit, session_name=session_name, session_host=session_host, plan=plan)


def legacy_unit_host(*, kind: Literal["legacy tmux unit"], production: str, unit: str,
                     doc: str, session_name: str) -> LegacyTmuxUnitHost:
    PARENTS[unit] = staff_sources.UnitProduction(production, doc)
    return LegacyTmuxUnitHost(kind=kind, unit=unit, session_name=session_name)
