"""Admission socket client, with bounded queries and replay-safe acquisitions."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib import import_module
import errno
import json
import math
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, Callable, Literal, Protocol, cast
import uuid

from scripts.build_queue import admission_code, placement, snapshot_codec
from scripts.build_targets import target_config
from scripts.direct import step_selection
if TYPE_CHECKING:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
else:
    from scripts.build_queue import admission_wire as wire
from scripts.build_queue.system_clock import SystemClock

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

BUILDS_SESSION = "builds"
LAUNCH_WAIT_LIMIT_REASON = "launch wait limit reached without admission"
LOADED_ADMISSION_CODE_VERSION = os.environ.get("BUILDLOG_ADMISSION_CODE_VERSION") or admission_code.admission_code_version()


class ClientClock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class PlacementConstraintBuilder(Protocol):
    """Read fixed caller policy without importing the provider command cycle."""

    def __call__(self, repo_root: Path, package: str,
                 kind: Literal["check", "lint", "doc", "test", "build", "example", "final"],
                 command: tuple[str, ...], session: placement.SubmittingSession,
                 source_key: placement.SourceIdentity, routing: target_config.Routing,
                 selection: step_selection.TargetSelection, *, writes_tree: bool) -> placement.PlacementConstraints: ...


CLOCK: ClientClock = SystemClock()


class TransportUnavailable(ConnectionError):
    pass


class CheckerFailure(ValueError):
    pass


class _QueueUnreachable(Exception):
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


def _remaining_seconds(deadline: float) -> float:
    left = deadline - CLOCK.monotonic()
    if left <= 0:
        raise TimeoutError("no answer")
    return left


def _line(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
    while b"\n" not in pending:
        connection.settimeout(_remaining_seconds(deadline))
        chunk = connection.recv(65536)
        if not chunk:
            raise TransportUnavailable("closed connection")
        pending.extend(chunk)
    end = pending.index(b"\n")
    line = bytes(pending[:end])
    del pending[:end + 1]
    return line


def _is_placement_control(request: wire.Request) -> bool:
    return isinstance(request, (wire.ClaimFailure, wire.ConsumePermission, wire.ExecutionReport, wire.CancelPlacement))


def _query(request: wire.ViewRequest | wire.ExplainRequest | wire.MineRequest | wire.PlacementRequest
           | wire.ClaimFailure | wire.ConsumePermission | wire.ExecutionReport | wire.PrerequisiteReport
           | wire.CancelPlacement) -> wire.Reply:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    if _is_placement_control(request):
        reply = _call(request, lambda _wait: None)
        if isinstance(reply, LeftQueue):
            raise _QueueLeft()
        if isinstance(reply, wire.Refused):
            raise wire.AdmissionUnavailable(reply.reason)
        return reply
    request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
    timeout = records.read_memory_config().query_timeout_seconds
    deadline = CLOCK.monotonic() + timeout
    try:
        with _connect(_remaining_seconds(deadline)) as connection:
            connection.settimeout(_remaining_seconds(deadline))
            connection.sendall(wire.encode_request(request).encode())
            line = _line(connection, bytearray(), deadline)
            reply = wire.parse_reply(line, request)
            if isinstance(reply, wire.Refused):
                raise wire.AdmissionUnavailable(reply.reason)
            return reply
    except FileNotFoundError as error:
        raise wire.AdmissionUnavailable("no socket") from error
    except ConnectionRefusedError as error:
        raise wire.AdmissionUnavailable("connect refused") from error
    except TimeoutError as error:
        raise wire.AdmissionUnavailable(f"no answer in {timeout:g} s") from error
    except (OSError, ValueError) as error:
        raise wire.AdmissionUnavailable(str(error) or "invalid answer") from error


def request_placement(request: wire.PlacementRequest
                      ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus:
    """Request a durable destination without selecting a fallback in the caller."""
    reply = _call(request, lambda _wait: None)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Refused):
        raise wire.AdmissionUnavailable(reply.reason)
    if isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted, wire.PlacementWait, wire.DispatchStatus)):
        return reply
    raise wire.AdmissionUnavailable("invalid placement answer")


def report_prerequisite(request: wire.PrerequisiteReport) -> wire.Ok:
    reply = _call(request, lambda _wait: None)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Ok):
        return reply
    raise wire.AdmissionUnavailable("invalid prerequisite answer")


def cancel_placement(request: wire.CancelPlacement) -> wire.Ok:
    reply = _query(request)
    if isinstance(reply, wire.Ok):
        return reply
    raise wire.AdmissionUnavailable("invalid cancellation answer")


def read_assignment(path: Path) -> wire.Assignment | wire.LocalPlacementGranted:
    reply = wire.parse_reply(path.read_text())
    if isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted)):
        return reply
    raise wire.AdmissionUnavailable("file contains no assigned attempt")


def write_assignment(path: Path, reply: wire.Assignment | wire.LocalPlacementGranted) -> None:
    write_text_atomic(path, wire.encode_reply(reply))


@dataclass(frozen=True)
class KeepPlacementPrerequisite:
    pass


KEEP_PLACEMENT_PREREQUISITE = KeepPlacementPrerequisite()


def finish_step(path: Path, status: int, reason: str = "", *,
                prerequisite: placement.LocalExecutionPrerequisite | KeepPlacementPrerequisite = KEEP_PLACEMENT_PREREQUISITE
                ) -> wire.DispatchStatus:
    from scripts.build_queue import placement

    reply = read_assignment(path)
    if not isinstance(reply, wire.LocalPlacementGranted):
        raise wire.AdmissionUnavailable("finish-step requires a local grant")
    if not isinstance(prerequisite, KeepPlacementPrerequisite):
        _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex,
            reply.assignment.episode, reply.assignment.owner, prerequisite))
    _ = release(reply.grant.reservation, status, reply.grant.cleanup_timing)
    return report_execution(wire.ExecutionReport(uuid.uuid4().hex, reply.assignment, placement.NoProviderClaim(),
        placement.ExecutionNotStarted() if reason else placement.ExecutionEvidenceUnavailable("the local command has ended", CLOCK.now()),
        placement.PrelaunchRefused(reason) if reason else placement.AttemptCompleted(status),
        placement.DispatchPathSettled()))


def claim_failure(request: wire.ClaimFailure) -> wire.DispatchStatus:
    reply = _query(request)
    if isinstance(reply, wire.DispatchStatus):
        return reply
    raise wire.AdmissionUnavailable("invalid claim failure answer")


def consume_permission(request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
    """A repeated call can return status but cannot restore a lost authorization."""
    reply = _query(request)
    if isinstance(reply, (wire.FirstAuthorization, wire.DispatchStatus)):
        return reply
    raise wire.AdmissionUnavailable("invalid permission answer")


def report_execution(request: wire.ExecutionReport) -> wire.DispatchStatus:
    reply = _query(request)
    if isinstance(reply, wire.DispatchStatus):
        return reply
    raise wire.AdmissionUnavailable("invalid execution report answer")


@dataclass
class EstimateConnection:
    connection: socket.socket
    pending: bytearray


_ESTIMATE_CONNECTION: ContextVar[EstimateConnection | None] = ContextVar("estimate_connection", default=None)


@dataclass(frozen=True)
class ReadConfigurationAtUse:
    """Read local admission configuration when a request needs it."""


READ_CONFIGURATION_AT_USE = ReadConfigurationAtUse()


def build_history_generation(configuration: records.MemoryConfig | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE) -> records.BuildHistoryGeneration:
    """Read the generation before reading build history."""
    from scripts.build_queue import admission_records as records
    reply = _call(wire.BuildHistoryRequest(), lambda _wait: None, channel=_ESTIMATE_CONNECTION.get(),
                  configuration=configuration)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Refused):
        raise CheckerFailure(reply.reason)
    if not isinstance(reply, wire.BuildHistoryReply) or reply.generation < 0:
        raise CheckerFailure("invalid build history generation answer")
    return records.BuildHistoryGeneration(reply.generation)


def view() -> records.QueueRead | wire.CloudQueueRead:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.ViewRequest())
    if not isinstance(reply, wire.ViewReply):
        raise wire.AdmissionUnavailable("invalid view answer")
    records.warn_configuration_diagnostics(reply.reading.diagnostics)
    return reply.reading


def explain() -> records.QueueExplanation:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.ExplainRequest())
    if not isinstance(reply, wire.ExplainReply):
        raise wire.AdmissionUnavailable("invalid explanation answer")
    records.warn_configuration_diagnostics(reply.diagnostics)
    return reply.explanation


def mine(session_id: str) -> records.QueueRead | wire.CloudQueueRead:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.MineRequest(session_id))
    if not isinstance(reply, wire.MineReply):
        raise wire.AdmissionUnavailable("invalid step answer")
    records.warn_configuration_diagnostics(reply.reading.diagnostics)
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
        if isinstance(first.place, wire.CloudPlace):
            return first
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
    unreachable_deadline: float = float("inf")

    def start_leave_schedule(self, interval: float) -> None:
        self.leave_interval = interval
        if self.next_leave == float("inf") and (
                os.environ.get("LINT_QUEUE_LEAVE_HOOK") or os.environ.get("BUILDLOG_MAC_IDLE_HOOK")):
            self.next_leave = CLOCK.monotonic() + self.leave_interval

    def left_queue(self) -> bool:
        if CLOCK.monotonic() < self.next_leave:
            return False
        left = _leave_queue()
        self.next_leave = CLOCK.monotonic() + self.leave_interval
        return left

    def bound(self, seconds: float) -> float:
        return max(0.0, min(seconds, self.deadline - CLOCK.monotonic(),
                            self.next_leave - CLOCK.monotonic(), self.unreachable_deadline - CLOCK.monotonic()))


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
          configuration: records.MemoryConfig | wire.AcquisitionTiming | wire.CleanupTiming | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE
          ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok | wire.BuildHistoryReply | LeftQueue | wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | wire.FirstAuthorization:
    acquisition = _is_placement_control(request) or isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest,
        wire.BuildHistoryRequest, wire.PlacementRequest, wire.PrerequisiteReport))
    retry = _ACQUISITION_RETRY.get() if acquisition else None
    owns_retry = retry is None
    if retry is None:
        limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
        launch = isinstance(request, wire.LaunchRequest)
        retry = AcquisitionRetry(CLOCK.monotonic() + limit if launch and limit > 0 else float("inf"),
                                 launch_without_wait=launch and limit == 0)
    token = _ACQUISITION_RETRY.set(retry)
    try:
        if isinstance(configuration, ReadConfigurationAtUse):
            from scripts.build_queue import admission_records as records
            resolved = records.read_memory_config(report=False)
        else:
            resolved = configuration
        request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
        reply = _acquire(request, on_wait, refresh, attempt=attempt, channel=channel, retry=retry,
                         configuration=resolved)
        if not isinstance(reply, wire.Refused) and not isinstance(resolved, (wire.AcquisitionTiming, wire.CleanupTiming)):
            from scripts.build_queue import admission_records as records
            records.warn_configuration_diagnostics(resolved.diagnostics)
        return reply
    except _QueueLeft:
        return LeftQueue()
    except _QueueUnreachable as error:
        if not owns_retry:
            raise
        return _refused(str(error))
    except conf.Unusable as error:
        return _refused(str(error), checker=True)
    finally:
        _ACQUISITION_RETRY.reset(token)


def _acquire(request: wire.Request, on_wait: Callable[[wire.Wait], None],
             refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
             *, attempt: Callable[[], None], channel: EstimateConnection | None, retry: AcquisitionRetry,
             configuration: records.MemoryConfig | wire.AcquisitionTiming | wire.CleanupTiming
             ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok | wire.BuildHistoryReply | LeftQueue | wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | wire.FirstAuthorization:
    first = request
    placement_control = _is_placement_control(first)
    leave_interval = (configuration.running_pass_seconds if not isinstance(configuration, (wire.AcquisitionTiming, wire.CleanupTiming))
                      else configuration.reconnect_seconds)
    generation_query = isinstance(first, wire.BuildHistoryRequest)
    # The caller can hold a lease before the service accepts a connection.
    if isinstance(first, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest,
            wire.BuildHistoryRequest, wire.PlacementRequest, wire.PrerequisiteReport)):
        retry.start_leave_schedule(leave_interval)
    launch = isinstance(first, wire.LaunchRequest)
    failure_limit = _whole_seconds("BUILDLOG_MEM_CHECK_RETRY_S", 60)
    had_wait = False
    refresh_pending = False
    failed_since: float | None = None  # An external transport/checker boundary, not stored domain state.
    restarted = False
    last_valid = CLOCK.monotonic()
    last_silence_notice = last_valid
    if (not isinstance(configuration, wire.CleanupTiming)
            and (isinstance(configuration, wire.AcquisitionTiming) or placement_control)
            and retry.unreachable_deadline == float("inf")):
        retry.unreachable_deadline = last_valid + configuration.unreachable_limit_seconds

    def check_unreachable() -> None:
        if (not isinstance(configuration, wire.CleanupTiming)
                and CLOCK.monotonic() >= retry.unreachable_deadline):
            duration = _unreachable_duration(configuration.unreachable_limit_seconds)
            reason = f"build queue unreachable for {duration}; step stopped; cargo token given back"
            raise _QueueUnreachable(reason)

    while True:
        if retry.left_queue():
            return LeftQueue()
        if CLOCK.monotonic() >= retry.deadline:
            return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
        check_unreachable()
        attempt()
        try:
            timeout = configuration.silence_seconds
            if generation_query:
                timeout = min(timeout, configuration.reconnect_seconds)
            timeout = retry.bound(timeout)
            active_channel, channel = channel, None
            with nullcontext(active_channel.connection) if active_channel is not None else _connect(timeout) as connection:
                send_timeout = retry.bound(timeout)
                if active_channel is not None or send_timeout < timeout:
                    connection.settimeout(send_timeout)
                connection.sendall(wire.encode_request(request).encode())
                pending = active_channel.pending if active_channel is not None else bytearray()
                while True:
                    deadline = max(last_valid, last_silence_notice) + configuration.silence_seconds
                    if generation_query:
                        deadline = min(deadline, CLOCK.monotonic() + configuration.reconnect_seconds)
                    try:
                        line = _line(connection, pending, min(deadline, retry.deadline, retry.next_leave,
                                                             retry.unreachable_deadline))
                    except TimeoutError:
                        if CLOCK.monotonic() >= retry.next_leave:
                            if retry.left_queue():
                                return LeftQueue()
                            if CLOCK.monotonic() < min(deadline, retry.deadline, retry.unreachable_deadline):
                                continue
                        if CLOCK.monotonic() >= retry.deadline:
                            return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                        check_unreachable()
                        if generation_query:
                            raise
                        if retry.launch_without_wait:
                            return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                        _silence_notice(last_valid)
                        last_silence_notice = CLOCK.monotonic()
                        continue
                    try:
                        with snapshot_codec.snapshot_read_policy(snapshot_codec.SnapshotReadPolicy.ACQUISITION_FIELDS):
                            reply = wire.parse_reply(line, request)
                    except ValueError as error:
                        raise CheckerFailure(str(error)) from error
                    if CLOCK.monotonic() > retry.deadline:
                        return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    retry.unreachable_deadline = (CLOCK.monotonic() + configuration.unreachable_limit_seconds
                        if isinstance(configuration, wire.AcquisitionTiming) else float("inf"))
                    if isinstance(reply, wire.Refused):
                        if generation_query or placement_control:
                            return reply
                        return _refused(reply.reason, checker=isinstance(first, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest)))
                    if placement_control:
                        if isinstance(reply, (wire.DispatchStatus, wire.FirstAuthorization, wire.Ok)):
                            return reply
                        raise CheckerFailure("invalid placement control answer")
                    if isinstance(first, wire.PlacementRequest):
                        if isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted, wire.PlacementWait, wire.DispatchStatus)):
                            return reply
                        raise CheckerFailure("invalid placement answer")
                    if generation_query:
                        if retry.left_queue():
                            return LeftQueue()
                        if isinstance(reply, wire.BuildHistoryReply):
                            return reply
                        if isinstance(reply, wire.Wait):
                            continue
                        raise CheckerFailure("invalid build history generation answer")
                    if isinstance(reply, (wire.Admitted, wire.LaunchAdmitted, wire.Ok)):
                        if (isinstance(first, (wire.AdmitRequest, wire.ServiceEstimateRequest)) and not isinstance(reply, wire.Admitted)
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
                    from scripts.build_queue import admission_records as records
                    cause = reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason
                    if isinstance(cause, records.ReadingUnavailable):
                        raise CheckerFailure(cause.reason)
                    if launch and retry.launch_without_wait:
                        return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    had_wait = True
                    failed_since = None
                    last_valid = CLOCK.monotonic()
                    records.warn_configuration_diagnostics(reply.snapshot.diagnostics)
                    on_wait(reply)
                    if retry.left_queue():
                        return LeftQueue()
                    if CLOCK.monotonic() >= retry.deadline:
                        return _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
                    if isinstance(cause, records.EstimateExpired) and isinstance(first, wire.ServiceEstimateRequest):
                        break
                    if isinstance(cause, records.EstimateExpired):
                        if refresh is None or not isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)):
                            raise CheckerFailure("estimate cannot be refreshed")
                        refresh_pending = True
                        try:
                            request = replace(_refreshed(first, refresh, channel=EstimateConnection(connection, pending)),
                                              code_version=LOADED_ADMISSION_CODE_VERSION)
                            refresh_pending = False
                        except (_QueueLeft, _QueueUnreachable, OSError, TransportUnavailable):
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
                except (_QueueLeft, _QueueUnreachable):
                    raise
                except Exception as refresh_error:
                    reason = str(refresh_error) or "checker returned an error"
        except (OSError, TransportUnavailable) as error:
            if placement_control and isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
                reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused"
                raise wire.AdmissionUnavailable(reason) from error
            if retry.launch_without_wait or CLOCK.monotonic() >= retry.deadline:
                return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else _refused(LAUNCH_WAIT_LIMIT_REASON, checker=True)
            restart_error = isinstance(error, TransportUnavailable) or error.errno in (errno.ECONNRESET, errno.EPIPE)
            if (isinstance(first, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest,
                    wire.BuildHistoryRequest, wire.PlacementRequest, wire.PrerequisiteReport))
                    and not isinstance(configuration, wire.CleanupTiming)
                    and (restart_error or isinstance(error, (FileNotFoundError, ConnectionRefusedError)))
                    and retry.unreachable_deadline == float("inf")):
                retry.unreachable_deadline = CLOCK.monotonic() + configuration.unreachable_limit_seconds
                duration = _unreachable_duration(configuration.unreachable_limit_seconds)
                print(f"build queue not reachable; trying for {duration}", file=sys.stderr)
            if restart_error:
                if not restarted:
                    print("admission service restarted; waiting again", file=sys.stderr)
                    restarted = True
                continue
            if CLOCK.monotonic() - retry.last_outage_line >= (configuration.outage_notice_seconds if not isinstance(configuration, (wire.AcquisitionTiming, wire.CleanupTiming)) else configuration.silence_seconds):
                reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused" if isinstance(error, ConnectionRefusedError) else str(error)
                print(f"admission service unavailable: {reason}; waiting again", file=sys.stderr)
                retry.last_outage_line = CLOCK.monotonic()
            CLOCK.sleep(retry.bound(configuration.reconnect_seconds))


def _unreachable_duration(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds % 60 == 0 else f"{seconds:g} s"


def admit(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest], *,
          _attempt: Callable[[], None] = _no_attempt_hook) -> wire.Admitted | wire.Refused | LeftQueue:
    return cast(wire.Admitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh, attempt=_attempt))


def launch(request: wire.LaunchRequest, on_wait: Callable[[wire.Wait], None],
           refresh: Callable[[], wire.LaunchRequest]) -> wire.LaunchAdmitted | wire.Refused | LeftQueue:
    return cast(wire.LaunchAdmitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh))


def _silence_notice(last_valid: float) -> None:
    from scripts.lib.user_zone import user_zone
    since = datetime.fromtimestamp(CLOCK.now() - (CLOCK.monotonic() - last_valid), user_zone()).strftime("%H:%M")
    print(f"admission service not answering since {since}", file=sys.stderr)


def _mutation(request: wire.ReleaseRequest | wire.WidthRequest | wire.AdmitHeadRequest | wire.PrioritySetRequest | wire.PriorityClearRequest | wire.TokenWaitRequest,
              configuration: records.MemoryConfig | wire.CleanupTiming | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE) -> wire.Ok | wire.Refused:
    return cast(wire.Ok | wire.Refused, _call(request, lambda waiting: None, configuration=configuration))


def request_id() -> str:
    """Keep replay identities inside the connecting account's namespace."""
    return f"uid:{os.getuid()}:{uuid.uuid4().hex}"


def service_admit(request: wire.ServiceEstimateRequest, on_wait: Callable[[wire.Wait], None],
                  timing: wire.AcquisitionTiming = wire.PROTOCOL_ACQUISITION_TIMING
                  ) -> wire.Admitted | wire.Refused | LeftQueue:
    if isinstance(on_wait, WaitOutput):
        on_wait.configuration = timing
    reply = _call(request, on_wait, configuration=timing)
    if isinstance(reply, (wire.Admitted, wire.Refused, LeftQueue)):
        return reply
    return wire.Refused("invalid service estimate answer")


def release(reservation: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.ReleaseRequest(request_id(), reservation, status), configuration)


def width(reservation: str, width: int) -> wire.Ok | wire.Refused:
    return _mutation(wire.WidthRequest(request_id(), reservation, width))


def admit_head(why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.AdmitHeadRequest(uuid.uuid4().hex, why, actor))


def priority_set(unit: str, why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.PrioritySetRequest(uuid.uuid4().hex, unit, why, actor))


def priority_clear(actor: str, unit: str = "") -> wire.Ok | wire.Refused:
    return _mutation(wire.PriorityClearRequest(uuid.uuid4().hex, actor, unit))


def token_wait(reservation: str, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.TokenWaitRequest(request_id(), reservation), configuration)


def wait_reasons(result: records.AdmissionWaitSnapshot) -> tuple[str, ...]:
    """Describe why admission holds, without treating changing samples as a new reason."""
    from scripts.build_queue import admission_records as records
    cause = result.head_reason.cause if isinstance(result.head_reason, records.HeadAdmissionStatus) else result.head_reason
    if isinstance(cause, records.MemoryPressure) and cause.condition == "swap":
        return ("swap memory pressure",)
    if isinstance(cause, records.BehindHead):
        return (f"queue head {cause.head_name}",)
    if isinstance(cause, records.ReadyForAdmission):
        return ("admission refresh",)
    return (records.reason_phrase(cause, ()),)


@dataclass(frozen=True)
class UnseenWaitHolder:
    pass


@dataclass(frozen=True)
class QueuedPlacementEpisode:
    episode: str


@dataclass(frozen=True)
class PlacementWaitNotice:
    started: float
    next_notice: float
    last_reasons: tuple[str, ...]
    last_need: int
    last_position: int
    last_reply: wire.Wait | wire.PlacementResourceWait
    elapsed_memory_seconds: float = 0.0
    provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()


@dataclass
class WaitOutput:
    started: float = field(default_factory=lambda: CLOCK.monotonic())
    deadline_reached: bool = False
    next_notice: float = 0.0
    held: bool = False
    last_reasons: tuple[str, ...] = ()
    last_need: int = 0
    last_position: int = 0
    last_holder: records.WaitHolder | UnseenWaitHolder = field(default_factory=UnseenWaitHolder)
    on_event: Callable[[str], None] = field(default=lambda _event: None)
    configuration: records.MemoryConfig | wire.AcquisitionTiming | ReadConfigurationAtUse = field(
        default=READ_CONFIGURATION_AT_USE, kw_only=True)
    last_resource: wire.PlacementResourceWait | wire.PlacementEvidenceWait = field(default_factory=wire.PlacementEvidenceWait)
    elapsed_memory_seconds: float = 0.0
    last_provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()
    announced_retries: set[str] = field(default_factory=set)

    def retry_notices(self, notices: tuple[placement.ProviderRetryNotice, ...], *, placement_events: bool) -> None:
        for retry in notices:
            if retry.target not in self.announced_retries:
                target = "Mac" if retry.target == "mac" else retry.target
                text = (f"verify.sh: {target} attempt ended ({retry.reason}); "
                        f"waiting {retry.delay_seconds:g} s before each retry while natedev waits.")
                if placement_events:
                    self.on_event("retry " + text)
                else:
                    print(text, file=sys.stderr, flush=True)
                self.announced_retries.add(retry.target)

    def provider_diagnostics(self, diagnostics: tuple[placement.BusyProviderDiagnostic, ...]) -> None:
        from scripts.lib.user_zone import user_zone

        for diagnostic in diagnostics:
            if diagnostic not in self.last_provider_diagnostics:
                since = datetime.fromisoformat(diagnostic.since).astimezone(user_zone()).strftime("%H:%M")
                print(f"busy: {diagnostic.what} since {since}", file=sys.stderr)
                self.on_event("provider " + json.dumps({"target": diagnostic.target, "what": diagnostic.what}, separators=(",", ":")))
        self.last_provider_diagnostics = diagnostics

    def placement_notices(self, reply: wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus,
                          *, placement_events: bool) -> None:
        if isinstance(reply, (wire.PlacementWait, wire.Assignment, wire.LocalPlacementGranted)):
            self.provider_diagnostics(reply.provider_diagnostics)
            if reply.mac_disk_exclusions:
                self.on_event("disk " + ",".join(reply.mac_disk_exclusions))
        if isinstance(reply, wire.PlacementWait):
            self.retry_notices(reply.retry_notices, placement_events=placement_events)

    def resource_wait(self, reply: wire.PlacementResourceWait) -> None:
        from scripts.build_queue import placement

        previous = self.last_resource
        changed = not isinstance(previous, wire.PlacementResourceWait) or (
            previous.resource, previous.reason) != (reply.resource, reply.reason)
        if self.held and changed:
            if not isinstance(previous, wire.PlacementResourceWait):
                self.elapsed_memory_seconds += CLOCK.monotonic() - self.started
            self.on_event("done")
            self.held = False
        if not self.held or previous != reply:
            holder = ({"kind": "session", "session_id": reply.holder.session_id, "session_name": ""}
                      if isinstance(reply.holder, placement.ResourceHolderSession) else
                      {"kind": "unit", "unit": reply.holder.unit}
                      if isinstance(reply.holder, placement.ResourceHolderUnit) else {"kind": "none"})
            self.on_event("resource " + json.dumps({"resource": reply.resource, "reason": reply.reason,
                "holder": holder}, separators=(",", ":")))
        self.held = True
        self.last_resource = reply

    def reply_seconds(self) -> float:
        from scripts.build_queue import admission_records as records
        configuration = (records.read_memory_config(report=False)
            if isinstance(self.configuration, ReadConfigurationAtUse) else self.configuration)
        return configuration.wait_reply_seconds

    def __call__(self, reply: wire.Wait) -> None:
        from scripts.build_queue import admission_records as records, queue_lines
        from scripts.lib import build_steps
        from scripts.lib.user_zone import user_zone
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            self.on_event("done")
            self.held = False
            self.last_resource = wire.PlacementEvidenceWait()
            self.started = CLOCK.monotonic()
        now = CLOCK.monotonic()
        first = not self.held
        if isinstance(reply.reason, wire.CloudSlotWait):
            reasons = (reply.text,)
            if first:
                self.held = True
                self.on_event("first")
                self.on_event('holder {"kind":"none"}')
            if first or reasons != self.last_reasons or now >= self.next_notice:
                print(reply.text, file=sys.stderr)
                self.last_reasons = reasons
                self.next_notice = now + self.reply_seconds()
            return
        reasons = wait_reasons(reply.snapshot)
        changed = (reasons != self.last_reasons or reply.snapshot.peak.bytes != self.last_need
                   or reply.position != self.last_position)
        if first:
            self.next_notice, self.held = now + self.reply_seconds(), True
            self.on_event("first")
        if first or reply.snapshot.holder != self.last_holder:
            holder = reply.snapshot.holder
            payload = ({"kind": "session", "session_id": holder.session_id, "session_name": holder.session_name}
                       if isinstance(holder, records.SessionHolder) else
                       {"kind": "unit", "unit": holder.unit} if isinstance(holder, records.UnitHolder) else {"kind": "none"})
            self.on_event("holder " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
            self.last_holder = reply.snapshot.holder
        if not first and now < self.next_notice and not changed:
            return
        if now >= self.next_notice:
            self.next_notice = now + self.reply_seconds()
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
        description = descriptions.get(peak.source, "no history, so the fallback")
        prefix = ("waiting for memory since " + datetime.fromtimestamp(CLOCK.now(), user_zone()).strftime("%H:%M %Z")
                  if first else f"still waiting for memory ({int(now - self.started) // 60} min)")
        text = (f"{prefix}: the machine has {snapshot.available / records.GIB:.1f} GiB free; "
            f"a build starts at {decision.threshold / records.GIB:.1f} ({subject}: {peak.bytes / records.GIB:.3f} GiB needed")
        if first:
            text += f", {description}"
        if reply.position == 1 and self.last_position != 1:
            text += "; this step is now first in line"
        self.last_position = reply.position
        if snapshot.head_need:
            text += f"; head {snapshot.head_label} needs {snapshot.head_need / records.GIB:.1f} GiB"
        from scripts.message import unit_labels

        labels = unit_labels.unit_labels_for([item.session_id for item in snapshot.running], os.environ)
        summary = ", ".join(("" if queue_lines.step_owner_name(item, labels) else Path(item.folder).name + " ")
            + f"{queue_lines.step_text(item, build_steps.RECORDED_STEP_NAMES, labels=labels)} in {item.folder}"
            for item in snapshot.running)
        reason_text = (records.head_wait_text(snapshot.head_reason, user_zone())
            if isinstance(snapshot.head_reason, records.HeadAdmissionStatus) else
            records.pressure_text(decision.pressure) if isinstance(decision.pressure, records.MemoryPressure) else "")
        summary = "; ".join(part for part in (reason_text, summary) if part)
        if not snapshot.running and summary:
            text += "; " + summary
        else:
            text += (f"; {decision.promised / records.GIB:.1f} GiB promised, {decision.measured / records.GIB:.1f} GiB measured "
                     f"across {len(snapshot.running)} running steps")
            if snapshot.running:
                text += ": " + summary
        if decision.reserve:
            text += f"; {decision.reserve / records.GIB:.1f} GiB kept for earlyoom"
        print(text + ")", file=sys.stderr, flush=True)

    def elapsed_seconds(self) -> int:
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            return int(self.elapsed_memory_seconds)
        return int(self.elapsed_memory_seconds + CLOCK.monotonic() - self.started) if self.held or self.deadline_reached else 0

    def finished(self, *, left: bool = False, placement_events: bool = False) -> None:
        if not self.held:
            return
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            if not (left and self.last_resource.resource == "natedev" and self.last_resource.reason == "blocked"):
                return
            text = "verify.sh: another machine is free; leaving natedev's hold wait to run there."
        else:
            seconds = self.elapsed_seconds()
            text = (f"another machine is free after {seconds // 60} min {seconds % 60} s: leaving the memory wait" if left else
                    f"memory free after {seconds // 60} min {seconds % 60} s: starting")
        if left and placement_events:
            self.on_event("left " + text)
        else:
            print(text, file=sys.stderr, flush=True)


def parse_run_identity(value: str) -> wire.RunIdentity | wire.NoRun:
    """Parse the run identity at the client boundary."""
    if sys.platform == "darwin":
        return wire.NO_RUN
    pid, separator, start = value.partition(":")
    if separator and pid.isascii() and pid.isdecimal() and int(pid) > 0 and start.isascii() and start.isdecimal():
        return wire.RunIdentity(int(pid), start)
    return wire.NO_RUN


@dataclass(frozen=True)
class InferProfileAtUse:
    pass


INFER_PROFILE_AT_USE = InferProfileAtUse()


def step_identity(step: str, worktree: str) -> wire.Identity:
    from scripts.build_queue import admission_records as records
    from scripts.production import staff

    identity = records.waiter_identity(step, worktree)
    if not identity["session_id"] and (reference := os.environ.get("BUILDLOG_OWNER_STAFF_ID", "")):
        # Resolve the recorded staff owner only when the caller has no session identity.
        try:
            member = staff.snapshot().by_id(staff.StaffId(reference))
        except staff.StaffUnreadable:
            member = None
        if member is not None:
            identity["session_id"] = member.current_session
            identity["session_name"] = member.name
    return wire.Identity(identity["session_id"], identity["session_name"], identity["step"],
                         identity["folder"], identity.get("unit", ""))


def step_request(argv: list[str], cwd: str, owner: wire.Owner, since: float, sidecar: str,
                 episode: str, profile: records.InferFromArgv | wire.Profile | InferProfileAtUse = INFER_PROFILE_AT_USE,
                 *, run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
                 ) -> wire.AdmitRequest:
    from scripts.build_queue import step_estimator, admission_records as records
    from scripts.buildlog import store

    if isinstance(profile, InferProfileAtUse):
        profile = records.INFER_FROM_ARGV
    config = records.read_memory_config()
    estimate = step_estimator.step_estimate(argv, cwd, profile, lambda: datetime.fromtimestamp(CLOCK.now(), UTC),
                                  store.root(), config.policy, first_peak_bytes=config.program_first_peaks["cargo"],
                                  build_history_generation=lambda: build_history_generation(config))
    width_value = records.test_threads(argv)
    width_value = width_value or (int(os.environ["NEXTEST_TEST_THREADS"]) if os.environ.get("NEXTEST_TEST_THREADS", "").isdecimal() else None)
    width_value_typed: records.TestWidth = (records.RequestedTestWidth(width_value) if width_value else
        records.UnspecifiedTestWidth() if estimate.profile.step == "nextest" else records.NoTestWidth())
    call = os.environ.get("BUILDLOG_CALL_ID", "")
    identity = step_identity(estimate.profile.step, estimate.profile.worktree)
    if isinstance(estimate.profile, records.ProgramProfile):
        identity = replace(identity, folder=estimate.profile.worktree)
    return wire.AdmitRequest(episode, owner, tuple(argv), cwd, since, sidecar,
        identity,
        records.CallRecord(call) if call else records.NoCallRecord(), width_value_typed, estimate.profile,
        isinstance(estimate.cache, records.FreshTest), float(os.environ.get("BUILDLOG_MEM_TOKEN_BACK_AT", "0")), run)


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
    from scripts.build_queue import admission_records as records
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv
    configuration = records.read_memory_config()
    started = CLOCK.monotonic()
    while True:
        try:
            _ = view()
            return 0
        except wire.AdmissionUnavailable:
            if CLOCK.monotonic() - started >= configuration.silence_seconds:
                break
            CLOCK.sleep(configuration.reconnect_seconds)
    settings = conf.read("lint")
    claimed = outage_alerts.claim("build-admission")
    if not isinstance(claimed, outage_alerts.AlertClaim):
        return 0

    def send(command: list[str], message: str = "") -> send_result.Outcome:
        try:
            result = subprocess.run(command, input=message, text=True, capture_output=True,
                                    timeout=send_budget.seconds(settings.number("sweep_sender_timeout_seconds")))
            header = send_result.parse_outcome(result.stdout)
            if isinstance(header, send_result.OutcomeHeader):
                print(f"{header.outcome.upper()}: {header.detail}", flush=True)
            outcome = send_result.send_outcome(result.returncode, result.stdout)
            if isinstance(outcome, send_result.ConfirmedSendOutcome):
                return outcome.header.outcome
            print(f"build admission: outage alert failed: {result.stderr.strip()}", file=sys.stderr)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"build admission: outage alert failed: {error}", file=sys.stderr)
        return "failed"

    retained = False
    try:
        restart = "sudo systemctl restart build-admission.socket build-admission.service"
        message = (f"The build queue service has not answered for {configuration.silence_seconds:g} seconds. Builds wait.\n"
                   "The service restarts by itself. Read journalctl -u build-admission.service.\n"
                   f"If the user must act, push the phone with this command: {restart}")
        session_outcome = send(session_send_argv(BUILDS_SESSION, sender="build-admission", lint_settings=settings), message)
        release_outcomes = ("failed", "refused", "retry")
        retained = session_outcome not in release_outcomes
        if session_outcome != "sent":
            from scripts.notify import push_texts
            command = push_texts.user_send_argv(push_texts.render("alert.blocked",
                sender="build-admission", title="Build queue service is down",
                action=f"On natedev, run: {restart}",
                body="The builds session did not take the message. Builds wait until the service answers."))
            scripts = Path(__file__).resolve().parents[1]
            command.extend(["--clears-when", shlex.join([str(scripts / "lib/py"), str(Path(__file__).resolve()), "view"])])
            phone_outcome = send(command)
            retained = retained or phone_outcome not in release_outcomes
        return 0 if retained else 1
    finally:
        if retained:
            outage_alerts.delivered(claimed)
        else:
            outage_alerts.release(claimed)


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


class ClientArguments(argparse.Namespace):
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
    episode: str = ""
    repo_root: str = ""
    package: str = ""
    kind: str = ""
    source_key: str = ""
    target: str = ""
    run: str = ""
    parent_episode: str = ""
    call_started: float = 0.0
    assignment_file: Path = Path()
    token: str = "none"
    writes_tree: bool = False
    local_only: bool = False
    mac_only: bool = False
    once: bool = False
    reason: str = ""
    status: int = 0


def _place_step(args: ClientArguments, output: WaitOutput) -> int:
    if args.command == "cancel-placement":
        from scripts.build_queue import placement, admission_records as records
        from scripts.buildlog import store

        start = records.process_start_time(args.pid)
        if start is None:
            raise CheckerFailure("owner no longer exists")
        owner = placement.SubmittingOwner(store.host_name(), args.pid, start)
        _ = cancel_placement(wire.CancelPlacement(uuid.uuid4().hex, args.episode, owner, args.reason))
        return 0
    placement_wait_state = args.assignment_file.with_name(args.assignment_file.name + ".wait")
    queued_episode_state = args.assignment_file.with_name(args.assignment_file.name + ".queued")
    already_queued = False
    keep_wait = False
    try:
        if args.command == "place-step" and placement_wait_state.exists():
            try:
                saved = wire.parse_record(placement_wait_state.read_text(), PlacementWaitNotice)
            except (ValueError, TypeError):
                placement_wait_state.unlink(missing_ok=True)
            else:
                output.started, output.next_notice = saved.started, saved.next_notice
                output.last_reasons, output.last_need = saved.last_reasons, saved.last_need
                output.last_position = saved.last_position
                output.elapsed_memory_seconds = saved.elapsed_memory_seconds
                output.last_provider_diagnostics = saved.provider_diagnostics
                if isinstance(saved.last_reply, wire.Wait):
                    output.last_holder = saved.last_reply.snapshot.holder
                else:
                    output.last_resource = saved.last_reply
                output.held = True
        from scripts.build_queue import placement, admission_records as records
        from scripts.buildlog import store

        start = records.process_start_time(args.pid)
        if start is None:
            raise CheckerFailure("owner no longer exists")
        owner = placement.SubmittingOwner(store.host_name(), args.pid, start)
        from scripts.build_hold.owning_session import NoOwningSession, OwningSession

        step_argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        episode = args.episode or f"{args.pid}-{start}-{args.source_key}"
        if queued_episode_state.exists():
            try:
                queued = wire.parse_record(queued_episode_state.read_text(), QueuedPlacementEpisode)
            except (ValueError, TypeError):
                queued_episode_state.unlink(missing_ok=True)
            else:
                already_queued = queued.episode == episode
        if args.assignment_file.exists():
            try:
                assigned = read_assignment(args.assignment_file)
            except (FileNotFoundError, ValueError, wire.AdmissionUnavailable):
                pass
            else:
                already_queued = already_queued or assigned.assignment.episode == episode
        def refresh_local() -> wire.AdmitRequest:
            return step_request(step_argv, os.getcwd(), wire.Owner(args.pid, start), args.since,
                                args.sidecar, episode, run=parse_run_identity(args.run))

        local = refresh_local()
        session = OwningSession(args.session_id) if args.session_id else NoOwningSession()
        selection = step_selection.Pinned(args.target) if args.target else step_selection.ROUTED
        routing = target_config.LocalRequired("local caller") if args.local_only else target_config.ANY_ELIGIBLE_TARGET
        kind = args.kind or local.profile.step
        if kind == "nextest":
            kind = "test"
        elif kind in ("clippy", "mend-check"):
            kind = "lint"
        builder = cast(PlacementConstraintBuilder, getattr(
            import_module("scripts.build_targets.build_targets"), "fixed_placement_constraints"))
        constraints = builder(Path(args.repo_root or os.getcwd()), args.package,
            cast(Literal["check", "lint", "doc", "test", "build", "example", "final"], kind), tuple(step_argv), session,
            placement.SourceKeyKnown(args.source_key) if args.source_key else placement.SourceKeyUnavailable("source key is unavailable"),
            routing, selection,
            writes_tree=args.writes_tree)
        if args.mac_only and not args.writes_tree:
            constraints = replace(constraints, selection=placement.MacOnlyHost())
        prerequisite = (placement.TokenRequired(placement.TokenReadinessUnchecked())
                        if args.token == "required" and args.once and not already_queued else
                        placement.TokenRequired() if args.token == "required" else
                        placement.TokenHeld() if args.token == "held" else placement.TokenNotRequired())
        request = wire.PlacementRequest(episode, owner, args.since, local.profile, constraints,
            identity=local.identity, run=local.run,
            local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call,
                local.test_width, local.fresh_test, local.token_back_at), prerequisite=prerequisite,
            purpose=placement.SpeculativeCopy(args.parent_episode, args.call_started)
                if args.parent_episode else placement.NormalStep())
        if already_queued:
            _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex, episode, owner, prerequisite))
        while True:
            answer = request_placement(request)
            output.placement_notices(answer, placement_events=args.wait_events)
            if isinstance(answer, wire.PlacementWait) and isinstance(answer.wait, wire.Wait):
                _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex, episode, owner, prerequisite))
                answer = request_placement(request)
                output.placement_notices(answer, placement_events=args.wait_events)
            if not isinstance(answer, wire.PlacementWait):
                break
            if isinstance(answer.wait, wire.Wait) and not isinstance(prerequisite, placement.TokenRequired):
                output(answer.wait)
                notice = PlacementWaitNotice(output.started, output.next_notice, output.last_reasons,
                    output.last_need, output.last_position, answer.wait, output.elapsed_memory_seconds, output.last_provider_diagnostics)
                write_text_atomic(placement_wait_state, wire.encode_record(notice), new_file_mode=0o600)
            elif isinstance(answer.wait, wire.PlacementResourceWait):
                output.resource_wait(answer.wait)
                notice = PlacementWaitNotice(output.started, output.next_notice, output.last_reasons,
                    output.last_need, output.last_position, answer.wait, output.elapsed_memory_seconds, output.last_provider_diagnostics)
                write_text_atomic(placement_wait_state, wire.encode_record(notice), new_file_mode=0o600)
            if isinstance(answer.wait, wire.Wait):
                cause = answer.wait.reason
                if isinstance(cause, records.HeadAdmissionStatus):
                    cause = cause.cause
                if isinstance(cause, records.EstimateExpired):
                    local = cast(wire.AdmitRequest, _refreshed(local, refresh_local))
                    request = replace(request, profile=local.profile,
                        local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call,
                            local.test_width, local.fresh_test, local.token_back_at))
                    continue
            if args.once:
                if answer.eligibility:
                    write_text_atomic(queued_episode_state, wire.encode_record(QueuedPlacementEpisode(episode)),
                                      new_file_mode=0o600)
                keep_wait = True
                if not args.wait_events:
                    print(wire.encode_reply(answer), end="")
                return 78
            CLOCK.sleep(records.read_memory_config().waiting_pass_seconds)
        if isinstance(answer, wire.DispatchStatus):
            if args.mac_only:
                reason = answer.state.reason if isinstance(answer.state, placement.ClosedAssignment) else answer.reason
                print("refused\t" + reason)
            else:
                print(wire.encode_reply(answer), end="")
            return 2
        write_assignment(args.assignment_file, answer)
        if isinstance(answer, wire.LocalPlacementGranted) and args.shell_state is not None:
            write_shell_grant(args.shell_state, answer.grant, output.elapsed_seconds())
        output.finished(left=isinstance(answer, wire.Assignment), placement_events=args.wait_events)
        if output.held:
            output.on_event("done")
            output.held = False
        destination = answer.assignment.destination
        print("\t".join((destination.provider, destination.target, destination.host, str(destination.slot),
                         answer.assignment.episode, answer.assignment.attempt)))
        return 0
    finally:
        if not keep_wait:
            if output.held:
                output.on_event("done")
                output.held = False
            if args.command == "place-step":
                placement_wait_state.unlink(missing_ok=True)
                queued_episode_state.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("view", "explain", "down-alert", "priority-clear", "code-version"):
        _ = subcommands.add_parser(name)
    placement_parser = subcommands.add_parser("place-step")
    for name in ("episode", "repo-root", "package", "kind", "source-key", "target", "session-id", "run", "sidecar", "parent-episode"):
        _ = placement_parser.add_argument("--" + name, default="")
    _ = placement_parser.add_argument("--pid", type=int, default=os.getppid())
    _ = placement_parser.add_argument("--since", type=float, required=True)
    _ = placement_parser.add_argument("--call-started", type=float, default=0.0)
    _ = placement_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = placement_parser.add_argument("--shell-state", type=Path)
    _ = placement_parser.add_argument("--token", choices=("required", "held", "none"), default="none")
    _ = placement_parser.add_argument("--writes-tree", action="store_true")
    _ = placement_parser.add_argument("--local-only", action="store_true")
    _ = placement_parser.add_argument("--mac-only", action="store_true")
    _ = placement_parser.add_argument("--once", action="store_true")
    _ = placement_parser.add_argument("--wait-events", action="store_true")
    _ = placement_parser.add_argument("argv", nargs=argparse.REMAINDER)
    cancel_parser = subcommands.add_parser("cancel-placement")
    _ = cancel_parser.add_argument("--episode", required=True)
    _ = cancel_parser.add_argument("--pid", type=int, default=os.getppid())
    _ = cancel_parser.add_argument("--reason", default="caller cancelled")
    finish_parser = subcommands.add_parser("finish-step")
    _ = finish_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = finish_parser.add_argument("--status", type=int, default=1)
    _ = finish_parser.add_argument("--reason", default="")
    _ = finish_parser.add_argument("--token", choices=("required",), default="")
    prerequisite_parser = subcommands.add_parser("report-prerequisite")
    _ = prerequisite_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = prerequisite_parser.add_argument("--token", choices=("required",), required=True)
    failed_parser = subcommands.add_parser("claim-failed")
    _ = failed_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = failed_parser.add_argument("--reason", required=True)
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
    args = parser.parse_args(argv, namespace=ClientArguments())
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
        if args.command == "finish-step":
            prerequisite = placement.TokenRequired() if args.token == "required" else KEEP_PLACEMENT_PREREQUISITE
            print(wire.encode_reply(finish_step(args.assignment_file, args.status, args.reason,
                prerequisite=prerequisite)), end="")
            return 0
        if args.command == "report-prerequisite":
            reply = read_assignment(args.assignment_file)
            print(wire.encode_reply(report_prerequisite(wire.PrerequisiteReport(
                uuid.uuid4().hex, reply.assignment.episode, reply.assignment.owner, placement.TokenRequired()))), end="")
            return 0
        if args.command == "claim-failed":
            reply = read_assignment(args.assignment_file)
            print(wire.encode_reply(claim_failure(wire.ClaimFailure(
                uuid.uuid4().hex, reply.assignment, args.reason))), end="")
            return 0
        if args.command in ("place-step", "cancel-placement"):
            return _place_step(args, output)
        if args.command in ("admit", "launch"):
            from scripts.build_queue import admission_records as records
            owner_start = records.process_start_time(args.pid)
            if owner_start is None:
                raise CheckerFailure("owner no longer exists")
            owner, episode, since = wire.Owner(args.pid, owner_start), uuid.uuid4().hex, args.since or CLOCK.now()
            run = parse_run_identity(os.environ.get("BUILDLOG_RUN", ""))
            step_argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            def refresh() -> wire.AdmitRequest:
                try:
                    request = step_request(step_argv, os.getcwd(), owner, since, args.sidecar, episode, run=run)
                except (_QueueLeft, _QueueUnreachable):
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
        def command_actor() -> str:
            from scripts.build_queue import admission_records as records
            return records.queue_command_actor()

        result = (view() if args.command == "view" else explain() if args.command == "explain" else
                  mine(args.session_id) if args.command == "mine" else release(args.reservation, args.value, _saved_cleanup_timing())
                  if args.command == "release" else width(args.reservation, args.value)
                  if args.command == "width" else token_wait(args.reservation, _saved_cleanup_timing()) if args.command == "token-wait" else
                  priority_set(args.unit, args.why, command_actor()) if args.command == "priority-set" else
                  priority_clear(command_actor()) if args.command == "priority-clear" else
                  admit_head(args.why, command_actor()))
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
    except _QueueUnreachable as error:
        _ = _refused(str(error))
        return 2
    except wire.AdmissionUnavailable as error:
        suffix = "; step refused" if args.command == "place-step" else ""
        print(f"admission service unavailable: {error.reason}{suffix}", file=sys.stderr)
        return 2
    except (CheckerFailure, ValueError, OSError) as error:
        if args.command == "launch" and str(error) == LAUNCH_WAIT_LIMIT_REASON:
            output.deadline_reached = True
        _ = _refused(str(error) or "checker returned an error", checker=args.command in ("admit", "launch"))
        return 2
    finally:
        if output.held and args.command not in ("place-step", "cancel-placement"):
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
