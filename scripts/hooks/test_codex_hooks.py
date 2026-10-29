"""Codex hook installation and trust use a sandboxed JSON-RPC app-server."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

import codex_hooks


INSTALLER = Path(__file__).with_name("codex_hooks.py")
FN_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"'
RETIRED_COMMAND = FN_COMMAND.replace("fn-length", "mul-add")
FN_HANDLER: dict[str, object] = {"type": "command", "command": FN_COMMAND, "timeout": 10}
BERTH_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/pre-tool-use-berth.py"'
BERTH_GROUP: dict[str, object] = {"matcher": "apply_patch",
                                  "hooks": [{"type": "command", "command": BERTH_COMMAND, "timeout": 30}]}
GUARD_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/pre-tool-use-scratch-guard.py"'
GUARD_GROUP: dict[str, object] = {"matcher": "Bash",
                                  "hooks": [{"type": "command", "command": GUARD_COMMAND, "timeout": 10}]}
HOLD_COMMAND = '"$HOME/.claude/scripts/hooks/pre-tool-use-build-hold-guard.sh"'
HOLD_GROUP: dict[str, object] = {"matcher": "Bash",
                                "hooks": [{"type": "command", "command": HOLD_COMMAND, "timeout": 10}]}
PRE_GROUPS = [BERTH_GROUP, GUARD_GROUP, HOLD_GROUP]

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
                    "key": key, "eventName": event[:1].lower() + event[1:], "matcher": matcher,
                    "handlerType": handler.get("type"),
                    "command": handler.get("command"),
                    "timeoutSec": handler.get("timeout"),
                    "sourcePath": str(hooks_file),
                    "currentHash": current_hash, "trustStatus": status,
                    "enabled": os.environ.get("STUB_DISABLED") != "1" and
                               os.environ.get("STUB_DISABLED_COMMAND") != handler.get("command"),
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
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=INSTALLER.resolve().parents[2])))
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
        _ = self.environment.pop("STUB_DISABLED_COMMAND", None)
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

    def groups(self, event: str = "PostToolUse") -> list[dict[str, object]]:
        config = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, object], config["hooks"])
        return cast(list[dict[str, object]], hooks[event])

    def trust(self) -> dict[str, str]:
        return cast(dict[str, str], self.state()["trust"])

    def clear_calls(self) -> None:
        state = self.state()
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))

    def save_trust(self, trust: dict[str, str]) -> None:
        state = self.state()
        state["trust"] = trust
        _ = self.state_file.write_text(json.dumps(state))

    def listed_handlers(self) -> list[dict[str, object]]:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            server = codex_hooks.AppServer(str(self.root / "bin/codex"), str(self.codex_home))
            try:
                _ = server.call("initialize", {})
                result = server.call("hooks/list", {"cwds": [str(self.home)]})
            finally:
                server.close()
        workspaces = cast(list[dict[str, object]], result["data"])
        return cast(list[dict[str, object]], workspaces[0]["hooks"])

    def add_retired_custom_group(self, command: str, event: str = "PostToolUse") -> None:
        config = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], config["hooks"])
        hooks[event].append({"matcher": "apply_patch", "hooks": [
            {"type": "command", "command": RETIRED_COMMAND, "timeout": 99},
            {"type": "command", "command": command, "timeout": 20},
        ]})
        _ = self.hooks_file.write_text(json.dumps(config))

    def test_fresh_install_creates_group_and_records_trust(self) -> None:
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
        ])
        self.assertTrue(self.hooks_file.read_text().endswith("\n"))
        self.assertIn('\n  "hooks":', self.hooks_file.read_text())
        calls = self.calls()
        self.assertEqual([call["method"] for call in calls],
                         ["initialize", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list"])
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
        self.assertEqual(self.groups("PreToolUse"), PRE_GROUPS)
        trust = self.trust()
        self.assertEqual(len(trust), 4)
        self.assertIn("fn-length codex hook: trusted " + next(iter(trust)), result.stdout)
        self.assertIn(f"berth codex hook: trusted {self.hooks_file}:pre_tool_use:0:0", result.stdout)
        self.assertIn(f"scratch-guard codex hook: trusted {self.hooks_file}:pre_tool_use:1:0", result.stdout)
        self.assertIn(f"build-hold-guard codex hook: trusted {self.hooks_file}:pre_tool_use:2:0", result.stdout)
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
        self.assertEqual(self.groups()[2:], [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
        ])
        self.assertEqual(len(self.groups()), 3)

    def test_install_keeps_the_hooks_link_and_adds_missing_hooks_to_its_target(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        target = shared / "hooks.json"
        existing = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        _ = target.write_text(json.dumps({"hooks": {"PostToolUse": [existing]}}))
        self.hooks_file.symlink_to(Path("..") / "shared" / "hooks.json")
        original_link = os.readlink(self.hooks_file)

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.hooks_file.is_symlink())
        self.assertEqual(os.readlink(self.hooks_file), original_link)
        self.assertEqual(self.hooks_file.resolve(), target)
        self.assertEqual(self.groups(), [existing])
        self.assertEqual(target.read_bytes(), self.hooks_file.read_bytes())
        self.assertEqual(len(self.trust()), 4)
        self.assertEqual({entry.name for entry in shared.iterdir()}, {"hooks.json"})

    def test_install_creates_the_temporary_hooks_file_beside_the_link_target(self) -> None:
        target = self.root / "shared" / "hooks.json"
        self.hooks_file.symlink_to(target)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(sys, "argv", ["codex_hooks.py", "install"]),
            mock.patch.object(tempfile, "mkstemp", wraps=tempfile.mkstemp) as create,
            redirect_stdout(output),
        ):
            result = codex_hooks.main()
        self.assertEqual(result, 0, output.getvalue())
        create.assert_called_once_with(prefix=".hooks-", suffix=".json", dir=str(target.parent.resolve()))
        self.assertTrue(self.hooks_file.is_symlink())
        self.assertEqual(self.groups(), [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
        ])

    def _assert_existing_fn_group_keeps_trust(self, old_groups: list[dict[str, object]]) -> None:
        fn_group = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        original = [*old_groups, fn_group]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": original}}))
        seeded = self.run_installer("install")
        self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
        fn_key = next(key for key in self.trust() if key.endswith(f":{len(old_groups)}:0"))
        fn_hash = self.trust()[fn_key]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": original}}))
        self.save_trust({fn_key: fn_hash})
        self.clear_calls()

        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), original)
        self.assertEqual(self.trust()[fn_key], fn_hash)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        written = [key for call in writes
                   for edit in cast(list[dict[str, object]], cast(dict[str, object], call["params"])["edits"])
                   for key in cast(dict[str, object], edit["value"])]
        self.assertEqual(written, [f"{self.hooks_file}:pre_tool_use:{index}:0" for index in range(3)])
        self.assertEqual(len(self.trust()), 4)

    def test_existing_natedev_fn_group_keeps_trust(self) -> None:
        self._assert_existing_fn_group_keeps_trust([])

    def test_existing_mac_fn_group_keeps_trust(self) -> None:
        old_groups: list[dict[str, object]] = [
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "random-ack", "timeout": 5}]},
            {"matcher": "apply_patch", "hooks": [{"type": "command", "command": "basedpyright", "timeout": 20}]},
        ]
        self._assert_existing_fn_group_keeps_trust(old_groups)

    def test_second_install_is_idempotent_and_does_not_retrust(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.groups("PreToolUse").count(HOLD_GROUP), 1)
        before = self.hooks_file.read_bytes()
        modified = self.hooks_file.stat().st_mtime_ns
        trust = self.trust()
        state = self.state()
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        second = self.run_installer("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        self.assertEqual(self.hooks_file.stat().st_mtime_ns, modified)
        self.assertEqual(self.groups("PreToolUse").count(HOLD_GROUP), 1)
        self.assertEqual(self.trust(), trust)
        self.assertEqual([call["method"] for call in self.calls()], ["initialize", "hooks/list"])
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("build-hold-guard codex hook: trusted", checked.stdout.splitlines())
        hold = next(entry for entry in self.listed_handlers() if entry["command"] == HOLD_COMMAND)
        self.assertEqual((hold["eventName"], hold["matcher"], hold["trustStatus"], hold["enabled"]),
                         ("preToolUse", "Bash", "trusted", True))

    def test_cleanup_preserves_trusted_custom_handlers_at_new_keys_in_one_batch(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.add_retired_custom_group("custom-post")
        self.add_retired_custom_group("custom-pre", "PreToolUse")
        trusted = self.trust()
        custom_hashes: dict[str, str] = {}
        for entry in self.listed_handlers():
            command = entry["command"]
            if command in {"custom-post", "custom-pre"}:
                key = cast(str, entry["key"])
                current_hash = cast(str, entry["currentHash"])
                trusted[key] = current_hash
                custom_hashes[cast(str, command)] = current_hash
        self.save_trust(trusted)
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 1)
        params = cast(dict[str, object], writes[0]["params"])
        edits = cast(list[dict[str, object]], params["edits"])
        expected = {
            f"{self.hooks_file}:post_tool_use:1:0": {"trusted_hash": custom_hashes["custom-post"]},
            f"{self.hooks_file}:pre_tool_use:3:0": {"trusted_hash": custom_hashes["custom-pre"]},
        }
        self.assertEqual(edits, [{"keyPath": "hooks.state", "value": expected, "mergeStrategy": "upsert"}])
        self.assertEqual(params["reloadUserConfig"], True)
        self.assertEqual(set(self.trust()) - set(trusted), set(expected))
        for entry in self.listed_handlers():
            if entry["command"] in custom_hashes:
                self.assertEqual(entry["trustStatus"], "trusted")
                self.assertIn(entry["key"], expected)
        self.clear_calls()
        repeated = self.run_installer("install")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual([call["method"] for call in self.calls()], ["initialize", "hooks/list"])

    def test_cleanup_leaves_untrusted_custom_sibling_untrusted(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.add_retired_custom_group("custom-untrusted")
        trusted = self.trust()
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust(), trusted)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])
        custom = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-untrusted")
        self.assertEqual(custom["key"], f"{self.hooks_file}:post_tool_use:1:0")
        self.assertEqual(custom["trustStatus"], "untrusted")

    def test_cleanup_does_not_restore_trust_for_disabled_custom_sibling(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.add_retired_custom_group("custom-disabled")
        custom = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-disabled")
        trusted = {**self.trust(), cast(str, custom["key"]): cast(str, custom["currentHash"])}
        self.save_trust(trusted)
        self.environment["STUB_DISABLED_COMMAND"] = "custom-disabled"
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust(), trusted)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])
        moved = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-disabled")
        self.assertEqual(moved["key"], f"{self.hooks_file}:post_tool_use:1:0")
        self.assertEqual(moved["trustStatus"], "untrusted")
        self.assertEqual(moved["enabled"], False)

    def assert_custom_command_preserved(self, command: str) -> subprocess.CompletedProcess[str]:
        _ = self.hooks_file.write_text(json.dumps({"hooks": {
            "PostToolUse": [{"matcher": "apply_patch", "hooks": [FN_HANDLER]},
                            {"matcher": "Write", "hooks": [{"type": "command", "command": command}]}],
            "PreToolUse": PRE_GROUPS,
        }}, indent=4))
        before = self.hooks_file.read_bytes()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        return result

    def test_command_that_only_mentions_retired_script_stays_byte_for_byte(self) -> None:
        script = RETIRED_COMMAND.rsplit("/", 1)[1].removesuffix('"')
        result = self.assert_custom_command_preserved(f"printf '%s\\n' /archive/{script}")
        self.assertIn(str(self.hooks_file), result.stderr)
        self.assertIn("by hand", result.stderr)

    def test_retired_command_with_shell_suffix_stays_and_warns_about_file(self) -> None:
        result = self.assert_custom_command_preserved(RETIRED_COMMAND + "; true")
        self.assertEqual(result.stderr.splitlines(), [
            f"{self.hooks_file}: remove the remaining retired script handler by hand",
        ])

    def test_install_removes_retired_handlers_and_preserves_other_configuration(self) -> None:
        retired: dict[str, object] = {"type": "command", "command": RETIRED_COMMAND, "timeout": 10}
        other: dict[str, object] = {"type": "command", "command": "basedpyright", "timeout": 20}
        similar: dict[str, object] = {
            "type": "command", "command": RETIRED_COMMAND + ".backup", "timeout": 5,
        }
        absolute: dict[str, object] = {
            "type": "command", "command": RETIRED_COMMAND.replace("$HOME", str(self.home)), "timeout": 99,
        }
        retained_groups: list[dict[str, object]] = [
            {"matcher": "apply_patch", "hooks": [], "description": "old group"},
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
            {"matcher": "Write", "hooks": [other, absolute, similar]},
        ]
        original_groups = [
            {**retained_groups[0], "hooks": [retired]},
            retained_groups[1],
            {**retained_groups[2], "hooks": [other, {**retired, "timeout": 99}, absolute, similar]},
        ]
        _ = self.hooks_file.write_text(json.dumps({
            "description": "user configuration",
            "hooks": {"PostToolUse": original_groups, "PreToolUse": PRE_GROUPS},
        }))

        installed = self.run_installer("install")

        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        document = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        self.assertEqual(document, {
            "description": "user configuration",
            "hooks": {"PostToolUse": retained_groups, "PreToolUse": PRE_GROUPS},
        })
        self.assertEqual(installed.stderr.splitlines(), [
            f"{self.hooks_file}: remove the remaining retired script handler by hand",
        ])
        self.assertEqual(len(self.trust()), 4)
        self.assertIn(f"{self.hooks_file}:post_tool_use:1:0", self.trust())
        before = self.hooks_file.read_bytes()
        modified = self.hooks_file.stat().st_mtime_ns
        trust = self.trust()
        self.clear_calls()

        repeated = self.run_installer("install")

        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        self.assertEqual(self.hooks_file.stat().st_mtime_ns, modified)
        self.assertEqual(self.trust(), trust)
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
        duplicate = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [duplicate, duplicate]}}))
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        self.assertEqual(len(trust), 4)
        first_key = next(key for key in trust if key.endswith(":post_tool_use:0:0"))
        second_key = first_key.replace(":0:0", ":1:0")
        trust[second_key] = trust.pop(first_key)
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])
        self.assertEqual(self.trust(), trust)
        self.assertEqual(len(self.groups()), 2)

    def test_same_handler_under_other_matcher_does_not_count(self) -> None:
        existing = {"matcher": "Write", "hooks": [FN_HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [existing]}}))
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [existing,
                         {"matcher": "apply_patch", "hooks": [FN_HANDLER]}])

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

    def test_only_modified_hook_is_retrusted(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        trust = self.trust()
        fn_key = next(key for key in trust if key.endswith(":post_tool_use:0:0"))
        berth_key = next(key for key in trust if key.endswith(":pre_tool_use:0:0"))
        fn_hash = trust[fn_key]
        trust[berth_key] = "stale hash"
        self.save_trust(trust)
        self.clear_calls()

        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust()[fn_key], fn_hash)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 1)
        params = cast(dict[str, object], writes[0]["params"])
        edits = cast(list[dict[str, object]], params["edits"])
        self.assertEqual(list(cast(dict[str, object], edits[0]["value"])), [berth_key])
        self.assertNotEqual(self.trust()[berth_key], "stale hash")

    def test_check_names_berth_when_fn_is_trusted(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        fn_line = "fn-length codex hook: trusted"
        guard_line = "scratch-guard codex hook: trusted"
        hold_line = "build-hold-guard codex hook: trusted"

        trust = self.trust()
        berth_key = next(key for key in trust if key.endswith(":pre_tool_use:0:0"))
        del trust[berth_key]
        self.save_trust(trust)
        untrusted = self.run_installer("check")
        self.assertEqual(untrusted.returncode, 1)
        self.assertEqual(untrusted.stdout.splitlines(), [fn_line, "berth codex hook: untrusted", guard_line, hold_line])

        trust[berth_key] = "stale hash"
        self.save_trust(trust)
        modified = self.run_installer("check")
        self.assertEqual(modified.returncode, 1)
        self.assertEqual(modified.stdout.splitlines(), [fn_line, "berth codex hook: modified", guard_line, hold_line])

        restored = self.run_installer("install")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.environment["STUB_DISABLED_COMMAND"] = BERTH_COMMAND
        disabled = self.run_installer("check")
        self.assertEqual(disabled.returncode, 1)
        self.assertEqual(disabled.stdout.splitlines(), [fn_line, "berth codex hook: disabled", guard_line, hold_line])
        failed_install = self.run_installer("install")
        self.assertEqual(failed_install.returncode, 1)
        self.assertEqual(failed_install.stdout.splitlines(), [fn_line, "berth codex hook: disabled", guard_line, hold_line])
        _ = self.environment.pop("STUB_DISABLED_COMMAND")

        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": self.groups()[:1]}}))
        absent = self.run_installer("check")
        self.assertEqual(absent.returncode, 1)
        self.assertEqual(absent.stdout.splitlines(),
                         [fn_line, "berth codex hook: absent", "scratch-guard codex hook: absent",
                          "build-hold-guard codex hook: absent"])

    def test_check_reports_absent_untrusted_modified_and_disabled(self) -> None:
        absent = self.run_installer("check")
        self.assertEqual(absent.returncode, 1)
        self.assertIn("fn-length codex hook: absent", absent.stdout + absent.stderr)
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
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
        self.assertEqual(result.stdout, "")
        self.assertIn("codex hooks: app-server closed before replying", result.stderr)

    def started_server(self, script: str) -> codex_hooks.AppServer:
        executable = Path(self.environment["PATH"]) / "codex"
        _ = executable.write_text(script)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            server = codex_hooks.AppServer(str(executable), str(self.codex_home))
        self.addCleanup(server.close)
        return server

    # A server that ends is met either by the write of the request or by the read of the reply,
    # whichever comes first. The two tests below fix the order each way; both must read the same.
    def test_a_server_gone_before_the_request_is_written_reads_as_closed(self) -> None:
        server = self.started_server("#!/bin/sh\nexit 7\n")
        _ = server.process.wait(timeout=10)
        with self.assertRaisesRegex(codex_hooks.HookError, "^app-server closed before replying$"):
            _ = server.call("initialize", {})

    def test_a_server_that_ends_after_reading_the_request_reads_as_closed(self) -> None:
        server = self.started_server("#!/bin/sh\nread line\nexit 7\n")
        with self.assertRaisesRegex(codex_hooks.HookError, "^app-server closed before replying$"):
            _ = server.call("initialize", {})

    def test_invalid_hooks_json_is_unchanged(self) -> None:
        original = b"{ invalid json\n"
        _ = self.hooks_file.write_bytes(original)
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(f"codex hooks: {self.hooks_file}", result.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), original)
        self.assertFalse(self.state_file.exists())

    def test_batch_write_refusal_reports_server_error(self) -> None:
        self.environment["STUB_REFUSE"] = "1"
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "codex hooks: invalid config key: hooks.state")


if __name__ == "__main__":
    _ = unittest.main()
