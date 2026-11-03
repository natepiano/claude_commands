"""Build folder allocation preserves parent, ownership, and cleanup rules."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import build_folders as folders, conf, copy_record, scratch, unit_runs


class BuildFolderTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "TMPDIR": str(self.root), copy_record.ROOT_ENV: str(self.root / "scratch"),
            copy_record.COPIES_ENV: str(self.root / "copies"),
            scratch.HOLDS_ENV: str(self.root / "holds.json"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
        })))

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(folders.__file__)), *arguments],
                              text=True, capture_output=True, timeout=10)

    def test_import_does_not_load_cli_or_owner_modules(self) -> None:
        probe = """import runpy, sys
runpy.run_path(sys.argv[1])
heavy = {'argparse', 'scripts.lib.unit_runs', 'scripts.lib.copy_record',
         'scripts.lib.scratch', 'herdr'}
loaded = sorted(heavy.intersection(sys.modules))
assert not loaded, loaded
"""
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(Path(folders.__file__))],
                                cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_inherited_parent_changes_between_allocations(self) -> None:
        first = folders.create_build_folder(folders.UniqueBuildFolder("merge-gate-"))
        other = self.root / "inherited parent with spaces"
        other.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(other)}):
            second = folders.create_build_folder(folders.UniqueBuildFolder("merge-gate-"))
        self.assertEqual(first.parent, self.root)
        self.assertEqual(second.parent, other)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue(first.name.startswith("merge-gate-"))
        self.assertTrue(second.name.startswith("merge-gate-"))

    def test_explicit_parent_overrides_tmpdir_and_keeps_prefix_and_permissions(self) -> None:
        parent = self.root / "record parent with spaces"
        parent.mkdir()
        path = folders.create_build_folder(folders.UniqueBuildFolder(
            "bump-gate-", folders.ExplicitBuildParent(parent)))
        self.assertEqual(path.parent, parent)
        self.assertTrue(path.name.startswith("bump-gate-"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertFalse((path / copy_record.RECORD).exists())

    def test_concurrent_unique_allocations_never_share_an_output_folder(self) -> None:
        def allocate(index: int) -> Path:
            path = folders.create_build_folder(folders.UniqueBuildFolder("promotion-index-"))
            _ = (path / "owner").write_text(str(index))
            return path

        with ThreadPoolExecutor(max_workers=8) as workers:
            jobs = [workers.submit(allocate, index) for index in range(32)]
            paths = [job.result(timeout=10) for job in jobs]
        self.assertEqual(len(set(paths)), 32)
        for index, path in enumerate(paths):
            self.assertEqual((path / "owner").read_text(), str(index))
            self.assertEqual(path.parent, self.root)

    def test_stable_owner_reuses_contents_and_existing_permissions(self) -> None:
        request = folders.StableOwnerFolder("session-123", folders.ExplicitBuildParent(self.root), mode=0o700)
        first = folders.create_build_folder(request)
        _ = (first / "live.sock.record").write_text("keep")
        first.chmod(0o750)
        with ThreadPoolExecutor(max_workers=4) as workers:
            jobs = [workers.submit(folders.create_build_folder, request) for _ in range(8)]
            paths = [job.result(timeout=10) for job in jobs]
        self.assertEqual(paths, [first] * 8)
        self.assertEqual((first / "live.sock.record").read_text(), "keep")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o750)

    def test_stable_owner_file_collision_keeps_the_existing_file(self) -> None:
        occupied = self.root / "owner"
        _ = occupied.write_text("live owner record")
        with self.assertRaises(FileExistsError):
            _ = folders.create_build_folder(folders.StableOwnerFolder(
                occupied.name, folders.ExplicitBuildParent(self.root)))
        self.assertEqual(occupied.read_text(), "live owner record")

    def test_explicit_parent_refuses_missing_or_unwritable_storage_without_fallback(self) -> None:
        missing = self.root / "missing memory filesystem"
        with self.assertRaises(OSError):
            _ = folders.create_build_folder(folders.UniqueBuildFolder(
                "merge-gate-", folders.ExplicitBuildParent(missing)))
        self.assertFalse(missing.exists())
        self.root.chmod(0o500)
        try:
            with self.assertRaises(PermissionError):
                _ = folders.create_build_folder(folders.UniqueBuildFolder(
                    "merge-gate-", folders.ExplicitBuildParent(self.root)))
        finally:
            self.root.chmod(0o700)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK),
                         "the memory filesystem is unavailable")
    def test_explicit_parent_allocates_under_a_private_dev_shm_parent(self) -> None:
        with tempfile.TemporaryDirectory(dir="/dev/shm") as temporary:
            parent = Path(temporary)
            with folders.temporary_build_folder(folders.UniqueBuildFolder(
                    "merge-gate-", folders.ExplicitBuildParent(parent))) as name:
                path = Path(name)
                self.assertEqual(path.parent, parent)
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_dev, parent.stat().st_dev)
            self.assertFalse(path.exists())

    def test_context_removes_build_output_after_completion_and_interruption(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                path = self.root
                try:
                    with folders.temporary_build_folder(folders.UniqueBuildFolder("queue-place-")) as name:
                        path = Path(name)
                        _ = (path / "module.log").write_text("output")
                        self.assertTrue(path.is_dir())
                        if interrupted:
                            raise KeyboardInterrupt("injected build interruption")
                except KeyboardInterrupt:
                    if not interrupted:
                        raise
                self.assertNotEqual(path, self.root)
                self.assertFalse(path.exists())

    def test_authority_readers_follow_current_scratch_copy_and_unit_roots(self) -> None:
        for index in range(2):
            scratch_root = self.root / f"scratch-{index}"
            copies_root = self.root / f"copies-{index}"
            current_run = self.root / f"unit-{index}"
            former_run = self.root / f"delegate-{index}"
            with (mock.patch.dict(os.environ, {copy_record.ROOT_ENV: str(scratch_root),
                                               copy_record.COPIES_ENV: str(copies_root)}),
                  mock.patch.object(unit_runs, "run_roots", return_value=(current_run, former_run))):
                parents = (folders.ExplicitBuildParent(copy_record.root()),
                           folders.ExplicitBuildParent(copy_record.copies_root()), folders.UnitRunParent())
                for parent, expected in zip(parents, (scratch_root, copies_root, current_run), strict=True):
                    path = folders.create_build_folder(folders.StableOwnerFolder("owner", parent))
                    self.assertEqual(path, expected / "owner")
                    self.assertTrue(path.is_dir())
                self.assertFalse(former_run.exists())

    def test_context_removes_nonempty_read_only_subfolders(self) -> None:
        path = self.root
        try:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as name:
                path = Path(name)
                nested = path / "read-only-copy"
                nested.mkdir()
                _ = (nested / "output").write_text("compiled output")
                nested.chmod(0o500)
                self.assertTrue((nested / "output").is_file())
        finally:
            if path.exists() and path != self.root:
                for nested in path.iterdir():
                    if nested.is_dir():
                        nested.chmod(0o700)
        self.assertNotEqual(path, self.root)
        self.assertFalse(path.exists())

    def test_context_keeps_published_output_when_the_stage_moves(self) -> None:
        destination = self.root / "published shell inputs"
        with folders.temporary_build_folder(folders.UniqueBuildFolder(".shell-stage-")) as name:
            stage = Path(name)
            _ = (stage / "exports.sh").write_text("export FIXTURE=1\n")
            os.replace(stage, destination)
        self.assertFalse(stage.exists())
        self.assertEqual((destination / "exports.sh").read_text(), "export FIXTURE=1\n")

    def test_registered_task_uses_scratch_naming_record_and_task_cleanup(self) -> None:
        settings = conf.read("scratch")
        with mock.patch.object(scratch, "target", wraps=scratch.target) as naming:
            path = scratch.new("gate:folder-test", "checkout", False, scratch_settings=settings)
        naming.assert_called_once_with("gate:folder-test", "checkout", False, scratch_settings=settings)
        self.assertEqual(path.parent, copy_record.root())
        self.assertTrue(path.name.startswith("gate-folder-test-checkout-"))
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:folder-test")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual(scratch.end("gate:folder-test"), [path])
        self.assertFalse(path.exists())

    def test_cli_preserves_spaces_existing_permissions_and_the_one_path_output(self) -> None:
        parent = self.root / "shell parent"
        parent.mkdir()
        unique = self.command("unique", "--prefix", "verify-tree.", "--parent", str(parent))
        self.assertEqual(unique.returncode, 0, unique.stderr)
        path = Path(unique.stdout.strip())
        self.assertEqual(path.parent, parent)
        self.assertTrue(path.is_dir())
        self.assertTrue(path.name.startswith("verify-tree."))
        self.assertEqual(unique.stdout, f"{path}\n")
        owner = parent / "run tmp"
        owner.mkdir(mode=0o700)
        stable = self.command("stable", "--name", "run tmp", "--parent", str(parent))
        self.assertEqual(stable.returncode, 0, stable.stderr)
        self.assertEqual(stable.stdout, f"{owner}\n")
        self.assertEqual(stat.S_IMODE(owner.stat().st_mode), 0o700)

    def test_cli_whole_path_and_run_temp_preserve_existing_output(self) -> None:
        whole = self.root / "inherited parent" / "run tmp"
        made = self.command("stable", "--path", str(whole))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(made.stdout, f"{whole}\n")
        _ = (whole / "output").write_text("keep whole-path output")
        repeated = self.command("stable", "--path", str(whole))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((whole / "output").read_text(), "keep whole-path output")
        run = self.root / "unit run"
        named = self.command("run-temp-path", str(run))
        self.assertEqual(named.returncode, 0, named.stderr)
        allocated = self.command("stable", "--run-temp", str(run))
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        self.assertEqual(allocated.stdout, named.stdout)
        self.assertEqual(Path(allocated.stdout.strip()), unit_runs.run_temp_path(run))
        self.assertTrue(Path(allocated.stdout.strip()).is_dir())

    def test_cli_missing_parent_prints_no_path_and_creates_no_fallback(self) -> None:
        result = self.command("unique", "--prefix", "module-", "--parent", str(self.root / "missing"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(str(self.root / "missing"), result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_task_uses_the_existing_registry_and_task_end(self) -> None:
        result = subprocess.run([sys.executable, str(Path(scratch.__file__)), "new",
                                 "--task", "gate:shell-folder", "--name", "checkout"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(path.parent, copy_record.root())
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:shell-folder")
        self.assertEqual(scratch.end("gate:shell-folder"), [path])
        self.assertFalse(path.exists())

    def test_names_cannot_escape_the_explicit_parent(self) -> None:
        for name in ("..", "../escape", "/absolute", "bad\x00name"):
            for request in (folders.UniqueBuildFolder(name, folders.ExplicitBuildParent(self.root)),
                            folders.StableOwnerFolder(name, folders.ExplicitBuildParent(self.root))):
                with self.subTest(name=name, request=type(request).__name__), self.assertRaises(ValueError):
                    _ = folders.create_build_folder(request)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
