"""Measure one test child without a memory sampler."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Sequence


def main(arguments: Sequence[str]) -> int:
    if not arguments:
        raise ValueError('The wrapper needs a test command')
    started = time.monotonic()
    status = subprocess.run(arguments, check=False).returncode
    # Nextest also uses the target runner to list tests.
    if '--list' not in arguments:
        name = next((value for value in arguments[1:] if not value.startswith('-')), '')
        binary = os.environ.get('NEXTEST_BINARY_ID', '')
        if not binary or not name:
            raise ValueError('The test has no binary ID or test name')
        row = {'binary': binary, 'test': name, 'status': status,
               'wall_s': time.monotonic() - started,
               'largest_process_peak_rss_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
        folder = Path(os.environ['GPU_TRIAL_SAMPLES'])
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256((binary + '\0' + name).encode()).hexdigest()
        destination = folder / (key + '.json')
        with destination.open('x') as output:
            _ = output.write(json.dumps(row) + '\n')
    if status < 0:
        # Preserve a signal termination without installing a signal handler.
        _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, {-status})
        os.kill(os.getpid(), -status)
    return status


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
