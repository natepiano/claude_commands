"""The Codex berth hook and its checkpoint backstop, against a fake cargo-berth and temp repos."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "delegate"))
import phase_reservation  # noqa: E402

HOOK = Path(__file__).with_name("pre-tool-use-berth.py")
DIRECTOR = "sid-director"
RESERVATION = "01a13f9c-0000-7000-8000-000000000001"
RUN = "01a13f9c-0000-7000-8000-0000000000aa"
HEAD = "0123456789abcdef0123456789abcdef01234567"
FAKE_BERTH = r'''#!__PYTHON__
import json, os, sys
with open(os.environ["FAKE_BERTH_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                          "session": os.environ.get("CARGO_BERTH_SESSION_ID")}) + "\n")
print(open(os.environ["FAKE_BERTH_ANSWER"]).read())
'''
AWAITING: dict[str, object] = {"kind": "enrolled_awaiting_first_touch", "phase": "15"}


def envelope(status: str, message: str = "", acquisition: dict[str, str] | None = None) -> dict[str, object]:
    data: dict[str, object] = {"acquisition": acquisition} if acquisition else {}
    return {"verb": "check", "status": status, "message": message, "payload": {"kind": "check", "data": data}}


def acquired(reservation: str = RESERVATION) -> dict[str, str]:
    return {"kind": "appended", "reservation_id": reservation, "coordination_run_id": RUN, "phase_start_head": HEAD}


def patch_text(*headers: str) -> str:
    return "\n".join(["*** Begin Patch", *headers, "+line", "*** End Patch"])


class BerthHookTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    session_dir: Path = Path()
    bin_dir: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        for command in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@example.invalid"],
                        ["config", "gc.auto", "0"], ["config", "maintenance.auto", "false"]):
            _ = subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        _ = (self.repo / "src" / "lib.rs").write_text("fn a() {}\n")
        _ = subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "base"], check=True, capture_output=True)
        self.session_dir = self.root / "unit" / "run-1"
        self.session_dir.mkdir(parents=True)
        (self.root / "unit" / "active").mkdir()
        _ = (self.root / "unit" / "active" / DIRECTOR).write_text(f"{self.session_dir}\n")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        fake = self.bin_dir / "cargo-berth"
        _ = fake.write_text(FAKE_BERTH.replace("__PYTHON__", sys.executable))
        fake.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "CLAUDE_CODE_SESSION_ID": DIRECTOR,
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "unit" / "active"),
            "FAKE_BERTH_LOG": str(self.root / "berth.log"),
            "FAKE_BERTH_ANSWER": str(self.root / "answer.json"),
        }
        _ = self.environment.pop("CARGO_BERTH_SESSION_ID", None)
        # A test run from inside a seat inherits its run; each test names the run it wants.
        _ = self.environment.pop("PLAN_DELEGATE_BOARD_DIR", None)
        self.answer(envelope("clear", acquisition=acquired()))
        _ = self.state(AWAITING)

    def answer(self, value: dict[str, object]) -> None:
        _ = (self.root / "answer.json").write_text(json.dumps(value))

    def state(self, value: dict[str, object] | None = None, phase: str = "15") -> dict[str, object]:
        directory = phase_reservation.phase_directory(self.session_dir, phase)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / phase_reservation.STATE_FILE
        if value is not None:
            _ = path.write_text(json.dumps(value))
        return cast(dict[str, object], json.loads(path.read_text()))

    def open_second_phase(self) -> None:
        """Phase 16 opens beside 15; each lists its files."""
        _ = self.state({"kind": "enrolled_awaiting_first_touch", "phase": "16"}, phase="16")
        _ = (phase_reservation.phase_directory(self.session_dir, "15") / phase_reservation.FILES_FILE).write_text(
            "src/lib.rs\n")
        _ = (phase_reservation.phase_directory(self.session_dir, "16") / phase_reservation.FILES_FILE).write_text(
            "./src/panels/\ndocs/panels.md\n")

    def berth_calls(self) -> list[dict[str, object]]:
        log = self.root / "berth.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [cast(dict[str, object], json.loads(line)) for line in lines]

    def hook(self, patch: str, tool: str = "apply_patch", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": tool, "cwd": str(cwd or self.repo), "tool_input": {"command": patch}}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                              text=True, check=False, env=self.environment, timeout=60)

    def test_a_path_a_sibling_phase_holds_allows_the_edit_and_records_nothing(self) -> None:
        # The engine names the sibling's reservation and leaves this phase's session unmapped.
        answer = envelope("clear", acquisition=acquired())
        data = cast(dict[str, object], cast(dict[str, object], answer["payload"])["data"])
        cast(dict[str, object], data["acquisition"])["session_mapping_publication"] = {"status": "left_unmapped"}
        self.answer(answer)
        before = self.state()
        result = self.hook(patch_text("*** Update File: lib.rs"), cwd=self.repo / "src")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.state(), before)

    def answer_left_unmapped(self) -> None:
        """The engine names a reservation another session of the run holds, and leaves the phase's unmapped."""
        answer = envelope("clear", acquisition=acquired())
        data = cast(dict[str, object], cast(dict[str, object], answer["payload"])["data"])
        cast(dict[str, object], data["acquisition"])["session_mapping_publication"] = {"status": "left_unmapped"}
        self.answer(answer)

    def map_director_session(self, reservation: str = RESERVATION) -> None:
        """cargo-berth's session map names `reservation` for the director's bare session id."""
        ledger = self.repo / ".git" / phase_reservation.LEDGER_DIRECTORY
        ledger.mkdir()
        _ = (ledger / phase_reservation.SESSION_MAP_FILE).write_text(json.dumps({"identities": {
            DIRECTOR: {"coordination_run_id": RUN, "reservation_id": reservation}}}))

    def test_with_one_open_phase_the_directors_own_reservation_is_recorded_active(self) -> None:
        # The director edited the plan doc itself, so its reservation is mapped to the bare session id.
        self.answer_left_unmapped()
        self.map_director_session()
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        code, written = self.backstop()
        self.assertEqual(code, 0)
        self.assertIn(f"Active, reservation {RESERVATION}", written)
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["reservation_id"], RESERVATION)

    def test_the_hook_records_the_directors_own_reservation_with_one_open_phase(self) -> None:
        self.answer_left_unmapped()
        self.map_director_session()
        result = self.hook(patch_text("*** Update File: lib.rs"), cwd=self.repo / "src")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.state()["kind"], "active")

    def test_a_left_unmapped_reservation_stays_unrecorded_unless_the_map_names_it_for_one_open_phase(self) -> None:
        self.answer_left_unmapped()
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        for case in ("another reservation mapped", "a sibling phase open"):
            with self.subTest(case=case):
                if case == "another reservation mapped":
                    self.map_director_session(reservation="01a13f9c-0000-7000-8000-000000000002")
                else:
                    shutil.rmtree(self.repo / ".git" / phase_reservation.LEDGER_DIRECTORY)
                    self.map_director_session()
                    self.open_second_phase()
                code, written = self.backstop()
                self.assertEqual(code, 0)
                self.assertIn("a sibling phase's reservation holds every changed path", written)
                self.assertEqual(self.state(), AWAITING)

    def test_a_seat_first_touch_checks_its_paths_and_records_active(self) -> None:
        result = self.hook(patch_text("*** Add File: new.rs", "*** Update File: lib.rs"), cwd=self.repo / "src")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.berth_calls(), [{"argv": ["check", "src/new.rs", "src/lib.rs", "--json"],
                                               "cwd": str(self.repo), "session": f"{DIRECTOR}.phase-15"}])
        self.assertEqual(self.state(), {"kind": "active", "active_phase_reservation": {
            "reservation_id": RESERVATION, "coordination_run_id": RUN, "phase": "15", "phase_start_head": HEAD,
        }})

    def side_run(self) -> Path:
        """A second run of the same director, its phase 7 open; the marker still names run-1."""
        run = self.root / "unit" / "run-2"
        directory = phase_reservation.phase_directory(run, "7")
        directory.mkdir(parents=True)
        _ = (directory / phase_reservation.STATE_FILE).write_text(json.dumps(
            {"kind": "enrolled_awaiting_first_touch", "phase": "7"}))
        return run

    def test_a_seat_of_a_side_run_checks_its_own_runs_phase_not_the_markers(self) -> None:
        run = self.side_run()
        self.environment["PLAN_DELEGATE_BOARD_DIR"] = str(run)
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-7"])
        state_path = phase_reservation.phase_directory(run, "7") / phase_reservation.STATE_FILE
        self.assertEqual(cast(dict[str, object], json.loads(state_path.read_text()))["kind"], "active")
        self.assertEqual(self.state(), AWAITING)

    def test_without_a_board_dir_the_marker_names_the_run(self) -> None:
        _ = self.side_run()
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-15"])
        self.assertEqual(self.state()["kind"], "active")

    def test_a_board_dir_that_is_gone_falls_back_to_the_marker(self) -> None:
        _ = self.side_run()
        self.environment["PLAN_DELEGATE_BOARD_DIR"] = str(self.root / "unit" / "ended-run")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-15"])
        self.assertEqual(self.state()["kind"], "active")

    def test_an_already_active_phase_keeps_its_reservation(self) -> None:
        active: dict[str, object] = {"kind": "active", "active_phase_reservation": {
            "reservation_id": "earlier", "coordination_run_id": RUN, "phase": "15", "phase_start_head": HEAD}}
        _ = self.state(active)
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(len(self.berth_calls()), 1)
        self.assertEqual(self.state(), active)

    def test_a_blocked_check_refuses_the_edit_with_the_engine_reason(self) -> None:
        self.answer(envelope("blocked_by_overlap", "src/lib.rs is held by reservation 01a1 of run hana"))
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("blocked_by_overlap: src/lib.rs is held by reservation 01a1 of run hana.",
                      output["permissionDecisionReason"])
        self.assertEqual(self.state(), AWAITING)

    def test_an_unenrolled_repository_is_allowed_silently(self) -> None:
        self.answer(envelope("unconfigured", "this repository has no cargo-berth configuration"))
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.state(), AWAITING)

    def test_a_codex_session_that_is_no_seat_is_never_checked(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "sid-not-a-unit"
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.berth_calls(), [])

    def test_other_tools_and_paths_outside_the_repository_are_not_checked(self) -> None:
        self.assertEqual(self.hook(patch_text("*** Update File: src/lib.rs"), tool="shell").stdout, "")
        outside = self.hook(patch_text(f"*** Add File: {self.root / 'scratch' / 'notes.md'}"))
        self.assertEqual((outside.returncode, outside.stdout), (0, ""), outside.stderr)
        self.assertEqual(self.berth_calls(), [])

    def test_a_missing_engine_refuses_the_edit(self) -> None:
        git_only = self.root / "git-only"
        git_only.mkdir()
        git = shutil.which("git")
        assert git is not None
        (git_only / "git").symlink_to(git)
        self.environment["PATH"] = str(git_only)
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("not_installed: the cargo-berth binary is not on PATH.", output["permissionDecisionReason"])

    def test_patch_paths_names_every_written_deleted_and_moved_path_once(self) -> None:
        patch = patch_text("*** Update File: a.rs", "*** Move to: b.rs", "*** Delete File: c.rs",
                           "*** Add File: a.rs", "*** Update File: d.rs")
        self.assertEqual(phase_reservation.patch_paths(patch), ["a.rs", "b.rs", "c.rs", "d.rs"])

    def test_two_open_phases_each_record_active_from_the_paths_their_files_list_owns(self) -> None:
        self.open_second_phase()
        reservation_16 = "01a13f9c-0000-7000-8000-000000000016"
        self.answer(envelope("clear", acquisition=acquired(reservation_16)))
        result = self.hook(patch_text("*** Add File: src/panels/info.rs", "*** Update File: docs/panels.md"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["argv"] for call in self.berth_calls()],
                         [["check", "src/panels/info.rs", "docs/panels.md", "--json"]])
        self.assertEqual(self.state(), AWAITING)
        self.assertEqual(cast(dict[str, str], self.state(phase="16")["active_phase_reservation"]),
                         {"reservation_id": reservation_16, "coordination_run_id": RUN, "phase": "16",
                          "phase_start_head": HEAD})
        result = self.hook(patch_text("*** Update File: src/lib.rs", "*** Update File: src/panels/info.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["argv"] for call in self.berth_calls()[1:]],
                         [["check", "src/lib.rs", "--json"], ["check", "src/panels/info.rs", "--json"]])
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["phase"], "15")

    def test_with_two_open_phases_a_path_neither_files_list_owns_is_refused(self) -> None:
        self.open_second_phase()
        result = self.hook(patch_text("*** Update File: src/lib.rs", "*** Add File: src/other.rs"))
        output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("src/other.rs is in no single open phase's files list (open phases: 15, 16)",
                      output["permissionDecisionReason"])
        self.assertEqual((self.berth_calls(), self.state()), ([], AWAITING))

    def test_a_session_from_before_phase_directories_moves_its_files_into_its_phase(self) -> None:
        shutil.rmtree(self.session_dir / phase_reservation.PHASES_DIR)
        _ = (self.session_dir / phase_reservation.STATE_FILE).write_text(json.dumps(AWAITING))
        _ = (self.session_dir / phase_reservation.BASELINE_FILE).write_text(HEAD + "\n")
        _ = (self.session_dir / phase_reservation.BASELINE_STATUS_FILE).write_text("")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["reservation_id"], RESERVATION)
        phase = phase_reservation.phase_directory(self.session_dir, "15")
        self.assertEqual(sorted(path.name for path in phase.iterdir()),
                         [phase_reservation.STATE_FILE, phase_reservation.BASELINE_FILE,
                          phase_reservation.BASELINE_STATUS_FILE])
        self.assertEqual(sorted(path.name for path in self.session_dir.iterdir()), [phase_reservation.PHASES_DIR])

    def backstop(self, phase: str = "15") -> tuple[int, str]:
        _ = (phase_reservation.phase_directory(self.session_dir, phase) / phase_reservation.BASELINE_FILE).write_text(
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                           capture_output=True, text=True).stdout)
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(output):
            code = phase_reservation.main(["record-active", "--session-dir", str(self.session_dir),
                                           "--phase", phase, "--repo", str(self.repo)])
        return code, output.getvalue()

    def test_the_backstop_records_active_from_every_change_since_the_baseline(self) -> None:
        code, unchanged = self.backstop()
        self.assertEqual(code, 0)
        self.assertIn("still awaiting first touch", unchanged)
        self.assertEqual(self.berth_calls(), [])
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        _ = (self.repo / "src" / "added.rs").write_text("fn c() {}\n")
        code, written = self.backstop()
        self.assertEqual(code, 0)
        self.assertIn(f"Active, reservation {RESERVATION}", written)
        self.assertEqual(self.berth_calls(), [{"argv": ["check", "src/lib.rs", "src/added.rs", "--json"],
                                               "cwd": str(self.repo), "session": f"{DIRECTOR}.phase-15"}])
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["phase"], "15")

    def test_the_backstop_checks_only_the_paths_its_phase_owns(self) -> None:
        self.open_second_phase()
        (self.repo / "src" / "panels").mkdir()
        _ = (self.repo / "src" / "panels" / "info.rs").write_text("fn p() {}\n")
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        code, written = self.backstop(phase="16")
        self.assertEqual(code, 0)
        self.assertIn(f"Active, reservation {RESERVATION}", written)
        self.assertEqual([call["argv"] for call in self.berth_calls()], [["check", "src/panels/info.rs", "--json"]])
        self.assertEqual((self.state(), self.state(phase="16")["kind"]), (AWAITING, "active"))

    def test_the_backstop_leaves_every_other_state_alone(self) -> None:
        unenrolled: dict[str, object] = {"kind": "repository_not_enrolled", "phase": "15"}
        _ = self.state(unenrolled)
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        code, output = self.backstop()
        self.assertEqual((code, self.state(), self.berth_calls()), (0, unenrolled, []))
        self.assertIn("repository_not_enrolled; nothing to record", output)

    def test_the_backstop_fails_on_a_refused_check(self) -> None:
        self.answer(envelope("blocked_by_overlap", "held elsewhere"))
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, _ = self.backstop()
        self.assertEqual(code, 1)
        self.assertIn("answered blocked_by_overlap: held elsewhere", errors.getvalue())
        self.assertEqual(self.state(), AWAITING)


if __name__ == "__main__":
    _ = unittest.main()
