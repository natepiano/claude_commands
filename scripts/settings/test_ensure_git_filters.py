"""Filter setup repairs a stop between command setup and required=true."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import cast


class EnsureGitFiltersTests(unittest.TestCase):
    def test_rerun_finishes_required_after_clean_and_smudge_were_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "scripts/settings"
            settings.mkdir(parents=True)
            script = settings / "ensure_git_filters.sh"
            _ = shutil.copy2(Path(__file__).with_name(script.name), script)
            _ = (settings / "git_filters_table.sh").write_text('''GIT_FILTERS=(fixture)
git_filters_parse() { GF_NAME=fixture; GF_CLEAN=clean; GF_SMUDGE=smudge; }
git_filters_retire() { :; }
git_filters_refresh_all() { :; }
''')
            _ = (settings / "settings_local_keys.sh").write_text('''settings_local_keys_restore() { :; }
settings_local_keys_snapshot() { :; }
''')
            bin_dir = root / "bin"
            bin_dir.mkdir()
            git = bin_dir / "git"
            _ = git.write_text(f"#!{sys.executable}\n" + '''import json, os, signal, sys
from pathlib import Path
root = Path(os.environ['FILTER_TEST_ROOT'])
args = sys.argv[3:]
if args == ['rev-parse', '--git-dir']:
    print('.git')
    raise SystemExit(0)
path = root / 'config.json'
config = json.loads(path.read_text()) if path.exists() else {}
key = args[-1] if '--get' in args else args[-2]
if '--get' in args:
    if key not in config:
        raise SystemExit(1)
    print(config[key])
else:
    config[key] = args[-1]
    path.write_text(json.dumps(config, sort_keys=True))
    if key.endswith('.smudge') and (root / 'stop').exists():
        os.kill(os.getppid(), signal.SIGTERM)
''')
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root / "home"),
                   "FILTER_TEST_ROOT": str(root)}
            (root / "stop").touch()
            stopped = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(stopped.returncode, 0)
            path = root / "config.json"
            partial = cast(dict[str, str], json.loads(path.read_text()))
            self.assertEqual(partial, {"filter.fixture.clean": "clean", "filter.fixture.smudge": "smudge"})
            (root / "stop").unlink()
            retried = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            expected = {**partial, "filter.fixture.required": "true"}
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertIn("installed fixture", retried.stdout)
            path.unlink()
            clean = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
            self.assertEqual((clean.returncode, clean.stdout, clean.stderr),
                             (retried.returncode, retried.stdout, retried.stderr))
            self.assertEqual(json.loads(path.read_text()), expected)


if __name__ == "__main__":
    _ = unittest.main()
