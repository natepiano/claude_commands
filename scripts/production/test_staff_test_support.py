"""Fixture workers use private account folders and remain visible to readers."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


CHECKOUT = Path(__file__).resolve().parents[2]
PREPARE = '''
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest
from unittest.mock import patch

root = Path(sys.argv[1])
family, selection = sys.argv[2:]
login_home = root / "login"
login_home.mkdir()
real_claude = login_home / (".claude" if selection == "default" else ".claude-live")
real_codex = login_home / (".codex" if selection == "default" else ".codex-live")
real_claude.mkdir()
real_codex.mkdir()
home = login_home if selection == "default" else root / "environment-home"
home.mkdir(exist_ok=True)
claude = real_claude if selection == "real" else home / "claude"
codex = real_codex if selection == "real" else home / "codex"
os.environ.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(codex),
                  STAFF_FILE=str(root / "staff.json"))
if selection == "default":
    del os.environ["CLAUDE_CONFIG_DIR"]
    del os.environ["CODEX_HOME"]
login = pwd.struct_passwd(("fixture", "x", os.getuid(), os.getgid(), "", str(login_home), "/bin/sh"))
with patch.object(pwd, "getpwuid", return_value=login):
    from scripts.production import staff_test_support as fixtures
from scripts.production import staff, session_staff
from scripts.message import sessions
case = unittest.TestCase()
CHILD_READERS = """import os, sys
from pathlib import Path
from scripts.production import staff, session_staff
from scripts.message import sessions
row = staff.by_id(staff.StaffId(sys.argv[1]))
family, folder = sys.argv[2:]
assert os.environ['CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'] == folder
assert session_staff.worker_family(staff.local_snapshot(), row.id) == family
backend = staff.backend(row)
assert backend.family == family
paths = sessions.transcript_locations(row.current_session) if family == 'claude' else list(
    (backend.codex_home / 'sessions').glob(f'{row.current_session}.jsonl'))
assert len(paths) == 1, paths
assert paths[0].is_relative_to(Path(folder)), paths
assert paths[0].read_bytes() == b''
"""
parent = fixtures.unit_membership("fixture-showrunner", "fixture-unit")
'''


class FixtureAccountTests(unittest.TestCase):
    def run_fixture(self, body: str, family: str, selection: str = "real") -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("STAFF_ROW_FD_") and key not in (
                               "STAFF_OPERATION_FD", "PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY")}
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(PREPARE) + textwrap.dedent(body),
                 temporary, family, selection],
                cwd=CHECKOUT, env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_redirects_account_folders_and_preserves_private_selections(self) -> None:
        for family in ("claude", "codex"):
            for selection in ("real", "default", "private"):
                with self.subTest(family=family, selection=selection):
                    self.run_fixture('''
                        selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                        original = claude if family == "claude" else codex
                        if selection != "private":
                            case.assertFalse(selected.is_relative_to(login_home))
                        else:
                            case.assertEqual(selected, original)
                        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(selected)
                        row = fixtures.fixture_worker("worker", parent.id, backend=backend)
                        files = list(selected.rglob(f"{row.current_session}.jsonl"))
                        case.assertEqual(len(files), 1)
                        case.assertEqual(files[0].read_bytes(), b"")
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    ''', family, selection)

    def test_account_path_aliases_redirect_before_worker_publication(self) -> None:
        for family in ("claude", "codex"):
            for alias in ("parent", "relative", "symlink", "account-symlink"):
                with self.subTest(family=family, alias=alias):
                    self.run_fixture(f"alias = {alias!r}\n" + textwrap.dedent('''
                        real_home = real_claude if family == "claude" else real_codex
                        if alias == "parent":
                            selected = login_home / ".." / login_home.name / real_home.name
                        elif alias == "relative":
                            os.chdir(root)
                            selected = Path("login") / real_home.name
                        elif alias == "symlink":
                            selected = root / "account-alias"
                            selected.symlink_to(real_home, target_is_directory=True)
                        else:
                            target = root / "account-target"
                            real_home.rename(target)
                            real_home.symlink_to(target, target_is_directory=True)
                            selected = real_home
                        variable = "CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"
                        with patch.dict(os.environ, {variable: str(selected)}):
                            backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(selected)
                            row = fixtures.fixture_worker("alias-worker", parent.id, backend=backend)
                            private_home = Path(os.environ[variable])
                            case.assertEqual(private_home.name, real_home.name)
                            case.assertFalse(private_home.is_relative_to(login_home))
                            observed = staff.backend(row)
                            case.assertEqual(observed.family, family)
                            paths = sessions.transcript_locations(row.current_session) if family == "claude" else list(
                                (observed.codex_home / "sessions").glob(f"{row.current_session}.jsonl"))
                            case.assertEqual(len(paths), 1)
                            case.assertTrue(paths[0].is_relative_to(private_home))
                            case.assertEqual(paths[0].read_bytes(), b"")
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    '''), family, "private")

    def test_claude_file_creation_updates_the_environment_only_after_redirecting(self) -> None:
        for creator in ("session", "transcript"):
            for selection in ("real", "private"):
                with self.subTest(creator=creator, selection=selection):
                    self.run_fixture(f"creator = {creator!r}\n" + textwrap.dedent('''
                        row = fixtures.fixture_worker("reader-worker", parent.id)
                        environment = {"HOME": str(login_home if selection == "real" else home),
                                       "STAFF_FILE": str(root / "staff.json")}
                        with patch.dict(os.environ, environment, clear=True):
                            before = dict(os.environ)
                            path = fixtures._fixture_session_path(row.current_session, staff.ClaudeBackend())
                            case.assertEqual(dict(os.environ), before)
                            if creator == "session":
                                fixtures.fixture_session(row.current_session, staff.ClaudeBackend())
                            else:
                                written, evidence = fixtures.transcript_fixture(row, staff.ClaudeBackend())
                                case.assertEqual(written, path)
                                case.assertIn(evidence[0][0], written.read_text())
                            if selection == "real":
                                case.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], str(path.parents[2]))
                                case.assertFalse(path.is_relative_to(login_home))
                            else:
                                case.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
                            case.assertEqual(sessions.transcript_locations(row.current_session), [path])
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    '''), "claude", selection)

    def test_late_environment_and_explicit_codex_home_redirect_before_worker_publication(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.run_fixture('''
                    environment = {"HOME": str(login_home), "STAFF_FILE": str(root / "staff.json"),
                                   "CLAUDE_CONFIG_DIR": str(real_claude), "CODEX_HOME": str(real_codex)}
                    with patch.dict(os.environ, environment, clear=True):
                        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(real_codex)
                        row = fixtures.fixture_worker("late-worker", parent.id, backend=backend)
                        selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                        case.assertFalse(selected.is_relative_to(login_home))
                        case.assertEqual(len(list(selected.rglob(f"{row.current_session}.jsonl"))), 1)
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                        case.assertEqual(session_staff.worker_family(staff.local_snapshot(), row.id), family)
                ''', family)

    def test_readers_and_child_process_find_the_parent_worker_in_the_private_folder(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.run_fixture('''
                    backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(real_codex)
                    row = fixtures.fixture_worker("visible-worker", parent.id, backend=backend)
                    selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                    case.assertFalse(selected.is_relative_to(login_home))
                    case.assertEqual(session_staff.worker_family(staff.local_snapshot(), row.id), family)
                    observed = staff.backend(row)
                    case.assertEqual(observed.family, family)
                    if family == "claude":
                        paths = sessions.transcript_locations(row.current_session)
                        case.assertEqual(len(paths), 1)
                        case.assertTrue(paths[0].is_relative_to(selected))
                    else:
                        case.assertEqual(observed.codex_home, selected)
                    child = subprocess.run([sys.executable, "-c", CHILD_READERS, row.id, family, str(selected)], capture_output=True, text=True, check=False)
                    case.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                    case.assertEqual(list(real_claude.iterdir()), [])
                    case.assertEqual(list(real_codex.iterdir()), [])
                ''', family)
