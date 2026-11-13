"""Validate the tagged decoder with both Python union forms."""
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Literal, NotRequired, TypedDict, cast
import unittest
from unittest import mock

from scripts.build_queue import admission_records as records
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.admission_test_support import wait_reply
from scripts.build_queue.scope_activity import NotServiceSlice, RunningServiceJob


@dataclass(frozen=True)
class Ready:
    count: int


@dataclass(frozen=True)
class Unavailable:
    reason: str


class Reading(TypedDict):
    state: Literal["ready"] | Literal["waiting"]
    value: Ready | Unavailable
    detail: NotRequired[str]


def service_snapshot() -> records.AdmissionWaitSnapshot:
    memory = records.UnownedSliceMemory(123, pid=42, job_state=RunningServiceJob(100))
    running = records.RunningStep("service", "hana CI", "service", "hana-ci.slice", 0, memory, pid=42)
    return replace(wait_reply(records.NeedsMemory()).snapshot, running=(running,))


@contextmanager
def pinned_snapshot_reader() -> Generator[tuple[Callable[[object, object], object], type]]:
    """Load both old sources without a checkout or a copy of their schema."""
    modules = {name: ModuleType(name) for name in ("_pinned_snapshot_codec", "_pinned_admission_records")}
    with mock.patch.dict(sys.modules, modules):
        for name, module in modules.items():
            path = "scripts/build_queue/" + name.removeprefix("_pinned_") + ".py"
            source = subprocess.run(["git", "show", "7fdfd9a6a:" + path],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True).stdout
            exec(compile(source, "7fdfd9a6a:" + path, "exec"), module.__dict__)
        yield (cast(Callable[[object, object], object], getattr(modules["_pinned_snapshot_codec"], "parse_snapshot_value")),
               cast(type, getattr(modules["_pinned_admission_records"], "AdmissionWaitSnapshot")))


class SnapshotCodecTests(unittest.TestCase):
    def test_pinned_client_schema_ignores_new_service_states_in_the_optional_field(self) -> None:
        with pinned_snapshot_reader() as (parse, schema):
            for kind in ("ServiceJobStateNotRecorded", "WaitingServiceJob", "UnmatchedServiceJob"):
                with self.subTest(kind=kind):
                    raw = cast(dict[str, object], codec.snapshot_value(service_snapshot()))
                    running = cast(list[dict[str, object]], raw["running"])
                    memory = cast(dict[str, object], running[0]["measured"])
                    memory["service_job_state"] = {"kind": kind, "started_at": 100}
                    parsed = parse(raw, schema)
                    steps = cast(tuple[object, ...], getattr(parsed, "running"))
                    measured = cast(object, getattr(steps[0], "measured"))
                    self.assertEqual(cast(object, getattr(measured, "bytes")), 123)
                    state = cast(object, getattr(measured, "job_state"))
                    self.assertIsInstance(state, RunningServiceJob)
                    self.assertEqual(cast(object, getattr(state, "started_at")), 100)
                    self.assertFalse(hasattr(measured, "service_job_state"))
                    memory["job_state"] = {"kind": "WaitingServiceJob", "started_at": 100}
                    with self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
                        _ = parse(raw, schema)

    def test_acquisition_defaults_unused_variants_and_restores_strict_reads(self) -> None:
        for field in ("job_state", "service_job_state"):
            with self.subTest(field=field):
                raw = cast(dict[str, object], codec.snapshot_value(service_snapshot()))
                running = cast(list[dict[str, object]], raw["running"])
                memory = cast(dict[str, object], running[0]["measured"])
                memory[field] = {"kind": "FutureServiceJob"}
                with codec.snapshot_read_policy(codec.SnapshotReadPolicy.ACQUISITION_FIELDS):
                    parsed = cast(records.AdmissionWaitSnapshot, codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot))
                measured = parsed.running[0].measured
                self.assertIsInstance(measured, records.UnownedSliceMemory)
                assert isinstance(measured, records.UnownedSliceMemory)
                self.assertEqual(measured.bytes, 123)
                self.assertEqual(measured.job_state, NotServiceSlice())
                self.assertEqual(measured.service_job_state, records.ServiceJobStateNotRecorded())
                with self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
                    _ = codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot)

    def test_acquisition_refuses_unknown_variants_in_a_used_field(self) -> None:
        raw = cast(dict[str, object], codec.snapshot_value(service_snapshot()))
        raw["head_reason"] = {"kind": "FutureAdmissionReason"}
        with codec.snapshot_read_policy(codec.SnapshotReadPolicy.ACQUISITION_FIELDS), \
             self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
            _ = codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot)

    def test_literal_unions_accept_each_member_and_refuse_unknown_values(self) -> None:
        schema = Literal["ready"] | Literal["waiting"]
        for value in ("ready", "waiting"):
            self.assertEqual(codec.parse_snapshot_value(value, schema), value)
        for value in ("unknown", 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value(value, schema)

    def test_nested_union_records_keep_tags_and_optional_fields(self) -> None:
        for record in (Ready(3), Unavailable("not read")):
            raw = {"state": "waiting", "value": codec.snapshot_value(record)}
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record})
            raw["detail"] = "fixture"
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record, "detail": "fixture"})
        with self.assertRaises(ValueError):
            _ = codec.parse_snapshot_value({"state": "ready", "value": {"kind": "Other"}}, Reading)

    def test_scalar_unions_keep_strict_types_and_finite_numbers(self) -> None:
        schema = list[int | str | float]
        self.assertEqual(codec.parse_snapshot_value([1, "sample", 2.5], schema), [1, "sample", 2.5])
        for value in (True, float("nan"), float("inf"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value([value], schema)


if __name__ == "__main__":
    _ = unittest.main()
