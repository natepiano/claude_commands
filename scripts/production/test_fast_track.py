"""Run fast-track commands against private Git origins and checked tool replies."""

from __future__ import annotations

from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
from typing import cast, final, override
import unittest
from unittest.mock import patch

from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture
from scripts.production import fast_track
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case


REPOSITORY = Path(__file__).resolve().parents[2]
COMMAND = REPOSITORY / "scripts/production/fast_track.py"

MODULE = '''import json
import os
from pathlib import Path

VALUE = "before"
if os.environ.get("FAST_TRACK_TEST_EVENTS"):
    with Path(os.environ["FAST_TRACK_TEST_EVENTS"]).open("a") as output:
        output.write(json.dumps({"kind": "import", "cwd": str(Path.cwd()),
                                 "value": VALUE}) + "\\n")
'''

DIRECT_TEST = '''import json
import os
from pathlib import Path
import subprocess
import unittest
from scripts.sample import item

class ChangedValueTests(unittest.TestCase):
    def test_changed_value(self):
        self.assertEqual(item.VALUE, "after")
        self.assertEqual(os.environ["HERDR_BIN"], "/nonexistent/herdr")
        for name in ("PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY", "STAFF_OPERATION_FD"):
            self.assertNotIn(name, os.environ)
        self.assertIn("BUILDLOG_DIR", os.environ)
        with Path(os.environ["FAST_TRACK_TEST_EVENTS"]).open("a") as output:
            output.write(json.dumps({"kind": "test", "module": __name__.rsplit(".", 1)[-1],
                                     "package_module": __name__,
                                     "cwd": str(Path.cwd()), "private_git": (Path.cwd() / ".git").is_dir(),
                                     "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True, timeout=__HANG_GUARD__).strip(),
                                     "common": subprocess.check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True, timeout=__HANG_GUARD__).strip()}) + "\\n")
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))

TYPE_TOOL = '''#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

with Path(os.environ["FAST_TRACK_TEST_LOCK"]).open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = False
    except BlockingIOError:
        held = True
with Path(os.environ["FAST_TRACK_TEST_EVENTS"]).open("a") as output:
    output.write(json.dumps({"kind": "types", "args": sys.argv[1:],
                             "cwd": str(Path.cwd()), "lock_held": held}) + "\\n")
control = Path(os.environ["FAST_TRACK_TEST_CONTROL"])
move = control / "move-origin"
if move.exists():
    move.unlink()
    subprocess.run(["git", "-C", str(control / "writer"), "push", "origin", "HEAD:main"], check=True, timeout=__HANG_GUARD__)
edit = control / "index-edit.json"
if edit.exists():
    change = json.loads(edit.read_text())
    edit.unlink()
    checkout = Path(change["repo"])
    target = checkout / change["path"]
    published = target.read_bytes()
    target.write_text(change["content"])
    subprocess.run(["git", "-C", str(checkout), "add", "--", change["path"]], check=True, timeout=__HANG_GUARD__)
    target.write_bytes(published)
    subprocess.run(["git", "-C", str(checkout), "gc", "--prune=now"], check=True, timeout=__HANG_GUARD__)
if (control / "bad-types").exists():
    print("0 errors, 1 warning, 0 notes")
    sys.exit(0)
print("0 errors, 0 warnings, 0 notes")
# The project accepts the footer, including when the configured venv is absent.
sys.exit(3)
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))


GIT_ARGUMENTS = '''operation=
destination=
checkout=
previous=
for argument do
    if [ -z "$operation" ]; then
        case "$previous" in
            -C) checkout=$argument; previous=; continue ;;
            -c) previous=; continue ;;
        esac
        case "$argument" in
            -C|-c) previous=$argument ;;
            -*) ;;
            *) operation=$argument ;;
        esac
    elif [ "$argument" = HEAD:main ]; then
        destination=main
    fi
done
'''


INTERRUPTED_GIT = '''#!/bin/sh
# Use the real Git process unless this command needs an interruption event.
''' + GIT_ARGUMENTS + '''
point=
marker="$FAST_TRACK_TEST_CONTROL/stop-git"
if [ -f "$marker" ]; then IFS= read -r point < "$marker" || :; fi
stop=
case "$point:$operation:$destination" in
    *-push:push:main|*-restore:restore:*) stop=yes ;;
esac
if [ -n "$stop" ]; then
    rm "$marker"
    printf '%s' "$checkout" > "$FAST_TRACK_TEST_CONTROL/stopped-copy"
    case "$point" in
        before-*) kill -KILL "$PPID"; exit 0 ;;
    esac
fi
ready=${FAST_TRACK_PAUSE_COMMIT:-}
pause=
if [ "$operation" = commit ] && [ -n "$ready" ] && [ ! -f "$ready" ]; then pause=yes; fi
set -- "$FAST_TRACK_REAL_GIT" "$@"
if [ -x "$FAST_TRACK_BOUND_GIT" ]; then set -- "$FAST_TRACK_BOUND_GIT" "$@"; fi
if [ -z "$stop$pause" ]; then
    exec "$@"
fi
"$@" || exit
if [ -n "$pause" ]; then
    : > "$ready"
    cat "$FAST_TRACK_COMMIT_RELEASE" > /dev/null
fi
if [ -n "$stop" ]; then kill -KILL "$PPID"; fi
'''


FETCH_RACE_GIT = '''#!/bin/sh
''' + GIT_ARGUMENTS + '''
if { [ "$checkout" = "$FAST_TRACK_FETCH_DIRECTOR" ] && [ "$operation" = fetch ] && [ "$#" -eq 5 ] && [ "$4" = origin ] && [ "$5" = main ]; } ||
   { [ "$operation" = push ] && [ "$destination" = main ]; }; then
    exec "$FAST_TRACK_FETCH_LOGIC" "$@"
fi
if [ -x "$FAST_TRACK_BOUND_GIT" ]; then
    exec "$FAST_TRACK_BOUND_GIT" "$FAST_TRACK_REAL_GIT" "$@"
fi
exec "$FAST_TRACK_REAL_GIT" "$@"
'''


FETCH_RACE_LOGIC = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
git = [os.environ["FAST_TRACK_REAL_GIT"]]
if Path(os.environ["FAST_TRACK_BOUND_GIT"]).is_file():
    git.insert(0, os.environ["FAST_TRACK_BOUND_GIT"])
control = Path(os.environ["FAST_TRACK_TEST_CONTROL"])
director = Path(os.environ["FAST_TRACK_FETCH_DIRECTOR"])
lock = Path(os.environ["FAST_TRACK_FETCH_LOCK"])
plan = json.loads((control / "fetch-plan.json").read_text())
armed = control / "fetch-armed"
events = control / "fetch-events.jsonl"
in_director = args[:2] == ["-C", str(director)]
is_fetch = in_director and args[2:] == ["fetch", "origin", "main"]
if is_fetch and (armed.exists() or plan["point"] == "before-push"):
    rows = events.read_text().splitlines() if events.exists() else []
    failure = plan["failures"][len(rows)] if len(rows) < len(plan["failures"]) else ""
    if failure == "lock":
        if plan["point"] == "before-push" and not rows:
            subprocess.run([*git, "-C", str(director),
                            "update-ref", "refs/remotes/origin/main", "HEAD^"],
                           check=True, timeout=__HANG_GUARD__)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
    if failure == "other":
        result = subprocess.CompletedProcess(args, 128, "", "fatal: fixture fetch transport failed\\n")
    else:
        result = subprocess.run([*git, *args],
                                capture_output=True, text=True, check=False, timeout=__HANG_GUARD__)
    with events.open("a") as output:
        output.write(json.dumps({"returncode": result.returncode, "stderr": result.stderr}) + "\\n")
    if failure == "lock" and not plan["persistent"]:
        lock.unlink()
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
result = subprocess.run([*git, *args], check=False, timeout=__HANG_GUARD__)
if "push" in args and "HEAD:main" in args and result.returncode == 0:
    armed.touch()
sys.exit(result.returncode)
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))


@final
class FastTrackTests(MergeCheckpointFixture):
    production_name: str = "example"

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.config: Path = Path()
        self.records_dir: Path = Path()
        self.events_path: Path = Path()
        self.control: Path = Path()
        self.settings: dict[str, object] = {}
        self.initial: str = ""

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        for path, content in {
            "scripts/__init__.py": "", "scripts/sample/__init__.py": "",
            "scripts/lib/py": (REPOSITORY / "scripts/lib/py").read_text(),
            "scripts/sample/item.py": MODULE,
            "scripts/sample/note.txt": "before\n",
            "scripts/production/add_unit.py": "VALUE = 'staff writer'\n",
            "scripts/sample/bridge.py": "from scripts.production.add_unit import VALUE\n",
            **{f"folder/file{number}.txt": "before\n" for number in range(6)},
            "scripts/sample/test_item.py": DIRECT_TEST,
            "scripts/sample/test_item_extra.py": DIRECT_TEST,
            "scripts/sample/test_unrelated.py": "raise AssertionError('full suite ran')\n",
            "scripts/production/merge_gate.py": "raise AssertionError('gate ran')\n",
        }.items():
            self.write(self.checkout, path, content)
        (self.checkout / "scripts/lib/py").chmod(0o755)
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "fast-track fixture modules")
        _ = self.git("push", "origin", "HEAD:main", "HEAD:production")
        _ = self.git("reset", "--hard", "production", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "production", cwd=self.promote)

    @override
    def setUp(self) -> None:
        super().setUp()
        # SIGKILL bypasses the child context, so the test owns its temporary parent.
        self.env["TMPDIR"] = self.enterContext(tempfile.TemporaryDirectory())
        self.config = self.root / "config" / "fast_track.conf"
        self.config.parent.mkdir()
        self.env.update(config_environment(self.config.parent))
        self.records_dir = self.root / "fast-track-state"
        self.events_path = self.root / "fast-track-events.jsonl"
        self.control = self.root / "fast-track-control"
        self.control.mkdir()
        bounded_git = self.bin / "bounded-git"
        self.env["FAST_TRACK_BOUND_GIT"] = str(bounded_git)
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.unit))
        self.env.update({
            "FAST_TRACK_STATE_DIR": str(self.records_dir),
            "FAST_TRACK_TEST_EVENTS": str(self.events_path), "FAST_TRACK_TEST_CONTROL": str(self.control),
            "FAST_TRACK_TEST_LOCK": str(common / "main-push.lock"),
        })
        with patch.dict(os.environ, {"CONF_DIR": str(REPOSITORY / "config")}):
            settings_read = conf.read("fast_track")
            self.settings = {
                "max_changed_lines": settings_read.whole("max_changed_lines"),
                "max_files": settings_read.whole("max_files"),
                "check_time_limit_seconds": settings_read.number("check_time_limit_seconds"),
                "max_push_attempts": settings_read.whole("max_push_attempts"),
                "fetch_lock_attempts": settings_read.whole("fetch_lock_attempts"),
                "fetch_lock_wait_seconds": settings_read.number("fetch_lock_wait_seconds"),
                "refused_paths": list(settings_read.words("refused_paths")),
            }
        self.settings.update(max_changed_lines=100, max_files=10, check_time_limit_seconds=30,
                             max_push_attempts=2, fetch_lock_attempts=3, fetch_lock_wait_seconds=0.01)
        self.save_config()
        self.executable(self.bin / "basedpyright", TYPE_TOOL)
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        self.initial = self.git("rev-parse", "refs/heads/main", cwd=self.origin)

    def save_config(self) -> None:
        lines: list[str] = []
        for line in (REPOSITORY / "config/fast_track.conf").read_text().splitlines():
            key, separator = line.partition("=")[:2]
            if not separator:
                lines.append(line)
            elif key.strip() in self.settings:
                value = self.settings[key.strip()]
                rendered = (" ".join(cast(list[str], value))
                            if isinstance(value, list) else
                            str(value) if key.strip() == "refused_paths" else json.dumps(value))
                lines.append(f"{key.strip()} = {rendered}")
            elif lines and lines[-1].lstrip().startswith(("# whole:", "# range:", "# one of:")):
                _ = lines.pop()
        _ = self.config.write_text("\n".join(lines) + "\n")

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        # The interruption wrapper must kill a child process.
        if (self.control / "stop-git").exists() or process_boundary_for_case(self):
            return subprocess.run([sys.executable, str(COMMAND), *arguments], cwd=self.unit,
                                  env=self.env, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        args = list(arguments)
        with LauncherHangGuard("fast-track CLI").protect():
            return invoke_cli(fast_track, lambda: fast_track.main(args), args, self.env, self.unit)

    def fast_track(self, *paths: str) -> subprocess.CompletedProcess[str]:
        return self.command("--unit", "alpha", "--production", self.production_name,
                            "--why", "repair the sample", *(paths or ("scripts/sample/item.py",)))

    def change_module(self) -> None:
        self.write(self.unit, "scripts/sample/item.py", MODULE.replace('VALUE = "before"', 'VALUE = "after"'))

    def observations(self, kind: str) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        return [row for line in self.events_path.read_text().splitlines()
                if (row := cast(dict[str, object], json.loads(line)))["kind"] == kind]

    def records(self) -> list[dict[str, object]]:
        path = self.records_dir / "records.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_refused(self, result: subprocess.CompletedProcess[str], detail: str = "") -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(detail, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), self.initial)
        self.assertEqual(self.records(), [])

    def move_main_during_check(self) -> str:
        writer = self.control / "writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "maintenance.auto", "false", cwd=writer)
        _ = self.git("config", "user.name", "Concurrent Writer", cwd=writer)
        _ = self.git("config", "user.email", "writer@example.invalid", cwd=writer)
        self.write(writer, "concurrent.txt", "another fix\n")
        _ = self.git("add", ".", cwd=writer)
        _ = self.git("commit", "-m", "concurrent fix", cwd=writer)
        _ = (self.control / "move-origin").touch()
        return self.git("rev-parse", "HEAD", cwd=writer)

    def interrupt_git(self, point: str) -> None:
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        self.env["FAST_TRACK_REAL_GIT"] = str(real_git)
        self.executable(self.bin / "git", INTERRUPTED_GIT)
        _ = (self.control / "stop-git").write_text(point)

    def arrange_fetch_failures(self, point: str, failures: list[str], *, persistent: bool = False) -> Path:
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.unit))
        lock = common / "refs/remotes/origin/main.lock"
        self.env.update(FAST_TRACK_REAL_GIT=str(real_git), FAST_TRACK_FETCH_DIRECTOR=str(self.unit),
                        FAST_TRACK_FETCH_LOCK=str(lock))
        _ = (self.control / "fetch-plan.json").write_text(json.dumps({
            "point": point, "failures": failures, "persistent": persistent,
        }))
        logic = self.bin / "fetch-race.py"
        self.executable(logic, FETCH_RACE_LOGIC)
        self.env["FAST_TRACK_FETCH_LOGIC"] = str(logic)
        self.executable(self.bin / "git", FETCH_RACE_GIT)
        return lock

    def director_fetches(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line))
                for line in (self.control / "fetch-events.jsonl").read_text().splitlines()]

    def test_fetch_wrapper_skips_python_logic_for_ordinary_git_commands(self) -> None:
        _ = self.arrange_fetch_failures("after-push", ["lock"])
        self.env["FAST_TRACK_FETCH_LOGIC"] = str(self.root / "missing-fetch-logic")
        _ = (self.control / "fetch-plan.json").write_text("invalid fetch plan")
        result = subprocess.run([str(self.bin / "git"), "-C", str(self.unit), "rev-parse", "HEAD"],
                                env=self.env, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), self.initial)
        self.assertFalse((self.control / "fetch-events.jsonl").exists())

    def test_post_push_ref_lock_retries_then_restores_and_promotes(self) -> None:
        self.change_module()
        lock = self.arrange_fetch_failures("after-push", ["lock"])
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetches = self.director_fetches()
        self.assertEqual(len(fetches), 2, fetches)
        self.assertNotEqual(fetches[0]["returncode"], 0)
        self.assertIn("cannot lock ref", str(fetches[0]["stderr"]))
        self.assertEqual(fetches[1]["returncode"], 0)
        self.assertFalse(lock.exists())
        tip = self.git("rev-parse", "main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(len(self.events("ssh")), 1)
        row, = self.records()
        self.assertEqual(row["commit"], tip)

    def test_pre_push_ref_lock_retries_before_the_patch_is_published(self) -> None:
        self.change_module()
        _ = self.arrange_fetch_failures("before-push", ["lock"])
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetches = self.director_fetches()
        self.assertIn("cannot lock ref", str(fetches[0]["stderr"]))
        self.assertEqual(fetches[1]["returncode"], 0)
        tip = self.git("rev-parse", "main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)

    def test_persistent_post_push_ref_lock_stops_at_the_fetch_attempt_limit(self) -> None:
        self.change_module()
        self.settings["fetch_lock_attempts"] = 2
        self.save_config()
        lock = self.arrange_fetch_failures("after-push", ["lock", "lock"], persistent=True)
        result = self.fast_track()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        fetches = self.director_fetches()
        self.assertEqual(len(fetches), 2, fetches)
        self.assertTrue(all(row["returncode"] != 0 and "cannot lock ref" in str(row["stderr"])
                            for row in fetches), fetches)
        self.assertIn(str(fetches[-1]["stderr"]).strip(), result.stderr)
        self.assertTrue(lock.exists())
        self.assertNotEqual(self.git("rev-parse", "main", cwd=self.origin), self.initial)
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), self.initial)
        self.assertEqual(self.events("ssh"), [])

    def test_transport_failure_after_a_ref_lock_is_not_retried(self) -> None:
        self.change_module()
        _ = self.arrange_fetch_failures("after-push", ["lock", "other"])
        result = self.fast_track()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        fetches = self.director_fetches()
        self.assertEqual(len(fetches), 2, fetches)
        self.assertIn("cannot lock ref", str(fetches[0]["stderr"]))
        self.assertIn("fatal: fixture fetch transport failed", result.stderr)
        self.assertNotIn("cannot lock ref", result.stderr)
        self.assertNotEqual(self.git("rev-parse", "main", cwd=self.origin), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), self.initial)

    def test_pre_push_transport_failure_refuses_without_retry_or_publication(self) -> None:
        self.change_module()
        _ = self.arrange_fetch_failures("before-push", ["other"])
        result = self.fast_track()
        self.assert_refused(result, "fatal: fixture fetch transport failed")
        self.assertEqual(len(self.director_fetches()), 1)
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())

    def test_fetch_retry_keys_missing_or_invalid_refuse_before_publication(self) -> None:
        self.change_module()
        valid = self.settings.copy()
        for key, invalid in {
            "fetch_lock_attempts": [0, -1, 1.5, "many", True],
            "fetch_lock_wait_seconds": [-1, "soon", True, "nan", "inf"],
        }.items():
            for value in [None, *invalid]:
                with self.subTest(key=key, value=value):
                    self.settings = valid.copy()
                    if value is None:
                        del self.settings[key]
                    else:
                        self.settings[key] = value
                    self.save_config()
                    self.assert_refused(self.fast_track(), key)
        self.assertEqual(self.observations("types"), [])

    def retry_interrupted_restore(self, point: str) -> None:
        self.change_module()
        self.write(self.unit, "scripts/sample/note.txt", "after\n")
        self.interrupt_git(point)
        stopped = self.fast_track("scripts/sample/item.py", "scripts/sample/note.txt")
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(),
                         MODULE if point == "after-restore" else MODULE.replace('VALUE = "before"', 'VALUE = "after"'))
        self.assertEqual((self.unit / "scripts/sample/note.txt").read_text(), "after\n")
        retry = self.fast_track("scripts/sample/item.py", "scripts/sample/note.txt")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn(f"already on main at {tip[:7]}", retry.stdout)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual((self.unit / "scripts/sample/note.txt").read_text(), "before\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.observations("types")), 1)

    def test_deleted_python_module_still_runs_its_direct_importing_tests(self) -> None:
        (self.unit / "scripts/sample/item.py").unlink()
        result = self.fast_track()
        self.assert_refused(result, "test_item")
        self.assertIn("ImportError", result.stdout)
        self.assertEqual(self.observations("types"), [])
        self.assertFalse((self.unit / "scripts/sample/item.py").exists())

    def test_replacing_a_directory_counts_every_deleted_and_added_path(self) -> None:
        shutil.rmtree(self.unit / "folder")
        self.write(self.unit, "folder", "replacement\n")
        self.settings["max_files"] = 5
        self.save_config()
        self.assert_refused(self.fast_track("folder"), "max_files")
        self.assertEqual(self.observations("types"), [])

    def test_directory_replacement_restores_the_tracked_children_after_push(self) -> None:
        shutil.rmtree(self.unit / "folder")
        self.write(self.unit, "folder", "replacement\n")
        result = self.fast_track("folder")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertEqual(self.git("show", f"{tip}:folder", cwd=self.origin), "replacement")
        self.assertTrue((self.unit / "folder").is_dir())
        self.assertEqual({p.name: p.read_text() for p in (self.unit / "folder").iterdir()},
                         {f"file{number}.txt": "before\n" for number in range(6)})
        row, = self.records()
        self.assertEqual(len(cast(list[str], row["paths"])), 7)

    def test_replacing_a_directory_checks_refusals_for_its_deleted_children(self) -> None:
        shutil.rmtree(self.unit / "folder")
        self.write(self.unit, "folder", "replacement\n")
        self.settings["refused_paths"] = [*cast(list[str], self.settings["refused_paths"]), "folder/file0.txt"]
        self.save_config()
        self.assert_refused(self.fast_track("folder"), "folder/file0.txt")

    def test_python_imports_of_refused_repo_modules_are_refused_transitively(self) -> None:
        for source in ("from scripts.production.add_unit import VALUE\n",
                       "from .bridge import VALUE\n",
                       "from scripts.production import add_unit\n",
                       "import scripts.production.add_unit\n"):
            with self.subTest(source=source):
                self.write(self.unit, "scripts/sample/entry.py", source)
                self.assert_refused(self.fast_track("scripts/sample/entry.py"), "scripts/production/add_unit.py")
        self.assertEqual(self.observations("types"), [])

    def test_agents_cannot_bypass_the_staff_writer_refusal(self) -> None:
        path = "scripts/agents/shutdown.py"
        self.write(self.unit, path, "from add_unit import VALUE\n")
        self.assert_refused(self.fast_track(path), "refused path")

    def test_identical_patches_from_two_units_keep_separate_recovery_records(self) -> None:
        self.change_module()
        self.interrupt_git("after-push")
        ready = self.control / "beta-committed"
        release = self.control / "beta-release"
        os.mkfifo(release)
        environment = {**self.env, "FAST_TRACK_PAUSE_COMMIT": str(ready),
                       "FAST_TRACK_COMMIT_RELEASE": str(release)}
        with subprocess.Popen([sys.executable, str(COMMAND), "--unit", "beta",
                               "--production", self.production_name, "--why", "repair the sample",
                               "scripts/sample/item.py"], cwd=self.unit, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as beta:
            try:
                wait_until(ready.exists, self.control)
                self.assertTrue(ready.exists(), "B never prepared its candidate on the original base")
                stopped = self.fast_track()
                self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
                tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
                self.assertEqual(self.records(), [])
                with LauncherHangGuard("release prepared candidate").protect():
                    with release.open("w") as output:
                        _ = output.write("continue\n")
                stdout, stderr = beta.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(beta.returncode, 2, stdout + stderr)
            finally:
                try:
                    with os.fdopen(os.open(release, os.O_WRONLY | os.O_NONBLOCK), "w") as output:
                        _ = output.write("continue\n")
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                if beta.poll() is None:
                    beta.kill()
                    _ = beta.communicate(timeout=TEST_HANG_GUARD_SECS)
        retry = self.fast_track()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn("already on main", retry.stdout)
        row, = self.records()
        self.assertEqual((row["unit"], row["commit"]), ("alpha", tip))
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)

    def test_pending_finds_a_pushed_fix_without_a_fast_track_rerun(self) -> None:
        self.change_module()
        self.interrupt_git("after-push")
        stopped = self.fast_track()
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertEqual(self.records(), [])
        # Review discovery must not need a safe restore or a successful promotion.
        self.write(self.unit, "scripts/sample/item.py", "later local edit\n")
        _ = (self.state / "mac-rc").write_text("1\n")
        pending = self.command("--pending", "--unit", "alpha")
        self.assertEqual(pending.returncode, 0, pending.stdout + pending.stderr)
        self.assertIn(tip, pending.stdout)
        self.assertIn('VALUE = "after"', pending.stdout)
        row, = self.records()
        self.assertEqual((row["commit"], row["review"]), (tip, "pending"))
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), "later local edit\n")
        again = self.command("--pending", "--unit", "alpha")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.records(), [row])

    def interrupted_module_push(self) -> tuple[str, Path]:
        self.change_module()
        return self.interrupted_push("scripts/sample/item.py")

    def interrupted_push(self, *paths: str) -> tuple[str, Path]:
        self.interrupt_git("after-push")
        stopped = self.fast_track(*paths)
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        attempt, = (self.records_dir / "attempts").glob("*.json")
        return tip, attempt

    def attempt_state(self, path: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads(path.read_text()))

    def integrate_interrupted_push(self) -> tuple[str, Path]:
        tip, attempt = self.interrupted_module_push()
        _ = self.git("restore", "--staged", "--worktree", "scripts/sample/item.py", cwd=self.unit)
        _ = self.git("fetch", "origin", "main", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "origin/main", cwd=self.unit)
        return tip, attempt

    def test_merged_interrupted_push_closes_restore_before_an_unrelated_fix(self) -> None:
        tip, attempt = self.integrate_interrupted_push()
        self.write(self.unit, "scripts/sample/note.txt", "new note\n")
        result = self.fast_track("scripts/sample/note.txt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attempt_state(attempt)["restore_closed"], "ancestry")
        self.assertTrue(self.attempt_state(attempt)["restored"])
        self.assertEqual(self.git("merge-base", "--is-ancestor", tip, "refs/heads/main", cwd=self.origin), "")
        self.assertEqual(self.git("show", "main:scripts/sample/note.txt", cwd=self.origin), "new note")

    def test_new_edit_after_integrating_a_push_is_published_instead_of_reported_recovered(self) -> None:
        tip, attempt = self.integrate_interrupted_push()
        later = (self.unit / "scripts/sample/item.py").read_text() + "# another fix\n"
        self.write(self.unit, "scripts/sample/item.py", later)
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("already on main", result.stdout)
        self.assertNotEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        self.assertEqual(self.git("show", "main:scripts/sample/item.py", cwd=self.origin), later.strip())
        self.assertEqual(self.attempt_state(attempt)["restore_closed"], "ancestry")

    def assert_unrelated_attempt_does_not_block(self, *, missing_production: bool) -> None:
        tip, attempt = self.interrupted_module_push()
        if missing_production:
            valid_doc = self.root / "valid-production.md"
            _ = valid_doc.write_text(self.doc.read_text())
            self.doc.unlink()
            self.doc = valid_doc
            self.production_name = "valid-production"
            from scripts.production import staff
            from scripts.production.staff_test_support import fixture_member, isolated_staff_file
            with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
                _ = fixture_member("showrunner", name=self.production_name, doc=staff.LiveDoc("natedev", valid_doc))
        else:
            _ = self.git("restore", "--staged", "--worktree", "scripts/sample/item.py", cwd=self.unit)
            self.write(self.unit, "local.txt", "unrelated local commit\n")
            _ = self.git("add", "local.txt", cwd=self.unit)
            _ = self.git("commit", "-m", "advance without main", cwd=self.unit)
        self.write(self.unit, "scripts/sample/note.txt", "new note\n")
        result = self.fast_track("scripts/sample/note.txt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn(attempt.stem, result.stderr)
        self.assertIn("No such file" if missing_production else "overlapping request", result.stderr)
        self.assertEqual(self.attempt_state(attempt)["promoted"], not missing_production)
        self.assertFalse(self.attempt_state(attempt)["restored"])
        self.assertEqual(self.git("show", "main:scripts/sample/note.txt", cwd=self.origin), "new note")
        self.assertEqual({str(row["commit"]) for row in self.records()},
                         {tip, self.git("rev-parse", "main", cwd=self.origin)})

    def test_unrelated_stuck_restore_does_not_block_a_new_fix(self) -> None:
        self.assert_unrelated_attempt_does_not_block(missing_production=False)

    def test_unrelated_missing_production_does_not_block_a_new_fix(self) -> None:
        self.assert_unrelated_attempt_does_not_block(missing_production=True)

    def close_restore(self, attempt: Path) -> subprocess.CompletedProcess[str]:
        return self.command("--unit", "alpha", "--close-restore", attempt.stem)

    def test_overlapping_unsafe_restore_names_an_idempotent_close_that_preserves_edits(self) -> None:
        tip, attempt = self.interrupted_module_push()
        later = (self.unit / "scripts/sample/item.py").read_text() + "# later edit\n"
        self.write(self.unit, "scripts/sample/item.py", later)
        _ = self.git("add", "scripts/sample/item.py", cwd=self.unit)
        index = self.git("write-tree", cwd=self.unit)
        # The directory selection overlaps the recorded file selection.
        refused = self.fast_track("scripts/sample")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"--close-restore {attempt.stem}", refused.stderr)
        self.assertIn("kept worktree edits", refused.stderr)
        closed = self.close_restore(attempt)
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), later)
        self.assertEqual(self.git("write-tree", cwd=self.unit), index)
        self.assertEqual(self.attempt_state(attempt)["restore_closed"], "explicit")
        self.assertEqual(self.attempt_state(attempt)["publication"], "published")
        self.assertEqual(self.records()[0]["review"], "pending")
        saved = attempt.read_bytes()
        again = self.close_restore(attempt)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(attempt.read_bytes(), saved)
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        self.assertEqual(self.git("show", "main:scripts/sample/item.py", cwd=self.origin), later.strip())

    def test_explicit_restore_close_survives_a_repeat_with_the_original_patch(self) -> None:
        tip, attempt = self.interrupted_module_push()
        later = MODULE.replace('VALUE = "before"', 'VALUE = "later"')
        self.write(self.unit, "scripts/sample/item.py", later)
        _ = self.git("add", "scripts/sample/item.py", cwd=self.unit)
        self.change_module()
        index = self.git("write-tree", cwd=self.unit)
        closed = self.close_restore(attempt)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        # This has exactly the recorded patch digest, but closure forbids cleanup.
        result = self.fast_track()
        self.assertNotIn("kept worktree edits", result.stderr)
        self.assertEqual(self.attempt_state(attempt)["restore_closed"], "explicit")
        self.assertEqual(self.git("write-tree", cwd=self.unit), index)
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_partial_overlap_validates_the_other_files_in_the_original_attempt(self) -> None:
        self.change_module()
        self.write(self.unit, "scripts/sample/note.txt", "after\n")
        tip, attempt = self.interrupted_push("scripts/sample/item.py", "scripts/sample/note.txt")
        self.write(self.unit, "scripts/sample/note.txt", "later note\n")
        result = self.fast_track("scripts/sample/item.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("scripts/sample/note.txt changed", result.stderr)
        self.assertIn(f"--close-restore {attempt.stem}", result.stderr)
        self.assertEqual((self.unit / "scripts/sample/note.txt").read_text(), "later note\n")
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_retry_closes_an_unpublished_attempt_after_main_moves(self) -> None:
        self.change_module()
        self.interrupt_git("before-push")
        stopped = self.fast_track()
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), self.initial)
        stale = [path for path in self.records_dir.rglob("*.json")]
        self.assertEqual(len(stale), 1)
        original = cast(dict[str, object], json.loads(stale[0].read_text()))
        concurrent = self.move_main_during_check()
        _ = self.git("push", "origin", "HEAD:main", cwd=self.control / "writer")
        (self.control / "move-origin").unlink()
        retry = self.fast_track()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        row, = self.records()
        tip = str(row["commit"])
        self.assertEqual(self.git("merge-base", "--is-ancestor", concurrent, tip, cwd=self.origin), "")
        # The stale attempt remains auditable, but can no longer block a retry.
        self.assertTrue(stale[0].exists())
        closed = cast(dict[str, object], json.loads(stale[0].read_text()))
        self.assertEqual(closed["publication"], "not_published")
        self.assertEqual(closed["restore"], original["restore"])
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        again = self.fast_track()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.records(), [row])

    def test_unpublished_candidate_survives_checkout_reset_and_source_pruning(self) -> None:
        self.change_module()
        self.interrupt_git("before-push")
        stopped = self.fast_track()
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        attempt, = (self.records_dir / "attempts").glob("*.json")
        state = fast_track.read_attempt(attempt)
        candidate = state["restore"]["record"]["commit"]
        reference = f"refs/fast-track/attempts/{attempt.stem}/commit"
        self.assertEqual(self.git("rev-parse", reference, cwd=self.unit), candidate)
        tree = Path((self.control / "stopped-copy").read_text())
        _ = self.git("checkout", "--force", "--detach", self.initial, cwd=tree)
        _ = self.git("reflog", "expire", "--expire=now", "--all", cwd=tree)
        _ = self.git("gc", "--prune=now", cwd=tree)
        _ = self.git("reflog", "expire", "--expire=now", "--all", cwd=self.unit)
        _ = self.git("gc", "--prune=now", cwd=self.unit)
        self.assertEqual(self.git("cat-file", "-t", candidate, cwd=self.unit), "commit")
        self.assertIn('VALUE = "after"', self.git("show", f"{candidate}:scripts/sample/item.py", cwd=self.unit))

    def test_repeated_checks_reuse_checkout_and_keep_ignored_target(self) -> None:
        self.change_module()
        first = self.fast_track()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        tree = Path(str(self.observations("types")[0]["cwd"]))
        cache = tree / "target" / "warm-cache"
        cache.parent.mkdir(exist_ok=True)
        _ = cache.write_text("keep")
        self.write(self.unit, "scripts/sample/note.txt", "new note\n")
        second = self.fast_track("scripts/sample/note.txt")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(Path(str(self.observations("types")[-1]["cwd"])), tree)
        self.assertEqual(cache.read_text(), "keep")

    def test_rerun_recovers_a_push_that_stopped_before_the_review_record(self) -> None:
        self.change_module()
        self.interrupt_git("after-push")
        stopped = self.fast_track()
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual(self.records(), [])
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())
        retry = self.fast_track()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn(f"already on main at {tip[:7]}", retry.stdout)
        row, = self.records()
        self.assertEqual((row["commit"], row["review"]), (tip, "pending"))
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertEqual(len(self.observations("types")), 1)
        pending = self.command("--pending", "--unit", "alpha")
        self.assertIn(tip, pending.stdout)
        self.assertIn('VALUE = "after"', pending.stdout)

    def test_rerun_finishes_restore_after_the_review_record_was_saved(self) -> None:
        self.retry_interrupted_restore("before-restore")

    def test_rerun_finishes_only_the_unrestored_paths_after_a_partial_restore(self) -> None:
        self.retry_interrupted_restore("after-restore")

    def test_recovery_keeps_later_worktree_and_index_edits(self) -> None:
        self.change_module()
        self.interrupt_git("before-restore")
        stopped = self.fast_track()
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        later = MODULE.replace('VALUE = "before"', 'VALUE = "later"')
        self.write(self.unit, "scripts/sample/item.py", later)
        _ = self.git("add", "scripts/sample/item.py", cwd=self.unit)
        # The worktree again matches the pushed fix, but the later index edit stays.
        self.change_module()
        retry = self.fast_track()
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        self.assertIn("kept worktree edits", retry.stderr)
        self.assertEqual(self.git("show", ":scripts/sample/item.py", cwd=self.unit), later.strip())
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.observations("types")), 1)
        _ = self.git("reset", "HEAD", "--", "scripts/sample/item.py", cwd=self.unit)
        self.write(self.unit, "scripts/sample/item.py", later)
        retry = self.fast_track()
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        self.assertIn("kept worktree edits", retry.stderr)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), later)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)

    def test_recovery_keeps_and_names_an_index_edit_made_during_checks(self) -> None:
        path = "scripts/sample/note.txt"
        self.write(self.unit, path, "captured index\n")
        _ = self.git("add", path, cwd=self.unit)
        self.write(self.unit, path, "published note\n")
        self.change_module()
        _ = (self.control / "index-edit.json").write_text(json.dumps({
            "repo": str(self.unit), "path": path, "content": "later staged edit\n",
        }))
        tip, attempt = self.interrupted_push("scripts/sample/item.py", path)
        # A partial restore can put another path back at its baseline.
        _ = self.git("restore", "--staged", "--worktree", "scripts/sample/item.py", cwd=self.unit)
        index = self.git("write-tree", cwd=self.unit)
        retry = self.fast_track("scripts/sample/item.py", path)
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        self.assertIn(path + " changed", retry.stderr)
        self.assertEqual(self.git("write-tree", cwd=self.unit), index)
        self.assertEqual(self.git("show", ":" + path, cwd=self.unit), "later staged edit")
        self.assertEqual((self.unit / path).read_text(), "published note\n")
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertFalse(self.attempt_state(attempt)["restored"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_partial_restore_accepts_baseline_captured_index_and_published_states(self) -> None:
        paths = ("scripts/sample/note.txt", "folder/file0.txt", "folder/file1.txt")
        for path in paths:
            self.write(self.unit, path, "captured index\n")
        _ = self.git("add", "--", *paths, cwd=self.unit)
        for path in paths:
            self.write(self.unit, path, "published content\n")
        self.interrupt_git("after-restore")
        stopped = self.fast_track(*paths)
        self.assertEqual(stopped.returncode, -9, stopped.stdout + stopped.stderr)
        # The first path is restored. The second keeps its captured index.
        _ = self.git("add", "--", paths[2], cwd=self.unit)
        retry = self.fast_track(*paths)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn("already on main", retry.stdout)
        for path in paths:
            self.assertEqual((self.unit / path).read_text(), "before\n")
            self.assertEqual(self.git("show", ":" + path, cwd=self.unit), "before")
        self.assertEqual(len(self.records()), 1)

    def test_removed_new_file_recovery_also_removes_its_staged_addition(self) -> None:
        path = "scripts/sample/new.txt"
        self.write(self.unit, path, "published addition\n")
        _ = self.git("add", "--", path, cwd=self.unit)
        tip, attempt = self.interrupted_push(path)
        (self.unit / path).unlink()
        for retry_number in range(2):
            with self.subTest(retry=retry_number):
                retry = self.fast_track(path)
                self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                self.assertIn("already on main", retry.stdout)
                self.assertFalse((self.unit / path).exists())
                self.assertEqual(self.git("ls-files", "--", path, cwd=self.unit), "")
                self.assertTrue(self.attempt_state(attempt)["restored"])
                self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_recovery_objects_survive_gc_until_automatic_restore_closes(self) -> None:
        path = "scripts/sample/note.txt"
        self.write(self.unit, path, "captured index\n")
        _ = self.git("add", "--", path, cwd=self.unit)
        self.write(self.unit, path, "published note\n")
        tip, attempt = self.interrupted_push(path)
        _ = self.git("reset", "HEAD", "--", path, cwd=self.unit)
        _ = self.git("gc", "--prune=now", cwd=self.unit)
        retry = self.fast_track(path)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual((self.unit / path).read_text(), "before\n")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        # A restored attempt still supports the original patch returning.
        self.write(self.unit, path, "published note\n")
        _ = self.git("gc", "--prune=now", cwd=self.unit)
        again = self.fast_track(path)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual((self.unit / path).read_text(), "before\n")
        prefix = "refs/fast-track/attempts/" + attempt.stem
        self.assertTrue(self.git("for-each-ref", "--format=%(refname)", prefix, cwd=self.unit))
        closed = self.close_restore(attempt)
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/fast-track", cwd=self.unit), "")

    def test_safe_unrelated_attempt_keeps_its_index_worktree_and_restore_record(self) -> None:
        tip, attempt = self.interrupted_module_push()
        pending = self.command("--pending", "--unit", "alpha")
        self.assertEqual(pending.returncode, 0, pending.stdout + pending.stderr)
        index = self.git("write-tree", cwd=self.unit)
        content = (self.unit / "scripts/sample/item.py").read_bytes()
        self.write(self.unit, "scripts/sample/note.txt", "new note\n")
        result = self.fast_track("scripts/sample/note.txt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(attempt.stem, result.stderr)
        self.assertEqual(self.git("write-tree", cwd=self.unit), index)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_bytes(), content)
        self.assertFalse(self.attempt_state(attempt)["restored"])
        self.assertEqual(self.records()[0]["commit"], tip)
        saved = attempt.read_bytes()
        self.write(self.unit, "folder/file0.txt", "another fix\n")
        next_fix = self.fast_track("folder/file0.txt")
        self.assertEqual(next_fix.returncode, 0, next_fix.stdout + next_fix.stderr)
        self.assertEqual(attempt.read_bytes(), saved)
        self.assertEqual(self.git("write-tree", cwd=self.unit), index)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_bytes(), content)

    def test_fast_track_without_a_build_cache_records_the_push_and_removes_its_copy(self) -> None:
        _ = self.git("rm", "Cargo.toml")
        _ = self.git("commit", "-m", "repository without a build cache")
        _ = self.git("push", "origin", "HEAD:main", "HEAD:production")
        _ = self.git("reset", "--hard", "production", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "production", cwd=self.promote)
        self.change_module()

        result = self.fast_track()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        row, = self.records()
        self.assertEqual((row["unit"], row["commit"], row["review"]), ("alpha", tip, "pending"))
        tests = self.observations("test")
        self.assertEqual({row["module"] for row in tests}, {"test_item", "test_item_extra"})
        for observed in tests:
            checkout = Path(str(observed["cwd"]))
            self.assertNotIn(checkout, (self.unit, self.checkout, self.promote))
            self.assertTrue(observed["private_git"], observed)
            self.assertFalse(checkout.exists())
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)

    def test_one_command_shares_its_settings_through_fetch_and_recovery(self) -> None:
        self.change_module()
        with patch.object(conf, "read", wraps=conf.read) as reads:
            result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call.args[0] for call in reads.call_args_list if call.args[0] == "fast_track"],
                         ["fast_track"])
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)

    # Keep one complete CLI process case.
    @process_boundary
    def test_push_checks_only_direct_tests_in_a_private_clone_and_restores_named_paths(self) -> None:
        self.change_module()
        self.env.update(PYTHONUNBUFFERED="1", REFUSAL_WATCH_INVENTORY="fixture", STAFF_OPERATION_FD="-1")
        self.write(self.unit, "unrelated.txt", "keep this edit\n")
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual(self.git("show", "-s", "--format=%B", tip, cwd=self.origin),
                         "fast-track(alpha): repair the sample\n\nFast-Track: alpha")
        self.assertEqual(self.git("show", f"{tip}:scripts/sample/item.py", cwd=self.origin),
                         MODULE.replace('VALUE = "before"', 'VALUE = "after"').strip())
        self.assertEqual((self.unit / "scripts/sample/item.py").read_text(), MODULE)
        self.assertEqual((self.unit / "unrelated.txt").read_text(), "keep this edit\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(len(self.events("ssh")), 1)
        tests = self.observations("test")
        self.assertEqual({row["module"] for row in tests}, {"test_item", "test_item_extra"})
        for row in tests:
            with self.subTest(module=row["module"], case="package name"):
                self.assertEqual(row["package_module"], "scripts.sample." + str(row["module"]))
            self.assertTrue(row["private_git"], row)
            self.assertEqual(row["branch"], "", row)
            self.assertEqual(Path(str(row["common"])), Path(str(row["cwd"])) / ".git")
        checks = self.observations("types")
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["lock_held"], checks)
        self.assertTrue(any(row["value"] == "after" for row in self.observations("import")))
        for row in [*tests, *checks]:
            clone = Path(str(row["cwd"]))
            self.assertNotIn(clone, (self.unit, self.checkout, self.promote))
            self.assertTrue((clone / ".git").is_dir(), "the standing checkout must remain")
        row, = self.records()
        self.assertEqual((row["unit"], row["commit"], row["why"], row["review"]),
                         ("alpha", tip, "repair the sample", "pending"))
        self.assertRegex(str(row["patch_sha256"]), r"^[0-9a-f]{64}$")
        self.assertTrue(datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00")).isoformat().endswith("+00:00"))

    def test_new_files_and_changed_test_modules_are_checked_and_removed_after_push(self) -> None:
        path = "scripts/sample/test_added.py"
        self.write(self.unit, path, DIRECT_TEST.replace('self.assertEqual(item.VALUE, "after")',
                                                     'self.assertEqual(item.VALUE, "before")'))
        result = self.fast_track(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.unit / path).exists())
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertIn(path, self.git("ls-tree", "-r", "--name-only", tip, cwd=self.origin))
        self.assertEqual(len(self.observations("types")), 1)
        self.assertEqual([row["module"] for row in self.observations("test")], ["test_added"])

    def test_import_with_empty_suite_text_and_nonzero_exit_refuses_the_patch(self) -> None:
        path = "scripts/sample/added.py"
        self.write(self.unit, path, "print('Ran 0 tests')\nraise SystemExit(5)\n")
        self.assert_refused(self.fast_track(path), "fast check failed")
        self.assertEqual(self.observations("types"), [])

    def test_empty_discovery_passes_before_a_successful_import(self) -> None:
        path = "scripts/sample/added.py"
        self.write(self.unit, path, "VALUE = 'checked'\n")
        self.write(self.unit, "scripts/sample/test_added.py", "# This module has no tests.\n")
        result = self.fast_track(path, "scripts/sample/test_added.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 0 tests", result.stdout)
        self.assertIn("scripts/sample/added.py", self.git("ls-tree", "-r", "--name-only", "main", cwd=self.origin))
        self.assertFalse((self.unit / path).exists())

    def test_import_failure_without_a_direct_test_refuses_the_patch(self) -> None:
        path = "scripts/sample/added.py"
        self.write(self.unit, path, "raise RuntimeError('broken package import')\n")
        self.assert_refused(self.fast_track(path))

    def test_no_change_and_outside_paths_are_refused_without_a_push(self) -> None:
        self.assert_refused(self.fast_track())
        outside = self.root / "outside.txt"
        _ = outside.write_text("outside\n")
        self.assert_refused(self.fast_track(str(outside)))

    def test_sensitive_paths_are_refused_before_any_check(self) -> None:
        patterns = cast(list[str], self.settings["refused_paths"])
        paths = {pattern.replace("**", "fixture").replace("*", "fixture").rstrip("/")
                 + ("/fixture.py" if pattern.endswith("/") else "") for pattern in patterns}
        paths.update({"settings.json", "scripts/hooks/private/check.py", "scripts/lib/py",
                      ".local/state/staff/staff.json", "scripts/production/staff.py",
                      "scripts/production/staff_sources.py", "scripts/production/add_unit.py",
                      "scripts/production/add_showrunner.py", "scripts/agents/shutdown.py",
                      "scripts/production/promote_showrunner.py", "scripts/production/transfer_unit.py",
                      "scripts/accounts/moves.py", "scripts/agents/shutdown.py",
                      "scripts/agents/shutdown.py", "scripts/production/merge_gate.py",
                      "scripts/production/merge_checkpoint.py", "scripts/direct/verify.sh",
                      "scripts/production/build_hold.py", "scripts/production/fast_track.py",
                      "config/fast_track.conf"})
        for path in sorted(paths):
            with self.subTest(path=path):
                self.write(self.unit, path, "refused edit\n")
                self.assert_refused(self.fast_track(path))
        self.assertEqual(self.observations("types"), [])

    def test_missing_and_invalid_configuration_keys_name_the_key(self) -> None:
        self.change_module()
        valid = self.settings.copy()
        invalid: dict[str, list[object]] = {
            "max_changed_lines": [0, -1, 1.5, "many", True], "max_files": [0, -1, 1.5, "many", True],
            "check_time_limit_seconds": [0, -1, "soon", True],
            "max_push_attempts": [0, -1, 1.5, "many", True], "refused_paths": ["", "   "],
        }
        for key, values in invalid.items():
            for value in [None, *values]:
                with self.subTest(key=key, value=value):
                    self.settings = valid.copy()
                    if value is None:
                        del self.settings[key]
                    else:
                        self.settings[key] = value
                    self.save_config()
                    self.assert_refused(self.fast_track(), key)

    def test_file_and_line_limits_are_read_from_configuration(self) -> None:
        self.write(self.unit, "one.txt", "one\n")
        self.write(self.unit, "two.txt", "two\n")
        self.settings["max_files"] = 1
        self.save_config()
        self.assert_refused(self.fast_track("one.txt", "two.txt"), "max_files")
        self.settings.update(max_files=2, max_changed_lines=1)
        self.save_config()
        self.assert_refused(self.fast_track("one.txt", "two.txt"), "max_changed_lines")

    def test_conflicting_main_change_keeps_the_patch_and_pushes_nothing(self) -> None:
        self.change_module()
        self.write(self.checkout, "scripts/sample/item.py", MODULE.replace('VALUE = "before"', 'VALUE = "other"'))
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "other repair")
        _ = self.git("push", "origin", "HEAD:main")
        self.initial = self.git("rev-parse", "HEAD")
        self.assert_refused(self.fast_track())
        self.assertIn('VALUE = "after"', (self.unit / "scripts/sample/item.py").read_text())

    def test_red_direct_test_or_type_warning_prevents_the_push(self) -> None:
        self.change_module()
        _ = (self.control / "bad-types").touch()
        self.assert_refused(self.fast_track())
        (self.control / "bad-types").unlink()
        self.write(self.unit, "scripts/sample/item.py", MODULE.replace('VALUE = "before"', 'VALUE = "wrong"'))
        self.assert_refused(self.fast_track())

    def test_origin_main_move_reapplies_and_rechecks_outside_the_push_lock(self) -> None:
        self.change_module()
        concurrent = self.move_main_during_check()
        result = self.fast_track()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertEqual(self.git("merge-base", "--is-ancestor", concurrent, tip, cwd=self.origin), "")
        self.assertEqual(self.git("show", f"{tip}:concurrent.txt", cwd=self.origin), "another fix")
        checks = self.observations("types")
        self.assertEqual(len(checks), 2, checks)
        self.assertFalse(any(row["lock_held"] for row in checks))
        self.assertEqual(len(self.records()), 1)

    def test_push_attempt_limit_stops_after_the_configured_number_of_checks(self) -> None:
        self.change_module()
        self.settings["max_push_attempts"] = 1
        self.save_config()
        concurrent = self.move_main_during_check()
        result = self.fast_track()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), concurrent)
        self.assertEqual(len(self.observations("types")), 1)
        self.assertEqual(self.records(), [])

    def test_check_time_limit_stops_the_check_process_group(self) -> None:
        self.change_module()
        self.settings["check_time_limit_seconds"] = 1
        self.save_config()
        pid_file = self.root / "check-descendant.pid"
        self.env["FAST_TRACK_DESCENDANT_PID"] = str(pid_file)
        # The alarm outlasts the wait below, so only the time limit can satisfy the assertions.
        self.env["FAST_TRACK_FIXTURE_ALARM_SECS"] = str(2 * TEST_HANG_GUARD_SECS)
        self.write(self.unit, "scripts/sample/test_item.py", '''import os
from pathlib import Path
import signal
import subprocess
import sys
child = subprocess.Popen([sys.executable, "-c", "import os, signal; signal.alarm(int(os.environ[\\\"FAST_TRACK_FIXTURE_ALARM_SECS\\\"])); signal.pause()"])
Path(os.environ["FAST_TRACK_DESCENDANT_PID"]).write_text(str(child.pid))
signal.alarm(int(os.environ["FAST_TRACK_FIXTURE_ALARM_SECS"]))
signal.pause()
''')
        result = self.fast_track("scripts/sample/item.py", "scripts/sample/test_item.py")
        self.assert_refused(result, "over the fast-track time limit; use the normal path")
        self.assertTrue(pid_file.exists(), "the limit must stop a running check")
        pid = int(pid_file.read_text())

        def exited() -> bool:
            try:
                return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] == "Z"
            except FileNotFoundError:
                return True

        if not exited():
            descriptor = os.pidfd_open(pid)
            try:
                readable, _, _ = select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)
                self.assertTrue(readable, "the check descendant survived the limit")
            finally:
                os.close(descriptor)
        self.assertTrue(exited(), "the check descendant survived the limit")

    def test_reruns_after_restore_or_promotion_failure_do_not_push_twice(self) -> None:
        self.change_module()
        _ = (self.state / "mac-rc").write_text("1\n")
        first = self.fast_track()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.assertNotEqual(tip, self.initial)
        self.assertEqual(len(self.records()), 1)
        _ = (self.state / "mac-rc").write_text("0\n")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                retry = self.fast_track()
                self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                self.assertIn(f"already on main at {tip[:7]}", retry.stdout)
                self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
                self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.observations("types")), 1)

    def test_pending_outputs_the_pushed_diff_and_review_marks_only_named_snapshot_rows(self) -> None:
        self.change_module()
        pushed = self.fast_track()
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        row, = self.records()
        pending = self.command("--pending", "--unit", "alpha")
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertIn(str(row["commit"]), pending.stdout)
        self.assertIn('VALUE = "after"', pending.stdout)
        self.assertIn("diff --git", pending.stdout)
        self.assertRegex(pending.stdout, r"(?m)^#+ .+")
        foreign = {**row, "unit": "beta", "commit": self.initial,
                   "patch_sha256": hashlib.sha256(b"foreign").hexdigest()}
        later = {**row, "commit": self.initial, "patch_sha256": hashlib.sha256(b"later").hexdigest()}
        with (self.records_dir / "records.jsonl").open("a") as output:
            _ = output.write(json.dumps(foreign) + "\n" + json.dumps(later) + "\n")
        marked = self.command("--mark-reviewed", "--unit", "alpha", "--pass", "7d/1/contract",
                              "--commit", str(row["commit"]))
        self.assertEqual(marked.returncode, 0, marked.stdout + marked.stderr)
        rows = self.records()
        self.assertEqual((rows[0]["review"], rows[0]["review_pass"]), ("reviewed", "7d/1/contract"))
        self.assertEqual([entry["review"] for entry in rows[1:]], ["pending", "pending"])
        pending = self.command("--pending", "--unit", "no-such")
        self.assertEqual((pending.returncode, pending.stdout), (0, ""))


if __name__ == "__main__":
    _ = unittest.main()
