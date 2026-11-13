"""Test local gate evidence without locks, writes, or provider requests."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast
from unittest import mock

from scripts.aws_target import aws_target, ledger, open_gates
from scripts.aws_target.ledger_test_support import CostFixture


class OpenGateTests(CostFixture):
    def save(self, states: tuple[str, ...], *, pending: bool = False) -> Path:
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(1, (ledger.PricedResource('fixture-storage', Decimal('0')),), 'fixture inventory'))
        if pending:
            book.record_compute_request(ledger.ComputeRequest(1, 'fixture-stack', 'fixture-region', Decimal('1')))
        document = ledger.mapping(cast(object, json.loads(book.path.read_text())))
        document['gate_reservations'] = [dict(attempt=f'fixture-{number}', at=1, expires=2,
            usd_per_hour='1', allowance_usd='0', instance_type='fixture.xlarge', region='fixture-region',
            mode='targeted', state={'kind': kind, 'at': 2, 'instance_id': 'fixture-private-instance'})
            for number, kind in enumerate(states)]
        _ = book.path.write_text(json.dumps(document))
        os.utime(book.path, (1, 1))
        return book.path

    def test_open_states_remain_open_after_expiry_and_never_print_instance_ids(self) -> None:
        path = self.save(('pending', 'running', 'absent', 'terminated'), pending=True)
        before = {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in self.state.iterdir()}
        with (mock.patch.object(subprocess, 'run', side_effect=AssertionError('No external command')),
              mock.patch.object(ledger.CostLedger, '_locked', side_effect=AssertionError('No ledger lock')),
              mock.patch.object(ledger, 'write_text_atomic', side_effect=AssertionError('No write')),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(open_gates.report(self.state, 100), 1)
        text = output.getvalue()
        self.assertIn(str(path), text)
        self.assertIn('File written: ' + datetime.fromtimestamp(1, UTC).isoformat() + ' (age 99s)', text)
        for name in ('GateLaunchPending', 'GateRunning', 'GateAbsent', 'GateTerminated'):
            self.assertIn(f'{name}: 1', text)
        self.assertIn('Open entries: 2', text)
        self.assertIn('Pending compute requests: 1', text)
        self.assertIn('attempt=fixture-0 state=GateLaunchPending reserved=', text)
        self.assertIn('instance_type=fixture.xlarge region=fixture-region mode=targeted', text)
        self.assertNotIn('fixture-private-instance', text)
        self.assertNotIn('attempt=fixture-2', text)
        self.assertNotIn('attempt=fixture-3', text)
        self.assertEqual(before, {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in self.state.iterdir()})

    def test_closed_and_empty_ledgers_return_zero(self) -> None:
        for states in ((), ('absent', 'terminated')):
            with self.subTest(states=states):
                _ = self.save(states)
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(open_gates.report(self.state, 100), 0)
                self.assertIn('Open entries: 0', output.getvalue())
                self.assertIn('Pending compute requests: 0', output.getvalue())

    def test_missing_evidence_returns_two_without_creating_a_directory(self) -> None:
        directory = self.state / 'missing'
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(open_gates.report(directory, 100), 2)
        self.assertFalse(directory.exists())

    def test_cli_uses_the_read_only_report(self) -> None:
        with (mock.patch.object(sys, 'argv', ['aws_target.py', 'open-gates', '--directory', str(self.state)]),
              mock.patch.object(open_gates, 'report', return_value=1) as report):
            self.assertEqual(aws_target.main(), 1)
        self.assertEqual(report.call_args.args[0], self.state)
