#!/usr/bin/env python3
"""Prove Linux execution over Tailscale SSH in a managed scratch home."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import uuid

from scripts.mac_test import linux_runner
from scripts.lib import conf

from scripts.lint import memory_admit


REMOTE_PROOF = r"""
import json
import os
from pathlib import Path
import subprocess
import sys

home = Path(sys.argv[1])
values = json.loads(sys.argv[2])
with open(home / '.claude/scripts/lib/package_root.py') as package_root:
    exec(package_root.read(), {'__file__': package_root.name})
from scripts.mac_test import linux_runner
from scripts.lint.memory_test_support import fake_memory_readings, gate_isolation_environment

fixture = home / 'fixture'
fixture.mkdir()
(fixture / 'src').mkdir()
(fixture / 'Cargo.toml').write_text('[package]\nname="linux-loopback-proof"\nversion="0.1.0"\nedition="2021"\n')
source = '/// A small proof.\npub fn proof() -> bool { true }\n#[test] fn works() { assert!(proof()); }\n'
(fixture / 'src/lib.rs').write_text(source)
(fixture / 'flake.nix').write_text('''{
  inputs.nixpkgs.url = "nixpkgs";
  outputs = { self, nixpkgs }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
  in { devShells.x86_64-linux.default = pkgs.mkShell {
    packages = [ pkgs.rustc pkgs.cargo pkgs.cargo-nextest ];
  }; };
}\n''')
# Resolve the public Nix input before selecting the small shell copy.
subprocess.run(['nix', 'flake', 'lock', str(fixture)], check=True)
subprocess.run(['git', 'init', '-q', str(fixture)], check=True)
subprocess.run(['git', '-C', str(fixture), 'add', '.'], check=True)
subprocess.run(['git', '-C', str(fixture), 'update-index', '--refresh'], check=True)
readings = home / 'memory'
readings.mkdir()
kib = int(float(values['loopback.memory_gib']) * 1024 * 1024)
os.environ.update(fake_memory_readings(readings, available_kib=kib, total_kib=kib))
os.environ.update(gate_isolation_environment(home))
os.environ['BUILDLOG_DIR'] = str(home / '.local/state/buildlog')
os.environ['HOME'] = str(home)
settings = {
    'kinds': 'test,check,doc', 'devshell': values['loopback.shell'],
    'shell_cache': '.local/state/build-target/shells',
    'shell_timeout_s': values['loopback.shell_timeout_s'],
    'build_budget_gib': values['loopback.build_budget_gib'],
    'target_budget_gib': values['loopback.target_budget_gib'],
    'free_floor_gib': values['loopback.free_floor_gib'],
}
import shutil
mirror = home / '.local/state/build-target/mirror/proof/1'
shutil.copytree(fixture, mirror)
for verb in ('nextest', 'check', 'doc'):
    if verb == 'doc':
        # The staged snapshot stays valid while the worktree contains invalid source.
        (fixture / 'src/lib.rs').write_text('this is not valid Rust\n')
        staged = subprocess.run(['git', '-C', str(fixture), 'show', ':src/lib.rs'],
                                capture_output=True, text=True, check=True).stdout
        (mirror / 'src/lib.rs').write_text(staged)
    status = linux_runner.run(mirror, settings, 'linux-loopback-proof', verb,
                              (), 'linux-loopback-proof', '')
    if status:
        raise SystemExit(status)
print('loopback: test, check and staged doc passed; the slot retained its warm build folder')
"""


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    registry = Path(os.environ.get('BUILD_TARGETS_CONFIG', str(root / 'config/build_targets.conf'))).expanduser()
    config = conf.read(registry)
    values = config.values()
    memory = int(linux_runner.positive_setting(values, 'loopback.memory_gib') * memory_admit.GIB)
    # The isolated fixture has no history for nextest, check or doc.
    try:
        configuration = memory_admit.read_memory_config()
        need = configuration.program_first_peaks["cargo"]
        required_free = need + configuration.policy.safety_bytes
    except conf.Unusable as error:
        print(str(error), file=sys.stderr)
        return 2
    if memory < required_free:
        print(f'loopback: cannot admit the fixture; loopback.memory_gib must provide '
              + f'{required_free / memory_admit.GIB:g} GiB including the safety reserve', file=sys.stderr)
        return 2
    host = values['loopback.host']
    connect = config.number('loopback.connect_timeout_s')
    deadline = config.number('loopback.command_timeout_s')
    if not host or connect <= 0 or deadline <= 0:
        raise ValueError('loopback host and time limits must be configured')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={connect:g}']
    port = values['loopback.port']
    known_hosts = values['loopback.known_hosts']
    if port:
        ssh.extend(['-p', port])
    if known_hosts:
        ssh.extend(['-o', f'UserKnownHostsFile={known_hosts}', '-o', 'StrictHostKeyChecking=yes'])
    ssh.append(host)
    task = 'gate:linux-loopback-' + uuid.uuid4().hex
    bootstrap = (
        'import sys; from pathlib import Path\n'
        'with open(Path.home()/".claude/scripts/lib/package_root.py") as package_root:\n'
        '    exec(package_root.read(), {"__file__": package_root.name})\n'
        'from scripts.lib import scratch; print(scratch.new(task=sys.argv[1],word="linux-home"))'
    )
    created = subprocess.run([*ssh, shlex.join(['python3', '-c', bootstrap, task])],
                             capture_output=True, text=True, timeout=deadline, check=True)
    home = Path(created.stdout.strip())
    if not home.is_absolute() or len(created.stdout.splitlines()) != 1:
        raise ValueError('the remote scratch home is not an absolute path')
    try:
        listing = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', 'scripts', 'config'],
                                 capture_output=True, text=True, check=True)
        paths = [path for path in listing.stdout.split('\0') if path]
        # The director can prove the new public modules before the phase commit.
        for path in ('scripts/mac_test/linux_runner.py', 'scripts/mac_test/loopback_proof.py'):
            if path not in paths:
                paths.append(path)
        paths = [path for path in paths if path.startswith('scripts/') or path in (
            'config/lint.conf', 'config/build_targets.conf', 'config/aws_target.conf', 'config/user_zone.conf')]
        with tempfile.NamedTemporaryFile('w', encoding='utf-8') as manifest:
            _ = manifest.write('\n'.join(paths) + '\n')
            manifest.flush()
            _ = subprocess.run([*ssh, f'mkdir -p -- {shlex.quote(str(home / ".claude"))}'], check=True, timeout=deadline)
            _ = subprocess.run(['rsync', '-rlpc', f'--files-from={manifest.name}',
                            '-e', shlex.join(ssh[:-1]), f'{root}/',
                            f'{host}:{shlex.quote(str(home / ".claude"))}/'], check=True, timeout=deadline)
        completed = subprocess.run([*ssh, shlex.join(['python3', '-c', REMOTE_PROOF, str(home), json.dumps(values)])],
                                   check=False, timeout=deadline)
        return completed.returncode
    finally:
        cleanup = (
            'import sys; from pathlib import Path\n'
            'with open(Path.home()/".claude/scripts/lib/package_root.py") as package_root:\n'
            '    exec(package_root.read(), {"__file__": package_root.name})\n'
            'from scripts.lib import scratch; scratch.end(sys.argv[1])'
        )
        _ = subprocess.run([*ssh, shlex.join(['python3', '-c', cleanup, task])], check=True, timeout=deadline)
        print(f'loopback: removed the managed scratch home {home}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
