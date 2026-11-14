"""Approval and delivery-path gates for fixed phone texts."""
from __future__ import annotations

import ast
import importlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from scripts.notify import gpu_xid
from scripts.notify import push_texts
from scripts.message import session_send
from scripts.notify.push_texts import ActionTemplate, PushExample, PushText, SessionTemplate
from scripts.notify.user_action import ActionRequired, NO_SESSION_LINE, SessionLine


def literal_path(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = literal_path(node.left), literal_path(node.right)
        return left.rstrip("/") + "/" + right if right else ""
    if isinstance(node, ast.Call) and node.args and isinstance(node.func, (ast.Name, ast.Attribute)):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        if name in ("str", "Path"):
            return literal_path(node.args[0])
    return ""


def literal_delivery_calls(source: str) -> list[int]:
    """Report forbidden routing tokens at their source lines, without following values."""
    tree = ast.parse(source)
    broadcast_names = {"broadcast"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            broadcast_names.update(alias.asname or alias.name for alias in node.names
                                   if alias.name.rsplit(".", 1)[-1] == "broadcast")
        elif isinstance(node, ast.ImportFrom):
            broadcast_names.update(alias.asname or alias.name for alias in node.names
                                   if alias.name == "broadcast" or (node.module or "").rsplit(".", 1)[-1] == "broadcast")
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Call)):
            arguments = node.args if isinstance(node, ast.Call) else node.elts
            uses_builder = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "session_send"
                            and node.func.attr == "send_argv")
            command_index = 0 if uses_builder else 2
            if len(arguments) > command_index and literal_path(arguments[command_index]) == "retire":
                continue
            for left, right in zip(arguments, arguments[1:]):
                if literal_path(left) == "--to" and literal_path(right) == "user":
                    hits.add(left.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "--to=user" or node.value.endswith("notify/pushover.py"):
                hits.add(node.lineno)
        if isinstance(node, (ast.BinOp, ast.Call)) and literal_path(node).endswith("/pushover.py"):
            hits.add(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in broadcast_names
                and node.func.attr == "send" and node.args and literal_path(node.args[0]) == "user"):
            hits.add(node.lineno)
        # Keep separately appended routing tokens visible without tracking list contents.
        for _, field in cast(Iterator[tuple[str, object]], ast.iter_fields(node)):
            if not isinstance(field, list):
                continue
            statements = cast(list[ast.AST], field)
            for left, right in zip(statements, statements[1:]):
                if not isinstance(left, ast.Expr) or not isinstance(right, ast.Expr):
                    continue
                a, b = left.value, right.value
                if (isinstance(a, ast.Call) and isinstance(b, ast.Call)
                        and isinstance(a.func, ast.Attribute) and isinstance(b.func, ast.Attribute)
                        and a.func.attr == b.func.attr == "append"
                        and ast.dump(a.func.value) == ast.dump(b.func.value)
                        and len(a.args) == len(b.args) == 1
                        and literal_path(a.args[0]) == "--to" and literal_path(b.args[0]) == "user"):
                    hits.add(a.lineno)
    return sorted(hits)


def instruction_phone_sends(source: str) -> list[int]:
    """Find phone commands without a registered template in instruction text."""
    findings: set[int] = set()
    for match in re.finditer(r"```[^\n]*\n([\s\S]*?)```|`([^`\n]+)`", source):
        block = match.group(1) or match.group(2)
        # Join shell continuations before checking each command.
        block = block.replace("\\\n", " ")
        try:
            lexer = shlex.shlex(block, posix=False, punctuation_chars=";\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        commands: list[list[str]] = [[]]
        for token in tokens:
            if token and all(c in ";\n" for c in token):
                commands.append([])
            else:
                commands[-1].append(token)
        for command in commands:
            # Remove quotes for comparisons without parsing each token as a new command.
            values = [token.replace('"', "").replace("'", "") for token in command]
            send_positions = [i for i, token in enumerate(values) if token.endswith("send.py")]
            direct_positions = [i for i, token in enumerate(values) if token.endswith("pushover.py")]
            for index in send_positions + direct_positions:
                args = values[index + 1:]
                if args[:1] in (["retire"], ["cancel"]):
                    continue
                is_phone = index in direct_positions or "--to=user" in args or any(
                    left == "--to" and right == "user" for left, right in zip(args, args[1:]))
                # Quoted body text cannot supply a template flag.
                flag_tokens = command[index + 1:]
                registered = "--text-id" in flag_tokens or any(
                    arg.startswith("--text-id=") for arg in flag_tokens)
                if is_phone and not registered:
                    findings.add(source.count("\n", 0, match.start()) + 1)
    return sorted(findings)


def missing_approvals(entries: tuple[PushText, ...], path: Path) -> list[str]:
    approvals = cast(dict[str, dict[str, dict[str, str]]], json.loads(path.read_text()) if path.exists() else {})
    return [f"{entry.id}/{example.name}: run review.py texts --id {entry.id} --approve"
            for entry in entries for example in entry.examples
            if approvals.get(entry.id, {}).get(example.name, {}).get("sha256") != push_texts.fingerprint(entry)]


class PushTextTests(unittest.TestCase):
    def test_login_sitting_examples_name_end_dates_and_steps_as_normal_notes(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "accounts.login_sitting")
        self.assertEqual(entry.sender, "accounts")
        self.assertEqual(entry.need, "note")
        self.assertEqual([example.name for example in entry.examples], ["weekly", "safety"])
        for example in entry.examples:
            with self.subTest(example=example.name):
                push = push_texts.render_entry(entry, example.values)
                self.assertTrue(push.body.startswith("You asked "))
                self.assertIn("login end dates", push.body)
                self.assertIn("Sign out of claude.ai in the browser.", push.body)
                self.assertIn("Sign in as that account.", push.body)
                self.assertIn("run /login in a Claude session on that account.", push.body)
                self.assertNotIn("No action needed.", push.message)

    def entry(self) -> PushText:
        return PushText("test", "test", "decision", "Choose {version}", ActionTemplate("Reply upgrade or wait."),
                        "The check passed on {version}.", (PushExample("typical", {"version": "1.2"}), PushExample("long", {"version": "1.2000"})),
                        purpose="Test.")

    def test_blocked_mac_catalog_keeps_its_required_action(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "mac_idle.blocked")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertIsInstance(push.action, ActionRequired)
        self.assertEqual(push.action, ActionRequired(cast(ActionTemplate, entry.action).text.format_map(push.values)))

    def test_every_catalog_example_has_current_approval(self) -> None:
        entries = push_texts.catalog()
        self.assertTrue(entries, "push_catalog.ENTRIES must include script and nixos pushes")
        self.assertEqual(missing_approvals(entries, push_texts.REVIEWED_FILE), [])

    def test_every_catalog_example_fits_the_phone_title_and_body_limits(self) -> None:
        for entry in push_texts.catalog():
            for example in entry.examples:
                with self.subTest(id=entry.id, example=example.name):
                    push = push_texts.render_entry(entry, example.values)
                    text = push.message
                    if entry.id in ("decision.choices", "review_watch.acknowledge"):
                        self.assertLess(len(text), 1024)
                    else:
                        self.assertLess(len(text), 500)
                    self.assertLess(len(push.title), 45)

    def test_decision_argv_carries_the_question_record_path(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "decision.choices")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        argv = push_texts.user_send_argv(push, question="/tmp/question-layout.md")
        self.assertEqual(argv[argv.index("--question") + 1], "/tmp/question-layout.md")

    def test_user_commands_use_the_current_sender_and_preserve_an_explicit_override(self) -> None:
        entry = self.entry()
        push = push_texts.render_entry(entry, entry.examples[0].values)
        with tempfile.TemporaryDirectory() as folder:
            current, override = Path(folder) / "current.py", Path(folder) / "override.py"
            with mock.patch.object(session_send, "SEND_SCRIPT", current):
                default = push_texts.user_send_argv(push)
                explicit = push_texts.user_send_argv(push, send_script=override)
                retired = push_texts.user_retire_argv("sender", "held")
        self.assertEqual(default[:2], [sys.executable, str(current)])
        self.assertEqual(explicit, [sys.executable, str(override), *default[2:]])
        self.assertEqual(retired, [sys.executable, str(current), "retire", "--to", "user", "--from", "sender", "--key", "held"])

    def test_decision_choices_builds_the_approved_rows_and_keeps_structured_values(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "decision.choices")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "Assembly opening stalls on cable routes")
        self.assertEqual(push.action, ActionRequired("On natedev, reply in the hana terminal: fix now or wait."))
        self.assertIn("Choices:\nFix now: cables-unit makes this small fix, then returns to its hold. Assemblies open without this stall.\nWait: cables-unit remains on hold. The stall remains until its cable work resumes.", push.body)
        self.assertEqual(push.values, entry.examples[0].values)
        self.assertTrue(push_texts.agent_written(push.id))
        self.assertTrue(push_texts.approval_date(push.id))

    def test_decision_choices_refuses_invalid_structured_values(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "decision.choices")
        values = entry.examples[0].values
        cases = ("[]", '[{"label":"Fix now","effect":"Repair."}]',
                 '[{"label":"Fix now","effect":"Repair."},{"label":"fix now","effect":"Wait."}]',
                 '[{"label":"Fix now","effect":""},{"label":"Wait","effect":"Wait."}]', "free body")
        for choices in cases:
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                _ = push_texts.render_entry(entry, values | {"choices": choices})
        with self.assertRaises(ValueError):
            _ = push_texts.render_entry(entry, values | {"recommended_choice": "Other"})
        with self.assertRaises(ValueError):
            _ = push_texts.render_entry(entry, values | {"choice_labels": "injected"})

    def test_decision_values_keep_unit_labels_consistent(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "decision.choices")
        values = entry.examples[0].values | {
            "choices": json.dumps([{"label": "build-unit", "effect": "Repair."}, {"label": "Wait", "effect": "Wait."}]),
            "recommended_choice": "build-unit"}
        push = push_texts.render_entry(entry, values)
        self.assertIn("build-unit: Repair.", push.body)
        self.assertIn("Recommendation: build-unit.", push.body)
        self.assertIn("build-unit or wait", cast(ActionRequired, push.action).text)

    def test_decision_values_refuse_every_line_separator(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "decision.choices")
        for separator in ("\n", "\r", "\u2028", "\u2029", "\u0085", "\v", "\f", "\x1c", "\x1d", "\x1e"):
            for field in entry.examples[0].values:
                if field == "choices":
                    for choice_field in ("label", "effect"):
                        choices = [{"label": "Fix now", "effect": "Repair."}, {"label": "Wait", "effect": "Wait."}]
                        choices[0][choice_field] += separator
                        with self.subTest(field=choice_field, separator=separator), self.assertRaises(ValueError):
                            _ = push_texts.render_entry(entry, entry.examples[0].values | {"choices": json.dumps(choices)})
                else:
                    with self.subTest(field=field, separator=separator), self.assertRaises(ValueError):
                        _ = push_texts.render_entry(entry, entry.examples[0].values | {field: entry.examples[0].values[field] + separator})

    def test_review_watch_uses_the_approved_choices_and_live_review(self) -> None:
        push = push_texts.render("review_watch.acknowledge", session="builds", machine="Mac", report="Read the report.")
        self.assertEqual(push.title, "Review report needs your acknowledgment")
        self.assertEqual(push.action, ActionRequired("In the builds terminal on Mac, acknowledge the review report."))
        self.assertEqual(push.body, "Situation: Read the report.\n\nChoices:\nAcknowledge: You confirm that you read the report. Later build reports stop showing this reminder.\nWait: The reminder remains in later build reports.\n\nRecommendation: Acknowledge after you read the report.")
        self.assertTrue(push_texts.agent_written(push.id))
        self.assertTrue(push_texts.approval_date(push.id))

    def test_ups_maintenance_alerts_keep_the_approved_text_and_nonurgent_action(self) -> None:
        cases = (
            ("nixos.ups_replace_battery", "UPS battery needs replacing",
             "Replace the battery in the natedev UPS.",
             "The natedev UPS reports that its battery is worn out. During a power outage it may not keep natedev on long enough to shut down safely."),
            ("nixos.ups_no_contact", "natedev lost contact with its UPS",
             "Check the USB cable between the UPS and natedev.",
             "natedev cannot read its UPS. During a power outage it gets no warning and cannot shut down safely."),
        )
        for text_id, title, action, body in cases:
            with self.subTest(id=text_id):
                push = push_texts.render(text_id)
                self.assertEqual((push.sender, push.need, push.title, push.action, push.body),
                                 ("ups", "note", title, ActionRequired(action), body))

    def test_every_gpu_xid_renders_its_meaning_and_restart_action(self) -> None:
        for code, meaning in gpu_xid.XID_MEANINGS.items():
            with self.subTest(xid=code):
                push = push_texts.render("nixos.gpu_stopped", xid=code, meaning=meaning, time="02:17 EDT")
                self.assertEqual(push.sender, "gpu")
                self.assertEqual(push.need, "decision")
                self.assertEqual(push.title, "natedev GPU stopped")
                self.assertIn(f"Xid {code}", push.message)
                self.assertIn(meaning, push.message)
                self.assertIn("02:17 EDT", push.message)
                self.assertEqual(push.message.splitlines()[0], "Action: Restart natedev to bring the GPU back.")

    def test_gpu_xid_command_reports_known_codes_without_extra_output(self) -> None:
        for code, meaning in gpu_xid.XID_MEANINGS.items():
            with self.subTest(xid=code):
                result = subprocess.run([str(Path(gpu_xid.__file__).resolve().parents[1] / "lib" / "py"), str(Path(gpu_xid.__file__)), code],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, meaning + "\n")
                self.assertEqual(result.stderr, "")

    def test_gpu_xid_command_refuses_unknown_codes_on_one_line(self) -> None:
        for code, shown in (("999", "'999'"), ("999\nsecond line", "'999\\nsecond line'")):
            with self.subTest(code=code):
                result = subprocess.run([str(Path(gpu_xid.__file__).resolve().parents[1] / "lib" / "py"), str(Path(gpu_xid.__file__)), code],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"gpu_xid: unknown Xid code: {shown}\n")

    def test_failed_restart_catalog_examples_show_the_count_and_one_command(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "shutdown.restart_failed")
        self.assertEqual({example.name for example in entry.examples}, {"typical", "one_long_name"})
        for example in entry.examples:
            with self.subTest(example=example.name):
                push = push_texts.render_entry(entry, example.values)
                stopped = push.values["stopped"]
                self.assertRegex(stopped, r"^(?:1 session|[1-9][0-9]* sessions)")
                self.assertIn(stopped, push.message)
                self.assertIn("Coding work stays stopped until the restart succeeds.", push.body)
                self.assertEqual(cast(ActionRequired, push.action).text.count("/agents restart"), 1)
                self.assertIn("natedev", cast(ActionRequired, push.action).text)
                self.assertLess(len(push.message), 500)
        self.assertEqual(entry.examples[0].values["stopped"], "5 sessions")

    def test_trust_catalog_selects_commands_for_configured_folders_and_a_request_for_unlisted_folders(self) -> None:
        entries = {entry.id: entry for entry in push_texts.catalog()}
        covered = entries["shutdown.restart_untrusted"]
        self.assertEqual({example.name for example in covered.examples}, {"typical"})
        for example in covered.examples:
            push = push_texts.render_entry(covered, example.values)
            self.assertIn(example.values["stopped"], push.body)
            action = cast(ActionRequired, push.action).text
            self.assertEqual(action.count("accounts.py trust"), 1)
            self.assertEqual(action.count("/agents restart"), 1)
            self.assertNotIn("cd /", push.message)
            self.assertLess(len(push.message), 500)
        unlisted = entries["shutdown.restart_untrusted_unlisted"]
        self.assertEqual({example.values["stopped"] for example in unlisted.examples}, {"1 session", "4 sessions"})
        for example in unlisted.examples:
            push = push_texts.render_entry(unlisted, example.values)
            self.assertIn(example.values["stopped"], push.body)
            self.assertIn(". Claude does not trust the folders. ", push.body)
            self.assertIn(f'Trust the folders of the stopped Claude sessions on {example.values["machine"]} from their shutdown records.',
                          cast(ActionRequired, push.action).text)
            self.assertNotIn("accounts.py trust", push.message)
            self.assertNotIn("/agents restart", push.message)
            self.assertNotIn("cd /", push.message)
            self.assertLess(len(push.message), 500)
        self.assertNotIn("shutdown.move_untrusted", entries)

    def test_manual_restart_catalog_groups_sessions_by_machine(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "shutdown.manual_restart")
        self.assertEqual({example.values["stopped"] for example in entry.examples}, {"1 session", "3 sessions"})
        for example in entry.examples:
            with self.subTest(example=example.name):
                push = push_texts.render_entry(entry, example.values)
                self.assertIn(example.values["stopped"], push.body)
                self.assertIn(f'Resume the stopped Claude sessions on {example.values["machine"]} from their shutdown records.',
                              cast(ActionRequired, push.action).text)
                self.assertNotIn("/agents restart", push.message)
                self.assertLess(len(push.message), 500)

    def test_every_catalog_example_keeps_email_addresses_out_of_pushes(self) -> None:
        for entry in push_texts.catalog():
            for example in entry.examples:
                with self.subTest(id=entry.id, example=example.name):
                    push = push_texts.render_entry(entry, example.values)
                    self.assertNotIn("@", push.sender + "\n" + push.title + "\n" + push.message)

    def test_every_catalog_entry_has_a_one_line_purpose(self) -> None:
        for entry in push_texts.catalog():
            with self.subTest(id=entry.id):
                self.assertTrue(entry.purpose.strip())
                self.assertNotIn("\n", entry.purpose)
                self.assertLessEqual(len(entry.purpose), 72)

    def test_purpose_is_not_part_of_the_fingerprint(self) -> None:
        entry = self.entry()
        self.assertEqual(push_texts.fingerprint(entry), push_texts.fingerprint(replace(entry, purpose="Other.")))

    def test_agent_written_is_not_part_of_the_fingerprint(self) -> None:
        entry = self.entry()
        self.assertFalse(entry.agent_written)
        self.assertEqual(push_texts.fingerprint(entry), push_texts.fingerprint(replace(entry, agent_written=True)))

    def test_agent_written_entries_and_nate_approval_are_current(self) -> None:
        self.assertEqual([entry.id for entry in push_texts.catalog() if push_texts.agent_written(entry.id)], ["decision.choices", "review_watch.acknowledge", "alert.blocked", "alert.decision", "nate.adhoc"])
        self.assertFalse(push_texts.agent_written("unknown"))
        self.assertRegex(push_texts.approval_date("nate.adhoc"), r"^\d{4}-\d{2}-\d{2}$")

    def test_listing_prints_one_padded_line_per_entry_sorted_by_id(self) -> None:
        entries = (replace(self.entry(), id="zeta", purpose="Last."), replace(self.entry(), id="a.long_id", purpose="First."))
        self.assertEqual(push_texts.listing(entries), "a.long_id  First.\nzeta       Last.")
        ordered = sorted(push_texts.catalog(), key=lambda entry: entry.id)
        lines = push_texts.listing(push_texts.catalog()).splitlines()
        self.assertEqual([line.split()[0] for line in lines], [entry.id for entry in ordered])
        self.assertEqual(len({line.rindex(entry.purpose) for line, entry in zip(lines, ordered)}), 1)

    def test_shown_prints_the_template_then_its_first_example_as_sent(self) -> None:
        entry = replace(self.entry(), session=SessionTemplate("{version}"))
        push = push_texts.render_entry(entry, {"version": "1.2"})
        self.assertEqual(push_texts.shown(entry),
                         "test  Test.\n\nTemplate:\nTitle: Choose {version}\nSession: {version}\nAction: Reply upgrade or wait.\n"
                         + "The check passed on {version}.\n\nExample (typical):\nTitle: Choose 1.2\n" + push.message)

    def test_session_template_renders_before_action_and_body(self) -> None:
        entry = replace(self.entry(), session=SessionTemplate("{version}"))
        push = push_texts.render_entry(entry, {"version": "1.2"})
        self.assertEqual(push.session, SessionLine("1.2"))
        self.assertEqual(push.message, "Session: 1.2\nAction: Reply upgrade or wait.\nThe check passed on 1.2.")

    def test_entry_without_session_keeps_its_existing_message(self) -> None:
        push = push_texts.render_entry(self.entry(), {"version": "1.2"})
        self.assertEqual(push.session, NO_SESSION_LINE)
        self.assertEqual(push.message, "Action: Reply upgrade or wait.\nThe check passed on 1.2.")

    def test_session_only_placeholder_is_required_and_fingerprinted(self) -> None:
        entry = replace(self.entry(), session=SessionTemplate("{session}"))
        with self.assertRaises(ValueError):
            _ = push_texts.render_entry(entry, {"version": "1.2"})
        values = {"version": "1.2", "session": "Owner"}
        self.assertEqual(push_texts.render_entry(entry, values).session, SessionLine("Owner"))
        entry = replace(entry, examples=(PushExample("typical", values),))
        self.assertNotEqual(push_texts.fingerprint(entry), push_texts.fingerprint(replace(entry, session=SessionTemplate("{session:s}"))))

    def test_nate_adhoc_renders_without_a_header_line(self) -> None:
        push = push_texts.render("nate.adhoc", sender="natedev", title="the /nate skill is ready", description="It is ready now.")
        self.assertEqual(push.title, "natedev the /nate skill is ready")
        self.assertEqual(push.message, "It is ready now.")

    def test_vault_stops_have_no_phone_template(self) -> None:
        self.assertNotIn("vault.stopped", {entry.id for entry in push_texts.catalog()})

    def test_auto_mode_refusal_says_what_happened_and_what_to_type(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "auto_mode.refused")
        self.assertEqual(entry.need, "decision")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "cargo-liner-style: modules commands refused")
        # The first line says what auto mode refused and where (user, 2026-10-23); each command follows on its own line.
        self.assertEqual(push.message.split("\n")[:5], [
            "Session: modules", "Action: On natedev, in the modules pane, type: Go: run the refused commands",
            "Auto mode refused 2 shell commands in the modules session. The modules build waits for them.",
            "implement.sh --to modules-impl", "review.sh --to modules-test"])
        self.assertEqual([line.split(":")[0] for line in push.body.split("\n\n")[1:]], ["Go", "Allow rule"])
        self.assertTrue(push.body.endswith("auto mode lets these commands run from then on."))
        one = push_texts.render_entry(entry, entry.examples[1].values)
        self.assertTrue(one.body.startswith("Auto mode refused a shell command in the messaging session."
                                            + " The merge of the messaging work waits for it.\n"
                                            + "git push origin build-followups-messaging\n"))
        self.assertTrue(one.body.endswith(" The rule: Bash(git push:*)"))

    def test_paid_credits_keeps_the_user_approved_lines(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "quota.paid_credits")
        self.assertEqual(entry.need, "decision")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "quota_alert: codex2 now uses paid credits")
        self.assertEqual(push.message, "Action: To stop it, remove codex2 from codex_credit_accounts in ~/.claude/config/accounts.conf on natedev\n"
                         + "codex2 has 25,345 paid credits left. Its plan refills Tue 10-27 02:33 AST.\n\n"
                         + "Leave on (recommended): work spends these credits, then moves to Claude.\n\n"
                         + "Turn off: work moves to Claude until Sat 10-24 16:40 AST.")

    def test_paid_credits_queued_keeps_the_agreed_lines(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "quota.paid_credits_queued")
        plain = next(entry for entry in push_texts.catalog() if entry.id == "quota.paid_credits")
        self.assertEqual((entry.title, entry.action, entry.need, entry.sender), (plain.title, plain.action, plain.need, plain.sender))
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "quota_alert: codex2 now uses paid credits")
        self.assertEqual(push.message, "Action: To stop it, remove codex2 from codex_credit_accounts in ~/.claude/config/accounts.conf on natedev\n"
                         + "codex2 has 25,345 paid credits left. Its plan refills Tue 10-27 02:33 AST.\n\n"
                         + "Leave on (recommended): workers spend these credits until your queued move runs. "
                         + "If the credits run out first, workers wait.\n\n"
                         + "Turn off: workers stop and wait for your queued move.")
        self.assertNotIn("next_reset", entry.body)

    def test_needs_go_keeps_the_user_approved_lines(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "session.needs_go")
        self.assertEqual(entry.need, "decision")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "natedev: trunk waits for your go")
        self.assertEqual(push.message, "Session: trunk\n"
                         + "Action: On natedev, in the trunk pane, type: Go: remove arrange_jacks/ from phases/130/files; 140 owns it\n"
                         + "Steps 130 and 140 both claim the arrange_jacks folder, and your permission check refused the fix.\n"
                         + "Your go removes it from step 130, and trunk goes on.")

    def test_header_is_fingerprinted_and_cannot_hide_an_action_or_session(self) -> None:
        entry = replace(self.entry(), action=push_texts.NoActionNeeded())
        self.assertNotEqual(push_texts.fingerprint(entry), push_texts.fingerprint(replace(entry, header=False)))
        for hidden in (self.entry(), replace(entry, session=SessionTemplate("{version}"))):
            module = SimpleNamespace(ENTRIES=(replace(hidden, header=False),))
            with (self.subTest(hidden=hidden), mock.patch.object(importlib, "import_module", return_value=module),
                  self.assertRaises(ValueError)):
                _ = push_texts.catalog()

    def test_remote_control_push_is_absent_from_catalog_and_approvals(self) -> None:
        removed_id = "shutdown." + "remote_control"
        self.assertNotIn(removed_id, {entry.id for entry in push_texts.catalog()})
        approvals = cast(dict[str, object], json.loads(push_texts.REVIEWED_FILE.read_text()))
        self.assertNotIn(removed_id, approvals)

    def test_template_change_with_same_example_output_invalidates_every_approval(self) -> None:
        entry = self.entry()
        changed = replace(entry, body="The check passed on {version:s}.")
        self.assertEqual(push_texts.render_entry(entry, entry.examples[0].values).body,
                         push_texts.render_entry(changed, changed.examples[0].values).body)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reviewed.json"
            _ = path.write_text(json.dumps({entry.id: {example.name: {"sha256": push_texts.fingerprint(entry)} for example in entry.examples}}))
            self.assertEqual(missing_approvals((entry,), path), [])
            self.assertEqual(len(missing_approvals((changed,), path)), 2)

    def test_render_rejects_unknown_missing_and_extra_values(self) -> None:
        with mock.patch.object(push_texts, "catalog", return_value=(self.entry(),)):
            for id, values in (("absent", {"version": "1"}), ("test", {}), ("test", {"version": "1", "other": "2"})):
                with self.subTest(id=id, values=values), self.assertRaises(ValueError):
                    _ = push_texts.render(id, **values)

    def test_missing_catalog_fails_instead_of_appearing_empty_or_unknown(self) -> None:
        with mock.patch.object(importlib, "import_module", side_effect=ModuleNotFoundError("catalog absent", name="push_catalog")):
            with self.assertRaises(ModuleNotFoundError):
                _ = push_texts.catalog()
            with self.assertRaises(ModuleNotFoundError):
                _ = push_texts.render("quota.claude")

    def test_annotated_literal_argv_reports_the_routing_tokens(self) -> None:
        self.assertEqual(literal_delivery_calls('command: list[str] = ["send.py", "--to", "user", "--text", "literal"]\nrun(command)'), [1])

    def test_literal_user_argv_remains_checked_after_list_mutations(self) -> None:
        for mutation in ('command += ["--summary", "title"]',
                         'command.extend(["--summary", "title"])',
                         'command.append("--summary")'):
            with self.subTest(mutation=mutation):
                source = 'command = ["send.py", "--to", "user", "--text", "fixed"]\n' + mutation + '\nsubprocess.run(command)'
                self.assertEqual(literal_delivery_calls(source), [1])
        self.assertEqual(literal_delivery_calls('command = ["send.py"]\ncommand.append("--to")\ncommand.append("user")\nsubprocess.run(command)'), [2])
        self.assertEqual(literal_delivery_calls('command = ["send.py", "--to", "user"]\nalias = command\nalias += ["--summary", "title"]\nsubprocess.run(command)'), [1])
        self.assertEqual(literal_delivery_calls('push = render("good")\npush += ["literal"]\nbroadcast.send("user", push)'), [3])

    def test_literal_routing_tokens_are_found_after_an_alias_crosses_a_branch(self) -> None:
        source = ('command = ["send.py"]\nalias = command\nif ready:\n    pass\n'
                  'alias += ["--to", "user"]\nsubprocess.run(command)')
        self.assertEqual(literal_delivery_calls(source), [5])

    def test_token_gate_checks_call_arguments_equals_flags_paths_and_broadcast_imports(self) -> None:
        for source in ('run("send.py", "--to", "user")',
                       'command = ("send.py", "--to", "user")',
                       'command = ["send.py", "--to=user"]',
                       'path = ROOT / "notify" / "pushover.py"',
                       'path = Path("scripts/notify/pushover.py")'):
            with self.subTest(source=source):
                self.assertEqual(literal_delivery_calls(source), [1])
        for imported, call in (('import broadcast as notices', 'notices.send("user", push)'),
                               ('from production import broadcast as notices', 'notices.send("user", push)'),
                               ('from broadcast import transport as notices', 'notices.send("user", push)')):
            with self.subTest(imported=imported):
                self.assertEqual(literal_delivery_calls(imported + "\n" + call), [2])
        self.assertEqual(literal_delivery_calls('run(user_send_argv(render("good")))'), [])
        self.assertEqual(literal_delivery_calls('other.send("user", "text")'), [])

    def test_shared_sender_builder_exempts_retirement_but_checks_delivery(self) -> None:
        self.assertEqual(literal_delivery_calls(
            'session_send.send_argv("retire", "--to", "user", "--key", "held")'), [])
        self.assertEqual(literal_delivery_calls(
            'session_send.send_argv("--to", "user", "--text", "literal")'), [1])
        self.assertEqual(literal_delivery_calls(
            'other.send_argv("retire", "--to", "user")'), [1])

    def test_rendered_binding_cannot_excuse_a_later_string_or_conditional_string(self) -> None:
        self.assertEqual(literal_delivery_calls('push = render("good")\npush = "literal"\nbroadcast.send("user", "script", push)'), [3])
        self.assertEqual(literal_delivery_calls('push = render_entry(entry, values)\nif condition:\n    push = "literal"\nbroadcast.send("user", "script", push)'), [4])
        self.assertEqual(literal_delivery_calls('push = render("good")\ndef other():\n    push = "literal"\nbroadcast.send("user", push)'), [4])
        self.assertEqual(literal_delivery_calls('push = "literal"\npush = render_entry(entry, values)\nbroadcast.send("user", push)'), [3])
        self.assertEqual(literal_delivery_calls('push = "literal"\nfor item in items:\n    push = render("good")\nbroadcast.send("user", push)'), [4])
        self.assertEqual(literal_delivery_calls('push = render("good")\nfor item in items:\n    broadcast.send("user", push)\n    push = "literal"'), [3])
        self.assertEqual(literal_delivery_calls('push = "literal"\ntry:\n    push = render("good")\nexcept ValueError:\n    pass\nbroadcast.send("user", push)'), [6])

    def test_render_call_does_not_excuse_another_literal_send_in_the_module(self) -> None:
        source = 'push = render("good")\nrun(user_send_argv(push))\nrun(["send.py", "--to", "user", "--text", "literal"])'
        self.assertEqual(literal_delivery_calls(source), [3])
        self.assertEqual(literal_delivery_calls('broadcast.send("user", "literal")'), [1])
        self.assertEqual(literal_delivery_calls('command = ["send.py", "--to", "user"]\nrun(command)\ncommand = user_send_argv(render("good"))\nrun(command)'), [1])
        self.assertEqual(literal_delivery_calls('def deliver(push: RenderedPush):\n    broadcast.send("user", push)'), [2])
        self.assertEqual(literal_delivery_calls('command = ["scripts/notify/pushover.py", "title", "body"]\nrun(command)'), [1])
        self.assertEqual(literal_delivery_calls('run(["python3", str(ROOT / "notify" / "pushover.py"), "title", "body"])'), [1])

    def test_instruction_gate_checks_inline_and_multiline_phone_commands(self) -> None:
        self.assertEqual(instruction_phone_sends('`pushover.py --no-action Title Body`'), [1])
        self.assertEqual(instruction_phone_sends('`send.py --to user --text "use --text-id"`'), [1])
        self.assertEqual(instruction_phone_sends('`send.py --to user --text "--text-id"`'), [1])
        self.assertEqual(instruction_phone_sends('`pushover.py --no-action Title "--text-id"`'), [1])
        self.assertEqual(instruction_phone_sends('`pushover.py --no-action Title "use --text-id"`'), [1])
        self.assertEqual(instruction_phone_sends('`pushover.py --text-id nate.adhoc --value title=x`'), [])
        self.assertEqual(instruction_phone_sends('`send.py --to user --summary "Title" --text "Body"`'), [1])
        self.assertEqual(instruction_phone_sends('```sh\nsend.py --to=user \\\n --text "body"\n```'), [1])
        self.assertEqual(instruction_phone_sends('`send.py --to user --text-id nate.adhoc --value title=x`'), [])
        self.assertEqual(instruction_phone_sends('`send.py retire --to user --from owner --key key`'), [])

    def test_instruction_gate_checks_quoted_user_recipients(self) -> None:
        for recipient in ('"user"', "'user'", '"--to=user"', '--to="user"'):
            target = recipient if "--to" in recipient else f"--to {recipient}"
            with self.subTest(recipient=recipient):
                self.assertEqual(instruction_phone_sends(f'`send.py {target} --text "--text-id"`'), [1])
                self.assertEqual(instruction_phone_sends(f'`send.py {target} --text-id nate.adhoc`'), [])

    def test_instruction_gate_handles_quotes_inside_value_tokens(self) -> None:
        for template, expected in (("", [1]), (" --text-id decision.choices", [])):
            with self.subTest(template=template):
                source = '`send.py --to "user" --value title="User\'s choice"' + template + '`'
                self.assertEqual(instruction_phone_sends(source), expected)

    def test_instruction_gate_checks_quoted_executable_paths(self) -> None:
        for executable, target in (("send.py", "--to user"), ("pushover.py", "--no-action Title")):
            for quote in ('"', "'"):
                path = f"{quote}/a path/{executable}{quote}"
                with self.subTest(executable=executable, quote=quote):
                    self.assertEqual(instruction_phone_sends(f'`{path} {target} "--text-id"`'), [1])
                    self.assertEqual(instruction_phone_sends(f'`{path} {target} --text-id nate.adhoc`'), [])
