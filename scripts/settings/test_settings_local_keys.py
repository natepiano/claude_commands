from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

KEYS = Path(__file__).with_name("settings_local_keys.sh")
BASH = shutil.which("bash") or "/bin/bash"
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@invalid"}
NATEDEV = {
    "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False, "unit-board@nate-local": True},
    "extraKnownMarketplaces": {"nate-local": {"source": {"source": "directory", "path": "/home/natepiano/.claude/mods"}}},
    "hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]},
        {"matcher": "^(startup|resume|clear|compact|fork)$", "hooks": [
            {"type": "command", "command": "bash '/home/natepiano/.claude/hooks/herdr-agent-state.sh' session"}]},
    ]},
    "model": "opus",
    "theme": "dark-ansi",
}
MAC = {
    "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False},
    "theme": "dark",
    "voice": {"enabled": True, "mode": "hold"},
    "hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]},
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "bash '/Users/natemccoy/.claude/hooks/herdr-agent-state.sh' session"}]},
    ]},
}


def program(name: str, settings: object, sidecar: object | None = None) -> object:
    """Run one of the jq programs settings_local_keys.sh defines."""
    script = f'source "$0"; jq --argjson baseline "$2" --argjson local "$1" "${name}"'
    ran = subprocess.run([BASH, "-c", script, str(KEYS),
                          json.dumps([sidecar] if sidecar is not None else []), '["hooks","enabledPlugins"]'],
                         input=json.dumps(settings), capture_output=True,
                         text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
    return cast(object, json.loads(ran.stdout))


@final
class SettingsLocalKeysTests(unittest.TestCase):
    def test_each_machine_cleans_to_the_same_blob(self) -> None:
        self.assertEqual(program("SETTINGS_LOCAL_KEYS_STRIP", NATEDEV), program("SETTINGS_LOCAL_KEYS_STRIP", MAC))
        self.assertEqual(program("SETTINGS_LOCAL_KEYS_STRIP", NATEDEV), {
            "enabledPlugins": {"rust-analyzer-lsp@claude-plugins-official": False},
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "shared.py"}]}]}})

    def test_the_sidecar_puts_each_machine_back(self) -> None:
        for settings in (NATEDEV, MAC):
            sidecar = program("SETTINGS_LOCAL_KEYS_PICK", settings)
            merged = program("SETTINGS_LOCAL_KEYS_MERGE", program("SETTINGS_LOCAL_KEYS_STRIP", settings), sidecar)
            self.assertEqual(merged, settings)
            self.assertEqual(program("SETTINGS_LOCAL_KEYS_MISSING", merged, sidecar), 0)


@final
class SettingsFilterTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    env: dict[str, str] = {}
    settings: Path = Path()
    sidecar: Path = Path()
    history: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "repository with spaces"
        self.home = Path(temporary) / "home"
        self.env = {**os.environ, **GIT_ENV, "HOME": str(self.home), "CONF_DIR": str(self.root / "config"),
                    "XDG_STATE_HOME": str(self.home / "state"), "XDG_CACHE_HOME": str(self.home / "cache")}
        self.settings = self.root / "settings.json"
        self.sidecar = self.root / "settings.local-keys.json"
        self.history = self.root / "settings.local-key-notices.json"
        self.root.mkdir()
        self.home.mkdir()
        self.env.update(interpreter_cache_environment(self.home / "cache"))
        for folder in ("scripts/settings", "scripts/lib", "scripts/message", "config"):
            (self.root / folder).mkdir(parents=True)
        for source in KEYS.parent.glob("*.sh"):
            _ = shutil.copy2(source, self.root / "scripts/settings" / source.name)
        for name in ("conf.sh", "conf.py", "conf_reads.py", "atomic_file.py", "py"):
            _ = shutil.copy2(KEYS.parents[1] / "lib" / name, self.root / "scripts/lib" / name)
        for name in ("settings", "send", "postmaster", "codex_daemon"):
            source = KEYS.parents[2] / "config" / f"{name}.conf"
            _ = shutil.copy2(source, self.root / "config" / source.name)
        _ = shutil.copy2(KEYS.with_name("local_key_notices.py"), self.root / "scripts/settings/local_key_notices.py")
        for name in ("send_result.py", "send_budget.py", "session_send.py"):
            _ = shutil.copy2(KEYS.parents[1] / "message" / name, self.root / "scripts/message" / name)
        _ = (self.root / "scripts/message/send.py").write_text('''import json, sys, runpy
from pathlib import Path
if __name__ != "__main__":
    protocol = runpy.run_path(REAL_SENDER)
    delivery_timeout = protocol["delivery_timeout"]
else:
    root = Path(__file__).resolve().parents[2]
    with (root / "sent.jsonl").open("a") as output:
        output.write(json.dumps(sys.argv[1:]) + "\\n")
    if (root / "fail-send").exists():
        print("FAILED: fixture")
        raise SystemExit(3)
    if (root / "queue-send").exists():
        print("QUEUED: fixture")
        raise SystemExit(1)
    print("SENT: fixture")
'''.replace("REAL_SENDER", repr(str(KEYS.parents[1] / "message/send.py"))))
        self.write_settings({"shared": 1})
        _ = (self.root / ".gitattributes").write_text("settings.json filter=claude-settings\n")
        _ = self.git("init", "-qb", "baseline")
        _ = (self.root / ".git/info/exclude").write_text("*\n")
        _ = self.git("add", "-f", "settings.json", ".gitattributes")
        _ = self.git("commit", "-qm", "Baseline")
        _ = self.git("config", "filter.claude-settings.clean", "bash scripts/settings/clean_settings_json.sh")
        _ = self.git("config", "filter.claude-settings.smudge", "bash scripts/settings/smudge_settings_json.sh")
        _ = self.git("config", "filter.claude-settings.required", "true")

    def run_command(self, *arguments: str, contents: str = "", check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=self.root, env=self.env, input=contents,
                              capture_output=True, text=True, check=check, timeout=TEST_HANG_GUARD_SECS)

    def git(self, *arguments: str, contents: str = "") -> str:
        return self.run_command("git", *arguments, contents=contents).stdout

    def write_settings(self, settings: object) -> None:
        _ = self.settings.write_text(json.dumps(settings, indent=2) + "\n")

    def read_json(self, path: Path) -> object:
        return cast(object, json.loads(path.read_text()))

    def sends(self) -> list[list[str]]:
        path = self.root / "sent.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def refresh(self) -> subprocess.CompletedProcess[str]:
        return self.run_command(BASH, "scripts/settings/refresh_filtered_index.sh")

    def test_unknown_key_is_saved_before_checkout_and_does_not_change_diff_or_status(self) -> None:
        local = {"shared": 1, "newSetting": {"enabled": True}, "voice": {"enabled": True}}
        self.write_settings(local)
        cleaned = self.run_command(BASH, "scripts/settings/clean_settings_json.sh", contents=self.settings.read_text())
        self.assertEqual(json.loads(cleaned.stdout), {"shared": 1})
        self.assertEqual(self.read_json(self.sidecar), {"newSetting": {"enabled": True}, "voice": {"enabled": True}})
        self.assertEqual(self.sends(), [])
        self.assertIn('"pending"', self.history.read_text())
        self.assertNotIn("SENT:", cleaned.stdout + cleaned.stderr)
        self.assertEqual(self.git("diff", "HEAD", "--", "settings.json"), "")
        _ = self.refresh()
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.read_json(self.settings), local)
        self.settings.unlink()
        _ = self.git("checkout", "--", "settings.json")
        self.assertEqual(self.read_json(self.settings), local)
        self.write_settings({"shared": 1})
        restored = self.run_command(BASH, "-c", 'source scripts/settings/settings_local_keys.sh; settings_local_keys_restore')
        self.assertIn("restored", restored.stdout)
        self.assertEqual(self.read_json(self.settings), local)
        self.assertEqual(len(self.sends()), 1)

    def test_incoming_commit_promotes_an_unlisted_key_before_head_advances(self) -> None:
        self.write_settings({"shared": 1, "newSetting": "local", "voice": {"enabled": True}})
        _ = self.refresh()
        baseline = self.git("rev-parse", "HEAD").strip()
        incoming = {"shared": 1, "newSetting": "committed", "voice": {"enabled": False}}
        blob = self.git("hash-object", "-w", "--stdin", contents=json.dumps(incoming, indent=2) + "\n").strip()
        _ = self.git("update-index", "--cacheinfo", f"100644,{blob},settings.json")
        tree = self.git("write-tree").strip()
        revision = self.git("commit-tree", tree, "-p", baseline, contents="Add shared key\n").strip()
        _ = self.git("update-ref", "refs/heads/incoming", revision)
        _ = self.git("read-tree", baseline)
        before_head = self.run_command(BASH, "scripts/settings/smudge_settings_json.sh", contents=json.dumps(incoming))
        self.assertEqual(json.loads(before_head.stdout), {**incoming, "voice": {"enabled": True}})
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), baseline)
        _ = self.git("checkout", "-q", "incoming")
        self.assertEqual(self.read_json(self.settings), {**incoming, "voice": {"enabled": True}})
        _ = self.refresh()
        self.assertEqual(self.read_json(self.sidecar), {"voice": {"enabled": True}})
        self.write_settings({**incoming, "newSetting": "edited"})
        _ = self.refresh()
        diff = self.git("diff", "HEAD", "--", "settings.json")
        self.assertIn('"newSetting": "edited"', diff)
        self.assertIn("settings.json", self.git("status", "--porcelain", "--untracked-files=no"))
        clean = self.run_command(BASH, "scripts/settings/clean_settings_json.sh", contents=self.settings.read_text())
        self.assertEqual(json.loads(clean.stdout), {"shared": 1, "newSetting": "edited"})

    def test_shared_edits_stay_tracked_while_listed_keys_stay_local(self) -> None:
        self.write_settings({"shared": 2, "theme": "dark", "voice": {"enabled": True}})
        _ = self.refresh()
        self.assertIn('"shared": 2', self.git("diff", "HEAD", "--", "settings.json"))
        self.assertIn("settings.json", self.git("status", "--porcelain", "--untracked-files=no"))
        self.assertEqual(self.sends(), [])

    def test_unreadable_baseline_reports_an_error_and_preserves_live_and_saved_values(self) -> None:
        self.write_settings({"shared": 1, "newSetting": "local"})
        _ = self.refresh()
        before = self.settings.read_bytes(), self.sidecar.read_bytes(), self.history.read_bytes()
        _ = self.git("update-ref", "-d", "HEAD")
        clean = self.run_command(BASH, "scripts/settings/clean_settings_json.sh", contents=self.settings.read_text(), check=False)
        self.assertNotEqual(clean.returncode, 0)
        self.assertEqual(clean.stdout, "")
        self.assertIn("cannot read committed HEAD:settings.json",
                      (self.root / "settings.local-keys.errors.log").read_text())
        smudge = self.run_command(BASH, "scripts/settings/smudge_settings_json.sh", contents='{"shared": 1}\n')
        self.assertEqual(json.loads(smudge.stdout), {"shared": 1, "newSetting": "local"})
        self.assertEqual(smudge.stderr, "")
        self.assertEqual((self.settings.read_bytes(), self.sidecar.read_bytes(), self.history.read_bytes()), before)

    def test_notices_survive_deletion_restart_and_use_live_configuration(self) -> None:
        self.write_settings({"shared": 1, "firstKey": True, "theme": "dark"})
        _ = self.refresh()
        _ = self.refresh()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][1], "natedev")
        _ = (self.root / "config/settings.conf").write_text("local_key_notice_to=fixture-destination\n")
        self.write_settings({"shared": 1, "theme": "dark"})
        _ = self.refresh()
        self.sidecar.unlink()
        self.write_settings({"shared": 1, "firstKey": False, "secondKey": "local"})
        _ = self.refresh()
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[1][1], "fixture-destination")
        self.assertEqual(sends[1][-1], f"Settings key secondKey is local on {socket.gethostname().split(".")[0]}.")

    def test_notices_deliver_without_the_source_checkout_on_the_import_path(self) -> None:
        _ = self.env.pop("PYTHONPATH", None)
        self.write_settings({"shared": 1, "freshKey": True})
        result = self.refresh()
        self.assertEqual(len(self.sends()), 1, result.stderr)
        self.assertIn("freshKey", self.sends()[0][-1])
        self.assertIn('"accepted"', self.history.read_text())

    def test_missing_budget_config_reports_an_error_and_keeps_the_notice_pending(self) -> None:
        (self.root / "config/postmaster.conf").unlink()
        self.write_settings({"shared": 1, "newSetting": True})
        _ = self.refresh()
        result = self.run_command(BASH, "-c", '''source scripts/settings/settings_local_keys.sh
settings_local_keys_notices deliver
''', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("postmaster.conf", result.stderr)
        self.assertIn("no usable committed value", result.stderr)
        self.assertEqual(self.sends(), [])
        self.assertIn('"pending"', self.history.read_text())

    def test_failed_notice_retries_after_key_deletion_and_queued_notice_is_accepted(self) -> None:
        (self.root / "fail-send").touch()
        self.write_settings({"shared": 1, "newSetting": True, "theme": "dark"})
        result = self.refresh()
        self.assertIn("retry pending", result.stderr)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('"pending"', self.history.read_text())
        self.write_settings({"shared": 1, "theme": "dark"})
        (self.root / "fail-send").unlink()
        (self.root / "queue-send").touch()
        _ = self.refresh()
        self.assertEqual(len(self.sends()), 2)
        self.assertIn('"accepted"', self.history.read_text())
        _ = self.refresh()
        self.assertEqual(len(self.sends()), 2)

    def test_session_start_delivers_pending_notices_and_requires_filters(self) -> None:
        self.write_settings({"shared": 1, "newSetting": True})
        _ = self.run_command(BASH, "scripts/settings/clean_settings_json.sh", contents=self.settings.read_text())
        self.assertEqual(self.sends(), [])
        _ = self.run_command(BASH, "scripts/settings/ensure_git_filters.sh")
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.git("config", "filter.claude-settings.required").strip(), "true")

    def test_idle_snapshot_starts_no_notifier_and_changes_no_saved_file(self) -> None:
        self.write_settings({"shared": 1, "newSetting": True})
        _ = self.refresh()
        saved = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.sidecar, self.history)]
        shim = self.root / "scripts/lib/py"
        _ = shim.write_text("#!/bin/sh\nexit 92\n")
        _ = self.run_command(BASH, "scripts/settings/clean_settings_json.sh", contents=self.settings.read_text())
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.sidecar, self.history)], saved)

    def test_watcher_event_runs_one_snapshot_and_keeps_shared_edits_visible(self) -> None:
        keys = self.root / "scripts/settings/settings_local_keys.sh"
        _ = keys.write_text(keys.read_text().replace(
            "settings_local_keys_snapshot() {",
            'settings_local_keys_snapshot() {\n    echo snapshot >>"$_SETTINGS_LOCAL_ROOT/snapshot-count"'))
        count = self.root / "snapshot-count"
        voice = {"enabled": True, "padding": "x" * (256 * 1024)}
        index_blob = self.git("rev-parse", ":settings.json")
        for shared in (1, 2):
            with self.subTest(shared=shared):
                self.write_settings({"shared": shared, "voice": voice})
                self.assertGreater(self.settings.stat().st_size, 128 * 1024)
                _ = count.write_text("")
                _ = self.refresh()
                self.assertEqual(count.read_text().splitlines(), ["snapshot"])
                self.assertEqual(self.read_json(self.sidecar), {"voice": voice})
                self.assertEqual(self.git("rev-parse", ":settings.json"), index_blob)
                status = self.git("status", "--porcelain", "--untracked-files=no")
                self.assertEqual(bool(status), shared != 1)
                self.assertEqual(json.loads(self.git("show", ":settings.json")), {"shared": 1})

    def test_refresh_keeps_an_executable_mode_change_unstaged(self) -> None:
        self.write_settings({"shared": 1, "voice": {"enabled": True}})
        self.settings.chmod(0o755)
        for filemode in ("true", "false"):
            with self.subTest(filemode=filemode):
                _ = self.git("config", "core.filemode", filemode)
                before = self.git("ls-files", "--stage", "--", "settings.json")
                self.assertTrue(before.startswith("100644 "))
                _ = self.refresh()
                self.assertEqual(self.git("ls-files", "--stage", "--", "settings.json"), before)
                status = self.git("status", "--porcelain", "--untracked-files=no")
                self.assertEqual(status, " M settings.json\n" if filemode == "true" else "")
                self.assertEqual(self.settings.stat().st_mode & 0o777, 0o755)

    def test_snapshot_failure_stops_required_filter_and_keeps_its_error_log(self) -> None:
        self.write_settings({"shared": 1, "newSetting": True})
        self.sidecar.mkdir()
        failed = self.run_command("git", "add", "settings.json", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("cannot preserve", failed.stderr)
        self.assertTrue((self.root / "settings.local-keys.errors.log").read_text())
        self.assertEqual(json.loads(self.git("show", ":settings.json")), {"shared": 1})
        before = self.settings.read_bytes()
        log = self.root / "settings.local-keys.errors.log"
        for caller in ("refresh_filtered_index", "ensure_git_filters"):
            with self.subTest(caller=caller):
                log.unlink()
                failed = self.run_command(BASH, f"scripts/settings/{caller}.sh", check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(f"{caller}: clean failed; index refresh stopped.", failed.stderr)
                self.assertTrue(log.is_file())
                self.assertIn(f"settings: destination is a directory: {self.sidecar}", log.read_text())
                self.assertEqual(self.settings.read_bytes(), before)
                self.assertEqual(json.loads(self.git("show", ":settings.json")), {"shared": 1})
        self.assertEqual(self.sends(), [])

    def test_refresh_keeps_racing_shared_edits_and_another_processes_staged_blob(self) -> None:
        real_git = shlex.quote(shutil.which("git") or "/usr/bin/git")
        shims = self.root / "shims"
        shims.mkdir()
        marker = self.root / "refresh-interrupted"
        original = {"shared": 1, "voice": {"enabled": True}}
        _ = (self.root / "original.json").write_text(json.dumps(original, indent=2) + "\n")
        _ = (self.root / "changed.json").write_text('{"shared": 22222}')
        staged_blob = self.git("hash-object", "--no-filters", "-w", "--stdin",
                               contents='{"shared": 33333}').strip()
        baseline_blob = self.git("rev-parse", "HEAD:settings.json").strip()
        for change in ("shared edit", "returned input", "staged blob"):
            with self.subTest(change=change):
                _ = self.git("read-tree", "HEAD")
                self.write_settings(original)
                marker.unlink(missing_ok=True)
                action = "cp changed.json settings.json"
                expected_blob = baseline_blob
                if change == "returned input":
                    action += "\ncp original.json settings.json"
                elif change == "staged blob":
                    action = f"{real_git} update-index --cacheinfo 100644,{staged_blob},settings.json"
                    expected_blob = staged_blob
                shim = shims / "git"
                _ = shim.write_text(f'''#!{BASH}
if [[ " $* " == *" update-index "* && ! -e refresh-interrupted ]]; then
    touch refresh-interrupted
    {action}
fi
exec {real_git} "$@"
''')
                shim.chmod(0o755)
                old_path = self.env.get("PATH", os.defpath)
                self.env["PATH"] = str(shims) + os.pathsep + old_path
                try:
                    _ = self.refresh()
                finally:
                    self.env["PATH"] = old_path
                self.assertTrue(marker.is_file())
                self.assertEqual(self.git("rev-parse", ":settings.json").strip(), expected_blob)
                status = self.git("status", "--porcelain", "--untracked-files=no")
                if change == "returned input":
                    self.assertEqual(status, "")
                else:
                    self.assertIn("settings.json", status)
                if change == "shared edit":
                    self.assertEqual(self.read_json(self.settings), {"shared": 22222})
                self.assertEqual(self.read_json(self.sidecar), {"voice": {"enabled": True}})

    def test_clean_snapshots_its_input_instead_of_a_changed_working_file(self) -> None:
        self.write_settings({"shared": 2, "voice": {"enabled": False}})
        clean = self.run_command(BASH, "scripts/settings/clean_settings_json.sh",
                                 contents='{"shared": 1, "voice": {"enabled": true}}')
        self.assertEqual(json.loads(clean.stdout), {"shared": 1})
        self.assertEqual(self.read_json(self.sidecar), {"voice": {"enabled": True}})
        self.assertEqual(self.read_json(self.settings), {"shared": 2, "voice": {"enabled": False}})

    def test_shell_notices_use_the_supported_interpreter_instead_of_path_python(self) -> None:
        shims = self.root / "shims"
        shims.mkdir()
        marker = self.root / "path-python-ran"
        shim = shims / "python3"
        _ = shim.write_text(f"#!{BASH}\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        shim.chmod(0o755)
        self.env["PATH"] = str(shims) + os.pathsep + self.env.get("PATH", os.defpath)
        self.env["CLAUDE_PYTHON"] = sys.executable
        _ = self.run_command(BASH, "-c", '''source scripts/settings/settings_local_keys.sh
settings_local_keys_notices record '["freshKey"]'
''')
        self.assertIn('"pending"', self.history.read_text())
        _ = self.run_command(BASH, "-c", '''source scripts/settings/settings_local_keys.sh
settings_local_keys_notices deliver
''')
        self.assertIn('"accepted"', self.history.read_text())
        self.assertEqual(len(self.sends()), 1)
        self.assertIn("freshKey", self.sends()[0][-1])
        self.assertFalse(marker.exists())

    def test_sourcing_local_keys_reads_neither_git_nor_jq(self) -> None:
        shims = self.root / "shims"
        shims.mkdir()
        marker = self.root / "baseline-command-ran"
        for command in ("git", "jq"):
            shim = shims / command
            _ = shim.write_text(f"#!{BASH}\ntouch {shlex.quote(str(marker))}\nexit 1\n")
            shim.chmod(0o755)
        self.env["PATH"] = str(shims) + os.pathsep + self.env.get("PATH", os.defpath)
        sourced = self.run_command(BASH, "-c", "source scripts/settings/settings_local_keys.sh")
        self.assertEqual(sourced.stdout, "")
        self.assertEqual(sourced.stderr, "")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    _ = unittest.main()
