"""Refuse state-directory changes when an installed script starts."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import pwd
import sys


OVERRIDE_VARIABLES: tuple[str, ...] = (
    "BUILD_HOLD_DIR",
    "BUILD_HOLD_RELEASE_DIR",
    "MAC_TEST_STATE_DIR",
    "MAC_TEST_CONFIG",
    "BUILD_TARGETS_SCRIPT",
    "BUILD_TARGETS_CONFIG",
    "BUILD_TARGETS_NOW",
    "PLAN_DELEGATE_ACTIVE_DIR",
)
REFUSED_STATUS = 2


@lru_cache(maxsize=1)
def user_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


@dataclass(frozen=True)
class OverridesAllowed:
    pass


@dataclass(frozen=True)
class OverridesRefused:
    line: str


def refusal(
    script: Path, relative: str, home: Path, environment: Mapping[str, str]
) -> OverridesAllowed | OverridesRefused:
    for name in OVERRIDE_VARIABLES:
        if name in environment:
            break
    else:
        if "HOME" not in environment or environment["HOME"] == str(home):
            return OverridesAllowed()
        try:
            if os.path.samefile(environment["HOME"], home):
                return OverridesAllowed()
        except OSError:
            pass
        name = "HOME"

    installed_root = home / ".claude" / "scripts"
    try:
        if not os.path.samefile(script, installed_root / relative):
            return OverridesAllowed()
    except OSError:
        return OverridesAllowed()
    return OverridesRefused(
        f"{script.name}: {name} is set to '{environment[name]}'; "
        + f"the installed scripts under {installed_root} do not run with it. "
        + "Unset it and run again."
    )


def refuse_overrides(script: Path, relative: str) -> None:
    # With nothing set, a call took 1.3 microseconds on natedev.
    result = refusal(script, relative, user_home(), os.environ)
    if isinstance(result, OverridesRefused):
        print(result.line, file=sys.stderr)
        raise SystemExit(REFUSED_STATUS)
