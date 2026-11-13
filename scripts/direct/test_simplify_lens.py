"""Check daily simplify reviews against a private origin and a fake Claude."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from typing import ClassVar, cast, override
from unittest.mock import patch

from scripts.direct import simplify_daily
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.git_test_support import commit_repository
from scripts.lib.nix_env_test_support import NixFixture, ROOT
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case
from scripts.whoami.account_test_support import write_claude_account


class ReviewTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> ReviewTime:
        moment = cls.fromisoformat(os.environ["SIMPLIFY_TEST_NOW"])
        return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)


class DailySimplifyTests(NixFixture):
    agent_exec: ClassVar[Path]
    origin: Path = Path()
    session: Path = Path()
    calls: Path = Path()
    before: str = ""
    tip: str = ""
    first: str = ""
    real_git: str = ""
    day: str = "2026-11-05"

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        launcher = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        for folder, names in (
            ("agents", ("agent_exec.sh", "models_config.sh", "claude_account.sh", "claude_result.py")),
            ("lib", ("py", "package_root.py", "models_registry.py", "nix_env.py")),
        ):
            destination = launcher / "scripts" / folder
            destination.mkdir(parents=True)
            for name in names:
                _ = shutil.copy2(ROOT / "scripts" / folder / name, destination / name)
        direct = launcher / "scripts/direct"
        direct.mkdir()
        _ = (direct / "assign_gate.py").write_text("# Admission succeeds in this private fixture.\n")
        cls.agent_exec = launcher / "scripts/agents/agent_exec.sh"

    @override
    def setUp(self) -> None:
        super().setUp()
        self.real_git = shutil.which("git") or "git"
        self.origin = self.root / "origin.git"
        self.session = self.root / "run"
        self.session.mkdir()
        self.calls = self.root / "claude_calls.jsonl"
        self.env.update({
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            **interpreter_cache_environment(self.root / "cache"), "CLAUDE_CONFIG_DIR": str(self.home / ".claude1"),
            "USAGE_STATE_DIR": str(self.root / "usage"), "USER_ZONE": "America/New_York",
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "UNIT_HISTORY_DIR": str(self.root / "history"), 
            "SIMPLIFY_TEST_CALLS": str(self.calls), "SIMPLIFY_TEST_NOW": "2026-11-06T03:30:00+00:00",
            "TMPDIR": str(self.root / "tmp"),
        })
        Path(self.env["TMPDIR"]).mkdir()
        shim = self.home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(ROOT / "scripts/lib/py")
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "unit@example.invalid"},
        }))
        with patch.dict(os.environ, self.env):
            write_claude_account("claude1", "unit@example.invalid", 100,
                                 datetime.now(timezone.utc) + timedelta(days=30))
        registry = write_registry(self.root / "models.conf", base_text=(ROOT / "config/models.conf").read_text())
        self.env.update({"MODELS_CONFIG_FILE": str(registry), "MODELS_CONFIG_PUBLISH": "0",
                         "CODEX_CATALOG_SYNC_STATE_FILE": str(registry)})
        settings = Path(self.env["CONF_DIR"])
        _ = write_config_fixture(settings / "direct.conf", "SIMPLIFY_LENS=on\nSIMPLIFY_FIRST_RUN_BEFORE=2026-11-05T22:50:00-05:00\n")
        _ = write_config_fixture(settings / "build_folders.conf", f"roots.copies={self.root / 'copies'}\n")
        startup = self.root / "startup"
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text('''import datetime, os, sys
if sys.argv[0].endswith(("assign.py", "simplify_daily.py")):
    original_datetime = datetime.datetime
    class ReviewTime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            moment = original_datetime.fromisoformat(os.environ["SIMPLIFY_TEST_NOW"])
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)
    datetime.datetime = ReviewTime
    def enter(frame, event, argument):
        if event == "call" and frame.f_code.co_name == "main" and frame.f_code.co_filename.endswith("/direct/assign.py"):
            sys.setprofile(None)
            frame.f_globals["require_live_director"] = lambda: None
            if os.environ.get("SIMPLIFY_TEST_STATUS_FAILURE") == "1":
                from scripts.direct import simplify_daily
                publish = simplify_daily.write_text_atomic
                def publish_status(path, text, **kwargs):
                    if path.name == "status" and text == "reviewed\\n":
                        raise PermissionError("reviewed status refused")
                    publish(path, text, **kwargs)
                simplify_daily.write_text_atomic = publish_status
    sys.setprofile(enter)
''')
        self.env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "scripts/lib/python_startup"), str(startup)))
        self.script(self.bin / "claude", '''import json, os, subprocess, sys, uuid
from pathlib import Path
session = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else str(uuid.uuid4())
record = {"argv": sys.argv[1:], "prompt": sys.stdin.read(), "cwd": os.getcwd(),
          "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "session_id": session,
          "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
          "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
          "content": Path("payload.txt").read_text()}
with Path(os.environ["SIMPLIFY_TEST_CALLS"]).open("a") as stream:
    stream.write(json.dumps(record) + "\\n")
failed = os.environ.get("SIMPLIFY_TEST_ERROR") == "1"
print(json.dumps({"type": "result", "session_id": session, "is_error": failed,
                  "result": "Provider refused the review" if failed else "Pass"}))
raise SystemExit(1 if failed else 0)
''')
        _ = self.git("init", "--bare", "--template=", "-b", "trunk", str(self.origin))
        _ = self.git("init", "--template=", "-b", "trunk", str(self.work))
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.first = self.commit("root", "2026-11-04T10:00:00-05:00")
        self.before = self.commit("before cutoff", "2026-11-05T22:49:00-05:00")
        self.tip = self.commit("origin tip", "2026-11-05T22:51:00-05:00")
        _ = self.git("push", "origin", "trunk")

    def git(self, *arguments: str, repository: Path | None = None) -> str:
        result = subprocess.run([self.real_git, "-C", str(repository or self.work), *arguments],
                                env=self.env, capture_output=True, text=True, check=True,
                                timeout=TEST_HANG_GUARD_SECS)
        return result.stdout.strip()

    def commit(self, content: str, at: str) -> str:
        _ = (self.work / "payload.txt").write_text(content + "\n")
        return commit_repository(self.work, content, env={**self.env, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at})

    def launch(self, *, session: Path | None = None) -> subprocess.CompletedProcess[str]:
        arguments = ["--session-dir", str(session or self.session), "--cwd", str(self.work)]
        if process_boundary_for_case(self):
            return subprocess.run(["bash", str(ROOT / "scripts/direct/assign.sh"), "--role", "simplify", *arguments],
                                  env=self.env, capture_output=True, text=True, check=False,
                                  timeout=TEST_HANG_GUARD_SECS)
        with (patch.object(simplify_daily, "datetime", ReviewTime),
              patch.object(simplify_daily, "AGENT_EXEC", self.agent_exec)):
            return invoke_cli(simplify_daily, lambda: simplify_daily.main(arguments), arguments,
                              {**self.env, "MODELS_CONFIG_TIER_ONLY": "1"}, Path.cwd())

    def reviews(self) -> list[dict[str, object]]:
        return ([cast(dict[str, object], json.loads(line)) for line in self.calls.read_text().splitlines()]
                if self.calls.exists() else [])

    def refs(self) -> dict[str, str]:
        rows = self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/simplify", repository=self.origin)
        return {ref: value for ref, separator, value in (row.partition(" ") for row in rows.splitlines()) if separator}

    def artifacts(self, day: str = "2026-11-05", *, session: Path | None = None) -> Path:
        return (session or self.session) / "simplify" / day

    def test_off_starts_no_claude_and_does_not_read_origin(self) -> None:
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "direct.conf", "SIMPLIFY_LENS=off\n")
        _ = self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        result = self.launch()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "simplify: off"), result.stderr)
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.refs(), {})

    @process_boundary
    def test_first_run_reviews_the_diff_from_the_commit_before_the_cutoff(self) -> None:
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        files = self.artifacts()
        self.assertEqual((files / "diff").read_text().strip(), self.git("diff", self.before, self.tip))
        source = (files / "source_prompt.md").read_text()
        for value in (str(self.origin), self.before, self.tip, "everything that has changed since the last simplify run"):
            self.assertIn(value, source)
        prompt = (files / "prompt.md").read_text()
        self.assertTrue(prompt.startswith("/simplify " + str(files / "diff")))
        self.assertEqual(self.reviews()[0]["prompt"], prompt)
        self.assertEqual((files / "report.txt").read_text().strip(), "Pass")
        self.assertEqual((files / "status").read_text().strip(), "reviewed")
        self.assertIn('"type": "result"', (files / "agent.log").read_text())
        self.assertIn(f"simplify: reviewed; report {files / 'report.txt'}", result.stdout)
        self.assertEqual(self.refs()["refs/simplify/claim/" + self.day], self.tip)

    def test_first_run_uses_the_root_when_no_commit_precedes_the_cutoff(self) -> None:
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "direct.conf", "SIMPLIFY_FIRST_RUN_BEFORE=2000-01-01T00:00:00+00:00\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.artifacts() / "diff").read_text().strip(), self.git("diff", self.first, self.tip))

    def test_later_run_uses_the_last_ref_and_the_origin_default_branch_tip(self) -> None:
        _ = self.git("update-ref", "refs/simplify/last", self.first, repository=self.origin)
        _ = self.git("checkout", "-b", "unit-work")
        local_tip = self.commit("unpublished unit work", "2026-11-05T23:00:00-05:00")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.artifacts() / "diff").read_text().strip(), self.git("diff", self.first, self.tip))
        self.assertEqual(self.reviews()[0]["head"], self.tip)
        self.assertEqual(self.git("rev-parse", "HEAD"), local_tip)
        self.assertEqual(self.refs()["refs/simplify/last"], self.tip)

    def test_empty_diff_starts_no_claude_and_makes_no_claim(self) -> None:
        _ = self.git("update-ref", "refs/simplify/last", self.tip, repository=self.origin)
        before = self.refs()
        result = self.launch()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "simplify: no changes"), result.stderr)
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.refs(), before)

    def test_second_unit_on_the_same_day_starts_no_claude(self) -> None:
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        changed = self.commit("later origin work", "2026-11-05T23:05:00-05:00")
        _ = self.git("push", "origin", "trunk")
        second = self.launch(session=self.root / "second-unit")
        self.assertEqual((second.returncode, second.stdout.strip()), (0, "simplify: claimed today"), second.stderr)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.refs()["refs/simplify/last"], self.tip)
        self.assertNotEqual(changed, self.tip)

    def test_an_earlier_claim_is_removed_after_the_new_day_is_claimed(self) -> None:
        old = "refs/simplify/claim/2026-11-04"
        _ = self.git("update-ref", old, self.before, repository=self.origin)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.reviews()), 1)
        self.assertNotIn(old, self.refs())
        self.assertEqual(self.refs()["refs/simplify/claim/" + self.day], self.tip)

    def test_a_failed_review_keeps_the_base_and_the_next_day_includes_its_changes(self) -> None:
        _ = self.git("update-ref", "refs/simplify/last", self.before, repository=self.origin)
        self.env["SIMPLIFY_TEST_ERROR"] = "1"
        failed = self.launch()
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("Provider refused the review", failed.stderr)
        self.assertEqual((self.artifacts() / "status").read_text().strip(), "error")
        self.assertEqual(self.refs()["refs/simplify/last"], self.before)
        repeated = self.launch(session=self.root / "same-day-retry")
        self.assertEqual((repeated.returncode, repeated.stdout.strip()), (0, "simplify: claimed today"), repeated.stderr)
        self.assertEqual(len(self.reviews()), 1)
        del self.env["SIMPLIFY_TEST_ERROR"]
        self.env["SIMPLIFY_TEST_NOW"] = "2026-11-07T03:30:00+00:00"
        changed = self.commit("next day work", "2026-11-06T22:00:00-05:00")
        _ = self.git("push", "origin", "trunk")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        files = self.artifacts("2026-11-06")
        self.assertEqual((files / "diff").read_text().strip(), self.git("diff", self.before, changed))
        self.assertEqual(self.refs()["refs/simplify/last"], changed)
        self.assertEqual(len(self.reviews()), 2)

    def test_claude_uses_the_callers_account_and_a_detached_clone_at_tip(self) -> None:
        _ = (self.work / "payload.txt").write_text("uncommitted unit work\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call = self.reviews()[0]
        self.assertEqual(call["config_dir"], self.env["CLAUDE_CONFIG_DIR"])
        self.assertEqual((call["head"], call["branch"], call["content"]), (self.tip, "", "origin tip\n"))
        clone = Path(cast(str, call["cwd"]))
        self.assertNotEqual(clone, self.work)
        self.assertFalse(clone.exists(), "The launcher must remove only its own clone")
        self.assertEqual((self.work / "payload.txt").read_text(), "uncommitted unit work\n")
        rows = [cast(dict[str, object], json.loads(line)) for line in
                (Path(self.env["USAGE_STATE_DIR"]) / "agents.jsonl").read_text().splitlines()]
        self.assertEqual([(row["id"], row["role"], row["lens"]) for row in rows],
                         [(call["session_id"], "review", "simplify")])

    def test_a_failed_assignment_append_does_not_stop_the_review(self) -> None:
        (Path(self.env["USAGE_STATE_DIR"]) / "agents.jsonl").mkdir(parents=True)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual((self.artifacts() / "status").read_text().strip(), "reviewed")
        self.assertEqual(self.refs()["refs/simplify/last"], self.tip)
        self.assertEqual(result.stderr.count("simplify: could not record assignment:"), 1)

    def test_a_non_utf8_diff_is_reviewed_and_advances_the_last_ref(self) -> None:
        _ = (self.work / "latin1.txt").write_bytes(b"caf\xe9\n")
        changed = commit_repository(self.work, "Add Latin-1 text", env={
            **self.env, "GIT_AUTHOR_DATE": "2026-11-05T23:00:00-05:00",
            "GIT_COMMITTER_DATE": "2026-11-05T23:00:00-05:00",
        })
        _ = self.git("push", "origin", "trunk")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("+caf\ufffd", (self.artifacts() / "diff").read_text())
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual((self.artifacts() / "status").read_text().strip(), "reviewed")
        self.assertEqual(self.refs()["refs/simplify/last"], changed)

    def test_an_invalid_standing_prompt_records_error_before_claude_starts(self) -> None:
        standing = Path(self.env["CONF_DIR"]) / "review/simplify.md"
        standing.parent.mkdir(parents=True, exist_ok=True)
        _ = standing.write_text("Review $missing.\n")
        _ = self.git("update-ref", "refs/simplify/last", self.before, repository=self.origin)
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.artifacts() / "status").read_text(), "error\n")
        self.assertTrue((self.artifacts() / "report.txt").read_text().startswith("ERROR:"))
        self.assertIn("missing", result.stderr)
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.refs()["refs/simplify/last"], self.before)

    @process_boundary
    def test_a_failed_reviewed_status_write_keeps_the_last_ref(self) -> None:
        self.env["SIMPLIFY_TEST_STATUS_FAILURE"] = "1"
        _ = self.git("update-ref", "refs/simplify/last", self.before, repository=self.origin)
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("reviewed status refused", result.stderr)
        self.assertEqual((self.artifacts() / "status").read_text(), "error\n")
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.refs()["refs/simplify/last"], self.before)

    def test_a_failed_last_ref_push_records_error_after_a_clean_review(self) -> None:
        _ = self.git("update-ref", "refs/simplify/last", self.before, repository=self.origin)
        hook = self.origin / "hooks/pre-receive"
        hook.parent.mkdir()
        _ = hook.write_text("""#!/bin/sh
while read old new ref; do
  if [ "$ref" = refs/simplify/last ]; then
    echo 'last ref update refused' >&2
    exit 1
  fi
done
""")
        hook.chmod(0o755)
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("last ref update refused", result.stderr)
        self.assertEqual((self.artifacts() / "status").read_text(), "error\n")
        self.assertTrue((self.artifacts() / "report.txt").read_text().startswith("ERROR:"))
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.refs()["refs/simplify/last"], self.before)

    def test_a_push_failure_other_than_a_lease_rejection_starts_no_claude(self) -> None:
        hook = self.origin / "hooks/pre-receive"
        hook.parent.mkdir()
        _ = hook.write_text("#!/bin/sh\necho 'origin maintenance rejects this push' >&2\nexit 1\n")
        hook.chmod(0o755)
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("origin maintenance rejects this push", result.stderr)
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.refs(), {})


if __name__ == "__main__":
    _ = unittest.main()
