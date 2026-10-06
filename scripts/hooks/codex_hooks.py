#!/usr/bin/env python3
"""Install and trust the Rust PostToolUse hooks for Codex."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from typing import cast


class CodexHook:
    """A named PostToolUse handler that must be installed and trusted."""

    __slots__: tuple[str, ...] = ("name", "command")

    def __init__(self, name: str, script: str) -> None:
        self.name: str = name
        self.command: str = f'"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/{script}"'

    def handler(self) -> dict[str, object]:
        return {"type": "command", "command": self.command, "timeout": 10}


HOOKS: tuple[CodexHook, ...] = (
    CodexHook("fn-length", "post-tool-use-fn-length.py"),
    CodexHook("mul_add", "post-tool-use-mul-add.py"),
)


class HookError(Exception):
    """A local file or app-server failure that prevents a trusted hook."""


class LocatedHook:
    """A named handler as listed by the app-server."""

    __slots__: tuple[str, ...] = ("hook", "key", "current_hash", "trust_status", "enabled")

    def __init__(self, hook: CodexHook, key: str, current_hash: str, trust_status: str, enabled: bool) -> None:
        self.hook: CodexHook = hook
        self.key: str = key
        self.current_hash: str = current_hash
        self.trust_status: str = trust_status
        self.enabled: bool = enabled


class MissingHook:
    """No matching user-layer handler was listed."""

    __slots__: tuple[str, ...] = ("hook",)

    def __init__(self, hook: CodexHook) -> None:
        self.hook: CodexHook = hook


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HookError(f"invalid {description}")
    return cast(dict[str, object], value)


def _home() -> str:
    return os.path.abspath(os.environ.get("HOME") or os.path.expanduser("~"))


def _codex_home() -> str:
    return os.path.abspath(os.environ.get("CODEX_HOME") or os.path.join(_home(), ".codex"))


def _command_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return os.path.abspath(configured)
    found = shutil.which("codex")
    return os.path.abspath(found) if found else os.path.join(_home(), ".local", "bin", "codex")


def _matching_group(group: object, hook: CodexHook) -> bool:
    if not isinstance(group, dict):
        return False
    fields = cast(dict[str, object], group)
    if fields.get("matcher") != "apply_patch" or not isinstance(fields.get("hooks"), list):
        return False
    handlers = cast(list[object], fields["hooks"])
    return any(
        isinstance(handler, dict)
        and cast(dict[str, object], handler).get("type") == "command"
        and cast(dict[str, object], handler).get("command") == hook.command
        and cast(dict[str, object], handler).get("timeout") == 10
        for handler in handlers
    )


def _install_file(path: str) -> None:
    document: dict[str, object]
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as source:
                document = _object(cast(object, json.load(source)), str(path))
        except (OSError, ValueError, UnicodeError) as error:
            raise HookError(f"{path}: {error}") from error
    else:
        document = {"hooks": {}}
    hooks = _object(document.get("hooks"), f"{path} hooks")
    groups_value = hooks.setdefault("PostToolUse", [])
    if not isinstance(groups_value, list):
        raise HookError(f"{path}: invalid PostToolUse groups")
    groups = cast(list[object], groups_value)
    missing = [hook for hook in HOOKS if not any(_matching_group(group, hook) for group in groups)]
    if not missing:
        return
    for hook in missing:
        groups.append({"matcher": "apply_patch", "hooks": [hook.handler()]})
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hooks-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, indent=2)
            _ = output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AppServer:
    """One bounded JSON-RPC conversation over Codex's stdio transport."""

    __slots__: tuple[str, ...] = ("process", "deadline", "buffer", "next_id", "selector")

    def __init__(self, binary: str, codex_home: str) -> None:
        self.deadline: float = time.monotonic() + 30
        self.buffer: bytes = b""
        self.next_id: int = 0
        environment = {**os.environ, "CODEX_HOME": codex_home}
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            [binary, "app-server"], cwd=_home(), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise HookError("app-server has no stdio pipes")
        self.selector: selectors.BaseSelector = selectors.DefaultSelector()
        _ = self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.kill()
        try:
            _ = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _ = self.process.wait()

    def _line(self) -> bytes:
        if self.process.stdout is None:
            raise HookError("app-server has no stdout")
        while True:
            line, separator, remainder = self.buffer.partition(b"\n")
            if separator:
                self.buffer = remainder
                return line
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise HookError("app-server timed out")
            if not self.selector.select(remaining):
                raise HookError("app-server timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise HookError("app-server closed before replying")
            self.buffer += chunk

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if self.process.stdin is None:
            raise HookError("app-server has no stdin")
        self.next_id += 1
        request_id = self.next_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            _ = self.process.stdin.write((json.dumps(request) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise HookError(f"app-server write failed: {error}") from error
        while True:
            try:
                response = _object(cast(object, json.loads(self._line())), "app-server response")
            except (ValueError, UnicodeError) as error:
                raise HookError(f"app-server sent invalid JSON: {error}") from error
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = _object(response["error"], "RPC error")
                raise HookError(str(error.get("message", error)))
            return _object(response.get("result"), f"{method} result")


def _listed_hooks(server: AppServer, path: str) -> dict[CodexHook, LocatedHook | MissingHook]:
    result = server.call("hooks/list", {"cwds": [_home()]})
    data = result.get("data")
    if not isinstance(data, list):
        raise HookError("invalid hooks/list data")
    selected: dict[CodexHook, LocatedHook | MissingHook] = {hook: MissingHook(hook) for hook in HOOKS}
    for workspace in cast(list[object], data):
        hooks = _object(workspace, "hooks/list workspace").get("hooks")
        if not isinstance(hooks, list):
            raise HookError("invalid hooks/list hooks")
        for candidate in cast(list[object], hooks):
            entry = _object(candidate, "hooks/list entry")
            if (
                entry.get("eventName") != "postToolUse"
                or entry.get("matcher") != "apply_patch"
                or entry.get("handlerType") != "command"
                or entry.get("timeoutSec") != 10
                or entry.get("sourcePath") != path
            ):
                continue
            hook = next((hook for hook in HOOKS if entry.get("command") == hook.command), None)
            if hook is None:
                continue
            key = entry.get("key")
            current_hash = entry.get("currentHash")
            trust_status = entry.get("trustStatus")
            enabled = entry.get("enabled")
            if not isinstance(key, str) or not isinstance(current_hash, str) or not isinstance(trust_status, str) or not isinstance(enabled, bool):
                raise HookError("incomplete hooks/list entry")
            found = LocatedHook(hook, key, current_hash, trust_status, enabled)
            previous = selected[hook]
            if found.trust_status == "trusted" and found.enabled:
                selected[hook] = found
            elif not (isinstance(previous, LocatedHook) and previous.trust_status == "trusted" and previous.enabled):
                if isinstance(previous, MissingHook) or (found.trust_status == "trusted" and previous.trust_status != "trusted"):
                    selected[hook] = found
    return selected


def _status(entry: LocatedHook | MissingHook) -> str:
    if isinstance(entry, MissingHook):
        return "absent"
    return "disabled" if not entry.enabled else entry.trust_status


def _run(action: str) -> int:
    codex_home = _codex_home()
    binary = _command_binary()
    path = os.path.join(codex_home, "hooks.json")
    if action == "install":
        _install_file(path)
    server = AppServer(binary, codex_home)
    try:
        _ = server.call(
            "initialize",
            {"clientInfo": {"name": "codex_hooks", "version": "1.0"},
             "capabilities": {"experimentalApi": True}},
        )
        entries = _listed_hooks(server, path)
        if action == "install":
            for hook in HOOKS:
                entry = entries[hook]
                if isinstance(entry, LocatedHook) and entry.trust_status in {"untrusted", "modified"}:
                    _ = server.call(
                        "config/batchWrite",
                        {"edits": [{"keyPath": "hooks.state",
                                    "value": {entry.key: {"trusted_hash": entry.current_hash}},
                                    "mergeStrategy": "upsert"}],
                         "reloadUserConfig": True},
                    )
                    entries = _listed_hooks(server, path)
    finally:
        server.close()
    success = all(isinstance(entry, LocatedHook) and entry.trust_status == "trusted" and entry.enabled
                  for entry in entries.values())
    for hook in HOOKS:
        entry = entries[hook]
        if action == "install" and success and isinstance(entry, LocatedHook):
            print(f"{hook.name} codex hook: trusted {entry.key}")
        else:
            print(f"{hook.name} codex hook: {_status(entry)}")
    return 0 if success else 1


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"install", "check"}:
        print("usage: codex_hooks.py <install|check>", file=sys.stderr)
        return 1
    try:
        return _run(sys.argv[1])
    except (HookError, OSError) as error:
        print(f"codex hooks: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
