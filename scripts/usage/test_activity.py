"""Request activity rules for shell commands, Codex rollout items and Claude transcript lines."""

from __future__ import annotations

import unittest
from collections.abc import Iterable

from scripts.usage.activity import (
    Atom, NeutralAtom, PostingStatus, RequestActivity, RequestEvidence, StatusAtom, WaitAtom, Waiting, WorkAtom,
    Working, activity_name, claude_atoms, codex_atoms, decode_activity, exec_cell_atoms, shell_atoms, wait_action,
)

BOARD = "bash ~/.claude/scripts/delegate/board.sh"
TASKS = "/tmp/claude-1000/-home-natepiano-project/session/tasks"


def request(*atom_groups: Iterable[Atom]) -> RequestActivity:
    evidence = RequestEvidence()
    for atoms in atom_groups:
        evidence.add(atoms)
    return evidence.result()


def response_item(payload: dict[str, object]) -> dict[str, object]:
    return {"timestamp": "2026-10-16T12:00:00Z", "type": "response_item", "payload": payload}


def exec_cell(source: str) -> dict[str, object]:
    return response_item({"type": "custom_tool_call", "name": "exec", "input": source})


def assistant(*blocks: dict[str, object]) -> dict[str, object]:
    return {"type": "assistant", "message": {"id": "message", "content": list(blocks)}}


def tool(name: str, **arguments: object) -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": arguments}


class ShellTests(unittest.TestCase):
    def assert_atoms(self, command: str, *expected: Atom) -> None:
        self.assertEqual(shell_atoms(command), expected, command)

    def test_waiting_statements(self) -> None:
        cases: tuple[tuple[str, Atom], ...] = (
            ("sleep 45", WaitAtom("sleep")),
            ("wait 1234", WaitAtom("wait")),
            (f"{BOARD} read /tmp/board --since 12", WaitAtom("board.sh read")),
            ("~/.claude/scripts/delegate/board.sh read /tmp/board", WaitAtom("board.sh read")),
            ("tail -f /tmp/build.log", WaitAtom("tail *.log")),
            ("tail -n 5 progress", WaitAtom("tail")),
            (f"cat {TASKS}/b37sx2o7k.output", WaitAtom("cat *.output")),
            ("cat /proc/loadavg", WaitAtom("cat /proc/loadavg")),
            ("grep -c error /tmp/run.exit", WaitAtom("grep *.exit")),
            ("rg FAILED /tmp/test.out", WaitAtom("rg *.out")),
            ("wc -l /tmp/build.log", WaitAtom("wc *.log")),
            ("stat /tmp/job/exit_code", WaitAtom("stat exit_code")),
            ("head -3 /tmp/ready.marker", WaitAtom("head marker")),
            ("test -f /tmp/job.done", WaitAtom("test")),
            ("[ -s /tmp/job.rc ]", WaitAtom("test")),
            ("[[ -e /tmp/x ]]", WaitAtom("test")),
            ("ps -p 4242", WaitAtom("ps")),
            ("pgrep -f cargo", WaitAtom("pgrep")),
            ("kill -0 4242", WaitAtom("kill -0")),
        )
        for command, atom in cases:
            with self.subTest(command=command):
                self.assert_atoms(command, atom)

    def test_working_statements(self) -> None:
        for command in ("cargo build --release", "cat src/main.rs", "grep -rn todo src", "kill 4242",
                        "python3 -m unittest", "tail -5 /tmp/build.log > /tmp/summary.txt",
                        "grep error /tmp/build.log > /tmp/errors.txt", f"{BOARD} post /tmp/board impl note x",
                        f"{BOARD} claim /tmp/board"):
            with self.subTest(command=command):
                self.assert_atoms(command, WorkAtom())

    def test_status_post_needs_the_status_kind(self) -> None:
        self.assert_atoms(f'{BOARD} post /tmp/board impl status "build started"', StatusAtom())
        self.assert_atoms(f'board.sh post /tmp/board impl status "tests; then review"', StatusAtom())

    def test_neutral_words_and_control_words(self) -> None:
        self.assert_atoms("cd /tmp && echo ready", NeutralAtom(), NeutralAtom())
        self.assert_atoms("while ! test -f /tmp/job.done; do sleep 5; done",
                          WaitAtom("test"), WaitAtom("sleep"), NeutralAtom())
        self.assert_atoms("until kill -0 99 2>/dev/null; do sleep 2; done",
                          WaitAtom("kill -0"), WaitAtom("sleep"), NeutralAtom())
        self.assert_atoms("if [ -f /tmp/x.status ]; then cat /tmp/x.status; fi",
                          WaitAtom("test"), WaitAtom("cat *.status"), NeutralAtom())
        self.assert_atoms("for i in 1 2 3", NeutralAtom())

    def test_assignments(self) -> None:
        self.assert_atoms("TZ=America/New_York date", NeutralAtom())
        self.assert_atoms("RUST_LOG=debug cargo test", WorkAtom())
        self.assert_atoms("status=$(cat /tmp/run.exit); echo $status", WaitAtom("cat *.exit"), NeutralAtom())
        self.assert_atoms("out=$(cargo metadata)", WorkAtom())

    def test_separators_and_background(self) -> None:
        self.assert_atoms("sleep 45; tail -2 /tmp/build3.log", WaitAtom("sleep"), WaitAtom("tail *.log"))
        self.assert_atoms("sleep 1 || true", WaitAtom("sleep"), NeutralAtom())
        self.assert_atoms("cargo build &\nsleep 60", WorkAtom(), WaitAtom("sleep"))

    def test_pipes_into_filters_keep_the_first_statement(self) -> None:
        self.assert_atoms("ps aux | grep cargo | head -3", WaitAtom("ps"))
        self.assert_atoms("cat /tmp/build.log | tail -20 | wc -l", WaitAtom("cat *.log"))
        self.assert_atoms("tail /tmp/build.log | sh", WorkAtom())
        self.assert_atoms("cargo test 2>&1 | tail -5", WorkAtom())
        self.assert_atoms("tail /tmp/build.log | grep error > /tmp/errors", WorkAtom())

    def test_discarded_redirections_do_not_count_as_writes(self) -> None:
        self.assert_atoms("tail -1 /tmp/build.log 2>/dev/null", WaitAtom("tail *.log"))
        self.assert_atoms("cat /tmp/run.exit >/dev/null 2>&1", WaitAtom("cat *.exit"))
        self.assert_atoms("grep -q done /tmp/run.status &>/dev/null", WaitAtom("grep *.status"))
        self.assert_atoms("cat /tmp/run.exit > /tmp/copy", WorkAtom())

    def test_quoted_text_is_one_word_and_keeps_paths_visible(self) -> None:
        self.assert_atoms("git commit -m 'sleep 5; tail x.log'", WorkAtom())
        self.assert_atoms('grep "a > b; c" /tmp/build.log', WaitAtom("grep *.log"))
        self.assert_atoms('tail -3 "/tmp/my build/run.log"', WaitAtom("tail *.log"))
        self.assert_atoms('echo "waiting | still"', NeutralAtom())
        self.assert_atoms('grep "x\\" && sleep 9" /tmp/a.log', WaitAtom("grep *.log"))

    def test_heredoc_bodies_are_dropped(self) -> None:
        self.assert_atoms("cat > /tmp/notes.md <<'EOF'\nsleep 5\ntail /tmp/x.log\nEOF\nsleep 1",
                          WorkAtom(), WaitAtom("sleep"))
        self.assert_atoms("python3 - <<EOF\nimport time\nEOF", WorkAtom())
        self.assert_atoms("cat <<-END\n\tps aux\n\tEND\ntest -f /tmp/a.done", WorkAtom(), WaitAtom("test"))
        self.assert_atoms("cat /tmp/a.log <<< 'x'", WorkAtom())


class CodexTests(unittest.TestCase):
    def test_exec_cells(self) -> None:
        cases: tuple[tuple[str, tuple[Atom, ...]], ...] = (
            ('await tools.write_stdin({session_id: 3, chars: "", yield_time_ms: 30000});',
             (WaitAtom("empty write_stdin"),)),
            ("await tools.write_stdin({session_id: 3, yield_time_ms: 1000});", (WaitAtom("empty write_stdin"),)),
            ('await tools.write_stdin({session_id: 3, chars: "y\\n"});', (WorkAtom(),)),
            ("await new Promise(r => setTimeout(r, 45000));", (WaitAtom("setTimeout"),)),
            ("const total = 1 + 2;", (WorkAtom(),)),
            ('await tools.apply_patch("*** Begin Patch");', (WorkAtom(),)),
            ('await tools.exec_command({cmd: "sleep 30"}); await tools.list_agents({});',
             (WorkAtom(), WaitAtom("sleep"))),
            ('await tools.exec_command({"cmd": "tail -5 \\"/tmp/a b/build.log\\""});',
             (WaitAtom("tail *.log"),)),
            ("await tools.exec_command({cmd: `sleep 5\\ncargo build`});", (WaitAtom("sleep"), WorkAtom())),
            ('await tools.exec_command({cmd: "' + BOARD + ' post /tmp/b impl status \\"build started\\""});',
             (StatusAtom(),)),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(exec_cell_atoms(source), expected)

    def test_function_calls_and_other_items(self) -> None:
        cases: tuple[tuple[dict[str, object], tuple[Atom, ...]], ...] = (
            ({"type": "function_call", "name": "sleep", "arguments": "{}"}, (WaitAtom("sleep tool"),)),
            ({"type": "function_call", "name": "wait", "arguments": "{}"}, (WaitAtom("wait tool"),)),
            ({"type": "function_call", "name": "list_agents", "arguments": "{}"}, (WorkAtom(),)),
            ({"type": "custom_tool_call", "name": "apply_patch", "input": "patch"}, (WorkAtom(),)),
            ({"type": "web_search_call"}, (WorkAtom(),)),
            ({"type": "reasoning"}, ()),
            ({"type": "function_call_output", "output": "x"}, ()),
            ({"type": "custom_tool_call_output", "output": "x"}, ()),
            ({"type": "message", "role": "assistant", "content": []}, ()),
            ({"type": "message", "role": "user", "content": []}, ()),
            ({"type": "message", "role": "assistant", "phase": "final_answer", "content": []}, (WorkAtom(),)),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(codex_atoms(response_item(payload)), expected)
        self.assertEqual(codex_atoms({"type": "event_msg", "payload": {"type": "token_count"}}), ())

    def test_request_rule(self) -> None:
        poll = codex_atoms(exec_cell('await tools.write_stdin({session_id: 1, chars: ""});'))
        sleep = codex_atoms(response_item({"type": "function_call", "name": "sleep"}))
        status = codex_atoms(exec_cell('await tools.exec_command({cmd: "' + BOARD + ' post /b w status x"});'))
        final = codex_atoms(response_item({"type": "message", "role": "assistant", "phase": "final_answer"}))
        self.assertEqual(request(poll, sleep, status), Waiting("empty write_stdin"))
        self.assertEqual(request(sleep, poll), Waiting("sleep tool"))
        self.assertEqual(request(status), PostingStatus())
        self.assertEqual(request(poll, final), Working())
        self.assertEqual(request(), Working())
        self.assertEqual(request(shell_atoms("cd /tmp")), Working())


class ClaudeTests(unittest.TestCase):
    def test_tool_blocks(self) -> None:
        cases: tuple[tuple[dict[str, object], tuple[Atom, ...]], ...] = (
            (assistant(tool("Bash", command="sleep 45; tail -2 /tmp/build3.log")),
             (WaitAtom("sleep"), WaitAtom("tail *.log"))),
            (assistant(tool("Read", file_path=f"{TASKS}/b37sx2o7k.output")), (WaitAtom("Read *.output"),)),
            (assistant(tool("Read", file_path="/home/x/src/main.rs")), (WorkAtom(),)),
            (assistant(tool("Edit", file_path="a.py")), (WorkAtom(),)),
            (assistant(tool("Bash", command=f'{BOARD} post /tmp/b impl status "ok"')), (StatusAtom(),)),
            (assistant({"type": "text", "text": "sleep 5"}, {"type": "thinking", "thinking": "wait"}), ()),
            ({"type": "user", "message": {"content": [tool("Bash", command="sleep 1")]}}, ()),
        )
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(claude_atoms(record), expected)

    def test_text_only_message_is_work(self) -> None:
        self.assertEqual(request(claude_atoms(assistant({"type": "text", "text": "Done."}))), Working())


class StoredActivityTests(unittest.TestCase):
    def test_names_and_actions(self) -> None:
        values: tuple[tuple[RequestActivity, str, str | None], ...] = (
            (Working(), "work", None), (Waiting("sleep"), "wait", "sleep"), (PostingStatus(), "status", None))
        for value, name, action in values:
            with self.subTest(value=value):
                self.assertEqual((activity_name(value), wait_action(value)), (name, action))
                self.assertEqual(decode_activity(name, action), value)

    def test_decoder_refuses_inconsistent_pairs(self) -> None:
        for activity, action in (("wait", None), ("wait", ""), ("wait", 3), ("work", "sleep"),
                                 ("status", "x"), ("unclassified", None), (None, None), ("napping", None)):
            with self.subTest(activity=activity, action=action):
                with self.assertRaises(ValueError):
                    _ = decode_activity(activity, action)


if __name__ == "__main__":
    _ = unittest.main()
