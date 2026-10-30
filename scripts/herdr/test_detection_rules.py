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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detection_rules
import herdr
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

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
    reloaded: list[str] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reloaded = []
        for key, value in {"XDG_STATE_HOME": self.root / "state", "XDG_CONFIG_HOME": self.root / "config"}.items():
            self.addCleanup(_restore, key, os.environ.get(key))
            os.environ[key] = str(value)

    def reload(self, session: str) -> bool:
        self.reloaded.append(session)
        return True

    def write_remote(self, text: str) -> None:
        path = detection_rules.remote_path()
        path.parent.mkdir(parents=True)
        _ = path.write_text(text, encoding="utf-8")

    def test_refresh_writes_once_and_reloads_each_running_session(self) -> None:
        self.write_remote(REMOTE)
        reloaded = self.reloaded
        self.assertTrue(detection_rules.refresh({"probe", "agents"}, self.reload))
        self.assertEqual(reloaded, ["agents", "probe"])
        self.assertEqual(detection_rules.override_path().read_text(encoding="utf-8"),
                         detection_rules.built_rules(REMOTE))
        self.assertFalse(detection_rules.refresh({"agents"}, self.reload))
        self.assertEqual(reloaded, ["agents", "probe"])

    def test_an_upstream_update_is_built_again(self) -> None:
        self.write_remote(REMOTE)
        _ = detection_rules.refresh(set(), self.reload)
        _ = detection_rules.remote_path().write_text(REMOTE.replace("2026.10.11.1", "2026.10.30.1"), encoding="utf-8")
        self.assertTrue(detection_rules.refresh(set(), self.reload))
        self.assertIn('version = "2026.10.30.1.1"', detection_rules.override_path().read_text(encoding="utf-8"))

    def test_failed_herdr_reload_is_retried_without_reloading_confirmed_sessions(self) -> None:
        self.write_remote(REMOTE)
        errors = io.StringIO()
        with patch.object(herdr.Herdr, "run", side_effect=["", herdr.HerdrFailure("not ready"), ""]) as run:
            with redirect_stderr(errors):
                self.assertTrue(detection_rules.refresh({"agents", "probe"}, herdr.reload_agent_rules))
            self.assertIn("probe: detection rules not reloaded: not ready", errors.getvalue())
            receipt = detection_rules.override_path().with_suffix(".loaded").read_text(encoding="utf-8").splitlines()
            self.assertEqual(receipt[1:], ["agents"])
            self.assertFalse(detection_rules.refresh({"agents", "probe"}, herdr.reload_agent_rules))
            self.assertEqual(run.call_count, 3)
            self.assertFalse(detection_rules.refresh({"agents", "probe"}, herdr.reload_agent_rules))
            self.assertEqual(run.call_count, 3)

    def test_new_session_reloads_only_rules_missing_from_the_current_receipt(self) -> None:
        self.write_remote(REMOTE)
        reloaded = self.reloaded

        self.assertTrue(detection_rules.refresh({"agents"}, self.reload))
        reloaded.clear()
        self.assertFalse(detection_rules.refresh({"agents", "probe"}, self.reload))
        self.assertEqual(reloaded, ["probe"])
        self.assertFalse(detection_rules.refresh({"agents", "probe"}, self.reload))
        self.assertEqual(reloaded, ["probe"])

    def test_failed_reload_retries_every_live_session_on_the_next_refresh(self) -> None:
        self.write_remote(REMOTE)
        reloaded = self.reloaded

        def fail_last(session: str) -> bool:
            reloaded.append(session)
            if session == "probe":
                raise RuntimeError("reload failed")
            return True

        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            _ = detection_rules.refresh({"agents", "probe"}, fail_last)
        self.assertTrue(detection_rules.override_path().is_file())
        reloaded.clear()
        self.assertFalse(detection_rules.refresh({"agents", "probe"}, self.reload))
        self.assertEqual(reloaded, ["agents", "probe"])

    def test_override_without_reload_confirmation_reloads_live_sessions(self) -> None:
        self.write_remote(REMOTE)
        target = detection_rules.override_path()
        target.parent.mkdir(parents=True)
        _ = target.write_text(detection_rules.built_rules(REMOTE), encoding="utf-8")
        reloaded = self.reloaded
        self.assertFalse(detection_rules.refresh({"agents"}, self.reload))
        self.assertEqual(reloaded, ["agents"])

    def test_failed_rebuild_does_not_reuse_an_older_reload_receipt(self) -> None:
        self.write_remote(REMOTE)
        _ = detection_rules.refresh({"agents"}, self.reload)
        _ = detection_rules.override_path().write_text("replaced externally", encoding="utf-8")
        def fail_reload(_session: str) -> bool:
            raise RuntimeError("reload failed")

        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            _ = detection_rules.refresh({"agents"}, fail_reload)
        reloaded = self.reloaded
        reloaded.clear()
        self.assertFalse(detection_rules.refresh({"agents"}, self.reload))
        self.assertEqual(reloaded, ["agents"])

    def test_no_downloaded_rules_writes_nothing(self) -> None:
        self.assertFalse(detection_rules.refresh({"agents"}, self.reload))
        self.assertFalse(detection_rules.override_path().exists())

    def test_a_rules_change_keeps_the_old_override(self) -> None:
        self.write_remote(REMOTE)
        _ = detection_rules.refresh(set(), self.reload)
        before = detection_rules.override_path().read_text(encoding="utf-8")
        _ = detection_rules.remote_path().write_text(REMOTE.replace("❯", ">"), encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertFalse(detection_rules.refresh({"agents"}, self.reload))
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
