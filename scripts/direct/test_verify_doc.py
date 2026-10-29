#!/usr/bin/env python3
"""Judge real Git trees with fake Cargo, without changing the caller's index."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import TypedDict, cast, override
import unittest

VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
HANG_SECONDS = 300


class DocPassRecord(TypedDict):
    repo_top: str
    tree_id: str
    time: str
    call_id: str


class CargoDocCall(TypedDict):
    args: list[str]
    rustdocflags: str
    call_id: str
    cwd: str
    sources: dict[str, str]
    notes_present: bool
    target: str


CARGO_STUB = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

arguments = sys.argv[1:]
if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
elif arguments[:1] == ["doc"]:
    with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
        output.write(json.dumps({"args": arguments, "rustdocflags": os.environ.get("RUSTDOCFLAGS", ""),
                                 "call_id": os.environ.get("BUILDLOG_CALL_ID", ""),
                                 "cwd": str(Path.cwd()),
                                 "target": os.environ.get("CARGO_TARGET_DIR", ""),
                                 "sources": {name: Path(name).read_text(encoding="utf-8")
                                             for name in ("alpha/src/lib.rs", "beta/src/lib.rs")},
                                 "notes_present": Path("notes.md").exists()}) + "\n")
    if os.environ.get("TEST_EDIT_DURING_DOC"):
        with Path(os.environ["TEST_EDIT_DURING_DOC"]).open("a", encoding="utf-8") as source:
            source.write("// edited during rustdoc\n")
        if os.environ.get("TEST_STAGE_DURING_DOC"):
            stage_environment = {name: value for name, value in os.environ.items()
                                 if name not in {"GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_DIR"}}
            subprocess.run(["git", "-C", os.environ["TEST_ORIGINAL_REPO"], "add", "--",
                            os.environ["TEST_EDIT_DURING_DOC"]], check=True, env=stage_environment)
    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))
else:
    raise SystemExit("unexpected cargo invocation: " + repr(arguments))
'''


class VerifyDocTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    records: Path = Path()
    events: Path = Path()
    config: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "workspace"
        self.repo.mkdir()
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/scripts").symlink_to(SCRIPTS, target_is_directory=True)
        self.records = home / ".local/state/verify/doc_pass"
        self.events = self.root / "cargo-events.jsonl"
        self.config = self.root / "lint.conf"
        _ = self.config.write_text("doc=on\nsweep=off\n", encoding="utf-8")
        tools = self.root / "bin"
        tools.mkdir()
        self.executable(tools / "cargo", CARGO_STUB)
        self.executable(tools / "rustc", 'print("rustc fixture")\n')
        targets = tools / "build-targets.py"
        self.executable(targets, "raise SystemExit(0)\n")
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.executable(tools / name, "raise SystemExit(99)\n")
        for name in ("tmp", "state", "cache", "runtime", "target", "session"):
            (self.root / name).mkdir()
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        _ = (self.repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["alpha", "beta"]\nresolver = "2"\n', encoding="utf-8",
        )
        packages: list[dict[str, object]] = []
        for name in ("alpha", "beta"):
            member = self.repo / name
            (member / "src").mkdir(parents=True)
            _ = (member / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
                encoding="utf-8",
            )
            source = member / "src/lib.rs"
            _ = source.write_text("pub fn value() {}\n", encoding="utf-8")
            packages.append({"name": name, "id": name, "features": {"default": [], "extra": []},
                             "manifest_path": str(member / "Cargo.toml"),
                             "targets": [{"name": name, "kind": ["lib"], "test": True,
                                          "src_path": str(source), "required-features": []}]})
        self.environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("BUILD", "UNIT_", "LINT_", "MAC_TEST_", "CARGO_", "RUST",
                                    "NEXTEST_", "CLAUDE_CODE_", "CODEX_", "GIT_"))
        }
        self.environment.update({
            "HOME": str(home), "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_CARGO_EVENTS": str(self.events),
            "TEST_ORIGINAL_REPO": str(self.repo),
            "TEST_METADATA": json.dumps({"workspace_root": str(self.repo),
                                         "target_directory": str(self.root / "target"),
                                         "workspace_members": ["alpha", "beta"], "packages": packages}),
            "TMPDIR": str(self.root / "tmp"), "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"), "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CARGO_HOME": str(self.root / "cargo-home"), "CARGO_TARGET_DIR": str(self.root / "target"),
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILDLOG_OFF": "1", "BUILDLOG_SCOPE": "0",
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILD_HOLD_DIR": str(self.root / "build-hold"), "BUILD_TARGETS_SCRIPT": str(targets),
            "LINT_CONFIG_READER": str(SCRIPTS / "lint/lint_config.sh"),
            "LINT_CONFIG_FILE": str(self.config),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })
        _ = self.git("init", "-q", "-b", "test")
        _ = self.git("add", "-A")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "base")

    def executable(self, path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        path.chmod(0o755)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.environment,
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    def verify(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "doc", *arguments], cwd=self.repo,
                              env=self.environment, capture_output=True, text=True, check=False,
                              timeout=HANG_SECONDS)

    def pass_records(self) -> list[Path]:
        return list(self.records.rglob("*.json")) if self.records.exists() else []

    def cargo_calls(self) -> list[CargoDocCall]:
        return [cast(CargoDocCall, json.loads(line))
                for line in self.events.read_text(encoding="utf-8").splitlines()]

    def test_doc_records_the_complete_judged_tree_and_preserves_the_index(self) -> None:
        source = self.repo / "alpha/src/lib.rs"
        _ = source.write_text("pub fn staged() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        expected_tree = self.git("write-tree")
        _ = source.write_text("pub fn unstaged() {}\n", encoding="utf-8")
        other = self.repo / "beta/src/lib.rs"
        _ = other.write_text("pub fn unrelated_unstaged() {}\n", encoding="utf-8")
        _ = (self.repo / "notes.md").write_text("untracked plan edit\n", encoding="utf-8")
        _ = (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        _ = (self.repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        index_before = (self.repo / ".git/index").read_bytes()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.assertEqual(source.read_text(encoding="utf-8"), "pub fn unstaged() {}\n")
        self.assertEqual(other.read_text(encoding="utf-8"), "pub fn unrelated_unstaged() {}\n")
        records = self.pass_records()
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = cast(DocPassRecord, json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual(record["repo_top"], str(self.repo))
        self.assertEqual(record["tree_id"], expected_tree)
        self.assertIn(expected_tree, records[0].name)
        self.assertTrue(record["time"])
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(Path(calls[0]["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(calls[0]["sources"], {
            "alpha/src/lib.rs": "pub fn staged() {}\n", "beta/src/lib.rs": "pub fn value() {}\n",
        })
        self.assertFalse(calls[0]["notes_present"])
        self.assertEqual(record["call_id"], calls[0]["call_id"])
        self.assertTrue(record["call_id"])
        self.assertIn("--workspace", calls[0]["args"])
        self.assertIn("--no-deps", calls[0]["args"])
        self.assertNotIn("-p", calls[0]["args"])
        self.assertIn("-D warnings", calls[0]["rustdocflags"])

    def test_doc_failure_leaves_no_durable_pass(self) -> None:
        self.environment["TEST_DOC_STATUS"] = "41"
        result = self.verify()
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual(self.pass_records(), [])

    def test_doc_reuses_checkout_and_target_preserving_unchanged_mtimes(self) -> None:
        removed = self.repo / "deleted.txt"
        _ = removed.write_text("remove in second tree\n", encoding="utf-8")
        _ = self.git("add", "deleted.txt")
        first = self.verify()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.cargo_calls()[0]
        checkout = Path(initial["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "target/verify-doc"))
        self.assertEqual(initial["target"], str(self.root / "target"))
        self.assertTrue((checkout / "deleted.txt").is_file())
        # Set a distinct old mtime: this proves preservation without a sleep or
        # dependence on the filesystem's clock resolution between doc runs.
        unchanged = checkout / "beta/src/lib.rs"
        stamp = 1_000_000_000_000_000_000
        os.utime(unchanged, ns=(stamp, stamp))
        _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn next_tree() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        removed.unlink()
        _ = self.git("add", "-u", "--", "deleted.txt")
        second = self.verify()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = self.cargo_calls()[1]
        self.assertEqual(current["cwd"], initial["cwd"])
        self.assertEqual(current["target"], initial["target"])
        self.assertEqual(unchanged.stat().st_mtime_ns, stamp, second.stdout + second.stderr)
        self.assertEqual(current["sources"]["alpha/src/lib.rs"], "pub fn next_tree() {}\n")
        self.assertFalse((checkout / "deleted.txt").exists())

    def test_doc_target_defaults_to_repo_metadata_and_reuses_checkout(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(self.root / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertEqual(second["cwd"], first["cwd"])

    def test_doc_recovers_a_missing_or_damaged_private_index(self) -> None:
        for damage in ("missing", "corrupt"):
            with self.subTest(damage=damage):
                before = self.verify()
                self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
                checkout = Path(self.cargo_calls()[-1]["cwd"])
                index = checkout.parent / "index"
                self.assertTrue(index.is_file())
                if damage == "missing":
                    index.unlink()
                else:
                    _ = index.write_text("damaged index\n", encoding="utf-8")
                _ = (checkout / "beta/src/lib.rs").write_text("damaged checkout\n", encoding="utf-8")
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                call = self.cargo_calls()[-1]
                self.assertEqual(call["cwd"], str(checkout))
                self.assertEqual(call["sources"]["beta/src/lib.rs"], "pub fn value() {}\n")

    def test_edits_during_doc_leave_no_pass_even_without_a_worker_session(self) -> None:
        self.environment["TEST_STAGE_DURING_DOC"] = "1"
        for name in ("alpha/src/lib.rs", "new-plan.md"):
            with self.subTest(path=name):
                self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / name)
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [], result.stdout + result.stderr)

    def test_unstaged_edits_during_doc_do_not_invalidate_the_staged_verdict(self) -> None:
        staged_tree = self.git("write-tree")
        self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / "alpha/src/lib.rs")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.pass_records()
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = cast(DocPassRecord, json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual(record["tree_id"], staged_tree)
        self.assertIn("edited during rustdoc", (self.repo / "alpha/src/lib.rs").read_text(encoding="utf-8"))

    def test_doc_off_still_runs_the_workspace_check_and_explains_once(self) -> None:
        _ = self.config.write_text("doc=off\nsweep=off\n", encoding="utf-8")
        result = self.verify("--features", "alpha/extra")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--workspace", calls[0]["args"])
        features_at = calls[0]["args"].index("--features")
        self.assertEqual(calls[0]["args"][features_at + 1], "alpha/extra")
        notices = [line for line in (result.stdout + result.stderr).splitlines()
                   if "cannot be switched off" in line]
        self.assertEqual(len(notices), 1, result.stdout + result.stderr)
        self.assertEqual(len(self.pass_records()), 1)

    def test_doc_pass_survives_session_cleanup_and_prunes_only_old_records(self) -> None:
        self.records.mkdir(parents=True)
        old = self.records / ("0" * 40 + ".json")
        recent = self.records / ("1" * 40 + ".json")
        for path in (old, recent):
            _ = path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - 15 * 24 * 60 * 60
        os.utime(old, (stamp, stamp))
        self.environment["UNIT_SESSION_DIR"] = str(self.root / "session")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        records = [path for path in self.pass_records() if path != recent]
        self.assertEqual(len(records), 1)
        _ = (self.root / "session").rename(self.root / "ended-session")
        self.assertTrue(records[0].exists())


if __name__ == "__main__":
    _ = unittest.main()
