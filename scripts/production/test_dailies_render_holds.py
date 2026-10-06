"""Report and footer build holds read the same temporary holder files."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("dailies_render.py")
HOLD_SCRIPT = SCRIPT.parent.parent / "build_hold" / "build_hold.py"
AT = "2026-10-04T11:00"
ZONE = "America/Los_Angeles"
CYCLE_ID = "c" * 32


def unit(held: bool) -> dict[str, object]:
    fields: dict[str, object] = {
        "unit": "widget-enhancements",
        "label": "widget",
        "project": "panel widgets that work by keyboard and draw cleanly",
        "phase": "Phase 2 of 3: small text reads clearly",
        "started": "2026-10-04T08:50",
        "held": None,
        "update": "building the fix for small text",
        "eta": {"time": "12:40", "percent": 60},
    }
    if held:
        fields["build_hold"] = True
    return fields


class DailiesHoldTests(unittest.TestCase):
    folder: Path = Path()
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # HOME puts the renderer's AGENTS_DIR, READINGS_LOG and RUN_OUTS_LOG under this test directory.
        (self.scratch / "rust/hanadocs/agents").mkdir(parents=True)
        (self.scratch / ".local/state/agent-notes").mkdir(parents=True)
        self.folder = self.scratch / "holders"
        self.folder.mkdir()
        _ = (self.scratch / "meminfo").write_text("MemAvailable: 67108864 kB\n")

    def write_holder(self, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
        path = self.folder / name
        _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
        return path

    def write_cycle(self, states: list[tuple[str, str]], *, outcomes: dict[str, str] | None = None) -> None:
        release = self.scratch / "release"
        directory = release / CYCLE_ID
        directory.mkdir(parents=True)
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        entries: list[dict[str, str]] = []
        for index, (session_id, state) in enumerate(states):
            entry = {
                "session_id": session_id, "name": f"session {index + 1}", "state": state,
            }
            if state in {"DeliveryQueued", "DeliveryFailed"}:
                entry["attempted_at"] = "2026-10-04T10:57:00-07:00"
            elif state in {"ReleasedAwaitingAdmission", "NoAdmissionAck", "WaitingForMemory"}:
                entry["released_at"] = "2026-10-04T10:57:00-07:00"
            if state == "WaitingForMemory":
                entry["wait_started_at"] = "2026-10-04T10:58:00-07:00"
            if state == "MemoryGateReturned":
                entry["wait_ended_at"] = "2026-10-04T10:59:30-07:00"
                entry["outcome"] = (outcomes or {}).get(session_id, "Granted")
            entries.append(entry)
        cycle: dict[str, object] = {
            "id": CYCLE_ID,
            "opened_at": "2026-10-04T10:55:00-07:00",
            "release_started_at": "2026-10-04T10:57:00-07:00",
            "holders": {"seat": {"since": "2026-10-04T10:56:00-07:00", "released_at": ""}},
            "recipients": {session_id: f"session {index + 1}" for index, (session_id, _) in enumerate(states)},
            "entries": entries,
        }
        _ = (directory / "cycle.json").write_text(json.dumps(cycle) + "\n")

    def run_script(self, script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(script), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "HOME": str(self.scratch),
                "BUILD_HOLD_DIR": str(self.folder),
                "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
                "BUILDLOG_MEMINFO": str(self.scratch / "meminfo"),
            },
        )

    def report(self, held: bool, *, extra: dict[str, object] | None = None) -> subprocess.CompletedProcess[str]:
        fields: dict[str, object] = {
            "length": "simple",
            "zone": ZONE,
            "next_run": "11:30",
            "units": [unit(held)],
        }
        if extra is not None:
            fields.update(extra)
        path = self.scratch / "report.json"
        _ = path.write_text(json.dumps(fields))
        return self.run_script(SCRIPT, str(path), "--at", AT)

    def footer(self) -> subprocess.CompletedProcess[str]:
        return self.run_script(SCRIPT, "--footer", "--zone", ZONE, "--next-run", "11:30", "--nothing-needed", "--at", AT)

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def assert_refused(self, result: subprocess.CompletedProcess[str], *parts: str) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for part in parts:
            self.assertIn(part, result.stderr)

    def test_no_holder_has_no_hold_line_or_marker(self) -> None:
        report = self.assert_ok(self.report(False))
        footer = self.assert_ok(self.footer())
        self.assertEqual([line for line in report if "build hold" in line], [])
        self.assertEqual([line for line in footer if "build hold" in line], [])

    def test_release_directory_alone_is_not_a_holder(self) -> None:
        self.write_cycle([("first", "DeliveryQueued")])
        report = self.assert_ok(self.report(False))
        footer = self.assert_ok(self.footer())
        self.assertFalse(any("build hold" in line or "DeliveryQueued" in line for line in report))
        self.assertFalse(any("build hold" in line or "DeliveryQueued" in line for line in footer))
        status = self.run_script(HOLD_SCRIPT, "status")
        self.assertEqual(status.stdout.strip(), "no build hold")
        buildlog = SCRIPT.parent.parent / "buildlog"
        checked = subprocess.run(
            ["python3", "-c", "import rust_release; print(rust_release.build_hold_active())"],
            capture_output=True, text=True, check=False,
            env={**os.environ, "HOME": str(self.scratch), "PYTHONPATH": str(buildlog),
                 "BUILD_HOLD_DIR": str(self.folder), "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
                 "BUILDLOG_MEMINFO": str(self.scratch / "meminfo")},
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.strip(), "False")

    def test_footer_and_report_name_each_release_state_before_agents(self) -> None:
        _ = self.write_holder("seat", "2026-10-04T10:56:00-07:00", "the focused test")
        states = [
            ("first", "MemoryGateReturned"), ("second", "AwaitingRelease"),
            ("third", "DeliveryQueued"), ("fourth", "DeliveryFailed"),
            ("fifth", "RecipientGone"), ("sixth", "ReleasedAwaitingAdmission"),
            ("seventh", "WaitingForMemory"), ("eighth", "MemoryGateReturned"),
            ("ninth", "MemoryGateReturned"), ("tenth", "NoAdmissionAck"),
            ("eleventh", "NoRegistration"),
        ]
        outcomes = {"first": "Granted", "eighth": "TimedOut", "ninth": "MeminfoUnavailable"}
        self.write_cycle(states, outcomes=outcomes)
        for lines in (self.assert_ok(self.report(True)), self.assert_ok(self.footer())):
            hold_index = next(index for index, line in enumerate(lines) if line.startswith("build hold:"))
            for index, (session_id, state) in enumerate(states, start=1):
                expected = f"session {index} [{session_id}]: {state}"
                if state == "MemoryGateReturned":
                    expected += f"({outcomes[session_id]})"
                matches = [line for line in lines if line.strip() == expected]
                self.assertEqual(matches, ["  " + expected])
                self.assertGreater(lines.index(matches[0]), hold_index)
            self.assertTrue(any("next session 2 [second] at 11:00:30 PDT" in line for line in lines))

    def test_footer_and_report_keep_hold_lines_when_release_record_is_damaged(self) -> None:
        _ = self.write_holder("seat", "2026-10-04T10:56:00-07:00", "the focused test")
        self.write_cycle([("first", "ReleasedAwaitingAdmission")])
        path = self.scratch / "release" / CYCLE_ID / "cycle.json"
        cycle = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(list[dict[str, str]], cycle["entries"])
        entries[0]["released_at"] = "broken"
        _ = path.write_text(json.dumps(cycle) + "\n")
        before = path.read_bytes()
        report = self.assert_ok(self.report(True))
        footer = self.assert_ok(self.footer())
        for lines in (report, footer):
            hold_index = next(index for index, line in enumerate(lines) if line.startswith("build hold:"))
            error_line = "release record could not be read: invalid released_at for first; /build_hold release sets it aside and ends the hold"
            self.assertEqual([line.strip() for line in lines if error_line in line], [error_line])
            self.assertLess(hold_index, next(index for index, line in enumerate(lines) if error_line in line))
        self.assertLess(next(index for index, line in enumerate(report) if error_line in line), report.index("### Agents"))
        self.assertEqual(path.read_bytes(), before)

    def test_footer_reports_damaged_current_without_changing_it(self) -> None:
        _ = self.write_holder("seat", "2026-10-04T10:56:00-07:00", "the focused test")
        self.write_cycle([("first", "AwaitingRelease")])
        path = self.scratch / "release" / "current"
        content = b"invalid-current\n"
        _ = path.write_bytes(content)
        lines = self.assert_ok(self.footer())
        self.assertIn(
            "  release record could not be read: invalid current hold cycle; /build_hold release sets it aside and ends the hold",
            lines,
        )
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(path.parent.glob("current.damaged-*")), [])

    def test_legacy_holder_renders_unknown_release(self) -> None:
        _ = (self.folder / "old-seat").write_text("old seat, 2026-10-04T10:56:00-07:00, the focused test\n")
        lines = self.assert_ok(self.report(True))
        holds = [line for line in lines if line.startswith("build hold:")]
        self.assertEqual(len(holds), 1)
        self.assertIn("build hold: old-seat since 10:56 PDT", holds[0])
        self.assertIn("the focused test - release eta: unknown", holds[0])

    def test_multiple_holders_ordered_with_known_past_future_and_unknown_eta(self) -> None:
        _ = self.write_holder("future", "2026-10-04T10:55:00-07:00", "the second test", "2026-10-04T11:15:00-07:00")
        _ = self.write_holder("past", "2026-10-04T10:50:00-07:00", "the first test", "2026-10-04T10:54:00-07:00")
        _ = self.write_holder("unknown", "2026-10-04T10:58:00-07:00", "the third test")
        expected = [
            "build hold: past since 10:50 PDT, for the first test - release eta: 10:54 PDT (overdue 6 minutes)",
            "build hold: future since 10:55 PDT, for the second test - release eta: 11:15 PDT (15 minutes)",
            "build hold: unknown since 10:58 PDT, for the third test - release eta: unknown",
        ]
        report = self.assert_ok(self.report(True))
        footer = self.assert_ok(self.footer())
        self.assertEqual([line for line in report if line.startswith("build hold:")], expected)
        self.assertEqual([line for line in footer if line.startswith("build hold:")], expected)
        self.assertEqual(len([line for line in report if line.startswith("widget") and "build hold" in line]), 1)

    def test_known_release_on_next_day_shows_weekday(self) -> None:
        _ = self.write_holder("later", "2026-10-04T10:56:00-07:00", "the later test", "2026-10-05T09:30:00-07:00")
        lines = self.assert_ok(self.footer())
        self.assertIn("release eta: Mon 09:30 PDT (1350 minutes)", lines[0])

    def test_stale_unit_marker_after_last_release_is_refused(self) -> None:
        self.assert_refused(self.report(True), "build_hold", "holder")

    def test_active_holder_without_marked_unit_is_refused(self) -> None:
        _ = self.write_holder("seat", "2026-10-04T10:56:00-07:00", "the focused test")
        self.assert_refused(self.report(False), "build_hold", "unit")

    def test_top_level_build_hold_is_refused_even_when_files_exist(self) -> None:
        _ = self.write_holder("seat", "2026-10-04T10:56:00-07:00", "the focused test")
        self.assert_refused(
            self.report(True, extra={"build_hold": {"since": "10:56", "for": "the focused test", "release": "11:15"}}),
            "build_hold",
            "holder",
        )

    def test_report_footer_and_marker_agree_after_partial_release(self) -> None:
        _ = self.write_holder("first", "2026-10-04T10:50:00-07:00", "the first test")
        _ = self.write_holder("second", "2026-10-04T10:56:00-07:00", "the second test")
        units = [unit(True), {**unit(True), "unit": "frame-time", "label": "frame"}]
        release = self.run_script(HOLD_SCRIPT, "release", "--holder", "first")
        self.assertEqual(release.returncode, 0, release.stderr)
        self.assertIn("still held by second", release.stdout)
        report = self.assert_ok(self.report(True, extra={"units": units}))
        footer = self.assert_ok(self.footer())
        hold_lines = [line for line in report if line.startswith("build hold:")]
        self.assertEqual(hold_lines, ["build hold: second since 10:56 PDT, for the second test - release eta: unknown"])
        self.assertEqual([line for line in footer if line.startswith("build hold:")], hold_lines)
        for label in ("widget", "frame"):
            self.assertEqual(len([line for line in report if line.startswith(label) and "build hold" in line]), 1)
        self.assertFalse((self.folder / "first").exists())
        self.assertTrue((self.folder / "second").exists())

    def test_active_holder_cannot_mark_its_own_unit(self) -> None:
        _ = self.write_holder("frame-time", "2026-10-04T10:56:00-07:00", "the focused test")
        units = [unit(True), {**unit(True), "unit": "frame-time", "label": "frame"}]
        self.assert_refused(
            self.report(True, extra={"units": units}),
            "units.build_hold: frame-time holds the build hold itself; remove its marker",
        )


if __name__ == "__main__":
    _ = unittest.main()
