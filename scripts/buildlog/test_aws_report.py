"""Render AWS costs, wait estimates, and peer exchange evidence."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import time
from typing import cast, override
from unittest import mock
import unittest

from scripts.buildlog import aws_report, index
from scripts.buildlog import mac_outcome
from scripts.buildlog import report
from scripts.buildlog import record
from scripts.buildlog import store
from scripts.buildlog import sync
from scripts.buildlog.test_index import use_test_log

use_test_log()

from scripts.aws_target import ledger, measurement
from scripts.aws_target.ledger_test_support import numeric_settings
from scripts.aws_target.measurement_test_support import DAY, MIDNIGHT, NOW, MeasurementFixture, narrow, read_only_evidence


class AwsReportTests(MeasurementFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(
            registry.read_text()
            + "# whole: 0..10\nvault.lock_retries=5\n"
            + "# range: 0..5\nvault.lock_retry_delay_s=3\n")
        self.state: Path = self.root / ".local/state/build-targets/aws"
        self.seed_cost()
        self.text_setting("host", "aws-build")
        mac_settings = self.root / "mac_test.conf"
        source = mac_settings.read_text().splitlines()
        _ = mac_settings.write_text("\n".join(
            "host=fixture-mac" if line.startswith("host=") else line for line in source) + "\n")
        _ = cast(object, self.enterContext(mock.patch.object(store, "host_name", return_value="natedev")))
        _ = cast(object, self.enterContext(mock.patch.object(time, "time", return_value=float(NOW))))

    def render(self, day: str = DAY) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection, read_only_evidence(self, self.root):
            output = report.report(connection, day)
        return output

    def exchange(self, target: str, *, ok: bool, at: int) -> None:
        host = "aws-build" if target == "aws" else "fixture-mac"
        with mock.patch.object(time, "time", return_value=float(at)):
            sync.write_status(host, ok, target)

    def peer_line(self, output: str, target: str) -> str:
        return next(line for line in output.splitlines() if f"{target} (" in line)

    def test_read_only_evidence_rejects_changes_outside_index_wal_sidecars(self) -> None:
        _ = index.update()
        database = index.index_path()
        paths = (database, self.root / "evidence", Path(str(database) + index.INDEX_SIDECAR_SUFFIXES[0]),
                 self.root / (database.name + index.INDEX_SIDECAR_SUFFIXES[1]))
        for path in paths:
            with self.subTest(path=path.relative_to(self.root)), self.assertRaises(AssertionError):
                with read_only_evidence(self, self.root):
                    with path.open("ab") as handle:
                        _ = handle.write(b"x")

    def test_legacy_local_hosts_match_sql_and_python_classification(self) -> None:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace(
            "sync.hosts.natedev=natedev", "sync.hosts.natedev=natedev,local-alias")
            + "kind.worker=local\nplatform.worker=linux\nsync.hosts.worker=worker-host\n")
        _ = registry.write_text(registry.read_text().replace("targets=natedev,mac,aws", "targets=natedev,worker,mac,aws"))
        identity = record.local_step_identity()
        self.assertEqual(set(identity.hosts), {"natedev", "local-alias", "worker-host"})
        for number, host in enumerate((*identity.hosts, "aws-build")):
            self.write_records(self.local_step(f"legacy-{number}", 60, 10, host=host, target=""))
        _ = index.update()
        with closing(index.read_only()) as connection:
            selected = {row[0] for row in cast(list[tuple[str]], connection.execute(
                "SELECT host FROM steps WHERE " + mac_outcome.natedev_step_predicate()).fetchall())}
        expected = {host for host in (*identity.hosts, "aws-build") if record.is_local_step("", host, "verify")}
        self.assertEqual(selected, expected)
        self.assertEqual(selected, set(identity.hosts))
        for number, host in enumerate(identity.hosts):
            self.write_records(self.local_step(f"legacy-linux-{number}", 60, 10,
                                               host=host, target="", caller="verify-linux"))
        _ = index.update()
        with closing(index.read_only()) as connection:
            linux_selected = {row[0] for row in cast(list[tuple[str]], connection.execute(
                "SELECT host FROM steps WHERE caller='verify-linux' AND "
                + mac_outcome.natedev_step_predicate()).fetchall())}
        linux_expected = {host for host in identity.hosts if record.is_local_step("", host, "verify-linux")}
        self.assertEqual(linux_selected, linux_expected)
        self.assertEqual(linux_selected, set())

    def test_report_prints_each_mac_peer_state_once(self) -> None:
        self.exchange("mac", ok=True, at=NOW - 180)
        output = self.render()
        self.assertEqual(sum(line.startswith("- mac (fixture-mac):") for line in output.splitlines()), 1)
        self.assertNotIn("Mac rows as of", output)

    def test_report_renders_one_aws_section_without_target_registration(self) -> None:
        registry = self.root / "build_targets.conf"
        fixture = registry.read_text()
        shipped = (Path(__file__).resolve().parents[2] / 'config/build_targets.conf').read_text()
        for source, configuration in (('fixture', fixture), ('shipped', shipped)):
            with self.subTest(configuration=source):
                _ = registry.write_text(configuration)
                original = self.render()
                self.assertIn("### AWS\n", original)
                self.assertIn("AWS: up 2.00 h", original)
                self.assertEqual(sum(line.startswith("AWS: up ") for line in original.splitlines()), 1)
                self.assertLess(original.index("### Mac\n"), original.index("### AWS\n"))
                self.assertLess(original.index("### AWS\n"), original.rindex("Mac copies: none started."))
        account = self.root / 'account/aws_target.conf'
        account.parent.mkdir()
        _ = account.write_bytes(self.settings.read_bytes())
        numeric_settings(account, {'daily_limit_usd': '7'})
        self.costs({'daily_limit_usd': '1'})
        with self.subTest(settings='installed account'), mock.patch.dict(os.environ, {'AWS_TARGET_CONFIG': str(account)}):
            output = self.render()
            self.assertIn('limit $7.00', output)
            self.assertIn('AWS: up 2.00 h', output)
            self.assertNotIn('AWS measurement unavailable', output)

    def test_instrument_counts_use_targets_while_samples_use_local_hosts(self) -> None:
        self.write_records(
            self.local_step("local-target", 0, 10, host="elsewhere", slice="none"),
            self.local_step("remote-target", 0, 10, host="natedev", target="aws", slice="fallback"),
            self.call("local-kill", host="elsewhere", target="natedev", mem_kills=1, mem_kill_stopped=0),
            self.call("remote-kill", host="natedev", target="aws", mem_kills=1, mem_kill_stopped=0),
            {"kind": "sample", "v": 1, "host": "natedev", "at": store.utc_iso(NOW),
             "mem_used_bytes": 1024, "swap_used_bytes": 0, "sccache_in_service": False},
            {"kind": "sample", "v": 1, "host": "elsewhere", "at": store.utc_iso(NOW),
             "mem_used_bytes": 1024, "swap_used_bytes": 0, "sccache_in_service": False},
        )
        output = self.render()
        self.assertIn("unsliced steps: 1 never tried a scope", output)
        self.assertNotIn("fell back to a plain run", output)
        self.assertIn("memory kills: 1 calls passed after a re-run", output)
        self.assertIn("sccache outside its service: 1 min", output)

    def test_aws_call_with_empty_mac_outcome_stays_out_of_local_summaries(self) -> None:
        self.write_records(
            self.call("local", build_s=10, token_wait_s=60, cached=False),
            self.call("remote", target="aws", mac="", build_s=90000, token_wait_s=90000, cached=False),
        )
        _ = index.update()
        with closing(index.read_only()) as connection:
            output = "\n".join(report.test_builds_section(connection, DAY))
            token, _ = report.build_folder_waits(connection, DAY)
        self.assertIn("10.0 s", output)
        self.assertEqual([row[0] for row in token.waits], [60.0])

    def test_mixed_host_report_uses_the_required_lead_and_preserves_negative_run_savings(self) -> None:
        shipped = Path(__file__).resolve().parents[2] / 'config/build_targets.conf'
        _ = (self.root / 'build_targets.conf').write_bytes(shipped.read_bytes())
        self.comparison_rows()
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step(target=""))
        output = self.render()
        self.assertIn("### AWS", output)
        self.assertIn("AWS: up 2.00 h, 1 steps, about 0.08 h of queue wait saved for $2.00 ($24.00 per saved hour, estimated).", output)
        self.assertIn("Estimated run time saved: -0.01 h", output)
        self.assertNotIn("go-live", output)

    def test_missing_comparisons_show_unavailable_instead_of_a_saving(self) -> None:
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step())
        output = self.render()
        self.assertIn("queue wait saved unavailable", output)
        self.assertIn("cost per saved hour unavailable", output)
        self.assertIn("Estimated run time saved: unavailable", output)

    def test_missing_remote_duration_shows_an_unavailable_comparison(self) -> None:
        self.comparison_rows()
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step(duration_s=None))
        output = self.render()
        self.assertIn("AWS: up 2.00 h, 1 steps", output)
        self.assertIn("Estimated run time saved: unavailable", output)

    def test_empty_usage_handles_zero_saved_hours_without_a_ratio(self) -> None:
        output = self.render()
        self.assertIn("0 steps", output)
        self.assertIn("0.00 h of queue wait saved", output)
        self.assertIn("unavailable per saved hour", output)

    def test_gate_type_comparison_uses_finished_times_and_billed_days_in_the_window(self) -> None:
        numeric_settings(self.settings, {'gate_type_window_days': '2'})
        launches: list[dict[str, object]] = []
        for attempt, size, mode, at, box_s, gate_s in (
            ('merge-m', 'm7a.8xlarge', 'merge-gate', MIDNIGHT - 3600, 100, 60),
            ('target-m', 'm7a.8xlarge', 'targeted-test', MIDNIGHT - 3600, 200, 30),
            ('merge-c', 'c7a.8xlarge', 'merge-gate', MIDNIGHT - 3600, 300, 180),
            ('target-c', 'c7a.8xlarge', 'targeted-test', MIDNIGHT - 3600, 100, 90),
            ('unbilled-m', 'm7a.8xlarge', 'merge-gate', NOW, 600, 120),
            ('unbilled-c', 'c7a.8xlarge', 'targeted-test', NOW, 400, 150),
            ('old', 'old.8xlarge', 'merge-gate', MIDNIGHT - 86401, 100, 90),
            ('future', 'future.8xlarge', 'targeted-test', MIDNIGHT + 86400, 100, 90),
        ):
            launches.extend((
                {'time': at - box_s, 'attempt': attempt, 'instance_type': size, 'mode': mode, 'result': 'launched'},
                {'time': at, 'attempt': attempt, 'instance_type': size, 'mode': mode, 'result': 'finished', 'gate_s': gate_s}))
        launches.extend((
            {'time': NOW, 'instance_type': 'legacy.8xlarge', 'result': 'finished', 'gate_s': 10},
            {'time': NOW, 'attempt': 'orphan', 'instance_type': 'orphan.8xlarge', 'mode': 'merge-gate', 'result': 'finished', 'gate_s': 30},
            {'time': NOW - 60, 'attempt': 'legacy-launch', 'instance_type': 'orphan.8xlarge', 'result': 'launched'},
            {'time': NOW, 'attempt': 'legacy-launch', 'instance_type': 'orphan.8xlarge', 'mode': 'targeted-test', 'result': 'finished', 'gate_s': 30},
            {'time': NOW, 'instance_type': 'refused.8xlarge', 'mode': 'targeted-test', 'result': 'refused'}))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in reversed(launches)))
        charges = self.state / 'charges'
        charges.mkdir(exist_ok=True)
        usage = (ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.20'), Decimal('.25'), 'Hrs'),
                 ledger.UsageTypeCost('USW2-SpotUsage:m7a.8xlarge', Decimal('.10'), Decimal('.25'), 'Hrs'),
                 ledger.UsageTypeCost('USE2-SpotUsage:c7a.8xlarge', Decimal('.90'), Decimal('.5'), 'Hrs'),
                 ledger.UsageTypeCost('USE2-SpotUsage:excluded.8xlarge', Decimal('10'), Decimal('5'), 'Hrs'))
        for days_ago in (1, 2):
            at = MIDNIGHT - days_ago * 86400
            day = datetime.fromtimestamp(at, UTC).date().isoformat()
            reading = ledger.ChargeRead(day, NOW, at, at + 86400, Decimal('11.20'), Decimal(0), Decimal(0), usage)
            _ = (charges / f'{day}.json').write_text(json.dumps(ledger.charge_record(reading)))
        output = self.render()
        self.assertIn('| Machine type | Merge gates | Targeted tests | All runs |', output)
        self.assertIn('| m7a.8xlarge | $0.1000 per run (2) | $0.2000 per run (1) | $0.1500 per run (3) |', output)
        self.assertIn('| c7a.8xlarge | $0.6750 per run (1) | $0.2250 per run (2) | $0.4500 per run (3) |', output)
        self.assertIn('| All types | $0.3875 per run (3) | $0.2125 per run (3) | $0.3000 per run (6) |', output)
        self.assertIn('Median test time: c7a.8xlarge merge gates: 3m 0s; c7a.8xlarge targeted tests: 2m 0s; '
                      + 'm7a.8xlarge merge gates: 1m 30s; m7a.8xlarge targeted tests: 0m 30s.', output)
        self.assertIn('| 2026-01-15 | 1 | 2m 0s | 10m 0s | 1 | 2m 30s | 6m 40s | not billed yet |', output)
        self.assertIn('| 2026-01-14 | 2 | 2m 0s | 3m 20s | 2 | 1m 0s | 2m 30s | $0.3000 |', output)
        self.assertLess(output.index('| 2026-01-15 |'), output.index('| 2026-01-14 |'))
        for size in ('legacy.8xlarge', 'old.8xlarge', 'future.8xlarge', 'refused.8xlarge', 'orphan.8xlarge', 'excluded.8xlarge'):
            self.assertNotIn(f'| {size} |', output)
        self.assertIn('Source: cloud-gate-launches.jsonl and charges/<day>.json; UTC 2026-01-14 through 2026-01-15 (2 days)', output)
        self.assertIn('billed days only; 4 rows omitted with no kind or matching launch; Spot cost can include the older builder.', output)

    def test_gate_type_comparison_shows_unbilled_and_empty_cells_for_one_type(self) -> None:
        launches = [
            {'time': NOW - 120, 'attempt': 'one', 'instance_type': 'm7a.8xlarge', 'mode': 'targeted-test', 'result': 'launched'},
            {'time': NOW, 'attempt': 'one', 'instance_type': 'm7a.8xlarge', 'mode': 'targeted-test', 'result': 'finished', 'gate_s': 60}]
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in launches))
        lines = aws_report.gate_type_lines(self.state, DAY, 14)
        self.assertIn('| m7a.8xlarge | — | not billed yet (1) | not billed yet (1) |', lines)
        self.assertFalse(any(line.startswith('| All types |') for line in lines))
        self.assertIn('| 2026-01-15 | 0 | — | — | 1 | 1m 0s | 2m 0s | not billed yet |', lines)

    def test_gate_type_comparison_uses_shared_files_without_a_steps_query(self) -> None:
        with read_only_evidence(self, self.root):
            lines = aws_report.gate_type_lines(self.state, DAY, 14)
        self.assertIn('| Machine type | Merge gates | Targeted tests | All runs |', lines)
        self.assertTrue(any('UTC 2026-01-02 through 2026-01-15 (14 days)' in line for line in lines))

    def test_gate_type_comparison_with_malformed_evidence_preserves_the_report(self) -> None:
        self.write_records(self.local_step('comparison-evidence', 60, 10))
        launched = {'time': NOW - 60, 'attempt': 'missing-time', 'instance_type': 'm7a.8xlarge',
                    'mode': 'merge-gate', 'result': 'launched'}
        finished = {**launched, 'time': NOW, 'result': 'finished'}
        missing_time = json.dumps(launched) + '\n' + json.dumps(finished) + '\n'
        for filename, content, reason in (
            (f'charges/{DAY}.json', '{bad json\n', 'Expecting property name enclosed in double quotes'),
            ('cloud-gate-launches.jsonl', '{bad json\n', 'Expecting property name enclosed in double quotes'),
            ('cloud-gate-launches.jsonl', missing_time, "'gate_s'"),
        ):
            with self.subTest(filename=filename, reason=reason):
                path = self.state / filename
                path.parent.mkdir(exist_ok=True)
                _ = path.write_text(content)
                output = self.render()
                self.assertIn(f'Gate type comparison unavailable: {reason}', output)
                self.assertEqual(output.count('Gate type comparison unavailable:'), 1)
                self.assertIn('### AWS\n', output)
                self.assertIn('AWS: up ', output)
                self.assertIn('Mac copies: none started.', output)
                self.assertIn('### Summary: all', output)
                path.unlink()

    def test_report_prices_pending_requests_without_confirmed_uptime_and_keeps_resolved_costs(self) -> None:
        request = self.pending_request()
        output = self.render()
        self.assertIn('AWS: up 2.00 h', output)
        self.assertIn('for $22.00', output)
        saved = self.book().path.read_bytes()
        for resolution in (ledger.ComputeInstanceConfirmed(NOW, 'i-fixture'), ledger.ComputeResourcesAbsent(NOW)):
            with self.subTest(resolution=resolution):
                _ = self.book().path.write_bytes(saved)
                later = self.resolve_request(request, resolution)
                with mock.patch.object(time, 'time', return_value=float(later)):
                    output = self.render()
                self.assertIn('AWS: up 2.00 h', output)
                self.assertIn('for $22.00', output)

    def test_daily_charge_reconciliation_changes_cost_once_and_shows_its_difference(self) -> None:
        shipped = Path(__file__).resolve().parents[2] / 'config/build_targets.conf'
        _ = (self.root / 'build_targets.conf').write_bytes(shipped.read_bytes())
        at = NOW + 86400
        result = measurement.read_charges(self.settings, self.state, at, self.charge_fetch(Decimal(3)))
        reading = narrow(self, result, measurement.ChargeRead)
        launches = [
            {'time': MIDNIGHT, 'instance_type': 'm7a.8xlarge', 'result': 'launched'},
            {'time': NOW, 'instance_type': 'm7a.8xlarge', 'result': 'launched'},
            {'time': NOW, 'instance_type': 'm7a.4xlarge', 'result': 'launched'},
            {'time': NOW, 'instance_type': 'm7a.4xlarge', 'result': 'refused'},
            {'time': NOW, 'instance_type': 'm7a.8xlarge', 'result': 'finished', 'gate_s': 10},
            {'time': MIDNIGHT + 86400, 'instance_type': 'm7a.4xlarge', 'result': 'launched'},
        ]
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in launches))
        usage = (ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('0.06'), Decimal('.1'), 'Hrs'),
                 ledger.UsageTypeCost('USW2-SpotUsage:m7a.8xlarge', Decimal('0.02'), Decimal('.1'), 'Hrs'),
                 ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal('0.03'), Decimal('.1'), 'Hrs'),
                 ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', Decimal('2.29')),
                 ledger.UsageTypeCost('USE2-PublicIPv4:InUseAddress', Decimal('.1')),
                 ledger.UsageTypeCost('USE2-DataTransfer-Out-Bytes', Decimal('.2')),
                 ledger.UsageTypeCost('USE2-Lambda-Request', Decimal('.3')))
        for provisional in (False, True):
            with self.subTest(provisional=provisional):
                reading = ledger.ChargeRead(reading.day, at, reading.period_start, reading.period_end,
                                           Decimal(3), reading.estimated_usd, reading.query_usd, usage, provisional)
                _ = (self.state / 'charges' / f'{reading.day}.json').write_text(json.dumps(ledger.charge_record(reading)))
                with mock.patch.object(time, 'time', return_value=float(at)):
                    output = self.render()
                self.assertIn('for $3.00', output)
                state = 'provisional' if provisional else 'reported'
                read_at = datetime.fromtimestamp(at, UTC).isoformat()
                self.assertIn(f'AWS bill (account, UTC {DAY}, {state}): $3.00; '
                              + f'read at {read_at}; query cost $0.00.', output)
                self.assertIn('m7a.8xlarge: $0.0800; 2 launched boxes; $0.0400 per run (daily allocation)', output)
                self.assertIn('m7a.4xlarge: $0.0300; 1 launched boxes; $0.0300 per run (daily allocation)', output)
                self.assertIn('older Spot builder', output)
                for group, amount, share in (
                    ('gate compute at 32 CPU', '.0800', '2.7'), ('compute at 16 CPU', '.0300', '1.0'),
                    ('storage', '2.2900', '76.3'), ('public IPv4', '.1000', '3.3'),
                    ('data transfer', '.2000', '6.7'), ('other', '.3000', '10.0')):
                    self.assertIn(f'| {group} | ${Decimal(amount):.4f} | {share}% |', output)
                self.assertNotIn("Today's spend", output)
                with mock.patch.object(time, 'time', return_value=float(at)):
                    current = self.render('2026-01-16')
                self.assertIn("Today's spend (UTC 2026-01-16): $2.89; limit $20.00; 1 run; estimated from the newest bill.", current)
                self.assertIn('Spend (UTC 2026-01-15): $3.00; limit $20.00; 3 runs; AWS bill.', output)
        for amount in (Decimal('-0.0000036'), Decimal('0.0000036')):
            with self.subTest(amount=amount):
                dust = ledger.ChargeRead(reading.day, at, reading.period_start, reading.period_end,
                    amount, reading.estimated_usd, reading.query_usd, (ledger.UsageTypeCost('storage', amount),))
                _ = (self.state / 'charges' / f'{dust.day}.json').write_text(json.dumps(ledger.charge_record(dust)))
                with mock.patch.object(time, 'time', return_value=float(at)):
                    output = self.render('2026-01-16')
                self.assertNotIn(f'AWS bill (account, UTC {DAY},', output)
                self.assertIn('estimated from configured prices', output)

    def test_aws_failure_preserves_mac_success_without_an_aws_data_sync_line(self) -> None:
        self.exchange("mac", ok=True, at=NOW - 180)
        self.exchange("aws", ok=True, at=NOW - 120)
        self.exchange("aws", ok=False, at=NOW - 60)
        output = self.render()
        aws, mac = self.peer_line(output, "aws"), self.peer_line(output, "mac")
        self.assertIn("failed", aws)
        self.assertIn("last successful exchange", aws)
        self.assertIn("latest failure", aws)
        self.assertIn("buildlog sync", aws)
        self.assertIn("current", mac)
        self.assertIn("latest failure none", mac)
        self.assertNotIn("AWS report data synchronized at", output)
        self.assertNotIn("AWS report data never synchronized", output)

    def test_mac_failure_does_not_mark_the_aws_exchange_as_failed(self) -> None:
        self.exchange("aws", ok=True, at=NOW - 120)
        self.exchange("mac", ok=True, at=NOW - 180)
        self.exchange("mac", ok=False, at=NOW - 60)
        output = self.render()
        self.assertIn("failed", self.peer_line(output, "mac"))
        self.assertIn("current", self.peer_line(output, "aws"))
        self.assertIn("latest failure none", self.peer_line(output, "aws"))

    def test_first_failure_and_never_synchronized_peers_show_the_recovery_action(self) -> None:
        self.exchange("aws", ok=False, at=NOW - 60)
        output = self.render()
        aws = self.peer_line(output, "aws")
        self.assertIn("last successful exchange never", aws)
        self.assertIn("latest failure", aws)
        self.assertIn("buildlog sync", aws)
        self.assertIn("never synchronized; run buildlog sync", self.peer_line(output, "mac"))
        self.assertNotIn("AWS report data never synchronized", output)

    def test_global_pause_names_resume_for_each_active_peer(self) -> None:
        self.exchange("aws", ok=True, at=NOW - 120)
        paused = self.root / "buildlog" / store.SYNC_PAUSED_NAME
        _ = paused.write_text(json.dumps({"since": store.utc_iso(NOW - 60), "why": "fixture maintenance"}))
        output = self.render()
        for target in ("mac", "aws"):
            line = self.peer_line(output, target)
            self.assertIn("globally paused", line)
            self.assertIn("fixture maintenance", line)
            self.assertIn("buildlog sync resume", line)

    def test_recovery_keeps_the_latest_failure_and_the_legacy_mac_status(self) -> None:
        self.exchange("mac", ok=True, at=NOW - 180)
        self.exchange("aws", ok=False, at=NOW - 120)
        self.exchange("aws", ok=True, at=NOW - 60)
        output = self.render()
        line = self.peer_line(output, "aws")
        self.assertIn("recovered", line)
        self.assertNotIn("latest failure none", line)
        expected = sync.sync_time(store.utc_iso(NOW - 120), datetime.now())
        self.assertIn(f"latest failure {expected}", line)
        legacy = sync.read_status()
        legacy = narrow(self, legacy, sync.SyncStatus)
        self.assertEqual(legacy.peer, "fixture-mac")
        self.assertTrue(legacy.ok)


if __name__ == "__main__":
    _ = unittest.main()
