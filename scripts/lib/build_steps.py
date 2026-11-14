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

    def for_owner(self, step: str, unit: str, role: OwnerRole, *,
                  ci: bool = False, current_name: str = "") -> str:
        """Use the configured owner template when the owner is known."""
        fallback = current_name or self.shown(step)
        if ci:
            prefix = "owned.ci-"
        elif role is OwnerRole.SHOWRUNNER:
            prefix = "owned.showrunner-"
        elif role is OwnerRole.UNIT and unit:
            prefix = "owned."
        else:
            return fallback
        template = self.settings.get(prefix + step, self.settings.get(prefix + "default"))
        return template.format(unit=unit, step=self.shown(step)) if template is not None else fallback


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))
