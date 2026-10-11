#!/usr/bin/env python3
"""Specification tests for the feature-log note writer and reader."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import fcntl
from io import StringIO
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Literal, NamedTuple, Protocol, TypedDict, cast, final, override
from unittest.mock import patch

import feature_log
from feature_log import locked, state_dir, vault_dir


TODAY = "2026-10-10"
EVENT_TIME = "2026-10-10T12:34:56-07:00"
SHA_ONE = "0000000000000000000000000000000000000001"
SHA_TWO = "0000000000000000000000000000000000000002"
SHA_THREE = "0000000000000000000000000000000000000003"

HANA_EXAMPLE = """---
area: "[[widgets]]"
completed_date: "[[2026-10-09]]"
crate:
  - hana
  - hana_catalyst
  - hana_diegetic
date_created: "[[2026-10-10]]"
date_modified: "[[2026-10-10]]"
kind: feature
link: https://github.com/natepiano/hana/commit/afb0f5e992069a3e2d948bfff093292eff8f41a4
production: tool-based-ui
project: "[[hana]]"
release: []
replaced_by: []
return:
  - "[[feature-log - hana.base]]"
  - "[[feature-log.base]]"
state: live
summary: A number row shows the live value arriving through its cable.
tags: [feature-log]
unit:
  - organon
---
A cabled number row now reports the connected value where the value is edited
and inspected. A changing signal no longer needs another panel just to read it.

## See it

Connect a changing number output to a number row. The displayed value follows
the cable.

## Evidence

- Showrunner merge `afb0f5e9`, from checkpoint `98f7d68a`.
"""

NATEDEV_EXAMPLE = """---
area: "[[builds]]"
completed_date: "[[2026-10-04]]"
crate: []
date_created: "[[2026-10-10]]"
date_modified: "[[2026-10-10]]"
kind: feature
link: https://github.com/natepiano/nixos/commit/408b3d881969ac71ec9d622708f0b40dcb2e1de4
project: "[[natedev]]"
release: []
replaced_by: []
return:
  - "[[feature-log - natedev.base]]"
  - "[[feature-log.base]]"
state: live
summary: Every local build passes through one machine-wide admission gate.
tags: [feature-log]
---
Local Cargo builds now share one admission gate before they draw work from the
machine. Sessions can no longer start enough independent builds to crowd each
other out.

## See it

Start builds in two sessions. Both draw from the same natedev build capacity.

## Evidence

- Activated NixOS configuration commit `408b3d88`.
"""


class CliResult(NamedTuple):
    status: int
    stdout: str
    stderr: str


class AddEvent(TypedDict):
    at: str
    verb: Literal["add"]
    project: str
    title: str
    kind: str
    summary: str
    release: list[str]
    backfill: bool


class SupersedeEvent(TypedDict):
    at: str
    verb: Literal["supersede"]
    project: str
    title: str
    kind: str
    summary: str
    release: list[str]
    backfill: bool
    old: list[str]


class LegacyAmendEvent(TypedDict):
    """An amendment written before provenance snapshots were supported."""

    at: str
    verb: Literal["amend"]
    project: str
    title: str
    fields: list[str]
    backfill: bool


class AmendEvent(TypedDict):
    at: str
    verb: Literal["amend"]
    project: str
    title: str
    fields: list[str]
    backfill: bool
    old_link: str
    new_link: str
    old_completed: str
    new_completed: str
    releases_added: list[str]


FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent


class ProposalArguments(TypedDict):
    project: str
    title: str
    kind: str
    summary: str
    behavior: str
    see: str
    link: str
    evidence: list[str]
    crate: list[str]
    area: str | None
    completed: str
    production: str | None
    unit: list[str]
    release: list[str]
    backfill: bool
    separate_result: bool
    old: list[str]


class OpenProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArguments
    status: Literal["open"]
    at: str


class LandedProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArguments
    status: Literal["landed"]
    at: str
    note: str


class DroppedProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArguments
    status: Literal["dropped"]
    at: str
    verdict: str


FeatureLogProposal = OpenProposal | LandedProposal | DroppedProposal


class DigestCursor(TypedDict):
    line: int
    at: str


class SshRunner(Protocol):
    def __call__(self, command: list[str]) -> int: ...


class CliInvocation(Protocol):
    def __call__(self) -> int: ...


@final
class RecordingSshRunner:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> int:
        self.commands.append(command.copy())
        print(self.stdout, end="")
        if self.stderr:
            print(self.stderr, end="", file=sys.stderr)
        return self.returncode


@final
class RecordingGitRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        del options
        self.commands.append(command.copy())
        return subprocess.CompletedProcess[str](
            command,
            0,
            stdout=self.stdout,
            stderr="",
        )


class FeatureLogTests(unittest.TestCase):
    root: Path = Path()
    vault: Path = Path()
    state: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.vault = self.root / "vault"
        state_home = self.root / "state-home"
        self.state = state_home / "claude/feature-log"
        _environment = cast(
            dict[str, str],
            self.enterContext(
                patch.dict(
                    os.environ,
                    {
                        "FEATURE_LOG_VAULT": str(self.vault),
                        "XDG_STATE_HOME": str(state_home),
                    },
                )
            ),
        )
        _ = self.enterContext(patch.object(feature_log, "today", return_value=TODAY))
        _ = self.enterContext(
            patch.object(feature_log, "_event_time", return_value=EVENT_TIME)
        )

    def cli(self, arguments: list[str]) -> CliResult:
        return self.capture_cli(lambda: feature_log.main(arguments))

    def cli_with_runner(
        self, arguments: list[str], runner: SshRunner
    ) -> CliResult:
        return self.capture_cli(
            lambda: feature_log.main(arguments, ssh_runner=runner)
        )

    def capture_cli(self, invocation: CliInvocation) -> CliResult:
        stdout = StringIO()
        stderr = StringIO()
        status = 0
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                returned = invocation()
                status = returned
            except SystemExit as error:
                if not isinstance(error.code, int):
                    raise
                status = error.code
        return CliResult(status, stdout.getvalue(), stderr.getvalue())

    def add_arguments(
        self,
        *,
        project: str = "hana",
        title: str = "A useful result",
        kind: str = "feature",
        summary: str = "People can use the useful result.",
        behavior: str = "The useful behavior is available now.",
        see: str = "Exercise the useful behavior.",
        link: str = SHA_ONE,
        crates: tuple[str, ...] = ("hana",),
        evidence: tuple[str, ...] = ("Accepted by the user.",),
    ) -> list[str]:
        arguments = [
            "add",
            "--project",
            project,
            "--title",
            title,
            "--kind",
            kind,
            "--summary",
            summary,
            "--behavior",
            behavior,
            "--see",
            see,
            "--link",
            link,
        ]
        for crate in crates:
            arguments.extend(("--crate", crate))
        for line in evidence:
            arguments.extend(("--evidence", line))
        return arguments

    def proposal_arguments(
        self,
        row: str,
        add_arguments: list[str],
        *,
        old: tuple[str, ...] = (),
    ) -> list[str]:
        arguments = add_arguments.copy()
        arguments[0] = "propose"
        arguments[1:1] = ["--review", row]
        for title in old:
            arguments.extend(("--old", title))
        return arguments

    def note(self, project: str, title: str) -> Path:
        return self.vault / "feature-log" / project / f"{title}.md"

    def vault_snapshot(self) -> dict[str, bytes]:
        if not self.vault.exists():
            return {}
        snapshot: dict[str, bytes] = {}
        for path in sorted(self.vault.rglob("*")):
            relative = path.relative_to(self.vault).as_posix()
            snapshot[relative] = b"<directory>" if path.is_dir() else path.read_bytes()
        return snapshot

    def events(self) -> list[FeatureLogEvent]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [
            cast(FeatureLogEvent, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def write_events(self, events: list[FeatureLogEvent]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        contents = "".join(json.dumps(event) + "\n" for event in events)
        _ = (self.state / "events.jsonl").write_text(contents, encoding="utf-8")

    def proposal(self, row: str) -> FeatureLogProposal:
        path = self.state / "proposals" / f"{row}.json"
        return cast(FeatureLogProposal, json.loads(path.read_text(encoding="utf-8")))

    def digest_cursor(self) -> DigestCursor:
        path = self.state / "digest.json"
        return cast(DigestCursor, json.loads(path.read_text(encoding="utf-8")))

    def assert_rejected(self, arguments: list[str], reason: str) -> None:
        before_vault = self.vault_snapshot()
        before_events = self.events()
        result = self.cli(arguments)
        self.assertEqual(result.status, 2)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, r"^feature_log: [^\n]+\n$")
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual(self.events(), before_events)
        self.assert_no_temporary_files()

    def assert_no_temporary_files(self) -> None:
        leftovers = [
            path
            for root in (self.vault, self.state)
            if root.exists()
            for path in root.rglob(".*.tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_public_paths_follow_environment_and_locked_uses_current_state(self) -> None:
        alternate_vault = self.root / "alternate-vault"
        alternate_state_home = self.root / "alternate-state-home"
        with patch.dict(
            os.environ,
            {
                "FEATURE_LOG_VAULT": str(alternate_vault),
                "XDG_STATE_HOME": str(alternate_state_home),
            },
        ):
            self.assertEqual(vault_dir(), alternate_vault)
            expected_state = alternate_state_home / "claude/feature-log"
            self.assertEqual(state_dir(), expected_state)

            lock_path = expected_state / "lock"
            with locked():
                self.assertTrue(lock_path.is_file())
                with lock_path.open("a+", encoding="utf-8") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            with lock_path.open("a+", encoding="utf-8") as contender:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(contender.fileno(), fcntl.LOCK_UN)

    def test_non_natedev_host_forwards_to_the_one_vault(self) -> None:
        arguments = ["live", "--project", "natedev", "--grep", "two words"]
        runner = RecordingSshRunner(
            stdout="remote list\n",
            stderr="remote warning\n",
            returncode=7,
        )
        with patch.dict(os.environ, {}, clear=False):
            _ = os.environ.pop("FEATURE_LOG_VAULT")
            with patch.object(
                socket, "gethostname", return_value="macbook.local"
            ):
                result = self.cli_with_runner(arguments, runner)

        remote = (
            "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py "
            + shlex.join(arguments)
        )
        self.assertEqual(runner.commands, [["ssh", "natedev", remote]])
        self.assertEqual(result, CliResult(7, "remote list\n", "remote warning\n"))

    def test_natedev_and_explicit_vault_calls_stay_local(self) -> None:
        natedev_runner = RecordingSshRunner(returncode=99)
        with patch.dict(os.environ, {}, clear=False):
            _ = os.environ.pop("FEATURE_LOG_VAULT")
            with (
                patch.object(
                    socket,
                    "gethostname",
                    return_value="natedev.example",
                ),
                patch.object(feature_log, "vault_dir", return_value=self.vault),
            ):
                natedev_result = self.cli_with_runner(
                    ["live", "--project", "natedev"], natedev_runner
                )
        self.assertEqual(natedev_result, CliResult(0, "", ""))
        self.assertEqual(natedev_runner.commands, [])

        explicit_runner = RecordingSshRunner(returncode=99)
        with (
            patch.object(
                socket,
                "gethostname",
                return_value="macbook.example",
            ),
            patch.object(feature_log, "vault_dir", return_value=self.vault),
        ):
            explicit_result = self.cli_with_runner(
                ["live", "--project", "natedev"], explicit_runner
            )
        self.assertEqual(explicit_result, CliResult(0, "", ""))
        self.assertEqual(explicit_runner.commands, [])

    def test_project_of_never_forwards_and_maps_supported_origin_forms(self) -> None:
        cases = (
            ("git@github.com:natepiano/cargo-liner.git\n", "cargo-liner"),
            ("https://github.com/natepiano/bevy_brp\n", "bevy_brp"),
            ("https://github.com/someone/unknown.git\n", "none"),
        )
        checkout = self.root / "checkout"
        for origin, expected in cases:
            with self.subTest(origin):
                ssh_runner = RecordingSshRunner(returncode=99)
                git_runner = RecordingGitRunner(origin)
                with patch.dict(os.environ, {}, clear=False):
                    _ = os.environ.pop("FEATURE_LOG_VAULT")
                    with (
                        patch.object(
                            socket,
                            "gethostname",
                            return_value="macbook.example",
                        ),
                        patch.object(
                            subprocess,
                            "run",
                            side_effect=git_runner,
                        ),
                    ):
                        result = self.cli_with_runner(
                            ["project-of", str(checkout)], ssh_runner
                        )
                self.assertEqual(result, CliResult(0, f"{expected}\n", ""))
                self.assertEqual(ssh_runner.commands, [])
                self.assertEqual(
                    git_runner.commands,
                    [["git", "-C", str(checkout), "remote", "get-url", "origin"]],
                )

    def test_hana_production_example_is_written_byte_for_byte(self) -> None:
        arguments = self.add_arguments(
            title="Live Values on Cabled Number Rows",
            summary="A number row shows the live value arriving through its cable.",
            behavior=(
                "A cabled number row now reports the connected value where the value is edited\n"
                "and inspected. A changing signal no longer needs another panel just to read it."
            ),
            see=(
                "Connect a changing number output to a number row. The displayed value follows\n"
                "the cable."
            ),
            link="afb0f5e992069a3e2d948bfff093292eff8f41a4",
            crates=("hana_diegetic", "hana", "hana_catalyst"),
            evidence=("Showrunner merge `afb0f5e9`, from checkpoint `98f7d68a`.",),
        )
        arguments.extend(
            (
                "--area",
                "widgets",
                "--completed",
                "2026-10-09",
                "--production",
                "tool-based-ui",
                "--unit",
                "organon",
                "--backfill",
            )
        )

        result = self.cli(arguments)

        path = self.note("hana", "live values on cabled number rows")
        self.assertEqual(result, CliResult(0, f"added: {path.relative_to(self.vault)}\n", ""))
        self.assertEqual(path.read_text(encoding="utf-8"), HANA_EXAMPLE)
        self.assert_no_temporary_files()

    def test_natedev_example_is_written_byte_for_byte(self) -> None:
        arguments = self.add_arguments(
            project="natedev",
            title="One Admission for Every Build",
            summary="Every local build passes through one machine-wide admission gate.",
            behavior=(
                "Local Cargo builds now share one admission gate before they draw work from the\n"
                "machine. Sessions can no longer start enough independent builds to crowd each\n"
                "other out."
            ),
            see="Start builds in two sessions. Both draw from the same natedev build capacity.",
            link="408b3d881969ac71ec9d622708f0b40dcb2e1de4",
            crates=(),
            evidence=("Activated NixOS configuration commit `408b3d88`.",),
        )
        arguments.extend(("--area", "builds", "--completed", "2026-10-04", "--backfill"))

        result = self.cli(arguments)

        path = self.note("natedev", "one admission for every build")
        self.assertEqual(result, CliResult(0, f"added: {path.relative_to(self.vault)}\n", ""))
        self.assertEqual(path.read_text(encoding="utf-8"), NATEDEV_EXAMPLE)

    def test_all_basic_rejections_leave_the_vault_unchanged(self) -> None:
        cases: list[tuple[str, list[str], str]] = [
            ("unknown project", self.add_arguments(project="other"), "project"),
            ("unknown kind", self.add_arguments(kind="documentation"), "kind"),
            ("foreign crate", self.add_arguments(crates=("cargo-mend",)), "crate"),
            ("missing crate", self.add_arguments(crates=()), "crate"),
            (
                "natedev crate",
                self.add_arguments(project="natedev", crates=("hana",)),
                "crate",
            ),
            (
                "release crate absent",
                self.add_arguments() + ["--release", "hana_lagrange@1.2.3"],
                "release",
            ),
            (
                "release crate unpublished",
                self.add_arguments() + ["--release", "hana@1.2.3"],
                "published",
            ),
            (
                "bad release version",
                self.add_arguments(crates=("hana_lagrange",))
                + ["--release", "hana_lagrange@1.2"],
                "version",
            ),
            ("empty summary", self.add_arguments(summary=""), "summary"),
            ("empty behavior", self.add_arguments(behavior=""), "behavior"),
            ("empty see", self.add_arguments(see=""), "see"),
            ("empty link", self.add_arguments(link=""), "link"),
            ("malformed link", self.add_arguments(link="not-a-commit"), "link"),
            (
                "link for another repository",
                self.add_arguments(
                    link=f"https://github.com/natepiano/cargo-liner/commit/{SHA_ONE}"
                ),
                "link",
            ),
            ("no evidence", self.add_arguments(evidence=()), "evidence"),
            ("bullet summary", self.add_arguments(summary="- not a summary"), "summary"),
            ("multiline summary", self.add_arguments(summary="first\nsecond"), "summary"),
            (
                "production outside hana",
                self.add_arguments(project="natedev", crates=()) + ["--production", "machine"],
                "production",
            ),
            (
                "unit outside hana",
                self.add_arguments(project="natedev", crates=()) + ["--unit", "system"],
                "unit",
            ),
            ("empty normalized title", self.add_arguments(title="--- !!!"), "title"),
            ("long normalized title", self.add_arguments(title="a" * 51), "50"),
            (
                "bad completion date",
                self.add_arguments() + ["--completed", "October 10"],
                "completed",
            ),
        ]

        for label, arguments, reason in cases:
            with self.subTest(label):
                self.assert_rejected(arguments, reason)

    def test_release_values_are_derived_from_published_crates(self) -> None:
        ordinary = self.add_arguments(
            title="Ordinary release derivation",
            crates=("hana", "hana_lagrange"),
        )
        backfill = self.add_arguments(
            title="Backfill release derivation",
            link=SHA_TWO,
            crates=("hana", "hana_lagrange"),
        ) + ["--backfill"]
        explicit = self.add_arguments(
            title="Explicit release derivation",
            link=SHA_THREE,
            crates=("hana_lagrange",),
        ) + ["--release", "hana_lagrange@1.2.3-alpha.1"]

        self.assertEqual(self.cli(ordinary).status, 0)
        self.assertEqual(self.cli(backfill).status, 0)
        self.assertEqual(self.cli(explicit).status, 0)

        self.assertIn(
            "release:\n  - hana_lagrange@Unreleased\n",
            self.note("hana", "ordinary release derivation").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "release: []\n",
            self.note("hana", "backfill release derivation").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "release:\n  - hana_lagrange@1.2.3-alpha.1\n  - hana_lagrange@Unreleased\n",
            self.note("hana", "explicit release derivation").read_text(encoding="utf-8"),
        )

    def test_backfill_uses_only_explicit_release_values(self) -> None:
        arguments = self.add_arguments(
            title="Released backfill",
            crates=("hana_lagrange",),
        ) + ["--backfill", "--release", "hana_lagrange@2.3.4"]

        self.assertEqual(self.cli(arguments).status, 0)

        contents = self.note("hana", "released backfill").read_text(encoding="utf-8")
        self.assertIn("release:\n  - hana_lagrange@2.3.4\n", contents)
        self.assertNotIn("Unreleased", contents)

    def test_title_normalization_keeps_underscores_and_enforces_the_limit(self) -> None:
        result = self.cli(self.add_arguments(title="  Hello--Crate_name!!  There  "))
        self.assertEqual(result.status, 0)
        self.assertTrue(self.note("hana", "hello crate_name there").is_file())

        exactly_fifty = self.add_arguments(title="b" * 50, link=SHA_TWO)
        self.assertEqual(self.cli(exactly_fifty).status, 0)
        self.assertTrue(self.note("hana", "b" * 50).is_file())

        self.assert_rejected(self.add_arguments(title="c" * 51, link=SHA_THREE), "50")

    def test_same_normalized_title_with_a_different_link_is_rejected(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Same Result", link=SHA_ONE)).status, 0)
        self.assert_rejected(
            self.add_arguments(title="Same--Result!!", link=SHA_TWO),
            "specific",
        )

    def test_exact_repeat_is_unchanged_and_changed_repeat_requires_amend(self) -> None:
        arguments = self.add_arguments(title="Repeatable Result")
        first = self.cli(arguments)
        original = self.note("hana", "repeatable result").read_bytes()

        repeated = self.cli(arguments)

        relative = "feature-log/hana/repeatable result.md"
        self.assertEqual(first.stdout, f"added: {relative}\n")
        self.assertEqual(repeated, CliResult(0, f"unchanged: {relative}\n", ""))
        self.assertEqual(self.note("hana", "repeatable result").read_bytes(), original)
        self.assert_rejected(
            self.add_arguments(title="Repeatable Result", summary="A changed summary."),
            "amend",
        )

    def test_repeats_without_completed_keep_existing_date_after_midnight(self) -> None:
        first_day = "2026-10-10"
        next_day = "2026-10-11"
        add_arguments = self.add_arguments(title="Midnight Add", link=SHA_ONE)
        with patch.object(feature_log, "today", return_value=first_day):
            self.assertEqual(self.cli(add_arguments).status, 0)
        add_contents = self.note("hana", "midnight add").read_bytes()

        with patch.object(feature_log, "today", return_value=next_day):
            repeated_add = self.cli(add_arguments)
            self.assert_rejected(
                [*add_arguments, "--completed", next_day],
                "amend",
            )

        self.assertEqual(
            repeated_add,
            CliResult(
                0,
                "unchanged: feature-log/hana/midnight add.md\n",
                "",
            ),
        )
        self.assertEqual(self.note("hana", "midnight add").read_bytes(), add_contents)

        old_arguments = self.add_arguments(title="Midnight Old", link=SHA_TWO)
        replacement_arguments = self.add_arguments(title="Midnight New", link=SHA_THREE)
        with patch.object(feature_log, "today", return_value=first_day):
            self.assertEqual(self.cli(old_arguments).status, 0)
            self.assertEqual(self.cli(replacement_arguments).status, 0)
        replacement_contents = self.note("hana", "midnight new").read_bytes()
        supersede_arguments = replacement_arguments.copy()
        supersede_arguments[0] = "supersede"
        supersede_arguments.extend(("--old", "Midnight Old"))

        with patch.object(feature_log, "today", return_value=next_day):
            resumed = self.cli(supersede_arguments)

        self.assertEqual(resumed.status, 0)
        self.assertIn("unchanged: feature-log/hana/midnight new.md\n", resumed.stdout)
        self.assertIn("superseded: midnight old → midnight new\n", resumed.stdout)
        self.assertEqual(
            self.note("hana", "midnight new").read_bytes(), replacement_contents
        )

    def test_same_link_needs_separate_result_for_another_title(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="First Result")).status, 0)
        self.assert_rejected(self.add_arguments(title="Second Result"), "separate-result")

        allowed = self.cli(self.add_arguments(title="Second Result") + ["--separate-result"])

        self.assertEqual(allowed.status, 0)
        self.assertTrue(self.note("hana", "second result").is_file())

    def test_supersede_writes_the_new_note_and_updates_each_old_note(self) -> None:
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(self.add_arguments(title="Old A", link=SHA_ONE)).status, 0)
            self.assertEqual(self.cli(self.add_arguments(title="Old B", link=SHA_TWO)).status, 0)
        arguments = self.add_arguments(title="Replacement", link=SHA_THREE)
        arguments[0] = "supersede"
        arguments.extend(("--old", "Old A", "--old", "Old B"))

        result = self.cli(arguments)

        self.assertEqual(result.status, 0)
        self.assertIn("added: feature-log/hana/replacement.md\n", result.stdout)
        self.assertIn("superseded: old a → replacement\n", result.stdout)
        self.assertIn("superseded: old b → replacement\n", result.stdout)
        for old in ("old a", "old b"):
            contents = self.note("hana", old).read_text(encoding="utf-8")
            self.assertIn("state: superseded\n", contents)
            self.assertIn("replaced_by:\n  - \"[[replacement]]\"\n", contents)
            self.assertIn('date_created: "[[2026-10-01]]"\n', contents)
            self.assertIn(f'date_modified: "[[{TODAY}]]"\n', contents)
        self.assertIn("state: live\n", self.note("hana", "replacement").read_text(encoding="utf-8"))
        self.assert_no_temporary_files()

    def test_supersede_finishes_an_old_note_after_an_interrupted_run(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Old Result", link=SHA_ONE)).status, 0)
        replacement = self.add_arguments(title="New Result", link=SHA_TWO)
        self.assertEqual(self.cli(replacement).status, 0)
        interrupted_contents = self.note("hana", "new result").read_bytes()
        arguments = replacement.copy()
        arguments[0] = "supersede"
        arguments.extend(("--old", "Old Result"))

        resumed = self.cli(arguments)
        repeated = self.cli(arguments)

        self.assertEqual(resumed.status, 0)
        self.assertIn("unchanged: feature-log/hana/new result.md\n", resumed.stdout)
        self.assertIn("superseded: old result → new result\n", resumed.stdout)
        self.assertEqual(repeated.status, 0)
        self.assertEqual(self.note("hana", "new result").read_bytes(), interrupted_contents)
        old_contents = self.note("hana", "old result").read_text(encoding="utf-8")
        self.assertEqual(old_contents.count('  - "[[new result]]"\n'), 1)
        self.assert_no_temporary_files()

    def test_supersede_rejects_non_live_old_and_new_as_its_own_old(self) -> None:
        absent = self.add_arguments(title="Replacement", link=SHA_TWO)
        absent[0] = "supersede"
        absent.extend(("--old", "Missing Result"))
        self.assert_rejected(absent, "live")

        itself = self.add_arguments(title="Same Result", link=SHA_THREE)
        itself[0] = "supersede"
        itself.extend(("--old", "Same Result"))
        self.assert_rejected(itself, "old")

    def test_supersede_allows_old_link_but_rejects_an_unrelated_live_holder(self) -> None:
        self.assertEqual(
            self.cli(self.add_arguments(title="Retitled Old", link=SHA_ONE)).status,
            0,
        )
        retitle = self.add_arguments(title="Retitled New", link=SHA_ONE)
        retitle[0] = "supersede"
        retitle.extend(("--old", "Retitled Old"))

        retitled = self.cli(retitle)

        self.assertEqual(retitled.status, 0)
        self.assertIn("added: feature-log/hana/retitled new.md\n", retitled.stdout)
        self.assertIn("superseded: retitled old → retitled new\n", retitled.stdout)

        self.assertEqual(
            self.cli(self.add_arguments(title="Shared Old", link=SHA_TWO)).status,
            0,
        )
        third_holder = self.add_arguments(title="Third Holder", link=SHA_TWO)
        third_holder.append("--separate-result")
        self.assertEqual(self.cli(third_holder).status, 0)
        blocked_retitle = self.add_arguments(title="Shared New", link=SHA_TWO)
        blocked_retitle[0] = "supersede"
        blocked_retitle.extend(("--old", "Shared Old"))

        self.assert_rejected(blocked_retitle, "third holder")

    def test_amend_updates_allowed_fields_appends_evidence_and_prunes_release(self) -> None:
        original_arguments = self.add_arguments(
            title="Amended Result",
            crates=("hana", "hana_lagrange"),
            evidence=("Original evidence.",),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(original_arguments).status, 0)
        path = self.note("hana", "amended result")
        original = path.read_text(encoding="utf-8")
        original_link = next(line for line in original.splitlines() if line.startswith("link: "))

        result = self.cli(
            [
                "amend",
                "--project",
                "hana",
                "--title",
                "Amended Result",
                "--summary",
                "The amended summary.",
                "--behavior",
                "The amended behavior.",
                "--see",
                "Check the amended behavior.",
                "--area",
                "editing",
                "--kind",
                "change",
                "--crate",
                "hana",
                "--evidence",
                "Additional evidence.",
            ]
        )

        self.assertEqual(result.status, 0)
        self.assertIn("amended: feature-log/hana/amended result.md", result.stdout)
        amended = path.read_text(encoding="utf-8")
        self.assertIn("area: \"[[editing]]\"\n", amended)
        self.assertIn("completed_date: \"[[2026-10-01]]\"\n", amended)
        self.assertIn("crate:\n  - hana\n", amended)
        self.assertIn("kind: change\n", amended)
        self.assertIn("release: []\n", amended)
        self.assertIn("summary: The amended summary.\n", amended)
        self.assertIn('date_created: "[[2026-10-01]]"\n', amended)
        self.assertIn(f'date_modified: "[[{TODAY}]]"\n', amended)
        self.assertIn("The amended behavior.\n\n## See it\n\nCheck the amended behavior.", amended)
        self.assertIn("- Original evidence.\n", amended)
        self.assertIn("- Additional evidence.\n", amended)
        self.assertIn(original_link, amended)
        self.assertIn("state: live\n", amended)
        self.assertIn("replaced_by: []\n", amended)
        self.assert_no_temporary_files()

        event = self.events()[-1]
        self.assertEqual(event["verb"], "amend")
        if event["verb"] != "amend":
            self.fail("amend wrote the wrong event variant")
        self.assertCountEqual(
            event["fields"],
            ["area", "behavior", "crate", "evidence", "kind", "see", "summary"],
        )

    def test_amend_backfill_corrects_link_and_completed_together(self) -> None:
        title = "Corrected accepting merge"
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            added = self.cli(
                self.add_arguments(title=title)
                + ["--backfill", "--completed", "2026-10-01"]
            )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        original = feature_log.parse_note(path)
        corrected_link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"

        result = self.cli(
            [
                "amend", "--project", "hana", "--title", title,
                "--backfill", "--link", corrected_link,
                "--completed", "2026-09-30",
            ]
        )

        self.assertEqual(result.status, 0, result.stderr)
        self.assertIn(f"amended: feature-log/hana/{title.lower()}.md", result.stdout)
        self.assertEqual(result.stderr, "")
        expected = original.copy()
        expected["link"] = corrected_link
        expected["completed_date"] = "2026-09-30"
        expected["date_modified"] = TODAY
        self.assertEqual(feature_log.parse_note(path), expected)
        self.assertEqual(list(path.parent.glob("*.md")), [path])
        events = self.events()
        self.assertEqual(len(events), 2)
        event = events[-1]
        if event["verb"] != "amend":
            self.fail("provenance correction wrote the wrong event variant")
        self.assertCountEqual(event["fields"], ["completed_date", "link"])
        self.assertEqual(
            {**event, "fields": sorted(event["fields"])},
            {
                "at": EVENT_TIME,
                "verb": "amend",
                "project": "hana",
                "title": title.lower(),
                "fields": ["completed_date", "link"],
                "backfill": True,
                "old_link": original["link"],
                "new_link": corrected_link,
                "old_completed": "2026-10-01",
                "new_completed": "2026-09-30",
                "releases_added": [],
            },
        )
        self.assert_no_temporary_files()

    def test_amend_accepting_provenance_requires_backfill_and_both_fields(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Paired provenance")).status, 0)
        prefix = ["amend", "--project", "hana", "--title", "Paired provenance"]
        link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"
        for label, suffix, reason in (
            ("link alone", ["--backfill", "--link", link], "completed"),
            ("date alone", ["--backfill", "--completed", "2026-10-01"], "link"),
            ("live link", ["--link", link], "backfill"),
            ("live date", ["--completed", "2026-10-01"], "backfill"),
            (
                "live pair",
                ["--link", link, "--completed", "2026-10-01"],
                "backfill",
            ),
        ):
            with self.subTest(label=label):
                self.assert_rejected(prefix + suffix, reason)

    def test_amend_backfill_rejects_invalid_link_or_completed_date(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Validated provenance")).status, 0)
        prefix = [
            "amend", "--project", "hana", "--title", "Validated provenance",
            "--backfill",
        ]
        valid_link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"
        for link, completed, reason in (
            ("", "2026-10-01", "link"),
            ("https://github.com/natepiano/hana/commit/abc", "2026-10-01", "link"),
            (f"https://github.com/natepiano/nixos/commit/{SHA_TWO}", "2026-10-01", "link"),
            (valid_link, "2026-02-30", "completed"),
            (valid_link, "October 1", "completed"),
        ):
            with self.subTest(link=link, completed=completed):
                self.assert_rejected(
                    prefix + ["--link", link, "--completed", completed], reason
                )

    def test_amend_duplicate_live_link_needs_separate_result(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Correction target")).status, 0)
        self.assertEqual(
            self.cli(self.add_arguments(title="Other live result", link=SHA_TWO)).status,
            0,
        )
        other_path = self.note("hana", "other live result")
        other_contents = other_path.read_bytes()
        shared_link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"
        arguments = [
            "amend", "--project", "hana", "--title", "Correction target",
            "--backfill", "--link", shared_link, "--completed", "2026-10-01",
        ]
        self.assert_rejected(arguments, "other live result")

        result = self.cli(arguments + ["--separate-result"])

        self.assertEqual(result.status, 0, result.stderr)
        target = feature_log.parse_note(self.note("hana", "correction target"))
        self.assertEqual(target["link"], shared_link)
        self.assertEqual(target["completed_date"], "2026-10-01")
        self.assertEqual(other_path.read_bytes(), other_contents)
        self.assertEqual(len(list(other_path.parent.glob("*.md"))), 2)

    def test_amend_same_note_link_allows_a_corrected_date(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Same accepting merge")).status, 0)
        path = self.note("hana", "same accepting merge")
        original = feature_log.parse_note(path)

        result = self.cli(
            [
                "amend", "--project", "hana", "--title", "Same accepting merge",
                "--backfill", "--link", original["link"],
                "--completed", "2026-10-01",
            ]
        )

        self.assertEqual(result.status, 0, result.stderr)
        updated = feature_log.parse_note(path)
        self.assertEqual(updated["link"], original["link"])
        self.assertEqual(updated["completed_date"], "2026-10-01")

    def test_amend_link_held_only_by_a_superseded_note_is_allowed(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Correction target")).status, 0)
        self.assertEqual(
            self.cli(self.add_arguments(title="Retired result", link=SHA_TWO)).status,
            0,
        )
        replacement = self.add_arguments(title="Current replacement", link=SHA_THREE)
        replacement[0] = "supersede"
        self.assertEqual(self.cli(replacement + ["--old", "Retired result"]).status, 0)
        old_path = self.note("hana", "retired result")
        old_contents = old_path.read_bytes()
        link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"

        result = self.cli(
            [
                "amend", "--project", "hana", "--title", "Correction target",
                "--backfill", "--link", link, "--completed", "2026-10-01",
            ]
        )

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(
            feature_log.parse_note(self.note("hana", "correction target"))["link"],
            link,
        )
        self.assertEqual(old_path.read_bytes(), old_contents)

    def test_amend_backfill_adds_exact_releases_and_repeats_unchanged(self) -> None:
        title = "Historical release membership"
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            added = self.cli(
                self.add_arguments(title=title, crates=("hana_lagrange",))
                + ["--backfill", "--completed", "2026-10-01",
                   "--release", "hana_lagrange@1.2.0"]
            )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        original = feature_log.parse_note(path)
        arguments = [
            "amend", "--project", "hana", "--title", title, "--backfill",
            "--release", "hana_lagrange@2.3.5-alpha.1",
            "--release", "hana_lagrange@1.2.0",
            "--release", "hana_lagrange@2.3.4",
            "--release", "hana_lagrange@2.3.4",
        ]

        result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        updated = feature_log.parse_note(path)
        expected = original.copy()
        expected["release"] = [
            "hana_lagrange@1.2.0", "hana_lagrange@2.3.4",
            "hana_lagrange@2.3.5-alpha.1",
        ]
        expected["date_modified"] = TODAY
        self.assertEqual(updated, expected)
        self.assertNotIn("Unreleased", path.read_text(encoding="utf-8"))
        self.assertEqual(
            self.events()[-1],
            {
                "at": EVENT_TIME,
                "verb": "amend",
                "project": "hana",
                "title": title.lower(),
                "fields": ["release"],
                "backfill": True,
                "old_link": original["link"],
                "new_link": original["link"],
                "old_completed": "2026-10-01",
                "new_completed": "2026-10-01",
                "releases_added": ["hana_lagrange@2.3.4", "hana_lagrange@2.3.5-alpha.1"],
            },
        )
        note_before = path.read_bytes()
        events_before = (self.state / "events.jsonl").read_bytes()

        repeated = self.cli(arguments)

        self.assertEqual(
            repeated,
            CliResult(0, f"unchanged: feature-log/hana/{title.lower()}.md\n", ""),
        )
        self.assertEqual(path.read_bytes(), note_before)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), events_before)
        self.assert_no_temporary_files()

    def test_amend_historical_releases_require_backfill_and_a_valid_note_crate(self) -> None:
        self.assertEqual(
            self.cli(self.add_arguments(title="Validated releases", crates=("hana_lagrange",))).status,
            0,
        )
        prefix = ["amend", "--project", "hana", "--title", "Validated releases"]
        self.assert_rejected(prefix + ["--release", "hana_lagrange@1.2.3"], "backfill")
        for release, reason in (
            ("hana_liminal@1.2.3", "crate"),
            ("hana_lagrange", "release"),
            ("hana_lagrange@", "release"),
            ("hana_lagrange@1.2", "release"),
            ("hana_lagrange@1.2.3@extra", "release"),
            ("hana_lagrange@Unreleased", "release"),
        ):
            with self.subTest(release=release):
                self.assert_rejected(prefix + ["--backfill", "--release", release], reason)

    def test_release_prerelease_identifiers_cannot_be_empty_in_any_writer(self) -> None:
        for verb in ("add", "supersede", "amend"):
            for number, version in enumerate(("1.2.3-..", "1.2.3-", "1.2.3-a..b")):
                with self.subTest(verb=verb, version=version):
                    title = f"Rejected {verb} prerelease {number}"
                    if verb == "amend":
                        added = self.cli(
                            self.add_arguments(title=title, crates=("hana_lagrange",))
                            + ["--backfill", "--separate-result"]
                        )
                        self.assertEqual(added.status, 0, added.stderr)
                        arguments = ["amend", "--project", "hana", "--title", title]
                    else:
                        arguments = self.add_arguments(
                            title=title, link=SHA_TWO, crates=("hana_lagrange",)
                        )
                        arguments[0] = verb
                        arguments.append("--separate-result")
                        if verb == "supersede":
                            old_title = f"Old rejected prerelease {number}"
                            added = self.cli(
                                self.add_arguments(title=old_title)
                                + ["--separate-result"]
                            )
                            self.assertEqual(added.status, 0, added.stderr)
                            arguments.extend(("--old", old_title))
                    self.assert_rejected(
                        arguments + ["--backfill", "--release", f"hana_lagrange@{version}"],
                        "version",
                    )

    def test_release_prerelease_identifiers_accept_dots_and_hyphens_in_any_writer(self) -> None:
        for version in ("1.2.3-rc.1", "1.2.3-rc-one.1"):
            for verb in ("add", "supersede", "amend"):
                with self.subTest(verb=verb, version=version):
                    title = f"Accepted {verb} {version}"
                    if verb == "amend":
                        added = self.cli(
                            self.add_arguments(title=title, crates=("hana_lagrange",))
                            + ["--backfill", "--separate-result"]
                        )
                        self.assertEqual(added.status, 0, added.stderr)
                        arguments = ["amend", "--project", "hana", "--title", title]
                    else:
                        arguments = self.add_arguments(
                            title=title, link=SHA_TWO, crates=("hana_lagrange",)
                        )
                        arguments[0] = verb
                        arguments.append("--separate-result")
                        if verb == "supersede":
                            old_title = f"Old accepted {version}"
                            added = self.cli(
                                self.add_arguments(title=old_title)
                                + ["--separate-result"]
                            )
                            self.assertEqual(added.status, 0, added.stderr)
                            arguments.extend(("--old", old_title))
                    release = f"hana_lagrange@{version}"
                    result = self.cli(arguments + ["--backfill", "--release", release])
                    self.assertEqual(result.status, 0, result.stderr)
                    note = feature_log.parse_note(self.note("hana", feature_log.normalize_title(title)))
                    self.assertEqual(note["release"], [release])

    def test_amend_correction_retry_preserves_history_after_either_write_fails(self) -> None:
        append_event = cast(
            Callable[[feature_log.FeatureLogEvent], None], getattr(feature_log, "_append_event")
        )
        write_note = cast(
            Callable[[Path, feature_log.Note], None], getattr(feature_log, "_write_note")
        )
        for writer in ("_append_event", "_write_note"):
            for written_before_failure in (False, True):
                with self.subTest(writer=writer, written_before_failure=written_before_failure):
                    title = f"Retry {writer} {written_before_failure}"
                    added = self.cli(
                        self.add_arguments(title=title, crates=("hana_lagrange",))
                        + ["--backfill", "--completed", "2026-10-01", "--separate-result"]
                    )
                    self.assertEqual(added.status, 0, added.stderr)
                    path = self.note("hana", feature_log.normalize_title(title))
                    original = feature_log.parse_note(path)
                    new_link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"
                    release = "hana_lagrange@1.2.3"
                    arguments = [
                        "amend", "--project", "hana", "--title", title,
                        "--backfill", "--link", new_link, "--completed", "2026-09-30",
                        "--release", release, "--separate-result",
                    ]

                    def interrupted_append(event: feature_log.FeatureLogEvent) -> None:
                        if written_before_failure:
                            append_event(event)
                        raise OSError("interrupted journal append")

                    def interrupted_note(note_path: Path, note: feature_log.Note) -> None:
                        if written_before_failure:
                            write_note(note_path, note)
                        raise OSError("interrupted note write")

                    if writer == "_append_event":
                        with patch.object(feature_log, writer, side_effect=interrupted_append):
                            with self.assertRaises(OSError):
                                _ = self.cli(arguments)
                    else:
                        with patch.object(feature_log, writer, side_effect=interrupted_note):
                            with self.assertRaises(OSError):
                                _ = self.cli(arguments)

                    retried = self.cli(arguments)
                    self.assertEqual(retried.status, 0, retried.stderr)
                    expected = original.copy()
                    expected["link"] = new_link
                    expected["completed_date"] = "2026-09-30"
                    expected["release"] = [release]
                    self.assertEqual(feature_log.parse_note(path), expected)
                    events = [
                        event for event in self.events()
                        if event["verb"] == "amend" and event["title"] == original["title"]
                    ]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(
                        events[0],
                        {
                            "at": EVENT_TIME, "verb": "amend", "project": "hana",
                            "title": original["title"],
                            "fields": ["link", "completed_date", "release"], "backfill": True,
                            "old_link": original["link"], "new_link": new_link,
                            "old_completed": "2026-10-01", "new_completed": "2026-09-30",
                            "releases_added": [release],
                        },
                    )
                    note_bytes = path.read_bytes()
                    event_bytes = (self.state / "events.jsonl").read_bytes()
                    repeated = self.cli(arguments)
                    self.assertEqual(repeated.status, 0, repeated.stderr)
                    self.assertIn("unchanged:", repeated.stdout)
                    self.assertEqual(path.read_bytes(), note_bytes)
                    self.assertEqual((self.state / "events.jsonl").read_bytes(), event_bytes)
                    self.assert_no_temporary_files()

    def test_amend_release_retry_preserves_additions_after_a_previous_amend(self) -> None:
        for writer in ("_append_event", "_write_note"):
            with self.subTest(writer=writer):
                title = f"Release retry {writer}"
                added = self.cli(
                    self.add_arguments(title=title, crates=("hana_lagrange",))
                    + ["--backfill", "--completed", "2026-10-01", "--separate-result"]
                )
                self.assertEqual(added.status, 0, added.stderr)
                prefix = ["amend", "--project", "hana", "--title", title, "--backfill"]
                prior = self.cli(prefix + ["--evidence", "A previous review."])
                self.assertEqual(prior.status, 0, prior.stderr)
                prior_event = self.events()[-1]
                release = "hana_lagrange@1.2.3"
                arguments = prefix + ["--release", release]
                with patch.object(feature_log, writer, side_effect=OSError("interrupted write")):
                    with self.assertRaises(OSError):
                        _ = self.cli(arguments)

                retried = self.cli(arguments)
                self.assertEqual(retried.status, 0, retried.stderr)
                path = self.note("hana", feature_log.normalize_title(title))
                note = feature_log.parse_note(path)
                self.assertEqual(note["release"], [release])
                events = [
                    event for event in self.events()
                    if event["verb"] == "amend" and event["title"] == note["title"]
                ]
                self.assertEqual(len(events), 2)
                self.assertEqual(events[0], prior_event)
                event = events[1]
                if event["verb"] != "amend" or "releases_added" not in event:
                    self.fail("release correction wrote the wrong event variant")
                self.assertEqual(event["old_link"], note["link"])
                self.assertEqual(event["new_link"], note["link"])
                self.assertEqual(event["old_completed"], "2026-10-01")
                self.assertEqual(event["new_completed"], "2026-10-01")
                self.assertEqual(event["releases_added"], [release])

    def test_amend_separate_result_requires_a_link(self) -> None:
        added = self.cli(self.add_arguments(title="Explicit separate result"))
        self.assertEqual(added.status, 0, added.stderr)
        prefix = [
            "amend", "--project", "hana", "--title", "Explicit separate result",
            "--separate-result",
        ]
        self.assert_rejected(prefix, "--separate-result requires --link")
        self.assert_rejected(
            prefix + ["--summary", "A revised summary."],
            "--separate-result requires --link",
        )
        self.assert_rejected(
            prefix + ["--backfill", "--completed", "2026-10-01"],
            "--separate-result requires --link",
        )

    def test_amend_correction_can_be_applied_again_after_it_is_reversed(self) -> None:
        title = "Repeat a later correction"
        added = self.cli(
            self.add_arguments(title=title)
            + ["--backfill", "--completed", "2026-10-01"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        prefix = ["amend", "--project", "hana", "--title", title, "--backfill"]
        correction = prefix + ["--link", SHA_TWO, "--completed", "2026-09-30"]
        reversal = prefix + ["--link", SHA_ONE, "--completed", "2026-10-01"]
        for arguments in (correction, reversal, correction):
            result = self.cli(arguments)
            self.assertEqual(result.status, 0, result.stderr)
        events = [event for event in self.events() if event["verb"] == "amend"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0], events[2])
        self.assertEqual(
            feature_log.parse_note(self.note("hana", title.lower()))["link"],
            f"https://github.com/natepiano/hana/commit/{SHA_TWO}",
        )

    def test_amend_event_records_provenance_and_releases_in_one_correction(self) -> None:
        title = "Corrected historical result"
        added = self.cli(
            self.add_arguments(title=title, crates=("hana_lagrange",))
            + ["--backfill", "--completed", "2026-10-01"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        new_link = f"https://github.com/natepiano/hana/commit/{SHA_TWO}"
        result = self.cli(
            [
                "amend", "--project", "hana", "--title", title, "--backfill",
                "--link", new_link, "--completed", "2026-09-30",
                "--release", "hana_lagrange@1.2.3",
            ]
        )
        self.assertEqual(result.status, 0, result.stderr)
        event = self.events()[-1]
        if event["verb"] != "amend":
            self.fail("historical correction wrote the wrong event variant")
        self.assertCountEqual(event["fields"], ["completed_date", "link", "release"])
        self.assertEqual(
            {**event, "fields": sorted(event["fields"])},
            {
                "at": EVENT_TIME,
                "verb": "amend",
                "project": "hana",
                "title": title.lower(),
                "fields": ["completed_date", "link", "release"],
                "backfill": True,
                "old_link": f"https://github.com/natepiano/hana/commit/{SHA_ONE}",
                "new_link": new_link,
                "old_completed": "2026-10-01",
                "new_completed": "2026-09-30",
                "releases_added": ["hana_lagrange@1.2.3"],
            },
        )
        self.assertEqual(len(self.events()), 2)

    def test_amend_rejects_empty_fields_and_immutable_options(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Stable Result")).status, 0)
        prefix = ["amend", "--project", "hana", "--title", "Stable Result"]
        for label, suffix, reason in (
            ("no fields", [], "field"),
            ("empty summary", ["--summary", ""], "summary"),
            ("empty behavior", ["--behavior", ""], "behavior"),
            ("empty see", ["--see", ""], "see"),
            ("bad crate", ["--crate", "cargo-mend"], "crate"),
            ("state", ["--state", "superseded"], "state"),
            ("replaced by", ["--replaced-by", "other"], "replaced"),
        ):
            with self.subTest(label):
                self.assert_rejected(prefix + suffix, reason)

    def test_mutating_verbs_reject_embedded_body_section_headings(self) -> None:
        self.assertEqual(
            self.cli(self.add_arguments(title="Old Heading Note", link=SHA_ONE)).status,
            0,
        )
        self.assertEqual(
            self.cli(self.add_arguments(title="Amend Heading Note", link=SHA_TWO)).status,
            0,
        )
        dangerous_values = (
            "Before.\n## See it   \nAfter.",
            "Before.\n## Evidence\t\nAfter.",
        )
        for verb in ("add", "supersede", "amend"):
            for field in ("behavior", "see"):
                for dangerous_value in dangerous_values:
                    with self.subTest(verb=verb, field=field, value=dangerous_value):
                        if verb == "amend":
                            arguments = [
                                "amend",
                                "--project",
                                "hana",
                                "--title",
                                "Amend Heading Note",
                                f"--{field}",
                                dangerous_value,
                            ]
                        else:
                            arguments = self.add_arguments(
                                title="Rejected Heading Note",
                                link=SHA_THREE,
                                behavior=(
                                    dangerous_value
                                    if field == "behavior"
                                    else "Safe behavior."
                                ),
                                see=(
                                    dangerous_value
                                    if field == "see"
                                    else "Safe check."
                                ),
                            )
                            arguments[0] = verb
                            if verb == "supersede":
                                arguments.extend(("--old", "Old Heading Note"))
                        self.assert_rejected(arguments, "section heading")

    def test_live_lists_newest_first_and_formats_every_field(self) -> None:
        first = self.add_arguments(
            title="Older Widget",
            kind="fix",
            summary="The older widget works.",
            behavior="Old widget behavior is searchable.",
            link=SHA_ONE,
            crates=("hana", "hana_catalyst"),
        ) + ["--area", "widgets", "--completed", "2026-10-01", "--backfill"]
        second = self.add_arguments(
            title="Newer Panel",
            summary="The newer panel is visible.",
            behavior="New panel behavior.",
            link=SHA_TWO,
            crates=("hana",),
        ) + ["--area", "panels", "--completed", "2026-10-09", "--backfill"]
        third = self.add_arguments(
            title="Newest Widget",
            kind="perf",
            summary="The newest widget is quick.",
            behavior="Fast searchable behavior.",
            link=SHA_THREE,
            crates=("hana",),
        ) + ["--area", "widgets", "--completed", "2026-10-10", "--backfill"]
        for arguments in (first, second, third):
            self.assertEqual(self.cli(arguments).status, 0)

        listed = self.cli(["live", "--project", "hana"])

        self.assertEqual(
            listed,
            CliResult(
                0,
                "2026-10-10 perf newest widget — The newest widget is quick. [hana] (widgets) "
                + f"https://github.com/natepiano/hana/commit/{SHA_THREE}\n"
                + "2026-10-09 feature newer panel — The newer panel is visible. [hana] (panels) "
                + f"https://github.com/natepiano/hana/commit/{SHA_TWO}\n"
                + "2026-10-01 fix older widget — The older widget works. [hana, hana_catalyst] (widgets) "
                + f"https://github.com/natepiano/hana/commit/{SHA_ONE}\n",
                "",
            ),
        )

    def test_live_filters_by_crate_area_kind_and_case_insensitive_text(self) -> None:
        entries = (
            self.add_arguments(
                title="Widget Cable",
                kind="fix",
                summary="A signal is visible.",
                behavior="The rare BehaviorToken is present.",
                link=SHA_ONE,
                crates=("hana", "hana_catalyst"),
            )
            + ["--area", "widgets", "--backfill"],
            self.add_arguments(
                title="Panel Result",
                link=SHA_TWO,
                crates=("hana",),
            )
            + ["--area", "panels", "--backfill"],
        )
        for arguments in entries:
            self.assertEqual(self.cli(arguments).status, 0)

        filters = (
            ["--crate", "hana_catalyst"],
            ["--area", "widgets"],
            ["--kind", "fix"],
            ["--grep", "WIDGET CABLE"],
            ["--grep", "SIGNAL IS VISIBLE"],
            ["--grep", "behaviortoken"],
        )
        for extra in filters:
            with self.subTest(extra):
                result = self.cli(["live", "--project", "hana", *extra])
                self.assertEqual(result.status, 0)
                self.assertIn("widget cable", result.stdout)
                self.assertNotIn("panel result", result.stdout)

    def test_live_excludes_superseded_notes(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Old", link=SHA_ONE)).status, 0)
        replacement = self.add_arguments(title="New", link=SHA_TWO)
        replacement[0] = "supersede"
        replacement.extend(("--old", "Old"))
        self.assertEqual(self.cli(replacement).status, 0)

        result = self.cli(["live", "--project", "hana"])

        self.assertIn(" new — ", result.stdout)
        self.assertNotIn(" old — ", result.stdout)

    def test_check_accepts_writer_output(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Checked Result")).status, 0)
        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 1 notes\n", ""))

    def test_check_accepts_single_quoted_scalars_and_inline_lists(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Linter Forms")).status, 0)
        path = self.note("hana", "linter forms")
        contents = path.read_text(encoding="utf-8")
        contents = contents.replace(
            'completed_date: "[[2026-10-10]]"\n',
            "completed_date: '[[2026-10-10]]'\n",
        )
        contents = contents.replace("crate:\n  - hana\n", "crate: [hana]\n")
        contents = contents.replace(
            "summary: People can use the useful result.\n",
            "summary: 'People can use the useful result.'\n",
        )
        _ = path.write_text(contents, encoding="utf-8")

        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 1 notes\n", ""))

    def test_check_rejects_bare_scalars_that_yaml_changes(self) -> None:
        fixtures = (
            ("Valid Scalar", SHA_ONE),
            ("Colon Scalar", SHA_TWO),
            ("Comment Scalar", SHA_THREE),
        )
        for title, link in fixtures:
            self.assertEqual(self.cli(self.add_arguments(title=title, link=link)).status, 0)
        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 3 notes\n", ""))

        colon = self.note("hana", "colon scalar")
        _ = colon.write_text(
            colon.read_text(encoding="utf-8").replace(
                "summary: People can use the useful result.\n",
                "summary: a: b\n",
            ),
            encoding="utf-8",
        )
        comment = self.note("hana", "comment scalar")
        _ = comment.write_text(
            comment.read_text(encoding="utf-8").replace(
                "summary: People can use the useful result.\n",
                "summary: text # comment\n",
            ),
            encoding="utf-8",
        )

        result = self.cli(["check"])

        self.assertEqual(result.status, 1)
        self.assertIn("feature-log/hana/colon scalar.md: malformed bare scalar", result.stdout)
        self.assertIn("feature-log/hana/comment scalar.md: malformed bare scalar", result.stdout)
        self.assertNotIn("feature-log/hana/valid scalar.md:", result.stdout)

    def test_check_reports_each_kind_of_broken_fixture(self) -> None:
        fixtures = (
            ("missing key", SHA_ONE),
            ("unknown key", SHA_TWO),
            ("live replacement", SHA_THREE),
            ("dangling replacement", "0000000000000000000000000000000000000004"),
        )
        for title, link in fixtures:
            self.assertEqual(self.cli(self.add_arguments(title=title, link=link)).status, 0)

        missing = self.note("hana", "missing key")
        _ = missing.write_text(
            missing.read_text(encoding="utf-8").replace(
                "summary: People can use the useful result.\n", ""
            ),
            encoding="utf-8",
        )
        unknown = self.note("hana", "unknown key")
        _ = unknown.write_text(
            unknown.read_text(encoding="utf-8").replace("kind: feature\n", "kind: feature\nmystery: value\n"),
            encoding="utf-8",
        )
        live = self.note("hana", "live replacement")
        _ = live.write_text(
            live.read_text(encoding="utf-8").replace(
                "replaced_by: []\n", 'replaced_by:\n  - "[[unknown target]]"\n'
            ),
            encoding="utf-8",
        )
        dangling = self.note("hana", "dangling replacement")
        _ = dangling.write_text(
            dangling.read_text(encoding="utf-8")
            .replace("replaced_by: []\n", 'replaced_by:\n  - "[[absent target]]"\n')
            .replace("state: live\n", "state: superseded\n"),
            encoding="utf-8",
        )

        result = self.cli(["check"])

        self.assertEqual(result.status, 1)
        self.assertEqual(result.stderr, "")
        for title in ("missing key", "unknown key", "live replacement", "dangling replacement"):
            self.assertIn(f"feature-log/hana/{title}.md:", result.stdout)
        self.assertIn("summary", result.stdout)
        self.assertIn("unknown", result.stdout)
        self.assertIn("live", result.stdout)
        self.assertIn("absent target", result.stdout)

    def test_check_rejects_duplicate_keys_bad_syntax_and_wrong_file_name(self) -> None:
        for title, link in (("duplicate", SHA_ONE), ("syntax", SHA_TWO), ("wrong name", SHA_THREE)):
            self.assertEqual(self.cli(self.add_arguments(title=title, link=link)).status, 0)
        duplicate = self.note("hana", "duplicate")
        _ = duplicate.write_text(
            duplicate.read_text(encoding="utf-8").replace("kind: feature\n", "kind: feature\nkind: fix\n"),
            encoding="utf-8",
        )
        syntax = self.note("hana", "syntax")
        _ = syntax.write_text(
            syntax.read_text(encoding="utf-8").replace("summary: People", "summary People"),
            encoding="utf-8",
        )
        _ = self.note("hana", "wrong name").rename(self.note("hana", "WRONG NAME"))

        result = self.cli(["check"])

        self.assertEqual(result.status, 1)
        self.assertIn("duplicate", result.stdout)
        self.assertIn("syntax", result.stdout)
        self.assertIn("WRONG NAME.md", result.stdout)

    def test_check_reports_invalid_utf8_note_without_a_traceback(self) -> None:
        path = self.note("hana", "invalid utf8")
        path.parent.mkdir(parents=True)
        _ = path.write_bytes(b"\xff\xfe")

        result = self.cli(["check"])

        self.assertEqual(result.status, 1)
        self.assertEqual(result.stderr, "")
        self.assertIn("feature-log/hana/invalid utf8.md:", result.stdout)
        self.assertIn("utf-8", result.stdout)

    def test_each_mutating_command_writes_only_its_event_fields(self) -> None:
        add = self.add_arguments(
            title="Event Old",
            kind="fix",
            summary="The event old result works.",
            crates=("hana_lagrange",),
        )
        self.assertEqual(self.cli(add).status, 0)
        added = self.events()
        self.assertEqual(len(added), 1)
        add_event = added[0]
        self.assertEqual(add_event["verb"], "add")
        if add_event["verb"] != "add":
            self.fail("add wrote the wrong event variant")
        self.assertEqual(
            set(add_event),
            {
                "at",
                "verb",
                "project",
                "title",
                "kind",
                "summary",
                "release",
                "backfill",
            },
        )
        self.assertEqual(add_event["at"], EVENT_TIME)
        self.assertEqual(add_event["project"], "hana")
        self.assertEqual(add_event["title"], "event old")
        self.assertEqual(add_event["kind"], "fix")
        self.assertEqual(add_event["summary"], "The event old result works.")
        self.assertEqual(add_event["release"], ["hana_lagrange@Unreleased"])
        self.assertFalse(add_event["backfill"])

        amend = [
            "amend",
            "--project",
            "hana",
            "--title",
            "Event Old",
            "--summary",
            "The amended event works.",
            "--backfill",
        ]
        self.assertEqual(self.cli(amend).status, 0)
        amended = self.events()
        self.assertEqual(len(amended), 2)
        amend_event = amended[-1]
        self.assertEqual(amend_event["verb"], "amend")
        if amend_event["verb"] != "amend":
            self.fail("amend wrote the wrong event variant")
        self.assertEqual(
            set(amend_event),
            {
                "at", "verb", "project", "title", "fields", "backfill",
                "old_link", "new_link", "old_completed", "new_completed",
                "releases_added",
            },
        )
        self.assertEqual(amend_event["fields"], ["summary"])
        self.assertTrue(amend_event["backfill"])

        supersede = self.add_arguments(title="Event New", link=SHA_TWO)
        supersede[0] = "supersede"
        supersede.extend(("--old", "Event Old"))
        self.assertEqual(self.cli(supersede).status, 0)
        events = self.events()
        self.assertEqual(len(events), 3)
        supersede_event = events[-1]
        self.assertEqual(supersede_event["verb"], "supersede")
        if supersede_event["verb"] != "supersede":
            self.fail("supersede wrote the wrong event variant")
        self.assertEqual(
            set(supersede_event),
            {
                "at",
                "verb",
                "project",
                "title",
                "kind",
                "summary",
                "release",
                "backfill",
                "old",
            },
        )
        self.assertEqual(supersede_event["old"], ["event old"])

    def test_exact_repeat_repairs_one_missing_event_only_once(self) -> None:
        arguments = self.add_arguments(title="Repair My Event")
        self.assertEqual(self.cli(arguments).status, 0)
        path = self.note("hana", "repair my event")
        original = path.read_bytes()
        (self.state / "events.jsonl").unlink()

        repaired = self.cli(arguments)

        self.assertEqual(
            repaired,
            CliResult(
                0,
                "unchanged: feature-log/hana/repair my event.md\n",
                "",
            ),
        )
        self.assertEqual(path.read_bytes(), original)
        repaired_events = self.events()
        self.assertEqual(len(repaired_events), 1)
        self.assertEqual(repaired_events[0]["verb"], "add")

        repeated = self.cli(arguments)

        self.assertEqual(repeated.status, 0)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_repeated_evidence_amend_repairs_event_without_duplicating_evidence(
        self,
    ) -> None:
        add = self.add_arguments(title="Repair Evidence Event")
        self.assertEqual(self.cli(add).status, 0)
        evidence = "Observed in the repaired runtime."
        amend = [
            "amend",
            "--project",
            "hana",
            "--title",
            "Repair Evidence Event",
            "--evidence",
            evidence,
        ]
        self.assertEqual(self.cli(amend).status, 0)
        self.write_events(
            [event for event in self.events() if event["verb"] != "amend"]
        )
        path = self.note("hana", "repair evidence event")
        note_before = path.read_bytes()

        repaired = self.cli(amend)

        self.assertEqual(
            repaired,
            CliResult(
                0,
                "unchanged: feature-log/hana/repair evidence event.md\n",
                "",
            ),
        )
        self.assertEqual(path.read_bytes(), note_before)
        repaired_note = feature_log.parse_note(path)
        self.assertEqual(repaired_note["evidence"].count(evidence), 1)
        repaired_events = self.events()
        self.assertEqual(
            sum(event["verb"] == "amend" for event in repaired_events),
            1,
        )

        events_before = (self.state / "events.jsonl").read_bytes()
        repeated = self.cli(amend)

        self.assertEqual(repeated, repaired)
        self.assertEqual(path.read_bytes(), note_before)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), events_before)

        new_evidence = "Confirmed by a second behavioral check."
        mixed = amend + ["--evidence", new_evidence]
        amended = self.cli(mixed)

        self.assertEqual(
            amended,
            CliResult(
                0,
                "amended: feature-log/hana/repair evidence event.md (evidence)\n",
                "",
            ),
        )
        mixed_note = feature_log.parse_note(path)
        self.assertEqual(mixed_note["evidence"].count(evidence), 1)
        self.assertEqual(mixed_note["evidence"].count(new_evidence), 1)
        self.assertLess(
            mixed_note["evidence"].index(evidence),
            mixed_note["evidence"].index(new_evidence),
        )
        last_event = self.events()[-1]
        self.assertEqual(last_event["verb"], "amend")
        if last_event["verb"] != "amend":
            self.fail("evidence amend wrote the wrong event variant")
        self.assertEqual(last_event["fields"], ["evidence"])

    def test_propose_writes_no_note_and_replaces_an_open_proposal(self) -> None:
        first = self.proposal_arguments(
            "C900",
            self.add_arguments(
                title="Proposed Result",
                summary="The first proposed summary.",
            ),
        )

        result = self.cli(first)

        self.assertEqual(result.status, 0)
        self.assertEqual(self.vault_snapshot(), {})
        self.assertEqual(self.events(), [])
        proposal = self.proposal("C900")
        self.assertEqual(proposal["status"], "open")
        if proposal["status"] != "open":
            self.fail("propose did not create an open proposal")
        self.assertEqual(set(proposal), {"row", "verb", "args", "status", "at"})
        self.assertEqual(proposal["row"], "C900")
        self.assertEqual(proposal["verb"], "add")
        self.assertEqual(proposal["at"], EVENT_TIME)
        self.assertEqual(proposal["args"]["title"], "Proposed Result")
        self.assertEqual(proposal["args"]["completed"], TODAY)
        self.assertEqual(proposal["args"]["old"], [])

        corrected = self.proposal_arguments(
            "C900",
            self.add_arguments(
                title="Proposed Result",
                summary="The corrected proposed summary.",
            ),
        )
        self.assertEqual(self.cli(corrected).status, 0)

        replacement = self.proposal("C900")
        self.assertEqual(replacement["status"], "open")
        self.assertEqual(
            replacement["args"]["summary"],
            "The corrected proposed summary.",
        )
        self.assertEqual(self.vault_snapshot(), {})
        self.assertEqual(self.events(), [])

    def test_corrected_open_proposal_preserves_or_explicitly_replaces_date(self) -> None:
        implicit = self.proposal_arguments(
            "C904",
            self.add_arguments(
                title="Implicit Proposal Date",
                summary="The initial implicit-date summary.",
                link=SHA_ONE,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(implicit).status, 0)

        implicit_correction = self.proposal_arguments(
            "C904",
            self.add_arguments(
                title="Implicit Proposal Date",
                summary="The corrected implicit-date summary.",
                link=SHA_ONE,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            self.assertEqual(self.cli(implicit_correction).status, 0)
        self.assertEqual(self.proposal("C904")["args"]["completed"], "2026-10-01")

        with patch.object(feature_log, "today", return_value="2026-10-03"):
            self.assertEqual(self.cli(["add", "--review", "C904"]).status, 0)
        implicit_contents = self.note(
            "hana", "implicit proposal date"
        ).read_text(encoding="utf-8")
        self.assertIn('completed_date: "[[2026-10-01]]"\n', implicit_contents)

        explicit = self.proposal_arguments(
            "C905",
            self.add_arguments(
                title="Explicit Proposal Date",
                summary="The initial explicit-date summary.",
                link=SHA_TWO,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(explicit).status, 0)

        explicit_correction = self.proposal_arguments(
            "C905",
            self.add_arguments(
                title="Explicit Proposal Date",
                summary="The corrected explicit-date summary.",
                link=SHA_TWO,
            ),
        )
        explicit_correction.extend(("--completed", "2026-09-30"))
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            self.assertEqual(self.cli(explicit_correction).status, 0)
        self.assertEqual(self.proposal("C905")["args"]["completed"], "2026-09-30")

        with patch.object(feature_log, "today", return_value="2026-10-03"):
            self.assertEqual(self.cli(["add", "--review", "C905"]).status, 0)
        explicit_contents = self.note(
            "hana", "explicit proposal date"
        ).read_text(encoding="utf-8")
        self.assertIn('completed_date: "[[2026-09-30]]"\n', explicit_contents)

    def test_propose_validates_review_row_and_old_note(self) -> None:
        invalid_row = self.proposal_arguments(
            "review-1", self.add_arguments(title="Invalid Row")
        )
        self.assert_rejected(invalid_row, "review")
        missing_old = self.proposal_arguments(
            "A12",
            self.add_arguments(title="Replacement Proposal", link=SHA_TWO),
            old=("Missing Result",),
        )
        self.assert_rejected(missing_old, "live")
        self.assertFalse((self.state / "proposals/review-1.json").exists())
        self.assertFalse((self.state / "proposals/A12.json").exists())

    def test_add_review_lands_once_and_keeps_the_proposed_date(self) -> None:
        arguments = self.proposal_arguments(
            "C900",
            self.add_arguments(title="Reviewed Result"),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(arguments).status, 0)

        with patch.object(feature_log, "today", return_value="2026-10-02"):
            landed = self.cli(["add", "--review", "C900"])

        self.assertEqual(landed.status, 0)
        self.assertIn("[[reviewed result]]\n", landed.stdout)
        notes = list((self.vault / "feature-log/hana").glob("*.md"))
        self.assertEqual(notes, [self.note("hana", "reviewed result")])
        contents = notes[0].read_text(encoding="utf-8")
        self.assertIn('completed_date: "[[2026-10-01]]"\n', contents)
        self.assertEqual(len(self.events()), 1)
        proposal = self.proposal("C900")
        self.assertEqual(proposal["status"], "landed")
        if proposal["status"] != "landed":
            self.fail("add --review did not land its proposal")
        self.assertEqual(
            set(proposal),
            {"row", "verb", "args", "status", "at", "note"},
        )
        self.assertEqual(proposal["note"], "reviewed result")

        note_before = notes[0].read_bytes()
        proposal_path = self.state / "proposals/C900.json"
        proposal_before = proposal_path.read_bytes()
        events_before = (self.state / "events.jsonl").read_bytes()
        with patch.object(feature_log, "today", return_value="2026-10-03"):
            repeated = self.cli(["add", "--review", "C900"])

        self.assertEqual(repeated.status, 0)
        self.assertIn("unchanged", repeated.stdout)
        self.assertEqual(notes[0].read_bytes(), note_before)
        self.assertEqual(proposal_path.read_bytes(), proposal_before)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), events_before)

        refused = self.cli(arguments)
        self.assertEqual(refused.status, 2)
        self.assertIn("landed", refused.stderr)
        self.assertEqual(proposal_path.read_bytes(), proposal_before)

    def test_review_requires_a_proposal_and_the_matching_verb(self) -> None:
        missing = self.cli(["add", "--review", "A999"])
        self.assertEqual(
            missing,
            CliResult(
                2,
                "",
                "feature_log: no proposal for A999; run propose first\n",
            ),
        )

        self.assertEqual(
            self.cli(self.add_arguments(title="Verb Old", link=SHA_ONE)).status,
            0,
        )
        supersede_proposal = self.proposal_arguments(
            "C902",
            self.add_arguments(title="Verb New", link=SHA_TWO),
            old=("Verb Old",),
        )
        self.assertEqual(self.cli(supersede_proposal).status, 0)
        self.assert_rejected(["add", "--review", "C902"], "supersede")

        landed = self.cli(["supersede", "--review", "C902"])

        self.assertEqual(landed.status, 0)
        self.assertIn("[[verb new]]", landed.stdout)
        old_contents = self.note("hana", "verb old").read_text(encoding="utf-8")
        self.assertIn("state: superseded\n", old_contents)
        self.assertIn('  - "[[verb new]]"\n', old_contents)
        self.assertEqual(self.proposal("C902")["status"], "landed")

        add_proposal = self.proposal_arguments(
            "C903",
            self.add_arguments(title="Add Only", link=SHA_THREE),
        )
        self.assertEqual(self.cli(add_proposal).status, 0)
        self.assert_rejected(["supersede", "--review", "C903"], "add")

    def test_same_merge_results_require_separate_result_to_land_together(self) -> None:
        first = self.proposal_arguments(
            "C906",
            self.add_arguments(title="Shared Merge First", link=SHA_ONE),
        )
        first.append("--separate-result")
        second = self.proposal_arguments(
            "C907",
            self.add_arguments(title="Shared Merge Second", link=SHA_ONE),
        )
        second.append("--separate-result")
        self.assertEqual(self.cli(first).status, 0)
        self.assertEqual(self.cli(second).status, 0)

        first_landed = self.cli(["add", "--review", "C906"])
        second_landed = self.cli(["add", "--review", "C907"])

        self.assertEqual(first_landed.status, 0)
        self.assertEqual(second_landed.status, 0)
        self.assertTrue(self.note("hana", "shared merge first").is_file())
        self.assertTrue(self.note("hana", "shared merge second").is_file())

        unmarked_first = self.proposal_arguments(
            "C908",
            self.add_arguments(title="Unmarked Merge First", link=SHA_TWO),
        )
        unmarked_second = self.proposal_arguments(
            "C909",
            self.add_arguments(title="Unmarked Merge Second", link=SHA_TWO),
        )
        self.assertEqual(self.cli(unmarked_first).status, 0)
        self.assertEqual(self.cli(unmarked_second).status, 0)
        self.assertEqual(self.cli(["add", "--review", "C908"]).status, 0)

        refused = self.cli(["add", "--review", "C909"])

        self.assertEqual(refused.status, 2)
        self.assertIn("--separate-result", refused.stderr)
        self.assertFalse(self.note("hana", "unmarked merge second").exists())

    def test_drop_closes_an_open_proposal_without_a_note(self) -> None:
        arguments = self.proposal_arguments(
            "A901", self.add_arguments(title="Dropped Result")
        )
        self.assertEqual(self.cli(arguments).status, 0)

        dropped = self.cli(
            ["drop", "--review", "A901", "--verdict", "defect: still broken"]
        )

        self.assertEqual(dropped.status, 0)
        self.assertEqual(self.vault_snapshot(), {})
        self.assertEqual(self.events(), [])
        proposal = self.proposal("A901")
        self.assertEqual(proposal["status"], "dropped")
        if proposal["status"] != "dropped":
            self.fail("drop did not close the proposal")
        self.assertEqual(
            set(proposal),
            {"row", "verb", "args", "status", "at", "verdict"},
        )
        self.assertEqual(proposal["verdict"], "defect: still broken")
        proposal_path = self.state / "proposals/A901.json"
        before = proposal_path.read_bytes()

        repeated = self.cli(
            ["drop", "--review", "A901", "--verdict", "defect: still broken"]
        )

        self.assertEqual(repeated.status, 0)
        self.assertIn("unchanged", repeated.stdout)
        self.assertEqual(proposal_path.read_bytes(), before)
        refused = self.cli(arguments)
        self.assertEqual(refused.status, 2)
        self.assertIn("dropped", refused.stderr)

    def test_digest_groups_each_live_event_and_advances_past_backfill(self) -> None:
        cargo_add: AddEvent = {
            "at": EVENT_TIME,
            "verb": "add",
            "project": "cargo-liner",
            "title": "cargo result",
            "kind": "feature",
            "summary": "Cargo result summary.",
            "release": ["cargo-mend@Unreleased"],
            "backfill": False,
        }
        hana_add: AddEvent = {
            "at": EVENT_TIME,
            "verb": "add",
            "project": "hana",
            "title": "hana result",
            "kind": "feature",
            "summary": "Hana result summary.",
            "release": [],
            "backfill": False,
        }
        hana_supersede: SupersedeEvent = {
            "at": EVENT_TIME,
            "verb": "supersede",
            "project": "hana",
            "title": "hana replacement",
            "kind": "change",
            "summary": "Hana replacement summary.",
            "release": [],
            "backfill": False,
            "old": ["old hana result"],
        }
        hana_amend: LegacyAmendEvent = {
            "at": EVENT_TIME,
            "verb": "amend",
            "project": "hana",
            "title": "hana replacement",
            "fields": ["evidence", "summary"],
            "backfill": False,
        }
        hidden_backfill: AddEvent = {
            "at": EVENT_TIME,
            "verb": "add",
            "project": "bevy_brp",
            "title": "hidden backfill",
            "kind": "fix",
            "summary": "This must stay hidden.",
            "release": [],
            "backfill": True,
        }
        self.write_events(
            [cargo_add, hana_add, hana_supersede, hana_amend, hidden_backfill]
        )

        result = self.cli(["digest"])

        self.assertEqual(result.status, 0)
        self.assertIn("hana", result.stdout)
        self.assertIn("cargo-liner", result.stdout)
        self.assertIn("+ hana result — Hana result summary.", result.stdout)
        self.assertIn("→ old hana result ⇒ hana replacement", result.stdout)
        self.assertIn("~ hana replacement (evidence, summary)", result.stdout)
        self.assertIn("+ cargo result — Cargo result summary.", result.stdout)
        self.assertNotIn("hidden backfill", result.stdout)
        self.assertLess(
            result.stdout.index("+ hana result"),
            result.stdout.index("+ cargo result"),
        )
        self.assertEqual(result.stdout.count("+ hana result"), 1)
        self.assertEqual(result.stdout.count("→ old hana result"), 1)
        self.assertRegex(result.stdout, r"4 changes since .+\n$")
        self.assertEqual(
            self.digest_cursor(),
            DigestCursor(line=5, at=EVENT_TIME),
        )

        empty = self.cli(["digest"])

        self.assertEqual(
            empty,
            CliResult(
                0,
                "feature log: nothing new since 2026-10-10 12:34 PDT\n",
                "",
            ),
        )
        self.assertEqual(self.digest_cursor()["line"], 5)

    def test_cursor_advancing_digest_waits_for_the_feature_log_lock(self) -> None:
        event: AddEvent = {
            "at": EVENT_TIME,
            "verb": "add",
            "project": "hana",
            "title": "locked digest result",
            "kind": "feature",
            "summary": "The digest waits for its cursor lock.",
            "release": [],
            "backfill": False,
        }
        self.write_events([event])
        lock_path = self.state / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as held_lock:
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
            process: subprocess.Popen[str] = subprocess.Popen(
                [
                    sys.executable,
                    "scripts/feature_log/feature_log.py",
                    "digest",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = process.wait(timeout=0.5)
            finally:
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.communicate(timeout=5)
            raise
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("+ locked digest result", stdout)
        self.assertEqual(self.digest_cursor()["line"], 1)

    def test_digest_since_filters_without_advancing_the_cursor(self) -> None:
        old: AddEvent = {
            "at": "2026-10-09T23:59:00-07:00",
            "verb": "add",
            "project": "hana",
            "title": "old event",
            "kind": "feature",
            "summary": "Old summary.",
            "release": [],
            "backfill": False,
        }
        current: AddEvent = {
            "at": "2026-10-10T00:00:00-07:00",
            "verb": "add",
            "project": "hana",
            "title": "current event",
            "kind": "feature",
            "summary": "Current summary.",
            "release": [],
            "backfill": False,
        }
        self.write_events([old, current])

        result = self.cli(["digest", "--since", "2026-10-10"])

        self.assertEqual(result.status, 0)
        self.assertNotIn("old event", result.stdout)
        self.assertIn("+ current event — Current summary.", result.stdout)
        self.assertIn("1 changes since 2026-10-10 00:00 PDT", result.stdout)
        self.assertFalse((self.state / "digest.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
