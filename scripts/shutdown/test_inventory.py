"""Behavioral tests for account-scoped shutdown inventory discovery."""

from __future__ import annotations

import copy
import json
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from scripts.lib import process_start as process_identity
from scripts.lib.process_start_test_support import write_stat
from typing import ClassVar, cast, final, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.socket_test_support import short_socket_directory
from scripts.production.staff_test_support import unit_membership
from scripts.production import staff
from test_support import ShutdownTestCase, linux_process_fixture
from test_restart import entry, top_level, worker_session
import shutdown_record
import inventory
import unit_runs
from scripts.whoami import account
import herdr
import sessions
from scripts.production import showrunners
import unit_lookup
import restart
import shutdown
from fake_herdr import CommandResponse
from cli_fixture import ImmutableGitSeed, invoke_cli
import fake_herdr


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
REAL_HERDR_RUN_PROGRAM = herdr.run_program


def replay_herdr_commands(test: unittest.TestCase) -> None:
    """Run the recording fake's real entry point without a Python process per read."""
    fake = str(Path(__file__).parents[1] / "herdr/fake_herdr.py")

    def run_herdr(argv: list[str], timeout: float = 10.0, *,
                  environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if argv[0] != fake:
            return REAL_HERDR_RUN_PROGRAM(argv, timeout, environment=environment)

        def execute(command: list[str], *, capture_output: bool, text: bool, check: bool,
                    timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            test.assertTrue(capture_output and text)
            test.assertFalse(check)
            test.assertGreater(timeout, 0)
            return invoke_cli(fake_herdr, lambda: fake_herdr.main(command[1:]), command[1:],
                              env, Path.cwd())

        # Retain herdr's environment sanitization and every inventory parser.
        with patch.object(subprocess, "run", execute):
            return REAL_HERDR_RUN_PROGRAM(argv, timeout, environment=environment)

    _ = test.enterContext(patch.object(herdr, "run_program", run_herdr))


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


class InventoryImportTests(unittest.TestCase):
    def test_package_inventory_imports_without_shutdown_on_search_path(self) -> None:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "-I", "-c", """
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
assert str(root / "scripts/shutdown") not in sys.path
from scripts.shutdown import inventory
assert inventory.codex_seats([]) == []
""", str(root)],
            cwd=root / "scripts" / "herdr",
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@final
class InventoryTests(ShutdownTestCase):
    seed: ClassVar[ImmutableGitSeed]
    seed_head: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="inventory-seed-")))
        fixture = cls()
        fixture.root = root
        fixture.checkout = root / "checkout"
        fixture.checkout.mkdir()
        fixture.origin = root / "origin.git"
        fixture.build_git_checkout()
        cls.seed = ImmutableGitSeed(fixture.checkout, fixture.origin)
        cls.seed_head = fixture.head
        fixture.live_socket.close()

    def test_codex_seats_records_nearest_launcher_argv_and_excludes_ended_and_other_runs(self) -> None:
        run = self.root / "paused run"
        run.mkdir()
        command = ["bash", "/config/scripts/direct/review.sh", "--phase", "1", str(run), "/checkout with spaces", "/old/prompt.md", "review-task", "review instructions", "review task", "1", "api"]
        outer = ["bash", "/config/scripts/direct/implement.sh", "outer arguments"]
        self.herdr_process(810, 811, ["python3", "codex_mesh.py", "follow"], {})
        self.herdr_process(811, 812, command, {})
        self.herdr_process(812, 1, outer, {})
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            "busy": {"role": "review", "thread_id": "busy-thread", "turn_id": "turn", "status": "running", "launcher_pid": 810},
            "idle": {"role": "impl", "thread_id": "idle-thread", "status": "done"},
            "orphan": {"role": "impl", "thread_id": "orphan-thread", "status": "starting", "launcher_pid": 99999999},
            "ended": {"role": "impl", "thread_id": "ended-thread", "status": "ended"},
        }))
        def launcher_alive(pid: int) -> bool:
            return pid in {810, 811, 812}

        with patch.object(inventory, "_pid_alive", side_effect=launcher_alive):
            found = inventory.codex_seats([run])
        seats = {seat["seat"]: seat for seat in found}
        self.assertEqual(set(seats), {"busy", "idle", "orphan"})
        self.assertEqual(seats["busy"], shutdown_record.PausedSeat(run_dir=str(run), seat="busy", role="review", thread_id="busy-thread", turn_live=True, launcher_command=command))
        self.assertEqual(seats["idle"], shutdown_record.PausedSeat(run_dir=str(run), seat="idle", role="impl", thread_id="idle-thread", turn_live=False, launcher_command=[]))
        self.assertEqual(seats["orphan"]["launcher_command"], [])

    def test_codex_seats_leaves_out_a_run_its_director_left_after_an_earlier_stop_offered_it(self) -> None:
        def run_with(name: str, statuses: dict[str, str], *, board_at: int, offered_at: int | None) -> Path:
            run = self.root / name
            run.mkdir()
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                seat: {"role": "test", "thread_id": f"{seat}-thread", "status": status}
                for seat, status in statuses.items()
            }))
            board = run / "board.log"
            _ = board.write_text("[director] dispatch\n")
            os.utime(board, (board_at, board_at))
            if offered_at is not None:
                for seat in statuses:
                    prompt = run / f"{seat}.resume-prompt.md"
                    _ = prompt.write_text("paused\n")
                    os.utime(prompt, (offered_at, offered_at))
            return run

        left = run_with("left", {"a75-test": "paused", "p39a-impl": "paused"}, board_at=1_000, offered_at=2_000)
        taken_back = run_with("taken back", {"impl": "paused"}, board_at=3_000, offered_at=2_000)
        dispatched = run_with("dispatched", {"impl": "paused", "test": "done"}, board_at=1_000, offered_at=2_000)
        never_offered = run_with("never offered", {"impl": "paused"}, board_at=1_000, offered_at=None)

        found = inventory.codex_seats([left, taken_back, dispatched, never_offered])

        self.assertEqual(
            sorted((Path(seat["run_dir"]).name, seat["seat"]) for seat in found),
            [("dispatched", "impl"), ("dispatched", "test"), ("never offered", "impl"), ("taken back", "impl")],
        )

    def test_old_shutdown_record_reads_back_without_codex_seats_and_new_seats_survive_wire_roundtrip(self) -> None:
        saved = entry(top_level("legacy-director"))
        document = cast(dict[str, object], json.loads(json.dumps(saved)))
        _ = document.pop("codex_seats", None)
        parsed = shutdown_record.parse_session_records(json.dumps([document]))[0]
        self.assertNotIn("codex_seats", parsed)
        seats = [shutdown_record.PausedSeat(run_dir="/run", seat="impl", role="impl", thread_id="thread-kept", turn_live=True, launcher_command=["bash", "/scripts/implement.sh", "argument with spaces"])]
        document["codex_seats"] = seats
        parsed = shutdown_record.parse_session_records(json.dumps([document]))[0]
        self.assertEqual(parsed.get("codex_seats"), seats)
        document["codex_seats"] = [{**seats[0], "turn_live": "true"}]
        with self.assertRaisesRegex(shutdown_record.InvalidRecord, "turn_live"):
            _ = shutdown_record.parse_session_records(json.dumps([document]))

    def test_updated_at_preserves_registry_milliseconds_through_inventory_wire_parse(self) -> None:
        timestamp = 1_791_563_350_123
        self.write_session(DESKTOP_PID, DESKTOP_ID, "notes", self.root / "plain", updated_at=timestamp)
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        self.assertEqual(found["sessions"][0]["updated_at"], timestamp)
        decoded = inventory.parse_inventory(json.dumps(found))
        self.assertEqual(decoded["sessions"][0]["updated_at"], timestamp)

    def test_updated_at_is_required_and_must_be_an_integer_in_fresh_inventory(self) -> None:
        found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        values = cast(dict[str, object], cast(object, found["sessions"][0]))
        for invalid in (True, 12.5, "123", None):
            with self.subTest(updated_at=invalid):
                values["updated_at"] = invalid
                with self.assertRaisesRegex(inventory.InvalidInventory, r"updated_at"):
                    _ = inventory.parse_inventory(json.dumps(found))
        del values["updated_at"]
        with self.assertRaisesRegex(inventory.InvalidInventory, r"updated_at"):
            _ = inventory.parse_inventory(json.dumps(found))

    def test_shutdown_record_without_updated_at_remains_readable(self) -> None:
        saved = entry(top_level("legacy-owner"))
        document = cast(dict[str, object], json.loads(json.dumps(saved)))
        owner = cast(dict[str, object], document["session"])
        del owner["updated_at"]
        parsed = shutdown_record.parse_session_records(json.dumps([document]))[0]
        self.assertEqual(parsed["session"]["updated_at"], 0)
        self.assertEqual(parsed["session"]["session_id"], "legacy-owner")

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

    def test_run_markers_under_either_folder_name_map_their_directors(self) -> None:
        for name in ("unit", "delegate"):
            (self.root / name / "active").mkdir(parents=True, exist_ok=True)
            _ = (self.root / name / "active" / f"{name}-director").write_text(f"/runs/{name}\n", encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key != "SHUTDOWN_RUN_ROOT"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(unit_runs, "RUN_ROOT", self.root / "unit"), \
                patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "delegate"):
            found = inventory._active_run_directories()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(
            (found.get("unit-director"), found.get("delegate-director")),
            ([Path("/runs/unit")], [Path("/runs/delegate")]),
        )

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
                    self.assertEqual(light.production, "build-followups")
                    self.assertNotIn("production", session["host"])
                    self.assertEqual(light.doc, str(self.root / "build-followups-production.md"))
                    self.assertNotIn("doc", session["host"])

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
        with patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.target_config)):
            listed = inventory.listing(TARGET_LOGIN)
        self.assertEqual({session.session_id for session in listed}, identities)
        self.assertGreaterEqual(transcript.stat().st_size, 50 * 1024 * 1024)
        with TranscriptReadCounter(transcript) as stream:
            with patch.object(Path, "open", return_value=stream):
                model = inventory.last_model(transcript)
            self.assertEqual(model, "large-file-model")
            self.assertGreater(stream.bytes_read, 0)
            self.assertLess(stream.bytes_read, 1024 * 1024)


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
        self.runs = Path()
        self.checkout = Path()
        self.origin = Path()
        self.bin = Path()
        self.proc = Path()
        self.herdr_log = Path()
        self.pane_processes: dict[int, tuple[int, list[str], dict[str, str]]] = {}
        self.target_config = Path()
        self.other_config = Path()
        self.director_pid = 0
        self.live_socket = socket.socket(socket.AF_UNIX)
        self.head = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        linux_process_fixture(self)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.notes = self.root / "agents"
        self.records = self.root / "sessions"
        self.notifier = self.root / "notifier"
        self.history = self.root / "history"
        self.runs = self.root / "runs"
        self.checkout = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.bin = self.root / "bin"
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
            self.runs / "active",
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
            "UNIT_HISTORY_DIR": str(self.history),
            "SHUTDOWN_RUN_ROOT": str(self.runs),
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr" / "fake_herdr.py"),
            "HERDR_PROC": str(self.proc),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
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
        _ = self.enterContext(
            patch.object(process_identity, "process_start", return_value=process_identity.parse_start(CURRENT_PROCESS_START))
        )

        self.director_pid = os.getpid()
        socket_path = Path(self.enterContext(short_socket_directory())) / "director.sock"
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
        def config_dir_for_pid(pid: int) -> account.ConfiguredClaudeFolder | account.ProcessEnvironmentUnread:
            directory = config_by_pid.get(pid)
            return account.ConfiguredClaudeFolder(directory) if directory is not None else account.ProcessEnvironmentUnread("unreadable")

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
        self.make_worker_state()
        replay_herdr_commands(self)

    def write_account(self, config_dir: Path, login: str) -> None:
        _ = (config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": login}}),
            encoding="utf-8",
        )

    def herdr_process(self, pid: int, parent: int, arguments: list[str], environment: dict[str, str]) -> None:
        process = self.proc / str(pid)
        process.mkdir(exist_ok=True)
        _ = write_stat(process.parent, int(process.name), ppid=parent)
        _ = (process / "cmdline").write_bytes(("\0".join(arguments) + "\0").encode())
        _ = (process / "environ").write_bytes(
            ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode()
        )
        (process / "cwd").unlink(missing_ok=True)
        (process / "cwd").symlink_to(self.checkout)
        self.pane_processes[pid] = (parent, arguments, environment)
        if arguments and Path(arguments[0]).name == "herdr" and arguments[-1] == "server":
            name = arguments[arguments.index("--session") + 1] if "--session" in arguments else "default"
            config = self.home / ".config/herdr"
            directory = config if name == "default" else config / "sessions" / name
            sockets = self.proc / "net/unix"
            sockets.parent.mkdir(exist_ok=True)
            previous = sockets.read_text() if sockets.exists() else ""
            _ = sockets.write_text(previous + f"000: 00000002 00000000 00010000 0001 01 {pid} {directory}/herdr.sock\n")
            (process / "fd").mkdir()
            (process / "fd/3").symlink_to(f"socket:[{pid}]")
        self.configure_process_panes()

    def configure_process_panes(self) -> None:
        """Publish each fake server's live panes and the shell that owns each pane."""
        path = self.root / "herdr-responses.json"
        responses = cast(dict[str, CommandResponse], json.loads(path.read_text()))
        panes: dict[str, dict[str, int]] = {}
        for pid, (_, arguments, environment) in self.pane_processes.items():
            if arguments and Path(arguments[0]).name == "herdr" and arguments[-1] == "server":
                session = arguments[arguments.index("--session") + 1] if "--session" in arguments else "default"
                _ = panes.setdefault(session, {})
            pane = environment.get("HERDR_PANE_ID")
            if not pane or environment.get("TMUX_PANE"):
                continue
            ancestor, shell = pid, pid
            seen: set[int] = set()
            while ancestor in self.pane_processes and ancestor not in seen:
                seen.add(ancestor)
                parent, arguments, marks = self.pane_processes[ancestor]
                if arguments and Path(arguments[0]).name == "herdr" and arguments[-1] == "server":
                    session = arguments[arguments.index("--session") + 1] if "--session" in arguments else "default"
                    panes.setdefault(session, {})[pane] = shell
                    break
                if marks.get("HERDR_PANE_ID") == pane:
                    shell = ancestor
                ancestor = parent
        for session, shells in panes.items():
            responses[f"--session {session} pane list"] = {"code": 0, "stdout": json.dumps({"result": {
                "panes": [{"pane_id": pane, "workspace_id": pane.split(":", 1)[0]} for pane in shells]}})}
            for pane, shell in shells.items():
                responses[f"--session {session} pane process-info --pane {pane}"] = {
                    "code": 0, "stdout": json.dumps({"result": {"process_info": {"shell_pid": shell}}})}
        _ = path.write_text(json.dumps(responses))

    def herdr_sessions(self, names: list[str]) -> None:
        entries = [herdr.SessionEntry(name=name, running=True, default=False,
                                    session_dir=str(self.root / name), socket_path="fake-socket") for name in names]
        response = CommandResponse(stdout=json.dumps(herdr.SessionListResult(sessions=entries)), code=0)
        path = self.root / "herdr-responses.json"
        responses = cast(dict[str, CommandResponse], json.loads(path.read_text())) if path.exists() else {}
        responses["default"] = response
        _ = path.write_text(json.dumps(responses))

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
        # ProbeInventoryTests also constructs this fixture without a class lifecycle.
        if not hasattr(type(self), "seed") or not self.seed.checkout.is_dir():
            self.build_git_checkout()
            return
        self.checkout.rmdir()
        self.seed.copy_to(self.checkout, self.origin)
        self.head = self.seed_head

    def build_git_checkout(self) -> None:
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

    def write_executable(self, name: str, body: str, *, interpreter: str = sys.executable) -> None:
        path = self.bin / name
        _ = path.write_text(f"#!{interpreter}\n{body}", encoding="utf-8")
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
            """import copy
import json
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
        self.write_executable("kdotool", "exit 1\n", interpreter="/bin/sh")

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
        member = unit_membership("build-followups", "shutdown-unit")
        assert member.parent is not None
        parent = staff.by_id(member.parent)
        assert parent is not None
        _ = staff.update_current_session(parent.current_session, staff.StaffId(DIRECTOR_ID))
        _ = staff.update_current_session(member.current_session, staff.StaffId(UNIT_ID))
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

        self.herdr_sessions(["unit-renamed"])
        self.herdr_process(72_001, 1, ["/fixture/herdr", "--session", "unit-renamed", "server"], {})
        self.herdr_process(UNIT_PID, 72_001, ["/bin/claude"], {
            "HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "build-followups",
            "SHOWRUNNER_UNIT_ID": "shutdown-unit",
        })
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

    def make_worker_state(self) -> None:
        run_dir = self.runs / "phase-two"
        run_dir.mkdir()
        _ = (self.runs / "active" / DIRECTOR_ID).write_text(
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


    def test_inventory_is_account_scoped_and_discovers_every_owned_resource(self) -> None:
        _ = self.enterContext(patch.object(herdr, "run_program", REAL_HERDR_RUN_PROGRAM))
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
            {"kind": "not in snapshot"},
        )
        self.assertEqual(director["production"], "build-followups")
        self.assertEqual(
            director["doc"], str(self.root / "build-followups-production.md")
        )
        self.assertEqual(director["run_dirs"], [str(self.runs / "phase-two")])
        self.assertEqual(
            director["codex_servers"],
            [
                {
                    "run_dir": str(self.runs / "phase-two"),
                    "pid": os.getpid(),
                    "busy_workers": ["busy-codex"],
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
                "unit": "shutdown-unit",
                "plan": {"kind": "plan", "path": "docs/shutdown-plan.md"},
                "session_name": "unit-renamed",
                "session_host": "herdr",
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
                "desktop": {"kind": "not in snapshot"},
                "window_shell": DESKTOP_SHELL_PID,
                "supervisor": {"kind": "no supervisor"},
            },
        )

        limited = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))
        self.assertEqual(
            {session["session_id"] for session in limited["sessions"]},
            {DIRECTOR_ID, WORKER_ID},
        )

    def test_truncated_wrapped_ghostty_name_has_no_desktop_observation(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(director["host"]["kind"], "ghostty")
        self.assertEqual(
            director["host"].get("desktop"),
            {"kind": "not in snapshot"},
        )


    def test_herdr_host_comes_from_the_process_server_ancestor(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "writing", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"],
                           {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "inherited-other-name"})
        self.assertEqual(herdr.herdr_place_of(DESKTOP_PID), herdr.HerdrPlace("writing", "w1:p1"))
        found = inventory.inventory(TARGET_LOGIN)
        desktop = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(desktop["host"], {"kind": "herdr", "herdr_session": "writing", "pane_id": "w1:p1", "presentation": "detached"})
        self.assertEqual(inventory.parse_inventory(json.dumps(found)), found)
        self.assertEqual(shutdown._host_text(desktop["host"]), "herdr writing")  # pyright: ignore[reportPrivateUsage]

    def test_herdr_inventory_records_attached_clients_and_defaults_unreadable_clients_to_detached(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "writing", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"], {"HERDR_PANE_ID": "w1:p1"})
        for clients, expected in ((herdr.ClientsAttached(2), "attached window"),
                                  (herdr.NoClientAttached(), "detached"),
                                  (herdr.ClientsUnreadable("cannot read processes"), "detached")):
            with self.subTest(clients=clients), patch.object(herdr, "attached_clients", return_value=clients):
                found = inventory.inventory(TARGET_LOGIN)
            desktop = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
            self.assertEqual(desktop["host"].get("presentation"), expected)
            parsed = inventory.parse_inventory(json.dumps(found))
            self.assertEqual(parsed, found)

    def test_old_herdr_inventory_reads_detached_and_rejects_invalid_presentation(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "writing", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"], {"HERDR_PANE_ID": "w1:p1"})
        found = inventory.inventory(TARGET_LOGIN)
        wire = cast(dict[str, object], json.loads(json.dumps(found)))
        owners = cast(list[dict[str, object]], wire["sessions"])
        host = cast(dict[str, object], next(owner for owner in owners if owner["session_id"] == DESKTOP_ID)["host"])
        _ = host.pop("presentation")
        parsed = inventory.parse_inventory(json.dumps(wire))
        desktop = next(owner for owner in parsed["sessions"] if owner["session_id"] == DESKTOP_ID)
        self.assertEqual(desktop["host"].get("presentation"), "detached")
        for invalid in ("window", 3, {"kind": "invalid"}, None):
            with self.subTest(invalid=invalid):
                host["presentation"] = invalid
                with self.assertRaises(inventory.InvalidInventory):
                    _ = inventory.parse_inventory(json.dumps(wire))

    def test_herdr_unit_records_its_host_and_live_session_name(self) -> None:
        self.herdr_sessions(["unit-herdr"])
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
        self.assertEqual(shutdown._host_text(unit["host"]), "unit shutdown-unit (herdr unit-herdr)")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(unit["host"].get("plan"), {"kind": "plan", "path": "docs/shutdown-plan.md"})


    def test_user_tmux_is_unknown_despite_inherited_herdr_environment(self) -> None:
        self.herdr_process(71_001, 1, ["/fixture/herdr", "--session", "outer", "server"], {})
        self.herdr_process(DESKTOP_PID, 71_001, ["/bin/claude"], {"HERDR_PANE_ID": "w1:p1"})
        with patch("inventory._process_environment", return_value={"TMUX_PANE": "%9", "HERDR_PANE_ID": "w1:p1"}):
            found = inventory.inventory(TARGET_LOGIN)
        desktop = next(session for session in found["sessions"] if session["session_id"] == DESKTOP_ID)
        self.assertEqual(desktop["host"], {"kind": "unknown"})

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
                found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))
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
              patch.object(process_identity, "process_start", return_value=process_identity.parse_start("supervisor-start"))):
            host = inventory._terminal_host(101, inventory.DesktopNotInSnapshot(kind="not in snapshot"))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(host, {"kind": "terminal", "supervisor": {
                "kind": "supervisor", "pid": 902, "proc_start": "supervisor-start"}})
            owner = top_level("mac-supervised", host=host)
            saved = entry(owner)
            parsed = shutdown_record.parse_session_records(json.dumps([saved]))[0]
            self.assertEqual(parsed["session"]["host"], host)
            rows[101] = "901 claude"
            with patch.object(inventory, "_supervisor_process", return_value=inventory.NoSupervisor(kind="no supervisor")):
                legacy = inventory._terminal_host(101, inventory.DesktopNotInSnapshot(kind="not in snapshot"))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(legacy, {"kind": "terminal"})

    def test_ghostty_shell_parent_is_not_a_supervisor(self) -> None:
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError):
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DIRECTOR_ID}))
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
        parsed = shutdown_record.parse_session_records(json.dumps([saved]))[0]
        legacy_host = parsed["session"]["host"]
        assert legacy_host["kind"] == "ghostty"
        self.assertEqual(legacy_host.get("supervisor"), {"kind": "no supervisor"})

    def test_only_running_codex_workers_with_live_launchers_are_busy(self) -> None:
        found = inventory.inventory(TARGET_LOGIN)
        director = next(
            session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID
        )

        self.assertEqual(
            director["codex_servers"][0]["busy_workers"],
            ["busy-codex"],
        )
        self.assertNotIn("orphaned-start", director["codex_servers"][0]["busy_workers"])

    def test_stale_running_entries_with_dead_or_missing_launchers_are_not_busy(self) -> None:
        run_dir = self.runs / "phase-two"
        roster = cast(dict[str, dict[str, object]], json.loads((run_dir / "mesh_roster.json").read_text()))
        roster["orphaned-start"]["status"] = "running"
        _ = (run_dir / "mesh_roster.json").write_text(json.dumps(roster))
        found = inventory.inventory(TARGET_LOGIN)
        director = next(session for session in found["sessions"] if session["session_id"] == DIRECTOR_ID)
        self.assertEqual(director["codex_servers"][0]["busy_workers"], ["busy-codex"])

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
        record["procStart"] = "123"
        _ = path.write_text(json.dumps(record), encoding="utf-8")

        with patch.object(process_identity, "process_start", return_value=process_identity.ProcessStartTime(456)):
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
        current = self.runs / "phase-two" / "workers"
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
        (self.runs / "phase-two" / "workers").unlink()
        transcript = (
            self.home / ".claude" / "projects" / "fixture" / f"{DESKTOP_ID}.jsonl"
        )
        transcript.unlink()

        found = inventory.inventory(TARGET_LOGIN)
        by_id = {session["session_id"]: session for session in found["sessions"]}

        unit = cast(inventory.UnitSession, by_id[UNIT_ID])
        worker = cast(inventory.WorkerSession, by_id[WORKER_ID])
        assert unit["host"]["kind"] == "unit"
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

        with patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(default_dir)):
            found = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))

        self.assertEqual(len(found["sessions"]), 1)
        self.assertEqual(found["sessions"][0].get("config_dir"), str(default_dir))

    def test_process_without_an_account_setting_is_attributed_only_until_home_cutover(self) -> None:
        home_json = self.home / ".claude.json"
        _ = home_json.write_text(json.dumps({"oauthAccount": {"emailAddress": TARGET_LOGIN}}))
        with patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()):
            before = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
            _ = home_json.write_text("{}")
            after = inventory.inventory(TARGET_LOGIN, only=frozenset({DESKTOP_ID}))
        self.assertEqual([item["session_id"] for item in before["sessions"]], [DESKTOP_ID])
        self.assertEqual(before["sessions"][0].get("config_dir"), str(self.home / ".claude"))
        self.assertEqual(after["sessions"], [])
        self.assertEqual(after["unattributed"], [{"pid": DESKTOP_PID, "name": "notes", "reason": "account unreadable"}])

    def test_listing_and_inventory_share_home_identity_until_the_next_pass(self) -> None:
        identities = frozenset({DESKTOP_ID, UNIT_ID})
        owners = [record for record in sessions.live_sessions() if record["sessionId"] in identities]
        self.assertEqual(len(owners), 2)
        home_json = self.home / ".claude.json"

        def cutover_during_process_read(_pid: int) -> account.NoAccountSetting:
            _ = home_json.write_text("{}")
            return account.NoAccountSetting()

        for consumer in ("listing", "inventory"):
            with self.subTest(consumer=consumer):
                _ = home_json.write_text(json.dumps({"oauthAccount": {"emailAddress": TARGET_LOGIN}}))
                with (patch.object(sessions, "live_sessions", return_value=owners),
                      patch.object(account, "process_config_dir", side_effect=cutover_during_process_read),
                      patch.object(account, "home_account_state", wraps=account.home_account_state) as home_reads):
                    if consumer == "listing":
                        before = inventory.listing(TARGET_LOGIN)
                        after = inventory.listing(TARGET_LOGIN)
                        found_ids = {item.session_id for item in before}
                    else:
                        found = inventory.inventory(TARGET_LOGIN, only=identities)
                        found_ids = {item["session_id"] for item in found["sessions"]}
                        after = inventory.inventory(TARGET_LOGIN, only=identities)["sessions"]
                self.assertEqual(found_ids, identities)
                self.assertEqual(after, [])
                self.assertEqual(home_reads.call_count, 2)

    def test_account_identity_and_config_dir_share_one_process_lookup(self) -> None:
        """F007: capture the account and directory from the same environment read."""
        looked_up: list[int] = []

        def config_dir_for_pid(pid: int) -> account.ConfiguredClaudeFolder:
            looked_up.append(pid)
            return account.ConfiguredClaudeFolder(self.target_config if len(looked_up) == 1 else self.other_config)

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
                patch.object(account, reader, return_value=account.ProcessEnvironmentUnread("unreadable") if reader == "process_config_dir" else None),
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

    def test_parse_inventory_normalizes_a_legacy_tmux_host_to_manual_recovery(self) -> None:
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

        self.assertEqual(parsed["host"], {"kind": "legacy tmux", "session_name": "legacy"})

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
        self.assertEqual(probe.call_args_list[0].args[0], ["ps", "-o", "command=", "-p", "42"])
        self.assertEqual(probe.call_args_list[1].args[0], ["ps", "eww", "-o", "command=", "-p", "42"])
        self.assertEqual(probe.call_args_list[2].args[0], ["ps", "-o", "command=", "-p", "42"])
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


class FreshSessionTests(ShutdownTestCase):
    def test_fresh_session_reports_owned_workers_and_preserves_the_recorded_identity(self) -> None:
        owner = top_level('owner')
        owner['pid'] = 101
        worker = worker_session('worker', owner='owner')
        unrelated = worker_session('other', owner='elsewhere')
        observed = inventory.fresh_session(owner, inventory.Inventory(machine="natedev", login=TARGET_LOGIN, label="claude2", sessions=[owner, worker, unrelated], unattributed=[]))
        self.assertEqual(observed, {'kind': 'matching session', 'session': owner, 'workers': [worker]})
        changed = copy.deepcopy(owner)
        changed['pid'] = 201
        self.assertEqual(inventory.fresh_session(owner, inventory.Inventory(machine="natedev", login=TARGET_LOGIN, label="claude2", sessions=[changed], unattributed=[])), {'kind': 'session identity lost'})
        self.assertEqual(owner['pid'], 101)


    def test_fresh_session_rejects_any_resumed_occurrence_even_beside_the_recorded_process(self) -> None:
        owner = top_level("owner")
        owner["pid"] = 101
        resumed = copy.deepcopy(owner)
        resumed.update(pid=501, proc_start="200")
        reports: tuple[list[inventory.Session], ...] = ([owner, resumed], [resumed, owner])
        for rows in reports:
            with self.subTest(pids=[row["pid"] for row in rows]):
                self.assertEqual(inventory.fresh_session(owner, inventory.Inventory(machine="natedev", login=TARGET_LOGIN, label="claude2", sessions=rows, unattributed=[])),
                                 {"kind": "session identity lost"})
