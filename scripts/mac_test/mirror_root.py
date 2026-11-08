"""Validate the shared Mac mirror root."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from scripts.lib import conf


@dataclass(frozen=True)
class MacMirrorRoot:
    """A validated mirror path below the Mac home folder."""

    relative: str

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", self.relative) is None
                or any(component in (".", "..") for component in self.relative.split("/"))):
            raise conf.Unusable("mac_test.conf: mirror_root must contain valid path components below ~/")

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> MacMirrorRoot:
        """Read and validate the configured remote path once."""
        value = settings.text("mirror_root")
        if not value.startswith("~/"):
            raise conf.Unusable("mac_test.conf: mirror_root must start with ~/")
        return cls(value[2:])

    def local_path(self) -> Path:
        """Expand the validated root on the Mac."""
        return Path.home() / self.relative
