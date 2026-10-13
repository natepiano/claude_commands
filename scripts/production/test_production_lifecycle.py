"""Exercise production lifecycle steps in disposable git repositories."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override

import fake_tmux
from fake_tmux import FakeSession


SCRIPT = Path(__file__).with_name("production_lifecycle.py")
STUB = '''#!__PYTHON__
import os
from pathlib import Path
import sys

state = Path(os.environ["LIFECYCLE_TEST_STATE"])
name = Path(sys.argv[0]).name
with (state / "calls").open("a") as output:
    output.write(name + " " + " ".join(sys.argv[1:])
                 + (" cwd=" + os.getcwd() if name == "cargo-berth" else "") + "\\n")
if name == "tmux" and sys.argv[1:2] == ["has-session"]:
    session = sys.argv[-1].removeprefix("=")
    raise SystemExit(0 if (state / ("live-" + session)).exists() else 1)
if name == "ssh":
    print("rc=" + (state / "mac-rc").read_text().strip() if (state / "mac-rc").exists() else "rc=0")
    raise SystemExit(0)
if name == "cargo-berth" and sys.argv[1:2] == ["board"]:
    print((state / "reservations.json").read_text() if (state / "reservations.json").exists() else "")
    raise SystemExit(0)
if name in ("notifier.sh", "showrunners.py", "cargo-berth", "py"):
    raise SystemExit(0)
raise SystemExit(99)
'''


@final
class LifecycleTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.origin = Path()
        self.checkout = Path()
        self.alpha = Path()
        self.beta = Path()
        self.doc = Path()
        self.log = Path()
        self.state = Path()
        self.home = Path()
        self.phase_table_vault = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "checkout"
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.doc = self.checkout / "docs/plans/example-production.md"
        self.log = self.checkout / "production.log"
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.phase_table_vault = self.root / "vault" / "showrunners"
        self.state.mkdir()
        self.home.mkdir()
        self.phase_table_vault.parent.mkdir()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ("tmux", "claude", "systemd-run", "ssh", "nix", "gh", "zsh", "cargo-berth"):
            self.stub(bin_path / name)
        for relative in (".claude/scripts/message/notifier.sh",
                         ".claude/scripts/production/showrunners.py",
                         ".claude/scripts/lib/py"):
            self.stub(self.home / relative)
        self.env = {**os.environ, "HOME": str(self.home), "LIFECYCLE_TEST_STATE": str(self.state),
                    "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                    # Unit sessions are looked up in this stand-in; with no state file it has no sessions.
                    "UNIT_LOOKUP_TMUX": str(Path(__file__).with_name("fake_tmux.py")),
                    "FAKE_TMUX_STATE": str(self.state / "tmux.json"),
                    "CLAUDE_CODE_SESSION_ID": "test-showrunner",
                    "SHOWRUNNER_SESSION": "environment-showrunner",
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.state / "sessions"),
                    "PHASE_TABLE_VAULT": str(self.phase_table_vault)}
        self.checkout.mkdir()
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Lifecycle Test")
        _ = self.git("config", "user.email", "lifecycle@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "README.md", "base\n")
        self.write(self.checkout / "docs/plans/alpha.md", "# Alpha plan\n")
        self.write(self.checkout / "docs/plans/beta.md", "# Beta plan\n")
        self.production_doc("planned")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial production plan")
        _ = self.git("push", "-u", "origin", "main")

    def stub(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write(path, STUB.replace("__PYTHON__", sys.executable))
        path.chmod(0o644 if path.name == "showrunners.py" else 0o755)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, (args, process.stdout, process.stderr))
        return process.stdout.strip()

    def production_doc(self, status: str, *, promote: str = "", close_out: str = "") -> None:
        lines = [
            "# Production — example", "",
            f"> **Status: PRODUCTION — {status}.** Example production.",
            "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** old-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes",
            "- **Push:** git",
        ]
        if promote:
            lines.append(f"- **Promote:** {promote}")
        lines.extend([
            "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| alpha-unit | docs/plans/alpha.md | `{self.alpha}` | alpha-branch | alpha | — | `alpha.txt` |",
            f"| beta-unit | docs/plans/beta.md | `{self.beta}` | beta-branch | beta | — | `beta.txt` |",
            "", "## Close-out", "",
        ])
        if close_out:
            lines.append(close_out)
        lines.extend(["", "## Production rules", "", "- No CI, smoke launch, Mac run, or validate_and_push.sh."])
        self.write(self.doc, "\n".join(lines) + "\n")

    def running(self) -> None:
        _ = self.git("switch", "-c", "production")
        self.production_doc("running")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "production(example): plans for 2 units")
        _ = self.git("push", "-u", "origin", "production")
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.alpha), "production")
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.beta), "production")
        _ = self.git("push", "origin", "alpha-branch", "beta-branch")
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        with (self.checkout / common_dir / "info/exclude").open("a", encoding="utf-8") as output:
            _ = output.write("production.log\n")
        self.write(self.log, "# Production log — example\n\n### STATE\nold state\n")

    def run_lifecycle(self, action: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), action, "--production", str(self.doc),
                               *extra], cwd=self.checkout, env=self.env, capture_output=True,
                              text=True, check=False, timeout=25)

    def assert_step(self, result: subprocess.CompletedProcess[str], state: str) -> None:
        self.assertRegex(result.stdout, rf"(?m)^[\w-]+: {state} — .+")
        self.assertEqual(result.returncode, 0 if state == "ok" else 2,
                         result.stdout + result.stderr)

    def assert_no_ci_skips(self, output: str) -> None:
        steps = cast(list[str], re.findall(r"(?m)^([\w-]+): ok — skipped: no CI$", output))
        for name in ("ci", "smoke", "mac"):
            self.assertTrue(any(name in step.lower() for step in steps), output)

    def side_commit(self, worktree: Path, branch: str, unit: str, phase: str,
                    *, shrink: bool = False) -> tuple[str, str]:
        relative = f"{unit}-{phase}{'-shrink' if shrink else ''}.txt"
        self.write(worktree / relative, relative + "\n")
        _ = self.git("add", relative, cwd=worktree)
        _ = self.git("commit", "-m", relative, cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        _ = self.git("push", "origin", branch, cwd=worktree)
        subject = f"Merge {unit} phase {phase}{' shrink' if shrink else ''} ({commit[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-m", subject, branch)
        return commit, self.git("rev-parse", "HEAD")

    def merge_tip(self) -> str:
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        return self.git("rev-parse", "HEAD")

    def promotion_checkout(self, name: str) -> Path:
        destination = self.root / name
        _ = self.git("clone", "-b", "main", str(self.origin), str(destination), cwd=self.root)
        _ = self.git("config", "user.name", "Lifecycle Test", cwd=destination)
        _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=destination)
        return destination

    def calls(self) -> str:
        return (self.state / "calls").read_text() if (self.state / "calls").exists() else ""

    def write_phase_note(self, name: str, production: str, unit: str) -> Path:
        note = self.phase_table_vault / "showrunner" / f"{name}.md"
        self.write(
            note,
            "\n".join(
                (
                    "---",
                    "phase_table: true",
                    f"production: {production}",
                    f"unit: {unit}",
                    "---",
                    "",
                    f"# {name}",
                    "",
                )
            ),
        )
        return note

    def test_load_holds_checkout_on_other_branch(self) -> None:
        self.running()
        _ = self.git("switch", "main")
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "held")
        self.assertIn("Run /showrunner:produce in a checkout on production.", result.stdout)

    def test_load_fails_when_production_doc_lacks_merge_branch(self) -> None:
        self.running()
        self.write(self.doc, self.doc.read_text().replace("- **Merge branch:** `production`\n", ""))
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "failed")
        self.assertIn("Merge branch", result.stdout)

    def test_load_resume_reads_state_for_planned_production(self) -> None:
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        with (self.checkout / common_dir / "info/exclude").open("a", encoding="utf-8") as output:
            _ = output.write("production.log\n")
        self.write(self.log, "# Production log — example\n\n### STATE\nresumed state\n")

        ordinary = self.run_lifecycle("load", "--no-ci")
        self.assert_step(ordinary, "ok")
        self.assertNotIn("resumed state", ordinary.stdout)
        resumed = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(resumed, "ok")
        self.assertIn("resumed state", resumed.stdout)
        self.assertIn("alpha-unit: no code checkpoint", resumed.stdout)

    def test_load_reports_each_last_code_merge_even_after_shrink(self) -> None:
        self.running()
        _, alpha_merge = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "2")
        _, beta_merge = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "2", shrink=True)
        _ = (self.state / "live-alpha").touch()
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "ok")
        self.assertIn("old state", result.stdout)
        self.assertIn("alpha-unit", result.stdout)
        self.assertIn("beta-unit", result.stdout)
        self.assertIn(alpha_merge[:7], result.stdout)
        self.assertIn(beta_merge[:7], result.stdout)
        self.assertIn("session: ok — alpha-unit: gone", result.stdout)
        self.assertIn("session: ok — beta-unit: gone", result.stdout)

    def test_load_finds_each_unit_session_by_its_mark_whatever_it_is_called(self) -> None:
        self.running()
        fake_tmux.write(self.state / "tmux.json", {
            # A session that only has the name the unit was launched under is not the unit.
            "$1": FakeSession(label="alpha", panes=["%1"], env={}),
            "$2": FakeSession(label="renamed-since-launch", panes=["%2"],
                              env={"SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": "beta-unit"}),
        })
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "ok")
        self.assertIn("alpha-unit: gone", result.stdout)
        self.assertIn("beta-unit: renamed-since-launch live", result.stdout)

    def test_open_holds_when_production_is_wrapped(self) -> None:
        self.running()
        self.production_doc("wrapped")
        before = self.git("rev-parse", "HEAD")
        result = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(result, "held")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_open_resumes_at_push_after_rejection(self) -> None:
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        failed = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(failed, "failed")
        self.assertIn("push", failed.stdout.lower())
        first_tip = self.git("rev-parse", "HEAD")
        self.assertIn("running", self.doc.read_text())
        reject.unlink()
        retried = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(retried, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), first_tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), first_tip)
        # The showrunner's name is written nowhere: open leaves no name in the doc.
        self.assertNotIn("test-showrunner", self.doc.read_text())
        self.assertTrue(self.log.read_text().startswith("# Production log — example"))
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        self.assertIn("production.log", (self.checkout / common_dir / "info/exclude").read_text())

    def test_open_needs_no_session_name(self) -> None:
        result = self.run_lifecycle("open")
        self.assert_step(result, "ok")
        self.assertIn("doc: ok — running", result.stdout)

    def test_open_rerun_does_not_commit_plans_twice_and_writes_no_session_name(self) -> None:
        first = self.run_lifecycle("open", "--session", "first-showrunner")
        self.assert_step(first, "ok")
        tip = self.git("rev-parse", "HEAD")
        second = self.run_lifecycle("open", "--session", "other-showrunner")
        self.assert_step(second, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertEqual(self.git("log", "--format=%s", "--grep=^production(example): plans for 2 units$", "production"),
                         "production(example): plans for 2 units")
        self.assertNotIn("first-showrunner", self.doc.read_text())
        self.assertNotIn("other-showrunner", self.doc.read_text())

    def test_open_running_with_committed_plans_skips_commit_and_push(self) -> None:
        self.running()
        tip = self.git("rev-parse", "HEAD")
        result = self.run_lifecycle("open", "--session", "other-showrunner")
        self.assert_step(result, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertIn("**Showrunner session:** old-showrunner", self.doc.read_text())

    def test_promote_main_holds_when_origin_main_is_ahead(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Lifecycle Test", cwd=writer)
        _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=writer)
        self.write(writer / "main-only.txt", "main\n")
        _ = self.git("add", "main-only.txt", cwd=writer)
        _ = self.git("commit", "-m", "main moved", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "held")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin),
                         self.git("rev-parse", "main", cwd=writer))

    def test_promote_main_fails_when_origin_rejects_main_push(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "failed")
        self.assertIn("push", result.stdout.lower())

    def test_promote_main_requires_ci_and_smoke_evidence_on_same_tip(self) -> None:
        self.running()
        other = self.git("rev-parse", "HEAD")
        tip = self.merge_tip()
        main_before = self.git("rev-parse", "main", cwd=self.origin)

        no_ci_record = self.run_lifecycle("promote-main")
        self.assert_step(no_ci_record, "held")
        self.assertIn("ci: held", no_ci_record.stdout)
        green_without_smoke = self.run_lifecycle("promote-main", "--ci-green", tip)
        self.assert_step(green_without_smoke, "held")
        self.assertIn("smoke-launch: held", green_without_smoke.stdout)
        wrong_ci = self.run_lifecycle("promote-main", "--ci-green", other, "--smoke-passed", tip)
        self.assert_step(wrong_ci, "held")
        self.assertIn("ci: held", wrong_ci.stdout)
        self.assertIn(other, wrong_ci.stdout)
        self.assertIn(tip, wrong_ci.stdout)
        wrong_smoke = self.run_lifecycle("promote-main", "--ci-green", tip, "--smoke-passed", other)
        self.assert_step(wrong_smoke, "held")
        self.assertIn("smoke-launch: held", wrong_smoke.stdout)
        self.assertIn(other, wrong_smoke.stdout)
        self.assertIn(tip, wrong_smoke.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_before)
        self.assertEqual(self.git("rev-parse", "main"), main_before)

        incompatible = self.run_lifecycle("promote-main", "--no-ci", "--ci-green", tip)
        self.assert_step(incompatible, "failed")
        self.assertIn("input: failed — --no-ci excludes the verdict flags", incompatible.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_before)

        passed = self.run_lifecycle("promote-main", "--ci-green", tip, "--smoke-passed", tip[:9])
        self.assert_step(passed, "ok")
        self.assertIn("ci: ok", passed.stdout)
        self.assertIn("smoke-launch: ok", passed.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_produce_names_live_ci_and_smoke_steps(self) -> None:
        command = Path(__file__).parents[2] / "commands/showrunner/produce.md"
        text = command.read_text(encoding="utf-8")
        section = text.split("<PromoteMain>", 1)[-1].split("</PromoteMain>", 1)[0]
        for phrase in ("smoke_launch.sh", "gh run view", "--ci-green <sha>", "--smoke-passed <sha>"):
            self.assertIn(phrase, section)

    def test_promote_main_pushes_local_tip_when_only_origin_is_behind(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        self.production_doc("running", promote=f"`{destination}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=destination)
        _ = self.git("merge", "--ff-only", tip, cwd=destination)
        first = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(first, "ok")
        self.assertIn(f"{destination}: pushed {tip}", first.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        second = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(second, "ok")
        self.assertIn(f"{destination}: already at {tip}", second.stdout)

    def test_promote_main_holds_foreign_local_main_commit_without_push(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        self.production_doc("running", promote=f"`{destination}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=destination)
        _ = self.git("merge", "--ff-only", tip, cwd=destination)
        self.write(destination / "foreign.txt", "foreign\n")
        _ = self.git("add", "foreign.txt", cwd=destination)
        _ = self.git("commit", "-m", "foreign main", cwd=destination)
        old_main = self.git("rev-parse", "main", cwd=self.origin)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("has main commits outside the merge branch", result.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), old_main)

    def test_promote_main_pulls_mac_with_and_without_ci(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        mac = self.root / "mac-installed"
        self.production_doc("running", promote=f"`{destination}`, mac `{mac}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        for options in (("--ci-green", tip, "--smoke-passed", tip), ("--no-ci",)):
            _ = (self.state / "calls").write_text("")
            result = self.run_lifecycle("promote-main", *options)
            self.assert_step(result, "ok")
            self.assertIn(f"mac {mac} main pulled", result.stdout)
            calls = [line for line in self.calls().splitlines() if line.startswith("ssh ")]
            self.assertEqual(len(calls), 1)
            self.assertIn("ssh mac", calls[0])
            self.assertIn(f"git pull --ff-only natedev:{destination} main", calls[0])
        _ = (self.state / "mac-rc").write_text("1\n")
        failed = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(failed, "failed")
        self.assertIn("promote: failed", failed.stdout)

    def test_promote_main_skips_ci_smoke_mac_and_repairs_behind_destination(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        at_tip = self.root / "at-tip"
        behind = self.root / "behind"
        for path in (at_tip, behind):
            _ = self.git("clone", "-b", "main", str(self.origin), str(path), cwd=self.root)
            _ = self.git("config", "user.name", "Lifecycle Test", cwd=path)
            _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=path)
        _ = self.git("fetch", "origin", "production", cwd=at_tip)
        _ = self.git("merge", "--ff-only", tip, cwd=at_tip)
        self.production_doc("running", promote=f"`{at_tip}`, `{behind}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "set promotion destinations")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=at_tip)
        _ = self.git("merge", "--ff-only", tip, cwd=at_tip)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "ok")
        self.assert_no_ci_skips(result.stdout)
        self.assertIn(f"{at_tip}: pushed {tip}", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=behind), tip)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        calls = (self.state / "calls").read_text() if (self.state / "calls").exists() else ""
        self.assertNotIn("ssh ", calls)
        self.assertNotIn("gh ", calls)

    def test_wrap_holds_unapproved_close_out(self) -> None:
        self.running()
        self.production_doc("running", close_out="- When done, delete the cache")
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn('When done, delete the cache is not recorded done', result.stdout)
        self.assertTrue(self.alpha.exists())
        self.assertIn("running", self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)

        near = self.run_lifecycle("wrap", "--no-ci", "--close-out-done", "When done delete the cache")
        self.assert_step(near, "held")
        self.assertIn("When done, delete the cache", near.stdout)
        self.assertTrue(self.alpha.exists())

    def test_wrap_holds_planned_production(self) -> None:
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("wrap: held", result.stdout)

    def test_wrap_rejects_verdict_for_other_tip_before_retirement(self) -> None:
        self.running()
        other = self.git("rev-parse", "HEAD")
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        tip = self.git("rev-parse", "HEAD")
        origin_before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_lifecycle("wrap", "--ci-green", other, "--smoke-passed", tip)
        self.assert_step(result, "held")
        self.assertIn("ci: held", result.stdout)
        self.assertTrue(self.alpha.exists())
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), origin_before)

        incompatible = self.run_lifecycle("wrap", "--no-ci", "--smoke-passed", tip)
        self.assert_step(incompatible, "failed")
        self.assertIn("input: failed — --no-ci excludes the verdict flags", incompatible.stdout)

    def test_wrap_accepts_exact_close_out_completion(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        item = "Archive records to safe location."
        self.production_doc("running", close_out=f"- {item}")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "record close-out item")
        _ = self.git("push", "origin", "production")

        result = self.run_lifecycle("wrap", "--no-ci", "--close-out-done", item)
        self.assert_step(result, "ok")
        self.assertIn(f"close-out: ok — {item} (recorded done)", result.stdout)
        self.assertIn("wrapped", self.doc.read_text())

    def test_wrap_requires_merged_remote_branch_when_worktree_is_gone(self) -> None:
        self.running()
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        self.write(self.alpha / "unmerged.txt", "unmerged\n")
        _ = self.git("add", "unmerged.txt", cwd=self.alpha)
        _ = self.git("commit", "-m", "unmerged", cwd=self.alpha)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.alpha)
        _ = self.git("worktree", "remove", str(self.alpha))
        _ = self.git("branch", "-D", "alpha-branch")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("alpha-branch", result.stdout)
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "alpha-branch").split()[1],
                         "refs/heads/alpha-branch")

    def test_wrap_reports_already_retired_unit_with_no_branches(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        _ = self.git("worktree", "remove", str(self.alpha))
        _ = self.git("branch", "-d", "alpha-branch")
        _ = self.git("push", "origin", "--delete", "alpha-branch")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("alpha-unit", result.stdout)
        self.assertIn("already retired", result.stdout)

    def test_wrap_releases_reservation_from_unit_worktree_through_python_wrapper(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        reservations = '{"reservations": [{"worktree": "' + str(self.alpha) + '", "id": "alpha-reservation"}]}'
        _ = (self.state / "reservations.json").write_text(reservations)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn(f"cargo-berth release alpha-reservation --json cwd={self.alpha}", self.calls())
        # Removing the update timer is the whole of forgetting a showrunner: no registry is rewritten.
        self.assertNotIn("showrunners.py", self.calls())

    def test_wrap_resumes_after_final_push_rejection_without_second_commit(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        marker = self.root / "reject-production"
        marker.touch()
        hook = self.origin / "hooks/pre-receive"
        self.write(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                   + f'  if [ "$ref" = "refs/heads/production" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                   + 'done\nexit 0\n')
        hook.chmod(0o755)
        failed = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(failed, "failed")
        self.assertIn("wrapped", self.doc.read_text())
        wrapped_tip = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), wrapped_tip)
        self.assertFalse(self.alpha.exists())
        retired_calls = self.calls()
        marker.unlink()
        retried = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(retried, "ok")
        self.assertIn("| Next |", retried.stdout)
        self.assertIn("by the earlier run |", retried.stdout)
        self.assertNotIn("(0 commits)", retried.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), wrapped_tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), wrapped_tip)
        self.assertEqual(self.git("log", "--format=%s", "--grep=^production(example): wrapped$", "production"),
                         "production(example): wrapped")
        self.assertEqual(self.calls(), retired_calls)
        calls_after = self.calls()
        already_pushed = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(already_pushed, "ok")
        self.assertIn("| Next |", already_pushed.stdout)
        self.assertEqual(self.calls(), calls_after)

    def test_wrap_fails_on_dirty_unit_worktree(self) -> None:
        self.running()
        self.write(self.alpha / "dirty.txt", "unsaved\n")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "failed")
        self.assertIn("alpha", result.stdout)
        self.assertTrue(self.alpha.exists())

    def test_wrap_without_ci_retires_units_removes_notifier_and_reports(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assert_no_ci_skips(result.stdout)
        self.assertFalse(self.alpha.exists())
        self.assertFalse(self.beta.exists())
        self.assertIn("wrapped", self.doc.read_text())
        for area in ("Units", "Merge branch", "CI", "Close-out", "Main", "Next"):
            self.assertIn(f"| {area} |", result.stdout)
        calls = (self.state / "calls").read_text()
        self.assertIn("notifier.sh remove", calls)
        self.assertNotIn("showrunners.py", calls)
        self.assertNotIn("gh ", calls)
        self.assertNotIn("ssh ", calls)

    def test_wrap_archives_owned_phase_note_and_keeps_other_files(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        owned = self.write_phase_note("alpha", "example", "alpha-unit")
        owned_bytes = owned.read_bytes()
        other = self.write_phase_note("other", "other", "alpha-unit")
        other_bytes = other.read_bytes()
        handwritten = self.phase_table_vault / "showrunner" / "notes.md"
        self.write(handwritten, "kept by a person\n")

        result = self.run_lifecycle("wrap", "--no-ci")

        archived = (
            self.phase_table_vault.parent
            / "archive"
            / "showrunner"
            / "example"
            / "alpha.md"
        )
        self.assert_step(result, "ok")
        self.assertIn("phase-notes: ok — archived 1 phase note", result.stdout)
        self.assertFalse(owned.exists())
        self.assertEqual(archived.read_bytes(), owned_bytes)
        self.assertEqual(other.read_bytes(), other_bytes)
        self.assertEqual(handwritten.read_text(encoding="utf-8"), "kept by a person\n")

    def test_wrap_stays_running_when_phase_note_archive_fails(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        note = self.write_phase_note("alpha", "example", "alpha-unit")
        archive = self.phase_table_vault.parent / "archive"
        self.write(archive, "blocks the archive directory\n")

        result = self.run_lifecycle("wrap", "--no-ci")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^phase-notes: failed — .+")
        self.assertIn("cannot archive phase note", result.stdout)
        self.assertTrue(note.is_file())
        self.assertIn("**Status: PRODUCTION — running.**", self.doc.read_text())
        self.assertIn("notifier.sh remove", self.calls())


if __name__ == "__main__":
    _ = unittest.main()
