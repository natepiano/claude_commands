"""Judge real Git trees with fake Cargo, without changing the caller's index."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from typing import ClassVar, TypedDict, cast, override
import unittest

from scripts.lint.memory_test_support import fake_memory_readings

VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


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
import shutil
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


class VerifyDocFixture(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    records: Path = Path()
    events: Path = Path()
    config: Path = Path()
    environment: dict[str, str] = {}

    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="verify-doc-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture_root = Path(temporary.name) / "case"
        cls.fixture_seed = Path(temporary.name) / "seed"
        cls.fixture_root.mkdir()
        builder = cls()
        builder.root = cls.fixture_root
        builder.build_fixture()
        cls.fixture_environment = builder.environment.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        # Dev-feature tests also instantiate this fixture without setUpClass.
        if "fixture_seed" not in type(self).__dict__ or not self.fixture_seed.exists():
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="verify-doc-test-")))
            self.build_fixture()
            return
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = self.root / "workspace"
        self.records = self.root / "home/.local/state/verify/doc_pass"
        self.events = self.root / "cargo-events.jsonl"
        self.config = self.root / "lint.conf"
        self.environment = self.fixture_environment.copy()

    def build_fixture(self) -> None:
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
        self.executable(tools / "rustc", '#!/bin/sh\nprintf "rustc fixture\\n"\n')
        targets = tools / "build-targets.py"
        self.executable(targets, 'import sys\nif sys.argv[1] == "eligible": print("natedev_only test target\\nnatedev\\teligible\\t-\\t-")\n')
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.executable(tools / name, "#!/bin/sh\nexit 99\n")
        for name in ("tmp", "state", "cache", "runtime", "target", "session"):
            (self.root / name).mkdir()
        memory_environment = fake_memory_readings(self.root)
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
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_CARGO_EVENTS": str(self.events),
            "TEST_ORIGINAL_REPO": str(self.repo),
            "TEST_METADATA": json.dumps({"workspace_root": str(self.repo),
                                         "target_directory": str(self.root / "target"),
                                         "workspace_members": ["alpha", "beta"], "packages": packages}),
            "TMPDIR": str(self.root / "tmp"), "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"), "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CARGO_HOME": str(self.root / "cargo-home"), "CARGO_TARGET_DIR": str(self.root / "target"),
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILDLOG_OFF": "1", "BUILDLOG_SCOPE": "0",
            **memory_environment,
            "BUILD_HOLD_DIR": str(self.root / "build-hold"), "BUILD_TARGETS_SCRIPT": str(targets),
            "LINT_CONFIG_READER": str(SCRIPTS / "lint/lint_config.sh"),
            "LINT_CONFIG_FILE": str(self.config),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })
        # These are doc-policy tests, not interpreter-discovery tests. Exercise
        # the real shim's cache hit instead of revalidating Python every call.
        cache = self.root / "cache/claude/python3-3.11"
        cache.parent.mkdir()
        _ = cache.write_text(sys.executable + "\n", encoding="utf-8")
        _ = self.environment.pop("CLAUDE_PYTHON", None)
        _ = self.git("init", "-q", "-b", "test")
        _ = self.git("add", "-A")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "base")

    def executable(self, path: Path, source: str) -> None:
        _ = path.write_text(source if source.startswith("#!") else f"#!{sys.executable}\n{source}", encoding="utf-8")
        path.chmod(0o755)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.environment,
                              capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    def verify(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "doc", *arguments], cwd=self.repo,
                              env=self.environment, capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def pass_records(self) -> list[Path]:
        return list(self.records.rglob("*.json")) if self.records.exists() else []

    def cargo_calls(self) -> list[CargoDocCall]:
        return [cast(CargoDocCall, json.loads(line))
                for line in self.events.read_text(encoding="utf-8").splitlines()]

    def enable_held_mac_doc(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("private hold")
        settings = self.root / "mac.conf"
        _ = settings.write_text("offload=on\n")
        self.environment["MAC_TEST_CONFIG"] = str(settings)
        self.environment["MAC_IDLE_STATE_DIR"] = str(self.root / "mac-idle")
        self.environment["MAC_TEST_STATE_DIR"] = str(self.root / "mac-state")
        targets = Path(self.environment["BUILD_TARGETS_SCRIPT"])
        self.executable(targets, '''
import os
from pathlib import Path
import sys
if sys.argv[1] == "eligible":
    print("mac_allowed")
    print("natedev\\teligible\\t-\\t-")
    print("mac\\teligible\\t-\\t" + os.environ["MAC_TEST_CONFIG"])
elif sys.argv[1] in ("register-local", "check-local"):
    raise SystemExit(10)
''')
        runner = self.root / "mac-doc.py"
        self.executable(runner, '''
import os
from pathlib import Path
import sys
words = sys.argv[1:]
assert words[words.index("--verb") + 1] == "doc"
assert words[words.index("--repo-root") + 1] == os.environ["TEST_ORIGINAL_REPO"]
os.chdir(words[words.index("--source-root") + 1])
result = "failed" if int(os.environ.get("TEST_DOC_STATUS", "0")) else "passed"
Path(words[words.index("--result") + 1]).write_text("mac=" + result + "\\nseconds=0\\n")
''' + CARGO_STUB.replace('arguments = sys.argv[1:]', 'arguments = ["doc", *words[words.index("--") + 1:]]'))
        self.environment["VERIFY_MAC_RUNNER"] = str(runner)
