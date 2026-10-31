#!/usr/bin/env python3
"""add_showrunner.py through its CLI, with fake herdr, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts import accounts
from scripts.production import add_showrunner
import production_units
from scripts.production import showrunners
import showrunner_tree
from scripts.whoami import usage_store
import herdr as runtime
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse

from scripts.production.add_showrunner_test_support import AddShowrunnerFixture, SCRIPT, TEST_HANG_GUARD_SECS


from scripts.production import staff
from scripts.production import completion_receipt
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import sessions

class AddShowrunnerTests(AddShowrunnerFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = staff.ensure("showrunner", name="top")

    def test_existing_staff_parent_survives_a_stale_saved_parent_during_ensure(self) -> None:
        self.timer("kid", "kid-session")
        parent = staff.ensure("showrunner", name="other")
        child = staff.ensure("showrunner", name="kid", parent=parent.id)
        path = self.root / "showrunner/kid/parent"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, "top\n")
        before = self.staff.staff_file.read_bytes(), path.read_bytes()
        with (patch.dict(os.environ, self.env),
              patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier")):
            ensured = add_showrunner.ensure_staff_showrunner(production_units.read_production(self.doc("kid")), "top")
        self.assertEqual(ensured, child)
        self.assertEqual((self.staff.staff_file.read_bytes(), path.read_bytes()), before)

    def assert_dead_launch_recovers(self, *, transcript_exists: bool) -> None:
        self.starts("kid-session", registers="kid")
        with patch.object(add_showrunner, "launch", side_effect=RuntimeError("stopped after saving launch")):
            with self.assertRaisesRegex(RuntimeError, "stopped after saving launch"):
                _ = self.run_command("kid")
        receipt = completion_receipt.read_receipt(self.root / "showrunner/kid/add_showrunner.json", staff.snapshot())
        assert isinstance(receipt, completion_receipt.OwnedReceipt)
        identity = add_showrunner.ShowrunnerLaunch.from_receipt(receipt.fields)
        if transcript_exists:
            transcript = identity.config_dir / "projects/fixture" / f"{identity.session_id}.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(transcript, '{}\n')
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
            recovered = self.run_command("kid")
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        flag = "--resume" if transcript_exists else "--session-id"
        self.assertEqual(starts[0][starts[0].index(flag) + 1], identity.session_id)
        self.assertNotIn("--session-id" if transcript_exists else "--resume", starts[0])
        prompt = f"/showrunner:produce {self.doc('kid')}" + (" resume" if transcript_exists else "")
        self.assertEqual([row[-1] for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]], [prompt])
        self.assertEqual(self.parent_of("kid"), "top")

    def test_dead_saved_launch_resumes_its_transcript_with_the_resume_prompt(self) -> None:
        self.assert_dead_launch_recovers(transcript_exists=True)

    def test_dead_saved_launch_without_a_transcript_starts_its_reserved_session(self) -> None:
        self.assert_dead_launch_recovers(transcript_exists=False)

    def assert_open_launch_recovers(self, prompt_submitted: bool) -> None:
        self.starts("kid-session", registers="kid")
        _ = self.on_start.pop(str(self.timer_conf("kid")))
        if prompt_submitted:
            with patch.object(add_showrunner, "showrunner_appeared", side_effect=RuntimeError("stopped before timer")):
                with self.assertRaisesRegex(RuntimeError, "stopped before timer"):
                    _ = self.run_command("kid")
        else:
            with patch.object(runtime.Herdr, "submit_opening_prompt", return_value=runtime.PromptNotSubmitted("pane busy")):
                first = self.run_command("kid")
            self.assertEqual(first.returncode, add_showrunner.SESSION_NOT_READY, first.stderr)
        starts = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        self.assertEqual(starts, 1)
        self.assertNotIn("TARGET=session:kid-session", self.timer_conf("kid").read_text())

        def register(slug: str) -> bool:
            self.assertEqual(slug, "kid")
            _ = self.timer_conf(slug).write_text(self.timer_text(slug, "kid-session"))
            return True

        with (patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(12345)),
              patch.object(runtime, "herdr_place_of", return_value=runtime.HerdrPlace("agents", "w1:p1")),
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
        self.assertEqual(child.parent, parent.id)

    def test_open_showrunner_recovers_before_its_timer_registers_without_second_launch(self) -> None:
        self.assert_open_launch_recovers(True)

    def test_open_showrunner_submits_its_owed_prompt_without_second_launch(self) -> None:
        self.assert_open_launch_recovers(False)

    def test_retired_showrunner_receipt_cannot_complete_a_new_add_request(self) -> None:
        self.starts("kid-session", registers="kid")
        first = self.run_command("kid")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        child = staff.by_path("kid")
        assert child is not None
        _ = staff.retire(child.id)
        second = self.run_command("kid")
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertNotIn("already started", second.stdout)
        self.assertIsNone(staff.by_id(child.id))

    def test_add_showrunner_rerun(self) -> None:
        self.starts("kid-session", registers="kid")
        original = add_showrunner.ensure_staff_showrunner
        for after in (False, True):
            def interrupted(child: production_units.Production, caller: str) -> staff.StaffRow:
                if after:
                    _ = original(child, caller)
                raise RuntimeError("stopped at staff step")
            with patch.object(add_showrunner, "ensure_staff_showrunner", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff step"):
                    _ = self.run_command("kid")
            self.assertFalse(any(row[2:4] == ["agent", "start"] for row in self.herdr_calls()))
        first = self.run_command("kid")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        rows = staff.snapshot().rows
        child = next(row for row in rows if row.name == "kid")
        parent = next(row for row in rows if row.name == "top")
        self.assertEqual(child.parent, parent.id)
        data, edge = self.staff.staff_file.read_bytes(), self.parent_of("kid")
        calls = sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls())
        again = self.run_command("kid")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already started", again.stdout)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.parent_of("kid")), (data, edge))
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), calls)

    def test_new_showrunner_launches_on_picked_default_account_and_attaches_it(self) -> None:
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid", subprocess_cli=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2],
                         ["started kid: herdr session attach agents, workspace kid, tab kid", "kid now reports to top"])
        self.assertEqual(self.parent_of("kid"), "top")
        # The parent ran the command and reads its output; only the child is told.
        self.assertEqual(self.sent_to(), [f"uds:{self.root / 'kid-session.sock'}"])
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
            "--session-id", started[started.index("--session-id") + 1], "--remote-control", "kid", "-n", "kid", "--settings", '{"disableAgentView": true}'])
        self.assertEqual([row for row in calls if row[2:4] == ["agent", "prompt"]], [[
            "--session", "agents", "agent", "prompt", "w1:p1", f"/showrunner:produce {self.doc('kid')}"]])

    def test_resume_starts_in_the_given_directory_with_the_resume_prompt(self) -> None:
        self.starts("abc-123", registers="kid")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(elsewhere), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.opened()[7], str(elsewhere))
        args = self.started()
        self.assertEqual(args[args.index("--resume") + 1], "abc-123")
        self.assertIn(["--session", "agents", "agent", "prompt", "w1:p1",
                       f"/showrunner:produce {self.doc('kid')} resume"], self.herdr_calls())
        alone = self.run_command("other", "--resume", "abc-123")
        self.assertEqual(alone.returncode, 2)
        self.assertIn("--resume and --cwd go together", alone.stderr)

    def test_showrunner_shared_tab_retries_enter_with_the_selected_session_transcript(self) -> None:
        child = production_units.read_production(self.doc("kid"))
        selected = accounts.NamedAccount("claude3", self.root / "selected")
        transcript = selected.dir / "projects/fixture/resumed-id.jsonl"
        transcript.parent.mkdir(parents=True)
        text = f"/showrunner:produce {child.doc} resume"
        elapsed = 0.0
        screen = "────────\n❯ \n────────"

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        def visible(*_args: str) -> str:
            return screen

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

        with (patch.object(runtime, "shared_session", return_value="agents"),
              patch.object(runtime, "start_server"),
              patch.object(runtime, "open_tab", return_value="destination:p1") as tab,
              patch.object(runtime, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)),
              patch.object(runtime.Herdr, "panes", return_value=[{"pane_id": "destination:p1", "label": "kid"}]),
              patch.object(runtime.Herdr, "start_command", return_value=subprocess.CompletedProcess([], 0, "", "")),
              patch.object(runtime.Herdr, "readiness", return_value={"agent_status": "idle", "interactive_ready": True}) as readiness,
              patch.object(runtime.Herdr, "prompt", autospec=True, side_effect=typed) as prompt,
              patch.object(runtime.Herdr, "run", side_effect=visible),
              patch.object(runtime.Herdr, "send_keys", autospec=True, side_effect=enter) as keys):
            outcome = add_showrunner.launch(child, "opus", "high", "resumed-id", self.root, selected)
        self.assertIsInstance(outcome, runtime.PromptSubmitted)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(keys.call_count, 1)
        readiness.assert_called_once_with("destination:p1")
        self.assertEqual(tab.call_args.args[:3], ("agents", "kid", "kid"))
        self.assertGreaterEqual(elapsed, 5)

    def test_unsubmitted_showrunner_is_named_and_never_attached_even_if_live(self) -> None:
        _ = self.doc("kid")
        for outcome in (runtime.PromptNotSubmitted("fixture lost input"), runtime.PromptSubmissionNotRequired()):
            with (self.subTest(outcome=outcome), patch.dict(os.environ, self.env, clear=True),
                  patch.object(showrunners, "own_slug", return_value="top"),
                  patch.object(add_showrunner, "checked"),
                  patch.object(runtime, "shared_session", return_value="agents"),
                  patch.object(runtime, "start_server"), patch.object(runtime, "open_tab", return_value="destination:p1"),
                  patch.object(runtime, "start_agent", return_value=outcome) as start,
                  patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                  patch.object(add_showrunner, "showrunner_appeared", return_value=True) as appeared,
                  redirect_stderr(io.StringIO()) as error):
                code = add_showrunner.main(["--production", str(self.doc("kid")), "--model", "opus", "--effort", "high"])
            self.assertEqual(code, add_showrunner.SESSION_NOT_READY)
            self.assertIn("kid: opening prompt not submitted", error.getvalue())
            appeared.assert_not_called()
            opening = cast(runtime.OpeningPrompt, start.call_args.args[4])
            assert isinstance(opening.transcript, runtime.SessionTranscript)
            self.assertEqual(opening.transcript.config_dir, Path(self.env["HOME"]) / ".claude2")
            arguments = cast(list[str], start.call_args.args[3])
            self.assertEqual(opening.transcript.session_id, arguments[arguments.index("--session-id") + 1])
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
        saved = {"main@example.invalid": {"weekly": {"left": 99, "at": now.isoformat(),
                  "resets": (now + timedelta(hours=1)).isoformat()}},
                 "target@example.invalid": {"weekly": {"left": 0, "at": now.isoformat(),
                  "resets": (now + timedelta(days=4)).isoformat()}}}
        _ = (home / ".local/state/agent-notes/usage.json").write_text(json.dumps(saved))
        self.starts("kid-session", registers="kid")
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
        for options in ((), ("--resume", "abc-123", "--cwd", str(self.root))):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "accounts.py move showrunners <claudeN>")
                self.assertFalse(path.exists())
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_launch_accepts_one_account_shared_with_units(self) -> None:
        """F004: both launch groups may use the same configured account."""
        directory = self.named_account()
        _ = self.configure_groups(units="claude3", showrunners="claude3")
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_new_showrunner_refuses_configured_account_with_wrong_login(self) -> None:
        """F006: a trusted directory cannot bypass the configured account's readiness."""
        directory = self.named_account()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "other@example.invalid"},
            "projects": {str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        before = path.read_bytes()
        result = self.run_command("kid")
        self.refused(result, "logged in as other@example.invalid, not claude3")
        self.assertEqual(path.read_bytes(), before)

    def test_new_showrunner_repairs_a_wrong_shared_link_before_launching(self) -> None:
        directory = self.named_account()
        linked = directory / "projects"
        linked.unlink()
        linked.symlink_to(Path(self.env["HOME"]) / "missing-projects")
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), Path(self.env["HOME"]) / ".claude/projects")
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertEqual(self.parent_of("kid"), "top")

    def test_new_showrunner_refuses_a_real_file_against_an_existing_shared_folder(self) -> None:
        directory = self.named_account()
        local = directory / "projects"
        local.unlink()
        _ = local.write_text("local projects data")
        result = self.run_command("kid")
        self.refused(result, f"{local} is a real file; move it aside or merge it into ~/.claude")
        self.assertFalse(local.is_symlink())
        self.assertEqual(local.read_text(), "local projects data")

    def test_picked_named_account_refuses_untrusted_folder_despite_default_trust(self) -> None:
        directory = self.named_account()
        _ = self.env.pop("ADD_SHOWRUNNER_CLAUDE_JSON")
        home = Path(self.env["HOME"])
        _ = (home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"},
            "projects": {str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "target@example.invalid"}, "projects": {}}))
        file = home / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text()))
        now = datetime.now(timezone.utc)
        saved["target@example.invalid"] = {"weekly": {"left": 1, "at": now.isoformat(),
            "resets": (now + timedelta(days=1)).isoformat()}}
        _ = file.write_text(json.dumps(saved))
        result = self.run_command("kid")
        self.refused(result, f"kid: Claude is not trusted in {self.root / 'kid-checkout'} on claude3. Run:")
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", result.stderr)

    def test_showrunner_resume_refuses_unconfigured_directory_before_reading_its_trust(self) -> None:
        """F004: a retained directory cannot override group configuration."""
        directory = Path(self.env["HOME"]) / "retained account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text("SECRET malformed account contents")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude2")
        self.assertNotIn("SECRET", result.stderr)

    def test_resume_refuses_an_unrelated_tab_of_the_same_name_without_closing_it(self) -> None:
        (self.state / "live-agents").touch()
        self.live("unrelated-conversation")
        result = self.run_command("kid", "--resume", "ended-conversation", "--cwd", str(self.root),
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
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude3")
        self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_accepts_relative_directory_of_configured_account(self) -> None:
        """F004: explicit matching directories are accepted after path resolution."""
        home = Path(self.env["HOME"])
        directory = self.named_account()
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", ".claude3", cwd=home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_config_directory_requires_resume_and_excludes_account(self) -> None:
        directory = Path(self.env["HOME"]) / "retained"
        directory.mkdir()
        for options in (("--config-dir", str(directory)),
                        ("--resume", "abc-123", "--cwd", str(self.root), "--config-dir", str(directory),
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
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_resume_uses_the_configured_showrunners_account(self) -> None:
        directory = self.named_account()
        child = production_units.read_production(self.doc("kid"))
        self.resume_record()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(accounts, "role_account", wraps=accounts.role_account) as role:
            selected = add_showrunner.account_for_launch(child, "abc-123")
        self.assertEqual(selected, accounts.NamedAccount("claude3", directory))
        role.assert_called_once_with("showrunner")

    def test_check_validates_the_destination_without_returning_a_binary(self) -> None:
        child = production_units.read_production(self.doc("kid"))
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(add_showrunner, "running", return_value=False), \
                patch.object(showrunner_tree, "descendants_of", return_value=[]), \
                patch.object(add_showrunner, "herdr_tab_open", return_value=False) as destination:
            result = add_showrunner.checked(child, "top", config_dir=Path(self.env["HOME"]) / ".claude2")
        self.assertIsNone(result)
        destination.assert_called_once_with("kid", add_showrunner.NoEndingTab())

    def test_recorded_herdr_resume_without_an_account_passes_check_using_configured_group(self) -> None:
        self.resume_record()
        for pid, parent, argv, environment in (
            (991000, 0, ["herdr", "--session", "kid", "server"], {}),
            (991001, 991000, ["claude"], {"HERDR_PANE_ID": "w1:p1"}),
        ):
            process = Path(self.env["HERDR_PROC"]) / str(pid)
            process.mkdir(parents=True)
            _ = (process / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
            _ = (process / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
            _ = (process / "environ").write_bytes(
                ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode()
            )
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check: ok", result.stdout)
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_showrunner_refuses_explicit_account_different_from_configured_group(self) -> None:
        """F004: an explicit account must agree with the showrunners account."""
        _ = self.named_account()
        for options in (("--account", "claude2"),
                        ("--resume", "abc-123", "--cwd", str(self.root), "--account", "claude2")):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "claude3")
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_without_recorded_account_uses_configured_group(self) -> None:
        """F004: configuration is sufficient for a resume with no retained account."""
        directory = self.named_account()
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--account", "claude3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_launch_carries_no_marks_or_caller_account_variables(self) -> None:
        self.env["CLAUDE_CODE_OAUTH_TOKEN"] = "caller-token"
        self.env["SHOWRUNNER_UNIT"] = "parent"
        self.env["SHOWRUNNER_UNIT_ID"] = "parent-unit"
        self.starts("kid-session", registers="kid")
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
        self.starts("kid-session", registers="kid")
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
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={Path(self.env['HOME']) / '.claude2'}", self.opened())

    def test_a_timer_left_by_an_ended_session_does_not_stand_in_the_way(self) -> None:
        # The child ran before under this parent; its session ended, and its timer and its edge are still stored.
        self.timer("kid", "abc-123")
        edge = self.root / "showrunner" / "kid" / "parent"
        edge.parent.mkdir(parents=True)
        _ = edge.write_text("top\n", encoding="utf-8")
        top = staff.ensure("showrunner", name="top")
        _ = staff.ensure("showrunner", name="kid", parent=top.id)
        self.starts("abc-123")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["started kid: herdr session attach agents, workspace kid, tab kid", "kid already reports to top"])
        self.assertEqual(self.sent_to(), [])

    def test_a_stored_timer_alone_is_not_a_showrunner_that_appeared(self) -> None:
        self.timer("kid", "ended-session")
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
        self.timer("kid", "some-session")
        self.refused(self.run_command("kid"), "cannot tell whether session:some-session is running: registry gone")

    def test_a_registry_unreadable_for_a_moment_during_the_wait_is_tried_again(self) -> None:
        self.flaky_registry(1)
        self.env["ADD_SHOWRUNNER_WAIT_S"] = "30"
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.parent_of("kid"), "top")

    def test_showrunner_wait_retries_with_the_configured_interval(self) -> None:
        self.flaky_registry(1)
        self.env["ADD_SHOWRUNNER_WAIT_S"] = "300"
        self.starts("kid-session", registers="kid")
        polls = self.root / "polls.jsonl"
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.parent_of("kid"), "top")
        self.assertEqual([json.loads(line) for line in polls.read_text().splitlines()], [0.01])

    def test_showrunner_poll_default_remains_two_seconds(self) -> None:
        environment = {key: value for key, value in self.env.items() if key != "ADD_SHOWRUNNER_POLL_S"}
        program = f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import add_showrunner; print(add_showrunner.POLL_S)"
        result = subprocess.run([sys.executable, "-c", program], env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual((result.returncode, result.stdout), (0, "2.0\n"), result.stderr)

    def test_a_registry_still_unreadable_at_the_deadline_leaves_the_child_unattached(self) -> None:
        self.flaky_registry(1000)
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid is not attached: cannot tell whether session:kid-session is running: "
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
        notes = self.root / "notes.md"
        _ = notes.write_text("# notes\n", encoding="utf-8")
        not_a_doc = self.invoke(["--production", str(notes), "--model", "opus", "--effort", "high"])
        self.assertEqual(not_a_doc.returncode, 2, not_a_doc.stdout + not_a_doc.stderr)
        self.assertIn("add_showrunner: ", not_a_doc.stderr)
        self.refused(self.run_command("top"), "top is this session's own production")
        self.timer("running", "running-session")
        self.live("running-session")
        self.refused(self.run_command("running"), "running already has a running showrunner")
        (self.state / "live-agents").touch()
        self.refused(self.run_command("kid"), "a tab named kid is already open in herdr session agents")

    def test_a_production_above_the_caller_is_refused(self) -> None:
        self.timer("middle", "middle-session")
        self.timer("grand", "grand-session")
        grand = staff.ensure("showrunner", name="grand")
        middle = staff.ensure("showrunner", name="middle", parent=grand.id)
        top = next(row for row in staff.find(kind="showrunner") if row.name == "top")
        _ = staff.move(top.id, middle.id)
        for child, parent in (("top", "middle"), ("middle", "grand")):
            edge = self.root / "showrunner" / child / "parent"
            edge.parent.mkdir(parents=True)
            _ = edge.write_text(parent + "\n", encoding="utf-8")
        self.refused(self.run_command("grand"), "top already reports to grand, directly or through others")

    def test_an_untrusted_start_folder_is_refused_naming_it_and_nothing_from_the_config(self) -> None:
        checkout = self.root / "kid-checkout"
        _ = self.doc("kid")
        self.trust(self.root, extra={"oauthAccount": {"emailAddress": "SECRET-EMAIL"}, "userID": "SECRET-ID"})
        result = self.run_command("kid", "--check")
        self.refused(result, f"kid: Claude is not trusted in {checkout.resolve()} on claude2. "
                     + f"Run: cd {shlex.quote(str(checkout.resolve()))} && "
                     + f"CLAUDE_CONFIG_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude2'))} "
                     + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude'))} claude")
        self.assertNotIn("SECRET", result.stdout + result.stderr)
        self.assertEqual(self.herdr_calls(), [])

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
                result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(folder), "--account", "claude2", "--check")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_trusted_parent_folder_does_not_cover_a_repository_below_it(self) -> None:
        _ = self.doc("kid")
        _ = subprocess.run(["git", "init", "-q", str(self.root / "kid-checkout")], check=True, capture_output=True)
        self.trust(self.root)
        self.refused(self.run_command("kid", "--check"), "kid: Claude is not trusted in")

    def test_an_unreadable_config_refuses_without_its_contents(self) -> None:
        _ = (self.root / "claude.json").write_text('{"oauthAccount": "SECRET-EMAIL", ', encoding="utf-8")
        result = self.run_command("kid", "--check")
        self.refused(result, f"kid: cannot read {self.root / 'claude.json'} to check Claude trust (JSONDecodeError)")
        self.assertNotIn("SECRET", result.stderr)
        self.assertEqual(self.herdr_calls(), [])

    def test_a_session_that_runs_no_production_is_refused(self) -> None:
        self.env["CLAUDE_CODE_SESSION_ID"] = "no-such-session"
        self.refused(self.run_command("kid"), "this session runs no production, so it has nothing to attach kid to")


if __name__ == "__main__":
    _ = unittest.main()
