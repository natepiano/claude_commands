"""An interrupted publication keeps the last complete text available."""
from pathlib import Path
from contextlib import ExitStack
import errno
import os
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from typing import BinaryIO, TextIO, cast

from scripts.lib import atomic_file
from scripts.lib.atomic_file import write_text_atomic


class AtomicFileTests(unittest.TestCase):
    def test_new_file_mode_publishes_complete_private_text_and_preserves_existing_permissions(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "private.json"
                if existing:
                    _ = path.write_text("old content")
                    path.chmod(0o640)
                replace = os.replace

                def publish(temporary: Path, destination: Path) -> None:
                    self.assertEqual(destination.exists(), existing)
                    self.assertEqual(temporary.read_text(), '{"complete": true}')
                    self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o640 if existing else 0o600)
                    replace(temporary, destination)

                with patch.object(os, "replace", side_effect=publish):
                    write_text_atomic(path, '{"complete": true}', new_file_mode=0o600)
                self.assertEqual(path.read_text(), '{"complete": true}')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640 if existing else 0o600)

    def test_temporary_targets_match_the_names_used_for_publication(self) -> None:
        for target in (".index.json", "report.1.2.html", ".draft.txt", "draft\nnotes.txt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(atomic_file.temporary_target(f".{target}.123.456.tmp"), target)
                path = Path(directory) / target
                publish = os.replace
                temporary_names: list[str] = []

                def replace_file(source: Path, destination: Path) -> None:
                    temporary_names.append(source.name)
                    publish(source, destination)

                with patch.object(os, "replace", side_effect=replace_file):
                    atomic_file.write_bytes_atomic(path, b"complete content")
                self.assertEqual(len(temporary_names), 1)
                self.assertEqual(atomic_file.temporary_target(temporary_names[0]), target)
                self.assertEqual(path.read_bytes(), b"complete content")
        for name in ("index.json", ".index.json.bak", ".review-link-x", "draft.tmp"):
            with self.subTest(name=name):
                self.assertIsNone(atomic_file.temporary_target(name))

    def test_cleanup_matches_the_complete_target_before_checking_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / ".report.12.34.5678.tmp"
            _ = temporary.write_bytes(b"unfinished")
            self.assertEqual(atomic_file.temporary_target(temporary.name), "report.12")
            with patch.object(os, "kill", side_effect=ProcessLookupError(34)) as process:
                atomic_file.remove_dead_temporary_files(root / "report")
                process.assert_not_called()
                self.assertTrue(temporary.exists())
                atomic_file.remove_dead_temporary_files(root / "report.12")
                process.assert_called_once_with(34, 0)
            self.assertFalse(temporary.exists())

    def test_explicit_binary_mode_applies_to_the_temporary_and_published_file(self) -> None:
        for exists in (False, True):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "private.json"
                if exists:
                    _ = path.write_bytes(b"old")
                    path.chmod(0o644)
                publish = os.replace
                modes: list[int] = []

                def replace_file(source: Path, destination: Path) -> None:
                    modes.append(stat.S_IMODE(source.stat().st_mode))
                    self.assertEqual(source.read_bytes(), b"complete private record")
                    publish(source, destination)

                with patch.object(os, "replace", side_effect=replace_file):
                    atomic_file.write_bytes_atomic(path, b"complete private record", mode=0o600)
                self.assertEqual(modes, [0o600])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), b"complete private record")
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_publication_failure_identifies_whether_the_complete_replacement_is_visible(self) -> None:
        for binary in (False, True):
            for stage in ("write", "flush", "fsync", "replace", "folder fsync"):
                with (self.subTest(binary=binary, stage=stage), tempfile.TemporaryDirectory() as directory,
                      ExitStack() as patches):
                    path = Path(directory) / "worker-state.json"
                    old = b'old complete state\xff\x00\r\n'
                    text = '{"worker": "new complete state ☃"}\n'
                    new = b'new complete state\xfe\xff\x00\r\n' if binary else text.encode("utf-8")
                    _ = path.write_bytes(old)
                    failure = OSError(errno.ENOSPC, "injected " + stage)
                    fdopen = os.fdopen

                    def failing_stream(descriptor: int, mode: str, *, encoding: str | None) -> TextIO | BinaryIO:
                        stream = cast(TextIO | BinaryIO, fdopen(descriptor, mode, encoding=encoding))
                        observer = MagicMock(wraps=stream)
                        cast(MagicMock, observer.__enter__).return_value = observer

                        def close_stream(*_args: object) -> None:
                            stream.close()

                        cast(MagicMock, observer.__exit__).side_effect = close_stream
                        if stage == "write":
                            def partial_write(content: str | bytes) -> int:
                                if isinstance(content, str):
                                    _ = cast(TextIO, stream).write(content[:5])
                                else:
                                    _ = cast(BinaryIO, stream).write(content[:5])
                                raise failure
                            cast(MagicMock, observer.write).side_effect = partial_write
                        else:
                            cast(MagicMock, observer.flush).side_effect = failure
                        return cast(TextIO | BinaryIO, cast(object, observer))

                    if stage in ("write", "flush"):
                        _ = patches.enter_context(patch.object(os, "fdopen", failing_stream))
                    elif stage == "fsync":
                        _ = patches.enter_context(patch.object(os, "fsync", side_effect=failure))
                    elif stage == "replace":
                        _ = patches.enter_context(patch.object(os, "replace", side_effect=failure))
                    else:
                        _ = patches.enter_context(patch.object(atomic_file, "sync_folder", side_effect=failure))
                    with self.assertRaises(OSError) as caught:
                        if binary:
                            atomic_file.write_bytes_atomic(path, new)
                        else:
                            write_text_atomic(path, text)
                    self.assertEqual(path.read_bytes(), new if stage == "folder fsync" else old)
                    self.assertEqual(list(path.parent.iterdir()), [path])
                    self.assertEqual(type(caught.exception).__name__,
                                     "AtomicDurabilityUnconfirmed" if stage == "folder fsync" else "AtomicPublicationFailed")
                    self.assertIn(str(path), str(caught.exception))

    def test_create_only_publication_keeps_existing_and_concurrent_files_without_temporary_files(self) -> None:
        for already_exists in (False, True):
            with self.subTest(already_exists=already_exists), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "private.json"
                original = '{"login": "fixture@example.invalid"}\n'
                if already_exists:
                    _ = path.write_text(original, encoding="utf-8")
                link = os.link

                def publish(temporary: Path, destination: Path) -> None:
                    self.assertEqual(temporary.read_text(encoding="utf-8"), '{"seed": true}\n')
                    self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
                    if not already_exists:
                        _ = destination.write_text(original, encoding="utf-8")
                    link(temporary, destination)

                with patch("scripts.lib.atomic_file.os.link", side_effect=publish):
                    self.assertFalse(atomic_file.create_text_atomic(path, '{"seed": true}\n'))
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_create_only_publication_is_private_and_synced_before_and_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "private.json"
            sync = os.fsync
            synced_versions: list[tuple[bool, bool]] = []

            def record_sync(descriptor: int) -> None:
                synced_versions.append((stat.S_ISDIR(os.fstat(descriptor).st_mode), path.exists()))
                sync(descriptor)

            with patch("scripts.lib.atomic_file.os.fsync", side_effect=record_sync):
                self.assertTrue(atomic_file.create_text_atomic(path, '{"seed": true}\n'))
            self.assertEqual(synced_versions, [(False, False), (True, True)])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"seed": true}\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_next_write_removes_dead_process_temporary_files_and_keeps_live_ones(self) -> None:
        for permission_denied in (False, True):
            with self.subTest(permission_denied=permission_denied), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "change-review - demo.md"
                live_pid = os.getpid()
                dead_pid = live_pid + 1000000
                live = path.with_name(f".{path.name}.{live_pid}.live1234.tmp")
                dead = path.with_name(f".{path.name}.{dead_pid}.dead1234.tmp")
                unrelated = path.with_name(f".change-review - demoXmd.{dead_pid}.other123.tmp")
                for file in (live, dead, unrelated):
                    _ = file.write_text("unfinished")
                real_kill = os.kill

                def process_exists(pid: int, signal: int) -> None:
                    if pid == dead_pid:
                        raise ProcessLookupError(pid)
                    if permission_denied:
                        raise PermissionError(pid)
                    real_kill(pid, signal)

                with patch("scripts.lib.atomic_file.os.kill", side_effect=process_exists):
                    write_text_atomic(path, "Reviewed\n")
                self.assertEqual(path.read_text(), "Reviewed\n")
                self.assertFalse(dead.exists())
                self.assertEqual(live.read_text(), "unfinished")
                self.assertEqual(unrelated.read_text(), "unfinished")

    def test_write_creates_a_missing_parent_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "nested" / "state.json"
            write_text_atomic(path, '["question"]\n')
            self.assertEqual(path.read_text(), '["question"]\n')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_replacement_syncs_parent_folder_after_publishing_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            _ = path.write_text("old state", encoding="utf-8")
            sync = os.fsync
            synced_versions: list[tuple[bool, str]] = []

            def record_sync(descriptor: int) -> None:
                synced_versions.append((stat.S_ISDIR(os.fstat(descriptor).st_mode),
                                        path.read_text(encoding="utf-8")))
                sync(descriptor)

            with patch("scripts.lib.atomic_file.os.fsync", side_effect=record_sync):
                write_text_atomic(path, "new complete state")
            self.assertEqual(synced_versions, [(False, "old state"), (True, "new complete state")])

    def test_durable_text_write_preserves_previous_file_when_content_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            write_text_atomic(path, "old text\n")
            before = path.read_bytes()
            with patch("scripts.lib.atomic_file.os.fsync", side_effect=OSError("injected content sync failure")):
                with self.assertRaisesRegex(OSError, "injected content sync failure"):
                    write_text_atomic(path, "replacement \N{SNOWMAN}\n")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".note.txt.*")), [])
            write_text_atomic(path, "replacement \N{SNOWMAN}\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement \N{SNOWMAN}\n")
            self.assertEqual(list(path.parent.glob(".note.txt.*")), [])

    def test_new_file_after_interrupted_publish_uses_the_default_permissions(self) -> None:
        for binary in (False, True):
            with self.subTest(binary=binary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report"

                def publish() -> None:
                    if binary:
                        atomic_file.write_bytes_atomic(path, b"complete report")
                    else:
                        write_text_atomic(path, "complete report")

                previous = os.umask(0o027)
                try:
                    with patch.object(os, "umask", wraps=os.umask) as umask:
                        with patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("stopped")):
                            with self.assertRaises(OSError):
                                publish()
                        self.assertFalse(path.exists())
                        publish()
                        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                        self.assertEqual(path.read_text(), "complete report")
                        umask.assert_not_called()
                finally:
                    _ = os.umask(previous)

    def test_replacing_a_file_keeps_its_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared-report"
            _ = path.write_text("old report")
            path.chmod(0o664)
            previous = os.umask(0o077)
            try:
                write_text_atomic(path, "new report")
            finally:
                _ = os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o664)
            self.assertEqual(path.read_text(), "new report")

    def test_replacement_uses_permissions_tightened_during_content_sync(self) -> None:
        for binary in (False, True):
            with self.subTest(binary=binary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report"
                _ = path.write_text("old report")
                path.chmod(0o644)
                sync = os.fsync

                def tighten_permissions(descriptor: int) -> None:
                    if stat.S_ISREG(os.fstat(descriptor).st_mode):
                        path.chmod(0o600)
                    sync(descriptor)

                with patch.object(os, "fsync", side_effect=tighten_permissions):
                    if binary:
                        atomic_file.write_bytes_atomic(path, b"new report")
                    else:
                        write_text_atomic(path, "new report")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_bytes(), b"new report")
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_publish_keeps_old_file_and_rerun_publishes_full_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            _ = path.write_text("old state", encoding="utf-8")
            with patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("stopped")):
                with self.assertRaises(OSError):
                    write_text_atomic(path, "new complete state")
            self.assertEqual(path.read_text(), "old state")
            write_text_atomic(path, "new complete state")
            self.assertEqual(path.read_text(), "new complete state")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_first_publish_leaves_no_partial_file_for_a_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan"
            with patch("scripts.lib.atomic_file.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_text_atomic(path, "## Source\n\ncomplete brief\n")
            self.assertFalse(path.exists())
            write_text_atomic(path, "## Source\n\ncomplete brief\n")
            self.assertEqual(path.read_text(), "## Source\n\ncomplete brief\n")
