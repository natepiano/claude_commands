"""A dirty tree cannot produce a final pass record."""
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_staged_final_writes_no_record_and_default_query_refuses(self) -> None:
        self.assert_dirty_final("staged")


if __name__ == "__main__":
    _ = unittest.main()
