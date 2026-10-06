"""Exercise the Linux window recorder through isolated executable stubs."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("screen_record.py")
REAL_TIMEOUT = shutil.which("timeout")
STUB_BODY = r'''
import json
import os
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["SCREEN_RECORD_TEST_ROOT"])
settings = json.loads((root / "settings.json").read_text())
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"tool": name, "args": args}) + "\n")

if name == "xprop":
    if settings.get("xprop_hang_on") == args[0]:
        time.sleep(60)
    if args[0] == "-root":
        if settings.get("root_fail"):
            print("xprop: root query failed", file=sys.stderr)
            sys.exit(1)
        count_file = root / "root-count"
        count = int(count_file.read_text()) + 1 if count_file.exists() else 1
        count_file.write_text(str(count))
        if count <= settings.get("empty_root_calls", 0):
            print("_NET_CLIENT_LIST:  not found.")
        else:
            ids = [window["id"] for window in settings.get("windows", [])]
            print("_NET_CLIENT_LIST(WINDOW): window id # " + ", ".join(ids))
    else:
        window = next((item for item in settings.get("windows", []) if item["id"] == args[1]), None)
        if window is None or window.get("title") is None:
            print("WM_NAME:  not found.")
        else:
            title = window["title"].replace("\\", "\\\\").replace('"', '\\"')
            print('WM_NAME(UTF8_STRING) = "' + title + '"')
elif name == "ffmpeg":
    clip = Path(args[-1])
    if settings.get("inject_collision") and not clip.exists():
        clip.write_bytes(b"another recording")
        sys.exit(1)
    with clip.open("wb") as output:
        output.truncate(settings.get("file_size", 1000))
    if settings.get("hang"):
        time.sleep(60)
    sys.exit(settings.get("ffmpeg_exit", 0))
elif name == "timeout":
    if settings.get("short"):
        args[1] = "1"
    executable = settings["real_timeout"]
    os.execv(executable, [executable, *args])
'''


class ScreenRecordTests(unittest.TestCase):
    root: Path = Path()
    clips: Path = Path()
    stubs: Path = Path()
    settings: dict[str, object] = {}
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.assertIsNotNone(REAL_TIMEOUT)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clips = self.root / "clips"
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        for name in ("xprop", "timeout", "ffmpeg"):
            stub = self.stubs / name
            _ = stub.write_text("#!" + sys.executable + "\n" + STUB_BODY)
            stub.chmod(0o755)
        self.settings = {
            "real_timeout": REAL_TIMEOUT,
            "windows": [{"id": "0x2600006", "title": "hana debug 15731"}],
        }
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "SCREEN_RECORD_DIR": str(self.clips),
            "SCREEN_RECORD_TEST_ROOT": str(self.root),
            "DISPLAY": ":99",
            "PATH": str(self.stubs),
        }

    def run_script(self, *arguments: str, deadline: float = 20) -> subprocess.CompletedProcess[str]:
        _ = (self.root / "settings.json").write_text(json.dumps(self.settings))
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            _ = process.communicate()
            self.fail(f"screen recorder exceeded the {deadline} s test deadline")
        exit_code = process.returncode
        if exit_code is None:
            self.fail("screen recorder has no exit status")
        return subprocess.CompletedProcess(process.args, exit_code, stdout, stderr)

    def calls_for(self, tool: str) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        calls = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [cast(list[str], call["args"]) for call in calls if call["tool"] == tool]

    def assert_no_effects(self) -> None:
        self.assertFalse(self.clips.exists())
        self.assertFalse((self.root / "calls.jsonl").exists())

    def make_clip(self, name: str, size: int, days_old: int) -> Path:
        self.clips.mkdir(exist_ok=True)
        clip = self.clips / name
        with clip.open("wb") as output:
            _ = output.truncate(size)
        instant = time.time() - days_old * 86_400
        _ = os.utime(clip, (instant, instant))
        return clip

    def test_refusals_precede_tools_and_directory_creation(self) -> None:
        cases = (
            ((), "--seconds is required", "no default"),
            (("--seconds", "0"), "whole number from 1 to 60", "got '0'"),
            (("--seconds", "61"), "60 s ceiling", "no override"),
            (("--seconds", "abc"), "whole number from 1 to 60", "got 'abc'"),
            (("--seconds", "2.5"), "whole number from 1 to 60", "got '2.5'"),
        )
        for seconds, first, second in cases:
            with self.subTest(seconds=seconds):
                result = self.run_script("--window", "debug 15731", *seconds)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(first, result.stderr)
                self.assertIn(second, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assert_no_effects()
        for extra, message in (
            (("--window", ""), "--window needs a non-empty"),
            (("--window", "debug 15731", "--name", "../x"), "--name must match"),
        ):
            with self.subTest(extra=extra):
                result = self.run_script("--seconds", "2", *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assert_no_effects()
        (self.stubs / "xprop").unlink()
        result = self.run_script("--window", "debug", "--seconds", "61")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("60 s ceiling", result.stderr)
        self.assert_no_effects()
        result = self.run_script("--window", "debug", "--seconds", "2", "--force")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_no_effects()

    def test_sixty_seconds_is_allowed_with_a_seventy_five_second_outer_limit(self) -> None:
        result = self.run_script("--window", "debug 15731", "--seconds", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_for("timeout")[0][:3], ["--kill-after=5", "75", "ffmpeg"])
        ffmpeg = self.calls_for("ffmpeg")[0]
        self.assertEqual(ffmpeg[ffmpeg.index("-t") + 1], "60")

    def test_records_matching_window_with_required_ffmpeg_limits(self) -> None:
        result = self.run_script("--window", "debug 15731", "--seconds", "5", "--name", "startup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        clip = Path(result.stdout.strip())
        self.assertTrue(clip.exists())
        self.assertEqual(clip.parent, self.clips)
        self.assertRegex(clip.name, r"^startup-\d{8}T\d{6}Z\.mp4$")
        self.assertIn("recorded 5 s of 'hana debug 15731'", result.stderr)
        self.assertEqual(self.calls_for("timeout")[0][:3], ["--kill-after=5", "20", "ffmpeg"])
        ffmpeg = self.calls_for("ffmpeg")[0]
        expected = [
            "-f", "x11grab", "-framerate", "60", "-window_id", "39845894", "-i", ":99",
            "-t", "5", "-fs", "500000000", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p",
        ]
        self.assertEqual(ffmpeg[:5], ["-hide_banner", "-loglevel", "error", "-nostdin", "-y"])
        self.assertNotIn("-n", ffmpeg)
        self.assertEqual(ffmpeg[5:-1], expected)
        self.assertEqual(ffmpeg[-1], str(clip))

    def test_same_label_runs_preserve_the_first_clip(self) -> None:
        first = self.run_script("--window", "debug", "--seconds", "2", "--name", "shared")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_path = Path(first.stdout.strip())
        first_bytes = first_path.read_bytes()
        second = self.run_script("--window", "debug", "--seconds", "2", "--name", "shared")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_path = Path(second.stdout.strip())
        self.assertNotEqual(second_path, first_path)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertTrue(second_path.exists())
        if first_path.stem == second_path.stem.removesuffix("-2"):
            self.assertEqual(second_path.stem, first_path.stem + "-2")

    def test_existing_base_clip_survives_a_new_recording(self) -> None:
        self.clips.mkdir()
        existing: set[Path] = set()
        for offset in range(10):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() + offset))
            base = self.clips / f"shared-{stamp}.mp4"
            _ = base.write_bytes(b"existing recording")
            existing.add(base)
        result = self.run_script("--window", "debug", "--seconds", "2", "--name", "shared")
        self.assertEqual(result.returncode, 0, result.stderr)
        clip = Path(result.stdout.strip())
        self.assertTrue(clip.name.endswith("-2.mp4"))
        base = clip.with_name(clip.name.removesuffix("-2.mp4") + ".mp4")
        self.assertIn(base, existing)
        self.assertEqual(base.read_bytes(), b"existing recording")
        self.assertIn("-y", self.calls_for("ffmpeg")[0])
        self.assertNotIn("-n", self.calls_for("ffmpeg")[0])

    def test_new_clip_path_is_reserved_before_ffmpeg_starts(self) -> None:
        self.settings["inject_collision"] = True
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).stat().st_size, 1000)
        self.assertIn("-y", self.calls_for("ffmpeg")[0])

    def test_polls_until_the_window_appears(self) -> None:
        self.settings["empty_root_calls"] = 3
        start = time.monotonic()
        result = self.run_script("--window", "debug 15731", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - start, 2)
        self.assertGreaterEqual(len([call for call in self.calls_for("xprop") if call[0] == "-root"]), 4)

    def test_multiple_matching_titles_stop_before_ffmpeg(self) -> None:
        self.settings["windows"] = [
            {"id": "0x2600006", "title": "hana debug 15731"},
            {"id": "0x2800003", "title": "other debug 15731"},
        ]
        result = self.run_script("--window", "debug 15731", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("2 windows match", result.stderr)
        self.assertIn("hana debug 15731", result.stderr)
        self.assertIn("other debug 15731", result.stderr)
        self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_no_matching_title_reports_titles_after_ten_seconds(self) -> None:
        start = time.monotonic()
        result = self.run_script("--window", "absent title", "--seconds", "2")
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertGreaterEqual(elapsed, 9.5)
        self.assertLess(elapsed, 15)
        self.assertIn("after 10 s", result.stderr)
        self.assertIn("hana debug 15731", result.stderr)
        self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_root_query_failure_stops_at_once(self) -> None:
        self.settings["root_fail"] = True
        start = time.monotonic()
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertLess(time.monotonic() - start, 2)
        self.assertIn("root query failed", result.stderr)
        self.assertEqual(len(self.calls_for("xprop")), 1)
        self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_hung_window_queries_end_within_lookup_limit(self) -> None:
        for query in ("-root", "-id"):
            with self.subTest(query=query):
                self.settings["xprop_hang_on"] = query
                start = time.monotonic()
                result = self.run_script("--window", "debug", "--seconds", "2", deadline=14)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertLess(time.monotonic() - start, 12)
                self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_refusal_values_with_newlines_produce_one_stderr_line(self) -> None:
        for arguments in (
            ("--window", "debug", "--seconds", "1\n2"),
            ("--window", "debug", "--seconds", "2", "--name", "a\nb"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assert_no_effects()

    def test_missing_tool_and_display_are_reported(self) -> None:
        (self.stubs / "xprop").unlink()
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("missing on PATH: xprop", result.stderr)
        self.assertEqual(self.calls_for("ffmpeg"), [])
        _ = (self.stubs / "xprop").write_text("#!" + sys.executable + "\n" + STUB_BODY)
        (self.stubs / "xprop").chmod(0o755)
        del self.environment["DISPLAY"]
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("DISPLAY is not set", result.stderr)
        self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_ffmpeg_failure_deletes_partial_clip(self) -> None:
        self.settings["ffmpeg_exit"] = 1
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("ffmpeg exited 1; partial clip deleted", result.stderr)
        self.assertEqual(list(self.clips.glob("*.mp4")), [])

    def test_file_cap_keeps_clip_and_reports_limit(self) -> None:
        self.settings["file_size"] = 500_000_001
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(Path(result.stdout.strip()).stat().st_size, 500_000_001)
        self.assertIn("500 MB file cap", result.stderr)

    def test_outer_timeout_deletes_partial_clip(self) -> None:
        self.settings.update({"hang": True, "short": True})
        start = time.monotonic()
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(self.calls_for("timeout")[0][:3], ["--kill-after=5", "17", "ffmpeg"])
        self.assertIn("outer timeout at 17 s", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.clips.glob("*.mp4")), [])

    def test_prunes_clips_older_than_seven_days(self) -> None:
        old = self.make_clip("old.mp4", 100, 8)
        recent = self.make_clip("recent.mp4", 100, 6)
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(Path(result.stdout.strip()).exists())
        self.assertIn("1 older than 7 days", result.stderr)

    def test_prunes_oldest_clips_until_under_two_gib(self) -> None:
        clips = [self.make_clip(f"age-{age}.mp4", 700 * 1024 * 1024, age) for age in (4, 3, 2, 1)]
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(clips[0].exists())
        self.assertFalse(clips[1].exists())
        self.assertTrue(clips[2].exists())
        self.assertTrue(clips[3].exists())
        self.assertTrue(Path(result.stdout.strip()).exists())
        self.assertIn("0 older than 7 days, 2 to bring the directory under 2 GiB", result.stderr)

    def test_age_prune_precedes_size_prune(self) -> None:
        old = self.make_clip("old.mp4", 1536 * 1024 * 1024, 8)
        current = [self.make_clip(f"current-{n}.mp4", 600 * 1024 * 1024, 1) for n in (1, 2)]
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(all(clip.exists() for clip in current))
        self.assertIn("1 older than 7 days, 0 to bring the directory under 2 GiB", result.stderr)

    def test_non_mp4_files_directories_and_symlinks_are_not_pruned(self) -> None:
        notes = self.make_clip("notes.txt", 3 * 1024 * 1024 * 1024, 30)
        directory = self.clips / "folder.mp4"
        directory.mkdir()
        outside = self.root / "outside.mp4"
        _ = outside.write_bytes(b"clip")
        link = self.clips / "old.mp4"
        link.symlink_to(outside)
        instant = time.time() - 30 * 86_400
        _ = os.utime(outside, (instant, instant))
        _ = os.utime(link, (instant, instant), follow_symlinks=False)
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(notes.exists())
        self.assertTrue(directory.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.exists())

    def test_pruning_runs_before_window_lookup(self) -> None:
        old = self.make_clip("old.mp4", 100, 8)
        recent = self.make_clip("recent.mp4", 100, 6)
        self.settings["root_fail"] = True
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertIn("1 older than 7 days", result.stderr)
        self.assertEqual(self.calls_for("ffmpeg"), [])

    def test_default_directory_is_under_temporary_home(self) -> None:
        del self.environment["SCREEN_RECORD_DIR"]
        result = self.run_script("--window", "debug", "--seconds", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        clip = Path(result.stdout.strip())
        self.assertEqual(clip.parent, self.root / ".cache" / "screen-record")
        self.assertTrue(clip.exists())


if __name__ == "__main__":
    _ = unittest.main()
