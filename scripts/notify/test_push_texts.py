"""Approval and delivery-path gates for fixed phone texts."""
from __future__ import annotations

import ast
import importlib
import json
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from code_kind import is_test_code  # noqa: E402

import gpu_xid
import push_texts
from push_texts import ActionTemplate, PushExample, PushText, SessionTemplate
from user_action import NO_SESSION_LINE, SessionLine

ROOT = Path(__file__).resolve().parents[2]
EXEMPT = {"scripts/notify/push_texts.py", "scripts/notify/pushover.py", "scripts/message/send.py", "scripts/message/escalate.py"}


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


def missing_approvals(entries: tuple[PushText, ...], path: Path) -> list[str]:
    approvals = cast(dict[str, dict[str, dict[str, str]]], json.loads(path.read_text()) if path.exists() else {})
    return [f"{entry.id}/{example.name}: run review.py texts --id {entry.id} --approve"
            for entry in entries for example in entry.examples
            if approvals.get(entry.id, {}).get(example.name, {}).get("sha256") != push_texts.fingerprint(entry)]


class PushTextTests(unittest.TestCase):
    def entry(self) -> PushText:
        return PushText("test", "test", "decision", "Choose {version}", ActionTemplate("Reply upgrade or wait."),
                        "The check passed on {version}.", (PushExample("typical", {"version": "1.2"}), PushExample("long", {"version": "1.2000"})),
                        purpose="Test.")

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
                    self.assertLess(len(text), 500)
                    self.assertLess(len(push.title), 45)

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
                result = subprocess.run([sys.executable, str(Path(gpu_xid.__file__)), code],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, meaning + "\n")
                self.assertEqual(result.stderr, "")

    def test_gpu_xid_command_refuses_unknown_codes_on_one_line(self) -> None:
        for code, shown in (("999", "'999'"), ("999\nsecond line", "'999\\nsecond line'")):
            with self.subTest(code=code):
                result = subprocess.run([sys.executable, str(Path(gpu_xid.__file__)), code],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"gpu_xid: unknown Xid code: {shown}\n")

    def test_failed_restart_catalog_examples_show_named_sessions(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "shutdown.restart_failed")
        self.assertEqual({example.name for example in entry.examples}, {"typical", "one_long_name"})
        for example in entry.examples:
            with self.subTest(example=example.name):
                push = push_texts.render_entry(entry, example.values)
                stopped = push.values["stopped"]
                self.assertRegex(stopped, r"\([^()]+\)")
                self.assertIn(stopped, push.message)

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

    def test_only_nate_adhoc_is_agent_written_and_its_approval_is_current(self) -> None:
        self.assertEqual([entry.id for entry in push_texts.catalog() if push_texts.agent_written(entry.id)], ["nate.adhoc"])
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

    def test_auto_mode_refusal_keeps_the_user_approved_lines(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "auto_mode.refused")
        self.assertEqual(entry.need, "decision")
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "cargo-liner-style: auto mode stopped modules")
        # The first line says what auto mode refused and where (user, 2026-10-23); each command follows on its own line.
        self.assertEqual(push.message.split("\n")[:5], [
            "Session: modules", "Action: On natedev, in the modules pane, approve the prompt or add an allow rule",
            "Auto mode refused 2 shell commands in the modules session. The modules build waits for them.",
            "implement.sh --to modules-impl", "review.sh --to modules-test"])
        self.assertEqual([line.split(":")[0] for line in push.body.split("\n\n")[1:]], ["Approve", "Allow rule"])
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
        self.assertEqual(push.message, "Action: To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex2.md on natedev\n"
                         + "codex2 has 25,345 paid credits left. Its plan refills Tue 10-27 02:33 AST.\n\n"
                         + "Leave on (recommended): work spends these credits, then moves to Claude.\n\n"
                         + "Turn off: work moves to Claude until Sat 10-24 16:40 AST.")

    def test_paid_credits_queued_keeps_the_agreed_lines(self) -> None:
        entry = next(entry for entry in push_texts.catalog() if entry.id == "quota.paid_credits_queued")
        plain = next(entry for entry in push_texts.catalog() if entry.id == "quota.paid_credits")
        self.assertEqual((entry.title, entry.action, entry.need, entry.sender), (plain.title, plain.action, plain.need, plain.sender))
        push = push_texts.render_entry(entry, entry.examples[0].values)
        self.assertEqual(push.title, "quota_alert: codex2 now uses paid credits")
        self.assertEqual(push.message, "Action: To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex2.md on natedev\n"
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

    def test_each_script_delivery_uses_the_registry(self) -> None:
        findings: list[str] = []
        for path in (ROOT / "scripts").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT or is_test_code(path):
                continue
            findings.extend(f"{relative}:{line}: literal phone send; use push_texts.user_send_argv" for line in literal_delivery_calls(path.read_text()))
        self.assertEqual(findings, [])

    def test_only_pushover_posts_to_pushover(self) -> None:
        """pushover.post_message adds the "Sent by" and "Template" lines; no other script may post round it."""
        found = [path.relative_to(ROOT).as_posix() for path in (ROOT / "scripts").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and not is_test_code(path)
                 and path.name != "pushover.py" and b"api.pushover.net" in path.read_bytes()]
        self.assertEqual(found, [])
