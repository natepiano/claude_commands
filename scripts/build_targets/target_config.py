"""Read the configured targets without loading coordination state readers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Literal


SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "lint"))
from config_values import config_values


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


def read_targets(path: Path | None = None) -> tuple[Target, ...]:
    default = path if path is not None else SCRIPTS.parent / "config/build_targets.conf"
    config = Path(os.environ.get("BUILD_TARGETS_CONFIG", str(default))).expanduser().resolve()
    values = config_values(str(config))
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


