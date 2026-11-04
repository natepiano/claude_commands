"""Check context deferrals and quoted or descriptive text through the Stop hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import override

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

HOOK = Path(__file__).with_name("stop-context-deferral.py")
CONFIG = HOOK.parents[2] / "config" / "context_deferral.conf"
REASON = "Compaction runs by itself mid-work; do the work now."
TRUE_HITS = (
    "My context is full; I will finish the work later.",
    "My context is nearly full; I'll do the edit later.",
    "Context is running out; we will complete the work later.",
    "I'll finish the edit after the context reset.",
    "We will run the checks after compaction.",
    "We'll continue once the context compacts.",
    "I will write the tests in the next window.",
    "We will finish the work in the next context.",
    "I'll complete the change in a fresh context.",
    "I will finish later because my context is nearly full.",
    "I will finish the change\nafter the context reset.",
    "My context is full. I will finish the work later.",
    "After compaction, I will finish the work.",
    "My context is full; I will tackle the rest later.",
    "My context is full; I will do it after the reset.",
    "I’ll finish the edit after compaction.",
    "MY CONTEXT IS FULL; I'LL DO IT LATER.",
    "- My context is nearly full, so I will finish this later.",
    "Phase 3 is done. My context is nearly full, so I will start phase 4 later.",
)
NEAR_MISSES = (
    "Compaction runs by itself during work. It preserves the task.",
    "The example is `I will do it after the reset.`",
    'The example is "I will do it after the reset."',
    "The example is 'I'll do it after the reset.'",
    "The example is “I will do it after the reset.”",
    "The example is ‘I’ll do the edit after compaction.’",
    "> I will do it after the reset.\nThe example is a deferral.",
    "```text\nI will do it after the reset.\n```\nThe example is a deferral.",
    "~~~text\nI will do it after the reset.\n~~~\nThe example is a deferral.",
    "Compaction ran and I continued the work.",
    "The Stop hook detects a plan to do work after compaction.",
    "I will finish the work now. My context is nearly full.",
    "> A worker said:\nI will finish the edit after compaction.",
    "In this phase the Stop hook checks for work deferred after compaction.",
    "My context is full.",
    "The example is `I will finish after compaction.`",
    'The example is "I will finish after compaction."',
    "The example is 'I will finish after compaction.'",
    "The example is “I will finish after compaction.”",
    "The example is ‘I’ll finish after compaction.’",
    "```text\nI will finish after compaction.\n```",
    "~~~text\nI will finish after compaction.\n~~~",
)
NO_CONTEXT_REASON = (
    "I will do it after the reset.",
    "I will run the tests after the reset completes on the Mac. The machine is rebooting.",
    "I will run the tests after the build finishes.",
    "I will do this later.",
    "The context window is 1M tokens. I will finish the work later.",
)


class ContextDeferralTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "context_deferral.conf").write_bytes(CONFIG.read_bytes())
        self.environment = {**os.environ, "CONF_DIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}

    def call(self, **payload: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
                              capture_output=True, check=False, env=self.environment,
                              timeout=TEST_HANG_GUARD_SECS)

    def assert_allowed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_each_context_deferral_family_blocks_the_stop(self) -> None:
        for text in TRUE_HITS:
            with self.subTest(text=text):
                result = self.call(last_assistant_message=text)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", REASON + "\n"))

    def test_quotes_and_context_descriptions_allow_the_stop(self) -> None:
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assert_allowed(self.call(last_assistant_message=text))

    def test_plans_without_a_context_reason_allow_the_stop(self) -> None:
        for text in NO_CONTEXT_REASON:
            with self.subTest(text=text):
                self.assert_allowed(self.call(last_assistant_message=text))

    def test_other_contexts_allow_the_stop(self) -> None:
        for text in (
            "The test context is full of fixtures. I will clean it later.",
            "Worker impl2's context is full. I will resume it later.",
        ):
            with self.subTest(text=text):
                self.assert_allowed(self.call(last_assistant_message=text))

    def test_negative_plans_allow_the_stop(self) -> None:
        for text in (
            "My context is full; I will not defer this work until later.",
            "The context is full of examples; I will not do it after compaction.",
        ):
            with self.subTest(text=text):
                self.assert_allowed(self.call(last_assistant_message=text))

    def test_context_contractions_and_completion_markers_block_the_stop(self) -> None:
        for text in (
            "My context’s full. I’ll finish later.",
            "Once compaction finishes, I will continue the work.",
            "The context window is nearly full, so I will finish this later.",
        ):
            with self.subTest(text=text):
                result = self.call(last_assistant_message=text)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", REASON + "\n"))

    def test_long_text_without_sentence_ends_finishes_within_two_seconds(self) -> None:
        cases = {
            "8000_newlines": "\n" * 8000,
            "100000_newlines": "\n" * 100000,
            "million_spaces": " " * 1000000,
            "words": ("work remains " * 16000)[:200000],
            "words_with_suffix": ("work remains " * 16000)[:200000] + " my context is full and I will",
            "many_actions_and_later": ("I will do it later. The hook runs. " * 40000)[:1000000],
            "many_actions_and_states": ("I will do it. My context is full. " * 40000)[:1000000],
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                started = time.perf_counter()
                result = subprocess.run(
                    [sys.executable, str(HOOK)],
                    input=json.dumps({"last_assistant_message": text}),
                    text=True, capture_output=True, check=False,
                    env=self.environment, timeout=2,
                )
                elapsed = time.perf_counter() - started
                self.assert_allowed(result)
                self.assertLess(elapsed, 2)
                print(f"CONTEXT_DEFERRAL_SECONDS case={name} seconds={elapsed:.6f}")

    def test_a_previous_stop_hook_block_prevents_a_second_block(self) -> None:
        self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0], stop_hook_active=True))

    def test_empty_or_missing_final_text_allows_the_stop(self) -> None:
        self.assert_allowed(self.call())
        self.assert_allowed(self.call(last_assistant_message=""))

    def test_missing_or_bad_patterns_allow_the_stop_without_output(self) -> None:
        path = self.root / "context_deferral.conf"
        path.unlink()
        self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0]))
        for content in ("[", "# No patterns.\n", CONFIG.read_text(encoding="utf-8") + "\n[\n"):
            _ = path.write_text(content, encoding="utf-8")
            self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0]))

    def test_config_changes_take_effect_on_the_next_call(self) -> None:
        _ = (self.root / "context_deferral.conf").write_text(
            "window: 200\naction: I will defer\nmarker: until tomorrow\nexclude: hook\n", encoding="utf-8")
        result = self.call(last_assistant_message="I will defer the edit until tomorrow.")
        self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))
        self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0]))

    def test_a_smaller_window_allows_a_plan_outside_its_region(self) -> None:
        text = "I will finish the work after compaction."
        result = self.call(last_assistant_message=text)
        self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))
        _ = (self.root / "context_deferral.conf").write_text(
            CONFIG.read_text(encoding="utf-8").replace("window: 200", "window: 20"), encoding="utf-8")
        self.assert_allowed(self.call(last_assistant_message=text))

    def test_an_exclusion_inside_the_region_allows_the_stop(self) -> None:
        self.assert_allowed(self.call(last_assistant_message="I will check the hook after compaction."))
        result = self.call(last_assistant_message="I will check the work after compaction.")
        self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))

    def test_invalid_entries_allow_the_stop_without_output(self) -> None:
        valid = CONFIG.read_text(encoding="utf-8")
        for content in (valid.replace("window: 200", "window: many"),
                        valid + "\nunknown: text\n", valid + "\nmarker: [\n",
                        valid + "\nwindow: 20\n", valid.replace("window: 200", "window: 0")):
            with self.subTest(content=content):
                _ = (self.root / "context_deferral.conf").write_text(content, encoding="utf-8")
                self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0]))

    def test_missing_required_kinds_allow_the_stop_without_output(self) -> None:
        lines = CONFIG.read_text(encoding="utf-8").splitlines()
        for kind in ("window", "action", "exclude"):
            with self.subTest(kind=kind):
                _ = (self.root / "context_deferral.conf").write_text(
                    "\n".join(line for line in lines if not line.startswith(kind + ":")), encoding="utf-8")
                self.assert_allowed(self.call(last_assistant_message=TRUE_HITS[0]))

    def test_absent_phrase_kinds_disable_only_their_rule(self) -> None:
        lines = CONFIG.read_text(encoding="utf-8").splitlines()
        for kind in ("marker", "state", "later"):
            with self.subTest(kind=kind):
                _ = (self.root / "context_deferral.conf").write_text(
                    "\n".join(line for line in lines if not line.startswith(kind + ":")), encoding="utf-8")
                disabled = "I will finish after compaction." if kind == "marker" else TRUE_HITS[0]
                self.assert_allowed(self.call(last_assistant_message=disabled))
                enabled = TRUE_HITS[0] if kind == "marker" else "I will finish after compaction."
                result = self.call(last_assistant_message=enabled)
                self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))

    def test_a_block_quote_ends_at_the_next_blank_line(self) -> None:
        result = self.call(last_assistant_message="> A worker said:\nI will finish after compaction.\n\nI will finish after compaction.")
        self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))

    def test_an_exclusion_outside_the_region_keeps_the_block(self) -> None:
        result = self.call(last_assistant_message="The hook runs. I will finish after compaction.")
        self.assertEqual((result.returncode, result.stderr), (2, REASON + "\n"))


if __name__ == "__main__":
    _ = unittest.main()
