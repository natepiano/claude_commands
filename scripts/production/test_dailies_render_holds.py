"""Report and footer build holds read the same temporary holder files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
    mac_state: Path = Path()
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # HOME puts the renderer's AGENTS_DIR and READINGS_LOG under this test directory.
        (self.scratch / "rust/hanadocs/agents").mkdir(parents=True)
        (self.scratch / ".local/state/agent-notes").mkdir(parents=True)
        self.folder = self.scratch / "holders"
        self.folder.mkdir()
        self.mac_state = self.scratch / "mac-test"
        self.mac_state.mkdir()
        _ = (self.scratch / "meminfo").write_text("MemAvailable: 67108864 kB\n")

    def write_holder(self, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
        path = self.folder / name
        _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
        return path

    def write_mac_block(
        self,
        state: str = "active",
        *,
        ci: str = "off_by_this_block",
        reason: str = "renderer work",
    ) -> Path:
        record: dict[str, object] = {
            "version": 2,
            "holder": "alice",
            "for": reason,
            "since": "2026-10-04T16:50:00+00:00",
            "expires": "2026-10-05T18:30:00+00:00",
            "session": None,
            "showrunner": None,
            "state": state,
            "ci": ci,
        }
        if state == "pending":
            record["waiting_on"] = {"kind": "ci_job"}
        else:
            record["free_message"] = {"kind": "not_needed"}
        path = self.mac_state / "block.json"
        _ = path.write_text(json.dumps(record) + "\n")
        return path

    def write_cycle(
        self, states: list[tuple[str, str]], *,
        names: dict[str, str] | None = None, release_started_at: str = "2026-10-04T10:57:00-07:00",
    ) -> None:
        release = self.scratch / "release"
        directory = release / CYCLE_ID
        directory.mkdir(parents=True)
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        entries: list[dict[str, str]] = []
        for index, (session_id, delivery) in enumerate(states):
            entries.append({"session_id": session_id,
                            "name": (names or {}).get(session_id, f"session {index + 1}"),
                            "delivery": delivery})
        cycle: dict[str, object] = {
            "id": CYCLE_ID,
            "opened_at": "2026-10-04T10:55:00-07:00",
            "release_started_at": release_started_at,
            "holders": {"worker": {"since": "2026-10-04T10:56:00-07:00", "released_at": ""}},
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
                "MAC_TEST_STATE_DIR": str(self.mac_state),
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
        self.assertEqual([line for line in report if "Mac block" in line], [])
        self.assertEqual([line for line in footer if "Mac block" in line], [])

    def test_active_and_pending_mac_blocks_appear_in_report_and_footer(self) -> None:
        expected = {
            "active": "* Mac block: alice since 09:50 PDT, for renderer work - lifts Mon 11:30 PDT",
            "pending": "* Mac block pending: alice since 09:50 PDT, for renderer work - lifts Mon 11:30 PDT",
        }
        for state, line in expected.items():
            with self.subTest(state=state):
                _ = self.write_mac_block(state)
                self.assertIn(line, self.assert_ok(self.report(False)))
                self.assertIn(line, self.assert_ok(self.footer()))

    def test_uncertain_switch_adds_warning_to_active_and_pending_blocks(self) -> None:
        for state in ("active", "pending"):
            for ci in ("still_on", "off_unconfirmed"):
                with self.subTest(state=state, ci=ci):
                    _ = self.write_mac_block(state, ci=ci)
                    mac_line = next(
                        line for line in self.assert_ok(self.footer())
                        if line.startswith("* Mac block")
                    )
                    self.assertTrue(mac_line.endswith(" - CI can still use the Mac"))

    def test_mac_block_follows_hold_release_lines_and_precedes_agents(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        self.write_cycle([("first", "DeliveryFailed")])
        _ = self.write_mac_block()
        lines = self.assert_ok(self.footer())
        hold_index = next(index for index, line in enumerate(lines) if line.startswith("* build hold:"))
        release_index = lines.index("  * session 1 could not be told builds are released; tell it")
        mac_index = next(index for index, line in enumerate(lines) if line.startswith("* Mac block:"))
        agent_index = lines.index("* none active")
        self.assertLess(hold_index, release_index)
        self.assertLess(release_index, mac_index)
        self.assertLess(mac_index, agent_index)
        self.assertEqual(lines[-1], "* next dailies: 11:30 PDT - nothing needed")

    def test_mac_block_reason_with_plumbing_is_refused_with_recovery(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch), "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.folder),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "TZ": ZONE, "USER_ZONE": ZONE,
        }))
        _ = self.write_mac_block(reason="the tester handoff")
        suffix = "; have alice run /build:mac block again with other words"
        self.assert_refused(self.report(False), "tester", suffix)
        self.assert_refused(self.footer(), "tester", suffix)

    def test_unreadable_mac_block_is_reported_without_refusing_footer(self) -> None:
        path = self.mac_state / "block.json"
        cases: list[tuple[str, str]] = [
            ("malformed JSON", "{"),
            ("missing key", ""),
            ("timestamp without zone", ""),
        ]
        for name, content in cases:
            with self.subTest(case=name):
                if name == "malformed JSON":
                    _ = path.write_text(content)
                else:
                    _ = self.write_mac_block()
                    record = cast(dict[str, object], json.loads(path.read_text()))
                    if name == "missing key":
                        del record["holder"]
                    else:
                        record["expires"] = "2026-10-05T18:30:00"
                    _ = path.write_text(json.dumps(record))
                for lines in (
                    self.assert_ok(self.report(False)),
                    self.assert_ok(self.footer()),
                ):
                    self.assertIn(
                        f"* Mac block: its state file cannot be read ({path})",
                        lines,
                    )

    def test_release_directory_alone_is_not_a_holder(self) -> None:
        self.write_cycle([("first", "QueuedUnconfirmed")])
        report = self.assert_ok(self.report(False))
        footer = self.assert_ok(self.footer())
        self.assertFalse(any("build hold" in line or "QueuedUnconfirmed" in line for line in report))
        self.assertFalse(any("build hold" in line or "QueuedUnconfirmed" in line for line in footer))
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

    def test_a_release_shows_its_progress_and_names_only_sessions_to_act_on(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        states = [("first", "Delivered"), ("second", "NotAttempted"),
                  ("third", "QueuedUnconfirmed"), ("fourth", "DeliveryFailed"), ("fifth", "RecipientGone")]
        self.write_cycle(states)
        for lines in (self.assert_ok(self.report(True)), self.assert_ok(self.footer())):
            self.assertIn("* build hold: worker holds builds since 10:56 PDT for the focused test; "
                          + "released 10:57 PDT; the memory gate orders builds", lines)
            hold_index = next(index for index, line in enumerate(lines) if line.startswith("* build hold:"))
            self.assertEqual(lines[hold_index + 1:hold_index + 4], [
                f"  * session {number} could not be told builds are released; tell it" for number in (3, 4, 5)
            ])
            text = "\n".join(lines)
            for session_id, delivery in states:
                self.assertNotIn(f"[{session_id}]", text)
                self.assertNotIn(delivery, text)

    def test_a_session_whose_name_is_its_id_is_never_shown_by_that_id(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        session_id = "450ac99b-caed-49ba-8c6f-29016df421ab"
        self.write_cycle([(session_id, "DeliveryFailed")], names={session_id: session_id})
        lines = self.assert_ok(self.footer())
        self.assertIn("  * an unnamed session could not be told builds are released; tell it", lines)
        self.assertNotIn(session_id, "\n".join(lines))

    def test_a_hold_not_yet_released_keeps_its_expected_release(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test", "2026-10-04T11:15:00-07:00")
        self.write_cycle([("first", "NotAttempted")], release_started_at="")
        lines = self.assert_ok(self.footer())
        self.assertIn("* build hold: worker holds builds since 10:56 PDT for the focused test; "
                      + "expected to release 11:15 PDT (in 15 minutes)", lines)
        self.assertEqual([line for line in lines if line.startswith("  * ")], [])

    def test_footer_and_report_keep_hold_lines_when_release_record_is_damaged(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch), "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.folder),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "TZ": ZONE, "USER_ZONE": ZONE,
        }))
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        self.write_cycle([("first", "Delivered")])
        path = self.scratch / "release" / CYCLE_ID / "cycle.json"
        cycle = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(list[dict[str, str]], cycle["entries"])
        entries[0]["delivery"] = "broken"
        _ = path.write_text(json.dumps(cycle) + "\n")
        before = path.read_bytes()
        report = self.assert_ok(self.report(True))
        footer = self.assert_ok(self.footer())
        for lines in (report, footer):
            hold_index = next(index for index, line in enumerate(lines) if line.startswith("* build hold:"))
            error_line = "release record could not be read: invalid delivery for entry 0; /build:hold release sets it aside and ends the hold"
            self.assertIn("  * " + error_line, lines)
            self.assertLess(hold_index, next(index for index, line in enumerate(lines) if error_line in line))
        self.assertLess(next(index for index, line in enumerate(report) if error_line in line), report.index("* none active"))
        self.assertEqual(path.read_bytes(), before)

    def test_footer_reports_damaged_current_without_changing_it(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch), "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.folder),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "TZ": ZONE, "USER_ZONE": ZONE,
        }))
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        self.write_cycle([("first", "NotAttempted")])
        path = self.scratch / "release" / "current"
        content = b"invalid-current\n"
        _ = path.write_bytes(content)
        lines = self.assert_ok(self.footer())
        self.assertIn(
            "  * release record could not be read: invalid current hold cycle; /build:hold release sets it aside and ends the hold",
            lines,
        )
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(path.parent.glob("current.damaged-*")), [])

    def test_legacy_holder_renders_unknown_release(self) -> None:
        _ = (self.folder / "old-worker").write_text("old worker, 2026-10-04T10:56:00-07:00, the focused test\n")
        lines = self.assert_ok(self.report(True))
        holds = [line for line in lines if line.startswith("* build hold:")]
        self.assertEqual(len(holds), 1)
        self.assertIn("build hold: old-worker holds builds since 10:56 PDT", holds[0])
        self.assertIn("the focused test; no release time given", holds[0])

    def test_multiple_holders_ordered_with_known_past_future_and_unknown_eta(self) -> None:
        _ = self.write_holder("future", "2026-10-04T10:55:00-07:00", "the second test", "2026-10-04T11:15:00-07:00")
        _ = self.write_holder("past", "2026-10-04T10:50:00-07:00", "the first test", "2026-10-04T10:54:00-07:00")
        _ = self.write_holder("unknown", "2026-10-04T10:58:00-07:00", "the third test")
        expected = [
            "build hold: past holds builds since 10:50 PDT for the first test; expected to release 10:54 PDT (6 minutes overdue)",
            "build hold: future holds builds since 10:55 PDT for the second test; expected to release 11:15 PDT (in 15 minutes)",
            "build hold: unknown holds builds since 10:58 PDT for the third test; no release time given",
        ]
        report = self.assert_ok(self.report(True))
        footer = self.assert_ok(self.footer())
        self.assertEqual([line.removeprefix("* ") for line in report if line.startswith("* build hold:")], expected)
        self.assertEqual([line.removeprefix("* ") for line in footer if line.startswith("* build hold:")], expected)
        self.assertEqual(len([line for line in report if line.startswith("widget") and "build hold" in line]), 1)

    def test_known_release_on_next_day_shows_weekday(self) -> None:
        _ = self.write_holder("later", "2026-10-04T10:56:00-07:00", "the later test", "2026-10-05T09:30:00-07:00")
        lines = self.assert_ok(self.footer())
        self.assertTrue(any("expected to release Mon 09:30 PDT (in 1350 minutes)" in line for line in lines))

    def test_stale_unit_marker_after_last_release_is_refused(self) -> None:
        self.assert_refused(self.report(True), "build_hold", "holder")

    def test_active_holder_without_marked_unit_is_refused(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        self.assert_refused(self.report(False), "build_hold", "unit")

    def test_top_level_build_hold_is_refused_even_when_files_exist(self) -> None:
        _ = self.write_holder("worker", "2026-10-04T10:56:00-07:00", "the focused test")
        self.assert_refused(
            self.report(True, extra={"build_hold": {"since": "10:56", "for": "the focused test", "release": "11:15"}}),
            "build_hold",
            "holder",
        )

    def test_report_footer_and_marker_agree_after_partial_release(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch), "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.folder),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "TZ": ZONE, "USER_ZONE": ZONE,
        }))
        _ = self.write_holder("first", "2026-10-04T10:50:00-07:00", "the first test")
        _ = self.write_holder("second", "2026-10-04T10:56:00-07:00", "the second test")
        units = [unit(True), {**unit(True), "unit": "frame-time", "label": "frame"}]
        # The child joins its recorder before the temporary log is removed.
        child = (
            "import sys, threading; sys.path.insert(0, sys.argv.pop(1)); import build_hold; "
            "status = build_hold.main(sys.argv[1:]); "
            f"[thread.join(timeout={TEST_HANG_GUARD_SECS}) for thread in threading.enumerate() if thread.name == 'hold-event-recorder']; "
            "assert not any(thread.name == 'hold-event-recorder' for thread in threading.enumerate()); "
            "sys.exit(status)"
        )
        release = subprocess.run(
            [sys.executable, "-c", child, str(HOLD_SCRIPT.parent), "release", "--holder", "first"],
            capture_output=True, text=True, check=False, env=dict(os.environ))
        self.assertEqual(release.returncode, 0, release.stderr)
        self.assertIn("still held by second", release.stdout)
        self.assertEqual(len(list((self.scratch / "buildlog").rglob("*.jsonl"))), 1)
        report = self.assert_ok(self.report(True, extra={"units": units}))
        footer = self.assert_ok(self.footer())
        hold_lines = [line for line in report if line.startswith("* build hold:")]
        self.assertEqual(hold_lines, ["* build hold: second holds builds since 10:56 PDT for the second test; no release time given"])
        self.assertEqual([line for line in footer if line.startswith("* build hold:")], hold_lines)
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
