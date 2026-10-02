#!/usr/bin/env python3
"""Write one build-log record: a step run() ran, or a verify.sh call.

  record.py step STATUS START END TTY LOG PEAK ARGV...
      From invoke.sh's run(), detached, after every step. START and END are
      epoch seconds; TTY is 1 when a terminal watched the step (no log is
      captured then); LOG is the step's output handed off by run(), PEAK the
      file the cgroup scope wrote memory.peak to. Either may be empty. Both
      are deleted here.
  record.py call OUTCOME STATUS CACHED WAIT WALL BUILD SAVED ELAPSED VERB [ARG...]
      From verify.sh, once per call. OUTCOME is ran, failed, interrupted,
      reused or replayed; CACHED is 1 when the call was eligible for a pass
      record; WAIT, WALL, BUILD and SAVED are whole seconds, ELAPSED the
      script's $SECONDS at the end. STATUS and BUILD may be empty (unknown).
  record.py backfill-verify [LEDGER]
      Copy verify.sh's old call ledger (~/.local/state/verify/events.jsonl)
      into call records, once: each line gets an id from its own hash, so a
      second run adds nothing. The ledger is left as it is.

A recorder never fails its caller: any error goes to <root>/errors.log and the
exit status is 0. Records are JSON lines (store.py); index.py reads them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import parse
import store

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
    board = env.get("PLAN_DELEGATE_BOARD_DIR") or env.get("PLAN_DELEGATE_SESSION_DIR")
    return {
        "seat": env.get("PLAN_DELEGATE_TEAM_ROLE") or None,
        "delegate_session": Path(board.rstrip("/")).name if board else None,
        "session": env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_THREAD_ID") or None,
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


def peak_bytes(path: str) -> int | None:
    if not path:
        return None
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    try:
        return int(text) if text else None
    except ValueError:
        return None


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
        record: dict[str, object] = {
            "kind": "step",
            "v": RECORD_VERSION,
            "id": record_id,
            "host": host,
            "started_at": store.utc_iso(start),
            "ended_at": store.utc_iso(end),
            "duration_s": round(end - start, 3),
            "step": name,
            "argv": argv,
            "cwd": cwd,
            **git_facts(git_directory(argv, cwd)),
            "caller": caller(tty),
            **who(),
            "call_id": os.environ.get("BUILDLOG_CALL_ID") or None,
            "tty": tty,
            "status": status,
            "rustc": None if name == "sweep" else rustc_version(argv, cwd),
            "peak_mem_bytes": peak_bytes(peak),
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
    return int(float(text)) if text.strip() else None


def call(args: list[str]) -> None:
    outcome, status, cached, wait, wall, build, saved, elapsed, verb = args[:9]
    words = args[9:]
    end = time.time()
    host = store.host_name()
    cwd = os.getcwd()
    started = end - float(elapsed or 0)
    record: dict[str, object] = {
        "kind": "call",
        "v": RECORD_VERSION,
        "id": os.environ.get("BUILDLOG_CALL_ID") or new_id(host, started),
        "host": host,
        "started_at": store.utc_iso(started),
        "ended_at": store.utc_iso(end),
        "tool": "verify.sh",
        "command": " ".join([verb, *words]),
        "verb": verb,
        "package": words[0] if words and verb != "final" else None,
        "outcome": outcome,
        "status": optional_int(status),
        "cached": cached == "1",
        "wait_s": optional_int(wait) or 0,
        "wall_s": optional_int(wall) or 0,
        "build_s": optional_int(build),
        "saved_s": optional_int(saved) or 0,
        "cwd": cwd,
        **git_facts(cwd),
        **who(),
        "backfilled": False,
    }
    store.append_line(store.host_file(host, started), record)


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
            "seat": None,
            "delegate_session": event["session"] or None,
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
        elif command == "call":
            call(rest)
        else:
            store.note_error(f"record.py: unknown command {command}")
    except Exception:  # noqa: BLE001 -- the recorder never fails the build it records
        store.note_error(f"record.py {' '.join(argv)[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
