"""Read the units and workers that the cargo-handler tiles show, from one census."""
from __future__ import annotations

import json
import subprocess
from typing import Literal, NamedTuple, cast

from scripts.lib import conf
from scripts.lib.process_failure import failure_detail
from scripts.production import staff

COMMAND = ("cargo-handler", "census", "--json")
SCHEMA = 2

type RunState = Literal["running", "finished_queued", "missing"]
RUN_STATES: tuple[RunState, ...] = ("running", "finished_queued", "missing")


class CensusMachine(NamedTuple):
    name: str
    reachable: bool


class CensusSession(NamedTuple):
    """A session that a handler tile shows. `status` is None when the accounts command has no entry."""

    session: str
    status: str | None


class CensusRun(NamedTuple):
    """A detached run of a worker. `started_at` is None when the census cannot read the start."""

    label: str
    started_at: float | None
    state: RunState


class CensusPass(NamedTuple):
    role: str
    phase: str


class NoPass(NamedTuple):
    """The worker has no open pass."""


class CensusWorker(NamedTuple):
    staff_id: staff.StaffId
    name: str
    parent_staff_id: staff.StaffId
    status: str | None
    assignment: CensusPass | NoPass
    runs: tuple[CensusRun, ...]


class Census(NamedTuple):
    machines: tuple[CensusMachine, ...]
    sessions: tuple[CensusSession, ...]
    workers: tuple[CensusWorker, ...]


class CensusFailed(NamedTuple):
    reason: str


class _BadField(Exception):
    pass


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _BadField(f"{where} is not an object")
    return cast(dict[str, object], value)


def _list(fields: dict[str, object], key: str, where: str) -> list[object]:
    value = fields.get(key)
    if not isinstance(value, list):
        raise _BadField(f"{where}.{key} is not a list")
    return cast(list[object], value)


def _text(fields: dict[str, object], key: str, where: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise _BadField(f"{where}.{key} is not text")
    return value


def _optional_text(fields: dict[str, object], key: str, where: str) -> str | None:
    value = fields.get(key)
    if value is not None and not isinstance(value, str):
        raise _BadField(f"{where}.{key} is not text or null")
    return value


def _run(value: object, where: str) -> CensusRun:
    fields = _object(value, where)
    state = fields.get("state")
    if state not in RUN_STATES:
        raise _BadField(f"{where}.state is not one of {', '.join(RUN_STATES)}")
    started = fields.get("started_at")
    if started is not None and (isinstance(started, bool) or not isinstance(started, int | float)):
        raise _BadField(f"{where}.started_at is not a number or null")
    return CensusRun(_text(fields, "label", where), None if started is None else float(started), state)


def _assignment(value: object, where: str) -> CensusPass | NoPass:
    if value is None:
        return NoPass()
    fields = _object(value, where)
    return CensusPass(_text(fields, "role", where), _text(fields, "phase", where))


def _session(value: object, where: str) -> CensusSession:
    fields = _object(value, where)
    return CensusSession(_text(fields, "session", where), _optional_text(fields, "status", where))


def _worker(value: object, where: str) -> CensusWorker:
    fields = _object(value, where)
    return CensusWorker(staff.StaffId(_text(fields, "staff_id", where)), _text(fields, "name", where),
                        staff.StaffId(_text(fields, "parent_staff_id", where)), _optional_text(fields, "status", where),
                        _assignment(fields.get("pass"), f"{where}.pass"),
                        tuple(_run(run, f"{where}.runs[{index}]")
                              for index, run in enumerate(_list(fields, "runs", where))))


def _machine(value: object, where: str) -> CensusMachine:
    fields = _object(value, where)
    reachable = fields.get("reachable")
    if not isinstance(reachable, bool):
        raise _BadField(f"{where}.reachable is not true or false")
    return CensusMachine(_text(fields, "name", where), reachable)


def parse(text: str) -> Census | CensusFailed:
    """Parse census schema 2."""
    try:
        document = _object(json.loads(text), "census")  # pyright: ignore[reportAny]
        schema = document.get("schema")
        if isinstance(schema, bool) or schema != SCHEMA:
            return CensusFailed(f"census schema {schema!r} is not {SCHEMA}")
        return Census(
            tuple(_machine(item, f"machines[{index}]") for index, item in enumerate(_list(document, "machines", "census"))),
            tuple(_session(item, f"sessions[{index}]")
                  for index, item in enumerate(_list(document, "sessions", "census"))),
            tuple(_worker(item, f"workers[{index}]") for index, item in enumerate(_list(document, "workers", "census"))))
    except json.JSONDecodeError as error:
        return CensusFailed(f"census output is not JSON: {error}")
    except _BadField as error:
        return CensusFailed(f"census: {error}")


def read() -> Census | CensusFailed:
    """Run one census within `units_census_seconds`."""
    try:
        seconds = conf.read_or_repository("production").number("units_census_seconds")
    except conf.Unusable as error:
        return CensusFailed(f"census: units_census_seconds: {error}")
    try:
        done = subprocess.run(COMMAND, capture_output=True, text=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        return CensusFailed(f"census took longer than {seconds:g} s")
    except (OSError, UnicodeError) as error:
        return CensusFailed(f"census: {error}")
    if done.returncode:
        return CensusFailed(f"census exited {done.returncode}: {' '.join(failure_detail(done).splitlines())}")
    return parse(done.stdout)
