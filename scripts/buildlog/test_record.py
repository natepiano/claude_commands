#!/usr/bin/env python3
"""Test record contents in process and keep real CLI entry cases on temporary roots."""

from __future__ import annotations

from functools import cached_property
from contextlib import closing
import gzip
import io
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, ClassVar, cast, override


from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.shell_test_support import shell_function, shell_functions

from scripts.buildlog import record as recorder
from scripts.buildlog import step_context
from scripts.buildlog import index
from scripts.buildlog import report, sample
from scripts.buildlog import store
from scripts.buildlog.cgroup_test_support import CPU, IO_LINES, save_scope_counters, write_cgroup

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.test_verify_untested_examples import FIXED_RUSTC, rust_tool_path
from scripts.lint.memory_test_support import fake_memory_readings
from scripts.buildlog.test_index import point_root_at
from scripts.lib import test_hang_guard
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case

SCRIPT = Path(__file__).with_name("record.py")
CONTEXT_SCRIPT = SCRIPT.with_name("step_context.py")
TESTDATA = Path(__file__).with_name("testdata")
START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC).timestamp()
CALL_ID = "natedev-20261002T120000-abcdef"
SWEEP = ["/home/u/.claude/scripts/lib/py", "/home/u/.claude/scripts/lint/sweep.py"]
NEXTEST = ["cargo", "nextest", "run", "--workspace", "--retries", "2"]
# What the ambient session would otherwise stamp on every record: the caller,
# the worker, the session and the call id all come from these.
AMBIENT = (
    "BUILDLOG_CALLER",
    "BUILDLOG_CALL_ID",
    "BUILDLOG_LOCAL_REASON",
    "BUILDLOG_MAC_HOLDER",
    "BUILDLOG_MAC_COPY",
    "BUILDLOG_MAC_COPY_S",
    "BUILDLOG_MAC_COPY_COUNT",
    "BUILDLOG_MAC_COPY_NATEDEV_S",
    "BUILDLOG_TARGET",
    "BUILDLOG_HOLD_WAIT_S",
    "LINT_OUTPUT_DIR",
    "VALIDATE_TARGET_DIR",
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "MANIFEST_PATH",
    "BUILDLOG_TREE_START",
    "BUILDLOG_SENT_TREE_KEY",
    "BUILDLOG_MEM_WAIT_S",
    "BUILDLOG_TARGET_FOLDER",
    "BUILDLOG_KNOWN_TARGET_DIR",
    "BUILDLOG_OUTPUT_ROOTS",
    "CARGO_TARGET_DIR",
    "CARGO_BUILD_TARGET_DIR",
)
Record = dict[str, object]


def ledger_line(**fields: object) -> str:
    event: Record = {
        "at": "2026-10-01T15:34:46+00:00",
        "machine": "natedev",
        "workspace": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "commit": "",
        "command": "test hana_diegetic",
        "outcome": "reused",
        "session": "",
        "wait_s": 0,
        "wall_s": 0,
        "build_s": 0,
        "saved_s": 19,
    }
    event.update(fields)
    return json.dumps(event)


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.addModuleCleanup(time.tzset)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}))
    time.tzset()


class RecordTests(unittest.TestCase):
    repository_seed: ClassVar[Path]
    base: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        seed_root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="record-seed-"))).resolve()
        seed_home = seed_root / "home"
        seed_home.mkdir()
        cls.repository_seed = seed_root / "repository"
        cls.repository_seed.mkdir()
        builder = cls()
        builder.base = seed_root
        builder.root = seed_root / "buildlog"
        builder.repo = cls.repository_seed
        with mock.patch.dict(os.environ, {"HOME": str(seed_home), "XDG_CONFIG_HOME": str(seed_home / ".config")}):
            _ = builder.git("init", "-q", "-b", "main", cwd=builder.repo)
            _ = builder.git("commit", "-q", "--allow-empty", "-m", "first", cwd=builder.repo)

    @override
    def setUp(self) -> None:
        # Other test modules also construct this fixture without class setup.
        cls = type(self)
        if not hasattr(cls, "repository_seed") or not cls.repository_seed.is_dir():
            self.addCleanup(cls.doClassCleanups)
            cls.setUpClass()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ))
        if "BUILDLOG_REPO_IDENTITY" in os.environ:
            del os.environ["BUILDLOG_REPO_IDENTITY"]
        self.enterContext(mock.patch.dict(os.environ, fake_memory_readings(self.base)))
        tools = rust_tool_path(self.base / "tools")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": tools}))
        self.root = self.base / "buildlog"
        point_root_at(self, self.root)
        self.repo = self.base / "hana"
        _ = shutil.copytree(cls.repository_seed, self.repo)
        _ = self.admission
        self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))

    @cached_property
    def admission(self) -> AdmissionFixture:
        return caller_admission(self, self.base, self.environment())

    def environment(self, **extra: str) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in AMBIENT and not name.startswith("UNIT_")
        }
        environment["BUILDLOG_DIR"] = str(self.root)
        environment["BUILD_HOLD_DIR"] = str(self.base / "holders")
        environment["BUILD_HOLD_RELEASE_DIR"] = str(self.base / "release")
        environment["STAFF_FILE"] = str(self.base / "staff.json")
        environment["BUILDLOG_CALL_ID"] = environment["BUILDLOG_CALLER"] = ""
        environment["BUILDLOG_HOLD_WAIT_FILE"] = environment["BUILDLOG_HOLD_ADMISSION_FILE"] = ""
        environment["BUILDLOG_HOLD_STARTED_FILE"] = environment["BUILDLOG_HOLD_WAITER_FILE"] = ""
        environment["BUILDLOG_HOLD_REGISTRATION_FILE"] = str(self.base / "registration")
        environment["MAC_TEST_STATE_DIR"] = str(self.base / "mac-state")
        environment["NOTIFIER_SESSIONS_DIR"] = str(self.base / "sessions")
        # No signing, hooks or identity from the user's own git config.
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_AUTHOR_NAME"] = environment["GIT_COMMITTER_NAME"] = "test"
        environment["GIT_AUTHOR_EMAIL"] = environment["GIT_COMMITTER_EMAIL"] = "test@example.com"
        environment.update(extra)
        if "BUILDLOG_MEMINFO" in extra:
            self.admission.readings.host_path = Path(extra["BUILDLOG_MEMINFO"])
        return environment

    def git(self, *args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.environment(), capture_output=True, text=True, check=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS
        )
        return result.stdout.strip()

    def record(
        self, *args: str, cwd: Path | None = None, extra: dict[str, str] | None = None, input: str = ""
    ) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return self.record_process(*args, cwd=cwd, extra=extra, input=input)
        module = step_context if args and args[0] == "step-context" else recorder
        arguments = list(args)
        with mock.patch.object(recorder, "detach"), mock.patch.object(sys, "stdin", io.StringIO(input)):
            return invoke_cli(module, lambda: module.main(arguments), arguments,
                              self.environment(**(extra or {})), cwd or self.repo)

    def record_process(
        self, *args: str, cwd: Path | None = None, extra: dict[str, str] | None = None, input: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT.resolve().parents[1] / "lib/py"),
             str(CONTEXT_SCRIPT if args and args[0] == "step-context" else SCRIPT), *args],
            cwd=cwd or self.repo,
            input=input,
            env=self.environment(**(extra or {})),
            capture_output=True,
            text=True,
            check=False,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def step(
        self, status: int, tty: str, handoff: str, peak: str, argv: list[str], extra: dict[str, str] | None = None,
        *, cwd: Path | None = None
    ) -> Record:
        result = self.record(
            "step", str(status), str(START), str(START + 12.5), tty, handoff, peak, *argv, extra=extra, cwd=cwd
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [row for row in self.records() if row.get("kind") == "step"][-1]

    def records(self) -> list[Record]:
        found: list[Record] = []
        for path in sorted(self.root.glob("*/*.jsonl")):
            found += [cast(Record, json.loads(line)) for line in path.read_text().splitlines()]
        return found

    def test_reserved_test_phase_row_keeps_the_sampler_reservation_with_or_without_charge(self) -> None:
        reservation = "sampled-reservation"
        charge: recorder.TestPhaseChargeUse = {
            "bytes": 8192, "selected_at": START, "reservation_id": reservation, "need": {},
        }
        history = self.root / "admission/anon_peaks.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        for has_charge in (False, True):
            with self.subTest(has_charge=has_charge):
                _ = history.write_text(json.dumps({
                    "reservation_id": reservation,
                    "test_phase_charge_use": charge if has_charge else None,
                }) + "\n")
                result = self.record("test-phase-context", json.dumps({"reservation_id": reservation}), *NEXTEST)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = cast(Record, json.loads(result.stdout))
                phase = self.base / f"reserved-phase-{has_charge}.json"
                _ = phase.write_text(json.dumps({
                    **context, "test_phase_peak_bytes": 4096, "test_phase_started_at": START,
                }))
                row = self.step(0, "1", "", "", NEXTEST, {"BUILDLOG_TEST_PHASE_FILE": str(phase)})
                self.assertEqual(row["test_phase_charge"], charge if has_charge else None)
                self.assertEqual(row["reservation_id"], reservation)

    def test_test_phase_without_a_text_reservation_omits_the_sidecar_and_row_key(self) -> None:
        contexts: tuple[Record, ...] = ({}, {"reservation_id": ""}, {"reservation_id": None}, {"reservation_id": 7})
        for case, sampling_context in enumerate(contexts):
            with self.subTest(sampling_context=sampling_context):
                result = self.record("test-phase-context", json.dumps(sampling_context), *NEXTEST)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = cast(Record, json.loads(result.stdout))
                phase = self.base / f"unreserved-phase-{case}.json"
                _ = phase.write_text(json.dumps({
                    **context, "test_phase_peak_bytes": 4096, "test_phase_started_at": START,
                }))
                row = self.step(0, "1", "", "", NEXTEST, {"BUILDLOG_TEST_PHASE_FILE": str(phase)})
                self.assertNotIn("reservation_id", row)
                self.assertNotIn("reservation_id", context)

    def test_step_records_resolved_target_and_build_directories(self) -> None:
        cargo = self.base / "tools" / "cargo"
        target, build = self.repo / "artifacts", self.base / "intermediates"
        payload = json.dumps({"workspace_root": str(self.repo), "target_directory": str(target),
                              "build_directory": str(build)})
        _ = cargo.write_text("#!/bin/sh\nif [ \"$1\" = metadata ]; then\n  printf '%s\\n' '" + payload + "'\nelse\n  exit 99\nfi\n")
        cargo.chmod(0o755)
        row = self.step(0, "1", "", "", NEXTEST, {"BUILDLOG_TARGET_FOLDER": str(target)})
        self.assertTrue(row["target_dir"] == row["target_directory"] == str(target))
        self.assertEqual(row["build_directory"], str(build))
        _ = index.update()
        with closing(index.read_only()) as connection:
            indexed = connection.execute(
                "SELECT target_folder, build_history_group, target_directory, build_directory FROM steps"
            ).fetchall()
        self.assertEqual(indexed, [(str(target), "folder:" + str(target), str(target), str(build))])

    def test_floor_scan_records_filesystem_identity_without_changing_old_rows(self) -> None:
        recorder.floor_scan(1, 2, [], START, filesystem="8-1:/outputs")
        row, = self.records()
        self.assertEqual(row["filesystem"], "8-1:/outputs")
        self.assertEqual(row["shortfall"], 1)

    def test_package_and_script_entry_points_work_without_pythonpath(self) -> None:
        repository = SCRIPT.resolve().parents[2]
        workspace = self.base / "cargo-workspace"
        workspace.mkdir()
        _ = (workspace / "Cargo.toml").write_text('[workspace]\nmembers = []\n', encoding="utf-8")
        (workspace / "scripts").symlink_to(repository / "scripts", target_is_directory=True)
        environment = self.environment(CARGO_HOME=str(self.base / "cargo-home"))
        _ = environment.pop("PYTHONPATH", None)
        for statement in ("from scripts.test_times import gate_record", "import scripts.buildlog.record"):
            with self.subTest(entry_point=statement):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", statement], cwd=repository, env=environment,
                    capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for entry_point in (["-m", "scripts.buildlog.step_context"], [str(CONTEXT_SCRIPT.resolve())]):
            with self.subTest(entry_point=entry_point):
                result = subprocess.run(
                    [str(repository / "scripts/lib/py"), "-B", *entry_point, "step-context", "cargo", "check"],
                    cwd=workspace, env=environment, input="", capture_output=True, text=True,
                    check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(len(lines), 2, result.stdout + result.stderr)
                self.assertEqual(lines[1], str(workspace / "target"))
        result = self.record_process(cwd=workspace)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("record.py step STATUS START END TTY LOG PEAK ARGV...", result.stderr)

    def folder_context(self, argv: list[str], *, extra: dict[str, str] | None = None,
                       cwd: Path | None = None, metadata: str = "") -> str:
        result = self.record("step-context", *argv, cwd=cwd, input=metadata,
                             extra={"CARGO_HOME": str(self.base / "cargo-home"), **(extra or {})})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout + result.stderr)
        self.assertNotEqual(lines[1], "")
        return lines[1]

    def assert_folder_record(self, argv: list[str], expected: str, *, extra: dict[str, str] | None = None,
                             cwd: Path | None = None, metadata: str = "") -> Record:
        captured = self.folder_context(argv, extra=extra, cwd=cwd, metadata=metadata)
        errors = self.root / "errors.log"
        self.assertEqual(captured, expected, errors.read_text() if errors.exists() else "")
        row = self.step(0, "1", "", "", argv,
                        {**(extra or {}), "BUILDLOG_TARGET_FOLDER": captured}, cwd=cwd)
        self.assertEqual(row["target_dir"], expected)
        return row

    def resolved_folder(self, argv: list[str], *, extra: dict[str, str] | None = None,
                        cwd: Path | None = None, metadata: str = "") -> str:
        environment = self.environment(CARGO_HOME=str(self.base / "cargo-home"), **(extra or {}))
        return step_context.target_folder_text(step_context.resolve_target_folder(
            argv, str(cwd or self.repo), environment, metadata))

    def test_each_cargo_step_records_its_cli_folder(self) -> None:
        for kind in ("build", "check", "clippy", "mend", "doc", "nextest"):
            with self.subTest(kind=kind):
                folder = str(self.repo / (kind + " output"))
                self.assertEqual(self.resolved_folder(["cargo", kind, "--target-dir", folder],
                                                     extra={"CARGO_TARGET_DIR": "ignored"}), folder)

    def test_environment_precedence_and_relative_paths_reach_the_writer(self) -> None:
        cases = (
            ({"CARGO_TARGET_DIR": "relative", "CARGO_BUILD_TARGET_DIR": "lower"}, self.repo / "relative"),
            ({"CARGO_BUILD_TARGET_DIR": str(self.base / "absolute")}, self.base / "absolute"),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment):
                self.assertEqual(self.resolved_folder(NEXTEST, extra=environment), str(expected))

    def test_target_flag_equals_stops_at_separator_and_uses_selected_toolchain(self) -> None:
        self.assertEqual(self.resolved_folder(["/usr/bin/cargo", "+nightly", "clippy", "--target-dir=chosen",
                                               "--", "--target-dir=ignored"]), str(self.repo / "chosen"))

    def test_env_wrapper_assignments_removals_and_directory_select_the_folder(self) -> None:
        other = self.base / "directory with spaces"
        other.mkdir()
        self.assertEqual(self.resolved_folder(["/usr/bin/env", "-u", "CARGO_TARGET_DIR", "-C", str(other),
                                               "CARGO_BUILD_TARGET_DIR=wrapped", "cargo", "check"],
                                              extra={"CARGO_TARGET_DIR": "ignored"}), str(other / "wrapped"))

    def test_config_ancestors_override_cargo_home_with_their_own_relative_base(self) -> None:
        cargo_home = self.base / "cargo-home"
        cargo_home.mkdir()
        _ = (cargo_home / "config.toml").write_text('[build]\ntarget-dir = "home-output"\n')
        self.assertEqual(self.resolved_folder(NEXTEST), str(self.base / "home-output"))
        config = self.repo / ".cargo"
        config.mkdir()
        _ = (config / "config.toml").write_text('[build]\ntarget-dir = "repo-output"\n')
        member = self.repo / "crates" / "member"
        member.mkdir(parents=True)
        _ = self.assert_folder_record(NEXTEST, str(self.repo / "repo-output"), cwd=member)
        self.assertEqual(self.resolved_folder(NEXTEST, cwd=member, extra={"CARGO_TARGET_DIR": "env-output"}),
                         str(member / "env-output"))

    def test_existing_metadata_and_known_folder_avoid_cargo_discovery(self) -> None:
        metadata = json.dumps({"workspace_root": str(self.repo), "target_directory": str(self.base / "metadata")})
        self.assertEqual(self.resolved_folder(["/missing/cargo", "build"], metadata=metadata), str(self.base / "metadata"))
        self.assertEqual(self.resolved_folder(["/missing/cargo", "nextest", "run"],
                                             extra={"BUILDLOG_KNOWN_TARGET_DIR": str(self.base / "known")}),
                         str(self.base / "known"))

    def test_workspace_default_reads_manifests_for_members_and_external_manifests(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text('[workspace]\nmembers = ["member"]\nresolver = "2"\n')
        member = self.repo / "member"
        member.mkdir()
        _ = (member / "Cargo.toml").write_text('[package]\nname = "member"\nversion = "0.1.0"\n')
        (member / "src").mkdir()
        _ = (member / "src/lib.rs").write_text("")
        _ = self.assert_folder_record(["/missing/cargo", "check"], str(self.repo / "target"), cwd=member)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        _ = self.assert_folder_record(["/missing/cargo", "check", "--manifest-path=" + str(member / "Cargo.toml")],
                                      str(self.repo / "target"), cwd=elsewhere)
        metadata = json.dumps({"workspace_root": str(elsewhere), "target_directory": str(elsewhere / "wrong")})
        _ = self.assert_folder_record(["/missing/cargo", "check"], str(self.repo / "target"), cwd=member, metadata=metadata)

    def test_external_package_workspace_redirect_selects_the_workspace_default(self) -> None:
        member = self.base / "redirected-package"
        (member / "src").mkdir(parents=True)
        _ = (member / "src/lib.rs").write_text("")
        _ = (member / "Cargo.toml").write_text(
            '[package]\nname = "redirected"\nversion = "0.1.0"\nworkspace = "../hana"\n')
        _ = (self.repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["../redirected-package"]\nresolver = "2"\n')
        _ = self.assert_folder_record(["/missing/cargo", "check"], str(self.repo / "target"), cwd=member)

    def test_workspace_default_reads_manifests_without_starting_cargo(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("Cargo must not start")):
            self.assertEqual(self.resolved_folder(["/missing/cargo", "+nightly", "check"]), str(self.repo / "target"))
        _ = self.assert_folder_record(["/missing/cargo", "check"], str(self.repo / "target"))

    def test_env_separator_keeps_assignments_before_the_cargo_command(self) -> None:
        argv = ["env", "--", "CARGO_TARGET_DIR=/x", "cargo", "check"]
        self.assertEqual(self.resolved_folder(argv), "/x")
        _ = self.assert_folder_record(argv, "/x")

    @process_boundary
    def test_changed_target_environment_rejects_the_callers_metadata(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text('[workspace]\n')
        metadata = json.dumps({"workspace_root": str(self.repo), "target_directory": "/stale"})
        for variable in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
            for wrapper in (["-u", variable], [variable + "="], [variable + "=changed"]):
                with self.subTest(variable=variable, wrapper=wrapper):
                    argv = ["env", *wrapper, "cargo", "check"]
                    expected = self.repo / ("changed" if wrapper == [variable + "=changed"] else "target")
                    self.assertEqual(self.resolved_folder(argv, extra={variable: "/stale"}, metadata=metadata), str(expected))
        _ = self.assert_folder_record(["env", "-u", "CARGO_TARGET_DIR", "cargo", "check"], str(self.repo / "target"),
                                      extra={"CARGO_TARGET_DIR": "/stale", "BUILDLOG_KNOWN_TARGET_DIR": "/stale"},
                                      metadata=metadata)

    def test_unresolved_steps_never_read_or_establish_warm_history(self) -> None:
        argv = ["cargo", "check", "--manifest-path", str(self.base / "missing/Cargo.toml")]
        captured = self.folder_context(argv)
        self.assertEqual(captured, "unresolved")
        for number in range(2):
            handoff = self.base / f"build-{number}.log"
            _ = handoff.write_text('    Finished `dev` profile target(s) in 1.0s\n')
            result = self.record("step", "0", str(START + number * 20), str(START + number * 20 + 1),
                                 "0", str(handoff), "", *argv, extra={"BUILDLOG_TARGET_FOLDER": captured})
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.records()[-1]
            self.assertEqual(row["target_dir"], "unresolved")
            self.assertEqual(row["finished_s"], 1.0)
        _ = index.update()
        with closing(index.read_only()) as connection:
            rows = connection.execute("SELECT target_folder, build_history_group, " + report.warm_step_clause()
                                      + " FROM steps").fetchall()
        self.assertEqual(rows, [(None, None, 0), (None, None, 0)])

    def test_steps_without_cargo_output_record_none_despite_inherited_folder(self) -> None:
        for argv in (["cargo", "+nightly", "fmt", "--all"], ["taplo", "fmt"], SWEEP, ["python3", "tool.py"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.resolved_folder(argv, extra={"CARGO_TARGET_DIR": "inherited"}), "none")
        fields: Record = {"pid": 123, "port": 15803, "binary": "/tmp/existing-app",
                          "started": "2026-10-02T12:00:00+00:00"}
        source = self.base / "launch.json"
        _ = source.write_text(json.dumps(fields))
        self.assertTrue(recorder.launch(source, self.repo))
        self.assertEqual(self.records()[-1]["target_dir"], "none")

    def test_malformed_config_records_unresolved_and_an_error(self) -> None:
        config = self.repo / ".cargo"
        config.mkdir()
        _ = (config / "config.toml").write_text('[build\n')
        _ = self.assert_folder_record(NEXTEST, "unresolved")
        self.assertIn("target folder:", (self.root / "errors.log").read_text())

    def test_invalid_cli_config_records_unresolved_and_an_error(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        malformed = self.base / "malformed.toml"
        _ = malformed.write_text('[build\n')
        unreadable = self.base / "config-directory"
        unreadable.mkdir()
        for setting in ('build.target-dir="broken', str(malformed), str(unreadable), str(self.base / "missing.toml")):
            with self.subTest(setting=setting):
                self.assertEqual(self.resolved_folder(["cargo", "check", "--config", setting]), "unresolved")
                self.assertIn("target folder:", (self.root / "errors.log").read_text())

    def test_target_environment_overrides_cli_config_in_capture(self) -> None:
        argv = ["cargo", "check", "--config", 'build.target-dir="configured-output"']
        for variable, expected in (("CARGO_TARGET_DIR", "environment-output"),
                                   ("CARGO_BUILD_TARGET_DIR", "configured-output")):
            with self.subTest(variable=variable):
                self.assertEqual(self.resolved_folder(argv, extra={variable: "environment-output"}),
                                 str(self.repo / expected))

    def test_captured_folder_survives_config_change_and_pending_retry(self) -> None:
        config = self.repo / ".cargo"
        config.mkdir()
        file = config / "config.toml"
        _ = file.write_text('[build]\ntarget-dir = "first"\n')
        captured = self.folder_context(NEXTEST)
        self.assertEqual(captured, str(self.repo / "first"))
        _ = file.write_text('[build]\ntarget-dir = "second"\n')
        environment = self.environment(BUILDLOG_TARGET_FOLDER=captured)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(os, "getcwd", return_value=str(self.repo)), \
                mock.patch.object(recorder, "write_step_line", side_effect=OSError("delayed publication")):
            recorder.step(["0", str(START), str(START + 1), "1", "", "", *NEXTEST])
        pending = list(recorder.pending_steps_root().glob("*.json"))
        self.assertEqual(len(pending), 1)
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "changed"}):
            _ = recorder.finish_pending_step(pending[0])
        self.assertEqual(self.records()[-1]["target_dir"], captured)
        self.assertEqual(self.folder_context(NEXTEST), str(self.repo / "second"))

    def test_shell_completion_branches_preserve_the_folder_and_refresh_saved_arguments(self) -> None:
        invoke = SCRIPT.parent.parent / "lint/invoke.sh"
        verify = SCRIPT.parent.parent / "direct/verify.sh"

        shell = "\n".join(shell_function(invoke.read_text(), name) for name in
                          ("buildlog_read_context", "buildlog_now", "buildlog_begin", "buildlog_write_step", "buildlog_end"))
        shell += "\n" + shell_functions(verify, "set_verdict_keys", "refresh_test_build_inputs")
        shell += 'tree_key() { printf key; }\n'
        home = self.base / "shell-home"
        launcher = home / ".claude/scripts/lib/py"
        launcher.parent.mkdir(parents=True)
        socket_root = Path(self.enterContext(short_socket_directory()))
        address = socket_root / "completion"
        listener = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        listener.bind(str(address))
        listener.settimeout(test_hang_guard.TEST_HANG_GUARD_SECS)
        _ = launcher.write_text('#!' + sys.executable + '\nimport socket, subprocess, sys\n'
                                + 'result = subprocess.run([sys.executable, *sys.argv[1:]], check=False)\n'
                                + 'if sys.argv[2] == "step":\n'
                                + '    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:\n'
                                + f'        channel.sendto(b"done", {str(address)!r})\n'
                                + 'raise SystemExit(result.returncode)\n')
        launcher.chmod(0o755)
        hold = self.base / "hold-context"
        hold.mkdir()
        command = shell + r'''
export CARGO_TARGET_DIR="$TEST_FIRST_FOLDER"
buildlog_begin cargo check
export CARGO_TARGET_DIR="$TEST_CHANGED_FOLDER"
buildlog_end 0 "" 0 cargo check
export CARGO_TARGET_DIR="$TEST_FIRST_FOLDER"
buildlog_begin cargo check --target-dir refresh-output
refresh_test_build_inputs
printf '%s\n' "$BUILDLOG_TARGET_FOLDER"
unset CARGO_TARGET_DIR
GATE_METADATA="$TEST_METADATA"
buildlog_read_context /missing/cargo check
printf '%s\n' "$BUILDLOG_TARGET_FOLDER"
GATE_METADATA="" BUILD_TARGET_DIR="$TEST_KNOWN_FOLDER"
buildlog_read_context /missing/cargo build
printf '%s\n' "$BUILDLOG_TARGET_FOLDER"
BUILD_TARGET_DIR="" BUILDLOG_TEST_TARGET_DIR="$TEST_KNOWN_FOLDER"
buildlog_read_context /missing/cargo nextest run
printf '%s\n' "$BUILDLOG_TARGET_FOLDER"
'''
        completion, refresh = command.split('export CARGO_TARGET_DIR="$TEST_FIRST_FOLDER"', 2)[1:]
        environment = self.environment(HOME=str(home), BUILDLOG_RECORD=str(SCRIPT),
            BUILDLOG_SCOPE="0", PY=sys.executable, HOLD_WAIT_DIR=str(hold),
            TEST_FIRST_FOLDER=str(self.repo / "first"), TEST_CHANGED_FOLDER=str(self.repo / "changed"),
            TEST_METADATA=json.dumps({"workspace_root": str(self.repo),
                                      "target_directory": str(self.repo / "metadata-output")}),
            TEST_KNOWN_FOLDER=str(self.repo / "known-output"))
        for sync in ("1", "0"):
            with self.subTest(sync=sync):
                first = str(self.repo / f"first-{sync}")
                environment.update(BUILDLOG_SYNC=sync, TEST_FIRST_FOLDER=first)
                result = subprocess.run(["bash", "-eu", "-c", shell + 'export CARGO_TARGET_DIR="$TEST_FIRST_FOLDER"' + completion], cwd=self.repo, env=environment,
                                        capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(listener.recv(32), b"done")
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.records()[-1]["target_dir"], first)

        result = subprocess.run(["bash", "-eu", "-c", shell + 'export CARGO_TARGET_DIR="$TEST_FIRST_FOLDER"' + refresh],
                                cwd=self.repo, env=environment, capture_output=True, text=True,
                                timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.repo / "refresh-output"),
            str(self.repo / "metadata-output"), str(self.repo / "known-output"), str(self.repo / "known-output")])

    def test_older_callers_without_a_captured_folder_record_unresolved(self) -> None:
        argv = ["cargo", "check", "--target-dir", "older-output"]
        row = self.step(0, "1", "", "", argv, {"CARGO_TARGET_DIR": "ignored"})
        self.assertEqual(row["target_dir"], "unresolved")

    def test_floor_scan_records_free_space_shortfall_and_the_idle_target_inventory(self) -> None:
        targets: list[recorder.IdleFloorTarget] = [
            {"folder": "/trees/older/target", "last_use": START - 7200, "bytes": 123456},
            {"folder": "/trees/recent/target", "last_use": START - 60, "bytes": 789012},
        ]
        recorder.floor_scan(7 << 30, 10 << 30, targets, START)
        row, = self.records()
        self.assertEqual((row["kind"], row["v"], row["host"], row["at"]),
                         ("floor_scan", recorder.RECORD_VERSION, store.host_name(), store.utc_iso(START)))
        self.assertEqual((row["free_after_scan"], row["floor"], row["shortfall"]),
                         (7 << 30, 10 << 30, 3 << 30))
        self.assertEqual(row["idle_targets"], targets)
        self.assertIsInstance(row["id"], str)

    def test_floor_scan_preserves_takeable_bytes_and_reason_only_where_supplied(self) -> None:
        targets: list[recorder.IdleFloorTarget] = [
            {"folder": "/trees/whole/target", "last_use": START - 7200, "bytes": 65536},
            {"folder": "/trees/linked/target", "last_use": START - 60, "bytes": 65536,
             "takeable_bytes": 0, "reason": "links outside the target"},
        ]
        recorder.floor_scan(7 << 30, 10 << 30, targets, START)
        row, = self.records()
        self.assertEqual(row["idle_targets"], [
            {"folder": "/trees/whole/target", "last_use": START - 7200, "bytes": 65536},
            {"folder": "/trees/linked/target", "last_use": START - 60, "bytes": 65536,
             "takeable_bytes": 0, "reason": "links outside the target"},
        ])
        self.assertEqual(row["shortfall"], 3 << 30)

    def test_queue_priority_events_keep_action_and_user_scope_in_the_build_log(self) -> None:
        at = float(START)
        for offset, action in enumerate(("set", "set", "clear")):
            recorder.queue_priority(action, "resources", "finish the queue", "showrunner-id", at + offset)
        rows = self.records()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["action"] for row in rows], ["set", "set", "clear"])
        for offset, row in enumerate(rows):
            self.assertEqual((row["kind"], row["v"], row["host"], row["at"]),
                ("queue_priority", recorder.RECORD_VERSION, store.host_name(), store.utc_iso(at + offset)))
            self.assertEqual((row["unit"], row["why"], row["set_by"]),
                ("resources", "finish the queue", "showrunner-id"))
            self.assertNotIn("until", row)
            self.assertIsInstance(row["id"], str)
        self.assertEqual(len({row["id"] for row in rows}), 3)

    def test_retried_priority_event_keeps_its_transition_id_and_one_row(self) -> None:
        for action in ("set", "clear"):
            for attempt in range(2):
                recorder.queue_priority(action, "resources", "retry", "showrunner-id",
                    START + attempt, transition_id=f"priority-{action}")
        rows = self.records()
        self.assertEqual([row["action"] for row in rows], ["set", "clear"])
        self.assertEqual([row["id"] for row in rows], ["priority-set", "priority-clear"])
        self.assertTrue(all(row["at"] == store.utc_iso(START) for row in rows))

    def test_priority_append_retries_from_its_saved_offset_without_reading_the_old_log(self) -> None:
        path = store.host_file(store.host_name(), START)
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = b"old month content is not JSON\n" * 128
        _ = path.write_bytes(prefix)
        append = store.append_locked

        def stop_after_append(handle: BinaryIO, row: object) -> None:
            append(handle, row)
            raise OSError("fixture stopped after priority append")

        with mock.patch.object(json, "loads", wraps=json.loads) as decoded:
            with mock.patch.object(store, "append_locked", side_effect=stop_after_append), \
                 self.assertRaises(OSError):
                recorder.queue_priority("set", "resources", "finish work", "showrunner-id", START,
                    transition_id="bounded-priority", start=len(prefix))
            for attempt in range(2):
                recorder.queue_priority("set", "resources", "finish work", "showrunner-id", START + attempt + 1,
                    transition_id="bounded-priority", start=len(prefix))
            self.assertNotIn(mock.call(prefix.splitlines(keepends=True)[0]), decoded.call_args_list)
            self.assertNotIn(mock.call(prefix.splitlines(keepends=True)[0].decode()), decoded.call_args_list)
        contents = path.read_bytes()
        self.assertEqual(contents[:len(prefix)], prefix)
        row, = [cast(Record, json.loads(line)) for line in contents[len(prefix):].splitlines()]
        self.assertEqual((row["id"], row["action"], row["unit"], row["at"]),
                         ("bounded-priority", "set", "resources", store.utc_iso(START)))

    def handoff(self) -> Path:
        path = self.base / "handoff.log"
        _ = shutil.copyfile(TESTDATA / "nextest.txt", path)
        return path

    def peak_file(self) -> Path:
        path = self.base / "peak"
        _ = path.write_text("123456\n")
        return path

    @process_boundary
    def test_step_record_fields(self) -> None:
        handoff, peak = self.handoff(), self.peak_file()
        record = self.step(1, "0", str(handoff), str(peak), NEXTEST, {"BUILDLOG_CALL_ID": CALL_ID})
        self.assertEqual(record["kind"], "step")
        self.assertEqual(record["step"], "nextest")
        self.assertEqual(record["rustc"], FIXED_RUSTC)
        self.assertEqual(record["status"], 1)
        self.assertEqual(record["started_at"], "2026-10-02T12:00:00.000Z")
        self.assertEqual(record["duration_s"], 12.5)
        self.assertEqual(record["argv"], NEXTEST)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertEqual(record["call_id"], CALL_ID)
        self.assertEqual(record["caller"], "unknown")
        self.assertIs(record["tty"], False)
        self.assertEqual((record["tests_run"], record["tests_failed"], record["tests_flaky"]), (5, 1, 1))
        tests = cast(list[Record], record["tests"])
        self.assertEqual(
            sorted((row["test"], row["status"]) for row in tests),
            [("tests::fails", "failed"), ("tests::flaky", "flaky"), ("tests::slow", "passed")],
        )
        self.assertEqual(record["repo_path"], str(self.repo))
        self.assertEqual(record["worktree"], str(self.repo))
        self.assertEqual(record["branch"], "main")
        self.assertEqual(record["sha"], self.git("rev-parse", "--short", "HEAD", cwd=self.repo))
        host = socket.gethostname().split(".")[0]
        self.assertEqual(record["host"], host)
        self.assertRegex(str(record["id"]), rf"^{re.escape(host)}-20261002T120000-[0-9a-f]{{6}}$")
        # A failed step keeps its output, gzipped, beside the records.
        log = record["log"]
        self.assertIsInstance(log, str)
        self.assertEqual(gzip.decompress((self.root / str(log)).read_bytes()), (TESTDATA / "nextest.txt").read_bytes())
        self.assertFalse(handoff.exists())
        self.assertFalse(peak.exists())

    def test_scope_capture_records_cpu_and_optional_io_before_scope_disappears(self) -> None:
        for with_io in (False, True):
            with self.subTest(with_io=with_io):
                scope = self.base / f"scope-{with_io}"
                write_cgroup(scope, with_io=with_io)
                peak = self.peak_file().rename(self.base / f"peak-{with_io}")
                sidecar = Path(str(peak) + ".cgroup")
                _ = sidecar.write_text(str(scope) + "\n")
                captured = save_scope_counters(peak, scope)
                self.assertEqual(captured.returncode, 0, captured.stderr)
                sidecar.unlink()
                shutil.rmtree(scope)
                row = self.step(0, "0", "", str(peak), SWEEP)
                resources = cast(Record, row["scope_resources"])
                self.assertEqual(resources["cpu"], CPU)
                io = cast(Record, resources["io"])
                if with_io:
                    self.assertEqual(io["state"], "measured")
                    devices = cast(list[Record], io["devices"])
                    self.assertEqual([device["raw"] for device in devices], list(IO_LINES))
                else:
                    self.assertEqual(io, {"state": "unavailable"})
                self.assertFalse(Path(str(peak) + ".resources").exists())
                self.assertFalse(Path(str(peak) + ".cpu.stat").exists())
                self.assertFalse(Path(str(peak) + ".io.stat").exists())

    def test_step_records_cgroup_memory_stall_seconds(self) -> None:
        peak = self.base / "peak-pressure"
        _ = peak.write_text("\n".join([
            "123456",
            "some avg10=0.00 avg60=0.01 avg300=0.02 total=1250000",
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=250000",
            "",
        ]))
        record = self.step(0, "0", "", str(peak), SWEEP)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertEqual(record["mem_stall_some_s"], 1.25)
        self.assertEqual(record["mem_stall_full_s"], 0.25)
        self.assertFalse(peak.exists())

    def test_step_records_memory_wait_separately_from_build_duration(self) -> None:
        record = self.step(0, "0", "", "", SWEEP, {"BUILDLOG_MEM_WAIT_S": "23"})
        self.assertEqual(record["mem_wait_s"], 23)
        self.assertEqual(record["duration_s"], 12.5)

    def test_piped_run_records_memory_wait_outside_step_duration(self) -> None:
        self.admission.configure(**{"program_first_peak_gib.cargo": 9})
        _ = self.admission.occupy_host()
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        runtime = Path(self.enterContext(short_socket_directory())) / "systemd"
        runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as manager:
            manager.bind(str(runtime / "private"))
            systemd_run = self.base / "systemd-run"
            _ = systemd_run.write_text(
                '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\n'
                + 'shift\npeak="$4"\nshift 4\nprintf "123\\n" > "$peak"\n"$@"\n'
            )
            systemd_run.chmod(0o755)
            command = (
                'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
                'buildlog_now() { '
                'if [[ -e "$TEST_STEP_FINISHED" ]]; then BUILDLOG_NOW=1790942507; '
                'elif [[ "$(< "$BUILDLOG_MEMINFO")" == "MemAvailable: 12582912 kB" ]]; '
                'then BUILDLOG_NOW=1790942500; else BUILDLOG_NOW=1790942400; fi; }; '
                "run_once bash -c 'printf \"step\\n\"; touch \"$TEST_STEP_FINISHED\"'"
            )
            process = subprocess.Popen(
                ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT)],
                cwd=self.repo,
                env=self.environment(
                    PATH=f"{self.base}:{os.environ['PATH']}",
                    XDG_RUNTIME_DIR=str(runtime.parent),
                    BUILDLOG_OFF="0", BUILDLOG_SCOPE="1",
                    BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="5",
                    BUILDLOG_SYNC="1", TEST_STEP_FINISHED=str(self.base / "step-finished"),
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                assert process.stderr is not None
                readable, _, _ = select.select([process.stderr], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertTrue(readable, "memory wait message did not arrive")
                first_message = cast(str, process.stderr.readline())
                self.assertIn("waiting for memory since ", first_message)
                _ = threading.Event().wait(1.1)
                _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
                output, remaining_errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                errors = first_message + remaining_errors
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, errors)
        self.assertIn("step", output)
        self.assertEqual(errors.count("waiting for memory since "), 1)
        self.assertNotIn("memory wait limit reached", errors)
        record = self.records()[-1]
        wait = record["mem_wait_s"]
        duration = record["duration_s"]
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00")).timestamp()
        assert isinstance(wait, int)
        assert isinstance(duration, (int, float))
        self.assertGreaterEqual(wait, 1)
        self.assertEqual(started, 1790942500)
        self.assertEqual(duration, 7)

    def test_launch_memory_wait_limit_refuses_once(self) -> None:
        self.admission.configure(**{"program_first_peak_gib.cargo": 9})
        _ = self.admission.occupy_host()
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11010048 kB\n")
        command = ('source "$1"; sleep() { SECONDS=$((SECONDS + 1)); }; '
                   'status=0; buildlog_wait_for_memory || status=$?; '
                   'printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"; exit "$status"')
        environment = self.environment(BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="1")
        result = subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
            env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, r"waiting for memory since \d\d:\d\d P[DS]T: the machine has 10\.5 GiB free; a build starts at 12\.0")
        self.assertEqual(result.stderr.count("waiting for memory since "), 1)
        reason = "memory admission check failed (launch wait limit reached without admission); step refused"
        self.assertEqual(result.stderr.count(reason), 1)
        self.assertNotIn("starting", result.stderr)
        self.assertGreaterEqual(int(result.stdout.strip()), 1)

    def test_memory_admission_refuses_invalid_readings_and_allows_at_floor(self) -> None:
        command = 'set -e; source "$1"; buildlog_wait_for_memory; printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        cases = {
            "missing": self.base / "missing-meminfo",
            "unreadable": self.base / "directory",
            "no available line": self.base / "without-available",
            "non-numeric available": self.base / "invalid-available",
            "at floor": self.base / "at-floor",
            "above floor": self.base / "above-floor",
        }
        cases["unreadable"].mkdir()
        _ = cases["no available line"].write_text("MemFree: 1000 kB\n")
        _ = cases["non-numeric available"].write_text("MemAvailable: unknown kB\n")
        _ = cases["at floor"].write_text("MemAvailable: 12582912 kB\n")
        _ = cases["above floor"].write_text("MemAvailable: 13631488 kB\n")
        for name, meminfo in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
                    env=self.environment(BUILDLOG_MEMINFO=str(meminfo)),
                    capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                if name in ("at floor", "above floor"):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout.strip(), "0")
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, f"memory admission check failed (cannot read MemAvailable from {meminfo}); step refused\n")
                    self.assertEqual(result.stdout, "")

    def test_memory_admission_does_not_count_a_slow_read_above_floor(self) -> None:
        meminfo = self.base / "meminfo-fifo"
        os.mkfifo(meminfo)
        opened = threading.Event()
        read_text = Path.read_text

        def bounded_fifo_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path != meminfo:
                return read_text(path, encoding=encoding, errors=errors)
            descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            try:
                opened.set()
                if not select.select([descriptor], [], [], 5)[0]:
                    raise TimeoutError("the meminfo FIFO received no sample within 5 seconds")
                value = os.read(descriptor, 4096).decode()
                sample_path = self.base / "fifo-sample"
                _ = sample_path.write_text(value)
                self.admission.readings.host_path = sample_path
                return value
            finally:
                os.close(descriptor)

        _ = self.enterContext(mock.patch.object(Path, "read_text", autospec=True, side_effect=bounded_fifo_read))
        command = 'source "$1"; SECONDS=0; printf "ready\\n"; buildlog_wait_for_memory; printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        process = subprocess.Popen(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
            env=self.environment(BUILDLOG_MEMINFO=str(meminfo)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "the shell did not start within 5 seconds")
            self.assertEqual(process.stdout.readline(), "ready\n")
            self.assertTrue(opened.wait(5), "the private server did not read the FIFO within 5 seconds")
            _ = threading.Event().wait(1.1)
            descriptor = os.open(meminfo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                _ = os.write(descriptor, b"MemAvailable: 12582912 kB\n")
            finally:
                os.close(descriptor)
            output, errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(errors, "")
        self.assertEqual(output.strip(), "0")

    def test_sweep_step_starts_without_memory_wait(self) -> None:
        sweep = self.base / "sweep.py"
        _ = sweep.write_text('print("sweep started")\n')
        self.assert_step_starts_without_memory_wait([sys.executable, str(sweep)], "sweep started")

    def test_cargo_fmt_step_starts_without_memory_wait(self) -> None:
        cargo = self.base / "cargo"
        _ = cargo.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            + 'fmt|"+nightly fmt") printf "fmt started\\n" ;;\n'
            + '*) echo "Unexpected cargo call: $*" >&2; exit 99 ;;\nesac\n'
        )
        cargo.chmod(0o755)
        for toolchain in ([], ["+nightly"]):
            with self.subTest(toolchain=toolchain):
                self.assert_step_starts_without_memory_wait(["cargo", *toolchain, "fmt"], "fmt started")
                self.assertEqual(self.records()[-1]["rustc"], FIXED_RUSTC)

    def assert_step_starts_without_memory_wait(self, argv: list[str], output_line: str) -> None:
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        command = (
            'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
            'shift 2; run_once "$@"; printf "wait=%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        )
        result = subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT), *argv],
            cwd=self.repo,
            env=self.environment(PATH=f"{self.base}:{os.environ['PATH']}", BUILDLOG_OFF="0", BUILDLOG_SCOPE="0",
                                 BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1",
                                 BUILDLOG_MEM_WAIT_LIMIT_S="0", BUILDLOG_SYNC="1"),
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(output_line, result.stdout)
        self.assertIn("wait=0", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.records()[-1]["mem_wait_s"], 0)

    def test_unscoped_step_waits_for_memory(self) -> None:
        self.admission.configure(**{"program_first_peak_gib.cargo": 9})
        _ = self.admission.occupy_host()
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        marker = self.base / "step-started"
        step = self.base / "step.sh"
        _ = step.write_text('#!/bin/sh\n: > "$STEP_MARKER"\nprintf "step\\n"\n')
        step.chmod(0o755)
        command = (
            'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
            'run_once "$3"; printf "wait=%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        )
        process = subprocess.Popen(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT), str(step)],
            cwd=self.repo,
            env=self.environment(BUILDLOG_OFF="0", BUILDLOG_SCOPE="0", BUILDLOG_MEMINFO=str(meminfo),
                                 BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="5", BUILDLOG_SYNC="1",
                                 STEP_MARKER=str(marker)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stderr is not None
            readable, _, _ = select.select([process.stderr], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)
            self.assertTrue(readable, "memory wait message did not arrive")
            first_message = cast(str, process.stderr.readline())
            self.assertIn("waiting for memory since ", first_message)
            self.assertFalse(marker.exists())
            _ = threading.Event().wait(1.1)
            _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
            released_at = meminfo.stat().st_mtime
            output, remaining_errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            errors = first_message + remaining_errors
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, errors)
        self.assertIn("step", output)
        self.assertRegex(output, r"wait=[1-9][0-9]*")
        self.assertNotIn("memory wait limit reached", errors)
        self.assertGreaterEqual(marker.stat().st_mtime, released_at)
        record = self.records()[-1]
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00")).timestamp()
        self.assertGreaterEqual(started, released_at - 0.01)
        self.assertEqual(record["mem_wait_s"], int(output.split("wait=")[-1].strip()))

    def test_scope_memory_uses_shared_pressure_parser_for_partial_counters(self) -> None:
        peak = self.base / "partial-pressure"
        _ = peak.write_text("123456\nsome avg10=0.00 total=1250000\n")
        memory = recorder.peak_and_stall(str(peak))
        self.assertEqual(memory.stall_some_s, 1.25)
        self.assertIs(memory.stall_full_s, sample.Unmeasured.VALUE)
        recorded = self.step(0, "0", "", str(peak), SWEEP)
        self.assertEqual(recorded["mem_stall_some_s"], 1.25)
        self.assertIsNone(recorded["mem_stall_full_s"])

    def test_step_accepts_older_peak_only_file_without_stalls(self) -> None:
        record = self.step(0, "0", "", str(self.peak_file()), SWEEP)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertIsNone(record.get("mem_stall_some_s"))
        self.assertIsNone(record.get("mem_stall_full_s"))

    def test_passing_step_keeps_no_log(self) -> None:
        handoff = self.handoff()
        record = self.step(0, "0", str(handoff), "", NEXTEST)
        self.assertIsNone(record["log"])
        self.assertIsNone(record["peak_mem_bytes"])
        self.assertIsNone(record.get("mem_stall_some_s"))
        self.assertIsNone(record.get("mem_stall_full_s"))
        self.assertEqual(record["tests_run"], 5)
        self.assertFalse(handoff.exists())
        self.assertEqual(list(self.root.glob("*/logs")), [])

    def test_tty_step_records_no_log_facts(self) -> None:
        handoff = self.handoff()
        record = self.step(1, "1", str(handoff), "", NEXTEST)
        self.assertIs(record["tty"], True)
        self.assertEqual(record["caller"], "alias")
        for fact in ("finished_s", "crates_compiled", "warnings", "errors", "tests_run", "tests", "log"):
            self.assertIsNone(record[fact], fact)
        self.assertFalse(handoff.exists())

    def test_caller_precedence(self) -> None:
        every = {
            "BUILDLOG_CALLER": "verify",
            "LINT_OUTPUT_DIR": "/tmp/lint",
            "VALIDATE_TARGET_DIR": "/tmp/validate",
            "CLAUDECODE": "1",
        }
        cases: list[tuple[dict[str, str], str, str]] = [
            (every, "0", "verify"),
            ({**every, "BUILDLOG_CALLER": ""}, "0", "cargo-port"),
            ({"VALIDATE_TARGET_DIR": "/tmp/validate", "CLAUDECODE": "1"}, "1", "validate_ci"),
            ({"CLAUDECODE": "1"}, "1", "agent"),
            ({"CODEX_THREAD_ID": "thread-9"}, "0", "agent"),
            ({}, "1", "alias"),
            ({}, "0", "unknown"),
        ]
        for extra, tty, expected in cases:
            with self.subTest(expected=expected, extra=extra):
                self.assertEqual(self.step(0, tty, "", "", SWEEP, extra)["caller"], expected)
        self.assertEqual(self.records()[4]["session"], "thread-9")

    def test_session_prefers_codex_thread_id(self) -> None:
        cases: list[tuple[dict[str, str], str | None]] = [
            ({"CODEX_THREAD_ID": "thread-9", "CLAUDE_CODE_SESSION_ID": "session-1"}, "thread-9"),
            ({"CLAUDE_CODE_SESSION_ID": "session-1"}, "session-1"),
            ({}, None),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment):
                record = self.step(0, "0", "", "", SWEEP, environment)
                self.assertEqual(record["session"], expected)

    def test_linked_worktree_and_detached_head(self) -> None:
        feature = self.base / "feature"
        _ = self.git("worktree", "add", "-q", "-b", "feature", str(feature), cwd=self.repo)
        record = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=feature)
        self.assertEqual(record.returncode, 0, record.stderr)
        linked = self.records()[-1]
        self.assertEqual((linked["repo_path"], linked["worktree"], linked["branch"]), (str(self.repo), str(feature), "feature"))
        self.assertEqual(linked["cwd"], str(feature))
        self.assertIsNone(linked["rustc"])

        _ = self.git("checkout", "-q", "--detach", cwd=self.repo)
        detached = self.step(0, "0", "", "", SWEEP)
        self.assertEqual(detached["branch"], "detached")
        self.assertEqual(detached["sha"], self.git("rev-parse", "--short", "HEAD", cwd=self.repo))

    def test_manifest_path_names_the_repo(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        argv = ["cargo", "clippy", "--manifest-path", str(self.repo / "Cargo.toml")]
        result = self.record("step", "0", str(START), "", "0", "", "", *argv, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual((record["cwd"], record["repo_path"], record["branch"]), (str(elsewhere), str(self.repo), "main"))

    def key(self, *argv: str, cwd: Path | None = None) -> str:
        result = self.record("step-context", *argv, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()[0]

    def test_tree_key_kept_only_when_the_files_held_still(self) -> None:
        key = self.key(*NEXTEST)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        held = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_TREE_START": key})
        self.assertEqual((held["tree_key"], held["tree_changed"]), (key, False))
        moved = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_TREE_START": "0" * 64})
        self.assertEqual((moved["tree_key"], moved["tree_changed"]), (None, True))
        unknown = self.step(0, "0", "", "", NEXTEST)
        self.assertEqual((unknown["tree_key"], unknown["tree_changed"]), (None, None))
        _ = (self.repo / "new.rs").write_text("\n")
        self.assertNotEqual(self.key(*NEXTEST), key)

    def test_step_without_a_command_keeps_unknown_repository_identity(self) -> None:
        _ = self.git("remote", "add", "origin", "https://example.org/local.git", cwd=self.repo)
        row = self.step(0, "0", "", "", [])
        self.assertEqual(row["repo_identity"], store.UNKNOWN_IDENTITY)

    def test_mirror_steps_record_the_sent_repository_identity_without_derivation(self) -> None:
        mirror = self.base / "mirror"
        mirror.mkdir()
        _ = self.git("init", "-q", "--template=", cwd=mirror)
        identity = "https://example.org/source"
        for caller in ("verify-mac", "verify-linux"):
            with self.subTest(caller=caller), mock.patch.dict(os.environ, {"BUILDLOG_REPO_IDENTITY": identity}):
                row = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_CALLER": caller}, cwd=mirror)
                self.assertEqual(row["repo_identity"], identity)
                _ = index.update()
                with closing(index.read_only()) as connection:
                    indexed = cast(tuple[str, int], connection.execute(
                        "SELECT repo_identity, identity_derived FROM steps WHERE id = ?", (row["id"],),
                    ).fetchone())
                self.assertEqual(indexed, (identity, 0))

    def test_ordinary_steps_ignore_the_sent_repository_identity(self) -> None:
        directory = self.base / "ordinary"
        directory.mkdir()
        with mock.patch.dict(os.environ, {"BUILDLOG_REPO_IDENTITY": "https://example.org/source"}):
            row = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_CALLER": "verify"}, cwd=directory)
        self.assertEqual(row["repo_identity"], store.UNKNOWN_IDENTITY)

    def test_mirror_steps_without_a_sent_identity_keep_unknown_identity(self) -> None:
        directory = self.base / "mirror"
        directory.mkdir()
        for caller in ("verify-mac", "verify-linux"):
            for sent in (None, "", " \t\n"):
                with self.subTest(caller=caller, sent=sent), mock.patch.dict(os.environ):
                    if sent is None:
                        if "BUILDLOG_REPO_IDENTITY" in os.environ:
                            del os.environ["BUILDLOG_REPO_IDENTITY"]
                    else:
                        os.environ["BUILDLOG_REPO_IDENTITY"] = sent
                    self.assertIsInstance(recorder.sent_repository_identity(), recorder.NoSentRepositoryIdentity)
                    call_id = f"{caller}-{sent!r}"
                    row = self.step(0, "0", "", "", NEXTEST, {
                        "BUILDLOG_CALLER": caller, "BUILDLOG_CALL_ID": call_id,
                    }, cwd=directory)
                    self.assertEqual(row["repo_identity"], store.UNKNOWN_IDENTITY)
                    recorder.ssh_attempt({
                        "call_id": call_id, "target": "mac" if caller == "verify-mac" else "aws",
                        "repo_identity": "https://example.org/source", "queued_at": START,
                        "departed_at": START + 1, "slot": 1, "outcome": "passed",
                        "completion": "confirmed", "cleanup": "confirmed", "unresolved_count": 0,
                        "evidence_at": START + 2,
                    })
                    _ = index.update()
                    with closing(index.read_only()) as connection:
                        indexed = cast(tuple[str, int], connection.execute(
                            "SELECT repo_identity, identity_derived FROM steps WHERE id = ?", (row["id"],),
                        ).fetchone())
                    self.assertEqual(indexed, ("https://example.org/source", 1))

    def test_known_local_repository_identity_wins_over_the_sent_identity(self) -> None:
        identity = "https://example.org/local"
        _ = self.git("remote", "add", "origin", identity + ".git", cwd=self.repo)
        for caller in ("verify-mac", "verify-linux"):
            with self.subTest(caller=caller), mock.patch.dict(
                os.environ, {"BUILDLOG_REPO_IDENTITY": "https://example.org/source"},
            ):
                row = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_CALLER": caller})
                self.assertEqual(row["repo_identity"], identity)

    def test_sent_repository_identity_strips_surrounding_space(self) -> None:
        with mock.patch.dict(os.environ, {"BUILDLOG_REPO_IDENTITY": "  https://example.org/source\n"}):
            self.assertEqual(recorder.sent_repository_identity(),
                             recorder.SentRepositoryIdentity("https://example.org/source"))

    def test_verify_mac_step_without_git_records_the_sent_tree_key_and_call_id(self) -> None:
        mirror = self.base / "mirror"
        mirror.mkdir()
        sent_key = "a" * 64
        result = self.record(
            "step", "1", str(START), str(START + 1), "0", "", "", *NEXTEST,
            cwd=mirror,
            extra={"BUILDLOG_CALLER": "verify-mac", "BUILDLOG_CALL_ID": CALL_ID,
                   "BUILDLOG_SENT_TREE_KEY": sent_key},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["caller"], row["call_id"], row["step"]), ("verify-mac", CALL_ID, "nextest"))
        self.assertEqual((row["tree_key"], row["tree_changed"]), (sent_key, None))

    def test_verify_linux_step_without_git_keeps_the_sent_key_target_and_call(self) -> None:
        mirror = self.base / "linux-mirror"
        mirror.mkdir()
        sent_key = "b" * 64

        result = self.record(
            "step", "1", str(START), str(START + 1), "0", "", "", *NEXTEST,
            cwd=mirror,
            extra={"BUILDLOG_CALLER": "verify-linux", "BUILDLOG_CALL_ID": CALL_ID,
                   "BUILDLOG_TARGET": "aws", "BUILDLOG_SENT_TREE_KEY": sent_key},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["caller"], row["target"], row["call_id"]), ("verify-linux", "aws", CALL_ID))
        self.assertEqual((row["tree_key"], row["tree_changed"]), (sent_key, None))

    def test_verify_linux_step_preserves_tree_verification_over_the_sent_key(self) -> None:
        own_key = self.key(*NEXTEST)
        sent_key = "b" * 64
        for start_key, expected in ((own_key, (own_key, False)), ("0" * 64, (None, True))):
            with self.subTest(start_key=start_key):
                row = self.step(1, "0", "", "", NEXTEST, {
                    "BUILDLOG_CALLER": "verify-linux", "BUILDLOG_TARGET": "aws",
                    "BUILDLOG_TREE_START": start_key, "BUILDLOG_SENT_TREE_KEY": sent_key,
                })

                self.assertEqual((row["tree_key"], row["tree_changed"]), expected)

    def assert_unverified_mac_step_uses_sent_key(self) -> None:
        # The fallback must work where the preceding step's guard does not apply.
        mirror = self.base / "mirror-control"
        mirror.mkdir()
        sent_key = "a" * 64
        result = self.record(
            "step", "1", str(START + 20), str(START + 21), "0", "", "", *NEXTEST,
            cwd=mirror, extra={"BUILDLOG_CALLER": "verify-mac", "BUILDLOG_SENT_TREE_KEY": sent_key},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["tree_key"], row["tree_changed"]), (sent_key, None))

    def test_local_step_with_changed_tree_does_not_use_the_sent_tree_key(self) -> None:
        end_key = self.key(*NEXTEST)
        start_key = "0" * 64
        self.assertNotEqual(start_key, end_key)
        row = self.step(0, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify", "BUILDLOG_TREE_START": start_key,
            "BUILDLOG_SENT_TREE_KEY": "a" * 64,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, True))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_verify_mac_step_with_changed_tree_does_not_use_the_sent_tree_key(self) -> None:
        end_key = self.key(*NEXTEST)
        start_key = "0" * 64
        self.assertNotEqual(start_key, end_key)
        row = self.step(1, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify-mac", "BUILDLOG_TREE_START": start_key,
            "BUILDLOG_SENT_TREE_KEY": "a" * 64,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, True))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_verify_mac_step_keeps_its_own_verified_key_over_a_different_sent_key(self) -> None:
        own_key = self.key(*NEXTEST)
        sent_key = "a" * 64
        self.assertNotEqual(own_key, sent_key)
        row = self.step(1, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify-mac", "BUILDLOG_TREE_START": own_key,
            "BUILDLOG_SENT_TREE_KEY": sent_key,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (own_key, False))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_local_step_without_git_does_not_use_the_sent_tree_key(self) -> None:
        mirror = self.base / "local-without-git"
        mirror.mkdir()
        result = self.record(
            "step", "0", str(START), str(START + 1), "0", "", "", *NEXTEST,
            cwd=mirror, extra={"BUILDLOG_CALLER": "verify", "BUILDLOG_SENT_TREE_KEY": "a" * 64},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, None))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_key_follows_the_manifest_and_is_empty_outside_git(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.key(cwd=elsewhere), "")
        manifest = ["cargo", "clippy", "--manifest-path", str(self.repo / "Cargo.toml")]
        self.assertEqual(self.key(*manifest, cwd=elsewhere), self.key())
        start = {"BUILDLOG_TREE_START": "0" * 64}
        result = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=elsewhere, extra=start)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual((record["tree_key"], record["tree_changed"]), (None, None))

    def test_outside_git_has_no_git_facts(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual([record[key] for key in ("repo_path", "worktree", "branch", "sha")], [None] * 4)

    def test_call_record_fields(self) -> None:
        board = {"UNIT_TEAM_ROLE": "impl2", "UNIT_RUN_DIR": "/x/boards/run-7/"}
        result = self.record(
            "call",
            *("reused", "0", "1", "3", "0", "", "42", "5", "test", "hana"),
            extra={"BUILDLOG_CALL_ID": CALL_ID, "CLAUDE_CODE_SESSION_ID": "session-1", **board},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        expected: Record = {
            "kind": "call",
            "id": CALL_ID,
            "tool": "verify.sh",
            "command": "test hana",
            "verb": "test",
            "package": "hana",
            "outcome": "reused",
            "status": 0,
            "cached": True,
            "wait_s": 3,
            "wall_s": 0,
            "build_s": None,
            "saved_s": 42,
            "cwd": str(self.repo),
            "repo_path": str(self.repo),
            "branch": "main",
            "worker": "impl2",
            "unit_session": "run-7",
            "session": "session-1",
            "backfilled": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)
        self.assertEqual(record["token_wait_s"], 0)
        started = datetime.fromisoformat(str(record["started_at"]))
        ended = datetime.fromisoformat(str(record["ended_at"]))
        self.assertAlmostEqual((ended - started).total_seconds(), 5.0, delta=0.002)

        final = self.record("call", "interrupted", "", "0", "0", "12", "", "0", "12", "final")
        self.assertEqual(final.returncode, 0, final.stderr)
        record = self.records()[-1]
        self.assertEqual((record["verb"], record["package"], record["status"], record["build_s"]), ("final", "workspace", None, None))
        self.assertIs(record["cached"], False)
        self.assertNotEqual(record["id"], CALL_ID)

    def test_call_records_cargo_token_wait_separately_from_total_wait(self) -> None:
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
            extra={"BUILDLOG_TOKEN_WAIT_S": "4"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call_record = self.records()[-1]
        self.assertEqual(call_record["token_wait_s"], 4)
        self.assertEqual(call_record["wait_s"], 9)

    def test_ssh_attempts_keep_each_target_and_outcome_after_local_fallback(self) -> None:
        for target, outcome, completion in (
            ("aws", "failed", "confirmed"),
            ("aws", "lost", "missing"),
            ("spare", "declined", "missing"),
            ("aws", "no_tests", "confirmed"),
        ):
            recorder.ssh_attempt({
                "call_id": CALL_ID, "target": target,
                "repo_identity": "github.com/example/hana",
                "queued_at": START, "departed_at": START + 100, "slot": 2,
                "outcome": outcome, "completion": completion,
                "cleanup": "unconfirmed", "unresolved_count": 2,
                "evidence_at": START + 150,
            })
        result = self.record(
            "call", "ran", "0", "0", "100", "10", "", "0", "110", "test", "hana",
            extra={"BUILDLOG_CALL_ID": CALL_ID, "BUILDLOG_TARGET": "natedev"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.records()
        attempts = [row for row in rows if row["kind"] == "ssh_attempt"]
        self.assertEqual([(row["target"], row["outcome"]) for row in attempts],
                         [("aws", "failed"), ("aws", "lost"), ("spare", "declined"), ("aws", "no_tests")])
        for row in attempts:
            self.assertEqual((row["call_id"], row["queued_at"], row["departed_at"], row["slot"]),
                             (CALL_ID, START, START + 100, 2))
            self.assertEqual((row["cleanup"], row["unresolved_count"], row["evidence_at"]),
                             ("unconfirmed", 2, START + 150))
        final = next(row for row in rows if row["kind"] == "call")
        self.assertEqual(final["target"], "natedev")

    def test_call_records_zero_token_wait_when_value_is_invalid(self) -> None:
        for value in ("", "abc"):
            with self.subTest(value=value):
                before = len(self.records())
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_TOKEN_WAIT_S": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = self.records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual(records[-1]["token_wait_s"], 0)

    def test_busy_call_stores_the_claim_what_as_mac_holder(self) -> None:
        result = self.record(
            "call", "ran", "0", "1", "0", "1", "1", "0", "1", "test", "hana",
            extra={"BUILDLOG_MAC": "declined", "BUILDLOG_MAC_REASON": "busy",
                   "BUILDLOG_MAC_S": "1", "BUILDLOG_MAC_HOLDER": "hana nextest"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["mac_holder"], "hana nextest")

    def test_hold_event_keeps_the_key_and_the_locked_utc_time(self) -> None:
        result = self.record("hold-event", "began", "mac", "block-key", "hana", "render",
                             "2026-10-12T12:00:00.123456+00:00", "2026-10-12T14:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual(row, {
            "kind": "hold_event", "v": recorder.RECORD_VERSION,
            "at": "2026-10-12T12:00:00.123456Z", "host": socket.gethostname().split(".")[0],
            "target": "mac", "event": "began", "hold_key": "block-key", "holder": "hana",
            "reason": "render", "until": "2026-10-12T14:00:00+00:00",
        })
        result = self.record("hold-event", "ended", "mac", "block-key", "hana", "render",
                             "2026-10-12T14:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["until"], "")

    def test_hold_event_write_failure_returns_zero_and_adds_no_event(self) -> None:
        with mock.patch.object(store, "append_line", side_effect=OSError("write failed")):
            self.assertEqual(recorder.main(["hold-event", "began", "natedev", "key", "hana", "render",
                                            "2026-10-12T12:00:00+00:00"]), 0)
        self.assertEqual(self.records(), [])
        self.assertIn("write failed", (self.root / "errors.log").read_text())

    def test_hold_event_accepts_an_empty_planned_end(self) -> None:
        result = self.record("hold-event", "began", "mac", "block-key", "hana", "render",
                             "2026-10-12T12:00:00Z", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["until"], "")

    def test_hold_build_deduplicates_lifetimes_and_holds_in_permanent_log(self) -> None:
        since = "2026-10-28T12:00:00+00:00"
        arguments = ["hold-build", "holder", since, "boot-one", "123", "456", "other_session",
                     "session", "cargo test", "/repo"]
        for _ in range(2):
            result = self.record(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.records()
        self.assertEqual((row["kind"], row["hold_key"], row["pid"], row["proc_start"], row["class"]),
                         ("hold_build", "holder", 123, "456", "other_session"))
        self.assertEqual((row["session"], row["command"], row["cwd"]), ("session", "cargo test", "/repo"))
        arguments[3] = "boot-two"
        _ = self.record(*arguments)
        arguments[5] = "457"
        _ = self.record(*arguments)
        arguments[2] = "2026-10-28T13:00:00+00:00"
        _ = self.record(*arguments)
        self.assertEqual(len(self.records()), 4)

    def test_hold_build_refuses_holder_and_admitted_rows(self) -> None:
        for classification in ("holder", "admitted"):
            result = self.record("hold-build", "holder", "2026-10-28T12:00:00Z", "boot", "123", "456",
                                 classification, "session", "cargo test", "/repo")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])

    def test_call_records_mac_result_and_local_reason(self) -> None:
        local_reason = 'needs Linux path handling with a "quoted" mount'
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
            extra={
                "BUILDLOG_MAC": "passed",
                "BUILDLOG_MAC_REASON": "",
                "BUILDLOG_MAC_S": "12.5",
                "BUILDLOG_LOCAL_REASON": local_reason,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call_record = self.records()[-1]
        self.assertEqual(call_record["mac"], "passed")
        self.assertEqual(call_record["mac_reason"], "")
        self.assertEqual(call_record["mac_s"], 12.5)
        self.assertEqual(call_record["local_reason"], local_reason)

    def test_call_records_the_mac_copy_outcome_and_times(self) -> None:
        result = self.record(
            "call", "failed", "1", "0", "9", "100", "", "0", "110", "test", "hana",
            extra={
                "BUILDLOG_MAC_COPY": "won",
                "BUILDLOG_MAC_COPY_S": "41.5",
                "BUILDLOG_MAC_COPY_COUNT": "2",
                "BUILDLOG_MAC_COPY_NATEDEV_S": "95.25",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(
            [record[key] for key in ("copy", "copy_s", "copy_count", "copy_natedev_s")], ["won", 41.5, 2, 95.25]
        )

    def test_call_without_a_copy_records_empty_outcome_and_null_times(self) -> None:
        for value in ("", "abc", "-1", "nan"):
            with self.subTest(value=value):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={
                        "BUILDLOG_MAC_COPY_S": value,
                        "BUILDLOG_MAC_COPY_COUNT": "" if value == "-1" else value,
                        "BUILDLOG_MAC_COPY_NATEDEV_S": value,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.records()[-1]
                self.assertEqual(
                    [record[key] for key in ("copy", "copy_s", "copy_count", "copy_natedev_s")], ["", None, None, None]
                )

    def test_call_local_reason_defaults_to_empty(self) -> None:
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["local_reason"], "")

    def test_call_records_target_and_fractional_hold_wait(self) -> None:
        for target in ("mac", "natedev", ""):
            with self.subTest(target=target):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_TARGET": target, "BUILDLOG_HOLD_WAIT_S": "0.4"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.records()[-1]["target"], target)
                self.assertEqual(self.records()[-1]["hold_wait_s"], 0.4)

    def test_call_records_null_hold_wait_for_invalid_numbers(self) -> None:
        for value in ("", "abc", "-0.4", "nan", "inf"):
            with self.subTest(value=value):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_HOLD_WAIT_S": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.records()[-1]["hold_wait_s"])

    def test_call_records_empty_target_and_null_hold_wait_when_unset(self) -> None:
        result = self.record("call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["target"], "")
        self.assertIsNone(self.records()[-1]["hold_wait_s"])

    def test_call_records_null_mac_seconds_when_value_is_unusable(self) -> None:
        for value in ("", "abc", "-1"):
            with self.subTest(value=value):
                before = len(self.records())
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={
                        "BUILDLOG_MAC": "declined",
                        "BUILDLOG_MAC_REASON": "runner",
                        "BUILDLOG_MAC_S": value,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = self.records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual(records[-1]["mac"], "declined")
                self.assertEqual(records[-1]["mac_reason"], "runner")
                self.assertIsNone(records[-1]["mac_s"])

    def test_backfill_is_idempotent(self) -> None:
        same = ledger_line()
        lines = [
            same,
            same,
            ledger_line(at="2026-09-30T23:00:00+00:00", outcome="ran", wait_s=5, wall_s=60, build_s=40, saved_s=0),
            ledger_line(machine="Nates-MacBook-Pro-2023", outcome="failed", commit="abc1234", session="run-3"),
        ]
        ledger = self.base / "events.jsonl"
        _ = ledger.write_text("\n".join(lines) + "\n\n")
        first = self.record("backfill-verify", str(ledger))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("4 added, 0 already present", first.stdout)
        records = self.records()
        self.assertEqual(len(records), 4)
        self.assertEqual(len({record["id"] for record in records}), 4)
        self.assertTrue((self.root / "natedev" / "2026-09.jsonl").is_file())
        self.assertTrue((self.root / "Nates-MacBook-Pro-2023" / "2026-10.jsonl").is_file())

        by_outcome = {str(record["outcome"]): record for record in records}
        ran = by_outcome["ran"]
        self.assertEqual((ran["started_at"], ran["ended_at"]), ("2026-09-30T22:58:55.000Z", "2026-09-30T23:00:00.000Z"))
        self.assertEqual((ran["status"], ran["build_s"], ran["cached"], ran["backfilled"]), (0, 40, True, True))
        failed = by_outcome["failed"]
        self.assertEqual((failed["host"], failed["status"], failed["sha"]), ("Nates-MacBook-Pro-2023", None, "abc1234"))
        self.assertEqual((failed["unit_session"], failed["repo_path"]), ("run-3", "/r/hana"))
        self.assertIsNone(by_outcome["reused"]["sha"])

        second = self.record("backfill-verify", str(ledger))
        self.assertIn("0 added, 4 already present", second.stdout)
        self.assertEqual(len(self.records()), 4)

    def test_an_error_exits_zero_and_is_logged(self) -> None:
        handoff, peak = self.handoff(), self.peak_file()
        bad_start = self.record("step", "0", "not-a-number", "", "0", str(handoff), str(peak), *NEXTEST)
        short_call = self.record("call", "ran")
        unknown = self.record("bogus")
        for result in (bad_start, short_call, unknown):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])
        self.assertFalse(handoff.exists())
        self.assertFalse(peak.exists())
        errors = (self.root / "errors.log").read_text()
        self.assertIn("record.py step 0 not-a-number", errors)
        self.assertIn("ValueError", errors)
        self.assertIn("record.py call ran", errors)
        self.assertIn("unknown command bogus", errors)


if __name__ == "__main__":
    _ = unittest.main()
