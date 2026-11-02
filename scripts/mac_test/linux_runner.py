#!/usr/bin/env python3
"""Execute trusted source in a cached Linux development shell."""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict, cast
import uuid

PROC_ROOT = Path('/proc')


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
            with tempfile.TemporaryDirectory(prefix=f'.{key}.', dir=cache) as stage_text:
                stage = Path(stage_text)
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


def policy_setting(values: dict[str, str], key: str, repository: str) -> str:
    return values[f'{key}.{repository}'] if f'{key}.{repository}' in values else values[key]


def probe_script(mirror: str, *, shell_environment: bool = False) -> str:
    """Read Linux disk capacity and the public tool versions."""
    return '\n'.join((
        ':' if shell_environment else 'export PATH="$HOME/.cargo/bin:$PATH"',
        f'mkdir -p -- "$HOME"/{shlex.quote(mirror)} || exit 1',
        'free_gib=$(df -Pk "$HOME" | awk \'NR == 2 {printf "%.6f", $4 / 1048576}\') || exit 1',
        'printf \'free_gib=%s\\nhost=%s\\n\' "$free_gib" "$(hostname -s)"',
        'rustc_version=$(rustc -V) || exit 1',
        'nextest_version=$(cargo nextest --version) || exit 1',
        'mend_version=$(cargo mend --version) || exit 1',
        'scripts_commit=$(cat "$HOME/.claude/.build-target-script-commit") || exit 1',
        'printf \'rustc=%s\\nnextest=%s\\nmend=%s\\nscripts_commit=%s\\nrc=0\\n\' '
        + '"$rustc_version" "$nextest_version" "$mend_version" "$scripts_commit"',
    ))


class SlotRun(TypedDict):
    """A process group that owns one slot until every process ends."""

    pid: int
    start_time: int
    process_group: int
    token: str


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
    return {'pid': cast(int, fields['pid']), 'start_time': cast(int, fields['start_time']),
            'process_group': cast(int, fields['process_group']), 'token': cast(str, fields['token'])}


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
    """An earlier run still has unresolved process ownership."""


SLOT_PROCESSES_UNRESOLVED = 3
STARTUP_REFUSAL_RECORD = 'linux_startup=unresolved'


def select_slot_processes(mirror: Path, proc_root: Path = PROC_ROOT) -> SlotProcessSelection:
    """Read each ownership source independently before deciding uncertainty."""
    lease = read_slot_run(mirror / '.linux-run.json')
    selected: set[int] = set()
    parents: dict[int, int] = {}
    uncertain: set[int] = set()
    root = mirror.resolve()
    own_pid = os.getpid()
    for process in proc_root.glob('[0-9]*'):
        pid = int(process.name)
        try:
            if process.stat().st_uid != os.getuid():
                continue
            fields = process_fields(pid, proc_root)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            uncertain.add(pid)
            continue
        if fields[0] in ('Z', 'X'):
            continue
        if not isinstance(lease, UnmarkedSlot):
            if pid == lease['pid']:
                if (int(fields[19]) != lease['start_time']
                        or int(fields[2]) != lease['process_group']):
                    raise ValueError('slot process identity changed')
                selected.add(pid)
                parents[pid] = int(fields[1])
                continue
            if int(fields[19]) < lease['start_time']:
                continue
        parents[pid] = int(fields[1])
        if pid == own_pid:
            continue
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
        if denied and pid not in selected:
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


def slot_processes(mirror: Path, proc_root: Path = PROC_ROOT) -> tuple[int, ...]:
    """Return only confirmed processes, or refuse uncertain cleanup."""
    selection = select_slot_processes(mirror, proc_root)
    if selection.unresolved:
        raise SlotProcessesUnresolved('slot process ownership remains unresolved')
    if not selection.pids:
        (mirror / '.linux-run.json').unlink(missing_ok=True)
    return selection.pids


def print_slot_processes(mirror: Path) -> int:
    """Refuse an uncertain external selection without a traceback or kill list."""
    try:
        pids = slot_processes(mirror)
    except SlotProcessesUnresolved:
        return SLOT_PROCESSES_UNRESOLVED
    except (OSError, ValueError):
        return 1
    if pids:
        print(*pids, sep='\n')
    return 0


@contextmanager
def slot_run(mirror: Path) -> Generator[None, None, None]:
    """Publish ownership before shell evaluation can start a child process."""
    try:
        if (mirror / '.linux-run.json').exists() and slot_processes(mirror):
            raise ValueError('the slot still has running processes')
    except SlotProcessesUnresolved as error:
        raise SlotStartupRefused('an earlier run still has unresolved ownership') from error
    pid = os.getpid()
    token = uuid.uuid4().hex
    lease: SlotRun = {'pid': pid, 'start_time': int(process_fields(pid)[19]),
                      'process_group': os.getpgrp(), 'token': token}
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
            children = slot_processes(mirror)
        except (OSError, ValueError, SlotProcessesUnresolved):
            print('mac_cleanup=unconfirmed', flush=True)
        else:
            if children:
                print('mac_cleanup=unconfirmed', flush=True)
            else:
                marker.unlink(missing_ok=True)


def mirror_selector_script(mirror: str) -> str:
    """Select the runner and its children even outside the slot directory."""
    code = ("import sys; from pathlib import Path; "
            + "sys.path.insert(0, str(Path.home()/'.claude/scripts/mac_test')); "
            + "from linux_runner import print_slot_processes; "
            + "sys.exit(print_slot_processes(Path(sys.argv[1])))")
    return ('select_leftovers() {\n'
            + f'    python3 -c {shlex.quote(code)} "$HOME"/{shlex.quote(mirror)}\n'
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
                   words: tuple[str, ...], call_id: str, tree_key: str) -> str:
    arguments = ['python3', '.claude/scripts/mac_test/linux_runner.py', 'run',
                 '--mirror', mirror, '--values', json.dumps(values), '--repository', repository,
                 '--verb', verb, '--call-id', call_id, '--tree-key', tree_key, '--', *words]
    return shlex.join(arguments)


def probe_command_script(mirror: str, values: dict[str, str], repository: str) -> str:
    return shlex.join(['python3', '.claude/scripts/mac_test/linux_runner.py', 'probe',
                       '--mirror', mirror, '--values', json.dumps(values), '--repository', repository])


def probe_shell(mirror: Path, values: dict[str, str], repository: str) -> int:
    """Read parity inside the same cached environment as the work."""
    try:
        with slot_run(mirror):
            shell = prepare_shell(mirror, Path.home() / values['shell_cache'],
                                  policy_setting(values, 'devshell', repository),
                                  positive_setting(values, 'shell_timeout_s'))
            command = (f'source {shlex.quote(str(shell.exports))} || exit 75\n'
                       + 'if [[ -n ${shellHook-} ]]; then eval "$shellHook" || exit 75; fi\n'
                       + f'cd -- {shlex.quote(str(mirror))} || exit 75\n'
                       + probe_script(str(mirror.relative_to(Path.home())), shell_environment=True))
            return subprocess.run(['bash', '-c', command], cwd=mirror, check=False).returncode
    except SlotStartupRefused:
        print(STARTUP_REFUSAL_RECORD, flush=True)
        return 75


def run(mirror: Path, values: dict[str, str], repository: str, verb: str,
        words: tuple[str, ...], call_id: str, tree_key: str) -> int:
    try:
        with slot_run(mirror):
            return execute_slot(mirror, values, repository, verb, words, call_id, tree_key)
    except SlotStartupRefused:
        print(STARTUP_REFUSAL_RECORD, flush=True)
        return 75


def trim_build_folder(target: Path, environment: dict[str, str]) -> int:
    """Use the existing budget sweep without the machine-wide disk sweep."""
    code = '\n'.join((
        'import os, sys',
        'from pathlib import Path',
        "sys.path.insert(0, str(Path.home() / '.claude/scripts/lint'))",
        'import sweep',
        'config = os.path.expanduser(os.environ.get(sweep.CONFIG_ENV) or sweep.DEFAULT_CONFIG)',
        'values = sweep.config_values(config)',
        'recent = sweep.size_bytes(values.get(sweep.RECENT_KEY, str(sweep.DEFAULT_RECENT_MINUTES)), 60)',
        "if recent is None: raise ValueError('invalid sweep recent window')",
        'sys.exit(sweep.sweep_workspace(sys.argv[1], config, False, recent / 60))',
    ))
    return subprocess.run([sys.executable, '-c', code, str(target)],
                          env={**os.environ, **environment}, check=False).returncode


def execute_slot(mirror: Path, values: dict[str, str], repository: str, verb: str,
        words: tuple[str, ...], call_id: str, tree_key: str) -> int:
    if verb not in ('nextest', 'check', 'doc'):
        raise ValueError('Linux targets accept only test, check and doc')
    if verb == 'nextest':
        words = nextest_words_with_skip(words, values.get(f'skip.{repository}', ''), insert_filter=True)
    kind = 'test' if verb == 'nextest' else verb
    if kind not in {word.strip() for word in values['kinds'].split(',')}:
        raise ValueError(f'{kind}: target does not permit this command')
    selector = policy_setting(values, 'devshell', repository)
    shell = prepare_shell(mirror, Path.home() / values['shell_cache'], selector,
                          positive_setting(values, 'shell_timeout_s'))
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
    environment = {
        'BUILDLOG_BUILD_BUDGET_GIB': f'{build_budget:g}',
        'LINT_SWEEP_BUDGET_GIB': f'{budget:g}',
        'BUILDLOG_CALLER': 'verify-linux', 'BUILDLOG_CALL_ID': call_id,
        'BUILDLOG_SYNC': '0', 'LINT_SKIP_SWEEP': '1',
        'CARGO_TARGET_DIR': str(target),
    }
    if tree_key:
        environment['BUILDLOG_SENT_TREE_KEY'] = tree_key
    if verb == 'doc':
        environment['LINT_CONFIG_FORCE'] = '1'
    assignments = ' '.join(f'{key}={shlex.quote(value)}' for key, value in environment.items())
    parity: list[str] = []
    for name, invocation in (('rustc', 'rustc -V'), ('nextest', 'cargo nextest --version'),
                             ('mend', 'cargo mend --version')):
        expected = values.get(f'parity.{name}', '')
        if expected:
            parity.append(f'[ "$({invocation})" = {shlex.quote(expected)} ] || exit 75')
    command = (f'source {shlex.quote(str(shell.exports))} || exit 75\n'
               + 'if [[ -n ${shellHook-} ]]; then eval "$shellHook" || exit 75; fi\n'
               + f'cd -- {shlex.quote(str(mirror))} || exit 75\n'
               + '\n'.join(parity) + '\n'
               + f'export {assignments}\n')
    print(f'linux_runner: mirror={mirror} shell_key={shell.key}', flush=True)
    with tempfile.TemporaryDirectory(prefix='.linux-step-', dir=mirror) as step_directory:
        started = Path(step_directory) / 'started'
        command += (f': > {shlex.quote(str(started))} || exit 75\n'
                    + f'exec "$HOME/.claude/scripts/lint/lint" {shlex.join([verb, *words])}')
        completed = subprocess.run(['bash', '-c', command], cwd=mirror, check=False)
        status = completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
        step_started = started.exists()
    try:
        free = shutil.disk_usage(mirror).free / (1024 ** 3)
        if free < floor:
            if target.exists():
                shutil.rmtree(target)
            cleanup = '0'
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
               arguments.call_id, arguments.tree_key)


if __name__ == '__main__':
    raise SystemExit(main())
