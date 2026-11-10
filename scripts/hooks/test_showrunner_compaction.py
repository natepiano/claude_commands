#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Check the showrunner resume text and the absence of PreCompact writes."""

from __future__ import annotations

from scripts.agents import remote

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast, final, override
from unittest.mock import patch
from scripts.lib.file_module import load_file_module
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file

SESSION = fixture_runtime_id("compaction-showrunner")
HOOK = Path(__file__).resolve().parent / "showrunner-compaction.py"
# Record any call to the former PreCompact writer.
FAKE_LOG = """\
import json, os, sys
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
"""


@final
class ShowrunnerCompactionTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.doc = Path()
        self.fake = Path()
        self.calls = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.doc = self.root / "docs" / "example-production.md"
        self.doc.parent.mkdir()
        _ = self.doc.write_text("# Example\n", encoding="utf-8")
        timer = self.root / "notifier" / "showrunner-example"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(
            f"TARGET=session:{SESSION}\nCHECK=zsh /x/production_check.sh example\n", encoding="utf-8")
        self.fake = self.root / "update_registration.py"
        _ = self.fake.write_text(FAKE_LOG, encoding="utf-8")
        self.calls = self.root / "calls.jsonl"
        self.env = {**os.environ, "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNER_UPDATE_REGISTRATION": str(self.fake), "CALLS": str(self.calls)}

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))
        _ = fixture_member("showrunner", name="example", runtime_id=SESSION,
                           doc=staff.LiveDoc(remote.local_machine(), self.doc))

    def run_hook(self, payload: dict[str, str], **env: str) -> str:
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                                text=True, env={**self.env, **env}, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def logged(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def test_precompact_writes_nothing(self) -> None:
        state = self.root / "showrunner" / "example" / "session" / "showrunner_state.json"
        state.parent.mkdir(parents=True)
        _ = state.write_text('{"next": ["merge alpha"]}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.run_hook({"session_id": SESSION, "hook_event_name": "PreCompact",
                                        "trigger": "auto"}), "")
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.logged(), [])

    def test_session_start_after_compaction_names_the_resume(self) -> None:
        output = self.run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "compact"})
        context = cast(dict[str, dict[str, str]], json.loads(output))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"run `/showrunner:produce {self.doc} resume`", context)
        self.assertIn("just compacted", context)
        self.assertIn("current `next` list from the state file. Act on it first.", context)

    def test_any_other_session_or_source_gets_nothing(self) -> None:
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "PreCompact"}), "")
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "SessionStart",
                                        "source": "compact"}), "")
        self.assertEqual(self.run_hook({"session_id": SESSION, "hook_event_name": "SessionStart",
                                        "source": "startup"}), "")
        self.assertEqual(self.logged(), [])


class ContextNoticeTests(unittest.TestCase):
    def test_a_showrunner_is_told_to_fill_next_and_not_to_write_a_handoff(self) -> None:
        module = load_file_module(
            "post_tool_use_context_usage", Path(__file__).resolve().parent / "post-tool-use-context-usage.py")
        spec = module.__spec__
        assert spec is not None and spec.loader is not None
        state = Path("/state/example/session/showrunner_state.json")
        notice = cast(str, module.build_context(249_000, 283_800, False, state))  # pyright: ignore[reportAny]
        self.assertIn("write no handoff doc", notice)
        self.assertIn(f"`next` list of your showrunner state file ({state})", notice)
        ordinary = cast(str, module.build_context(249_000, 283_800, False))  # pyright: ignore[reportAny]
        self.assertIn("write a handoff doc first", ordinary)


class ContextMessageHook(Protocol):
    DEBUG_LOG: Path

    def main(self) -> None: ...


class ContextMessageSwitchTests(unittest.TestCase):
    root: Path = Path()
    transcript: Path = Path()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        module = load_file_module("context_message_switch", HOOK.with_name("post-tool-use-context-usage.py"))
        spec = module.__spec__
        assert spec is not None and spec.loader is not None
        self.hook: ContextMessageHook = cast(ContextMessageHook, cast(object, module))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transcript = self.root / "transcript.jsonl"
        _ = self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 160_000}},
        }) + "\n", encoding="utf-8")
        own = self.transcript.with_suffix("") / "subagents" / "agent-fixture-worker.jsonl"
        own.parent.mkdir(parents=True)
        _ = own.write_bytes(self.transcript.read_bytes())
        self.hook.DEBUG_LOG = self.root / "debug.jsonl"
        self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root), "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k",
            "CLAUDE_CONTEXT_HOOK_MODE": "dynamic", "CLAUDE_CONTEXT_HOOK_DEBUG": "1",
        }))

    def call(self, *, subagent: bool = False) -> str:
        payload = {"transcript_path": str(self.transcript), "session_id": "fixture-session"}
        if subagent:
            payload["agent_id"] = "fixture-worker"
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output):
            self.hook.main()
        return output.getvalue()

    def test_off_keeps_the_reading_and_log_without_building_either_context_message(self) -> None:
        _ = (self.root / "context_usage.conf").write_text("message = off\n", encoding="utf-8")
        with patch.object(self.hook, "showrunner_state") as state, patch.object(self.hook, "build_context") as build:
            for subagent in (False, True):
                with self.subTest(subagent=subagent):
                    self.assertEqual(self.call(subagent=subagent), "")
            state.assert_not_called()
            build.assert_not_called()
        records = [cast(dict[str, object], json.loads(line))
                   for line in self.hook.DEBUG_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["tokens"] for record in records], [160_000, 160_000])
        own = self.transcript.with_suffix("") / "subagents" / "agent-fixture-worker.jsonl"
        self.assertEqual([record["transcript_read"] for record in records], [str(self.transcript), str(own)])

    def test_on_retains_the_main_thread_and_subagent_messages(self) -> None:
        _ = (self.root / "context_usage.conf").write_text("message = on\n", encoding="utf-8")
        with patch.object(self.hook, "showrunner_state", return_value=None):
            self.assertIn("write a handoff doc first", self.call())
            self.assertIn("return your results now", self.call(subagent=True))

    def test_missing_key_file_or_bad_value_keeps_the_old_message(self) -> None:
        path = self.root / "context_usage.conf"
        for content in (None, "other = off\n", "message = invalid\n"):
            with self.subTest(content=content), patch.object(self.hook, "showrunner_state", return_value=None):
                if content is not None:
                    _ = path.write_text(content, encoding="utf-8")
                self.assertIn("write a handoff doc first", self.call())

    def test_the_switch_is_read_again_in_the_same_process(self) -> None:
        path = self.root / "context_usage.conf"
        _ = path.write_text("message = off\n", encoding="utf-8")
        self.assertEqual(self.call(), "")
        _ = path.write_text("message = on\n", encoding="utf-8")
        with patch.object(self.hook, "showrunner_state", return_value=None):
            self.assertIn("write a handoff doc first", self.call())


if __name__ == "__main__":
    _ = unittest.main()
