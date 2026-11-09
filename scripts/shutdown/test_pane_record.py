from __future__ import annotations

from scripts.shutdown import remote

import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest import mock


from scripts.shutdown import pane_record
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id, isolated_staff_file
from scripts.herdr import herdr, pane_launch
from scripts.herdr.test_herdr import native_process_commands
from scripts.message import sessions
from scripts.message import sessions as runtime_sessions
from scripts.shutdown.test_pane_restore import FakeClient
from scripts.whoami import account  # noqa: E402  Imported before a test sets the platform to the Mac: urllib checks it on import.

HOOK = Path(__file__).resolve().parent.parent / "hooks/session-start-pane-record.py"
BASH = shutil.which("bash") or "/bin/sh"
# Runs the hook below itself, as Claude runs a hook; started as `claude` with Claude's flags. The
# interpreter is named outright: a process called `claude` finds the real Claude through PATH as
# its sys.executable, and a test must never start it.
RUNNER = """\
import subprocess, sys
payload = sys.stdin.read()
sys.exit(subprocess.run([sys.argv[1], sys.argv[2]], input=payload, text=True).returncode)
"""


@final
class PaneRecordTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.records = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.records = self.root / "herdr-panes"
        _ = (self.root / "runner.py").write_text(RUNNER)
        self.env = {
            # No PATH: nothing the test starts can find a real claude.
            "PATH": "", "HOME": str(self.root),
            "HERDR_PANE_RECORD_DIR": str(self.records), "HERDR_ENV": "1", "HERDR_SESSION": "agents",
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "ENABLE_TOOL_SEARCH": "true",
            ('' + 'SHOWRUNNER_' + 'UNIT'): "demo", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret", "UNRELATED": "x",
        }

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))

    def test_pane_start_replaces_staff_home_without_a_launch_copy(self) -> None:
        for kind, variable, folder in (("claude", "CLAUDE_CONFIG_DIR", ".claude2"),
                                       ("codex", "CODEX_HOME", ".codex2")):
            owner = staff.ensure("showrunner", name="TEST-" + kind,
                                 runtime_id=fixture_runtime_id("TEST-home-" + kind))
            for home in (self.root / folder, self.root / (folder + "-moved")):
                entry = pane_record.launch("TEST-pane", owner.current_session, str(self.root),
                                           {variable: str(home)}, [kind], kind=cast(Literal["claude", "codex"], kind))
                pane_record.write("TEST-session", entry)
                saved = staff.by_id(owner.id)
                self.assertIsNotNone(saved)
                assert saved is not None
                self.assertEqual(saved.to_json().get("home"), str(home))
                self.assertNotIn("config_dir", pane_record.read("TEST-session")["TEST-pane"])

    def test_admission_completes_the_pane_home_publication(self) -> None:
        parent = staff.ensure("showrunner", name="TEST-parent", runtime_id=fixture_runtime_id("TEST-pane-parent"))
        for member_kind in ("showrunner", "unit", "worker"):
            session = staff.native_id(fixture_runtime_id("TEST-pane-" + member_kind))
            home = self.root / ("TEST-home-" + member_kind)
            entry = pane_record.launch("TEST-" + member_kind, session, str(self.root),
                {"CLAUDE_CONFIG_DIR": str(home)}, ["claude"])
            pane_record.write("TEST-admission", entry)
            self.assertIn("config_dir", pane_record.read("TEST-admission")["TEST-" + member_kind])
            if member_kind == "worker":
                row = staff.ensure_worker(session, "TEST-worker", parent.id)
            else:
                row = staff.ensure(cast(staff.Kind, member_kind), name="TEST-" + member_kind,
                    parent=staff.ReportsTo(parent.id), runtime_id=session)
            self.assertIsInstance(row.home, staff.HomeUnknown)
            row = pane_launch.staff_admitted(row)
            self.assertEqual(row.to_json().get("home"), str(home))
            retained = pane_record.read("TEST-admission")["TEST-" + member_kind]
            self.assertNotIn("config_dir", retained)
            self.assertEqual(pane_launch.config_home(retained), staff.SessionHome(home))

    def test_retained_write_keeps_the_running_home_until_a_pane_start(self) -> None:
        owner = staff.ensure("showrunner", name="TEST-write-order", runtime_id=fixture_runtime_id("TEST-write-order"))
        running_home, target_home = self.root / ".claude2", self.root / ".claude3"
        entry = pane_record.launch("TEST-write-order", owner.current_session, str(self.root),
                                   {"CLAUDE_CONFIG_DIR": str(running_home)}, ["claude"])
        pane_record.write("TEST-write-order", entry)
        entry["config_dir"] = str(target_home)
        pane_record.write("TEST-write-order", entry)
        current = staff.by_id(owner.id)
        assert current is not None
        self.assertEqual(current.to_json().get("home"), str(running_home))
        self.assertNotIn("config_dir", pane_record.read("TEST-write-order")["TEST-write-order"])

    def start(self, pane: str, session_id: str, *flags: str, source: str = "startup") -> subprocess.CompletedProcess[str]:
        """The hook under a process whose argv is `claude <flags>`, as a pane's Claude runs it."""
        command = [BASH, "-c", 'exec -a claude "$0" "$@"', sys.executable, str(self.root / "runner.py"),
                   sys.executable, str(HOOK), *flags]
        payload = json.dumps({"session_id": session_id, "cwd": "/work/alpha", "source": source})
        return subprocess.run(command, input=payload, env={**self.env, "HERDR_PANE_ID": pane},
                              capture_output=True, text=True, check=False, timeout=60)

    def entries(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], json.loads((self.records / "agents.json").read_text()))

    def test_plugin_directories_survive_hook_capture_and_resume(self) -> None:
        directories = ["/fixture/postmaster", "/fixture/second plugin"]
        started = self.start("w1:p2", "plugin-session", "--plugin-dir", directories[0],
                             "--plugin-dir=" + directories[1], "-n", "postmaster")
        self.assertEqual(started.returncode, 0, started.stderr)
        with mock.patch.object(pane_record, "RECORD_DIR", self.records):
            entry = pane_record.read_strict("agents")["w1:p2"]
        self.assertEqual(entry.get("plugin_directories"), directories)
        arguments = pane_record.resume_arguments(entry)
        parsed = pane_record.parse_claude_arguments(["claude", *arguments])
        self.assertEqual(parsed.plugin_directories, tuple(directories))
        self.assertFalse(parsed.has_prompt)
        self.assertEqual(arguments.count("--plugin-dir"), 2)

    def test_relative_plugin_paths_keep_their_target_after_a_folder_move(self) -> None:
        original = self.root / "work/unit"
        moved = self.root / "elsewhere/unit"
        entry = pane_record.launch("w1:p2", "plugin-session", str(original), {},
                                   ["claude", "--plugin-dir", "../shared/postmaster"])
        entry["cwd"] = str(moved)
        arguments = pane_record.resume_arguments(entry)
        self.assertEqual(arguments[arguments.index("--plugin-dir") + 1],
                         str(self.root / "work/shared/postmaster"))
        self.assertEqual(entry.get("plugin_directories"), [str(self.root / "work/shared/postmaster")])

    def test_plugin_options_after_the_terminator_are_only_prompt_text(self) -> None:
        arguments = ["claude", "--plugin-dir", "/wanted", "--", "--plugin-dir=/unwanted"]
        parsed = pane_record.parse_claude_arguments(arguments)
        self.assertTrue(parsed.has_prompt)
        self.assertEqual(parsed.plugin_directories, ("/wanted",))
        entry = pane_record.launch("w1:p2", "plugin-session", "/fixture", {}, arguments)
        self.assertEqual(entry.get("plugin_directories"), ["/wanted"])
        self.assertNotIn("/unwanted", pane_record.resume_arguments(entry))

    def test_codex_resume_keeps_the_recorded_policy_after_the_model(self) -> None:
        policy = ["--sandbox", "read-only", "--ask-for-approval", "never",
                  "-c", 'model_reasoning_effort="high"',
                  "-c", "notice.hide_rate_limit_model_nudge=true",
                  "-c", 'service_tier="default"', "-c", 'notify=["/fixture/notify"]']
        for model, recorded in (("fixture-model", False), ("fixture-model", True), ("", True)):
            with self.subTest(model=model, recorded=recorded):
                entry = pane_record.launch("w1:p2", "codex-thread", str(self.root), {}, ["codex"], kind="codex")
                entry["model"] = model
                if recorded:
                    entry["codex_policy"] = policy.copy()
                else:
                    _ = entry.pop("codex_policy", None)
                expected = ["resume", "codex-thread"] + (["--model", model] if model else [])
                self.assertEqual(pane_record.resume_arguments(entry), expected + (policy if recorded else []))
                if recorded:
                    self.assertEqual(entry.get("codex_policy", []), policy)

    def test_plugin_directories_require_nonempty_paths_in_launch_records(self) -> None:
        entry = pane_record.launch("w1:p2", "plugin-session", "/fixture", {}, ["claude"])
        self.records.mkdir()
        for invalid in ("/not-a-list", [""], [1]):
            with self.subTest(invalid=invalid):
                document: dict[str, object] = {**entry, "plugin_directories": invalid}
                _ = (self.records / "agents.json").write_text(json.dumps({"w1:p2": document}))
                with mock.patch.object(pane_record, "RECORD_DIR", self.records):
                    with self.assertRaisesRegex(ValueError, "plugin_directories"):
                        _ = pane_record.read_strict("agents")

    def test_moved_session_uses_newest_launch_across_accounts_and_record_files(self) -> None:
        old = pane_record.launch("old-pane", "moved", "/old", {"CLAUDE_CONFIG_DIR": "/accounts/.claude4"},
                                 ["claude", "-n", "old", "--model", "opus"])
        old["written_at"] = "2026-10-31T01:00:00+00:00"
        newest = pane_record.launch("new-pane", "moved", "/new", {"CLAUDE_CONFIG_DIR": "/accounts/.claude2"},
                                    ["claude", "-n", "new", "--model", "sonnet"])
        newest["written_at"] = "2026-10-31T02:00:00+00:00"
        newest["ended"] = True
        with mock.patch.object(pane_record, "RECORD_DIR", self.records):
            pane_record.write("first", newest)
            pane_record.write("last", old)
            self.assertEqual(pane_record.launch_for_session("moved"), newest)
            self.assertIsNone(pane_record.launch_for_session("absent"))

    def test_seed_process_info_reads_only_selected_panes_and_defaults_to_all(self) -> None:
        client = FakeClient([{"pane_id": pane, "workspace_id": "w1"} for pane in ("w1:p1", "w1:p2", "w2:p1")])
        original = client.result
        def result(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            return {"process_info": {"foreground_processes": []}} if arguments[:2] == ("pane", "process-info") else original(*arguments, deadline=deadline)
        info = mock.Mock(side_effect=result)
        with mock.patch.object(herdr, "Herdr", return_value=client), mock.patch.object(client, "result", info):
            _ = pane_record.seed("agents", select=lambda pane: pane["pane_id"] == "w1:p2")
            self.assertEqual([invocation for invocation in info.call_args_list if invocation.args[:2] == ("pane", "process-info")],
                             [mock.call("pane", "process-info", "--pane", "w1:p2")])
            info.reset_mock()
            _ = pane_record.seed("agents")
            self.assertEqual([invocation for invocation in info.call_args_list if invocation.args[:2] == ("pane", "process-info")],
                             [mock.call("pane", "process-info", "--pane", pane) for pane in ("w1:p1", "w1:p2", "w2:p1")])

    def seed_foreground(self, kind: Literal["claude", "codex"], reference: str,
                        rows: tuple[staff.StaffRow, ...] = ()) -> str:
        process = self.root / "proc/4242"
        process.mkdir(parents=True, exist_ok=True)
        config = self.root / ".claude3"
        (config / "sessions").mkdir(parents=True, exist_ok=True)
        _ = (config / "sessions/4242.json").write_text(json.dumps({"sessionId": "claude-thread"}))
        _ = (process / "environ").write_bytes(
            f"CODEX_HOME={self.root / '.codex2'}\0CLAUDE_CONFIG_DIR={config}\0UNRELATED=secret\0".encode())
        client = FakeClient([{"pane_id": "w2C:p1", "label": None, "workspace_id": "w2C",
                              "session_id": fixture_runtime_id("pane-record-codex") if kind == "codex" and rows else ""}])
        arguments = ["codex"] if kind == "codex" else ["claude", "-n", "claude-name", "--model", "opus"]
        client.process_info = {"foreground_processes": [{"pid": 4242, "argv": arguments, "cwd": str(self.root)}]}
        write_text_atomic(process / "cmdline", "\0".join(arguments))
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"HOME": str(self.root), "HERDR_PROC": str(process.parent),
                                          "HERDR_PLATFORM": "linux"}),
              mock.patch.object(pane_record, "PROC", process.parent),
              mock.patch.object(pane_record, "RECORD_DIR", self.records),
              native_process_commands(4242),
              mock.patch.object(sessions, "process_start", return_value="fixture-start"),
              mock.patch.object(runtime_sessions, "process_start", return_value="fixture-start"),
              mock.patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w2C:p1")),
              mock.patch.object(client, "pane_tab", return_value=("morty", 1, {"name": reference})),
              redirect_stdout(output)):
            if kind == "codex" and rows:
                self.assertEqual(client.codex_launch("w2C:p1")["session_id"], fixture_runtime_id("pane-record-codex"))
            self.assertEqual(pane_record.seed("agents"), 0)
        return output.getvalue()

    def test_seed_records_a_plain_codex_client_from_its_own_process(self) -> None:
        owner = staff.StaffRow(staff.native_id("019b3a40-4250-7000-8000-000000000101"), "agent", "morty",
                              staff.StaffRoot(), current_session=staff.native_id("019b3a40-4250-7000-8000-000000000102"))
        for reference in (owner.name, owner.id):
            with self.subTest(reference=reference):
                output = self.seed_foreground("codex", reference, (owner,))
                entry = self.entries()["w2C:p1"]
                self.assertEqual(entry["kind"], "codex")
                identity = fixture_runtime_id("pane-record-codex")
                self.assertEqual(entry["session_id"], identity)
                self.assertNotEqual(entry["session_id"], owner.current_session)
                with (mock.patch.object(pane_record, "RECORD_DIR", self.records),
                      mock.patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,)))):
                    retained = pane_record.read("agents")["w2C:p1"]
                    self.assertEqual(pane_record.resume_arguments(pane_record.resume_launch(retained)),
                                     ["resume", identity])
                self.assertEqual(entry["config_dir"], str(self.root / ".codex2"))
                self.assertEqual((entry["pid"], entry["proc_start"]), (4242, "fixture-start"))
                self.assertEqual(entry["environment"], {})
                self.assertIn(identity, output)

    def test_seed_keeps_the_claude_registry_and_launch_flags(self) -> None:
        output = self.seed_foreground("claude", "unrelated-tab")
        entry = self.entries()["w2C:p1"]
        self.assertEqual((entry["kind"], entry["session_id"], entry["config_dir"], entry["name"], entry["model"]),
                         ("claude", "claude-thread", str(self.root / ".claude3"), "claude-name", "opus"))
        self.assertEqual(entry["environment"], {})
        self.assertIn("claude-thread", output)

    def test_seed_skips_a_codex_client_without_process_identity_with_one_line(self) -> None:
        self.assertEqual(self.seed_foreground("codex", "unknown"), "w2C:p1: no conversation id for 4242\n")
        self.assertFalse((self.records / "agents.json").exists())

    def test_shared_parent_reader_reads_linux_comm_with_parentheses(self) -> None:
        process = self.root / "proc/42"
        process.mkdir(parents=True)
        _ = (process / "stat").write_text("42 (shell (fixture)) S 17 0 0")
        with mock.patch.dict(os.environ, {"HERDR_PROC": str(process.parent), "HERDR_PLATFORM": "linux"}):
            self.assertEqual(pane_record.process_parent(42), 17)
            with self.assertRaises(OSError):
                _ = pane_record.process_parent(99)

    def test_shared_parent_reader_propagates_mac_errors_and_hook_keeps_its_empty_answer(self) -> None:
        failure = subprocess.CalledProcessError(1, ["ps"])
        with (mock.patch.dict(os.environ, {"HERDR_PLATFORM": "darwin", "HERDR_PS": "/fixture/ps"}),
              mock.patch.object(subprocess, "run", side_effect=failure) as parent):
            with self.assertRaises(subprocess.CalledProcessError):
                _ = pane_record.process_parent(42)
            self.assertEqual(pane_record.claude_arguments(42), [])
            self.assertEqual(parent.call_args_list, [mock.call(["/fixture/ps", "-o", "ppid=", "-p", "42"],
                capture_output=True, text=True, check=True, timeout=5)] * 2)

    def test_the_hook_records_the_pane_launch_from_its_own_claude(self) -> None:
        started = self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha", "--remote-control", "alpha",
                             "--model", "opus", "--settings", "{}", "resume checkpoint")
        self.assertEqual(started.returncode, 0, started.stderr)
        entry = self.entries()["w1:p2"]
        self.assertEqual({key: value for key, value in entry.items() if key != "written_at"}, {
            "pane_id": "w1:p2", "session_id": "019b3a40-4250-7000-8000-000000000102", "config_dir": str(self.root / ".claude3"),
            "cwd": "/work/alpha", "name": "alpha", "model": "opus", "remote_control": True,
            "environment": {"ENABLE_TOOL_SEARCH": "true"},
            "flags_read": True, "ended": False, "kind": "claude"})

    def test_hook_preserves_staff_identity_for_restore_and_membership(self) -> None:
        self.env["STAFF_ID"] = "019b3a40-4250-7000-8000-000000000102"
        result = self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries()["w1:p2"]
        environment = cast(dict[str, str], entry["environment"])
        self.assertNotIn("STAFF_ID", environment)
        self.assertEqual(entry["session_id"], "019b3a40-4250-7000-8000-000000000102")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

    def test_a_new_start_replaces_its_own_pane_and_keeps_the_others(self) -> None:
        for pane, session_id in (("w1:p2", "019b3a40-4250-7000-8000-000000000102"), ("w2:p1", "sid-ups"), ("w1:p2", "sid-alpha-2")):
            self.assertEqual(self.start(pane, session_id).returncode, 0)
        entries = self.entries()
        self.assertEqual({pane: entry["session_id"] for pane, entry in entries.items()},
                         {"w1:p2": "sid-alpha-2", "w2:p1": "sid-ups"})
        self.assertEqual((entries["w2:p1"]["name"], entries["w2:p1"]["remote_control"]), ("", False))

    def test_explicit_record_directory_keeps_identity_sources_separate(self) -> None:
        source = self.root / "staff-source"
        source.mkdir()
        entry = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", "/fixture", {
            ('' + 'SHOWRUNNER_' + 'UNIT'): "demo", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha",
            "STAFF_ID": "019b3a40-4250-7000-8000-000000000102"}, ["claude"])
        evidence = source / "agents.json"
        _ = evidence.write_text(json.dumps({"w1:p2": entry}))
        before = evidence.read_bytes()
        identity = staff.StaffId(entry["session_id"])
        owner = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity)
        with (mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(source)}),
              mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,))),
              mock.patch.object(pane_record, "RECORD_DIR", self.records)):
            pane_record.write("agents", entry)
            expected = entry.copy()
            expected["environment"] = {}
            _ = expected.pop("config_dir", None)
            self.assertEqual(pane_record.read("agents"), {"w1:p2": expected})
            self.assertEqual(pane_record.forget_unit(identity), ["agents:w1:p2"])
            self.assertEqual(pane_record.read("agents"), {})
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(json.loads((self.records / "agents.json").read_text()), {})

    def test_unmarked_write_does_not_recover_a_previous_launchs_staff_identity(self) -> None:
        marked = pane_record.launch("old", "conversation", "/fixture", {
            "STAFF_ID": "019b3a40-4250-7000-8000-000000000102", "SHOWRUNNER_UNIT": "demo",
            "SHOWRUNNER_UNIT_ID": "alpha"}, ["claude"])
        with mock.patch.object(pane_record, "RECORD_DIR", self.records):
            pane_record.write("agents", marked)
            for pane in ("new", "old"):
                with self.subTest(pane=pane):
                    unmarked = pane_record.launch(pane, "conversation", "/fixture", {}, ["claude"])
                    pane_record.write("agents", unmarked)
                    self.assertEqual(pane_record.read("agents")[pane], unmarked)

    def test_markless_hook_preserves_only_the_same_conversations_identity(self) -> None:
        self.env["STAFF_ID"] = "019b3a40-4250-7000-8000-000000000102"
        self.assertEqual(self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102").returncode, 0)
        for key in ("STAFF_ID", ('' + 'SHOWRUNNER_' + 'UNIT'), ('' + 'SHOWRUNNER_' + 'UNIT_ID')):
            del self.env[key]
        self.assertEqual(self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102").returncode, 0)
        environment = cast(dict[str, str], self.entries()["w1:p2"]["environment"])
        self.assertEqual(environment, {"ENABLE_TOOL_SEARCH": "true"})
        self.assertEqual(self.start("w1:p2", "different-conversation").returncode, 0)
        self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_resume_projection_uses_current_session_without_rewriting_retained_evidence(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              mock.patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=staff.ReportsTo(parent.id), runtime_id="019b3a40-4250-7000-8000-000000000102")
            current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
            _ = staff.update_current_session(owner.current_session, current)
            retained = pane_record.launch("w1:p2", owner.id, "/work/alpha", {}, ["claude"])
            with mock.patch.object(pane_record, "RECORD_DIR", self.records):
                pane_record.write("agents", retained)
                record_path = self.records / "agents.json"
                before = path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns
                projected = pane_record.resume_launch(retained)
                self.assertEqual(projected, {**retained, "session_id": current})
                self.assertEqual(retained["session_id"], owner.id)
                self.assertEqual(pane_record.resume_launch(projected), projected)
                unknown = pane_record.launch("w1:p4", "019b3a40-4250-7000-8000-000000000104", "/other", {}, ["claude"])
                self.assertEqual(pane_record.resume_launch(unknown), unknown)
                self.assertEqual((path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns), before)

    def test_writers_remove_a_legacy_staff_id_and_keep_account_evidence(self) -> None:
        entry = pane_record.launch("w1:p2", "saved-session", "/work", {}, ["claude", "-n", "saved"])
        entry["environment"] = {"STAFF_ID": "parent", "ENABLE_TOOL_SEARCH": "true"}
        with (mock.patch.object(pane_record, "RECORD_DIR", self.records),
              mock.patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(()))):
            pane_record.write("agents", entry)
            self.assertEqual(pane_record.read("agents")["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})
            pane_record.save("agents", {"w1:p2": entry})
            self.assertEqual(pane_record.read("agents")["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})
            self.assertEqual(pane_launch.config_home(pane_record.read("agents")["w1:p2"]), pane_launch.config_home(entry))
        self.assertEqual(entry["environment"]["STAFF_ID"], "parent")

    def test_worker_resume_uses_the_typed_parent_for_its_folder(self) -> None:
        from scripts.shutdown import launch_names, session_types

        location = staff.PresentWorktree(remote.local_machine(), self.root / "unit")
        unit = staff.StaffRow(staff.StaffId("unit"), "unit", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId("unit-session"),
                             worktree=location)
        for parent in (staff.ReportsTo(unit.id), staff.StaffRoot()):
            with self.subTest(parent=parent):
                worker = staff.StaffRow(staff.StaffId("worker"), "worker", "impl", parent,
                                       current_session=staff.StaffId("worker-session"))
                record = staff.StaffSnapshot((unit, worker))
                retained = pane_record.launch("w1:p2", worker.id, "/saved", {}, ["claude"])
                expected = str(location.path) if isinstance(parent, staff.ReportsTo) else "/saved"
                with (mock.patch.object(staff, "initial_snapshot", return_value=record),
                      mock.patch.object(staff, "snapshot", return_value=record)):
                    self.assertEqual(pane_record.resume_launch(retained)["cwd"], expected)
                    self.assertEqual(launch_names.LaunchNames(record).resume(retained)["cwd"], expected)
                    self.assertEqual(session_types.unit_worktree(worker.id),
                                     location if isinstance(parent, staff.ReportsTo) else staff.WORKTREE_UNFILLED)

    def current_owner(self) -> tuple[staff.StaffRow, staff.StaffId, Path]:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        _ = self.enterContext(isolated_staff_file(path, {}))
        environment = cast(object, self.enterContext(mock.patch.dict(os.environ, source_environment(self.root / "sources"))))
        del environment
        _ = self.enterContext(isolated_source_readers())
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.records))
        parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
        owner = staff.ensure("unit", name="alpha", parent=staff.ReportsTo(parent.id), runtime_id="019b3a40-4250-7000-8000-000000000102")
        current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
        _ = staff.update_current_session(owner.current_session, current)
        return owner, current, path

    def test_recording_current_conversation_persists_fixed_owner_without_mutating_launch_observation(self) -> None:
        owner, current, path = self.current_owner()
        observed = pane_record.launch("w1:p2", current, "/work/alpha",
            {"CLAUDE_CODE_SESSION_ID": current}, ["claude", "--resume", current])
        original = observed.copy()
        before = path.read_bytes(), path.stat().st_mtime_ns

        pane_record.write("agents", observed)

        self.assertEqual(pane_record.read("agents")["w1:p2"], {**observed, "session_id": owner.id})
        self.assertEqual(observed, original)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        record_path = self.records / "agents.json"
        retained = record_path.read_bytes(), record_path.stat().st_mtime_ns
        pane_record.write("agents", observed)
        self.assertEqual((record_path.read_bytes(), record_path.stat().st_mtime_ns), retained)




    def test_current_conversation_lookup_projects_only_its_exact_retained_pane_without_writing(self) -> None:
        owner, current, path = self.current_owner()
        retained = pane_record.launch("w1:p2", owner.id, "/work/alpha", {}, ["claude"])
        unrelated = pane_record.launch("w1:p3", "019b3a40-4250-7000-8000-000000000104", "/work/other", {}, ["claude"])
        pane_record.write("agents", retained)
        pane_record.write("agents", unrelated)
        record_path = self.records / "agents.json"
        before = path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns

        self.assertEqual(pane_record.launch_for_session(current), retained)
        self.assertEqual(pane_record.matching_launch(herdr.HerdrPlace("agents", "w1:p2"), current),
                         {**retained, "session_id": current})
        self.assertIsNone(pane_record.matching_launch(herdr.HerdrPlace("agents", "w1:p3"), current))
        self.assertIsNone(pane_record.launch_for_session("019b3a40-4250-7000-8000-000000000105"))
        self.assertEqual((path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns), before)

    def test_current_conversation_exit_ends_only_its_fixed_owner_record_and_stale_exit_writes_nothing(self) -> None:
        owner, current, path = self.current_owner()
        retained = pane_record.launch("w1:p2", owner.id, "/work/alpha", {}, ["claude"])
        unrelated = pane_record.launch("w1:p3", "019b3a40-4250-7000-8000-000000000104", "/work/other", {}, ["claude"])
        pane_record.write("agents", retained)
        pane_record.write("agents", unrelated)
        record_path = self.records / "agents.json"
        staff_before = path.read_bytes(), path.stat().st_mtime_ns
        before = record_path.read_bytes(), record_path.stat().st_mtime_ns

        pane_record.end("agents", "w1:p2", owner.id)
        pane_record.end("agents", "w1:p3", current)
        self.assertEqual((record_path.read_bytes(), record_path.stat().st_mtime_ns), before)
        pane_record.end("agents", "w1:p2", current)
        self.assertEqual(pane_record.read("agents"), {"w1:p2": {**retained, "ended": True}, "w1:p3": unrelated})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), staff_before)

    def test_repair_recovers_history_and_staff_identity_without_changing_conversations(self) -> None:
        self.assert_repair_recovers_history(legacy=False)

    def test_repair_recovers_identity_from_a_three_field_launch_without_name_or_cwd(self) -> None:
        self.assert_repair_recovers_history(legacy=True)

    def assert_repair_recovers_history(self, *, legacy: bool) -> None:
        from scripts.production import staff, session_staff
        from scripts.shutdown import pane_record
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        from scripts.message import sessions

        environment = {"HERDR_PANE_RECORD_DIR": str(self.records)}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
            old = pane_record.launch("old", owner.id, "/old", {}, ["claude"])
            old["environment"] = {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha"}
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            new = pane_record.launch("new", owner.id, "/new", {"ENABLE_TOOL_SEARCH": "true"}, ["claude"])
            retained: dict[str, object] = {**new, "environment": {
                **new["environment"], "CLAUDE_CODE_SESSION_ID": owner.current_session}}
            if legacy:
                retained = {key: value for key, value in retained.items()
                            if key in {"session_id", "written_at", "environment"}}
            # Store legacy labels to test their removal.
            self.records.mkdir(parents=True, exist_ok=True)
            path = self.records / "agents.json"
            write_text_atomic(path, json.dumps({"old": old, "new": retained}))
            with mock.patch.object(sessions, "running_sessions", return_value=[]):
                self.assertEqual(len(session_staff.repair_marks()), 1)
                repaired = pane_record.read_strict("agents")
                expected = {**retained, "environment": new["environment"]}
                self.assertEqual(repaired["new"], expected)
                self.assertEqual(repaired["old"], {**old, "environment": {}})
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                self.assertEqual(session_staff.repair_marks(), [])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_repair_command_reports_each_record_then_is_idle_on_rerun(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, source_environment, unit_membership

        environment = {"PATH": os.defpath, **source_environment(self.root / "sources"),
                       "HERDR_PANE_RECORD_DIR": str(self.records), "HOME": str(self.root), "HERDR_SESSION": "agents"}
        with isolated_staff_file(self.root / "staff.json", environment):
            owner = unit_membership("demo", "alpha")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        old = pane_record.launch("old", owner.id, "/fixture", {}, ["claude"])
        old["written_at"] = "2026-10-30T00:00:00+00:00"
        new = pane_record.launch("new", owner.id, "/fixture", {}, ["claude"])
        new["environment"] = {"CLAUDE_CODE_SESSION_ID": owner.current_session}
        self.records.mkdir(parents=True)
        path = self.records / "agents.json"
        write_text_atomic(path, json.dumps({"old": old, "new": new}))
        command = [sys.executable, "-m", "scripts.production.session_staff", "repair"]
        first = subprocess.run(command, cwd=HOOK.parents[2], env=environment,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.splitlines(), [
            f"agents:new:{owner.id}: removed CLAUDE_CODE_SESSION_ID"])
        self.assertEqual(self.entries()["new"], {**new, "environment": {}})
        before = path.read_bytes(), path.stat().st_mtime_ns
        second = subprocess.run(command, cwd=HOOK.parents[2], env=environment,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "nothing to repair\n")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_unmarked_compaction_keeps_repaired_labels_only_for_the_same_conversation(self) -> None:
        from scripts.production import staff, session_staff
        from scripts.shutdown import pane_record
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        from scripts.message import sessions

        environment = {"HERDR_PANE_RECORD_DIR": str(self.records)}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
            old = pane_record.launch("old", owner.id, "/work/alpha", {}, ["claude"])
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            damaged = pane_record.launch("w1:p2", owner.id, "/work/alpha", {}, ["claude"])
            damaged["environment"] = {"CLAUDE_CODE_SESSION_ID": owner.current_session}
            self.records.mkdir(parents=True)
            write_text_atomic(self.records / "agents.json", json.dumps({"old": old, "w1:p2": damaged}))
            with mock.patch.object(sessions, "running_sessions", return_value=[]):
                self.assertEqual(len(session_staff.repair_marks()), 1)
            self.env["STAFF_FILE"] = str(self.root / "staff.json")
            for key in (('' + 'SHOWRUNNER_' + 'UNIT'), ('' + 'SHOWRUNNER_' + 'UNIT_ID')):
                del self.env[key]
            for _compaction in range(2):
                hook = self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102", source="compact")
                self.assertEqual(hook.returncode, 0, hook.stderr)
                self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})
                self.assertEqual(self.entries()["w1:p2"]["session_id"], owner.id)
            hook = self.start("w1:p2", "different-conversation")
            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_codex_publication_and_label_repair_keep_kind_without_claude_settings(self) -> None:
        from scripts.production import session_staff
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership

        environment = {**self.env, "CODEX_HOME": str(self.root / ".codex2")}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha")
            launch = pane_record.launch("w1:p2", owner.current_session, str(self.root), environment,
                                       ["codex"], kind="codex")
            history = launch.copy()
            history["environment"] = {
                "CODEX_HOME": environment["CODEX_HOME"], "CODEX_THREAD_ID": owner.current_session,
                "STAFF_ID": owner.id,
            }
            self.records.mkdir(parents=True)
            path = self.records / "agents.json"
            write_text_atomic(path, json.dumps({"w1:p2": history}))
            with mock.patch.object(sessions, "running_sessions", return_value=[]):
                pane_record.write("agents", session_staff.record_membership(launch))
                published = pane_record.read_strict("agents")["w1:p2"]
                self.assertEqual(published.get("kind"), "codex")
                self.assertNotIn("config_dir", published)
                self.assertEqual(pane_launch.config_home(published), staff.SessionHome(Path(environment["CODEX_HOME"])))
                self.assertEqual(published["session_id"], owner.id)
                self.assertEqual(published["environment"], {
                    "CODEX_HOME": environment["CODEX_HOME"], "ENABLE_TOOL_SEARCH": "true",
                })
                write_text_atomic(path, json.dumps({"w1:p2": {**published, "environment": history["environment"]}}))
                repaired = session_staff.repair_marks()
            self.assertEqual(len(repaired), 1)
            self.assertEqual(pane_record.read_strict("agents")["w1:p2"], {
                **published, "environment": {
                    "CODEX_HOME": environment["CODEX_HOME"],
                },
            })

    def test_restored_unit_id_comes_from_the_staff_row(self) -> None:
        from scripts.production.session_labels import StaffLabels

        parent = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000101"), "showrunner", "demo", staff.StaffRoot(), current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000101"))
        owner = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000102"), "unit", "alpha", staff.ReportsTo(parent.id), current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        entry = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/fixture", {"CLAUDE_CODE_SESSION_ID": owner.current_session}, ["claude"])
        labels = StaffLabels(staff.StaffSnapshot((parent, owner)))
        self.assertEqual(labels.environment(entry["environment"], owner.current_session), {"CLAUDE_CODE_SESSION_ID": owner.current_session})

    def test_staff_owner_supersedes_a_promoted_showrunners_old_session_staff(self) -> None:
        from scripts.production.session_labels import StaffLabels

        owner = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000102"), "showrunner", "demo", staff.StaffRoot(), current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        entry = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/fixture", {
            ('' + 'SHOWRUNNER_' + 'UNIT'): "former", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "demo"}, ["claude"])
        labels = StaffLabels(staff.StaffSnapshot((owner,)))
        repaired = labels.environment(entry["environment"], owner.current_session)
        self.assertEqual(repaired, {"CLAUDE_CODE_SESSION_ID": owner.current_session})
        entry["environment"] = {**entry["environment"], **repaired}
        self.assertEqual(labels.environment(entry["environment"], owner.current_session), repaired)
        with mock.patch.object(pane_record, "RECORD_DIR", self.records):
            pane_record.write("agents", entry)
            canonical = entry.copy()
            canonical["environment"] = repaired
            pane_record.write("agents", canonical)
            self.assertEqual(pane_record.read("agents")["new"]["environment"], repaired)

    def test_a_session_outside_herdr_writes_nothing(self) -> None:
        self.env["HERDR_ENV"] = ""
        self.assertEqual(self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102").returncode, 0)
        self.assertFalse(self.records.exists())


TOKEN = "sk-ant-oat01-do-not-keep"
# A Mac KERN_PROCARGS2 record: argc, the executable path, NUL padding, the arguments, the
# environment, then the kernel's own `key=value` strings.
MAC_RECORD = (
    (5).to_bytes(4, sys.byteorder) + b"/opt/claude/bin/claude\0\0\0\0"
    + b"claude\0--name\0alpha\0--model\0opus\0"
    + b"CLAUDE_CONFIG_DIR=/Users/n/.claude3\0CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN.encode()
    + (b'\x00' + b'SHOWRUNNER_' + b'UNIT_ID=alpha\x00ENABLE_TOOL_SEARCH=true\x00HOME=/Users/n\x00\x00')
    + b"executable_path=/opt/claude/bin/claude\0\0\0"
)


@final
class MacRecordTests(unittest.TestCase):
    def test_on_the_mac_a_launch_reads_its_flags_and_never_keeps_the_token(self) -> None:
        parents = {"200": "100\n", "100": "1\n"}

        def ps(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, parents[command[-1]], "")

        def kernel(pid: int) -> bytes:
            return MAC_RECORD if pid == 100 else (1).to_bytes(4, sys.byteorder) + b"/bin/zsh\0zsh\0\0"

        with (mock.patch.object(sys, "platform", "darwin"),
              mock.patch.object(subprocess, "run", side_effect=ps),
              mock.patch.object(account, "kern_procargs2", side_effect=kernel)):
            arguments = pane_record.claude_arguments(200)
            environment = pane_record._process_environment(100)  # pyright: ignore[reportPrivateUsage]
        entry = pane_record.launch("w1:p1", "019b3a40-4250-7000-8000-000000000102", "/work/alpha", environment, arguments)
        self.assertEqual((entry["name"], entry["model"], entry["flags_read"]), ("alpha", "opus", True))
        self.assertEqual(pane_launch.config_home(entry), staff.SessionHome(Path("/Users/n/.claude3")))
        self.assertNotIn(TOKEN, json.dumps(environment) + json.dumps(entry))


if __name__ == "__main__":
    _ = unittest.main()
