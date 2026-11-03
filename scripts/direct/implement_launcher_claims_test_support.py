"""Private same-path snapshots of a real completed launcher, for claim tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import ClassVar, cast, override

from scripts.direct.implement_launcher_test_support import ImplementLauncherFixture, STUB_MESH
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until


class CompletedWorkerFixture(ImplementLauncherFixture):
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    completed_launch: ClassVar[subprocess.CompletedProcess[str]]
    needs_completed_worker: ClassVar[bool] = True

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="launcher-claims-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        cls.fixture_root.mkdir()
        builder = cls("runTest")
        builder.root = cls.fixture_root
        builder.prepare_fixture()
        if cls.needs_completed_worker:
            session = builder.start_phase("completed")
            cls.completed_launch = builder.mesh_launch(session)
            builder.assertEqual(cls.completed_launch.returncode, 0, cls.completed_launch.stderr)
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.restore_fixture()

    def restore_fixture(self) -> None:
        self.root: Path = self.fixture_root
        if self.root.exists():
            shutil.rmtree(self.root)
        _ = shutil.copytree(self.fixture_snapshot, self.root)
        self.history_dir: Path = self.root / "history"
        self.working_dir: Path = self.root / "project"
        self.config_file: Path = self.root / "direct.conf"
        self.prompt_file: Path = self.root / "implementation_prompt.md"
        self.implement_script: Path = self.root / "scripts/direct/implement.sh"

    def completed_worker(self) -> tuple[Path, subprocess.CompletedProcess[str]]:
        # Each subcase gets private history, claims and roster at the paths the
        # real initial launcher recorded. Its successful result is part of the seed.
        self.restore_fixture()
        return self.root / "completed", self.completed_launch

    @override
    def tearDown(self) -> None:
        shutil.rmtree(self.root)


class DefaultIdentityProbesFixture(CompletedWorkerFixture):
    def assert_default_identity_probes(self, follow: bool) -> None:
        arguments = ["bash", str(self.implement_script)]
        if follow:
            session, initial = self.completed_worker()
            self.assertEqual(initial.returncode, 0, initial.stderr)
            roster = cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))
            arguments.extend(["--to", next(iter(roster))])
        else:
            session = self.start_phase("default-probes-new")
        arguments.extend([str(session), str(self.working_dir), str(self.prompt_file),
                          "impl", "the retry path", "impl", "writing the retry path", "0", "impl"])
        binaries = session / "bin"
        binaries.mkdir()
        sleep = binaries / "sleep"
        _ = sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_PROBES"\n')
        sleep.chmod(0o755)
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('#!/bin/bash\nif [[ -n "${TEST_PROBES_FINISHED:-}" ]]; then '
                               + ': > "$TEST_PROBES_FINISHED"; fi\n')
        mesh = self.root / "scripts/agents/codex_mesh.py"
        _ = mesh.write_text(STUB_MESH.replace(
            "verb = sys.argv[1]", "verb = sys.argv[1]\n"
            + "if verb in ('start', 'follow'):\n"
            + "    import subprocess\n"
            + "    agent_pid = pathlib.Path(os.environ['TEST_AGENT_PID'])\n"
            + "    agent_pid.with_name(agent_pid.name + '.tmp').write_text(str(os.getpid()))\n"
            + "    os.replace(agent_pid.with_name(agent_pid.name + '.tmp'), agent_pid)\n"
            + "    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['TEST_DEFAULT_RELEASE']], check=True)"
        ))
        probes = session / "probes"
        finished = session / "probes-finished"
        release = session / "release"
        pid_file = session / "agent-pid"
        environment = self.mesh_environment()
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "TEST_PROBES": str(probes), "TEST_PROBES_FINISHED": str(finished),
                            "TEST_AGENT_PID": str(pid_file), "TEST_DEFAULT_RELEASE": str(release)})
        process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            wait_until(lambda: finished.exists(), session, processes=(process,))
            os.kill(wait_for_pid(pid_file, processes=(process,)), 0)
            self.assertIsNone(process.poll())
            self.assertEqual(probes.read_text().splitlines(), ["1"] * 30)
            if follow:
                claim = cast("dict[str, object]", json.loads((session / "impl_claim_impl.json").read_text()))
                self.assertEqual(claim["turn_id"], "")
            else:
                self.assertFalse((session / "impl_worker_impl.json").exists())
        finally:
            output, errors = self.finish_default_launcher(process, release, mesh)
        self.assertEqual(process.returncode, 0, output + errors)
