"""Exercise CI points and review alerts in disposable productions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import cast, final, override


SCRIPT = Path(__file__).with_name("ci_points.py")
LIFECYCLE = SCRIPT.with_name("production_lifecycle.py")
STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["CI_POINTS_TEST_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "calls.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps({"name": name, "args": args}) + "\\n")
if name == "review_regime.py":
    if args[:1] == ["watch"]:
        delay = os.environ.get("CI_POINTS_TEST_WATCH_DELAY")
        if delay:
            import time
            time.sleep(float(delay))
        print("12 of 12 phases: report ready, waiting for your acknowledgment")
        raise SystemExit(3 if (state / "watch-three").exists() else 0)
    if args[:1] == ["report"]:
        print("| review | after |")
        print("| phases merged | 12 |")
        raise SystemExit(0)
if name == "gh":
    record = json.loads((state / "ci-result.json").read_text())
    if args[:2] == ["repo", "view"]:
        print(json.dumps({"nameWithOwner": "example/example"}))
    elif args[:2] == ["run", "list"]:
        print(json.dumps([{"databaseId": 123, "headSha": record["headSha"],
                           "status": record["status"], "conclusion": record["conclusion"]}]))
    elif args[:2] == ["run", "view"]:
        print(json.dumps(record))
    elif args[:2] == ["run", "watch"]:
        hook = os.environ.get("CI_POINTS_TEST_WATCH_HOOK")
        if hook:
            import subprocess
            subprocess.run([sys.executable, hook], check=True)
        raise SystemExit(0 if record["conclusion"] == "success" else 1)
    else:
        raise SystemExit(99)
    raise SystemExit(0)
if name in ("pushover.py", "tmux", "claude", "systemd-run", "ssh", "nix", "cargo-berth", "notifier.sh", "showrunners.py", "py"):
    raise SystemExit(0)
raise SystemExit(99)
'''
VALIDATE_STUB = '''#!/bin/sh
printf '%s\\n' "$*" >> "$CI_POINTS_TEST_STATE/validate-calls"
printf '=== CI HANDOFF TO AGENT ===\\n'
printf 'repo: example/example\\n'
printf 'sha: %s\\n' "$(git rev-parse production)"
printf 'run_id: 123\\n'
exit 0
'''


@final
class CiPointsTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    checkout: Path = Path()
    state: Path = Path()
    stub_state: Path = Path()
    bin: Path = Path()
    doc: Path = Path()
    log: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.stub_state = self.root / "stub-state"
        self.stub_state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.doc = self.root / "example-production.md"
        self.log = self.checkout / "production.log"
        for name in ("review_regime.py", "pushover.py", "gh", "tmux", "claude",
                     "systemd-run", "ssh", "nix", "cargo-berth"):
            self.stub(self.bin / name)
        for path in (self.bin / "validate_and_push.sh",
                     self.home / ".claude/scripts/validate_and_push/validate_and_push.sh"):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write(path, VALIDATE_STUB)
            path.chmod(0o755)
        for relative in (".claude/scripts/notify/pushover.py", ".claude/scripts/message/notifier.sh",
                         ".claude/scripts/production/showrunners.py", ".claude/scripts/lib/py"):
            self.stub(self.home / relative)
        self.env = {**os.environ, "HOME": str(self.home), "CI_POINTS_TEST_STATE": str(self.stub_state),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "DAILIES_REVIEW_REGIME": str(self.bin / "review_regime.py"),
                    "CLAUDE_CODE_SESSION_ID": "test-showrunner",
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.state / "sessions")}
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "CI Points Test")
        _ = self.git("config", "user.email", "ci-points@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "README.md", "base\n")
        _ = self.git("add", "README.md")
        _ = self.git("commit", "-m", "initial")
        _ = self.git("push", "-u", "origin", "main")
        _ = self.git("switch", "-c", "production")
        with (self.checkout / ".git/info/exclude").open("a", encoding="utf-8") as excluded:
            _ = excluded.write("production.log\n")
        self.write(self.log, "# Production log — example\n\n### STATE\n")
        self.production_doc()
        _ = self.git("push", "-u", "origin", "production")
        self.ci_result(self.git("rev-parse", "HEAD"))

    def stub(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write(path, STUB.replace("__PYTHON__", sys.executable))
        path.chmod(0o755)

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def production_doc(self) -> None:
        self.write(self.doc, "\n".join((
            "# Production — example", "", "> **Status: PRODUCTION — running.** Example production.",
            "", "## Production Context", "", "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`", "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "- **Push:** git", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| alpha-unit | docs/alpha.md | `alpha` | alpha | alpha | — | — |", "",
            "## Gates", "", "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
            "| G1 | alpha-unit Phase 4 | beta-unit Phase 2 | checkpoint merged |", "",
            "## Production rules", "", "- CI runs on the production branch.", "",
        )))

    def run_ci(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--production", str(self.doc)],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True,
                              check=False, timeout=25)

    def calls(self, name: str) -> list[list[str]]:
        path = self.stub_state / "calls.jsonl"
        if not path.exists():
            return []
        rows = (cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines())
        return [cast(list[str], row["args"]) for row in rows if row["name"] == name]

    def ci_result(self, head: str, *, conclusion: str = "success",
                  status: str = "completed", test_suite: str = "success") -> None:
        record = {"databaseId": 123, "headSha": head, "status": status,
                  "conclusion": conclusion, "jobs": [
                      {"name": "Test Suite", "conclusion": test_suite, "status": status},
                      {"name": "macOS: Compile and Test", "conclusion": "skipped", "status": "completed"}]}
        self.write(self.stub_state / "ci-result.json", json.dumps(record))

    def merge(self, number: int, *, shrink: bool = False, unit: str = "alpha-unit") -> str:
        branch = f"unit-{number}{'-shrink' if shrink else ''}"
        _ = self.git("switch", "-c", branch)
        self.write(self.checkout / f"{branch}.txt", branch + "\n")
        _ = self.git("add", f"{branch}.txt")
        _ = self.git("commit", "-m", f"work {branch}")
        unit_tip = self.git("rev-parse", "HEAD")
        _ = self.git("switch", "production")
        subject = f"Merge {unit} phase {number}{' shrink' if shrink else ''} ({unit_tip[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-m", subject, branch)
        _ = self.git("push", "origin", "production")
        return self.git("rev-parse", "HEAD")

    def assert_step(self, result: subprocess.CompletedProcess[str], step: str, ok: bool) -> None:
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        self.assertIn(f"{step}: {'ok' if ok else 'failed'} — ", result.stdout)

    def start_point(self) -> subprocess.CompletedProcess[str]:
        result = self.run_ci("ci", "start", "--state-dir", str(self.state))
        self.assert_step(result, "ci start", True)
        return result

    def collect_point(self) -> subprocess.CompletedProcess[str]:
        return self.run_ci("ci", "collect", "--state-dir", str(self.state))

    def test_due_counts_code_merges_across_restarts_and_ignores_shrink(self) -> None:
        for number in range(1, 5):
            _ = self.merge(number)
        fourth = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(fourth, "due", True)
        self.assertIn("4 of 5 code merges", fourth.stdout)
        _ = self.merge(40, shrink=True)
        shrink = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(shrink, "due", True)
        self.assertIn("4 of 5 code merges", shrink.stdout)
        _ = self.merge(5)
        for _ in range(2):
            fifth = self.run_ci("due", "--state-dir", str(self.state))
            self.assert_step(fifth, "due", True)
            self.assertIn("CI point due", fifth.stdout)
        _ = self.merge(6)
        sixth = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(sixth, "due", True)
        self.assertIn("CI point due", sixth.stdout)

    def test_no_ci_skips_due_start_and_collect_without_side_effects(self) -> None:
        for args, step in (("due", "due"), ("ci start", "ci start"), ("ci collect", "ci collect")):
            with self.subTest(command=args):
                result = self.run_ci(*args.split(), "--state-dir", str(self.state), "--no-ci")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), f"{step}: ok — skipped: no CI")
        self.assertFalse((self.state / "ci_points.json").exists())
        self.assertFalse((self.state / "ci_run.json").exists())
        self.assertFalse((self.stub_state / "validate-calls").exists())
        self.assertEqual(self.calls("gh"), [])

    def test_showrunner_and_dailies_share_review_watch_state_directory(self) -> None:
        root = Path(__file__).resolve().parents[2]
        produce = (root / "commands/showrunner/produce.md").read_text(encoding="utf-8")
        dailies = (root / "commands/showrunner/dailies.md").read_text(encoding="utf-8")
        state_list = produce.split("State:\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(state_list, r"`DAILIES_STATE_DIR`\s+—\s+`<SCRATCH>/dailies_input_state`")
        calls = cast(list[tuple[str, str]], re.findall(
            r"ci_points\.py\s+(due|watch|ci start|ci collect|notice clear|notice lift)\b([^`]*)`",
            produce, flags=re.DOTALL))
        self.assertEqual(len(calls), 6, calls)
        stateful = [(command, body) for command, body in calls
                    if command in ("due", "watch", "ci start", "ci collect")]
        self.assertEqual(len(stateful), 4, calls)
        for command, body in stateful:
            with self.subTest(command=command):
                self.assertRegex(body, r"--state-dir\s+DAILIES_STATE_DIR\b")
                self.assertNotRegex(body, r"--state-dir\s+SCRATCH\b")
        self.assertRegex(dailies, r"dailies_input\.py[\s\S]*?--state-dir\s+<scratchpad>/dailies_input_state\b")

    def test_ci_start_passes_cancel_prior_only_when_requested(self) -> None:
        flagged = self.run_ci("ci", "start", "--state-dir", str(self.state), "--cancel-prior")
        self.assert_step(flagged, "ci start", True)
        calls = (self.stub_state / "validate-calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--cancel-prior", calls[0].split())
        self.assertTrue(calls[0].endswith(" --cancel-prior"), calls[0])
        plain = self.start_point()
        self.assert_step(plain, "ci start", True)
        calls = (self.stub_state / "validate-calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--cancel-prior", calls[1].split())
        skipped = self.run_ci("ci", "start", "--state-dir", str(self.state), "--no-ci", "--cancel-prior")
        self.assertEqual(skipped.returncode, 0, skipped.stdout + skipped.stderr)
        self.assertEqual(skipped.stdout.strip(), "ci start: ok — skipped: no CI")
        self.assertEqual(len((self.stub_state / "validate-calls").read_text().splitlines()), 2)

    def test_collect_records_green_tip_and_resets_five_merge_interval(self) -> None:
        for number in range(1, 5):
            _ = self.merge(number)
        tip = self.merge(5)
        self.ci_result(tip)
        _ = self.start_point()
        collected = self.collect_point()
        self.assert_step(collected, "ci collect", True)
        self.assertEqual(collected.stdout.splitlines()[-1], f"--ci-green {tip}")
        self.assertEqual(json.loads((self.state / "ci_points.json").read_text()),
                         {"count": 5, "tip": tip})
        self.assertTrue(any(call[:2] == ["run", "watch"] for call in self.calls("gh")),
                        self.calls("gh"))
        recorded = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(recorded, "due", True)
        self.assertIn("0 of 5 code merges", recorded.stdout)
        _ = self.merge(6)
        one_after = self.run_ci("due", "--state-dir", str(self.state))
        self.assertIn("1 of 5 code merges", one_after.stdout)
        _ = self.merge(60, shrink=True)
        after_shrink = self.run_ci("due", "--state-dir", str(self.state))
        self.assertIn("1 of 5 code merges", after_shrink.stdout)
        for number in range(7, 10):
            _ = self.merge(number)
        fourth_after = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(fourth_after, "due", True)
        self.assertIn("4 of 5 code merges", fourth_after.stdout)
        _ = self.merge(10)
        fifth_after = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(fifth_after, "due", True)
        self.assertIn("CI point due", fifth_after.stdout)

    def test_collect_rejects_run_on_pre_validation_tip_and_names_rerun(self) -> None:
        old_tip = self.merge(1)
        self.ci_result(old_tip)
        _ = self.start_point()
        new_tip = self.merge(2)
        collected = self.collect_point()
        self.assert_step(collected, "ci collect", False)
        self.assertIn("CI", collected.stdout)
        self.assertIn(old_tip, collected.stdout)
        self.assertIn(new_tip, collected.stdout)
        self.assertNotIn("--ci-green", collected.stdout)
        self.assertFalse((self.state / "ci_points.json").exists())

    def test_collect_records_code_merges_at_tested_tip_before_wait(self) -> None:
        for number in range(1, 5):
            _ = self.merge(number)
        tip = self.merge(5)
        self.ci_result(tip)
        _ = self.start_point()
        hook = self.stub_state / "merge-during-watch.py"
        self.write(hook, textwrap.dedent('''\
            import subprocess

            def git(*args: str) -> str:
                result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
                return result.stdout.strip()

            git("switch", "-c", "unit-6")
            git("commit", "--allow-empty", "-m", "work unit-6")
            unit_tip = git("rev-parse", "HEAD")
            git("switch", "production")
            git("merge", "--no-ff", "-m", f"Merge alpha-unit phase 6 ({unit_tip[:7]}) into production", "unit-6")
            git("push", "origin", "production")
            '''))
        self.env["CI_POINTS_TEST_WATCH_HOOK"] = str(hook)
        collected = self.collect_point()
        self.assert_step(collected, "ci collect", True)
        self.assertEqual(collected.stdout.splitlines()[-1], f"--ci-green {tip}")
        self.assertEqual(json.loads((self.state / "ci_points.json").read_text()),
                         {"count": 5, "tip": tip})
        due = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(due, "due", True)
        self.assertIn("1 of 5 code merges", due.stdout)

    def test_collect_rejects_missing_or_empty_head_commit(self) -> None:
        tip = self.merge(1)
        self.ci_result(tip)
        _ = self.start_point()
        path = self.stub_state / "ci-result.json"
        for head in (None, ""):
            with self.subTest(head=head):
                record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                if head is None:
                    del record["headSha"]
                else:
                    record["headSha"] = head
                self.write(path, json.dumps(record))
                collected = self.collect_point()
                self.assert_step(collected, "ci collect", False)
                self.assertIn(f"rerun CI on {tip}", collected.stdout)
                self.assertNotIn("--ci-green", collected.stdout)
                self.assertFalse((self.state / "ci_points.json").exists())

    def test_collect_rejects_red_required_job_and_unfinished_run(self) -> None:
        tip = self.merge(1)
        self.ci_result(tip)
        _ = self.start_point()
        for conclusion, status, test_suite in (("failure", "completed", "failure"),
                                               ("", "in_progress", "")):
            with self.subTest(conclusion=conclusion, status=status):
                self.ci_result(tip, conclusion=conclusion, status=status, test_suite=test_suite)
                collected = self.collect_point()
                self.assert_step(collected, "ci collect", False)
                self.assertIn("Test Suite" if status == "completed" else "CI", collected.stdout)
                self.assertIn(tip, collected.stdout)
                self.assertNotIn("--ci-green", collected.stdout)
                self.assertFalse((self.state / "ci_points.json").exists())

    def test_collected_verdict_promotes_main_only_while_tip_matches(self) -> None:
        tip = self.merge(1)
        self.ci_result(tip)
        _ = self.start_point()
        collected = self.collect_point()
        self.assert_step(collected, "ci collect", True)
        flag, value = collected.stdout.splitlines()[-1].split()
        self.assertEqual((flag, value), ("--ci-green", tip))
        promoted = subprocess.run([sys.executable, str(LIFECYCLE), "promote-main", "--production",
                                   str(self.doc), flag, value, "--smoke-passed", tip],
                                  cwd=self.checkout, env=self.env, capture_output=True,
                                  text=True, check=False, timeout=25)
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        self.assertIn("ci: ok", promoted.stdout)
        _ = self.merge(2)
        held = subprocess.run([sys.executable, str(LIFECYCLE), "promote-main", "--production",
                               str(self.doc), flag, value, "--smoke-passed", tip],
                              cwd=self.checkout, env=self.env, capture_output=True,
                              text=True, check=False, timeout=25)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn("ci: held", held.stdout)

    def test_watch_first_exit_three_reports_notifies_and_logs_once(self) -> None:
        _ = (self.stub_state / "watch-three").touch()
        first = self.run_ci("watch", "--state-dir", str(self.state))
        self.assert_step(first, "watch", True)
        self.assertIn("| phases merged | 12 |", first.stdout)
        second = self.run_ci("watch", "--state-dir", str(self.state))
        self.assert_step(second, "watch", True)
        self.assertNotIn("| phases merged | 12 |", second.stdout)
        self.assertEqual(len(self.calls("pushover.py")), 1)
        self.assertEqual(len(self.calls("review_regime.py")), 3)
        self.assertEqual(self.log.read_text().count("review watch"), 1)

    def test_simultaneous_watch_calls_claim_only_one_first_alert(self) -> None:
        _ = (self.stub_state / "watch-three").touch()
        self.env["CI_POINTS_TEST_WATCH_DELAY"] = "0.5"
        command = [sys.executable, str(SCRIPT), "watch", "--production", str(self.doc),
                   "--state-dir", str(self.state)]
        processes = [subprocess.Popen(command, cwd=self.checkout, env=self.env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        results = [process.communicate(timeout=25) for process in processes]
        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        self.assertEqual(sum("| phases merged | 12 |" in stdout for stdout, _ in results), 1)
        self.assertEqual(len(self.calls("pushover.py")), 1)
        self.assertEqual(self.log.read_text().count("review watch"), 1)

    def test_notice_clear_names_waiting_unit_and_merge_hash(self) -> None:
        tip = self.merge(2, unit="beta-unit")
        result = self.run_ci("notice", "clear", "G1")
        self.assert_step(result, "notice clear", True)
        self.assertIn(f"send alpha-unit: From the showrunner: G1 clear — beta-unit phase 2 is on production as {tip}",
                      result.stdout)

    def test_notice_lift_requires_log_and_names_test_evidence(self) -> None:
        missing = self.run_ci("notice", "lift", "G1")
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        log = self.root / "scratch-test.log"
        self.write(log, "tests passed\n")
        lifted = self.run_ci("notice", "lift", "G1", "--log", str(log))
        self.assert_step(lifted, "notice lift", True)
        self.assertIn(f"send alpha-unit: From the showrunner: G1 lifted — your tests pass without beta-unit phase 2 ({log}). Continue.",
                      lifted.stdout)


if __name__ == "__main__":
    _ = unittest.main()
