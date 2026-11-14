"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.lint.memory_test_support import write_pressure_reading, admission_policy_fixture

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
import json
import os
import shlex
import struct
import subprocess
import time
import unittest
from typing import Self, cast, override
from unittest import mock

from scripts.build_queue.scope_activity import ScopeProcesses, NoLiveBuildRun, LiveBuildRun, IdleService, RunningServiceJob, NotServiceSlice
from scripts.buildlog import store
from scripts.build_queue import head_facts, queue_card, queue_view, queue_evidence
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue import placement
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.queue_fixture import (
    ci_step,
    ASKING_SESSION, GOLDEN, NOW, QueueFixture, QueueFixtureTests, queue_read_fixture, waiting_rows, with_waiting,
)
from scripts.build_queue.queue_lines import QueueLines, cut_tail, json_form, plain, wrap
from scripts.lib.build_steps import read_step_names
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import fake_memory_readings
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels
from scripts.build_queue.queue_fixture import write_memory_config, write_process


class RecordedOwnerQueueTests(unittest.TestCase):
    def test_parentless_owner_keeps_its_name_without_changing_the_step_name(self) -> None:
        with QueueFixture() as fixture:
            model = fixture.model()
            waiter = replace(model.reading.view.waiting[0], session_id="solo", session_name="solo",
                             unit="", project="repository", step="check")
            model = with_waiting(model, [waiter])
            model = replace(model, labels={"solo": UnitLabels("solo")})
            lines = queue_view.render_view(model, 120)
            self.assertIn("solo", plain(lines))
            self.assertIn("repository check", plain(lines))
            self.assertNotIn("solo check", plain(lines))
            self.assertEqual(queue_view.name_text(waiter, model), "solo")

    def test_detached_and_systemd_rows_resolve_the_recorded_staff_owner(self) -> None:
        from scripts.build_queue import admission_client
        from scripts.production import staff
        from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file

        with QueueFixture() as fixture, isolated_staff_file(fixture.root / "staff.json", {}):
            runner = fixture_member("showrunner", name="hana")
            unit = fixture_member("unit", name="demo", parent=runner.id)
            worker = fixture_member("worker", name="impl", parent=unit.id)
            current = fixture_runtime_id("owner-after-session-restart")
            worker = staff.update_current_session(worker.current_session, staff.native_id(current))
            model = fixture.model()
            for source, owner, ran_by in (("detached", worker, unit_labels.Runner.WORKER),
                                          ("systemd", unit, unit_labels.Runner.UNIT_DIRECTOR)):
                with self.subTest(source=source), mock.patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                    "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": str(owner.id),
                }), mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                    identity = admission_client.step_identity("targeted-test", "/private/tree")
                    self.assertEqual(identity.session_id, owner.current_session)
                    labels = unit_labels.unit_labels_for([identity.session_id], os.environ)
                    waiter = replace(model.reading.view.waiting[0], session_id=identity.session_id,
                                     session_name=identity.name, unit=identity.unit)
                    running = replace(model.reading.view.running[0], session_id=identity.session_id,
                                      session_name=identity.name, unit=identity.unit)
                    for step in (waiter, running):
                        expected = UnitLabels(owner.name, "hana", "demo", unit_labels.OwnerRole.UNIT, ran_by)
                        self.assertEqual(queue_evidence.step_labels(step, labels), expected)
                    cloud = replace(cloud_reading(model.reading).cloud[0], identity=identity)
                    self.assertEqual(queue_view.cloud_step_labels(cloud, replace(model, labels=labels)),
                                     expected)

    def test_live_session_identity_takes_priority_over_the_recorded_staff_owner(self) -> None:
        from scripts.build_queue import admission_client

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "live-session", "CLAUDE_CODE_SESSION_ID": "",
                                         "BUILDLOG_OWNER_STAFF_ID": "other-owner"}):
            self.assertEqual(admission_client.step_identity("nextest", "").session_id, "live-session")

    def test_missing_owner_records_have_explicit_labels_on_every_destination(self) -> None:
        from scripts.build_queue import admission_client
        from scripts.production.staff_test_support import isolated_staff_file

        with QueueFixture() as fixture, isolated_staff_file(fixture.root / "staff.json", {}):
            model = fixture.model()
            for reference in ("", "missing-staff-row"):
                with self.subTest(reference=reference), mock.patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                    "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": reference,
                }):
                    identity = admission_client.step_identity("targeted-test", "")
                    self.assertEqual(identity.session_id, "")
                    for step in (model.reading.view.waiting[0], model.reading.view.running[0]):
                        label = queue_evidence.step_labels(replace(step, session_id="", session_name="", unit=""), {})
                        self.assertEqual((label.showrunner, label.unit), ("owner not recorded", "owner not recorded"))
                    cloud = replace(cloud_reading(model.reading).cloud[0], identity=identity)
                    label = queue_view.cloud_step_labels(cloud, replace(model, labels={}))
                    self.assertEqual((label.showrunner, label.unit), ("owner not recorded", "owner not recorded"))


def queue_json_text(data: QueueLines) -> str:
    """Read the text cells from the queue pane's JSON lines."""
    return "\n".join("".join(span["text"] for span in row) for row in data["lines"])


def with_converted_executions(reading: admission_records.QueueRead) -> admission_records.QueueRead:
    """Keep a local charge beside host-scoped executions with equal process values."""
    local = reading.view.running[0]
    local = replace(local, execution=placement.ObservedExecution(
        placement.ProcessExecution("local-host", "local-generation", 900, "equal-start")), assignment_host="local-host")
    remote = tuple(replace(local, session_id=f"remote-{host}", session_name=f"remote-{host}",
        promise=0, measured=admission_records.RemoteMemoryUnavailable("remote memory is unavailable"), pid=900,
        execution=placement.ObservedExecution(placement.ProcessExecution(host, "remote-generation", 900, "equal-start")),
        assignment_host=host) for host in ("fixture-mac", "fixture-ssh"))
    uncertain = replace(remote[0], session_id="remote-uncertain", session_name="remote-uncertain",
        pid=admission_records.PidNotRecorded(), execution=placement.ExecutionEvidenceUnavailable("transport unavailable", NOW),
        assignment_host="uncertain-mac")
    explained = tuple(admission_records.ExplainedRunningStep(step, admission_records.AdmissionTimeNotRecorded(),
        0, 0, 0, owner=f"remote-{index}") for index, step in enumerate((*remote, uncertain)))
    running = (local, *reading.view.running[1:], *remote, uncertain)
    existing = tuple(replace(row, step=local) if row.step == reading.view.running[0] else row
                     for row in reading.explanation.running)
    return replace(reading, view=replace(reading.view, running=running),
                   explanation=replace(reading.explanation, running=(*existing, *explained)))


class BumpRecordsWithVisitCount(tuple[head_facts.BumpRecord, ...]):
    visits: int

    def __new__(cls, records: tuple[head_facts.BumpRecord, ...]) -> Self:
        result = super().__new__(cls, records)
        result.visits = 0
        return result

    @override
    def __iter__(self) -> Iterator[head_facts.BumpRecord]:
        for record in super().__iter__():
            self.visits += 1
            yield record


def cloud_reading(reading: admission_records.QueueRead) -> wire.CloudQueueRead:
    """Add running and waiting cloud work to a private queue reading."""
    now = reading.explanation.now
    rows = (
        wire.CloudRow(wire.Owner(700001, "100"),
            wire.Identity("cloud-session", "route-one", "merge-gate", "/private/cloud", "cloud-unit"),
            wire.CloudPlace("aws"), now - 245, now - 240,
            cloud_progress.CloudProgress("running", 32, 1, now - 120),
            progress_profile.ProgressCounter("modules", 7, 12), admission_records.ReadyForAdmission()),
        wire.CloudRow(wire.Owner(700002, "200"),
            wire.Identity("cloud-waiter", "route-two", "targeted-test", "/private/waiting", "waiting-unit"),
            wire.CloudPlace("aws"), now - 60, 0,
            cloud_progress.NoCloudProgress(), progress_profile.NoCounter(), wire.CloudSlotWait(1, 1)),
    )
    return wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered,
        reading.diagnostics, rows, 1, cloud_gate_stale_pushes=3)






def empty_mac_reading(fixture: QueueFixture) -> None:
    """Keep existing renderer tests on a fresh private Mac reading."""
    path = fixture.root / "mac-builds.json"
    _ = path.write_text(json.dumps({"read_at": NOW, "builds": [], "error": ""}))
    _ = (fixture.root / "memory_feed.conf").write_text("# whole: 1..\nmac_activity_refresh_seconds=30\n")
    _ = fixture.enter_context(mock.patch.object(queue_view, "mac_reading_path", return_value=path))
    _ = fixture.enter_context(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))


class MacQueueViewTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "Root can read a folder with mode 000.")
    def test_project_record_returns_empty_for_an_unreadable_folder(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folder = fixture.root / "unreadable"
            folder.mkdir()
            folder.chmod(0)
            try:
                script = queue_view.mac_build_script().replace("print(json.dumps(read_builds()))",
                    f"print(json.dumps(project_record({str(folder)!r})))")
                result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
            finally:
                folder.chmod(0o700)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout.removesuffix("rc=0\n")),
                             {"recorded_unit": "", "session": "", "repository": ""})

    def test_project_record_catches_each_filesystem_error(self) -> None:
        with QueueFixture(captured=False) as fixture:
            for operation in ("resolve", "is_symlink", "stat", "exists", "read_text"):
                with self.subTest(operation=operation):
                    record = fixture.root / ".copy-record.json"
                    if operation == "read_text":
                        _ = record.write_text('{}')
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_builds()))",
                        "from unittest.mock import patch\n"
                        + f"with patch.object(Path, {operation!r}, side_effect=PermissionError('unreadable')):\n"
                        + f"    print(json.dumps(project_record({str(fixture.root)!r})))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout.removesuffix("rc=0\n")),
                                     {"recorded_unit": "", "session": "", "repository": ""})

    def test_mac_reader_keeps_rows_when_one_row_field_fails(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folders = {pid: str(fixture.root / str(pid)) for pid in (100, 200, 300)}
            processes = {pid: {"parent": 1, "command": "cargo build", "executable": "/bin/cargo",
                              "resident_bytes": pid, "started_at": NOW + pid} for pid in folders}
            captures = {pid: {"folder": folder, "command": "cargo build", "progress_tail": ""}
                        for pid, folder in folders.items()}
            for failed in ("project_record", "repository_name", "running_test_pid", "build_origin"):
                with self.subTest(failed=failed):
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_builds()))",
                        f"read_processes = lambda: {processes!r}\n"
                        + f"read_captures = lambda processes: {captures!r}\n"
                        + "output = lambda arguments: ''\n"
                        + "def project_record(folder):\n"
                        + "    return dict(recorded_unit='unit', session='session', repository='')\n"
                        + "repository_name = lambda folder: 'repo'\n"
                        + "running_test_pid = lambda pid, *args: pid + 1\n"
                        + "build_origin = lambda pid, processes: 'ci'\n"
                        + f"original = {failed}\n"
                        + "def fail_one(value, *args):\n"
                        + f"    if value in (200, {folders[200]!r}):\n"
                        + "        raise PermissionError('unreadable row')\n"
                        + "    return original(value, *args)\n"
                        + f"{failed} = fail_one\nprint(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    self.assertEqual([row["pid"] for row in rows], [100, 200, 300])
                    for row in rows:
                        bad = row["pid"] == 200
                        self.assertEqual(row["recorded_unit"], "" if bad and failed == "project_record" else "unit")
                        self.assertEqual(row["session"], "" if bad and failed == "project_record" else "session")
                        self.assertEqual(row["repository"], "" if bad and failed == "repository_name" else "repo")
                        self.assertEqual(row["test_pid"], -1 if bad and failed == "running_test_pid" else int(cast(int, row["pid"])) + 1)
                        self.assertEqual(row["origin"], "unknown" if bad and failed == "build_origin" else "ci")

    def test_mac_build_origin_survives_a_record_round_trip_and_old_records_stay_unknown(self) -> None:
        for origin in queue_view.MacBuildOrigin:
            with self.subTest(origin=origin):
                build = queue_view.MacBuild(100, "/Users/nate/hana", NOW, origin=origin,
                                            ci_runner="hana-mac-1", ci_job="test-hana")
                row = queue_view.mac_build_record(build)
                self.assertEqual(queue_view.mac_build_from_record(row), build)
                del row["origin"]
                self.assertIs(queue_view.mac_build_from_record(row).origin, queue_view.MacBuildOrigin.UNKNOWN)
                del row["ci_runner"], row["ci_job"]
                self.assertEqual(queue_view.mac_build_from_record(row).ci_runner, "")
                self.assertEqual(queue_view.mac_build_from_record(row).ci_job, "")

    def test_mac_reader_marks_runner_ancestors_and_stops_at_parent_cycles(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folder = str(fixture.root)
            processes: dict[int, dict[str, object]] = {
                100: {"parent": 90, "command": "/bin/cargo build", "executable": "/bin/cargo",
                      "resident_bytes": 1234, "started_at": NOW},
                90: {"parent": 80, "command": "/bin/sh build.sh", "executable": "/bin/sh",
                     "resident_bytes": 0, "started_at": NOW},
                80: {"parent": 1, "command": "", "executable": "/runner", "resident_bytes": 0, "started_at": NOW},
            }
            for command, parent, expected in (
                ("/actions/Runner.Worker spawn", 1, "ci"),
                ('"/actions runner/Runner.Listener" run', 1, "ci"),
                ("/bin/echo Runner.Worker", 1, "non-ci"),
                ("/actions/Runner.Worker-helper", 1, "non-ci"),
                ("/bin/sh build.sh", 90, "non-ci"),
            ):
                with self.subTest(command=command, parent=parent):
                    processes[80]["command"] = command
                    processes[80]["parent"] = parent
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_builds()))",
                        f"read_processes = lambda: {processes!r}\n" +
                        "read_captures = lambda processes: {}\n" +
                        f"output = lambda arguments: 'p100\\nn{folder}\\n'\n" +
                        "repository_name = lambda folder: 'hana'\n" +
                        "process_environment = lambda pid: {'RUNNER_NAME': 'hana-mac-1', 'GITHUB_JOB': 'test-hana'}\n" +
                        "print(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["pid"], 100)
                    self.assertEqual(rows[0]["origin"], expected)
                    self.assertEqual(rows[0]["ci_runner"], "hana-mac-1" if expected == "ci" else "")
                    self.assertEqual(rows[0]["ci_job"], "test-hana" if expected == "ci" else "")

    def test_mac_names_use_the_package_then_the_repository(self) -> None:
        for command, repository, expected in (
            ("cargo build --release -p hana", "", "hana build"),
            ("cargo build -phana", "other", "hana build"),
            ("cargo build --package=hana", "other", "hana build"),
            ("cargo check --package hana", "other", "hana check"),
            ("cargo nextest run", "cargo-liner", "cargo-liner nextest"),
            ("cargo build", "", "build"),
            ("", "", "cargo"),
        ):
            with self.subTest(command=command):
                self.assertEqual(queue_view.mac_step_name(queue_view.MacBuild(1, "/project", 0,
                    command, repository)), expected)

    def test_mac_command_jobs_reads_all_supported_positive_values(self) -> None:
        for argument in ("--test-threads 12", "--test-threads=12", "-j12", "-j 12", "--jobs 12", "--jobs=12"):
            with self.subTest(argument=argument):
                self.assertEqual(queue_view.mac_command_jobs("cargo nextest run " + argument), 12)
        for command in ("cargo build", "cargo build -j0", "cargo nextest run --test-threads 0"):
            self.assertIsInstance(queue_view.mac_command_jobs(command), queue_view.MacJobsUnavailable)

    def test_periodic_reader_preserves_folders_and_elapsed_days(self) -> None:
        with QueueFixture(captured=False) as fixture:
            _ = (fixture.root / "mac_test.conf").write_text(
                "host=fixture-mac\n# range: 5e-324..\nconnect_timeout_s=2\nmirror_root=~/.local/state/mac-test/mirror\n")
            path = fixture.root / "mac-builds.json"
            now = NOW
            for elapsed, seconds in (("03:04", 184), ("02:03:04", 7384), ("1-02:03:04", 93784)):
                with self.subTest(elapsed=elapsed):
                    result = subprocess.CompletedProcess(["ssh"], 0,
                        stdout=json.dumps([{"pid": 710001, "folder": "/Users/nate/mac clone", "started_at": now - seconds,
                            "resident_bytes": 1048576, "test_pid": 710002, "command": "cargo nextest run --jobs=4",
                            "repository": "hana", "progress_tail": "", "recorded_unit": "", "session": ""}]) + "\nrc=0\n", stderr="")
                    with (mock.patch.object(queue_view, "mac_reading_path", return_value=path),
                          mock.patch.object(subprocess, "run", return_value=result),
                          mock.patch.object(time, "time", return_value=now)):
                        queue_view.refresh_mac_builds()
                        reading = queue_view.read_mac_builds()
                    self.assertEqual(reading.error, "")
                    self.assertEqual(reading.read_at, now)
                    build, = reading.builds
                    self.assertEqual(build.pid, 710001)
                    self.assertEqual(build.folder, "/Users/nate/mac clone")
                    self.assertEqual(build.started_at, now - seconds)
                    self.assertEqual(build.stage, queue_view.MacTesting(710002))
                    self.assertEqual(build.resident_bytes, 1048576)
                    self.assertEqual(build.jobs, 4)
                    self.assertEqual(queue_view.mac_step_name(build), "hana nextest")




    def test_mac_reader_finds_all_three_tile_runs_and_their_counters(self) -> None:
        with QueueFixture(captured=False) as fixture:
            binary = fixture.root / "bin"
            binary.mkdir()
            born = int(time.mktime(time.strptime("Thu Nov 12 18:00:00 2026", "%a %b %d %H:%M:%S %Y")))
            stamp = "Thu Nov 12 18:00:00 2026"
            records = fixture.root / "captures" / "501"
            (records / "state/pids").mkdir(parents=True)
            folders = [fixture.root / "native" / "trunk-p172", fixture.root / "mirror" / "hana",
                       fixture.root / "mirror" / "cargo-liner"]
            for index, (pid, folder, done, total) in enumerate(zip((100, 200, 300), folders, (604, 647, 983), (1000,) * 3, strict=True)):
                folder.mkdir(parents=True)
                _ = (folder / ".copy-record.json").write_text(json.dumps({
                    "owner": "trunk" if index == 0 else "mirror-unit",
                    "repo": "cargo-liner" if index == 2 else "hana" if index == 1 else ""}))
                if index == 0:
                    _ = (folder / "Cargo.toml").write_text(
                        '[workspace.package]\nrepository = "https://github.com/natepiano/hana"\n')
                generation = f"fixture-{pid}"
                log = f"run-{generation}-{pid}.log"
                arguments = ["build", "--release", "-p", "hana"] if index == 0 else ["nextest", "run", "--test-threads", "12"]
                fields = ["cargo-tile-v3", generation, "fixture-boot", str(born), log, str(folder),
                          str(fixture.root), str(len(arguments)), *arguments]
                _ = (records / "state/pids" / f"{pid}.{generation}").write_bytes("\0".join([*fields, ""]).encode())
                _ = (records / log).write_text(f"\rBuilding [==> ] {done}/{total}: fixture\r")
            # The nested cargo and the compiler stay in their parent's row.
            process_rows = [f"{pid} 1 501 100 01:00 {stamp} /bin/cargo" for pid in (100, 200, 300)]
            process_rows += [f"201 200 501 200 00:59 {stamp} /bin/cargo",
                             f"202 201 501 300 00:58 {stamp} /bin/rustc"]
            scripts = {
                "sysctl": "printf 'fixture-boot\\n'",
                "git": "exit 1",
                "lsof": "exit 1",
                "ps": "case \"$*\" in *comm=*) cat <<'ROWS'\n" + "\n".join(process_rows) + "\nROWS\n;;\n*) cat <<'ROWS'\n" +
                      "\n".join(f"{pid} /bin/cargo nextest run" for pid in (100, 200, 201, 300)) + "\n202 /bin/rustc\nROWS\n;;\nesac",
            }
            for name, body in scripts.items():
                path = binary / name
                _ = path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            script = queue_view.mac_build_script().replace('CAPTURE_ROOT = Path("/tmp/cargo-tile")',
                f"CAPTURE_ROOT = Path({str(records.parent)!r})")
            result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
            self.assertEqual([row["pid"] for row in rows], [100, 200, 300])
            self.assertEqual([row["resident_bytes"] for row in rows], [100 * 1024, 600 * 1024, 100 * 1024])
            self.assertEqual([row["recorded_unit"] for row in rows], ["trunk", "mirror-unit", "mirror-unit"])
            self.assertEqual([row["repository"] for row in rows], ["hana", "hana", "cargo-liner"])
            self.assertEqual(queue_view.mac_command_jobs(str(rows[1]["command"])), 12)
            percentages: list[float] = []
            for row in rows:
                counter = progress_profile.parse_progress(str(row["progress_tail"]))
                assert isinstance(counter, progress_profile.ProgressCounter)
                percentages.append(counter.percent)
            self.assertEqual(percentages, [60.4, 64.7, 98.3])
            # A reused PID cannot supply the old capture's folder or counter.
            stale = records / "state/pids/100.fixture-100"
            _ = stale.write_bytes(stale.read_bytes().replace(str(born).encode(), str(born - 1).encode()))
            result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
            self.assertEqual([row["pid"] for row in rows], [200, 300])

    def test_mac_reader_finds_tests_in_the_selected_target_directory(self) -> None:
        with QueueFixture(captured=False) as fixture:
            binary = fixture.root / "bin"
            binary.mkdir()
            folder = fixture.root / "project"
            (folder / ".cargo").mkdir(parents=True)
            _ = (folder / "Cargo.toml").write_text('[workspace]\n')
            config = folder / ".cargo/config.toml"
            (folder / "linked-output").symlink_to(folder / "out space", target_is_directory=True)
            cases: list[tuple[str, list[str], dict[str, str], str, str, bool, bool]] = [
                ("default", [], {}, "", "target/debug/deps/test_binary", False, True),
                ("argument", ["--target-dir", "out space"], {"CARGO_TARGET_DIR": "wrong"}, "", "out space/debug/deps/test_binary", False, True),
                ("equals", ["--target-dir=out space"], {}, "", "out space/debug/deps/test_binary", False, True),
                ("symlink", ["--target-dir", "linked-output"], {}, "", "out space/debug/deps/test_binary", False, True),
                ("environment", [], {"CARGO_TARGET_DIR": "out space"}, "", "out space/debug/deps/test_binary", False, True),
                ("build environment", [], {"CARGO_BUILD_TARGET_DIR": "out space"}, "", "out space/debug/deps/test_binary", False, True),
                ("configuration", [], {}, "out space", "out space/debug/deps/test_binary", False, True),
                ("CLI configuration", ["--config", 'build.target-dir="out space"'], {}, "", "out space/debug/deps/test_binary", False, True),
                ("compiler observation", [], {}, "", "out space/debug/deps/test_binary", True, True),
                ("unrelated directory", ["--target-dir", "out space"], {}, "", "other/debug/deps/test_binary", False, False),
                ("directory prefix", ["--target-dir", "out space"], {}, "", "out space-other/debug/deps/test_binary", False, False),
                ("build script", ["--target-dir", "out space"], {}, "", "out space/debug/deps/build-script-build", False, False),
            ]
            for name, arguments, environment, configured, executable, compiler, testing in cases:
                with self.subTest(source=name):
                    config.unlink(missing_ok=True)
                    if configured:
                        _ = config.write_text('[build]\ntarget-dir = ' + json.dumps(configured) + '\n')
                    stamp = "Thu Nov 12 18:00:00 2026"
                    process_rows = [f"100 1 501 100 01:00 {stamp} /bin/cargo",
                                    f"101 100 501 100 01:00 {stamp} {folder / executable}"]
                    commands = ["100 " + shlex.join(["cargo", "test", *arguments]), f"101 {folder / executable}"]
                    if compiler:
                        process_rows.append(f"102 100 501 100 01:00 {stamp} /bin/rustc")
                        commands.append("102 " + shlex.join(["rustc", "--crate-name", "hana_tests", "--out-dir",
                                                            str(folder / executable).rsplit("/", 1)[0]]))
                    scripts = {
                        "sysctl": "exit 1",
                        "git": "exit 1",
                        "lsof": "cat <<'ROWS'\np100\nn" + str(folder) + "\nROWS",
                        "ps": "case \"$*\" in *comm=*) cat <<'ROWS'\n" + "\n".join(process_rows) + "\nROWS\n;;\n*) cat <<'ROWS'\n" + "\n".join(commands) + "\nROWS\n;;\nesac",
                    }
                    for program, body in scripts.items():
                        path = binary / program
                        _ = path.write_text("#!/bin/sh\n" + body + "\n")
                        path.chmod(0o755)
                    environment = {"HOME": str(fixture.root), **environment}
                    # The kernel buffer contains argv, then environment fields.
                    data = struct.pack("i", 3) + b"/bin/cargo\0\0cargo\0test\0CARGO_TARGET_DIR=not-an-environment-field\0"
                    data += b"\0".join(os.fsencode(key + "=" + value) for key, value in environment.items())
                    data += b"\0TOKEN=not-public\0\0"
                    script = queue_view.mac_build_script().replace('CAPTURE_ROOT = Path("/tmp/cargo-tile")',
                        f"CAPTURE_ROOT = Path({str(fixture.root / 'captures')!r})")
                    script = script.replace("print(json.dumps(read_builds()))",
                        f"assert cargo_environment({data!r}) == {environment!r}\n"
                        + f"process_environment = lambda pid: cargo_environment({data!r})\nprint(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertNotIn("not-public", result.stdout)
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    row, = rows
                    self.assertEqual(row["test_pid"], 101 if testing else 0)
                    row["counter"] = {}
                    build = queue_view.mac_build_from_record(row)
                    self.assertIsInstance(build.stage, queue_view.MacTesting if testing else queue_view.MacBuilding)

    def test_mac_command_jobs_requires_a_recorded_positive_value(self) -> None:
        for command in ("cargo nextest run -j8", "cargo build -j 8", "cargo test --jobs=8", "cargo check --jobs 8"):
            self.assertEqual(queue_view.mac_command_jobs(command), 8)
        for command in ("cargo test", "cargo build --jobs=0", "cargo test --jobs=-1"):
            self.assertIsInstance(queue_view.mac_command_jobs(command), queue_view.MacJobsUnavailable)

    def test_mac_reader_logs_raw_errors_without_publishing_them(self) -> None:
        with QueueFixture(captured=False) as fixture:
            _ = (fixture.root / "mac_test.conf").write_text("host=fixture\n# range: 5e-324..\nconnect_timeout_s=2\nmirror_root=~/.local/state/mac-test/mirror\n")
            path = fixture.root / "mac-builds.json"
            _ = path.write_text('{"read_at":0,"builds":[],"error":""}')
            result = subprocess.CompletedProcess(["ssh"], 0, stdout="rc=0\n", stderr="awk: private raw error")
            with (mock.patch.object(queue_view, "mac_reading_path", return_value=path),
                  mock.patch.object(subprocess, "run", return_value=result) as run,
                  self.assertLogs(queue_view.__name__, level="WARNING") as logs):
                queue_view.refresh_mac_builds()
            self.assertEqual(run.call_count, 1)
            self.assertIn("private raw error", str(logs.output))
            self.assertNotIn("private raw error", path.read_text())

    def test_native_mac_rows_show_the_folder_without_local_memory(self) -> None:
        with QueueFixture(captured=False) as fixture:
            empty_mac_reading(fixture)
            fixture.write_running(used=admission_records.GIB, need=3 * admission_records.GIB)
            model = fixture.model()
            now = model.reading.explanation.now
            reading = queue_view.MacBuildReading(now, (queue_view.MacBuild(710001, "/Users/nate/mac-clone", now - 45),))
            with mock.patch.object(queue_view, "read_mac_builds", return_value=reading):
                text = plain(queue_view.render_view(model, 240))
            for expected in ("started on the Mac", "mac-clone", "Progress unavailable", "owner not recorded"):
                self.assertIn(expected, text)
            row, = [line for line in text.splitlines() if "pid 710001" in line]
            self.assertIn("mac", row)

    def test_old_native_mac_readings_hide_their_process_rows(self) -> None:
        with QueueFixture(captured=False) as fixture:
            empty_mac_reading(fixture)
            model = fixture.model()
            now = model.reading.explanation.now
            build = queue_view.MacBuild(710001, "/Users/nate/mac-clone", now - 120)
            for read_at, error in ((now - 90, ""), (now - 30, "Mac unreachable"), (0, "Mac reading missing")):
                with self.subTest(read_at=read_at, error=error):
                    reading = queue_view.MacBuildReading(read_at, (build,), error)
                    with mock.patch.object(queue_view, "read_mac_builds", return_value=reading):
                        text = plain(queue_view.render_view(model, 240))
                    self.assertIn("Mac reading is old", text)
                    self.assertNotIn("pid 710001", text)


class CloudQueueViewTests(unittest.TestCase):
    def test_page_step_names_use_roles_and_configured_owner_templates(self) -> None:
        model = self.cloud_model()[0]
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        director = unit_labels.make_labels("panes", unit_labels.NoShowrunner(), unit_labels.DirectsUnit("panes"))
        runner = unit_labels.make_labels("builds", unit_labels.RunsProduction("builds"), unit_labels.NoDirector())
        for step, owner, expected in (
            ("targeted-test", director, "panes targeted"),
            ("merge-gate", director, "panes merge"),
            ("merge-gate", runner, "showrunner merge"),
            ("merge-gate", unit_labels.UnitLabels("builds", "builds"), "merge gate"),
            ("targeted-test", UnitLabels("worker", "sdlc", "panes"), "targeted test"),
            ("merge-gate", UnitLabels("worker", "sdlc", "panes"), "merge gate"),
            ("build", director, "panes build"),
            ("literal{step}", director, "panes literal{step}"),
        ):
            with self.subTest(step=step, owner=owner):
                current = replace(row, identity=replace(row.identity, step=step))
                labeled = replace(model, labels={row.identity.session_id: owner})
                self.assertEqual(queue_view.cloud_step_name(current, labeled), expected)
        current = replace(row, identity=replace(row.identity, step="targeted-test"))
        settings = dict(model.names.settings)
        settings["owned.targeted-test"] = "targeted for {unit}"
        configured = replace(model, names=type(model.names)(settings), labels={row.identity.session_id: director})
        self.assertEqual(queue_view.cloud_step_name(current, configured), "targeted for panes")
        self.assertEqual(configured.names.shown("targeted-test"), "targeted test")
        self.assertEqual(configured.names.prose("merge-gate"), "merge gate")

    def cloud_model(self) -> tuple[queue_view.QueueModel, QueueFixture]:
        fixture = self.enterContext(QueueFixture(captured=False))
        empty_mac_reading(fixture)
        write_memory_config(fixture.root / "build_admission.conf",
            cloud_gate_slots="1", cloud_gate_stale_pushes="3")
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        model = fixture.model()
        return replace(model, reading=cloud_reading(model.reading)), fixture

    def test_cloud_rows_share_the_table_with_slots_progress_size_elapsed_and_wait(self) -> None:
        model, _fixture = self.cloud_model()
        names = read_step_names()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        assert isinstance(row.progress, cloud_progress.CloudProgress)
        expected = (GOLDEN / "cloud_view.txt").read_text()
        cases: tuple[tuple[cloud_progress.CloudState, int, str, float, str], ...] = (
            ("running", 1, "1 failed module", NOW - 120, "2 min"),
            ("running", 2, "2 failed modules", NOW - 120, "2 min"),
            ("requesting", 0, "", NOW - 120, "2 min"),
            ("setup", 1, "1 failed module", NOW - 120, "2 min"),
            ("uploading", 2, "2 failed modules", NOW - 120, "2 min"),
            ("running", 1, "1 failed module", NOW - 45, "45 s"),
            ("running", 1, "1 failed module", NOW - 10800, "3 h"),
            ("running", 1, "1 failed module", NOW - 172800, "2 d"),
            ("running", 1, "1 failed module", 0.0, "20756 d"),
        )
        for state, failures, failure_text, updated_at, late_age in cases:
            with self.subTest(state=state, failures=failures, updated_at=updated_at):
                current = replace(row, progress=replace(row.progress, state=state,
                    failed_modules=failures, updated_at=updated_at))
                model = replace(model, reading=replace(reading, cloud=(current, *reading.cloud[1:])))
                text = plain(queue_view.render_view(model, 140))
                self.assertNotIn("Cloud ·", text)
                self.assertIn("1 of 1 slots", text)
                for value in ("cloud-unit", names.shown("merge-gate"), state, "waiting-unit",
                              names.shown("targeted-test"), "32", "vCPU"):
                    self.assertIn(value, text)
                self.assertRegex(text, r"7(?: of |/)12")
                header, = [line for line in text.splitlines() if line.lstrip().startswith("# ")]
                running, = [line for line in text.splitlines() if line.lstrip().startswith("run ")]
                waiting, = [line for line in text.splitlines() if line.startswith("cloud")]
                self.assertLess(header.index("machine"), header.index("vCPU"))
                self.assertLess(header.index("vCPU"), header.index("pid"))
                self.assertEqual(running[header.index("min"):header.index("machine")].strip(), "4")
                self.assertEqual(running[header.index("machine"):header.index("vCPU")].strip(), "aws")
                self.assertEqual(running[header.index("vCPU"):header.index("pid")].strip(), "32")
                self.assertIn("pid 700001", running)
                self.assertIn("pid 700002", waiting)
                self.assertEqual(waiting[header.index("machine"):header.index("pid")].strip(), "")
                self.assertNotRegex(running, r"[█░▒]")
                self.assertNotRegex(waiting, r"[█░▒]")
                error_text = " ".join(span.text.strip() for line in queue_view.render_view(model, 140)
                    for span in line if span.tone == "error")
                self.assertIn(f"no update for {late_age}", error_text)
                head, = [line for line in text.splitlines() if line.startswith("Head:")]
                self.assertIn("cloud 1 of 1 slots", head)
                row_text = " ".join(line.removeprefix("· ").strip() for line in text.splitlines())
                self.assertIn(f"no update for {late_age}", row_text)
                self.assertIn("waits for a cloud slot: 1 of 1 in use", row_text)
                if state == "running" and updated_at == NOW - 120:
                    self.assertEqual(text + "\n", expected.replace("1 failed module", failure_text))
                elif failures:
                    self.assertIn(failure_text, text)
                else:
                    self.assertNotIn("failed module", text)

    def test_cloud_rows_show_stage_and_location(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        assert isinstance(row.progress, cloud_progress.CloudProgress)
        for stage, region, zone, detail in (
            ("tools ready", "", "", "tools ready"),
            ("tests start", "eu-south-2", "eu-south-2a", "tests start · eu-south-2, zone a"),
            ("", "eu-south-2", "us-east-1b", "eu-south-2, zone us-east-1b"),
            ("", "", "eu-south-2a", "zone eu-south-2a"),
            ("", "eu-south-2", "", "eu-south-2"),
            ("", "", "", ""),
        ):
            with self.subTest(stage=stage, region=region, zone=zone):
                current = replace(row, progress=replace(row.progress, stage=stage, region=region, zone=zone))
                self.assertEqual(queue_view.cloud_detail(current), detail)
                current_model = replace(model, reading=replace(reading, cloud=(current, *reading.cloud[1:])))
                text = plain(queue_view.render_view(current_model, 240))
                expected = " · ".join(filter(None, ("running", detail, "7 of 12 modules", "1 failed module")))
                self.assertIn(expected, text)

    def test_cloud_rows_sort_with_local_steps_and_keep_the_local_memory_bars(self) -> None:
        model, fixture = self.cloud_model()
        fixture.write_running(used=admission_records.GIB, need=3 * admission_records.GIB)
        _ = fixture.write_waiter({"pid": 123, "start_time": "123", "need": 9 * admission_records.GIB,
            "since": NOW - 30, "session_id": "local-waiter", "session_name": "local",
            "step": "check", "folder": "/private/local"})
        model = replace(model, reading=cloud_reading(fixture.read()))
        with mock.patch.object(os, "cpu_count", return_value=17):
            text = plain(queue_view.render_view(model, 140))
        rows = [line for line in text.splitlines()
            if "pid " in line and line.split("pid ", 1)[1].split()[0].isdigit()]
        self.assertEqual([line.split("pid ", 1)[1].split()[0] for line in rows],
            ["700001", "900", "700002", "123"])
        header, = [line for line in text.splitlines() if line.lstrip().startswith("# ")]
        self.assertEqual(rows[1][header.index("vCPU"):header.index("pid")].strip(), "17")
        self.assertEqual(rows[3][header.index("machine"):header.index("pid")].strip(), "")
        self.assertIn("2.0 / 1.0", rows[1])
        self.assertRegex(rows[1], r"[█░▒]")
        self.assertRegex(rows[3], r"[█░▒]")

    def test_missing_cloud_progress_has_a_named_display_state(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        for reason in ("missing", "unreadable"):
            with self.subTest(reason=reason):
                row = replace(reading.cloud[0], progress=cloud_progress.NoCloudProgress(reason))
                model = replace(model, reading=replace(reading, cloud=(row,)))
                text = plain(queue_view.render_view(model, 140))
                self.assertIn(reason, text)
                self.assertNotIn("no update for", text)

    def test_stale_age_uses_the_pass_threshold_and_the_current_push_interval(self) -> None:
        model, fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        assert isinstance(row.progress, cloud_progress.CloudProgress)
        for seconds, expected in ((14, False), (15, True), (16, True)):
            with self.subTest(seconds=seconds):
                current = replace(row, progress=replace(row.progress, updated_at=NOW - seconds))
                model = replace(model, reading=replace(reading, cloud=(current,)))
                text = plain(queue_view.render_view(model, 140))
                self.assertEqual("no update for" in text, expected)
        write_memory_config(fixture.root / "build_admission.conf", cloud_gate_stale_pushes="4")
        configuration = gate.read_admission_configuration()
        assert isinstance(configuration, gate.UsableAdmissionConfiguration)
        model = replace(model, reading=replace(model.reading,
            cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes))
        self.assertNotIn("no update for", plain(queue_view.render_view(model, 140)))
        write_memory_config(fixture.root / "build_admission.conf", cloud_gate_stale_pushes="3")
        configuration = gate.read_admission_configuration()
        assert isinstance(configuration, gate.UsableAdmissionConfiguration)
        model = replace(model, reading=replace(model.reading,
            cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes))
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=10\n")
        self.assertNotIn("no update for", plain(queue_view.render_view(model, 140)))

    def test_cloud_unit_uses_the_local_label_lookup_when_the_request_has_no_unit(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        row = replace(row, identity=replace(row.identity, unit=""))
        model = replace(model, reading=replace(reading, cloud=(row,)))
        with mock.patch.object(unit_labels, "unit_labels_for",
                return_value={row.identity.session_id: UnitLabels("route-label", unit="lookup-unit")}) as lookup:
            text = plain(queue_view.render_view(model, 140))
            card = plain(queue_card.render_selection(model, "cloud-session", 140))
        lookup.assert_called_once_with({row.identity.session_id}, os.environ)
        for rendered in (text, card):
            self.assertIn("lookup-unit", rendered)
            self.assertNotIn("route-one", rendered)

    def test_cloud_selection_accepts_the_folder_name_like_local_selection(self) -> None:
        model, _fixture = self.cloud_model()
        expected = plain(queue_card.render_selection(model, "cloud-session", 140))
        self.assertEqual(plain(queue_card.render_selection(model, "cloud", 140)), expected)
        self.assertIn("cloud-unit", expected)
        self.assertNotIn("waiting-unit", expected)

    def test_missing_push_file_or_key_omits_the_stale_age(self) -> None:
        model, fixture = self.cloud_model()
        config = fixture.root / "cloud_gate.conf"
        for missing in ("key", "file"):
            with self.subTest(missing=missing):
                if missing == "key":
                    _ = config.write_text("[cloud_gate]\n")
                else:
                    config.unlink()
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("running", text)
                self.assertRegex(text, r"7(?: of |/)12")
                self.assertNotIn("no update for", text)


class QueueViewFixtureTests(QueueFixtureTests):
    @override
    def setUp(self) -> None:
        super().setUp()
        empty_mac_reading(self.fixture)
        _ = self.enterContext(mock.patch.object(os, "cpu_count", return_value=17))


class QueueViewTests(QueueViewFixtureTests):

    def test_converted_hosts_keep_equal_process_values_separate_and_preserve_the_local_charge(self) -> None:
        self.fixture.write_running(used=admission_records.GIB, need=3 * admission_records.GIB)
        baseline = self.fixture.model()
        model = replace(baseline, reading=with_converted_executions(baseline.reading))
        rows = queue_view.running_builds(model)
        self.assertEqual(len(rows), len(queue_view.running_builds(baseline)) + 3)
        self.assertEqual(sum(row.used for row in rows), sum(row.used for row in queue_view.running_builds(baseline)))
        text = plain(queue_view.render_view(model, 160))
        for host in ("local-host", "fixture-mac", "fixture-ssh", "uncertain-mac"):
            self.assertIn(host, text)
        self.assertEqual(model.reading.explanation.room, baseline.reading.explanation.room)
        unknown = next(row for row in rows if row.step.session_id == "remote-uncertain")
        self.assertIsInstance(unknown.step.pid, admission_records.PidNotRecorded)
        self.assertFalse(unknown.measured)
        self.assertEqual(unknown.used, 0)

    def test_targeted_test_step_uses_its_shown_name(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        running = model.reading.view.running
        view = replace(model.reading.view,
            waiting=(replace(waiting[0], step="targeted-test"), *waiting[1:]),
            running=(replace(running[0], step="targeted-test"), *running[1:]))
        model = replace(model, reading=queue_read_fixture(view, policy=model.reading.explanation.policy))
        text = plain(queue_view.render_view(model, 140))
        rows = [row for row in text.splitlines() if "targeted test" in row]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row.startswith("run") for row in rows))
        self.assertNotIn("targeted-test", text)

    def test_running_machine_column_uses_the_private_host_and_preserves_names(self) -> None:
        model = self.fixture.cap_model()
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                text = plain(queue_view.render_view(model, columns))
                header, = [row for row in text.splitlines() if row.lstrip().startswith("# ")]
                rows = [row for row in text.splitlines() if row.startswith("run")]
                self.assertTrue(rows)
                for row in rows:
                    self.assertRegex(row, r"[█░▒]")
                if columns >= 100:
                    self.assertLess(header.index("min"), header.index("machine"))
                    self.assertIn("vCPU", header)
                    self.assertLess(header.index("machine"), header.index("vCPU"))
                    self.assertLess(header.index("vCPU"), header.index("pid"))
                    for row in rows:
                        self.assertEqual(row.index("fixture-host"), header.index("machine"))
                        self.assertEqual(row.index("pid "), header.index("pid"))
                        self.assertEqual(row[header.index("vCPU"):header.index("pid")].strip(), "17")
                    for row in waiting_rows(queue_view.render_view(model, columns)):
                        self.assertEqual(row[header.index("machine"):header.index("pid")].strip(), "")
                else:
                    self.assertNotIn("machine", header)
                    self.assertNotIn("fixture-host", text)
                self.assertTrue(all(terminal_width(row) <= columns for row in text.splitlines()))

    def test_long_machine_name_drops_before_the_view_cuts_names(self) -> None:
        model = self.fixture.cap_model()
        baseline = plain(queue_view.render_view(model, 100))
        with mock.patch.object(store, "host_name", return_value="private-machine-with-a-long-name"):
            text = plain(queue_view.render_view(model, 100))
        header, = [row for row in text.splitlines() if row.lstrip().startswith("# ")]
        self.assertNotIn("machine", header)
        before_rows = [row for row in baseline.splitlines() if "pid " in row]
        after_rows = [row for row in text.splitlines() if "pid " in row]
        self.assertEqual([row.split()[:5] for row in before_rows],
                         [row.split()[:5] for row in after_rows])
        self.assertIn("metrics-gathering", text)
        self.assertIn("metrics-gathering", baseline)

    def test_project_step_text_sets_the_column_width_and_keeps_rows_aligned(self) -> None:
        model = self.fixture.cap_model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, project="nateroids", step="nextest"),
            replace(second, project="long-project-name", step="nextest")])
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                rows = waiting_rows(lines)
                self.assertIn("cables nextest" if columns >= 100 else "cables nexte…", rows[0])
                for marker in ("pid ",):
                    self.assertEqual(terminal_width(rows[0][:rows[0].index(marker)]),
                                     terminal_width(rows[1][:rows[1].index(marker)]))
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
        self.assertEqual(model.reading.view.waiting[0].step, "nextest")

    def test_snapshot_without_project_fields_reads_with_empty_projects(self) -> None:
        reservation = cast(dict[str, object], json.loads(self.fixture.reservation.read_text()))
        reservation["repo"] = ""
        self.fixture.write_json(self.fixture.reservation, reservation)
        reading = self.fixture.model().reading
        encoded = wire.encode_reply(wire.ViewReply(reading))
        # Remove only the new optional field from the complete service reply.
        def without_project(value: object) -> object:
            if isinstance(value, dict):
                return {key: without_project(item) for key, item in cast(dict[str, object], value).items()
                        if key != "project"}
            if isinstance(value, list):
                return [without_project(item) for item in cast(list[object], value)]
            return value
        reply = wire.parse_reply(json.dumps(without_project(cast(object, json.loads(encoded)))))
        self.assertIsInstance(reply, wire.ViewReply)
        if isinstance(reply, wire.ViewReply):
            self.assertEqual(reply.reading, reading)
            self.assertTrue(all(not step.project for step in reply.reading.view.waiting))
            self.assertTrue(all(not step.project for step in reply.reading.view.running))

    def test_service_rows_show_the_repository_project_in_the_view_and_cards(self) -> None:
        cases = (
            ("checkout-alias", admission_records.RepositoryIdentity("repo-key", "nateroids"), "nextest", "nateroids"),
            ("nateroids", admission_records.UnknownRepository(), "nextest", "nateroids"),
            ("/home/natepiano/.claude", admission_records.UnknownRepository(), "merge-gate", ""),
        )
        for repo, repository, step, project in cases:
            with self.subTest(repo=repo), AdmissionFixture() as fixture:
                running = fixture.request(need=admission_records.GIB, step=step)
                running = replace(running, profile=replace(running.profile, repo=repo, repository=repository),
                                  sidecar=str(fixture.scope(running.owner)))
                grant = only_admitted(fixture.server.handle_request(running))
                waiting = fixture.request(need=60 * admission_records.GIB, step=step)
                waiting = replace(waiting, profile=replace(waiting.profile, repo=repo, repository=repository))
                replies = fixture.server.handle_request(waiting)
                self.assertTrue(any(isinstance(reply, wire.Wait) for reply in replies))
                _ = fixture.run_pass()
                model = self.service_model(fixture)
                active, = [row for row in model.reading.view.running if row.pid == running.owner.pid]
                queued, = model.reading.view.waiting
                self.assertEqual((active.project, queued.project), (project, project))
                self.assertEqual((active.step, queued.step), (step, step))
                shown_step = read_step_names().shown(step)
                shown = f"{project} {shown_step}" if project else shown_step
                lines = queue_view.render_view(model, 140)
                active_line, = [plain([line]) for line in lines if f"pid {running.owner.pid}" in plain([line])]
                waiting_line, = waiting_rows(lines)
                self.assertIn(shown, active_line)
                self.assertIn(shown, waiting_line)
                self.assertIn(shown, plain(queue_card.render_card(model, [1], 140)))
                self.assertIn(shown, plain(queue_card.render_selection(model, active.session_id, 140)))
                if not project:
                    self.assertNotIn(repo, active_line)
                    self.assertNotIn(repo, waiting_line)
                fixture.release(grant)

    def test_ci_reservation_label_comes_from_configuration_and_runner_identity(self) -> None:
        with AdmissionFixture() as admission:
            write_memory_config(admission.root / "build_admission.conf", ci_label="CI {runner} {job}")
            request = admission.service_request(runner="a", job="job")
            grant = only_admitted(admission.server.handle_request(request))
            _ = admission.run_pass()
            reply, = admission.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(reply, wire.ViewReply)
            if isinstance(reply, wire.ViewReply):
                row, = [row for row in reply.reading.view.running if row.pid == request.owner.pid]
                self.assertEqual(row.session_name, "CI a job")
                model = replace(self.fixture.model(), reading=reply.reading,
                    facts=head_facts.read_head_facts(reply.reading))
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("CI a job", text)
            admission.release(grant)

    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)})))


    def test_token_wait_keeps_place_one_and_head_status_moves_to_the_eligible_row(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        (token_path, token), (head_path, head) = inputs.ordered[:2]
        inputs = replace(inputs, ordered=[
            (token_path, {**token, "token_left_at": NOW - 10}),
            (head_path, {**head, "priority_since": NOW - 42}),
        ])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        self.assertEqual([step.pid for step in reading.view.waiting], [token["pid"], head["pid"]])
        self.assertIsInstance(queue_view.step_checks(model, 1), admission_records.LineWaiter)
        eligible = queue_view.step_checks(model, 2)
        self.assertIsInstance(eligible, admission_records.HeadExplanation)
        if isinstance(eligible, admission_records.HeadExplanation):
            self.assertEqual(eligible.waiter.pid, head["pid"])
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[0].reason, reading.view.waiting),
                         "waiting for the cargo token")
        self.assertIn("waiting for the cargo token", plain(queue_card.render_card(model, [1], 140)))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual([int(row.split()[0]) for row in rows], [1, 2])
        self.assertNotIn("↑", rows[0])
        self.assertNotIn("⊘", rows[0])
        self.assertIn("↑", rows[1])

    def test_merge_gate_rows_show_the_counted_anonymous_use_beside_the_keep_aside(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        source = admission_records.ProcessSource((admission_records.ProcessAnonymousUse(900, admission_records.AnonymousRunUse(4 * admission_records.GIB)),))
        original = inputs.running[0]
        parent = replace(original, session_id="id-parent-holder", session_name="parent-holder", unit="parent-holder", pid=900, promise=2 * admission_records.GIB,
            measured=admission_records.MeasuredMemory(7 * admission_records.GIB, anonymous=admission_records.AnonymousRunUse(4 * admission_records.GIB),
                                         anonymous_source=source), step="merge-gate")
        child = replace(parent, session_id="id-child-holder", session_name="child-holder", unit="child-holder", promise=6 * admission_records.GIB)
        base = inputs.reservations[0]
        rows = (dict(base, need=2 * admission_records.GIB, reservation_id="z-parent"),
                dict(base, need=6 * admission_records.GIB, reservation_id="a-child"))
        reading = gate.queue_read_from(replace(inputs, running=(parent, child), reservations=rows), admission_policy_fixture())
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        holds = {hold.memory.owner: hold for hold in room.runs}
        self.assertEqual(holds["a-child"].memory.use, admission_records.AnonymousRunUse(4 * admission_records.GIB))
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_view.render_view(model, 200))
        for name, owner in (("parent-holder", "z-parent"), ("child-holder", "a-child")):
            row, = [line for line in text.splitlines() if name in line]
            hold = holds[owner]
            assert isinstance(hold.memory.use, admission_records.AnonymousRunUse)
            self.assertIn(f"{hold.bytes / admission_records.GIB:.1f} / {hold.memory.use.bytes / admission_records.GIB:.1f}", row)

    def service_model(self, fixture: AdmissionFixture) -> queue_view.QueueModel:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return replace(self.fixture.model(), reading=reply.reading,
                       facts=head_facts.read_head_facts(reply.reading))

    def test_running_waiting_and_held_rows_show_run_pids_at_every_width(self) -> None:
        for folder in ("", "builds.slice/builds-detached.slice/verify.scope"):
            with self.subTest(scope=folder), AdmissionFixture() as fixture:
                owner = fixture.fixture_owner(7000001, "100")
                child = fixture.fixture_owner(7000002, "200")
                run = wire.RunIdentity(owner.pid, owner.start_time)
                _ = (fixture.proc.path / str(child.pid) / "environ").write_bytes(
                    f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
                request = replace(fixture.request(owner=child, run=run), sidecar=str(fixture.scope(child, folder=folder)))
                grant = only_admitted(fixture.server.handle_request(request))
                waiter = fixture.request(owner=fixture.fixture_owner(7000003, "300"),
                    run=wire.RunIdentity(7000004, "400"), need=60 * admission_records.GIB)
                _ = fixture.fixture_owner(7000004, "400")
                _ = fixture.server.handle_request(waiter)
                for held in (False, True):
                    if held:
                        _ = fixture.server.handle_request(wire.ReleaseRequest("held", grant.reservation, 0))
                    model = self.service_model(fixture)
                    for columns in (60, 80, 100, 140):
                        with self.subTest(held=held, columns=columns):
                            lines = queue_view.render_view(model, columns)
                            running, = [plain([line]) for line in lines
                                        if plain([line]).startswith("run") and "pid 7000001" in plain([line])]
                            queued, = waiting_rows(lines)
                            self.assertIn("pid 7000001", running)
                            self.assertIn("pid 7000004", queued)
                            self.assertNotIn("pid 7000002", running)
                            self.assertNotIn("pid 7000003", queued)
                            self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                            card = queue_card.render_selection(model, "1", columns)
                            self.assertIn("pid 7000004", plain(card))
                            self.assertTrue(all(terminal_width(plain([line])) <= columns for line in card))
                    if held:
                        self.assertFalse(fixture.server.reservations)
                        room = model.reading.explanation.room
                        assert isinstance(room, admission_records.MemoryRoom)
                        self.assertGreater(sum(hold.memory.held for hold in room.runs), 0)

    def test_service_without_a_recorded_process_shows_pid_absence(self) -> None:
        model = self.fixture.model()
        compiler = admission_records.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
            admission_records.CompilerServiceMemory(admission_records.GIB, pid=admission_records.PidNotRecorded()), pid=admission_records.PidNotRecorded())
        reading = queue_read_fixture(replace(model.reading.view, running=(compiler,)),
                                     policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        for lines in (queue_view.render_view(model, 140), queue_card.render_card(model, [], 140)):
            self.assertIn("pid —", plain(lines))
            self.assertNotIn("pid 0", plain(lines))


    def test_running_row_marks_only_the_asking_session(self) -> None:
        model = self.fixture.model()
        grants = tuple(admission_records.RunningStep(session, session, "merge-gate", "/gate", 6 * admission_records.GIB,
                                        admission_records.MeasuredMemory(admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB)), pid=900) for session in ("mine", "other"))
        reading = queue_read_fixture(replace(model.reading.view, running=grants), policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        for you, expected in (("mine", [1, 0]), ("other", [0, 1]), ("", [0, 0])):
            with self.subTest(you=you):
                rows = [row for row in queue_view.render_view(model, 100, you=you)
                        if plain([row]).startswith("run")]
                self.assertEqual([sum(span.text.count("▸") for span in row if span.tone == "suggestion")
                                  for row in rows], expected)

    def test_service_records_ended_scope_identity_and_the_view_uses_its_label(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "environ").write_bytes(
                b"CLAUDE_CODE_SESSION_ID=ended-session\0CLAUDE_SESSION_NAME=recorded worker\0")
            _ = (process / "cmdline").write_bytes(b"python3\0worker.py\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "cgroup.procs").write_text(str(owner.pid))
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "ended.scope")
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"ended-session": UnitLabels("named worker")})
            step, = (step for step in model.reading.view.running if step.session_id == "ended-session")
            self.assertIsInstance(step.measured, admission_records.UnownedSliceMemory)
            assert isinstance(step.measured, admission_records.UnownedSliceMemory)
            self.assertIsInstance(step.measured.activity, ScopeProcesses)
            text = plain(queue_view.render_view(model, 140))
            self.assertIn("Still running after their build ended (1 process, 1.0 GiB)", text)
            self.assertIn("named worker · python3", text)
            self.assertNotIn("ended-session", text)

    def ci_service_model(self, fixture: AdmissionFixture, *, active: bool) -> queue_view.QueueModel:
        ticks = int(os.sysconf("SC_CLK_TCK"))
        boot = int(fixture.clock.now()) - 172801
        _ = (fixture.proc.path / "stat").write_text(f"btime {boot}\n")
        listener = fixture.fixture_owner(700001, str(ticks))
        process = fixture.proc.path / str(listener.pid)
        _ = (process / "comm").write_text("Runner.Listener\n")
        _ = (process / "cmdline").write_bytes(b"Runner.Listener\0")
        scope = Path(fixture.scope(listener).read_text()).rename(
            fixture.root / admission_records.read_memory_config().ci_memory_slice)
        _ = (scope / "memory.max").write_text(str(8 * admission_records.GIB))
        _ = (scope / "memory.current").write_text(str(int(0.09 * admission_records.GIB)))
        _ = (scope / "memory.swap.current").write_text(str(int(1.98 * admission_records.GIB)))
        if active:
            worker = fixture.fixture_owner(700002, str((172801 - 120) * ticks))
            process = fixture.proc.path / str(worker.pid)
            _ = (process / "comm").write_text("Runner.Worker\n")
            _ = (process / "cmdline").write_bytes(b"Runner.Worker\0")
            _ = (scope / "cgroup.procs").write_text(f"{listener.pid}\n{worker.pid}\n")
        with mock.patch.object(gate, "ci_slice_cgroup", return_value=scope):
            _ = fixture.server.run_pass()
            return self.service_model(fixture)

    def test_idle_ci_keeps_a_small_ram_row_and_separate_swap_without_runner_age(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=False)
            self.assertEqual(model.reading.explanation.totals.charged, int(0.09 * admission_records.GIB))
            self.assertEqual(model.reading.explanation.totals.measured, int(0.09 * admission_records.GIB))
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    rows = [line for line in plain(lines).splitlines() if "ci.slice:" in line]
                    self.assertEqual(len(rows), 1)
                    row, = rows
                    self.assertIn("idle", row)
                    self.assertIn("92 MiB RAM", row)
                    self.assertIn("swap", row)
                    self.assertIn("2.0", row)
                    self.assertNotIn("min", row)
                    for hidden in ("2880", "2 days", "admitted", "Runner.Listener"):
                        self.assertNotIn(hidden, row)

    def test_active_ci_shows_worker_age_instead_of_listener_uptime(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=True)
            ci, = [step for step in model.reading.view.running
                   if isinstance(step.measured, admission_records.UnownedSliceMemory)]
            assert isinstance(ci.measured, admission_records.UnownedSliceMemory)
            self.assertEqual(ci.pid, 700002)
            self.assertEqual(ci.measured.pid, 700002)
            _ = (fixture.proc.path / "700002" / "comm").write_text("Runner.Listener\n")
            _ = (fixture.root / model.ci_memory_slice / "cgroup.procs").write_text("9999999\n")
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    rows = [line for line in plain(lines).splitlines() if "ci.slice:" in line]
                    self.assertEqual(len(rows), 1)
                    row, = rows
                    self.assertIn("job", row)
                    self.assertIn("2 min", row)
                    self.assertIn("pid 700002", row)
                    self.assertNotIn("9999999", row)
                    self.assertIn("swap", row)
                    for hidden in ("idle", "2880", "2 days", "Runner.Listener"):
                        self.assertNotIn(hidden, row)

    def test_running_ci_has_no_keep_aside_in_the_view_or_card(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=True)
            for source, lines in (("view", queue_view.render_view(model, 400)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    self.assertNotIn("CI keep-aside", plain(lines))

    def test_idle_ci_has_no_keep_aside_in_the_view_or_card(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=False)
            for source, lines in (("view", queue_view.render_view(model, 400)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    self.assertNotIn("CI keep-aside", plain(lines))

    def test_build_swap_has_a_separate_label_and_does_not_enter_ram_values(self) -> None:
        model = self.fixture.model()
        step = admission_records.RunningStep("owner", "worker", "clippy", "/tree", 0,
            admission_records.MeasuredMemory(admission_records.GIB // 10, swap_bytes=2 * admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB // 10)), started_at=NOW - 120, pid=123)
        reading = queue_read_fixture(replace(model.reading.view, running=(step,)),
                                     policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        view_row, = [line for line in plain(queue_view.render_view(model, 240)).splitlines()
                     if line.startswith("run")]
        self.assertIn("0.1", view_row)
        self.assertIn("2.0 GiB swapped", view_row)
        self.assertNotIn("2.1", view_row)
        card_row, = [line for line in plain(queue_card.render_card(model, [], 400)).splitlines()
                     if "worker, clippy:" in line]
        self.assertIn("need 0.0 GiB; anonymous use 0.1 GiB; held slot 0.0 GiB; kept aside 0.0 GiB", card_row)
        self.assertIn("2.0 GiB swapped", card_row)

    def test_small_compiler_service_keeps_its_own_ram_row(self) -> None:
        for used in (0, admission_records.GIB // 10):
            model = self.fixture.model()
            compiler = admission_records.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
                                       admission_records.CompilerServiceMemory(used, swap_bytes=2 * admission_records.GIB, pid=admission_records.PidNotRecorded()), pid=admission_records.PidNotRecorded())
            reading = queue_read_fixture(replace(model.reading.view, running=(compiler,)),
                                         policy=admission_policy_fixture())
            model = replace(model, reading=reading)
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(used=used, source=source):
                    rows = [line for line in plain(lines).splitlines() if "sccache" in line]
                    self.assertEqual(len(rows), 1)
                    self.assertIn(f"{used / admission_records.GIB:.1f} GiB RAM", rows[0])
                    self.assertIn("idle", rows[0])
                    self.assertIn("2.0 GiB swapped", rows[0])
                    self.assertNotIn("admission time unknown", rows[0])

    def test_ci_slice_processes_never_appear_as_ended_builds(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "environ").write_bytes(b"CLAUDE_SESSION_NAME=ci-worker\0")
            scope = Path(fixture.scope(owner).read_text()).rename(
                fixture.root / admission_records.read_memory_config().ci_memory_slice)
            _ = (scope / "memory.max").write_text(str(8 * admission_records.GIB))
            for program in ("bash", "Runner.Listener", "Runner.Worker"):
                with self.subTest(program=program):
                    _ = (process / "cmdline").write_bytes(program.encode() + b"\0")
                    _ = (process / "comm").write_text(program)
                    with mock.patch.object(gate, "ci_slice_cgroup", return_value=scope):
                        _ = fixture.server.run_pass()
                    model = self.service_model(fixture)
                    self.assertTrue(any(isinstance(step.measured, admission_records.UnownedSliceMemory)
                                        for step in model.reading.view.running))
                    text = plain(queue_view.render_view(model, 140))
                    self.assertNotIn("Still running after their build ended", text)
                    self.assertNotIn("ci-worker ·", text)

    def test_live_run_owner_outside_the_scope_keeps_its_processes_live(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            child = fixture.fixture_owner(700002, "200")
            process = fixture.proc.path / str(child.pid)
            _ = (process / "environ").write_bytes(b"BUILDLOG_RUN=700001:100\0")
            _ = (process / "cmdline").write_bytes(b"nextest\0")
            scope = Path(fixture.scope(child).read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / f"run-p{owner.pid}-iabc.scope")
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            self.assertEqual(len(model.reading.view.running), 2)
            step, = (step for step in model.reading.view.running
                     if isinstance(step.measured, admission_records.UnownedSliceMemory)
                     and isinstance(step.measured.job_state, NotServiceSlice))
            assert isinstance(step.measured, admission_records.UnownedSliceMemory)
            activity = step.measured.activity
            self.assertIsInstance(activity, ScopeProcesses)
            assert isinstance(activity, ScopeProcesses)
            self.assertIsInstance(activity.run, LiveBuildRun)
            assert isinstance(activity.run, LiveBuildRun)
            self.assertEqual((activity.run.pid, activity.run.start_time), (owner.pid, owner.start_time))
            self.assertNotIn("Still running after their build ended", plain(queue_view.render_view(model, 140)))

    def test_a_detached_supervisor_does_not_make_a_python_scope_a_live_build(self) -> None:
        with AdmissionFixture() as fixture:
            supervisor = fixture.fixture_owner(700001, "100")
            child = fixture.fixture_owner(700002, "200")
            write_process(fixture.proc.path, child.pid, admission_records.GIB, parent=supervisor.pid, start=child.start_time)
            parent = fixture.proc.path / str(supervisor.pid)
            _ = (parent / "cmdline").write_bytes(b"python3\0detached_runs.py\0supervise\0private.json\0")
            process = fixture.proc.path / str(child.pid)
            _ = (process / "cmdline").write_bytes(b"python3\0-m\0unittest\0")
            scope = Path(fixture.scope(child, folder="builds.slice/python.scope").read_text())
            _ = fixture.run_pass()
            model = self.service_model(fixture)
            step, = [step for step in model.reading.view.running
                if isinstance(step.measured, admission_records.UnownedSliceMemory) and step.measured.cgroup == str(scope)]
            assert isinstance(step.measured, admission_records.UnownedSliceMemory)
            activity = step.measured.activity
            assert isinstance(activity, ScopeProcesses)
            self.assertIsInstance(activity.run, NoLiveBuildRun)
            self.assertEqual(queue_view.running_builds(model), [])
            self.assertEqual(queue_view.leftover_processes(model.reading.view.running), [(step, step.measured, activity)])

    def test_ended_processes_use_session_labels_and_exclude_the_ci_slice(self) -> None:
        model = self.fixture.model()
        ended = ScopeProcesses(2, "private-session-id", "recorded worker", "/tree", "python3", NOW - 120,
                               True, NoLiveBuildRun())
        live = replace(ended, run=LiveBuildRun(321, "123", NOW - 180))
        listener = replace(ended, session_id="ci-owner", program="Runner.Listener")
        rows = tuple(admission_records.RunningStep(activity.session_id, activity.session_name, "unowned", activity.folder, 0,
            admission_records.UnownedSliceMemory(admission_records.GIB, owner=model.ci_memory_slice if activity is listener else "opaque.scope",
                                    activity=activity,
                                    job_state=IdleService() if activity is listener else NotServiceSlice(), pid=admission_records.PidNotRecorded()), pid=admission_records.PidNotRecorded())
            for activity in (ended, live, listener))
        reading = queue_read_fixture(replace(model.reading.view, running=rows), policy=admission_policy_fixture())
        for labels, owner in (({"private-session-id": UnitLabels("named worker", "builds", "queue")}, "named worker"),
                              ({}, "recorded worker")):
            with self.subTest(owner=owner):
                text = plain(queue_view.render_view(replace(model, reading=reading, labels=labels), 140))
                section = text.split("Still running after their build ended", 1)[1]
                self.assertIn("2 processes, 1.0 GiB", section)
                self.assertIn(owner + " · python3 · 2 min · 1.0 GiB", section)
                self.assertIn("ask the named owner to stop them if no longer needed", section)
                for hidden in ("private-session-id", "unknown owner", "Runner.Listener", "opaque.scope"):
                    self.assertNotIn(hidden, section)
                self.assertEqual(section.count("python3"), 1)

    def test_leftover_age_ranges_and_edges_match_the_golden(self) -> None:
        model = self.fixture.model()
        minutes = (0, 45, 119, 120, 19 * 60, 47 * 60, 48 * 60 - 1, 48 * 60, 3567)
        rows = tuple(admission_records.RunningStep("worker", "worker", "unowned", "/tree", 0,
            admission_records.UnownedSliceMemory(admission_records.GIB, owner=f"scope-{value}", activity=ScopeProcesses(
                1, "worker", "worker", "/tree", "python3", NOW - value * 60, True, NoLiveBuildRun()), pid=admission_records.PidNotRecorded()), pid=admission_records.PidNotRecorded())
            for value in minutes)
        reading = queue_read_fixture(replace(model.reading.view, running=rows), policy=admission_policy_fixture())
        text = plain(queue_view.render_view(replace(model, reading=reading, labels={}), 140))
        section = "Still running after their build ended" + text.split("Still running after their build ended", 1)[1]
        self.assertEqual(section + "\n", (GOLDEN / "leftover_ages.txt").read_text())

    def test_joined_running_row_counts_process_swap_once(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "status").write_text(
                f"VmRSS: {admission_records.GIB // 1024} kB\nVmSwap: {admission_records.GIB // 1024} kB\n")
            _ = (process / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "memory.swap.current").write_text(str(admission_records.GIB))
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "joined.scope")
            _ = sidecar.write_text(str(builds / "joined.scope"))
            request = replace(fixture.request(owner=owner, run=wire.RunIdentity(owner.pid, owner.start_time)), sidecar=str(sidecar))
            _ = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            row, = queue_view.running_builds(model)
            self.assertEqual(row.used, admission_records.GIB)
            self.assertEqual(row.swap_bytes, admission_records.GIB)
            text = plain(queue_view.render_view(model, 180))
            rendered, = (line for line in text.splitlines() if line.startswith("run"))
            self.assertIn("1.0 GiB swapped", rendered)

    def test_unowned_scope_stays_outside_the_granted_rows(self) -> None:
        model = self.fixture.model()
        grant = admission_records.RunningStep("gate", "gate", "merge-gate", "/gate", 6 * admission_records.GIB, admission_records.MeasuredMemory(admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB)), pid=123)
        scope = admission_records.RunningStep("gate", "gate", "unowned", "/gate", 0,
                                admission_records.UnownedSliceMemory(2 * admission_records.GIB, owner="gate.scope", pid=admission_records.PidNotRecorded()), pid=admission_records.PidNotRecorded())
        reading = queue_read_fixture(replace(model.reading.view, running=(grant, scope)), policy=admission_policy_fixture())
        current = replace(model, reading=reading)
        row, = queue_view.running_builds(current)
        self.assertIs(row.step, grant)
        text = plain(queue_view.render_view(current, 140))
        self.assertEqual(sum(line.startswith("run") for line in text.splitlines()), 1)
        self.assertNotIn("gate.scope", text)
        self.assertEqual(current.reading.explanation.totals.measured, 3 * admission_records.GIB)

    def test_running_and_waiting_share_one_table_without_merging_grants(self) -> None:
        model = self.fixture.model()
        grants = tuple(admission_records.RunningStep(f"gate-{index}", "gate", "merge-gate", f"/gate-{index}",
            6 * admission_records.GIB, admission_records.MeasuredMemory(admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB)), unit="queue-unit", pid=900 + index) for index in range(3))
        reading = queue_read_fixture(replace(model.reading.view, running=grants), policy=admission_policy_fixture())
        explained = tuple(replace(row, admitted_at=NOW - (index + 1) * 60)
                          for index, row in enumerate(reading.explanation.running))
        reading = replace(reading, explanation=replace(reading.explanation, running=explained))
        model = replace(model, reading=reading, labels={**model.labels,
            **{step.session_id: UnitLabels("gate", "builds", "wrong-unit") for step in grants}})
        for columns in (60, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                text = plain(lines)
                header, = [line for line in text.splitlines() if line.lstrip().startswith("# ")]
                running = [line for line in text.splitlines() if line.startswith("run")]
                self.assertEqual(len(running), 3)
                for index, row in enumerate(running):
                    unit = "queue-…" if columns == 60 else "queue-unit"
                    self.assertEqual(row[:header.index("step")].split(),
                                     ["run", "buil…" if columns == 60 else "builds", unit])
                    self.assertEqual(row[header.index("step"):header.index("min")].strip(),
                        cut_tail("queue-unit merge gate", header.index("min") - header.index("step") - 1))
                    self.assertEqual(row[header.index("min"):].split()[0], str(index + 1))
                    self.assertIn("5.0", row)
                    if columns >= 100:
                        self.assertIn("5.0 / 1.0", row)
                waiting = waiting_rows(lines)
                self.assertEqual([int(row.split()[0]) for row in waiting], list(range(1, 33)))
                self.assertLess(text.index(running[-1]), text.index(waiting[0]))
                self.assertEqual(sum(line.lstrip().startswith("# ") for line in text.splitlines()), 1)
                self.assertTrue(all(terminal_width(line) <= columns for line in text.splitlines()))

    def test_unit_override_survives_restart_and_matches_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request()
            later = fixture.request(since=first.since + 1)
            later = replace(later, identity=replace(later.identity, unit="queue-unit"),
                            sidecar=str(fixture.scope(later.owner)))
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            fixture.restart()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            labels = {first.identity.session_id: UnitLabels("first", "builds", "other-unit"),
                      later.identity.session_id: UnitLabels("gate", "builds", "wrong-unit")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                refused, = fixture.completed_queue_change(wire.PrioritySetRequest("wrong-bump", "wrong-unit", "urgent", "user"))
                self.assertIsInstance(refused, wire.Refused)
                replies = fixture.completed_queue_change(wire.PrioritySetRequest("bump-unit", "queue-unit", "urgent", "user"))
                self.assertEqual(replies, [wire.Ok()])
                model = self.service_model(fixture)
                self.assertEqual([step.pid for step in model.reading.view.waiting], [later.owner.pid, first.owner.pid])
                self.assertEqual(model.reading.view.waiting[0].unit, "queue-unit")
                self.assertEqual(model.reading.view.waiting[1].unit, "")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[1], labels).unit, "other-unit")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[0], {}).unit, "queue-unit")
                fixture.free(60 * admission_records.GIB)
                write_pressure_reading(fixture.root / "pressure")
                _ = fixture.server.run_pass()
                _ = fixture.server.run_pass()
                model = self.service_model(fixture)
                running = next(step for step in model.reading.view.running if step.session_id == later.identity.session_id)
                self.assertEqual(queue_evidence.step_labels(running, labels).unit, "queue-unit")

    def test_unit_override_view_row_matches_golden(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        step = replace(waiting[0], unit="queue-unit", step="merge-gate")
        model = replace(model, labels={**model.labels, step.session_id: UnitLabels("gate", "builds")},
            reading=replace(model.reading, view=replace(model.reading.view, waiting=(step, *waiting[1:]))))
        rows = plain(queue_view.render_view(model, 140)).splitlines()
        row, = [row for row in rows if "queue-unit" in row]
        self.assertEqual(row + "\n", (GOLDEN / "unit_override_view.txt").read_text())

    def test_showrunner_without_a_unit_keeps_the_empty_unit_label(self) -> None:
        model = self.fixture.model()
        model = replace(model, labels={session: UnitLabels("builds", "builds") for session in model.labels})
        text = plain(queue_view.render_view(model, 140))
        rows = [line for line in text.splitlines() if line.startswith("run") or line.lstrip().startswith("1 ")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.split()[1:3], ["builds", "—"])
    def test_merge_gate_grant_has_a_running_row_with_use_and_grant(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate")
            request = replace(request, identity=wire.Identity("gate-owner", "gate-worker", "", str(fixture.root)),
                profile=admission_records.ProgramProfile("merge-gate", "repo", "tree", admission_records.HistoryPeakSizing(6 * admission_records.GIB, admission_records.FirstProgramPeak(6 * admission_records.GIB), 0.0)),
                sidecar=str(fixture.scope(request.owner)))
            _ = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(7 * 60)
            fixture.free(10 * admission_records.GIB)
            _ = fixture.server.handle_request(fixture.request(need=8 * admission_records.GIB))
            model = replace(self.service_model(fixture), labels={"gate-owner": UnitLabels("gate-worker", "devtools", "devtools")})
            for colour in (False, True):
                lines = queue_view.render_view(model, 140, colour=colour)
                text = plain(lines)
                running = "\n".join(line for line in text.splitlines() if line.startswith("run"))
                queued = "\n".join(waiting_rows(lines))
                row, = [line for line in running.splitlines() if read_step_names().shown("merge-gate") in line]
                self.assertIn("devtools", row)
                self.assertIn("5.0 / 1.0", row)
                self.assertIn("7", row.split())
                self.assertEqual(row.count(read_step_names().shown("merge-gate")), 1)
                queued_row, = waiting_rows(lines)
                bar_characters = "░█▏▎▍▌▋▊▉"
                bar_column = terminal_width(row[:next(index for index, char in enumerate(row) if char in bar_characters)])
                self.assertEqual(bar_column, terminal_width(queued_row[:next(
                    index for index, char in enumerate(queued_row) if char in bar_characters)]))
                rendered_lines = text.splitlines()
                cap_index = next(index for index, line in enumerate(rendered_lines) if line.lstrip().startswith("cap "))
                top = rendered_lines[cap_index + 1]
                self.assertEqual(bar_column, terminal_width(top[:next(
                    index for index, char in enumerate(top) if char in "░█·▒│")]))
                self.assertIn("8.0", queued)
                self.assertIn("░", row)

    def test_live_scope_without_a_reservation_or_hold_has_no_queue_row(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (fixture.proc.path / "stat").write_text(
                f"btime {fixture.clock.now() - 12 * 60 - 100 / os.sysconf('SC_CLK_TCK')}\n")
            _ = (process / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0CLAUDE_CODE_SESSION_ID=between-owner\0".encode())
            _ = (process / "cmdline").write_bytes(b"bash\0verify.sh\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "between.scope")
            _ = sidecar.write_text(str(builds / "between.scope"))
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"between-owner": UnitLabels("worker", "hana", "hana")})
            text = plain(queue_view.render_view(model, 140))
            self.assertFalse(fixture.server.reservations)
            room = model.reading.explanation.room
            assert isinstance(room, admission_records.MemoryRoom)
            self.assertFalse(room.runs)
            self.assertTrue(any(isinstance(step.measured, admission_records.UnownedSliceMemory)
                                and isinstance(step.measured.activity, ScopeProcesses)
                                and isinstance(step.measured.activity.run, LiveBuildRun)
                                for step in model.reading.view.running))
            self.assertFalse(queue_view.running_builds(model))
            self.assertNotIn("between steps", text)
            self.assertFalse(any(line.startswith("run") for line in text.splitlines()))
            self.assertNotIn("Still running after their build ended", text)

    def test_live_scope_with_a_hold_shows_one_between_steps_row(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            child = fixture.fixture_owner(700002, "200")
            _ = (fixture.proc.path / str(child.pid) / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
            sidecar = fixture.scope(child)
            scope = Path(sidecar.read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            scope = scope.rename(builds / "held.scope")
            _ = sidecar.write_text(str(scope))
            request = replace(fixture.request(owner=child, run=wire.RunIdentity(owner.pid, owner.start_time)),
                              sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.release(grant)
            _ = fixture.run_pass()
            model = self.service_model(fixture)
            self.assertFalse(fixture.server.reservations)
            room = model.reading.explanation.room
            assert isinstance(room, admission_records.MemoryRoom)
            hold, = room.runs
            self.assertGreater(hold.bytes, 0)
            self.assertTrue(any(isinstance(step.measured, admission_records.UnownedSliceMemory)
                                and isinstance(step.measured.activity, ScopeProcesses)
                                and isinstance(step.measured.activity.run, LiveBuildRun)
                                for step in model.reading.view.running))
            row, = queue_view.running_builds(model)
            self.assertEqual(row.kept, hold.bytes)
            self.assertEqual(row.step.run, hold.memory.run)
            text = plain(queue_view.render_view(model, 140))
            line, = [line for line in text.splitlines() if line.startswith("run")]
            self.assertIn("between steps", line)
            self.assertIn(f"pid {owner.pid}", line)

    def test_ended_run_scope_names_its_process_owner_program_and_age(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700002, "100")
            second = fixture.fixture_owner(700003, "101")
            process = fixture.proc.path / str(owner.pid)
            _ = (fixture.proc.path / "stat").write_text(
                f"btime {fixture.clock.now() - 2 * 86400 - 100 / os.sysconf('SC_CLK_TCK')}\n")
            _ = (process / "environ").write_bytes(b"BUILDLOG_RUN=999999:1\0CLAUDE_CODE_SESSION_ID=ended-owner\0")
            _ = (process / "cmdline").write_bytes(b"python3\0-m\0unittest\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "cgroup.procs").write_text(f"{owner.pid}\n{second.pid}\n")
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "unreadable-run-name.scope")
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"ended-owner": UnitLabels("worker", "builds", "queue-unit")})
            ended, = [step for step in model.reading.view.running
                      if isinstance(step.measured, admission_records.UnownedSliceMemory) and step.measured.owner == "unreadable-run-name.scope"]
            assert isinstance(ended.measured, admission_records.UnownedSliceMemory)
            self.assertEqual(ended.pid, owner.pid)
            self.assertEqual(ended.measured.pid, owner.pid)
            activity = ended.measured.activity
            assert isinstance(activity, ScopeProcesses)
            self.assertEqual([member.pid for member in activity.members], [owner.pid, second.pid])
            _ = (builds / "unreadable-run-name.scope" / "cgroup.procs").write_text("9999999\n")
            text = plain(queue_view.render_view(model, 140))
            section = text.split("Still running after their build ended", 1)[1]
            self.assertIn("2 processes", section)
            self.assertIn("1.0 GiB", section)
            self.assertIn("worker · python3", section)
            self.assertIn(f"pid {owner.pid}", section)
            self.assertIn(f"pid {second.pid}", section)
            self.assertNotIn("9999999", section)
            self.assertNotIn("ended-owner", section)
            self.assertIn("python3", section)
            self.assertIn("2 d", section)
            self.assertNotIn("unreadable-run-name.scope", text)
            self.assertNotIn("(unowned)", text)

    def test_empty_scope_cache_remains_charged_but_has_no_visible_row(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/cache-only.scope"
            scope.mkdir(parents=True)
            _ = (scope / "memory.current").write_text(str(5 * admission_records.GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = (scope / "cgroup.procs").write_text("")
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            self.assertEqual(model.reading.explanation.totals.charged, 5 * admission_records.GIB)
            text = plain(queue_view.render_view(model, 140))
            self.assertIn("room", text)
            self.assertNotIn("taken", text)
            self.assertNotIn("cache-only", text)
            self.assertNotIn("Still running after their build ended", text)
            self.assertNotIn("(unowned)", text)

    def test_service_counts_unowned_scope_once_and_renders_the_held_heads_numbers(self) -> None:
        import os
        from pathlib import Path
        from scripts.build_queue import admission_client as client
        from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
        with AdmissionFixture() as server:
            request = server.request(need=admission_records.GIB)
            sidecar = server.scope(request.owner)
            owned = Path(sidecar.read_text())
            builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
            builds.mkdir(parents=True, exist_ok=True)
            _ = owned.rename(builds / "owned.scope")
            _ = sidecar.write_text(str(builds / "owned.scope"))
            _ = only_admitted(server.server.handle_request(replace(request, sidecar=str(sidecar))))
            unowned = builds / "replay.scope"
            unowned.mkdir()
            amount = int(17.4 * admission_records.GIB)
            _ = (unowned / "memory.current").write_text(str(amount))
            _ = (unowned / "memory.stat").write_text("inactive_file 0\n")
            _ = (unowned / "cgroup.procs").write_text("")
            server.free(12 * admission_records.GIB)
            _ = server.server.handle_request(server.request(need=8 * admission_records.GIB))
            with server.serving():
                reading = client.view()
            measured = [step.measured for step in reading.view.running
                        if isinstance(step.measured, admission_records.UnownedSliceMemory)]
            self.assertEqual(len(measured), 2)
            scope, = (memory for memory in measured if isinstance(memory.job_state, NotServiceSlice))
            idle, = (memory for memory in measured if isinstance(memory.job_state, IdleService))
            self.assertEqual(idle.bytes, 0)
            self.assertEqual(scope.bytes, amount)
            head = reading.explanation.head
            assert isinstance(head, admission_records.HeadExplanation)
            self.assertEqual(head.decision.charged, admission_records.GIB)
            self.assertEqual(head.free.available, 12 * admission_records.GIB)
            model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
            view = plain(queue_view.render_view(model, 400))
            card = plain(queue_card.render_card(model, [1], 400))
            page = view + "\n\n" + card
            self.assertNotIn("replay.scope", page)
            self.assertNotIn("Still running after their build ended", page)
            self.assertIn("Room: need 8.0 GiB <= room 9.0 GiB: pass", page)
            self.assertNotIn("owned.scope:", page)
            self.assertEqual(page.count("Room: need 8.0 GiB"), 1)

    def test_expired_head_with_passing_checks_has_an_error_hold_at_sixty_cells(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, row = inputs.ordered[0]
        inputs = replace(inputs, running=(), reservations=(), pressure=admission_records.NoPressureHold(),
            host=admission_records.HostMemory(60 * admission_records.GIB, 60 * admission_records.GIB),
            ordered=[(path, {**row, "need": admission_records.GIB, "estimated_at": NOW - 600,
                            "priority_since": NOW - 42}), inputs.ordered[1]])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        head = reading.explanation.head
        assert isinstance(head, admission_records.HeadExplanation)
        self.assertTrue(head.room.passes)
        self.assertTrue(head.free.passes)
        self.assertIsInstance(head.decision.pressure, admission_records.NoPressureHold)
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        lines = queue_view.render_view(model, 60)
        rows = waiting_rows(lines)
        text_lines = plain(lines).splitlines()
        diagnostic_lines = [rows[0]]
        for line in text_lines[text_lines.index(rows[0]) + 1:]:
            if not line.startswith("· "):
                break
            diagnostic_lines.append(line.removeprefix("· "))
        self.assertIn("↑ estimate expired", " ".join(diagnostic_lines))
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[0].reason, reading.view.waiting),
                         "estimate expired")
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))
        self.assertNotIn("█", rows[0])
        self.assertTrue(any(span.text.endswith("estimate expired") and span.tone == "error"
                            for line in lines for span in line))

    def test_pressure_threshold_text_uses_the_policy_in_the_reading(self) -> None:
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config, full_pressure_limit="9", some_pressure_limit="12", recovery_start_seconds="45")
        with mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)}):
            policy = admission_records.read_memory_config().policy
            write_memory_config(config, full_pressure_limit="3", some_pressure_limit="30")
            model = self.fixture.model()
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, policy=policy)))
        with mock.patch.object(admission_records, "read_memory_config", side_effect=AssertionError("render read config")):
            _, full = queue_view.pressure_check(admission_records.MemoryPressure(10, condition="full"), model)
            _, some = queue_view.pressure_check(admission_records.MemoryPressure(0, 15, 80, condition="some"), model)
            _, recovery = queue_view.pressure_check(admission_records.MemoryPressure(0, next_start_at=NOW + 45, condition="full"), model)
        self.assertIn("starts below full avg10 9.0%", full)
        self.assertIn("starts below some avg10 12.0%", some)
        self.assertIn("starts below full avg10 9.0%", recovery)
        self.assertNotIn("one start per", recovery)

    def test_live_priority_marks_and_cards_use_the_snapshot_without_bump_history(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(model, facts=replace(facts, bump_records=(), bumps=head_facts.BumpHistory((), 0, ())))
        self.assertEqual([step.session_id for step in model.reading.view.waiting[:3]],
                         ["id-work-pan", "id-work-lead", "id-work-lead"])
        with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("captured roster")), \
             mock.patch.object(gate, "read_queue_priority", side_effect=AssertionError("captured priority")):
            self.assertEqual([queue_view.is_moved(model, position) for position in range(1, 5)],
                             [True, True, True, False])
            self.assertEqual(queue_view.waiting_moves(model, 1), ())
            view = plain(queue_view.render_view(model, 100))
            card = plain(queue_card.render_card(model, [1], 100))
            frozen = gate.head_after_steps_end(model.reading.explanation, ())
        self.assertIn("queue priority: cables (captured priority)", view)
        self.assertIn("queue priority: cables (captured priority)", card)
        self.assertIn("waiting 36 min", card)
        for text in (view, card):
            self.assertNotIn("head_since", text)
            self.assertNotIn("became first", text)
        head = model.reading.explanation.head
        assert isinstance(head, admission_records.HeadExplanation)
        self.assertEqual(head.waiter.session_id, "id-work-pan")
        self.assertIsInstance(frozen, gate.HeadStillHeld)
        assert isinstance(frozen, gate.HeadStillHeld)
        self.assertEqual(frozen.decision, head.decision)

    def test_live_bump_marks_every_unit_row_without_moving_columns(self) -> None:
        model = self.fixture.model()
        for units in (("cables",), ("widget",), ("cables", "widget")):
            priority = admission_records.QueuePriority(tuple(
                admission_records.PriorityEntry(unit=unit, why="test", set_at=NOW, set_by="actor") for unit in units))
            bumped = replace(model, reading=replace(model.reading,
                explanation=replace(model.reading.explanation, priority=priority)))
            unbumped = replace(model, reading=replace(model.reading,
                explanation=replace(model.reading.explanation, priority=admission_records.NoQueuePriority())))
            for columns in (40, 60, 80, 100, 140):
                with self.subTest(units=units, columns=columns):
                    baseline = queue_view.render_view(unbumped, columns, you=ASKING_SESSION, selector="widget")
                    with mock.patch.object(head_facts, "queue_moves", side_effect=AssertionError("bump history read")), \
                         mock.patch.object(gate, "read_queue_priority", side_effect=AssertionError("live priority read")):
                        lines = queue_view.render_view(bumped, columns, you=ASKING_SESSION, selector="widget")
                    waiting = set(waiting_rows(lines))
                    previous_waiting = set(waiting_rows(baseline))
                    rows = [line for line in lines if plain([line]).startswith("run") or plain([line]) in waiting]
                    previous = [line for line in baseline if plain([line]).startswith("run") or plain([line]) in previous_waiting]
                    steps = [row.step for row in queue_view.running_builds(model)] + list(model.reading.view.waiting)
                    self.assertEqual(len(rows), len(steps))
                    for row, before, step in zip(rows, previous, steps):
                        expected = model.labels[step.session_id].unit in units
                        self.assertEqual(sum(span.text == "●" for span in row), int(expected))
                        # The step, minutes, bars and selection mark retain their positions.
                        unit_end = 5 if expected else 4
                        self.assertEqual(row[unit_end:], before[4:])
                        self.assertEqual(terminal_width(plain([row[:unit_end]])), terminal_width(plain([before[:4]])))
                        self.assertEqual(row[:3], before[:3])
                    data = json_form(lines, columns, NOW)
                    self.assertEqual(sum(part["text"] == "●" and part.get("tone") == "warning"
                                         for row in data["lines"] for part in row),
                                     sum(model.labels[step.session_id].unit in units for step in steps))

    def test_no_live_bump_keeps_the_recorded_board_bytes(self) -> None:
        model = self.fixture.cap_model()
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, priority=admission_records.NoQueuePriority())))
        self.assertEqual(plain(queue_view.render_view(model, 100, you=ASKING_SESSION)) + "\n",
                         (GOLDEN / "no_bump_100.txt").read_text())



    def test_unrelated_bump_history_adds_no_record_visits_to_render(self) -> None:
        model = self.fixture.model()
        steps = tuple(replace(model.reading.view.waiting[0], pid=pid,
                              session_id=f"row-{pid}", folder=f"/tmp/row-{pid}")
                      for pid in range(4000))
        reading = queue_read_fixture(replace(model.reading.view, waiting=steps), policy=admission_policy_fixture())
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        empty = replace(model, reading=reading, facts=replace(facts, bump_records=()))
        records = BumpRecordsWithVisitCount(tuple(
            head_facts.BumpRecord(NOW - 1, "actor", f"unrelated-{index}",
                                  "check", f"/tmp/unrelated-{index}", 20)
            for index in range(5000)))
        history = replace(empty, facts=replace(facts, bump_records=records))
        expected = plain(queue_view.render_view(empty, 100))
        self.assertEqual(plain(queue_view.render_view(history, 100)), expected)
        self.assertEqual(records.visits, 0, "the view must not scan unrelated bump records")


    def test_name_column_stays_aligned_when_places_grow_to_three_digits(self) -> None:
        model = self.fixture.model()
        step = model.reading.view.waiting[0]
        steps = tuple(replace(step, pid=pid) for pid in range(1, 101))
        model = replace(model, reading=queue_read_fixture(replace(model.reading.view, waiting=steps), policy=admission_policy_fixture()))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual(len(rows), 100)
        for row, position in zip(rows[98:], (99, 100)):
            self.assertEqual(int(row.split()[0]), position)
        self.assertEqual(terminal_width(rows[98][:rows[98].index("tool-based-ui")]),
                         terminal_width(rows[99][:rows[99].index("tool-based-ui")]))


    def test_every_width_keeps_one_waiting_row_per_step_without_running_records(self) -> None:
        for columns in (30, 40, 60, 80, 100, 140, 400):
            with self.subTest(columns=columns):
                rendered = queue_view.render_view(self.fixture.cap_model(), columns)
                rows = waiting_rows(rendered)
                self.assertEqual(len(rows), 32)
                self.assertEqual([int(row.split()[0]) for row in rows], list(range(1, 33)))
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in rendered))
                self.assertFalse(any(row.startswith("Session:") for row in rows))
                self.assertEqual(sum(line.startswith("run") for line in plain(rendered).splitlines()), 1)
                self.assertEqual(sum(line.lstrip().startswith("# ") for line in plain(rendered).splitlines()), 1)
                self.assertEqual(plain(rendered).count("●"), 3)
                if columns == 30:
                    self.assertTrue(any(line.startswith("· ") for line in plain(rendered).splitlines()))
                for removed in ("starts", "moved up:", "#1 waits on"):
                    self.assertNotIn(removed, plain(rendered))

    def test_card_checks_keep_all_room_numbers_and_failure_tones_when_wrapped(self) -> None:
        model = self.fixture.model()
        checks = queue_view.step_checks(model, 1)
        assert isinstance(checks, admission_records.HeadExplanation)
        expected = queue_evidence.room_check_text(checks.room)
        self.assertEqual(checks.room.need, int(7.6 * admission_records.GIB))
        for columns in (400, 60, 30):
            with self.subTest(columns=columns):
                pressure = queue_view.check_line(*queue_view.pressure_check(checks.decision.pressure, model))
                lines = [row for check in [pressure, *queue_view.check_lines(checks, model)]
                         for row in wrap(check, columns)]
                text = "".join(plain(lines).split())
                for fact in ("✗ memory pressure: full avg10 7.7%, some avg10 0.0%, swap used 0.0%; starts below full avg10 5.0%", expected):
                    self.assertIn("".join(fact.split()), text)
                self.assertEqual(sum(span.text.count("✗") for row in lines for span in row if span.tone == "error"),
                                 1 + int(not checks.room.passes))
                self.assertEqual(sum(span.text.count("✓") for row in lines for span in row if span.tone == "success"),
                                 int(checks.room.passes))

    def test_step_columns_align_wide_and_combining_names_by_cells(self) -> None:
        model = self.fixture.cap_model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, step="界e\u0301"), replace(second, step="doc")])
        rows = waiting_rows(queue_view.render_view(model, 140))
        for marker in ("tool-based-ui", "█"):
            self.assertEqual(terminal_width(rows[0][:rows[0].index(marker)]),
                             terminal_width(rows[1][:rows[1].index(marker)]))
        self.assertIn("界e\u0301", rows[0])

    def test_view_ignores_historical_moves_token_history_and_pace(self) -> None:
        model = self.fixture.cap_model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        expected = plain(queue_view.render_view(model, 100))
        wait = head_facts.CargoTokenWait("id-work-lead", "nextest", 1001, "1001", NOW - 200,
                                       head_facts.TokenReturned(NOW - 46))
        for changed in (replace(facts, bumps=head_facts.BumpsUnavailable("unreadable")),
                        replace(facts, bump_records=()),
                        replace(facts, token_waits=head_facts.RecordedTokenWaits((wait,), False, (wait,)))):
            for pace in (head_facts.RecordedPace(0), head_facts.PaceUnavailable()):
                self.assertEqual(plain(queue_view.render_view(replace(model, facts=changed, pace=pace), 100)), expected)
        for removed in ("unknown", "cargo token:", "Head for", "moved up:", "starts:"):
            self.assertNotIn(removed, expected)

    def test_empty_queue_keeps_cap_and_header_without_invented_steps(self) -> None:
        model = self.fixture.cap_model()
        reading = queue_read_fixture(admission_records.QueueView((), (), 24 * admission_records.GIB, 0, 3 * admission_records.GIB, NOW), policy=admission_policy_fixture())
        rendered = queue_view.render_view(replace(model, reading=reading, facts=admission_records.NoWaiter()), 100)
        text = plain(rendered)
        self.assertTrue(text.startswith("Build queue · 10:46\n\n"))
        self.assertIn("room 21.0", text)
        self.assertIn("cap ", text)
        self.assertEqual(waiting_rows(rendered), [])
        self.assertNotIn("⊘", text)
        self.assertNotIn("↑", text)

    def test_card_checks_use_the_recorded_room_without_redeciding(self) -> None:
        model = self.fixture.model()
        checks = queue_view.step_checks(model, 1)
        assert isinstance(checks, admission_records.HeadExplanation)
        with mock.patch.object(gate, "admission_pass", side_effect=AssertionError("admission decides once")),              mock.patch.object(gate, "memory_room", side_effect=AssertionError("recorded room")):
            text = plain(queue_view.check_lines(checks, model))
        self.assertEqual(checks.room.need, int(7.6 * admission_records.GIB))
        self.assertIn(queue_evidence.room_check_text(checks.room), text)
        self.assertNotIn("budget", text.lower())


class CapViewTests(QueueViewFixtureTests):
    def cap_model(self, *, available: int = 20, need: int = 10, used: int = 5) -> queue_view.QueueModel:
        model = self.fixture.cap_model()
        room = gate.memory_room(admission_records.HostMemory(available * admission_records.GIB, 60 * admission_records.GIB), [admission_records.RunMemory("run", need * admission_records.GIB, admission_records.AnonymousRunUse(used * admission_records.GIB))], admission_policy_fixture())
        return replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, room=room)))


    def test_compact_page_matches_reviewed_golden_files(self) -> None:
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(self.fixture.cap_model(), columns, you=ASKING_SESSION)
                self.assertEqual(plain(lines) + "\n", (GOLDEN / f"view_{columns}.txt").read_text())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                page = [*lines, (), *queue_card.render_selection(self.fixture.cap_model(), "1", columns)]
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in page))

    def test_cap_and_row_bars_share_the_sixty_column_page(self) -> None:
        lines = queue_view.render_view(self.cap_model(), 60)
        text = plain(lines).splitlines()
        self.assertEqual(text[:2], ["Build queue · 10:46", ""])
        self.assertIn("cap 22.0", plain(lines))
        bar = next(line for line in lines if "│" in plain([line]) and "█" in plain([line]))
        prefix = ""
        for span in bar:
            if "█" in span.text:
                break
            prefix += span.text
        rows = waiting_rows(lines)
        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0].index("█"), terminal_width(prefix))
        self.assertEqual([int(row.split()[0]) for row in rows], list(range(1, 33)))
        self.assertNotIn("budget", plain(lines).lower())
        self.assertTrue(all(terminal_width(line) <= 60 for line in text))

    def test_cap_orders_use_keep_room_safety_and_other_programs(self) -> None:
        room = self.cap_model().reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar]), "█" * 5 + "░" * 5 + "█" * 12 + "│" + "█" * 37)
        self.assertEqual([span.tone for span in bar],
            ["suggestion"] * 5 + ["warning"] * 5 + ["success"] * 12
            + ["error"] + ["warning"] * 2 + ["inactive"] * 35)
        self.assertEqual(queue_evidence.room_text(room),
            "Cap 22.0 GiB; anonymous use 5.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB")
        self.assertEqual(queue_evidence.keep_aside_text(room.runs[0]),
            "need 10.0 GiB; anonymous use 5.0 GiB; held slot 0.0 GiB; kept aside 5.0 GiB")

    def test_view_and_cap_bar_include_ci_use_in_the_shared_cap(self) -> None:
        model = self.fixture.cap_model()
        policy = admission_policy_fixture()
        inputs = self.fixture.inputs(policy=policy)
        run = replace(inputs.running[0], promise=10 * admission_records.GIB,
                      measured=admission_records.MeasuredMemory(5 * admission_records.GIB, anonymous=admission_records.AnonymousRunUse(5 * admission_records.GIB)))
        ci = ci_step(4 * admission_records.GIB, RunningServiceJob(NOW - 60))
        inputs = replace(inputs, running=(run, ci),
            reservations=({**inputs.reservations[0], "need": 10 * admission_records.GIB},),
            host=admission_records.HostMemory(20 * admission_records.GIB, 60 * admission_records.GIB))
        reading = gate.queue_read_from(inputs, policy)
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        shown = plain(queue_view.render_view(replace(model, reading=reading), 140))
        self.assertIn("cap 26.0", shown)
        self.assertIn("Cap 26.0 GiB", queue_evidence.room_text(room))
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar[:9]]), "█" * 9)
        self.assertEqual([span.tone for span in bar],
            ["suggestion"] * 9 + ["warning"] * 5 + ["success"] * 12 + ["error"] + ["warning"] * 2 + ["inactive"] * 31)

    def test_unreadable_meminfo_reports_no_reading_instead_of_low_memory(self) -> None:
        _ = fake_memory_readings(self.fixture.root, full_avg10=0.0)
        model = self.fixture.cap_model()
        (self.fixture.root / "meminfo").unlink()
        reading = self.fixture.read()
        head = reading.explanation.head
        assert isinstance(head, admission_records.HeadExplanation)
        self.assertIsInstance(head.cause, admission_records.ReadingUnavailable)
        self.assertFalse(head.free.passes)
        model = replace(model, reading=reading)
        lines = queue_view.render_view(model, 180, colour=True)
        first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
        self.assertTrue(plain([first]).endswith("memory reading unavailable"))
        self.assertNotIn("low memory", plain([first]))
        self.assertNotIn("pressure", plain([first]))
        self.assertTrue(any("memory reading unavailable" in span.text and span.tone == "error" for span in first))

    def test_head_room_check_uses_the_recorded_host_and_keep_aside(self) -> None:
        model = self.fixture.model()
        for available, expected_room, expected_passes in ((10, 1, False), (20, 11, True)):
            with self.subTest(available=available):
                inputs = self.fixture.inputs(policy=admission_policy_fixture())
                inputs = replace(inputs, pressure=admission_records.NoPressureHold(),
                    host=admission_records.HostMemory(available * admission_records.GIB, 60 * admission_records.GIB),
                    reservations=tuple({**row, "need": 6 * admission_records.GIB} for row in inputs.reservations),
                    running=(replace(inputs.running[0], promise=6 * admission_records.GIB,
                        measured=admission_records.MeasuredMemory(0, anonymous=admission_records.AnonymousRunUse(0))),))
                reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
                head = reading.explanation.head
                assert isinstance(head, admission_records.HeadExplanation)
                self.assertEqual(head.room.room, expected_room * admission_records.GIB)
                self.assertEqual(head.room.passes, expected_passes)
                shown = replace(model, reading=reading)
                self.assertIn(queue_evidence.room_check_text(head.room),
                              plain(queue_card.render_card(shown, [1], 400)))

    def test_empty_run_set_has_room_before_the_cap_and_safety_after_it(self) -> None:
        room = self.cap_model(need=0, used=0).reading.explanation.room
        bar = queue_view.cap_bar(room, 60, colour=False)
        self.assertEqual(plain([bar]), "·" * 17 + "│" + "▒" * 2 + "█" * 40)
        self.assertTrue(all(span.tone == "success" for span in bar[:17]))
        self.assertEqual(bar[17].tone, "error")


    def test_row_styles_and_you_marker_match_the_drawn_page(self) -> None:
        lines = queue_view.render_view(self.fixture.cap_model(), 60, colour=True, you=ASKING_SESSION)
        rows = [line for line in lines if plain([line]) in waiting_rows(lines)]
        for position, row in enumerate(rows, 1):
            with self.subTest(position=position):
                self.assertTrue(any(str(position) in span.text and span.tone == ("warning" if position == 1 else "inactive")
                                    for span in row))
                self.assertTrue(any(span.tone == "suggestion" and ("cables" in span.text or position > 3)
                                    for span in row))
                self.assertEqual(sum(span.text.count("▸") for span in row if span.tone == "suggestion"),
                                 1 if position in (2, 3) else 0)
                self.assertTrue(any(span.tone == "warning" and "█" in span.text for span in row))
        self.assertEqual(lines[0][0].tone, "inactive")

    def test_negative_room_shows_zero_and_the_cap_explains_the_shortfall(self) -> None:
        model = self.cap_model(available=2, need=10, used=5)
        room = model.reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        self.assertEqual((room.room, room.shortfall, room.cap), (0, 6 * admission_records.GIB, 4 * admission_records.GIB))
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar]), "████│" + "█" * 55)
        self.assertEqual([span.tone for span in bar[:5]], ["suggestion"] * 4 + ["error"])
        self.assertFalse(any(span.tone == "success" for span in bar))
        self.assertIn("shortfall 6.0 GiB", queue_evidence.room_text(room))
        lines = queue_view.render_view(model, 60, colour=True)
        self.assertIn("room 0.0", plain(lines))
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))

    def test_size_bars_show_uncut_needs_with_nearest_eighth_and_zero(self) -> None:
        model = self.fixture.cap_model()
        for columns, need, expected in ((100, 6.8, "██████▊"), (100, 0, ""), (100, 25, "█" * 23),
                                        (120, 6.8, "██████▊"), (120, 0, ""), (120, 25, "█" * 25)):
            with self.subTest(columns=columns, need=need):
                step = replace(model.reading.view.waiting[1], need=int(need * admission_records.GIB))
                changed = with_waiting(model, [model.reading.view.waiting[0], step])
                lines = queue_view.render_view(changed, columns, colour=True)
                row = next(line for line in lines if plain([line]).lstrip().startswith("2 "))
                self.assertEqual("".join(span.text for span in row if span.tone == "warning"), "●" + expected)
                self.assertTrue(any(f"{need:.1f}" in span.text and span.tone == "inactive" for span in row))

    def test_long_name_growth_and_missing_unit_session_fallback(self) -> None:
        model = self.fixture.cap_model()
        first, second = model.reading.view.waiting[:2]
        first = replace(first, step="nextest")
        labels = dict(model.labels)
        labels[first.session_id] = UnitLabels(
            "session", "performance", "performance-metrics-viewer")
        second = replace(second, session_id="no-unit")
        labels[second.session_id] = UnitLabels("other-session", "tool-based-ui", "—")
        model = replace(with_waiting(model, [first, second]), labels=labels)
        for columns in (80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                rows = waiting_rows(lines)
                self.assertIn("metrics-viewer", rows[0])
                self.assertNotIn("performance-metrics-viewer", rows[0])
                self.assertIn("tool-based-ui", rows[1])
                self.assertEqual(rows[1].split()[2], "—")
                self.assertNotIn("other-session", rows[1])
                header, = [line for line in plain(lines).splitlines() if line.lstrip().startswith("# ")]
                start, end = header.index("step"), header.index("min")
                self.assertEqual(rows[0][start:end].strip(), cut_tail("metrics-viewer nextest", end - start - 1))
                self.assertEqual(rows[1][start:end].strip(), "nextest")

    def test_showrunner_prefix_is_removed_from_running_and_queued_units(self) -> None:
        model = self.fixture.cap_model()
        queued = model.reading.view.waiting[0]
        running = model.reading.explanation.running[0].step
        labels = dict(model.labels)
        for step in (queued, running):
            labels[step.session_id] = UnitLabels("merge-gate", "builds", "builds-queue")
        text = plain(queue_view.render_view(replace(model, labels=labels), 140))
        running_text = "\n".join(line for line in text.splitlines() if line.startswith("run"))
        queued_text = "\n".join(waiting_rows(queue_view.render_view(replace(model, labels=labels), 140)))
        for section, offset in ((running_text, 1), (queued_text, 1)):
            row, = [line for line in section.splitlines() if "builds" in line]
            self.assertEqual(row.split()[offset:offset + 2], ["builds", "queue"])
            self.assertNotIn("builds-queue", row)

    def test_queued_showrunner_without_a_unit_uses_its_session_name(self) -> None:
        model = self.fixture.cap_model()
        step = model.reading.view.waiting[0]
        for missing_unit in ("", "—"):
            with self.subTest(unit=missing_unit):
                labels = {**model.labels, step.session_id: UnitLabels("builds", "builds", missing_unit)}
                row = waiting_rows(queue_view.render_view(replace(model, labels=labels), 140))[0]
                self.assertEqual(row.split()[1:3], ["builds", "—"])

    def test_queued_staff_unit_overrides_merge_gate_session_and_folder_names(self) -> None:
        model = self.fixture.cap_model()
        step = replace(model.reading.view.waiting[0], session_name="merge-gate", folder="/tmp/merge-gate", step="merge-gate")
        labels = {**model.labels, step.session_id: UnitLabels("merge-gate", "sdlc", "devtools")}
        model = replace(with_waiting(model, [step]), labels=labels)
        lines = queue_view.render_view(model, 140)
        header, = [line for line in plain(lines).splitlines() if line.lstrip().startswith("# ")]
        row, = waiting_rows(lines)
        self.assertEqual(row[:header.index("step")].split()[1:], ["sdlc", "devtools"])
        self.assertEqual(row[header.index("step"):header.index("min")].strip(), "devtools merge gate")



    def test_host_holds_keep_short_words_and_large_need_columns_aligned(self) -> None:
        model = self.cap_model()
        head = model.reading.explanation.head
        assert isinstance(head, admission_records.HeadExplanation)
        for pressure, passes, word in ((admission_records.NoPressureHold(), False, "low room"),
                (admission_records.MemoryPressure(7.7, condition="full"), True, "pressure")):
            with self.subTest(word=word):
                step = replace(head.waiter, step="nextest", need=21 * admission_records.GIB)
                changed = with_waiting(model, [step, model.reading.view.waiting[1]])
                changed = replace(changed, reading=replace(changed.reading,
                    explanation=replace(changed.reading.explanation,
                        head=replace(head, waiter=step, decision=replace(head.decision,
                            pressure=pressure, free_passes=passes, starts_at=0),
                            cause=pressure if isinstance(pressure, admission_records.MemoryPressure) else
                                admission_records.NeedsMemory(step.need, head.free.available)))))
                lines = queue_view.render_view(changed, 140)
                first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
                self.assertTrue(plain([first]).endswith(word))
                self.assertTrue(any(word in span.text and span.tone == "error" for span in first))
                narrow = queue_view.render_view(changed, 60)
                rows = waiting_rows(narrow)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in narrow))
                header, = [line for line in plain(narrow).splitlines() if line.lstrip().startswith("# ")]
                start, end = header.index("step"), header.index("min")
                self.assertEqual([row[start:end].strip() for row in rows], ["cables nexte…", "cables nexte…"])


if __name__ == "__main__":
    _ = unittest.main()
