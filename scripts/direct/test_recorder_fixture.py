"""Check the shared environment for launcher test children."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct.recorder_fixture import launcher_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class LauncherEnvironmentTests(unittest.TestCase):
    def test_child_receives_no_inherited_herdr_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "HERDR_PANE_ID": "w11:p5", "HERDR_BIN": "/real/herdr",
            "HERDR_FUTURE_SETTING": "live", "TEST_CHILD_KEPT": "kept",
        }):
            result = subprocess.run(
                [sys.executable, "-c", """import json, os
print(json.dumps({
    'herdr': {name: value for name, value in os.environ.items() if name.startswith('HERDR_')},
    'kept': os.environ['TEST_CHILD_KEPT'],
}))
"""],
                env=launcher_environment(Path(directory)), capture_output=True, text=True,
                check=True, timeout=TEST_HANG_GUARD_SECS,
            )
        self.assertEqual(cast(object, json.loads(result.stdout)), {"herdr": {}, "kept": "kept"})


if __name__ == "__main__":
    _ = unittest.main()
