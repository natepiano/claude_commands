"""Read the display names and prose names of build steps."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from scripts.lib import conf
from scripts.message.unit_labels import OwnerRole


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))

    def for_owner(self, step: str, unit: str, role: OwnerRole) -> str:
        """Use the configured owner template when the owner is known."""
        if role is OwnerRole.SHOWRUNNER:
            key = f"owned.showrunner-{step}"
        elif role is OwnerRole.UNIT and unit:
            key = f"owned.{step}"
        else:
            return self.shown(step)
        template = self.settings.get(key)
        return template.format(unit=unit) if template is not None else self.shown(step)


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))
