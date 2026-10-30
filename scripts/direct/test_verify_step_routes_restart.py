"""Real verify step routing: restart."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_stopped_local_check_is_routed_again(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        (holders / "other").unlink()
        original = (self.fixture.bin_directory / "cargo").read_text()
        stub = original.replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if arguments[:1] == ["check"]:
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (Path(os.environ["PACKAGE_ROOT"]) / "compiling-state").write_text(record["state"])
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("new hold")
    raise SystemExit(143)
''')
        _ = (self.fixture.bin_directory / "cargo").write_text(stub)
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hold stopped this step", result.stderr)
        self.assertEqual((self.fixture.root / "compiling-state").read_text(), "on_natedev")
        self.assertEqual(len(self.mac_rows()), 1)
        self.assert_removed()

    def test_recorded_stop_after_release_runs_the_local_step_again(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        (holders / "other").unlink()
        cargo = self.fixture.bin_directory / "cargo"
        self.fixture.environment["TEST_PROCESS_REPOSITORY"] = str(Path(__file__).resolve().parents[2])
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if arguments[:1] == ["check"]:
    import socket
    first = Path(os.environ["PACKAGE_ROOT"]) / "first-check"
    if not first.exists():
        first.write_text("stopped")
        proc = Path("/proc")
        sys.path.insert(0, os.environ["TEST_PROCESS_REPOSITORY"])
        from scripts.lib import process_start
        stat = process_start.read_stat(os.getpid())
        if isinstance(stat, process_start.ProcessGone):
            raise SystemExit("the test process ended before its stop record")
        start = stat.start.text
        log = Path(os.environ["BUILDLOG_DIR"]) / socket.gethostname().split(".")[0]
        log.mkdir(parents=True, exist_ok=True)
        (log / "hold-builds-test.jsonl").write_text(json.dumps(dict(
            kind="hold_build", action="stopped", call_id=os.environ["BUILDLOG_CALL_ID"],
            boot_id=(proc / "sys/kernel/random/boot_id").read_text().strip(),
            pid=os.getpid(), proc_start=start, id="stopped-check")) + "\\n")
        raise SystemExit(143)
''')
        _ = cargo.write_text(source)
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hold stopped this step", result.stderr)
        self.assertEqual(self.mac_rows(), [])
        self.assertEqual(len([event for event in self.fixture.cargo_events() if event["args"][:1] == ["check"]]), 1)
        self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
