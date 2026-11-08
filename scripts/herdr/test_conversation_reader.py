"""Conversation evidence stays distinct from launch evidence."""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, override
from unittest.mock import call, patch


from scripts.herdr import herdr
from scripts.agents import codex_mesh
from scripts.lib import unit_runs
from scripts.lib import operation_deadline
from scripts.message import codex_daemon, sessions
from scripts.production import fake_unit_host
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class ConversationReaderTests(StaffTestCase):
    """Use private process files for the shared reader."""

    proc: Path = Path()
    identity: str = ""
    launched: str = ""
    runs: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.proc = self.root / "reader-proc"
        self.proc.mkdir()
        self.identity = fixture_runtime_id("reader-current")
        self.launched = fixture_runtime_id("reader-launch")
        self.runs = self.root / "runs"
        self.runs.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(self.proc)})))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="1"))
        _ = self.enterContext(patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()))
        _ = self.enterContext(patch.object(unit_runs, "run_roots", return_value=(self.runs,)))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))

    def codex(self, *, rollout: bool = True, arguments: list[str] | None = None,
              environment: dict[str, str] | None = None, pid: int = 998071) -> None:
        fake_unit_host.process(os.environ, pid, 1, arguments or ["codex", "resume", self.launched],
                               environment or {"CODEX_THREAD_ID": self.launched}, cwd=self.root)
        descriptors = self.proc / str(pid) / "fd"
        descriptors.mkdir(exist_ok=True)
        if rollout:
            record = self.root / f"rollout-{self.identity}.jsonl"
            _ = record.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.identity}}) + "\n")
            (descriptors / "4").symlink_to(record)

    def unreadable_process(self, pid: int) -> None:
        self.codex(rollout=False, pid=pid)
        arguments = herdr._process_arguments  # pyright: ignore[reportPrivateUsage]

        def read_arguments(path: Path) -> list[str]:
            if path.name == str(pid):
                raise PermissionError("argv unreadable")
            return arguments(path)

        _ = self.enterContext(patch.object(herdr, "_process_arguments", side_effect=read_arguments))

    def test_current_identity_requires_current_evidence(self) -> None:
        for evidence in (herdr.RolloutThread(self.identity), herdr.EnvironmentThread(self.identity)):
            with self.subTest(evidence=evidence):
                self.assertEqual(herdr.NativeAgentProcess(evidence, "1", {}).current_identity, self.identity)
        launched = herdr.NativeAgentProcess(herdr.LaunchArgument(self.launched), "1", {})
        self.assertEqual(launched.identity.id, self.launched)
        with self.assertRaisesRegex(ValueError, "launch argument does not prove"):
            _ = launched.current_identity

    def test_codex_open_rollout_proves_current_instead_of_launch(self) -> None:
        self.codex()
        self.assertEqual(herdr.read_conversation(self.identity, "codex"), herdr.LiveConversation("codex", (998071,)))
        self.assertEqual(herdr.read_conversation(self.launched, "codex"), herdr.StoppedConversation())
        observed = herdr.native_process(998071, "codex")
        self.assertEqual(observed.identity, herdr.RolloutThread(self.identity))
        self.assertEqual(observed.environment["CODEX_THREAD_ID"], self.launched)

    def test_codex_resume_argument_is_unknown_without_a_rollout(self) -> None:
        self.codex(rollout=False)
        with patch.object(herdr, "_process_display_name", return_value="test-tab"):
            observed = herdr.read_conversation(self.launched, "codex")
        self.assertEqual(observed, herdr.UnknownConversation(
            f"{self.launched} is not proved live or stopped: 1 processes unknown, first: test-tab (Codex thread unknown)",
            shared_codex=True))
        self.assertEqual(herdr.native_process(998071, "codex").identity, herdr.LaunchArgument(self.launched))

    def test_live_identity_does_not_name_an_unknown_process_elsewhere(self) -> None:
        self.codex()
        self.unreadable_process(998072)
        with patch.object(herdr, "_process_display_name") as name:
            self.assertEqual(herdr.read_conversation(self.identity, "codex"),
                             herdr.LiveConversation("codex", (998071,)))
        name.assert_not_called()

    def test_missing_identity_keeps_the_unknown_process_reason(self) -> None:
        self.codex()
        self.unreadable_process(998072)
        with patch.object(herdr, "_process_display_name", return_value="test-tab") as name:
            observed = herdr.read_conversation("absent", "codex")
        name.assert_called_once_with(998072)
        self.assertEqual(observed, herdr.UnknownConversation(
            "absent is not proved live or stopped: 1 processes unknown, first: test-tab (process argv unknown)",
            shared_codex=True))

    def test_shared_snapshot_names_unknown_processes_in_note_order(self) -> None:
        self.codex(rollout=False, pid=998071)
        self.unreadable_process(998072)
        home = self.root / "account"
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"),
                                     codex_daemon.DaemonUnreadable("control socket unavailable"))

        def display_name(pid: int) -> str:
            return f"test-tab-{pid}"

        with (patch.object(codex_daemon, "read_daemons", return_value=[read]),
              patch.object(herdr, "_process_display_name", side_effect=display_name) as name):
            snapshot = herdr.codex_conversations()
        self.assertEqual(snapshot.unknown, (
            "test-tab-998072 (process argv unknown)",
            f"{home} (Codex daemon unknown: control socket unavailable)",
            "test-tab-998071 (Codex thread unknown)"))
        self.assertEqual(name.call_args_list, [call(998072), call(998071)])

    def test_daemon_clients_use_one_read_and_prove_every_loaded_thread(self) -> None:
        home = self.root / "account"
        for pid in (998071, 998072):
            self.codex(rollout=False, arguments=["codex"], environment={"CODEX_HOME": str(home)}, pid=pid)
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"), [
            codex_daemon.DaemonSession(identity, "test-client", str(self.root), "idle")
            for identity in (self.identity, self.launched)])
        with patch.object(codex_daemon, "read_daemons", return_value=[read]) as reader:
            snapshot = herdr.codex_conversations()
        reader.assert_called_once_with(os.environ)
        self.assertEqual(snapshot, herdr.CodexConversations({self.identity: (), self.launched: ()}, ()))
        self.assertEqual(herdr.read_conversation(self.identity, "codex", codex=snapshot), herdr.LiveConversation("codex", ()))
        self.assertEqual(herdr.read_conversation("absent", "codex", codex=snapshot), herdr.StoppedConversation())

    def test_unreadable_daemon_names_the_account_once_for_multiple_clients(self) -> None:
        home = self.root / "account"
        for pid in (998071, 998072):
            self.codex(rollout=False, arguments=["codex"], environment={"CODEX_HOME": str(home)}, pid=pid)
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"),
                                     codex_daemon.DaemonUnreadable("control socket unavailable"))
        with (patch.object(codex_daemon, "read_daemons", return_value=[read]),
              patch.object(herdr, "_process_display_name") as name):
            snapshot = herdr.codex_conversations()
        name.assert_not_called()
        self.assertEqual(snapshot.unknown, (f"{home} (Codex daemon unknown: control socket unavailable)",))
        observed = herdr.read_conversation(self.identity, "codex", codex=snapshot)
        self.assertIsInstance(observed, herdr.UnknownConversation)
        assert isinstance(observed, herdr.UnknownConversation)
        self.assertTrue(observed.shared_codex)
        self.assertIn(str(home), observed.reason)

    def test_readable_empty_daemon_resolves_a_client_without_an_open_rollout(self) -> None:
        home = self.root / "account"
        self.codex(rollout=False, arguments=["codex"], environment={"CODEX_HOME": str(home)})
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"), [])
        with patch.object(codex_daemon, "read_daemons", return_value=[read]):
            self.assertEqual(herdr.read_conversation(self.identity, "codex"), herdr.StoppedConversation())

    def test_readable_daemon_does_not_resolve_an_unreadable_rollout(self) -> None:
        home = self.root / "account"
        self.codex(rollout=False, arguments=["codex"], environment={"CODEX_HOME": str(home)})
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"), [])
        with (patch.object(codex_daemon, "read_daemons", return_value=[read]),
              patch.object(herdr, "codex_rollout_threads", side_effect=PermissionError("rollout unreadable")),
              patch.object(herdr, "_process_display_name", return_value="test-client")):
            self.assertEqual(herdr.codex_conversations().unknown, ("test-client (Codex thread unknown)",))

    def test_readable_daemon_does_not_resolve_a_client_with_daemon_disabled(self) -> None:
        home = self.root / "account"
        self.codex(rollout=False, arguments=["codex", "--no-daemon"], environment={"CODEX_HOME": str(home)})
        read = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "socket"), [])
        with (patch.object(codex_daemon, "read_daemons", return_value=[read]),
              patch.object(herdr, "_process_display_name", return_value="test-client")):
            self.assertEqual(herdr.codex_conversations().unknown, ("test-client (Codex thread unknown)",))

    def test_pid_update_loop_helper_needs_no_conversation_read(self) -> None:
        self.codex(rollout=False, arguments=["codex", "-c", "key=value", "app-server", "daemon", "pid-update-loop"])
        with patch.object(herdr, "native_process") as native:
            self.assertEqual(herdr.codex_conversations(), herdr.CodexConversations({}, ()))
        native.assert_not_called()

    def test_idle_app_server_with_an_empty_run_read_is_proved_stopped(self) -> None:
        run = self.runs / "test-run"
        self.codex(rollout=False, arguments=["codex", "app-server"], environment={"UNIT_RUN_DIR": str(run)})
        with patch.object(codex_mesh, "loaded_threads", return_value=set()) as loaded:
            snapshot = herdr.codex_conversations()
        loaded.assert_called_once()
        self.assertEqual(loaded.call_args.args[0], run)
        self.assertEqual(snapshot, herdr.CodexConversations({}, ()))

    def test_idle_app_server_with_a_failed_run_read_is_unknown_once(self) -> None:
        run = self.runs / "test-run"
        self.codex(rollout=False, arguments=["codex", "app-server"], environment={"UNIT_RUN_DIR": str(run)})
        with patch.object(codex_mesh, "loaded_threads", side_effect=ValueError("server unavailable")):
            snapshot = herdr.codex_conversations()
        self.assertEqual(snapshot.unknown, ("test-run (Codex hosted threads unknown: server unavailable)",))

    def test_idle_app_server_without_a_run_remains_unknown(self) -> None:
        self.codex(rollout=False, arguments=["codex", "app-server"])
        with patch.object(herdr, "_process_display_name", return_value="test-server"):
            snapshot = herdr.codex_conversations()
        self.assertEqual(snapshot.unknown, ("test-server (Codex thread unknown)",))

    def test_row_specific_claude_failure_is_not_shared_codex_uncertainty(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-server unknown",))
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionLivenessUnknown("record unavailable")):
            observed = herdr.read_conversation(self.identity, codex=snapshot)
        self.assertIsInstance(observed, herdr.UnknownConversation)
        assert isinstance(observed, herdr.UnknownConversation)
        self.assertFalse(observed.shared_codex)

    def test_supplied_snapshot_matches_fresh_live_unknown_and_stopped_reads(self) -> None:
        self.codex()
        for evidence in (herdr.RolloutThread(self.identity), herdr.LaunchArgument(self.launched),
                         herdr.RolloutThread("other-thread")):
            with (self.subTest(evidence=evidence),
                  patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(evidence, "1", {})),
                  patch.object(herdr, "_process_display_name", return_value="test-tab")):
                fresh = herdr.read_conversation(self.identity, "codex")
                snapshot = herdr.codex_conversations()
                with patch.object(herdr, "codex_conversations") as scan:
                    self.assertEqual(herdr.read_conversation(self.identity, "codex", codex=snapshot), fresh)
                    self.assertEqual(herdr.read_conversation(self.identity, codex=snapshot), fresh)
                scan.assert_not_called()
                if isinstance(evidence, herdr.LaunchArgument):
                    self.assertIsInstance(fresh, herdr.UnknownConversation)
                elif evidence.id == self.identity:
                    self.assertEqual(fresh, herdr.LiveConversation("codex", (998071,)))
                else:
                    self.assertEqual(fresh, herdr.StoppedConversation())

    def test_claude_read_ignores_codex_snapshot_and_never_scans(self) -> None:
        with (patch.object(herdr, "codex_conversations") as scan,
              patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(998071))):
            snapshot = herdr.CodexConversations({self.identity: (998072,)}, ("test-tab (Codex thread unknown)",))
            self.assertEqual(herdr.read_conversation(self.identity, "claude", codex=snapshot), herdr.LiveConversation("claude", (998071,)))
            self.assertEqual(herdr.read_conversation(self.identity, "claude"), herdr.LiveConversation("claude", (998071,)))
        scan.assert_not_called()

    def test_running_threads_uses_a_supplied_snapshot_and_reports_each_unknown_once(self) -> None:
        snapshot = herdr.CodexConversations({self.identity: ()}, ("test-tab unknown", "test-tab unknown"))
        with (patch.object(herdr, "codex_conversations") as scan,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(herdr.running_codex_threads(thread_host=lambda _argv: False, codex=snapshot), {self.identity})
        scan.assert_not_called()
        self.assertEqual(errors.getvalue(), "anomaly: test-tab unknown; not blocking\n")

    def test_snapshot_scan_keeps_the_operation_deadline(self) -> None:
        run = self.runs / "test-run"
        run.mkdir()
        (run / codex_mesh.SERVER_FILE).touch()
        with (operation_deadline.within(deadline := time.monotonic() + 0.5),
              patch.object(codex_mesh, "loaded_threads", return_value={self.identity}) as loaded):
            snapshot = herdr.codex_conversations()
        self.assertEqual(snapshot.live, {self.identity: ()})
        self.assertLessEqual(cast(float, loaded.call_args.args[1]), deadline + 0.001)

    def test_daemon_snapshot_read_runs_inside_the_operation_deadline(self) -> None:
        observed_deadlines: list[float] = []

        def read(_environment: object) -> list[codex_daemon.DaemonRead]:
            observed_deadlines.append(time.monotonic() + operation_deadline.remaining())
            return []

        with (operation_deadline.within(deadline := time.monotonic() + 0.5),
              patch.object(codex_daemon, "read_daemons", side_effect=read)):
            _ = herdr.codex_conversations()
        self.assertEqual(len(observed_deadlines), 1)
        self.assertAlmostEqual(observed_deadlines[0], deadline, places=3)

    def test_unknown_other_process_names_the_target_first(self) -> None:
        unknown = ["morty (Codex thread unknown)\nprivate detail", "other (Codex thread unknown)", "pid 42 (Codex thread unknown)"]
        with patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, tuple(unknown))):
            observed = herdr.read_conversation(self.identity, "codex")
        self.assertIsInstance(observed, herdr.UnknownConversation)
        assert isinstance(observed, herdr.UnknownConversation)
        self.assertEqual(observed.reason, f"{self.identity} is not proved live or stopped: "
                         + "3 processes unknown, first: morty (Codex thread unknown) private detail")
        self.assertEqual(len(observed.reason.splitlines()), 1)
        with redirect_stderr(io.StringIO()) as output:
            herdr.conversation_note(observed)
        self.assertEqual(output.getvalue().count(self.identity), 1)

    def test_complete_read_proves_stopped_for_both_runtimes(self) -> None:
        for kind in ("claude", "codex", ""):
            with self.subTest(kind=kind):
                self.assertEqual(herdr.read_conversation(self.identity, kind), herdr.StoppedConversation())

    def test_claude_registry_proves_the_live_target(self) -> None:
        with patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(998071)):
            self.assertEqual(herdr.read_conversation(self.identity, "claude"), herdr.LiveConversation("claude", (998071,)))
            self.assertEqual(herdr.read_conversation(self.identity), herdr.LiveConversation("claude", (998071,)))

    def test_unknown_claude_record_never_proves_stopped(self) -> None:
        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionLivenessUnknown(
                  str(self.root / "998071.json") + ": unreadable record")),
              patch.object(herdr, "_process_display_name", return_value="test-tab")):
            observed = herdr.read_conversation(self.identity, "claude")
        self.assertIsInstance(observed, herdr.UnknownConversation)
        assert isinstance(observed, herdr.UnknownConversation)
        self.assertIn("test-tab", observed.reason)
        self.assertIn("unreadable record", observed.reason)

    def test_known_codex_live_target_survives_an_unknown_claude_record(self) -> None:
        self.codex()
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionLivenessUnknown("private record unavailable")):
            self.assertEqual(herdr.read_conversation(self.identity), herdr.LiveConversation("codex", (998071,)))

    def test_app_server_loaded_threads_prove_live_without_a_tui(self) -> None:
        run = self.runs / "test-run"
        run.mkdir()
        (run / codex_mesh.SERVER_FILE).touch()
        with patch.object(codex_mesh, "loaded_threads", return_value={self.identity}) as loaded:
            self.assertEqual(herdr.read_conversation(self.identity, "codex"), herdr.LiveConversation("codex", ()))
        loaded.assert_called_once()
        self.assertEqual(loaded.call_args.args[0], run)
        self.assertGreater(cast(float, loaded.call_args.args[1]), 0)

    def test_idle_astra_app_server_proves_its_thread_live_without_an_unknown_pid(self) -> None:
        run = unit_runs.ASTRA_RUN_ROOT / "test-review"
        run.mkdir(parents=True)
        _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": 998071}))
        self.codex(rollout=False, arguments=["codex", "app-server"],
                   environment={"CODEX_HOME": str(self.root / "account")})
        with patch.object(codex_mesh, "loaded_threads", return_value={self.identity}) as loaded:
            snapshot = herdr.codex_conversations()
        self.assertEqual(herdr.read_conversation(self.identity, "codex", codex=snapshot), herdr.LiveConversation("codex", ()))
        self.assertEqual(snapshot.unknown, ())
        loaded.assert_called_once()
        self.assertEqual(loaded.call_args.args[0], run)
        self.assertGreater(cast(float, loaded.call_args.args[1]), 0)

    def test_failed_app_server_read_is_unknown(self) -> None:
        run = self.runs / "test-run"
        run.mkdir()
        (run / codex_mesh.SERVER_FILE).touch()
        with patch.object(codex_mesh, "loaded_threads", side_effect=ValueError("loaded threads unavailable")):
            observed = herdr.read_conversation(self.identity, "codex")
        self.assertIsInstance(observed, herdr.UnknownConversation)
        assert isinstance(observed, herdr.UnknownConversation)
        self.assertIn("test-run", observed.reason)
        self.assertIn("loaded threads unavailable", observed.reason)

    def test_managed_daemon_rollout_alone_does_not_prove_live(self) -> None:
        self.codex(arguments=["codex", "app-server", "--managed-daemon"])
        self.assertEqual(herdr.read_conversation(self.identity, "codex"), herdr.StoppedConversation())

    def test_tab_name_read_failure_uses_registry_then_pid_for_both_runtimes(self) -> None:
        self.codex(rollout=False)
        record: sessions.SessionRecord = {"pid": 998071, "sessionId": self.launched, "name": "test-session",
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": []}
        for kind in ("claude", "codex"):
            for registry_fails in (False, True):
                with (self.subTest(kind=kind, registry_fails=registry_fails),
                      patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1")),
                      patch.object(herdr.Herdr, "pane_tab", side_effect=herdr.HerdrFailure("tab read failed")),
                      patch.object(sessions, "running_sessions", return_value=[record],
                          side_effect=OSError("registry read failed") if registry_fails else None),
                      patch.object(sessions, "session_liveness", return_value=sessions.SessionLivenessUnknown("998071.json: unreadable")),
                      redirect_stderr(io.StringIO()) as output):
                    observed = herdr.read_conversation(self.launched, kind)
                    self.assertIsInstance(observed, herdr.UnknownConversation)
                    assert isinstance(observed, herdr.UnknownConversation)
                    herdr.conversation_note(observed)
                expected = "pid 998071" if registry_fails else "test-session"
                self.assertIn(expected, output.getvalue())
                self.assertEqual(len(output.getvalue().splitlines()), 1)
