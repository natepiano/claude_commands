"""Build private fixtures for the SSH target tests."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import json
import os
from pathlib import Path
from decimal import Decimal
from dataclasses import dataclass
import subprocess
import time
from unittest import mock
from typing import Callable, TypedDict, cast, override

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission, caller_provider_controls
from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, GIT_STUB, install_fixed_cargo
from scripts.lint.memory_test_support import fake_memory_readings, write_config_fixture

from scripts.aws_target import ledger


REPOSITORY = Path(__file__).resolve().parents[2]
MAC_TEST_SCRIPT = REPOSITORY / "scripts/mac_test/mac_test.py"


class SshRunnerEvent(TypedDict):
    target: str
    owner: int
    slot: int
    reserved: bool
    same_path: bool
    queued_at: str
    memory_reservation: str
    actions: list[str]
    entry_claim_ids: list[str]
    adopted_claim_ids: list[str]
    remaining_claim_ids: list[str]
    mode: str
    result_type: str
    clock: str
    holder: str
    remote_output: str
    remote_status: int
    interruption_generation: str
    token_held: bool
    pinned: str


class CompletedMemoryHook(TypedDict):
    left: bool
    clock: str
    attempts: int


@dataclass(frozen=True)
class ReleaseMemoryAtWait:
    """Release local memory when a real queue wait occurs."""


RELEASE_MEMORY_AT_WAIT = ReleaseMemoryAtWait()


QUEUED_MEMORY_OBSERVER = r'''
import json
import os
from pathlib import Path
import sys
from unittest import mock

with open(Path(os.environ["TEST_SOURCE_REPO"]) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.build_queue import admission_client, admission_protocol as wire, placement
from scripts.lib.atomic_file import write_text_atomic


def main(arguments):
    root = Path(os.environ["TEST_RUNNER_STATE"])
    request_placement = admission_client.request_placement

    def observe(request):
        reply = request_placement(request)
        events = root / "ssh-events.jsonl"
        attempts = len(events.read_text().splitlines()) if events.exists() else 0
        if attempts < int(os.environ.get("TEST_OBSERVE_AFTER_ATTEMPTS", "0")):
            return reply
        sequence = len(tuple(root.glob("memory-hook-[0-9]*")))
        if (isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait)
                and reply.eligibility
                and not any(isinstance(candidate.eligibility, placement.EvidenceNeeded) for candidate in reply.eligibility)):
            left = False
        elif isinstance(reply, wire.Assignment) and sequence:
            left = True
        else:
            return reply
        clock = os.environ.get("TEST_ROUTING_CLOCK", "")
        event = {"left": left, "clock": Path(clock).read_text() if clock else "", "attempts": attempts,
                 "episode": request.episode,
                 "attempt": reply.assignment.attempt if isinstance(reply, wire.Assignment) else ""}
        if not left:
            with (root / "memory-polls.jsonl").open("a") as output:
                output.write(json.dumps(event) + "\n")
        write_text_atomic(root / f"memory-hook-{sequence + 1}", json.dumps(event))
        if not (root / "memory-hook-settled").exists():
            with (root / "memory-hook-ack").open() as acknowledgment:
                assert acknowledgment.read(len("continue")) == "continue"
        return reply

    with mock.patch.object(admission_client, "request_placement", side_effect=observe):
        return admission_client.main(arguments)
'''


SSH_RUNNER = r'''
import io
import json
import os
from pathlib import Path
import subprocess
import shlex
import time
from contextlib import nullcontext, redirect_stdout
from unittest import mock

source = Path(os.environ["TEST_SOURCE_REPO"])
with open(source / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.mac_test import offload, mac_test, claim_state
from scripts.build_queue import admission_protocol as queue_wire, placement

args = offload.parser().parse_args(namespace=offload.CliArguments())
request = offload.request_from_arguments(args)
config = offload.read_config(Path(args.settings))
controller = offload.ProcessController()
root = Path(os.environ["TEST_RUNNER_STATE"])
mode = json.loads(os.environ.get("TEST_SSH_MODES", "{}" )).get(request.target, "passed")
if mode in ("disk-once", "lost-once"):
    mode = "passed" if (root / "ssh-events.jsonl").exists() else mode.removesuffix("-once")
actions = []
adopted_claim_ids = []
initial = mac_test.read_claims(mac_test.state_paths(request.target))
before = {claim.path: claim.record for claim in initial}
reserved = bool(args.claim_owner_pid and any(claim.record["pid"] == args.claim_owner_pid for claim in initial))
same_path = False
remote_output = ""
remote_status = 0

def complete(arguments, attempt):
    global same_path
    parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
    text = io.StringIO()
    with redirect_stdout(text):
        if isinstance(request.placement, offload.AssignedOffloadRequest):
            assigned = request.placement.assignment
            status = mac_test.command_claim(parsed, mac_test.state_paths(parsed.target), watch_claim=False,
                                            session=assigned.session, assignment=assigned)
        else:
            status = mac_test.command_claim(parsed, mac_test.state_paths(parsed.target), watch_claim=False)
    attempt.answer = offload.read_claim_answer(offload.CommandOutput(status, text.getvalue(), ""))
    if status in (0, 13):
        controller.claim_identity = claim_state.reply_claim_identity(text.getvalue())
    actions.append("claim:" + str(status))
    after = mac_test.read_claims(mac_test.state_paths(parsed.target))
    adopted_claim_ids.extend(claim.record["claim_id"] for claim in after if claim.record["pid"] == os.getpid())
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
    if mode == "probe-interrupted":
        return offload.CommandOutput(75, "aws_interrupted=fixture-generation\n", "")
    if mode == "probe-leftovers":
        return offload.CommandOutput(255, "", "host unavailable")
    if mode == "probe-refusal":
        (root / (request.target + ".marker")).touch()
        return offload.CommandOutput(75, "linux_startup=unresolved\n", "")
    wire = "free_gib=1000\nhost=aws-build\nnextest=nextest fixture\n"
    wire += "mend=mend fixture\nscripts_commit=fixture-commit\nrc=0\n"
    if mode == "disk":
        wire = wire.replace("free_gib=1000", "free_gib=0")
    if mode == "version-mismatch":
        wire = wire.replace("nextest=nextest fixture", "nextest=nextest other")
    if mode == "after-probe":
        wire += "mac_cleanup=unconfirmed\n"
        (root / (request.target + ".marker")).touch()
    return offload.CommandOutput(0, wire, "")

def copy_scripts(*arguments):
    actions.append("scripts-copy")
    return True

def copy_source(*arguments):
    actions.append("source-copy")
    if isinstance(request.placement, offload.AssignedOffloadRequest):
        source = request.placement.assignment.source_key
        return offload.CopiedTree(offload.KnownTreeKey(source.key) if isinstance(source, placement.SourceKeyKnown)
                                  else offload.UNAVAILABLE_TREE_KEY)
    return offload.CopiedTree(offload.KnownTreeKey("sent-tree"))

def stream(arguments):
    global remote_output, remote_status
    actions.append("run")
    if mode in ("run-refusal", "after-probe"):
        wire, status = "linux_startup=unresolved\n", 75
        (root / (request.target + ".marker")).touch()
    elif mode == "runtime-version-mismatch":
        wire = (request.target + " version mismatch: rustc box=rustc other natedev=rustc fixture; "
                + "nextest box=nextest other natedev=nextest fixture; move the box pins in nixos hosts/aws-build.nix\n"
                + "mac_cleanup=kept\n")
        status = 75
    elif mode in ("interrupted", "other-generation"):
        generation = "fixture-generation" if mode == "interrupted" else "old-generation"
        wire, status = "aws_interrupted=" + generation + "\n", 255
    elif mode == "runner-sigterm":
        from scripts.mac_test import linux_runner
        from scripts.aws_target.runner_test_support import interrupt_runner, seed_runner_permission

        now = int(time.time())
        permission_directory = seed_runner_permission(Path.home(), config.source, now, "fixture-boot")
        values = offload.runner_values(offload.read_config(config.source), request.target)
        import unittest
        from scripts.mac_test.offload_test_support import mirror_use_payload

        command = shlex.split(mirror_use_payload(unittest.TestCase(), arguments)[-1])
        mirror = Path.home() / command[command.index("--mirror") + 1]
        mirror.mkdir(parents=True, exist_ok=True)
        exports = root / "shell-exports"
        shell = linux_runner.ShellEnvironment("fixture-shell", exports, root)

        def interrupted_child(argv, **options):
            assert argv[:2] == ["bash", "-c"], argv
            step, = mirror.glob(".linux-step-*")
            (step / "started").touch()
            interrupt_runner(permission_directory, now)
            return subprocess.CompletedProcess(argv, 143)

        output = io.StringIO()
        with mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"), \
             mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
             mock.patch.object(linux_runner, "prepare_shell", return_value=shell), \
             mock.patch.object(linux_runner, "trim_build_folder", return_value=0), \
             mock.patch.object(linux_runner, "output_free_bytes", return_value=1000 * 1024 ** 3), \
             mock.patch.object(subprocess, "run", side_effect=interrupted_child), \
             redirect_stdout(output):
            status = linux_runner.run(mirror, values, request.package, request.verb,
                                      request.nextest_words, request.call_id, "sent-tree")
        wire = output.getvalue()
    elif mode == "lost":
        wire, status = "connection lost\n", 255
    else:
        status = 75 if mode == "completed-decline" else 42 if mode == "failed" else 4 if mode == "no-tests" else 0
        wire = "mac_exit=" + str(status) + "\nmac_cleanup=0\n"
        if isinstance(controller.claim_identity, claim_state.ClaimIdentity):
            command = offload.start_fence_command(controller.claim_identity, request.target,
                settle=True, scope_key=controller.claim_scope_key)
            settled = real_run(["bash", "-c", command], capture_output=True,
                               text=True, check=False, timeout=5)
            assert settled.returncode == 0, settled.stderr
            wire += settled.stdout

    remote_output, remote_status = wire, status

    class Remote:
        stdout = io.StringIO(wire)
        def wait(self):
            return status
        def poll(self):
            return status

    with mock.patch.object(subprocess, "Popen", return_value=Remote()):
        return offload.ProcessController.stream_mac_run(controller, arguments)

real_run = subprocess.run
def commands(arguments, **options):
    if arguments[0] == "ssh":
        actions.append("cleanup")
        unresolved = mode in ("probe-refusal", "run-refusal", "after-probe")
        count = 2 if mode in ("probe-leftovers", "retained-leftovers") else 0
        fence = ""
        if not unresolved and count == 0 and isinstance(controller.claim_identity, claim_state.ClaimIdentity):
            fence = "dispatch_start_settled=" + offload.pending_start_key(controller.claim_identity) + "\n"
        return subprocess.CompletedProcess(arguments, 3 if unresolved else 0,
            "" if unresolved else fence + "leftovers=" + str(count) + "\nrc=0\n", "")
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

configured_mode = json.loads(os.environ.get("TEST_SSH_MODES", "{}" )).get(request.target, "passed")
if not configured_mode.endswith("-once"):
    reason = ("unreachable" if isinstance(result, (offload.LostMacRun, offload.InterruptedTargetRun)) else
              "busy" if isinstance(result, offload.BusyMacDecline) and controller.attempt_evidence.cleanup != "confirmed" else
              result.reason if isinstance(result, offload.DeclinedMacRun) else "")
    outcome = root / ("provider-outcome-" + request.target + ".json")
    if reason:
        ready = outcome.with_suffix(".ready")
        ready.write_text(json.dumps({"target": request.target, "reason": reason}))
        os.replace(ready, outcome)
    else:
        outcome.unlink(missing_ok=True)

event = dict(target=request.target, owner=args.claim_owner_pid, slot=controller.attempt_evidence.slot,
             token_held=(Path(os.environ["TEST_TOKEN_RECORD"]).exists()
                         and Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() == os.environ.get("BUILDLOG_HOLD_PID", "")),
             pinned=os.environ.get("BUILDLOG_PINNED_TARGET", ""),
             reserved=reserved, same_path=same_path, queued_at=os.environ.get("BUILDLOG_MEM_WAIT_SINCE", ""),
             memory_reservation=os.environ.get("BUILDLOG_MEM_RESERVATION", ""), actions=actions, mode=mode,
             entry_claim_ids=[claim.record["claim_id"] for claim in initial],
             adopted_claim_ids=adopted_claim_ids,
             remaining_claim_ids=[claim.record["claim_id"] for claim in mac_test.read_claims(mac_test.state_paths(request.target))],
             result_type=type(result).__name__,
             remote_output=remote_output, remote_status=remote_status,
             interruption_generation=result.generation if isinstance(result, offload.InterruptedTargetRun) else "",
             holder=type(result.holder).__name__ if isinstance(result, offload.BusyMacDecline) else "",
             clock=Path(os.environ["TEST_ROUTING_CLOCK"]).read_text() if os.environ.get("TEST_ROUTING_CLOCK") else "")
with (root / "ssh-events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
try:
    status = offload.write_result(result, request.result, request.last_decline, request.verb,
                                  request.target, controller.attempt_evidence)
finally:
    try:
        offload.report_placement_result(controller, request, result)
    except (queue_wire.AdmissionUnavailable, OSError, ValueError, ConnectionError) as error:
        print(f"offload: cannot report placement: {error}", file=sys.stderr)
    try:
        offload.persist_attempt(request, result, controller.attempt_evidence, started)
    except (OSError, ValueError) as error:
        print(f"offload: cannot record the attempt: {error}", file=sys.stderr)
raise SystemExit(status)
'''


class VerifySshTargetsFixture(VerifyMacOffloadFixture):
    admission: AdmissionFixture  # pyright: ignore[reportUninitializedInstanceVariable]
    memory_events_enabled: bool = False
    memory_hook_observer: Callable[[CompletedMemoryHook], bool] | ReleaseMemoryAtWait = RELEASE_MEMORY_AT_WAIT

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        self.environment.update(fake_memory_readings(self.root))
        self.environment["TEST_SOURCE_REPO"] = str(REPOSITORY)
        # Restore the private wait identity after the gate removes inherited values.
        reader = Path(self.environment["LINT_CONFIG_READER"])
        with reader.open("a") as stream:
            _ = stream.write(r'''
fixture_wait_since=${BUILDLOG_MEM_WAIT_SINCE-}
fixture_wait_first_file=${BUILDLOG_MEM_WAIT_FIRST_FILE-}
source() {
    builtin source "$@"
    local status=$?
    if [[ "$1" == */memory_gate.sh ]]; then
        export BUILDLOG_MEM_WAIT_SINCE=$fixture_wait_since
        export BUILDLOG_MEM_WAIT_FIRST_FILE=$fixture_wait_first_file
        buildlog_memory_python() { "$HOME/.claude/scripts/lib/py" "$@"; }
        if [ -n "${TEST_ADMISSION_CLIENT:-}" ]; then BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_ADMISSION_CLIENT; fi
        unset -f source
    fi
    return "$status"
}
''')
        self.write_executable(self.runner, SSH_RUNNER)
        self.write_executable(self.bin_directory / "git", GIT_STUB.replace(
            "wanted=ls-files", 'wanted=remote\nif has "$@"; then printf "https://example.com/team/hana.git\\n"; exit 0; fi\nwanted=ls-files'))
        _ = (self.root / ".git/config").write_text('[remote "origin"]\nurl=https://example.com/team/hana.git\n')
        self.configure("mac,natedev,aws")
        install_fixed_cargo(self.bin_directory / "cargo")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.admission = caller_admission(self, self.root, self.environment)
        # The interval also bounds admission socket reads. Keep room for scheduling delay at high load.
        self.admission.configure(waiting_pass_seconds=1, running_pass_seconds=0.25)
        self.memory_events_enabled = False
        self.memory_hook_observer = RELEASE_MEMORY_AT_WAIT
        previous = self.admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ExecutionReport, wire.ClaimFailure)):
                targets = ({candidate.destination.target for candidate in request.constraints.candidates
                            if candidate.destination.provider != "local"} if isinstance(request, wire.PlacementRequest)
                           else {request.assignment.destination.target})
                event = {"request": json.loads(wire.encode_request(request)),
                         "replies": [json.loads(wire.encode_reply(reply)) for reply in replies],
                         "claims": {target: [held.record for held in caller_provider_controls(self, self.admission).claims(target)]
                                    for target in targets}}
                with (self.runner_state / "queue-events.jsonl").open("a") as output:
                    _ = output.write(json.dumps(event) + "\n")
            return replies

        _ = self.enterContext(mock.patch.object(self.admission.server, "handle_request", side_effect=observe))

    @contextmanager
    def spare_after_aws_refusal(self) -> Generator[None]:
        """Make the spare available after the first provider refuses the step."""
        controls = caller_provider_controls(self, self.admission)
        controls.set_host("spare", placement.TemporaryExclusion("starting"))
        previous = self.admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if (isinstance(request, wire.ClaimFailure)
                    or isinstance(request, wire.ExecutionReport)
                    and isinstance(request.outcome, placement.PrelaunchRefused)):
                if request.assignment.destination.target == "aws":
                    controls.set_host("spare", placement.AllowedHost())
            return replies

        with mock.patch.object(self.admission.server, "handle_request", side_effect=observe):
            yield

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
        observed_at = int(time.time())
        for target in ("aws", "spare"):
            book = ledger.CostLedger(Path(self.environment["HOME"]) / ".local/state/build-targets" / target)
            book.record_inventory(ledger.ResourceInventory(observed_at, (
                ledger.PricedResource("retained-storage", Decimal("1")),), "private priced inventory"))
            book.observe(ledger.InstanceObservation(observed_at, "stopped", "fake DescribeInstances"))

    def ssh_events(self) -> list[SshRunnerEvent]:
        path = self.runner_state / "ssh-events.jsonl"
        return [cast(SshRunnerEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def attempts(self) -> list[dict[str, object]]:
        rows = [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if cast(dict[str, object], json.loads(line)).get("kind") == "ssh_attempt"]
        return sorted(rows, key=lambda row: cast(str, row["at"]))

    def enable_mac(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\nlinux_only.case=\n")

    def release_memory_after_hook(self, observe: Callable[[CompletedMemoryHook], bool] | ReleaseMemoryAtWait = RELEASE_MEMORY_AT_WAIT,
                                  *, after_attempts: int = 0) -> None:
        _ = self.hold_memory()
        self.memory_events_enabled = True
        self.memory_hook_observer = observe
        directory = self.root / "observed-client"
        directory.mkdir(exist_ok=True)
        client = directory / "admission_client.py"
        self.write_executable(client, QUEUED_MEMORY_OBSERVER)
        self.environment.update(TEST_ADMISSION_CLIENT=str(client), TEST_OBSERVE_AFTER_ATTEMPTS=str(after_attempts))
        os.mkfifo(self.runner_state / "memory-hook-ack")

    @override
    def verify(self, *arguments: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if not self.memory_events_enabled:
            return super().verify(*arguments, extra=extra)
        acknowledgment = os.open(self.runner_state / "memory-hook-ack", os.O_RDWR)
        try:
            process = self.start_verify(*arguments, extra=extra)
            try:
                sequence = 0
                while True:
                    sequence += 1
                    event_path = self.runner_state / f"memory-hook-{sequence}"
                    self.wait_until(lambda: event_path.exists() or process.poll() is not None,
                                    str(event_path), (process,))
                    if not event_path.exists():
                        with self.admission.loop_turn():
                            authorized = any(isinstance(saved.permission, placement.PermissionConsumed)
                                             and saved.assignment.destination.provider != "local"
                                             for saved in self.admission.server.assignments.values())
                        if authorized and isinstance(self.memory_hook_observer, ReleaseMemoryAtWait):
                            return self.finish_verify(process)
                        self.wait_for_path(event_path, (process,))
                    event = cast(CompletedMemoryHook, json.loads(event_path.read_text()))
                    release = (True if isinstance(self.memory_hook_observer, ReleaseMemoryAtWait)
                               else self.memory_hook_observer(event))
                    if release and not event["left"]:
                        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 268435456 kB\n")
                    settled = release or event["left"]
                    if settled:
                        # Admission can wait after memory is free. Its later hooks do not wait for this test.
                        (self.runner_state / "memory-hook-settled").touch()
                    _ = os.write(acknowledgment, b"continue")
                    if settled:
                        break
                output, errors = process.communicate()
                return subprocess.CompletedProcess(process.args, process.wait(), output, errors)
            except BaseException:
                try:
                    self.capture_queue_state()
                finally:
                    self.stop_verify(process)
                raise
        finally:
            os.close(acknowledgment)

    def target_state(self, target: str) -> Path:
        return self.home / ".local/state/build-targets" / target

    def seed_controller(self, target: str = "aws") -> None:
        _ = (self.target_state(target) / "controller.json").write_text(
            json.dumps({"generation": "fixture-generation"}))

    def first_wait(self) -> tuple[Path, dict[str, str]]:
        path = self.root / "first-wait"
        _ = path.write_text("100.25\n")
        return path, {"BUILDLOG_MEM_WAIT_SINCE": "100.25", "BUILDLOG_MEM_WAIT_FIRST_FILE": str(path)}


RUN_BUILD_TARGETS = r'''
status = 0
try:
    runpy.run_path(str(source / "scripts/build_targets/build_targets.py"), run_name="__main__")
except SystemExit as result:
    status = result.code or 0
'''
