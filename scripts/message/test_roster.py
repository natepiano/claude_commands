#!/usr/bin/env python3
"""The machine roster joins Claude sessions, productions, runs, and Codex."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

from scripts.guilds import roles
from scripts.lib.socket_test_support import short_socket_directory

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(SCRIPTS / "production"))
sys.path.insert(0, str(SCRIPTS / "direct"))

import codex_daemon
import process_table
import remove_workers
import roster
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
    "director_address", "slot_role", "session_dir", "thread",
}
WORKER_SESSION_ID = "12345678-1234-5678-9abc-def012345678"


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
            "GUILD_ROLES_STATE": str(self.root / "guild-roles"),
            "GUILD_OWNERSHIP_STATE": str(self.root / "guild-ownership"),
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
        self, name: str, role: str, address: str, *, kind: str = "claude",
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
        design = roles.HeldDesign(roles.DesignPlacement("00000000-0000-4000-8000-000000000046", "fixture-host", 2),
            "fixture-repo", str(self.root / "checkout"), str(self.root / "checkout/design.md"), "design",
            "2026-10-21T12:00:00+00:00", roles.UnitHolder("alpha", "unit-one"), roles.DesignUnbound(),
            (roles.RoleInstance("designer", "lasting-designer-alias", roles.RoleOpen("lasting-thread")),
             roles.RoleInstance("reviewer", "lasting-reviewer-alias", roles.RoleNotOpened())))
        roles._save(design)  # pyright: ignore[reportPrivateUsage]
        mesh = roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True)
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"lasting-designer-alias": {
            "thread_id": "lasting-thread", "status": "done"}}))
        with mock.patch.object(roles, "resolve_holder", return_value=roles.HolderReach("sid-director")):
            found = self._entries()
        entry = found["lasting-designer-alias"]
        self.assertEqual(entry.thread, roster.CodexThread("lasting-thread"))
        self.assertEqual(entry.role, "astra-designer")
        self.assertEqual(entry.director, roster.UnderDirector("unit-one", "director", "session:sid-director"))
        self.assertEqual(entry.assignment, roster.WorkerAssignment("astra-designer", str(mesh)))
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
            "test", str(self.director_run),
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
            roster.WorkerAssignment("review (adversary)", str(self.director_run)),
        )
        self.assertEqual(
            entries["alpha-impl"].assignment,
            roster.WorkerAssignment("no role", str(self.director_run)),
        )
        self.assertEqual(
            entries["adversary reviewer"].assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run)),
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
        runner = next(line for line in lines if line.lstrip().startswith("showrunner runner"))
        director = next(line for line in lines if line.lstrip().startswith("unit director director"))
        worker = next(line for line in lines if line.lstrip().startswith("worker alpha-impl"))
        self.assertIn("(you)", runner)
        self.assertEqual(len(director) - len(director.lstrip()), len(runner) - len(runner.lstrip()) + 2)
        self.assertEqual(len(worker) - len(worker.lstrip()), len(director) - len(director.lstrip()) + 2)
        self.assertLess(lines.index(runner), lines.index(director))
        self.assertLess(lines.index(director), lines.index(worker))
        orphan_header = f"  director not running: {self.runs / 'orphan-run'}"
        self.assertIn(orphan_header, lines)
        orphan_worker = next(line for line in lines if line.lstrip().startswith("worker orphan-impl"))
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
                directors = [line for line in lines if line.lstrip().startswith("unit director director")]
                self.assertEqual(len(directors), 1)
                director = directors[0]
                workers = ("alpha-impl",) if arguments else ("alpha-impl", "claude-worker")
                for name in workers:
                    worker = next(line for line in lines if line.lstrip().startswith(f"worker {name}"))
                    self.assertEqual(len(worker) - len(worker.lstrip()),
                                     len(director) - len(director.lstrip()) + 2)
                    self.assertLess(lines.index(director), lines.index(worker))
                    self.assertEqual(sum(line.lstrip().startswith(f"worker {name}") for line in lines), 1)
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
                runners = [line for line in lines if line.lstrip().startswith("showrunner runner")]
                self.assertEqual(len(runners), 1)
                director = next(line for line in lines if line.lstrip().startswith("unit director director"))
                worker = next(line for line in lines if line.lstrip().startswith("worker alpha-impl"))
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
        self.assertEqual(worker.assignment, roster.WorkerAssignment("test", str(self.director_run)))
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
        self.assertEqual(late_worker.assignment, roster.WorkerAssignment("impl", str(self.director_run)))
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
                roster.WorkerAssignment("test", str(self.director_run)),
            ),
        )

    def test_codex_review_lens_uses_role_and_lens_fields(self) -> None:
        entry = self._entries()["alpha-research"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run)),
        )

    def test_codex_review_without_lens_uses_its_role(self) -> None:
        entry = self._entries()["alpha-review-plain"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review", str(self.director_run)),
        )

    def test_codex_worker_without_recorded_role_has_no_role(self) -> None:
        self.assertEqual(
            self._entries()["alpha-impl"].assignment,
            roster.WorkerAssignment("no role", str(self.director_run)),
        )

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
        self.assertTrue(any("newest-free (you)" in line for line in lines))

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
        worker_a_line = next(line for line in lines if "worker worker-a" in line)
        worker_b_line = next(line for line in lines if "worker worker-b" in line)
        self.assertEqual((result, errors.getvalue()), (0, ""))
        self.assertLess(lines.index(runner_a_line), lines.index(director_a_line))
        self.assertLess(lines.index(director_a_line), lines.index(worker_a_line))
        self.assertLess(lines.index(runner_b_line), lines.index(director_b_line))
        self.assertLess(lines.index(director_b_line), lines.index(worker_b_line))
        self.assertEqual(sum("unit director same-director" in line for line in lines), 2)
        self.assertEqual(sum("worker worker-" in line for line in lines), 2)

    def test_review_status_and_pid_find_the_descendant_codex_reviewer(self) -> None:
        with mock.patch.object(os, "readlink", return_value="/review/cwd"):
            entries = self._entries()
        reviewer = entries["adversary reviewer"]
        self.assertEqual(
            (reviewer.kind, reviewer.role, reviewer.assignment, reviewer.reach, reviewer.cwd),
            (
                "codex", "worker",
                roster.WorkerAssignment("review (adversary)", str(self.director_run)),
                roster.CannotReceiveMessages("blind reviewer"), "/review/cwd",
            ),
        )
        blind_reviewer = entries["blind reviewer"]
        self.assertEqual(
            (blind_reviewer.assignment, blind_reviewer.reach),
            (
                roster.WorkerAssignment("review", str(self.director_run)),
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
            ("codex", roster.WorkerAssignment("review (adversary)", str(self.director_run))),
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
        runner = next(line for line in lines if line.lstrip().startswith("showrunner runner"))
        director = next(line for line in lines if line.lstrip().startswith("unit director director"))
        self.assertIn("(context)", runner)
        self.assertIn("(context)", director)
        self.assertEqual(runner.count("(context)"), 1)
        self.assertEqual(director.count("(context)"), 1)
        self.assertTrue(any("worker alpha-impl" in line for line in lines))
        self.assertFalse(any("claude-worker" in line for line in lines))
        self.assertFalse(any("free-claude" in line for line in lines))
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
            all(isinstance(value, str) for key, value in row.items() if key != "thread")
            for row in rows
        ))
        by_name = {cast(str, row["name"]): row for row in rows}
        self.assertEqual(by_name["adversary reviewer"]["address"], "")
        for field in (
            "production", "showrunner", "showrunner_address", "unit", "director",
            "director_address", "slot_role", "session_dir",
        ):
            self.assertEqual(by_name["free-claude"][field], "")

        entry = roster.RosterEntry(
            "", "", "", "", "", roster.Messageable(""), "",
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
        self.assertTrue(any("worker alpha-impl (you)" in line for line in lines))
        self.assertFalse(any("unit director director (you)" in line for line in lines))

    def test_daemon_codex_identity_marks_its_own_window(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-window"}):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(len([line for line in lines if "(you)" in line]), 1)
        self.assertTrue(any("Codex window (you)" in line for line in lines))

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
        _ = (notes / f"{tool}{number}.md").write_text(f"---\nlogin: {login}\n---\n")
        home = self.home / (f".codex{number}" if tool == "codex" and number != 2 else ".codex" if tool == "codex" else f".claude{number}")
        home.mkdir(exist_ok=True)
        if tool == "codex":
            claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
            _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        else:
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
            "old-impl": {"thread_id": "old-thread", "status": "running", "port": 45677, "launcher_pid": os.getpid()},
            "new-test": {"thread_id": "new-thread", "status": "running", "port": 45678, "launcher_pid": os.getpid()},
            "between-turns": {"thread_id": "waiting-thread", "status": "done", "launcher_pid": os.getpid()},
            "missing-server": {"thread_id": "lost-thread", "status": "running", "port": 49999, "launcher_pid": os.getpid()},
        }))
        entries = self._entries()
        self.assertEqual(entries["old-impl"].account, roster.AccountLabel("codex1"))
        self.assertEqual(entries["new-test"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["between-turns"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["missing-server"].account, roster.AccountLabel("codex2"))
        _ = (self.director_run / "mesh_server.json").write_text(json.dumps({"port": 45678, "pid": os.getpid()}))
        entries = self._entries()
        self.assertEqual(entries["new-test"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["between-turns"].account, roster.AccountLabel("codex2"))
        self.assertEqual(entries["old-impl"].account, roster.AccountLabel("codex1"))

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
        with mock.patch.object(codex_daemon, "daemon_socket", return_value=home / "app-server-control" / "daemon.sock"):
            entry = self._entries()["Codex window"]
        self.assertEqual(entry.account, roster.AccountLabel("codex1"))

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
        self.assertTrue(any("free-claude" in line for line in lines))
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
                    reach=roster.Messageable("codex:daemon")),
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


if __name__ == "__main__":
    _ = unittest.main()
