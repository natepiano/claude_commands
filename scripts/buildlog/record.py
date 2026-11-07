#!/usr/bin/env python3
"""Write one build-log record: a step run() ran, or a verify.sh call.

port_lint.py writes its own call records through write_call() here.

  record.py step STATUS START END TTY LOG PEAK ARGV...
      From invoke.sh's run(), detached, after every step. START and END are
      epoch seconds; TTY is 1 when a terminal watched the step (no log is
      captured then); LOG is the step's output handed off by run(), PEAK the
      file the cgroup scope wrote memory.peak and memory.pressure to. Either may be empty. Both
      are deleted only after the row is durable. A pending step preserves
      the original row and sources for collect() to finish after a failed
      save. BUILDLOG_TEST_PHASE_FILE names the step's sampled test-phase
      sidecar, retained with those sources; missing observations are unknown.
      BUILDLOG_TREE_START is the tree key step_context.py printed
      before the step; the key taken here, after it, decides tree_key and
      tree_changed.
  record.py test-phase-context SAMPLING_CONTEXT ARGV...
      Before release, print the reservation's repository and filter metadata
      with the actual post-share argv's test width. The sampler adds its window.
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
  record.py launch STATE WORKTREE
      Read hana_shot's existing atomic process-start state and record that
      launch once. Launches take no memory admission; mem_wait_s is null.
      A pending record in the build log survives failed publication.
  record.py backfill-verify [LEDGER]
      Copy verify.sh's old call ledger (~/.local/state/verify/events.jsonl)
      into call records, once: each line gets an id from its own hash, so a
      second run adds nothing. The ledger is left as it is.

A recorder never fails its caller: any error goes to <root>/errors.log and the
exit status is 0. Records are JSON lines (store.py); index.py reads them.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from scripts.lib.test_phase import (
    CallRecord,
    NoCallRecord,
    TestPhaseChargeUse as TestPhaseChargeUse,
    NoTestPhaseChargeUse,
    parse_test_phase_charge_use,
    NO_TEST_PHASE_RECORD as NO_TEST_PHASE_RECORD,
    NoTestPhaseRecord as NoTestPhaseRecord,
    ObservedTestPhase as ObservedTestPhase,
    TestPhaseContext as TestPhaseContext,
    TestPhaseRecord as TestPhaseRecord,
    UnobservedTestPhase as UnobservedTestPhase,
    UnreadableTestPhaseRecord as UnreadableTestPhaseRecord,
)

from scripts.buildlog import cgroup_capture, store, step_context
from scripts.buildlog.repository import (
    GIT_TIMEOUT_S as GIT_TIMEOUT_S,
    output as output,
    remote_identity as remote_identity,
    repository_key as repository_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lint"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_targets"))
if TYPE_CHECKING:
    from ..lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
else:
    from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
if TYPE_CHECKING:
    from ..aws_target.config import MeasurementUnavailable


__all__ = ["_target_hosts"]


RECORD_VERSION = 1
TARGET_DIR_UNSET = "unset"
RUSTC_TIMEOUT_S = 10
LEDGER = Path.home() / ".local/state/verify/events.jsonl"
LAUNCH_LOCK_NAME = "launches.lock"
CPU_SIDECAR, IO_SIDECAR = ".cpu.stat", ".io.stat"


def git_directory(argv: list[str], cwd: str) -> str:
    """Keep the directory helper available to memory admission callers."""
    return step_context.git_directory(argv, cwd)


def transcript_target_folder(metadata: Mapping[str, object], parameters: Mapping[str, object]) -> step_context.StepTargetFolder:
    """Use saved build facts without the collector's current Cargo settings."""
    target = metadata.get("target_directory")
    if isinstance(target, str) and Path(target).is_absolute():
        return step_context.CargoTargetFolder(Path(target))
    binary, profile = metadata.get("binary_path"), metadata.get("profile") or parameters.get("profile")
    if isinstance(binary, str) and Path(binary).is_absolute() and profile in ("debug", "release"):
        path = Path(binary)
        if metadata.get("launched_as") == "example" and path.parent.name == "examples":
            path = path.parent
        if path.parent.name == profile:
            folder = path.parent.parent
            if folder.name == "target":
                return step_context.CargoTargetFolder(folder)
    return step_context.UnresolvedTargetFolder()


def sync_config() -> dict[str, str]:
    """Read peer settings when a sync or report needs them."""
    from scripts.lib import conf

    path = Path(os.environ.get("BUILD_TARGETS_CONFIG", str(Path(__file__).resolve().parents[2]
                                                         / "config/build_targets.conf"))).expanduser()
    return conf.read(path).values()


def target_hosts(target: str) -> tuple[str, ...]:
    """Map a target to host names, including explicit legacy Mac aliases."""
    return _target_hosts(sync_config(), target)


def _target_hosts(values: Mapping[str, str], target: str) -> tuple[str, ...]:
    configured = tuple(name.strip() for name in values.get(f"sync.hosts.{target}", "").split(",") if name.strip())
    if any(name in (".", "..") or Path(name).name != name for name in configured):
        raise ValueError("sync host names must be directory names")
    return configured


def mac_targets() -> tuple[str, ...]:
    from scripts.build_targets.target_config import SshTarget, read_targets

    return tuple(target.name for target in read_targets()
                 if isinstance(target, SshTarget) and target.platform == "macos")


def configured_targets() -> tuple[str, ...]:
    from scripts.build_targets.target_config import read_targets

    return tuple(target.name for target in read_targets())


def local_targets() -> tuple[str, ...]:
    from scripts.build_targets.target_config import LocalTarget, read_targets

    return tuple(target.name for target in read_targets() if isinstance(target, LocalTarget))


@dataclass(frozen=True)
class LocalStepIdentity:
    targets: tuple[str, ...]
    hosts: tuple[str, ...]


def local_step_identity(targets: tuple[str, ...] = ()) -> LocalStepIdentity:
    configured = targets or local_targets()
    values = sync_config()
    hosts = tuple(dict.fromkeys(alias for name in configured for alias in _target_hosts(values, name)))
    return LocalStepIdentity(configured, hosts)


NONLOCAL_STEP_CALLERS = ('verify-linux',)


def is_local_step(target: object, host: object, caller: object, *,
                  targets: tuple[str, ...] = (), hosts: tuple[str, ...] = ()) -> bool:
    """Use explicit target identity before legacy host identity."""
    if caller in NONLOCAL_STEP_CALLERS:
        return False
    identity = LocalStepIdentity(targets, hosts) if targets and hosts else local_step_identity(targets)
    configured = targets or identity.targets
    if target:
        return target in configured
    aliases = hosts or identity.hosts
    return host in aliases

def synchronized_hosts() -> tuple[str, ...]:
    values = sync_config()
    names: list[str] = []
    for key, value in values.items():
        if key.startswith("sync.hosts."):
            names.extend(name.strip() for name in value.split(",") if name.strip())
    if any(name in (".", "..") or Path(name).name != name for name in names):
        raise ValueError("sync host names must be directory names")
    return tuple(dict.fromkeys(names))


def recorded_target(host: str) -> str:
    explicit = os.environ.get("BUILDLOG_TARGET", "")
    if explicit:
        return explicit
    values = sync_config()
    names = [key.removeprefix("sync.hosts.") for key in values if key.startswith("sync.hosts.")]
    for name in names:
        if host in _target_hosts(values, name):
            return name
    return ""


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class LaunchStep(GitFacts):
    kind: Literal["step"]
    v: int
    id: str
    host: str
    started_at: str
    ended_at: str
    duration_s: None
    step: Literal["launch"]
    target_dir: str
    argv: list[str]
    cwd: str
    caller: Literal["hana-shot"]
    session: str | None
    worker: str | None
    unit_session: str | None
    mem_wait_s: None
    slice: Literal["none"]
    tty: bool
    status: int


@dataclass(frozen=True)
class InvalidLaunchState:
    pass


@dataclass(frozen=True)
class PublishedLaunch:
    added: bool
    unknown_worktree: bool


@dataclass(frozen=True)
class NoHeldLog:
    pass


@dataclass(frozen=True)
class NoPeakFile:
    pass


@dataclass(frozen=True)
class PendingStep:
    """The original event and its sources, retained until publication and cleanup finish."""

    row_key: str
    row: dict[str, object]
    held_log: Path | NoHeldLog
    peak_file: Path | NoPeakFile
    month_row_end: int
    test_phase_file: Path | NoTestPhaseRecord = NO_TEST_PHASE_RECORD
    test_phase: TestPhaseRecord | NoTestPhaseRecord | UnreadableTestPhaseRecord = NO_TEST_PHASE_RECORD


@dataclass(frozen=True)
class PublishedStep:
    added: bool


def hana_launch_record(fields: Mapping[str, object], directory: str) -> LaunchStep | InvalidLaunchState:
    """Read process-start facts; launching an app takes no memory admission."""
    pid, port, binary, started = (fields.get(key) for key in ("pid", "port", "binary", "started"))
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536
            or not isinstance(binary, str) or not binary or not isinstance(started, str)):
        return InvalidLaunchState()
    try:
        instant = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return InvalidLaunchState()
        timestamp = store.utc_iso(instant.timestamp())
    except (ValueError, OverflowError, OSError):
        return InvalidLaunchState()
    saved_directory = fields.get("worktree") or fields.get("cwd")
    if not directory and isinstance(saved_directory, str):
        directory = saved_directory
    if not directory:
        executable = Path(binary)
        directory = str(next((parent for parent in executable.parents if (parent / ".git").exists()), executable.parent))
    facts = git_facts(directory)
    identity = who()
    for key in ("worker", "unit_session", "session"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            identity[key] = value
    host = fields.get("host")
    host = host if isinstance(host, str) and host else store.host_name()
    return LaunchStep(
        kind="step", v=RECORD_VERSION, id=f"hana-shot:{host}:{pid}:{port}:{timestamp}", host=host,
        started_at=started, ended_at=started, duration_s=None, step="launch", argv=[binary], cwd=directory,
        **facts, caller="hana-shot", session=identity["session"], worker=identity["worker"],
        unit_session=identity["unit_session"], mem_wait_s=None,
        slice="none", tty=False, status=0, target_dir="none",
    )


def _launch_row_path(record_data: Mapping[str, object]) -> Path:
    import hashlib

    digest = hashlib.sha256(str(record_data["id"]).encode()).hexdigest()
    return store.root() / str(record_data["host"]) / f"launch-{digest}.jsonl"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_launch_line(record_data: Mapping[str, object]) -> bool:
    """Publish one whole immutable line; the source identity makes retries harmless."""
    path = _launch_row_path(record_data)
    if path.exists():
        _sync_directory(path.parent)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{os.urandom(6).hex()}.tmp")
    try:
        with temporary.open("w") as handle:
            _ = handle.write(json.dumps(dict(record_data), separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            _sync_directory(path.parent)
            return False
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def pending_launch_root() -> Path:
    return store.root() / "pending-launches"


def _save_pending_record(path: Path, fields: Mapping[str, object], *, replace_existing: bool = False) -> Path:
    if not replace_existing and path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + f".{os.getpid()}.{os.urandom(6).hex()}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(dict(fields), temporary, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        _sync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _pending_launch_path(row: LaunchStep) -> Path:
    return pending_launch_root() / _launch_row_path(row).with_suffix(".json").name


def _save_pending_launch(row: LaunchStep) -> Path:
    return _save_pending_record(_pending_launch_path(row), row)


def finish_pending_launch(path: Path) -> PublishedLaunch:
    """Keep the retry source until its immutable row is durable or already present."""
    loaded = cast(object, json.loads(path.read_text()))
    if not isinstance(loaded, dict):
        raise ValueError("pending launch is not an object")
    row = cast(dict[str, object], loaded)
    if row.get("step") != "launch" or row.get("caller") != "hana-shot" or not isinstance(row.get("id"), str):
        raise ValueError("pending record is not a Hana launch")
    added = write_launch_line(row)
    if not _launch_row_path(row).is_file():
        raise OSError("launch row was not published")
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)
    return PublishedLaunch(added, row.get("worktree") is None)


def launch(state_path: Path, worktree: Path) -> bool:
    """Record the existing atomic launch state without failing the launcher."""
    try:
        loaded = cast(object, json.loads(state_path.read_text()))
        if not isinstance(loaded, dict):
            return False
        row = hana_launch_record(cast(dict[str, object], loaded), str(worktree))
        if isinstance(row, InvalidLaunchState):
            return False
        store.root().mkdir(parents=True, exist_ok=True)
        with (store.root() / LAUNCH_LOCK_NAME).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return finish_pending_launch(_save_pending_launch(row)).added
            except Exception:  # noqa: BLE001 -- retain the pending retry and report its error
                pending = _pending_launch_path(row)
                store.note_error(f"pending record {pending}" if pending.is_file() else f"launch {state_path}")
                return False
    except Exception:  # noqa: BLE001 -- a recorder never fails its caller
        store.note_error(f"launch {state_path}")
        return False


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
    return f"{host}-{datetime.fromtimestamp(epoch, UTC).strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"


def rustc_version(argv: list[str], cwd: str) -> str | None:
    from scripts.buildlog import parse

    words = ["rustc"]
    chosen = parse.toolchain(argv)
    if chosen:
        words.append(chosen)
    text = output([*words, "-V"], cwd, RUSTC_TIMEOUT_S)
    return text.strip() if text else None


class StepScopeMemory(NamedTuple):
    peak_bytes: int | cgroup_capture.Unmeasured
    stall_some_s: float | cgroup_capture.Unmeasured
    stall_full_s: float | cgroup_capture.Unmeasured


def peak_and_stall(path: str) -> StepScopeMemory:
    unmeasured = cgroup_capture.Unmeasured.VALUE

    if not path:
        return StepScopeMemory(unmeasured, unmeasured, unmeasured)
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return StepScopeMemory(unmeasured, unmeasured, unmeasured)
    try:
        peak = int(lines[0]) if lines else unmeasured
    except ValueError:
        peak = unmeasured
    stalls = cgroup_capture.parse_pressure("\n".join(lines[1:]), require_both=False)
    some = stalls.some_us / 1_000_000 if isinstance(stalls.some_us, int) else unmeasured
    full = stalls.full_us / 1_000_000 if isinstance(stalls.full_us, int) else unmeasured
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


@dataclass(frozen=True)
class SentRepositoryIdentity:
    value: str


@dataclass(frozen=True)
class NoSentRepositoryIdentity:
    pass


def sent_repository_identity() -> SentRepositoryIdentity | NoSentRepositoryIdentity:
    """Read the source repository identity sent to a mirror step."""
    value = os.environ.get("BUILDLOG_REPO_IDENTITY", "").strip()
    return SentRepositoryIdentity(value) if value else NoSentRepositoryIdentity()


def _log_relative_path(host: str, epoch: float, record_id: str) -> str:
    return f"{host}/{store.LOGS_DIR}/{store.month(epoch)}/{record_id}.log.gz"


def keep_log(data: bytes, host: str, epoch: float, record_id: str) -> str:
    import gzip

    relative = _log_relative_path(host, epoch, record_id)
    target = store.root() / relative
    if target.is_file():
        _sync_directory(target.parent)
        return relative
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with partial.open("wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb") as compressed:
                _ = compressed.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
        _sync_directory(target.parent)
    finally:
        partial.unlink(missing_ok=True)
    return relative


def pending_steps_root() -> Path:
    return store.root() / "pending-steps"


@dataclass(frozen=True)
class ReservedTestPhaseRecord(TestPhaseRecord):
    reservation_id: str


@dataclass(frozen=True)
class ChargedTestPhaseRecord(ReservedTestPhaseRecord):
    charge: TestPhaseChargeUse


def completed_test_phase_charge(reservation_id: str) -> TestPhaseChargeUse | NoTestPhaseChargeUse:
    """Copy the completed reservation's evidence without parsing unrelated history rows."""
    import mmap

    try:
        path = store.root() / "admission/anon_peaks.jsonl"
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return NoTestPhaseChargeUse()
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            end = store.last_whole_row_end(handle)
            if not end:
                return NoTestPhaseChargeUse()
            identity = json.dumps(reservation_id).encode("utf-8")
            with mmap.mmap(handle.fileno(), end, access=mmap.ACCESS_READ) as contents:
                while (position := contents.rfind(identity, 0, end)) >= 0:
                    start = contents.rfind(b"\n", 0, position) + 1
                    stop = contents.find(b"\n", position, end)
                    try:
                        loaded = cast(object, json.loads(contents[start:stop]))
                    except ValueError:
                        loaded = None
                    if isinstance(loaded, dict):
                        row = cast(dict[str, object], loaded)
                        if row.get("reservation_id") == reservation_id:
                            evidence = row.get("test_phase_charge_use")
                            if evidence is None:
                                return NoTestPhaseChargeUse()
                            if not isinstance(evidence, dict):
                                raise ValueError("test-phase charge is not an object")
                            return parse_test_phase_charge_use(cast(dict[str, object], evidence))
                    end = start
        return NoTestPhaseChargeUse()
    except (OSError, ValueError) as error:
        try:
            with (store.root() / store.ERRORS_NAME).open("a") as errors:
                _ = errors.write(f"{store.utc_iso(time.time())} test-phase charge {reservation_id!r}: {error!r}\n")
        except OSError:
            pass
        return NoTestPhaseChargeUse()


def test_phase_context(sampling_context: str, argv: list[str]) -> dict[str, object]:
    """Copy admission's selection, using the argv that actually runs for its width."""
    from scripts.buildlog import parse

    loaded = cast(object, json.loads(sampling_context))
    if not isinstance(loaded, dict):
        raise ValueError("test-phase sampling context is not an object")
    fields = cast(dict[str, object], loaded)
    call = fields.get("call_id")
    if isinstance(call, dict):
        call = cast(dict[str, object], call)
        if call.get("kind") == CallRecord.__name__ and isinstance(call.get("call_id"), str):
            fields["call_id"] = call["call_id"]
        elif call.get("kind") == NoCallRecord.__name__:
            fields["call_id"] = None
        else:
            fields["call_id"] = None
    # Admission's parser returns the first width. Supplying nextest's environment
    # default last preserves an explicit flag, including a nonnumeric one.
    width_argv = argv
    if "NEXTEST_TEST_THREADS" in os.environ:
        width_argv = [*argv, "--test-threads", os.environ["NEXTEST_TEST_THREADS"]]
    context = _test_phase_context({**fields, "test_threads": parse.test_threads(width_argv)})
    result: dict[str, object] = {**context, "test_phase_charge": None}
    reservation_id = fields.get("reservation_id")
    if isinstance(reservation_id, str) and reservation_id:
        result["reservation_id"] = reservation_id
    return result


def _test_phase_context(fields: Mapping[str, object]) -> TestPhaseContext:
    text: dict[str, str | None] = {}
    for key in ("repo_identity", "package", "call_id"):
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"test-phase {key} is not text")
        text[key] = value
    scope, width, filters = fields.get("test_scope"), fields.get("test_threads"), fields.get("filters")
    if scope is None:
        test_scope: Literal["full", "filtered"] | None = None
    elif scope == "full":
        test_scope = "full"
    elif scope == "filtered":
        test_scope = "filtered"
    else:
        raise ValueError("test-phase scope is invalid")
    if width is not None and (not isinstance(width, int) or isinstance(width, bool)):
        raise ValueError("test-phase width is not an integer")
    if filters is not None and (not isinstance(filters, list)
                                or not all(isinstance(item, str) for item in cast(list[object], filters))):
        raise ValueError("test-phase filters are not text")
    return {
        "repo_identity": text["repo_identity"], "test_scope": test_scope,
        "package": text["package"], "call_id": text["call_id"],
        "test_threads": width, "filters": cast(list[str] | None, filters),
    }


def _test_phase_record(fields: Mapping[str, object]) -> TestPhaseRecord:
    peak, started = fields.get("test_phase_peak_bytes"), fields.get("test_phase_started_at")
    if peak is None and started is None:
        window: ObservedTestPhase | UnobservedTestPhase = UnobservedTestPhase()
    elif (isinstance(peak, int) and not isinstance(peak, bool) and peak >= 0
            and isinstance(started, (int, float)) and not isinstance(started, bool)
            and math.isfinite(started) and started > 0):
        window = ObservedTestPhase(peak, float(started))
    else:
        raise ValueError("test-phase peak or start time is invalid")
    context = _test_phase_context(fields)
    reservation_id = fields.get("reservation_id")
    if isinstance(reservation_id, str) and reservation_id:
        return ReservedTestPhaseRecord(window, context, reservation_id)
    return TestPhaseRecord(window, context)


def read_test_phase(path: Path | NoTestPhaseRecord) -> TestPhaseRecord | NoTestPhaseRecord | UnreadableTestPhaseRecord:
    if isinstance(path, NoTestPhaseRecord):
        return path
    try:
        loaded = cast(object, json.loads(path.read_text()))
        if not isinstance(loaded, dict):
            raise ValueError("test-phase sidecar is not an object")
        return _test_phase_record(cast(dict[str, object], loaded))
    except FileNotFoundError:
        return NO_TEST_PHASE_RECORD
    except (OSError, ValueError) as error:
        return UnreadableTestPhaseRecord(str(error))


def test_phase_fields(phase: TestPhaseRecord | NoTestPhaseRecord) -> dict[str, object]:
    fields: dict[str, object] = {
        "test_phase_peak_bytes": None, "test_phase_started_at": None, "test_phase_charge": None,
    }
    if isinstance(phase, TestPhaseRecord):
        fields.update({key: value for key, value in phase.context.items() if key != "repo_identity"})
        if isinstance(phase, ReservedTestPhaseRecord):
            fields["reservation_id"] = phase.reservation_id
        if isinstance(phase, ChargedTestPhaseRecord):
            fields["test_phase_charge"] = phase.charge
        if isinstance(phase.window, ObservedTestPhase):
            fields.update(test_phase_peak_bytes=phase.window.peak_bytes,
                          test_phase_started_at=phase.window.started_at)
    return fields


def _save_pending_step(pending: PendingStep, *, replace_existing: bool = False) -> Path:
    return _save_pending_record(pending_steps_root() / f"{pending.row_key}.json", {
        "row_key": pending.row_key, "row": pending.row,
        "held_log": str(pending.held_log) if isinstance(pending.held_log, Path) else None,
        "peak_file": str(pending.peak_file) if isinstance(pending.peak_file, Path) else None,
        "test_phase_file": str(pending.test_phase_file) if isinstance(pending.test_phase_file, Path) else None,
        "test_phase_read_error": (pending.test_phase.reason
                                  if isinstance(pending.test_phase, UnreadableTestPhaseRecord) else None),
        "month_row_end": pending.month_row_end,
    }, replace_existing=replace_existing)


def _read_pending_step(path: Path) -> PendingStep:
    loaded = cast(object, json.loads(path.read_text()))
    if not isinstance(loaded, dict):
        raise ValueError("pending step is not an object")
    fields = cast(dict[str, object], loaded)
    row_key, row = fields.get("row_key"), fields.get("row")
    if not isinstance(row_key, str) or not isinstance(row, dict):
        raise ValueError("pending step has no row identity")
    row = cast(dict[str, object], row)
    if (row.get("kind") != "step" or row.get("step") == "launch"
            or not all(isinstance(row.get(key), str) for key in ("host", "id", "started_at"))):
        raise ValueError("pending record is not a build step")
    held, peak, phase = fields.get("held_log"), fields.get("peak_file"), fields.get("test_phase_file")
    if ((held is not None and not isinstance(held, str))
            or (peak is not None and not isinstance(peak, str))
            or (phase is not None and not isinstance(phase, str))):
        raise ValueError("pending step has invalid source paths")
    boundary_key = "month_row_end" if "month_row_end" in fields else "month_offset"
    saved_boundary = fields.get(boundary_key)
    if not isinstance(saved_boundary, int) or isinstance(saved_boundary, bool) or saved_boundary < 0:
        raise ValueError("pending step has no valid month row end")
    # Legacy sizes can lie inside a tail that an append later cuts. Only a
    # recorded row boundary is safe for a suffix scan; legacy rows scan from 0.
    month_row_end = saved_boundary if boundary_key == "month_row_end" else 0
    phase_error = fields.get("test_phase_read_error")
    test_phase = (UnreadableTestPhaseRecord(phase_error) if isinstance(phase_error, str)
                  else _test_phase_record(row) if isinstance(phase, str) else NO_TEST_PHASE_RECORD)
    return PendingStep(row_key, row, Path(held) if isinstance(held, str) else NoHeldLog(),
                       Path(peak) if isinstance(peak, str) else NoPeakFile(), month_row_end,
                       Path(phase) if isinstance(phase, str) else NO_TEST_PHASE_RECORD, test_phase)


def _step_month_file(row: Mapping[str, object]) -> Path:
    return store.host_file(str(row["host"]), datetime.fromisoformat(str(row["started_at"])).timestamp())


def write_step_line(pending: PendingStep) -> bool:
    path = _step_month_file(pending.row)
    store.append_line(path, pending.row)
    _sync_directory(path.parent)
    return True


def append_row_once(path: Path, row: Mapping[str, object], start: int = 0) -> bool:
    """Recover an append by its fixed id while holding the same log lock."""
    added = True
    with store.locked_append(path) as handle:
        _ = handle.seek(start)
        for line in handle:
            try:
                previous = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(previous, dict) and cast(dict[str, object], previous).get("id") == row["id"]:
                added = False
                break
        if added:
            store.append_locked(handle, row)
    _sync_directory(path.parent)
    return added


def _resume_step_line(pending: PendingStep) -> bool:
    """Scan from the saved row boundary, or byte 0 for a legacy pending step."""
    return append_row_once(_step_month_file(pending.row), pending.row, pending.month_row_end)


def _delete_step_sources(held: Path | NoHeldLog, peak: Path | NoPeakFile,
                         phase: Path | NoTestPhaseRecord = NO_TEST_PHASE_RECORD) -> None:
    sidecars = [Path(str(peak) + suffix) for suffix in (CPU_SIDECAR, IO_SIDECAR)] if isinstance(peak, Path) else []
    for path in (held, peak, phase, *sidecars):
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
            if path.parent.is_dir():
                _sync_directory(path.parent)


def _finish_step(path: Path, pending: PendingStep, publish: Callable[[PendingStep], bool]) -> PublishedStep:
    if isinstance(pending.test_phase, UnreadableTestPhaseRecord):
        raise OSError(f"test-phase sidecar unreadable: {pending.test_phase.reason}")
    row = pending.row
    saved_log = row.get("log")
    if isinstance(saved_log, str):
        if not (store.root() / saved_log).is_file():
            if not isinstance(pending.held_log, Path):
                raise OSError("failed step has no held log to save")
            started = datetime.fromisoformat(str(row["started_at"])).timestamp()
            _ = keep_log(pending.held_log.read_bytes(), str(row["host"]), started, str(row["id"]))
        if not (store.root() / saved_log).is_file():
            raise OSError("failed step log was not saved")
        _sync_directory((store.root() / saved_log).parent)
    added = publish(pending)
    _delete_step_sources(pending.held_log, pending.peak_file, pending.test_phase_file)
    os.utime(path, None)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(path, path.with_suffix(".done"))
    _sync_directory(path.parent)
    return PublishedStep(added)


def finish_pending_step(path: Path) -> PublishedStep:
    """Finish the saved context and sources, then retain proof of completion."""
    pending = _read_pending_step(path)
    if isinstance(pending.test_phase, UnreadableTestPhaseRecord):
        phase = read_test_phase(pending.test_phase_file)
        if isinstance(phase, ReservedTestPhaseRecord) and isinstance(phase.window, ObservedTestPhase):
            charge = completed_test_phase_charge(phase.reservation_id)
            if isinstance(charge, dict):
                phase = ChargedTestPhaseRecord(phase.window, phase.context, phase.reservation_id, charge)
        if isinstance(phase, UnreadableTestPhaseRecord):
            raise OSError(f"test-phase sidecar unreadable: {phase.reason}")
        pending = replace(pending, row={**pending.row, **test_phase_fields(phase)}, test_phase=phase)
        _ = _save_pending_step(pending, replace_existing=True)
    return _finish_step(path, pending, _resume_step_line)


def scope_resources(peak: str) -> cgroup_capture.StepScopeCounters:
    """Parse raw scope sidecars; only absence may lose a reading on retry.

    CPU excludes compiles run by sccache.service. Any other read failure
    propagates so step() keeps the sources instead of publishing unavailable.
    """
    def sidecar(suffix: str) -> str:
        if not peak:
            return ""
        try:
            return Path(peak + suffix).read_text()
        except FileNotFoundError:
            return ""

    return {"cpu": cgroup_capture.parse_cpu(sidecar(CPU_SIDECAR)),
            "io": cgroup_capture.parse_io(sidecar(IO_SIDECAR))}


def step(args: list[str]) -> None:
    import hashlib
    from scripts.buildlog import parse
    from scripts.buildlog import treekey

    status_text, start_text, end_text, tty_text, held, peak = args[:6]
    argv = args[6:]
    host = store.host_name()
    cwd = os.getcwd()
    phase_path = os.environ.get("BUILDLOG_TEST_PHASE_FILE", "")
    identity: list[object] = [host, cwd, args]
    if phase_path:
        identity.append(str(Path(phase_path).absolute()))
    row_key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    pending_path = pending_steps_root() / f"{row_key}.json"
    done_path = pending_path.with_suffix(".done")
    held_log = Path(held).absolute() if held else NoHeldLog()
    peak_file = Path(peak).absolute() if peak else NoPeakFile()
    phase_file = Path(phase_path).absolute() if phase_path else NO_TEST_PHASE_RECORD
    store.root().mkdir(parents=True, exist_ok=True)
    try:
        with (store.root() / LAUNCH_LOCK_NAME).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if pending_path.exists():
                _ = finish_pending_step(pending_path)
                return
            if (held or peak or phase_path) and done_path.exists():
                return
        try:
            start = float(start_text)
            end = float(end_text) if end_text else time.time()
            status = int(status_text)
            started_at, ended_at = store.utc_iso(start), store.utc_iso(end)
        except (ValueError, OverflowError, OSError):
            store.note_error(f"record.py step {' '.join(args)[:500]}")
            _delete_step_sources(held_log, peak_file, phase_file)
            return
        tty = tty_text == "1"
        directory = git_directory(argv, cwd)
        repo_identity = repository_key(directory) if argv else store.UNKNOWN_IDENTITY
        tree = tree_facts(os.environ.get("BUILDLOG_TREE_START"), treekey.tree_key(directory))
        step_caller = caller(tty)
        sent_identity = sent_repository_identity()
        if (repo_identity == store.UNKNOWN_IDENTITY and step_caller in ("verify-mac", "verify-linux")
                and isinstance(sent_identity, SentRepositoryIdentity)):
            repo_identity = sent_identity.value
        sent_key = sent_tree_key()
        if step_caller in ("verify-mac", "verify-linux") and tree["tree_changed"] is None and isinstance(sent_key, SentTreeKey):
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
                log = _log_relative_path(host, start, record_id)
        output_roots = step_context.step_output_roots(argv, cwd) if name != "sweep" else CargoOutputUnavailable("sweep step")
        target_folder = step_context.captured_target_folder(os.environ.get("BUILDLOG_TARGET_FOLDER", ""))
        if (isinstance(output_roots, CargoOutputRoots)
                and isinstance(target_folder, step_context.CargoTargetFolder)
                and output_roots.target_directory != target_folder.path):
            store.note_error(f"step output folders: target_directory {output_roots.target_directory} "
                             + f"differs from target_dir {target_folder.path}")
            output_roots = CargoOutputUnavailable("step target folder mismatch")
        memory = peak_and_stall(peak)
        step_slice = "none" if not peak else "builds" if Path(peak).is_file() else "fallback"
        phase = read_test_phase(phase_file)
        if isinstance(phase, ReservedTestPhaseRecord) and isinstance(phase.window, ObservedTestPhase):
            charge = completed_test_phase_charge(phase.reservation_id)
            if isinstance(charge, dict):
                phase = ChargedTestPhaseRecord(phase.window, phase.context, phase.reservation_id, charge)
        record: dict[str, object] = {
            "kind": "step",
            "v": RECORD_VERSION,
            "id": record_id,
            "host": host,
            "target": recorded_target(host),
            "pinned": bool(os.environ.get("BUILDLOG_PINNED_TARGET")),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": round(end - start, 3),
            "mem_wait_s": int(os.environ.get("BUILDLOG_MEM_WAIT_S", "0")),
            "slice": step_slice,
            "step": name,
            "argv": argv,
            "cwd": cwd,
            "target_dir": step_context.target_folder_text(target_folder),
            **({"target_directory": str(output_roots.target_directory),
                "build_directory": str(output_roots.build_directory)}
               if isinstance(output_roots, CargoOutputRoots) else {}),
            "repo_identity": repo_identity,
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
            "scope_resources": scope_resources(peak),
            "peak_mem_bytes": memory.peak_bytes if isinstance(memory.peak_bytes, int) else None,
            "mem_stall_some_s": memory.stall_some_s if isinstance(memory.stall_some_s, float) else None,
            "mem_stall_full_s": memory.stall_full_s if isinstance(memory.stall_full_s, float) else None,
            **facts,
            "log": log,
            **(test_phase_fields(phase) if not isinstance(phase, UnreadableTestPhaseRecord) else {}),
        }
        with (store.root() / LAUNCH_LOCK_NAME).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if pending_path.exists():
                _ = finish_pending_step(pending_path)
                return
            if (held or peak or phase_path) and done_path.exists():
                return
            month_file = store.host_file(host, start)
            month_row_end = store.read_last_whole_row_end(month_file)
            pending = PendingStep(row_key, record, held_log, peak_file, month_row_end, phase_file, phase)
            _ = _finish_step(_save_pending_step(pending), pending, write_step_line)
    except Exception:  # noqa: BLE001 -- retain every source needed for a retry
        if pending_path.is_file():
            store.note_error(f"pending record {pending_path}")
        else:
            store.note_error(f"record.py step {' '.join(args)[:500]}")


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
        "pinned": bool(os.environ.get("BUILDLOG_PINNED_TARGET")),
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


class SshAttempt(TypedDict):
    reason: NotRequired[str]
    call_id: str
    target: str
    repo_identity: str
    queued_at: float
    departed_at: float
    slot: int
    outcome: str
    completion: str
    cleanup: str
    unresolved_count: int | None
    evidence_at: float


def ssh_attempt(attempt: SshAttempt) -> None:
    """Keep an SSH attempt when later local work changes the final target."""
    host = store.host_name()
    at = time.time()
    store.append_line(store.host_file(host, at), {
        **attempt, "kind": "ssh_attempt", "v": RECORD_VERSION,
        "pinned": bool(os.environ.get("BUILDLOG_PINNED_TARGET")),
        "id": f"{host}-{time.time_ns()}-{os.urandom(4).hex()}", "host": host,
        "at": store.utc_iso(at),
    })


@dataclass(frozen=True)
class AttemptResult:
    repo_identity: str
    slot: int
    outcome: str
    completion: str
    cleanup: str
    unresolved_count: int | MeasurementUnavailable
    evidence_at: float


def read_attempt_results(target: str) -> tuple[AttemptResult, ...]:
    """Read the latest repository slot results from host month files."""
    if not TYPE_CHECKING:
        from scripts.aws_target.config import MeasurementUnavailable

    latest: dict[tuple[str, int], AttemptResult] = {}
    for path in sorted(store.root().glob('*/[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl')):
        for row in store.locked_rows(path):
            if row.get('kind') != 'ssh_attempt' or row.get('target') != target:
                continue
            count = row.get('unresolved_count')
            at = float(str(row.get('evidence_at')))
            slot = int(str(row.get('slot')))
            if not math.isfinite(at) or at < 0 or slot < 0:
                raise ValueError('invalid attempt evidence')
            result = AttemptResult(str(row.get('repo_identity', 'unknown')), slot,
                                   str(row.get('outcome', 'unavailable')), str(row.get('completion', 'missing')),
                                   str(row.get('cleanup', 'unconfirmed')),
                                   count if type(count) is int and count >= 0 else MeasurementUnavailable('process count is unavailable'), at)
            key = result.repo_identity, result.slot
            if key not in latest or latest[key].evidence_at <= at:
                latest[key] = result
    return tuple(latest[key] for key in sorted(latest))

def hold_event(args: list[str]) -> None:
    from scripts.build_targets.target_config import SshTarget, read_targets

    event, target, key, holder, reason, at = args[:6]
    if event not in ("began", "ended") or len(args) not in (6, 7):
        raise ValueError("invalid hold-event arguments")
    if target not in ("mac", "natedev"):
        if not any(item.name == target and isinstance(item, SshTarget) for item in read_targets()):
            raise ValueError(f"unknown SSH hold target: {target}")
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


def head_admission(action: Literal["set", "admitted", "lapsed"], unit: str, step: str,
                   need: int, free: int, reserve: int, why: str, set_by: str, at: float) -> None:
    host = store.host_name()
    store.append_line(store.host_file(host, at), {
        "kind": "head_admission", "v": RECORD_VERSION,
        "id": f"{host}-{time.time_ns()}", "host": host, "at": store.utc_iso(at),
        "action": action, "unit": unit, "step": step, "need": need, "free": free,
        "reserve": reserve, "why": why, "set_by": set_by,
    })


def queue_priority(action: Literal["set", "clear"], unit: str,
                   why: str, set_by: str, at: float, *, transition_id: str = "", start: int = 0) -> None:
    host = store.host_name()
    _ = append_row_once(store.host_file(host, at), {
        "kind": "queue_priority", "v": RECORD_VERSION,
        "id": transition_id or f"{host}-{time.time_ns()}", "host": host, "at": store.utc_iso(at),
        "action": action, "unit": unit, "why": why, "set_by": set_by,
    }, start)


class IdleFloorTarget(TypedDict):
    folder: str
    last_use: float
    bytes: int
    takeable_bytes: NotRequired[int]
    reason: NotRequired[str]


def floor_scan(free_after_scan: int, floor: int, idle_targets: list[IdleFloorTarget], at: float,
               *, filesystem: str = "") -> None:
    host = store.host_name()
    store.append_line(store.host_file(host, at), {
        "kind": "floor_scan", "v": RECORD_VERSION,
        "id": f"{host}-{time.time_ns()}", "host": host, "at": store.utc_iso(at),
        "free_after_scan": free_after_scan, "floor": floor, "shortfall": floor - free_after_scan,
        "idle_targets": idle_targets,
        **({"filesystem": filesystem} if filesystem else {}),
    })


def sweep_trim(folder: str, bytes_freed: int, extent: Literal["whole", "partial"],
               cause: Literal["budget take", "floor take"], at: float) -> None:
    host = store.host_name()
    store.append_line(store.host_file(host, at), {
        "kind": "sweep_trim", "v": RECORD_VERSION,
        "id": f"{host}-{time.time_ns()}", "host": host, "at": store.utc_iso(at),
        "folder": folder, "bytes_freed": bytes_freed, "extent": extent, "cause": cause,
    })


def hold_build(args: list[str]) -> None:
    import hashlib

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
    import hashlib

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
        elif command == "test-phase-context":
            print(json.dumps(test_phase_context(rest[0], rest[1:]), separators=(",", ":")))
        elif command == "hold-event":
            hold_event(rest)
        elif command == "hold-build":
            hold_build(rest)
        elif command == "call":
            call(rest)
        elif command == "launch":
            if len(rest) != 2:
                raise ValueError("usage: record.py launch STATE WORKTREE")
            _ = launch(Path(rest[0]), Path(rest[1]))
        else:
            store.note_error(f"record.py: unknown command {command}")
    except Exception:  # noqa: BLE001 -- the recorder never fails the build it records
        store.note_error(f"record.py {' '.join(argv)[:500]}")
    return 0


if __name__ == "__main__":
    _ = sys.modules.setdefault("record", sys.modules[__name__])
    sys.exit(main(sys.argv[1:]))
