"""Fixture tests for names, source coverage, production names and message routing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import roles_audit as audit
import showrunners


def term(name: str, *others: str, defined_in: tuple[str, ...] = ("docs/words.md#Words",),
         kept: tuple[str, ...] = (), other_meaning_files: tuple[str, ...] = ()) -> audit.Term:
    return audit.Term(name, "role", defined_in, others, kept, other_meaning_files)


def message(at: datetime, sender: str = "alpha", recipient: str = "user", **fields: str) -> str:
    return json.dumps({"time": at.isoformat(), "machine": "natedev", "from": sender,
                       "to": recipient, "outcome": "sent", **fields})


class NameTests(unittest.TestCase):
    def test_counts_phrases_files_and_boundaries_but_not_paths_or_kept_commands(self) -> None:
        terms = (term("worker", "seat", kept=("/keep:seat", "docs/seat/")), term("/unit:direct", "/unit:delegate"))
        files = (audit.LiveFile("commands/a.md", "Worker worker. seat seatbelt worker-unit /tmp/worker.md "
                                + "docs/seat/worker.md worker.md /keep:seat /unit:direct /unit:delegate"),
                 audit.LiveFile("docs/b.md", "seat Worker"))
        rows = audit.measure_names(terms, files)
        self.assertEqual([(r.name, r.hits, r.files) for r in rows], [
            ("worker", 3, ("commands/a.md", "docs/b.md")),
            ("seat", 2, ("commands/a.md", "docs/b.md")),
            ("/unit:direct", 1, ("commands/a.md",)),
            ("/unit:delegate", 1, ("commands/a.md",)),
        ])

    def test_python_strings_exclude_code_comments_docstrings_keys_and_paths(self) -> None:
        source = '''"""worker docs"""
worker = "worker_key"
lookup = {"worker": 1}
value = lookup["worker"]
# worker comment
print("worker", "docs/worker/file.md")
print(f"seat for {worker}")
parser.add_argument("--worker", help="worker picks a seat")
if value == "worker":
    pass
'''
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.py", audit.python_strings(source)),))
        self.assertEqual([(r.name, r.hits) for r in rows], [("worker", 2), ("seat", 2)])

    def test_shell_output_excludes_comments_and_assignments(self) -> None:
        text = '# echo "worker"\nWORKER="worker"\necho "worker has a seat" >&2\nprintf "%s\\n" "seat"\n'
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.sh", audit.shell_strings(text)),))
        self.assertEqual([row.hits for row in rows], [1, 2])

    def test_python_output_follows_variable_labels_and_function_returns(self) -> None:
        source = '''labels = {"worker": "seat"}
label = "worker"
def choose():
    return label
print(labels["worker"], choose())
subprocess.run(["echo", "worker has a seat"])
'''
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.py", audit.python_strings(source)),))
        self.assertEqual([row.hits for row in rows], [1, 1])

    def test_python_output_uses_its_own_scope_and_visible_module_bindings(self) -> None:
        source = '''label = "worker"
def hidden():
    label = "seat"
def visible():
    label = "showrunner"
    print(label)
def module_label():
    print(label)
def shadowed(label):
    print(label)
'''
        strings = audit.python_strings(source)
        self.assertEqual(set(strings.splitlines()), {"worker", "showrunner"})

    def test_python_helper_call_arguments_are_not_followed_into_parameters(self) -> None:
        source = '''label = "seat"
def display(label):
    print(label)
display("worker")
'''
        self.assertEqual(audit.python_strings(source), "")

    def test_shell_counts_output_heredocs_but_does_not_parse_input_bodies_as_commands(self) -> None:
        text = '''cat <<'EOF'
worker
seat
EOF
cat <<-"TEXT"
\tseat
\tTEXT
consumer <<'INPUT'
echo "worker"
INPUT
'''
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.sh", audit.shell_strings(text)),))
        self.assertEqual([row.hits for row in rows], [1, 2])

    def test_shell_counts_only_output_arguments_on_lines_with_test_operands(self) -> None:
        text = '''if [ "$role" = "worker" ]; then echo "seat"; fi
test "$role" = "worker" && printf "%s" "seat"
[[ "$role" == "worker" ]] || echo "seat"
echo "seat"; test "$role" = "worker"
'''
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.sh", audit.shell_strings(text)),))
        self.assertEqual([row.hits for row in rows], [0, 4])

    def test_reader_excludes_plans_tests_fixtures_and_non_script_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                "CLAUDE.md": "worker", "commands/a.md": "seat", "docs/words.md": "worker",
                "docs/plans/a.md": "seat", "skills/a/SKILL.md": "worker",
                "scripts/a.py": 'print("seat")', "scripts/test_a.py": 'print("worker")',
                "scripts/fixtures/a.py": 'print("worker")', "scripts/data.json": '"worker"',
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(text, encoding="utf-8")
            files, issues = audit.read_live_files(root)
        self.assertFalse(issues)
        self.assertEqual({file.path for file in files},
                         {"CLAUDE.md", "commands/a.md", "docs/words.md", "skills/a/SKILL.md", "scripts/a.py"})
        self.assertEqual([row.hits for row in audit.measure_names((term("worker", "seat"),), files)], [3, 2])

    def test_undefined_terms_and_multiple_definition_files_are_separate_findings(self) -> None:
        terms = (term("worker"), term("landing call", defined_in=()))
        files = (audit.LiveFile("docs/words.md", "- **Worker** — does work"),
                 audit.LiveFile("docs/other.md", "**worker** — also does work"))
        rows = audit.measure_definitions(terms, files)
        self.assertEqual(rows, (audit.DefinitionFinding("worker", "multiple files", ("docs/other.md", "docs/words.md")),
                                audit.DefinitionFinding("landing call", "no defined_in", ())))

    def test_definition_discovery_excludes_other_meanings_and_keeps_second_definitions(self) -> None:
        terms = audit.parse_terms('''[[terms]]
name = "change"
kind = "record"
defined_in = "commands/change_review.md#add"
other_names = []
other_meaning_files = ["commands/changelog.md", "commands/commit_prep.md"]
''')
        menu_files = (audit.LiveFile("commands/changelog.md", "- **change** - Provide a different entry"),
                      audit.LiveFile("commands/commit_prep.md", "- **change** - Revise the message"))
        self.assertEqual(audit.measure_definitions(terms, menu_files), ())
        second = audit.LiveFile("docs/records.md", "- **change** — A change-review record with a C number")
        self.assertEqual(audit.measure_definitions(terms, (*menu_files, second)),
                         (audit.DefinitionFinding("change", "multiple files",
                                                  ("commands/change_review.md", "docs/records.md")),))

    def test_terms_support_kept_names_and_multiple_sources_and_reject_invalid_data(self) -> None:
        text = '''[[terms]]
name = "worker"
kind = "role"
defined_in = "docs/a.md#Words; docs/b.md#Words"
other_names = ["seat"]
kept = ["docs/seat/"]
'''
        self.assertEqual(audit.parse_terms(text),
                         (term("worker", "seat", defined_in=("docs/a.md#Words", "docs/b.md#Words"), kept=("docs/seat/",)),))
        for invalid in (text.replace('kind = "role"', 'kind = "other"'),
                        text.replace('["seat"]', '[2]'), text + text):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _ = audit.parse_terms(invalid)


class ProductionNameTests(unittest.TestCase):
    def test_reads_only_units_and_finds_format_and_cross_role_collisions(self) -> None:
        text = '''# A
- **Showrunner session:** alpha
## Units
| Unit | Plan | Worktree | Branch |
| --- | --- | --- | --- |
| `render-unit` | a.md | /tmp/a | a |
| old | (retired) | /tmp/b | b |
## Other
| not-a-unit | ignored |
'''
        first = audit.parse_production("one", "/fixture/one.md", text)
        second = audit.ProductionNames("two", "/fixture/two.md", "render-unit", ("tools-unit",))
        rows = audit.measure_production_names((first, second))
        self.assertEqual(first.units, ("render-unit", "old"))
        self.assertEqual(rows, (
            audit.ProductionNameFinding("unit format", "old", ("one",), ("/fixture/one.md",)),
            audit.ProductionNameFinding("shared name", "render-unit", ("one", "two"), ("/fixture/one.md", "/fixture/two.md")),
        ))


class MessageTests(unittest.TestCase):
    def test_window_machine_outcome_and_historical_roles_control_counts(self) -> None:
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=2)
        director = {"from_role": "unit director", "from_production": "one"}
        lines = [message(start, **director),
                 message(start, recipient="another", to_role="showrunner", to_production="two", **director),
                 message(start, recipient="own", to_role="showrunner", to_production="one", **director),
                 message(start, recipient="unknown", to_role="showrunner", **director),
                 message(start, sender="named-unit"),
                 message(start, sender="unresolved", machine=""),
                 message(start, sender="mac-unit", machine="mac", **director),
                 message(start, outcome="skipped", **director),
                 message(end, **director), message(start - timedelta(seconds=1), **director)]
        source = audit.parse_messages("\n".join(lines))
        counts = audit.measure_wrong_party(source.records, audit.AuditWindow(start, end))
        self.assertEqual(counts.senders, (audit.SenderContacts("alpha", 1, 1, 0, 1, 4),
                                         audit.SenderContacts("named-unit", 0, 0, 1, 0, 1)))
        self.assertEqual((counts.window_records, counts.sent_records, counts.other_machine_records, counts.machine_unknown),
                         (8, 5, 1, 1))

    def test_bad_lines_and_naive_times_are_counted_without_losing_good_records(self) -> None:
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = audit.parse_messages('\n'.join([message(at), '{', '[]', '{}', message(at.replace(tzinfo=None))]))
        self.assertEqual(source.invalid_lines, 4)
        self.assertEqual([r.sender for r in source.records], ["alpha"])

    def test_date_window_uses_local_midnight_and_until_is_inclusive(self) -> None:
        with patch.dict("os.environ", {"USER_ZONE": "America/New_York"}):
            window = audit.audit_window("2026-10-01", "2026-10-02", datetime(2026, 10, 9, tzinfo=timezone.utc))
            self.assertEqual(window.since.astimezone(timezone.utc), datetime(2026, 10, 1, 4, tzinfo=timezone.utc))
            self.assertEqual(window.until.astimezone(timezone.utc), datetime(2026, 10, 3, 4, tzinfo=timezone.utc))
            self.assertIn("America/New_York", window.label())
            with self.assertRaises(ValueError):
                _ = audit.audit_window("2026-10-04", "2026-10-02", window.until)

    def test_report_exposes_unknown_roles_sources_window_and_total_share(self) -> None:
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = audit.parse_messages(message(start))
        result = audit.report((term("worker", "seat"),), (audit.LiveFile("commands/a.md", "worker seat seat"),),
                              (), source, audit.AuditWindow(start, start + timedelta(days=1)), ())
        self.assertIn("2 / 3", result)
        self.assertIn("66.7%", result)
        self.assertIn("not recorded: 1", result)
        self.assertIn(audit.ROLE_SOURCE, result)
        self.assertIn("natedev only", result)
        self.assertIn("2026-09-30", result)
        self.assertIn("live: n=1 files", result)
        self.assertIn("helper call arguments are not followed into parameters", result)

    def test_baseline_reads_fixture_productions_and_reports_unavailable_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "scripts/production/role_terms.toml"
            inventory.parent.mkdir(parents=True)
            _ = inventory.write_text('''[[terms]]
name = "worker"
kind = "role"
defined_in = "none"
other_names = ["seat"]
''', encoding="utf-8")
            doc = root / "fixture-production.md"
            _ = doc.write_text("## Units\n| Unit | Plan |\n| --- | --- |\n| bad | a.md |\n", encoding="utf-8")
            at = datetime(2026, 10, 1, tzinfo=timezone.utc)
            with patch.object(showrunners, "timer_docs", return_value={"fixture": doc}), \
                    patch.object(Path, "home", return_value=root), \
                    patch.dict("os.environ", {"USER_ZONE": "America/New_York"}):
                result = audit.baseline(root, audit.AuditWindow(at, at + timedelta(days=1)))
            self.assertIn("unit format", result)
            self.assertIn("bad", result)
            self.assertIn("no defined_in", result)
            self.assertIn("unavailable:", result)
            self.assertIn("natedev sent n=0", result)


if __name__ == "__main__":
    _ = unittest.main()
