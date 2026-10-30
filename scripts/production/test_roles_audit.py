"""Fixture tests for names, source coverage, production names and message routing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch
from collections.abc import Callable
from typing import cast

import roles_audit as audit
import showrunners
from scripts.lib.test_names import NAMES_TOML


def term(name: str, *others: str, defined_in: tuple[str, ...] = ("docs/words.md#Words",),
         kept: tuple[str, ...] = (), other_meaning_files: tuple[str, ...] = (),
         inflected_forms: tuple[audit.NameInflection, ...] = (), retired: tuple[str, ...] = (),
         kept_contexts: tuple[audit.KeptContext, ...] = ()) -> audit.Term:
    return audit.Term(name, "role", defined_in, others, kept, other_meaning_files,
                      inflected_forms, retired, kept_contexts)


WORKER_TERMS = '''[[terms]]
name = "worker"
kind = "role"
defined_in = "docs/words.md#Words"
other_names = ["seat", "Codex seat", "delegate", "Codex worker"]
retired = ["seat", "seats", "Codex seat", "Codex seats"]
[terms.inflected_forms]
worker = ["workers"]
seat = ["seats"]
"Codex seat" = ["Codex seats"]
delegate = ["delegates"]
"Codex worker" = ["Codex workers"]
'''


def message(at: datetime, sender: str = "alpha", recipient: str = "user", **fields: str) -> str:
    return json.dumps({"time": at.isoformat(), "machine": "natedev", "from": sender,
                       "to": recipient, "outcome": "sent", **fields})


class NameTests(unittest.TestCase):
    def test_names_count_forms_independently_and_check_reports_longest_once(self) -> None:
        terms = (*audit.parse_terms(WORKER_TERMS), term("gate", "/unit:direct"))
        files = (audit.LiveFile("docs/a.md", "worker workers seat seats CODEX SEAT Codex seats "
                                + "delegate delegates Codex worker Codex workers gate gates "
                                + "/unit:direct /unit:directs seatbelt seats-unit"),)
        rows = audit.measure_names(terms, files)
        self.assertEqual([(row.name, row.hits) for row in rows], [
            ("worker", 2), ("workers", 2), ("seat", 2), ("seats", 2),
            ("Codex seat", 1), ("Codex seats", 1), ("delegate", 1), ("delegates", 1),
            ("Codex worker", 1), ("Codex workers", 1), ("gate", 1), ("/unit:direct", 1),
        ])
        uses = audit.check_retired(terms, files)
        self.assertEqual([(use.name, use.replacement) for use in uses],
                         [("seat", audit.CurrentName("worker")), ("seats", audit.CurrentName("worker")),
                          ("CODEX SEAT", audit.CurrentName("worker")), ("Codex seats", audit.CurrentName("worker"))])

    def test_a_term_that_retires_itself_names_no_replacement(self) -> None:
        gone = term("review pool", retired=("review pool",))
        renamed = term("worker", "seat", retired=("seat",))
        files = (audit.LiveFile("docs/a.md", "The review pool and one seat."),)
        self.assertEqual([use.hint() for use in audit.check_retired((gone, renamed), files)], [
            "docs/a.md:1: retired 'review pool'; no current name replaces it",
            "docs/a.md:1: retired 'seat'; use 'worker'",
        ])

    def test_scoped_protocol_history_and_legacy_contexts_keep_lexical_counts(self) -> None:
        data = WORKER_TERMS + '''
[[terms.kept_contexts]]
path = "docs/protocol.md"
text = "Stored field `seat`; send --to <seat>."
reason = "Stored field and command placeholder."
[[terms.kept_contexts]]
path = "docs/history.md"
text = "2026-10-16: two Codex seats resumed."
reason = "Dated probe history."
[[terms.kept_contexts]]
path = "scripts/migrate.py"
text = "DELETE FROM changes WHERE role = 'seats'"
reason = "Legacy migration input."
'''
        terms = audit.parse_terms(data)
        files = (audit.LiveFile("docs/protocol.md", "Stored field `seat`; send --to <seat>."),
                 audit.LiveFile("docs/history.md", "2026-10-16: two Codex seats resumed."),
                 audit.LiveFile("scripts/migrate.py", "DELETE FROM changes WHERE role = 'seats'"))
        self.assertEqual(audit.check_retired(terms, files), ())
        self.assertEqual(sum(row.hits for row in audit.measure_names(terms, files)), 5)
        self.assertEqual(terms[0].kept_contexts[1].reason, "Dated probe history.")

    def test_copied_changed_and_repeated_contexts_do_not_hide_retired_uses(self) -> None:
        data = WORKER_TERMS + '''
[[terms.kept_contexts]]
path = "docs/protocol.md"
text = "Send --to <seat>."
reason = "Command placeholder."
'''
        terms = audit.parse_terms(data)
        for path, text, expected in (
            ("docs/copied.md", "Send --to <seat>.", [(1, "seat")]),
            ("docs/protocol.md", "Dispatch --to <seat>.", [(1, "seat")]),
            ("docs/protocol.md", "Send --to <seats>.", [(1, "seats")]),
            ("docs/protocol.md", "Send --to <seat>.\nSend --to <seat>.", [(2, "seat")]),
            ("docs/protocol.md", "Send --to <seat>.\nDispatch a Codex seat and two seats.",
             [(2, "Codex seat"), (2, "seats")]),
        ):
            with self.subTest(path=path, text=text):
                uses = audit.check_retired(terms, (audit.LiveFile(path, text),))
                self.assertEqual([(use.line, use.name) for use in uses], expected)
                self.assertTrue(all(use.path == path for use in uses))

    def test_term_data_rejects_undeclared_forms_retirement_and_unexplained_context(self) -> None:
        for invalid in (
            WORKER_TERMS.replace('worker = ["workers"]', 'unknown = ["workers"]'),
            WORKER_TERMS.replace('retired = ["seat",', 'retired = ["unlisted",'),
            WORKER_TERMS + '\n[[terms.kept_contexts]]\npath = "docs/a.md"\ntext = "seat"\n',
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _ = audit.parse_terms(invalid)

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
                                   (audit.LiveFile("scripts/a.py", audit.python_prose(source).text),))
        self.assertEqual([(r.name, r.hits) for r in rows], [("worker", 2), ("seat", 2)])

    def test_shell_output_excludes_comments_and_assignments(self) -> None:
        text = '# echo "worker"\nWORKER="worker"\necho "worker has a seat" >&2\nprintf "%s\\n" "seat"\n'
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.sh", audit.shell_prose(text).text),))
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
                                   (audit.LiveFile("scripts/a.py", audit.python_prose(source).text),))
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
        strings = audit.python_prose(source).text
        self.assertEqual(set(strings.splitlines()), {"worker", "showrunner"})

    def test_python_helper_call_arguments_are_not_followed_into_parameters(self) -> None:
        source = '''label = "seat"
def display(label):
    print(label)
display("worker")
'''
        self.assertEqual(audit.python_prose(source).text, "")

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
                                   (audit.LiveFile("scripts/a.sh", audit.shell_prose(text).text),))
        self.assertEqual([row.hits for row in rows], [1, 2])

    def test_shell_counts_only_output_arguments_on_lines_with_test_operands(self) -> None:
        text = '''if [ "$role" = "worker" ]; then echo "seat"; fi
test "$role" = "worker" && printf "%s" "seat"
[[ "$role" == "worker" ]] || echo "seat"
echo "seat"; test "$role" = "worker"
'''
        rows = audit.measure_names((term("worker", "seat"),),
                                   (audit.LiveFile("scripts/a.sh", audit.shell_prose(text).text),))
        self.assertEqual([row.hits for row in rows], [0, 4])

    def test_reader_excludes_plans_tests_fixtures_and_non_script_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                "CLAUDE.md": "worker", "commands/a.md": "seat", "docs/words.md": "worker",
                "docs/plans/a.md": "seat", "skills/a/SKILL.md": "worker",
                "scripts/a.py": 'print("seat")', "scripts/test_a.py": 'print("worker")',
                "scripts/fake_tool.py": 'print("worker")', "scripts/tool_test_support.py": 'print("worker")',
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


class SourceCheckTests(unittest.TestCase):
    def test_config_history_retires_doc_displays_but_keeps_stored_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "docs/current.md": "Dispatch a worker.\n",
                "scripts/records.py": 'record = {"role": "worker"}\nif record["role"] == "worker":\n    pass\n',
            })
            config = root / "config/names.toml"
            config.parent.mkdir(exist_ok=True)
            for history, prose, expected in (
                ('["builder", "worker"]', "Dispatch a worker.\n", "worker"),
                ('["maker", "builder", "worker"]', "Dispatch a builder.\n", "builder"),
            ):
                with self.subTest(history=history):
                    _ = config.write_text(NAMES_TOML.replace('["worker"]', history), encoding="utf-8")
                    _ = (root / "docs/current.md").write_text(prose, encoding="utf-8")
                    issues = audit.check_sources(root)
                    self.assertEqual(len(issues), 1, issues)
                    self.assertIn("docs/current.md:1:", issues[0])
                    self.assertIn("retired", issues[0])
                    self.assertIn(expected, issues[0])
                    _ = (root / "docs/current.md").write_text("Dispatch a maker.\n", encoding="utf-8")
                    self.assertEqual(audit.check_sources(root), ())

    def test_whole_literal_display_labels_fail_in_python_and_stderr_at_physical_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "scripts/label.py": '# excluded comment\n\nprint("builder")\n',
                "scripts/label.sh": "# excluded comment\ncat >&2 <<'OUTPUT'\nbuilder\nOUTPUT\nconsumer <<'INPUT'\nbuilder\nINPUT\n",
                "docs/allowed.md": "A builder is a current display.\n",
                "scripts/keys.py": 'record = {"role": "worker"}\nif record["role"] == "worker":\n    pass\n',
            })
            config = root / "config/names.toml"
            config.parent.mkdir(exist_ok=True)
            _ = config.write_text(NAMES_TOML.replace('["worker"]', '["builder", "worker"]'), encoding="utf-8")
            issues = audit.check_sources(root)
            self.assertEqual(len(issues), 2, issues)
            self.assertTrue(any("scripts/label.py:3:" in issue and "builder" in issue for issue in issues), issues)
            self.assertTrue(any("scripts/label.sh:3:" in issue and "builder" in issue for issue in issues), issues)
            self.assertEqual(issues, audit.check_sources(root, full_extraction=True))
            files, errors = audit.read_live_files(root)
            self.assertEqual(errors, ())
            counts = audit.measure_names((term("builder"),), files)
            self.assertEqual(counts[0].hits, 3)

    def test_stderr_output_heredoc_counts_retired_words_and_excludes_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/probe.sh":
                "# excluded\ncat >&2 <<-'OUTPUT'\n\tseat\n\tOUTPUT\nconsumer <<'INPUT'\nseat\nINPUT\n"})
            files, errors = audit.read_live_files(root)
            self.assertEqual(errors, ())
            self.assertEqual(audit.measure_names((term("worker", "seat"),), files)[1].hits, 1)
            self.assertEqual(audit.check_sources(root), ("scripts/probe.sh:3: retired 'seat'; use 'worker'",))

    def test_output_heredocs_after_shell_boundaries_and_stderr_redirects_keep_source_lines(self) -> None:
        for command in ("cat", "true && cat", "false || cat", "true; cat", "true | cat",
                        "cat >&2", "cat 1>&2", "true && cat >&2", "false || cat 1>&2"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_check_root(root, {"scripts/output.sh":
                    f"# header\n{command} <<'OUTPUT'\nseat\nworker\nOUTPUT\nconsumer <<'INPUT'\nseat\nworker\nINPUT\n"})
                files, errors = audit.read_live_files(root)
                self.assertEqual(errors, ())
                counts = audit.measure_names((term("worker", "seat"),), files)
                self.assertEqual([(row.name, row.hits) for row in counts], [("worker", 1), ("seat", 1)])
                self.assertEqual(audit.check_sources(root), (
                    "scripts/output.sh:3: retired 'seat'; use 'worker'",
                    "scripts/output.sh:4: fixed display name 'worker'; read config/names.toml",
                ))
                self.assertEqual(audit.source_check(root), audit.source_check(root, full_extraction=True))

    def test_output_bindings_are_labels_regardless_of_the_variable_name(self) -> None:
        for variable in ("group", "group_key", "key", "job_key", "label"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_check_root(root, {"scripts/output.py": f'{variable} = "worker"\nprint({variable})\n'})
                self.assertEqual(audit.check_sources(root), (
                    "scripts/output.py:1: fixed display name 'worker'; read config/names.toml",
                ))
                self.assertEqual(audit.source_check(root), audit.source_check(root, full_extraction=True))

    def test_printed_conditionals_check_each_branch_without_checking_the_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/output.py":
                'print("worker" if ready else "designer")\n'
                + 'label = (\n    "unit director" if record["role"] == "showrunner"\n'
                + '    else "code reviewer"\n)\nprint(label)\n'
                + 'print("The worker failed." if failed else "The designer succeeded.")\n'})
            result = audit.source_check(root)
            self.assertCountEqual(result.issues, (
                "scripts/output.py:1: fixed display name 'worker'; read config/names.toml",
                "scripts/output.py:1: fixed display name 'designer'; read config/names.toml",
                "scripts/output.py:3: fixed display name 'unit director'; read config/names.toml",
                "scripts/output.py:4: fixed display name 'code reviewer'; read config/names.toml",
            ))
            self.assertCountEqual(result.sentences, (
                audit.SentenceRoleWord("scripts/output.py", 7, "worker"),
                audit.SentenceRoleWord("scripts/output.py", 7, "designer"),
            ))
            self.assertEqual(result, audit.source_check(root, full_extraction=True))

    def test_check_and_baseline_describe_missing_config_entries_in_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {})
            config = root / "config/names.toml"
            _ = config.write_text(NAMES_TOML.replace('worker = ["worker"]\n', ''), encoding="utf-8")
            expected = f"names: {config}: [jobs] has no worker entry"
            self.assertEqual(audit.check_sources(root), (expected,))
            for action in ("check", "baseline"):
                with self.subTest(action=action):
                    output, errors = StringIO(), StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        status = audit.main([action, "--root", str(root)])
                    self.assertEqual(status, 1)
                    if action == "check":
                        self.assertEqual(output.getvalue().splitlines(), [
                            expected, "role words in sentences: 0 (list with --prose)"])
                        self.assertEqual(errors.getvalue(), "")
                    else:
                        self.assertEqual(output.getvalue(), "")
                        self.assertEqual(errors.getvalue().strip(), f"roles_audit: {expected}")

    def test_conditional_bindings_keep_sentence_and_shown_name_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/output.py":
                'group = "units" if role == "unit" else "showrunners"\n'
                + 'raise ValueError(f"accounts: {group} account cannot be read")\n'
                + 'print("The " + ("worker" if ready else "designer") + " failed.")\n'
                + 'print(f"roles · {\"worker\" if ready else \"designer\"}")\n'})
            result = audit.source_check(root)
            self.assertCountEqual(result.issues, (
                "scripts/output.py:4: fixed display name 'worker'; read config/names.toml",
                "scripts/output.py:4: fixed display name 'designer'; read config/names.toml",
            ))
            self.assertCountEqual(result.sentences, (
                audit.SentenceRoleWord("scripts/output.py", 1, "showrunners"),
                audit.SentenceRoleWord("scripts/output.py", 3, "worker"),
                audit.SentenceRoleWord("scripts/output.py", 3, "designer"),
            ))
            self.assertEqual(result, audit.source_check(root, full_extraction=True))

    def test_a_label_split_between_addition_operands_is_selected_for_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/output.py": 'print("wor" + "ker")\n'})
            self.assertEqual(audit.check_sources(root), (
                "scripts/output.py:1: fixed display name 'worker'; read config/names.toml",
            ))
            self.assertEqual(audit.source_check(root), audit.source_check(root, full_extraction=True))

    def test_key_uses_stay_contracts_without_relying_on_binding_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/keys.py":
                'label = "worker"\nprint(record[label])\nprint({label: value})\nprint(label == record.role)\n'
                + 'print(record.get(label))\n'})
            self.assertEqual(audit.source_check(root), audit.SourceCheck((), (), (), ()))

    def test_concatenated_output_keeps_sentence_context_and_detects_whole_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/output.py":
                'print("The " + "worker" + " failed.")\n'
                + 'print(\n    "wor" +\n    "ker"\n)\n'
                + 'print("unit " + "director")\n'})
            result = audit.source_check(root)
            self.assertEqual(result.issues, (
                "scripts/output.py:3: fixed display name 'worker'; read config/names.toml",
                "scripts/output.py:6: fixed display name 'unit director'; read config/names.toml",
            ))
            self.assertEqual(result.sentences, (audit.SentenceRoleWord("scripts/output.py", 1, "worker"),))
            self.assertEqual(result, audit.source_check(root, full_extraction=True))

    def test_shown_name_jobs_and_status_columns_fail_the_fixed_display_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "scripts/labels.py": 'print(f"{unit} · unit director")\nslot = "impl"\nprint(f"{address} | unit director | {slot}")\n',
                "scripts/labels.sh": "cat >&2 <<'OUTPUT'\nroles · unit director\nOUTPUT\n",
                "scripts/keys.py": 'record = {"job": "worker"}\n',
            })
            issues = audit.check_sources(root)
            self.assertEqual(len(issues), 3, issues)
            for location in ("scripts/labels.py:1:", "scripts/labels.py:3:", "scripts/labels.sh:2:"):
                self.assertTrue(any(location in issue and "fixed display name 'unit director'" in issue
                                    for issue in issues), issues)
            self.assertEqual(issues, audit.check_sources(root, full_extraction=True))

    def test_help_and_error_sentences_only_count_until_prose_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/messages.py":
                '# ignored comment\nparser.add_argument("--role", help="Ask the unit director for help.")\n'
                + 'raise ValueError("The worker record cannot be read.")\n'})
            self.assertEqual(audit.check_sources(root), ())
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root)])
            count_line = "role words in sentences: 2 (list with --prose)"
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), [count_line])
            detailed = StringIO()
            with redirect_stdout(detailed):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(detailed.getvalue().splitlines()[0], "role words in sentences: 2")
            self.assertCountEqual(detailed.getvalue().splitlines()[1:], [
                "scripts/messages.py:2: 'unit director'",
                "scripts/messages.py:3: 'worker'",
            ])

    def test_account_group_keys_are_neither_label_failures_nor_sentence_uses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/groups.py":
                'group = "units" if role == "unit" else "showrunners"\nprint(accounts[group])\n'})
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "role words in sentences: 0",
            ])
            self.assertEqual(audit.check_sources(root), audit.check_sources(root, full_extraction=True))

    def test_formatted_error_sentence_keeps_its_context_instead_of_becoming_a_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/error.py":
                'raise ValueError(f"showrunner {runner.slug} is not running")\n'})
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "role words in sentences: 1",
                "scripts/error.py:1: 'showrunner'",
            ])
            self.assertEqual(audit.check_sources(root), audit.check_sources(root, full_extraction=True))

    def write_check_root(self, root: Path, sources: dict[str, str]) -> None:
        config = root / "config/names.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(NAMES_TOML, encoding="utf-8")
        inventory = root / "scripts/production/role_terms.toml"
        inventory.parent.mkdir(parents=True)
        _ = inventory.write_text(WORKER_TERMS, encoding="utf-8")
        for name, source in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(source, encoding="utf-8")

    def test_check_selects_python_literal_joins_and_decoded_escapes(self) -> None:
        control = 'print("Dispatch a process.")\n'
        cases = (
            ('print("se" "at")\n', 1),
            ('print(\n    "se"\n    "at"\n)\n', 2),
            (r'print("\x73eat")' + "\n", 1),
            (r'print("\N{LATIN SMALL LETTER S}eat")' + "\n", 1),
        )
        for source, line in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_check_root(root, {"scripts/joined.py": source,
                                             "scripts/control.py": control})
                with patch.object(audit, "python_prose", wraps=audit.python_prose) as extract:
                    issues = audit.check_sources(root)
                self.assertEqual(issues, (f"scripts/joined.py:{line}: retired 'seat'; use 'worker'",))
                self.assertNotIn(call(control), extract.call_args_list,
                                 "plain control must skip full prose extraction")

    def test_check_preserves_shell_quotes_and_tab_stripped_heredocs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = 'echo "Dispatch a process."\n'
            self.write_check_root(root, {
                "scripts/quoted.sh": 'echo "seat"\n',
                "scripts/prompt.sh": "cat <<-'PROMPT'\n\tCodex seats\n\tPROMPT\n",
                "scripts/control.sh": control,
            })
            with patch.object(audit, "shell_prose", wraps=audit.shell_prose) as extract:
                issues = audit.check_sources(root)
            self.assertEqual(issues, (
                "scripts/prompt.sh:2: retired 'Codex seats'; use 'worker'",
                "scripts/quoted.sh:1: retired 'seat'; use 'worker'",
            ))
            self.assertNotIn(call(control), extract.call_args_list,
                             "plain control must skip full prose extraction")

    def test_check_reports_read_and_syntax_errors_without_a_raw_retired_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "scripts/broken.py": 'print("Dispatch a worker."\n',
                "scripts/control.py": 'print("Dispatch a worker.")\n',
            })
            _ = (root / "scripts/undecodable.py").write_bytes(b"\xff\n")
            issues = audit.check_sources(root)
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root)])
            self.assertEqual(len(issues), 2)
            self.assertTrue(any(issue.startswith("scripts/broken.py:") for issue in issues), issues)
            self.assertTrue(any(issue.startswith("scripts/undecodable.py:") for issue in issues), issues)
            self.assertEqual(status, 1)
            self.assertIn("scripts/broken.py:", output.getvalue())
            self.assertIn("scripts/undecodable.py:", output.getvalue())

    def test_check_covers_non_git_roots_and_untracked_git_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "scripts/untracked.py": 'print("Dispatch a seat.")\n',
                "scripts/control.py": 'print("Dispatch a worker.")\n',
            })
            expected = ("scripts/untracked.py:1: retired 'seat'; use 'worker'",)
            self.assertEqual(audit.check_sources(root), expected)
            _ = subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            self.assertEqual(audit.check_sources(root), expected)

    def test_repository_candidate_check_matches_full_extraction(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        # The full-tree reference found no findings or read issues. Keep its
        # protocol/history files and each source format in this bounded sample;
        # the separate repository check still covers every live source.
        paths = (
            "config/names.toml",
            "scripts/production/role_terms.toml",
            "docs/as-built/shutdown-and-restart.md",
            "docs/as-built/message-clarity.md",
            "docs/as-built/usage-ledger.md",
            "docs/as-built/build-memory-admission.md",
            "docs/production_format.md",
            "scripts/production/rename_unit.py",
            "scripts/direct/implement.sh",
            "scripts/usage/collect.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in paths:
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_bytes((repository / name).read_bytes())
            self.assertEqual(audit.check_sources(root), audit.check_sources(root, full_extraction=True))

    def test_formatted_string_fragments_keep_prose_when_they_cannot_tokenize(self) -> None:
        sources = {
            "scripts/apostrophe.py": 'print(f"Dispatch a seat\'s work to {name}.")\n',
            "scripts/parenthesis.py": 'print(f"Dispatch a seat ({name})")\n',
            "scripts/spanning.py": 'print(\n    f"{name} — dispatch a seat: "\n    f"add ~/rust/hanadocs/agents/{name}"\n)\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(source, encoding="utf-8")
            files, issues = audit.read_live_files(root)
            terms = audit.parse_terms(WORKER_TERMS)
            uses = audit.check_retired(terms, files)
            rows = audit.measure_names(terms, files)
        self.assertEqual(issues, ())
        self.assertEqual([(use.path, use.line, use.name) for use in uses], [
            ("scripts/apostrophe.py", 1, "seat"),
            ("scripts/parenthesis.py", 1, "seat"),
            ("scripts/spanning.py", 2, "seat"),
        ])
        self.assertEqual(next(row.hits for row in rows if row.name == "seat"), 3)

    def test_script_findings_report_source_lines_after_excluded_text(self) -> None:
        sources = {
            "scripts/concatenated.py": 'print(\n    "Dispatch "\n    "a seat."\n)\n',
            "scripts/concatenated_parts.py": 'print(\n    "A seat, "\n    "then seats."\n)\n',
            "scripts/concatenated_path.py": 'print(\n    "Read docs/workers.md: "\n    "dispatch a seat."\n)\n',
            "scripts/dispatch.py": '# A dispatch helper.\n\nprint("Dispatch a seat.")\n',
            "scripts/dispatch.sh": '# A dispatch helper.\n\necho "Dispatch a seat."\n',
            "scripts/prompt.sh": '# A dispatch helper.\n\ncat <<\'PROMPT\'\nDispatch Codex seats.\nPROMPT\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(source, encoding="utf-8")
            files, issues = audit.read_live_files(root)
            uses = audit.check_retired(audit.parse_terms(WORKER_TERMS), files)
        self.assertEqual(issues, ())
        self.assertEqual([(use.path, use.line, use.name) for use in uses], [
            ("scripts/concatenated.py", 3, "seat"),
            ("scripts/concatenated_parts.py", 2, "seat"),
            ("scripts/concatenated_parts.py", 3, "seats"),
            ("scripts/concatenated_path.py", 3, "seat"),
            ("scripts/dispatch.py", 3, "seat"),
            ("scripts/dispatch.sh", 3, "seat"),
            ("scripts/prompt.sh", 4, "Codex seats"),
        ])

    def test_source_check_detects_a_returning_use_without_reading_home_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config/names.toml"
            config.parent.mkdir()
            _ = config.write_text(NAMES_TOML, encoding="utf-8")
            data = root / "scripts/production/role_terms.toml"
            data.parent.mkdir(parents=True)
            _ = data.write_text(WORKER_TERMS, encoding="utf-8")
            source = root / "docs/words.md"
            source.parent.mkdir()
            _ = source.write_text("A worker keeps its thread.\n", encoding="utf-8")
            with patch.object(Path, "home", side_effect=AssertionError("source check must not read home")), \
                    patch.object(showrunners, "timer_docs", side_effect=AssertionError("source check must not read productions")):
                self.assertEqual(audit.check_sources(root), ())
                _ = source.write_text("A worker keeps its thread.\nDispatch Codex seats.\n", encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(audit.main(["check", "--root", str(root)]), 1)
                self.assertIn("docs/words.md:2: retired 'Codex seats'; use 'worker'", output.getvalue())
                _ = source.write_text("Dispatch workers.\n", encoding="utf-8")
                with redirect_stdout(StringIO()):
                    self.assertEqual(audit.main(["check", "--root", str(root)]), 0)

    def test_repository_sources_have_no_unkept_retired_names(self) -> None:
        root = Path(__file__).resolve().parents[2]
        issues = audit.check_sources(root)
        self.assertEqual(issues, (), "\n".join(issues))

    def test_term_definitions_point_to_existing_repository_files(self) -> None:
        root = Path(__file__).resolve().parents[2]
        terms = audit.parse_terms((root / "scripts/production/role_terms.toml").read_text(encoding="utf-8"))
        for item in terms:
            for definition in item.defined_in:
                with self.subTest(term=item.name, definition=definition):
                    self.assertTrue((root / definition.split("#", 1)[0]).is_file(), definition)

    def test_retired_pool_name_flags_a_new_use_outside_its_kept_contexts(self) -> None:
        root = Path(__file__).resolve().parents[2]
        terms = audit.parse_terms((root / "scripts/production/role_terms.toml").read_text(encoding="utf-8"))
        uses = audit.check_retired(terms, (audit.LiveFile("docs/new-review.md", "Use the review pool."),))
        self.assertEqual([(use.path, use.line, use.name) for use in uses],
                         [("docs/new-review.md", 1, "review pool")])

    def test_retired_design_role_names_fail_the_live_source_check(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        data = (repository / "scripts/production/role_terms.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config/names.toml"
            config.parent.mkdir()
            _ = config.write_text(NAMES_TOML, encoding="utf-8")
            terms = root / "scripts/production/role_terms.toml"
            terms.parent.mkdir(parents=True)
            _ = terms.write_text(data, encoding="utf-8")
            document = root / "docs/roles.md"
            document.parent.mkdir()
            _ = document.write_text("A held design role keeps its thread.\n", encoding="utf-8")
            with patch.object(Path, "home", side_effect=AssertionError("source checks must not read home")):
                with redirect_stdout(StringIO()):
                    self.assertEqual(audit.main(["check", "--root", str(root)]), 0)
                for name in ("guild", "guilds", "guild head", "guild heads"):
                    with self.subTest(name=name):
                        _ = document.write_text(f"Use the {name}.\n", encoding="utf-8")
                        output = StringIO()
                        with redirect_stdout(output):
                            self.assertEqual(audit.main(["check", "--root", str(root)]), 1)
                        self.assertEqual(output.getvalue().splitlines(), [
                            f"docs/roles.md:1: retired {name!r}; use 'held design role'",
                            "role words in sentences: 0 (list with --prose)",
                        ])


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
        counts = audit.measure_sent_contacts(source.records, audit.AuditWindow(start, end))
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
                              (), source, audit.AuditWindow(start, start + timedelta(days=1)), (),
                              source_result=audit.SourceCheck((), (), (), ()), unread_production_docs=())
        self.assertIn("2 / 3", result)
        self.assertIn("66.7%", result)
        self.assertIn("role unknown (n=1)", result)
        self.assertIn("Request intent is not recorded.", result)
        self.assertIn("natedev only", result)
        self.assertIn("2026-09-30", result)
        self.assertIn("live: n=1 files", result)
        self.assertNotIn("\nNames\n", result)

    def test_baseline_reads_fixture_productions_and_reports_unavailable_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config/names.toml"
            config.parent.mkdir()
            _ = config.write_text(NAMES_TOML, encoding="utf-8")
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
            self.assertIn("invalid unit format", result)
            self.assertIn("bad", result)
            self.assertIn("has no declared definition", result)
            self.assertIn("unavailable:", result)
            self.assertIn("Sent contacts: unknown (n=0)", result)
            self.assertIn(f"Warning: {root / '.local/state/message/log.jsonl'}:", result)


class BaselineReportTests(unittest.TestCase):
    def write_fixture(self, root: Path) -> audit.AuditWindow:
        paths = {
            "config/names.toml": NAMES_TOML,
            "scripts/production/role_terms.toml": '''[[terms]]
name = "worker"
kind = "role"
defined_in = "docs/words.md#Words"
other_names = ["seat", "delegate"]
[[terms]]
name = "gate"
kind = "step"
defined_in = "none"
other_names = []
''',
            "docs/words.md": "# Words\nworker seat seat delegate\n",
            "scripts/output.py": 'print("worker")\n',
            "fixture-production.md": "## Units\n| Unit | Plan |\n| --- | --- |\n| bad | a.md |\n",
        }
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        paths[".local/state/message/log.jsonl"] = message(at)
        for relative, text in paths.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(text, encoding="utf-8")
        return audit.AuditWindow(at, at + timedelta(days=1))

    def test_report_rejects_omitted_source_check_evidence(self) -> None:
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        # Exercise runtime callers that bypass the required keyword's type check.
        unchecked_report = cast(Callable[..., str], audit.report)
        with self.assertRaisesRegex(TypeError, "source_result"):
            _ = unchecked_report((), (), (), audit.parse_messages(""),
                                 audit.AuditWindow(at, at + timedelta(days=1)), ())

    def test_report_shows_zero_labels_after_a_successful_source_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            _ = (root / "scripts/output.py").write_text('print("Ready.")\n', encoding="utf-8")
            checked = audit.source_check(root)
            self.assertEqual(checked.labels, ())
            self.assertEqual(checked.unread_sources, ())
            result = audit.report((), (), (), audit.parse_messages(""), window, (),
                                  source_result=checked, unread_production_docs=())
            self.assertIn("Fixed display labels: n=0 to fix;", result)
            self.assertNotIn("sources not read", result)

    def test_unread_sources_make_zero_label_and_production_counts_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            _ = (root / "scripts/output.py").write_text('print("Ready.")\n', encoding="utf-8")
            _ = (root / "scripts/bad.py").write_bytes(b"\xff")
            unreadable_doc = root / "unreadable-production.md"
            _ = unreadable_doc.write_bytes(b"\xff")
            missing_doc = root / "missing-production.md"
            checked = audit.source_check(root)
            self.assertEqual([source.path for source in checked.unread_sources], ["scripts/bad.py"])
            with patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={"unreadable": unreadable_doc, "missing": missing_doc}):
                result = audit.baseline(root, window)
            label_line = next(line for line in result.splitlines() if line.startswith("Fixed display labels:"))
            production_line = next(line for line in result.splitlines() if line.startswith("Unit and session names:"))
            self.assertEqual(label_line, "Fixed display labels: n=0 to fix; n=1 sources not read; "
                             + "role words in sentences: n=0 (information only; live: n=2 files; n=1 sources not read).")
            self.assertEqual(production_line, "Unit and session names: n=0 findings across n=0 production docs "
                             + "(including retired units); n=2 sources not read.")
            for prefix in ("Other names:", "Most used other names:", "Definitions:", "Files:"):
                line = next(line for line in result.splitlines() if line.startswith(prefix))
                self.assertIn("n=1 sources not read", line)
            for path in ("scripts/bad.py", str(unreadable_doc), str(missing_doc)):
                self.assertTrue(any(line.startswith("Warning:") and path in line for line in result.splitlines()), result)

    def test_baseline_keeps_first_read_failure_in_name_and_label_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            output = root / "scripts/output.py"
            original_read = Path.read_text
            output_reads = 0

            def read_fixture(path: Path, encoding: str = "utf-8") -> str:
                nonlocal output_reads
                if path == output:
                    output_reads += 1
                    if output_reads == 1:
                        raise OSError("fixture source temporarily unavailable")
                return original_read(path, encoding=encoding)

            with patch.object(Path, "read_text", new=read_fixture), \
                    patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={}):
                result = audit.baseline(root, window)
            lines = result.splitlines()
            self.assertIn("Other names: 3 / 4 (75.0%; live: n=1 files; n=1 sources not read).", lines)
            self.assertIn("Fixed display labels: n=0 to fix; n=1 sources not read; "
                          + "role words in sentences: n=0 (information only; live: n=1 files; n=1 sources not read).", lines)
            self.assertIn("Warning: scripts/output.py: fixture source temporarily unavailable", lines)
            self.assertFalse(any(line.startswith("Fix: scripts/output.py:") for line in lines), result)
            self.assertEqual(output_reads, 1)

    def test_fixture_baseline_starts_with_share_worst_names_and_actionable_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            with patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={"fixture": root / "fixture-production.md"}):
                result = audit.baseline(root, window)
            self.assertEqual(result.splitlines()[:2], [
                "Other names: 3 / 5 (60.0%; live: n=2 files).",
                "Most used other names: 'seat' n=2 (worker); 'delegate' n=1 (worker) (live: n=2 files).",
            ])
            self.assertIn("Definition: 'gate' has no declared definition.", result)
            self.assertIn("Name: 'bad' has an invalid unit format in fixture.", result)
            self.assertIn("Fixed display labels: n=1 to fix; role words in sentences: n=0", result)
            self.assertIn("Fix: scripts/output.py:1: fixed display name 'worker'; read config/names.toml", result)
            self.assertIn(f"Message window: {window.label()} (natedev only).", result)
            self.assertIn("role unknown (n=1)", result)
            self.assertIn("Request intent is not recorded.", result)

    def test_baseline_cli_prints_full_tables_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = self.write_fixture(root)
            arguments = ["baseline", "--root", str(root), "--since", "2026-10-01", "--until", "2026-10-01"]
            with patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={"fixture": root / "fixture-production.md"}):
                short, detailed = StringIO(), StringIO()
                with redirect_stdout(short):
                    self.assertEqual(audit.main(arguments), 0)
                with redirect_stdout(detailed):
                    self.assertEqual(audit.main([*arguments, "--tables"]), 0)
            self.assertTrue(detailed.getvalue().startswith(short.getvalue().rstrip() + "\n\n"))
            for heading in ("Roles baseline", "Names", "Source issues"):
                self.assertNotIn(heading, short.getvalue().splitlines())
                self.assertIn(heading, detailed.getvalue().splitlines())
            self.assertIn("Definitions (declared sources", detailed.getvalue())
            self.assertIn("Unit and session names — natedev only", detailed.getvalue())
            self.assertIn("Sent contacts — natedev only; request intent not recorded", detailed.getvalue())
            self.assertNotIn("Wrong-party asks", detailed.getvalue())
            self.assertIn(audit.ROLE_SOURCE, detailed.getvalue())
            self.assertIn("helper call arguments are not followed into parameters", detailed.getvalue())

    def test_sent_contact_tables_drop_zero_and_unknown_only_rows_without_hiding_coverage(self) -> None:
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = audit.parse_messages("\n".join([
            message(at, sender="zero-only", from_role="worker", from_production="one"),
            message(at, sender="unknown-only"),
            message(at, sender="known-director", from_role="unit director", from_production="one"),
            message(at, sender="known-other-production", recipient="runner", from_role="unit director",
                    from_production="one", to_role="showrunner", to_production="two"),
            message(at, sender="unknown-production", recipient="runner", from_role="unit director", to_role="showrunner"),
            message(at, sender="unknown-machine", machine=""),
        ]))
        window = audit.AuditWindow(at, at + timedelta(days=1))
        coverage = audit.measure_sent_contacts(source.records, window)
        self.assertIsInstance(coverage, audit.SentContactCoverage)
        result = audit.report((), (), (), source, window, (), tables=True,
                              source_result=audit.SourceCheck((), (), (), ()), unread_production_docs=())
        contacts = result.split("Sent contacts — natedev only; request intent not recorded\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("known-director", contacts)
        self.assertIn("known-other-production", contacts)
        for dropped in ("zero-only", "unknown-only", "unknown-production"):
            self.assertNotIn(dropped, contacts)
        self.assertIn("role unknown (n=1)", result)
        self.assertIn("production unknown (n=1)", result)
        self.assertIn("machine unknown (n=1)", result)
        self.assertIn("n=5 sent / n=6 window records", result)

    def test_short_summary_names_each_conflicting_definition_and_shared_unit(self) -> None:
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        result = audit.report(
            (term("worker", defined_in=("docs/a.md#Words", "docs/b.md#Words")),), (),
            (audit.ProductionNames("one", "one.md", "runner-one", ("shared-unit",)),
             audit.ProductionNames("two", "two.md", "runner-two", ("shared-unit",))),
            audit.parse_messages(""), audit.AuditWindow(at, at + timedelta(days=1)), (),
            source_result=audit.SourceCheck((), (), (), ()), unread_production_docs=())
        self.assertIn("Definitions: n=1 findings across n=1 terms.", result)
        self.assertIn("Definition: 'worker' has definitions in docs/a.md, docs/b.md.", result)
        self.assertIn("Unit and session names: n=1 findings across n=2 production docs", result)
        self.assertIn("Name: 'shared-unit' is shared by one, two.", result)

    def test_unreadable_sources_have_individual_warnings_in_the_short_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            (root / ".local/state/message/log.jsonl").unlink()
            _ = (root / "scripts/bad.py").write_bytes(b"\xff")
            missing_doc = root / "missing-production.md"
            with patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={"missing": missing_doc}):
                result = audit.baseline(root, window)
            for path in ("scripts/bad.py", str(missing_doc), str(root / ".local/state/message/log.jsonl")):
                self.assertTrue(any(line.startswith("Warning:") and path in line for line in result.splitlines()), result)
            self.assertIn("Sent contacts: unknown (n=0); message log unavailable:", result)
            self.assertNotIn("Sent contacts: n=0 sent", result)

    def test_sentence_only_baseline_reports_information_without_label_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = self.write_fixture(root)
            _ = (root / "scripts/output.py").write_text('print("Ask the worker for help.")\n', encoding="utf-8")
            with patch.object(Path, "home", return_value=root), \
                    patch.object(showrunners, "timer_docs", return_value={}):
                result = audit.baseline(root, window)
            self.assertIn("Fixed display labels: n=0 to fix; role words in sentences: n=1 (information only; live: n=2 files).", result)
            self.assertFalse(any(line.startswith("Fix:") for line in result.splitlines()), result)


if __name__ == "__main__":
    _ = unittest.main()
