#!/usr/bin/env python3
"""The machine roster joins Claude sessions, productions, runs, and Codex."""

from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname
from scripts.herdr import herdr_types

from scripts.lib import names
from scripts.lib.test_names import fixture_names
from scripts.agents.test_support import write_machine_config

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from functools import cached_property

from typing import TYPE_CHECKING, cast, override
from unittest import mock

from scripts.production import staff, session_staff
from scripts.whoami import agent_accounts
from scripts.production.staff_test_support import StaffTestCase
from scripts.herdr import worker_panes
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.usage import agents_log
from scripts.production.staff_test_support import (
    isolated_staff_file, production_membership, unit_membership, showrunner_relationship, showrunner_launch,
    fixture_worker, fixture_runtime_id, fixture_session, recorded_unit, fixture_member, recorded_showrunner,
)
from scripts.astra import design_roles
from scripts.astra.design_role_test_support import designer_fixture
from scripts.astra.design_role_test_support import write_empty_binding_record
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message import unit_labels

SCRIPTS = Path(__file__).parents[1]

from scripts.message import codex_daemon
from scripts.message.codex_test_support import FakeDaemon, receive_frame, reply, write_codex_account
from scripts.message import process_table
from scripts.message import roster
from scripts.message import send
from scripts.production import unit_lookup
from scripts.lib import unit_runs
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse, herdr_listener
from scripts.message import sessions, session_records
from scripts.message.relay_identity import relay_session_name
from scripts.message.relay_test_support import write_relay_record
from scripts.production import showrunners
if TYPE_CHECKING:
    from scripts.whoami import account
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


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())
    _ = unittest.enterModuleContext(mock.patch("scripts.message.failed_lookup.send_notice"))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", root / "astra-runs"))


class RosterTests(unittest.TestCase):
    @cached_property
    def pane_world(self) -> PaneWorld:
        return PaneWorld()

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
            self.home, self.sessions, self.notifier, self.runs, binaries,
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

        transport_config = self.root / "config"
        transport_config.mkdir()
        write_text_atomic(transport_config / "direct.conf", "# Worker settings.\n")
        write_machine_config(transport_config)
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
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.ssh_log),
            "SSH_SNAPSHOT": roster.RosterSnapshot(roster.Roster([], [])).encode(),
            "CLAUDE_BIN": str(fake_codex),
            "CLAUDE_CODE_SESSION_ID": "acd50bad-5afa-5d3d-b155-3766b7d113e0",
            "CODEX_THREAD_ID": "",
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "SHOWRUNNERS_CONFIG": str(config),
            "CONF_DIR": str(transport_config),
            "SHOWRUNNERS_SESSIONS": str(Path(__file__).with_name("sessions.py")),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "USAGE_STATE_DIR": str(self.root / "usage"),
            "ROSTER_RUN_ROOT": str(self.runs),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
            "ROSTER_PS_FIXTURE": str(self.ps_state),
            "PROCESS_TABLE_PS": f"cat {self.ps_state}",
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership"),
        }
        environment_patch = cast(
            AbstractContextManager[object],
            mock.patch.dict(os.environ, self.environment, clear=True),
        )
        _ = self.enterContext(environment_patch)
        _ = self.enterContext(mock.patch.object(unit_runs, "run_roots", return_value=(self.runs,)))
        _ = self.enterContext(mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", self.root / "astra-runs"))
        _ = self.enterContext(mock.patch.object(showrunners, "CONFIG", config))
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier))
        _ = self.enterContext(mock.patch.object(
            showrunners, "SESSIONS", Path(__file__).with_name("sessions.py"),
        ))
        _ = self.enterContext(mock.patch.object(
            process_table, "PS", ["ps", "-eo", "pid=,ppid=,comm=,args="],
        ))
        _ = self.enterContext(mock.patch.object(herdr, "herdr_binary", return_value=self._herdr_fixture(self._herdr_sessions())))
        running_session = session_records.running_session
        def fixture_running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991100 or running_session(record)
        _ = self.enterContext(mock.patch.object(session_records, "running_session", side_effect=fixture_running))
        self.environment["HERDR_PROC"] = str(self.root / "herdr-proc")
        os.environ["HERDR_PROC"] = self.environment["HERDR_PROC"]
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = unit_membership("alpha", "unit-one")
        self.pane_world.add(unit_membership("alpha", "unit-one"), "w1:p0")
        native_process = herdr.native_process
        def fixture_process(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
            if pid in self.pane_world.processes:
                return self.pane_world.native_process(pid, kind, reported)
            return native_process(pid, kind, reported)
        pane_patches = [
            cast(object, self.enterContext(mock.patch.object(worker_panes, "_client", return_value=self.pane_world))),
            cast(object, self.enterContext(mock.patch.object(herdr, "native_process", side_effect=fixture_process))),
            cast(object, self.enterContext(mock.patch.object(herdr, "_startup_processes", side_effect=self.pane_world.descendants))),
        ]
        self.addCleanup(pane_patches.clear)
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
        instance = self.notifier / f"showrunner-{showrunner_relationship('alpha').id}"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            "TARGET=session:acd50bad-5afa-5d3d-b155-3766b7d113e0\n"
            + f"CHECK=zsh /scripts/production_check.sh {showrunner_relationship('alpha').id}\n",
            encoding="utf-8",
        )

    def _publish_workers(self, path: Path, entries: dict[str, dict[str, object]],
                         *, codex_home: Path | None = None) -> None:
        parent = (unit_membership("alpha", "orphan-unit") if path.parent.name == "orphan-run"
                  else unit_membership("alpha", "unit-one"))
        if path.parent.name == "orphan-run":
            parent = staff.set_worktree(parent.id, staff.PresentWorktree(roster.remote.local_machine(), self.root / "orphan-tree"),
                                        expected=parent.worktree)
        write_text_atomic(path.parent / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
        for name, entry in entries.items():
            identity = entry.get("thread_id", fixture_runtime_id(name))
            assert isinstance(identity, str)
            try:
                identity = staff.native_id(identity)
            except staff.StaffRefused:
                identity = fixture_runtime_id(identity)
            child = fixture_worker(name, parent.id, runtime_id=identity,
                                   backend=staff.CodexBackend(codex_home or self.root / "codex-home"))
            pane = next((key for key, value in self.pane_world.locations.items() if value["label"] == name), "")
            if entry.get("status") == "ended":
                if pane:
                    self.pane_world.remove_leaf(pane)
                    del self.pane_world.locations[pane]
                    if pane in self.pane_world.foreground:
                        del self.pane_world.foreground[pane]
            else:
                if pane:
                    self.pane_world.activate(child.current_session, pane, "codex")
                else:
                    pane = f"w1:p{len(self.pane_world.locations) + 10}"
                    self.pane_world.add(child, pane, family="codex")
                self.pane_world.locations[pane]["agent_status"] = "working" if entry.get("status") == "running" else "idle"
            role = entry.get("role")
            if role in staff.WORKER_ROLES:
                _ = staff.set_worker_role(child.id, staff.KnownWorkerRole(role))
            if role in agents_log.ROLES:
                lens = next((value for value in agents_log.LENSES if value == entry.get("lens")), None)
                agents_log.append(identity, role, cast(agents_log.Lens | None, lens), datetime.now(UTC))
        write_text_atomic(path.parent / unit_runs.STATE_FILE, json.dumps({
            unit_runs.OWNER_FIELD: parent.id,
            "pass": {name: {"worker_id": entry.get("thread_id"), "kind": "review", "status": "active",
                            "lens": entry["lens"], "started_at": 1}
                     for name, entry in entries.items() if entry.get("role") == "review" and entry.get("lens")},
        }))

    def _write_pane_workers(self, run: Path, entries: dict[str, dict[str, object]],
                            *, server_pid: int | None = None) -> None:
        del server_pid
        run.mkdir(parents=True, exist_ok=True)
        self._publish_workers(run / unit_runs.STATE_FILE, entries)

    def _build_complete_roster(self) -> None:
        runner_cwd = self.home / "projects" / "runner"
        director_cwd = self.home / "projects" / "unit-one"
        worker_cwd = self.home / "projects" / "worker"
        solo_cwd = self.home / "projects" / "solo"
        free_cwd = self.home / "projects" / "free"
        self.production_doc = self.root / "alpha-production.md"
        _ = self.production_doc.write_text("\n".join((
            "- **User zone:** America/New_York",
            "- **Merge branch:** main",
            "- **Log:** log.md",
            f"- **Showrunner checkout:** {runner_cwd}",
        )), encoding="utf-8")
        _ = recorded_unit("alpha", "unit-one", director_cwd, self.production_doc, plan="docs/plan.md")
        self._write_showrunner_timer()
        runner = showrunner_relationship("alpha")
        _ = staff.update_current_session(runner.current_session,
            staff.native_id("acd50bad-5afa-5d3d-b155-3766b7d113e0"))
        unit = unit_membership("alpha", "unit-one")
        _ = staff.update_current_session(unit.current_session,
            staff.native_id("17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))
        self._write_session(str(os.getpid()), "runner", "acd50bad-5afa-5d3d-b155-3766b7d113e0", runner_cwd, status="ready")
        self._write_session(
            "director", "director", "17e44ec4-3e24-5ae1-b10d-5cf30de9455b", director_cwd,
            status="working", tmux="old-label:@1.%1",
        )
        self._write_session(
            "worker", "claude-worker", WORKER_SESSION_ID, worker_cwd, status="idle"
        )
        _ = fixture_worker("alpha-test", unit.id, runtime_id=WORKER_SESSION_ID)
        self._write_session("solo", "solo-director", "22135b22-7da2-5f04-8840-738da7f248ba", solo_cwd, status="waiting")
        self._write_session("free-old", "old-free-name", "abd1a227-f410-5c76-bc96-a0c77c0f3ef0", free_cwd, updated=1)
        self._write_session("free-new", "free-claude", "abd1a227-f410-5c76-bc96-a0c77c0f3ef0", free_cwd, status="ready", updated=2)

        self.director_run = self.runs / "director-run"
        self.director_run.mkdir()
        _ = (self.director_run / unit_runs.STATE_FILE).write_text(json.dumps({unit_runs.OWNER_FIELD: unit.id}),
                                                               encoding="utf-8")
        _ = staff.set_run(unit.id, staff.OpenRun(self.director_run))
        _ = (self.director_run / "workers").write_text(
            "12345678\talpha-test\n", encoding="utf-8",
        )
        self._write_pane_workers(self.director_run, {
            "alpha-impl": {
                "thread_id": "7aa082fc-e724-5e46-8846-bd68e7eb3f46", "turn_id": "turn-1", "status": "running",
                "launcher_pid": os.getpid(), "role": "impl",
            },
            "alpha-fix": {"thread_id": "d6c57ffb-f619-567d-b06c-1a1f7791b8af", "status": "done", "role": "fix"},
            "alpha-failed": {"thread_id": "ea8ee697-99ad-5903-9952-ebc9b68e2eef", "status": "failed"},
            "alpha-start": {
                "thread_id": "8174bc31-7a95-5fd8-9220-e33314e2cd14", "status": "starting", "launcher_pid": os.getpid(),
            },
            "alpha-wait": {
                "thread_id": "aa6c4f2b-129f-58c6-a216-8b690d57643f", "status": "waiting_capacity",
                "launcher_pid": os.getpid(),
            },
            "alpha-exhaust": {"thread_id": "a0113ce8-7818-5b68-b018-eda02ae69013", "status": "capacity_exhausted"},
            "alpha-research": {
                "thread_id": "76d3172a-4806-51c4-89b6-e328af9f6367", "turn_id": "turn-2", "status": "running",
                "launcher_pid": os.getpid(), "role": "review", "lens": "adversary",
            },
            "alpha-review-plain": {
                "thread_id": "54d79ec8-8609-5bb5-a05d-e1c2476abfbe", "turn_id": "turn-3", "status": "running",
                "launcher_pid": os.getpid(), "role": "review",
            },
            "alpha-ended": {"thread_id": "94e324e8-4161-520a-8910-2efc613382bb", "status": "ended"},
            "alpha-no-thread": {"thread_id": fixture_runtime_id("alpha-published-start"),
                                "status": "starting", "launcher_pid": os.getpid()},
        })

        solo_run = self.runs / "solo-run"
        solo_run.mkdir()
        solo = fixture_member("producer", name="solo-director", runtime_id="22135b22-7da2-5f04-8840-738da7f248ba")
        _ = staff.set_run(solo.id, staff.OpenRun(solo_run))

        orphan_run = self.runs / "orphan-run"
        self._write_pane_workers(orphan_run, {
            "orphan-impl": {
                "thread_id": "ceb99381-63dd-541b-aab3-79e899496801", "turn_id": "turn-orphan", "status": "running",
                "launcher_pid": os.getpid(),
            },
            "orphan-fix": {"thread_id": "eb7a624c-bc14-5a52-9eac-6f6c188a5ea5", "status": "done"},
        })
        (orphan_run / "heartbeat.log").touch()

        stale_run = self.runs / "stale-marker-run"
        self._write_pane_workers(stale_run, {
            "stale-impl": {
                "thread_id": "6c55fc12-fc24-5b88-ad32-0baaac429dc0", "turn_id": "turn-stale", "status": "running",
                "launcher_pid": os.getpid(),
            },
        })
        dead = fixture_member("producer", name="dead-director")
        _ = staff.set_run(dead.id, staff.OpenRun(stale_run))

        dead_server_run = self.runs / "dead-server-run"
        self._write_pane_workers(dead_server_run, {
            "dead-impl": {
                "thread_id": "43a61b0f-684c-531a-b22a-e17537e0398a", "turn_id": "turn-dead", "status": "running",
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
                "7aa082fc-e724-5e46-8846-bd68e7eb3f46", str(self.home / "projects" / "unit-one"), "active",
            ),
            codex_daemon.DaemonSession(
                "eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539", str(self.home / "projects" / "codex"), "idle",
            ),
        ]

    def test_docless_staff_row_warns_once_and_keeps_the_good_unit(self) -> None:
        bad = fixture_member("showrunner", name="bad", runtime_id="abd1a227-f410-5c76-bc96-a0c77c0f3ef0")
        membership = staff.snapshot()
        self.assertEqual(membership, staff.initial_snapshot())
        self.assertEqual(membership.inputs, (str(self.root / "staff.json"),))
        self.assertIsInstance(membership.by_id(bad.id), staff.StaffRow)
        self.assertIsInstance(bad.doc, staff.DocUnfilled)
        status, output, errors = self._run_main("--here", "--json")
        report = cast(dict[str, object], json.loads("\n".join(output)))
        self.assertEqual(status, 0)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["warnings"], ["bad: the production document is not recorded"])
        self.assertEqual(errors, ["could not read: warning: bad: the production document is not recorded"])
        self.assertIn("unit-one", [entry["name"] for entry in cast(list[dict[str, object]], report["entries"])])
        self.assertNotIn(f"session:{bad.current_session}",
                         [entry["address"] for entry in cast(list[dict[str, object]], report["entries"])])

    def test_docless_staff_row_does_not_block_a_peer_name(self) -> None:
        _ = fixture_member("showrunner", name="bad", runtime_id="abd1a227-f410-5c76-bc96-a0c77c0f3ef0")
        owner = fixture_member("producer", name="peer-only")
        membership = staff.snapshot()
        result = roster.roster(os.environ, roster.ThisMachine())
        template = next(entry for entry in result.entries if entry.name == "unit-one")
        peer_entry = replace(template, name="peer-only", reach=roster.Messageable(f"session:{owner.current_session}"),
                             session=roster.ClaudeSession(str(owner.current_session)))
        with mock.patch.object(roster, "peer_roster", return_value=roster.Roster([peer_entry], [])) as peer:
            found = send.matching_recipients("peer-only", send.LocalThenPeer(), result, membership=membership)
        self.assertIsInstance(found, send.RecipientMatches)
        assert isinstance(found, send.RecipientMatches)
        self.assertEqual(found.entries, [peer_entry])
        self.assertIsInstance(found.destination, send.NamedMachine)
        peer.assert_called_once()
        with mock.patch.object(roster, "peer_roster", return_value=roster.Roster([], [])):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = send.resolve_recipient("bad", send.LocalThenPeer(), result, membership=membership)

    def test_docless_staff_warning_allows_peer_delivery_and_refuses_its_own_recipient(self) -> None:
        bad = fixture_member("showrunner", name="bad", runtime_id="abd1a227-f410-5c76-bc96-a0c77c0f3ef0")
        owner = fixture_member("producer", name="peer-only")
        membership = staff.snapshot()
        self.assertEqual(membership.inputs, (str(self.root / "staff.json"),))
        self.assertEqual(membership.by_id(bad.id), bad)
        observed = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(observed.problems, [])
        self.assertTrue(any(entry.name == "unit-one" for entry in observed.entries))
        template = next(entry for entry in observed.entries if entry.name == "unit-one")
        peer_entry = replace(template, name="peer-only", reach=roster.Messageable(f"session:{owner.current_session}"),
                             session=roster.ClaudeSession(str(owner.current_session)))
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(roster, "peer_roster", return_value=roster.Roster([peer_entry], [])) as peer,
              mock.patch.object(send, "STATE", self.root / "send-state"),
              mock.patch.object(send, "remote", return_value=send.Result("sent", "fixture peer")) as deliver,
              mock.patch.object(send, "relay", side_effect=AssertionError("no local relay")),
              mock.patch.object(send, "user", side_effect=AssertionError("no user push")),
              redirect_stdout(output), redirect_stderr(errors)):
            status = send.main(["--to", "peer-only", "--from", "fixture-reviewer", "--text", "fixture text"])
            self.assertEqual(status, 0, output.getvalue() + errors.getvalue())
            self.assertEqual(output.getvalue(), "SENT: fixture peer\n")
            self.assertEqual(errors.getvalue(), "")
            peer.assert_called_once()
            deliver.assert_called_once()
            message = cast(send.Message, deliver.call_args.args[0])
            self.assertEqual((message.to, message.text), (f"session:{owner.current_session}", "fixture text"))
            with self.assertRaises(SystemExit) as refused:
                _ = send.send(send.parse(["--to", "bad", "--from", "fixture-reviewer", "--text", "fixture text"]))
            self.assertEqual(refused.exception.code, 2)
            self.assertIn("bad: the production document is not recorded", errors.getvalue())
            peer.assert_called_once()
            deliver.assert_called_once()

    def test_document_read_failure_still_blocks_unproven_peer_fallback(self) -> None:
        _ = fixture_member("showrunner", name="unreadable", doc=staff.LiveDoc(
            roster.remote.local_machine(), self.root / "missing-production.md"))
        membership = staff.snapshot()
        result = roster.roster(os.environ, roster.ThisMachine())
        self.assertTrue(any("missing-production.md" in problem for problem in result.problems))
        with mock.patch.object(roster, "peer_roster") as peer:
            found = send.matching_recipients("peer-only", send.LocalThenPeer(), result, membership=membership)
        self.assertIsInstance(found, send.LocalRosterIncomplete)
        peer.assert_not_called()
        self.assertTrue(any(entry.name == "unit-one" for entry in result.entries))

    def test_invalid_staff_parent_remains_a_staff_read_failure(self) -> None:
        membership = staff.snapshot()
        unit = next(row for row in membership.rows if row.kind == "unit")
        for parent in (staff.StaffId("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"), unit.id):
            with self.subTest(parent=parent):
                rows = [replace(row, parent=staff.ReportsTo(parent)) if row.id == unit.id else row for row in membership.rows]
                _ = (self.root / "staff.json").write_text(json.dumps({"schema": 2, "rows": [row.to_json() for row in rows]}))
                with self.assertRaises(staff.StaffUnreadable):
                    _ = staff.snapshot()
                observed = roster.roster(os.environ, roster.ThisMachine())
                self.assertTrue(any("requires a showrunner parent" in problem for problem in observed.problems))
                self.assertEqual(observed.warnings, [])
                with (mock.patch.object(roster, "peer_roster") as peer,
                      redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused):
                    _ = send.send(send.parse(["--to", "peer-only", "--from", "fixture-reviewer", "--text", "fixture text"]))
                self.assertEqual(refused.exception.code, 2)
                peer.assert_not_called()

    def test_unit_worker_uses_its_staff_parent_messaging_address(self) -> None:
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            result = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(result.problems, [])
        unit_worker = next(entry for entry in result.entries if entry.name == "alpha-impl")
        self.assertEqual(unit_worker.reach, roster.Messageable("worker:unit-one/alpha-impl"))

    def _entries(self) -> dict[str, roster.RosterEntry]:
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual(found.problems, [])
        return {entry.name or (
            f"session:{entry.session.session_id}" if isinstance(entry.session, roster.ClaudeSession)
            else f"herdr {entry.pane.session}:{entry.pane.pane}" if isinstance(entry.pane, herdr.HerdrPlace)
            else f"codex:{entry.thread.thread_id}" if isinstance(entry.thread, roster.CodexThread)
            else entry.reach.address if isinstance(entry.reach, roster.Messageable)
            else f"review:{entry.assignment.slot}" if isinstance(entry.assignment, roster.WorkerAssignment) else ""
        ): entry for entry in found.entries}

    def test_codex_session_without_a_staff_row_is_freestanding_and_lists_successfully(self) -> None:
        result, _, errors = self._run_main("--json")
        entry = self._entries()["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"]
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(
            (entry.role, entry.assignment, entry.showrunner, entry.director),
            ("freestanding", roster.NotAWorker(), roster.NoShowrunner(), roster.NoDirector()),
        )
        self.assertEqual(entry.reach, roster.Messageable("codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"))
        observed = roster.Roster(list(self._entries().values()), [])
        for native, old_name in (
            ("codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539", "Codex window"),
            ("session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0", "free-claude"),
        ):
            with self.subTest(native=native):
                unnamed = next(row for row in observed.entries
                               if isinstance(row.reach, roster.Messageable) and row.reach.address == native)
                self.assertEqual(unnamed.name, "")
                found = send.matching_recipients(old_name, send.LocalOnly(), observed, membership=staff.snapshot())
                self.assertIsInstance(found, send.RecipientMatches)
                assert isinstance(found, send.RecipientMatches)
                self.assertEqual(found.entries, [])
                addressed = send.matching_recipients(native, send.LocalOnly(), observed, membership=staff.snapshot())
                self.assertIsInstance(addressed, send.RecipientMatches)
                assert isinstance(addressed, send.RecipientMatches)
                self.assertEqual(addressed.entries, [unnamed])

    def test_codex_unit_row_without_a_parent_claim_is_freestanding_and_lists_successfully(self) -> None:
        record = staff.snapshot()
        row = staff.StaffRow(
            staff.StaffId("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"), "unit", "Staff owner", staff.StaffRoot(),
            current_session=staff.StaffId("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"), unit_type=staff.UnitDetails("direct"),
        )
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((*record.rows, row))):
            result, _, errors = self._run_main("--json")
            entry = self._entries()["Staff owner"]
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(
            (entry.role, entry.assignment, entry.showrunner, entry.director),
            ("freestanding", roster.NotAWorker(), roster.NoShowrunner(), roster.NoDirector()),
        )
        self.assertEqual(entry.reach, roster.Messageable("codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"))
        self.assertEqual(entry.name, "Staff owner")

    def test_codex_unit_with_an_unavailable_recorded_parent_keeps_its_named_problem(self) -> None:
        record = staff.snapshot()
        wrong_kind = next(row.id for row in record.rows if row.kind == "unit")
        for parent in (staff.StaffId("missing-owner"), wrong_kind):
            with self.subTest(parent=parent):
                row = staff.StaffRow(
                    staff.StaffId("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"), "unit", "Codex window", staff.ReportsTo(parent),
                    current_session=staff.StaffId("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"), unit_type=staff.UnitDetails("direct"),
                )
                with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((*record.rows, row))):
                    result, lines, errors = self._run_main("--json")
                self.assertEqual(result, 1)
                self.assertEqual(json.loads("\n".join(lines))["problems"],
                                 ["Codex window: the staff parent is unavailable"])
                self.assertEqual(errors, ["could not read: Codex window: the staff parent is unavailable"])

    def test_keyed_send_reuses_roster_registry_before_delivery(self) -> None:
        state = self.root / "message"
        state.mkdir()
        unit = next(row for row in staff.find(kind="unit") if row.name == "unit-one")
        _ = fixture_worker("impl2", unit.id)
        identity = "abd1a227-f410-5c76-bc96-a0c77c0f3ef0"
        owner = fixture_member("producer", name="free-claude", runtime_id=identity)
        registered = sessions.addressed(f"session:{identity}", sessions.live_sessions())
        assert registered is not None
        started = send.now()
        targets = ("free-claude", f"session:{identity}", f"uds:{registered['messagingSocketPath']}")
        for target in targets:
            registry = mock.Mock(wraps=sessions.read_registry)
            with self.subTest(target=target):
                previous = f"staff:{owner.id}" if target == owner.name else target
                _ = (state / "keys.json").write_text(json.dumps({"notice": {"last": {previous: started.isoformat()}}}))
                options = send.parse(["--here", "--to", target, "--from", "fixture", "--text", "Notice",
                                      "--key", "notice", "--repeat-minutes", "30"])
                with (mock.patch.object(send, "STATE", state),
                      mock.patch.object(send, "now", return_value=started),
                      mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()),
                      mock.patch.object(sessions, "read_registry", registry),
                      mock.patch.object(send, "relay", return_value=send.Result("sent", "fixture")) as relay):
                    self.assertEqual(send.send(options).outcome, "skipped")
                registry.assert_called_once_with()
                relay.assert_not_called()


    def test_fast_labels_agree_with_full_roster_for_live_claude_sessions(self) -> None:
        full = roster.unit_labels(list(self._entries().values()))
        wanted = ["17e44ec4-3e24-5ae1-b10d-5cf30de9455b", "acd50bad-5afa-5d3d-b155-3766b7d113e0", "abd1a227-f410-5c76-bc96-a0c77c0f3ef0"]
        with mock.patch.object(process_table, "snapshot", side_effect=AssertionError("no process table")) as scan, \
             mock.patch.object(codex_daemon, "loaded_sessions", side_effect=AssertionError("no daemon")) as daemon, \
             mock.patch.object(account, "home_account_state", side_effect=AssertionError("no accounts")) as accounts:
            fast = roster.unit_labels_for(wanted, self.environment)
        self.assertEqual(fast, {session_id: full[session_id] for session_id in wanted})
        self.assertEqual(fast["17e44ec4-3e24-5ae1-b10d-5cf30de9455b"], roster.UnitLabels("unit-one", "alpha", "unit-one", unit_labels.OwnerRole.UNIT))
        scan.assert_not_called()
        daemon.assert_not_called()
        accounts.assert_not_called()

    def test_transferred_director_keeps_its_new_production_without_a_new_document_row(self) -> None:
        receiving = showrunner_relationship("receiving")
        row = next(row for row in staff.find(kind="unit") if row.name == "unit-one")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        env_file = self.root / "herdr-proc/991100/environ"
        _ = env_file.write_bytes(env_file.read_bytes() + f"\0CODEX_THREAD_ID={row.id}".encode())
        doc = self.root / "receiving-production.md"
        _ = doc.write_text("# Receiving\n")
        _ = staff.set_doc(receiving.id, staff.LiveDoc(roster.remote.local_machine(), doc))
        timer = self.notifier / "showrunner-receiving"
        timer.mkdir()
        _ = (timer / "conf").write_text(f"TARGET=session:522e6367-86e1-5829-ada8-2c71b5c28c97\nCHECK=zsh /scripts/production_check.sh {receiving.id}\n")
        self._write_session(str(os.getppid()), "receiving-runner", "522e6367-86e1-5829-ada8-2c71b5c28c97", self.home / "receiving",
                            pid=os.getppid())
        director = self._entries()["unit-one"]
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))
        self.assertEqual(director.showrunner,
                         roster.UnderShowrunner("receiving", "receiving", "session:522e6367-86e1-5829-ada8-2c71b5c28c97"))
        self.assertNotIn("unit-one", doc.read_text())

    def test_full_roster_reads_the_staff_owner_once(self) -> None:
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            entries = self._entries()
        self.assertEqual(entries["unit-one"].director, roster.DirectsUnit("unit-one"))
        self.assertEqual(reads.call_count, 1)

    def test_stopped_run_uses_the_display_staff_record(self) -> None:
        owner = unit_membership("alpha", "unit-one")
        write_text_atomic(self.director_run / unit_runs.STATE_FILE, json.dumps({
            unit_runs.OWNER_FIELD: owner.id,
        }))
        (self.sessions / "director.json").unlink()
        (self.director_run / "heartbeat.log").touch()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            entries = self._entries()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(entries["alpha-impl"].director, roster.UnitWithoutRunningDirector("unit-one"))
        self.assertEqual(entries["alpha-test"].director, roster.UnitWithoutRunningDirector("unit-one"))


    def test_backfilled_retired_rows_leave_full_roster_and_fast_session_labels_unchanged(self) -> None:
        before = self._entries()
        wanted = ["17e44ec4-3e24-5ae1-b10d-5cf30de9455b", "acd50bad-5afa-5d3d-b155-3766b7d113e0"]
        labels = roster.unit_labels_for(wanted, self.environment)
        old = unit_membership("alpha", "retired")
        _ = staff.set_worktree(old.id, staff.RemovedWorktree("natedev", self.root / "retired"), expected=old.worktree)
        historical = showrunner_relationship("wrapped")
        _ = staff.set_doc(historical.id, staff.WrappedDoc("natedev", self.root / "wrapped-production.md"))
        self.assertEqual(self._entries(), before)
        self.assertEqual(roster.unit_labels_for(wanted, self.environment), labels)
        self.assertIsNotNone(staff.by_id(old.id))

    def test_session_labels_read_the_staff_owner_once(self) -> None:
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            labels = roster.unit_labels_for(["17e44ec4-3e24-5ae1-b10d-5cf30de9455b", "acd50bad-5afa-5d3d-b155-3766b7d113e0"], self.environment)
        self.assertEqual(labels["17e44ec4-3e24-5ae1-b10d-5cf30de9455b"], roster.UnitLabels("unit-one", "alpha", "unit-one", unit_labels.OwnerRole.UNIT))
        self.assertEqual(reads.call_count, 1)

    def test_renamed_production_has_the_same_full_and_fast_labels(self) -> None:
        owner = showrunner_relationship("alpha")
        owner = staff.set_doc(owner.id, staff.LiveDoc(roster.remote.local_machine(), self.production_doc))
        showrunner_launch(owner, "acd50bad-5afa-5d3d-b155-3766b7d113e0", self.environment)
        _ = staff.rename(owner.id, "builds")
        full = roster.unit_labels(self._entries().values())
        fast = roster.unit_labels_for(["17e44ec4-3e24-5ae1-b10d-5cf30de9455b"], self.environment)
        self.assertEqual(full["17e44ec4-3e24-5ae1-b10d-5cf30de9455b"], roster.UnitLabels("unit-one", "builds", "unit-one", unit_labels.OwnerRole.UNIT))
        self.assertEqual(fast, {"17e44ec4-3e24-5ae1-b10d-5cf30de9455b": full["17e44ec4-3e24-5ae1-b10d-5cf30de9455b"]})

    def test_unregistered_runtime_with_the_same_unit_labels_keeps_the_registered_units_owner(self) -> None:
        self._write_session("duplicate", "other-director", "6e203923-e688-516d-a58a-3a0712762814", self.home / "other", pid=991101)
        self._herdr_process(991101, 991099, ["claude"], HERDR_PANE_ID="w1:p2",
                            SHOWRUNNER_UNIT="alpha", SHOWRUNNER_UNIT_ID="unit-one")
        running_session = session_records.running_session

        def fixture_running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991101 or running_session(record)

        with mock.patch.object(session_records, "running_session", side_effect=fixture_running), \
             mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(self.environment, roster.ThisMachine())
            fast = roster.unit_labels_for(["17e44ec4-3e24-5ae1-b10d-5cf30de9455b", "6e203923-e688-516d-a58a-3a0712762814"], self.environment)
        full = roster.unit_labels(found.entries)
        self.assertFalse(any("its mark is on herdr sessions" in problem for problem in found.problems))
        expected = {
            "17e44ec4-3e24-5ae1-b10d-5cf30de9455b": roster.UnitLabels("unit-one", "alpha", "unit-one", unit_labels.OwnerRole.UNIT),
            "6e203923-e688-516d-a58a-3a0712762814": roster.UnitLabels("", "—", "—"),
        }
        self.assertEqual({sid: full[sid] for sid in expected}, expected)
        self.assertEqual(fast, expected)

    def _run_main(
        self, *arguments: str,
        daemon: list[codex_daemon.DaemonSession] | codex_daemon.DaemonUnreadable | None = None,
        here: bool = True,
        native_identity: str = "acd50bad-5afa-5d3d-b155-3766b7d113e0",
    ) -> tuple[int, list[str], list[str]]:
        output, errors = io.StringIO(), io.StringIO()
        daemon_result = self._daemon_sessions() if daemon is None else daemon
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=daemon_result), \
                mock.patch.object(staff, "runtime_id", return_value=native_identity), \
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
        member = designer_fixture(unit_membership("alpha", "unit-one").id,
                                  "lasting-astra-designer", self.root / "codex-home")
        design = design_roles.HeldDesign(design_roles.DesignPlacement("00000000-0000-4000-8000-000000000046", "fixture-host", 2),
            "fixture-repo", str(self.root / "checkout"), str(self.root / "checkout/design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "lasting-astra-designer", design_roles.RoleOpen(member.id)),
             design_roles.RoleInstance("reviewer", "lasting-reviewer-alias", design_roles.RoleNotOpened())))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        design_folder = design_roles.design_directory(design.placement.design_id)
        found = self._entries()
        entry = found["lasting-astra-designer"]
        self.assertEqual(entry.thread, roster.CodexThread(member.current_session))
        self.assertEqual(entry.role, "astra-designer")
        self.assertEqual(entry.director, roster.UnderDirector("unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))
        self.assertEqual(entry.assignment, roster.HeldDesignRole("designer", str(design_folder)))
        self.assertIsInstance(entry.reach, roster.CannotReceiveMessages)
        self.assertEqual(sum(row.thread == roster.CodexThread(member.current_session) for row in found.values()), 1)

    def test_durable_role_keeps_its_fixed_worker_after_a_rename_and_name_reuse(self) -> None:
        parent = unit_membership("alpha", "unit-one")
        checkout = self.root / "checkout"
        parent = staff.set_worktree(parent.id, staff.PresentWorktree("fixture-host", checkout), expected=parent.worktree)
        original = designer_fixture(parent.id, "held-designer", self.root / "codex-home")
        design = design_roles.HeldDesign(
            design_roles.DesignPlacement("00000000-0000-4000-8000-000000000047", "fixture-host", 1),
            "fixture-repo", str(self.root / "checkout"), str(self.root / "checkout/design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", original.name, design_roles.RoleOpen(original.id)),
             design_roles.RoleInstance("reviewer", "held-reviewer", design_roles.RoleNotOpened())),
            checkout=design_roles.UnitCheckout(parent.id))
        save = cast(Callable[[design_roles.HeldDesign], None], getattr(design_roles, "_save"))
        save(design)
        with mock.patch.object(herdr.Herdr, "retitle_row", return_value=True):
            renamed = staff.rename(original.id, "renamed-designer")
        replacement = fixture_worker(original.name, parent.id,
                                     runtime_id=fixture_runtime_id("replacement-held-designer"),
                                     backend=staff.CodexBackend(self.root / "codex-home"))
        destination = self.root / "moved-checkout"
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("fixture-host", destination), expected=parent.worktree)
        found = roster.roster(os.environ, roster.ThisMachine())
        entries = [entry for entry in found.entries if entry.role == "astra-designer" and entry.name == renamed.name]
        self.assertEqual(found.warnings, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, renamed.name)
        self.assertEqual(entries[0].thread, roster.CodexThread(renamed.current_session))
        self.assertEqual(entries[0].cwd, str(destination))
        self.assertNotEqual(entries[0].thread, roster.CodexThread(replacement.current_session))
        self.assertEqual(sum(entry.thread == roster.CodexThread(renamed.current_session) for entry in found.entries), 1)

    def test_every_role_and_status_is_returned_with_its_relationships(self) -> None:
        entries = self._entries()
        machine = roster.machine_name()

        self.assertEqual(entries["alpha"], roster.RosterEntry(
            machine=machine, kind="claude", role="showrunner", name="alpha",
            status="ready", reach=roster.Messageable("session:acd50bad-5afa-5d3d-b155-3766b7d113e0"),
            cwd=str(self.home / "projects" / "runner"),
            showrunner=roster.RunsProduction("alpha"),
            director=roster.NoDirector(),
            assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("acd50bad-5afa-5d3d-b155-3766b7d113e0"),
            registry_name=roster.RegistryName("runner"),
            account=roster.AccountUnread("process account could not be read"),
        ))
        self.assertEqual(entries["unit-one"], roster.RosterEntry(
            machine=machine, kind="claude", role="unit director", name="unit-one",
            status="working", reach=roster.Messageable("session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"),
            cwd=str(self.home / "projects" / "unit-one"),
            showrunner=roster.UnderShowrunner(
                "alpha", "alpha", "session:acd50bad-5afa-5d3d-b155-3766b7d113e0",
            ),
            director=roster.DirectsUnit("unit-one"),
            assignment=roster.NotAWorker(),
            session=roster.ClaudeSession("17e44ec4-3e24-5ae1-b10d-5cf30de9455b"),
            registry_name=roster.RegistryName("director"),
            account=roster.AccountUnread("process account could not be read"),
        ))
        self.assertEqual(entries["alpha-test"].role, "worker")
        self.assertEqual(entries["alpha-test"].director, roster.UnderDirector(
            "unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b",
        ))
        self.assertEqual(entries["alpha-test"].assignment, roster.WorkerAssignment(
            "test", str(self.director_run), roster.AssignmentUnrecorded(), "test",
        ))
        self.assertEqual(entries["solo-director"].role, "unit director")
        self.assertEqual(entries["solo-director"].showrunner, roster.NoShowrunner())
        self.assertEqual(entries["session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"].role, "freestanding")
        self.assertNotIn("old-free-name", entries)

        for name in ("alpha-impl", "alpha-fix"):
            with self.subTest(name=name):
                self.assertEqual(entries[name].status,
                                 f"{'working' if name == 'alpha-impl' else 'idle'}; pane "
                                 + next(pane for pane, location in self.pane_world.locations.items()
                                        if location.get("label") == name))
                self.assertEqual(entries[name].role, "worker")
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))
                self.assertEqual(entries[name].showrunner, roster.UnderShowrunner(
                    "alpha", "alpha", "session:acd50bad-5afa-5d3d-b155-3766b7d113e0"))
                self.assertEqual(entries[name].reach, roster.Messageable(f"worker:unit-one/{name}"))
        self.assertEqual(
            entries["alpha-research"].assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "research"),
        )
        self.assertEqual(
            entries["alpha-impl"].assignment,
            roster.WorkerAssignment("impl", str(self.director_run), "impl", "impl"),
        )
        self.assertEqual(
            entries["review:adversary"].assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "adversary"),
        )
        self.assertEqual(
            entries["review:adversary"].reach,
            roster.CannotReceiveMessages("blind reviewer"),
        )
        self.assertNotIn("contract reviewer", entries)
        self.assertEqual(entries["orphan-impl"].director, roster.UnitWithoutRunningDirector("orphan-unit"))
        self.assertEqual(entries["alpha-ended"].reach, roster.CannotReceiveMessages("closed"))
        self.assertIn("dead-impl", entries)
        self.assertEqual(entries["stale-impl"].reach, roster.Messageable("worker:unit-one/stale-impl"))

        self.assertEqual(entries["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"].role, "freestanding")
        self.assertEqual(
            entries["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"].reach,
            roster.Messageable("codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"),
        )
        self.assertNotIn("worker duplicate", entries)

        path = self.sessions / "director.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        record["name"] = "Wrong registry title"
        write_text_atomic(path, json.dumps(record))
        claude_rows: dict[str, roster.RosterEntry] = {}
        def observe_entries[**P](
            build: Callable[P, dict[str, roster.RosterEntry]],
        ) -> Callable[P, dict[str, roster.RosterEntry]]:
            def read_claude(*args: P.args, **kwargs: P.kwargs) -> dict[str, roster.RosterEntry]:
                rows = build(*args, **kwargs)
                claude_rows.update(rows)
                return rows
            return read_claude

        with mock.patch.object(roster, "_claude_entries", side_effect=observe_entries(
                roster._claude_entries)):  # pyright: ignore[reportPrivateUsage]
            renamed = self._entries()
        with self.subTest(director_registry_name="Wrong registry title"):
            self.assertEqual(renamed["unit-one"].name, "unit-one")
            self.assertEqual(claude_rows[WORKER_SESSION_ID].director, roster.UnderDirector(
                "unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))

    def test_text_nests_workers_and_marks_this_session(self) -> None:
        result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        runner = next(line for line in lines if "[session:acd50bad-5afa-5d3d-b155-3766b7d113e0]" in line)
        director = next(line for line in lines if "[session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b]" in line)
        worker = next(line for line in lines if "[worker:unit-one/alpha-impl]" in line)
        self.assertIn("(you)", runner)
        self.assertEqual(len(director) - len(director.lstrip()), len(runner) - len(runner.lstrip()) + 2)
        self.assertEqual(len(worker) - len(worker.lstrip()), len(director) - len(director.lstrip()) + 2)
        self.assertLess(lines.index(runner), lines.index(director))
        self.assertLess(lines.index(director), lines.index(worker))
        orphan_header = f"  director not running: {self.runs / 'orphan-run'}"
        self.assertIn(orphan_header, lines)
        orphan_worker = next(line for line in lines if "worker:orphan-unit/orphan-impl" in line)
        self.assertTrue(orphan_worker.startswith("    "))
        self.assertTrue(any(line.strip() == "your Codex sessions" for line in lines))
        self.assertTrue(any("~/projects/unit-one" in line for line in lines))

    def test_claude_shell_status_shows_background_and_keeps_raw_data(self) -> None:
        session_id = "acd50bad-5afa-5d3d-b155-3766b7d113e0"
        self._write_session(str(os.getpid()), "runner", session_id,
                            self.home / "projects" / "runner", status="shell")
        self.assertEqual(self._entries()["alpha"].status, "shell")
        for here in (True, False):
            with self.subTest(here=here):
                result, lines, errors = self._run_main(here=here)
                self.assertEqual((result, errors), (0, []))
                runner = next(line for line in lines if f"[session:{session_id}]" in line)
                self.assertIn("claude, background", runner)
                self.assertNotIn("shell", runner)
                result, lines, errors = self._run_main("--json", here=here)
                self.assertEqual((result, errors), (0, []))
                report = cast(dict[str, object], json.loads("\n".join(lines)))
                entries = cast(list[dict[str, object]], report["entries"])
                entry = next(row for row in entries if row["address"] == f"session:{session_id}")
                self.assertEqual(entry["status"], "shell")
                result, lines, errors = self._run_main("--snapshot", here=here)
                self.assertEqual((result, errors), (0, []))
                snapshot = roster.RosterSnapshot.decode("\n".join(lines)).result
                self.assertEqual(next(row for row in snapshot.entries if row.name == "alpha").status, "shell")

    def test_socketless_director_keeps_worker_links_and_json_addresses(self) -> None:
        self._write_session(
            "director-new", "director", "17e44ec4-3e24-5ae1-b10d-5cf30de9455b", self.home / "projects" / "unit-one",
            updated=20, with_socket=False,
        )
        entries = self._entries()
        self.assertEqual(entries["unit-one"].reach, roster.CannotReceiveMessages("no messaging socket"))
        for name in ("alpha-test", "alpha-impl", "review:adversary"):
            with self.subTest(worker=name):
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b",
                ))
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[roster.SerializedRosterEntry], document["entries"])
        by_name = {row["name"] or row["address"]
                   or "review:" + row["slot_role"].removeprefix("review (").removesuffix(")"): row for row in rows}
        self.assertEqual(by_name["unit-one"]["address"], "")
        for name in ("alpha-test", "alpha-impl", "review:adversary"):
            with self.subTest(serialized_worker=name):
                self.assertEqual(by_name[name]["director_address"], "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b")

    def test_socketless_director_nests_its_workers_in_full_and_filtered_text(self) -> None:
        self._write_session(
            "director-new", "director", "17e44ec4-3e24-5ae1-b10d-5cf30de9455b", self.home / "projects" / "unit-one",
            updated=20, with_socket=False,
        )
        for arguments in ((), ("--kind", "codex")):
            with self.subTest(arguments=arguments):
                result, lines, errors = self._run_main(*arguments)
                self.assertEqual((result, errors), (0, []))
                directors = [line for line in lines if "[session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b]" in line]
                self.assertEqual(len(directors), 1)
                director = directors[0]
                workers = ("alpha-impl",) if arguments else ("alpha-impl", "alpha-test")
                for name in workers:
                    address = f"worker:unit-one/{name}" if name == "alpha-impl" else f"session:{WORKER_SESSION_ID}"
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
        runner = replace(entries["alpha"], reach=roster.CannotReceiveMessages("no messaging socket"))
        fixture = roster.Roster([runner, entries["unit-one"], entries["alpha-impl"]], [])
        for arguments in ([], ["--kind", "codex"]):
            with self.subTest(arguments=arguments):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(roster, "roster", return_value=fixture), \
                        redirect_stdout(output), redirect_stderr(errors):
                    result = roster.main(["--here", *arguments])
                lines = output.getvalue().splitlines()
                self.assertEqual((result, errors.getvalue()), (0, ""))
                runners = [line for line in lines if "[session:acd50bad-5afa-5d3d-b155-3766b7d113e0]" in line]
                self.assertEqual(len(runners), 1)
                director = next(line for line in lines if "[session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b]" in line)
                worker = next(line for line in lines if "[worker:unit-one/alpha-impl]" in line)
                self.assertEqual(len(director) - len(director.lstrip()),
                                 len(runners[0]) - len(runners[0].lstrip()) + 2)
                self.assertEqual(len(worker) - len(worker.lstrip()),
                                 len(director) - len(director.lstrip()) + 2)
                self.assertLess(lines.index(runners[0]), lines.index(director))
                self.assertLess(lines.index(director), lines.index(worker))
                if arguments:
                    self.assertIn("(context)", runners[0])

    def test_roster_uses_staff_workers_and_leaves_old_ledger_files_unchanged(self) -> None:
        current = self.director_run / "workers"
        previous = self.director_run / "seats"
        _ = current.replace(previous)
        old_time = 1_700_000_000
        os.utime(previous, (old_time, old_time))
        before = previous.read_bytes(), previous.stat().st_mtime_ns
        worker = self._entries()["alpha-test"]
        self.assertEqual(worker.assignment, roster.WorkerAssignment("test", str(self.director_run), roster.AssignmentUnrecorded(), "test"))
        self.assertFalse(current.exists())
        self.assertEqual((previous.read_bytes(), previous.stat().st_mtime_ns), before)
        self.assertEqual(self._entries()["alpha-test"].assignment, worker.assignment)
        native = fixture_runtime_id("late-worker")
        self._write_session("late-worker", "late-worker", native, self.home / "late-project")
        self.assertEqual(self._entries()[f"session:{native}"].role, "freestanding")
        with previous.open("a", encoding="utf-8") as ledger:
            _ = ledger.write(f"{native}\tlate-impl\n")
        modified = previous.read_bytes(), previous.stat().st_mtime_ns
        self.assertEqual(self._entries()[f"session:{native}"].role, "freestanding")
        _ = fixture_worker("late-impl", unit_membership("alpha", "unit-one").id, runtime_id=native)
        late_worker = self._entries()["late-impl"]
        self.assertEqual(late_worker.role, "worker")
        self.assertEqual(late_worker.assignment, roster.WorkerAssignment("impl", str(self.director_run), roster.AssignmentUnrecorded(), "impl"))
        self.assertEqual(late_worker.director, worker.director)
        self.assertFalse(current.exists())
        self.assertEqual((previous.read_bytes(), previous.stat().st_mtime_ns), modified)

    def test_switched_worker_resolves_once_before_and_after_a_transcript_and_after_returning_to_codex(self) -> None:
        from scripts.production import broadcast

        native = staff.native_id(fixture_runtime_id("switched-claude-session"))
        unit = unit_membership("alpha", "unit-one")
        child = staff.ensure_worker(native, "switched", unit.id)
        native = child.current_session
        self._write_session("switched", "switched-claude", native, self.home / "switched")
        self.assertEqual(sessions.transcript_locations(native), [])
        for transcript in (False, True):
            with self.subTest(transcript=transcript):
                if transcript:
                    fixture_session(native, staff.ClaudeBackend())
                with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
                    observed = roster.roster(os.environ, roster.ThisMachine())
                membership = staff.snapshot()
                matches = send.matching_recipients(f"session:{native}", send.LocalOnly(), observed,
                                                   membership=membership)
                self.assertIsInstance(matches, send.RecipientMatches)
                assert isinstance(matches, send.RecipientMatches)
                self.assertEqual(len(matches.entries), 1)
                recipient = matches.entries[0]
                self.assertEqual((recipient.kind, recipient.role, recipient.session),
                                 ("claude", "worker", roster.ClaudeSession(native)))
                self.assertEqual(recipient.reach, roster.Messageable(f"session:{native}"))
                route = send.resolve_recipient(f"session:{native}", send.LocalOnly(), observed,
                                               membership=membership)
                self.assertEqual(route, send.PaneWorkerRecipient(child))
                self.assertTrue(broadcast.selects(broadcast.Workers(), recipient))
                self.assertTrue(broadcast.selects(broadcast.WorkersOfUnit(
                    "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b", unit.name, recipient.machine), recipient))
                self.assertFalse(roster.is_top_level(recipient))
        codex = staff.native_id(fixture_runtime_id("switched-back-to-codex"))
        _ = staff.update_current_session(native, codex)
        fixture_session(codex, staff.CodexBackend(self.root / "return-codex"))
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            observed = roster.roster(os.environ, roster.ThisMachine())
        matches = send.matching_recipients(f"session:{codex}", send.LocalOnly(), observed, membership=staff.snapshot())
        self.assertIsInstance(matches, send.RecipientMatches)
        assert isinstance(matches, send.RecipientMatches)
        self.assertEqual(len(matches.entries), 1)
        self.assertEqual((matches.entries[0].kind, matches.entries[0].role, matches.entries[0].thread),
                         ("codex", "worker", roster.CodexThread(codex)))

    def test_background_id_prefix_places_claude_worker_in_its_run(self) -> None:
        worker = self._entries()["alpha-test"]
        self.assertTrue(WORKER_SESSION_ID.startswith("12345678"))
        self.assertEqual(
            (worker.role, worker.director, worker.assignment),
            (
                "worker",
                roster.UnderDirector("unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"),
                roster.WorkerAssignment("test", str(self.director_run), roster.AssignmentUnrecorded(), "test"),
            ),
        )

    def test_codex_review_uses_staff_role_and_dispatch_lens(self) -> None:
        original = self._entries()
        entry = original["alpha-research"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "research"),
        )
        history = self.root / "damaged-dispatch-history.jsonl"
        _ = history.write_bytes(b"\xff\n")
        state_path = self.director_run / unit_runs.STATE_FILE
        state = cast(dict[str, object], json.loads(state_path.read_text()))
        state["history_file"] = str(history)
        write_text_atomic(state_path, json.dumps(state))
        damaged = self._entries()
        expected = dict(original)
        expected["alpha-research"] = replace(entry, assignment=roster.WorkerAssignment(
            "review", str(self.director_run), "review", "research"))
        self.assertEqual(damaged, expected)
        member = staff.local_snapshot().by_current_session("76d3172a-4806-51c4-89b6-e328af9f6367")
        assert member is not None
        _ = staff.set_worker_role(member.id, staff.KnownWorkerRole("fix"))
        self.assertEqual(self._entries()["alpha-research"].assignment,
                         roster.WorkerAssignment("fix", str(self.director_run), "fix", "research"))

    def test_codex_review_without_lens_uses_its_role(self) -> None:
        entry = self._entries()["alpha-review-plain"]
        self.assertEqual(
            entry.assignment,
            roster.WorkerAssignment("review", str(self.director_run), "review", "plain"),
        )


    def test_codex_worker_reads_its_role_from_staff_without_a_roster_role(self) -> None:
        self.assertEqual(
            self._entries()["alpha-impl"].assignment,
            roster.WorkerAssignment("impl", str(self.director_run), "impl", "impl"),
        )
        row = staff.snapshot().by_current_session("7aa082fc-e724-5e46-8846-bd68e7eb3f46")
        assert row is not None
        for role in ("fix", "test"):
            with self.subTest(staff_role=role):
                _ = staff.set_worker_role(row.id, staff.KnownWorkerRole(cast(staff.WorkerRole, role)))
                self.assertEqual(self._entries()["alpha-impl"].assignment,
                                 roster.WorkerAssignment(role, str(self.director_run),
                                                         cast(roster.WorkerRole, role), "impl"))
        for contents in (b"", b"\xff"):
            with self.subTest(history=contents):
                _ = (agents_log.state_dir() / "agents.jsonl").write_bytes(contents)
                self.assertEqual(self._entries()["alpha-impl"].assignment,
                                 roster.WorkerAssignment("test", str(self.director_run), "test", "impl"))

    def test_a_roster_takes_folder_and_status_from_the_registry_read_it_receives(self) -> None:
        registry = sessions.read_registry()
        solo = next(item.record for item in registry.entries if item.record["name"] == "solo-director")
        (self.sessions / "solo.json").unlink()

        found = roster.roster(os.environ, roster.ThisMachine(), registry=registry)

        entry = next(entry for entry in found.entries if entry.name == "solo-director")
        self.assertEqual((entry.cwd, entry.status), (solo.get("cwd"), "waiting"))

    def test_a_send_by_name_stops_when_the_session_folder_cannot_be_listed(self) -> None:
        missing = self.root / "no-session-folder"
        membership = staff.snapshot()
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(missing)}):
            result = roster.roster(os.environ, roster.ThisMachine())
        self.assertIn(f"Claude session records: cannot list {missing}", result.problems)
        with mock.patch.object(roster, "peer_roster") as peer:
            found = send.matching_recipients("peer-only", send.LocalThenPeer(), result, membership=membership)
        self.assertIsInstance(found, send.LocalRosterIncomplete)
        peer.assert_not_called()

    def test_a_run_with_an_unreadable_owner_is_a_problem_for_that_run_only(self) -> None:
        run = self.runs / "lost-owner-run"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({unit_runs.OWNER_FIELD: "missing-unit"}))
        lost = unit_labels.LiveRun(run, unit_labels.NoRunningDirector())
        problems: list[str] = []

        found = roster._worker_ledger(lost, problems, staff.snapshot())  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(found, [])
        self.assertEqual(problems, [f"{run}: run owner missing-unit: unit staff row is unavailable"])

    def test_a_stopped_run_with_an_unreadable_owner_is_a_problem_for_that_run_only(self) -> None:
        run = self.runs / "lost-owner-run"
        run.mkdir()
        self._write_pane_workers(run, {"lost-worker": {"thread_id": fixture_runtime_id("lost-worker"), "status": "done", "role": "impl"}})
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({unit_runs.OWNER_FIELD: "missing-unit", "status": "active"}))
        _ = (run / unit_runs.HEARTBEAT).write_text("live\n", encoding="utf-8")

        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())

        self.assertIn(f"{run}: run owner missing-unit: unit staff row is unavailable", found.problems)
        self.assertIn("lost-worker", {entry.name for entry in found.entries})
        self.assertTrue(found.entries, found.problems)

    def test_a_roster_build_does_not_read_the_role_log(self) -> None:
        with mock.patch.object(agents_log, "assignments", wraps=agents_log.assignments) as read:
            entries = self._entries()

        self.assertGreater(sum(entry.role == "worker" for entry in entries.values()), 1)
        self.assertEqual(read.call_count, 0)

    def test_each_roster_shares_run_scans_across_worker_parents(self) -> None:
        self.assertGreater(len(session_staff.pane_parents(staff.snapshot())), 1)
        with mock.patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scan:
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    entries = self._entries()
                    self.assertGreater(sum(entry.role == "worker" for entry in entries.values()), 1)
                    self.assertEqual(scan.call_count, 2 * (attempt + 1))

    def test_message_participants_keep_the_full_roster_rows_and_select_one_worker(self) -> None:
        full = self._entries()
        sender, recipient = full["alpha-impl"], full["unit-one"]
        assert isinstance(sender.thread, roster.CodexThread)
        assert isinstance(recipient.session, roster.ClaudeSession)
        identities = frozenset((sender.thread.thread_id, recipient.session.session_id))
        record = staff.snapshot()
        worker = record.by_current_session(sender.thread.thread_id)
        assert worker is not None
        expected_parent = record.parent_of(worker)
        assert expected_parent is not None
        with (mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()),
              mock.patch.object(process_table, "snapshot", side_effect=AssertionError("unrelated reviewer process scan")),
              mock.patch.object(worker_panes, "observe_parents", wraps=worker_panes.observe_parents) as observe):
            focused = roster.roster(os.environ, roster.ThisMachine(), membership=record,
                                    participants=roster.MessageParticipants(identities))
        def selected(entries: list[roster.RosterEntry]) -> list[roster.RosterEntry]:
            return [entry for entry in entries
                    if isinstance(entry.thread, roster.CodexThread) and entry.thread.thread_id in identities
                    or isinstance(entry.session, roster.ClaudeSession) and entry.session.session_id in identities]
        self.assertEqual(selected(focused.entries), selected(list(full.values())))
        observe.assert_called_once()
        parents = cast(list[staff.StaffRow], observe.call_args.args[0])
        self.assertEqual([parent.id for parent in parents], [expected_parent.id])
        self.assertEqual(observe.call_args.kwargs["selection"], worker_panes.SelectedWorkerRows(frozenset((worker.id,))))


    def test_display_rename_keeps_record_keys_and_adds_a_structured_job(self) -> None:
        entry = replace(self._entry("identity-impl", "worker", "worker:run/identity-impl"),
                        showrunner=roster.ProductionWithoutRunningShowrunner("sdlc"),
                        director=roster.UnderDirector("roles", "director", "session:director"),
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
        shown = roster.ShownName(roster.ProductionPart("sdlc"), roster.UnitPart("roles"), job, "worker:run/identity")
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
                        director=roster.DirectsUnit("roles"))
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
                       director=roster.UnderDirector("roles", "director", "session:director"))
        for role, slot, expected in (("impl", "impl", "worker impl"), ("test", "test", "worker test"),
                                     ("review", "review", "code reviewer review"),
                                     ("review", "review2", "code reviewer review2")):
            with self.subTest(slot=slot):
                entry = replace(base, assignment=roster.WorkerAssignment(
                    role, "/run", roster.worker_role({"role": role}), slot))
                self.assertEqual(roster.shown_name(entry).render(), f"sdlc · roles · {expected}")
                self.assertEqual(entry.role, "worker")
                self.assertEqual(entry.reach, roster.Messageable("worker:run/arbitrary"))
        director = replace(base, role="unit director", director=roster.DirectsUnit("roles"),
                           assignment=roster.NotAWorker())
        self.assertEqual(roster.shown_name(director).render(), "sdlc · roles · unit director")
        runner = replace(director, role="showrunner", showrunner=roster.RunsProduction("sdlc"))
        self.assertEqual(roster.shown_name(runner).render(), "sdlc · showrunner")

    def test_shown_names_omit_missing_parts_and_freestanding_uses_its_address(self) -> None:
        entry = replace(self._entry("name-review", "worker", "worker:run/name-review"),
                        assignment=roster.WorkerAssignment("test", "/run", "test", "test"))
        self.assertEqual(roster.shown_name(entry).render(), "worker test")
        self.assertEqual(roster.shown_name(replace(entry, director=roster.UnderDirector(
            "roles", "director", "session:director"))).render(), "roles · worker test")
        self.assertEqual(roster.shown_name(replace(entry, showrunner=roster.ProductionWithoutRunningShowrunner(
            "sdlc"))).render(), "sdlc · worker test")
        free = replace(entry, role="freestanding", assignment=roster.NotAWorker())
        shown = roster.shown_name(free)
        self.assertIsInstance(shown.job, roster.JobUnassigned)
        self.assertEqual(shown.render(), "worker:run/name-review")
        native = "746a93a1-733c-4b53-9e98-000000000001"
        address = f"session:{native}"
        for updated, registry_name in enumerate(("", "mac"), start=1):
            with self.subTest(registry_name=registry_name):
                self._write_session(f"unowned-mac-{updated}", registry_name, native, self.home,
                                    status="idle", updated=updated)
                status, lines, errors = self._run_main()
                self.assertEqual((status, errors), (0, []))
                label = f"mac [{address}]" if registry_name else address
                self.assertTrue(any(line.strip() == f"freestanding {label} — claude, idle, freestanding session, ~"
                                    for line in lines))
                status, lines, errors = self._run_main("--json")
                self.assertEqual((status, errors), (0, []))
                document = cast(dict[str, object], json.loads("\n".join(lines)))
                rows = cast(list[dict[str, object]], document["entries"])
                row = next(row for row in rows if row["address"] == address)
                self.assertEqual(row["name"], "")
                self.assertEqual(row["shown_name"], registry_name or address)

    def test_unit_rename_and_transfer_change_only_shown_location(self) -> None:
        original = replace(self._entry("fixed-address", "worker", "worker:run/fixed-address", kind="codex"),
                           showrunner=roster.UnderShowrunner("sdlc", "runner", "session:runner"),
                           director=roster.UnderDirector("roles", "director", "session:director"),
                           assignment=roster.WorkerAssignment("impl", "/run", "impl", "impl"),
                           thread=roster.CodexThread("fixed-thread"))
        renamed = replace(original, director=roster.UnderDirector("records", "director", "session:director"))
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


    def test_bad_durable_binding_leaves_a_named_peer_send_quiet(self) -> None:
        _ = write_empty_binding_record(self.root)
        peer = self._entry('natedev', 'freestanding', 'session:peer-session')
        os.environ['SSH_SNAPSHOT'] = roster.RosterSnapshot(roster.Roster([peer], [])).encode()
        output, errors = io.StringIO(), io.StringIO()
        doc = self.root / 'peer-production.md'
        _ = doc.write_text('# Peer production\n')
        local = staff.local_snapshot()
        membership = staff.StaffSnapshot((*local.rows, staff.StaffRow(staff.StaffId('peer-session'), 'showrunner',
            'natedev', staff.StaffRoot(), current_session=staff.StaffId('peer-session'),
            doc=staff.LiveDoc(roster.remote.local_machine(), doc))))
        with mock.patch.object(staff, 'snapshot', return_value=membership), \
                mock.patch.object(send, 'STATE', self.root / 'send-state'), \
                mock.patch.object(send, 'remote', return_value=send.Result('sent', 'fixture peer')) as deliver, \
                mock.patch.object(send, 'user', side_effect=AssertionError('no real notification')), \
                redirect_stdout(output), redirect_stderr(errors):
            code = send.main(['--to', 'natedev', '--from', 'fixture-reviewer', '--text', 'owner update'])
        self.assertEqual(code, 0, output.getvalue() + errors.getvalue())
        self.assertEqual(output.getvalue(), 'SENT: fixture peer\n')
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(deliver.call_count, 1)
        forwarded = cast(send.Message, deliver.call_args.args[0])
        self.assertEqual(forwarded.to, 'session:peer-session')
        self.assertEqual(forwarded.text, 'owner update')

    def test_durable_warnings_survive_snapshots_and_do_not_change_roster_exit_status(self) -> None:
        runner = fixture_member("showrunner", name="alpha")
        _ = staff.rename(runner.id, "runner")
        bad = write_empty_binding_record(self.root)
        for row, reason in (("invalid-repository", "missing or invalid repository"),
                            ("missing-owner", "checkout owner is absent: missing-owner")):
            with self.subTest(row=row):
                if row == "missing-owner":
                    fields = cast(dict[str, object], json.loads(bad.read_text()))
                    del fields["repository"]
                    fields["repository_owner"] = "missing-owner"
                    _ = bad.write_text(json.dumps(fields))
                code, output, errors = self._run_main('--json')
                self.assertEqual(code, 0, errors)
                report = cast(dict[str, object], json.loads('\n'.join(output)))
                self.assertEqual(report['problems'], [])
                self.assertEqual(report['warnings'], [f'durable role {bad}: {bad}: worker state unreadable: {reason}'])
                self.assertEqual(errors, [f'could not read: warning: durable role {bad}: {bad}: worker state unreadable: {reason}'])
                result = roster.roster(os.environ, roster.ThisMachine())
                self.assertIn("runner", {entry.name for entry in result.entries})
                decoded = roster.RosterSnapshot.decode(roster.RosterSnapshot(result).encode()).result
                self.assertEqual(decoded.warnings, result.warnings)
                self.assertEqual(decoded.problems, [])

    def test_worker_titles_use_worker_parents_and_fall_back_when_the_design_is_unreadable(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000099"
        unit = unit_membership("sdlc", "roles")
        runner = showrunner_relationship("sdlc")
        directory = str(design_roles.design_directory(design_id))
        for parent, prefix in ((unit, "sdlc · roles"), (runner, "sdlc")):
            designer = fixture_worker("design-address", parent.id)
            reviewer = fixture_worker("review-address", parent.id)
            design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 1),
                "fixture-repo", str(self.root), str(self.root / "design.md"), "design",
                "2026-10-21T12:00:00+00:00", design_roles.DesignUnbound(),
                (design_roles.RoleInstance("designer", "design-address", design_roles.RoleOpen(designer.id)),
                 design_roles.RoleInstance("reviewer", "review-address", design_roles.RoleOpen(reviewer.id))))
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            for held_role, name, job in (("designer", "design-address", "designer"),
                                         ("reviewer", "review-address", "design reviewer")):
                with self.subTest(parent=parent, role=held_role):
                    shown = roster.worker_shown_name(directory, name, {"held_design_id": design_id, "held_role": held_role})
                    self.assertEqual(shown.render(), f"{prefix} · {job}")
                    self.assertEqual(shown.address, name)
        _ = (design_roles.design_directory(design_id) / "design.json").write_text("broken JSON")
        self.assertEqual(roster.worker_shown_name(directory, "review-address", {
            "held_design_id": design_id, "held_role": "reviewer"}).render(), "review-address")

    def test_docless_row_keeps_repeated_worker_title_reads_silent(self) -> None:
        showrunner = fixture_member("showrunner", name="docless")
        owner = fixture_member("unit", name="docless", parent=showrunner.id)
        run = self.root / "unplaced-run"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({unit_runs.OWNER_FIELD: owner.id}))
        errors = io.StringIO()
        with redirect_stderr(errors):
            for _ in range(2):
                self.assertEqual(roster.worker_shown_name(
                    str(run), "unplaced-worker", {"role": "impl"}).render(),
                    "unplaced-worker")
        self.assertEqual(errors.getvalue(), "")







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
        run_dir = str(self.root / "held-design")
        base = replace(self._entry("unrelated-name", "worker", "worker:held/arbitrary", kind="codex"),
                       showrunner=roster.ProductionWithoutRunningShowrunner("sdlc"),
                       director=roster.UnderDirector("roles", "director", "session:director"))
        entries = [
            replace(base, role="astra-designer", name="design-address",
                    assignment=roster.HeldDesignRole("designer", run_dir),
                    reach=roster.Messageable("worker:held/design-address"),
                    thread=roster.CodexThread("design-thread")),
            replace(base, role="astra-reviewer", name="review-address",
                    assignment=roster.HeldDesignRole("reviewer", run_dir),
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
                                             "session_dir": run_dir}
                legacy = roster.RosterSnapshot.decode(json.dumps(document)).result.entries[index]
                self.assertEqual(legacy, expected)

        with mock.patch.object(roster, "roster", return_value=roster.Roster(entries, [])):
            result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        rendered = cast(dict[str, object], json.loads("\n".join(lines)))
        flat = cast(list[dict[str, object]], rendered["entries"])
        for row, expected, job in zip(flat, entries, ("designer", "design reviewer"), strict=True):
            self.assertEqual((row["name"], row["role"], row["slot_role"], row["session_dir"]),
                             (expected.name, expected.role, expected.role, run_dir))
            self.assertEqual(row["address"], f"worker:held/{expected.name}")
            self.assertEqual(row["shown_name"], f"sdlc · roles · {job}")

    def test_own_production_and_unit_are_not_links_to_blank_sessions(self) -> None:
        entries = self._entries()

        self.assertEqual(entries["alpha"].showrunner, roster.RunsProduction("alpha"))
        self.assertEqual(entries["unit-one"].director, roster.DirectsUnit("unit-one"))
        self.assertNotIsInstance(entries["alpha"].showrunner, roster.UnderShowrunner)
        self.assertNotIsInstance(entries["unit-one"].director, roster.UnderDirector)
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
        by_name = {cast(str, row["name"] or row["address"]
                        or "review:" + cast(str, row["slot_role"]).removeprefix("review (").removesuffix(")")): row
                   for row in rows}
        self.assertEqual(by_name["alpha"]["shown_name"], "alpha · showrunner")
        self.assertEqual(by_name["unit-one"]["shown_name"], "alpha · unit-one · unit director")
        for name, entry in self._entries().items():
            self.assertEqual(by_name[name]["name"], entry.name)
            self.assertEqual(by_name[name]["role"], entry.role)
            self.assertEqual(by_name[name]["shown_name"], roster.shown_name(entry).render())
        self.assertEqual(
            (
                by_name["alpha"]["production"],
                by_name["alpha"]["showrunner"],
                by_name["alpha"]["showrunner_address"],
            ),
            ("alpha", "", ""),
        )
        self.assertEqual(
            (
                by_name["unit-one"]["unit"],
                by_name["unit-one"]["director"],
                by_name["unit-one"]["director_address"],
            ),
            ("unit-one", "", ""),
        )



    def test_workers_keep_their_unit_title_after_the_director_stops(self) -> None:
        _ = (self.director_run / "progress_history_state.json").write_text(json.dumps({
            "production": "alpha", "unit": "unit-one",
            "working_dir": str(self.home / "projects" / "unit-one"),
        }))
        self._publish_workers(self.director_run / unit_runs.STATE_FILE, {
            "alpha-impl": {"thread_id": "7aa082fc-e724-5e46-8846-bd68e7eb3f46", "status": "running", "role": "impl",
                           "launcher_pid": os.getpid()},
            "alpha-fix": {"thread_id": "d6c57ffb-f619-567d-b06c-1a1f7791b8af", "status": "done", "role": "fix"},
        })
        _ = (self.director_run / "board.log").write_text("2026-10-29T12:00:00Z [test] handoff: role=impl fixture\n")
        before = self._entries()
        (self.sessions / "director.json").unlink()
        (self.director_run / "heartbeat.log").touch()
        entries = self._entries()
        for name in ("alpha-impl", "alpha-test"):
            with self.subTest(worker=name):
                entry = entries[name]
                self.assertEqual(roster.shown_name(entry), roster.shown_name(before[name]))
                self.assertEqual(entry.director, roster.UnitWithoutRunningDirector("unit-one"))
                restored = roster.RosterSnapshot.decode(
                    roster.RosterSnapshot(roster.Roster([entry], [])).encode()).result.entries[0]
                self.assertEqual(restored, entry)
        self.assertEqual(entries["alpha-fix"].reach, roster.Messageable("worker:unit-one/alpha-fix"))
        result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        worker = next(line.strip() for line in lines if "[worker:unit-one/alpha-impl]" in line)
        self.assertTrue(worker.startswith("alpha · unit-one · worker impl [worker:unit-one/alpha-impl]"), worker)
        self.assertNotIn("worker worker", worker)
        self.assertTrue(any("director not running:" in line for line in lines))



    def test_newer_dead_record_does_not_supply_live_session_details(self) -> None:
        dead_cwd = self.home / "projects" / "dead-resume"
        self._write_session(
            "free-dead", "free-claude", "abd1a227-f410-5c76-bc96-a0c77c0f3ef0", dead_cwd,
            status="dead status", updated=3, pid=999_999_999,
        )
        entry = self._entries()["session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"]
        self.assertEqual((entry.cwd, entry.status), (
            str(self.home / "projects" / "free"), "ready",
        ))

    def test_a_relay_named_like_a_session_is_left_out_of_the_roster(self) -> None:
        # send.py's relay is a newer `claude -p` run started with `-n free-claude`.
        self._write_session("relay", "free-claude", "d2d1a6c7-68f1-5f01-b1af-3a7622ca03c6", self.home / "projects" / "relay",
                            status="relaying", updated=9, entrypoint="sdk-cli")
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        named = [entry.session for entry in found.entries
                 if entry.session == roster.ClaudeSession("abd1a227-f410-5c76-bc96-a0c77c0f3ef0")]
        self.assertEqual(named, [roster.ClaudeSession("abd1a227-f410-5c76-bc96-a0c77c0f3ef0")])

    def test_a_recorded_relay_is_absent_even_without_its_entrypoint(self) -> None:
        relay_name = relay_session_name("free-claude")
        self._write_session("relay", relay_name, "d2d1a6c7-68f1-5f01-b1af-3a7622ca03c6", self.home / "projects" / "relay",
                            status="relaying", updated=9, entrypoint="", pid=os.getppid())
        write_relay_record(self.root / "state/message", os.getppid(), "free-claude")
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=self._daemon_sessions()):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertNotIn("d2d1a6c7-68f1-5f01-b1af-3a7622ca03c6", [entry.session.session_id for entry in found.entries
                                       if isinstance(entry.session, roster.ClaudeSession)])
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())):
            self.assertEqual(send.resolve_recipient("session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0", send.LocalOnly(), found),
                             send.ClaudeRecipient("session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"))
        self.assertEqual([entry for entry in found.entries if entry.name == relay_name], [])

    def test_socketless_record_with_another_process_start_is_absent_from_the_roster(self) -> None:
        self._write_session(
            "reused", "phantom", "bbf424a8-e952-5b7a-8a50-9249787bdb50", self.home / "projects" / "ended",
            with_socket=False, proc_start="another start",
        )
        entries = self._entries()
        self.assertNotIn("phantom", list(entries))
        self.assertFalse(any(entry.session == roster.ClaudeSession("bbf424a8-e952-5b7a-8a50-9249787bdb50") for entry in entries.values()))

    def test_running_session_without_socket_retains_identity_and_has_no_address(self) -> None:
        self._write_session(
            "socketless", "socketless", "3970025f-e6e7-5c02-b247-0f4a0753902b", self.home / "projects" / "socketless",
            with_socket=False,
        )
        entry = self._entries()["session:3970025f-e6e7-5c02-b247-0f4a0753902b"]
        self.assertEqual(entry.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(entry.session, roster.ClaudeSession("3970025f-e6e7-5c02-b247-0f4a0753902b"))
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        row = next(row for row in rows if row["cwd"] == str(self.home / "projects/socketless"))
        self.assertEqual(row["address"], "")
        self.assertEqual(set(row), ENTRY_FIELDS)

    def test_sender_uses_newest_claude_identity_even_without_a_socket(self) -> None:
        self._write_session(
            "free-socketless", "newest-free", "abd1a227-f410-5c76-bc96-a0c77c0f3ef0", self.home / "projects" / "resumed",
            status="waiting", updated=20, with_socket=False,
        )
        entries = self._entries()
        entry = entries["session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"]
        self.assertNotIn("newest-free", entries)
        self.assertEqual((entry.reach, entry.cwd, entry.status), (
            roster.CannotReceiveMessages("no messaging socket"),
            str(self.home / "projects" / "resumed"), "waiting",
        ))
        self.assertTrue(roster.is_sender(entry, roster.ClaudeSession("abd1a227-f410-5c76-bc96-a0c77c0f3ef0")))
        self.assertFalse(roster.is_sender(entry, roster.ClaudeSession("e85a5b38-58dc-572a-8c07-b0fee760dba0")))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abd1a227-f410-5c76-bc96-a0c77c0f3ef0"}):
            result, lines, errors = self._run_main(native_identity="abd1a227-f410-5c76-bc96-a0c77c0f3ef0")
        self.assertEqual((result, errors), (0, []))
        self.assertTrue(any("newest-free [session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0] (you)" in line for line in lines))

    def test_showrunner_socket_identity_beats_a_duplicate_display_name(self) -> None:
        self._write_session(
            "runner-imposter", "runner", "b9ec3232-747a-57d3-8f9f-6a271a1096b4",
            self.home / "projects" / "imposter", updated=20,
        )
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            entries = roster.roster(os.environ, roster.ThisMachine()).entries
        real = next(
            entry for entry in entries
            if entry.reach == roster.Messageable("session:acd50bad-5afa-5d3d-b155-3766b7d113e0")
        )
        imposter = next(
            entry for entry in entries
            if entry.reach == roster.Messageable("session:b9ec3232-747a-57d3-8f9f-6a271a1096b4")
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
        reviewer = entries["review:adversary"]
        self.assertEqual(
            (reviewer.kind, reviewer.role, reviewer.assignment, reviewer.reach, reviewer.cwd),
            (
                "codex", "worker",
                roster.WorkerAssignment("review (adversary)", str(self.director_run), "review", "adversary"),
                roster.CannotReceiveMessages("blind reviewer"), "/review/cwd",
            ),
        )
        blind_reviewer = entries["review:review"]
        for item in (reviewer, blind_reviewer):
            with self.subTest(assignment=item.assignment):
                self.assertEqual(item.name, "")
        self.assertEqual(
            (blind_reviewer.assignment, blind_reviewer.reach),
            (
                roster.WorkerAssignment("review", str(self.director_run), "review", "review"),
                roster.CannotReceiveMessages("blind reviewer"),
            ),
        )
        self.assertNotIn("contract reviewer", entries)

        identity = fixture_runtime_id("native-reviewer")
        self._write_session("native-reviewer", "Wrong reviewer registry title", identity,
                            self.root / "review", pid=991101)
        write_text_atomic(self.ps_state,
                          f"700 {os.getpid()} bash bash /scripts/agents/agent_exec.sh\n"
                          + "991101 700 claude claude\n")
        running_session = session_records.running_session

        def fixture_running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991101 or running_session(record)

        for owned in (False, True):
            with self.subTest(native_reviewer_owned=owned):
                if owned:
                    _ = fixture_member("producer", name="Staff reviewer", runtime_id=identity)
                with mock.patch.object(session_records, "running_session", side_effect=fixture_running):
                    found = roster.roster(os.environ, roster.ThisMachine())
                reviewers = [entry for entry in found.entries
                             if isinstance(entry.assignment, roster.WorkerAssignment)
                             and entry.assignment.slot in ("adversary", "review")]
                self.assertEqual(len(reviewers), 2)
                self.assertEqual([entry.name for entry in reviewers],
                                 ["Staff reviewer", "Staff reviewer"] if owned else ["", ""])

    def test_the_roster_recognizes_the_simplify_status_writer(self) -> None:
        from scripts.lib.atomic_file import write_text_atomic

        write_text_atomic(self.director_run / "review_status_adversary", "reviewing\n")
        with mock.patch.object(os, "readlink", return_value="/review/cwd"):
            entries = self._entries()
        self.assertEqual(entries["review:adversary"].status, "running")

    def test_review_status_in_a_phase_directory_finds_its_reviewer(self) -> None:
        phase = self.director_run / "phases" / "4"
        phase.mkdir(parents=True)
        for name in ("review_status_adversary", "review_pid_adversary"):
            _ = (self.director_run / name).rename(phase / name)
        with mock.patch.object(os, "readlink", return_value="/review/cwd"):
            entries = self._entries()
        reviewer = entries["review:adversary"]
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
        self.assertNotIn("review:review", reviewing)
        self.assertNotIn("review:adversary", reviewing)
        self.assertIn("alpha-research", reviewing)

        _ = self.ps_state.write_text(processes)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()):
            running = self._entries()
        for name in ("review:review", "review:adversary"):
            with self.subTest(reviewer=name):
                self.assertEqual(running[name].status, "running")
                self.assertEqual(running[name].account, roster.AccountLabel("codex2"))


    def test_kind_codex_keeps_only_needed_claude_context(self) -> None:
        result, lines, errors = self._run_main("--kind", "codex")
        self.assertEqual((result, errors), (0, []))
        runner = next(line for line in lines if "[session:acd50bad-5afa-5d3d-b155-3766b7d113e0]" in line)
        director = next(line for line in lines if "[session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b]" in line)
        self.assertIn("(context)", runner)
        self.assertIn("(context)", director)
        self.assertEqual(runner.count("(context)"), 1)
        self.assertEqual(director.count("(context)"), 1)
        self.assertTrue(any("[worker:unit-one/alpha-impl]" in line for line in lines))
        self.assertFalse(any("claude-worker" in line for line in lines))
        self.assertFalse(any("[session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0]" in line for line in lines))
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
        by_name = {cast(str, row["name"] or row["address"]
                        or "review:" + cast(str, row["slot_role"]).removeprefix("review (").removesuffix(")")): row
                   for row in rows}
        self.assertEqual(next(row for row in rows if row["slot_role"] == "review (adversary)" and not row["thread"])["address"], "")
        for field in (
            "production", "showrunner", "showrunner_address", "unit", "director",
            "director_address", "slot_role", "session_dir",
        ):
            self.assertEqual(by_name["session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"][field], "")

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
        self.assertEqual(entries["alpha-impl"].thread, roster.CodexThread("7aa082fc-e724-5e46-8846-bd68e7eb3f46"))
        self.assertEqual(entries["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"].thread, roster.CodexThread("eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"))
        for name in ("alpha", "alpha-test"):
            self.assertEqual(entries[name].thread, roster.NoCodexThread())
        result, lines, errors = self._run_main("--json")
        self.assertEqual((result, errors), (0, []))
        document = cast(dict[str, object], json.loads("\n".join(lines)))
        rows = cast(list[dict[str, object]], document["entries"])
        by_name = {cast(str, row["name"] or row["address"]
                        or "review:" + cast(str, row["slot_role"]).removeprefix("review (").removesuffix(")")): row
                   for row in rows}
        self.assertEqual(by_name["alpha-impl"]["thread"], "7aa082fc-e724-5e46-8846-bd68e7eb3f46")
        self.assertEqual(by_name["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"]["thread"], "eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539")
        for name in ("alpha", "alpha-test"):
            self.assertIsNone(by_name[name]["thread"])

    def test_codex_worker_identity_marks_the_worker_and_not_its_inherited_director(self) -> None:
        with mock.patch.dict(os.environ, {
            "CODEX_THREAD_ID": "7aa082fc-e724-5e46-8846-bd68e7eb3f46", "CLAUDE_CODE_SESSION_ID": "17e44ec4-3e24-5ae1-b10d-5cf30de9455b",
        }):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(len([line for line in lines if "(you)" in line]), 1)
        self.assertTrue(any("[worker:unit-one/alpha-impl] (you)" in line for line in lines))
        self.assertFalse(any("[session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b] (you)" in line for line in lines))

    def test_daemon_codex_identity_marks_its_own_window(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"}):
            result, lines, errors = self._run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(len([line for line in lines if "(you)" in line]), 1)
        self.assertTrue(any("codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539 (you)" in line for line in lines))

    def test_plain_shell_marks_no_session(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            result, lines, errors = self._run_main(native_identity="")
        self.assertEqual((result, errors), (0, []))
        self.assertFalse(any("(you)" in line for line in lines))

    def test_sender_identity_has_frozen_named_states_and_codex_precedence(self) -> None:
        with mock.patch.object(staff, "runtime_id", return_value=""):
            self.assertEqual(roster.sender_identity({}), roster.NoSession())
        with mock.patch.object(staff, "runtime_id", return_value="claude"):
            self.assertEqual(roster.sender_identity({"CLAUDE_CODE_SESSION_ID": "claude"}),
                             roster.ClaudeSession("claude"))
        identity = roster.sender_identity({
            "CODEX_THREAD_ID": "codex", "CLAUDE_CODE_SESSION_ID": "inherited",
        })
        self.assertEqual(identity, roster.CodexThread("codex"))
        assert isinstance(identity, roster.CodexThread)
        with self.assertRaises(FrozenInstanceError):
            identity.thread_id = "changed"  # pyright: ignore[reportAttributeAccessIssue]

    def test_sender_identity_uses_its_exact_registry_when_the_claude_hint_is_missing_or_stale(self) -> None:
        from scripts.message import session_records as runtime_sessions

        identity = "019b3a40-4250-7000-8000-000000000199"
        started = runtime_sessions.process_start(os.getpid())
        assert started is not None
        self._write_session(str(os.getpid()), "current caller", identity, self.home,
                            pid=os.getpid(), proc_start=started)
        with mock.patch.object(runtime_sessions, "sessions_dir", return_value=self.sessions):
            for hint in ("", "019b3a40-4250-7000-8000-000000000198"):
                with self.subTest(inherited=hint):
                    self.assertEqual(roster.sender_identity({"CLAUDE_CODE_SESSION_ID": hint}),
                                     roster.ClaudeSession(identity))

    def test_labeled_session_without_live_claude_does_not_claim_other_panes_by_worktree(self) -> None:
        (self.sessions / "director.json").unlink()
        worktree = self.home / "projects" / "unit-one"
        for name, pane in (("other-pane", "helper:@2.%2"), ("outside-herdr", "")):
            self._write_session(name, name, f"sid-{name}", worktree, tmux=pane)
        entries = self._entries()
        for name in ("other-pane", "outside-herdr"):
            with self.subTest(session=name):
                self.assertEqual(entries[f"session:sid-{name}"].role, "freestanding")
                self.assertEqual(entries[f"session:sid-{name}"].director, roster.NoDirector())
                self.assertTrue(roster.is_top_level(entries[f"session:sid-{name}"]))
        self.assertEqual(self.ssh_calls(), [])

    def test_unit_without_marked_session_does_not_claim_a_worktree_session(self) -> None:
        _ = (self.root / "herdr-proc/991100/environ").write_bytes(b"HERDR_PANE_ID=w1:p1\0")
        worktree = self.home / "projects" / "unit-one"
        self._write_session(
            "helper", "helper", "a24eb3a2-e6ab-50ff-b5ff-654fb1ab9708", worktree,
            tmux="unmarked:@2.%2",
        )
        helper = self._entries()["session:a24eb3a2-e6ab-50ff-b5ff-654fb1ab9708"]
        self.assertEqual(helper.role, "freestanding")
        self.assertEqual(helper.director, roster.NoDirector())
        self.assertTrue(roster.is_top_level(helper))
        self.assertEqual(self.ssh_calls(), [])


    def test_socketless_director_in_marked_pane_keeps_unit_without_worktree_match(self) -> None:
        self._write_session(
            "director", "director", "17e44ec4-3e24-5ae1-b10d-5cf30de9455b", self.home / "projects" / "elsewhere",
            tmux="old-label:@1.%1", with_socket=False,
        )
        entries = self._entries()
        director = entries["unit-one"]
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))
        self.assertEqual(director.showrunner, roster.UnderShowrunner(
            "alpha", "alpha", "session:acd50bad-5afa-5d3d-b155-3766b7d113e0",
        ))
        for name in ("alpha-test", "alpha-impl", "review:adversary"):
            with self.subTest(worker=name):
                self.assertEqual(entries[name].director, roster.UnderDirector(
                    "unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b",
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
        self._write_session("director", "director", "17e44ec4-3e24-5ae1-b10d-5cf30de9455b", self.home / "elsewhere", with_socket=False, pid=991001, proc_start="fixture")
        _ = (self.root / "herdr-proc/991100/environ").write_bytes(b"HERDR_PANE_ID=w1:p2\0")
        self._herdr_process(991000, 0, ["herdr", "--session", "native", "server"])
        self._herdr_process(991001, 991000, ["claude"], HERDR_PANE_ID="w1:p1",
                            SHOWRUNNER_UNIT="alpha", SHOWRUNNER_UNIT_ID="unit-one")
        binary = self._herdr_fixture(self._herdr_sessions())
        running_session = session_records.running_session

        def running(record: sessions.SessionRecord) -> bool:
            return record["pid"] == 991001 or running_session(record)

        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                mock.patch.object(session_records, "running_session", side_effect=running):
            director = self._entries()["unit-one"]
        self.assertEqual(director.role, "unit director")
        self.assertEqual(director.director, roster.DirectsUnit("unit-one"))
        self.assertEqual(director.showrunner, roster.UnderShowrunner("alpha", "alpha", "session:acd50bad-5afa-5d3d-b155-3766b7d113e0"))
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))

    def test_interactive_codex_panes_are_appended_without_messaging_addresses(self) -> None:
        old, current = self.account_note(1), self.account_note(2)
        responses = self._herdr_sessions()
        responses["--session native agent list"]["stdout"] = json.dumps({"ok": True, "result": {"agents": [
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "working"},
            {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
            {"pane_id": "w1:p3", "agent": "codex", "agent_status": "idle",
             "agent_session": {"kind": "id", "value": "private-pane-thread"}},
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
            worker = entries["alpha-impl"].thread
            assert isinstance(worker, roster.CodexThread)
            environment = Path(os.environ["HERDR_PROC"]) / "991001" / "environ"
            _ = environment.write_bytes(environment.read_bytes() + f"\0CODEX_THREAD_ID={worker.thread_id}".encode())
            without_worker = self._entries()
        pane = entries["herdr native:w1:p1"]
        self.assertEqual((pane.kind, pane.role, pane.status, pane.cwd), ("codex", "freestanding", "working", str(self.home)))
        self.assertEqual(pane.reach, roster.CannotReceiveMessages("interactive Codex in a herdr pane"))
        self.assertEqual((pane.showrunner, pane.director, pane.assignment),
                         (roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker()))
        self.assertEqual(pane.account, roster.AccountLabel("codex1"))
        self.assertIsInstance(entries["herdr native:w1:p3"].account, roster.AccountUnread)
        reported = entries["herdr native:w1:p3"]
        self.assertEqual(reported.thread, roster.CodexThread("private-pane-thread"))
        self.assertEqual(reported.reach, roster.Messageable("codex:private-pane-thread"))
        self.assertEqual(reported.pane, herdr.HerdrPlace("native", "w1:p3"))
        self.assertIsInstance(reported.daemon, roster.NotDaemonSession)
        self.assertIn(mock.call(991001), read_home.call_args_list)
        self.assertNotIn(mock.call(991002), read_home.call_args_list)
        self.assertNotIn(mock.call(991003), read_home.call_args_list)
        self.assertEqual(entries["herdr native:w1:p3"].cwd, str(self.home))
        self.assertNotIn("herdr native:w1:p2", entries)
        self.assertNotIn("herdr native:w1:p1", without_worker)
        self.assertIn("herdr native:w1:p3", without_worker)

    def test_codex_pane_without_a_session_report_uses_its_process_identity(self) -> None:
        world = self.pane_world
        row = fixture_member("producer", name="process-pane")
        world.add(row, "w1:p5", family="codex")
        responses = self._herdr_sessions()
        responses["--session native agent list"]["stdout"] = json.dumps({"result": {"agents": [
            {"pane_id": "w1:p5", "agent": "codex", "agent_status": "idle"},
            {"pane_id": "w1:p6", "agent": "codex", "agent_status": "idle",
             "agent_session": {"kind": "id", "value": "reported-thread"}},
        ]}})
        responses["--session native pane list"] = {"code": 0, "stdout": json.dumps({
            "result": {"panes": list(world.locations.values())},
        })}
        process = world.result("pane", "process-info", "--pane", "w1:p5")
        outcomes: list[CommandResponse] = [
            {"code": 0, "stdout": json.dumps({"result": process})},
            {"code": 1, "stdout": "", "stderr": "private process read failed"},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                responses["--session native pane process-info --pane w1:p5"] = outcome
                binary = self._herdr_fixture(responses)
                with (mock.patch.object(herdr, "herdr_binary", return_value=binary),
                      mock.patch.object(herdr, "native_process", side_effect=world.native_process)):
                    found = roster.roster(os.environ, roster.ThisMachine())
                pane, reported = [entry for entry in found.entries if isinstance(entry.pane, herdr.HerdrPlace)
                                  and entry.pane.session == "native"]
                if outcome["code"] == 0:
                    self.assertEqual(pane.thread, roster.CodexThread(str(row.current_session)))
                    self.assertEqual(pane.reach, roster.Messageable(f"codex:{row.current_session}"))
                else:
                    self.assertIsInstance(pane.thread, roster.NoCodexThread)
                    self.assertEqual(pane.reach, roster.CannotReceiveMessages("interactive Codex in a herdr pane"))
                self.assertEqual(pane.pane, herdr.HerdrPlace("native", "w1:p5"))
                self.assertEqual(reported.thread, roster.CodexThread("reported-thread"))
                self.assertEqual(reported.reach, roster.Messageable("codex:reported-thread"))
        commands = [cast(list[str], json.loads(line)) for line in (self.root / "herdr.jsonl").read_text().splitlines()]
        self.assertNotIn(["--session", "native", "pane", "process-info", "--pane", "w1:p6"], commands)

    def test_a_herdr_failure_is_a_problem_and_keeps_the_other_roster_rows(self) -> None:
        binary = self._herdr_fixture({"default": {"code": 1, "stdout": json.dumps({
            "error": {"code": "closed", "message": "fixture herdr unavailable"}
        })}})
        with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertTrue(any(entry.name == "unit-one" for entry in found.entries))
        self.assertEqual(len(found.problems), 2)
        self.assertTrue(any("unit hosts for alpha" in problem for problem in found.problems))
        self.assertTrue(any("herdr sessions" in problem for problem in found.problems))
        self.assertTrue(all("fixture herdr unavailable" in problem for problem in found.problems))

    def test_an_unreadable_herdr_session_keeps_codex_rows_from_other_sessions(self) -> None:
        responses = self._herdr_sessions()
        sessions_response = {"sessions": [herdr.SessionEntry(name=name, running=True, default=False,
                            session_dir="/fixture", socket_path="/fixture/socket") for name in ("broken", "native")]}
        responses["--session default session list --json"]["stdout"] = json.dumps(sessions_response)
        failures: list[CommandResponse] = [{"code": 1, "stdout": "", "stderr": "fixture broken"}]
        malformed_agents: list[dict[str, object]] = [
            {"agent": "codex", "agent_status": "idle"},
            {"pane_id": "w1:p1", "agent": "codex"},
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle", "agent_session": []},
        ]
        for agent in malformed_agents:
            failures.append({"code": 0, "stdout": json.dumps({"result": {"agents": [agent]}})})
        responses["--session native agent list"]["stdout"] = json.dumps({"ok": True, "result": {"agents": [
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle"}
        ]}})
        for failure in failures:
            with self.subTest(failure=failure):
                responses["--session broken agent list"] = failure
                binary = self._herdr_fixture(responses)
                with mock.patch.object(herdr, "herdr_binary", return_value=binary), \
                        mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
                    found = roster.roster(os.environ, roster.ThisMachine())
                self.assertEqual([entry.pane for entry in found.entries if isinstance(entry.pane, herdr.HerdrPlace)
                                  and entry.pane.session == "native"], [herdr.HerdrPlace("native", "w1:p1")])
                self.assertEqual(len(found.problems), 1)
                self.assertIn("herdr session broken", found.problems[0])


    def test_unreadable_herdr_fallback_places_director_but_never_ledger_worker_by_cwd(self) -> None:
        owner = unit_membership("alpha", "unit-one")
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(roster.remote.local_machine(), self.home / "projects/unit-one"), expected=owner.worktree)
        _ = self.production_doc.write_text("# Production\n")
        worker_path = self.sessions / "worker.json"
        worker = cast(dict[str, object], json.loads(worker_path.read_text(encoding="utf-8")))
        worker["cwd"] = str(self.home / "projects" / "unit-one")
        _ = worker_path.write_text(json.dumps(worker), encoding="utf-8")
        with mock.patch.object(herdr, "herdr_binary", side_effect=herdr_types.HerdrMissing("herdr is not installed")), mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        director = next(entry for entry in found.entries if entry.name == "unit-one")
        worker_entry = next(entry for entry in found.entries if entry.name == "alpha-test")
        self.assertEqual(
            (director.role, director.showrunner),
            (
                "unit director",
                roster.UnderShowrunner("alpha", "alpha", "session:acd50bad-5afa-5d3d-b155-3766b7d113e0"),
            ),
        )
        self.assertEqual(
            (worker_entry.role, worker_entry.director),
            (
                "worker",
                roster.UnderDirector("unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"),
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
            }), mock.patch.object(codex_daemon, "timeout_seconds", return_value=0.3), \
                    mock.patch.object(codex_daemon, "read_daemons", new=timed_read_daemons):
                found = roster.roster(os.environ, roster.ThisMachine())
            self.assertEqual(len(read_times), 1)
            elapsed = read_times[0]
            self.assertLess(elapsed, 0.9, "account timeouts must share one discovery budget")
            entries = [entry for entry in found.entries if entry.thread == roster.CodexThread("healthy-thread")]
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

    def ssh_calls(self) -> list[list[str]]:
        if not self.ssh_log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.ssh_log.read_text().splitlines()]

    def peer_snapshot(self, result: roster.Roster, *, status: int = 0) -> None:
        os.environ["SSH_SNAPSHOT"] = roster.RosterSnapshot(result).encode()
        os.environ["SSH_STATUS"] = str(status)

    def test_pane_assignment_states_come_from_records_and_not_names(self) -> None:
        self._write_pane_workers(self.director_run, {
            "arbitrary": {"thread_id": "c11994bc-45da-585c-bb8c-b9aa62be4800", "status": "done", "role": "impl"},
            "another": {"thread_id": "78c792af-377b-51b5-8da7-bddbe198f72c", "status": "done", "role": "test"},
            "repair": {"thread_id": fixture_runtime_id("assignment-repair"), "status": "done", "role": "fix"},
            "arbitrary-reviewer": {"thread_id": fixture_runtime_id("assignment-reviewer"), "status": "done", "role": "review"},
            "legacy-impl": {"thread_id": "05a8f984-dd3e-5322-970d-625b046472de", "status": "done", "role": "impl"},
        })
        found = roster.roster(os.environ, roster.ThisMachine())
        entries = {entry.name: entry for entry in found.entries}
        for name, expected in (("arbitrary", "impl"), ("another", "test"), ("repair", "fix"),
                               ("arbitrary-reviewer", "review"),
                               ("legacy-impl", "impl")):
            with self.subTest(name=name):
                entry = entries[name]
                self.assertEqual(entry.role, "worker")
                self.assertIsInstance(entry.assignment, roster.WorkerAssignment)
                assert isinstance(entry.assignment, roster.WorkerAssignment)
                self.assertEqual(entry.assignment.role, expected)
                restored = roster.RosterSnapshot.decode(roster.RosterSnapshot(roster.Roster([entry], [])).encode()).result.entries[0]
                self.assertEqual(restored.assignment, entry.assignment)
                self.assertEqual(restored.thread, entry.thread)
        self.assertNotIsInstance(roster.shown_name(entries["legacy-impl"]).job, roster.JobUnassigned)

    def test_named_launch_record_wins_over_another_workers_matching_suffix(self) -> None:
        self._write_pane_workers(self.director_run, {
            "arbitrary-impl": {"thread_id": "named-thread", "status": "running", "role": "test"}})
        parent = unit_membership("alpha", "unit-one")
        member, = [row for row in staff.local_snapshot().find(kind="worker", parent=parent.id)
                   if row.name == "arbitrary-impl"]
        for slot, reference in (("impl", fixture_runtime_id("another-worker")), ("test", member.id)):
            write_text_atomic(self.director_run / f"impl_worker_{slot}.json",
                              json.dumps({"staff_id": reference, "slot": slot}))
        entry = self._entries()[member.name]
        self.assertIsInstance(entry.assignment, roster.WorkerAssignment)
        assert isinstance(entry.assignment, roster.WorkerAssignment)
        self.assertEqual(entry.assignment.slot, "test")
        self.assertEqual(roster.shown_name(entry).render(), "alpha · unit-one · worker test")
        title = roster.worker_shown_name(str(self.director_run), member.name, {"staff_id": member.id})
        self.assertEqual(title.render(), "alpha · unit-one · worker test")

    def test_pane_workers_keep_their_backend_accounts_across_a_selection_change(self) -> None:
        old, current = self.account_note(1), self.account_note(2)
        shared = current / "sessions"
        shared.mkdir(parents=True, exist_ok=True)
        (old / "sessions").symlink_to(shared, target_is_directory=True)
        self._publish_workers(self.director_run / unit_runs.STATE_FILE, {
            "old-impl": {"thread_id": "old-thread", "status": "running", "role": "impl"},
            "shared-worker": {"thread_id": "shared-thread", "status": "done"}}, codex_home=old)
        self._publish_workers(self.director_run / unit_runs.STATE_FILE, {
            "new-test": {"thread_id": "new-thread", "status": "running", "role": "test"},
            "between-turns": {"thread_id": "waiting-thread", "status": "done"}}, codex_home=current)
        with mock.patch.object(agent_accounts, "workers_home", return_value=old):
            before = self._entries()
        with mock.patch.object(agent_accounts, "workers_home", return_value=current):
            after = self._entries()
        for name, label in (("old-impl", "codex1"), ("new-test", "codex2"),
                            ("between-turns", "codex2"), ("shared-worker", "codex1")):
            with self.subTest(worker=name):
                self.assertEqual(before[name].account, roster.AccountLabel(label))
                self.assertEqual(after[name].account, roster.AccountLabel(label))
                self.assertEqual(after[name].thread, before[name].thread)
                self.assertEqual(after[name].assignment, before[name].assignment)
                self.assertEqual(after[name].reach, before[name].reach)

    def test_closed_pane_worker_keeps_its_backend_account(self) -> None:
        home = self.account_note(2)
        self._publish_workers(self.director_run / unit_runs.STATE_FILE, {
            "idle-worker": {"thread_id": "idle-thread", "status": "done"}}, codex_home=home)
        before = self._entries()["idle-worker"]
        self._publish_workers(self.director_run / unit_runs.STATE_FILE, {
            "idle-worker": {"thread_id": "idle-thread", "status": "ended"}}, codex_home=home)
        after = self._entries()["idle-worker"]
        self.assertEqual(before.account, roster.AccountLabel("codex2"))
        self.assertEqual(after.account, before.account)
        self.assertEqual(after.thread, before.thread)
        self.assertEqual(after.reach, roster.CannotReceiveMessages("closed"))

    def test_worker_slots_keep_full_launch_identity_in_roster_and_titles(self) -> None:
        placements = (("alpha-render-a", "render-a", "impl"), ("alpha-physics-a", "physics-a", "impl"),
                      ("arbitrary-address", "test-b", "test"))
        self._write_pane_workers(self.director_run, {
            name: {"thread_id": fixture_runtime_id(name), "status": "done", "role": role}
            for name, role in ((item[0], item[2]) for item in placements)})
        parent = unit_membership("alpha", "unit-one")
        members = {row.name: row for row in staff.local_snapshot().find(kind="worker", parent=parent.id)}
        for name, slot, role in placements:
            write_text_atomic(self.director_run / f"impl_worker_{slot}.json",
                              json.dumps({"staff_id": members[name].id, "slot": slot, "role": role}))
        entries = self._entries()
        for name, slot, role in placements:
            with self.subTest(name=name, role=role):
                entry = entries[name]
                assert isinstance(entry.assignment, roster.WorkerAssignment)
                self.assertEqual(entry.assignment.slot, slot)
                self.assertEqual(roster.shown_name(entry).render(), f"alpha · unit-one · worker {slot}")
                title = roster.worker_shown_name(str(self.director_run), name,
                                               {"staff_id": members[name].id, "slot": slot})
                self.assertEqual(title.render(), f"alpha · unit-one · worker {slot}")
                self.assertEqual(title.address, name)

    def test_pane_reader_uses_the_current_thread_from_the_staff_backend_record(self) -> None:
        parent = unit_membership("alpha", "unit-one")
        worker = fixture_worker("impl2", parent.id, backend=staff.CodexBackend(self.root / "codex-home"))
        current = staff.native_id(fixture_runtime_id("roster-replaced-thread"))
        fixture_session(current, staff.CodexBackend(self.root / "codex-home"))
        worker = staff.update_current_session(worker.current_session, current)
        self.pane_world.add(worker, "w1:p99", family="codex")
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        found = self._entries()[worker.name]
        self.assertEqual(found.thread, roster.CodexThread(current))
        self.assertIsInstance(found.director, roster.UnderDirector)
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertFalse(any(path.name.endswith("_roster.json") for path in self.director_run.iterdir()))

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
        for name in ("alpha", "session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0"):
            self.assertEqual(before[name].account, roster.AccountLabel("claude2"))
            self.assertIsInstance(after[name].account, roster.AccountUnread)
        self.assertEqual(home_reads.call_count, 2)

    def test_claude_roster_account_comes_from_its_process_configuration(self) -> None:
        directory = self.account_note(3, tool="claude")
        with mock.patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(directory)):
            entry = self._entries()["alpha"]
        self.assertEqual(entry.account, roster.AccountLabel("claude3"))



    def test_daemon_rows_use_the_home_holding_the_socket(self) -> None:
        home = self.account_note(1)
        _ = self.account_note(2)
        path = home / "app-server-control" / "daemon.sock"
        with mock.patch.dict(os.environ, {"ROSTER_CODEX_SOCKET": str(path)}):
            entry = self._entries()["codex:eaa2b2bc-f66c-5ac8-98a1-ed9d5a1eb539"]
        self.assertEqual(entry.account, roster.AccountLabel("codex1"))
        self.assertEqual(entry.daemon, codex_daemon.CodexDaemon(home, path))

    def test_reviewers_require_a_readable_environment_before_using_the_default_home(self) -> None:
        _ = self.account_note(1)
        _ = self.account_note(2)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()):
            entries = self._entries()
        self.assertEqual(entries["review:adversary"].account, roster.AccountLabel("codex2"))
        with mock.patch.object(process_table, "snapshot", return_value={}):
            missing = self._entries()
        self.assertNotIn("review:review", missing)
        with mock.patch.object(account, "process_codex_home", return_value=account.ProcessEnvironmentUnread("access denied")):
            entries = self._entries()
        self.assertIsInstance(entries["review:adversary"].account, roster.AccountUnread)
        assert isinstance(entries["review:adversary"].account, roster.AccountUnread)
        self.assertIn("access denied", entries["review:adversary"].account.reason)

    def test_snapshot_without_account_decodes_as_peer_attribution_unavailable(self) -> None:
        row = replace(self._entry("Mac director", "unit director", "session:mac-id"),
                      machine="mac", account=roster.AccountLabel("claude3"), registry_name=roster.RegistryName("mac"))
        encoded = roster.RosterSnapshot(roster.Roster([row], [])).encode()
        self.assertEqual(roster.RosterSnapshot.decode(encoded).result.entries[0].account,
                         roster.AccountLabel("claude3"))
        self.assertEqual(roster.RosterSnapshot.decode(encoded).result.entries[0].registry_name,
                         roster.RegistryName("mac"))
        old = cast(dict[str, object], json.loads(encoded))
        entries = cast(list[dict[str, object]], old["entries"])
        del entries[0]["account"]
        del entries[0]["registry_name"]
        decoded = roster.RosterSnapshot.decode(json.dumps(old)).result.entries[0]
        self.assertEqual(decoded.name, "Mac director")
        self.assertIsInstance(decoded.account, roster.AccountAttributionUnavailable)
        self.assertEqual(decoded.registry_name, roster.NoRegistryName())

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
        self.assertEqual(commands[0][:2], ["-o", "BatchMode=yes"])
        self.assertIn("--here --snapshot", commands[0][-1])
        self.assertIn('"$HOME/.claude/scripts/lib/py"', commands[0][-1])

    def test_unreachable_peer_is_one_problem_while_local_rows_still_print(self) -> None:
        os.environ["SSH_SNAPSHOT"] = "ssh: connection refused"
        os.environ["SSH_STATUS"] = "255"
        result, lines, errors = self._run_main(here=False)
        self.assertEqual(result, 1)
        self.assertTrue(any("session:abd1a227-f410-5c76-bc96-a0c77c0f3ef0" in line for line in lines))
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
            replace(base, kind="codex", session=roster.NoClaudeSession(), thread=roster.CodexThread("pane-thread"),
                    reach=roster.Messageable("codex:pane-thread"), pane=herdr.HerdrPlace("private", "w1:p2")),
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
        director = next(row for row in snapshot.result.entries if row.name == "unit-one")
        worker = next(row for row in snapshot.result.entries if row.name == "alpha-impl")
        self.assertEqual(director.session, roster.ClaudeSession("17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))
        self.assertEqual(director.reach, roster.CannotReceiveMessages("no messaging socket"))
        self.assertEqual(worker.director, roster.UnderDirector("unit-one", "unit-one", "session:17e44ec4-3e24-5ae1-b10d-5cf30de9455b"))
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
        record = staff.snapshot()
        with (mock.patch.object(staff, "snapshot", return_value=record),
              mock.patch.object(sys, "platform", "darwin"),
              mock.patch.object(herdr, "herdr_binary", side_effect=herdr_types.HerdrMissing("herdr is not installed")),
              mock.patch.object(codex_daemon, "loaded_sessions", return_value=[])):
            result = roster.roster(os.environ, roster.ThisMachine())
        director = next(row for row in result.entries if row.name == "unit-one")
        self.assertEqual(director.role, "unit director")
        self.assertTrue(any("unit hosts for alpha were not read" in problem for problem in result.problems))
        self.assertEqual(self.ssh_calls(), [])


class PaneRosterTests(StaffTestCase):
    def test_missing_herdr_keeps_session_entries_and_reports_pane_workers(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.unit.current_session, "director")
        config = self.root / "config"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# Worker settings.\n")
        write_machine_config(config)
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}), \
             mock.patch.object(herdr, "herdr_binary", side_effect=herdr_types.HerdrMissing("herdr is not installed")), \
             mock.patch.object(account, "process_config_dir", return_value=None), \
             mock.patch.object(codex_daemon, "read_daemons", return_value=[]):
            found = roster.roster(os.environ, roster.ThisMachine())
        self.assertEqual([problem for problem in found.problems if problem.startswith("pane workers:")],
            ["pane workers: herdr is not installed"])
        self.assertIn(fixture.unit.name, [entry.name for entry in found.entries])

    def test_pane_workers_keep_five_states_and_replace_native_session_entries(self) -> None:
        fixture = self.row_production()
        rows = [fixture_worker(name, fixture.unit.id,
                               backend=staff.CodexBackend(self.root / "codex") if name == "open" else staff.ClaudeBackend())
                for name in ("open", "stopped", "closed", "unknown")]
        remote_parent = fixture_member("unit", name="remote-parent", parent=fixture.runner.id,
                                       worktree=staff.PresentWorktree("mac", self.root / "remote-tree"))
        remote_worker = fixture_worker("remote-worker", remote_parent.id)
        world = PaneWorld()
        world.add(fixture.unit, "w1:p0")
        world.add(rows[0], "w1:p1", family="codex")
        world.add(rows[1], "w1:p2", live=False)
        world.add(rows[3], "w1:p4")
        world.locations["w1:p1"]["agent_status"] = "working"
        _ = self.session_record(rows[0].current_session, "registry-copy")
        config = self.root / "config"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# Worker settings.\n")
        write_machine_config(config)
        before = self.staff_file.read_bytes()
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config), "ROSTER_RUN_ROOT": str(self.root / "runs")}),
              mock.patch.object(worker_panes, "_client", return_value=world),
              mock.patch.object(herdr, "native_process", side_effect=world.native_process),
              mock.patch.object(herdr, "_startup_processes", side_effect=world.descendants),
              mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]),
              mock.patch.object(herdr, "Herdr", return_value=world),
              mock.patch.object(herdr, "herdr_processes", return_value=[
                  herdr.HerdrProcess(world.foreground["w1:p1"][0], herdr.HerdrPlace("agents", "w1:p1"),
                                     {"CODEX_THREAD_ID": rows[0].current_session}, "codex", str(fixture.worktree))]),
              mock.patch.object(herdr, "process_environment", side_effect=ProcessLookupError("process exited")),
              mock.patch.object(codex_daemon, "read_daemons", return_value=[])):
            with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                result = roster.roster(os.environ, roster.ThisMachine())
            self.assertEqual(reads.call_count, 1)
            entries = {entry.name: entry for entry in result.entries}
            self.assertEqual(entries["open"].reach, roster.Messageable(f"worker:{fixture.unit.name}/open"))
            self.assertEqual(entries["open"].status, "working; pane w1:p1")
            self.assertIn("stopped", entries["stopped"].status)
            self.assertEqual(entries["closed"].reach, roster.CannotReceiveMessages("closed"))
            self.assertNotIn("registry-copy", entries)
            self.assertNotIn("herdr agents:w1:p1", entries)
            self.assertNotIn(remote_worker.name, entries)
            world.unknown["w1:p4"] = "process read denied"
            result = roster.roster(os.environ, roster.ThisMachine())
            entries = {entry.name: entry for entry in result.entries}
            self.assertEqual(entries["unknown"].status, "unknown; pane w1:p4: process read denied")
            self.assertIsInstance(entries["unknown"].reach, roster.CannotReceiveMessages)
            self.assertIn("incomplete:", entries["closed"].status)
            self.assertIn("absence", entries["closed"].status)
            self.assertEqual(self.staff_file.read_bytes(), before)

    def test_roster_names_owner_and_record_failures(self) -> None:
        _ = self.row_production()
        _ = self.write_document({"schema": 2, "rows": ["invalid member"]})
        result = roster.roster(os.environ, roster.ThisMachine(), registry=sessions.RegistryRead((), ()))
        self.assertTrue(any("staff membership" in problem for problem in result.problems), result.problems)
        self.assertTrue(any("staff" in problem for problem in result.problems), result.problems)
        with self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}):
            result = roster.roster(os.environ, roster.ThisMachine(), registry=sessions.RegistryRead((), ()))
        self.assertTrue(any("not reached" in problem for problem in result.problems), result.problems)


class CodexHolderLabelsTests(unittest.TestCase):
    @cached_property
    def pane_world(self) -> PaneWorld:
        return PaneWorld()

    root: Path = Path()
    runs: Path = Path()
    registry: Path = Path()
    environment: dict[str, str] = {}
    director_id: str = ""
    runner_id: str = ""
    thread: str = "01a1775e-0fcd-7842-a664-436a33f9007b"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runs = self.root / "runs"
        self.registry = self.root / "sessions"
        notifier = self.root / "notifier"
        for directory in (self.registry, notifier, self.root / "home"):
            directory.mkdir(parents=True)
        self.environment = {
            **os.environ, "HOME": str(self.root / "home"),
            "NOTIFIER_SESSIONS_DIR": str(self.registry), "NOTIFIER_STATE_DIR": str(notifier),
            "ROSTER_RUN_ROOT": str(self.runs), "HERDR_PROC": str(self.root / "proc"),
            "HERDR_BIN": str(SCRIPTS / "herdr/fake_herdr.py"),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
        }
        environment_patch = cast(AbstractContextManager[object], mock.patch.dict(os.environ, self.environment, clear=True))
        _ = self.enterContext(environment_patch)
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        unit = unit_membership("builds", "report")
        unit = staff.set_worktree(unit.id, staff.PresentWorktree(roster.remote.local_machine(), self.root),
                                  expected=unit.worktree)
        self.director_id = unit.id
        assert isinstance(unit.parent, staff.ReportsTo)
        self.runner_id = unit.parent.id
        # Both registry records are live processes, with real start identities.
        # Only their declared session labels and run metadata come from fixture files.
        for pid, sid, name in ((os.getppid(), self.director_id, "report-director"),
                               (os.getpid(), self.runner_id, "builds-runner")):
            start = sessions.process_start(pid)
            self.assertIsNotNone(start)
            _ = (self.registry / f"{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": sid, "name": name, "procStart": start,
                "messagingSocketPath": "", "updatedAt": 1, "cwd": str(self.root / name),
            }))
        self.process(991099, 0, ["herdr", "--session", "native", "server"])
        self.process(os.getppid(), 991099, ["claude"],
                     ('HERDR_PANE_ID=w1:p1\x00' + 'SHOWRUNNER_' + 'UNIT=builds\x00' + 'SHOWRUNNER_' + 'UNIT_ID=report\x00'))
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
        write_text_atomic(doc, f"# builds\n- **Merge branch:** main\n- **Log:** log.md\n- **Showrunner checkout:** {self.root}\n")
        _ = recorded_showrunner("builds", doc)
        timer = notifier / "showrunner-builds"
        timer.mkdir()
        _ = (timer / "conf").write_text(f"TARGET=session:{self.runner_id}\nCHECK=zsh /scripts/production_check.sh builds\n")
        run = self.runs / "run-00"
        self.worker_run(run, {"report-test2": {"thread_id": self.thread, "status": "running"}})
        native_process = herdr.native_process

        def fixture_process(pid: int, family: str,
                            reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            if pid in self.pane_world.processes:
                return self.pane_world.native_process(pid, family, reported if isinstance(reported, str) else "")
            return native_process(pid, family, reported)

        pane_patches = [
            cast(object, self.enterContext(mock.patch.object(worker_panes, "_client", return_value=self.pane_world))),
            cast(object, self.enterContext(mock.patch.object(herdr, "native_process", side_effect=fixture_process))),
            cast(object, self.enterContext(mock.patch.object(herdr, "_startup_processes", side_effect=self.pane_world.descendants))),
        ]
        self.addCleanup(pane_patches.clear)
        self.addCleanup(self.pane_world.process_directory.cleanup)
        row = staff.local_snapshot().by_current_session(self.director_id)
        assert row is not None
        _ = staff.set_run(row.id, staff.OpenRun(run))

    def process(self, pid: int, parent: int, argv: list[str], environment: str = "") -> None:
        directory = self.root / "proc" / str(pid)
        directory.mkdir(parents=True)
        _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
        _ = (directory / "cmdline").write_bytes("\0".join(argv).encode())
        _ = (directory / "environ").write_bytes(environment.encode())
        (directory / "cwd").symlink_to(self.root)

    def worker_run(self, run: Path, workers: dict[str, dict[str, object]]) -> None:
        run.mkdir(parents=True, exist_ok=True)
        for name, fields in workers.items():
            child = fixture_worker(name, staff.StaffId(self.director_id), runtime_id=str(fields["thread_id"]),
                                   backend=staff.CodexBackend(self.root / "codex-home"))
            fields["staff_id"] = child.id
            self.pane_world.add(child, f"w1:p{len(self.pane_world.locations) + 10}", family="codex")
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: self.director_id}))

    def test_codex_thread_labels_equal_full_roster_without_mocking_readers(self) -> None:
        full = roster.unit_labels(roster.roster(self.environment, roster.ThisMachine()).entries)
        fast = roster.unit_labels_for([self.thread, self.director_id, "unknown-thread", "report-test2"], self.environment)
        self.assertEqual(fast, {sid: full[sid] for sid in (self.thread, self.director_id)})
        self.assertEqual(fast[self.thread], roster.UnitLabels("report-test2", "builds", "report", unit_labels.OwnerRole.UNIT))
        self.assertEqual(fast[self.director_id], roster.UnitLabels("report", "builds", "report", unit_labels.OwnerRole.UNIT))

    def test_twenty_run_lookup_is_well_inside_the_cli_budget(self) -> None:
        wanted = [self.thread]
        expected = {self.thread: roster.UnitLabels("report-test2", "builds", "report", unit_labels.OwnerRole.UNIT)}
        for number in range(1, 20):
            run = self.runs / f"run-{number:02}"
            thread = f"01a1775e-0fcd-7842-a664-{number:012}"
            name = f"report-test-{number}"
            self.worker_run(run, {name: {"thread_id": thread, "status": "done"}})
            (run / "heartbeat.log").touch()
            _ = (run / "progress_history_state.json").write_text(json.dumps({"production": "builds", "unit": "report"}))
            wanted.append(thread)
            expected[thread] = roster.UnitLabels(name, "builds", "report", unit_labels.OwnerRole.UNIT)
        started = time.perf_counter()
        labels = roster.unit_labels_for([*wanted, "unknown-thread"], self.environment)
        elapsed = time.perf_counter() - started
        self.assertEqual(labels, expected)
        self.assertLess(elapsed, 0.2, f"20-run label lookup took {elapsed:.3f} seconds")



class UnitLabelsTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        production_membership("cargo-liner-style", "cargo-liner-style-types")

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
            self.entry("director-id", "report-director", showrunner=runner, director=roster.DirectsUnit("report")),
            self.entry("worker-thread", "report-test2", showrunner=runner,
                       director=roster.UnderDirector("report", "report-director", "session:director-id"),
                       codex=True),
            self.entry("runner-id", "builds-showrunner", showrunner=roster.RunsProduction("builds")),
            self.entry("unmatched-id", "scratch"),
            self.entry("offline-id", "orphan", showrunner=roster.ProductionWithoutRunningShowrunner("offline")),
        ]
        labels = roster.unit_labels(entries)
        self.assertEqual(labels, {
            "director-id": roster.UnitLabels("report-director", "builds", "report", unit_labels.OwnerRole.UNIT),
            "worker-thread": roster.UnitLabels("report-test2", "builds", "report", unit_labels.OwnerRole.UNIT),
            "runner-id": roster.UnitLabels("builds-showrunner", "builds", "—", unit_labels.OwnerRole.SHOWRUNNER),
            "unmatched-id": roster.UnitLabels("scratch", "—", "—"),
            "offline-id": roster.UnitLabels("orphan", "offline", "—", unit_labels.OwnerRole.SHOWRUNNER),
        })
        self.assertEqual(labels["director-id"].folder_name(), "builds-report")
        self.assertEqual(labels["worker-thread"].folder_name(), "builds-report")

    def test_worker_roles_agree_for_live_recorded_and_roster_readers(self) -> None:
        runner = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="report", parent=runner.id)
        records: list[sessions.SessionRecord] = []
        entries: list[roster.RosterEntry] = []
        expected: dict[str, roster.UnitLabels] = {}
        codex_home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for parent, role, director in (
            (runner, unit_labels.OwnerRole.SHOWRUNNER, roster.NoDirector()),
            (unit, unit_labels.OwnerRole.UNIT, roster.UnderDirector("report", "report", "")),
        ):
            for backend in (staff.ClaudeBackend(), staff.CodexBackend(codex_home)):
                name = parent.name + "-" + backend.family
                worker = fixture_worker(name, parent.id, backend=backend)
                codex = isinstance(backend, staff.CodexBackend)
                entries.append(self.entry(worker.current_session, name,
                    showrunner=roster.UnderShowrunner("builds", "builds", ""), director=director, codex=codex))
                expected[worker.current_session] = roster.UnitLabels(
                    name, "builds", "report" if parent == unit else "—", role)
                if not codex:
                    records.append({"pid": len(records) + 100, "sessionId": worker.current_session,
                        "name": name, "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": []})
        with (mock.patch.object(unit_labels, "newest_sessions", return_value=records),
              mock.patch.object(unit_labels, "registered_showrunners", return_value=[])):
            fast = roster.unit_labels_for(expected, {})
        self.assertEqual(fast, expected)
        self.assertEqual(roster.unit_labels(entries), expected)
        self.assertEqual(unit_labels.recorded_owner_labels(set(expected), staff.snapshot()), expected)

    def test_labels_and_shown_name_agree_on_production_and_unit(self) -> None:
        entry = self.entry("director-id", "report-director",
                           showrunner=roster.UnderShowrunner("builds", "builds-showrunner", ""),
                           director=roster.DirectsUnit("report"))
        shown = roster.shown_name(entry)
        labels = roster.unit_labels([entry])["director-id"]
        self.assertEqual(shown.production, roster.ProductionPart(labels.showrunner))
        self.assertIsInstance(shown.unit, roster.UnitPart)
        self.assertEqual(shown.render().split(" · ")[:2], [labels.showrunner, labels.unit])

    def test_style_unit_keeps_its_registered_showrunner_prefix(self) -> None:
        runner = roster.UnderShowrunner("cargo-liner-style", "cargo-liner", "session:liner")
        entries = [
            self.entry("style-director", "types-director", showrunner=runner,
                       director=roster.DirectsUnit("cargo-liner-style-types")),
            self.entry("style-worker", "types-review", showrunner=runner,
                       director=roster.UnderDirector("cargo-liner-style-types", "types-director",
                                                     "session:style-director"),
                       codex=True),
        ]
        labels = roster.unit_labels(entries)
        self.assertEqual(labels, {
            "style-director": roster.UnitLabels("types-director", "cargo-liner-style", "cargo-liner-style-types", unit_labels.OwnerRole.UNIT),
            "style-worker": roster.UnitLabels("types-review", "cargo-liner-style", "cargo-liner-style-types", unit_labels.OwnerRole.UNIT),
        })
        self.assertEqual(labels["style-worker"].folder_name(), "cargo-liner-style-types")

    def test_labels_collapse_whitespace_without_changing_the_registered_name(self) -> None:
        entry = self.entry("id", "report\n test2", showrunner=roster.UnderShowrunner(" builds\t", "runner", ""),
                           director=roster.UnderDirector(" report\n", "director", ""), codex=True)
        self.assertEqual(roster.unit_labels([entry]), {"id": roster.UnitLabels("report test2", "builds", "report", unit_labels.OwnerRole.UNIT)})

    def test_fast_lookup_uses_only_fixture_sessions_registry_and_staff_marks(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        unit = unit_membership("cargo-liner-style", "cargo-liner-style-types")
        assert isinstance(unit.parent, staff.ReportsTo)
        worker = fixture_worker("types-review", unit.id)
        doc = root / "production.md"
        _ = staff.set_doc(unit.parent.id, staff.LiveDoc(roster.remote.local_machine(), doc))
        _ = doc.write_text("# cargo-liner\n")
        records: list[sessions.SessionRecord] = [
            {"pid": pid, "sessionId": sid, "name": name, "messagingSocketPath": f"/fixture/{sid}.sock",
             "updatedAt": 1, "tmux": "", "formerNames": []}
            for pid, sid, name in (
                (101, unit.parent.id, "cargo-liner"), (102, unit.id, "cargo-liner-style-types"),
                (103, "free-id", "scratch"), (104, worker.id, "types-review"),
            )
        ]
        for record in records:
            process = root / "proc" / str(record["pid"])
            process.mkdir(parents=True)
            environment_by_identity: dict[str, str] = {
                unit.id: ('' + 'SHOWRUNNER_' + 'UNIT=cargo-liner-style\x00' + 'SHOWRUNNER_' + 'UNIT_ID=cargo-liner-style-types\x00'),
                worker.id: f"CLAUDE_CODE_SESSION_ID={unit.id}\0",
            }
            marks = environment_by_identity.get(record["sessionId"], "")
            _ = (process / "environ").write_text(marks)
        registry = root / "sessions"
        registry.mkdir()
        for record in records:
            _ = (registry / f"{record['pid']}.json").write_text(json.dumps(record))
        registered: list[showrunners.Showrunner] = [{
            "session": "cargo-liner", "socket": f"/fixture/{unit.parent.id}.sock", "name": "cargo-liner-style",
            "zone": "America/New_York", "doc": str(doc),
        }]
        environment = {"HERDR_PROC": str(root / "proc"), "NOTIFIER_SESSIONS_DIR": str(root / "sessions")}
        notifier = root / "notifier"
        (notifier / "showrunner-cargo-liner-style").mkdir(parents=True)
        _ = (notifier / "showrunner-cargo-liner-style/conf").write_text(
            f"TARGET=session:{unit.parent.id}\nCHECK=zsh /scripts/production_check.sh {unit.parent.id}\n")
        with mock.patch.object(unit_labels, "newest_sessions", return_value=records), \
             mock.patch.object(session_records, "sessions_dir", return_value=registry), \
             mock.patch.object(session_records, "running_session", return_value=True), \
             mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier), \
             mock.patch.object(unit_labels, "registered_showrunners", return_value=registered), \
             mock.patch.object(unit_labels, "live_runs", side_effect=AssertionError("no run scans")) as runs, \
             mock.patch.object(unit_lookup, "every_labeled_session", side_effect=AssertionError("no mark scan")) as marks_read, \
             mock.patch.object(herdr, "herdr_places", side_effect=AssertionError("no process scan")) as places, \
             mock.patch.object(process_table, "snapshot", side_effect=AssertionError("no process table")) as processes, \
             mock.patch.object(account, "home_account_state", side_effect=AssertionError("no accounts")) as accounts, \
             mock.patch.object(codex_daemon, "loaded_sessions", side_effect=AssertionError("no daemon")) as daemon:
            labels = roster.unit_labels_for([unit.id, unit.parent.id, "free-id", worker.id, "missing-id"],
                                            environment)
        self.assertEqual(labels, {
            unit.id: roster.UnitLabels("cargo-liner-style-types", "cargo-liner-style", "cargo-liner-style-types", unit_labels.OwnerRole.UNIT),
            worker.id: roster.UnitLabels("types-review", "cargo-liner-style", "cargo-liner-style-types", unit_labels.OwnerRole.UNIT),
            unit.parent.id: roster.UnitLabels("cargo-liner-style", "cargo-liner-style", "—", unit_labels.OwnerRole.SHOWRUNNER),
            "free-id": roster.UnitLabels("", "—", "—"),
        })
        runs.assert_not_called()
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
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        _ = showrunner_relationship("unrelated")
        with mock.patch.object(unit_labels, "newest_sessions", return_value=records), \
             mock.patch.object(unit_labels, "registered_showrunners", return_value=[]):
            labels = roster.unit_labels_for(["requested", "missing"], {"HERDR_PROC": str(root)})
        self.assertEqual(labels, {"requested": roster.UnitLabels("", "—", "—")})


if __name__ == "__main__":
    _ = unittest.main()
