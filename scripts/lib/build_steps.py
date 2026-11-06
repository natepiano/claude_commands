"""Read the display names and prose names of build steps."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from scripts.lib import conf


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))
