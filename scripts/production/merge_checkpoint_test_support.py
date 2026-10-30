"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock
from pathlib import Path
from typing import ClassVar, NamedTuple, cast, override

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_checkpoint
import change_review
from scripts.direct import progress_history
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.production.cli_fixture import invoke_cli
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def invoke_checkpoint(arguments: list[str], environment: dict[str, str], cwd: Path,
                      root: Path) -> subprocess.CompletedProcess[str]:
    """Run the real CLI and current fake scripts, keeping Git as real processes."""
    real_run = subprocess.run

    def run(command: list[str], *, capture_output: bool = False, text: bool = False,
            check: bool = False, timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: Path | str | None = None, stdout: int | None = None,
            stderr: int | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
        child_env = dict(os.environ) if env is None else env
        child_cwd = Path.cwd() if cwd is None else Path(cwd)
        executable = Path(shutil.which(command[0], path=child_env.get("PATH")) or command[0])
        script, args = executable, command[1:]
        if not script.is_relative_to(root) and executable.name.startswith("python") and len(command) > 1:
            script, args = Path(command[1]), command[2:]
        if executable.name != "git" and script.is_relative_to(root) and script.is_file() and script.read_text().startswith(f"#!{sys.executable}\n"):
            fake = ModuleType("checkpoint_tool_fixture")
            fake.__file__ = str(script)

            def main() -> int:
                exec(compile(script.read_text(), str(script), "exec"), {**vars(fake), "__name__": "__main__"})
                return 0

            return invoke_cli(fake, main, args, child_env, child_cwd)
        if script == Path(progress_history.__file__):
            return invoke_recorder(args, child_env, child_cwd)
        return real_run(command, capture_output=capture_output, text=text, check=check,
                        timeout=timeout, env=env, cwd=cwd, stdout=stdout, stderr=stderr, input=input)

    with mock.patch.object(subprocess, "run", run), mock.patch.object(
        change_review, "LISTS", Path(environment["HOME"]) / "rust/hanadocs/change-review",
    ):
        return invoke_cli(merge_checkpoint, merge_checkpoint.main, arguments, environment, cwd,
                          production_state=True)


# Script completion deadlines only bound hangs; startup can be slow under load.

SCRIPT = Path(__file__).with_name("merge_checkpoint.py")
STARTED = "2026-10-06T12:00:00-07:00"
TRIAL = ("review trial: ux 2 findings, code 3 findings, review-worker minutes 14, "
         "ux check minutes 4, ux repair minutes 1")
SIMPLE_OWNS = "`src/alpha.txt`, `crates/alpha/`, `docs/plans/alpha.md`, `docs/plans/alpha-next.md`"
REAL_OWNS = ("`docs/plans/build-followups-enh-showrunner.md`; `scripts/production/add_unit.py`, "
             "`test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; "
             "`commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: "
             "`commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, "
             "`scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests "
             "(hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): "
             "`commands/unit/direct.md`, `commands/unit/checkpoint.md`, "
             "`docs/direct/run_phase_review.md`, `docs/production_format.md`, "
             "`commands/unit/add_ons.md`, `commands/plan/shrink.md`, "
             "`commands/plan/phase_review.md`, `docs/direct/phase_end.md`, "
             "`docs/direct_plan_format.md`, `commands/unit/eta_breakdown.md`, "
             "`docs/direct/final_gate_commit.md`")
SIMPLE_MERGE_TESTS = "- **Merge tests:** `checkpoint-test`"
REAL_MERGE_TESTS = ("- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` "
                    "and `basedpyright scripts/buildlog` (pass is its `0 errors, 0 warnings` line: it exits 3 "
                    "because pyrightconfig names a `.venv` no checkout has), run in the showrunner checkout")
DELIVERS = "Delivers checkpoint merge behavior."
PARALLEL = "Parallel: none — no phase is waiting"
STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["CHECKPOINT_TEST_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as output:
    output.write(json.dumps({"command": name, "args": args}) + "\\n")
if name == "ssh":
    print("rc=" + (state / "mac-rc").read_text().strip() if (state / "mac-rc").exists() else "rc=0")
    raise SystemExit(0)
if name == "style_sync.py":
    import subprocess
    repo = args[args.index("--repo-dir") + 1]
    branch = args[args.index("--branch") + 1]
    observed = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/" + branch],
                              capture_output=True, text=True, check=True).stdout.split()[0]
    (state / "notified-tip").write_text(observed)
if name == "verify.sh":
    fail_rows = state / "fail-rows"
    if fail_rows.exists() and "--filter" not in args:
        print(fail_rows.read_text(), end="")
        raise SystemExit(1)
    key = "_".join(args).replace("/", "_")
    if args[:1] == ["test"]:
        package_key = "test-" + args[1] if len(args) > 1 else "test"
        package_marker = state / ("fail-once-" + package_key)
        if package_marker.exists():
            package_marker.unlink()
            raise SystemExit(1)
        if (state / ("fail-always-" + package_key)).exists():
            raise SystemExit(1)
        key = "test"
    marker = state / ("fail-once-" + key)
    if marker.exists():
        marker.unlink()
        raise SystemExit(1)
    if (state / ("fail-always-" + key)).exists():
        raise SystemExit(1)
if (state / ("fail-" + name)).exists():
    raise SystemExit(1)
if name == "basedpyright":
    print("0 errors, 0 warnings, 0 notes", flush=True)
    print("venv .venv subdirectory not found in venv path " + os.getcwd() + ".", file=sys.stderr)
    raise SystemExit(3)
'''


def configure_test_environment() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them. The
    scratch folders a merge removes are in a temporary folder, never /tmp/claude/scratch or ~/rust/repro."""
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        "USER_ZONE": "America/Los_Angeles", "SCRATCH_ROOT": f"{folder.name}/scratch",
        "SCRATCH_VISIBLE_ROOT": f"{folder.name}/repro"}))


class MergeResult(NamedTuple):
    process: subprocess.CompletedProcess[str]
    before: str


class MergeCheckpointFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.origin: Path = Path()
        self.checkout: Path = Path()
        self.unit: Path = Path()
        self.other: Path = Path()
        self.promote: Path = Path()
        self.doc: Path = Path()
        self.log: Path = Path()
        self.bin: Path = Path()
        self.state: Path = Path()
        self.env: dict[str, str] = {}
        self.push: str = "git"
        self.promotions: str = ""
        self.known_flakes: str = ""
        self.known_flaky_tests: str = ""
        self.gate: bool = True

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="checkpoint-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        builder.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        # Git's linked worktree metadata names absolute paths. Each case gets
        # private files, refs and origin restored at the seed's original path.
        if self._testMethodName == "runTest" and self.fixture_root.exists():
            # The provenance matrices create sequential nested fixtures. Their
            # outer fixture is unused; each matrix cell restores a fresh seed.
            shutil.rmtree(self.fixture_root)
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)

        def cleanup() -> None:
            if self.fixture_root.exists():
                shutil.rmtree(self.fixture_root)

        self.addCleanup(cleanup)
        self.bind_fixture(self.fixture_root)

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "hana"
        self.unit = self.root / "alpha"
        self.other = self.root / "beta"
        self.promote = self.root / "installed"
        self.doc = self.root / "example-production.md"
        self.log = self.checkout / "production.log"
        self.bin = self.root / "bin"
        self.state = self.root / "stub-state"
        home = self.root / "home"
        self.env = {**os.environ, "HOME": str(home),
                    "MERGE_CHECKPOINT_STYLE_SYNC": str(self.bin / "style_sync.py"),
                    "MERGE_CHECKPOINT_STYLE_LEDGER": str(self.bin / "style_ledger.py"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "CHECKPOINT_TEST_STATE": str(self.state),
                    "MERGE_CHECKPOINT_SCRATCH": str(self.state),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunners"),
                    "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                    "MERGE_CHECKPOINT_RUN_ROOT": str(self.root / "unit-runs"),
                    "UNIT_HISTORY_DIR": str(self.root / "history"), "UNIT_SYNC_WRITES": "0",
                    "CLAUDE_PROBE_SESSION": ""}

    def build_fixture(self) -> None:
        self.root.mkdir()
        self.bin.mkdir()
        self.state.mkdir()
        self.checkout.mkdir()
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.checkout)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.checkout)
        _ = self.git("remote", "add", "origin", str(self.origin), cwd=self.checkout)
        self.write(self.checkout, "README.md", "base\n")
        self.write(self.checkout, ".gitignore", "production.log\n")
        self.write(self.checkout, "src/alpha.txt", "alpha base\n")
        self.write(self.checkout, "src/beta.txt", "beta base\n")
        _ = self.git("add", ".", cwd=self.checkout)
        _ = self.git("commit", "-m", "initial", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "main", cwd=self.checkout)
        _ = self.git("branch", "production", cwd=self.checkout)
        _ = self.git("checkout", "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "production", cwd=self.checkout)
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.unit), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "alpha-branch", cwd=self.unit)
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.other), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "beta-branch", cwd=self.other)
        _ = self.git("worktree", "add", str(self.promote), "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.unit)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.unit)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.other)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.other)
        for name in ("verify.sh", "checkpoint-test", "validate_and_push.sh", "review_regime.py", "ssh",
                     "style_sync.py", "style_ledger.py"):
            self.executable(self.bin / name, STUB)
        home = self.root / "home"
        for relative, name in ((".claude/scripts/direct", "verify.sh"),
                               (".claude/scripts/validate_and_push", "validate_and_push.sh"),
                               (".claude/scripts/production", "review_regime.py")):
            directory = home / relative
            directory.mkdir(parents=True)
            self.executable(directory / name, STUB)
        self.production_doc()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def write(self, checkout: Path, relative: str, content: str) -> None:
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def executable(self, path: Path, content: str) -> None:
        content = content.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
        _ = path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def production_doc(self, *, owns_cell: str = SIMPLE_OWNS,
                       merge_tests_line: str | None = SIMPLE_MERGE_TESTS,
                       hub_files: bool = True, change_review: Path | None = None) -> None:
        rows = [
            "# Production — example", "", "## Production Context", "",
            f"- **Repository:** `{self.checkout}`",
            "- **Merge branch:** `production`",
            *([f"- **Change review:** `{change_review}`"] if change_review else []),
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
        ]
        if merge_tests_line is not None:
            rows.append(merge_tests_line)
        if self.push:
            rows.append(f"- **Push:** {self.push}")
        if self.promotions:
            rows.append(f"- **Promote:** {self.promotions}")
        if self.known_flakes:
            rows.append(f"- **Known flakes:** {self.known_flakes}")
        if self.known_flaky_tests:
            rows.append(f"- **Known flaky tests:** {self.known_flaky_tests}")
        rows.extend([
            "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/plans/alpha.md` | `{self.unit}` | `alpha-branch` | alpha | — | {owns_cell} |",
            f"| `beta-unit` | `docs/plans/beta.md` | `{self.other}` | `beta-branch` | beta | — | `src/beta.txt` |",
            "", "## Gates", "",
            "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
        ])
        if hub_files:
            hub_start = rows.index("## Gates") - 1
            rows[hub_start:hub_start] = [
                "## Hub files", "",
                "| File | Owner unit | Other units that touch it |",
                "| --- | --- | --- |",
                "| `shared.txt` | beta-unit | alpha-unit |", "",
            ]
        if self.gate:
            rows.append("| G1 | beta-unit Phase 4 | alpha-unit Phase 2 | checkpoint merged |")
        rows.extend(["", "## Production rules", "", "- Merge each phase and push the merge branch."])
        _ = self.doc.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def checkpoint(self, relative: str = "src/alpha.txt", content: str = "alpha change\n",
                   *, subject: str = "alpha work", push: bool = True) -> str:
        self.write(self.unit, relative, content)
        _ = self.git("add", relative, cwd=self.unit)
        _ = self.git("commit", "-m", subject, cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        if push:
            _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return sha

    def run_checkpoint(self, sha: str, *extra: str, phase: str = "2",
                       review: bool = True, trial: str = TRIAL,
                       delivers: str | None = DELIVERS, parallel: str | None = PARALLEL) -> MergeResult:
        before = self.git("rev-parse", "HEAD")
        arguments = ["--production", str(self.doc), "alpha-unit", phase, sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--trailer", "Co-authored-by: Test <test@example.invalid>"]
        if review:
            arguments.extend(("--review-trial", trial))
        if parallel is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--parallel", parallel))
        if delivers is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--delivers", delivers))
        arguments.extend(extra)
        process = self.invoke(arguments)
        return MergeResult(process, before)

    def events(self, command: str) -> list[list[str]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [cast(list[str], row["args"]) for row in rows if row["command"] == command]

    def assert_stopped_before_merge(self, result: MergeResult, state: str, detail: str) -> None:
        self.assertIn(f": {state} —", result.process.stdout, result.process.stdout + result.process.stderr)
        self.assertIn(detail, result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.events("review_regime.py"), [])
        self.assertIn("send alpha-unit:", result.process.stdout)

    def receipts(self) -> list[dict[str, object]]:
        path = self.root / "history" / "merge_receipts.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def record_history(self, *arguments: str) -> dict[str, object]:
        result = invoke_recorder(arguments, self.env, self.unit)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout)) if arguments[0] == "waits" else {}

    def start_history_run(self, name: str, worktree: Path) -> Path:
        session = self.root / "unit-runs" / name
        _ = self.record_history("start-run", "--session-dir", str(session), "--working-dir", str(worktree),
                                "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "director")
        return session

    def assert_checkpoint_provenance(self, *, across_runs: bool, second_active: bool, checkpoint_events: int) -> None:
        checkpoint = self.checkpoint()
        first = self.start_history_run("first", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(first), "--phase-id", "2", "--phase-title", "First")
        if checkpoint_events:
            _ = self.record_history("checkpoint", "--session-dir", str(first), "--phase", "2", "--commit", checkpoint)
        _ = self.record_history("finish-phase", "--session-dir", str(first), "--phase-id", "2", "--status", "completed")
        if across_runs:
            _ = self.record_history("finish-run", "--session-dir", str(first), "--status", "completed")
        second = self.start_history_run("second", self.unit) if across_runs else first
        _ = self.record_history("start-phase", "--session-dir", str(second), "--phase-id", "2", "--phase-title", "Second")
        if checkpoint_events == 2:
            _ = self.record_history("checkpoint", "--session-dir", str(second), "--phase", "2", "--commit", checkpoint)
        if not second_active:
            _ = self.record_history("finish-phase", "--session-dir", str(second), "--phase-id", "2", "--status", "completed")
        merged = self.run_checkpoint(checkpoint)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        report = self.record_history("waits")
        phases = cast(list[dict[str, object]], report["phases"])
        waits = cast(list[dict[str, object]], report["waits"])
        self.assertEqual(len(phases), 2)
        if checkpoint_events == 1:
            self.assertEqual(len(waits), 1)
            self.assertEqual((waits[0]["run_id"], waits[0]["phase_instance_id"]),
                             (phases[0]["run_id"], phases[0]["phase_instance_id"]))
            self.assertEqual([phase["wait_n"] for phase in phases], [1, 0])
        else:
            self.assertEqual(waits, [])
            self.assertEqual(report["missing_record_n"], 1)
            for phase in phases:
                self.assertEqual(phase["state"], "UncertainCoverage")
                self.assertNotIn("wait_n", phase)
                self.assertTrue(phase["reason"])
                self.assertTrue(phase["candidates"])

    def assert_real_owns_path_allowed(self, relative: str) -> None:
        self.production_doc(owns_cell=REAL_OWNS, hub_files=False)
        sha = self.checkpoint(relative, relative + "\n", subject="own " + relative)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok", result.process.stdout)

    def dated_checkpoint(self, message: str) -> str:
        """A checkpoint(...) commit on the unit branch, dated after the Change-Review rule landed."""
        self.write(self.unit, "src/alpha.txt", message)
        _ = self.git("add", "src/alpha.txt", cwd=self.unit)
        _ = subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.unit, check=True,
                           env={**os.environ, "GIT_COMMITTER_DATE": "2026-10-15T12:00:00-04:00"})
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def as_built_commit(self, *extra: str) -> str:
        """Land the plan through a shrink, then delete it beside a new as-built doc."""
        shrink = self.checkpoint("docs/plans/alpha.md", "plan\n", subject="shrink")
        landed = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(landed.process.returncode, 0, landed.process.stderr + landed.process.stdout)
        _ = self.git("rm", "-q", "docs/plans/alpha.md", cwd=self.unit)
        self.write(self.unit, "docs/as-built/alpha.md", "as built\n")
        for path in extra:
            self.write(self.unit, path, "extra\n")
        _ = self.git("add", "docs/as-built/alpha.md", *extra, cwd=self.unit)
        _ = self.git("commit", "-m", "docs(alpha): as-built", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def child_production(self, *, parent: str = "example") -> Path:
        doc = self.root / "example-style-production.md"
        _ = doc.write_text("\n".join([
            "# Style production", "- **Kind:** style", "- **Branch point:** production",
            "- **Merge branch:** alpha-branch", f"- **Showrunner checkout:** {self.unit}",
            "- **Log:** style.log", "", "## Units", "",
        ]))
        _ = (self.state / "child-parent").write_text(parent)
        registry = self.bin / "showrunners.py"
        self.executable(registry, "\n".join([
            "#!/usr/bin/env python3", "import sys", "from pathlib import Path",
            f"state = Path({str(self.state)!r})",
            "if sys.argv[1] == 'parent': print((state / 'child-parent').read_text())",
            f"elif sys.argv[1] == 'list': print('example-style\\t{doc}')",
            "elif sys.argv[1] == 'address': print('session:fake-child')",
            "else: raise SystemExit(2)", "",
        ]))
        self.executable(self.bin / "send.py", STUB)
        self.env["MERGE_CHECKPOINT_SHOWRUNNERS"] = str(registry)
        self.env["MERGE_CHECKPOINT_SEND"] = str(self.bin / "send.py")
        return doc

    def run_handoff(self, checkpoint: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.invoke(["--from-child", "example-style", "--checkpoint", checkpoint,
                            "--production", str(self.doc), "--started", STARTED, *extra])

    def prepare_handoff_ledger(self, checkpoint: str, *, add_handoff: bool = True) -> None:
        ledger = Path(__file__).resolve().parents[1] / "style/style_ledger.py"
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(ledger)
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "style-state")
        catalog = self.root / "rule-catalog"
        catalog.mkdir()
        self.env["STYLE_HISTORY_NATE_STYLE_DIR"] = str(catalog)
        commands = [["reconcile", "--branch-point", "production", "--style-branch", "alpha-branch"]]
        if add_handoff:
            commands.extend([
                ["handoff", "add", "--checkpoint", checkpoint],
                ["handoff", "send", "--checkpoint", checkpoint, "--to", "example"],
            ])
        for arguments in commands:
            result = subprocess.run(
                [sys.executable, str(ledger), *arguments, "--repo", str(self.unit)],
                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def handoff_ledger_row(self, checkpoint: str) -> tuple[str, str]:
        database = self.root / "style-state" / self.checkout.name / "ledger.sqlite"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT state,merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchall())
        self.assertEqual(len(rows), 1)
        return rows[0]

    def invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        # Keep real executable coverage for both the unit and child entry paths.
        if self._testMethodName in (
            "test_checkpoint_receipt_names_pushed_merge_and_source_once_on_retry",
            "test_attached_child_handoff_merges_without_a_units_row_and_acknowledges_after_push",
        ):
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False,
                                  timeout=TEST_HANG_GUARD_SECS)
        return invoke_checkpoint(arguments, self.env, self.checkout, self.root)
