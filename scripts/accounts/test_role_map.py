"""One authoritative role map, isolated remote transport, and local launch readiness."""
from __future__ import annotations

import io
import json
import os
import shlex
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Self, cast, override
from unittest.mock import patch

from scripts.accounts import accounts, role_map
from scripts.agents import codex_mesh
from scripts.whoami import agent_accounts


class RoleMapReadTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 10, 16, 15, 24, tzinfo=timezone.utc).astimezone(tz)


class RoleMapTests(unittest.TestCase):
    home: Path = Path()
    server_home: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home, self.server_home = root / "mac home", root / "natedev home"
        for home in (self.home, self.server_home):
            (home / ".claude").mkdir(parents=True)
            (home / "notes").mkdir()
            _ = (home / ".claude.json").write_text("{}")
            for label in ("claude1", "claude2", "claude3", "codex1", "codex3"):
                _ = (home / "notes" / f"{label}.md").write_text(
                    f"---\nlogin: {label}@example.invalid\n---\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes"),
            "USER_ZONE": "America/New_York", "PATH": os.defpath,
        }, clear=True)))
        _ = self.enterContext(patch.object(role_map, "ROLE_MAP_HOST", "natedev"))
        _ = self.enterContext(patch.object(socket, "gethostname", return_value="macbook"))
        # Every test must opt in to a fake transport; never attempt real ssh.
        _ = self.enterContext(patch.object(role_map, "ssh_role_map",
            side_effect=AssertionError("test did not configure fake role-map ssh")))

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def remote(self, arguments: tuple[str, ...]) -> str:
        """Run the real remote CLI against a second scratch home, never a shell or ssh."""
        with (patch.dict(os.environ, {"HOME": str(self.server_home),
                                     "AGENT_NOTES_DIR": str(self.server_home / "notes")}),
              patch.object(socket, "gethostname", return_value="natedev")):
            code, output, error = self.cli(*arguments)
        self.assertEqual((code, error), (0, ""))
        return output

    def seed(self, home: Path, *, groups: dict[str, str], workers: str = "codex1") -> None:
        group_file = home / ".local/state/accounts/groups.json"
        group_file.parent.mkdir(parents=True, exist_ok=True)
        _ = group_file.write_text(json.dumps(groups))
        config = home / ".claude/config/accounts.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(json.dumps({"workers": workers}))

    def files(self, home: Path) -> dict[str, bytes]:
        return {str(path.relative_to(home)): path.read_bytes()
                for path in home.rglob("*") if path.is_file()}

    def test_mac_group_write_changes_only_natedev_and_preserves_transition_history(self) -> None:
        self.seed(self.home, groups={"units": "claude3"})
        self.seed(self.server_home, groups={"showrunners": "claude1"})
        before = self.files(self.home)
        reset = datetime(2026, 10, 20, tzinfo=timezone.utc)
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh:
            transition = accounts.set_group_account("units", "claude2", why="weekly refusal",
                now=RoleMapReadTime.now(timezone.utc),
                origin=accounts.RefusalOrigin("weekly", accounts.KnownReset(reset)), move_id="move-from-mac")
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(transition.move_id, "move-from-mac")
        self.assertEqual(self.files(self.home), before)
        stored = cast(dict[str, object], json.loads(
            (self.server_home / ".local/state/accounts/groups.json").read_text()))
        self.assertEqual(stored["showrunners"], "claude1")
        selected = cast(dict[str, object], stored["units"])
        self.assertEqual(selected, {"label": "claude2", "cause": "refusal", "window": "weekly",
                                   "reset": reset.isoformat(), "move_id": "move-from-mac"})
        history = (self.server_home / ".local/state/accounts/roles.jsonl").read_text().splitlines()
        self.assertEqual(len(history), 1)
        change = cast(dict[str, object], json.loads(history[0]))
        self.assertEqual(change, {"at": RoleMapReadTime.now(timezone.utc).isoformat(), "group": "units",
            "label": "claude2", "why": "weekly refusal", "cause": "refusal", "window": "weekly",
            "reset": reset.isoformat(), "move_id": "move-from-mac"})

    def test_failed_mac_write_refuses_once_without_changing_local_map_history_or_cache(self) -> None:
        self.seed(self.home, groups={"units": "claude3"})
        before = self.files(self.home)
        with patch.object(role_map, "ssh_role_map", side_effect=OSError("connection refused")):
            with self.assertRaises(accounts.AccountsError) as refused:
                _ = accounts.set_group_account("units", "claude2", why="operator choice", move_id="failed-move")
        message = str(refused.exception)
        self.assertEqual(message, "accounts: natedev refused units → claude2")
        self.assertEqual(self.files(self.home), before)

    def test_mac_workers_write_preserves_remote_groups_and_all_move_evidence(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude1"})
        before = self.files(self.home)
        change = role_map.RoleMapChange("workers", "codex3", "quota exhausted",
            RoleMapReadTime.now(timezone.utc).isoformat(), "worker-move", "refusal", "five_hour", "2026-10-16T17:00:00+00:00")
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh:
            role_map.write_role_map(change)
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(self.files(self.home), before)
        config = self.server_home / ".claude/config/accounts.json"
        self.assertEqual(json.loads(config.read_text()), {"workers": "codex3"})
        groups = self.server_home / ".local/state/accounts/groups.json"
        self.assertEqual(json.loads(groups.read_text()), {"units": "claude2", "showrunners": "claude1"})
        history = (groups.parent / "roles.jsonl").read_text().splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0]), {"role": "workers", "label": "codex3",
            "account_label": "codex3", "why": "quota exhausted", "at": change.at,
            "move_id": "worker-move", "cause": "refusal", "window": "five_hour", "reset": change.reset})

    def test_mac_cli_workers_selection_writes_natedev_without_requiring_a_local_account(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex1")
        self.seed(self.server_home, groups={"units": "claude2"})
        before = self.files(self.home)
        self.assertFalse((self.home / ".codex3").exists())
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh:
            self.assertEqual(self.cli("set-role", "workers", "codex3"), (0, "", ""))
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(self.files(self.home), before)
        config = self.server_home / ".claude/config/accounts.json"
        self.assertEqual(json.loads(config.read_text()), {"workers": "codex3"})
        entries = (self.server_home / ".local/state/accounts/roles.jsonl").read_text().splitlines()
        self.assertEqual(len(entries), 1)
        entry = cast(dict[str, object], json.loads(entries[0]))
        self.assertEqual((entry["role"], entry["label"], entry["why"]), ("workers", "codex3", "by hand"))
        self.assertNotIn("reset", entry)

    def test_mac_read_uses_natedev_selection_and_refreshes_cache_without_overwriting_local_map(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude1"})
        local_groups = accounts.groups_file().read_bytes()
        local_workers = accounts.accounts_config_file().read_bytes()
        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh,
              patch.object(role_map, "datetime", RoleMapReadTime)):
            selected = role_map.read_role_map()
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(selected.groups, {"units": "claude2", "showrunners": "claude1"})
        self.assertEqual(selected.workers, "codex1")
        cache = role_map.role_map_cache_file()
        self.assertTrue(cache.is_file())
        self.seed(self.server_home, groups={"units": "claude1"}, workers="codex3")
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote):
            selected = role_map.read_role_map()
        self.assertEqual((selected.groups, selected.workers), ({"units": "claude1"}, "codex3"))
        before_cache = cache.read_bytes()
        with (patch.object(role_map, "ssh_role_map", side_effect=OSError("offline")),
              redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            cached = role_map.read_role_map()
        self.assertEqual(cached, selected)
        self.assertEqual(cache.read_bytes(), before_cache)
        self.assertEqual(accounts.groups_file().read_bytes(), local_groups)
        self.assertEqual(accounts.accounts_config_file().read_bytes(), local_workers)

    def test_unreachable_natedev_uses_cached_map_and_names_cache_time_in_users_zone(self) -> None:
        self.seed(self.server_home, groups={"units": "claude2"})
        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote),
              patch.object(role_map, "datetime", RoleMapReadTime)):
            expected = role_map.read_role_map()
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(role_map, "ssh_role_map", side_effect=OSError("offline")),
              redirect_stdout(output), redirect_stderr(error)):
            actual = role_map.read_role_map()
        self.assertEqual(actual, expected)
        self.assertEqual((output.getvalue() + error.getvalue()).splitlines(), [
            "role map from 2026-10-16 11:24 EDT; natedev unreachable"])

    def test_unreachable_natedev_without_cache_refuses_instead_of_using_local_selections(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        before = self.files(self.home)
        with patch.object(role_map, "ssh_role_map", side_effect=OSError("offline")):
            with self.assertRaises(role_map.RoleMapUnavailable) as refused:
                _ = role_map.read_role_map()
        self.assertEqual(str(refused.exception),
                         "accounts: cannot read role map from natedev; no usable map")
        self.assertEqual(self.files(self.home), before)

    def test_unset_workers_role_refuses_instead_of_resolving_a_default_home(self) -> None:
        self.seed(self.server_home, groups={})
        _ = (self.server_home / ".claude/config/accounts.json").write_text("{}")
        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote),
              patch.object(agent_accounts, "codex_account_home",
                           side_effect=AssertionError("unset workers must not choose a home"))):
            with self.assertRaises(ValueError) as refused:
                _ = agent_accounts.workers_home()
        self.assertEqual(str(refused.exception),
                         "accounts: workers has no mapped account; run accounts.py set-role workers <codexN>")

    def test_mac_launch_reads_the_map_once_per_command_and_the_next_command_observes_a_change(self) -> None:
        self.seed(self.server_home, groups={"units": "claude2"})
        self.assertEqual(accounts.setup("claude2"), [])
        self.assertEqual(accounts.setup("claude3"), [])
        for label in ("claude2", "claude3"):
            _ = (self.home / f".{label}/.claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh,
              patch.object(os, "execvpe") as launch):
            self.assertEqual(self.cli("exec", "units", "--version"), (0, "", ""))
            self.assertEqual(ssh.call_count, 1)
            self.assertEqual(cast(dict[str, str], launch.call_args.args[2])["CLAUDE_CONFIG_DIR"],
                             str(self.home / ".claude2"))
            self.seed(self.server_home, groups={"units": "claude3"})
            self.assertEqual(self.cli("exec", "units", "--version"), (0, "", ""))
            self.assertEqual(ssh.call_count, 2)
            self.assertEqual(cast(dict[str, str], launch.call_args.args[2])["CLAUDE_CONFIG_DIR"],
                             str(self.home / ".claude3"))

    def test_mapped_claude_role_refuses_missing_setup_and_login_for_fresh_and_resumed_launches(self) -> None:
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude2"})
        for is_set_up in (False, True):
            if is_set_up:
                self.assertEqual(accounts.setup("claude2"), [])
            for role in ("units", "showrunners"):
                for arguments in ((), ("--resume", "existing-thread")):
                    with self.subTest(is_set_up=is_set_up, role=role, arguments=arguments):
                        state = "not logged in" if is_set_up else "not set up"
                        step = (f"run CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude2'))} claude and /login"
                                if is_set_up else "run accounts.py setup claude2")
                        expected = f"accounts: {role} is on claude2, which is {state} on this machine; {step}\n"
                        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote),
                              patch.object(os, "execvpe") as launch,
                              patch.object(accounts, "pick", side_effect=AssertionError("launch chose another account"))):
                            code, output, error = self.cli("exec", role, *arguments)
                        self.assertEqual((code, output, error), (2, "", expected))
                        launch.assert_not_called()

    def test_mapped_workers_refuse_missing_setup_and_login_without_launching_another_home(self) -> None:
        self.seed(self.server_home, groups={})
        for is_set_up in (False, True):
            if is_set_up:
                (self.home / ".codex1").mkdir()
            with self.subTest(is_set_up=is_set_up):
                state = "not logged in" if is_set_up else "not set up"
                step = (f"CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login"
                        if is_set_up else "run accounts.py setup codex1")
                with (patch.object(role_map, "ssh_role_map", side_effect=self.remote),
                      patch.object(os, "execvpe") as launch):
                    code, output, error = self.cli("exec", "workers", "--version")
                self.assertEqual((code, output, error), (2, "",
                    f"accounts: workers is on codex1, which is {state} on this machine; {step}\n"))
                launch.assert_not_called()

    def test_mesh_launch_refuses_the_mapped_missing_account_with_one_line_before_starting_a_server(self) -> None:
        self.seed(self.server_home, groups={})
        run = self.home / "scratch-run"
        run.mkdir()
        prompt = run / "prompt.txt"
        _ = prompt.write_text("Scratch delegate task")
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(role_map, "ssh_role_map", side_effect=self.remote),
              patch.object(codex_mesh, "_start_server") as start,
              redirect_stdout(output), redirect_stderr(error)):
            code = codex_mesh.main(["start", "--session-dir", str(run), "--name", "scratch-test",
                "--role", "test", "--cwd", str(run), "--prompt-file", str(prompt),
                "--summary-file", str(run / "summary.txt"), "--log-file", str(run / "mesh.log")])
        self.assertEqual((code, output.getvalue(), error.getvalue()), (1, "",
            "accounts: workers is on codex1, which is not set up on this machine; run accounts.py setup codex1\n"))
        start.assert_not_called()

    def test_status_keeps_its_table_and_prints_the_same_missing_setup_and_login_refusals(self) -> None:
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude2"})
        for is_set_up in (False, True):
            if is_set_up:
                self.assertEqual(accounts.setup("claude2"), [])
                (self.home / ".codex1").mkdir()
            with self.subTest(is_set_up=is_set_up):
                state = "not logged in" if is_set_up else "not set up"
                claude_step = (f"run CLAUDE_CONFIG_DIR={shlex.quote(str(self.home / '.claude2'))} claude and /login"
                               if is_set_up else "run accounts.py setup claude2")
                codex_step = (f"CODEX_HOME={shlex.quote(str(self.home / '.codex1'))} codex login"
                              if is_set_up else "run accounts.py setup codex1")
                with (patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh,
                      patch.object(accounts, "_stored_usage", return_value={})):
                    code, output, error = self.cli("status")
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(ssh.call_count, 1)
                self.assertIn("| Folder", output)
                for role in ("units", "showrunners"):
                    self.assertIn(f"accounts: {role} is on claude2, which is {state} on this machine; {claude_step}",
                                  output.splitlines())
                self.assertIn(f"accounts: workers is on codex1, which is {state} on this machine; {codex_step}",
                              output.splitlines())


if __name__ == "__main__":
    _ = unittest.main()
