"""Configured displays change independently of their record keys."""
from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from typing import override

from scripts.lib import names


NAMES_TOML = '''[jobs]
showrunner = ["showrunner"]
unit_director = ["unit director"]
worker = ["worker"]
code_reviewer = ["code reviewer"]
ui_reviewer = ["UI reviewer"]
designer = ["designer"]
design_reviewer = ["design reviewer"]
[audiences]
showrunners = ["showrunners"]
unit_directors = ["unit directors"]
agents = ["agents"]
workers = ["workers"]
top_level = ["top level"]
all_roles = ["all roles"]
'''


def fixture_names(*, worker: str = "worker", director: str = "unit director") -> names.Names:
    """Complete fixture configuration; render tests never need the live file."""
    return names.Names(
        {"showrunner": ("showrunner",), "unit_director": (director,), "worker": (worker,),
         "code_reviewer": ("code reviewer",), "ui_reviewer": ("UI reviewer",),
         "designer": ("designer",), "design_reviewer": ("design reviewer",)},
        {"showrunners": ("showrunners",), "unit_directors": ("unit directors",),
         "agents": ("agents",), "workers": ("workers",), "top_level": ("top level",),
         "all_roles": ("all roles",)},
    )


class NamesTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        names.runtime_names.cache_clear()
        self.addCleanup(names.runtime_names.cache_clear)

    def test_runtime_configuration_is_loaded_once_even_when_a_later_read_would_change(self) -> None:
        original = fixture_names(worker="builder")
        with patch.object(names, "load_names", side_effect=(original, fixture_names(worker="maker"))) as load:
            self.assertEqual(names.runtime_names(), original)
            self.assertEqual(names.runtime_names(), original)
        load.assert_called_once_with()

    def test_configuration_errors_name_the_file_table_and_entry_in_plain_text(self) -> None:
        output = StringIO()
        with patch.object(names, "_reported_errors", set[names.NamesError]()), redirect_stderr(output):
            names.report_error(names.MissingKey("jobs", "worker"))
        self.assertEqual(output.getvalue(), f"names: {names.CONFIG_PATH}: [jobs] has no worker entry\n")
        for error, detail in (
            (names.UnknownKey("jobs", "builder"), "[jobs] has an unknown builder entry"),
            (names.MissingDisplayName("jobs", "worker"), "[jobs].worker needs a nonempty list of display names"),
            (names.DuplicateDisplayName("jobs", "builder"), "[jobs] repeats the display name 'builder'"),
            (names.UnreadableNames("/fixture/names.toml", "denied"), "cannot read the names: denied"),
            (names.InvalidNames("/fixture/names.toml", "bad table"), "invalid names: bad table"),
        ):
            with self.subTest(error=error):
                self.assertIn(detail, names.error_text(error))
                self.assertNotIn(type(error).__name__ + "(", names.error_text(error))

    def test_live_configuration_is_valid(self) -> None:
        self.assertIsInstance(names.load_names(names.CONFIG_PATH), names.Names)

    def test_successive_one_line_renames_retain_both_retired_displays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.toml"
            for history, display, retired in (
                ('["worker"]', "worker", ()),
                ('["builder", "worker"]', "builder", ("worker",)),
                ('["maker", "builder", "worker"]', "maker", ("builder", "worker")),
            ):
                with self.subTest(display=display):
                    _ = path.write_text(NAMES_TOML.replace('["worker"]', history), encoding="utf-8")
                    loaded = names.load_names(path)
                    self.assertIsInstance(loaded, names.Names)
                    assert isinstance(loaded, names.Names)
                    self.assertEqual(loaded.job("worker"), display)
                    self.assertEqual(loaded.retired(), retired)
                    self.assertIn(display, loaded.current())
                    self.assertNotIn(display, loaded.retired())

    def test_invalid_keys_histories_and_files_have_distinct_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.toml"
            self.assertIsInstance(names.load_names(path), names.UnreadableNames)
            for source, expected in (
                (NAMES_TOML.replace('worker = ["worker"]', 'unknown = ["worker"]'), names.UnknownKey),
                (NAMES_TOML.replace('worker = ["worker"]\n', ''), names.MissingKey),
                (NAMES_TOML.replace('["worker"]', '[]'), names.MissingDisplayName),
                (NAMES_TOML.replace('["worker"]', '[""]'), names.MissingDisplayName),
                (NAMES_TOML.replace('["worker"]', '["showrunner"]'), names.DuplicateDisplayName),
                (NAMES_TOML.replace('["worker"]', '["builder", "showrunner"]'), names.DuplicateDisplayName),
                (NAMES_TOML.replace('["worker"]', '["builder", "BUILDER"]'), names.DuplicateDisplayName),
                ('[jobs', names.InvalidNames),
            ):
                with self.subTest(expected=expected.__name__, source=source):
                    _ = path.write_text(source, encoding="utf-8")
                    self.assertIsInstance(names.load_names(path), expected)

    def test_runtime_reports_one_error_once_without_inventing_names(self) -> None:
        error = names.UnreadableNames("/fixture/missing-names.toml", "fixture denied")
        output = StringIO()
        with patch.object(names, "load_names", return_value=error), \
                patch.object(names, "_reported_errors", set[names.NamesError]()), redirect_stderr(output):
            self.assertEqual(names.runtime_names(), error)
            self.assertEqual(names.runtime_names(), error)
        self.assertEqual(output.getvalue().count("fixture denied"), 1)


if __name__ == "__main__":
    _ = unittest.main()
