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


CARGO_STUB = r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
elif arguments[:1] == ["doc"]:
    with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
        output.write(json.dumps({"args": arguments, "rustdocflags": os.environ.get("RUSTDOCFLAGS", ""),
                                 "call_id": os.environ.get("BUILDLOG_CALL_ID", "")}) + "\n")
    if os.environ.get("TEST_EDIT_DURING_DOC"):
        with Path(os.environ["TEST_EDIT_DURING_DOC"]).open("a", encoding="utf-8") as source:
            source.write("// edited during rustdoc\n")
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
            "TEST_METADATA": json.dumps({"workspace_root": str(self.repo),
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
        _ = source.write_text("pub fn unstaged() {}\n", encoding="utf-8")
        _ = (self.repo / "notes.md").write_text("untracked plan edit\n", encoding="utf-8")
        _ = (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        _ = (self.repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        index_before = (self.repo / ".git/index").read_bytes()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        _ = self.git("add", "-A")
        expected_tree = self.git("write-tree")
        records = self.pass_records()
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = cast(DocPassRecord, json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual(record["repo_top"], str(self.repo))
        self.assertEqual(record["tree_id"], expected_tree)
        self.assertIn(expected_tree, records[0].name)
        self.assertTrue(record["time"])
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 1)
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

    def test_edits_during_doc_leave_no_pass_even_without_a_worker_session(self) -> None:
        for name in ("alpha/src/lib.rs", "new-plan.md"):
            with self.subTest(path=name):
                self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / name)
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [], result.stdout + result.stderr)

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
