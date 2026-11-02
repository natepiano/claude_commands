"""Run the real CLI on a synthetic production with isolated state and external tools."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import shutil
import socket
import urllib.request
import fcntl
from types import ModuleType
from collections.abc import Generator
from contextlib import contextmanager
from typing import ClassVar
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import upgrade_record as upgrade_record
import bevy_upgrade
import upstream
from scripts.production import showrunners
from cli_fixture import invoke_cli
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        """The checker marker is an identity decorator on Python 3.11."""
        return method


SCRIPT = Path(__file__).with_name("bevy_upgrade.py")
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
REVISION = "1234567890abcdef1234567890abcdef12345678"
ZONE = ZoneInfo("Pacific/Kiritimati")


def setUpModule() -> None:
    """The dates these tests expect are in `ZONE`; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "Pacific/Kiritimati"}))


class CliFixture(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    env: dict[str, str] = {}
    fixture_root: ClassVar[Path]
    snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        folder = tempfile.TemporaryDirectory(prefix="bevy-cli-")
        cls.addClassCleanup(folder.cleanup)
        cls.fixture_root = Path(folder.name).resolve() / "case"
        cls.snapshot = cls.fixture_root.with_name("snapshot")
        seed = cls()
        seed.root = cls.fixture_root
        seed.checkout = seed.root / "checkout"
        seed.root.mkdir()
        seed.build_fixture()
        _ = shutil.copytree(seed.root, cls.snapshot)
        shutil.rmtree(seed.root)

    @override
    def setUp(self) -> None:
        self.root = self.fixture_root
        _ = shutil.copytree(self.snapshot, self.root)
        self.addCleanup(shutil.rmtree, self.root)
        self.checkout = self.root / "checkout"
        self.doc = self.checkout / "demo-production.md"
        self.env = dict(os.environ)
        self.env.update(cast(dict[str, str], json.loads((self.root / "environment.json").read_text())))

    def build_fixture(self) -> None:
        self.checkout.mkdir()
        _ = (self.checkout / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        self.write_lock()
        self.doc = self.checkout / "demo-production.md"
        _ = self.doc.write_text(
            "# Demo\n\n- **Merge branch:** demo\n"
            + f"- **Showrunner checkout:** {self.checkout}\n"
            + f"- **Log:** log.md\n- **User zone:** {ZONE.key}\n\n## Units\n",
            encoding="utf-8",
        )
        helper = self.root / "tool.py"
        _ = helper.write_text(
            "import fcntl, json, os, sys\nfrom pathlib import Path\n"
            + "kind, *args = sys.argv[1:]\n"
            + "state = Path(os.environ['BEVY_UPGRADE_STATE_DIR']) / 'demo.json'\n"
            + "record = json.loads(state.read_text()) if state.exists() else {}\n"
            + "locked = False\n"
            + "lock = state.with_suffix('.lock')\n"
            + "if lock.exists():\n"
            + "    with lock.open('a') as competitor:\n"
            + "        try:\n"
            + "            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            + "        except BlockingIOError:\n"
            + "            locked = True\n"
            + "event = {'kind': kind, 'args': args, 'closed': record.get('closed'),\n"
            + "         'record_exists': state.exists(), 'locked': locked, 'announced': record.get('announced')}\n"
            + "with Path(os.environ[kind.upper() + '_CALLS']).open('a') as out:\n"
            + "    out.write(json.dumps(event) + '\\n')\n"
            + "with Path(os.environ['TOOL_EVENTS']).open('a') as out:\n"
            + "    out.write(json.dumps(event) + '\\n')\n"
            + "if kind == 'notifier':\n"
            + "    if args[0] == 'status':\n"
            + "        print('job status\\nnext_due=1791850000 (local time)')\n"
            + "    raise SystemExit(0)\n"
            + "if args[0] == 'retire':\n"
            + "    print(os.environ.get('RETIRE_OUTCOME', 'retired 1 queued message'))\n"
            + "    raise SystemExit(int(os.environ.get('RETIRE_EXIT', '0')))\n"
            + "print(os.environ.get('SEND_OUTCOME', 'SENT: test showrunner'))\n"
            + "raise SystemExit(int(os.environ.get('SEND_EXIT', '0')))\n",
            encoding="utf-8",
        )
        notifier = self.root / "notifier-stub.zsh"
        _ = notifier.write_text('python3 "$TEST_TOOL_HELPER" notifier "$@"\n', encoding="utf-8")
        sender = self.root / "send-stub.py"
        _ = sender.write_text(
            '#!/usr/bin/env python3\nimport os, runpy, sys\n'
            + 'sys.argv.insert(1, "send")\nrunpy.run_path(os.environ["TEST_TOOL_HELPER"], run_name="__main__")\n',
            encoding="utf-8",
        )
        sender.chmod(0o755)
        isolation = self.root / "isolation"
        isolation.mkdir()
        _ = (isolation / "sitecustomize.py").write_text(
            'import socket\nimport urllib.request\n'
            + 'def reject_network(*_args: object, **_kwargs: object) -> None:\n'
            + '    raise AssertionError("a test attempted network access")\n'
            + 'socket.socket.connect = reject_network\nurllib.request.urlopen = reject_network\n',
            encoding="utf-8",
        )
        self.write_fixtures()
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.root / "home"), "BEVY_UPGRADE_STATE_DIR": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
            "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json"),
            "BEVY_UPGRADE_NOTIFIER": str(notifier), "BEVY_UPGRADE_SEND": str(sender),
            "TEST_TOOL_HELPER": str(helper), "NOTIFIER_CALLS": str(self.root / "notifier-calls"),
            "SEND_CALLS": str(self.root / "send-calls"), "TEST_SHOWRUNNER_ADDRESS": "uds:/test/session",
            "TOOL_EVENTS": str(self.root / "tool-calls"), "RETIRE_EXIT": "0",
            "RETIRE_OUTCOME": "retired 1 queued message",
            "EXTERNAL_CALLS": str(self.root / "external-calls"),
            "CLAUDE_CODE_SESSION_ID": "no-real-session", "PYTHONPATH": str(isolation),
            "PYTHONDONTWRITEBYTECODE": "1", "BEVY_UPGRADE_FIXTURES": str(self.root / "fixtures.json"),
        })
        _ = (self.root / "environment.json").write_text(json.dumps({
            key: value for key, value in self.env.items() if value != os.environ.get(key)
        }))

    def write_lock(self, version: str = "1.2.3", bevy: str = "0.19.1", name: str = "adapter") -> None:
        _ = (self.checkout / "Cargo.lock").write_text(
            'version = 4\n[[package]]\nname = "bevy_ecs"\n'
            + f'version = "{bevy}"\nsource = "{REGISTRY}"\n'
            + f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
            + f'source = "git+https://github.com/example/adapter?branch=main#{REVISION}"\n'
            + 'dependencies = ["bevy_ecs"]\n'
            + '[[package]]\nname = "app"\nversion = "0.1.0"\n'
            + f'dependencies = ["{name}"]\n', encoding="utf-8",
        )


    def read_record(self) -> upgrade_record.Record:
        return cast(upgrade_record.Record, json.loads((self.root / "state/demo.json").read_text(encoding="utf-8")))


    def assert_table(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", result.stdout)


    def begin(self) -> None:
        self.assert_table(self.run_cli("start", "0.20"))


    def write_fixtures(self, ready: bool = False) -> None:
        api = "https://crates.io/api/v1/crates"
        github = "https://api.github.com/repos/example/adapter"
        rev = "abcdef0123456789"
        bodies: dict[str, object] = {
            f"{api}/bevy/versions": {"versions": [{"num": "0.20.0", "created_at": "2026-10-12T00:00:00Z"}]},
            f"{api}/adapter": {"crate": {"repository": "https://github.com/example/adapter.git"},
                               "versions": [{"num": "2.0.0", "yanked": False}, {"num": "1.2.3", "yanked": False}]},
            f"{api}/adapter/2.0.0/dependencies": {"dependencies": [
                {"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.20" if ready else "^0.19"}]},
            f"{api}/adapter/1.2.3/dependencies": {"dependencies": [
                {"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.19"}]},
            github: {"default_branch": "main"},
            f"{github}/commits/main": {"sha": rev, "commit": {"committer": {"date": "2026-10-12T00:00:00Z"}}},
            f"https://raw.githubusercontent.com/example/adapter/{rev}/Cargo.toml": '[dependencies]\nbevy = "^0.19"\n',
            f"{github}/pulls/7": {"state": "open", "merged": False,
                                 "head": {"ref": "port", "sha": "1234567890abcdef"}},
        }
        for since in {"2026-10-12", datetime.now(timezone.utc).date().isoformat()}:
            bodies[f"{github}/issues?state=all&since={since}T00:00:00Z&sort=updated&direction=desc&per_page=100"] = [
                {"number": 7, "title": "Port to Bevy 0.20", "state": "open", "pull_request": {},
                 "created_at": "2026-10-12T00:00:00Z", "updated_at": "2026-10-12T00:00:00Z"},
            ]
        _ = (self.root / "fixtures.json").write_text(json.dumps(bodies), encoding="utf-8")


    def tool_calls(self, kind: str) -> list[dict[str, object]]:
        path = self.root / f"{kind}-calls"
        return [cast(dict[str, object], json.loads(row)) for row in path.read_text().splitlines()] if path.exists() else []


    def tool_args(self, kind: str) -> list[list[str]]:
        arguments = [cast(list[str], call["args"]) for call in self.tool_calls(kind)]
        return [args for args in arguments if kind != "send" or args[0] != "retire"]


    def retire_calls(self) -> list[dict[str, object]]:
        return [call for call in self.tool_calls("send") if cast(list[str], call["args"])[0] == "retire"]


    def retire_arguments(self) -> list[str]:
        return ["retire", "--to", "uds:/test/session", "--from", "bevy-upgrade", "--key", "bevy-upgrade-demo"]


    def clear_calls(self) -> None:
        for kind in ("notifier", "send", "save", "fetch"):
            (self.root / f"{kind}-calls").unlink(missing_ok=True)
        (self.root / "tool-calls").unlink(missing_ok=True)


    def store_record(self, current: upgrade_record.Record) -> None:
        _ = (self.root / "state/demo.json").write_text(json.dumps(current), encoding="utf-8")


    def make_branch_ready(self) -> None:
        bodies = cast(dict[str, object], json.loads((self.root / "fixtures.json").read_text()))
        rev = "fedcba9876543210"
        bodies["https://api.github.com/repos/example/adapter/commits/main"] = {
            "sha": rev, "commit": {"committer": {"date": "2026-10-12T00:00:00Z"}}}
        bodies[f"https://raw.githubusercontent.com/example/adapter/{rev}/Cargo.toml"] = '[dependencies]\nbevy = "^0.20"\n'
        _ = (self.root / "fixtures.json").write_text(json.dumps(bodies), encoding="utf-8")


    def assert_silent_check(self, result: subprocess.CompletedProcess[str], code: int = 0) -> None:
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stdout, "")


    def run_cli_process(self, *arguments: str, explicit_production: bool = True,
                count_saves: bool = False) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPT)]
        counter = (
            "import fcntl\nimport os\nimport sys\nfrom pathlib import Path\n"
            + "sys.path.append(str(Path(sys.argv.pop(1)).parent))\n"
            + "import upstream\n"
            + "factory = upstream.fixture_fetch\n"
            + "def traced_factory(path: Path) -> upstream.Fetch:\n"
            + "    fetch = factory(path)\n"
            + "    def traced_fetch(url: str) -> bytes:\n"
            + "        with Path(os.environ['EXTERNAL_CALLS']).with_name('fetch-calls').open('a') as stream:\n"
            + "            stream.write(url + '\\n')\n"
            + "        return fetch(url)\n"
            + "    return traced_fetch\n"
            + ("upstream.fixture_fetch = traced_factory\n" if count_saves else "")
            + "import bevy_upgrade\nimport upgrade_record\nfrom scripts.production import showrunners\n"
            + "showrunners.current_address = lambda slug: os.environ['TEST_SHOWRUNNER_ADDRESS']\n"
            + "save = upgrade_record.save\n"
            + "def counted_save(current: upgrade_record.Record) -> None:\n"
            + "    lock = upgrade_record.record_path(current['slug']).with_suffix('.lock')\n"
            + "    with lock.open('a') as competitor:\n"
            + "        try:\n"
            + "            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            + "        except BlockingIOError:\n"
            + "            pass\n"
            + "        else:\n"
            + "            raise AssertionError('CLI save ran outside the exclusive record lock')\n"
            + "    save(current)\n"
            + "    with Path(os.environ['EXTERNAL_CALLS']).with_name('save-calls').open('a') as stream:\n"
            + "        stream.write('saved\\n')\n"
            + ("upgrade_record.save = counted_save\n" if count_saves else "")
            + "raise SystemExit(bevy_upgrade.main())\n"
        )
        command = [sys.executable, "-c", counter, str(SCRIPT)]
        if explicit_production:
            command.extend(["--production", str(self.doc)])
        result = subprocess.run(command + list(arguments), cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        return result


    def run_cli(self, *arguments: str, explicit_production: bool = True,
                count_saves: bool = False) -> subprocess.CompletedProcess[str]:
        # Keep one full interpreter/wrapper boundary; the other cases prove CLI behavior.
        if self._testMethodName == "test_start_reads_the_production_checkout_and_writes_an_open_record":
            return self.run_cli_process(*arguments, explicit_production=explicit_production,
                                        count_saves=count_saves)
        command = ["--production", str(self.doc)] if explicit_production else []
        real_fetch = upstream.fixture_fetch
        real_save = upgrade_record.save
        real_run = subprocess.run

        def traced_factory(path: Path) -> upstream.Fetch:
            fetch = real_fetch(path)
            def traced_fetch(url: str) -> bytes:
                with (self.root / "fetch-calls").open("a") as stream:
                    _ = stream.write(url + "\n")
                return fetch(url)
            return traced_fetch

        def counted_save(current: upgrade_record.Record) -> None:
            lock = upgrade_record.record_path(current["slug"]).with_suffix(".lock")
            with lock.open("a") as competitor:
                try:
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("CLI save ran outside the exclusive record lock")
            real_save(current)
            with (self.root / "save-calls").open("a") as stream:
                _ = stream.write("saved\n")

        def run(args: list[str], *, capture_output: bool = False, text: bool = False,
                check: bool = False) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["zsh", self.env["BEVY_UPGRADE_NOTIFIER"]]:
                # This fixture's shell wrapper forwards exactly these args to tool.py.
                script = Path(self.env["TEST_TOOL_HELPER"])
                tool_args = ["notifier", *args[2:]]
            elif len(args) > 1 and args[1] == self.env["BEVY_UPGRADE_SEND"]:
                script = Path(args[1])
                tool_args = args[2:]
            else:
                return real_run(args, capture_output=capture_output, text=text, check=check)
            fake = ModuleType("bevy_cli_tool_fixture")
            fake.__file__ = str(script)
            def main() -> int:
                exec(compile(script.read_text(), str(script), "exec"),
                     {**vars(fake), "__name__": "__main__"})
                return 0
            return invoke_cli(fake, main, tool_args, dict(os.environ), Path.cwd())

        def reject_network(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a test attempted network access")

        def current_address(_slug: str) -> str:
            return os.environ["TEST_SHOWRUNNER_ADDRESS"]

        with mock.patch.object(subprocess, "run", run), mock.patch.object(
            showrunners, "current_address", current_address,
        ), mock.patch.object(socket.socket, "connect", reject_network), mock.patch.object(
            urllib.request, "urlopen", reject_network,
        ), self.trace_calls(count_saves, traced_factory, counted_save):
            return invoke_cli(bevy_upgrade, bevy_upgrade.main, command + list(arguments),
                              self.env, self.checkout, production_state=True)

    @contextmanager
    def trace_calls(self, enabled: bool, fetch: object, save: object) -> Generator[None]:
        if enabled:
            with mock.patch.object(upstream, "fixture_fetch", fetch), mock.patch.object(upgrade_record, "save", save):
                yield
        else:
            yield
