"""Pin the step row and step context that record.py writes."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import TYPE_CHECKING, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import record
import step_context
import treekey
import store
from test_index import use_test_log
from test_record import START, NEXTEST

use_test_log()

if TYPE_CHECKING:
    from ..lib.cargo_output import CargoOutputRoots
else:
    from scripts.lib.cargo_output import CargoOutputRoots


class RecorderCompatibilityTests(unittest.TestCase):
    def test_scp_remote_identity_does_not_load_url_parsing(self) -> None:
        source = (
            "import sys; sys.path.insert(0, sys.argv[1]); import record; "
            "assert record.remote_identity('git@EXAMPLE.com:owner/project.git', '/fixture') "
            "== 'https://example.com/owner/project'; assert 'urllib.parse' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", source, str(Path(__file__).parent)],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plain_phase_context_does_not_load_admission(self) -> None:
        source = (
            "import sys; sys.path.insert(0, sys.argv[1]); import record; "
            "value = record.test_phase_context('{}', ['cargo', 'nextest', 'run', '-j4']); "
            "assert value['test_threads'] == 4; assert 'memory_admit' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", source, str(Path(__file__).parent)],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_row_ids_do_not_load_random_or_temporary_file_modules(self) -> None:
        source = (
            "import sys; sys.path.insert(0, sys.argv[1]); import record; "
            "value = record.new_id('fixture', 0); assert value.startswith('fixture-19700101T000000-'); "
            "assert len(value.rsplit('-', 1)[1]) == 6; "
            "assert 'secrets' not in sys.modules; assert 'tempfile' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", source, str(Path(__file__).parent)],
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
            "import sys; sys.path.insert(0, sys.argv[1]); import step_context; "
            "assert 'json' not in sys.modules; assert 'store' not in sys.modules; "
            "step_context.metadata_target_folder('  ', [], '/fixture'); "
            "assert 'json' not in sys.modules; assert 'store' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", source, str(Path(__file__).parent)],
                                capture_output=True, text=True, env={
                                    key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_target_configuration_records_the_error(self) -> None:
        import cargo_target_config

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
                "import sys; sys.path.insert(0, sys.argv[1]); from scripts.buildlog import record; "
                "memory = record.peak_and_stall(sys.argv[2]); "
                "assert memory.peak_bytes == 123456; assert memory.stall_some_s == 1.25; "
                "assert memory.stall_full_s is record.cgroup_capture.Unmeasured.VALUE"
            )
            result = subprocess.run(
                [sys.executable, "-c", source, str(Path(__file__).resolve().parents[2]), str(peak)],
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
                record, "output", side_effect=[remote, roots] if not remote else [remote],
            ):
                self.assertEqual(record.recorded_repository_key(NEXTEST, "/fixture/project"), expected)
        self.assertEqual(record.recorded_repository_key([], "/fixture/project"), store.UNKNOWN_IDENTITY)

    def test_target_lookup_reads_configuration_once(self) -> None:
        with mock.patch.object(record, "sync_config", return_value={
            "sync.hosts.first": "first-host", "sync.hosts.second": "second-host",
        }) as read:
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(record.recorded_target("second-host"), "second")
            read.assert_called_once_with()

    def capture(self, terminal: bool) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
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
                mock.patch.object(record, "read_cargo_output_roots", return_value=CargoOutputRoots(
                    Path("/fixture/project"), Path("/fixture/target"), Path("/fixture/build"))),
                mock.patch.object(treekey, "tree_key", return_value="tree"),
            ):
                record.step(["0", str(int(START)), str(int(START) + 2), "1" if terminal else "0",
                             "" if terminal else str(held), "",
                             *NEXTEST])
            paths = list(Path(temporary).glob("fixture/*.jsonl"))
            self.assertEqual(len(paths), 1)
            return cast(dict[str, object], json.loads(paths[0].read_text()))

    def test_terminal_row_matches_baseline(self) -> None:
        self.assertEqual(self.capture(True), TERMINAL_BASELINE)

    def test_tee_row_matches_baseline(self) -> None:
        self.assertEqual(self.capture(False), TEE_BASELINE)

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
