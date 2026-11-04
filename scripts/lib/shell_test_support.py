"""Read complete shell functions for isolated caller tests."""

import re


def shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}", source)
    if match is None:
        raise AssertionError(f"missing shell function: {name}")
    return match.group(0)
