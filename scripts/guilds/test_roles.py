"""Exercise durable design identity with local stores and a recording mesh transport."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
from scripts.astra import astra_roles  # noqa: E402
from scripts.accounts import role_map  # noqa: E402
from scripts.agents import codex_mesh  # noqa: E402
from scripts.guilds import astra_work, daily_read, roles, tips  # noqa: E402
from scripts.guilds.roles import resolve_holder as resolve_current_holder  # noqa: E402
from scripts.guilds.roles import release_servers as release_design_servers  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import production_tree  # noqa: E402
from scripts.whoami import agent_accounts  # noqa: E402

Role = Literal["designer", "reviewer"]


class DurableRolesTests(unittest.TestCase):
    root: Path = Path()
    repository: Path = Path()
    run_a: Path = Path()
    run_b: Path = Path()
    launches: list[tuple[str, str]] = []
    dispatches: list[tuple[str, str, str]] = []
    releases: list[str] = []
    holder_session: str = "holder-one"
    lose_creation_ack: bool = False
    fail_dispatch: bool = False

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repository = self.root / "checkout"
        self.repository.mkdir()
        _ = (self.repository / "design.md").write_text("# Design\n")
        self.launches, self.dispatches, self.releases = [], [], []
        self.holder_session, self.lose_creation_ack, self.fail_dispatch = "holder-one", False, False
        config = self.root / "agents.conf"
        _ = config.write_text("[assignments]\nguild=codex\n[guild.codex]\n" +
                              "designer=gpt-fixture:xhigh\nreviewer=gpt-review-fixture:high\n" +
                              "[codex.agents]\ngpt-fixture=xhigh\ngpt-review-fixture=high\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "GUILD_ROLES_STATE": str(self.root / "roles"),
            "GUILD_OWNERSHIP_STATE": str(self.root / "ownership"), "GUILD_ROLES_HOST": "natedev",
            "ASTRA_ROLES_STATE": str(self.root / "holds"), "CLAUDE_CODE_SESSION_ID": self.holder_session,
            "AGENTS_CONFIG_FILE": str(config), "GUILD_TIPS_REPO": str(self.root / "unused-tips"),
        })))
        _ = self.enterContext(patch.object(roles, "authority_request", side_effect=self.authority))
        _ = self.enterContext(patch.object(roles, "resolve_holder", side_effect=self.reach))
        _ = self.enterContext(patch.object(roles, "launch_thread", side_effect=self.launch))
        _ = self.enterContext(patch.object(roles, "dispatch_turn", side_effect=self.dispatch))
        _ = self.enterContext(patch.object(roles, "release_servers", side_effect=self.release))
        _ = self.enterContext(patch.object(role_map, "read_role_map", return_value=role_map.RoleMap({}, "codex1")))
        _ = self.enterContext(patch.object(agent_accounts, "codex_account_home", return_value=
            agent_accounts.CodexHomeHoldingLogin("codex1", self.root / "codex-home", "fixture@example.invalid")))
        def fixture_tips(guild: str) -> str:
            return "Fixture tips: " + guild

        _ = self.enterContext(patch.object(tips, "load", side_effect=fixture_tips))
        roles.register_repository("fixture-repo", {"natedev": str(self.repository),
                                                    "macbook": str(self.root / "mac-checkout")},
                                  inventory_complete=True)
        self.run_a = self.phase("run-a", "instance-a")
        self.run_b = self.phase("run-b", "instance-b")

    def authority(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        return roles._authority_local(operation, payload)  # pyright: ignore[reportPrivateUsage]

    def reach(self, holder: roles.Holder) -> roles.HolderReach:
        del holder
        return roles.HolderReach(self.holder_session)

    def phase(self, name: str, instance: str) -> Path:
        directory = self.root / name
        phase = directory / "phases/1"
        phase.mkdir(parents=True)
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "1"}))
        _ = (phase / "files").write_text("design.md\n")
        _ = (directory / "progress_history_state.json").write_text(json.dumps({
            "run_id": name, "history_file": str(self.root / (name + ".jsonl")),
            "phase": {"id": "1", "instance_id": instance, "status": "open"},
            "open_phases": [{"id": "1", "instance_id": instance, "status": "open"}],
        }))
        return directory

    def binding(self, directory: Path) -> roles.DesignBoundToPhase:
        return roles.DesignBoundToPhase(str(directory), "1", str(self.repository), ("design.md",))

    def instance(self, design: roles.HeldDesign, role: Role) -> roles.RoleInstance:
        return next(instance for instance in design.roles if instance.role == role)

    def launch(self, design: roles.HeldDesign, role: Role, prompt: Path, model: str, effort: str) -> str:
        self.assertTrue(prompt.is_file())
        self.assertEqual((model, effort), ("gpt-fixture", "xhigh") if role == "designer"
                         else ("gpt-review-fixture", "high"))
        thread = "thread-" + role
        mesh = roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True, exist_ok=True)
        path = mesh / "mesh_roster.json"
        roster = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
        instance = self.instance(design, role)
        roster[instance.name] = {"thread_id": thread, "status": "done", "role": "astra-" + role,
                                 "held_design_id": design.placement.design_id, "held_role": role,
                                 "cwd": design.repository, "launcher_pid": 0}
        _ = path.write_text(json.dumps(roster))
        _ = (mesh / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.root / "codex-home")}))
        self.launches.append((role, thread))
        if self.lose_creation_ack:
            raise TimeoutError("creation acknowledgement lost")
        return thread

    def dispatch(self, design: roles.HeldDesign, role: Role, prompt: Path, reply: Path,
                 model: str, effort: str) -> None:
        del model, effort
        lifecycle = self.instance(design, role).lifecycle
        self.assertIsInstance(lifecycle, (roles.RoleOpen, roles.RolePausedForMove))
        thread = cast(roles.RoleOpen, lifecycle).thread_id
        self.dispatches.append((role, thread, prompt.read_text()))
        if self.fail_dispatch:
            raise roles.RoleRefused("turn acceptance unknown")
        _ = reply.write_text("Saved reply from " + thread)

    def release(self, design: roles.HeldDesign) -> None:
        self.releases.append(design.placement.design_id)

    def open_designer(self, *, document: str = "design.md", request_id: str = "open-one") -> roles.HeldDesign:
        return roles.open_role("fixture-repo", document, "designer", roles.UnitHolder("demo", "design-unit"),
                          "design", binding=self.binding(self.run_a), request_id=request_id)

    def resume(self, design: roles.HeldDesign, role: Role, text: str = "Revise with cited evidence.") -> Path:
        prompt = self.root / "assignment.md"
        _ = prompt.write_text(text)
        return roles.resume(design.placement.design_id + "/" + role, prompt)

    def test_open_retry_and_normalized_document_alias_keep_one_pair_and_thread(self) -> None:
        design = self.open_designer()
        retried = self.open_designer(document=str(self.repository / "sub/../design.md"), request_id="retry")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(self.launches, [("designer", "thread-designer")])
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_registered_document_alias_reuses_the_original_pair(self) -> None:
        design = self.open_designer()
        _ = self.authority("alias", {"repository_id": "fixture-repo", "host": "natedev",
                                     "document": "design.md", "alias": "renamed/design.md"})
        retried = self.open_designer(document="renamed/design.md")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unknown_thread_creation_retains_the_claim_instead_of_launching_again(self) -> None:
        with patch.object(roles, "launch_thread", side_effect=ConnectionError("creation outcome unknown")) as launch:
            with self.assertRaises(ConnectionError):
                _ = self.open_designer()
            retry = self.open_designer(request_id="another-attempt")
        self.assertEqual(launch.call_count, 1)
        lifecycle = self.instance(retry, "designer").lifecycle
        self.assertIsInstance(lifecycle, roles.RoleNotOpened)
        if isinstance(lifecycle, roles.RoleNotOpened):
            self.assertTrue(lifecycle.claim)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_incomplete_inventory_and_inventoried_legacy_source_refuse_new_threads(self) -> None:
        roles.register_repository("unresolved-repo", {"natedev": str(self.repository)}, inventory_complete=False)
        with self.assertRaisesRegex(roles.RoleRefused, "inventory"):
            _ = roles.open_role("unresolved-repo", "design.md", "reviewer", roles.ShowrunnerHolder("demo"), "design")
        _ = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                       "host": "natedev", "request_id": "inventory"})
        _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": "design.md", "host": "natedev",
                                      "source": {"mesh": str(self.root / "old-run"), "name": "legacy-designer", "thread_id": "legacy-thread"}})
        with self.assertRaisesRegex(roles.RoleRefused, "AdoptionRequired.*legacy-thread"):
            _ = self.open_designer()
        self.assertEqual(self.launches, [])
        self.assertEqual(list((self.root / "roles").glob("*/design.json")), [])

    def test_simultaneous_hosts_reserve_only_one_document_placement(self) -> None:
        def reserve(host: str) -> str:
            try:
                placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": "race.md",
                                                       "host": host, "request_id": host})
                return str(placement["design_id"])
            except roles.RoleRefused:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("natedev", "macbook")))
        self.assertEqual(results.count("refused"), 1)
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(self.launches, [])

    def test_lost_creation_acknowledgement_never_launches_a_second_thread(self) -> None:
        self.lose_creation_ack = True
        with self.assertRaises(TimeoutError):
            _ = self.open_designer()
        retried = self.open_designer()
        lifecycle = self.instance(retried, "designer").lifecycle
        self.assertIsInstance(lifecycle, (roles.RoleNotOpened, roles.RoleNeedsRecovery))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unreachable_authority_and_host_mismatch_cannot_create_roles(self) -> None:
        with patch.object(roles, "authority_request", side_effect=roles.RoleRefused("authority unavailable")):
            with self.assertRaisesRegex(roles.RoleRefused, "authority unavailable"):
                _ = self.open_designer()
        with self.assertRaisesRegex(roles.RoleRefused, "host"):
            _ = roles.open_role("fixture-repo", "design.md", "reviewer", roles.ShowrunnerHolder("demo"),
                           "design", host="macbook")
        self.assertEqual(self.launches, [])
        self.assertEqual(list((self.root / "roles").glob("*/design.json")), [])

    def test_invalid_model_or_missing_tip_baseline_refuses_before_thread_creation(self) -> None:
        for failing_call in (roles.RoleRefused("invalid guild model"), tips.BaselineMissing("restore the guild tips baseline before starting")):
            with self.subTest(reason=str(failing_call)):
                patched_name = "resolve_model" if isinstance(failing_call, roles.RoleRefused) else "load"
                target = roles if patched_name == "resolve_model" else tips
                with patch.object(target, patched_name, side_effect=failing_call), self.assertRaises(ValueError):
                    _ = self.open_designer()
        self.assertEqual(self.launches, [])

    def test_missing_or_disallowed_configured_model_never_uses_a_fallback(self) -> None:
        config = Path(os.environ["AGENTS_CONFIG_FILE"])
        for row in ("", "designer=unlisted-model:xhigh", "designer=gpt-fixture:invalid-effort"):
            with self.subTest(row=row):
                _ = config.write_text("[assignments]\nguild=codex\n[guild.codex]\n" + row +
                                      "\n[codex.agents]\ngpt-fixture=xhigh\n")
                with self.assertRaisesRegex(roles.RoleRefused, "model configuration"):
                    _ = self.open_designer()
        self.assertEqual(self.launches, [])

    def test_showrunner_cannot_open_unbound_writer_but_reviewer_is_read_only(self) -> None:
        with self.assertRaisesRegex(roles.RoleRefused, "binding"):
            _ = roles.open_role("fixture-repo", "design.md", "designer", roles.ShowrunnerHolder("demo"), "design")
        design = roles.open_role("fixture-repo", "design.md", "reviewer", roles.ShowrunnerHolder("demo"), "design")
        self.assertIsInstance(roles.access_policy(design, "reviewer"), roles.ReadOnlyReviewer)
        self.assertEqual(self.launches, [("reviewer", "thread-reviewer")])

    def test_prompts_and_replies_are_saved_and_read_as_daily_assignments(self) -> None:
        design = self.open_designer()
        reply = self.resume(design, "designer")
        prompts = sorted(roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
        self.assertEqual(len(prompts), 2)
        header = astra_work.read_header(prompts[-1])
        self.assertIsInstance(header, astra_work.AstraWork)
        if not isinstance(header, astra_work.AstraWork):
            self.fail("saved assignment has no work header")
        self.assertEqual((header.member, header.role, header.reply_path),
                         (self.instance(design, "designer").name, "astra-designer", str(reply)))
        self.assertEqual(header.work, astra_work.phase_work(self.run_a, "1"))
        self.assertEqual(prompts[-1].read_text().count("Your guild is `designers`."), 1)
        now = datetime.now(UTC)
        sources = daily_read.LocalSources((), self.root / "missing-history", self.root / "astra",
                                         self.root / "roles", "fixture-host")
        report = daily_read.read_daily("designers", now - timedelta(hours=1), now + timedelta(seconds=1), sources)
        assignments = cast(list[dict[str, object]], report["assignments"])
        self.assertTrue(any(row["member"] == header.member and row["role"] == header.role
                            and row["outcome"] == "OutputRecorded" for row in assignments))

    def test_rebind_to_an_existing_second_run_keeps_thread_and_changes_next_header(self) -> None:
        design = self.open_designer()
        first_reply = self.resume(design, "designer")
        previous = first_reply.read_bytes()
        rebound = roles.rebind(design.placement.design_id, expected_generation=1, reason="next phase",
                               binding=self.binding(self.run_b))
        next_reply = self.resume(rebound, "designer")
        self.assertTrue(self.run_a.is_dir())
        self.assertEqual(first_reply.read_bytes(), previous)
        self.assertNotEqual(next_reply, first_reply)
        self.assertEqual(self.launches, [("designer", "thread-designer")])
        self.assertEqual([entry[1] for entry in self.dispatches], ["thread-designer", "thread-designer"])
        header = astra_work.read_header(sorted(roles.design_directory(design.placement.design_id).glob("prompts/*.md"))[-1])
        self.assertIsInstance(header, astra_work.AstraWork)
        if isinstance(header, astra_work.AstraWork):
            self.assertEqual(header.work, astra_work.phase_work(self.run_b, "1"))
        self.assertEqual(len(self.releases), 1)

    def test_pending_messages_clear_only_after_successful_turn_acceptance(self) -> None:
        design = self.open_designer()
        message = roles.queue_message(design.placement.design_id, "designer", "guild-head", "Keep evidence.")
        _ = roles.queue_message(design.placement.design_id, "designer", "guild-head", "Keep evidence.", message_id=message.id)
        self.assertEqual(roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.fail_dispatch = True
        with self.assertRaises(roles.RoleRefused):
            _ = self.resume(design, "designer")
        self.assertEqual(roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.assertIn(message.id, self.dispatches[-1][2])
        lifecycle = self.instance(roles.read_design(design.placement.design_id), "designer").lifecycle
        self.assertIsInstance(lifecycle, roles.RoleNeedsRecovery)

    def test_successful_assignment_consumes_its_pending_ids_without_waking_on_send(self) -> None:
        design = self.open_designer()
        message = roles.queue_message(design.placement.design_id, "designer", "peer", "Cite the file.")
        self.assertEqual(self.dispatches, [])
        _ = self.resume(design, "designer")
        self.assertIn(message.id, self.dispatches[0][2])
        self.assertEqual(roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_message_queued_during_a_turn_is_kept_for_the_next_assignment(self) -> None:
        design = self.open_designer()
        first = roles.queue_message(design.placement.design_id, "designer", "peer", "First evidence.")
        during_turn: list[roles.PendingMessage] = []

        def dispatch_with_message(current: roles.HeldDesign, role: Role, prompt: Path,
                                  reply: Path, model: str, effort: str) -> None:
            during_turn.append(roles.queue_message(design.placement.design_id, role, "peer", "Later evidence."))
            self.dispatch(current, role, prompt, reply, model, effort)

        with patch.object(roles, "dispatch_turn", side_effect=dispatch_with_message):
            _ = self.resume(design, "designer")
        self.assertEqual(roles.pending_messages(design.placement.design_id, "designer"), tuple(during_turn))
        self.assertIn(first.id, self.dispatches[-1][2])
        self.assertNotIn(during_turn[0].id, self.dispatches[-1][2])
        _ = self.resume(design, "designer")
        self.assertIn(during_turn[0].id, self.dispatches[-1][2])
        self.assertEqual(roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_holder_transfer_fences_the_old_session_and_keeps_the_thread(self) -> None:
        design = self.open_designer()
        destination = roles.UnitHolder("demo", "next-unit")

        def resolve(holder: roles.Holder) -> roles.HolderReach:
            return roles.HolderReach("next-holder" if holder == destination else "holder-one")

        with patch.object(roles, "resolve_holder", side_effect=resolve):
            transferred = roles.rebind(design.placement.design_id, expected_generation=1,
                                       reason="holder transfer", holder=destination)
            with self.assertRaisesRegex(roles.RoleRefused, "current holder"):
                _ = self.resume(transferred, "designer")
            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "next-holder"}):
                _ = self.resume(transferred, "designer")
                records = astra_roles.live_roles()
        self.assertEqual(transferred.holder, destination)
        self.assertEqual(transferred.placement.generation, 2)
        self.assertEqual([(record["thread_id"], record["manager"]) for record in records],
                         [("thread-designer", "next-holder")])
        self.assertEqual(self.launches, [("designer", "thread-designer")])
        self.assertEqual([entry[1] for entry in self.dispatches], ["thread-designer"])

    def test_holder_restart_repairs_a_missing_hold_without_replacing_the_thread(self) -> None:
        design = self.open_designer()
        (self.root / "holds/thread-designer.json").unlink()
        self.holder_session = "holder-two"
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.holder_session
        _ = self.resume(design, "designer")
        records = astra_roles.live_roles()
        self.assertEqual([(record["thread_id"], record["manager"]) for record in records],
                         [("thread-designer", "holder-two")])
        self.assertEqual(len(self.launches), 1)

    def test_durable_hold_survives_run_removal_and_keeps_one_census_edge(self) -> None:
        design = self.open_designer()
        shutil.rmtree(self.run_a)
        records = astra_roles.live_roles()
        self.assertEqual([(record["thread_id"], record["state"]) for record in records],
                         [("thread-designer", "idle")])
        self.assertIsInstance(records[0]["lifetime"], astra_roles.HeldDesignRole)
        edges = production_tree.astra_edges()
        self.assertEqual([(edge["manager"], edge["member"], edge["role"], edge["codex_home"], edge["state"]) for edge in edges],
                         [("holder-one", "thread-designer", "astra-designer", str(self.root / "codex-home"), "idle")])
        self.assertEqual(roles.read_design(design.placement.design_id).placement.design_id, design.placement.design_id)

    def test_server_refresh_uses_current_run_holder_and_generation_on_the_same_account(self) -> None:
        design = self.open_designer()
        mesh = roles.mesh_directory(design.placement.design_id)
        old_context = cast(dict[str, object], json.loads((mesh / "guild_context.json").read_text()))
        server = mesh / "mesh_server.json"
        home = self.root / "codex-home"
        _ = server.write_text(json.dumps({"port": 32100, "pid": os.getpid(), "codex_home": str(home),
                                         "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
                                         "guild_context": old_context}))
        rebound = roles.rebind(design.placement.design_id, expected_generation=1, reason="run change",
                               binding=self.binding(self.run_b))
        operations: list[str] = []
        environments: list[dict[str, str]] = []

        def release_server(folder: str) -> None:
            self.assertEqual(folder, str(mesh))
            operations.append("release")
            server.unlink()

        def start_server(folder: str, selected_home: Path) -> int:
            operations.append("start")
            environments.append(codex_mesh._server_environment(folder, selected_home))  # pyright: ignore[reportPrivateUsage]
            return 32101

        with patch.object(codex_mesh, "_release_held_server", side_effect=release_server), \
                patch.object(codex_mesh, "_start_server", side_effect=start_server), \
                patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_a),
                                        "PLAN_DELEGATE_PHASE": "stale-phase"}):
            self.assertEqual(codex_mesh.ensure_server(str(mesh), home), (32101, True))
        self.assertEqual(operations, ["release", "start"])
        self.assertEqual(environments[0]["UNIT_RUN_DIR"], str(self.run_b))
        self.assertEqual(environments[0]["GUILD_BINDING_GENERATION"], str(rebound.placement.generation))
        self.assertNotIn("PLAN_DELEGATE_PHASE", environments[0])
        self.holder_session = "holder-restarted"
        context = codex_mesh._server_environment(str(mesh), home)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(context["CLAUDE_CODE_SESSION_ID"], "holder-restarted")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "holder-restarted"}):
            unbound = roles.rebind(design.placement.design_id, expected_generation=2, reason="run end",
                                   binding=roles.DesignUnbound())
        with patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_a), "PLAN_DELEGATE_BOARD_DIR": str(self.run_a),
                                     "UNIT_PHASE": "old"}):
            context = codex_mesh._server_environment(str(mesh), home)  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(key.startswith(("UNIT_", "PLAN_DELEGATE_")) for key in context))
        self.assertEqual(context["GUILD_BINDING_GENERATION"], str(unbound.placement.generation))

    def test_stale_rebind_and_busy_rebind_preserve_the_current_binding(self) -> None:
        design = self.open_designer()
        with self.assertRaisesRegex(roles.RoleRefused, "generation"):
            _ = roles.rebind(design.placement.design_id, expected_generation=2, reason="stale",
                             binding=self.binding(self.run_b))
        roster = roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        entries = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
        entries[self.instance(design, "designer").name]["status"] = "running"
        _ = roster.write_text(json.dumps(entries))
        with self.assertRaisesRegex(roles.RoleRefused, "busy"):
            _ = roles.rebind(design.placement.design_id, expected_generation=1, reason="busy",
                             binding=self.binding(self.run_b))
        self.assertEqual(roles.read_design(design.placement.design_id).binding, design.binding)
        self.assertEqual(self.releases, [])

    def list_output(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(roles.main(["list", *arguments]), 0)
        return output.getvalue()

    def test_list_prints_each_role_with_its_holder_session_thread_and_document(self) -> None:
        design = self.open_designer()
        lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2, lines)
        for role in ("designer", "reviewer"):
            instance_id = design.placement.design_id + "/" + role
            line = next(line for line in lines if instance_id in line)
            self.assertIn("design-unit (demo)", line)
            self.assertIn("holder-one", line)
            self.assertIn(design.document, line)
            self.assertNotRegex(line, r"[{}]")
            if role == "designer":
                self.assertIn("RoleOpen", line)
                self.assertIn("thread-designer", line)
            else:
                self.assertIn("RoleNotOpened", line)
                self.assertNotIn("thread", line)

    def test_list_names_showrunner_and_unavailable_holder_reason(self) -> None:
        design = roles.open_role("fixture-repo", "design.md", "reviewer", roles.ShowrunnerHolder("demo"), "design")
        with patch.object(roles, "resolve_holder", return_value=roles.HolderUnavailable("holder session is absent")):
            lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(design.placement.design_id, line)
            self.assertIn("demo showrunner", line)
            self.assertIn("holder session is absent", line)
            self.assertNotIn("holder-one", line)
            self.assertIn(design.document, line)
        reviewer = next(line for line in lines if design.placement.design_id + "/reviewer" in line)
        self.assertIn("thread-reviewer", reviewer)

    def test_list_prints_empty_inventory_and_record_problems(self) -> None:
        self.assertEqual(self.list_output(), "no held roles\n")
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir(parents=True)
        _ = bad.write_text('{"schema": 999}')
        lines = self.list_output().splitlines()
        self.assertEqual(lines[0], "no held roles")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith(f"problem: {bad}: "), lines)
        self.assertGreater(len(lines[1]), len(f"problem: {bad}: "))

    def test_list_json_preserves_full_inventory_and_problems(self) -> None:
        _ = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        report = cast(dict[str, object], json.loads(self.list_output("--json")))
        inventory = cast(object, json.loads(json.dumps(roles.list_roles())))
        self.assertEqual(report, inventory)
        entries = cast(list[dict[str, object]], report["roles"])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["holder_reach"], {"kind": "HolderReach", "session_id": "holder-one"})
        self.assertIn(str(bad), str(report["problems"]))

    def test_inventory_lists_bad_records_without_network_and_keeps_valid_roles(self) -> None:
        design = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        with patch.object(roles, "authority_request", side_effect=AssertionError("inventory called authority")), \
                patch.object(subprocess, "run", side_effect=AssertionError("inventory ran a command")):
            inventory = roles.list_roles()
        entries = cast(list[dict[str, object]], inventory["roles"])
        self.assertIn(design.placement.design_id, [entry["design_id"] for entry in entries])
        self.assertIn(str(bad), str(inventory["problems"]))

    def test_inventory_reports_paused_and_failed_threads_with_the_holder_route(self) -> None:
        design = self.open_designer()
        mesh = roles.mesh_directory(design.placement.design_id)
        name = self.instance(design, "designer").name
        marker = mesh / (name + ".account-move.json")
        _ = marker.write_text("{}")
        paused = cast(list[dict[str, object]], roles.list_roles()["roles"])
        designer = next(row for row in paused if row["role"] == "designer")
        self.assertEqual(designer["lifecycle"], {"kind": "RolePausedForMove", "thread_id": "thread-designer"})
        self.assertEqual(designer["holder_reach"], {"kind": "HolderReach", "session_id": "holder-one"})
        marker.unlink()
        roster = cast(dict[str, dict[str, object]], json.loads((mesh / "mesh_roster.json").read_text()))
        roster[name]["status"] = "failed"
        _ = (mesh / "mesh_roster.json").write_text(json.dumps(roster))
        failed = cast(list[dict[str, object]], roles.list_roles()["roles"])
        designer = next(row for row in failed if row["role"] == "designer")
        self.assertEqual(cast(dict[str, object], designer["lifecycle"])["kind"], "RoleNeedsRecovery")
        self.assertEqual(cast(dict[str, object], designer["lifecycle"])["thread_id"], "thread-designer")
        self.assertEqual(self.dispatches, [])

    def test_patch_checks_use_the_current_run_and_refuse_stale_or_mixed_paths(self) -> None:
        design = self.open_designer()
        rebound = roles.rebind(design.placement.design_id, expected_generation=1, reason="next run",
                               binding=self.binding(self.run_b))
        self.assertTrue(self.run_a.is_dir())
        accepted = roles.check_patch(design.placement.design_id, 2, self.repository, ["./design.md"])
        self.assertEqual(accepted, rebound.binding)
        for generation, paths in ((1, ["design.md"]), (2, ["design.md", "unowned.md"]),
                                  (2, ["design.md", "renamed.md"])):
            with self.subTest(generation=generation, paths=paths), self.assertRaises(roles.RoleRefused):
                _ = roles.check_patch(design.placement.design_id, generation, self.repository, paths)
        _ = (self.run_b / "phases/1/delegated_phase_reservation_state.json").write_bytes(b"\xff")
        with self.assertRaises((ValueError, OSError)):
            _ = roles.check_patch(design.placement.design_id, 2, self.repository, ["design.md"])

    def test_a_binding_to_an_ad_hoc_phase_open_beside_another_accepts_its_patch(self) -> None:
        phase = self.run_a / "phases/adhoc"
        phase.mkdir()
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "ad hoc"}))
        _ = (phase / "files").write_text("design.md\n")
        state = self.run_a / "progress_history_state.json"
        recorded = cast(dict[str, object], json.loads(state.read_text()))
        ad_hoc = {"id": "ad hoc", "instance_id": "instance-ad-hoc", "status": "active"}
        recorded["open_phases"] = [*cast(list[object], recorded["open_phases"]), ad_hoc]
        _ = state.write_text(json.dumps(recorded))
        binding = roles.DesignBoundToPhase(str(self.run_a), "ad hoc", str(self.repository), ("design.md",))
        design = roles.open_role("fixture-repo", "design.md", "designer", roles.UnitHolder("demo", "design-unit"),
                                 "design", binding=binding, request_id="open-ad-hoc")
        self.assertEqual(roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"]), binding)

    def test_patch_cannot_follow_a_symlink_outside_the_reserved_repository(self) -> None:
        design = self.open_designer()
        outside = self.root / "outside.md"
        _ = outside.write_text("Private evidence")
        (self.repository / "design.md").unlink()
        (self.repository / "design.md").symlink_to(outside)
        with self.assertRaises(roles.RoleRefused):
            _ = roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"])
        self.assertEqual(outside.read_text(), "Private evidence")

    def test_unbind_keeps_evidence_and_threads_but_refuses_the_next_writer_turn(self) -> None:
        design = self.open_designer()
        reply = self.resume(design, "designer")
        saved = {path: path.read_bytes() for path in roles.design_directory(design.placement.design_id).glob("prompts/*.md")}
        roles.unbind_run(str(self.run_a))
        unbound = roles.read_design(design.placement.design_id)
        self.assertIsInstance(unbound.binding, roles.DesignUnbound)
        self.assertEqual(self.instance(unbound, "designer").lifecycle, roles.RoleOpen("thread-designer"))
        with self.assertRaises(roles.RoleRefused):
            _ = self.resume(unbound, "designer")
        for path, contents in saved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(reply.is_file())
        self.assertEqual(len(self.launches), 1)

    def test_end_is_terminal_and_keeps_saved_prompts(self) -> None:
        design = self.open_designer()
        folder = roles.design_directory(design.placement.design_id)
        saved = {path: path.read_bytes() for path in folder.glob("prompts/*.md")}
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(codex_mesh, "held_thread_end_confirmed", return_value=True):
            ended = roles.end(design.placement.design_id + "/designer")
            retried = roles.end(design.placement.design_id + "/designer")
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, roles.RoleEnded)
        self.assertEqual(retried, ended)
        with self.assertRaisesRegex(roles.RoleRefused, "role ended; this design identity cannot be reopened"):
            _ = self.open_designer()
        self.assertEqual(len(self.launches), 1)
        self.assertFalse((self.root / "holds/thread-designer.json").exists())
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_unit_rename_retry_keeps_both_original_threads_and_updates_live_paths(self) -> None:
        design = self.open_designer()
        design = roles.open_role("fixture-repo", "design.md", "reviewer", design.holder, "design",
                            binding=self.binding(self.run_a))
        initial_names = tuple(instance.name for instance in design.roles)
        initial_prompts = {path: path.read_bytes() for path in roles.design_directory(design.placement.design_id).glob("prompts/*.md")}
        destination = self.root / "renamed-checkout"
        _ = self.repository.rename(destination)
        for _attempt in range(2):
            _ = roles.rename_holder("demo", "design-unit", "renamed-unit", str(self.repository), str(destination))
        current = roles.read_design(design.placement.design_id)
        self.assertEqual(current.holder, roles.UnitHolder("demo", "renamed-unit"))
        self.assertEqual(current.repository, str(destination))
        self.assertEqual(current.document, str(destination / "design.md"))
        self.assertEqual(current.placement.generation, 2)
        self.assertEqual(tuple(instance.name for instance in current.roles), initial_names)
        self.assertEqual(roles.rename_failures("demo", "design-unit", str(self.repository)), [])
        self.assertEqual({path: path.read_bytes() for path in initial_prompts}, initial_prompts)
        for kind in ("designer", "reviewer"):
            _ = self.resume(current, kind)
        self.assertEqual([(role, thread) for role, thread, _text in self.dispatches],
                         [("designer", "thread-designer"), ("reviewer", "thread-reviewer")])
        self.assertEqual(len(self.launches), 2)

    def check_holder_move(self, old: roles.Holder, new: roles.Holder) -> None:
        """The held role resumes under `new` only after the move, in the same thread, with its paths kept."""
        design = roles.open_role("fixture-repo", "design.md", "designer", old, "design",
                                 binding=self.binding(self.run_a))

        def resolve(holder: roles.Holder) -> roles.HolderReach | roles.HolderUnavailable:
            return roles.HolderReach("holder-one") if holder == new else roles.HolderUnavailable("not marked")

        with patch.object(roles, "resolve_holder", side_effect=resolve):
            with self.assertRaisesRegex(roles.RoleRefused, "not marked"):
                _ = self.resume(design, "designer")
            self.assertTrue(roles.holder_failures(old))
            for _attempt in range(2):
                roles.move_holder(old, new, reason="unit transfer")
            current = roles.read_design(design.placement.design_id)
            _ = self.resume(current, "designer")
        self.assertEqual(current.holder, new)
        self.assertEqual((current.repository, current.document, current.binding),
                         (design.repository, design.document, design.binding))
        self.assertEqual(current.placement.generation, 2)
        self.assertEqual(current.roles[0].name, design.roles[0].name)
        roster = cast(dict[str, dict[str, object]], json.loads(
            (roles.mesh_directory(design.placement.design_id) / "mesh_roster.json").read_text()))
        self.assertEqual(roster[design.roles[0].name]["cwd"], design.repository)
        self.assertEqual(roles.holder_failures(old), [])
        self.assertEqual(self.dispatches[-1][1], "thread-designer")
        self.assertEqual(self.launches, [("designer", "thread-designer")])

    def test_a_unit_transfer_moves_the_holder_to_the_receiving_production(self) -> None:
        self.check_holder_move(roles.UnitHolder("demo", "design-unit"), roles.UnitHolder("other", "design-unit"))

    def test_a_production_rename_moves_the_showrunner_holder(self) -> None:
        self.check_holder_move(roles.ShowrunnerHolder("demo"), roles.ShowrunnerHolder("renamed"))

    def test_uncertain_server_release_blocks_rename_before_holder_changes(self) -> None:
        design = self.open_designer()
        with patch.object(roles, "release_servers", side_effect=roles.RoleRefused("release uncertain")):
            with self.assertRaisesRegex(roles.RoleRefused, "uncertain"):
                _ = roles.rename_holder("demo", "design-unit", "renamed-unit", str(self.repository),
                                    str(self.root / "new-checkout"))
        self.assertEqual(roles.read_design(design.placement.design_id).holder, design.holder)
        self.assertTrue(roles.rename_failures("demo", "design-unit", str(self.repository)))

    def test_legacy_adoption_requires_matching_thread_launch_evidence(self) -> None:
        holder = roles.UnitHolder("demo", "design-unit")
        for evidence in ("matching", "missing", "different-model", "different-effort", "other-thread"):
            with self.subTest(evidence=evidence):
                document = evidence + ".md"
                _ = (self.repository / document).write_text("# Design\n")
                source = self.root / ("legacy-" + evidence)
                source.mkdir()
                name, thread = "legacy-" + evidence, "legacy-thread-" + evidence
                _ = (source / "mesh_roster.json").write_text(json.dumps({name: {
                    "thread_id": thread, "status": "paused", "paused_from": "done", "cwd": str(self.repository)}}))
                _ = (source / "mesh_server.json").write_text(json.dumps({"port": 32100, "pid": os.getpid(),
                    "codex_home": str(self.root / "codex-home")}))
                if evidence != "missing":
                    launch_thread = "another-thread" if evidence == "other-thread" else thread
                    model = "gpt-other" if evidence == "different-model" else "gpt-fixture"
                    effort = "low" if evidence == "different-effort" else "xhigh"
                    _ = (source / "impl_worker_design.json").write_text(json.dumps({
                        "name": name, "slot": "design", "family": "codex", "model": model, "worker_id": launch_thread}))
                    phase = source / "phases/3"
                    phase.mkdir(parents=True)
                    _ = (phase / "impl_agent_design").write_text(
                        "task=impl\nfamily=codex\nagent=" + model + "\neffort=" + effort + "\n")
                    _ = (phase / "impl_agent_design.log").write_text(
                        "[2026-10-21T12:00:00Z] mesh: " + name + " thread " + launch_thread + "\n")
                placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": document,
                    "host": "natedev", "request_id": "inventory-" + evidence})
                _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": document,
                    "host": "natedev", "source": {"mesh": str(source), "name": name, "thread_id": thread,
                    "holder": {"kind": "UnitHolder", "production_slug": "demo", "unit_name": "design-unit"}}})
                _ = (self.run_a / "phases/1/files").write_text(document + "\n")
                binding = roles.DesignBoundToPhase(str(self.run_a), "1", str(self.repository), (document,))
                methods: list[str] = []

                def request(method: str, params: object, **options: object) -> dict[str, object]:
                    del params, options
                    methods.append(method)
                    if method == "thread/read":
                        return {"result": {"thread": {"id": thread, "status": {"type": "idle"}, "turns": []}}}
                    if method == "thread/queue/list":
                        return {"result": {"data": []}}
                    if method == "thread/unsubscribe":
                        return {"result": {"status": "notLoaded"}}
                    raise AssertionError("unexpected adoption request: " + method)

                client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
                client.port = 32100
                with patch.object(codex_mesh, "Client", return_value=client), \
                        patch.object(client, "call", side_effect=request), patch.object(roles, "confirm_adoption"):
                    if evidence == "matching":
                        adopted = roles.adopt("fixture-repo", document, "designer", holder, "design",
                            source_mesh=str(source), name=name, thread_id=thread, adoption_id="adopt-" + evidence,
                            binding=binding)
                        destination = roles.mesh_directory(adopted.placement.design_id)
                        roster = cast(dict[str, dict[str, object]], json.loads((destination / "mesh_roster.json").read_text()))
                        self.assertEqual(self.instance(adopted, "designer").lifecycle, roles.RoleOpen(thread))
                        self.assertEqual(adopted.placement.design_id, placement["design_id"])
                        self.assertEqual(roster[name]["thread_id"], thread)
                        self.assertEqual((roster[name]["model"], roster[name]["effort"]), ("gpt-fixture", "xhigh"))
                        self.assertEqual(roster[name]["status"], "done")
                        self.assertNotIn("paused_from", roster[name])
                        self.assertIn("thread/unsubscribe", methods)
                    else:
                        with self.assertRaisesRegex(roles.RoleRefused, "^AdoptionRequired:"):
                            _ = roles.adopt("fixture-repo", document, "designer", holder, "design",
                                source_mesh=str(source), name=name, thread_id=thread, adoption_id="adopt-" + evidence,
                                binding=binding)
                        self.assertNotIn("thread/unsubscribe", methods)
                        self.assertFalse((roles.design_directory(str(placement["design_id"])) / "design.json").exists())
        self.assertEqual(self.launches, [])

    def test_refused_adoption_leaves_the_authority_without_a_transfer_claim(self) -> None:
        holder = roles.UnitHolder("demo", "design-unit")
        for reason in ("unbound-designer", "missing-model-evidence"):
            with self.subTest(reason=reason):
                document = reason + ".md"
                _ = (self.repository / document).write_text("# Design\n")
                source = self.root / reason
                source.mkdir()
                name, thread = reason + "-seat", reason + "-thread"
                entry: dict[str, object] = {"thread_id": thread, "status": "done"}
                if reason == "unbound-designer":
                    entry.update({"model": "gpt-fixture", "effort": "xhigh"})
                roster = source / "mesh_roster.json"
                _ = roster.write_text(json.dumps({name: entry}))
                _ = (source / "mesh_server.json").write_text(json.dumps({"port": 32100, "pid": os.getpid(),
                    "codex_home": str(self.root / "codex-home")}))
                _ = self.authority("reserve", {"repository_id": "fixture-repo", "document": document,
                    "host": "natedev", "request_id": "inventory-" + reason})
                _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": document,
                    "host": "natedev", "source": {"mesh": str(source), "name": name, "thread_id": thread,
                    "holder": {"kind": "UnitHolder", "production_slug": "demo", "unit_name": "design-unit"}}})
                _ = (self.run_a / "phases/1/files").write_text(document + "\n")
                binding = (roles.DesignUnbound() if reason == "unbound-designer" else
                    roles.DesignBoundToPhase(str(self.run_a), "1", str(self.repository), (document,)))
                before = roster.read_bytes()
                client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
                with patch.object(codex_mesh, "Client", return_value=client), \
                        patch.object(client, "call", return_value={"result": {"thread": {"id": thread, "status": {"type": "idle"}}}}), \
                        self.assertRaises(roles.RoleRefused):
                    _ = roles.adopt("fixture-repo", document, "designer", holder, "design", source_mesh=str(source),
                        name=name, thread_id=thread, adoption_id="refused-" + reason, binding=binding)
                authority = self.authority("lookup", {"repository_id": "fixture-repo", "document": document,
                    "host": "natedev"})
                self.assertNotEqual(authority.get("adoption_state"), "AdoptionReserved")
                self.assertNotIn("adoption_id", authority)
                self.assertEqual(roster.read_bytes(), before)
                self.assertFalse((source / "transfer.json").exists())
                with self.assertRaisesRegex(roles.RoleRefused, "^AdoptionRequired:"):
                    _ = roles.open_role("fixture-repo", document, "reviewer", holder, "design")
        self.assertEqual(self.launches, [])

    def test_last_role_end_stops_the_server_and_preserves_design_evidence(self) -> None:
        design = self.open_designer()
        design = roles.open_role("fixture-repo", "design.md", "reviewer", design.holder, "design",
                                 binding=self.binding(self.run_a))
        design_id = design.placement.design_id
        directory, mesh = roles.design_directory(design_id), roles.mesh_directory(design_id)
        _ = self.resume(design, "reviewer")
        message = roles.queue_message(design_id, "reviewer", "fixture", "Keep this evidence after end.")
        evidence = {path: path.read_bytes() for folder in ("prompts", "replies", "pending")
                    for path in (directory / folder).rglob("*") if path.is_file()}
        authority_before = self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                  "host": "natedev"})
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def clean_process() -> None:
            if process.poll() is None:
                process.kill()
            _ = process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()

        self.addCleanup(clean_process)
        server = mesh / "mesh_server.json"
        _ = server.write_text(json.dumps({"pid": process.pid, "port": 32100,
                                         "codex_home": str(self.root / "codex-home")}))
        methods: list[str] = []

        def request(method: str, params: dict[str, object], **options: object) -> dict[str, object]:
            del options
            methods.append(method)
            if method == "thread/queue/list":
                return {"result": {"data": []}}
            if method == "thread/read":
                return {"result": {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}}
            if method == "thread/unsubscribe":
                return {"result": {"status": "notLoaded"}}
            raise AssertionError("unexpected end request: " + method)

        def end_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            self.assertEqual(command[2], "end")
            result = codex_mesh.command_end(argparse.Namespace(session_dir=str(mesh), to=command[-1]))
            return subprocess.CompletedProcess(command, result, "", "")

        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
        with patch.object(subprocess, "run", side_effect=end_command), \
                patch.object(codex_mesh, "Client", return_value=client), patch.object(client, "call", side_effect=request), \
                patch.object(roles, "release_servers", side_effect=release_design_servers), \
                patch.object(codex_mesh, "REAP_WAIT_SECS", 0.05), patch.object(codex_mesh, "REAP_POLL_SECS", 0.001):
            first = roles.end(design_id + "/designer")
            self.assertIsInstance(self.instance(first, "designer").lifecycle, roles.RoleEnded)
            self.assertIsInstance(self.instance(first, "reviewer").lifecycle, roles.RoleOpen)
            self.assertIsNone(process.poll())
            self.assertTrue(server.exists())
            self.assertNotIn("thread/unsubscribe", methods)
            ended = roles.end(design_id + "/reviewer")
            _ = process.wait(timeout=2)
        self.assertTrue(all(isinstance(instance.lifecycle, roles.RoleEnded) for instance in ended.roles))
        self.assertFalse(server.exists())
        self.assertIn("thread/unsubscribe", methods)
        roster = cast(dict[str, dict[str, object]], json.loads((mesh / "mesh_roster.json").read_text()))
        self.assertEqual({entry["thread_id"] for entry in roster.values()}, {"thread-designer", "thread-reviewer"})
        self.assertTrue(all(entry["status"] == "ended" for entry in roster.values()))
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertEqual(roles.pending_messages(design_id, "reviewer"), (message,))
        self.assertEqual(self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                 "host": "natedev"}), authority_before)

    def test_adoption_preserves_thread_alias_and_pending_text_and_is_retryable(self) -> None:
        source = self.root / "old-run"
        source.mkdir()
        name, thread = "original-designer-alias", "original-thread"
        _ = (source / "mesh_roster.json").write_text(json.dumps({name: {
            "thread_id": thread, "status": "done", "cwd": str(self.repository),
            "model": "gpt-fixture", "effort": "xhigh"}}))
        _ = (source / "mesh_server.json").write_text(json.dumps({"port": 32100, "pid": os.getpid(),
                                                                 "codex_home": str(self.root / "codex-home")}))
        holder = roles.UnitHolder("demo", "design-unit")
        placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                               "host": "natedev", "request_id": "inventory"})
        _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": "design.md", "host": "natedev",
            "source": {"mesh": str(source), "name": name, "thread_id": thread,
                       "holder": {"kind": "UnitHolder", "production_slug": "demo", "unit_name": "design-unit"}}})
        queued = [{"id": "queued-one", "input": [{"type": "text", "text": "Keep this queued evidence."}]}]
        methods: list[str] = []
        release_fails = True

        def request(method: str, params: object, **options: object) -> dict[str, object]:
            del params, options
            methods.append(method)
            if method == "thread/queue/list":
                return {"result": {"data": list(queued)}}
            if method == "thread/read":
                return {"result": {"thread": {"id": thread, "status": {"type": "idle"}, "turns": []}}}
            if method == "thread/queue/delete":
                self.assertIn("Keep this queued evidence.", (source / (name + ".pending.json")).read_text())
                queued.clear()
                return {"result": {}}
            if method == "thread/unsubscribe":
                if release_fails:
                    raise ConnectionError("release acknowledgement lost")
                return {"result": {"status": "notLoaded"}}
            raise AssertionError("unexpected mesh method: " + method)

        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
        client.port = 32100
        confirm = MagicMock()
        with patch.object(codex_mesh, "Client", return_value=client), \
                patch.object(client, "call", side_effect=request), \
                patch.object(roles, "confirm_adoption", confirm):
            with self.assertRaisesRegex(roles.RoleRefused, "AdoptionNeedsRecovery"):
                _ = roles.adopt("fixture-repo", "design.md", "designer", roles.UnitHolder("demo", "design-unit"),
                                "design", source_mesh=str(source), name=name, thread_id=thread,
                                adoption_id="transfer-one", binding=self.binding(self.run_a))
            fenced = cast(dict[str, dict[str, object]], json.loads((source / "mesh_roster.json").read_text()))
            self.assertEqual(cast(dict[str, object], fenced[name]["transferring_role"])["adoption_id"], "transfer-one")
            self.assertFalse(list((self.root / "roles").glob("*/design.json")))
            release_fails = False
            adopted = roles.adopt("fixture-repo", "design.md", "designer", roles.UnitHolder("demo", "design-unit"),
                                  "design", source_mesh=str(source), name=name, thread_id=thread,
                                  adoption_id="transfer-one", binding=self.binding(self.run_a))
            before = len(methods)
            retried = roles.adopt("fixture-repo", "design.md", "designer", roles.UnitHolder("demo", "design-unit"),
                                  "design", source_mesh=str(source), name=name, thread_id=thread,
                                  adoption_id="transfer-one", binding=self.binding(self.run_a))
        self.assertEqual(retried.placement, adopted.placement)
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual(len(methods), before)
        self.assertEqual(self.instance(adopted, "designer"), roles.RoleInstance("designer", name, roles.RoleOpen(thread)))
        destination = roles.mesh_directory(adopted.placement.design_id)
        self.assertIn("Keep this queued evidence.", (destination / (name + ".pending.json")).read_text())
        redirected = cast(dict[str, dict[str, object]], json.loads((source / "mesh_roster.json").read_text()))
        self.assertEqual(cast(dict[str, object], redirected[name]["role_redirect"])["design_id"], adopted.placement.design_id)
        self.assertEqual(self.launches, [])
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("thread/fork", methods)
        authority = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                               "host": "natedev", "request_id": "inspect"})
        self.assertEqual(authority["design_id"], placement["design_id"])
        self.assertEqual(authority["adoption_state"], "completed")
        self.assertNotIn("adoption_required", authority)
        counterpart = roles.open_role("fixture-repo", "design.md", "reviewer", holder, "design",
                                      binding=self.binding(self.run_a))
        self.assertEqual(counterpart.placement, adopted.placement)
        self.assertEqual(self.instance(counterpart, "reviewer").lifecycle, roles.RoleOpen("thread-reviewer"))

    def test_adoption_retry_completes_after_the_authority_commit_acknowledgement_is_lost(self) -> None:
        source = self.root / "legacy-mesh"
        source.mkdir()
        name, thread = "original-designer", "original-thread"
        _ = (source / "mesh_roster.json").write_text(json.dumps({name: {
            "thread_id": thread, "status": "done", "cwd": str(self.repository),
            "model": "gpt-fixture", "effort": "xhigh"}}))
        _ = (source / "mesh_server.json").write_text(json.dumps({"port": 32100, "pid": os.getpid(),
                                                                 "codex_home": str(self.root / "codex-home")}))
        holder = roles.UnitHolder("demo", "design-unit")
        placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                               "host": "natedev", "request_id": "inventory"})
        _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": "design.md", "host": "natedev",
            "source": {"mesh": str(source), "name": name, "thread_id": thread,
                       "holder": {"kind": "UnitHolder", "production_slug": "demo", "unit_name": "design-unit"}}})
        lose_commit_ack = True

        def authority(operation: str, payload: dict[str, object]) -> dict[str, object]:
            nonlocal lose_commit_ack
            result = self.authority(operation, payload)
            if operation == "adopt-commit" and lose_commit_ack:
                lose_commit_ack = False
                raise OSError("authority commit acknowledgement lost")
            return result

        def request(method: str, params: object, **options: object) -> dict[str, object]:
            del params, options
            if method == "thread/read":
                return {"result": {"thread": {"id": thread, "status": {"type": "idle"}, "turns": []}}}
            if method == "thread/queue/list":
                return {"result": {"data": []}}
            if method == "thread/unsubscribe":
                return {"result": {"status": "notLoaded"}}
            raise AssertionError("unexpected mesh method: " + method)

        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
        client.port = 32100
        with patch.object(codex_mesh, "Client", return_value=client), \
                patch.object(client, "call", side_effect=request), \
                patch.object(roles, "authority_request", side_effect=authority), \
                patch.object(roles, "confirm_adoption"):
            with self.assertRaisesRegex(OSError, "commit acknowledgement lost"):
                _ = roles.adopt("fixture-repo", "design.md", "designer", holder, "design", source_mesh=str(source),
                                name=name, thread_id=thread, adoption_id="transfer-one", binding=self.binding(self.run_a))
            committed = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                                   "host": "natedev", "request_id": "inspect-interruption"})
            self.assertEqual(committed["adoption_state"], "completed")
            self.assertNotIn("adoption_required", committed)
            adopted = roles.adopt("fixture-repo", "design.md", "designer", holder, "design", source_mesh=str(source),
                                  name=name, thread_id=thread, adoption_id="transfer-one", binding=self.binding(self.run_a))
        authority_state = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                                    "host": "natedev", "request_id": "inspect-completion"})
        self.assertEqual(authority_state["adoption_state"], "completed")
        self.assertNotIn("adoption_required", authority_state)
        self.assertEqual(adopted.placement.design_id, placement["design_id"])
        self.assertEqual(roles.read_design(adopted.placement.design_id).placement, adopted.placement)
        self.assertEqual(self.instance(adopted, "designer").lifecycle, roles.RoleOpen(thread))
        redirected = cast(dict[str, dict[str, object]], json.loads((source / "mesh_roster.json").read_text()))
        self.assertEqual(cast(dict[str, object], redirected[name]["role_redirect"])["design_id"], placement["design_id"])
        self.assertTrue((roles.design_directory(adopted.placement.design_id) / "adoption.json").is_file())
        self.assertFalse((roles.design_directory(adopted.placement.design_id) / "transition.json").exists())
        self.assertEqual([record["thread_id"] for record in astra_roles.live_roles()], [thread])
        self.assertEqual(self.launches, [])

    def test_adoption_refuses_a_thread_or_holder_outside_the_recorded_source(self) -> None:
        source = self.root / "inventoried-mesh"
        source.mkdir()
        roster = source / "mesh_roster.json"
        _ = roster.write_text(json.dumps({"original-name": {"thread_id": "original-thread", "status": "done"}}))
        _ = self.authority("reserve", {"repository_id": "fixture-repo", "document": "design.md",
                                       "host": "natedev", "request_id": "inventory"})
        _ = self.authority("legacy", {"repository_id": "fixture-repo", "document": "design.md", "host": "natedev",
            "source": {"mesh": str(source), "name": "original-name", "thread_id": "original-thread",
                       "holder": {"kind": "UnitHolder", "production_slug": "demo", "unit_name": "design-unit"}}})
        before = roster.read_bytes()
        for thread, holder in (("other-thread", roles.UnitHolder("demo", "design-unit")),
                               ("original-thread", roles.UnitHolder("demo", "other-unit"))):
            with self.subTest(thread=thread, holder=holder), \
                    patch.object(codex_mesh, "Client", side_effect=AssertionError("source must not be contacted")), \
                    self.assertRaisesRegex(roles.RoleRefused, "AdoptionRequired"):
                _ = roles.adopt("fixture-repo", "design.md", "designer", holder, "design", source_mesh=str(source),
                                name="original-name", thread_id=thread, adoption_id="transfer", binding=self.binding(self.run_a))
        self.assertEqual(roster.read_bytes(), before)
        self.assertFalse((source / "transfer.json").exists())
        self.assertFalse(list((self.root / "roles").glob("*/design.json")))

    def test_end_retry_requires_proof_that_the_original_turn_is_no_longer_live(self) -> None:
        design = self.open_designer()
        instance_id = design.placement.design_id + "/designer"
        active = True
        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))

        def request(method: str, params: object, **options: object) -> dict[str, object]:
            del params, options
            if method == "thread/turns/list":
                return {"result": {"data": [{"id": "still-running", "status": "inProgress"}] if active else []}}
            if method != "thread/read":
                raise AssertionError("unexpected cleanup proof request: " + method)
            return {"result": {"thread": {"id": "thread-designer", "status": {"type": "active" if active else "idle"}}}}

        mesh = roles.mesh_directory(design.placement.design_id)
        _ = (mesh / "mesh_server.json").write_text(json.dumps({"pid": os.getpid(), "port": 32100}))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(roles.RoleRefused, "RoleNeedsRecovery"):
                _ = roles.end(instance_id)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(codex_mesh, "Client", return_value=client), patch.object(client, "call", side_effect=request):
            with self.assertRaisesRegex(roles.RoleRefused, "RoleNeedsRecovery"):
                _ = roles.end(instance_id)
            self.assertIsInstance(self.instance(roles.read_design(design.placement.design_id), "designer").lifecycle,
                                  roles.RoleNeedsRecovery)
            self.assertTrue((self.root / "holds/thread-designer.json").exists())
            active = False
            ended = roles.end(instance_id)
        self.assertEqual(self.instance(ended, "designer").lifecycle, roles.RoleEnded("thread-designer"))
        self.assertFalse((self.root / "holds/thread-designer.json").exists())

    def test_showrunner_notifier_target_requires_a_live_local_session(self) -> None:
        with patch.object(production_tree, "showrunner_sessions", return_value={"demo": "stale-session"}), \
                patch.object(sessions, "running_sessions", return_value=[]):
            reach = resolve_current_holder(roles.ShowrunnerHolder("demo"))
        self.assertIsInstance(reach, roles.HolderUnavailable)
        if isinstance(reach, roles.HolderUnavailable):
            self.assertIn("local session", reach.reason)

    def test_adoption_destination_requires_the_same_idle_thread_without_a_new_turn(self) -> None:
        design = roles.open_role("fixture-repo", "design.md", "reviewer", roles.ShowrunnerHolder("demo"), "design")
        directory = roles.design_directory(design.placement.design_id)
        mesh = roles.mesh_directory(design.placement.design_id)
        prompt = next(directory.glob("prompts/*.md"))
        roster = cast(dict[str, dict[str, object]], json.loads((mesh / "mesh_roster.json").read_text()))
        roster[self.instance(design, "reviewer").name]["instructions_file"] = str(prompt)
        _ = (mesh / "mesh_roster.json").write_text(json.dumps(roster))
        _ = (mesh / "serving_home.json").write_text(json.dumps({"codex_home": str(self.root / "codex-home")}))
        resumed_thread = "thread-reviewer"
        requests: list[tuple[str, dict[str, object]]] = []

        def request(method: str, params: dict[str, object], *arguments: object,
                    **options: object) -> dict[str, object]:
            del arguments, options
            requests.append((method, params))
            if method == "thread/resume":
                return {"result": {"thread": {"id": resumed_thread}}}
            if method == "thread/read":
                return {"result": {"thread": {"id": resumed_thread, "status": {"type": "idle"}, "turns": []}}}
            raise AssertionError("adoption must not create a thread or turn: " + method)

        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
        with patch.object(codex_mesh, "ensure_server", return_value=(32100, False)), \
                patch.object(codex_mesh, "Client", return_value=client), \
                patch.object(client, "call", side_effect=request):
            roles.confirm_adoption(design, "reviewer", "thread-reviewer")
            resumed_thread = "replacement-thread"
            with self.assertRaisesRegex(roles.RoleRefused, "identity differs"):
                roles.confirm_adoption(design, "reviewer", "thread-reviewer")
        self.assertEqual([method for method, _params in requests], ["thread/resume", "thread/read", "thread/resume"])
        self.assertEqual(requests[0][1], {"threadId": "thread-reviewer",
                                         "developerInstructions": prompt.read_text(),
                                         "sandbox": "read-only", "approvalPolicy": "never"})


if __name__ == "__main__":
    _ = unittest.main()
