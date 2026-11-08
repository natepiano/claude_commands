"""Pin the step row and step context that record.py writes."""

from __future__ import annotations

import contextlib
from functools import cached_property, partial
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import cast, override

from scripts.buildlog import record
from scripts.buildlog import repository
from scripts.buildlog import step_context
from scripts.buildlog import treekey
from scripts.buildlog import store
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.buildlog.test_index import use_test_log
from scripts.buildlog.test_record import START, NEXTEST

use_test_log()

PHASE_CHARGE: record.TestPhaseChargeUse = {
    "bytes": 8192, "selected_at": START + 1, "reservation_id": "own-reservation",
    "need": {"kind": "test_phase_need", "bytes": 8192, "runs": 10,
             "width": 4, "recorded_width": 2, "form": "scaled_up", "margin": 1.2},
}
PHASE_GRANT = json.dumps({
    "reservation_id": "own-reservation", "repo_identity": "original-repository",
    "test_scope": "full", "package": "", "filters": [],
    "call_id": {"kind": "CallRecord", "call_id": "phase-call"},
    "test_phase_charge_use": {"kind": "NoTestPhaseChargeUse"},
})

from scripts.lib.cargo_output import CargoOutputRoots
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots


class RecorderCompatibilityTests(unittest.TestCase):
    def test_output_fixture_accepts_command_inputs_during_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary).resolve()
            target = checkout / "selected-output"
            build = checkout / "intermediate-output"
            roots = CargoOutputRoots(checkout, target, build)
            environment = {"BUILDLOG_DIR": str(checkout / "rows"), "BUILDLOG_TARGET_FOLDER": str(target)}
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(record, "output", return_value=None),
                mock.patch.object(repository, "output", return_value=None),
                mock.patch.object(treekey, "tree_key", return_value=None),
                fake_cargo_output_roots(roots),
                contextlib.chdir(checkout),
            ):
                record.step(["0", str(int(START)), str(int(START) + 2), "1", "", "",
                             "cargo", "check", "--target-dir", str(target)])
                rows = [cast(dict[str, object], json.loads(line))
                        for path in store.root().glob("*/*.jsonl") for line in path.read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["target_dir"], rows[0]["target_directory"], rows[0]["build_directory"]),
                                 (str(target), str(target), str(build)))

    def test_scp_remote_identity_does_not_load_url_parsing(self) -> None:
        source = (
            "import sys; from scripts.buildlog import repository; "
            "assert repository.remote_identity('git@EXAMPLE.com:owner/project.git', '/fixture') "
            "== 'https://example.com/owner/project'; assert 'urllib.parse' not in sys.modules; "
            "assert 'scripts.buildlog.record' not in sys.modules"
        )
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", source],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plain_phase_context_does_not_load_admission(self) -> None:
        source = (
            "import sys; from scripts.buildlog import record; "
            "value = record.test_phase_context(sys.argv[1], ['cargo', 'nextest', 'run', '-j4']); "
            "assert value['test_threads'] == 4; assert value['call_id'] == 'phase-call'; "
            "assert 'memory_admit' not in sys.modules; assert 'scripts.lint.memory_admit' not in sys.modules"
        )
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", source, PHASE_GRANT],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_row_ids_do_not_load_random_or_temporary_file_modules(self) -> None:
        source = (
            "import sys; from scripts.buildlog import record; "
            "value = record.new_id('fixture', 0); assert value.startswith('fixture-19700101T000000-'); "
            "assert len(value.rsplit('-', 1)[1]) == 6; "
            "assert 'secrets' not in sys.modules; assert 'tempfile' not in sys.modules"
        )
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", source],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_identity_reads_sync_configuration_once(self) -> None:
        with mock.patch.object(record, "sync_config", return_value={
            "sync.hosts.first": "one,shared", "sync.hosts.second": "shared,two",
        }) as read:
            self.assertEqual(record.local_step_identity(("first", "second")),
                             record.LocalStepIdentity(("first", "second"), ("one", "shared", "two")))
            read.assert_called_once_with()

    def test_empty_metadata_does_not_load_json_or_store(self) -> None:
        source = (
            "import sys; from scripts.buildlog import step_context; "
            "assert 'json' not in sys.modules; assert 'scripts.buildlog.store' not in sys.modules; "
            "step_context.metadata_target_folder('  ', [], '/fixture'); "
            "assert 'json' not in sys.modules; assert 'scripts.buildlog.store' not in sys.modules"
        )
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", source],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_target_configuration_records_the_error(self) -> None:
        from scripts.lib import cargo_target_config

        with (
            mock.patch.object(cargo_target_config, "cargo_folder_settings", side_effect=ValueError("invalid")),
            mock.patch.object(store, "note_error") as error,
        ):
            folder = step_context.resolve_target_folder(NEXTEST, "/fixture", {})
        self.assertIsInstance(folder, step_context.UnresolvedTargetFolder)
        error.assert_called_once_with("target folder: invalid")

    def test_package_scope_memory_uses_the_package_pressure_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            peak = Path(temporary) / "peak"
            _ = peak.write_text("123456\nsome avg10=0.00 total=1250000\n")
            source = (
                "import sys; from scripts.buildlog import record; "
                "memory = record.peak_and_stall(sys.argv[1]); "
                "assert memory.peak_bytes == 123456; assert memory.stall_some_s == 1.25; "
                "assert memory.stall_full_s is record.cgroup_capture.Unmeasured.VALUE"
            )
            result = subprocess.run(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", source, str(peak)],
                cwd=temporary, capture_output=True, text=True,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repository_keys_preserve_remote_and_root_identity(self) -> None:
        cases = [
            ("https://example.com/owner/project.git", "", "https://example.com/owner/project"),
            ("ssh://git@example.com/owner/project.git", "", "https://example.com/owner/project"),
            ("git@example.com:owner/project.git", "", "https://example.com/owner/project"),
            ("git/owner@EXAMPLE.com:repo", "", "https://git/owner@EXAMPLE.com/repo"),
            ("git@example.com:owner/project.git?query#fragment", "", "https://example.com/owner/project"),
            ("git@example.com:", "", "https://example.com"),
            ("git@example.com:/", "", "https://example.com"),
            ("https://user:password@EXAMPLE.com/owner/project.git", "", "https://example.com/owner/project"),
            ("ssh://git@example.com:2222/owner/project.git", "", "https://example.com:2222/owner/project"),
            ("https://example.com:443/owner/project/", "", "https://example.com/owner/project"),
            ("file:///fixture/project.git", "", "/fixture/project"),
            ("../project.git", "", "/fixture/project"),
            ("/fixture/project.git", "", "/fixture/project"),
            ("", "abc\ndef\n", "git-root:abc"),
            ("", "", store.UNKNOWN_IDENTITY),
        ]
        for remote, roots, expected in cases:
            with self.subTest(remote=remote, roots=roots), mock.patch.object(
                repository, "output", side_effect=[remote, roots] if not remote else [remote],
            ):
                self.assertEqual(repository.repository_key("/fixture/project"), expected)

    def test_target_lookup_reads_configuration_once(self) -> None:
        with mock.patch.object(record, "sync_config", return_value={
            "sync.hosts.first": "first-host", "sync.hosts.second": "second-host",
        }) as read:
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(record.recorded_target("second-host"), "second")
            read.assert_called_once_with()

    def capture(self, terminal: bool, *, admission_history: str | None = None,
                recover: bool = False) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            phase_file = Path(temporary) / "phase.json"
            history = Path(temporary) / "admission/anon_peaks.jsonl"
            held = Path(temporary) / "step.log"
            _ = held.write_text("Finished test profile [unoptimized + debuginfo] target(s) in 1.00s\n")
            environment = {
                "BUILDLOG_DIR": temporary, "BUILDLOG_CALLER": "verify", "BUILDLOG_TARGET": "natedev",
                "BUILDLOG_TREE_START": "tree", "BUILDLOG_TARGET_FOLDER": "/fixture/target",
            }

            def command(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[0] == "rustc":
                    text = "rustc fixture"
                elif args[-3:] == ["remote", "get-url", "origin"]:
                    text = "git@example.com:owner/project.git"
                elif "--short" in args:
                    text = "1234567"
                else:
                    text = "/fixture/project/.git\n/fixture/project\nmain"
                return subprocess.CompletedProcess(args, 0, text + "\n", "")

            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(os, "getcwd", return_value="/fixture/project"),
                mock.patch.object(store, "host_name", return_value="fixture"),
                mock.patch.object(subprocess, "run", side_effect=command),
                mock.patch.object(record, "new_id", return_value="fixture-id"),
                mock.patch("scripts.lib.cargo_output.read_cargo_output_roots", return_value=CargoOutputRoots(
                    Path("/fixture/project"), Path("/fixture/target"), Path("/fixture/build"))),
                mock.patch.object(treekey, "tree_key", return_value="tree"),
            ):
                if admission_history is not None:
                    context = record.test_phase_context(PHASE_GRANT, NEXTEST)
                    _ = phase_file.write_text(json.dumps({
                        **context, "test_phase_peak_bytes": 4096, "test_phase_started_at": START,
                    }))
                    os.environ["BUILDLOG_TEST_PHASE_FILE"] = str(phase_file)
                    history.parent.mkdir()
                    _ = history.write_text(admission_history)
                publication = (mock.patch.object(record, "write_step_line", side_effect=OSError("publication stopped"))
                               if recover else contextlib.nullcontext())
                with publication:
                    record.step(["0", str(int(START)), str(int(START) + 2), "1" if terminal else "0",
                                 "" if terminal else str(held), "", *NEXTEST])
                if recover:
                    history.unlink()
                    phase_file.unlink()
                    pending = list(record.pending_steps_root().glob("*.json"))
                    self.assertEqual(len(pending), 1)
                    self.assertIsInstance(record.finish_pending_step(pending[0]), record.PublishedStep)
            paths = list(Path(temporary).glob("fixture/*.jsonl"))
            self.assertEqual(len(paths), 1)
            return cast(dict[str, object], json.loads(paths[0].read_text()))

    def test_terminal_row_matches_baseline(self) -> None:
        self.assertEqual(self.capture(True), TERMINAL_BASELINE)

    def test_tee_row_matches_baseline(self) -> None:
        self.assertEqual(self.capture(False), TEE_BASELINE)

    def assert_completed_charge(self, terminal: bool, *, recover: bool = False) -> None:
        history = "\n".join(json.dumps(row) for row in (
            {"reservation_id": "older", "test_phase_charge_use": {**PHASE_CHARGE, "bytes": 99999}},
            {"reservation_id": "own-reservation", "test_phase_charge_use": PHASE_CHARGE},
            {"reservation_id": "newer", "test_phase_charge_use": {**PHASE_CHARGE, "bytes": 1}},
        )) + "\n"
        # An incomplete append must not replace the completed evidence.
        history += json.dumps({"reservation_id": "own-reservation", "test_phase_charge_use": None})
        row = self.capture(terminal, admission_history=history, recover=recover)
        self.assertEqual(row, {
            **(TERMINAL_BASELINE if terminal else TEE_BASELINE),
            "test_scope": "full", "package": "", "filters": [],
            "call_id": "phase-call", "test_threads": None, "test_phase_charge": PHASE_CHARGE,
            "test_phase_peak_bytes": 4096, "test_phase_started_at": START,
            "reservation_id": "own-reservation",
        })

    def test_terminal_row_copies_its_completed_admission_charge(self) -> None:
        self.assert_completed_charge(True)

    def test_tee_row_copies_its_completed_admission_charge(self) -> None:
        self.assert_completed_charge(False)

    def test_steps_without_charge_evidence_record_null(self) -> None:
        histories = ("", json.dumps({"reservation_id": "own-reservation"}) + "\n",
                     json.dumps({"reservation_id": "other", "test_phase_charge_use": PHASE_CHARGE}) + "\n")
        for terminal in (True, False):
            for history in histories:
                with self.subTest(terminal=terminal, history=history):
                    row = self.capture(terminal, admission_history=history)
                    self.assertIsNone(row["test_phase_charge"])

    def test_saved_charge_survives_pending_record_recovery_without_history(self) -> None:
        for terminal in (True, False):
            with self.subTest(terminal=terminal):
                self.assert_completed_charge(terminal, recover=True)

    def test_admission_observation_does_not_read_completed_charge_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "phase.json"
            _ = path.write_text(json.dumps({
                "reservation_id": "own-reservation", "test_phase_peak_bytes": 4096,
                "test_phase_started_at": START, "test_phase_charge": None,
            }))
            with mock.patch.object(record, "completed_test_phase_charge", side_effect=AssertionError("history read")):
                phase = record.read_test_phase(path)
            self.assertIsInstance(phase, record.TestPhaseRecord)
            if isinstance(phase, record.TestPhaseRecord):
                self.assertEqual(phase.window, record.ObservedTestPhase(4096, START))

    def test_invalid_charge_publishes_each_step_with_one_error_line(self) -> None:
        history = json.dumps({"reservation_id": "own-reservation",
                              "test_phase_charge_use": {**PHASE_CHARGE, "bytes": "broken"}}) + "\n"
        publish = record.write_step_line

        def check_and_publish(pending: record.PendingStep) -> bool:
            errors = (store.root() / "errors.log").read_text().splitlines()
            self.assertEqual(len(errors), 1)
            self.assertIn("test-phase charge 'own-reservation'", errors[0])
            self.assertIsNone(pending.row["test_phase_charge"])
            return publish(pending)

        for terminal in (True, False):
            with self.subTest(terminal=terminal), mock.patch.object(record, "write_step_line", side_effect=check_and_publish):
                row = self.capture(terminal, admission_history=history)
                self.assertIsNone(row["test_phase_charge"])
                self.assertEqual(row["test_phase_peak_bytes"], 4096)

    def test_invalid_charge_publishes_recovered_step_with_one_error_line(self) -> None:
        for terminal in (True, False):
            with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"BUILDLOG_DIR": temporary}):
                phase_file = Path(temporary) / "phase.json"
                _ = phase_file.write_text(json.dumps({
                    **record.test_phase_context(PHASE_GRANT, NEXTEST),
                    "test_phase_peak_bytes": 4096, "test_phase_started_at": START,
                }))
                history = Path(temporary) / "admission/anon_peaks.jsonl"
                history.parent.mkdir()
                _ = history.write_text(json.dumps({"reservation_id": "own-reservation",
                    "test_phase_charge_use": {**PHASE_CHARGE, "bytes": "broken"}}) + "\n")
                pending = record.pending_steps_root() / "fixture.json"
                pending.parent.mkdir()
                _ = pending.write_text(json.dumps({
                    "row_key": "fixture", "row": TERMINAL_BASELINE if terminal else TEE_BASELINE, "held_log": None, "peak_file": None,
                    "test_phase_file": str(phase_file), "test_phase_read_error": "previous sidecar error",
                    "month_row_end": 0,
                }))
                self.assertIsInstance(record.finish_pending_step(pending), record.PublishedStep)
                row = cast(dict[str, object], json.loads(store.host_file("fixture", START).read_text()))
                self.assertIsNone(row["test_phase_charge"])
                self.assertEqual(row["test_phase_peak_bytes"], 4096)
                self.assertEqual(len((Path(temporary) / "errors.log").read_text().splitlines()), 1)
                self.assertFalse(phase_file.exists())
                self.assertFalse(pending.exists())

    def test_unobserved_phase_does_not_open_charge_history(self) -> None:
        read_phase = record.read_test_phase

        def unobserved(path: Path | record.NoTestPhaseRecord
                       ) -> record.TestPhaseRecord | record.NoTestPhaseRecord | record.UnreadableTestPhaseRecord:
            if isinstance(path, Path):
                fields = cast(dict[str, object], json.loads(path.read_text()))
                fields.update(test_phase_peak_bytes=None, test_phase_started_at=None)
                _ = path.write_text(json.dumps(fields))
            return read_phase(path)

        with (
            mock.patch.object(record, "read_test_phase", side_effect=unobserved),
            mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened,
        ):
            row = self.capture(True, admission_history="")
        for call in opened.call_args_list:
            if cast(Path, call.args[0]).name == "anon_peaks.jsonl":
                self.assertEqual(call.kwargs.get("mode", call.args[1] if len(call.args) > 1 else "r"), "w")
        self.assertIsNone(row["test_phase_charge"])
        self.assertIsNone(row["test_phase_peak_bytes"])

    def test_unknown_call_tag_keeps_the_phase_sidecar(self) -> None:
        context = cast(dict[str, object], json.loads(PHASE_GRANT))
        context["call_id"] = {"kind": "FutureCallRecord", "call_id": "future"}
        fields = record.test_phase_context(json.dumps(context), NEXTEST)
        self.assertIsNone(fields["call_id"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "phase.json"
            _ = path.write_text(json.dumps(fields))
            self.assertIsInstance(record.read_test_phase(path), record.TestPhaseRecord)
            self.assertTrue(path.exists())

    def test_grant_charge_does_not_affect_the_phase_context(self) -> None:
        context = cast(dict[str, object], json.loads(PHASE_GRANT))
        context["test_phase_charge_use"] = {"bytes": "broken"}
        fields = record.test_phase_context(json.dumps(context), NEXTEST)
        self.assertIsNone(fields["test_phase_charge"])
        self.assertEqual(fields["call_id"], "phase-call")
        self.assertNotIn("reservation_id", record.test_phase_context("{}", NEXTEST))

    def test_only_matched_history_creates_a_charged_phase(self) -> None:
        publish = record.write_step_line
        phases: list[record.TestPhaseRecord | record.NoTestPhaseRecord | record.UnreadableTestPhaseRecord] = []

        def capture_phase(pending: record.PendingStep) -> bool:
            phases.append(pending.test_phase)
            return publish(pending)

        with mock.patch.object(record, "write_step_line", side_effect=capture_phase):
            _ = self.capture(True, admission_history="")
            _ = self.capture(True, admission_history=json.dumps({
                "reservation_id": "own-reservation", "test_phase_charge_use": PHASE_CHARGE,
            }) + "\n")
        self.assertIsInstance(phases[0], record.ReservedTestPhaseRecord)
        self.assertNotIsInstance(phases[0], record.ChargedTestPhaseRecord)
        self.assertIsInstance(phases[1], record.ChargedTestPhaseRecord)
        if isinstance(phases[1], record.ChargedTestPhaseRecord):
            self.assertEqual(phases[1].charge, PHASE_CHARGE)

    def test_context_matches_baseline(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"HOME": "/fixture", "BUILDLOG_KNOWN_TARGET_DIR": "/fixture/target"}, clear=True),
            mock.patch.object(os, "getcwd", return_value="/fixture/project"),
            mock.patch.object(treekey, "tree_key", return_value="tree"),
            contextlib.redirect_stdout(output),
        ):
            step_context.step_context(NEXTEST, "")
        self.assertEqual(output.getvalue(), "tree\n/fixture/target\n")


class StepOutputFolderFixture(unittest.TestCase):
    @cached_property
    def temporary(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    @property
    def workspace(self) -> Path:
        return self.temporary / "workspace"

    @property
    def outside(self) -> Path:
        return self.temporary / "outside"

    @property
    def log_root(self) -> Path:
        return self.temporary / "rows"

    @override
    def setUp(self) -> None:
        source = self.workspace / "member/src"
        source.mkdir(parents=True)
        _ = (self.workspace / "Cargo.toml").write_text('[workspace]\nmembers = ["member"]\nresolver = "2"\n')
        _ = (source.parent / "Cargo.toml").write_text(
            '[package]\nname = "output-fixture"\nversion = "0.1.0"\nedition = "2024"\n')
        _ = (source / "lib.rs").write_text("pub fn fixture() {}\n")
        self.outside.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CARGO_", "BUILDLOG_"))}
        environment.update({"CARGO_HOME": str(self.temporary / "cargo-home"),
                            "BUILDLOG_DIR": str(self.log_root), "BUILDLOG_TARGET": "natedev"})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        _ = cast(object, self.enterContext(mock.patch.object(record, "output", return_value=None)))
        _ = cast(object, self.enterContext(mock.patch.object(repository, "output", return_value=None)))
        _ = cast(object, self.enterContext(mock.patch.object(treekey, "tree_key", return_value=None)))

    def write_step(self, argv: list[str], target: Path, *, cwd: Path) -> dict[str, object]:
        captured = step_context.target_folder_text(step_context.resolve_target_folder(argv, str(cwd), os.environ))
        self.assertEqual(captured, str(target))
        os.environ["BUILDLOG_TARGET_FOLDER"] = captured
        return self.publish_step(argv, cwd=cwd)

    def publish_step(self, argv: list[str], *, cwd: Path) -> dict[str, object]:
        with contextlib.chdir(cwd):
            record.step(["0", str(int(START)), str(int(START) + 2), "1", "", "", *argv])
        rows = [cast(dict[str, object], json.loads(line))
                for path in self.log_root.glob("*/*.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_output_pair(self, row: dict[str, object], target: Path, build: Path) -> None:
        self.assertEqual(row["target_dir"], str(target))
        self.assertEqual(row["target_directory"], row["target_dir"])
        self.assertEqual(row["build_directory"], str(build))
        self.assertFalse((self.log_root / store.ERRORS_NAME).exists())


class StepOutputFolderTests(StepOutputFolderFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        # Phase 19 confirmed these answers with cargo metadata --format-version 1 --no-deps.
        # Each row supplies that command's options, cwd, and CARGO_TARGET_DIR.
        cases: list[tuple[list[str], Path, str | None, Path, Path]] = [
            (["--config", 'build.target-dir="unused"', "--config",
              'build.target-dir="' + str(self.workspace / "relative-output") + '"'],
             self.workspace, None, self.workspace / "relative-output", self.workspace / "relative-output"),
            (["--config", 'build.target-dir="' + str(self.temporary / "absolute-output") + '"'],
             self.workspace, None, self.temporary / "absolute-output", self.temporary / "absolute-output"),
            (['--config=build.target-dir="configured-output"'], self.workspace, None,
             self.workspace / "configured-output", self.workspace / "configured-output"),
            ([], self.workspace, "environment-output",
             self.workspace / "environment-output", self.workspace / "environment-output"),
            (["--config", 'build.target-dir="' + str(self.workspace / "configured-output") + '"'],
             self.workspace, str(self.workspace / "environment-output"),
             self.workspace / "environment-output", self.workspace / "environment-output"),
            (["--manifest-path", "../workspace/member/Cargo.toml"], self.outside, None,
             self.outside / "manifest-output", self.outside / "manifest-output"),
            (["--config", 'build.build-dir="intermediate-output"'], self.workspace, None,
             self.workspace / "target", self.workspace / "intermediate-output"),
            (["--config", str(self.temporary / "settings/.cargo/config.toml")], self.workspace, None,
             self.temporary / "settings/file-output", self.temporary / "settings/file-output"),
            ([], self.workspace, "changed-output",
             self.workspace / "changed-output", self.workspace / "changed-output"),
            ([], self.workspace, None, self.workspace / "target", self.workspace / "target"),
            (["--config", 'build.target-dir="' + str(self.workspace / "first-output") + '"'],
             self.workspace, None, self.workspace / "first-output", self.workspace / "first-output"),
        ]
        table = self.temporary / "cargo-calls.json"
        _ = table.write_text(json.dumps([
            {"call": {"argv": ["metadata", "--format-version", "1", "--no-deps", *options],
                      "cwd": str(cwd), "CARGO_TARGET_DIR": cargo_target},
             "answer": {"workspace_root": str(self.workspace), "target_directory": str(target),
                        "build_directory": str(build)}}
            for options, cwd, cargo_target, target, build in cases
        ]))
        binaries = self.temporary / "bin"
        binaries.mkdir()
        cargo = binaries / "cargo"
        _ = cargo.write_text("\n".join([
            f"#!{sys.executable}",
            "import json, os, sys",
            f"with open({str(table)!r}) as source:",
            "    cases = json.load(source)",
            "call = {'argv': sys.argv[1:], 'cwd': os.getcwd(),",
            "        'CARGO_TARGET_DIR': os.environ.get('CARGO_TARGET_DIR')}",
            "for case in cases:",
            "    if case['call'] == call:",
            "        print(json.dumps(case['answer']))",
            "        sys.exit(0)",
            "print('Unexpected Cargo call: ' + json.dumps(call), file=sys.stderr)",
            "sys.exit(1)",
            "",
        ]))
        cargo.chmod(0o755)
        os.environ["PATH"] = str(binaries) + os.pathsep + os.environ.get("PATH", "")

    def test_relative_target_argument_overrides_config(self) -> None:
        target = self.workspace / "relative-output"
        row = self.write_step(["cargo", "check", "--config", 'build.target-dir="unused"',
                               "--target-dir", "relative-output"], target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_absolute_target_argument_selects_output(self) -> None:
        target = self.temporary / "absolute-output"
        row = self.write_step(["cargo", "check", "--target-dir=" + str(target)],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_inline_config_selects_target_output(self) -> None:
        config = self.workspace / ".cargo/config.toml"
        config.parent.mkdir()
        _ = config.write_text('[build]\ntarget-dir = "file-output"\n')
        target = self.workspace / "configured-output"
        row = self.write_step(["cargo", "check", "--config=build.target-dir=\"configured-output\""],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_env_assignment_selects_target_output(self) -> None:
        target = self.workspace / "environment-output"
        row = self.write_step(["env", "CARGO_TARGET_DIR=environment-output", "cargo", "check"],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_target_environment_overrides_cli_config_for_output_pair(self) -> None:
        target = self.workspace / "environment-output"
        configured = self.workspace / "configured-output"
        row = self.write_step(["env", "CARGO_TARGET_DIR=" + str(target), "cargo", "check",
                               "--config", 'build.target-dir="' + str(configured) + '"'],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_manifest_argument_selects_workspace_outside_working_folder(self) -> None:
        config = self.outside / ".cargo/config.toml"
        config.parent.mkdir()
        _ = config.write_text('[build]\ntarget-dir = "manifest-output"\n')
        target = self.outside / "manifest-output"
        row = self.write_step(["cargo", "check", "--manifest-path", "../workspace/member/Cargo.toml"],
                              target, cwd=self.outside)
        self.assert_output_pair(row, target, target)

    def test_build_directory_config_preserves_separate_intermediate_output(self) -> None:
        target = self.workspace / "target"
        build = self.workspace / "intermediate-output"
        row = self.write_step(["cargo", "check", "--config", 'build.build-dir="intermediate-output"'],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, build)

    def test_config_file_resolves_paths_from_its_parent_folder(self) -> None:
        config = self.temporary / "settings" / ".cargo/config.toml"
        config.parent.mkdir(parents=True)
        _ = config.write_text('[build]\ntarget-dir = "file-output"\n')
        target = self.temporary / "settings/file-output"
        row = self.write_step(["cargo", "check", "--config", str(config)], target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)

    def test_env_chdir_resolves_relative_target_from_effective_working_folder(self) -> None:
        target = self.workspace / "changed-output"
        row = self.write_step(["env", "-C", "../workspace", "CARGO_TARGET_DIR=changed-output", "cargo", "check"],
                              target, cwd=self.outside)
        self.assert_output_pair(row, target, target)

    def test_mismatched_output_pair_is_omitted_with_one_diagnostic(self) -> None:
        target = self.workspace / "captured-output"
        os.environ["BUILDLOG_TARGET_FOLDER"] = str(target)
        row = self.publish_step(["cargo", "check"], cwd=self.workspace)
        self.assertEqual(row["target_dir"], str(target))
        self.assertNotIn("target_directory", row)
        self.assertNotIn("build_directory", row)
        errors = (self.log_root / store.ERRORS_NAME).read_text()
        self.assertEqual(sum("step output folders:" in line for line in errors.splitlines()), 1)
        self.assertIn(str(self.workspace / "target"), errors)
        self.assertIn(str(target), errors)

    def test_unresolved_target_keeps_successful_output_pair(self) -> None:
        row = self.publish_step(["cargo", "check"], cwd=self.workspace)
        self.assertEqual(row["target_dir"], "unresolved")
        self.assertEqual(row["target_directory"], str(self.workspace / "target"))
        self.assertEqual(row["build_directory"], str(self.workspace / "target"))
        self.assertFalse((self.log_root / store.ERRORS_NAME).exists())

    def test_first_target_argument_selects_start_and_end_output(self) -> None:
        target = self.workspace / "first-output"
        row = self.write_step(["cargo", "check", "--target-dir=first-output", "--target-dir=last-output"],
                              target, cwd=self.workspace)
        self.assert_output_pair(row, target, target)


TERMINAL_BASELINE: dict[str, object] = {'kind': 'step',
 'v': 1,
 'id': 'fixture-id',
 'host': 'fixture',
 'target': 'natedev',
 'pinned': False,
 'started_at': '2026-10-02T12:00:00.000Z',
 'ended_at': '2026-10-02T12:00:02.000Z',
 'duration_s': 2.0,
 'mem_wait_s': 0,
 'slice': 'none',
 'step': 'nextest',
 'argv': NEXTEST,
 'cwd': '/fixture/project',
 'target_dir': '/fixture/target',
 'target_directory': '/fixture/target',
 'build_directory': '/fixture/build',
 'repo_identity': 'https://example.com/owner/project',
 'repo_path': '/fixture/project',
 'worktree': '/fixture/project',
 'branch': 'main',
 'sha': '1234567',
 'tree_key': 'tree',
 'tree_changed': False,
 'test_build_key': None,
 'caller': 'verify',
 'worker': None,
 'unit_session': None,
 'session': None,
 'call_id': None,
 'tty': True,
 'status': 0,
 'rustc': 'rustc fixture',
 'scope_resources': {'cpu': {'state': 'unavailable'},
                     'io': {'state': 'unavailable'}},
 'peak_mem_bytes': None,
 'mem_stall_some_s': None,
 'mem_stall_full_s': None,
 'finished_s': None,
 'crates_compiled': None,
 'compiled_crate_names': None,
 'warnings': None,
 'errors': None,
 'tests_run': None,
 'tests_passed': None,
 'tests_failed': None,
 'tests_skipped': None,
 'tests_flaky': None,
 'tests_retried': None,
 'tests': None,
 'mend_fixes': None,
 'mend_check_s': None,
 'mend_s': None,
 'sweep_freed_bytes': None,
 'log': None,
 'test_phase_peak_bytes': None,
 'test_phase_started_at': None,
 'test_phase_charge': None}


TEE_BASELINE: dict[str, object] = {
    **TERMINAL_BASELINE,
    "tty": False,
    "finished_s": 1.0,
    "crates_compiled": 0,
    "compiled_crate_names": [],
    "warnings": 0,
    "errors": 0,
    "tests_retried": 0,
    "tests": [],
}


class VerifyOutputEvidenceTests(StepOutputFolderFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        tools = self.temporary / "tools"
        tools.mkdir()
        launcher = tools / "cargo"
        _ = launcher.write_text(
            "#!/bin/sh\n"
            + f"printf '%s\\n' \"$*\" >> '{self.temporary / 'cargo-calls'}'\n"
            + f"exec cat {shlex.quote(str(self.temporary / 'metadata.json'))}\n")
        launcher.chmod(0o755)
        self.set_metadata_roots(self.workspace, self.workspace / "target", self.workspace / "target")
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
        _ = cast(object, self.enterContext(mock.patch.object(
            subprocess, "run", side_effect=partial(subprocess.run, timeout=TEST_HANG_GUARD_SECS))))

    def set_metadata_roots(self, workspace: Path, target: Path, build: Path) -> None:
        _ = (self.temporary / "metadata.json").write_text(json.dumps({
            "workspace_root": str(workspace), "target_directory": str(target), "build_directory": str(build),
        }))

    def capture_export(self) -> str:
        metadata = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1"],
                                  cwd=self.workspace, capture_output=True, text=True, check=True,
                                  timeout=TEST_HANG_GUARD_SECS).stdout
        environment = dict(os.environ)
        _ = environment.pop("PYTHONPATH", None)
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(step_context.__file__)), "output-roots"],
                                input=metadata, cwd=self.workspace, env=environment, capture_output=True, text=True, check=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(len(result.stdout.rstrip("\n").split("\t")), 4, result.stdout + result.stderr)
        return result.stdout.strip()

    def assert_recorded_roots(self, exported: str, argv: list[str], *, reuse: bool = False,
                              cwd: Path | None = None) -> None:
        directory = cwd or self.workspace
        with mock.patch.dict(os.environ, {"BUILDLOG_OUTPUT_ROOTS": ""}):
            expected = step_context.step_output_roots(argv, str(directory))
        self.assertIsInstance(expected, CargoOutputRoots)
        assert isinstance(expected, CargoOutputRoots)
        answer = cast(dict[str, str], json.loads((self.temporary / "metadata.json").read_text()))
        self.assertEqual((str(expected.workspace_root), str(expected.target_directory), str(expected.build_directory)),
                         (answer["workspace_root"], answer["target_directory"], answer["build_directory"]))
        os.environ["BUILDLOG_OUTPUT_ROOTS"] = exported
        _ = (self.temporary / "cargo-calls").write_text("")
        self.assertEqual(step_context.step_output_roots(argv, str(directory)), expected)
        calls = (self.temporary / "cargo-calls").read_text().splitlines()
        self.assertEqual(sum("metadata" in line.split() for line in calls), 0 if reuse else 1, calls)
        os.environ["BUILDLOG_TARGET_FOLDER"] = str(expected.target_directory)
        _ = (self.temporary / "cargo-calls").write_text("")
        row = self.publish_step(argv, cwd=directory)
        self.assertEqual(row["target_directory"], str(expected.target_directory))
        self.assert_output_pair(row, expected.target_directory, expected.build_directory)
        calls = (self.temporary / "cargo-calls").read_text().splitlines()
        self.assertEqual(sum("metadata" in line.split() for line in calls), 0 if reuse else 1, calls)

    def test_plain_step_reuses_the_metadata_output_folders(self) -> None:
        self.assert_recorded_roots(self.capture_export(), ["cargo", "check"], reuse=True)

    def test_target_environment_wrapper_reads_its_own_output_folders(self) -> None:
        exported = self.capture_export()
        target = self.workspace / "wrapped-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["env", "CARGO_TARGET_DIR=wrapped-target", "cargo", "check"])

    def test_build_environment_wrapper_reads_its_own_output_folders(self) -> None:
        exported = self.capture_export()
        self.set_metadata_roots(self.workspace, self.workspace / "target", self.workspace / "wrapped-build")
        self.assert_recorded_roots(exported, ["env", "CARGO_BUILD_BUILD_DIR=wrapped-build", "cargo", "check"])

    def test_external_manifest_reads_the_other_workspace_output_folders(self) -> None:
        other = self.temporary / "other"
        other.mkdir()
        _ = (other / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        exported = self.capture_export()
        self.set_metadata_roots(other, other / "target", other / "target")
        self.assert_recorded_roots(exported, ["cargo", "check", "--manifest-path", str(other / "Cargo.toml")])

    def test_inline_config_reads_its_selected_output_folders(self) -> None:
        exported = self.capture_export()
        target = self.workspace / "inline-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["cargo", "check", "--config", 'build.target-dir="inline-target"'])

    def test_target_argument_reads_its_selected_output_folders(self) -> None:
        exported = self.capture_export()
        target = self.workspace / "argument-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["cargo", "check", "--target-dir=argument-target"])

    def test_changed_config_reads_the_new_output_folders(self) -> None:
        exported = self.capture_export()
        config = self.workspace / ".cargo/config.toml"
        config.parent.mkdir()
        _ = config.write_text('[build]\ntarget-dir = "changed-target"\n')
        target = self.workspace / "changed-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["cargo", "check"])

    def test_legacy_config_change_reads_the_new_output_folders(self) -> None:
        exported = self.capture_export()
        config = self.workspace / ".cargo/config"
        config.parent.mkdir()
        _ = config.write_text('[build]\ntarget-dir = "legacy-target"\n')
        target = self.workspace / "legacy-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["cargo", "check"])

    def test_changed_manifest_rejects_the_prior_metadata_evidence(self) -> None:
        exported = self.capture_export()
        _ = (self.workspace / "Cargo.toml").write_text('[workspace]\nmembers = []\nresolver = "3"\n')
        self.assert_recorded_roots(exported, ["cargo", "check"])

    def test_other_working_folder_reads_its_own_output_folders(self) -> None:
        member = self.workspace / "other-cwd"
        member.mkdir()
        self.assert_recorded_roots(self.capture_export(), ["cargo", "check"], cwd=member)

    def test_cargo_home_config_change_reads_the_new_output_folders(self) -> None:
        exported = self.capture_export()
        home = Path(os.environ["CARGO_HOME"])
        home.mkdir(exist_ok=True)
        _ = (home / "config.toml").write_text('[build]\ntarget-dir = "home-target"\n')
        target = home.parent / "home-target"
        self.set_metadata_roots(self.workspace, target, target)
        self.assert_recorded_roots(exported, ["cargo", "check"])

    def test_toolchain_file_change_rejects_prior_metadata_evidence(self) -> None:
        exported = self.capture_export()
        _ = (self.workspace / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "stable"\n')
        self.assert_recorded_roots(exported, ["cargo", "check"])

    def test_invalid_metadata_clears_the_export(self) -> None:
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(step_context.__file__)), "output-roots"],
                                input="{}", cwd=self.workspace, capture_output=True, text=True, check=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.stdout, "")
