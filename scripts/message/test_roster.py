#!/usr/bin/env python3
"""The machine roster joins Claude sessions, productions, runs, and Codex."""

from __future__ import annotations

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
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast, override
from unittest import mock

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))
sys.path.insert(0, str(SCRIPTS / "delegate"))

import broadcast
import codex_daemon
import remove_seats
import roster
import showrunners


ENTRY_FIELDS = {
    "machine", "kind", "role", "name", "status", "address", "cwd",
    "production", "showrunner", "showrunner_address", "unit", "director",
    "director_address", "slot_role", "session_dir",
}
WORKER_SESSION_ID = "12345678-1234-5678-9abc-def012345678"


class RosterTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    sessions: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    notifier: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    delegate: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    tmux_state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    ps_state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    production_doc: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    director_run: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.sessions = self.root / "sessions"
        self.notifier = self.root / "notifier"
        self.delegate = self.root / "delegate"
        binaries = self.root / "bin"
        for directory in (
            self.home, self.sessions, self.notifier, self.delegate / "active", binaries,
            self.root / "history" / "runs",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.tmux_state = self.root / "tmux.json"
        self.ps_state = self.root / "ps.txt"
        fake_ps = binaries / "ps"
        _ = fake_ps.write_text(
            "#!/bin/sh\ncat \"$ROSTER_PS_FIXTURE\"\n", encoding="utf-8",
        )
        fake_ps.chmod(0o755)
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
            "CLAUDE_BIN": str(fake_codex),
            "CLAUDE_CODE_SESSION_ID": "sid-runner",
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "SHOWRUNNERS_CONFIG": str(config),
            "SHOWRUNNERS_SESSIONS": str(Path(__file__).with_name("sessions.py")),
            "UNIT_LOOKUP_TMUX": str(Path(__file__).parents[1] / "production" / "fake_tmux.py"),
            "FAKE_TMUX_STATE": str(self.tmux_state),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history"),
            "ROSTER_DELEGATE_ROOT": str(self.delegate),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "ROSTER_PS_FIXTURE": str(self.ps_state),
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
            remove_seats, "shutdown_held_sessions",
            return_value={"kind": "held sessions", "session_ids": set()},
        ))
        _ = self.enterContext(mock.patch.object(
            broadcast, "PS", ["ps", "-eo", "pid=,ppid=,comm=,args="],
        ))
        self._build_complete_roster()

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
    ) -> None:
        socket_path = self.sessions / f"{file_name}.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        self.addCleanup(listener.close)
        _ = (self.sessions / f"{file_name}.json").write_text(json.dumps({
            "pid": os.getpid() if pid is None else pid,
            "sessionId": session_id,
            "name": name,
            "messagingSocketPath": str(socket_path),
            "updatedAt": updated,
            "tmux": tmux,
            "cwd": str(cwd),
            "status": status,
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
        _ = self.tmux_state.write_text(json.dumps({
            "$1": {
                "label": "unit-renamed",
                "panes": ["%1"],
                "env": {"SHOWRUNNER_UNIT": "alpha", "SHOWRUNNER_UNIT_ID": "unit-one"},
            },
        }), encoding="utf-8")

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

        self.director_run = self.delegate / "director-run"
        self.director_run.mkdir()
        _ = (self.delegate / "active" / "sid-director").write_text(
            str(self.director_run) + "\n", encoding="utf-8",
        )
        _ = (self.director_run / "seats").write_text(
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
            "alpha-ended": {"thread_id": "thread-ended", "status": "ended"},
        })

        solo_run = self.delegate / "solo-run"
        solo_run.mkdir()
        _ = (self.delegate / "active" / "sid-solo").write_text(
            str(solo_run) + "\n", encoding="utf-8",
        )

        orphan_run = self.delegate / "orphan-run"
        self._write_mesh(orphan_run, {
            "orphan-impl": {
                "thread_id": "thread-orphan", "turn_id": "turn-orphan", "status": "running",
                "launcher_pid": os.getpid(),
            },
        })
        (orphan_run / "heartbeat.log").touch()

        stale_run = self.delegate / "stale-marker-run"
        self._write_mesh(stale_run, {
            "stale-impl": {
                "thread_id": "thread-stale", "turn_id": "turn-stale", "status": "running",
                "launcher_pid": os.getpid(),
            },
        })
        _ = (self.delegate / "active" / "dead-director").write_text(
            str(stale_run) + "\n", encoding="utf-8",
        )

        dead_server_run = self.delegate / "dead-server-run"
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
            found = roster.roster(os.environ)
        self.assertEqual(found.problems, [])
        return {entry.name: entry for entry in found.entries}

    def _run_main(
        self, *arguments: str,
        daemon: list[codex_daemon.DaemonSession] | codex_daemon.DaemonUnreadable | None = None,
    ) -> tuple[int, list[str], list[str]]:
        output, errors = io.StringIO(), io.StringIO()
        daemon_result = self._daemon_sessions() if daemon is None else daemon
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=daemon_result), \
                redirect_stdout(output), redirect_stderr(errors):
            result = roster.main(list(arguments))
        return result, output.getvalue().splitlines(), errors.getvalue().splitlines()

    def _entry(
        self, name: str, role: str, address: str, *, kind: str = "claude",
        cwd: str = "", showrunner: str = "", showrunner_address: str = "",
        unit: str = "", director: str = "", director_address: str = "",
        session_dir: str = "",
    ) -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="machine", kind=kind, role=role, name=name, status="running",
            address=address, cwd=cwd, production="production", showrunner=showrunner,
            showrunner_address=showrunner_address, unit=unit, director=director,
            director_address=director_address,
            slot_role="impl" if role == "worker" else "", session_dir=session_dir,
        )

    def test_every_role_and_status_is_returned_with_its_relationships(self) -> None:
        entries = self._entries()
        machine = socket.gethostname().split(".", 1)[0]

        self.assertEqual(entries["runner"], roster.RosterEntry(
            machine=machine, kind="claude", role="showrunner", name="runner",
            status="ready", address="session:sid-runner",
            cwd=str(self.home / "projects" / "runner"), production="alpha",
            showrunner="", showrunner_address="", unit="", director="",
            director_address="", slot_role="", session_dir="",
        ))
        self.assertEqual(entries["director"], roster.RosterEntry(
            machine=machine, kind="claude", role="unit director", name="director",
            status="working", address="session:sid-director",
            cwd=str(self.home / "projects" / "unit-one"), production="alpha",
            showrunner="runner", showrunner_address="session:sid-runner", unit="unit-one",
            director="", director_address="", slot_role="", session_dir="",
        ))
        self.assertEqual(entries["claude-worker"].role, "worker")
        self.assertEqual(entries["claude-worker"].director, "director")
        self.assertEqual(entries["claude-worker"].director_address, "session:sid-director")
        self.assertEqual(entries["claude-worker"].slot_role, "test")
        self.assertEqual(entries["claude-worker"].session_dir, str(self.director_run))
        self.assertEqual(entries["solo-director"].role, "unit director")
        self.assertEqual(entries["solo-director"].production, "")
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
                self.assertEqual(entries[name].director, "director")
                self.assertEqual(entries[name].production, "alpha")
                self.assertEqual(entries[name].showrunner, "runner")
                self.assertEqual(entries[name].unit, "unit-one")
                self.assertEqual(entries[name].address, name)
        self.assertEqual(entries["alpha-research"].slot_role, "review (adversary)")
        self.assertEqual(entries["alpha-impl"].slot_role, "role unknown")
        self.assertEqual(entries["adversary reviewer"].slot_role, "review (adversary)")
        self.assertEqual(entries["adversary reviewer"].address, "")
        self.assertNotIn("contract reviewer", entries)
        self.assertEqual(entries["orphan-impl"].director, "")
        self.assertNotIn("alpha-ended", entries)
        self.assertNotIn("dead-impl", entries)
        self.assertNotIn("stale-impl", entries)

        self.assertEqual(entries["Codex window"].role, "freestanding")
        self.assertEqual(entries["Codex window"].address, "codex:thread-window")
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
        orphan_header = f"  director not running: {self.delegate / 'orphan-run'}"
        self.assertIn(orphan_header, lines)
        orphan_worker = next(line for line in lines if line.lstrip().startswith("worker orphan-impl"))
        self.assertTrue(orphan_worker.startswith("    "))
        self.assertTrue(any(line.strip() == "your Codex sessions" for line in lines))
        self.assertTrue(any("~/projects/unit-one" in line for line in lines))

    def test_background_id_prefix_places_claude_worker_in_its_run(self) -> None:
        worker = self._entries()["claude-worker"]
        self.assertTrue(WORKER_SESSION_ID.startswith("12345678"))
        self.assertEqual(
            (worker.role, worker.director, worker.director_address, worker.session_dir),
            ("worker", "director", "session:sid-director", str(self.director_run)),
        )

    def test_codex_review_lens_uses_role_and_lens_fields(self) -> None:
        entry = self._entries()["alpha-research"]
        self.assertEqual(entry.slot_role, "review (adversary)")

    def test_codex_worker_without_recorded_role_is_unknown(self) -> None:
        self.assertEqual(self._entries()["alpha-impl"].slot_role, "role unknown")

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

    def test_showrunner_socket_identity_beats_a_duplicate_display_name(self) -> None:
        self._write_session(
            "runner-imposter", "runner", "sid-imposter",
            self.home / "projects" / "imposter", updated=20,
        )
        with mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            entries = roster.roster(os.environ).entries
        real = next(entry for entry in entries if entry.address == "session:sid-runner")
        imposter = next(entry for entry in entries if entry.address == "session:sid-imposter")
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
            showrunner="same-runner", showrunner_address=runner_a.address, unit="unit-a",
        )
        director_b = self._entry(
            "same-director", "unit director", "session:director-b",
            showrunner="same-runner", showrunner_address=runner_b.address, unit="unit-b",
        )
        worker_a = self._entry(
            "worker-a", "worker", "worker-a", kind="codex",
            showrunner="same-runner", showrunner_address=runner_a.address,
            unit="unit-a", director="same-director", director_address=director_a.address,
        )
        worker_b = self._entry(
            "worker-b", "worker", "worker-b", kind="codex",
            showrunner="same-runner", showrunner_address=runner_b.address,
            unit="unit-b", director="same-director", director_address=director_b.address,
        )
        output, errors = io.StringIO(), io.StringIO()
        fixture = roster.Roster(
            [runner_a, runner_b, director_a, director_b, worker_a, worker_b], []
        )
        with mock.patch.object(roster, "roster", return_value=fixture), \
                redirect_stdout(output), redirect_stderr(errors):
            result = roster.main(["--kind", "codex"])
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
            (reviewer.kind, reviewer.role, reviewer.slot_role, reviewer.address, reviewer.cwd),
            ("codex", "worker", "review (adversary)", "", "/review/cwd"),
        )
        self.assertNotIn("contract reviewer", entries)

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
        self.assertTrue(all(all(isinstance(value, str) for value in row.values()) for row in rows))

        entry = roster.RosterEntry(*("" for _field in ENTRY_FIELDS))
        with self.assertRaises(FrozenInstanceError):
            entry.name = "changed"  # pyright: ignore[reportAttributeAccessIssue]
        result_value = roster.Roster([], [])
        with self.assertRaises(FrozenInstanceError):
            result_value.entries = []  # pyright: ignore[reportAttributeAccessIssue]

    def test_tmux_fallback_places_director_but_never_ledger_worker_by_cwd(self) -> None:
        missing_tmux = self.root / "bin" / "missing-tmux"
        worker_path = self.sessions / "worker.json"
        worker = cast(dict[str, object], json.loads(worker_path.read_text(encoding="utf-8")))
        worker["cwd"] = str(self.home / "projects" / "unit-one")
        _ = worker_path.write_text(json.dumps(worker), encoding="utf-8")
        with mock.patch.dict(os.environ, {"UNIT_LOOKUP_TMUX": str(missing_tmux)}), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ)
        director = next(entry for entry in found.entries if entry.name == "director")
        worker_entry = next(entry for entry in found.entries if entry.name == "claude-worker")
        self.assertEqual(
            (director.role, director.production, director.showrunner, director.unit),
            ("unit director", "alpha", "runner", "unit-one"),
        )
        self.assertEqual(
            (worker_entry.role, worker_entry.director, worker_entry.unit),
            ("worker", "director", "unit-one"),
        )
        self.assertTrue(any("tmux" in problem.lower() for problem in found.problems))

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
        with mock.patch.object(broadcast, "processes", side_effect=failure), \
                mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]):
            found = roster.roster(os.environ)
        self.assertTrue(found.entries)
        self.assertEqual(len([problem for problem in found.problems if "process" in problem]), 1)

    def test_recent_heartbeat_is_live_but_an_old_one_is_not(self) -> None:
        orphan = self.delegate / "orphan-run" / "heartbeat.log"
        old = time.time() - remove_seats.LIVE_HEARTBEAT_SECS - 5
        os.utime(orphan, (old, old))
        entries = self._entries()
        self.assertNotIn("orphan-impl", entries)


if __name__ == "__main__":
    _ = unittest.main()
