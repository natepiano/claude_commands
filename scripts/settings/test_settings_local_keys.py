from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import cast, final

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

KEYS = Path(__file__).with_name("settings_local_keys.sh")
BASH = shutil.which("bash") or "/bin/bash"
NATEDEV = {
    "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False, "unit-board@nate-local": True},
    "extraKnownMarketplaces": {"nate-local": {"source": {"source": "directory", "path": "/home/natepiano/.claude/mods"}}},
    "hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]},
        {"matcher": "^(startup|resume|clear|compact|fork)$", "hooks": [
            {"type": "command", "command": "bash '/home/natepiano/.claude/hooks/herdr-agent-state.sh' session"}]},
    ]},
    "model": "opus",
}
MAC = {
    "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False},
    "hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]},
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "bash '/Users/natemccoy/.claude/hooks/herdr-agent-state.sh' session"}]},
    ]},
}


def program(name: str, settings: object, sidecar: object | None = None) -> object:
    """Run one of the jq programs settings_local_keys.sh defines."""
    script = f'source "$0"; jq --argjson keys "$SETTINGS_LOCAL_KEYS_JSON" --argjson local "$1" "${name}"'
    ran = subprocess.run([BASH, "-c", script, str(KEYS), json.dumps([sidecar] if sidecar is not None else [])],
                         input=json.dumps(settings), capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
    return cast(object, json.loads(ran.stdout))


@final
class SettingsLocalKeysTests(unittest.TestCase):
    def test_each_machine_cleans_to_the_same_blob(self) -> None:
        self.assertEqual(program("SETTINGS_LOCAL_KEYS_STRIP", NATEDEV), program("SETTINGS_LOCAL_KEYS_STRIP", MAC))
        self.assertEqual(program("SETTINGS_LOCAL_KEYS_STRIP", NATEDEV), {
            "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False},
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]}]}})

    def test_the_sidecar_puts_each_machine_back(self) -> None:
        for settings in (NATEDEV, MAC):
            sidecar = program("SETTINGS_LOCAL_KEYS_PICK", settings)
            merged = program("SETTINGS_LOCAL_KEYS_MERGE", program("SETTINGS_LOCAL_KEYS_STRIP", settings), sidecar)
            self.assertEqual(merged, settings)
            self.assertEqual(program("SETTINGS_LOCAL_KEYS_MISSING", merged, sidecar), 0)


if __name__ == "__main__":
    _ = unittest.main()
