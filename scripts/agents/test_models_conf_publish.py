#!/usr/bin/env python3
"""Shared-registry reads, atomic edits, and isolated Git publication."""
from __future__ import annotations

import configparser
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Literal, final, override
from unittest import mock

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib import models_registry, atomic_file  # noqa: E402
from scripts.lib.test_models_registry import ExactRows, registry_text, write_registry  # noqa: E402
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS  # noqa: E402
from scripts.lib.main_push_lock_test_support import publish_after_contender

AGENTS = Path(__file__).resolve().parent
REPO = AGENTS.parents[1]
GIT = shutil.which("git") or "git"
LIVE_REGISTRY = Path.home() / ".claude/config/models.conf"
DEFAULTS = registry_text({
    "assignments": ExactRows({"direct": "codex", "production": "claude"}),
    "direct.codex": ExactRows({"impl": "gpt-a:high", "review": "gpt-a:high"}),
    "direct.options": {"codex_service_tier": "default"},
    "production.claude": {"director": "opus:high"},
    "codex.agents": ExactRows({"gpt-a": "low,high,max", "gpt-b": "low,high,max"}),
})


def require_isolated_registry(environment: dict[str, str]) -> None:
    configured = Path(environment.get("MODELS_CONFIG_FILE", str(LIVE_REGISTRY))).expanduser().resolve()
    if configured == LIVE_REGISTRY.resolve() and environment.get("MODELS_CONFIG_PUBLISH") != "0":
        raise AssertionError(f"test refused to publish the live registry: {configured}; set MODELS_CONFIG_PUBLISH=0")


def prepare_catalog(root: Path, environment: dict[str, str]) -> None:
    binaries = root / "catalog-bin"
    binaries.mkdir()
    claude = binaries / "claude"
    _ = claude.write_text("#!/bin/sh\nprintf '%s\\n' 'Usage: claude --help'\n")
    claude.chmod(0o755)
    environment["PATH"] = str(binaries) + os.pathsep + environment["PATH"]
    _ = Path(environment["CODEX_CONFIG_FILE"]).write_text('model = "gpt-a"\n')
    _ = Path(environment["CODEX_MODELS_CACHE_FILE"]).write_text(
        '{"models":[{"slug":"gpt-a","visibility":"list",'
        + '"supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},'
        + '{"slug":"gpt-new","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]}]}')


@final
class RegistrySafetyTests(unittest.TestCase):
    def test_a_live_registry_target_is_refused_before_a_test_can_spawn_a_writer(self) -> None:
        environment = {"MODELS_CONFIG_FILE": str(LIVE_REGISTRY), "MODELS_CONFIG_PUBLISH": "1"}
        with self.assertRaisesRegex(AssertionError, "test refused to publish the live registry"):
            require_isolated_registry(environment)
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "registry-link"
            link.symlink_to(LIVE_REGISTRY)
            environment["MODELS_CONFIG_FILE"] = str(link)
            with self.assertRaisesRegex(AssertionError, "MODELS_CONFIG_PUBLISH=0"):
                require_isolated_registry(environment)
        environment["MODELS_CONFIG_PUBLISH"] = "0"
        require_isolated_registry(environment)


class RegistryFixture(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    registry: Path = Path()
    command_directory: Path = REPO
    environment: dict[str, str] = {}

    def set_up_registry(self, relative_path: str, *, publishing: bool = False) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="agents-registry-")))
        self.home = self.root / "home"
        self.home.mkdir()
        self.registry = self.root / relative_path
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        _ = write_registry(self.registry, base_text=DEFAULTS)
        self.environment = {**os.environ, **interpreter_cache_environment(self.root / "cache"), "HOME": str(self.home),
                            "MODELS_CONFIG_FILE": str(self.registry),
                            "MODELS_CONFIG_PUBLISH": "1" if publishing else "0",
                            "MODELS_CONFIG_TIER_ONLY": "1", "CODEX_CONFIG_FILE": str(self.root / "codex.toml"),
                            "CODEX_MODELS_CACHE_FILE": str(self.root / "models.json"),
                            "CODEX_CATALOG_SYNC_STATE_FILE": str(self.root / "catalog-state"),
                            "PYTHONPATH": str(REPO), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Registry Test",
                            "GIT_AUTHOR_EMAIL": "registry@example.invalid", "GIT_COMMITTER_NAME": "Registry Test",
                            "GIT_COMMITTER_EMAIL": "registry@example.invalid"}
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT"):
            _ = self.environment.pop(key, None)

    def command(self, *arguments: str, cwd: Path | None = None,
                input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        require_isolated_registry(self.environment)
        configured = Path(self.environment["MODELS_CONFIG_FILE"]).resolve()
        self.assertTrue(configured.is_relative_to(self.root), "registry fixture escaped its temporary folder")
        return subprocess.run(arguments, cwd=cwd or self.command_directory, env=self.environment, text=True,
                              input=input_text, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def bash(self, operation: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.command("bash", "-c", 'source "$1"; shift; ' + operation,
                            "registry-test", str(AGENTS / "models_config.sh"), *arguments)

    def value(self, section: str, key: str) -> str:
        parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
        _ = parser.read(self.registry)
        return parser[section][key]

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@final
class SharedRegistryTests(RegistryFixture):
    @override
    def setUp(self) -> None:
        self.set_up_registry("models.conf")

    def test_store_reports_its_published_configuration_after_a_peer_replaces_the_file(self) -> None:
        before = self.root / "before"
        after = self.root / "after"
        _ = before.write_text(DEFAULTS)
        published = DEFAULTS.replace("impl=gpt-a:high", "impl=gpt-b:max")
        peer = published.replace("impl=gpt-b:max", "impl=gpt-a:low")
        _ = after.write_text(published)
        store = models_registry.store_registry

        def store_then_peer_write(registry: Path, before: str, after: str, description: str, *,
                                  additive_catalog: bool = False) -> str:
            # The real writer releases its lock before the peer writes.
            result = store(registry, before, after, description, additive_catalog=additive_catalog)
            _ = self.registry.write_text(peer)
            return result

        output = io.StringIO()
        with (mock.patch.dict(os.environ, self.environment), redirect_stdout(output),
              mock.patch.object(models_registry, "store_registry", side_effect=store_then_peer_write),
              mock.patch.object(sys, "argv", ["models_registry.py", "store", "--registry", str(self.registry),
                                             "--before", str(before), "--after", str(after)])):
            self.assertEqual(models_registry.main(), 0)
        shell = self.command("bash", "-c", output.getvalue() +
                             'printf "%s\\0" "$MODELS_REGISTRY_BASE" "${_models_table_values[@]}"')
        self.assert_ok(shell)
        reported = shell.stdout.split("\0")
        self.assertEqual(reported[0], published)
        self.assertIn("gpt-b:max", reported[1:])
        self.assertNotIn("gpt-a:low", reported[1:])
        self.assertEqual(self.registry.read_text(), peer)

    def test_shell_setters_do_not_replay_an_intermediate_unrelated_row(self) -> None:
        hook = self.root / "read-hook"
        hook.mkdir()
        _ = (hook / "sitecustomize.py").write_text('''import os
from pathlib import Path
from scripts.lib import conf_reads
read_text = conf_reads.read_text
def read_then_edit(path, *arguments, **keywords):
    text = read_text(path, *arguments, **keywords)
    path = Path(path)
    if str(path) == os.environ["MODELS_CONFIG_FILE"] and "note=original" in text:
        peer = path.with_suffix(".peer")
        peer.write_text(text.replace("note=original", "note=intermediate"))
        peer.replace(path)
    return text
conf_reads.read_text = read_then_edit
''')
        self.environment["PYTHONPATH"] = str(hook) + os.pathsep + self.environment["PYTHONPATH"]
        operations = ("agents_set_row direct.impl gpt-b:max",
                      "agents_set_assignment direct claude", "agents_set_all_assignments claude",
                      "agents_set_model gpt-b direct", "agents_set_service_tier fast direct")
        for operation in operations:
            with self.subTest(operation=operation):
                _ = write_registry(self.registry, {"direct.options": {"note": "original"}}, base_text=DEFAULTS)
                result = self.bash('''
eval "$(declare -f models_registry_store | sed '1s/models_registry_store/saved_registry_store/')"
models_registry_store() {
    sed 's/note=intermediate/note=latest/' "$MODELS_CONFIG_FILE" > "$MODELS_CONFIG_FILE.peer"
    mv "$MODELS_CONFIG_FILE.peer" "$MODELS_CONFIG_FILE"
    saved_registry_store "$@"
}
''' + operation)
                self.assert_ok(result)
                self.assertEqual(self.value("direct.options", "note"), "latest")

    def test_a_stray_local_registry_changes_neither_shell_nor_python_readers(self) -> None:
        stray = self.registry.with_name("agents.local.conf")
        _ = write_registry(stray, {"direct.codex": {"impl": "gpt-b:max"},
                                   "assignments": {"direct": "claude"}})
        stray_contents = stray.read_text()
        result = self.bash('agents_resolve direct.impl && printf "%s:%s:%s" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"')
        self.assert_ok(result)
        self.assertEqual(result.stdout, "codex:gpt-a:high")
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(models_registry.registry_text(), DEFAULTS)
        self.assertEqual(stray.read_text(), stray_contents)

    def test_each_shell_writer_replaces_the_shared_file_without_touching_an_open_reader(self) -> None:
        operations = (
            ("agents_set_row direct.impl gpt-b:max", "direct.codex", "impl", "gpt-b:max"),
            ("agents_set_assignment direct claude", "assignments", "direct", "claude"),
            ("agents_set_all_assignments claude", "assignments", "direct", "claude"),
            ("agents_set_model gpt-b", "direct.codex", "impl", "gpt-b:high"),
            ("agents_set_service_tier fast direct", "direct.options", "codex_service_tier", "fast"),
        )
        for operation, section, key, expected in operations:
            with self.subTest(operation=operation):
                _ = write_registry(self.registry, base_text=DEFAULTS)
                self.registry.chmod(0o640)
                with self.registry.open() as before:
                    original_inode = self.registry.stat().st_ino
                    result = self.bash(operation)
                    self.assert_ok(result)
                    self.assertNotEqual(self.registry.stat().st_ino, original_inode)
                    self.assertEqual(before.read(), DEFAULTS, "writer truncated an existing reader's file")
                self.assertEqual(self.value(section, key), expected)
                self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)
                self.assertFalse(self.registry.with_name("agents.local.conf").exists())

    def test_the_python_writer_replaces_the_file_atomically_and_ignores_stray_local_rows(self) -> None:
        _ = write_registry(self.registry.with_name("agents.local.conf"), {"direct.codex": {"impl": "gpt-b:low"}})
        self.registry.chmod(0o640)
        with self.registry.open() as before, mock.patch.dict(os.environ, self.environment):
            inode = self.registry.stat().st_ino
            changed = models_registry.store_registry(self.registry, DEFAULTS,
                DEFAULTS.replace("impl=gpt-a:high", "impl=gpt-b:max"), "test row change")
            self.assertTrue(changed)
            self.assertNotEqual(self.registry.stat().st_ino, inode)
            self.assertEqual(before.read(), DEFAULTS)
        self.assertEqual(self.value("direct.codex", "impl"), "gpt-b:max")
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)

    def test_the_registry_writer_persists_the_replacement_directory_entry(self) -> None:
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(atomic_file, "sync_folder", wraps=atomic_file.sync_folder) as sync):
            self.assertTrue(models_registry.store_registry(self.registry, DEFAULTS,
                DEFAULTS.replace("impl=gpt-a:high", "impl=gpt-b:max"), "row edit"))
        sync.assert_called_once_with(self.registry.parent)

    def test_catalog_sync_adds_a_model_and_effort_retains_peer_rows_and_is_idle_when_current(self) -> None:
        prepare_catalog(self.root, self.environment)
        self.registry.chmod(0o640)
        with self.registry.open() as before:
            inode = self.registry.stat().st_ino
            result = self.command("bash", str(AGENTS / "sync_codex_catalog.sh"))
            self.assert_ok(result)
            self.assertNotEqual(self.registry.stat().st_ino, inode)
            self.assertEqual(before.read(), DEFAULTS)
        self.assertEqual(set(self.value("codex.agents", "gpt-a").split(",")), {"low", "high", "max", "xhigh"})
        self.assertEqual(self.value("codex.agents", "gpt-b"), "low,high,max")
        self.assertEqual(self.value("codex.agents", "gpt-new"), "high")
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)
        snapshot = self.registry.stat()
        contents = self.registry.read_bytes()
        self.assert_ok(self.command("bash", str(AGENTS / "sync_codex_catalog.sh")))
        self.assertEqual((self.registry.stat().st_ino, self.registry.stat().st_mtime_ns),
                         (snapshot.st_ino, snapshot.st_mtime_ns))
        self.assertEqual(self.registry.read_bytes(), contents)


@final
class RegistryPublishingTests(RegistryFixture):
    checkout: Path = Path()
    competitor: Path = Path()
    remote: Path = Path()
    initial: str = ""

    @override
    def setUp(self) -> None:
        self.set_up_registry("home/.claude/config/models.conf", publishing=True)
        self.checkout = self.home / ".claude"
        self.command_directory = self.checkout
        self.competitor = self.root / "competitor"
        self.remote = self.root / "origin.git"
        _ = (self.checkout / "dirty.txt").write_text("committed dirty base\n")
        _ = (self.checkout / "staged.txt").write_text("committed staged base\n")
        _ = self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        _ = self.git("init", "--initial-branch=main")
        _ = self.git("add", "config/models.conf", "dirty.txt", "staged.txt")
        _ = self.git("commit", "-m", "fixture")
        _ = self.git("remote", "add", "origin", str(self.remote))
        _ = self.git("push", "--set-upstream", "origin", "main")
        _ = self.git("clone", str(self.remote), str(self.competitor), cwd=self.root)
        self.initial = self.git("rev-parse", "HEAD").strip()
        _ = (self.checkout / "dirty.txt").write_text("uncommitted dirty edit\n")
        _ = (self.checkout / "staged.txt").write_text("staged user edit\n")
        _ = self.git("add", "staged.txt")
        _ = (self.checkout / "staged.txt").write_text("staged user edit plus unstaged edit\n")

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = self.command(GIT, *arguments, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def edit(self, operation: str = "agents_set_row direct.impl gpt-b:max") -> subprocess.CompletedProcess[str]:
        return self.bash(operation)

    def dirty_state(self) -> tuple[str, str, str]:
        return (self.git("diff", "--", "dirty.txt", "staged.txt"),
                self.git("diff", "--cached", "--", "dirty.txt", "staged.txt"),
                self.git("ls-files", "--stage", "dirty.txt", "staged.txt"))

    def assert_published(self, before: tuple[str, str, str], expected: str = "impl=gpt-b:max") -> None:
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.git("rev-parse", "origin/main").strip())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main").strip(),
                         self.git("rev-parse", "HEAD").strip())
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines(),
                         ["config/models.conf"])
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("config(models): "))
        self.assertIn(expected, self.registry.read_text())
        self.assertEqual(self.dirty_state(), before)
        self.assertFalse(self.registry.with_name("agents.local.conf").exists())

    def assert_writer_publishes(self, operation: str, expected: str) -> None:
        before = self.dirty_state()
        result = self.edit(operation)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before, expected)
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_a_row_writer_publishes_only_the_registry_and_preserves_dirty_and_staged_files(self) -> None:
        self.assert_writer_publishes("agents_set_row direct.impl gpt-b:max", "impl=gpt-b:max")

    def test_publication_uses_models_prefixes_for_its_private_git_indexes(self) -> None:
        binaries = self.root / "git-probe"
        binaries.mkdir()
        captured = self.root / "index-paths"
        wrapper = binaries / "git"
        _ = wrapper.write_text('#!/bin/bash\n'
                               + 'if [[ -n "${GIT_INDEX_FILE:-}" ]]; then\n'
                               + '  printf "%s\\n" "$GIT_INDEX_FILE" >> "$TEST_INDEX_PATHS"\n'
                               + 'fi\nexec "$TEST_REAL_GIT" "$@"\n')
        wrapper.chmod(0o755)
        self.environment.update(PATH=str(binaries) + os.pathsep + self.environment["PATH"],
                                TEST_INDEX_PATHS=str(captured), TEST_REAL_GIT=GIT)
        self.assert_writer_publishes("agents_set_row direct.impl gpt-b:max", "impl=gpt-b:max")
        paths = captured.read_text().splitlines()
        self.assertTrue(paths)
        self.assertTrue(all(Path(path).parent.name.startswith("models-") for path in paths), paths)

    def test_an_assignment_writer_publishes_only_the_registry(self) -> None:
        self.assert_writer_publishes("agents_set_assignment direct claude", "direct=claude")

    def test_the_all_assignments_writer_publishes_only_the_registry(self) -> None:
        self.assert_writer_publishes("agents_set_all_assignments claude", "direct=claude")

    def test_a_model_sweep_publishes_only_the_registry(self) -> None:
        self.assert_writer_publishes("agents_set_model gpt-b direct", "impl=gpt-b:high")

    def test_a_service_tier_writer_publishes_only_the_registry(self) -> None:
        self.assert_writer_publishes("agents_set_service_tier fast direct", "codex_service_tier=fast")

    def test_the_agent_admin_command_publishes_its_row_edit(self) -> None:
        before = self.dirty_state()
        result = self.command("bash", str(AGENTS / "default_models.sh"), "direct.impl", "gpt-b:max")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before)
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_the_python_writer_publishes_only_the_registry(self) -> None:
        before = self.dirty_state()
        program = ("from pathlib import Path; from scripts.lib import models_registry; "
                   + "p=models_registry.registry_path(); before=p.read_text(); "
                   + "models_registry.store_registry(p,before,before.replace('impl=gpt-a:high','impl=gpt-b:max'),'row edit')")
        result = self.command(sys.executable, "-c", program)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before)
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

        with self.subTest(holder="another process"):
            program = ("from scripts.lib import models_registry; "
                       "p=models_registry.registry_path(); before=p.read_text(); "
                       "models_registry.store_registry(p,before,before.replace('review=gpt-a:high','review=gpt-b:max'),'row edit')")
            _ = publish_after_contender(self, self.checkout, self.remote, [sys.executable, "-c", program],
                                    self.environment, cwd=self.checkout)
            self.assert_published(before, expected="review=gpt-b:max")

    def test_identical_edit_publishes_after_interruption_following_the_atomic_write(self) -> None:
        before = self.dirty_state()
        program = '''
from scripts.lib import models_registry
name = 'write_text_atomic' if hasattr(models_registry, 'write_text_atomic') else '_atomic_write'
write = getattr(models_registry, name)
def interrupted(path, text):
    write(path, text)
    raise SystemExit(17)
setattr(models_registry, name, interrupted)
p = models_registry.registry_path()
text = p.read_text()
models_registry.store_registry(p, text, text.replace('impl=gpt-a:high', 'impl=gpt-b:max'), 'row edit')
'''
        result = self.command(sys.executable, "-c", program)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertIn("impl=gpt-b:max", self.registry.read_text())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assert_ok(self.edit())
        self.assert_published(before)
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_push_retry_preserves_an_earlier_unpublished_registry_row(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace("review=gpt-a:high", "review=gpt-b:max"))
        _ = self.install_push_race(always_refuse=False)
        before = self.dirty_state()
        self.assert_ok(self.edit())
        self.assert_published(before)
        self.assertEqual(self.value("direct.codex", "review"), "gpt-b:max")
        self.assertIn("review=gpt-b:max", self.git("show", "HEAD:config/models.conf"))

    def test_a_lost_push_acknowledgement_reconciles_without_another_commit(self) -> None:
        count = self.install_push_race(always_refuse=False)
        self.environment["TEST_LOSE_ACK"] = "1"
        before = self.dirty_state()
        self.assert_ok(self.edit())
        self.assert_published(before)
        self.assertEqual(count.read_text(), "1")
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_identical_edit_recovers_after_all_push_attempts_were_rejected(self) -> None:
        _ = self.install_push_race(always_refuse=True)
        self.assertNotEqual(self.edit().returncode, 0)
        self.environment["PATH"] = os.path.dirname(GIT) + os.pathsep + os.environ["PATH"]
        before = self.dirty_state()
        self.assert_ok(self.edit())
        self.assert_published(before)

    def assert_obstructed_fast_forward_is_refused(self, obstruction: str, *, accepted_push: bool,
                                                directory_obstruction: bool = False) -> None:
        if accepted_push:
            program = '''
from scripts.lib import models_registry
git = models_registry._git
def interrupted(repo, *arguments, **keywords):
    result = git(repo, *arguments, **keywords)
    if arguments[0] == 'push':
        raise SystemExit(17)
    return result
models_registry._git = interrupted
p = models_registry.registry_path()
text = p.read_text()
models_registry.store_registry(p, text, text.replace('impl=gpt-a:high', 'impl=gpt-b:max'), 'row edit')
'''
            interrupted = self.command(sys.executable, "-c", program)
            self.assertEqual(interrupted.returncode, 17, interrupted.stdout + interrupted.stderr)
            self.assertIn("impl=gpt-b:max", self.git("--git-dir", str(self.remote), "show", "main:config/models.conf"))
            self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
            _ = self.git("pull", "--ff-only", "origin", "main", cwd=self.competitor)

        remote_registry = self.competitor / "config/models.conf"
        _ = remote_registry.write_text(remote_registry.read_text().replace("review=gpt-a:high", "review=gpt-b:low"))
        local_obstruction = self.checkout / obstruction
        if obstruction != "dirty.txt":
            if directory_obstruction:
                local_obstruction = local_obstruction / "local-work.txt"
            local_obstruction.parent.mkdir(parents=True, exist_ok=True)
            _ = local_obstruction.write_text("untracked local work\n")
        remote_obstruction = self.competitor / obstruction
        remote_obstruction.parent.mkdir(parents=True, exist_ok=True)
        _ = remote_obstruction.write_text("peer work\n")
        _ = self.git("add", "config/models.conf", obstruction, cwd=self.competitor)
        _ = self.git("commit", "-m", "peer registry and file changes", cwd=self.competitor)
        _ = self.git("push", "origin", "main", cwd=self.competitor)

        head = self.git("rev-parse", "HEAD")
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        registry_bytes, index_bytes = self.registry.read_bytes(), index.read_bytes()
        obstruction_bytes = local_obstruction.read_bytes()
        refused = self.edit()
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("registry edit refused before writing", refused.stdout + refused.stderr)
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(local_obstruction.read_bytes(), obstruction_bytes)

    def test_recovery_refuses_a_dirty_conflicting_file_without_changing_the_registry_index_or_head(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("dirty.txt", accepted_push=True)

    def test_recovery_refuses_an_untracked_obstruction_without_changing_the_registry_index_or_head(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("untracked.txt", accepted_push=True)

    def test_recovery_refuses_an_untracked_directory_in_the_way_of_a_remote_file(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("untracked-directory", accepted_push=True,
                                                     directory_obstruction=True)

    def test_an_ordinary_fast_forward_refuses_a_dirty_conflicting_file_without_changing_the_index(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("dirty.txt", accepted_push=False)

    def test_an_ordinary_fast_forward_refuses_an_untracked_obstruction_without_changing_the_index(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("untracked.txt", accepted_push=False)

    def test_an_ordinary_fast_forward_refuses_an_untracked_directory_in_the_way_of_a_remote_file(self) -> None:
        self.assert_obstructed_fast_forward_is_refused("untracked-directory", accepted_push=False,
                                                     directory_obstruction=True)

    def test_a_lost_acknowledgement_does_not_reapply_an_already_shared_edit(self) -> None:
        count = self.install_push_race(always_refuse=False)
        self.environment.update(TEST_LOSE_ACK="1", TEST_ADVANCE_ACCEPTED="1")
        before = self.dirty_state()
        self.assert_ok(self.edit())
        self.assert_published(before, "impl=gpt-a:low")
        self.assertEqual(count.read_text(), "1")
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "2")

    def test_current_catalog_sync_recovers_its_previous_unshared_additions(self) -> None:
        prepare_catalog(self.root, self.environment)
        _ = self.install_push_race(always_refuse=True)
        self.assertNotEqual(self.command("bash", str(AGENTS / "sync_codex_catalog.sh")).returncode, 0)
        self.environment["PATH"] = str(self.root / "catalog-bin") + os.pathsep + os.environ["PATH"]
        before = self.dirty_state()
        self.assert_ok(self.command("bash", str(AGENTS / "sync_codex_catalog.sh")))
        self.assert_published(before, "gpt-new=high")
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_the_showrunner_effort_writer_publishes_the_director_launch_row(self) -> None:
        before = self.dirty_state()
        program = ("from scripts.production import unit_effort; import os, sys; "
                   + "from scripts.lib import models_registry; "
                   + "problem=unit_effort.set_row('opus','max',models_registry.edit_base(os.environ)); "
                   + "print(problem); sys.exit(bool(problem))")
        result = self.command(str(REPO / "scripts/lib/py"), "-c", program)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before, "director=opus:max")
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")

    def test_catalog_sync_publishes_additions_once_and_does_not_write_or_publish_without_new_rows(self) -> None:
        prepare_catalog(self.root, self.environment)
        before = self.dirty_state()
        result = self.command("bash", str(AGENTS / "sync_codex_catalog.sh"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before, "gpt-new=high")
        self.assertIn("gpt-b=low,high,max", self.registry.read_text())
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "1")
        head, contents, snapshot = self.git("rev-parse", "HEAD"), self.registry.read_bytes(), self.registry.stat()
        second = self.command("bash", str(AGENTS / "sync_codex_catalog.sh"))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "origin/main"), head)
        self.assertEqual(self.registry.read_bytes(), contents)
        self.assertEqual((self.registry.stat().st_ino, self.registry.stat().st_mtime_ns),
                         (snapshot.st_ino, snapshot.st_mtime_ns))
        self.assertEqual(self.dirty_state(), before)

    def test_a_behind_checkout_fast_forwards_before_applying_the_row_edit(self) -> None:
        path = self.competitor / "config/models.conf"
        _ = path.write_text(path.read_text().replace("review=gpt-a:high", "review=gpt-b:low"))
        _ = self.git("add", "config/models.conf", cwd=self.competitor)
        _ = self.git("commit", "-m", "peer review row", cwd=self.competitor)
        _ = self.git("push", "origin", "main", cwd=self.competitor)
        before = self.dirty_state()
        result = self.edit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_published(before)
        self.assertIn("review=gpt-b:low", self.registry.read_text())

    def commit_local_work(self) -> str:
        local_file = self.checkout / "local-commit.txt"
        _ = local_file.write_text("local history\n")
        _ = self.git("add", "local-commit.txt")
        _ = self.git("commit", "--only", "-m", "local work", "--", "local-commit.txt")
        return self.git("rev-parse", "HEAD").strip()

    def test_an_ahead_checkout_publishes_one_registry_commit_on_its_local_tip(self) -> None:
        local_head = self.commit_local_work()
        self.assertEqual(self.git("rev-parse", "origin/main").strip(), self.initial)
        before = self.dirty_state()
        unrelated_index = self.git("ls-files", "--stage", "--", ".", ":(exclude)config/models.conf")
        unrelated_files = {name: (self.checkout / name).read_bytes()
                           for name in ("dirty.txt", "staged.txt", "local-commit.txt")}

        self.assert_ok(self.edit())

        self.assert_published(before)
        self.assertEqual(self.git("rev-parse", "HEAD^").strip(), local_head)
        self.assertEqual(self.git("rev-list", "--count", f"{local_head}..HEAD").strip(), "1")
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", "main:local-commit.txt"),
                         "local history\n")
        self.assertEqual(self.git("ls-files", "--stage", "--", ".", ":(exclude)config/models.conf"),
                         unrelated_index)
        for name, contents in unrelated_files.items():
            self.assertEqual((self.checkout / name).read_bytes(), contents)

    def test_an_already_contained_target_leaves_the_registry_index_and_head_unchanged(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace("impl=gpt-a:high", "impl=gpt-b:max"))
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        registry_bytes, index_bytes = self.registry.read_bytes(), index.read_bytes()
        program = ("import sys; from pathlib import Path; from scripts.lib import models_registry; "
                   + "models_registry._fast_forward(Path.cwd(), models_registry.registry_path(), "
                   + "sys.argv[1], registry_already_shared=True)")
        self.assert_ok(self.command(sys.executable, "-c", program, self.initial))
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)

    def test_another_fetch_between_the_fetch_and_the_read_does_not_change_the_target(self) -> None:
        _ = self.git("checkout", "-b", "phases", cwd=self.competitor)
        _ = (self.competitor / "phases.txt").write_text("another branch\n")
        _ = self.git("add", "phases.txt", cwd=self.competitor)
        _ = self.git("commit", "-m", "another branch", cwd=self.competitor)
        _ = self.git("push", "origin", "phases", cwd=self.competitor)
        program = "\n".join((
            "from pathlib import Path",
            "from scripts.lib import models_registry",
            "real = models_registry._git",
            "def peer(repo, *arguments, **options):",
            "    output = real(repo, *arguments, **options)",
            "    if arguments[:1] == ('fetch',):",
            "        _ = real(repo, 'fetch', 'origin', 'phases')",
            "    return output",
            "models_registry._git = peer",
            "print(models_registry._fetch(Path.cwd(), 'main'))",
        ))
        result = self.command(sys.executable, "-c", program)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), self.initial)

    def assert_unfinished_merge_is_refused(self, *, ahead: bool, unmerged: bool,
                                          merge_head_present: bool = True) -> None:
        if ahead:
            _ = self.commit_local_work()
        merge_head = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())
        if merge_head_present:
            _ = merge_head.write_text(self.initial + "\n")
        blob = self.git("rev-parse", "HEAD:dirty.txt").strip()
        merge_file = self.checkout / "merge-work.txt"
        _ = merge_file.write_text("resolved local work\n")
        if unmerged:
            result = self.command(GIT, "update-index", "--index-info",
                                  input_text="".join(f"100644 {blob} {stage}\tmerge-work.txt\n" for stage in (1, 2, 3)))
            self.assert_ok(result)
        else:
            _ = self.git("add", "merge-work.txt")
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        registry_bytes, index_bytes = self.registry.read_bytes(), index.read_bytes()
        head, remote = self.git("rev-parse", "HEAD"), self.git("--git-dir", str(self.remote), "rev-parse", "main")
        merge_bytes = merge_head.read_bytes() if merge_head_present else b""
        unmerged_entries = self.git("ls-files", "--unmerged")

        refused = self.edit()

        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("registry edit refused before writing", refused.stdout + refused.stderr)
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), remote)
        self.assertEqual(merge_head.exists(), merge_head_present)
        if merge_head_present:
            self.assertEqual(merge_head.read_bytes(), merge_bytes)
        self.assertEqual(self.git("ls-files", "--unmerged"), unmerged_entries)
        self.assertEqual(merge_file.read_text(), "resolved local work\n")

    def test_an_equal_remote_refuses_a_fully_staged_unfinished_merge_before_publication(self) -> None:
        self.assert_unfinished_merge_is_refused(ahead=False, unmerged=False)

    def test_an_equal_remote_refuses_an_unresolved_merge_before_publication(self) -> None:
        self.assert_unfinished_merge_is_refused(ahead=False, unmerged=True)

    def test_an_ahead_checkout_refuses_a_fully_staged_unfinished_merge_before_publication(self) -> None:
        self.assert_unfinished_merge_is_refused(ahead=True, unmerged=False)

    def test_an_ahead_checkout_refuses_an_unresolved_merge_before_publication(self) -> None:
        self.assert_unfinished_merge_is_refused(ahead=True, unmerged=True)

    def test_unmerged_index_entries_refuse_publication_even_without_merge_head(self) -> None:
        self.assert_unfinished_merge_is_refused(ahead=False, unmerged=True, merge_head_present=False)

    def test_divergence_refuses_before_writing_the_registry_or_touching_other_changes(self) -> None:
        _ = (self.checkout / "local-commit.txt").write_text("local history\n")
        _ = self.git("add", "local-commit.txt")
        _ = self.git("commit", "--only", "-m", "local divergence", "--", "local-commit.txt")
        _ = (self.competitor / "peer.txt").write_text("remote history\n")
        _ = self.git("add", "peer.txt", cwd=self.competitor)
        _ = self.git("commit", "-m", "remote divergence", cwd=self.competitor)
        _ = self.git("push", "origin", "main", cwd=self.competitor)
        before, contents = self.dirty_state(), self.registry.read_bytes()
        head = self.git("rev-parse", "HEAD")
        result = self.edit()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout + result.stderr, "(?i)fast.forward|diverg")
        self.assertEqual(self.registry.read_bytes(), contents)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.dirty_state(), before)

    def install_push_race(self, *, always_refuse: bool, obstruction: str = "",
                          unfinished_merge: bool = False,
                          accepted_merge: Literal["none", "merge_head", "unmerged_index"] = "none") -> Path:
        binaries = self.root / "bin"
        binaries.mkdir()
        count = self.root / "push-count"
        wrapper = binaries / "git"
        _ = wrapper.write_text('''#!/usr/bin/env python3
import os
import pathlib
import shutil
import subprocess
import sys
arguments = sys.argv[1:]
real = os.environ["TEST_REAL_GIT"]
def start_merge(git_dir, unmerged=False):
    if unmerged:
        blob = subprocess.check_output([real, "rev-parse", "HEAD:dirty.txt"], text=True).strip()
        entries = "".join(f"100644 {blob} {stage}\\tmerge-work.txt\\n" for stage in (1, 2, 3))
        subprocess.run([real, "update-index", "--index-info"], input=entries, text=True, check=True)
    else:
        head = subprocess.check_output([real, "rev-parse", "HEAD"], text=True)
        (git_dir / "MERGE_HEAD").write_text(head)
def snapshot_state(name, git_dir, counter):
    snapshot = counter.parent / name
    snapshot.mkdir()
    shutil.copyfile("config/models.conf", snapshot / "models.conf")
    shutil.copyfile(git_dir / "index", snapshot / "index")
    (snapshot / "HEAD").write_bytes(subprocess.check_output([real, "rev-parse", "HEAD"]))
def advance_peer(accepted=False):
    peer = pathlib.Path(os.environ["TEST_COMPETITOR"])
    if accepted:
        subprocess.run([real, "pull", "--ff-only", "origin", "main"], cwd=peer, check=True, capture_output=True)
    registry = peer / "config/models.conf"
    text = registry.read_text()
    if accepted:
        text = text.replace("impl=gpt-b:max", "impl=gpt-a:low")
    else:
        text = text.replace("impl=gpt-a:high", "impl=gpt-a:low").replace("review=gpt-a:high", "review=gpt-b:low")
    registry.write_text(text)
    paths = ["config/models.conf"]
    obstruction = os.environ["TEST_OBSTRUCTION"]
    if obstruction:
        (peer / obstruction).write_text("peer work\\n")
        paths.append(obstruction)
    subject = "config(models): later peer edit" if accepted else "concurrent row edits"
    for command in (["add", *paths], ["commit", "-m", subject], ["push", "origin", "main"]):
        subprocess.run([real, *command], cwd=peer, check=True, capture_output=True)
if "push" in arguments:
    counter = pathlib.Path(os.environ["TEST_PUSH_COUNT"])
    attempt = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(attempt))
    if attempt == 1:
        git_dir = pathlib.Path(subprocess.check_output([real, "rev-parse", "--absolute-git-dir"], text=True).strip())
        accepted_merge = os.environ["TEST_ACCEPTED_MERGE"]
        if accepted_merge != "none":
            subprocess.run([real, *arguments], check=True)
            start_merge(git_dir, unmerged=accepted_merge == "unmerged_index")
            snapshot_state("after-push", git_dir, counter)
            sys.exit(0)
        if os.environ["TEST_UNFINISHED_MERGE"] == "1":
            start_merge(git_dir)
        snapshot_state("before-retry", git_dir, counter)
    if os.environ.get("TEST_LOSE_ACK") == "1":
        result = subprocess.run([real, *arguments], check=False)
        if result.returncode == 0 and os.environ.get("TEST_ADVANCE_ACCEPTED") == "1":
            advance_peer(accepted=True)
        sys.exit(result.returncode or 1)
    if os.environ["TEST_REJECT_ALL"] == "1":
        print("! [rejected] main -> main (test remote refused push)", file=sys.stderr)
        sys.exit(1)
    if attempt == 1:
        advance_peer()
os.execv(real, [real, *arguments])
''')
        wrapper.chmod(0o755)
        self.environment.update(PATH=str(binaries) + os.pathsep + self.environment["PATH"],
                                TEST_REAL_GIT=GIT, TEST_PUSH_COUNT=str(count),
                                TEST_COMPETITOR=str(self.competitor), TEST_REJECT_ALL="1" if always_refuse else "0",
                                TEST_OBSTRUCTION=obstruction, TEST_UNFINISHED_MERGE="1" if unfinished_merge else "0",
                                TEST_ACCEPTED_MERGE=accepted_merge)
        return count

    def assert_accepted_push_preserves_local_merge(self, *, unmerged: bool) -> None:
        count = self.install_push_race(always_refuse=False,
                                       accepted_merge="unmerged_index" if unmerged else "merge_head")

        refused = self.edit()

        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn(f"{self.registry} is shared, but local reconciliation failed", refused.stderr)
        self.assertNotIn("refused before writing", refused.stderr)
        self.assertEqual(count.read_text(), "1")
        snapshot = self.root / "after-push"
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        self.assertEqual(self.registry.read_bytes(), (snapshot / "models.conf").read_bytes())
        self.assertEqual(index.read_bytes(), (snapshot / "index").read_bytes())
        head = (snapshot / "HEAD").read_text()
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        remote = self.git("--git-dir", str(self.remote), "rev-parse", "main").strip()
        self.assertNotEqual(remote, head.strip())
        self.assertEqual(self.git("rev-parse", f"{remote}^"), head)
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", remote).splitlines(),
                         ["config/models.conf"])
        self.assertEqual(self.git("show", f"{remote}:config/models.conf").encode(),
                         (snapshot / "models.conf").read_bytes())
        self.assertIn("impl=gpt-b:max", self.git("show", f"{remote}:config/models.conf"))
        merge_head = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())
        self.assertEqual(merge_head.exists(), not unmerged)
        if unmerged:
            self.assertIn("merge-work.txt", self.git("ls-files", "--unmerged"))
        else:
            self.assertEqual(merge_head.read_text(), head)

    def test_a_successful_push_preserves_a_merge_started_before_local_reconciliation(self) -> None:
        self.assert_accepted_push_preserves_local_merge(unmerged=False)

    def test_a_successful_push_preserves_unmerged_entries_without_merge_head_before_local_reconciliation(self) -> None:
        self.assert_accepted_push_preserves_local_merge(unmerged=True)

    def assert_retry_refuses_before_rewriting(self, *, obstruction: str = "", unfinished_merge: bool = False,
                                            accepted: bool = False) -> None:
        if obstruction == "untracked.txt":
            _ = (self.checkout / obstruction).write_text("untracked local work\n")
        count = self.install_push_race(always_refuse=False, obstruction=obstruction,
                                       unfinished_merge=unfinished_merge)
        if accepted:
            self.environment.update(TEST_LOSE_ACK="1", TEST_ADVANCE_ACCEPTED="1")
        obstruction_bytes = (self.checkout / obstruction).read_bytes() if obstruction else b""
        head = self.git("rev-parse", "HEAD")

        refused = self.edit()

        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertEqual(count.read_text(), "1", refused.stdout + refused.stderr)
        expected = "is shared, but local reconciliation failed" if accepted else "is edited but not shared"
        self.assertIn(expected, refused.stdout + refused.stderr)
        self.assertNotIn("refused before writing", refused.stdout + refused.stderr)
        snapshot = self.root / "before-retry"
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        self.assertEqual(self.registry.read_bytes(), (snapshot / "models.conf").read_bytes())
        self.assertEqual(index.read_bytes(), (snapshot / "index").read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"),
                         self.git("rev-parse", "HEAD", cwd=self.competitor))
        if obstruction:
            self.assertEqual((self.checkout / obstruction).read_bytes(), obstruction_bytes)
        if unfinished_merge:
            merge_head = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())
            self.assertEqual(merge_head.read_text(), head)

    def test_a_rejected_push_refuses_a_new_unfinished_merge_before_another_write_or_push(self) -> None:
        self.assert_retry_refuses_before_rewriting(unfinished_merge=True)

    def test_a_rejected_push_refuses_a_dirty_peer_path_before_another_write_or_push(self) -> None:
        self.assert_retry_refuses_before_rewriting(obstruction="dirty.txt")

    def test_a_rejected_push_refuses_an_untracked_peer_path_before_another_write_or_push(self) -> None:
        self.assert_retry_refuses_before_rewriting(obstruction="untracked.txt")

    def test_an_accepted_push_reports_a_local_reconciliation_obstruction_as_shared(self) -> None:
        self.assert_retry_refuses_before_rewriting(obstruction="dirty.txt", accepted=True)

    def test_a_rejected_push_reapplies_the_same_row_on_the_new_remote_tip(self) -> None:
        count = self.install_push_race(always_refuse=False)
        before = self.dirty_state()
        result = self.edit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(count.read_text(), "2")
        self.assert_published(before)
        self.assertIn("review=gpt-b:low", self.registry.read_text())
        self.assertEqual(self.git("rev-list", "--count", f"{self.initial}..HEAD").strip(), "2")

    def test_three_rejected_pushes_say_the_file_is_edited_but_not_shared(self) -> None:
        count = self.install_push_race(always_refuse=True)
        before = self.dirty_state()
        result = self.edit()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(count.read_text(), "3")
        self.assertRegex(result.stdout + result.stderr, "(?i)edited.*not shared")
        self.assertIn("impl=gpt-b:max", self.registry.read_text())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main").strip(), self.initial)
        self.assertEqual(self.dirty_state(), before)

    def test_an_ahead_checkout_keeps_its_local_parent_when_retrying_a_rejected_push(self) -> None:
        local_head = self.commit_local_work()
        count = self.install_push_race(always_refuse=True)
        before = self.dirty_state()

        refused = self.edit()

        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(count.read_text(), "3")
        self.assertIn("is edited but not shared", refused.stdout + refused.stderr)
        self.assertIn("impl=gpt-b:max", self.registry.read_text())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), local_head)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main").strip(), self.initial)
        self.assertEqual(self.dirty_state(), before)

    def test_disabled_publishing_edits_without_committing_or_pushing(self) -> None:
        self.environment["MODELS_CONFIG_PUBLISH"] = "0"
        before = self.dirty_state()
        result = self.edit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("impl=gpt-b:max", self.registry.read_text())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main").strip(), self.initial)
        self.assertEqual(self.dirty_state(), before)

    def test_a_registry_outside_the_real_checkout_is_not_published(self) -> None:
        external = self.root / "external/models.conf"
        external.parent.mkdir()
        _ = write_registry(external, base_text=DEFAULTS)
        self.environment["MODELS_CONFIG_FILE"] = str(external)
        before = self.dirty_state()
        result = self.edit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("impl=gpt-b:max", external.read_text())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.dirty_state(), before)


if __name__ == "__main__":
    _ = unittest.main()
