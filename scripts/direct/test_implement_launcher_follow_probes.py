"""Pin the default identity probe count without spending thirty real seconds."""

from typing import ClassVar
import unittest
from scripts.direct.implement_launcher_claims_test_support import DefaultIdentityProbesFixture


class ImplementLauncherFollowProbeTests(DefaultIdentityProbesFixture):
    needs_completed_worker: ClassVar[bool] = True

    def test_followed_worker_identity_probes_default_to_thirty_one_second_waits(self) -> None:
        self.assert_default_identity_probes(True)


if __name__ == "__main__":
    _ = unittest.main()
