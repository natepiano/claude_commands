"""Render AWS capacity, spending, and cleanup from local evidence."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import TYPE_CHECKING, cast, override

if TYPE_CHECKING:
    from ..lib import process_start as process_identity
    from ..lib.process_start_test_support import process_reader
else:
    from scripts.lib import process_start as process_identity
    from scripts.lib.process_start_test_support import process_reader

from unittest import mock
import unittest

from scripts.build_targets import build_targets
from scripts.mac_test import mac_test, offload

if TYPE_CHECKING:
    from ..aws_target import aws_target, ledger, config as aws_config
    from ..aws_target.control_test_support import AwsReplies
    from ..aws_target.measurement_test_support import MeasurementFixture, NOW, REPOSITORY, private_history, read_only_evidence
    from ..message import roster
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.aws_target import aws_target, ledger, config as aws_config
    from scripts.aws_target.control_test_support import AwsReplies
    from scripts.aws_target.measurement_test_support import MeasurementFixture, NOW, REPOSITORY, private_history, read_only_evidence
    from scripts.message import roster


class AwsStatusTests(MeasurementFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.text_setting("host", "aws-build")
        self.costs({"slots": "2", "slots.hana": "2", "slots.other": "2"})
        self.state: Path = mac_test.state_paths("aws").directory
        self.seed_cost()
        _ = cast(object, self.enterContext(mock.patch.object(roster, "unit_labels_for", return_value={})))
        _ = cast(object, self.enterContext(mock.patch.object(subprocess, "run", side_effect=private_history)))
        _ = cast(object, self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("status must not start commands"))))
        _ = cast(object, self.enterContext(mock.patch.object(time, "time", return_value=float(NOW))))
        _ = cast(object, self.enterContext(mock.patch.object(process_identity, "process_start", side_effect=process_reader(self.process_start))))
        self.controller("running")

    @staticmethod
    def process_start(pid: int) -> str:
        return "live-start" if pid == 101 else ""

    def controller(self, state: str, reason: str = "") -> None:
        _ = (self.state / "controller.json").write_text(json.dumps({
            "state": state, "at": NOW - 60, "generation": "i-fixture:boot-a", "reason": reason,
        }))

    def claim(self, name: str, slot: int, *, pid: int = 101, pending: bool = False) -> Path:
        paths = mac_test.state_paths("aws")
        identity = REPOSITORY if name == "hana" else "github.com/example/other"
        path = mac_test.claim_path(paths, mac_test.RepositoryScope(name, identity, slot))
        row: dict[str, object] = {
            "pid": pid, "proc_start": "live-start", "what": f"nextest {name}",
            "worktree": str(self.root / name), "since": "2026-01-15T10:00:00+00:00",
            "remote_host": "aws-build", "repo_name": name, "repo_key": identity,
            "slot": slot, "instance_generation": "i-fixture:boot-a",
        }
        if pending:
            row["cleanup_pending"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(row))
        return path

    def render(self, *, invalid_key: str = "") -> str:
        output, errors = io.StringIO(), io.StringIO()
        with read_only_evidence(self, self.root, allow_private_history=True), redirect_stdout(output), redirect_stderr(errors), \
             mock.patch.object(offload, "probe_reachability", side_effect=AssertionError("ordinary status cannot probe")):
            code = build_targets.command_status()
        self.assertEqual(code, 0, errors.getvalue())
        if errors.getvalue():
            self.assertTrue(invalid_key, errors.getvalue())
            self.assertIn(invalid_key, errors.getvalue())
        return output.getvalue()

    def test_incomplete_down_shows_retained_storage_and_keeps_counting_it(self) -> None:
        _ = (self.state / 'down.json').write_text(json.dumps({'volume': 'vol-retained', 'budget': 'retained-budget'}))
        book = self.book()
        resources = tuple(ledger.PricedResource(name, Decimal(value)) for name, value in (
            ('retained_storage_usd_per_month', '40'), ('gp3_iops_usd_per_month', '15'), ('gp3_throughput_usd_per_month', '15')))
        book.record_inventory(ledger.ResourceInventory(NOW, resources, 'retained volume during removal'))
        output = self.render()
        self.assertIn('down incomplete: vol-retained, retained-budget; storage $2.26/day; run deploy.py down --approve-destroy', output)
        before = ledger.costs_from_evidence(book.read_snapshot(), aws_config.read_settings(self.settings), NOW, current_window=False)
        after = ledger.costs_from_evidence(book.read_snapshot(), aws_config.read_settings(self.settings), NOW + 3600, current_window=False)
        self.assertGreater(after.monthly.usd, before.monthly.usd)
        self.assertEqual(book.read_snapshot().inventories[-1].resources, resources)

    def test_corrupt_report_files_do_not_make_status_unreadable(self) -> None:
        charges = self.state / "charges"
        charges.mkdir()
        for path in (charges / "2026-01-14.json", self.state / "go-live.json"):
            with self.subTest(path=path.name):
                _ = path.write_text("{broken")
                output = self.render()
                self.assertIn("aws: running", output)
                self.assertIn("month $2.00 / $400.00", output)
                self.assertNotIn("state unreadable", output)
                self.assertEqual(path.read_text(), "{broken")
                path.unlink()

    def test_status_prices_pending_requests_and_keeps_the_guard_refusal_after_resolution(self) -> None:
        request = self.pending_request()
        pending = self.render()
        self.assertIn('month $22.00 / $400.00', pending)
        self.assertIn('spending refused', pending)
        saved = self.book().path.read_bytes()
        for resolution in (ledger.ComputeInstanceConfirmed(NOW, 'i-fixture'), ledger.ComputeResourcesAbsent(NOW)):
            with self.subTest(resolution=resolution):
                _ = self.book().path.write_bytes(saved)
                later = self.resolve_request(request, resolution)
                with mock.patch.object(time, 'time', return_value=float(later)):
                    output = self.render()
                self.assertIn('month $22.00 / $400.00', output)
                self.assertIn('spending refused', output)

    def test_invalid_monthly_limit_shows_unavailable_cap_and_refuses_spending(self) -> None:
        self.commit_settings()
        self.replace("monthly_limit_usd", "broken")
        decision = self.book().guard(self.settings, "admission", NOW, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingUnavailable)
        output = self.render(invalid_key="monthly_limit_usd")
        self.assertIn("/ unavailable", output)
        self.assertIn("spending unavailable", output)

    def test_missing_controller_evidence_prints_the_recovery_action_once(self) -> None:
        (self.state / "controller.json").unlink()
        _ = self.claim("hana", 1, pending=True)
        output = self.render()
        self.assertIn("aws: unavailable", output)
        self.assertEqual(output.count("aws_target.py tick"), 1)

    def test_aws_hold_and_malformed_block_use_one_status_format_with_or_without_probe(self) -> None:
        block = mac_test.state_paths("aws").block
        block.parent.mkdir(parents=True, exist_ok=True)
        _ = block.write_text(json.dumps({"holder": "hana", "session": "hana", "for": "render checks",
                                       "state": "active", "since": "2026-01-15T10:00:00+00:00"}))
        outputs: list[str] = []
        for probe in (False, True):
            with self.subTest(probe=probe, block="held"):
                output = io.StringIO()
                with redirect_stdout(output), mock.patch.object(offload, "probe_reachability"):
                    self.assertEqual(build_targets.command_status(probe=probe), 0)
                line = next(line for line in output.getvalue().splitlines() if line.startswith("aws:"))
                outputs.append(line)
                self.assertIn("held by hana", line)
                self.assertIn("render checks", line)
        self.assertEqual(outputs[0], outputs[1])
        _ = block.write_text('{"holder": 42}')
        for probe in (False, True):
            with self.subTest(probe=probe, block="malformed"):
                output = io.StringIO()
                with redirect_stdout(output), mock.patch.object(offload, "probe_reachability"):
                    self.assertEqual(build_targets.command_status(probe=probe), 0)
                self.assertIn("aws: state unreadable", output.getvalue())
        self.assertEqual(block.read_text(), '{"holder": 42}')

    def test_repeated_pending_ticks_preserve_the_original_start_time_in_status(self) -> None:
        _ = (self.state / "deployment.json").write_text(json.dumps({
            "region": "us-east-1", "outputs": {"InstanceId": "i-fixture"}}))
        replies = AwsReplies()
        now = NOW + 1
        controller = aws_target.Controller(self.settings, self.state, self.root / "admission",
                                           execute=replies, clock=lambda: float(now),
                                           notify=lambda _event: True, publish=lambda _envelope: True)
        self.assertEqual(controller.start().state, "starting")
        for seconds in (60, 120):
            now = NOW + 1 + seconds
            self.assertEqual(controller.tick().state, "starting")
        with mock.patch.object(time, "time", return_value=float(now)):
            output = self.render()
        self.assertIn("starting since 12:00", output)
        self.assertIn("evidence 12:02", output)

    def test_slots_are_per_repository_and_retained_claims_are_not_active_work(self) -> None:
        _ = self.claim("hana", 1)
        _ = self.claim("hana", 2, pid=102)
        _ = self.claim("other", 1, pending=True)
        output = self.render()
        self.assertIn("hana: 2/2 slots; 1 active, 1 retained pending cleanup", output)
        self.assertIn("other: 1/2 slots; 0 active, 1 retained pending cleanup", output)
        self.assertIn("aws: running", output)
        self.assertIn("month $2.00 / $400.00", output)
        self.assertIn("recovery:", output)
        self.assertIn("aws_target.py tick", output)

    def test_confirmed_result_and_uncertain_cleanup_render_separate_from_capacity(self) -> None:
        _ = self.claim("hana", 1, pid=102, pending=True)
        self.write_records(self.attempt(outcome="failed", cleanup="unconfirmed", unresolved_count=3))
        output = self.render()
        self.assertIn("hana: 1/2 slots; 0 active, 1 retained pending cleanup", output)
        self.assertIn("slot 1: failed; completion confirmed; cleanup unconfirmed; unresolved 3", output)
        self.assertIn("evidence 12:00", output)
        self.assertIn("recovery:", output)

    def test_recovered_capacity_keeps_the_confirmed_result_and_removes_recovery_advice(self) -> None:
        path = self.claim("hana", 1, pid=102, pending=True)
        self.write_records(self.attempt(cleanup="unconfirmed", unresolved_count=2))
        self.assertIn("retained pending cleanup", self.render())
        path.unlink()
        proof = self.state / "release-evidence" / "stopped.json"
        proof.parent.mkdir()
        _ = proof.write_text(json.dumps({"generation": "i-fixture:boot-a", "state": "stopped", "observed_at": NOW}))
        self.controller("stopped", "manual_stop")
        output = self.render()
        self.assertIn("aws: stopped", output)
        self.assertIn("completion confirmed; cleanup unconfirmed; unresolved 2", output)
        self.assertNotIn("retained pending cleanup", output)
        self.assertNotIn("  recovery:", output)

    def test_controller_states_show_local_evidence_and_start_time(self) -> None:
        for state, reason in (("stopped", "idle"), ("starting", "queue threshold"),
                              ("draining", "interruption"), ("refused", "monthly limit")):
            with self.subTest(state=state):
                self.controller(state, reason)
                output = self.render()
                self.assertIn(f"aws: {state}", output)
                self.assertIn("evidence 11:59", output)
                self.assertIn(reason, output)
                if state == "starting":
                    self.assertIn("starting since 11:59", output)

    def test_refusal_renders_invalid_live_limits_and_pending_notice_actions(self) -> None:
        book = self.book()
        book.reconcile(ledger.CostReconciliation(NOW, NOW - 3600, NOW, Decimal(20), "fixture charge"))
        _ = book.guard(self.settings, "admission", NOW, lambda _event: False)
        output = self.render()
        self.assertIn("spending refused", output)
        self.assertIn("decision 12:00", output)
        self.assertIn("pending", output)
        self.assertIn("retry", output)
        self.replace("daily_limit_usd", "broken")
        _ = book.guard(self.settings, "admission", NOW, lambda _event: False)
        output = self.render(invalid_key="daily_limit_usd")
        self.assertIn("spending unavailable", output)
        self.assertIn("daily_limit_usd", output)

    def test_disabled_and_missing_decisions_have_distinct_spending_states(self) -> None:
        self.assertIn("spending allowed", self.render())
        document = cast(dict[str, object], json.loads(self.book().path.read_text()))
        document["decision"] = {}
        _ = self.book().path.write_text(json.dumps(document))
        self.assertIn("spending unavailable", self.render())
        self.text_setting("offload", "off")
        self.assertIn("spending disabled", self.render())

    def test_delivered_limit_notice_survives_a_status_read(self) -> None:
        book = self.book()
        book.reconcile(ledger.CostReconciliation(NOW, NOW - 3600, NOW, Decimal(20), "fixture charge"))
        _ = book.guard(self.settings, "admission", NOW, lambda _event: True)
        output = self.render()
        self.assertIn("spending refused", output)
        self.assertIn("delivered", output)
        self.assertNotIn("retry after", output)

    def test_latest_result_keeps_confirmed_cleanup_and_names_missing_process_counts(self) -> None:
        self.write_records(
            self.attempt("latest", cleanup="confirmed", unresolved_count=None),
            self.attempt("older", evidence_at=NOW - 1, cleanup="unconfirmed", unresolved_count=9),
        )
        output = self.render()
        self.assertIn("completion confirmed; cleanup confirmed; unresolved unavailable", output)
        self.assertNotIn("unresolved 9", output)

    def test_malformed_claim_reports_unreadable_state_without_deleting_evidence(self) -> None:
        path = self.claim("hana", 1)
        _ = path.write_text('{"pid": "broken"}')
        self.assertIn("aws: state unreadable", self.render())
        self.assertEqual(path.read_text(), '{"pid": "broken"}')

    def test_aws_evidence_times_use_the_user_zone(self) -> None:
        with mock.patch.dict("os.environ", {"USER_ZONE": "America/Los_Angeles"}):
            output = self.render()
        self.assertIn("evidence 03:59 PST", output)
        self.assertIn("decision 04:00 PST", output)


if __name__ == "__main__":
    _ = unittest.main()
