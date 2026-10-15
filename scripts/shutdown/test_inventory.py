"""Behavioral tests for account-scoped shutdown inventory discovery."""

from __future__ import annotations

import json
import io
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from test_support import ShutdownTestCase
from test_restart import entry, pane_display, top_level
import record
import inventory
import account
import fake_tmux
import herdr
import sessions
import showrunners
import unit_lookup
import restart
import shutdown
import stop
from fake_tmux import FakeSession


TARGET_LOGIN = "owner@example.com"
OTHER_LOGIN = "other@example.com"
DIRECTOR_ID = "11111111-1111-1111-1111-111111111111"
UNIT_ID = "22222222-2222-2222-2222-222222222222"
WORKER_ID = "aaaaaaaa-3333-3333-3333-333333333333"
DESKTOP_ID = "44444444-4444-4444-4444-444444444444"
OTHER_ID = "55555555-5555-5555-5555-555555555555"
UNKNOWN_ID = "66666666-6666-6666-6666-666666666666"
UNIT_PID = 41_002
WORKER_PID = 41_003
DESKTOP_PID = 41_004
OTHER_PID = 41_005
UNKNOWN_PID = 41_006
DIRECTOR_SHELL_PID = 51_001
DESKTOP_SHELL_PID = 51_004
CURRENT_PROCESS_START = "current-process-start"
FAKE_TMUX = Path(__file__).parents[1] / "production" / "fake_tmux.py"


class TranscriptReadCounter(io.BufferedReader):
    """Count transcript bytes delivered to the reader, independent of elapsed time."""

    def __init__(self, path: Path) -> None:
        super().__init__(io.FileIO(path, "r"))
        self.bytes_read: int = 0

    @override
    def read(self, size: int | None = -1) -> bytes:
        value = super().read(size)
        self.bytes_read += len(value)
        return value

    @override
    def readline(self, size: int | None = -1) -> bytes:
        value = super().readline(size)
        self.bytes_read += len(value)
        return value


@final
class InventoryTests(ShutdownTestCase):
    def test_last_model_skips_trailing_metadata_and_invalid_json(self) -> None:
        transcript = self.root / "metadata.jsonl"
        entries: list[dict[str, object]] = [
            {"type": "assistant", "message": {"model": "older-model"}},
            {"type": "assistant", "message": {"model": "latest-model"}},
            {"type": "system", "message": {"model": "system-model"}},
            {"type": "cost-state", "model": "cost-model"},
            {"type": "last-prompt", "message": {"model": "prompt-model"}},
        ]
        contents = "\n".join(json.dumps(entry) for entry in entries)
        for ending in ("\n", ""):
            with self.subTest(ending=repr(ending)):
                _ = transcript.write_text(contents + "\nmalformed-json" + ending, encoding="utf-8")
                self.assertEqual(inventory.last_model(transcript), "latest-model")

    def test_last_model_reassembles_lines_across_tail_blocks(self) -> None:
        transcript = self.root / "split-line.jsonl"
        entries = [
            {"type": "assistant", "message": {"model": "older-model"}},
            {"type": "assistant", "message": {"model": "split-model", "content": "é" * 70_000}},
            {"type": "system", "content": "tail"},
        ]
        _ = transcript.write_text(
            "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(inventory.last_model(transcript), "split-model")

    def test_transcript_path_uses_newest_direct_project_and_excludes_subagents(self) -> None:
        older = self.target_config / "projects/older/lookup.jsonl"
        newer = self.target_config / "projects/newer/lookup.jsonl"
        nested = self.target_config / "projects/newer/subagents/lookup.jsonl"
        for timestamp, path in ((10, older), (20, newer), (30, nested)):
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(json.dumps({"type": "assistant", "message": {"model": path.parent.name}}))
            os.utime(path, (timestamp, timestamp))
        self.assertEqual(inventory.transcript_path(self.target_config, "lookup"), newer)
        older.unlink()
        newer.unlink()
        self.assertIsNone(inventory.transcript_path(self.target_config, "lookup"))

    def test_inventory_models_come_from_each_sessions_config_directory(self) -> None:
        (self.target_config / "projects").unlink()
        transcript = self.target_config / "projects/own-tree" / f"{DESKTOP_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "message": {"model": "own-account-model"}}))
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        self.assertEqual(found["sessions"][0]["model"], {"kind": "model", "name": "own-account-model"})

    def test_inventory_filter_includes_owned_workers_and_empty_selection_includes_all(self) -> None:
        cases = (
            (frozenset({DIRECTOR_ID}), {DIRECTOR_ID, WORKER_ID}),
            (frozenset({WORKER_ID}), {WORKER_ID}),
            (frozenset[str](), {DIRECTOR_ID, UNIT_ID, WORKER_ID, DESKTOP_ID}),
        )
        for selected, expected in cases:
            for keyword in (True, False):
                with self.subTest(selected=selected, keyword=keyword):
                    found = (
                        inventory.inventory(TARGET_LOGIN, only=selected)
                        if keyword else inventory.inventory(TARGET_LOGIN, selected)
                    )
                    self.assertEqual({session["session_id"] for session in found["sessions"]}, expected)
                    for session in found["sessions"]:
                        if session["kind"] == "worker":
                            self.assertEqual(session["owner"], {"kind": "director", "session_id": DIRECTOR_ID})

    def test_inventory_only_builds_expensive_fields_for_selected_ids(self) -> None:
        with patch.object(inventory, "_model", wraps=inventory._model) as read_model:  # pyright: ignore[reportPrivateUsage]
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID, OTHER_ID}))
        self.assertEqual([session["session_id"] for session in found["sessions"]], [DESKTOP_ID])
        self.assertEqual(read_model.call_count, 1)
        self.assertIn(DESKTOP_ID, read_model.call_args.args)

    def test_listing_agrees_with_full_inventory_and_excludes_expensive_work(self) -> None:
        full = inventory.inventory(TARGET_LOGIN)["sessions"] + inventory.inventory(OTHER_LOGIN)["sessions"]
        with (
            patch.object(inventory, "_model", side_effect=AssertionError("listing reads a transcript")),
            patch.object(inventory, "_checkout", side_effect=AssertionError("listing inspects git")),
            patch.object(inventory, "_git", side_effect=AssertionError("listing runs git")),
            patch.object(inventory, "_timers", side_effect=AssertionError("listing reads timers")),
            patch.object(inventory, "_host", side_effect=AssertionError("listing builds a host")),
            patch.object(inventory, "_snapshot_desktops", side_effect=AssertionError("listing reads desktops")),
            patch.object(inventory, "_newest_unit_plans", side_effect=AssertionError("listing reads plan history")),
        ):
            listed = inventory.listing()
            owned = inventory.listing(TARGET_LOGIN)
        by_id = {session.session_id: session for session in listed}
        self.assertEqual(set(by_id), {DIRECTOR_ID, UNIT_ID, WORKER_ID, DESKTOP_ID, OTHER_ID})
        self.assertEqual({session.session_id for session in owned}, {DIRECTOR_ID, UNIT_ID, WORKER_ID, DESKTOP_ID})
        for session in full:
            light = by_id[session["session_id"]]
            with self.subTest(session=light.session_id):
                self.assertEqual(light.kind, session["kind"])
                self.assertEqual(light.pid, session["pid"])
                self.assertEqual(light.proc_start, session["proc_start"])
                self.assertEqual(light.name, session["name"])
                self.assertEqual(str(light.config_dir), session.get("config_dir"))
                self.assertEqual(light.login, OTHER_LOGIN if light.session_id == OTHER_ID else TARGET_LOGIN)
                if session["kind"] == "showrunner":
                    self.assertEqual(light.production, session["production"])
                    self.assertEqual(light.doc, session["doc"])
                elif session["kind"] == "unit":
                    self.assertEqual(light.production, session["host"]["production"])
                    self.assertEqual(light.doc, session["host"]["doc"])

    def test_listing_and_absent_target_check_share_timer_reads_without_resolving_sockets(self) -> None:
        stale = self.notifier / "showrunner-stale"
        stale.mkdir()
        stale_doc = self.root / "stale-production.md"
        _ = stale_doc.write_text("- **User zone:** America/Los_Angeles\n", encoding="utf-8")
        _ = (stale / "conf").write_text(
            f"TARGET=session:00000000-0000-4000-8000-000000000000\nCHECK=zsh /opt/production_check.sh {stale_doc}\n",
            encoding="utf-8")
        with (patch.object(showrunners, "socket_for", side_effect=AssertionError("listing probed a messaging socket")),
              patch.object(showrunners, "registered_showrunners", side_effect=AssertionError("listing resolved showrunner messaging")),
              patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as checked):
            registrations = inventory.showrunner_registrations()
            listed = inventory.listing(registrations=registrations)
            missing = inventory.unregistered_showrunners(listed, registrations=registrations)
        self.assertEqual(checked.call_count, 2, "membership and missing-target check must share the timer read")
        director = next(item for item in listed if item.session_id == DIRECTOR_ID)
        self.assertEqual((director.kind, director.production, director.doc),
            ("showrunner", "build-followups", str(self.root / "build-followups-production.md")))
        unit = next(item for item in listed if item.session_id == UNIT_ID)
        self.assertEqual((unit.kind, unit.production, unit.doc),
            ("unit", "build-followups", director.doc))
        self.assertEqual([(runner["slug"], runner["doc"]) for runner in missing], [("stale", str(stale_doc))])

    def test_forty_session_listing_and_large_transcript_read_budget(self) -> None:
        for path in self.records.glob("*.json"):
            path.unlink()
        identities = {f"fixture-{number:02}" for number in range(40)}
        for number, identity in enumerate(sorted(identities)):
            self.write_session(60_000 + number, identity, identity, self.root / "plain")
        transcript = self.target_config / "projects/fixture/fixture-39.jsonl"
        with transcript.open("wb") as stream:
            padding = json.dumps({"type": "system", "content": "x" * 1024}).encode() + b"\n"
            for _ in range((50 * 1024 * 1024 // len(padding)) + 1):
                _ = stream.write(padding)
            _ = stream.write(b'{"type":"assistant","message":{"model":"large-file-model"}}\n')
            _ = stream.write(b'{"type":"last-prompt","content":"after the reply"}\n')
        with patch.object(account, "process_config_dir", return_value=self.target_config):
            listed = inventory.listing(TARGET_LOGIN)
        self.assertEqual({session.session_id for session in listed}, identities)
        self.assertGreaterEqual(transcript.stat().st_size, 50 * 1024 * 1024)
        with TranscriptReadCounter(transcript) as stream:
            with patch.object(Path, "open", return_value=stream):
                model = inventory.last_model(transcript)
            self.assertEqual(model, "large-file-model")
            self.assertGreater(stream.bytes_read, 0)
            self.assertLess(stream.bytes_read, 1024 * 1024)

    def test_cached_desktop_snapshot_never_invokes_the_snapshot_updater(self) -> None:
        with (patch.object(shutil, "which", return_value="/fixture/agent-sessions-snapshot"),
              patch.object(subprocess, "run") as updater):
            desktops = inventory._snapshot_desktops(refresh=False)  # pyright: ignore[reportPrivateUsage]
        updater.assert_not_called()
        self.assertEqual(desktops[DESKTOP_ID], "Writing")

    def test_nested_shutdown_inventory_reads_existing_snapshot_without_updating_notes(self) -> None:
        with (patch.dict(os.environ, {"REFUSAL_WATCH_INVENTORY": "read existing"}),
              patch.object(shutil, "which", return_value="/fixture/agent-sessions-snapshot"),
              patch.object(subprocess, "run") as updater):
            desktops = inventory._snapshot_desktops()  # pyright: ignore[reportPrivateUsage]
        updater.assert_not_called()
        self.assertEqual(desktops[DIRECTOR_ID], "Production")

    def test_refusal_synthetic_model_keeps_the_last_real_reply_model(self) -> None:
        transcript = self.home / ".claude/projects/model-fixture/refused.jsonl"
        transcript.parent.mkdir(parents=True)
        entries = [
            {"type": "assistant", "message": {"model": "claude-opus-real"}},
            {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "weekly limit"}]}},
        ]
        _ = transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        model = inventory._model("refused", self.home / ".claude")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(inventory.last_model(transcript), "claude-opus-real")
        self.assertEqual(model, inventory.ModelName(kind="model", name="claude-opus-real"))
        owner = top_level("refused", model=model)
        self.assertIn("--model claude-opus-real", restart.resume_command(owner, "claude3"))
        self.assertNotIn("<synthetic>", restart.resume_command(owner, "claude3"))

    def test_only_synthetic_refusal_has_no_real_reply_model(self) -> None:
        transcript = self.home / ".claude/projects/model-fixture/refused.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + "\n")
        self.assertEqual(inventory._model("refused", self.home / ".claude"), inventory.NoReplyYet(kind="no reply yet"))  # pyright: ignore[reportPrivateUsage]

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
        self.proc = Path()
        self.herdr_log = Path()
        self.target_config = Path()
        self.other_config = Path()
        self.director_pid = 0
        self.live_socket = socket.socket(socket.AF_UNIX)
        self.head = ""

    @override
    def setUp(self) -> None:
        super().setUp()
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
        self.proc = self.root / "proc"
        self.herdr_log = self.root / "herdr.jsonl"
        responses = self.root / "herdr-responses.json"
        self.herdr_sessions([])
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
            self.proc,
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
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr" / "fake_herdr.py"),
            "HERDR_PROC": str(self.proc),
            "FAKE_HERDR_LOG": str(self.herdr_log),
            "FAKE_HERDR_RESPONSES": str(responses),
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
        _ = (self.notes / "claude2.md").write_text(
            f"---\nlogin: {TARGET_LOGIN.upper()}\nsessions: 1\n---\n",
            encoding="utf-8",
        )
        config_by_pid: dict[int, Path | None] = {
            self.director_pid: self.target_config,
            UNIT_PID: self.target_config,
            WORKER_PID: self.target_config,
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
        for config_dir in (self.target_config, self.other_config):
            (config_dir / "projects").symlink_to(self.home / ".claude/projects", target_is_directory=True)
        self.make_production_state()
        self.make_delegate_state()
        self.make_snapshot()

    def write_account(self, config_dir: Path, login: str) -> None:
        _ = (config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": login}}),
            encoding="utf-8",
        )

    def herdr_process(self, pid: int, parent: int, arguments: list[str], environment: dict[str, str]) -> None:
        process = self.proc / str(pid)
        process.mkdir()
        _ = (process / "stat").write_text(f"{pid} (fixture) S {parent} 0 0\n")
        _ = (process / "cmdline").write_bytes(("\0".join(arguments) + "\0").encode())
        _ = (process / "environ").write_bytes(
            ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode()
        )
        (process / "cwd").symlink_to(self.checkout)

    def herdr_sessions(self, names: list[str]) -> None:
        entries = [herdr.SessionEntry(name=name, running=True, default=False,
                                    session_dir=str(self.root / name), socket_path="fake-socket") for name in names]
        response = {"stdout": json.dumps(herdr.SessionListResult(sessions=entries)), "code": 0}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": response}))

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
        self.write_executable("kdotool", "raise SystemExit(1)\n")

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
            WORKER_PID,
            WORKER_ID,
            "review-worker",
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
        _ = (run_dir / "workers").write_text("aaaaaaaa\treview-worker\n", encoding="utf-8")
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
        self.assertEqual(found["label"], "claude2")
        by_id = {session["session_id"]: session for session in found["sessions"]}
        self.assertEqual(set(by_id), {DIRECTOR_ID, UNIT_ID, WORKER_ID, DESKTOP_ID})
        self.assertTrue(
            all(session.get("config_dir") == str(self.target_config) for session in by_id.values())
        )
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
                    "busy_workers": ["busy-codex", "busy-without-launcher"],
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
                "session_name": "unit-renamed",
                "session_host": "tmux",
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

        worker = cast(inventory.WorkerSession, by_id[WORKER_ID])
        self.assertEqual(worker["kind"], "worker")
        self.assertEqual(
            worker["owner"], {"kind": "director", "session_id": DIRECTOR_ID}
        )
        self.assertEqual(by_id[DESKTOP_ID]["kind"], "top-level")
        self.assertEqual(
            cast(inventory.TopLevelSession, by_id[DESKTOP_ID])["host"],
            {
                "kind": "ghostty",
                "desktop": {"kind": "named", "name": "Writing"},
                "window_shell": DESKTOP_SHELL_PID,
                "supervisor": {"kind": "no supervisor"},
            },
        )

        limited = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))
        self.assertEqual(
            {session["session_id"] for session in limited["sessions"]},
            {DIRECTOR_ID, WORKER_ID},
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

        self.assertEqual(unit["host"].get("session_name"), "unit-renamed")
        self.assertEqual(unit["host"].get("session_host"), "tmux")
        self.assertEqual(shutdown._host_text(unit["host"]), "unit build-followups/shutdown-unit")  # pyright: ignore[reportPrivateUsage]

    def test_empty_herdr_unit_name_does_not_fall_back_to_a_tmux_record(self) -> None:
        session = inventory.SessionFile(sessionId="unit", tmux="unrelated:0.0")
        for host in ("tmux", "herdr"):
            marks = inventory.UnitMarks(production="build", unit="one", doc="/tmp/build.md",
                                        session_name="", session_host=host)
            recorded = inventory._host(session, marks, {}, {})  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(recorded.get("session_name"), "unrelated" if host == "tmux" else "")

    def test_herdr_host_comes_from_the_process_server_ancestor(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "writing", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"],
                           {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "inherited-other-name"})
        self.assertEqual(herdr.herdr_place_of(DESKTOP_PID), herdr.HerdrPlace("writing", "w1:p1"))
        found = inventory.inventory(TARGET_LOGIN)
        desktop = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(desktop["host"], {"kind": "herdr", "herdr_session": "writing", "pane_id": "w1:p1"})
        self.assertEqual(inventory.parse_inventory(json.dumps(found)), found)
        self.assertEqual(shutdown._host_text(desktop["host"]), "herdr writing")  # pyright: ignore[reportPrivateUsage]

    def test_herdr_unit_records_its_host_and_live_session_name(self) -> None:
        self.herdr_sessions(["unit-herdr"])
        fake_tmux.write(self.tmux_state, {})
        path = self.records / f"{UNIT_PID}.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        record["tmux"] = ""
        _ = path.write_text(json.dumps(record))
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "unit-herdr", "server"], {})
        self.herdr_process(UNIT_PID, 71_001, ["/bin/claude"], {
            "HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "build-followups",
            "SHOWRUNNER_UNIT_ID": "shutdown-unit",
        })
        found = inventory.inventory(TARGET_LOGIN)
        unit = next(session for session in found["sessions"] if session["session_id"] == UNIT_ID)
        self.assertEqual(unit["kind"], "unit")
        self.assertEqual(unit["host"].get("session_name"), "unit-herdr")
        self.assertEqual(unit["host"].get("session_host"), "herdr")
        self.assertEqual(inventory.parse_inventory(json.dumps(found)), found)
        self.assertEqual(shutdown._host_text(unit["host"]), "unit build-followups/shutdown-unit (herdr unit-herdr)")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(unit["host"].get("plan"), {"kind": "plan", "path": "docs/shutdown-plan.md"})

    def test_herdr_host_cleanup_leaves_its_pane_and_server_running(self) -> None:
        session = cast(inventory.TopLevelSession, next(
            item for item in inventory.inventory(TARGET_LOGIN)["sessions"] if item["session_id"] == DESKTOP_ID
        ))
        session["host"] = inventory.HerdrHost(kind="herdr", herdr_session="writing", pane_id="w1:p1")
        with patch.object(stop, "_stop_unit_tmux") as tmux_cleanup, patch.object(stop, "_close_ghostty_window"):
            issues = stop._stop_owner_resources(  # pyright: ignore[reportPrivateUsage]
                session, kill=lambda _pid, _signal: self.fail("unexpected kill"), is_alive=lambda _pid: False,
                codex_mesh=lambda _argv: self.fail("unexpected Codex stop"),
                tmux=lambda _argv: self.fail("unexpected tmux call"), clock=datetime.now,
                claim=stop.StopClaim(TARGET_LOGIN, DESKTOP_ID, "fixture", "fixture"),
            )
        self.assertEqual(issues, [])
        tmux_cleanup.assert_not_called()

    def test_tmux_host_takes_precedence_over_inherited_herdr_environment(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "outer", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"], {"HERDR_PANE_ID": "w1:p1"})
        with patch("inventory._process_environment", return_value={"TMUX_PANE": "%9"}), patch(
            "inventory._tmux_session", return_value=(
                "inner", inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
                inventory.TmuxServerNotRecorded(kind="not recorded"), inventory.PaneNotRecorded(kind="not recorded"),
            ),
        ):
            found = inventory.inventory(TARGET_LOGIN)
        desktop = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(desktop["host"]["kind"], "tmux")
        self.assertEqual(desktop["host"].get("tmux_session"), "inner")

    def test_tmux_hosts_record_the_pane_or_an_explicit_absence(self) -> None:
        def process_environment(pid: int) -> dict[str, str]:
            if pid == self.director_pid:
                return {"TMUX_PANE": "%8"}
            if pid == DESKTOP_PID:
                return {"TMUX_PANE": "%9"}
            return {}

        def tmux_details(
            pane: str,
        ) -> tuple[str, inventory.TmuxSessionId | inventory.TmuxSessionIdNotRecorded, inventory.TmuxServer | inventory.TmuxServerNotRecorded, inventory.TmuxPane | inventory.PaneNotRecorded]:
            if pane == "%8":
                return "production work", inventory.TmuxSessionId(kind="session id", session_id="$8"), inventory.TmuxServer(kind="server", pid=34500, started_at=1700000000), {
                    "kind": "pane",
                    "pane_id": "%8",
                    "pane_pid": 62_008,
                }
            if pane == "%9":
                return "writing", inventory.TmuxSessionIdNotRecorded(kind="not recorded"), inventory.TmuxServerNotRecorded(kind="not recorded"), {"kind": "not recorded"}
            return "", inventory.TmuxSessionIdNotRecorded(kind="not recorded"), inventory.TmuxServerNotRecorded(kind="not recorded"), {"kind": "not recorded"}

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
                "session_ref": {"kind": "session id", "session_id": "$8"},
                "server": {"kind": "server", "pid": 34500, "started_at": 1700000000},
                "pane": {"kind": "pane", "pane_id": "%8", "pane_pid": 62_008},
            },
        )
        self.assertEqual(
            by_id[DESKTOP_ID]["host"],
            {
                "kind": "tmux",
                "tmux_session": "writing",
                "session_ref": {"kind": "not recorded"},
                "server": {"kind": "not recorded"},
                "pane": {"kind": "not recorded"},
            },
        )

    def discover_tmux(
        self, name: str, session_id: str, pid: str, started_at: str = "1700000000",
        *, include_start: bool = True, server_pid: str = "34500", include_pid: bool = True,
    ) -> tuple[str, inventory.TmuxSessionId | inventory.TmuxSessionIdNotRecorded,
               inventory.TmuxServer | inventory.TmuxServerNotRecorded, inventory.TmuxPane | inventory.PaneNotRecorded]:
        calls: list[tuple[str, ...]] = []
        def probe(argv: tuple[str, ...], *, capture_output: bool, text: bool, timeout: int, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, timeout, check
            calls.append(argv)
            self.assertEqual(argv[:5], ("/test/tmux", "display", "-p", "-t", "%8"))
            self.assertCountEqual(argv[-1].split("\t"), ["#{session_name}", "#{session_id}", "#{pane_pid}", "#{start_time}", "#{pid}"])
            format_text = argv[-1]
            if not include_start:
                format_text = "\t".join(field for field in format_text.split("\t") if field != "#{start_time}")
            if not include_pid:
                format_text = "\t".join(field for field in format_text.split("\t") if field != "#{pid}")
            return subprocess.CompletedProcess(argv, 0,
                pane_display(format_text, name, session_id, pid, "", started_at, server_pid), "")
        with patch.object(unit_lookup, "tmux_binary", return_value="/test/tmux"), patch("inventory.subprocess.run", probe):
            found = inventory._tmux_session("%8")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(calls), 1)
        return found

    def test_tmux_discovery_reads_session_and_pane_pid_in_one_call(self) -> None:
        self.assertEqual(self.discover_tmux("production work", "$8", "62008"),
            ("production work", {"kind": "session id", "session_id": "$8"},
             {"kind": "server", "pid": 34500, "started_at": 1700000000},
             {"kind": "pane", "pane_id": "%8", "pane_pid": 62008}))

    def test_tmux_discovery_preserves_spaces_in_session_name(self) -> None:
        self.assertEqual(self.discover_tmux(" work ", "$8", "62008"),
            (" work ", {"kind": "session id", "session_id": "$8"},
             {"kind": "server", "pid": 34500, "started_at": 1700000000},
             {"kind": "pane", "pane_id": "%8", "pane_pid": 62008}))

    def test_tmux_discovery_keeps_session_when_pane_pid_is_unusable(self) -> None:
        self.assertEqual(self.discover_tmux("writing", "$9", "not-a-pid"),
            ("writing", {"kind": "session id", "session_id": "$9"},
             {"kind": "server", "pid": 34500, "started_at": 1700000000}, {"kind": "not recorded"}))

    def test_tmux_discovery_records_missing_session_id_without_losing_pane(self) -> None:
        self.assertEqual(self.discover_tmux("writing", "", "62009"),
            ("writing", {"kind": "not recorded"}, {"kind": "server", "pid": 34500, "started_at": 1700000000},
             {"kind": "pane", "pane_id": "%8", "pane_pid": 62009}))

    def test_tmux_discovery_records_missing_or_invalid_server_without_losing_ids(self) -> None:
        for started_at, include_start in (("1700000000", False), ("", True), ("not-an-integer", True)):
            with self.subTest(started_at=started_at, include_start=include_start):
                self.assertEqual(self.discover_tmux("writing", "$9", "62009", started_at, include_start=include_start),
                    ("writing", {"kind": "session id", "session_id": "$9"}, {"kind": "not recorded"},
                     {"kind": "pane", "pane_id": "%8", "pane_pid": 62009}))

    def test_tmux_discovery_records_missing_or_invalid_server_pid_without_losing_ids(self) -> None:
        for server_pid, include_pid in (("34500", False), ("", True), ("not-an-integer", True)):
            with self.subTest(server_pid=server_pid, include_pid=include_pid):
                self.assertEqual(self.discover_tmux("writing", "$9", "62009", server_pid=server_pid, include_pid=include_pid),
                    ("writing", {"kind": "session id", "session_id": "$9"}, {"kind": "not recorded"},
                     {"kind": "pane", "pane_id": "%8", "pane_pid": 62009}))

    def test_ghostty_host_carries_window_shell_between_terminal_and_claude(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(director["host"].get("window_shell"), DIRECTOR_SHELL_PID)

    def test_ghostty_supervisor_identity_is_discovered_with_cached_desktops(self) -> None:
        supervisor_pid = 99_999_999
        rows = cast(dict[str, list[object]], json.loads(os.environ["SHUTDOWN_TEST_PARENT_ROWS"]))
        rows[str(self.director_pid)] = [supervisor_pid, "claude"]
        rows[str(supervisor_pid)] = [DIRECTOR_SHELL_PID, "python3"]
        def cmdline(path: Path) -> bytes:
            if path == Path(f"/proc/{supervisor_pid}/cmdline"):
                return b"python3\0/tmp/relaunch.py\0run\0--\0claude\0"
            raise FileNotFoundError(path)
        for refresh in (True, False):
            with (
                self.subTest(refresh=refresh),
                patch.dict(os.environ, {"SHUTDOWN_TEST_PARENT_ROWS": json.dumps(rows)}),
                patch.object(Path, "read_bytes", autospec=True, side_effect=cmdline),
            ):
                found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}),
                    refresh_desktops=refresh)
                owner = next(item for item in found["sessions"] if item["session_id"] == DIRECTOR_ID)
                host = owner["host"]
                assert host["kind"] == "ghostty"
                self.assertEqual(host["window_shell"], DIRECTOR_SHELL_PID)
                self.assertEqual(host.get("supervisor"), {"kind": "supervisor", "pid": supervisor_pid,
                    "proc_start": CURRENT_PROCESS_START})

    def test_mac_ghostty_inventory_preserves_supervisor_and_legacy_terminal_host(self) -> None:
        rows = {
            101: "902 claude", 902: "901 python3", 901: "900 zsh",
            900: "1 /Applications/Ghostty.app/Contents/MacOS/ghostty",
        }
        def process(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            pid = int(argv[-1])
            output = ("python3 /tmp/relaunch.py run -- claude" if argv[2] == "args="
                      else rows[pid])
            return subprocess.CompletedProcess(argv, 0, output, "")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(Path, "read_bytes", side_effect=FileNotFoundError),
              patch.object(subprocess, "run", side_effect=process),
              patch.object(inventory, "_process_start", return_value="supervisor-start")):
            host = inventory._terminal_host(101, inventory.DesktopNotInSnapshot(kind="not in snapshot"))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(host, {"kind": "terminal", "supervisor": {
                "kind": "supervisor", "pid": 902, "proc_start": "supervisor-start"}})
            owner = top_level("mac-supervised", host=host)
            saved = entry(owner)
            parsed = record.parse_session_records(json.dumps([saved]))[0]
            self.assertEqual(parsed["session"]["host"], host)
            rows[101] = "901 claude"
            with patch.object(inventory, "_supervisor_process", return_value=inventory.NoSupervisor(kind="no supervisor")):
                legacy = inventory._terminal_host(101, inventory.DesktopNotInSnapshot(kind="not in snapshot"))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(legacy, {"kind": "terminal"})

    def test_ghostty_shell_parent_is_not_a_supervisor(self) -> None:
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError):
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}),
                refresh_desktops=False)
        owner = next(item for item in found["sessions"] if item["session_id"] == DIRECTOR_ID)
        host = owner["host"]
        assert host["kind"] == "ghostty"
        self.assertEqual(host.get("supervisor"), {"kind": "no supervisor"})

    def test_pre_supervisor_window_inventory_and_record_read_as_no_supervisor(self) -> None:
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        owner = found["sessions"][0]
        host = owner["host"]
        assert host["kind"] == "ghostty"
        _ = host.pop("supervisor", None)
        decoded = inventory.parse_inventory(json.dumps(found))["sessions"][0]["host"]
        assert decoded["kind"] == "ghostty"
        self.assertEqual(decoded.get("supervisor"), {"kind": "no supervisor"})
        saved = entry(owner)
        parsed = record.parse_session_records(json.dumps([saved]))[0]
        legacy_host = parsed["session"]["host"]
        assert legacy_host["kind"] == "ghostty"
        self.assertEqual(legacy_host.get("supervisor"), {"kind": "no supervisor"})

    def test_running_codex_worker_without_launcher_is_busy_but_dead_launcher_is_not(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(
            director["codex_servers"][0]["busy_workers"],
            ["busy-codex", "busy-without-launcher"],
        )
        self.assertNotIn("orphaned-start", director["codex_servers"][0]["busy_workers"])

    def test_a_showrunner_timer_naming_a_session_not_running_is_unregistered(self) -> None:
        before = {runner["slug"] for runner in inventory.unregistered_showrunners()}
        stale = self.notifier / "showrunner-stale"
        stale.mkdir()
        stale_doc = self.root / "stale-production.md"
        _ = stale_doc.write_text("- **User zone:** America/Los_Angeles\n", encoding="utf-8")
        _ = (stale / "conf").write_text(
            f"TARGET=session:00000000-0000-4000-8000-000000000000\nCHECK=zsh /opt/production_check.sh {stale_doc}\n",
            encoding="utf-8")
        self.assertEqual({runner["slug"] for runner in inventory.unregistered_showrunners()} - before, {"stale"})

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

    def test_inventory_reads_shared_worker_ledger_and_later_old_path_appends(self) -> None:
        self.write_session(DESKTOP_PID, DESKTOP_ID, "late-worker", self.root / "plain", kind="bg")
        current = self.delegate / "phase-two" / "workers"
        previous = current.with_name("seats")
        modified = current.stat().st_mtime_ns
        _ = current.replace(previous)
        found = inventory.inventory(TARGET_LOGIN)
        worker = next(session for session in found["sessions"] if session["session_id"] == WORKER_ID)
        self.assertEqual(worker["kind"], "worker")
        self.assertEqual(cast(inventory.WorkerSession, worker)["owner"],
                         {"kind": "director", "session_id": DIRECTOR_ID})
        late_worker = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(cast(inventory.WorkerSession, late_worker)["owner"], {"kind": "no director"})
        self.assertTrue(previous.samefile(current))
        self.assertEqual(current.stat().st_mtime_ns, modified)
        _ = inventory.inventory(TARGET_LOGIN)
        self.assertEqual(current.stat().st_mtime_ns, modified)
        with previous.open("a", encoding="utf-8") as ledger:
            _ = ledger.write(f"{DESKTOP_ID[:8]}\tlate-worker\n")
        self.assertIn("late-worker", current.read_text(encoding="utf-8"))
        modified = current.stat().st_mtime_ns
        found = inventory.inventory(TARGET_LOGIN)
        late_worker = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(late_worker["kind"], "worker")
        self.assertEqual(cast(inventory.WorkerSession, late_worker)["owner"],
                         {"kind": "director", "session_id": DIRECTOR_ID})
        self.assertTrue(previous.samefile(current))
        self.assertEqual(current.stat().st_mtime_ns, modified)

    def test_live_worker_keeps_owner_after_director_exits(self) -> None:
        (self.records / f"{self.director_pid}.json").unlink()

        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))

        self.assertEqual(
            [
                (session["session_id"], session["owner"])
                for session in found["sessions"]
                if session["kind"] == "worker"
            ],
            [(WORKER_ID, {"kind": "director", "session_id": DIRECTOR_ID})],
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
        (self.delegate / "phase-two" / "workers").unlink()
        transcript = (
            self.home / ".claude" / "projects" / "fixture" / f"{DESKTOP_ID}.jsonl"
        )
        transcript.unlink()

        found = inventory.inventory(TARGET_LOGIN)
        by_id = {session["session_id"]: session for session in found["sessions"]}

        unit = cast(inventory.UnitSession, by_id[UNIT_ID])
        worker = cast(inventory.WorkerSession, by_id[WORKER_ID])
        self.assertEqual(unit["host"]["plan"], {"kind": "no run record"})
        self.assertEqual(by_id[DESKTOP_ID]["model"], {"kind": "no reply yet"})
        self.assertEqual(worker["owner"], {"kind": "no director"})
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

    def test_config_dir_records_the_process_directory_for_the_same_login(self) -> None:
        default_dir = self.home / ".claude"
        default_dir.mkdir(exist_ok=True)
        _ = (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": TARGET_LOGIN}}),
            encoding="utf-8",
        )

        with patch.object(account, "process_config_dir", return_value=default_dir):
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))

        self.assertEqual(len(found["sessions"]), 1)
        self.assertEqual(found["sessions"][0].get("config_dir"), str(default_dir))

    def test_account_identity_and_config_dir_share_one_process_lookup(self) -> None:
        """F007: capture the account and directory from the same environment read."""
        looked_up: list[int] = []

        def config_dir_for_pid(pid: int) -> Path:
            looked_up.append(pid)
            return self.target_config if len(looked_up) == 1 else self.other_config

        with patch.object(account, "process_config_dir", side_effect=config_dir_for_pid):
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))

        self.assertEqual(len(found["sessions"]), 1)
        self.assertEqual(found["sessions"][0].get("config_dir"), str(self.target_config))
        self.assertEqual(looked_up, [DESKTOP_PID])

    def test_unreadable_session_configuration_never_defaults_to_main(self) -> None:
        """F007: unreadable environment or identity uses the same unattributed path."""
        for reader in ("process_config_dir", "claude_account"):
            with (
                self.subTest(reader=reader),
                patch.object(account, reader, return_value=None),
            ):
                found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))

            self.assertEqual(found["sessions"], [])
            self.assertEqual(
                found["unattributed"],
                [{"pid": DESKTOP_PID, "name": "notes", "reason": "account unreadable"}],
            )

    def test_parse_inventory_validates_config_dir_and_accepts_legacy_absence(self) -> None:
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        session_values = cast(dict[str, object], cast(object, found["sessions"][0]))
        session_values["config_dir"] = 123
        with self.assertRaisesRegex(
            inventory.InvalidInventory,
            r"inventory\.sessions\[0\]\.config_dir must be a string",
        ):
            _ = inventory.parse_inventory(json.dumps(found))

        del session_values["config_dir"]
        self.assertEqual(inventory.parse_inventory(json.dumps(found)), found)

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
                "session_ref": {"kind": "not recorded"},
                "server": {"kind": "not recorded"},
                "pane": {"kind": "not recorded"},
            },
        )

    def test_parse_inventory_restores_session_reference_and_preserves_legacy_pane(self) -> None:
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        document = cast(dict[str, object], cast(object, json.loads(json.dumps(found))))
        session_values = cast(list[object], document["sessions"])
        owner = cast(dict[str, object], session_values[0])
        host: dict[str, object] = {"kind": "tmux", "tmux_session": "old name",
            "pane": {"kind": "pane", "pane_id": "%8", "pane_pid": 700},
            "session_ref": {"kind": "session id", "session_id": "$4"},
            "server": {"kind": "server", "pid": 34500, "started_at": 1700000000}}
        owner["host"] = host
        parsed = inventory.parse_inventory(json.dumps(document))["sessions"][0]["host"]
        self.assertEqual(parsed, host)
        host["server"] = {"kind": "server", "started_at": 1700000000}
        parsed = inventory.parse_inventory(json.dumps(document))["sessions"][0]["host"]
        self.assertEqual(parsed, {**host, "server": {"kind": "not recorded"}})
        del host["server"]
        parsed = inventory.parse_inventory(json.dumps(document))["sessions"][0]["host"]
        self.assertEqual(parsed, {**host, "server": {"kind": "not recorded"}})
        del host["session_ref"]
        parsed = inventory.parse_inventory(json.dumps(document))["sessions"][0]["host"]
        self.assertEqual(parsed, {**host, "session_ref": {"kind": "not recorded"}, "server": {"kind": "not recorded"}})

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


class ProcessEnvironmentTests(unittest.TestCase):
    def test_darwin_environment_excludes_assignments_in_arguments(self) -> None:
        arguments = 'claude --resume conversation "TMUX_PANE=%41 CLAUDE_CONFIG_DIR=/x"'
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, f"  {arguments}\n", ""),
            subprocess.CompletedProcess[str]([], 0, f"  {arguments} HERDR_PANE_ID=w1:p1 REAL_ENTRY=value=with=equals\n", ""),
            subprocess.CompletedProcess[str]([], 0, f"  {arguments}\n", ""),
        ]) as probe:
            environment = inventory._process_environment(42)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(environment, {"HERDR_PANE_ID": "w1:p1", "REAL_ENTRY": "value=with=equals"})
        self.assertEqual(probe.call_args_list[0].args[0], ("ps", "-o", "command=", "-p", "42"))
        self.assertEqual(probe.call_args_list[1].args[0], ("ps", "eww", "-o", "command=", "-p", "42"))
        self.assertEqual(probe.call_args_list[2].args[0], ("ps", "-o", "command=", "-p", "42"))
        self.assertEqual(probe.call_count, 3)

    def test_darwin_environment_refuses_arguments_added_between_reads(self) -> None:
        arguments = "claude --resume conversation"
        extended_arguments = arguments + " TMUX_PANE=%41 CLAUDE_CONFIG_DIR=/x"
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
            subprocess.CompletedProcess[str]([], 0, extended_arguments + "\n", ""),
            subprocess.CompletedProcess[str]([], 0, extended_arguments + "\n", ""),
        ]) as probe:
            self.assertEqual(inventory._process_environment(42), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(probe.call_count, 3)

    def test_darwin_environment_refuses_changed_or_empty_command(self) -> None:
        for arguments, expanded in (
            ("claude --resume old", "claude --resume new TMUX_PANE=%41"),
            ("claude", "claude-other TMUX_PANE=%41"),
            ("", "TMUX_PANE=%41"),
        ):
            with self.subTest(arguments=arguments, expanded=expanded):
                with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[
                    subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
                    subprocess.CompletedProcess[str]([], 0, expanded + "\n", ""),
                    subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
                ]):
                    self.assertEqual(inventory._process_environment(42), {})  # pyright: ignore[reportPrivateUsage]

    def test_darwin_environment_is_empty_when_any_ps_call_fails(self) -> None:
        for failed_call in (0, 1, 2):
            for failure in (
                subprocess.CompletedProcess[str]([], 1, "claude TMUX_PANE=%41", "gone"),
                OSError("process vanished"),
                subprocess.TimeoutExpired("ps", 5),
            ):
                with self.subTest(failed_call=failed_call, failure=failure):
                    answers = [subprocess.CompletedProcess[str]([], 0, "claude\n", "")] * failed_call + [failure]
                    with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=answers) as probe:
                        self.assertEqual(inventory._process_environment(42), {})  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(probe.call_count, failed_call + 1)

    def test_darwin_command_without_environment_contributes_no_assignments(self) -> None:
        arguments = "claude TMUX_PANE=%41 CLAUDE_CONFIG_DIR=/x"
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
            subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
            subprocess.CompletedProcess[str]([], 0, arguments + "\n", ""),
        ]):
            self.assertEqual(inventory._process_environment(42), {})  # pyright: ignore[reportPrivateUsage]

    def test_linux_environment_still_reads_null_delimited_proc_entries(self) -> None:
        with patch.object(sys, "platform", "linux"), patch.object(Path, "read_bytes", return_value=(
            b"TMUX_PANE=%41\0CLAUDE_CONFIG_DIR=/real account\0INVALID\0VALUE=a=b\0"
        )) as read_environment, patch.object(subprocess, "run") as probe:
            self.assertEqual(inventory._process_environment(42), {  # pyright: ignore[reportPrivateUsage]
                "TMUX_PANE": "%41", "CLAUDE_CONFIG_DIR": "/real account", "VALUE": "a=b",
            })
        read_environment.assert_called_once_with()
        probe.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
