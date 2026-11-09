"""Check complete configuration fixtures and successive value changes."""

from pathlib import Path
import tempfile
import unittest

from scripts.lint.memory_test_support import write_config_fixture


class ConfigFixtureTests(unittest.TestCase):
    def test_partial_config_keeps_values_and_receives_committed_defaults_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "direct.conf"
            _ = path.write_text("MAX_FIX_ATTEMPTS=17\nfixture_only=kept\n")
            _ = write_config_fixture(path, "MAX_REOPENS=19\n")
            text = path.read_text()
        committed = Path(__file__).resolve().parents[2] / "config" / "direct.conf"
        expected = dict(line.split("=", 1) for line in committed.read_text().splitlines()
                        if "=" in line and not line.startswith("#"))
        expected.update(MAX_FIX_ATTEMPTS="17", MAX_REOPENS="19", fixture_only="kept")
        actual = dict(line.split("=", 1) for line in text.splitlines()
                      if "=" in line and not line.startswith("#"))
        self.assertEqual(actual, expected)
        self.assertIn("# whole: 1..\nMAX_FIX_ATTEMPTS=17\n", text)

    def test_successive_overrides_keep_prior_values_and_allow_an_explicit_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lint.conf"
            _ = write_config_fixture(path, "sweep_ci_targets=/private/ci\nfixture_only=first\n")
            _ = write_config_fixture(path, "sweep_floor_roots=/private/root\nfixture_only=\n")
            text = path.read_text()
        self.assertIn("sweep_ci_targets=/private/ci\n", text)
        self.assertIn("sweep_floor_roots=/private/root\n", text)
        self.assertIn("fixture_only=\n", text)
        self.assertNotIn("fixture_only=first\n", text)

    def test_named_template_completes_an_existing_alternate_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-mac.conf"
            _ = path.write_text("host=private-host\n")
            _ = write_config_fixture(path, "offload=on\n", template="mac_test")
            text = path.read_text()
        self.assertIn("host=private-host\n", text)
        self.assertIn("offload=on\n", text)
        self.assertIn("leftover_timeout_s=", text)

    def test_existing_and_explicit_limits_follow_their_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aws_target.conf"
            _ = path.write_text("# range: 0..\nfixed_usd_per_month=0\n")
            _ = write_config_fixture(path, "kinds=test,doc\n# whole: 0..\nslots=0\n")
            _ = write_config_fixture(path, "offload=on\n")
            text = path.read_text()
        self.assertIn("# range: 0..\nfixed_usd_per_month=0\n", text)
        self.assertIn("# whole: 0..\nslots=0\n", text)
        self.assertIn("kinds=test,doc\n", text)


if __name__ == "__main__":
    _ = unittest.main()
