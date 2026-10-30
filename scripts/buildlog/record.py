#!/usr/bin/env python3
"""Write one build-log record: a step run() ran, or a verify.sh call.

port_lint.py writes its own call records through write_call() here.

  record.py step STATUS START END TTY LOG PEAK ARGV...
      From invoke.sh's run(), detached, after every step. START and END are
      epoch seconds; TTY is 1 when a terminal watched the step (no log is
      captured then); LOG is the step's output handed off by run(), PEAK the
      file the cgroup scope wrote memory.peak and memory.pressure to. Either may be empty. Both
      are deleted here. BUILDLOG_TREE_START is the tree key `key` printed
      before the step; the key taken here, after it, decides tree_key and
      tree_changed.
  record.py key ARGV...
      From invoke.sh's run(), before every step: print the tree key
      (treekey.py) of the worktree the step would be recorded under, or
      nothing.
  record.py call OUTCOME STATUS CACHED WAIT WALL BUILD SAVED ELAPSED VERB [ARG...]
      From verify.sh, once per call. OUTCOME is ran, failed, interrupted,
      reused, replayed, cancelled (tree changed during a run, status 77), or superseded (a newer call of the same verb from the
      same worker replaced it in the token queue, status 76); CACHED is 1 when the call was eligible for a pass
      record; WAIT, WALL, BUILD and SAVED are whole seconds, ELAPSED the
      script's $SECONDS at the end. STATUS and BUILD may be empty (unknown).
      BUILDLOG_TOKEN_WAIT_S is the time spent acquiring the cargo token.
      BUILDLOG_MAC_COPY, BUILDLOG_MAC_COPY_S, BUILDLOG_MAC_COPY_COUNT and
      BUILDLOG_MAC_COPY_NATEDEV_S describe a Mac copy of a still-compiling
      natedev test: its outcome word, the seconds copies held the Mac, the
      copies started, and how long the natedev step had run when the result
      was decided.
  record.py hold-event <began|ended> <target> <hold key> <holder> <reason> <at> [<until>]
      AT is an ISO 8601 UTC instant with offset. UNTIL is empty when no end was planned.
  record.py hold-build KEY SINCE BOOT PID START CLASS SESSION COMMAND CWD
      Record an other_session or no_session build once per hold and process lifetime.
  record.py backfill-verify [LEDGER]
      Copy verify.sh's old call ledger (~/.local/state/verify/events.jsonl)
      into call records, once: each line gets an id from its own hash, so a
      second run adds nothing. The ledger is left as it is.

A recorder never fails its caller: any error goes to <root>/errors.log and the
exit status is 0. Records are JSON lines (store.py); index.py reads them.
"""

from __future__ import annotations

import gzip
import fcntl
import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

import parse
import sample
import store
import treekey

RECORD_VERSION = 1
GIT_TIMEOUT_S = 5
RUSTC_TIMEOUT_S = 10
LEDGER = Path.home() / ".local/state/verify/events.jsonl"


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class VerifyEvent(TypedDict):
    at: str
    machine: str
    workspace: str
    worktree: str
    branch: str
    commit: str
    command: str
    outcome: str
    session: str
    wait_s: int
    wall_s: int
    build_s: int
    saved_s: int


def detach() -> None:
    """Leave the caller's session and every descriptor it handed down.

    run() starts this in the background, so it must not hold the caller's
    terminal, a pipe the caller waits on for EOF, or a lock the caller took.
    """
    try:
        _ = os.setsid()
    except OSError:
        pass
    try:
        descriptors = [int(name) for name in os.listdir("/dev/fd")]
    except OSError:
        return
    for descriptor in descriptors:
        if descriptor > 2:
            try:
                os.close(descriptor)
            except OSError:
                pass


def output(args: list[str], cwd: str, timeout: float) -> str | None:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def git_facts(directory: str) -> GitFacts:
    facts: GitFacts = {"repo_path": None, "worktree": None, "branch": None, "sha": None}
    base = ["git", "-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"]
    text = output([*base, "--abbrev-ref", "HEAD"], directory, GIT_TIMEOUT_S)
    lines = text.splitlines() if text else []
    if len(lines) < 3:
        # An unborn HEAD fails the whole call; the paths still resolve.
        text = output(base, directory, GIT_TIMEOUT_S)
        lines = text.splitlines() if text else []
    if len(lines) >= 2:
        common = Path(lines[0])
        facts["repo_path"] = str(common.parent if common.name == ".git" else common)
        facts["worktree"] = lines[1]
    if len(lines) >= 3:
        facts["branch"] = "detached" if lines[2] == "HEAD" else lines[2]
        # Its own call: --abbrev-ref is sticky, so a later HEAD in the same
        # rev-parse prints the branch name again, not the commit.
        sha = output(["git", "-C", directory, "rev-parse", "--short", "HEAD"], directory, GIT_TIMEOUT_S)
        facts["sha"] = sha.strip() if sha else None
    return facts


def git_directory(argv: list[str], cwd: str) -> str:
    """The manifest's folder when the step names one, so a cargo-port run from elsewhere still finds its repo."""
    manifest = parse.manifest_path(argv) or os.environ.get("MANIFEST_PATH")
    if manifest:
        folder = (Path(cwd) / manifest).parent
        if folder.is_dir():
            return str(folder)
    return cwd


def caller(tty: bool) -> str:
    env = os.environ
    explicit = env.get("BUILDLOG_CALLER")
    if explicit:
        return explicit
    if env.get("LINT_OUTPUT_DIR"):
        return "cargo-port"
    if env.get("VALIDATE_TARGET_DIR"):
        return "validate_ci"
    if env.get("CLAUDECODE") or env.get("CODEX_THREAD_ID"):
        return "agent"
    return "alias" if tty else "unknown"


def who() -> dict[str, str | None]:
    env = os.environ
    board = (env.get("UNIT_RUN_DIR") or env.get("PLAN_DELEGATE_BOARD_DIR")
             or env.get("UNIT_SESSION_DIR") or env.get("PLAN_DELEGATE_SESSION_DIR"))
    return {
        "worker": env.get("UNIT_TEAM_ROLE") or env.get("PLAN_DELEGATE_TEAM_ROLE") or None,
        "unit_session": Path(board.rstrip("/")).name if board else None,
        "session": env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID") or None,
    }


def new_id(host: str, epoch: float) -> str:
    return f"{host}-{datetime.fromtimestamp(epoch, UTC).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def rustc_version(argv: list[str], cwd: str) -> str | None:
    words = ["rustc"]
    chosen = parse.toolchain(argv)
    if chosen:
        words.append(chosen)
    text = output([*words, "-V"], cwd, RUSTC_TIMEOUT_S)
    return text.strip() if text else None


class StepScopeMemory(NamedTuple):
    peak_bytes: int | sample.Unmeasured
    stall_some_s: float | sample.Unmeasured
    stall_full_s: float | sample.Unmeasured


def peak_and_stall(path: str) -> StepScopeMemory:
    if not path:
        return StepScopeMemory(sample.Unmeasured.VALUE, sample.Unmeasured.VALUE, sample.Unmeasured.VALUE)
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return StepScopeMemory(sample.Unmeasured.VALUE, sample.Unmeasured.VALUE, sample.Unmeasured.VALUE)
    try:
        peak = int(lines[0]) if lines else sample.Unmeasured.VALUE
    except ValueError:
        peak = sample.Unmeasured.VALUE
    stalls = sample.parse_pressure("\n".join(lines[1:]), require_both=False)
    some = stalls.some_us / 1_000_000 if isinstance(stalls.some_us, int) else sample.Unmeasured.VALUE
    full = stalls.full_us / 1_000_000 if isinstance(stalls.full_us, int) else sample.Unmeasured.VALUE
    return StepScopeMemory(peak, some, full)


def tree_facts(start: str | None, end: str | None) -> dict[str, object]:
    """The key when the tree was the same before and after the step; tree_changed only when both are known."""
    if not start or not end:
        return {"tree_key": None, "tree_changed": None}
    return {"tree_key": start if start == end else None, "tree_changed": start != end}


@dataclass(frozen=True)
class SentTreeKey:
    value: str


@dataclass(frozen=True)
class NoSentTreeKey:
    pass


def sent_tree_key() -> SentTreeKey | NoSentTreeKey:
    if key := os.environ.get("BUILDLOG_SENT_TREE_KEY"):
        return SentTreeKey(key)
    return NoSentTreeKey()


def keep_log(data: bytes, host: str, epoch: float, record_id: str) -> str:
    relative = f"{host}/{store.LOGS_DIR}/{store.month(epoch)}/{record_id}.log.gz"
    target = store.root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + f".{os.getpid()}.tmp")
    with gzip.open(partial, "wb") as handle:
        _ = handle.write(data)
    _ = partial.replace(target)
    return relative


def step(args: list[str]) -> None:
    status_text, start_text, end_text, tty_text, held, peak = args[:6]
    argv = args[6:]
    try:
        start = float(start_text)
        end = float(end_text) if end_text else time.time()
        tty = tty_text == "1"
        status = int(status_text)
        host = store.host_name()
        cwd = os.getcwd()
        directory = git_directory(argv, cwd)
        tree = tree_facts(os.environ.get("BUILDLOG_TREE_START"), treekey.tree_key(directory))
        step_caller = caller(tty)
        sent_key = sent_tree_key()
        if step_caller == "verify-mac" and tree["tree_changed"] is None and isinstance(sent_key, SentTreeKey):
            tree["tree_key"] = sent_key.value
        name = parse.step_name(argv)
        record_id = new_id(host, start)
        facts = parse.no_facts()
        log: str | None = None
        if not tty and held and os.path.isfile(held):
            data = Path(held).read_bytes()
            try:
                facts = parse.log_facts(name, data.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 -- a parser bug loses the facts, never the record
                store.note_error(f"parse {name} {held}")
            if status != 0:
                log = keep_log(data, host, start, record_id)
        memory = peak_and_stall(peak)
        step_slice = "none" if not peak else "builds" if Path(peak).is_file() else "fallback"
        record: dict[str, object] = {
            "kind": "step",
            "v": RECORD_VERSION,
            "id": record_id,
            "host": host,
            "started_at": store.utc_iso(start),
            "ended_at": store.utc_iso(end),
            "duration_s": round(end - start, 3),
            "mem_wait_s": int(os.environ.get("BUILDLOG_MEM_WAIT_S", "0")),
            "slice": step_slice,
            "step": name,
            "argv": argv,
            "cwd": cwd,
            **git_facts(directory),
            **tree,
            # A reusable test build needs verify's Cargo inputs as well as
            # unchanged sources. Older callers leave this unknown.
            "test_build_key": (os.environ.get("BUILDLOG_TEST_BUILD_KEY") or None)
            if name == "nextest" and tree["tree_changed"] is False else None,
            "caller": step_caller,
            **who(),
            "call_id": os.environ.get("BUILDLOG_CALL_ID") or None,
            "tty": tty,
            "status": status,
            "rustc": None if name == "sweep" else rustc_version(argv, cwd),
            "peak_mem_bytes": memory.peak_bytes if isinstance(memory.peak_bytes, int) else None,
            "mem_stall_some_s": memory.stall_some_s if isinstance(memory.stall_some_s, float) else None,
            "mem_stall_full_s": memory.stall_full_s if isinstance(memory.stall_full_s, float) else None,
            **facts,
            "log": log,
        }
        store.append_line(store.host_file(host, start), record)
    finally:
        for leftover in (held, peak):
            if leftover:
                try:
                    os.unlink(leftover)
                except OSError:
                    pass


def optional_int(text: str) -> int | None:
    try:
        return int(float(text)) if text.strip() else None
    except (ValueError, OverflowError):
        return None


def optional_nonnegative_number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def write_call(started: float, fields: dict[str, object], record_id: str | None = None) -> None:
    """Append a call record: fields hold what the tool knows, this adds where, when and for whom.

    verify.sh and port-lint both write through here. reuses and reason are
    port-lint's; verify.sh leaves them NULL.
    """
    end = time.time()
    host = store.host_name()
    cwd = os.getcwd()
    record: dict[str, object] = {
        "kind": "call",
        "v": RECORD_VERSION,
        "id": record_id or new_id(host, started),
        "host": host,
        "started_at": store.utc_iso(started),
        "ended_at": store.utc_iso(end),
        "reuses": None,
        "reason": None,
        **fields,
        "cwd": cwd,
        **git_facts(cwd),
        **who(),
        "backfilled": False,
    }
    store.append_line(store.host_file(host, started), record)


def call(args: list[str]) -> None:
    outcome, status, cached, wait, wall, build, saved, elapsed, verb = args[:9]
    words = args[9:]
    fields: dict[str, object] = {
        "tool": "verify.sh",
        "caller": os.environ.get("BUILDLOG_CALLER") or "verify",
        "command": " ".join([verb, *words]),
        "verb": verb,
        "package": words[0] if words and verb != "final" else None,
        "outcome": outcome,
        "status": optional_int(status),
        "cached": cached == "1",
        "wait_s": optional_int(wait) or 0,
        "token_wait_s": optional_int(os.environ.get("BUILDLOG_TOKEN_WAIT_S", "0")) or 0,
        "mac": os.environ.get("BUILDLOG_MAC", ""),
        "mac_reason": os.environ.get("BUILDLOG_MAC_REASON", ""),
        "mac_holder": os.environ.get("BUILDLOG_MAC_HOLDER", ""),
        "local_reason": os.environ.get("BUILDLOG_LOCAL_REASON", ""),
        "mac_s": optional_nonnegative_number(os.environ.get("BUILDLOG_MAC_S", "")),
        "target": os.environ.get("BUILDLOG_TARGET", ""),
        "hold_wait_s": optional_nonnegative_number(os.environ.get("BUILDLOG_HOLD_WAIT_S", "")),
        "copy": os.environ.get("BUILDLOG_MAC_COPY", ""),
        "copy_s": optional_nonnegative_number(os.environ.get("BUILDLOG_MAC_COPY_S", "")),
        "copy_count": optional_int(os.environ.get("BUILDLOG_MAC_COPY_COUNT", "")),
        "copy_natedev_s": optional_nonnegative_number(os.environ.get("BUILDLOG_MAC_COPY_NATEDEV_S", "")),
        "wall_s": optional_int(wall) or 0,
        "build_s": optional_int(build),
        "saved_s": optional_int(saved) or 0,
        "mem_kills": int(os.environ.get("BUILDLOG_MEM_KILLS", "0")),
        "mem_kill_stopped": os.environ.get("BUILDLOG_MEM_KILL_STOPPED") == "1",
    }
    write_call(time.time() - float(elapsed or 0), fields, os.environ.get("BUILDLOG_CALL_ID"))


class HoldEventRecord(TypedDict):
    kind: str
    v: int
    at: str
    host: str
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    until: str


def hold_event(args: list[str]) -> None:
    event, target, key, holder, reason, at = args[:6]
    if event not in ("began", "ended") or target not in ("mac", "natedev") or len(args) not in (6, 7):
        raise ValueError("invalid hold-event arguments")
    instant = datetime.fromisoformat(at)
    if instant.utcoffset() is None:
        raise ValueError("hold-event at needs an offset")
    until = args[6] if len(args) == 7 else ""
    if until and datetime.fromisoformat(until).utcoffset() is None:
        raise ValueError("hold-event until needs an offset")
    host = store.host_name()
    record: HoldEventRecord = {
        "kind": "hold_event", "v": RECORD_VERSION,
        "at": instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "host": host, "target": target, "event": event, "hold_key": key,
        "holder": holder, "reason": reason, "until": until,
    }
    store.append_line(store.host_file(host, instant.timestamp()), record)


def queue_bump(actor: str, session: str, step: str, folder: str, old_position: int, at: float) -> None:
    host = store.host_name()
    store.append_line(store.host_file(host, at), {
        "kind": "queue_bump", "v": RECORD_VERSION,
        "id": f"{host}-{time.time_ns()}", "host": host, "at": store.utc_iso(at),
        "actor": actor, "session": session, "step": step, "folder": folder, "old_position": old_position,
    })


def hold_build(args: list[str]) -> None:
    key, since, boot, pid, start, classification, session, command, cwd = args
    if classification not in {"other_session", "no_session"} or int(pid) <= 0 or not start.isdecimal():
        raise ValueError("invalid hold-build identity or class")
    if datetime.fromisoformat(since).utcoffset() is None:
        raise ValueError("hold-build since needs an offset")
    hold_id = hashlib.sha256(json.dumps([key, since]).encode()).hexdigest()
    build_id = hashlib.sha256(json.dumps([boot, pid, start]).encode()).hexdigest()
    host = store.host_name()
    path = store.root() / host / f"hold-builds-{hold_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # This file is both the permanent record and the set of builds seen for this hold.
    with path.open("a+") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        _ = log.seek(0)
        for line in log:
            value = cast(object, json.loads(line))
            if isinstance(value, dict) and cast(dict[str, object], value).get("id") == build_id:
                return
        row: dict[str, object] = {
            "kind": "hold_build", "v": RECORD_VERSION, "id": build_id,
            "at": store.utc_iso(time.time()), "host": host, "hold_key": key, "hold_since": since,
            "boot_id": boot, "pid": int(pid), "proc_start": start, "class": classification,
            "session": session, "command": command, "cwd": cwd,
        }
        _ = log.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")


def backfilled_ids() -> set[str]:
    ids: set[str] = set()
    for path in store.root().glob("*/*.jsonl"):
        with path.open("rb") as handle:
            for raw in handle:
                if b'"backfilled":true' not in raw:
                    continue
                try:
                    line = cast(dict[str, object], json.loads(raw))
                except ValueError:
                    continue
                ids.add(str(line.get("id")))
    return ids


def backfill(args: list[str]) -> None:
    ledger = Path(args[0]) if args else LEDGER
    known = backfilled_ids()
    seen: dict[str, int] = {}
    added = 0
    skipped = 0
    for raw in ledger.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        # Two calls can leave identical lines (same second, same tree); the
        # occurrence number keeps both while the hash stays deterministic.
        occurrence = seen.get(line, 0)
        seen[line] = occurrence + 1
        record_id = "b" + hashlib.sha256(f"{line}\0{occurrence}".encode()).hexdigest()[:24]
        if record_id in known:
            skipped += 1
            continue
        event = cast(VerifyEvent, json.loads(line))
        at = datetime.fromisoformat(event["at"]).timestamp()
        words = event["command"].split()
        outcome = event["outcome"]
        worktree = event["worktree"] or None
        record: dict[str, object] = {
            "kind": "call",
            "v": RECORD_VERSION,
            "id": record_id,
            "host": event["machine"],
            "started_at": store.utc_iso(at - event["wait_s"] - event["wall_s"]),
            "ended_at": store.utc_iso(at),
            "tool": "verify.sh",
            "caller": "verify",
            "command": event["command"],
            "verb": words[0] if words else None,
            "package": words[1] if len(words) > 1 else None,
            "outcome": outcome,
            # ran and reused exit 0 by definition; a failure's status was never written down.
            "status": 0 if outcome in ("ran", "reused") else None,
            "cached": True,
            "wait_s": event["wait_s"],
            "wall_s": event["wall_s"],
            "build_s": event["build_s"],
            "saved_s": event["saved_s"],
            "cwd": worktree,
            "repo_path": event["workspace"] or None,
            "worktree": worktree,
            "branch": event["branch"] or None,
            "sha": event["commit"] or None,
            "worker": None,
            "unit_session": event["session"] or None,
            "session": None,
            "backfilled": True,
        }
        store.append_line(store.root() / event["machine"] / f"{store.month(at)}.jsonl", record)
        known.add(record_id)
        added += 1
    print(f"buildlog backfill-verify: {added} added, {skipped} already present, from {ledger}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "backfill-verify":
        backfill(rest)
        return 0
    try:
        if command == "step":
            detach()
            step(rest)
        elif command == "key":
            found = treekey.tree_key(git_directory(rest, os.getcwd()))
            if found:
                print(found)
        elif command == "hold-event":
            hold_event(rest)
        elif command == "hold-build":
            hold_build(rest)
        elif command == "call":
            call(rest)
        else:
            store.note_error(f"record.py: unknown command {command}")
    except Exception:  # noqa: BLE001 -- the recorder never fails the build it records
        store.note_error(f"record.py {' '.join(argv)[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
