"""Describe a failed command without losing either output stream."""

import subprocess


def failure_detail(result: subprocess.CompletedProcess[str], *, lines: int = 20) -> str:
    """Return labelled tails of stderr and stdout, or the exit code if empty."""
    if lines < 1:
        raise ValueError("lines must be positive")
    details: list[str] = []
    for label, output in (("stderr", result.stderr), ("stdout", result.stdout)):
        output = output.strip()
        if output:
            tail = "\n".join(output.splitlines()[-lines:])
            details.append(f"{label}: {tail}")
    return "\n".join(details) or f"exit {result.returncode}"
