#!/usr/bin/env python3
"""The machine roster joins Claude sessions, productions, runs, and Codex."""

from __future__ import annotations

from scripts.lib import names
from scripts.lib.test_names import fixture_names

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

from scripts.astra import design_roles
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message import unit_labels

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(SCRIPTS / "production"))
sys.path.insert(0, str(SCRIPTS / "direct"))

import codex_daemon
from codex_test_support import FakeDaemon, receive_frame, reply, write_codex_account
import process_table
import remove_workers
import roster
import send
import broadcast
import unit_lookup
import unit_runs
import herdr
from fake_herdr import CommandResponse, herdr_listener
import sessions
import showrunners
if TYPE_CHECKING:
    from ..whoami import account, agent_accounts
else:
    account = roster.account
    agent_accounts = roster.agent_accounts


ENTRY_FIELDS = {
    "machine", "kind", "role", "name", "status", "address", "cwd",
    "production", "showrunner", "showrunner_address", "unit", "director",
    "director_address", "slot_role", "session_dir", "thread", "shown_name", "job",
}
WORKER_SESSION_ID = "12345678-1234-5678-9abc-def012345678"
NO_SHOWRUNNER = roster.NoShowrunner()
NO_DIRECTOR = roster.NoDirector()


class RosterTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    sessions: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    notifier: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    runs: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    ps_state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    production_doc: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    director_run: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    ssh_log: Path = Path()
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        names.runtime_names.cache_clear()
        self.addCleanup(names.runtime_names.cache_clear)
        _ = self.enterContext(mock.patch.object(names, "load_names", return_value=fixture_names()))
        # Fixture processes run as this test's own pid: never read the real account it runs on.
        _ = self.enterContext(mock.patch.object(account, "process_config_dir", return_value=None))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.sessions = self.root / "sessions"
        self.notifier = self.root / "notifier"
        self.runs = self.root / "runs"
        binaries = self.root / "bin"
        for directory in (
            self.home, self.sessions, self.notifier, self.runs / "active", binaries,
            self.root / "history" / "runs",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.ps_state = self.root / "ps.txt"
        fake_ps = binaries / "ps"
        _ = fake_ps.write_text(
            "#!/bin/sh\ncat \"$ROSTER_PS_FIXTURE\"\n", encoding="utf-8",
        )
        fake_ps.chmod(0o755)
        self.ssh_log = self.root / "ssh.jsonl"
        fake_ssh = binaries / "ssh"
        _ = fake_ssh.write_text(
            f"#!{sys.executable}\n"
            + "import json, os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print(os.environ['SSH_SNAPSHOT'])\n"
            + "raise SystemExit(int(os.environ.get('SSH_STATUS', '0')))\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        fake_codex = binaries / "codex"
        _ = fake_codex.write_text(
            "#!/bin/sh\necho 'test roster unexpectedly ran codex' >&2\nexit 91\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)

        config = self.root / "showrunners.json"
        _ = config.write_text(json.dumps({
            "threshold_percent": 2,
            "repeat_minutes": 30,
            "stall_minutes": 5,
            "faults_to": "runner",
            "always": [],
        }), encoding="utf-8")
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.ssh_log),
            "SSH_SNAPSHOT": roster.RosterSnapshot(roster.Roster([], [])).encode(),
            "CLAUDE_BIN": str(fake_codex),
            "CLAUDE_CODE_SESSION_ID": "sid-runner",
            "CODEX_THREAD_ID": "",
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "SHOWRUNNERS_CONFIG": str(config),
            "SHOWRUNNERS_SESSIONS": str(Path(__file__).with_name("sessions.py")),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "ROSTER_RUN_ROOT": str(self.runs),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "ROSTER_PS_FIXTURE": str(self.ps_state),
            "PROCESS_TABLE_PS": f"cat {self.ps_state}",
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership"),
            "REVIEW_POOL_STATE": str(self.root / "review-pool"),
        }
        environment_patch = cast(
            AbstractContextManager[object],
            mock.patch.dict(os.environ, self.environment, clear=True),
        )
        _ = self.enterContext(environment_patch)
        _ = self.enterContext(mock.patch.object(showrunners, "CONFIG", config))
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier))
        _ = self.enterContext(mock.patch.object(showrunners, "SESSIONS_DIR", self.sessions))
        _ = self.enterContext(mock.patch.object(
            showrunners, "SESSIONS", Path(__file__).with_name("sessions.py"),
        ))
        _ = self.enterContext(mock.patch.object(
            remove_workers, "shutdown_held_sessions",
            return_value={"kind": "held sessions", "session_ids": set()},
        ))
        _ = self.enterContext(mock.patch.object(
            process_table, "PS", ["ps", "-eo", "pid=,ppid=,comm=,args="],
        ))
        _ = self.enterContext(mock.patch.object(herdr, "herdr_binary", return_value=self._herdr_fixture(self._herdr_sessions())))
        running_session = sessions.running_session
        def fixture_running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991100 or running_session(record)
        _ = self.enterContext(mock.patch.object(sessions, "running_session", side_effect=fixture_running))
        self.environment["HERDR_PROC"] = str(self.root / "herdr-proc")
        os.environ["HERDR_PROC"] = self.environment["HERDR_PROC"]
        self._build_complete_roster()
        self._herdr_process(991099, 0, ["herdr", "--session", "native", "server"])
        self._herdr_process(991100, 991099, ["claude"], HERDR_PANE_ID="w1:p1", SHOWRUNNER_UNIT="alpha", SHOWRUNNER_UNIT_ID="unit-one")

    def _write_session(
        self,
        file_name: str,
        name: str,
        session_id: str,
        cwd: Path,
        *,
        status: str = "working",
        tmux: str = "",
        updated: int = 1,
        pid: int | None = None,
        with_socket: bool = True,
        proc_start: str = "",
        entrypoint: str = "cli",
    ) -> None:
        socket_path = Path(self.enterContext(short_socket_directory())) / f"{file_name}.sock"
        if with_socket:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(socket_path))
            self.addCleanup(listener.close)
        record_pid = (991100 if file_name == "director" else os.getpid()) if pid is None else pid
        if not with_socket and not proc_start:
            started = sessions.process_start(record_pid)
            proc_start = started or "fixture"
        _ = (self.sessions / f"{file_name}.json").write_text(json.dumps({
            "pid": record_pid,
            "sessionId": session_id,
            "name": name,
            "messagingSocketPath": str(socket_path) if with_socket else "",
            "updatedAt": updated,
            "procStart": proc_start,
            "tmux": tmux,
            "cwd": str(cwd),
            "status": status,
            "entrypoint": entrypoint,
        }), encoding="utf-8")

    def _write_showrunner_timer(self) -> None:
        instance = self.notifier / "showrunner-alpha"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            "TARGET=session:sid-runner\n"
            + f"CHECK=zsh /scripts/production_check.sh {self.production_doc}\n",
            encoding="utf-8",
        )

    def _write_mesh(
        self,
        run: Path,
        entries: dict[str, dict[str, object]],
        *,
        server_pid: int | None = None,
    ) -> None:
        run.mkdir(parents=True, exist_ok=True)
        _ = (run / "mesh_server.json").write_text(json.dumps({
            "port": 45_678,
            "pid": os.getpid() if server_pid is None else server_pid,
        }), encoding="utf-8")
        _ = (run / "mesh_roster.json").write_text(json.dumps(entries), encoding="utf-8")

    def _build_complete_roster(self) -> None:
        runner_cwd = self.home / "projects" / "runner"
        director_cwd = self.home / "projects" / "unit-one"
        worker_cwd = self.home / "projects" / "worker"
        solo_cwd = self.home / "projects" / "solo"
        free_cwd = self.home / "projects" / "free"
        self.production_doc = self.root / "alpha-production.md"
        _ = self.production_doc.write_text("\n".join((
            "- **User zone:** America/New_York",
            "## Units",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| unit-one | docs/plan.md | {director_cwd} | unit-one | — | — |",
        )), encoding="utf-8")
        self._write_showrunner_timer()
        self._write_session(str(os.getpid()), "runner", "sid-runner", runner_cwd, status="ready")
        self._write_session(
            "director", "director", "sid-director", director_cwd,
            status="working", tmux="old-label:@1.%1",
        )
        self._write_session(
            "worker", "claude-worker", WORKER_SESSION_ID, worker_cwd, status="idle"
        )
        self._write_session("solo", "solo-director", "sid-solo", solo_cwd, status="waiting")
        self._write_session("free-old", "old-free-name", "sid-free", free_cwd, updated=1)
        self._write_session("free-new", "free-claude", "sid-free", free_cwd, status="ready", updated=2)

        self.director_run = self.runs / "director-run"
        self.director_run.mkdir()
        _ = (self.runs / "active" / "sid-director").write_text(
            str(self.director_run) + "\n", encoding="utf-8",
        )
        _ = (self.director_run / "workers").write_text(
            "12345678\talpha-test\n", encoding="utf-8",
        )
        self._write_mesh(self.director_run, {
            "alpha-impl": {
                "thread_id": "thread-shared", "turn_id": "turn-1", "status": "running",
                "launcher_pid": os.getpid(),
            },
            "alpha-fix": {"thread_id": "thread-fix", "status": "done"},
            "alpha-failed": {"thread_id": "thread-failed", "status": "failed"},
            "alpha-start": {
                "thread_id": "thread-start", "status": "starting", "launcher_pid": os.getpid(),
            },
            "alpha-wait": {
                "thread_id": "thread-wait", "status": "waiting_capacity",
                "launcher_pid": os.getpid(),
            },
            "alpha-exhaust": {"thread_id": "thread-exhaust", "status": "capacity_exhausted"},
            "alpha-research": {
                "thread_id": "thread-lens", "turn_id": "turn-2", "status": "running",
                "launcher_pid": os.getpid(), "role": "review", "lens": "adversary",
            },
            "alpha-review-plain": {
                "thread_id": "thread-review", "turn_id": "turn-3", "status": "running",
                "launcher_pid": os.getpid(), "role": "review",
            },
            "alpha-ended": {"thread_id": "thread-ended", "status": "ended"},
            "alpha-no-thread": {"status": "starting", "launcher_pid": os.getpid()},
        })

        solo_run = self.runs / "solo-run"
        solo_run.mkdir()
        _ = (self.runs / "active" / "sid-solo").write_text(
            str(solo_run) + "\n", encoding="utf-8",
        )

        orphan_run = self.runs / "orphan-run"
        self._write_mesh(orphan_run, {
            "orphan-impl": {
                "thread_id": "thread-orphan", "turn_id": "turn-orphan", "status": "running",
                "launcher_pid": os.getpid(),
            },
            "orphan-fix": {"thread_id": "thread-orphan-fix", "status": "done"},
        })
        (orphan_run / "heartbeat.log").touch()

        stale_run = self.runs / "stale-marker-run"
        self._write_mesh(stale_run, {
            "stale-impl": {
                "thread_id": "thread-stale", "turn_id": "turn-stale", "status": "running",
                "launcher_pid": os.getpid(),
            },
        })
        _ = (self.runs / "active" / "dead-director").write_text(
            str(stale_run) + "\n", encoding="utf-8",
        )

        dead_server_run = self.runs / "dead-server-run"
        self._write_mesh(dead_server_run, {
            "dead-impl": {
                "thread_id": "thread-dead", "turn_id": "turn-dead", "status": "running",
                "launcher_pid": 999_999_999,
            },
        }, server_pid=999_999_999)
        _ = (dead_server_run / "heartbeat.log").touch()

        _ = (self.director_run / "review_status_adversary").write_text(
            "reviewing\n", encoding="utf-8"
        )
        _ = (self.director_run / "review_pid_adversary").write_text(
            f"{os.getpid()}\n", encoding="utf-8"
        )
        _ = (self.director_run / "review_status_contract").write_text(
            "reviewed\n", encoding="utf-8"
        )
        _ = (self.director_run / "review_pid_contract").write_text(
            f"{os.getpid()}\n", encoding="utf-8"
        )
        _ = (self.director_run / "review_status").write_text(
            "reviewing\n", encoding="utf-8"
        )
        _ = (self.director_run / "review_pid").write_text(
            f"{os.getpid()}\n", encoding="utf-8"
        )
        _ = self.ps_state.write_text(
            f"700 {os.getpid()} bash bash /scripts/agents/agent_exec.sh\n"
            + "701 700 codex codex exec --ephemeral --sandbox read-only\n",
            encoding="utf-8",
        )

    def _daemon_sessions(self) -> list[codex_daemon.DaemonSession]:
        return [
            codex_daemon.DaemonSession(
                "thread-shared", "worker duplicate", str(self.home / "projects" / "unit-one"), "active",
            ),
            codex_daemon.DaemonSession(
                "thread-window", "Codex window", str(self.home / "projects" / "codex"), "idle",
            ),
        ]

    def _entries(self) -> dict[str, roster.RosterEntry]:
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(found.problems, [])
        return {entry.name: entry for entry in found.entries}

    def test_fast_labels_agree_with_full_roster_for_live_claude_sessions(self) -> None:
        full = roster.unit_labels(list(self._entries().values()))
        wanted = ["sid-director", "sid-runner", "sid-free"]
        with mock.patch.object(process_table, "snapshot", side_effect=AssertionError("no process table")) as scan, \
             mock.patch.object(codex_daemon, "loaded_sessions", side_effect=AssertionError("no daemon")) as daemon, \
             mock.patch.object(account, "home_account_state", side_effect=AssertionError("no accounts")) as accounts:
            fast = roster.unit_labels_for(wanted, self.environment)
        self.assertEqual(fast, {session_id: full[session_id] for session_id in wanted})
        self.assertEqual(fast["sid-director"], roster.UnitLabels("director", "alpha", "unit-one"))
        scan.assert_not_called()
        daemon.assert_not_called()
        accounts.assert_not_called()

    def test_renamed_production_has_the_same_full_and_fast_labels(self) -> None:
        _ = (self.notifier / "showrunner-alpha").rename(self.notifier / "showrunner-builds")
        _ = self.production_doc.write_text(
            "## Production Context\n\n- **Formerly:** `alpha`\n\n" + self.production_doc.read_text(),
        )
        full = roster.unit_labels(self._entries().values())
        fast = roster.unit_labels_for(["sid-director"], self.environment)
        self.assertEqual(full["sid-director"], roster.UnitLabels("director", "builds", "unit-one"))
        self.assertEqual(fast, {"sid-director": full["sid-director"]})

    def test_duplicate_unit_marks_leave_both_sessions_unnamed_on_both_paths(self) -> None:
        self._write_session("duplicate", "other-director", "sid-duplicate", self.home / "other", pid=991101)
        self._herdr_process(991101, 991099, ["claude"], HERDR_PANE_ID="w1:p2",
                            SHOWRUNNER_UNIT="alpha", SHOWRUNNER_UNIT_ID="unit-one")
        running_session = sessions.running_session

        def fixture_running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991101 or running_session(record)

        with mock.patch.object(sessions, "running_session", side_effect=fixture_running), \
             mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(self.environment, roster.ThisMachine())
            fast = roster.unit_labels_for(["sid-director", "sid-duplicate"], self.environment)
        full = roster.unit_labels(found.entries)
        self.assertTrue(any("its mark is on herdr sessions" in problem for problem in found.problems))
        expected = {
            "sid-director": roster.UnitLabels("director", "—", "—"),
            "sid-duplicate": roster.UnitLabels("other-director", "—", "—"),
        }
        self.assertEqual({sid: full[sid] for sid in expected}, expected)
        self.assertEqual(fast, expected)

    def _run_main(
        self, *arguments: str,
        daemon: list[codex_daemon.DaemonSession] | codex_daemon.DaemonUnreadable | None = None,
        here: bool = True,
    ) -> tuple[int, list[str], list[str]]:
        output, errors = io.StringIO(), io.StringIO()
        daemon_result = self._daemon_sessions() if daemon is None else daemon
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=daemon_result), \
                redirect_stdout(output), redirect_stderr(errors):
            result = roster.main([*( ["--here"] if here else []), *arguments])
        return result, output.getvalue().splitlines(), errors.getvalue().splitlines()

    def _entry(
        self, name: str, role: roster.Role, address: str, *, kind: str = "claude",
        cwd: str = "", showrunner: str = "", showrunner_address: str = "",
        unit: str = "", director: str = "", director_address: str = "",
        session_dir: str = "",
    ) -> roster.RosterEntry:
        showrunner_link: roster.ShowrunnerRelationship = (
            roster.UnderShowrunner("production", showrunner, showrunner_address)
            if showrunner else roster.NoShowrunner()
        )
        director_link: roster.DirectorRelationship = (
            roster.UnderDirector(unit, director, director_address)
            if director else roster.NoDirector()
        )
        if role == "unit director" and unit:
            director_link = roster.DirectsUnit(unit)
        assignment: roster.Assignment = (
            roster.WorkerAssignment("impl", session_dir)
            if role == "worker" else roster.NotAWorker()
        )
        return roster.RosterEntry(
            machine="machine", kind=kind, role=role, name=name, status="running",
            reach=roster.Messageable(address), cwd=cwd, showrunner=showrunner_link,
            director=director_link, assignment=assignment,
            session=(roster.ClaudeSession(address.removeprefix("session:"))
                     if kind == "claude" and address.startswith("session:") else roster.NoClaudeSession()),
        )

    def test_durable_role_roster_uses_holder_reach_after_its_run_folder_is_gone(self) -> None:
        design = design_roles.HeldDesign(design_roles.DesignPlacement("00000000-0000-4000-8000-000000000046", "fixture-host", 2),
            "fixture-repo", str(self.root / "checkout"), str(self.root / "checkout/design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.UnitHolder("alpha", "unit-one"), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "lasting-designer-alias", design_roles.RoleOpen("lasting-thread")),
             design_roles.RoleInstance("reviewer", "lasting-reviewer-alias", design_roles.RoleNotOpened())))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        mesh = design_roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True)
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"lasting-designer-alias": {
            "thread_id": "lasting-thread", "status": "done"}}))
        with mock.patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach("sid-director")):
            found = self._entries()
        entry = found["lasting-designer-alias"]
        self.assertEqual(entry.thread, roster.CodexThread("lasting-thread"))
        self.assertEqual(entry.role, "astra-designer")
        self.assertEqual(entry.director, roster.UnderDirector("unit-one", "director", "session:sid-director"))
        self.assertEqual(entry.assignment, roster.HeldDesignRole("designer", str(mesh)))
        self.assertIsInstance(entry.reach, roster.CannotReceiveMessages)
        self.assertEqual(sum(row.thread == roster.CodexThread("lasting-thread") for row in found.values()), 1)

    def test_every_role_and_status_is_returned_with_its_relationships(self) -> None:
        entries = self._entries()
        machine = socket.gethostname().split(".", 1)[0]

        self.assertEqual(entries["runner"], roster.RosterEntry(
            machine=machine, kind="claude", role="showrunner", name="runner",
            status="ready", reach=roster.Messageable("session:sid-runner"),
            cwd=str(self.home / "projects" / "runner"),
            showrunner=roster.RunsProduction("alpha"),
            director=roster.NoDirector(),
            assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("sid-runner"),
            account=roster.AccountUnread("process account could not be read"),
        ))
        self.assertEqual(entries["director"], roster.RosterEntry(
            machine=machine, kind="claude", role="unit director", name="director",
            status="working", reach=roster.Messageable("session:sid-director"),
            cwd=str(self.home / "projects" / "unit-one"),
            showrunner=roster.UnderShowrunner(
                "alpha", "runner", "session:sid-runner",
            ),
            director=roster.DirectsUnit("unit-one"),
            assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("sid-director"),
            account=roster.AccountUnread("process account could not be read"),
        ))
        self.assertEqual(entries["claude-worker"].role, "worker")
        self.assertEqual(entries["claude-worker"].director, roster.UnderDirector(
            "unit-one", "director", "session:sid-director",
        ))
        self.assertEqual(entries["claude-worker"].assignment, roster.WorkerAssignment(
            "test", str(self.director_run), roster.AssignmentUnrecorded(), "test",
        ))
        self.assertEqual(entries["solo-director"].role, "unit director")
        self.assertEqual(entries["solo-director"].showrunner, roster.NoShowrunner())
        self.assertEqual(entries["free-claude"].role, "freestanding")
        self.assertNotIn("old-free-name", entries)

        expected_statuses = {
            "alpha-impl": "running",
            "alpha-fix": "idle",
            "alpha-failed": "failed",
            "alpha-start": "starting",
            "alpha-wait": "waiting for capacity",
            "alpha-exhaust": "out of capacity",
        }
        for name, status in expected_statuses.items():
            with self.subTest(name=name):
                self.assertEqual(entries[name].status, status)
                self.assertEqual(entries[name].role, "worker")
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "director", "session:sid-director",
                ))
                self.assertEqual(entries[name].showrunner, roster.UnderShowrunner(
                    "alpha", "runner", "session:sid-runner",
                ))
                expected_reach: roster.MessageReach = (
                    roster.Messageable(f"worker:director-run/{name}")
                    if name in ("alpha-impl", "alpha-wait")
                    else roster.CannotReceiveMessages(
                        "between turns; its unit director resumes it"
                    )
                )
                self.assertEqual(entries[name].reach, expected_reach)
        self.assertEqual(
            entries["alpha-research"].assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "research"),
        )
        self.assertEqual(
            entries["alpha-impl"].assignment,
            roster.WorkerAssignment("no role", str(self.director_run), roster.AssignmentUnrecorded(), "impl"),
        )
        self.assertEqual(
            entries["adversary reviewer"].assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "adversary"),
        )
        self.assertEqual(
            entries["adversary reviewer"].reach,
            roster.CannotReceiveMessages("blind reviewer"),
        )
        self.assertNotIn("contract reviewer", entries)
        self.assertEqual(entries["orphan-impl"].director, roster.NoDirector())
        self.assertNotIn("alpha-ended", entries)
        self.assertIn("dead-impl", entries)
        self.assertNotIn("stale-impl", entries)

        self.assertEqual(entries["Codex window"].role, "freestanding")
        self.assertEqual(
            entries["Codex window"].reach,
            roster.Messageable("codex:thread-window"),
        )
        self.assertNotIn("worker duplicate", entries)

    def test_text_nests_workers_and_marks_this_session(self) -> None:
        result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        runner = next(line for line in lines if "[session:sid-runner]" in line)
        director = next(line for line in lines if "[session:sid-director]" in line)
        worker = next(line for line in lines if "[worker:director-run/alpha-impl]" in line)
        self.assertIn("(you)", runner)
        self.assertEqual(len(director) - len(director.lstrip()), len(runner) - len(runner.lstrip()) + 2)
        self.assertEqual(len(worker) - len(worker.lstrip()), len(director) - len(director.lstrip()) + 2)
        self.assertLess(lines.index(runner), lines.index(director))
        self.assertLess(lines.index(director), lines.index(worker))
        orphan_header = f"  director not running: {self.runs / 'orphan-run'}"
        self.assertIn(orphan_header, lines)
        orphan_worker = next(line for line in lines if "worker:orphan-run/orphan-impl" in line)
        self.assertTrue(orphan_worker.startswith("    "))
        self.assertTrue(any(line.strip() == "your Codex sessions" for line in lines))
        self.assertTrue(any("~/projects/unit-one" in line for line in lines))

    def test_socketless_director_keeps_worker_links_and_json_addresses(self) -> None:
        self._write_session(
            "director-new", "director", "sid-director", self.home / "projects" / "unit-one",
            updated=20, with_socket=False,
        )
        entries = self._entries()
        self.assertEqual(entries["director"].reach, roster.CannotReceiveMessages("no messaging socket"))
        for name in ("claude-worker", "alpha-impl", "adversary reviewer"):
            with self.subTest(worker=name):
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "director", "session:sid-director",
                ))
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[roster.SerializedRosterEntry], document["entries"])
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["director"]["address"], "")
        for name in ("claude-worker", "alpha-impl", "adversary reviewer"):
            with self.subTest(serialized_worker=name):
                self.assertEqual(by_name[name]["director_address"], "session:sid-director")

    def test_socketless_director_nests_its_workers_in_full_and_filtered_text(self) -> None:
        self._write_session(
            "director-new", "director", "sid-director", self.home / "projects" / "unit-one",
            updated=20, with_socket=False,
        )
        for arguments in ((), ("--kind", "codex")):
            with self.subTest(arguments=arguments):
                result, lines, errors = self._run_main(*arguments)
                self.assertEqual((result, errors), (0, []))
                directors = [line for line in lines if "[session:sid-director]" in line]
                self.assertEqual(len(directors), 1)
                director = directors[0]
                workers = ("alpha-impl",) if arguments else ("alpha-impl", "claude-worker")
                for name in workers:
                    address = f"worker:director-run/{name}" if name == "alpha-impl" else f"session:{WORKER_SESSION_ID}"
                    worker = next(line for line in lines if f"[{address}]" in line)
                    self.assertEqual(len(worker) - len(worker.lstrip()),
                                     len(director) - len(director.lstrip()) + 2)
                    self.assertLess(lines.index(director), lines.index(worker))
                    self.assertEqual(sum(f"[{address}]" in line for line in lines), 1)
                self.assertIn(f"  director not running: {self.runs / 'orphan-run'}", lines)
                if arguments:
                    self.assertIn("(context)", director)

    def test_socketless_showrunner_nests_linked_directors_in_full_and_filtered_text(self) -> None:
        entries = self._entries()
        runner = replace(entries["runner"], reach=roster.CannotReceiveMessages("no messaging socket"))
        fixture = roster.Roster([runner, entries["director"], entries["alpha-impl"]], [])
        for arguments in ([], ["--kind", "codex"]):
            with self.subTest(arguments=arguments):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(roster, "roster", return_value=fixture), \
                        redirect_stdout(output), redirect_stderr(errors):
                    result = roster.main(["--here", *arguments])
                lines = output.getvalue().splitlines()
                self.assertEqual((result, errors.getvalue()), (0, ""))
                runners = [line for line in lines if "[session:sid-runner]" in line]
                self.assertEqual(len(runners), 1)
                director = next(line for line in lines if "[session:sid-director]" in line)
                worker = next(line for line in lines if "[worker:director-run/alpha-impl]" in line)
                self.assertEqual(len(director) - len(director.lstrip()),
                                 len(runners[0]) - len(runners[0].lstrip()) + 2)
                self.assertEqual(len(worker) - len(worker.lstrip()),
                                 len(director) - len(director.lstrip()) + 2)
                self.assertLess(lines.index(runners[0]), lines.index(director))
                self.assertLess(lines.index(director), lines.index(worker))
                if arguments:
                    self.assertIn("(context)", runners[0])

    def test_roster_reads_shared_worker_ledger_and_later_old_path_appends(self) -> None:
        current = self.director_run / "workers"
        previous = self.director_run / "seats"
        _ = current.replace(previous)
        old_time = 1_700_000_000
        os.utime(previous, (old_time, old_time))
        worker = self._entries()["claude-worker"]
        self.assertEqual(worker.assignment, roster.WorkerAssignment("test", str(self.director_run), roster.AssignmentUnrecorded(), "test"))
        self.assertTrue(previous.samefile(current))
        self.assertEqual(current.stat().st_mtime, old_time)
        modified = current.stat().st_mtime_ns
        self.assertEqual(self._entries()["claude-worker"].assignment, worker.assignment)
        self.assertEqual(current.stat().st_mtime_ns, modified)
        self._write_session("late-worker", "late-worker", "late-worker-id", self.home / "late-project")
        self.assertEqual(self._entries()["late-worker"].role, "freestanding")
        with previous.open("a", encoding="utf-8") as ledger:
            _ = ledger.write("late-worker-id\talpha-impl\n")
        self.assertIn("late-worker-id", current.read_text(encoding="utf-8"))
        modified = current.stat().st_mtime_ns
        late_worker = self._entries()["late-worker"]
        self.assertEqual(late_worker.role, "worker")
        self.assertEqual(late_worker.assignment, roster.WorkerAssignment("impl", str(self.director_run), roster.AssignmentUnrecorded(), "impl"))
        self.assertEqual(late_worker.director, worker.director)
        self.assertTrue(previous.samefile(current))
        self.assertEqual(current.stat().st_mtime_ns, modified)

    def test_background_id_prefix_places_claude_worker_in_its_run(self) -> None:
        worker = self._entries()["claude-worker"]
        self.assertTrue(WORKER_SESSION_ID.startswith("12345678"))
        self.assertEqual(
            (worker.role, worker.director, worker.assignment),
            (
                "worker",
                roster.UnderDirector("unit-one", "director", "session:sid-director"),
                roster.WorkerAssignment("test", str(self.director_run), roster.AssignmentUnrecorded(), "test"),
            ),
        )

    def test_codex_review_lens_uses_role_and_lens_fields(self) -> None:
        entry = self._entries()["alpha-research"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "research"),
        )

    def test_codex_review_without_lens_uses_its_role(self) -> None:
        entry = self._entries()["alpha-review-plain"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review", str(self.director_run), "review", "plain"),
        )

    def test_codex_worker_without_recorded_role_has_no_role(self) -> None:
        self.assertEqual(
            self._entries()["alpha-impl"].assignment,
            roster.WorkerAssignment("no role", str(self.director_run), roster.AssignmentUnrecorded(), "impl"),
        )

    def test_mesh_assignment_states_come_from_records_and_not_names(self) -> None:
        self._write_mesh(self.director_run, {
            "arbitrary": {"thread_id": "thread-impl", "status": "done", "role": "impl"},
            "another": {"thread_id": "thread-test", "status": "done", "role": "test"},
            "repair": {"thread_id": "thread-fix", "status": "done", "role": "fix"},
            "arbitrary-reviewer": {"thread_id": "thread-review", "status": "done", "role": "review"},
            "legacy-impl": {"thread_id": "thread-old", "status": "done"},
            "invalid-review": {"thread_id": "thread-bad", "status": "done", "role": "designer"},
        })
        entries = self._entries()
        for name, expected in (("arbitrary", "impl"), ("another", "test"), ("repair", "fix"),
                               ("arbitrary-reviewer", "review"),
                               ("legacy-impl", roster.AssignmentUnrecorded()),
                               ("invalid-review", roster.AssignmentUnreadable("designer"))):
            with self.subTest(name=name):
                entry = entries[name]
                self.assertEqual(entry.role, "worker")
                self.assertIsInstance(entry.assignment, roster.WorkerAssignment)
                assert isinstance(entry.assignment, roster.WorkerAssignment)
                self.assertEqual(entry.assignment.role, expected)
                restored = roster.RosterSnapshot.decode(roster.RosterSnapshot(roster.Roster([entry], [])).encode()).result.entries[0]
                self.assertEqual(restored.assignment, entry.assignment)
                self.assertEqual(restored.thread, entry.thread)
        self.assertIsInstance(roster.shown_name(entries["legacy-impl"]).job, roster.JobUnassigned)
        self.assertIsInstance(roster.shown_name(entries["invalid-review"]).job, roster.JobUnassigned)

    def test_display_rename_keeps_record_keys_and_adds_a_structured_job(self) -> None:
        entry = replace(self._entry("identity-impl", "worker", "worker:run/identity-impl"),
                        showrunner=roster.ProductionWithoutRunningShowrunner("sdlc"),
                        director=roster.UnderDirector("roles-unit", "director", "session:director"),
                        assignment=roster.WorkerAssignment("impl", "/fixture/run", "impl", "impl"))
        snapshot = roster.RosterSnapshot(roster.Roster([entry], [])).encode()
        with mock.patch.object(names, "runtime_names", return_value=fixture_names(worker="builder")), \
                mock.patch.object(roster, "roster", return_value=roster.Roster([entry], [])):
            self.assertEqual(roster.shown_name(entry).render(), "sdlc · roles · builder impl")
            result, lines, errors = self._run_main("--json")
            self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        row = cast(list[dict[str, object]], document["entries"])[0]
        self.assertEqual(row["job"], {"state": "assigned", "key": "worker", "display": "builder", "qualifier": "impl"})
        self.assertEqual((row["role"], row["name"], row["address"], row["slot_role"]),
                         ("worker", "identity-impl", "worker:run/identity-impl", "impl"))
        self.assertEqual(roster.RosterSnapshot(roster.Roster([entry], [])).encode(), snapshot)
        self.assertEqual(roster.RosterSnapshot.decode(snapshot).result.entries, [entry])

    def test_rendering_and_serializing_multiple_jobs_share_one_configuration_read(self) -> None:
        job = roster.ShownJob("worker", roster.JobSlot("impl"))
        shown = roster.ShownName(roster.ProductionPart("sdlc"), roster.UnitPart("roles-unit"), job, "worker:run/identity")
        with mock.patch.object(names, "load_names", return_value=fixture_names(worker="builder")) as load:
            for attempt in range(3):
                with self.subTest(attempt=attempt):
                    self.assertEqual(shown.render(), "sdlc · roles · builder impl")
                    self.assertEqual(roster.serialized_job(job)["display"], "builder")
        load.assert_called_once_with()

    def test_unreadable_job_display_names_the_configuration_entry_to_fix(self) -> None:
        job = roster.ShownJob("worker", roster.Unqualified())
        with mock.patch.object(names, "runtime_names", return_value=names.MissingKey("jobs", "worker")):
            self.assertEqual(roster.serialized_job(job), {
                "state": "unassigned", "reason": f"names: {names.CONFIG_PATH}: [jobs] has no worker entry",
            })

    def test_missing_jobs_report_the_assignment_problem_and_record_to_fix(self) -> None:
        base = self._entry("misleading-impl", "worker", "worker:run/misleading-impl")
        for state, reason in ((roster.AssignmentUnrecorded(), "assignment not recorded"),
                              (roster.AssignmentUnreadable("bad-role"), "assignment record not readable")):
            with self.subTest(state=state):
                entry = replace(base, assignment=roster.WorkerAssignment("impl", "/fixture/run", state, "impl"))
                with mock.patch.object(roster, "roster", return_value=roster.Roster([entry], [])):
                    result, lines, errors = self._run_main()
                    self.assertEqual((result, errors), (0, []))
                    human = "\n".join(lines)
                    self.assertIn(reason, human)
                    self.assertIn("/fixture/run", human)
                    result, lines, errors = self._run_main("--json")
                document = cast(dict[str, object], json.loads("\n".join(lines)))
                row = cast(list[dict[str, object]], document["entries"])[0]
                job = cast(dict[str, str], row["job"])
                self.assertEqual(job["state"], "unassigned")
                self.assertIn(reason, job["reason"])
                self.assertNotIn("key", job)

    def test_invalid_name_configuration_renders_the_address_and_reports_once(self) -> None:
        entry = replace(self._entry("identity", "unit director", "session:identity"),
                        showrunner=roster.ProductionWithoutRunningShowrunner("sdlc"),
                        director=roster.DirectsUnit("roles-unit"))
        error = names.InvalidNames("/fixture/names.toml", "invalid fixture table")
        output = io.StringIO()
        with mock.patch.object(names, "runtime_names", wraps=names.runtime_names), \
                mock.patch.object(names, "load_names", return_value=error), \
                mock.patch.object(names, "_reported_errors", set[names.NamesError]()), redirect_stderr(output):
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    self.assertEqual(roster.shown_name(entry).render(), "session:identity")
        self.assertEqual(output.getvalue().count("invalid fixture table"), 1)

    def test_shown_names_keep_organizational_role_assignment_and_slot_distinct(self) -> None:
        base = replace(self._entry("unrelated-name", "worker", "worker:run/arbitrary"),
                       showrunner=roster.UnderShowrunner("sdlc", "runner", "session:runner"),
                       director=roster.UnderDirector("roles-unit", "director", "session:director"))
        for role, slot, expected in (("impl", "impl", "worker impl"), ("test", "test", "worker test"),
                                     ("review", "code6", "code reviewer 6"),
                                     ("review", "code7", "code reviewer 7")):
            with self.subTest(slot=slot):
                entry = replace(base, assignment=roster.WorkerAssignment(
                    role, "/run", roster.worker_role({"role": role}), slot))
                self.assertEqual(roster.shown_name(entry).render(), f"sdlc · roles · {expected}")
                self.assertEqual(entry.role, "worker")
                self.assertEqual(entry.reach, roster.Messageable("worker:run/arbitrary"))
        director = replace(base, role="unit director", director=roster.DirectsUnit("roles-unit"),
                           assignment=roster.NotAWorker())
        self.assertEqual(roster.shown_name(director).render(), "sdlc · roles · unit director")
        runner = replace(director, role="showrunner", showrunner=roster.RunsProduction("sdlc"))
        self.assertEqual(roster.shown_name(runner).render(), "sdlc · showrunner")

    def test_shown_names_omit_missing_parts_and_freestanding_uses_its_address(self) -> None:
        entry = replace(self._entry("name-review", "worker", "worker:run/name-review"),
                        assignment=roster.WorkerAssignment("test", "/run", "test", "test"))
        self.assertEqual(roster.shown_name(entry).render(), "worker test")
        self.assertEqual(roster.shown_name(replace(entry, director=roster.UnderDirector(
            "roles-unit", "director", "session:director"))).render(), "roles · worker test")
        self.assertEqual(roster.shown_name(replace(entry, showrunner=roster.ProductionWithoutRunningShowrunner(
            "sdlc"))).render(), "sdlc · worker test")
        free = replace(entry, role="freestanding", assignment=roster.NotAWorker())
        shown = roster.shown_name(free)
        self.assertIsInstance(shown.job, roster.JobUnassigned)
        self.assertEqual(shown.render(), "worker:run/name-review")

    def test_unit_rename_and_transfer_change_only_shown_location(self) -> None:
        original = replace(self._entry("fixed-address", "worker", "worker:run/fixed-address", kind="codex"),
                           showrunner=roster.UnderShowrunner("sdlc", "runner", "session:runner"),
                           director=roster.UnderDirector("roles-unit", "director", "session:director"),
                           assignment=roster.WorkerAssignment("impl", "/run", "impl", "impl"),
                           thread=roster.CodexThread("fixed-thread"))
        renamed = replace(original, director=roster.UnderDirector("records-unit", "director", "session:director"))
        transferred = replace(renamed, showrunner=roster.UnderShowrunner("tools", "next", "session:next"))
        for entry, expected in ((original, "sdlc · roles · worker impl"),
                                (renamed, "sdlc · records · worker impl"),
                                (transferred, "tools · records · worker impl")):
            with self.subTest(expected=expected):
                decoded = roster.RosterSnapshot.decode(roster.RosterSnapshot(roster.Roster([entry], [])).encode()).result.entries[0]
                self.assertEqual(roster.shown_name(decoded).render(), expected)
                self.assertEqual(decoded.assignment, original.assignment)
                self.assertEqual(decoded.thread, roster.CodexThread("fixed-thread"))
                self.assertEqual(decoded.reach, roster.Messageable("worker:run/fixed-address"))

    def test_pool_titles_follow_each_number_and_current_lease(self) -> None:
        pool = self.root / "review-pool"
        state = pool / "sdlc" / "pool.json"
        state.parent.mkdir(parents=True)
        reviewers: dict[str, object] = {
            name: {"type": "code", "lease": {"unit": "roles-unit", "ordinal": number}}
            for number, name in ((6, "code6"), (7, "code7"))
        }
        _ = state.write_text(json.dumps({"reviewers": reviewers}))
        with mock.patch.dict(os.environ, {"REVIEW_POOL_STATE": str(pool)}):
            for number, name in ((6, "code6"), (7, "code7")):
                with self.subTest(name=name):
                    shown = roster.mesh_shown_name(str(pool / "sdlc" / "mesh" / name), name, {"role": "review"})
                    self.assertEqual(shown.render(), f"sdlc · roles · code reviewer {number}")
                    self.assertEqual(shown.address, name)
            reviewers["code6"] = {"type": "code", "lease": {"unit": "other-unit", "ordinal": 1}}
            _ = state.write_text(json.dumps({"reviewers": reviewers}))
            moved = roster.mesh_shown_name(str(pool / "sdlc" / "mesh" / "code6"), "code6", {"role": "review"})
            self.assertEqual(moved.render(), "sdlc · other · code reviewer 6")
            self.assertEqual(moved.address, "code6")

    def test_mesh_titles_use_held_design_holder_and_fall_back_when_it_is_unreadable(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000099"
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 1),
            "fixture-repo", str(self.root), str(self.root / "design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.UnitHolder("sdlc", "roles-unit"), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "design-address", design_roles.RoleNotOpened()),
             design_roles.RoleInstance("reviewer", "review-address", design_roles.RoleNotOpened())))
        mesh = str(design_roles.mesh_directory(design_id))
        for holder, prefix in ((design_roles.UnitHolder("sdlc", "roles-unit"), "sdlc · roles"),
                               (design_roles.ShowrunnerHolder("sdlc"), "sdlc")):
            design_roles._save(replace(design, holder=holder))  # pyright: ignore[reportPrivateUsage]
            for held_role, name, job in (("designer", "design-address", "designer"),
                                         ("reviewer", "review-address", "design reviewer")):
                with self.subTest(holder=holder, role=held_role):
                    shown = roster.mesh_shown_name(mesh, name, {"held_design_id": design_id, "held_role": held_role})
                    self.assertEqual(shown.render(), f"{prefix} · {job}")
                    self.assertEqual(shown.address, name)
        _ = (design_roles.design_directory(design_id) / "design.json").write_text("broken JSON")
        self.assertEqual(roster.mesh_shown_name(mesh, "review-address", {
            "held_design_id": design_id, "held_role": "reviewer"}).render(), "review-address")

    def test_mesh_worker_slots_keep_full_launch_identity_in_roster_and_titles(self) -> None:
        _ = (self.director_run / unit_runs.DIRECTOR_FILE).write_text("sid-director\n")
        self._write_mesh(self.director_run, {
            "alpha-render-a": {"thread_id": "render-thread", "status": "done", "role": "impl"},
            "alpha-physics-a": {"thread_id": "physics-thread", "status": "done", "role": "impl"},
            "arbitrary-address": {"thread_id": "explicit-thread", "status": "done", "role": "test", "slot": "test-b"},
        })
        for slot in ("render-a", "physics-a"):
            _ = (self.director_run / f"impl_worker_{slot}.json").write_text(json.dumps({
                "name": f"alpha-{slot}", "slot": slot}))
        entries = self._entries()
        for name, slot in (("alpha-render-a", "render-a"), ("alpha-physics-a", "physics-a"),
                           ("arbitrary-address", "test-b")):
            with self.subTest(name=name):
                entry = entries[name]
                assert isinstance(entry.assignment, roster.WorkerAssignment)
                self.assertEqual(entry.assignment.slot, slot)
                self.assertEqual(roster.shown_name(entry).render(), f"alpha · unit-one · worker {slot}")
                record: dict[str, object] = {"role": entry.assignment.role}
                if name == "arbitrary-address":
                    record["slot"] = slot
                title = roster.mesh_shown_name(str(self.director_run), name, record)
                self.assertEqual(title.render(), f"alpha · unit-one · worker {slot}")
                self.assertEqual(title.address, name)

    def test_roster_discovers_pool_reviewers_and_tracks_lease_changes_without_duplicate_daemon_rows(self) -> None:
        pool = self.root / "review-pool" / "sdlc"
        pool.mkdir(parents=True)
        state = pool / "pool.json"
        reviewers: dict[str, object] = {
            "code6": {"type": "code", "lease": {"unit": "roles-unit"}},
            "ui2": {"type": "ui", "lease": None},
            "code7": {"type": "code", "lease": {"unit": "roles-unit"}},
        }
        _ = state.write_text(json.dumps({"reviewers": reviewers}))
        mesh = pool / "mesh" / "3"
        self._write_mesh(mesh, {
            "code6": {"thread_id": "pool-code", "status": "running", "role": "review"},
            "ui2": {"thread_id": "pool-ui", "status": "done", "role": "review"},
            "code7": {"thread_id": "pool-ended", "status": "ended", "role": "review"},
        })
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[
            codex_daemon.DaemonSession("pool-code", "daemon-alias", str(self.root), "idle")]):
            result = roster.roster(os.environ, roster.ThisMachine())
        entries = {entry.name: entry for entry in result.entries}
        self.assertNotIn("daemon-alias", entries)
        self.assertNotIn("code7", entries)
        code, ui = entries["code6"], entries["ui2"]
        self.assertEqual(code.role, "worker")
        self.assertEqual(code.assignment, roster.WorkerAssignment("review", str(mesh), "review", "code6", "code"))
        self.assertEqual(code.reach, roster.Messageable("worker:review-pool/sdlc/3/code6"))
        self.assertEqual(code.thread, roster.CodexThread("pool-code"))
        self.assertEqual(roster.shown_name(code).render(), "sdlc · roles · code reviewer 6")
        self.assertEqual(roster.shown_name(ui).render(), "sdlc · UI reviewer 2")
        self.assertIsInstance(roster.shown_name(ui).unit, roster.NoUnit)
        reviewers["code6"] = {"type": "code", "lease": {"unit": "next-unit"}}
        _ = state.write_text(json.dumps({"reviewers": reviewers}))
        moved = self._entries()["code6"]
        self.assertEqual(roster.shown_name(moved).render(), "sdlc · next · code reviewer 6")
        self.assertEqual((moved.thread, moved.reach, moved.assignment), (code.thread, code.reach, code.assignment))

    def test_named_launch_record_wins_over_another_workers_matching_suffix(self) -> None:
        _ = (self.director_run / unit_runs.DIRECTOR_FILE).write_text("sid-director\n")
        self._write_mesh(self.director_run, {
            "arbitrary-impl": {"thread_id": "named-thread", "status": "running", "role": "test"}})
        for slot, name in (("impl", "another-worker"), ("test", "arbitrary-impl")):
            _ = (self.director_run / f"impl_worker_{slot}.json").write_text(json.dumps({
                "name": name, "slot": slot}))
        entry = self._entries()["arbitrary-impl"]
        self.assertIsInstance(entry.assignment, roster.WorkerAssignment)
        assert isinstance(entry.assignment, roster.WorkerAssignment)
        self.assertEqual(entry.assignment.slot, "test")
        self.assertEqual(roster.shown_name(entry).render(), "alpha · unit-one · worker test")
        title = roster.mesh_shown_name(str(self.director_run), "arbitrary-impl", {"role": "test"})
        self.assertEqual(title.render(), "alpha · unit-one · worker test")

    def test_pool_addresses_resolve_separately_across_productions_with_the_same_run_and_name(self) -> None:
        meshes: dict[str, Path] = {}
        for slug in ("sdlc", "hana"):
            pool = self.root / "review-pool" / slug
            pool.mkdir(parents=True)
            _ = (pool / "pool.json").write_text(json.dumps({"reviewers": {
                "code6": {"type": "code", "lease": None}}}))
            meshes[slug] = pool / "mesh" / "3"
            self._write_mesh(meshes[slug], {
                "code6": {"thread_id": f"{slug}-thread", "status": "running", "role": "review"}})
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            result = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(result.problems, [])
        reviewers = [entry for entry in result.entries if entry.name == "code6"]
        self.assertEqual(len(reviewers), 2)
        addresses: set[str] = set()
        for entry in reviewers:
            assert isinstance(entry.reach, roster.Messageable)
            assert isinstance(entry.showrunner, roster.ProductionWithoutRunningShowrunner)
            addresses.add(entry.reach.address)
            recipient = send.resolve_recipient(entry.reach.address, send.LocalOnly(), result)
            self.assertEqual(recipient, send.CodexWorkerRecipient(
                entry.reach.address, "code6", str(meshes[entry.showrunner.production])))
        self.assertEqual(len(addresses), 2)
        unit_worker = next(entry for entry in result.entries if entry.name == "alpha-impl")
        self.assertEqual(unit_worker.reach, roster.Messageable("worker:director-run/alpha-impl"))

    def test_leased_pool_reviewers_join_only_their_productions_unit_broadcast(self) -> None:
        for slug in ("alpha", "other"):
            pool = self.root / "review-pool" / slug
            pool.mkdir(parents=True)
            _ = (pool / "pool.json").write_text(json.dumps({"reviewers": {
                "code6": {"type": "code", "lease": {"unit": "unit-one"}}}}))
            self._write_mesh(pool / "mesh" / "3", {
                "code6": {"thread_id": f"{slug}-thread", "status": "running", "role": "review"}})
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            result = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(result.problems, [])
        director = next(entry for entry in result.entries if entry.name == "director")
        audience = broadcast.WorkersOfUnit("session:sid-director", "unit-one", director.machine)
        reviewers = [entry for entry in result.entries if entry.name == "code6"]
        self.assertEqual(len(reviewers), 2)
        for entry in reviewers:
            assert isinstance(entry.showrunner, roster.ProductionWithoutRunningShowrunner)
            if entry.showrunner.production == "alpha":
                self.assertEqual(entry.director, roster.UnderDirector("unit-one", "director", "session:sid-director"))
                self.assertTrue(broadcast.selects(audience, entry))
            else:
                self.assertEqual(entry.director, roster.UnderDirector("unit-one", "", ""))
                self.assertFalse(broadcast.selects(audience, entry))

        # A lease survives the director going away; its address does not.
        (self.sessions / "director.json").unlink()
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            offline = roster.roster(os.environ, roster.ThisMachine())
        leased = next(entry for entry in offline.entries if entry.name == "code6"
                      and isinstance(entry.showrunner, roster.ProductionWithoutRunningShowrunner)
                      and entry.showrunner.production == "alpha")
        self.assertEqual(leased.director, roster.UnderDirector("unit-one", "", ""))
        self.assertFalse(broadcast.selects(audience, leased))

    def test_unreadable_pool_reports_one_problem_without_listing_partial_reviewers(self) -> None:
        pool = self.root / "review-pool" / "sdlc"
        pool.mkdir(parents=True)
        self._write_mesh(pool / "mesh" / "1", {
            "code6": {"thread_id": "pool-code", "status": "running", "role": "review"}})
        for text in ("broken JSON", json.dumps({"reviewers": {
                "code6": {"type": "code", "lease": None}, "ui2": {"type": "unknown"}}})):
            with self.subTest(text=text):
                _ = (pool / "pool.json").write_text(text)
                result = roster.roster(os.environ, roster.ThisMachine())
                self.assertFalse(any(entry.name == "code6" for entry in result.entries))
                problems = [problem for problem in result.problems if "review pool" in problem]
                self.assertEqual(len(problems), 1, result.problems)
                self.assertIn(str(pool / "pool.json"), problems[0])

    def test_legacy_snapshot_assignment_is_unrecorded_and_unknown_organization_is_rejected(self) -> None:
        entry = self._entry("old-impl", "worker", "worker:old/old-impl")
        document = cast(dict[str, object], json.loads(roster.RosterSnapshot(roster.Roster([entry], [])).encode()))
        rows = cast(list[dict[str, object]], document["entries"])
        rows[0]["assignment"] = {"tag": "WorkerAssignment", "slot_role": "impl", "session_dir": "/old"}
        decoded = roster.RosterSnapshot.decode(json.dumps(document)).result.entries[0]
        self.assertEqual(decoded.assignment, roster.WorkerAssignment("impl", "/old", roster.AssignmentUnrecorded()))
        self.assertIsInstance(roster.shown_name(decoded).job, roster.JobUnassigned)
        rows[0]["role"] = "unknown organization"
        with self.assertRaises(ValueError):
            _ = roster.RosterSnapshot.decode(json.dumps(document))

    def test_held_design_roles_keep_jobs_json_and_legacy_snapshot_identity(self) -> None:
        mesh = str(self.root / "held-design")
        base = replace(self._entry("unrelated-name", "worker", "worker:held/arbitrary", kind="codex"),
                       showrunner=roster.ProductionWithoutRunningShowrunner("sdlc"),
                       director=roster.UnderDirector("roles-unit", "director", "session:director"))
        entries = [
            replace(base, role="astra-designer", name="design-address",
                    assignment=roster.HeldDesignRole("designer", mesh),
                    reach=roster.Messageable("worker:held/design-address"),
                    thread=roster.CodexThread("design-thread")),
            replace(base, role="astra-reviewer", name="review-address",
                    assignment=roster.HeldDesignRole("reviewer", mesh),
                    reach=roster.Messageable("worker:held/review-address"),
                    thread=roster.CodexThread("review-thread")),
        ]
        document = cast(dict[str, object], json.loads(roster.RosterSnapshot(roster.Roster(entries, [])).encode()))
        rows = cast(list[dict[str, object]], document["entries"])
        for index, job in enumerate(("designer", "design reviewer")):
            with self.subTest(job=job):
                expected = entries[index]
                self.assertEqual(roster.shown_name(expected).render(), f"sdlc · roles · {job}")
                restored = roster.RosterSnapshot.decode(json.dumps(document)).result.entries[index]
                self.assertEqual(restored, expected)
                # A pre-change snapshot uses WorkerAssignment for the same held role.
                rows[index]["assignment"] = {"tag": "WorkerAssignment", "slot_role": expected.role,
                                             "session_dir": mesh}
                legacy = roster.RosterSnapshot.decode(json.dumps(document)).result.entries[index]
                self.assertEqual(legacy, expected)

        with mock.patch.object(roster, "roster", return_value=roster.Roster(entries, [])):
            result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        rendered = cast(dict[str, object], json.loads("\n".join(lines)))
        flat = cast(list[dict[str, object]], rendered["entries"])
        for row, expected, job in zip(flat, entries, ("designer", "design reviewer"), strict=True):
            self.assertEqual((row["name"], row["role"], row["slot_role"], row["session_dir"]),
                             (expected.name, expected.role, expected.role, mesh))
            self.assertEqual(row["address"], f"worker:held/{expected.name}")
            self.assertEqual(row["shown_name"], f"sdlc · roles · {job}")

    def test_own_production_and_unit_are_not_links_to_blank_sessions(self) -> None:
        entries = self._entries()

        self.assertEqual(entries["runner"].showrunner, roster.RunsProduction("alpha"))
        self.assertEqual(entries["director"].director, roster.DirectsUnit("unit-one"))
        self.assertNotIsInstance(entries["runner"].showrunner, roster.UnderShowrunner)
        self.assertNotIsInstance(entries["director"].director, roster.UnderDirector)
        for entry in entries.values():
            if isinstance(entry.showrunner, roster.UnderShowrunner):
                self.assertTrue(entry.showrunner.name)
                self.assertTrue(entry.showrunner.address)
            if isinstance(entry.director, roster.UnderDirector):
                self.assertTrue(entry.director.name)
                self.assertTrue(entry.director.address)

        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["runner"]["shown_name"], "alpha · showrunner")
        self.assertEqual(by_name["director"]["shown_name"], "alpha · unit-one · unit director")
        for name, entry in self._entries().items():
            self.assertEqual(by_name[name]["name"], entry.name)
            self.assertEqual(by_name[name]["role"], entry.role)
            self.assertEqual(by_name[name]["shown_name"], roster.shown_name(entry).render())
        self.assertEqual(
            (
                by_name["runner"]["production"],
                by_name["runner"]["showrunner"],
                by_name["runner"]["showrunner_address"],
            ),
            ("alpha", "", ""),
        )
        self.assertEqual(
            (
                by_name["director"]["unit"],
                by_name["director"]["director"],
                by_name["director"]["director_address"],
            ),
            ("unit-one", "", ""),
        )

    def test_worker_between_turns_cannot_receive_messages(self) -> None:
        entry = self._entries()["alpha-fix"]

        self.assertEqual(entry.status, "idle")
        self.assertEqual(
            entry.reach,
            roster.CannotReceiveMessages(
                "between turns; its unit director resumes it"
            ),
        )

        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["alpha-fix"]["address"], "")

    def test_worker_between_turns_without_running_director_says_so(self) -> None:
        entry = self._entries()["orphan-fix"]

        self.assertEqual(entry.status, "idle")
        self.assertEqual(entry.director, roster.NoDirector())
        self.assertEqual(
            entry.reach,
            roster.CannotReceiveMessages(
                "between turns; no unit director is running"
            ),
        )

        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["orphan-fix"]["address"], "")

    def test_workers_keep_their_unit_title_after_the_director_stops(self) -> None:
        _ = (self.director_run / "progress_history_state.json").write_text(json.dumps({
            "production": "alpha", "unit": "unit-one",
            "working_dir": str(self.home / "projects" / "unit-one"),
        }))
        _ = (self.director_run / "mesh_roster.json").write_text(json.dumps({
            "alpha-impl": {"thread_id": "thread-shared", "status": "running", "role": "impl",
                           "launcher_pid": os.getpid()},
            "alpha-fix": {"thread_id": "thread-fix", "status": "done", "role": "fix"},
        }))
        _ = (self.director_run / "board.log").write_text("2026-10-29T12:00:00Z [test] handoff: role=impl fixture\n")
        before = self._entries()
        (self.sessions / "director.json").unlink()
        (self.director_run / "heartbeat.log").touch()
        entries = self._entries()
        for name in ("alpha-impl", "claude-worker"):
            with self.subTest(worker=name):
                entry = entries[name]
                self.assertEqual(roster.shown_name(entry), roster.shown_name(before[name]))
                self.assertEqual(entry.director, roster.UnitWithoutRunningDirector("unit-one"))
                restored = roster.RosterSnapshot.decode(
                    roster.RosterSnapshot(roster.Roster([entry], [])).encode()).result.entries[0]
                self.assertEqual(restored, entry)
        self.assertEqual(entries["alpha-fix"].reach, roster.CannotReceiveMessages(
            "between turns; no unit director is running"))
        result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        worker = next(line.strip() for line in lines if "[worker:director-run/alpha-impl]" in line)
        self.assertTrue(worker.startswith("alpha · unit-one · worker impl [worker:director-run/alpha-impl]"), worker)
        self.assertNotIn("worker worker", worker)
        self.assertTrue(any("director not running:" in line for line in lines))

    def test_same_worker_name_in_two_live_runs_has_two_unique_addresses(self) -> None:
        second_run = self.runs / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_mesh(second_run, {
            "alpha-impl": {
                "thread_id": "thread-other-run", "turn_id": "turn-other-run",
                "status": "running", "launcher_pid": os.getpid(), "role": "impl",
            },
        })
        (second_run / "heartbeat.log").touch()

        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())

        duplicate_workers = [entry for entry in found.entries if entry.name == "alpha-impl"]
        self.assertEqual(len(duplicate_workers), 2)
        self.assertEqual(
            {entry.reach for entry in duplicate_workers},
            {
                roster.Messageable("worker:director-run/alpha-impl"),
                roster.Messageable(
                    "worker:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/alpha-impl"
                ),
            },
        )
        addresses = [
            entry.reach.address
            for entry in found.entries
            if isinstance(entry.reach, roster.Messageable)
        ]
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_a_run_under_the_former_folder_name_is_still_listed(self) -> None:
        former = self.root / "former"
        second_run = former / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_mesh(second_run, {
            "alpha-impl": {
                "thread_id": "thread-other-run", "turn_id": "turn-other-run",
                "status": "running", "launcher_pid": os.getpid(), "role": "impl",
            },
        })
        (second_run / "heartbeat.log").touch()
        environment = {name: value for name, value in os.environ.items() if name != "ROSTER_RUN_ROOT"}

        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(unit_runs, "RUN_ROOT", self.runs), \
                mock.patch.object(unit_runs, "FORMER_RUN_ROOT", former), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())

        self.assertEqual(
            {entry.reach for entry in found.entries if entry.name == "alpha-impl"},
            {
                roster.Messageable("worker:director-run/alpha-impl"),
                roster.Messageable("worker:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/alpha-impl"),
            },
        )

    def test_newer_dead_record_does_not_supply_live_session_details(self) -> None:
        dead_cwd = self.home / "projects" / "dead-resume"
        self._write_session(
            "free-dead", "free-claude", "sid-free", dead_cwd,
            status="dead status", updated=3, pid=999_999_999,
        )
        entry = self._entries()["free-claude"]
        self.assertEqual((entry.cwd, entry.status), (
            str(self.home / "projects" / "free"), "ready",
        ))

    def test_a_relay_named_like_a_session_is_left_out_of_the_roster(self) -> None:
        # send.py's relay is a newer `claude -p` run started with `-n free-claude`.
        self._write_session("relay", "free-claude", "sid-relay", self.home / "projects" / "relay",
                            status="relaying", updated=9, entrypoint="sdk-cli")
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        named = [entry.session for entry in found.entries if entry.name == "free-claude"]
        self.assertEqual(named, [roster.ClaudeSession("sid-free")])

    def test_socketless_record_with_another_process_start_is_absent_from_the_roster(self) -> None:
        self._write_session(
            "reused", "phantom", "sid-phantom", self.home / "projects" / "ended",
            with_socket=False, proc_start="another start",
        )
        entries = self._entries()
        self.assertNotIn("phantom", list(entries))
        self.assertFalse(any(entry.session == roster.ClaudeSession("sid-phantom") for entry in entries.values()))

    def test_running_session_without_socket_retains_identity_and_has_no_address(self) -> None:
        self._write_session(
            "socketless", "socketless", "sid-socketless", self.home / "projects" / "socketless",
            with_socket=False,
        )
        entry = self._entries()["socketless"]
        self.assertEqual(entry.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(entry.session, roster.ClaudeSession("sid-socketless"))
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        row = next(row for row in rows if row["name"] == "socketless")
        self.assertEqual(row["address"], "")
        self.assertEqual(set(row), ENTRY_FIELDS)

    def test_sender_uses_newest_claude_identity_even_without_a_socket(self) -> None:
        self._write_session(
            "free-socketless", "newest-free", "sid-free", self.home / "projects" / "resumed",
            status="waiting", updated=20, with_socket=False,
        )
        entries = self._entries()
        entry = entries["newest-free"]
        self.assertNotIn("free-claude", entries)
        self.assertEqual((entry.reach, entry.cwd, entry.status), (
            roster.CannotReceiveMessages("no messaging socket"),
            str(self.home / "projects" / "resumed"), "waiting",
        ))
        self.assertTrue(roster.is_sender(entry, roster.ClaudeSession("sid-free")))
        self.assertFalse(roster.is_sender(entry, roster.ClaudeSession("sid-other")))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-free"}):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertTrue(any("session:sid-free (you)" in line for line in lines))

    def test_showrunner_socket_identity_beats_a_duplicate_display_name(self) -> None:
        self._write_session(
            "runner-imposter", "runner", "sid-imposter",
            self.home / "projects" / "imposter", updated=20,
        )
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            entries = roster.roster(os.environ, roster.ThisMachine()).entries
        real = next(
            entry for entry in entries
            if entry.reach == roster.Messageable("session:sid-runner")
        )
        imposter = next(
            entry for entry in entries
            if entry.reach == roster.Messageable("session:sid-imposter")
        )
        self.assertEqual((real.role, imposter.role), ("showrunner", "freestanding"))

    def test_duplicate_display_names_keep_each_hierarchy_on_its_identity(self) -> None:
        runner_a = self._entry(
            "same-runner", "showrunner", "session:runner-a", cwd="/runner-a"
        )
        runner_b = self._entry(
            "same-runner", "showrunner", "session:runner-b", cwd="/runner-b"
        )
        director_a = self._entry(
            "same-director", "unit director", "session:director-a",
            showrunner="same-runner", showrunner_address="session:runner-a", unit="unit-a",
        )
        director_b = self._entry(
            "same-director", "unit director", "session:director-b",
            showrunner="same-runner", showrunner_address="session:runner-b", unit="unit-b",
        )
        worker_a = self._entry(
            "worker-a", "worker", "worker-a", kind="codex",
            showrunner="same-runner", showrunner_address="session:runner-a",
            unit="unit-a", director="same-director", director_address="session:director-a",
        )
        worker_b = self._entry(
            "worker-b", "worker", "worker-b", kind="codex",
            showrunner="same-runner", showrunner_address="session:runner-b",
            unit="unit-b", director="same-director", director_address="session:director-b",
        )
        output, errors = io.StringIO(), io.StringIO()
        fixture = roster.Roster(
            [runner_a, runner_b, director_a, director_b, worker_a, worker_b], []
        )
        with mock.patch.object(roster, "roster", return_value=fixture), \
                redirect_stdout(output), redirect_stderr(errors):
            result = roster.main(["--here", "--kind", "codex"])
        lines = output.getvalue().splitlines()
        runner_a_line = next(line for line in lines if "/runner-a" in line)
        runner_b_line = next(line for line in lines if "/runner-b" in line)
        director_a_line = next(line for line in lines if "unit director" in line and "unit-a" in line)
        director_b_line = next(line for line in lines if "unit director" in line and "unit-b" in line)
        worker_a_line = next(line for line in lines if "[worker-a]" in line)
        worker_b_line = next(line for line in lines if "[worker-b]" in line)
        self.assertEqual((result, errors.getvalue()), (0, ""))
        self.assertLess(lines.index(runner_a_line), lines.index(director_a_line))
        self.assertLess(lines.index(director_a_line), lines.index(worker_a_line))
        self.assertLess(lines.index(runner_b_line), lines.index(director_b_line))
        self.assertLess(lines.index(director_b_line), lines.index(worker_b_line))
        self.assertEqual(sum("[session:director-" in line for line in lines), 2)
        self.assertEqual(sum("[worker-" in line for line in lines), 2)

    def test_review_status_and_pid_find_the_descendant_codex_reviewer(self) -> None:
        with mock.patch.object(os, "readlink", return_value="/review/cwd"):
            entries = self._entries()
        reviewer = entries["adversary reviewer"]
        self.assertEqual(
            (reviewer.kind, reviewer.role, reviewer.assignment, reviewer.reach, reviewer.cwd),
            (
                "codex", "worker",
                roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "adversary"),
                roster.CannotReceiveMessages("blind reviewer"), "/review/cwd",
            ),
        )
        blind_reviewer = entries["blind reviewer"]
        self.assertEqual(
            (blind_reviewer.assignment, blind_reviewer.reach),
            (
                roster.WorkerAssignment("review", str(self.director_run), "review", "review"),
                roster.CannotReceiveMessages("blind reviewer"),
            ),
        )
        self.assertNotIn("contract reviewer", entries)

    def test_review_status_in_a_phase_directory_finds_its_reviewer(self) -> None:
        phase = self.director_run / "phases" / "4"
        phase.mkdir(parents=True)
        for name in ("review_status_adversary", "review_pid_adversary"):
            _ = (self.director_run / name).rename(phase / name)
        with mock.patch.object(os, "readlink", return_value="/review/cwd"):
            entries = self._entries()
        reviewer = entries["adversary reviewer"]
        self.assertEqual(
            (reviewer.kind, reviewer.assignment),
            ("codex", roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "adversary")),
        )

    def test_review_markers_without_a_process_add_no_rows_or_unattributed_sessions(self) -> None:
        _ = self.account_note(2)
        processes = self.ps_state.read_text()
        _ = self.ps_state.write_text("")
        statuses = [self.director_run / "review_status", self.director_run / "review_status_adversary"]
        for status in statuses:
            _ = status.write_text("reviewed\n")
        before = self._entries()

        for status in statuses:
            _ = status.write_text("reviewing\n")
        reviewing = self._entries()
        self.assertEqual(set(reviewing), set(before))
        self.assertEqual(
            {name for name, entry in reviewing.items() if isinstance(entry.account, roster.AccountUnread)},
            {name for name, entry in before.items() if isinstance(entry.account, roster.AccountUnread)},
        )
        self.assertNotIn("blind reviewer", reviewing)
        self.assertNotIn("adversary reviewer", reviewing)
        self.assertIn("alpha-research", reviewing)

        _ = self.ps_state.write_text(processes)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()):
            running = self._entries()
        for name in ("blind reviewer", "adversary reviewer"):
            with self.subTest(reviewer=name):
                self.assertEqual(running[name].status, "running")
                self.assertEqual(running[name].account, roster.AccountLabel("codex2"))

    def test_live_run_workers_survive_server_exit_but_ended_workers_and_dead_runs_do_not(self) -> None:
        for liveness in ("active marker", "fresh heartbeat", "dead run"):
            with self.subTest(liveness=liveness):
                run = self.runs / liveness.replace(" ", "-")
                records: dict[str, dict[str, object]] = {
                    f"{run.name}-{status}": {
                        "thread_id": f"{run.name}-{status}-thread",
                        "status": status,
                        "launcher_pid": 999_999_999,
                    }
                    for status in ("running", "done", "failed", "ended")
                }
                self._write_mesh(run, records)
                if liveness == "active marker":
                    _ = (self.runs / "active" / "sid-director").write_text(str(run) + "\n")
                else:
                    heartbeat = run / "heartbeat.log"
                    heartbeat.touch()
                    if liveness == "dead run":
                        old = time.time() - unit_runs.LIVE_HEARTBEAT_SECS - 5
                        os.utime(heartbeat, (old, old))
                before = self._entries()
                self._write_mesh(run, records, server_pid=999_999_999)
                after = self._entries()
                expected: set[str] = set(records) - {f"{run.name}-ended"} if liveness != "dead run" else set()
                self.assertEqual(set(before) & set(records), expected)
                self.assertEqual(set(after) & set(records), expected)
                for name in expected:
                    self.assertEqual(after[name], before[name])

    def test_kind_codex_keeps_only_needed_claude_context(self) -> None:
        result, lines, errors = self._run_main("--kind", "codex")
        self.assertEqual((result, errors), (0, []))
        runner = next(line for line in lines if "[session:sid-runner]" in line)
        director = next(line for line in lines if "[session:sid-director]" in line)
        self.assertIn("(context)", runner)
        self.assertIn("(context)", director)
        self.assertEqual(runner.count("(context)"), 1)
        self.assertEqual(director.count("(context)"), 1)
        self.assertTrue(any("[worker:director-run/alpha-impl]" in line for line in lines))
        self.assertFalse(any("claude-worker" in line for line in lines))
        self.assertFalse(any("[session:sid-free]" in line for line in lines))
        self.assertFalse(any("solo-director" in line for line in lines))

    def test_json_has_all_public_fields_and_plain_problem_records(self) -> None:
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        self.assertEqual(document["problems"], [])
        rows = cast(list[dict[str, object]], document["entries"])
        self.assertTrue(rows)
        self.assertTrue(all(set(row) == ENTRY_FIELDS for row in rows))
        self.assertTrue(all(
            all(isinstance(value, str) for key, value in row.items() if key not in {"thread", "job"})
            for row in rows
        ))
        self.assertTrue(all(isinstance(row["job"], dict) for row in rows))
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["adversary reviewer"]["address"], "")
        for field in (
            "production", "showrunner", "showrunner_address", "unit", "director",
            "director_address", "slot_role", "session_dir",
        ):
            self.assertEqual(by_name["free-claude"][field], "")

        entry = roster.RosterEntry(
            "", "", "freestanding", "", "", roster.Messageable(""), "",
            roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker(),
        )
        with self.assertRaises(FrozenInstanceError):
            entry.name = "changed"  # pyright: ignore[reportAttributeAccessIssue]
        result_value = roster.Roster([], [])
        with self.assertRaises(FrozenInstanceError):
            result_value.entries = []  # pyright: ignore[reportAttributeAccessIssue]

    def test_worker_and_daemon_threads_are_domain_states_and_json_ids(self) -> None:
        entries = self._entries()
        self.assertEqual(entries["alpha-impl"].thread, roster.CodexThread("thread-shared"))
        self.assertEqual(entries["Codex window"].thread, roster.CodexThread("thread-window"))
        for name in ("runner", "claude-worker", "alpha-no-thread"):
            self.assertEqual(entries[name].thread, roster.NoCodexThread())
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["alpha-impl"]["thread"], "thread-shared")
        self.assertEqual(by_name["Codex window"]["thread"], "thread-window")
        for name in ("runner", "claude-worker", "alpha-no-thread"):
            self.assertIsNone(by_name[name]["thread"])

    def test_codex_worker_identity_marks_the_worker_and_not_its_inherited_director(self) -> None:
        with mock.patch.dict(os.environ, {
            "CODEX_THREAD_ID": "thread-shared", "CLAUDE_CODE_SESSION_ID": "sid-director",
        }):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(len([line for line in lines if "(you)" in line]), 1)
        self.assertTrue(any("[worker:director-run/alpha-impl] (you)" in line for line in lines))
        self.assertFalse(any("[session:sid-director] (you)" in line for line in lines))

    def test_daemon_codex_identity_marks_its_own_window(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-window"}):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(len([line for line in lines if "(you)" in line]), 1)
        self.assertTrue(any("codex:thread-window (you)" in line for line in lines))

    def test_plain_shell_marks_no_session(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertFalse(any("(you)" in line for line in lines))

    def test_sender_identity_has_frozen_named_states_and_codex_precedence(self) -> None:
        self.assertEqual(roster.sender_identity({}), roster.NoSession())
        self.assertEqual(roster.sender_identity({"CLAUDE_CODE_SESSION_ID": "claude"}),
                         roster.ClaudeSession("claude"))
        identity = roster.sender_identity({
            "CODEX_THREAD_ID": "codex", "CLAUDE_CODE_SESSION_ID": "inherited",
        })
        self.assertEqual(identity, roster.CodexThread("codex"))
        assert isinstance(identity, roster.CodexThread)
        with self.assertRaises(FrozenInstanceError):
            identity.thread_id = "changed"  # pyright: ignore[reportAttributeAccessIssue]

    def test_marked_unit_without_live_claude_does_not_claim_other_panes_by_worktree(self) -> None:
        (self.sessions / "director.json").unlink()
        worktree = self.home / "projects" / "unit-one"
        for name, pane in (("other-pane", "helper:@2.%2"), ("outside-herdr", "")):
            self._write_session(name, name, f"sid-{name}", worktree, tmux=pane)
        entries = self._entries()
        for name in ("other-pane", "outside-herdr"):
            with self.subTest(session=name):
                self.assertEqual(entries[name].role, "freestanding")
                self.assertEqual(entries[name].director, roster.NoDirector())
                self.assertTrue(roster.is_top_level(entries[name]))
        self.assertEqual(self.ssh_calls(), [])

    def test_unit_without_marked_session_does_not_claim_a_worktree_session(self) -> None:
        _ = (self.root / "herdr-proc/991100/environ").write_bytes(b"HERDR_PANE_ID=w1:p1\0")
        worktree = self.home / "projects" / "unit-one"
        worktree.mkdir(parents=True)
        _ = (worktree / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
        self._write_session(
            "helper", "helper", "sid-helper", worktree,
            tmux="unmarked:@2.%2",
        )
        helper = self._entries()["helper"]
        self.assertEqual(helper.role, "freestanding")
        self.assertEqual(helper.director, roster.NoDirector())
        self.assertTrue(roster.is_top_level(helper))
        self.assertEqual(self.ssh_calls(), [])

    def test_a_duplicate_mark_on_another_unit_keeps_this_units_director(self) -> None:
        read = unit_lookup.every_marked_unit

        def with_duplicate() -> unit_lookup.MarkedProductions:
            found = read()
            return unit_lookup.MarkedProductions(
                found.units, {"alpha": {"unit-two": "unit-two: its mark is on herdr sessions copy and first"}})

        with mock.patch.object(unit_lookup, "every_marked_unit", side_effect=with_duplicate), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(found.problems, [
            "unit hosts left out under alpha: unit-two: its mark is on herdr sessions copy and first"])
        director = next(entry for entry in found.entries if entry.name == "director")
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))

    def test_socketless_director_in_marked_pane_keeps_unit_without_worktree_match(self) -> None:
        self._write_session(
            "director", "director", "sid-director", self.home / "projects" / "elsewhere",
            tmux="old-label:@1.%1", with_socket=False,
        )
        entries = self._entries()
        director = entries["director"]
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))
        self.assertEqual(director.showrunner, roster.UnderShowrunner(
            "alpha", "runner", "session:sid-runner",
        ))
        for name in ("claude-worker", "alpha-impl", "adversary reviewer"):
            with self.subTest(worker=name):
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "director", "session:sid-director",
                ))
        self.assertEqual(self.ssh_calls(), [])

    def _herdr_fixture(self, responses: dict[str, CommandResponse]) -> str:
        path = self.root / "herdr-responses.json"
        _ = path.write_text(json.dumps(responses))
        os.environ["FAKE_HERDR_RESPONSES"] = str(path)
        os.environ["FAKE_HERDR_LOG"] = str(self.root / "herdr.jsonl")
        return str(SCRIPTS / "herdr/fake_herdr.py")

    def _herdr_process(self, pid: int, parent: int, argv: list[str], **environment: str) -> None:
        path = Path(os.environ["HERDR_PROC"]) / str(pid)
        path.mkdir(parents=True)
        _ = (path / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
        _ = (path / "cmdline").write_bytes("\0".join(argv).encode())
        _ = (path / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in environment.items()).encode())
        (path / "cwd").symlink_to(self.home)
        if argv[:2] == ["herdr", "--session"] and argv[3:] == ["server"]:
            herdr_listener(path.parent, pid, argv[2], os.environ)

    def _herdr_sessions(self) -> dict[str, CommandResponse]:
        return {"--session default session list --json": {"code": 0, "stdout": json.dumps({
            "sessions": [{"name": "native", "running": True, "default": False,
                          "session_dir": "/fixture", "socket_path": "/fixture/socket"}]
        })}, "--session native agent list": {"code": 0, "stdout": '{"ok": true, "result": {"agents": []}}'}}

    def test_socketless_herdr_director_is_placed_by_pid(self) -> None:
        # A socketless live record is deliberately excluded from _claudes; the
        # roster must join it by place rather than worktree ownership.
        (self.sessions / "director.json").unlink()
        self._write_session("director", "director", "sid-director", self.home / "elsewhere", with_socket=False, pid=991001, proc_start="fixture")
        _ = (self.root / "herdr-proc/991100/environ").write_bytes(b"HERDR_PANE_ID=w1:p2\0")
        self._herdr_process(991000, 0, ["herdr", "--session", "native", "server"])
        self._herdr_process(991001, 991000, ["claude"], HERDR_PANE_ID="w1:p1",
                            SHOWRUNNER_UNIT="alpha", SHOWRUNNER_UNIT_ID="unit-one")
        binary = self._herdr_fixture(self._herdr_sessions())
        running_session = sessions.running_session

        def running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991001 or running_session(record)

        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                mock.patch.object(sessions, "running_session", side_effect=running):
            director = self._entries()["director"]
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))
        self.assertEqual(director.showrunner, roster.UnderShowrunner("alpha", "runner", "session:sid-runner"))
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))

    def test_interactive_codex_panes_are_appended_without_messaging_addresses(self) -> None:
        old, current = self.account_note(1), self.account_note(2)
        responses = self._herdr_sessions()
        responses["--session native agent list"]["stdout"] = json.dumps({"ok": True, "result": {"agents": [
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "working"},
            {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
            {"pane_id": "w1:p3", "agent": "codex", "agent_status": "idle"},
            {"pane_id": "w1:p4", "agent_status": "idle"},
        ]}})
        self._herdr_process(991000, 0, ["herdr", "--session", "native", "server"])
        self._herdr_process(991001, 991000, ["codex"], HERDR_PANE_ID="w1:p1")
        self._herdr_process(991002, 991000, ["zsh"], HERDR_PANE_ID="w1:p1")
        self._herdr_process(991003, 991000, ["zsh"], HERDR_PANE_ID="w1:p3")
        def process_home(pid: int) -> account.ProcessCodexHome:
            return account.ProcessCodexHome(old if pid == 991001 else current)
        binary = self._herdr_fixture(responses)
        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
             mock.patch.object(account, "process_codex_home", side_effect=process_home) as read_home:
            entries = self._entries()
        pane = entries["herdr native:w1:p1"]
        self.assertEqual((pane.kind, pane.role, pane.status, pane.cwd), ("codex", "freestanding", "working", str(self.home)))
        self.assertEqual(pane.reach, roster.CannotReceiveMessages("interactive Codex in a herdr pane"))
        self.assertEqual((pane.showrunner, pane.director, pane.assignment),
                         (roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker()))
        self.assertEqual(pane.account, roster.AccountLabel("codex1"))
        self.assertIsInstance(entries["herdr native:w1:p3"].account, roster.AccountUnread)
        self.assertIn(mock.call(991001), read_home.call_args_list)
        self.assertNotIn(mock.call(991002), read_home.call_args_list)
        self.assertNotIn(mock.call(991003), read_home.call_args_list)
        self.assertEqual(entries["herdr native:w1:p3"].cwd, str(self.home))
        self.assertNotIn("herdr native:w1:p2", entries)

    def test_a_herdr_failure_is_a_problem_and_keeps_the_other_roster_rows(self) -> None:
        binary = self._herdr_fixture({"default": {"code": 1, "stdout": json.dumps({
            "error": {"code": "closed", "message": "fixture herdr unavailable"}
        })}})
        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertTrue(any(entry.name == "director" for entry in found.entries))
        self.assertEqual(len(found.problems), 2)
        self.assertTrue(any("unit hosts for alpha" in problem for problem in found.problems))
        self.assertTrue(any("herdr sessions" in problem for problem in found.problems))
        self.assertTrue(all("fixture herdr unavailable" in problem for problem in found.problems))

    def test_an_unreadable_herdr_session_keeps_codex_rows_from_other_sessions(self) -> None:
        responses = self._herdr_sessions()
        sessions_response = {"sessions": [herdr.SessionEntry(name=name, running=True, default=False,
                            session_dir="/fixture", socket_path="/fixture/socket") for name in ("broken", "native")]}
        responses["--session default session list --json"]["stdout"] = json.dumps(sessions_response)
        responses["--session broken agent list"] = {"code": 1, "stdout": "", "stderr": "fixture broken"}
        responses["--session native agent list"]["stdout"] = json.dumps({"ok": True, "result": {"agents": [
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle"}
        ]}})
        binary = self._herdr_fixture(responses)
        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual([entry.name for entry in found.entries if entry.name.startswith("herdr ")], ["herdr native:w1:p1"])
        self.assertEqual(len(found.problems), 1)
        self.assertIn("herdr session broken", found.problems[0])


    def test_unreadable_herdr_fallback_places_director_but_never_ledger_worker_by_cwd(self) -> None:
        worker_path = self.sessions / "worker.json"
        worker = cast(dict[str, object], json.loads(worker_path.read_text(encoding="utf-8")))
        worker["cwd"] = str(self.home / "projects" / "unit-one")
        _ = worker_path.write_text(json.dumps(worker), encoding="utf-8")
        with mock.patch.object(herdr, "herdr_binary", side_effect=herdr.HerdrMissing("herdr is not installed")), mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        director = next(entry for entry in found.entries if entry.name == "director")
        worker_entry = next(entry for entry in found.entries if entry.name == "claude-worker")
        self.assertEqual(
            (director.role, director.showrunner),
            (
                "unit director",
                roster.UnderShowrunner("alpha", "runner", "session:sid-runner"),
            ),
        )
        self.assertEqual(
            (worker_entry.role, worker_entry.director),
            (
                "worker",
                roster.UnderDirector("unit-one", "director", "session:sid-director"),
            ),
        )
        self.assertTrue(any("unit hosts for alpha were not read" in problem for problem in found.problems))

    def test_unreadable_daemon_is_one_problem_and_main_returns_one(self) -> None:
        result, lines, errors = self._run_main(
            daemon=codex_daemon.DaemonUnreadable("daemon closed mid-call"),
        )
        self.assertEqual(result, 1)
        self.assertTrue(lines)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("could not read: "))
        self.assertIn("daemon closed mid-call", errors[0])

    def test_stalled_accounts_share_one_deadline_and_keep_the_healthy_daemons_rows(self) -> None:
        read_times: list[float] = []
        read_daemons = codex_daemon.read_daemons

        def timed_read_daemons(environment: Mapping[str, str]) -> list[codex_daemon.DaemonRead]:
            started = time.monotonic()
            found = read_daemons(environment)
            read_times.append(time.monotonic() - started)
            return found

        def stalled(connection: socket.socket) -> None:
            self.assertEqual(receive_frame(connection)["method"], "initialize")
            connection.settimeout(2)
            self.assertEqual(connection.recv(1), b"")

        def healthy(connection: socket.socket) -> None:
            request = receive_frame(connection)
            self.assertEqual(request["method"], "initialize")
            reply(connection, request, {})
            self.assertEqual(receive_frame(connection)["method"], "initialized")
            request = receive_frame(connection)
            self.assertEqual(request["method"], "thread/loaded/list")
            reply(connection, request, {"data": ["healthy-thread"]})
            request = receive_frame(connection)
            self.assertEqual(request["method"], "thread/read")
            reply(connection, request, {"thread": {
                "id": "healthy-thread", "name": "Healthy account", "cwd": str(self.root),
                "status": {"type": "idle"},
            }})

        with short_socket_directory() as directory, ExitStack() as daemons:
            home = Path(directory)
            notes = home / "notes"
            homes = [home / ".codex", *(home / f".codex{number}" for number in range(1, 5))]
            for number, account_home in enumerate(homes):
                write_codex_account(account_home, notes, f"codex{number + 1}")
                control = account_home / "app-server-control"
                control.mkdir()
                _ = daemons.enter_context(FakeDaemon(
                    control / "app-server-control.sock", healthy if account_home == homes[-1] else stalled,
                ))
            with mock.patch.dict(os.environ, {
                "HOME": str(home), "AGENT_NOTES_DIR": str(notes), "ROSTER_CODEX_SOCKET": "",
            }), mock.patch.object(codex_daemon, "TIMEOUT_SECONDS", 0.3), \
                    mock.patch.object(codex_daemon, "read_daemons", new=timed_read_daemons):
                found = roster.roster(os.environ, roster.ThisMachine())
            self.assertEqual(len(read_times), 1)
            elapsed = read_times[0]
            self.assertLess(elapsed, 0.9, "account timeouts must share one discovery budget")
            entries = [entry for entry in found.entries if entry.name == "Healthy account"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].thread, roster.CodexThread("healthy-thread"))
            self.assertEqual(entries[0].daemon, codex_daemon.CodexDaemon(
                homes[-1], homes[-1] / "app-server-control/app-server-control.sock"))
            self.assertEqual(entries[0].account, roster.AccountLabel("codex5"))
            self.assertEqual(found.problems, [
                f"Codex daemon ~/{account_home.name}: timed out after 0.3 s"
                for account_home in homes[:-1]
            ])

    def test_unreadable_process_table_is_a_problem_not_a_crash(self) -> None:
        failure = subprocess.CalledProcessError(2, ["ps"])
        with mock.patch.object(process_table, "snapshot", side_effect=failure), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertTrue(found.entries)
        self.assertEqual(len([problem for problem in found.problems if "process" in problem]), 1)

    def test_recent_heartbeat_is_live_but_an_old_one_is_not(self) -> None:
        orphan = self.runs / "orphan-run" / "heartbeat.log"
        old = time.time() - unit_runs.LIVE_HEARTBEAT_SECS - 5
        os.utime(orphan, (old, old))
        entries = self._entries()
        self.assertNotIn("orphan-impl", entries)


    def ssh_calls(self) -> list[list[str]]:
        if not self.ssh_log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.ssh_log.read_text().splitlines()]

    def peer_snapshot(self, result: roster.Roster, *, status: int = 0) -> None:
        os.environ["SSH_SNAPSHOT"] = roster.RosterSnapshot(result).encode()
        os.environ["SSH_STATUS"] = str(status)

    def account_note(self, number: int, *, tool: str = "codex") -> Path:
        notes = self.root / "agents"
        notes.mkdir(exist_ok=True)
        os.environ["AGENT_NOTES_DIR"] = str(notes)
        self.environment["AGENT_NOTES_DIR"] = str(notes)
        login = f"{tool}{number}@example.invalid"
        home = self.home / (f".codex{number}" if tool == "codex" and number != 2 else ".codex" if tool == "codex" else f".claude{number}")
        home.mkdir(exist_ok=True)
        if tool == "codex":
            write_codex_account(home, notes, f"{tool}{number}")
        else:
            _ = (notes / f"{tool}{number}.md").write_text(f"---\nlogin: {login}\n---\n")
            _ = (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        return home

    def test_roster_keeps_one_home_identity_per_pass_and_refreshes_after_cutover(self) -> None:
        _ = self.account_note(2, tool="claude")
        home_json = self.home / ".claude.json"
        _ = home_json.write_text(json.dumps({"oauthAccount": {"emailAddress": "claude2@example.invalid"}}))

        def cutover_during_process_read(_pid: int) -> account.NoAccountSetting:
            _ = home_json.write_text("{}")
            return account.NoAccountSetting()

        with (mock.patch.object(account, "process_config_dir", side_effect=cutover_during_process_read),
              mock.patch.object(account, "home_account_state", wraps=account.home_account_state) as home_reads):
            before = self._entries()
            after = self._entries()
        for name in ("runner", "free-claude"):
            self.assertEqual(before[name].account, roster.AccountLabel("claude2"))
            self.assertIsInstance(after[name].account, roster.AccountUnread)
        self.assertEqual(home_reads.call_count, 2)

    def test_claude_roster_account_comes_from_its_process_configuration(self) -> None:
        directory = self.account_note(3, tool="claude")
        with mock.patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(directory)):
            entry = self._entries()["runner"]
        self.assertEqual(entry.account, roster.AccountLabel("claude3"))

    def test_working_workers_keep_their_serving_server_account_across_a_swap(self) -> None:
        old, current = self.account_note(1), self.account_note(2)
        _ = self.enterContext(mock.patch.object(agent_accounts, "workers_home", return_value=current))
        _ = (self.director_run / "mesh_server.json").write_text(json.dumps({
            "port": 45678, "pid": os.getpid(), "codex_home": str(current),
        }))
        _ = (self.director_run / "mesh_retired.json").write_text(json.dumps({"servers": [{
            "port": 45677, "pid": os.getpid(), "codex_home": str(old),
        }]}))
        _ = (self.director_run / "mesh_roster.json").write_text(json.dumps({
            "old-impl": {"thread_id": "old-thread", "status": "running", "port": 45677, "launcher_pid": os.getpid(), "role": "impl"},
            "new-test": {"thread_id": "new-thread", "status": "running", "port": 45678, "launcher_pid": os.getpid(), "role": "test"},
            "between-turns": {"thread_id": "waiting-thread", "status": "done", "launcher_pid": os.getpid()},
            "missing-server": {"thread_id": "lost-thread", "status": "running", "port": 49999, "launcher_pid": os.getpid()},
        }))
        entries = self._entries()
        before_move = {name: entries[name] for name in ("old-impl", "new-test")}
        self.assertEqual(entries["old-impl"].account, roster.AccountLabel("codex1"))

        self.assertEqual(entries["new-test"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["between-turns"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["missing-server"].account, roster.AccountLabel("codex2"))
        _ = (self.director_run / "mesh_server.json").write_text(json.dumps({"port": 45678, "pid": os.getpid()}))
        entries = self._entries()
        self.assertEqual(entries["new-test"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["between-turns"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["old-impl"].account, roster.AccountLabel("codex1"))

        for name, role, thread in (("old-impl", "impl", "old-thread"), ("new-test", "test", "new-thread")):
            with self.subTest(name=name):
                self.assertEqual(entries[name].role, "worker")
                self.assertEqual(entries[name].assignment, before_move[name].assignment)
                self.assertIsInstance(entries[name].assignment, roster.WorkerAssignment)
                assignment = entries[name].assignment
                assert isinstance(assignment, roster.WorkerAssignment)
                self.assertEqual(assignment.role, role)
                self.assertEqual(entries[name].thread, roster.CodexThread(thread))
                self.assertEqual(entries[name].reach, before_move[name].reach)

    def test_live_workers_keep_their_account_when_idle_stop_deletes_server_metadata(self) -> None:
        home = self.account_note(2)
        _ = (self.director_run / "mesh_server.json").write_text(json.dumps({
            "port": 45678, "pid": os.getpid(), "codex_home": str(home),
        }))
        _ = (self.director_run / "mesh_roster.json").write_text(json.dumps({
            "idle-worker": {"thread_id": "idle-thread", "status": "done", "port": 45678},
            "legacy-worker": {"thread_id": "legacy-thread", "status": "done"},
        }))
        with mock.patch.object(agent_accounts, "workers_home", return_value=home):
            before = self._entries()
            (self.director_run / "mesh_server.json").unlink()
            after = self._entries()
        for name in ("idle-worker", "legacy-worker"):
            with self.subTest(worker=name):
                self.assertEqual(before[name].account, roster.AccountLabel("codex2"))
                self.assertEqual(after[name], before[name])

    def test_daemon_rows_use_the_home_holding_the_socket(self) -> None:
        home = self.account_note(1)
        _ = self.account_note(2)
        path = home / "app-server-control" / "daemon.sock"
        with mock.patch.dict(os.environ, {"ROSTER_CODEX_SOCKET": str(path)}):
            entry = self._entries()["Codex window"]
        self.assertEqual(entry.account, roster.AccountLabel("codex1"))
        self.assertEqual(entry.daemon, codex_daemon.CodexDaemon(home, path))

    def test_reviewers_require_a_readable_environment_before_using_the_default_home(self) -> None:
        _ = self.account_note(1)
        _ = self.account_note(2)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()):
            entries = self._entries()
        self.assertEqual(entries["adversary reviewer"].account, roster.AccountLabel("codex2"))
        with mock.patch.object(process_table, "snapshot", return_value={}):
            missing = self._entries()
        self.assertNotIn("blind reviewer", missing)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessEnvironmentUnread("access denied")):
            entries = self._entries()
        self.assertIsInstance(entries["adversary reviewer"].account, roster.AccountUnread)
        assert isinstance(entries["adversary reviewer"].account, roster.AccountUnread)
        self.assertIn("access denied", entries["adversary reviewer"].account.reason)

    def test_snapshot_without_account_decodes_as_peer_attribution_unavailable(self) -> None:
        row = replace(self._entry("Mac director", "unit director", "session:mac-id"),
                      machine="mac", account=roster.AccountLabel("claude3"))
        encoded = roster.RosterSnapshot(roster.Roster([row], [])).encode()
        self.assertEqual(roster.RosterSnapshot.decode(encoded).result.entries[0].account,
                         roster.AccountLabel("claude3"))
        old = cast(dict[str, object], json.loads(encoded))
        entries = cast(list[dict[str, object]], old["entries"])
        del entries[0]["account"]
        decoded = roster.RosterSnapshot.decode(json.dumps(old)).result.entries[0]
        self.assertEqual(decoded.name, "Mac director")
        self.assertIsInstance(decoded.account, roster.AccountAttributionUnavailable)

    def test_peer_snapshot_timeout_keeps_the_machine_and_failure_reason(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 20)):
            peer = roster.peer_snapshot(os.environ)
        self.assertIsInstance(peer, roster.PeerNotReached)
        assert isinstance(peer, roster.PeerNotReached)
        self.assertTrue(peer.host)
        self.assertIn("timed out", peer.reason)

    def test_here_reads_only_this_machine_without_ssh(self) -> None:
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        self.assertTrue(lines)
        self.assertEqual(self.ssh_calls(), [])

    def test_default_scope_merges_peer_rows_after_local_rows_with_one_snapshot_call(self) -> None:
        peer = replace(self._entry("Mac Claude", "freestanding", "session:mac-id"), machine="mac")
        self.peer_snapshot(roster.Roster([peer], []))
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            result = roster.roster(os.environ, roster.AllMachines())
        self.assertEqual(result.problems, [])
        self.assertEqual(result.entries[-1], peer)
        self.assertTrue(all(row.machine != "mac" for row in result.entries[:-1]))
        commands = self.ssh_calls()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:4], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        self.assertIn("--here --snapshot", commands[0][-1])
        self.assertIn('"$HOME/.claude/scripts/lib/py"', commands[0][-1])

    def test_unreachable_peer_is_one_problem_while_local_rows_still_print(self) -> None:
        os.environ["SSH_SNAPSHOT"] = "ssh: connection refused"
        os.environ["SSH_STATUS"] = "255"
        result, lines, errors = self._run_main(here=False)
        self.assertEqual(result, 1)
        self.assertTrue(any("session:sid-free" in line for line in lines))
        self.assertEqual(len(errors), 1)
        self.assertIn("not reached", errors[0])
        self.assertEqual(len(self.ssh_calls()), 1)

    def test_peer_snapshot_with_problems_and_exit_one_preserves_rows_and_prefixes_problem(self) -> None:
        peer = replace(self._entry("Mac Claude", "freestanding", "session:mac-id"), machine="mac")
        self.peer_snapshot(roster.Roster([peer], ["Codex daemon: closed"]), status=1)
        result, lines, errors = self._run_main("--json", here=False)
        self.assertEqual(result, 1)
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        self.assertTrue(any(row["name"] == "Mac Claude" for row in rows))
        self.assertEqual(document["problems"], ["mac: Codex daemon: closed"])
        self.assertEqual(len(errors), 1)
        self.assertIn("mac: Codex daemon: closed", errors[0])

    def test_snapshot_round_trips_every_relationship_reach_thread_and_session_state(self) -> None:
        base = self._entry("base", "freestanding", "session:base")
        rows = [
            replace(base, showrunner=roster.RunsProduction("show")),
            replace(base, showrunner=roster.UnderShowrunner("show", "runner", "session:runner"),
                    director=roster.DirectsUnit("unit"), reach=roster.CannotReceiveMessages("no messaging socket")),
            replace(base, kind="codex", role="worker", session=roster.NoClaudeSession(),
                    thread=roster.CodexThread("worker-thread"), reach=roster.Messageable("worker:run/impl"),
                    showrunner=roster.ProductionWithoutRunningShowrunner("gone"),
                    director=roster.UnderDirector("unit", "base", "session:base"),
                    assignment=roster.WorkerAssignment("impl", "/remote/run")),
            replace(base, kind="codex", session=roster.NoClaudeSession(), thread=roster.CodexThread("daemon"),
                    reach=roster.Messageable("codex:daemon"),
                    daemon=codex_daemon.CodexDaemon(Path("/remote/.codex2"), Path("/remote/.codex2/socket"))),
            replace(base, kind="codex", role="worker", session=roster.NoClaudeSession(),
                    reach=roster.CannotReceiveMessages("between turns; no unit director is running"),
                    assignment=roster.WorkerAssignment("test", "/remote/no-thread")),
        ]
        result = roster.Roster(rows, ["remote source problem"])
        encoded = roster.RosterSnapshot(result).encode()
        decoded = roster.RosterSnapshot.decode(encoded)
        self.assertEqual(decoded.result, result)

    def test_snapshot_command_retains_socketless_director_identity_and_linked_workers(self) -> None:
        path = self.sessions / "director.json"
        data = cast(dict[str, object], json.loads(path.read_text()))
        data["messagingSocketPath"] = ""
        data["procStart"] = sessions.process_start(os.getpid())
        _ = path.write_text(json.dumps(data))
        result, lines, errors = self._run_main("--snapshot")
        self.assertEqual((result, errors), (0, []))
        snapshot = roster.RosterSnapshot.decode("\n".join(lines))
        director = next(row for row in snapshot.result.entries if row.name == "director")
        worker = next(row for row in snapshot.result.entries if row.name == "alpha-impl")
        self.assertEqual(director.session, roster.ClaudeSession("sid-director"))
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(worker.director, roster.UnderDirector("unit-one", "director", "session:sid-director"))
        self.assertEqual(self.ssh_calls(), [])

    def test_snapshot_is_hidden_from_help_and_public_json_has_no_session_field(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            _ = roster.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("--snapshot", output.getvalue())
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        for row in cast(list[dict[str, object]], document["entries"]):
            self.assertEqual(set(row), ENTRY_FIELDS)
            self.assertNotIn("session", row)

    def test_invalid_peer_snapshot_is_one_not_reached_problem(self) -> None:
        os.environ["SSH_SNAPSHOT"] = '{"entries": [], "problems": []}'
        result = roster.peer_roster(os.environ)
        self.assertEqual(result.entries, [])
        self.assertEqual(len(result.problems), 1)
        self.assertIn("not reached", result.problems[0])
        self.assertEqual(len(self.ssh_calls()), 1)

    def test_peer_legacy_label_decodes_to_the_numbered_account_note(self) -> None:
        row = replace(self._entry("Mac director", "unit director", "session:mac-id"),
                      machine="mac", account=roster.AccountLabel("claude 1"))
        decoded = roster.RosterSnapshot.decode(roster.RosterSnapshot(roster.Roster([row], [])).encode())
        self.assertEqual(decoded.result.entries[0].account, roster.AccountLabel("claude1"))

    def test_peer_snapshot_timeout_is_twenty_seconds_and_sender_ids_are_not_forwarded(self) -> None:
        calls: list[list[str]] = []
        def timeout(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            self.assertEqual(command[0], "ssh")
            self.assertEqual(options["timeout"], 20)
            # The caller's input is never handed to ssh, which would wait on it or consume it.
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            environment = cast(dict[str, str], options["env"])
            self.assertNotIn("CODEX_THREAD_ID", environment)
            self.assertNotIn("CLAUDE_CODE_SESSION_ID", environment)
            raise subprocess.TimeoutExpired(command, 20)
        with mock.patch.object(subprocess, "run", new=timeout):
            result = roster.peer_roster(os.environ)
        self.assertEqual(result.entries, [])
        self.assertEqual(len(result.problems), 1)
        self.assertIn("not reached: timed out after 20 s", result.problems[0])
        self.assertEqual(len(calls), 1)

    def test_mac_without_hosts_keeps_worktree_directors(self) -> None:
        with (mock.patch.object(sys, "platform", "darwin"),
              mock.patch.object(herdr, "herdr_binary", side_effect=herdr.HerdrMissing("herdr is not installed")),
              mock.patch.object(codex_daemon, "loaded_sessions", return_value=[])):
            result = roster.roster(os.environ, roster.ThisMachine())
        director = next(row for row in result.entries if row.name == "director")
        self.assertEqual(director.role, "unit director")
        self.assertTrue(any("unit hosts for alpha were not read" in problem for problem in result.problems))
        self.assertEqual(self.ssh_calls(), [])


class CodexHolderLabelsTests(unittest.TestCase):
    root: Path = Path()
    runs: Path = Path()
    registry: Path = Path()
    environment: dict[str, str] = {}
    thread: str = "01a1775e-0fcd-7842-a664-436a33f9007b"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runs = self.root / "runs"
        self.registry = self.root / "sessions"
        notifier = self.root / "notifier"
        for directory in (self.runs / "active", self.registry, notifier, self.root / "home"):
            directory.mkdir(parents=True)
        self.environment = {
            **os.environ, "HOME": str(self.root / "home"),
            "NOTIFIER_SESSIONS_DIR": str(self.registry), "NOTIFIER_STATE_DIR": str(notifier),
            "ROSTER_RUN_ROOT": str(self.runs), "HERDR_PROC": str(self.root / "proc"),
            "HERDR_BIN": str(SCRIPTS / "herdr/fake_herdr.py"),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "REVIEW_POOL_STATE": str(self.root / "review-pool"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
        }
        environment_patch = cast(AbstractContextManager[object], mock.patch.dict(os.environ, self.environment, clear=True))
        _ = self.enterContext(environment_patch)
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier))
        # Both registry records are live processes, with real start identities.
        # Only their declared unit marks and run metadata come from fixture files.
        for pid, sid, name in ((os.getppid(), "holder-director", "report-director"),
                               (os.getpid(), "holder-runner", "builds-runner")):
            start = sessions.process_start(pid)
            self.assertIsNotNone(start)
            _ = (self.registry / f"{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": sid, "name": name, "procStart": start,
                "messagingSocketPath": "", "updatedAt": 1, "cwd": str(self.root / name),
            }))
        self.process(991099, 0, ["herdr", "--session", "native", "server"])
        self.process(os.getppid(), 991099, ["claude"],
                     "HERDR_PANE_ID=w1:p1\0SHOWRUNNER_UNIT=builds\0SHOWRUNNER_UNIT_ID=report-unit\0")
        self.process(os.getpid(), 0, ["claude"])
        herdr_listener(self.root / "proc", 991099, "native", os.environ)
        _ = (self.root / "herdr.json").write_text(json.dumps({
            "--session default session list --json": {"code": 0, "stdout": json.dumps({
                "sessions": [{"name": "native", "running": True, "default": False,
                              "session_dir": "/fixture", "socket_path": "/fixture/socket"}],
            })},
            "--session native agent list": {"code": 0, "stdout": '{"ok": true, "result": {"agents": []}}'},
        }))
        doc = self.root / "production.md"
        _ = doc.write_text("## Units\n| Unit | Plan | Worktree | Branch | Port | Owns |\n"
                           + "| --- | --- | --- | --- | --- | --- |\n"
                           + f"| report-unit | plan.md | {self.root / 'report-director'} | report | — | — |\n")
        timer = notifier / "showrunner-builds"
        timer.mkdir()
        _ = (timer / "conf").write_text(f"TARGET=session:holder-runner\nCHECK=zsh /scripts/production_check.sh {doc}\n")
        run = self.runs / "run-00"
        self.mesh(run, {"report-test2": {"thread_id": self.thread, "status": "running"}})
        _ = (self.runs / "active/holder-director").write_text(str(run) + "\n")

    def process(self, pid: int, parent: int, argv: list[str], environment: str = "") -> None:
        directory = self.root / "proc" / str(pid)
        directory.mkdir(parents=True)
        _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
        _ = (directory / "cmdline").write_bytes("\0".join(argv).encode())
        _ = (directory / "environ").write_bytes(environment.encode())
        (directory / "cwd").symlink_to(self.root)

    def mesh(self, run: Path, workers: dict[str, dict[str, object]]) -> None:
        run.mkdir(parents=True, exist_ok=True)
        _ = (run / "mesh_roster.json").write_text(json.dumps(workers))

    def test_codex_thread_labels_equal_full_roster_without_mocking_readers(self) -> None:
        full = roster.unit_labels(roster.roster(self.environment, roster.ThisMachine()).entries)
        fast = roster.unit_labels_for([self.thread, "holder-director", "unknown-thread", "report-test2"], self.environment)
        self.assertEqual(fast, {sid: full[sid] for sid in (self.thread, "holder-director")})
        self.assertEqual(fast[self.thread], roster.UnitLabels("report-test2", "builds", "report"))
        self.assertEqual(fast["holder-director"], roster.UnitLabels("report-director", "builds", "report"))

    def test_twenty_run_lookup_is_well_inside_the_cli_budget(self) -> None:
        wanted = [self.thread]
        expected = {self.thread: roster.UnitLabels("report-test2", "builds", "report")}
        for number in range(1, 20):
            run = self.runs / f"run-{number:02}"
            thread = f"01a1775e-0fcd-7842-a664-{number:012}"
            name = f"report-test-{number}"
            self.mesh(run, {name: {"thread_id": thread, "status": "done"}})
            (run / "heartbeat.log").touch()
            _ = (run / "progress_history_state.json").write_text(json.dumps({"production": "builds", "unit": "report-unit"}))
            wanted.append(thread)
            expected[thread] = roster.UnitLabels(name, "builds", "report")
        started = time.perf_counter()
        labels = roster.unit_labels_for([*wanted, "unknown-thread"], self.environment)
        elapsed = time.perf_counter() - started
        self.assertEqual(labels, expected)
        self.assertLess(elapsed, 0.2, f"20-run label lookup took {elapsed:.3f} seconds")

    def test_retired_and_redirected_workers_and_stale_runs_are_not_holders(self) -> None:
        records: dict[str, dict[str, object]] = {
            "report-test2": {"thread_id": self.thread, "status": "done"},
            "ended": {"thread_id": "ended-thread", "status": "ended"},
            "moved": {"thread_id": "moved-thread", "role_redirect": {"name": "elsewhere"}},
            "moving": {"thread_id": "moving-thread", "transferring_role": {"name": "elsewhere"}},
            "held": {"thread_id": "held-thread", "held_design_id": "fixture-design"},
        }
        self.mesh(self.runs / "run-00", records)
        self.mesh(self.runs / "stale", {"stale": {"thread_id": "stale-thread", "status": "running"}})
        self.assertEqual(roster.unit_labels_for(
            [self.thread, "ended-thread", "moved-thread", "moving-thread", "held-thread", "stale-thread"],
            self.environment), {self.thread: roster.UnitLabels("report-test2", "builds", "report")})


class UnitLabelsTests(unittest.TestCase):
    def test_folder_name_joins_showrunner_and_unit(self) -> None:
        self.assertEqual(roster.UnitLabels("director", "hana", "lfo").folder_name(), "hana-lfo")
        self.assertEqual(roster.UnitLabels("director", "hana", "hanami").folder_name(), "hana-hanami")

    def test_folder_name_does_not_repeat_the_units_showrunner_prefix(self) -> None:
        for unit in ("cargo-liner-style", "cargo-liner-style-types"):
            with self.subTest(unit=unit):
                self.assertEqual(roster.UnitLabels("director", "cargo-liner-style", unit).folder_name(), unit)

    def test_folder_name_drops_a_missing_relationship(self) -> None:
        self.assertEqual(roster.UnitLabels("director", "builds", "—").folder_name(), "builds")
        self.assertEqual(roster.UnitLabels("director", "—", "lfo").folder_name(), "lfo")

    def test_folder_name_uses_the_session_when_both_relationships_are_missing(self) -> None:
        self.assertEqual(roster.UnitLabels("scratch").folder_name(), "scratch")

    def entry(
        self, session_id: str, name: str, *,
        showrunner: roster.ShowrunnerRelationship = NO_SHOWRUNNER,
        director: roster.DirectorRelationship = NO_DIRECTOR,
        codex: bool = False,
    ) -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="fixture", kind="codex" if codex else "claude", role="freestanding",
            name=name, status="running", reach=roster.CannotReceiveMessages("fixture"), cwd="/fixture",
            showrunner=showrunner, director=director, assignment=roster.NotAWorker(),
            session=roster.NoClaudeSession() if codex else roster.ClaudeSession(session_id),
            thread=roster.CodexThread(session_id) if codex else roster.NoCodexThread(),
        )

    def test_labels_use_production_and_unit_short_names_not_session_names(self) -> None:
        runner = roster.UnderShowrunner("builds", "builds-showrunner", "session:runner-id")
        entries = [
            self.entry("director-id", "report-director", showrunner=runner, director=roster.DirectsUnit("report-unit")),
            self.entry("worker-thread", "report-test2", showrunner=runner,
                       director=roster.UnderDirector("report-unit", "report-director", "session:director-id"),
                       codex=True),
            self.entry("runner-id", "builds-showrunner", showrunner=roster.RunsProduction("builds")),
            self.entry("unmatched-id", "scratch"),
            self.entry("offline-id", "orphan", showrunner=roster.ProductionWithoutRunningShowrunner("offline")),
        ]
        labels = roster.unit_labels(entries)
        self.assertEqual(labels, {
            "director-id": roster.UnitLabels("report-director", "builds", "report"),
            "worker-thread": roster.UnitLabels("report-test2", "builds", "report"),
            "runner-id": roster.UnitLabels("builds-showrunner", "builds", "—"),
            "unmatched-id": roster.UnitLabels("scratch", "—", "—"),
            "offline-id": roster.UnitLabels("orphan", "offline", "—"),
        })
        self.assertEqual(labels["director-id"].folder_name(), "builds-report")
        self.assertEqual(labels["worker-thread"].folder_name(), "builds-report")

    def test_labels_and_shown_name_agree_on_production_and_unit(self) -> None:
        entry = self.entry("director-id", "report-director",
                           showrunner=roster.UnderShowrunner("builds", "builds-showrunner", ""),
                           director=roster.DirectsUnit("report-unit"))
        shown = roster.shown_name(entry)
        labels = roster.unit_labels([entry])["director-id"]
        self.assertEqual(shown.production, roster.ProductionPart(labels.showrunner))
        self.assertIsInstance(shown.unit, roster.UnitPart)
        self.assertEqual(shown.render().split(" · ")[:2], [labels.showrunner, labels.unit])

    def test_style_unit_keeps_its_registered_showrunner_prefix(self) -> None:
        runner = roster.UnderShowrunner("cargo-liner-style", "cargo-liner", "session:liner")
        entries = [
            self.entry("style-director", "types-director", showrunner=runner,
                       director=roster.DirectsUnit("cargo-liner-style-types-unit")),
            self.entry("style-worker", "types-review", showrunner=runner,
                       director=roster.UnderDirector("cargo-liner-style-types-unit", "types-director",
                                                     "session:style-director"),
                       codex=True),
        ]
        labels = roster.unit_labels(entries)
        self.assertEqual(labels, {
            "style-director": roster.UnitLabels("types-director", "cargo-liner-style", "cargo-liner-style-types"),
            "style-worker": roster.UnitLabels("types-review", "cargo-liner-style", "cargo-liner-style-types"),
        })
        self.assertEqual(labels["style-worker"].folder_name(), "cargo-liner-style-types")

    def test_labels_collapse_whitespace_without_changing_the_registered_name(self) -> None:
        entry = self.entry("id", "report\n test2", showrunner=roster.UnderShowrunner(" builds\t", "runner", ""),
                           director=roster.UnderDirector(" report-unit\n", "director", ""), codex=True)
        self.assertEqual(roster.unit_labels([entry]), {"id": roster.UnitLabels("report test2", "builds", "report")})

    def test_fast_lookup_uses_only_fixture_sessions_registry_and_unit_marks(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doc = root / "production.md"
        _ = doc.write_text(
            "## Units\n| Unit | Plan | Worktree | Branch | Port | Owns |\n"
            + "| --- | --- | --- | --- | --- | --- |\n"
            + "| cargo-liner-style-types-unit | plan.md | /fixture/style | style | — | — |\n",
        )
        records: list[sessions.SessionRecord] = [
            {"pid": pid, "sessionId": sid, "name": name, "messagingSocketPath": f"/fixture/{sid}.sock",
             "updatedAt": 1, "tmux": "", "formerNames": []}
            for pid, sid, name in (
                (101, "runner-id", "cargo-liner"), (102, "style-id", "cargo-liner-style-types"),
                (103, "free-id", "scratch"), (104, "worker-id", "types-review"),
            )
        ]
        for record in records:
            process = root / "proc" / str(record["pid"])
            process.mkdir(parents=True)
            marks = {
                "style-id": "SHOWRUNNER_UNIT=cargo-liner-style\0SHOWRUNNER_UNIT_ID=cargo-liner-style-types-unit\0",
                "worker-id": "CLAUDE_CODE_SESSION_ID=style-id\0",
            }.get(record["sessionId"], "")
            _ = (process / "environ").write_text(marks)
        registered: list[showrunners.Showrunner] = [{
            "session": "cargo-liner", "socket": "/fixture/runner-id.sock", "slug": "cargo-liner-style",
            "zone": "America/New_York", "doc": str(doc),
        }]
        environment = {"HERDR_PROC": str(root / "proc"), "NOTIFIER_SESSIONS_DIR": str(root / "sessions")}
        notifier = root / "notifier"
        (notifier / "showrunner-cargo-liner-style").mkdir(parents=True)
        with mock.patch.object(unit_labels, "newest_sessions", return_value=records), \
             mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier), \
             mock.patch.object(unit_labels, "registered_showrunners", return_value=registered), \
             mock.patch.object(unit_labels, "live_runs", side_effect=AssertionError("no meshes")) as meshes, \
             mock.patch.object(unit_lookup, "every_marked_unit", side_effect=AssertionError("no mark scan")) as marks_read, \
             mock.patch.object(herdr, "herdr_places", side_effect=AssertionError("no process scan")) as places, \
             mock.patch.object(process_table, "snapshot", side_effect=AssertionError("no process table")) as processes, \
             mock.patch.object(account, "home_account_state", side_effect=AssertionError("no accounts")) as accounts, \
             mock.patch.object(codex_daemon, "loaded_sessions", side_effect=AssertionError("no daemon")) as daemon:
            labels = roster.unit_labels_for(["style-id", "runner-id", "free-id", "worker-id", "missing-id"],
                                            environment)
        self.assertEqual(labels, {
            "style-id": roster.UnitLabels("cargo-liner-style-types", "cargo-liner-style", "cargo-liner-style-types"),
            "worker-id": roster.UnitLabels("types-review", "cargo-liner-style", "cargo-liner-style-types"),
            "runner-id": roster.UnitLabels("cargo-liner", "cargo-liner-style", "—"),
            "free-id": roster.UnitLabels("scratch", "—", "—"),
        })
        meshes.assert_not_called()
        marks_read.assert_not_called()
        places.assert_not_called()
        processes.assert_not_called()
        accounts.assert_not_called()
        daemon.assert_not_called()

    def test_fast_lookup_returns_only_requested_session_ids(self) -> None:
        records: list[sessions.SessionRecord] = [
            {"pid": 101, "sessionId": "requested", "name": "scratch", "messagingSocketPath": "",
             "updatedAt": 1, "tmux": "", "formerNames": []},
            {"pid": 102, "sessionId": "other", "name": "unrequested", "messagingSocketPath": "",
             "updatedAt": 1, "tmux": "", "formerNames": []},
        ]
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.object(unit_labels, "newest_sessions", return_value=records), \
             mock.patch.object(unit_labels, "registered_showrunners", return_value=[]):
            labels = roster.unit_labels_for(["requested", "missing"], {"HERDR_PROC": str(root)})
        self.assertEqual(labels, {"requested": roster.UnitLabels("scratch", "—", "—")})


if __name__ == "__main__":
    _ = unittest.main()
