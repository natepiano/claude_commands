"""Load the build log store under one package name."""

from pathlib import Path
import os
import subprocess
import unittest

from scripts.buildlog.test_index import use_test_log

use_test_log()

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class StoreIdentityTests(unittest.TestCase):
    def test_package_import_loads_one_store_without_a_bare_alias(self) -> None:
        source = '''
import sys, unittest
from pathlib import Path
from scripts.buildlog import store
import scripts.buildlog.store
check = unittest.TestCase()
check.assertIs(store, scripts.buildlog.store)
check.assertNotIn("store", sys.modules)
store_path = Path(store.__file__).resolve()
copies = {name: module for name, module in tuple(sys.modules.items())
          if isinstance(getattr(module, "__file__", None), str)
          and Path(module.__file__).resolve() == store_path}
check.assertEqual(copies, {"scripts.buildlog.store": store})
'''
        result = subprocess.run(
            [str(REPOSITORY_ROOT / "scripts/lib/py"), "-c", source], cwd=REPOSITORY_ROOT,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
