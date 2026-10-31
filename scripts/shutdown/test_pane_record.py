from __future__ import annotations

import json
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

import pane_record
import herdr
from test_pane_restore import FakeClient
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
        def result(*arguments: str) -> dict[str, object]:
            return {"process_info": {"foreground_processes": []}} if arguments[:2] == ("pane", "process-info") else original(*arguments)
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
        started = self.start("w1:p2", "sid-alpha", "-n", "alpha", "--remote-control", "alpha",
                             "--model", "opus", "--settings", "{}", "resume checkpoint")
        self.assertEqual(started.returncode, 0, started.stderr)
        entry = self.entries()["w1:p2"]
        self.assertEqual({key: value for key, value in entry.items() if key != "written_at"}, {
            "pane_id": "w1:p2", "session_id": "sid-alpha", "config_dir": str(self.root / ".claude3"),
            "cwd": "/work/alpha", "name": "alpha", "model": "opus", "remote_control": True,
            "environment": {"ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                            "SHOWRUNNER_UNIT_ID": "alpha-unit"},
            "flags_read": True, "ended": False})

    def test_hook_preserves_staff_identity_for_restore_and_membership(self) -> None:
        self.env["STAFF_ID"] = "s123456789ab"
        result = self.start("w1:p2", "sid-alpha", "-n", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries()["w1:p2"]
        environment = cast(dict[str, str], entry["environment"])
        self.assertEqual(environment.get("STAFF_ID"), "s123456789ab")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

    def test_a_new_start_replaces_its_own_pane_and_keeps_the_others(self) -> None:
        for pane, session_id in (("w1:p2", "sid-alpha"), ("w2:p1", "sid-ups"), ("w1:p2", "sid-alpha-2")):
            self.assertEqual(self.start(pane, session_id).returncode, 0)
        entries = self.entries()
        self.assertEqual({pane: entry["session_id"] for pane, entry in entries.items()},
                         {"w1:p2": "sid-alpha-2", "w2:p1": "sid-ups"})
        self.assertEqual((entries["w2:p1"]["name"], entries["w2:p1"]["remote_control"]), ("", False))

    def test_explicit_record_directory_keeps_identity_sources_separate(self) -> None:
        source = self.root / "staff-source"
        source.mkdir()
        entry = pane_record.launch("w1:p2", "sid-alpha", "/fixture", {
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit",
            "STAFF_ID": "s123456789ab"}, ["claude"])
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

    def test_markless_hook_preserves_only_the_same_conversations_identity(self) -> None:
        self.env["STAFF_ID"] = "s123456789ab"
        self.assertEqual(self.start("w1:p2", "sid-alpha").returncode, 0)
        for key in ("STAFF_ID", "SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID"):
            del self.env[key]
        self.assertEqual(self.start("w1:p2", "sid-alpha").returncode, 0)
        environment = cast(dict[str, str], self.entries()["w1:p2"]["environment"])
        self.assertEqual(environment, {"ENABLE_TOOL_SEARCH": "true", "STAFF_ID": "s123456789ab",
                                      "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        self.assertEqual(self.start("w1:p2", "different-conversation").returncode, 0)
        self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_repair_recovers_history_and_staff_identity_without_changing_conversations(self) -> None:
        from scripts.production import staff, session_staff
        from scripts.shutdown import pane_record
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        import sessions

        environment = {"HERDR_PANE_RECORD_DIR": str(self.records)}
        with (mock.patch.dict(os.environ, environment),
              isolated_staff_file(self.root / "staff.json", environment)):
            owner = unit_membership("demo", "alpha-unit")
            old = pane_record.launch("old", "sid-alpha", "/old", {
                "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            new = pane_record.launch("new", "sid-alpha", "/new", {"ENABLE_TOOL_SEARCH": "true"}, ["claude"])
            # Reproduce the damaged store without passing through the now-preserving writer.
            self.records.mkdir(parents=True, exist_ok=True)
            path = self.records / "agents.json"
            _ = path.write_text(json.dumps({"old": old, "new": new}))
            membership = session_staff.LaunchMembership(staff.snapshot(), session_staff.MemberSources({}, (old, new)))
            with (mock.patch.object(session_staff.LaunchMembership, "read", return_value=membership),
                  mock.patch.object(sessions, "running_sessions", return_value=[])):
                self.assertEqual(len(session_staff.repair_marks()), 1)
                repaired = pane_record.read_strict("agents")
                expected = {**new, "environment": {**old["environment"], **new["environment"], "STAFF_ID": owner.id}}
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
        old = pane_record.launch("old", "same-session", "/fixture", {
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
        old["written_at"] = "2026-10-30T00:00:00+00:00"
        new = pane_record.launch("new", "same-session", "/fixture", {}, ["claude"])
        self.records.mkdir(parents=True)
        path = self.records / "agents.json"
        _ = path.write_text(json.dumps({"old": old, "new": new}))
        command = [sys.executable, "-m", "scripts.production.session_staff", "repair"]
        first = subprocess.run(command, cwd=HOOK.parents[2], env=environment,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.splitlines(), [
            "agents:new:same-session: set SHOWRUNNER_UNIT=demo; set SHOWRUNNER_UNIT_ID=alpha-unit; "
            + f"set STAFF_ID={owner.id}"])
        self.assertEqual(self.entries()["new"]["environment"], {**old["environment"], "STAFF_ID": owner.id})
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
            old = pane_record.launch("old", "sid-alpha", "/work/alpha", {
                "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude"])
            old["written_at"] = "2026-10-30T00:00:00+00:00"
            damaged = pane_record.launch("w1:p2", "sid-alpha", "/work/alpha", {}, ["claude"])
            self.records.mkdir(parents=True)
            _ = (self.records / "agents.json").write_text(json.dumps({"old": old, "w1:p2": damaged}))
            membership = session_staff.LaunchMembership(staff.snapshot(), session_staff.MemberSources({}, (old, damaged)))
            with (mock.patch.object(session_staff.LaunchMembership, "read", return_value=membership),
                  mock.patch.object(sessions, "running_sessions", return_value=[])):
                self.assertEqual(len(session_staff.repair_marks()), 1)
            for key in ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID"):
                del self.env[key]
            for _compaction in range(2):
                hook = self.start("w1:p2", "sid-alpha", source="compact")
                self.assertEqual(hook.returncode, 0, hook.stderr)
                self.assertEqual(self.entries()["w1:p2"]["environment"], {
                    "ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                    "SHOWRUNNER_UNIT_ID": "alpha-unit", "STAFF_ID": owner.id})
            hook = self.start("w1:p2", "different-conversation")
            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(self.entries()["w1:p2"]["environment"], {"ENABLE_TOOL_SEARCH": "true"})

    def test_restored_unit_id_comes_from_the_productions_units_table(self) -> None:
        from scripts.production import staff, session_staff, showrunners

        parent = staff.StaffRow(staff.StaffId("s123456789aa"), "showrunner", "demo", None)
        owner = staff.StaffRow(staff.StaffId("s123456789ab"), "unit", "alpha", parent.id)
        document = "# Production — demo\n\n## Units\n\n| Unit | Plan |\n| --- | --- |\n| `alpha` | standby |\n"
        source = showrunners.ShowrunnerSource(
            showrunners.Timer("demo", "session:runner", self.root / "demo.md", "UTC"), document)
        entry = pane_record.launch("new", "sid-alpha", "/fixture", {"STAFF_ID": owner.id}, ["claude"])
        membership = session_staff.LaunchMembership(staff.StaffSnapshot((parent, owner)),
            session_staff.MemberSources({"demo": source}, ()))
        self.assertEqual(membership.environment(entry), {"STAFF_ID": owner.id,
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha"})

    def test_staff_owner_supersedes_a_promoted_showrunners_old_session_staff(self) -> None:
        from scripts.production import staff, session_staff, showrunners

        owner = staff.StaffRow(staff.StaffId("s123456789ab"), "showrunner", "demo", None)
        source = showrunners.ShowrunnerSource(
            showrunners.Timer("demo", "session:sid-alpha", self.root / "demo.md", "UTC"), "# demo\n")
        entry = pane_record.launch("new", "sid-alpha", "/fixture", {
            "SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "demo-unit"}, ["claude"])
        membership = session_staff.LaunchMembership(staff.StaffSnapshot((owner,)),
            session_staff.MemberSources({"demo": source}, ()))
        repaired = membership.environment(entry)
        self.assertEqual(repaired, {"STAFF_ID": owner.id})
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
        self.assertEqual(self.start("w1:p2", "sid-alpha").returncode, 0)
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
        entry = pane_record.launch("w1:p1", "sid-alpha", "/work/alpha", environment, arguments)
        self.assertEqual((entry["name"], entry["model"], entry["flags_read"]), ("alpha-unit", "opus", True))
        self.assertEqual(entry["config_dir"], "/Users/n/.claude3")
        self.assertNotIn(TOKEN, json.dumps(environment) + json.dumps(entry))


if __name__ == "__main__":
    _ = unittest.main()
