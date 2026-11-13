"""Retain launch options and remove retired identity at caller boundaries."""
from __future__ import annotations

import unittest
import json
import os
from contextvars import Context
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Literal, override, cast
from unittest.mock import patch


from scripts.herdr import pane_launch
from scripts.production import session_labels, staff
from scripts.production.staff_test_support import isolated_staff_file
from scripts.lib import atomic_file, legacy_identity
from scripts.lib.test_models_registry import write_registry
from scripts.message import sessions
from scripts.agents import pane_record
from scripts.herdr import herdr


class LaunchEnvironmentTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        _ = self.enterContext(isolated_staff_file(Path(directory) / "staff.json", {}))

    def test_launch_does_not_retain_staff_environment(self) -> None:
        policy = ["--sandbox", "read-only", "-c", 'model_reasoning_effort="high"',
                  "--ask-for-approval", "never", "-c", "notice.hide_rate_limit_model_nudge=true",
                  "-c", 'service_tier="fast"', "-c", 'notify=["python3","notify.py"]']
        rows: tuple[tuple[Literal["claude", "codex"], list[str], list[str]], ...] = (
            ("claude", ["claude"], []),
            ("codex", ["codex", "--model", "sol", *policy], policy),
            ("codex", ["codex", "--sandbox=read-only", '-c=model_reasoning_effort="high"',
                       "--ask-for-approval=never", "-c", "notice.hide_rate_limit_model_nudge=true",
                       '-c=service_tier="fast"', '-c=notify=["python3","notify.py"]'], policy),
            ("codex", ["codex", "-C", "/work", "-c", 'projects={"/old"={trust_level="trusted"}}',
                       '-c=projects={"/older"={trust_level="trusted"}}', "resume", "native", "--model", "m", *policy], policy),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panes.json"
            for kind, arguments, expected in rows:
                with self.subTest(kind=kind, arguments=arguments):
                    entry = pane_launch.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, arguments, kind=kind)
                    self.assertEqual(entry["environment"], {})
                    pane_launch.save(path, {"pane": entry})
                    retained = pane_launch.read_strict(path)["pane"]
                    if kind == "codex":
                        self.assertEqual(retained.get("codex_policy"), expected)
                    else:
                        self.assertNotIn("codex_policy", retained)

    def test_shutdown_launch_removes_retired_identity_before_the_locked_launch(self) -> None:
        entry = pane_record.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, ["claude"])
        self.assertEqual(entry["environment"], {})

    def test_producer_publication_replaces_only_its_proved_binding(self) -> None:
        cases = ("control same pane", "move", "native reference", "different records", "interrupted records", "interrupted then unit write",
                 "wrong identity", "wrong backend", "wrong folder", "unread flags", "ended", "stale process",
                 "stale publication", "repeated publication", "requested account", "wrong requested account", "separate wrong requested account", "compaction after clear", "staff facts removed", "closed prior pane", "foreign prior pane")
        for kind in ("claude", "codex"):
            for case in cases:
                with self.subTest(kind=kind, case=case), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    owner = staff.StaffRow(staff.StaffId("fixed-producer"), "producer", "TEST-owner", staff.STAFF_ROOT,
                        current_session=staff.StaffId("native-producer"), home=staff.SessionHome(root / "account"),
                        worktree=staff.PresentWorktree("fixture", root))
                    other = replace(owner, id=staff.StaffId("other-producer"), name="TEST-other",
                                    current_session=staff.StaffId("other-native"))
                    member = replace(owner, id=staff.StaffId("unit"), kind="unit", current_session=staff.StaffId("unit-native"))
                    record = staff.StaffSnapshot((owner, other, member))
                    model = "opus" if kind == "claude" else "gpt-test"
                    registry = write_registry(root / "models.conf", {"producers": {owner.name: f"{kind}:{model}:high"}})
                    source = root / "source.json"
                    destination = root / "destination.json" if case in ("different records", "interrupted records", "interrupted then unit write") else source
                    observation = pane_launch.launch("old", owner.id, str(root), {}, [kind], kind=kind)
                    observation["written_at"] = "2026-01-01T00:00:00+00:00"
                    retained = pane_launch.StaffPaneLaunch(pane_id="old", staff_id=owner.id,
                        model=observation["model"], remote_control=observation["remote_control"],
                        environment=observation["environment"], flags_read=observation["flags_read"],
                        ended=False, written_at=observation["written_at"])
                    if "codex_policy" in observation:
                        retained["codex_policy"] = observation["codex_policy"]
                    old: pane_launch.PaneLaunch = retained
                    if case == "native reference":
                        old = {**observation, "session_id": owner.current_session}
                    unrelated: dict[str, pane_launch.PaneLaunch] = {
                        "other": {**retained, "pane_id": "other", "staff_id": other.id},
                        "unit": {**retained, "pane_id": "unit", "staff_id": member.id}}
                    pane_launch.save(source, {"old": old, **unrelated})
                    target = "old" if case == "control same pane" else "new"
                    launch = observation.copy()
                    launch.update(pane_id=target, session_id=owner.current_session, kind=kind,
                                  pid=456, proc_start="current", written_at="2026-02-01T00:00:00+00:00")
                    if case == "wrong backend":
                        launch["kind"] = "codex" if kind == "claude" else "claude"
                    if case == "wrong folder":
                        launch["cwd"] = str(root / "wrong")
                    if case == "unread flags":
                        launch["flags_read"] = False
                    if case == "ended":
                        launch["ended"] = True
                    observed = herdr.NativeAgentProcess(
                        herdr.EnvironmentThread("wrong" if case == "wrong identity" else owner.current_session),
                        "old" if case == "stale process" else "current",
                        {"HERDR_PANE_ID": target, "HERDR_SESSION": destination.stem,
                         "CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR": str(
                             root / "destination-account" if case in ("requested account", "compaction after clear") else root / "account")})
                    request = pane_launch.ProducerLaunchRequest(owner.id, owner.current_session, kind,
                                                               root, root / "destination-account")
                    def native_process(pid: int, family: str, reported: str = "") -> herdr.NativeAgentProcess:
                        del family, reported
                        return (replace(observed, identity=herdr.EnvironmentThread(other.current_session))
                                if pid == 999 else observed)
                    with (patch.object(staff, "initial_snapshot", return_value=record),
                          patch.object(staff, "local_snapshot", return_value=record),
                          patch.dict("os.environ", {"MODELS_CONFIG_FILE": str(registry)}),
                          patch.object(herdr, "native_process", side_effect=native_process),
                          patch.object(herdr.Herdr, "result", return_value={"process_info": {
                              "shell_pid": 998, "foreground_process_group_id": 999,
                              "foreground_processes": [{"pid": 999, "name": kind, "argv": [kind]}]}})
                          if case == "foreign prior pane" else nullcontext(),
                          patch.object(herdr.Herdr, "panes", return_value=[] if case == "closed prior pane" else
                                       [{"pane_id": "old"}]),
                          pane_launch.producer_launch_request(request) if case in ("requested account", "wrong requested account", "compaction after clear") else nullcontext()):
                        if case == "separate wrong requested account":
                            environment = {"HERDR_PRODUCER_LAUNCH_REQUEST" + owner.id.encode().hex(): json.dumps({
                                "member": request.member, "session": request.session, "kind": request.kind,
                                "folder": str(request.folder), "home": str(request.home)})}
                            with patch.dict("os.environ", environment):
                                with self.assertRaisesRegex(ValueError, "producer publication uses another account"):
                                    Context().run(pane_launch.write, destination, launch, sources=(source, destination))
                            self.assertEqual(pane_launch.read_strict(source), {"old": old, **unrelated})
                            self.assertEqual(pane_launch.read_strict(destination),
                                             {"old": old, **unrelated} if destination == source else {})
                            continue
                        if case in ("wrong identity", "wrong backend", "wrong folder", "unread flags", "ended", "stale process", "wrong requested account"):
                            with self.assertRaises(ValueError):
                                pane_launch.write(destination, launch, sources=(source, destination))
                            self.assertEqual(pane_launch.read_strict(source), {"old": old, **unrelated})
                            self.assertEqual(pane_launch.read_strict(destination),
                                             {"old": old, **unrelated} if destination == source else {})
                            continue
                        if case in ("interrupted records", "interrupted then unit write"):
                            publish = atomic_file.write_text_atomic
                            def interrupt(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
                                publish(path, text, new_file_mode=new_file_mode)
                                if path == destination:
                                    raise OSError("fixture interrupted publication")
                            with patch.object(atomic_file, "write_text_atomic", side_effect=interrupt):
                                with self.assertRaises(OSError):
                                    pane_launch.write(destination, launch, sources=(source, destination))
                            if case == "interrupted then unit write":
                                update = unrelated["unit"].copy()
                                update["model"] = "updated-unit-model"
                                pane_launch.write(source, update)
                                _ = update.pop("config_dir", None)
                                unrelated["unit"] = update
                        else:
                            pane_launch.write(destination, launch, sources=(source, destination))
                        if case == "compaction after clear":
                            owner = replace(owner, current_session=staff.StaffId("cleared-producer"),
                                            home=staff.SessionHome(request.home))
                            cleared_record = staff.StaffSnapshot((owner, other, member))
                            launch.update(session_id=owner.current_session, written_at="2026-03-01T00:00:00+00:00")
                            cleared_process = replace(observed, identity=herdr.EnvironmentThread(owner.current_session))
                            with (patch.object(staff, "initial_snapshot", return_value=cleared_record),
                                  patch.object(staff, "local_snapshot", return_value=cleared_record),
                                  patch.object(herdr, "native_process", return_value=cleared_process)):
                                Context().run(pane_launch.write, destination, launch, sources=(source, destination))
                        committed = pane_launch.read_strict(destination)[target]
                        if case == "staff facts removed":
                            self.assertEqual(set(committed) & {"session_id", "name", "cwd", "kind", "config_dir"}, set())
                        if case == "compaction after clear":
                            self.assertEqual(committed["written_at"], launch["written_at"])
                        if case == "stale publication":
                            stale = launch.copy()
                            stale.update(pane_id="old", written_at=old["written_at"])
                            pane_launch.write(source, stale, sources=(source, destination))
                        if case == "repeated publication":
                            before = destination.read_bytes()
                            pane_launch.write(destination, launch, sources=(source, destination))
                            self.assertEqual(destination.read_bytes(), before)
                    combined = {pane: entry for path in {source, destination} for pane, entry in pane_launch.read_strict(path).items()}
                    prior = ({"old": pane_launch.retained_launch(old, record)}
                             if target != "old" and case not in ("closed prior pane", "foreign prior pane") else {})
                    self.assertEqual(combined, {**unrelated, **prior, target: committed})
                    self.assertEqual(cast(pane_launch.StaffPaneLaunch, committed)["staff_id"], owner.id)

    def test_readout_keeps_a_staffed_pane_with_an_unknown_home(self) -> None:
        row = staff.StaffRow(staff.StaffId("TEST-unknown-home"), "producer", "TEST-unknown-home", staff.STAFF_ROOT,
                             current_session=staff.StaffId("TEST-unknown-home"))
        entry = pane_launch.launch("TEST-pane", row.id, "/work", {}, ["claude"])
        _ = entry.pop("config_dir", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST-panes.json"
            entries: dict[str, pane_launch.PaneLaunch] = {}
            for index in range(3):
                current = entry.copy()
                current["pane_id"] = "TEST-pane-" + str(index)
                entries[current["pane_id"]] = current
            pane_launch.save(path, entries)
            with patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))) as snapshot:
                self.assertIsInstance(pane_launch.config_home(entry), staff.HomeUnknown)
                snapshot.reset_mock()
                self.assertEqual(pane_launch.read_valid_entries(path), entries)
                snapshot.assert_called_once_with()
                snapshot.reset_mock()
                self.assertEqual(pane_launch.read_valid_entries(path, record=staff.StaffSnapshot((row,))), entries)
                snapshot.assert_not_called()
            self.assertEqual(pane_launch.read_strict(path), entries)
            user_home = Path(directory) / "home"
            user_home.mkdir()
            for source in ("configured", "staff", "numbered"):
                for folder in ("sessions", "archived_sessions"):
                    with self.subTest(source=source, folder=folder):
                        home = user_home / ".codex2" if source == "numbered" else Path(directory) / source
                        identity = f"{row.current_session}-{source}-{folder}"
                        transcript = home / folder / f"{identity}.jsonl"
                        transcript.parent.mkdir(parents=True, exist_ok=True)
                        transcript.touch()
                        current = replace(row, current_session=staff.StaffId(identity),
                                          home=staff.SessionHome(home) if source == "staff" else staff.HomeUnknown())
                        with (patch.dict(os.environ, {"HOME": str(user_home),
                                                      "CODEX_HOME": str(home) if source == "configured" else ""}),
                              patch.object(pane_launch, "native_kind", return_value=""),
                              patch.object(sessions, "transcript_locations", return_value=[])):
                            self.assertEqual(pane_launch.current_kind(current), "codex")

    def test_environment_boundaries_use_the_shared_identity_filter(self) -> None:
        environment = {"STAFF_ID": "old", "SHOWRUNNER_UNIT": "old", "CODEX_THREAD_ID": "native", "PATH": "/bin"}
        expected = {"CODEX_THREAD_ID": "native", "PATH": "/bin"}
        self.assertEqual(legacy_identity.without_legacy_identity(environment), expected)
        self.assertEqual(environment["STAFF_ID"], "old")
        labels = session_labels.StaffLabels(staff.StaffSnapshot((), production=False))
        entry = pane_launch.launch("pane", "native", "/work", {}, ["claude"])
        entry["environment"] = environment
        with (patch.object(legacy_identity, "without_legacy_identity", return_value=expected) as remove,
              patch.object(sessions, "read_session", return_value=sessions.UnreadableSessionRecord.FOUND)):
            self.assertEqual(pane_launch.RecordedLabels().resolve(1, environment), expected)
            self.assertEqual(labels.environment(environment, "native"), expected)
            self.assertEqual(pane_record.without_legacy_identity(entry)["environment"], expected)
            self.assertEqual(herdr.child_environment(environment), expected)
        self.assertEqual(remove.call_count, 4)


if __name__ == "__main__":
    _ = unittest.main()
