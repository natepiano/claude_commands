"""Check SSH startup and result boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from contextlib import redirect_stdout
import io
from typing import override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct import verify_doc_test_support as doc_support
from scripts.build_targets import test_ssh_eligibility as eligibility_support
from scripts.build_targets import build_targets
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import mac_test, claim_state

from scripts.lib import process_start as process_identity


class VerifySshTargetsTests(VerifySshTargetsFixture):
    def test_local_only_request_never_reserves_or_runs_linux_work(self) -> None:
        self.configure("linux,natedev")
        result = self.verify("test", "sample", "--local", "requires local files", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.attempts(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_probe_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target(self) -> None:
        self.check_busy_startup("probe-refusal")

    def test_run_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target(self) -> None:
        self.check_busy_startup("run-refusal")

    def test_after_probe_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target(self) -> None:
        self.check_busy_startup("after-probe")

    def check_busy_startup(self, mode: str) -> None:
        self.configure("linux,spare,natedev")
        with self.spare_after_linux_refusal():
            result = self.verify("test", "sample", "--no-cache", extra={
                "TEST_SSH_MODES": json.dumps({"linux": mode}), "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.ssh_events()
        self.assertEqual([event["target"] for event in events], ["linux", "spare"],
                         json.dumps(events) + result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        self.assertNotIn("release", events[0]["actions"])
        self.assertEqual(events[0]["queued_at"], "100.25")
        self.assertEqual(events[1]["queued_at"], "100.25")
        self.assertTrue((self.runner_state / "linux.marker").exists())
        state = self.target_state("linux")
        self.assertTrue(list((state / "claims").glob("**/*.json")))
        self.assertFalse((state / "unreachable_until").exists())
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        attempts = self.attempts()
        self.assertEqual([attempt["target"] for attempt in attempts], ["linux", "spare"])
        self.assertEqual((attempts[0]["outcome"], attempts[0]["completion"], attempts[0]["cleanup"]),
                         ("declined", "missing", "unconfirmed"))
        self.assertIsNone(attempts[0]["unresolved_count"])


class SshReservationTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: eligibility_support.SshEligibilityTests = eligibility_support.SshEligibilityTests()

    @override
    def setUp(self) -> None:
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        _ = write_config_fixture(self.fixture.root / "linux.conf", "offload=on\nslots=1\nslots.hana=1\n")

    def reserve(self, pid: int) -> tuple[int, Path]:
        path = self.fixture.root / f"reservation-{pid}"
        args = build_targets.CliArguments()
        args.repo_root, args.package, args.kind = str(self.fixture.root), "linux-part", "test"
        args.pid, args.reservation_file = pid, str(path)
        args.claim_identity_file = str(path.with_suffix(".identity"))
        return build_targets.command_reserve(args), path

    def test_retry_recovers_its_claim_when_the_reservation_file_was_not_published(self) -> None:
        state = mac_test.state_paths("linux")
        args = mac_test.CliArguments()
        args.target, args.repo_root, args.pid = "linux", str(self.fixture.root), 71
        args.settings, args.package, args.verb = str(self.fixture.root / "linux.conf"), "linux-part", "nextest"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim_wait(args, state), 0)
        claim, = mac_test.read_claims(state)
        destination = self.fixture.root / "reservation-71"
        self.assertFalse(destination.exists())
        status, answer = self.reserve(71)
        self.assertEqual(status, 0)
        self.assertEqual(answer.read_text().split("\t")[0], "linux")
        recovered, = mac_test.read_claims(state)
        self.assertEqual(recovered.path, claim.path)
        self.assertEqual(recovered.record, claim.record)
        self.assertEqual(self.reserve(72)[0], 1)

    def test_wait_release_keeps_a_claim_from_an_earlier_process_with_the_same_pid(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        state = mac_test.state_paths("linux")
        claim, = mac_test.read_claims(state)
        identity = claim_state.claim_identity(claim.record)
        with mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("reused-start")):
            self.assertEqual(build_targets.command_release_wait(71, identity=identity, session=NoOwningSession()), 0)
        self.assertTrue(claim.path.exists())
        self.assertEqual(mac_test.read_claims(state)[0].record, claim.record)
        self.assertEqual(build_targets.command_release_wait(71, identity=identity, session=NoOwningSession()), 0)
        self.assertFalse(claim.path.exists())

    def test_reservation_race_keeps_the_loser_queued_without_an_answer(self) -> None:
        status, first = self.reserve(71)
        self.assertEqual(status, 0)
        self.assertEqual(first.read_text().split("\t")[0], "linux")
        with mock.patch.object(mac_test, "free_now", return_value="free"):
            status, losing = self.reserve(72)
        self.assertEqual(status, 1)
        self.assertFalse(losing.exists())
        claims = mac_test.read_claims(mac_test.state_paths("linux"))
        self.assertEqual([claim.record["pid"] for claim in claims], [71])
        self.assertNotIn("remote_host", claims[0].record)

    def test_runner_adopts_the_reserved_slot_without_an_unclaimed_interval(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        paths = mac_test.state_paths("linux")
        original, = mac_test.read_claims(paths)
        identity = claim_state.claim_identity(original.record)
        args = mac_test.CliArguments()
        args.target, args.worktree, args.what = "linux", str(self.fixture.root), "check hana"
        args.pid, args.from_pid, args.remote = 91, 71, "fake-linux"
        args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
        unlink = Path.unlink

        def prevent_gap(path: Path, missing_ok: bool = False) -> None:
            self.assertNotEqual(path, original.path, "The reservation must remain held during transfer")
            unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=prevent_gap), redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False, session=NoOwningSession()), 0)
        transferred, = mac_test.read_claims(paths)
        self.assertEqual(transferred.path, original.path)
        self.assertEqual(transferred.scope, original.scope)
        self.assertEqual(transferred.record["pid"], 91)
        self.assertEqual(transferred.record.get("remote_host"), "fake-linux")
        self.assertEqual(build_targets.command_release_wait(71, identity=identity, session=NoOwningSession()), 0)
        self.assertEqual(build_targets.command_release_wait(91, identity=identity, session=NoOwningSession()), 0)
        self.assertTrue(original.path.exists())
        self.assertEqual(self.reserve(72)[0], 1)

    def test_exit_releases_only_an_untransferred_local_reservation(self) -> None:
        self.assertEqual(self.reserve(71)[0], 0)
        claim, = mac_test.read_claims(mac_test.state_paths("linux"))
        self.assertEqual(build_targets.command_release_wait(71,
            identity=claim_state.claim_identity(claim.record), session=NoOwningSession()), 0)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("linux")), ())
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
                                          target="linux", evidence=evidence)
        self.assertEqual(status, 0)
        fields = dict(line.split("=", 1) for line in destination.read_text().splitlines())
        self.assertEqual(fields["outcome"], "passed")
        self.assertEqual(fields["target"], "linux")
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
    @override
    def start_admission(self) -> None:
        super().start_admission()
        admission = caller_admission(self, self.root, self.environment)
        _ = admission.occupy_host()
        with admission.loop_turn():
            admission.free(0)

    def test_linux_doc_judges_the_staged_tree_without_a_local_hold(self) -> None:
        self.linux_doc_case(pinned=False)

    def test_pinned_linux_doc_keeps_the_staged_tree_and_skips_local_admission(self) -> None:
        self.linux_doc_case(pinned=True)

    def linux_doc_case(self, *, pinned: bool) -> None:
        self.enable_held_mac_doc()
        for holder in Path(self.environment["BUILD_HOLD_DIR"]).iterdir():
            holder.unlink()
        self.environment["TEST_SOURCE_REPO"] = str(Path(__file__).resolve().parents[2])
        targets = Path(self.environment["BUILD_TARGETS_SCRIPT"])
        self.executable(targets, r'''
import os
from pathlib import Path
import sys
import runpy
words = sys.argv[1:]
settings = str(Path(os.environ["CONF_DIR"]) / "linux_target.conf")
if words[0] in ("constraints", "submitting-session", "source-key", "assigned-info", "reserve", "recover-reservation", "release-wait"):
    source = Path(os.environ["TEST_SOURCE_REPO"])
    runpy.run_path(str(source / "scripts/build_targets/build_targets.py"), run_name="__main__")
    raise SystemExit(0)
if words[0] == "eligible":
    print("any_target")
    print("linux\teligible\t-\t" + settings + "\tlinux")
elif words[0] == "reserve":
    Path(words[words.index("--reservation-file") + 1]).write_text("linux\t" + settings + "\n")
    Path(words[words.index("--claim-identity-file") + 1]).write_text("doc-claim\tdoc-episode\tdoc-attempt\t\n")
elif words[0] in ("register-local", "check-local") and os.environ.get("TEST_PINNED_DOC") == "1":
    raise AssertionError("pinned doc requested local admission")
elif words[0] not in ("release-wait", "register-local", "check-local"):
    raise AssertionError(words)
''')
        config = Path(self.environment["CONF_DIR"]) / "build_targets.conf"
        _ = config.write_text("targets=linux,natedev\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux_target.conf\n"
                              + "kind.natedev=local\nplatform.natedev=linux\n")
        _ = write_config_fixture(config.parent / "linux_target.conf", "offload=on\nkinds=doc\n", template="mac_test")
        runner = Path(self.environment["VERIFY_MAC_RUNNER"])
        self.environment["TEST_LINUX_DOC_RUN"] = str(self.root / "linux-doc-run")
        self.environment["TEST_PINNED_DOC"] = "1" if pinned else "0"
        runner_source = runner.read_text().replace('"mac=" + result', '"outcome=" + result + "\\ntarget=linux"')
        runner_source = runner_source.replace('words = sys.argv[1:]', '''words = sys.argv[1:]
assert words[words.index("--target") + 1] == "linux"
Path(os.environ["TEST_LINUX_DOC_RUN"]).touch()''')
        self.executable(runner, runner_source)
        staged = self.repo / "alpha/src/lib.rs"
        _ = staged.write_text("pub fn staged() {}\n")
        _ = self.git("add", "alpha/src/lib.rs")
        _ = staged.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("private fixture")
        original_index = (self.repo / ".git/index").read_bytes()

        result = self.verify("--target", "linux") if pinned else self.verify()

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
