"""Real gate jobs with event-driven barriers and a direct Python interpreter."""

from __future__ import annotations

import os
from pathlib import Path
import select
import sys
from typing import override

from scripts.production.merge_gate_test_support import MergeGateFixture, SUITE_MODULE
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# Keep the shared fixture unchanged for its other subjects. These jobs still run
# in separate processes, but need no Python stub which only execs Python again.
CONCURRENCY_POLL = '''            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while not (state / (str(os.getpgrp()) + ".released")).exists():
                if time.monotonic() >= deadline:
                    self.fail("runner never released the module")
                time.sleep(0.01)'''
CONCURRENCY_EVENT = '''            import subprocess
            helper = Path(os.environ["MERGE_GATE_TEST_WAIT"])
            subprocess.run([str(helper.parents[1] / "scripts/lib/py"), str(helper),
                            "file", str(state / (str(os.getpgrp()) + ".released"))],
                           check=True, timeout=TEST_HANG_GUARD_SECS)'''
OVERLAP_POLL = '''            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while True:
                with (state / "modules.jsonl").open() as source:
                    starts = [json.loads(line) for line in source if line.strip()]
                if len([row for row in starts if row["suite"] == "lib" and row["event"] == "start"]) == 2:
                    break
                if time.monotonic() >= deadline:
                    self.fail("module processes never overlapped")
                time.sleep(0.01)'''
OVERLAP_EVENT = '''            import subprocess
            helper = Path(os.environ["MERGE_GATE_TEST_WAIT"])
            subprocess.run([str(helper.parents[1] / "scripts/lib/py"), str(helper), "overlap", str(state)],
                           check=True, timeout=TEST_HANG_GUARD_SECS)'''

WAIT_FOR_STATE = '''import json
import sys
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_until

path = Path(sys.argv[2])
if sys.argv[1] == "file":
    wait_until(path.exists, path.parent)
else:
    def both_started():
        with (path / "modules.jsonl").open() as source:
            starts = [json.loads(line) for line in source if line.strip()]
        return len([row for row in starts if row["suite"] == "lib" and row["event"] == "start"]) == 2
    wait_until(both_started, path)
'''


class GateSuitesFixture(MergeGateFixture):
    @override
    def bind_fixture(self, root: Path) -> None:
        super().bind_fixture(root)
        self.environment["LINT_CONFIG_FILE"] = str(root / "lint.conf")
        self.environment["MERGE_GATE_PYTHON"] = sys.executable
        self.environment["MERGE_GATE_TEST_WAIT"] = str(self.bin / "wait-for-state.py")

    @override
    def build_fixture(self, root: Path) -> None:
        super().build_fixture(root)
        self.write(self.bin / "wait-for-state.py", WAIT_FOR_STATE)

    @override
    def write(self, path: Path, content: str) -> None:
        if content == SUITE_MODULE:
            # Fail if the shared template changes instead of silently losing the
            # event conversion. All assertions within the jobs stay unchanged.
            for polling, event in ((CONCURRENCY_POLL, CONCURRENCY_EVENT), (OVERLAP_POLL, OVERLAP_EVENT)):
                self.assertEqual(content.count(polling), 1)
                content = content.replace(polling, event)
        super().write(path, content)

    @override
    def assert_process_stopped(self, pid: int) -> None:
        def stopped() -> bool:
            try:
                return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
            except FileNotFoundError:
                return True

        if stopped():
            return
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            readable, _, _ = select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)
            if not readable or not stopped():
                self.fail(f"gate left running child {pid}")
        finally:
            os.close(descriptor)
