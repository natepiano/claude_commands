"""Repeat transitions, durable concurrency, and notices through a fake push sink."""
from __future__ import annotations

import io
import fcntl
import json
import time
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send
import repeat_guard as guard
import repeats


NOTICE_FIXTURE = r'''import sys,os,json,signal,io
from pathlib import Path
from unittest.mock import patch
import send,repeat_guard as g,roster
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
        if mode=='owner-block' and source=='watchdog': signal.pause()
        return 'codex:owner'
    def accept(message,*args,**kwargs):
        if kind=='phone' and mode=='phone-block': signal.pause()
        if kind=='phone' and mode in ('before','after'):
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
        return send.EXIT[result.outcome],result.outcome.upper()+': '+result.detail,''
    sys.stdin=io.StringIO(json.dumps(request))
    with patch.object(send,'repeat_notice_owner',owner), patch.object(send,'run',transport):
        send.main(['repeat-notice-attempt'])
'''


class RepeatGuardTests(unittest.TestCase):
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    limits: guard.Limits  # pyright: ignore[reportUninitializedInstanceVariable]
    state: guard.SenderState  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")}))
        limits = guard.configuration()
        if isinstance(limits, guard.GuardUnavailable):
            raise AssertionError(limits.reason)
        self.limits = limits
        self.state = guard.SenderState("watchdog", "fixture-host", 0, 0)
        self.root = self.home / "state/message/repeats"

    def attempt(self, text: str = "Morty: worker stopped", *, at: float = 1,
                receiver: str = "session:morty", protected: bool = False) -> guard.Attempt:
        return guard.Attempt("watchdog", receiver, text, "fixture-host", at, protected)

    def apply(self, text: str = "Morty: worker stopped", *, at: float = 1) -> guard.Decision:
        return guard.transition(self.state, self.attempt(text, at=at), self.limits)

    def persist(self, attempt: guard.Attempt) -> guard.Decision:
        # Storage and notice fixtures use the real worker without depending on
        # interpreter startup scheduling. Public deadline tests still use check.
        return guard._stored_check(self.root, attempt)  # pyright: ignore[reportPrivateUsage]

    def test_first_two_copies_pass_and_the_third_is_held_and_recorded(self) -> None:
        self.assertEqual([self.apply(at=index).held for index in range(1, 6)], [False, False, True, True, True])
        self.assertEqual(self.state.holds, [3, 4, 5])

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
        for index in range(self.limits.sender_rate_count + 1):
            self.assertFalse(self.apply(f"worker-{index}: inspect issue #{index}", at=index).held)
        self.assertEqual([notice.kind for notice in self.state.notices], ["owner"])

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

    def test_one_owner_and_phone_notice_cover_all_receivers_of_a_flood(self) -> None:
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
        self.assertEqual(sink, [("owner", 10, 2), ("phone", 10, 2)])

    def test_different_texts_have_separate_episodes_and_expire_after_inactivity(self) -> None:
        for text in ("Morty stopped", "Berth stopped"):
            for at in range(3):
                _ = guard.transition(self.state, self.attempt(text, at=at), self.limits)
        self.assertEqual([notice.kind for notice in self.state.notices].count("phone"), 2)
        for at in range(self.limits.window_seconds + 5, self.limits.window_seconds + 8):
            _ = guard.transition(self.state, self.attempt("Morty stopped", at=at), self.limits)
        self.assertEqual([notice.kind for notice in self.state.notices].count("phone"), 3)

    def test_notice_commands_use_the_existing_push_template_and_cannot_feed_the_guard(self) -> None:
        push_sink: list[str] = []
        owner_sink: list[str] = []
        def run(command: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
            self.assertEqual(stdin, "")
            self.assertEqual(timeout, self.limits.notice_timeout_seconds)
            options = send.parse(command[2:])
            self.assertEqual(options.sender, guard.NOTICE_SENDER)
            if options.to == "user":
                self.assertEqual(options.text_id, guard.PHONE_TEXT)
                push_sink.append(options.text or "")
            else:
                owner_sink.append(options.text or "")
            decision = guard.check(self.root, guard.Attempt(options.sender or "", options.to,
                options.text or "", "fixture-host", 4))
            self.assertFalse(decision.held)
            return 0, "SENT: fake sink", ""
        for index in range(4):
            _ = self.persist(self.attempt(at=index))
        with patch.object(send, "run", run), patch.object(send, "repeat_notice_owner", return_value="owner"):
            guard.drain(self.root, send.deliver_repeat_notice, 5)
        self.assertEqual(len(push_sink), 1)
        self.assertIn("watchdog", push_sink[0])
        self.assertIn("4 messages", push_sink[0])
        self.assertIn("1 sessions", push_sink[0])
        self.assertEqual(len(owner_sink), 1)
        self.assertIn("Morty: worker stopped", owner_sink[0])
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)

    def test_concurrent_processes_admit_on_contention_and_keep_valid_state(self) -> None:
        program = ("import sys; from pathlib import Path; import repeat_guard as g; "
                   + "print(g.check(Path(sys.argv[1]), g.Attempt('watchdog','morty','stopped','fixture-host',float(sys.argv[2]))).held)")
        def attempt(index: int) -> bool:
            result = subprocess.run([sys.executable, "-c", program, str(self.root), str(index)],
                env={**os.environ, "PYTHONPATH": str(Path(guard.__file__).parent)},
                capture_output=True, text=True, check=True)
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
        self.assertEqual(state.rate_count, 3)
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
            self.assertEqual([item[1].kind for item in delivered], ["owner", "phone"])
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
                self.assertFalse(guard.check(self.root, self.attempt()).held)
        self.assertLess(time.monotonic() - start, guard.GUARD_IO_CEILING_SECONDS + 0.2)

    def test_stalled_reads_and_publication_release_the_send_at_its_deadline(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        contents = path.read_text()
        path.unlink()
        os.mkfifo(path)
        start = time.monotonic()
        self.assertFalse(guard.check(self.root, self.attempt(at=4)).held)
        self.assertLess(time.monotonic() - start, guard.GUARD_IO_CEILING_SECONDS + 0.2)
        path.unlink()
        _ = path.write_text(contents)
        # A fresh worker reaches the real publication boundary before it is stalled.
        marker = self.home / "publication"
        program = """import json, sys, threading
from dataclasses import asdict
from pathlib import Path
import repeat_guard as g
def stalled(path, text):
    Path(sys.argv[2]).write_text(str(path) + "\\n" + text)
    threading.Event().wait()
g.write_text_atomic = stalled
print(json.dumps(asdict(g._stored_check(Path(sys.argv[1]), g.Attempt('watchdog','session:morty','Morty: worker stopped','fixture-host',4)))))
"""
        start = time.monotonic()
        with patch.dict(os.environ, {"PYTHONPATH": str(Path(guard.__file__).parent)}):
            self.assertFalse(guard.isolated([sys.executable, "-c", program, str(self.root), str(marker)], "", 1).held)
        self.assertLess(time.monotonic() - start, 1.3)
        self.assertIn(str(path), marker.read_text())
        self.assertIn('"rate_count": 4', marker.read_text())

    def assert_notice(self, state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> None:
        self.assertTrue(any(item.id == notice.id for item in state.notices))
        self.assertGreaterEqual(notice.copies, limits.repeat_count)

    def test_confirmed_notice_failure_retries_but_queue_ownership_does_not(self) -> None:
        self.prime_outbox()
        calls: list[str] = []
        def failed(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            calls.append(notice.kind)
            return "failed" if notice.kind == "phone" else "queued"
        guard.drain(self.root, failed, 3)
        def delivered(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            calls.append(notice.kind)
            return "delivered"
        guard.drain(self.root, delivered, 4)
        guard.drain(self.root, delivered, 5)
        self.assertEqual(calls, ["owner", "phone", "phone"])

    def test_lost_acknowledgement_retries_the_durable_outbox_with_its_stable_key(self) -> None:
        self.prime_outbox()
        keys: list[str] = []
        def unknown(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            keys.append(notice.id)
            return "unknown"
        guard.drain(self.root, unknown, 3)
        guard.drain(self.root, unknown, 4)
        self.assertEqual(len(keys), 4)
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual([notice.status for notice in state.notices], ["unknown", "unknown"])
        self.assertEqual(keys[0], keys[1])

    def test_notice_delivery_distinguishes_queued_failure_and_lost_acknowledgement(self) -> None:
        self.prime_outbox()
        saved = guard.decode(next(self.root.glob("*.json")).read_text())
        notice = saved.notices
        keys: list[str] = []
        for code, output, expected in ((1, "QUEUED: durable owner", "queued"),
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

    def test_an_active_episode_crosses_both_window_boundaries_without_another_alert(self) -> None:
        _ = self.apply("unrelated", at=0)
        for at in (1790, 1791, 1792, 1800, 3591, 3592):
            _ = self.apply("stopped", at=at)
        self.assertTrue(self.apply("stopped", at=3593).held)
        self.assertEqual([notice.kind for notice in self.state.notices].count("phone"), 1)
        self.assertEqual([flood.first for flood in self.state.floods.values()], [1790])

    def test_infinite_saved_counter_is_dropped_and_never_holds_or_crashes_a_send(self) -> None:
        self.prime_outbox()
        path = next(self.root.glob("*.json"))
        raw = cast(dict[str, object], json.loads(path.read_text()))
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
        grandchild = "import os,signal; os.kill(os.getpid(), signal.SIGSTOP)"
        child_program = """import subprocess,sys,os,signal
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', sys.argv[2]])
Path(sys.argv[1]).write_text(str(os.getpid()) + ' ' + str(child.pid))
os.kill(os.getpid(), signal.SIGSTOP)
"""
        runner = "import sys; import send; send.run(sys.argv[1:], '', 60)"
        command = [sys.executable, "-c", runner, sys.executable, "-c", child_program, str(marker), grandchild]
        started = time.monotonic()
        with patch.dict(os.environ, {"PYTHONPATH": str(Path(guard.__file__).parent)}):
            guard.notice_job(command, 1)
        self.assertLess(time.monotonic() - started, 1.4)
        for pid in marker.read_text().split():
            try:
                status = subprocess.run(["ps", "-p", pid, "-o", "stat="], text=True, capture_output=True, check=False)
                self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"), status.stdout)
            finally:
                try:
                    os.kill(int(pid), 9)
                except ProcessLookupError:
                    pass

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
        program = """import sys
from pathlib import Path
original = Path.read_text
def read(path, *args, **kwargs):
    return original(Path(sys.argv[1]) if path.name == "send_repeat_guard.conf" else path, *args, **kwargs)
Path.read_text = read
import repeat_guard as g
g.CONFIG=Path(sys.argv[1])
assert not g.check(Path(sys.argv[2]), g.Attempt('user','session:a','same','fixture',0)).held
assert not g.check(Path(sys.argv[2]), g.Attempt('watchdog','session:a','same','fixture',0)).held
"""
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", program, str(fifo), str(self.root)],
            env={**os.environ, "SEND_REPEAT_CONFIG": str(fifo), "PYTHONPATH": str(Path(guard.__file__).parent)},
            capture_output=True, text=True, timeout=2, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 1)
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
                                 {"owner": "delivered", "phone": "claimed"})
                claims = cast(dict[str, object], json.loads((self.home / "state/message/keys.json").read_text()))
                self.assertTrue(any(str(key).endswith(":phone") for key in claims))
                before = self.fixture_accepts()
                self.run_notice_fixture("ok")
                accepted = self.fixture_accepts()[len(before):]
                self.assertEqual([entry["kind"] for entry in accepted], ["phone"])
                self.assertEqual(accepted[0]["key"], next(str(key) for key in claims if str(key).endswith(":phone")))
                if phase == "after":
                    self.assertEqual(before[-1], accepted[0])
                self.run_notice_fixture("ok")
                self.assertEqual(self.fixture_accepts()[len(before):], accepted)

    def test_each_flood_gets_its_owner_outbox_entry_without_a_sender_cooldown(self) -> None:
        for text in ("Ada stopped", "Bea stopped"):
            for at in range(3):
                _ = self.apply(text, at=at)
        self.assertEqual([n.kind for n in self.state.notices], ["owner", "phone", "owner", "phone"])


    def run_notice_fixture(self, mode: str, *, short: bool = False) -> None:
        path = self.home / "notice_fixture.py"
        if not path.exists():
            _ = path.write_text(NOTICE_FIXTURE)
        limits = replace(self.limits, notice_timeout_seconds=1, drain_timeout_seconds=3) if short else self.limits
        result = subprocess.run([sys.executable, str(path), "supervise", mode, str(self.root), str(self.home / "accepted")],
            env={**os.environ, "SEND_REPEAT_CONFIG": str(self.limits_file(limits)),
                 "PYTHONPATH": str(Path(guard.__file__).parent)}, capture_output=True, text=True, timeout=8 if short else 10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def fixture_accepts(self) -> list[dict[str, str]]:
        path = self.home / "accepted"
        return [cast(dict[str, str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_real_send_claim_stops_retry_after_durable_queue_ownership(self) -> None:
        self.prime_outbox()
        self.run_notice_fixture("queued")
        state = guard.decode(next(self.root.glob("*.json")).read_text())
        self.assertEqual([notice.status for notice in state.notices], ["queued", "queued"])
        self.assertEqual(len(list((self.home / "state/message/queue").glob("*.jsonl"))), 2)
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
            self.assertEqual([notice.status for notice in state.notices], ["unknown", "unknown"])
        guard.drain(self.root, lambda state, notice, limits: self.fail("expired notice was delivered"),
                    self.limits.notice_max_age_seconds + 2)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_outbox_capacity_admits_new_floods_without_evicting_unfinished_notices(self) -> None:
        limits = replace(self.limits, max_keys=1)
        for at in range(3):
            _ = guard.transition(self.state, self.attempt(at=at), limits)
        before = cast(object, asdict(self.state)["notices"])
        for at in range(3):
            self.assertFalse(guard.transition(self.state, self.attempt("new flood", at=limits.window_seconds + 3 + at), limits).held)
        self.assertEqual(asdict(self.state)["notices"], before)

    def test_full_budget_is_reserved_before_rotated_notices_are_attempted(self) -> None:
        self.prime_outbox()
        clock = [0.0]
        completed: list[str] = []
        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            clock[0] += 60 if notice.kind == "owner" else 45
            if notice.kind == "phone":
                completed.append(notice.kind)
                return "delivered"
            return "unknown"
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            guard.drain(self.root, deliver, 3, cleanup_seconds=20)
            self.assertEqual(completed, [])
            state = guard.decode(next(self.root.glob("*.json")).read_text())
            self.assertEqual([notice.kind for notice in state.notices], ["phone", "owner"])
            guard.drain(self.root, deliver, 4, cleanup_seconds=20)
            self.assertEqual(completed, ["phone"])

    def test_budget_deferral_preserves_sender_progress_until_an_attempt_is_admitted(self) -> None:
        self.prime_outbox()
        for at in range(3):
            _ = self.persist(guard.Attempt("other", "session:Bea", "other flood", "fixture-host", at))
        a_path, b_path = sorted(self.root.glob("*.json"))
        a = guard.decode(a_path.read_text())
        a.notices = [notice for notice in a.notices if notice.kind == "phone"]
        _ = a_path.write_text(json.dumps(asdict(a)))
        b_before = guard.decode(b_path.read_text()).notices
        clock = [0.0]
        attempts: list[str] = []
        remaining_at_start: list[float] = []
        limits = replace(self.limits, drain_timeout_seconds=90, notice_timeout_seconds=60)

        def deliver(state: guard.SenderState, notice: guard.Notice, limits: guard.Limits) -> guard.NoticeOutcome:
            self.assert_notice(state, notice, limits)
            sender = "A" if state.sender == a.sender else "B"
            attempts.append(f"{sender}.{notice.kind}")
            remaining_at_start.append(90 - clock[0])
            self.assertGreaterEqual(remaining_at_start[-1], 80)
            if sender == "B" and notice.kind == "phone":
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
        self.assertEqual(attempts, ["A.phone", "B.owner", "A.phone", "B.phone"])
        self.assertEqual(remaining_at_start, [90, 90, 90, 90])
        b_after = guard.decode(b_path.read_text())
        self.assertEqual(next(notice.status for notice in b_after.notices if notice.kind == "phone"), "delivered")

    def test_blocked_discovery_and_phone_attempts_cannot_starve_later_notices(self) -> None:
        self.prime_outbox()
        for text, sender in (("other flood", "watchdog"), ("third flood", "other")):
            for at in range(3):
                _ = self.persist(guard.Attempt(sender, "session:Bea", text, "fixture-host", at))
        self.run_notice_fixture("owner-block", short=True)
        self.run_notice_fixture("owner-block", short=True)
        self.assertTrue(any(entry["kind"] == "phone" and entry["source"] == "watchdog" for entry in self.fixture_accepts()))

    def test_blocked_phone_rotates_behind_other_floods_and_senders(self) -> None:
        self.prime_outbox()
        for text, sender in (("other flood", "watchdog"), ("third flood", "other")):
            for at in range(3):
                _ = self.persist(guard.Attempt(sender, "session:Bea", text, "fixture-host", at))
        for at in range(3):
            _ = self.persist(guard.Attempt("new", "session:Bea", "fourth flood", "fixture-host", at))
        for iteration in range(4):
            with self.subTest(iteration=iteration):
                self.run_notice_fixture("phone-block", short=True)
        owners = {entry["source"] for entry in self.fixture_accepts() if entry["kind"] == "owner"}
        self.assertTrue({"other", "new"}.issubset(owners))


if __name__ == "__main__":
    _ = unittest.main()
