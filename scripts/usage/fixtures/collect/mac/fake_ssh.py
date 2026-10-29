#!/usr/bin/env python3
"""Run an SSH command in a fixture home and optionally interrupt its stream."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict, cast


class SshConfiguration(TypedDict):
    home: str
    execution_directory: str
    report: str
    behavior: str
    database: str
    previous_role_read_at: str


class CommittedRoleSnapshot(TypedDict):
    problems: int
    state: str
    read_at: str


class StreamReport(TypedDict):
    measurement_records: int
    cursor_records: int
    forwarded_bytes: int
    stdin_bytes: int
    options: list[str]
    turn_records: int
    compaction_records: int
    opening_records: int
    edit_records: int
    folder_records: int
    outcome_records: int
    history_cursor_records: int
    altered_outcome_records: int
    altered_lifecycle_records: int
    role_snapshots: list[CommittedRoleSnapshot]
    director_records: int
    history_file_records: int
    history_chunks: int
    metadata_chunks: int
    altered_evidence_records: int


StreamKind = Literal["turn", "compaction", "opening", "edit", "folder", "cursor", "other"]


def stream_kind(value: object) -> StreamKind:
    if not isinstance(value, dict):
        return "other"
    fields = cast(dict[str, object], value)
    for name in ("kind", "type"):
        tag = fields.get(name)
        if tag in ("turn", "compaction", "opening", "edit", "folder", "cursor"):
            return cast(StreamKind, tag)
    for child in fields.values():
        kind = stream_kind(child)
        if kind != "other":
            return kind
    return "other"


def observe_role_snapshot(configuration: SshConfiguration) -> CommittedRoleSnapshot:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(f"file:{configuration['database']}?mode=ro", uri=True)) as database:
            rows = cast(list[tuple[int, str, str]], database.execute(
                "SELECT problems, state, read_at FROM role_log_sources WHERE machine = 'mac'").fetchall())
        if rows and rows[0][2] != configuration["previous_role_read_at"]:
            problems, state, read_at = rows[0]
            return {"problems": problems, "state": state, "read_at": read_at}
        time.sleep(0.01)
    raise RuntimeError("fixture never observes the committed role snapshot")


def save_stream_report(path: Path, report: StreamReport) -> None:
    pending = path.with_suffix(".pending")
    _ = pending.write_text(json.dumps(report), encoding="utf-8")
    _ = pending.replace(path)


def relay_remote_errors(stream: BinaryIO) -> None:
    errors = cast(BinaryIO, sys.stderr.buffer)
    for line in cast(Iterable[bytes], stream):
        _ = errors.write(line)
        errors.flush()


def main() -> int:
    configuration = cast(SshConfiguration, json.loads(
        Path(os.environ["USAGE_TEST_SSH_CONFIG"]).read_text(encoding="utf-8")))
    stdin = b"" if configuration["behavior"] == "early_disconnect" else sys.stdin.buffer.read()
    output = cast(BinaryIO, sys.stdout.buffer)
    report: StreamReport = {
        "measurement_records": 0, "cursor_records": 0, "forwarded_bytes": 0,
        "stdin_bytes": len(stdin), "options": sys.argv[1:-1],
        "turn_records": 0, "compaction_records": 0,
        "opening_records": 0, "edit_records": 0, "folder_records": 0, "outcome_records": 0, "history_cursor_records": 0,
        "altered_outcome_records": 0, "altered_lifecycle_records": 0,
        "role_snapshots": [],
        "director_records": 0, "history_file_records": 0, "history_chunks": 0,
        "metadata_chunks": 0, "altered_evidence_records": 0,
    }
    behavior = configuration["behavior"]
    try:
        if behavior == "wait_for_release":
            marker = Path(configuration["report"])
            _ = marker.with_suffix(".waiting").write_bytes(b"")
            deadline = time.monotonic() + 15
            while not marker.with_suffix(".release").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture pass was never released")
                time.sleep(0.01)
        if behavior in ("unreachable", "early_disconnect"):
            print("\nfixture SSH is unreachable\nsecond fixture diagnostic", file=sys.stderr)
            return 255
        if behavior == "python_error":
            print("fixture remote Python error", file=sys.stderr)
            # The collector stops SSH when it reads a failed status, so the
            # report must exist before the status line can reach it.
            save_stream_report(Path(configuration["report"]), report)
            _ = output.write(b"rc=1\n")
            return 0
        environment = os.environ.copy()
        _ = environment.pop("PYTHONPATH", None)
        _ = environment.pop("UNIT_HISTORY_DIR", None)
        environment["HOME"] = configuration["home"]
        environment["USAGE_STATE_DIR"] = str(Path(configuration["home"]) / ".local/state/usage-model")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with subprocess.Popen(
            ["/bin/sh", "-c", sys.argv[-1]], cwd=configuration["execution_directory"],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process:
            try:
                if process.stdin is None or process.stdout is None or process.stderr is None:
                    raise RuntimeError("fixture remote command has no stream")
                # Carry remote stderr through this process, as SSH does: once the
                # collector terminates it, a remote writer that outlives the
                # connection can no longer reach the collector's diagnostics.
                remote_errors = threading.Thread(target=relay_remote_errors, args=(process.stderr,), daemon=True)
                remote_errors.start()
                with process.stdin:
                    _ = process.stdin.write(stdin)
                second_file_turns = 0
                changed_record = False
                attribution_ready = False
                history_file = False
                transcript_file = False
                for line in cast(Iterable[bytes], process.stdout):
                    if behavior == "cut_before_status" and line.startswith(b"rc="):
                        report["role_snapshots"].append(observe_role_snapshot(configuration))
                        break
                    kind: StreamKind = "other"
                    fields: dict[str, object] = {}
                    try:
                        value = cast(object, json.loads(line))
                        kind = stream_kind(value)
                        if isinstance(value, dict):
                            fields = cast(dict[str, object], value)
                    except (ValueError, UnicodeError):
                        pass
                    record_type = fields.get("type")
                    if record_type == "file":
                        history_file = fields.get("kind") == "history"
                        transcript_file = fields.get("kind") in ("claude", "codex")
                        if history_file:
                            report["history_file_records"] += 1
                    elif record_type == "chunk":
                        report["metadata_chunks"] += 1
                        if history_file:
                            report["history_chunks"] += 1
                    elif record_type == "end":
                        history_file = False
                    elif record_type == "attribution_ready":
                        attribution_ready = True
                    elif (fields and not attribution_ready and record_type not in ("manifest", "success")):
                        report["director_records"] += 1
                        if behavior in ("missing_evidence_field", "malformed_evidence_field") and not changed_record:
                            changed_record = True
                            required = next(name for name in fields if name != "type")
                            if behavior == "missing_evidence_field":
                                del fields[required]
                            else:
                                fields[required] = []
                            line = json.dumps(fields).encode("utf-8") + b"\n"
                            report["altered_evidence_records"] += 1
                    cut_record = False
                    outcome_record = bool(fields) and history_file and record_type not in ("file", "cursor", "problem")
                    if outcome_record and behavior == "missing_outcome_field" and not changed_record:
                        changed_record = True
                        payload_field = next(name for name in fields if name not in ("type", "kind", "family", "path"))
                        del fields[payload_field]
                        line = json.dumps(fields).encode("utf-8") + b"\n"
                        report["altered_outcome_records"] += 1
                    if not changed_record and ((behavior == "missing_opening_time" and kind == "opening")
                                               or (behavior == "invalid_edit_time" and kind == "edit")
                                               or (behavior in ("missing_folder", "invalid_folder") and kind == "folder")):
                        changed_record = True
                        if behavior == "missing_opening_time":
                            del fields["at"]
                        elif behavior == "invalid_edit_time":
                            fields["at"] = "invalid-time"
                        elif behavior == "missing_folder":
                            del fields["folder"]
                        else:
                            fields["folder"] = []
                        line = json.dumps(fields).encode("utf-8") + b"\n"
                        report["altered_lifecycle_records"] += 1
                    if behavior in ("malformed_record", "unknown_measurement", "missing_output",
                                    "oversized_output", "cut_inside_record", "invalid_activity",
                                    "wait_without_action", "missing_activity") and (
                            transcript_file and report["cursor_records"] == 1 and kind == "turn" and not changed_record):
                        changed_record = True
                        if behavior == "malformed_record":
                            line = b"not JSON\n"
                        elif behavior == "cut_inside_record":
                            line = line[:len(line) // 2]
                            cut_record = True
                        else:
                            record = cast(dict[str, object], json.loads(line))
                            if behavior == "unknown_measurement":
                                record["kind"] = "unknown-measurement"
                            elif behavior == "invalid_activity":
                                record["activity"] = "napping"
                            elif behavior == "wait_without_action":
                                record["activity"] = "wait"
                                record["wait_action"] = None
                            elif behavior == "missing_activity":
                                del record["activity"]
                            else:
                                tokens = cast(dict[str, object], record["tokens"])
                                if behavior == "missing_output":
                                    del tokens["output"]
                                else:
                                    tokens["output"] = 2**63
                            line = json.dumps(record).encode("utf-8") + b"\n"
                    report["forwarded_bytes"] += len(line)
                    if kind in ("turn", "compaction", "opening", "edit", "folder"):
                        report["measurement_records"] += 1
                    if kind == "turn":
                        report["turn_records"] += 1
                        if report["cursor_records"] == 1:
                            second_file_turns += 1
                    elif kind == "compaction":
                        report["compaction_records"] += 1
                    elif kind == "opening":
                        report["opening_records"] += 1
                    elif kind == "edit":
                        report["edit_records"] += 1
                    elif kind == "folder":
                        report["folder_records"] += 1
                    elif kind == "cursor":
                        if history_file:
                            report["history_cursor_records"] += 1
                        else:
                            report["cursor_records"] += 1
                    elif outcome_record:
                        report["outcome_records"] += 1
                    save_stream_report(Path(configuration["report"]), report)
                    try:
                        _ = output.write(line)
                        output.flush()
                    except BrokenPipeError:
                        # Python's documented SIGPIPE idiom: drop the unsent
                        # buffer so the exit flush cannot report a broken pipe.
                        _ = os.dup2(os.open(os.devnull, os.O_WRONLY), output.fileno())
                        break
                    if behavior == "cut_inside_first_file" and kind == "turn":
                        report["role_snapshots"].append(observe_role_snapshot(configuration))
                        break
                    if cut_record:
                        break
                    if behavior == "cut_inside_history" and outcome_record:
                        break
                    if behavior == "cut_after_history_cursor" and kind == "cursor" and history_file:
                        break
                    if behavior == "cut_after_first_cursor" and kind == "cursor" and transcript_file and report["cursor_records"] == 1:
                        break
                    if behavior == "cut_inside_second_file" and second_file_turns == 2:
                        break
                else:
                    status = process.wait()
                    remote_errors.join()
                    return status
                # Cut only the forwarded stream, also when the collector stopped
                # reading. Closing the pipe or killing the shell now can leave its
                # Python writer raising BrokenPipeError on stderr, hiding the
                # collector's own diagnostic.
                for _ in cast(Iterable[bytes], process.stdout):
                    pass
                _ = process.wait()
                remote_errors.join()
            finally:
                if process.poll() is None:
                    process.terminate()
                    _ = process.wait()
        return 0
    finally:
        save_stream_report(Path(configuration["report"]), report)


if __name__ == "__main__":
    raise SystemExit(main())
