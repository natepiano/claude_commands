"""Select one checkout for this process and preserve other startup hooks."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
from types import ModuleType


STARTUP_FILES = ("scripts/lib/py", "scripts/lib/package_root.py", "scripts/lib/python_startup/sitecustomize.py")


def checkout_root(start: str) -> str:
    """Find the checkout that contains a script or working directory."""
    folder = os.path.realpath(start)
    while folder:
        if os.path.isfile(os.path.join(folder, "scripts/lib/py")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return ""


def inherited_paths(value: str) -> list[str]:
    """Keep generic paths and remove checkout roots and startup directories."""
    return [entry for entry in value.split(os.pathsep) if value
            and not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
            and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup")]


def _selection_inputs(paths: list[str]) -> tuple[tuple[str, ...], str, bool, str]:
    try:
        directory = os.getcwd()
    except OSError:
        directory = ""
    return tuple(paths), os.environ.get("PYTHONPATH", ""), "PYTHONPATH" in os.environ, directory


def select_checkout(arguments: list[str]) -> str:
    """Select the script checkout, working checkout, or helper checkout once."""
    state = sys.modules.setdefault("_devtools_checkout", ModuleType("_devtools_checkout"))
    if not hasattr(state, "__path__"):
        state.__path__ = []
    selected = next(iter(state.__path__), "")
    if selected and state.__dict__.get("completed_selection") == (selected, _selection_inputs(sys.path)):
        return selected
    if not selected:
        script = arguments[0] if arguments else ""
        selected = checkout_root(os.path.dirname(os.path.abspath(script))) if script not in {"", "-", "-c", "-m"} else ""
        selected = selected or checkout_root(os.getcwd())
        selected = selected or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
        state.__path__ = [selected]
    paths = inherited_paths(os.environ.get("PYTHONPATH", ""))
    if paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        _ = os.environ.pop("PYTHONPATH", None)
    # Keep the entry script's folder before package imports.
    paths = [entry for entry in sys.path
             if os.path.realpath(entry) == selected
             or (not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
                 and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup"))]
    if not any(os.path.realpath(entry) == selected for entry in paths):
        script_folder = os.path.dirname(os.path.abspath(arguments[0])) if arguments else ""
        position = int(bool(paths) and os.path.realpath(paths[0]) == script_folder and not getattr(sys.flags, "safe_path", False))
        paths.insert(position, selected)
    sys.path[:] = paths
    state.__dict__["completed_selection"] = (selected, _selection_inputs(sys.path))
    return selected


def after_startup(callback: Callable[[], None]) -> None:
    """Start an observer after the outer startup hook finishes."""
    sys.modules["_devtools_checkout"].__dict__["ready"] = callback


def finish_startup(filename: str, arguments: list[str]) -> None:
    """Finish path selection before Python adds its automatic search directory."""
    hook = sys.modules.get("sitecustomize")
    if hook is None or not hook.__file__ or os.path.realpath(hook.__file__) != os.path.realpath(filename):
        return
    selected = select_checkout(arguments)
    automatic_paths = sys.path
    if not getattr(sys.flags, "safe_path", False):
        script = arguments[0] if arguments else ""
        automatic = os.getcwd() if script in {"", "-", "-c", "-m"} else os.path.dirname(os.path.abspath(script))
        if os.path.realpath(automatic) == selected:
            sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != selected]
            automatic_paths = [automatic, *sys.path]
        elif (not os.path.isdir(os.path.join(automatic, "scripts"))
              and not automatic.rstrip(os.sep).endswith(os.sep + "lib/python_startup")):
            automatic_paths = [automatic, *sys.path]
    state = sys.modules["_devtools_checkout"]
    # Python adds its automatic path after this hook returns.
    state.__dict__["completed_selection"] = (selected, _selection_inputs(automatic_paths))
    if "ready" in state.__dict__:
        from typing import cast

        ready = cast(Callable[[], None], state.__dict__.pop("ready"))
        ready()


def next_sitecustomize(filename: str, search: list[str] | None = None) -> None:
    """Run the next hook after this hook's directory."""
    directory = os.path.realpath(os.path.dirname(filename))
    remaining: list[str] = []
    passed_own = False
    for entry in sys.path if search is None else search:
        if os.path.realpath(entry) == directory:
            passed_own = True
        elif passed_own and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup"):
            # Continue past redundant copies of this startup helper.
            remaining.append(entry)
    spec = PathFinder.find_spec("sitecustomize", remaining)
    if spec is not None and spec.loader is not None:
        from typing import cast

        module = module_from_spec(spec)
        # Keep hook contexts active for the process lifetime.
        hooks = cast(list[ModuleType], sys.modules["_devtools_checkout"].__dict__.setdefault("startup_hooks", []))
        hooks.append(module)
        spec.loader.exec_module(module)


# Reuse this module when an entry loads package_root.py.
if __name__ in sys.modules:
    _ = sys.modules.setdefault("_devtools_python_startup", sys.modules[__name__])


if __name__ == "sitecustomize":
    # Save the hook order before selection removes this startup directory.
    following = sys.path[:]
    _ = select_checkout(sys.argv)
    next_sitecustomize(__file__, following)
    finish_startup(__file__, sys.argv)
