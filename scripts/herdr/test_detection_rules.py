"""Tests for the local override of herdr's Claude detection rules."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, final, override

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detection_rules

REMOTE = """id = "claude"
version = "2026.10.11.1"
min_engine_version = 2

[[rules]]
id = "live_prompt_box"
state = "idle"
priority = 950
region = "prompt_box_body"
visible_idle = true
line_regex = ['^\\s*❯']

[[rules]]
id = "bash_permission_prompt"
state = "blocked"
priority = 850
region = "whole_recent"
contains = ["do you want to proceed?"]
"""

RULE = "─" * 60
DIALOG = [
    RULE,
    " Bash command",
    " │ rm -f /home/natepiano/.claude1/projects/x/b1e4yc8b9.txt",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and always allow access to /home/natepiano/.claude1/projects/x from this project",
    "   3. No",
    "",
    " Esc to cancel · Tab to amend",
]
# The screen that read idle: a stray rule high on the screen and a `❯` line below it.
STRAY_RULE_DIALOG = [RULE, "❯ Another Claude session sent a message:", "● Running 1 shell command…", *DIALOG]
IDLE_BOX = ["● Done.", "", RULE, "❯ ", RULE, "  unit | ~/rust/x", "  ⏵⏵ auto mode on (shift+tab to cycle)"]


def _restore(key: str, value: str | None) -> None:
    if value is None:
        _ = os.environ.pop(key, None)
    else:
        os.environ[key] = value


def _rules(text: str) -> dict[object, dict[str, object]]:
    rules = cast(list[dict[str, object]], tomllib.loads(text)["rules"])
    return {rule.get("id"): rule for rule in rules}


@final
class BuiltRulesTests(unittest.TestCase):
    def test_build_adds_the_dialog_rule_and_anchors_the_prompt_box(self) -> None:
        built = detection_rules.built_rules(REMOTE)
        parsed = tomllib.loads(built)
        rules = _rules(built)
        self.assertEqual(parsed["version"], "2026.10.11.1.1")
        self.assertEqual(rules["live_prompt_box"].get("regex"), ["\\A(?:[ \\t]*\\n)*[ \\t]*❯"])
        self.assertNotIn("line_regex", rules["live_prompt_box"])
        dialog = rules["live_permission_prompt"]
        self.assertEqual((dialog["state"], dialog["priority"], dialog["region"]),
                         ("blocked", 980, "after_last_horizontal_rule"))
        self.assertEqual(rules["bash_permission_prompt"]["priority"], 850)
        self.assertTrue(built.startswith("# Built from "))

    def test_a_changed_prompt_box_is_refused(self) -> None:
        changed = REMOTE.replace("line_regex = ['^\\s*❯']", "line_regex = ['^\\s*>']")
        with self.assertRaises(detection_rules.RulesChanged):
            _ = detection_rules.built_rules(changed)

    def test_rules_that_already_hold_the_dialog_rule_are_refused(self) -> None:
        with self.assertRaises(detection_rules.RulesChanged):
            _ = detection_rules.built_rules(REMOTE + '\n[[rules]]\nid = "live_permission_prompt"\nstate = "blocked"\n')


@final
class RefreshTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for key, value in {"XDG_STATE_HOME": self.root / "state", "XDG_CONFIG_HOME": self.root / "config"}.items():
            self.addCleanup(_restore, key, os.environ.get(key))
            os.environ[key] = str(value)

    def write_remote(self, text: str) -> None:
        path = detection_rules.remote_path()
        path.parent.mkdir(parents=True)
        _ = path.write_text(text, encoding="utf-8")

    def test_refresh_writes_once_and_reloads_each_running_session(self) -> None:
        self.write_remote(REMOTE)
        reloaded: list[str] = []
        self.assertTrue(detection_rules.refresh({"probe", "agents"}, reloaded.append))
        self.assertEqual(reloaded, ["agents", "probe"])
        self.assertEqual(detection_rules.override_path().read_text(encoding="utf-8"),
                         detection_rules.built_rules(REMOTE))
        self.assertFalse(detection_rules.refresh({"agents"}, reloaded.append))
        self.assertEqual(reloaded, ["agents", "probe"])

    def test_an_upstream_update_is_built_again(self) -> None:
        self.write_remote(REMOTE)
        _ = detection_rules.refresh(set(), lambda _: None)
        _ = detection_rules.remote_path().write_text(REMOTE.replace("2026.10.11.1", "2026.10.30.1"), encoding="utf-8")
        self.assertTrue(detection_rules.refresh(set(), lambda _: None))
        self.assertIn('version = "2026.10.30.1.1"', detection_rules.override_path().read_text(encoding="utf-8"))

    def test_no_downloaded_rules_writes_nothing(self) -> None:
        self.assertFalse(detection_rules.refresh({"agents"}, lambda _: None))
        self.assertFalse(detection_rules.override_path().exists())

    def test_a_rules_change_keeps_the_old_override(self) -> None:
        self.write_remote(REMOTE)
        _ = detection_rules.refresh(set(), lambda _: None)
        before = detection_rules.override_path().read_text(encoding="utf-8")
        _ = detection_rules.remote_path().write_text(REMOTE.replace("❯", ">"), encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertFalse(detection_rules.refresh({"agents"}, lambda _: None))
        self.assertEqual(detection_rules.override_path().read_text(encoding="utf-8"), before)
        self.assertIn("kept the old override", errors.getvalue())


HERDR = shutil.which("herdr")
LIVE_REMOTE = Path.home() / ".local" / "state" / "herdr" / "agent-detection" / "remote" / "claude.toml"


@final
@unittest.skipUnless(HERDR and LIVE_REMOTE.exists(), "needs herdr and its downloaded Claude rules")
class HerdrMatcherTests(unittest.TestCase):
    """herdr's own matcher, on the downloaded rules and on the override built from them."""

    root: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override_file = self.root / "herdr" / "agent-detection" / "claude.toml"
        override_file.parent.mkdir(parents=True)
        _ = override_file.write_text(detection_rules.built_rules(LIVE_REMOTE.read_text(encoding="utf-8")),
                                     encoding="utf-8")

    def state(self, screen: list[str], *, fixed: bool) -> str:
        path = self.root / "screen.txt"
        _ = path.write_text("\n".join(screen) + "\n", encoding="utf-8")
        environment = {**os.environ, "XDG_CONFIG_HOME": str(self.root if fixed else self.root / "none")}
        result = subprocess.run([cast(str, HERDR), "agent", "explain", "--file", str(path), "--agent", "claude"],
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, env=environment, check=True)
        return next(line for line in result.stdout.splitlines() if line.startswith("state:")).removeprefix("state: ")

    def test_a_dialog_under_a_stray_rule_reads_blocked(self) -> None:
        self.assertEqual(self.state(STRAY_RULE_DIALOG, fixed=False), "idle")
        self.assertEqual(self.state(STRAY_RULE_DIALOG, fixed=True), "blocked")

    def test_a_plain_dialog_and_an_idle_box_keep_their_states(self) -> None:
        self.assertEqual(self.state(DIALOG, fixed=True), "blocked")
        self.assertEqual(self.state(IDLE_BOX, fixed=True), "idle")


if __name__ == "__main__":
    _ = unittest.main()
