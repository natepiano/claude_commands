"""Check SSH routes with fake execution and real result parsers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import redirect_stdout
import io
from typing import TypedDict, cast, override
import unittest
from unittest import mock

from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, GIT_STUB
from scripts.direct import verify_doc_test_support as doc_support
from scripts.build_targets import test_ssh_eligibility as eligibility_support
from scripts.lint.memory_test_support import fake_memory_readings, write_config_fixture


REPOSITORY = Path(__file__).resolve().parents[2]

build_targets = eligibility_support.build_targets
mac_test = eligibility_support.mac_test


class SshRunnerEvent(TypedDict):
    target: str
    owner: int
    slot: int
    reserved: bool
    same_path: bool
    queued_at: str
    memory_reservation: str
    actions: list[str]
    mode: str
    result_type: str
    clock: str
    holder: str


class MacWaitRegistration(TypedDict):
    markers: list[bool]
    memory_hook: bool


SSH_RUNNER = r'''
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import redirect_stdout
from unittest import mock

source = Path(os.environ["TEST_SOURCE_REPO"])
sys.path.insert(0, str(source))
for name in ("mac_test", "build_targets", "build_hold", "buildlog", "lint", "lib"):
    sys.path.insert(0, str(source / "scripts" / name))
import offload
import mac_test

args = offload.parser().parse_args(namespace=offload.CliArguments())
request = offload.request_from_arguments(args)
config = offload.read_config(Path(args.settings))
controller = offload.ProcessController()
root = Path(os.environ["TEST_RUNNER_STATE"])
mode = json.loads(os.environ.get("TEST_SSH_MODES", "{}" )).get(request.target, "passed")
if mode == "disk-once":
    mode = "passed" if (root / "ssh-events.jsonl").exists() else "disk"
actions = []
initial = mac_test.read_claims(mac_test.state_paths(request.target))
before = {claim.path: claim.record for claim in initial}
reserved = bool(args.claim_owner_pid and any(claim.record["pid"] == args.claim_owner_pid for claim in initial))
same_path = False

def complete(arguments, attempt):
    global same_path
    parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
    text = io.StringIO()
    with redirect_stdout(text):
        status = mac_test.command_claim(parsed, mac_test.state_paths(parsed.target), watch_claim=False)
    attempt.answer = offload.read_claim_answer(offload.CommandOutput(status, text.getvalue(), ""))
    actions.append("claim:" + str(status))
    after = mac_test.read_claims(mac_test.state_paths(parsed.target))
    same_path = any(claim.path in before and claim.record["pid"] == os.getpid() for claim in after)

def capture(arguments, deadline=offload.NO_COMMAND_TIME_LIMIT):
    if len(arguments) > 2 and arguments[2] == "release":
        parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
        actions.append("release")
        return offload.CommandOutput(mac_test.command_release(parsed, mac_test.state_paths(parsed.target)), "", "")
    versions = {("rustc", "-V"): "rustc fixture",
                ("cargo", "nextest", "--version"): "nextest fixture",
                ("cargo", "mend", "--version"): "mend fixture"}
    if tuple(arguments) in versions:
        return offload.CommandOutput(0, versions[tuple(arguments)] + "\n", "")
    if arguments[0] == "git" and arguments[-2:] == ["rev-parse", "HEAD"]:
        return offload.CommandOutput(0, "fixture-commit\n", "")
    assert arguments[0] == "ssh", arguments
    if arguments[-1] == "true":
        actions.append("reachability")
        return offload.CommandOutput(0, "", "")
    actions.append("probe")
    if mode == "probe-leftovers":
        return offload.CommandOutput(255, "", "host unavailable")
    if mode == "disk":
        return offload.CommandOutput(0, "free_gib=0\nhost=test-mac\nrc=0\n", "")
    if mode == "probe-refusal":
        (root / (request.target + ".marker")).touch()
        return offload.CommandOutput(75, "linux_startup=unresolved\n", "")
    wire = "free_gib=1000\nhost=aws-build\nrustc=rustc fixture\nnextest=nextest fixture\n"
    wire += "mend=mend fixture\nscripts_commit=fixture-commit\nrc=0\n"
    if mode == "after-probe":
        wire += "mac_cleanup=unconfirmed\n"
        (root / (request.target + ".marker")).touch()
    return offload.CommandOutput(0, wire, "")

def copy_scripts(*arguments):
    actions.append("scripts-copy")
    return True

def copy_source(*arguments):
    actions.append("source-copy")
    return offload.CopiedTree(offload.KnownTreeKey("sent-tree"))

def stream(arguments):
    actions.append("run")
    if mode in ("run-refusal", "after-probe"):
        wire, status = "linux_startup=unresolved\n", 75
        (root / (request.target + ".marker")).touch()
    elif mode == "lost":
        wire, status = "connection lost\n", 255
    else:
        status = 42 if mode == "failed" else 4 if mode == "no-tests" else 0
        wire = "mac_exit=" + str(status) + "\nmac_cleanup=0\n"

    class Remote:
        stdout = io.StringIO(wire)
        def wait(self):
            return status

    with mock.patch.object(subprocess, "Popen", return_value=Remote()):
        return offload.ProcessController.stream_mac_run(controller, arguments)

real_run = subprocess.run
def commands(arguments, **options):
    if arguments[0] == "ssh":
        actions.append("cleanup")
        unresolved = mode in ("probe-refusal", "run-refusal", "after-probe")
        count = 2 if mode in ("probe-leftovers", "retained-leftovers") else 0
        return subprocess.CompletedProcess(arguments, 3 if unresolved else 0, "" if unresolved else "leftovers=" + str(count) + "\nrc=0\n", "")
    return real_run(arguments, **options)

started = time.monotonic()
with mock.patch.object(controller, "complete_claim", side_effect=complete), \
     mock.patch.object(controller, "capture", side_effect=capture), \
     mock.patch.object(controller, "stream_mac_run", side_effect=stream), \
     mock.patch.object(offload, "copy_tracked_scripts", side_effect=copy_scripts), \
     mock.patch.object(offload, "copy_tree_with_key", side_effect=copy_source), \
     mock.patch.object(offload, "trusted_linux_source", return_value=True), \
     mock.patch.object(subprocess, "run", side_effect=commands):
    result = offload.run_offload(controller, config, request, started)

event = dict(target=request.target, owner=args.claim_owner_pid, slot=controller.attempt_evidence.slot,
             reserved=reserved, same_path=same_path, queued_at=os.environ.get("BUILDLOG_MEM_WAIT_SINCE", ""),
             memory_reservation=os.environ.get("BUILDLOG_MEM_RESERVATION", ""), actions=actions, mode=mode,
             result_type=type(result).__name__,
             holder=type(result.holder).__name__ if isinstance(result, offload.BusyMacDecline) else "",
             clock=Path(os.environ["TEST_ROUTING_CLOCK"]).read_text() if os.environ.get("TEST_ROUTING_CLOCK") else "")
with (root / "ssh-events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
offload.persist_attempt(request, result, controller.attempt_evidence, started)
raise SystemExit(offload.write_result(result, request.result, request.last_decline, request.verb,
                                     request.target, controller.attempt_evidence))
'''


class VerifySshTargetsTests(VerifyMacOffloadFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment.update(fake_memory_readings(self.root))
        self.environment["TEST_SOURCE_REPO"] = str(REPOSITORY)
        self.write_executable(self.runner, SSH_RUNNER)
        self.write_executable(self.bin_directory / "git", GIT_STUB.replace(
            "wanted=ls-files", 'wanted=remote\nif has "$@"; then printf "https://example.com/team/hana.git\\n"; exit 0; fi\nwanted=ls-files'))
        _ = (self.root / ".git/config").write_text('[remote "origin"]\nurl=https://example.com/team/hana.git\n')
        self.configure("mac,natedev,aws")

    def configure(self, order: str) -> None:
        _ = (self.root / "build_targets.conf").write_text(
            f"targets={order}\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n"
            + "kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=off\n")
        for name in ("aws_target", "spare"):
            _ = write_config_fixture(self.root / f"{name}.conf",
                "offload=on\nhost=fake-aws\nkinds=test,check,doc\nslots=1\nslots.hana=1\n"
                + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\n"
                + "parity.mend=mend fixture\nparity.scripts_commit=fixture-commit\n", template="aws_target")

    def ssh_events(self) -> list[SshRunnerEvent]:
        path = self.runner_state / "ssh-events.jsonl"
        return [cast(SshRunnerEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def attempts(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if cast(dict[str, object], json.loads(line)).get("kind") == "ssh_attempt"]

    def enable_mac(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\nlinux_only.case=\n")

    def release_memory_after_polls(self, *, advance_clock: bool = False) -> None:
        _ = self.hold_memory()
        self.environment["TEST_ADVANCE_CLOCK"] = "1" if advance_clock else "0"
        self.write_executable(self.bin_directory / "sleep", r'''
import json
import os
from pathlib import Path
root = Path(os.environ["TEST_RUNNER_STATE"])
path = root / "memory-polls.jsonl"
events = root / "ssh-events.jsonl"
row = {"attempts": len(events.read_text().splitlines()) if events.exists() else 0}
with path.open("a") as output:
    output.write(json.dumps(row) + "\n")
count = len(path.read_text().splitlines())
if count == 2 and os.environ["TEST_ADVANCE_CLOCK"] == "1":
    Path(os.environ["TEST_ROUTING_CLOCK"]).write_text("281")
if count >= 3:
    Path(os.environ["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 268435456 kB\n")
''')

    def retain_slots(self, capacity: int = 2) -> list[Path]:
        _ = write_config_fixture(self.root / "aws_target.conf",
                                 (self.root / "aws_target.conf").read_text()
                                 + f"slots={capacity}\nslots.hana={capacity}\n",
                                 template="aws_target")
        paths: list[Path] = []
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(mac_test, "process_start_time", side_effect=eligibility_support.SshEligibilityTests().process_start), \
             mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.RepositoryScope(
                 "hana", "https://example.com/team/hana")):
            state = mac_test.state_paths("aws")
            for pid in range(2147483000, 2147483000 + capacity):
                args = mac_test.CliArguments()
                args.target, args.worktree, args.pid = "aws", str(self.root), pid
                args.what, args.remote = "test hana", "fake-aws"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(mac_test.command_claim(args, state, watch_claim=False), 0)
            paths = [claim.path for claim in mac_test.read_claims(state)]
        self.assertEqual(len(paths), capacity)
        return paths

    def test_failed_mac_reservation_keeps_wait_registration_visible_to_the_idle_reader(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        self.release_memory_after_polls()
        wrapper = self.root / "reserve-refusal.py"
        self.write_executable(wrapper, r'''
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

source = Path(os.environ["TEST_SOURCE_REPO"])
sys.path.insert(0, str(source))
sys.path.insert(0, str(source / "scripts/buildlog"))
from scripts.buildlog import mac_idle
from scripts.direct import step_record

if sys.argv[1] == "reserve":
    row = json.loads(step_record.record_path(os.environ["BUILDLOG_CALL_ID"]).read_text())
    process = mac_idle.LiveProcess(row["pid"], 1, row["proc_start"], ("verify.sh",), {}, row["repo"])
    with mock.patch.object(step_record, "process_start", return_value=process.start):
        calls = mac_idle.waiting_calls(mac_idle.watch_paths(), {process.pid: process})
    snapshot = {"markers": [call.marker_hook for call in calls],
                "memory_hook": bool(os.environ.get("LINT_QUEUE_LEAVE_HOOK"))}
    with (Path(os.environ["TEST_RUNNER_STATE"]) / "idle-registrations.jsonl").open("a") as output:
        output.write(json.dumps(snapshot) + "\n")
    raise SystemExit(1)
raise SystemExit(subprocess.run([sys.executable, str(source / "scripts/build_targets/build_targets.py"),
                                *sys.argv[1:]], check=False).returncode)
''')
        result = self.verify("test", "sample", "--no-cache", extra={"BUILD_TARGETS_SCRIPT": str(wrapper)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshots = [cast(MacWaitRegistration, json.loads(line))
                     for line in (self.runner_state / "idle-registrations.jsonl").read_text().splitlines()]
        self.assertTrue(snapshots)
        self.assertTrue(any(snapshot["memory_hook"] for snapshot in snapshots))
        self.assertTrue(all(snapshot["markers"] == [True] for snapshot in snapshots), snapshots)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_mac_backoff_table_runs_locally_without_a_remote_attempt(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        _ = (state / "unreachable_until").write_text(str(10**12))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_queue_retry_cleans_retained_slots_after_their_owners_exit(self) -> None:
        self.configure("natedev,aws")
        claims = self.retain_slots()
        self.release_memory_after_polls()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertIn("cleanup", event["actions"])
        self.assertIn("claim:13", event["actions"])
        self.assertLess(event["actions"].index("cleanup"), event["actions"].index("run"))
        self.assertEqual(event["result_type"], "PassedOnTarget")
        self.assertEqual(self.local_runs(), [])
        self.assertLess(sum(path.exists() for path in claims), len(claims))

    def test_retained_slots_stay_owned_when_retry_cannot_confirm_cleanup(self) -> None:
        self.configure("natedev,aws,spare")
        claims = self.retain_slots()
        self.release_memory_after_polls()
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"retained-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["target"], second["target"]), ("aws", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertIn("cleanup", first["actions"])
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertTrue(all(path.exists() for path in claims))
        self.assertEqual(first["queued_at"], second["queued_at"])
        self.assertFalse((self.home / ".local/state/build-targets/aws/unreachable_until").exists())

    def test_permanent_mac_refusal_leaves_no_memory_retry_candidate(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        self.release_memory_after_polls()
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.ssh_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        attempt, = self.attempts()
        self.assertEqual((attempt["outcome"], attempt["completion"]), ("no_tests", "confirmed"))

    def test_permanent_mac_refusal_keeps_other_ssh_retry_candidates(self) -> None:
        self.configure("mac,natedev,aws")
        self.enable_mac()
        self.release_memory_after_polls()
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["target"] for event in self.ssh_events()], ["mac", "aws"])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_mac_memory_retry_waits_until_its_retry_deadline(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        _ = self.control_routing_clock()
        self.release_memory_after_polls(advance_clock=True)
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"disk-once"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["clock"], second["clock"]), ("100", "281"))
        polls = [cast(dict[str, int], json.loads(line))
                 for line in (self.runner_state / "memory-polls.jsonl").read_text().splitlines()]
        self.assertEqual([row["attempts"] for row in polls[:2]], [1, 1])
        self.assertEqual(self.local_runs(), [])

    def test_failed_probe_with_live_leftovers_continues_without_backoff(self) -> None:
        self.configure("aws,spare,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"probe-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["target"], second["target"]), ("aws", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertEqual(first["queued_at"], second["queued_at"])
        state = self.home / ".local/state/build-targets/aws"
        self.assertTrue(list((state / "claims").glob("**/*.json")))
        self.assertFalse((state / "unreachable_until").exists())
        attempt = self.attempts()[0]
        self.assertEqual((attempt["cleanup"], attempt["unresolved_count"]), ("unconfirmed", 2))

    def test_no_matching_tests_have_a_distinct_durable_outcome(self) -> None:
        self.configure("aws,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"aws":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        attempt, = self.attempts()
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("no_tests", "confirmed", "confirmed"))
        self.assertEqual(len(self.local_runs()), 1)

    def test_free_linux_target_takes_a_queued_test_without_a_local_hold(self) -> None:
        _ = self.hold_memory()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertGreater(event["owner"], 0)
        self.assertEqual(event["slot"], 1)
        self.assertEqual(event["result_type"], "PassedOnTarget")
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_held_local_target_does_not_block_linux_check(self) -> None:
        _ = self.routing_fixture()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertIn("run", event["actions"])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_linux_slot_takes_a_queued_check_without_a_local_hold(self) -> None:
        _ = self.hold_memory()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(event["actions"][:3], ["claim:0", "scripts-copy", "source-copy"])
        self.assertLess(event["actions"].index("source-copy"), event["actions"].index("probe"))
        self.assertFalse(any(row["args"][:1] == ["check"] for row in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_confirmed_linux_failure_never_repeats_the_test_locally(self) -> None:
        self.configure("aws,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"aws":"failed"}'})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        attempt, = self.attempts()
        self.assertEqual(self.ssh_events()[0]["result_type"], "FailedOnTarget")
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("failed", "confirmed", "confirmed"))

    def test_linux_loss_keeps_the_attempt_and_queue_age_after_local_success(self) -> None:
        self.configure("aws,natedev")
        first = self.root / "first-wait"
        _ = first.write_text("100.25\n")
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"lost"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25",
            "BUILDLOG_MEM_WAIT_FIRST_FILE": str(first)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertTrue(self.ssh_events(), result.stdout + result.stderr)
        self.assertTrue(self.attempts(), json.dumps(self.ssh_events()) + result.stdout + result.stderr)
        attempt, = self.attempts()
        self.assertEqual((attempt["target"], attempt["slot"], attempt["outcome"], attempt["queued_at"]),
                         ("aws", 1, "lost", 100.25))
        self.assertEqual(attempt["repo_identity"], "https://example.com/team/hana")
        self.assertEqual(self.routing_record()["target"], "natedev")
        self.assertEqual(first.read_text().strip(), "100.25")

    def test_local_only_request_never_reserves_or_runs_linux_work(self) -> None:
        self.configure("aws,natedev")
        result = self.verify("test", "sample", "--local", "requires local files", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.attempts(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_busy_startup_keeps_queue_age_and_continues_to_the_next_ssh_target(self) -> None:
        class StartupFixture(VerifySshTargetsTests):
            pass

        for mode in ("probe-refusal", "run-refusal", "after-probe"):
            with self.subTest(mode=mode):
                fixture = StartupFixture()
                fixture.setUp()
                try:
                    fixture.configure("aws,spare,natedev")
                    result = fixture.verify("test", "sample", "--no-cache", extra={
                        "TEST_SSH_MODES": json.dumps({"aws": mode}), "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    events = fixture.ssh_events()
                    self.assertEqual([event["target"] for event in events], ["aws", "spare"],
                                     json.dumps(events) + result.stdout + result.stderr)
                    self.assertEqual(fixture.local_runs(), [])
                    self.assertNotIn("release", events[0]["actions"])
                    self.assertEqual(events[0]["queued_at"], "100.25")
                    self.assertEqual(events[1]["queued_at"], "100.25")
                    self.assertTrue((fixture.runner_state / "aws.marker").exists())
                    state = fixture.home / ".local/state/build-targets/aws"
                    self.assertTrue(list((state / "claims").glob("**/*.json")))
                    self.assertFalse((state / "unreachable_until").exists())
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    attempts = fixture.attempts()
                    self.assertEqual([attempt["target"] for attempt in attempts], ["aws", "spare"])
                    self.assertEqual((attempts[0]["outcome"], attempts[0]["completion"], attempts[0]["cleanup"]),
                                     ("declined", "missing", "unconfirmed"))
                    self.assertIsNone(attempts[0]["unresolved_count"])
                finally:
                    fixture.doCleanups()


class SshReservationTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: eligibility_support.SshEligibilityTests = eligibility_support.SshEligibilityTests()

    @override
    def setUp(self) -> None:
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        _ = write_config_fixture(self.fixture.root / "aws_target.conf", "offload=on\nslots=1\nslots.hana=1\n")

    def reserve(self, pid: int) -> tuple[int, Path]:
        path = self.fixture.root / f"reservation-{pid}"
        args = build_targets.CliArguments()
        args.repo_root, args.package, args.kind = str(self.fixture.root), "linux-part", "test"
        args.pid, args.reservation_file = pid, str(path)
        return build_targets.command_reserve(args), path

    def test_retry_recovers_its_claim_when_the_reservation_file_was_not_published(self) -> None:
        state = mac_test.state_paths("aws")
        args = mac_test.CliArguments()
        args.target, args.repo_root, args.pid = "aws", str(self.fixture.root), 71
        args.settings, args.package, args.verb = str(self.fixture.root / "aws_target.conf"), "linux-part", "nextest"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim_wait(args, state), 0)
        claim, = mac_test.read_claims(state)
        destination = self.fixture.root / "reservation-71"
        self.assertFalse(destination.exists())
        status, answer = self.reserve(71)
        self.assertEqual(status, 0)
        self.assertEqual(answer.read_text().split("\t")[0], "aws")
        recovered, = mac_test.read_claims(state)
        self.assertEqual(recovered.path, claim.path)
        self.assertEqual(recovered.record, claim.record)
        self.assertEqual(self.reserve(72)[0], 1)

    def test_wait_release_keeps_a_claim_from_an_earlier_process_with_the_same_pid(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        state = mac_test.state_paths("aws")
        claim, = mac_test.read_claims(state)
        with mock.patch.object(mac_test, "process_start_time", return_value="reused-start"):
            self.assertEqual(build_targets.command_release_wait(71), 0)
        self.assertTrue(claim.path.exists())
        self.assertEqual(mac_test.read_claims(state)[0].record, claim.record)
        self.assertEqual(build_targets.command_release_wait(71), 0)
        self.assertFalse(claim.path.exists())

    def test_reservation_race_keeps_the_loser_queued_without_an_answer(self) -> None:
        status, first = self.reserve(71)
        self.assertEqual(status, 0)
        self.assertEqual(first.read_text().split("\t")[0], "aws")
        with mock.patch.object(mac_test, "free_now", return_value="free"):
            status, losing = self.reserve(72)
        self.assertEqual(status, 1)
        self.assertFalse(losing.exists())
        claims = mac_test.read_claims(mac_test.state_paths("aws"))
        self.assertEqual([claim.record["pid"] for claim in claims], [71])
        self.assertNotIn("remote_host", claims[0].record)

    def test_runner_adopts_the_reserved_slot_without_an_unclaimed_interval(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        paths = mac_test.state_paths("aws")
        original, = mac_test.read_claims(paths)
        args = mac_test.CliArguments()
        args.target, args.worktree, args.what = "aws", str(self.fixture.root), "check hana"
        args.pid, args.from_pid, args.remote = 91, 71, "fake-aws"
        unlink = Path.unlink

        def prevent_gap(path: Path, missing_ok: bool = False) -> None:
            self.assertNotEqual(path, original.path, "The reservation must remain held during transfer")
            unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=prevent_gap), redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False), 0)
        transferred, = mac_test.read_claims(paths)
        self.assertEqual(transferred.path, original.path)
        self.assertEqual(transferred.scope, original.scope)
        self.assertEqual(transferred.record["pid"], 91)
        self.assertEqual(transferred.record.get("remote_host"), "fake-aws")
        self.assertEqual(build_targets.command_release_wait(71), 0)
        self.assertEqual(build_targets.command_release_wait(91), 0)
        self.assertTrue(original.path.exists())
        self.assertEqual(self.reserve(72)[0], 1)

    def test_exit_releases_only_an_untransferred_local_reservation(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        self.assertEqual(build_targets.command_release_wait(71), 0)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())
        self.assertEqual(self.reserve(72)[0], 0)


class SshResultBoundaryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: eligibility_support.SshEligibilityTests = eligibility_support.SshEligibilityTests()

    @override
    def setUp(self) -> None:
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_linux_result_keeps_completion_separate_from_unconfirmed_cleanup(self) -> None:
        offload = eligibility_support.offload
        destination = self.fixture.root / "result"
        evidence = offload.AttemptEvidence(slot=2, completion="confirmed", cleanup="unconfirmed",
                                          unresolved_count=offload.UnresolvedLeftoverCount(), evidence_at=150.5)
        with redirect_stdout(io.StringIO()):
            status = offload.write_result(offload.PassedOnTarget(4.5), offload.ResultFile(destination),
                                          target="aws", evidence=evidence)
        self.assertEqual(status, 0)
        fields = dict(line.split("=", 1) for line in destination.read_text().splitlines())
        self.assertEqual(fields["outcome"], "passed")
        self.assertEqual(fields["target"], "aws")
        self.assertEqual(fields["slot"], "2")
        self.assertEqual(fields["completion"], "confirmed")
        self.assertEqual(fields["cleanup"], "unconfirmed")
        self.assertEqual(fields["unresolved_count"], "unavailable")
        self.assertEqual(fields["evidence_at"], "150.5")
        self.assertNotIn("mac", fields)

    def test_mac_wire_results_keep_the_legacy_boundary(self) -> None:
        offload = eligibility_support.offload
        destination = self.fixture.root / "result"
        for result, expected_status, outcome in ((offload.PassedOnMac(4.5), 0, "passed"),
                                                  (offload.FailedOnMac(42, 4.5), 42, "failed")):
            with self.subTest(outcome=outcome), redirect_stdout(io.StringIO()):
                status = offload.write_result(result, offload.ResultFile(destination))
                self.assertEqual(status, expected_status)
                self.assertEqual(destination.read_text(), f"mac={outcome}\nreason=\nseconds=4.500000\n")


class VerifyLinuxStagedDocTests(doc_support.VerifyDocFixture):
    def test_linux_doc_judges_the_staged_tree_without_a_local_hold(self) -> None:
        self.enable_held_mac_doc()
        for holder in Path(self.environment["BUILD_HOLD_DIR"]).iterdir():
            holder.unlink()
        targets = Path(self.environment["BUILD_TARGETS_SCRIPT"])
        self.executable(targets, r'''
import os
from pathlib import Path
import sys
words = sys.argv[1:]
settings = str(Path(os.environ["CONF_DIR"]) / "aws_target.conf")
if words[0] == "eligible":
    print("ssh_allowed")
    print("aws\teligible\t-\t" + settings)
elif words[0] == "platform":
    print("linux")
elif words[0] == "reserve":
    Path(words[words.index("--reservation-file") + 1]).write_text("aws\t" + settings + "\n")
elif words[0] not in ("release-wait", "register-local", "check-local"):
    raise AssertionError(words)
''')
        config = Path(self.environment["CONF_DIR"]) / "build_targets.conf"
        _ = config.write_text("targets=aws,natedev\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n"
                              + "kind.natedev=local\nplatform.natedev=linux\n")
        _ = write_config_fixture(config.parent / "aws_target.conf", "offload=on\n", template="aws_target")
        runner = Path(self.environment["VERIFY_MAC_RUNNER"])
        self.environment["TEST_LINUX_DOC_RUN"] = str(self.root / "linux-doc-run")
        runner_source = runner.read_text().replace('"mac=" + result', '"outcome=" + result + "\\ntarget=aws"')
        runner_source = runner_source.replace('words = sys.argv[1:]', '''words = sys.argv[1:]
assert words[words.index("--target") + 1] == "aws"
Path(os.environ["TEST_LINUX_DOC_RUN"]).touch()''')
        self.executable(runner, runner_source)
        staged = self.repo / "alpha/src/lib.rs"
        _ = staged.write_text("pub fn staged() {}\n")
        _ = self.git("add", "alpha/src/lib.rs")
        _ = staged.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("private fixture")
        original_index = (self.repo / ".git/index").read_bytes()

        result = self.verify()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(Path(self.environment["TEST_LINUX_DOC_RUN"]).exists())
        call, = self.cargo_calls()
        self.assertEqual(call["sources"]["alpha/src/lib.rs"], "pub fn staged() {}\n")
        self.assertFalse(call["notes_present"])
        self.assertNotEqual(call["cwd"], str(self.repo))
        self.assertEqual((self.repo / ".git/index").read_bytes(), original_index)
        self.assertEqual(staged.read_text(), "pub fn unstaged() {}\n")
        self.assertEqual(len(self.pass_records()), 1)


if __name__ == "__main__":
    _ = unittest.main()
