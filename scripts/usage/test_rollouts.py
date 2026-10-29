"""Fixture tests for the shipped standard-library Codex rollout parser."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.usage import rollouts


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

def record(record_type: str, at: str, payload: dict[str, object]) -> str:
    return json.dumps({"timestamp": at, "type": record_type, "payload": payload})


def session(at: str) -> str:
    return record("session_meta", at, {"timestamp": at})


def tokens(at: str, input_tokens: int, cached: int, output: int) -> str:
    return record("event_msg", at, {
        "type": "token_count",
        "info": {"last_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
        }},
    })


def inspection(at: str) -> str:
    return record("response_item", at, {
        "type": "custom_tool_call",
        "name": "exec",
        "input": 'const r = await tools.exec_command({cmd: "rg -F \'tools.apply_patch(\' scripts"});',
    })


def exec_edit(at: str) -> str:
    return record("response_item", at, {
        "type": "custom_tool_call",
        "name": "exec",
        "input": (
            'const patch = "*** Begin Patch\\n*** Update File: sample.py"; '
            "const result = await tools.apply_patch(patch); text(result);"
        ),
    })


def template_expression_edit(at: str) -> str:
    return record("response_item", at, {
        "type": "custom_tool_call",
        "name": "exec",
        "input": "const result = `${await tools.apply_patch(patch)}`;",
    })


class RolloutTests(unittest.TestCase):
    def builder_from(self, lines: tuple[str, ...]) -> rollouts.ThreadRolloutBuilder:
        builder = rollouts.ThreadRolloutBuilder()
        for line in lines:
            value = rollouts.decode_rollout(line)
            assert value is not None
            builder.consume(value)
        return builder

    def test_quoted_apply_patch_call_mention_is_not_an_edit(self) -> None:
        thread = self.builder_from((
            session("2026-01-01T00:00:00Z"),
            inspection("2026-01-01T00:01:00Z"),
            tokens("2026-01-01T00:02:00Z", 20, 0, 0),
        ))
        self.assertFalse(thread.edit_seen)
        self.assertEqual([sample.before_first_edit for sample in thread.usage], [True])
        self.assertEqual(thread.edits, [])

    def test_rollout_builder_keeps_usage_opening_and_compaction_lifecycle(self) -> None:
        builder = rollouts.ThreadRolloutBuilder()
        for line in (session("2026-01-01T00:00:00Z"), tokens("2026-01-01T01:00:00Z", 100, 20, 10),
                     record("compacted", "2026-01-01T02:00:00Z", {})):
            value = rollouts.decode_rollout(line)
            assert value is not None
            builder.consume(value)
        thread = builder
        self.assertEqual(thread.opening, rollouts.KnownThreadOpening(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(thread.usage[0].usage, rollouts.TokenUsage(80, 20, 10))
        self.assertEqual(thread.compactions, [datetime(2026, 1, 1, 2, tzinfo=timezone.utc)])

    def test_real_exec_apply_patch_call_marks_the_edit(self) -> None:
        thread = self.builder_from((
            session("2026-01-01T00:00:00Z"),
            inspection("2026-01-01T00:01:00Z"),
            tokens("2026-01-01T00:02:00Z", 20, 0, 0),
            exec_edit("2026-01-01T00:03:00Z"),
            tokens("2026-01-01T00:04:00Z", 10, 0, 0),
        ))
        self.assertTrue(thread.edit_seen)
        self.assertEqual([sample.before_first_edit for sample in thread.usage], [True, False])
        self.assertEqual(thread.edits, [datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc)])

    def test_apply_patch_inside_template_expression_marks_the_edit(self) -> None:
        thread = self.builder_from((
            session("2026-01-01T00:00:00Z"),
            template_expression_edit("2026-01-01T00:01:00Z"),
        ))
        self.assertTrue(thread.edit_seen)
        self.assertEqual(
            thread.edits,
            [datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)],
        )

    def test_module_import_needs_only_the_standard_library(self) -> None:
        script = """
import importlib.util
import sys
sys.path[:] = [path for path in sys.path if path and 'site-packages' not in path]
assert not any(name == 'scripts' or name.startswith('scripts.') for name in sys.modules)
spec = importlib.util.spec_from_file_location('fixture_rollouts', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert not any(name == 'scripts' or name.startswith('scripts.') for name in sys.modules)
assert hasattr(module, 'ThreadRolloutBuilder')
assert hasattr(module, 'KnownThreadOpening')
"""
        environment = os.environ.copy()
        environment.update(PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(Path(rollouts.__file__))],
                                    env=environment, cwd=directory, capture_output=True,
                                    text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")


if __name__ == "__main__":
    _ = unittest.main()
