"""Adopt a store module that this package's checkout loaded through its bare name."""

import sys
from pathlib import Path


def _adopt_bare_store() -> None:
    module = sys.modules.get("store")
    if module is None or module.__file__ is None:
        return
    if Path(module.__file__).resolve().parent != Path(__file__).resolve().parent:
        return
    sys.modules["scripts.buildlog.store"] = module
    setattr(sys.modules[__name__], "store", module)


_adopt_bare_store()
