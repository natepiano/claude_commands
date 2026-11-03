#!/usr/bin/env python3
"""App starts reach the log once and need no memory admission."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator, Mapping
from contextlib import ExitStack, closing, contextmanager, redirect_stderr
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, cast, override
from unittest import mock

import index
import launches
import record
import report
import store
from test_index import Record, encode, point_root_at, step
from test_launches import CLI, result_line
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard

if TYPE_CHECKING:
    from ..hana_shot import hana_shot
else:
    from scripts.hana_shot import hana_shot


class RecordingStopped(BaseException):
    """Recording stopped before it could finish its durable retry record."""


class RecordingStop(Enum):
    AFTER_PENDING_WRITE = "after pending write"
    AFTER_PUBLICATION = "after publication"
    BEFORE_PENDING_DELETE = "before pending delete"
    FAILED_FIRST_PUBLICATION = "failed first publication"


class LaunchRecordingTests(unittest.TestCase):
    root: Path = Path()
    worktree: Path = Path()
    binary: Path = Path()
    cache: Path = Path()

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = base / "buildlog"
        self.cache = base / "cache"
        self.worktree = base / "hana-fixture"
        (self.worktree / "crates" / "hana").mkdir(parents=True)
        self.binary = self.worktree / "target" / "debug" / "hana"
        self.binary.parent.mkdir(parents=True)
        _ = self.binary.write_bytes(b"fixture binary; never execute\n")
        _ = subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(self.worktree)],
            check=True, capture_output=True,
        )
        point_root_at(self, self.root)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "XDG_CACHE_HOME": str(self.cache),
            "BUILDLOG_LAUNCH_STATES": str(self.cache / "hana-shot" / "launches"),
            "BUILDLOG_TRANSCRIPTS": str(base / "transcripts"),
            "BUILDLOG_MEM_WAIT_S": "999",
            "CODEX_THREAD_ID": "launch-fixture-thread",
            "CLAUDE_CODE_SESSION_ID": "unrelated-claude-session",
            "USER_ZONE": "UTC",
        })))

    def records(self) -> list[Record]:
        return [cast(Record, json.loads(line))
                for path in sorted(self.root.glob("*/*.jsonl"))
                for line in path.read_text().splitlines()]

    def saved_launch(self, port: int, started: str, **facts: object) -> Path:
        path = self.cache / "hana-shot" / "launches" / f"{port}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({
            "pid": port + 100_000,
            "port": port,
            "scratch": str(self.root.parent / f"scratch-{port}"),
            "log": str(self.root.parent / f"scratch-{port}" / "hana.log"),
            "binary": str(self.binary),
            "started": started,
            **facts,
        }))
        return path

    def structured_launch_transcript(self, path: Path) -> Path:
        transcript = Path(os.environ["BUILDLOG_TRANSCRIPTS"]) / "project" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({
            "type": "user", "sessionId": "recorded-session",
            "toolUseResult": {"stdout": path.read_text()},
            "mcpMeta": {"structuredContent": json.loads(path.read_text())},
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "hana-shot-launch", "content": path.read_text(),
            }]},
        }) + "\n")
        return transcript

    @contextmanager
    def fake_launch_process(self, port: int) -> Generator[subprocess.Popen[bytes]]:
        scratch = self.root.parent / f"new-scratch-{port}"
        scratch.mkdir()
        poll = mock.Mock(return_value=None)
        process = mock.Mock(pid=424_242 + port, poll=poll)

        def start(command: list[str], **kwargs: object) -> object:
            if command[-1] == str(self.binary):
                self.assertEqual(kwargs["cwd"], self.worktree)
                return process
            return cast(object, mock.DEFAULT)

        def stop(method: str, **kwargs: object) -> Record:
            self.assertEqual(method, "brp_extras/shutdown")
            self.assertIn("timeout", kwargs)
            poll.return_value = 0
            return {}

        with (
            mock.patch.object(hana_shot, "port_answers", return_value=False),
            mock.patch.object(hana_shot, "uncommitted_build_paths", return_value=[]),
            mock.patch.object(hana_shot, "wait_ready"),
            mock.patch.object(tempfile, "mkdtemp", return_value=str(scratch)),
            mock.patch.object(subprocess, "Popen", side_effect=start, wraps=subprocess.Popen),
            mock.patch.object(hana_shot, "Brp", return_value=mock.Mock(call=mock.Mock(side_effect=stop))),
            redirect_stderr(io.StringIO()),
        ):
            yield cast(subprocess.Popen[bytes], process)

    def launch_without_process(self, port: int) -> hana_shot.LaunchState:
        with self.fake_launch_process(port):
            launched = hana_shot.launch(port, str(self.worktree), str(self.binary))
        return launched[0]

    def test_hana_launch_records_one_start_with_no_admission(self) -> None:
        state = self.launch_without_process(15803)

        rows = self.records()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["step"], "launch")
        self.assertEqual(row["caller"], "hana-shot")
        self.assertEqual(row["argv"], [str(self.binary)])
        self.assertEqual(row["cwd"], str(self.worktree))
        self.assertEqual(row["session"], "launch-fixture-thread")
        self.assertEqual(row["started_at"], state["started"])
        self.assertIsNone(row["mem_wait_s"])
        source = hana_shot.launch_state_path(15803)
        original = source.read_bytes()
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(self.records(), rows)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(source.parent.glob("*.json")), [source])

    def test_launch_ignores_obsolete_wait_and_admission_record_fields(self) -> None:
        admission = self.root / "admission" / "launch-fixture.json"
        admission.parent.mkdir(parents=True)
        _ = admission.write_text(json.dumps({"mem_wait_s": 37}))
        original = admission.read_bytes()
        cases: tuple[Record, ...] = (
            {"mem_wait_s": 0}, {"mem_wait_s": 41},
            {"admission_record": str(admission)},
            {"mem_wait_s": 53, "admission_record": str(admission)},
        )
        for number, facts in enumerate(cases):
            with self.subTest(facts=facts):
                started = f"2026-10-17T12:0{number}:00+00:00"
                path = self.saved_launch(
                    15804 + number, started,
                    **facts, session="recorded-launch-session", cwd=str(self.worktree),
                )
                source = path.read_bytes()
                self.assertTrue(record.launch(path, self.worktree))
                row = next(row for row in self.records() if row["started_at"] == started)
                self.assertIsNone(row["mem_wait_s"])
                self.assertEqual(row["session"], "recorded-launch-session")
                self.assertFalse(record.launch(path, self.worktree))
                self.assertEqual(len(self.records()), number + 1)
                self.assertEqual(path.read_bytes(), source)
                self.assertEqual(admission.read_bytes(), original)

    def test_same_launch_recorded_twice_and_collected_twice_stays_one_row(self) -> None:
        source = self.saved_launch(
            15813, "2026-10-17T12:00:00+00:00", mem_wait_s=29,
            session="idempotent-launch-session", cwd=str(self.worktree),
        )
        original = source.read_bytes()
        self.assertTrue(record.launch(source, self.worktree))
        once = self.records()
        self.assertEqual(len(once), 1)
        self.assertIsNone(once[0]["mem_wait_s"])

        self.assertFalse(record.launch(source, self.worktree))
        self.assertEqual(self.records(), once)
        for rerun in (1, 2):
            with self.subTest(collection=rerun):
                self.assertEqual(launches.collect().added, 0)
                self.assertEqual(self.records(), once)

        self.assertEqual(index.update(), 1)
        with closing(index.read_only()) as connection:
            first_report = report.launches_section(connection, "2026-10-17")
            self.assertEqual(report.launches_section(connection, "2026-10-17"), first_report)
            self.assertEqual(connection.execute("SELECT count(*) FROM steps").fetchone(), (1,))
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.records(), once)
        self.assertEqual(source.read_bytes(), original)

    def test_stop_after_durable_transcript_backfill_then_rerun_records_each_launch_once(self) -> None:
        transcript = Path(os.environ["BUILDLOG_TRANSCRIPTS"]) / "project" / "app-session.jsonl"
        transcript.parent.mkdir(parents=True)
        source = "".join(result_line(
            "app-session", target, self.worktree, launched_as="app",
            ended_at=f"2026-10-17T12:0{number}:00+00:00",
        ) for number, target in enumerate(("editor", "preview")))
        _ = transcript.write_text(source)
        original = transcript.read_bytes()
        save = launches.save_launch

        def stop_after_first_write(row: Record) -> bool:
            saved = save(row)
            if saved:
                raise RuntimeError("fixture: stopped after the durable launch write")
            return saved

        with mock.patch.object(launches, "save_launch", side_effect=stop_after_first_write):
            with self.assertRaisesRegex(RuntimeError, "after the durable launch write"):
                _ = launches.collect()
        self.assertEqual(len(self.records()), 1)

        resumed = launches.collect()
        self.assertEqual(resumed.added, 1)
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["id"] for row in rows}, {
            "app-session:2026-10-17T12:00:00+00:00",
            "app-session:2026-10-17T12:01:00+00:00",
        })
        self.assertEqual(index.update(), 2)
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(index.update(), 0)
        self.assertEqual(transcript.read_bytes(), original)

    def test_collect_ignores_cached_launch_states_on_both_sides_of_the_old_cutoff(self) -> None:
        sources = [
            self.saved_launch(15800, "2026-10-14T23:59:59+00:00"),
            self.saved_launch(15801, "2026-10-15T00:00:00+00:00"),
            self.saved_launch(15802, "2026-10-17T12:00:00+00:00"),
        ]
        original = {path: path.read_bytes() for path in sources}

        first = launches.collect()

        self.assertEqual(first.added, 0)
        self.assertEqual(self.records(), [])
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(self.records(), [])
        self.assertEqual({path: path.read_bytes() for path in sources}, original)

    def test_importing_hana_shot_leaves_the_import_path_and_record_name_unchanged(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "\n".join((
                "import json, sys",
                "before = list(sys.path)",
                "import scripts.hana_shot.hana_shot",
                "print(json.dumps({'before': before, 'after': sys.path, 'record': 'record' in sys.modules}))",
            ))],
            cwd=Path(__file__).resolve().parents[2],
            env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        imports = cast(Record, json.loads(result.stdout))
        self.assertEqual(imports["after"], imports["before"])
        self.assertIs(imports["record"], False)

    def test_signal_during_recording_shuts_down_the_fake_app_and_removes_launch_state(self) -> None:
        recorder_path = str(Path(record.__file__).resolve())

        def interrupt_recording(*profile_event: object) -> None:
            frame = cast(FrameType, profile_event[0])
            if (profile_event[1] == "call" and frame.f_code.co_name == "launch"
                    and frame.f_code.co_filename == recorder_path):
                raise hana_shot.ExitSignal(signal.SIGTERM)

        port = 15815
        with self.fake_launch_process(port) as process:
            previous_profile = sys.getprofile()
            try:
                sys.setprofile(interrupt_recording)
                with self.assertRaises(hana_shot.ExitSignal) as caught:
                    _ = hana_shot.launch(port, str(self.worktree), str(self.binary))
            finally:
                sys.setprofile(previous_profile)

            self.assertEqual(caught.exception.number, signal.SIGTERM)
            self.assertEqual(process.poll(), 0)
            self.assertFalse(hana_shot.launch_state_path(port).exists())
            self.assertFalse((self.root.parent / f"new-scratch-{port}").exists())

    def assert_pending_recovery(self, pending: Path, stage: RecordingStop) -> None:
        source = self.saved_launch(15808, "2026-10-17T12:00:00+00:00", cwd=str(self.worktree))
        original = source.read_bytes()
        write = record.write_launch_line
        replace = os.replace
        unlink = Path.unlink

        def stop_after_publication(row: Mapping[str, object]) -> bool:
            added = write(row)
            if added:
                raise RecordingStopped(stage.value)
            return added

        def stop_after_pending_write(old: str | os.PathLike[str], new: str | os.PathLike[str]) -> None:
            replace(old, new)
            if Path(new).parent == pending:
                raise RecordingStopped(stage.value)

        def stop_before_pending_delete(path: Path, *, missing_ok: bool = False) -> None:
            if path.parent == pending and self.records():
                raise RecordingStopped(stage.value)
            unlink(path, missing_ok=missing_ok)

        with ExitStack() as cuts:
            match stage:
                case RecordingStop.AFTER_PENDING_WRITE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(
                        os, "replace", side_effect=stop_after_pending_write,
                    )))
                case RecordingStop.AFTER_PUBLICATION:
                    _ = cast(object, cuts.enter_context(mock.patch.object(
                        record, "write_launch_line", side_effect=stop_after_publication,
                    )))
                case RecordingStop.BEFORE_PENDING_DELETE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(
                        Path, "unlink", stop_before_pending_delete,
                    )))
                case RecordingStop.FAILED_FIRST_PUBLICATION:
                    _ = cast(object, cuts.enter_context(mock.patch.object(
                        record, "write_launch_line", side_effect=OSError("first publication failed"),
                    )))
            if stage is RecordingStop.FAILED_FIRST_PUBLICATION:
                self.assertFalse(record.launch(source, self.worktree))
            else:
                with self.assertRaises(RecordingStopped):
                    _ = record.launch(source, self.worktree)

        unpublished = stage in {RecordingStop.AFTER_PENDING_WRITE, RecordingStop.FAILED_FIRST_PUBLICATION}
        self.assertEqual(len(self.records()), 0 if unpublished else 1)
        self.assertTrue(pending.is_relative_to(self.root))
        self.assertEqual(len(list(pending.glob("*"))), 1)
        source.unlink()

        self.assertEqual(launches.collect().added, 1 if unpublished else 0)
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["mem_wait_s"])
        self.assertEqual(list(pending.glob("*")), [])
        self.assertEqual(index.update(), 1)
        _ = source.write_bytes(original)
        self.assertFalse(record.launch(source, self.worktree))
        self.assertEqual(self.records(), rows)
        self.assertEqual(list(pending.glob("*")), [])
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(index.update(), 0)
        self.assertEqual(source.read_bytes(), original)

    def test_stop_after_pending_write_recovers_one_launch(self) -> None:
        self.assert_pending_recovery(record.pending_launches_root(), RecordingStop.AFTER_PENDING_WRITE)

    def test_stop_after_publication_recovers_without_repeating_the_launch(self) -> None:
        self.assert_pending_recovery(record.pending_launches_root(), RecordingStop.AFTER_PUBLICATION)

    def test_stop_before_pending_delete_recovers_without_repeating_the_launch(self) -> None:
        self.assert_pending_recovery(record.pending_launches_root(), RecordingStop.BEFORE_PENDING_DELETE)

    def test_failed_first_publication_keeps_the_launch_for_collection(self) -> None:
        self.assert_pending_recovery(record.pending_launches_root(), RecordingStop.FAILED_FIRST_PUBLICATION)

    def test_failed_publication_survives_shutdown_and_collects_both_starts_once(self) -> None:
        publish = record.write_launch_line
        attempts = 0

        def fail_first_publication(row: Mapping[str, object]) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("first publication failed")
            return publish(row)

        with mock.patch.object(record, "write_launch_line", side_effect=fail_first_publication):
            for port in (15816, 15817):
                with self.fake_launch_process(port) as process:
                    _ = hana_shot.launch(port, str(self.worktree), str(self.binary))
                    self.assertTrue(hana_shot.launch_state_path(port).exists())
                    hana_shot.shutdown(port, process)
                    self.assertEqual(process.poll(), 0)
                    self.assertFalse(hana_shot.launch_state_path(port).exists())

        pending = record.pending_launches_root()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(list(pending.glob("*"))), 1)
        self.assertEqual(launches.collect().added, 1)
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertTrue(all(row["mem_wait_s"] is None for row in rows))
        self.assertEqual(list(pending.glob("*")), [])
        self.assertEqual(index.update(), 2)
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(self.records(), rows)
        self.assertEqual(index.update(), 0)

    def test_structured_hana_output_is_ignored_while_legacy_brp_still_collects(self) -> None:
        path = self.saved_launch(
            15810, "2026-10-17T12:00:00+00:00", mem_wait_s=17,
            cwd=str(self.worktree), session="recorded-session",
        )
        transcript = self.structured_launch_transcript(path)
        original = {source: source.read_bytes() for source in (path, transcript)}

        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(self.records(), [])
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual({source: source.read_bytes() for source in original}, original)

        with transcript.open("a") as handle:
            _ = handle.write(result_line("legacy-session", "editor", self.worktree))
        self.assertEqual(launches.collect().added, 1)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["caller"], "brp-launch")
        self.assertEqual(launches.collect().added, 0)

    def test_hostless_ssh_output_does_not_repeat_a_live_launch_under_the_collecting_host(self) -> None:
        source = self.saved_launch(
            15814, "2026-10-17T12:00:00+00:00", cwd=str(self.worktree),
        )
        with mock.patch.object(store, "host_name", return_value="launching-host"):
            self.assertTrue(record.launch(source, self.worktree))
        once = self.records()
        self.assertEqual(len(once), 1)
        self.assertEqual(once[0]["host"], "launching-host")
        transcript = self.structured_launch_transcript(source)
        original = transcript.read_bytes()

        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(self.records(), once)
        self.assertEqual(launches.collect().added, 0)
        self.assertEqual(transcript.read_bytes(), original)

    def test_report_labels_launch_rows_no_admission_and_preserves_legacy_wait_wording(self) -> None:
        source = self.root / store.host_name() / "2026-10.jsonl"
        source.parent.mkdir(parents=True)
        on_day: Record = {
            "host": store.host_name(), "started_at": "2026-10-17T12:00:00Z",
            "ended_at": "2026-10-17T12:00:00Z", "cwd": str(self.worktree),
        }
        _ = source.write_bytes(b"".join(encode(row) for row in (
            step("hana-launch", **on_day, step="launch", caller="hana-shot", mem_wait_s=None),
            step("legacy-launch", **on_day, step="build", caller="brp-launch", mem_wait_s=41),
            step("unknown-launch", **on_day, step="launch", caller="hana-shot", mem_wait_s=None),
            step("legacy-unknown", **on_day, step="build", caller="brp-launch", mem_wait_s=None),
            step("ordinary-build", **on_day, step="build", caller="verify", mem_wait_s=999),
            step("other-day", step="launch", caller="hana-shot", mem_wait_s=1234,
                 started_at="2026-10-18T12:00:00Z", ended_at="2026-10-18T12:00:00Z"),
        )))
        original = source.read_bytes()
        self.assertEqual(index.update(), 6)

        with closing(index.read_only()) as connection:
            section = report.launches_section(connection, "2026-10-17")
            rendered = report.report(connection, "2026-10-17")

        self.assertIn("### App launches", section)
        self.assertTrue(any("Launches: 4" in line for line in section), section)
        self.assertIn("\n".join(section), rendered)
        rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
                for line in section if line.startswith("| ")]
        hana = next(row for row in rows if row[0] == "app launches (hana_shot)")
        legacy = next(row for row in rows if row[0] == "example launches (brp)")
        self.assertEqual(hana[1], "2")
        self.assertIn("no admission", " ".join(hana))
        self.assertNotIn("not recorded", " ".join(hana))
        self.assertEqual(legacy[1], "2")
        self.assertIn("41.0 s", legacy)
        self.assertTrue(any("not recorded" in line for line in section), section)
        self.assertEqual(source.read_bytes(), original)

    def test_repeated_report_commands_keep_live_launch_counts_and_no_admission(self) -> None:
        sources = [
            self.saved_launch(15811, "2026-10-17T12:00:00+00:00", mem_wait_s=37),
            self.saved_launch(15812, "2026-10-17T12:01:00+00:00"),
        ]
        original = {path: path.read_bytes() for path in sources}
        for path in sources:
            self.assertTrue(record.launch(path, self.worktree))
        environment = {**os.environ, "TZ": "UTC"}

        first = subprocess.run(
            [sys.executable, str(CLI), "report", "2026-10-17"], env=environment,
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Launches: 2", first.stdout)
        self.assertIn("no admission", first.stdout)
        self.assertNotIn("37.0 s", first.stdout)
        self.assertIn("(0 new", first.stderr)
        with closing(index.read_only()) as connection:
            rows = cast(list[tuple[object, ...]], connection.execute(
                "SELECT step, mem_wait_s FROM steps ORDER BY started_at",
            ).fetchall())
        self.assertEqual(rows, [("launch", None), ("launch", None)])

        repeated = subprocess.run(
            [sys.executable, str(CLI), "report", "2026-10-17"], env=environment,
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("(0 new", repeated.stderr)
        self.assertEqual(repeated.stdout, first.stdout)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual({path: path.read_bytes() for path in original}, original)


if __name__ == "__main__":
    _ = unittest.main()
