"""Enable child collection on request, then preserve the next site customization."""
from __future__ import annotations

import os
import sys


def start_child_collection() -> None:
    """Load the collector by path without exposing its folder to module imports."""
    from collections.abc import Callable
    import importlib.util
    from pathlib import Path
    from typing import cast

    path = Path(__file__).resolve().parent.parent / "line_collector.py"
    spec = importlib.util.spec_from_file_location("_natest_line_collector", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load the line collector: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cast(Callable[[], None], module.start_child)()


def next_sitecustomize() -> None:
    """Load the next site customization once, including the Nix Python setup."""
    from importlib.machinery import PathFinder
    from importlib.util import module_from_spec

    own = os.path.realpath(os.path.dirname(__file__))
    remaining: list[str] = []
    passed_own = False
    for entry in sys.path:
        if os.path.realpath(entry) == own:
            passed_own = True
        elif passed_own:
            remaining.append(entry)
    spec = PathFinder.find_spec("sitecustomize", remaining)
    if spec is not None and spec.loader is not None:
        module = module_from_spec(spec)
        spec.loader.exec_module(module)


if os.environ.get("NATEST_COVERAGE_TEST"):
    start_child_collection()
next_sitecustomize()
