"""Unit configuration survives retirement, interrupted publication and resumed launch."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tomllib
from typing import cast
from unittest import mock
from unittest.mock import patch

from scripts.lib import atomic_file, conf, git_exclude
from scripts.lib.git_exclude import ExcludeRefused
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff, unit_config as config
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.agents import remote
from scripts.production.unit_config_test_support import UnitConfigFixture
from scripts.production.unit_config_format_test_support import (
    check_format, count_taplo, remove_assignment_spacing, require_taplo, write_aligning_config,
)


class UnitPortSelectionTests(StaffTestCase):
    def test_port_range_excludes_recorded_reserved_and_bound_ports(self) -> None:
        atomic_file.write_text_atomic(self.root / "Cargo.toml", '[package]\nname = "hana"\n')
        peer = self.root / "peer"
        peer.mkdir()
        owner = fixture_member("showrunner", name="port-owner")
        row = fixture_member("unit", name="port-peer", parent=owner.id,
                             worktree=staff.PresentWorktree(remote.local_machine(), peer))
        for cause in ("recorded", "bound", "mac", "smoke", "full", "available"):
            with self.subTest(cause=cause), socket.socket() as held:
                held.bind(("127.0.0.1", 0))
                number = cast(tuple[str, int], held.getsockname())[1]
                settings = self.root / "production.conf"
                atomic_file.write_text_atomic(settings, f"# whole: 1..\nunit_port_first={number}\n# whole: 1..\nunit_port_last={number}\n"
                    + f"# whole: 1..\nmac_run_port={number if cause == 'mac' else 1}\n"
                    + f"# whole: 1..\nsmoke_port={number if cause == 'smoke' else 2}\n")
                atomic_file.write_text_atomic(peer / ".unit.toml", 'plan = "plan.md"\nresponsibilities = []\n'
                    + (f"port = {number}\n" if cause == "recorded" else ""))
                if cause not in ("bound", "full"):
                    held.close()
                settings_read = conf.read(settings)
                with patch.object(conf, "read", return_value=settings_read):
                    if cause == "available":
                        self.assertEqual(config.default_port(self.root, config.NoAppPort()), config.AppPort(number))
                    else:
                        with self.assertRaises(config.ConfigRefused) as refused:
                            _ = config.default_port(self.root, config.NoAppPort())
                        self.assertEqual(len(str(refused.exception).splitlines()), 1)
                        self.assertIn(f"{number}..{number}", str(refused.exception))
        self.assertIsNotNone(staff.snapshot().by_id(row.id))


class UnitPortCellTests(StaffTestCase):
    def test_app_port_defaults_skip_a_bound_port_and_keep_explicit_values(self) -> None:
        import tempfile
        _ = fixture_member("showrunner", name="port-defaults")
        with tempfile.TemporaryDirectory() as directory, socket.socket() as held:
            root = Path(directory)
            atomic_file.write_text_atomic(root / "Cargo.toml", '[package]\nname = "hana"\n')
            held.bind(("127.0.0.1", 0))
            held.listen()
            occupied = cast(tuple[str, int], held.getsockname())[1]
            for selected in (config.NoAppPort(), config.AppPort(8123)):
                with self.subTest(selected=selected):
                    port = config.default_port(root, selected)
                    self.assertIsInstance(port, config.AppPort)
                    assert isinstance(port, config.AppPort)
                    self.assertNotEqual(port.number, occupied)
                    if isinstance(selected, config.AppPort):
                        self.assertEqual(port, selected)
                    else:
                        with socket.socket() as available:
                            available.bind(("127.0.0.1", port.number))

    def test_raw_port_cells_accept_absence_numbers_and_history_notes(self) -> None:
        for raw in ("—", "-", "none", "", " `—` "):
            with self.subTest(raw=raw):
                self.assertEqual(config.parse_port_cell(raw), config.NoAppPort())
        for raw in ("15733", "15733 (note)", "`15733`", "`15733` (note)",
                    "15733 (after it clashed with startup on 15731, 2026-10-02)",
                    "1", "65535"):
            with self.subTest(raw=raw):
                expected = 1 if raw == "1" else 65535 if raw == "65535" else 15733
                self.assertEqual(config.parse_port_cell(raw), config.AppPort(expected))

    def test_invalid_port_cells_keep_the_raw_text_in_the_refusal(self) -> None:
        for raw in ("0", "70000", "15733 extra", "abc", "`70000`", "`15733 extra`"):
            with self.subTest(raw=raw), self.assertRaises(config.ConfigRefused) as caught:
                _ = config.parse_port_cell(raw)
            self.assertIn(raw, str(caught.exception))


class UnitPortRepairTests(UnitConfigFixture):
    def test_active_repair_preserves_other_fields_and_an_unchanged_rerun_preserves_bytes(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        original = self.config()
        _ = config.write(self.worktree, original)
        settled = config.AppPort(8234)
        target = config.InWorktree(self.source)
        with patch.object(atomic_file, "write_bytes_atomic", wraps=atomic_file.write_bytes_atomic) as publications:
            self.assertIsInstance(config.repair_port(target, original.port, settled), config.Published)
        self.assertEqual(publications.call_count, 1)
        self.assertEqual(publications.call_args.args[0], self.source)
        self.assertEqual(config.read(self.worktree), replace(original, port=settled))
        result = check_format(binary, self.source, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        complete = self.file_versions()
        with patch.object(atomic_file, "write_bytes_atomic", wraps=atomic_file.write_bytes_atomic) as writes:
            self.assertIsInstance(config.repair_port(target, original.port, settled), config.AlreadyCurrent)
        writes.assert_not_called()
        self.assertEqual(self.file_versions(), complete)

    def test_parked_repair_keeps_retirement_and_saved_formatting_after_worktree_removal(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        self.park_config()
        saved = config.read_parked(self.unit)
        assert isinstance(saved, config.UnitConfig)
        removed = self.root / "removed-staff-folder"
        row = staff.snapshot().by_id(self.unit)
        assert row is not None
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", removed), expected=row.worktree)
        self.parked.unlink()
        _ = config.write_parked(self.unit, saved)
        self.assertFalse(removed.exists())
        source = self.source.read_bytes(), self.source.stat().st_mtime_ns
        target = config.Parked(self.parked)
        settled = config.AppPort(8234)
        self.assertIsInstance(config.repair_port(target, saved.port, settled), config.Published)
        repaired = config.read_parked(self.unit)
        self.assertEqual(repaired, replace(saved, port=settled))
        assert isinstance(repaired, config.UnitConfig)
        self.assertEqual(repaired.formatting, saved.formatting)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), source)
        result = check_format(binary, self.parked, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        complete = self.file_versions()
        self.assertIsInstance(config.repair_port(target, saved.port, settled), config.AlreadyCurrent)
        self.assertEqual(self.file_versions(), complete)

    def test_repair_accepts_recorded_no_app_port_and_can_settle_no_app_port(self) -> None:
        for parked in (False, True):
            with self.subTest(parked=parked):
                original = replace(self.config(), port=config.NoAppPort())
                if parked:
                    original = replace(original, retired=self.retirement())
                    _ = config.write_parked(self.unit, original)
                    target = config.Parked(self.parked)
                else:
                    _ = config.write(self.worktree, original)
                    target = config.InWorktree(self.source)
                self.assertIsInstance(config.repair_port(target, config.NoAppPort(), config.AppPort(8234)), config.Published)
                self.assertIsInstance(config.repair_port(target, config.AppPort(8234), config.NoAppPort()), config.Published)
                actual = config.read_parked(self.unit) if parked else config.read(self.worktree)
                self.assertEqual(actual, original)

    def test_third_port_refuses_before_any_publication_for_active_and_parked_configs(self) -> None:
        self.park_config()
        before = self.file_versions()
        for target in (config.InWorktree(self.source), config.Parked(self.parked)):
            with (self.subTest(target=target),
                  patch.object(atomic_file, "write_bytes_atomic", wraps=atomic_file.write_bytes_atomic) as writes):
                with self.assertRaises(config.ConfigRefused) as caught:
                    _ = config.repair_port(target, config.AppPort(8001), config.AppPort(8002))
            self.assertIn(str(target.path), str(caught.exception))
            self.assertIn("port", str(caught.exception))
            writes.assert_not_called()
            self.assertEqual(self.file_versions(), before)

    def test_missing_or_malformed_config_cannot_supply_a_port(self) -> None:
        for target in (config.InWorktree(self.source), config.Parked(self.parked)):
            with self.subTest(target=target, state="missing"):
                with self.assertRaises(config.ConfigRefused):
                    _ = config.repair_port(target, config.NoAppPort(), config.AppPort(8234))
                self.assertFalse(target.path.exists())
            atomic_file.write_text_atomic(target.path, 'plan = "standby"\nport = "unknown"\nresponsibilities = []\n')
            before = target.path.read_bytes(), target.path.stat().st_mtime_ns
            with self.subTest(target=target, state="malformed"):
                with self.assertRaises(config.ConfigRefused):
                    _ = config.repair_port(target, config.NoAppPort(), config.AppPort(8234))
                self.assertEqual((target.path.read_bytes(), target.path.stat().st_mtime_ns), before)

    def test_interrupted_repair_before_or_after_publication_recovers_once(self) -> None:
        real_replace = os.replace
        settled = config.AppPort(8234)
        for parked in (False, True):
            for published in (False, True):
                with self.subTest(parked=parked, published=published):
                    self.parked.unlink(missing_ok=True)
                    self.park_config()
                    target = config.Parked(self.parked) if parked else config.InWorktree(self.source)
                    original = config.read_parked(self.unit) if parked else config.read(self.worktree)
                    assert isinstance(original, config.UnitConfig)
                    other = self.source if parked else self.parked
                    other_before = other.read_bytes(), other.stat().st_mtime_ns
                    target_before = target.path.read_bytes()

                    def interrupt(temporary: Path, destination: Path) -> None:
                        if destination == target.path:
                            if published:
                                real_replace(temporary, destination)
                            raise OSError("stopped at port publication")
                        real_replace(temporary, destination)

                    with patch.object(os, "replace", side_effect=interrupt):
                        with self.assertRaisesRegex(OSError, "stopped at port publication"):
                            _ = config.repair_port(target, original.port, settled)
                    actual = config.read_parked(self.unit) if parked else config.read(self.worktree)
                    self.assertEqual(actual, replace(original, port=settled) if published else original)
                    if not published:
                        self.assertEqual(target.path.read_bytes(), target_before)
                    self.assertEqual((other.read_bytes(), other.stat().st_mtime_ns), other_before)
                    expected = config.AlreadyCurrent if published else config.Published
                    self.assertIsInstance(config.repair_port(target, original.port, settled), expected)
                    self.assertEqual(config.read_parked(self.unit) if parked else config.read(self.worktree),
                                     replace(original, port=settled))
                    complete = self.file_versions()
                    self.assertIsInstance(config.repair_port(target, original.port, settled), config.AlreadyCurrent)
                    self.assertEqual(self.file_versions(), complete)
                    self.assertEqual(list(target.path.parent.glob(f".{target.path.name}.*.tmp")), [])

    def test_regular_parked_writer_still_refuses_a_port_change(self) -> None:
        self.park_config()
        original = config.read_parked(self.unit)
        assert isinstance(original, config.UnitConfig)
        before = self.file_versions()
        with self.assertRaisesRegex(config.ConfigRefused, "port"):
            _ = config.write_parked(self.unit, replace(original, port=config.AppPort(8234)))
        self.assertEqual(self.file_versions(), before)

    def test_active_repair_refuses_a_tracked_configuration_without_changing_it(self) -> None:
        _ = config.write(self.worktree, self.config())
        _ = self.git("add", "-f", ".unit.toml")
        before = self.file_versions()
        with self.assertRaises((ExcludeRefused, config.ConfigRefused)):
            _ = config.repair_port(config.InWorktree(self.source), self.config().port, config.AppPort(8234))
        self.assertEqual(self.file_versions(), before)

    def test_repair_refuses_a_port_that_changes_before_publication(self) -> None:
        _ = config.write(self.worktree, self.config())
        competing = self.source.read_text().replace("8123", "8999")
        real_check = git_exclude.require_untracked_ignored

        def publish_competing_port(worktree: Path, name: str) -> None:
            real_check(worktree, name)
            atomic_file.write_text_atomic(self.source, competing)

        with patch.object(git_exclude, "require_untracked_ignored", side_effect=publish_competing_port):
            with self.assertRaises(config.ConfigRefused):
                _ = config.repair_port(config.InWorktree(self.source), self.config().port, config.AppPort(8234))
        self.assertEqual(self.source.read_text(), competing)
        self.assertEqual(config.read(self.worktree), replace(self.config(), port=config.AppPort(8999)))

    def test_writer_after_the_repair_guard_cannot_lose_its_third_port(self) -> None:
        writer = """
from dataclasses import replace
import fcntl
from pathlib import Path
import sys
from scripts.production import unit_config as config
from scripts.production.staff import StaffId

lock_path = Path.home() / '.local/state/staff/unit-config.lock'
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open('a+b') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(75)
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
path = Path(sys.argv[2])
if sys.argv[1] == 'parked':
    value = config.read_parked(StaffId(sys.argv[3]), cleanup=False)
    assert isinstance(value, config.UnitConfig)
    config.repair_port(config.Parked(path), value.port, config.AppPort(8999))
else:
    value = config.read(path.parent, cleanup=False)
    assert isinstance(value, config.UnitConfig)
    config.write(path.parent, replace(value, port=config.AppPort(8999)))
"""
        real_read_bytes = Path.read_bytes
        for parked in (False, True):
            with self.subTest(parked=parked):
                self.parked.unlink(missing_ok=True)
                self.park_config()
                target = config.Parked(self.parked) if parked else config.InWorktree(self.source)
                original = config.read_parked(self.unit) if parked else config.read(self.worktree)
                assert isinstance(original, config.UnitConfig)
                attempts: list[subprocess.CompletedProcess[str]] = []
                reads = 0

                def start_writer_after_guard(path: Path) -> bytes:
                    nonlocal reads
                    observed = real_read_bytes(path)
                    if path == target.path:
                        reads += 1
                        if reads == 2:
                            attempts.append(subprocess.run(
                                [sys.executable, "-B", "-c", writer, "parked" if parked else "active",
                                 str(target.path), self.unit],
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                            ))
                    return observed

                with patch.object(Path, "read_bytes", start_writer_after_guard):
                    try:
                        outcome = config.repair_port(target, original.port, config.AppPort(8234))
                    except config.ConfigRefused as error:
                        outcome = error
                self.assertEqual(len(attempts), 1)
                self.assertIn(attempts[0].returncode, (0, 75), attempts[0].stderr)
                if attempts[0].returncode == 0:
                    self.assertIsInstance(outcome, config.ConfigRefused)
                    expected = replace(original, port=config.AppPort(8999))
                else:
                    self.assertIsInstance(outcome, config.Published)
                    expected = replace(original, port=config.AppPort(8234))
                actual = config.read_parked(self.unit) if parked else config.read(self.worktree)
                self.assertEqual(actual, expected)

    def test_active_and_parked_writers_enter_after_the_repair_publishes(self) -> None:
        writer = """
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import sys
from scripts.production import unit_config as config

path = Path(sys.argv[2])
lock_path = Path.home() / '.local/state/staff/unit-config.lock'
real_flock = fcntl.flock
def observe_lock(descriptor, operation):
    number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
    if operation == fcntl.LOCK_EX and os.path.samestat(os.fstat(number), lock_path.stat()):
        print('attempt', flush=True)
        real_flock(descriptor, operation)
        assert config.parse(path.read_text()).port == config.AppPort(8234)
        print('acquired', flush=True)
    else:
        real_flock(descriptor, operation)
fcntl.flock = observe_lock
if sys.argv[1] == 'parked':
    config.repair_port(config.Parked(path), config.AppPort(8234), config.AppPort(8999))
else:
    value = config.read(path.parent, cleanup=False)
    assert isinstance(value, config.UnitConfig)
    config.write(path.parent, replace(value, port=config.AppPort(8999)))
print('complete', flush=True)
"""
        real_read_bytes = Path.read_bytes
        for parked in (False, True):
            with self.subTest(parked=parked):
                self.parked.unlink(missing_ok=True)
                self.park_config()
                target = config.Parked(self.parked) if parked else config.InWorktree(self.source)
                original = config.read_parked(self.unit) if parked else config.read(self.worktree)
                assert isinstance(original, config.UnitConfig)
                processes: list[subprocess.Popen[str]] = []
                results: list[tuple[str, str]] = []
                reads = 0

                def start_public_writer(path: Path) -> bytes:
                    nonlocal reads
                    observed = real_read_bytes(path)
                    if path == target.path:
                        reads += 1
                        if reads == 2:
                            process = subprocess.Popen(
                                [sys.executable, "-B", "-c", writer, "parked" if parked else "active", str(target.path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            )
                            processes.append(process)
                            assert process.stdout is not None
                            ready, _, _ = select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)
                            self.assertTrue(ready, "The competing writer must attempt its lock.")
                            self.assertEqual(process.stdout.readline(), "attempt\n")
                    return observed

                try:
                    with patch.object(Path, "read_bytes", start_public_writer):
                        self.assertIsInstance(config.repair_port(target, original.port, config.AppPort(8234)), config.Published)
                finally:
                    for process in processes:
                        try:
                            results.append(process.communicate(timeout=TEST_HANG_GUARD_SECS))
                        except subprocess.TimeoutExpired:
                            process.kill()
                            _ = process.communicate()
                            raise
                self.assertEqual(len(processes), 1)
                output, errors = results[0]
                self.assertEqual(processes[0].returncode, 0, errors)
                self.assertEqual(output, "acquired\ncomplete\n")
                self.assertEqual(config.read_parked(self.unit) if parked else config.read(self.worktree),
                                 replace(original, port=config.AppPort(8999)))


class UnitConfigTests(UnitConfigFixture):
    def test_responsibilities_accept_names_and_paths_and_reject_invalid_values(self) -> None:
        responsibilities = ("nightly-review", "scripts/production/", "settings.json")
        value = config.UnitConfig(config.StandbyPlan(), config.NoAppPort(), responsibilities)
        self.assertEqual(value.responsibilities, tuple(sorted(responsibilities)))
        for invalid in ("", "all production scripts", "../outside", "/absolute", "scripts/../outside"):
            with self.subTest(invalid=invalid), self.assertRaises(config.ConfigRefused):
                _ = replace(value, responsibilities=(invalid,))

    def test_remote_configuration_fields_preserve_retirement_and_reject_invalid_values(self) -> None:
        fields: dict[str, object] = {
            "plan": "docs/plans/worker.md", "port": 8123,
            "responsibilities": ["nightly-review", "build-check"],
            "retired": {"date": "2026-10-31", "session": "session-before-retire",
                        "folder": str(self.worktree), "branch": "feature/worker"},
        }
        self.assertEqual(config.decode(fields), replace(self.config(), retired=self.retirement()))
        for key, invalid in (("port", True), ("responsibilities", "nightly-review"),
                             ("retired", {"session": "session-before-retire"})):
            with self.subTest(key=key):
                with self.assertRaises(config.ConfigRefused):
                    _ = config.decode({**fields, key: invalid})
        self.assertFalse(self.source.exists())
        self.assertFalse(self.parked.exists())

    def test_launcher_subprocess_fake_does_not_replace_taplo_for_live_or_parked_writes(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)

        def launcher_run(command: list[str], **_keywords: object) -> object:
            if command[0] == "taplo":
                return subprocess.CompletedProcess(command, 0, "", "")
            return cast(object, mock.DEFAULT)

        with count_taplo(self, self.root) as calls, patch(
            "subprocess.run", wraps=subprocess.run, side_effect=launcher_run,
        ):
            self.assertIsInstance(config.write(self.worktree, self.config()), config.Published)
            self.assertIsInstance(config.park(self.unit, self.worktree, self.retirement()), config.Published)
            self.assertEqual(calls.count(), 2)

        for path in (self.source, self.parked):
            result = check_format(binary, path, self.worktree)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertEqual(config.read_parked(self.unit), replace(self.config(), retired=self.retirement()))

    def test_parked_write_uses_live_config_before_saved_formatting(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        self.park_config()
        saved = config.read_parked(self.unit)
        assert isinstance(saved, config.UnitConfig)
        before = self.parked.read_bytes()
        atomic_file.write_text_atomic(self.worktree / "taplo.toml",
                                      "[formatting]\nalign_entries = false\nreorder_keys = false\n")
        self.assertIsInstance(config.write_parked(self.unit, saved), config.Published)
        self.assertNotEqual(self.parked.read_bytes(), before)
        result = check_format(binary, self.parked, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = config.read_parked(self.unit)
        assert isinstance(current, config.UnitConfig)
        self.assertEqual(current, saved)
        self.assertEqual(current.formatting, config.repository_formatting(self.worktree))
        self.assertIsInstance(config.write_parked(self.unit, current), config.AlreadyCurrent)

    def test_removed_worktree_uses_saved_formatting_from_read_parked(self) -> None:
        binary = require_taplo(self)
        removed = self.root / "removed-worktree"
        _ = self.git("worktree", "add", "-b", "retired", str(removed))
        _ = write_aligning_config(removed)
        retired = self.retirement()
        row = staff.snapshot().by_id(self.unit)
        assert row is not None
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", removed), expected=row.worktree)
        _ = config.write(removed, self.config())
        _ = config.park(self.unit, removed, retired)
        saved = config.read_parked(self.unit)
        assert isinstance(saved, config.UnitConfig)
        before = self.parked.read_bytes()
        _ = self.git("worktree", "remove", "--force", str(removed))
        _ = write_aligning_config(self.worktree)
        remove_assignment_spacing(self.parked)
        self.assertIsInstance(config.write_parked(self.unit, saved), config.Published)
        self.assertEqual(self.parked.read_bytes(), before)
        result = check_format(binary, self.parked, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(config.write_parked(self.unit, saved), config.AlreadyCurrent)

    def test_write_applies_a_rule_scoped_to_the_unit_file(self) -> None:
        binary = require_taplo(self)
        settings = self.worktree / "taplo.toml"
        atomic_file.write_text_atomic(settings, '[formatting]\nalign_entries = false\nreorder_keys = false\n'
                                     + '\n[[rule]]\ninclude = [".unit.toml"]\n'
                                     + '[rule.formatting]\nalign_entries = true\nreorder_keys = true\n')
        self.assertTrue(config.needs_write(self.worktree, self.config()))
        _ = config.write(self.worktree, self.config())
        result = check_format(binary, self.source, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        default_result = check_format(binary, self.source, self.worktree, defaults=True)
        self.assertNotEqual(default_result.returncode, 0, "The file rule must change the writer's output.")
        self.assertEqual(config.read(self.worktree), self.config())

    def test_write_uses_the_repository_aligning_config(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        _ = config.write(self.worktree, self.config())
        result = check_format(binary, self.source, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read(self.worktree), self.config())
        default_result = check_format(binary, self.source, self.worktree, defaults=True)
        self.assertNotEqual(default_result.returncode, 0, "The fixture must distinguish repository formatting from defaults.")

    def test_write_repairs_equal_values_with_wrong_format_and_then_preserves_bytes_and_mtime(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        _ = config.write(self.worktree, self.config())
        remove_assignment_spacing(self.source)
        self.assertEqual(config.read(self.worktree), self.config())
        before = self.source.read_bytes()
        self.assertTrue(config.needs_write(self.worktree, self.config()))
        _ = config.write(self.worktree, self.config())
        self.assertNotEqual(self.source.read_bytes(), before)
        self.assertEqual(config.read(self.worktree), self.config())
        result = check_format(binary, self.source, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repaired = self.file_versions()
        self.assertFalse(config.needs_write(self.worktree, self.config()))
        _ = config.write(self.worktree, self.config())
        self.assertEqual(self.file_versions(), repaired)

    def test_parked_write_uses_the_recorded_repository_after_worktree_removal(self) -> None:
        binary = require_taplo(self)
        removed = self.root / "removed-worktree"
        _ = self.git("worktree", "add", "-b", "retired", str(removed))
        _ = write_aligning_config(removed)
        retired = self.retirement()
        row = staff.snapshot().by_id(self.unit)
        assert row is not None
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", removed), expected=row.worktree)
        _ = config.write(removed, self.config())
        _ = config.park(self.unit, removed, retired)
        _ = self.git("worktree", "remove", "--force", str(removed))
        self.assertFalse(removed.exists())
        _ = write_aligning_config(self.worktree)
        value = replace(self.config(), retired=retired)
        result = check_format(binary, self.parked, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read_parked(self.unit), value)
        remove_assignment_spacing(self.parked)
        before = self.parked.read_bytes()
        self.assertTrue(config.parked_needs_write(self.unit, value))
        _ = config.write_parked(self.unit, value)
        self.assertNotEqual(self.parked.read_bytes(), before)
        result = check_format(binary, self.parked, self.worktree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repaired = self.file_versions()
        self.assertFalse(config.parked_needs_write(self.unit, value))
        _ = config.write_parked(self.unit, value)
        self.assertEqual(self.file_versions(), repaired)

    def test_parked_write_uses_defaults_without_a_repository_or_worktree(self) -> None:
        binary = require_taplo(self)
        _ = write_aligning_config(self.worktree)
        removed = self.root / "removed-worktree"
        self.assertFalse(removed.exists())
        row = staff.snapshot().by_id(self.unit)
        assert row is not None
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", removed), expected=row.worktree)
        value = replace(self.config(), retired=self.retirement())
        _ = config.write_parked(self.unit, value)
        result = check_format(binary, self.parked, self.worktree, defaults=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read_parked(self.unit), value)
        aligned = check_format(binary, self.parked, self.worktree)
        self.assertNotEqual(aligned.returncode, 0, "The fixture must distinguish repository formatting from defaults.")

    def test_preview_read_keeps_abandoned_files_until_a_recovery_read(self) -> None:
        _ = config.write(self.worktree, self.config())
        dead = self.source.with_name(f".{self.source.name}.999999999.abandoned.tmp")
        _ = dead.write_text('plan="unfinished')
        before = self.file_versions(), dead.read_bytes(), dead.stat().st_mtime_ns
        self.assertEqual(config.read(self.worktree, cleanup=False), self.config())
        self.assertTrue(dead.exists())
        self.assertEqual((self.file_versions(), dead.read_bytes(), dead.stat().st_mtime_ns), before)
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertFalse(dead.exists())

    def test_plan_states_port_and_responsibilities_round_trip_without_tracking(self) -> None:
        for plan in (config.PlanFile(Path("docs/a plan.md")), config.StandbyPlan(), config.PermanentPlan()):
            for port in (config.NoAppPort(), config.AppPort(8124)):
                with self.subTest(plan=plan, port=port):
                    value = config.UnitConfig(plan, port, ("nightly-review",))
                    _ = config.write(self.worktree, value)
                    self.assertEqual(config.read(self.worktree), value)
                    fields = tomllib.loads(self.source.read_text())
                    self.assertEqual(fields["responsibilities"], ["nightly-review"])
                    self.assertEqual("port" in fields, isinstance(port, config.AppPort))
                    self.assertNotIn("retired", fields)
                    before = self.file_versions()
                    _ = config.write(self.worktree, value)
                    self.assertEqual(self.file_versions(), before)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git("ls-files", ".unit.toml"), "")
        self.assertEqual(self.git("check-ignore", ".unit.toml"), ".unit.toml")

    def test_missing_config_is_distinct_from_malformed_or_unreadable_config(self) -> None:
        missing = config.read(self.worktree)
        self.assertIsInstance(missing, config.ConfigMissing)
        if isinstance(missing, config.ConfigMissing):
            self.assertIn(str(self.source), missing.reason)
        for text in ('plan = [', 'plan="standby"\nport="1234"\nresponsibilities=[]\n',
                     'plan="../elsewhere"\nresponsibilities=[]\n',
                     'plan="/absolute"\nresponsibilities=[]\n',
                     'plan="standby"\nresponsibilities="nightly-review"\n'):
            with self.subTest(text=text):
                _ = self.source.write_text(text)
                with self.assertRaises(config.ConfigRefused) as caught:
                    _ = config.read(self.worktree)
                self.assertIn(str(self.source), str(caught.exception))
                self.assertEqual(self.source.read_text(), text)
        _ = self.source.write_bytes(b'plan="\xff"')
        with self.assertRaises(config.ConfigRefused):
            _ = config.read(self.worktree)

    def test_park_restore_and_discard_keep_retirement_facts_until_the_last_step(self) -> None:
        self.assertIsInstance(config.place(self.unit, self.worktree), config.Nowhere)
        _ = config.write(self.worktree, self.config())
        source = self.source.read_bytes()
        self.assertEqual(config.place(self.unit, self.worktree), config.InWorktree(self.source))
        _ = config.park(self.unit, self.worktree, self.retirement())
        self.assertEqual(self.source.read_bytes(), source)
        facts = cast(dict[str, object], tomllib.loads(self.parked.read_text())["retired"])
        self.assertEqual(facts, {"date": "2026-10-31", "session": "session-before-retire",
                                 "branch": "feature/worker"})
        self.assertEqual(config.place(self.unit, self.worktree), config.Both(self.source, self.parked, True))
        self.source.unlink()
        self.assertEqual(config.place(self.unit, self.worktree), config.Parked(self.parked))
        parked = self.parked.read_bytes(), self.parked.stat().st_mtime_ns
        config.restore(self.unit, self.worktree)
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertEqual((self.parked.read_bytes(), self.parked.stat().st_mtime_ns), parked)
        self.assertNotIn("retired", tomllib.loads(self.source.read_text()))
        config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        before = self.file_versions()
        config.discard_parked(self.unit)
        config.restore(self.unit, self.worktree)
        self.assertEqual(self.file_versions(), before)

    def test_legacy_retirement_folder_is_ignored_and_not_written(self) -> None:
        _ = config.write(self.worktree, self.config())
        _ = config.park(self.unit, self.worktree, self.retirement())
        legacy = self.parked.read_text().replace('[retired]', '[retired]\nfolder = "/old/unit"')
        _ = self.parked.write_text(legacy)
        saved = config.read_parked(self.unit)
        self.assertEqual(saved, replace(self.config(), retired=self.retirement()))
        assert isinstance(saved, config.UnitConfig)
        _ = config.write_parked(self.unit, saved)
        self.assertNotIn("folder =", self.parked.read_text())
        before = self.file_versions()
        _ = config.write_parked(self.unit, saved)
        self.assertEqual(self.file_versions(), before)

    def test_park_after_worktree_removal_uses_verified_retirement_and_changes_nothing(self) -> None:
        self.park_config()
        self.source.unlink()
        before = self.file_versions()
        _ = config.park(self.unit, self.worktree, self.retirement())
        self.assertEqual(self.file_versions(), before)
        with self.assertRaises(config.ConfigRefused):
            _ = config.park(self.unit, self.worktree, replace(self.retirement(), session_id="another-session"))
        self.assertEqual(self.file_versions(), before)

    def test_missing_copies_refuse_park_and_restore_without_creating_evidence(self) -> None:
        for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                          lambda: config.restore(self.unit, self.worktree)):
            with self.assertRaises(config.ConfigRefused):
                _ = operation()
            self.assertFalse(self.source.exists())
            self.assertFalse(self.parked.exists())

    def test_malformed_parked_evidence_is_never_overwritten_or_used_for_restore(self) -> None:
        self.park_config()
        _ = self.parked.write_text('[retired]\ndate="unfinished"\n')
        before = self.file_versions()
        for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                          lambda: config.restore(self.unit, self.worktree)):
            with self.assertRaises(config.ConfigRefused) as caught:
                _ = operation()
            self.assertIn(str(self.parked), str(caught.exception))
            self.assertEqual(self.file_versions(), before)

    def test_publication_is_read_back_before_park_or_restore_reports_success(self) -> None:
        _ = config.write(self.worktree, self.config())
        real_replace = os.replace
        for destination in (self.parked, self.source):
            with self.subTest(destination=destination):
                if destination == self.source:
                    self.parked.unlink(missing_ok=True)
                    _ = config.park(self.unit, self.worktree, self.retirement())
                    self.source.unlink()

                def corrupt_publication(temporary: Path, path: Path) -> None:
                    if path == destination:
                        _ = temporary.write_text(temporary.read_text().replace("8123", "8126"))
                    real_replace(temporary, path)

                with patch.object(os, "replace", side_effect=corrupt_publication):
                    with self.assertRaises(config.ConfigRefused) as caught:
                        if destination == self.parked:
                            _ = config.park(self.unit, self.worktree, self.retirement())
                        else:
                            config.restore(self.unit, self.worktree)
                self.assertIn(str(destination), str(caught.exception))
                retained = self.source if destination == self.parked else self.parked
                self.assertEqual(tomllib.loads(retained.read_text())["port"], 8123)

    def test_different_copies_refuse_both_operations_and_name_paths_and_first_key(self) -> None:
        self.park_config()
        for key, different in (
            ("plan", replace(self.config(), plan=config.StandbyPlan(), port=config.NoAppPort())),
            ("port", replace(self.config(), port=config.NoAppPort(), responsibilities=())),
            ("responsibilities", replace(self.config(), responsibilities=())),
        ):
            _ = config.write(self.worktree, different)
            self.assertEqual(config.place(self.unit, self.worktree), config.Both(self.source, self.parked, False))
            before = self.file_versions()
            for operation in (lambda: config.park(self.unit, self.worktree, self.retirement()),
                              lambda: config.restore(self.unit, self.worktree)):
                with self.subTest(key=key), self.assertRaises(config.ConfigRefused) as caught:
                    _ = operation()
                message = str(caught.exception)
                self.assertIn(str(self.source), message)
                self.assertIn(str(self.parked), message)
                self.assertIn(key, message)
                self.assertEqual(len(message.splitlines()), 1)
                self.assertEqual(self.file_versions(), before)

    def test_different_retirement_facts_refuse_without_replacing_the_saved_session(self) -> None:
        self.park_config()
        before = self.file_versions()
        with self.assertRaisesRegex(config.ConfigRefused, "retired"):
            _ = config.park(self.unit, self.worktree, replace(self.retirement(), branch=config.Branch("other")))
        self.assertEqual(self.file_versions(), before)

    def test_matching_copies_make_park_and_restore_reruns_noops(self) -> None:
        self.park_config()
        before = self.file_versions()
        _ = config.park(self.unit, self.worktree, self.retirement())
        config.restore(self.unit, self.worktree)
        self.assertEqual(self.file_versions(), before)

    def test_park_interrupted_before_or_after_publication_recovers_without_removing_the_source(self) -> None:
        _ = config.write(self.worktree, self.config())
        source = self.source.read_bytes()
        real_replace = os.replace
        for published in (False, True):
            with self.subTest(published=published):
                self.parked.unlink(missing_ok=True)

                def interrupt(temporary: Path, destination: Path) -> None:
                    if destination == self.parked:
                        if published:
                            real_replace(temporary, destination)
                        raise OSError("stopped at parked publication")
                    real_replace(temporary, destination)

                with patch.object(os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "stopped at parked publication"):
                        _ = config.park(self.unit, self.worktree, self.retirement())
                self.assertEqual(self.source.read_bytes(), source)
                self.assertEqual(self.parked.exists(), published)
                if published:
                    self.assertEqual(tomllib.loads(self.parked.read_text())["retired"]["session"],
                                     "session-before-retire")
                _ = config.park(self.unit, self.worktree, self.retirement())
                complete = self.file_versions()
                _ = config.park(self.unit, self.worktree, self.retirement())
                self.assertEqual(self.file_versions(), complete)
                self.assertEqual(list(self.parked.parent.glob(f".{self.parked.name}.*")), [])

    def test_restore_interrupted_before_or_after_publication_keeps_the_parked_facts_and_recovers(self) -> None:
        self.park_config()
        saved = self.parked.read_bytes(), self.parked.stat().st_mtime_ns
        real_replace = os.replace
        for published in (False, True):
            with self.subTest(published=published):
                self.source.unlink(missing_ok=True)

                def interrupt(temporary: Path, destination: Path) -> None:
                    if destination == self.source:
                        if published:
                            real_replace(temporary, destination)
                        raise OSError("stopped at worktree publication")
                    real_replace(temporary, destination)

                with patch.object(os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "stopped at worktree publication"):
                        config.restore(self.unit, self.worktree)
                self.assertEqual((self.parked.read_bytes(), self.parked.stat().st_mtime_ns), saved)
                self.assertEqual(self.source.exists(), published)
                if published:
                    self.assertEqual(config.read(self.worktree), self.config())
                config.restore(self.unit, self.worktree)
                complete = self.file_versions()
                config.restore(self.unit, self.worktree)
                self.assertEqual(self.file_versions(), complete)

    def test_discard_interrupted_before_unlink_preserves_facts_and_retry_removes_only_parked_file(self) -> None:
        self.park_config()
        before = self.file_versions()
        real_unlink = Path.unlink

        def interrupt(path: Path, missing_ok: bool = False) -> None:
            if path == self.parked:
                raise PermissionError("stopped before parked unlink")
            real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", interrupt):
            with self.assertRaisesRegex(OSError, "stopped before parked unlink"):
                config.discard_parked(self.unit)
        self.assertEqual(self.file_versions(), before)
        config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        self.assertEqual(self.source.read_bytes(), before[self.source][0])
        complete = self.file_versions()
        config.discard_parked(self.unit)
        self.assertEqual(self.file_versions(), complete)

    def test_discard_interrupted_after_unlink_leaves_restored_config_and_safe_rerun(self) -> None:
        self.park_config()
        source = self.source.read_bytes(), self.source.stat().st_mtime_ns
        with patch.object(atomic_file, "sync_folder", side_effect=OSError("stopped after parked unlink")):
            with self.assertRaisesRegex(OSError, "stopped after parked unlink"):
                config.discard_parked(self.unit)
        self.assertFalse(self.parked.exists())
        config.discard_parked(self.unit)
        config.restore(self.unit, self.worktree)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), source)

    def test_interrupted_replacement_keeps_the_old_whole_config_until_retry(self) -> None:
        _ = config.write(self.worktree, self.config())
        before = self.file_versions()
        updated = replace(self.config(), port=config.AppPort(8125))
        with patch.object(os, "replace", side_effect=OSError("stopped before replace")):
            with self.assertRaisesRegex(OSError, "stopped before replace"):
                _ = config.write(self.worktree, updated)
        self.assertEqual(self.file_versions(), before)
        _ = config.write(self.worktree, updated)
        self.assertEqual(config.read(self.worktree), updated)
        self.assertEqual(list(self.worktree.glob("..unit.toml.*")), [])

    def test_read_removes_an_abandoned_temporary_file_and_keeps_the_last_complete_config(self) -> None:
        _ = config.write(self.worktree, self.config())
        dead = self.source.with_name(f".{self.source.name}.999999999.abandoned.tmp")
        _ = dead.write_text('plan="unfinished')
        self.assertEqual(config.read(self.worktree), self.config())
        self.assertFalse(dead.exists())

    def test_tracked_or_unignored_worktree_file_cannot_be_overwritten_by_write_or_restore(self) -> None:
        self.park_config()
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    _ = self.git("add", "-f", ".unit.toml")
                else:
                    _ = (self.worktree / ".gitignore").write_text("!/.unit.toml\n")
                before = self.file_versions()
                for operation in (lambda: config.write(self.worktree, self.config()),
                                  lambda: config.restore(self.unit, self.worktree)):
                    with self.assertRaises((ExcludeRefused, config.ConfigRefused)):
                        _ = operation()
                    self.assertEqual(self.file_versions(), before)

    def test_restore_refuses_unsafe_git_rules_before_publishing_a_missing_worktree_copy(self) -> None:
        self.park_config()
        self.source.unlink()
        _ = (self.worktree / ".gitignore").write_text("!/.unit.toml\n")
        saved = self.parked.read_bytes()
        with self.assertRaises((ExcludeRefused, config.ConfigRefused)):
            config.restore(self.unit, self.worktree)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.parked.read_bytes(), saved)
