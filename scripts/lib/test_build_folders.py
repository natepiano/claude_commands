"""Build folder allocation preserves parent, ownership, and cleanup rules."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
import os
import json
import shutil
import signal
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import build_folders as folders, conf, copy_record, scratch, unit_runs
from scripts.lint.memory_test_support import config_environment


class BuildFolderTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(config), "TMPDIR": str(self.root), copy_record.ROOT_ENV: str(self.root / "scratch"),
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

    def test_tmpfs_parent_uses_only_inherited_tmpfs_paths(self) -> None:
        for value, expected in (("/dev/shm/outer/module", Path("/dev/shm/outer/module")),
                                ("/dev/shm-other", Path("/dev/shm"))):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TMPDIR": value}):
                self.assertEqual(folders.tmpfs_temporary_parent(), expected)

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
        with tempfile.TemporaryDirectory(dir=folders.tmpfs_temporary_parent()) as temporary:
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

    def test_cancellation_during_allocation_or_removal_keeps_no_folder(self) -> None:
        def interrupted(number: int, frame: object) -> None:
            del frame
            raise KeyboardInterrupt(signal.Signals(number).name)

        for stage in ("allocation", "removal"):
            with self.subTest(stage=stage):
                paths: list[Path] = []
                allocate = folders.create_build_folder
                remove = shutil.rmtree
                cancel = threading.Event()
                sent = threading.Event()
                release = threading.Event()

                def send_from_thread() -> None:
                    if cancel.wait(10) and not release.is_set():
                        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
                        sent.set()
                        _ = release.wait(10)

                worker = threading.Thread(target=send_from_thread)
                worker.start()

                def allocate_then_cancel(request: folders.BuildFolderRequest | folders.BuildFolderSpec) -> Path:
                    path = allocate(request)
                    paths.append(path)
                    if stage == "allocation":
                        os.kill(os.getpid(), signal.SIGTERM)
                    return path

                def cancel_then_remove(path: str, *, dir_fd: int,
                                       onexc: Callable[[Callable[..., object], str, BaseException], object]) -> None:
                    if stage == "removal":
                        cancel.set()
                        self.assertTrue(sent.wait(10), "the worker did not send SIGTERM")
                    remove(path, dir_fd=dir_fd, onexc=onexc)

                previous = signal.signal(signal.SIGTERM, interrupted)
                try:
                    with (mock.patch.object(folders, "create_build_folder", side_effect=allocate_then_cancel),
                          mock.patch.object(shutil, "rmtree", side_effect=cancel_then_remove),
                          self.assertRaisesRegex(KeyboardInterrupt, "SIGTERM")):
                        with folders.temporary_build_folder(folders.UniqueBuildFolder("gate-proof-")):
                            pass
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(paths[0].exists())
                finally:
                    release.set()
                    cancel.set()
                    worker.join(timeout=10)
                    _ = signal.signal(signal.SIGTERM, previous)

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

    def test_context_removes_nonempty_mode_zero_folders(self) -> None:
        for restrict_root in (False, True):
            with self.subTest(restrict_root=restrict_root):
                path = self.root
                nested = self.root
                try:
                    with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as path:
                        nested = path / "private-output"
                        nested.mkdir()
                        _ = (nested / "output").write_text("compiled output")
                        nested.chmod(0)
                        if restrict_root:
                            path.chmod(0)
                finally:
                    if path != self.root and path.exists():
                        path.chmod(0o700)
                        if nested.exists():
                            nested.chmod(0o700)
                self.assertNotEqual(path, self.root)
                self.assertFalse(path.exists())

    def test_context_removes_dangling_symlinks_from_read_only_folders(self) -> None:
        path = self.root
        locked = self.root
        try:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as path:
                locked = path / "locked"
                locked.mkdir()
                (locked / "link").symlink_to("missing")
                locked.chmod(0o500)
        finally:
            if path != self.root and path.exists():
                locked.chmod(0o700)
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

    def test_context_keeps_a_replacement_folder_after_the_stage_moves(self) -> None:
        published = self.root / "published"
        replacement = self.root / "unrelated"
        replacement.mkdir()
        _ = (replacement / "keep").write_text("unrelated output")
        with folders.temporary_build_folder(folders.UniqueBuildFolder("stage-")) as stage:
            _ = (stage / "output").write_text("published output")
            _ = stage.rename(published)
            _ = replacement.rename(stage)
        self.assertEqual((stage / "keep").read_text(), "unrelated output")
        self.assertEqual((published / "output").read_text(), "published output")

    def test_allocation_refuses_a_repository_parent_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        kinds: tuple[folders.BuildFolderKind, ...] = ("temporary", "standing")
        for kind in kinds:
            with self.subTest(kind=kind):
                parent = self.root / kind
                parent.mkdir()
                (parent / "repo").symlink_to(outside, target_is_directory=True)
                spec = folders.BuildFolderSpec(kind=kind, parent=parent, suffix_length=3,
                                               repository="repo", owner="unit", purpose="proof", suffix="abc")
                with self.assertRaises(OSError):
                    _ = folders.create_build_folder(spec)
                self.assertEqual(list(outside.iterdir()), [])

    def test_context_cleans_its_parent_when_the_parent_path_is_replaced(self) -> None:
        parent = self.root / "parent"
        parent.mkdir()
        moved = self.root / "moved"
        outside = self.root / "outside"
        outside.mkdir()
        request = folders.UniqueBuildFolder("stage-", folders.ExplicitBuildParent(parent))
        with folders.temporary_build_folder(request) as stage:
            _ = parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            replacement = outside / stage.name
            replacement.mkdir()
            _ = (replacement / "keep").write_text("unrelated output")
        self.assertEqual((replacement / "keep").read_text(), "unrelated output")
        self.assertFalse((moved / stage.name).exists())

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


    def test_approved_patterns_share_one_naming_function(self) -> None:
        cases: list[tuple[folders.BuildFolderKind, str]] = [
            ("temporary", "hana-key/unit-proof-abcdef"), ("gate", "proof-abcdef"),
            ("standing", "hana-key/unit-gate"), ("editable", "hana-key-unit/proof"),
            ("mirror", "hana-key/unit-s3"), ("target", "proof"), ("shell-cache", "hana-key"),
        ]
        for kind, expected in cases:
            with self.subTest(kind=kind):
                spec = folders.BuildFolderSpec(kind=kind, parent=self.root, suffix_length=6,
                                                repository="hana-key", owner="unit", purpose="proof", slot=3,
                                                suffix="abcdef" if kind in ("temporary", "gate") else "")
                self.assertEqual(folders.build_folder_path(spec), self.root / expected)
        with mock.patch.object(folders, "build_folder_path", wraps=folders.build_folder_path) as naming:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("proof-")):
                pass
        naming.assert_called_once()

    def test_collision_retries_the_same_name_rule(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=2,
                                       repository="repo", owner="unit", purpose="proof")
        with mock.patch("secrets.choice", side_effect=list("aabb")):
            first = folders.create_build_folder(spec)
        with mock.patch("secrets.choice", side_effect=list("aabb")):
            second = folders.create_build_folder(spec)
        self.assertEqual(first.name, "unit-proof-aa")
        self.assertEqual(second.name, "unit-proof-bb")

    def test_nonvisible_scratch_target_uses_the_shared_name_rule(self) -> None:
        with (mock.patch.object(scratch, "validate_phase_task"),
              mock.patch.object(scratch, "build_folder_path", wraps=folders.build_folder_path) as naming):
            path = scratch.target("gate:proof", "tree", False)
        naming.assert_called_once()
        self.assertEqual(path.parent, copy_record.root())
        self.assertRegex(path.name, r"^gate-proof-tree-[a-z0-9]{6}$")

    def test_configuration_helper_supplies_missing_files_and_preserves_overrides(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        original = config.read_text()
        config.unlink()
        environment = config_environment(config.parent)
        self.assertEqual(environment, {"CONF_DIR": str(config.parent)})
        sources = Path(__file__).resolve().parents[2] / "config"
        for source in sources.glob("*.conf"):
            if source.name != "build_admission.conf":
                self.assertEqual((config.parent / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(folders.FOLDER_PREFIXES["admit-test"], "admit-test-")
        with folders.temporary_build_folder(folders.UniqueBuildFolder("probe-", folders.SystemTemporaryParent())) as path:
            self.assertEqual(path.parent, Path("/tmp"))
            self.assertTrue(path.is_dir())
        self.assertFalse(path.exists())
        _ = config.write_text(original.replace("prefixes.admit-test=admit-test-", "prefixes.admit-test=private-"))
        _ = config_environment(config.parent)
        self.assertEqual(folders.FOLDER_PREFIXES["admit-test"], "private-")

    def test_repository_key_keeps_the_fixed_path_name(self) -> None:
        self.assertEqual(folders.repository_key(Path("/fixture/hana")), "hana-e4417bba")
        self.assertEqual(folders.repository_key_for("hana", "/fixture/hana"), "hana-e4417bba")
        self.assertEqual(folders.repository_key_for("hana space%", "/fixture/hana"), "hana space%-e4417bba")

    def test_equal_repository_basenames_have_distinct_keys(self) -> None:
        first = self.root / "first" / "hana"
        second = self.root / "second" / "hana"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(first, target_is_directory=True)
        self.assertNotEqual(folders.repository_key(first), folders.repository_key(second))
        self.assertEqual(folders.repository_key(first), folders.repository_key(alias))

    def test_private_config_without_folder_rules_uses_repository_without_git(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        original = config.read_text()
        config.unlink()
        with mock.patch("scripts.lib.conf.subprocess.run", side_effect=AssertionError("Git must not run")):
            self.assertEqual(folders.FOLDER_PREFIXES["verify-hold"], "verify-hold.{owner}.")
            path = folders.create_build_folder(folders.UniqueBuildFolder("private-"))
        self.assertTrue(path.is_dir())
        self.assertFalse(config.exists())
        _ = config.write_text(original.replace("prefixes.verify-hold=verify-hold.{owner}.",
                                               "prefixes.verify-hold=private.{owner}."))
        self.assertEqual(folders.FOLDER_PREFIXES["verify-hold"], "private.{owner}.")

    def test_folder_rules_keep_the_live_config_reader_without_a_private_override(self) -> None:
        settings = conf.read(Path(__file__).resolve().parents[2] / "config" / "build_folders.conf")
        with mock.patch.dict(os.environ):
            _ = os.environ.pop("CONF_DIR", None)
            with mock.patch.object(conf, "read", return_value=settings) as read:
                self.assertTrue(folders.FOLDER_PREFIXES["verify-hold"])
            read.assert_called_once_with("build_folders")

    def test_naming_reads_changed_configuration_at_use(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        _ = config.write_text(config.read_text().replace("patterns.temporary={repository}/{owner}-{purpose}-{suffix}",
                                                       "patterns.temporary={repository}/{purpose}-{owner}-{suffix}"))
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=3,
                                       repository="repo", owner="unit", purpose="proof", suffix="xyz")
        self.assertEqual(folders.build_folder_path(spec), self.root / "repo" / "proof-unit-xyz")

    def test_run_removes_work_on_success_and_failure_and_records_owner(self) -> None:
        probe = """import json, os, pathlib, sys
path = pathlib.Path.cwd()
assert str(path) == os.environ['BUILD_FOLDER']
marker = json.loads((path / '.build-folder-owner.json').read_text())
assert marker['pid'] == os.getppid()
assert marker['start_time'] == pathlib.Path('/proc/%s/stat' % marker['pid']).read_text().rsplit(')',1)[1].split()[19]
assert marker['boot_id'] == pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
print(json.dumps({'path': str(path), 'owner': marker['owner']}))
sys.exit(int(sys.argv[1]))
"""
        for status in (0, 7):
            with self.subTest(status=status):
                result = self.command("run", "--owner", "unit", "--name", "proof", "--", sys.executable,
                                      "-c", probe, str(status))
                self.assertEqual(result.returncode, status, result.stderr)
                record = cast(dict[str, str], json.loads(result.stdout))
                self.assertEqual(record["owner"], "unit")
                self.assertFalse(Path(record["path"]).exists())

    def test_run_stops_and_reaps_an_escaped_descendant(self) -> None:
        probe = """import os, pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import os, signal; os.setsid(); print(os.getpid(), flush=True); signal.pause()'], stdout=subprocess.PIPE, text=True)
assert child.stdout is not None
print(str(pathlib.Path.cwd()), child.stdout.readline().strip(), flush=True)
os._exit(0)
"""
        result = self.command("run", "--owner", "unit", "--name", "proof", "--", sys.executable, "-c", probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        path, child_pid = result.stdout.split()
        self.assertFalse(Path(path).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid), 0)

    def test_run_removes_work_after_each_handled_signal(self) -> None:
        probe = "import os, pathlib, signal; print(str(pathlib.Path.cwd()), os.getpid(), flush=True); signal.pause()"
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=number):
                process: subprocess.Popen[str] = subprocess.Popen([sys.executable, str(Path(folders.__file__)), "run", "--owner", "unit",
                                            "--name", "proof", "--", sys.executable, "-c", probe],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    assert process.stdout is not None
                    path, child_pid = cast(str, process.stdout.readline()).split()
                    process.send_signal(number)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + number, stdout + stderr)
                    self.assertFalse(Path(path).exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(child_pid), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=10)


if __name__ == "__main__":
    _ = unittest.main()
