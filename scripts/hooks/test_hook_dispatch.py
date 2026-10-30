"""Compare dispatch with separate hooks and exercise hook isolation and merging."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, cast, override
from unittest.mock import patch


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

HOOKS = Path(__file__).resolve().parent
DISPATCH = HOOKS / "hook_dispatch.py"
ROOT = HOOKS.parents[1]
# The registrations before dispatch was introduced: order, matcher, timeout.
LEGACY: dict[str, tuple[tuple[str, str, float], ...]] = {
    "pre_tool_use": (
        ("pre-tool-use-heredoc-guard.py", "Bash", 600),
        ("pre-tool-use-tool-output-read.py", "Bash", 600),
        ("pre-tool-use-showrunner-move-guard.py", "Bash", 600),
        ("pre-tool-use-change-review-gate.py", "Bash", 600),
        ("pre-tool-use-checkpoint-doc-gate.py", "Bash", 600),
        ("pre-tool-use-scratch-guard.py", "Bash|Write|NotebookEdit", 10),
    ),
    "post_tool_use": (
        ("post-tool-use-basedpyright.py", "Edit|MultiEdit|Write", 600),
        ("post-tool-use-fn-length.py", "Edit|MultiEdit|Write", 600),
        ("post-tool-use-context-usage.py", "", 600),
        ("post-tool-use-banned-words.py", "", 600),
    ),
    "stop": (
        ("stop-direct-continue.py", "", 600),
        ("stop-conversation-pause.py", "", 600),
        ("stop-assistant-prose-banned-words.py", "", 600),
        ("herdr_tab_title.py", "", 600),
    ),
}
GUIDE = '---\ntier: B\n---\n## Forbidden words\n\n### "zorblax"\n\nUse a precise description.\n'


def object_fields(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return cast(dict[str, object], value)


def parsed_output(stdout: str) -> dict[str, object]:
    return object_fields(cast(object, json.loads(stdout))) if stdout.strip() else {}


class HookOutcome(NamedTuple):
    filename: str
    code: int
    stdout: str
    stderr: str


class EventOutcome(NamedTuple):
    code: int
    output: dict[str, object]
    stderr: str


def counter_snapshot(directory: Path) -> tuple[dict[str, object], list[str]]:
    path = directory / "forbidden-word-counts.json"
    if not path.exists():
        return {}, []
    records = parsed_output(path.read_text(encoding="utf-8"))
    timestamps: list[str] = []
    for value in object_fields(records["words"]).values():
        fields = object_fields(value)
        timestamp = fields.pop("last_triggered_at", None)
        if isinstance(timestamp, str):
            timestamps.append(timestamp)
    return records, timestamps


def counter_time_independent(outcome: EventOutcome, timestamps: list[str]) -> EventOutcome:
    text = json.dumps(outcome.output)
    for timestamp in timestamps:
        text = text.replace(f"(last {timestamp})", "(last <counter timestamp>)")
    return EventOutcome(outcome.code, parsed_output(text), outcome.stderr)


def old_hook_combination(outcomes: Sequence[HookOutcome]) -> EventOutcome:
    """Independent oracle for the effect of separately registered hooks."""
    refused = [result.stderr.rstrip("\n") for result in outcomes if result.code == 2]
    if refused:
        return EventOutcome(2, {}, "\n\n".join(refused))
    documents: list[dict[str, object]] = []
    for result in outcomes:
        try:
            document = parsed_output(result.stdout) if result.code == 0 else {}
        except (ValueError, AssertionError):
            document = {}
            invalid = True
        else:
            invalid = False
        if result.code != 0 or invalid:
            detail = result.stderr.strip() or f"exit {result.code}"
            document = cast(dict[str, object], {"systemMessage": f"{result.filename} hook error: {detail}"})
        if document:
            documents.append(document)
    merged: dict[str, object] = {}
    specifics = [object_fields(doc["hookSpecificOutput"]) for doc in documents if "hookSpecificOutput" in doc]
    for source, target in ((documents, merged), (specifics, {})):
        for document in source:
            for key, value in document.items():
                _ = target.setdefault(key, value)
        for key, separator in (("additionalContext", "\n\n"), ("systemMessage", "\n")):
            values = [str(doc[key]) for doc in source if key in doc]
            if values:
                target[key] = separator.join(values)
        if source is specifics and specifics:
            ranks = {"allow": 0, "ask": 1, "defer": 2, "deny": 3}
            decisions = [doc for doc in source if "permissionDecision" in doc]
            if decisions:
                winner = max(decisions, key=lambda doc: ranks[str(doc["permissionDecision"])])["permissionDecision"]
                target["permissionDecision"] = winner
                target["permissionDecisionReason"] = "\n\n".join(
                    str(doc["permissionDecisionReason"]) for doc in decisions
                    if doc["permissionDecision"] == winner and "permissionDecisionReason" in doc
                )
            merged["hookSpecificOutput"] = target
    stopped = [doc for doc in documents if doc.get("continue") is False]
    if stopped:
        merged["continue"] = False
        merged["stopReason"] = "\n".join(str(doc["stopReason"]) for doc in stopped if "stopReason" in doc)
    blocked = [doc for doc in documents if doc.get("decision") == "block"]
    if blocked:
        merged["decision"] = "block"
        merged["reason"] = "\n\n".join(str(doc["reason"]) for doc in blocked if "reason" in doc)
    return EventOutcome(0, merged, "")


class IsolatedHookTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        guide = self.home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text(GUIDE, encoding="utf-8")
        temp = self.root / "temp"
        temp.mkdir()
        # Start afresh: never inherit a director's active run, state, pane or log paths.
        self.environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        self.environment.update({
            "HOME": str(self.home), "TMPDIR": str(temp), "USER_ZONE": "UTC",
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "CLAUDE_CONTEXT_HOOK_DEBUG": "0", "CLAUDE_CONTEXT_HOOK_MODE": "dynamic",
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
            "FN_LENGTH_HOOK_STATE": str(self.root / "fn-length"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def run_python(self, arguments: list[str], payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *arguments], input=json.dumps(payload),
                              text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                              env=self.environment, cwd=self.root)

    def event_outcome(self, result: subprocess.CompletedProcess[str]) -> EventOutcome:
        return EventOutcome(result.returncode, parsed_output(result.stdout), result.stderr)


class RealHookEquivalenceTests(IsolatedHookTests):
    def compare(self, event: str, payload: dict[str, object]) -> EventOutcome:
        state = self.home / ".claude/state"
        legacy_state = self.root / "legacy-counter-state"
        for directory in (state, legacy_state):
            if directory.exists():
                shutil.rmtree(directory)
        results: list[HookOutcome] = []
        for filename, matcher, _timeout in LEGACY[event]:
            if matcher and payload.get("tool_name") not in matcher.split("|"):
                continue
            result = self.run_python([str(HOOKS / filename)], payload)
            self.assertEqual(result.returncode, 0, f"{filename}: {result.stderr}")
            results.append(HookOutcome(filename, result.returncode, result.stdout, result.stderr))
        expected = old_hook_combination(results)
        if state.exists():
            _ = state.rename(legacy_state)
        actual = self.run_python([str(DISPATCH), event], payload)
        # Both runs start from empty, separate counter files at the same hook
        # path. Counts and all output stay comparable across a second boundary.
        expected_counts, expected_times = counter_snapshot(legacy_state)
        actual_counts, actual_times = counter_snapshot(state)
        self.assertEqual(actual_counts, expected_counts)
        self.assertEqual(
            counter_time_independent(self.event_outcome(actual), actual_times),
            counter_time_independent(expected, expected_times),
        )
        if not expected.output:
            self.assertEqual(actual.stdout, "")
        return expected

    def test_counter_equivalence_across_a_second_keeps_counts_decisions_and_text(self) -> None:
        payload: dict[str, object] = {"tool_name": "Write"}
        for change in ("", "count", "decision", "text", "exit"):
            with self.subTest(change=change):
                def simulated(arguments: list[str], supplied: dict[str, object]) -> subprocess.CompletedProcess[str]:
                    self.assertEqual(supplied, payload)
                    dispatch = arguments[0] == str(DISPATCH)
                    if not dispatch and not arguments[0].endswith("post-tool-use-banned-words.py"):
                        return subprocess.CompletedProcess(arguments, 0, "", "")
                    timestamp = "2026-10-28 12:00:01 UTC" if dispatch else "2026-10-28 12:00:00 UTC"
                    count = 2 if dispatch and change == "count" else 1
                    state = self.home / ".claude/state/forbidden-word-counts.json"
                    state.parent.mkdir(parents=True, exist_ok=True)
                    _ = state.write_text(json.dumps({"version": 1, "words": {
                        "zorblax": {"count": count, "last_triggered_at": timestamp},
                    }}))
                    decision = "allow" if dispatch and change == "decision" else "block"
                    text = "changed" if dispatch and change == "text" else "unchanged"
                    output = {"decision": decision, "reason": f"{text}: zorblax={count} (last {timestamp})"}
                    code = 2 if dispatch and change == "exit" else 0
                    return subprocess.CompletedProcess(arguments, code, json.dumps(output), "")

                with patch.object(self, "run_python", side_effect=simulated):
                    if change:
                        with self.assertRaises(AssertionError):
                            _ = self.compare("post_tool_use", payload)
                    else:
                        _ = self.compare("post_tool_use", payload)

    def test_pre_tool_use_matches_separate_hooks(self) -> None:
        cases = (
            ("allows", {"command": "printf ready"}, ""),
            ("heredoc", {"command": "cat <<EOF\n$(date)\nEOF"}, "unquoted heredoc"),
            ("tool-output-read", {
                "command": f"awk 'NR > 2' {self.home}/.claude/projects/project/tool-results/result.txt",
            }, "this awk read of saved tool output"),
            ("scratch", {"command": f"mkdir {self.home}/rust/loose"}, "new loose folder"),
            ("checkpoint", {"command": 'git commit -m "checkpoint(test): hooks"'}, "Change-Review"),
        )
        for label, tool_input, reason in cases:
            with self.subTest(case=label):
                expected = self.compare("pre_tool_use", {
                    "tool_name": "Bash", "tool_input": tool_input, "cwd": str(self.root),
                })
                if reason:
                    decision = object_fields(expected.output["hookSpecificOutput"])
                    self.assertEqual(decision["permissionDecision"], "deny")
                    self.assertIn(reason, str(decision["permissionDecisionReason"]))
                else:
                    self.assertEqual(expected.output, {})

    def test_write_runs_only_scratch_guard(self) -> None:
        outcome = self.compare("pre_tool_use", {
            "tool_name": "Write", "cwd": str(self.root),
            "tool_input": {"file_path": str(self.home / "rust/loose/file.txt"), "content": "ready"},
        })
        self.assertEqual(object_fields(outcome.output["hookSpecificOutput"])["permissionDecision"], "deny")

    def test_post_tool_use_matches_separate_hooks(self) -> None:
        for tool, tool_input, tool_response in (
            ("Read", {"file_path": str(self.root / "notes.txt")}, {"content": "zorblax"}),
            ("Bash", {"command": "printf ready"}, {"stdout": "ready"}),
            ("Write", {"file_path": str(self.root / "notes.txt"), "content": "A zorblax claim."}, {}),
        ):
            with self.subTest(tool=tool):
                outcome = self.compare("post_tool_use", {
                    "tool_name": tool, "tool_input": tool_input, "tool_response": tool_response,
                    "cwd": str(self.root),
                })
                if tool == "Write":
                    self.assertEqual(outcome.output["decision"], "block")
                else:
                    self.assertEqual(outcome.output, {})

    def test_stop_direct_continue_blocks_active_run_near_compaction(self) -> None:
        session_id = "dispatcher-active-director"
        run = self.root / "unit-run"
        run.mkdir()
        _ = (run / "director").write_text(session_id + "\n", encoding="utf-8")
        markers = Path(self.environment["UNIT_ACTIVE_DIR"])
        markers.mkdir()
        _ = (markers / session_id).write_text(str(run) + "\n", encoding="utf-8")
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        _ = settings.write_text(json.dumps({"autoCompactWindow": 200_000}), encoding="utf-8")
        transcript = self.root / "active-transcript.jsonl"
        _ = transcript.write_text(json.dumps({
            "type": "assistant", "message": {
                "role": "assistant", "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "The checks passed."}],
                "usage": {"input_tokens": 170_000},
            },
        }) + "\n", encoding="utf-8")

        outcome = self.compare("stop", {
            "session_id": session_id, "transcript_path": str(transcript),
            "last_assistant_message": "The checks passed.",
        })
        self.assertEqual(outcome.output["decision"], "block")
        self.assertIn("/unit:direct run active, 170,000", str(outcome.output["reason"]))

    def test_stop_matches_separate_hooks_and_logs_last_assistant_prose(self) -> None:
        transcript = self.root / "transcript.jsonl"
        for message in ("The checks passed.", "A zorblax claim."):
            with self.subTest(message=message):
                _ = transcript.write_text(json.dumps({
                    "type": "assistant", "message": {"role": "assistant", "content": [
                        {"type": "text", "text": message},
                    ]},
                }) + "\n", encoding="utf-8")
                _ = self.compare("stop", {
                    "session_id": "dispatcher-test", "transcript_path": str(transcript),
                    "last_assistant_message": message,
                })
                log = self.home / ".claude/state/forbidden-word-prose.log"
                self.assertEqual(log.exists(), "zorblax" in message)
                if log.exists():
                    self.assertIn("\tdispatcher-test\tzorblax\t", log.read_text(encoding="utf-8"))


class FakeHook(NamedTuple):
    source: str
    timeout: float = 1
    matcher: str = ""


def emit(document: dict[str, object]) -> str:
    return f"import json\nprint(json.dumps({document!r}))\n"


class FakeHookTests(IsolatedHookTests):
    def fake_files(self, hooks: Sequence[FakeHook]) -> list[tuple[str, str, float]]:
        rows: list[tuple[str, str, float]] = []
        for index, hook in enumerate(hooks):
            path = self.root / f"fake-{index}.py"
            script = (
                "def main():\n" + textwrap.indent(hook.source, "    ")
                + "\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n"
            )
            _ = path.write_text(script, encoding="utf-8")
            rows.append((str(path), hook.matcher, hook.timeout))
        return rows

    def run_fakes(self, event: str, hooks: Sequence[FakeHook], tool: str = "Bash") -> EventOutcome:
        rows = self.fake_files(hooks)
        driver = (
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            + "from scripts.hooks import hook_dispatch as dispatch\n"
            + f"dispatch.EVENTS[{event!r}] = tuple(dispatch.HookSpec(filename, matcher or None, timeout) "
            + f"for filename, matcher, timeout in {rows!r})\n"
            + f"sys.argv = [{str(DISPATCH)!r}, {event!r}]\ndispatch.main()\n"
        )
        result = self.run_python(["-c", driver], {"tool_name": tool})
        outcome = self.event_outcome(result)
        if outcome.code == 0 and not outcome.output:
            self.assertEqual(result.stdout, "")
        return outcome

    def test_two_exit_two_hooks_join_stderr_in_table_order(self) -> None:
        outcome = self.run_fakes("pre_tool_use", [
            FakeHook("import sys\nprint('first refusal', file=sys.stderr)\nraise SystemExit(2)"),
            FakeHook(emit({"systemMessage": "discarded"})),
            FakeHook("import sys\nprint('second refusal', file=sys.stderr)\nraise SystemExit(2)"),
        ])
        self.assertEqual(outcome, EventOutcome(2, {}, "first refusal\n\nsecond refusal\n"))

    def test_permission_precedence_and_only_winning_reasons(self) -> None:
        for winner, decisions in (
            ("deny", ("ask", "deny", "allow", "defer", "deny")),
            ("defer", ("ask", "defer", "allow", "defer")),
            ("ask", ("allow", "ask", "ask")),
            ("allow", ("allow", "allow")),
        ):
            with self.subTest(winner=winner):
                outcome = self.run_fakes("pre_tool_use", [
                    FakeHook(emit({"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": decision,
                        "permissionDecisionReason": f"reason {index}",
                    }})) for index, decision in enumerate(decisions)
                ])
                self.assertEqual(outcome.code, 0)
                self.assertEqual(outcome.stderr, "")
                specific = object_fields(outcome.output["hookSpecificOutput"])
                self.assertEqual(specific["permissionDecision"], winner)
                self.assertEqual(specific["permissionDecisionReason"], "\n\n".join(
                    f"reason {index}" for index, decision in enumerate(decisions) if decision == winner
                ))

    def test_context_messages_and_other_keys_keep_order(self) -> None:
        outcome = self.run_fakes("post_tool_use", [
            FakeHook(emit({"systemMessage": "first", "custom": "kept", "hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": "context one", "custom": 1,
            }})),
            FakeHook(emit({"systemMessage": "second", "custom": "lost", "hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": "context two", "custom": 2,
            }})),
        ])
        self.assertEqual(outcome, EventOutcome(0, {
            "systemMessage": "first\nsecond", "custom": "kept", "hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": "context one\n\ncontext two", "custom": 1,
            },
        }, ""))

    def test_stop_blocks_join_only_blocking_reasons(self) -> None:
        outcome = self.run_fakes("stop", [
            FakeHook(emit({"decision": "approve", "reason": "ignored"})),
            FakeHook(emit({"decision": "block", "reason": "first"})),
            FakeHook(emit({"decision": "block", "reason": "second"})),
        ])
        self.assertEqual(outcome, EventOutcome(0, {"decision": "block", "reason": "first\n\nsecond"}, ""))

    def test_continue_false_wins_and_joins_its_reasons(self) -> None:
        outcome = self.run_fakes("stop", [
            FakeHook(emit({"continue": True, "stopReason": "ignored"})),
            FakeHook(emit({"continue": False, "stopReason": "first"})),
            FakeHook(emit({"continue": False, "stopReason": "second"})),
        ])
        self.assertEqual(outcome, EventOutcome(0, {"continue": False, "stopReason": "first\nsecond"}, ""))

    def test_crash_exit_three_and_invalid_json_do_not_stop_later_hooks(self) -> None:
        for source, detail in (
            ("raise RuntimeError('fixture fault')", "RuntimeError: fixture fault"),
            ("raise SystemExit(3)", "exit 3"),
            ("print('not JSON')", "exit 0"),
            ("import sys\nprint('first line\\nsecond line', file=sys.stderr)\nraise SystemExit(3)",
             "first line\nsecond line"),
        ):
            with self.subTest(source=source):
                outcome = self.run_fakes("post_tool_use", [
                    FakeHook(source), FakeHook(emit({"systemMessage": "later hook ran"})),
                ])
                if detail == "RuntimeError: fixture fault":
                    self.assertEqual((outcome.code, outcome.stderr), (0, ""))
                    self.assertEqual(set(outcome.output), {"systemMessage"})
                    message = str(outcome.output["systemMessage"])
                    self.assertTrue(message.startswith("fake-0.py hook error: Traceback (most recent call last):\n"))
                    self.assertTrue(message.endswith(f"{detail}\nlater hook ran"))
                else:
                    self.assertEqual(outcome, EventOutcome(0, {
                        "systemMessage": f"fake-0.py hook error: {detail}\nlater hook ran",
                    }, "not JSON\n" if detail == "exit 0" else ""))

    def test_timeout_discards_partial_output_and_runs_later_hook(self) -> None:
        outcome = self.run_fakes("post_tool_use", [
            FakeHook("import time\nprint('{\"custom\": \"discarded\"}', flush=True)\ntime.sleep(0.5)", 0.04),
            FakeHook(emit({"custom": "later", "systemMessage": "later hook ran"})),
        ])
        self.assertEqual(outcome, EventOutcome(0, {"custom": "later", "systemMessage": "later hook ran"}, ""))

    def test_hook_can_install_and_receive_its_own_alarm(self) -> None:
        outcome = self.run_fakes("stop", [FakeHook(
            "import json, signal, time\n"
            + "def alarm(signum, frame):\n    print(json.dumps({'alarm_received': True}))\n"
            + "signal.signal(signal.SIGALRM, alarm)\n"
            + "signal.setitimer(signal.ITIMER_REAL, 0.02)\ntime.sleep(0.06)\n"
        )])
        self.assertEqual(outcome, EventOutcome(0, {"alarm_received": True}, ""))

    def test_process_environment_argv_and_directory_are_restored(self) -> None:
        self.environment["DISPATCH_FIXTURE"] = "original"
        source = (
            "import os, sys\nos.environ.clear()\nos.environ['DISPATCH_FIXTURE'] = 'changed'\n"
            + f"os.chdir({str(self.home)!r})\nsys.argv[:] = ['changed', 'extra']\n"
        )
        inspect = (
            "import json, os, sys\nprint(json.dumps({'environment': os.environ['DISPATCH_FIXTURE'], "
            + "'cwd': os.getcwd(), 'argv': sys.argv}))\n"
        )
        outcome = self.run_fakes("stop", [FakeHook(source), FakeHook(inspect)])
        self.assertEqual(outcome, EventOutcome(0, {
            "environment": "original", "cwd": str(self.root), "argv": [str(self.root / "fake-1.py")],
        }, ""))

    def test_subprocess_stdout_is_captured_with_its_hook(self) -> None:
        child = emit({"systemMessage": "from child"})
        outcome = self.run_fakes("stop", [FakeHook(
            f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
        ), FakeHook(emit({"systemMessage": "later"}))])
        self.assertEqual(outcome, EventOutcome(0, {"systemMessage": "from child\nlater"}, ""))

    def test_matchers_skip_other_tools_and_preserve_table_order(self) -> None:
        outcome = self.run_fakes("pre_tool_use", [
            FakeHook(emit({"systemMessage": "bash only"}), matcher="Bash"),
            FakeHook(emit({"systemMessage": "write"}), matcher="Bash|Write|NotebookEdit"),
            FakeHook(emit({"systemMessage": "all"})),
        ], tool="Write")
        self.assertEqual(outcome, EventOutcome(0, {"systemMessage": "write\nall"}, ""))

    def test_empty_hooks_emit_no_json(self) -> None:
        self.assertEqual(self.run_fakes("stop", [FakeHook("pass")]), EventOutcome(0, {}, ""))


class SettingsRegistrationTests(IsolatedHookTests):
    def test_push_delivery_hook_runs_only_for_push_notifications(self) -> None:
        driver = (
            f"import sys, json\nsys.path.insert(0, {str(ROOT)!r})\n"
            + "from scripts.hooks import hook_dispatch as dispatch\n"
            + "calls = []\n"
            + "from pathlib import Path\n"
            + "def run_hook(path: Path, payload: str, timeout: float) -> dispatch.HookResult:\n"
            + "    calls.append(path.name)\n"
            + "    return dispatch.HookResult(path.name, 0, '', '')\n"
            + "dispatch.run_hook = run_hook\n"
            + "sys.argv = ['hook_dispatch.py', 'post_tool_use']\n"
            + "try: dispatch.main()\n"
            + "except SystemExit as result: assert result.code == 0\n"
            + "print(json.dumps({'calls': calls}))\n"
        )
        for tool, expected in (("PushNotification", 1), ("Bash", 0)):
            with self.subTest(tool=tool):
                result = self.run_python(["-c", driver], {"tool_name": tool})
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                calls = parsed_output(result.stdout)["calls"]
                self.assertIsInstance(calls, list)
                self.assertEqual(cast(list[object], calls).count("post-tool-use-push-delivered.py"), expected)

    def test_dispatcher_outer_timeouts_cover_all_hooks_and_overhead(self) -> None:
        driver = (
            f"import sys, json, re\nsys.path.insert(0, {str(ROOT)!r})\n"
            + "from scripts.hooks import hook_dispatch as dispatch\n"
            + "tools = ('Bash', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'PushNotification', '')\n"
            + "print(json.dumps({event: max(sum(spec.timeout for spec in table "
            + "if spec.matcher is None or re.fullmatch(spec.matcher, tool)) for tool in tools) "
            + "+ dispatch.DISPATCH_OVERHEAD_SECONDS for event, table in dispatch.EVENTS.items()}))\n"
        )
        result = self.run_python(["-c", driver], {})
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        required_timeouts = parsed_output(result.stdout)
        settings = object_fields(cast(object, json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))))
        registrations = object_fields(settings["hooks"])
        for event, settings_event in (
            ("pre_tool_use", "PreToolUse"), ("post_tool_use", "PostToolUse"), ("stop", "Stop"),
        ):
            with self.subTest(event=event):
                dispatchers: list[dict[str, object]] = []
                for group_value in cast(list[object], registrations[settings_event]):
                    group = object_fields(group_value)
                    for hook_value in cast(list[object], group["hooks"]):
                        hook = object_fields(hook_value)
                        arguments = shlex.split(str(hook.get("command", "")))
                        if any(Path(argument).name == "hook_dispatch.py" for argument in arguments):
                            dispatchers.append(hook)
                self.assertEqual(len(dispatchers), 1)
                self.assertIn("timeout", dispatchers[0])
                self.assertEqual(dispatchers[0]["timeout"], required_timeouts[event])

    def test_dispatch_tables_equal_legacy_files_matchers_and_timeouts(self) -> None:
        driver = (
            f"import sys, json\nsys.path.insert(0, {str(ROOT)!r})\n"
            + "from scripts.hooks import hook_dispatch as dispatch\n"
            + "tables = {'pre_tool_use': dispatch.PRE_TOOL_USE, 'post_tool_use': dispatch.POST_TOOL_USE, "
            + "'stop': dispatch.STOP}\n"
            + "assert dispatch.EVENTS == tables\n"
            + "print(json.dumps({event: [[spec.filename, spec.matcher or '', spec.timeout] "
            + "for spec in table] for event, table in tables.items()}))\n"
        )
        result = self.run_python(["-c", driver], {})
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        expected = {
            event: [list(row) for row in rows] for event, rows in LEGACY.items()
        }
        expected["post_tool_use"].append(["post-tool-use-push-delivered.py", "PushNotification", 600])
        self.assertEqual(parsed_output(result.stdout), expected)

    def test_each_event_registers_dispatch_once_and_removes_old_python_commands(self) -> None:
        settings = object_fields(cast(object, json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))))
        registrations = object_fields(settings["hooks"])
        for event, settings_event, matcher in (
            ("pre_tool_use", "PreToolUse", "Bash|Write|NotebookEdit"),
            ("post_tool_use", "PostToolUse", ""),
            ("stop", "Stop", ""),
        ):
            with self.subTest(event=event):
                groups = cast(list[object], registrations[settings_event])
                dispatchers: list[dict[str, object]] = []
                for group_value in groups:
                    group = object_fields(group_value)
                    for hook_value in cast(list[object], group["hooks"]):
                        hook = object_fields(hook_value)
                        command = str(hook.get("command", ""))
                        for filename, _matcher, _timeout in LEGACY[event]:
                            self.assertNotIn(filename, command)
                        arguments = shlex.split(command)
                        if any(Path(argument).name == "hook_dispatch.py" for argument in arguments):
                            self.assertEqual(Path(arguments[-2]).name, "hook_dispatch.py")
                            self.assertEqual(arguments[-1], event)
                            self.assertEqual(group.get("matcher", ""), matcher)
                            dispatchers.append(hook)
                self.assertEqual(len(dispatchers), 1)


class DispatcherRepairTests(IsolatedHookTests):
    def run_repair_fakes(self, event: str, hooks: Sequence[FakeHook]) -> EventOutcome:
        rows: list[tuple[str, float]] = []
        for index, hook in enumerate(hooks):
            path = self.root / f"repair-{index}.py"
            _ = path.write_text(hook.source + "\n", encoding="utf-8")
            rows.append((str(path), hook.timeout))
        driver = (
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            + "from scripts.hooks import hook_dispatch as dispatch\n"
            + f"dispatch.EVENTS[{event!r}] = tuple(dispatch.HookSpec(filename, None, timeout) "
            + f"for filename, timeout in {rows!r})\n"
            + f"sys.argv = [{str(DISPATCH)!r}, {event!r}]\ndispatch.main()\n"
        )
        return self.event_outcome(self.run_python(["-c", driver], {}))

    def test_f001_malformed_common_fields_cannot_erase_valid_block(self) -> None:
        malformed: tuple[dict[str, object], ...] = (
            {"continue": "bad"}, {"continue": 0}, {"suppressOutput": []},
            {"additionalContext": []}, {"systemMessage": False}, {"stopReason": []},
            {"reason": {}}, {"terminalSequence": 1}, {"decision": []},
            {"decision": "invalid"}, {"hookSpecificOutput": []},
            {"hookSpecificOutput": {}},
        )
        for fields in malformed:
            with self.subTest(fields=fields):
                outcome = self.run_repair_fakes("stop", [
                    FakeHook(emit({"systemMessage": "discard this hook", **fields})),
                    FakeHook(emit({"decision": "block", "reason": "valid block"})),
                ])
                self.assertEqual(outcome, EventOutcome(0, {
                    "systemMessage": "repair-0.py hook error: exit 0",
                    "decision": "block", "reason": "valid block",
                }, ""))

    def test_f001_malformed_specific_fields_cannot_erase_valid_deny(self) -> None:
        malformed: tuple[dict[str, object], ...] = (
            {"permissionDecision": []}, {"permissionDecision": None},
            {"permissionDecision": "invalid"}, {"permissionDecisionReason": []},
            {"updatedInput": []}, {"additionalContext": []},
            {"classifierContext": False}, {"hookEventName": "PostToolUse"},
            {"hookEventName": []},
        )
        valid = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                 "permissionDecisionReason": "valid denial"}
        for fields in malformed:
            with self.subTest(fields=fields):
                outcome = self.run_repair_fakes("pre_tool_use", [
                    FakeHook(emit({"hookSpecificOutput": {**valid, **fields}, "continue": False})),
                    FakeHook(emit({"hookSpecificOutput": valid})),
                ])
                self.assertEqual(outcome, EventOutcome(0, {
                    "systemMessage": "repair-0.py hook error: exit 0",
                    "hookSpecificOutput": valid,
                }, ""))

    def test_f002_closed_streams_retain_output_and_allow_later_hooks(self) -> None:
        for stream in ("stdout", "stderr", "stdin"):
            with self.subTest(stream=stream):
                outcome = self.run_repair_fakes("stop", [
                    FakeHook("import sys\n" + emit({"systemMessage": "before close"})
                             + f"sys.{stream}.close()"),
                    FakeHook(emit({"systemMessage": "later hook ran"})),
                ])
                self.assertEqual(outcome, EventOutcome(0, {
                    "systemMessage": "before close\nlater hook ran",
                }, ""))

    def test_f002_closed_stderr_retains_refusal_and_does_not_stop_cleanup(self) -> None:
        outcome = self.run_repair_fakes("pre_tool_use", [
            FakeHook("import sys\nprint('before close', file=sys.stderr)\n"
                     + "sys.stdout.close()\nsys.stderr.close()\nsys.stdin.close()\nraise SystemExit(2)"),
            FakeHook("import sys\nprint('later refusal', file=sys.stderr)\nraise SystemExit(2)"),
        ])
        self.assertEqual(outcome, EventOutcome(2, {}, "before close\n\nlater refusal\n"))

    def test_f002_closed_stderr_still_captures_crash_and_later_block(self) -> None:
        outcome = self.run_repair_fakes("stop", [
            FakeHook("import sys\nprint('before crash', file=sys.stderr)\n"
                     + "sys.stderr.close()\nraise RuntimeError('closed stderr crash')"),
            FakeHook(emit({"decision": "block", "reason": "later valid block"})),
        ])
        self.assertEqual((outcome.code, outcome.stderr), (0, ""))
        self.assertEqual(outcome.output["decision"], "block")
        self.assertEqual(outcome.output["reason"], "later valid block")
        message = str(outcome.output["systemMessage"])
        self.assertTrue(message.startswith("repair-0.py hook error: before crash\nTraceback"))
        self.assertTrue(message.endswith("RuntimeError: closed stderr crash"))

    def test_f003_non_alarm_signal_handlers_are_restored(self) -> None:
        inspect = (
            "import json, signal\n"
            + "signals = (signal.SIGTERM, signal.SIGUSR1, signal.SIGHUP, signal.SIGPIPE)\n"
            + "handlers = {item.name: int(signal.getsignal(item)) for item in signals}\n"
        )
        outcome = self.run_repair_fakes("stop", [
            FakeHook(inspect + "print(json.dumps({'before': handlers}))\n"
                     + "for item in signals:\n"
                     + "    signal.signal(item, signal.SIG_IGN if handlers[item.name] == signal.SIG_DFL else signal.SIG_DFL)"),
            FakeHook(inspect + "print(json.dumps({'after': handlers}))"),
        ])
        self.assertEqual((outcome.code, outcome.stderr), (0, ""))
        self.assertEqual(outcome.output["before"], outcome.output["after"])

    def test_f004_error_message_retains_whole_stripped_stderr(self) -> None:
        outcome = self.run_repair_fakes("post_tool_use", [
            FakeHook("import sys\nprint('  first line\\nsecond line  ', file=sys.stderr)\nraise SystemExit(3)"),
            FakeHook(emit({"systemMessage": "later hook ran"})),
        ])
        results: list[HookOutcome] = []
        for index in range(2):
            path = self.root / f"repair-{index}.py"
            separate = self.run_python([str(path)], {})
            results.append(HookOutcome(path.name, separate.returncode, separate.stdout, separate.stderr))
        expected = old_hook_combination(results)
        self.assertEqual(expected, EventOutcome(0, {
            "systemMessage": "repair-0.py hook error: first line\nsecond line\nlater hook ran",
        }, ""))
        self.assertEqual(outcome, expected)

    def test_f004_traceback_includes_exception_and_chained_cause(self) -> None:
        outcome = self.run_repair_fakes("post_tool_use", [
            FakeHook("try:\n    raise ValueError('root cause')\n"
                     + "except ValueError as cause:\n    raise RuntimeError('outer failure') from cause"),
            FakeHook(emit({"systemMessage": "later hook ran"})),
        ])
        self.assertEqual((outcome.code, outcome.stderr), (0, ""))
        message = str(outcome.output["systemMessage"])
        self.assertTrue(message.startswith("repair-0.py hook error: Traceback (most recent call last):"))
        self.assertIn("ValueError: root cause", message)
        self.assertIn("The above exception was the direct cause", message)
        self.assertTrue(message.endswith("RuntimeError: outer failure\nlater hook ran"))


if __name__ == "__main__":
    _ = unittest.main()
