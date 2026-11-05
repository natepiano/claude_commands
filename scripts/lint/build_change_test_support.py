"""Private git commits for history-split replay tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
import os
import subprocess
from pathlib import Path

from scripts.lib.git_test_support import commit_repository


def index_step(path: Path, *, host: str, repository: str, repository_identity: str,
               step: str, call_id: str, started_at: datetime, arguments: list[str], sha: str) -> None:
    """Record private step evidence for a history estimate."""
    with closing(sqlite3.connect(path)) as connection, connection:
        _ = connection.execute(
            "INSERT INTO steps(host,repo,repo_identity,step,started_at,call_id,argv,crates_compiled,sha) "
            + "VALUES (?,?,?,?,?,?,?,?,?)",
            (host, repository, repository_identity, step, started_at.isoformat(),
             call_id, json.dumps(arguments), 1, sha))


class BuildChangeRepository:
    """A change has one parent and one later commit in a private repository."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path
        path.mkdir()
        self.environment: dict[str, str] = {
            **os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "History fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "History fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        _ = self.git("init", "-q")
        _ = self.git("remote", "add", "origin", "https://example.org/nate/hana")
        self.before: str = self.commit("before the change")
        self.change: str = self.commit("the build change")
        self.after: str = self.commit("after the change")

    def git(self, *arguments: str, input: str | None = None) -> str:
        return subprocess.run(["git", "-C", str(self.path), *arguments], input=input,
                              capture_output=True, text=True, check=True,
                              env=self.environment, timeout=10).stdout.strip()

    def commit(self, message: str) -> str:
        return commit_repository(self.path, message, allow_empty=True, env=self.environment)

    def ambiguous_commit_prefix(self) -> str:
        """Write two valid commits with the same four-character object prefix."""
        tree = self.git("rev-parse", "HEAD^{tree}")
        objects: dict[str, str] = {}
        for counter in range(10000):
            body = (f"tree {tree}\nparent {self.before}\n"
                    + "author Fixture <fixture@example.invalid> 1 +0000\n"
                    + "committer Fixture <fixture@example.invalid> 1 +0000\n\n"
                    + f"collision fixture {counter}\n")
            encoded = body.encode()
            digest = hashlib.sha1(f"commit {len(encoded)}\0".encode() + encoded).hexdigest()
            prefix = digest[:4]
            if prefix in objects:
                _ = self.git("hash-object", "-t", "commit", "-w", "--stdin", input=objects[prefix])
                _ = self.git("hash-object", "-t", "commit", "-w", "--stdin", input=body)
                return prefix
            objects[prefix] = body
        raise AssertionError("the private commit fixture has no prefix collision")
