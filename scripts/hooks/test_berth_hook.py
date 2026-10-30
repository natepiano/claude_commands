"""The Codex berth hook and its checkpoint backstop, against a fake cargo-berth and temp repos."""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from collections.abc import Callable
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "direct"))
import phase_reservation  # noqa: E402
from scripts.astra import design_roles  # noqa: E402

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
board = os.environ["FAKE_BERTH_ANSWER"] + ".board"
print(open(board if sys.argv[1] == "board" and os.path.exists(board) else os.environ["FAKE_BERTH_ANSWER"]).read())
'''
AWAITING: dict[str, object] = {"kind": "enrolled_awaiting_first_touch", "phase": "15"}
EARLIER = "01a15ec6-0000-7000-8000-000000000001"
ACTIVE_EARLIER: dict[str, object] = {"kind": "active", "active_phase_reservation": {
    "reservation_id": EARLIER, "coordination_run_id": RUN, "phase": "15", "phase_start_head": "f" * 40}}


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
            "UNIT_ACTIVE_DIR": str(self.root / "unit" / "active"),
            "FAKE_BERTH_LOG": str(self.root / "berth.log"),
            "FAKE_BERTH_ANSWER": str(self.root / "answer.json"),
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership"),
            "DESIGN_ROLES_HOST": "fixture-host",
        }
        _ = self.environment.pop("CARGO_BERTH_SESSION_ID", None)
        # A test run from inside a seat inherits its run; each test names the run it wants.
        _ = self.environment.pop("UNIT_RUN_DIR", None)
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

    def claim_phase_worktree(self, phase: str, root: Path) -> None:
        directory = phase_reservation.phase_directory(self.session_dir, phase)
        state = directory / phase_reservation.STATE_FILE
        before = state.read_bytes()
        state.unlink()
        try:
            with mock.patch.dict(os.environ, self.environment, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                code = phase_reservation.main(["claim", "--session-dir", str(self.session_dir),
                                               "--phase", phase, "--repo", str(root)])
            self.assertEqual(code, 0, errors.getvalue())
        finally:
            _ = state.write_bytes(before)

    def berth_calls(self) -> list[dict[str, object]]:
        log = self.root / "berth.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [cast(dict[str, object], json.loads(line)) for line in lines]

    def hook(self, patch: str, tool: str = "apply_patch", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": tool, "cwd": str(cwd or self.repo), "tool_input": {"command": patch}}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                              text=True, check=False, env=self.environment, timeout=60)

    def bind_designer(self, directory: Path, phase: str) -> None:
        design_id = "00000000-0000-4000-8000-000000000036"
        binding = design_roles.DesignBoundToPhase(str(directory), phase, str(self.repo), ("src/lib.rs",))
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 2), "fixture-repo",
                                  str(self.repo), str(self.repo / "design.md"), "design",
                                  "2026-10-21T12:00:00+00:00", design_roles.UnitHolder("demo", "design-unit"), binding,
                                  (design_roles.RoleInstance("designer", "lasting-designer", design_roles.RoleOpen("designer-thread")),
                                   design_roles.RoleInstance("reviewer", "lasting-reviewer", design_roles.RoleNotOpened())))
        self.environment.update({"HELD_DESIGN_ID": design_id, "DESIGN_BINDING_GENERATION": "2"})
        with mock.patch.dict(os.environ, self.environment):
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        _ = (directory / "phases" / phase / "files").write_text("src/lib.rs\n")
        _ = (directory / "progress_history_state.json").write_text(json.dumps({
            "run_id": directory.name, "history_file": str(self.root / (directory.name + ".jsonl")),
            "open_phases": [{"id": phase, "instance_id": "instance-" + phase, "status": "open"}],
            "phase": {"id": phase, "instance_id": "instance-" + phase, "status": "open"},
        }))

    def held_hook(self, patch: str) -> str:
        decide = cast(Callable[[str], int], runpy.run_path(str(HOOK))["decide"])
        payload = json.dumps({"tool_name": "apply_patch", "cwd": str(self.repo), "tool_input": {"command": patch}})
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach(DIRECTOR)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(decide(payload), 0)
        return output.getvalue()

    def test_held_designer_uses_rebound_run_while_old_board_folder_still_exists(self) -> None:
        current = self.side_run()
        self.bind_designer(current, "7")
        self.environment["UNIT_RUN_DIR"] = str(self.session_dir)
        self.assertEqual(self.held_hook(patch_text("*** Update File: src/lib.rs")), "")
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-7"])
        self.assertEqual(self.state(), AWAITING)
        state = cast(dict[str, object], json.loads((current / "phases/7" / phase_reservation.STATE_FILE).read_text()))
        self.assertEqual(state["kind"], "active")

    def test_held_patch_rejects_stale_generation_and_both_unlisted_rename_paths_before_attribution(self) -> None:
        self.bind_designer(self.session_dir, "15")
        cases = (
            ("1", patch_text("*** Update File: src/lib.rs")),
            ("2", patch_text("*** Update File: src/lib.rs", "*** Move to: src/other.rs")),
            ("2", patch_text("*** Update File: src/other.rs", "*** Move to: src/lib.rs")),
            ("2", patch_text("*** Update File: src/lib.rs", "*** Add File: private.md")),
        )
        for generation, command in cases:
            with self.subTest(generation=generation, patch=command):
                self.environment["DESIGN_BINDING_GENERATION"] = generation
                refused = cast(dict[str, object], json.loads(self.held_hook(command)))
                self.assertEqual(cast(dict[str, object], refused["hookSpecificOutput"])["permissionDecision"], "deny")
                self.assertEqual(self.berth_calls(), [])
                self.assertEqual(self.state(), AWAITING)

    def test_held_designer_fails_closed_when_the_engine_cannot_read_its_ledger(self) -> None:
        self.bind_designer(self.session_dir, "15")
        self.answer(envelope("ledger_unreadable"))
        refused = cast(dict[str, object], json.loads(self.held_hook(patch_text("*** Update File: src/lib.rs"))))
        detail = cast(dict[str, object], refused["hookSpecificOutput"])
        self.assertEqual(detail["permissionDecision"], "deny")
        self.assertIn("ledger", str(detail["permissionDecisionReason"]))
        self.assertEqual(self.state(), AWAITING)

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
        self.environment["UNIT_RUN_DIR"] = str(run)
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
        self.environment["UNIT_RUN_DIR"] = str(self.root / "unit" / "ended-run")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-15"])
        self.assertEqual(self.state()["kind"], "active")

    def test_an_active_phase_keeps_the_reservation_its_check_still_names(self) -> None:
        _ = self.state(ACTIVE_EARLIER | {"active_phase_reservation": {
            "reservation_id": RESERVATION, "coordination_run_id": RUN, "phase": "15", "phase_start_head": HEAD}})
        before = self.state()
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(len(self.berth_calls()), 1)
        self.assertEqual(self.state(), before)

    def test_an_active_phase_follows_a_new_reservation_after_berth_released_its_own(self) -> None:
        """cargo-liner handler-unit phase 22: berth released 01a15ec6 at a shrink checkpoint, the next
        seat edit acquired 01a15ec8, and the record still named 01a15ec6, so drift exited 5."""
        _ = self.state(ACTIVE_EARLIER)
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"]),
                         {"reservation_id": RESERVATION, "coordination_run_id": RUN, "phase": "15",
                          "phase_start_head": HEAD})

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

    def test_an_unreadable_ledger_allows_the_edit_as_the_engine_does(self) -> None:
        # A scratch clone carries the berth configuration but no ledger (hana, 2026-10-20).
        self.answer(envelope("ledger_unreadable", "cargo-berth could not establish edit safety; "
                             + "editing is allowed because ledger loss fails open."))
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

    def recorder_phases(self, *phases: str) -> None:
        records = [{"id": phase, "status": "active"} for phase in phases]
        _ = (self.session_dir / "progress_history_state.json").write_text(json.dumps({
            "phase": records[-1] if records else {"id": "15", "status": "completed"},
            "open_phases": records[:-1]}))

    def map_phase_session(self, phase: str) -> None:
        """Berth's session map names the phase's own session, as a check acquired under it leaves it."""
        ledger = self.repo / ".git" / phase_reservation.LEDGER_DIRECTORY
        ledger.mkdir(exist_ok=True)
        _ = (ledger / phase_reservation.SESSION_MAP_FILE).write_text(json.dumps({"identities": {
            phase_reservation.phase_session_id(DIRECTOR, phase): {"reservation_id": RESERVATION,
                                                                   "coordination_run_id": RUN}}}))

    def hold_phase(self) -> None:
        """Phase 15's own session maps to a reservation the board shows active, with the phase baseline written."""
        self.map_phase_session("15")
        self.board("active")
        _ = (phase_reservation.phase_directory(self.session_dir, "15") / phase_reservation.BASELINE_FILE).write_text(
            HEAD + "\n")

    def assert_held_without_a_check(self) -> None:
        self.assertEqual([call["argv"] for call in self.berth_calls()],
                         [["board", "--reservation", RESERVATION, "--json"]])
        self.assertEqual(self.state()["active_phase_reservation"], {
            "reservation_id": RESERVATION, "coordination_run_id": RUN, "phase": "15", "phase_start_head": HEAD})

    def test_an_edit_in_a_phase_that_holds_its_reservation_runs_no_berth_check(self) -> None:
        # The check took 77 s on 2026-10-29; Codex stops this hook at 30 s and refused the edit.
        self.hold_phase()
        self.answer(envelope("timed_out", "cargo-berth check did not answer"))
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assert_held_without_a_check()

    def test_a_claim_for_a_phase_that_holds_its_reservation_runs_no_berth_check(self) -> None:
        self.hold_phase()
        _ = (phase_reservation.phase_directory(self.session_dir, "15") / phase_reservation.FILES_FILE).write_text(
            "src/lib.rs\n")
        self.answer(envelope("timed_out", "cargo-berth check did not answer"))
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(output):
            code = phase_reservation.main(["claim", "--session-dir", str(self.session_dir), "--phase", "15",
                                           "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertIn(f"Active, reservation {RESERVATION}", output.getvalue())
        self.assert_held_without_a_check()

    def test_a_check_waits_ninety_seconds_for_berth(self) -> None:
        self.recorder_phases("15")
        phase = phase_reservation.open_phases(self.session_dir)[0]
        timeouts: list[object] = []
        run = subprocess.run

        def stop_berth(argv: list[str], **options: object) -> object:
            if argv[0] != "cargo-berth":
                return run(argv, **options)  # pyright: ignore[reportCallIssue, reportArgumentType, reportUnknownVariableType]
            timeouts.append(options["timeout"])
            raise subprocess.TimeoutExpired(argv, 90)

        with mock.patch.object(subprocess, "run", side_effect=stop_berth):
            answer = phase_reservation.phase_check(self.repo, ["src/lib.rs"], DIRECTOR, phase)
        self.assertEqual((answer.status, timeouts), ("timed_out", [90]))

    def test_a_recorder_phase_never_enrolled_checks_under_the_bare_session(self) -> None:
        # FinalGate's `final` phase starts with no reservation: no state file and no berth mapping.
        self.recorder_phases("final")
        directory = phase_reservation.phase_directory(self.session_dir, "final")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [DIRECTOR])
        self.assertFalse((directory / phase_reservation.STATE_FILE).exists())
        # Once berth maps the phase's own session, the missing state file is a lost record.
        self.map_phase_session("final")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn(phase_reservation.repair_command(self.session_dir, "final", self.repo),
                      output["permissionDecisionReason"])

    def test_an_unreadable_recorder_state_checks_the_held_phases_without_a_traceback(self) -> None:
        state = self.session_dir / "progress_history_state.json"
        for content in ("not JSON", "[1]", "\udcff"):
            with self.subTest(content=content):
                _ = state.write_bytes(content.encode("utf-8", "surrogateescape"))
                result = self.hook(patch_text("*** Update File: src/lib.rs"))
                self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.berth_calls()[-1]["session"], f"{DIRECTOR}.phase-15")
                self.assertEqual(self.state()["kind"], "active")

    def test_a_checkpoint_released_phase_does_not_own_edits_during_plan_review(self) -> None:
        self.open_second_phase()
        self.recorder_phases("15", "16")
        directory = phase_reservation.phase_directory(self.session_dir, "15")
        _ = phase_reservation.record_active(directory, phase_reservation.Acquisition(RESERVATION, RUN, HEAD))
        phase_reservation.committed(directory, HEAD)
        self.assertTrue(phase_reservation.clear(directory, HEAD))
        # Phase 16 alone owns both the released phase's old file and an unlisted new file.
        result = self.hook(patch_text("*** Update File: src/lib.rs", "*** Add File: docs/new.md"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-16"])
        self.assertEqual(self.state(phase="16")["kind"], "active")
        self.assertFalse((directory / phase_reservation.STATE_FILE).exists())

    def test_reading_open_phases_starts_no_process(self) -> None:
        self.recorder_phases("15")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("process started")):
            phases = phase_reservation.open_phases(self.session_dir)
        self.assertEqual([phase.phase for phase in phases], ["15"])

    def test_a_lone_open_phase_owns_paths_once_listed_by_finished_phases(self) -> None:
        self.open_second_phase()
        directory = phase_reservation.phase_directory(self.session_dir, "16")
        (directory / phase_reservation.STATE_FILE).unlink()
        _ = (directory / phase_reservation.FILES_FILE).write_text("src/other.rs\n")
        self.recorder_phases("15")
        result = self.hook(patch_text("*** Add File: src/other.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [f"{DIRECTOR}.phase-15"])
        self.assertEqual(self.state()["kind"], "active")
        self.assertFalse((directory / phase_reservation.STATE_FILE).exists())

    def test_between_phases_a_finished_directory_is_not_a_lost_record(self) -> None:
        directory = phase_reservation.phase_directory(self.session_dir, "15")
        (directory / phase_reservation.STATE_FILE).unlink()
        self.recorder_phases()
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual([call["session"] for call in self.berth_calls()], [DIRECTOR])
        self.assertFalse((directory / phase_reservation.STATE_FILE).exists())

    def test_a_lone_open_phase_with_a_lost_record_owns_unlisted_paths(self) -> None:
        self.open_second_phase()
        directory = phase_reservation.phase_directory(self.session_dir, "15")
        (directory / phase_reservation.STATE_FILE).unlink()
        finished = phase_reservation.phase_directory(self.session_dir, "16")
        (finished / phase_reservation.STATE_FILE).unlink()
        self.recorder_phases("15")
        self.map_phase_session("15")
        result = self.hook(patch_text("*** Add File: src/other.rs"))
        self.assertTrue(result.stdout, "the lost record must refuse the edit")
        output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn(phase_reservation.repair_command(self.session_dir, "15", self.repo),
                      output["permissionDecisionReason"])
        self.assertNotIn("--phase 16", output["permissionDecisionReason"])
        self.assertEqual(self.berth_calls(), [])

    def test_a_missing_phase_record_refuses_with_the_ledger_repair_command(self) -> None:
        for sibling_open in (False, True):
            with self.subTest(sibling_open=sibling_open):
                _ = self.state(AWAITING)
                if sibling_open:
                    self.open_second_phase()
                self.recorder_phases(*(["15", "16"] if sibling_open else ["15"]))
                self.map_phase_session("15")
                directory = phase_reservation.phase_directory(self.session_dir, "15")
                (directory / phase_reservation.STATE_FILE).unlink()
                result = self.hook(patch_text("*** Update File: src/lib.rs"))
                output = cast(dict[str, dict[str, str]], json.loads(result.stdout))["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "deny")
                command = shlex.join(["python3", str(Path(__file__).resolve().parents[1] / "direct" / "phase_reservation.py"), "repair",
                                      "--session-dir", str(self.session_dir), "--phase", "15",
                                      "--repo", str(self.repo)])
                self.assertIn(command, output["permissionDecisionReason"])
                self.assertEqual(self.berth_calls(), [])
                self.assertFalse((directory / phase_reservation.STATE_FILE).exists())

    def test_a_completed_phases_files_do_not_block_an_explicit_live_owner(self) -> None:
        self.open_second_phase()
        self.recorder_phases("15")
        directory = phase_reservation.phase_directory(self.session_dir, "16")
        (directory / phase_reservation.STATE_FILE).unlink()
        _ = (directory / phase_reservation.FILES_FILE).write_text("src/lib.rs\n")
        result = self.hook(patch_text("*** Update File: src/lib.rs"))
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.state()["kind"], "active")

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

    def test_same_path_in_two_worktrees_checks_only_that_worktrees_phase(self) -> None:
        self.open_second_phase()
        other = self.root / "other-worktree"
        _ = subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "other", str(other)],
                           check=True, capture_output=True, timeout=60)
        self.claim_phase_worktree("15", self.repo)
        self.claim_phase_worktree("16", other)
        directory = phase_reservation.phase_directory(self.session_dir, "16")
        _ = (directory / phase_reservation.FILES_FILE).write_text("src/lib.rs\n")
        untouched = (directory / phase_reservation.STATE_FILE).read_bytes()

        first = self.hook(patch_text("*** Update File: src/lib.rs"), cwd=self.repo)

        self.assertEqual((first.returncode, first.stdout), (0, ""), first.stderr)
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["phase"], "15")
        self.assertEqual((directory / phase_reservation.STATE_FILE).read_bytes(), untouched)
        first_state = self.state()
        reservation_16 = "01a13f9c-0000-7000-8000-000000000016"
        self.answer(envelope("clear", acquisition=acquired(reservation_16)))

        second = self.hook(patch_text("*** Update File: src/lib.rs"), cwd=other)

        self.assertEqual((second.returncode, second.stdout), (0, ""), second.stderr)
        self.assertEqual(self.state(), first_state)
        self.assertEqual(cast(dict[str, str], self.state(phase="16")["active_phase_reservation"]),
                         {"reservation_id": reservation_16, "coordination_run_id": RUN, "phase": "16",
                          "phase_start_head": HEAD})
        self.assertEqual(self.berth_calls(), [
            {"argv": ["check", "src/lib.rs", "--json"], "cwd": str(self.repo), "session": f"{DIRECTOR}.phase-15"},
            {"argv": ["check", "src/lib.rs", "--json"], "cwd": str(other), "session": f"{DIRECTOR}.phase-16"},
        ])

    def test_edit_outside_all_claimed_phase_worktrees_checks_the_bare_session(self) -> None:
        other = self.root / "other-worktree"
        _ = subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "other", str(other)],
                           check=True, capture_output=True, timeout=60)
        self.claim_phase_worktree("15", other)
        directory = phase_reservation.phase_directory(self.session_dir, "15")
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

        result = self.hook(patch_text("*** Update File: src/lib.rs"), cwd=self.repo)

        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.berth_calls(), [
            {"argv": ["check", "src/lib.rs", "--json"], "cwd": str(self.repo), "session": DIRECTOR},
        ])
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}, before)

    def test_record_active_outside_the_phases_worktree_keeps_its_state(self) -> None:
        other = self.root / "other-worktree"
        _ = subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "other", str(other)],
                           check=True, capture_output=True, timeout=60)
        self.claim_phase_worktree("15", other)
        _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")

        code, output = self.backstop()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.state(), AWAITING)

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

    def board(self, status: str) -> None:
        _ = (self.root / "answer.json.board").write_text(json.dumps(
            {"verb": "board", "status": "board_ready", "payload": {"kind": "reservation", "data": {
                "reservation_id": EARLIER, "lifecycle": {"status": status}}}}))

    def test_the_backstop_keeps_an_active_record_whose_reservation_is_live(self) -> None:
        _ = self.state(ACTIVE_EARLIER)
        self.board("active")
        code, output = self.backstop()
        self.assertEqual((code, self.state()), (0, ACTIVE_EARLIER))
        self.assertIn(f"reservation {EARLIER} is active on the board", output)
        self.assertEqual([call["argv"] for call in self.berth_calls()],
                         [["board", "--reservation", EARLIER, "--json"]])

    def test_the_backstop_moves_a_released_reservation_to_the_phases_live_one(self) -> None:
        _ = self.state(ACTIVE_EARLIER)
        self.board("released")
        _ = (self.repo / "src" / "lib.rs").write_text("fn b() {}\n")
        code, output = self.backstop()
        self.assertEqual(code, 0)
        self.assertIn(f"Active, reservation {RESERVATION}; reservation {EARLIER} is released on the board", output)
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["reservation_id"], RESERVATION)
        self.assertEqual(self.berth_calls()[1]["argv"], ["check", "src/lib.rs", "--json"])

    def test_the_backstop_fails_when_no_live_reservation_replaces_a_released_one(self) -> None:
        _ = self.state(ACTIVE_EARLIER)
        self.board("released")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, _ = self.backstop()
        self.assertEqual((code, self.state()), (1, ACTIVE_EARLIER))
        self.assertIn(f"reservation {EARLIER} is released on the board, and nothing of this phase changed",
                      errors.getvalue())

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
