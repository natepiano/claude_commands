#!/usr/bin/env python3
"""Installed front-end fixtures for the hook wrapper suite."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import (
    ClassVar,
    cast,
    override,
)

SCRIPTS_ROOT = Path.home() / ".claude/scripts"
BERTH_ROOT = SCRIPTS_ROOT / "berth"
INSTALLED_DIRECTORY = Path.home() / ".cargo/bin"
INSTALLED_BINARY = INSTALLED_DIRECTORY / "cargo-berth"
PRE_EDIT_HOOK = BERTH_ROOT / "install/hooks/berth_pre_edit.sh"
POST_BASH_HOOK = BERTH_ROOT / "install/hooks/berth_post_bash.sh"
SESSION_START_HOOK = BERTH_ROOT / "install/hooks/berth_session_start.sh"
INSTALL_SCRIPT = BERTH_ROOT / "install/install.sh"
INSTALLED_FRONT_END_ARTIFACTS = (
    INSTALLED_BINARY,
    PRE_EDIT_HOOK,
    POST_BASH_HOOK,
    SESSION_START_HOOK,
)

sys.path.insert(0, str(SCRIPTS_ROOT))


def installed_front_end_artifact_digests() -> dict[str, str]:
    """Fingerprint the globally installed engine and the three hook wrappers.

    These four files are the whole installed front end. An assertion about the
    wrappers is only ever a statement about the pair that produced it, so the
    digests are taken once per class and travel with the run.
    """

    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in INSTALLED_FRONT_END_ARTIFACTS
    }


@dataclass(frozen=True)
class HookInvocation:
    """One completed hook plus every cargo-berth argv the hook issued."""

    process: subprocess.CompletedProcess[str]
    cargo_berth_calls: list[list[str]]


@dataclass
class ScratchPostToolUseRepository:
    """Own one real repository the installed hook can be pointed at."""

    temporary_directory: tempfile.TemporaryDirectory[str]
    repository_root: Path

    def cleanup(self) -> None:
        """Remove the independently constructed scratch repository."""

        self.temporary_directory.cleanup()


class InstalledFrontEndFixture(unittest.TestCase):
    """Build repositories and drive the installed wrappers and engine.

    This carries no test of its own. It holds one definition of the installed
    front end so a suite asserting wrapper behavior does not have to restate it.
    """

    base_environment: ClassVar[dict[str, str]] = {}
    installed_artifact_digests: ClassVar[dict[str, str]] = {}

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        scratch_root = Path("/tmp/claude")
        scratch_root.mkdir(parents=True, exist_ok=True)
        self.temporary_directory: tempfile.TemporaryDirectory[str] = (
            tempfile.TemporaryDirectory(
                prefix="cargo-berth-hook-rendering.", dir=scratch_root
            )
        )
        self.fixture_root: Path = Path(self.temporary_directory.name)
        self.repository_root: Path = self.fixture_root / "repository"
        self.edit_path: Path = self.repository_root / "source file.rs"

    @classmethod
    @override
    def setUpClass(cls) -> None:
        unavailable_artifacts = [
            path
            for path in INSTALLED_FRONT_END_ARTIFACTS
            if not path.is_file() or not os.access(path, os.R_OK)
        ]
        if unavailable_artifacts:
            raise RuntimeError(
                f"this suite never installs anything; run `{INSTALL_SCRIPT} /path/to/cargo-liner` separately before testing (unavailable: {unavailable_artifacts!r})"
            )
        if not INSTALLED_BINARY.is_file() or not os.access(INSTALLED_BINARY, os.X_OK):
            raise RuntimeError(
                "the explicitly installed cargo-berth binary is not executable"
            )
        cls.base_environment = os.environ.copy()
        cls.base_environment["PATH"] = os.pathsep.join(
            [str(INSTALLED_DIRECTORY), cls.base_environment.get("PATH", "")]
        )
        selected_binary = shutil.which(
            "cargo-berth", path=cls.base_environment["PATH"]
        )
        if selected_binary != str(INSTALLED_BINARY):
            raise RuntimeError(
                f"fixture PATH did not select the explicitly installed cargo-berth binary: selected {selected_binary!r}, expected {str(INSTALLED_BINARY)!r}"
            )
        cls.installed_artifact_digests = installed_front_end_artifact_digests()

    @override
    def setUp(self) -> None:
        self.repository_root.mkdir()
        (self.repository_root / ".git").mkdir()

    @override
    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_installed_engine_hook(
        self,
        fixture: ScratchPostToolUseRepository,
        hook: Path = POST_BASH_HOOK,
        session_id: str = "fixture-session",
    ) -> HookInvocation:
        """Run the canonical hook with a forwarding engine that records its argv."""

        fixture_bin = Path(
            tempfile.mkdtemp(
                prefix="cargo-berth-hook-trace.",
                dir=fixture.temporary_directory.name,
            )
        )
        cargo_call_log = fixture_bin / "cargo-calls.txt"
        cargo_wrapper = fixture_bin / "cargo-berth"
        _ = cargo_wrapper.write_text(
            self.forwarding_cargo_berth_wrapper(cargo_call_log),
            encoding="utf-8",
        )
        cargo_wrapper.chmod(0o755)

        hook_environment = self.base_environment.copy()
        hook_environment.update(
            {
                "CARGO_BERTH_TEST_BINARY": str(INSTALLED_BINARY),
                "CARGO_BERTH_TEST_CARGO_CALL_LOG": str(cargo_call_log),
                "PATH": os.pathsep.join(
                    [str(fixture_bin), self.base_environment["PATH"]]
                ),
            }
        )
        completed = subprocess.run(
            [str(hook)],
            cwd=fixture.repository_root,
            env=hook_environment,
            input=json.dumps(
                self.post_bash_payload_for(fixture.repository_root, session_id)
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        return HookInvocation(
            process=completed,
            cargo_berth_calls=self.read_delimited_calls(cargo_call_log),
        )

    def forwarding_cargo_berth_wrapper(self, call_log: Path) -> str:
        """Return a wrapper that records its argv and execs the real engine."""

        return f"""#!/bin/sh
set -eu
printf '%s' \"${{1-}}\" >> {shlex.quote(str(call_log))}
command_name=${{1-}}
shift || true
printf '\\t%s' \"$@\" >> {shlex.quote(str(call_log))}
printf '\\n' >> {shlex.quote(str(call_log))}
exec \"$CARGO_BERTH_TEST_BINARY\" \"$command_name\" \"$@\"
"""

    @staticmethod
    def read_delimited_calls(path: Path) -> list[list[str]]:
        """Read tab-delimited argv records emitted by the forwarding wrapper."""

        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text().splitlines()]

    def pre_edit_payload(self) -> dict[str, object]:
        return {
            "tool_name": "Edit",
            "session_id": "fixture-session",
            "cwd": str(self.repository_root),
            "tool_input": {"file_path": str(self.edit_path)},
        }

    def post_bash_payload(self) -> dict[str, object]:
        return {
            "tool_name": "Bash",
            "session_id": "fixture-session",
            "cwd": str(self.repository_root),
            "tool_input": {"command": "true"},
        }

    def session_start_payload(self) -> dict[str, object]:
        return {"session_id": "fixture-session", "cwd": str(self.repository_root)}

    def post_bash_payload_for(
        self,
        repository_root: Path,
        session_id: str = "fixture-session",
    ) -> dict[str, object]:
        """Build a PostToolUse payload naming one scratch repository."""

        return {
            "tool_name": "Bash",
            "session_id": session_id,
            "cwd": str(repository_root),
            "tool_input": {"command": "true"},
        }

    def git_command(self, repository_root: Path, arguments: list[str]) -> str:
        """Run git in a scratch repository and return trimmed standard output."""

        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            env=self.base_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"git {' '.join(arguments)} failed: {completed.stderr}",
        )
        return completed.stdout.strip()

    def engine_command(
        self,
        repository_root: Path,
        arguments: list[str],
        expected_exit_codes: tuple[int, ...] = (0,),
        session_id: str = "",
        coordination_run_id: str = "",
    ) -> dict[str, object]:
        """Run the installed engine against one scratch repository."""

        environment = self.base_environment.copy()
        for variable in (
            "CARGO_BERTH_BYPASS",
            "CARGO_BERTH_POST_COMMIT",
            "CARGO_BERTH_RUN",
            "CARGO_BERTH_SESSION_ID",
        ):
            _ = environment.pop(variable, None)
        if session_id:
            environment["CARGO_BERTH_SESSION_ID"] = session_id
        if coordination_run_id:
            environment["CARGO_BERTH_RUN"] = coordination_run_id
        completed = subprocess.run(
            [str(INSTALLED_BINARY), *arguments],
            cwd=repository_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn(
            completed.returncode,
            expected_exit_codes,
            f"cargo-berth {' '.join(arguments)} failed: {completed.stdout}\n{completed.stderr}",
        )
        decoded = cast(object, json.loads(completed.stdout))
        self.assertIsInstance(decoded, dict)
        return cast(dict[str, object], decoded)

    def new_scratch_post_tool_use_repository(
        self, initial_commit_count: int
    ) -> ScratchPostToolUseRepository:
        """Construct an initialized repository below `/tmp/claude`."""

        Path("/tmp/claude").mkdir(parents=True, exist_ok=True)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="cargo-berth-post-tool-use.", dir="/tmp/claude"
        )
        fixture_root = Path(temporary_directory.name)
        repository_root = fixture_root / "repository"
        repository_root.mkdir()
        _ = (repository_root / "src").mkdir()
        _ = (repository_root / "src/lib.rs").write_text(
            "pub fn base() {}\n", encoding="utf-8"
        )
        _ = self.git_command(
            repository_root, ["init", "--quiet", "--initial-branch", "main"]
        )
        _ = self.git_command(
            repository_root, ["config", "user.email", "test@example.invalid"]
        )
        _ = self.git_command(
            repository_root, ["config", "user.name", "Berth Fixture"]
        )
        _ = self.git_command(repository_root, ["add", "src/lib.rs"])
        _ = self.git_command(
            repository_root, ["commit", "--quiet", "-m", "initial"]
        )
        _ = self.engine_command(repository_root, ["init", "--json"])
        _ = self.git_command(
            repository_root, ["add", ".claude/config/berth.toml"]
        )
        _ = self.git_command(
            repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "-m",
                "configure berth",
            ],
        )
        for commit_index in range(initial_commit_count - 2):
            _ = self.git_command(
                repository_root,
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    f"fixture history {commit_index}",
                ],
            )
        return ScratchPostToolUseRepository(
            temporary_directory=temporary_directory,
            repository_root=repository_root,
        )

    @staticmethod
    def rendered_post_tool_use_detail(rendered: str) -> str:
        """Read the hook's additional context from one JSON response."""

        if not rendered:
            return ""
        decoded = cast(dict[str, object], json.loads(rendered))
        hook_output = cast(dict[str, object], decoded["hookSpecificOutput"])
        return cast(str, hook_output["additionalContext"])
