"""Read the interpreter floor from the real finder for test fixtures."""

from pathlib import Path
import re
import sys
from typing import cast


_FINDER = Path(__file__).with_name("py")


def floor(path: Path = _FINDER) -> tuple[int, int]:
    """Require one integer assignment for each version component."""
    text = path.read_text(encoding="utf-8")
    values: dict[str, int] = {}
    for key in ("NEED_MAJOR", "NEED_MINOR"):
        matches = cast(list[str], re.findall(rf"^{key}=(.*)$", text, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"{path}: expected exactly one {key}=<integer> line")
        assignment = re.fullmatch(r"([0-9]+)(?:[ \t]+#.*|[ \t]*)", matches[0])
        if assignment is None:
            raise ValueError(f"{path}: expected exactly one {key}=<integer> line")
        values[key] = int(assignment[1])
    return values["NEED_MAJOR"], values["NEED_MINOR"]


def cache_file_name() -> str:
    """Use the finder's version floor for the cache name."""
    major, minor = floor()
    return f"python3-{major}.{minor}"


def seed_interpreter_cache(cache_home: Path, interpreter: Path | str = sys.executable) -> None:
    """Seed the launcher cache with the fixture interpreter."""
    cache = cache_home / "claude" / cache_file_name()
    cache.parent.mkdir(parents=True, exist_ok=True)
    _ = cache.write_text(str(interpreter) + "\n", encoding="utf-8")


def interpreter_cache_environment(cache_home: Path) -> dict[str, str]:
    """Select a private cache with the fixture interpreter."""
    seed_interpreter_cache(cache_home)
    return {"XDG_CACHE_HOME": str(cache_home)}
