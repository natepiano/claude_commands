"""Read the display names, prose names and memory-cap labels of build steps."""
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

    def memory_cap_text(self, step: str) -> str:
        program = ("the " if step in {"merge-gate", "full-test"} else "") + self.prose(step)
        return f"{program}'s memory cap"


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))
