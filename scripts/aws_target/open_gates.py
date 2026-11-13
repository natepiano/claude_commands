"""Show open gate reservations without changing ledger evidence."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
import sys

from scripts.aws_target import ledger


def report(directory: Path, now: int) -> int:
    """Print local evidence and return one when a gate reservation is open."""
    book = ledger.CostLedger(directory)
    print(f'Ledger: {book.path}')
    try:
        written = book.path.stat().st_mtime
        state = book.read_snapshot()
        pending = book.pending_compute_requests()
    except (OSError, ledger.LedgerUnavailable) as error:
        print(f'Ledger unavailable: {type(error).__name__}', file=sys.stderr)
        return 2
    print(f'File written: {datetime.fromtimestamp(written, UTC).isoformat()} (age {int(now - written)}s)')
    counts = Counter(type(row.state).__name__ for row in state.gate_reservations)
    for name in ('GateLaunchPending', 'GateRunning', 'GateAbsent', 'GateTerminated'):
        print(f'{name}: {counts[name]}')
    opened = [row for row in state.gate_reservations if ledger.gate_is_open(row)]
    print(f'Open entries: {len(opened)}')
    for row in opened:
        reserved = datetime.fromtimestamp(row.at, UTC).isoformat()
        expires = datetime.fromtimestamp(row.expires, UTC).isoformat()
        print(f'  attempt={row.attempt} state={type(row.state).__name__} reserved={reserved} '
              + f'expiry={expires} instance_type={row.instance_type or "unknown"} '
              + f'region={row.region or "unknown"} mode={row.mode or "unknown"}')
    print(f'Pending compute requests: {len(pending)}')
    return int(bool(opened))
