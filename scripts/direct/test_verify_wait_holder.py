"""Wait recorder arguments use fake boards, holds and memory poll answers.

Verify's recorder functions and the extracted memory loop run with a fake
checker and meminfo. This module never consults live memory or build holds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest


VERIFY = Path(__file__).with_name("verify.sh")
FIRST_SESSION = "11111111-1111-4111-8111-111111111111"
NEXT_SESSION = "22222222-2222-4222-8222-222222222222"


class VerifyWaitHolderTests(unittest.TestCase):
    root: Path = Path()
    board: Path = Path()
    holds: Path = Path()
    environment: dict[str, str] = {}
    shell: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="verify-holder-test-")))
        self.board = self.root / "board"
        self.holds = self.root / "holds"
        for path in (self.holds, self.root / "waits", self.root / "home",
                     self.board / "locks" / "cargo.d"):
            path.mkdir(parents=True)
        _ = (self.board / "progress_history_state.json").write_text('{"unit":"compile-unit"}')
        recorder = self.root / "recorder.py"
        _ = recorder.write_text('''import json
import os
from pathlib import Path
import sys
with Path(os.environ["TEST_RECORDER_CALLS"]).open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "start-wait":
    print("fixture-wait")
if sys.argv[1] == "wait-holder" and os.environ.get("TEST_FAIL_HOLDER_ONCE"):
    failure = Path(os.environ["TEST_FAIL_HOLDER_ONCE"])
    if not failure.exists():
        failure.touch()
        sys.exit(1)
if sys.argv[1] == "wait-holder" and os.environ.get("TEST_HOLDER_UPDATE"):
    with Path(os.environ["TEST_HOLDER_UPDATE"]).open("w") as signal:
        signal.write("updated\\n")
''')
        source = VERIFY.read_text()
        start = source.index('PHASE_WAIT_SESSION="')
        end = source.index("\ncollect_routing_wait() {", start)
        self.shell = "\n".join((
            "set -euo pipefail",
            source[start:end],
            'PHASE_WAIT_RECORDER="$TEST_RECORDER"',
            'buildlog_memory_line_inner() { :; }',
        ))
        # Retain only command lookup. All domain readings have private paths;
        # inherited session ids, probe flags and admission overrides stay out.
        self.environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.root / "home"),
            "PY": sys.executable,
            "UNIT_RUN_DIR": str(self.board),
            "CODEX_THREAD_ID": "fixture-waiter",
            "HOLD_WAIT_DIR": str(self.root / "waits"),
            "BOARD_DIR": str(self.board),
            "BUILD_HOLD_DIR": str(self.holds),
            "BUILD_TARGETS_POLL_S": "0.01",
            "TEST_RECORDER": str(recorder),
            "TEST_RECORDER_CALLS": str(self.root / "calls.jsonl"),
        }

    def token(self, slot: str, session: str) -> None:
        _ = (self.board / "locks" / "cargo.d" / "holder").write_text(slot)
        _ = (self.board / f"impl_worker_{slot}.json").write_text(json.dumps({
            "slot": slot, "worker_id": session, "family": "codex",
        }))

    def hold(self, key: str, name: str, since: str = "2026-10-29T12:00:00+00:00") -> None:
        _ = (self.holds / key).write_text(json.dumps({
            "holder": name, "since": since, "for": "fixture", "release_eta": "unknown",
        }))

    def run_wait(self, commands: str) -> list[list[str]]:
        fixture = self.root / "fixture.sh"
        _ = fixture.write_text(self.shell + "\n" + commands + "\n")
        result = subprocess.run(["bash", str(fixture)], env=self.environment,
                                capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.root / "calls.jsonl"
        return ([cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
                if calls.exists() else [])

    def memory_loop(self, blockers: list[dict[str, str]], *, detailed: bool = True,
                    hook: bool = True, move_cache_to_admit: bool = False) -> list[list[str]]:
        gate = VERIFY.parent.parent / "lint" / "memory_gate.sh"
        match = re.search(r"(?ms)^buildlog_wait_for_memory\(\) \{\n.*?^\}", gate.read_text())
        assert match is not None
        loop = match.group(0)
        if move_cache_to_admit:
            # Cache placement is gate-owned and must not affect observation.
            assignment = '        cached_need=$need cached_source=$source cached_count=$count'
            self.assertIn(assignment, loop)
            loop = loop.replace(assignment, '').replace(
                '        if [[ "$state" == admit ]]; then',
                '        if [[ "$state" == admit ]]; then\n' + assignment)
        self.shell += "\n" + loop + '''
buildlog_memory_python() { "$PY" "$@"; }
buildlog_memory_refuse() { printf '%s\\n' "$*" >&2; return 2; }
sleep() { :; }
original_lookup="$(declare -f phase_wait_holder_flags)"
eval "${original_lookup/phase_wait_holder_flags/phase_wait_holder_flags_observed}"
phase_wait_holder_flags() {
    printf '%s\\n' "$1" >> "$TEST_HOLDER_LOOKUPS"
    phase_wait_holder_flags_observed "$@"
}
'''
        if not hook:
            self.shell += '''
unset -f buildlog_memory_poll_hook
buildlog_memory_line() { printf '<%s>\\n' "$@" >> "$TEST_MEMORY_LINES"; }
'''
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 20971520 kB\n")
        checker = self.root / "checker.py"
        _ = checker.write_text('''import json
import os
from pathlib import Path
import sys
assert sys.argv[1] == "check", "fixture refuses non-check calls, including live memory tripwires"
assert Path(sys.argv[2]) == Path(os.environ["BUILDLOG_MEMINFO"])
if os.environ["TEST_MEMORY_HOOK"] == "1":
    assert os.environ["BUILDLOG_MEM_BLOCKER"] == "1"
else:
    assert "BUILDLOG_MEM_BLOCKER" not in os.environ
assert os.environ["BUILDLOG_MEM_DETAIL"] == "1"
answers = Path(os.environ["TEST_MEMORY_ANSWERS"])
rows = json.loads(answers.read_text())
answer = rows.pop(0)
answers.write_text(json.dumps(rows))
print(answer)
''')
        answers: list[str] = []
        for index, blocker in enumerate([*blockers, {}]):
            admitted = index == len(blockers)
            fields = ["admit" if admitted else "hold", "12884901888", "8589934592", "fallback", "0",
                      "8589934592", "1", "0", "fixture", str(self.root / "reservation") if admitted else "",
                      "fixture", "check", "21474836480", "reserved" if admitted else "held", str(self.root)]
            if detailed:
                fields.extend(["0", "0", ""])
            if hook:
                fields.append(json.dumps(blocker, separators=(",", ":")).replace("|", "\\u007c"))
            answers.append("|".join(fields))
        answer_file = self.root / "answers.json"
        _ = answer_file.write_text(json.dumps(answers))
        self.environment.update({
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEM_ADMIT_SCRIPT": str(checker),
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE": "1",
            "TEST_MEMORY_ANSWERS": str(answer_file),
            "TEST_HOLDER_LOOKUPS": str(self.root / "lookups"),
            "TEST_MEMORY_HOOK": "1" if hook else "0",
            "TEST_MEMORY_LINES": str(self.root / "memory-lines"),
            "TEST_RESERVATION": str(self.root / "reservation"),
        })
        calls = self.run_wait('''buildlog_wait_for_memory cargo check
[[ "$BUILDLOG_MEM_OUTCOME" == Granted ]]
[[ "$BUILDLOG_MEM_RESERVATION" == "$TEST_RESERVATION" ]]''')
        self.assertEqual(json.loads(answer_file.read_text()), [], "all faked admission polls must run")
        self.assertEqual(list((self.root / "waits").iterdir()), [], "admission must close the memory wait")
        return calls

    def assert_holder(self, call: list[str], kind: str, resource: str, owner_flag: str, owner: str) -> None:
        expected = ["--holder-resource-kind", kind, "--holder-resource", resource, owner_flag, owner]
        first = call.index("--holder-resource-kind") if "--holder-resource-kind" in call else len(call)
        self.assertEqual(call[first:first + len(expected)], expected)
        self.assertEqual(sum(argument.startswith("--holder-") for argument in call), 3)

    def test_slot_names_the_board_token_owner(self) -> None:
        self.token("builder", FIRST_SESSION)
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assertEqual(len(calls), 1)
        self.assert_holder(calls[0], "slot", "cargo", "--holder-session", FIRST_SESSION)

    def test_director_slot_names_the_recorded_director(self) -> None:
        _ = (self.board / "locks" / "cargo.d" / "holder").write_text("director")
        _ = (self.board / "director").write_text(FIRST_SESSION)
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assert_holder(calls[0], "slot", "cargo", "--holder-session", FIRST_SESSION)

    def test_worker_name_resolves_its_session_when_the_durable_slot_differs(self) -> None:
        _ = (self.board / "locks/cargo.d/holder").write_text("project-test")
        _ = (self.board / "impl_worker_test.json").write_text(json.dumps({
            "name": "project-test", "slot": "test", "worker_id": FIRST_SESSION,
        }))
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assert_holder(calls[0], "slot", "cargo", "--holder-session", FIRST_SESSION)

    def test_worker_name_takes_precedence_over_another_workers_slot(self) -> None:
        self.token("project-test", NEXT_SESSION)
        _ = (self.board / "impl_worker_test.json").write_text(json.dumps({
            "name": "project-test", "slot": "test", "worker_id": FIRST_SESSION,
        }))
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assert_holder(calls[0], "slot", "cargo", "--holder-session", FIRST_SESSION)

    def test_slot_falls_back_to_the_known_unit(self) -> None:
        self.token("builder", "")
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assert_holder(calls[0], "slot", "cargo", "--holder-unit", "compile-unit")

    def test_conflicting_slot_sessions_fall_back_to_the_unit(self) -> None:
        self.token("builder", FIRST_SESSION)
        _ = (self.board / "impl_worker_former.json").write_text(json.dumps({
            "slot": "builder", "worker_id": NEXT_SESSION,
        }))
        calls = self.run_wait('phase_wait_start token "cargo token"')
        self.assert_holder(calls[0], "slot", "cargo", "--holder-unit", "compile-unit")

    def test_build_hold_names_the_holders_session(self) -> None:
        self.hold(FIRST_SESSION, "render-unit")
        calls = self.run_wait('phase_wait_start hold "build hold"; phase_wait_finish hold')
        self.assert_holder(calls[0], "slot", "natedev", "--holder-session", FIRST_SESSION)
        self.assertEqual(calls[-1][0], "finish-wait")

    def test_legacy_build_hold_names_its_unit(self) -> None:
        self.hold("render-unit", "render-unit")
        calls = self.run_wait('phase_wait_start hold "build hold"; phase_wait_finish hold')
        self.assert_holder(calls[0], "slot", "natedev", "--holder-unit", "render-unit")

    def test_multiple_build_holds_follow_admission_order(self) -> None:
        self.hold(NEXT_SESSION, "later", "2026-10-29T12:30:00+00:00")
        self.hold(FIRST_SESSION, "earlier")
        calls = self.run_wait('phase_wait_start hold "build hold"; phase_wait_finish hold')
        self.assert_holder(calls[0], "slot", "natedev", "--holder-session", FIRST_SESSION)

    def test_memory_names_the_owner_in_the_supplied_poll_answer(self) -> None:
        self.environment["TEST_BLOCKER"] = json.dumps({
            "session_id": FIRST_SESSION, "session_name": "render|unit", "pool": "memory",
        })
        calls = self.run_wait('phase_wait_start memory "memory admission" "$TEST_BLOCKER"')
        self.assert_holder(calls[0], "memory", "memory", "--holder-session", FIRST_SESSION)

    def test_memory_can_name_only_a_unit(self) -> None:
        calls = self.run_wait('phase_wait_start memory "memory admission" \'{"unit":"render-unit"}\'')
        self.assert_holder(calls[0], "memory", "memory", "--holder-unit", "render-unit")

    def test_memory_poll_updates_a_changed_holder_without_rechecking_unchanged_polls(self) -> None:
        first = {"session_id": FIRST_SESSION, "session_name": "render|unit", "pool": "memory"}
        calls = self.memory_loop([first, first, {"session_id": NEXT_SESSION, "pool": "memory"}])
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder", "finish-wait"])
        self.assert_holder(calls[0], "memory", "memory", "--holder-session", FIRST_SESSION)
        self.assert_holder(calls[1], "memory", "memory", "--holder-session", NEXT_SESSION)
        self.assertEqual((self.root / "lookups").read_text().splitlines(), ["memory", "memory"])
        self.assertEqual(calls[1][calls[1].index("--wait-id") + 1], "fixture-wait")

    def test_memory_poll_accepts_the_field_16_unit_holder(self) -> None:
        calls = self.memory_loop([{"unit": "render-unit"}], detailed=False)
        self.assertEqual([call[0] for call in calls], ["start-wait", "finish-wait"])
        self.assert_holder(calls[0], "memory", "memory", "--holder-unit", "render-unit")

    def test_memory_hook_observes_blocked_polls_when_cache_updates_only_on_admission(self) -> None:
        calls = self.memory_loop([{"session_id": FIRST_SESSION}, {"session_id": NEXT_SESSION}],
                                 move_cache_to_admit=True)
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder", "finish-wait"])
        self.assert_holder(calls[0], "memory", "memory", "--holder-session", FIRST_SESSION)
        self.assert_holder(calls[1], "memory", "memory", "--holder-session", NEXT_SESSION)

    def test_memory_gate_without_a_hook_preserves_ordinary_poll_output(self) -> None:
        self.assertEqual(self.memory_loop([{}], hook=False), [])
        common = ["21474836480", "12884901888", "8589934592", "fallback", "0",
                  "8589934592", "1", "0", "fixture"]
        expected = ["first", *common, "fixture", "check", "0", "0", "", "done", *common]
        self.assertEqual((self.root / "memory-lines").read_text().splitlines(),
                         [f"<{value}>" for value in expected])
        self.assertFalse((self.root / "lookups").exists())

    def test_memory_holder_recording_failure_retries_the_same_blocker_on_the_next_poll(self) -> None:
        self.environment["TEST_FAIL_HOLDER_ONCE"] = str(self.root / "recorder-failed")
        calls = self.memory_loop([{"session_id": FIRST_SESSION}, {"session_id": NEXT_SESSION},
                                  {"session_id": NEXT_SESSION}])
        self.assertTrue((self.root / "recorder-failed").exists())
        self.assertEqual([call[0] for call in calls],
                         ["start-wait", "wait-holder", "wait-holder", "finish-wait"])
        for call in calls[1:3]:
            self.assert_holder(call, "memory", "memory", "--holder-session", NEXT_SESSION)
            self.assertEqual(call[call.index("--wait-id") + 1], "fixture-wait")

    def test_memory_poll_with_no_single_blocker_passes_no_holder_flags(self) -> None:
        calls = self.memory_loop([{}, {}])
        self.assertEqual([call[0] for call in calls], ["start-wait", "finish-wait"])
        self.assertFalse(any(argument.startswith("--holder-") for call in calls for argument in call))
        self.assertEqual((self.root / "lookups").read_text().splitlines(), ["memory"])

    def test_memory_admitted_on_the_first_poll_records_no_wait(self) -> None:
        self.assertEqual(self.memory_loop([]), [])
        self.assertFalse((self.root / "lookups").exists())

    def test_memory_identity_changes_update_and_names_alone_do_not(self) -> None:
        self.environment["TEST_FIRST_BLOCKER"] = json.dumps({"session_id": FIRST_SESSION, "session_name": "before"})
        self.environment["TEST_RENAMED_BLOCKER"] = json.dumps({"session_id": FIRST_SESSION, "session_name": "after"})
        self.environment["TEST_NEXT_BLOCKER"] = json.dumps({"session_id": NEXT_SESSION, "pool": "memory"})
        calls = self.run_wait('''phase_wait_start memory "memory admission" "$TEST_FIRST_BLOCKER"
phase_wait_start memory "memory admission" "$TEST_RENAMED_BLOCKER"
phase_wait_start memory "memory admission" "$TEST_NEXT_BLOCKER"
phase_wait_start memory "memory admission" '{}'
phase_wait_start memory "memory admission" '{}'
phase_wait_finish memory''')
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder", "wait-holder", "finish-wait"])
        self.assert_holder(calls[1], "memory", "memory", "--holder-session", NEXT_SESSION)
        self.assertFalse(any(argument.startswith("--holder-") for argument in calls[2]))

    def test_holder_change_updates_the_same_wait_once(self) -> None:
        self.token("builder", FIRST_SESSION)
        self.environment["TEST_NEXT_WORKER"] = json.dumps({"slot": "builder", "worker_id": NEXT_SESSION})
        calls = self.run_wait('''phase_wait_start token "cargo token"
phase_wait_start token "cargo token"
printf '%s' "$TEST_NEXT_WORKER" > "$BOARD_DIR/impl_worker_builder.json"
phase_wait_start token "cargo token"
phase_wait_start token "cargo token"
phase_wait_finish token''')
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder", "finish-wait"])
        self.assert_holder(calls[1], "slot", "cargo", "--holder-session", NEXT_SESSION)
        self.assertEqual(calls[1][calls[1].index("--wait-id") + 1], "fixture-wait")

    def test_build_hold_change_is_observed_while_the_caller_is_blocked(self) -> None:
        self.hold(FIRST_SESSION, "render-unit")
        self.environment["TEST_FIRST_SESSION"] = FIRST_SESSION
        self.environment["TEST_NEXT_SESSION"] = NEXT_SESSION
        self.environment["TEST_HOLDER_UPDATE"] = str(self.root / "holder-update")
        os.mkfifo(self.environment["TEST_HOLDER_UPDATE"])
        calls = self.run_wait('''phase_wait_start hold "build hold"
mv "$BUILD_HOLD_DIR/$TEST_FIRST_SESSION" "$BUILD_HOLD_DIR/$TEST_NEXT_SESSION"
IFS= read -r update < "$TEST_HOLDER_UPDATE"
phase_wait_start hold "build hold"
phase_wait_finish hold''')
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder", "finish-wait"])
        self.assert_holder(calls[1], "slot", "natedev", "--holder-session", NEXT_SESSION)
        self.assertFalse(list((self.root / "waits").iterdir()), "closing a wait must remove observer state")

    def test_lost_holder_clears_the_same_wait(self) -> None:
        self.token("builder", FIRST_SESSION)
        calls = self.run_wait('''phase_wait_start token "cargo token"
rm "$BOARD_DIR/locks/cargo.d/holder"
phase_wait_start token "cargo token"
phase_wait_start token "cargo token"''')
        self.assertEqual([call[0] for call in calls], ["start-wait", "wait-holder"])
        self.assertFalse(any(argument.startswith("--holder-") for argument in calls[1]))

    def test_no_holder_passes_none_of_the_holder_flags(self) -> None:
        calls = self.run_wait('''phase_wait_start token "cargo token"
phase_wait_start token "cargo token"
phase_wait_finish token
phase_wait_start memory "memory admission" '{}'
phase_wait_finish memory
phase_wait_start hold "build hold"
phase_wait_finish hold''')
        self.assertEqual([call[0] for call in calls], ["start-wait", "finish-wait"] * 3)
        self.assertFalse(any(argument.startswith("--holder-") for call in calls for argument in call))


if __name__ == "__main__":
    _ = unittest.main()
