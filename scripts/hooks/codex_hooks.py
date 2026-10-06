#!/usr/bin/env python3
"""Install and trust the fn-length PostToolUse hook for Codex."""

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


COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"'
HANDLER: dict[str, object] = {"type": "command", "command": COMMAND, "timeout": 10}


class HookError(Exception):
    """A local file or app-server failure that prevents a trusted hook."""


class LocatedHook:
    """The fn-length handler as listed by the app-server."""

    __slots__: tuple[str, ...] = ("key", "current_hash", "trust_status", "enabled")

    def __init__(self, key: str, current_hash: str, trust_status: str, enabled: bool) -> None:
        self.key: str = key
        self.current_hash: str = current_hash
        self.trust_status: str = trust_status
        self.enabled: bool = enabled


class MissingHook:
    """No matching user-layer fn-length handler was listed."""

    __slots__: tuple[str, ...] = ()


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


def _matching_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    fields = cast(dict[str, object], group)
    if fields.get("matcher") != "apply_patch" or not isinstance(fields.get("hooks"), list):
        return False
    handlers = cast(list[object], fields["hooks"])
    return any(
        isinstance(handler, dict)
        and cast(dict[str, object], handler).get("type") == "command"
        and cast(dict[str, object], handler).get("command") == COMMAND
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
    if any(_matching_group(group) for group in groups):
        return
    groups.append({"matcher": "apply_patch", "hooks": [HANDLER]})
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


def _listed_hook(server: AppServer, path: str) -> LocatedHook | MissingHook:
    result = server.call("hooks/list", {"cwds": [_home()]})
    data = result.get("data")
    if not isinstance(data, list):
        raise HookError("invalid hooks/list data")
    selected: LocatedHook | MissingHook = MissingHook()
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
                or entry.get("command") != COMMAND
                or entry.get("timeoutSec") != 10
                or entry.get("sourcePath") != path
            ):
                continue
            key = entry.get("key")
            current_hash = entry.get("currentHash")
            trust_status = entry.get("trustStatus")
            enabled = entry.get("enabled")
            if not isinstance(key, str) or not isinstance(current_hash, str) or not isinstance(trust_status, str) or not isinstance(enabled, bool):
                raise HookError("incomplete hooks/list entry")
            found = LocatedHook(key, current_hash, trust_status, enabled)
            if found.trust_status == "trusted" and found.enabled:
                return found
            if isinstance(selected, MissingHook) or (found.trust_status == "trusted" and selected.trust_status != "trusted"):
                selected = found
    return selected


def _status(entry: LocatedHook | MissingHook) -> str:
    if isinstance(entry, MissingHook):
        return "absent"
    return "disabled" if not entry.enabled else entry.trust_status


def _run(action: str) -> None:
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
        entry = _listed_hook(server, path)
        if action == "install" and isinstance(entry, LocatedHook) and entry.trust_status in {"untrusted", "modified"}:
            _ = server.call(
                "config/batchWrite",
                {"edits": [{"keyPath": "hooks.state",
                            "value": {entry.key: {"trusted_hash": entry.current_hash}},
                            "mergeStrategy": "upsert"}],
                 "reloadUserConfig": True},
            )
            entry = _listed_hook(server, path)
        if not isinstance(entry, LocatedHook) or entry.trust_status != "trusted" or not entry.enabled:
            raise HookError(_status(entry))
        if action == "install":
            print(f"trusted {entry.key}")
    finally:
        server.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"install", "check"}:
        print("usage: codex_hooks.py <install|check>", file=sys.stderr)
        return 1
    try:
        _run(sys.argv[1])
    except (HookError, OSError) as error:
        print(f"fn-length codex hook: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
