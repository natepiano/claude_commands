"""Exercise the watcher's scope with actual git trees and Cargo workspace inputs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import override

from scripts.direct.wait_for_test_state import wait_until


WATCHER = Path(__file__).with_name("tree_watch.py")
WATCH_INTERVAL = 0.5
# Allow process launch and git work on a busy host in addition to one interval.
WATCH_DEADLINE = WATCH_INTERVAL + 2


def write_workspace(root: Path) -> dict[str, object]:
    """A virtual workspace, including an unrelated member and a path dependency."""
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["sample", "dep", "unrelated"]\nresolver = "2"\n',
        "Cargo.lock": "# fixture lock\nversion = 3\n",
        ".cargo/config.toml": "[build]\njobs = 2\n",
        "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n',
        "rust-toolchain": "stable\n",
        "docs/design.md": "external design\n",
        "plans/work.md": "external plan\n",
        "scripts/helper.py": "# external helper\n",
        "session/board.log": "session state\n",
        ".gitignore": "target/\n",
        "sample/fixtures/input.bin": "fixture data\n",
        "sample/src/removed.rs": "// removed input\n",
    }
    packages: list[dict[str, object]] = []
    for name in ("sample", "dep", "unrelated"):
        files[f"{name}/Cargo.toml"] = f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
        files[f"{name}/src/lib.rs"] = "pub fn value() -> u8 { 1 }\n"
        dependencies: list[dict[str, object]] = ([{"name": "dep", "path": str(root / "dep"), "kind": None,
                          "optional": False, "uses_default_features": True, "features": []}]
                        if name == "sample" else [])
        packages.append({"id": f"{name}-id", "name": name, "features": {},
                         "manifest_path": str(root / name / "Cargo.toml"),
                         "dependencies": dependencies,
                         "targets": [{"name": name, "kind": ["lib"], "test": True,
                                      "src_path": str(root / name / "src/lib.rs"),
                                      "required-features": []}]})
    files["sample/Cargo.toml"] += '\n[dependencies]\ndep = { path = "../dep" }\n'
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "workspace fixture"]):
        _ = subprocess.run(["git", "-C", str(root), *args], check=True,
                           capture_output=True, text=True, timeout=10)
    return {"workspace_root": str(root), "workspace_members": [f"{name}-id" for name in
            ("sample", "dep", "unrelated")], "packages": packages,
            "target_directory": str(root / "target"), "metadata": {}}


class TreeWatchTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    metadata: dict[str, object]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.metadata = write_workspace(self.root)
        tools = self.root / "tools"
        tools.mkdir()
        rustc = tools / "rustc"
        _ = rustc.write_text("#!/bin/sh\necho 'rustc fixture'\n")
        rustc.chmod(0o755)
        self.environment = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                            "VERIFY_TREE_WATCH_S": str(WATCH_INTERVAL),
                            "UNIT_SESSION_DIR": str(self.root / "session"),
                            "UNIT_RUN_DIR": str(self.root / "session"),
                            "PYTHONDONTWRITEBYTECODE": "1"}

    def key_arguments(self, package: str = "sample") -> list[str]:
        scope = [] if package == "--workspace" else ["--package", package]
        return [*scope, "--"]

    def key(self, package: str = "sample") -> str:
        result = subprocess.run([sys.executable, str(WATCHER), "--watch-key",
                                 *self.key_arguments(package)], cwd=self.root,
                                input=json.dumps(self.metadata), env=self.environment,
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout.strip(), r"^[a-f0-9]{64}$")
        return result.stdout.strip()

    def start_watcher(self, parent: int, package: str = "sample") -> tuple[subprocess.Popen[str], Path, str]:
        before = self.key(package)
        start = self.root / "start-key"
        fired = self.root / "fired-key"
        fired.unlink(missing_ok=True)
        _ = start.write_text(before + "\n")
        process = subprocess.Popen([sys.executable, str(WATCHER), str(start), str(fired),
                                    str(parent), *self.key_arguments(package)],
                                   cwd=self.root, env=self.environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdin is not None
        _ = process.stdin.write(json.dumps(self.metadata))
        process.stdin.close()
        process.stdin = None
        self.addCleanup(self.stop_watcher, process)
        return process, fired, before

    @staticmethod
    def stop_watcher(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=10)

    def assert_fires(self, process: subprocess.Popen[str], fired: Path, before: str) -> None:
        started = time.monotonic()
        output, errors = process.communicate(timeout=WATCH_DEADLINE)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertLess(time.monotonic() - started, WATCH_DEADLINE)
        self.assertTrue(fired.exists(), "watcher exited without publishing a changed key")
        self.assertNotEqual(fired.read_text().strip(), before)
        self.assertEqual(fired.read_text().strip(), self.key())

    def test_in_scope_edit_table_fires(self) -> None:
        for relative in ("sample/src/lib.rs", "dep/src/lib.rs", "sample/fixtures/input.bin",
                         "Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml", "unrelated/Cargo.toml"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = self.root / relative
                _ = path.write_text(path.read_text() + "\n# changed\n")
                self.assert_fires(process, fired, before)

    def test_out_of_scope_edit_table_does_not_fire(self) -> None:
        for relative in ("docs/design.md", "plans/work.md", "scripts/helper.py",
                         "unrelated/src/lib.rs", "session/board.log"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = self.root / relative
                _ = path.write_text(path.read_text() + "changed\n")
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = process.communicate(timeout=WATCH_INTERVAL * 2 + 0.1)
                self.assertFalse(fired.exists(), "out-of-scope edit cancelled the run")
                self.assertEqual(self.key(), before)
                # Prove the watcher was alive and observing, rather than silently broken.
                source = self.root / "sample/src/lib.rs"
                _ = source.write_text(source.read_text() + "// in scope\n")
                self.assert_fires(process, fired, before)

    def test_added_deleted_and_staged_member_inputs_fire(self) -> None:
        for change in ("untracked", "deleted", "staged", "executable"):
            with self.subTest(change=change):
                process, fired, before = self.start_watcher(os.getpid())
                source = self.root / "sample/src/lib.rs"
                if change == "untracked":
                    _ = (source.parent / "new.rs").write_text("// new input\n")
                elif change == "deleted":
                    (source.parent / "removed.rs").unlink()
                elif change == "executable":
                    source.chmod(0o755)
                else:
                    _ = source.write_text(source.read_text() + "// staged input\n")
                    _ = subprocess.run(["git", "add", "sample/src/lib.rs"], cwd=self.root,
                                       check=True, capture_output=True, timeout=10)
                self.assert_fires(process, fired, before)

    def test_workspace_watch_includes_every_member_but_excludes_external_docs(self) -> None:
        before = self.key("--workspace")
        _ = (self.root / "docs/design.md").write_text("new external documentation\n")
        self.assertEqual(self.key("--workspace"), before)
        _ = (self.root / "unrelated/src/lib.rs").write_text("// workspace member input\n")
        self.assertNotEqual(self.key("--workspace"), before)

    def test_parent_exit_stops_watcher_without_firing(self) -> None:
        parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                  stdin=subprocess.PIPE)
        try:
            watcher, fired, _ = self.start_watcher(parent.pid)
            parent.terminate()
            _ = parent.communicate(timeout=10)
            output, errors = watcher.communicate(timeout=WATCH_DEADLINE)
            self.assertEqual(watcher.returncode, 3, output + errors)
            self.assertFalse(fired.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
            _ = parent.communicate(timeout=10)

    def test_unavailable_git_snapshot_keeps_running_until_a_readable_change(self) -> None:
        git = shutil.which("git")
        assert git is not None
        fault = self.root / "git-read-fault"
        observed = self.root / "git-read-failed"
        wrapper = self.root / "tools/git"
        _ = wrapper.write_text(f"#!{sys.executable}\n" +
            "import os, sys\nfrom pathlib import Path\n" +
            f"fault = Path({str(fault)!r})\nobserved = Path({str(observed)!r})\n" +
            "if 'ls-tree' in sys.argv and fault.exists():\n" +
            "    observed.touch()\n    raise SystemExit(128)\n" +
            f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        # Workspace mode used to turn a failed ls-tree into an empty key.
        process, fired, before = self.start_watcher(os.getpid(), "--workspace")
        fault.touch()
        wait_until(lambda: observed.exists() or process.poll() is not None,
                   self.root, timeout=10, processes=(process,))
        self.assertTrue(observed.exists(), "fault injection never reached Git")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=WATCH_INTERVAL * 2 + 0.1)
        self.assertFalse(fired.exists(), "an uncertain snapshot cancelled an unchanged tree")
        source = self.root / "sample/src/lib.rs"
        _ = source.write_text(source.read_text() + "// edit during unreadable snapshot\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=WATCH_INTERVAL * 2 + 0.1)
        self.assertFalse(fired.exists(), "an unreadable edit was treated as proven stale")
        fault.unlink()
        output, errors = process.communicate(timeout=WATCH_DEADLINE)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotEqual(fired.read_text().strip(), before)
        self.assertEqual(fired.read_text().strip(), self.key("--workspace"))

    def test_nested_cargo_workspace_watches_its_own_root_inputs(self) -> None:
        cargo_root = self.root / "rust"
        self.metadata = write_workspace(cargo_root)
        shutil.rmtree(cargo_root / ".git")
        _ = subprocess.run(["git", "add", "rust"], cwd=self.root,
                           check=True, capture_output=True, timeout=10)
        _ = subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "-qm", "nested workspace"],
                           cwd=self.root, check=True, capture_output=True, timeout=10)
        for relative in ("Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = cargo_root / relative
                _ = path.write_text(path.read_text() + "\n# changed nested root input\n")
                self.assert_fires(process, fired, before)

    def test_git_root_inputs_outside_nested_cargo_workspace_do_not_cancel(self) -> None:
        cargo_root = self.root / "rust"
        self.metadata = write_workspace(cargo_root)
        shutil.rmtree(cargo_root / ".git")
        process, fired, before = self.start_watcher(os.getpid())
        for relative in ("Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml"):
            path = self.root / relative
            _ = path.write_text(path.read_text() + "\n# outside Cargo workspace\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=WATCH_INTERVAL * 2 + 0.1)
        self.assertFalse(fired.exists(), "Git root inputs were confused with Cargo root inputs")
        _ = (cargo_root / "Cargo.lock").write_text("# changed actual Cargo root input\n")
        self.assert_fires(process, fired, before)


if __name__ == "__main__":
    _ = unittest.main()
