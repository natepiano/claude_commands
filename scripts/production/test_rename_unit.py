"""Exercise unit rename orchestration with isolated tmux, git, and state roots."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import add_unit
import rename_state
import rename_unit
import send
import showrunners
import tmux_names


TMUX_STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["TEST_TMUX_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with Path(os.environ["TEST_TMUX_EVENTS"]).open("a") as events:
    events.write(json.dumps(args) + "\n")
if args[0] == "list-panes":
    for name, panes in state.items():
        for pane in panes:
            print(name + "\t" + pane)
elif args[0] == "display-message":
    if os.environ.get("TEST_TMUX_SOCKET_FAILURE") == "1":
        print("injected socket query failure", file=sys.stderr)
        raise SystemExit(1)
    print(os.environ["TEST_TMUX_SOCKET"])
elif args[0] == "send-keys":
    if "-l" in args:
        Path(os.environ["TEST_TMUX_PENDING"]).write_text(args[-1].removeprefix("/rename "))
    elif args[-1] == "Enter" and os.environ.get("TEST_RENAME_APPLIES") == "1":
        record_path = Path(os.environ["TEST_SESSION_RECORD"])
        record = json.loads(record_path.read_text())
        old = record["name"]
        new = Path(os.environ["TEST_TMUX_PENDING"]).read_text()
        record["name"] = new
        record["nameSource"] = "user"
        record.setdefault("formerNames", []).append({"name": old, "until": "later", "sessionId": record["sessionId"]})
        record_path.write_text(json.dumps(record))
elif args[0] == "rename-session":
    if Path(os.environ["TEST_TMUX_RENAME_FAILURE"]).exists():
        print("injected tmux rename failure", file=sys.stderr)
        raise SystemExit(1)
    old = args[args.index("-t") + 1].removeprefix("=")
    new = args[-1]
    state[new] = state.pop(old)
    state_path.write_text(json.dumps(state))
else:
    raise SystemExit(2)
'''


def no_sleep(_seconds: float) -> None:
    pass


@final
class RenameUnitTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.checkout = Path()
        self.origin = Path()
        self.doc = Path()
        self.scratch = Path()
        self.sessions = Path()
        self.proc = Path()
        self.config = Path()
        self.tmux_state = Path()
        self.events_path = Path()
        self.record_path = Path()
        self.child: subprocess.Popen[bytes] | None = None
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.root / "project-trunk"
        self.checkout.mkdir()
        self.origin = self.root / "origin.git"
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "build-followups")
        _ = self.git("config", "user.name", "Unit Test")
        _ = self.git("config", "user.email", "unit@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.doc = self.checkout / "docs/plans/build-followups-production.md"
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")
        _ = self.git("push", "-u", "origin", "build-followups")

        self.scratch = self.root / "scratch"
        eta = self.scratch / "dailies_input_state/eta_seen.json"
        eta.parent.mkdir(parents=True)
        _ = eta.write_text(json.dumps({"old|phase": "seen"}) + "\n", encoding="utf-8")
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.close_child)
        self.record_path = self.sessions / f"{self.child.pid}.json"
        _ = self.record_path.write_text(json.dumps({
            "pid": self.child.pid,
            "name": "old",
            "nameSource": "user",
            "sessionId": "session-1",
            "formerNames": [],
        }), encoding="utf-8")
        process = self.proc / str(self.child.pid)
        process.mkdir()
        _ = (process / "environ").write_bytes(b"TMUX=/tmp/tmux-test/default,123,0\0TMUX_PANE=%1\0")

        self.config = self.root / "showrunners.json"
        self.write_registry([{"session": "director", "zone": "America/New_York", "units": ["old"]}])
        self.tmux_state = self.root / "tmux.json"
        _ = self.tmux_state.write_text(json.dumps({"old": ["%1"]}), encoding="utf-8")
        self.events_path = self.root / "events.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        tmux = bin_dir / "tmux"
        _ = tmux.write_text(TMUX_STUB, encoding="utf-8")
        tmux.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "SHOWRUNNERS_CONFIG": str(self.config),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "STALL_WATCH_STATE_DIR": str(self.root / "stall-watch"),
            "TMUX_NAMES_PROC_DIR": str(self.proc),
            "TMUX_NAMES_TMUX": "tmux",
            "XDG_STATE_HOME": str(self.root / "state"),
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "TEST_TMUX_STATE": str(self.tmux_state),
            "TEST_TMUX_SOCKET": "/tmp/tmux-test/default",
            "TEST_TMUX_EVENTS": str(self.events_path),
            "TEST_TMUX_PENDING": str(self.root / "pending"),
            "TEST_TMUX_RENAME_FAILURE": str(self.root / "tmux-failure"),
            "TEST_SESSION_RECORD": str(self.record_path),
            "TEST_RENAME_APPLIES": "1",
        }

    def close_child(self) -> None:
        if self.child is not None:
            self.child.terminate()
            _ = self.child.wait(timeout=3)

    def production_doc(self) -> str:
        return ("# Production\n\n"
                "- **Merge branch:** `build-followups`\n"
                f"- **Showrunner checkout:** `{self.checkout}`\n"
                "- **Showrunner session:** director\n"
                "- **Log:** `docs/plans/build-followups-log.md`\n"
                "- **User zone:** America/New_York\n\n"
                "## Units\n\n"
                "| Unit | Plan | Worktree | Branch | Session | Port | Owns |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| alpha-unit | docs/plans/alpha.md | /tmp/alpha | build-followups-alpha | `old` — active | — | src/alpha |\n\n"
                "## Gates\n")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout, capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def write_registry(self, runners: list[dict[str, object]]) -> None:
        _ = self.config.write_text(json.dumps({
            "threshold_percent": 2,
            "repeat_minutes": 30,
            "stall_minutes": 5,
            "faults_to": "natedev",
            "always": [],
            "showrunners": runners,
        }) + "\n", encoding="utf-8")

    def set_claude_name(self, name: str, *, former: str | None = None) -> None:
        record = cast(dict[str, object], json.loads(self.record_path.read_text()))
        record["name"] = name
        record["formerNames"] = ([] if former is None else [
            {"name": former, "until": "later", "sessionId": record["sessionId"]},
        ])
        _ = self.record_path.write_text(json.dumps(record), encoding="utf-8")

    def add_dead_record(self, name: str) -> None:
        live_record = self.record_path.read_bytes()
        self.record_path.unlink()
        _ = (self.sessions / f"{2**30}.json").write_text(json.dumps({
            "pid": 2**30,
            "name": name,
            "nameSource": "user",
            "sessionId": "session-1",
            "formerNames": [],
        }), encoding="utf-8")
        _ = self.record_path.write_bytes(live_record)

    def events(self) -> list[list[str]]:
        if not self.events_path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.events_path.read_text().splitlines()]

    def cli(self, old: str = "old", new: str = "new") -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(tmux_names, "SESSIONS_DIR", self.sessions), \
                mock.patch.object(tmux_names, "PROC_DIR", self.proc), \
                mock.patch.object(tmux_names, "TMUX", "tmux"), \
                mock.patch.object(showrunners, "CONFIG", self.config), \
                mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"), \
                mock.patch.object(send, "STATE", self.root / "state/message"), \
                mock.patch.object(rename_unit, "WAIT", rename_unit.RenameWait(0, no_sleep)), \
                redirect_stdout(output), redirect_stderr(errors):
            result = rename_unit.main([
                "--production", str(self.doc), "--scratch", str(self.scratch), old, new,
            ])
        return result, output.getvalue(), errors.getvalue()

    def registry(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], json.loads(self.config.read_text())["showrunners"])

    def test_typed_rename_updates_every_orchestration_target_and_is_repeatable(self) -> None:
        result, output, errors = self.cli()
        self.assertEqual(result, 0, errors)
        self.assertEqual(errors, "")
        sends = [event for event in self.events() if event[:1] == ["send-keys"]]
        self.assertEqual(sends, [
            ["send-keys", "-t", "%1", "-l", "/rename new"],
            ["send-keys", "-t", "%1", "Enter"],
        ])
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"new": ["%1"]})
        self.assertEqual(self.registry()[0]["units"], ["new"])
        self.assertIn("| `new` — active |", self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): alpha-unit's session is now new")
        self.assertEqual(self.git("rev-parse", "HEAD"),
                         self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"))
        eta = cast(dict[str, object], json.loads(
            (self.scratch / "dailies_input_state/eta_seen.json").read_text()))
        self.assertEqual(eta, {"new|phase": "seen"})
        self.assertIn("renamed: Claude session", output)
        self.assertIn("left: remote-control name", output)

        event_count = len(self.events())
        second, second_output, second_errors = self.cli()
        self.assertEqual(second, 0, second_errors)
        self.assertNotIn("renamed:", second_output)
        self.assertEqual(len(self.events()), event_count + 3)

    def test_record_that_already_has_new_name_sends_no_keys(self) -> None:
        self.set_claude_name("new", former="old")
        result, output, errors = self.cli()
        self.assertEqual(result, 0, errors)
        self.assertNotIn("renamed: Claude session", output)
        self.assertFalse(any(event[:1] == ["send-keys"] for event in self.events()))
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"new": ["%1"]})

    def test_typed_rename_ignores_dead_record_with_same_session_id(self) -> None:
        self.add_dead_record("old")
        result, _output, errors = self.cli()
        self.assertEqual(result, 0, errors)
        self.assert_finished()

    def test_session_on_another_tmux_server_is_not_typed_into(self) -> None:
        _ = (self.proc / self.record_path.stem / "environ").write_bytes(
            b"TMUX=/tmp/tmux-test/other,123,0\0TMUX_PANE=%1\0"
        )
        self.assert_refused("Claude sessions found: 0 named old")

    def test_tmux_socket_query_failure_refuses_before_changes(self) -> None:
        self.environment["TEST_TMUX_SOCKET_FAILURE"] = "1"
        self.assert_refused("tmux could not be asked")

    def test_rerun_ignores_dead_record_with_same_session_id(self) -> None:
        self.set_claude_name("new", former="old")
        self.add_dead_record("old")
        result, _output, errors = self.cli()
        self.assertEqual(result, 0, errors)
        self.assertFalse(any(event[:1] == ["send-keys"] for event in self.events()))
        self.assert_finished()

    def test_session_timeout_changes_no_other_state(self) -> None:
        self.environment["TEST_RENAME_APPLIES"] = "0"
        document = self.doc.read_bytes()
        registry = self.config.read_bytes()
        head = self.git("rev-parse", "HEAD")
        result, output, errors = self.cli()
        self.assertEqual(result, 1, output)
        self.assertIn("did not take the name within 30 seconds", errors)
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(self.config.read_bytes(), registry)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"old": ["%1"]})

    def test_state_step_uses_rename_all_result(self) -> None:
        self.set_claude_name("new", former="old")
        with mock.patch.object(rename_state, "rename_all",
                               return_value=["recorded state"]) as rename_all:
            result, output, errors = self.cli()
        self.assertEqual(result, 0, errors)
        rename_all.assert_called_once_with("old", "new", self.scratch)
        self.assertIn("renamed: recorded state", output)

    def test_retry_after_typed_rename_finishes(self) -> None:
        incomplete = tmux_names.RenameIncomplete("injected after typed rename")
        with mock.patch.object(tmux_names, "rename_session", return_value=incomplete):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("injected after typed rename", errors)
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_registry_rename_finishes(self) -> None:
        failure = self.root / "tmux-failure"
        _ = failure.touch()
        first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("tmux session still names", errors)
        self.assertEqual(self.registry()[0]["units"], ["new"])
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"old": ["%1"]})
        failure.unlink()
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_tmux_rename_finishes(self) -> None:
        with mock.patch.object(rename_unit, "_commit_row", side_effect=OSError("after tmux rename")):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("after tmux rename", errors)
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"new": ["%1"]})
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_cell_write_finishes(self) -> None:
        def write_then_fail(plan: rename_unit.RenamePlan, old: str, new: str) -> list[str]:
            assert isinstance(plan.row, rename_unit.RowNamesOld)
            rename_unit.rewrite_row(plan.production, plan.row.row, old, new)
            raise OSError("after cell write")

        with mock.patch.object(rename_unit, "_commit_row", side_effect=write_then_fail):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("after cell write", errors)
        self.assertIn("| `new` — active |", self.doc.read_text())
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_commit_finishes_push(self) -> None:
        def commit_then_fail(plan: rename_unit.RenamePlan, old: str, new: str) -> list[str]:
            assert isinstance(plan.row, rename_unit.RowNamesOld)
            rename_unit.rewrite_row(plan.production, plan.row.row, old, new)
            relative = str(plan.production.doc.relative_to(plan.production.checkout))
            _ = add_unit.git(plan.production, "add", "--", relative)
            _ = add_unit.git(
                plan.production,
                "commit",
                "--only",
                "-m",
                f"production({plan.production.slug}): {plan.unit}'s session is now {new}",
                "--",
                relative,
            )
            raise OSError("after commit")

        origin_before = self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups")
        with mock.patch.object(rename_unit, "_commit_row", side_effect=commit_then_fail):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("after commit", errors)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), origin_before)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"),
                         origin_before)
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_push_finishes_state(self) -> None:
        refusal = rename_state.RenameRefused("after push")
        with mock.patch.object(rename_state, "rename_all", side_effect=refusal):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("after push", errors)
        self.assertEqual(self.git("rev-parse", "HEAD"),
                         self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"))
        second, _output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assert_finished()

    def test_retry_after_state_moves_finishes(self) -> None:
        real_rename_all = rename_state.rename_all

        def move_then_fail(old: str, new: str, scratch: Path) -> list[str]:
            _ = real_rename_all(old, new, scratch)
            raise rename_state.RenameRefused("after state stores")

        with mock.patch.object(rename_state, "rename_all", side_effect=move_then_fail):
            first, _output, errors = self.cli()
        self.assertEqual(first, 1)
        self.assertIn("after state stores", errors)
        second, second_output, errors = self.cli()
        self.assertEqual(second, 0, errors)
        self.assertNotIn("renamed:", second_output)
        self.assert_finished()

    def test_tmux_name_collision_refuses_before_changes(self) -> None:
        _ = self.tmux_state.write_text(json.dumps({"old": ["%1"], "new": ["%2"]}))
        self.assert_refused("tmux session new is already taken")

    def test_showrunner_session_collision_refuses_before_changes(self) -> None:
        self.write_registry([
            {"session": "director", "zone": "America/New_York", "units": ["old"]},
            {"session": "new", "zone": "America/New_York", "units": []},
        ])
        self.assert_refused("showrunner registry session new is already taken")

    def test_registry_unit_collision_with_old_refuses_before_changes(self) -> None:
        self.write_registry([{"session": "director", "zone": "America/New_York",
                              "units": ["old", "new"]}])
        self.assert_refused("showrunner registry unit new is already taken")

    def test_registry_unit_collision_while_awaiting_refuses_before_changes(self) -> None:
        self.write_registry([{"session": "director", "zone": "America/New_York", "units": ["new"]}])
        self.assert_refused("showrunner registry unit new is already taken")

    def test_second_units_row_with_either_name_refuses_before_changes(self) -> None:
        extra = "| retired-unit | retired after completion | /tmp/retired | retired | new | — | — |\n\n"
        _ = self.doc.write_text(self.doc.read_text().replace("## Gates", extra + "## Gates"))
        self.assert_refused("2 rows, live or retired")

    def test_invalid_and_equal_names_refuse_before_changes(self) -> None:
        document = self.doc.read_bytes()
        registry = self.config.read_bytes()
        head = self.git("rev-parse", "HEAD")
        for new, message in (("new name", "new name must contain only"),
                             ("old", "new name must differ")):
            with self.subTest(new=new):
                result, output, errors = self.cli(new=new)
                self.assertEqual(result, 1, output)
                self.assertIn(message, errors)
                self.assertEqual(self.doc.read_bytes(), document)
                self.assertEqual(self.config.read_bytes(), registry)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(any(event[:1] == ["send-keys"] for event in self.events()))

    def test_checkout_off_merge_branch_refuses_before_changes(self) -> None:
        _ = self.git("checkout", "-b", "other-branch")
        self.assert_refused("Showrunner checkout is not on build-followups")

    def test_new_claude_name_without_object_former_name_refuses(self) -> None:
        self.set_claude_name("new")
        self.assert_refused("new-name records list old in formerNames")
        record = cast(dict[str, object], json.loads(self.record_path.read_text()))
        record["formerNames"] = ["old"]
        _ = self.record_path.write_text(json.dumps(record))
        self.assert_refused("new-name records list old in formerNames")

    def test_no_matching_units_row_refuses_before_changes(self) -> None:
        _ = self.doc.write_text(self.doc.read_text().replace("`old` — active", "other"))
        self.assert_refused("0 rows, live or retired")

    def assert_refused(self, message: str) -> None:
        document = self.doc.read_bytes()
        registry = self.config.read_bytes()
        head = self.git("rev-parse", "HEAD")
        result, output, errors = self.cli()
        self.assertEqual(result, 1, output)
        self.assertIn(message, errors)
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(self.config.read_bytes(), registry)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(any(event[:1] == ["send-keys"] for event in self.events()))

    def assert_finished(self) -> None:
        record = cast(dict[str, object], json.loads(self.record_path.read_text()))
        self.assertEqual(record["name"], "new")
        self.assertEqual(json.loads(self.tmux_state.read_text()), {"new": ["%1"]})
        self.assertEqual(self.registry()[0]["units"], ["new"])
        self.assertIn("| `new` — active |", self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"),
                         self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"))
        eta = cast(dict[str, object], json.loads(
            (self.scratch / "dailies_input_state/eta_seen.json").read_text()))
        self.assertEqual(eta, {"new|phase": "seen"})


if __name__ == "__main__":
    _ = unittest.main()
