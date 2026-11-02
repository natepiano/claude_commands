"""Check SSH routing before reservation with private target state."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast, override
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in ("build_targets", "mac_test", "build_hold"):
    sys.path.insert(0, str(SCRIPTS / directory))

if TYPE_CHECKING:
    from . import build_targets, targets
    from ..mac_test import mac_test as mac_test, offload as offload
    from ..build_hold import build_hold
    from ..lint.memory_test_support import config_environment, write_config_fixture
else:
    import build_targets
    import targets
    import mac_test
    import offload
    import build_hold
    from scripts.lint.memory_test_support import config_environment, write_config_fixture


class SshEligibilityTests(unittest.TestCase):
    root: Path = Path()
    commands: list[list[str]] = []
    reachable: bool = True

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.commands = []
        self.reachable = True
        environment = config_environment(self.root)
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,aws\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n")
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=linux-part\n")
        _ = write_config_fixture(self.root / "aws_target.conf",
                                 "offload=on\nhost=fake-aws\nkinds=test,check,doc\nslots=2\nslots.hana=2\n")
        _ = (self.root / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **environment, "HOME": str(self.root), "MAC_TEST_CONFIG": str(self.root / "mac_test.conf"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILD_HOLD_DIR": str(self.root / "holds"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "releases"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity",
                                               return_value=offload.KnownRepository("hana", "github.com/team/hana")))
        _ = self.enterContext(mock.patch.object(mac_test, "target_claim_scope",
                                               return_value=mac_test.RepositoryScope("hana", "github.com/team/hana")))
        _ = self.enterContext(mock.patch.object(mac_test, "process_start_time", side_effect=self.process_start))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(build_hold, "owning_session", return_value=build_hold.NoOwningSession()))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected child process")))
        _ = self.enterContext(mock.patch.object(offload.ProcessController, "capture", side_effect=self.capture))
        _ = self.enterContext(mock.patch.object(offload, "probe_mac", side_effect=AssertionError("No execution probe before reservation")))

    def process_start(self, pid: int) -> str:
        return f"start-{pid}"

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-1], "true", "Routing must not evaluate a shell")
        return offload.CommandOutput(0 if self.reachable else 255, "", "")

    def answer(self, kind: str = "test", local: bool = False) -> list[list[str]]:
        output = io.StringIO()
        arguments = ["build_targets.py", "eligible", "--kind", kind, "--repo-root", str(self.root),
                     "--package", "linux-part"]
        if local:
            arguments += ["--local", "requires local files"]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(output):
            self.assertEqual(build_targets.main(), 0)
        return [line.split("\t") for line in output.getvalue().splitlines()[1:]]

    def aws(self, kind: str = "test", local: bool = False) -> list[str]:
        return next(row for row in self.answer(kind, local) if row[0] == "aws")

    def occupy(self, pid: int) -> None:
        args = mac_test.CliArguments()
        args.target, args.worktree, args.pid = "aws", str(self.root), pid
        args.what, args.remote = "check hana", "fake-aws"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, mac_test.state_paths("aws")), 0)

    def decision(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(build_targets.command_kind_eligible(
                self.root, "linux-part", "test", targets.ANY_ELIGIBLE_TARGET), 0)
        return output.getvalue().splitlines()[0]

    def test_linux_backoff_disables_the_memory_leave_route(self) -> None:
        offload.write_backoff(10**12, "aws")
        self.assertEqual(self.decision(), "natedev_only backoff")
        self.assertEqual(self.commands, [])

    def test_unreachable_linux_target_disables_the_memory_leave_route(self) -> None:
        self.reachable = False
        self.assertEqual(self.decision(), "natedev_only unreachable")
        self.assertTrue(self.commands)

    def test_disk_refusal_disables_the_memory_leave_route(self) -> None:
        with mock.patch.object(mac_test, "free_now", return_value="disk"):
            self.assertEqual(self.decision(), "natedev_only disk")
        self.assertEqual(self.commands, [])

    def test_capacity_exclusions_keep_the_memory_leave_route_available(self) -> None:
        self.occupy(71)
        self.occupy(72)
        self.assertEqual(self.decision(), "ssh_allowed")
        with mock.patch.object(mac_test, "free_now", return_value="blocked"):
            self.assertEqual(self.decision(), "ssh_allowed")
        self.assertEqual(self.commands, [])

    def test_linux_only_work_keeps_linux_capacity_without_a_mac(self) -> None:
        rows = self.answer()
        self.assertEqual([row[0] for row in rows], ["mac", "natedev", "aws"])
        self.assertEqual(rows[0][1:3], ["excluded", "linux_only"])
        self.assertEqual(rows[2][1:3], ["eligible", "-"])
        self.assertTrue(self.commands)

    def test_linux_test_check_and_doc_need_no_mac_repository_setting(self) -> None:
        for kind in ("test", "check", "doc"):
            with self.subTest(kind=kind):
                self.assertEqual(self.aws(kind)[1], "eligible")

    def test_local_only_request_excludes_every_ssh_target(self) -> None:
        rows = self.answer(local=True)
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("mac", "excluded"), ("natedev", "eligible"), ("aws", "excluded")])
        self.assertEqual(self.commands, [])

    def test_artifacts_final_gate_and_lint_never_use_linux_ssh(self) -> None:
        for kind in ("build", "example", "final", "lint"):
            with self.subTest(kind=kind):
                self.assertEqual(self.aws(kind)[1], "excluded")
        self.assertEqual(self.commands, [])

    def test_configured_kinds_are_read_on_each_eligibility_check(self) -> None:
        self.assertEqual(self.aws("check")[1], "eligible")
        _ = write_config_fixture(self.root / "aws_target.conf", "offload=on\nkinds=test,doc\n")
        self.assertEqual(self.aws("check")[1], "excluded")

    def test_mac_backoff_does_not_exclude_linux_but_linux_backoff_does(self) -> None:
        offload.write_backoff(10**12, "mac")
        self.assertEqual(self.aws()[1], "eligible")
        self.commands.clear()
        offload.write_backoff(10**12, "aws")
        self.assertEqual(self.aws()[1:3], ["excluded", "backoff"])
        self.assertEqual(self.commands, [])

    def test_all_occupied_slots_exclude_target_before_ssh(self) -> None:
        self.occupy(71)
        self.assertEqual(self.aws()[1], "eligible")
        self.occupy(72)
        self.commands.clear()
        self.assertEqual(self.aws()[1:3], ["excluded", "busy"])
        self.assertEqual(self.commands, [])

    def test_unreachable_host_is_excluded_without_shell_evaluation(self) -> None:
        self.reachable = False
        self.assertEqual(self.aws()[1:3], ["excluded", "unreachable"])
        self.assertTrue(self.commands)

    def test_queue_retries_never_evaluate_or_reserve_a_shell(self) -> None:
        with mock.patch.object(mac_test, "command_claim", side_effect=AssertionError("Routing cannot reserve")):
            for _ in range(3):
                self.assertEqual(self.aws()[1], "eligible")
        self.assertTrue(self.commands)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())


if __name__ == "__main__":
    _ = unittest.main()
