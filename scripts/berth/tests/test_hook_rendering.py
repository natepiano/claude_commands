#!/usr/bin/env python3
"""What each installed cargo-berth hook wrapper does on its own."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import cast

from berth.tests.installed_front_end import (
    INSTALL_SCRIPT,
    POST_BASH_HOOK,
    PRE_EDIT_HOOK,
    SESSION_START_HOOK,
    InstalledFrontEndFixture,
)


class HookWrapperTests(InstalledFrontEndFixture):
    """Assert the wrapper contract and the installer that publishes it."""

    def test_installed_engine_hook_renders_the_exact_session_id_contract(self) -> None:
        fixture = self.new_scratch_post_tool_use_repository(2)
        self.addCleanup(fixture.temporary_directory.cleanup)
        exact_detail = (
            "STOP: `cargo-berth hook post-tool-use` requires valid JSON, tool_name "
            "Bash, a session_id of 1 to 256 characters with no control characters, "
            "and a cwd that is a string when it is present. Run `cargo-berth drift "
            "--reservation <id> --json` by hand."
        )
        engine_call = ["hook", "post-tool-use"]

        accepted = self.run_installed_engine_hook(
            fixture,
            session_id="é" * 256,
        )
        self.assertEqual(accepted.process.returncode, 0)
        self.assertEqual(accepted.cargo_berth_calls, [engine_call])
        accepted_rendering = accepted.process.stdout + accepted.process.stderr
        self.assertNotIn(
            "cargo-berth rejected an invalid PostToolUse payload",
            accepted_rendering,
        )
        self.assertNotIn(exact_detail, accepted_rendering)

        rejected_session_ids = (
            ("257 multibyte characters", "é" * 257),
            ("control character", "fixture\x1fsession"),
        )
        for fixture_name, session_id in rejected_session_ids:
            with self.subTest(fixture=fixture_name):
                rejected = self.run_installed_engine_hook(
                    fixture,
                    session_id=session_id,
                )
                self.assertEqual(rejected.process.returncode, 0)
                self.assertEqual(rejected.cargo_berth_calls, [engine_call])
                self.assertEqual(
                    self.rendered_post_tool_use_detail(rejected.process.stdout),
                    exact_detail,
                )
                decoded = cast(
                    dict[str, object], json.loads(rejected.process.stdout)
                )
                self.assertEqual(
                    decoded["systemMessage"],
                    "cargo-berth rejected an invalid PostToolUse payload.",
                )

    def test_every_wrapper_states_its_own_binary_absent_failure(self) -> None:
        """Each wrapper's answer when there is no engine to ask.

        This is the one text the front end produces on its own, so it is the one
        behavior that cannot move into the engine and the only reason this file
        still runs a hook. `tests/front_end_corpus.rs` names this test as what
        replaced the three frozen `-missing-validator` entries, so the three
        shapes asserted here are the ones those entries froze: the pre-edit
        wrapper fails closed on exit 2 with its refusal on standard error, and
        the other two exit 0 with a static repair notice as a protocol object on
        standard output.
        """

        engineless_path = os.pathsep.join(
            directory
            for directory in self.base_environment["PATH"].split(os.pathsep)
            if not (Path(directory) / "cargo-berth").exists()
        )
        self.assertIsNone(shutil.which("cargo-berth", path=engineless_path))

        cases = (
            (PRE_EDIT_HOOK, self.pre_edit_payload(), 2, True),
            (POST_BASH_HOOK, self.post_bash_payload(), 0, False),
            (SESSION_START_HOOK, self.session_start_payload(), 0, False),
        )
        for hook, payload, expected_exit_code, states_on_stderr in cases:
            with self.subTest(hook=hook.name):
                environment = {**self.base_environment, "PATH": engineless_path}
                completed = subprocess.run(
                    [str(hook)],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected_exit_code)
                stated = completed.stderr if states_on_stderr else completed.stdout
                unused = completed.stdout if states_on_stderr else completed.stderr
                self.assertEqual(unused, "")
                self.assertIn("cargo-berth", stated)
                self.assertIn("not on PATH", stated)

                if states_on_stderr:
                    self.assertTrue(
                        stated.startswith("cargo-berth refused this edit hook request: ")
                    )
                    continue

                published = cast(dict[str, object], json.loads(stated))
                self.assertEqual(
                    published["systemMessage"],
                    "cargo-berth hook installation needs repair.",
                )
                specific = cast(dict[str, object], published["hookSpecificOutput"])
                self.assertEqual(
                    specific["hookEventName"],
                    "PostToolUse" if hook is POST_BASH_HOOK else "SessionStart",
                )
                self.assertIn(
                    "not on PATH", cast(str, specific["additionalContext"])
                )

    def test_installer_arms_rollback_after_complete_prior_backups(self) -> None:
        source = INSTALL_SCRIPT.read_text(encoding="utf-8")
        cleanup_trap = source.index("trap cleanup_staging EXIT HUP INT TERM")
        binary_backup = source.index(
            'cp -p -- "$binary_path" "$staging_directory/previous-cargo-berth"'
        )
        rollback_trap = source.index("trap rollback_installation EXIT HUP INT TERM")
        engine_build = source.index("step='engine build'")
        binary_publication = source.index("binary_publication_state=ReplacementStarted")

        self.assertLess(cleanup_trap, binary_backup)
        self.assertLess(binary_backup, rollback_trap)
        self.assertLess(rollback_trap, engine_build)
        self.assertLess(engine_build, binary_publication)
        self.assertIn(
            "if [[ $installed -eq 0 && $binary_publication_state == ReplacementStarted ]]",
            source,
        )
        self.assertIn(
            'cp -p -- "$staging_directory/previous-cargo-berth" "$binary_path"',
            source,
        )

        for retired_generated_artifact in (
            "staged_generated",
            "previous-generated",
            "generated_publication_state",
            "generated_existed",
            "consumer_artifacts",
            "py_compile",
        ):
            with self.subTest(retired=retired_generated_artifact):
                self.assertNotIn(retired_generated_artifact, source)


if __name__ == "__main__":
    _ = unittest.main()
