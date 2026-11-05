"""Worker operations preserve native identity and proved pane ownership."""
from __future__ import annotations

import io
import json
import fcntl
import math
import os
import subprocess
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from scripts.production import staff, worker_admission
from scripts.shutdown import restart
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_session, fixture_worker
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import unit_runs
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.accounts import accounts
from scripts.whoami import agent_accounts
from scripts.herdr import worker_panes

import herdr
import pane_launch


@final
class PaneWorld(herdr.Herdr):
    """A private pane server supplies process facts to the real shared scan."""

    def __init__(self) -> None:
        super().__init__("agents")
        self.locations: dict[str, dict[str, object]] = {}
        self.processes: dict[int, herdr.NativeAgentProcess] = {}
        self.foreground: dict[str, tuple[int, str, list[str]]] = {}
        self.shells: dict[str, int] = {}
        self.unknown: dict[str, str] = {}
        self.commands: list[tuple[str, ...]] = []
        self.process_reads: list[str] = []
        self.launches: list[herdr.ExistingPane | herdr.SplitPane] = []
        self.stops: list[str] = []
        self.closed: list[tuple[str, str]] = []
        self.requests: list[tuple[str, list[str], dict[str, str]]] = []
        self.outcome: herdr.PromptSubmissionNotRequired | herdr.PromptNotSubmitted = herdr.PromptSubmissionNotRequired()
        self.interrupt = ""
        self.layouts: dict[str, dict[str, object]] = {}
        self.layout_changes: list[tuple[str, float]] = []
        self.unreadable = False

    def add(self, row: staff.StaffRow, pane: str, *, live: bool = True,
            family: Literal["claude", "codex"] = "claude", terminal: str = "",
            label: str = "", workspace: str = "w1", tab: str = "w1:t1") -> None:
        previous = [identity for identity, value in self.locations.items() if value["tab_id"] == tab]
        self.locations[pane] = {"pane_id": pane, "terminal_id": terminal or "terminal-" + pane,
                                "workspace_id": workspace, "tab_id": tab, "label": label or row.name,
                                "agent_status": "idle", "focused": False}
        if previous:
            self.split_leaf(previous[-1], pane, "right" if len(previous) == 1 else "down", 0.5)
        else:
            self.layouts[tab] = {"type": "pane", "pane_id": pane}
        if live:
            self.activate(row.current_session, pane, family)

    def split_leaf(self, pane: str, new_pane: str, direction: str, ratio: float) -> None:
        tab = cast(str, self.locations[pane]["tab_id"])

        def replace(node: dict[str, object]) -> dict[str, object]:
            if node.get("type") == "pane":
                if node["pane_id"] != pane:
                    return node
                return {"type": "split", "direction": direction, "ratio": ratio,
                        "first": node, "second": {"type": "pane", "pane_id": new_pane}}
            return {**node, "first": replace(cast(dict[str, object], node["first"])),
                    "second": replace(cast(dict[str, object], node["second"]))}

        self.layouts[tab] = replace(self.layouts[tab])

    def remove_leaf(self, pane: str) -> None:
        tab = cast(str, self.locations[pane]["tab_id"])

        def remove(node: dict[str, object]) -> dict[str, object]:
            if node.get("type") == "pane":
                return {} if node["pane_id"] == pane else node
            first = remove(cast(dict[str, object], node["first"]))
            second = remove(cast(dict[str, object], node["second"]))
            return {**node, "first": first, "second": second} if first and second else first or second

        self.layouts[tab] = remove(self.layouts[tab])

    @property
    def ratios(self) -> dict[str, float]:
        result: dict[str, float] = {}

        def read(node: dict[str, object]) -> None:
            if node.get("type") != "split":
                return
            first = cast(dict[str, object], node["first"])
            if first.get("type") == "pane":
                result[cast(str, first["pane_id"])] = cast(float, node["ratio"])
            read(first)
            read(cast(dict[str, object], node["second"]))

        for root in self.layouts.values():
            read(root)
        return result

    def activate(self, identity: str, pane: str, family: Literal["claude", "codex"]) -> None:
        pid = 998600 + len(self.processes)
        native = herdr.EnvironmentThread(identity) if family == "claude" else herdr.RolloutThread(identity)
        self.processes[pid] = herdr.NativeAgentProcess(native, "fixture-generation", {})
        self.foreground[pane] = pid, family, [family, "--resume" if family == "claude" else "resume", identity]

    def native_process(self, pid: int, kind: str) -> herdr.NativeAgentProcess:
        if self.foreground and kind not in ("claude", "codex"):
            raise AssertionError(f"unexpected family {kind}")
        return self.processes[pid]

    def descendants(self, shell: herdr.PaneShell, kind: str,
                    tracked: set[herdr.AgentProcessIdentity]) -> herdr.AgentProcessPresent | herdr.PaneAtShell | herdr.PaneRunning | herdr.PaneProcessesUnreadable:
        del tracked
        pane = next((pane for pane, pid in self.shells.items() if pid == shell.pid), "")
        if pane not in self.locations or pane in self.unknown:
            return herdr.PaneProcessesUnreadable()
        process = self.foreground.get(pane)
        if process is None:
            return herdr.PaneAtShell()
        if process[1] == kind and process[0] in self.processes:
            return herdr.AgentProcessPresent((herdr.AgentProcessIdentity(process[0], 1),))
        return herdr.PaneRunning((process[1],))

    @override
    def native_pane_process(self, pane: str, kind: str, timeout: float) -> herdr.NativeAgentProcess:
        del timeout
        pid, family, _ = self.foreground[pane]
        if kind != family:
            raise herdr.HerdrFailure("wrong native family")
        return self.processes[pid]

    def launch(self, name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               prepared: herdr.PreparedAgentEnvironment, kind: str, agent_arguments: list[str],
               opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int, *, agent: str,
               reported_unknown: str = "") -> herdr.AgentLaunched:
        del name, opening, timeout_ms, reported_unknown
        if isinstance(destination, herdr.NewTab):
            raise AssertionError("a worker must not create a tab")
        self.launches.append(destination)
        self.requests.append((kind, agent_arguments.copy(), prepared.env.copy()))
        if isinstance(destination, herdr.SplitPane):
            pane = "w1:p" + str(1 + max((int(identity.split(":p")[-1]) for identity in self.locations
                                         if identity.split(":p")[-1].isdigit()), default=0))
            anchor = self.locations[destination.of_pane]
            self.locations[pane] = {**anchor, "pane_id": pane, "label": destination.label,
                                    "terminal_id": "terminal-" + pane, "focused": False}
            self.split_leaf(destination.of_pane, pane, destination.direction, destination.ratio)
        else:
            pane = destination.pane
            if pane in self.foreground:
                raise AssertionError("cannot launch in a busy pane")
        if self.interrupt == "split":
            self.interrupt = ""
            raise OSError("interrupted after split")
        identity = next((agent_arguments[index + 1] for index, argument in enumerate(agent_arguments[:-1])
                         if argument in ("--resume", "resume", "--session-id")), fixture_runtime_id("backend-" + agent))
        self.activate(identity, pane, cast(Literal["claude", "codex"], kind))
        pid, family, _ = self.foreground[pane]
        self.foreground[pane] = pid, family, [kind, *agent_arguments]
        fixture_session(identity, staff.ClaudeBackend() if kind == "claude" else
                        staff.CodexBackend(Path(prepared.env["CODEX_HOME"])))
        if self.interrupt == "agent":
            self.interrupt = ""
            raise OSError("interrupted after agent start")
        return herdr.AgentLaunched(pane, self.outcome)

    def stop(self, row: staff.StaffRow, client: herdr.Herdr) -> bool:
        if client is not self:
            raise AssertionError("stop uses another client")
        self.stops.append(str(row.current_session))
        for pane, (pid, _, _) in tuple(self.foreground.items()):
            if self.processes[pid].current_identity == row.current_session:
                del self.foreground[pane]
        return True

    @override
    def socket_request(self, method: str, **params: object) -> dict[str, object]:
        if method == "layout.set_split_ratio":
            pane = cast(str, params["pane_id"])
            ratio = cast(float, params["ratio"])
            path = cast(list[bool], params["path"])
            node = self.layouts[cast(str, self.locations[pane]["tab_id"])]
            for second in path:
                if node.get("type") != "split":
                    raise AssertionError(f"ratio path has no split: {path}")
                node = cast(dict[str, object], node["second" if second else "first"])
            if node.get("type") != "split":
                raise AssertionError(f"ratio path has no split: {path}")
            node["ratio"] = ratio
            self.layout_changes.append((pane, ratio))
            return {}
        if method != "layout.export":
            raise AssertionError(f"unexpected socket request {method}: {params}")
        return {"layout": {"root": deepcopy(self.layouts[cast(str, params["tab_id"])])}}

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        del deadline
        if arguments[:2] == ("pane", "list"):
            if self.unreadable:
                raise herdr.HerdrFailure("pane inventory unreadable")
            return {"panes": [dict(value) for value in self.locations.values()]}
        if arguments[:2] == ("pane", "process-info"):
            pane = arguments[-1]
            self.process_reads.append(pane)
            if pane in self.unknown:
                raise herdr.HerdrFailure(self.unknown[pane])
            shell = self.shells.setdefault(pane, 998000 + len(self.shells))
            pid, name, argv = self.foreground.get(pane, (shell, "zsh", ["zsh"]))
            return {"process_info": {"shell_pid": shell, "foreground_process_group_id": pid,
                                      "foreground_processes": [{"pid": pid, "name": name, "argv": argv}]}}
        if arguments[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": pane, "agent": "worker", "agent_status": self.locations[pane]["agent_status"]}
                                for pane in self.foreground]}
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "unit", "focused": True},
                                   {"workspace_id": "w2", "label": "other", "focused": False}]}
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "unit"},
                             {"tab_id": "w2:t2", "workspace_id": "w2", "label": "other"}]}
        raise AssertionError(f"unexpected server read {arguments}")

    @override
    def run(self, *arguments: str, deadline: float = math.inf) -> str:
        del deadline
        self.commands.append(arguments)
        if arguments[:2] == ("pane", "close"):
            self.closed.append((arguments[2], str(self.locations[arguments[2]]["terminal_id"])))
            self.remove_leaf(arguments[2])
            _ = self.locations.pop(arguments[2])
            _ = self.foreground.pop(arguments[2], None)
        return ""


class WorkerPaneTests(StaffTestCase):
    world: PaneWorld = PaneWorld()
    parent_id: staff.StaffId = staff.StaffId("")

    @property
    def parent(self) -> staff.StaffRow:
        row = staff.by_id(self.parent_id)
        assert row is not None
        return row

    @override
    def setUp(self) -> None:
        super().setUp()
        showrunner = fixture_member("showrunner", name="showrunner")
        self.parent_id = fixture_member("unit", name="unit", parent=showrunner.id,
                                        worktree=staff.PresentWorktree("natedev", self.root)).id
        self.world = PaneWorld()
        self.world.add(self.parent, "w1:p0")
        config = self.root / "conf"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "WORKER_TRANSPORT=mesh\n# range: 0.1..0.9\nWORKER_SPLIT_RATIO=0.667\n")
        registry = write_registry(config / "models.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"), "CONF_DIR": str(config),
            "MODELS_CONFIG_FILE": str(registry), "MODELS_CONFIG_PUBLISH": "0",
            "HERDR_BIN": "/nonexistent/herdr", "TMPDIR": str(self.root),
            "UNIT_RUN_DIR": str(self.root / "run"),
        })))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.world))
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "unit-runs"))
        _ = self.enterContext(patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former-runs"))
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=self.world.native_process))
        _ = self.enterContext(patch.object(herdr, "_startup_processes", side_effect=self.world.descendants))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.world.launch))
        _ = self.enterContext(patch.object(restart, "stop_row", side_effect=self.world.stop))
        _ = self.enterContext(patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.root / "codex")))
        _ = self.enterContext(patch.object(accounts, "account_for_unscoped_launch", return_value=accounts.LegacyHomeLaunch()))

    def worker(self, name: str = "impl2", *, family: Literal["claude", "codex"] = "claude") -> staff.StaffRow:
        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(self.root / "codex")
        row = fixture_worker(name, self.parent.id, backend=backend)
        current = staff.native_id(fixture_runtime_id("current-" + name))
        result = staff.update_current_session(row.current_session, current)
        fixture_session(current, backend)
        return result

    def observation(self, row: staff.StaffRow) -> worker_panes.WorkerPane:
        return next(item for item in worker_panes.observe(self.parent.id, staff.snapshot()) if item.row.id == row.id)

    def start(self, name: str = "impl2", role: staff.WorkerRole | worker_panes.WorkerRoleUnavailable = "impl",
              *, account: worker_panes.ConfiguredWorkerAccount | worker_panes.DestinationWorkerAccount = worker_panes.ConfiguredWorkerAccount.CURRENT) -> worker_panes.WorkerStart:
        return worker_panes.start_or_resume(self.parent.id, staff.WorkerName.parse(name), role, account=account)

    def command(self, *arguments: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker_panes.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def dispatch(self, row: staff.StaffRow, role: staff.WorkerRole, *, completed: bool = False) -> None:
        run = self.root / "run"
        run.mkdir(exist_ok=True)
        write_text_atomic(run / "progress_history_state.json", json.dumps({"pass": {
            row.name: {"instance_id": "dispatch-" + row.name, "worker_name": row.name,
                       "team_slot": row.name, "kind": role, "status": "completed" if completed else "active"}}}))

    def completed_dispatch(self, row: staff.StaffRow, role: staff.WorkerRole) -> None:
        self.dispatch(row, role, completed=True)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
        source = worker_admission.transcript_source(row, backend)
        message = "The task is complete.\nDONE dispatch-" + row.name
        entry = ({"type": "event_msg", "payload": {"type": "agent_message", "message": message}}
                 if isinstance(backend, staff.CodexBackend) else
                 {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": message}]}})
        write_text_atomic(source, json.dumps(entry) + "\n")

    def test_dispatch_holder_starts_its_worker_and_keeps_other_operations_out(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        name = staff.WorkerName.parse(row.name)
        with worker_panes.worker_lock(self.parent.id, name) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            opened = worker_panes.start_or_resume(self.parent.id, name, "impl", lock=hold)
            self.assertIsInstance(opened, worker_panes.OpenWorker)
            before = self.staff_state()
            launches = self.world.launches.copy()
            with worker_panes.worker_lock(self.parent.id, name) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
            self.assertIsInstance(worker_panes.close(self.parent.id, name), worker_panes.Kept)
            refused = worker_panes.start_or_resume(self.parent.id, name, "review")
            self.assertIsInstance(refused, worker_panes.StartRefused)
            self.assertEqual(self.staff_state(), before)
            self.assertEqual(self.world.launches, launches)
            self.assertEqual(self.world.closed, [])
            self.assertEqual(self.world.stops, [])

    def test_creation_holder_acquires_the_published_fixed_id_lock(self) -> None:
        name = staff.WorkerName.parse("new-worker")
        with worker_panes.worker_lock(self.parent.id, name) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            result = worker_panes.start_or_resume(self.parent.id, name, "impl", lock=hold)
            self.assertIsInstance(result, worker_panes.OpenWorker)
            assert isinstance(result, worker_panes.OpenWorker)
            self.assertTrue(hold.owns(result.row))
            replacement = staff.native_id(fixture_runtime_id("replacement-native-session"))
            changed = staff.update_current_session(result.row.current_session, replacement)
            self.assertNotEqual(changed.id, changed.current_session)
            with worker_panes.worker_lock(self.parent.id, name) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.parent.id)), 1)

    def test_creation_lock_refuses_a_second_unpublished_dispatch(self) -> None:
        name = staff.WorkerName.parse("new-worker")
        with worker_panes.worker_lock(self.parent.id, name) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            with worker_panes.worker_lock(self.parent.id, name) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
            opened = worker_panes.start_or_resume(self.parent.id, name, "impl", lock=hold)
            self.assertIsInstance(opened, worker_panes.OpenWorker)
        self.assertEqual(len(self.world.launches), 1)

    def test_dispatch_holder_can_change_a_completed_launch_policy(self) -> None:
        opened = self.start()
        assert isinstance(opened, worker_panes.OpenWorker)
        self.completed_dispatch(opened.row, "impl")
        name = staff.WorkerName.parse(opened.row.name)
        with worker_panes.worker_lock(self.parent.id, name) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            changed = worker_panes.start_or_resume(self.parent.id, name, "review", lock=hold)
        self.assertIsInstance(changed, worker_panes.OpenWorker)
        assert isinstance(changed, worker_panes.OpenWorker)
        self.assertEqual((changed.row.id, changed.row.current_session, changed.terminal),
                         (opened.row.id, opened.row.current_session, opened.terminal))
        self.assertEqual(self.world.stops, [opened.row.current_session])
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.world.requests[-1][1][self.world.requests[-1][1].index("--sandbox") + 1], "read-only")

    def test_close_permission_leaves_the_pane_open_until_close_completes(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        observed = self.observation(row)
        assert isinstance(observed, worker_panes.StoppedWorker)
        self.assertIsInstance(worker_panes.can_close(observed), worker_panes.CloseAllowed)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(set(self.world.locations), {"w1:p0", "w1:p1"})
        self.assertIsInstance(worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name)), worker_panes.Closed)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])

    def test_completed_second_review_stage_allows_closure(self) -> None:
        row = self.worker(family="codex")
        self.world.add(row, "w1:p1", family="codex")
        self.completed_dispatch(row, "review")
        source = worker_admission.transcript_source(row, staff.CodexBackend(self.root / "codex"))
        write_text_atomic(source, source.read_text().replace(f"DONE dispatch-{row.name}", f"DONE dispatch-{row.name}-2"))
        observed = self.observation(row)
        assert isinstance(observed, worker_panes.OpenWorker)
        self.assertIsInstance(worker_panes.can_close(observed), worker_panes.CloseAllowed)
        self.assertEqual(self.world.closed, [])

    def test_close_holder_keeps_the_fixed_id_locked_until_row_retirement(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        name = staff.WorkerName.parse(row.name)
        with worker_panes.worker_lock(self.parent.id, name) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            self.assertIsInstance(worker_panes.close(self.parent.id, name, lock=hold), worker_panes.Closed)
            self.assertEqual(staff.snapshot().by_id(row.id), row)
            with worker_panes.worker_lock(self.parent.id, name) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
            self.assertEqual(staff.retire(row.id), row)
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.parent.id), [])
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])

    def test_dispatch_role_reports_missing_history_and_uses_the_recorded_role(self) -> None:
        row = self.worker("impl2")
        self.assertIsInstance(worker_panes.dispatch_role(row), worker_panes.WorkerRoleUnavailable)
        self.dispatch(row, "astra-reviewer")
        self.assertEqual(worker_panes.dispatch_role(row), "astra-reviewer")
        self.assertEqual(staff.snapshot().by_id(row.id), row)

    def test_herdr_pinned_close_ignores_a_mesh_phase_lock(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        run = self.root / "run"
        run.mkdir()
        write_text_atomic(run / "worker_transport", "herdr\n")
        phase = run / "phases/8"
        phase.mkdir(parents=True)
        with (phase / "dispatch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])

    def test_start_rerun_after_launch_proof_keeps_the_same_backend_and_pane(self) -> None:
        first = self.start()
        self.assertIsInstance(first, worker_panes.OpenWorker)
        assert isinstance(first, worker_panes.OpenWorker)
        self.dispatch(first.row, "impl")
        before = self.staff_state()
        locations = {key: dict(value) for key, value in self.world.locations.items()}
        repeated = self.start()
        self.assertEqual(repeated, first)
        self.assertEqual(self.staff_state(), before)
        self.assertEqual(self.world.locations, locations)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.stops, [])

    def test_worker_name_does_not_select_the_dispatch_role(self) -> None:
        result = self.start("impl2", "review")
        self.assertIsInstance(result, worker_panes.OpenWorker)
        kind, arguments, environment = self.world.requests[0]
        self.assertEqual(kind, "codex")
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertEqual(environment["UNIT_TEAM_ROLE"], "impl2")
        self.assertNotIn("STAFF_ID", environment)
        self.assertEqual(environment["TMPDIR"], str(self.root))

    def test_all_explicit_roles_reach_their_registry_settings(self) -> None:
        for role in staff.WORKER_ROLES:
            with self.subTest(role=role):
                result = self.start("worker-" + role, role)
                self.assertIsInstance(result, worker_panes.OpenWorker)
                _, arguments, environment = self.world.requests[-1]
                self.assertEqual(environment["UNIT_TEAM_ROLE"], "worker-" + role)
                self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-test")
                if role in ("review", "astra-reviewer"):
                    self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
                else:
                    self.assertNotEqual(arguments[arguments.index("--sandbox") + 1], "read-only")

    def test_two_workers_with_one_role_have_separate_native_ids_and_recorder_slots(self) -> None:
        first, second = self.start("left", "test"), self.start("right", "test")
        self.assertIsInstance(first, worker_panes.OpenWorker)
        self.assertIsInstance(second, worker_panes.OpenWorker)
        assert isinstance(first, worker_panes.OpenWorker) and isinstance(second, worker_panes.OpenWorker)
        self.assertNotEqual(first.row.id, second.row.id)
        self.assertNotEqual(first.terminal, second.terminal)
        self.assertEqual([request[2]["UNIT_TEAM_ROLE"] for request in self.world.requests], ["left", "right"])

    def test_first_split_uses_the_private_live_ratio_and_the_next_split_is_below_it(self) -> None:
        write_text_atomic(self.root / "conf/direct.conf", "# range: 0.1..0.9\nWORKER_SPLIT_RATIO=0.25\n")
        first = self.start("first")
        self.assertIsInstance(first, worker_panes.OpenWorker)
        assert isinstance(first, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches[0], herdr.SplitPane("w1:p0", "right", 0.25, "first"))
        write_text_atomic(self.root / "conf/direct.conf", "# range: 0.1..0.9\nWORKER_SPLIT_RATIO=0.75\n")
        second = self.start("second")
        self.assertIsInstance(second, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches[1], herdr.SplitPane(first.pane, "down", 0.75, "second"))

    def test_start_rerun_after_agent_exit_resumes_current_session_in_the_same_terminal(self) -> None:
        row = self.worker(family="codex")
        self.world.add(row, "w1:p1", live=False)
        result = self.start()
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        self.assertEqual((result.row.id, result.row.current_session, result.pane, result.terminal),
                         (row.id, row.current_session, "w1:p1", "terminal-w1:p1"))
        self.assertEqual(self.world.launches, [herdr.ExistingPane("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.world.requests[0][1][:2], ["resume", row.current_session])
        self.assertEqual(self.world.closed, [])

    def test_incomplete_start_keeps_the_agent_and_does_not_launch_twice(self) -> None:
        self.world.outcome = herdr.PromptNotSubmitted("startup prompt not submitted")
        result = self.start()
        self.assertIsInstance(result, worker_panes.IncompleteStart)
        assert isinstance(result, worker_panes.IncompleteStart)
        self.dispatch(result.row, "impl")
        repeated = self.start()
        self.assertIsInstance(repeated, worker_panes.OpenWorker)
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn(result.pane, self.world.locations)

    def test_failed_publication_retains_one_backend_and_publishes_once_on_rerun(self) -> None:
        with patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("fixture publication failure")):
            failed = self.start()
        self.assertIsInstance(failed, worker_panes.UnpublishedWorker)
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.parent.id), [])
        self.assertEqual(len(self.world.launches), 1)
        repeated = self.start()
        self.assertIsInstance(repeated, worker_panes.OpenWorker)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.parent.id)), 1)

    def test_start_rerun_after_split_reuses_the_labelled_shell(self) -> None:
        self.world.interrupt = "split"
        try:
            _ = self.start()
        except OSError:
            pass
        self.assertEqual(len(self.world.locations), 2)
        pane = next(key for key in self.world.locations if key != "w1:p0")
        terminal = self.world.locations[pane]["terminal_id"]
        repeated = self.start()
        self.assertIsInstance(repeated, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches[-1], herdr.ExistingPane(pane, str(terminal)))
        self.assertEqual(len(self.world.locations), 2)
        self.assertEqual(self.world.closed, [])

    def test_start_rerun_after_agent_start_publishes_the_existing_backend(self) -> None:
        self.world.interrupt = "agent"
        try:
            _ = self.start()
        except OSError:
            pass
        identity = self.world.processes[self.world.foreground["w1:p1"][0]].current_identity
        result = self.start()
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        self.assertEqual((result.row.current_session, result.pane), (identity, "w1:p1"))
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.closed, [])

    def test_start_rerun_after_row_write_retains_the_published_native_row(self) -> None:
        publish = staff.ensure_worker

        def interrupted(identity: staff.StaffId, name: str, parent: staff.StaffId) -> staff.StaffRow:
            _ = publish(identity, name, parent)
            raise OSError("interrupted after row write")

        with patch.object(staff, "ensure_worker", side_effect=interrupted):
            _ = self.start()
        rows = staff.snapshot().find(kind="worker", parent=self.parent.id)
        self.assertEqual(len(rows), 1)
        before = self.staff_state()
        result = self.start()
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        self.assertEqual(result.row, rows[0])
        self.assertEqual(self.staff_state(), before)
        self.assertEqual(len(self.world.launches), 1)

    def test_concurrent_starts_publish_one_row_and_one_pane(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as callers:
            results = list(callers.map(self.start, ("shared", "shared")))
        self.assertTrue(all(isinstance(result, worker_panes.OpenWorker) for result in results), results)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.parent.id)), 1)
        self.assertEqual(len(self.world.launches), 1)

    def test_close_rerun_after_pane_close_before_layout_matches_one_clean_close(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        siblings = [self.worker("sibling-" + str(index)) for index in range(3)]
        for index, sibling in enumerate(siblings, 2):
            self.world.add(sibling, "w1:p" + str(index))
        request = self.world.socket_request
        failed = False

        def interrupt(method: str, **params: object) -> dict[str, object]:
            nonlocal failed
            if method == "layout.export" and not failed:
                failed = True
                raise OSError("interrupted after pane close")
            return request(method, **params)

        with patch.object(self.world, "socket_request", side_effect=interrupt):
            with self.assertRaises(OSError):
                _ = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.world.ratios["w1:p2"], 1 / 3)
        changes = self.world.layout_changes.copy()
        locations = {key: dict(value) for key, value in self.world.locations.items()}
        _ = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertEqual(self.world.layout_changes, changes)
        self.assertEqual(self.world.locations, locations)

    def test_active_dispatch_role_change_keeps_every_file_and_process(self) -> None:
        result = self.start()
        assert isinstance(result, worker_panes.OpenWorker)
        self.dispatch(result.row, "impl")
        before = self.staff_state()
        changed = self.start(role="review")
        self.assertIsInstance(changed, worker_panes.StartRefused)
        assert isinstance(changed, worker_panes.StartRefused)
        self.assertEqual(changed.reason, worker_panes.StartRefusal.BUSY_ROLE_CHANGE)
        self.assertIn("active", changed.detail)
        self.assertEqual(self.staff_state(), before)
        self.assertEqual((len(self.world.launches), self.world.stops), (1, []))

    def test_same_role_keeps_a_hand_selected_model_after_a_registry_edit(self) -> None:
        result = self.start()
        assert isinstance(result, worker_panes.OpenWorker)
        self.dispatch(result.row, "impl")
        pid, kind, arguments = self.world.foreground[result.pane]
        changed = [*arguments, "--model", "hand-selected-model"]
        self.world.foreground[result.pane] = pid, kind, changed
        _ = write_registry(self.root / "conf/models.conf", {"direct.codex": {"impl": "gpt-test:low"}})
        self.assertEqual(self.start(), result)
        self.assertEqual((len(self.world.launches), self.world.stops), (1, []))

    def test_restore_without_a_dispatch_role_has_a_distinct_refusal(self) -> None:
        before = self.staff_state()
        result = self.start(role=worker_panes.WorkerRoleUnavailable("no saved dispatch role"))
        self.assertIsInstance(result, worker_panes.StartRefused)
        assert isinstance(result, worker_panes.StartRefused)
        self.assertEqual((result.reason, result.detail),
                         (worker_panes.StartRefusal.ROLE_UNAVAILABLE, "no saved dispatch role"))
        self.assertEqual(self.staff_state(), before)
        self.assertEqual(self.world.launches, [])

    def test_unknown_role_cli_names_the_cause_without_creating_a_backend(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.parent.current_session}):
            code, output, errors = self.command("start-or-resume", "new", "--role", "missing-role")
        self.assertEqual((code, output), (worker_panes.ExitCode.REFUSED, ""))
        self.assertIn("unknown role", errors)
        self.assertIn("missing-role", errors)
        self.assertEqual(self.world.launches, [])

    def test_worker_caller_cannot_admit_another_worker(self) -> None:
        row = self.worker()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}):
            code, _, errors = self.command("start-or-resume", "new", "--role", "impl")
        self.assertEqual(code, worker_panes.ExitCode.REFUSED)
        self.assertIn("caller", errors)
        self.assertEqual(self.world.launches, [])

    def test_showrunner_can_start_a_worker(self) -> None:
        self.parent_id = fixture_member("showrunner", name="showrunner").id
        self.world.foreground.clear()
        self.world.add(self.parent, "w1:p0")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.parent.current_session}):
            code, _, errors = self.command("start-or-resume", "helper", "--role", "test")
        self.assertEqual(code, worker_panes.ExitCode.OK, errors)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.parent.id)), 1)

    def test_unusable_configuration_opens_no_split(self) -> None:
        for value in ("0.01", "0.99", "invalid"):
            with self.subTest(value=value):
                write_text_atomic(self.root / "conf/direct.conf", f"WORKER_SPLIT_RATIO={value}\n")
                result = self.start()
                self.assertIsInstance(result, worker_panes.StartRefused)
                assert isinstance(result, worker_panes.StartRefused)
                self.assertEqual(result.reason, worker_panes.StartRefusal.INVALID_ROLE_CONFIGURATION)
                self.assertEqual(self.world.launches, [])

    def test_invalid_live_ratio_uses_the_committed_ratio_and_prints_a_warning(self) -> None:
        for arguments in (("init", "-q"), ("add", "conf/direct.conf"),
                          ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "-qm", "private configuration")):
            _ = subprocess.run(["git", "-C", str(self.root), *arguments], check=True, capture_output=True,
                               text=True, timeout=TEST_HANG_GUARD_SECS)
        write_text_atomic(self.root / "conf/direct.conf", "# range: 0.1..0.9\nWORKER_SPLIT_RATIO=invalid\n")
        with redirect_stderr(io.StringIO()) as errors:
            result = self.start()
        self.assertIsInstance(result, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches, [herdr.SplitPane("w1:p0", "right", 0.667, "impl2")])
        self.assertIn("warning:", errors.getvalue())

    def test_invalid_registry_settings_are_reported_before_backend_creation(self) -> None:
        _ = write_registry(self.root / "conf/models.conf", {"direct.codex": {"impl": "missing:high"}})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.parent.current_session}):
            code, output, errors = self.command("start-or-resume", "new", "--role", "impl")
        self.assertEqual((code, output), (worker_panes.ExitCode.REFUSED, ""))
        self.assertIn("invalid role configuration", errors)
        self.assertIn("missing", errors)
        self.assertEqual(self.world.launches, [])

    def test_each_family_resumes_on_destination_and_source_accounts_in_one_terminal(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                _ = write_registry(self.root / "conf/models.conf", {"assignments": {"direct": family}})
                row = self.worker("account-" + family, family=family)
                pane = "w1:p-" + family
                self.world.add(row, pane, live=False)
                for label in (family + "2", family + "1"):
                    target = self.root / label
                    with (patch.object(agent_accounts, "codex_account_home",
                                       return_value=agent_accounts.CodexHomeHoldingLogin(label, target, "fixture-login")),
                          patch.object(accounts, "account_directory_readiness", return_value=accounts.AccountDirectoryReady("fixture-login")),
                          patch.object(accounts, "prepare_claude_launch",
                                       return_value=accounts.PreparedAccount({"CLAUDE_CONFIG_DIR": str(target)}, frozenset()))):
                        result = self.start(row.name, account=worker_panes.DestinationWorkerAccount(label))
                    self.assertIsInstance(result, worker_panes.OpenWorker)
                    assert isinstance(result, worker_panes.OpenWorker)
                    self.assertEqual((result.row.current_session, result.pane, result.terminal),
                                     (row.current_session, pane, "terminal-" + pane))
                    arguments, environment = self.world.requests[-1][1:]
                    self.assertIn(row.current_session, arguments)
                    self.assertEqual(environment["CODEX_HOME" if family == "codex" else "CLAUDE_CONFIG_DIR"], str(target))
                    self.assertNotIn("STAFF_ID", environment)
                    del self.world.foreground[pane]
                self.assertEqual(self.world.closed, [])

    def test_completed_dispatch_can_change_policy_in_both_directions_without_changing_identity(self) -> None:
        result = self.start()
        assert isinstance(result, worker_panes.OpenWorker)
        first = result
        transitions: tuple[tuple[staff.WorkerRole, staff.WorkerRole, str], ...] = (
            ("impl", "review", "read-only"), ("review", "impl", "danger-full-access"))
        for old, requested, sandbox in transitions:
            with self.subTest(requested=requested):
                self.completed_dispatch(result.row, old)
                changed = self.start(role=requested)
                self.assertIsInstance(changed, worker_panes.OpenWorker)
                assert isinstance(changed, worker_panes.OpenWorker)
                self.assertEqual((changed.row, changed.pane, changed.terminal), (first.row, first.pane, first.terminal))
                arguments = self.world.requests[-1][1]
                self.assertEqual(arguments[arguments.index("--sandbox") + 1], sandbox)
                result = changed
        self.assertEqual(self.world.stops, [str(first.row.current_session)] * 2)
        self.assertEqual(self.world.closed, [])

    def test_native_ids_with_equal_first_32_characters_stop_only_the_selected_worker(self) -> None:
        selected = self.worker("selected", family="codex")
        sibling = self.worker("sibling", family="codex")
        selected = staff.update_current_session(selected.current_session, staff.native_id("11111111-1111-4111-8111-111111111111"))
        sibling = staff.update_current_session(sibling.current_session, staff.native_id("11111111-1111-4111-8111-111111112222"))
        fixture_session(selected.current_session, staff.CodexBackend(self.root / "codex"))
        fixture_session(sibling.current_session, staff.CodexBackend(self.root / "codex"))
        self.world.add(selected, "w2:p9", family="codex", workspace="w2", tab="w2:t2", label="changed")
        self.world.add(sibling, "w1:p2", family="codex")
        self.completed_dispatch(selected, "impl")
        result = worker_panes.close(self.parent.id, staff.WorkerName.parse(selected.name))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(self.world.stops, [str(selected.current_session)])
        self.assertEqual(self.world.closed, [("w2:p9", "terminal-w2:p9")])
        self.assertEqual(set(self.world.locations), {"w1:p0", "w1:p2"})
        self.assertEqual(self.world.processes[self.world.foreground["w1:p2"][0]].current_identity, sibling.current_session)

    def test_busy_worker_close_keeps_the_agent_and_names_the_blocker(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1")
        before = self.staff_state()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.parent.current_session}):
            code, _, errors = self.command("close", row.name)
        self.assertEqual(code, worker_panes.ExitCode.KEPT)
        self.assertIn("completed pass", errors)
        self.assertEqual(self.staff_state(), before)
        self.assertEqual((self.world.closed, self.world.stops), ([], []))

    def test_close_rerun_after_agent_stop_closes_only_that_terminal_once(self) -> None:
        row = self.worker()
        sibling = self.worker("sibling")
        self.world.add(row, "w1:p1", live=False)
        self.world.add(sibling, "w1:p2")
        before = self.staff_state()
        first = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(first, worker_panes.Closed)
        repeated = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(repeated, worker_panes.Closed)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(set(self.world.locations), {"w1:p0", "w1:p2"})
        self.assertEqual(self.staff_state(), before)
        self.assertEqual(self.world.stops, [])

    def test_close_rereads_the_terminal_and_shell_before_any_close(self) -> None:
        for change in ("moved", "busy", "duplicate"):
            with self.subTest(change=change):
                row = self.worker(change)
                pane = "w1:p-" + change
                self.world.add(row, pane, live=False)
                read = self.world.panes
                count = 0

                def changed_panes() -> list[herdr.PaneIdentity]:
                    nonlocal count
                    count += 1
                    if count == 2:
                        if change == "moved":
                            item = self.world.locations.pop(pane)
                            self.world.locations["w2:p9"] = {**item, "pane_id": "w2:p9"}
                        elif change == "duplicate":
                            self.world.locations["w2:p9"] = {**self.world.locations[pane], "pane_id": "w2:p9"}
                        else:
                            self.world.foreground[pane] = (998598, "bash", ["bash", "long-work"])
                    return read()

                with patch.object(self.world, "panes", side_effect=changed_panes):
                    result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
                self.assertIsInstance(result, worker_panes.UncertainWorker)
                self.assertEqual(self.world.closed, [])
                self.world.locations = {"w1:p0": self.world.locations["w1:p0"]}
                self.world.foreground = {"w1:p0": self.world.foreground["w1:p0"]}

    def test_a_dispatch_lock_keeps_a_stopped_pane_open(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        run = self.root / "run"
        run.mkdir()
        with (run / "dispatch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(result, worker_panes.Kept)
        assert isinstance(result, worker_panes.Kept)
        self.assertIn("lock", result.blocker)
        self.assertEqual(self.world.closed, [])

    def test_stopped_worker_close_honors_the_phase_dispatch_lock(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        phase = self.root / "run/phases/7"
        phase.mkdir(parents=True)
        with (phase / "dispatch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(result, worker_panes.Kept)
        assert isinstance(result, worker_panes.Kept)
        self.assertEqual(result.blocker, "dispatch lock is held")
        self.assertEqual(self.world.closed, [])
        self.assertEqual(set(self.world.locations), {"w1:p0", "w1:p1"})

    def test_stale_session_mark_does_not_prevent_proved_shell_recovery(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1", live=False)
        self.world.locations["w1:p1"]["agent_session"] = {"value": fixture_runtime_id("stale-mark")}
        observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.StoppedWorker)
        result = self.start()
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        self.assertEqual((result.row, result.pane, result.terminal), (row, "w1:p1", "terminal-w1:p1"))
        self.assertEqual(self.world.launches, [herdr.ExistingPane("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.world.closed, [])

    def test_new_worker_split_follows_the_lowest_pane_after_close_and_resume(self) -> None:
        first = self.start("a")
        second = self.start("b")
        assert isinstance(first, worker_panes.OpenWorker)
        assert isinstance(second, worker_panes.OpenWorker)
        self.completed_dispatch(first.row, "impl")
        self.assertIsInstance(worker_panes.close(self.parent.id, staff.WorkerName.parse("a")), worker_panes.Closed)
        resumed = self.start("a")
        assert isinstance(resumed, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches[-1], herdr.SplitPane(second.pane, "down", 0.667, "a"))
        third = self.start("c")
        self.assertIsInstance(third, worker_panes.OpenWorker)
        self.assertEqual(self.world.launches[-1], herdr.SplitPane(resumed.pane, "down", 0.667, "c"))
        self.assertEqual(self.world.closed, [(first.pane, first.terminal)])

    def test_layout_balances_each_split_in_a_nested_worker_column(self) -> None:
        for name, pane in (("a", "w1:p1"), ("b", "w1:p2"), ("c", "w1:p3")):
            self.world.add(self.worker(name), pane)
        self.world.layouts["w1:t1"] = {
            "type": "split", "direction": "right", "ratio": 0.667,
            "first": {"type": "pane", "pane_id": "w1:p0"},
            "second": {"type": "split", "direction": "down", "ratio": 0.5,
                       "first": {"type": "split", "direction": "down", "ratio": 0.8,
                                 "first": {"type": "pane", "pane_id": "w1:p1"},
                                 "second": {"type": "pane", "pane_id": "w1:p2"}},
                       "second": {"type": "pane", "pane_id": "w1:p3"}},
        }
        expected = deepcopy(self.world.layouts["w1:t1"])
        column = cast(dict[str, object], expected["second"])
        column["ratio"] = 2 / 3
        cast(dict[str, object], column["first"])["ratio"] = 1 / 2
        result = worker_panes.close(self.parent.id, staff.WorkerName.parse("absent"))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(self.world.layouts["w1:t1"], expected)
        self.assertEqual(self.world.closed, [])

    def test_start_rerun_finishes_layout_after_a_failed_layout_read(self) -> None:
        for name in ("a", "b", "c"):
            self.assertIsInstance(self.start(name), worker_panes.OpenWorker)
        clean = deepcopy(self.world.layouts)
        row = next(row for row in staff.snapshot().find(kind="worker", parent=self.parent.id) if row.name == "c")
        self.completed_dispatch(row, "impl")
        self.assertIsInstance(worker_panes.close(self.parent.id, staff.WorkerName.parse("c")), worker_panes.Closed)
        request = self.world.socket_request
        interrupted = False

        def interrupt(method: str, **params: object) -> dict[str, object]:
            nonlocal interrupted
            if method == "layout.export" and len(self.world.launches) == 4 and not interrupted:
                interrupted = True
                raise OSError("interrupted before layout")
            return request(method, **params)

        with patch.object(self.world, "socket_request", side_effect=interrupt):
            with self.assertRaises(OSError):
                _ = self.start("c")
        result = self.start("c")
        self.assertIsInstance(result, worker_panes.OpenWorker)
        self.assertEqual(self.world.layouts, clean)
        self.assertEqual(len(self.world.launches), 4)

    def test_close_rerun_retires_the_record_after_confirmed_pane_closure(self) -> None:
        row = self.worker(family="codex")
        self.world.add(row, "w1:p1", live=False, family="codex")
        path = pane_launch.record_directory() / "agents.json"
        entry: pane_launch.PaneLaunch = {
            "kind": "codex", "pane_id": "w1:p1", "session_id": str(row.current_session),
            "config_dir": str(self.root / "codex"), "cwd": str(self.root), "name": row.name,
            "model": "gpt-test", "remote_control": False, "environment": {}, "flags_read": True,
            "written_at": "2026-11-05T00:00:00+00:00",
        }
        pane_launch.write(path, entry)
        with patch.object(pane_launch, "retire_launch", side_effect=OSError("interrupted before record retirement")):
            with self.assertRaises(OSError):
                _ = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(list(pane_launch.read_valid_entries(path)), ["w1:p1"])
        result = worker_panes.close(self.parent.id, staff.WorkerName.parse(row.name))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])

    def test_lifecycle_commands_report_done_on_completion_and_rerun(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.parent.current_session)}):
            self.assertEqual(self.command("start-or-resume", "a", "--role", "impl"), (0, "done\n", ""))
            self.assertEqual(self.command("start-or-resume", "a", "--role", "impl"), (0, "done\n", ""))
            row = next(row for row in staff.snapshot().find(kind="worker", parent=self.parent.id) if row.name == "a")
            self.completed_dispatch(row, "impl")
            self.assertEqual(self.command("close", "a"), (0, "done\n", ""))
            self.assertEqual(self.command("close", "a"), (0, "done\n", ""))
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.closed, [("w1:p1", "terminal-w1:p1")])

    def test_unknown_process_and_structural_fault_do_not_block_an_unrelated_close(self) -> None:
        unknown, duplicate, stopped = self.worker("unknown"), self.worker("duplicate"), self.worker("stopped")
        self.world.add(unknown, "w1:p1")
        self.world.unknown["w1:p1"] = "unknown worker process"
        self.world.add(duplicate, "w1:p2")
        self.world.add(duplicate, "w2:p9", workspace="w2", tab="w2:t2")
        self.world.add(stopped, "w1:p3", live=False)
        result = worker_panes.close(self.parent.id, staff.WorkerName.parse(stopped.name))
        self.assertIsInstance(result, worker_panes.Closed)
        self.assertEqual(self.world.closed, [("w1:p3", "terminal-w1:p3")])
        self.assertEqual(set(self.world.locations), {"w1:p0", "w1:p1", "w1:p2", "w2:p9"})

    def test_account_readiness_failure_keeps_the_stopped_terminal(self) -> None:
        row = self.worker(family="codex")
        self.world.add(row, "w1:p1", live=False)
        with patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexSetupHome("codex2", self.root / "codex2")):
            result = self.start(account=worker_panes.DestinationWorkerAccount("codex2"))
        self.assertIsInstance(result, worker_panes.StartRefused)
        assert isinstance(result, worker_panes.StartRefused)
        self.assertEqual(result.reason, worker_panes.StartRefusal.ACCOUNT_UNAVAILABLE)
        self.assertEqual((self.world.launches, self.world.closed, self.world.stops), ([], [], []))

    def test_busy_unpublished_recovery_pane_is_refused_and_kept(self) -> None:
        showrunner = fixture_member("showrunner", name="foreign-showrunner")
        foreign = fixture_member("unit", name="foreign", parent=showrunner.id)
        self.world.add(foreign, "w1:p1", label="impl2")
        result = self.start()
        self.assertIsInstance(result, worker_panes.StartRefused)
        assert isinstance(result, worker_panes.StartRefused)
        self.assertEqual(result.reason, worker_panes.StartRefusal.PANE_NOT_AT_SHELL)
        self.assertEqual((self.world.launches, self.world.closed, self.world.stops), ([], [], []))

    def test_observe_uses_current_session_after_label_and_workspace_change(self) -> None:
        row = self.worker()
        self.world.add(row, "w2:p9", label="changed-label", workspace="w2", tab="w2:t2")
        observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.OpenWorker)
        assert isinstance(observed, worker_panes.OpenWorker)
        self.assertEqual((observed.row, observed.pane, observed.terminal, observed.status),
                         (row, "w2:p9", "terminal-w2:p9", herdr.AgentStatus.IDLE))
        self.assertEqual(self.world.process_reads.count("w2:p9"), 1)

    def test_a_former_native_identity_does_not_prove_the_current_worker_live(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1")
        self.world.activate(row.id, "w1:p1", "claude")
        observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.UncertainWorker)
        self.assertEqual(self.world.commands, [])

    def test_labelled_shell_is_stopped_and_an_absent_pane_is_closed(self) -> None:
        stopped = self.worker("stopped")
        closed = self.worker("closed")
        self.world.add(stopped, "w1:p1", live=False)
        found = {item.row.id: item for item in worker_panes.observe(self.parent.id, staff.snapshot())}
        self.assertIsInstance(found[stopped.id], worker_panes.StoppedWorker)
        self.assertIsInstance(found[closed.id], worker_panes.ClosedWorker)
        self.assertEqual(self.world.commands, [])

    def test_duplicate_native_identity_leaves_the_worker_uncertain(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1")
        self.world.add(row, "w2:p9", workspace="w2", tab="w2:t2")
        observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.UncertainWorker)
        self.assertEqual(self.world.closed, [])

    def test_missing_terminal_never_opens_a_replacement(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1")
        self.world.locations["w1:p1"]["terminal_id"] = ""
        result = worker_panes.start_or_resume(self.parent.id, staff.WorkerName.parse(row.name), "impl")
        self.assertIsInstance(result, worker_panes.UncertainWorker)
        self.assertEqual((self.world.launches, self.world.stops, self.world.closed), ([], [], []))

    def test_unknown_process_evidence_keeps_its_location_and_one_named_note(self) -> None:
        row = self.worker()
        self.world.add(row, "w1:p1")
        self.world.unknown["w1:p1"] = "process permission denied"
        with redirect_stderr(io.StringIO()) as errors:
            observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.ProcessUnknownWorker)
        assert isinstance(observed, worker_panes.ProcessUnknownWorker)
        self.assertEqual((observed.pane, observed.terminal), ("w1:p1", "terminal-w1:p1"))
        self.assertIn("process permission denied", observed.reason)
        notes = errors.getvalue().splitlines()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("w1:p1", notes[0])

    def test_unknown_foreign_pane_does_not_hide_a_proved_worker(self) -> None:
        known = self.worker("known")
        absent = self.worker("absent")
        self.world.add(known, "w1:p1")
        self.world.add(self.parent, "w2:p9", live=False, label="foreign", workspace="w2", tab="w2:t2")
        self.world.unknown["w2:p9"] = "foreign process unreadable"
        found = {item.row.id: item for item in worker_panes.observe(self.parent.id, staff.snapshot())}
        self.assertIsInstance(found[known.id], worker_panes.OpenWorker)
        self.assertIsInstance(found[absent.id], worker_panes.UncertainWorker)
        self.assertEqual(self.world.commands, [])

    def test_unreadable_pane_inventory_never_proves_absence(self) -> None:
        row = self.worker()
        self.world.unreadable = True
        self.assertIsInstance(self.observation(row), worker_panes.UncertainWorker)

    def test_a_second_parent_worker_is_not_a_member_despite_its_label(self) -> None:
        row = self.worker()
        showrunner = fixture_member("showrunner", name="other-showrunner")
        other = fixture_member("unit", name="other", parent=showrunner.id)
        foreign = fixture_worker(row.name, other.id)
        self.world.add(foreign, "w1:p1")
        observed = self.observation(row)
        self.assertIsInstance(observed, worker_panes.UncertainWorker)
        self.assertEqual(self.world.commands, [])

    def test_codex_launch_thread_without_a_current_rollout_is_unknown(self) -> None:
        row = self.worker(family="codex")
        self.world.add(row, "w1:p1", family="codex")
        pid = self.world.foreground["w1:p1"][0]
        self.world.processes[pid] = herdr.NativeAgentProcess(herdr.LaunchArgument(row.current_session), "generation", {})
        result = worker_panes.start_or_resume(self.parent.id, staff.WorkerName.parse(row.name), "impl")
        self.assertIsInstance(result, worker_panes.ProcessUnknownWorker)
        self.assertEqual((self.world.launches, self.world.closed), ([], []))
