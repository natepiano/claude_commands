"""Observe real verifier sessions without starting a machine-wide ps per check."""
from __future__ import annotations

import os
from pathlib import Path
import select
import sys
import time
from typing import override

from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


class VerifyTokenOwnershipFixture(VerifyTokenWaitFixture):
    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        # The real board reaches sleep only after a failed acquisition. Publish
        # that event, then exec the same real sleep with the original arguments.
        sleep = self.root / "stubs/sleep"
        _ = sleep.write_text(sleep.read_text().replace(
            "#!/bin/sh\n",
            '#!/bin/sh\nif [ "$1" = 3 ]; then : > "$TEST_ACQUIRE_STARTED"; fi\n',
            1,
        ))
        self.environment["TEST_ACQUIRE_STARTED"] = str(self.root / "second-acquiring")

    @override
    def live_group_processes(self, group: int) -> list[int]:
        if sys.platform != "linux":
            return super().live_group_processes(group)
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                if os.getpgid(pid) != group and os.getsid(pid) != group:
                    continue
                # comm can contain spaces and parentheses. Fields after its
                # final ')' start with state, ppid, pgrp, session.
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if fields[0] != "Z" and group in (int(fields[2]), int(fields[3])):
                found.append(pid)
        return found

    def wait_for_group_exit(self, group: int) -> None:
        """Observe exits without killing anything: the verifier must clean up."""
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while members := self.live_group_processes(group):
            descriptors: list[int] = []
            try:
                for pid in members:
                    try:
                        descriptors.append(os.pidfd_open(pid))
                    except ProcessLookupError:
                        pass
                if descriptors:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, f"session {group} never stopped")
                    ready, _, _ = select.select(descriptors, [], [], remaining)
                    self.assertTrue(ready, f"session {group} never stopped")
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
