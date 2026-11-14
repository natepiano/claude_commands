"""The census reader turns cargo-handler census JSON into typed records or one failure."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.lib import conf
from scripts.production import census, staff

WORKER: dict[str, object] = {
    "staff_id": "handler-impl", "name": "handler-impl", "parent_staff_id": "handler-unit",
    "machine": "natedev", "status": "busy", "activity": "verify test",
    "activity_started_at": 1794600060, "pass": {"role": "impl", "phase": "8"}, "pane": "open",
    "runs": [{"label": "verify test", "started_at": 1794600060, "state": "running", "unknown": None},
             {"label": "lint", "started_at": None, "state": "missing", "unknown": {"reason": "run has no start time"}}],
    "unknown": None}
SAMPLE: dict[str, object] = {
    "schema": 2,
    "machines": [{"name": "natedev", "reachable": True, "error": None},
                 {"name": "mac", "reachable": False, "error": "timed out"}],
    "directors": [{"staff_id": "handler-unit", "name": "handler-unit", "machine": "natedev",
                   "current_run_id": "/tmp/claude/unit/54fb1c7e", "status": "busy", "activity": "thinking",
                   "activity_started_at": 1794600000, "unknown": {"reason": "no open pass"}},
                  {"staff_id": "quiet-unit", "name": "quiet-unit", "machine": "natedev", "current_run_id": None,
                   "status": None, "activity": "", "activity_started_at": None, "unknown": None}],
    "workers": [WORKER,
                {"staff_id": "handler-test", "name": "handler-test", "parent_staff_id": "handler-unit",
                 "machine": "natedev", "status": "idle", "activity": "", "activity_started_at": None,
                 "pass": None, "pane": "closed", "runs": [], "unknown": None}],
    "sessions": [{"session": "4dbdb78e", "machine": "natedev", "status": "busy", "activity": "impl · waiting"},
                 {"session": "01a18dbb", "machine": "natedev", "status": None, "activity": None}],
}


class CensusTests(unittest.TestCase):
    def test_schema_two_parses_into_typed_records(self) -> None:
        expected = census.Census(
            (census.CensusMachine("natedev", True), census.CensusMachine("mac", False)),
            (census.CensusSession("4dbdb78e", "busy"), census.CensusSession("01a18dbb", None)),
            (census.CensusWorker(staff.StaffId("handler-impl"), "handler-impl", staff.StaffId("handler-unit"), "busy",
                                 census.CensusPass("impl", "8"),
                                 (census.CensusRun("verify test", 1794600060.0, "running"),
                                  census.CensusRun("lint", None, "missing"))),
             census.CensusWorker(staff.StaffId("handler-test"), "handler-test", staff.StaffId("handler-unit"), "idle",
                                 census.NoPass(), ())))
        self.assertEqual(census.parse(json.dumps(SAMPLE)), expected)

    def test_bad_output_is_one_failure_with_its_reason(self) -> None:
        worker = WORKER
        cases: list[tuple[str, str]] = [
            ("not json", "census output is not JSON"),
            (json.dumps([]), "census: census is not an object"),
            (json.dumps({**SAMPLE, "schema": 1}), "census schema 1 is not 2"),
            (json.dumps({**SAMPLE, "schema": True}), "census schema True is not 2"),
            (json.dumps({key: value for key, value in SAMPLE.items() if key != "sessions"}),
             "census: census.sessions is not a list"),
            (json.dumps({**SAMPLE, "sessions": [{"session": "s", "status": 2}]}),
             "census: sessions[0].status is not text or null"),
            (json.dumps({**SAMPLE, "machines": [{"name": "natedev", "reachable": "yes"}]}),
             "census: machines[0].reachable is not true or false"),
            (json.dumps({**SAMPLE, "workers": [{**worker, "status": 3}]}),
             "census: workers[0].status is not text or null"),
            (json.dumps({**SAMPLE, "workers": [{**worker, "runs": [{"label": "x", "started_at": 1, "state": "done"}]}]}),
             "census: workers[0].runs[0].state is not one of running, finished_queued, missing"),
            (json.dumps({**SAMPLE, "workers": [{**worker, "runs": [{"label": "x", "started_at": True,
                                                                     "state": "running"}]}]}),
             "census: workers[0].runs[0].started_at is not a number or null"),
            (json.dumps({**SAMPLE, "workers": [{**worker, "pass": {"role": "impl"}}]}),
             "census: workers[0].pass.phase is not text"),
        ]
        for text, reason in cases:
            with self.subTest(reason=reason):
                result = census.parse(text)
                self.assertIsInstance(result, census.CensusFailed)
                assert isinstance(result, census.CensusFailed)
                self.assertTrue(result.reason.startswith(reason), result.reason)

    def test_read_runs_the_census_once_within_the_configured_time(self) -> None:
        done = subprocess.CompletedProcess(list(census.COMMAND), 0, json.dumps(SAMPLE), "")
        with patch.object(subprocess, "run", return_value=done) as run:
            result = census.read()
        self.assertIsInstance(result, census.Census)
        run.assert_called_once_with(census.COMMAND, capture_output=True, text=True, timeout=10.0, check=False)

    def test_read_failures_name_the_cause(self) -> None:
        cases: list[tuple[subprocess.CompletedProcess[str] | Exception, str]] = [
            (subprocess.TimeoutExpired(list(census.COMMAND), 10), "census took longer than 10 s"),
            (FileNotFoundError(2, "No such file or directory", "cargo-handler"),
             "census: [Errno 2] No such file or directory: 'cargo-handler'"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
             "census: 'utf-8' codec can't decode byte 0xff"),
            (subprocess.CompletedProcess(list(census.COMMAND), 2, "", "bad config\nsecond line"),
             "census exited 2: "),
        ]
        for outcome, reason in cases:
            with self.subTest(reason=reason):
                runner = (patch.object(subprocess, "run", side_effect=outcome) if isinstance(outcome, Exception)
                          else patch.object(subprocess, "run", return_value=outcome))
                with runner:
                    result = census.read()
                self.assertIsInstance(result, census.CensusFailed)
                assert isinstance(result, census.CensusFailed)
                self.assertTrue(result.reason.startswith(reason), result.reason)
                self.assertNotIn("\n", result.reason)

    def test_an_unusable_time_limit_is_a_failure(self) -> None:
        with (patch.object(conf, "read_or_repository", side_effect=conf.Unusable("no usable value")),
              patch.object(subprocess, "run") as run):
            result = census.read()
        self.assertEqual(result, census.CensusFailed("census: units_census_seconds: no usable value"))
        run.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
