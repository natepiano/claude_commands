"""Admission socket client, with bounded queries and replay-safe acquisitions."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import errno
import os
from pathlib import Path
import socket
import sys
from typing import Callable, Protocol, cast
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_service import SystemClock
from scripts.lib.user_zone import user_zone
from scripts.lint import memory_admit as gate

QUERY_TIMEOUT_SECONDS = 0.5


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


def _line(connection: socket.socket, pending: bytes, deadline: float) -> tuple[bytes, bytes]:
    while b"\n" not in pending:
        left = deadline - CLOCK.monotonic()
        if left <= 0:
            raise TimeoutError("no answer")
        connection.settimeout(left)
        chunk = connection.recv(65536)
        if not chunk:
            raise TransportUnavailable("closed connection")
        pending += chunk
    line, _, remaining = pending.partition(b"\n")
    return line, remaining


def _query(request: wire.ViewRequest | wire.ExplainRequest | wire.MineRequest) -> wire.Reply:
    deadline = CLOCK.monotonic() + QUERY_TIMEOUT_SECONDS
    try:
        with _connect(QUERY_TIMEOUT_SECONDS) as connection:
            connection.sendall(wire.encode_request(request).encode())
            line, _ = _line(connection, b"", deadline)
            return wire.parse_reply(line, request)
    except FileNotFoundError as error:
        raise wire.AdmissionUnavailable("no socket") from error
    except ConnectionRefusedError as error:
        raise wire.AdmissionUnavailable("connect refused") from error
    except TimeoutError as error:
        raise wire.AdmissionUnavailable("no answer in 0.5 s") from error
    except (OSError, ValueError) as error:
        raise wire.AdmissionUnavailable(str(error) or "invalid answer") from error


def view() -> gate.QueueRead:
    reply = _query(wire.ViewRequest())
    if not isinstance(reply, wire.ViewReply):
        raise wire.AdmissionUnavailable("invalid view answer")
    return reply.reading


def explain() -> gate.QueueExplanation:
    reply = _query(wire.ExplainRequest())
    if not isinstance(reply, wire.ExplainReply):
        raise wire.AdmissionUnavailable("invalid explanation answer")
    return reply.explanation


def mine(session_id: str) -> gate.QueueRead:
    reply = _query(wire.MineRequest(session_id))
    if not isinstance(reply, wire.MineReply):
        raise wire.AdmissionUnavailable("invalid step answer")
    return reply.reading


def _refreshed(first: wire.AdmitRequest | wire.LaunchRequest,
               refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest]
               ) -> wire.AdmitRequest | wire.LaunchRequest:
    updated = refresh()
    # A refresh changes estimates only, never the step's identity or place.
    if isinstance(first, wire.AdmitRequest) and isinstance(updated, wire.AdmitRequest):
        return replace(first, profile=updated.profile, fresh_test=updated.fresh_test)
    if isinstance(first, wire.LaunchRequest) and isinstance(updated, wire.LaunchRequest):
        return replace(first, profile=updated.profile)
    raise CheckerFailure("refresh changed request kind")


def _call(request: wire.Request, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None = None
          ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok:
    first = request
    started = CLOCK.monotonic()
    launch = isinstance(first, wire.LaunchRequest)
    launch_limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
    failure_limit = _whole_seconds("BUILDLOG_MEM_CHECK_RETRY_S", 60)
    had_wait = False
    refresh_pending = False
    failed_since: float | None = None  # An external transport/checker boundary, not stored domain state.
    restarted = False
    last_outage_line = float("-inf")
    last_valid = CLOCK.monotonic()
    last_silence_notice = last_valid
    while True:
        elapsed = CLOCK.monotonic() - started
        if launch and launch_limit > 0 and elapsed >= launch_limit:
            return wire.Refused("launch wait limit reached without admission")
        try:
            timeout = min(gate.silence_seconds(), max(0.001, launch_limit - elapsed)) if launch and launch_limit > 0 else gate.silence_seconds()
            with _connect(timeout) as connection:
                connection.sendall(wire.encode_request(request).encode())
                pending = b""
                while True:
                    deadline = max(last_valid, last_silence_notice) + gate.silence_seconds()
                    if launch and launch_limit > 0:
                        deadline = min(deadline, started + launch_limit)
                    try:
                        line, pending = _line(connection, pending, deadline)
                    except TimeoutError:
                        if launch and CLOCK.monotonic() - started >= launch_limit:
                            return wire.Refused("launch wait limit reached without admission")
                        since = datetime.fromtimestamp(CLOCK.now() - (CLOCK.monotonic() - last_valid), user_zone()).strftime("%H:%M")
                        print(f"admission service not answering since {since}", file=sys.stderr)
                        last_silence_notice = CLOCK.monotonic()
                        continue
                    try:
                        reply = wire.parse_reply(line, request)
                    except ValueError as error:
                        raise CheckerFailure(str(error)) from error
                    if isinstance(reply, wire.Refused):
                        return reply
                    if isinstance(reply, (wire.Admitted, wire.LaunchAdmitted, wire.Ok)):
                        if launch and launch_limit > 0 and CLOCK.monotonic() - started > launch_limit:
                            return wire.Refused("launch wait limit reached without admission")
                        if (isinstance(first, wire.AdmitRequest) and not isinstance(reply, wire.Admitted)
                                or isinstance(first, wire.LaunchRequest) and not isinstance(reply, wire.LaunchAdmitted)
                                or isinstance(first, (wire.ReleaseRequest, wire.WidthRequest, wire.AdmitHeadRequest))
                                and not isinstance(reply, wire.Ok)):
                            raise CheckerFailure("admission outcome does not match request")
                        return reply
                    if not isinstance(reply, wire.Wait):
                        raise CheckerFailure("invalid admission answer")
                    had_wait = True
                    failed_since = None
                    last_valid = CLOCK.monotonic()
                    on_wait(reply)
                    if launch and CLOCK.monotonic() - started >= launch_limit:
                        return wire.Refused("launch wait limit reached without admission")
                    if isinstance(reply.reason, gate.EstimateExpired):
                        if refresh is None or not isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)):
                            raise CheckerFailure("estimate cannot be refreshed")
                        refresh_pending = True
                        try:
                            request = _refreshed(first, refresh)
                            refresh_pending = False
                        except Exception as error:
                            raise CheckerFailure(str(error)) from error
                        break
        except CheckerFailure as error:
            reason = str(error) or "checker returned an error"
            if not had_wait:
                return wire.Refused(reason)
            if failed_since is None:
                failed_since = CLOCK.monotonic()
            if CLOCK.monotonic() - failed_since >= failure_limit:
                return wire.Refused(reason)
            print(f"memory admission check failed ({reason}); trying again, the step keeps its place", file=sys.stderr)
            # A failed refresh must be retried before another valid expired-wait
            # line can reset the failure interval.
            while refresh_pending and refresh is not None and isinstance(first, (wire.AdmitRequest, wire.LaunchRequest)):
                CLOCK.sleep(min(gate.reconnect_seconds(), max(0.0, failure_limit - (CLOCK.monotonic() - failed_since))))
                if launch and CLOCK.monotonic() - started >= launch_limit:
                    return wire.Refused("launch wait limit reached without admission")
                try:
                    request = _refreshed(first, refresh)
                    refresh_pending = False
                    break
                except Exception as refresh_error:
                    reason = str(refresh_error) or "checker returned an error"
                    if CLOCK.monotonic() - failed_since >= failure_limit:
                        return wire.Refused(reason)
            else:
                CLOCK.sleep(gate.reconnect_seconds())
        except (OSError, TransportUnavailable) as error:
            if launch and CLOCK.monotonic() - started >= launch_limit:
                return wire.Refused("launch wait limit reached without admission")
            if isinstance(error, TransportUnavailable) or error.errno in (errno.ECONNRESET, errno.EPIPE):
                if not restarted:
                    print("admission service restarted; waiting again", file=sys.stderr)
                    restarted = True
                continue
            if CLOCK.monotonic() - last_outage_line >= gate.outage_notice_seconds():
                reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused" if isinstance(error, ConnectionRefusedError) else str(error)
                print(f"admission service unavailable: {reason}; waiting again", file=sys.stderr)
                last_outage_line = CLOCK.monotonic()
            remaining = max(0.0, launch_limit - (CLOCK.monotonic() - started)) if launch else gate.reconnect_seconds()
            CLOCK.sleep(min(gate.reconnect_seconds(), remaining))


def admit(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest]) -> wire.Admitted | wire.Refused:
    return cast(wire.Admitted | wire.Refused, _call(request, on_wait, refresh))


def launch(request: wire.LaunchRequest, on_wait: Callable[[wire.Wait], None],
           refresh: Callable[[], wire.LaunchRequest]) -> wire.LaunchAdmitted | wire.Refused:
    return cast(wire.LaunchAdmitted | wire.Refused, _call(request, on_wait, refresh))


def _mutation(request: wire.ReleaseRequest | wire.WidthRequest | wire.AdmitHeadRequest) -> wire.Ok | wire.Refused:
    return cast(wire.Ok | wire.Refused, _call(request, lambda _wait: None))


def release(reservation: str, status: int) -> wire.Ok | wire.Refused:
    return _mutation(wire.ReleaseRequest(uuid.uuid4().hex, reservation, status))


def width(reservation: str, width: int) -> wire.Ok | wire.Refused:
    return _mutation(wire.WidthRequest(uuid.uuid4().hex, reservation, width))


def admit_head(why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.AdmitHeadRequest(uuid.uuid4().hex, why, actor))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("view", "explain"):
        _ = subcommands.add_parser(name)
    mine_parser = subcommands.add_parser("mine")
    _ = mine_parser.add_argument("session_id")
    release_parser = subcommands.add_parser("release")
    _ = release_parser.add_argument("reservation")
    _ = release_parser.add_argument("status", type=int)
    width_parser = subcommands.add_parser("width")
    _ = width_parser.add_argument("reservation")
    _ = width_parser.add_argument("width", type=int)
    head_parser = subcommands.add_parser("admit-head")
    _ = head_parser.add_argument("why")
    class Arguments(argparse.Namespace):
        command: str = ""
        session_id: str = ""
        reservation: str = ""
        status: int = 0
        width: int = 0
        why: str = ""
    args = parser.parse_args(namespace=Arguments())
    try:
        result = (view() if args.command == "view" else explain() if args.command == "explain" else
                  mine(args.session_id) if args.command == "mine" else release(args.reservation, args.status)
                  if args.command == "release" else width(args.reservation, args.width)
                  if args.command == "width" else admit_head(args.why, gate.require_showrunner()))
        if isinstance(result, wire.Refused):
            print(result.reason, file=sys.stderr)
            return 3
        print(wire.encode_record(result))
    except wire.AdmissionUnavailable as error:
        print(f"admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except gate.AdmitHeadRefused as error:
        print(str(error), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
