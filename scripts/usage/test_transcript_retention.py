"""Apply age bands without changing staff transcripts or summaries."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.transcripts import open_transcript
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.usage import transcript_retention as retention


class RetentionTests(StaffTestCase):
    def test_each_age_band_preserves_recent_and_staff_files_and_modes(self) -> None:
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        policy = retention.RetentionPolicy(timedelta(days=14), timedelta(days=60))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                member = fixture_member("showrunner", name="retention-fixture")
                content = b'{"message":"retained output"}\n' * 100
                cases = (("recent.jsonl", 13), ("compress.jsonl", 14), ("delete.jsonl", 61),
                         (str(member.current_session) + ".jsonl", 80), ("compressed-old.jsonl", 70),
                         (f"rollout-2026-01-01-{member.current_session}.jsonl", 80),
                         (f"{member.current_session}/subagents/agent-test.jsonl", 80))
                paths: dict[str, Path] = {}
                for name, days in cases:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_bytes(content)
                    path.chmod(0o640)
                    if name == "compressed-old.jsonl":
                        path = compressed_copy(path)
                    stamp = (now - timedelta(days=days)).timestamp()
                    os.utime(path, (stamp, stamp))
                    paths[name] = path
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                          for path in paths.values()}
                with redirect_stdout(io.StringIO()) as output:
                    totals = retention.retain_root(root, policy, retention.retained_sessions(), now,
                                                   dry_run=dry_run, finalize=retention.unchanged_summary)
                self.assertEqual((totals.compressed, totals.deleted, totals.skipped), (1, 2, 3))
                self.assertEqual(totals.freed, 2 * len(content))
                self.assertIn("bytes", output.getvalue())
                self.assertEqual(paths["recent.jsonl"].read_bytes(), content)
                self.assertEqual(paths[str(member.current_session) + ".jsonl"].read_bytes(), content)
                if dry_run:
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                                      for path in root.rglob("*") if path.is_file()}, before)
                else:
                    self.assertFalse(paths["delete.jsonl"].exists())
                    self.assertFalse(paths["compressed-old.jsonl"].exists())
                    plain = paths["compress.jsonl"]
                    compressed = plain.with_suffix(".jsonl.zst")
                    self.assertFalse(plain.exists())
                    self.assertEqual(compressed.stat().st_mtime_ns, before[plain][1])
                    self.assertEqual(compressed.stat().st_mode & 0o777, 0o640)
                    with open_transcript(compressed) as stream:
                        self.assertEqual(stream.read(), content)

    def test_policy_and_roots_come_from_scratch_configuration_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            _ = (config / "transcript_retention.conf").write_text(
                "# whole: 1..\ncompress_after_days=3\n# whole: 1..\ndelete_after_days=9\n")
            projects = root / ".claude/projects"
            projects.mkdir(parents=True)
            (root / ".claude2").mkdir()
            (root / ".claude2/projects").symlink_to(projects, target_is_directory=True)
            rollouts = root / ".codex2/sessions"
            rollouts.mkdir(parents=True)
            with patch.dict(os.environ, {"CONF_DIR": str(config)}):
                self.assertEqual(retention.read_policy(), retention.RetentionPolicy(timedelta(days=3), timedelta(days=9)))
            self.assertEqual(retention.transcript_roots(root), [projects, rollouts])
