#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Execute trusted source in a cached Linux development shell."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, cast
from types import FrameType
import uuid

if TYPE_CHECKING:
    from scripts.copies import copies
    from scripts.lib import build_folders
    from scripts.lib.boot_identity import boot_identity as boot_identity
    from scripts.lib.cargo_output import CargoOutputUnavailable, read_cargo_output_roots
    from scripts.aws_target import ledger
    from scripts.build_targets.target_config import SshTarget, is_aws_target
else:
    from scripts.copies import copies
    from scripts.lib import build_folders
    from scripts.lib.boot_identity import boot_identity as boot_identity
    from scripts.lib.cargo_output import CargoOutputUnavailable, read_cargo_output_roots
    from scripts.build_targets.target_config import SshTarget, is_aws_target



PROC_ROOT = Path('/proc')


class SlotBoot(Enum):
    CURRENT = 'current'
    PREVIOUS = 'previous'
    LEGACY = 'legacy'


def slot_boot(lease: SlotRun, current_boot: str, *, require_boot_identity: bool = False) -> SlotBoot:
    """Keep unconfirmed AWS ownership separate from a confirmed earlier boot."""
    if 'boot_id' not in lease:
        if require_boot_identity:
            raise SlotProcessesUnresolved('retained marker has no boot identity')
        return SlotBoot.LEGACY
    identity = lease['boot_id']
    if not identity:
        raise ValueError('invalid slot boot identity')
    return SlotBoot.CURRENT if identity == current_boot else SlotBoot.PREVIOUS


@dataclass(frozen=True)
class ShellEnvironment:
    """Exports published after a successful shell evaluation."""

    key: str
    exports: Path
    inputs: Path


def shell_inputs(root: Path) -> tuple[Path, ...]:
    """List the shell inputs without source files or build output."""
    paths = [root / 'flake.nix', root / 'flake.lock']
    nix = root / 'nix'
    if nix.is_symlink():
        raise ValueError('unsafe shell input directory: nix')
    if nix.exists():
        children = tuple(nix.rglob('*'))
        if any(path.is_symlink() for path in children):
            raise ValueError('shell inputs must not contain symbolic links')
        paths.extend(path for path in children if not path.is_dir())
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'unsafe or missing shell input: {path}')
        for parent in path.relative_to(root).parents:
            if (root / parent).is_symlink():
                raise ValueError(f'unsafe shell input directory: {parent}')
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def shell_key(root: Path, selector: str) -> str:
    digest = hashlib.sha256()
    digest.update(selector.encode() + b'\0')
    for path in shell_inputs(root):
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big') + name)
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


def exports_are_valid(exports: Path) -> bool:
    """Reevaluate exports when a referenced store path no longer exists."""
    text = exports.read_text()
    roots: list[str] = re.findall(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+', text)
    return bool(text.strip()) and all(Path(root).exists() for root in roots)


def prepare_shell(root: Path, cache: Path, selector: str, timeout_s: float) -> ShellEnvironment:
    """Publish immutable shell inputs and exports under a content key."""
    key = shell_key(root, selector)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / key
    with (cache / f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exports = destination / 'exports.sh'
        if exports.is_file() and exports_are_valid(exports):
            return ShellEnvironment(key, exports, destination / 'inputs')
        if not destination.exists():
            with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
                    build_folders.FOLDER_PREFIXES["linux-cache-stage"].format(owner=key), build_folders.ExplicitBuildParent(cache))) as stage:
                inputs = stage / 'inputs'
                inputs.mkdir()
                for path in shell_inputs(root):
                    target = inputs / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _ = shutil.copyfile(path, target)
                if shell_key(inputs, selector) != key:
                    raise ValueError('shell inputs changed during the copy')
                os.replace(stage, destination)
        inputs = destination / 'inputs'
        if shell_key(inputs, selector) != key:
            raise ValueError('cached shell inputs do not match their key')
        result = subprocess.run(
            ['nix', 'print-dev-env', f'path:{inputs}#{selector}'],
            capture_output=True, text=True, check=True, timeout=timeout_s,
        )
        if not result.stdout.strip():
            raise ValueError('nix returned no shell exports')
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination, delete=False) as output:
            temporary = Path(output.name)
            try:
                _ = output.write(result.stdout)
                output.flush()
                os.replace(temporary, exports)
            finally:
                temporary.unlink(missing_ok=True)
    return ShellEnvironment(key, exports, destination / 'inputs')


def positive_setting(values: dict[str, str], key: str) -> float:
    value = float(values[key])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{key}: expected a positive number')
    return value


def shell_environment_script(shell: ShellEnvironment) -> str:
    """Keep the caller's tools after the development shell's tools."""
    return ('linux_runner_path=$PATH\n'
            + f'source {shlex.quote(str(shell.exports))} || exit 75\n'
            + 'export PATH="$PATH:$linux_runner_path"\n'
            + 'unset linux_runner_path\n'
            + 'if [[ -n ${shellHook-} ]]; then eval "$shellHook" || exit 75; fi\n')


def policy_setting(values: dict[str, str], key: str, repository: str) -> str:
    return values[f'{key}.{repository}'] if f'{key}.{repository}' in values else values[key]


def normalize_tool_version(value: str) -> str:
    """Keep each nonempty version line in one common form."""
    return ' '.join(line.strip() for line in value.splitlines() if line.strip())


def tool_version_filter() -> str:
    """Use the same normalizer for versions read by Bash."""
    code = "\n".join((
        "import sys",
        "from pathlib import Path",
        "sys.path.insert(0, str(Path.home() / '.claude'))",
        "from scripts.mac_test.linux_runner import normalize_tool_version",
        "print(normalize_tool_version(sys.stdin.read()))",
    ))
    return f'python3 -c {shlex.quote(code)}'


def probe_script(mirror: str, *, shell_environment: bool = False) -> str:
    """Read Linux disk capacity and the public tool versions."""
    return '\n'.join((
        ':' if shell_environment else 'export PATH="$HOME/.cargo/bin:$PATH"',
        copies.mirror_probe_setup(mirror),
        f'checkout="$HOME"/{shlex.quote(mirror)}',
        'disk_usage=$(df -Pk "$checkout") || exit 1',
        'free_gib=$(printf \'%s\\n\' "$disk_usage" | awk \'NR > 1 {if (!seen || $4 < free) free=$4; seen=1} END {printf "%.6f", free / 1048576}\') || exit 1',
        'printf \'free_gib=%s\\nhost=%s\\n\' "$free_gib" "$(hostname -s)"',
        'nextest_version=$(cargo-nextest nextest --version) || exit 1',
        'mend_version=$(cargo-mend mend --version) || exit 1',
        f'nextest_version=$(printf \'%s\' "$nextest_version" | {tool_version_filter()}) || exit 1',
        f'mend_version=$(printf \'%s\' "$mend_version" | {tool_version_filter()}) || exit 1',
        'scripts_commit=$(cat "$HOME/.claude/.build-target-script-commit") || exit 1',
        'printf \'nextest=%s\\nmend=%s\\nscripts_commit=%s\\nrc=0\\n\' '
        + '"$nextest_version" "$mend_version" "$scripts_commit"',
    ))


class SlotRun(TypedDict):
    """A process group that owns one slot until every process ends."""

    pid: int
    start_time: int
    process_group: int
    token: str
    boot_id: NotRequired[str]


@dataclass(frozen=True)
class UnmarkedSlot:
    """The slot has no process marker."""


def read_slot_run(marker: Path) -> SlotRun | UnmarkedSlot:
    if not marker.exists():
        return UnmarkedSlot()
    raw = cast(object, json.loads(marker.read_text()))
    if not isinstance(raw, dict):
        raise ValueError('invalid slot process marker')
    fields = cast(dict[str, object], raw)
    for key in ('pid', 'start_time', 'process_group'):
        if type(fields.get(key)) is not int or cast(int, fields[key]) < 1:
            raise ValueError('invalid slot process identity')
    if not isinstance(fields.get('token'), str) or not fields['token']:
        raise ValueError('invalid slot process token')
    lease: SlotRun = {'pid': cast(int, fields['pid']), 'start_time': cast(int, fields['start_time']),
                      'process_group': cast(int, fields['process_group']), 'token': cast(str, fields['token'])}
    if 'boot_id' in fields:
        identity = fields['boot_id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('invalid slot boot identity')
        lease['boot_id'] = identity
    return lease


def process_fields(pid: int, proc_root: Path = PROC_ROOT) -> list[str]:
    """Read stat fields after the process name, which can contain spaces."""
    return (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()


@dataclass(frozen=True)
class SlotProcessSelection:
    """Confirmed slot processes and evidence that prevents release."""

    pids: tuple[int, ...]
    unresolved: bool


class SlotProcessesUnresolved(RuntimeError):
    """Process evidence cannot confirm that the slot has ended."""


class SlotStartupRefused(RuntimeError):
    """A slot has live processes or unresolved process ownership."""


SLOT_PROCESSES_UNRESOLVED = 3
STARTUP_REFUSAL_RECORD = 'linux_startup=unresolved'


@dataclass(frozen=True)
class ProcessIdentity:
    """Public process fields that remain readable for non-dumpable processes."""

    real_uid: int
    parent: int
    group: int
    start_time: int
    state: str


def process_identity(pid: int, proc_root: Path) -> ProcessIdentity:
    fields = process_fields(pid, proc_root)
    status = (proc_root / str(pid) / 'status').read_text()
    uid = re.search(r'^Uid:\s+(\d+)\b', status, re.MULTILINE)
    if uid is None:
        raise ValueError('process real UID is unavailable')
    return ProcessIdentity(int(uid[1]), int(fields[1]), int(fields[2]), int(fields[19]), fields[0])


def select_slot_processes(mirror: Path, proc_root: Path = PROC_ROOT, *,
                          require_boot_identity: bool = False) -> SlotProcessSelection:
    """Read each ownership source independently before deciding uncertainty."""
    lease = read_slot_run(mirror / '.linux-run.json')
    if not isinstance(lease, UnmarkedSlot):
        current_boot = boot_identity(proc_root) if 'boot_id' in lease or require_boot_identity else ''
        if slot_boot(lease, current_boot, require_boot_identity=require_boot_identity) is SlotBoot.PREVIOUS:
            return SlotProcessSelection((), False)
    selected: set[int] = set()
    parents: dict[int, int] = {}
    uncertain: set[int] = set()
    root = mirror.resolve()
    own_pid = os.getpid()
    own_uid = os.getuid()
    identities: dict[int, ProcessIdentity] = {}
    for process in proc_root.glob('[0-9]*'):
        pid = int(process.name)
        try:
            identities[pid] = process_identity(pid, proc_root)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if not isinstance(lease, UnmarkedSlot):
                uncertain.add(pid)
    # The checking process and its ancestors cannot be leftovers of a lost run.
    checking_chain: set[int] = set()
    ancestor = own_pid
    while ancestor in identities and ancestor not in checking_chain:
        checking_chain.add(ancestor)
        ancestor = identities[ancestor].parent
    for pid, identity in identities.items():
        if identity.real_uid != own_uid or identity.state in ('Z', 'X'):
            continue
        if not isinstance(lease, UnmarkedSlot):
            if pid == lease['pid']:
                if (identity.start_time != lease['start_time']
                        or identity.group != lease['process_group']):
                    raise ValueError('slot process identity changed')
                selected.add(pid)
                parents[pid] = identity.parent
                continue
            if identity.start_time < lease['start_time']:
                continue
        if pid in checking_chain:
            continue
        # Slot children keep their user; only an orphan can acquire a root parent (PID 1).
        parent = identities.get(identity.parent)
        if identity.parent != 1 and parent is not None and parent.real_uid != own_uid:
            continue
        parents[pid] = identity.parent
        process = proc_root / str(pid)
        denied = False
        try:
            command = (process / 'cmdline').read_bytes().split(b'\0')
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            command = []
            denied = True
        runner = any(word.endswith(b'/linux_runner.py') for word in command)
        if runner and b'--mirror' in command:
            index = command.index(b'--mirror') + 1
            if index < len(command):
                path = Path(os.fsdecode(command[index]))
                invocation_root = path if path.is_absolute() else Path.home() / path
                if invocation_root.resolve() == root:
                    selected.add(pid)
        if not isinstance(lease, UnmarkedSlot):
            try:
                environment = (process / 'environ').read_bytes().split(b'\0')
                token = f"BUILD_TARGET_RUN_TOKEN={lease['token']}".encode()
                if token in environment:
                    selected.add(pid)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                denied = True
        try:
            cwd = (process / 'cwd').resolve(strict=True)
            if cwd == root or root in cwd.parents:
                selected.add(pid)
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            denied = True
        if denied and pid not in selected and not isinstance(lease, UnmarkedSlot):
            # Confirm that missing evidence does not belong to an exited process.
            try:
                if process_fields(pid, proc_root)[0] in ('Z', 'X'):
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                pass
            uncertain.add(pid)
    # A parent chain proves ownership even when a child hides its environment.
    changed = True
    while changed:
        children = {pid for pid, parent in parents.items() if parent in selected}
        changed = not children.issubset(selected)
        selected.update(children)
    uncertain.difference_update(selected)
    selected.discard(own_pid)
    return SlotProcessSelection(tuple(sorted(selected)), bool(uncertain))


def slot_processes(mirror: Path, proc_root: Path = PROC_ROOT, *,
                   require_boot_identity: bool = False) -> tuple[int, ...]:
    """Return only confirmed processes, or refuse uncertain cleanup."""
    selection = select_slot_processes(mirror, proc_root, require_boot_identity=require_boot_identity)
    if selection.unresolved:
        raise SlotProcessesUnresolved('slot process ownership remains unresolved')
    if not selection.pids:
        (mirror / '.linux-run.json').unlink(missing_ok=True)
    return selection.pids


def print_slot_processes(mirror: Path, *, require_boot_identity: bool = False) -> int:
    """Refuse an uncertain external selection without a traceback or kill list."""
    try:
        pids = slot_processes(mirror, require_boot_identity=require_boot_identity)
    except SlotProcessesUnresolved:
        return SLOT_PROCESSES_UNRESOLVED
    except (OSError, ValueError):
        return 1
    if pids:
        print(*pids, sep='\n')
    return 0


@contextmanager
def slot_run(mirror: Path, *, require_boot_identity: bool = False) -> Generator[None, None, None]:
    """Publish ownership before shell evaluation can start a child process."""
    try:
        if ((mirror / '.linux-run.json').exists()
                and slot_processes(mirror, require_boot_identity=require_boot_identity)):
            raise SlotStartupRefused('the slot still has running processes')
    except (OSError, ValueError, SlotProcessesUnresolved) as error:
        raise SlotStartupRefused('an earlier run still has unresolved ownership') from error
    pid = os.getpid()
    token = uuid.uuid4().hex
    lease: SlotRun = {'pid': pid, 'start_time': int(process_fields(pid)[19]),
                      'process_group': os.getpgrp(), 'token': token, 'boot_id': boot_identity()}
    marker = mirror / '.linux-run.json'
    with marker.open('x') as output:
        _ = output.write(json.dumps(lease))
    previous = os.environ.get('BUILD_TARGET_RUN_TOKEN', '')
    os.environ['BUILD_TARGET_RUN_TOKEN'] = token
    try:
        yield
    finally:
        if previous:
            os.environ['BUILD_TARGET_RUN_TOKEN'] = previous
        else:
            del os.environ['BUILD_TARGET_RUN_TOKEN']
        # Keep the marker when a detached child still owns the slot.
        try:
            children = slot_processes(mirror, require_boot_identity=require_boot_identity)
        except (OSError, ValueError, SlotProcessesUnresolved):
            print('mac_cleanup=unconfirmed', flush=True)
        else:
            if children:
                print('mac_cleanup=unconfirmed', flush=True)
            else:
                marker.unlink(missing_ok=True)


def mirror_selector_script(mirror: str, *, require_boot_identity: bool = False) -> str:
    """Select the runner and its children even outside the slot directory."""
    code = ("import sys; from pathlib import Path; "
            + "from scripts.mac_test.linux_runner import print_slot_processes; "
            + f"sys.exit(print_slot_processes(Path(sys.argv[1]), require_boot_identity={require_boot_identity!r}))")
    return ('select_leftovers() {\n'
            + f'    "$HOME/.claude/scripts/lib/py" -c {shlex.quote(code)} "$HOME"/{shlex.quote(mirror)}\n'
            + '}\n')


def nextest_words_with_skip(
    words: tuple[str, ...], skip_expression: str, *, insert_filter: bool = False
) -> tuple[str, ...]:
    if not skip_expression:
        return words
    updated = list(words)
    boundary = updated.index("--") if "--" in updated else len(updated)
    for index, word in enumerate(updated[:boundary]):
        if not insert_filter and word != '-E':
            continue
        if word in ("-E", "--filter-expr", "--filterset"):
            if index + 1 >= boundary:
                return words
            updated[index + 1] = f"({updated[index + 1]}) & not ({skip_expression})"
            return tuple(updated)
        for prefix in ("--filter-expr=", "--filterset=", "-E=", "-E"):
            if word.startswith(prefix) and word != prefix:
                updated[index] = f"{prefix}({word[len(prefix):]}) & not ({skip_expression})"
                return tuple(updated)
    if insert_filter:
        updated[boundary:boundary] = ["-E", f"not ({skip_expression})"]
    return tuple(updated)


def command_script(mirror: str, values: dict[str, str], repository: str, verb: str,
                   words: tuple[str, ...], call_id: str, tree_key: str, repo_identity: str,
                   test_environment: tuple[tuple[str, str], ...] = (),
                   clear_environment: str = '') -> str:
    arguments = ['.claude/scripts/lib/py', '.claude/scripts/mac_test/linux_runner.py', 'run',
                 '--mirror', mirror, '--values', json.dumps(values), '--repository', repository,
                 '--verb', verb, '--call-id', call_id, '--tree-key', tree_key]
    if repo_identity:
        arguments.extend(['--repo-identity', repo_identity])
    for name, value in test_environment:
        arguments.extend(['--env', name + '=' + value])
    if clear_environment:
        arguments.extend(['--clear-environment', clear_environment])
    arguments.extend(['--', *words])
    return shlex.join(arguments)


def probe_command_script(mirror: str, values: dict[str, str], repository: str) -> str:
    return shlex.join(['.claude/scripts/lib/py', '.claude/scripts/mac_test/linux_runner.py', 'probe',
                       '--mirror', mirror, '--values', json.dumps(values), '--repository', repository])


class AwsExecutionEvidence(Protocol):
    """Read generation-bound AWS evidence only for an AWS execution."""

    def current_generation(self, directory: Path, boot_id: str) -> str: ...
    def interruption_generation(self, directory: Path, *, target: str, generation: str) -> str: ...
    def execution_permission(self, directory: Path, target: str, *, now: int,
                             boot_id: str) -> ledger.AdmissionPermission: ...


def aws_execution_evidence() -> AwsExecutionEvidence:
    return cast(AwsExecutionEvidence, cast(object, import_module('scripts.aws_target.remote')))


@dataclass(frozen=True)
class AwsExecutionContext:
    target: str
    directory: Path


@dataclass(frozen=True)
class IndependentLinuxExecution:
    """This Linux target does not use AWS spending permission."""


def aws_execution_context(values: dict[str, str]) -> AwsExecutionContext | IndependentLinuxExecution:
    target = SshTarget(values.get('selected_target', ''), 'linux', Path(values.get('selected_settings', '')))
    if not is_aws_target(target):
        return IndependentLinuxExecution()
    return AwsExecutionContext(target.name, Path.home() / values['remote_state_directory'])


def interruption_generation(values: dict[str, str]) -> str:
    try:
        context = aws_execution_context(values)
        if isinstance(context, IndependentLinuxExecution):
            return ''
        remote = aws_execution_evidence()
        generation = remote.current_generation(context.directory, boot_identity())
        return remote.interruption_generation(context.directory, target=context.target, generation=generation)
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def permission_refused(values: dict[str, str]) -> bool:
    try:
        context = aws_execution_context(values)
        if isinstance(context, IndependentLinuxExecution):
            return False
        from scripts.aws_target import ledger
        remote = aws_execution_evidence()
        permission = remote.execution_permission(context.directory, context.target, now=int(time.time()),
                                                 boot_id=boot_identity())
        if isinstance(permission, ledger.PermissionAllowed):
            return False
        reason = permission.reason
    except (OSError, ValueError, KeyError) as error:
        reason = str(error)
    print(f'aws_permission=refused reason={reason}', flush=True)
    return True


@contextmanager
def interruption_signals(values: dict[str, str]) -> Generator[None, None, None]:
    if isinstance(aws_execution_context(values), IndependentLinuxExecution):
        yield
        return

    def interrupted(signum: int, _frame: FrameType | None) -> None:
        if not interruption_generation(values):
            raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        _ = signal.signal(signal.SIGTERM, previous)
        if generation := interruption_generation(values):
            print(f'aws_interrupted={generation}', flush=True)


@contextmanager
def interruptible_slot(mirror: Path, values: dict[str, str]) -> Generator[None, None, None]:
    with interruption_signals(values), slot_run(
        mirror, require_boot_identity=isinstance(aws_execution_context(values), AwsExecutionContext)
    ):
        yield


def probe_shell(mirror: Path, values: dict[str, str], repository: str) -> int:
    """Read parity inside the same cached environment as the work."""
    if permission_refused(values):
        return 75
    try:
        with interruptible_slot(mirror, values):
            shell = prepare_shell(mirror, Path.home() / values['shell_cache'],
                                  policy_setting(values, 'devshell', repository),
                                  positive_setting(values, 'shell_timeout_s'))
            if permission_refused(values):
                return 75
            command = (shell_environment_script(shell)
                       + f'cd -- {shlex.quote(str(mirror))} || exit 75\n'
                       + probe_script(str(mirror.relative_to(Path.home())), shell_environment=True))
            return subprocess.run(['bash', '-c', command], cwd=mirror, check=False,
                                   pass_fds=copies.inherited_mirror_descriptors()).returncode
    except SlotStartupRefused:
        print(STARTUP_REFUSAL_RECORD, flush=True)
        return 75


def run(mirror: Path, values: dict[str, str], repository: str, verb: str,
        words: tuple[str, ...], call_id: str, tree_key: str, repo_identity: str = '',
        test_environment: tuple[tuple[str, str], ...] = (),
        clear_environment: str = '') -> int:
    if permission_refused(values):
        return 75
    try:
        with interruptible_slot(mirror, values):
            return execute_slot(mirror, values, repository, verb, words, call_id, tree_key, repo_identity, test_environment, clear_environment)
    except SlotStartupRefused:
        print(STARTUP_REFUSAL_RECORD, flush=True)
        return 75


def output_filesystem_paths(checkout: Path, environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Resolve the checkout and its Cargo output filesystems."""
    outputs = read_cargo_output_roots(checkout, environment=environment)
    if isinstance(outputs, CargoOutputUnavailable):
        if not (checkout / "Cargo.toml").exists():
            return (checkout,)
        raise OSError(outputs.reason)
    return (checkout, outputs.target_directory, outputs.build_directory)


def output_free_bytes(checkout: Path, environment: Mapping[str, str] | None = None) -> int:
    readings: list[int] = []
    for path in output_filesystem_paths(checkout, environment):
        while True:
            try:
                readings.append(shutil.disk_usage(path)[2])
                break
            except FileNotFoundError:
                if path == path.parent:
                    raise
                path = path.parent
    return min(readings)


def trim_build_folder(target: Path, environment: dict[str, str]) -> int:
    """Use the existing budget sweep without the machine-wide disk sweep."""
    code = '\n'.join((
        'import sys',
        'from pathlib import Path',
        "sys.path.insert(0, str(Path.home() / '.claude/scripts/lint'))",
        'import sweep',
        'import conf',
        'config = conf.read(conf.path("lint"))',
        'recent_seconds = int(config.number(sweep.RECENT_KEY) * 60)',
        'import os',
        'target = Path(sys.argv[1])',
        'checkout = Path(os.environ["BUILDLOG_WORKSPACE"])',
        'os.chdir(checkout)',
        'selected = None if (checkout / "Cargo.toml").is_file() else str(target)',
        'sys.exit(sweep.sweep_workspace(selected, config, False, recent_seconds / 60))',
    ))
    return subprocess.run([sys.executable, '-c', code, str(target)],
                          env={**os.environ, **environment}, check=False).returncode


def version_mismatch(target: str, actual: Mapping[str, str], expected: Mapping[str, str], pin_file: str) -> str:
    """Name each tool mismatch and the configured file that controls its version."""
    differences = [f"{name} box={actual.get(name, 'unavailable')} natedev={expected[name]}"
                   for name in ('rustc', 'nextest', 'mend')
                   if expected.get(name) and actual.get(name) != expected[name]]
    if not differences:
        return ''
    hint = f'; move the box pins in {pin_file}' if pin_file else ''
    return f'{target} version mismatch: ' + '; '.join(differences) + hint


def toolchain_install_script(expected_rustc: str, target: str = 'linux') -> str:
    """Install the tree's toolchain under one lock for its channel."""
    version = re.fullmatch(r'rustc (\d+\.\d+\.\d+)(?: .*)?', expected_rustc)
    fallback = (f'export RUSTUP_TOOLCHAIN={shlex.quote(version[1])}' if version else ':')
    name_code = '\n'.join((
        'import hashlib, os, tomllib',
        'from pathlib import Path',
        'path = next((p for p in (Path("rust-toolchain"), Path("rust-toolchain.toml")) if p.is_file()), None)',
        'if os.environ.get("RUSTUP_TOOLCHAIN"):',
        '    print(os.environ["RUSTUP_TOOLCHAIN"])',
        'elif path is None:',
        '    print("")',
        'else:',
        '    text = path.read_text()',
        '    try:',
        '        toolchain = tomllib.loads(text)["toolchain"].get("channel") or hashlib.sha256(text.encode()).hexdigest()',
        '    except tomllib.TOMLDecodeError:',
        '        toolchain = text.strip()',
        '    print(toolchain)',
    ))
    return '\n'.join((
        'if [[ -z ${RUSTUP_TOOLCHAIN-} && ! -f rust-toolchain && ! -f rust-toolchain.toml ]]; then',
        f'  {fallback}',
        'fi',
        f'toolchain=$(python3 -c {shlex.quote(name_code)}) || exit 75',
        'if [[ -n "$toolchain" ]]; then',
        '  mkdir -p -- "${RUSTUP_HOME:-$HOME/.rustup}" || exit 75',
        '  (',
        '    flock -x 9 || exit 75',
        '    if install_output=$(rustup toolchain install --no-self-update --no-update 2>&1); then',
        '      if [[ -n "$install_output" ]]; then printf \'%s\\n\' "$install_output"; fi',
        '    else',
        '      install_output=${install_output//$\'\\n\'/ }',
        f'      printf \'%s version mismatch: toolchain install failed: %s\\n\' {shlex.quote(target)} "$install_output" >&2',
        '      exit 75',
        '    fi',
        '  ) 9>"${RUSTUP_HOME:-$HOME/.rustup}/$toolchain.lock" || exit 75',
        'fi',
    ))


def execute_slot(mirror: Path, values: dict[str, str], repository: str, verb: str,
        words: tuple[str, ...], call_id: str, tree_key: str, repo_identity: str = '',
        test_environment: tuple[tuple[str, str], ...] = (),
        clear_environment: str = '') -> int:
    if verb not in ('nextest', 'check', 'doc', 'clippy', 'mend-check', 'build'):
        raise ValueError('unsupported remote verb')
    if verb == 'nextest':
        words = nextest_words_with_skip(words, values.get(f'skip.{repository}', ''), insert_filter=True)
    kind = values.get('step_kind') or ('test' if verb == 'nextest' else 'lint' if verb in ('clippy', 'mend-check') else verb)
    if kind not in {word.strip() for word in values['kinds'].split(',')}:
        raise ValueError(f'{kind}: target does not permit this command')
    selector = policy_setting(values, 'devshell', repository)
    shell = prepare_shell(mirror, Path.home() / values['shell_cache'], selector,
                          positive_setting(values, 'shell_timeout_s'))
    if permission_refused(values):
        return 75
    build_budget = positive_setting(values, 'build_budget_gib')
    budget = float(policy_setting(values, 'target_budget_gib', repository))
    if not math.isfinite(budget) or not 0 < budget <= build_budget:
        raise ValueError('repository budget exceeds the build budget')
    floor = positive_setting(values, 'free_floor_gib')
    target = mirror / 'target'
    boundary = words.index('--') if '--' in words else len(words)
    for index, word in enumerate(words[:boundary]):
        if word == '--target-dir' and index + 1 < boundary:
            target = mirror / words[index + 1]
        elif word.startswith('--target-dir='):
            target = mirror / word.partition('=')[2]
    target = target.resolve()
    if target == mirror.resolve() or mirror.resolve() not in target.parents:
        raise ValueError('the Linux build folder must stay inside its slot')
    # Finish the recorder write before the completion marker lets natedev pull the records.
    environment = {
        'LINT_SWEEP_BUDGET_GIB': f'{budget:g}',
        'BUILDLOG_CALLER': 'verify-linux', 'BUILDLOG_CALL_ID': call_id,
        'BUILDLOG_TARGET': values.get('selected_target', ''),
        'BUILDLOG_PINNED_TARGET': values.get('pinned_target', ''),
        'BUILDLOG_SYNC': '1', 'LINT_SKIP_SWEEP': '1',
        'CARGO_TARGET_DIR': str(target),
        'BUILDLOG_WORKSPACE': str(mirror),
    }
    if verb == 'nextest':
        environment.update(test_environment)
    if tree_key:
        environment['BUILDLOG_SENT_TREE_KEY'] = tree_key
    if repo_identity:
        environment['BUILDLOG_REPO_IDENTITY'] = repo_identity
    if verb == 'doc':
        environment['LINT_CONFIG_FORCE'] = '1'
    assignments = ' '.join(f'{key}={shlex.quote(value)}' for key, value in environment.items())
    parity: list[str] = ["mismatches=()"]
    for name, invocation in (('rustc', 'rustc -V'), ('nextest', 'cargo nextest --version'),
                             ('mend', 'cargo mend --version')):
        expected = normalize_tool_version(values.get(f'parity.{name}', ''))
        if expected:
            parity.extend([
                f'actual=$({invocation} 2>/dev/null) || actual=unavailable',
                f'actual=$(printf \'%s\' "$actual" | {tool_version_filter()}) || exit 75',
                f'if [ "$actual" != {shlex.quote(expected)} ]; then',
                f'  mismatches+=("{name} box=$actual natedev="{shlex.quote(expected)})',
                'fi',
            ])
    target_name = values.get('selected_target', '')
    hint = values.get('version_pin_file', '')
    advice = '; move the box pins in ' + hint if hint else ''
    parity.extend([
        'if (( ${#mismatches[@]} )); then',
        f'  printf \'%s version mismatch: %s\' {shlex.quote(target_name)} "${{mismatches[0]}}" >&2',
        '  for mismatch in "${mismatches[@]:1}"; do printf \'; %s\' "$mismatch" >&2; done',
        f'  printf \'%s\\n\' {shlex.quote(advice)} >&2',
        '  exit 75',
        'fi',
    ])
    print(f'linux_runner: mirror={mirror} shell_key={shell.key}', flush=True)
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
            build_folders.FOLDER_PREFIXES["linux-step"], build_folders.ExplicitBuildParent(mirror))) as step_directory:
        started = step_directory / 'started'
        toolchain_environment = step_directory / 'toolchain'
        _ = toolchain_environment.write_text('')
        command = (shell_environment_script(shell)
                    + f'cd -- {shlex.quote(str(mirror))} || exit 75\n'
                    + toolchain_install_script(normalize_tool_version(values.get('parity.rustc', '')), target_name) + '\n'
                    + f'printf \'%s\' "${{RUSTUP_TOOLCHAIN-}}" > {shlex.quote(str(toolchain_environment))} || exit 75\n'
                    + '\n'.join(parity) + '\n'
                    + (clear_environment + '\n' if verb == 'nextest' and clear_environment else '')
                    + f'export {assignments}\n'
                    + f': > {shlex.quote(str(started))} || exit 75\n'
                    + f'exec "$HOME/.claude/scripts/lint/lint" {shlex.join([verb, *words])}')
        completed = subprocess.run(['bash', '-c', command], cwd=mirror, check=False,
                                   pass_fds=copies.inherited_mirror_descriptors())
        status = completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
        step_started = started.exists()
        toolchain = toolchain_environment.read_text()
        if toolchain:
            environment['RUSTUP_TOOLCHAIN'] = toolchain
    try:
        free = output_free_bytes(mirror, {**os.environ, **environment}) / (1024 ** 3)
        if free < floor:
            if target.exists():
                shutil.rmtree(target)
            cleanup = '0' if trim_build_folder(target, environment) == 0 else '1'
        else:
            trimmed = trim_build_folder(target, environment)
            cleanup = 'kept' if trimmed == 0 else '1'
    except OSError:
        cleanup = '1'
    if step_started:
        print(f'mac_exit={status}', flush=True)
    print(f'mac_cleanup={cleanup}', flush=True)
    return status


class RunnerArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.mirror: str = ''
        self.values: str = ''
        self.repository: str = ''
        self.verb: str = ''
        self.call_id: str = ''
        self.tree_key: str = ''
        self.repo_identity: str = ''
        self.environment: list[str] = []
        self.clear_environment: str = ''
        self.words: list[str] = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    runner = commands.add_parser('run')
    probe = commands.add_parser('probe')
    for command in (runner, probe):
        _ = command.add_argument('--mirror', required=True)
        _ = command.add_argument('--values', required=True)
        _ = command.add_argument('--repository', required=True)
    _ = runner.add_argument('--verb', required=True)
    _ = runner.add_argument('--call-id', required=True)
    _ = runner.add_argument('--tree-key', default='')
    _ = runner.add_argument('--repo-identity', default='')
    _ = runner.add_argument('--clear-environment', default='')
    _ = runner.add_argument('--env', dest='environment', action='append', default=[])
    _ = runner.add_argument('words', nargs=argparse.REMAINDER)
    arguments = parser.parse_args(namespace=RunnerArguments())
    values = cast(object, json.loads(arguments.values))
    if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in cast(dict[object, object], values).items()):
        raise ValueError('target settings must contain strings')
    if arguments.command == 'probe':
        return probe_shell(Path.home() / arguments.mirror, cast(dict[str, str], values), arguments.repository)
    words = arguments.words
    return run(Path.home() / arguments.mirror, cast(dict[str, str], values),
               arguments.repository, arguments.verb,
               tuple(words[1:] if words[:1] == ['--'] else words),
               arguments.call_id, arguments.tree_key, arguments.repo_identity,
               tuple((name, value) for entry in arguments.environment
                     for name, _, value in (entry.partition('='),)),
               arguments.clear_environment)


if __name__ == '__main__':
    raise SystemExit(main())
