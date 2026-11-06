"""The 10:46 queue, stored as admission inputs rather than renderer decisions."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from typing import cast, override
from zoneinfo import ZoneInfo

from scripts.buildlog import store
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture, fake_memory_readings, write_config_fixture
from scripts.message.unit_labels import UnitLabels
from scripts.message import roster
from scripts.build_queue import head_facts, queue_view
from scripts.build_queue.queue_lines import Line, plain

NOW = datetime(2026, 10, 30, 14, 46, 12, tzinfo=UTC).timestamp()
ZONE = ZoneInfo("America/New_York")
ASKING_SESSION = "id-work-lead"
GOLDEN = Path(__file__).with_name("golden")

# production, unit, session, step, need in GiB, minutes waited, folder
STEPS = (
    ("tool-based-ui", "cables", "work-lead", "nextest", 11.5, 6, "/tmp/work"),
    ("tool-based-ui", "cables", "work-lead", "nextest", 11.5, 6, "/tmp/measure-repair2"),
    ("tool-based-ui", "hana-migration", "hana-migration", "mend", 7.6, 39, "/tmp/hana_catalyst-hana-migration"),
    ("cargo-liner", "berth", "cargo-liner-berth-parallel-impl", "clippy", 2.9, 37, "/home/natepiano/rust/cargo-liner/repo"),
    ("tool-based-ui", "cables", "work-pan", "mend", 7.6, 36, "/tmp/tree"),
    ("tool-based-ui", "lfo", "hana_catalyst-lfo-impl6", "nextest", 10.8, 35, "/tmp/lfo-repo"),
    ("cargo-liner-style", "cargo-liner-style-modules", "cargo-liner-style-modules", "doc", 3.5, 35, "/tmp/style-modules"),
    ("cargo-liner-style", "—", "cargo-liner-style", "nextest", 1.8, 33, "/tmp/cargo-liner-style"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "mend", 7.6, 32, "/tmp/frame-time"),
    ("tool-based-ui", "—", "hana", "clippy", 6.8, 31, "/tmp/hana_catalyst"),
    ("cargo-liner-style", "cargo-liner-style-types", "cargo-liner-style-types", "doc", 3.5, 31, "/tmp/style-types"),
    ("tool-based-ui", "geometry-material", "organon-w89", "nextest", 1.0, 30, "/tmp/base86"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-portal", "doc", 4.4, 30, "/tmp/portal"),
    ("cargo-liner", "handler", "handler", "clippy", 3.0, 30, "/tmp/handler"),
    ("tool-based-ui", "demo", "tool-based-ui-demo-impl", "clippy", 6.8, 27, "/tmp/demo"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-fix2", "doc", 4.4, 24, "/tmp/trunk-fix2"),
    ("cargo-liner", "port", "cargo-liner-port-impl", "mend", 3.2, 24, "/tmp/cargo-liner-port"),
    ("cargo-liner", "port", "cargo-liner-port-impl", "nextest", 1.8, 24, "/tmp/cargo-liner-port"),
    ("tool-based-ui", "inspectors", "hana_catalyst-inspectors-fix2", "nextest", 11.5, 21, "/tmp/inspectors"),
    ("tool-based-ui", "demo", "tool-based-ui-demo-impl3", "mend", 7.6, 20, "/tmp/demo3"),
    ("tool-based-ui", "startup", "tool-based-ui-startup-fix2", "nextest", 10.8, 14, "/tmp/land60"),
    ("tool-based-ui", "geometry-material", "organon-w74", "clippy", 6.1, 14, "/tmp/mergeclone"),
    ("tool-based-ui", "hana-migration", "hana-migration", "nextest", 12.1, 10, "/tmp/hana_catalyst-hana-migration"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "nextest", 12.1, 10, "/tmp/frame-time"),
    ("cargo-liner", "handler", "handler", "nextest", 1.8, 9, "/tmp/handler"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "nextest", 8.5, 8, "/tmp/frame-time-second"),
    ("performance", "benchmark", "benchmark", "nextest", 12.1, 8, "/tmp/benchmark"),
    ("cargo-liner", "tile", "cargo-liner-tile-fixes-impl", "nextest", 10.8, 6, "/tmp/tile"),
    ("tool-based-ui", "lfo", "hana_catalyst-lfo-test2", "nextest", 1.0, 3, "/tmp/lfo-test"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-fix3", "nextest", 10.8, 2, "/tmp/trunk-fix3"),
    ("tool-based-ui", "fastest", "hana_catalyst-build-speed-tests-test", "clippy", 6.8, 1, "/tmp/fastest"),
    ("tool-based-ui", "assembly", "hana_catalyst-assembly-impl", "clippy", 6.8, 0, "/tmp/assembly"),
)



def write_memory_config(path: Path, **changes: str | None) -> None:
    """Write one complete, private memory configuration for admission consumers."""
    _ = write_config_fixture(path, template="build_admission")
    lines = [line for line in path.read_text().splitlines()
             if not line.startswith("peak_hold_seconds.")
             and (not line.startswith("program_first_peak_gib.")
                  or line.startswith("program_first_peak_gib.cargo="))]
    for key, value in changes.items():
        indices = [index for index, line in enumerate(lines) if line.partition("=")[0].strip() == key]
        if indices:
            index = indices[0]
            if value is None:
                del lines[index]
            else:
                lines[index] = f"{key}={value}"
        elif value is not None:
            form = "whole: 1.." if key.startswith("peak_hold_seconds.") else "range: 5e-324..1.6742321987285425e299"
            lines.extend((f"# {form}", f"{key}={value}"))
    _ = path.write_text("\n".join(lines) + "\n")




def write_lines(path: Path, lines: Sequence[bytes], *, tail: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"".join(lines) + tail
    if path.write_bytes(content) != len(content):
        raise OSError("fixture log write was incomplete")


def bump_line(at: float, *, session: str = "bumped-session", step: str = "check",
              folder: str = "/tmp/bumped", actor: str = "actor-session", old_position: int = 3) -> bytes:
    return (json.dumps({"kind": "queue_bump", "at": store.utc_iso(at), "session": session,
                        "step": step, "folder": folder, "actor": actor, "old_position": old_position},
                       separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def write_bumps(epoch: float, lines: Sequence[bytes], *, tail: bytes = b"") -> None:
    write_lines(store.host_file(store.host_name(), epoch), lines, tail=tail)


def queue_time_line(at: float, *, kind: str = "admitted", **fields: object) -> bytes:
    return (json.dumps({"kind": kind, "at": at, **fields}, separators=(",", ":")) + "\n").encode()


def token_gap_line(left_at: float, back_at: float, *, session: str = "head-session",
                   step: str = "nextest", pid: int = 123, start_time: str = "123") -> bytes:
    return queue_time_line(back_at, kind="token_gap", session_id=session, step=step, pid=pid,
                           start_time=start_time, token_left_at=left_at, token_back_at=back_at)


def write_token_gaps(lines: Sequence[bytes], *, tail: bytes = b"") -> None:
    from scripts.build_queue import head_facts
    write_lines(store.root() / head_facts.QUEUE_TIMES_PATH, lines, tail=tail)


HEAD_NOW = datetime(2026, 10, 30, 11, 0, tzinfo=UTC).timestamp()


def write_process(proc: Path, pid: int, used: int = 0, *, parent: int = 1,
                  start: str | None = None, anonymous: int | None = None, swap: int = 0) -> None:
    """Write private process identity, resident use, anonymous use, and swap."""
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(parent), *(["0"] * 17), str(pid) if start is None else start]
    if (directory / "stat").write_text(f"{pid} (verify) {' '.join(fields)}\n") <= 0:
        raise OSError("empty fixture process")
    anon = used if anonymous is None else anonymous
    if (directory / "status").write_text(
            f"VmRSS: {used // 1024} kB\nRssAnon: {anon // 1024} kB\nVmSwap: {swap // 1024} kB\n") <= 0:
        raise OSError("empty fixture memory")


def queue_read_fixture(view: gate.QueueView, policy: gate.AdmissionPolicy) -> gate.QueueRead:
    """Keep recorded renderer decisions with explicit room evidence."""
    ordered: list[tuple[Path, gate.RankedWaiter]] = [(Path(f"{step.pid}-{step.pid}.waiting"),
        {"pid": step.pid, "start_time": str(step.pid), "need": step.need, "since": step.since,
         "session_id": step.session_id, "session_name": step.session_name,
         "step": step.step, "folder": step.folder, "order": gate.NormalOrder()}) for step in view.waiting]
    reservations: tuple[gate.Reservation, ...] = tuple({
        "pid": index, "start_time": str(index), "need": step.promise,
        "repo": "fixture", "step": step.step, "worktree": step.folder,
        "admitted_at": "", "sidecar": "", "test_threads": gate.NoTestWidth(),
        "call_id": gate.NoCallRecord(),
    } for index, step in enumerate(view.running, 1) if gate.running_has_reservation(step))
    inputs = gate.QueueInputs(ordered, view.running, reservations,
        gate.HostMemory(view.available, gate.UnknownHostTotal()), gate.NoPressureHold(),
        gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), view.now)
    reading = gate.queue_read_from(inputs, gate.AdmissionLimits(view.reserve), policy)
    head = reading.explanation.head
    if isinstance(head, gate.HeadExplanation):
        original = next(step for step in view.waiting if step.pid == head.waiter.pid)
        reason = original.reason
        decision = head.decision
        if isinstance(reason, gate.HeadAdmissionStatus):
            decision = reason.decision
        elif isinstance(reason, gate.NeedsMemory):
            room = max(0, view.available - reason.reserve - reason.growth)
            decision = replace(decision, state="hold", free_passes=False,
                threshold=reason.threshold, promised=reason.growth, reserve=reason.reserve,
                room=gate.RoomCheck(original.need, room, False, basis=gate.DoesNotFit()))
        elif isinstance(reason, (gate.MemoryPressure, gate.PressureUnavailable)):
            decision = replace(decision, state="hold", pressure=reason)
        cause = reason.cause if isinstance(reason, gate.HeadAdmissionStatus) else reason
        assert not isinstance(cause, gate.TokenWaitingWaiter)
        head = replace(head, waiter=original, decision=decision, cause=cause,
                       room=decision.room, free=gate.decision_checks(decision, original.need, view.available)[1])
    explanation = replace(reading.explanation, head=head,
        line=replace(reading.explanation.line, steps=tuple(replace(row,
            waiter=next(step for step in view.waiting if step.pid == row.waiter.pid))
            for row in reading.explanation.line.steps)))
    if view.waiting and isinstance(view.waiting[0].reason, gate.ReadingUnavailable):
        explanation = replace(explanation, host=view.waiting[0].reason)
    return replace(reading, view=view, explanation=explanation)


def head_reading(*, since: float = HEAD_NOW - 600, now: float = HEAD_NOW,
                 fields: dict[str, object] | None = None, step_name: str = "nextest") -> gate.QueueRead:
    step = gate.WaitingStep(123, "head-session", "head", step_name, "/tmp/head", since,
                            gate.GIB, gate.ReadyForAdmission())
    reading = queue_read_fixture(gate.QueueView((step,), (), 30 * gate.GIB, 0, 3 * gate.GIB, now), policy=admission_policy_fixture())
    row = cast(gate.RankedWaiter, cast(object, {"pid": step.pid, "start_time": "123", "need": step.need,
                            "since": since, "session_id": step.session_id,
                            "session_name": step.session_name, "step": step.step,
                            "folder": step.folder, "order": gate.NormalOrder(), **(fields or {})}))
    return replace(reading, ordered=[(Path("123-123.waiting"), row)])


def with_waiting_process(reading: gate.QueueRead, step: gate.WaitingStep, start: str) -> gate.QueueRead:
    """Add a waiting row and its recorded process identity to the same snapshot."""
    row: gate.RankedWaiter = {"pid": step.pid, "start_time": start, "need": step.need,
                        "since": step.since, "session_id": step.session_id, "step": step.step,
                        "order": gate.NormalOrder()}
    return replace(reading, ordered=[*reading.ordered, (Path(f"{step.pid}-{start}.waiting"), row)],
                   view=replace(reading.view, waiting=(*reading.view.waiting, step)))



def fixture_queue_inputs(directory: Path, processes: dict[int, gate.ProcessIdentity], now: float,
                         policy: gate.AdmissionPolicy) -> gate.QueueInputs:
    """Read captured fixture rows through the import readers and the shared builder."""
    try:
        host = gate.read_meminfo(Path(os.environ["BUILDLOG_MEMINFO"]))
    except (OSError, ValueError):
        host = gate.ReadingUnavailable()
    rows: list[tuple[Path, gate.Waiter]] = []
    for path in directory.glob("*.waiting"):
        try:
            rows.append((path, gate.read_waiter(path)))
        except (OSError, ValueError, TypeError):
            continue
    ordered, priority = gate.resolve_queue_order(directory, rows)
    reservations = [(row, gate.reservation_memory(path, row, datetime.fromtimestamp(now, UTC),
                     processes, Path(os.environ["BUILDLOG_PROC"]), policy))
                    for path, row in sorted(gate.reservation_rows(directory), key=lambda item: item[0])]
    readings = gate.QueueSnapshotReadings(processes, {}, host=host,
        pressure=gate.read_pressure_reading(host if isinstance(host, gate.HostMemory) else gate.HostMemory(0, gate.UnknownHostTotal()), policy=policy), recovery=gate.ReadyToStart(),
        compilers=gate.sccache_memory(), policy=policy)
    return gate.build_queue_inputs(ordered, reservations, readings, gate.read_head_override(directory), now, priority)


def fixture_queue_read(directory: Path, processes: dict[int, gate.ProcessIdentity], now: float) -> gate.QueueRead:
    config = gate.read_memory_config()
    inputs = fixture_queue_inputs(directory, processes, now, config.policy)
    return gate.queue_read_from(inputs, gate.resolve_admission_limits(config), config.policy)


class QueueFixture(ExitStack):
    """Private files for every renderer, card and CLI fixture consumer."""

    def __init__(self, *, captured: bool = True) -> None:
        super().__init__()
        self.root: Path = Path(self.enter_context(tempfile.TemporaryDirectory()))
        self.directory: Path = self.root / "buildlog/admission"
        self.proc: Path = self.root / "proc"
        self.directory.mkdir(parents=True)
        self.proc.mkdir()
        environment = fake_memory_readings(self.root, available_kib=int(24.5 * gate.GIB) // 1024,
                                            total_kib=60 * gate.GIB // 1024, full_avg10=7.7)
        environment.update({"BUILDLOG_DIR": str(self.root / "buildlog"), "BUILDLOG_PROC": str(self.proc),
                            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                            "USER_ZONE": "America/New_York"})
        self.enter_context(mock.patch.dict(os.environ, environment))
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        self.enter_context(mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}))
        self.labels: dict[str, UnitLabels] = {}
        _ = self.enter_context(mock.patch.object(gate, "priority_labels", return_value=self.labels))
        _ = self.enter_context(mock.patch.object(roster, "roster",
            side_effect=AssertionError("a queue fixture must not read the live roster")))
        self.paths: list[Path] = []
        self.moves: list[bytes] = []
        self.reservation: Path = self.directory / "900-900.reservation"
        if not captured:
            return
        for position, (production, unit, session, step, need, waited, folder) in enumerate(STEPS, 1):
            pid = 1000 + position
            # Session identities repeat exactly where the captured queue does.
            session_id = "id-" + session
            self.labels[session_id] = UnitLabels(session, production, unit)
            since = NOW - waited * 60 - 12 + (position / 100 if position > 2 else 0)
            row: dict[str, object] = {"pid": pid, "start_time": str(pid), "need": int(need * gate.GIB),
                "since": since, "session_id": session_id, "session_name": session,
                "step": step, "folder": folder}
            if position <= 2:
                row.update(priority_since=NOW - 12)
            self.paths.append(self.write_waiter(row))
        self.labels["id-widget"] = UnitLabels("widget-enhancements-title", "tool-based-ui", "widget")
        self.labels["id-widget-director"] = UnitLabels("widget", "tool-based-ui", "widget")
        self.write_running(used=int(5.1 * gate.GIB), need=int(11.1 * gate.GIB))
        self.moves = [self.move(1, NOW - 372, 27), self.move(2, NOW - 372, 27),
            bump_line(NOW - 192, session="id-widget", step="nextest", folder="/tmp/widget",
                      actor="id-widget-director", old_position=29), self.move(1, NOW - 12, 1)]
        self.write_json(self.directory / "queue_priority.json", {"unit": "cables", "why": "captured priority",
            "set_at": NOW - 12, "set_by": "user"})
        write_bumps(NOW, self.moves)
        self.write_pace([NOW - 3600 + index * 80 for index in range(44)])

    @staticmethod
    def write_json(path: Path, row: dict[str, object]) -> None:
        if path.write_text(json.dumps(row, separators=(",", ":"))) <= 0:
            raise OSError("empty fixture row")

    def write_running(self, *, used: int, need: int, step: str = "nextest") -> None:
        write_process(self.proc, 900, used)
        scope = self.root / "widget.scope"
        scope.mkdir(exist_ok=True)
        _ = (scope / "memory.current").write_text(str(used))
        _ = (scope / "memory.stat").write_text(f"anon {used}\ninactive_file 0\n")
        _ = (scope / "cgroup.procs").write_text("900\n")
        sidecar = self.root / "widget.scope-name"
        _ = sidecar.write_text(str(scope))
        self.write_json(self.reservation, {"pid": 900, "start_time": "900", "need": need,
            "admitted_at": store.utc_iso(NOW - 25), "repo": "hana", "worktree": "/tmp/widget",
            "sidecar": str(sidecar), "session_id": "id-widget", "session_name": "widget-enhancements-title",
            "step": step, "folder": "/tmp/widget",
            "queue_waiter": {"pid": 900, "start_time": "900", "need": need, "since": NOW - 25,
                "session_id": "id-widget", "session_name": "widget-enhancements-title",
                "step": step, "folder": "/tmp/widget"}})

    def write_waiter(self, row: dict[str, object]) -> Path:
        pid = int(str(row["pid"]))
        write_process(self.proc, pid)
        path = self.directory / f"{pid}-{row['start_time']}.waiting"
        self.write_json(path, row)
        return path

    def waiter(self, pid: int, session: str, name: str, step: str = "nextest") -> Path:
        return self.write_waiter({"pid": pid, "start_time": str(pid), "need": 12 * gate.GIB,
            "since": NOW + pid, "session_id": session, "session_name": name,
            "step": step, "folder": f"/tmp/tree-{pid}"})

    def change(self, position: int, **fields: object) -> None:
        path = self.paths[position - 1]
        row = cast(dict[str, object], json.loads(path.read_text()))
        row.update(fields)
        self.write_json(path, row)

    def move(self, position: int, at: float, old_position: int, *, actor: str = "id-work-lead") -> bytes:
        row = cast(dict[str, object], json.loads(self.paths[position - 1].read_text()))
        return bump_line(at, session=str(row["session_id"]), step=str(row["step"]),
                         folder=str(row["folder"]), actor=actor, old_position=old_position)

    def write_pace(self, ended: Sequence[float], *, tail: bytes = b"", host: str = "") -> None:
        lines = [(json.dumps({"host": host or store.host_name(), "ended_at": store.utc_iso(at)}) + "\n").encode()
                 for at in ended]
        write_lines(self.directory / "anon_peaks.jsonl", lines, tail=tail)

    def inputs(self, policy: gate.AdmissionPolicy) -> gate.QueueInputs:
        """Resolve the fixed 10:46 queue once, including raw pressure."""
        return fixture_queue_inputs(self.directory, gate.process_table(self.proc), NOW, policy)

    def read(self) -> gate.QueueRead:
        return fixture_queue_read(self.directory, gate.process_table(self.proc), NOW)

    def model(self) -> queue_view.QueueModel:
        reading = self.read()
        return queue_view.QueueModel(reading, self.labels, head_facts.read_head_facts(reading),
                                     head_facts.read_pace(NOW), ZONE, queue_view.HostLoadUnavailable(),
                                     gate.smallest_head_release(reading.explanation),
                                     ci_memory_slice=gate.read_memory_config().ci_memory_slice)

    def cap_model(self) -> queue_view.QueueModel:
        """Show the compact page with a clear pressure reading."""
        _ = (self.root / "pressure").write_text("full avg10=0\nsome avg10=0\n")
        self.change(1, need=int(6.8 * gate.GIB), priority_since=NOW - 42)
        return self.model()


class QueueFixtureTests(unittest.TestCase):
    fixture: QueueFixture = cast(QueueFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        self.fixture = self.enterContext(QueueFixture())


def with_waiting(model: queue_view.QueueModel, steps: Sequence[gate.WaitingStep]) -> queue_view.QueueModel:
    """Replace displayed rows while retaining their matching admission row identities."""
    by_pid = {row["pid"]: (path, row) for path, row in model.reading.ordered}
    reading = replace(model.reading, view=replace(model.reading.view, waiting=tuple(steps)),
                      ordered=[by_pid[step.pid] for step in steps])
    return replace(model, reading=reading)


def waiting_rows(lines: Sequence[Line]) -> list[str]:
    """Identify step rows by the fixed place column, including a clipped name."""
    import re
    return [text for line in lines
            if re.match(r"^ *[0-9]+(?: |▸)", text := plain([line]))]


class AdmissionLogTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root)}))
