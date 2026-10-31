"""Exercise CI points and review alerts in disposable productions."""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import TypedDict, cast, final, override

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_points
from cli_fixture import invoke_cli
from merge_checkpoint import merge_lock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("ci_points.py")
LIFECYCLE = SCRIPT.with_name("production_lifecycle.py")
STUB = '''#!__PYTHON__
import fcntl
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
        print("12 of 12 phases: report ready, waiting for your acknowledgment")
        raise SystemExit(3 if (state / "watch-three").exists() else 0)
    if args[:1] == ["report"]:
        print("| review | after |")
        print("| phases merged | 12 |")
        raise SystemExit(0)
if name in ("send.py", "claude", "systemd-run", "ssh", "nix", "cargo-berth", "notifier.sh", "showrunners.py", "py"):
    raise SystemExit(0)
raise SystemExit(99)
'''
GH_STUB = r'''#!/bin/bash
set -euo pipefail
state=${CI_POINTS_TEST_STATE:?}
reply=$(jq -cn --slurpfile result "$state/ci-result.json" --args '
    $ARGS.positional as $args | {name:"gh", args:$args},
    (if $args[0:2] == ["repo", "view"] then {nameWithOwner:"example/example"}
     elif $args[0:2] == ["run", "list"] then [$result[0] | {databaseId:123, headSha, status, conclusion}]
     elif $args[0:2] == ["run", "view"] then $result[0]
     elif $args[0:2] == ["run", "watch"] then {code:(if $result[0].conclusion == "success" then 0 else 1 end)}
     else {code:99} end)' -- "$@")
printf '%s\n' "${reply%%$'\n'*}" >> "$state/calls.jsonl"
answer=${reply#*$'\n'}
if [[ ${1:-} == repo && ${2:-} == view ]]; then
    if [[ " $* " == *' defaultBranchRef '* ]]; then
        printf 'main\n'
    else
        printf 'example/example\n'
    fi
    exit 0
fi
if [[ ${1:-} == run && ${2:-} == list && " $* " == *' databaseId,createdAt '* ]]; then
    if [[ -f "$state/fail-handoff" ]]; then
        rm "$state/fail-handoff"
        printf 'none 2026-10-30 00:00:00 UTC\n'
        exit 0
    fi
    printf '123 2026-10-30 00:00:00 UTC\n'
    exit 0
fi
if [[ ${1:-} == run && ${2:-} == watch && -n ${CI_POINTS_TEST_WATCH_HOOK:-} ]]; then
    "${TEST_PYTHON:?}" "$CI_POINTS_TEST_WATCH_HOOK"
fi
case "$answer" in
    '{"code":0}') exit 0 ;;
    '{"code":1}') exit 1 ;;
    '{"code":99}') exit 99 ;;
    *) printf '%s\n' "$answer" ;;
esac
'''
VALIDATE_STUB = '''#!/bin/sh
printf '%s\\n' "$*" >> "$CI_POINTS_TEST_STATE/validate-calls"
exit 0
'''
PUSH_STUB = '''#!/bin/sh
printf '%s|%s\\n' "$CANCEL_PRIOR" "$*" >> "$CI_POINTS_TEST_STATE/publish-calls"
if [ "$1" = --publish-only ]; then
    printf '%s contained\\n' "$2"
    exit 0
fi
printf '=== CI HANDOFF TO AGENT ===\\n'
printf 'repo: example/example\\n'
printf 'sha: %s\\n' "$2"
printf 'run_id: 123\\n'
exit 0
'''


class ValidationObservation(TypedDict):
    directory: str
    initial: str
    validated: str
    origin: str
    remote_tip: str
    cargo_target: str


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
        for name in ("review_regime.py", "gh", "claude",
                     "systemd-run", "ssh", "nix", "cargo-berth"):
            self.stub(self.bin / name)
        for path in (self.bin / "validate_and_push.sh",
                     self.home / ".claude/scripts/validate_and_push/validate_and_push.sh"):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write(path, VALIDATE_STUB)
            path.chmod(0o755)
            self.write(path.with_name("push_direct.sh"), PUSH_STUB)
        for relative in (".claude/scripts/message/send.py", ".claude/scripts/message/notifier.sh",
                         ".claude/scripts/production/showrunners.py", ".claude/scripts/lib/py"):
            self.stub(self.home / relative)
        self.env = {**os.environ, "HOME": str(self.home), "CI_POINTS_TEST_STATE": str(self.stub_state), "TEST_PYTHON": sys.executable,
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
        self.write(path, GH_STUB if path.name == "gh" else STUB.replace("__PYTHON__", sys.executable))
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
        arguments = [*args, "--production", str(self.doc)]
        return invoke_cli(ci_points, ci_points.main, arguments, self.env, self.checkout)

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

    def fixed_commit_validation(self, *, move_branch: bool = False,
                                fix_commit: bool = False, fail: bool = False) -> None:
        self.env.update({"CI_POINTS_TEST_CHECKOUT": str(self.checkout),
                         "CI_POINTS_TEST_MOVE": str(int(move_branch)),
                         "CI_POINTS_TEST_FIX": str(int(fix_commit)),
                         "CI_POINTS_TEST_FAIL": str(int(fail))})
        validate = self.bin / "validate_and_push.sh"
        self.env["CI_POINTS_VALIDATE_SCRIPT"] = str(validate)
        scripts = SCRIPT.parents[1] / "validate_and_push"
        for name in ("validate_and_push.sh", "push_direct.sh", "post_push_hook.sh"):
            _ = shutil.copy2(scripts / name, self.bin / name)
        self.write(self.bin / "run_validation.sh", '''#!/bin/sh
set -eu
"$TEST_PYTHON" - <<'PY'
import json
import os
from pathlib import Path
import subprocess

state = Path(os.environ["CI_POINTS_TEST_STATE"])
checkout = Path(os.environ["CI_POINTS_TEST_CHECKOUT"])
directory = Path.cwd()

def git(*args, cwd=directory):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()

initial = git("rev-parse", "HEAD")
origin = git("remote", "get-url", "origin")
if os.environ["CI_POINTS_TEST_FIX"] == "1" and (directory / "README.md").read_text() != "validation fix\\n":
    (directory / "README.md").write_text("validation fix\\n")
    git("add", "README.md")
    git("-c", "user.name=CI Points Test", "-c", "user.email=ci-points@example.invalid",
        "commit", "-m", "validation fix")
validated = git("rev-parse", "HEAD")
if os.environ["CI_POINTS_TEST_MOVE"] == "1":
    git("commit", "--allow-empty", "-m", "concurrent merge", cwd=checkout)
    git("push", "origin", "production", cwd=checkout)
remote_tip = git("ls-remote", "origin", "refs/heads/production").split()[0]
observation = {
    "directory": str(directory), "initial": initial, "validated": validated,
    "origin": origin, "remote_tip": remote_tip,
    "cargo_target": os.environ.get("CARGO_TARGET_DIR", ""),
}
(state / "validation-observation.json").write_text(json.dumps(observation))
with (state / "validation-attempts.jsonl").open("a") as attempts:
    attempts.write(json.dumps(observation) + "\\n")
result = json.loads((state / "ci-result.json").read_text())
result["headSha"] = validated if os.environ["CI_POINTS_TEST_FIX"] == "1" else remote_tip
(state / "ci-result.json").write_text(json.dumps(result))
if os.environ["CI_POINTS_TEST_FAIL"] == "1":
    print("FAILED: fixed-commit validation")
    raise SystemExit(7)
PY
''')
        update = self.origin / "hooks/update"
        self.write(update, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CI_POINTS_TEST_STATE/pushes"\n')
        update.chmod(0o755)

    def validation_observation(self) -> ValidationObservation:
        return cast(ValidationObservation, json.loads(
            (self.stub_state / "validation-observation.json").read_text(encoding="utf-8")))

    def test_a_contained_validated_commit_passes_after_the_branch_moves_without_pushing(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(move_branch=True)

        result = self.start_point()

        observed = self.validation_observation()
        self.assertEqual(observed["initial"], initial)
        self.assertEqual(observed["validated"], initial)
        self.assertNotEqual(observed["remote_tip"], initial)
        self.assertIn(f"validation: ok — validated {initial}; no push; following {observed['remote_tip']}",
                      result.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), observed["remote_tip"])
        pushes = (self.stub_state / "pushes").read_text().splitlines()
        self.assertEqual(pushes, [f"refs/heads/production {initial} {observed['remote_tip']}"])
        self.assertEqual(ci_points.read_run(self.state).tip, observed["remote_tip"])
        self.assertTrue(any("--commit" in call and observed["remote_tip"] in call
                            for call in self.calls("gh")), self.calls("gh"))
        self.assertFalse(Path(observed["directory"]).exists())

    def test_a_validation_fix_fast_forwards_an_unchanged_remote_branch(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fix_commit=True)

        result = self.start_point()

        observed = self.validation_observation()
        fixed = observed["validated"]
        self.assertNotEqual(fixed, initial)
        self.assertIn(f"validation: ok — validated {fixed}; fast-forward push to origin/production",
                      result.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), fixed)
        self.assertEqual(self.git("rev-parse", f"{fixed}^", cwd=self.origin), initial)
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {fixed}"])
        self.assertEqual(ci_points.read_run(self.state).tip, fixed)
        self.assertEqual(self.git("rev-parse", "HEAD"), fixed)
        self.assertEqual(self.git("show", "production:README.md", cwd=self.origin), "validation fix")
        self.assertEqual((self.checkout / "README.md").read_text(), "validation fix\n")
        self.assertFalse(Path(observed["directory"]).exists())

    def test_collect_accepts_green_ci_on_the_published_validation_fix(self) -> None:
        self.fixed_commit_validation(fix_commit=True)
        _ = self.start_point()
        fixed = self.validation_observation()["validated"]

        result = self.collect_point()

        self.assert_step(result, "ci collect", True)
        self.assertEqual(result.stdout.splitlines()[-1], f"--ci-green {fixed}")
        self.assertEqual(json.loads((self.state / "ci_points.json").read_text()),
                         {"count": 0, "tip": fixed})
        self.assertEqual(self.git("rev-parse", "production"), fixed)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), fixed)

    def test_rerun_after_a_pushed_fix_with_no_handoff_reuses_the_fix_commit(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fix_commit=True)
        _ = (self.stub_state / "fail-handoff").touch()

        first = self.run_ci("ci", "start", "--state-dir", str(self.state))

        self.assert_step(first, "ci start", False)
        self.assertIn("validation did not provide a CI run", first.stdout)
        fixed = self.validation_observation()["validated"]
        self.assertNotEqual(fixed, initial)
        self.assertEqual(self.git("rev-parse", "production"), fixed)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), fixed)
        self.assertFalse((self.state / "ci_run.json").exists())
        self.assertFalse(Path(self.validation_observation()["directory"]).exists())

        result = self.start_point()

        attempts = [cast(ValidationObservation, json.loads(line)) for line in
                    (self.stub_state / "validation-attempts.jsonl").read_text().splitlines()]
        self.assertEqual([(attempt["initial"], attempt["validated"]) for attempt in attempts],
                         [(initial, fixed), (fixed, fixed)])
        self.assertEqual(self.git("rev-list", f"{initial}..production").splitlines(), [fixed])
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), fixed)
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {fixed}"])
        self.assertIn(f"validation: ok — validated {fixed}; no push; following {fixed}", result.stdout)
        self.assertEqual(ci_points.read_run(self.state).tip, fixed)
        self.assertTrue(any("--commit" in call and fixed in call for call in self.calls("gh")))
        self.assertTrue(all(not Path(attempt["directory"]).exists() for attempt in attempts))

    def test_retry_after_a_failed_push_publishes_the_existing_fix_without_a_sibling_commit(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fix_commit=True)
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, '#!/bin/sh\nprintf "simulated push failure\\n" >&2\nexit 1\n')
        reject.chmod(0o755)

        first = self.run_ci("ci", "start", "--state-dir", str(self.state))

        self.assert_step(first, "validation", False)
        self.assertIn("simulated push failure", first.stdout)
        fixed = self.validation_observation()["validated"]
        self.assertNotEqual(fixed, initial)
        self.assertEqual(self.git("rev-parse", "production"), fixed)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), initial)
        self.assertFalse((self.stub_state / "pushes").exists())
        self.assertFalse((self.state / "ci_run.json").exists())
        self.assertFalse(self.calls("gh"))
        self.assertFalse(Path(self.validation_observation()["directory"]).exists())
        reject.unlink()

        result = self.start_point()

        attempts = [cast(ValidationObservation, json.loads(line)) for line in
                    (self.stub_state / "validation-attempts.jsonl").read_text().splitlines()]
        self.assertEqual([(attempt["initial"], attempt["validated"]) for attempt in attempts],
                         [(initial, fixed), (fixed, fixed)])
        self.assertEqual(self.git("rev-list", f"{initial}..production").splitlines(), [fixed])
        self.assertEqual(self.git("rev-parse", "production"), fixed)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), fixed)
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {fixed}"])
        self.assertIn(f"validation: ok — validated {fixed}; fast-forward push to origin/production",
                      result.stdout)
        self.assertEqual(ci_points.read_run(self.state).tip, fixed)
        self.assertTrue(any("--commit" in call and fixed in call for call in self.calls("gh")))
        self.assertTrue(all(not Path(attempt["directory"]).exists() for attempt in attempts))

    def test_a_later_merge_push_fast_forwards_from_the_published_ci_fix(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fix_commit=True)
        _ = self.start_point()
        fixed = self.validation_observation()["validated"]
        self.assertEqual(self.git("rev-parse", "production"), fixed)

        merged = self.merge(1)

        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merged)
        self.assertEqual(self.git("merge-base", fixed, merged), fixed)
        self.assertEqual((self.checkout / "README.md").read_text(), "validation fix\n")
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {fixed}",
                          f"refs/heads/production {fixed} {merged}"])

    def test_a_merge_landing_while_ci_publish_waits_for_the_lock_is_preserved(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fix_commit=True)
        with merge_lock(self.checkout, "example"):
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "ci", "start", "--production", str(self.doc),
                 "--state-dir", str(self.state)],
                cwd=self.checkout, env=self.env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            output = process.stdout
            try:
                descriptor = output.fileno()
                readable, _, _ = select.select([descriptor], [], [], 300)
                self.assertEqual(readable, [descriptor], "CI publish never reached the merge lock")
                waiting = cast(str, output.readline())
                self.assertTrue(waiting.startswith("merge lock: waiting —"), waiting)
                self.assertIsNone(process.poll(), "CI publish did not wait for the held merge lock")
                observed = self.validation_observation()
                self.assertNotEqual(observed["validated"], initial)
                self.assertEqual(self.git("rev-parse", "production"), initial)
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), initial)
                self.assertFalse((self.stub_state / "pushes").exists())
                merged = self.merge(1)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=300)
                raise
        try:
            stdout, stderr = process.communicate(timeout=300)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=300)

        self.assertEqual(process.returncode, 2, waiting + stdout + stderr)
        self.assertIn("merge branch moved during validation", stdout)
        self.assertEqual(self.git("rev-parse", "production"), merged)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merged)
        self.assertEqual(self.git("merge-base", initial, merged), initial)
        self.assertEqual((self.checkout / "README.md").read_text(), "base\n")
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {merged}"])
        self.assertFalse((self.state / "ci_run.json").exists())
        self.assertFalse(Path(observed["directory"]).exists())

    def test_a_validation_fix_cannot_overwrite_a_branch_that_moves_during_validation(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(move_branch=True, fix_commit=True)

        result = self.run_ci("ci", "start", "--state-dir", str(self.state))

        self.assert_step(result, "validation", False)
        self.assertIn("merge branch moved during validation", result.stdout)
        observed = self.validation_observation()
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), observed["remote_tip"])
        self.assertNotEqual(observed["remote_tip"], observed["validated"])
        self.assertEqual((self.stub_state / "pushes").read_text().splitlines(),
                         [f"refs/heads/production {initial} {observed['remote_tip']}"])
        self.assertFalse((self.state / "ci_run.json").exists())
        self.assertFalse(Path(observed["directory"]).exists())

    def test_validation_uses_a_private_copy_and_leaves_the_production_checkout_unchanged(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        before_status = self.git("status", "--porcelain")
        before_readme = (self.checkout / "README.md").read_bytes()
        self.fixed_commit_validation()

        _ = self.start_point()

        observed = self.validation_observation()
        self.assertNotEqual(Path(observed["directory"]), self.checkout)
        self.assertEqual(observed["initial"], initial)
        self.assertEqual(observed["origin"], str(self.origin))
        self.assertEqual(Path(observed["cargo_target"]), Path(observed["directory"]) / "target")
        self.assertEqual(self.git("rev-parse", "HEAD"), initial)
        self.assertEqual(self.git("status", "--porcelain"), before_status)
        self.assertEqual((self.checkout / "README.md").read_bytes(), before_readme)
        self.assertFalse((self.stub_state / "pushes").exists())
        self.assertFalse(Path(observed["directory"]).exists())

    def test_a_failed_validation_removes_its_private_copy_without_pushing(self) -> None:
        initial = self.git("rev-parse", "HEAD")
        self.fixed_commit_validation(fail=True)

        result = self.run_ci("ci", "start", "--state-dir", str(self.state))

        self.assert_step(result, "validation", False)
        self.assertEqual(
            result.stdout,
            "validation: failed — stdout: === STEP: validation ===; FAILED: fixed-commit validation\n",
        )
        observed = self.validation_observation()
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), initial)
        self.assertFalse((self.stub_state / "pushes").exists())
        self.assertFalse((self.state / "ci_run.json").exists())

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
        self.assertRegex(state_list, r"`DAILIES_STATE_DIR`\s+—\s+`<STATE_DIR>/dailies_input_state`")
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
        self.assertRegex(dailies, r"dailies_input\.py[\s\S]*?--state-dir\s+<STATE_DIR>/dailies_input_state\b")

    def test_ci_start_passes_cancel_prior_only_when_requested(self) -> None:
        tip = self.git("rev-parse", "HEAD")
        flagged = self.run_ci("ci", "start", "--state-dir", str(self.state), "--cancel-prior")
        self.assert_step(flagged, "ci start", True)
        calls = (self.stub_state / "validate-calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        publication = (self.stub_state / "publish-calls").read_text().splitlines()
        self.assertEqual(publication, [f"true|--publish-only {tip}",
                                       f"true|--follow-published {tip} contained"])
        plain = self.start_point()
        self.assert_step(plain, "ci start", True)
        calls = (self.stub_state / "validate-calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        publication += [f"false|--publish-only {tip}",
                        f"false|--follow-published {tip} contained"]
        self.assertEqual((self.stub_state / "publish-calls").read_text().splitlines(), publication)
        skipped = self.run_ci("ci", "start", "--state-dir", str(self.state), "--no-ci", "--cancel-prior")
        self.assertEqual(skipped.returncode, 0, skipped.stdout + skipped.stderr)
        self.assertEqual(skipped.stdout.strip(), "ci start: ok — skipped: no CI")
        self.assertEqual(len((self.stub_state / "validate-calls").read_text().splitlines()), 2)
        self.assertEqual((self.stub_state / "publish-calls").read_text().splitlines(), publication)

    def test_validation_failure_names_the_failed_check_when_stderr_only_reports_memory_waits(self) -> None:
        validate = self.bin / "validate_and_push.sh"
        self.write(validate, "\n".join((
            "#!/bin/sh",
            "printf 'waiting for memory: 2 GiB available\\nwaiting for memory: 3 GiB available\\n' >&2",
            "printf 'FAILED: quota formatter check\\n'",
            "exit 7",
            "",
        )))
        self.env["CI_POINTS_VALIDATE_SCRIPT"] = str(validate)

        result = self.run_ci("ci", "start", "--state-dir", str(self.state))

        self.assert_step(result, "validation", False)
        self.assertEqual(result.stdout.strip(), (
            "validation: failed — stderr: waiting for memory: 2 GiB available; "
            + "waiting for memory: 3 GiB available; stdout: FAILED: quota formatter check"
        ))
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.state / "ci_run.json").exists())

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
                                  text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        self.assertIn("ci: ok", promoted.stdout)
        _ = self.merge(2)
        held = subprocess.run([sys.executable, str(LIFECYCLE), "promote-main", "--production",
                               str(self.doc), flag, value, "--smoke-passed", tip],
                              cwd=self.checkout, env=self.env, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
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
        self.assertEqual(self.calls("send.py"), [])
        self.assertIn("| phases merged | 12 |", self.log.read_text())
        self.assertEqual(len(self.calls("review_regime.py")), 3)
        self.assertEqual(self.log.read_text().count("review watch"), 1)

    def test_simultaneous_watch_calls_claim_only_one_first_alert(self) -> None:
        _ = (self.stub_state / "watch-three").touch()
        program = """import fcntl, os, runpy, sys
ready = int(sys.argv.pop(1))
sys.argv.pop(0)
original = fcntl.flock
def observed_lock(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            os.write(ready, b'blocked')
        else:
            raise RuntimeError('expected the parent to hold the review lock')
        # Observe only the initial contention; later locks use their real path.
        fcntl.flock = original
    return original(fd, operation)
fcntl.flock = observed_lock
sys.path.insert(0, os.path.dirname(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        self.state.mkdir(exist_ok=True)
        processes: list[subprocess.Popen[str]] = []
        readiness: list[int] = []
        try:
            with (self.state / "review_watch.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                for _ in range(2):
                    read_ready, write_ready = os.pipe()
                    readiness.append(read_ready)
                    try:
                        processes.append(subprocess.Popen(
                            [sys.executable, "-c", program, str(write_ready), str(SCRIPT),
                             "watch", "--production", str(self.doc), "--state-dir", str(self.state)],
                            cwd=self.checkout, env=self.env, pass_fds=(write_ready,),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        ))
                    finally:
                        os.close(write_ready)
                for fd, process in zip(readiness, processes, strict=True):
                    readable, _, _ = select.select([fd], [], [], 300)
                    self.assertEqual(readable, [fd], "watch never reached the held lock")
                    if os.read(fd, 7) != b"blocked":
                        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.fail("watch exited before observing the review lock: " + stdout + stderr)
                self.assertTrue(all(process.poll() is None for process in processes))
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                results = [process.communicate(timeout=TEST_HANG_GUARD_SECS) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            for fd in readiness:
                os.close(fd)
        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        self.assertEqual(sum("| phases merged | 12 |" in stdout for stdout, _ in results), 1)
        self.assertEqual(len(self.calls("send.py")), 0)
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


    def test_mixed_handoff_history_does_not_advance_the_ci_interval(self) -> None:
        for number in range(1, 5):
            _ = self.merge(number)
        for subject in (
            "Merge alpha-unit hand-off (aaaaaaa) into production",
            "Merge style-child hand-off (bbbbbbb) into production",
            "Merge alpha-unit phase 4 shrink (ccccccc) into production",
            "chore(example): rename unit alpha-unit to renamed-unit",
        ):
            _ = self.git("commit", "--allow-empty", "-m", subject)
        due = self.run_ci("due", "--state-dir", str(self.state))
        self.assert_step(due, "due", True)
        self.assertIn("4 of 5 code merges", due.stdout)
        self.ci_result(self.git("rev-parse", "HEAD"))
        _ = self.start_point()
        self.assert_step(self.collect_point(), "ci collect", True)
        record = cast(dict[str, object], json.loads((self.state / "ci_points.json").read_text()))
        self.assertEqual(record["count"], 4)

    def test_ci_fix_commit_label_uses_last_unit_phase_when_a_handoff_is_newest(self) -> None:
        _ = self.merge(2)
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit hand-off (aaaaaaa) into production")
        _ = self.start_point()
        command = (self.stub_state / "validate-calls").read_text()
        self.assertIn("ci(example): validation fixes after alpha-unit phase 2", command)
        self.assertNotIn("hand-off", command)

    def test_notice_clear_cannot_treat_a_child_named_like_the_target_as_a_unit_phase(self) -> None:
        _ = self.git("commit", "--allow-empty", "-m", "Merge beta-unit hand-off (bbbbbbb) into production")
        result = self.run_ci("notice", "clear", "G1")
        self.assert_step(result, "notice clear", False)
        self.assertIn("beta-unit phase 2 has no code merge", result.stdout)
        self.assertNotIn("send alpha-unit", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
