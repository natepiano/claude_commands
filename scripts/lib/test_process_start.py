"""The shared process reader keeps identity, absence and read errors distinct."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from functools import partial
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest.mock import call, patch

from scripts.lib import process_start
from scripts.lib.process_start_test_support import write_stat


class ProcessStartTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp/claude")))
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def use_ps(self) -> None:
        real_stat = Path.stat

        def without_host_proc(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == process_start.PROC_ROOT:
                raise FileNotFoundError("host has no proc")
            return real_stat(path, follow_symlinks=follow_symlinks)

        _ = self.enterContext(patch.object(Path, "stat", autospec=True, side_effect=without_host_proc))

    def cli(self, command: str, pid: int | str, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            ExitStack() as stack,
            patch.object(sys, "argv", ["process_start.py", command, str(pid), *arguments]),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            if command == "owner":
                _ = stack.enter_context(patch.object(
                    process_start, "check_owner", partial(process_start.check_owner, proc_root=self.proc),
                ))
            elif command == "leader":
                _ = stack.enter_context(patch.object(
                    process_start, "check_group_leader", partial(process_start.check_group_leader, proc_root=self.proc),
                ))
            else:
                _ = stack.enter_context(patch.object(
                    process_start, "process_start", partial(process_start.process_start, proc_root=self.proc),
                ))
                _ = stack.enter_context(patch.object(
                    process_start, "process_state", partial(process_start.process_state, proc_root=self.proc),
                ))
            try:
                status = process_start.main()
            except SystemExit as error:
                if not isinstance(error.code, int):
                    self.fail("command did not give an integer exit code")
                status = error.code
        return status, stdout.getvalue(), stderr.getvalue()

    def test_linux_ticks_come_from_the_given_proc_root(self) -> None:
        _ = write_stat(self.proc, 41, start_ticks=987654)
        self.assertEqual(process_start.process_start(41, self.proc), process_start.ProcessStartTime(987654))

    def test_zombie_and_dead_processes_cannot_own_a_start_identity(self) -> None:
        for state in ("Z", "X"):
            with self.subTest(state=state):
                _ = write_stat(self.proc, 41, state=state)
                self.assertEqual(process_start.process_start(41, self.proc), process_start.ProcessGone(41))

    def test_a_missing_stat_entry_is_gone(self) -> None:
        self.assertEqual(process_start.read_stat(41, self.proc), process_start.ProcessGone(41))
        self.assertEqual(process_start.process_start(41, self.proc), process_start.ProcessGone(41))
        self.assertIs(process_start.process_state(41, self.proc), process_start.ProcessRunState.GONE)

    def test_nonpositive_pids_are_gone_without_a_process_probe(self) -> None:
        with patch("scripts.lib.process_start.subprocess.run", side_effect=AssertionError("must not run ps")):
            for pid in (0, -1):
                with self.subTest(pid=pid):
                    self.assertEqual(process_start.read_stat(pid, self.root / "no-proc"),
                                     process_start.ProcessGone(pid))
                    self.assertEqual(process_start.process_start(pid, self.root / "no-proc"),
                                     process_start.ProcessGone(pid))
                    self.assertIs(process_start.process_state(pid, self.root / "no-proc"),
                                  process_start.ProcessRunState.GONE)

    def test_command_spaces_and_closing_parentheses_do_not_shift_stat_fields(self) -> None:
        _ = write_stat(self.proc, 41, command="a worker ) with (parentheses))", start_ticks=765)
        self.assertEqual(process_start.process_start(41, self.proc), process_start.ProcessStartTime(765))

    def test_ps_start_dates_use_a_fixed_locale_zone_and_timeout(self) -> None:
        self.use_ps()
        result = subprocess.CompletedProcess(["ps"], 0, "Thu Oct  29   12:34:56 2026\n", "")
        with patch("scripts.lib.process_start.subprocess.run", side_effect=[
            result, subprocess.CompletedProcess(["ps"], 0, "S", ""),
        ]) as run:
            self.assertEqual(process_start.process_start(41),
                             process_start.ProcessStartDate("Thu Oct  29   12:34:56 2026"))
        self.assertEqual(run.call_args_list, [
            call(["ps", "-o", "lstart=", "-p", "41"], capture_output=True, text=True, check=False,
                 timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}),
            call(["ps", "-o", "stat=", "-p", "41"], capture_output=True, text=True, check=False,
                 timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}),
        ])

    def test_ps_zombies_and_dead_processes_cannot_own_a_start_identity(self) -> None:
        self.use_ps()
        for state in ("Z", "X"):
            with self.subTest(state=state), patch("scripts.lib.process_start.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["ps"], 0, "Thu Oct 29 12:34:56 2026", ""),
                subprocess.CompletedProcess(["ps"], 0, state, ""),
            ]):
                self.assertEqual(process_start.process_start(41),
                                 process_start.ProcessGone(41))

    def test_ps_start_preserves_day_padding_for_stored_session_identity(self) -> None:
        self.use_ps()
        date = "Fri Oct  9 23:24:25 2026"
        with patch("scripts.lib.process_start.subprocess.run", side_effect=[
            subprocess.CompletedProcess(["ps"], 0, date + "\n", ""),
            subprocess.CompletedProcess(["ps"], 0, "S", ""),
        ]):
            start = process_start.process_start(41)
        self.assertEqual(start, process_start.parse_start(date))
        self.assertIsInstance(start, process_start.ProcessStartDate)
        if isinstance(start, process_start.ProcessGone):
            self.fail("fixture process was reported gone")
        self.assertEqual(start.text, date)
        self.assertEqual(process_start.started_at(start, self.root / "no-proc"),
                         datetime(2026, 10, 9, 23, 24, 25, tzinfo=timezone.utc))

    def test_ps_diagnostics_cannot_be_reported_as_a_gone_process(self) -> None:
        self.use_ps()
        for status in (0, 1, 2):
            with self.subTest(status=status), patch(
                "scripts.lib.process_start.subprocess.run",
                return_value=subprocess.CompletedProcess(["ps"], status, "", "process table denied"),
            ):
                with self.assertRaisesRegex(process_start.ProcessReadError, "process table denied"):
                    _ = process_start.process_start(41)
                with self.assertRaisesRegex(process_start.ProcessReadError, "process table denied"):
                    _ = process_start.process_state(41)

    def test_ps_with_no_selected_process_is_gone(self) -> None:
        self.use_ps()
        for status in (0, 1):
            with self.subTest(status=status), patch(
                "scripts.lib.process_start.subprocess.run",
                return_value=subprocess.CompletedProcess(["ps"], status, "", ""),
            ):
                self.assertEqual(process_start.process_start(41),
                                 process_start.ProcessGone(41))
                self.assertIs(process_start.process_state(41),
                              process_start.ProcessRunState.GONE)

    def test_failed_or_timed_out_ps_is_a_read_error(self) -> None:
        self.use_ps()
        for error in (FileNotFoundError("no ps"), subprocess.TimeoutExpired("ps", 5)):
            with self.subTest(error=error), patch("scripts.lib.process_start.subprocess.run", side_effect=error):
                with self.assertRaises(process_start.ProcessReadError):
                    _ = process_start.process_start(41)
                with self.assertRaises(process_start.ProcessReadError):
                    _ = process_start.process_state(41)
        with patch("scripts.lib.process_start.subprocess.run", return_value=
                          subprocess.CompletedProcess(["ps"], 2, "", "permission denied")):
            with self.assertRaisesRegex(process_start.ProcessReadError, "permission denied"):
                _ = process_start.process_start(41)

    def test_malformed_ps_output_is_a_read_error(self) -> None:
        self.use_ps()
        with patch("scripts.lib.process_start.subprocess.run", return_value=
                          subprocess.CompletedProcess(["ps"], 0, "not a start date", "")):
            with self.assertRaises(process_start.ProcessReadError):
                _ = process_start.process_start(41)

    def test_stored_start_text_round_trips_without_changing_kind(self) -> None:
        _ = write_stat(self.proc, 41, start_ticks=123456)
        tick_start = process_start.process_start(41, self.proc)
        self.assertIsInstance(tick_start, process_start.ProcessStartTime)
        if isinstance(tick_start, process_start.ProcessGone):
            self.fail("fixture process was reported gone")
        self.assertEqual(process_start.parse_start(tick_start.text), tick_start)
        self.use_ps()
        with patch("scripts.lib.process_start.subprocess.run", side_effect=[
            subprocess.CompletedProcess(["ps"], 0, "Thu Oct 29 12:34:56 2026", ""),
            subprocess.CompletedProcess(["ps"], 0, "S", ""),
        ]):
            date_start = process_start.process_start(41)
        self.assertIsInstance(date_start, process_start.ProcessStartDate)
        if isinstance(date_start, process_start.ProcessGone):
            self.fail("fixture ps selection was reported gone")
        self.assertEqual(process_start.parse_start(date_start.text), date_start)

    def test_empty_stored_start_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = process_start.parse_start("")

    def test_linux_state_letters_distinguish_stopped_and_gone(self) -> None:
        for letter in "RSDIUtWPNK":
            with self.subTest(letter=letter):
                _ = write_stat(self.proc, 41, state=letter)
                self.assertIs(process_start.process_state(41, self.proc), process_start.ProcessRunState.RUNNING)
        _ = write_stat(self.proc, 41, state="T")
        self.assertIs(process_start.process_state(41, self.proc), process_start.ProcessRunState.STOPPED)
        for letter in ("Z", "X"):
            with self.subTest(letter=letter):
                _ = write_stat(self.proc, 41, state=letter)
                self.assertIs(process_start.process_state(41, self.proc), process_start.ProcessRunState.GONE)

    def test_ps_state_uses_the_state_letter_before_modifiers(self) -> None:
        self.use_ps()
        for text, expected in (("S+", process_start.ProcessRunState.RUNNING),
                               ("T+", process_start.ProcessRunState.STOPPED),
                               ("Z", process_start.ProcessRunState.GONE),
                               ("X", process_start.ProcessRunState.GONE)):
            with self.subTest(text=text), patch("scripts.lib.process_start.subprocess.run", return_value=
                                                      subprocess.CompletedProcess(["ps"], 0, text, "")) as run:
                self.assertIs(process_start.process_state(41), expected)
                run.assert_called_once_with(
                    ["ps", "-o", "stat=", "-p", "41"], capture_output=True, text=True, check=False,
                    timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
                )

    def test_unknown_ps_state_is_a_read_error(self) -> None:
        self.use_ps()
        with patch("scripts.lib.process_start.subprocess.run", return_value=
                          subprocess.CompletedProcess(["ps"], 0, "?", "")):
            with self.assertRaises(process_start.ProcessReadError):
                _ = process_start.process_state(41)

    def test_stat_keeps_tree_cpu_terminal_and_memory_fields_for_a_zombie(self) -> None:
        _ = write_stat(self.proc, 41, state="Z", ppid=7, pgrp=8, session=9, tty_nr=34816,
                       tpgid=-1, utime=234, stime=56, start_ticks=789, rss=321)
        self.assertEqual(process_start.read_stat(41, self.proc), process_start.ProcessStat(
            state="Z", ppid=7, pgrp=8, session=9, tty_nr=34816, tpgid=-1,
            utime=234, stime=56, start=process_start.ProcessStartTime(789), rss=321,
        ))

    def test_zombie_leader_identity_remains_available_for_its_live_process_group(self) -> None:
        _ = write_stat(self.proc, 41, state="Z", pgrp=41, start_ticks=789)
        _ = write_stat(self.proc, 42, state="S", ppid=41, pgrp=41)
        leader = process_start.read_stat(41, self.proc)
        child = process_start.read_stat(42, self.proc)
        self.assertIsInstance(leader, process_start.ProcessStat)
        self.assertIsInstance(child, process_start.ProcessStat)
        if isinstance(leader, process_start.ProcessGone) or isinstance(child, process_start.ProcessGone):
            self.fail("the process-group fixture lost a stat record")
        self.assertEqual(leader.start, process_start.parse_start("789"))
        self.assertEqual((leader.pgrp, child.ppid, child.pgrp), (41, 41, 41))
        self.assertEqual(process_start.process_start(41, self.proc), process_start.ProcessGone(41))
        self.assertIs(process_start.process_state(42, self.proc), process_start.ProcessRunState.RUNNING)

    def test_stat_identity_survives_a_command_name_cut_inside_a_utf8_character(self) -> None:
        stat = write_stat(self.proc, 41, start_ticks=8675309)
        _ = stat.write_bytes(stat.read_bytes().replace(b"worker", b"worker\xc3"))
        identity = process_start.read_stat(41, self.proc)
        self.assertIsInstance(identity, process_start.ProcessStat)
        if isinstance(identity, process_start.ProcessGone):
            self.fail("fixture process was reported gone")
        self.assertEqual(identity.start, process_start.ProcessStartTime(8675309))
        self.assertEqual(process_start.process_start(41, self.proc), identity.start)
        self.assertIs(process_start.process_state(41, self.proc), process_start.ProcessRunState.RUNNING)

    def test_malformed_stat_is_a_read_error_and_never_process_absence(self) -> None:
        stat = write_stat(self.proc, 41)
        for text in ("41 missing command", "41 (worker) S 1 2", "41 (worker) ? " + "0 " * 30,
                     "41 (worker) S " + "bad " * 30):
            with self.subTest(text=text):
                _ = stat.write_text(text, encoding="utf-8")
                with self.assertRaises(process_start.ProcessReadError):
                    _ = process_start.read_stat(41, self.proc)
                with self.assertRaises(process_start.ProcessReadError):
                    _ = process_start.process_start(41, self.proc)

    def test_unreadable_stat_is_a_read_error(self) -> None:
        _ = write_stat(self.proc, 41)
        with patch.object(Path, "open", side_effect=PermissionError("stat denied")):
            with self.assertRaisesRegex(process_start.ProcessReadError, "stat denied"):
                _ = process_start.read_stat(41, self.proc)

    def test_undecodable_process_output_is_a_read_error(self) -> None:
        stat = write_stat(self.proc, 41)
        _ = stat.write_bytes(b"41 (worker) S \xff")
        with self.assertRaises(process_start.ProcessReadError):
            _ = process_start.process_start(41, self.proc)
        self.use_ps()
        with patch("scripts.lib.process_start.subprocess.run", side_effect=
                   UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid process output")):
            with self.assertRaises(process_start.ProcessReadError):
                _ = process_start.process_start(41)

    def test_tick_start_wall_time_uses_the_given_boot_time_and_clock_rate(self) -> None:
        _ = (self.proc / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n", encoding="utf-8")
        with patch("scripts.lib.process_start.os.sysconf", return_value=100):
            actual = process_start.started_at(process_start.ProcessStartTime(125), self.proc)
        self.assertEqual(actual, datetime(2023, 11, 14, 22, 13, 21, 250000, tzinfo=timezone.utc))

    def test_ps_start_wall_time_is_utc_without_a_proc_read(self) -> None:
        actual = process_start.started_at(process_start.ProcessStartDate("Thu Oct 29 12:34:56 2026"),
                                          self.root / "no-proc")
        self.assertEqual(actual, datetime(2026, 10, 29, 12, 34, 56, tzinfo=timezone.utc))

    def test_missing_or_malformed_boot_time_is_a_read_error(self) -> None:
        for text in ("cpu 1 2 3\n", "btime invalid\n", "btime\n"):
            with self.subTest(text=text):
                _ = (self.proc / "stat").write_text(text, encoding="utf-8")
                with self.assertRaises(process_start.ProcessReadError):
                    _ = process_start.started_at(process_start.ProcessStartTime(125), self.proc)

    def test_owner_check_distinguishes_the_recorded_process_from_a_reused_pid(self) -> None:
        for recorded, expected in ((765, process_start.OwnerCheck.ALIVE),
                                   (123, process_start.OwnerCheck.REPLACED)):
            with self.subTest(recorded=recorded):
                _ = write_stat(self.proc, 41, start_ticks=765)
                self.assertIs(process_start.check_owner(41, process_start.ProcessStartTime(recorded), self.proc),
                              expected)

    def test_owner_check_reports_missing_zombie_and_dead_owners_as_gone(self) -> None:
        recorded = process_start.ProcessStartTime(765)
        for pid in (0, -1, 41):
            with self.subTest(pid=pid):
                self.assertIs(process_start.check_owner(pid, recorded, self.proc), process_start.OwnerCheck.GONE)
        for state in ("Z", "X", "x"):
            with self.subTest(state=state):
                _ = write_stat(self.proc, 41, start_ticks=765, state=state)
                self.assertIs(process_start.check_owner(41, recorded, self.proc), process_start.OwnerCheck.GONE)

    def test_owner_checks_keep_the_record_when_stat_cannot_be_read(self) -> None:
        stat = write_stat(self.proc, 41)
        _ = stat.write_bytes(b"torn process stat")
        for check in (process_start.check_owner, process_start.check_group_leader):
            with self.subTest(check=check.__name__):
                self.assertIs(check(41, process_start.ProcessStartTime(123), self.proc),
                              process_start.OwnerCheck.UNKNOWN)
                with patch.object(Path, "open", side_effect=PermissionError("stat denied")):
                    self.assertIs(check(41, process_start.ProcessStartTime(123), self.proc),
                                  process_start.OwnerCheck.UNKNOWN)

    def test_owner_date_padding_does_not_change_the_recorded_identity(self) -> None:
        self.use_ps()
        recorded = process_start.parse_start("Fri Oct  9 23:24:25 2026")
        with patch("scripts.lib.process_start.subprocess.run", side_effect=[
            subprocess.CompletedProcess(["ps"], 0, "Fri Oct 9 23:24:25 2026\n", ""),
            subprocess.CompletedProcess(["ps"], 0, "S", ""),
        ]) as run:
            self.assertIs(process_start.check_owner(41, recorded), process_start.OwnerCheck.ALIVE)
        self.assertEqual(run.call_count, 2)

    def test_a_different_date_keeps_the_owner_without_a_legacy_zone_retry(self) -> None:
        self.use_ps()
        for live in ("Thu Oct 29 12:00:00 2026", "Thu Oct 29 16:00:00 2026"):
            with self.subTest(live=live), patch("scripts.lib.process_start.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["ps"], 0, live, ""),
                subprocess.CompletedProcess(["ps"], 0, "S", ""),
            ]) as run:
                self.assertIs(process_start.check_owner(
                    41, process_start.parse_start("Thu Oct 29 08:00:00 2026"),
                ), process_start.OwnerCheck.UNKNOWN)
                self.assertEqual(run.call_args_list, [
                    call(["ps", "-o", "lstart=", "-p", "41"], capture_output=True, text=True, check=False,
                         timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}),
                    call(["ps", "-o", "stat=", "-p", "41"], capture_output=True, text=True, check=False,
                         timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}),
                ])

    def test_owner_check_keeps_incompatible_tick_and_date_records(self) -> None:
        _ = write_stat(self.proc, 41, start_ticks=765)
        self.assertIs(process_start.check_owner(
            41, process_start.ProcessStartDate("Fri Oct  9 23:24:25 2026"), self.proc,
        ), process_start.OwnerCheck.UNKNOWN)
        self.use_ps()
        with patch("scripts.lib.process_start.subprocess.run", side_effect=[
            subprocess.CompletedProcess(["ps"], 0, "Fri Oct  9 23:24:25 2026", ""),
            subprocess.CompletedProcess(["ps"], 0, "S", ""),
        ]):
            self.assertIs(process_start.check_owner(41, process_start.ProcessStartTime(765)),
                          process_start.OwnerCheck.UNKNOWN)

    def test_an_unavailable_explicit_proc_root_never_uses_the_host_process_table(self) -> None:
        unavailable = self.root / "no-proc"
        for path in (unavailable, self.root / "not-a-directory"):
            if path.name == "not-a-directory":
                _ = path.write_text("not proc", encoding="utf-8")
            with self.subTest(path=path), patch("scripts.lib.process_start.subprocess.run") as run:
                for reader in (process_start.process_start, process_start.process_state, process_start.read_stat):
                    with self.subTest(reader=reader.__name__), self.assertRaises(process_start.ProcessReadError):
                        _ = reader(41, path)
                for check in (process_start.check_owner, process_start.check_group_leader):
                    with self.subTest(check=check.__name__):
                        self.assertIs(check(41, process_start.ProcessStartTime(765), path),
                                      process_start.OwnerCheck.UNKNOWN)
                run.assert_not_called()

    def test_an_unreadable_default_proc_root_keeps_the_owner_without_a_ps_fallback(self) -> None:
        with (
            patch.object(Path, "stat", side_effect=PermissionError("proc denied")),
            patch("scripts.lib.process_start.subprocess.run") as run,
        ):
            for reader in (process_start.process_start, process_start.process_state, process_start.read_stat):
                with self.subTest(reader=reader.__name__), self.assertRaisesRegex(
                    process_start.ProcessReadError, "proc denied",
                ):
                    _ = reader(41)
            for check in (process_start.check_owner, process_start.check_group_leader):
                with self.subTest(check=check.__name__):
                    self.assertIs(check(41, process_start.ProcessStartTime(765)), process_start.OwnerCheck.UNKNOWN)
            run.assert_not_called()

    def test_group_leader_keeps_zombie_identity_and_detects_a_reused_pid(self) -> None:
        _ = write_stat(self.proc, 41, state="Z", start_ticks=765)
        for recorded, expected in ((765, process_start.OwnerCheck.ALIVE),
                                   (123, process_start.OwnerCheck.REPLACED)):
            with self.subTest(recorded=recorded):
                self.assertIs(process_start.check_group_leader(
                    41, process_start.ProcessStartTime(recorded), self.proc,
                ), expected)
        self.assertIs(process_start.check_owner(41, process_start.ProcessStartTime(765), self.proc),
                      process_start.OwnerCheck.GONE)

    def test_group_leader_reports_missing_and_dead_entries_as_gone(self) -> None:
        recorded = process_start.ProcessStartTime(765)
        for pid in (0, -1, 41):
            with self.subTest(pid=pid):
                self.assertIs(process_start.check_group_leader(pid, recorded, self.proc), process_start.OwnerCheck.GONE)
        for state in ("X", "x"):
            with self.subTest(state=state):
                _ = write_stat(self.proc, 41, state=state)
                self.assertIs(process_start.check_group_leader(41, recorded, self.proc), process_start.OwnerCheck.GONE)

    def test_group_leader_ps_reads_zombie_identity_and_state_together(self) -> None:
        self.use_ps()
        date = "Fri Oct  9 23:24:25 2026"
        for state, expected in (("Z+", process_start.OwnerCheck.ALIVE),
                                ("S", process_start.OwnerCheck.ALIVE),
                                ("X", process_start.OwnerCheck.GONE)):
            with self.subTest(state=state), patch("scripts.lib.process_start.subprocess.run", return_value=
                    subprocess.CompletedProcess(["ps"], 0, date + " " + state + "\n", "")) as run:
                self.assertIs(process_start.check_group_leader(41, process_start.parse_start(date)), expected)
                run.assert_called_once_with(
                    ["ps", "-o", "lstart=,stat=", "-p", "41"], capture_output=True, text=True, check=False,
                    timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
                )

    def test_group_leader_dates_keep_uncertain_records_and_allow_padding(self) -> None:
        self.use_ps()
        for recorded, expected in ((process_start.ProcessStartDate("Fri Oct 9 23:24:25 2026"),
                                    process_start.OwnerCheck.ALIVE),
                                   (process_start.ProcessStartDate("Fri Oct 9 19:24:25 2026"),
                                    process_start.OwnerCheck.UNKNOWN),
                                   (process_start.ProcessStartTime(765), process_start.OwnerCheck.UNKNOWN)):
            with self.subTest(recorded=recorded), patch("scripts.lib.process_start.subprocess.run", return_value=
                    subprocess.CompletedProcess(["ps"], 0, "Fri Oct  9 23:24:25 2026 Z", "")):
                self.assertIs(process_start.check_group_leader(41, recorded), expected)

    def test_owner_checks_keep_the_record_on_ps_probe_failure(self) -> None:
        self.use_ps()
        for check in (process_start.check_owner, process_start.check_group_leader):
            for error in (FileNotFoundError("no ps"), subprocess.TimeoutExpired("ps", 5)):
                with self.subTest(check=check.__name__, error=error), patch(
                    "scripts.lib.process_start.subprocess.run", side_effect=error,
                ):
                    self.assertIs(check(41, process_start.parse_start("Fri Oct 9 23:24:25 2026")),
                                  process_start.OwnerCheck.UNKNOWN)
            with self.subTest(check=check.__name__), patch("scripts.lib.process_start.subprocess.run", return_value=
                    subprocess.CompletedProcess(["ps"], 1, "", "process table denied")):
                self.assertIs(check(41, process_start.parse_start("Fri Oct 9 23:24:25 2026")),
                              process_start.OwnerCheck.UNKNOWN)

    def test_owner_checks_report_an_empty_ps_selection_as_gone(self) -> None:
        self.use_ps()
        for check in (process_start.check_owner, process_start.check_group_leader):
            with self.subTest(check=check.__name__), patch("scripts.lib.process_start.subprocess.run", return_value=
                    subprocess.CompletedProcess(["ps"], 1, "", "")):
                self.assertIs(check(41, process_start.parse_start("Fri Oct 9 23:24:25 2026")),
                              process_start.OwnerCheck.GONE)

    def test_group_leader_keeps_the_record_when_ps_identity_or_state_is_malformed(self) -> None:
        self.use_ps()
        for text in ("not a date Z", "Fri Oct 9 23:24:25 2026", "Fri Oct 9 23:24:25 2026 ?"):
            with self.subTest(text=text), patch("scripts.lib.process_start.subprocess.run", return_value=
                    subprocess.CompletedProcess(["ps"], 0, text, "")):
                self.assertIs(process_start.check_group_leader(41, process_start.parse_start("Fri Oct 9 23:24:25 2026")),
                              process_start.OwnerCheck.UNKNOWN)

    def test_owner_and_leader_commands_print_all_outcomes_and_exit_zero(self) -> None:
        for command in ("owner", "leader"):
            with self.subTest(command=command):
                _ = write_stat(self.proc, 41, start_ticks=765)
                self.assertEqual(self.cli(command, 41, "765"), (0, "alive\n", ""))
                self.assertEqual(self.cli(command, 41, "123"), (0, "replaced\n", ""))
                self.assertEqual(self.cli(command, 42, "765"), (0, "gone\n", ""))
                _ = (self.proc / "41" / "stat").write_bytes(b"torn stat")
                self.assertEqual(self.cli(command, 41, "765"), (0, "unknown\n", ""))

    def test_owner_and_leader_commands_refuse_bad_arguments_without_stdout(self) -> None:
        for command in ("owner", "leader"):
            for pid, arguments in (("not-a-pid", ("765",)), (41, ("",)), (41, ("   ",)), (41, ())):
                with self.subTest(command=command, pid=pid, arguments=arguments):
                    status, stdout, stderr = self.cli(command, pid, *arguments)
                    self.assertEqual((status, stdout), (2, ""))
                    self.assertTrue(stderr, "bad arguments must have a diagnostic")

    def test_start_command_prints_identity_and_exits_zero(self) -> None:
        _ = write_stat(self.proc, 41, start_ticks=765)
        self.assertEqual(self.cli("start", 41), (0, "765\n", ""))

    def test_script_entry_point_uses_the_package_reader_and_its_identity_types(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", [process_start.__file__, "start", "0"]),
            patch.object(sys, "path", sys.path.copy()),
            patch.object(process_start, "process_start", return_value=process_start.ProcessStartTime(765)),
            redirect_stdout(stdout), redirect_stderr(stderr),
            self.assertRaises(SystemExit) as stopped,
        ):
            _ = runpy.run_path(process_start.__file__, run_name="__main__")
        self.assertEqual((stopped.exception.code, stdout.getvalue(), stderr.getvalue()), (0, "765\n", ""))

    def test_start_command_exits_one_without_output_for_a_gone_process(self) -> None:
        self.assertEqual(self.cli("start", 41), (1, "", ""))

    def test_start_command_exits_two_and_reports_a_read_error(self) -> None:
        stat = write_stat(self.proc, 41)
        _ = stat.write_text("torn stat", encoding="utf-8")
        status, stdout, stderr = self.cli("start", 41)
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("malformed process stat", stderr)

    def test_state_command_prints_each_state_and_exits_zero(self) -> None:
        for letter, expected in (("R", "running"), ("T", "stopped"), ("Z", "gone")):
            with self.subTest(letter=letter):
                _ = write_stat(self.proc, 41, state=letter)
                self.assertEqual(self.cli("state", 41), (0, expected + "\n", ""))


if __name__ == "__main__":
    _ = unittest.main()
