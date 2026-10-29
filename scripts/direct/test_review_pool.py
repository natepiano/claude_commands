#!/usr/bin/env python3
"""Pool CLI contracts, using real processes and a controlled mesh subprocess."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
from typing import cast, override


from scripts.direct import review_pool
from scripts.direct.wait_for_test_state import wait_until

SCRIPT = Path(__file__).with_name("review_pool.py")

# A gate controls completion, not a fixed delay. Each invocation records
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
for verb in ("prime", "fork", "reset", "end", "stop", "set-instructions"):
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
    elif verb == "set-instructions":
        command.add_argument("--to", required=True)
        command.add_argument("--instructions-file", required=True)
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
if args.verb == "set-instructions":
    event["instructions"] = Path(args.instructions_file).read_text()
temporary = events / (str(os.getpid()) + ".tmp")
temporary.write_text(json.dumps(event))
temporary.rename(events / (str(os.getpid()) + "-" + str(time.time_ns()) + ".json"))
gate = setting.get("gate", "")
if gate:
    sys.path.insert(0, os.environ["POOL_TEST_HELPER_DIR"])
    from wait_for_test_state import wait_for_file
    wait_for_file(Path(gate))
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
if args.verb == "set-instructions":
    session.mkdir(parents=True, exist_ok=True)
    with (session / "roster.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        roster_path = session / "mesh_roster.json"
        roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
        roster.setdefault(name, {})["instructions_file"] = args.instructions_file
        roster_path.write_text(json.dumps(roster))
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
        self.enterContext(patch.dict(os.environ, {"UNIT_SYNC_WRITES": "0"}))
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
                _ = process.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                process.kill()
                _ = process.communicate(timeout=300)
        self.temporary.cleanup()

    def test_pool_write_syncs_by_default_and_flushes_without_sync_for_disposable_state(self) -> None:
        production = review_pool.ProductionPool(self.pool)

        def flushed(descriptor: int) -> None:
            self.assertGreaterEqual(descriptor, 0)
            temporary, = self.pool.glob(".pool-*")
            self.assertTrue(temporary.read_text())

        for durable in (True, False):
            with self.subTest(durable=durable), patch.dict(os.environ), patch.object(os, "fsync", side_effect=flushed) as sync:
                if durable:
                    _ = os.environ.pop("UNIT_SYNC_WRITES", None)
                else:
                    os.environ["UNIT_SYNC_WRITES"] = "0"
                with production.locked() as pool:
                    pool.generation += 1
                    generation = pool.generation
                self.assertEqual(sync.call_count, int(durable))
                self.assertEqual(json.loads((self.pool / "pool.json").read_text())["generation"], generation)
                self.assertEqual(list(self.pool.glob(".pool-*")), [])

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "REVIEW_POOL_STATE": str(self.root / "state"),
            "REVIEW_POOL_MESH": str(self.stub),
            "POOL_STUB_CONTROL": str(self.control_file),
            "POOL_STUB_EVENTS": str(self.events_dir),
            "POOL_TEST_HELPER_DIR": str(SCRIPT.parent),
            # No base prompt unless a test writes one: an allocate then keeps the template a test primed.
            "REVIEW_POOL_CONFIG": str(self.root / "config"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
        })
        return environment

    def command(self, *arguments: str) -> list[str]:
        return [sys.executable, str(SCRIPT), arguments[0], "--production", "production", *arguments[1:]]

    def run_pool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments), env=self.environment(), capture_output=True,
            text=True, check=False, timeout=300,
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
        output, errors = process.communicate(timeout=300)
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
        def ready() -> bool:
            if any(event["name"] == name for event in self.events(verb)):
                return True
            if process.poll() is not None:
                self.fail(f"process exited before {verb}:{name}: {self.finish(process)}")
            return False

        wait_until(ready, self.events_dir, processes=(process,))
        return next(event for event in self.events(verb) if event["name"] == name)

    def state(self) -> dict[str, object]:
        return parse_object((self.pool / "pool.json").read_text(encoding="utf-8"))

    def reviewers(self) -> dict[str, object]:
        return json_object(self.state()["reviewers"])

    def reviewer(self, name: str) -> dict[str, object]:
        return json_object(self.reviewers()[name])

    def write_state(self, state: dict[str, object]) -> None:
        self.pool.mkdir(parents=True, exist_ok=True)
        _ = (self.pool / "pool.json").write_text(json.dumps(state), encoding="utf-8")

    def seed(self, entries: dict[str, object]) -> None:
        self.write_state({
            "generation": 0,
            "templates": {"code": {"name": "code-template-current", "thread_id": "template-thread",
                                    "prime_hash": "current"}},
            "reviewers": entries,
        })

    def idle(self, unit: str = "", *, prime_hash: str = "current") -> dict[str, object]:
        return {
            "type": "code", "thread_id": "existing-thread", "prime_hash": prime_hash,
            "lease": {"unit": unit, "ordinal": 3} if unit else None,
            "busy_pid": None, "reviews": 5, "last_allocated": time.time(),
        }

    def fork_sources(self) -> list[str]:
        sources: list[str] = []
        for event in self.events("fork"):
            argv = cast("list[str]", event["argv"])
            sources.append(argv[argv.index("--from") + 1])
        return sources

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

    def test_new_pool_uses_default_generation_and_content_addressed_prime(self) -> None:
        expected = hashlib.sha256(self.instructions.read_bytes()).hexdigest()
        self.assertEqual(self.prime(), expected)
        state = self.state()
        self.assertNotIn("split", state)
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

    def test_changed_prime_gives_old_and_new_reviewers_the_new_hash(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        self.release(str(old["reviewer"]))
        _ = self.instructions.write_text("A changed standing contract.\n", encoding="utf-8")
        new_hash = self.prime()
        self.assertNotEqual(new_hash, old_hash)
        self.assertFalse((self.pool / "prime" / f"{old_hash}.md").exists())
        new = self.allocate(unit="another-unit")
        self.assertNotEqual(new["reviewer"], old["reviewer"])
        self.assertEqual(new["prime_hash"], new_hash)
        self.assertEqual(self.reviewer(str(old["reviewer"]))["prime_hash"], new_hash)

    def test_changed_prime_repoints_an_idle_reviewer_and_keeps_its_thread(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        old_name = str(old["reviewer"])
        self.release(old_name)
        before = self.reviewer(old_name)
        old_file = self.pool / "prime" / f"{old_hash}.md"

        _ = self.instructions.write_text("Replacement contract.\n", encoding="utf-8")
        new_hash = self.prime()

        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(self.reviewer(old_name), {**before, "prime_hash": new_hash})
        repointed = self.events("set-instructions")
        self.assertEqual([event["name"] for event in repointed], [old_name])
        self.assertEqual(repointed[0]["instructions"], "Replacement contract.\n")
        self.assertEqual(self.events("end"), [])
        self.assertFalse(old_file.exists())
        new_file = self.pool / "prime" / f"{new_hash}.md"
        roster = parse_object((Path(str(old["mesh_dir"])) / "mesh_roster.json").read_text(encoding="utf-8"))
        self.assertEqual(json_object(roster[old_name])["instructions_file"], str(new_file))
        again = self.allocate()
        self.assertEqual((again["reviewer"], again["thread_id"], again["prime_hash"]),
                         (old_name, old["thread_id"], new_hash))
        self.assertEqual(len(self.events("set-instructions")), 1)

    def test_changed_prime_leaves_a_busy_reviewer_until_its_next_allocate(self) -> None:
        old_hash = self.prime()
        old = self.allocate()
        old_name = str(old["reviewer"])
        before = self.reviewer(old_name)
        old_file = self.pool / "prime" / f"{old_hash}.md"
        old_content = old_file.read_bytes()

        _ = self.instructions.write_text("Replacement contract.\n", encoding="utf-8")
        new_hash = self.prime()

        self.assertEqual(self.reviewer(old_name), before)
        self.assertEqual(self.events("set-instructions"), [])
        self.assertEqual(old_file.read_bytes(), old_content)
        self.release(old_name)
        again = self.allocate()
        self.assertEqual((again["reviewer"], again["prime_hash"]), (old_name, new_hash))
        self.assertEqual([event["name"] for event in self.events("set-instructions")], [old_name])
        self.assertFalse(old_file.exists())

    def test_a_refused_repoint_keeps_the_old_prompt_and_reports_the_hash_follow_sends(self) -> None:
        old_hash = self.prime()
        old_name = str(self.allocate()["reviewer"])
        self.release(old_name)
        self.control({"set-instructions": {"exit": 2}})
        _ = self.instructions.write_text("Replacement contract.\n", encoding="utf-8")
        result = self.run_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{old_name} keeps its standing prompt", result.stderr)
        self.assertEqual(self.reviewer(old_name)["prime_hash"], old_hash)
        self.assertIsNone(self.reviewer(old_name)["busy_pid"])
        self.assertTrue((self.pool / "prime" / f"{old_hash}.md").is_file())
        self.assertEqual(self.allocate()["prime_hash"], old_hash)

    def base_prompt(self, reviewer_type: str, text: str) -> str:
        """Write the type's base prompt; returns the hash of the standing prompt it gives."""
        path = self.root / "config" / f"{reviewer_type}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return hashlib.sha256((text.rstrip() + "\n").encode()).hexdigest()

    def test_the_first_allocate_primes_the_template_from_the_base_prompt(self) -> None:
        expected = self.base_prompt("code", "Review the code.\n\n")
        allocation = self.allocate()
        self.assertEqual(allocation["prime_hash"], expected)
        prime, = self.events("prime")
        self.assertEqual(prime["instructions"], "Review the code.\n")
        self.assertEqual(self.state()["templates"], {"code": {
            "name": f"code-template-{expected[:8]}", "thread_id": f"thread-code-template-{expected[:8]}",
            "prime_hash": expected}})

    def test_code_standing_instructions_use_the_review_directory(self) -> None:
        config = self.root / "config"
        (config / "review").mkdir(parents=True)
        (config / "review_pool").mkdir()
        _ = (config / "review" / "code.md").write_text("Keep the unit's review context.\n", encoding="utf-8")
        _ = (config / "review_pool" / "code.md").write_text("Old prompt must not be read.\n", encoding="utf-8")
        with patch.dict(os.environ, {"REVIEW_POOL_CONFIG": ""}), \
                patch.object(review_pool, "__file__", str(self.root / "scripts/direct/review_pool.py")):
            self.assertEqual(review_pool.standing_prompt("production", "code"),
                             b"Keep the unit's review context.\n")

    def test_ui_standing_instructions_keep_the_ui_directory(self) -> None:
        config = self.root / "config"
        (config / "review_pool").mkdir(parents=True)
        (config / "review").mkdir()
        _ = (config / "review_pool" / "ui.md").write_text("Review the screens.\n", encoding="utf-8")
        _ = (config / "review" / "ui.md").write_text("Wrong prompt must not be read.\n", encoding="utf-8")
        guide = self.root / "ux"
        guide.mkdir()
        doc = self.root / "production.md"
        _ = doc.write_text(f"- **UX guide:** `{guide}`\n", encoding="utf-8")
        with patch.dict(os.environ, {"REVIEW_POOL_CONFIG": ""}), \
                patch.object(review_pool, "__file__", str(self.root / "scripts/direct/review_pool.py")), \
                patch.object(review_pool, "timer_docs", return_value={"production": doc}):
            self.assertEqual(review_pool.standing_prompt("production", "ui"),
                             f"Review the screens.\n\n## Rules\n\nUX guide: {guide.resolve()}\n".encode())

    def test_a_changed_base_prompt_primes_again_and_keeps_the_unit_s_reviewer(self) -> None:
        _ = self.base_prompt("code", "Review the code.\n")
        first = self.allocate()
        self.release(str(first["reviewer"]))
        _ = self.allocate()
        self.release(str(first["reviewer"]))
        self.assertEqual(len(self.events("prime")), 1)
        changed = self.base_prompt("code", "Review the code and its tests.\n")
        again = self.allocate()
        self.assertEqual((again["reviewer"], again["thread_id"], again["prime_hash"]),
                         (first["reviewer"], first["thread_id"], changed))
        self.assertEqual(len(self.events("prime")), 2)

    def test_with_no_base_prompt_and_no_template_allocate_names_the_cause(self) -> None:
        result = self.run_pool(*self.allocation_args())
        self.assertEqual(result.returncode, 1)
        self.assertIn("review_pool: no primed code template: no code base prompt", result.stderr)
        self.assertEqual(self.events(), [])

    def test_a_ui_prompt_carries_the_production_s_ux_rules(self) -> None:
        repository = self.root / "repository"
        guide = repository / "ux"
        (repository / "docs/hana").mkdir(parents=True)
        guide.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(repository)], check=True)
        _ = (guide / "spacing.md").write_text("---\ntest: gaps are even\n---\nEven gaps.\n", encoding="utf-8")
        _ = (guide / "README.md").write_text("---\ntitle: guide\n---\nNot a rule.\n", encoding="utf-8")
        _ = (repository / "docs/design-decisions.md").write_text("One button per action.\n", encoding="utf-8")
        _ = (repository / "docs/hana/targets.md").write_text("Phone first.\n", encoding="utf-8")
        doc = repository / "docs/production-production.md"
        _ = doc.write_text(f"# Production\n\n- **UX guide:** `{guide}`\n", encoding="utf-8")
        instance = self.root / "notifier/showrunner-production"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:sid-production\nCHECK=zsh /x/production_check.sh {doc}\n", encoding="utf-8")
        _ = self.base_prompt("ui", "Review the screens.\n")
        _ = self.allocate(reviewer_type="ui", lens="ux")
        prime, = self.events("prime")
        self.assertEqual(prime["instructions"], "\n\n".join([
            "Review the screens.", "## Rules", f"UX guide: {guide.resolve()}",
            "### spacing.md\n\n---\ntest: gaps are even\n---\nEven gaps.",
            "## Design decisions\n\nOne button per action.", "## Targets\n\nPhone first."]) + "\n")

    def test_a_ui_allocate_without_a_ux_guide_names_the_cause(self) -> None:
        _ = self.base_prompt("ui", "Review the screens.\n")
        result = self.run_pool(*self.allocation_args(reviewer_type="ui", lens="ux"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("review_pool: no primed ui template: no production doc for production", result.stderr)

    def test_closure_after_a_memory_change_resumes_the_same_reviewer_with_the_new_prompt(self) -> None:
        old_hash = self.prime()
        broad = self.allocate()
        self.release(str(broad["reviewer"]))
        _ = self.instructions.write_text("Standing contract with new repository memory.\n", encoding="utf-8")
        new_hash = self.prime()
        self.assertNotEqual(new_hash, old_hash)
        closure = self.allocate(review_pass="2", lens="")
        self.assertEqual(closure["reviewer"], broad["reviewer"])
        self.assertEqual((closure["thread_id"], closure["prime_hash"]), (broad["thread_id"], new_hash))
        self.assertEqual(closure["lease_ordinal"], 2)
        self.assertEqual(len(self.events("fork")), 1)

    def test_prime_failure_does_not_publish_a_template(self) -> None:
        self.control({"prime": {"exit": 1}})
        result = self.run_pool("prime", "--type", "code", "--instructions-file", str(self.instructions))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failure", result.stderr)
        self.assertEqual(self.state()["templates"], {})

    def test_a_pool_written_with_a_split_loads_and_drops_it_on_the_next_write(self) -> None:
        self.write_state({"split": "all", "generation": 0, "templates": {}, "reviewers": {}})
        _ = self.prime()
        self.assertNotIn("split", self.state())

    def test_arm_and_split_are_gone(self) -> None:
        self.seed({})
        for arguments in (("arm", "--unit", "unit", "--phase", "phase1"), ("split", "--set", "all")):
            with self.subTest(verb=arguments[0]):
                result = self.run_pool(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("invalid choice", result.stderr)

    def test_allocation_prefers_the_units_reviewer_over_unleased_reviewer(self) -> None:
        self.seed({"code1": self.idle(), "code2": self.idle("unit")})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code2")
        self.assertEqual(result["lease_ordinal"], 4)
        self.assertEqual(self.reviewer("code2")["busy_pid"], os.getpid())
        self.assertEqual(self.events(), [])

    def test_later_phases_passes_and_lenses_reuse_the_units_one_reviewer(self) -> None:
        self.seed({"code1": self.idle("unit"), "code2": self.idle()})
        for phase, review_pass, lens in (("phase1", "1", "contract"), ("phase1", "1", "adversary"),
                                         ("phase2", "1", "contract"), ("phase3", "2", "")):
            with self.subTest(phase=phase, review_pass=review_pass, lens=lens):
                self.assertEqual(self.allocate(phase=phase, review_pass=review_pass, lens=lens)["reviewer"], "code1")
                self.release("code1")
        self.assertEqual(self.reviewer("code1")["lease"], {"unit": "unit", "ordinal": 7})
        self.assertIsNone(self.reviewer("code2")["lease"])
        self.assertEqual(self.events(), [])

    def test_concurrent_review_forks_the_units_newest_reviewer_not_the_template(self) -> None:
        older = {**self.idle("unit"), "busy_pid": os.getpid(), "last_allocated": time.time() - 60}
        newest = {**self.idle("unit", prime_hash="old"), "busy_pid": os.getpid()}
        self.seed({"code1": older, "code2": newest, "code3": self.idle(), "code4": self.idle("other-unit")})
        result = self.allocate()
        self.assertEqual(result["reviewer"], "code5")
        self.assertEqual(result["lease_ordinal"], 4)
        self.assertEqual(result["prime_hash"], "old")
        self.assertEqual(self.fork_sources(), ["code2"])
        self.assertEqual(self.reviewer("code5")["lease"], {"unit": "unit", "ordinal": 4})
        self.release("code5")
        self.assertEqual(self.allocate(phase="phase2")["reviewer"], "code5")
        self.assertEqual(len(self.events("fork")), 1)

    def test_failed_fork_of_the_units_reviewer_falls_back_to_the_template(self) -> None:
        self.seed({"code1": {**self.idle("unit"), "busy_pid": os.getpid()}})
        self.control({"fork:code2": {"exit": 1, "failures": 1}})
        result = self.run_pool(*self.allocation_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parse_object(result.stdout)["reviewer"], "code2")
        self.assertEqual(self.fork_sources(), ["code1", "code-template-current"])
        self.assertIn("fork of code1 failed", result.stderr)
        self.assertEqual(self.reviewer("code2")["thread_id"], "thread-code2")
        # The template fork has none of the unit's context, so its lease starts again at the first review.
        self.assertEqual(parse_object(result.stdout)["lease_ordinal"], 1)

    def test_unit_reviewer_with_an_old_hash_is_reused_not_forked(self) -> None:
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
        self.assertEqual(self.reviewer("code1")["lease"], {"unit": "unit", "ordinal": 1})

    def test_first_review_skips_busy_wrong_type_other_unit_and_old_unleased_reviewers(self) -> None:
        busy = {**self.idle(), "busy_pid": os.getpid()}
        ui = {**self.idle("unit"), "type": "ui"}
        self.seed({"code1": busy, "ui1": ui, "code2": self.idle("other-unit"),
                   "code3": self.idle(prime_hash="old"), "code4": self.idle()})
        self.assertEqual(self.allocate()["reviewer"], "code4")
        self.assertEqual(self.events("fork"), [])

    def test_dead_owner_is_idle_and_last_allocated_is_refreshed(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=300)
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
        self.assertEqual(after["thread_id"], before["thread_id"])

    def test_release_updates_replaced_thread_only_for_matching_owner(self) -> None:
        self.seed({"code1": {**self.idle("unit"), "busy_pid": os.getpid()}})
        before = self.reviewer("code1")
        _ = self.succeed("release", "--reviewer", "code1", "--pid", str(os.getpid() + 1),
                         "--thread-id", "unowned-thread")
        self.assertEqual(self.reviewer("code1"), before)
        _ = self.succeed("release", "--reviewer", "code1", "--pid", str(os.getpid()),
                         "--thread-id", "replacement-thread")
        after = self.reviewer("code1")
        self.assertEqual(after["thread_id"], "replacement-thread")
        self.assertIsNone(after["busy_pid"])
        self.assertEqual(after["reviews"], 6)
        # A new thread holds none of the unit's reviews, so the next review counts as the first.
        self.assertEqual(after["lease"], {"unit": "unit", "ordinal": 0})
        again = self.allocate()
        self.assertEqual((again["reviewer"], again["thread_id"], again["lease_ordinal"]),
                         ("code1", "replacement-thread", 1))

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

    def test_fresh_is_accepted_and_resets_nothing(self) -> None:
        self.seed({"code1": self.idle("unit")})
        result = parse_object(self.succeed(*self.allocation_args(), "--fresh"))
        self.assertEqual((result["reviewer"], result["thread_id"]), ("code1", "existing-thread"))
        self.assertEqual(self.events(), [])

    def test_phase_end_lease_leaves_the_units_reviewer(self) -> None:
        self.seed({"code1": self.idle("unit")})
        before = self.reviewer("code1")
        _ = self.succeed("end-lease", "--unit", "unit", "--phase", "phase1")
        self.assertEqual(self.reviewer("code1"), before)
        self.assertEqual(self.events(), [])

    def test_end_lease_ends_nothing_and_health_is_gone(self) -> None:
        self.seed({"code1": {**self.idle("unit"), "last_allocated": time.time() - 13 * 3600}})
        self.assertEqual(self.run_pool("health").returncode, 2)
        before = self.reviewers()
        for arguments in (("end-lease", "--unit", "unit"), ("end-lease", "--unit", "unit", "--phase", "phase1")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.succeed(*arguments), "")
                self.assertEqual(self.reviewers(), before)
        self.assertEqual(self.events(), [])

    def test_retire_ends_every_reviewer_of_the_unit_and_no_other(self) -> None:
        self.seed({"code1": self.idle("unit"), "ui1": {**self.idle("unit"), "type": "ui"},
                   "code2": self.idle("other"), "code3": self.idle("unit-unit")})
        kept = {name: self.reviewer(name) for name in ("code2", "code3")}
        self.assertEqual(sorted(self.succeed("retire", "--unit", "unit").splitlines()), ["code1", "ui1"])
        self.assertEqual(self.reviewers(), kept)
        self.assertEqual(sorted(str(event["name"]) for event in self.events("end")), ["code1", "ui1"])

    def units_table(self, *units: str) -> None:
        """Register the production's doc with a Units table of these ids."""
        doc = self.root / "production-production.md"
        _ = doc.write_text("\n".join(["## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                       "| --- | --- | --- | --- | --- | --- |",
                                       *(f"| {unit} | p.md | /w/{unit} | {unit} | — | — |" for unit in units)]) + "\n",
                           encoding="utf-8")
        instance = self.root / "notifier/showrunner-production"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:sid-production\nCHECK=zsh /x/production_check.sh {doc}\n", encoding="utf-8")

    def test_retire_of_one_unit_leaves_a_unit_whose_name_extends_it(self) -> None:
        self.units_table("handler-enh", "handler-enh-unit")
        self.seed({"code7": self.idle("handler-enh"), "code3": self.idle("handler-enh-unit")})
        kept = self.reviewer("code3")
        self.assertEqual(self.succeed("retire", "--unit", "handler-enh").splitlines(), ["code7"])
        self.assertEqual(self.reviewers(), {"code3": kept})

    def test_retire_of_a_leased_short_name_leaves_the_units_row_it_would_resolve_to(self) -> None:
        self.units_table("handler-enh-unit")
        self.seed({"code7": self.idle("handler-enh"), "code3": self.idle("handler-enh-unit")})
        kept = self.reviewer("code3")
        self.assertEqual(self.succeed("retire", "--unit", "handler-enh").splitlines(), ["code7"])
        self.assertEqual(self.reviewers(), {"code3": kept})

    def test_a_short_name_resolves_to_its_units_row_for_retire_and_allocate(self) -> None:
        self.units_table("trunk-unit")
        _ = self.prime()
        allocation = self.allocate(unit="trunk")
        self.assertEqual(self.reviewer(str(allocation["reviewer"]))["lease"], {"unit": "trunk-unit", "ordinal": 1})
        self.release(str(allocation["reviewer"]))
        self.assertEqual(self.succeed("retire", "--unit", "trunk").splitlines(), [allocation["reviewer"]])

    def test_retire_removes_a_reservation_whose_owner_died(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=300)
        self.seed({"code1": {**self.idle("unit"), "thread_id": "", "busy_pid": child.pid}})
        self.assertEqual(self.succeed("retire", "--unit", "unit"), "code1")
        self.assertNotIn("code1", self.reviewers())
        self.assertEqual(self.events(), [])

    def test_retire_leaves_a_reviewing_reviewer_and_names_it(self) -> None:
        for record in ({**self.idle("unit"), "busy_pid": os.getpid()},
                       {**self.idle("unit"), "thread_id": "", "busy_pid": os.getpid()}):
            with self.subTest(thread_id=record["thread_id"]):
                self.seed({"code1": record})
                result = self.run_pool("retire", "--unit", "unit")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("code1 is reviewing", result.stderr)
                self.assertEqual(self.reviewer("code1"), record)
                self.assertEqual(self.events(), [])

    def test_removed_dead_reservation_does_not_free_its_issued_reviewer_name(self) -> None:
        self.seed({})
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        _ = child.wait(timeout=300)
        gate = self.hold("fork:code1")
        arguments = self.allocation_args()
        arguments[arguments.index("--pid") + 1] = str(child.pid)
        pending = self.start_pool(*arguments)
        first_event = self.await_event("fork", "code1", pending)
        self.assertEqual(self.reviewer("code1")["thread_id"], "")

        ended = self.run_pool("retire", "--unit", "unit")
        self.assertEqual(ended.returncode, 0, ended.stderr)
        self.assertEqual(ended.stdout.strip(), "code1")
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

    def test_stop_clears_templates_and_reviewers_and_advances_generation(self) -> None:
        self.seed({})
        original = self.allocate()
        self.assertEqual(original["reviewer"], "code1")
        _ = self.succeed("stop")
        state = self.state()
        self.assertEqual(state["templates"], {})
        self.assertEqual(state["reviewers"], {})
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["next_index"], {"code": 1, "ui": 1})
        self.assertEqual(len(self.events("stop")), 1)
        _ = self.prime()
        self.assertTrue(json_object(self.state()["templates"])["code"])
        replacement = self.allocate()
        self.assertEqual(replacement["reviewer"], "code1")
        self.assertNotEqual(replacement["mesh_dir"], original["mesh_dir"])
        self.assertEqual(self.event_mesh_dir(self.events("stop")[0]), Path(str(original["mesh_dir"])))
        self.assert_fork_names_are_unique_per_generation()

    def test_stop_clears_ui_adoption_and_prime_keeps_the_new_pool_in_shadow(self) -> None:
        _ = self.prime("ui")
        adoption = self.pool / "ui_adopted"
        adoption.touch()
        result = self.run_pool("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(adoption.exists())
        self.assertIn("review_pool: ui adoption cleared", result.stderr)
        _ = self.prime("ui")
        self.assertFalse(adoption.exists())
        self.assertTrue(json_object(self.state()["templates"])["ui"])

    def test_failed_stop_still_clears_ui_adoption(self) -> None:
        self.seed({})
        adoption = self.pool / "ui_adopted"
        adoption.touch()
        self.control({"stop": {"exit": 1}})
        result = self.run_pool("stop")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(adoption.exists())
        self.assertIn("review_pool: ui adoption cleared", result.stderr)
        self.assertTrue(self.state().get("stopping", False))

    def test_stop_without_ui_adoption_does_not_report_an_adoption_change(self) -> None:
        self.seed({})
        result = self.run_pool("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ui adoption cleared", result.stderr)
        self.assertFalse((self.pool / "ui_adopted").exists())

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

    def test_stop_during_retire_drops_late_publication(self) -> None:
        self.seed({"code1": self.idle("unit")})
        gate = self.hold("end:code1")
        ending = self.start_pool("retire", "--unit", "unit")
        _ = self.await_event("end", "code1", ending)
        _ = self.succeed("stop")
        gate.touch()
        code, _output, errors = self.finish(ending)
        self.assertEqual(code, 1, errors)
        self.assertIn("stop", errors.lower())
        self.assertEqual(self.reviewers(), {})

    def test_status_names_reviewers_types_leases_ownership_and_review_counts(self) -> None:
        self.seed({"code1": self.idle("unit"),
                   "code2": {**self.idle(), "busy_pid": os.getpid()}})
        lines = self.succeed("status").splitlines()
        self.assertEqual(len(lines), 2)
        leased = next(line for line in lines if "code1" in line)
        busy = next(line for line in lines if "code2" in line)
        for text in ("code", "unit:3", "idle", "5"):
            self.assertIn(text, leased)
        self.assertIn("busy", busy)

    def migrate(self, running: set[str], *, apply: bool) -> list[list[str]]:
        arguments = review_pool.PoolArguments()
        arguments.production = "production"
        arguments.apply = apply
        output = io.StringIO()
        with (patch.dict(os.environ, self.environment()),
              patch.object(review_pool, "running_units", return_value=running) as units,
              contextlib.redirect_stdout(output)):
            self.assertEqual(review_pool.command_migrate(arguments), 0)
        units.assert_called_once_with("production")
        return [line.split("\t") for line in output.getvalue().splitlines()]

    def seed_tool_based_ui(self) -> None:
        """The tool-based-ui pool's layout: phase leases left on every finished phase's reviewers."""
        now = time.time()

        def phase_leased(unit: str, phase: str, age: int) -> dict[str, object]:
            return {"type": "code", "thread_id": f"thread-{unit}-{phase}-{age}", "prime_hash": "current",
                    "lease": {"unit": unit, "phase": phase, "ordinal": 1}, "busy_pid": None,
                    "served": [["1", "contract"]], "reviews": 1, "last_allocated": now - age}

        self.seed({
            "code49": phase_leased("widget-unit", "14", 900), "code71": phase_leased("widget-unit", "phase15", 500),
            "code80": phase_leased("widget-unit", "15", 100),
            "code74": phase_leased("trunk", "127", 700), "code78": phase_leased("trunk", "128", 300),
            "code63": phase_leased("gone-unit", "26", 800),
            "code81": {**self.idle(), "thread_id": "thread-spare"},
            "code82": {**phase_leased("widget-unit", "16", 50), "busy_pid": os.getpid()},
        })

    def test_migrate_prints_its_plan_and_changes_nothing_without_apply(self) -> None:
        self.seed_tool_based_ui()
        before = self.state()
        plan = self.migrate({"widget-unit", "trunk-unit"}, apply=False)
        self.assertEqual({row[1]: row[0] for row in plan}, {
            "code82": "keep", "code80": "keep", "code71": "retire", "code49": "retire",
            "code78": "keep", "code74": "retire", "code63": "retire", "code81": "retire",
        })
        self.assertEqual(self.state(), before)
        self.assertEqual(self.events(), [])

    def test_migrate_apply_keeps_one_reviewer_per_running_unit_and_ends_the_rest(self) -> None:
        self.seed_tool_based_ui()
        _ = self.migrate({"widget-unit", "trunk-unit"}, apply=True)
        self.assertEqual(sorted(self.reviewers()), ["code78", "code80", "code82"])
        self.assertEqual(sorted(str(event["name"]) for event in self.events("end")),
                         ["code49", "code63", "code71", "code74", "code81"])
        self.assertEqual(self.reviewer("code80")["lease"], {"unit": "widget-unit", "ordinal": 1})
        self.assertNotIn("served", self.reviewer("code80"))
        self.assertEqual(self.allocate(unit="trunk", phase="140")["reviewer"], "code78")
        self.assertEqual(self.events("fork"), [])

    def test_migrate_apply_leaves_a_reviewer_allocation_took_after_the_plan(self) -> None:
        plan = [review_pool.MigrationStep("code2", "existing-thread", False, "unit keeps a newer code reviewer")]
        self.seed({"code1": self.idle("unit"), "code2": {**self.idle("unit"), "busy_pid": os.getpid()}})
        with patch.object(review_pool, "migration_plan", return_value=plan):
            arguments = review_pool.PoolArguments()
            arguments.production = "production"
            arguments.apply = True
            errors = io.StringIO()
            with (patch.dict(os.environ, self.environment()),
                  patch.object(review_pool, "running_units", return_value={"unit"}),
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors)):
                self.assertEqual(review_pool.command_migrate(arguments), 0)
        self.assertIn("code2", self.reviewers())
        self.assertIn("code2 changed since the plan", errors.getvalue())
        self.assertEqual(self.events(), [])

    def test_running_units_counts_a_unit_of_unknown_state_as_running(self) -> None:
        # A stand-in module keeps herdr and the top-level showrunners module out of this suite.
        class ClaudeNotRunning:
            pass

        class LiveClaude:
            pass

        class ClaudeUnknown:
            pass

        marked = {unit: SimpleNamespace(claude=claude()) for unit, claude in (
            ("live-unit", LiveClaude), ("unknown-unit", ClaudeUnknown), ("stopped-unit", ClaudeNotRunning))}
        lookup = Mock(return_value=marked)
        stand_in = ModuleType("unit_lookup")
        stand_in.__dict__.update(ClaudeNotRunning=ClaudeNotRunning, marked_units=lookup)
        with patch.dict(sys.modules, {"unit_lookup": stand_in}):
            self.assertEqual(review_pool.running_units("production"), {"live-unit", "unknown-unit"})
        lookup.assert_called_once_with("production")

class UIStandingPromptTests(unittest.TestCase):
    def test_ui_standing_prompt_defines_report_and_measurement_without_rule_reads(self) -> None:
        prompt = (SCRIPT.parents[2] / "config" / "review_pool" / "ui.md").read_text(encoding="utf-8")
        self.assertRegex(prompt, r"Verdict:\s*pass\b")
        self.assertRegex(prompt, r"Verdict:\s*(?:pass\s*\|\s*)?defects\b")
        self.assertIn("| Shot | Rule | Defect | Fix |", prompt)
        self.assertIn("measure_text.py", prompt)
        instructions = " ".join(prompt.split())
        self.assertNotIn("Follow `~/.claude/commands/ux_eval.md`", instructions)
        self.assertNotIn("Read a rule's full text", instructions)


if __name__ == "__main__":
    _ = unittest.main()
