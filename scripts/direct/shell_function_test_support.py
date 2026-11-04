"""Extract real shell helpers for isolated invocation tests."""

from pathlib import Path
import re


def shell_functions(path: Path, *names: str) -> str:
    """Return top-level Bash definitions in the requested dependency order."""
    if not names:
        raise ValueError("At least one Bash function name is required")
    source = path.read_text(encoding="utf-8")
    definitions: list[str] = []
    for name in names:
        match = re.search(rf"^{re.escape(name)}\(\) [{{(]\n.*?^[}})]$", source,
                          re.MULTILINE | re.DOTALL)
        if match is None:
            raise ValueError(f"Bash function {name!r} not found in {path}")
        definitions.append(match.group() + "\n")
    return "".join(definitions)
