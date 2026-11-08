"""Repeat transitions, durable concurrency, and notices through fake delivery sinks."""
from __future__ import annotations

import io
import fcntl
import json
import time
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import redirect_stderr
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.message.process_test_support import OwnedTestProcesses
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS

from scripts.message import send
from scripts.message import repeat_guard as guard
from scripts.message import repeats
from scripts.message.repeat_guard_test_support import (
    NoticeAttemptTrace, NoticeDrainClock, install_guard_test_budget, install_notice_test_config, run_ready_notice_attempt,
    wait_for_notice_exit, wait_for_notice_readiness,
)


def setUpModule() -> None:
    install_guard_test_budget()


# A time bound is this many times its limit, so gate load cannot fail it.
DEADLINE_CEILING_FACTOR = 10

NOTICE_FIXTURE = r'''import sys,os,json,signal,io
from pathlib import Path
from unittest.mock import patch
from scripts.message import send, repeat_guard as g, roster
from scripts.message import send_result
from scripts.production import staff
staff.snapshot=lambda: staff.StaffSnapshot(())
role,mode,root,sink=sys.argv[1:]
if role=='supervise':
    limits=g.configuration()
    g.notice_job([sys.executable,__file__,'drain',mode,root,sink],limits.drain_timeout_seconds)
elif role=='drain':
    real_run=send.run
    def attempt(command,stdin,timeout):
        return real_run([sys.executable,__file__,'attempt',mode,root,sink],stdin,timeout)
    with patch.object(send,'run',attempt):
        g.drain(Path(root),send.attempt_repeat_notice,3)
else:
    request=json.loads(sys.stdin.read())
    source=request['state']['sender']
    kind=request['kind']
    def owner(sender):
        if mode=='owner-block' and source=='watchdog':
            os.write(int(os.environ['NOTICE_READY_FD']),b'owner-block\n')
            signal.pause()
        return 'codex:owner'
    def accept(message,*args,**kwargs):
        if source=='watchdog' and mode=='delivery-block':
            os.write(int(os.environ['NOTICE_READY_FD']),b'delivery-block\n')
            signal.pause()
        if mode in ('before','after'):
            if mode=='after':
                with Path(sink).open('a') as f: f.write(json.dumps({'kind':kind,'key':message.key,'source':source})+'\n')
            os.kill(os.getppid(),signal.SIGKILL)
            os._exit(9)
        with Path(sink).open('a') as f: f.write(json.dumps({'kind':kind,'key':message.key,'source':source})+'\n')
        if mode=='queued':
            send.enqueue(message,'fake durable queue',send.now())
            return send.Result('queued','fake durable queue')
        return send.Result('sent','fake sink')
    def transport(command,stdin,timeout):
        options=send.parse(command[2:])._replace(shown_sender=send.ShownSender(g.NOTICE_SENDER))
        with patch.object(roster,'roster',return_value=roster.Roster([],[])), patch.object(send,'resolve_recipient',return_value=send.CodexSessionRecipient('codex:owner','owner','owner',send.codex_daemon.CodexDaemon(Path(root),Path(root)/'unused.sock'))), patch.object(send,'codex_session',accept), patch.object(send,'user',accept), patch.object(send,'wrong_template',return_value=None):
            result=send.send(options)
        return send_result.EXIT[result.outcome],result.outcome.upper()+': '+result.detail,''
    sys.stdin=io.StringIO(json.dumps(request))
    with patch.object(send,'repeat_notice_owner',owner), patch.object(send,'run',transport):
        send.main(['repeat-notice-attempt'])
'''


class RepeatGuardTests(unittest.TestCase):
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    limits: guard.Limits  # pyright: ignore[reportUninitializedInstanceVariable]
    state: guard.SenderState  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.owned: OwnedTestProcesses = OwnedTestProcesses()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")}))
        config = install_notice_test_config(self.home)
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)}))
        limits = guard.configuration()
        if isinstance(limits, guard.GuardUnavailable):
            raise AssertionError(limits.reason)
        self.limits = limits
        self.state = guard.SenderState("watchdog", "fixture-host", 0)
        self.root = self.home / "state/message/repeats"
        self.owned = OwnedTestProcesses()
        self.addCleanup(self.owned.finish)

        def launch(command: list[str], *, stdin: int | None = None, stdout: int | None = None,
                   stderr: int | None = None, text: Literal[True] = True,
                   start_new_session: bool = False, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
            child = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                     text=text, start_new_session=start_new_session, env=self.owned.environment(env))
            self.owned.track(child, group=start_new_session)
            return child

        _ = self.enterContext(patch.object(guard, "Popen", launch))

    def attempt(self, text: str = "Morty: worker stopped", *, at: float = 1,
                receiver: str = "session:morty", protected: bool = False) -> guard.Attempt:
        return guard.Attempt("watchdog", receiver, text, "fixture-host", at, protected)

    def apply(self, text: str = "Morty: worker stopped", *, at: float = 1) -> guard.Decision:
        return guard.transition(self.state, self.attempt(text, at=at), self.limits)

    def persist(self, attempt: guard.Attempt) -> guard.Decision:
        # Storage and notice fixtures use the real worker without depending on
        # interpreter startup scheduling. Public deadline tests still use check.
        return guard._stored_check(self.root, attempt)  # pyright: ignore[reportPrivateUsage]

    def test_notice_config_needs_no_repository_or_git_recovery(self) -> None:
        config = self.home / ".claude/config"
        self.assertTrue((config / "send.conf").is_file())
        with patch.object(subprocess, "Popen", side_effect=AssertionError("configuration started a process")):
            self.assertGreater(send.delivery_timeout(), 0)
            self.assertGreater(send.kill_grace(), 0)

    def test_notice_readiness_patch_leaves_other_module_launchers_unchanged(self) -> None:
        launcher = subprocess.Popen
        cleanup_budget = send.kill_grace

        def read_cleanup_budget() -> float:
            self.assertIs(subprocess.Popen, launcher)
            return cleanup_budget()

        with patch.object(send, "kill_grace", read_cleanup_budget):
            code, output, errors = run_ready_notice_attempt(
                send.run, [sys.executable, "-c", "print('ready')"], "", TEST_HANG_GUARD_SECS, "")
        self.assertEqual((code, output, errors), (0, "ready\n", ""))

    def test_first_two_copies_pass_and_the_third_is_held_and_recorded(self) -> None:
        self.assertEqual([self.apply(at=index).held for index in range(1, 6)], [False, False, True, True, True])
        self.assertEqual(self.state.holds, [3, 4, 5])

    def test_slow_worker_start_keeps_repeat_decisions_under_the_shared_test_budget(self) -> None:
        launch = subprocess.Popen

        def slow_start(command: list[str], *, stdin: int, stdout: int, stderr: int,
                       text: Literal[True], start_new_session: bool) -> subprocess.Popen[str]:
            time.sleep(1)
            child = launch(command, stdin=stdin, stdout=stdout, stderr=stderr,
                           text=text, start_new_session=start_new_session, env=self.owned.environment())
            self.owned.track(child, group=start_new_session)
            return child

        errors = io.StringIO()
        with (patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")}, clear=True),
              patch.object(guard, "Popen", slow_start), redirect_stderr(errors)):
            held = [guard.check(self.root, self.attempt(at=index)).held for index in range(3)]
        self.assertEqual(held, [False, False, True])
        self.assertEqual(errors.getvalue(), "")

        program = """import json, sys, time
from pathlib import Path
from unittest.mock import patch
from scripts.message import send
guard = send.repeat_guard
launch = guard.Popen
def slow_start(*args, **kwargs):
    time.sleep(1)
    return launch(*args, **kwargs)
with patch.object(guard, 'Popen', slow_start):
    held = [guard.check(Path(sys.argv[1]), guard.Attempt(
        'watchdog', 'session:morty', 'stopped', 'fixture-host', at)).held for at in range(3)]
print(json.dumps(held))
"""
        child = subprocess.run([sys.executable, "-c", program, str(self.root / "child")],
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False,
            env=self.owned.environment())
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), [False, False, True])
        self.assertEqual(child.stderr, "")

    def test_isolated_ends_its_worker_on_interrupt_and_communication_error(self) -> None:
        for error in (KeyboardInterrupt(), OSError("fixture read failed")):
            with self.subTest(error=type(error).__name__):
                child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True, env=self.owned.environment())
                self.owned.track(child, group=True)
                exchange = child.communicate
                reaped = Event()
                interrupted = False

                def communicate(input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        raise error
                    try:
                        return exchange(input, timeout=timeout)
                    finally:
                        reaped.set()

                with (patch.object(guard, "Popen", return_value=child),
                      patch.object(child, "communicate", communicate)):
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            _ = guard.isolated(["fixture"], "", guard.GUARD_IO_CEILING_SECONDS)
                    else:
                        self.assertFalse(guard.isolated(["fixture"], "", guard.GUARD_IO_CEILING_SECONDS).held)
                    LauncherHangGuard("interrupted guard worker reaping").event(reaped)
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_child_test_budget_preserves_the_system_sitecustomize(self) -> None:
        site = self.home / "nix-site"
        site.mkdir()
        _ = (site / "guard_site_probe.py").write_text("ready = True\n")
        program = """import json, os
import guard_site_probe
from scripts.message import repeat_guard
print(json.dumps([guard_site_probe.ready, 'NIX_PYTHONPATH' in os.environ,
                  repeat_guard.GUARD_IO_CEILING_SECONDS]))
"""
        child = subprocess.run([sys.executable, "-c", program],
            env=self.owned.environment({**os.environ, "NIX_PYTHONPATH": str(site)}), capture_output=True, text=True,
            timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), [True, False, TEST_HANG_GUARD_SECS])
        self.assertEqual(child.stderr, "")

    def test_unknown_prose_compares_exactly_even_when_values_look_volatile(self) -> None:
        for values in (("10:00", "11:00", "12:00"),
                       ("2026-10-31T02:01:00Z", "2026-10-31T02:02:00Z", "2026-10-31T02:03:00Z"),
                       ("pid 1", "pid 2", "pid 3"),
                       ("/tmp/one.sock", "/tmp/two.sock", "/tmp/three.sock"),
                       ("msg_id 1", "msg_id 2", "msg_id 3"),
                       ("fe80::1:10", "fe80::1:11", "fe80::1:12"),
                       ("<time>", "<pid>", "<socket>")):
            texts = ["inspect " + value for value in values]
            self.assertEqual([self.apply(text).held for text in texts], [False, False, False])
            self.assertFalse(self.apply(texts[0]).held)
            self.assertTrue(self.apply(texts[0]).held)

    def test_distinct_names_and_meaningful_numbers_are_never_held(self) -> None:
        for index in range(self.limits.receiver_rate_count + 1):
            self.assertFalse(self.apply(f"worker-{index}: inspect issue #{index}", at=index).held)
        self.assertEqual([notice.kind for notice in self.state.notices], ["owner"])

    def test_restart_results_spread_over_seven_receivers_do_not_notify_or_hold(self) -> None:
        self.state = guard.SenderState("mesh-runs", "fixture-host", 0)
        index = 0
        for receiver, count in enumerate((50, 22, 15, 8, 3, 3, 1)):
            for _ in range(count):
                attempt = guard.Attempt("mesh-runs", f"session:{receiver}", f"run-{index} finished",
                                        "fixture-host", index * 22 * 60 / 101)
                self.assertFalse(guard.transition(self.state, attempt, self.limits).held)
                index += 1
        self.assertEqual(index, 102)
        self.assertEqual(self.state.notices, [])
        self.assertEqual(self.state.holds, [])

    def test_rate_threshold_is_not_combined_across_receivers(self) -> None:
        for index in range(self.limits.receiver_rate_count):
            self.assertFalse(guard.transition(self.state, self.attempt(
                f"result {index}", receiver=f"session:{index % 2}", at=index), self.limits).held)
        self.assertEqual(self.state.notices, [])
        self.assertEqual(self.state.holds, [])

    def test_one_receivers_distinct_flood_notifies_its_owner_once_without_phone_or_hold(self) -> None:
        target = "session:results"
        for index in range(self.limits.receiver_rate_count):
            self.assertFalse(self.persist(self.attempt(f"result {index}", receiver=target, at=index)).held)
        sink: list[send.Options] = []
        def run(command: list[str], _stdin: str, _timeout: float) -> tuple[int, str, str]:
            sink.append(send.parse(command[2:]))
            return 0, "SENT: fixture owner", ""
        with patch.object(send, "run", run), patch.object(send, "repeat_notice_owner", return_value="owner"):
            guard.drain(self.root, send.deliver_repeat_notice, self.limits.receiver_rate_count)
            self.assertFalse(self.persist(self.attempt("next distinct result", receiver=target,
                                                     at=self.limits.receiver_rate_count + 1)).held)
            guard.drain(self.root, send.deliver_repeat_notice, self.limits.receiver_rate_count + 2)
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0].to, "owner")
        self.assertIn(target, sink[0].text or "")
        self.assertIn("1 sessions", sink[0].text or "")
        self.assertIn(f"{self.limits.receiver_rate_count} copies", sink[0].text or "")
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual([notice.kind for notice in state.notices], ["owner"])
        self.assertEqual(state.holds, [])

    def test_receiver_rate_windows_and_episodes_are_independent_and_bounded(self) -> None:
        limits = replace(self.limits, receiver_rate_count=2)
        for target in ("session:a", "session:b"):
            for at in (0, 1):
                self.assertFalse(guard.transition(self.state, self.attempt(
                    f"{target} result {at}", receiver=target, at=at), limits).held)
        self.assertEqual([notice.receivers for notice in self.state.notices], [1, 1])
        self.assertEqual(len({notice.id for notice in self.state.notices}), 2)
        for at in (limits.window_seconds - 1, limits.window_seconds, limits.window_seconds + 1):
            self.assertFalse(guard.transition(self.state, self.attempt(
                f"ongoing {at}", receiver="session:a", at=at), limits).held)
        self.assertEqual(len(self.state.notices), 2)
        for at in (3 * limits.window_seconds, 3 * limits.window_seconds + 1):
            self.assertFalse(guard.transition(self.state, self.attempt(
                f"new episode {at}", receiver="session:a", at=at), limits).held)
        self.assertEqual(len(self.state.notices), 3)
        state = guard.SenderState("watchdog", "fixture-host", 0)
        for target in ("session:a", "session:b", "session:c"):
            _ = guard.transition(state, self.attempt(target, receiver=target), replace(limits, max_keys=2))
        self.assertEqual(set(state.rates), {"session:a", "session:b"})

    def test_receiver_counts_do_not_add_up_across_fixed_windows(self) -> None:
        limits = replace(self.limits, receiver_rate_count=3)
        for at in (0, 1, limits.window_seconds):
            self.assertFalse(guard.transition(self.state, self.attempt(f"result {at}", at=at), limits).held)
        self.assertEqual(self.state.notices, [])
        for at in (limits.window_seconds + 1, limits.window_seconds + 2):
            self.assertFalse(guard.transition(self.state, self.attempt(f"result {at}", at=at), limits).held)
        self.assertEqual([notice.kind for notice in self.state.notices], ["owner"])

    def test_receiver_rate_policy_is_read_at_use_and_the_old_key_is_rejected(self) -> None:
        config = self.limits_file(replace(self.limits, receiver_rate_count=2))
        with patch.dict(os.environ, {"SEND_REPEAT_CONFIG": str(config)}):
            self.assertFalse(self.persist(self.attempt("first result", at=0)).held)
            _ = self.limits_file(replace(self.limits, receiver_rate_count=3))
            self.assertFalse(self.persist(self.attempt("second result", at=1)).held)
            state = guard.decode(next(self.root.glob("*.json")).read_text())
            self.assertEqual(state.notices, [])
            self.assertFalse(self.persist(self.attempt("third result", at=2)).held)
            state = guard.decode(next(self.root.glob("*.json")).read_text())
            self.assertEqual([notice.kind for notice in state.notices], ["owner"])
            _ = config.write_text(config.read_text().replace("receiver_rate_count", "sender_rate_count"))
            with redirect_stderr(io.StringIO()):
                self.assertIsInstance(guard.configuration(), guard.GuardUnavailable)

    def test_legacy_sender_rate_state_migrates_silently_without_an_owner_notice(self) -> None:
        raw = asdict(self.state)
        del raw["rates"]
        raw.update(rate_first=0, rate_count=self.limits.receiver_rate_count)
        errors = io.StringIO()
        self.root.mkdir(parents=True)
        path = self.root / (guard.digest("watchdog") + ".json")
        _ = path.write_text(json.dumps(raw))
        def unexpected_notice(_state: guard.SenderState, _notice: guard.Notice,
                              _limits: guard.Limits) -> guard.NoticeOutcome:
            self.fail("legacy rate generated a notice")
        with redirect_stderr(errors):
            decoded = guard.decode(path.read_text())
            self.assertEqual(decoded.rates, {})
            self.assertFalse(self.persist(self.attempt("one new result", at=1)).held)
            guard.drain(self.root, unexpected_notice, 2)
        self.assertEqual(errors.getvalue(), "")
        migrated = guard.decode(path.read_text())
        self.assertEqual(migrated.notices, [])
        self.assertEqual(migrated.rates["session:morty"].count, 1)
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertNotIn("rate_first", saved)
        self.assertNotIn("rate_count", saved)

    def test_legacy_rate_outbox_is_retired_without_losing_exact_text_notices(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        state = guard.decode(path.read_text())
        exact = state.notices.copy()
        legacy = guard.Flood("Many distinct messages; latest: result", 0, 2, 102,
                             ["session:a", "session:b"])
        state.floods["rate"] = legacy
        episode = guard.digest(state.sender + "\0rate\0" + str(legacy.first))
        state.notices.append(guard.Notice("owner", legacy.text, 0, 2, 102, 2, episode))
        raw = asdict(state)
        del raw["rates"]
        raw.update(rate_first=0, rate_count=102)
        decoded = guard.decode(json.dumps(raw))
        self.assertEqual(decoded.notices, exact)
        self.assertNotIn("rate", decoded.floods)
        self.assertEqual(decoded.receivers, state.receivers)
        self.assertEqual(decoded.holds, state.holds)
        raw["holds"] = ["corrupt"]
        with self.assertRaises(ValueError):
            _ = guard.decode(json.dumps(raw))

    def test_an_expired_legacy_rate_flood_cannot_leave_a_pending_false_notice(self) -> None:
        raw = asdict(self.state)
        del raw["rates"]
        raw.update(rate_first=0, rate_count=102)
        episode = guard.digest(self.state.sender + "\0rate\0" + str(0))
        raw["notices"] = [asdict(guard.Notice("owner", "Many distinct messages", 0, 2, 102, 7, episode))]
        self.assertEqual(guard.decode(json.dumps(raw)).notices, [])

    def test_rate_counts_continue_when_literal_keys_are_full(self) -> None:
        limits = replace(self.limits, max_keys=2, receiver_rate_count=3)
        for index in range(4):
            self.assertFalse(guard.transition(self.state, self.attempt(f"result {index}", at=index), limits).held)
        self.assertEqual([notice.kind for notice in self.state.notices], ["owner"])
        self.assertEqual(self.state.notices[0].receivers, 1)
        self.assertEqual(self.state.rates["session:morty"].count, 4)
        self.assertEqual(len(self.state.receivers), 2)
        self.assertEqual(len(self.state.floods), 2)

    def test_person_messages_and_phone_questions_never_enter_guard_state(self) -> None:
        self.assertEqual([self.apply(at=index).held for index in range(3)], [False, False, True])
        before = asdict(self.state)
        for receiver in ("session:morty", "user"):
            for index in range(6):
                decision = guard.transition(self.state, self.attempt(at=index, receiver=receiver,
                    protected=receiver != "user"), self.limits)
                self.assertFalse(decision.held)
        self.assertEqual(asdict(self.state), before)

    def test_window_expiry_and_a_changed_message_resume_delivery(self) -> None:
        self.assertEqual([self.apply(at=index).held for index in range(3)], [False, False, True])
        self.assertFalse(self.apply("Morty: recovered", at=3).held)
        self.assertFalse(self.apply("Morty: recovered", at=4).held)
        self.assertFalse(self.apply(at=self.limits.window_seconds + 4).held)

    def test_interleaving_distinct_notices_does_not_reset_a_repeated_texts_count(self) -> None:
        self.assertFalse(self.apply("Morty stopped", at=0).held)
        self.assertFalse(self.apply("Berth stopped", at=1).held)
        self.assertFalse(self.apply("Morty stopped", at=2).held)
        self.assertFalse(self.apply("Berth recovered", at=3).held)
        self.assertTrue(self.apply("Morty stopped", at=4).held)

    def test_one_owner_notice_covers_all_receivers_of_a_flood(self) -> None:
        for receiver in ("session:morty", "session:berth"):
            for index in range(5):
                _ = self.persist(self.attempt(at=index, receiver=receiver))
        sink: list[tuple[str, int, int]] = []
        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            sink.append((notice.kind, notice.copies, notice.receivers))
            self.assertEqual(state.sender, "watchdog")
            self.assertEqual(limits, self.limits)
            return "delivered"
        guard.drain(self.root, deliver, 6)
        guard.drain(self.root, deliver, 7)
        self.assertEqual(sink, [("owner", 10, 2)])

    def test_different_texts_have_separate_episodes_and_expire_after_inactivity(self) -> None:
        for text in ("Morty stopped", "Berth stopped"):
            for at in range(3):
                _ = guard.transition(self.state, self.attempt(text, at=at), self.limits)
        self.assertEqual([notice.kind for notice in self.state.notices].count("owner"), 2)
        for at in range(self.limits.window_seconds + 5, self.limits.window_seconds + 8):
            _ = guard.transition(self.state, self.attempt("Morty stopped", at=at), self.limits)
        self.assertEqual([notice.kind for notice in self.state.notices].count("owner"), 3)

    def test_held_flood_notifies_only_the_owner_and_cannot_feed_the_guard(self) -> None:
        owner_sink: list[str] = []
        def run(command: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
            self.assertEqual(stdin, "")
            self.assertEqual(timeout, self.limits.notice_timeout_seconds)
            self.assertNotIn("pushover.py", " ".join(command))
            options = send.parse(command[2:])
            self.assertEqual(options.sender, guard.NOTICE_SENDER)
            self.assertEqual(options.to, "owner")
            owner_sink.append(options.text or "")
            decision = guard.check(self.root, guard.Attempt(options.sender or "", options.to,
                options.text or "", "fixture-host", 4))
            self.assertFalse(decision.held)
            return 0, "SENT: fake sink", ""
        for index in range(4):
            _ = self.persist(self.attempt(at=index))
        with patch.object(send, "run", run), patch.object(send, "repeat_notice_owner", return_value="owner"), \
                patch.object(send, "user", side_effect=AssertionError("unexpected phone delivery")) as phone:
            guard.drain(self.root, send.deliver_repeat_notice, 5)
            guard.drain(self.root, send.deliver_repeat_notice, 6)
        phone.assert_not_called()
        self.assertEqual(len(owner_sink), 1)
        self.assertIn("watchdog", owner_sink[0])
        self.assertIn("4 copies", owner_sink[0])
        self.assertIn("1 sessions", owner_sink[0])
        self.assertIn("Morty: worker stopped", owner_sink[0])
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)

    def test_saved_phone_notices_are_dropped_without_rejecting_owner_work(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        raw = cast(dict[str, object], json.loads(path.read_text()))
        notices = cast(list[dict[str, object]], raw["notices"])
        owner = notices[0]
        for status in ("pending", "claimed", "queued", "unknown", "delivered"):
            notices.append({**owner, "kind": "phone", "status": status})
        _ = path.write_text(json.dumps(raw))
        self.assertEqual([notice.kind for notice in guard.decode(path.read_text()).notices], ["owner"])
        calls: list[str] = []
        guard.drain(self.root, lambda state, notice, limits: calls.append(notice.kind) or "delivered", 3)
        self.assertEqual(calls, ["owner"])
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual([notice["kind"] for notice in cast(list[dict[str, object]], saved["notices"])], ["owner"])

    def test_concurrent_processes_admit_on_contention_and_keep_valid_state(self) -> None:
        program = ("import sys; from pathlib import Path; from scripts.message import repeat_guard as g; "
                   + "print(g.check(Path(sys.argv[1]), g.Attempt('watchdog','morty','stopped','fixture-host',float(sys.argv[2]))).held)")
        def attempt(index: int) -> bool:
            result = subprocess.run([sys.executable, "-c", program, str(self.root), str(index)],
                capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
                env=self.owned.environment())
            return result.stdout.strip() == "True"
        # Every competing process must fail open while this real shard lock is held.
        with guard.sender_record(self.root, "watchdog"):
            with ThreadPoolExecutor(max_workers=8) as workers:
                held = list(workers.map(attempt, range(8)))
        self.assertEqual(held, [False] * 8)
        self.assertEqual(list(self.root.glob("*.json")), [])
        # Once contention ends, durable counting still enforces the threshold.
        self.assertEqual([attempt(index) for index in range(8, 11)], [False, False, True])
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual(state.rates["morty"].count, 3)
        self.assertEqual(state.holds, [10])

    def test_missing_or_invalid_config_fails_open_and_prints_one_error(self) -> None:
        for contents in (None, "repeat_count=bad\n"):
            path = self.home / ("missing.conf" if contents is None else "bad.conf")
            if contents is not None:
                _ = path.write_text(contents)
            errors = io.StringIO()
            with patch.dict(os.environ, {"SEND_REPEAT_CONFIG": str(path)}), redirect_stderr(errors):
                for index in range(5):
                    self.assertFalse(guard.check(self.root, self.attempt(at=index)).held)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertFalse(self.root.exists())

    def limits_file(self, limits: guard.Limits) -> Path:
        path = self.home / "limits.conf"
        values = cast(dict[str, int], asdict(limits))
        _ = path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        return path

    def test_config_is_read_at_use_and_unfinished_notices_outlive_counters(self) -> None:
        config = self.limits_file(replace(self.limits, repeat_count=4))
        with patch.dict(os.environ, {"SEND_REPEAT_CONFIG": str(config)}):
            self.assertEqual([self.persist(self.attempt(at=index)).held for index in range(4)],
                             [False, False, False, True])
            delivered: list[tuple[guard.SenderState, guard.Notice, guard.Limits]] = []
            guard.drain(self.root, lambda state, notice, limits: delivered.append((state, notice, limits)) or "delivered", self.limits.window_seconds + 5)
            self.assertEqual([item[1].kind for item in delivered], ["owner"])
            guard.drain(self.root, lambda state, notice, limits: "delivered", self.limits.window_seconds + 6)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_replay_is_pure_and_counts_no_distinct_or_protected_holds(self) -> None:
        rows: list[dict[str, object]] = [{"from": "watchdog", "to": "morty", "text": text,
            "time": f"2026-10-31T01:00:0{index}+00:00", "machine": "fixture-host",
            "safety": "distinct" if text == "recovered" else "repeat"}
            for index, text in enumerate(("stopped", "stopped", "stopped", "recovered"))]
        rows += [{**rows[0], "from": "user"}, {**rows[0], "to": "user"}]
        result = repeats.replay(rows, self.limits)
        self.assertEqual(result["watchdog"].held, 1)
        self.assertEqual(sum(item.distinct_held for item in result.values()), 0)
        self.assertEqual(sum(item.protected_held for item in result.values()), 0)
        self.assertFalse(self.root.exists())

    def prime_outbox(self) -> None:
        self.assertEqual([self.persist(self.attempt(at=index)).held for index in range(3)],
                         [False, False, True])

    def test_letter_names_and_meaningful_digit_tokens_never_collide(self) -> None:
        for texts in (("process Ada stopped", "process Abe stopped", "process Bea stopped"),
                      ("process worker1 stopped", "process worker2 stopped", "process worker3 stopped"),
                      ("issue #1 in repo1", "issue #2 in repo2", "issue #3 in repo3")):
            for text in texts:
                self.assertFalse(self.apply(text).held, text)

    def test_held_lock_and_reloadable_capacity_never_delay_or_split_a_sender(self) -> None:
        start = time.monotonic()
        with guard.sender_record(self.root, "watchdog"):
            with self.assertRaises(BlockingIOError):
                with guard.sender_record(self.root, "watchdog"):
                    self.fail("capacity reload selected a different lock")
            with patch.dict(os.environ, {"SEND_REPEAT_CONFIG": str(self.limits_file(replace(self.limits, max_senders=7)))}):
                with patch.object(guard, "GUARD_IO_CEILING_SECONDS", 0.25):
                    self.assertFalse(guard.check(self.root, self.attempt()).held)
                    self.assertLess(time.monotonic() - start, guard.GUARD_IO_CEILING_SECONDS * DEADLINE_CEILING_FACTOR)

    def test_stalled_reads_and_publication_release_the_send_at_its_deadline(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        contents = path.read_text()
        path.unlink()
        os.mkfifo(path)
        start = time.monotonic()
        with patch.object(guard, "GUARD_IO_CEILING_SECONDS", 0.25):
            self.assertFalse(guard.check(self.root, self.attempt(at=4)).held)
            self.assertLess(time.monotonic() - start, guard.GUARD_IO_CEILING_SECONDS * DEADLINE_CEILING_FACTOR)
        path.unlink()
        _ = path.write_text(contents)
        # A fresh worker reaches the real publication boundary before it is stalled.
        marker = self.home / "publication"
        program = """import json, sys, threading
from dataclasses import asdict
from pathlib import Path
from scripts.message import repeat_guard as g
def stalled(path, text):
    Path(sys.argv[2]).write_text(str(path) + "\\n" + text)
    threading.Event().wait()
g.write_text_atomic = stalled
print(json.dumps(asdict(g._stored_check(Path(sys.argv[1]), g.Attempt('watchdog','session:morty','Morty: worker stopped','fixture-host',4)))))
"""
        start = time.monotonic()
        with patch.dict(os.environ, {"PYTHONPATH": str(Path(guard.__file__).parent)}):
            self.assertFalse(guard.isolated([sys.executable, "-c", program, str(self.root), str(marker)], "", 1).held)
        self.assertLess(time.monotonic() - start, DEADLINE_CEILING_FACTOR)
        self.assertIn(str(path), marker.read_text())
        published = cast(dict[str, dict[str, dict[str, object]]], json.loads(marker.read_text().split("\n", 1)[1]))
        self.assertEqual(published["rates"]["session:morty"]["count"], 4)

    def assert_notice(self, state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> None:
        self.assertTrue(any(item.id == notice.id for item in state.notices))
        self.assertGreaterEqual(notice.copies, limits.repeat_count)

    def test_confirmed_notice_failure_retries_but_queue_ownership_does_not(self) -> None:
        self.prime_outbox()
        for at in range(3):
            _ = self.persist(self.attempt("other flood", at=at))
        calls: list[str] = []
        def failed(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            calls.append(notice.kind)
            return "failed" if notice.text == "Morty: worker stopped" else "queued"
        guard.drain(self.root, failed, 3)
        def delivered(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            calls.append(notice.kind)
            return "delivered"
        guard.drain(self.root, delivered, 4)
        guard.drain(self.root, delivered, 5)
        self.assertEqual(calls, ["owner", "owner", "owner"])

    def test_lost_acknowledgement_retries_the_durable_outbox_with_its_stable_key(self) -> None:
        self.prime_outbox()
        keys: list[str] = []
        def unknown(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            keys.append(notice.id)
            return "unknown"
        guard.drain(self.root, unknown, 3)
        guard.drain(self.root, unknown, 4)
        self.assertEqual(len(keys), 2)
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual([notice.status for notice in state.notices], ["unknown"])
        self.assertEqual(keys[0], keys[1])

    def test_notice_delivery_distinguishes_queued_failure_and_lost_acknowledgement(self) -> None:
        self.prime_outbox()
        saved = guard.decode(next(self.root.glob("*.json")).read_text())
        notice = saved.notices
        keys: list[str] = []
        for code, output, expected in ((1, "QUEUED: durable owner", "queued"),
                                        (0, "KEPT: until silence ends", "queued"),
                                        (0, "REPLACED: newer notice", "unknown"),
                                        (2, "REFUSED: action required", "failed"),
                                        (4, "HELD: review", "failed"),
                                        (0, "checking recipient\nSENT: fake sink", "delivered"),
                                        (1, "checking recipient\nQUEUED: durable owner", "queued"),
                                        (3, "FAILED: unavailable", "failed"),
                                        (None, "", "unknown"), (-9, "", "unknown"), (0, "SKIPPED: previous claim", "unknown"),
                                        (0, "", "unknown"), (0, "SENT: fake sink", "delivered")):
            with patch.object(send, "repeat_notice_owner", return_value="owner"), patch.object(send, "run", return_value=(code, output, "")) as delivery:
                self.assertEqual(send.deliver_repeat_notice(saved, notice[0], self.limits), expected)
            arguments = cast(list[str], delivery.call_args.args[0])
            keys.append(arguments[arguments.index("--key") + 1])
        self.assertEqual(len(set(keys)), 1)
        self.assertIn(notice[0].id, keys[0])
        self.assertEqual(arguments[arguments.index("--repeat-minutes") + 1], "0")

    def test_notice_payload_does_not_turn_a_failure_into_a_queued_notice(self) -> None:
        self.prime_outbox()
        saved = guard.decode(next(self.root.glob("*.json")).read_text())
        for code in (1, 3):
            with (self.subTest(code=code), patch.object(send, "repeat_notice_owner", return_value="owner"),
                  patch.object(send, "run", return_value=(code, "FAILED: unavailable\nQUEUED: quoted result", ""))):
                self.assertEqual(send.deliver_repeat_notice(saved, saved.notices[0], self.limits), "failed")

    def test_an_active_episode_crosses_both_window_boundaries_without_another_alert(self) -> None:
        _ = self.apply("unrelated", at=0)
        for at in (1790, 1791, 1792, 1800, 3591, 3592):
            _ = self.apply("stopped", at=at)
        self.assertTrue(self.apply("stopped", at=3593).held)
        self.assertEqual([notice.kind for notice in self.state.notices].count("owner"), 1)
        self.assertEqual([flood.first for flood in self.state.floods.values()], [1790])

    def test_infinite_saved_counter_is_dropped_and_never_holds_or_crashes_a_send(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        raw = cast(dict[str, object], json.loads(path.read_text()))
        raw["rate_first"] = 0
        raw["rate_count"] = float("inf")
        _ = path.write_text(json.dumps(raw).replace("Infinity", "1e309"))
        self.assertFalse(guard.check(self.root, self.attempt(at=4)).held)
        self.assertFalse(path.exists())
        self.assertFalse(guard.check(self.root, self.attempt(at=5)).held)

    def test_drain_lock_is_nonblocking_and_rotation_advances_before_a_slow_sender(self) -> None:
        self.prime_outbox()
        descriptor = os.open(self.root / "drain.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls: list[str] = []
            guard.drain(self.root, lambda state, notice, limits: calls.append(notice.kind) or "delivered", 3)
            self.assertEqual(calls, [])
        finally:
            os.close(descriptor)
        for at in range(3):
            _ = self.persist(guard.Attempt("other", "session:Bea", "different flood", "fixture", at))
        visited: list[str] = []
        def interrupted(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            visited.append(state.sender)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            guard.drain(self.root, interrupted, 3)
        first = visited[0]
        def delivered(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            visited.append(state.sender)
            return "delivered"
        guard.drain(self.root, delivered, 4)
        self.assertNotEqual(visited[1], first)

    def test_notice_supervisor_kills_real_delivery_child_and_grandchild(self) -> None:
        marker = self.home / "descendants"
        self.owned.markers.append(marker)
        ready = self.home / "ready"
        os.mkfifo(ready)
        grandchild = "import os,signal; os.kill(os.getpid(), signal.SIGSTOP)"
        child_program = r"""import subprocess,sys,os,signal
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', sys.argv[2]])
Path(sys.argv[1]).write_text(str(os.getpid()) + ' ' + str(child.pid))
with open(sys.argv[3], 'w') as ready: ready.write('ready\n')
os.kill(os.getpid(), signal.SIGSTOP)
"""
        runner = "import sys; from scripts.message import send; send.run(sys.argv[1:], '', 60)"
        command = [sys.executable, "-c", runner, sys.executable, "-c", child_program, str(marker), grandchild, str(ready)]
        # Start the deadline check only after the delivery descendants exist.
        child = subprocess.Popen(command, start_new_session=True,
            env=self.owned.environment({**os.environ, "PYTHONPATH": str(Path(guard.__file__).parent), "SEND_REPEAT_NOTICE_JOB": "1"}))
        self.owned.track(child, group=True)
        try:
            wait_for_notice_readiness(ready, child)
            started = time.monotonic()
            with patch.object(guard, "Popen", return_value=child):
                guard.notice_job(command, 1)
            self.assertLess(time.monotonic() - started, DEADLINE_CEILING_FACTOR)
            for pid in marker.read_text().split():
                wait_for_notice_exit(int(pid))
                status = subprocess.run(["ps", "-p", pid, "-o", "stat="], text=True, capture_output=True, check=False)
                self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"), status.stdout)
        finally:
            self.owned.finish()

    def test_notice_readiness_fails_fast_when_the_runner_exits(self) -> None:
        for opens_fifo in (False, True):
            with self.subTest(opens_fifo=opens_fifo):
                ready = self.home / f"early-exit-{opens_fifo}"
                os.mkfifo(ready)
                program = "import sys; "
                if opens_fifo:
                    program += "open(sys.argv[1], 'w').close(); "
                program += "sys.exit(7)"
                child = subprocess.Popen([sys.executable, "-c", program, str(ready)],
                                         env=self.owned.environment())
                self.owned.track(child)
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(AssertionError, "runner exited 7 before readiness"):
                        wait_for_notice_readiness(ready, child)
                    self.assertLess(time.monotonic() - started, TEST_HANG_GUARD_SECS / 10)
                    self.assertEqual(child.returncode, 7)
                finally:
                    self.owned.finish()

    def test_direct_events_report_observed_outcomes_and_replay_uses_independent_labels(self) -> None:
        receiver = guard.recipient_identity("", thread_id="thread-1")
        for text in ["stopped"] * 3:
            decision = guard.transport_check("direct", receiver, text)
            if not decision.held:
                guard.transport_result("direct", receiver, "stopped", "queued")
        rows = [cast(dict[str, object], json.loads(line))
                for line in (self.home / "state/message/transports.jsonl").read_text().splitlines()]
        self.assertEqual([row["outcome"] for row in rows], ["queued", "queued", "held"])
        totals = repeats.replay(rows, self.limits, simulate=False)["direct"]
        self.assertEqual((totals.count, totals.held, totals.receivers), (3, 1, {receiver}))
        labelled = [{**rows[0], "text": f"process {name} stopped", "safety": "distinct"}
                    for name in ("Ada", "Abe", "Bea")]
        result = repeats.replay(labelled, self.limits)["direct"]
        self.assertEqual((result.held, result.distinct_held), (0, 0))
        with patch.object(guard, "normalize", return_value="broken collision"):
            result = repeats.replay(labelled, self.limits)["direct"]
        self.assertEqual((result.held, result.distinct_held), (1, 1))

    def test_digit_prefixed_names_dates_durations_and_meaningful_paths_stay_distinct(self) -> None:
        for texts in (
            [f"process {i}-worker stopped" for i in (1, 2, 3)],
            [f"due 2026-10-{i:02}" for i in (1, 2, 3)],
            [f"wait {i} seconds" for i in (1, 2, 3)],
            [f"inspect /work/release-{i}/notes.md" for i in (1, 2, 3)],
            [f"pid {i}-worker stopped" for i in (1, 2, 3)],
        ):
            self.assertEqual([self.apply(text).held for text in texts], [False, False, False])

    def test_import_and_protected_calls_do_not_read_config_and_fifo_config_is_bounded(self) -> None:
        fifo = self.home / "policy"
        os.mkfifo(fifo)
        workers = self.home / "fifo-workers"
        self.owned.markers.append(workers)
        program = """import sys
from pathlib import Path
original = Path.read_text
def read(path, *args, **kwargs):
    return original(Path(sys.argv[1]) if path.name == "send_repeat_guard.conf" else path, *args, **kwargs)
Path.read_text = read
from scripts.message import repeat_guard as g
g.CONFIG=Path(sys.argv[1])
launch = g.Popen
def tracked(*args, **kwargs):
    child = launch(*args, **kwargs)
    with Path(sys.argv[3]).open('a') as marker: marker.write(str(child.pid) + '\\n')
    return child
g.Popen = tracked
g.GUARD_IO_CEILING_SECONDS = 0.25
assert not g.check(Path(sys.argv[2]), g.Attempt('user','session:a','same','fixture',0)).held
print('ready', flush=True)
sys.stdin.readline()
assert not g.check(Path(sys.argv[2]), g.Attempt('watchdog','session:a','same','fixture',0)).held
"""
        command = [sys.executable, "-c", program, str(fifo), str(self.root), str(workers)]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
                env=self.owned.environment({**os.environ, "SEND_REPEAT_CONFIG": str(fifo),
                     "PYTHONPATH": str(Path(guard.__file__).parent)})) as process:
            self.owned.track(process, group=True)
            try:
                with LauncherHangGuard("guard import and protected call").protect():
                    if process.stdout is None or process.stdout.readline() != "ready\n":
                        raise AssertionError("guard import or protected call did not finish")
                stdout, stderr = process.communicate("go\n", timeout=TEST_HANG_GUARD_SECS)
                result = subprocess.CompletedProcess(command, process.wait(timeout=TEST_HANG_GUARD_SECS), stdout, stderr)
            finally:
                self.owned.finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("operation deadline exceeded", result.stderr)
        self.assertFalse(self.root.exists())

    def test_interrupted_real_send_claim_retries_before_and_after_sink_acceptance(self) -> None:
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                for path in self.root.glob("*.json"):
                    path.unlink()
                self.prime_outbox()
                self.run_notice_fixture(phase)
                state = guard.decode(next(self.root.glob("*.json")).read_text())
                self.assertEqual({notice.kind: notice.status for notice in state.notices},
                                 {"owner": "claimed"})
                claims = cast(dict[str, object], json.loads((self.home / "state/message/keys.json").read_text()))
                self.assertTrue(any(str(key).endswith(":owner") for key in claims))
                before = self.fixture_accepts()
                self.run_notice_fixture("ok")
                accepted = self.fixture_accepts()[len(before):]
                self.assertEqual([entry["kind"] for entry in accepted], ["owner"])
                self.assertEqual(accepted[0]["key"], next(str(key) for key in claims if str(key).endswith(":owner")))
                if phase == "after":
                    self.assertEqual(before[-1], accepted[0])
                self.run_notice_fixture("ok")
                self.assertEqual(self.fixture_accepts()[len(before):], accepted)

    def test_each_flood_gets_its_owner_outbox_entry_without_a_sender_cooldown(self) -> None:
        for text in ("Ada stopped", "Bea stopped"):
            for at in range(3):
                _ = self.apply(text, at=at)
        self.assertEqual([n.kind for n in self.state.notices], ["owner", "owner"])


    def run_notice_fixture(self, mode: str) -> None:
        path = self.home / "notice_fixture.py"
        if not path.exists():
            _ = path.write_text(NOTICE_FIXTURE)
        child = subprocess.Popen([sys.executable, str(path), "supervise", mode, str(self.root), str(self.home / "accepted")],
            env=self.owned.environment({**os.environ, "SEND_REPEAT_CONFIG": str(self.limits_file(self.limits))}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.owned.track(child, group=True)
        try:
            _, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(child.returncode, 0, errors)
        finally:
            self.owned.finish()

    def fixture_accepts(self) -> list[dict[str, str]]:
        path = self.home / "accepted"
        return [cast(dict[str, str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_real_send_claim_stops_retry_after_durable_queue_ownership(self) -> None:
        self.prime_outbox()
        self.run_notice_fixture("queued")
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual([notice.status for notice in state.notices], ["queued"])
        self.assertEqual(len(list((self.home / "state/message/queue").glob("*.jsonl"))), 1)
        before = self.fixture_accepts()
        self.run_notice_fixture("ok")
        self.assertEqual(self.fixture_accepts(), before)

    def test_old_masked_counters_reset_without_discarding_unfinished_notices(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        old = cast(dict[str, object], json.loads(path.read_text()))
        _ = old.pop("identity_version")
        _ = path.write_text(json.dumps(old))
        before = guard.decode(path.read_text()).notices
        self.assertEqual([self.persist(self.attempt(at=index)).held for index in range(3, 6)], [False, False, True])
        state = guard.decode(path.read_text())
        self.assertEqual(state.identity_version, 2)
        self.assertTrue(all(any(item.id == notice.id and item.kind == notice.kind for item in state.notices) for notice in before))

    def test_unfinished_notices_survive_episode_expiry_until_the_configured_age_limit(self) -> None:
        self.prime_outbox()
        for at in (self.limits.window_seconds + 3, self.limits.notice_max_age_seconds + 1):
            guard.drain(self.root, lambda state, notice, limits: "unknown", at)
            state = guard.decode(next(self.root.glob("*.json")).read_text())
            self.assertEqual([notice.status for notice in state.notices], ["unknown"])
        guard.drain(self.root, lambda state, notice, limits: self.fail("expired notice was delivered"),
                    self.limits.notice_max_age_seconds + 2)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_outbox_capacity_admits_new_floods_without_evicting_unfinished_notices(self) -> None:
        limits = replace(self.limits, max_keys=1)
        for at in range(3):
            _ = guard.transition(self.state, self.attempt(at=at), limits)
        for at in range(3):
            _ = guard.transition(self.state, self.attempt("second flood", at=limits.window_seconds + 3 + at), limits)
        before = cast(object, asdict(self.state)["notices"])
        for at in range(3):
            self.assertFalse(guard.transition(self.state, self.attempt("new flood", at=2 * limits.window_seconds + 6 + at), limits).held)
        self.assertEqual(asdict(self.state)["notices"], before)

    def test_full_budget_is_reserved_before_rotated_notices_are_attempted(self) -> None:
        self.prime_outbox()
        for at in range(3):
            _ = self.persist(self.attempt("other flood", at=at))
        clock = [0.0]
        completed: list[str] = []
        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            clock[0] += 60 if notice.text == "Morty: worker stopped" else 45
            if notice.text == "other flood":
                completed.append(notice.text)
                return "delivered"
            return "unknown"
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            guard.drain(self.root, deliver, 3, cleanup_seconds=20)
            self.assertEqual(completed, [])
            state = guard.decode(next(self.root.glob("*.json")).read_text())
            self.assertEqual([notice.text for notice in state.notices], ["other flood", "Morty: worker stopped"])
            guard.drain(self.root, deliver, 4, cleanup_seconds=20)
            self.assertEqual(completed, ["other flood"])

    def test_budget_deferral_preserves_sender_progress_until_an_attempt_is_admitted(self) -> None:
        self.prime_outbox()
        for at in range(3):
            _ = self.persist(guard.Attempt("other", "session:Bea", "other flood", "fixture-host", at))
        a_path, b_path = sorted(self.root.glob("*.json"))
        a = guard.decode(a_path.read_text())
        b = guard.decode(b_path.read_text())
        for at in range(3):
            _ = self.persist(guard.Attempt(b.sender, "session:Bea", "second flood", "fixture-host", at))
        b_before = guard.decode(b_path.read_text()).notices
        clock = [0.0]
        attempts: list[str] = []
        remaining_at_start: list[float] = []
        limits = replace(self.limits, drain_timeout_seconds=90, notice_timeout_seconds=60)

        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            sender = "A" if state.sender == a.sender else "B"
            attempts.append(f"{sender}.{notice.text}")
            remaining_at_start.append(90 - clock[0])
            self.assertGreaterEqual(remaining_at_start[-1], 80)
            if sender == "B" and notice.text == "second flood":
                clock[0] += 45
                return "delivered"
            clock[0] += 60
            return "unknown"

        with patch.object(guard, "configuration", return_value=limits), \
                patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
                patch.dict(os.environ, {"SEND_REPEAT_NOTICE_DEADLINE": "inf"}):
            for invocation in range(4):
                clock[0] = 0
                guard.drain(self.root, deliver, 3 + invocation, cleanup_seconds=20)
                if invocation == 0:
                    self.assertEqual((self.root / "cursor").read_text(), a_path.name)
                    self.assertEqual(guard.decode(b_path.read_text()).notices, b_before)
        self.assertEqual(attempts, [f"A.{a.notices[0].text}", f"B.{b.notices[0].text}", f"A.{a.notices[0].text}", "B.second flood"])
        self.assertEqual(remaining_at_start, [90, 90, 90, 90])
        b_after = guard.decode(b_path.read_text())
        self.assertEqual(next(notice.status for notice in b_after.notices if notice.text == "second flood"), "delivered")

    def run_blocked_notice_fixture(self, mode: str) -> list[NoticeAttemptTrace]:
        path = self.home / "notice_fixture.py"
        _ = path.write_text(NOTICE_FIXTURE)
        limits = replace(self.limits, notice_timeout_seconds=1, drain_timeout_seconds=1)
        clock = NoticeDrainClock()
        attempts: list[NoticeAttemptTrace] = []
        real_run = send.run

        def run(command: list[str], request: str, timeout: float) -> tuple[int | None, str, str]:
            return run_ready_notice_attempt(real_run,
                [command[0], str(path), "attempt", mode, str(self.root), str(self.home / "accepted")],
                request, timeout, mode if attempts[-1].sender == "watchdog" else "")

        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            attempt = NoticeAttemptTrace(state.sender, notice.id, mode if state.sender == "watchdog" else "")
            attempts.append(attempt)
            try:
                outcome = send.attempt_repeat_notice(state, notice, limits)
                attempt.outcome = outcome
                return outcome
            finally:
                clock.elapsed += 1

        with (patch.object(guard, "configuration", return_value=limits), patch.object(guard, "time", clock),
              patch.object(send, "run", run), patch.dict(os.environ, {
                  "SEND_REPEAT_NOTICE_DEADLINE": "inf", "SEND_REPEAT_NOTICE_JOB": "0"})):
            guard.drain(self.root, deliver, 3)
        return attempts

    def notice_state(self, sender: str) -> guard.SenderState:
        return next(state for path in self.root.glob("*.json")
                    if (state := guard.decode(path.read_text())).sender == sender)

    def start_notice_rotation_at_watchdog(self) -> None:
        paths = sorted(self.root.glob("*.json"))
        index = next(index for index, path in enumerate(paths)
                     if guard.decode(path.read_text()).sender == "watchdog")
        _ = (self.root / "cursor").write_text(paths[index - 1].name)

    def test_blocked_discovery_cannot_starve_later_owner_notices(self) -> None:
        self.prime_outbox()
        for text, sender in (("other flood", "watchdog"), ("third flood", "other")):
            for at in range(3):
                _ = self.persist(guard.Attempt(sender, "session:Bea", text, "fixture-host", at))
        self.start_notice_rotation_at_watchdog()
        first_id = self.notice_state("watchdog").notices[0].id
        attempts = self.run_blocked_notice_fixture("owner-block")
        self.assertEqual([(item.sender, item.notice_id, item.marker, item.outcome) for item in attempts],
                         [("watchdog", first_id, "owner-block", "unknown")], str(attempts))
        self.assertEqual(next(notice.status for notice in self.notice_state("watchdog").notices
                              if notice.id == first_id), "unknown", str(attempts))
        attempts += self.run_blocked_notice_fixture("owner-block")
        self.assertEqual([(item.sender, item.outcome) for item in attempts],
                         [("watchdog", "unknown"), ("other", "delivered")], str(attempts))
        self.assertEqual([notice.status for notice in self.notice_state("other").notices],
                         ["delivered"], str(attempts))
        self.assertTrue(any(entry["kind"] == "owner" and entry["source"] == "other"
                            for entry in self.fixture_accepts()), str(attempts))

    def test_blocked_owner_delivery_rotates_behind_other_floods_and_senders(self) -> None:
        self.prime_outbox()
        for text, sender in (("other flood", "watchdog"), ("third flood", "other")):
            for at in range(3):
                _ = self.persist(guard.Attempt(sender, "session:Bea", text, "fixture-host", at))
        for at in range(3):
            _ = self.persist(guard.Attempt("new", "session:Bea", "fourth flood", "fixture-host", at))
        self.start_notice_rotation_at_watchdog()
        notice_ids = [notice.id for notice in self.notice_state("watchdog").notices]
        healthy_order = [guard.decode(path.read_text()).sender for path in sorted(self.root.glob("*.json"),
                         key=lambda path: (path.name <= (self.root / "cursor").read_text(), path.name))]
        attempts: list[NoticeAttemptTrace] = []
        for iteration in range(4):
            with self.subTest(iteration=iteration):
                attempts += self.run_blocked_notice_fixture("delivery-block")
                if iteration == 0:
                    self.assertEqual([(item.sender, item.notice_id, item.marker, item.outcome) for item in attempts],
                                     [("watchdog", notice_ids[0], "delivery-block", "unknown")], str(attempts))
                    self.assertEqual(next(notice.status for notice in self.notice_state("watchdog").notices
                                          if notice.id == notice_ids[0]), "unknown", str(attempts))
        self.assertEqual([item.sender for item in attempts], healthy_order + ["watchdog"], str(attempts))
        self.assertEqual([item.notice_id for item in attempts if item.sender == "watchdog"], notice_ids, str(attempts))
        self.assertEqual([item.marker for item in attempts if item.sender == "watchdog"],
                         ["delivery-block", "delivery-block"], str(attempts))
        self.assertEqual([item.outcome for item in attempts],
                         ["unknown", "delivered", "delivered", "unknown"], str(attempts))
        for sender in ("other", "new"):
            self.assertEqual([notice.status for notice in self.notice_state(sender).notices], ["delivered"], str(attempts))
        self.assertEqual([notice.status for notice in self.notice_state("watchdog").notices],
                         ["unknown", "unknown"], str(attempts))
        owners = {entry["source"] for entry in self.fixture_accepts() if entry["kind"] == "owner"}
        self.assertTrue({"other", "new"}.issubset(owners), str(attempts))


if __name__ == "__main__":
    _ = unittest.main()
