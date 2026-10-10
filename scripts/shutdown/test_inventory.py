"""Behavioral tests for account-scoped shutdown inventory discovery."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

import inventory
import account
import fake_tmux
import sessions
import showrunners
import unit_lookup
from fake_tmux import FakeSession


TARGET_LOGIN = "owner@example.com"
OTHER_LOGIN = "other@example.com"
DIRECTOR_ID = "11111111-1111-1111-1111-111111111111"
UNIT_ID = "22222222-2222-2222-2222-222222222222"
SEAT_ID = "aaaaaaaa-3333-3333-3333-333333333333"
DESKTOP_ID = "44444444-4444-4444-4444-444444444444"
OTHER_ID = "55555555-5555-5555-5555-555555555555"
UNKNOWN_ID = "66666666-6666-6666-6666-666666666666"
UNIT_PID = 41_002
SEAT_PID = 41_003
DESKTOP_PID = 41_004
OTHER_PID = 41_005
UNKNOWN_PID = 41_006
DIRECTOR_SHELL_PID = 51_001
DESKTOP_SHELL_PID = 51_004
CURRENT_PROCESS_START = "current-process-start"
FAKE_TMUX = Path(__file__).parents[1] / "production" / "fake_tmux.py"


@final
class InventoryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.notes = Path()
        self.records = Path()
        self.notifier = Path()
        self.history = Path()
        self.delegate = Path()
        self.checkout = Path()
        self.origin = Path()
        self.bin = Path()
        self.tmux_state = Path()
        self.target_config = Path()
        self.other_config = Path()
        self.director_pid = 0
        self.live_socket = socket.socket(socket.AF_UNIX)
        self.head = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.notes = self.root / "agents"
        self.records = self.root / "sessions"
        self.notifier = self.root / "notifier"
        self.history = self.root / "history"
        self.delegate = self.root / "delegate"
        self.checkout = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.bin = self.root / "bin"
        self.tmux_state = self.root / "tmux.json"
        self.target_config = self.root / "claude-owner"
        self.other_config = self.root / "claude-other"
        for directory in (
            self.home,
            self.notes,
            self.records,
            self.notifier,
            self.history / "runs",
            self.delegate / "active",
            self.bin,
            self.checkout,
            self.target_config,
            self.other_config,
        ):
            directory.mkdir(parents=True)

        environment = {
            **os.environ,
            "HOME": str(self.home),
            "AGENT_NOTES_DIR": str(self.notes),
            "NOTIFIER_SESSIONS_DIR": str(self.records),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.history),
            "SHUTDOWN_DELEGATE_ROOT": str(self.delegate),
            "UNIT_LOOKUP_TMUX": str(FAKE_TMUX),
            "FAKE_TMUX_STATE": str(self.tmux_state),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        patch_dict_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del patch_dict_context
        _home_context: object = self.enterContext(
            patch.object(Path, "home", return_value=self.home)
        )
        _notifier_context: object = self.enterContext(
            patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier)
        )
        _sessions_context: object = self.enterContext(
            patch.object(showrunners, "SESSIONS_DIR", self.records)
        )
        _live_context: object = self.enterContext(
            patch.object(sessions, "live_session", return_value=True)
        )
        _unit_live_context: object = self.enterContext(
            patch.object(unit_lookup, "live_session", return_value=True)
        )
        _process_environment_context = cast(
            object,
            self.enterContext(patch("inventory._process_environment", return_value={})),
        )
        _process_start_context: object = self.enterContext(
            patch("inventory._process_start", return_value=CURRENT_PROCESS_START)
        )

        self.director_pid = os.getpid()
        socket_path = self.root / "director.sock"
        self.live_socket.close()
        self.live_socket = socket.socket(socket.AF_UNIX)
        self.live_socket.bind(str(socket_path))
        self.addCleanup(self.live_socket.close)
        self.write_account(self.target_config, TARGET_LOGIN)
        self.write_account(self.other_config, OTHER_LOGIN)
        _ = (self.notes / "claude 2.md").write_text(
            f"---\nlogin: {TARGET_LOGIN.upper()}\nstate: active\n---\n",
            encoding="utf-8",
        )
        config_by_pid: dict[int, Path | None] = {
            self.director_pid: self.target_config,
            UNIT_PID: self.target_config,
            SEAT_PID: self.target_config,
            DESKTOP_PID: self.target_config,
            OTHER_PID: self.other_config,
            UNKNOWN_PID: None,
        }
        def config_dir_for_pid(pid: int) -> Path | None:
            return config_by_pid.get(pid)

        _account_context: object = self.enterContext(
            patch.object(
                account,
                "process_config_dir",
                side_effect=config_dir_for_pid,
            )
        )

        self.make_git_checkout()
        self.make_command_stand_ins()
        self.make_sessions(socket_path)
        self.make_production_state()
        self.make_delegate_state()
        self.make_snapshot()

    def write_account(self, config_dir: Path, login: str) -> None:
        _ = (config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": login}}),
            encoding="utf-8",
        )

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def make_git_checkout(self) -> None:
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "shutdown-unit", cwd=self.checkout)
        _ = self.git("config", "user.name", "Shutdown Test")
        _ = self.git("config", "user.email", "shutdown@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        _ = (self.checkout / "dirty.txt").write_text("base\n", encoding="utf-8")
        _ = self.git("add", "dirty.txt")
        _ = self.git("commit", "-m", "base")
        _ = self.git("push", "-u", "origin", "shutdown-unit")
        for number in (1, 2):
            _ = (self.checkout / f"ahead-{number}.txt").write_text(
                f"ahead {number}\n", encoding="utf-8"
            )
            _ = self.git("add", f"ahead-{number}.txt")
            _ = self.git("commit", "-m", f"ahead {number}")
        _ = (self.checkout / "dirty.txt").write_text("changed\n", encoding="utf-8")
        self.head = self.git("rev-parse", "HEAD")

    def write_executable(self, name: str, body: str) -> None:
        path = self.bin / name
        _ = path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)

    def make_command_stand_ins(self) -> None:
        parent_rows = {
            str(self.director_pid): [DIRECTOR_SHELL_PID, "claude"],
            str(DIRECTOR_SHELL_PID): [51_002, "zsh"],
            "51002": [1, ".ghostty-wrappe"],
            str(DESKTOP_PID): [DESKTOP_SHELL_PID, "claude"],
            str(DESKTOP_SHELL_PID): [51_005, "zsh"],
            "51005": [1, "ghostty"],
        }
        self.write_executable(
            "ps",
            """import json
import os
import sys

rows = json.loads(os.environ["SHUTDOWN_TEST_PARENT_ROWS"])
pid = sys.argv[-1]
row = rows.get(pid, [1, "unknown"])
formats = [sys.argv[index + 1] for index, token in enumerate(sys.argv[:-1]) if token == "-o"]
joined = ",".join(formats)
if "ppid=" in joined and "comm=" in joined:
    print(f"{row[0]} {row[1]}")
elif "ppid=" in joined:
    print(row[0])
elif "comm=" in joined:
    print(row[1])
""",
        )
        os.environ["SHUTDOWN_TEST_PARENT_ROWS"] = json.dumps(parent_rows)
        self.write_executable("agent-sessions-snapshot", "raise SystemExit(0)\n")

    def write_session(
        self,
        pid: int,
        session_id: str,
        name: str,
        cwd: Path,
        *,
        socket_path: Path | None = None,
        kind: str = "interactive",
        status: str = "idle",
        tmux: str = "",
        updated_at: int = 1,
        model: str = "opus-test",
        proc_start: str = CURRENT_PROCESS_START,
    ) -> None:
        transcript = self.home / ".claude" / "projects" / "fixture" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(
            "\n".join(
                (
                    json.dumps({"type": "assistant", "message": {"model": "old-model"}}),
                    json.dumps({"type": "assistant", "message": {"model": model}}),
                    "",
                )
            ),
            encoding="utf-8",
        )
        record: dict[str, object] = {
            "pid": pid,
            "sessionId": session_id,
            "name": name,
            "cwd": str(cwd),
            "kind": kind,
            "status": status,
            "tmux": tmux,
            "messagingSocketPath": str(socket_path or self.root / f"{pid}.sock"),
            "updatedAt": updated_at,
            "formerNames": [],
            "procStart": proc_start,
        }
        _ = (self.records / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")

    def make_sessions(self, director_socket: Path) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.write_session(
            self.director_pid,
            DIRECTOR_ID,
            "build-followups",
            plain,
            socket_path=director_socket,
            updated_at=60,
            model="director-model",
        )
        self.write_session(
            UNIT_PID,
            UNIT_ID,
            "shutdown-unit",
            self.checkout,
            tmux="old-label:@1.%4",
            updated_at=50,
            model="unit-model",
        )
        self.write_session(
            SEAT_PID,
            SEAT_ID,
            "review-seat",
            plain,
            kind="bg",
            updated_at=40,
        )
        self.write_session(
            DESKTOP_PID,
            DESKTOP_ID,
            "notes",
            plain,
            updated_at=30,
        )
        self.write_session(OTHER_PID, OTHER_ID, "other-account", plain, updated_at=20)
        self.write_session(UNKNOWN_PID, UNKNOWN_ID, "hidden-account", plain, updated_at=10)

    def make_production_state(self) -> None:
        production_doc = self.root / "build-followups-production.md"
        _ = production_doc.write_text("- **User zone:** America/Los_Angeles\n", encoding="utf-8")
        showrunner = self.notifier / "showrunner-build-followups"
        showrunner.mkdir()
        _ = (showrunner / "conf").write_text(
            "\n".join(
                (
                    f"TARGET=session:{DIRECTOR_ID}",
                    f"CHECK=zsh /opt/production_check.sh {production_doc}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        unit_timer = self.notifier / "unit-reminder"
        unit_timer.mkdir()
        _ = (unit_timer / "conf").write_text(f"TARGET=session:{UNIT_ID}\n", encoding="utf-8")
        other_timer = self.notifier / "other-reminder"
        other_timer.mkdir()
        _ = (other_timer / "conf").write_text(f"TARGET=session:{OTHER_ID}\n", encoding="utf-8")

        fake_tmux.write(
            self.tmux_state,
            {
                "$1": FakeSession(
                    label="unit-renamed",
                    panes=["%4"],
                    env={
                        "SHOWRUNNER_UNIT": "build-followups",
                        "SHOWRUNNER_UNIT_ID": "shutdown-unit",
                    },
                )
            },
        )
        for name, started_at, plan in (
            ("newer", 20, "docs/shutdown-plan.md"),
            ("older", 10, "docs/old-plan.md"),
        ):
            event = {
                "event_type": "run_started",
                "run_started_at": started_at,
                "main_agent": {"session_id": UNIT_ID},
                "working_dir": str(self.checkout),
                "plan_doc": plan,
                "branch": "shutdown-unit",
            }
            _ = (self.history / "runs" / f"{name}.jsonl").write_text(
                json.dumps(event) + "\n", encoding="utf-8"
            )

    def make_delegate_state(self) -> None:
        run_dir = self.delegate / "phase-two"
        run_dir.mkdir()
        _ = (self.delegate / "active" / DIRECTOR_ID).write_text(
            str(run_dir) + "\n", encoding="utf-8"
        )
        _ = (run_dir / "seats").write_text("aaaaaaaa\treview-seat\n", encoding="utf-8")
        _ = (run_dir / "mesh_server.json").write_text(
            json.dumps({"port": 45_678, "pid": os.getpid()}), encoding="utf-8"
        )
        _ = (run_dir / "mesh_roster.json").write_text(
            json.dumps(
                {
                    "busy-codex": {
                        "thread_id": "thread-busy",
                        "turn_id": "turn-busy",
                        "status": "running",
                        "launcher_pid": os.getpid(),
                    },
                    "busy-without-launcher": {
                        "thread_id": "thread-without-launcher",
                        "status": "running",
                    },
                    "orphaned-start": {
                        "thread_id": "thread-orphaned",
                        "status": "starting",
                        "launcher_pid": 999_999_999,
                    },
                    "finished-codex": {"thread_id": "thread-done", "status": "done"},
                }
            ),
            encoding="utf-8",
        )

    def make_snapshot(self) -> None:
        snapshot = self.home / "rust" / "hanadocs" / "agent sessions.md"
        snapshot.parent.mkdir(parents=True)
        _ = snapshot.write_text(
            "\n".join(
                (
                    "## desktop: Production",
                    f"`{self.root / 'plain'}` · ghostty · build-followups",
                    "```sh",
                    f"cd {self.root / 'plain'} && claude --resume {DIRECTOR_ID} -n build-followups",
                    "```",
                    "",
                    "## desktop: Writing",
                    f"`{self.root / 'plain'}` · ghostty · notes",
                    "```sh",
                    f"cd {self.root / 'plain'} && claude --resume {DESKTOP_ID} -n notes",
                    "```",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def test_inventory_is_account_scoped_and_discovers_every_owned_resource(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)

        self.assertEqual(found["login"], TARGET_LOGIN)
        self.assertEqual(found["label"], "claude 2")
        by_id = {session["session_id"]: session for session in found["sessions"]}
        self.assertEqual(set(by_id), {DIRECTOR_ID, UNIT_ID, SEAT_ID, DESKTOP_ID})
        self.assertNotIn(OTHER_ID, by_id)
        self.assertEqual(
            found["unattributed"],
            [
                {
                    "pid": UNKNOWN_PID,
                    "name": "hidden-account",
                    "reason": "account unreadable",
                }
            ],
        )

        director = cast(inventory.ShowrunnerSession, by_id[DIRECTOR_ID])
        self.assertEqual(director["kind"], "showrunner")
        self.assertEqual(director["status"], "idle")
        self.assertEqual(
            director["model"], {"kind": "model", "name": "director-model"}
        )
        self.assertEqual(director["timers"], ["showrunner-build-followups"])
        self.assertEqual(director["host"]["kind"], "ghostty")
        self.assertEqual(
            director["host"].get("desktop"),
            {"kind": "named", "name": "Production"},
        )
        self.assertEqual(director["production"], "build-followups")
        self.assertEqual(
            director["doc"], str(self.root / "build-followups-production.md")
        )
        self.assertEqual(director["run_dirs"], [str(self.delegate / "phase-two")])
        self.assertEqual(
            director["codex_servers"],
            [
                {
                    "run_dir": str(self.delegate / "phase-two"),
                    "pid": os.getpid(),
                    "busy_seats": ["busy-codex", "busy-without-launcher"],
                }
            ],
        )

        unit = cast(inventory.UnitSession, by_id[UNIT_ID])
        self.assertEqual(unit["kind"], "unit")
        self.assertEqual(
            unit["model"], {"kind": "model", "name": "unit-model"}
        )
        self.assertEqual(unit["timers"], ["unit-reminder"])
        self.assertEqual(
            unit["host"],
            {
                "kind": "unit",
                "production": "build-followups",
                "unit": "shutdown-unit",
                "doc": str(self.root / "build-followups-production.md"),
                "plan": {"kind": "plan", "path": "docs/shutdown-plan.md"},
                "tmux_session": "unit-renamed",
            },
        )
        self.assertEqual(
            unit["checkout"],
            {
                "kind": "git",
                "path": str(self.checkout),
                "head": {"kind": "branch", "name": "shutdown-unit"},
                "upstream": {"kind": "tracking", "ahead": 2},
                "dirty": ["dirty.txt"],
            },
        )

        seat = cast(inventory.SeatSession, by_id[SEAT_ID])
        self.assertEqual(seat["kind"], "seat")
        self.assertEqual(
            seat["owner"], {"kind": "director", "session_id": DIRECTOR_ID}
        )
        self.assertEqual(by_id[DESKTOP_ID]["kind"], "top-level")
        self.assertEqual(
            cast(inventory.TopLevelSession, by_id[DESKTOP_ID])["host"],
            {
                "kind": "ghostty",
                "desktop": {"kind": "named", "name": "Writing"},
                "window_shell": DESKTOP_SHELL_PID,
            },
        )

        limited = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))
        self.assertEqual(
            {session["session_id"] for session in limited["sessions"]},
            {DIRECTOR_ID, SEAT_ID},
        )

    def test_truncated_wrapped_ghostty_name_keeps_snapshot_desktop(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(director["host"]["kind"], "ghostty")
        self.assertEqual(
            director["host"].get("desktop"),
            {"kind": "named", "name": "Production"},
        )

    def test_unit_host_carries_live_tmux_session_name(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        unit = next(session for session in found["sessions"] if session["session_id"] == UNIT_ID)

        self.assertEqual(unit["host"].get("tmux_session"), "unit-renamed")

    def test_tmux_hosts_record_the_pane_or_an_explicit_absence(self) -> None:
        def process_environment(pid: int) -> dict[str, str]:
            if pid == self.director_pid:
                return {"TMUX_PANE": "%8"}
            if pid == DESKTOP_PID:
                return {"TMUX_PANE": "%9"}
            return {}

        def tmux_details(
            pane: str,
        ) -> tuple[str, inventory.TmuxPane | inventory.PaneNotRecorded]:
            if pane == "%8":
                return "production work", {
                    "kind": "pane",
                    "pane_id": "%8",
                    "pane_pid": 62_008,
                }
            if pane == "%9":
                return "writing", {"kind": "not recorded"}
            return "", {"kind": "not recorded"}

        with (
            patch("inventory._process_environment", side_effect=process_environment),
            patch("inventory._tmux_session", side_effect=tmux_details),
        ):
            found = inventory.inventory(TARGET_LOGIN)

        by_id = {session["session_id"]: session for session in found["sessions"]}
        self.assertEqual(
            by_id[DIRECTOR_ID]["host"],
            {
                "kind": "tmux",
                "tmux_session": "production work",
                "pane": {"kind": "pane", "pane_id": "%8", "pane_pid": 62_008},
            },
        )
        self.assertEqual(
            by_id[DESKTOP_ID]["host"],
            {
                "kind": "tmux",
                "tmux_session": "writing",
                "pane": {"kind": "not recorded"},
            },
        )

    def test_tmux_discovery_reads_session_and_pane_pid_in_one_call(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="production work\t62008\n"
        )
        with (
            patch.object(unit_lookup, "tmux_binary", return_value="/test/tmux"),
            patch("inventory.subprocess.run", return_value=completed) as run,
        ):
            found = inventory._tmux_session("%8")  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            found,
            (
                "production work",
                {"kind": "pane", "pane_id": "%8", "pane_pid": 62_008},
            ),
        )
        self.assertEqual(
            run.call_args.args[0],
            (
                "/test/tmux",
                "display",
                "-p",
                "-t",
                "%8",
                "#{session_name}\t#{pane_pid}",
            ),
        )

    def test_tmux_discovery_preserves_spaces_in_session_name(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(), returncode=0, stdout=" work \t62008\n"
        )
        with patch("inventory.subprocess.run", return_value=completed):
            found = inventory._tmux_session("%8")  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            found,
            (
                " work ",
                {"kind": "pane", "pane_id": "%8", "pane_pid": 62_008},
            ),
        )

    def test_tmux_discovery_keeps_session_when_pane_pid_is_unusable(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="writing\tnot-a-pid\n"
        )
        with patch("inventory.subprocess.run", return_value=completed):
            found = inventory._tmux_session("%9")  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(found, ("writing", {"kind": "not recorded"}))

    def test_ghostty_host_carries_window_shell_between_terminal_and_claude(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(director["host"].get("window_shell"), DIRECTOR_SHELL_PID)

    def test_running_codex_seat_without_launcher_is_busy_but_dead_launcher_is_not(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(
            director["codex_servers"][0]["busy_seats"],
            ["busy-codex", "busy-without-launcher"],
        )
        self.assertNotIn("orphaned-start", director["codex_servers"][0]["busy_seats"])

    def test_incomplete_showrunner_timer_leaves_session_top_level(self) -> None:
        invalid_showrunner = self.notifier / "showrunner-incomplete"
        invalid_showrunner.mkdir()
        _ = (invalid_showrunner / "conf").write_text(
            f"TARGET=session:{DESKTOP_ID}\n", encoding="utf-8"
        )

        found = inventory.inventory(TARGET_LOGIN)
        desktop = next(
            session for session in found["sessions"] if session["session_id"] == DESKTOP_ID
        )

        self.assertEqual(desktop["kind"], "top-level")

    def test_reused_pid_with_different_process_start_is_unknown(self) -> None:
        path = self.records / f"{DESKTOP_PID}.json"
        record = cast(dict[str, object], cast(object, json.loads(path.read_text(encoding="utf-8"))))
        record["procStart"] = "recorded-before-pid-reuse"
        _ = path.write_text(json.dumps(record), encoding="utf-8")

        with patch("inventory._process_start", return_value="live-reused-pid-start"):
            found = inventory.inventory(TARGET_LOGIN)

        self.assertNotIn(DESKTOP_ID, {session["session_id"] for session in found["sessions"]})
        self.assertIn(
            {
                "pid": DESKTOP_PID,
                "name": "notes",
                "reason": "process start mismatch",
            },
            found["unattributed"],
        )

    def test_live_seat_keeps_owner_after_director_exits(self) -> None:
        (self.records / f"{self.director_pid}.json").unlink()

        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))

        self.assertEqual(
            [
                (session["session_id"], session["owner"])
                for session in found["sessions"]
                if session["kind"] == "seat"
            ],
            [(SEAT_ID, {"kind": "director", "session_id": DIRECTOR_ID})],
        )

    def test_newest_unit_plan_is_selected_by_started_time_not_write_order(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        unit = next(session for session in found["sessions"] if session["session_id"] == UNIT_ID)

        self.assertEqual(
            unit["host"].get("plan"),
            {"kind": "plan", "path": "docs/shutdown-plan.md"},
        )

    def test_absent_values_are_explicit_variants(self) -> None:
        for path in (self.history / "runs").glob("*.jsonl"):
            path.unlink()
        (self.delegate / "phase-two" / "seats").unlink()
        transcript = (
            self.home / ".claude" / "projects" / "fixture" / f"{DESKTOP_ID}.jsonl"
        )
        transcript.unlink()

        found = inventory.inventory(TARGET_LOGIN)
        by_id = {session["session_id"]: session for session in found["sessions"]}

        unit = cast(inventory.UnitSession, by_id[UNIT_ID])
        seat = cast(inventory.SeatSession, by_id[SEAT_ID])
        self.assertEqual(unit["host"]["plan"], {"kind": "no run record"})
        self.assertEqual(by_id[DESKTOP_ID]["model"], {"kind": "no reply yet"})
        self.assertEqual(seat["owner"], {"kind": "no director"})
        self.assertEqual(
            by_id[DIRECTOR_ID]["checkout"], {"kind": "not a checkout"}
        )

    def test_detached_head_and_missing_upstream_are_explicit(self) -> None:
        _ = self.git("checkout", "--detach")

        found = inventory.inventory(TARGET_LOGIN)
        unit = next(session for session in found["sessions"] if session["session_id"] == UNIT_ID)

        self.assertEqual(
            unit["checkout"],
            {
                "kind": "git",
                "path": str(self.checkout),
                "head": {"kind": "detached", "commit": self.head},
                "upstream": {"kind": "no upstream"},
                "dirty": ["dirty.txt"],
            },
        )

    def test_parse_inventory_accepts_inventory_output(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)

        decoded = inventory.parse_inventory(json.dumps(found))

        self.assertEqual(decoded, found)

    def test_parse_inventory_treats_a_legacy_tmux_host_as_pane_not_recorded(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        document = cast(dict[str, object], cast(object, json.loads(json.dumps(found))))
        session_values = cast(list[object], document["sessions"])
        desktop = next(
            cast(dict[str, object], value)
            for value in session_values
            if cast(dict[str, object], value)["session_id"] == DESKTOP_ID
        )
        desktop["host"] = {"kind": "tmux", "tmux_session": "legacy"}

        decoded = inventory.parse_inventory(json.dumps(document))
        parsed = next(
            session
            for session in decoded["sessions"]
            if session["session_id"] == DESKTOP_ID
        )

        self.assertEqual(
            parsed["host"],
            {
                "kind": "tmux",
                "tmux_session": "legacy",
                "pane": {"kind": "not recorded"},
            },
        )

    def test_parse_inventory_validates_a_recorded_tmux_pane(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        document = cast(dict[str, object], cast(object, json.loads(json.dumps(found))))
        session_values = cast(list[object], document["sessions"])
        first = cast(dict[str, object], session_values[0])
        first["host"] = {
            "kind": "tmux",
            "tmux_session": "work",
            "pane": {"kind": "pane", "pane_id": "%2", "pane_pid": "invalid"},
        }

        with self.assertRaisesRegex(
            inventory.InvalidInventory,
            r"inventory\.sessions\[0\]\.host\.pane\.pane_pid must be an integer",
        ):
            _ = inventory.parse_inventory(json.dumps(document))

    def test_parse_inventory_rejects_unknown_kind_and_missing_variant_field(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        document = cast(dict[str, object], cast(object, json.loads(json.dumps(found))))
        session_values = cast(list[object], document["sessions"])
        first = cast(dict[str, object], session_values[0])
        first["host"] = {"kind": "spaceship"}
        with self.assertRaisesRegex(
            inventory.InvalidInventory,
            r"inventory\.sessions\[0\]\.host\.kind is invalid",
        ):
            _ = inventory.parse_inventory(json.dumps(document))

        document = cast(dict[str, object], cast(object, json.loads(json.dumps(found))))
        session_values = cast(list[object], document["sessions"])
        window_host: dict[str, object] | None = None
        for item in session_values:
            session_data = cast(dict[str, object], item)
            host_data = cast(dict[str, object], session_data["host"])
            if host_data["kind"] == "ghostty":
                window_host = host_data
                break
        self.assertIsNotNone(window_host)
        assert window_host is not None
        host = window_host
        del host["window_shell"]
        with self.assertRaisesRegex(
            inventory.InvalidInventory,
            r"inventory\.sessions\[0\]\.host\.window_shell is missing",
        ):
            _ = inventory.parse_inventory(json.dumps(document))


@final
class DarwinProcessStartTests(unittest.TestCase):
    def test_darwin_start_is_read_in_the_locale_and_zone_claude_records(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="Fri Oct  9 23:24:25 2026\n"
        )
        with (
            patch("inventory.sys.platform", "darwin"),
            patch("inventory.subprocess.run", return_value=completed) as run,
        ):
            started = inventory._process_start(4242)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(started, "Fri Oct  9 23:24:25 2026")
        environment = cast(dict[str, str], run.call_args.kwargs["env"])
        self.assertEqual((environment["LC_ALL"], environment["TZ"]), ("C", "UTC"))


if __name__ == "__main__":
    _ = unittest.main()
