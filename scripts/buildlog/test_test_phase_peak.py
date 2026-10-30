#!/usr/bin/env python3
"""The scoped test window survives admission release and recorder recovery."""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

import launches
import record
import store
import test_record as record_tests
import test_record_rerun as recovery_tests

if TYPE_CHECKING:
    from ..lint import test_invoke_scope as scope_tests
else:
    from scripts.lint import test_invoke_scope as scope_tests


@dataclass(frozen=True)
class SampleTimeline:
    files: scope_tests.SamplerFiles
    proc: Path
    ticks: Path


@dataclass(frozen=True)
class SampledTestPhase:
    source: Path
    timeline: SampleTimeline
    reservation: Path


class PhaseRecordingStop(Enum):
    AFTER_PENDING_SAVE = "after pending save"
    AFTER_ROW_PUBLICATION = "after row publication"
    BEFORE_SIDECAR_DELETE = "before sidecar deletion"


def sample_timeline(root: Path, repository: Path, *, marked: bool = True) -> SampleTimeline:
    files = scope_tests.sampler_files(root)
    proc = root / "proc"
    ticks = root / "ticks"
    ticks.mkdir()
    for pid, executable in ((101, repository / "tools" / "rustc"),
                            (201, repository / "target" / "debug" / "deps" / "one-ab12"),
                            (202, repository / "target" / "debug" / "deps" / "two-cd34"),
                            (999, repository / "target" / "release" / "deps" / "unrelated-ef56")):
        process = proc / str(pid)
        process.mkdir(parents=True)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        (process / "exe").symlink_to(executable)
    for index, anon in enumerate((9000, 500, 1700, 800)):
        tick = ticks / str(index)
        tick.mkdir()
        _ = (tick / "memory.stat").write_text(f"anon {anon}\nfile 900000\n")
        pids = "101\n" if not marked or index == 0 else ("201\n" if index == 1 else "201\n202\n")
        _ = (tick / "cgroup.procs").write_text(pids)
    return SampleTimeline(files, proc, ticks)


TIMELINE_SHELL = r'''
tick=0
date() { printf '%s\n' "$((1000 + tick))"; }
advance_tick() {
    cp "$ticks/$tick/memory.stat" "$scope/memory.stat"
    cp "$ticks/$tick/cgroup.procs" "$scope/cgroup.procs"
}
sleep() {
    [[ "$1" == 1 ]] || exit 91
    printf '%s:%s\n' "$tick" "$(cat "$maximum")" >> "$ticks/observed"
    tick=$((tick + 1))
    if (( tick == 4 )); then
        if [[ -n "${completion_pipe:-}" ]]; then printf 'done\n' > "$completion_pipe"; fi
        exit 0
    fi
    advance_tick
}
'''


class TestPhasePeakTests(unittest.TestCase):
    recovery: recovery_tests.RecordRerunTests  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.recovery = recovery_tests.RecordRerunTests()
        self.addCleanup(self.recovery.doCleanups)
        self.recovery.setUp()
        launcher = self.recovery.fixture.base / ".claude" / "scripts" / "lib" / "py"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(sys.executable)

    def timeline(self, *, marked: bool = True, name: str = "admission") -> SampleTimeline:
        root = self.recovery.fixture.root / name
        root.mkdir(parents=True)
        return sample_timeline(root, self.recovery.fixture.repo, marked=marked)

    def reservation(self, timeline: SampleTimeline, *, filtered: bool = True, width: int = 8) -> Path:
        path = timeline.files.maximum.with_suffix(".reservation")
        fields: dict[str, object] = {
            "pid": 101, "start_time": "original-process", "need": 987654321,
            "repo": "hana", "step": "nextest", "worktree": str(self.recovery.fixture.repo),
            "sidecar": str(timeline.files.scope_path), "test_threads": width,
            "repo_identity": "original-repository", "test_scope": "filtered" if filtered else "full",
            "package": "hana" if filtered else "", "filters": ["one", "two"] if filtered else [],
            "call_id": "original-phase-call",
        }
        _ = path.write_text(json.dumps(fields))
        return path

    def invoke_environment(self, timeline: SampleTimeline, *, caller: str = "verify") -> dict[str, str]:
        return self.recovery.fixture.environment(
            HOME=str(self.recovery.fixture.base), BUILDLOG_PROC=str(timeline.proc),
            BUILDLOG_SYNC="1", BUILDLOG_OFF="0", LINT_INVOKE_SOURCED="",
            TEST_PHASE_RECORDER=str(Path(record.__file__).resolve()),
            CODEX_THREAD_ID="original-step-worker", BUILDLOG_CALLER=caller,
            BUILDLOG_CALL_ID="original-phase-call", USER_ZONE="UTC", TZ="UTC",
        )

    def sample(self, *, marked: bool = True, filtered: bool = True) -> SampledTestPhase:
        return self.sample_scope(self.timeline(marked=marked), filtered=filtered)

    def sample_scope(self, timeline: SampleTimeline, *, filtered: bool = True,
                     argv: tuple[str, ...] = ("cargo", "nextest", "run", "--test-threads", "8")) -> SampledTestPhase:
        reservation = self.reservation(timeline, filtered=filtered)
        source = timeline.files.scope.parent / "test-phase.json"
        shell = TIMELINE_SHELL + r'''
BUILDLOG_RECORD=$TEST_PHASE_RECORDER
scope_path=$1 scope=$2 maximum=$3 ticks=$4 phase_file=$5 reservation=$6
shift 6
advance_tick
buildlog_sample_anon "$scope_path" "$maximum" "$BASHPID" "$phase_file" "$reservation" "$@"
'''
        arguments = [str(timeline.files.scope_path), str(timeline.files.scope),
                     str(timeline.files.maximum), str(timeline.ticks), str(source), str(reservation),
                     *argv]
        result = scope_tests.BuildAdmissionTests().run_invoke(
            self.recovery.fixture.base, shell, arguments,
            environment=self.invoke_environment(timeline), cwd=self.recovery.fixture.repo,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((timeline.ticks / "observed").read_text().splitlines(),
                         ["0:9000", "1:9000", "2:9000", "3:9000"])
        self.assertEqual(timeline.files.maximum.with_suffix(".wave").read_text(), "9000 1000\n")
        self.assertEqual(list(timeline.files.maximum.parent.glob("*.tmp.*")), [])
        return SampledTestPhase(source, timeline, reservation)

    def assert_phase_context(self, fields: dict[str, object], *, filtered: bool = True) -> None:
        self.assertEqual(fields["repo_identity"], "original-repository")
        self.assert_phase_selection(fields, filtered=filtered)

    def fixture_repository_identity(self) -> str:
        root_commit = self.recovery.fixture.git("rev-list", "--max-parents=0", "HEAD", cwd=self.recovery.fixture.repo)
        return "git-root:" + root_commit

    def assert_recorded_phase_context(self, fields: dict[str, object], *, filtered: bool = True) -> None:
        self.assertEqual(fields["repo_identity"], self.fixture_repository_identity())
        self.assert_phase_selection(fields, filtered=filtered)

    def assert_phase_selection(self, fields: dict[str, object], *, filtered: bool) -> None:
        self.assertEqual(fields["test_threads"], 8)
        self.assertEqual(fields["test_scope"], "filtered" if filtered else "full")
        self.assertEqual(fields["package"], "hana" if filtered else "")
        self.assertEqual(fields["filters"], ["one", "two"] if filtered else [])
        self.assertEqual(fields["call_id"], "original-phase-call")

    def remove_admission_sources(self, sampled: SampledTestPhase) -> None:
        for path in (sampled.reservation, sampled.timeline.files.maximum,
                     sampled.timeline.files.maximum.with_suffix(".wave"), sampled.timeline.files.scope_path):
            path.unlink()

    def test_first_scoped_test_opens_the_window_and_parallel_tests_share_its_peak(self) -> None:
        sampled = self.sample()
        fields = cast(dict[str, object], json.loads(sampled.source.read_text()))
        self.assertEqual(fields["test_phase_started_at"], 1001)
        self.assertEqual(fields["test_phase_peak_bytes"], 1700)
        self.assert_phase_context(fields)
        self.assertEqual(sampled.timeline.files.maximum.read_text(), "9000\n")

    def test_build_script_beneath_a_deps_ancestor_does_not_open_the_test_window(self) -> None:
        repository = self.recovery.fixture.repo
        build_script = repository.parent / "work" / "deps" / "hana" / "target" / "debug" / "build" / "pkg-a" / "build-script-build"
        build_script.parent.mkdir(parents=True)
        build_script.touch()
        for profile in (Path("debug"), Path("x86_64-unknown-linux-gnu") / "release"):
            with self.subTest(profile=profile):
                timeline = self.timeline(name="layout-" + "-".join(profile.parts))
                executables = ((101, build_script),
                               (201, repository / "target" / profile / "deps" / "one-ab12"),
                               (202, repository / "target" / profile / "deps" / "two-cd34"))
                for pid, executable in executables:
                    executable.parent.mkdir(parents=True, exist_ok=True)
                    executable.touch()
                    link = timeline.proc / str(pid) / "exe"
                    link.unlink()
                    link.symlink_to(executable)
                sampled = self.sample_scope(timeline)
                fields = cast(dict[str, object], json.loads(sampled.source.read_text()))
                self.assertEqual(fields["test_phase_started_at"], 1001)
                self.assertEqual(fields["test_phase_peak_bytes"], 1700)
                self.assertEqual(timeline.files.maximum.read_text(), "9000\n")

    def assert_test_directory_opens_the_window(self, directory: Path) -> None:
        timeline = self.timeline()
        for pid, binary in ((201, "hana-ab12"), (202, "hana-cd34")):
            link = timeline.proc / str(pid) / "exe"
            link.unlink()
            link.symlink_to(directory / binary)
        sampled = self.sample_scope(timeline)
        fields = cast(dict[str, object], json.loads(sampled.source.read_text()))
        self.assertEqual(fields["test_phase_started_at"], 1001)
        self.assertEqual(fields["test_phase_peak_bytes"], 1700)
        self.assertEqual(timeline.files.maximum.read_text(), "9000\n")

    def test_native_test_in_a_custom_target_directory_opens_the_window(self) -> None:
        self.assert_test_directory_opens_the_window(Path("/cache/cargo-hana/debug/deps"))

    def test_cross_target_test_in_a_custom_target_directory_opens_the_window(self) -> None:
        self.assert_test_directory_opens_the_window(
            Path("/cache/cargo-hana/x86_64-unknown-linux-gnu/debug/deps"))

    def test_nextest_environment_width_is_recorded_and_an_explicit_flag_takes_priority(self) -> None:
        for flag_width in (False, True):
            with self.subTest(flag_width=flag_width), mock.patch.dict(os.environ, {"NEXTEST_TEST_THREADS": "5"}):
                timeline = self.timeline(name="width-flag" if flag_width else "width-environment")
                argv = ("cargo", "nextest", "run", "--workspace")
                if flag_width:
                    argv += ("--test-threads", "8")
                sampled = self.sample_scope(timeline, argv=argv)
                self.remove_admission_sources(sampled)
                with mock.patch.dict(os.environ, {"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)}):
                    self.assertEqual(self.recovery.run_step(status=0, with_sources=False), 0)
                row = self.recovery.step_records()[-1]
                self.assertEqual(row["test_threads"], 8 if flag_width else 5)
                self.assertEqual(row["test_phase_peak_bytes"], 1700)
                self.assertFalse(sampled.source.exists())

    def test_invalid_step_arguments_delete_the_phase_sidecar_with_the_other_sources(self) -> None:
        valid = ["1", str(record_tests.START), str(record_tests.START + 12.5)]
        for position, name in enumerate(("status", "start", "end")):
            with self.subTest(invalid=name):
                sampled = self.sample_scope(self.timeline(name="invalid-" + name))
                self.remove_admission_sources(sampled)
                _ = self.recovery.held.write_bytes(self.recovery.output)
                _ = self.recovery.peak.write_bytes(self.recovery.peak_contents)
                arguments = list(valid)
                arguments[position] = "invalid-" + name
                result = self.recovery.fixture.record(
                    "step", *arguments, "0", str(self.recovery.held), str(self.recovery.peak),
                    *record_tests.NEXTEST, extra={"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(sampled.source.exists())
                self.assertFalse(self.recovery.held.exists())
                self.assertFalse(self.recovery.peak.exists())
                self.assertEqual(self.recovery.step_records(), [])
                self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        self.assertTrue((self.recovery.fixture.root / store.ERRORS_NAME).exists())

    def test_reading_phase_context_repeatedly_leaves_the_import_search_path_unchanged(self) -> None:
        timeline = self.timeline()
        reservation = self.reservation(timeline)
        original_path = sys.path.copy()
        self.addCleanup(setattr, sys, "path", original_path)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                context = record.test_phase_context(reservation, ["cargo", "nextest", "run", "--test-threads", "8"])
                self.assertEqual(sys.path, original_path)
                self.assert_phase_context(context)

    def test_no_test_executable_in_the_scope_keeps_the_phase_unknown(self) -> None:
        sampled = self.sample(marked=False)
        self.remove_admission_sources(sampled)
        with mock.patch.dict(os.environ, {"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)}):
            self.assertEqual(self.recovery.run_step(), 0)
        row = self.recovery.step_records()[0]
        self.assertIsNone(row["test_phase_peak_bytes"])
        self.assertIsNone(row["test_phase_started_at"])
        self.assert_recorded_phase_context(row)
        self.assertFalse(sampled.source.exists())

    def test_absent_admission_entry_charge_is_unknown_rather_than_the_need_or_peak(self) -> None:
        sampled = self.sample()
        self.remove_admission_sources(sampled)
        with mock.patch.dict(os.environ, {"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)}):
            self.assertEqual(self.recovery.run_step(), 0)
        row = self.recovery.step_records()[0]
        self.assertIsNone(row["test_phase_charge"])
        self.assertEqual(row["test_phase_peak_bytes"], 1700)
        self.assert_recorded_phase_context(row)

    def assert_phase_recovers(self, stop: PhaseRecordingStop) -> None:
        sampled = self.sample()
        original_contents = sampled.source.read_bytes()
        original_fields = cast(dict[str, object], json.loads(original_contents))
        self.remove_admission_sources(sampled)
        publish, unlink = record.write_step_line, os.unlink

        def stop_after_publication(pending: record.PendingStep) -> bool:
            _ = publish(pending)
            raise recovery_tests.RecorderStopped("after publication with the original phase")

        def stop_before_delete(path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                               *, dir_fd: int | None = None) -> None:
            if Path(os.fsdecode(path)) == sampled.source:
                raise recovery_tests.RecorderStopped("before phase sidecar deletion")
            unlink(path, dir_fd=dir_fd)

        match stop:
            case PhaseRecordingStop.AFTER_PENDING_SAVE:
                boundary = mock.patch.object(record, "keep_log", side_effect=recovery_tests.RecorderStopped(stop.value))
            case PhaseRecordingStop.AFTER_ROW_PUBLICATION:
                boundary = mock.patch.object(record, "write_step_line", side_effect=stop_after_publication)
            case PhaseRecordingStop.BEFORE_SIDECAR_DELETE:
                boundary = mock.patch.object(os, "unlink", side_effect=stop_before_delete)
        with mock.patch.dict(os.environ, {"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)}), boundary:
            with self.assertRaises(recovery_tests.RecorderStopped):
                _ = self.recovery.run_step()
        pending_paths = list(record.pending_steps_root().glob("*.json"))
        self.assertEqual(len(pending_paths), 1)
        pending = cast(dict[str, object], json.loads(pending_paths[0].read_text()))
        saved_row = cast(dict[str, object], pending["row"])
        self.assertEqual(pending["test_phase_file"], str(sampled.source))
        self.assertEqual(sampled.source.read_bytes(), original_contents)
        expected_fields = {**original_fields, "repo_identity": self.fixture_repository_identity()}
        for field, value in expected_fields.items():
            self.assertEqual(saved_row[field], value, field)
        self.assertEqual(len(self.recovery.step_records()), 0 if stop is PhaseRecordingStop.AFTER_PENDING_SAVE else 1)
        self.assertEqual(self.recovery.held.exists(), stop is not PhaseRecordingStop.BEFORE_SIDECAR_DELETE)
        self.assertEqual(self.recovery.peak.exists(), stop is not PhaseRecordingStop.BEFORE_SIDECAR_DELETE)
        _ = self.recovery.fresh_cli("launches")
        rows = self.recovery.step_records()
        self.assertEqual(rows, [saved_row])
        self.assert_recorded_phase_context(rows[0])
        self.assertEqual(rows[0]["test_phase_peak_bytes"], 1700)
        self.assertFalse(sampled.source.exists())
        self.assertFalse(self.recovery.held.exists())
        self.assertFalse(self.recovery.peak.exists())
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        self.assertEqual(len(self.recovery.saved_logs()), 1)
        _ = self.recovery.fresh_cli("launches")
        with mock.patch.dict(os.environ, {"BUILDLOG_TEST_PHASE_FILE": str(sampled.source)}):
            self.assertEqual(self.recovery.run_step(), 0)
        self.assertEqual(self.recovery.step_records(), rows)
        self.assertEqual(launches.collect().steps_added, 0)

    def test_stop_after_pending_save_keeps_the_original_phase_before_log_save(self) -> None:
        self.assert_phase_recovers(PhaseRecordingStop.AFTER_PENDING_SAVE)

    def test_stop_after_row_publication_recovers_the_original_phase_from_pending(self) -> None:
        self.assert_phase_recovers(PhaseRecordingStop.AFTER_ROW_PUBLICATION)

    def test_stop_before_sidecar_delete_recovers_with_admission_files_already_gone(self) -> None:
        self.assert_phase_recovers(PhaseRecordingStop.BEFORE_SIDECAR_DELETE)

    def assert_scoped_record(self, *, caller: str, filtered: bool, terminal: bool) -> None:
        name = f"{caller}-{'filtered' if filtered else 'full'}-{'terminal' if terminal else 'tee'}"
        timeline = self.timeline(name=name)
        reservation = self.reservation(timeline, filtered=filtered, width=32)
        peak = timeline.files.scope.parent / "record.peak"
        completion = timeline.files.scope.parent / "sample-completed"
        os.mkfifo(completion)
        arguments = ["cargo", "nextest", "run", "--test-threads", "32"]
        arguments += (["-E", "package(hana) & (test(one) | test(two))"] if filtered else ["--workspace"])
        shell = TIMELINE_SHELL + r'''
BUILDLOG_RECORD=$TEST_PHASE_RECORDER
scope_path=$1 scope=$2 maximum=$3 ticks=$4 reservation=$5 peak=$6 completion_pipe=$7
shift 7
exec 7<>"$completion_pipe"
LINT_NEXTEST_SHARE="${reservation%/*}/share"
build_hold_wait_local() { :; }
build_hold_check_local() { return 0; }
buildlog_now() { BUILDLOG_NOW=1790942400; }
buildlog_begin() {
    BUILDLOG_START=1790942400 BUILDLOG_PEAK=$peak BUILDLOG_TREE="" BUILDLOG_MEM_WAIT_S=0
    printf '123456\n' > "$peak"
    printf '%s\n' "$scope" > "$peak.cgroup"
}
buildlog_wait_for_memory() {
    BUILDLOG_MEM_RESERVATION=$reservation BUILDLOG_MEM_WAIT_S=17 BUILDLOG_MEM_OUTCOME=Granted
}
buildlog_memory_python() {
    if [[ "${2:-}" == claim ]]; then
        printf '8\n'
    else
        if [[ "${2:-}" == test-phase-context ]]; then printf 'context\n' >> "$ticks/context-calls"; fi
        "$HOME/.claude/scripts/lib/py" "$@"
    fi
}
buildlog_exec() {
    printf '%s\n' "$@" > "$ticks/executed-argv"
    local completed
    read -r -t 10 completed <&7 || return 92
    [[ "$completed" == done ]] || return 93
    cp "$reservation" "$ticks/released-reservation.json"
    printf 'fake nextest completed\n'
}
sweep_after_step() { :; }
advance_tick
run_once "$@"
test ! -e "$reservation"
test ! -e "$maximum"
test ! -e "${maximum%.max}.wave"
'''
        before = [row for row in self.recovery.fixture.records() if row.get("kind") == "step"]
        result = scope_tests.BuildAdmissionTests().run_invoke(
            self.recovery.fixture.base, shell,
            [str(timeline.files.scope_path), str(timeline.files.scope), str(timeline.files.maximum),
             str(timeline.ticks), str(reservation), str(peak), str(completion), *arguments],
            environment=self.invoke_environment(timeline, caller=caller),
            cwd=self.recovery.fixture.repo, terminal=terminal,
        )
        errors = self.recovery.fixture.root / store.ERRORS_NAME
        detail = result.stdout + result.stderr + (errors.read_text() if errors.exists() else "")
        self.assertEqual(result.returncode, 0, detail)
        rows = [row for row in self.recovery.fixture.records() if row.get("kind") == "step"]
        self.assertEqual(len(rows), len(before) + 1, detail)
        row = next(item for item in rows if item["id"] not in {old["id"] for old in before})
        self.assertEqual((row["tty"], row["status"], row["caller"]), (terminal, 0, caller))
        self.assertEqual(row["test_phase_peak_bytes"], 1700)
        self.assertEqual(row["test_phase_started_at"], 1001)
        self.assert_recorded_phase_context(row, filtered=filtered)
        self.assertEqual(row["cwd"], str(self.recovery.fixture.repo))
        self.assertIsNone(row["test_phase_charge"])
        expected_argv = [*arguments]
        expected_argv[4] = "8"
        self.assertEqual(row["argv"], expected_argv)
        self.assertEqual((timeline.ticks / "executed-argv").read_text().splitlines(), expected_argv)
        released = cast(dict[str, object], json.loads((timeline.ticks / "released-reservation.json").read_text()))
        self.assertEqual(released["test_threads"], 8)
        self.assertEqual((timeline.ticks / "context-calls").read_text().splitlines(), ["context"])
        self.assertEqual((timeline.ticks / "observed").read_text().splitlines(),
                         ["0:9000", "1:9000", "2:9000", "3:9000"])
        self.assertFalse(reservation.exists())
        self.assertFalse(timeline.files.maximum.exists())
        self.assertFalse(timeline.files.maximum.with_suffix(".wave").exists())
        self.assertFalse(peak.exists())
        self.assertFalse(Path(str(peak) + ".test-phase.json").exists())

    def test_terminal_and_tee_steps_keep_the_actual_shared_width_after_admission_release(self) -> None:
        for caller in ("verify", "cargo-port", "validate_ci"):
            for filtered in (False, True):
                for terminal in (False, True):
                    with self.subTest(caller=caller, filtered=filtered, terminal=terminal):
                        self.assert_scoped_record(caller=caller, filtered=filtered, terminal=terminal)

    def test_step_without_a_test_phase_mark_records_unknown_instead_of_zero(self) -> None:
        self.assertEqual(self.recovery.run_step(), 0)
        rows = self.recovery.step_records()
        self.assertEqual(len(rows), 1)
        for field in ("test_phase_peak_bytes", "test_phase_started_at"):
            with self.subTest(field=field):
                self.assertIn(field, rows[0])
                self.assertIsNone(rows[0][field])
        self.assertEqual(rows[0]["peak_mem_bytes"], 123456)
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
