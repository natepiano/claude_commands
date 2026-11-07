"""Read live states from real verify calls with private tools."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from typing import cast

from scripts.direct import step_record
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture, TARGETS


def read_step_row(call_id: str) -> dict[str, object]:
    """Adapt a decoded record for existing fixture observations."""
    record = step_record.read(call_id)
    if not isinstance(record, step_record.StepRecord):
        raise AssertionError(f"The live record is invalid: {record}")
    location: dict[str, object]
    match record.runs_on:
        case step_record.MacAllowed():
            location = {"kind": "mac_allowed"}
        case step_record.SshAllowed(_, mac_targets):
            location = {"kind": "ssh_allowed", "mac_targets": list(mac_targets)}
        case step_record.NatedevOnly(reason):
            location = {"kind": "natedev_only", "reason": reason}
    return dict(kind=record.kind, state=record.state.value, selected_target=record.selected_target,
                updated=record.updated, runs_on=location, eligible_targets=list(record.eligible_targets),
                pinned_target=record.selection.target if isinstance(record.selection, step_record.Pinned) else "",
                package=step_record.package_name(record.package))


OBSERVER = r'''
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, os.environ["TEST_STATE_REPOSITORY"])
from scripts.direct.test_verify_step_state import read_step_row
from scripts.direct import step_record
call_id = os.environ.get("BUILDLOG_CALL_ID", "")
if call_id:
    path = step_record.record_path(call_id)
    text = path.read_text()
    original = Path(os.environ["TEST_STATE_ORIGINAL"])
    if sys.argv[1] == "initialize_step_record":
        original.write_text(text)
    row = read_step_row(call_id)
    row.update(event=sys.argv[1], unchanged=text == original.read_text())
    with Path(os.environ["TEST_STATE_ROWS"]).open("a") as output:
        output.write(json.dumps(row) + "\n")
'''

SHELL_OBSERVER = r'''
set -T
observe_step_return() {
    case "${FUNCNAME[1]:-}" in
        initialize_step_record|step_state|run)
            "$TEST_REAL_PYTHON" "$TEST_STATE_OBSERVER" "${FUNCNAME[1]}"
            ;;
    esac
    if [[ "${FUNCNAME[1]:-}" == initialize_step_record && "${TEST_STATE_SEQUENCE:-0}" == 1 ]]; then
        for word in waiting_hold waiting_mac waiting_ssh on_mac on_ssh on_natedev local_noncompiling; do
            if [[ "$word" == on_ssh ]]; then step_state "$word" aws; else step_state "$word"; fi
            "$TEST_REAL_PYTHON" "$TEST_STATE_OBSERVER" step_state
        done
    fi
}
trap observe_step_return RETURN
'''


class VerifyStepStateTests(VerifyStepRoutesFixture):
    def observe(self) -> Path:
        root = self.fixture.root
        observer = root / "state-observer.py"
        _ = observer.write_text(OBSERVER)
        shell = root / "state-observer.sh"
        _ = shell.write_text(SHELL_OBSERVER)
        starts = root / "python-starts"
        self.fixture.write_executable(self.fixture.bin_directory / "python3", '''import json, os, sys
from pathlib import Path
with Path(os.environ["TEST_PYTHON_STARTS"]).open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(os.environ["TEST_REAL_PYTHON"], [os.environ["TEST_REAL_PYTHON"], *sys.argv[1:]])
''')
        self.fixture.environment.update({
            "BASH_ENV": str(shell), "TEST_STATE_OBSERVER": str(observer),
            "TEST_STATE_REPOSITORY": str(Path(__file__).resolve().parents[2]),
            "TEST_STATE_ORIGINAL": str(root / "original-record"),
            "TEST_STATE_ROWS": str(root / "state-rows"), "TEST_PYTHON_STARTS": str(starts),
            "TEST_REAL_PYTHON": sys.executable, "CLAUDE_PYTHON": str(self.fixture.bin_directory / "python3"),
        })
        return starts

    def verbs(self, starts: Path) -> list[str]:
        words = [cast(list[str], json.loads(line)) for line in starts.read_text().splitlines()]
        return [row[1] for row in words if len(row) > 1 and Path(row[0]).name == "step_record.py"]

    def observations(self) -> list[dict[str, object]]:
        path = self.fixture.root / "state-rows"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]

    def test_read_only_call_starts_only_start_and_write(self) -> None:
        starts = self.observe()
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.verbs(starts), ["start", "write"])

    def test_two_tree_steps_reuse_the_record_and_restore_the_call_decision(self) -> None:
        starts = self.observe()
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        reader = Path(self.fixture.environment["LINT_CONFIG_READER"])
        _ = reader.write_text('lint_config_enabled() { [[ "$1" == mend || "$1" == fmt ]]; }\n'
                              + 'lint_config_skip_notice() { :; }\n_lint_config_raw() { :; }\n')
        cargo = self.fixture.bin_directory / "cargo"
        _ = cargo.write_text(cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if "--version" not in arguments and ("fmt" in arguments or ("mend" in arguments and "--fix" in arguments)):
    import subprocess
    subprocess.run([sys.executable, os.environ["TEST_STATE_OBSERVER"], "tree"], check=True)
'''))
        result = self.run_verify("lint", "--workspace", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.observations()
        initial = rows[0]
        trees = [index for index, row in enumerate(rows) if row["event"] == "tree"]
        self.assertEqual(len(trees), 2, rows)
        for index in trees:
            self.assertEqual(cast(dict[str, object], rows[index]["runs_on"])["kind"], "natedev_only")
            restored = next(row for row in rows[index + 1:] if row["event"] == "run")
            self.assertEqual(restored["runs_on"], initial["runs_on"])
            self.assertEqual(restored["state"], "local_noncompiling")
        self.assertEqual(self.verbs(starts), ["start", "write"])
        self.assertTrue(all(row["unchanged"] for row in rows), rows)

    def test_published_states_preserve_the_call_fields_and_select_the_target(self) -> None:
        _ = self.observe()
        self.fixture.environment["TEST_STATE_SEQUENCE"] = "1"
        targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
        _ = targets.write_text(TARGETS.replace('print("mac_allowed" if allowed else "natedev_only makes natedev artifacts")',
                                               'print("ssh_allowed" if allowed else "natedev_only makes natedev artifacts")')
                              + 'if command == "reserve":\n'
                              + '    Path(sys.argv[sys.argv.index("--reservation-file") + 1]).write_text("mac\\t" + os.environ["MAC_TEST_CONFIG"] + "\\n")\n')
        result = self.run_verify("test", "sample", "--target", "mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.observations()
        initial = rows[0]
        published = [row for row in rows if row["event"] == "step_state"]
        expected = {"waiting_hold": "", "waiting_mac": "", "waiting_ssh": "", "on_mac": "mac",
                    "on_ssh": "aws", "on_natedev": "natedev", "local_noncompiling": ""}
        self.assertTrue(set(expected).issubset({cast(str, row["state"]) for row in published}), rows)
        for row in published:
            self.assertEqual(row["selected_target"], expected[cast(str, row["state"])])
            for field in ("pinned_target", "eligible_targets", "runs_on", "package"):
                self.assertEqual(row[field], initial[field], (field, row))
        self.assertTrue(all(row["unchanged"] for row in rows), rows)

    def test_initial_ssh_record_lists_only_macos_targets_as_mac_targets(self) -> None:
        _ = self.observe()
        targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
        _ = targets.write_text(TARGETS.replace(
            'print("mac_allowed" if allowed else "natedev_only makes natedev artifacts")',
            'print("ssh_allowed" if allowed else "natedev_only makes natedev artifacts")')
            + 'if command == "eligible":\n'
            + '    print("linux-box\\teligible\\t-\\t/tmp/linux.conf\\tlinux")\n'
            + 'if command == "reserve":\n'
            + '    Path(sys.argv[sys.argv.index("--reservation-file") + 1]).write_text("mac\\t" + os.environ["MAC_TEST_CONFIG"] + "\\n")\n')
        result = self.run_verify("test", "sample", "--target", "mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        initial = next(row for row in self.observations() if row["event"] == "initialize_step_record")
        self.assertEqual(initial["runs_on"], {"kind": "ssh_allowed", "mac_targets": ["mac"]})

    def test_removed_or_foreign_record_refuses_before_the_step(self) -> None:
        for damage in ("removed", "foreign", "nul"):
            with self.subTest(damage=damage):
                targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
                source = TARGETS.replace('raise SystemExit(10 if any(holders.iterdir()) else 0)', '''
    if command == "register-local":
        for path in Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"):
            if os.environ["TEST_RECORD_DAMAGE"] == "removed":
                path.unlink()
            elif os.environ["TEST_RECORD_DAMAGE"] == "nul":
                path.write_bytes(path.read_bytes() + bytes([0]))
            else:
                row = json.loads(path.read_text())
                row["pid"] = os.getpid()
                path.write_text(json.dumps(row))
    raise SystemExit(10 if any(holders.iterdir()) else 0)''')
                _ = targets.write_text(source)
                self.fixture.environment["TEST_RECORD_DAMAGE"] = damage
                result = self.run_verify("check", "sample")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("step refused", result.stderr)
                self.assertEqual(self.mac_rows(), [])
                self.assertFalse(any(row["args"][:1] == ["check"] for row in self.fixture.cargo_events()))

    def test_call_cleanup_removes_the_record_state_and_temporary_files(self) -> None:
        _ = self.observe()
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.observations())
        records = Path(self.fixture.environment["VERIFY_STEPS_DIR"])
        self.assertEqual(list(records.iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
