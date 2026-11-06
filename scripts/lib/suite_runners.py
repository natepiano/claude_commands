"""Identify suite runner entry points without loading their code."""
from collections.abc import Sequence
from pathlib import Path


SUITE_RUNNERS = frozenset({"merge_gate.py", "targeted_test.py"})
SUITE_MODULES = frozenset(("-m", f"scripts.production.{Path(name).stem}") for name in SUITE_RUNNERS)


def starts_suite_runner(target: Sequence[str]) -> bool:
    return tuple(target[:2]) in SUITE_MODULES or bool(target) and Path(target[0]).name in SUITE_RUNNERS
