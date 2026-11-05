"""Test Hana discovery and cleanup without reading or signalling live processes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from collections.abc import Callable
from dataclasses import replace
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.hana_instances import hana_instances
from scripts.lint.memory_test_support import config_environment, write_config_fixture


RUN_FIXTURE_SHELL = subprocess.run
MAC_STARTED = "Sat Oct 31 10:00:00 2026"
# These shell functions replace every process read, signal and sleep in the remote
# stop program. The program itself runs unchanged, without ssh or live signals.
REMOTE_PROCESS = r'''
ps() {
    identity=$(cat "$REMOTE_STATE")
    [ "$identity" = gone ] || printf '201 %s\n' "$identity"
}
kill() {
    printf '%s\n' "$*" >> "$REMOTE_SIGNALS"
    [ "$1" != "$REMOTE_FAIL" ] || return 1
    [ "$1" != -KILL ] || printf 'gone' > "$REMOTE_STATE"
    return 0
}
sleep() {
    printf '%s\n' "$1" >> "$REMOTE_SLEEPS"
    [ "$REMOTE_REUSE" != yes ] || printf 'Sat Oct 31 11:00:00 2026' > "$REMOTE_STATE"
}
'''


class HanaInstancesTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    config: Path = Path()
    mac_config: Path = Path()
    signals: list[tuple[int, int]] = []
    sleeps: list[float] = []
    ssh_calls: list[list[str]] = []
    mac_output: str = ""
    mac_status: int = 0
    mac_environments: dict[int, dict[str, str]] = {}
    remote_fail: str = ""
    remote_reuse: bool = False
    remote_shell: str = "bash"
    mac_environment_failure: bool = False

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = (self.proc / "stat").write_text("btime 1000\n")
        self.signals = []
        self.sleeps = []
        self.ssh_calls = []
        self.mac_output = ""
        self.mac_status = 0
        self.mac_environments = {}
        self.remote_fail = ""
        self.remote_reuse = False
        self.remote_shell = "bash"
        self.mac_environment_failure = False
        _ = (self.root / "remote-identity").write_text(MAC_STARTED)
        self.config = self.root / "config/hana_instances.conf"
        _ = write_config_fixture(self.config,
            "exempt_ports=31000,31001\ntest_max_hours=2.5\n"
            + "unlabelled_max_minutes=7\nstop_grace_seconds=0.125\n"
        )
        self.mac_config = self.config.parent / "mac_test.conf"
        _ = write_config_fixture(self.mac_config, "host=private-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(self.config.parent))))
        _ = self.enterContext(mock.patch.object(os, "kill", side_effect=AssertionError("real process signal")))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("real ssh")))

    def test_config_folder_selects_limits_and_exempt_ports_at_use(self) -> None:
        other = self.root / "other-config"
        _ = write_config_fixture(other / "hana_instances.conf", "exempt_ports=32000\ntest_max_hours=3.75\n")
        with mock.patch.dict(os.environ, config_environment(other)):
            config = hana_instances.read_config()
        self.assertEqual(config.exempt_ports, frozenset({32000}))
        self.assertEqual(config.test_max_seconds, 3.75 * 3600)
        self.assertEqual(hana_instances.read_config().exempt_ports, frozenset({31000, 31001}))

    def process(self, pid: int, environment: dict[str, str], *, age: float = 600.0, executable: str = "target/debug/hana") -> Path:
        directory = self.proc / str(pid)
        directory.mkdir()
        checkout = self.root / f"checkout-{pid}"
        checkout.mkdir()
        binary = checkout / executable
        binary.parent.mkdir(parents=True, exist_ok=True)
        _ = binary.write_text("never execute")
        (directory / "exe").symlink_to(binary)
        (directory / "cwd").symlink_to(checkout)
        _ = (directory / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in environment.items()) + b"\0")
        # Linux stat field 22 is starttime; all process data is private.
        ticks = int((200_000 - age) * os.sysconf("SC_CLK_TCK"))
        fields = ["S", *(["0"] * 18), str(ticks), "0"]
        _ = (directory / "stat").write_text(f"{pid} (hana) " + " ".join(fields))
        _ = (self.proc / "uptime").write_text("200000.0 0.0\n")
        return checkout


    def environment(self, pid: int) -> dict[str, str]:
        fields = (self.proc / str(pid) / "environ").read_bytes().split(b"\0")
        return dict(field.decode().split("=", 1) for field in fields if b"=" in field)

    def local_rows(self) -> list[hana_instances.HanaProcess]:
        return hana_instances.local_processes(self.proc, now=201_000, environment_reader=self.environment)

    def ssh(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.ssh_calls.append(command)
        remote = command[-1]
        if self.mac_status:
            return subprocess.CompletedProcess(command, self.mac_status, "", "unreachable")
        if "darwin_process_environment" in remote:
            if self.mac_environment_failure:
                raise ValueError("private ps environment permission denied")
            for pid, environment in self.mac_environments.items():
                if f"darwin_process_environment({pid})" in remote:
                    return subprocess.CompletedProcess(command, 0, json.dumps(environment), "")
            raise AssertionError(f"unexpected environment read: {remote}")
        if "kill -TERM" in remote:
            return RUN_FIXTURE_SHELL(
                [self.remote_shell, "-f", "-c", REMOTE_PROCESS + remote],
                env={**os.environ, "REMOTE_STATE": str(self.root / "remote-identity"),
                     "REMOTE_SIGNALS": str(self.root / "remote-signals"),
                     "REMOTE_SLEEPS": str(self.root / "remote-sleeps"),
                     "REMOTE_FAIL": self.remote_fail,
                     "REMOTE_REUSE": "yes" if self.remote_reuse else "no"},
                capture_output=True, text=True, check=False, timeout=10,
            )
        return subprocess.CompletedProcess(command, 0, self.mac_output, "")

    def send_signal(self, pid: int, number: int) -> None:
        self.signals.append((pid, number))
        if number == signal.SIGKILL:
            (self.proc / str(pid) / "stat").unlink()

    def identity(self, pid: int) -> hana_instances.ProcessIdentity | hana_instances.ProcessGone:
        return hana_instances.local_identity(pid, self.proc)

    def mac_row(self) -> hana_instances.HanaProcess:
        self.mac_output = f"201 {MAC_STARTED} 00:45 /Users/test/work/target/debug/hana\n"
        self.mac_environments = {201: {"BRP_EXTRAS_PORT": "31003"}}
        return hana_instances.mac_processes(self.mac_output, self.mac_environments.__getitem__)[0]

    def invoke(
        self, *argv: str, stop_call: Callable[[hana_instances.HanaProcess, hana_instances.HanaInstancesConfig], None] | None = None,
    ) -> tuple[int, str, str]:
        collection = hana_instances.collect_processes(self.local_rows, self.ssh)
        stop = hana_instances.stop_process

        def stop_fixture(row: hana_instances.HanaProcess, config: hana_instances.HanaInstancesConfig) -> None:
            stop(row, config, signal_call=self.send_signal, sleep_call=self.sleeps.append, ssh_runner=self.ssh, identity_reader=self.identity)

        output, errors = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(output), redirect_stderr(errors),
            mock.patch.object(hana_instances, "collect_processes", return_value=collection),
            mock.patch.object(hana_instances, "stop_process", side_effect=stop_fixture if stop_call is None else stop_call),
        ):
            code = hana_instances.main(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def test_linux_rows_read_identity_labels_age_and_checkout_from_private_proc(self) -> None:
        checkout = self.process(101, {
            "BRP_EXTRAS_PORT": "31002", "HANA_TEST_OWNER": "window-unit",
            "HANA_TEST_PURPOSE": "CI icon spacing",
        }, age=901)
        _ = self.process(102, {}, executable="target/release/other-app")
        _ = self.process(103, {}, executable="bin/hana")
        rows = self.local_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.machine, row.pid, row.port), ("natedev", 101, 31002))
        self.assertEqual((row.owner, row.purpose), ("window-unit", "CI icon spacing"))
        self.assertEqual(row.age_seconds, 901)
        self.assertEqual(row.checkout, checkout)
        self.assertEqual(row.start_identity, str(int((200_000 - 901) * os.sysconf("SC_CLK_TCK"))))
        code, output, errors = self.invoke("list")
        self.assertEqual(code, 0, errors)
        for field in ("natedev:101", "31002", "window-unit", "CI icon spacing", str(checkout)):
            self.assertIn(field, output)
        self.assertNotIn("natedev:102", output)

    def test_mac_ps_rows_keep_multiword_purpose_and_elapsed_days(self) -> None:
        self.mac_output = (
            f" 201 {MAC_STARTED} 1-02:03:04 /Users/test/work/target/release/hana --flag FAKE_KEY=argv\n"
            + f" 202 {MAC_STARTED} 00:45 /Users/test/other/target/debug/hana\n"
            + f" 203 {MAC_STARTED} 00:45 /usr/bin/hana\n"
            + f" 204 {MAC_STARTED} 00:45 /Users/test/work/target/debug/not-hana\n"
            + f" 205 {MAC_STARTED} 00:45 /bin/sh -c /Users/test/work/target/debug/hana\n"
        )
        self.mac_environments = {
            201: {"BRP_EXTRAS_PORT": "31003", "HANA_TEST_OWNER": "mac-unit",
                  "HANA_TEST_PURPOSE": "panel label spacing"},
            202: {"BRP_EXTRAS_PORT": "31004"},
        }
        rows, problems = hana_instances.collect_processes(self.local_rows, self.ssh)
        self.assertEqual(problems, [])
        self.assertEqual([row.pid for row in rows], [201, 202])
        self.assertEqual((rows[0].machine, rows[0].port, rows[0].owner, rows[0].purpose),
                         ("mac", 31003, "mac-unit", "panel label spacing"))
        self.assertEqual(rows[0].age_seconds, 93_784)
        self.assertEqual(rows[0].checkout, Path("/Users/test/work"))
        self.assertEqual(rows[1].age_seconds, 45)
        self.assertIn("private-mac", self.ssh_calls[0])
        self.assertIn("ConnectTimeout=2", self.ssh_calls[0])
        self.assertIn("ps eww -axww", self.ssh_calls[0][-1])
        code, output, errors = self.invoke("list")
        self.assertEqual(code, 0, errors)
        self.assertIn("mac:201", output)
        self.assertIn("panel label spacing", output)
        self.assertIn("mac:202 port 31004 owner unlabelled purpose unlabelled", output)

    def test_assignment_like_arguments_do_not_become_mac_environment(self) -> None:
        from scripts.herdr import herdr

        arguments = "/Users/test/work/target/debug/hana FOO=bar HANA_TEST_PURPOSE=forged"
        command = f"201 {MAC_STARTED} 00:45 {arguments}\n"
        reads: list[list[str]] = []

        def ps(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertGreater(timeout, 0)
            reads.append(argv)
            suffix = " BRP_EXTRAS_PORT=31000" if "eww" in argv else ""
            return subprocess.CompletedProcess(argv, 0, arguments + suffix + "\n", "")

        with mock.patch.object(herdr, "run_program", side_effect=ps):
            rows = hana_instances.mac_processes(command, hana_instances.darwin_environment)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].environment, {"BRP_EXTRAS_PORT": "31000"})
        self.assertEqual(rows[0].purpose, "")
        self.assertFalse(hana_instances.is_test_hana(rows[0], hana_instances.read_config()))
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(argv[-1] == "201" for argv in reads))

    def test_unreadable_mac_environment_stays_visible_and_cannot_be_stopped(self) -> None:
        row = self.mac_row()
        self.mac_environment_failure = True
        rows, problems = hana_instances.collect_processes(self.local_rows, self.ssh)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].pid, rows[0].start_identity, rows[0].checkout),
                         (row.pid, row.start_identity, row.checkout))
        self.assertEqual(len(problems), 1)
        self.assertIn("mac:201", problems[0])
        self.assertIn("private ps environment permission denied", problems[0])
        code, output, errors = self.invoke("list")
        self.assertEqual(code, 0, errors)
        self.assertIn("mac:201 port unknown owner unknown purpose unknown", output)
        self.assertIn("private ps environment permission denied", output)
        code, output, errors = self.invoke("stop", "mac:201")
        self.assertNotEqual(code, 0)
        self.assertNotIn("already gone", output)
        self.assertNotIn("stopped", output)
        self.assertIn("environment unknown", errors)
        self.assertIn("private ps environment permission denied", errors)
        self.assertFalse((self.root / "remote-signals").exists())
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"}, age=421)
        code, output, errors = self.invoke("sweep")
        self.assertNotEqual(code, 0)
        self.assertIn("natedev:101: stopped", output)
        self.assertNotIn("mac:201: stopped", output)
        self.assertEqual(errors, "")
        self.assertFalse((self.root / "remote-signals").exists())

    def test_checkout_cleanup_scopes_unreadable_environments_to_its_processes(self) -> None:
        from scripts.lint import folder_audit, tmp_sweep

        checkout = self.root / "scratch" / "checkout"
        checkout.mkdir(parents=True)
        self.mac_environment_failure = True
        for case, process_checkout, inventory_index in (
            ("unrelated", self.root / "user-hana", 0),
            ("matching first inventory", checkout, 0),
            ("matching final inventory", checkout, 1),
            ("nested checkout", checkout / "nested", 0),
            ("global inventory failure", self.root / "user-hana", 0),
        ):
            with self.subTest(case=case):
                self.mac_output = f"201 {MAC_STARTED} 00:45 {process_checkout}/target/debug/hana\n"
                collection = hana_instances.collect_processes(self.local_rows, self.ssh)
                self.assertEqual(len(collection[0]), 1)
                self.assertEqual(len(collection[1]), 1)
                if case == "global inventory failure":
                    collection[1].append("mac: process table unavailable")
                inventories: list[tuple[list[hana_instances.HanaProcess], list[str]]] = (
                    [collection, collection] if inventory_index == 0 else [([], []), collection]
                )
                output, errors = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(output), redirect_stderr(errors),
                    mock.patch.object(hana_instances, "collect_processes", side_effect=inventories) as collect,
                    mock.patch.object(hana_instances, "stop_process", side_effect=AssertionError("unreadable process signalled")) as stop,
                    mock.patch.object(folder_audit, "process_uses", return_value=tmp_sweep.ProcessUsesKnown([])) as uses,
                ):
                    code = hana_instances.main(["stop-checkout", str(checkout)])
                stop.assert_not_called()
                if case == "unrelated":
                    self.assertEqual((code, output.getvalue(), errors.getvalue()),
                                     (0, "hana-instances: checkout clear\n", ""))
                    self.assertEqual(collect.call_count, 2)
                    uses.assert_called_once_with([checkout.resolve()])
                else:
                    self.assertNotEqual(code, 0)
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("process table unavailable" if case == "global inventory failure"
                                  else "mac:201: environment unknown", errors.getvalue())
                    uses.assert_not_called()

    def test_each_configured_exempt_port_requires_labels_or_agent_environment(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"})
        base = self.local_rows()[0]
        config = hana_instances.read_config()
        for port in (31000, 31001):
            user = replace(base, port=port, environment={"BRP_EXTRAS_PORT": str(port)})
            with self.subTest(port=port, case="user"):
                self.assertFalse(hana_instances.is_test_hana(user, config))
                self.assertFalse(hana_instances.over_limit(replace(user, age_seconds=1_000_000), config))
            for key in ("CLAUDECODE", "SSH_CONNECTION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                with self.subTest(port=port, agent=key):
                    self.assertTrue(hana_instances.is_test_hana(replace(user, environment={**user.environment, key: "agent"}), config))
            with self.subTest(port=port, agent="SHOWRUNNER_UNIT_ID"):
                self.assertFalse(hana_instances.is_test_hana(replace(user, environment={"SHOWRUNNER_UNIT_ID": "agent"}), config))
            self.assertTrue(hana_instances.is_test_hana(replace(user, owner="window-unit"), config))
            self.assertTrue(hana_instances.is_test_hana(replace(user, purpose="CI icon"), config))
        unknown = replace(base, port=0, environment={})
        self.assertFalse(hana_instances.is_test_hana(unknown, config))
        self.assertFalse(hana_instances.over_limit(replace(unknown, age_seconds=1_000_000), config))
        self.assertTrue(hana_instances.is_test_hana(replace(unknown, purpose="CI icon"), config))
        owner_only = replace(base, owner="window-unit", age_seconds=421)
        self.assertFalse(hana_instances.over_limit(owner_only, config))
        self.assertTrue(hana_instances.over_limit(replace(owner_only, age_seconds=9001), config))
        # The usual user port is deliberately not exempt in this private config.
        self.assertTrue(hana_instances.is_test_hana(replace(base, port=15702), config))

    def test_sweep_uses_runtime_limits_and_only_stops_overdue_test_hanas(self) -> None:
        cases = (
            (101, "CI icon", 9001, 31002), (102, "CI icon", 9000, 31002),
            (103, "", 421, 31003), (104, "", 420, 31003),
            (105, "", 100_000, 31000),
        )
        for pid, purpose, age, port in cases:
            _ = self.process(pid, {"BRP_EXTRAS_PORT": str(port), "HANA_TEST_PURPOSE": purpose}, age=age)
        original_stats = {pid: (self.proc / str(pid) / "stat").read_text() for pid in (101, 103)}
        code, output, errors = self.invoke("sweep")
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.splitlines(), ["natedev:101: stopped", "natedev:103: stopped"])
        self.assertEqual(self.signals, [
            (101, signal.SIGTERM), (101, signal.SIGKILL),
            (103, signal.SIGTERM), (103, signal.SIGKILL),
        ])
        self.assertEqual(self.sleeps, [0.125, 0.125])
        # Put the same processes back so the next result depends on the new
        # limits, rather than on the first sweep having removed their records.
        for pid, contents in original_stats.items():
            _ = (self.proc / str(pid) / "stat").write_text(contents)
        # A config edit takes effect in this process, without a build or reload.
        _ = write_config_fixture(self.config,
            "exempt_ports=31000,31001\ntest_max_hours=10\n"
            + "unlabelled_max_minutes=15\nstop_grace_seconds=0.25\n"
        )
        self.signals.clear()
        self.sleeps.clear()
        code, output, errors = self.invoke("sweep")
        self.assertEqual((code, output, errors), (0, "", ""))
        self.assertEqual(self.signals, [])
        self.assertEqual(self.sleeps, [])

    def test_sweep_dry_run_names_both_machines_without_signals(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"}, age=421)
        _ = self.mac_row()
        self.mac_output = self.mac_output.replace("00:45", "03:00:00")
        self.mac_environments[201]["HANA_TEST_PURPOSE"] = "CI icon"
        code, output, errors = self.invoke("sweep", "--dry-run")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("would stop natedev:101", output)
        self.assertIn("would stop mac:201", output)
        self.assertEqual(self.signals, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.ssh_calls), 2)

    def test_stop_by_port_and_missing_pid_are_successful_and_user_hana_is_protected(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"})
        _ = self.process(102, {"BRP_EXTRAS_PORT": "31000"})
        code, output, errors = self.invoke("stop", "natedev:31002")
        self.assertEqual((code, output, errors), (0, "natedev:101: stopped\n", ""))
        self.signals.clear()
        code, output, errors = self.invoke("stop", "natedev:99999")
        self.assertEqual((code, output, errors), (0, "natedev:99999: already gone\n", ""))
        self.assertEqual(self.signals, [])
        code, output, errors = self.invoke("stop", "natedev:102")
        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("user Hana is exempt", errors)
        self.assertEqual(self.signals, [])

    def test_a_process_that_exits_after_term_gets_no_kill(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"})
        row = self.local_rows()[0]

        def exits_after_term(pid: int, number: int) -> None:
            self.send_signal(pid, number)
            (self.proc / str(pid) / "stat").unlink()

        output = io.StringIO()
        with redirect_stdout(output):
            hana_instances.stop_process(row, hana_instances.read_config(), signal_call=exits_after_term,
                               sleep_call=self.sleeps.append, ssh_runner=self.ssh, identity_reader=self.identity)
        self.assertEqual(self.signals, [(101, signal.SIGTERM)])
        self.assertEqual(output.getvalue(), "natedev:101: stopped\n")

    def test_mac_stop_uses_configured_host_and_grace_without_a_local_signal(self) -> None:
        _ = self.mac_row()
        code, output, errors = self.invoke("stop", "mac:201")
        self.assertEqual((code, output, errors), (0, "mac:201: stopped\n", ""))
        self.assertEqual(self.signals, [])
        self.assertEqual(len(self.ssh_calls), 3)
        command = self.ssh_calls[-1]
        self.assertIn("private-mac", command)
        self.assertIn("kill -TERM 201", command[-1])
        self.assertIn("sleep 0.125", command[-1])
        self.assertIn("kill -KILL 201", command[-1])
        self.assertEqual((self.root / "remote-signals").read_text().splitlines(), ["-TERM 201", "-KILL 201"])
        self.assertEqual((self.root / "remote-sleeps").read_text(), "0.125\n")

    def test_remote_stop_runs_under_zsh_and_reaches_term(self) -> None:
        shell = shutil.which("zsh")
        if shell is None:
            self.skipTest("zsh is not installed; remote default-shell compatibility needs zsh")
        self.remote_shell = shell
        _ = self.mac_row()
        code, output, errors = self.invoke("stop", "mac:201")
        self.assertEqual((code, output, errors), (0, "mac:201: stopped\n", ""))
        self.assertEqual((self.root / "remote-signals").read_text().splitlines(),
                         ["-TERM 201", "-KILL 201"])
        self.assertEqual(self.signals, [])

    def test_failed_remote_term_or_kill_never_reports_stopped(self) -> None:
        _ = self.mac_row()
        for failed_signal, expected in (
            ("-TERM", ["-TERM 201"]),
            ("-KILL", ["-TERM 201", "-KILL 201"]),
        ):
            with self.subTest(signal=failed_signal):
                (self.root / "remote-signals").unlink(missing_ok=True)
                self.remote_fail = failed_signal
                code, output, errors = self.invoke("stop", "mac:201")
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn("mac:201: stop could not be confirmed", errors)
                self.assertEqual((self.root / "remote-signals").read_text().splitlines(), expected)

    def test_reused_linux_pid_is_not_signalled_before_term_or_after_grace(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"})
        row = self.local_rows()[0]
        stat = self.proc / "101" / "stat"
        original = stat.read_text()
        replacement = original.replace(f" {row.start_identity} 0", f" {int(row.start_identity) + 1} 0")

        def reuse_during_grace(seconds: float) -> None:
            self.sleeps.append(seconds)
            _ = stat.write_text(replacement)

        for before_term in (True, False):
            with self.subTest(before_term=before_term):
                _ = stat.write_text(replacement if before_term else original)
                self.signals.clear()
                self.sleeps.clear()
                output = io.StringIO()
                with redirect_stdout(output):
                    hana_instances.stop_process(row, hana_instances.read_config(),
                                       signal_call=self.send_signal, identity_reader=self.identity,
                                       sleep_call=reuse_during_grace, ssh_runner=self.ssh)
                self.assertEqual(self.signals, [] if before_term else [(101, signal.SIGTERM)])
                self.assertEqual(self.sleeps, [] if before_term else [0.125])
                self.assertEqual(output.getvalue(), "natedev:101: stopped\n")

    def test_remote_success_status_without_exit_confirmation_is_a_failure(self) -> None:
        row = self.mac_row()
        output = io.StringIO()

        def masked_status(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "", "")

        with redirect_stdout(output), self.assertRaisesRegex(ValueError, "stop could not be confirmed"):
            hana_instances.stop_process(row, hana_instances.read_config(), ssh_runner=masked_status)
        self.assertEqual(output.getvalue(), "")

    def test_reused_remote_pid_is_not_signalled_before_term_or_after_grace(self) -> None:
        _ = self.mac_row()
        for before_term in (True, False):
            with self.subTest(before_term=before_term):
                (self.root / "remote-signals").unlink(missing_ok=True)
                _ = (self.root / "remote-identity").write_text(
                    "Sat Oct 31 11:00:00 2026" if before_term else MAC_STARTED
                )
                self.remote_reuse = not before_term
                code, output, errors = self.invoke("stop", "mac:201")
                self.assertEqual((code, output, errors), (0, "mac:201: stopped\n", ""))
                signals = self.root / "remote-signals"
                self.assertEqual(signals.read_text().splitlines() if signals.exists() else [],
                                 [] if before_term else ["-TERM 201"])

    def test_macos_collection_uses_local_ps_and_never_proc_or_ssh(self) -> None:
        row = self.mac_row()
        local_commands: list[list[str]] = []

        def ps(command: list[str]) -> subprocess.CompletedProcess[str]:
            local_commands.append(command)
            return subprocess.CompletedProcess(command, 0, self.mac_output, "")

        with mock.patch.object(sys, "platform", "darwin"):
            rows, problems = hana_instances.collect_processes(
                local_source=mock.Mock(side_effect=AssertionError("Darwin read /proc")),
                ssh_runner=mock.Mock(side_effect=AssertionError("Darwin used ssh")),
                local_runner=ps, environment_reader=self.mac_environments.__getitem__,
            )
        self.assertEqual(problems, [])
        self.assertEqual(rows, [row])
        self.assertEqual(len(local_commands), 1)
        self.assertIn("ps ", local_commands[0][-1])
        self.assertIn("lstart=", local_commands[0][-1])

    def test_sweep_reports_each_failed_stop_and_still_stops_later_processes(self) -> None:
        for pid in (101, 102, 103):
            _ = self.process(pid, {"BRP_EXTRAS_PORT": "31002"}, age=421)
        attempted: list[int] = []

        def stop(row: hana_instances.HanaProcess, config: hana_instances.HanaInstancesConfig) -> None:
            attempted.append(row.pid)
            self.assertEqual(config.stop_grace_seconds, 0.125)
            if row.pid in (101, 102):
                raise PermissionError(f"{row.machine}:{row.pid}: permission denied")
            print(f"{row.machine}:{row.pid}: stopped")

        code, output, errors = self.invoke("sweep", stop_call=stop)
        self.assertNotEqual(code, 0)
        self.assertEqual(attempted, [101, 102, 103])
        self.assertEqual(output, "natedev:103: stopped\n")
        self.assertEqual(len(errors.splitlines()), 2)
        self.assertIn("natedev:101: permission denied", errors)
        self.assertIn("natedev:102: permission denied", errors)

    def test_a_finished_sweep_repeated_has_no_more_stops(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"}, age=421)
        code, output, errors = self.invoke("sweep")
        self.assertEqual((code, output, errors), (0, "natedev:101: stopped\n", ""))
        first_signals = list(self.signals)
        self.assertIn((101, signal.SIGTERM), first_signals)
        code, output, errors = self.invoke("sweep")
        self.assertEqual((code, output, errors), (0, "", ""))
        self.assertEqual(self.signals, first_signals)

    def test_unreachable_mac_prints_one_problem_and_keeps_natedev_rows(self) -> None:
        _ = self.process(101, {"BRP_EXTRAS_PORT": "31002"})
        self.mac_status = 255
        code, output, errors = self.invoke("list")
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.splitlines().count("mac: unreachable"), 1)
        self.assertIn("natedev:101", output)
        self.assertEqual(len(output.splitlines()), 2)



if __name__ == "__main__":
    _ = unittest.main()
