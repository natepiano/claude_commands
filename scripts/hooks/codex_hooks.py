#!/usr/bin/env python3
"""Install and trust the Codex hooks: the Rust PostToolUse checks and the berth PreToolUse check on
`apply_patch`, and the scratch and build-hold PreToolUse checks on `Bash`."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from enum import Enum
from typing import NamedTuple, cast


RETIRED_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-mul-add.py"'


class PythonHookScript(NamedTuple):
    """A hook run through the shared Python launcher."""

    script: str

    def command(self) -> str:
        return f'"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/{self.script}"'


class ShellHookFront(NamedTuple):
    """A hook front that starts Python only when its checks need it."""

    script: str

    def command(self) -> str:
        return f'"$HOME/.claude/scripts/hooks/{self.script}"'


class CodexHook:
    """A named handler for one hook event and tool matcher that must be installed and trusted."""

    __slots__: tuple[str, ...] = ("name", "command", "event", "timeout", "matcher")

    def __init__(self, name: str, script: PythonHookScript | ShellHookFront, event: str = "PostToolUse", timeout: int = 10,
                 matcher: str = "apply_patch") -> None:
        self.name: str = name
        self.command: str = script.command()
        self.event: str = event
        self.timeout: int = timeout
        self.matcher: str = matcher

    def handler(self) -> dict[str, object]:
        return {"type": "command", "command": self.command, "timeout": self.timeout}

    def listed_event(self) -> str:
        """The event name `hooks/list` reports: `PreToolUse` lists as `preToolUse`."""
        return self.event[:1].lower() + self.event[1:]


HOOKS: tuple[CodexHook, ...] = (
    CodexHook("fn-length", PythonHookScript("post-tool-use-fn-length.py")),
    # cargo-berth's own deadline is ten seconds; the hook allows for a slow start under load.
    CodexHook("berth", PythonHookScript("pre-tool-use-berth.py"), "PreToolUse", 30),
    # Codex sends a shell command as tool `Bash`, as Claude Code does.
    CodexHook("scratch-guard", PythonHookScript("pre-tool-use-scratch-guard.py"), "PreToolUse", 10, "Bash"),
    CodexHook("build-hold-guard", ShellHookFront("pre-tool-use-build-hold-guard.sh"), "PreToolUse", 10, "Bash"),
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


class AllToolsMatcher(Enum):
    ALL_TOOLS = "all_tools"


class DefaultHookTimeout(Enum):
    DEFAULT = "default"


class AbsentHandlerCommand(Enum):
    NO_COMMAND = "no_command"


class HandlerTrustIdentity(NamedTuple):
    """The handler contents and hash to which the user's trust applies."""

    event: str
    matcher: str | AllToolsMatcher
    command: str | AbsentHandlerCommand
    timeout: int | float | DefaultHookTimeout
    current_hash: str


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
    if fields.get("matcher") != hook.matcher or not isinstance(fields.get("hooks"), list):
        return False
    handlers = cast(list[object], fields["hooks"])
    return any(
        isinstance(handler, dict)
        and cast(dict[str, object], handler).get("type") == "command"
        and cast(dict[str, object], handler).get("command") == hook.command
        and cast(dict[str, object], handler).get("timeout") == hook.timeout
        for handler in handlers
    )


def _is_retired_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    entry = cast(dict[str, object], handler)
    return entry.get("type") == "command" and entry.get("command") == RETIRED_COMMAND


def _remove_retired_handlers(hooks: dict[str, object], path: str) -> bool:
    changed = False
    needs_manual_removal = False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in cast(list[object], groups):
            if not isinstance(group, dict):
                continue
            fields = cast(dict[str, object], group)
            handlers = fields.get("hooks")
            if not isinstance(handlers, list):
                continue
            entries = cast(list[object], handlers)
            kept = [handler for handler in entries if not _is_retired_handler(handler)]
            changed |= len(kept) != len(entries)
            for handler in kept:
                if not isinstance(handler, dict):
                    continue
                entry = cast(dict[str, object], handler)
                command = entry.get("command")
                if entry.get("type") == "command" and isinstance(command, str) and "post-tool-use-mul-add.py" in command:
                    needs_manual_removal = True
            # Keep groups in place so other groups keep their trust keys.
            fields["hooks"] = kept
    if needs_manual_removal:
        print(f"{path}: remove the remaining retired script handler by hand", file=sys.stderr)
    return changed


def _install_file(path: str, binary: str, codex_home: str) -> list[HandlerTrustIdentity]:
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
    removed = _remove_retired_handlers(hooks, path)
    changed = removed
    for hook in HOOKS:
        groups_value = hooks.setdefault(hook.event, [])
        if not isinstance(groups_value, list):
            raise HookError(f"{path}: invalid {hook.event} groups")
        groups = cast(list[object], groups_value)
        if not any(_matching_group(group, hook) for group in groups):
            groups.append({"matcher": hook.matcher, "hooks": [hook.handler()]})
            changed = True
    if not changed:
        return []
    previously_trusted: list[HandlerTrustIdentity] = []
    if removed:
        server = AppServer(binary, codex_home)
        try:
            _initialize(server)
            previously_trusted = [
                _handler_identity(entry) for entry in _listed_hook_entries(server, path)
                if entry.get("trustStatus") == "trusted" and entry.get("enabled") is True
            ]
        finally:
            server.close()
    destination = os.path.realpath(path) if os.path.islink(path) else path
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hooks-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, indent=2)
            _ = output.write("\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return previously_trusted


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
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                # A request the server never read is still buffered, and closing fails on it.
                with contextlib.suppress(OSError):
                    pipe.close()
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
        except BrokenPipeError as error:
            # The server ended before it read the request. Whether the write or the read meets
            # that first is a matter of timing, so both report it the same way.
            raise HookError("app-server closed before replying") from error
        except OSError as error:
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


def _initialize(server: AppServer) -> None:
    _ = server.call(
        "initialize",
        {"clientInfo": {"name": "codex_hooks", "version": "1.0"},
         "capabilities": {"experimentalApi": True}},
    )


def _listed_hook_entries(server: AppServer, path: str) -> list[dict[str, object]]:
    result = server.call("hooks/list", {"cwds": [_home()]})
    data = result.get("data")
    if not isinstance(data, list):
        raise HookError("invalid hooks/list data")
    entries: list[dict[str, object]] = []
    for workspace in cast(list[object], data):
        hooks = _object(workspace, "hooks/list workspace").get("hooks")
        if not isinstance(hooks, list):
            raise HookError("invalid hooks/list hooks")
        for candidate in cast(list[object], hooks):
            entry = _object(candidate, "hooks/list entry")
            if entry.get("sourcePath") == path:
                entries.append(entry)
    return entries


def _handler_identity(entry: dict[str, object]) -> HandlerTrustIdentity:
    event = entry.get("eventName")
    matcher = entry.get("matcher")
    command = entry.get("command")
    timeout = entry.get("timeoutSec")
    current_hash = entry.get("currentHash")
    if (
        not isinstance(event, str) or not isinstance(current_hash, str)
        or (command is not None and not isinstance(command, str))
        or (matcher is not None and not isinstance(matcher, str))
        or (timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)))
    ):
        raise HookError("incomplete hooks/list entry")
    return HandlerTrustIdentity(
        event, AllToolsMatcher.ALL_TOOLS if matcher is None else matcher,
        AbsentHandlerCommand.NO_COMMAND if command is None else command,
        DefaultHookTimeout.DEFAULT if timeout is None else timeout, current_hash,
    )


def _trust_handlers(server: AppServer, values: dict[str, dict[str, str]]) -> None:
    _ = server.call(
        "config/batchWrite",
        {"edits": [{"keyPath": "hooks.state", "value": values, "mergeStrategy": "upsert"}],
         "reloadUserConfig": True},
    )


def _restore_moved_trust(
    server: AppServer, entries: list[dict[str, object]], previously_trusted: list[HandlerTrustIdentity],
) -> bool:
    values: dict[str, dict[str, str]] = {}
    for entry in entries:
        if entry.get("trustStatus") != "untrusted":
            continue
        identity = _handler_identity(entry)
        if identity in previously_trusted:
            key = entry.get("key")
            if not isinstance(key, str):
                raise HookError("incomplete hooks/list entry")
            values[key] = {"trusted_hash": identity.current_hash}
    if not values:
        return False
    _trust_handlers(server, values)
    return True


def _select_hooks(entries: list[dict[str, object]]) -> dict[CodexHook, LocatedHook | MissingHook]:
    selected: dict[CodexHook, LocatedHook | MissingHook] = {hook: MissingHook(hook) for hook in HOOKS}
    for entry in entries:
        hook = next((hook for hook in HOOKS if entry.get("command") == hook.command), None)
        if (
            hook is None
            or entry.get("eventName") != hook.listed_event()
            or entry.get("matcher") != hook.matcher
            or entry.get("handlerType") != "command"
            or entry.get("timeoutSec") != hook.timeout
        ):
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
    previously_trusted = _install_file(path, binary, codex_home) if action == "install" else []
    server = AppServer(binary, codex_home)
    try:
        _initialize(server)
        listed = _listed_hook_entries(server, path)
        if previously_trusted and _restore_moved_trust(server, listed, previously_trusted):
            listed = _listed_hook_entries(server, path)
        entries = _select_hooks(listed)
        if action == "install":
            for hook in HOOKS:
                entry = entries[hook]
                if isinstance(entry, LocatedHook) and entry.trust_status in {"untrusted", "modified"}:
                    _trust_handlers(server, {entry.key: {"trusted_hash": entry.current_hash}})
                    entries = _select_hooks(_listed_hook_entries(server, path))
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
