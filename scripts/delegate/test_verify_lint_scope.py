#!/usr/bin/env python3
"""Check delegate lint scope with synthetic Cargo metadata and real lint policy."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, final, override
import unittest


VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
# A hang limit, not a speed check. One `verify.sh lint` call starts about 60 processes: 10 s at
# load 75 on natedev, and over 30 s at load 160 (two merge gates went red at 30 s, 2026-10-18).
# A real hang still fails, after this many seconds.
HANG_SECONDS = 300


class CargoInvocation(TypedDict):
    args: list[str]
    call_id: str


class TaploInvocation(TypedDict):
    args: list[str]
    cwd: str


class ClippyReplayRound(TypedDict):
    stream: str
    status: int
    before: dict[str, str]


CargoTargetMetadata = TypedDict("CargoTargetMetadata", {
    "name": str,
    "kind": list[str],
    "test": bool,
    "src_path": str,
    "required-features": list[str],
})


class CargoPackageMetadata(TypedDict):
    name: str
    id: str
    features: dict[str, list[str]]
    manifest_path: str
    targets: list[CargoTargetMetadata]


class CargoWorkspaceMetadata(TypedDict):
    workspace_root: str
    workspace_members: list[str]
    packages: list[CargoPackageMetadata]


CARGO_STUB = r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps({"args": arguments, "call_id": os.environ.get("BUILDLOG_CALL_ID", "")}) + "\n")
if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
elif arguments[:1] not in (["mend"], ["clippy"], ["doc"], ["+nightly"]):
    raise SystemExit("unexpected cargo invocation: " + repr(arguments))
# A file changes while this stage runs: mend's own rewrite, or a writer's edit.
if arguments[:1] == [os.environ.get("TEST_EDIT_DURING", "")]:
    with Path(os.environ["PACKAGE_ROOT"], "src", "lib.rs").open("a", encoding="utf-8") as source:
        source.write("// changed during " + arguments[0] + "\n")
if arguments[:1] == ["clippy"]:
    events = [json.loads(line)["args"] for line in
              Path(os.environ["TEST_CARGO_EVENTS"]).read_text(encoding="utf-8").splitlines()]
    if "--message-format=json" in arguments:
        replay_index = sum("--message-format=json" in args for args in events) - 1
        rounds = json.loads(os.environ.get("TEST_CLIPPY_JSON_ROUNDS", "[]"))
        if rounds:
            if replay_index >= len(rounds):
                raise SystemExit("unexpected extra JSON replay")
            replay = rounds[replay_index]
            for name, expected in replay["before"].items():
                if Path(name).read_text(encoding="utf-8") != expected:
                    raise SystemExit("JSON replay did not read the preceding round's edits")
            print(replay["stream"])
            raise SystemExit(replay["status"])
        if replay_index == 0:
            print(os.environ["TEST_CLIPPY_JSON"])
            raise SystemExit(int(os.environ.get("TEST_CLIPPY_JSON_STATUS", "101")))
        print(json.dumps({"reason": "build-finished", "success": True}))
        raise SystemExit(0)
    judgments = sum(args[:1] == ["clippy"] and "--message-format=json" not in args for args in events)
    status = int(os.environ.get("TEST_CLIPPY_STATUS", "0"))
    if judgments > 1:
        status = int(os.environ.get("TEST_CLIPPY_SECOND_STATUS", str(status)))
        for name, expected in json.loads(os.environ.get("TEST_REWRITTEN_SOURCES", "{}")).items():
            if Path(name).read_text(encoding="utf-8") != expected:
                raise SystemExit("final judgment did not read all expected rewrites")
    diagnostic = os.environ.get("TEST_CLIPPY_DIAGNOSTIC", "")
    if status and diagnostic:
        wording = "can" if diagnostic == "can" else "may"
        print(f"error: multiply and add expressions {wording} be calculated more efficiently and accurately", file=sys.stderr)
        lint = "suboptimal_flops" if diagnostic in ("may", "can") else "unreadable_literal"
        print("  = help: https://rust-lang.github.io/rust-clippy/master/index.html#" + lint, file=sys.stderr)
    raise SystemExit(status)
if arguments[:2] == ["+nightly", "fmt"] and "--version" not in arguments:
    events = [json.loads(line)["args"] for line in
              Path(os.environ["TEST_CARGO_EVENTS"]).read_text(encoding="utf-8").splitlines()]
    if any("--message-format=json" in args for args in events):
        for name, expected in json.loads(os.environ.get("TEST_REWRITTEN_SOURCES", "{}")).items():
            if Path(name).read_text(encoding="utf-8") != expected:
                raise SystemExit("format ran before the multiply-add suggestions were applied")
'''


TAPLO_STUB = r'''
import json
import os
from pathlib import Path
import sys

with Path(os.environ["TEST_TAPLO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps({"args": sys.argv[1:], "cwd": str(Path.cwd())}) + "\n")
status = int(os.environ["TEST_TAPLO_STATUS"])
if status == 101:
    print("thread 'main' panicked", file=sys.stderr)
elif status:
    print("error: Cargo.toml is not formatted", file=sys.stderr)
raise SystemExit(status)
'''


GIT_STUB = r'''
import os
import sys

arguments = sys.argv[1:]
if "rev-parse" in arguments:
    if "--show-toplevel" in arguments:
        if "--git-common-dir" in arguments:
            print(os.environ["PACKAGE_ROOT"] + "/.git")
        print(os.environ["PACKAGE_ROOT"])
        if "--abbrev-ref" in arguments:
            print("main")
    elif "HEAD^{tree}" in arguments:
        print("fixed-tree")
    elif "--short" in arguments:
        print("abc1234")
    else:
        raise SystemExit(1)
elif "diff" in arguments and "--name-only" in arguments:
    sys.stdout.write("src/lib.rs\0")
elif "status" in arguments:
    if os.environ.get("TEST_GIT_DIRTY"):
        sys.stdout.write(" M src/lib.rs\0")
elif "ls-files" not in arguments:
    raise SystemExit(1)
'''


@final
class VerifyLintScopeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.events = Path()
        self.taplo_events = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = self.root / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (claude / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        self.events = self.root / "cargo-events.jsonl"
        self.taplo_events = self.root / "taplo-events.jsonl"
        self.config = self.root / "lint.conf"
        self.write_config(clippy="on")
        self.write_executable(bin_directory / "cargo", CARGO_STUB)
        self.write_executable(bin_directory / "taplo", TAPLO_STUB)
        self.write_executable(bin_directory / "git", GIT_STUB)
        self.write_executable(bin_directory / "rustc", 'print("rustc fixture")\n')
        targets = self.root / "build-targets.py"
        self.write_executable(targets, "raise SystemExit(0)\n")
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_executable(bin_directory / name, "raise SystemExit(99)\n")
        for directory in ("tmp", "state", "cache", "runtime", "target", "cargo-home", ".git"):
            (self.root / directory).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n',
            encoding="utf-8",
        )
        _ = (self.root / "taplo.toml").write_text(
            'include = ["**/*.toml"]\n', encoding="utf-8",
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
        metadata: CargoWorkspaceMetadata = {
            "workspace_root": str(self.root),
            "workspace_members": ["sample-id"],
            "packages": [{
                "name": "sample",
                "id": "sample-id",
                "features": {"default": [], "a": []},
                "manifest_path": str(self.root / "Cargo.toml"),
                "targets": [
                    {"name": "sample", "kind": ["lib"], "test": True,
                     "src_path": str(source), "required-features": []},
                    {"name": "plain", "kind": ["example"], "test": False,
                     "src_path": str(self.root / "examples" / "plain.rs"),
                     "required-features": []},
                    {"name": "featured", "kind": ["example"], "test": False,
                     "src_path": str(self.root / "examples" / "featured.rs"),
                     "required-features": ["a"]},
                ],
            }],
        }
        self.environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("BUILD", "PLAN_DELEGATE_", "LINT_", "MAC_TEST_",
                                    "CARGO_", "RUST", "NEXTEST_", "CLAUDE_CODE_", "CODEX_"))
        }
        self.environment.update({
            "HOME": str(home),
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PACKAGE_ROOT": str(self.root),
            "TEST_CARGO_EVENTS": str(self.events),
            "TEST_TAPLO_EVENTS": str(self.taplo_events),
            "TEST_TAPLO_STATUS": "0",
            "TEST_METADATA": json.dumps(metadata),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILDLOG_MEMINFO": str(self.root / "missing-meminfo"),
            "MUL_ADD_REWRITE_LOG_DIR": str(self.root / "rewrite-logs"),
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_TARGETS_SCRIPT": str(targets),
            "LINT_CONFIG_READER": str(SCRIPTS / "lint" / "lint_config.sh"),
            "LINT_CONFIG_FILE": str(self.config),
        })

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def write_config(self, *, clippy: str) -> None:
        _ = self.config.write_text(
            f"mend=on\nfmt=on\nclippy={clippy}\ndoc=on\nsweep=off\n",
            encoding="utf-8",
        )

    def enable_cache(self) -> Path:
        session = self.root / "session"
        session.mkdir()
        self.environment["PLAN_DELEGATE_SESSION_DIR"] = str(session)
        return session / "verify_cache"

    def isolate_path_without_taplo(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        for name in (
            "bash", "python3", "awk", "cat", "cut", "date", "dirname", "env", "grep",
            "head", "mkdir", "mktemp", "mv", "ps", "rm", "sed", "sleep",
            "sort", "tail", "tee", "tr", "uname", "wc",
        ):
            executable = shutil.which(name)
            if executable is None:
                self.fail(f"fixture requires {name}")
            (tools / name).symlink_to(executable)
        (self.root / "bin" / "taplo").unlink()
        self.environment["PATH"] = f"{self.root / 'bin'}{os.pathsep}{tools}"

    def verify(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self.invoke_verify(*arguments, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def invoke_verify(
        self, *arguments: str, cwd: Path, taplo_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        self.events.unlink(missing_ok=True)
        self.taplo_events.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(VERIFY), *arguments], cwd=cwd,
            env={**self.environment, "TEST_TAPLO_STATUS": str(taplo_status)},
            capture_output=True, text=True, check=False, timeout=HANG_SECONDS,
        )

    def taplo_calls(self) -> list[TaploInvocation]:
        if not self.taplo_events.exists():
            return []
        return [cast(TaploInvocation, json.loads(line))
                for line in self.taplo_events.read_text(encoding="utf-8").splitlines()]

    def assert_workspace_taplo_check(self) -> None:
        self.assertEqual(self.taplo_calls(), [{"args": ["fmt", "--check"], "cwd": str(self.root)}])

    def calls(self, command: str) -> list[list[str]]:
        return [args[1:] for args in self.cargo_calls() if args[:1] == [command]]

    def cargo_calls(self) -> list[list[str]]:
        events = [cast(CargoInvocation, json.loads(line))
                  for line in self.events.read_text(encoding="utf-8").splitlines()]
        return [event["args"] for event in events]

    def configure_mul_add_failure(self, *, wording: str = "may") -> dict[str, str]:
        sources = {
            "src/lib.rs": (
                "pub fn product(a: f64, b: f64, c: f64) -> f64 { a * b + c }\n"
                "pub fn offset(x: f64, y: f64, z: f64) -> f64 { x + y * z }\n"
            ),
            "src/other.rs": "pub fn other(a: f64, b: f64, c: f64) -> f64 { a * b + c }\n",
        }
        expected: dict[str, str] = {}
        records: list[dict[str, object]] = []
        for name, source in sources.items():
            _ = (self.root / name).write_text(source, encoding="utf-8")
            rewritten = source
            children: list[dict[str, object]] = []
            for old, replacement in (("a * b + c", "a.mul_add(b, c)"), ("x + y * z", "y.mul_add(z, x)")):
                if old not in source:
                    continue
                start = source.encode().index(old.encode())
                children.append({"spans": [{
                    "file_name": name, "byte_start": start, "byte_end": start + len(old.encode()),
                    "suggested_replacement": replacement, "suggestion_applicability": "MachineApplicable",
                }]})
                rewritten = rewritten.replace(old, replacement)
            expected[str(self.root / name)] = rewritten
            records.append({"reason": "compiler-message", "message": {
                "code": {"code": "clippy::suboptimal_flops"}, "spans": [], "children": children,
            }})
        records.append({"reason": "build-finished", "success": False})
        self.environment.update({
            "TEST_CLIPPY_STATUS": "41",
            "TEST_CLIPPY_SECOND_STATUS": "0",
            "TEST_CLIPPY_DIAGNOSTIC": wording,
            "TEST_CLIPPY_JSON": "\n".join(json.dumps(record) for record in records),
            "TEST_CLIPPY_JSON_STATUS": "101",
            "TEST_REWRITTEN_SOURCES": json.dumps(expected),
        })
        return expected

    def assert_rewrite_clippy_calls(self, *, replays: int = 2) -> None:
        judgment = ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
        rewrite = [
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--message-format=json",
            "--", "-D", "warnings",
        ]
        self.assertEqual(self.calls("clippy"), [judgment, *([rewrite] * replays), judgment])

    def replay_round(
        self, name: str, source: str, replacements: list[tuple[str, str]],
    ) -> ClippyReplayRound:
        spans: list[dict[str, object]] = []
        for old, replacement in replacements:
            start = source.encode().index(old.encode())
            spans.append({
                "file_name": name, "byte_start": start, "byte_end": start + len(old.encode()),
                "suggested_replacement": replacement, "suggestion_applicability": "MachineApplicable",
            })
        records: list[dict[str, object]] = [{"reason": "compiler-message", "message": {
            "code": {"code": "clippy::suboptimal_flops"}, "spans": [], "children": [{"spans": spans}],
        }}, {"reason": "build-finished", "success": False}]
        return {"stream": "\n".join(json.dumps(record) for record in records), "status": 101,
                "before": {str(self.root / name): source}}

    def configure_replay_rounds(
        self, sources: dict[str, str], rounds: list[ClippyReplayRound], expected: dict[str, str],
    ) -> None:
        _ = self.configure_mul_add_failure()
        for name, source in sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(source, encoding="utf-8")
        self.environment.update({
            "BUILDLOG_OFF": "0",
            "TEST_CLIPPY_JSON_ROUNDS": json.dumps(rounds),
            "TEST_REWRITTEN_SOURCES": json.dumps({str(self.root / name): source
                                                   for name, source in expected.items()}),
        })

    def saved_rewrite_lines(self) -> list[str]:
        events = [cast(CargoInvocation, json.loads(line))
                  for line in self.events.read_text(encoding="utf-8").splitlines()]
        call_ids = {event["call_id"] for event in events if "--message-format=json" in event["args"]}
        self.assertEqual(len(call_ids), 1)
        call_id = call_ids.pop()
        self.assertTrue(call_id)
        path = Path(self.environment["MUL_ADD_REWRITE_LOG_DIR"]) / f"{call_id}.log"
        return path.read_text(encoding="utf-8").splitlines()

    def test_chained_sum_replays_the_skipped_inner_term_and_saves_both_rounds(self) -> None:
        name = "src/lib.rs"
        original = "fn sum() -> f64 { a * x + b * y + c * z }\n"
        outer = original.replace("a * x + b * y + c * z", "c.mul_add(z, a * x + b * y)")
        final = outer.replace("a * x + b * y", "a.mul_add(x, b * y)")
        rounds = [
            self.replay_round(name, original, [
                ("a * x + b * y + c * z", "c.mul_add(z, a * x + b * y)"),
                ("a * x + b * y", "a.mul_add(x, b * y)"),
            ]),
            self.replay_round(name, outer, [("a * x + b * y", "a.mul_add(x, b * y)")]),
            self.replay_round(name, final, []),
        ]
        self.configure_replay_rounds({name: original}, rounds, {name: final})
        result = self.verify("lint", "sample")
        self.assert_rewrite_clippy_calls(replays=3)
        self.assertEqual((self.root / name).read_text(encoding="utf-8"), final)
        reports = ["verify.sh: rewrote 1 multiply-add(s) in 1 file(s) (1 skipped: overlapping)",
                   "verify.sh: rewrote 1 multiply-add(s) in 1 file(s)"]
        self.assertEqual(self.saved_rewrite_lines(), reports)
        self.assertEqual([line for line in result.stdout.splitlines()
                          if line.startswith("verify.sh: rewrote")], reports)
        self.assertEqual([args for args in self.calls("+nightly") if "--version" not in args],
                         [["fmt", "-p", "sample"], ["fmt", "-p", "sample"]])
        self.assertNotIn("changed nothing", result.stdout)
        self.assertNotIn("after 5 rounds", result.stdout)

    def test_downstream_crate_first_reported_in_second_round_is_rewritten(self) -> None:
        first, downstream = "src/lib.rs", "downstream/src/lib.rs"
        original = "fn sum() -> f64 { a * b + c }\n"
        final = original.replace("a * b + c", "a.mul_add(b, c)")
        rounds = [self.replay_round(first, original, [("a * b + c", "a.mul_add(b, c)")]),
                  self.replay_round(downstream, original, [("a * b + c", "a.mul_add(b, c)")]),
                  self.replay_round(downstream, final, [])]
        rounds[1]["before"][str(self.root / first)] = final
        self.configure_replay_rounds({first: original, downstream: original}, rounds,
                                     {first: final, downstream: final})
        _ = self.verify("lint", "sample")
        self.assert_rewrite_clippy_calls(replays=3)
        for name in (first, downstream):
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), final)
        self.assertEqual(self.saved_rewrite_lines(), [
            "verify.sh: rewrote 1 multiply-add(s) in 1 file(s)",
            "verify.sh: rewrote 1 multiply-add(s) in 1 file(s)",
        ])

    def test_five_successful_rounds_stop_at_the_cap_then_format_and_judge(self) -> None:
        name = "src/lib.rs"
        expression = "a * b + (" * 5 + "c" + ")" * 5
        source = original = f"fn sum() -> f64 {{ {expression} }}\n"
        rounds: list[ClippyReplayRound] = []
        for depth in range(5, 0, -1):
            old = "a * b + (" * depth + "c" + ")" * depth
            inner = "a * b + (" * (depth - 1) + "c" + ")" * (depth - 1)
            replacement = f"a.mul_add(b, {inner})"
            rounds.append(self.replay_round(name, source, [(old, replacement)]))
            source = source.replace(old, replacement)
        self.configure_replay_rounds({name: original}, rounds, {name: source})
        result = self.verify("lint", "sample")
        self.assert_rewrite_clippy_calls(replays=5)
        self.assertEqual((self.root / name).read_text(encoding="utf-8"), source)
        self.assertEqual(result.stdout.splitlines().count(
            "verify.sh: stopped the multiply-add rewrite after 5 rounds"), 1)
        self.assertEqual(self.saved_rewrite_lines(), [
            "verify.sh: rewrote 1 multiply-add(s) in 1 file(s)",
        ] * 5)
        self.assertEqual([args for args in self.calls("+nightly") if "--version" not in args],
                         [["fmt", "-p", "sample"], ["fmt", "-p", "sample"]])

    def test_later_round_failure_keeps_edits_and_still_formats_and_judges(self) -> None:
        for failure in ("replay", "incomplete", "missing_file"):
            with self.subTest(failure=failure):
                name = "src/lib.rs"
                original = "fn sum() -> f64 { a * b + c }\n"
                final = original.replace("a * b + c", "a.mul_add(b, c)")
                first = self.replay_round(name, original, [("a * b + c", "a.mul_add(b, c)")])
                later = self.replay_round(name, final, [])
                if failure == "replay":
                    later["status"] = 44
                elif failure == "incomplete":
                    later["stream"] = later["stream"].rsplit("\n", 1)[0]
                else:
                    later["stream"] = self.replay_round(
                        "missing.rs", original, [("a * b + c", "a.mul_add(b, c)")],
                    )["stream"]
                self.configure_replay_rounds({name: original}, [first, later], {name: final})
                final_status = 0 if failure == "replay" else 43
                self.environment["TEST_CLIPPY_SECOND_STATUS"] = str(final_status)
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, final_status, result.stdout + result.stderr)
                self.assert_rewrite_clippy_calls()
                self.assertEqual((self.root / name).read_text(encoding="utf-8"), final)
                self.assertIn("verify.sh: clippy's multiply-add rewrite failed; see the output above",
                              (result.stdout + result.stderr).splitlines())
                self.assertEqual(self.saved_rewrite_lines(), [
                    "verify.sh: rewrote 1 multiply-add(s) in 1 file(s)",
                ])
                self.assertEqual([args for args in self.calls("+nightly") if "--version" not in args],
                                 [["fmt", "-p", "sample"], ["fmt", "-p", "sample"]])
                self.assertEqual(len(self.calls("doc")), 1 if final_status == 0 else 0)

    def test_macro_body_suggestion_stays_unchanged_without_a_second_judgment(self) -> None:
        _ = self.configure_mul_add_failure()
        source = "macro_rules! sum { () => { a * b + c }; }\n"
        _ = (self.root / "src/lib.rs").write_text(source, encoding="utf-8")
        replay = self.replay_round("src/lib.rs", source, [("a * b + c", "a.mul_add(b, c)")])
        replay["stream"] = replay["stream"].replace('"MachineApplicable"', '"MaybeIncorrect"')
        self.environment["TEST_CLIPPY_JSON_ROUNDS"] = json.dumps([replay])
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual((self.root / "src/lib.rs").read_text(encoding="utf-8"), source)
        self.assertEqual(["--message-format=json" in args for args in self.calls("clippy")], [False, True])
        self.assertTrue(all("--keep-going" in args for args in self.calls("clippy")))
        self.assertIn("verify.sh: clippy's multiply-add rewrite changed nothing", result.stdout.splitlines())
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
        self.assertEqual(self.calls("doc"), [])

    def test_mul_add_failure_rewrites_formats_and_judges_again(self) -> None:
        for wording, json_status, features in (("may", "101", []), ("can", "0", ["--features", "a"])):
            with self.subTest(wording=wording, json_status=json_status):
                expected = self.configure_mul_add_failure(wording=wording)
                self.environment["TEST_CLIPPY_JSON_STATUS"] = json_status
                result = self.verify("lint", "sample", *features)
                self.assert_rewrite_clippy_calls()
                stages = [args[:2] if args[0] == "+nightly" else args[:1]
                          for args in self.cargo_calls()
                          if args[0] in ("clippy", "doc") or
                          (args[:2] == ["+nightly", "fmt"] and "--version" not in args)]
                self.assertEqual(stages, [
                    ["+nightly", "fmt"], ["clippy"], ["clippy"], ["clippy"],
                    ["+nightly", "fmt"], ["clippy"], ["doc"],
                ])
                self.assertEqual(self.calls("+nightly")[-1], ["fmt", "-p", "sample"])
                self.assertEqual(result.stdout.splitlines().count(
                    "verify.sh: rewrote 3 multiply-add(s) in 2 file(s)"), 1)
                for name, source in expected.items():
                    self.assertEqual(Path(name).read_text(encoding="utf-8"), source)
                self.assert_workspace_taplo_check()

    def test_failure_without_mul_add_link_keeps_status_without_rewrite(self) -> None:
        self.environment.update({"TEST_CLIPPY_STATUS": "42", "TEST_CLIPPY_DIAGNOSTIC": "other"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.calls("doc"), [])
        self.assertNotIn("verify.sh: rewrote", result.stdout)

    def test_second_judgment_failure_keeps_its_status_and_stops(self) -> None:
        _ = self.configure_mul_add_failure()
        self.environment["TEST_CLIPPY_SECOND_STATUS"] = "43"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 43, result.stdout + result.stderr)
        self.assert_rewrite_clippy_calls()
        self.assertEqual(self.calls("doc"), [])

    def test_zero_rewrites_keep_first_status_without_second_judgment(self) -> None:
        _ = self.configure_mul_add_failure()
        self.environment["TEST_CLIPPY_JSON"] = json.dumps({"reason": "build-finished", "success": True})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual(["--message-format=json" in args for args in self.calls("clippy")], [False, True])
        self.assertIn("verify.sh: clippy's multiply-add rewrite changed nothing",
                      (result.stdout + result.stderr).splitlines())
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
        self.assertEqual(self.calls("doc"), [])

    def test_failed_json_replay_keeps_first_status_and_leaves_sources_untouched(self) -> None:
        for failure in ("status", "incomplete"):
            with self.subTest(failure=failure):
                expected = self.configure_mul_add_failure()
                original = {name: Path(name).read_bytes() for name in expected}
                if failure == "status":
                    self.environment["TEST_CLIPPY_JSON_STATUS"] = "44"
                else:
                    self.environment["TEST_CLIPPY_JSON"] = self.environment["TEST_CLIPPY_JSON"].rsplit("\n", 1)[0]
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
                self.assertEqual(["--message-format=json" in args for args in self.calls("clippy")], [False, True])
                self.assertIn("verify.sh: clippy's multiply-add rewrite failed; see the output above",
                              (result.stdout + result.stderr).splitlines())
                self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
                self.assertEqual(self.calls("doc"), [])
                self.assertNotIn("verify.sh: rewrote", result.stdout)
                for name, source in original.items():
                    self.assertEqual(Path(name).read_bytes(), source)

    def test_applier_failure_keeps_first_verdict_without_second_judgment(self) -> None:
        _ = self.configure_mul_add_failure()
        (self.root / "src/other.rs").unlink()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertIn("verify.sh: clippy's multiply-add rewrite failed; see the output above",
                      (result.stdout + result.stderr).splitlines())
        self.assertEqual(["--message-format=json" in args for args in self.calls("clippy")], [False, True])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
        self.assertEqual(self.calls("doc"), [])

    def test_rewrite_count_is_saved_under_its_buildlog_call_id(self) -> None:
        _ = self.configure_mul_add_failure()
        self.environment["BUILDLOG_OFF"] = "0"
        _ = self.verify("lint", "sample")
        events = [cast(CargoInvocation, json.loads(line))
                  for line in self.events.read_text(encoding="utf-8").splitlines()]
        call_ids = {event["call_id"] for event in events if "--message-format=json" in event["args"]}
        self.assertEqual(len(call_ids), 1)
        call_id = call_ids.pop()
        self.assertTrue(call_id)
        logs = list(Path(self.environment["MUL_ADD_REWRITE_LOG_DIR"]).glob("*.log"))
        self.assertEqual([path.name for path in logs], [f"{call_id}.log"])
        self.assertEqual(logs[0].read_text(encoding="utf-8"),
                         "verify.sh: rewrote 3 multiply-add(s) in 2 file(s)\n")

    def test_failed_count_log_save_does_not_fail_lint(self) -> None:
        _ = self.configure_mul_add_failure()
        self.environment["BUILDLOG_OFF"] = "0"
        parent = self.root / "blocked-log-directory"
        _ = parent.write_text("a file prevents creating the log directory", encoding="utf-8")
        self.environment["MUL_ADD_REWRITE_LOG_DIR"] = str(parent / "logs")
        result = self.verify("lint", "sample")
        self.assert_rewrite_clippy_calls()
        self.assertIn("verify.sh: rewrote 3 multiply-add(s) in 2 file(s)", result.stdout.splitlines())
        self.assertEqual(len(self.calls("doc")), 1)

    def test_rewritten_tree_is_cached_after_second_judgment(self) -> None:
        cache = self.dirty_cache()
        _ = self.configure_mul_add_failure()
        result = self.verify("lint", "sample")
        self.assert_rewrite_clippy_calls()
        self.assertNotIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

    def test_example_mul_add_failure_never_runs_rewrite(self) -> None:
        _ = self.configure_mul_add_failure()
        result = self.invoke_verify("example", "sample", "plain", cwd=self.root)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--example", "plain", "--", "-D", "warnings",
        ]])
        self.assertNotIn("verify.sh: rewrote", result.stdout)

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(self.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
        ])
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
        self.assertEqual(self.calls("doc"), [["--no-deps", "--jobs", "8", "-p", "sample"]])

    def test_features_reach_mend_and_doc_but_do_not_narrow_clippy(self) -> None:
        _ = self.verify("lint", "sample", "--features", "a")
        self.assert_workspace_taplo_check()
        self.assertEqual(self.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
        ])
        self.assertEqual(self.calls("mend"), [[
            "--all-targets", "--workspace", "--fix", "--features", "sample/a"
        ]])
        self.assertEqual(self.calls("doc"), [[
            "--no-deps", "--jobs", "8", "-p", "sample", "--features", "sample/a"
        ]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])

    def test_example_keeps_its_single_target_and_required_features(self) -> None:
        for example, features in (("plain", []), ("featured", ["--features", "sample/a"])):
            with self.subTest(example=example):
                _ = self.verify("example", "sample", example)
                self.assertEqual(self.calls("clippy"), [[
                    "--workspace", "--example", example, *features, "--", "-D", "warnings"
                ]])
                self.assertEqual(self.calls("mend"), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual(self.calls("+nightly"), [])
                self.assertEqual(self.taplo_calls(), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        _ = self.configure_mul_add_failure()
        for features in ([], ["--features", "a"]):
            with self.subTest(features=features):
                result = self.verify("lint", "sample", *features)
                skip_lines = [line for line in result.stdout.splitlines()
                              if line.startswith("SKIPPED:") and "clippy" in line]
                self.assertEqual(skip_lines, [
                    "SKIPPED: cargo clippy --workspace --all-targets --all-features"
                    + f" — clippy=off in {self.config}"
                ])
                self.assertEqual(self.calls("clippy"), [])
                self.assertEqual(len(self.calls("mend")), 1)
                self.assertEqual(len(self.calls("doc")), 1)
                self.assert_workspace_taplo_check()

    def test_taplo_uses_workspace_config_when_lint_starts_in_a_member_directory(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()

    def test_taplo_failure_fails_lint_and_names_the_unformatted_file(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src", taplo_status=7)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)

    def test_missing_taplo_fails_before_stages_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.isolate_path_without_taplo()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["taplo is required"])
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("+nightly"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.taplo_calls(), [])
        self.assertEqual(list(cache.glob("*.fail")), [])

        self.write_executable(self.root / "bin" / "taplo", TAPLO_STUB)
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_linux_is_a_tooling_failure_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_executable(self.root / "bin" / "uname", 'print("Linux")\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("tooling failure", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_macos_requests_unsandboxed_retry_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_executable(self.root / "bin" / "uname", 'print("Darwin")\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("macOS sandbox", result.stderr)
        self.assertIn("Re-run the same command with the sandbox disabled", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_unformatted_toml_remains_a_cached_tree_failure(self) -> None:
        cache = self.enable_cache()
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.fail"))), 1)

        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL (recorded)", result.stdout)
        self.assertEqual(self.taplo_calls(), [])

    def dirty_cache(self) -> Path:
        """Turn the cache on with src/lib.rs uncommitted, so its content is in the tree key."""
        self.environment["TEST_GIT_DIRTY"] = "1"
        return self.enable_cache()

    def clippy_ran(self) -> bool:
        return self.events.exists() and bool(self.calls("clippy"))

    def test_edit_while_clippy_fails_records_nothing_and_the_next_lint_runs(self) -> None:
        cache = self.dirty_cache()
        self.environment.update({"TEST_EDIT_DURING": "clippy", "TEST_CLIPPY_STATUS": "101"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran, so this result was not recorded", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])

        self.environment.update({"TEST_EDIT_DURING": "", "TEST_CLIPPY_STATUS": "0"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("(recorded)", result.stdout)
        self.assertTrue(self.clippy_ran())
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_edit_while_clippy_passes_records_nothing(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "clippy"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])

    def test_mend_rewrite_is_recorded_under_the_tree_it_leaves(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "mend"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        self.environment["TEST_EDIT_DURING"] = ""
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


@final
class InvokeOutputTests(unittest.TestCase):
    def function_body(self, name: str) -> str:
        source = (SCRIPTS / "lint" / "invoke.sh").read_text(encoding="utf-8")
        match = re.search(rf"^{name}\(\) [{{(]\n.*?^[}})]$", source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, name)
        assert match is not None
        return match.group()

    def shell(self, script: str, root: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ, HOME=str(root), TMPDIR=str(root))
        return subprocess.run(
            ["bash", "-c", script], cwd=root, env=environment,
            capture_output=True, text=True, check=False, timeout=HANG_SECONDS,
        )

    def command_stubs(self) -> str:
        return r'''
set -euo pipefail
SANDBOX_SIGNATURE=not-a-sandbox-failure
buildlog_step_compiles() { return 1; }
buildlog_begin() { :; }
buildlog_exec() { "$@"; }
buildlog_finish_memory() { :; }
buildlog_end() { :; }
sweep_after_step() { :; }
'''

    def test_json_stdout_stays_separate_and_both_logs_are_complete_on_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = self.command_stubs() + self.function_body("run_json_stdout") + "\n"
            script += self.function_body("run_once") + r'''
buildlog_end() { cp "$2" inner-complete.log; }
emit() {
    printf '{"reason":"build-finished"}\n'
    local i
    for ((i=0; i<1000; i++)); do printf 'cargo stderr %s\n' "$i" >&2; done
    return 101
}
LINT_JSON_STDOUT=1
if run_json_stdout outer.log run_once emit; then status=0; else status=$?; fi
cp outer.log outer-complete.log
exit "$status"
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
            self.assertEqual(result.stdout, '{"reason":"build-finished"}\n')
            errors = "".join(f"cargo stderr {index}\n" for index in range(1000))
            self.assertEqual(result.stderr, "+ emit\n" + errors)
            for name in ("inner-complete.log", "outer-complete.log"):
                logged = (root / name).read_text(encoding="utf-8")
                self.assertIn(result.stdout, logged)
                self.assertEqual([line for line in logged.splitlines() if line.startswith("cargo stderr")],
                                 errors.splitlines())
            self.assertIn("+ emit\n", (root / "outer-complete.log").read_text(encoding="utf-8"))

    def test_concurrent_json_and_stderr_keep_complete_lines_in_both_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = self.command_stubs() + self.function_body("run_json_stdout") + "\n"
            script += self.function_body("run_once") + r'''
buildlog_end() { cp "$2" inner-complete.log; }
emit() {
    local i
    for ((i=0; i<1000; i++)); do
        printf '{"diagnostic":%s,"message":"concurrent output"}\n' "$i"
        printf 'cargo stderr %s\n' "$i" >&2
    done
    return 101
}
LINT_JSON_STDOUT=1
if run_json_stdout outer.log run_once emit; then status=0; else status=$?; fi
cp outer.log outer-complete.log
exit "$status"
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
            messages = [f'{{"diagnostic":{index},"message":"concurrent output"}}'
                        for index in range(1000)]
            errors = [f"cargo stderr {index}" for index in range(1000)]
            self.assertEqual(result.stdout.splitlines(), messages)
            self.assertEqual(result.stderr.splitlines(), ["+ emit", *errors])
            for name, count in (("inner-complete.log", 2000), ("outer-complete.log", 2001)):
                with self.subTest(log=name):
                    lines = (root / name).read_text(encoding="utf-8").splitlines()
                    self.assertEqual([line for line in lines if line.startswith("{")], messages)
                    self.assertEqual([line for line in lines if line.startswith("cargo stderr")], errors)
                    self.assertEqual(len(lines), count)

    def test_shell_libraries_use_no_named_descriptor_redirections(self) -> None:
        for path in (SCRIPTS / "lint" / "invoke.sh", VERIFY):
            with self.subTest(path=path):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\{[A-Za-z_][A-Za-z_0-9]*\}[<>]")

    def assert_command_status_with_failed_tee(self, *, json_stdout: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            tee = binary / "tee"
            _ = tee.write_text("#!/bin/sh\ncat\nexit 7\n", encoding="utf-8")
            tee.chmod(0o755)
            for status in (0, 47):
                with self.subTest(status=status):
                    script = self.command_stubs() + self.function_body("run_json_stdout") + "\n"
                    script += self.function_body("run_once")
                    script += f"\nLINT_JSON_STDOUT={int(json_stdout)}\n"
                    script += f'\nexport PATH="$PWD/bin:$PATH"\nif run_once bash -c "exit {status}"; then result=0; else result=$?; fi\nexit "$result"\n'
                    result = self.shell(script, root)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_ordinary_step_returns_command_status_when_tee_fails(self) -> None:
        self.assert_command_status_with_failed_tee(json_stdout=False)

    def test_json_step_returns_command_status_when_tee_fails(self) -> None:
        self.assert_command_status_with_failed_tee(json_stdout=True)


if __name__ == "__main__":
    _ = unittest.main()
