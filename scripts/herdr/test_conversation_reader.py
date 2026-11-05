"""Conversation evidence stays distinct from launch evidence."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import herdr
from scripts.agents import codex_mesh
from scripts.lib import unit_runs
from scripts.message import sessions
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
        self.assertEqual(observed, herdr.UnknownConversation("test-tab (Codex thread unknown)"))
        self.assertEqual(herdr.native_process(998071, "codex").identity, herdr.LaunchArgument(self.launched))

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
