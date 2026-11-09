"""Check Claude reviews, Codex refusal, and interpreter resolution."""

from __future__ import annotations

from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file

from scripts.lib.py_floor_test_support import cache_file_name

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
from contextlib import ExitStack
from scripts.direct.local_release_wait import IntervalReleasePolling
from pathlib import Path
from typing import cast
import json
import os
import hashlib
from unittest.mock import patch
import subprocess
import shutil
import sys
import tempfile
from typing import override
from scripts.direct import wait_for_test_state
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_models_registry import RegistryReadProbe, write_registry


class ReviewLauncherPassTests(support.ReviewLauncherFixture):

    def test_review_reads_one_registry_snapshot_across_its_child_processes(self) -> None:
        session = self.start_implementation("registry-snapshot")
        self.finish_implementation(session)
        environment = self.environment()
        probe = RegistryReadProbe(self.root, Path(environment["MODELS_CONFIG_FILE"]))
        result = subprocess.run(self.review_command(session, "contract", ""),
                                env=probe.environment(environment), capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(probe.reads()), 1, probe.reads())

    def test_review_helper_keeps_the_launch_generation_after_a_registry_edit(self) -> None:
        session = self.start_implementation("review-helper-generation")
        self.finish_implementation(session)
        environment = self.environment()
        registry = Path(environment["MODELS_CONFIG_FILE"])
        _ = write_registry(registry, {"assignments": {"direct": "claude"},
            "direct.claude": {"review": "opus:high"},
            "claude.agents": {"opus": "high", "sonnet": "low"}})
        _ = (self.root / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"review@example.invalid"}}')
        backend = self.root / "bin/claude"
        backend.parent.mkdir(exist_ok=True)
        _ = backend.write_text("#!" + sys.executable + '''
import json, os, sys
from pathlib import Path
Path(os.environ["REVIEW_AGENT"]).write_text(json.dumps({"arguments": sys.argv[1:], "environment": dict(os.environ)}))
print(json.dumps({"type": "result", "result": "No findings."}))
''')
        backend.chmod(0o755)
        scripts = Path(__file__).resolve().parents[1]
        helper = self.root / "scripts/agents/agent_exec.sh"
        agent_exec = helper.with_name("agent_exec_real.sh")
        _ = shutil.copy2(scripts / "agents/agent_exec.sh", agent_exec)
        for name in ("claude_account.sh", "claude_result.py"):
            _ = shutil.copy2(scripts / "agents" / name, helper.with_name(name))
        _ = shutil.copy2(scripts / "lib/nix_env.py", self.root / "scripts/lib/nix_env.py")
        _ = helper.write_text('''#!/usr/bin/env bash
set -euo pipefail
python3 - <<'EDIT'
import os
from pathlib import Path
path = Path(os.environ["MODELS_CONFIG_FILE"])
path.write_text(os.environ["NEXT_REVIEW_REGISTRY"])
EDIT
exec bash "''' + str(agent_exec) + '''" "$@"
''')
        recorded = self.root / "review-agent.json"
        environment.update(PATH=str(backend.parent) + os.pathsep + environment["PATH"], REVIEW_AGENT=str(recorded),
            CLAUDE_CONFIG_DIR="",
            CODEX_CATALOG_SYNC_STATE_FILE=str(registry),
            NEXT_REVIEW_REGISTRY=registry.read_text().replace("review=opus:high", "review=sonnet:low"))
        probe = RegistryReadProbe(self.root, registry)
        result = subprocess.run(self.review_command(session, "contract", ""), env=probe.environment(environment),
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launched = cast(dict[str, object], json.loads(recorded.read_text()))
        arguments = cast(list[str], launched["arguments"])
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertEqual(arguments[arguments.index("--effort") + 1], "high")
        self.assertNotIn("MODELS_LAUNCH_CONFIGURATION", cast(dict[str, str], launched["environment"]))
        self.assertNotIn("MODELS_REGISTRY_EDIT_BASE", cast(dict[str, str], launched["environment"]))
        self.assertIn("review=sonnet:low", registry.read_text())
        self.assertEqual(len(probe.reads()), 1, probe.reads())
        self.assertIn("agent=opus", (session / "review_agent_contract").read_text())

    @override
    def environment(self) -> dict[str, str]:
        environment = super().environment()
        environment["FAST_TRACK_REVIEW_PROMPT"] = str(self.root / "received-fast-track-prompt.md")
        return environment

    def prepare_fast_track_review(self, name: str) -> tuple[Path, dict[str, str], str]:
        session = self.start_implementation(name)
        self.finish_implementation(session)
        with isolated_staff_file(self.root / "staff.json", {}):
            owner = staff.local_snapshot().by_member_reference(self.staff_parent)
            assert owner is not None
            _ = staff.rename(owner.id, "alpha")
        _ = (self.working_dir / "fast-fix.txt").write_text(f"already pushed fast fix {name}\n")
        for arguments in (["add", "fast-fix.txt"],
                          ["-c", "user.name=Review Test", "-c", "user.email=review@example.invalid",
                           "commit", "-m", "fast-track(alpha): repair the sample"]):
            _ = subprocess.run(["git", *arguments], cwd=self.working_dir, check=True, capture_output=True,
                               timeout=TEST_HANG_GUARD_SECS)
        tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.working_dir,
                             check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        environment = self.environment()
        directory = Path(environment["FAST_TRACK_STATE_DIR"])
        directory.mkdir(exist_ok=True)
        row = {"unit": "alpha", "commit": tip, "why": "repair the sample",
               "patch_sha256": hashlib.sha256(b"sample fix").hexdigest(),
               "time": "2026-10-31T12:00:00+00:00", "review": "pending"}
        _ = (directory / "records.jsonl").write_text(json.dumps(row) + "\n")
        # Keep the real command and its imports; only its fixture entry path changes.
        command = self.root / "scripts/production/fast_track.py"
        real_command = Path(__file__).resolve().parents[1] / "production/fast_track.py"
        _ = command.write_text("import os, sys\nos.execv(sys.executable, [sys.executable, "
                               + repr(str(real_command)) + ", *sys.argv[1:]])\n")
        agent = self.root / "scripts/agents/agent_exec.sh"
        with agent.open("a") as output:
            _ = output.write('cp "$4" "$FAST_TRACK_REVIEW_PROMPT"\n')
        return session, environment, tip

    def fast_track_review_rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in
                (self.root / "fast-track-state/records.jsonl").read_text().splitlines()]

    def test_each_broad_review_receives_pending_fast_fix_diffs_and_marks_the_snapshot(self) -> None:
        for lens in ("contract", "adversary"):
            with self.subTest(lens=lens):
                session, environment, tip = self.prepare_fast_track_review("fast-track-" + lens)
                result = subprocess.run(self.review_command(session, lens, ""), env=environment,
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prompt = Path(environment["FAST_TRACK_REVIEW_PROMPT"]).read_text()
                self.assertIn(tip, prompt)
                self.assertIn("diff --git a/fast-fix.txt b/fast-fix.txt", prompt)
                self.assertIn("+already pushed fast fix", prompt)
                row, = self.fast_track_review_rows()
                self.assertEqual(row["review"], "reviewed")
                self.assertIn(lens, str(row["review_pass"]))
                self.assertIn("1", str(row["review_pass"]))

    def test_broad_lenses_and_retry_keep_the_same_fast_fix_snapshot(self) -> None:
        session, environment, tip = self.prepare_fast_track_review("shared-fast-track-pass")
        command = self.review_command(session, "contract", "")
        first = subprocess.run(command, env=environment, capture_output=True, text=True,
                               timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        later = {**self.fast_track_review_rows()[0], "commit": "1" * 40,
                 "patch_sha256": hashlib.sha256(b"next pass fix").hexdigest(), "review": "pending"}
        records = self.root / "fast-track-state/records.jsonl"
        with records.open("a") as output:
            _ = output.write(json.dumps(later) + "\n")
        for lens in ("adversary", "contract"):
            with self.subTest(lens=lens):
                result = subprocess.run(self.review_command(session, lens, ""), env=environment,
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prompt = Path(environment["FAST_TRACK_REVIEW_PROMPT"]).read_text()
                self.assertIn(tip, prompt)
                self.assertIn("+already pushed fast fix", prompt)
                self.assertNotIn(str(later["commit"]), prompt)
        reviewed, pending = self.fast_track_review_rows()
        self.assertEqual(reviewed["review"], "reviewed")
        self.assertEqual(pending["review"], "pending")

    def test_failed_review_keeps_fast_fix_pending(self) -> None:
        session, environment, _ = self.prepare_fast_track_review("fast-track-red")
        with (self.root / "scripts/agents/agent_exec.sh").open("a") as output:
            _ = output.write("exit 9\n")
        result = subprocess.run(self.review_command(session, "contract", ""), env=environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        row, = self.fast_track_review_rows()
        self.assertEqual(row["review"], "pending")

    def test_successful_review_does_not_mark_a_fast_fix_added_after_dispatch(self) -> None:
        session, environment, tip = self.prepare_fast_track_review("fast-track-snapshot")
        later = {**self.fast_track_review_rows()[0], "commit": "1" * 40,
                 "patch_sha256": hashlib.sha256(b"later fix").hexdigest()}
        late_path = self.root / "late-fast-fix.jsonl"
        _ = late_path.write_text(json.dumps(later) + "\n")
        environment["FAST_TRACK_LATE_RECORD"] = str(late_path)
        environment["FAST_TRACK_RECORDS"] = str(self.root / "fast-track-state/records.jsonl")
        with (self.root / "scripts/agents/agent_exec.sh").open("a") as output:
            _ = output.write('cat "$FAST_TRACK_LATE_RECORD" >> "$FAST_TRACK_RECORDS"\n')
        result = subprocess.run(self.review_command(session, "contract", ""), env=environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.fast_track_review_rows()
        self.assertEqual((first["commit"], first["review"]), (tip, "reviewed"))
        self.assertEqual(second["review"], "pending")

    def test_rerun_lens_uses_review_slot_and_its_own_findings_file(self) -> None:
        session = self.start_implementation("rerun-slot")
        self.finish_implementation(session)
        result = subprocess.run(self.review_command(session, "rerun", ""),
                                env=self.environment(), capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "review_findings_1_rerun.txt").read_text(), "No findings.\n")
        events = [event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]
        self.assertEqual([event.get("team_slot") for event in events], ["review", "review"])

    def test_named_review_phase_reaches_verification_environment(self) -> None:
        session = self.start_implementation("review-phase-environment")
        self.finish_implementation(session)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the diff.\n")
        stub = self.root / "scripts" / "agents" / "agent_exec.sh"
        with stub.open("a") as output:
            _ = output.write('printf "%s|%s" "$UNIT_RUN_DIR" "$UNIT_TEAM_ROLE" > "$UNIT_RUN_DIR/wait-context"\n')
        result = self.checked_review(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((session / "wait-context").read_text(), f"{session}|")

    def test_interpreter_cache_reuses_the_resolved_python_without_a_version_probe(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "binaries"
        binaries.mkdir()
        interpreter = binaries / "python3"
        calls = self.root / "python-calls"
        _ = interpreter.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CACHE_TEST_CALLS"\nexec "$CACHE_TEST_REAL_PYTHON" "$@"\n'
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(self.root / "cold-cache"),
                            "CACHE_TEST_CALLS": str(calls), "CACHE_TEST_REAL_PYTHON": sys.executable})
        for _ in range(2):
            result = subprocess.run(["bash", str(launcher), "-c", "print('ready')"],
                                    env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual((result.returncode, result.stdout), (0, "ready\n"), result.stderr)
        commands = calls.read_text().splitlines()
        self.assertEqual(len(commands), 3)
        self.assertIn("sys.version_info", commands[0])
        self.assertEqual(commands[1:], ["-P -c print('ready')"] * 2)
        self.assertEqual((self.root / "cold-cache" / "claude" / cache_file_name()).read_text().strip(), str(interpreter))

    def test_interpreter_resolver_rejects_python310_and_its_old_cache(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "python310-bin"
        binaries.mkdir()
        interpreter = binaries / "python3"
        _ = interpreter.write_text(
            f"#!{sys.executable}\n"
            + "import datetime, sys\n"
            + "sys.version_info = (3, 10, 0, 'final', 0)\n"
            + "del datetime.UTC\n"
            + "exec(sys.argv[2])\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        cache_root = self.root / "python310-cache"
        old_cache = cache_root / "claude" / "python3-3.10"
        old_cache.parent.mkdir(parents=True)
        _ = old_cache.write_text(str(interpreter) + "\n")
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(cache_root)})
        result = subprocess.run(
            ["bash", str(launcher), "-c", "import datetime; print(datetime.UTC)"],
            env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual((result.returncode, result.stdout), (0, "UTC\n"), result.stderr)

    def test_interpreter_resolver_rejects_python311_and_its_old_cache(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "python311-bin"
        binaries.mkdir()
        interpreter = binaries / "python3"
        _ = interpreter.write_text(
            f"#!{sys.executable}\n"
            + "import sys\n"
            + "sys.version_info = (3, 11, 0, 'final', 0)\n"
            + "exec(sys.argv[2])\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        cache_root = self.root / "python311-cache"
        old_cache = cache_root / "claude" / "python3-3.11"
        old_cache.parent.mkdir(parents=True)
        _ = old_cache.write_text(str(interpreter) + "\n")
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(cache_root)})
        result = subprocess.run(
            ["bash", str(launcher), "-c", "import sys; print(sys.version_info[:2])"],
            env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual((result.returncode, result.stdout), (0, f"{sys.version_info[:2]}\n"), result.stderr)
        self.assertNotEqual((cache_root / "claude" / cache_file_name()).read_text().strip(), str(interpreter))


    def test_without_a_named_worker_the_fresh_read_only_path_is_unchanged(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session_dir / "dispatch_events.txt").exists())
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )


    def test_a_refused_launch_check_removes_a_stale_review_slot(self) -> None:
        session = self.start_implementation("stale-review-slot")
        self.finish_implementation(session)
        phase = session / "phases/99"
        phase.mkdir(parents=True)
        slot = phase / "review_slot_contract"
        _ = slot.write_text("review2\n", encoding="utf-8")
        result = self.checked_review(session, "--phase", "99", lens="contract")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unable to check the review launch", result.stderr)
        self.assertFalse(slot.exists())

    def test_codex_reviewers_must_use_assign(self) -> None:
        session = self.start_implementation("codex-refused")
        self.finish_implementation(session)
        registry = self.root / ".claude/config/models.conf"
        _ = registry.write_text(registry.read_text().replace("direct=claude", "direct=codex"))
        phase = session / "phases/3"
        phase.mkdir(parents=True, exist_ok=True)
        for name in ("review_slot_contract", "review_agent_1_contract.log"):
            _ = (phase / name).write_text("Keep this evidence.\n")
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in phase.iterdir()}
        events = self.events(session.name)
        environment = self.environment()
        environment["TEST_WORKER_STARTED"] = str(session / "review-worker-started")
        command = self.review_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["review.sh: Codex workers must use assign.sh."])
        self.assertFalse((session / "review-worker-started").exists())
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in phase.iterdir()}, before)
        self.assertEqual(self.turn_records(session), [])
        self.assertEqual(self.events(session.name), events)
        self.assertEqual(list(phase.glob("review_status*")), [])

    def test_removed_review_options_refuse_before_any_review_starts(self) -> None:
        for flag in ("--pool", "--lease", "--to", "--slot"):
            with self.subTest(flag=flag):
                session = self.start_implementation("removed-option-" + flag[2:])
                self.finish_implementation(session)
                command = self.review_command(session, "contract", "")
                command[2:2] = [flag, "unused"]
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                                        env=self.environment(), timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr.splitlines(), ["ERROR: unknown review option '" + flag + "'."])
                self.assertEqual(self.turn_records(session), [])


class FixtureReadinessTests(unittest.TestCase):
    def assert_exit_wakes_wait(self, *, portable: bool = False, polling: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "watched"
            directory.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                assert child.stdout is not None and child.stdin is not None
                self.assertEqual(child.stdout.readline(), "ready\n")
                checks = 0

                def exited() -> bool:
                    nonlocal checks
                    checks += 1
                    if checks == 1:
                        assert child.stdin is not None
                        child.stdin.close()
                        return False  # Exit races the wait, after registration.
                    return child.poll() is not None

                def polling_waiter(_a: Path, _b: Path, _mask: int) -> IntervalReleasePolling:
                    return IntervalReleasePolling()

                with ExitStack() as patches:
                    if portable:
                        _ = patches.enter_context(patch.object(os, "pidfd_open",
                            side_effect=AttributeError("pidfd_open unavailable"), create=True))
                    if polling:
                        _ = patches.enter_context(patch.object(wait_for_test_state,
                            "local_release_waiter", side_effect=polling_waiter))
                    # The exit predicate proves the wakeup; the deadline only guards a hang.
                    wait_until(exited, directory, timeout=TEST_HANG_GUARD_SECS, processes=(child,))
                self.assertGreaterEqual(checks, 2)
                self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                self.assertEqual(list(directory.iterdir()), [])
            finally:
                if child.poll() is None:
                    child.kill()
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
                if child.stdout is not None:
                    child.stdout.close()

    def test_child_exit_wakes_a_wait_without_a_directory_write(self) -> None:
        self.assert_exit_wakes_wait()

    def test_child_exit_wakes_a_wait_without_pidfd_support(self) -> None:
        self.assert_exit_wakes_wait(portable=True)

    def test_child_exit_wakes_a_wait_without_directory_notifications(self) -> None:
        self.assert_exit_wakes_wait(polling=True)


if __name__ == "__main__":
    _ = unittest.main()
