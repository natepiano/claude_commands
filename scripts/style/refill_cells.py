"""Maintain coverage cells without changing judgment decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, cast

from scripts.style.refill_facts import RefillFacts
from scripts.style.refill_inputs import input_identity

CellState = Literal["current", "stale", "open", "awaiting merge"]


JUDGMENT_READER_VERSION = 2


@dataclass(frozen=True)
class CellDependencies:
    """The source rows and immutable objects that supplied a cell result."""

    edges: tuple[tuple[str, str, str], ...]


class JudgmentCells:
    """Replace affected coverage rows within one catalog observation."""

    def __init__(self, facts: RefillFacts, view: str) -> None:
        self.facts: RefillFacts = facts
        self.connection: sqlite3.Connection = facts.connection
        self.view: str = view
        self.used: set[tuple[str, str]] = set()

    def result(self, rule: str, path: str, identity: str, available: Callable[[tuple[str, ...]], bool],
               calculate: Callable[[], tuple[CellState, str]],
               dependencies: Callable[[], CellDependencies], *, crate: str, family: str | None,
               tier: str) -> tuple[CellState, str]:
        self.used.add((rule, path))
        key = json.dumps((self.facts.catalog, rule, path, self.view), separators=(",", ":"))
        stored = cast(tuple[str, CellState, str, int, str] | None, self.connection.execute(
            "SELECT input_identity,state,verdict,dirty_version,objects FROM refill_cells "
            + "WHERE consumer_key=?", (key,)).fetchone())
        if stored is not None and stored[0] == identity and not stored[3]:
            objects = tuple(cast(list[str], json.loads(stored[4])))
            if available(objects):
                return stored[1], stored[2]
        versions = self.facts.source_versions()
        result = calculate()
        edges = dependencies().edges
        objects = tuple(sorted({source for kind, source, _ in edges if kind == "git_object"}))
        # A newer source mutation retains its dirty work.
        verified = available(objects)
        with self.facts.publication():
            if versions == self.facts.source_versions() and verified:
                _ = self.connection.execute(
                    "INSERT OR REPLACE INTO refill_cells "
                    + "(consumer_key,catalog,rule,path,view,input_identity,state,verdict,dirty_version,objects,crate,family,tier) "
                    + "VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)",
                    (key, self.facts.catalog, rule, path, self.view, identity, *result, json.dumps(objects), crate, family, tier))
                self.facts.dependencies("refill_cells", key, edges)
        return result

    def finish(self, rules: Iterable[str], crate: str | None, *, all_rules: bool = False) -> None:
        """Remove vanished membership only within the requested coverage domain."""
        with self.facts.publication():
            observed = tuple(rules)
            if all_rules:
                observed = tuple(rule for (rule,) in cast(list[tuple[str]], self.connection.execute(
                    "SELECT DISTINCT rule FROM refill_cells WHERE catalog=? AND view=?",
                    (self.facts.catalog, self.view)).fetchall()))
            for rule in observed:
                rows = cast(list[tuple[str, str]], self.connection.execute(
                    "SELECT consumer_key,path FROM refill_cells WHERE catalog=? AND view=? AND rule=? "
                    + "AND (? IS NULL OR crate=?)", (self.facts.catalog, self.view, rule, crate, crate)).fetchall())
                for key, path in rows:
                    if (rule, path) not in self.used:
                        _ = self.connection.execute("DELETE FROM refill_cells WHERE consumer_key=?", (key,))
                        _ = self.connection.execute(
                            "DELETE FROM refill_dependencies WHERE consumer_table='refill_cells' AND consumer_key=?", (key,))


def cell_identity(inputs: object) -> str:
    return input_identity((JUDGMENT_READER_VERSION, inputs))
