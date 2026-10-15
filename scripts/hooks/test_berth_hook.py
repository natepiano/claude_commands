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
        self.answer(envelope("clear", acquisition=acquired()))
        _ = self.state(AWAITING)

    def answer(self, value: dict[str, object]) -> None:
        _ = (self.root / "answer.json").write_text(json.dumps(value))

    def state(self, value: dict[str, object] | None = None) -> dict[str, object]:
        path = self.session_dir / phase_reservation.STATE_FILE
        if value is not None:
            _ = path.write_text(json.dumps(value))
        return cast(dict[str, object], json.loads(path.read_text()))

    def berth_calls(self) -> list[dict[str, object]]:
        log = self.root / "berth.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [cast(dict[str, object], json.loads(line)) for line in lines]

    def hook(self, patch: str, tool: str = "apply_patch", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": tool, "cwd": str(cwd or self.repo), "tool_input": {"command": patch}}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                              text=True, check=False, env=self.environment, timeout=60)

    def test_a_seat_first_touch_checks_its_paths_and_records_active(self) -> None:
        result = self.hook(patch_text("*** Add File: new.rs", "*** Update File: lib.rs"), cwd=self.repo / "src")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(self.berth_calls(), [{"argv": ["check", "src/new.rs", "src/lib.rs", "--json"],
                                               "cwd": str(self.repo), "session": DIRECTOR}])
        self.assertEqual(self.state(), {"kind": "active", "active_phase_reservation": {
            "reservation_id": RESERVATION, "coordination_run_id": RUN, "phase": "15", "phase_start_head": HEAD,
        }})

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

    def backstop(self) -> tuple[int, str]:
        _ = (self.session_dir / phase_reservation.BASELINE_FILE).write_text(
            subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                           capture_output=True, text=True).stdout)
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(output):
            code = phase_reservation.main(["record-active", "--session-dir", str(self.session_dir), "--repo", str(self.repo)])
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
                                               "cwd": str(self.repo), "session": DIRECTOR}])
        self.assertEqual(cast(dict[str, str], self.state()["active_phase_reservation"])["phase"], "15")

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
