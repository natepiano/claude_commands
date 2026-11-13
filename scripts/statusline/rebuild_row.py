"""Compare running systems with main and cache the last rebuild result."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import quote

from scripts.lib import conf_reads
from scripts.rebuild import need

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config/statusline.conf"


@dataclass(frozen=True)
class RebuildSettings:
    """The configured source, machines, refresh interval, and evaluation time limit."""

    repo: Path
    ref: str
    natedev: str
    mac: str
    ssh: str
    interval: int
    eval_timeout: int

    @classmethod
    def read(cls, path: Path) -> RebuildSettings:
        values = config_values(path)
        settings = cls(Path(values["rebuild.repo"]), values["rebuild.ref"],
                       values["rebuild.natedev"], values["rebuild.mac"],
                       values["rebuild.ssh"], int(values["rebuild.interval"]), int(values["rebuild.eval_timeout"]))
        if not settings.repo.is_absolute() or settings.interval <= 0:
            raise ValueError("Invalid rebuild settings")
        if not all((settings.ref, settings.natedev, settings.mac, settings.ssh)):
            raise ValueError("Empty rebuild settings")
        return settings


class CommandRunner(Protocol):
    """Read a command's output within its time limit."""

    def __call__(self, command: list[str], *, timeout: int = 30) -> str: ...


def command_output(command: list[str], *, timeout: int = 30) -> str:
    """Return command output, or an empty string when the command fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def system_path(value: str) -> bool:
    """Accept a single absolute Nix store path."""
    return value.startswith("/nix/store/") and len(value.split("/")) == 4 and not any(c.isspace() for c in value)


@dataclass(frozen=True)
class NoExpectedSystems:
    """No evaluation result exists for reuse."""


@dataclass(frozen=True)
class ExpectedSystems:
    """The expected system paths from one commit and evaluation source."""

    commit: str
    source: tuple[str, ...]
    natedev: str
    mac: str

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source,
                           "natedev": self.natedev, "mac": self.mac})


def expected_systems(settings: RebuildSettings, previous: ExpectedSystems | NoExpectedSystems,
                     run: CommandRunner) -> ExpectedSystems | NoExpectedSystems:
    """Reuse complete evaluations and retry missing results."""
    commit = run(["git", "-C", str(settings.repo), "rev-parse", "--verify", f"{settings.ref}^{{commit}}"])
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return NoExpectedSystems()
    source = (str(settings.repo), settings.ref, settings.natedev, settings.mac)
    if (isinstance(previous, ExpectedSystems) and previous.commit == commit and previous.source == source
            and system_path(previous.natedev) and system_path(previous.mac)):
        return previous
    flake = f"git+file://{quote(str(settings.repo))}?rev={commit}"
    expression = (
        f"let flake = builtins.getFlake {json.dumps(flake)}; "
        + "pathOrEmpty = value: let result = builtins.tryEval value; "
        + "in if result.success then result.value else \"\"; in { "
        + f"natedev = pathOrEmpty flake.nixosConfigurations.{json.dumps(settings.natedev)}.config.system.build.toplevel.outPath; "
        + f"mac = pathOrEmpty flake.darwinConfigurations.{json.dumps(settings.mac)}.system.outPath; }}"
    )
    output = run(["nix", "eval", "--json", "--no-write-lock-file", "--expr", expression], timeout=settings.eval_timeout)
    try:
        paths = cast(dict[str, object], json.loads(output))
        local, mac = paths["natedev"], paths["mac"]
        return ExpectedSystems(commit, source,
                               local if isinstance(local, str) and system_path(local) else "",
                               mac if isinstance(mac, str) and system_path(mac) else "")
    except (ValueError, KeyError, TypeError):
        pass
    return NoExpectedSystems()


def row(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
        run: Callable[[list[str]], str] = command_output) -> str:
    """Compare running systems with the last evaluated system paths."""
    items: list[str] = []
    for machine, path, command in (
        ("natedev", expected.natedev if isinstance(expected, ExpectedSystems) else "",
         ["realpath", "/run/current-system"]),
        ("mac", expected.mac if isinstance(expected, ExpectedSystems) else "",
         ["ssh", "-o", "BatchMode=yes", settings.ssh, "realpath /run/current-system"]),
    ):
        running = run(command)
        if system_path(path) and system_path(running) and running != path:
            items.append(f"rebuild {machine}")
    return ", ".join(items)


@dataclass(frozen=True)
class RebuildAnswer:
    """The known rebuild machines and whether the cache needs a refresh."""

    machines: tuple[str, ...]
    refresh_due: bool
    expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()

    def row(self) -> str:
        return ", ".join(f"rebuild {machine}" for machine in self.machines)


def config_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in conf_reads.read_text(path).splitlines():
        key, sep, value = line.partition("#")[0].partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def cache_path(config: Path) -> Path:
    path = Path(config_values(config)["rebuild.cache"]).expanduser()
    if not path.is_absolute():
        raise ValueError("The rebuild cache path must be absolute")
    return path


def read(config: Path = DEFAULT_CONFIG) -> RebuildAnswer:
    """Combine the cached comparison with open rebuild needs."""
    answer = _read_cache(config)
    try:
        machines = set(answer.machines) | {summary.newest["machine"] for summary in need.read_needs()}
    except (OSError, ValueError, KeyError, TypeError):
        return answer
    return replace(answer, machines=tuple(machine for machine in ("natedev", "mac") if machine in machines))


def _read_cache(config: Path) -> RebuildAnswer:
    """Read the configured comparison cache without evaluating systems."""
    try:
        cache = cache_path(config)
    except (OSError, ValueError, KeyError):
        return RebuildAnswer((), False)
    try:
        lines = cache.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2 or not lines[1].isdigit():
            return RebuildAnswer((), True)
        refresh_due = int(lines[1]) <= time.time() or config.stat().st_mtime_ns > cache.stat().st_mtime_ns
        items = lines[0].split(", ") if lines[0] else []
        if any(item not in ("rebuild natedev", "rebuild mac") for item in items):
            return RebuildAnswer((), True)
        expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
        if len(lines) >= 4:
            try:
                details = cast(dict[str, object], json.loads(lines[3]))
                commit, source = details["commit"], details["source"]
                local, mac = details["natedev"], details["mac"]
                if (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
                        and isinstance(source, list) and all(isinstance(value, str) for value in cast(list[object], source))
                        and isinstance(local, str) and system_path(local) and isinstance(mac, str) and system_path(mac)):
                    expected = ExpectedSystems(commit, tuple(cast(list[str], source)), local, mac)
            except (ValueError, KeyError, TypeError):
                pass
        return RebuildAnswer(tuple(item.removeprefix("rebuild ") for item in items), refresh_due, expected)
    except (OSError, ValueError):
        return RebuildAnswer((), True)


def write(config: Path, run: CommandRunner = command_output) -> None:
    """Refresh the configured cache without concurrent evaluations."""
    from scripts.statusline import counts_row

    try:
        cache = cache_path(config)
    except (OSError, ValueError, KeyError):
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        answer = read(config)
        if not answer.refresh_due:
            return
        began = time.time_ns()
        line, due, details = "", "", ""
        try:
            settings = RebuildSettings.read(config)
            expected = expected_systems(settings, answer.expected, run)
            line = row(settings, expected, run)
            if isinstance(expected, ExpectedSystems) and system_path(expected.natedev) and system_path(expected.mac):
                details = expected.details()
            due = str(int(time.time()) + settings.interval)
        except (OSError, ValueError, KeyError):
            pass
        counts_row.write_cache(cache, line, due, [config], began, details)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("mode", choices=("read", "refresh"))
    _ = parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = cast(Path, getattr(args, "config"))
    mode = cast(str, getattr(args, "mode"))
    if mode == "read":
        for machine in read(config).machines:
            print(machine)
    else:
        write(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
