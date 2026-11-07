"""Read the configured targets without loading coordination state readers."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from enum import Enum, auto
from pathlib import Path
import os
import json
from typing import Literal, cast


SCRIPTS = Path(__file__).resolve().parent.parent
from scripts.lib import conf


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


def is_aws_target(target: SshTarget) -> bool:
    """Identify Linux targets governed by the AWS spending guard."""
    return target.platform == "linux" and (target.name == "aws" or target.settings.name == "aws_target.conf")


Target = LocalTarget | SshTarget


ConfigError = conf.Unusable


class MissingSlotCapacity(Enum):
    REQUIRE_CONFIGURED = auto()
    SINGLE_SLOT = auto()


def slot_capacity(values: Mapping[str, str], repository: str, *,
                  missing: MissingSlotCapacity) -> int:
    """Read one positive slot capacity for claims and admission."""
    value = values.get(f"slots.{repository}", values.get("slots", ""))
    if f"slots.{repository}" not in values and "slots" not in values and missing is MissingSlotCapacity.SINGLE_SLOT:
        return 1
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ConfigError(f"slots.{repository}: expected a positive integer")
    return int(value)


def require_target_name(target: str) -> None:
    if target in (".", "..") or Path(target).name != target:
        raise ConfigError(f"invalid target name: {target}")


def ssh_target_state_directory(target: str) -> Path:
    """Return the state path without requiring target registration."""
    require_target_name(target)
    if target != "mac":
        return Path.home() / ".local/state/build-targets" / target
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return Path(configured).expanduser()


def uses_legacy_mac_layout(platform: Platform) -> bool:
    """Mac targets retain one mirror and claim per source repository name."""
    return platform == "macos"


def read_targets(path: Path | None = None) -> tuple[Target, ...]:
    config = conf.path("build_targets") if path is None else path
    values = conf.read(config).values()
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


def configured_ssh_target(target: str) -> SshTarget:
    """Resolve an SSH target before access to its coordination state."""
    for configured in read_targets():
        if configured.name == target and isinstance(configured, SshTarget):
            require_target_name(target)
            return configured
    raise ConfigError(f"unknown SSH target: {target}")


def aws_routing_enabled(settings: conf.ConfigRead) -> bool:
    """Read the external activation record before the legacy configuration value."""
    path = ssh_target_state_directory('aws') / 'routing.json'
    if path.exists():
        value = cast(object, json.loads(path.read_text()))
        if not isinstance(value, dict):
            raise ConfigError('routing.json: expected an activation record')
        record = cast(dict[str, object], value)
        if record.get('settings') == str(settings.source.resolve()):
            return record.get('enabled') is True
    return settings.text('offload') == 'on'
