"""Load a Python file under a registered module name."""
import importlib.util
import sys
from importlib.abc import Loader
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import cast


def load_file_module(name: str, path: Path) -> ModuleType:
    spec = cast(ModuleSpec, importlib.util.spec_from_file_location(name, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    cast(Loader, spec.loader).exec_module(module)
    return module
