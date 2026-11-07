"""Add the checkout root after the entry script's folder."""

import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.realpath(__file__))))
if not any(_os.path.realpath(_entry) == _root for _entry in _sys.path):
    _sys.path.insert(1, _root)
