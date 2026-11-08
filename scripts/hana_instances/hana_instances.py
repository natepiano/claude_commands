#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""List and stop test Hana processes on natedev and the Mac."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import cast

PROC_ROOT = Path("/proc")
# offload shares sibling modules with its CLI; keep that existing import boundary.

from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue



@dataclass(frozen=True)
class HanaInstancesConfig:
    exempt_ports: frozenset[int]
    test_max_seconds: float
    unlabelled_max_seconds: float
    stop_grace_seconds: float


@dataclass(frozen=True)
class EnvironmentKnown:
    pass


@dataclass(frozen=True)
class EnvironmentUnknown:
    cause: str


ENVIRONMENT_KNOWN = EnvironmentKnown()


@dataclass(frozen=True)
class HanaProcess:
    machine: str
    pid: int
    port: int
    owner: str
    purpose: str
    age_seconds: float
    checkout: Path
    environment: dict[str, str]
    start_identity: str
    environment_status: EnvironmentKnown | EnvironmentUnknown = ENVIRONMENT_KNOWN


def read_config(*, hana_instances_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> HanaInstancesConfig:
    if isinstance(hana_instances_settings, UseConfiguredValue):
        hana_instances_settings = conf.read("hana_instances")
    settings = hana_instances_settings.source
    values = hana_instances_settings.values()
    try:
        ports = frozenset(int(item.strip()) for item in values["exempt_ports"].split(",") if item.strip())
    except (KeyError, ValueError) as error:
        raise ValueError(f"{settings}: exempt_ports needs comma-separated ports") from error
    if any(not 0 < port <= 65535 for port in ports):
        raise ValueError(f"{settings}: exempt_ports contains an invalid port")
    return HanaInstancesConfig(ports, hana_instances_settings.number("test_max_hours") * 3600,
                       hana_instances_settings.number("unlabelled_max_minutes") * 60,
                       hana_instances_settings.number("stop_grace_seconds"))


@dataclass(frozen=True)
class HanaCheckout:
    path: Path


@dataclass(frozen=True)
class NotHana:
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    start_identity: str


@dataclass(frozen=True)
class ProcessGone:
    pass


def hana_checkout(executable: str) -> HanaCheckout | NotHana:
    match = re.fullmatch(r"(.+)/target/[^/]+/hana", executable.removesuffix(" (deleted)"))
    return HanaCheckout(Path(match[1])) if match else NotHana()


def process_row(machine: str, pid: int, executable: str, age: float,
                environment: dict[str, str], start_identity: str) -> HanaProcess | NotHana:
    checkout = hana_checkout(executable)
    if isinstance(checkout, NotHana):
        return checkout
    if not start_identity:
        raise ValueError(f"{machine}:{pid}: process start identity is missing")
    try:
        port = int(environment.get("BRP_EXTRAS_PORT", ""))
    except ValueError:
        port = 0
    return HanaProcess(machine, pid, port, environment.get("HANA_TEST_OWNER", "").strip(),
                       environment.get("HANA_TEST_PURPOSE", "").strip(), max(0.0, age),
                       checkout.path, environment, start_identity)


def local_identity(pid: int, proc_root: Path = PROC_ROOT) -> ProcessIdentity | ProcessGone:
    try:
        fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return ProcessGone()
    if fields[0] in ("Z", "X"):
        return ProcessGone()
    return ProcessIdentity(fields[19])


def local_processes(
    proc_root: Path = PROC_ROOT, now: float | None = None,
    environment_reader: Callable[[int], dict[str, str]] | None = None,
) -> list[HanaProcess]:
    if environment_reader is None:
        # Phase 43 uses this existing reader; its owning module keeps the private name.
        from scripts.shutdown.inventory import _process_environment  # pyright: ignore[reportPrivateUsage]
        environment_reader = _process_environment
    current = time.time() if now is None else now
    boot = next(float(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
    ticks = os.sysconf("SC_CLK_TCK")
    rows: list[HanaProcess] = []
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            pid = int(directory.name)
            identity = local_identity(pid, proc_root)
            if isinstance(identity, ProcessGone):
                continue
            executable = str((directory / "exe").readlink())
            if isinstance(hana_checkout(executable), NotHana):
                continue
            started = boot + float(identity.start_identity) / ticks
            row = process_row("natedev", pid, executable, current - started, environment_reader(pid), identity.start_identity)
            if local_identity(pid, proc_root) != identity:
                continue
        except (OSError, ValueError, IndexError):
            continue
        if isinstance(row, HanaProcess):
            rows.append(row)
    return rows


def elapsed_seconds(value: str) -> float:
    days, separator, clock = value.partition("-")
    parts = [int(part) for part in (clock if separator else days).split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds + (int(days) * 86400 if separator else 0))


def darwin_environment(pid: int) -> dict[str, str]:
    from scripts.herdr.herdr import HerdrFailure, darwin_process_environment
    try:
        return darwin_process_environment(pid)
    except HerdrFailure as error:
        raise ValueError(f"mac:{pid}: {error}") from error


# A fixed locale and zone make lstart a stable identity on either machine.
DARWIN_PS = "LC_ALL=C TZ=UTC ps eww -axww -o pid=,lstart=,etime=,command="


def mac_processes(output: str, environment_reader: Callable[[int], dict[str, str]] = darwin_environment) -> list[HanaProcess]:
    rows: list[HanaProcess] = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+([A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s+([\d:-]+)\s+(\S+)(?:\s+.*)?$", line)
        if match is None or isinstance(hana_checkout(match[4]), NotHana):
            continue
        environment_status: EnvironmentKnown | EnvironmentUnknown = ENVIRONMENT_KNOWN
        try:
            environment = environment_reader(int(match[1]))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            environment = {}
            environment_status = EnvironmentUnknown(str(error))
        row = process_row("mac", int(match[1]), match[4], elapsed_seconds(match[3]),
                          environment, " ".join(match[2].split()))
        if isinstance(row, HanaProcess):
            rows.append(replace(row, environment_status=environment_status))
    return rows


def is_test_hana(row: HanaProcess, config: HanaInstancesConfig) -> bool:
    return bool(row.owner or row.purpose or (row.port != 0 and row.port not in config.exempt_ports)
                or any(key in row.environment for key in ("CLAUDECODE", "SSH_CONNECTION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")))


def over_limit(row: HanaProcess, config: HanaInstancesConfig) -> bool:
    limit = config.test_max_seconds if row.owner or row.purpose else config.unlabelled_max_seconds
    return is_test_hana(row, config) and row.age_seconds > limit


def run_ssh(command: list[str]) -> subprocess.CompletedProcess[str]:
    from scripts.mac_test import offload
    return subprocess.run(command, capture_output=True, text=True, check=False,
                          timeout=offload.read_config().probe_timeout_s)


def run_local(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def remote_environment(pid: int, ssh_runner: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> dict[str, str]:
    from scripts.mac_test import offload
    # Reuse herdr's stable argv/environment boundary on the machine being inspected.
    program = ("import json; "
               "from scripts.herdr.herdr import darwin_process_environment; "
               f"print(json.dumps(darwin_process_environment({pid})))")
    result = ssh_runner([*offload.ssh_prefix(offload.read_config()),
                         "~/.claude/scripts/lib/py -c " + shlex.quote(program)])
    if result.returncode:
        raise ValueError(f"mac:{pid}: environment could not be read")
    value = cast(object, json.loads(result.stdout))
    if not isinstance(value, dict):
        raise ValueError(f"mac:{pid}: environment is invalid")
    environment: dict[str, str] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"mac:{pid}: environment is invalid")
        environment[key] = item
    return environment


def environment_problem(row: HanaProcess, status: EnvironmentUnknown) -> str:
    return f"{row.machine}:{row.pid}: environment unknown: {status.cause}"


def collect_processes(
    local_source: Callable[[], list[HanaProcess]] = local_processes,
    ssh_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_ssh,
    *, platform: str | None = None,
    local_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_local,
    environment_reader: Callable[[int], dict[str, str]] = darwin_environment,
) -> tuple[list[HanaProcess], list[str]]:
    problems: list[str] = []
    if (sys.platform if platform is None else platform) == "darwin":
        result = local_runner(["sh", "-c", DARWIN_PS])
        if result.returncode:
            raise ValueError("mac: local process table could not be read")
        rows = mac_processes(result.stdout, environment_reader)
    else:
        from scripts.mac_test import offload
        rows = local_source()
        try:
            result = ssh_runner([*offload.ssh_prefix(offload.read_config()), DARWIN_PS])
            if result.returncode:
                problems.append("mac: unreachable")
            else:
                rows.extend(mac_processes(result.stdout, lambda pid: remote_environment(pid, ssh_runner)))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            problems.append("mac: unreachable or process inventory could not be read")
    for row in rows:
        if isinstance(row.environment_status, EnvironmentUnknown):
            problems.append(environment_problem(row, row.environment_status))
    return sorted(rows, key=lambda row: (row.machine, row.pid)), problems


def darwin_identity(pid: int, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_local) -> ProcessIdentity | ProcessGone:
    result = runner(["sh", "-c", "LC_ALL=C TZ=UTC ps -axww -o pid=,lstart="])
    if result.returncode:
        raise ValueError(f"mac:{pid}: process identity could not be read")
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[0] == str(pid):
            return ProcessIdentity(" ".join(fields[1].split()))
    return ProcessGone()


def remote_stop(row: HanaProcess, config: HanaInstancesConfig,
                ssh_runner: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> None:
    from scripts.mac_test import offload
    # Read the complete table so a failed ps cannot be mistaken for an absent pid.
    command = f"""same_process() {{
    table=$(LC_ALL=C TZ=UTC ps -axww -o pid=,lstart=) || return 2
    current=$(printf '%s\n' "$table" | awk '$1 == {row.pid} {{$1=""; sub(/^ +/, ""); gsub(/ +/, " "); print}}')
    [ "$current" = {shlex.quote(row.start_identity)} ]
}}
gone() {{ printf '%s\n' 'hana-instances: process gone'; exit 0; }}
same_process
identity_status=$?
[ "$identity_status" = 1 ] && gone
[ "$identity_status" = 0 ] || exit 2
kill -TERM {row.pid} || exit 2
sleep {shlex.quote(str(config.stop_grace_seconds))} || exit 2
same_process
identity_status=$?
[ "$identity_status" = 1 ] && gone
[ "$identity_status" = 0 ] || exit 2
kill -KILL {row.pid} || exit 2
same_process
identity_status=$?
[ "$identity_status" = 1 ] && gone
exit 2"""
    result = ssh_runner([*offload.ssh_prefix(offload.read_config()), command])
    if result.returncode or result.stdout.strip() != "hana-instances: process gone":
        raise ValueError(f"mac:{row.pid}: stop could not be confirmed")


def stop_process(
    row: HanaProcess, config: HanaInstancesConfig, *,
    signal_call: Callable[[int, int], object] = os.kill,
    sleep_call: Callable[[float], object] = time.sleep,
    ssh_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_ssh,
    identity_reader: Callable[[int], ProcessIdentity | ProcessGone] | None = None,
    platform: str | None = None,
) -> None:
    if isinstance(row.environment_status, EnvironmentUnknown):
        raise ValueError(f"{environment_problem(row, row.environment_status)}; stop could not be confirmed")
    if not is_test_hana(row, config):
        raise ValueError(f"{row.machine}:{row.pid}: user Hana is exempt")
    host = sys.platform if platform is None else platform
    if row.machine == "mac" and host != "darwin":
        remote_stop(row, config, ssh_runner)
    else:
        reader = identity_reader if identity_reader is not None else (darwin_identity if host == "darwin" else local_identity)

        def same_process() -> bool:
            identity = reader(row.pid)
            return isinstance(identity, ProcessIdentity) and identity.start_identity == row.start_identity

        try:
            if same_process():
                _ = signal_call(row.pid, signal.SIGTERM)
                _ = sleep_call(config.stop_grace_seconds)
                if same_process():
                    _ = signal_call(row.pid, signal.SIGKILL)
                    if same_process():
                        raise ValueError(f"{row.machine}:{row.pid}: stop could not be confirmed")
        except ProcessLookupError:
            pass
    print(f"{row.machine}:{row.pid}: stopped")


def row_text(row: HanaProcess) -> str:
    if isinstance(row.environment_status, EnvironmentUnknown):
        return (f"{row.machine}:{row.pid} port unknown owner unknown purpose unknown "
                f"age {row.age_seconds / 3600:.2f} h checkout {row.checkout} environment unknown")
    return (f"{row.machine}:{row.pid} port {row.port or 'unknown'} owner {row.owner or 'unlabelled'} "
            f"purpose {row.purpose or 'unlabelled'} age {row.age_seconds / 3600:.2f} h checkout {row.checkout}")


def stop_checkout(checkout: Path, config: HanaInstancesConfig) -> None:
    """Confirm test Hanas exited and no process still uses a checkout before its task ends."""
    checkout = checkout.resolve()

    def checkout_processes() -> list[HanaProcess]:
        rows, problems = collect_processes()
        unrelated_environment_problems = {
            environment_problem(row, row.environment_status)
            for row in rows
            if isinstance(row.environment_status, EnvironmentUnknown)
            and not row.checkout.resolve().is_relative_to(checkout)
        }
        relevant_problems = [problem for problem in problems if problem not in unrelated_environment_problems]
        if relevant_problems:
            raise ValueError("; ".join(relevant_problems))
        return [row for row in rows if row.checkout.resolve().is_relative_to(checkout)]

    for row in checkout_processes():
        stop_process(row, config)
    if checkout_processes():
        raise ValueError(f"{checkout}: Hana exit could not be confirmed")
    # Reuse the audit's host-aware process-use scan for builds and examples too.
    from scripts.lint.folder_audit import ProcessUsesUnavailable, process_uses
    uses = process_uses([checkout])
    if isinstance(uses, ProcessUsesUnavailable):
        raise ValueError(f"{checkout}: process use could not be read")
    if any(Path(path).resolve().is_relative_to(checkout) for path, _, _ in uses.paths):
        raise ValueError(f"{checkout}: a process still uses the checkout")
    print("hana-instances: checkout clear")


class Arguments(argparse.Namespace):
    command: str = ""
    target: str = ""
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("list")
    stop = commands.add_parser("stop")
    _ = stop.add_argument("target", help="natedev:<pid|port> or mac:<pid|port>")
    checkout = commands.add_parser("stop-checkout")
    _ = checkout.add_argument("target", help="local checkout whose test Hanas must exit before removal")
    sweep = commands.add_parser("sweep")
    _ = sweep.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        config = read_config()
        if args.command == "stop-checkout":
            stop_checkout(Path(args.target), config)
            return 0
        rows, problems = collect_processes()
        for problem in problems:
            print(problem)
        if args.command == "list":
            for row in rows:
                print(row_text(row))
        elif args.command == "sweep":
            failed = bool(problems)
            for row in rows:
                if over_limit(row, config):
                    if args.dry_run:
                        print(f"would stop {row_text(row)}")
                    else:
                        try:
                            stop_process(row, config)
                        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                            print(f"hana-instances: {error}", file=sys.stderr)
                            failed = True
            return 2 if failed else 0
        else:
            machine, separator, identity = args.target.partition(":")
            if not separator or machine not in ("natedev", "mac") or not identity.isdecimal():
                raise ValueError("stop needs natedev:<pid|port> or mac:<pid|port>")
            matches = [row for row in rows if row.machine == machine and (row.pid == int(identity) or row.port == int(identity))]
            if len(matches) > 1:
                raise ValueError(f"{args.target}: more than one Hana matches; use its pid")
            if matches:
                stop_process(matches[0], config)
            elif any(problem.startswith(machine + ":") for problem in problems):
                raise ValueError(f"{machine}: stop could not be confirmed")
            else:
                print(f"{args.target}: already gone")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"hana-instances: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
