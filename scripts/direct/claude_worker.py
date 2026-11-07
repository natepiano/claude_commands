"""Start, deliver, and wait for a Claude worker in the assign process."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scripts.accounts import accounts
from scripts.agents import worker_lifecycle
from scripts.lib import conf, models_registry, nix_env
from scripts.message import sessions
from scripts.production import staff, worker_admission
from scripts.usage import agents_log

SCRIPTS = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ClaudeTurn:
    name: str
    run: Path
    cwd: Path
    prompt: Path
    summary: Path
    log: Path
    model: str
    effort: str
    role: str
    staff_id: str
    workers: bool
    env: Mapping[str, str]


@dataclass(frozen=True)
class TurnsNotCounted:
    """A new worker has no earlier turn to count."""


@dataclass(frozen=True)
class Delivered:
    native_id: str
    turns_before: int | TurnsNotCounted
    launch_environment: Mapping[str, str] = field(default_factory=dict[str, str], repr=False, compare=False)


@dataclass(frozen=True)
class Unchanged:
    """The worker already accepted this assignment."""


@dataclass(frozen=True)
class NotDelivered:
    code: int


class _LaunchFailed(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code: int = code


@contextmanager
def _environment(turn: ClaudeTurn) -> Generator[dict[str, str]]:
    # Staff, accounts, configuration, transcripts, and role history read the process environment.
    saved = dict(os.environ)
    environment = dict(turn.env)
    _ = environment.pop("STAFF_ID", None)
    environment["UNIT_RUN_DIR"] = str(turn.run)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield environment
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _binary(environment: Mapping[str, str]) -> str:
    binary = environment.get("CLAUDE_BIN", "")
    if not binary:
        preferred = Path.home() / ".local/bin/claude"
        binary = str(preferred) if os.access(preferred, os.X_OK) else shutil.which("claude", path=environment.get("PATH")) or ""
    if not binary:
        print("claude_worker: no claude binary found.", file=sys.stderr)
        raise _LaunchFailed(127)
    return binary


def _validate(turn: ClaudeTurn, environment: Mapping[str, str]) -> str:
    binary = _binary(environment)
    if not turn.prompt.is_file():
        print(f"claude_worker: prompt file not found: {turn.prompt}", file=sys.stderr)
        raise _LaunchFailed(2)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", turn.name):
        print(f"claude_worker: invalid mesh name '{turn.name}'.", file=sys.stderr)
        raise _LaunchFailed(2)
    return binary


def _stop_unpublished(turn: ClaudeTurn, binary: str, identity: str, environment: Mapping[str, str]) -> None:
    try:
        stopped = subprocess.run([binary, "stop", identity], env=environment, check=False)
        if stopped.returncode:
            print(f"staff: {turn.name}: {identity}: cleanup unproved", file=sys.stderr)
    except OSError:
        print(f"staff: {turn.name}: {identity}: cleanup unproved", file=sys.stderr)


def _banner_identity(banner: str) -> str:
    return next((match.group() for line in banner.splitlines() if "backgrounded" in line
                 if (match := re.search(r"\b[0-9a-f]{8}\b", line))), "")


def _start(turn: ClaudeTurn, binary: str, environment: dict[str, str]) -> str:
    try:
        selected = accounts.account_for_unscoped_launch(environment, workers=turn.workers)
    except accounts.AccountsError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise _LaunchFailed(1) from error
    if isinstance(selected, accounts.LegacyHomeLaunch):
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR"):
            _ = environment.pop(name, None)
    else:
        environment["CLAUDE_CONFIG_DIR"] = str(selected.dir)
        environment["CLAUDE_CODE_REMOTE_MEMORY_DIR"] = str(Path.home() / ".claude")
    _ = environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
        _ = os.environ.pop(name, None)
        if name in environment:
            os.environ[name] = environment[name]
    loaded = nix_env.ensure(turn.cwd, base_env=environment)
    if isinstance(loaded, nix_env.Refused):
        print(loaded.reason, file=sys.stderr)
        raise _LaunchFailed(1)
    settings = {name: value for name, value in loaded.env.items()
                if not name.startswith(("HERDR_", "CODEX_", "CLAUDE_CODE_MESSAGING_"))
                and name not in {"CLAUDE_CODE_BRIDGE_SESSION_ID", "TMUX", "TMUX_PANE"}}
    settings.update({name: "" for name in environment if name not in loaded.env})
    settings.update({name: environment.get(name, "") for name in ("UNIT_RUN_DIR", "UNIT_TEAM_ROLE", "MODELS_CONFIG_FILE")})
    settings.update({name: environment[name] for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR")
                     if name in environment})
    _ = settings.pop("CLAUDE_CODE_CHILD_SESSION", None)
    arguments = [binary, "--bg", "--name", turn.name, "--dangerously-skip-permissions", "--settings",
                 json.dumps({"sandbox": {"enabled": False}, "env": models_registry.agent_environment(settings)})]
    if turn.model:
        arguments += ["--model", turn.model]
    if turn.effort:
        arguments += ["--effort", turn.effort]
    start_env = models_registry.agent_environment(loaded.env)
    for name in ("UNIT_RUN_DIR", "UNIT_TEAM_ROLE", "MODELS_CONFIG_FILE", "UNIT_RESOLVES_ROUND", "CLAUDE_CODE_CHILD_SESSION"):
        _ = start_env.pop(name, None)
    if isinstance(loaded, nix_env.Loaded):
        start_env["NIX_ENV_ENVRC"] = str(loaded.envrc.resolve())
    else:
        _ = start_env.pop("NIX_ENV_ENVRC", None)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output:
            process = subprocess.Popen(arguments, cwd=turn.cwd, env=start_env, stdout=output, stderr=subprocess.STDOUT)
            try:
                _ = process.wait()
            except BaseException:
                _ = process.wait()
                _ = output.seek(0)
                identity = _banner_identity(output.read())
                if identity:
                    _stop_unpublished(turn, binary, identity, environment)
                raise
            _ = output.seek(0)
            banner = output.read()
    except OSError as error:
        banner = str(error)
    identity = _banner_identity(banner)
    if not identity:
        _ = turn.log.write_text(banner + ("" if banner.endswith("\n") else "\n"))
        print("claude_worker: could not start a background session; banner follows.", file=sys.stderr)
        print(banner, file=sys.stderr)
        raise _LaunchFailed(1)
    return identity


def _registered(identity: str, seconds: int) -> str:
    deadline = time.monotonic() + seconds
    while True:
        registry = sessions.read_registry()
        if registry.unreadable:
            raise worker_admission.AdmissionRefused("staff: registry is incomplete; native identity not published")
        found = [item.record for item in registry.entries
                 if item.record["sessionId"].startswith(identity) and sessions.running_session(item.record)]
        remaining = worker_lifecycle.effective_deadline(deadline) - time.monotonic()
        if found or remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
    if len(found) != 1:
        raise worker_admission.AdmissionRefused("staff: backend has no unique live registry identity")
    if staff.runtime_id(found[0]["pid"]) != found[0]["sessionId"]:
        raise worker_admission.AdmissionRefused("staff: Claude process changed during publication")
    return found[0]["sessionId"]


def _transcript_count(native_id: str) -> int:
    paths = sessions.transcript_locations(native_id)
    count = 0
    if paths:
        try:
            for line in paths[0].read_text().splitlines():
                try:
                    row = cast(object, json.loads(line))
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                record = cast(dict[str, object], row)
                message = record.get("message")
                if (record.get("type") == "assistant" and isinstance(message, dict)
                        and cast(dict[str, object], message).get("stop_reason") in ("end_turn", "stop_sequence")):
                    count += 1
        except OSError:
            pass
    return count


def _deliver(turn: ClaudeTurn, native_id: str, environment: Mapping[str, str]) -> None:
    if turn.role:
        try:
            agents_log.append(native_id, cast(agents_log.Role, turn.role), None, datetime.now(timezone.utc))
        except Exception as error:
            print("agents_log: " + " ".join(str(error).splitlines()), file=sys.stderr)
    script = environment.get("CLAUDE_WORKER_SEND_SCRIPT") or str(SCRIPTS / "message/send.py")
    result = subprocess.run([str(SCRIPTS / "lib/py"), script, "--to", f"session:{native_id}", "--from", "launcher",
                             "--file", str(turn.prompt)], env=environment, text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode:
        raise _LaunchFailed(2)
    if not result.stdout.startswith(("SENT:", "SKIPPED:")):
        print(f"claude_worker: opening prompt was not delivered: {result.stdout.strip()}", file=sys.stderr)
        raise _LaunchFailed(2)


def open_worker(turn: ClaudeTurn) -> Delivered | Unchanged | NotDelivered:
    """Publish a worker before delivering its assignment."""
    with _environment(turn) as environment:
        binary = ""
        created = ""
        published = False
        delivered: Delivered | Unchanged = Unchanged()
        prepared = turn
        try:
            binary = _validate(turn, environment)
            allowed = worker_admission.admit(turn.name, turn.run, reference=turn.staff_id)
            request = worker_admission.assignment_id(turn.prompt)
            register_seconds = conf.read("direct").whole("CLAUDE_WORKER_REGISTER_WAIT_SECS")

            def start(handoff: worker_admission.FamilyHandoff) -> str:
                nonlocal created, prepared
                if isinstance(handoff, worker_admission.TranscriptHandoff):
                    opening = turn.run / f"{allowed.lock_reference}.opening.md"
                    _ = opening.write_text(handoff.opening(turn.prompt.read_text()))
                    prepared = replace(turn, prompt=opening)
                created = _start(prepared, binary, environment)
                return _registered(created, register_seconds)

            def dispatch(worker: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
                nonlocal published, prepared, delivered
                published = True
                if not created:
                    if worker_admission.dispatch_record(allowed.run, allowed.name, report_anomaly=False).state is worker_admission.SwitchState.DISPATCH_IN_FLIGHT:
                        try:
                            source = worker_admission.transcript_source(worker.row, staff.ClaudeBackend())
                        except ValueError as error:
                            if str(error) != "no source transcript found":
                                raise
                            entries = ()
                        else:
                            entries = worker_admission.transcript_entries(source, set())
                        torn = False
                        for entry in entries:
                            if isinstance(entry, worker_admission.TornTranscriptRecord):
                                torn = True
                            elif entry.startswith("user:\n") and entry.endswith(turn.prompt.read_text()):
                                print("unchanged")
                                return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED
                        if torn:
                            raise worker_admission.AdmissionRefused("Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused")
                    if isinstance(worker.handoff, worker_admission.TranscriptHandoff):
                        opening = turn.run / f"{allowed.lock_reference}.opening.md"
                        _ = opening.write_text(worker.handoff.opening(turn.prompt.read_text()))
                        prepared = replace(turn, prompt=opening)
                before = TurnsNotCounted() if created else _transcript_count(worker.row.current_session)
                worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT,
                                                 expected_request=request)
                _deliver(prepared, worker.row.current_session, environment)
                delivered = Delivered(worker.row.current_session, before, dict(environment))
                return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

            _ = worker_admission.switch_worker(allowed, "claude", start=start, dispatch=dispatch,
                                               cleanup=lambda identity: None, request=request)
            return delivered
        except _LaunchFailed as error:
            return NotDelivered(error.code)
        except (worker_admission.AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused, OSError) as error:
            print(str(error), file=sys.stderr)
            return NotDelivered(2)
        finally:
            if created and not published:
                _stop_unpublished(turn, binary, created, environment)


def follow_up(turn: ClaudeTurn, native_id: str) -> Delivered | NotDelivered:
    """Count completed replies before delivering the next assignment."""
    with _environment(turn) as environment:
        try:
            _ = _validate(turn, environment)
            before = _transcript_count(native_id)
            _deliver(turn, native_id, environment)
            return Delivered(native_id, before)
        except _LaunchFailed as error:
            return NotDelivered(error.code)


def _status(binary: str, identity: str, environment: Mapping[str, str]) -> str:
    try:
        result = subprocess.run([binary, "agents", "--json"], env=environment, capture_output=True, text=True, check=False)
        rows = cast(list[dict[str, str]], json.loads(result.stdout))
        for row in rows:
            if row.get("id") == identity:
                state = row.get("state", "")
                return row.get("status") or {"working": "busy", "done": "idle", "blocked": "idle"}.get(state, state or "unknown")
        return "gone"
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def _refresh_log(turn: ClaudeTurn, binary: str, identity: str, environment: Mapping[str, str]) -> None:
    temporary = Path(str(turn.log) + ".tmp")
    try:
        with temporary.open("w") as output:
            result = subprocess.run([binary, "logs", identity], env=environment, stdout=output,
                                    stderr=subprocess.DEVNULL, check=False)
        if not result.returncode:
            _ = temporary.replace(turn.log)
    except OSError:
        pass


def _summary(turn: ClaudeTurn) -> None:
    if turn.summary.exists() and turn.summary.stat().st_size:
        return
    try:
        text = turn.log.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        text = ""
    if text:
        plain = re.sub(r"\x1b\].*?(?:\x07|\x1b\\|\Z)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[0-~]", "", text, flags=re.DOTALL)
        tail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", plain)[-4000:]
        if tail.strip():
            _ = turn.summary.write_text(tail, encoding="utf-8")
    if not turn.summary.exists() or not turn.summary.stat().st_size:
        _ = turn.summary.write_text(f"The background agent {turn.name} produced no summary.\n")


def wait_for_turn(turn: ClaudeTurn, delivered: Delivered) -> int:
    """Read native state and logs until the delivered turn finishes."""
    with _environment(replace(turn, env=delivered.launch_environment or turn.env)) as environment:
        binary = _binary(environment)
        seconds = float(environment.get("UNIT_WORKER_POLL_SECS") or conf.read("direct").text("CLAUDE_WORKER_POLL_SECS"))
        identity = delivered.native_id[:8]
        seen_busy = False
        while True:
            status = _status(binary, identity, environment)
            _refresh_log(turn, binary, identity, environment)
            if status in ("busy", "shell", "prompting"):
                seen_busy = True
            elif status == "idle":
                if isinstance(delivered.turns_before, int):
                    if _transcript_count(delivered.native_id) > delivered.turns_before:
                        break
                elif seen_busy:
                    break
            elif status == "gone":
                if isinstance(delivered.turns_before, int):
                    print(f"claude_worker: {turn.name} vanished after delivery", file=sys.stderr)
                    return 1
                break
            time.sleep(seconds)
        _refresh_log(turn, binary, identity, environment)
        _summary(turn)
        return 0
