"""Consultation status requires confirmed absence or a successful stop."""
from __future__ import annotations

import argparse
import io
import json
import os
import select
import subprocess
import sys
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh
from scripts.production import staff, worker_fill
from scripts.production.staff_test_support import StaffTestCase, fixture_worker
from scripts.shutdown import stop_resume


class EndFriendTests(StaffTestCase):
    def test_ending_a_codex_consultation_retires_the_friends_staff_row(self) -> None:
        _, unit, _ = self.tree()
        worker = fixture_worker("friend", unit.id, backend=staff.CodexBackend(self.root / "codex"))
        run = self.root / "consultation"
        run.mkdir()
        _ = (run / "consult_agent").write_text("launched\n")
        _ = (run / "friend_name").write_text(worker.name)
        _ = (run / "status").write_text("running\n")
        _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            worker.name: {"staff_id": worker.id, "status": "done"},
        }))
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = worker_fill.main(["end-friend", "--run", str(run)])
        self.assertEqual(code, 0, output.getvalue() + errors.getvalue())
        self.assertIsNone(staff.local_snapshot().by_id(worker.id))
        self.assertEqual((run / "status").read_text(), "ended\n")

    def test_a_staff_retirement_refusal_still_stops_the_consultation_server(self) -> None:
        _, unit, _ = self.tree()
        worker = fixture_worker("friend-refused", unit.id, backend=staff.CodexBackend(self.root / "codex"))
        notes = self.root / "account-notes"
        Path.home().mkdir(parents=True, exist_ok=True)
        test_codex_mesh.select_fixture_workers(Path.home(), notes=notes)
        _ = self.enterContext(cast(AbstractContextManager[object],
                                   patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)})))
        run = self.root / "refused-consultation"
        run.mkdir()
        _ = (run / "consult_agent").write_text("launched\n")
        _ = (run / "friend_name").write_text(worker.name)
        _ = (run / "status").write_text("running\n")
        _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            worker.name: {"staff_id": worker.id, "status": "done"},
        }))
        driver = self.root / "consultation_server.py"
        _ = driver.write_text("\n".join((
            "import signal",
            "from scripts.agents import test_codex_mesh",
            "def respond(request):",
            "    method = request.get('method')",
            "    result = {}",
            "    if method == 'thread/queue/list': result = {'data': [], 'nextCursor': None}",
            f"    if method == 'thread/read': result = {{'thread': {{'id': {worker.current_session!r}, 'status': {{'type': 'idle'}}, 'turns': []}}}}",
            "    return [{'id': request['id'], 'result': result}] if 'id' in request else None",
            "server = test_codex_mesh.StubAppServer(respond)",
            "print(server.port, flush=True)",
            "signal.pause()",
        )) + "\n")
        server = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])})

        def stop_server() -> None:
            if server.poll() is None:
                server.kill()
            _ = server.communicate(timeout=10)

        self.addCleanup(stop_server)
        assert server.stdout is not None
        self.assertTrue(select.select([server.stdout], [], [], 10)[0], "the fixture server did not start")
        port = int(cast(str, server.stdout.readline()))
        _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": server.pid, "port": port}))
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}),
              patch.object(staff, "retire", side_effect=staff.StaffRefused("fixture retirement refused")),
              patch.object(codex_mesh, "_list_server_processes", return_value=
                           f"{server.pid} 0 codex app-server --listen ws://127.0.0.1:{port}"),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = worker_fill.main(["end-friend", "--run", str(run)])
        self.assertEqual(code, 1, output.getvalue() + errors.getvalue())
        self.assertEqual(errors.getvalue(), f"codex_mesh: {worker.name}: fixture retirement refused\n")
        self.assertIsNotNone(server.poll(), "the consultation server survived the retirement refusal")
        self.assertIsNotNone(staff.local_snapshot().by_id(worker.id))
        self.assertEqual((run / "status").read_text(), "running\n")

    def test_status_changes_only_after_confirmed_absence_or_successful_stop(self) -> None:
        _, unit, _ = self.tree()
        for case in ("absent", "unreadable", "claude-stop-failed", "codex-stop-failed", "claude-stopped", "codex-stopped"):
            with self.subTest(case=case):
                backend = staff.CodexBackend(self.root / "codex") if case.startswith("codex") else staff.ClaudeBackend()
                worker = fixture_worker("friend-" + case, unit.id, backend=backend)
                run = self.root / case
                run.mkdir()
                _ = (run / "consult_agent").write_text("launched\n")
                _ = (run / "friend_name").write_text("missing" if case == "absent" else worker.name)
                status = run / "status"
                _ = status.write_text("running\n")
                stopped: list[str] = []
                before = self.staff_file.read_bytes()
                if case == "unreadable":
                    _ = self.staff_file.write_text("{broken")

                def stop(reference: str) -> stop_resume.Stopped | stop_resume.StopFailed:
                    self.assertEqual(reference, worker.id)
                    self.assertEqual(status.read_text(), "running\n")
                    stopped.append("stop")
                    return (stop_resume.StopFailed(reference, "fixture stop failed") if case.endswith("failed")
                            else stop_resume.Stopped(worker.current_session, 987654))

                def end(args: argparse.Namespace) -> int:
                    self.assertEqual((cast(str, args.session_dir), cast(str, args.to)), (str(run), worker.name))
                    self.assertEqual(status.read_text(), "running\n")
                    stopped.append("end")
                    return 0

                def stop_server(args: argparse.Namespace) -> int:
                    self.assertEqual(cast(str, args.session_dir), str(run))
                    self.assertEqual(status.read_text(), "running\n")
                    stopped.append("stop")
                    return int(case.endswith("failed"))

                try:
                    with (patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}),
                          patch.object(stop_resume, "stop", side_effect=stop),
                          patch.object(codex_mesh, "command_end", side_effect=end),
                          patch.object(codex_mesh, "command_stop", side_effect=stop_server),
                          redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                        code = worker_fill.main(["end-friend", "--run", str(run)])
                    ended = case == "absent" or case.endswith("stopped")
                    self.assertEqual(code == 0, ended, output.getvalue() + errors.getvalue())
                    self.assertEqual(status.read_text(), "ended\n" if ended else "running\n")
                    self.assertEqual(stopped, [] if case in ("absent", "unreadable")
                                     else ["end", "stop"] if case.startswith("codex") else ["stop"])
                finally:
                    _ = self.staff_file.write_bytes(before)

    def test_codex_cleanup_retries_the_server_stop_after_the_worker_end_fails(self) -> None:
        _, unit, _ = self.tree()
        for retired in (False, True):
            with self.subTest(retired=retired):
                worker = fixture_worker("friend-retry-" + str(retired), unit.id,
                                        backend=staff.CodexBackend(self.root / "codex"))
                run = self.root / worker.name
                run.mkdir()
                _ = (run / "consult_agent").write_text("launched\n")
                _ = (run / "friend_name").write_text(worker.name)
                _ = (run / codex_mesh.SERVER_FILE).write_text('{"pid":987654,"port":4101}')
                status = run / "status"
                _ = status.write_text("running\n")
                stops: list[str] = []

                def end(args: argparse.Namespace) -> int:
                    self.assertEqual(cast(str, args.to), worker.name)
                    if retired:
                        _ = staff.retire(worker.id)
                        return 0
                    raise SystemExit("codex_mesh: fixture worker has no roster entry")

                def stop_server(args: argparse.Namespace) -> int:
                    self.assertEqual(cast(str, args.session_dir), str(run))
                    self.assertEqual(status.read_text(), "running\n")
                    stops.append("stop")
                    return int(len(stops) == 1)

                with (patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}),
                      patch.object(codex_mesh, "command_end", side_effect=end),
                      patch.object(codex_mesh, "command_stop", side_effect=stop_server),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    self.assertEqual(worker_fill.main(["end-friend", "--run", str(run)]), 1)
                    self.assertEqual(status.read_text(), "running\n")
                    self.assertEqual(stops, ["stop"])
                    self.assertEqual(worker_fill.main(["end-friend", "--run", str(run)]), 0)
                self.assertEqual(stops, ["stop", "stop"])
                self.assertEqual(status.read_text(), "ended\n")

    def test_member_refuses_a_missing_worker(self) -> None:
        _, unit, _ = self.tree()
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = worker_fill.main(["member", "--name", "missing"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("no worker staff row", errors.getvalue())
