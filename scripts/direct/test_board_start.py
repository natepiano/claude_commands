"""Board posts work with path, filename, and symlink starts."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Literal

from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


SCRIPTS = Path(__file__).resolve().parents[1]


class BoardStartTests(unittest.TestCase):
    def assert_post(self, start: Literal["path", "filename", "symlink"]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "scripts" / "direct"
            library = root / "scripts" / "lib"
            tools = root / "bin"
            for folder in (direct, library, tools):
                folder.mkdir(parents=True)
            _ = shutil.copy2(SCRIPTS / "direct" / "board.sh", direct / "board.sh")
            _ = shutil.copy2(SCRIPTS / "direct" / "file_lock.sh", direct / "file_lock.sh")
            for name in ("conf.sh", "conf_record.sh", "py"):
                _ = shutil.copy2(SCRIPTS / "lib" / name, library / name)
            commands = ["bash", "date", "mkdir", "uname"]
            if start != "path":
                commands.append("readlink")
            for command in commands:
                executable = shutil.which(command)
                if executable is None:
                    self.fail(f"fixture requires {command}")
                (tools / command).symlink_to(executable)
            cache = root / "cache"
            seed_interpreter_cache(cache)
            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "XDG_CACHE_HOME": str(cache),
                "CONF_DIR": str(root / "config"),
                "PATH": str(tools),
            }
            _ = environment.pop("CLAUDE_PYTHON", None)
            cwd = root
            script = "scripts/direct/board.sh"
            if start == "filename":
                cwd = direct
                script = "board.sh"
            elif start == "symlink":
                (tools / "board").symlink_to(direct / "board.sh")
                script = "bin/board"
            session = root / "session"
            posted = subprocess.run(
                [str(tools / "bash"), script, "post", str(session), "w7", "status", "Start works"],
                cwd=cwd, env=environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
            lines = (session / "board.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertEqual(lines[0].split("] status: ", 1)[1], "Start works")

    def test_path_with_a_slash_posts_without_readlink(self) -> None:
        self.assert_post("path")

    def test_filename_only_start_posts_to_the_board(self) -> None:
        self.assert_post("filename")

    def test_symlink_start_posts_to_the_board(self) -> None:
        self.assert_post("symlink")
