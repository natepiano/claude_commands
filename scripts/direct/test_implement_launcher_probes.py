"""Pin the default identity probe count without spending thirty real seconds."""

from typing import ClassVar
import unittest
from scripts.direct.implement_launcher_claims_test_support import DefaultIdentityProbesFixture


class ImplementLauncherProbeTests(DefaultIdentityProbesFixture):
    needs_completed_worker: ClassVar[bool] = False

    def test_identity_probes_default_to_thirty_one_second_waits_for_new_and_followed_workers(self) -> None:
        self.assert_default_identity_probes(False)


if __name__ == "__main__":
    _ = unittest.main()
