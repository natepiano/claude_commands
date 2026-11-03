from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.shutdown import pane_record
from scripts.production import staff
import herdr
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
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret", "UNRELATED": "x",
        }

    def start(self, pane: str, session_id: str, *flags: str, source: str = "startup") -> subprocess.CompletedProcess[str]:
        """The hook under a process whose argv is `claude <flags>`, as a pane's Claude runs it."""
        command = [BASH, "-c", 'exec -a claude "$0" "$@"', sys.executable, str(self.root / "runner.py"),
                   sys.executable, str(HOOK), *flags]
        payload = json.dumps({"session_id": session_id, "cwd": "/work/alpha", "source": source})
        return subprocess.run(command, input=payload, env={**self.env, "HERDR_PANE_ID": pane},
                              capture_output=True, text=True, check=False, timeout=60)

    def entries(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], json.loads((self.records / "agents.json").read_text()))

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
            "environment": {"ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                            "SHOWRUNNER_UNIT_ID": "alpha-unit"},
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
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit",
            "STAFF_ID": "019b3a40-4250-7000-8000-000000000102"}, ["claude"])
        evidence = source / "agents.json"
        _ = evidence.write_text(json.dumps({"w1:p2": entry}))
        before = evidence.read_bytes()
        with (mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(source)}),
              mock.patch.object(pane_record, "RECORD_DIR", self.records)):
            pane_record.write("agents", entry)
            self.assertEqual(pane_record.read("agents"), {"w1:p2": entry})
            self.assertEqual(pane_record.forget_unit("alpha-unit"), ["agents:w1:p2"])
            self.assertEqual(pane_record.read("agents"), {})
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(json.loads((self.records / "agents.json").read_text()), {})

    def test_unmarked_write_does_not_recover_a_previous_launchs_staff_identity(self) -> None:
        marked = pane_record.launch("old", "conversation", "/fixture", {
            "STAFF_ID": "019b3a40-4250-7000-8000-000000000102", "SHOWRUNNER_UNIT": "demo",
            "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
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
        for key in ("STAFF_ID", "SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID"):
            del self.env[key]
        self.assertEqual(self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102").returncode, 0)
        environment = cast(dict[str, str], self.entries()["w1:p2"]["environment"])
        self.assertEqual(environment, {"ENABLE_TOOL_SEARCH": "true",
                                      "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        self.assertEqual(self.start("w1:p2", "different-conversation").returncode, 0)
        self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_resume_projection_uses_current_session_without_rewriting_retained_evidence(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              mock.patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=parent.id, runtime_id="019b3a40-4250-7000-8000-000000000102")
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
            self.assertEqual(pane_record.read("agents")["w1:p2"]["config_dir"], entry["config_dir"])
        self.assertEqual(entry["environment"]["STAFF_ID"], "parent")

    def current_owner(self) -> tuple[staff.StaffRow, staff.StaffId, Path]:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        _ = self.enterContext(isolated_staff_file(path, {}))
        environment = cast(object, self.enterContext(mock.patch.dict(os.environ, source_environment(self.root / "sources"))))
        del environment
        _ = self.enterContext(isolated_source_readers())
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.records))
        parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
        owner = staff.ensure("unit", name="alpha", parent=parent.id, runtime_id="019b3a40-4250-7000-8000-000000000102")
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

    def test_folder_move_updates_only_owner_cwd_with_the_supplied_snapshot_and_reruns_without_writes(self) -> None:
        owner, current, path = self.current_owner()
        old, new = self.root / "old-folder", self.root / "new-folder"
        fixed = pane_record.launch("w1:p2", owner.id, str(old), {}, ["claude", "-n", "alpha"])
        live = pane_record.launch("w2:p1", current, str(old), {}, ["claude", "-n", "alpha"])
        other = pane_record.launch("w1:p3", "019b3a40-4250-7000-8000-000000000104", str(old), {}, ["claude"])
        fixed["ended"] = True
        pane_record.write("agents", fixed)
        pane_record.write("agents", other)
        with pane_record.locked("retained"):
            pane_record.save("retained", {"w2:p1": live})
        record = staff.snapshot()
        before = path.read_bytes()

        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("reuse the supplied snapshot")):
            pane_record.move_cwd(owner.id, old, new, record)
            self.assertEqual(pane_record.read("agents"), {"w1:p2": {**fixed, "cwd": str(new)}, "w1:p3": other})
            self.assertEqual(pane_record.read("retained"), {"w2:p1": {**live, "cwd": str(new)}})
            paths = [self.records / name for name in ("agents.json", "retained.json")]
            moved = [(entry.read_bytes(), entry.stat().st_mtime_ns) for entry in paths]
            pane_record.move_cwd(owner.id, old, new, record)
            self.assertEqual([(entry.read_bytes(), entry.stat().st_mtime_ns) for entry in paths], moved)
        self.assertEqual(path.read_bytes(), before)

    def test_folder_move_refuses_an_owner_launch_from_an_unrelated_folder(self) -> None:
        owner, _current, path = self.current_owner()
        retained = pane_record.launch("w1:p2", owner.id, str(self.root / "unrelated"), {}, ["claude"])
        pane_record.write("agents", retained)
        record_path = self.records / "agents.json"
        before = path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "different working folder"):
            pane_record.move_cwd(owner.id, self.root / "old", self.root / "new", staff.snapshot())
        self.assertEqual((path.read_bytes(), record_path.read_bytes(), record_path.stat().st_mtime_ns), before)

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
        import sessions

        environment = {"HERDR_PANE_RECORD_DIR": str(self.records)}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha-unit")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
            old = pane_record.launch("old", "019b3a40-4250-7000-8000-000000000102", "/old", {
                "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            new = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/new", {"ENABLE_TOOL_SEARCH": "true"}, ["claude"])
            retained: dict[str, object] = dict(new)
            if legacy:
                retained = {key: value for key, value in retained.items()
                            if key in {"session_id", "written_at", "environment"}}
            # Reproduce the damaged store without passing through the now-preserving writer.
            self.records.mkdir(parents=True, exist_ok=True)
            path = self.records / "agents.json"
            _ = path.write_text(json.dumps({"old": old, "new": retained}))
            membership = session_staff.LaunchMembership(staff.snapshot(), session_staff.MemberSources({}, (old, new)))
            with (mock.patch.object(session_staff.LaunchMembership, "read", return_value=membership),
                  mock.patch.object(sessions, "running_sessions", return_value=[])):
                self.assertEqual(len(session_staff.repair_marks()), 1)
                repaired = pane_record.read_strict("agents")
                expected = {**retained, "environment": {**old["environment"], **new["environment"], "CLAUDE_CODE_SESSION_ID": owner.current_session}}
                self.assertEqual(repaired["new"], expected)
                self.assertEqual(repaired["old"], old)
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                self.assertEqual(session_staff.repair_marks(), [])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_repair_command_reports_each_record_then_is_idle_on_rerun(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, source_environment, unit_membership

        environment = {"PATH": os.defpath, **source_environment(self.root / "sources"),
                       "HERDR_PANE_RECORD_DIR": str(self.records), "HOME": str(self.root), "HERDR_SESSION": "agents"}
        with isolated_staff_file(self.root / "staff.json", environment):
            owner = unit_membership("demo", "alpha-unit")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        old = pane_record.launch("old", "019b3a40-4250-7000-8000-000000000102", "/fixture", {
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
        old["written_at"] = "2026-10-30T00:00:00+00:00"
        new = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/fixture", {}, ["claude"])
        self.records.mkdir(parents=True)
        path = self.records / "agents.json"
        _ = path.write_text(json.dumps({"old": old, "new": new}))
        command = [sys.executable, "-m", "scripts.production.session_staff", "repair"]
        first = subprocess.run(command, cwd=HOOK.parents[2], env=environment,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.splitlines(), [
            f"agents:new:{owner.current_session}: set CLAUDE_CODE_SESSION_ID={owner.current_session}; "
            + "set SHOWRUNNER_UNIT=demo; set SHOWRUNNER_UNIT_ID=alpha-unit"])
        self.assertEqual(self.entries()["new"]["environment"], {**old["environment"], "CLAUDE_CODE_SESSION_ID": owner.current_session})
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
        import sessions

        environment = {"HERDR_PANE_RECORD_DIR": str(self.records)}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha-unit")
            owner = staff.update_current_session(owner.current_session, staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
            old = pane_record.launch("old", "019b3a40-4250-7000-8000-000000000102", "/work/alpha", {
                "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            damaged = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", "/work/alpha", {}, ["claude"])
            self.records.mkdir(parents=True)
            _ = (self.records / "agents.json").write_text(json.dumps({"old": old, "w1:p2": damaged}))
            membership = session_staff.LaunchMembership(staff.snapshot(), session_staff.MemberSources({}, (old, damaged)))
            with (mock.patch.object(session_staff.LaunchMembership, "read", return_value=membership),
                  mock.patch.object(sessions, "running_sessions", return_value=[])):
                self.assertEqual(len(session_staff.repair_marks()), 1)
            self.env["STAFF_FILE"] = str(self.root / "staff.json")
            for key in ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID"):
                del self.env[key]
            for _compaction in range(2):
                hook = self.start("w1:p2", "019b3a40-4250-7000-8000-000000000102", source="compact")
                self.assertEqual(hook.returncode, 0, hook.stderr)
                self.assertEqual(self.entries()["w1:p2"]["environment"], {
                    "ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                    "SHOWRUNNER_UNIT_ID": "alpha-unit", "CLAUDE_CODE_SESSION_ID": owner.current_session})
            hook = self.start("w1:p2", "different-conversation")
            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_restored_unit_id_comes_from_the_productions_units_table(self) -> None:
        from scripts.production import staff, session_staff, showrunners

        parent = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000101"), "showrunner", "demo", None, current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000101"))
        owner = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000102"), "unit", "alpha", parent.id, current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        document = "# Production — demo\n\n## Units\n\n| Unit | Plan |\n| --- | --- |\n| `alpha` | standby |\n"
        source = showrunners.ShowrunnerSource(
            showrunners.Timer("demo", "session:runner", self.root / "demo.md", "UTC"), document)
        entry = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/fixture", {"CLAUDE_CODE_SESSION_ID": owner.current_session}, ["claude"])
        membership = session_staff.LaunchMembership(staff.StaffSnapshot((parent, owner)),
            session_staff.MemberSources({"demo": source}, ()))
        self.assertEqual(membership.environment(entry), {"CLAUDE_CODE_SESSION_ID": owner.current_session,
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha"})

    def test_staff_owner_supersedes_a_promoted_showrunners_old_session_staff(self) -> None:
        from scripts.production import staff, session_staff, showrunners

        owner = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000102"), "showrunner", "demo", None, current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        source = showrunners.ShowrunnerSource(
            showrunners.Timer("demo", "session:019b3a40-4250-7000-8000-000000000102", self.root / "demo.md", "UTC"), "# demo\n")
        entry = pane_record.launch("new", "019b3a40-4250-7000-8000-000000000102", "/fixture", {
            "SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "demo-unit"}, ["claude"])
        membership = session_staff.LaunchMembership(staff.StaffSnapshot((owner,)),
            session_staff.MemberSources({"demo": source}, ()))
        repaired = membership.environment(entry)
        self.assertEqual(repaired, {"CLAUDE_CODE_SESSION_ID": owner.current_session})
        entry["environment"] = {**entry["environment"], **repaired}
        self.assertEqual(membership.environment(entry), repaired)
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
    + b"claude\0--name\0alpha-unit\0--model\0opus\0"
    + b"CLAUDE_CONFIG_DIR=/Users/n/.claude3\0CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN.encode()
    + b"\0SHOWRUNNER_UNIT_ID=alpha-unit\0ENABLE_TOOL_SEARCH=true\0HOME=/Users/n\0\0"
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
        self.assertEqual((entry["name"], entry["model"], entry["flags_read"]), ("alpha-unit", "opus", True))
        self.assertEqual(entry["config_dir"], "/Users/n/.claude3")
        self.assertNotIn(TOKEN, json.dumps(environment) + json.dumps(entry))


if __name__ == "__main__":
    _ = unittest.main()
