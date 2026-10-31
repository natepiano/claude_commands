"""Named environment forwarding and per-call folders returned from Mac tests."""

from __future__ import annotations

from dataclasses import replace
import os
import shlex
import signal
import time
from typing import cast

import offload
from offload_test_support import HANA, OffloadCommandFixture, TEST_HANG_GUARD_SECS


class OffloadForwardTests(OffloadCommandFixture):
    def configure_fetch(self, **values: object) -> None:
        # Extend the existing shell fixture; no second command harness/config writer.
        cast(dict[str, object], cast(object, self.fixture)).update(values)
        self.sync_fixture()

    def assert_fetch_wire(self, word: str, mac: str = "passed", reason: str = "") -> None:
        lines = self.result_path.read_text().splitlines()
        values = dict(line.split("=", 1) for line in lines)
        self.assertEqual(set(values), {"mac", "reason", "seconds", "fetch"})
        self.assertEqual(len(lines), 4)
        self.assertEqual((values["mac"], values["reason"], values["fetch"]), (mac, reason, word))
        self.assertGreaterEqual(float(values["seconds"]), 0)

    def test_named_environment_reaches_the_remote_test_command(self) -> None:
        self.environment["SHOT_OUTPUT"] = "target/shots"
        answer = self.command(
            "--workspace", runner_options=("--env", "SHOT_OUTPUT"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        remote = self.events_for("ssh")[-1]["args"][-1]
        self.assertIn("SHOT_OUTPUT=target/shots", shlex.split(remote))

    def test_values_keep_their_bytes_and_only_named_variables_are_forwarded(self) -> None:
        value = "spaces 'quotes' \"double\"\nand a newline; $(do-not-run)"
        self.environment.update({"SHOT_OUTPUT": value, "UNNAMED_PRIVATE_VALUE": "never sent"})
        answer = self.command(
            "--workspace", runner_options=("--env", "SHOT_OUTPUT", "--env", "PATH", "--env", "SHOT_OUTPUT"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        remote = self.events_for("ssh")[-1]["args"][-1]
        words = shlex.split(remote)
        self.assertEqual(words[3:6], [f"SHOT_OUTPUT={value}", f"PATH={self.environment['PATH']}", f"SHOT_OUTPUT={value}"])
        self.assertEqual(words[6], "PATH=$HOME/.cargo/bin:$PATH")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", words)
        self.assertNotIn("UNNAMED_PRIVATE_VALUE", remote)
        self.assertNotIn("never sent", remote)
        self.assert_wire("passed", "")

    def test_default_remote_command_is_byte_equal_and_fetch_alone_keeps_artifacts(self) -> None:
        request = offload.OffloadRequest(self.repository, "hana", "call-forward", offload.NO_RESULT_FILE, ("--workspace",))
        config = offload.read_config(self.config_path)
        expected = (
            'cd .local/state/mac-test/mirror/hana && PATH="$HOME/.cargo/bin:$PATH" '
            + 'BUILDLOG_CALLER=verify-mac BUILDLOG_CALL_ID=call-forward BUILDLOG_SYNC=1 '
            + 'LINT_SKIP_SWEEP=1 LINT_SWEEP_BUDGET_GIB=64 MACOSX_DEPLOYMENT_TARGET=14.0 '
            + '~/.claude/scripts/lint/lint nextest --workspace; echo mac_exit=$?; '
            + 'if free_gib=$(df -g "$HOME"); then '
            + "free_gib=$(printf '%s\\n' \"$free_gib\" | awk 'NR == 2 {print $4}'); "
            + 'else free_gib=; fi; if awk -v free="$free_gib" -v floor=60 '
            + "'BEGIN { exit !(free ~ /^[0-9]+([.][0-9]+)?$/ && (free + 0) < (floor + 0)) }'; then "
            + 'if rm -rf -- "$HOME"/.local/state/mac-test/mirror/hana/target; then echo mac_cleanup=0; '
            + 'else echo mac_cleanup=$?; fi; else echo mac_cleanup=kept; fi'
        )
        self.assertEqual(offload.remote_test_command(config, request, HANA), expected)
        fetch = offload.remote_test_command(config, replace(request, fetch=offload.FetchFolder("target/shots")), HANA)
        self.assertIn("cd .local/state/mac-test/mirror/hana && rm -rf -- target/shots && PATH=", fetch)
        self.assertTrue(fetch.endswith("; echo mac_cleanup=kept"))
        self.assertNotIn("df -g", fetch)
        self.assertNotIn("rm -rf -- target/shots", expected)

    def test_argument_refusals_start_no_work(self) -> None:
        self.environment["SHOT_OUTPUT"] = ""
        _ = self.environment.pop("NOT_SET_HERE", None)
        cases = [
            ("--env",), ("--fetch",), ("--env", "bad-name"), ("--env", "1BAD"),
            ("--env", "NOT_SET_HERE"), ("--env", "SHOT_OUTPUT", "--verb", "check"),
            ("--env", "SHOT_OUTPUT", "--copy"), ("--fetch", "target/shots", "--verb", "doc"),
            ("--fetch", "target/shots", "--copy"),
        ]
        cases.extend(("--fetch", folder) for folder in (
            "", "/tmp/shots", "-shots", ".", "..", "target/../shots", "target/./shots", "target//shots",
            "target/shots//", "target/shot space", "target/shot;bad", "target/shots\n", "target/$(bad)",
        ))
        for options in cases:
            with self.subTest(options=options):
                answer = self.command(
                    "--workspace", runner_options=options, call_id="call-forward",
                    subprocess_cli=True, separate_stderr=True,
                )
                self.assertEqual(answer.returncode, 2, answer.stdout + answer.stderr)
                self.assertIn("error:", answer.stderr)
                self.assertEqual(self.events(), [])
                self.assertFalse(self.result_path.exists())
                self.assertFalse(self.claim_file.exists())

    def test_argument_refusal_removes_the_previous_result(self) -> None:
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"),
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertTrue(self.result_path.exists())
        self.clear_events()
        answer = self.command(
            "--workspace", runner_options=("--env", "bad-name"),
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 2, answer.stdout + answer.stderr)
        self.assertFalse(self.result_path.exists())
        self.assertEqual(self.events(), [])

    def test_empty_value_is_forwarded(self) -> None:
        self.environment["SHOT_OUTPUT"] = ""
        answer = self.command(
            "--workspace", runner_options=("--env", "SHOT_OUTPUT"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertIn("SHOT_OUTPUT=", shlex.split(self.events_for("ssh")[-1]["args"][-1]))

    def test_fetch_publishes_files_before_releasing_claim(self) -> None:
        self.configure_fetch(fetch_contents="PNG bytes\n", fetch_answer="fetch_kib=12\nrc=0")
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots/"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        landing = self.state_directory / "fetched/call-forward/target/shots"
        self.assertEqual((landing / "shot.png").read_text(), "PNG bytes\n")
        self.assertFalse((self.state_directory / "fetched/call-forward.partial").exists())
        self.assert_fetch_wire("fetched")
        self.assertIn(f"mac_test: fetched target/shots from the Mac to {landing} (12 KiB)", answer.stdout)
        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "rsync", "ssh", "ssh", "rsync"])
        self.assertTrue(all(event["claimed"] for event in events))
        self.assertFalse(self.claim_file.exists())
        self.assertIn("du -sk target/shots", events[-2]["args"][-1])
        pull = events[-1]["args"]
        self.assertEqual(pull[:4], ["-rlp", "--safe-links", "-e", shlex.join(["ssh", *offload.ssh_options(offload.read_config(self.config_path))])])
        self.assertEqual(pull[-2], "mac:.local/state/mac-test/mirror/hana/target/shots/")
        self.assertTrue(pull[-1].endswith("/fetched/call-forward.partial/target/shots/"))

    def test_fetch_missing_does_not_pull(self) -> None:
        self.configure_fetch(fetch_answer="fetch=missing\nrc=0")
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assert_fetch_wire("missing")
        self.assertIn("mac_test: nothing fetched: target/shots is missing on the Mac", answer.stdout)
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_fetch_limit_stops_an_oversized_pull(self) -> None:
        self.config["fetch_limit_mib"] = "1"
        self.sync_config()
        self.configure_fetch(fetch_answer="fetch_kib=1025\nrc=0")
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assert_fetch_wire("too_large")
        self.assertIn("over the 1 MiB limit", answer.stdout)
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_failed_pull_removes_partial_files(self) -> None:
        self.configure_fetch(pull_status=23)
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assert_fetch_wire("failed")
        self.assertEqual(list((self.state_directory / "fetched").iterdir()), [])
        self.assertFalse(self.claim_file.exists())

    def test_bad_size_answer_or_probe_failure_gives_failed_without_pull(self) -> None:
        for response, status in (("fetch_kib=broken\nrc=0", 0), ("fetch_kib=4\nrc=0", 255), ("fetch_kib=4", 0)):
            with self.subTest(response=response, status=status):
                self.clear_events()
                self.configure_fetch(fetch_answer=response, fetch_probe_status=status)
                answer = self.command(
                    "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
                    subprocess_cli=True, separate_stderr=True,
                )
                self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                self.assert_fetch_wire("failed")
                self.assertEqual(len(self.events_for("rsync")), 1)

    def test_size_probe_uses_its_deadline_and_releases_the_claim(self) -> None:
        self.config["probe_timeout_s"] = "0.5"
        self.sync_config()
        self.configure_fetch(hold_fetch_probe=True)
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertTrue((self.stub_directory / "fetch-probe-started").exists())
        self.assert_fetch_wire("failed")
        self.assertEqual(len(self.events_for("rsync")), 1)
        self.assertFalse(self.claim_file.exists())

    def test_fetch_after_failure_and_no_tests_preserves_the_mac_status(self) -> None:
        for status, wire_status, reason, exit_code in ((37, "failed", "", 37), (4, "declined", "no_tests", 75)):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.sync_fixture()
                answer = self.command(
                    "--workspace", runner_options=("--fetch", f"target/shots-{status}", "--call-id", f"call-{status}"), call_id="call-forward",
                    subprocess_cli=True, separate_stderr=True,
                )
                self.assertEqual(answer.returncode, exit_code, answer.stdout + answer.stderr)
                self.assert_fetch_wire("fetched", wire_status, reason)
                self.assertTrue((self.state_directory / f"fetched/call-{status}/target/shots-{status}/shot.png").exists())

    def test_lost_and_declined_runs_do_not_fetch(self) -> None:
        self.fixture["run_status_reported"] = False
        self.sync_fixture()
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 75, answer.stdout + answer.stderr)
        self.assert_fetch_wire("not_run", "lost")
        self.assertFalse((self.stub_directory / "pull-started").exists())
        self.config["offload"] = "off"
        self.sync_config()
        self.clear_events()
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 75, answer.stdout + answer.stderr)
        self.assert_fetch_wire("not_run", "declined", "off")
        self.assertEqual(self.events(), [])

    def test_existing_destination_is_not_overwritten(self) -> None:
        landing = self.state_directory / "fetched/call-forward"
        landing.mkdir(parents=True)
        _ = (landing / "keep").write_text("original")
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assert_fetch_wire("failed")
        self.assertEqual((landing / "keep").read_text(), "original")
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_prune_removes_only_entries_older_than_fourteen_days(self) -> None:
        fetched = self.state_directory / "fetched"
        old = time.time() - 15 * 24 * 60 * 60
        for name in ("old-call", "old-call.partial", "recent-call"):
            folder = fetched / name
            folder.mkdir(parents=True)
            _ = (folder / "keep").write_text(name)
            if name.startswith("old-"):
                os.utime(folder, (old, old))
        self.configure_fetch(fetch_answer="fetch=missing\nrc=0")
        answer = self.command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
            subprocess_cli=True, separate_stderr=True,
        )
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertFalse((fetched / "old-call").exists())
        self.assertFalse((fetched / "old-call.partial").exists())
        self.assertEqual((fetched / "recent-call/keep").read_text(), "recent-call")

    def test_signal_during_pull_removes_partial_and_checks_leftovers_before_release(self) -> None:
        self.configure_fetch(hold_pull=True)
        process = self.start_command(
            "--workspace", runner_options=("--fetch", "target/shots"), call_id="call-forward",
        )
        try:
            self.wait_for_path(self.stub_directory / "pull-started")
            self.assertTrue((self.state_directory / "fetched/call-forward.partial").is_dir())
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 143, output)
        self.assertFalse((self.state_directory / "fetched/call-forward.partial").exists())
        self.assertFalse((self.state_directory / "fetched/call-forward").exists())
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(len(self.leftover_events()), 1)
        self.assertTrue(self.leftover_events()[0]["claimed"])
        self.assertFalse(self.result_path.exists())
