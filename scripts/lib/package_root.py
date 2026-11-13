"""Select the process checkout through the shared startup helper."""

import os as _os
import sys as _sys
from types import ModuleType as _ModuleType

_helper = _sys.modules.get("_devtools_python_startup")
if _helper is None:
    _startup = _os.path.join(_os.path.dirname(__file__), "python_startup/sitecustomize.py")
    _helper = _ModuleType("_devtools_python_startup")
    _helper.__file__ = _startup
    _sys.modules[_helper.__name__] = _helper
    with open(_startup, encoding="utf-8") as _source:
        exec(compile(_source.read(), _startup, "exec"), _helper.__dict__)
_helper.__dict__["select_checkout"](_sys.argv)
