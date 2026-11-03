from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast, final, override
from unittest import mock

from scripts.production.rename_test_support import FolderMoveBackend, FolderMoveFixture
from scripts.production.cli_fixture import invoke_cli
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.message import sessions as runtime_sessions
import unit_move

import pane_restore  # first: it puts the herdr scripts on the path
import herdr  # noqa: E402
import pane_record  # noqa: E402
import pane_launch
import sessions  # noqa: E402

END_HOOK = Path(__file__).resolve().parent.parent / "hooks/session-end-pane-record.py"
SETTINGS = '{"disableAgentView": true}'
EXACT_RUNTIME_ID = staff.runtime_id
NATIVE_SESSION_READER = herdr.Herdr.native_session


@final
class FakeClient(herdr.Herdr):
    """The herdr calls a restore makes, on a layout the test sets."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        with mock.patch.object(herdr, "herdr_binary", return_value="fixture-herdr"):
            super().__init__("agents")
        self.rows = rows
        self.started: list[tuple[str, str, list[str]]] = []
        self.closed: list[str] = []
        self.start_error: herdr.HerdrFailure | None = None
        self.native_ids: dict[str, str] = {}
        self.prompts: list[tuple[str, herdr.OpeningPrompt]] = []
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.publish_claude: bool = True

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        del deadline
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}
        if arguments[:2] == ("pane", "list"):
            return {"panes": [{"tab_id": str(row["pane_id"]) + ":tab", **row} for row in self.rows]}
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "label": row["label"], "pane_count": 1} for row in self.rows]}
        if arguments[:2] == ("pane", "process-info"):
            return {"process_info": {"foreground_processes": [
                {"argv": ["claude", "-n", "alpha", "--model", "opus"], "pid": 4242, "cwd": "/work/alpha"}]}}
        raise AssertionError(arguments)

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        _ = pane
        return herdr.PaneAtShell()

    @override
    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
        _ = (kind, opening, timeout_ms)
        if not any(row["pane_id"] == pane for row in self.rows):
            self.rows.append({"pane_id": pane, "label": name, "workspace_id": "w1"})
        if self.start_error is not None:
            raise self.start_error
        self.started.append((name, pane, agent_arguments))
        if "--resume" in agent_arguments:
            self.native_ids[pane] = agent_arguments[agent_arguments.index("--resume") + 1]
            if self.publish_claude:
                entries = pane_record.read(self.session)
                original = next(entries[cast(str, row["pane_id"])] for row in self.rows
                                if row["pane_id"] in entries)
                environment = {**original["environment"], "CLAUDE_CONFIG_DIR": original["config_dir"]}
                replacement = pane_record.launch(pane, self.native_ids[pane], original["cwd"],
                    environment, ["claude", *agent_arguments])
                pane_record.write(self.session, replacement)
        return herdr.PromptSubmissionNotRequired()

    @override
    def native_session(self, pane: str, kind: str, timeout: float) -> str:
        del kind, timeout
        if pane not in self.native_ids:
            raise herdr.HerdrFailure(f"{pane}: fixture has no native process observation")
        return self.native_ids[pane]

    @override
    def submit_opening_prompt(self, pane: str, opening: herdr.OpeningPrompt, launched_at: datetime,
                              timeout_ms: int) -> herdr.PromptSubmitted:
        del timeout_ms
        self.prompts.append((pane, opening))
        return herdr.PromptSubmitted(launched_at)

    @override
    def close_tab_of(self, pane: str) -> None:
        self.closed.append(pane)
        self.rows[:] = [row for row in self.rows if row["pane_id"] != pane]


@final
class PaneRestoreTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.live: list[str] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / ".claude3"
        (self.config / "projects/work").mkdir(parents=True)
        _ = (self.config / "projects/work/019b3a40-4250-7000-8000-000000000102.jsonl").write_text("{}\n")
        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        for patcher in (mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
                        mock.patch.object(pane_record, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(pane_record, "PROC", self.root / "proc"),
                        mock.patch.dict(os.environ, {"HOME": str(self.root),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "herdr-panes")}),
                        mock.patch.object(sessions, "live_sessions", self.live_sessions),
                        mock.patch.object(sessions, "running_sessions", list),
                        mock.patch.object(herdr, "open_tab", self.open_tab)):
            _ = patcher.start()
            self.addCleanup(patcher.stop)

    def live_sessions(self) -> list[dict[str, str]]:
        return [{"sessionId": session_id} for session_id in self.live]

    def open_tab(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
        self.opened.append((name, space, title, cwd, environment))
        return "w1:p3"

    def entry(self, **changes: object) -> None:
        launch = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", "/work/alpha",
                                    {"CLAUDE_CONFIG_DIR": str(self.config), "SHOWRUNNER_UNIT": "demo",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "secret"},
                                    ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "opus"])
        pane_record.write("agents", cast(pane_record.PaneLaunch, cast(object, {**launch, **changes})))

    def run_restore(self, client: FakeClient) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(herdr, "Herdr", return_value=client), redirect_stdout(output), redirect_stderr(output):
            code = pane_restore.restore("agents", dry_run=False)
        return code, output.getvalue()

    @staticmethod
    def restored(session_id: str = "019b3a40-4250-7000-8000-000000000102") -> FakeClient:
        return FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1",
                            "agent_session": {"kind": "id", "value": session_id}}])

    def test_a_restored_pane_resumes_on_its_own_account_and_its_restored_tab_closes(self) -> None:
        self.entry()
        client = self.restored()
        code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        (name, space, title, cwd, environment), = self.opened
        self.assertEqual((name, space, title, cwd), ("agents", "demo", "alpha", "/work/alpha"))
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.config))
        self.assertNotIn("STAFF_ID", environment)
        self.assertEqual(environment["SHOWRUNNER_UNIT"], "demo")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        self.assertEqual(client.started, [("alpha", "w1:p3", [
            "--resume", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha", "--remote-control", "alpha", "--model", "opus",
            "--settings", SETTINGS])])
        self.assertEqual(client.closed, ["w1:p2"])

    def test_restore_uses_the_renamed_row_for_a_fixed_reference_and_current_session(self) -> None:
        from scripts.production import session_staff

        fixed = staff.StaffId("019b3a40-4250-7000-8000-000000000102")
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        parent = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000101"), "showrunner", "demo", None,
                                current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000101"))
        owner = staff.StaffRow(fixed, "unit", "row-name", parent.id, current_session=current)
        decoy = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000104"), "unit", "alpha", parent.id,
                               current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000104"))
        record = staff.StaffSnapshot((parent, owner, decoy))
        self.entry()
        _ = (self.config / "projects/work" / f"{current}.jsonl").write_text("{}\n")
        with (mock.patch.object(staff, "initial_snapshot", return_value=record),
              mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ()))):
            client = self.restored(current)
            code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opened[0][2], "row-name")
        arguments = client.started[0][2]
        self.assertEqual(arguments[arguments.index("--resume") + 1], current)
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(client.closed, ["w1:p2"])

    def test_restore_resumes_current_session_from_a_fixed_owner_record_without_rewriting_it(self) -> None:
        from scripts.production import staff
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              mock.patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=parent.id, runtime_id="019b3a40-4250-7000-8000-000000000102")
            current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
            _ = staff.update_current_session(owner.current_session, current)
            _ = (self.config / "projects/work" / f"{current}.jsonl").write_text("{}\n")
            self.entry()
            before = path.read_bytes()
            client = self.restored(current)
            code, output = self.run_restore(client)
            self.assertEqual(code, 0, output)
            self.assertEqual(len(client.started), 1)
            arguments = client.started[0][2]
            self.assertEqual(arguments[arguments.index("--resume") + 1], current)
            self.assertNotIn(owner.id, arguments)
            self.assertEqual(client.native_ids["w1:p3"], current)
            self.assertEqual(client.closed, ["w1:p2"])
            self.assertEqual(path.read_bytes(), before)
            replacement, = pane_record.read("agents").values()
            self.assertEqual((replacement["pane_id"], replacement["session_id"], replacement.get("kind"),
                              replacement["config_dir"], replacement["cwd"]),
                             ("w1:p3", owner.id, "claude", str(self.config), "/work/alpha"))

    def test_stale_shutdown_state_does_not_prevent_restoring_a_stopped_pane(self) -> None:
        state = self.root / "shutdown/account/sessions"
        state.mkdir(parents=True)
        _ = (state / "019b3a40-4250-7000-8000-000000000102.json").write_text('{"session":{"session_id":"019b3a40-4250-7000-8000-000000000102"}}')
        self.entry()
        with mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(state.parents[1])}):
            code, output = self.run_restore(self.restored())
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.opened), 1)
        self.assertIn("resumed 019b3a40-4250-7000-8000-000000000102", output)

    def test_restore_supplies_the_staff_owners_session_labels_to_the_new_tab(self) -> None:
        from scripts.production import staff, session_staff, showrunners

        parent = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000101"), "showrunner", "demo", None, current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000101"))
        owner = staff.StaffRow(staff.StaffId("019b3a40-4250-7000-8000-000000000102"), "unit", "alpha", parent.id, current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"))
        document = "# Production — demo\n\n## Units\n\n| Unit | Plan |\n| --- | --- |\n| `alpha-unit` | standby |\n"
        source = showrunners.ShowrunnerSource(
            showrunners.Timer("demo", "session:runner", self.root / "demo.md", "UTC"), document)
        self.entry(environment={"STAFF_ID": owner.id})
        membership = session_staff.LaunchMembership(staff.StaffSnapshot((parent, owner)),
            session_staff.MemberSources({"demo": source}, ()))
        with (mock.patch.object(staff, "initial_snapshot", return_value=membership.record),
              mock.patch.object(session_staff, "member_sources", return_value=membership.sources)):
            code, output = self.run_restore(self.restored())
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.opened), 1)
        environment = self.opened[0][4]
        self.assertEqual({key: environment.get(key) for key in herdr.MEMBER_MARKS}, {
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.config))
        self.assertNotIn("STAFF_ID", environment)


    def test_a_failed_start_closes_the_new_tab_keeps_the_restored_one_and_exits_1(self) -> None:
        self.entry()
        client = self.restored()
        client.start_error = herdr.HerdrFailure("agent not ready")
        code, output = self.run_restore(client)
        self.assertEqual(code, 1)
        self.assertIn("not resumed: agent not ready", output)
        self.assertEqual(client.closed, ["w1:p3"])


    def test_restore_keeps_the_original_tab_when_it_gains_other_work(self) -> None:
        for other_conversation in (False, True):
            with self.subTest(other_conversation=other_conversation):
                self.opened.clear()
                self.entry()
                client = self.restored()
                activity = herdr.PaneRunning(("other work",)) if not other_conversation else herdr.PaneAtShell()

                def fresh_activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
                    self.assertEqual(pane, "w1:p2")
                    if not client.started:
                        return herdr.PaneAtShell()
                    return activity

                original_start = client.start_agent

                def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                          opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                          timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                    result = original_start(name, pane, kind, agent_arguments, opening, timeout_ms)
                    if other_conversation:
                        client.rows[0]["agent_session"] = {"kind": "id", "value": "another-conversation"}
                    return result

                with (mock.patch.object(client, "pane_activity", side_effect=fresh_activity),
                      mock.patch.object(client, "start_agent", side_effect=start)):
                    code, output = self.run_restore(client)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(client.started), 1)
                self.assertEqual(client.closed, [])
                self.assertIn("pane close anomaly:", output)
                self.assertEqual(output.count("pane close anomaly:"), 1)
                self.assertIn("w1:p2", pane_record.read("agents"))

    def test_an_old_launch_without_kind_restores_as_claude_without_rewriting_the_record(self) -> None:
        self.entry()
        path = pane_record.record_path("agents")
        entries = cast(dict[str, dict[str, object]], json.loads(path.read_text()))
        _ = entries["w1:p2"].pop("kind", None)
        _ = path.write_text(json.dumps(entries))
        staff_path = self.root / "legacy-staff.json"
        _ = staff_path.write_text('{"schema": 2, "rows": []}\n')
        from scripts.production.staff_test_support import isolated_staff_file
        _ = self.enterContext(isolated_staff_file(staff_path, {}))
        before = staff_path.read_bytes()
        client = self.restored()

        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p3", "claude"))
            self.assertGreater(timeout, 0)
            self.assertEqual(client.closed, [])
            return "019b3a40-4250-7000-8000-000000000102"

        with mock.patch.object(client, "native_session", side_effect=observe):
            code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        self.assertEqual(client.started, [("alpha", "w1:p3", [
            "--resume", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha", "--remote-control", "alpha", "--model", "opus",
            "--settings", SETTINGS])])
        self.assertEqual(self.opened[0][4]["CLAUDE_CONFIG_DIR"], str(self.config))
        self.assertEqual(client.closed, ["w1:p2"])
        self.assertEqual(staff_path.read_bytes(), before)
        replacement, = pane_record.read("agents").values()
        self.assertEqual((replacement["pane_id"], replacement["session_id"], replacement.get("kind"),
                          replacement["config_dir"], replacement["cwd"]),
                         ("w1:p3", "019b3a40-4250-7000-8000-000000000102", "claude",
                          str(self.config), "/work/alpha"))


    def test_seed_writes_the_entry_of_each_running_claude(self) -> None:
        (self.root / "proc/4242").mkdir(parents=True)
        _ = (self.root / "proc/4242/environ").write_bytes(
            f"CLAUDE_CONFIG_DIR={self.config}\0SHOWRUNNER_UNIT=demo\0CLAUDE_CODE_OAUTH_TOKEN=secret\0".encode())
        (self.config / "sessions").mkdir()
        _ = (self.config / "sessions/4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "019b3a40-4250-7000-8000-000000000102"}))
        client = FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1"}])
        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(sessions, "process_start", return_value="fixture-generation"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(pane_record.seed("agents"), 0)
        entry = pane_record.read("agents")["w1:p2"]
        self.assertEqual((entry["session_id"], entry["config_dir"], entry["name"], entry["model"], entry["cwd"]),
                         ("019b3a40-4250-7000-8000-000000000102", str(self.config), "alpha", "opus", "/work/alpha"))
        self.assertEqual(entry["environment"], {"SHOWRUNNER_UNIT": "demo"})

    def test_the_end_hook_marks_only_an_end_by_hand_of_the_same_conversation(self) -> None:
        self.entry()
        environment = {"PATH": "", "HOME": str(self.root), "HERDR_ENV": "1", "HERDR_SESSION": "agents",
                       "HERDR_PANE_ID": "w1:p2", "HERDR_PANE_RECORD_DIR": str(self.root / "herdr-panes")}
        for session_id, reason, ended in (("019b3a40-4250-7000-8000-000000000102", "other", False), ("sid-old", "prompt_input_exit", False),
                                          ("019b3a40-4250-7000-8000-000000000102", "prompt_input_exit", True)):
            ran = subprocess.run([sys.executable, str(END_HOOK)], input=json.dumps(
                {"session_id": session_id, "reason": reason}), env=environment, capture_output=True, text=True,
                check=False, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            entry = pane_record.read("agents")["w1:p2"]
            assert "ended" in entry
            self.assertEqual(entry["ended"], ended, (session_id, reason))


@final
class RestoreStaffNamesTests(StaffTestCase):
    def saved_batch(self) -> tuple[Path, FakeClient]:
        parent, unit, worker = self.tree()
        unit = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("unit-after-clear")))
        unit = staff.rename(unit.id, "renamed-unit")
        worker = staff.update_current_session(worker.current_session, staff.native_id(fixture_runtime_id("worker-after-clear")))
        _ = fixture_member("unit", name="models", parent=parent.id, runtime_id=fixture_runtime_id("decoy-unit"))
        missing = fixture_runtime_id("missing-row")
        directory = self.root / "saved-panes"
        config = self.root / "account"
        (config / "projects/work").mkdir(parents=True)
        entries: dict[str, pane_record.PaneLaunch] = {}
        rows: list[dict[str, object]] = []
        for index, (reference, current, name) in enumerate((
            (unit.id, unit.current_session, "models"),
            (worker.current_session, worker.current_session, "old-worker-label"),
            (missing, missing, "models"),
        ), start=1):
            pane = f"w1:p{index}"
            entries[pane] = pane_record.launch(pane, reference, str(self.root),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "-n", name])
            _ = (config / "projects/work" / f"{current}.jsonl").write_text("{}\n")
            rows.append({"pane_id": pane, "label": name, "workspace_id": "w1",
                         "agent_session": {"kind": "id", "value": current}})
        with mock.patch.object(pane_record, "RECORD_DIR", directory):
            pane_record.save("agents", entries)
        return directory, FakeClient(rows)

    def test_mac_restore_reads_the_owner_once_and_never_selects_a_row_by_saved_name(self) -> None:
        directory, client = self.saved_batch()
        from scripts.production import session_staff

        output = io.StringIO()
        with (self.machine("mac"), mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(sessions, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ())),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=True), 0)
        commands = self.ssh_commands()
        self.assertEqual(len(commands), 1)
        self.assertIn("--snapshot", commands[0])
        self.assertEqual(output.getvalue().count("no staff row"), 1)
        self.assertIn(fixture_runtime_id("missing-row"), output.getvalue())
        self.assertEqual(output.getvalue().count(": resume "), 3)
        self.assertIn("w1:p1 renamed-unit: resume", output.getvalue())
        self.assertIn("w1:p2 impl1: resume", output.getvalue())
        self.assertIn("w1:p3 models: resume", output.getvalue())
        self.assertEqual(client.started, [])

    def test_unreadable_mac_owner_keeps_each_saved_name_and_reports_one_problem(self) -> None:
        directory, client = self.saved_batch()
        output = io.StringIO()
        before = (directory / "agents.json").read_bytes()
        with (self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}),
              mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(sessions, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=True), 0)
        self.assertEqual(len(self.ssh_commands()), 1)
        self.assertEqual(output.getvalue().count("staff is unavailable"), 1)
        self.assertNotIn("no staff row", output.getvalue())
        self.assertEqual((directory / "agents.json").read_bytes(), before)
        self.assertEqual(client.started, [])

    def test_mac_restore_launches_and_orders_several_tabs_with_one_owner_read(self) -> None:
        from scripts.herdr.test_herdr import TitledLayoutHerdr
        from scripts.production import session_staff

        directory, saved_client = self.saved_batch()
        client = TitledLayoutHerdr()
        client.session = "agents"
        client.spaces = [("w1", "demo")]
        client.tabs = [{"tab_id": f"saved-t{index}", "pane_id": str(row["pane_id"]),
                        "workspace_id": "w1", "label": str(row["label"])}
                       for index, row in enumerate(saved_client.rows)]
        client.count = 20
        record = staff.snapshot()
        identities: dict[str, str] = {}
        started: list[str] = []
        config = self.root / "account"

        def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
            _ = kind, opening, timeout_ms
            identity = agent_arguments[agent_arguments.index("--resume") + 1]
            identities[pane] = identity
            started.append(name)
            replacement = pane_record.launch(pane, identity, str(self.root),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", *agent_arguments])
            owner = record.by_current_session(identity)
            if owner is not None:
                replacement["session_id"] = owner.id
            entries = pane_record.read("agents")
            entries[pane] = replacement
            pane_record.save("agents", entries)
            return herdr.PromptSubmissionNotRequired()

        def native(pane: str, kind: str, timeout: float) -> str:
            _ = kind, timeout
            return identities[pane]

        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        output = io.StringIO()
        with (self.machine("mac"), mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(directory)}),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "herdr_processes", return_value=[]),
              mock.patch.object(client, "order_tabs", wraps=client.order_tabs) as order,
              mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
              mock.patch.object(client, "start_agent", side_effect=start),
              mock.patch.object(client, "native_session", side_effect=native),
              mock.patch.object(sessions, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ())),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0, output.getvalue())
            self.assertEqual(len(pane_record.read("agents")), 3)
        self.assertEqual(len(self.ssh_commands()), 1)
        self.assertEqual(order.call_count, 3)
        self.assertEqual(started, ["renamed-unit", "impl1", "models"])
        self.assertEqual(sorted(title for _, titles in client.layout() for title in titles),
                         ["impl1", "models", "renamed-unit"])


class CodexPaneRestoreTests(FolderMoveFixture):

    @contextmanager
    def replaced_codex_process(self, backend: FolderMoveBackend, *,
                               rollout: Literal["current", "unreadable"] = "current") -> Generator[None]:
        """Keep the old environment identity while the open rollout proves the current thread."""
        proc = self.root / "replaced-codex-proc"
        process = proc / str(backend.pid)
        descriptors = process / "fd"
        descriptors.mkdir(parents=True)
        transcript = backend.config / "sessions" / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session}}) + "\n")
        (descriptors / "7").symlink_to(transcript)
        _ = (process / "cmdline").write_bytes(b"codex\0resume\0")
        _ = (process / "environ").write_bytes(
            f"CODEX_THREAD_ID={self.owner.id}\0CODEX_HOME={backend.config}\0".encode())
        if rollout == "unreadable":
            (descriptors / "7").unlink()
            descriptors.rmdir()
            _ = descriptors.write_text("descriptor observation is unavailable\n")

        def environment(pid: int) -> dict[str, str]:
            self.assertEqual(pid, backend.pid)
            return {"CODEX_HOME": str(backend.config), "CODEX_THREAD_ID":
                    self.owner.current_session if backend.starts else self.owner.id}

        publish = backend.publish_process_state

        def publish_process_state() -> None:
            publish()
            _ = (process / "cmdline").write_bytes(b"codex\0resume\0" if backend.live else b"")
            if rollout == "current":
                descriptor = descriptors / "7"
                descriptor.unlink(missing_ok=True)
                if backend.live:
                    descriptor.symlink_to(transcript)

        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              mock.patch.object(backend, "publish_process_state", side_effect=publish_process_state),
              mock.patch.object(backend, "environment", side_effect=environment),
              mock.patch.object(herdr, "process_environment", side_effect=environment)):
            yield

    @contextmanager
    def retirement_evidence(self, backend: FolderMoveBackend, client: FakeClient, *,
                            current_pane: str = "w1:p3", stale_agent: bool = False,
                            shared_tab: bool = False, stopped: bool = False,
                            rollout: bool = True, exported_claude_id: bool = True) -> Generator[None]:
        """Supply fresh process evidence for a replacement and its old shell."""
        for row in client.rows:
            pane = cast(str, row["pane_id"])
            row.update(tab_id=pane.replace(":p", ":t"), terminal_id=pane + "-terminal")
        if shared_tab:
            client.rows.append({"pane_id": "w1:p9", "tab_id": "w1:t1", "terminal_id": "other-terminal",
                                "workspace_id": "w1", "label": "unrelated"})
        proc = self.root / "retirement-proc"
        descriptors = proc / str(backend.pid) / "fd"
        descriptors.mkdir(parents=True, exist_ok=True)
        _ = (descriptors.parent / "cmdline").write_bytes((backend.kind if not stopped else "zsh").encode() + b"\0")
        shell_task = proc / str(backend.shell_pid) / "task" / str(backend.shell_pid)
        shell_task.mkdir(parents=True, exist_ok=True)
        _ = (shell_task / "children").write_text("")
        _ = (shell_task.parent.parent / "cmdline").write_bytes(b"zsh\0")
        shell_fields = ["S", "1", str(backend.shell_pid), str(backend.shell_pid), "0", str(backend.shell_pid),
                        *(["0"] * 13), "100"]
        _ = (shell_task.parent.parent / "stat").write_text(
            f"{backend.shell_pid} (zsh) {' '.join(shell_fields)}\n")
        transcript = backend.config / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session}}) + "\n")
        if rollout:
            (descriptors / "7").symlink_to(transcript)
        registry = runtime_sessions.sessions_dir() / f"{backend.pid}.json"
        _ = registry.write_text(json.dumps({"pid": backend.pid, "procStart": "retirement-start",
            "sessionId": self.owner.current_session, "name": "kept-name", "updatedAt": 1,
            "messagingSocketPath": str(self.root / "registry.sock")}))
        original_result = client.result
        original_run = herdr.run_program

        def result(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] != ("pane", "process-info"):
                return original_result(*arguments, deadline=deadline)
            pane = arguments[-1]
            kind = backend.kind if pane == current_pane and not stopped else (
                "claude" if stale_agent and pane == "w1:p1" else "zsh")
            pid = backend.pid if pane == current_pane else backend.pid + 1
            return {"process_info": {"shell_pid": backend.shell_pid,
                "foreground_processes": [{"pid": pid, "argv": [kind], "cwd": str(self.new_folder)}]}}

        def activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
            if (pane == current_pane and not stopped) or (pane == "w1:p1" and stale_agent):
                return herdr.PaneRunning((backend.kind,))
            return herdr.PaneAtShell()

        def environment(pid: int) -> dict[str, str]:
            values = {"CODEX_THREAD_ID": self.owner.id, "CODEX_HOME": str(backend.config)}
            if exported_claude_id:
                values["CLAUDE_CODE_SESSION_ID"] = self.owner.current_session if pid == backend.pid else self.owner.id
            return values

        def alive(pid: int, signal: int) -> None:
            self.assertEqual((pid, signal), (backend.pid, 0))

        def native_session(pane: str, kind: str, timeout: float) -> str:
            return NATIVE_SESSION_READER(client, pane, kind, timeout)

        def run(argv: list[str], timeout: float = 10, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[:5] == [client.binary, "--session", "agents", "pane", "process-info"]:
                payload = client.result(*argv[3:])
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": payload}), "")
            return original_run(argv, timeout, environment=environment)

        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(client, "result", side_effect=result),
              mock.patch.object(client, "pane_activity", side_effect=activity),
              mock.patch.object(client, "native_session", side_effect=native_session),
              mock.patch.object(client, "startup_processes", return_value=herdr.PaneStartupObservation(
                  herdr.PaneShell(backend.shell_pid), herdr.PaneAtShell())),
              mock.patch.object(herdr, "process_environment", side_effect=environment),
              mock.patch.object(herdr, "run_program", side_effect=run),
              mock.patch.object(staff, "runtime_id", side_effect=EXACT_RUNTIME_ID),
              mock.patch.object(os, "kill", side_effect=alive),
              mock.patch.object(runtime_sessions, "process_start", return_value="retirement-start"),
              mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)})):
            yield

    def finished_pair(self, kind: Literal["claude", "codex"]) -> tuple[FolderMoveBackend, FakeClient]:
        backend = self.folder_backend(kind, live=False)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        original = pane_record.read("agents")["w1:p1"]
        original["ended"] = False
        pane_record.write("agents", original)
        replacement = original.copy()
        replacement["pane_id"] = "w1:p3"
        pane_record.write("agents", replacement)
        client = FakeClient([{"pane_id": pane, "workspace_id": "w1", "label": "kept-name"}
                             for pane in ("w1:p1", "w1:p3")])
        path = pane_record.record_path("agents")
        attempt = pane_launch.resume_attempt(path, "w1:p3")
        proof = pane_launch.VerifiedResume(replacement, frozenset({self.owner.id, self.owner.current_session}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "forced close failure"):
            pane_launch.finish_restore(path, original, attempt, proof,
                close=mock.Mock(side_effect=herdr.HerdrFailure("forced close failure")))
        return backend, client

    def finished_move_rerun(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)


    def test_restore_repair_proves_claude_from_its_exact_registry_without_an_exported_id(self) -> None:
        backend, client = self.finished_pair("claude")
        with (self.retirement_evidence(backend, client, exported_claude_id=False),
              mock.patch.object(sessions, "live_sessions", return_value=[{"sessionId": self.owner.current_session}]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertEqual(client.started, [])
        entries = [entry for entry in pane_record.read("agents").values() if entry["session_id"] == self.owner.id]
        self.assertEqual([entry["pane_id"] for entry in entries], ["w1:p3"])


    def test_restore_liveness_uses_the_replaced_codex_thread_running_outside_herdr(self) -> None:
        backend = self.folder_backend("codex", live=True)
        client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1",
                              "agent_session": {"kind": "id", "value": self.owner.current_session}}])
        output = io.StringIO()
        with (self.replaced_codex_process(backend),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(sessions, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(herdr.running_codex_threads(thread_host=lambda _argv: False),
                             {self.owner.current_session})
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0, output.getvalue())
        self.assertIn("it already runs", output.getvalue())
        self.assertEqual(client.started, [])
        self.assertEqual(client.closed, [])


    def test_restore_rerun_repairs_a_closed_tab_when_retirement_cannot_be_saved(self) -> None:
        backend, _transcript = self.moved_codex()
        save = pane_launch.save

        def refuse_retirement(path: Path, entries: dict[str, pane_record.PaneLaunch]) -> None:
            if "w1:p1" not in entries:
                raise OSError("forced retirement write failure")
            save(path, entries)

        with self.codex_restore(backend) as (client, _proof):
            with (mock.patch.object(pane_launch, "save", side_effect=refuse_retirement),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(pane_restore.restore("agents", dry_run=False), 1)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertIn("w1:p1", pane_record.read("agents"))
        with (self.retirement_evidence(backend, client),
              mock.patch.object(sessions, "live_sessions", return_value=[{"sessionId": self.owner.current_session}]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(len(client.started), 1)
        self.assertNotIn("w1:p1", pane_record.read("agents"))

    def test_restore_rerun_closes_the_extra_shell_after_a_failed_close(self) -> None:
        backend, _transcript = self.moved_codex()
        with self.codex_restore(backend) as (client, _proof):
            with (mock.patch.object(client, "close_tab_of", side_effect=herdr.HerdrFailure("forced close failure")),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(pane_restore.restore("agents", dry_run=False), 1)
        self.assertIn("w1:p1", pane_record.read("agents"))
        with (self.retirement_evidence(backend, client),
              mock.patch.object(sessions, "live_sessions", return_value=[{"sessionId": self.owner.current_session}]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertEqual(len(client.started), 1)
        self.assertNotIn("w1:p1", pane_record.read("agents"))

    def test_missing_pane_recovery_retires_its_source_before_the_next_folder_move(self) -> None:
        from scripts.shutdown import stop_resume

        backend = self.folder_backend("claude", live=False)
        original = pane_record.read("agents")["w1:p1"]
        original["ended"] = False
        pane_record.write("agents", original)
        client = FakeClient([])

        def open_tab(_space: str, name: str, cwd: str, _environment: dict[str, str]) -> str:
            self.assertEqual(cwd, str(self.old_folder))
            client.rows.append({"pane_id": "w1:p3", "workspace_id": "w1", "label": name})
            return "w1:p3"

        def start(tab: herdr.TabSession, entry: pane_record.PaneLaunch,
                  _account: str, _message: str) -> runtime_sessions.LiveSessionProcess:
            replacement = pane_record.launch(tab.pane_id, self.owner.current_session, entry["cwd"],
                {"CLAUDE_CONFIG_DIR": entry["config_dir"]}, ["claude", "--resume", self.owner.current_session])
            pane_record.write("agents", replacement)
            return runtime_sessions.LiveSessionProcess(backend.pid)

        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(client, "open_tab", side_effect=open_tab),
              mock.patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(self.owner.current_session)),
              mock.patch.object(stop_resume, "_start_session", side_effect=start),
              mock.patch.object(stop_resume, "_detach"),
              mock.patch.object(sessions, "process_start", return_value="fixture-start"),
              mock.patch.object(herdr, "heal_claude_trust", return_value=herdr.Trusted(
                  self.old_folder, backend.config / ".claude.json", self.old_folder))):
            result = stop_resume.resume(self.owner.id, why="fixture recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        with (mock.patch.object(backend, "panes", return_value=[])):
            moved = self.finished_move_rerun()
        self.assertEqual(moved.returncode, 0, moved.stderr)
        owners = [entry for entry in pane_record.read("agents").values() if entry["session_id"] == self.owner.id]
        self.assertEqual([(entry["pane_id"], pane_record.resume_launch(entry)["cwd"]) for entry in owners], [("w1:p3", str(self.new_folder))])


    def test_restart_replacement_retires_its_old_record_before_a_folder_move(self) -> None:
        from scripts.shutdown import restart
        from scripts.shutdown.test_restart import common_session
        from scripts.shutdown.session_types import TopLevelSession, UnknownHost
        from datetime import timezone

        backend = self.folder_backend("claude", live=False)
        client = FakeClient([{"pane_id": "w1:p1", "workspace_id": "w1", "label": "kept-name",
                              "terminal_id": "kept-terminal"}])
        owner = TopLevelSession(**common_session(self.owner.current_session, "kept-name", cwd=str(self.old_folder)),
                                kind="top-level", host=UnknownHost(kind="unknown"))
        owner["config_dir"] = str(backend.config)
        parts = restart.ResumeParts(str(self.old_folder), {}, ("--resume", self.owner.current_session),
            "Continue.", restart.RetainResumeAccount(backend.config), restart.ResumeConversation(self.owner.current_session))

        def launch(_name: str, _destination: herdr.NewTab, _prepared: herdr.PreparedAgentEnvironment,
                   kind: str, agent_arguments: list[str], opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                   timeout_ms: int, *, agent: str) -> herdr.AgentLaunched:
            _ = (opening, timeout_ms, agent)
            self.assertEqual((kind, agent_arguments), ("claude", ["--resume", self.owner.current_session]))
            replacement = pane_record.launch("w1:p3", self.owner.current_session, str(self.old_folder),
                {"CLAUDE_CONFIG_DIR": str(backend.config)}, ["claude", *agent_arguments])
            pane_record.write("agents", replacement)
            client.rows.append({"pane_id": "w1:p3", "workspace_id": "w1", "label": "kept-name"})
            client.native_ids["w1:p3"] = self.owner.current_session
            return herdr.AgentLaunched("w1:p3", herdr.PromptSubmitted(datetime.now(timezone.utc)))

        def close(pane: str) -> None:
            FakeClient.close_tab_of(client, pane)

        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(client, "agents", return_value=[]),
              mock.patch.object(client, "close_pane", side_effect=close),
              mock.patch.object(restart, "_copy_claude_trust"),
              mock.patch.object(restart, "_prepare_resume_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.old_folder, {})),
              mock.patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
              mock.patch.object(herdr, "server_environment", return_value={}),
              mock.patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              mock.patch.object(restart, "_record_scheduled_restart_prompt"),
              mock.patch.object(restart, "_present_restored_pane"), redirect_stdout(io.StringIO())):
            launch_recorded = cast(Callable[..., restart.HerdrLaunch], getattr(restart, "_herdr_launch"))
            _ = launch_recorded(parts, restart.RecordedHerdrPane("agents", "w1:p1"), owner, dry_run=False)
        with (mock.patch.object(backend, "panes", return_value=[])):
            result = self.finished_move_rerun()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("w1:p1", pane_record.read("agents"))
        self.assertEqual(pane_record.resume_launch(pane_record.read("agents")["w1:p3"])["cwd"], str(self.new_folder))


    def moved_codex(self) -> tuple[FolderMoveBackend, Path]:
        backend = self.folder_backend("codex", live=True)
        entry = pane_record.launch("w1:p1", self.owner.current_session, str(self.old_folder),
            {"CODEX_HOME": str(backend.config)}, ["codex", "--model", "fixture-model"], kind="codex")
        entry["name"] = "kept-name"
        pane_record.write("agents", entry)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                           dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertEqual(pane_record.resume_launch(pane_record.read("agents")["w1:p1"])["cwd"], str(self.new_folder))
        backend.live = False
        backend.publish_process_state()
        transcript = backend.config / "sessions/2026/11/02" / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session, "cwd": str(self.old_folder)}}) + "\n")
        return backend, transcript

    @contextmanager
    def codex_restore(self, backend: FolderMoveBackend, *, wrong_identity: bool = False,
                      reused_pid: bool = False, publish: bool = True) -> Generator[tuple[FakeClient, list[str]]]:
        client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1",
                              "agent_session": {"kind": "id", "value": self.owner.current_session}}])
        proc = self.root / "restore-proc"
        proc.mkdir(exist_ok=True)
        proof: list[str] = []
        published = False
        publishing = False
        read_rollout = herdr.codex_rollout_thread

        def open_tab(session: str, space: str, name: str, cwd: str, environment: dict[str, str]) -> str:
            client.opened.append((session, space, name, cwd, environment.copy()))
            client.rows.append({"pane_id": "w1:p3", "label": name, "workspace_id": "w1"})
            return "w1:p3"

        def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
            nonlocal published, publishing
            self.assertEqual(kind, "codex")
            self.assertEqual(agent_arguments, ["resume", self.owner.current_session, "--model", "fixture-model"])
            self.assertIsInstance(opening, herdr.NoOpeningPrompt)
            self.assertGreater(timeout_ms, 0)
            (proc / str(backend.pid) / "fd").mkdir(parents=True, exist_ok=True)
            _ = (proc / str(backend.pid) / "cmdline").write_bytes(b"codex\0")
            client.started.append((name, pane, agent_arguments.copy()))
            if publish:
                info: dict[str, object] = {"process_info": {"foreground_processes": [{
                    "pid": backend.pid, "argv": ["codex", *agent_arguments], "cwd": str(self.new_folder)}]}}
                publishing = True
                try:
                    with mock.patch.object(client, "result", return_value=info):
                        super(FakeClient, client).record_codex_start(pane, name, agent_arguments)
                finally:
                    publishing = False
                published = True
            return herdr.PromptSubmissionNotRequired()

        def process_info(arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[-4:], ["pane", "process-info", "--pane", "w1:p3"])
            self.assertGreater(timeout, 0)
            self.assertEqual(client.closed, [])
            self.assertEqual(len(client.started), 1)
            proof.append("foreground")
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": {"process_info": {
                "foreground_processes": [{"pid": backend.pid, "argv": ["codex", "resume", self.owner.current_session],
                                           "cwd": str(self.new_folder)}]}}}), "")

        def process_start(pid: int) -> str:
            self.assertEqual(pid, backend.pid)
            if client.started and not publishing:
                self.assertEqual(client.closed, [])
                proof.append("start")
            return "changed-start" if reused_pid and proof.count("start") > 1 else "restore-start"

        def rollout_process_birth(pid: int) -> str:
            self.assertEqual(pid, backend.pid)
            return "restore-start"

        def rollout_thread(pid: int) -> str:
            with mock.patch.object(herdr, "_process_birth", side_effect=rollout_process_birth):
                return read_rollout(pid)

        def close(pane: str) -> None:
            FakeClient.close_tab_of(client, pane)
            if pane == "w1:p3":
                _ = (proc / str(backend.pid) / "cmdline").write_bytes(b"")

        def environment(pid: int) -> dict[str, str]:
            values = backend.environment(pid)
            if wrong_identity and (not publish or published):
                values["CODEX_THREAD_ID"] = "019b3a40-4250-7000-8000-000000000199"
            if client.started and not publishing:
                self.assertEqual(client.closed, [])
                proof.append("environment")
            return values

        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p3", "codex"))
            return super(FakeClient, client).native_session(pane, kind, timeout)

        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "open_tab", side_effect=open_tab),
              mock.patch.object(herdr, "run_program", side_effect=process_info),
              mock.patch.object(herdr, "process_environment", side_effect=environment),
              mock.patch.object(herdr, "codex_rollout_thread", side_effect=rollout_thread),
              mock.patch.object(runtime_sessions, "process_start", side_effect=process_start),
              mock.patch.object(sessions, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(staff, "runtime_id", side_effect=AssertionError("Codex needs process evidence")),
              mock.patch.object(client, "start_agent", side_effect=start),
              mock.patch.object(client, "close_tab_of", side_effect=close),
              mock.patch.object(client, "native_session", side_effect=observe)):
            yield client, proof

    def second_move(self, backend: FolderMoveBackend) -> None:
        destination = self.root / "third-folder"
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(destination)]
        with (mock.patch.object(backend, "panes", return_value=[])):
            result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        launches = [entry for entry in pane_record.read("agents").values()
                    if entry["session_id"] in (self.owner.id, self.owner.current_session)]
        self.assertEqual(len(launches), 1)
        self.assertEqual((launches[0]["pane_id"], pane_record.resume_launch(launches[0])["cwd"]), ("w1:p3", str(destination)))
        self.assertTrue((destination / ".git").is_file())

    def test_codex_restore_publication_allows_a_second_folder_move(self) -> None:
        backend, _transcript = self.moved_codex()
        with self.codex_restore(backend, publish=True) as (client, _proof), redirect_stdout(io.StringIO()):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.second_move(backend)



    def test_claude_restore_requires_publication_and_then_allows_a_second_folder_move(self) -> None:
        backend = self.folder_backend("claude", live=False)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        entry = pane_record.read("agents")["w1:p1"]
        entry["ended"] = False
        pane_record.write("agents", entry)
        transcript = backend.config / "projects/work" / f"{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text("{}\n")
        before = pane_record.read("agents")
        for publish in (False, True):
            with self.subTest(publish=publish):
                client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1",
                    "agent_session": {"kind": "id", "value": self.owner.current_session}}])
                client.publish_claude = False

                def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                          opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                          timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                    outcome = FakeClient.start_agent(client, name, pane, kind, agent_arguments, opening, timeout_ms)
                    if publish:
                        replacement = pane_record.launch(pane, self.owner.current_session, str(self.new_folder),
                            {"CLAUDE_CONFIG_DIR": str(backend.config)}, ["claude", *agent_arguments])
                        pane_record.write("agents", replacement)
                    return outcome

                with (mock.patch.object(herdr, "Herdr", return_value=client),
                      mock.patch.object(herdr, "open_tab", return_value="w1:p3"),
                      mock.patch.object(sessions, "live_sessions", return_value=[]),
                      mock.patch.object(sessions, "running_sessions", return_value=[]),
                      mock.patch.object(client, "start_agent", side_effect=start),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    code = pane_restore.restore("agents", dry_run=False)
                if not publish:
                    self.assertEqual(code, 1)
                    self.assertNotIn("w1:p1", client.closed)
                    self.assertEqual(pane_record.read("agents"), before)
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(client.closed, ["w1:p1"])
                    self.second_move(backend)





if __name__ == "__main__":
    _ = unittest.main()
