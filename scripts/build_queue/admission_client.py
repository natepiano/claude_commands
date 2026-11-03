"""Admission socket client, with bounded queries and replay-safe acquisitions."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import errno
import math
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, Callable, Protocol, cast
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import admission_code
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_service import SystemClock
from scripts.lib.user_zone import user_zone
from scripts.lint import memory_admit as gate

if TYPE_CHECKING:
    from scripts.lib import conf
else:
    import conf

QUERY_TIMEOUT_SECONDS = 0.5
LAUNCH_WAIT_LIMIT_REASON = "launch wait limit reached without admission"
LOADED_ADMISSION_CODE_VERSION = os.environ.get("BUILDLOG_ADMISSION_CODE_VERSION") or admission_code.admission_code_version()


class ClientClock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


CLOCK: ClientClock = SystemClock()


class TransportUnavailable(ConnectionError):
    pass


class CheckerFailure(ValueError):
    pass


def _whole_seconds(key: str, fallback: int) -> int:
    raw = os.environ.get(key, str(fallback))
    return int(raw) if raw.isdecimal() else fallback


def _connect(timeout: float) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(wire.admission_socket_path()))
    except BaseException:
        connection.close()
        raise
    return connection


def _line(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
    while b"\n" not in pending:
        left = deadline - CLOCK.monotonic()
        if left <= 0:
            raise TimeoutError("no answer")
        connection.settimeout(left)
        chunk = connection.recv(65536)
        if not chunk:
            raise TransportUnavailable("closed connection")
        pending.extend(chunk)
    end = pending.index(b"\n")
    line = bytes(pending[:end])
    del pending[:end + 1]
    return line


def _query(request: wire.ViewRequest | wire.ExplainRequest | wire.MineRequest) -> wire.Reply:
    request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
    deadline = CLOCK.monotonic() + QUERY_TIMEOUT_SECONDS
    try:
        for main_service in (False, True):
            with _connect(QUERY_TIMEOUT_SECONDS) as connection:
                connection.sendall(wire.encode_request(request, main_service=main_service).encode())
                line = _line(connection, bytearray(), deadline)
                reply = wire.parse_reply(line, request)
                if isinstance(reply, wire.Refused):
                    if not main_service and reply.reason == "invalid snapshot record fields":
                        continue
                    raise wire.AdmissionUnavailable(reply.reason)
                return reply
    except FileNotFoundError as error:
        raise wire.AdmissionUnavailable("no socket") from error
    except ConnectionRefusedError as error:
        raise wire.AdmissionUnavailable("connect refused") from error
    except TimeoutError as error:
        raise wire.AdmissionUnavailable("no answer in 0.5 s") from error
    except (OSError, ValueError) as error:
        raise wire.AdmissionUnavailable(str(error) or "invalid answer") from error


@dataclass
class EstimateConnection:
    connection: socket.socket
    pending: bytearray


_ESTIMATE_CONNECTION: ContextVar[EstimateConnection | None] = ContextVar("estimate_connection", default=None)


def build_history_generation(configuration: gate.MemoryConfig | None = None) -> gate.BuildHistoryGeneration:
    """Read the generation before reading build history."""
    reply = _call(wire.BuildHistoryRequest(), lambda _wait: None, channel=_ESTIMATE_CONNECTION.get(),
                  configuration=configuration)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Refused):
        raise CheckerFailure(reply.reason)
    if not isinstance(reply, wire.BuildHistoryReply) or reply.generation < 0:
        raise CheckerFailure("invalid build history generation answer")
    return gate.BuildHistoryGeneration(reply.generation)


def view() -> gate.QueueRead:
    reply = _query(wire.ViewRequest())
    if not isinstance(reply, wire.ViewReply):
        raise wire.AdmissionUnavailable("invalid view answer")
    gate.warn_configuration_diagnostics(reply.reading.diagnostics)
    return reply.reading


def explain() -> gate.QueueExplanation:
    reply = _query(wire.ExplainRequest())
    if not isinstance(reply, wire.ExplainReply):
        raise wire.AdmissionUnavailable("invalid explanation answer")
    gate.warn_configuration_diagnostics(reply.diagnostics)
    return reply.explanation


def mine(session_id: str) -> gate.QueueRead:
    reply = _query(wire.MineRequest(session_id))
    if not isinstance(reply, wire.MineReply):
        raise wire.AdmissionUnavailable("invalid step answer")
    gate.warn_configuration_diagnostics(reply.reading.diagnostics)
    return reply.reading


def _refreshed(first: wire.AdmitRequest | wire.LaunchRequest,
               refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest], *,
               channel: EstimateConnection | None = None
               ) -> wire.AdmitRequest | wire.LaunchRequest:
    token = _ESTIMATE_CONNECTION.set(channel)
    try:
        updated = refresh()
    finally:
        _ESTIMATE_CONNECTION.reset(token)
    # A refresh changes estimates only, never the step's identity or place.
    if isinstance(first, wire.AdmitRequest) and isinstance(updated, wire.AdmitRequest):
        return replace(first, profile=updated.profile, fresh_test=updated.fresh_test)
    if isinstance(first, wire.LaunchRequest) and isinstance(updated, wire.LaunchRequest):
        return replace(first, profile=updated.profile)
    raise CheckerFailure("refresh changed request kind")


@dataclass(frozen=True)
class LeftQueue:
    pass


class _QueueLeft(Exception):
    pass


@dataclass
class AcquisitionRetry:
    deadline: float
    launch_without_wait: bool = False
    next_leave: float = float("inf")
    last_outage_line: float = float("-inf")
    leave_interval: float = float("inf")

    def start_leave_schedule(self, interval: float) -> None:
        self.leave_interval = interval
        if self.next_leave == float("inf") and (
                os.environ.get("LINT_QUEUE_LEAVE_HOOK") or os.environ.get("BUILDLOG_MAC_IDLE_HOOK")):
            self.next_leave = CLOCK.monotonic() + self.leave_interval

    def left_queue(self) -> bool:
        if CLOCK.monotonic() < self.next_leave:
            return False
        self.next_leave = CLOCK.monotonic() + self.leave_interval
        return _leave_queue()

    def bound(self, seconds: float) -> float:
        return max(0.0, min(seconds, self.deadline - CLOCK.monotonic(), self.next_leave - CLOCK.monotonic()))


_ACQUISITION_RETRY: ContextVar[AcquisitionRetry | None] = ContextVar("acquisition_retry", default=None)


def _no_attempt_hook() -> None:
    pass


@dataclass(frozen=True)
class AdmissionHoldCheck:
    wait_until_free: Callable[[], None]
    admits: Callable[[], bool]

    def __call__(self) -> None:
        self.wait_until_free()


def _leave_queue() -> bool:
    hook = os.environ.get("LINT_QUEUE_LEAVE_HOOK") or os.environ.get("BUILDLOG_MAC_IDLE_HOOK")
    return bool(hook and subprocess.run(["/bin/sh", "-c", hook], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0)


def _refused(reason: str, *, checker: bool = False) -> wire.Refused:
    text = f"memory admission check failed ({reason}); step refused" if checker else reason
    print(text, file=sys.stderr)
    return wire.Refused(reason)


def _call(request: wire.Request, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None = None,
          *, attempt: Callable[[], None] = _no_attempt_hook, channel: EstimateConnection | None = None,
          configuration: gate.MemoryConfig | wire.CleanupTiming | None = None
          ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok | wire.BuildHistoryReply | LeftQueue:
    acquisition = isinstance(request, (wire.AdmitRequest, wire.LaunchRequest, wire.BuildHistoryRequest))
    retry = _ACQUISITION_RETRY.get() if acquisition else None
    if retry is None:
        limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
        launch = isinstance(request, wire.LaunchRequest)
        retry = AcquisitionRetry(CLOCK.monotonic() + limit if launch and limit > 0 else float("inf"),
                                 launch_without_wait=launch and limit == 0)
    token = _ACQUISITION_RETRY.set(retry)
    try:
        resolved = configuration if configuration is not None else gate.read_memory_config(report=False)
        request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
        reply = _acquire(request, on_wait, refresh, attempt=attempt, channel=channel, retry=retry,
                         configuration=resolved)
        if not isinstance(reply, wire.Refused) and isinstance(resolved, gate.MemoryConfig):
            gate.warn_configuration_diagnostics(resolved.diagnostics)
        return reply
    except _QueueLeft:
        return LeftQueue()
    except conf.Unusable as error:
        return _refused(str(error), checker=True)
    finally:
        _ACQUISITION_RETRY.reset(token)


def _acquire(request: wire.Request, on_wait: Callable[[wire.Wait], None],
             refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
             *, attempt: Callable[[], None], channel: EstimateConnection | None, retry: AcquisitionRetry,
             configuration: gate.MemoryConfig | wire.CleanupTiming
             ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok | wire.BuildHistoryReply | LeftQueue:
    first = request
    main_service = False
    leave_interval = (configuration.running_pass_seconds if isinstance(configuration, gate.MemoryConfig)
                      else configuration.reconnect_seconds)
    generation_query = isinstance(first, wire.BuildHistoryRequest)
    if generation_query:
        retry.start_leave_schedule(leave_interval)
    launch = isinstance(first, wire.LaunchRequest)
    failure_limit = _whole_seconds("BUILDLOG_MEM_CHECK_RETRY_S", 60)
    had_wait = False
    refresh_pending = False
    failed_since: float | None = None  # An external transport/checker boundary, not stored domain state.
    restarted = False
    last_valid = CLOCK.monotonic()
    last_silence_notice = last_valid
    while True:
        if retry.left_queue():
            return LeftQueue()
        if CLOCK.monotonic() >= retry.deadline:
            return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
        attempt()
        try:
            timeout = configuration.silence_seconds
            if generation_query:
                timeout = min(timeout, QUERY_TIMEOUT_SECONDS)
            timeout = retry.bound(timeout)
            active_channel, channel = channel, None
            with nullcontext(active_channel.connection) if active_channel is not None else _connect(timeout) as connection:
                send_timeout = retry.bound(timeout)
                if active_channel is not None or send_timeout < timeout:
                    connection.settimeout(send_timeout)
                connection.sendall(wire.encode_request(request, main_service=main_service).encode())
                pending = active_channel.pending if active_channel is not None else bytearray()
                while True:
                    deadline = max(last_valid, last_silence_notice) + configuration.silence_seconds
                    if generation_query:
                        deadline = min(deadline, CLOCK.monotonic() + QUERY_TIMEOUT_SECONDS)
                    try:
                        line = _line(connection, pending, min(deadline, retry.deadline, retry.next_leave))
                    except TimeoutError:
                        if CLOCK.monotonic() >= retry.next_leave:
                            if retry.left_queue():
                                return LeftQueue()
                            if CLOCK.monotonic() < min(deadline, retry.deadline):
                                continue
                        if CLOCK.monotonic() >= retry.deadline:
                            return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                        if generation_query:
                            raise
                        if retry.launch_without_wait:
                            return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                        _silence_notice(last_valid)
                        last_silence_notice = CLOCK.monotonic()
                        continue
                    try:
                        reply = wire.parse_reply(line, request)
                    except ValueError as error:
                        raise CheckerFailure(str(error)) from error
                    if CLOCK.monotonic() > retry.deadline:
                        return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    if isinstance(reply, wire.Refused):
                        if not main_service and reply.reason == "invalid snapshot record fields":
                            main_service = True
                            break
                        if generation_query:
                            return reply
                        return _refused(reply.reason, checker=isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)))
                    if generation_query:
                        if retry.left_queue():
                            return LeftQueue()
                        if isinstance(reply, wire.BuildHistoryReply):
                            return reply
                        if isinstance(reply, wire.Wait):
                            continue
                        raise CheckerFailure("invalid build history generation answer")
                    if isinstance(reply, (wire.Admitted, wire.LaunchAdmitted, wire.Ok)):
                        if (isinstance(first, wire.AdmitRequest) and not isinstance(reply, wire.Admitted)
                                or isinstance(first, wire.LaunchRequest) and not isinstance(reply, wire.LaunchAdmitted)
                                or isinstance(first, (wire.ReleaseRequest, wire.WidthRequest, wire.AdmitHeadRequest))
                                and not isinstance(reply, wire.Ok)):
                            raise CheckerFailure("admission outcome does not match request")
                        try:
                            if isinstance(attempt, AdmissionHoldCheck):
                                if not attempt.admits() and isinstance(reply, wire.Admitted):
                                    _ = release(reply.reservation, 1, reply.cleanup_timing)
                                    if not isinstance(first, wire.AdmitRequest):
                                        raise CheckerFailure("grant does not match admission request")
                                    first = replace(first, episode=uuid.uuid4().hex)
                                    request = replace(cast(wire.AdmitRequest, request), episode=first.episode)
                                    break
                            else:
                                attempt()
                        except BaseException:
                            if isinstance(reply, wire.Admitted):
                                _ = release(reply.reservation, 1, reply.cleanup_timing)
                            raise
                        return reply
                    if not isinstance(reply, wire.Wait):
                        raise CheckerFailure("invalid admission answer")
                    if isinstance(reply.reason, gate.ReadingUnavailable):
                        raise CheckerFailure(reply.reason.reason)
                    if launch and retry.launch_without_wait:
                        return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    retry.start_leave_schedule(leave_interval)
                    had_wait = True
                    failed_since = None
                    last_valid = CLOCK.monotonic()
                    gate.warn_configuration_diagnostics(reply.snapshot.diagnostics)
                    on_wait(reply)
                    if retry.left_queue():
                        return LeftQueue()
                    if CLOCK.monotonic() >= retry.deadline:
                        return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    if isinstance(reply.reason, gate.EstimateExpired):
                        if refresh is None or not isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)):
                            raise CheckerFailure("estimate cannot be refreshed")
                        refresh_pending = True
                        try:
                            request = replace(_refreshed(first, refresh, channel=EstimateConnection(connection, pending)),
                                              code_version=LOADED_ADMISSION_CODE_VERSION)
                            refresh_pending = False
                        except (_QueueLeft, OSError, TransportUnavailable):
                            raise
                        except Exception as error:
                            raise CheckerFailure(str(error)) from error
                        break
        except CheckerFailure as error:
            if generation_query:
                return wire.Refused(str(error) or "checker returned an error")
            reason = str(error) or "checker returned an error"
            if not had_wait:
                return _refused(reason, checker=True)
            if failed_since is None:
                failed_since = CLOCK.monotonic()
            while True:
                if CLOCK.monotonic() - failed_since >= failure_limit:
                    return _refused(reason, checker=True)
                if retry.left_queue():
                    return LeftQueue()
                delay = min(configuration.reconnect_seconds, failure_limit - (CLOCK.monotonic() - failed_since))
                CLOCK.sleep(retry.bound(delay))
                if retry.left_queue():
                    return LeftQueue()
                if CLOCK.monotonic() >= retry.deadline:
                    return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                print(f"memory admission check failed ({reason}); trying again, the step keeps its place", file=sys.stderr)
                if not refresh_pending or refresh is None or not isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)):
                    break
                try:
                    request = replace(_refreshed(first, refresh), code_version=LOADED_ADMISSION_CODE_VERSION)
                    refresh_pending = False
                    break
                except _QueueLeft:
                    raise
                except Exception as refresh_error:
                    reason = str(refresh_error) or "checker returned an error"
        except (OSError, TransportUnavailable) as error:
            if retry.launch_without_wait or CLOCK.monotonic() >= retry.deadline:
                return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
            if isinstance(error, TransportUnavailable) or error.errno in (errno.ECONNRESET, errno.EPIPE):
                if not restarted:
                    print("admission service restarted; waiting again", file=sys.stderr)
                    restarted = True
                continue
            if CLOCK.monotonic() - retry.last_outage_line >= (configuration.outage_notice_seconds if isinstance(configuration, gate.MemoryConfig) else configuration.silence_seconds):
                reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused" if isinstance(error, ConnectionRefusedError) else str(error)
                print(f"admission service unavailable: {reason}; waiting again", file=sys.stderr)
                retry.last_outage_line = CLOCK.monotonic()
            CLOCK.sleep(retry.bound(configuration.reconnect_seconds))


def admit(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest], *,
          _attempt: Callable[[], None] = _no_attempt_hook) -> wire.Admitted | wire.Refused | LeftQueue:
    return cast(wire.Admitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh, attempt=_attempt))


def launch(request: wire.LaunchRequest, on_wait: Callable[[wire.Wait], None],
           refresh: Callable[[], wire.LaunchRequest]) -> wire.LaunchAdmitted | wire.Refused | LeftQueue:
    return cast(wire.LaunchAdmitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh))


def _silence_notice(last_valid: float) -> None:
    since = datetime.fromtimestamp(CLOCK.now() - (CLOCK.monotonic() - last_valid), user_zone()).strftime("%H:%M")
    print(f"admission service not answering since {since}", file=sys.stderr)


def _mutation(request: wire.ReleaseRequest | wire.WidthRequest | wire.AdmitHeadRequest | wire.PrioritySetRequest | wire.PriorityClearRequest | wire.TokenWaitRequest,
              configuration: gate.MemoryConfig | wire.CleanupTiming | None = None) -> wire.Ok | wire.Refused:
    return cast(wire.Ok | wire.Refused, _call(request, lambda waiting: None, configuration=configuration))


def release(reservation: str, status: int, configuration: gate.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.ReleaseRequest(uuid.uuid4().hex, reservation, status), configuration)


def width(reservation: str, width: int) -> wire.Ok | wire.Refused:
    return _mutation(wire.WidthRequest(uuid.uuid4().hex, reservation, width))


def admit_head(why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.AdmitHeadRequest(uuid.uuid4().hex, why, actor))


def priority_set(unit: str, why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.PrioritySetRequest(uuid.uuid4().hex, unit, why, actor))


def priority_clear(actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.PriorityClearRequest(uuid.uuid4().hex, actor))


def token_wait(reservation: str, configuration: gate.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.TokenWaitRequest(uuid.uuid4().hex, reservation), configuration)


def wait_reasons(result: gate.AdmissionWaitSnapshot) -> tuple[str, ...]:
    """Describe why admission holds, without treating changing samples as a new reason."""
    decision = result.decision
    if isinstance(decision.pressure, gate.MemoryPressure):
        if decision.pressure.next_start_at:
            return ("memory pressure recovery",)
        if decision.pressure.swap_used_percent > result.policy.pressure.swap_used_limit:
            return ("swap memory pressure",)
        return ("memory pressure",)
    if isinstance(result.head_reason, gate.BehindOlderFit):
        return (gate.older_fit_text(result.head_reason),)
    reasons: list[str] = []
    if result.head_need:
        reasons.append(f"queue head {result.head_label}")
    if decision.charged + result.peak.bytes > decision.budget:
        reasons.append("build memory budget")
    if result.available < decision.threshold:
        reasons.append("free memory")
    return tuple(reasons) or ("admission refresh",)


@dataclass
class WaitOutput:
    started: float = field(default_factory=lambda: CLOCK.monotonic())
    deadline_reached: bool = False
    next_notice: float = 0.0
    held: bool = False
    last_reasons: tuple[str, ...] = ()
    last_need: int = 0
    last_position: int = 0
    on_event: Callable[[str], None] = field(default=lambda _event: None)

    def __call__(self, reply: wire.Wait) -> None:
        now = CLOCK.monotonic()
        first = not self.held
        reasons = wait_reasons(reply.snapshot)
        changed = (reasons != self.last_reasons or reply.snapshot.peak.bytes != self.last_need
                   or reply.position != self.last_position)
        if first:
            self.next_notice, self.held = now + gate.read_memory_config(report=False).wait_reply_seconds, True
            self.on_event("first")
        elif now < self.next_notice and not changed:
            return
        elif now >= self.next_notice:
            self.next_notice = now + gate.read_memory_config(report=False).wait_reply_seconds
        self.last_reasons, self.last_need = reasons, reply.snapshot.peak.bytes
        snapshot = reply.snapshot
        decision, peak = snapshot.decision, snapshot.peak
        subject = f"{snapshot.repo} {snapshot.step}".strip()
        count = peak.count
        descriptions = {"measured": f"the 90th percentile of {count} measured runs",
            "buildlog": f"0.65 of the 90th percentile peak of {count} runs",
            "pooled": f"the 90th percentile of {count} measured runs of this step in every repo",
            "shape": ("the same-shape fallback: no earlier builds" if not count else
                      f"a same-shape promise from {count} earlier builds")}
        program = "the merge gate" if snapshot.step == "merge-gate" else snapshot.step
        description = (f"{program}'s memory cap ({peak.bytes / gate.GIB:.1f} GiB)" if peak.source == "cap" else
                       descriptions.get(peak.source, "no history, so the fallback"))
        prefix = ("waiting for memory since " + datetime.fromtimestamp(CLOCK.now(), user_zone()).strftime("%H:%M %Z")
                  if first else f"still waiting for memory ({int(now - self.started) // 60} min)")
        text = (f"{prefix}: the machine has {snapshot.available / gate.GIB:.1f} GiB free; "
            f"a build starts at {decision.threshold / gate.GIB:.1f} ({subject}: {peak.bytes / gate.GIB:.3f} GiB needed")
        if first:
            text += f", {description}"
        if reply.position == 1 and self.last_position != 1:
            text += "; this step is now first in line"
        self.last_position = reply.position
        if snapshot.head_need:
            text += f"; head {snapshot.head_label} needs {snapshot.head_need / gate.GIB:.1f} GiB"
        summary = ", ".join(f"{Path(item.folder).name} {item.step} in {item.folder}" for item in snapshot.running)
        reason_text = (gate.head_wait_text(snapshot.head_reason, user_zone(), snapshot.policy)
            if isinstance(snapshot.head_reason, gate.HeadAdmissionStatus) else
            gate.older_fit_text(snapshot.head_reason) if isinstance(snapshot.head_reason, gate.BehindOlderFit) else
            gate.pressure_text(decision.pressure, snapshot.policy) if isinstance(decision.pressure, gate.MemoryPressure) else "")
        summary = "; ".join(part for part in (reason_text, summary) if part)
        if not snapshot.running and summary:
            text += "; " + summary
        else:
            text += (f"; {decision.promised / gate.GIB:.1f} GiB promised, {decision.measured / gate.GIB:.1f} GiB measured "
                     f"across {len(snapshot.running)} running steps")
            if snapshot.running:
                text += ": " + summary
        if decision.reserve:
            text += f"; {decision.reserve / gate.GIB:.1f} GiB kept for earlyoom"
        print(text + ")", file=sys.stderr, flush=True)

    def elapsed_seconds(self) -> int:
        return int(CLOCK.monotonic() - self.started) if self.held or self.deadline_reached else 0

    def finished(self, *, left: bool = False) -> None:
        if self.held:
            seconds = self.elapsed_seconds()
            text = (f"another machine is free after {seconds // 60} min {seconds % 60} s: leaving the memory wait" if left else
                    f"memory free after {seconds // 60} min {seconds % 60} s: starting")
            print(text, file=sys.stderr, flush=True)


def parse_run_identity(value: str) -> wire.RunIdentity | wire.NoRun:
    """Parse the run identity at the client boundary."""
    if sys.platform == "darwin":
        return wire.NO_RUN
    pid, separator, start = value.partition(":")
    if separator and pid.isascii() and pid.isdecimal() and int(pid) > 0 and start.isascii() and start.isdecimal():
        return wire.RunIdentity(int(pid), start)
    return wire.NO_RUN


def step_request(argv: list[str], cwd: str, owner: wire.Owner, since: float, sidecar: str,
                 episode: str, profile: gate.InferFromArgv | wire.Profile = gate.INFER_FROM_ARGV,
                 *, run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
                 ) -> wire.AdmitRequest:
    config = gate.read_memory_config()
    estimate = gate.step_estimate(argv, cwd, profile, lambda: datetime.fromtimestamp(CLOCK.now(), UTC),
                                  gate.store.root(), config.policy, first_peak_bytes=config.program_first_peaks["cargo"],
                                  budget=gate.resolve_admission_limits(gate.UnknownHostTotal(), config).budget,
                                  build_history_generation=lambda: build_history_generation(config))
    width_value = gate.test_threads(argv)
    width_value = width_value or (int(os.environ["NEXTEST_TEST_THREADS"]) if os.environ.get("NEXTEST_TEST_THREADS", "").isdecimal() else None)
    width_value_typed: gate.TestWidth = (gate.RequestedTestWidth(width_value) if width_value else
        gate.UnspecifiedTestWidth() if estimate.profile.step == "nextest" else gate.NoTestWidth())
    call = os.environ.get("BUILDLOG_CALL_ID", "")
    identity = gate.waiter_identity(estimate.profile.step, estimate.profile.worktree)
    if isinstance(estimate.profile, gate.ProgramProfile):
        identity["folder"] = estimate.profile.worktree
    return wire.AdmitRequest(episode, owner, tuple(argv), cwd, since, sidecar,
        wire.Identity(identity["session_id"], identity["session_name"], identity["step"], identity["folder"]),
        gate.CallRecord(call) if call else gate.NoCallRecord(), width_value_typed, estimate.profile,
        isinstance(estimate.cache, gate.FreshTest), float(os.environ.get("BUILDLOG_MEM_TOKEN_BACK_AT", "0")), run)


def write_shell_grant(path: Path, grant: wire.Admitted | wire.LaunchAdmitted, elapsed: int) -> None:
    """Write assignments in the client process; the shell parses no protocol data."""
    values = {"BUILDLOG_MEM_OUTCOME": "Granted", "BUILDLOG_MEM_WAIT_S": str(elapsed)}
    if isinstance(grant, wire.Admitted):
        values.update(BUILDLOG_MEM_RESERVATION=grant.reservation, BUILDLOG_MEM_MAX_PATH=grant.max_path,
            BUILDLOG_MEM_WAVE_PATH=grant.wave_path, BUILDLOG_MEM_SAMPLING_CONTEXT=wire.encode_record(grant.sampling_context),
            BUILDLOG_MEM_CLEANUP_TIMING=wire.encode_record(grant.cleanup_timing))
    _ = path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))


def down_alert() -> int:
    from scripts.accounts import outage_alerts
    configuration = gate.read_memory_config()
    started = CLOCK.monotonic()
    while True:
        try:
            _ = view()
            return 0
        except wire.AdmissionUnavailable:
            if CLOCK.monotonic() - started >= configuration.silence_seconds:
                break
            CLOCK.sleep(configuration.reconnect_seconds)
    claimed = outage_alerts.claim("build-admission")
    if not isinstance(claimed, outage_alerts.AlertClaim):
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    from scripts.notify import push_texts
    command = push_texts.user_send_argv(push_texts.render("build_admission.down"))
    result = subprocess.run(command, check=False)
    if result.returncode:
        outage_alerts.release(claimed)
        return result.returncode
    outage_alerts.delivered(claimed)
    return 0


def _saved_cleanup_timing() -> wire.CleanupTiming:
    raw = os.environ.get("BUILDLOG_MEM_CLEANUP_TIMING")
    if raw:
        try:
            timing = wire.parse_record(raw, wire.CleanupTiming)
            if all(math.isfinite(value) and value > 0 for value in (timing.silence_seconds, timing.reconnect_seconds)):
                return timing
        except (ValueError, TypeError):
            pass
    return wire.PROTOCOL_CLEANUP_TIMING


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("view", "explain", "down-alert", "priority-clear", "code-version"):
        _ = subcommands.add_parser(name)
    for name in ("admit", "launch"):
        admission = subcommands.add_parser(name)
        _ = admission.add_argument("--pid", type=int, default=os.getppid())
        _ = admission.add_argument("--since", type=float, default=0.0)
        _ = admission.add_argument("--sidecar", default="")
        _ = admission.add_argument("--shell-state", type=Path)
        _ = admission.add_argument("--wait-events", action="store_true")
        _ = admission.add_argument("argv", nargs=argparse.REMAINDER)
    mine_parser = subcommands.add_parser("mine")
    _ = mine_parser.add_argument("session_id")
    for name in ("release", "width", "token-wait"):
        mutation = subcommands.add_parser(name)
        _ = mutation.add_argument("reservation")
        if name != "token-wait":
            _ = mutation.add_argument("value", type=int)
    head_parser = subcommands.add_parser("admit-head")
    _ = head_parser.add_argument("why")
    priority_parser = subcommands.add_parser("priority-set")
    _ = priority_parser.add_argument("unit")
    _ = priority_parser.add_argument("--why", default="the user's bump")
    class Arguments(argparse.Namespace):
        command: str = ""
        session_id: str = ""
        reservation: str = ""
        value: int = 0
        why: str = ""
        unit: str = ""
        pid: int = 0
        since: float = 0.0
        sidecar: str = ""
        shell_state: Path | None = None
        wait_events: bool = False
        argv: list[str] = []
    args = parser.parse_args(argv, namespace=Arguments())
    if args.command == "code-version":
        print(LOADED_ADMISSION_CODE_VERSION)
        return 0
    output = WaitOutput(on_event=(lambda event: print(event, flush=True))
                        if args.wait_events else lambda _event: None)
    launch_limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
    deadline = output.started + launch_limit if args.command == "launch" and launch_limit > 0 else float("inf")
    retry_token = _ACQUISITION_RETRY.set(AcquisitionRetry(deadline,
        launch_without_wait=args.command == "launch" and launch_limit == 0))
    outcome = "Refused"
    try:
        if args.command == "down-alert":
            return down_alert()
        if args.command in ("admit", "launch"):
            owner_start = gate.process_start_time(args.pid)
            if owner_start is None:
                raise CheckerFailure("owner no longer exists")
            owner, episode, since = wire.Owner(args.pid, owner_start), uuid.uuid4().hex, args.since or CLOCK.now()
            run = parse_run_identity(os.environ.get("BUILDLOG_RUN", ""))
            step_argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            def refresh() -> wire.AdmitRequest:
                try:
                    request = step_request(step_argv, os.getcwd(), owner, since, args.sidecar, episode, run=run)
                except _QueueLeft:
                    raise
                except Exception as error:
                    raise CheckerFailure(str(error)) from error
                return request
            if args.command == "admit":
                grant = admit(refresh(), output, refresh)
            else:
                def refresh_launch() -> wire.LaunchRequest:
                    request = refresh()
                    return wire.LaunchRequest(episode, owner, since, request.identity, request.profile)
                grant = launch(refresh_launch(), output, refresh_launch)
            if isinstance(grant, LeftQueue):
                raise _QueueLeft()
            if isinstance(grant, wire.Refused):
                output.deadline_reached = args.command == "launch" and grant.reason == LAUNCH_WAIT_LIMIT_REASON
                return 2
            if args.shell_state is not None:
                try:
                    write_shell_grant(args.shell_state, grant, output.elapsed_seconds())
                except BaseException:
                    if isinstance(grant, wire.Admitted):
                        _ = release(grant.reservation, 1, grant.cleanup_timing)
                    raise
            outcome = "Granted"
            output.finished()
            if not args.wait_events:
                print(wire.encode_reply(grant), end="")
            return 0
        result = (view() if args.command == "view" else explain() if args.command == "explain" else
                  mine(args.session_id) if args.command == "mine" else release(args.reservation, args.value, _saved_cleanup_timing())
                  if args.command == "release" else width(args.reservation, args.value)
                  if args.command == "width" else token_wait(args.reservation, _saved_cleanup_timing()) if args.command == "token-wait" else
                  priority_set(args.unit, args.why, gate.queue_command_actor()) if args.command == "priority-set" else
                  priority_clear(gate.queue_command_actor()) if args.command == "priority-clear" else
                  admit_head(args.why, gate.queue_command_actor()))
        if isinstance(result, wire.Refused):
            return 2
        print(wire.encode_record(result))
    except _QueueLeft:
        outcome = "LeftQueue"
        output.finished(left=True)
        if args.shell_state is not None:
            _ = args.shell_state.write_text("BUILDLOG_MEM_OUTCOME=LeftQueue\nBUILDLOG_MEM_WAIT_S="
                + str(output.elapsed_seconds()) + "\n")
        return 75
    except wire.AdmissionUnavailable as error:
        print(f"admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except (CheckerFailure, ValueError, OSError) as error:
        if args.command == "launch" and str(error) == LAUNCH_WAIT_LIMIT_REASON:
            output.deadline_reached = True
        _ = _refused(str(error) or "checker returned an error", checker=args.command in ("admit", "launch"))
        return 2
    finally:
        if output.held:
            output.on_event("done")
        _ACQUISITION_RETRY.reset(retry_token)
        if args.command in ("admit", "launch") and args.shell_state is not None:
            try:
                with args.shell_state.open("a") as state:
                    _ = state.write(f"BUILDLOG_MEM_WAIT_S={output.elapsed_seconds()}\n"
                                    + f"BUILDLOG_MEM_OUTCOME={outcome}\n")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
