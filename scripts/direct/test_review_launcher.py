"""Check reviewer identity, reuse, compaction and interpreter resolution."""

from __future__ import annotations

from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id, isolated_staff_file

from scripts.lib.py_floor_test_support import cache_file_name

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.test_implement_launcher import configure_transport, install_worker_command, configure_pane_account
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
    @override
    def setUp(self) -> None:
        super().setUp()
        configure_transport(self.root)

    def test_herdr_simplify_keeps_the_claude_launch_path(self) -> None:
        run = self.start_implementation("pane-simplify")
        self.finish_implementation(run)
        _ = (run / "worker_transport").write_text("herdr\n")
        source = Path(__file__).resolve().parents[1]
        (self.root / "scripts/direct/simplify_result.py").symlink_to(source / "direct/simplify_result.py")
        usage = self.root / "scripts/usage"
        usage.mkdir()
        (usage / "agents_log.py").symlink_to(source / "usage/agents_log.py")
        template = self.root / "review"
        template.mkdir()
        _ = (template / "simplify.md").write_text("Report only. Edit no file.\n")
        _ = write_registry(self.root / ".claude/config/models.conf", {"assignments": {"direct.simplify": "claude"},
                           "direct.claude": {"simplify": "opus:xhigh"}},
                           update=True)
        diff = self.root / "phase.diff"
        _ = diff.write_text("diff --git a/worker.py b/worker.py\n-old\n+new\n")
        agent = self.root / "scripts/agents/agent_exec.sh"
        _ = agent.write_text(agent.read_text().replace("No findings.", "Pass"))
        environment = self.environment()
        environment["HERDR_BIN"] = "/nonexistent/herdr"
        before = (self.root / "staff.json").read_bytes()
        command = self.review_command(run, "simplify", "")
        command[5] = "simplify"
        command[2:2] = ["--diff", str(diff)]
        result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((run / "review_findings_1_simplify.txt").read_text(), "Pass\n")
        self.assertEqual((run / "review_status_simplify").read_text(), "reviewed\n")
        self.assertIn("task=direct.simplify\nfamily=claude\n", (run / "review_agent_simplify").read_text())
        self.assertEqual((run / "worker_transport").read_text(), "herdr\n")
        self.assertEqual((self.root / "staff.json").read_bytes(), before)

    def test_herdr_review_keeps_unknown_evidence_without_a_mesh_claim(self) -> None:
        run = self.start_implementation("pane-unknown")
        self.finish_implementation(run)
        _ = (run / "worker_transport").write_text("herdr\n")
        install_worker_command(self.root)
        _ = self.open_worker(run)
        environment = self.environment()
        environment["HERDR_BIN"] = "/nonexistent/herdr"
        environment["AGENT_NOTES_DIR"] = str(self.root / "notes")
        configure_pane_account(self.root, Path(environment["AGENT_NOTES_DIR"]))
        before = (self.root / "staff.json").read_bytes()
        result = subprocess.run(self.review_command(run, "contract", ""), env=environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((run / "review_status_contract").read_text(), "error\n")
        self.assertEqual((self.root / "staff.json").read_bytes(), before)
        self.assertIn("/nonexistent/herdr", (run / "review_agent_1_contract.log").read_text())

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

    def test_own_claim_refusal_removes_a_stale_review_slot(self) -> None:
        session = self.start_implementation("stale-review-slot")
        self.finish_implementation(session)
        slot = session / "review_slot_contract"
        _ = slot.write_text("review2\n", encoding="utf-8")
        _ = self.open_worker(session, name="fixture-review", role="review", status="running")
        result = subprocess.run(self.unit_review_command(session), env=self.environment(), capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(slot.exists())

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
            _ = staff.rename(owner.id, "alpha-unit")
        _ = (self.working_dir / "fast-fix.txt").write_text(f"already pushed fast fix {name}\n")
        for arguments in (["add", "fast-fix.txt"],
                          ["-c", "user.name=Review Test", "-c", "user.email=review@example.invalid",
                           "commit", "-m", "fast-track(alpha-unit): repair the sample"]):
            _ = subprocess.run(["git", *arguments], cwd=self.working_dir, check=True, capture_output=True,
                               timeout=TEST_HANG_GUARD_SECS)
        tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.working_dir,
                             check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        environment = self.environment()
        directory = Path(environment["FAST_TRACK_STATE_DIR"])
        directory.mkdir(exist_ok=True)
        row = {"unit": "alpha-unit", "commit": tip, "why": "repair the sample",
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
            '#!/bin/bash\nprintf "%s\\n" "$2" >> "$CACHE_TEST_CALLS"\nexec "$CACHE_TEST_REAL_PYTHON" "$@"\n'
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
        self.assertEqual(commands[1:], ["print('ready')"] * 2)
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

    def test_a_named_open_worker_receives_the_review_and_writes_findings(self) -> None:
        session_dir = self.start_implementation("assigned")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)

        result = self.run_assigned_review(session_dir, worker)

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
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        reviews = [
            event for event in self.pass_events("assigned")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "impl"), ("pass_finished", "impl")],
        )
        self.assertEqual(reviews[0]["worker_name"], worker)

    def test_a_review_opened_worker_has_durable_identity_for_an_implementation_claim(self) -> None:
        session_dir = self.start_implementation("new-worker")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review2"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
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
        worker = "project-review2"
        self.assertEqual(roster[worker]["status"], "done")
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        # The seat's log is its own, so a second seat on pass 1 cannot truncate it.
        self.assertTrue((session_dir / "review_agent_1-review2.log").is_file())
        self.assertEqual(os.readlink(session_dir / "review_agent.log"), "review_agent_1-review2.log")
        reviews = [
            event for event in self.pass_events("new-worker")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "review2"), ("pass_finished", "review2")],
        )
        identity = cast(
            "dict[str, object]",
            json.loads((session_dir / "impl_worker_review2.json").read_text()),
        )
        self.assertEqual(identity, {
            "staff_id": roster[worker]["staff_id"],
            "slot": "review2",
            "model": "gpt-called",
        })

        implementation = subprocess.run(
            [
                "bash", str(self.review_script.with_name("implement.sh")),
                "--to", worker, str(session_dir), str(self.working_dir),
                str(self.prompt_file), "impl", "write the repair",
                "impl", "writing the repair", "0", "review2",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(implementation.returncode, 0, implementation.stderr)
        self.assertEqual(
            (session_dir / "impl_status_review2").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_a_test_worker_keeps_one_identity_when_assigned_a_review(self) -> None:
        run = self.start_implementation("test-becomes-review")
        self.finish_implementation(run)
        worker = self.open_worker(run, name="project-test", role="test", thread_id="thread-test")
        member = self.recorded_worker(run, worker)
        identity = {"staff_id": member.id, "slot": "test", "model": "gpt-called"}
        path = run / "impl_worker_test.json"
        _ = path.write_text(json.dumps(identity))

        reviewed = self.run_assigned_review(run, worker)

        self.assertEqual(reviewed.returncode, 0, reviewed.stderr + (run / "review_agent_1-test.log").read_text())
        records = [cast(dict[str, object], json.loads(item.read_text()))
                   for item in run.glob("impl_worker_*.json")]
        identities = [record for record in records if record.get("staff_id") == member.id]
        self.assertEqual(len(identities), 1, records)
        self.assertEqual(identities[0]["staff_id"], member.id)
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["staff_id"], member.id)
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["role"], "review")

    def test_hyphenated_new_slot_is_refused_to_keep_followup_identity_stable(self) -> None:
        session_dir = self.start_implementation("hyphenated-slot")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review-extra"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("alphanumeric/_ characters", result.stderr)
        self.assertFalse((session_dir / "mesh_roster.json").exists())

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
        self.assertFalse((session_dir / "mesh_events.txt").exists())
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_unit_reviewer_runs_and_keeps_its_thread(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.unit_review_command(session_dir, ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        events = (session_dir / "mesh_events.txt").read_text().splitlines()
        self.assertIn("follow", events)
        records = cast(dict[str, dict[str, object]], json.loads((session_dir / "mesh_roster.json").read_text()))
        self.assertEqual(len(records), 1)
        self.assertEqual(next(iter(records.values()))["role"], "review")
        self.assertEqual(self.turn_records(session_dir)[0]["arm"], "own")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_removed_review_options_refuse_before_any_review_starts(self) -> None:
        for flag in ("--pool", "--lease"):
            with self.subTest(flag=flag):
                session = self.start_implementation("removed-option-" + flag[2:])
                self.finish_implementation(session)
                command = self.review_command(session, "contract", "")
                command[2:2] = [flag, "unused"]
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                                        env=self.environment(), timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr.splitlines(), ["ERROR: unknown review option '" + flag + "'."])
                self.assertFalse((session / "mesh_roster.json").exists())
                self.assertEqual(self.turn_records(session), [])

    def test_paused_reviewer_receives_followup_on_its_recorded_thread(self) -> None:
        session_dir = self.start_implementation("paused-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="paused")
        result = self.run_assigned_review(session_dir, worker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text().splitlines(), ["can-follow", "follow"])
        roster = cast(dict[str, dict[str, object]], json.loads((session_dir / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["thread_id"], fixture_runtime_id("thread-fixture"))
        self.assertEqual((session_dir / "follow_message.txt").read_text(), "Review the diff.\n")
        self.assertEqual((session_dir / "review_status").read_text(), "reviewed\n")

    def test_a_busy_named_worker_is_refused_before_review_artifacts_change(self) -> None:
        session_dir = self.start_implementation("busy")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="running")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"codex_mesh: {worker}: worker is busy or not followable", result.stderr)
        self.assertFalse((session_dir / "review_status").exists())
        self.assertFalse((session_dir / "review_findings_1.txt").exists())

    def test_a_large_named_worker_is_compacted_before_the_review_claim(self) -> None:
        session_dir = self.start_implementation("compact")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact", "can-follow", "follow"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"compacted {worker}: 100001 -> 12000 tokens", board)

    def test_review_compaction_uses_the_first_private_config_value(self) -> None:
        session = self.start_implementation("first-compact-value")
        self.finish_implementation(session)
        with self.config_file.open("a", encoding="utf-8") as output:
            _ = output.write("\n# whole: 1..\nUNIT_COMPACT_ABOVE_TOKENS=300000\n")
        worker = self.open_worker(session, context_tokens=100001)

        result = self.run_assigned_review(session, worker)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((session / "mesh_events.txt").read_text().splitlines(),
                         ["compact", "can-follow", "follow"])

    def test_a_compaction_roster_conflict_stops_review_before_claim_or_follow(self) -> None:
        session_dir = self.start_implementation("compact-roster-changed")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        original = (session_dir / "mesh_roster.json").read_bytes()
        (session_dir / "compact_roster_changed").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("roster changed during compaction", result.stderr)
        self.assertIn("reopen", result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text().splitlines(), ["compact"])
        self.assertEqual((session_dir / "mesh_roster.json").read_bytes(), original)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        self.assertFalse((session_dir / "review_status").exists())
        self.assertFalse((session_dir / "review_findings_1.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)

    def test_review_of_an_unmeasured_worker_does_not_compact_again(self) -> None:
        session_dir = self.start_implementation("unmeasured-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        path = session_dir / "mesh_roster.json"
        roster = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        roster[worker]["context_tokens"] = "not_measured_since_compaction"
        _ = path.write_text(json.dumps(roster), encoding="utf-8")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["can-follow", "follow"])
        stored = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(stored[worker]["context_tokens"], "not_measured_since_compaction")

    def test_review_posts_the_unmeasured_compaction_line_unchanged(self) -> None:
        session_dir = self.start_implementation("compact-unmeasured-line")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_reports_zero").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher: compacted {worker}: 100001 tokens, size not measured until its next turn", board)

    def test_a_worker_left_with_a_live_turn_by_compaction_gets_no_review_follow_up(self) -> None:
        session_dir = self.start_implementation("compact-still-live")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_still_live").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the worker", result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)


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

                def polling_waiter(_a: Path, _b: Path) -> IntervalReleasePolling:
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
