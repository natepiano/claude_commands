"""The compact queue view and the checks shared with one-step cards."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field, replace
from functools import cache, cached_property
from enum import Enum
import os
import inspect
import logging
import re
import shlex
import json
from pathlib import Path
import subprocess
import time
from typing import cast
from zoneinfo import ZoneInfo

from scripts.buildlog import store
from scripts.build_queue import head_facts, queue_evidence, queue_lines, placement
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_records import (reason_phrase as reason_phrase, behind_place as behind_place,
    eligible_head_position as eligible_head_position)
from scripts.build_queue.queue_evidence import step_labels as step_labels
from scripts.build_queue.queue_lines import Line, Span, age, clip, cut_tail, plain, field, wrap
from scripts.lib.terminal_cells import pad_left, pad_right, terminal_width
from scripts.lib import cargo_target_config, conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import RECORDED_STEP_NAMES, StepNames, read_step_names
from scripts.build_queue import admission_records
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, ScopeProcesses


@dataclass(frozen=True)
class MacBuilding:
    """No test executable runs in this build's process tree."""


@dataclass(frozen=True)
class MacTesting:
    test_pid: int


@dataclass(frozen=True)
class MacTestingCounter:
    counter: progress_profile.ProgressCounter


@dataclass(frozen=True)
class MacStageUnavailable:
    """No process reading supplies the build stage."""


MacBuildStage = MacBuilding | MacTesting | MacTestingCounter | MacStageUnavailable


@dataclass(frozen=True)
class MacMemoryUnavailable:
    """No resident memory reading exists for this process tree."""


@dataclass(frozen=True)
class MacJobsUnavailable:
    """The command does not specify a positive jobs value."""


class MacBuildOrigin(Enum):
    CI = "ci"
    NON_CI = "non-ci"
    UNKNOWN = "unknown"


UNRECORDED_OWNER = UnitLabels("")


@dataclass(frozen=True)
class MacBuild:
    pid: int
    folder: str
    started_at: float
    command: str = ""
    repository: str = ""
    stage: MacBuildStage = MacStageUnavailable()
    resident_bytes: int | MacMemoryUnavailable = MacMemoryUnavailable()
    jobs: int | MacJobsUnavailable = MacJobsUnavailable()
    counter: progress_profile.ProgressCounter | progress_profile.NoCounter = progress_profile.NoCounter()
    recorded_unit: str = ""
    session: str = ""
    origin: MacBuildOrigin = MacBuildOrigin.UNKNOWN
    ci_runner: str = ""
    ci_job: str = ""


@dataclass(frozen=True)
class MacBuildReading:
    read_at: float
    builds: tuple[MacBuild, ...]
    error: str = ""


def mac_reading_path() -> Path:
    """Use the feed folder for the shared periodic Mac reading."""
    folder = Path(os.path.expandvars(conf.read("memory_feed").text("output_folder"))).expanduser()
    return folder / "mac-builds.json"


def mac_activity_refresh_seconds() -> int:
    return conf.read("memory_feed").whole("mac_activity_refresh_seconds")


def mac_command_jobs(command: str) -> int | MacJobsUnavailable:
    match = re.search(r"(?:^|\s)(?:-j\s*|--(?:jobs|test-threads)(?:=|\s+))([0-9]+)(?:\s|$)", command)
    return int(match[1]) if match and int(match[1]) > 0 else MacJobsUnavailable()


def mac_step_name(build: MacBuild, *, names: StepNames = RECORDED_STEP_NAMES,
                  identity: UnitLabels = UNRECORDED_OWNER) -> str:
    try:
        words = shlex.split(build.command)
    except ValueError:
        words = []
    step = "cargo"
    for index, word in enumerate(words):
        name = Path(word).name
        if name in ("cargo", "cargo-nextest"):
            step = "nextest" if name == "cargo-nextest" else "cargo"
            if name == "cargo-nextest":
                break
            for argument in words[index + 1:]:
                if argument in ("build", "check", "clippy", "nextest", "test", "run", "bench"):
                    step = argument
                    break
            break
        if name == "rustc":
            step = "rustc"
            break
    if step == "cargo" and isinstance(build.stage, (MacTesting, MacTestingCounter)):
        step = "test"
    package = ""
    for index, word in enumerate(words):
        if word in ("-p", "--package") and index + 1 < len(words):
            package = words[index + 1]
            break
        if word.startswith("--package="):
            package = word.removeprefix("--package=")
            break
        if word.startswith("-p") and len(word) > 2:
            package = word[2:]
            break
    current_name = " ".join(part for part in (package or build.repository, step) if part)
    return names.for_owner(step, identity.unit if identity.unit != "—" else "", identity.role,
                           ci=build.origin is MacBuildOrigin.CI, current_name=current_name)


def mac_build_record(build: MacBuild) -> dict[str, object]:
    return {"pid": build.pid, "folder": build.folder, "started_at": build.started_at,
            "command": build.command, "repository": build.repository,
            "test_pid": build.stage.test_pid if isinstance(build.stage, MacTesting) else
                0 if isinstance(build.stage, MacBuilding) else -1,
            "resident_bytes": build.resident_bytes if isinstance(build.resident_bytes, int) else -1,
            "counter": {"phase": build.counter.phase, "done": build.counter.done, "total": build.counter.total}
                if isinstance(build.counter, progress_profile.ProgressCounter) else {},
            "recorded_unit": build.recorded_unit, "session": build.session, "origin": build.origin.value,
            "ci_runner": build.ci_runner, "ci_job": build.ci_job}


def mac_build_from_record(row: dict[str, object]) -> MacBuild:
    test_pid = int(cast(int, row["test_pid"]))
    resident = int(cast(int, row["resident_bytes"]))
    command = str(row["command"])
    counter_data = cast(dict[str, object], row["counter"])
    counter = progress_profile.ProgressCounter(str(counter_data["phase"]), int(cast(int, counter_data["done"])),
        int(cast(int, counter_data["total"]))) if counter_data else progress_profile.NoCounter()
    stage = MacTesting(test_pid) if test_pid > 0 else MacBuilding() if test_pid == 0 else MacStageUnavailable()
    if isinstance(counter, progress_profile.ProgressCounter) and counter.phase.startswith("test:"):
        stage = MacTestingCounter(counter)
    return MacBuild(int(cast(int, row["pid"])), str(row["folder"]), float(cast(float, row["started_at"])),
                    command, str(row["repository"]), stage,
                    resident if resident >= 0 else MacMemoryUnavailable(), mac_command_jobs(command), counter,
                    str(row["recorded_unit"]), str(row["session"]),
                    MacBuildOrigin(str(row.get("origin", MacBuildOrigin.UNKNOWN.value))),
                    str(row.get("ci_runner", "")), str(row.get("ci_job", "")))


def read_mac_builds() -> MacBuildReading:
    """Read the cached Mac activity without a network call."""
    try:
        data = cast(dict[str, object], json.loads(mac_reading_path().read_text()))
        rows = cast(list[dict[str, object]], data["builds"])
        return MacBuildReading(float(cast(float, data["read_at"])), tuple(mac_build_from_record(row) for row in rows),
                               str(data.get("error", "")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        logging.getLogger(__name__).warning("Mac reading unavailable: %s", error)
        return MacBuildReading(0, (), "Mac reading unavailable")


def mac_reading_status(reading: MacBuildReading, now: float) -> str:
    """Replace old native rows with one short status and the reading age."""
    elapsed = max(0, now - reading.read_at)
    if not reading.error and elapsed <= 2 * mac_activity_refresh_seconds():
        return ""
    age_text = cloud_update_age(elapsed) if reading.read_at else "unavailable"
    return f"Mac reading is old · age {age_text}"


def mac_build_status(reading: MacBuildReading, now: float) -> str:
    elapsed = cloud_update_age(max(0, now - reading.read_at))
    return (f"started on the Mac · reading age {elapsed} · "
            "Progress unavailable: Mac process has no readable counter")


def mac_build_script() -> str:
    """Read cargo tile registrations and process trees in one SSH call."""
    return "python3 - <<'MAC_READING'\n" + inspect.getsource(cargo_target_config) + r'''
import ctypes
import json
import struct
import sys
import os
from pathlib import Path
import shlex
import subprocess
import time
import tomllib

CAPTURE_ROOT = Path("/tmp/cargo-tile")


def output(arguments):
    return subprocess.check_output(arguments, text=True, stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, LC_ALL="C"), timeout=4)


def read_processes():
    rows = {}
    for line in output(["ps", "-axww", "-o", "pid=,ppid=,uid=,rss=,etime=,lstart=,comm="]).splitlines():
        fields = line.split(None, 10)
        if len(fields) != 11:
            continue
        pid, parent, uid, rss = map(int, fields[:4])
        try:
            born = int(time.mktime(time.strptime(" ".join(fields[5:10]), "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            continue
        rows[pid] = dict(pid=pid, parent=parent, uid=uid, resident_bytes=rss * 1024,
                         started_at=born, executable=fields[10], command="")
    for line in output(["ps", "-axww", "-o", "pid=,args="]).splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and int(fields[0]) in rows:
            rows[int(fields[0])]["command"] = fields[1]
    return rows


def read_captures(processes):
    try:
        boot = output(["sysctl", "-n", "kern.bootsessionuuid"]).strip()
    except (OSError, subprocess.SubprocessError):
        return {}
    captures = {}
    for account in CAPTURE_ROOT.glob("[0-9]*"):
        if not account.name.isdecimal() or account.is_symlink():
            continue
        for record in (account / "state/pids").glob("*"):
            try:
                pid_text, generation = record.name.split(".", 1)
                pid = int(pid_text)
                process = processes.get(pid)
                if not process or process["uid"] != int(account.name):
                    continue
                if record.is_symlink() or record.stat().st_size > 65536:
                    continue
                raw = record.read_bytes()
                if not raw.endswith(b"\0"):
                    continue
                fields = raw[:-1].split(b"\0")
                if len(fields) < 8 or fields[0] != b"cargo-tile-v3":
                    continue
                if fields[1].decode() != generation or fields[2].decode() != boot:
                    continue
                if int(fields[3]) != process["started_at"]:
                    continue
                log_name = fields[4].decode()
                if Path(log_name).name != log_name:
                    continue
                folder = fields[5].decode()
                if not Path(folder).is_absolute():
                    continue
                count_at = 7 if not fields[6] or fields[6].startswith(b"/") else 6
                arguments = [word.decode() for word in fields[count_at + 1:]]
                if int(fields[count_at]) != len(arguments):
                    continue
                log = account / log_name
                tail = ""
                if not log.is_symlink():
                    try:
                        with log.open("rb") as stream:
                            stream.seek(max(0, log.stat().st_size - 65536))
                            tail = stream.read(65536).decode(errors="replace")
                    except OSError:
                        pass
                captures[pid] = dict(folder=folder, command=shlex.join(["cargo", *arguments]), progress_tail=tail)
            except (OSError, ValueError, UnicodeError, IndexError):
                continue
    return captures


def project_record(folder):
    try:
        tree = Path(folder).resolve()
        for parent in (tree, *tree.parents):
            for name in (".copy-record.json", ".scratch.json", ".gate.json", ".gate-copy.json"):
                record = parent / name
                try:
                    if record.is_symlink() or record.stat().st_size > 65536:
                        continue
                    values = json.loads(record.read_text())
                    owner = values.get("owner_unit", values.get("owner", ""))
                    session = values.get("session", values.get("holder_session", ""))
                    repo = values.get("repo", "")
                    if name == ".gate-copy.json" and not tree.is_relative_to(parent):
                        continue
                    return dict(recorded_unit=owner if isinstance(owner, str) else "",
                                session=session if isinstance(session, str) else "",
                                repository=repo if isinstance(repo, str) else "")
                except (FileNotFoundError, ValueError, TypeError):
                    continue
            if (parent / ".git").exists():
                break
    except OSError:
        pass
    return dict(recorded_unit="", session="", repository="")


def repository_name(folder):
    try:
        remote = output(["git", "-C", folder, "config", "--get", "remote.origin.url"]).strip()
        for _ in range(8):
            if not remote.startswith("/"):
                break
            remote = output(["git", "-C", remote, "config", "--get", "remote.origin.url"]).strip()
        if "://" in remote or remote.startswith("git@"):
            return remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        manifest = tomllib.loads((Path(folder) / "Cargo.toml").read_text())
        package = manifest.get("workspace", {}).get("package", {}) or manifest.get("package", {})
        repository = package.get("repository", "")
        if isinstance(repository, str) and ("://" in repository or repository.startswith("git@")):
            return repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, ValueError, TypeError):
        pass
    return ""


def process_environment(pid):
    """Read only Cargo paths and CI labels from the Mac environment."""
    if sys.platform != "darwin":
        return {}
    try:
        kernel = ctypes.CDLL(None, use_errno=True)
        kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        kernel.sysctl.restype = ctypes.c_int
        # sysinfo uses CTL_KERN and KERN_PROCARGS2 for the same process observation.
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if kernel.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 <= size.value <= 2097152:
            return {}
        data = ctypes.create_string_buffer(size.value)
        if kernel.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
            return {}
        return cargo_environment(data.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return {}


def cargo_environment(data):
    """Keep only Cargo paths and CI labels from the environment."""
    if len(data) < 4:
        return {}
    argc = struct.unpack_from("i", data)[0]
    if argc < 1:
        return {}
    start = data.find(b"\0", 4)
    if start < 0:
        return {}
    start += 1
    while start < len(data) and data[start] == 0:
        start += 1
    fields = data[start:].split(b"\0")
    selected = {}
    for field in fields[argc:]:
        if not field:
            break
        key, separator, value = field.partition(b"=")
        if separator and key in (b"CARGO_TARGET_DIR", b"CARGO_BUILD_TARGET_DIR", b"CARGO_HOME", b"HOME",
                                 b"RUNNER_NAME", b"GITHUB_JOB"):
            selected[os.fsdecode(key)] = os.fsdecode(value)
    return selected


def test_output_directories(pid, folder, command, members, processes):
    """Resolve target settings and observed compiler output directories."""
    try:
        arguments = shlex.split(command)
        environment = process_environment(pid)
        settings = cargo_folder_settings(arguments, folder, environment)
        if "target-dir" in settings:
            setting = settings["target-dir"]
            target = setting.base / setting.value
        else:
            try:
                target = cargo_workspace_manifest(arguments, folder).parent / "target"
            except (OSError, ValueError, TypeError):
                target = Path(folder) / "target"
        targets = {target.resolve()}
    except (OSError, ValueError, TypeError):
        targets = set()
    outputs = set()
    for member in members:
        try:
            words = shlex.split(processes[member]["command"])
            directory = cargo_path_argument(words, "--out-dir") if "--crate-name" in words else ""
            if directory:
                outputs.add((Path(folder) / directory).resolve())
        except (OSError, ValueError):
            continue
    return targets, outputs


def running_test_pid(pid, folder, command, members, processes):
    targets, outputs = test_output_directories(pid, folder, command, members, processes)
    for member in members:
        try:
            executable = Path(processes[member]["executable"]).resolve()
        except (OSError, RuntimeError):
            continue
        if (executable.parent.name == "deps" and
                not executable.name.startswith(("build-script", "build_script_")) and
                (executable.parent in outputs or any(executable.is_relative_to(target) for target in targets))):
            return member
    return 0


def build_origin(pid, processes):
    """Identify a build with a GitHub Actions runner ancestor."""
    cursor = pid
    seen = set()
    while cursor in processes and cursor not in seen:
        seen.add(cursor)
        try:
            words = shlex.split(processes[cursor]["command"])
        except ValueError:
            return "unknown"
        if words and Path(words[0]).name in ("Runner.Worker", "Runner.Listener"):
            return "ci"
        cursor = processes[cursor]["parent"]
    return "non-ci"


def read_builds():
    processes = read_processes()
    captures = read_captures(processes)
    candidates = set(captures)
    for pid, process in processes.items():
        if Path(process["executable"]).name in ("cargo", "cargo-nextest"):
            candidates.add(pid)
    roots = []
    for pid in candidates:
        cursor = processes[pid]["parent"]
        seen = {pid}
        while cursor in processes and cursor not in seen and cursor not in candidates:
            seen.add(cursor)
            cursor = processes[cursor]["parent"]
        if cursor not in candidates:
            roots.append(pid)
    cwd = {}
    if roots:
        try:
            current = 0
            for line in output(["lsof", "-a", "-p", ",".join(map(str, roots)), "-d", "cwd", "-Fpn"]).splitlines():
                if line.startswith("p"):
                    current = int(line[1:])
                elif line.startswith("n"):
                    cwd[current] = line[1:]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    builds = []
    for pid in roots:
        members = []
        for child in processes:
            cursor = child
            seen = set()
            while cursor in processes and cursor not in seen and cursor != pid:
                seen.add(cursor)
                cursor = processes[cursor]["parent"]
            if cursor == pid:
                members.append(child)
        capture_ids = [member for member in members if member in captures and captures[member]["progress_tail"]]
        capture = captures.get(pid, {})
        folder = capture.get("folder", cwd.get(pid, ""))
        if not folder:
            continue
        try:
            record = project_record(folder)
        except Exception:
            record = dict(recorded_unit="", session="", repository="")
        command = capture.get("command", processes[pid]["command"])
        try:
            test_pid = running_test_pid(pid, folder, command, members, processes)
        except Exception:
            test_pid = -1
        try:
            repository = record["repository"] or repository_name(folder)
        except Exception:
            repository = ""
        try:
            origin = build_origin(pid, processes)
        except Exception:
            origin = "unknown"
        ci_environment = process_environment(pid) if origin == "ci" else {}
        try:
            folder = str(Path(folder).resolve())
        except OSError:
            pass
        tail = capture.get("progress_tail", "")
        if not tail and capture_ids:
            tail = captures[min(capture_ids, key=lambda value: processes[value]["started_at"])]["progress_tail"]
        builds.append(dict(pid=pid, folder=folder, started_at=processes[pid]["started_at"],
                           command=command, test_pid=test_pid,
                           resident_bytes=sum(processes[member]["resident_bytes"] for member in members),
                           progress_tail=tail, repository=repository,
                           recorded_unit=record["recorded_unit"], session=record["session"],
                           origin=origin, ci_runner=ci_environment.get("RUNNER_NAME", ""),
                           ci_job=ci_environment.get("GITHUB_JOB", "")))
    return sorted(builds, key=lambda build: (build["started_at"], build["pid"]))


print(json.dumps(read_builds()))
print("rc=0")
''' + "MAC_READING\n"


def refresh_mac_builds() -> None:
    """Refresh the shared reading from the feed loop at its configured interval."""
    now = time.time()
    previous = read_mac_builds()
    try:
        settings = conf.read("mac_test")
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o",
            f"ConnectTimeout={settings.number('connect_timeout_s'):g}", settings.text("host"), mac_build_script()],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode or result.stderr or not result.stdout.endswith("rc=0\n"):
            raise ValueError(" ".join(result.stderr.split()) or "Mac activity check failed")
        records = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
        builds: list[MacBuild] = []
        for row in records:
            counter = progress_profile.parse_progress(str(row.pop("progress_tail")))
            row["counter"] = ({"phase": counter.phase, "done": counter.done, "total": counter.total}
                              if isinstance(counter, progress_profile.ProgressCounter) else {})
            builds.append(mac_build_from_record(row))
        reading = MacBuildReading(now, tuple(sorted(builds, key=lambda build: build.started_at)))
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        logging.getLogger(__name__).warning("Mac reading unavailable: %s", error)
        reading = MacBuildReading(previous.read_at, previous.builds, "Mac reading unavailable")
    write_text_atomic(mac_reading_path(), json.dumps({"read_at": reading.read_at, "error": reading.error,
        "builds": [mac_build_record(build) for build in reading.builds]}), new_file_mode=0o644)

def step_counter(step: admission_records.RunningStep) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
    """Read the counter from the local memory reading."""
    if isinstance(step.measured, admission_records.ProgressAdjustedMemory):
        return step.measured.counter
    return progress_profile.NoCounter("the step has no readable counter")


@dataclass(frozen=True)
class UnknownCpuCount:
    pass


@dataclass(frozen=True)
class HostLoad:
    load: float
    cpus: int | UnknownCpuCount


@dataclass(frozen=True)
class HostLoadUnavailable:
    pass


@dataclass(frozen=True)
class QueueModel:
    reading: admission_records.QueueRead
    labels: Mapping[str, UnitLabels]
    facts: head_facts.HeadFacts | admission_records.TokenWaitersOnly | admission_records.NoWaiter
    pace: head_facts.RecordedPace | head_facts.PaceUnavailable
    zone: ZoneInfo
    load: HostLoad | HostLoadUnavailable
    release: gate.HeadAfterEnd
    ci_memory_slice: str = ""
    names: StepNames = dataclass_field(default_factory=read_step_names)

    @cached_property
    def moves(self) -> head_facts.QueueMoves:
        return head_facts.queue_moves(self.reading, self.facts)

    @cached_property
    def cloud_labels(self) -> Mapping[str, UnitLabels]:
        missing = {row.identity.session_id for row in cloud_rows(self)
                   if row.identity.session_id and row.identity.session_id not in self.labels}
        return {**unit_labels.unit_labels_for(missing, os.environ), **self.labels} if missing else self.labels


def same_waiter(left: admission_records.WaitingStep, right: admission_records.WaitingStep) -> bool:
    """Match a saved decision to its row even when display fields change."""
    return left.pid == right.pid and left.since == right.since


def step_checks(model: QueueModel, position: int) -> admission_records.HeadExplanation | admission_records.LineWaiter | admission_records.ReadingUnavailable:
    explanation = model.reading.explanation
    if isinstance(explanation.host, admission_records.ReadingUnavailable):
        return explanation.host
    step = model.reading.view.waiting[position - 1]
    if isinstance(step.reason, admission_records.ReadingUnavailable):
        return step.reason
    if isinstance(explanation.head, admission_records.HeadExplanation) and same_waiter(explanation.head.waiter, step):
        return explanation.head
    return next((item for item in explanation.line.steps if same_waiter(item.waiter, step)),
                admission_records.ReadingUnavailable("step admission checks unavailable"))


def shown_waiters(selector: str, reading: admission_records.QueueRead, labels: Mapping[str, UnitLabels]) -> list[int]:
    return [position for position, step in enumerate(reading.view.waiting, 1)
            if step_labels(step, labels).session == selector]


def running_matches_selector(step: admission_records.RunningStep, selector: str, labels: Mapping[str, UnitLabels]) -> bool:
    return (selector == step_labels(step, labels).session
            or gate.step_matches(selector, step)
            or isinstance(step.measured, admission_records.UnownedSliceMemory) and selector == step.measured.owner)


def name_spans(step: admission_records.WaitingStep | admission_records.RunningStep, labels: Mapping[str, UnitLabels],
               *, production: bool = True) -> Line:
    identity = step_labels(step, labels)
    seen: set[str] = set()
    parts: list[Span] = []
    names = ((identity.showrunner, False),) if production else ()
    for text, session in (*names, (identity.unit, False), (identity.session, True)):
        text = field(text)
        if text == "—" or not text or text in seen:
            continue
        seen.add(text)
        if parts:
            parts.append(Span(" · "))
        parts.append(Span(text, "inactive" if session else "default"))
    return tuple(parts)


def name_text(step: admission_records.WaitingStep | admission_records.RunningStep, model: QueueModel) -> str:
    return plain([name_spans(step, model.labels)])


def clock(model: QueueModel, at: float, *, seconds: bool = False) -> str:
    return head_facts.clock(at, model.zone, seconds=seconds)


def estimate_minutes(model: QueueModel, position: int) -> int:
    return max(1, round(position * 60 / model.pace.steps)) if isinstance(model.pace, head_facts.RecordedPace) and model.pace.steps else 0


def start_text(model: QueueModel, position: int, *, clock_time: bool = False) -> str:
    minutes = estimate_minutes(model, position)
    if not minutes:
        return "—"
    return "~" + (clock(model, model.reading.explanation.now + minutes * 60) if clock_time else f"{minutes} min")


def waiting_moves(model: QueueModel, position: int) -> tuple[head_facts.BumpRecord, ...]:
    return model.moves.waiting[position - 1]


def is_moved(model: QueueModel, position: int) -> bool:
    return has_move_priority(model, position)


def has_move_priority(model: QueueModel, position: int) -> bool:
    return isinstance(admission_records.waiter_priority(model.reading.ordered[position - 1][1]), admission_records.PriorityUnit)


def pressure_check(pressure: admission_records.Pressure, model: QueueModel) -> tuple[bool, str]:
    if isinstance(pressure, admission_records.NoPressureHold):
        return True, "memory pressure: no hold"
    return False, queue_evidence.pressure_text(pressure, model.reading.explanation.policy)


def check_line(passes: bool, text: str) -> Line:
    return (Span("✓ " if passes else "✗ ", "success" if passes else "error"), Span(text))


def check_lines(checks: admission_records.HeadExplanation | admission_records.LineWaiter | admission_records.ReadingUnavailable,
                model: QueueModel) -> list[Line]:
    if isinstance(checks, admission_records.ReadingUnavailable):
        return [(Span(reason_phrase(checks, model.reading.view.waiting)),)]
    return [check_line(checks.room.passes, queue_evidence.room_check_text(checks.room))]








def bar_eighths(gib: float, cap: float) -> int:
    """Round to the nearest eighth on the shared one-cell-per-GiB scale."""
    return int(max(0.0, min(gib, cap)) * 8 + 0.5)


def size_bar(gib: float, cap: float) -> str:
    eighths = bar_eighths(gib, cap)
    whole, fraction = divmod(eighths, 8)
    return "█" * whole + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else "")


def head_status(model: QueueModel) -> Line:
    head = model.reading.explanation.head
    if not isinstance(head, admission_records.HeadExplanation):
        return ()
    result: Line = (Span(" ↑", "warning" if head.has_priority else "success"),)
    cause = head.cause
    if isinstance(cause, admission_records.MemoryPressure):
        return (*result, Span(" pressure", "error"))
    if isinstance(cause, admission_records.PressureUnavailable):
        return (*result, Span(" pressure unreadable", "error"))
    if isinstance(cause, admission_records.StartSpacingWait):
        return (*result, Span(" spacing", "inactive"))
    if isinstance(cause, admission_records.EstimateExpired):
        return (*result, Span(" estimate expired", "error"))
    if isinstance(cause, admission_records.ReadingUnavailable):
        return (*result, Span(" " + cause.reason, "error"))
    if isinstance(cause, admission_records.NeedsMemory):
        return (*result, Span(" low room", "error"))
    if isinstance(cause, admission_records.BehindHead):
        return (*result, Span(" queue order", "inactive"))
    return result


def cap_bar(room: admission_records.MemoryRoom | admission_records.ReadingUnavailable, width: int, *, colour: bool) -> Line:
    """Draw captured use, keep-aside, room, safety, and other programs."""
    if not isinstance(room, admission_records.MemoryRoom) or not isinstance(room.cap, int):
        return (Span("cap unavailable", "inactive"),)
    total = room.total if isinstance(room.total, int) else max(1, room.cap + room.safety)
    cells = max(1, min(width, (total + admission_records.GIB - 1) // admission_records.GIB))
    scale = total / cells
    cap_at = min(cells - 1, max(0, int(room.cap / scale)))
    used = room.cap_use
    kept_end = min(room.cap, used + room.kept)
    result: list[Span] = []
    for cell in range(cells):
        point = (cell + 0.5) * scale
        if cell == cap_at:
            result.append(Span("│", "error"))
        elif point < min(used, room.cap):
            result.append(Span("█", "suggestion"))
        elif point < kept_end:
            result.append(Span("░", "warning"))
        elif point < room.cap:
            result.append(Span("█" if colour else "·", "success"))
        elif point < room.cap + room.safety:
            result.append(Span("█" if colour else "▒", "warning"))
        else:
            result.append(Span("█", "inactive"))
    return tuple(result)


@dataclass(frozen=True)
class RunningBuild:
    step: admission_records.RunningStep
    used: int
    kept: int
    started_at: float
    measured: bool
    swap_bytes: int = 0


def anonymous_measurement(use: admission_records.AnonymousRunUse | admission_records.RunUseUnavailable) -> tuple[int, bool]:
    """Show the captured amount and whether its reading is available."""
    return (use.bytes, True) if isinstance(use, admission_records.AnonymousRunUse) else (0, False)


def running_builds(model: QueueModel) -> list[RunningBuild]:
    """Combine captured keep-asides and scope evidence for each run."""
    explanation = model.reading.explanation
    captured = explanation.room
    holds = {hold.memory.owner: hold for hold in captured.runs} if isinstance(captured, admission_records.MemoryRoom) else {}
    rows: dict[str, RunningBuild] = {}
    for index, running in enumerate(model.reading.explanation.running):
        step = running.step
        memory = step.measured
        if isinstance(memory, (admission_records.GoneMemory, admission_records.CompilerServiceMemory, admission_records.UnownedSliceMemory)):
            continue
        key = admission_records.execution_group(step, str(index))
        started = step.run.started_at if isinstance(step.run, LiveBuildRun) else step.started_at
        if not started and isinstance(running.admitted_at, float):
            started = running.admitted_at
        previous = rows.get(key)
        used = (memory.anonymous.bytes if isinstance(memory, admission_records.MeasuredMemory)
                and isinstance(memory.anonymous, admission_records.AnonymousRunUse) else 0)
        measured = isinstance(memory, admission_records.MeasuredMemory) and isinstance(memory.anonymous, admission_records.AnonymousRunUse)
        hold = holds.get(running.owner)
        if hold is not None and not isinstance(step.run, LiveBuildRun):
            used, measured = anonymous_measurement(hold.memory.use)
        kept = hold.bytes if hold is not None and not isinstance(step.run, LiveBuildRun) else 0
        if previous is None:
            rows[key] = RunningBuild(step, used, kept, started, measured,
                                      memory.swap_bytes if isinstance(memory, admission_records.MeasuredMemory) else 0)
        else:
            chosen = step if step.promise and not previous.step.promise else previous.step
            rows[key] = RunningBuild(chosen, previous.used + used, previous.kept + kept,
                                     previous.started_at or started, previous.measured and measured,
                                     previous.swap_bytes + (memory.swap_bytes if isinstance(memory, admission_records.MeasuredMemory) else 0))
    if isinstance(captured, admission_records.MemoryRoom):
        for key, row in rows.items():
            if not isinstance(row.step.run, LiveBuildRun):
                continue
            hold = holds.get(admission_records.run_owner(row.step.run.pid, row.step.run.start_time))
            if hold is not None:
                used, measured = anonymous_measurement(hold.memory.use)
                rows[key] = RunningBuild(row.step, used, hold.bytes, row.started_at, measured, row.swap_bytes)
        owners = {admission_records.run_owner(row.step.run.pid, row.step.run.start_time) for row in rows.values()
                  if isinstance(row.step.run, LiveBuildRun)}
        owners.update(entry.owner for entry in explanation.running
                      if not isinstance(entry.step.measured, admission_records.UnownedSliceMemory))
        for hold in captured.runs:
            run = hold.memory.run
            if hold.memory.owner in owners or not isinstance(run, LiveBuildRun):
                continue
            used, measured = anonymous_measurement(hold.memory.use)
            step = admission_records.RunningStep("", "", "between steps", "", 0, admission_records.MeasuredMemory(used), pid=run.pid, run=run)
            rows[admission_records.run_owner(run.pid, run.start_time)] = RunningBuild(step, used, hold.bytes, 0.0, measured)
    return list(rows.values())


def running_bar(used: float, grant: float, cap: float) -> Line:
    solid = size_bar(used, cap)
    rest = max(0, (bar_eighths(max(used, grant), cap) + 7) // 8 - len(solid))
    return (Span(solid, "suggestion"), Span("░" * rest, "inactive"))


def started_ago(started: float, now: float) -> str:
    if not started:
        return "start time unknown"
    seconds = max(0, int(now - started))
    for unit, scale in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= scale or scale == 1:
            count = seconds // scale
            return f"started {count} {unit}{'' if count == 1 else 's'} ago"
    raise AssertionError("the seconds scale always matches")


def leftover_age(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def leftover_processes(steps: Sequence[admission_records.RunningStep]) -> list[tuple[admission_records.RunningStep, admission_records.UnownedSliceMemory, ScopeProcesses]]:
    """Return the process scopes whose builds have ended."""
    ended: list[tuple[admission_records.RunningStep, admission_records.UnownedSliceMemory, ScopeProcesses]] = []
    for step in steps:
        if not isinstance(step.measured, admission_records.UnownedSliceMemory):
            continue
        activity = step.measured.activity
        if not isinstance(step.measured.job_state, NotServiceSlice):
            continue
        if isinstance(activity, ScopeProcesses) and not isinstance(activity.run, LiveBuildRun):
            ended.append((step, step.measured, activity))
    return ended


@dataclass(frozen=True)
class CloudPushInterval:
    seconds: float


@dataclass(frozen=True)
class CloudPushIntervalUnavailable:
    reason: str


def cloud_push_interval() -> CloudPushInterval | CloudPushIntervalUnavailable:
    try:
        return CloudPushInterval(conf.read("cloud_gate").number("log_push_s"))
    except conf.Unusable as error:
        return CloudPushIntervalUnavailable(str(error))


def cloud_rows(model: QueueModel) -> tuple[wire.CloudRow, ...]:
    return model.reading.cloud if isinstance(model.reading, wire.CloudQueueRead) else ()


def cloud_matches(row: wire.CloudRow, selector: str, labels: Mapping[str, UnitLabels]) -> bool:
    identity = row.identity
    label = labels.get(identity.session_id)
    return selector in (str(row.owner.pid), identity.session_id, identity.name, identity.unit,
                        identity.folder, Path(identity.folder).name, label.session if label else "")


@dataclass(frozen=True)
class CloudUpdateCurrent:
    pass


@dataclass(frozen=True)
class CloudUpdateLate:
    seconds: float


@dataclass(frozen=True)
class CloudWork:
    state: str
    modules: str
    vcpus: int | UnknownCpuCount
    failed_modules: int
    update: CloudUpdateCurrent | CloudUpdateLate


def cloud_work(row: wire.CloudRow, now: float, stale_pushes: int,
               interval: CloudPushInterval | CloudPushIntervalUnavailable) -> CloudWork:
    progress = row.progress
    state = (row.wait.text if isinstance(row.wait, wire.CloudSlotWait) else progress.state
             if isinstance(progress, cloud_progress.CloudProgress) else "progress " + progress.reason)
    modules = (f"{row.counter.done} of {row.counter.total} modules"
               if isinstance(row.counter, progress_profile.ProgressCounter) else "modules unknown")
    update: CloudUpdateCurrent | CloudUpdateLate = CloudUpdateCurrent()
    if (isinstance(progress, cloud_progress.CloudProgress) and isinstance(interval, CloudPushInterval)
            and now - progress.updated_at >= stale_pushes * interval.seconds):
        update = CloudUpdateLate(now - progress.updated_at)
    return CloudWork(state, modules,
                     progress.size_vcpus if isinstance(progress, cloud_progress.CloudProgress) else UnknownCpuCount(),
                     progress.failed_modules if isinstance(progress, cloud_progress.CloudProgress) else 0, update)


def cloud_update_age(seconds: float) -> str:
    for divisor, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= divisor:
            return f"{int(seconds // divisor)} {unit}"
    return f"{int(seconds)} s"


def cloud_detail(row: wire.CloudRow) -> str:
    progress = row.progress
    if not isinstance(progress, cloud_progress.CloudProgress):
        return ""
    zone = progress.zone.removeprefix(progress.region) if progress.region else progress.zone
    location = ", ".join(filter(None, (progress.region, f"zone {zone}" if zone else "")))
    return " · ".join(filter(None, (progress.stage, location)))


def cloud_status(row: wire.CloudRow, work: CloudWork) -> Line:
    if isinstance(row.wait, wire.CloudSlotWait):
        return (Span(" · ".join(filter(None, (work.state, cloud_detail(row))))),)
    text = " · ".join(filter(None, (work.state, cloud_detail(row), work.modules, cloud_failed_modules(work))))
    return (Span(text),)


def cloud_failed_modules(work: CloudWork) -> str:
    if work.failed_modules <= 0:
        return ""
    noun = "module" if work.failed_modules == 1 else "modules"
    return f"{work.failed_modules} failed {noun}"


def cloud_page_status(row: wire.CloudRow, work: CloudWork) -> Line:
    if isinstance(row.wait, wire.CloudSlotWait):
        return cloud_status(row, work)
    parts = (work.state if work.state != "running" else "",
             cloud_detail(row),
             work.modules if isinstance(row.counter, progress_profile.ProgressCounter) else "",
             cloud_failed_modules(work))
    return (Span(" · ".join(filter(None, parts))),)


def cloud_step_labels(row: wire.CloudRow, model: QueueModel) -> UnitLabels:
    identity = row.identity
    label = model.cloud_labels.get(identity.session_id, UnitLabels(identity.name, unit_labels.OWNER_NOT_RECORDED))
    return replace(label, unit=gate.queue_unit(identity.unit, identity.session_id, model.cloud_labels)
                   or unit_labels.OWNER_NOT_RECORDED)


def cloud_step_name(row: wire.CloudRow, model: QueueModel) -> str:
    """Name page steps from their recorded owner's labels."""
    label = model.cloud_labels.get(row.identity.session_id)
    if label is None:
        return model.names.shown(row.identity.step)
    unit = label.unit if label.unit not in ("", "—", unit_labels.OWNER_NOT_RECORDED) else ""
    return model.names.for_owner(row.identity.step, unit, label.role)


@dataclass(frozen=True)
class WaitingBuild:
    position: int
    step: admission_records.WaitingStep


def waiting_builds(model: QueueModel) -> list[WaitingBuild | wire.CloudRow]:
    """Insert cloud waits by arrival time without changing the local queue order."""
    rows: list[WaitingBuild | wire.CloudRow] = [WaitingBuild(index, step)
        for index, step in enumerate(model.reading.view.waiting, 1)]
    for cloud in sorted((row for row in cloud_rows(model) if isinstance(row.wait, wire.CloudSlotWait)),
                        key=lambda row: row.since):
        position = next((index for index, row in enumerate(rows)
                         if (row.step.since if isinstance(row, WaitingBuild) else row.since) > cloud.since), len(rows))
        rows.insert(position, cloud)
    return rows


def head_text(model: QueueModel) -> str:
    head = model.reading.explanation.head
    text = ""
    if isinstance(head, admission_records.HeadExplanation) and head.decision.state == "hold":
        text = (f"free {head.free.available / admission_records.GIB:.1f} GiB; "
                + f"reserve {head.free.reserve / admission_records.GIB:.1f} GiB; need {head.free.need / admission_records.GIB:.1f} GiB")
    if isinstance(model.reading, wire.CloudQueueRead):
        used = sum(isinstance(row.wait, admission_records.ReadyForAdmission) for row in cloud_rows(model))
        text += (" · " if text else "") + f"cloud {used} of {model.reading.cloud_slots} slots"
    return "Head: " + text if text else ""


def cloud_lines(model: QueueModel, columns: int, rows: tuple[wire.CloudRow, ...] = ()) -> list[Line]:
    reading = model.reading
    all_rows = cloud_rows(model)
    if not all_rows or not isinstance(reading, wire.CloudQueueRead):
        return []
    used = sum(isinstance(row.wait, admission_records.ReadyForAdmission) for row in all_rows)
    lines: list[Line] = [(Span(f"Cloud · {used} of {reading.cloud_slots} slots", bold=True),)]
    interval = cloud_push_interval()
    now = reading.explanation.now
    for row in rows or all_rows:
        identity = row.identity
        work = cloud_work(row, now, reading.cloud_gate_stale_pushes, interval)
        unit = cloud_step_labels(row, model).unit
        if isinstance(row.wait, wire.CloudSlotWait):
            state = "waiting; " + row.wait.text
        else:
            state = work.state
        size = f"{work.vcpus} vCPUs" if isinstance(work.vcpus, int) else "vCPUs unknown"
        elapsed = leftover_age(age(row.started_at or row.since, now))
        text = f"{field(unit)} · {field(model.names.shown(identity.step))} · {state} · {work.modules} · {size} · elapsed {elapsed}"
        if work.failed_modules:
            noun = "module" if work.failed_modules == 1 else "modules"
            text += f" · {work.failed_modules} failed {noun}"
        if isinstance(work.update, CloudUpdateLate):
            text += f" · no update for {cloud_update_age(work.update.seconds)}"
        lines.extend(wrap((Span("   " + text),), columns, indent=3))
    return lines


def render_view(model: QueueModel, columns: int, *, colour: bool = False, you: str = "",
                selector: str = "") -> list[Line]:
    explanation = model.reading.explanation
    head = explanation.head
    room = explanation.room
    cap_bytes = room.cap if isinstance(room, admission_records.MemoryRoom) and isinstance(room.cap, int) else 0
    cap = cap_bytes / admission_records.GIB
    waiting = model.reading.view.waiting
    running = running_builds(model)
    cloud = cloud_rows(model)
    mac_reading = read_mac_builds()
    mac_status = mac_reading_status(mac_reading, explanation.now)
    native_mac = () if mac_status else mac_reading.builds
    interval = cloud_push_interval() if cloud else CloudPushIntervalUnavailable("no cloud rows")
    stale_pushes = model.reading.cloud_gate_stale_pushes if isinstance(model.reading, wire.CloudQueueRead) else 0
    work = {row.owner: cloud_work(row, explanation.now, stale_pushes, interval) for row in cloud}
    local_cpus = os.cpu_count()
    cpus = str(local_cpus) if local_cpus is not None else "—"
    bumped_units = (() if isinstance(explanation.priority, admission_records.NoQueuePriority)
                    else tuple(entry["unit"] for entry in explanation.priority.entries))

    def names(step: admission_records.WaitingStep | admission_records.RunningStep) -> tuple[str, str, bool]:
        identity = step_labels(step, model.labels)
        show = field(step.session_name if not step.session_id and step.unit else identity.owner_text) or "—"
        unit = field(identity.unit).removeprefix(show + "-")
        unit = unit or "—"
        return show, unit, identity.unit in bumped_units

    waiting_names = [names(step) for step in waiting]
    running_names = [names(row.step) for row in running]
    cloud_names: dict[wire.Owner, tuple[str, str, bool]] = {}
    for row in cloud:
        identity = cloud_step_labels(row, model)
        show = field(identity.owner_text) or "—"
        unit = field(identity.unit).removeprefix(show + "-") or "—"
        cloud_names[row.owner] = show, unit, False
    all_names = [*waiting_names, *running_names, *cloud_names.values(),
                 *((unit_labels.OWNER_NOT_RECORDED, unit_labels.OWNER_NOT_RECORDED, False) for _ in native_mac)]
    bars = [size_bar(step.need / admission_records.GIB, cap) for step in waiting]
    status = head_status(model)
    waiting_status = [
        (Span(" " + reason_phrase(step.reason, waiting), "error"),)
        if isinstance(step.reason, admission_records.ReadingUnavailable) else
        status if isinstance(head, admission_records.HeadExplanation) and same_waiter(head.waiter, step) else ()
        for step in waiting]
    place_width = max(5 if any(isinstance(row.wait, wire.CloudSlotWait) for row in cloud) else 3, len(str(len(waiting))))
    minutes_width = max(3, max((len(str(age(step.since, explanation.now))) for step in waiting), default=0),
                        max((len(str(age(row.started_at, explanation.now))) for row in running if row.started_at), default=0),
                        max((len(str(age(row.started_at or row.since, explanation.now))) for row in cloud), default=0))
    step_width = max(7, min(max(13, columns // 6), max(
        max((terminal_width(queue_lines.step_text(row.step, model.names, labels=model.labels)) for row in running), default=7),
        max((terminal_width(queue_lines.step_text(step, model.names, labels=model.labels)) for step in waiting), default=7),
        max((terminal_width(field(model.names.shown(row.identity.step))) for row in cloud), default=7),
        max((terminal_width(field(Path(build.folder).name)) for build in native_mac), default=7))))
    pids = [queue_evidence.step_pid_text(step) for step in waiting]
    running_pids = [queue_evidence.step_pid_text(row.step) for row in running]
    pid_width = max((len(text) for text in [*pids, *running_pids, *(f"pid {row.owner.pid}" for row in cloud),
                     *(f"pid {build.pid}" for build in native_mac)]), default=5)
    machines = [field(admission_records.execution_host(row.step, store.host_name()))
        + (" ?" if row.step.assignment_host and not isinstance(row.step.execution, placement.ObservedExecution) else "")
        for row in running]
    machine_width = max(len("machine"), *(terminal_width(machine) for machine in machines), 3) if running or cloud or native_mac else 0
    vcpu_width = max(4, len(cpus), max((len(str(item.vcpus)) for item in work.values() if isinstance(item.vcpus, int)), default=0))
    # Reserve the text widths before the pid takes cells from the bars.
    fixed = place_width + step_width + minutes_width + 5
    show_width, unit_width = 12, 7
    # Keep the previous name widths with a fixed memory area of 24 cells.
    # The cap and the pid change the bars, not the name widths.
    name_room = columns - fixed - 24
    if columns > 60:
        longest_show = max((terminal_width(show) for show, _, _ in all_names), default=12)
        longest_unit = max((terminal_width(unit) for _, unit, _ in all_names), default=10)
        while show_width + unit_width < name_room and (show_width < longest_show or unit_width < longest_unit):
            if show_width < longest_show:
                show_width += 1
            if show_width + unit_width < name_room and unit_width < longest_unit:
                unit_width += 1
    while show_width + unit_width > name_room and (show_width > 3 or unit_width > 7):
        if show_width > 3:
            show_width -= 1
        if show_width + unit_width > name_room and unit_width > 7:
            unit_width -= 1
    names_fit = all(terminal_width(show) <= show_width and terminal_width(unit) <= unit_width
                    for show, unit, _ in all_names)
    if not cloud and not native_mac and not any(row.step.assignment_host for row in running) and (not names_fit or show_width + unit_width + machine_width + 1 > name_room):
        machine_width = 0
    machine_cells = machine_width + 1 if machine_width else 0
    bar_column = fixed + show_width + unit_width + pid_width + 1 + machine_cells + vcpu_width + 1
    running_text = [
        (f" {row.kept / admission_records.GIB:.1f} / {row.used / admission_records.GIB:.1f}" if row.measured
         else f" {row.kept / admission_records.GIB:.1f} / —")
        + (f"; {row.swap_bytes / admission_records.GIB:.1f} GiB swapped" if row.swap_bytes else "")
        for row in running]
    cap_label = f"cap {cap:.1f}" if isinstance(room, admission_records.MemoryRoom) and isinstance(room.cap, int) else "cap unavailable"
    memory_bar = cap_bar(room, max(1, columns - bar_column), colour=colour)
    cap_cell = next((index for index, span in enumerate(memory_bar) if span.text == "│"), 0)
    lines: list[Line] = [
        (Span(f"Build queue · {clock(model, explanation.now)}", "inactive"),), (),
        (Span(" " * max(0, min(columns - len(cap_label), bar_column + cap_cell))),
         Span(cap_label, "error")),
        (Span(" " * bar_column), *memory_bar), (),
    ]
    priority = head_facts.priority_text(explanation.priority)
    if priority:
        lines.insert(2, (Span(priority),))

    def heading() -> Line:
        place, memory = "#", "GiB: kept/use; need"
        return (Span(pad_left(place, place_width) + " " + pad_right(cut_tail("showrunner", show_width), show_width) + " "
                     + pad_right(cut_tail("unit", unit_width), unit_width) + " " + pad_right("step", step_width) + " "
                     + pad_left("min", minutes_width) + " "
                     + (pad_right("machine", machine_width) + " " if machine_width else "")
                     + pad_left("vCPU", vcpu_width) + " " + pad_right("pid", pid_width) + " " + memory, "inactive"),)

    @cache
    def column(text: str, width: int) -> str:
        if text.isascii():
            return (text if len(text) <= width else text[:max(0, width - 1)] + "…").ljust(width) + " "
        return pad_right(cut_tail(text, width), width) + " "

    def identity_columns(place: str, step: str, pair: tuple[str, str, bool], minutes: str, marker: str = " ") -> Line:
        show, unit, bumped = pair
        unit_spans = ((Span("●", "warning"), Span(column(unit, unit_width - 1), "suggestion"))
                      if bumped else (Span(column(unit, unit_width), "suggestion"),))
        return (Span(pad_left(place, place_width), "warning" if place == "1" else "inactive"),
                Span(marker, "suggestion"),
                Span(column(show, show_width)),
                *unit_spans,
                Span(column(field(step), step_width)),
                Span(pad_left(minutes, minutes_width) + " ", "inactive"))

    def append_row(row: Line) -> None:
        if terminal_width(plain([row])) <= columns:
            lines.append(row)
            return
        first, *continued = wrap(row, max(1, columns - 2), indent=0)
        lines.append(first)
        lines.extend((Span("· ", "inactive"), *line) for line in continued)

    lines.extend(wrap((Span(queue_evidence.room_text(explanation.room), "inactive"),), columns))
    lines.append(heading())

    def append_cloud_row(row: wire.CloudRow) -> None:
        information = work[row.owner]
        running_cloud = isinstance(row.wait, admission_records.ReadyForAdmission)
        cpu_text = str(information.vcpus) if running_cloud and isinstance(information.vcpus, int) else "—" if running_cloud else ""
        status = cloud_status(row, information)
        if running_cloud and isinstance(information.update, CloudUpdateLate):
            status = (*status, Span(f" · no update for {cloud_update_age(information.update.seconds)}", "error"))
        append_row((*identity_columns("run" if running_cloud else "cloud", model.names.shown(row.identity.step),
                                     cloud_names[row.owner], str(age(row.started_at or row.since, explanation.now)),
                                     "▸" if (you and row.identity.session_id == you
                                             or selector and cloud_matches(row, selector, model.cloud_labels)) else " "),
                    Span(column("aws" if running_cloud else "", machine_width), "inactive"),
                    Span(pad_left(cpu_text, vcpu_width) + " ", "inactive"),
                    Span(column(f"pid {row.owner.pid}", pid_width), "inactive"), *status))

    running_rows: list[RunningBuild | wire.CloudRow] = [*running,
        *(row for row in cloud if isinstance(row.wait, admission_records.ReadyForAdmission))]
    if cloud:
        running_rows.sort(key=lambda row: row.started_at if isinstance(row, RunningBuild) else row.started_at or row.since)
    running_positions = {id(row): index for index, row in enumerate(running)}
    for row in running_rows:
        if isinstance(row, wire.CloudRow):
            append_cloud_row(row)
            continue
        index = running_positions[id(row)]
        pair, text = running_names[index], running_text[index]
        bar_width = max(1, columns - bar_column - terminal_width(text))
        used = row.used / admission_records.GIB
        held_end = used + row.kept / admission_records.GIB
        bar = running_bar(min(used, bar_width), min(held_end, bar_width), cap)
        minutes = str(age(row.started_at, explanation.now)) if row.started_at else "—"
        append_row((*identity_columns("run", queue_lines.step_text(row.step, model.names, labels=model.labels), pair, minutes,
                                        "▸" if (you and row.step.session_id == you
                                                or selector and running_matches_selector(row.step, selector, model.labels)) else " "),
                      *((Span(column(machines[index], machine_width), "inactive"),) if machine_width else ()),
                      Span(pad_left("—" if row.step.assignment_host else cpus, vcpu_width) + " ", "inactive"),
                      Span(column(running_pids[index], pid_width), "inactive"),
                      *bar, Span(text)))
    for build in native_mac:
        folder = field(build.folder)
        append_row((*identity_columns("run", Path(folder).name,
                                      (unit_labels.OWNER_NOT_RECORDED, unit_labels.OWNER_NOT_RECORDED, False),
                                      str(age(build.started_at, explanation.now))),
                    Span(column("mac", machine_width), "inactive"),
                    Span(pad_left("—", vcpu_width) + " ", "inactive"),
                    Span(column(f"pid {build.pid}", pid_width), "inactive"),
                    Span(folder + " · " + mac_build_status(mac_reading, explanation.now))))
    if mac_status:
        append_row((Span(mac_status, "error"),))
    for entry in waiting_builds(model):
        if isinstance(entry, wire.CloudRow):
            append_cloud_row(entry)
            continue
        index, step = entry.position - 1, entry.step
        pair, bar, suffix = waiting_names[index], bars[index], waiting_status[index]
        amount = f" {step.need / admission_records.GIB:.1f}"
        bar_width = max(0, columns - bar_column - terminal_width(amount) - terminal_width(plain([suffix])))
        row = (*identity_columns(str(index + 1), queue_lines.step_text(step, model.names, labels=model.labels), pair, str(age(step.since, explanation.now)),
                                "▸" if you and step.session_id == you else " "),
               *((Span(" " * machine_cells),) if machine_width else ()),
               Span(" " * (vcpu_width + 1)),
               Span(column(pids[index], pid_width), "inactive"), Span(bar[:bar_width], "warning"), Span(amount, "warning" if isinstance(head, admission_records.HeadExplanation) and same_waiter(head.waiter, step) else "inactive"))
        append_row((*row, *suffix))
    if isinstance(head, admission_records.TokenWaitersOnly):
        lines.extend([(), (Span("All waiting steps are waiting for the cargo token."),)])
    summary = head_text(model)
    if summary:
        lines.append((Span(summary),))
    if isinstance(head, admission_records.HeadExplanation) and isinstance(head.cause, (admission_records.PressureUnavailable, admission_records.ReadingUnavailable)):
        append_row((Span("Head: " + head.cause.reason, "error"),))
    services = [step.measured for step in model.reading.view.running
                if isinstance(step.measured, admission_records.CompilerServiceMemory)
                or isinstance(step.measured, admission_records.UnownedSliceMemory)
                and not isinstance(step.measured.job_state, NotServiceSlice)]
    if services:
        lines.extend([(), (Span("Services", bold=True),)])
        for memory in services:
            append_row((Span("   " + queue_evidence.service_text(memory, explanation.now)),))
    ended = leftover_processes(model.reading.view.running)
    if ended:
        count = sum(activity.count for _, _, activity in ended)
        total = sum(memory.bytes for _, memory, _ in ended)
        noun = "process" if count == 1 else "processes"
        lines.extend([(), (Span(f"Still running after their build ended ({count} {noun}, {total / admission_records.GIB:.1f} GiB)",
                               bold=True),),
                      (Span("These processes outlive their builds; ask the named owner to stop them if no longer needed."),)])
        for step, memory, activity in ended:
            identity = model.labels.get(activity.session_id)
            owner = field(identity.session if identity else "") or field(activity.session_name) or "unlabelled session"
            elapsed = leftover_age(age(activity.started_at, explanation.now)) if activity.started_at else "—"
            lines.append((Span(f"   {queue_evidence.step_pid_text(step)} · {owner} · {field(activity.program)} · {elapsed} · {memory.bytes / admission_records.GIB:.1f} GiB"
                              + (f"; {memory.swap_bytes / admission_records.GIB:.1f} GiB swapped" if memory.swap_bytes else "")),))
            for member in activity.members[1:]:
                lines.append((Span(f"   pid {member.pid} · {field(member.program)} · {owner}"),))
    return [clip(line, columns) for line in lines]
