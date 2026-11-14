"""A test module fixture that keeps state reads off the live state folder."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import cast
from unittest import mock


def isolate_state_home() -> None:
    """Point XDG_STATE_HOME at a temporary folder for the whole test module.

    Call it from setUpModule. A test that sets only HOME still reads the live
    folder when the session sets XDG_STATE_HOME, as natedev does.
    """
    folder = unittest.enterModuleContext(tempfile.TemporaryDirectory())
    _ = cast(object, unittest.enterModuleContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": folder})))
