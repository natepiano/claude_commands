"""Run checkpoint tests and record and deliver bump gate results."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.message import session_send
from scripts.production import staff
from scripts.production.production_doc import ProductionDocument

TREE_CHANGED_STATUS = 77


class Stop(Exception):
    def __init__(self, step: str, state: str, detail: str, message: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.state: str = state
        self.detail: str = detail
        self.message: str = message


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail}", flush=True)


def tool(name: str, fallback: Path, override: str) -> str:
    return os.environ.get(override) or shutil.which(name) or str(fallback)


class CheckpointTestStep(NamedTuple):
    label: str
    command: list[str]
    folder: Path


class CargoPackage(NamedTuple):
    folder: Path
    name: str


class CargoWorkspace(NamedTuple):
    pass


def read_cargo_package(folder: Path) -> CargoPackage | CargoWorkspace:
    manifest = tomllib.loads((folder / "Cargo.toml").read_text(encoding="utf-8"))
    if "package" not in manifest:
        # A workspace root has no package tests.
        return CargoWorkspace()
    return CargoPackage(folder, cast(str, manifest["package"]["name"]))


def test_steps(production: ProductionDocument, paths: tuple[str, ...],
               merge_tests: tuple[tuple[str, ...], ...]) -> list[CheckpointTestStep]:
    checkout = production.checkout
    packages: set[CargoPackage] = set()
    examples: set[tuple[str, str]] = set()
    for raw in paths:
        path = checkout / raw
        for parent in (path.parent, *path.parents):
            if parent == checkout.parent:
                break
            manifest = parent / "Cargo.toml"
            if manifest.exists():
                package = read_cargo_package(parent)
                if isinstance(package, CargoPackage):
                    packages.add(package)
                break
        match = re.search(r"(?:^|/)crates/([^/]+)/examples/([^/.]+)", raw)
        if match:
            folder = checkout / "crates" / match.group(1)
            if (folder / "Cargo.toml").exists():
                package = read_cargo_package(folder)
                if isinstance(package, CargoPackage):
                    examples.add((package.name, match.group(2)))
    verify = tool("verify.sh", Path.home() / ".claude/scripts/direct/verify.sh", "MERGE_CHECKPOINT_VERIFY")
    verify_command = [verify] if os.access(verify, os.X_OK) else ["bash", verify]
    steps = [CheckpointTestStep(f"package {package.name}", [*verify_command, "test", package.name], package.folder)
             for package in sorted(packages)]
    steps.extend(CheckpointTestStep(f"example {package}/{name}", [*verify_command, "example", package, "--name", name], checkout)
                 for package, name in sorted(examples))
    steps.extend(CheckpointTestStep(f"merge test {index}", list(command), checkout)
                 for index, command in enumerate(merge_tests, 1))
    return steps


def run_test_command(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=folder, capture_output=True, text=True, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, "", f"{error}\n")

# A nextest failure row: `FAIL [  12.2s] (1012/1067) <binary> <test>`.
NEXTEST_FAIL = re.compile(r"^\s+FAIL \[[^]]*\] \([^)]*\) \S+ (\S+)\s*$", re.MULTILINE)


def failed_tests(output: str) -> frozenset[str]:
    """Return unique names of tests that nextest reports as failed."""
    return frozenset(NEXTEST_FAIL.findall(output))


def only_tests(command: list[str], names: frozenset[str]) -> list[str]:
    """Restrict the command to the named tests with verify.sh --filter or nextest -E test(=name)."""
    if any(Path(word).name == "verify.sh" for word in command):
        return [*command, *(word for name in sorted(names) for word in ("--filter", name))]
    return [*command, *(word for name in sorted(names) for word in ("-E", f"test(={name})"))]


def stop_cancelled_test(name: str, result: subprocess.CompletedProcess[str], log: Path) -> None:
    if result.returncode == TREE_CHANGED_STATUS:
        raise Stop("test", "cancelled", f"{name} cancelled because the tree changed; log {log}",
                   f"resubmit this checkpoint after your edits are done; test log: {log}")


def run_checkpoint_test_steps(production: ProductionDocument, paths: tuple[str, ...],
                              merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str],
                              flaky_tests: frozenset[str], log: Path) -> str:
    """Run checkpoint commands and their retries; the caller owns merge rollback."""
    steps = test_steps(production, paths, merge_tests)
    log.parent.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[CheckpointTestStep, frozenset[str]]] = []
    with log.open("a", encoding="utf-8") as output:
        for step in steps:
            result = run_test_command(step.command, step.folder)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{step.label.replace(' ', '_')}_EXIT={result.returncode}\n")
            output.flush()
            stop_cancelled_test(step.label, result, log)
            if not test_passed(result):
                failed.append((step, failed_tests(result.stdout + result.stderr)))
    unresolved: list[str] = []
    for step, tests in failed:
        name, command, folder = step
        # Run only the known flaky tests when they cause the failure.
        alone = bool(tests) and tests <= flaky_tests
        with log.open("a", encoding="utf-8") as output:
            result = run_test_command(only_tests(command, tests) if alone else command, folder)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_RERUN_EXIT={result.returncode}\n")
        stop_cancelled_test(name, result, log)
        package = name.removeprefix("package ")
        if test_passed(result) and (alone or package in flakes):
            note = (f"known flaky tests {', '.join(sorted(tests))}: red in {name}, green alone" if alone
                    else f"known flake {package}: red in batch, green alone")
            production.log.parent.mkdir(parents=True, exist_ok=True)
            with production.log.open("a", encoding="utf-8") as ledger:
                _ = ledger.write(f"- {datetime.now(production.zone):%H:%M %Z}: {note}\n")
        else:
            names = tests | failed_tests(result.stdout + result.stderr)
            unresolved.append(name + (f" ({', '.join(sorted(names))})" if names else ""))
    report("test", "ok", f"{len(steps)} commands ran; {len(failed)} red; log {log}")
    if unresolved:
        raise Stop("red", "failed", f"{', '.join(unresolved)} failed; log {log}",
                   f"repair {', '.join(unresolved)} and send a new checkpoint; test log: {log}")
    report("red", "ok", "all commands green or known flakes green alone")
    return ", ".join(step.label for step in steps) if steps else "no tests"


def test_passed(result: subprocess.CompletedProcess[str]) -> bool:
    """Accept only status zero."""
    return result.returncode == 0


class BumpGateRecord(TypedDict):
    """The gate plan and its process or completed result, owned by one merge."""

    tip: str
    checkpoint: str
    production_doc: str
    owner_id: str
    unit_id: str
    name: str
    merge_branch: str
    checkout: str
    production_log: str
    zone: str
    unit: str
    delivered_to: list[Literal["unit", "showrunner"]]
    paths: list[str]
    merge_tests: list[list[str]]
    flakes: list[str]
    flaky_tests: list[str]
    started: str
    pid: int
    proc_start: str
    result: Literal["pending", "green", "red"]
    detail: str
    log: str

_held_record_locks: ContextVar[frozenset[Path]] = ContextVar("held_record_locks", default=frozenset())


@contextmanager
def record_lock(path: Path) -> Generator[None]:
    held = _held_record_locks.get()
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _held_record_locks.set(held | {path})
        try:
            yield
        finally:
            _held_record_locks.reset(token)


def read_record(path: Path) -> BumpGateRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid bump gate record: {path}")
    return cast(BumpGateRecord, cast(object, raw))


def save_record(path: Path, record: BumpGateRecord) -> None:
    write_text_atomic(path, json.dumps(record, indent=2) + "\n")


def same_gate_runner(first: BumpGateRecord, second: BumpGateRecord) -> bool:
    return (first["tip"], first["pid"], first["proc_start"]) == (second["tip"], second["pid"], second["proc_start"])


def send_result(path: Path, record: BumpGateRecord) -> None:
    """Deliver each pending notice to the current recipient. Save each successful delivery."""
    with record_lock(path):
        current = read_record(path)
        if not same_gate_runner(current, record) or current["result"] == "pending":
            print("bump gate delivery superseded; no notice sent", flush=True)
            return
        red = current["result"] == "red"
        text = (f"Bump gate {'red' if red else 'green'}: {current['unit']}; tip {current['tip']}; "
                + f"{current['detail']}; log {current['log']}")
        script = tool("send.py", session_send.SEND_SCRIPT, "MERGE_CHECKPOINT_SEND")
        recipients: tuple[Literal["unit", "showrunner"], ...] = ("unit", "showrunner") if red else ("showrunner",)
        delivered = current.get("delivered_to", [])
        current["delivered_to"] = delivered
        for recipient in recipients:
            if recipient in delivered:
                continue
            for attempt in (1, 2):
                try:
                    members = staff.snapshot().rows_by_id
                    owner = members[staff.StaffId(current["owner_id"])]
                    target = members[staff.StaffId(current["unit_id"])] if recipient == "unit" else owner
                    address = f"session:{target.current_session}"
                    result = subprocess.run(
                        [sys.executable, script, "--to", address, "--from", owner.name, "--text", text],
                        capture_output=True, text=True, check=False,
                    )
                    if result.returncode:
                        raise ValueError(failure_detail(result))
                    delivered.append(recipient)
                    save_record(path, current)
                    break
                except (OSError, ValueError, KeyError) as error:
                    print(f"bump gate send {recipient} attempt {attempt} failed: {error}", flush=True)


def complete_gate(path: Path, record: BumpGateRecord, result: Literal["green", "red"], detail: str) -> None:
    with record_lock(path):
        current = read_record(path)
        if not same_gate_runner(current, record):
            print("bump gate completion superseded; no result saved or notice sent", flush=True)
            return
        current["result"] = result
        current["detail"] = " ".join(detail.splitlines())
        save_record(path, current)
    send_result(path, current)
