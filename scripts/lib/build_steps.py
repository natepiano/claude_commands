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


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))
