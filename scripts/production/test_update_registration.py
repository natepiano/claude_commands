"""Registration steps against a disposable production and notifier."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("update_registration.py")
NOTIFIER = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["REGISTRATION_TEST_STATE"])
args = sys.argv[1:]
with (root / "notifier-argv.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\\n")
instance = root / ("instance-" + args[1]) if len(args) > 1 else root / "missing"
if args[0] == "status":
    if not instance.exists():
        print(f"no such instance: {args[1]}", file=sys.stderr)
        raise SystemExit(1)
    mode = "stopped" if instance.read_text(encoding="utf-8") == "stopped" else "enabled"
    print(f"{args[1]} → session:test every 15 min, {mode}")
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
elif args[0] == "new":
    instance.touch()
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
else:
    raise SystemExit(2)
'''


class RegistrationTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    state: Path = Path()
    config: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.state = self.root / "state"
        self.config = self.root / "showrunners.json"
        self.doc = self.checkout / "docs/plans/example-production.md"
        self.home.mkdir()
        self.checkout.mkdir()
        self.state.mkdir()
        notifier = self.home / ".claude/scripts/message/notifier.sh"
        notifier.parent.mkdir(parents=True)
        _ = notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        notifier.chmod(0o755)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # The command text names zsh; this shim runs only the disposable Python notifier.
        zsh = bin_dir / "zsh"
        _ = zsh.write_text("#!/bin/sh\nexec '" + sys.executable + "' \"$@\"\n", encoding="utf-8")
        zsh.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "REGISTRATION_TEST_STATE": str(self.state), "SHOWRUNNER_STATE_DIR": str(self.state / "showrunner"),
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"), "SHOWRUNNERS_CONFIG": str(self.config),
                    "MAC_TEST_STATE_DIR": str(self.state / "mac-test"),
                    "CLAUDE_CODE_SESSION_ID": "current-session-id"}
        _ = self.git("init", "-b", "production", str(self.checkout), cwd=self.root)
        _ = self.git("config", "user.name", "Registration Test")
        _ = self.git("config", "user.email", "registration@example.invalid")
        self.doc.parent.mkdir(parents=True)
        # A linked worktree, where `.git` is a file: tmux is never asked whether its unit is gone.
        alpha = self.root / "alpha"
        alpha.mkdir()
        _ = (alpha / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        _ = self.doc.write_text("\n".join((
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`", f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** first-showrunner", "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles", "- **Updates:** every 15 minutes", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/alpha.md` | `{alpha}` | `alpha` | `alpha-session` | — | — |",
            "", "## Gates", "",
        )), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env if self.env else None,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.strip()

    def run_command(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--production", str(self.doc)],
                              cwd=self.checkout, env=env or self.env, capture_output=True, text=True, check=False)

    def notifier_calls(self) -> list[list[str]]:
        path = self.state / "notifier-argv.jsonl"
        return ([cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])

    def registry(self) -> list[dict[str, object]]:
        data = cast(dict[str, object], json.loads(self.config.read_text(encoding="utf-8")))
        return cast(list[dict[str, object]], data["showrunners"])

    def outstanding(self) -> Path:
        path = self.state / "showrunner/outstanding/example.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def copied_command(self) -> Path:
        tree = Path(tempfile.mkdtemp(prefix="command-copy-", dir=self.root))
        _ = shutil.copytree(SCRIPT.parent, tree / "scripts/production")
        for name in ("build_hold", "lint", "mac_test", "whoami"):
            _ = shutil.copytree(SCRIPT.parent.parent / name, tree / "scripts" / name)
        hooks = tree / "scripts/hooks"
        hooks.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT.parent.parent / "hooks/showrunner_footer.py", hooks)
        template = tree / "commands/showrunner/produce.md"
        template.parent.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT.parents[2] / "commands/showrunner/produce.md", template)
        return tree / "scripts/production/update_registration.py"

    def run_copy(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *args, "--production", str(self.doc)],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)

    def test_a_unit_with_no_session_and_no_worktree_leaves_the_registry(self) -> None:
        first = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # tmux knows only `kept-session`, and neither added unit has a worktree of its own.
        tmux = self.root / "bin" / "tmux"
        _ = tmux.write_text("#!/bin/sh\n[ \"$1\" = has-session ] || exit 99\n"
                            + "[ \"$3\" = '=kept-session' ] && exit 0\nexit 1\n", encoding="utf-8")
        tmux.chmod(0o755)
        rows = ("| `gone-unit` | `docs/gone.md` | `/nonexistent/gone` | `gone` | `gone-session` | — | — |\n"
                + "| `kept-unit` | `docs/kept.md` | `/nonexistent/kept` | `kept` | `kept-session` | — | — |\n")
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("\n\n## Gates", f"\n{rows}\n## Gates"), encoding="utf-8")
        _ = self.git("commit", "-am", "two more units")
        # The removed unit was registered while it was still there.
        added = subprocess.run([sys.executable, str(SCRIPT.with_name("showrunners.py")), "add", "first-showrunner",
                                "--zone", "America/Los_Angeles", "--unit", "gone-session"],
                               env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("gone-session", self.config.read_text(encoding="utf-8"))

        again = self.run_command("register", "--session", "first-showrunner")

        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.registry()[0]["units"],
                         [{"session": "alpha-session", "status": "running"},
                          {"session": "kept-session", "status": "running"}])

    def test_start_and_resume_retarget_only_updates_and_retire_old_session(self) -> None:
        first = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok", first.stdout)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.registry()[0]["session"], "first-showrunner")
        self.assertEqual(self.registry()[0]["units"],
                         [{"session": "alpha-session", "status": "running"}])
        initial_calls = self.notifier_calls()
        self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", "showrunner-example"]]), 1)
        updates = next(call for call in initial_calls if call[:2] == ["new", "showrunner-example"])
        self.assertEqual(updates[updates.index("--to") + 1], "session:current-session-id")
        self.assertIn("next_due", first.stdout)
        for instance in ("stall-watch", "tmux-names"):
            self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", instance]]), 1)

        new_env = {**self.env, "CLAUDE_CODE_SESSION_ID": "resume-session-id"}
        resumed = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("**Showrunner session:** resumed-showrunner", self.doc.read_text(encoding="utf-8"))
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(example): showrunner session resumed-showrunner")
        self.assertEqual([entry["session"] for entry in self.registry()], ["resumed-showrunner"])
        self.assertEqual(self.registry()[0]["units"],
                         [{"session": "alpha-session", "status": "running"}])
        registry_bytes = self.config.read_bytes()
        doc_bytes = self.doc.read_bytes()
        repeated = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.config.read_bytes(), registry_bytes)
        self.assertEqual(self.doc.read_bytes(), doc_bytes)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")
        calls = self.notifier_calls()
        self.assertEqual(len([call for call in calls if call[:2] == ["new", "showrunner-example"]]), 3)
        updates = [call for call in calls if call[:2] == ["new", "showrunner-example"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates],
                         ["session:current-session-id", "session:resume-session-id", "session:resume-session-id"])
        for instance in ("stall-watch", "tmux-names"):
            self.assertEqual(len([call for call in calls if call[:2] == ["new", instance]]), 1)

    def test_register_requires_session_and_environment_session_id(self) -> None:
        missing_name = self.run_command("register")
        self.assertEqual(missing_name.returncode, 2)
        self.assertEqual(self.notifier_calls(), [])
        without_id = {key: value for key, value in self.env.items() if key != "CLAUDE_CODE_SESSION_ID"}
        missing_id = self.run_command("register", "--session", "first-showrunner", env=without_id)
        self.assertEqual(missing_id.returncode, 2)
        self.assertIn("register: failed", missing_id.stdout + missing_id.stderr)
        self.assertEqual(self.notifier_calls(), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_refuses_dirty_production_doc_without_side_effects(self) -> None:
        with self.doc.open("a", encoding="utf-8") as output:
            _ = output.write("\nA local note.\n")
        before = self.doc.read_bytes()
        result = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("register: failed — the production doc has uncommitted edits; commit or discard them first", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notifier_calls(), [])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_failed_registry_rename_keeps_old_session_line_for_retry(self) -> None:
        first = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        script = self.copied_command()
        registry = script.with_name("showrunners.py")
        _ = registry.write_text("\n".join(("import sys", "if __name__ == '__main__':",
                                             "    print('rename unavailable', file=sys.stderr)",
                                             "    raise SystemExit(1)", "")), encoding="utf-8")
        doc_before = self.doc.read_bytes()
        commit_before = self.git("rev-parse", "HEAD")
        failed = self.run_copy(script, "register", "--session", "resumed-showrunner")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.assertIn("rename unavailable", failed.stdout)
        self.assertEqual(self.doc.read_bytes(), doc_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), commit_before)
        self.assertEqual([entry["session"] for entry in self.registry()], ["first-showrunner"])
        retry = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual([entry["session"] for entry in self.registry()], ["resumed-showrunner"])

    def test_missing_prompt_markers_fail_before_registration_changes(self) -> None:
        for marker in ("The prompt:", "**A tick**"):
            with self.subTest(marker=marker):
                script = self.copied_command()
                template = script.parents[2] / "commands/showrunner/produce.md"
                _ = template.write_text(template.read_text(encoding="utf-8").replace(marker, "", 1), encoding="utf-8")
                before = self.doc.read_bytes()
                result = self.run_copy(script, "register", "--session", "resumed-showrunner")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("register: failed", result.stdout)
                self.assertIn(marker, result.stdout)
                self.assertEqual(self.doc.read_bytes(), before)
                self.assertFalse(self.config.exists())
                self.assertEqual(self.notifier_calls(), [])
                self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
                shutil.rmtree(script.parents[2])

    def test_registered_prompt_keeps_status_save_dailies_call_and_render_state(self) -> None:
        result = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompts = list((self.state / "showrunner").rglob("prompt.txt"))
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0].read_text(encoding="utf-8")
        self.assertIn("unit_status.txt", prompt)
        self.assertIn("/showrunner:dailies simple", prompt)
        self.assertEqual(prompt.count("Pass `--render-state <SCRATCH>/dailies_state.json` to the builder"), 1)
        self.assertEqual(prompt.count("--render-state"), 1)
        self.assertIn("America/Los_Angeles", prompt)
        self.assertIn("first-showrunner", prompt)

    def test_time_converts_midnight_and_daylight_change_to_document_zone(self) -> None:
        midnight = self.run_command("time", "2026-10-07T06:30:00+00:00")
        self.assertEqual(midnight.returncode, 0, midnight.stdout + midnight.stderr)
        self.assertIn("23:30 PDT", midnight.stdout)
        winter = self.run_command("time", "2026-11-01T10:30:00+00:00")
        self.assertEqual(winter.returncode, 0, winter.stdout + winter.stderr)
        self.assertIn("02:30 PST", winter.stdout)
        self.assertNotIn("UTC", midnight.stdout + winter.stdout)

    def test_time_accepts_one_digit_hour_in_document_zone(self) -> None:
        result = self.run_command("time", "9:05")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^time: ok — 09:05 P[DS]T\n$")

    def test_tenth_log_event_lists_every_doc_unit_without_state(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        doc = doc.replace("\n## Gates", "| `beta-unit` | `docs/beta.md` | `/tmp/beta` | `beta` | `beta-session` | — | — |\n\n## Gates")
        _ = self.doc.write_text(doc, encoding="utf-8")
        for number in range(1, 11):
            result = self.run_command("log", f"event {number}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("### STATE "), 1)
        self.assertEqual(len(re.findall(r"^- \d{2}:\d{2} [A-Z]{3}: event \d+$", log, re.MULTILINE)), 10)
        self.assertIn("alpha-unit: not stated; last merged none; waits on not stated", log)
        self.assertIn("beta-unit: not stated; last merged none; waits on not stated", log)

    def test_state_block_matches_session_and_merge_history_and_reads_outstanding(self) -> None:
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit phase 2 (abc1234)")
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-04T10:00", "text": "review colors"}]) + "\n",
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": [{"unit": "alpha-session", "phase": "Phase 2 of 3: panels",
                                                    "wait": "review"}],
                                           "merges_held": ["alpha phase 2"],
                                           "open_for_user": ["stale state item"]}), encoding="utf-8")
        before = self.run_command("log", "compacting", "--state", str(state), "--before-compaction")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertIn("alpha-unit: Phase 2 of 3: panels; last merged phase 2 (abc1234); waits on review", log)
        self.assertIn("Merges accepted but held: alpha phase 2", log)
        self.assertIn("Open for the user: review colors", log)
        self.assertNotIn("stale state item", log)

    def test_invalid_state_fails_log_without_writing(self) -> None:
        log = self.checkout / "production.log"
        _ = log.write_text("existing line\n", encoding="utf-8")
        for key, value in (("unknown", 1), ("units", "wrong"), ("merges_held", "wrong")):
            with self.subTest(key=key):
                state = self.root / f"{key}.json"
                _ = state.write_text(json.dumps({key: value}), encoding="utf-8")
                before = log.read_bytes()
                result = self.run_command("log", "event", "--state", str(state), "--before-compaction")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"log: failed — {state}: {key} must be", result.stdout)
                self.assertEqual(log.read_bytes(), before)

    def test_footer_state_updates_outstanding_only_when_open_items_are_present(self) -> None:
        outstanding = self.outstanding()
        saved = [{"since": "2026-10-01T08:00", "text": "review colors"}]
        _ = outstanding.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        absent = self.run_command("footer", "--state", str(state))
        self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": ["review colors"]}), encoding="utf-8")
        retained = self.run_command("footer", "--state", str(state))
        self.assertEqual(retained.returncode, 0, retained.stdout + retained.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual(entries[0]["since"], "2026-10-01T08:00")
        _ = state.write_text(json.dumps({"open_for_user": []}), encoding="utf-8")
        cleared = self.run_command("footer", "--state", str(state))
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertEqual(json.loads(outstanding.read_text(encoding="utf-8")), [])

    def test_footer_rejects_bad_deferred_item_before_writing_outstanding(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"review colors"}]\n', encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}), encoding="utf-8")
        result = self.run_command("footer", "--state", str(state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("footer: failed", result.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_footer_nothing_needed_is_explicit(self) -> None:
        ordinary = self.run_command("footer")
        self.assertEqual(ordinary.returncode, 0, ordinary.stdout + ordinary.stderr)
        self.assertNotIn(" - nothing needed", ordinary.stdout)
        quiet = self.run_command("footer", "--nothing-needed")
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertIn("* no dailies scheduled - nothing needed", quiet.stdout)

    def test_footer_stopped_schedule_has_no_next_run(self) -> None:
        instance = self.state / "instance-showrunner-example"
        _ = instance.write_text("stopped", encoding="utf-8")
        stopped = self.run_command("footer")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("no dailies scheduled", stopped.stdout)
        self.assertNotIn("next dailies:", stopped.stdout)
        _ = instance.write_text("enabled", encoding="utf-8")
        enabled = self.run_command("footer")
        self.assertEqual(enabled.returncode, 0, enabled.stdout + enabled.stderr)
        self.assertIn("next dailies:", enabled.stdout)

    def test_footer_switch_off_drops_all_output_and_on_renders_footer(self) -> None:
        switch = self.state / "showrunner/footers-off/example"
        switch.parent.mkdir(parents=True)
        switch.touch()
        off = self.run_command("footer")
        self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
        self.assertEqual(off.stdout.strip(), "footer: ok — footers off")
        switch.unlink()
        on = self.run_command("footer")
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        self.assertIn("footer: ok", on.stdout)
        self.assertIn("update:", on.stdout)
        self.assertIn("no dailies scheduled", on.stdout)


if __name__ == "__main__":
    _ = unittest.main()
