"""Scaffolding resumes only destinations owned by an unfinished generation."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase


class RustGenerationRerunTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.write(self.home / "rust/rust-template/cargo-generate.toml", "[template]\n")
        _ = self.write(self.home / ".claude/templates/settings_local.json", "{}\n")
        shim = self.write(self.home / ".claude/scripts/lib/py", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)
        self.environment["TEST_REMOTE"] = str(self.remote)
        self.environment["TEST_SCRIPTS_ROOT"] = str(Path(__file__).resolve().parents[2])
        self.environment["TEST_CARGO_CALLS"] = str(self.root / "cargo-calls")
        for command in ("cargo-generate", "cargo-mend", "taplo"):
            _ = self.stub(command, "pass\n")
        _ = self.stub("cargo", '''import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["TEST_SCRIPTS_ROOT"])
from scripts.lib.rerun_test_support import initialize_repository
args = sys.argv[1:]
with Path(os.environ["TEST_CARGO_CALLS"]).open("a") as handle:
    handle.write(" ".join(args) + "\\n")
if args[0] == "generate":
    name = args[args.index("--name") + 1]
    dest = Path(args[args.index("--destination") + 1]) / name
    dest.mkdir()
    (dest / "src").mkdir()
    (dest / "src/main.rs").write_text("fn main() {}\\n")
    (dest / "Cargo.toml").write_text('[package]\\nname = "' + name + '"\\nversion = "0.1.0"\\n')
    if "--vcs" not in args:
        initialize_repository(dest, Path(os.environ["TEST_REMOTE"]), os.environ)
elif args[0] == "add":
    failure = Path(os.environ["HOME"]) / "fail-add"
    if failure.exists():
        failure.unlink()
        sys.exit(86)
    manifest = Path("Cargo.toml")
    text = manifest.read_text()
    if "bevy =" not in text:
        manifest.write_text(text + '[dependencies]\\nbevy = "0"\\n')
elif args[0] == "generate-lockfile":
    Path("Cargo.lock").write_text("# stub lock\\n")
elif args[0] not in ("clippy", "+nightly"):
    sys.exit("unexpected cargo command")
''')

    def member_arguments(self) -> tuple[str, ...]:
        _ = self.write(self.repo / "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n[workspace.package]\nrepository = "https://example.invalid/local"\n[workspace.dependencies]\n')
        self.seed()
        return ("member", "--workspace-root", str(self.repo), "--keywords", "test", "--categories", "development-tools", "--shared-dep")

    def test_an_interrupted_manifest_publish_preserves_the_workspace_for_rerun(self) -> None:
        arguments = self.member_arguments()
        manifest = self.repo / "Cargo.toml"
        original = manifest.read_bytes()
        self.stop_command("mv", target=str(manifest))
        stopped = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertEqual(manifest.read_bytes(), original)
        finished = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(manifest.read_text().count('member = { path = "crates/member" }'), 1)

    def test_an_interrupted_settings_publish_leaves_no_partial_destination_and_reruns(self) -> None:
        settings = '{"permissions": {"allow": ["Read"]}}\n'
        _ = self.write(self.home / ".claude/templates/settings_local.json", settings)
        self.stop_command("mv", target=".claude/settings.local.json")
        stopped = self.script("new_rust_project/rust_generate.sh", "sample", "--no-bevy")
        self.assertNotEqual(stopped.returncode, 0)
        dest = self.home / "rust/sample"
        self.assertFalse((dest / ".claude/settings.local.json").exists())
        finished = self.script("new_rust_project/rust_generate.sh", "sample", "--no-bevy")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual((dest / ".claude/settings.local.json").read_text(), settings)
        self.assertNotIn("rust-generate", self.git("-C", str(dest), "ls-files").stdout)

    def test_an_options_record_cannot_claim_a_foreign_standalone_destination(self) -> None:
        self.assert_foreign_destination_refused(("sample",), self.home / "rust/sample")

    def test_an_options_record_cannot_claim_a_foreign_workspace_member(self) -> None:
        arguments = self.member_arguments()
        self.assert_foreign_destination_refused(arguments, self.repo / "crates/member")

    def assert_foreign_destination_refused(self, arguments: tuple[str, ...], dest: Path) -> None:
        # The state is durable, but no generated directory has been published.
        self.stop_command("mv", target=str(dest.parent / f".{dest.name}.rust-generate"), after=True)
        stopped = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertFalse(dest.exists())
        _ = self.write(dest / "Cargo.toml", '[package]\nname = "foreign"\n')
        _ = self.write(dest / "src/keep.rs", "foreign bytes\n")
        before = {path.relative_to(dest): path.read_bytes() for path in dest.rglob("*") if path.is_file()}
        refused = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already exists", refused.stderr)
        self.assertEqual({path.relative_to(dest): path.read_bytes() for path in dest.rglob("*") if path.is_file()}, before)

    def test_a_generated_standalone_project_resumes_dependencies_and_its_initial_commit(self) -> None:
        _ = self.write(self.home / "fail-add", "stop\n")
        stopped = self.script("new_rust_project/rust_generate.sh", "sample")
        self.assertNotEqual(stopped.returncode, 0)
        dest = self.home / "rust/sample"
        self.assertTrue((dest / "Cargo.toml").exists())
        self.fail_git("commit", after=True)
        stopped = self.script("new_rust_project/rust_generate.sh", "sample")
        self.assertNotEqual(stopped.returncode, 0)
        finished = self.script("new_rust_project/rust_generate.sh", "sample")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(self.git("-C", str(dest), "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual((dest / ".git/info/exclude").read_text().count("settings.local.json\n"), 1)
        self.assertEqual((dest / "Cargo.toml").read_text().count("bevy ="), 1)
        self.assertEqual(sum(line.startswith("generate ") for line in (self.root / "cargo-calls").read_text().splitlines()), 1)
        self.assertFalse((dest.parent / ".sample.rust-generate").exists())
        self.assertFalse((dest / ".rust-generate-owner").exists())
        self.assertNotIn("rust-generate", self.git("-C", str(dest), "ls-files").stdout)

    def test_a_generated_workspace_member_resumes_after_its_commit_without_committing_twice(self) -> None:
        arguments = self.member_arguments()
        self.fail_git("commit", after=True)
        stopped = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertNotEqual(stopped.returncode, 0)
        committed = self.git("rev-parse", "HEAD").stdout
        finished = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, committed)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "2")
        self.assertEqual((self.repo / "Cargo.toml").read_text().count('member = { path = "crates/member" }'), 1)
        self.assertFalse((self.repo / "crates/.member.rust-generate").exists())
        self.assertFalse((self.repo / "crates/member/.rust-generate-owner").exists())
        self.assertNotIn("rust-generate", self.git("ls-files").stdout)

    def test_a_completed_standalone_project_finishes_interrupted_ownership_cleanup(self) -> None:
        self.assert_completed_cleanup_resumes(("sample",), self.home / "rust/sample", 1)

    def test_a_completed_workspace_member_finishes_interrupted_ownership_cleanup(self) -> None:
        self.assert_completed_cleanup_resumes(self.member_arguments(), self.repo / "crates/member", 2)

    def assert_completed_cleanup_resumes(self, arguments: tuple[str, ...], dest: Path, expected_commits: int) -> None:
        token = dest / ".rust-generate-owner"
        record = dest.parent / f".{dest.name}.rust-generate"
        self.stop_command("rm", target=str(token), after=True)
        stopped = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertFalse(token.exists())
        self.assertEqual(record.read_text().splitlines()[-1], "completed")
        commit_repo = dest if (dest / ".git").exists() else self.repo
        committed = self.git("-C", str(commit_repo), "rev-parse", "HEAD").stdout
        cargo_calls = (self.root / "cargo-calls").read_bytes()
        finished = self.script("new_rust_project/rust_generate.sh", *arguments)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertFalse(token.exists())
        self.assertFalse(record.exists())
        self.assertEqual(self.git("-C", str(commit_repo), "rev-parse", "HEAD").stdout, committed)
        self.assertEqual(self.git("-C", str(commit_repo), "rev-list", "--count", "HEAD").stdout.strip(), str(expected_commits))
        self.assertEqual((self.root / "cargo-calls").read_bytes(), cargo_calls)

    def test_an_existing_destination_without_generation_ownership_is_still_refused(self) -> None:
        dest = self.home / "rust/sample"
        _ = self.write(dest / "keep.txt", "user content\n")
        result = self.script("new_rust_project/rust_generate.sh", "sample")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((dest / "keep.txt").read_text(), "user content\n")
