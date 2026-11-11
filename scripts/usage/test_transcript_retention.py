"""Apply age bands without changing staff transcripts or summaries."""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, cast, override
from unittest.mock import patch

from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.transcripts import compress_transcript, open_transcript
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.usage import transcript_retention as retention


def write_transcript(path: Path, data: bytes, days: int, now: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(data)
    path.chmod(0o640)
    stamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class RetentionTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.root / "configuration"
        config.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)})))
        _ = self.write_policy()

    def write_policy(self, *, compress_days: int = 14, delete_days: int = 60,
                     sample_files: int = 20, sample_bytes: int = 1048576) -> retention.RetentionPolicy:
        config = Path(os.environ["CONF_DIR"]) / "transcript_retention.conf"
        _ = config.write_text(
            f"# whole: 1..\ncompress_after_days={compress_days}\n"
            + f"# whole: 1..\ndelete_after_days={delete_days}\n"
            + f"# whole: 1..\nsample_files={sample_files}\n"
            + f"# whole: 1..\nsample_bytes={sample_bytes}\n")
        return retention.read_policy()

    def test_each_age_band_preserves_recent_and_staff_files_and_modes(self) -> None:
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        policy = retention.read_policy()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                owner = fixture_member("showrunner", name="retention-owner")
                member = fixture_member("unit", name="retention-fixture", parent=owner.id)
                content = b'{"message":"retained output"}\n' * 100
                cases = (("recent.jsonl", 13), ("compress.jsonl", 14), ("delete.jsonl", 61),
                         (str(member.current_session) + ".jsonl", 80), ("compressed-old.jsonl", 70),
                         (f"rollout-2026-01-01-{member.current_session}.jsonl", 80),
                         (f"{member.current_session}/subagents/agent-test.jsonl", 80))
                paths: dict[str, Path] = {}
                for name, days in cases:
                    path = write_transcript(root / name, content, days, now)
                    if name == "compressed-old.jsonl":
                        path = compressed_copy(path)
                    paths[name] = path
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                          for path in paths.values()}
                with redirect_stdout(io.StringIO()) as output:
                    totals = retention.retain_root(root, policy, retention.retained_sessions(), now,
                                                   dry_run=dry_run, finalize=retention.unchanged_summary, files=True)
                self.assertEqual((totals.compressed, totals.deleted, totals.skipped), (1, 3, 2))
                self.assertEqual(totals.freed, 3 * len(content))
                self.assertIn("bytes", output.getvalue())
                self.assertEqual(paths["recent.jsonl"].read_bytes(), content)
                self.assertEqual(paths[str(member.current_session) + ".jsonl"].read_bytes(), content)
                if dry_run:
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                                      for path in root.rglob("*") if path.is_file()}, before)
                else:
                    self.assertFalse(paths["delete.jsonl"].exists())
                    self.assertFalse(paths["compressed-old.jsonl"].exists())
                    self.assertFalse(paths[f"{member.current_session}/subagents/agent-test.jsonl"].exists())
                    plain = paths["compress.jsonl"]
                    compressed = plain.with_suffix(".jsonl.zst")
                    self.assertFalse(plain.exists())
                    self.assertEqual(compressed.stat().st_mtime_ns, before[plain][1])
                    self.assertEqual(compressed.stat().st_mode & 0o777, 0o640)
                    with open_transcript(compressed) as stream:
                        self.assertEqual(stream.read(), content)

    def test_only_main_transcripts_of_recorded_staff_stay_plain_at_any_age(self) -> None:
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        owner = fixture_member("showrunner", name="fixture-owner")
        unit = fixture_member("unit", name="live-unit", parent=owner.id)
        retired = replace(fixture_member("unit", name="retired-unit", parent=owner.id),
                          worktree=staff.RemovedWorktree("natedev", self.root / "removed"))
        runner = replace(fixture_member("showrunner", name="retired-runner"),
                         doc=staff.WrappedDoc("natedev", self.root / "wrapped.md"))
        producer = fixture_member("producer", name="live-producer")
        worker = fixture_member("worker", name="worker", parent=unit.id)
        absent = fixture_member("unit", name="absent-unit", parent=owner.id)
        record = staff.StaffSnapshot((unit, retired, runner, producer, worker))
        cases = (
            (unit, "main", 80, True),
            (retired, "main", 80, True),
            (retired, "archive", 80, True),
            (runner, "main", 80, True),
            (producer, "main", 80, True),
            (absent, "main", 80, False),
            (unit, "subagent", 35, False),
            (worker, "rollout", 35, False),
            (worker, "rollout", 1, False),
        )
        with patch.object(staff, "local_snapshot", return_value=record):
            retained = retention.retained_sessions()
        for index, (row, form, days, kept) in enumerate(cases):
            with self.subTest(kind=row.kind, form=form, days=days):
                root = self.root / f"case-{index}"
                name = (f"{row.current_session}/subagents/agent-test.jsonl" if form == "subagent"
                        else f"rollout-2026-11-01-{row.current_session}.jsonl" if form == "rollout"
                        else f"{row.current_session}.jsonl")
                path = write_transcript(root / name, b"transcript\n", days, now)
                if form == "archive":
                    path = compressed_copy(path)
                totals = retention.RetentionTotals(root)
                candidates = retention.retention_candidates(root, retention.read_policy(), retained, now, totals)
                self.assertEqual(totals.skipped, int(kept))
                self.assertEqual(dict(totals.kept), {row.kind: 1} if kept else {})
                self.assertEqual([item.path for item in candidates],
                                 [] if kept or days < 14 else [path])

    def test_policy_and_roots_come_from_scratch_configuration_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.write_policy(compress_days=3, delete_days=9, sample_files=2, sample_bytes=8)
            projects = root / ".claude/projects"
            projects.mkdir(parents=True)
            (root / ".claude2").mkdir()
            (root / ".claude2/projects").symlink_to(projects, target_is_directory=True)
            rollouts = root / ".codex2/sessions"
            rollouts.mkdir(parents=True)
            self.assertEqual(policy, retention.RetentionPolicy(timedelta(days=3), timedelta(days=9), 2, 8))
            self.assertEqual(retention.transcript_roots(root), [projects, rollouts])

    def test_default_and_files_output_for_real_and_dry_runs(self) -> None:
        now = datetime.now(timezone.utc)
        member = fixture_member("showrunner", name="output-fixture")
        unit = fixture_member("unit", name="output-unit", parent=member.id)
        producer = fixture_member("producer", name="output-producer")
        for dry_run in (True, False):
            for files in (False, True):
                with (self.subTest(dry_run=dry_run, files=files),
                      tempfile.TemporaryDirectory() as directory):
                    root = Path(directory)
                    plain = write_transcript(root / "compress.jsonl", b"repeated message\n" * 100, 20, now)
                    deleted = write_transcript(root / "delete.jsonl", b"expired\n", 80, now)
                    for row in (member, unit, producer):
                        _ = write_transcript(root / f"{row.current_session}.jsonl", b"staff\n", 80, now)
                    with (patch.object(retention, "transcript_roots", return_value=[root]),
                          redirect_stdout(io.StringIO()) as output):
                        self.assertEqual(retention.main([
                            *(["--dry-run"] if dry_run else []), *(["--files"] if files else [])]), 0)
                    compressed_word = "would compress" if dry_run else "compressed"
                    deleted_word = "would delete" if dry_run else "deleted"
                    free_word = "would free about" if dry_run else "freed"
                    counts = (f"{compressed_word} 1 files; {deleted_word} 1 files; "
                              + f"3 staff files skipped; {free_word} 0.00 GB")
                    lines = output.getvalue().splitlines()
                    self.assertEqual(lines.pop(), "kept plain: 1 showrunner, 1 producer, 1 unit")
                    self.assertEqual(lines[-2:], [f"{root}: {counts}", f"total: {counts}"])
                    if not dry_run:
                        self.assertEqual(lines.pop(0), f"{root}: compressing 1 files, deleting 1 files")
                    if files:
                        self.assertEqual(len(lines), 4)
                        self.assertTrue(lines[0].startswith(f"{'would compress' if dry_run else 'compress'} {plain} -> "))
                        self.assertEqual(lines[1], f"{'would delete' if dry_run else 'delete'} {deleted}; frees 8 bytes")
                    else:
                        self.assertEqual(len(lines), 2)

    def test_compression_replaces_an_expired_archive_without_deleting_it(self) -> None:
        now = datetime.now(timezone.utc)
        content = b"replacement transcript\n" * 100
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plain = write_transcript(root / "same.jsonl", content, 20, now)
                archive = write_transcript(plain.with_suffix(".jsonl.zst"), b"expired archive", 80, now)
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                          for path in (plain, archive)}
                with tempfile.TemporaryFile() as output:
                    compressed_size = compress_transcript(plain, output)
                with redirect_stdout(io.StringIO()) as output:
                    totals = retention.retain_root(root, retention.read_policy(), {}, now,
                                                   dry_run=dry_run, finalize=retention.unchanged_summary, files=True)
                self.assertEqual((totals.compressed, totals.deleted), (1, 0))
                self.assertEqual(totals.freed, len(content) + len(before[archive][0]) - compressed_size)
                self.assertNotIn(f"delete {archive}", output.getvalue())
                if dry_run:
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                                      for path in root.iterdir()}, before)
                else:
                    self.assertFalse(plain.exists())
                    with open_transcript(archive) as stream:
                        self.assertEqual(stream.read(), content)

    def test_real_savings_use_the_archive_size_at_replacement(self) -> None:
        now = datetime.now(timezone.utc)
        content = b"replacement transcript\n" * 100
        plain = write_transcript(self.root / "transcripts/same.jsonl", content, 20, now)
        archive = write_transcript(plain.with_suffix(".jsonl.zst"), b"old archive", 1, now)
        updated_archive = b"changed archive\n" * 100

        def compress(path: Path, output: BinaryIO) -> int:
            size = compress_transcript(path, output)
            _ = archive.write_bytes(updated_archive)
            return size

        with (patch.object(retention, "compress_transcript", side_effect=compress),
              redirect_stdout(io.StringIO())):
            totals = retention.retain_root(plain.parent, retention.read_policy(), {}, now,
                                           dry_run=False, finalize=retention.unchanged_summary)
        self.assertEqual((totals.compressed, totals.deleted), (1, 0))
        self.assertEqual(totals.freed, len(content) + len(updated_archive) - archive.stat().st_size)
        self.assertFalse(plain.exists())
        with open_transcript(archive) as stream:
            self.assertEqual(stream.read(), content)

    def test_real_run_flushes_root_counts_before_compression(self) -> None:
        now = datetime.now(timezone.utc)
        plain = write_transcript(self.root / "transcripts/compress.jsonl", b"message\n" * 100, 20, now)
        with patch("builtins.print") as printed:
            def compress(path: Path, output: BinaryIO) -> int:
                printed.assert_called_once_with(
                    f"{plain.parent}: compressing 1 files, deleting 0 files", flush=True)
                return compress_transcript(path, output)

            with patch.object(retention, "compress_transcript", side_effect=compress):
                _ = retention.retain_root(plain.parent, retention.read_policy(), {}, now,
                                          dry_run=False, finalize=retention.unchanged_summary)

    def test_real_run_skips_candidates_changed_after_collection(self) -> None:
        now = datetime.now(timezone.utc)
        collect = retention.retention_candidates
        for days in (20, 80):
            for change in ("mtime", "size", "gone"):
                with self.subTest(days=days, change=change):
                    path = write_transcript(self.root / f"{days}-{change}/transcript.jsonl", b"message\n", days, now)
                    saved = path.stat()

                    def changed_candidates(root: Path, policy: retention.RetentionPolicy,
                                           retained: dict[str, retention.StaffKeepRule], at: datetime,
                                           totals: retention.RetentionTotals
                                           ) -> list[retention.DeletionCandidate | retention.CompressionCandidate]:
                        candidates = collect(root, policy, retained, at, totals)
                        self.assertEqual(len(candidates), 1)
                        if change == "mtime":
                            os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns + 1_000_000))
                        elif change == "size":
                            _ = path.write_bytes(b"message\nnew message\n")
                            os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
                        else:
                            path.unlink()
                        return candidates

                    with (patch.object(retention, "retention_candidates", side_effect=changed_candidates),
                          patch.object(retention, "compress_transcript") as compressor,
                          patch.object(retention, "unchanged_summary") as finalize,
                          redirect_stdout(io.StringIO())):
                        totals = retention.retain_root(path.parent, retention.read_policy(), {}, now,
                                                       dry_run=False, finalize=finalize)
                    self.assertEqual((totals.compressed, totals.deleted, totals.freed), (0, 0, 0))
                    compressor.assert_not_called()
                    finalize.assert_not_called()
                    self.assertFalse(path.with_suffix(".jsonl.zst").exists())
                    if change == "gone":
                        self.assertFalse(path.exists())
                    elif change == "size":
                        self.assertEqual(path.read_bytes(), b"message\nnew message\n")
                        self.assertEqual(path.stat().st_mtime_ns, saved.st_mtime_ns)
                    else:
                        self.assertEqual(path.read_bytes(), b"message\n")
                        self.assertEqual(path.stat().st_mtime_ns, saved.st_mtime_ns + 1_000_000)

    def test_dry_run_samples_configured_prefixes_and_estimates_other_files(self) -> None:
        now = datetime.now(timezone.utc)
        policy = self.write_policy(sample_files=2, sample_bytes=128)
        root = self.root / "transcripts"
        contents = [(b"sample message " + bytes([value])) * 1000 for value in range(5)]
        paths = [write_transcript(root / f"compress-{index}.jsonl", content, 20, now)
                 for index, content in enumerate(contents)]
        deleted = write_transcript(root / "delete.jsonl", b"old" * 37, 80, now)
        replaced = write_transcript(paths[0].with_suffix(".jsonl.zst"), b"prior compressed copy", 1, now)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                  for path in [*paths, deleted, replaced]}
        samples: list[bytes] = []
        sizes: list[int] = []

        def sample(path: Path, output: BinaryIO) -> int:
            self.assertNotIn(path, paths)
            samples.append(path.read_bytes())
            size = compress_transcript(path, output)
            sizes.append(size)
            return size

        with (patch.object(retention, "compress_transcript", side_effect=sample) as compressor,
              patch.object(retention, "unchanged_summary") as finalize,
              redirect_stdout(io.StringIO()) as output):
            totals = retention.retain_root(root, policy, {}, now,
                                           dry_run=True, finalize=finalize, files=True)
        self.assertEqual(samples, [content[:128] for content in contents[:2]])
        self.assertEqual(compressor.call_count, 2)
        ratio = sum(sizes) / sum(map(len, samples))
        expected = sum(round(len(content) * (1 - ratio)) for content in contents)
        self.assertEqual(totals.freed, expected + len(before[deleted][0]) + len(before[replaced][0]))
        self.assertEqual((totals.compressed, totals.deleted, totals.skipped), (5, 1, 0))
        self.assertIn("frees about", output.getvalue())
        finalize.assert_not_called()
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                          for path in root.iterdir()}, before)

    def test_deletion_bytes_stay_exact_without_compression_samples(self) -> None:
        now = datetime.now(timezone.utc)
        path = write_transcript(self.root / "transcripts/delete.jsonl", b"old" * 37, 80, now)
        with patch.object(retention, "compress_transcript") as compressor:
            totals = retention.retain_root(path.parent, retention.read_policy(), {}, now,
                                           dry_run=True, finalize=retention.unchanged_summary)
        self.assertEqual(totals.freed, 111)
        self.assertEqual(path.read_bytes(), b"old" * 37)
        compressor.assert_not_called()

    def test_summary_reports_gb_with_two_decimals(self) -> None:
        total = retention.RetentionTotals(Path("fixture"), 3, 4, 5, 1_234_567_890)
        self.assertEqual(retention.summary_line(total, dry_run=False),
                         "fixture: compressed 3 files; deleted 4 files; 5 staff files skipped; freed 1.23 GB")
        self.assertEqual(retention.summary_line(total, dry_run=True),
                         "fixture: would compress 3 files; would delete 4 files; 5 staff files skipped; would free about 1.23 GB")

    def test_default_output_sums_multiple_roots(self) -> None:
        now = datetime.now(timezone.utc)
        _ = fixture_member("showrunner", name="multi-root-fixture")
        roots = [Path.home() / ".claude/projects", Path.home() / ".codex/sessions"]
        for root in roots:
            _ = write_transcript(root / "delete.jsonl", b"old", 80, now)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(retention.main(["--dry-run"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            *(f"{root}: would compress 0 files; would delete 1 files; 0 staff files skipped; would free about 0.00 GB"
              for root in roots),
            "total: would compress 0 files; would delete 2 files; 0 staff files skipped; would free about 0.00 GB",
            "kept plain: 0 showrunner, 0 producer, 0 unit",
        ])

    def test_sample_limits_refuse_nonpositive_values(self) -> None:
        for key in ("sample_files", "sample_bytes"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                _ = self.write_policy(**{key: 0})
