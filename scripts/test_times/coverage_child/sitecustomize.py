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


import runpy
from pathlib import Path

helper = Path(__file__).resolve().parents[2] / "lib/python_startup/sitecustomize.py"
startup = runpy.run_path(str(helper), run_name="_coverage_python_startup")
startup["select_checkout"](sys.argv)
if os.environ.get("NATEST_COVERAGE_TEST"):
    startup["after_startup"](start_child_collection)
startup["next_sitecustomize"](__file__)
startup["finish_startup"](__file__, sys.argv)
