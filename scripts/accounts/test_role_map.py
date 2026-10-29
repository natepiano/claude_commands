"""One authoritative role map, isolated remote transport, and local launch readiness."""
from __future__ import annotations

import io
import json
import os
import shlex
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Self, cast, override
from unittest.mock import Mock, patch

from scripts.accounts import accounts, role_map
from scripts.accounts.nix_env_fake import fake_nix_loader, imports_with_nix_loader
from scripts.agents import codex_mesh
from scripts.whoami import agent_accounts

send_role_map_command = role_map.ssh_role_map


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
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.object(accounts, "import_module", side_effect=imports_with_nix_loader(fake_nix_loader()))))
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
        _ = self.enterContext(patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.home)))
        # Every test must opt in to a fake transport; never attempt real ssh.
        _ = self.enterContext(patch.object(role_map, "ssh_role_map",
            side_effect=AssertionError("test did not configure fake role-map ssh")))

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def test_named_workers_route_reaches_a_mac_launch_and_its_offline_cache(self) -> None:
        self.seed(self.home, groups={"showrunners": "claude1"})
        self.seed(self.server_home, groups={"showrunners": "claude2"})
        self.assertEqual(accounts.setup("claude3"), [])
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude3@example.invalid"}}))
        # A Mac write goes through the real CLI on a fake authority.
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.home)),
              patch.object(role_map, "ssh_role_map", side_effect=self.remote),
              patch.object(accounts, "datetime", RoleMapReadTime)):
            role_map.write_role_map(role_map.WorkersClaudeRouteChange(
                role_map.NamedWorkersClaudeAccount("claude3", RoleMapReadTime.now(timezone.utc) + timedelta(days=2)), "queued workers route",
                RoleMapReadTime.now(timezone.utc).isoformat()))
            with patch.object(role_map, "ssh_role_map", side_effect=self.remote) as fetch:
                selected = accounts.account_for_unscoped_launch(
                    {"CLAUDE_CONFIG_DIR": str(self.home / ".claude99")}, workers=True)
            fetch.assert_called_once_with(("role-map-read",))
            self.assertEqual(selected, accounts.NamedAccount("claude3", directory))
            with (patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline")),
                  redirect_stderr(io.StringIO()) as errors):
                cached = accounts.account_for_unscoped_launch({}, workers=True)
            self.assertEqual(cached, selected)
            self.assertIn("natedev unreachable", errors.getvalue())
            role_map.write_role_map(role_map.WorkersClaudeRouteChange(
                role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "Codex recovered",
                RoleMapReadTime.now(timezone.utc).isoformat()))
            self.assertIsInstance(role_map.read_role_map().workers_claude, role_map.DefaultWorkersClaudeAccount)
        server_config = self.server_home / ".claude/config/accounts.json"
        self.assertNotIn("workers_claude", cast(dict[str, object], json.loads(server_config.read_text())))

    def test_workers_launch_refuses_an_unreadable_map_without_a_cache(self) -> None:
        self.seed(self.home, groups={})
        self.assertEqual(accounts.setup("claude1"), [])
        directory = accounts.account_dir("claude1")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude1@example.invalid"}}))
        caller = {"CLAUDE_CONFIG_DIR": str(directory)}
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.home)),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline"))):
            with self.assertRaisesRegex(accounts.AccountsError, "no usable map"):
                _ = accounts.account_for_unscoped_launch(caller, workers=True)
        # An old map with no route field still uses the caller's account.
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.home)),
              patch.object(role_map, "ssh_role_map", return_value='{"groups": {}, "workers": "codex1"}')):
            self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                             accounts.NamedAccount("claude1", directory))
        configuration = accounts.accounts_config_file()
        _ = configuration.write_text(json.dumps({"workers": "codex1", "workers_claude": {
            "label": "codex3", "until": "2026-10-20T00:00:00+00:00"}}))
        with (patch.object(role_map, "is_role_map_host", return_value=True),
              self.assertRaisesRegex(accounts.AccountsError, "no usable map")):
            _ = accounts.account_for_unscoped_launch(caller, workers=True)

    def test_workers_route_uses_the_default_account_at_its_end_time(self) -> None:
        self.seed(self.home, groups={})
        for label in ("claude1", "claude3"):
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
        until = RoleMapReadTime.now(timezone.utc)
        wire = json.dumps({"groups": {}, "workers": "codex1", "workers_claude": {
            "label": "claude3", "until": until.isoformat()}})
        caller = {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude1"))}
        with patch.object(role_map, "ssh_role_map", return_value=wire):
            for now, label in ((until - timedelta(seconds=1), "claude3"),
                               (until, "claude1"), (until + timedelta(seconds=1), "claude1")):
                with self.subTest(now=now), patch.object(accounts, "datetime", wraps=datetime, now=Mock(return_value=now)):
                    self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                                     accounts.NamedAccount(label, accounts.account_dir(label)))

    def test_workers_route_without_an_end_time_uses_the_default_account(self) -> None:
        self.seed(self.home, groups={})
        self.assertEqual(accounts.setup("claude1"), [])
        directory = accounts.account_dir("claude1")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude1@example.invalid"}}))
        for route in ("claude3", {"label": "claude3"}):
            with self.subTest(route=route), patch.object(role_map, "ssh_role_map", return_value=json.dumps({
                    "groups": {}, "workers": "codex1", "workers_claude": route})):
                self.assertEqual(accounts.account_for_unscoped_launch(
                    {"CLAUDE_CONFIG_DIR": str(directory)}, workers=True), accounts.NamedAccount("claude1", directory))

    def test_remote_command_refusal_retains_the_last_nonempty_stderr_line(self) -> None:
        message = "next: units has 1 entries; position 3 is out of range"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                ["fixture ssh"], 1, stdout="", stderr=f"fixture diagnostic\n{message}\n\n")) as transport:
            with self.assertRaises(role_map.RemoteCommandRefused) as error:
                _ = send_role_map_command(("next", "remove", "units", "3"))
        self.assertEqual(str(error.exception), message)
        transport.assert_called_once()

    def test_remote_connection_failure_remains_a_transport_error_with_its_reason(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                ["fixture ssh"], 255, stdout="", stderr="fixture cannot connect\n")) as transport:
            with self.assertRaises(OSError) as error:
                _ = send_role_map_command(("next", "remove", "units", "3"))
        self.assertNotIsInstance(error.exception, role_map.RemoteCommandRefused)
        self.assertEqual(str(error.exception), "fixture cannot connect")
        transport.assert_called_once()

    def test_role_history_append_is_one_whole_line_write(self) -> None:
        self.seed(self.home, groups={"units": "claude1"})
        with (patch.object(socket, "gethostname", return_value="natedev"),
              patch.object(os, "write", wraps=os.write) as write):
            _ = accounts.set_group_account("units", "claude2", why="auto: line-write", origin=accounts.AutoOrigin())
        write.assert_called_once()
        line = cast(bytes, write.call_args.args[1])
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(json.loads(line)["why"], "auto: line-write")
        self.assertEqual(accounts.roles_history_file().read_bytes(), line)

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

    def test_scratch_home_reads_its_groups_and_workers_without_transport_or_cache_changes(self) -> None:
        self.seed(self.home, groups={"units": "claude3", "showrunners": "claude2"}, workers="codex3")
        self.seed(self.server_home, groups={"units": "claude1"}, workers="codex1")
        cache = role_map.role_map_cache_file()
        _ = cache.write_text(json.dumps({"at": RoleMapReadTime.now(timezone.utc).isoformat(),
                                        "map": {"groups": {"units": "claude1"}, "workers": "codex1"}}))
        before = self.files(self.home), self.files(self.server_home)
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.server_home)),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("scratch read reached ssh")) as ssh):
            selected = role_map.read_role_map()
        ssh.assert_not_called()
        self.assertEqual(selected.groups, {"units": "claude3", "showrunners": "claude2"})
        self.assertEqual(selected.workers, "codex3")
        self.assertEqual((self.files(self.home), self.files(self.server_home)), before)

        alias = self.home.parent / "login home alias"
        alias.symlink_to(self.home, target_is_directory=True)
        for process_home, login_home in ((alias, self.home), (self.home, alias)):
            with (self.subTest(process_home=process_home, login_home=login_home),
                  patch.dict(os.environ, {"HOME": str(process_home)}),
                  patch.object(role_map, "_login_home", return_value=role_map.LoginHome(login_home)),
                  patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh):
                selected = role_map.read_role_map()
            ssh.assert_called_once_with(("role-map-read",))
            self.assertEqual((selected.groups, selected.workers), ({"units": "claude1"}, "codex1"))

    def test_scratch_home_writes_its_groups_workers_and_history_without_remote_changes(self) -> None:
        self.seed(self.home, groups={"units": "claude3", "showrunners": "claude1"})
        self.seed(self.server_home, groups={"units": "claude1"}, workers="codex1")
        remote_before = self.files(self.server_home)
        at = RoleMapReadTime.now(timezone.utc).isoformat()
        group = role_map.RoleMapChange("units", "claude2", "scratch selection", at, "scratch-move")
        workers = role_map.RoleMapChange("workers", "codex3", "scratch workers", at)
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHome(self.server_home)),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("scratch write reached ssh")) as ssh):
            role_map.write_role_map(group)
            role_map.write_role_map(workers)
            selected = role_map.read_role_map()
        ssh.assert_not_called()
        groups = cast(dict[str, object], json.loads(accounts.groups_file().read_text()))
        self.assertEqual(groups["units"], {"label": "claude2", "move_id": "scratch-move",
                                          "cause": "by hand", "window": "unknown", "why": "scratch selection"})
        self.assertEqual(groups["showrunners"], "claude1")
        self.assertEqual(selected.groups, groups)
        self.assertEqual(selected.workers, "codex3")
        self.assertEqual(json.loads(accounts.accounts_config_file().read_text()), {"workers": "codex3"})
        history = [json.loads(line) for line in accounts.groups_file().with_name("roles.jsonl").read_text().splitlines()]
        self.assertEqual(history, [
            {"at": at, "group": "units", "label": "claude2", "why": "scratch selection",
             "move_id": "scratch-move", "cause": "by hand", "window": "unknown"},
            {"at": at, "role": "workers", "label": "codex3", "account_label": "codex3",
             "why": "scratch workers", "cause": "by hand", "window": "unknown"},
        ])
        self.assertEqual(self.files(self.server_home), remote_before)
        self.assertFalse(role_map.role_map_cache_file().exists())

    def test_unreadable_login_home_keeps_reads_and_writes_in_the_process_home(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        change = role_map.RoleMapChange("units", "claude2", "isolated selection",
                                        RoleMapReadTime.now(timezone.utc).isoformat(), "unread-login-move")
        with (patch.object(role_map, "_login_home", return_value=role_map.LoginHomeUnread()),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unread login home reached ssh")) as ssh):
            before = role_map.read_role_map()
            role_map.write_role_map(change)
            after = role_map.read_role_map()
        ssh.assert_not_called()
        self.assertEqual((before.groups, before.workers), ({"units": "claude3"}, "codex3"))
        stored = cast(dict[str, object], json.loads(accounts.groups_file().read_text()))
        self.assertEqual(stored["units"], {"label": "claude2", "move_id": "unread-login-move",
                                          "cause": "by hand", "window": "unknown", "why": "isolated selection"})
        self.assertEqual((after.groups, after.workers), (stored, "codex3"))
        history = accounts.groups_file().with_name("roles.jsonl").read_text().splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0])["move_id"], "unread-login-move")
        self.assertFalse(role_map.role_map_cache_file().exists())

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
                                   "reset": reset.isoformat(), "move_id": "move-from-mac", "why": "weekly refusal"})
        history = (self.server_home / ".local/state/accounts/roles.jsonl").read_text().splitlines()
        self.assertEqual(len(history), 1)
        change = cast(dict[str, object], json.loads(history[0]))
        self.assertEqual(change, {"at": RoleMapReadTime.now(timezone.utc).isoformat(), "group": "units",
            "label": "claude2", "why": "weekly refusal", "cause": "refusal", "window": "weekly",
            "reset": reset.isoformat(), "move_id": "move-from-mac"})

    def test_automatic_group_cause_and_rule_reason_survive_remote_transport_and_transition_readback(self) -> None:
        self.seed(self.home, groups={"units": "claude1"})
        self.seed(self.server_home, groups={"units": "claude1", "showrunners": "claude3"})
        before = self.files(self.home)
        change = role_map.RoleMapChange("units", "claude2", "auto: units-low",
            RoleMapReadTime.now(timezone.utc).isoformat(), "automatic-transition", cause="auto")
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote):
            role_map.write_role_map(change)
            transition = accounts.group_transition("units")
        self.assertIsInstance(transition, accounts.GroupTransition)
        assert isinstance(transition, accounts.GroupTransition)
        self.assertEqual(transition.move_id, "automatic-transition")
        self.assertEqual(transition.why, accounts.RecordedTransitionReason("auto: units-low"))
        self.assertEqual(accounts.move_origin_fields(transition.origin), {"cause": "auto", "window": "unknown"})
        history = self.server_home / ".local/state/accounts/roles.jsonl"
        self.assertEqual(json.loads(history.read_text()), {
            "at": change.at, "group": "units", "label": "claude2", "why": "auto: units-low",
            "move_id": "automatic-transition", "cause": "auto", "window": "unknown"})
        # The readback may create a transport cache, but it never changes local selections or history.
        for path, contents in before.items():
            self.assertEqual((self.home / path).read_bytes(), contents)
        self.assertFalse((self.home / ".local/state/accounts/roles.jsonl").exists())

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

    def test_authoritative_workers_write_preserves_home_cutovers_and_every_other_configuration_key(self) -> None:
        self.seed(self.home, groups={"units": "claude2"}, workers="codex1")
        configuration = accounts.accounts_config_file()
        original: dict[str, object] = {
            "workers": "codex1",
            "home_cutovers": [
                {"label": "claude2", "login": "claude@example.invalid", "at": "2026-10-16T12:00:00Z"},
                {"label": "codex2", "login": "codex@example.invalid", "at": "2026-10-16T16:00:00Z"},
            ],
            "unit": "codex2",
            "future_setting": {"enabled": True, "values": [1, "kept", None]},
        }
        _ = configuration.write_text(json.dumps(original))
        groups_before = accounts.groups_file().read_bytes()
        change = role_map.RoleMapChange("workers", "codex3", "operator choice",
                                        RoleMapReadTime.now(timezone.utc).isoformat())
        with patch.object(socket, "gethostname", return_value="natedev"):
            role_map.write_role_map(change)
        self.assertEqual(json.loads(configuration.read_text()), {**original, "workers": "codex3"})
        self.assertEqual(accounts.groups_file().read_bytes(), groups_before)
        entries = (accounts.groups_file().parent / "roles.jsonl").read_text().splitlines()
        self.assertEqual(len(entries), 1)
        self.assertEqual(json.loads(entries[0])["label"], "codex3")

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

    def test_status_refuses_an_unavailable_remote_map_unless_stored(self) -> None:
        self.seed(self.home, groups={"units": "claude3", "showrunners": "claude2"}, workers="codex3")
        expected = "accounts: cannot read role map from natedev; no usable map"
        self.assertFalse(role_map.role_map_cache_file().exists())
        before = self.files(self.home)
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=OSError("synthetic offline")) as ssh,
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("no signed-in Claude")),
              patch.object(accounts, "_codex_live", side_effect=AssertionError("no signed-in Codex")),
              patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected Popen"))):
            for flags in ((), ("--json",)):
                with self.subTest(flags=flags):
                    self.assertEqual(self.cli("status", *flags), (1, "", expected + "\n"))
            self.assertEqual(ssh.call_count, 2)
            ssh.reset_mock()
            code, output, error = self.cli("status", "--json", "--stored")
            ssh.assert_not_called()
        self.assertEqual((code, error), (0, ""))
        document = cast(dict[str, object], json.loads(output))
        rows = cast(list[dict[str, object]], document["accounts"])
        self.assertTrue(rows)
        self.assertTrue(all(row["roles"] == [] for row in rows))
        self.assertIn(expected, cast(list[str], document["problems"]))
        self.assertEqual(self.files(self.home), before)

    def test_stored_role_map_uses_old_cache_without_transport_or_writes_then_restores_remote_reads(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude1"}, workers="codex1")
        cache = role_map.role_map_cache_file()
        _ = cache.write_text(json.dumps({"at": "2001-01-02T03:04:00+00:00", "map": {
            "groups": {"units": "claude1", "showrunners": "claude2"}, "workers": "codex3"}}))
        before = self.files(self.home)
        cache_stat = cache.stat()
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored role map reached ssh")),
              patch.object(subprocess, "run", side_effect=AssertionError("stored role map spawned a process")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("stored role map spawned Popen")),
              role_map.stored_role_map()):
            selected = role_map.read_role_map()
            again = role_map.read_role_map()
        self.assertEqual((selected.groups, selected.workers),
                         ({"units": "claude1", "showrunners": "claude2"}, "codex3"))
        self.assertEqual(again, selected)
        self.assertEqual(self.files(self.home), before)
        self.assertEqual((cache.stat().st_ino, cache.stat().st_mtime_ns), (cache_stat.st_ino, cache_stat.st_mtime_ns))
        with patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh:
            refreshed = role_map.read_role_map()
        ssh.assert_called_once_with(("role-map-read",))
        self.assertEqual((refreshed.groups, refreshed.workers),
                         ({"units": "claude2", "showrunners": "claude1"}, "codex1"))

    def test_stored_status_without_usable_remote_cache_has_empty_roles_and_the_role_problem(self) -> None:
        self.seed(self.home, groups={"units": "claude3", "showrunners": "claude2"}, workers="codex3")
        expected = "accounts: cannot read role map from natedev; no usable map"
        cache = role_map.role_map_cache_file()
        for contents in (None, "not JSON", '{"at": "invalid", "map": {}}'):
            if contents is None:
                cache.unlink(missing_ok=True)
            else:
                _ = cache.write_text(contents)
            before = self.files(self.home)
            with (self.subTest(cache=contents),
                  patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored status reached ssh")),
                  patch.object(agent_accounts, "claude_live", side_effect=AssertionError("stored Claude read")),
                  patch.object(accounts, "_codex_live", side_effect=AssertionError("stored Codex read")),
                  patch.object(subprocess, "run", side_effect=AssertionError("stored subprocess")),
                  patch.object(subprocess, "Popen", side_effect=AssertionError("stored Popen"))):
                code, output, error = self.cli("status", "--json", "--stored")
            self.assertEqual((code, error), (0, ""))
            document = cast(dict[str, object], json.loads(output))
            rows = cast(list[dict[str, object]], document["accounts"])
            self.assertTrue(rows)
            self.assertTrue(all(row["roles"] == [] for row in rows))
            self.assertIn(expected, cast(list[str], document["problems"]))
            self.assertEqual(self.files(self.home), before)

    def test_stored_status_uses_cached_group_assignments_and_workers_instead_of_local_selections(self) -> None:
        self.seed(self.home, groups={"units": "claude3"}, workers="codex3")
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (self.home / ".claude2/.claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude2@example.invalid"}}))
        cache = role_map.role_map_cache_file()
        _ = cache.write_text(json.dumps({"at": "2001-01-02T03:04:00+00:00", "map": {
            "groups": {"units": "claude2", "showrunners": "claude2"}, "workers": "codex1"}}))
        before = self.files(self.home)
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("stored status reached ssh")),
              patch.object(agent_accounts, "claude_live", side_effect=AssertionError("stored Claude read")),
              patch.object(accounts, "_codex_live", side_effect=AssertionError("stored Codex read"))):
            code, output, error = self.cli("status", "--json", "--stored")
        self.assertEqual((code, error), (0, ""))
        document = cast(dict[str, object], json.loads(output))
        rows = cast(list[dict[str, object]], document["accounts"])
        selected = next(row for row in rows if row["label"] == "claude2")
        self.assertEqual(selected["roles"], ["units", "showrunners"])
        self.assertIn("accounts: workers is on codex1, which is not set up on this machine; run accounts.py setup codex1",
                      cast(list[str], document["problems"]))
        self.assertEqual(self.files(self.home), before)

    def test_json_status_fetches_the_role_map_once_like_the_table(self) -> None:
        self.seed(self.server_home, groups={"units": "claude2", "showrunners": "claude2"}, workers="codex1")
        for flags in ((), ("--json",)):
            with (self.subTest(flags=flags),
                  patch.object(role_map, "ssh_role_map", side_effect=self.remote) as ssh,
                  patch.object(agent_accounts, "claude_live", side_effect=AssertionError("no signed-in Claude")),
                  patch.object(accounts, "_codex_live", side_effect=AssertionError("no signed-in Codex"))):
                code, _, error = self.cli("status", *flags)
            self.assertEqual((code, error), (0, ""))
            ssh.assert_called_once_with(("role-map-read",))

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
        program = self.home / ".local/bin/claude"
        program.parent.mkdir(parents=True)
        _ = program.write_text("#!/bin/sh\nexit 99\n")
        program.chmod(0o700)
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
        _ = prompt.write_text("Scratch worker task")
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
