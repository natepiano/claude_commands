#!/usr/bin/env python3
"""Read configured build targets and their recorded holds and runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Literal


SCRIPTS = Path(__file__).resolve().parent.parent
for directory in ("build_hold", "mac_test", "lint"):
    sys.path.insert(0, str(SCRIPTS / directory))
import build_hold
import mac_test
import sweep


Platform = Literal["linux", "macos"]


@dataclass(frozen=True)
class LocalTarget:
    name: str
    platform: Platform


@dataclass(frozen=True)
class SshTarget:
    name: str
    platform: Platform
    settings: Path


Target = LocalTarget | SshTarget


class ConfigError(ValueError):
    """A build-target key is missing or has an unsupported value."""


@dataclass(frozen=True)
class ExpiresAt:
    when: datetime


@dataclass(frozen=True)
class ReleaseEstimated:
    when: datetime


@dataclass(frozen=True)
class UntilReleased:
    pass


@dataclass(frozen=True)
class AwaitingBlockRemoval:
    """A block remains enforced after its requested expiry."""


HoldEnd = ExpiresAt | ReleaseEstimated | UntilReleased | AwaitingBlockRemoval


@dataclass(frozen=True)
class Free:
    pass


@dataclass(frozen=True)
class Running:
    what: str
    since: datetime


@dataclass(frozen=True)
class Held:
    holder: str
    reason: str
    since: datetime
    end: HoldEnd


@dataclass(frozen=True)
class Unreachable:
    reason: str


@dataclass(frozen=True)
class StateUnreadable:
    reason: str


TargetState = Free | Running | Held | Unreachable | StateUnreadable


def read_targets(path: Path | None = None) -> tuple[Target, ...]:
    default = path if path is not None else SCRIPTS.parent / "config/build_targets.conf"
    config = Path(os.environ.get("BUILD_TARGETS_CONFIG", str(default))).expanduser().resolve()
    values = sweep.config_values(str(config))
    names = tuple(name.strip() for name in values.get("targets", "").split(","))
    if not all(names) or len(set(names)) != len(names):
        raise ConfigError(f"targets: expected distinct target names in {config}")
    targets: list[Target] = []
    for name in names:
        kind = values.get(f"kind.{name}", "")
        if kind not in ("local", "ssh"):
            raise ConfigError(f"kind.{name}: expected local or ssh, got {kind!r}")
        platform = values.get(f"platform.{name}", "")
        if platform not in ("linux", "macos"):
            raise ConfigError(f"platform.{name}: expected linux or macos, got {platform!r}")
        if kind == "local":
            targets.append(LocalTarget(name, platform))
            continue
        key = f"settings.{name}"
        setting = values.get(key, "")
        if not setting:
            raise ConfigError(f"{key}: expected a settings file")
        settings = (config.parent / Path(setting).expanduser()).resolve()
        if not settings.is_file():
            raise ConfigError(f"{key}: settings file does not exist: {settings}")
        targets.append(SshTarget(name, platform, settings))
    return tuple(targets)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def target_state(target: Target) -> TargetState:
    """Read state without probing a host or changing coordination files."""
    try:
        if isinstance(target, LocalTarget):
            holds = build_hold.read_holders(build_hold.holder_directory())
            if isinstance(holds, build_hold.NoHolders):
                return Free()
            holder = holds.holders[0]
            end = (
                ReleaseEstimated(holder.release.at)
                if isinstance(holder.release, build_hold.KnownReleaseEta)
                else UntilReleased()
            )
            return Held(holder.name, holder.purpose, holder.since, end)
        paths = mac_test.state_paths()
        run = mac_test.read_run(paths.run)
        block = mac_test.read_claim_block(paths)
        if not isinstance(block, mac_test.NoMacBlock):
            block_end = (
                ExpiresAt(block.expires)
                if block.expires > utc_now()
                else AwaitingBlockRemoval()
            )
            return Held(block.holder, block.reason, block.since, block_end)
        if isinstance(run, mac_test.NoRun) or not mac_test.run_is_live(run):
            return Free()
        since = mac_test.aware_timestamp(run["since"], paths.run, "since")
        return Running(run["what"], since)
    except (OSError, UnicodeError, mac_test.StateReadError) as error:
        return StateUnreadable(str(error))
