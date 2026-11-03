"""Find an agent executable without loading its launch shell."""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal


def resolve_program(tool: Literal["claude", "codex"], *, environment: Mapping[str, str] = os.environ) -> str:
    """Find the agent itself, without loading the launch shell a second time."""
    installed = Path.home() / ".local" / "bin" / tool
    directories = environment.get("PATH", os.defpath).split(os.pathsep)
    for directory in [*directories, str(installed.parent)]:
        found = shutil.which(tool, path=directory or os.curdir)
        if not found:
            continue
        program = Path(found)
        # Shell launchers invoke the shared helper. Agent scripts must still work.
        try:
            with program.open("rb") as source:
                if source.read(2) == b"#!":
                    script = source.read()
                    if b"nix_env.py" in script or b"lib.nix_env" in script:
                        continue
        except OSError:
            # An executable does not need read permission to run.
            pass
        return str(program.absolute())
    raise FileNotFoundError(f"{tool} program not found (looked in PATH and {installed})")


