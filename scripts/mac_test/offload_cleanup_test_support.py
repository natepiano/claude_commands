"""Private copies of the existing command fixture for cleanup subjects."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
from typing import ClassVar, override

from offload_test_support import CommandFixture, OffloadCommandFixture


class SeededCleanupFixture(OffloadCommandFixture):
    seed: ClassVar[OffloadCommandFixture]
    snapshot: ClassVar[Path]
    gates: ClassVar[tuple[str, ...]] = (
        "continue-copy", "continue-run", "continue-cleanup", "continue-probe", "finish-run",
    )

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.snapshot = Path(cls.enterClassContext(tempfile.TemporaryDirectory())) / "snapshot"
        cls.seed = OffloadCommandFixture()
        try:
            cls.seed.setUp()
            # FIFO descriptors belong to each test, never the copied seed. Preserve
            # absolute tool symlinks by restoring every case at the original path.
            _ = shutil.copytree(cls.seed.root, cls.snapshot, symlinks=True,
                                ignore=lambda _directory, names: set(names).intersection(cls.gates))
        finally:
            _ = cls.seed.doCleanups()

    @override
    def setUp(self) -> None:
        self.root: Path = self.seed.root
        _ = shutil.copytree(self.snapshot, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.repository: Path = self.seed.repository
        self.unknown_directory: Path = self.seed.unknown_directory
        self.state_directory: Path = self.seed.state_directory
        self.claim_file: Path = self.seed.claim_file
        self.stub_directory: Path = self.seed.stub_directory
        self.bin_directory: Path = self.seed.bin_directory
        self.config_path: Path = self.seed.config_path
        self.result_path: Path = self.seed.result_path
        self.environment: dict[str, str] = self.seed.environment.copy()
        self.config: dict[str, str] = self.seed.config.copy()
        self.fixture: CommandFixture = deepcopy(self.seed.fixture)
        for name in self.gates:
            gate = self.stub_directory / name
            os.mkfifo(gate)
            descriptor = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
