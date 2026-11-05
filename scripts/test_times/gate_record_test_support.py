"""Shared timing records for tests with private build logs."""

from scripts.test_times.gate_record import GateRun, ModuleTimes


def remove_run_measurements(record: dict[str, object]) -> None:
    """Remove fields that legacy gate records do not contain."""
    del record["slow_test_s"]
    del record["tests_run"]


def module_times() -> ModuleTimes:
    return {
        "wall_s": 3.0, "cpu_s": 2.0, "tests_s": 1.5, "tests_run": 2,
        "failures": 1, "errors": 0, "skipped": 0,
        "tests": [{"test": "Example.test_pass", "s": 1.0, "status": "pass"},
                  {"test": "Example.test_fail", "s": 0.5, "status": "fail"}],
    }


def gate_run() -> GateRun:
    times = module_times()
    return {
        "started_at": "2026-11-03T12:00:00.000Z", "ended_at": "2026-11-03T12:00:05.000Z",
        "wall_s": 5.0, "hold_wait_s": 1.0, "selection": "affected", "reason": "fixture change",
        "status": 1, "max_jobs": 2, "peak_mib": 30.0, "cap_mib": 100.0,
        "checkout": "/fixture/checkout", "branch": "fixture", "sha": "a" * 40,
        "tree": "b" * 40, "source": "tests-only", "aborted": False, "dirty": True,
        "slow_test_s": 0.5,
        "suites": [{"suite": "scripts", "wall_s": 4.0, "status": 1}],
        "modules": [{"suite": "scripts", "module": "test_example.py", "attempt": 1,
                     "wall_s": times["wall_s"], "cpu_s": times["cpu_s"], "peak_mib": 30.0,
                     "status": 1, "tests_run": times["tests_run"], "failures": times["failures"],
                     "errors": times["errors"], "skipped": times["skipped"],
                     "tests_s": times["tests_s"], "tests": times["tests"],
                     "restart": "hold", "times_missing": ""}],
    }
