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
    "UNIT_ACTIVE_DIR",
    # The same folder under its name before the rename, which unit_runs.environ still reads.
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


def is_installed_file(script: Path, relative: str, home: Path) -> bool:
    try:
        return os.path.samefile(script, home / ".claude" / "scripts" / relative)
    except OSError:
        return False


def require_checkout(script: Path) -> None:
    home = user_home()
    installed_root = home / ".claude" / "scripts"
    try:
        relative = script.resolve().relative_to(installed_root.resolve())
    except ValueError:
        return
    if is_installed_file(script, str(relative), home):
        raise RuntimeError(
            f"{script.name}: this suite runs from a checkout, not from the installed scripts under "
            + f"{installed_root}; run it from a worktree."
        )


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
    if not is_installed_file(script, relative, home):
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
