"""Read complete shell functions for isolated caller tests."""

from pathlib import Path
import re


def shell_function(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) (?:(?P<brace>\{{)|\()\n.*?^(?(brace)\}}|\))",
        source,
    )
    if match is None:
        raise AssertionError(f"missing shell function: {name}")
    return match.group(0)


def shell_functions(path: Path, *names: str) -> str:
    """Return shell functions in the requested dependency order."""
    if not names:
        raise ValueError("At least one shell function name is required")
    source = path.read_text(encoding="utf-8")
    return "".join(shell_function(source, name) + "\n" for name in names)
