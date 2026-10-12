#!/usr/bin/env python3
"""Pool CLI contracts, using real processes and a controlled mesh subprocess."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("review_pool.py")

# A gate controls completion, not a guessed duration. Each invocation records
# its own event file so concurrent subprocesses cannot corrupt a shared log.
STUB_MESH = r'''#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
verbs = parser.add_subparsers(dest="verb", required=True)
for verb in ("prime", "fork", "reset", "end", "stop"):
    command = verbs.add_parser(verb)
    command.add_argument("--session-dir", required=True)
    if verb == "prime":
        command.add_argument("--name", required=True)
        command.add_argument("--instructions-file", required=True)
        command.add_argument("--cwd", required=True)
        command.add_argument("--model")
        command.add_argument("--service-tier")
    elif verb == "fork":
        command.add_argument("--from", dest="source", required=True)
        command.add_argument("--name", required=True)
    elif verb in ("reset", "end"):
        command.add_argument("--to", required=True)
args = parser.parse_args()
session = Path(args.session_dir)
with (Path(os.environ["REVIEW_POOL_STATE"]) / "production" / "pool.lock").open("a+") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("mesh called while pool.lock is held")
    fcntl.flock(lock, fcntl.LOCK_UN)
name = getattr(args, "name", getattr(args, "to", ""))
control = json.loads(Path(os.environ["POOL_STUB_CONTROL"]).read_text())
setting = control.get(args.verb + ":" + name, control.get(args.verb, {}))
events = Path(os.environ["POOL_STUB_EVENTS"])
previous = sum(
    json.loads(path.read_text()).get("key") == args.verb + ":" + name
    for path in events.glob("*.json")
)
event = {"key": args.verb + ":" + name, "verb": args.verb,
         "name": name, "argv": sys.argv[1:], "pid": os.getpid()}
if args.verb == "prime":
    instructions = Path(args.instructions_file)
    if not instructions.is_absolute() or not instructions.is_file():
        sys.exit("prime needs an absolute readable instructions file")
    if not Path(args.cwd).is_dir():
        sys.exit("prime needs an existing cwd")
    event["instructions"] = instructions.read_text()
    event["cwd_entries"] = [path.name for path in Path(args.cwd).iterdir()]
temporary = events / (str(os.getpid()) + ".tmp")
temporary.write_text(json.dumps(event))
temporary.rename(events / (str(os.getpid()) + "-" + str(time.time_ns()) + ".json"))
gate = setting.get("gate", "")
while gate and not Path(gate).exists():
    time.sleep(0.01)
if previous < setting.get("failures", 10**9):
    failure = setting.get("exit", 0)
    if failure:
        print("scripted " + args.verb + " failure", file=sys.stderr)
        sys.exit(failure)
if args.verb in ("prime", "fork"):
    thread_id = setting.get("thread_id", "thread-" + name)
    session.mkdir(parents=True, exist_ok=True)
    with (session / "roster.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        roster_path = session / "mesh_roster.json"
        roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
        roster[name] = {"thread_id": thread_id, "status": "done"}
        roster_path.write_text(json.dumps(roster))
    print(thread_id)
'''


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object, got {value!r}")
    return cast("dict[str, object]", value)


def parse_object(text: str) -> dict[str, object]:
    return json_object(cast("object", json.loads(text)))


class ReviewPoolTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    pool: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    instructions: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stub: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    events_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    control_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    processes: list[subprocess.Popen[str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    gates: list[Path]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pool = self.root / "state" / "production"
        self.instructions = self.root / "instructions.md"
        _ = self.instructions.write_text("Review only.\nPreserve café.\n", encoding="utf-8")
        self.stub = self.root / "codex_mesh.py"
        _ = self.stub.write_text(STUB_MESH, encoding="utf-8")
        self.stub.chmod(0o755)
        self.events_dir = self.root / "events"
        self.events_dir.mkdir()
        self.control_file = self.root / "control.json"
        self.control({})
        self.processes = []
        self.gates = []

    @override
    def tearDown(self) -> None:
        for gate in self.gates:
            gate.touch()
        for process in self.processes:
            try:
                _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _ = process.communicate(timeout=5)
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "REVIEW_POOL_STATE": str(self.root / "state"),
            "REVIEW_POOL_MESH": str(self.stub),
            "POOL_STUB_CONTROL": str(self.control_file),
            "POOL_STUB_EVENTS": str(self.events_dir),
        })
        return environment

    def command(self, *arguments: str) -> list[str]:
        return [sys.executable, str(SCRIPT), arguments[0], "--production", "production", *arguments[1:]]

    def run_pool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments), env=self.environment(), capture_output=True,
            text=True, check=False, timeout=10,
        )

    def succeed(self, *arguments: str) -> str:
        result = self.run_pool(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def start_pool(self, *arguments: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            self.command(*arguments), env=self.environment(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(process)
        return process

    def finish(self, process: subprocess.Popen[str]) -> tuple[int, str, str]:
        output, errors = process.communicate(timeout=10)
        assert process.returncode is not None
        return process.returncode, output.strip(), errors

    def control(self, settings: dict[str, object]) -> None:
        _ = self.control_file.write_text(json.dumps(settings), encoding="utf-8")

    def hold(self, key: str) -> Path:
        gate = self.root / f"{key.replace(':', '-')}.gate"
        self.gates.append(gate)
        self.control({key: {"gate": str(gate)}})
        return gate

    def events(self, verb: str = "") -> list[dict[str, object]]:
        events = [parse_object(path.read_text(encoding="utf-8"))
                  for path in sorted(self.events_dir.glob("*.json"))]
        for event in events:
            argv = cast("list[str]", event["argv"])
            mesh_dir = Path(argv[argv.index("--session-dir") + 1])
            self.assertEqual(mesh_dir.parent, self.pool / "mesh")
            self.assertTrue(mesh_dir.name.isdecimal(), str(mesh_dir))
        return [event for event in events if not verb or event["verb"] == verb]

    def event_mesh_dir(self, event: dict[str, object]) -> Path:
        argv = cast("list[str]", event["argv"])
        return Path(argv[argv.index("--session-dir") + 1])

    def assert_fork_names_are_unique_per_generation(self) -> None:
        issued = [(self.event_mesh_dir(event), str(event["name"])) for event in self.events("fork")]
        self.assertEqual(len(issued), len(set(issued)), issued)

    def await_event(self, verb: str, name: str, process: subprocess.Popen[str]) -> dict[str, object]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for event in self.events(verb):
                if event["name"] == name:
                    return event
            if process.poll() is not None:
                self.fail(f"process exited before {verb}:{name}: {self.finish(process)}")
            time.sleep(0.01)
        self.fail(f"no {verb}:{name} event before deadline")

    def state(self) -> dict[str, object]:
        return parse_object((self.pool / "pool.json").read_text(encoding="utf-8"))

    def reviewers(self) -> dict[str, object]:
        return json_object(self.state()["reviewers"])

    def reviewer(self, name: str) -> dict[str, object]:
        return json_object(self.reviewers()[name])

    def write_state(self, state: dict[str, object]) -> None:
        self.pool.mkdir(parents=True, exist_ok=True)
        _ = (self.pool / "pool.json").write_text(json.dumps(state), encoding="utf-8")

    def seed(self, entries: dict[str, object], split: str = "hash") -> None:
        self.write_state({
            "split": split, "generation": 0,
            "templates": {"code": {"name": "code-template-current", "thread_id": "template-thread",
                                    "prime_hash": "current"}},
            "reviewers": entries,
        })

    def idle(self, unit: str = "", phase: str = "phase1", *, prime_hash: str = "current",
             served: tuple[tuple[str, str], ...] = ()) -> dict[str, object]:
        return {
            "type": "code", "thread_id": "existing-thread", "prime_hash": prime_hash,
            "lease": {"unit": unit, "phase": phase, "ordinal": 3} if unit else None,
            "busy_pid": None, "served": [list(entry) for entry in served], "reviews": 5,
            "last_allocated": time.time(),
        }

    def allocation_args(self, unit: str = "unit", phase: str = "phase1", review_pass: str = "1",
                        lens: str = "contract", reviewer_type: str = "code") -> list[str]:
        return ["allocate", "--type", reviewer_type, "--unit", unit, "--phase", phase,
                "--pass", review_pass, "--lens", lens, "--pid", str(os.getpid())]

    def allocate(self, **options: str) -> dict[str, object]:
        output = self.succeed(*self.allocation_args(**options))
        self.assertEqual(len(output.splitlines()), 1, output)
        result = parse_object(output)
        self.assertEqual(set(result), {"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"})
        self.assertEqual(result["mesh_dir"], str(self.pool / "mesh" / str(self.state()["generation"])))
        return result

    def prime(self, reviewer_type: str = "code") -> str:
        return self.succeed("prime", "--type", reviewer_type,
                            "--instructions-file", str(self.instructions))

    def release(self, name: str) -> None:
        _ = self.succeed("release", "--reviewer", name, "--pid", str(os.getpid()))

    def test_new_pool_uses_default_split_and_generation_and_content_addressed_prime(self) -> None:
        expected = hashlib.sha256(self.instructions.read_bytes()).hexdigest()
        self.assertEqual(self.prime(), expected)
        state = self.state()
        self.assertEqual(state["split"], "hash")
        self.assertEqual(state["generation"], 0)
        self.assertEqual(state["next_index"], {"code": 1, "ui": 1})
        self.assertEqual(state["reviewers"], {})
        template = json_object(json_object(state["templates"])["code"])
        self.assertEqual(template, {"name": f"code-template-{expected[:8]}",
                                    "thread_id": f"thread-code-template-{expected[:8]}", "prime_hash": expected})
        copy = self.pool / "prime" / f"{expected}.md"
        self.assertEqual(copy.read_bytes(), self.instructions.read_bytes())
        event = self.events("prime")[0]
        self.assertEqual(self.event_mesh_dir(event), self.pool / "mesh" / "0")
        argv = cast("list[str]", event["argv"])
        self.assertEqual(argv[argv.index("--instructions-file") + 1], str(copy))
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.pool / "cwd"))
        self.assertEqual(event["cwd_entries"], [])
        self.assertEqual(event["instructions"], self.instructions.read_text(encoding="utf-8"))
        self.assertTrue((self.pool / "pool.lock").is_file())

    def test_identical_prime_reuses_template_without_another_mesh_call(self) -> None:
        digest = self.prime()
        self.assertEqual(self.prime(), digest)
        self.assertEqual(len(self.events("prime")), 1)

    def test_changed_prime_preserves_old_file_for_reviewer_and_new_lease_uses_new_hash(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        self.release(str(old["reviewer"]))
        _ = self.instructions.write_text("A changed standing contract.\n", encoding="utf-8")
        new_hash = self.prime()
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual((self.pool / "prime" / f"{old_hash}.md").read_text(encoding="utf-8"),
                         "Review only.\nPreserve café.\n")
        new = self.allocate(unit="another-unit")
        self.assertNotEqual(new["reviewer"], old["reviewer"])
        self.assertEqual(new["prime_hash"], new_hash)
        self.assertEqual(self.reviewer(str(old["reviewer"]))["prime_hash"], old_hash)

    def test_changed_prime_retires_unleased_old_contract_and_deletes_its_unused_file(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        old_name = str(old["reviewer"])
        self.release(old_name)
        _ = self.succeed("end-lease", "--unit", "unit")
        self.assertIsNone(self.reviewer(old_name)["lease"])
        old_file = self.pool / "prime" / f"{old_hash}.md"
        self.assertTrue(old_file.is_file())

        _ = self.instructions.write_text("Replacement contract.\n", encoding="utf-8")
        new_hash = self.prime()

        self.assertNotEqual(new_hash, old_hash)
        self.assertNotIn(old_name, self.reviewers())
        self.assertEqual([event["name"] for event in self.events("end")], [old_name])
        self.assertFalse(old_file.exists())
        self.assertTrue((self.pool / "prime" / f"{new_hash}.md").is_file())
        replacement = self.allocate()
        self.assertEqual(replacement["reviewer"], "code2")
        self.assertEqual(replacement["prime_hash"], new_hash)
        self.assertEqual(replacement["mesh_dir"], old["mesh_dir"])
        self.assert_fork_names_are_unique_per_generation()

    def test_changed_prime_leaves_old_contract_phase_lease_and_its_file_untouched(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        old_name = str(old["reviewer"])
        self.release(old_name)
        before = self.reviewer(old_name)
        old_file = self.pool / "prime" / f"{old_hash}.md"
        old_content = old_file.read_bytes()

        _ = self.instructions.write_text("Replacement contract.\n", encoding="utf-8")
        self.assertNotEqual(self.prime(), old_hash)

        self.assertEqual(self.reviewer(old_name), before)
        self.assertEqual(self.events("end"), [])
        self.assertEqual(old_file.read_bytes(), old_content)

    def test_prime_failure_does_not_publish_a_template(self) -> None:
        self.control({"prime": {"exit": 1}})
        result = self.run_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failure", result.stderr)
        self.assertEqual(self.state()["templates"], {})

    def test_arm_without_pool_or_without_code_template_is_own(self) -> None:
        self.assertEqual(self.succeed("arm", "--unit", "unit", "--phase", "phase1"), "own")
        self.write_state({"split": "all", "generation": 0, "templates": {}, "reviewers": {}})
        self.assertEqual(self.succeed("arm", "--unit", "unit", "--phase", "phase1"), "own")

    def test_split_all_and_off_select_the_requested_arm(self) -> None:
        self.seed({})
        for split, arm in (("all", "pool"), ("off", "own")):
            with self.subTest(split=split):
                _ = self.succeed("split", "--set", split)
                self.assertEqual(self.state()["split"], split)
                self.assertEqual(self.succeed("arm", "--unit", "unit", "--phase", "phase1"), arm)

    def test_hash_arm_uses_first_byte_of_unit_newline_phase(self) -> None:
        self.seed({})
        observed: set[str] = set()
        for index in range(12):
            unit = f"unit-{index}"
            digest = hashlib.sha256(f"{unit}\nphase1".encode()).digest()
            expected = "pool" if digest[0] % 2 == 0 else "own"
            self.assertEqual(self.succeed("arm", "--unit", unit, "--phase", "phase1"), expected)
            observed.add(expected)
        self.assertEqual(observed, {"own", "pool"})

    def test_allocation_prefers_existing_phase_lease_over_unleased_reviewer(self) -> None:
        self.seed({"code1": self.idle(), "code2": self.idle("unit")})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code2")
        self.assertEqual(result["lease_ordinal"], 4)
        self.assertEqual(self.reviewer("code2")["busy_pid"], os.getpid())
        self.assertEqual(self.reviewer("code2")["served"], [["1", "contract"]])
        self.assertEqual(self.events(), [])

    def test_allocation_reuses_same_lens_but_excludes_other_lens_on_same_pass(self) -> None:
        self.seed({"code1": self.idle("unit", served=(("1", "contract"),)), "code2": self.idle()})
        self.assertEqual(self.allocate()["reviewer"], "code1")
        self.release("code1")
        self.assertEqual(self.allocate(lens="adversary")["reviewer"], "code2")

    def test_previous_pass_other_lens_does_not_exclude_phase_reviewer(self) -> None:
        self.seed({"code1": self.idle("unit", served=(("1", "adversary"),))})
        self.assertEqual(self.allocate(review_pass="2")["reviewer"], "code1")

    def test_old_hash_phase_lease_is_reused_until_lease_end(self) -> None:
        self.seed({"code1": self.idle("unit", prime_hash="old"), "code2": self.idle()})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code1")
        self.assertEqual(result["prime_hash"], "old")
        self.assertEqual(self.events("fork"), [])

    def test_allocate_accepts_omitted_lens(self) -> None:
        self.seed({"code1": self.idle()})
        arguments = self.allocation_args()
        index = arguments.index("--lens")
        del arguments[index:index + 2]
        result = parse_object(self.succeed(*arguments))
        self.assertEqual(result["reviewer"], "code1")
        self.assertEqual(result["lease_ordinal"], 1)

    def test_unleased_reviewer_takes_lease_with_first_ordinal(self) -> None:
        self.seed({"code1": self.idle()})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code1")
        self.assertEqual(result["lease_ordinal"], 1)
        self.assertEqual(self.reviewer("code1")["lease"], {"unit": "unit", "phase": "phase1", "ordinal": 1})

    def test_allocation_skips_busy_wrong_type_other_phase_and_old_unleased_reviewers(self) -> None:
        busy = {**self.idle("unit"), "busy_pid": os.getpid()}
        ui = {**self.idle("unit"), "type": "ui"}
        self.seed({"code1": busy, "ui1": ui, "code2": self.idle("unit", "phase2"),
                   "code3": self.idle(prime_hash="old"), "code4": self.idle()})
        self.assertEqual(self.allocate()["reviewer"], "code4")
        self.assertEqual(self.events("fork"), [])

    def test_dead_owner_is_idle_and_last_allocated_is_refreshed(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=5)
        self.seed({"code1": {**self.idle("unit"), "busy_pid": child.pid, "last_allocated": 0}})
        self.assertEqual(self.allocate()["reviewer"], "code1")
        self.assertNotEqual(self.reviewer("code1")["last_allocated"], 0)

    def test_legacy_pool_without_name_counters_skips_existing_name_and_records_fork(self) -> None:
        self.seed({"code1": {**self.idle(), "busy_pid": os.getpid()}})
        self.assertNotIn("next_index", self.state())
        self.control({"fork": {"thread_id": "chosen-fork-thread"}})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code2")
        self.assertEqual(result["thread_id"], "chosen-fork-thread")
        self.assertEqual(result["lease_ordinal"], 1)
        event = self.events("fork")[0]
        argv = cast("list[str]", event["argv"])
        self.assertEqual(argv[argv.index("--from") + 1], "code-template-current")
        self.assertEqual(self.reviewer("code2")["thread_id"], "chosen-fork-thread")
        self.assertEqual(self.state()["next_index"], {"code": 3, "ui": 1})

    def test_ui_allocation_uses_ui_template_and_name(self) -> None:
        _ = self.prime("ui")
        result = self.allocate(reviewer_type="ui")
        self.assertEqual(result["reviewer"], "ui1")
        self.assertEqual(self.reviewer("ui1")["type"], "ui")

    def test_missing_template_and_failed_fork_exit_one_without_a_reservation(self) -> None:
        self.seed({})
        missing = self.run_pool(*self.allocation_args(reviewer_type="ui"))
        self.assertEqual(missing.returncode, 1)
        self.assertTrue(missing.stderr.strip())
        self.assertEqual(self.events("fork"), [])
        self.control({"fork": {"exit": 2}})
        failed = self.run_pool(*self.allocation_args())
        self.assertEqual(failed.returncode, 1)
        self.assertIn("failure", failed.stderr)
        self.assertEqual(self.reviewers(), {})

    def test_failed_fork_consumes_its_reviewer_name_in_the_generation(self) -> None:
        self.seed({})
        self.control({"fork": {"exit": 1}})
        failed = self.run_pool(*self.allocation_args())
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertIn("scripted fork failure", failed.stderr)
        self.assertEqual(self.reviewers(), {})
        self.assertEqual([event["name"] for event in self.events("fork")], ["code1"])
        self.assertEqual(json_object(self.state()["next_index"])["code"], 2)

        self.control({})
        replacement = self.allocate()

        self.assertEqual(replacement["reviewer"], "code2")
        self.assert_fork_names_are_unique_per_generation()

    def test_name_issuance_is_independent_for_code_and_ui_reviewers(self) -> None:
        _ = self.prime("code")
        _ = self.prime("ui")
        self.assertEqual(self.allocate()["reviewer"], "code1")
        self.assertEqual(self.allocate(reviewer_type="ui")["reviewer"], "ui1")
        self.control({"fork:code2": {"exit": 1}})
        failed = self.run_pool(*self.allocation_args(unit="other-unit"))
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(self.events("fork")[-1]["name"], "code2")

        self.control({})
        self.assertEqual(self.allocate(unit="other-unit")["reviewer"], "code3")
        self.assertEqual(self.allocate(unit="other-unit", reviewer_type="ui")["reviewer"], "ui2")
        self.assert_fork_names_are_unique_per_generation()

    def test_release_requires_matching_owner_and_preserves_lease(self) -> None:
        self.seed({"code1": {**self.idle("unit"), "busy_pid": os.getpid()}})
        before = self.reviewer("code1")
        _ = self.succeed("release", "--reviewer", "code1", "--pid", str(os.getpid() + 1))
        self.assertEqual(self.reviewer("code1"), before)
        self.release("code1")
        after = self.reviewer("code1")
        self.assertIsNone(after["busy_pid"])
        self.assertEqual(after["reviews"], 6)
        self.assertEqual(after["lease"], before["lease"])

    def test_stalled_fork_reservation_does_not_block_concurrent_allocation(self) -> None:
        self.seed({})
        gate = self.hold("fork:code1")
        first = self.start_pool(*self.allocation_args())
        _ = self.await_event("fork", "code1", first)
        reserved = self.reviewer("code1")
        self.assertFalse(reserved.get("thread_id"))
        self.assertEqual(reserved["busy_pid"], os.getpid())
        second = self.start_pool(*self.allocation_args())
        code, output, errors = self.finish(second)
        self.assertEqual(code, 0, errors)
        self.assertEqual(parse_object(output)["reviewer"], "code2")
        self.assertIsNone(first.poll(), "second allocation must finish while first fork is held")
        gate.touch()
        code, output, errors = self.finish(first)
        self.assertEqual(code, 0, errors)
        self.assertEqual(parse_object(output)["reviewer"], "code1")
        self.assertEqual(len(self.reviewers()), 2)

    def test_reserved_threadless_reviewer_is_skipped_even_when_owner_is_dead(self) -> None:
        self.seed({"code1": {**self.idle(), "thread_id": "", "busy_pid": None}})
        self.assertEqual(self.allocate()["reviewer"], "code2")

    def test_fresh_resets_chosen_reviewer_before_publishing_and_keeps_lease(self) -> None:
        self.seed({"code1": self.idle("unit")})
        gate = self.hold("reset:code1")
        process = self.start_pool(*self.allocation_args(), "--fresh")
        _ = self.await_event("reset", "code1", process)
        self.assertEqual(self.reviewer("code1")["busy_pid"], os.getpid())
        self.assertIsNone(process.poll())
        gate.touch()
        code, output, errors = self.finish(process)
        self.assertEqual(code, 0, errors)
        self.assertEqual(parse_object(output)["lease_ordinal"], 4)
        self.assertEqual(self.reviewer("code1")["lease"], {"unit": "unit", "phase": "phase1", "ordinal": 4})
        self.assertEqual(self.reviewer("code1")["busy_pid"], os.getpid())

    def test_fresh_reset_failure_retires_reviewer_and_retries_allocation(self) -> None:
        self.seed({"code1": self.idle("unit")})
        self.control({"reset": {"exit": 2, "failures": 1}})
        result = self.run_pool(*self.allocation_args(), "--fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        allocated = parse_object(result.stdout)
        self.assertNotEqual(allocated["thread_id"], "existing-thread")
        self.assertTrue(self.events("end"))
        self.assertIn("code1", result.stderr)
        self.assertTrue(self.events("fork"))

    def test_fresh_returns_a_new_fork_without_resetting_it(self) -> None:
        self.seed({})
        result = parse_object(self.succeed(*self.allocation_args(), "--fresh"))
        self.assertEqual(result["reviewer"], "code1")
        self.assertEqual(len(self.events("fork")), 1)
        self.assertEqual(self.events("reset"), [])
        self.assertEqual(self.reviewer("code1")["busy_pid"], os.getpid())

    def test_fresh_reset_request_failure_also_retires_and_retries(self) -> None:
        self.seed({"code1": self.idle("unit")})
        self.control({"reset": {"exit": 1, "failures": 1}})
        result = self.run_pool(*self.allocation_args(), "--fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(parse_object(result.stdout)["thread_id"], "existing-thread")
        self.assertIn("code1", result.stderr)
        self.assertEqual(len(self.events("end")), 1)

    def test_fresh_with_persistent_reset_failure_returns_one_replacement_fork(self) -> None:
        self.seed({"code1": self.idle("unit")})
        self.control({"reset": {"exit": 1}})
        process = self.start_pool(*self.allocation_args(), "--fresh")
        code, output, errors = self.finish(process)
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(output.splitlines()), 1)
        allocation = parse_object(output)
        self.assertNotEqual(allocation["thread_id"], "existing-thread")
        self.assertEqual(len(self.events("reset")), 1)
        self.assertEqual(len(self.events("end")), 1)
        self.assertEqual(len(self.events("fork")), 1)
        self.assertEqual(self.reviewer(str(allocation["reviewer"]))["thread_id"], allocation["thread_id"])

    def test_fresh_retirement_never_reissues_the_retired_reviewer_name(self) -> None:
        self.seed({})
        original = self.allocate()
        self.assertEqual(original["reviewer"], "code1")
        self.release("code1")
        self.control({"reset": {"exit": 1}})

        result = self.run_pool(*self.allocation_args(), "--fresh")

        self.assertEqual(result.returncode, 0, result.stderr)
        replacement = parse_object(result.stdout)
        self.assertEqual(replacement["reviewer"], "code2")
        self.assertEqual(replacement["mesh_dir"], original["mesh_dir"])
        self.assertNotIn("code1", self.reviewers())
        self.assertEqual([event["name"] for event in self.events("reset")], ["code1"])
        self.assertEqual([event["name"] for event in self.events("end")], ["code1"])
        self.assert_fork_names_are_unique_per_generation()

    def test_end_lease_resets_idle_reviewer_and_clears_lease_served_and_owner(self) -> None:
        self.seed({"code1": self.idle("unit", served=(("1", "contract"),)),
                   "code2": self.idle("other-unit"), "code3": self.idle("unit", "phase2")})
        before_other = self.reviewer("code2")
        before_phase = self.reviewer("code3")
        _ = self.succeed("end-lease", "--unit", "unit", "--phase", "phase1")
        record = self.reviewer("code1")
        self.assertIsNone(record["lease"])
        self.assertIsNone(record["busy_pid"])
        self.assertEqual(record["served"], [])
        self.assertEqual(record["reviews"], 5)
        self.assertEqual(self.reviewer("code2"), before_other)
        self.assertEqual(self.reviewer("code3"), before_phase)
        self.assertEqual([event["name"] for event in self.events("reset")], ["code1"])
        reused = self.allocate(unit="new-unit")
        self.assertEqual(reused["reviewer"], "code1")
        self.assertEqual(reused["lease_ordinal"], 1)
        self.assertEqual(self.events("fork"), [])

    def test_end_lease_without_phase_clears_every_phase_for_the_unit(self) -> None:
        self.seed({"code1": self.idle("unit"), "code2": self.idle("unit", "phase2")})
        _ = self.succeed("end-lease", "--unit", "unit")
        self.assertIsNone(self.reviewer("code1")["lease"])
        self.assertIsNone(self.reviewer("code2")["lease"])

    def test_end_lease_leaves_busy_reviewer_and_names_it_on_stderr(self) -> None:
        record = {**self.idle("unit"), "busy_pid": os.getpid()}
        self.seed({"code1": record})
        result = self.run_pool("end-lease", "--unit", "unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("code1", result.stderr)
        self.assertEqual(self.reviewer("code1"), record)
        self.assertEqual(self.events(), [])

    def test_end_lease_and_health_remove_reservations_with_dead_owners(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=5)
        for arguments in (("end-lease", "--unit", "unit"), ("health",)):
            with self.subTest(verb=arguments[0]):
                self.seed({"code1": {
                    **self.idle("unit"), "thread_id": "", "busy_pid": child.pid,
                    "last_allocated": time.time() - 13 * 3600,
                }})
                result = self.run_pool(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("code1", self.reviewers())
                self.assertIn("code1", result.stderr)
                self.assertEqual(self.events("reset"), [])

    def test_end_lease_and_health_leave_live_reservations_and_report_them_busy(self) -> None:
        record = {
            **self.idle("unit"), "thread_id": "", "busy_pid": os.getpid(),
            "last_allocated": time.time() - 13 * 3600,
        }
        for arguments in (("end-lease", "--unit", "unit"), ("health",)):
            with self.subTest(verb=arguments[0]):
                self.seed({"code1": record})
                result = self.run_pool(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.reviewer("code1"), record)
                self.assertIn("code1", result.stderr)
                self.assertIn("busy", result.stderr.lower())
                self.assertEqual(self.events(), [])

    def test_removed_dead_reservation_does_not_free_its_issued_reviewer_name(self) -> None:
        self.seed({})
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=5)
        gate = self.hold("fork:code1")
        arguments = self.allocation_args()
        arguments[arguments.index("--pid") + 1] = str(child.pid)
        pending = self.start_pool(*arguments)
        first_event = self.await_event("fork", "code1", pending)
        self.assertEqual(self.reviewer("code1")["thread_id"], "")

        ended = self.run_pool("end-lease", "--unit", "unit")
        self.assertEqual(ended.returncode, 0, ended.stderr)
        self.assertIn("code1", ended.stderr)
        self.assertNotIn("code1", self.reviewers())
        self.control({})
        replacement = self.allocate()
        self.assertEqual(replacement["reviewer"], "code2")
        self.assertEqual(Path(str(replacement["mesh_dir"])), self.event_mesh_dir(first_event))
        self.assertIsNone(pending.poll(), "replacement must finish while obsolete fork is held")
        self.assert_fork_names_are_unique_per_generation()

        gate.touch()
        code, output, errors = self.finish(pending)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        roster = parse_object((Path(str(replacement["mesh_dir"])) / "mesh_roster.json").read_text(encoding="utf-8"))
        self.assertEqual(json_object(roster["code2"])["thread_id"], replacement["thread_id"])

    def test_end_lease_retires_reviewer_on_each_reset_failure_exit(self) -> None:
        for exit_code in (1, 2):
            with self.subTest(exit_code=exit_code):
                self.seed({"code1": self.idle("unit")})
                self.control({"reset": {"exit": exit_code}})
                result = self.run_pool("end-lease", "--unit", "unit")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("code1", result.stderr)
                self.assertEqual(self.reviewers(), {})
                self.assertEqual(self.events("end")[-1]["name"], "code1")
                self.assertNotEqual(self.allocate()["thread_id"], "existing-thread")

    def test_end_lease_retires_old_hash_without_reset(self) -> None:
        self.seed({"code1": self.idle("unit", prime_hash="old")})
        result = self.run_pool("end-lease", "--unit", "unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("code1", result.stderr)
        self.assertEqual(self.reviewers(), {})
        self.assertEqual(self.events("reset"), [])
        self.assertEqual(self.events("end")[0]["name"], "code1")

    def test_end_lease_reserves_resetting_reviewer_without_blocking_allocation(self) -> None:
        self.seed({"code1": self.idle("unit")})
        gate = self.hold("reset:code1")
        ending = self.start_pool("end-lease", "--unit", "unit")
        _ = self.await_event("reset", "code1", ending)
        self.assertEqual(self.reviewer("code1")["busy_pid"], ending.pid)
        second = self.allocate()
        self.assertEqual(second["reviewer"], "code2")
        self.assertIsNone(ending.poll())
        gate.touch()
        code, _output, errors = self.finish(ending)
        self.assertEqual(code, 0, errors)
        self.assertIsNone(self.reviewer("code1")["lease"])

    def test_stop_clears_templates_and_reviewers_preserves_split_and_advances_generation(self) -> None:
        self.seed({}, split="all")
        original = self.allocate()
        self.assertEqual(original["reviewer"], "code1")
        _ = self.succeed("stop")
        state = self.state()
        self.assertEqual(state["templates"], {})
        self.assertEqual(state["reviewers"], {})
        self.assertEqual(state["split"], "all")
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["next_index"], {"code": 1, "ui": 1})
        self.assertEqual(len(self.events("stop")), 1)
        self.assertEqual(self.succeed("arm", "--unit", "unit", "--phase", "phase1"), "own")
        _ = self.prime()
        self.assertTrue(json_object(self.state()["templates"])["code"])
        replacement = self.allocate()
        self.assertEqual(replacement["reviewer"], "code1")
        self.assertNotEqual(replacement["mesh_dir"], original["mesh_dir"])
        self.assertEqual(self.event_mesh_dir(self.events("stop")[0]), Path(str(original["mesh_dir"])))
        self.assert_fork_names_are_unique_per_generation()

    def test_stop_during_fork_drops_late_thread_instead_of_repopulating_pool(self) -> None:
        self.seed({})
        gate = self.hold("fork:code1")
        allocating = self.start_pool(*self.allocation_args())
        _ = self.await_event("fork", "code1", allocating)
        _ = self.succeed("stop")
        gate.touch()
        code, output, errors = self.finish(allocating)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.state()["templates"], {})
        self.assertEqual(self.reviewers(), {})

    def test_stop_during_prime_drops_late_template(self) -> None:
        digest = hashlib.sha256(self.instructions.read_bytes()).hexdigest()
        gate = self.hold(f"prime:code-template-{digest[:8]}")
        priming = self.start_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        _ = self.await_event("prime", f"code-template-{digest[:8]}", priming)
        _ = self.succeed("stop")
        gate.touch()
        code, output, errors = self.finish(priming)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.state()["templates"], {})

    def test_prime_during_in_flight_stop_cannot_publish_an_ended_template(self) -> None:
        self.seed({})
        gate = self.hold("stop")
        stopping = self.start_pool("stop")
        _ = self.await_event("stop", "", stopping)
        priming = self.run_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        self.assertEqual(priming.returncode, 1, priming.stderr)
        self.assertEqual(priming.stdout, "")
        self.assertIn("stop", priming.stderr.lower())
        self.assertEqual(self.events("prime"), [])
        self.assertIsNone(stopping.poll(), "prime must refuse while stop is still held")
        gate.touch()
        code, _output, errors = self.finish(stopping)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.state()["templates"], {})
        self.assertEqual(self.state()["generation"], 1)
        _ = self.prime()
        self.assertTrue(json_object(self.state()["templates"])["code"])

    def test_late_fork_cannot_replace_reviewer_created_after_stop(self) -> None:
        self.seed({})
        gate = self.hold("fork:code1")
        allocating = self.start_pool(*self.allocation_args())
        first_event = self.await_event("fork", "code1", allocating)
        first_mesh_dir = self.event_mesh_dir(first_event)
        self.assertEqual(first_mesh_dir, self.pool / "mesh" / "0")
        _ = self.succeed("stop")
        self.assertEqual(self.event_mesh_dir(self.events("stop")[0]), first_mesh_dir)
        self.control({"fork": {"thread_id": "replacement-thread"}})
        _ = self.prime()
        replacement = self.allocate()
        self.assertEqual(replacement["thread_id"], "replacement-thread")
        replacement_mesh_dir = Path(str(replacement["mesh_dir"]))
        self.assertNotEqual(replacement_mesh_dir, first_mesh_dir)
        self.assertEqual(replacement_mesh_dir, self.pool / "mesh" / "1")
        self.assertEqual(self.event_mesh_dir(self.events("prime")[0]), replacement_mesh_dir)
        replacement_event = next(event for event in self.events("fork")
                                 if event["pid"] != first_event["pid"])
        self.assertEqual(self.event_mesh_dir(replacement_event), replacement_mesh_dir)
        before = self.state()
        gate.touch()
        code, output, errors = self.finish(allocating)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.state(), before)
        replacement_roster = parse_object((replacement_mesh_dir / "mesh_roster.json").read_text(encoding="utf-8"))
        self.assertEqual(json_object(replacement_roster["code1"])["thread_id"], "replacement-thread")
        obsolete_roster = parse_object((first_mesh_dir / "mesh_roster.json").read_text(encoding="utf-8"))
        self.assertEqual(json_object(obsolete_roster["code1"])["thread_id"], "thread-code1")

    def test_unfinished_stop_names_recovery_and_a_second_stop_restores_priming(self) -> None:
        self.write_state({"split": "hash", "generation": 1, "stopping": True,
                          "templates": {}, "reviewers": {}})
        result = self.run_pool(*self.allocation_args())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("stop again", result.stderr.lower())
        _ = self.succeed("stop")
        self.assertFalse(self.state().get("stopping", False))
        self.assertEqual(self.state()["generation"], 1)
        self.assertEqual(self.event_mesh_dir(self.events("stop")[0]), self.pool / "mesh" / "0")
        _ = self.prime()
        self.assertTrue(json_object(self.state()["templates"])["code"])

    def test_failed_stop_keeps_recovery_marker_and_retries_the_same_generation(self) -> None:
        self.seed({})
        self.control({"stop": {"exit": 1}})
        failed = self.run_pool("stop")
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(failed.stdout, "")
        self.assertIn("scripted stop failure", failed.stderr)
        self.assertTrue(self.state().get("stopping", False))
        self.assertEqual(self.state()["generation"], 1)
        first_event = self.events("stop")[0]
        self.assertEqual(self.event_mesh_dir(first_event), self.pool / "mesh" / "0")

        allocating = self.run_pool(*self.allocation_args())
        priming = self.run_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        for refused in (allocating, priming):
            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertEqual(refused.stdout, "")
            self.assertIn("stop again", refused.stderr.lower())
        self.assertEqual(self.events("fork"), [])
        self.assertEqual(self.events("prime"), [])

        self.control({})
        _ = self.succeed("stop")
        self.assertFalse(self.state().get("stopping", False))
        self.assertEqual(self.state()["generation"], 1)
        self.assertEqual([self.event_mesh_dir(event) for event in self.events("stop")],
                         [self.pool / "mesh" / "0", self.pool / "mesh" / "0"])
        _ = self.prime()
        self.assertEqual(self.event_mesh_dir(self.events("prime")[0]), self.pool / "mesh" / "1")

    def test_stop_during_end_lease_reset_drops_late_publication(self) -> None:
        self.seed({"code1": self.idle("unit")})
        gate = self.hold("reset:code1")
        ending = self.start_pool("end-lease", "--unit", "unit")
        _ = self.await_event("reset", "code1", ending)
        _ = self.succeed("stop")
        gate.touch()
        code, _output, errors = self.finish(ending)
        self.assertEqual(code, 1, errors)
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.reviewers(), {})

    def test_stop_during_fresh_reset_drops_late_allocation(self) -> None:
        self.seed({"code1": self.idle("unit")})
        gate = self.hold("reset:code1")
        allocating = self.start_pool(*self.allocation_args(), "--fresh")
        _ = self.await_event("reset", "code1", allocating)
        _ = self.succeed("stop")
        gate.touch()
        code, output, errors = self.finish(allocating)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.reviewers(), {})

    def test_status_names_reviewers_types_leases_ownership_and_review_counts(self) -> None:
        self.seed({"code1": self.idle("unit"),
                   "code2": {**self.idle(), "busy_pid": os.getpid()}})
        lines = self.succeed("status").splitlines()
        self.assertEqual(len(lines), 2)
        leased = next(line for line in lines if "code1" in line)
        busy = next(line for line in lines if "code2" in line)
        for text in ("code", "unit", "phase1", "idle", "5"):
            self.assertIn(text, leased)
        self.assertIn("busy", busy)

    def test_health_ends_only_stale_leases_and_reports_them(self) -> None:
        self.seed({"code1": {**self.idle("old-unit"), "last_allocated": time.time() - 13 * 3600},
                   "code2": self.idle("new-unit")})
        output = self.succeed("health")
        self.assertIn("code1", output)
        self.assertIsNone(self.reviewer("code1")["lease"])
        self.assertIsNotNone(self.reviewer("code2")["lease"])
        self.assertEqual([event["name"] for event in self.events("reset")], ["code1"])

    def test_health_stale_hours_override_changes_expiry(self) -> None:
        self.seed({"code1": {**self.idle("unit"), "last_allocated": time.time() - 2 * 3600}})
        _ = self.succeed("health", "--stale-hours", "1")
        self.assertIsNone(self.reviewer("code1")["lease"])


if __name__ == "__main__":
    _ = unittest.main()
