"""Private target settings for Mac and natedev report fixtures."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..lint.memory_test_support import config_environment, write_config_fixture
else:
    from scripts.lint.memory_test_support import config_environment, write_config_fixture


def mac_natedev_environment(folder: Path) -> dict[str, str]:
    """Keep two-machine report cases independent of the shipped target list."""
    environment = config_environment(folder)
    _ = write_config_fixture(folder / "build_targets.conf", "targets=mac,natedev")
    return environment
