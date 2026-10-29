"""Seeded tools and atomic memory release for Mac retry integration tests."""
from __future__ import annotations

from pathlib import Path
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


class VerifyMacMemoryRetryFixture(VerifyMacOffloadFixture):
    def release_memory(self, meminfo: Path) -> None:
        # A polling reader must not see the empty interval of a truncated file.
        ready = meminfo.with_suffix(".ready")
        _ = ready.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        _ = ready.replace(meminfo)
