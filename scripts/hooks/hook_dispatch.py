#!/usr/bin/env python3
"""Run an event's Python hooks in registration order in one interpreter.

Claude runs separate matching hooks in parallel without a documented order.
This dispatcher deliberately uses settings order. Exit 2 wins, discards stdout,
and joins only exit-2 stderr with a blank line. Otherwise we exit 0 and merge
successful JSON objects: permission precedence is deny > defer > ask > allow;
only winning permission reasons join with blank lines. Context joins with blank
lines, system messages with newlines. Any continue:false wins (its stop reasons
join with newlines); PostToolUse/Stop block wins (block reasons join with blank
lines). Unknown keys keep their first value. Two updatedInput values keep the
first and add a system message naming the conflicting hooks.

The docs do not specify how simultaneous stderr, permission reasons,
systemMessage, continue:false/stopReason, decision:block/reason, updatedInput,
or unknown keys combine. These ordering, joining and first-value rules are our
explicit policy for those gaps. Every additionalContext remains available.
Nonblank non-JSON stdout is copied to debug stderr and dropped; invalid JSON or
nonzero/non-2 exit also adds a hook-error system message, since a single exit 0
cannot reproduce separate transcript hook-error notices. Known JSON fields are
validated before merging; a malformed document contributes only its error.
Error messages retain the hook's whole stripped stderr (including traceback),
or "exit N" when stderr is blank. A timeout discards all output and decisions.
With no JSON and no errors, stdout stays empty.

Execution uses importlib's bytecode cache with a fresh __main__ module because
compiling the largest hooks exceeds 2 ms. Streams (including stdin.buffer), file
descriptors, argv, environment, cwd, import path and signal handlers are isolated.
Alarm calls inside a hook are multiplexed with its outer deadline, so a hook's
own SIGALRM handler and shorter budget work without cancelling our timeout.
The outer timeout in settings.json covers the largest sum of matching hook budgets
plus 30 seconds of overhead, preserving every sequential hook's full budget.
Imported library modules remain cached, which is the intended startup saving.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import NoReturn, TypedDict, cast


DISPATCH_OVERHEAD_SECONDS = 30


@dataclass(frozen=True)
class HookSpec:
    filename: str
    matcher: str | None
    timeout: float = 600


@dataclass(frozen=True)
class HookResult:
    filename: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DispatchResult:
    exit_code: int
    stdout: str
    stderr: str


PRE_TOOL_USE = (
    HookSpec("pre-tool-use-heredoc-guard.py", "Bash"),
    HookSpec("pre-tool-use-tool-output-read.py", "Bash"),
    HookSpec("pre-tool-use-showrunner-move-guard.py", "Bash"),
    HookSpec("pre-tool-use-change-review-gate.py", "Bash"),
    HookSpec("pre-tool-use-checkpoint-doc-gate.py", "Bash"),
    HookSpec("pre-tool-use-scratch-guard.py", "Bash|Write|NotebookEdit", 10),
)
POST_TOOL_USE = (
    HookSpec("post-tool-use-push-delivered.py", "PushNotification"),
    HookSpec("post-tool-use-basedpyright.py", "Edit|MultiEdit|Write"),
    HookSpec("post-tool-use-fn-length.py", "Edit|MultiEdit|Write"),
    HookSpec("post-tool-use-context-usage.py", None),
    HookSpec("post-tool-use-banned-words.py", None),
)
STOP = (
    HookSpec("stop-direct-continue.py", None),
    HookSpec("stop-conversation-pause.py", None),
    HookSpec("stop-assistant-prose-banned-words.py", None),
    HookSpec("herdr_tab_title.py", None),
)
EVENTS = {"pre_tool_use": PRE_TOOL_USE, "post_tool_use": POST_TOOL_USE, "stop": STOP}
HOOKS = Path(__file__).resolve().parent


class ToolEventPayload(TypedDict, total=False):
    tool_name: str


class HookEventIdentity(TypedDict):
    hookEventName: str


class HookSpecificOutput(HookEventIdentity, total=False):
    permissionDecision: str
    permissionDecisionReason: str
    updatedInput: dict[str, object]
    additionalContext: str
    classifierContext: str


HookOutput = TypedDict("HookOutput", {
    "hookSpecificOutput": HookSpecificOutput,
    "additionalContext": str,
    "systemMessage": str,
    "continue": bool,
    "suppressOutput": bool,
    "terminalSequence": str,
    "stopReason": str,
    "decision": str,
    "reason": str,
}, total=False)


class HookTimedOut(BaseException):
    """An outer deadline must bypass a hook's fail-open Exception handlers."""


AlarmHandler = Callable[[int, FrameType | None], object] | int | None


@contextmanager
def _alarm_budget(timeout: float) -> Generator[None]:
    """Virtualize hook alarms while retaining a non-cancellable outer deadline."""
    real_signal, real_getsignal = signal.signal, signal.getsignal
    real_setitimer, real_getitimer, real_alarm = signal.setitimer, signal.getitimer, signal.alarm
    saved_handler = real_getsignal(signal.SIGALRM)
    saved_timer = real_setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    deadline = started + timeout
    hook_handler: AlarmHandler = signal.SIG_DFL
    hook_alarm = 0.0
    hook_interval = 0.0

    def remaining() -> tuple[float, float]:
        return (max(0.0, hook_alarm - time.monotonic()) if hook_alarm else 0.0, hook_interval)

    def arm() -> None:
        next_alarm = min(deadline, hook_alarm) if hook_alarm else deadline
        _ = real_setitimer(signal.ITIMER_REAL, max(0.000001, next_alarm - time.monotonic()))

    def delivered(signum: int, frame: FrameType | None) -> None:
        nonlocal hook_alarm
        if time.monotonic() >= deadline:
            raise HookTimedOut()
        hook_alarm = time.monotonic() + hook_interval if hook_interval else 0.0
        arm()
        if callable(hook_handler):
            _ = hook_handler(signum, frame)
        elif hook_handler == signal.SIG_DFL:
            raise SystemExit(128 + signum)

    def install(signum: int, handler: AlarmHandler) -> AlarmHandler:
        nonlocal hook_handler
        if signum != signal.SIGALRM:
            return real_signal(signum, handler)
        previous, hook_handler = hook_handler, handler
        return previous

    def handler_for(signum: int) -> AlarmHandler:
        return hook_handler if signum == signal.SIGALRM else real_getsignal(signum)

    def set_timer(which: int, seconds: float, interval: float = 0.0) -> tuple[float, float]:
        nonlocal hook_alarm, hook_interval
        if which != signal.ITIMER_REAL:
            return real_setitimer(which, seconds, interval)
        if seconds < 0 or interval < 0:
            return real_setitimer(which, seconds, interval)
        previous = remaining()
        hook_alarm = time.monotonic() + seconds if seconds else 0.0
        hook_interval = interval
        arm()
        return previous

    def get_timer(which: int) -> tuple[float, float]:
        return remaining() if which == signal.ITIMER_REAL else real_getitimer(which)

    def alarm(seconds: int) -> int:
        if seconds < 0:
            raise ValueError("alarm length must be non-negative")
        return math.ceil(set_timer(signal.ITIMER_REAL, seconds)[0])

    _ = real_signal(signal.SIGALRM, delivered)
    signal.signal, signal.getsignal = install, handler_for
    signal.setitimer, signal.getitimer, signal.alarm = set_timer, get_timer, alarm
    try:
        arm()
        yield
    finally:
        _ = real_setitimer(signal.ITIMER_REAL, 0)
        signal.signal, signal.getsignal = real_signal, real_getsignal
        signal.setitimer, signal.getitimer, signal.alarm = real_setitimer, real_getitimer, real_alarm
        _ = real_signal(signal.SIGALRM, saved_handler)
        if saved_timer[0]:
            _ = real_setitimer(signal.ITIMER_REAL,
                              max(0.000001, saved_timer[0] - (time.monotonic() - started)), saved_timer[1])


def _execute(path: Path) -> None:
    specification = importlib.util.spec_from_file_location("__main__", path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load hook: {path}")
    module = importlib.util.module_from_spec(specification)
    previous = sys.modules["__main__"]
    sys.modules["__main__"] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules["__main__"] = previous


def _try_cleanup(action: Callable[[], object]) -> None:
    """A broken hook resource must not prevent restoration of other resources."""
    try:
        _ = action()
    except Exception:
        pass


def _signal_handlers() -> dict[int, AlarmHandler]:
    handlers: dict[int, AlarmHandler] = {}
    for signum in signal.valid_signals():
        if signum in (signal.SIGKILL, signal.SIGSTOP):
            continue
        try:
            handlers[signum] = signal.getsignal(signum)
        except (OSError, ValueError):
            continue
    return handlers


def _restore_handlers(handlers: dict[int, AlarmHandler]) -> None:
    for signum, handler in handlers.items():
        try:
            _ = signal.signal(signum, handler)
        except (OSError, ValueError, TypeError):
            continue


def run_hook(path: Path, payload: str, timeout: float = 600) -> HookResult:
    """Execute a script, retaining output only if it completes within budget."""
    path = path.resolve()
    streams = sys.stdin, sys.stdout, sys.stderr
    argv, environment, search_path = sys.argv, os.environ, sys.path
    original_environment, original_search_path = dict(environment), list(search_path)
    handlers = _signal_handlers()
    cwd = os.open(".", os.O_RDONLY)
    exit_code = 0
    timed_out = False
    _try_cleanup(streams[1].flush)
    _try_cleanup(streams[2].flush)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr, \
            io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8") as stdin:
        descriptors = os.dup(1), os.dup(2)
        # Hooks own these duplicate streams. Closing one flushes its output but
        # leaves our capture file open for traceback reporting and reading.
        hook_stdout = os.fdopen(os.dup(stdout.fileno()), "w", encoding="utf-8", errors="replace")
        hook_stderr = os.fdopen(os.dup(stderr.fileno()), "w", encoding="utf-8", errors="replace")
        try:
            _ = os.dup2(stdout.fileno(), 1)
            _ = os.dup2(stderr.fileno(), 2)
            sys.stdin, sys.stdout, sys.stderr = stdin, hook_stdout, hook_stderr
            sys.argv = [str(path)]
            sys.path = [str(path.parent), *original_search_path]
            try:
                with _alarm_budget(timeout):
                    _execute(path)
            except HookTimedOut:
                timed_out = True
            except SystemExit as error:
                if error.code is None:
                    exit_code = 0
                elif isinstance(error.code, int):
                    exit_code = error.code
                else:
                    exit_code = 1
                    print(error.code, file=stderr)
            except BaseException:
                exit_code = 1
                traceback.print_exc(file=stderr)
        finally:
            try:
                _try_cleanup(hook_stdout.flush)
                _try_cleanup(hook_stderr.flush)
                _try_cleanup(hook_stdout.close)
                _try_cleanup(hook_stderr.close)
                _try_cleanup(stdout.flush)
                _try_cleanup(stderr.flush)
                sys.argv, sys.path = argv, search_path
                search_path[:] = original_search_path
                os.environ = environment
                _try_cleanup(environment.clear)
                _try_cleanup(lambda: environment.update(original_environment))
                _try_cleanup(lambda: os.fchdir(cwd))
                _try_cleanup(lambda: os.close(cwd))
                _restore_handlers(handlers)
            finally:
                sys.stdin, sys.stdout, sys.stderr = streams
                for descriptor, saved in zip((1, 2), descriptors):
                    try:
                        _ = os.dup2(saved, descriptor)
                    except OSError:
                        pass
                    finally:
                        _try_cleanup(lambda: os.close(saved))
        _ = stdout.seek(0)
        _ = stderr.seek(0)
        return HookResult(path.name, 0, "", "") if timed_out else HookResult(
            path.name, exit_code, stdout.read(), stderr.read())


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _valid_fields(document: dict[str, object], strings: Sequence[str],
                  booleans: Sequence[str] = ()) -> bool:
    return (all(isinstance(document[key], str) for key in strings if key in document)
            and all(isinstance(document[key], bool) for key in booleans if key in document))


def _valid_output(document: dict[str, object], event: str) -> bool:
    """Reject the entire hook document when a documented field is malformed.

    Unknown extension keys retain their JSON values. Tool output replacement
    fields also accept arbitrary JSON: their tool-specific schema is unavailable
    here. The deprecated top-level decision value "approve" remains accepted.
    """
    if not _valid_fields(document, ("additionalContext", "systemMessage", "stopReason",
                                    "reason", "decision", "terminalSequence"),
                         ("continue", "suppressOutput")):
        return False
    if "decision" in document and document["decision"] not in ("approve", "block"):
        return False
    if "hookSpecificOutput" not in document:
        return True
    value = document["hookSpecificOutput"]
    if not isinstance(value, dict):
        return False
    specific = _mapping(cast(object, value))
    expected_event = {"pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse", "stop": "Stop"}
    if specific.get("hookEventName") != expected_event.get(event):
        return False
    if not _valid_fields(specific, ("hookEventName", "permissionDecision", "permissionDecisionReason",
                                   "additionalContext", "classifierContext")):
        return False
    if "permissionDecision" in specific and specific["permissionDecision"] not in ("allow", "ask", "defer", "deny"):
        return False
    return "updatedInput" not in specific or isinstance(specific["updatedInput"], dict)


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _merge_fields(documents: Sequence[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for document in documents:
        for key, value in document.items():
            _ = merged.setdefault(key, value)
    for key, separator in (("additionalContext", "\n\n"), ("systemMessage", "\n")):
        values = [str(document[key]) for document in documents if key in document]
        if values:
            merged[key] = separator.join(values)
    return merged


def _merge_permissions(specifics: Sequence[dict[str, object]], merged: dict[str, object]) -> None:
    ranks = {"allow": 0, "ask": 1, "defer": 2, "deny": 3}
    decisions = [item for item in specifics if item.get("permissionDecision") in ranks]
    if decisions:
        winner = max(decisions, key=lambda item: ranks[str(item["permissionDecision"])])["permissionDecision"]
        merged["permissionDecision"] = winner
        merged["permissionDecisionReason"] = "\n\n".join(
            str(item["permissionDecisionReason"]) for item in decisions
            if item["permissionDecision"] == winner and "permissionDecisionReason" in item)


def combine_results(event: str, results: Sequence[HookResult]) -> DispatchResult:
    """Combine script outcomes using the explicit policy in this module's docstring."""
    refusals = [result.stderr.rstrip("\n") for result in results if result.exit_code == 2]
    if refusals:
        return DispatchResult(2, "", "\n\n".join(refusals))
    documents: list[dict[str, object]] = []
    names: list[str] = []
    debug: list[str] = []
    for result in results:
        invalid = False
        document: dict[str, object] = {}
        if result.exit_code == 0 and result.stdout.strip():
            try:
                loaded = cast(object, json.loads(result.stdout, parse_constant=_reject_json_constant))
                if not isinstance(loaded, dict):
                    invalid = True
                else:
                    # Known JSON keys are described by HookOutput; the mapping also
                    # retains extension keys whose first values must survive merging.
                    document = _mapping(cast(HookOutput, cast(object, loaded)))
                    invalid = not _valid_output(document, event)
                    # Reject parser/encoder depth-limit disagreements per hook,
                    # before they could prevent the other hooks' JSON rendering.
                    _ = json.dumps(document, allow_nan=False)
            except (ValueError, RecursionError):
                invalid = True
                debug.append(result.stdout.rstrip("\n"))
        if result.exit_code != 0 or invalid:
            detail = result.stderr.strip() or f"exit {result.exit_code}"
            document = {"systemMessage": f"{result.filename} hook error: {detail}"}
        documents.append(document)
        names.append(result.filename)
    merged = _merge_fields(documents)
    specifics = [_mapping(item["hookSpecificOutput"]) for item in documents if "hookSpecificOutput" in item]
    if specifics:
        specific = _merge_fields(specifics)
        if event == "pre_tool_use":
            _merge_permissions(specifics, specific)
        merged["hookSpecificOutput"] = specific
        updates = [name for name, doc in zip(names, documents)
                   if "updatedInput" in _mapping(doc.get("hookSpecificOutput"))]
        if len(updates) > 1:
            conflict = f"updatedInput conflict: {', '.join(updates)}; keeping {updates[0]}"
            merged["systemMessage"] = "\n".join(
                [str(merged["systemMessage"]), conflict] if "systemMessage" in merged else [conflict])
    stopped = [item for item in documents if item.get("continue") is False]
    if stopped:
        merged["continue"] = False
        merged["stopReason"] = "\n".join(str(item["stopReason"]) for item in stopped if "stopReason" in item)
    blocked = [item for item in documents if item.get("decision") == "block"]
    if event in ("post_tool_use", "stop") and blocked:
        merged["decision"] = "block"
        merged["reason"] = "\n\n".join(str(item["reason"]) for item in blocked if "reason" in item)
    return DispatchResult(0, json.dumps(merged) if merged else "", "\n".join(debug))


def main() -> NoReturn:
    if len(sys.argv) != 2 or sys.argv[1] not in EVENTS:
        print("usage: hook_dispatch.py <pre_tool_use|post_tool_use|stop>", file=sys.stderr)
        raise SystemExit(1)
    event = sys.argv[1]
    payload = sys.stdin.read()
    try:
        parsed = cast(object, json.loads(payload))
        tool_name = cast(ToolEventPayload, cast(object, parsed)).get("tool_name", "") if isinstance(parsed, dict) else ""
    except ValueError:
        tool_name = ""
    results = [run_hook(HOOKS / spec.filename, payload, spec.timeout) for spec in EVENTS[event]
               if spec.matcher is None or re.fullmatch(spec.matcher, tool_name)]
    result = combine_results(event, results)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
