"""Add the checkout root after the entry script's folder."""

import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.realpath(__file__))))
if _os.path.realpath(_sys.path[0]) != _root:
    _sys.path[1:] = [_root, *(_entry for _entry in _sys.path[1:] if _os.path.realpath(_entry) != _root)]
