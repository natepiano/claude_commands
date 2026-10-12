"""Run the real CLI on a synthetic production with isolated state and external tools."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from zoneinfo import ZoneInfo

import upgrade_record

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


@final
class CliTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-cli-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.checkout = self.root / "checkout"
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
        tool = self.root / "reject-tool"
        _ = tool.write_text('#!/bin/sh\nprintf called > "$EXTERNAL_CALLS"\nexit 97\n', encoding="utf-8")
        tool.chmod(0o755)
        isolation = self.root / "isolation"
        isolation.mkdir()
        _ = (isolation / "sitecustomize.py").write_text(
            'import socket\nimport urllib.request\n'
            + 'def reject_network(*args: object, **kwargs: object) -> None:\n'
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
            "BEVY_UPGRADE_NOTIFIER": str(tool), "BEVY_UPGRADE_SEND": str(tool),
            "EXTERNAL_CALLS": str(self.root / "external-calls"),
            "CLAUDE_CODE_SESSION_ID": "no-real-session", "PYTHONPATH": str(isolation),
            "PYTHONDONTWRITEBYTECODE": "1", "BEVY_UPGRADE_FIXTURES": str(self.root / "fixtures.json"),
        })

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

    def run_cli(self, *arguments: str, explicit_production: bool = True,
                count_saves: bool = False) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPT)]
        if count_saves:
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
                + "upstream.fixture_fetch = traced_factory\n"
                + "import bevy_upgrade\nimport upgrade_record\n"
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
                + "upgrade_record.save = counted_save\n"
                + "raise SystemExit(bevy_upgrade.main())\n"
            )
            command = [sys.executable, "-c", counter, str(SCRIPT)]
        if explicit_production:
            command.extend(["--production", str(self.doc)])
        result = subprocess.run(command + list(arguments), cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertFalse((self.root / "external-calls").exists(), "CLI invoked a notifier or send tool")
        return result

    def read_record(self) -> upgrade_record.Record:
        return cast(upgrade_record.Record, json.loads((self.root / "state/demo.json").read_text(encoding="utf-8")))

    def assert_table(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", result.stdout)

    def begin(self) -> None:
        self.assert_table(self.run_cli("start", "0.20"))

    def test_start_reads_the_production_checkout_and_writes_an_open_record(self) -> None:
        result = self.run_cli("start", "0.20")
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["slug"], "demo")
        self.assertEqual(current["production"], str(self.doc))
        self.assertEqual(current["checkout"], str(self.checkout))
        self.assertEqual(current["target"], "0.20")
        self.assertEqual(current["bevy"], ["0.19.1"])
        self.assertIsNone(current["closed"])
        self.assertIsNone(current["dependencies"]["adapter"]["answer"])
        self.assertIn(f"Bevy 0.20 upgrade for demo: {self.checkout}, on Bevy 0.19.1.", result.stdout)
        self.assertIn("| adapter | git branch=main @ 1234567 on Bevy 0.19.1 |", result.stdout)

    def test_all_non_start_verbs_with_no_record_print_the_same_guidance(self) -> None:
        for arguments in ((), ("status",), ("check",), ("approve", "adapter", "yes"),
                          ("decline", "adapter", "no"), ("forget", "adapter"), ("note", "adapter")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout,
                                 "no Bevy upgrade is tracked for demo; start one with /showrunner:bevy_upgrade start <version>\n")
                self.assertFalse((self.root / "state/demo.json").exists())

    def test_no_registered_production_has_the_specified_error(self) -> None:
        result = self.run_cli(explicit_production=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr,
                         "bevy_upgrade: this session runs no production; pass --production <doc>\n")

    def test_registered_production_comes_from_the_session_timer(self) -> None:
        timer = self.root / "notifier/showrunner-demo"
        timer.mkdir(parents=True)
        check_command = shlex.join([str(self.root / "production_check.sh"), str(self.doc)])
        _ = (timer / "conf").write_text(
            f'TARGET=session:no-real-session\nCHECK={check_command}\n',
            encoding="utf-8",
        )
        result = self.run_cli("start", "0.20", explicit_production=False)
        self.assert_table(result)
        self.assertEqual(self.read_record()["production"], str(self.doc))

    def test_approve_decline_forget_and_note_end_in_a_table(self) -> None:
        self.begin()
        words = '  yes, use this crate "as is"  '
        before = datetime.now(ZONE).date().isoformat()
        approved = self.run_cli("approve", "adapter", words)
        after = datetime.now(ZONE).date().isoformat()
        self.assert_table(approved)
        answer = self.read_record()["dependencies"]["adapter"]["answer"]
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertEqual(answer["answer"], "approved")
        self.assertEqual(answer["words"], words)
        self.assertIn(answer["at"], (before, after))
        self.assertIn(f'- adapter: approved {answer["at"]}: "{words}"', approved.stdout)
        declined = self.run_cli("decline", "adapter", "use a release instead")
        self.assert_table(declined)
        replaced = self.read_record()["dependencies"]["adapter"]["answer"]
        assert replaced is not None
        self.assertEqual(replaced["answer"], "declined")
        self.assertEqual(replaced["words"], "use a release instead")
        noted = self.run_cli("note", "adapter", "CI needs a feature")
        self.assert_table(noted)
        self.assertIn("Notes:\n- adapter: CI needs a feature", noted.stdout)
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["answer"], replaced)
        forgotten = self.run_cli("forget", "adapter")
        self.assert_table(forgotten)
        self.assertIsNone(self.read_record()["dependencies"]["adapter"]["answer"])
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["note"], "CI needs a feature")
        cleared = self.run_cli("note", "adapter")
        self.assert_table(cleared)
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["note"], "")
        self.assertNotIn("Notes:", cleared.stdout)

    def test_every_successful_verb_refreshes_and_persists_changed_pins(self) -> None:
        self.begin()
        for index, arguments in enumerate((("status",), (), ("start", "0.20"),
                                           ("approve", "adapter", "yes"), ("decline", "adapter", "no"),
                                           ("forget", "adapter"), ("note", "adapter", "why")), start=1):
            with self.subTest(arguments=arguments):
                bevy = f"0.19.{index + 1}"
                self.write_lock(version=f"1.2.{index + 3}", bevy=bevy)
                result = self.run_cli(*arguments)
                self.assert_table(result)
                current = self.read_record()
                self.assertEqual(current["dependencies"]["adapter"]["pin"]["version"], f"1.2.{index + 3}")
                self.assertEqual(current["dependencies"]["adapter"]["bevy"], bevy)
                self.assertEqual(current["bevy"], [bevy])
                self.assertIn(f"on Bevy {bevy}", result.stdout)

    def test_each_successful_run_saves_once_after_the_verbs_change(self) -> None:
        calls = self.root / "save-calls"
        commands: tuple[tuple[str, ...], ...] = (
            ("start", "0.20"), ("check",), ("status",), (), ("start", "0.20"),
            ("approve", "adapter", "use this pin"), ("decline", "adapter", "wait"),
            ("forget", "adapter"), ("note", "adapter", "why"), ("note", "adapter"),
        )
        for index, arguments in enumerate(commands, start=1):
            with self.subTest(arguments=arguments):
                self.write_lock(version=f"1.2.{index}")
                calls.unlink(missing_ok=True)
                self.assert_table(self.run_cli(*arguments, count_saves=True))
                self.assertEqual(calls.read_text(encoding="utf-8"), "saved\n")
                current = self.read_record()["dependencies"]["adapter"]
                self.assertEqual(current["pin"]["version"], f"1.2.{index}")
                verb = arguments[0] if arguments else "status"
                answer = current["answer"]
                if verb in ("approve", "decline"):
                    assert answer is not None
                    assert len(arguments) > 2
                    self.assertEqual(answer["answer"], "approved" if verb == "approve" else "declined")
                    self.assertEqual(answer["words"], arguments[2])
                elif verb == "forget":
                    self.assertIsNone(answer)
                elif verb == "note":
                    self.assertEqual(current["note"], arguments[2] if len(arguments) > 2 else "")

    def test_failed_pin_reads_still_save_each_user_change_once(self) -> None:
        self.begin()
        original = self.read_record()
        (self.checkout / "Cargo.lock").unlink()
        calls = self.root / "save-calls"
        commands: tuple[tuple[str, ...], ...] = (
            ("approve", "adapter", "yes"), ("decline", "adapter", "no"),
            ("forget", "adapter"), ("note", "adapter", "why"), ("note", "adapter"),
        )
        for arguments in commands:
            with self.subTest(arguments=arguments):
                calls.unlink(missing_ok=True)
                result = self.run_cli(*arguments, count_saves=True)
                self.assert_table(result)
                self.assertTrue(result.stdout.startswith("pins could not be re-read: "))
                self.assertEqual(calls.read_text(encoding="utf-8"), "saved\n")
                current = self.read_record()
                self.assertEqual(current["pins_read"], original["pins_read"])
                item = current["dependencies"]["adapter"]
                self.assertEqual(item["pin"], original["dependencies"]["adapter"]["pin"])
                answer = item["answer"]
                if arguments[0] in ("approve", "decline"):
                    assert answer is not None
                    assert len(arguments) > 2
                    self.assertEqual(answer["answer"], "approved" if arguments[0] == "approve" else "declined")
                    self.assertEqual(answer["words"], arguments[2])
                elif arguments[0] == "forget":
                    self.assertIsNone(answer)
                else:
                    self.assertEqual(item["note"], arguments[2] if len(arguments) > 2 else "")

    def test_status_replaces_removed_dependencies_without_approving_new_ones(self) -> None:
        self.begin()
        self.assert_table(self.run_cli("approve", "adapter", "only this crate"))
        self.write_lock(name="replacement")
        self.assert_table(self.run_cli("status"))
        current = self.read_record()
        self.assertEqual(set(current["dependencies"]), {"replacement"})
        self.assertIsNone(current["dependencies"]["replacement"]["answer"])

    def test_non_start_verbs_keep_stored_pins_and_time_when_the_lock_is_unreadable(self) -> None:
        self.begin()
        old = self.read_record()
        read_at = datetime.fromisoformat(old["pins_read"]).astimezone(ZONE).strftime("%H:%M %Z")
        (self.checkout / "Cargo.lock").unlink()
        for arguments in (("status",), ("approve", "adapter", "use the stored pin"),
                          ("decline", "adapter", "no"), ("forget", "adapter"), ("note", "adapter", "why")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assert_table(result)
                first_line = result.stdout.splitlines()[0]
                self.assertTrue(first_line.startswith(f"pins could not be re-read: {self.checkout / 'Cargo.lock'}: "))
                self.assertTrue(first_line.endswith(f"; showing pins read {read_at}"), first_line)
                current = self.read_record()
                self.assertEqual(current["pins_read"], old["pins_read"])
                self.assertEqual(current["dependencies"]["adapter"]["pin"], old["dependencies"]["adapter"]["pin"])
                self.assertEqual(current["bevy"], old["bevy"])
                self.assertIn("git branch=main @ 1234567 on Bevy 0.19.1", result.stdout)

    def test_start_refuses_an_unreadable_lock_and_writes_no_watch(self) -> None:
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("start", "0.20")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith(f"bevy_upgrade: {self.checkout / 'Cargo.lock'}: "))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse((self.root / "state/demo.json").exists())

    def test_record_refusals_print_one_error_and_preserve_the_watch(self) -> None:
        self.begin()
        original = self.read_record()
        cases = (
            (("start", "0.21"), "a watch for 0.20 is open; stop it first"),
            (("start", "0.20.0"), "target must be major.minor, such as 0.20"),
            (("approve", "missing", "yes"), "missing is not tracked; tracked: adapter"),
            (("decline", "missing", "no"), "missing is not tracked; tracked: adapter"),
            (("forget", "missing"), "missing is not tracked; tracked: adapter"),
            (("note", "missing", "why"), "missing is not tracked; tracked: adapter"),
        )
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, count_saves=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"bevy_upgrade: {reason}\n")
                self.assertEqual(self.read_record(), original)
                self.assertFalse((self.root / "save-calls").exists())
                self.assertFalse((self.root / "fetch-calls").exists())

    def test_a_refused_production_doc_prints_one_error(self) -> None:
        _ = self.doc.write_text("# Missing context\n", encoding="utf-8")
        result = self.run_cli("start", "0.20")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "bevy_upgrade: production doc lacks Merge branch\n")

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

    def test_start_checks_from_fixtures_before_its_single_save(self) -> None:
        self.write_fixtures(ready=True)
        result = self.run_cli("start", "0.20", count_saves=True)
        self.assert_table(result)
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        current = self.read_record()
        self.assertEqual(current["target_bevy"], {"status": "released", "version": "0.20.0",
                                                "date": "2026-10-12", "since": "2026-10-12"})
        facts = current["dependencies"]["adapter"]["crates_io"]
        assert facts["status"] == "read"
        self.assertEqual(facts["ready"], "2.0.0")
        self.assertEqual(facts["repository"], "example/adapter")
        self.assertEqual(facts["checked"], current["checked"])
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(), [
            "https://crates.io/api/v1/crates/bevy/versions",
            "https://crates.io/api/v1/crates/adapter",
            "https://crates.io/api/v1/crates/adapter/2.0.0/dependencies",
            "https://crates.io/api/v1/crates/adapter/1.2.3/dependencies",
        ])
        self.assertIn("release ready | bump to 2.0.0", result.stdout)

    def test_check_refreshes_pins_checks_once_and_saves_once(self) -> None:
        self.begin()
        self.write_lock(version="1.2.4", bevy="0.19.2")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(),
                         ["https://crates.io/api/v1/crates/adapter",
                          "https://api.github.com/repos/example/adapter/commits/main",
                          "https://api.github.com/repos/example/adapter/issues?state=all&since=2026-10-12T00:00:00Z&sort=updated&direction=desc&per_page=100"])
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["pin"]["version"], "1.2.4")
        self.assertEqual(current["dependencies"]["adapter"]["bevy"], "0.19.2")
        self.assertIsNotNone(current["checked"])
        self.assertIn("| main @ abcdef0 on Bevy ^0.19; PR #7 open (port @ 1234567) | port in PR | wait for PR #7 |",
                      result.stdout)

    def test_check_failed_pin_read_makes_no_request_or_save_and_exits_one(self) -> None:
        self.begin()
        before = (self.root / "state/demo.json").read_bytes()
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("check", count_saves=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.startswith("pins could not be re-read: "))
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", result.stdout)
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertFalse((self.root / "save-calls").exists())
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)

    def test_check_all_requested_entries_fail_exits_one_preserves_facts_and_saves_once(self) -> None:
        self.begin()
        old = self.read_record()["dependencies"]["adapter"]["crates_io"]
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("last check failed: crates.io: no fixture", result.stdout)
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["crates_io"], old)
        self.assertEqual(current["dependencies"]["adapter"]["error"], "crates.io: no fixture")
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")

    def test_check_one_failed_entry_and_one_successful_entry_exits_zero(self) -> None:
        with (self.checkout / "Cargo.lock").open("a", encoding="utf-8") as stream:
            _ = stream.write('[[package]]\nname = "missing-fixture"\nversion = "1.0.0"\n'
                             + f'source = "{REGISTRY}"\ndependencies = ["bevy_ecs"]\n')
        self.begin()
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["error"], "")
        self.assertEqual(current["dependencies"]["missing-fixture"]["error"], "crates.io: no fixture")
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(), [
            "https://crates.io/api/v1/crates/missing-fixture", "https://crates.io/api/v1/crates/adapter",
            "https://api.github.com/repos/example/adapter/commits/main",
            "https://api.github.com/repos/example/adapter/issues?state=all&since=2026-10-12T00:00:00Z&sort=updated&direction=desc&per_page=100",
        ])

    def test_start_succeeds_when_all_upstream_lookups_fail(self) -> None:
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("start", "0.20", count_saves=True)
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["target_bevy"], {"status": "unread"})
        self.assertEqual(current["target_error"], "crates.io: no fixture")
        self.assertEqual(current["dependencies"]["adapter"]["crates_io"], {"status": "unread"})
        self.assertEqual(current["dependencies"]["adapter"]["error"], "crates.io: no fixture")
        self.assertIsNotNone(current["checked"])
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")

    def test_target_failure_does_not_make_a_successful_entry_check_fail(self) -> None:
        self.begin()
        current = self.read_record()
        current["target_bevy"] = {"status": "unread"}
        _ = (self.root / "state/demo.json").write_text(json.dumps(current), encoding="utf-8")
        bodies = cast(dict[str, object], json.loads((self.root / "fixtures.json").read_text(encoding="utf-8")))
        del bodies["https://crates.io/api/v1/crates/bevy/versions"]
        _ = (self.root / "fixtures.json").write_text(json.dumps(bodies), encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertEqual(self.read_record()["target_error"], "crates.io: no fixture")
        self.assertIn("(stale)", result.stdout)

    def test_check_without_eligible_entry_requests_exits_zero(self) -> None:
        self.write_fixtures(ready=True)
        self.begin()
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")


if __name__ == "__main__":
    _ = unittest.main()
