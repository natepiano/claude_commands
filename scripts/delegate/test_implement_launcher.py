#!/usr/bin/env python3
"""Integration tests for implement.sh's seat arguments.

The failure these exist for left no error anywhere. A repair round went out with
the pass kind on one seat, the other two launchers ran their agents normally,
and the ledger kept describing the round before it -- two records closed `error`
an hour earlier -- until the one live window closed and the recorder began
refusing every progress call while two agents were still working.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, override


DELEGATE_DIR = Path(__file__).parent
AGENTS_DIR = DELEGATE_DIR.parent / "agents"

# The wrapper calls this as: <task> write <working_dir> <prompt> <summary> <log>.
# Writing both outputs and exiting immediately drives the wrapper's completion
# branch without a model call.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
printf 'Wrote the retry path.\\n' > "$5"
printf 'stub agent log\\n' > "$6"
"""


class OpenPass(TypedDict):
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


AGENTS_REGISTRY = """[assignments]
delegate=codex

[delegate.codex]
impl=gpt-called:xhigh
test=gpt-called:xhigh
fix=gpt-called:xhigh
review=gpt-blind:max

[codex.agents]
gpt-called=low,medium,high,xhigh,max
gpt-blind=low,medium,high,xhigh,max
"""


class ImplementLauncherSeatTests(unittest.TestCase):
    """Every seat carries a kind, and the board line says which."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    implement_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    prompt_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.working_dir.mkdir()
        _ = subprocess.run(
            ["git", "init", "-b", "feature/seats"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.config_file = self.root / "delegate.conf"
        _ = self.config_file.write_text(
            "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=180\n", encoding="utf-8"
        )
        registry = self.root / ".claude" / "config" / "agents.conf"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(AGENTS_REGISTRY, encoding="utf-8")
        self.prompt_file = self.root / "implementation_prompt.md"
        _ = self.prompt_file.write_text("Write the retry path.\n", encoding="utf-8")
        self.implement_script = self.build_script_tree()

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model."""
        delegate = self.root / "scripts" / "delegate"
        agents = self.root / "scripts" / "agents"
        delegate.mkdir(parents=True)
        agents.mkdir(parents=True)
        for name in ("implement.sh", "progress_history.py", "board.sh"):
            _ = shutil.copy2(DELEGATE_DIR / name, delegate / name)
        for name in ("agents_config.sh", "heartbeat.sh", "heartbeat_watch.sh"):
            _ = shutil.copy2(AGENTS_DIR / name, agents / name)
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        return delegate / "implement.sh"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.root / ".claude" / "config" / "agents.conf")
        # The plain launcher, so the stub above stands in for the agent. The mesh
        # path would need a live app-server, which is a different test's subject.
        environment["PLAN_DELEGATE_CODEX_MESH"] = "0"
        environment["TZ"] = "UTC"
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        return environment

    def recorder(self, *arguments: str) -> None:
        environment = self.environment()
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
        result = subprocess.run(
            ["python3", str(DELEGATE_DIR / "progress_history.py"), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def start_phase(self, name: str) -> Path:
        session_dir = self.root / name
        session_dir.mkdir()
        plan = self.working_dir / "plan.md"
        _ = plan.write_text("## Delegation Context\n\n- **Project:** Test\n", encoding="utf-8")
        self.recorder(
            "start-run", "--session-dir", str(session_dir),
            "--working-dir", str(self.working_dir), "--plan-doc", "plan.md",
            "--main-family", "codex", "--main-model", "gpt-main",
            "--main-effort", "xhigh", "--main-session-id", f"main-{name}",
        )
        self.recorder(
            "start-phase", "--session-dir", str(session_dir),
            "--phase-id", "3", "--phase-title", "Retry handling",
        )
        return session_dir

    def launch(
        self, session_dir: Path, kind: str, slot: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), kind, "the retry path", kind,
                "writing the retry path", "0", slot,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

    def open_passes(self, session_dir: Path) -> dict[str, OpenPass]:
        stored = cast(
            "ProgressState",
            json.loads((session_dir / "progress_history_state.json").read_text(encoding="utf-8")),
        )
        return stored["pass"]

    def test_a_dispatch_with_no_pass_kind_is_refused(self) -> None:
        # What the incident actually looked like on the wire: a seat launched
        # with the sixth argument dropped. It used to run the agent and record
        # nothing; now nothing runs at all.
        session_dir = self.start_phase("dropped")
        result = subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), "test", "the retry tests", "",
                "writing the retry tests", "0", "test",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("pass_kind must be impl, test, fix, or review", result.stderr)
        # Refused before anything ran: no status file to be read as a live seat,
        # and no register line for peers to answer.
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertFalse((session_dir / "board.log").exists())

    def test_an_unknown_pass_kind_is_refused_by_the_same_check(self) -> None:
        session_dir = self.start_phase("unknown")
        result = self.launch(session_dir, "implementation", "impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("got 'implementation'", result.stderr)

    def test_every_seat_records_its_own_pass_and_stamps_its_role(self) -> None:
        session_dir = self.start_phase("team")
        for kind, slot in (("impl", "impl"), ("test", "test"), ("impl", "review")):
            result = self.launch(session_dir, kind, slot)
            self.assertEqual(result.returncode, 0, result.stderr)

        # Three records, keyed by seat rather than by what each is doing: the
        # `review` seat opened as a second writer and is still slot `review`.
        passes = self.open_passes(session_dir)
        self.assertEqual(
            {slot: record["kind"] for slot, record in passes.items()},
            {"impl": "impl", "test": "test", "review": "impl"},
        )
        self.assertEqual(
            {record["status"] for record in passes.values()}, {"completed"}
        )

        # The register line carries the role, which is what fills the progress
        # table's columns before any agent has posted, and what made the
        # incident legible from the board alone once it had gone wrong.
        register = [
            line
            for line in (session_dir / "board.log").read_text(encoding="utf-8").splitlines()
            if "register:" in line
        ]
        self.assertEqual(len(register), 3, register)
        for line, expected in zip(register, ("role=impl", "role=test", "role=impl")):
            self.assertIn(expected, line)


if __name__ == "__main__":
    _ = unittest.main()
