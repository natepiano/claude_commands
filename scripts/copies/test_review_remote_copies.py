"""Check remote copy removal, admission and recovery through their entry points."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
from typing import ClassVar, TextIO, cast, final, override
import unittest
from unittest import mock

from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.copies import copies
from scripts.copies.copies_test_support import mirror_arguments, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport, warm_claim_servers
from scripts.lint import sweep
from scripts.lib import conf, scratch
from scripts.lib import process_start as process_identity
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import offload
from scripts.mac_test import mac_test
from scripts.mac_test import floor_claims
from scripts.build_targets import target_config
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli


@final
class RemoteEntryTests(unittest.TestCase):
    source_seed: ClassVar[ImmutableGitSeed]
    interruption_root: ClassVar[Path]
    interruption_seeds: ClassVar[dict[tuple[str, str], Path]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        cls.interruption_root = root / "interruptions"
        cls.interruption_seeds = {}
        with local_remote_transport(root):
            cls.source_seed = ImmutableGitSeed.create(root, "main", "Copy Tests", "copy@example.invalid",
                                                     {"Cargo.toml": "[workspace]\n", "source.txt": "content\n"})

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.source = Path()
        self.mirror = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(local_remote_transport(self.root))
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.enterContext(warm_claim_servers())
        _ = self.enterContext(mock.patch.object(mac_test, "offload", offload))
        self.source = self.root / "checkout"
        self.source_seed.copy_to(self.source, self.root / "origin.git")
        self.mirror = self.root / "mirrors/repository"

    def mirror_record(self, owner: str = "alpha", path: Path | None = None) -> copies.RemoteMirror:
        return copies.RemoteMirror(owner, copies.SelectedRepository("repository"), "host",
                                   str(self.mirror if path is None else path), "holder-session")

    def test_idle_removal_exempts_its_lease_and_keeps_a_busy_sibling(self) -> None:
        mine = self.mirror_record()
        other = self.mirror_record("beta", self.mirror.parent / "other")
        self.assertEqual(publish_mirror(mine), 0)
        self.assertEqual(publish_mirror(other), 0)
        with copies.remote_lease(other):
            claims = floor_claims.read_claims(TEST_HANG_GUARD_SECS)
            identity = floor_claims.MirrorIdentity(other.host, other.path)
            self.assertIn(floor_claims.HeldMirror(identity, other.session), claims.mirrors)
            self.assertTrue(claims.holds(other.path, self.mirror.parent))
            self.assertFalse(claims.holds(other.path, self.mirror.parent, exemption=identity))
            copies.remove_trees("alpha")
            self.assertFalse(self.mirror.exists())
            with self.assertRaisesRegex(copies.CopyError, "other.*holder-session"):
                copies.remove_trees("beta")
        self.assertEqual(copies.owned_remote_mirrors("beta"), [other])
        self.assertTrue(Path(other.path).exists())

        checkout = copies.clone(self.source, "gate:removal-race", "task-removal")
        task_mirror = self.mirror_record(path=checkout)
        self.assertEqual(publish_mirror(task_mirror), 0)
        marker = self.root / "use-started"
        use_results: list[subprocess.CompletedProcess[str]] = []

        def use_during_removal(parent: Path) -> None:
            # Remove the old lock files before checkout, as scratch.remove does.
            for path in (floor_claims.mirror_lock_path(checkout), floor_claims.record_lock_path(checkout)):
                path.unlink()
            result = subprocess.run([sys.executable, str(copies.__file__),
                "mirror-use", "--path", str(checkout), "--owner", "alpha",
                "--repository", "repository", "--session", "late-use", "--",
                sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker),
            ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            use_results.append(result)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("busy", result.stderr)
            self.assertFalse(marker.exists())
            shutil.rmtree(parent)

        with (mock.patch.object(scratch, "save_cargo_outputs", return_value=()),
              mock.patch.object(scratch, "remove", side_effect=use_during_removal)):
            self.assertEqual(copies.mirror_remove(mirror_arguments(task_mirror, "mirror-remove")), 0)
        self.assertEqual(len(use_results), 1)
        self.assertFalse(checkout.parent.exists())
        self.assertFalse(floor_claims.mirror_parent_lock_path(checkout).exists())

        replacement_tree = self.root / "replacement-parent/checkout"
        parent_lock = floor_claims.mirror_parent_lock_path(replacement_tree)
        original_flock = fcntl.flock
        replaced = False

        def replace_before_lock(descriptor: int, operation: int) -> None:
            nonlocal replaced
            if not replaced:
                self.assertEqual(os.fstat(descriptor).st_ino, parent_lock.stat().st_ino)
                result = subprocess.run([sys.executable, "-c",
                    "from pathlib import Path; import sys; "
                    + "path=Path(sys.argv[1]); path.unlink(); path.touch()", str(parent_lock)],
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                replaced = True
            original_flock(descriptor, operation)

        with mock.patch.object(fcntl, "flock", side_effect=replace_before_lock):
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                with copies.local_mirror_lease(replacement_tree, "replaced-lease"):
                    self.fail("A replaced lease must not admit a command")
        self.assertTrue(replaced)
        self.assertFalse(replacement_tree.exists())

    def test_claim_server_exits_with_stdin_open_and_releases_admission(self) -> None:
        paths = mac_test.state_paths()
        with subprocess.Popen([sys.executable, str(Path(floor_claims.__file__))],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as reader:
            self.assertIsNotNone(reader.stdin)
            self.assertIsNotNone(reader.stdout)
            assert reader.stdin is not None and reader.stdout is not None
            self.assertTrue(select.select([reader.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
            reply = cast(object, json.loads(cast(str, reader.stdout.readline())))
            self.assertEqual(reply, {"repositories": [], "mirrors": []})
            self.assertEqual(reader.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertFalse(reader.stdin.closed)
            with paths.lock.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        def print_after_release(*values: object, flush: bool = False) -> None:
            self.assertTrue(flush)
            self.assertEqual(len(values), 1)
            with paths.lock.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        with mock.patch("builtins.print", side_effect=print_after_release) as output:
            self.assertEqual(floor_claims.serve(), 0)
        output.assert_called_once()
        args = mac_test.CliArguments()
        args.pid, args.worktree, args.what = os.getpid(), str(self.source), "second admission"
        with ThreadPoolExecutor(max_workers=1) as executor:
            claims = floor_claims.read_claims(TEST_HANG_GUARD_SECS)
            self.assertFalse(claims.repositories)
            admission = executor.submit(mac_test.command_claim, args, paths, watch_claim=False)
            self.assertEqual(admission.result(timeout=TEST_HANG_GUARD_SECS), 0)
        claim, = mac_test.read_claims(paths)
        self.assertEqual(claim.record["what"], args.what)
        self.assertEqual(claim.record["worktree"], args.worktree)

    def test_removal_keeps_a_live_claim_despite_its_lease_exemption(self) -> None:
        mirror = self.mirror_record()
        self.assertEqual(publish_mirror(mirror), 0)
        with mock.patch.object(mac_test, "state_paths", return_value=mac_test.StatePaths(self.root / "local-claims")):
            paths = mac_test.state_paths()
        start = process_identity.process_start(os.getpid())
        self.assertIsInstance(start, process_identity.Started)
        if not isinstance(start, process_identity.Started):
            self.fail("The test process has no start identity")
        paths.claims.mkdir(parents=True)
        _ = (paths.claims / "repository.json").write_text(json.dumps({
            "pid": os.getpid(), "proc_start": start.start, "what": "tests", "worktree": str(self.source),
            "since": "2026-11-05T12:00:00+00:00", "repo_name": "repository", "repo_key": "",
        }))
        with self.assertRaisesRegex(copies.CopyError, "busy"):
            copies.remove_trees("alpha")
        self.assertTrue(self.mirror.exists())

    def test_configured_symlink_parent_permits_copy_and_removal(self) -> None:
        actual = self.root / "actual"
        actual.mkdir()
        link = self.root / "linked"
        link.symlink_to(actual, target_is_directory=True)
        mirror = self.mirror_record(path=link / "repository")
        args = mirror_arguments(mirror, "mirror-receive")
        _ = args.command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('copied')",
                        str(Path(mirror.path) / "payload")]
        copies.register_remote(mirror)
        self.assertEqual(copies.mirror_run(args), 0)
        self.assertEqual((actual / "repository/payload").read_text(), "copied")
        copies.remove_trees("alpha")
        self.assertFalse((actual / "repository").exists())
        self.assertTrue(link.is_symlink())

    def test_standing_checkout_offload_records_and_removes_the_gate_owner(self) -> None:
        with copies.standing("gate-owner", self.source, copies.CommittedSource("HEAD")) as tree:
            args = offload.parser().parse_args(["run", "--repo-root", str(tree), "--call-id", "call"],
                                              namespace=offload.CliArguments())
            request = offload.request_from_arguments(args)
            self.assertEqual(request.owner, "gate-owner")
            mirror = self.mirror_record(request.owner)
            self.assertEqual(publish_mirror(mirror), 0)
        copies.remove_trees("gate-owner")
        self.assertFalse(self.mirror.exists())
        self.assertEqual(copies.owned_remote_mirrors("gate-owner"), [])

    def test_interrupted_removal_resumes_before_and_after_record_deletion(self) -> None:
        for moment in ("payload", "record"):
            with self.subTest(moment=moment):
                mirror = self.mirror_record()
                self.assertEqual(publish_mirror(mirror), 0)
                folder = self.mirror / "payload"
                folder.mkdir()
                _ = (folder / "file").write_text("data")
                args = mirror_arguments(mirror, "mirror-remove")
                remove = shutil.rmtree
                unlink = Path.unlink

                def remove_payload(path: Path) -> None:
                    remove(path)
                    raise InterruptedError("removal stopped")

                def remove_record(path: Path, missing_ok: bool = False) -> None:
                    unlink(path, missing_ok=missing_ok)
                    if path == self.mirror / ".copy-record.json":
                        raise InterruptedError("removal stopped")

                patch = (mock.patch.object(shutil, "rmtree", side_effect=remove_payload) if moment == "payload"
                         else mock.patch.object(Path, "unlink", autospec=True, side_effect=remove_record))
                with patch, self.assertRaisesRegex(InterruptedError, "removal stopped"):
                    _ = copies.mirror_remove(args)
                self.assertTrue(copies.remote_record_path(mirror).exists())
                with self.assertRaisesRegex(copies.CopyError, "another owner"):
                    _ = copies.mirror_remove(mirror_arguments(replace(mirror, owner="other"), "mirror-remove"))
                copies.remove_trees("alpha")
                self.assertFalse(self.mirror.exists())
                self.assertFalse(copies.remote_record_path(mirror).exists())

    def test_empty_owner_removes_no_remote_mirrors(self) -> None:
        mirror = self.mirror_record()
        self.assertEqual(publish_mirror(mirror), 0)
        copies.remove_trees("")
        self.assertTrue(self.mirror.exists())
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [mirror])

    def test_unit_owner_keeps_underscore_spelling_and_removes_only_its_mirrors(self) -> None:
        owner = copies.unit_owner("alpha_unit")
        self.assertEqual(owner, "alpha_unit-unit")
        mine = self.mirror_record(owner)
        other = self.mirror_record("beta-unit", self.mirror.parent / "other")
        self.assertEqual(publish_mirror(mine), 0)
        self.assertEqual(publish_mirror(other), 0)
        with copies.standing(owner, self.source, copies.CommittedSource("HEAD")) as tree:
            pass
        copies.remove_trees(owner)
        self.assertFalse(tree.exists())
        self.assertFalse(self.mirror.exists())
        self.assertEqual(copies.owned_remote_mirrors(owner), [])
        self.assertTrue(Path(other.path).exists())
        self.assertEqual(copies.owned_remote_mirrors("beta-unit"), [other])

    def remove_trees_through_cli(self, owner: str) -> None:
        """Run the real remote CLI without another SSH interpreter."""
        def remote_cli(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            command = shlex.split(arguments[-1].partition("; copies_status=")[0])[2:]
            answer = invoke_cli(copies, lambda: copies.main(command), command, os.environ, self.root,
                                combined_output=not bool(options.get("capture_output")))
            return subprocess.CompletedProcess(arguments, answer.returncode,
                                               answer.stdout + f"\nrc={answer.returncode}\n", answer.stderr)

        with mock.patch.object(copies, "subprocess", SimpleNamespace(run=remote_cli)):
            copies.remove_trees(owner)

    def save_interrupted_mirror(self, mirror: copies.RemoteMirror, key: tuple[str, str]) -> None:
        """Keep the real interrupted files as an immutable class seed."""
        seed = self.interruption_root / key[0] / key[1]
        seed.mkdir(parents=True)
        _ = shutil.copytree(Path(mirror.path), seed / "tree")
        _ = shutil.copyfile(floor_claims.removal_authority_path(Path(mirror.path)), seed / "authority")
        self.interruption_seeds[key] = seed

    def restore_interrupted_mirror(self, mirror: copies.RemoteMirror, key: tuple[str, str]) -> Path:
        """Restore private files and register this case with its real path."""
        seed = self.interruption_seeds[key]
        tree = Path(mirror.path)
        if tree.exists():
            shutil.rmtree(tree)
        _ = shutil.copytree(seed / "tree", tree)
        authority = floor_claims.removal_authority_path(tree)
        _ = shutil.copyfile(seed / "authority", authority)
        copies.register_remote(mirror)
        return authority

    def interrupt_mirror_removal(self, mirror: copies.RemoteMirror, moment: str) -> Path:
        key = (moment, "removal")
        if key in self.interruption_seeds:
            return self.restore_interrupted_mirror(mirror, key)
        self.assertEqual(publish_mirror(mirror), 0)
        tree = Path(mirror.path)
        payload = tree / "old-payload"
        payload.mkdir()
        _ = (payload / "file").write_text("old content")
        remove, unlink = shutil.rmtree, Path.unlink

        def remove_payload(path: Path) -> None:
            remove(path)
            raise InterruptedError("removal stopped")

        def remove_record(path: Path, missing_ok: bool = False) -> None:
            unlink(path, missing_ok=missing_ok)
            if path == tree / ".copy-record.json":
                raise InterruptedError("removal stopped")

        patch = (mock.patch.object(shutil, "rmtree", side_effect=remove_payload) if moment == "payload"
                 else mock.patch.object(Path, "unlink", autospec=True, side_effect=remove_record))
        with patch, self.assertRaisesRegex(InterruptedError, "removal stopped"):
            _ = copies.mirror_remove(mirror_arguments(mirror, "mirror-remove"))
        _ = (tree / "leftover").write_text("partly removed content")
        authority = tree.parent / ("." + tree.name + ".copy-removing.json")
        self.assertTrue(authority.exists())
        self.assertEqual((tree / ".copy-record.json").exists(), moment == "payload")
        self.save_interrupted_mirror(mirror, key)
        return authority

    def test_receive_refills_interrupted_removal_before_another_owner_uses_and_removes(self) -> None:
        for moment in ("payload", "record"):
            with self.subTest(moment=moment):
                original = self.mirror_record(path=self.mirror.parent / moment)
                authority = self.interrupt_mirror_removal(original, moment)
                replacement = replace(original, owner="beta", session="beta-session")
                tree = Path(original.path)
                receive = mirror_arguments(replacement, "mirror-receive")
                receive.command = [sys.executable, "-c", """
from pathlib import Path
import sys
tree = Path(sys.argv[1])
assert not (tree / 'leftover').exists()
assert not (tree / 'old-payload').exists()
assert (tree / '.copy-session').read_text() == 'beta-session\\n'
(tree / 'source.txt').write_text('complete replacement')
""", str(tree)]
                self.assertEqual(copies.mirror_run(receive), 0)
                self.assertFalse(authority.exists())
                expected_owner = "alpha"
                self.assertEqual(json.loads((tree / ".copy-record.json").read_text())["owner"], expected_owner)
                self.assertEqual(copies.owned_remote_mirrors("alpha"), [original])
                use = mirror_arguments(replacement, "mirror-use")
                use.command = [sys.executable, "-c", """
from pathlib import Path
import json, sys
tree = Path(sys.argv[1])
assert (tree / 'source.txt').read_text() == 'complete replacement'
assert json.loads((tree / '.copy-record.json').read_text())['owner'] == 'beta'
""", str(tree)]
                copies.register_remote(replacement)
                self.assertEqual(copies.mirror_run(use), 0)
                copies.remove_trees("beta")
                self.assertFalse(tree.exists())
                self.assertFalse(authority.exists())

    def test_receive_keeps_complete_replacement_with_stale_removal_authority(self) -> None:
        for moment in ("payload", "record"):
            with self.subTest(moment=moment):
                original = self.mirror_record(path=self.mirror.parent / moment)
                authority = self.interrupt_mirror_removal(original, moment)
                replacement = replace(original, owner="beta", session="beta-session")
                tree = Path(original.path)
                _ = (tree / ".copy-record.json").write_text(json.dumps({
                    "kind": "remote-mirror", "owner": "beta", "repo": "repository", "session": "replacement-session",
                }))
                source = tree / "complete-source"
                _ = source.write_text("complete replacement")
                modified = source.stat().st_mtime_ns
                receive = mirror_arguments(replacement, "mirror-receive")
                receive.command = [sys.executable, "-c", """
from pathlib import Path
import sys
assert Path(sys.argv[1]).read_text() == 'complete replacement'
""", str(source)]
                self.assertEqual(copies.mirror_run(receive), 0)
                self.assertFalse(authority.exists())
                self.assertEqual(source.stat().st_mtime_ns, modified)
                self.assertEqual(json.loads((tree / ".copy-record.json").read_text())["owner"], "beta")
                use = mirror_arguments(replacement, "mirror-use")
                use.command = receive.command
                copies.register_remote(replacement)
                self.assertEqual(copies.mirror_run(use), 0)
                copies.remove_trees("beta")
                self.assertFalse(tree.exists())

    def test_failed_admission_after_recovery_keeps_the_received_owner(self) -> None:
        for moment in ("payload", "record"):
            with self.subTest(moment=moment):
                original = self.mirror_record(path=self.mirror.parent / "slot-1")
                authority = self.interrupt_mirror_removal(original, moment)
                tree = Path(original.path)
                with self.admission(free_gib=0) as (controller, config, request):
                    def receive(current: offload.ProcessController, settings: offload.OffloadConfig,
                                selected: offload.OffloadRequest,
                                repository: offload.KnownRepository) -> offload.CopiedTree:
                        self.assertIs(current, controller)
                        self.assertEqual(settings.host, config.host)
                        mirror = offload.request_mirror(settings, selected, repository)
                        args = mirror_arguments(mirror, "mirror-receive")
                        args.command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('fresh')",
                                        str(tree / "source.txt")]
                        self.assertEqual(copies.mirror_run(args), 0)
                        self.assertFalse(authority.exists())
                        return offload.CopiedTree(offload.KnownTreeKey("tree"))

                    with mock.patch.object(offload, "copy_tree_with_key", side_effect=receive):
                        result = offload.run_offload(controller, config, request, 0)
                self.assertIsInstance(result, offload.DeclinedMacRun)
                expected_owner = "alpha"
                self.assertEqual(json.loads((tree / ".copy-record.json").read_text())["owner"], expected_owner)
                self.assertEqual(copies.owned_remote_mirrors("alpha"), [original])
                self.assertEqual(copies.owned_remote_mirrors("new-owner"), [])
                copies.register_remote(replace(original, owner=expected_owner))
                copies.remove_trees(expected_owner)

    def interrupt_receive_recovery(self, original: copies.RemoteMirror, moment: str, stage: str) -> None:
        tree = Path(original.path)
        key = (moment, stage)
        if key in self.interruption_seeds:
            _ = self.restore_interrupted_mirror(original, key)
            return
        authority = tree.parent / ("." + tree.name + ".copy-removing.json")
        receive = mirror_arguments(replace(original, owner="beta", session="beta-session"), "mirror-receive")
        clear = copies.clear_mirror_content
        unlink = Path.unlink

        def clear_content(path: Path) -> None:
            clear(path)
            if stage == "clear":
                raise InterruptedError("receive stopped")

        def write_metadata(path: Path, content: str) -> None:
            write_text_atomic(path, content)
            if (stage == "record" and path == tree / ".copy-record.json"
                    or stage == "marker" and path == tree / ".mac-test-mirror"):
                raise InterruptedError("receive stopped")

        def unlink_authority(path: Path, missing_ok: bool = False) -> None:
            if stage == "authority" and path == authority:
                raise InterruptedError("receive stopped")
            unlink(path, missing_ok=missing_ok)

        with (mock.patch.object(copies, "clear_mirror_content", side_effect=clear_content),
              mock.patch.object(copies, "write_text_atomic", side_effect=write_metadata),
              mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink_authority),
              self.assertRaisesRegex(InterruptedError, "receive stopped")):
            _ = copies.mirror_run(receive)
        self.save_interrupted_mirror(original, key)

    def test_registered_owner_removes_after_each_receive_recovery_interruption(self) -> None:
        for moment in ("payload", "record"):
            for stage in ("clear", "record", "marker", "authority"):
                with self.subTest(moment=moment, stage=stage):
                    original = self.mirror_record(path=self.mirror.parent / f"{moment}-{stage}")
                    authority = self.interrupt_mirror_removal(original, moment)
                    self.interrupt_receive_recovery(original, moment, stage)
                    self.remove_trees_through_cli("alpha")
                    self.assertFalse(Path(original.path).exists())
                    self.assertFalse(authority.exists())
                    self.assertEqual(copies.owned_remote_mirrors("alpha"), [])

    def test_repeated_receive_keeps_registered_owner_after_each_recovery_interruption(self) -> None:
        for moment in ("payload", "record"):
            for stage in ("clear", "record", "marker", "authority"):
                with self.subTest(moment=moment, stage=stage):
                    original = self.mirror_record(path=self.mirror.parent / f"{moment}-{stage}")
                    authority = self.interrupt_mirror_removal(original, moment)
                    self.interrupt_receive_recovery(original, moment, stage)
                    receive = mirror_arguments(replace(original, owner="beta"), "mirror-receive")
                    self.assertEqual(copies.mirror_run(receive), 0)
                    tree = Path(original.path)
                    record = cast(dict[str, object], json.loads((tree / ".copy-record.json").read_text()))
                    self.assertEqual(record["owner"], "alpha")
                    self.assertEqual(record["repo"], original.repository.name)
                    self.assertTrue((tree / ".mac-test-mirror").exists())
                    self.assertFalse(authority.exists())
                    self.remove_trees_through_cli("alpha")
                    self.assertFalse(tree.exists())

    def test_sweep_finds_mirror_after_each_receive_recovery_interruption(self) -> None:
        for moment in ("payload", "record"):
            for stage in ("clear", "record", "marker", "authority"):
                with self.subTest(moment=moment, stage=stage):
                    original = self.mirror_record(path=self.mirror.parent / f"{moment}-{stage}")
                    _ = self.interrupt_mirror_removal(original, moment)
                    self.interrupt_receive_recovery(original, moment, stage)
                    tree = Path(original.path)
                    with copies.local_mirror_lease(tree, "active-session", create=False):
                        with copies.mirror_sweep_leases(tree.parent) as protected:
                            self.assertIn(tree, protected.busy)

    def test_replacement_owner_removes_despite_another_owners_stale_authority(self) -> None:
        original = self.mirror_record()
        authority = self.interrupt_mirror_removal(original, "record")
        replacement = replace(original, owner="beta")
        record = self.mirror / ".copy-record.json"
        _ = record.write_text(json.dumps({"owner": "beta", "repo": replacement.repository.name}))
        with self.assertRaisesRegex(copies.CopyError, "another owner"):
            _ = copies.mirror_remove(mirror_arguments(original, "mirror-remove"))
        self.assertTrue(record.exists())
        self.assertTrue(authority.exists())
        self.assertEqual(copies.mirror_remove(mirror_arguments(replacement, "mirror-remove")), 0)
        self.assertFalse(self.mirror.exists())
        self.assertFalse(authority.exists())

    def test_registered_owner_removes_after_recovered_receive_and_failed_admission(self) -> None:
        for moment in ("payload", "record"):
            with self.subTest(moment=moment):
                original = self.mirror_record(path=self.mirror.parent / "slot-1")
                _ = self.interrupt_mirror_removal(original, moment)
                with self.admission(free_gib=0) as (controller, config, request):
                    def receive(current: offload.ProcessController, settings: offload.OffloadConfig,
                                selected: offload.OffloadRequest,
                                repository: offload.KnownRepository) -> offload.CopiedTree:
                        self.assertIs(current, controller)
                        mirror = offload.request_mirror(settings, selected, repository)
                        args = mirror_arguments(mirror, "mirror-receive")
                        self.assertEqual(copies.mirror_run(args), 0)
                        return offload.CopiedTree(offload.KnownTreeKey("tree"))

                    with mock.patch.object(offload, "copy_tree_with_key", side_effect=receive):
                        result = offload.run_offload(controller, config, request, 0)
                self.assertIsInstance(result, offload.DeclinedMacRun)
                self.assertEqual(copies.owned_remote_mirrors("alpha"), [original])
                self.assertEqual(copies.owned_remote_mirrors("new-owner"), [])
                copies.remove_trees("alpha")
                self.assertFalse(Path(original.path).exists())
                self.assertEqual(copies.owned_remote_mirrors("alpha"), [])

    def test_slot_shell_child_keeps_the_lease_after_both_parents_end(self) -> None:
        mirror = self.mirror_record()
        self.assertEqual(publish_mirror(mirror), 0)
        tool_home = self.root / "slot-tools"
        lint = tool_home / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True)
        _ = lint.write_text("#!/bin/bash\n(cd /; exec sleep 600) &\nprintf 'SHELL %s\\n' \"$$\"\nprintf 'CHILD %s\\n' \"$!\"\nwait\n")
        lint.chmod(0o755)
        exports = self.root / "exports"
        _ = exports.write_text("export HOME=" + shlex.quote(str(tool_home)) + "\n")
        program = '''
from pathlib import Path
import os, sys
from unittest import mock
from scripts.mac_test import linux_runner
root, exports = Path(sys.argv[1]), Path(sys.argv[2])
print('RUNNER', os.getpid(), flush=True)
values = dict(kinds='test', devshell='default', shell_cache='cache', shell_timeout_s='1',
              build_budget_gib='20', target_budget_gib='10', free_floor_gib='1', abandoned_session_minutes='1')
with (mock.patch.object(linux_runner, 'prepare_shell', return_value=linux_runner.ShellEnvironment('key', exports, exports)),
      mock.patch.object(linux_runner, 'permission_refused', return_value=False),
      mock.patch.object(linux_runner, 'output_free_bytes', return_value=100 * 1024**3),
      mock.patch.object(linux_runner, 'trim_build_folder', return_value=0)):
    linux_runner.execute_slot(root, values, 'repository', 'nextest', (), 'call', 'tree')
'''
        command = [sys.executable, str(Path(copies.__file__)), "mirror-use", "--path", mirror.path,
                   "--owner", mirror.owner, "--repository", mirror.repository.name,
                   "--session", mirror.session, "--", sys.executable, "-c", program, mirror.path, str(exports)]
        owner: subprocess.Popen[str] = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
        runner, shell, child = 0, 0, 0
        try:
            if owner.stdout is None:
                self.fail("The owner has no output stream")
            output = cast(TextIO, owner.stdout)
            while True:
                line = output.readline()
                if not line:
                    break
                if line.startswith("RUNNER "):
                    runner = int(line.split()[1])
                if line.startswith("SHELL "):
                    shell = int(line.split()[1])
                if line.startswith("CHILD "):
                    child = int(line.split()[1])
                    break
            self.assertGreater(runner, 0)
            self.assertGreater(child, 0)
            owner.terminate()
            _ = owner.wait(timeout=5)
            for pid in (runner, shell):
                descriptor = os.pidfd_open(pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                    self.assertTrue(select.select([descriptor], [], [], 5)[0])
                finally:
                    os.close(descriptor)
            with self.assertRaisesRegex(copies.CopyError, "holder-session"):
                _ = copies.mirror_remove(mirror_arguments(mirror, "mirror-remove"))
            self.assertTrue((self.mirror / ".copy-record.json").exists())
        finally:
            send_process_group(owner.pid, signal.SIGKILL)
            _ = owner.communicate(timeout=5)

    def test_standing_checkout_repairs_a_stale_lock_and_a_damaged_index(self) -> None:
        for damaged in (False, True):
            with self.subTest(damaged=damaged):
                with copies.standing("alpha", self.source, copies.StagedSource(copies.staged_tree(self.source))) as tree:
                    cache = (tree / "target").resolve()
                    _ = (tree / ".git/index.lock").write_text("stopped writer")
                    if damaged:
                        _ = (tree / ".git/index").write_bytes(b"broken")
                    copies.initialize_git(tree, self.source)
                    self.assertFalse((tree / ".git/index.lock").exists())
                    _ = copies.git(tree, "read-tree", "HEAD")
                    self.assertEqual((tree / "source.txt").read_text(), "content\n")
                self.assertFalse(tree.exists())
                with copies.standing("alpha", self.source, copies.StagedSource(copies.staged_tree(self.source))) as reused:
                    self.assertNotEqual(reused, tree)
                    self.assertEqual((reused / "target").resolve(), cache)
                    self.assertEqual((reused / "source.txt").read_text(), "content\n")
                    self.assertFalse((reused / ".git/index.lock").exists())

    @contextmanager
    def admission(self, *, free_gib: float = 200) -> Generator[tuple[offload.ProcessController,
            offload.OffloadConfig, offload.OffloadRequest]]:
        settings = self.root / "target.conf"
        _ = write_config_fixture(settings, "slots=2\n", template="mac_test")
        target = target_config.SshTarget("linux", "linux", settings)
        config = offload.OffloadConfig(True, offload.SshConnectionSettings("host", 1, 1),
                                      1, 1, 1, "1", {"slots": "2"})
        request = offload.OffloadRequest(self.source, "package", "call", offload.NO_RESULT_FILE, (),
                                        target="linux", owner="new-owner", holder_session="new-session")
        controller = offload.ProcessController()

        def claim(arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
            args = mac_test.parser().parse_args(list(arguments[2:]), namespace=mac_test.CliArguments())
            output = io.StringIO()
            with redirect_stdout(output):
                status = mac_test.command_claim(args, mac_test.state_paths(args.target))
            attempt.answer = offload.read_claim_answer(offload.CommandOutput(status, output.getvalue(), ""))

        def release(current: offload.ProcessController, name: str) -> None:
            self.assertIs(current, controller)
            args = mac_test.CliArguments()
            args.pid = os.getpid()
            args.target = name
            _ = mac_test.command_release(args, mac_test.state_paths(name))

        def stream(arguments: Sequence[str]) -> offload.ReportedMacStatus:
            self.assertIn("mirror-use", arguments[-1])
            records = copies.owned_remote_mirrors("new-owner")
            self.assertEqual(len(records), 1)
            args = mirror_arguments(records[0], "mirror-use")
            self.assertEqual(copies.mirror_run(args), 0)
            return offload.ReportedMacStatus(0, offload.CleanupKept())

        def mirror_path(repository: offload.KnownRepository, target: str = "mac", slot: int = 1) -> str:
            self.assertEqual(repository.name, "repository")
            self.assertEqual(target, "linux")
            return str(self.mirror.parent / f"slot-{slot}")

        with (
            mock.patch.object(offload, "mac_step_eligibility", return_value=offload.KnownRepository("repository")),
            mock.patch.object(offload, "trusted_linux_source", return_value=True),
            mock.patch.object(target_config, "configured_ssh_target", return_value=target),
            mock.patch.object(offload, "remote_mirror", side_effect=mirror_path),
            mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.RepositoryScope("repository", "key")),
            mock.patch.object(mac_test, "start_claim_watcher"),
            mock.patch.object(controller, "complete_claim", side_effect=claim),
            mock.patch.object(offload, "expected_linux_parity", return_value={}),
            mock.patch.object(offload, "copy_tracked_scripts", return_value=True),
            mock.patch.object(offload, "copy_tree_with_key", return_value=offload.CopiedTree(offload.KnownTreeKey("tree"))),
            mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(free_gib, "host")),
            mock.patch.object(offload, "check_current_leftovers", return_value=offload.LeftoversEnded()),
            mock.patch.object(offload, "remote_test_command", return_value="true"),
            mock.patch.object(offload, "release_claim", side_effect=release),
            mock.patch.object(controller, "stream_mac_run", side_effect=stream),
            mock.patch("scripts.buildlog.sync.sync_target", return_value=0),
        ):
            yield controller, config, request

    def test_admission_selects_a_leased_slot_and_keeps_the_other_owner(self) -> None:
        first = self.mirror_record("first-owner", self.mirror.parent / "slot-1")
        second = self.mirror_record("second-owner", self.mirror.parent / "slot-2")
        self.assertEqual(publish_mirror(first), 0)
        self.assertEqual(publish_mirror(second), 0)
        with self.admission() as (controller, config, request), copies.remote_lease(first):
            result = offload.run_offload(controller, config, request, 0)
        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertEqual(copies.owned_remote_mirrors("first-owner"), [first])
        self.assertEqual([record.path for record in copies.owned_remote_mirrors("new-owner")], [second.path])
        self.assertEqual(json.loads((Path(first.path) / ".copy-record.json").read_text())["owner"], "first-owner")
        self.assertEqual(json.loads((Path(second.path) / ".copy-record.json").read_text())["owner"], "new-owner")

    def test_empty_slot_values_raise_config_error_while_absent_values_use_the_missing_policy(self) -> None:
        single = target_config.MissingSlotCapacity.SINGLE_SLOT
        required = target_config.MissingSlotCapacity.REQUIRE_CONFIGURED
        self.assertEqual(target_config.slot_capacity({}, "repository", missing=single), 1)
        with self.assertRaises(target_config.ConfigError):
            _ = target_config.slot_capacity({}, "repository", missing=required)
        for values in ({"slots.repository": "", "slots": "2"}, {"slots": ""}):
            with self.subTest(values=values, behavior="capacity reader"):
                with self.assertRaises(target_config.ConfigError):
                    _ = target_config.slot_capacity(values, "repository", missing=single)
            with self.subTest(values=values, behavior="admission"):
                with self.admission() as (controller, config, request):
                    settings = self.root / "target.conf"
                    _ = write_config_fixture(settings, "".join(f"{key}={value}\n" for key, value in values.items()),
                                             template="mac_test")
                    empty_config = replace(config, values=values, source=settings)
                    with self.assertRaises(target_config.ConfigError):
                        _ = offload.run_offload(controller, empty_config, request, 0)

    def test_bad_slot_capacity_raises_config_error_in_claims_and_admission(self) -> None:
        with self.admission() as (controller, config, request):
            settings = self.root / "target.conf"
            _ = write_config_fixture(settings, "slots=bad\n", template="mac_test")
            bad_config = replace(config, values={**config.values, "slots": "bad"}, source=settings)
            with self.assertRaises(target_config.ConfigError):
                _ = mac_test.slot_capacity(mac_test.state_paths("linux"), mac_test.RepositoryScope("repository", "key"))
            with self.assertRaises(target_config.ConfigError):
                _ = offload.run_offload(controller, bad_config, request, 0)

    def test_busy_refusal_names_the_session_from_the_unit_owner_chain(self) -> None:
        run = self.root / "unit-run"
        owner = fixture_member("showrunner", name="director-session")
        _ = staff.set_run(owner.id, staff.OpenRun(run))
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"UNIT_RUN_DIR", "UNIT_SESSION_DIR", "PLAN_DELEGATE_BOARD_DIR",
                                      "PLAN_DELEGATE_SESSION_DIR", 
                                      "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
        environment.update(UNIT_RUN_DIR=str(run),
                           CLAUDE_CODE_SESSION_ID="worker-shell-session")
        with mock.patch.dict(os.environ, environment, clear=True):
            args = offload.parser().parse_args([
                "run", "--repo-root", str(self.source), "--package", "repository", "--call-id", "owner-chain",
            ], namespace=offload.CliArguments())
            request = offload.request_from_arguments(args)
            config = offload.read_config()
            mirror = offload.request_mirror(config, request, offload.KnownRepository("repository"))
            with copies.remote_lease(mirror):
                copies.register_remote(mirror)
                with self.assertRaisesRegex(copies.CopyError, f"busy; held by session {owner.current_session}"):
                    copies.remove_trees(mirror.owner)

    def test_failed_admission_keeps_both_local_and_remote_owners(self) -> None:
        first = self.mirror_record("first-owner", self.mirror.parent / "slot-1")
        second = self.mirror_record("second-owner", self.mirror.parent / "slot-2")
        self.assertEqual(publish_mirror(first), 0)
        self.assertEqual(publish_mirror(second), 0)
        with self.admission(free_gib=0) as (controller, config, request):
            result = offload.run_offload(controller, config, request, 0)
        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertEqual(copies.owned_remote_mirrors("first-owner"), [first])
        self.assertEqual(copies.owned_remote_mirrors("second-owner"), [second])
        self.assertEqual(copies.owned_remote_mirrors("new-owner"), [])
        for mirror in (first, second):
            self.assertEqual(json.loads((Path(mirror.path) / ".copy-record.json").read_text())["owner"], mirror.owner)

    def test_successful_admission_keeps_the_unselected_slot_owner(self) -> None:
        first = self.mirror_record("first-owner", self.mirror.parent / "slot-1")
        second = self.mirror_record("second-owner", self.mirror.parent / "slot-2")
        self.assertEqual(publish_mirror(first), 0)
        self.assertEqual(publish_mirror(second), 0)
        with self.admission() as (controller, config, request):
            result = offload.run_offload(controller, config, request, 0)
        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertEqual(copies.owned_remote_mirrors("second-owner"), [second])
        self.assertEqual([record.path for record in copies.owned_remote_mirrors("new-owner")], [first.path])
        self.assertEqual(json.loads((Path(second.path) / ".copy-record.json").read_text())["owner"], "second-owner")

    def test_refresh_keeps_the_owner_until_the_selected_run_starts(self) -> None:
        mirror = self.mirror_record()
        self.assertEqual(publish_mirror(mirror), 0)
        args = mirror_arguments(replace(mirror, owner="next-owner"), "mirror-receive")
        self.assertEqual(copies.mirror_run(args), 0)
        self.assertEqual(json.loads((self.mirror / ".copy-record.json").read_text())["owner"], "alpha")
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [mirror])

    def test_adoption_keeps_a_reservation_outside_the_leased_slots(self) -> None:
        with self.admission() as (controller, config, request):
            args = mac_test.CliArguments()
            args.pid, args.worktree, args.what, args.target = os.getpid(), str(self.source), "reserved", "linux"
            paths = mac_test.state_paths("linux")
            scope = mac_test.RepositoryScope("repository", "key")
            with mac_test.locked(paths.lock), redirect_stdout(io.StringIO()):
                first = mac_test.claim_once(args, paths, scope, reserve_wait=True)
            self.assertEqual(first, mac_test.ClaimFinished(0))
            request = replace(request, admission=offload.MacClaimFromWait(os.getpid()))
            result = offload.claim_for_run(controller, config, request, offload.KnownRepository("repository"), (2,))
            self.assertIsInstance(result, offload.MacClaimNotConfirmed)
            claims = mac_test.read_claims(paths)
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0].record["pid"], os.getpid())
            self.assertNotIn("remote_host", claims[0].record)

    def test_cancelled_sweep_releases_real_locks_for_admission_and_owner_removal(self) -> None:
        from scripts.mac_test.mirror_root import MacMirrorRoot
        mirror = self.mirror_record(path=MacMirrorRoot.read(conf.read("mac_test")).local_path() / "repository")
        self.assertEqual(publish_mirror(mirror), 0)
        entered, cancel = Event(), Event()

        def hold(*arguments: object, **keywords: object) -> int:
            self.assertTrue(arguments)
            self.assertIn("policy", keywords)
            entered.set()
            self.assertTrue(cancel.wait(timeout=10))
            raise InterruptedError("cancelled sweep")

        with (mock.patch.object(floor_claims, "subprocess", subprocess),
              mock.patch.object(sweep, "hold_floor", side_effect=hold),
              ThreadPoolExecutor(max_workers=2) as executor):
            pending = executor.submit(sweep.main, ["--floor-only", "--host", "mac"])
            try:
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaisesRegex(copies.CopyError, "busy"):
                    copies.remove_trees("alpha")
                with self.admission() as (controller, config, request):
                    attempted = Event()
                    complete = controller.complete_claim

                    def claim(arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
                        attempted.set()
                        complete(arguments, attempt)

                    with mock.patch.object(controller, "complete_claim", side_effect=claim):
                        admission = executor.submit(offload.run_offload, controller, config, request, 0)
                        self.assertTrue(attempted.wait(timeout=5))
                        self.assertFalse(admission.done())
                        cancel.set()
                        result = admission.result(timeout=15)
                self.assertIsInstance(result, offload.PassedOnTarget)
                self.assertTrue(Path(mirror.path).exists())
            finally:
                cancel.set()
            with self.assertRaisesRegex(InterruptedError, "cancelled sweep"):
                _ = pending.result(timeout=10)
        copies.remove_trees("alpha")
        self.assertFalse(Path(mirror.path).exists())

    def test_doc_checkout_uses_only_the_copy_owner_reply(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "direct/verify.sh").read_text()
        checkout = source[source.index("doc_checkout() {"):source.index("doc_record() {")]
        self.assertNotIn('[[ -d "$DOC_CHECKOUT/.git" ]]', checkout)

    def test_doc_description_names_the_standing_checkout_and_lease(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "docs/as-built/rustdoc-once-and-final-targets.md").read_text()
        self.assertIn("copies.py hold", source)
        self.assertIn("StagedSource", source)
        self.assertNotIn("git read-tree -m -u", source)
        self.assertNotIn("verify-doc/<repo-path-hash>/lock", source)


if __name__ == "__main__":
    _ = unittest.main()
