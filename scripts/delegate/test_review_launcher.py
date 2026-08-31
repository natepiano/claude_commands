#!/usr/bin/env python3
"""Integration tests for review.sh's pass recording around the ready sentinel."""

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

# The wrapper calls this as: <task> readonly <working_dir> <prompt> <findings> <log>.
# Writing both outputs and exiting immediately drives the wrapper's completion
# branch without a model call.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
printf 'No findings.\\n' > "$5"
printf 'stub agent log\\n' > "$6"
"""

class OpenPass(TypedDict):
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
# The recorder keys the open passes by the team slot that opened each one, so
# the value is a map even when a single launcher is running.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


AGENTS_REGISTRY = """[assignments]
delegate=codex

[delegate.codex]
implementation=gpt-called:xhigh
review=gpt-blind:max

[codex.agents]
gpt-called=low,medium,high,xhigh,max
gpt-blind=low,medium,high,xhigh,max
"""


class ReviewLauncherPassTests(unittest.TestCase):
    """The early-launch contract: before the sentinel, no pass is recorded.

    The reviewer starts while the writer is still working, so the
    implementation pass is open the whole time it runs. A start-pass before the
    sentinel files a review pass for a verdict that cannot have read the final
    diff, and does it under the reviewer's own slot, where nothing later
    reconciles it against the diff it never saw.
    """

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    review_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    prompt_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.working_dir.mkdir()
        _ = subprocess.run(
            ["git", "init", "-b", "feature/review"],
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
        self.prompt_file = self.root / "review_prompt.md"
        _ = self.prompt_file.write_text("Review the diff.\n", encoding="utf-8")
        self.review_script = self.build_script_tree()

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model.

        agent_exec.sh is the only sibling replaced: the wrapper resolves every
        helper relative to its own directory, and the rest must stay real for
        the test to exercise the shipped resolution and recording paths.
        """
        delegate = self.root / "scripts" / "delegate"
        agents = self.root / "scripts" / "agents"
        delegate.mkdir(parents=True)
        agents.mkdir(parents=True)
        for name in ("review.sh", "progress_history.py", "board.sh"):
            _ = shutil.copy2(DELEGATE_DIR / name, delegate / name)
        for name in ("agents_config.sh", "heartbeat.sh", "heartbeat_watch.sh"):
            _ = shutil.copy2(AGENTS_DIR / name, agents / name)
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        return delegate / "review.sh"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.root / ".claude" / "config" / "agents.conf")
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

    def start_implementation(self, name: str) -> Path:
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
        self.recorder(
            "start-pass", "--session-dir", str(session_dir),
            "--pass-kind", "impl", "--activity", "writing the retry path",
            "--called-task", "delegate.implementation", "--called-family", "codex",
            "--called-model", "gpt-called", "--called-effort", "xhigh",
        )
        return session_dir

    def review_command(self, session_dir: Path, lens: str, ready_file: str) -> list[str]:
        return [
            "bash", str(self.review_script), str(session_dir), str(self.working_dir),
            str(self.prompt_file), "review", "blind review", "reviewing the diff",
            "1", lens, ready_file,
        ]

    def run_review(self, session_dir: Path, ready_file: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.review_command(session_dir, "", str(ready_file)),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

    def events(self, name: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def pass_events(self, name: str) -> list[dict[str, object]]:
        return [
            event
            for event in self.events(name)
            if str(event.get("event_type", "")).startswith("pass_")
        ]

    def test_a_reviewer_that_finishes_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("early")
        ready = session_dir / "final_diff_1.ready"
        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "review_status").read_text().strip(), "reviewed")

        recorded = self.pass_events("early")
        self.assertEqual(
            [event["event_type"] for event in recorded],
            ["pass_started"],
            "the implementation pass must be the only pass on the record",
        )
        self.assertEqual(recorded[0].get("pass_kind"), "impl")

        state = cast(
            ProgressState,
            json.loads((session_dir / "progress_history_state.json").read_text()),
        )
        open_passes = state["pass"]
        self.assertEqual(len(open_passes), 1)
        open_pass = next(iter(open_passes.values()))
        self.assertEqual(open_pass["kind"], "impl")
        self.assertEqual(open_pass["status"], "active")

    def test_a_reviewer_records_its_pass_once_the_sentinel_appears(self) -> None:
        session_dir = self.start_implementation("delivered")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        ready = session_dir / "final_diff_1.ready"
        _ = (session_dir / "final_diff_1.diff").write_text("diff\n", encoding="utf-8")
        _ = ready.write_text("", encoding="utf-8")

        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)

        kinds = [
            (event["event_type"], event.get("pass_kind"))
            for event in self.pass_events("delivered")
        ]
        self.assertEqual(
            kinds,
            [
                ("pass_started", "impl"),
                ("pass_finished", "impl"),
                ("pass_started", "review"),
                ("pass_finished", "review"),
            ],
        )

    def test_three_lenses_run_at_once_without_overwriting_each_other(self) -> None:
        """The whole reason the lens exists: three reviewers, one session dir.

        Unsuffixed artifacts and a shared pass slot made the three reviewers of
        a broad review destroy each other's work -- the last launched owned
        every file, and each start-pass closed the one before it as interrupted,
        leaving the ledger describing whichever happened to finish last.
        """
        session_dir = self.start_implementation("lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        lenses = ("adversary", "conformance", "reach")

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in lenses
        ]
        for process in running:
            _, errors = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, errors)

        for lens in lenses:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        recorded = [
            (event["event_type"], event.get("pass_kind"), event.get("team_slot"))
            for event in self.pass_events("lenses")
        ]
        self.assertEqual(
            sorted(entry for entry in recorded if entry[1] == "review"),
            sorted(
                [
                    ("pass_started", "review", slot)
                    for slot in ("review", "impl", "test")
                ]
                + [
                    ("pass_finished", "review", slot)
                    for slot in ("review", "impl", "test")
                ]
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("pass_status")) for event in self.pass_events("lenses")],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for slot in ("impl", "test", "review"):
            self.assertIn(f"[{slot}] register:", board)

    def test_the_wrapper_names_a_verdict_it_did_not_record(self) -> None:
        session_dir = self.start_implementation("void")
        result = self.run_review(session_dir, session_dir / "final_diff_1.ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)
        self.assertIn("verdict void", heartbeat)


if __name__ == "__main__":
    _ = unittest.main()
