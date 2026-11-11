#!/usr/bin/env python3
"""add_showrunner.py through its CLI, with fake herdr, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

from scripts.production.staff_test_support import ClaudeBackendFixture, StaffTestCase, fixture_member, fixture_runtime_id

import io
import json
import math
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import patch


from scripts.accounts import accounts
from scripts.production import add_showrunner
from scripts.production.production_doc import ProductionDocument, read_new_production, read_production
from scripts.production import showrunners
from scripts.whoami import usage_store
from scripts.whoami.account_test_support import stored_room
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse

from scripts.production.add_showrunner_test_support import AddShowrunnerFixture, SCRIPT
from scripts.production.add_unit_test_support import LINUX_LAUNCHER_CLI


from scripts.production import staff
from scripts.production import completion_receipt
from scripts.production.fake_unit_host import process as write_process
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import sessions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

class NativeClaudeShowrunnerTests(StaffTestCase):
    def test_resume_uses_the_row_name_after_rename_and_conversation_change(self) -> None:
        document = self.production("demo")
        _ = document.write_text(document.read_text() + "\n- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `demo-log.md`\n")
        owner = fixture_member("showrunner", name="demo", doc=staff.LiveDoc("natedev", document))
        current = staff.native_id(fixture_runtime_id("renamed-showrunner-current"))
        _ = staff.update_current_session(owner.current_session, current)
        _ = staff.rename(owner.id, "row-name")
        _ = fixture_member("showrunner", name="demo", runtime_id=fixture_runtime_id("decoy-showrunner"))
        with (patch.object(herdr, "start_server"), patch.object(herdr, "launch_agent", return_value=
              herdr.AgentLaunched("w1:p1", herdr.PromptSubmitted(datetime.now(timezone.utc)))) as launch):
            _ = add_showrunner.launch(read_production("row-name"), "fixture", "high", current,
                                      self.root, accounts.NamedAccount("claude1", self.root))
        arguments = cast(list[str], launch.call_args.kwargs["agent_arguments"])
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(launch.call_args.args[2], "row-name")
        self.assertNotIn("STAFF_ID", cast(dict[str, str], launch.call_args.args[4]))

    def test_fresh_showrunner_uses_the_observed_claude_id_before_its_opening_prompt(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text("# New production\n")
        _ = document.write_text(document.read_text() + "\n- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `demo-log.md`\n")
        child = read_new_production(document, "demo")
        self.assertIsNone(staff.initial_snapshot().by_path("demo"))
        identity = fixture_runtime_id("fresh-Claude-showrunner")
        backend = ClaudeBackendFixture(identity, self.root / "claude-account")
        with (patch.object(herdr, "start_server"), patch.object(herdr, "launch_agent", side_effect=backend.launch)):
            outcome = add_showrunner.launch(child, "fixture", "high", None, self.root,
                accounts.NamedAccount("claude1", backend.configuration))
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        row = backend.before_opening.by_id(staff.StaffId(identity))
        assert row is not None
        self.assertEqual((row.id, row.name, row.parent), (identity, "demo", staff.StaffRoot()))
        self.assertNotIn("--session-id", backend.arguments)
        self.assertNotIn("--resume", backend.arguments)
        self.assertNotIn("STAFF_ID", backend.environment)


class AddShowrunnerTests(AddShowrunnerFixture):
    def test_existing_showrunner_uses_its_staff_document_when_given_another_document(self) -> None:
        document = self.doc("kid")
        owner = fixture_member("showrunner", name="kid", doc=staff.LiveDoc("natedev", document))
        unrelated = self.doc("unrelated")
        result = self.run_command("kid", "--doc", str(unrelated), "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"would start kid in {self.root / 'kid-checkout'}", result.stdout)
        self.assertNotIn(str(self.root / "unrelated-checkout"), result.stdout)
        self.assertEqual(staff.by_id(owner.id), owner)

    def test_new_showrunner_and_missing_parent_are_admitted_with_their_live_docs(self) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        child = staff.by_path("kid")
        parent = staff.by_path("top")
        assert child is not None and parent is not None
        self.assertEqual(child.doc, staff.LiveDoc("natedev", self.doc("kid")))
        self.assertEqual(parent.doc, staff.LiveDoc("natedev", self.doc("top")))
        self.assertEqual(child.parent, staff.ReportsTo(parent.id))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(add_showrunner, "local_machine", return_value="natedev"))
        _ = self.enterContext(patch.object(herdr.Herdr, "run_in_pane"))
        _ = fixture_member("showrunner", name="top", doc=staff.LiveDoc("natedev", self.doc("top")))

    @override
    def invoke(self, arguments: list[str], *, cwd: Path | None = None,
               subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        if subprocess_cli:
            return subprocess.run([sys.executable, "-c", LINUX_LAUNCHER_CLI, str(SCRIPT), *arguments],
                                  env=self.env, capture_output=True, text=True, check=False, cwd=cwd,
                                  timeout=TEST_HANG_GUARD_SECS)
        return super().invoke(arguments, cwd=cwd)

    def test_launch_passes_the_ensured_showrunner_staff_id_to_the_pane(self) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [row for row in staff.find(kind="showrunner") if row.name == "kid"]
        self.assertEqual(len(rows), 1)
        opened = self.opened()
        environment = dict(opened[index + 1].split("=", 1)
                           for index, value in enumerate(opened) if value == "--env")
        self.assertNotIn("STAFF_ID", environment)
        pid = int(self.env["FAKE_CLAUDE_PID"])
        registry = sessions.read_session(Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{pid}.json")
        assert not isinstance(registry, sessions.UnreadableSessionRecord)
        self.assertEqual(registry["sessionId"], self.env["FAKE_CLAUDE_SESSION_ID"])
        self.assertEqual(registry.get("procStart"), sessions.process_start(pid))
        self.assertEqual((rows[0].id, rows[0].current_session), (registry["sessionId"], registry["sessionId"]))

    def test_showrunner_and_unit_add_paths_share_the_live_doc_admission_writer(self) -> None:
        from scripts.production import add_unit

        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        original = add_showrunner.ensure_staff_showrunner
        self.assertIs(add_unit.ensure_staff_showrunner, original)
        with (patch.object(add_showrunner, "ensure_staff_showrunner", wraps=original) as showrunner_admit,
              patch.object(add_unit, "ensure_staff_showrunner", wraps=original) as unit_admit):
            result = self.run_command("kid")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            child = next(row for row in staff.find(kind="showrunner") if row.name == "kid")
            self.assertGreater(showrunner_admit.call_count, 0)
            with (patch.dict(os.environ, self.env),
                  patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier")):
                unit = add_unit.ensure_staff_unit(read_production("kid"), "probe",
                    worktree=staff.PresentWorktree("natedev", self.root / "probe"),
                    runtime_id=fixture_runtime_id("probe-unit-backend"))
            unit_admit.assert_called_once()
        self.assertEqual(unit.parent, staff.ReportsTo(child.id))
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(child.doc, staff.LiveDoc("natedev", self.doc("kid")))
        self.assertEqual(len([row for row in staff.find(kind="showrunner") if row.id == child.id]), 1)

    def test_existing_staff_parent_survives_a_stale_saved_parent_during_ensure(self) -> None:
        self.timer("kid", "cfc4564d-fed3-5803-9a4c-e3119733bb53")
        parent = fixture_member("showrunner", name="other")
        child = staff.by_path("kid")
        assert child is not None
        child = staff.move(child.id, staff.ReportsTo(parent.id))
        path = self.root / "showrunner" / child.id / "parent"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, "top\n")
        before = self.staff.staff_file.read_bytes(), path.read_bytes()
        with (patch.dict(os.environ, self.env),
              patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier")):
            ensured = add_showrunner.ensure_staff_showrunner(read_production("kid"), "top")
        self.assertEqual(ensured, child)
        self.assertEqual((self.staff.staff_file.read_bytes(), path.read_bytes()), before)

    def assert_dead_launch_recovers(self, *, transcript_exists: bool) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        class NativeCallbackInterrupted(BaseException):
            """The backend was observed before this launcher process ended."""

        def interrupted(child: ProductionDocument, caller: str, *, runtime_id: str = "") -> staff.StaffRow:
            self.assertEqual((child.name, caller), ("kid", "top"))
            self.assertEqual(runtime_id, "cfc4564d-fed3-5803-9a4c-e3119733bb53")
            raise NativeCallbackInterrupted("stopped after saving launch")

        before = self.staff.staff_file.read_bytes()
        with patch.object(add_showrunner, "ensure_staff_showrunner", side_effect=interrupted):
            with self.assertRaisesRegex(NativeCallbackInterrupted, "stopped after saving launch"):
                _ = self.run_command("kid")
        receipt = completion_receipt.read_launch_receipt(self.root / "showrunner/cfc4564d-fed3-5803-9a4c-e3119733bb53/add_showrunner.json", staff.initial_snapshot())
        assert isinstance(receipt, completion_receipt.UnpublishedReceipt)
        identity = add_showrunner.ShowrunnerLaunch.from_receipt(receipt.fields)
        self.assertEqual(receipt.identity, identity.session_id)
        self.assertEqual(self.staff.staff_file.read_bytes(), before)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)
        self.assertFalse(any(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()))
        old_pid = int(self.env["FAKE_CLAUDE_PID"])
        old_process = next(process for process in self.processes if process.pid == old_pid)
        old_process.terminate()
        _ = old_process.wait(timeout=TEST_HANG_GUARD_SECS)
        returned_native = identity.session_id if transcript_exists else fixture_runtime_id("fresh-after-dead-showrunner")
        self.starts(returned_native, registers="kid")
        if transcript_exists:
            transcript = identity.config_dir / "projects/fixture" / f"{identity.session_id}.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(transcript, '{}\n')
        recovered = self.run_command("kid")
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 2)
        if transcript_exists:
            self.assertEqual(starts[-1][starts[-1].index("--resume") + 1], identity.session_id)
        else:
            self.assertNotIn("--resume", starts[-1])
        self.assertTrue(all("--session-id" not in command for command in starts))
        prompt = f"/showrunner:produce {self.doc('kid')}" + (" resume" if transcript_exists else "")
        self.assertEqual([row[-1] for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]], [prompt])
        self.assertEqual(self.parent_of("kid"), "top")
        child = staff.by_path("kid")
        assert child is not None
        self.assertEqual((child.id, child.current_session), (returned_native, returned_native))
        recovered_receipt = completion_receipt.read_launch_receipt(
            self.root / "showrunner" / child.id / "add_showrunner.json", staff.snapshot())
        assert isinstance(recovered_receipt, completion_receipt.OwnedReceipt)
        self.assertEqual(recovered_receipt.fields["session_id"], returned_native)

    def test_dead_saved_launch_resumes_its_transcript_with_the_resume_prompt(self) -> None:
        self.assert_dead_launch_recovers(transcript_exists=True)

    def test_dead_saved_launch_without_a_transcript_starts_its_reserved_session(self) -> None:
        self.assert_dead_launch_recovers(transcript_exists=False)

    def assert_open_launch_recovers(self, prompt_submitted: bool) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        _ = self.on_start.pop(str(self.timer_conf("kid")))
        if prompt_submitted:
            with patch.object(add_showrunner, "showrunner_appeared", side_effect=RuntimeError("stopped before timer")):
                with self.assertRaisesRegex(RuntimeError, "stopped before timer"):
                    _ = self.run_command("kid")
        else:
            with patch.object(herdr.Herdr, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("pane busy")):
                first = self.run_command("kid")
            self.assertEqual(first.returncode, add_showrunner.SESSION_NOT_READY, first.stderr)
        starts = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        self.assertEqual(starts, 1)
        self.assertNotIn("TARGET=session:cfc4564d-fed3-5803-9a4c-e3119733bb53", self.timer_conf("kid").read_text())

        def register(production_name: str) -> bool:
            self.assertEqual(production_name, "kid")
            _ = self.timer_conf(production_name).write_text(self.timer_text(production_name, "cfc4564d-fed3-5803-9a4c-e3119733bb53"))
            return True

        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(12345)),
              patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1")),
              patch.object(add_showrunner, "showrunner_appeared", side_effect=register)):
            recovered = self.run_command("kid")
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), starts)
        self.assertEqual(self.parent_of("kid"), "top")
        prompt_calls = sum(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls())
        self.assertEqual(prompt_calls, 1)
        child = staff.by_path("kid")
        parent = staff.by_path("top")
        assert child is not None and parent is not None
        self.assertEqual(child.parent, staff.ReportsTo(parent.id))

    def test_open_showrunner_recovers_before_its_timer_registers_without_second_launch(self) -> None:
        self.assert_open_launch_recovers(True)

    def test_open_showrunner_submits_its_owed_prompt_without_second_launch(self) -> None:
        self.assert_open_launch_recovers(False)

    def assert_observed_callback_recovers(self, after_publication: bool) -> None:
        native = "cfc4564d-fed3-5803-9a4c-e3119733bb53"
        self.starts(native, registers="kid")
        original = add_showrunner.ensure_staff_showrunner
        before = self.staff.staff_file.read_bytes()

        class NativeCallbackInterrupted(BaseException):
            """Process loss after native observation leaves the launch available for recovery."""

        def interrupted(child: ProductionDocument, caller: str, *, runtime_id: str = "") -> staff.StaffRow:
            self.assertEqual(runtime_id, native)
            if after_publication:
                _ = original(child, caller, runtime_id=runtime_id)
            raise NativeCallbackInterrupted("interrupted after native observation")

        with patch.object(add_showrunner, "ensure_staff_showrunner", side_effect=interrupted):
            with self.assertRaisesRegex(NativeCallbackInterrupted, "interrupted after native observation"):
                _ = self.run_command("kid")
        receipt_path = self.root / "showrunner/cfc4564d-fed3-5803-9a4c-e3119733bb53/add_showrunner.json"
        receipt = completion_receipt.read_launch_receipt(receipt_path, staff.initial_snapshot())
        if after_publication:
            self.assertIsInstance(receipt, completion_receipt.OwnedReceipt)
            assert isinstance(receipt, completion_receipt.OwnedReceipt)
            self.assertEqual(receipt.owner.id, native)
        else:
            self.assertIsInstance(receipt, completion_receipt.UnpublishedReceipt)
            assert isinstance(receipt, completion_receipt.UnpublishedReceipt)
            self.assertEqual(receipt.identity, native)
            self.assertIsInstance(completion_receipt.read_receipt(receipt_path, staff.initial_snapshot()),
                                  completion_receipt.NoReceipt)
            self.assertEqual(self.staff.staff_file.read_bytes(), before)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertNotIn("--session-id", starts[0])
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]], [])
        submit = herdr.Herdr.submit_opening_prompt

        def published_before_work(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                                  launched_at: datetime, timeout_ms: int) -> herdr.PromptSubmitted | herdr.PromptNotSubmitted:
            row = staff.by_id(staff.native_id(native))
            parent = staff.by_path("top")
            assert row is not None and parent is not None
            self.assertEqual((row.id, row.kind, row.name, row.doc),
                             (native, "showrunner", "kid", staff.LiveDoc("natedev", self.doc("kid"))))
            return submit(client, pane, opening, launched_at, timeout_ms)

        with (patch.object(herdr.Herdr, "submit_opening_prompt", new=published_before_work),
              patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1"))):
            recovered = self.run_command("kid")
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)
        self.assertEqual([row[-1] for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]],
                         [f"/showrunner:produce {self.doc('kid')}"])
        self.assertEqual(self.parent_of("kid"), "top")
        self.assertEqual([row.id for row in staff.find(kind="showrunner") if row.name == "kid"], [native])
        child, parent = staff.by_path("kid"), staff.by_path("top")
        assert child is not None and parent is not None
        self.assertEqual(child.parent, staff.ReportsTo(parent.id))

    def test_observed_native_launch_recovers_before_staff_publication_without_a_second_backend(self) -> None:
        self.assert_observed_callback_recovers(False)

    def test_observed_native_launch_recovers_after_staff_publication_before_its_opening_work(self) -> None:
        self.assert_observed_callback_recovers(True)

    def test_former_name_retry_reads_the_renamed_showrunners_receipt_and_refuses_another_backend(self) -> None:
        native = "cfc4564d-fed3-5803-9a4c-e3119733bb53"
        self.starts(native, registers="kid")
        first = self.run_command("kid")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        child = staff.by_path("kid")
        assert child is not None
        renamed = staff.rename(child.id, "renamed-kid")
        receipt_path = self.root / "showrunner" / child.id / "add_showrunner.json"
        receipt = completion_receipt.read_launch_receipt(receipt_path, staff.snapshot())
        self.assertIsInstance(receipt, completion_receipt.OwnedReceipt)
        before = self.staff.staff_file.read_bytes(), receipt_path.read_bytes(), self.herdr_calls()
        other_native = fixture_runtime_id("another-showrunner-backend")
        with (patch.object(completion_receipt, "read_launch_receipt",
                           wraps=completion_receipt.read_launch_receipt) as read,
              patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1")),
              patch.object(herdr.Herdr, "native_session", return_value=other_native)):
            retry = self.run_command("kid")
        self.assertEqual(retry.returncode, 2, retry.stdout + retry.stderr)
        self.assertIn(f"pending backend {native} differs from {other_native}", retry.stderr)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[0], receipt_path)
        self.assertEqual(staff.by_id(child.id), renamed)
        self.assertEqual((self.staff.staff_file.read_bytes(), receipt_path.read_bytes(), self.herdr_calls()), before)

    def test_retired_showrunner_receipt_cannot_complete_a_new_add_request(self) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        first = self.run_command("kid")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        child = staff.by_path("kid")
        assert child is not None
        wrapped = staff.set_doc(child.id, staff.WrappedDoc("natedev", self.doc("kid")))
        second = self.run_command("kid")
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertNotIn("already started", second.stdout)
        self.assertEqual(staff.by_id(child.id), wrapped)

    def test_add_showrunner_rerun(self) -> None:
        self.assert_observed_callback_recovers(False)
        rows = staff.snapshot().rows
        child = next(row for row in rows if row.name == "kid")
        parent = next(row for row in rows if row.name == "top")
        self.assertEqual(child.parent, staff.ReportsTo(parent.id))
        data, edge = self.staff.staff_file.read_bytes(), self.parent_of("kid")
        calls = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        with patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1")):
            again = self.run_command("kid")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already started", again.stdout)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.parent_of("kid")), (data, edge))
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), calls)

    def test_new_showrunner_launches_on_picked_default_account_and_attaches_it(self) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid", subprocess_cli=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2],
                         ["started kid: herdr session attach agents, workspace kid, tab kid", "kid now reports to top"])
        self.assertEqual(self.parent_of("kid"), "top")
        # The parent ran the command and reads its output; only the child is told.
        self.assertEqual(self.sent_to(), [f"uds:{self.root / 'cfc4564d-fed3-5803-9a4c-e3119733bb53.sock'}"])
        calls = self.herdr_calls()
        self.assertIn(["--user", "start", "herdr-agents.service"], calls)
        opened = self.opened()
        self.assertEqual(opened[:8], ["--session", "agents", "workspace", "create", "--label", "kid",
                                     "--cwd", str(self.root / "kid-checkout")])
        environment = dict(value.split("=", 1) for flag, value in zip(opened[8:], opened[9:]) if flag == "--env")
        self.assertEqual({key: value for key, value in environment.items()
                          if key.startswith(("CLAUDE_", "SHOWRUNNER_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
                          "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
                          "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        self.assertEqual(opened[-1], "--no-focus")
        started = self.started()
        self.assertEqual(started[started.index("--") + 1:], ["--model", "opus", "--effort", "high",
            "--remote-control", "kid", "-n", "kid", "--settings", '{"disableAgentView": true}'])
        self.assertEqual([row for row in calls if row[2:4] == ["agent", "prompt"]], [[
            "--session", "agents", "agent", "prompt", "w1:p1", f"/showrunner:produce {self.doc('kid')}"]])

    def test_resume_starts_in_the_given_directory_with_the_resume_prompt(self) -> None:
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(elsewhere), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.opened()[7], str(elsewhere))
        args = self.started()
        self.assertEqual(args[args.index("--resume") + 1], "cfc4564d-fed3-5803-9a4c-e3119733bb53")
        self.assertIn(["--session", "agents", "agent", "prompt", "w1:p1",
                       f"/showrunner:produce {self.doc('kid')} resume"], self.herdr_calls())
        alone = self.run_command("other", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53")
        self.assertEqual(alone.returncode, 2)
        self.assertIn("--resume and --cwd go together", alone.stderr)

    def test_showrunner_shared_tab_retries_enter_with_the_selected_session_transcript(self) -> None:
        child = read_new_production(self.doc("kid"), "kid")
        selected = accounts.NamedAccount("claude3", self.root / "selected")
        native = fixture_runtime_id("shared-tab-resumed-showrunner")
        row = fixture_member("showrunner", name="kid", doc=staff.LiveDoc("natedev", self.doc("kid")))
        _ = staff.update_current_session(row.current_session, staff.native_id(native))
        path, record = self.record(native, "kid")
        pid = cast(dict[str, object], json.loads(record))["pid"]
        transcript = selected.dir / "projects/fixture" / f"{native}.jsonl"
        transcript.parent.mkdir(parents=True)
        text = f"/showrunner:produce {child.doc} resume"
        elapsed = 0.0
        screen = "────────\n❯ \n────────"

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        def visible(*args: str, deadline: float = math.inf) -> str:
            del deadline
            if args[:2] == ("pane", "process-info"):
                return json.dumps({"result": {"process_info": {
                    "shell_pid": 998040, "foreground_process_group_id": pid,
                    "foreground_processes": [{"pid": pid, "name": "claude", "argv": ["claude"]}]}}})
            return screen

        def started(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            _ = Path(path).write_text(record)
            return subprocess.CompletedProcess([], 0, "", "")

        def turn(*, meta: bool) -> None:
            with transcript.open("a") as output:
                _ = output.write(json.dumps({"type": "user", "isMeta": meta,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "user", "content": text}}) + "\n")

        def typed(_client: herdr.Herdr, pane: str, prompt: str) -> None:
            nonlocal screen
            self.assertEqual((pane, prompt), ("destination:p1", text))
            screen = f"────────\n❯ {text}\n────────"
            turn(meta=True)

        def enter(_client: herdr.Herdr, pane: str, *keys: str) -> None:
            nonlocal screen
            self.assertEqual((pane, keys), ("destination:p1", ("Enter",)))
            screen = "────────\n❯ \n────────"
            turn(meta=False)

        def native_process(command: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
                           environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if command[0] == "git":
                return subprocess.run(command, env=environment, capture_output=True, text=True,
                                      check=False, timeout=timeout)
            self.assertEqual(command[-4:], ["pane", "process-info", "--pane", "destination:p1"])
            self.assertGreater(timeout, 0)
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": {"process_info": {
                "foreground_processes": [{"pid": pid, "name": "claude", "argv": ["claude"]}]}}}), "")

        with (patch.dict(os.environ, self.env),
              patch.object(herdr, "run_program", side_effect=native_process),
              patch.object(herdr, "shared_session", return_value="agents"),
              patch.object(herdr, "start_server"),
              patch.object(herdr, "open_tab", return_value="destination:p1") as tab,
              patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)),
              patch.object(herdr.Herdr, "panes", return_value=[{"pane_id": "destination:p1", "terminal_id": "destination-terminal", "workspace_id": "destination", "label": "kid"}]),
              patch.object(herdr.Herdr, "start_command", side_effect=started),
              patch.object(herdr.Herdr, "readiness", return_value={"agent_status": herdr.AgentStatus.IDLE, "interactive_ready": True}) as readiness,
              patch.object(herdr.Herdr, "_prompt", autospec=True, side_effect=typed) as prompt,
              patch.object(herdr.Herdr, "run", side_effect=visible),
              patch.object(herdr.Herdr, "_send_keys", autospec=True, side_effect=enter) as keys):
            outcome = add_showrunner.launch(child, "opus", "high", native, self.root, selected)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(keys.call_count, 1)
        readiness.assert_called_once_with("destination:p1")
        self.assertEqual(tab.call_args.args[:3], ("agents", "kid", "kid"))
        self.assertGreaterEqual(elapsed, 5)

    def test_unsubmitted_showrunner_is_named_and_never_attached_even_if_live(self) -> None:
        _ = self.doc("kid")
        for outcome in (herdr.PromptNotSubmitted("fixture lost input"), herdr.PromptSubmissionNotRequired()):
            with (self.subTest(outcome=outcome), patch.dict(os.environ, self.env, clear=True),
                  patch.object(showrunners, "own_name", return_value="top"),
                  patch.object(add_showrunner, "checked"),
                  patch.object(herdr, "shared_session", return_value="agents"),
                  patch.object(herdr, "start_server"), patch.object(herdr, "open_tab", return_value="destination:p1"),
                  patch.object(herdr, "start_agent", return_value=outcome) as start,
                  patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                  patch.object(add_showrunner, "showrunner_appeared", return_value=True) as appeared,
                  redirect_stderr(io.StringIO()) as error):
                code = add_showrunner.main(["--production", "kid", "--doc", str(self.doc("kid")), "--model", "opus", "--effort", "high"])
            self.assertEqual(code, add_showrunner.SESSION_NOT_READY)
            self.assertIn("kid: opening prompt not submitted", error.getvalue())
            appeared.assert_not_called()
            opening = cast(herdr.OpeningPrompt, start.call_args.args[4])
            assert isinstance(opening.transcript, herdr.SessionTranscript)
            self.assertEqual(opening.transcript.config_dir, Path(self.env["HOME"]) / ".claude2")
            arguments = cast(list[str], start.call_args.args[3])
            self.assertEqual(opening.transcript.session_id, "")
            self.assertNotIn("--session-id", arguments)
            self.assertIsNotNone(opening.before_prompt)
        self.assertEqual(self.parent_of("kid"), "")

    def test_new_showrunner_reads_stored_showrunners_account_despite_better_unit_reading(self) -> None:
        directory = self.named_account()
        _ = self.env.pop("ADD_SHOWRUNNER_CLAUDE_JSON")
        home = Path(self.env["HOME"])
        path = home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude3"}))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(accounts.setup("claude1"), [])
            caller = accounts.account_dir("claude1")
            _ = (caller / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "unit@example.invalid"}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, self.env):
            stored_room("main@example.invalid", 99, now + timedelta(hours=1))
            stored_room("target@example.invalid", 0, now + timedelta(days=4))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        before = path.read_bytes()
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR=" + str(Path(self.env["HOME"]) / ".claude"), self.opened())
        self.assertEqual(path.read_bytes(), before)

    def test_unconfigured_showrunners_refuse_new_and_resume_without_group_write(self) -> None:
        """F004: live parent identity and readings cannot supply missing configuration."""
        path = self.configure_groups()
        path.unlink()
        for options in ((), ("--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root))):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "accounts.py move showrunners <claudeN>")
                self.assertFalse(path.exists())
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_launch_accepts_one_account_shared_with_units(self) -> None:
        """F004: both launch groups may use the same configured account."""
        directory = self.named_account()
        _ = self.configure_groups(units="claude3", showrunners="claude3")
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_new_showrunner_repairs_a_wrong_shared_link_before_launching(self) -> None:
        directory = self.named_account()
        linked = directory / "projects"
        linked.unlink()
        linked.symlink_to(Path(self.env["HOME"]) / "missing-projects")
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), Path(self.env["HOME"]) / ".claude/projects")
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertEqual(self.parent_of("kid"), "top")

    def test_new_showrunner_preserves_a_real_shared_file_and_launches_after_an_anomaly(self) -> None:
        directory = self.named_account()
        local = directory / "skills"
        local.unlink()
        _ = local.write_text("local skills data")
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"accounts: anomaly: {local} is a real file; move it aside or merge it into ~/.claude",
                      result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertFalse(local.is_symlink())
        self.assertEqual(local.read_text(), "local skills data")

    def test_picked_named_account_writes_trust_despite_default_trust(self) -> None:
        directory = self.named_account()
        _ = self.env.pop("ADD_SHOWRUNNER_CLAUDE_JSON")
        home = Path(self.env["HOME"])
        write_text_atomic(home / ".claude.json", json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"},
            "projects": {str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        write_text_atomic(directory / ".claude.json", json.dumps({
            "oauthAccount": {"emailAddress": "target@example.invalid"}, "projects": {}}))
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, self.env):
            stored_room("target@example.invalid", 1, now + timedelta(days=1))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", self.opened())
        with patch.dict(os.environ, self.env):
            self.assertIsInstance(herdr.claude_trust(self.root / "kid-checkout", directory / ".claude.json"), herdr.Trusted)

    def test_showrunner_resume_refuses_unconfigured_directory_before_reading_its_trust(self) -> None:
        """F004: a retained directory cannot override group configuration."""
        directory = Path(self.env["HOME"]) / "retained account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text("SECRET malformed account contents")
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude2")
        self.assertNotIn("SECRET", result.stderr)

    def test_resume_refuses_an_unrelated_tab_of_the_same_name_without_closing_it(self) -> None:
        (self.state / "live-agents").touch()
        self.live("unrelated-conversation")
        result = self.run_command("kid", "--resume", fixture_runtime_id("ended-conversation"), "--cwd", str(self.root),
                                  "--account", "claude2")
        self.refused(result, "a tab named kid is already open in herdr session agents")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse(any(row[2:4] in (["server", "stop"], ["tab", "close"]) for row in self.herdr_calls()))
        self.assertTrue((self.state / "live-agents").exists())

    def test_showrunner_resume_refuses_directory_outside_configured_account(self) -> None:
        """F004: a valid retained directory is still not the configured account."""
        directory = Path(self.env["HOME"]) / "retained account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        _ = self.named_account()
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude3")
        self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_accepts_relative_directory_of_configured_account(self) -> None:
        """F004: explicit matching directories are accepted after path resolution."""
        home = Path(self.env["HOME"])
        directory = self.named_account()
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root),
                                  "--config-dir", ".claude3", cwd=home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_config_directory_requires_resume_and_excludes_account(self) -> None:
        directory = Path(self.env["HOME"]) / "retained"
        directory.mkdir()
        for options in (("--config-dir", str(directory)),
                        ("--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root), "--config-dir", str(directory),
                         "--account", "claude2")):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--config-dir", result.stderr)
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_uses_configured_account_instead_of_recorded_default(self) -> None:
        """F004: resume selection ignores the previous account."""
        directory = self.named_account()
        _ = (self.state / "kid@claude_config_dir").write_text(str(Path(self.env["HOME"]) / ".claude"))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_recorded_herdr_resume_without_an_account_passes_check_using_configured_group(self) -> None:
        self.resume_record()
        for pid, parent, argv, environment in (
            (991000, 0, ["herdr", "--session", "kid", "server"], {}),
            (991001, 991000, ["claude"], {"HERDR_PANE_ID": "w1:p1"}),
        ):
            write_process(self.env, pid, parent, argv, environment)
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root), "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check: ok", result.stdout)
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_showrunner_refuses_explicit_account_different_from_configured_group(self) -> None:
        """F004: an explicit account must agree with the showrunners account."""
        _ = self.named_account()
        for options in (("--account", "claude2"),
                        ("--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root), "--account", "claude2")):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "claude3")
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_without_recorded_account_uses_configured_group(self) -> None:
        """F004: configuration is sufficient for a resume with no retained account."""
        directory = self.named_account()
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root), "--account", "claude3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_launch_carries_no_marks_or_caller_account_variables(self) -> None:
        self.env["CLAUDE_CODE_OAUTH_TOKEN"] = "caller-token"
        self.env["SHOWRUNNER_UNIT"] = "parent"
        self.env["SHOWRUNNER_UNIT_ID"] = "parent"
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = dict(value.split("=", 1) for flag, value in zip(self.opened()[8:], self.opened()[9:]) if flag == "--env")
        self.assertEqual({key: value for key, value in environment.items()
                          if key.startswith(("CLAUDE_", "SHOWRUNNER_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
                          "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
                          "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        self.assertEqual(self.opened()[-1], "--no-focus")
        self.assertNotIn("caller-token", json.dumps(self.herdr_calls()))

    def test_showrunner_start_not_ready_returns_a_distinct_status_and_sends_no_prompt_or_key(self) -> None:
        self.herdr_overrides["agent start"] = CommandResponse(code=1, stdout=json.dumps({
            "error": {"code": "agent_not_ready", "message": "permission dialog"}}))
        screen = "Do you trust this folder?\n1. Yes\n2. No"
        self.herdr_overrides["--session agents pane read w1:p1 --source visible"] = CommandResponse(
            code=0, stdout=screen)
        result = self.run_command("kid")
        self.assertEqual(result.returncode, add_showrunner.SESSION_NOT_READY, result.stdout + result.stderr)
        self.assertIn(screen, result.stderr)
        self.assertFalse(any(row[2:4] in (["agent", "prompt"], ["pane", "send-keys"],
                                        ["pane", "send-text"]) for row in self.herdr_calls()))
        self.assertEqual(self.sent_to(), [])
        self.assertEqual(self.parent_of("kid"), "")

    def test_showrunner_start_failure_is_reported_without_attaching(self) -> None:
        self.herdr_overrides["workspace create"] = CommandResponse(
                code=1, stdout="", stderr="fixture could not open workspace")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("fixture could not open workspace", result.stderr)
        self.assertFalse(any(row[2:4] == ["agent", "start"] for row in self.herdr_calls()))
        self.assertEqual(self.parent_of("kid"), "")

    def test_unreadable_herdr_inventory_refuses_before_starting_the_child(self) -> None:
        self.herdr_overrides["--session default session list --json"] = CommandResponse(
            code=1, stdout="", stderr="fixture cannot list herdr sessions")
        result = self.run_command("kid")
        self.refused(result, "herdr session list: stderr: fixture cannot list herdr sessions")
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_child_opens_its_tab_in_a_shared_server_that_already_runs(self) -> None:
        self.herdr_overrides["--session agents status server"] = CommandResponse(
            code=0, stdout="status: running\n")
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.herdr_calls()
        self.assertFalse(any(row[:1] == ["--user"] for row in calls))
        self.assertEqual(self.opened()[:6], ["--session", "agents", "workspace", "create", "--label", "kid"])
        self.assertEqual(self.parent_of("kid"), "top")

    def test_new_showrunner_zero_reading_without_refusal_can_launch(self) -> None:
        file = Path(self.env["HOME"]) / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text()))
        held = saved["main@example.invalid"]
        assert "weekly" in held
        held["weekly"]["left"] = 0
        _ = file.write_text(json.dumps(saved))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={Path(self.env['HOME']) / '.claude2'}", self.opened())

    def test_a_timer_left_by_an_ended_session_does_not_stand_in_the_way(self) -> None:
        # The child ran before under this parent; its session ended, and its timer and its edge are still stored.
        self.timer("kid", "cfc4564d-fed3-5803-9a4c-e3119733bb53")
        edge = self.root / "showrunner" / "kid" / "parent"
        edge.parent.mkdir(parents=True)
        _ = edge.write_text("top\n", encoding="utf-8")
        top = fixture_member("showrunner", name="top")
        child = staff.by_path("kid")
        assert child is not None
        _ = staff.move(child.id, staff.ReportsTo(top.id))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53")
        result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(self.root), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["started kid: herdr session attach agents, workspace kid, tab kid", "kid already reports to top"])
        self.assertEqual(self.sent_to(), [])

    def test_a_stored_timer_alone_is_not_a_showrunner_that_appeared(self) -> None:
        self.timer("kid", fixture_runtime_id("ended-session"))
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid has no running showrunner after 0 s", result.stdout)

    def test_no_showrunner_in_time_leaves_the_session_running_and_unattached(self) -> None:
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid has no running showrunner after 0 s, so it is not attached. Its session is still running",
                      result.stdout)
        self.assertIn("showrunners.py attach kid top", result.stdout)
        self.assertIn("herdr session attach agents, workspace kid, tab kid", result.stdout)
        self.assertTrue((self.state / "live-agents").exists())
        self.assertEqual(self.parent_of("kid"), "")
        self.assertEqual(self.sent_to(), [])

    def test_session_records_that_cannot_be_read_refuse_the_start(self) -> None:
        _ = (self.root / "sessions.py").write_text(
            "import sys\nprint('registry gone', file=sys.stderr)\nraise SystemExit(3)\n", encoding="utf-8")
        self.timer("kid", fixture_runtime_id("some-session"))
        row = staff.by_path("kid")
        assert row is not None
        self.refused(self.run_command("kid"), f"cannot tell whether session:{row.current_session} is running: registry gone")

    def test_a_registry_unreadable_for_a_moment_during_the_wait_is_tried_again(self) -> None:
        self.flaky_registry(1)
        self.env["ADD_SHOWRUNNER_WAIT_S"] = "30"
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.parent_of("kid"), "top")

    def test_a_registry_still_unreadable_at_the_deadline_leaves_the_child_unattached(self) -> None:
        self.flaky_registry(1000)
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid is not attached: cannot tell whether session:cfc4564d-fed3-5803-9a4c-e3119733bb53 is running: "
                      + "one or more registry files could not be read", result.stdout)
        self.assertEqual(self.parent_of("kid"), "")

    def test_check_runs_the_refusals_and_starts_nothing(self) -> None:
        result = self.run_command("kid", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(),
                         f"check: ok — would start kid in {self.root / 'kid-checkout'} and attach it to top")
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_launch_check_with_no_ending_tab_refuses_a_same_named_shared_tab(self) -> None:
        (self.state / "live-agents").touch()
        self.refused(self.run_command("kid", "--check"), "a tab named kid is already open in herdr session agents")
        self.assertIn(["--session", "agents", "pane", "list"], self.herdr_calls())

    def test_each_refusal_names_its_cause_and_starts_nothing(self) -> None:
        not_a_doc = self.invoke(["--production", "unknown", "--model", "opus", "--effort", "high"])
        self.assertEqual(not_a_doc.returncode, 2, not_a_doc.stdout + not_a_doc.stderr)
        self.assertIn("add_showrunner: ", not_a_doc.stderr)
        self.refused(self.run_command("top"), "top is this session's own production")
        self.timer("running", fixture_runtime_id("running-session"))
        self.live(fixture_runtime_id("running-session"))
        self.refused(self.run_command("running"), "running already has a running showrunner")
        (self.state / "live-agents").touch()
        self.refused(self.run_command("kid"), "a tab named kid is already open in herdr session agents")

    def test_a_production_above_the_caller_is_refused(self) -> None:
        self.timer("middle", fixture_runtime_id("middle-session"))
        self.timer("grand", fixture_runtime_id("grand-session"))
        grand = fixture_member("showrunner", name="grand")
        middle = staff.by_path("middle")
        assert middle is not None
        middle = staff.move(middle.id, staff.ReportsTo(grand.id))
        top = next(row for row in staff.find(kind="showrunner") if row.name == "top")
        _ = staff.move(top.id, staff.ReportsTo(middle.id))
        for child, parent in (("top", "middle"), ("middle", "grand")):
            edge = self.root / "showrunner" / child / "parent"
            edge.parent.mkdir(parents=True)
            _ = edge.write_text(parent + "\n", encoding="utf-8")
        self.refused(self.run_command("grand"), "top already reports to grand, directly or through others")

    def test_an_untrusted_start_folder_writes_trust_and_starts_without_config_contents(self) -> None:
        checkout = self.root / "kid-checkout"
        _ = self.doc("kid")
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        write_text_atomic(config, json.dumps({"oauthAccount": {"emailAddress": "SECRET-EMAIL"}, "userID": "SECRET-ID"}))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("SECRET", result.stdout + result.stderr)
        self.assertEqual(self.started()[2:4], ["agent", "start"])
        with patch.dict(os.environ, self.env):
            self.assertIsInstance(herdr.claude_trust(checkout, config), herdr.Trusted)

    def test_trust_comes_from_the_repository_root_or_a_worktrees_main_checkout(self) -> None:
        main = self.root / "main"
        main.mkdir()
        for command in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@x.invalid",
                                                       "commit", "-q", "--allow-empty", "-m", "seed"],
                        ["worktree", "add", "-q", "-b", "side", str(self.root / "side")]):
            _ = subprocess.run(["git", "-C", str(main), *command], check=True, capture_output=True)
        (main / "sub").mkdir()
        self.trust(main)
        for folder in (main / "sub", self.root / "side"):
            with self.subTest(folder=folder.name):
                result = self.run_command("kid", "--resume", "cfc4564d-fed3-5803-9a4c-e3119733bb53", "--cwd", str(folder), "--account", "claude2", "--check")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_repository_below_a_trusted_parent_gets_its_own_trust_key(self) -> None:
        _ = self.doc("kid")
        checkout = self.root / "kid-checkout"
        _ = subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        write_text_atomic(config, json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        self.starts("cfc4564d-fed3-5803-9a4c-e3119733bb53", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        settings = cast(dict[str, dict[str, dict[str, bool]]], json.loads(config.read_text()))
        self.assertTrue(settings["projects"][str(checkout.resolve())]["hasTrustDialogAccepted"])
        self.assertTrue(settings["projects"][str(self.root)]["hasTrustDialogAccepted"])
        self.assertEqual(self.started()[2:4], ["agent", "start"])

    def test_an_unreadable_account_config_prints_one_anomaly_without_starting(self) -> None:
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        write_text_atomic(config, '{"oauthAccount": "SECRET-EMAIL", ')
        result = self.run_command("kid")
        self.assertEqual(result.returncode, add_showrunner.SESSION_NOT_READY, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn(f"{self.root / 'kid-checkout'}: trust anomaly: {config}: JSONDecodeError", result.stderr)
        self.assertNotIn("SECRET", result.stderr)
        self.assertFalse(any(row[2:4] == ["agent", "start"] for row in self.herdr_calls()))

    def test_a_session_that_runs_no_production_is_refused(self) -> None:
        self.env["CODEX_THREAD_ID"] = fixture_runtime_id("unattached-showrunner-caller")
        self.refused(self.run_command("kid"), "this session runs no production, so it has nothing to attach kid to")


if __name__ == "__main__":
    _ = unittest.main()
