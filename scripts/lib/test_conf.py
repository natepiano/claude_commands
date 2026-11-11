"""Configuration reads at use, validates limits, and recovers from git history."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from decimal import Decimal
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock


from scripts.lib import conf, conf_reads
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ConfTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "config"
        self.directory.mkdir()
        self.path = self.directory / "example.conf"
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.directory), "HOME": str(self.root / "home"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }))

    def write(self, value: str) -> None:
        _ = self.path.write_text(value, encoding="utf-8")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, check=True, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS,
        ).stdout.strip()

    def commit(self, contents: str) -> None:
        self.write(contents)
        _ = self.git("add", "config/example.conf")
        _ = self.git("commit", "-q", "-m", "configuration revision")

    def history(self) -> None:
        _ = self.git("init", "-q")
        _ = self.git("config", "user.name", "Configuration test")
        _ = self.git("config", "user.email", "conf@example.invalid")

    def test_config_reads_record_all_resolvers_and_cache_the_log_path(self) -> None:
        self.write("value=one\n")
        log = self.root / "reads"
        program = """from pathlib import Path
import os
from scripts.lib import conf, conf_reads
conf.path('example')
conf_reads.read_text(Path(os.environ['CONF_DIR']) / 'example.conf')
conf.path(Path(os.environ['CONF_DIR']) / 'example.conf')
conf._repository_path('example')
conf.read('example').text('value')
os.environ['CONF_READ_LOG'] = os.environ['CONF_READ_LOG'] + '.other'
conf.read_or_repository('example').text('value')
conf.effective_line('example', 'value')
conf.editable_line('example', 'value')
conf.check('example', 'value', 'two')
"""
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                                env={**os.environ, "CONF_READ_LOG": str(log)}, check=True)
        self.assertEqual(result.stderr, "")
        self.assertTrue(log.is_file(), "The reader must record its config reads")
        self.assertEqual(set(log.read_text().splitlines()), {"example"})
        self.assertGreaterEqual(len(log.read_text().splitlines()), 8)
        self.assertFalse(log.with_suffix(".other").exists())

    def test_config_read_log_context_restores_the_cached_destination(self) -> None:
        self.write("value=one\n")
        first = self.root / "first-reads"
        second = self.root / "second-reads"
        program = """import os
import sys
from scripts.lib import conf_reads
first, second, source = sys.argv[1:]
with conf_reads.record_reads(first):
    conf_reads.record('first')
    try:
        with conf_reads.record_reads(second):
            conf_reads.read_text(source)
            assert os.environ['CONF_READ_LOG'] == second
            raise ValueError('fixture interruption')
    except ValueError:
        pass
    assert os.environ['CONF_READ_LOG'] == first
    conf_reads.record('restored')
assert 'CONF_READ_LOG' not in os.environ
conf_reads.record('disabled')
"""
        environment = dict(os.environ)
        _ = environment.pop("CONF_READ_LOG", None)
        result = subprocess.run([sys.executable, "-c", program, str(first), str(second), str(self.path)],
                                capture_output=True, text=True, env=environment, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(first.read_text().splitlines(), ["first", "restored"])
        self.assertEqual(second.read_text().splitlines(), ["example"])

    def test_config_read_record_ignores_patched_os_functions(self) -> None:
        log = self.root / "reads"
        with conf_reads.record_reads(log), \
                mock.patch.object(os, "open", side_effect=OSError("fixture open refusal")), \
                mock.patch.object(os, "write", side_effect=OSError("fixture write refusal")), \
                mock.patch.object(os, "close", side_effect=OSError("fixture close refusal")):
            conf_reads.record("example")
        self.assertEqual(log.read_text().splitlines(), ["example"])

    def test_one_read_serves_many_settings_and_a_new_read_observes_changes(self) -> None:
        self.write("# range: 0..\nfraction=2.5\n# whole: 1..\ncount=3\n"
                   + "label=first\nnames=alpha beta\n# one of: small, large\nsize=small\n")
        reads: list[Path] = []
        read_text = Path.read_text

        def counted(source: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(source)
            return read_text(source, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=counted):
            settings = conf.read("example")
            self.assertEqual(reads, [self.path])
            self.assertEqual(settings.number("fraction"), 2.5)
            self.assertEqual(settings.whole("count"), 3)
            self.assertEqual(settings.text("label"), "first")
            self.assertEqual(settings.words("names"), ("alpha", "beta"))
            self.assertEqual(settings.text("size"), "small")
            self.assertEqual(reads, [self.path])
            self.write("# whole: 1..\ncount=11\n")
            self.assertEqual(settings.whole("count"), 3)
            changed = conf.read("example")
            self.assertEqual(changed.whole("count"), 11)
            self.assertEqual(reads, [self.path, self.path])

    def test_bad_live_setting_recovers_from_history_at_use(self) -> None:
        self.history()
        self.commit("# whole: 1..10\ncount=3\n")
        self.write("# whole: 1..10\ncount=broken\n")
        warning = io.StringIO()
        with redirect_stderr(warning), mock.patch.object(
            conf, "_git", side_effect=AssertionError("History must be read at use"),
        ):
            settings = conf.read("example")
        self.assertEqual(warning.getvalue(), "")
        self.commit("# whole: 1..10\ncount=8\n")
        with redirect_stderr(warning):
            self.assertEqual(settings.whole("count"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("count='broken'", warning.getvalue())
        self.assertIn(str(self.path), warning.getvalue())
        origin = settings.origin("count", int)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if not isinstance(origin, conf.RecoveredValue):
            self.fail("The invalid live setting must identify history recovery")
        self.assertEqual(origin.value, 8)

    def test_missing_file_defers_recovery_until_a_setting_is_used(self) -> None:
        self.history()
        self.commit("# whole: 1..10\ncount=3\n")
        self.path.unlink()
        warning = io.StringIO()
        with redirect_stderr(warning), mock.patch.object(
            conf, "_git", side_effect=AssertionError("History must be read at use"),
        ):
            settings = conf.read(self.path)
        self.assertEqual(warning.getvalue(), "")
        self.commit("# whole: 1..10\ncount=8\n")
        self.write("# whole: 1..10\ncount=9\n")
        with redirect_stderr(warning):
            self.assertEqual(settings.whole("count"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn(str(self.path), warning.getvalue())
        self.assertIn("count", warning.getvalue())

    def test_values_and_origin_share_the_live_read_with_host_overrides(self) -> None:
        self.write("[settings]\nlabel=first\nlabel=duplicate\nlabel.testhost=local\noptional=\n"
                   + "# range: 0..\namount=2.5\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"):
            settings = conf.read("example")
            self.write("label=changed\n# range: 0..\namount=9\n")
            self.assertEqual(settings.values(), {
                "label": "local", "label.testhost": "local", "optional": "", "amount": "2.5",
            })
            origin = settings.origin("amount", Decimal)
            self.assertIsInstance(origin, conf.UsableLiveValue)
            if not isinstance(origin, conf.UsableLiveValue):
                self.fail("The valid live setting must retain its origin")
            self.assertEqual(origin.value, Decimal("2.5"))
            self.assertEqual(conf.read("example").values(), {"label": "changed", "amount": "9"})

    def test_each_reader_returns_its_type_and_preserves_text_after_the_first_equals(self) -> None:
        self.write("# range: ..\nfraction=2.5\n# whole: ..\ncount=-2\nlabel= a=b c # note\nnames= alpha  beta\tempty\n")
        self.assertEqual(conf.read("example").number("fraction"), 2.5)
        self.assertIs(type(conf.read("example").number("fraction")), float)
        self.assertEqual(conf.read("example").whole("count"), -2)
        self.assertIs(type(conf.read("example").whole("count")), int)
        self.assertEqual(conf.read("example").text("label"), "a=b c")
        self.assertEqual(conf.read("example").words("names"), ("alpha", "beta", "empty"))

    def test_empty_and_whitespace_values_are_faults_for_every_reader(self) -> None:
        readers: tuple[Callable[[conf.ConfigRead, str], object], ...] = (
            conf.ConfigRead.number, conf.ConfigRead.whole, conf.ConfigRead.text, conf.ConfigRead.words,
        )
        for reader in readers:
            for value in ("", "   "):
                with self.subTest(reader=reader.__name__, value=value):
                    self.write(f"# {'whole' if reader is conf.ConfigRead.whole else 'range'}: ..\namount={value}\n")
                    warning = io.StringIO()
                    with redirect_stderr(warning), self.assertRaises(conf.Unusable):
                        _ = reader(conf.read("example"), "amount")
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("amount", warning.getvalue())
        self.history()
        recovery: tuple[tuple[Callable[[conf.ConfigRead, str], object], object], ...] = (
            (conf.ConfigRead.number, 3.0), (conf.ConfigRead.whole, 3),
            (conf.ConfigRead.text, "3"), (conf.ConfigRead.words, ("3",)),
        )
        for reader, expected in recovery:
            form = "whole" if reader is conf.ConfigRead.whole else "range"
            self.commit(f"# reader {reader.__name__}\n# {form}: ..\namount=3\n")
            self.write(f"# {form}: ..\namount=   \n")
            warning = io.StringIO()
            with self.subTest(reader=reader.__name__), redirect_stderr(warning):
                self.assertEqual(reader(conf.read("example"), "amount"), expected)
            self.assertEqual(len(warning.getvalue().splitlines()), 1)

    def test_reader_warnings_use_the_diagnostics_context(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                if recovery:
                    self.history()
                    self.commit("# whole: 1..4\ncount=3\n")
                self.write("# whole: 1..4\ncount=broken\n")
                with redirect_stderr(io.StringIO()) as stderr, conf.diagnostics(report=False) as warnings:
                    if recovery:
                        self.assertEqual(conf.read("example").whole("count"), 3)
                    else:
                        with self.assertRaises(conf.Unusable):
                            _ = conf.read("example").whole("count")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(len(warnings), 1)
                self.assertIn("count='broken'", warnings[0])

    def test_map_warnings_use_the_diagnostics_context(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                if recovery:
                    self.history()
                    self.commit("host=mac\n")
                    self.path.unlink()
                with redirect_stderr(io.StringIO()) as stderr, conf.diagnostics(report=False) as warnings:
                    if recovery:
                        self.assertEqual(conf.read("example").values(), {"host": "mac"})
                    else:
                        with self.assertRaises(conf.Unusable):
                            _ = conf.read("example").values()
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(len(warnings), 1)
                self.assertIn("keys:", warnings[0])

    def test_sections_are_skipped_and_the_first_value_wins(self) -> None:
        self.write("# note\n[values]\n# whole: ..\n LIMIT = 12 # first\n[another]\nLIMIT=99\nignored\n")
        self.assertEqual(conf.read("example").whole("LIMIT"), 12)

    def test_short_host_override_wins_and_its_first_value_is_used(self) -> None:
        self.write("# whole: ..\ncount=1\n# whole: ..\ncount.testhost=7\ncount.testhost=8\ncount.other=9\n")
        with mock.patch("socket.gethostname", return_value="testhost.example.invalid"):
            self.assertEqual(conf.read("example").whole("count"), 7)
        with mock.patch("socket.gethostname", return_value="unknown.example.invalid"):
            self.assertEqual(conf.read("example").whole("count"), 1)

    def test_home_config_is_used_when_conf_dir_is_not_set(self) -> None:
        home_config = self.root / "home/.claude/config"
        home_config.mkdir(parents=True)
        _ = (home_config / "example.conf").write_text("# whole: ..\ncount=41\n")
        self.write("# whole: ..\ncount=99\n")
        with mock.patch.dict(os.environ):
            del os.environ["CONF_DIR"]
            self.assertEqual(conf.read("example").whole("count"), 41)

    def test_every_call_observes_a_file_change(self) -> None:
        self.write("# whole: ..\ncount=2\n")
        self.assertEqual(conf.read("example").whole("count"), 2)
        self.write("# whole: ..\ncount=11\n")
        self.assertEqual(conf.read("example").whole("count"), 11)

    def test_temp_home_without_config_reads_the_repository_accounts_value(self) -> None:
        home = self.root / "home"
        home.mkdir()
        with (mock.patch.dict(os.environ), redirect_stderr(io.StringIO()) as warning,
              mock.patch.object(conf, "_git", side_effect=AssertionError("unexpected Git recovery"))):
            del os.environ["CONF_DIR"]
            self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 10)
            self.assertEqual(conf.read("accounts").text("usage_stale_minutes"), "10")
            self.assertEqual(conf.read("accounts").words("usage_stale_minutes"), ("10",))
            self.assertEqual(conf.read("accounts").values()["usage_stale_minutes"], "10")
            self.assertEqual(conf.path("accounts"), home / ".claude/config/accounts.conf")
        self.assertEqual(warning.getvalue(), "")
        self.assertEqual(list(home.iterdir()), [])

    def test_missing_override_file_refuses_until_a_live_file_exists(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").whole("usage_stale_minutes")
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").values()
        self.assertIsInstance(
            conf.read("accounts").origin("usage_stale_minutes", int), conf.UnavailableValue,
        )
        source = self.directory / "accounts.conf"
        _ = source.write_text("# whole: 1..\nusage_stale_minutes=23\n")
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        self.assertEqual(conf.read("accounts").values(), {"usage_stale_minutes": "23"})

    def test_missing_live_key_uses_repository_without_changing_live_values(self) -> None:
        del os.environ["CONF_DIR"]
        source = self.root / "home/.claude/config/accounts.conf"
        source.parent.mkdir(parents=True)
        contents = "# whole: 1..\nusage_stale_minutes=23\n"
        _ = source.write_text(contents)
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        with mock.patch.object(conf, "_git", side_effect=AssertionError("unexpected Git recovery")):
            self.assertEqual(conf.read("accounts").text("notes_directory"), "~/rust/hanadocs/agents")
            origin = conf.read("accounts").origin("notes_directory", str)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if isinstance(origin, conf.RecoveredValue):
            self.assertEqual(origin.value, "~/rust/hanadocs/agents")
        self.assertEqual(source.read_text(), contents)

    def test_missing_override_key_refuses_repository_values(self) -> None:
        source = self.directory / "accounts.conf"
        contents = "# whole: 1..\nusage_stale_minutes=23\n"
        _ = source.write_text(contents)
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").text("notes_directory")
        self.assertIsInstance(conf.read("accounts").origin("notes_directory", str), conf.UnavailableValue)
        self.assertEqual(source.read_text(), contents)

    def test_missing_accounts_key_uses_git_before_repository(self) -> None:
        self.history()
        source = self.directory / "accounts.conf"
        _ = source.write_text("# whole: 1..\nusage_stale_minutes=23\n")
        _ = self.git("add", "config/accounts.conf")
        _ = self.git("commit", "-qm", "accounts configuration revision")
        _ = source.write_text("other=2\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        origin = conf.read("accounts").origin("usage_stale_minutes", int)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if isinstance(origin, conf.RecoveredValue):
            self.assertEqual(origin.value, 23)

    def test_value_origin_uses_repository_for_an_absent_file(self) -> None:
        del os.environ["CONF_DIR"]
        origin = conf.read("accounts").origin("usage_stale_minutes", int)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if isinstance(origin, conf.RecoveredValue):
            self.assertEqual(origin.value, 10)

    def test_existing_invalid_key_and_explicit_missing_path_do_not_use_repository(self) -> None:
        del os.environ["CONF_DIR"]
        source = self.root / "home/.claude/config/accounts.conf"
        source.parent.mkdir(parents=True)
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read(source).whole("usage_stale_minutes")
        for contents in ("# whole: 1..\nusage_stale_minutes=broken\n", "# whole: 1..\nusage_stale_minutes=0\n"):
            with self.subTest(contents=contents):
                _ = source.write_text(contents)
                with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                    _ = conf.read("accounts").whole("usage_stale_minutes")
                self.assertIsInstance(
                    conf.read("accounts").origin("usage_stale_minutes", int), conf.UnavailableValue,
                )

    def test_host_override_uses_its_own_adjacent_limits_for_reads_and_check(self) -> None:
        self.write("# whole: 1..4\ncount=3\n# whole: 8..10\ncount.testhost=9\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"):
            self.assertEqual(conf.read("example").whole("count"), 9)
            conf.check("example", "count", "10")
            with self.assertRaises(conf.Unusable):
                conf.check("example", "count", "4")

    def test_inclusive_and_open_ended_ranges_accept_their_boundaries(self) -> None:
        for limits, value in (("1..4", "1"), ("1..4", "4"), ("..-2", "-2"),
                              ("2..", "2"), ("1.5..2.5", "2.5"), ("..", "-100"),
                              ("1..4", "1.0000000000000000"), ("1..4", "4.0000000000000000")):
            with self.subTest(limits=limits, value=value):
                self.write(f"# range: {limits}\namount={value}\n")
                self.assertEqual(conf.read("example").number("amount"), float(value))
                self.assertEqual((conf.read("example").origin("amount", Decimal),
                                  type(conf.read("example").number("amount"))),
                                 (conf.UsableLiveValue(Decimal(value)), float))
                conf.check("example", "amount", value)

    def test_configuration_import_does_not_load_decimal(self) -> None:
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), '-S', '-c',
             "import sys; from scripts.lib import conf; print('decimal' in sys.modules)"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')

    def test_check_refuses_values_that_overflow_the_numeric_reader(self) -> None:
        self.write("# range: 0.001..\namount=1\n")
        with self.assertRaises(conf.Unusable):
            conf.check("example", "amount", "1e400")
        self.assertEqual(conf.read("example").number("amount"), 1.0)

    def test_one_of_limits_accept_members_and_reject_other_values(self) -> None:
        self.write("# one of: small, medium, large\nsize=medium\n")
        self.assertEqual(conf.read("example").text("size"), "medium")
        conf.check("example", "size", "large")
        with self.assertRaises(conf.Unusable) as caught:
            conf.check("example", "size", "huge")
        self.assertIn("size", str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))

    def test_bad_types_and_out_of_range_values_warn_once_and_refuse_without_history(self) -> None:
        readers: tuple[tuple[Callable[[conf.ConfigRead, str], object], str], ...] = (
            (conf.ConfigRead.number, "bad"), (conf.ConfigRead.number, "nan"), (conf.ConfigRead.number, "inf"),
            (conf.ConfigRead.number, ""), (conf.ConfigRead.whole, "1.5"), (conf.ConfigRead.whole, "true"),
            (conf.ConfigRead.number, "0"), (conf.ConfigRead.number, "5"),
            (conf.ConfigRead.number, "4.0000000000000001"),
            (conf.ConfigRead.number, "0.99999999999999999"),
        )
        for reader, value in readers:
            with self.subTest(reader=reader.__name__, value=value):
                self.write(f"# {'whole' if reader is conf.ConfigRead.whole else 'range'}: 1..4\namount={value}\n")
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = reader(conf.read("example"), "amount")
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(str(self.path), warning.getvalue())
                self.assertIn("amount", warning.getvalue())
                self.assertIn(value, warning.getvalue())
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn("amount", str(caught.exception))

    def test_missing_file_key_and_bad_encoding_do_not_supply_defaults(self) -> None:
        for damage in ("missing file", "missing key", "bad encoding"):
            with self.subTest(damage=damage):
                self.path.unlink(missing_ok=True)
                if damage == "missing key":
                    self.write("other=2\n")
                elif damage == "bad encoding":
                    _ = self.path.write_bytes(b"amount=\xff\n")
                with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable) as caught:
                    _ = conf.read("example").whole("amount")
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn("amount", str(caught.exception))

    def test_fault_uses_the_newest_committed_value_that_passes_its_own_limits(self) -> None:
        self.history()
        self.commit("# whole: 1..10\namount=3\n")
        self.commit("# whole: 1..10\namount=8\n")
        self.commit("# whole: 1..4\namount=9\n")
        self.write("# whole: 1..2\namount=broken\n")
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assertEqual(conf.read("example").whole("amount"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("broken", warning.getvalue())
        self.assertIn("amount", warning.getvalue())
        self.assertIn(str(self.path), warning.getvalue())

    def test_missing_current_key_recovers_from_history_and_host_override_is_validated(self) -> None:
        self.history()
        self.commit("# whole: ..\namount=3\n# whole: 1..9\namount.testhost=7\n")
        self.write("other=0\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"), redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").whole("amount"), 7)

    def test_no_good_revision_raises_instead_of_using_a_code_default(self) -> None:
        self.history()
        self.commit("amount=broken\n")
        self.commit("# range: 1..4\namount=9\n")
        self.write("# range: ..\namount=nan\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("example").number("amount")

    def test_check_refuses_missing_key_and_invalid_candidate_without_falling_back_or_writing(self) -> None:
        self.history()
        self.commit("# range: 1..4\namount=2\n")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        for key, value in (("absent", "3"), ("amount", "0"), ("amount", "5"), ("amount", "bad")):
            with self.subTest(key=key, value=value), self.assertRaises(conf.Unusable):
                conf.check("example", key, value)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_cli_get_uses_the_same_validation_and_history(self) -> None:
        self.history()
        self.commit("# range: 1..4\namount=3\n")
        self.write("# range: 1..4\namount=9\n")
        result = subprocess.run(
            [sys.executable, str(Path(conf.__file__)), "get", "example", "amount"],
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n")
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_map_recovers_the_newest_decodable_file_after_file_damage(self) -> None:
        self.history()
        self.commit("host=old\n")
        self.commit("host=mac\nlinux_only.example=\n# whole: 1..\ntries=3\n")
        _ = self.path.write_bytes(b"host=\xff\n")
        _ = self.git("add", "config/example.conf")
        _ = self.git("commit", "-qm", "damaged encoding")
        for damage in ("missing", "encoding", "directory"):
            with self.subTest(damage=damage):
                self.path.unlink(missing_ok=True)
                if damage == "encoding":
                    _ = self.path.write_bytes(b"host=\xff\n")
                elif damage == "directory":
                    self.path.mkdir()
                warning = io.StringIO()
                with redirect_stderr(warning):
                    self.assertEqual(conf.read("example").values(), {
                        "host": "mac", "linux_only.example": "", "tries": "3",
                    })
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(str(self.path), warning.getvalue())
                if damage == "directory":
                    self.path.rmdir()

    def test_map_refuses_when_no_committed_file_decodes(self) -> None:
        for history in (False, True):
            with self.subTest(history=history):
                if history:
                    self.history()
                    _ = self.path.write_bytes(b"host=\xff\n")
                    _ = self.git("add", "config/example.conf")
                    _ = self.git("commit", "-qm", "damaged encoding")
                self.path.unlink(missing_ok=True)
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = conf.read("example").values()
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(str(self.path), str(caught.exception))

    def test_map_key_faults_keep_history_recovery_and_optional_empty_entries(self) -> None:
        self.history()
        self.commit("# whole: 1..4\ntries=3\noptional=\n")
        self.write("# whole: 1..4\ntries=1.5\noptional=\n")
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assertEqual(conf.read("example").values(), {"tries": "3", "optional": ""})
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("tries", warning.getvalue())

    def test_whole_limits_reject_fractional_publications_and_accept_exact_large_integers(self) -> None:
        self.write("# whole: 1..9007199254740993\ntries=3\n")
        for value in ("1.5", "0", "9007199254740994", "nan"):
            with self.subTest(value=value), self.assertRaises(conf.Unusable):
                conf.check("example", "tries", value)
        for value in ("1", "+3", "9007199254740993"):
            conf.check("example", "tries", value)
            self.write(f"# whole: 1..9007199254740993\ntries={value}\n")
            self.assertEqual(conf.read("example").whole("tries"), int(value))

    def test_whole_limits_accept_open_bounds_and_reject_values_beyond_them(self) -> None:
        for bounds, accepted, refused in (("..-2", "-2", "-1"), ("2..", "2", "1"),
                                          ("..", "-100", "1.5")):
            with self.subTest(bounds=bounds):
                self.write(f"# whole: {bounds}\namount={accepted}\n")
                conf.check("example", "amount", accepted)
                self.assertEqual(conf.read("example").whole("amount"), int(accepted))
                with self.assertRaises(conf.Unusable):
                    conf.check("example", "amount", refused)

    def test_numeric_readers_refuse_the_wrong_limit_form_and_missing_limits(self) -> None:
        for reader, form in ((conf.ConfigRead.whole, "range"), (conf.ConfigRead.number, "whole"),
                             (conf.ConfigRead.whole, "none"), (conf.ConfigRead.number, "none")):
            with self.subTest(reader=reader.__name__, form=form):
                self.write(f"# {form}: 1..4\namount=3\n")
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = reader(conf.read("example"), "amount")
                self.assertIn("amount", str(caught.exception))
                self.assertIn("limit", str(caught.exception))
                self.assertEqual(len(warning.getvalue().splitlines()), 1)

    def test_numeric_history_uses_the_matching_limit_form(self) -> None:
        self.history()
        self.commit("# whole: 1..4\namount=2\n")
        self.commit("# range: 1..4\namount=3\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").whole("amount"), 2)
        self.commit("# whole: 1..4\namount=4\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").number("amount"), 3.0)

    def test_editable_line_requires_an_exact_key_and_preserves_its_raw_value(self) -> None:
        self.write("# whole: 1..\ncount=invalid\ncount=9\nother.remote=4\n")
        before = self.path.read_bytes()
        self.assertEqual(conf.editable_line("example", "count"), (1, "invalid"))
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch("socket.gethostname", return_value="remote"):
            self.assertEqual(conf.effective_line("example", "other"), (3, "4"))
            with self.assertRaisesRegex(conf.Unusable, "other: exact stored key is missing"):
                _ = conf.editable_line("example", "other")

    def test_editable_line_refuses_host_overrides_and_names_both_source_lines(self) -> None:
        self.write("# whole: 1..\ncount=3\n# whole: 1..\ncount.remote=4\n")
        before = self.path.read_bytes()
        with mock.patch("socket.gethostname", return_value="local"):
            self.assertEqual(conf.effective_line("example", "count"), (1, "3"))
            with self.assertRaisesRegex(
                conf.Unusable, "count at line 2 has host override count.remote at line 4",
            ):
                _ = conf.editable_line("example", "count")
        self.assertEqual(self.path.read_bytes(), before)

    def test_installed_script_refuses_conf_dir_override(self) -> None:
        from scripts.build_hold import override_guard

        home = self.root / "installed-home"
        script = home / ".claude/scripts/lib/probe.py"
        script.parent.mkdir(parents=True)
        _ = script.write_text("# Private installed script.\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(override_guard, "user_home", return_value=home), self.assertRaises(SystemExit), \
                redirect_stderr(io.StringIO()) as warning:
            override_guard.refuse_overrides(script, "lib/probe.py")
        self.assertIn("CONF_DIR", warning.getvalue())

    def test_value_origin_distinguishes_live_permission_from_recovered_limits(self) -> None:
        self.history()
        self.commit("# range: 0..\namount=40\n")
        self.write("# range: 0..\namount=20\n")
        live = conf.read("example").origin("amount", Decimal)
        self.assertIsInstance(live, conf.UsableLiveValue)
        if not isinstance(live, conf.UsableLiveValue):
            self.fail("The valid live limit must identify live permission")
        self.assertEqual(live.value, Decimal("20"))
        for source in ("other=1\n", "# range: 0..\namount=broken\n",
                       "# range: 0..\namount=-1\n"):
            with self.subTest(source=source), redirect_stderr(io.StringIO()):
                self.write(source)
                recovered = conf.read("example").origin("amount", Decimal)
                self.assertIsInstance(recovered, conf.RecoveredValue)
                if not isinstance(recovered, conf.RecoveredValue):
                    self.fail("History must remain distinct from live spending permission")
                self.assertEqual(recovered.value, Decimal("40"))
                self.assertEqual(conf.read("example").number("amount"), 40)

    def test_value_origin_reports_unavailable_data_without_an_implicit_limit(self) -> None:
        self.write("# range: 0..\namount=broken\n")
        with redirect_stderr(io.StringIO()):
            result = conf.read("example").origin("amount", Decimal)
        self.assertIsInstance(result, conf.UnavailableValue)

    def test_value_origin_reads_changed_live_limits_after_history_recovery(self) -> None:
        self.history()
        self.commit("# range: 0..\namount=40\n")
        self.write("amount=broken\n")
        with redirect_stderr(io.StringIO()):
            recovered = conf.read(self.path).origin("amount", Decimal)
        self.assertIsInstance(recovered, conf.RecoveredValue)
        self.write("# range: 0..\namount=7.25\n")
        live = conf.read(self.path).origin("amount", Decimal)
        self.assertIsInstance(live, conf.UsableLiveValue)
        if not isinstance(live, conf.UsableLiveValue):
            self.fail("A repaired live limit must replace the recovered state")
        self.assertEqual(live.value, Decimal("7.25"))


if __name__ == "__main__":
    _ = unittest.main()
