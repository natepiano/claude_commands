"""Codex hook installation and trust use a sandboxed JSON-RPC app-server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


INSTALLER = Path(__file__).with_name("codex_hooks.py")
COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"'
HANDLER: dict[str, object] = {"type": "command", "command": COMMAND, "timeout": 10}

STUB = r'''#!__PYTHON__
import hashlib
import json
import os
import sys
from pathlib import Path

state_file = Path(os.environ["STUB_STATE"])
hooks_file = Path(os.environ["CODEX_HOME"]) / "hooks.json"

def load_state():
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"trust": {}, "calls": []}

def save_state(state):
    state_file.write_text(json.dumps(state))

def hooks_list(state):
    if not hooks_file.exists():
        return {"data": [{"hooks": []}]}
    config = json.loads(hooks_file.read_text())
    entries = []
    for event, groups in config.get("hooks", {}).items():
        for group_index, group in enumerate(groups):
            matcher = group.get("matcher", "")
            for handler_index, handler in enumerate(group.get("hooks", [])):
                key = f"{hooks_file}:{event.lower().replace('tooluse', '_tool_use')}:{group_index}:{handler_index}"
                material = json.dumps([event, matcher, handler], sort_keys=True)
                current_hash = hashlib.sha256(material.encode()).hexdigest()
                recorded_hash = state["trust"].get(key)
                status = ("untrusted" if recorded_hash is None else
                          "trusted" if recorded_hash == current_hash else "modified")
                entries.append({
                    "key": key, "eventName": "postToolUse", "matcher": matcher,
                    "handlerType": handler.get("type"),
                    "command": handler.get("command"),
                    "timeoutSec": handler.get("timeout"),
                    "sourcePath": str(hooks_file),
                    "currentHash": current_hash, "trustStatus": status,
                    "enabled": os.environ.get("STUB_DISABLED") != "1",
                })
    return {"data": [{"hooks": entries}]}

state = load_state()
state.setdefault("trust", {})
state.setdefault("calls", [])
state["argv"] = sys.argv[1:]
state["cwd"] = os.getcwd()
state["codex_home"] = os.environ["CODEX_HOME"]
save_state(state)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    params = request.get("params", {})
    state = load_state()
    state["calls"].append({"method": method, "params": params})
    save_state(state)
    if method == "initialize":
        result = {"userAgent": "stub"}
    elif method == "hooks/list":
        result = hooks_list(state)
    elif method == "config/batchWrite":
        if os.environ.get("STUB_REFUSE") == "1":
            print(json.dumps({"id": request["id"], "error": {
                "code": -32602, "message": "invalid config key: hooks.state"
            }}), flush=True)
            continue
        edits = params["edits"]
        for edit in edits:
            if edit["keyPath"] != "hooks.state" or edit["mergeStrategy"] != "upsert":
                raise RuntimeError("unexpected trust edit")
            for key, value in edit["value"].items():
                state["trust"][key] = value["trusted_hash"]
        save_state(state)
        result = {}
    else:
        print(json.dumps({"id": request["id"], "error": {
            "code": -32601, "message": "unknown method: " + str(method)
        }}), flush=True)
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
'''


class CodexHookInstallerTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    codex_home: Path = Path()
    hooks_file: Path = Path()
    state_file: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.hooks_file = self.codex_home / "hooks.json"
        self.state_file = self.root / "stub-state.json"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "codex"
        _ = executable.write_text(STUB.replace("__PYTHON__", sys.executable))
        executable.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "FN_LENGTH_HOOK_STATE": str(self.root / "fn-state"),
            "STUB_STATE": str(self.state_file),
            "PATH": str(bin_dir),
        }
        _ = self.environment.pop("CODEX_BIN", None)
        _ = self.environment.pop("STUB_DISABLED", None)
        _ = self.environment.pop("STUB_REFUSE", None)

    def run_installer(self, action: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INSTALLER), action],
            capture_output=True, text=True, check=False,
            env=self.environment, cwd=cwd, timeout=35,
        )

    def state(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.state_file.read_text()))

    def calls(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.state()["calls"])

    def groups(self) -> list[dict[str, object]]:
        config = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, object], config["hooks"])
        return cast(list[dict[str, object]], hooks["PostToolUse"])

    def test_fresh_install_creates_group_and_records_trust(self) -> None:
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [{"matcher": "apply_patch", "hooks": [HANDLER]}])
        self.assertTrue(self.hooks_file.read_text().endswith("\n"))
        self.assertIn('\n  "hooks":', self.hooks_file.read_text())
        calls = self.calls()
        self.assertEqual([call["method"] for call in calls],
                         ["initialize", "hooks/list", "config/batchWrite", "hooks/list"])
        initialize = cast(dict[str, object], calls[0]["params"])
        client = cast(dict[str, object], initialize["clientInfo"])
        capabilities = cast(dict[str, object], initialize["capabilities"])
        self.assertEqual(client["name"], "codex_hooks")
        self.assertEqual(capabilities["experimentalApi"], True)
        self.assertEqual(calls[1]["params"], {"cwds": [str(self.home)]})
        write = cast(dict[str, object], calls[2]["params"])
        self.assertEqual(write["reloadUserConfig"], True)
        edits = cast(list[dict[str, object]], write["edits"])
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["keyPath"], "hooks.state")
        self.assertEqual(edits[0]["mergeStrategy"], "upsert")
        trust = cast(dict[str, str], self.state()["trust"])
        self.assertEqual(len(trust), 1)
        self.assertIn("trusted " + next(iter(trust)), result.stdout)
        self.assertEqual(self.state()["cwd"], str(self.home))
        self.assertEqual(self.state()["argv"], ["app-server"])

    def test_existing_mac_groups_keep_their_indexes(self) -> None:
        old_groups = [
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "random-ack", "timeout": 5}]},
            {"matcher": "apply_patch", "hooks": [{"type": "command", "command": "basedpyright", "timeout": 20}]},
        ]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": old_groups}}))
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups()[:2], old_groups)
        self.assertEqual(self.groups()[2], {"matcher": "apply_patch", "hooks": [HANDLER]})
        self.assertEqual(len(self.groups()), 3)

    def test_second_install_is_idempotent_and_does_not_retrust(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.hooks_file.read_bytes()
        state = self.state()
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        second = self.run_installer("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])

    def test_relative_paths_resolve_before_app_server_changes_directory(self) -> None:
        """Relative home and binary overrides name the caller's files."""
        self.environment["CODEX_HOME"] = "codex-home"
        self.environment["PATH"] = "bin"
        installed = self.run_installer("install", cwd=self.root)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(self.state()["codex_home"], str(self.codex_home))
        self.environment["CODEX_BIN"] = "bin/codex"
        checked = self.run_installer("check", cwd=self.root)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(self.state()["codex_home"], str(self.codex_home))

    def test_trusted_later_copy_wins_without_retrusting_first(self) -> None:
        """A trusted duplicate satisfies check and install without a write."""
        duplicate = {"matcher": "apply_patch", "hooks": [HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [duplicate, duplicate]}}))
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        self.assertEqual(len(trust), 1)
        first_key = next(iter(trust))
        second_key = first_key.replace(":0:0", ":1:0")
        trust[second_key] = trust.pop(first_key)
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])
        self.assertEqual(cast(dict[str, str], self.state()["trust"]), {second_key: trust[second_key]})
        self.assertEqual(len(self.groups()), 2)

    def test_same_handler_under_other_matcher_does_not_count(self) -> None:
        existing = {"matcher": "Write", "hooks": [HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [existing]}}))
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [existing, {"matcher": "apply_patch", "hooks": [HANDLER]}])

    def test_modified_entry_is_trusted_again(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        key = next(iter(trust))
        trust[key] = "stale hash"
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        second = self.run_installer("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("config/batchWrite", [call["method"] for call in self.calls()])
        self.assertNotEqual(cast(dict[str, str], self.state()["trust"])[key], "stale hash")
        self.assertEqual(len(self.groups()), 1)

    def test_check_reports_absent_untrusted_modified_and_disabled(self) -> None:
        absent = self.run_installer("check")
        self.assertEqual(absent.returncode, 1)
        self.assertIn("fn-length codex hook: absent", absent.stdout + absent.stderr)
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [HANDLER]},
        ]}}))
        untrusted = self.run_installer("check")
        self.assertEqual(untrusted.returncode, 1)
        self.assertIn("fn-length codex hook: untrusted", untrusted.stdout + untrusted.stderr)
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        trusted = self.run_installer("check")
        self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        trust[next(iter(trust))] = "stale hash"
        _ = self.state_file.write_text(json.dumps(state))
        modified = self.run_installer("check")
        self.assertEqual(modified.returncode, 1)
        self.assertIn("fn-length codex hook: modified", modified.stdout + modified.stderr)
        self.environment["STUB_DISABLED"] = "1"
        disabled = self.run_installer("check")
        self.assertEqual(disabled.returncode, 1)
        self.assertIn("fn-length codex hook: disabled", disabled.stdout + disabled.stderr)

    def test_dead_app_server_is_reported(self) -> None:
        executable = Path(self.environment["PATH"]) / "codex"
        _ = executable.write_text("#!/bin/sh\nexit 7\n")
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.strip() or result.stderr.strip())

    def test_invalid_hooks_json_is_unchanged(self) -> None:
        original = b"{ invalid json\n"
        _ = self.hooks_file.write_bytes(original)
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.hooks_file), result.stdout + result.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), original)
        self.assertFalse(self.state_file.exists())

    def test_batch_write_refusal_reports_server_error(self) -> None:
        self.environment["STUB_REFUSE"] = "1"
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid config key: hooks.state", result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
