"""Keep one store module across the import paths that callers use."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from scripts.buildlog.test_index import use_test_log

use_test_log()


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

CHECK_SINGLE_STORE = '''
store_path = root / "scripts" / "buildlog" / "store.py"
copies = {}
for name, module in tuple(sys.modules.items()):
    filename = getattr(module, "__file__", None)
    if isinstance(filename, str) and Path(filename).resolve() == store_path:
        copies[name] = module
check.assertEqual(len({id(module) for module in copies.values()}), 1, copies)
'''

CHECK_IDENTITY = '''
check.assertIs(sys.modules["store"], sys.modules["scripts.buildlog.store"])
''' + CHECK_SINGLE_STORE


class StoreIdentityTests(unittest.TestCase):
    def check_imports(
        self, directory: str, imports: str, *, cwd: Path = REPOSITORY_ROOT,
        checks: str = CHECK_IDENTITY,
    ) -> None:
        source = '''
import sys, unittest
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts' / sys.argv[2]))
check = unittest.TestCase()
''' + imports + checks
        result = subprocess.run(
            [str(REPOSITORY_ROOT / "scripts/lib/py"), "-c", source, str(REPOSITORY_ROOT), directory], cwd=cwd,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_import_then_package_import_share_one_store(self) -> None:
        self.check_imports("buildlog", '''
import store as bare
check.assertNotIn("scripts.buildlog", sys.modules)
from scripts.buildlog import store as packaged
check.assertIs(bare, packaged)
''')

    def test_bare_import_then_dotted_import_sets_the_package_store_attribute(self) -> None:
        self.check_imports("buildlog", '''
import store
check.assertNotIn("scripts.buildlog", sys.modules)
import scripts.buildlog.store
check.assertIs(scripts.buildlog.store, sys.modules["store"])
''')

    def test_package_import_then_bare_import_share_one_store(self) -> None:
        self.check_imports("buildlog", '''
check.assertNotIn("scripts.buildlog", sys.modules)
from scripts.buildlog import store as packaged
import store as bare
check.assertIs(packaged, bare)
''')

    def test_sweep_import_uses_one_store_across_its_callers(self) -> None:
        self.check_imports("lint", '''
check.assertNotIn("scripts.buildlog", sys.modules)
import sweep
check.assertIs(sweep.store, sys.modules["store"])
''')

    def test_index_import_outside_the_repository_preserves_store_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="store-index-") as directory:
            self.check_imports("buildlog", '''
import index
check.assertIn("scripts.buildlog", sys.modules)
import scripts.buildlog.store
check.assertIs(scripts.buildlog.store, sys.modules["store"])
''', cwd=Path(directory))

    def test_bare_store_ignores_an_unrelated_scripts_package_and_preserves_sys_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="store-unrelated-") as directory:
            package = Path(directory) / "scripts"
            package.mkdir()
            _ = (package / "__init__.py").write_text('raise RuntimeError("unrelated scripts package imported")\n')
            self.check_imports("buildlog", '''
path_before_import = sys.path.copy()
import store
check.assertIs(store, sys.modules["store"])
check.assertNotIn("scripts.buildlog.store", sys.modules)
check.assertNotIn("scripts.buildlog", sys.modules)
check.assertNotIn("scripts", sys.modules)
check.assertEqual(sys.path, path_before_import)
''', cwd=Path(directory), checks=CHECK_SINGLE_STORE)


if __name__ == "__main__":
    _ = unittest.main()
