"""Fixture tests for retired names and source coverage."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch


from scripts.production import roles_audit as audit, showrunners
from scripts.lib.test_names import NAMES_TOML, fixture_names


def term(name: str, *others: str, kept: tuple[str, ...] = (),
         inflected_forms: tuple[audit.NameInflection, ...] = (), retired: tuple[str, ...] = (),
         kept_contexts: tuple[audit.KeptContext, ...] = ()) -> audit.Term:
    return audit.Term(name, others, kept,
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


def retired_prose_locations(path: str, prose: audit.SourceProse) -> list[tuple[str, int, str]]:
    terms = (term("maker", "worker", "seat", retired=("worker", "seat")),)
    files = (audit.LiveFile(path, prose.text, prose.source_lines),)
    return [(use.path, use.line, use.name) for use in audit.check_retired(terms, files)]


class NameTests(unittest.TestCase):
    def test_check_reports_longest_name_once(self) -> None:
        terms = (*audit.parse_terms(WORKER_TERMS), term("gate", "/unit:direct"))
        files = (audit.LiveFile("docs/a.md", "worker workers seat seats CODEX SEAT Codex seats "
                                + "delegate delegates Codex worker Codex workers gate gates "
                                + "/unit:direct /unit:directs seatbelt seats-unit"),)
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

    def test_retired_candidates_keep_boundaries_and_spaces_created_by_masking(self) -> None:
        terms = (term("worker", "seat", kept=("x",), retired=("seat",)),
                 term("held design role", "guild head", kept=("x",), retired=("guild head",)))
        files = (audit.LiveFile("docs/a.md", "Ready.\nxseat\nguildxhead\nguild/head\n"),)
        self.assertEqual([(use.line, use.name) for use in audit.check_retired(terms, files)],
                         [(2, "seat"), (3, "guild head")])

    def test_scoped_protocol_history_and_legacy_contexts_keep_retired_uses(self) -> None:
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

    def test_retired_uses_keep_file_boundaries_and_exclude_paths_and_kept_commands(self) -> None:
        terms = (
            term("maker", "worker", "seat", kept=("/keep:seat", "docs/seat/"), retired=("worker", "seat")),
            term("/unit:assign", "/unit:direct", "/unit:delegate", retired=("/unit:direct", "/unit:delegate")),
        )
        files = (audit.LiveFile("commands/a.md", "Worker worker. seat seatbelt worker-unit /tmp/worker.md "
                                + "docs/seat/worker.md worker.md /keep:seat /unit:direct /unit:delegate"),
                 audit.LiveFile("docs/b.md", "seat Worker"))
        self.assertEqual([(use.path, use.line, use.name) for use in audit.check_retired(terms, files)], [
            ("commands/a.md", 1, "Worker"), ("commands/a.md", 1, "worker"),
            ("commands/a.md", 1, "seat"), ("docs/b.md", 1, "seat"), ("docs/b.md", 1, "Worker"),
            ("commands/a.md", 1, "/unit:direct"), ("commands/a.md", 1, "/unit:delegate"),
        ])

    def test_retired_python_prose_excludes_code_comments_docstrings_keys_and_paths(self) -> None:
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
        self.assertCountEqual(retired_prose_locations("scripts/a.py", audit.python_prose(source)), [
            ("scripts/a.py", 6, "worker"),
            ("scripts/a.py", 7, "seat"),
            ("scripts/a.py", 8, "worker"),
            ("scripts/a.py", 8, "seat"),
        ])

    def test_retired_shell_output_excludes_comments_and_assignments(self) -> None:
        text = '# echo "worker"\nWORKER="worker"\necho "worker has a seat" >&2\nprintf "%s\\n" "seat"\n'
        self.assertCountEqual(retired_prose_locations("scripts/a.sh", audit.shell_prose(text)), [
            ("scripts/a.sh", 3, "worker"),
            ("scripts/a.sh", 3, "seat"),
            ("scripts/a.sh", 4, "seat"),
        ])

    def test_retired_python_output_follows_variable_labels_and_function_returns(self) -> None:
        source = '''labels = {"worker": "seat"}
label = "worker"
def choose():
    return label
print(labels["worker"], choose())
subprocess.run(["echo", "worker has a seat"])
'''
        self.assertCountEqual(retired_prose_locations("scripts/a.py", audit.python_prose(source)), [
            ("scripts/a.py", 1, "seat"),
            ("scripts/a.py", 2, "worker"),
        ])

    def test_retired_shell_output_heredocs_exclude_input_body_commands(self) -> None:
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
        self.assertCountEqual(retired_prose_locations("scripts/a.sh", audit.shell_prose(text)), [
            ("scripts/a.sh", 2, "worker"),
            ("scripts/a.sh", 3, "seat"),
            ("scripts/a.sh", 6, "seat"),
        ])

    def test_retired_shell_output_excludes_test_operands(self) -> None:
        text = '''if [ "$role" = "worker" ]; then echo "seat"; fi
test "$role" = "worker" && printf "%s" "seat"
[[ "$role" == "worker" ]] || echo "seat"
echo "seat"; test "$role" = "worker"
'''
        self.assertCountEqual(retired_prose_locations("scripts/a.sh", audit.shell_prose(text)), [
            ("scripts/a.sh", 1, "seat"),
            ("scripts/a.sh", 2, "seat"),
            ("scripts/a.sh", 3, "seat"),
            ("scripts/a.sh", 4, "seat"),
        ])

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

    def test_nested_helper_returns_keep_their_scope_and_physical_lines(self) -> None:
        source = '''def outer():
    def shown():
        return "worker"
    def hidden():
        return "tester"
    return shown()
print(outer())
'''
        prose = audit.python_prose(source)
        self.assertEqual(audit.check_fixed_display(fixture_names(), (
            audit.LiveFile("scripts/nested.py", prose.text, prose.source_lines,
                           prose.printed_text, prose.printed_lines),)), (
                audit.DisplayLabel("scripts/nested.py", 3, "worker"),))

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
        uses = audit.check_retired((term("maker", "worker", "seat", retired=("worker", "seat")),), files)
        self.assertEqual([(use.path, use.line, use.name) for use in uses], [
            ("CLAUDE.md", 1, "worker"), ("commands/a.md", 1, "seat"),
            ("docs/words.md", 1, "worker"), ("scripts/a.py", 1, "seat"),
            ("skills/a/SKILL.md", 1, "worker"),
        ])

    def test_terms_support_kept_names_and_reject_invalid_data(self) -> None:
        text = '''[[terms]]
name = "worker"
kind = "role"
defined_in = "docs/a.md#Words; docs/b.md#Words"
other_names = ["seat"]
kept = ["docs/seat/"]
'''
        self.assertEqual(audit.parse_terms(text),
                         (term("worker", "seat", kept=("docs/seat/",)),))
        for invalid, message in (
            (text.replace('kind = "role"', 'kind = "other"'), "worker: invalid kind"),
            (text.replace('defined_in = "docs/a.md#Words; docs/b.md#Words"', 'defined_in = ""'),
             "worker: defined_in must name a file and section, or none"),
            (text + 'other_meaning_files = [2]\n', "worker.other_meaning_files must contain nonempty strings"),
            (text.replace('["seat"]', '[2]'), "worker.other_names must contain nonempty strings"),
            (text + text, "defined term names must be unique"),
            (text.replace('kind = "role"', 'kind = "other"').replace('defined_in = ', 'defined_in = 2 #'),
             "worker: invalid kind"),
            (text.replace('defined_in = ', 'defined_in = 2 #') + 'other_meaning_files = [2]\n',
             "worker: defined_in must name a file and section, or none"),
            (text.replace('["docs/seat/"]', '[2]') + 'other_meaning_files = [2]\n',
             "worker.kept must contain nonempty strings"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError) as raised:
                    _ = audit.parse_terms(invalid)
                self.assertEqual(str(raised.exception), message)


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
            errors = audit.read_live_files(root)[1]
            self.assertEqual(errors, ())

    def test_stderr_output_heredoc_counts_retired_words_and_excludes_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/probe.sh":
                "# excluded\ncat >&2 <<-'OUTPUT'\n\tseat\n\tOUTPUT\nconsumer <<'INPUT'\nseat\nINPUT\n"})
            errors = audit.read_live_files(root)[1]
            self.assertEqual(errors, ())
            self.assertEqual(audit.check_sources(root), ("scripts/probe.sh:3: retired 'seat'; use 'worker'",))

    def test_output_heredocs_after_shell_boundaries_and_stderr_redirects_keep_source_lines(self) -> None:
        for command in ("cat", "true && cat", "false || cat", "true; cat", "true | cat",
                        "cat >&2", "cat 1>&2", "true && cat >&2", "false || cat 1>&2"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_check_root(root, {"scripts/output.sh":
                    f"# header\n{command} <<'OUTPUT'\nseat\nworker\nOUTPUT\nconsumer <<'INPUT'\nseat\nworker\nINPUT\n"})
                errors = audit.read_live_files(root)[1]
                self.assertEqual(errors, ())
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

    def test_check_describes_missing_config_entries_in_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {})
            config = root / "config/names.toml"
            _ = config.write_text(NAMES_TOML.replace('worker = ["worker"]\n', ''), encoding="utf-8")
            expected = f"names: {config}: [jobs] has no worker entry"
            self.assertEqual(audit.check_sources(root), (expected,))
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = audit.main(["check", "--root", str(root)])
            self.assertEqual(status, 1)
            self.assertEqual(output.getvalue().splitlines(), [expected])
            self.assertEqual(errors.getvalue(), "")

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

    def test_plain_selection_keeps_every_extracted_string_assembly(self) -> None:
        cases = {
            "conditional suffix binding": 'ending = "ker" if ready else "kers"\nprint("wor" + ending)\n',
            "conditional prefix binding": 'prefix = "wor" if ready else "tes"\nprint(prefix + "ker")\n',
            "annotated binding": 'ending: str = "ker" if ready else "kers"\nprint("wor" + ending)\n',
            "binding alias": 'ending = "ker" if ready else "kers"\nalias = ending\nprint("wor" + alias)\n',
            "lexical binding": 'ending = "ker" if ready else "kers"\ndef show():\n    print("wor" + ending)\n',
            "helper return": 'def label():\n    return "wor" + "ker"\nprint(label())\n',
            "direct branches": 'print("wor" + ("ker" if ready else "kers"))\n',
            "f-string suffix binding": 'ending = "ker" if ready else "kers"\nprint(f"wor{ending}")\n',
            "f-string prefix binding": 'prefix = "wor" if ready else "tes"\nprint(f"{prefix}ker")\n',
            "f-string branches": 'print(f"wor{\"ker\" if ready else \"kers\"}")\n',
            "nested joins": 'ending = "ker" if ready else "kers"\nprint("w" + ("or" + ending))\n',
            "nested fields": 'ending = "ker" if ready else "kers"\nprint(f"w{f\"or{ending}\"}")\n',
            "nested branches": 'ending = ("ker" if first else "kers") if ready else "ker"\nprint("wor" + ending)\n',
            "literal join": 'print("wor" + "ker")\n',
        }
        for name, source in cases.items():
            with self.subTest(assembly=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_check_root(root, {"scripts/output.py": source})
                complete = audit.source_check(root, full_extraction=True, prose=False)
                self.assertTrue(complete.labels, "The fixture must produce a display finding.")
                terms = audit.parse_terms((root / "scripts/production/role_terms.toml").read_text())
                retired = audit.forms_pattern(tuple(form for item in terms for form in item.retired))
                self.assertTrue(audit.extraction_candidate(source, True, retired, frozenset({"worker"}), prose=False))
                self.assertEqual(audit.source_check(root, prose=False), complete)

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
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue(), "")
            detailed = StringIO()
            with redirect_stdout(detailed):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(detailed.getvalue().splitlines()[0], "role words in sentences: 2")
            self.assertCountEqual(detailed.getvalue().splitlines()[1:], [
                "scripts/messages.py:2: 'unit director'",
                "scripts/messages.py:3: 'worker'",
            ])

    def test_plain_check_keeps_findings_and_diagnostics_without_sentence_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {
                "scripts/sentences.py": 'print("The worker needs help.")\n',
                "scripts/labels.py": 'print("wor" + "ker")\nprint(f"{name} · designer")\n',
                "scripts/escaped.py": r'print("Dispatch a \x73eat.")' + "\n",
                "scripts/conditional.py": 'print(f"wor{"ker" if ready else "kers"}")\n',
                "scripts/broken.py": "print(\n",
            })
            detailed = audit.source_check(root)
            plain = audit.source_check(root, prose=False)
            self.assertEqual(detailed, audit.source_check(root, full_extraction=True))
            self.assertEqual(plain.issues, detailed.issues)
            self.assertEqual(plain.labels, detailed.labels)
            self.assertEqual(plain.unread_sources, detailed.unread_sources)
            self.assertEqual(plain.sentences, ())
            self.assertTrue(detailed.sentences)
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root)])
            self.assertEqual(status, 1)
            self.assertEqual(output.getvalue().splitlines(), list(detailed.issues))

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
                'raise ValueError(f"showrunner {runner.name} is not running")\n'})
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "role words in sentences: 1",
                "scripts/error.py:1: 'showrunner'",
            ])
            self.assertEqual(audit.check_sources(root), audit.check_sources(root, full_extraction=True))

    def write_check_root(self, root: Path, sources: dict[str, str], terms: str = WORKER_TERMS) -> None:
        config = root / "config/names.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text(NAMES_TOML, encoding="utf-8")
        inventory = root / "scripts/production/role_terms.toml"
        inventory.parent.mkdir(parents=True)
        _ = inventory.write_text(terms, encoding="utf-8")
        for name, source in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(source, encoding="utf-8")

    def write_source_coverage_root(self, root: Path) -> None:
        self.write_check_root(root, {
            "docs/words.md": "# Words\nworker seat seat delegate\n",
            "scripts/output.py": 'print("worker")\n',
            "fixture-production.md": "# Fixture\n",
        }, terms='''[[terms]]
name = "worker"
kind = "role"
defined_in = "docs/words.md#Words"
other_names = ["seat", "delegate"]
[[terms]]
name = "gate"
kind = "step"
defined_in = "none"
other_names = []
''')

    def test_check_has_no_label_findings_after_successful_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
            _ = (root / "scripts/output.py").write_text('print("Ready.")\n', encoding="utf-8")
            self.assertEqual(audit.check_sources(root), ())

    def test_check_reports_unread_source_without_reading_production_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
            _ = (root / "scripts/output.py").write_text('print("Ready.")\n', encoding="utf-8")
            _ = (root / "scripts/bad.py").write_bytes(b"\xff")
            unreadable_doc = root / "unreadable-production.md"
            _ = unreadable_doc.write_bytes(b"\xff")
            missing_doc = root / "missing-production.md"
            with patch.object(showrunners, "timer_docs", return_value={"unreadable": unreadable_doc, "missing": missing_doc}):
                issues = audit.check_sources(root)
            self.assertEqual(len(issues), 1, issues)
            self.assertRegex(issues[0], r"^scripts/bad\.py: .*codec can't decode")

    def test_check_keeps_first_read_failure_without_reopening_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
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

            with patch.object(Path, "read_text", new=read_fixture):
                issues = audit.check_sources(root)
            self.assertEqual(issues, ("scripts/output.py: fixture source temporarily unavailable",))
            self.assertEqual(output_reads, 1)

    def test_check_reports_the_fixed_label_from_the_former_report_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
            self.assertEqual(audit.check_sources(root), (
                "scripts/output.py:1: fixed display name 'worker'; read config/names.toml",
            ))

    def test_check_keeps_label_findings_beside_source_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
            _ = (root / "scripts/bad.py").write_bytes(b"\xff")
            missing_doc = root / "missing-production.md"
            with patch.object(showrunners, "timer_docs", return_value={"missing": missing_doc}):
                issues = audit.check_sources(root)
            self.assertEqual(len(issues), 2, issues)
            self.assertRegex(issues[0], r"^scripts/bad\.py: .*codec can't decode")
            self.assertEqual(issues[1], "scripts/output.py:1: fixed display name 'worker'; read config/names.toml")

    def test_sentence_output_is_information_without_label_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source_coverage_root(root)
            _ = (root / "scripts/output.py").write_text('print("Ask the worker for help.")\n', encoding="utf-8")
            self.assertEqual(audit.check_sources(root), ())
            output = StringIO()
            with redirect_stdout(output):
                status = audit.main(["check", "--root", str(root), "--prose"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "role words in sentences: 1", "scripts/output.py:1: 'worker'",
            ])

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

    def test_reader_does_not_read_ignored_binary_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_check_root(root, {"scripts/current.py": 'print("Dispatch a worker.")\n'})
            _ = subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            _ = (root / "scripts/.gitignore").write_text("target/\n", encoding="utf-8")
            target = root / "scripts/target"
            target.mkdir()
            _ = (target / "binary").write_bytes(b"\x80")
            _ = subprocess.run(["git", "-C", str(root), "add", "scripts/current.py"],
                               check=True, capture_output=True)
            _ = (root / "scripts/untracked.py").write_text('print("Ready.")\n', encoding="utf-8")
            sources = audit.read_live_sources(root)
            self.assertEqual([file.path for file in sources.files], ["scripts/current.py", "scripts/untracked.py"])
            self.assertEqual(sources.unread_sources, ())

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
            "scripts/agents/shutdown.py",
            "scripts/direct/assign.sh",
            "scripts/usage/collect.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in paths:
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_bytes((repository / name).read_bytes())
            self.assertEqual(audit.check_sources(root), audit.check_sources(root, full_extraction=True))

    def test_parallel_check_keeps_source_order_contexts_and_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {f"scripts/source{index:02}.py": "# Padding.\n" * index + 'print("Ready.")\n'
                       for index in range(64)}
            sources.update({
                "docs/protocol.md": "Keep a seat stable.\nA seat needs work.\n",
                "scripts/first.py": 'print("Dispatch a seat.")\n',
                "scripts/last.py": 'print("worker")\n',
                "scripts/broken.py": "print(\n",
            })
            self.write_check_root(root, sources)
            inventory = root / "scripts/production/role_terms.toml"
            with inventory.open("a", encoding="utf-8") as output:
                _ = output.write('''
[[terms.kept_contexts]]
path = "docs/protocol.md"
text = "Keep a seat stable."
reason = "The protocol keeps this term."
''')
            _ = (root / "scripts/unreadable.py").write_bytes(b"\xff")
            checked = audit.source_check(root)
            self.assertEqual(checked, audit.source_check(root, full_extraction=True))
            self.assertEqual(len(checked.unread_sources), 2)
            self.assertEqual(checked.issues[2:], (
                "docs/protocol.md:2: retired 'seat'; use 'worker'",
                "scripts/first.py:1: retired 'seat'; use 'worker'",
                "scripts/last.py:1: fixed display name 'worker'; read config/names.toml",
            ))

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
        self.assertEqual(issues, ())
        self.assertEqual([(use.path, use.line, use.name) for use in uses], [
            ("scripts/apostrophe.py", 1, "seat"),
            ("scripts/parenthesis.py", 1, "seat"),
            ("scripts/spanning.py", 2, "seat"),
        ])

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
                        ])


if __name__ == "__main__":
    _ = unittest.main()
