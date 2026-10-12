#!/usr/bin/env python3
"""Integration tests for review.sh's fresh and long-lived reviewer paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
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

STUB_MESH = """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import time

verb = sys.argv[1]

def option(name):
    return sys.argv[sys.argv.index(name) + 1]

session = pathlib.Path(option('--session-dir'))
roster_path = session / 'mesh_roster.json'
roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
if verb in ('start', 'compact', 'can-follow', 'follow'):
    with (session / 'mesh_events.txt').open('a') as events:
        events.write(verb + '\\n')

name = option('--name') if verb == 'start' else option('--to')
entry = roster.get(name, {})
if verb == 'start':
    entry = {
        'thread_id': 'thread-' + name,
        'turn_id': 'turn-1',
        'status': 'running',
        'role': option('--role'),
        'context_tokens': 12000,
    }
    (session / 'follow_message.txt').write_text(
        pathlib.Path(option('--prompt-file')).read_text()
    )
    pathlib.Path(option('--summary-file')).write_text('No findings.\\n')
    pathlib.Path(option('--log-file')).write_text('stub mesh log\\n')
    entry['status'] = 'done'
elif verb == 'compact':
    if (roster_path.parent / 'compact_still_live').exists():
        print('stub turn still live', file=sys.stderr)
        sys.exit(3)
    before = entry.get('context_tokens', 0)
    entry['context_tokens'] = 12000
    roster_path.write_text(json.dumps(roster))
    print(f'compacted {name}: {before} -> 12000 tokens')
elif verb == 'can-follow':
    if entry.get('status') not in ('done', 'failed'):
        print(f'codex_mesh: {name}: seat is busy or not followable; follow refused',
              file=sys.stderr)
        sys.exit(2)
    entry['previous_status'] = entry['status']
    if 'role' in entry:
        entry['previous_role'] = entry['role']
    entry['status'] = 'starting'
    entry['role'] = option('--role')
    entry['launcher_pid'] = int(option('--claim-pid'))
elif verb == 'release-follow':
    if (entry.get('status') == 'starting'
            and entry.get('launcher_pid') == int(option('--claim-pid'))):
        entry['status'] = entry.get('previous_status', 'failed')
        if 'previous_role' in entry:
            entry['role'] = entry['previous_role']
        else:
            entry.pop('role', None)
        entry.pop('launcher_pid', None)
        entry.pop('previous_status', None)
        entry.pop('previous_role', None)
elif verb == 'follow':
    if (entry.get('status') != 'starting'
            or entry.get('launcher_pid') != int(option('--claim-pid'))):
        print(f'codex_mesh: {name}: follow claim changed after dispatch', file=sys.stderr)
        sys.exit(1)
    entry['status'] = 'running'
    entry['role'] = option('--role')
    pathlib.Path(session / 'follow_message.txt').write_text(
        pathlib.Path(option('--message-file')).read_text()
    )
    finish_file = os.environ.get('STUB_REVIEW_FINISH_FILE', '')
    while finish_file and not pathlib.Path(finish_file).exists():
        time.sleep(0.02)
    edit_path = os.environ.get('STUB_REVIEW_EDIT', '')
    if edit_path:
        pathlib.Path(edit_path).write_text('changed by reviewer\\n')
    if os.environ.get('STUB_REVIEW_COMMIT', ''):
        working_dir = os.environ['STUB_REVIEW_WORKING_DIR']
        subprocess.run(
            ['git', '-c', 'user.name=Review Test', '-c',
             'user.email=review@example.com', 'add', '.'],
            cwd=working_dir, check=True,
        )
        subprocess.run(
            ['git', '-c', 'user.name=Review Test', '-c',
             'user.email=review@example.com', 'commit', '-m', 'reviewer edit'],
            cwd=working_dir, check=True,
        )
    pathlib.Path(option('--summary-file')).write_text('No findings.\\n')
    pathlib.Path(option('--log-file')).write_text('stub mesh log\\n')
    entry['status'] = 'done'
else:
    sys.exit(2)
roster[name] = entry
roster_path.write_text(json.dumps(roster))
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

[delegate.options]
codex_mesh=1
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
            "\n".join([
                "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=180",
                "PLAN_DELEGATE_COMPACT_ABOVE_TOKENS=100000",
                "",
            ]),
            encoding="utf-8",
        )
        registry = self.root / ".claude" / "config" / "agents.conf"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(AGENTS_REGISTRY, encoding="utf-8")
        self.prompt_file = self.root / "review_prompt.md"
        _ = self.prompt_file.write_text("Review the diff.\n", encoding="utf-8")
        reviewed = self.working_dir / "reviewed.txt"
        _ = reviewed.write_text("original\n", encoding="utf-8")
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "add", "reviewed.txt"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "commit", "-m", "baseline"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
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
        lib = self.root / "scripts" / "lib"
        delegate.mkdir(parents=True)
        agents.mkdir(parents=True)
        lib.mkdir(parents=True)
        # The wrapper reaches python3 through ../lib/py, resolved from its own
        # directory.
        _ = shutil.copy2(DELEGATE_DIR.parent / "lib" / "py", lib / "py")
        for name in (
            "review.sh", "implement.sh", "progress_history.py", "findings.py",
            "board.sh", "seat_name.sh",
        ):
            _ = shutil.copy2(DELEGATE_DIR / name, delegate / name)
        for name in ("agents_config.sh", "heartbeat.sh", "heartbeat_watch.sh"):
            _ = shutil.copy2(AGENTS_DIR / name, agents / name)
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        mesh = agents / "codex_mesh.py"
        _ = mesh.write_text(STUB_MESH, encoding="utf-8")
        mesh.chmod(0o755)
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

    def review_command(
        self, session_dir: Path, lens: str, ready_file: str, to: str = "",
        new_slot: str = "",
    ) -> list[str]:
        command = [
            "bash", str(self.review_script), str(session_dir), str(self.working_dir),
            str(self.prompt_file), "review", "blind review", "reviewing the diff",
            "1", lens, ready_file,
        ]
        if to:
            command[2:2] = ["--to", to]
        elif new_slot:
            command[2:2] = ["--slot", new_slot]
        return command

    def run_review(self, session_dir: Path, ready_file: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.review_command(session_dir, "", str(ready_file)),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

    def run_seated_review(
        self,
        session_dir: Path,
        seat: str,
        *,
        edit_path: str = "",
        commit_change: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment["STUB_REVIEW_WORKING_DIR"] = str(self.working_dir)
        if edit_path:
            environment["STUB_REVIEW_EDIT"] = edit_path
        if commit_change:
            environment["STUB_REVIEW_COMMIT"] = "1"
        return subprocess.run(
            self.review_command(session_dir, "", "", seat),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )

    def finish_implementation(self, session_dir: Path) -> None:
        self.recorder(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed"
        )

    def open_seat(
        self, session_dir: Path, *, status: str = "done", context_tokens: int = 0
    ) -> str:
        name = "fixture-impl"
        _ = (session_dir / "mesh_roster.json").write_text(
            json.dumps({
                name: {
                    "thread_id": "thread-fixture",
                    "turn_id": "turn-1",
                    "status": status,
                    "role": "impl",
                    "context_tokens": context_tokens,
                }
            }),
            encoding="utf-8",
        )
        return name

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

    def test_a_named_open_seat_receives_the_review_and_writes_findings(self) -> None:
        session_dir = self.start_implementation("seated")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir)

        result = self.run_seated_review(session_dir, seat)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "follow_message.txt").read_text(encoding="utf-8"),
            "Review the diff.\n",
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(roster[seat]["role"], "review")
        reviews = [
            event for event in self.pass_events("seated")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "impl"), ("pass_finished", "impl")],
        )

    def test_a_review_opened_seat_has_durable_identity_for_an_implementation_claim(self) -> None:
        session_dir = self.start_implementation("new-seat")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review2"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["start"],
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        seat = "project-review2"
        self.assertEqual(roster[seat]["status"], "done")
        self.assertEqual(roster[seat]["role"], "review")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        reviews = [
            event for event in self.pass_events("new-seat")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "review2"), ("pass_finished", "review2")],
        )
        identity = cast(
            "dict[str, object]",
            json.loads((session_dir / "impl_seat_review2.json").read_text()),
        )
        self.assertEqual(identity, {
            "name": seat,
            "slot": "review2",
            "family": "codex",
            "model": "gpt-called",
            "seat_id": f"thread-{seat}",
        })

        implementation = subprocess.run(
            [
                "bash", str(self.review_script.with_name("implement.sh")),
                "--to", seat, str(session_dir), str(self.working_dir),
                str(self.prompt_file), "implementation", "write the repair",
                "impl", "writing the repair", "0", "review2",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )
        self.assertEqual(implementation.returncode, 0, implementation.stderr)
        self.assertEqual(
            (session_dir / "impl_status_review2").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_hyphenated_new_slot_is_refused_to_keep_followup_identity_stable(self) -> None:
        session_dir = self.start_implementation("hyphenated-slot")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review-extra"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("alphanumeric/_ characters", result.stderr)
        self.assertFalse((session_dir / "mesh_roster.json").exists())

    def test_an_early_seated_review_baselines_after_the_writers_final_edit(self) -> None:
        session_dir = self.start_implementation("early-writer-edit")
        seat = self.open_seat(session_dir)
        ready = session_dir / "final_diff_1.ready"
        finish_review = session_dir / "finish_review"
        environment = self.environment()
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish_review)
        process = subprocess.Popen(
            self.review_command(session_dir, "adversary", str(ready), seat),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        _ = (self.working_dir / "reviewed.txt").write_text(
            "writer final edit\n", encoding="utf-8"
        )
        self.finish_implementation(session_dir)
        ready.touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(
                event.get("event_type") == "pass_started"
                and event.get("pass_kind") == "review"
                for event in self.pass_events("early-writer-edit")
            ):
                break
            time.sleep(0.05)
        else:
            process.kill()
            self.fail("the early review never started its delivered pass")
        finish_review.touch()
        _stdout, errors = process.communicate(timeout=120)

        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )

    def test_a_seated_early_review_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("seated-early")
        seat = self.open_seat(session_dir)
        ready = session_dir / "final_diff_1.ready"

        result = subprocess.run(
            self.review_command(session_dir, "adversary", str(ready), seat),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        recorded = self.pass_events("seated-early")
        self.assertEqual(
            [(event["event_type"], event.get("pass_kind")) for event in recorded],
            [("pass_started", "impl")],
        )
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)

    def test_a_seated_reviewer_that_edits_fails_and_names_the_seat_and_path(self) -> None:
        session_dir = self.start_implementation("edited")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir)
        edited = self.working_dir / "reviewed.txt"

        result = self.run_seated_review(session_dir, seat, edit_path=str(edited))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(seat, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        reviews = [
            event for event in self.pass_events("edited")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(reviews[-1].get("status"), "error")

    def test_a_reviewer_commit_is_detected_and_names_its_changed_path(self) -> None:
        session_dir = self.start_implementation("committed-edit")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir)

        result = self.run_seated_review(
            session_dir,
            seat,
            edit_path=str(self.working_dir / "reviewed.txt"),
            commit_change=True,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(seat, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_a_change_to_an_existing_untracked_file_is_detected(self) -> None:
        session_dir = self.start_implementation("untracked-edit")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir)
        untracked = self.working_dir / "notes.txt"
        _ = untracked.write_text("before review\n", encoding="utf-8")

        result = self.run_seated_review(
            session_dir, seat, edit_path=str(untracked)
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(seat, result.stderr)
        self.assertIn("notes.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_without_a_named_seat_the_fresh_read_only_path_is_unchanged(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session_dir / "mesh_events.txt").exists())
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_a_busy_named_seat_is_refused_before_review_artifacts_change(self) -> None:
        session_dir = self.start_implementation("busy")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir, status="running")

        result = self.run_seated_review(session_dir, seat)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"codex_mesh: {seat}: seat is busy or not followable", result.stderr)
        self.assertFalse((session_dir / "review_status").exists())
        self.assertFalse((session_dir / "review_findings_1.txt").exists())

    def test_a_large_named_seat_is_compacted_before_the_review_claim(self) -> None:
        session_dir = self.start_implementation("compact")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir, context_tokens=100001)

        result = self.run_seated_review(session_dir, seat)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact", "can-follow", "follow"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"compacted {seat}: 100001 -> 12000 tokens", board)

    def test_a_seat_left_with_a_live_turn_by_compaction_gets_no_review_follow_up(self) -> None:
        session_dir = self.start_implementation("compact-still-live")
        self.finish_implementation(session_dir)
        seat = self.open_seat(session_dir, context_tokens=100001)
        (session_dir / "compact_still_live").touch()

        result = self.run_seated_review(session_dir, seat)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the seat", result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)

    def test_both_lenses_run_at_once_without_overwriting_each_other(self) -> None:
        """The whole reason the lens exists: two reviewers, one session dir.

        Unsuffixed artifacts and a shared pass slot made the reviewers of
        a broad review destroy each other's work -- the last launched owned
        every file, and each start-pass closed the one before it as interrupted,
        leaving the ledger describing whichever happened to finish last.
        """
        session_dir = self.start_implementation("lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        lenses = ("adversary", "contract")

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
                    for slot in ("impl", "test")
                ]
                + [
                    ("pass_finished", "review", slot)
                    for slot in ("impl", "test")
                ]
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in self.pass_events("lenses")],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for slot in ("impl", "test"):
            self.assertIn(f"[{slot}] register:", board)

    def test_three_lenses_run_at_once_each_in_a_seat_of_its_own(self) -> None:
        """The craft lens joins the other two in the `review` seat.

        No writer sits there, so its pass is a third record beside the two
        lenses' and closes neither of them.
        """
        session_dir = self.start_implementation("three-lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        seats = {"adversary": "test", "contract": "impl", "craft": "review"}

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in seats
        ]
        for process in running:
            _, errors = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, errors)

        for lens in seats:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        events = self.pass_events("three-lenses")
        reviews = [event for event in events if event.get("pass_kind") == "review"]
        self.assertEqual(
            sorted((str(event["event_type"]), str(event.get("team_slot"))) for event in reviews),
            sorted(
                (event_type, slot)
                for event_type in ("pass_started", "pass_finished")
                for slot in seats.values()
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in events],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for lens, slot in seats.items():
            self.assertIn(f"[{slot}] register: {lens} review up", board)

    def test_an_unknown_lens_is_refused_before_anything_runs(self) -> None:
        session_dir = self.start_implementation("unknown-lens")
        result = subprocess.run(
            self.review_command(session_dir, "style", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=120,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown review lens 'style' (adversary, contract, craft)", result.stderr)
        self.assertFalse((session_dir / "review_status_style").exists())
        self.assertEqual(
            [event["event_type"] for event in self.pass_events("unknown-lens")],
            ["pass_started"],
        )

    def test_the_wrapper_names_a_verdict_it_did_not_record(self) -> None:
        session_dir = self.start_implementation("void")
        result = self.run_review(session_dir, session_dir / "final_diff_1.ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)
        self.assertIn("verdict void", heartbeat)


if __name__ == "__main__":
    _ = unittest.main()
