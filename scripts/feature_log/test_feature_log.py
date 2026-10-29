#!/usr/bin/env python3
"""Specification tests for the feature-log note writer and reader."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import fcntl
from io import StringIO
import json
import os
import select
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


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

TODAY = "2026-10-10"
EVENT_TIME = "2026-10-10T12:34:56-07:00"
sha_one: str
sha_two: str
sha_three: str
sha_four: str
default_checkout: Path


def temporary_checkout(
    path: Path, *, dates: tuple[str, ...] = ("2026-10-10T12:00:00+00:00",) * 4,
) -> tuple[str, ...]:
    """Create real commits without consulting the user's git config or checkout."""
    path.mkdir(parents=True)
    environment = os.environ.copy()
    environment.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="Feature Log Test",
        GIT_AUTHOR_EMAIL="feature-log-test@example.invalid",
        GIT_COMMITTER_NAME="Feature Log Test",
        GIT_COMMITTER_EMAIL="feature-log-test@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T12:00:00+00:00",
        GIT_COMMITTER_DATE="2026-10-10T12:00:00+00:00",
    )

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True, text=True, check=True, env=environment,
        ).stdout.strip()

    _ = git("init", "--template=", "--object-format=sha1", "-b", "main")
    commits: list[str] = []
    for index, date in enumerate(dates):
        environment["GIT_COMMITTER_DATE"] = date
        _ = git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", f"Result {index}")
        commits.append(git("rev-parse", "HEAD"))
    return tuple(commits)

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


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    global sha_one, sha_two, sha_three, sha_four, default_checkout
    unittest.enterModuleContext(patch.dict(os.environ, {
        "USER_ZONE": "America/Los_Angeles",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    default_checkout = root / "checkout"
    sha_one, sha_two, sha_three, sha_four = temporary_checkout(default_checkout)


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


class ReleaseEvent(TypedDict):
    at: str
    verb: Literal["release"]
    project: str
    crate: str
    version: str
    titles: list[str]


FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent | ReleaseEvent


class ChangelogSelectionJson(TypedDict):
    section: str
    titles: list[str]


class ReviewedReleaseContents(TypedDict):
    crate: str
    titles: list[str]


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


@final
class RecordingForwardedReader:
    def __init__(
        self, result: feature_log.ChangelogSelection | feature_log.ForwardFailed,
    ) -> None:
        self.result = result
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str],
    ) -> feature_log.ChangelogSelection | feature_log.ForwardFailed:
        self.commands.append(command.copy())
        return self.result


class ForwardedReleaseCall(NamedTuple):
    command: list[str]
    contents: ReviewedReleaseContents


@final
class RecordingReleaseForwarder:
    def __init__(self) -> None:
        self.calls: list[ForwardedReleaseCall] = []

    def __call__(
        self, command: list[str], contents: feature_log.ReviewedReleaseContents,
    ) -> int:
        reviewed: ReviewedReleaseContents = {
            "crate": contents["crate"], "titles": contents["titles"].copy(),
        }
        self.calls.append(ForwardedReleaseCall(command.copy(), reviewed))
        return 0


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
        _projects = cast(dict[str, feature_log.ProjectSpec], self.enterContext(
            patch.dict(feature_log.PROJECTS, {
                name: spec._replace(checkouts=(default_checkout,))
                for name, spec in feature_log.PROJECTS.items()
            }),
        ))

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
        link: str | None = None,
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
            sha_one if link is None else link,
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
            ("git@github.com:natepiano/claude_commands\n", "claude"),
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

    def test_add_defaults_to_committer_date_in_the_users_zone(self) -> None:
        checkout = self.root / "dated-checkout"
        (sha,) = temporary_checkout(checkout, dates=("2026-10-09T01:30:00+00:00",))
        project = feature_log.PROJECTS["hana"]._replace(checkouts=(checkout,))
        for zone, completed in (
            ("America/Los_Angeles", "2026-10-08"),
            ("Asia/Tokyo", "2026-10-09"),
        ):
            with self.subTest(zone=zone), patch.dict(feature_log.PROJECTS, {"hana": project}):
                with patch.dict(os.environ, {"USER_ZONE": zone}):
                    title = "Pacific result" if zone == "America/Los_Angeles" else "Tokyo result"
                    arguments = self.add_arguments(
                        title=title, link=f"https://github.com/natepiano/hana/commit/{sha}",
                    ) + ["--separate-result"]

                    result = self.cli(arguments)

                self.assertEqual(result.status, 0, result.stderr)
                note = feature_log.parse_note(self.note("hana", title.lower()))
                self.assertEqual(note["completed_date"], completed)
                self.assertEqual(note["date_created"], TODAY)
                self.assertEqual(note["date_modified"], TODAY)

    def test_explicit_completed_date_bypasses_git_even_for_an_unknown_commit(self) -> None:
        arguments = self.add_arguments(link="f" * 40) + ["--completed", "2026-09-15"]
        with patch.object(
            feature_log.subprocess, "run", side_effect=AssertionError("unexpected git lookup"),
        ) as git:
            result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        git.assert_not_called()
        note = feature_log.parse_note(self.note("hana", "a useful result"))
        self.assertEqual(note["completed_date"], "2026-09-15")
        self.assertEqual(note["date_created"], TODAY)
        self.assertEqual(note["date_modified"], TODAY)

    def test_commit_date_uses_the_second_checkout_after_a_missing_commit_or_directory(self) -> None:
        empty = self.root / "empty-checkout"
        _ = temporary_checkout(empty, dates=())
        dated = self.root / "dated-checkout"
        (sha,) = temporary_checkout(dated, dates=("2026-10-03T01:30:00+00:00",))
        for first in (empty, self.root / "missing-checkout"):
            with self.subTest(first=first.name):
                project = feature_log.PROJECTS["hana"]._replace(checkouts=(first, dated))
                with patch.dict(feature_log.PROJECTS, {"hana": project}):
                    result = self.cli(self.add_arguments(
                        title=first.name, link=sha,
                    ) + ["--separate-result"])

                self.assertEqual(result.status, 0, result.stderr)
                note = feature_log.parse_note(self.note("hana", first.name.replace("-", " ")))
                self.assertEqual(note["completed_date"], "2026-10-02")

    def test_commit_date_stops_after_the_first_checkout_holds_the_commit(self) -> None:
        project = feature_log.PROJECTS["hana"]._replace(
            checkouts=(default_checkout, self.root / "missing-checkout"),
        )
        with (
            patch.dict(feature_log.PROJECTS, {"hana": project}),
            patch.object(feature_log.subprocess, "run", wraps=subprocess.run) as git,
        ):
            result = self.cli(self.add_arguments())

        self.assertEqual(result.status, 0, result.stderr)
        git.assert_called_once_with(
            ["git", "-C", str(default_checkout), "show", "-s", "--format=%cI", sha_one],
            capture_output=True, text=True, check=False,
        )

    def test_missing_commit_refuses_each_new_note_writer_without_writing(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Existing result")).status, 0)
        saved = self.proposal_arguments(
            "C901", self.add_arguments(title="Existing proposal", link=sha_two),
        )
        self.assertEqual(self.cli(saved).status, 0)
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()
        before_proposals = {
            path.relative_to(self.state): path.read_bytes()
            for path in (self.state / "proposals").rglob("*.json")
        }
        unknown_sha = "f" * 40
        project = feature_log.PROJECTS["hana"]._replace(
            checkouts=(default_checkout, self.root / "missing-checkout"),
        )
        for verb in ("add", "supersede", "propose"):
            with self.subTest(verb=verb), patch.dict(feature_log.PROJECTS, {"hana": project}):
                arguments = self.add_arguments(title="Undated result", link=unknown_sha)
                arguments[0] = verb
                if verb == "supersede":
                    arguments.extend(("--old", "Existing result"))
                elif verb == "propose":
                    arguments.extend(("--review", "C902"))

                result = self.cli(arguments)

                self.assertEqual(result, CliResult(
                    2, "", f"feature_log: cannot date commit {unknown_sha}: "
                    + "no checkout of natepiano/hana holds it; pass --completed\n",
                ))
                self.assertEqual(self.vault_snapshot(), before_vault)
                self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
                self.assertEqual({
                    path.relative_to(self.state): path.read_bytes()
                    for path in (self.state / "proposals").rglob("*.json")
                }, before_proposals)
                self.assert_no_temporary_files()

    def test_supersede_defaults_to_the_replacement_commits_date(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Existing result")).status, 0)
        checkout = self.root / "replacement-checkout"
        (sha,) = temporary_checkout(checkout, dates=("2026-10-05T01:30:00+00:00",))
        project = feature_log.PROJECTS["hana"]._replace(checkouts=(checkout,))
        arguments = self.add_arguments(title="Replacement result", link=sha)
        arguments[0] = "supersede"
        arguments.extend(("--old", "Existing result"))
        with patch.dict(feature_log.PROJECTS, {"hana": project}):
            result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        replacement = feature_log.parse_note(self.note("hana", "replacement result"))
        self.assertEqual(replacement["completed_date"], "2026-10-04")
        self.assertEqual(replacement["date_created"], TODAY)
        self.assertEqual(replacement["date_modified"], TODAY)
        old = feature_log.parse_note(self.note("hana", "existing result"))
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["completed_date"], TODAY)

    def test_proposal_keeps_commit_date_without_git_on_correction_and_landing(self) -> None:
        checkout = self.root / "proposal-checkout"
        (sha,) = temporary_checkout(checkout, dates=("2026-10-07T01:30:00+00:00",))
        project = feature_log.PROJECTS["hana"]._replace(checkouts=(checkout,))
        arguments = self.proposal_arguments(
            "C900", self.add_arguments(title="Dated proposal", link=sha),
        )
        with patch.dict(feature_log.PROJECTS, {"hana": project}):
            result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(self.proposal("C900")["args"]["completed"], "2026-10-06")
        self.assertEqual(self.vault_snapshot(), {})
        self.assertEqual(self.events(), [])
        corrected = self.proposal_arguments("C900", self.add_arguments(
            title="Dated proposal", link=sha_two, summary="The corrected behavior is available.",
        ))
        with patch.object(
            feature_log.subprocess, "run", side_effect=AssertionError("unexpected git lookup"),
        ) as git:
            correction = self.cli(corrected)
            self.assertEqual(correction.status, 0, correction.stderr)
            self.assertEqual(self.proposal("C900")["args"]["completed"], "2026-10-06")
            landed = self.cli(["add", "--review", "C900"])

        git.assert_not_called()
        self.assertEqual(landed.status, 0, landed.stderr)
        note = feature_log.parse_note(self.note("hana", "dated proposal"))
        self.assertEqual(note["completed_date"], "2026-10-06")
        self.assertEqual(note["date_created"], TODAY)
        self.assertEqual(note["date_modified"], TODAY)
        self.assertEqual(note["link"], f"https://github.com/natepiano/hana/commit/{sha_two}")
        self.assertEqual(self.proposal("C900")["status"], "landed")

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
                    link=f"https://github.com/natepiano/cargo-liner/commit/{sha_one}"
                ),
                "link",
            ),
            ("no evidence", self.add_arguments(evidence=()), "evidence"),
            ("bullet summary", self.add_arguments(summary="- not a summary"), "summary"),
            ("multiline summary", self.add_arguments(summary="first\nsecond"), "summary"),
            (
                "claude crate",
                self.add_arguments(project="claude", crates=("hana",)),
                "claude does not accept crates",
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
            link=sha_two,
            crates=("hana", "hana_lagrange"),
        ) + ["--backfill"]
        explicit = self.add_arguments(
            title="Explicit release derivation",
            link=sha_three,
            crates=("hana_lagrange",),
        ) + ["--backfill", "--release", "hana_lagrange@1.2.3-alpha.1"]

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
            "release:\n  - hana_lagrange@1.2.3-alpha.1\n",
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

    def test_add_refuses_pending_and_exact_releases_before_any_write(self) -> None:
        for backfill, releases in (
            (True, ["hana_lagrange@Unreleased", "hana_lagrange@1.2.3"]),
            (True, ["hana_lagrange@1.2.3", "hana_lagrange@Unreleased"]),
            (True, ["hana_lagrange@2.3.4", "hana_lagrange@Unreleased",
                    "hana_lagrange@1.2.3"]),
            (False, ["hana_lagrange@1.2.3"]),
        ):
            with self.subTest(backfill=backfill, releases=releases):
                arguments = self.add_arguments(crates=("hana_lagrange",))
                if backfill:
                    arguments.append("--backfill")
                arguments.extend(("--release", *releases))
                before_vault = self.vault_snapshot()

                result = self.cli(arguments)

                self.assertEqual(result, CliResult(
                    2, "", "feature_log: release cannot hold both "
                    + "hana_lagrange@Unreleased and hana_lagrange@1.2.3\n",
                ))
                self.assertEqual(self.vault_snapshot(), before_vault)
                self.assertFalse(self.note("hana", "a useful result").exists())
                self.assertFalse((self.state / "events.jsonl").exists())
                self.assert_no_temporary_files()

    def test_release_value_errors_are_reported_before_mixed_releases(self) -> None:
        for release, reason in (
            ("hana_lagrange", "invalid release hana_lagrange"),
            ("hana_lagrange@1.2", "invalid release version 1.2"),
            ("hana@1.2.3", "release crate hana is not published"),
            ("hana_liminal@1.2.3", "release crate hana_liminal is not in --crate"),
        ):
            with self.subTest(release=release):
                arguments = self.add_arguments(crates=("hana", "hana_lagrange"))
                arguments.extend((
                    "--backfill", "--release", "hana_lagrange@Unreleased",
                    "hana_lagrange@1.2.3", release,
                ))
                before_vault = self.vault_snapshot()

                result = self.cli(arguments)

                self.assertEqual(result, CliResult(2, "", f"feature_log: {reason}\n"))
                self.assertEqual(self.vault_snapshot(), before_vault)
                self.assertFalse((self.state / "events.jsonl").exists())
                self.assert_no_temporary_files()

    def test_title_normalization_keeps_underscores_and_enforces_the_limit(self) -> None:
        result = self.cli(self.add_arguments(title="  Hello--Crate_name!!  There  "))
        self.assertEqual(result.status, 0)
        self.assertTrue(self.note("hana", "hello crate_name there").is_file())

        exactly_fifty = self.add_arguments(title="b" * 50, link=sha_two)
        self.assertEqual(self.cli(exactly_fifty).status, 0)
        self.assertTrue(self.note("hana", "b" * 50).is_file())

        self.assert_rejected(self.add_arguments(title="c" * 51, link=sha_three), "50")

    def test_same_normalized_title_with_a_different_link_is_rejected(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Same Result", link=sha_one)).status, 0)
        self.assert_rejected(
            self.add_arguments(title="Same--Result!!", link=sha_two),
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
        first_day = "2026-10-09"
        next_day = "2026-10-11"
        add_arguments = self.add_arguments(title="Midnight Add", link=sha_one)
        with patch.object(feature_log, "today", return_value=first_day):
            self.assertEqual(self.cli([*add_arguments, "--completed", first_day]).status, 0)
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

        old_arguments = self.add_arguments(title="Midnight Old", link=sha_two)
        replacement_arguments = self.add_arguments(title="Midnight New", link=sha_three)
        with patch.object(feature_log, "today", return_value=first_day):
            self.assertEqual(self.cli(old_arguments).status, 0)
            self.assertEqual(self.cli([*replacement_arguments, "--completed", first_day]).status, 0)
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

    def test_supersede_refuses_mixed_releases_and_keeps_the_old_note_live(self) -> None:
        added = self.cli(
            self.add_arguments(title="Old pending result", crates=("hana_lagrange",))
        )
        self.assertEqual(added.status, 0, added.stderr)
        old_path = self.note("hana", "old pending result")
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()
        arguments = self.add_arguments(
            title="Replacement result", crates=("hana_lagrange",), link=sha_two,
        )
        arguments[0] = "supersede"
        arguments.extend((
            "--old", "Old pending result", "--backfill", "--release",
            "hana_lagrange@Unreleased", "hana_lagrange@1.2.3",
        ))

        result = self.cli(arguments)

        self.assertEqual(result, CliResult(
            2, "", "feature_log: release cannot hold both "
            + "hana_lagrange@Unreleased and hana_lagrange@1.2.3\n",
        ))
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
        old = feature_log.parse_note(old_path)
        self.assertEqual(old["state"], "live")
        self.assertEqual(old["replaced_by"], [])
        self.assertFalse(self.note("hana", "replacement result").exists())
        self.assert_no_temporary_files()

    def test_supersede_writes_the_new_note_and_updates_each_old_note(self) -> None:
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(self.add_arguments(title="Old A", link=sha_one)).status, 0)
            self.assertEqual(self.cli(self.add_arguments(title="Old B", link=sha_two)).status, 0)
        arguments = self.add_arguments(title="Replacement", link=sha_three)
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
        self.assertEqual(self.cli(self.add_arguments(title="Old Result", link=sha_one)).status, 0)
        replacement = self.add_arguments(title="New Result", link=sha_two)
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
        absent = self.add_arguments(title="Replacement", link=sha_two)
        absent[0] = "supersede"
        absent.extend(("--old", "Missing Result"))
        self.assert_rejected(absent, "live")

        itself = self.add_arguments(title="Same Result", link=sha_three)
        itself[0] = "supersede"
        itself.extend(("--old", "Same Result"))
        self.assert_rejected(itself, "old")

    def test_supersede_allows_old_link_but_rejects_an_unrelated_live_holder(self) -> None:
        self.assertEqual(
            self.cli(self.add_arguments(title="Retitled Old", link=sha_one)).status,
            0,
        )
        retitle = self.add_arguments(title="Retitled New", link=sha_one)
        retitle[0] = "supersede"
        retitle.extend(("--old", "Retitled Old"))

        retitled = self.cli(retitle)

        self.assertEqual(retitled.status, 0)
        self.assertIn("added: feature-log/hana/retitled new.md\n", retitled.stdout)
        self.assertIn("superseded: retitled old → retitled new\n", retitled.stdout)

        self.assertEqual(
            self.cli(self.add_arguments(title="Shared Old", link=sha_two)).status,
            0,
        )
        third_holder = self.add_arguments(title="Third Holder", link=sha_two)
        third_holder.append("--separate-result")
        self.assertEqual(self.cli(third_holder).status, 0)
        blocked_retitle = self.add_arguments(title="Shared New", link=sha_two)
        blocked_retitle[0] = "supersede"
        blocked_retitle.extend(("--old", "Shared Old"))

        self.assert_rejected(blocked_retitle, "third holder")

    def test_split_adds_a_second_replacement_and_repeat_changes_nothing(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Broad result")).status, 0)
        first = self.add_arguments(title="First result", link=sha_two)
        first[0] = "supersede"
        first.extend(("--old", "Broad result"))
        self.assertEqual(self.cli(first).status, 0)
        old_path = self.note("hana", "broad result")
        old = feature_log.parse_note(old_path)
        second = self.add_arguments(title="Second result", link=sha_three)
        second[0] = "supersede"
        second.extend(("--old", "Broad result", "--split"))

        result = self.cli(second)

        self.assertEqual(result, CliResult(
            0, "added: feature-log/hana/second result.md\n"
            + "superseded: broad result → second result\n", "",
        ))
        expected = old.copy()
        expected["replaced_by"] = ["first result", "second result"]
        self.assertEqual(feature_log.parse_note(old_path), expected)
        for title in ("first result", "second result"):
            replacement = feature_log.parse_note(self.note("hana", title))
            self.assertEqual(replacement["state"], "live")
            self.assertEqual(replacement["replaced_by"], [])
        event: SupersedeEvent = {
            "at": EVENT_TIME, "verb": "supersede", "project": "hana",
            "title": "second result", "kind": "feature",
            "summary": "People can use the useful result.", "release": [],
            "backfill": False, "old": ["broad result"],
        }
        self.assertEqual(self.events()[-1], event)
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()

        repeated = self.cli(second)

        self.assertEqual(repeated, CliResult(
            0, "unchanged: feature-log/hana/second result.md\n", "",
        ))
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 3 notes\n", ""))
        self.assert_no_temporary_files()

    def test_superseded_old_still_requires_split_for_another_replacement(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Broad result")).status, 0)
        first = self.add_arguments(title="First result", link=sha_two)
        first[0] = "supersede"
        first.extend(("--old", "Broad result"))
        self.assertEqual(self.cli(first).status, 0)
        second = self.add_arguments(title="Second result", link=sha_three)
        second[0] = "supersede"
        second.extend(("--old", "Broad result"))

        self.assert_rejected(second, "old note broad result is not live")

    def test_split_accepts_a_live_old_note(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Live broad result")).status, 0)
        arguments = self.add_arguments(title="Narrow result", link=sha_two)
        arguments[0] = "supersede"
        arguments.extend(("--old", "Live broad result", "--split"))

        result = self.cli(arguments)

        self.assertEqual(result, CliResult(
            0, "added: feature-log/hana/narrow result.md\n"
            + "superseded: live broad result → narrow result\n", "",
        ))
        old = feature_log.parse_note(self.note("hana", "live broad result"))
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["replaced_by"], ["narrow result"])
        self.assertEqual(
            feature_log.parse_note(self.note("hana", "narrow result"))["state"], "live",
        )
        self.assert_no_temporary_files()

    def test_split_is_refused_with_review_and_is_not_a_propose_flag(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Reviewed broad result")).status, 0)
        proposal = self.proposal_arguments(
            "C910", self.add_arguments(title="Reviewed replacement", link=sha_two),
            old=("Reviewed broad result",),
        )
        self.assertEqual(self.cli(proposal).status, 0)
        proposal_path = self.state / "proposals" / "C910.json"
        before_proposal = proposal_path.read_bytes()

        self.assert_rejected(
            ["supersede", "--review", "C910", "--split"],
            "--split cannot be combined with --review",
        )
        self.assertEqual(proposal_path.read_bytes(), before_proposal)
        self.assert_rejected(proposal + ["--split"], "unrecognized arguments: --split")
        self.assertEqual(proposal_path.read_bytes(), before_proposal)

    def test_split_three_replacements_share_an_accepting_link_with_separate_result(self) -> None:
        self.assertEqual(self.cli(self.add_arguments(title="Broad result")).status, 0)
        titles = ("alpha result", "beta result", "gamma result")
        for title in titles:
            arguments = self.add_arguments(
                title=title, link=sha_two, crates=("hana_lagrange",),
            )
            arguments[0] = "supersede"
            arguments.extend((
                "--old", "Broad result", "--split", "--separate-result",
                "--backfill", "--release", "hana_lagrange@Unreleased",
            ))
            with self.subTest(title=title):
                result = self.cli(arguments)
                self.assertEqual(result.status, 0, result.stderr)
                self.assertIn(f"superseded: broad result → {title}\n", result.stdout)
                note = feature_log.parse_note(self.note("hana", title))
                self.assertEqual(note["state"], "live")
                self.assertEqual(note["link"], f"https://github.com/natepiano/hana/commit/{sha_two}")
                self.assertEqual(note["release"], ["hana_lagrange@Unreleased"])
        old = feature_log.parse_note(self.note("hana", "broad result"))
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["replaced_by"], list(titles))
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 4 notes\n", ""))
        self.assert_no_temporary_files()

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
        self.assertIn(f'completed_date: "[[{TODAY}]]"\n', amended)
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
        corrected_link = f"https://github.com/natepiano/hana/commit/{sha_two}"

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
        link = f"https://github.com/natepiano/hana/commit/{sha_two}"
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
        valid_link = f"https://github.com/natepiano/hana/commit/{sha_two}"
        for link, completed, reason in (
            ("", "2026-10-01", "link"),
            ("https://github.com/natepiano/hana/commit/abc", "2026-10-01", "link"),
            (f"https://github.com/natepiano/nixos/commit/{sha_two}", "2026-10-01", "link"),
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
            self.cli(self.add_arguments(title="Other live result", link=sha_two)).status,
            0,
        )
        other_path = self.note("hana", "other live result")
        other_contents = other_path.read_bytes()
        shared_link = f"https://github.com/natepiano/hana/commit/{sha_two}"
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
            self.cli(self.add_arguments(title="Retired result", link=sha_two)).status,
            0,
        )
        replacement = self.add_arguments(title="Current replacement", link=sha_three)
        replacement[0] = "supersede"
        self.assertEqual(self.cli(replacement + ["--old", "Retired result"]).status, 0)
        old_path = self.note("hana", "retired result")
        old_contents = old_path.read_bytes()
        link = f"https://github.com/natepiano/hana/commit/{sha_two}"

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
        ):
            with self.subTest(release=release):
                self.assert_rejected(prefix + ["--backfill", "--release", release], reason)

    def test_amend_marks_a_live_backfilled_note_pending_and_repeat_is_unchanged(self) -> None:
        title = "Backfilled result"
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            added = self.cli(
                self.add_arguments(title=title, crates=("hana_lagrange",))
                + ["--backfill", "--completed", "2026-10-01"]
            )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        original = feature_log.parse_note(path)
        self.assertEqual(original["release"], [])
        arguments = [
            "amend", "--project", "hana", "--title", title, "--backfill",
            "--release", "hana_lagrange@Unreleased",
        ]

        result = self.cli(arguments)

        self.assertEqual(result, CliResult(
            0, "amended: feature-log/hana/backfilled result.md (release)\n", "",
        ))
        expected = original.copy()
        expected["release"] = ["hana_lagrange@Unreleased"]
        expected["date_modified"] = TODAY
        self.assertEqual(feature_log.parse_note(path), expected)
        event: AmendEvent = {
            "at": EVENT_TIME, "verb": "amend", "project": "hana",
            "title": title.lower(), "fields": ["release"], "backfill": True,
            "old_link": original["link"], "new_link": original["link"],
            "old_completed": "2026-10-01", "new_completed": "2026-10-01",
            "releases_added": ["hana_lagrange@Unreleased"],
        }
        self.assertEqual(self.events()[-1], event)
        self.assertEqual(len(self.events()), 2)
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()

        repeated = self.cli(arguments)

        self.assertEqual(repeated, CliResult(
            0, "unchanged: feature-log/hana/backfilled result.md\n", "",
        ))
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
        self.assert_no_temporary_files()

    def test_amend_summary_kind_and_pending_release_are_journaled_before_one_note_write(self) -> None:
        title = "Pending bullet"
        added = self.cli(
            self.add_arguments(title=title, crates=("hana_lagrange",))
            + ["--backfill"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        original = feature_log.parse_note(path)
        before_note = path.read_bytes()
        summary = "Repair `pending` output without losing **its text**."
        event: AmendEvent = {
            "at": EVENT_TIME, "verb": "amend", "project": "hana",
            "title": title.lower(), "fields": ["summary", "kind", "release"],
            "backfill": True, "old_link": original["link"],
            "new_link": original["link"], "old_completed": TODAY,
            "new_completed": TODAY, "releases_added": ["hana_lagrange@Unreleased"],
        }
        write_note = cast(
            Callable[[Path, feature_log.Note], None], getattr(feature_log, "_write_note")
        )
        writes: list[Path] = []

        def write_after_journal(note_path: Path, note: feature_log.Note) -> None:
            self.assertEqual(self.events()[-1], event)
            self.assertEqual(path.read_bytes(), before_note)
            writes.append(note_path)
            write_note(note_path, note)

        with patch.object(feature_log, "_write_note", side_effect=write_after_journal):
            result = self.cli([
                "amend", "--project", "hana", "--title", title, "--backfill",
                "--summary", summary, "--kind", "fix",
                "--release", "hana_lagrange@Unreleased",
            ])

        self.assertEqual(result, CliResult(
            0, "amended: feature-log/hana/pending bullet.md (summary, kind, release)\n", "",
        ))
        self.assertEqual(writes, [path])
        expected = original.copy()
        expected["summary"] = summary
        expected["kind"] = "fix"
        expected["release"] = ["hana_lagrange@Unreleased"]
        self.assertEqual(feature_log.parse_note(path), expected)
        self.assertEqual(self.events()[-1], event)
        self.assertEqual(len(self.events()), 2)
        self.assert_no_temporary_files()

    def test_amend_pending_refusals_leave_notes_and_events_unchanged(self) -> None:
        cases = (
            ("historical result", ("hana_lagrange",), ("hana_lagrange@1.2.3",),
             "hana_lagrange@Unreleased", True,
             "historical result already holds hana_lagrange@1.2.3"),
            ("unpublished result", ("hana",), (), "hana@Unreleased", True,
             "release crate hana is not published"),
            ("missing crate result", ("hana_lagrange",), (), "hana_liminal@Unreleased", True,
             "release crate hana_liminal is not in --crate"),
            ("not backfill result", ("hana_lagrange",), (), "hana_lagrange@Unreleased", False,
             "--release requires --backfill"),
        )
        for title, crates, releases, release, backfill, reason in cases:
            with self.subTest(title=title):
                arguments = self.add_arguments(title=title, crates=crates)
                arguments.extend(("--backfill", "--separate-result"))
                for existing in releases:
                    arguments.extend(("--release", existing))
                added = self.cli(arguments)
                self.assertEqual(added.status, 0, added.stderr)
                amend = [
                    "amend", "--project", "hana", "--title", title,
                    "--summary", "This must not replace the summary.", "--kind", "fix",
                    "--release", release,
                ]
                if backfill:
                    amend.append("--backfill")
                before_events = (self.state / "events.jsonl").read_bytes()

                self.assert_rejected(amend, reason)

                self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)

    def test_amend_exact_release_on_a_pending_crate_refuses_every_change(self) -> None:
        title = "Pending result"
        added = self.cli(self.add_arguments(title=title, crates=("hana_lagrange",)))
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        self.assertEqual(feature_log.parse_note(path)["release"], ["hana_lagrange@Unreleased"])
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()
        for other_flags in ([], ["--summary", "This must not replace the summary.",
                                 "--kind", "fix"]):
            with self.subTest(other_flags=other_flags):
                result = self.cli([
                    "amend", "--project", "hana", "--title", title, "--backfill",
                    "--release", "hana_lagrange@1.2.3", *other_flags,
                ])

                self.assertEqual(result, CliResult(
                    2, "", "feature_log: pending result already holds hana_lagrange@Unreleased\n",
                ))
                self.assertEqual(self.vault_snapshot(), before_vault)
                self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
                self.assert_no_temporary_files()

    def test_amend_exact_release_of_another_crate_preserves_pending_and_applies_changes(self) -> None:
        title = "Shared pending result"
        added = self.cli(
            self.add_arguments(title=title, crates=("hana_lagrange", "hana_liminal"))
            + ["--backfill", "--release", "hana_lagrange@Unreleased"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", title.lower())
        original = feature_log.parse_note(path)
        summary = "People can use the corrected shared result."

        result = self.cli([
            "amend", "--project", "hana", "--title", title, "--backfill",
            "--release", "hana_liminal@1.2.3", "--summary", summary, "--kind", "fix",
        ])

        self.assertEqual(result, CliResult(
            0, "amended: feature-log/hana/shared pending result.md (summary, kind, release)\n", "",
        ))
        expected = original.copy()
        expected["summary"] = summary
        expected["kind"] = "fix"
        expected["release"] = ["hana_lagrange@Unreleased", "hana_liminal@1.2.3"]
        self.assertEqual(feature_log.parse_note(path), expected)
        event: AmendEvent = {
            "at": EVENT_TIME, "verb": "amend", "project": "hana",
            "title": title.lower(), "fields": ["summary", "kind", "release"],
            "backfill": True, "old_link": original["link"], "new_link": original["link"],
            "old_completed": TODAY, "new_completed": TODAY,
            "releases_added": ["hana_liminal@1.2.3"],
        }
        self.assertEqual(self.events()[-1], event)
        self.assertEqual(len(self.events()), 2)
        self.assert_no_temporary_files()

    def test_amend_cannot_mark_a_superseded_note_pending(self) -> None:
        added = self.cli(
            self.add_arguments(title="Retired result", crates=("hana_lagrange",))
            + ["--backfill"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        replacement = self.add_arguments(
            title="Current result", crates=("hana_lagrange",), link=sha_two,
        )
        replacement[0] = "supersede"
        replacement.extend(("--old", "Retired result", "--backfill"))
        self.assertEqual(self.cli(replacement).status, 0)
        before_events = (self.state / "events.jsonl").read_bytes()

        self.assert_rejected([
            "amend", "--project", "hana", "--title", "Retired result", "--backfill",
            "--summary", "This must not replace the summary.", "--kind", "fix",
            "--release", "hana_lagrange@Unreleased",
        ], "a superseded note cannot be pending")

        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)

    def test_amend_refuses_pending_and_exact_releases_of_the_same_crate_in_one_call(self) -> None:
        release_arguments = (
            ["--release", "hana_lagrange@1.2.3", "hana_lagrange@Unreleased"],
            ["--release", "hana_lagrange@Unreleased", "hana_lagrange@1.2.3"],
            ["--release", "hana_lagrange@1.2.3", "--release", "hana_lagrange@Unreleased"],
            ["--release", "hana_lagrange@Unreleased", "--release", "hana_lagrange@1.2.3"],
        )
        for number, releases in enumerate(release_arguments):
            with self.subTest(releases=releases):
                title = f"Mixed release result {number}"
                added = self.cli(
                    self.add_arguments(title=title, crates=("hana_lagrange",))
                    + ["--backfill", "--separate-result"]
                )
                self.assertEqual(added.status, 0, added.stderr)
                original = feature_log.parse_note(self.note("hana", title.lower()))
                self.assertEqual(original["state"], "live")
                self.assertEqual(original["release"], [])
                self.assert_no_temporary_files()
                before_vault = self.vault_snapshot()
                before_events = (self.state / "events.jsonl").read_bytes()

                result = self.cli([
                    "amend", "--project", "hana", "--title", title, "--backfill",
                    "--summary", "This must not replace the summary.", "--kind", "fix",
                    *releases,
                ])

                self.assertEqual(result, CliResult(
                    2, "", "feature_log: release cannot hold both "
                    + "hana_lagrange@Unreleased and hana_lagrange@1.2.3\n",
                ))
                self.assertEqual(self.vault_snapshot(), before_vault)
                self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
                self.assert_no_temporary_files()

    def test_amend_accepts_pending_and_exact_releases_of_different_crates_in_one_call(self) -> None:
        added = self.cli(
            self.add_arguments(title="Shared result", crates=("hana_lagrange", "hana_liminal"))
            + ["--backfill"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", "shared result")
        original = feature_log.parse_note(path)
        self.assertEqual(original["release"], [])
        releases = ["hana_lagrange@Unreleased", "hana_liminal@1.2.3"]

        result = self.cli([
            "amend", "--project", "hana", "--title", "Shared result", "--backfill",
            "--release", *releases,
        ])

        self.assertEqual(result, CliResult(
            0, "amended: feature-log/hana/shared result.md (release)\n", "",
        ))
        expected = original.copy()
        expected["release"] = releases
        self.assertEqual(feature_log.parse_note(path), expected)
        self.assertEqual(len(self.events()), 2)
        event: AmendEvent = {
            "at": EVENT_TIME, "verb": "amend", "project": "hana",
            "title": "shared result", "fields": ["release"], "backfill": True,
            "old_link": original["link"], "new_link": original["link"],
            "old_completed": TODAY, "new_completed": TODAY, "releases_added": releases,
        }
        self.assertEqual(self.events()[-1], event)
        self.assert_no_temporary_files()

    def test_amend_pending_preserves_an_exact_release_of_another_crate(self) -> None:
        added = self.cli(
            self.add_arguments(title="Shared result", crates=("hana_lagrange", "hana_liminal"))
            + ["--backfill", "--release", "hana_liminal@1.2.3"]
        )
        self.assertEqual(added.status, 0, added.stderr)

        result = self.cli([
            "amend", "--project", "hana", "--title", "Shared result", "--backfill",
            "--release", "hana_lagrange@Unreleased",
        ])

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(
            feature_log.parse_note(self.note("hana", "shared result"))["release"],
            ["hana_lagrange@Unreleased", "hana_liminal@1.2.3"],
        )

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
                            title=title, link=sha_two, crates=("hana_lagrange",)
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
                            title=title, link=sha_two, crates=("hana_lagrange",)
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
                    new_link = f"https://github.com/natepiano/hana/commit/{sha_two}"
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
        correction = prefix + ["--link", sha_two, "--completed", "2026-09-30"]
        reversal = prefix + ["--link", sha_one, "--completed", "2026-10-01"]
        for arguments in (correction, reversal, correction):
            result = self.cli(arguments)
            self.assertEqual(result.status, 0, result.stderr)
        events = [event for event in self.events() if event["verb"] == "amend"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0], events[2])
        self.assertEqual(
            feature_log.parse_note(self.note("hana", title.lower()))["link"],
            f"https://github.com/natepiano/hana/commit/{sha_two}",
        )

    def test_amend_event_records_provenance_and_releases_in_one_correction(self) -> None:
        title = "Corrected historical result"
        added = self.cli(
            self.add_arguments(title=title, crates=("hana_lagrange",))
            + ["--backfill", "--completed", "2026-10-01"]
        )
        self.assertEqual(added.status, 0, added.stderr)
        new_link = f"https://github.com/natepiano/hana/commit/{sha_two}"
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
                "old_link": f"https://github.com/natepiano/hana/commit/{sha_one}",
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
            self.cli(self.add_arguments(title="Old Heading Note", link=sha_one)).status,
            0,
        )
        self.assertEqual(
            self.cli(self.add_arguments(title="Amend Heading Note", link=sha_two)).status,
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
                                link=sha_three,
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
            link=sha_one,
            crates=("hana", "hana_catalyst"),
        ) + ["--area", "widgets", "--completed", "2026-10-01", "--backfill"]
        second = self.add_arguments(
            title="Newer Panel",
            summary="The newer panel is visible.",
            behavior="New panel behavior.",
            link=sha_two,
            crates=("hana",),
        ) + ["--area", "panels", "--completed", "2026-10-09", "--backfill"]
        third = self.add_arguments(
            title="Newest Widget",
            kind="perf",
            summary="The newest widget is quick.",
            behavior="Fast searchable behavior.",
            link=sha_three,
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
                + f"https://github.com/natepiano/hana/commit/{sha_three}\n"
                + "2026-10-09 feature newer panel — The newer panel is visible. [hana] (panels) "
                + f"https://github.com/natepiano/hana/commit/{sha_two}\n"
                + "2026-10-01 fix older widget — The older widget works. [hana, hana_catalyst] (widgets) "
                + f"https://github.com/natepiano/hana/commit/{sha_one}\n",
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
                link=sha_one,
                crates=("hana", "hana_catalyst"),
            )
            + ["--area", "widgets", "--backfill"],
            self.add_arguments(
                title="Panel Result",
                link=sha_two,
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
        self.assertEqual(self.cli(self.add_arguments(title="Old", link=sha_one)).status, 0)
        replacement = self.add_arguments(title="New", link=sha_two)
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
            ("Valid Scalar", sha_one),
            ("Colon Scalar", sha_two),
            ("Comment Scalar", sha_three),
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

    def test_check_reports_a_stored_note_with_pending_and_exact_releases(self) -> None:
        added = self.cli(
            self.add_arguments(title="Mixed stored release", crates=("hana_lagrange",))
        )
        self.assertEqual(added.status, 0, added.stderr)
        path = self.note("hana", "mixed stored release")
        _ = path.write_text(
            path.read_text(encoding="utf-8").replace(
                "release:\n  - hana_lagrange@Unreleased\n",
                "release:\n  - hana_lagrange@1.2.3\n  - hana_lagrange@Unreleased\n",
            ),
            encoding="utf-8",
        )
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()

        result = self.cli(["check"])

        self.assertEqual(result, CliResult(
            1, "feature-log/hana/mixed stored release.md: release cannot hold both "
            + "hana_lagrange@Unreleased and hana_lagrange@1.2.3\n", "",
        ))
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)
        self.assert_no_temporary_files()

    def test_check_reports_each_kind_of_broken_fixture(self) -> None:
        fixtures = (
            ("missing key", sha_one),
            ("unknown key", sha_two),
            ("live replacement", sha_three),
            ("dangling replacement", sha_four),
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
        for title, link in (("duplicate", sha_one), ("syntax", sha_two), ("wrong name", sha_three)):
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

        supersede = self.add_arguments(title="Event New", link=sha_two)
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
                link=sha_one,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(implicit).status, 0)

        implicit_correction = self.proposal_arguments(
            "C904",
            self.add_arguments(
                title="Implicit Proposal Date",
                summary="The corrected implicit-date summary.",
                link=sha_one,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-02"):
            self.assertEqual(self.cli(implicit_correction).status, 0)
        self.assertEqual(self.proposal("C904")["args"]["completed"], TODAY)

        with patch.object(feature_log, "today", return_value="2026-10-03"):
            self.assertEqual(self.cli(["add", "--review", "C904"]).status, 0)
        implicit_contents = self.note(
            "hana", "implicit proposal date"
        ).read_text(encoding="utf-8")
        self.assertIn(f'completed_date: "[[{TODAY}]]"\n', implicit_contents)

        explicit = self.proposal_arguments(
            "C905",
            self.add_arguments(
                title="Explicit Proposal Date",
                summary="The initial explicit-date summary.",
                link=sha_two,
            ),
        )
        with patch.object(feature_log, "today", return_value="2026-10-01"):
            self.assertEqual(self.cli(explicit).status, 0)

        explicit_correction = self.proposal_arguments(
            "C905",
            self.add_arguments(
                title="Explicit Proposal Date",
                summary="The corrected explicit-date summary.",
                link=sha_two,
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
            self.add_arguments(title="Replacement Proposal", link=sha_two),
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
        self.assertIn(f'completed_date: "[[{TODAY}]]"\n', contents)
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
            self.cli(self.add_arguments(title="Verb Old", link=sha_one)).status,
            0,
        )
        supersede_proposal = self.proposal_arguments(
            "C902",
            self.add_arguments(title="Verb New", link=sha_two),
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
            self.add_arguments(title="Add Only", link=sha_three),
        )
        self.assertEqual(self.cli(add_proposal).status, 0)
        self.assert_rejected(["supersede", "--review", "C903"], "add")

    def test_same_merge_results_require_separate_result_to_land_together(self) -> None:
        first = self.proposal_arguments(
            "C906",
            self.add_arguments(title="Shared Merge First", link=sha_one),
        )
        first.append("--separate-result")
        second = self.proposal_arguments(
            "C907",
            self.add_arguments(title="Shared Merge Second", link=sha_one),
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
            self.add_arguments(title="Unmarked Merge First", link=sha_two),
        )
        unmarked_second = self.proposal_arguments(
            "C909",
            self.add_arguments(title="Unmarked Merge Second", link=sha_two),
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

    def test_claude_review_rows_name_their_list_and_land_beside_hana_rows(self) -> None:
        claude_link = f"https://github.com/natepiano/claude_commands/commit/{sha_two}"
        claude = self.add_arguments(
            project="claude", title="Change Review List", crates=(), link=sha_two,
            summary="Every production keeps a change review list.",
        ) + ["--production", "build-followups", "--unit", "build-report"]
        self.assert_rejected(self.proposal_arguments("C1", claude), "<list>/C1")
        self.assertFalse((self.state / "proposals/C1.json").exists())

        hana = self.proposal_arguments("C1", self.add_arguments(title="Hana Row One"))
        self.assertEqual(self.cli(hana).status, 0)
        proposed = self.cli(self.proposal_arguments("build-followups/C1", claude))
        self.assertEqual(
            proposed,
            CliResult(0, "proposed: build-followups/C1 (add change review list)\n", ""),
        )
        self.assertEqual(self.vault_snapshot(), {})
        self.assertEqual(self.proposal("C1")["args"]["project"], "hana")

        landed = self.cli(["add", "--review", "build-followups/C1"])

        path = self.note("claude", "change review list")
        self.assertEqual(landed, CliResult(
            0, f"added: {path.relative_to(self.vault)}\n[[change review list]]\n", "",
        ))
        contents = path.read_text(encoding="utf-8")
        for line in (
            "crate: []\n",
            f"link: {claude_link}\n",
            "production: build-followups\n",
            'project: "[[claude]]"\n',
            "release: []\n",
            '  - "[[feature-log - claude.base]]"\n',
            "unit:\n  - build-report\n",
        ):
            self.assertIn(line, contents)
        landed_proposal = self.proposal("build-followups/C1")
        self.assertEqual(landed_proposal["status"], "landed")
        self.assertEqual(landed_proposal["row"], "build-followups/C1")
        self.assertEqual(self.proposal("C1")["status"], "open")
        self.assertEqual(self.cli(["check"]), CliResult(0, "ok: 1 notes\n", ""))
        self.assertIn(
            "claude:\n+ change review list — Every production keeps a change review list.\n",
            self.cli(["digest"]).stdout,
        )

        dropped_row = self.proposal_arguments(
            "build-followups/C2",
            self.add_arguments(project="claude", title="Dropped Claude Row", crates=(), link=sha_three),
        )
        self.assertEqual(self.cli(dropped_row).status, 0)
        self.assertEqual(
            self.cli(["drop", "--review", "build-followups/C2", "--verdict", "the list is empty"]),
            CliResult(0, "dropped: build-followups/C2\n", ""),
        )
        self.assertFalse(self.note("claude", "dropped claude row").exists())
        self.assert_rejected(
            self.proposal_arguments("../C3", claude), "review row must match"
        )

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
            attempted_read, attempted_write = os.pipe()
            self.addCleanup(os.close, attempted_read)
            self.addCleanup(os.close, attempted_write)
            driver = "\n".join([
                "import fcntl, os, runpy",
                "real_flock = fcntl.flock",
                "def observe(fd, operation):",
                "    if operation == fcntl.LOCK_EX:",
                "        try: real_flock(fd, operation | fcntl.LOCK_NB)",
                "        except BlockingIOError:",
                f"            os.write({attempted_write}, b'B')",
                "        else: return",
                "    real_flock(fd, operation)",
                "fcntl.flock = observe",
                "runpy.run_path('scripts/feature_log/feature_log.py', run_name='__main__')",
            ])
            process: subprocess.Popen[str] = subprocess.Popen(
                [sys.executable, "-c", driver, "digest"],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, pass_fds=(attempted_write,),
            )
            self.addCleanup(process.communicate, timeout=TEST_HANG_GUARD_SECS)
            self.addCleanup(process.kill)
            try:
                ready, _, _ = select.select([attempted_read], [], [], TEST_HANG_GUARD_SECS)
                self.assertEqual(ready, [attempted_read], "digest never attempted its held cursor lock")
                self.assertEqual(os.read(attempted_read, 1), b"B")
                self.assertIsNone(process.poll())
            finally:
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

        try:
            stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
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


    def add_crate_note(
        self,
        title: str,
        summary: str,
        *,
        kind: str = "feature",
        crates: tuple[str, ...] = ("cargo-mend",),
        releases: tuple[str, ...] = (),
        backfill: bool = False,
    ) -> None:
        arguments = self.add_arguments(
            project="cargo-liner", title=title, summary=summary,
            kind=kind, crates=crates,
        ) + ["--separate-result"]
        for release in releases:
            arguments.extend(("--release", release))
        if backfill:
            arguments.append("--backfill")
        result = self.cli(arguments)
        self.assertEqual(result.status, 0, result.stderr)

    def supersede_crate_note(
        self, old: str, title: str, summary: str,
        *, crates: tuple[str, ...] = ("cargo-mend",),
    ) -> None:
        arguments = self.add_arguments(
            project="cargo-liner", title=title, summary=summary, crates=crates,
            link=sha_two,
        )
        arguments[0] = "supersede"
        arguments.extend(("--old", old, "--separate-result"))
        result = self.cli(arguments)
        self.assertEqual(result.status, 0, result.stderr)

    def compare_arguments(
        self, crate: str, changelog: Path, *, contents: Path | None = None,
    ) -> list[str]:
        arguments = [
            "changelog", "--crate", crate, "--version", "Unreleased",
            "--compare", str(changelog),
        ]
        if contents is not None:
            arguments.extend(("--save", str(contents)))
        return arguments

    def save_match(self, crate: str, section: str) -> Path:
        changelog = self.root / f"{crate}-CHANGELOG.md"
        contents = self.state / "release" / f"{crate}.json"
        _ = changelog.write_text(section, encoding="utf-8")
        result = self.cli(self.compare_arguments(crate, changelog, contents=contents))
        self.assertEqual(result, CliResult(0, "match\n", ""))
        self.assertTrue(contents.is_file())
        return contents

    def release_arguments(
        self, crate: str, version: str, contents: Path,
    ) -> list[str]:
        return [
            "release", "--crate", crate, "--version", version,
            "--contents", str(contents),
        ]

    def reviewed_contents(self, path: Path) -> ReviewedReleaseContents:
        return cast(ReviewedReleaseContents, json.loads(path.read_text(encoding="utf-8")))

    def off_host_cli(
        self, arguments: list[str], reader: feature_log.ForwardedReader,
        forwarder: feature_log.ReleaseForwarder,
    ) -> CliResult:
        ssh_runner = RecordingSshRunner(returncode=99)
        with patch.dict(os.environ, {}, clear=False):
            _ = os.environ.pop("FEATURE_LOG_VAULT")
            with (
                patch.object(socket, "gethostname", return_value="macbook.local"),
                patch.object(feature_log, "vault_dir", return_value=self.vault),
            ):
                result = self.capture_cli(lambda: feature_log.main(
                    arguments, ssh_runner=ssh_runner, forwarded_reader=reader,
                    release_forwarder=forwarder,
                ))
        self.assertEqual(ssh_runner.commands, [], "compare/release used the ordinary SSH runner")
        return result

    def remote_arguments(self, command: list[str]) -> list[str]:
        self.assertEqual(command[:2], ["ssh", "natedev"])
        self.assertEqual(len(command), 3)
        words = shlex.split(command[2])
        self.assertEqual(words[:2], [
            "~/.claude/scripts/lib/py", "~/.claude/scripts/feature_log/feature_log.py",
        ])
        return words[2:]

    def releases(self, title: str) -> list[str]:
        return feature_log.parse_note(self.note("cargo-liner", title))["release"]

    def release_events(self) -> list[ReleaseEvent]:
        return [event for event in self.events() if event["verb"] == "release"]

    def test_changelog_maps_every_kind_and_sorts_unchanged_summaries(self) -> None:
        records = (
            ("security fix", "security", "Close the credential leak."),
            ("ordinary fix", "fix", "Repair a `mend` pass without changing its input."),
            ("removed switch", "removal", "Remove the obsolete switch."),
            ("deprecated switch", "deprecation", "Deprecate the old switch."),
            ("faster passes", "perf", "Zulu passes finish sooner."),
            ("changed passes", "change", "Alpha passes use the new order."),
            ("zulu feature", "feature", "Zulu output includes [a guide](https://example.test/guide)."),
            ("alpha feature", "feature", "Alpha output keeps **its emphasis**."),
        )
        for title, kind, summary in records:
            self.add_crate_note(title, summary, kind=kind)

        result = self.cli(["changelog", "--crate", "cargo-mend", "--version", "Unreleased"])

        expected = (
            "## [Unreleased]\n\n### Added\n\n"
            + "- Alpha output keeps **its emphasis**.\n"
            + "- Zulu output includes [a guide](https://example.test/guide).\n\n"
            + "### Changed\n\n- Alpha passes use the new order.\n"
            + "- Zulu passes finish sooner.\n\n"
            + "### Deprecated\n\n- Deprecate the old switch.\n\n"
            + "### Removed\n\n- Remove the obsolete switch.\n\n"
            + "### Fixed\n\n- Repair a `mend` pass without changing its input.\n\n"
            + "### Security\n\n- Close the credential leak.\n"
        )
        self.assertEqual(result, CliResult(0, expected, ""))

    def test_cargo_mend_proposal_example_is_exact(self) -> None:
        self.add_crate_note(
            "mend stops after the last useful pass", "Stop after the last useful mend pass.",
            kind="fix", releases=("cargo-mend@0.23.0",), backfill=True,
        )
        result = self.cli(["changelog", "--crate", "cargo-mend", "--version", "0.23.0"])
        self.assertEqual(
            result,
            CliResult(0, "## [0.23.0]\n\n### Fixed\n\n- Stop after the last useful mend pass.\n", ""),
        )

    def test_changelog_requires_matching_crate_and_release_for_shared_result(self) -> None:
        self.add_crate_note(
            "shared result", "Share the useful result.",
            crates=("cargo-mend", "cargo-port"),
            releases=("cargo-mend@1.2.3", "cargo-port@2.3.4"), backfill=True,
        )
        self.add_crate_note(
            "other crate result", "Use the other crate.", crates=("cargo-tile",),
            releases=("cargo-tile@1.2.3",), backfill=True,
        )
        for crate, version, expected in (
            ("cargo-mend", "1.2.3", "## [1.2.3]\n\n### Added\n\n- Share the useful result.\n"),
            ("cargo-port", "2.3.4", "## [2.3.4]\n\n### Added\n\n- Share the useful result.\n"),
            ("cargo-port", "1.2.3", "## [1.2.3]\n"),
            ("cargo-mend", "Unreleased", "## [Unreleased]\n"),
        ):
            with self.subTest(crate=crate, version=version):
                result = self.cli(["changelog", "--crate", crate, "--version", version])
                self.assertEqual(result, CliResult(0, expected, ""))

    def test_released_section_keeps_a_superseded_shipped_result(self) -> None:
        self.add_crate_note(
            "shipped result", "Use the originally shipped behavior.",
            releases=("cargo-mend@1.2.3",), backfill=True,
        )
        self.supersede_crate_note("shipped result", "current result", "Use the current behavior.")

        result = self.cli(["changelog", "--crate", "cargo-mend", "--version", "1.2.3"])

        self.assertEqual(result, CliResult(
            0, "## [1.2.3]\n\n### Added\n\n- Use the originally shipped behavior.\n", "",
        ))
        current = self.cli(["changelog", "--crate", "cargo-mend", "--version", "Unreleased"])
        self.assertEqual(current, CliResult(
            0, "## [Unreleased]\n\n### Added\n\n- Use the current behavior.\n", "",
        ))

    def test_unpublished_crate_prints_nothing(self) -> None:
        for crate in ("cargo-berth", "hana", "obsidian_knife", "unknown-crate"):
            with self.subTest(crate=crate):
                result = self.cli(["changelog", "--crate", crate, "--version", "Unreleased"])
                self.assertEqual(result.status, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)

    def test_compare_uses_bullet_sets_within_unreleased_headings(self) -> None:
        self.add_crate_note("alpha feature", "Alpha result.")
        self.add_crate_note("zulu feature", "Zulu result.")
        self.add_crate_note("fixed result", "Repair the result.", kind="fix")
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "# Changelog\n\nA preamble.\n\n## [Unreleased]\n\n"
            + "### Fixed\n\n- Repair the result.\n\n"
            + "### Added\n\n- Zulu result.\n- Alpha result.\n- Alpha result.\n\n"
            + "### Security\n\n## [1.0.0] - 2026-01-01\n\n"
            + "### Added\n\n- This historical bullet is ignored.\n\n"
            + "[Unreleased]: https://example.test/compare\n",
            encoding="utf-8",
        )
        before = self.vault_snapshot()

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(0, "match\n", ""))
        self.assertEqual(self.vault_snapshot(), before)

    def test_compare_reports_missing_extra_and_misheaded_bullets(self) -> None:
        self.add_crate_note("added result", "Add the result.")
        self.add_crate_note("fixed result", "Repair the result.", kind="fix")
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Changed\n\n- Add the result.\n\n"
            + "### Fixed\n\n- A hand-written extra.\n", encoding="utf-8",
        )

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result.status, 1, result.stderr)
        self.assertNotEqual(result.stdout, "match\n")
        for detail in ("Added", "Changed", "Fixed", "Add the result.", "Repair the result.", "A hand-written extra."):
            self.assertIn(detail, result.stdout)

    def test_amended_pending_note_is_exported_compared_and_stamped(self) -> None:
        summary = "Repair `mend` output while preserving [its guide](https://example.test/guide)."
        self.add_crate_note("backfilled fix", "The old description.", backfill=True)
        amended = self.cli([
            "amend", "--project", "cargo-liner", "--title", "backfilled fix",
            "--backfill", "--summary", summary, "--kind", "fix",
            "--release", "cargo-mend@Unreleased",
        ])
        self.assertEqual(amended.status, 0, amended.stderr)
        section = f"## [Unreleased]\n\n### Fixed\n\n- {summary}\n"

        rendered = self.cli([
            "changelog", "--crate", "cargo-mend", "--version", "Unreleased",
        ])

        self.assertEqual(rendered, CliResult(0, section, ""))
        contents = self.save_match("cargo-mend", section)
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["backfilled fix"],
        })
        stamped = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))
        self.assertEqual(stamped, CliResult(0, "released cargo-mend@1.2.3 (1 notes)\n", ""))
        self.assertEqual(self.releases("backfilled fix"), ["cargo-mend@1.2.3"])
        self.assertEqual(self.cli([
            "changelog", "--crate", "cargo-mend", "--version", "Unreleased",
        ]), CliResult(0, "## [Unreleased]\n", ""))
        self.assertEqual(self.cli([
            "changelog", "--crate", "cargo-mend", "--version", "1.2.3",
        ]), CliResult(0, section.replace("[Unreleased]", "[1.2.3]"), ""))
        self.assert_no_temporary_files()

    def test_compare_notes_are_reported_after_match_without_a_difference(self) -> None:
        self.add_crate_note("matching result", "Use the result.")
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Added\n\n- Use the result.\n\n"
            + "### Notes\n\n- Installing the shim is explicit.\n"
            + "- Installing the shim is explicit.\n"
            + "- The shim retains\n  its wrapped explanation.\n",
            encoding="utf-8",
        )
        before_vault = self.vault_snapshot()
        before_events = (self.state / "events.jsonl").read_bytes()

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(0, "match\nnot compared: Notes (3 bullets)\n", ""))
        self.assertEqual(self.vault_snapshot(), before_vault)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before_events)

    def test_compare_one_notes_bullet_uses_singular(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Notes\n\n- A hand-written explanation.\n",
            encoding="utf-8",
        )

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(0, "match\nnot compared: Notes (1 bullet)\n", ""))

    def test_compare_reports_other_named_headings_in_sorted_order(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Notes\n\n- One explanation.\n- Another explanation.\n\n"
            + "### Acknowledgments\n\n- Thanks to the release reviewer.\n\n"
            + "### Empty heading\n\n## [1.2.3]\n\n### Historical notes\n\n- An old explanation.\n",
            encoding="utf-8",
        )

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(
            0, "match\nnot compared: Acknowledgments (1 bullet), Notes (2 bullets)\n", "",
        ))

    def test_compare_notes_follow_differences_and_do_not_supply_a_change_bullet(self) -> None:
        self.add_crate_note("missing result", "Use the result.")
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Added\n\n- An extra change.\n\n"
            + "### Notes\n\n- Use the result.\n",
            encoding="utf-8",
        )

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(
            1, "### Added\nOnly in feature log:\n- Use the result.\n"
            + "Only in changelog:\n- An extra change.\nnot compared: Notes (1 bullet)\n", "",
        ))

    def test_compare_with_notes_saves_matching_titles_and_removes_a_stale_save_on_difference(self) -> None:
        self.add_crate_note("matching result", "Use the result.")
        changelog = self.root / "CHANGELOG.md"
        contents = self.state / "release" / "cargo-mend.json"
        notes = "\n### Notes\n\n- A hand-written explanation.\n"
        _ = changelog.write_text(
            "## [Unreleased]\n\n### Added\n\n- Use the result.\n" + notes,
            encoding="utf-8",
        )
        arguments = self.compare_arguments("cargo-mend", changelog, contents=contents)

        matched = self.cli(arguments)

        self.assertEqual(matched, CliResult(0, "match\nnot compared: Notes (1 bullet)\n", ""))
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["matching result"],
        })
        _ = changelog.write_text("## [Unreleased]\n" + notes, encoding="utf-8")

        different = self.cli(arguments)

        self.assertEqual(different, CliResult(
            1, "### Added\nOnly in feature log:\n- Use the result.\n"
            + "not compared: Notes (1 bullet)\n", "",
        ))
        self.assertFalse(contents.exists())
        self.assert_no_temporary_files()

    def test_compare_unheaded_bullet_stays_an_uncategorized_difference(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        _ = changelog.write_text(
            "## [Unreleased]\n\n- A change without a heading.\n\n"
            + "### Notes\n\n- A hand-written explanation.\n",
            encoding="utf-8",
        )

        result = self.cli(self.compare_arguments("cargo-mend", changelog))

        self.assertEqual(result, CliResult(
            1, "Uncategorized\nOnly in changelog:\n- A change without a heading.\n"
            + "not compared: Notes (1 bullet)\n", "",
        ))

    def test_selection_contains_rendered_section_and_only_selected_titles(self) -> None:
        self.add_crate_note("old result", "Use the old result.")
        self.supersede_crate_note("old result", "new result", "Use the new result.")
        self.add_crate_note("other crate", "Use another crate.", crates=("cargo-port",))

        result = self.cli([
            "changelog", "--crate", "cargo-mend", "--version", "Unreleased", "--selection",
        ])

        self.assertEqual(result.status, 0, result.stderr)
        selection = cast(ChangelogSelectionJson, json.loads(result.stdout))
        self.assertEqual(selection, {
            "section": "## [Unreleased]\n\n### Added\n\n- Use the new result.\n",
            "titles": ["new result"],
        })

    def test_release_stamps_reviewed_contents_and_repeat_is_unchanged(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.", kind="fix")
        path = self.note("cargo-liner", "reviewed result")
        original = feature_log.parse_note(path)
        self.assertEqual(original["release"], ["cargo-mend@Unreleased"])
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Fixed\n\n- Use the reviewed result.\n",
        )
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["reviewed result"],
        })
        arguments = self.release_arguments("cargo-mend", "1.2.3", contents)

        result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@1.2.3"])
        expected = original.copy()
        expected["release"] = ["cargo-mend@1.2.3"]
        self.assertEqual(feature_log.parse_note(path), expected)
        self.assertEqual(self.release_events(), [{
            "at": EVENT_TIME, "verb": "release", "project": "cargo-liner",
            "crate": "cargo-mend", "version": "1.2.3", "titles": ["reviewed result"],
        }])
        before = self.vault_snapshot()
        event_bytes = (self.state / "events.jsonl").read_bytes()
        repeated = self.cli(arguments)
        self.assertEqual(repeated.status, 0, repeated.stderr)
        self.assertIn("unchanged", repeated.stdout)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), event_bytes)
        self.assert_no_temporary_files()

    def test_release_preserves_an_exact_release_of_another_crate_on_shared_note(self) -> None:
        self.add_crate_note(
            "shared result", "Use the shared result.", crates=("cargo-mend", "cargo-port"),
            releases=("cargo-mend@Unreleased", "cargo-port@0.9.0"), backfill=True,
        )
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the shared result.\n",
        )

        result = self.cli(self.release_arguments("cargo-mend", "1.0.0", contents))

        self.assertEqual(result.status, 0, result.stderr)
        self.assertCountEqual(self.releases("shared result"), [
            "cargo-mend@1.0.0", "cargo-port@0.9.0",
        ])

    def test_note_added_after_snapshot_keeps_unreleased_on_stamp_and_retry(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        self.add_crate_note("later result", "Use the later result.")
        arguments = self.release_arguments("cargo-mend", "1.2.3", contents)

        result = self.cli(arguments)

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@1.2.3"])
        self.assertEqual(self.releases("later result"), ["cargo-mend@Unreleased"])
        self.add_crate_note("retry time result", "Use new work during a publish retry.")
        before = self.vault_snapshot()
        events_before = self.events()
        retry = self.cli(arguments)
        self.assertEqual(retry.status, 0, retry.stderr)
        self.assertIn("unchanged", retry.stdout)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.events(), events_before)
        self.assertEqual(self.releases("retry time result"), ["cargo-mend@Unreleased"])

    def test_note_superseded_after_snapshot_is_stamped(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        self.supersede_crate_note("reviewed result", "replacement result", "Use the replacement result.")

        result = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@1.2.3"])
        self.assertEqual(self.releases("replacement result"), ["cargo-mend@Unreleased"])
        released = self.cli(["changelog", "--crate", "cargo-mend", "--version", "1.2.3"])
        self.assertEqual(released, CliResult(
            0, "## [1.2.3]\n\n### Added\n\n- Use the reviewed result.\n", "",
        ))

    def test_release_retry_leaves_later_superseded_pending_work_unchanged(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        arguments = self.release_arguments("cargo-mend", "1.2.3", contents)
        stamped = self.cli(arguments)
        self.assertEqual(stamped.status, 0, stamped.stderr)
        self.add_crate_note("next result", "Use new work after publication.")
        self.supersede_crate_note("next result", "next replacement", "Use its newer replacement.")
        before = self.vault_snapshot()
        events = self.events()

        repeated = self.cli(arguments)

        self.assertEqual(repeated.status, 0, repeated.stderr)
        self.assertIn("unchanged", repeated.stdout)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.events(), events)
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@1.2.3"])
        self.assertEqual(self.releases("next result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.releases("next replacement"), ["cargo-mend@Unreleased"])

    def test_empty_snapshot_release_records_one_event_and_repeat_preserves_new_work(self) -> None:
        contents = self.save_match("cargo-mend", "## [Unreleased]\n")
        arguments = self.release_arguments("cargo-mend", "1.2.3", contents)

        stamped = self.cli(arguments)

        self.assertEqual(stamped.status, 0, stamped.stderr)
        self.assertEqual(self.release_events(), [{
            "at": EVENT_TIME, "verb": "release", "project": "cargo-liner",
            "crate": "cargo-mend", "version": "1.2.3", "titles": [],
        }])
        self.add_crate_note("new pending result", "Use work added after the empty release.")
        before = self.vault_snapshot()
        events = self.events()
        repeated = self.cli(arguments)
        self.assertEqual(repeated.status, 0, repeated.stderr)
        self.assertIn("unchanged", repeated.stdout)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.events(), events)
        self.assertEqual(self.releases("new pending result"), ["cargo-mend@Unreleased"])
        self.assertEqual(len(self.release_events()), 1)

    def test_note_added_and_superseded_after_comparison_loses_pending_at_first_stamp(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        self.add_crate_note("new pending result", "Use work added after comparison.")
        self.supersede_crate_note(
            "new pending result", "new pending replacement", "Use the replacement added after comparison.",
        )

        stamped = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))

        self.assertEqual(stamped.status, 0, stamped.stderr)
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@1.2.3"])
        self.assertEqual(self.releases("new pending result"), [])
        self.assertEqual(self.releases("new pending replacement"), ["cargo-mend@Unreleased"])
        events = self.release_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["titles"], ["new pending result", "reviewed result"])
        unreleased = self.cli(["changelog", "--crate", "cargo-mend", "--version", "Unreleased"])
        self.assertEqual(unreleased, CliResult(
            0, "## [Unreleased]\n\n### Added\n\n- Use the replacement added after comparison.\n", "",
        ))
        released = self.cli(["changelog", "--crate", "cargo-mend", "--version", "1.2.3"])
        self.assertEqual(released, CliResult(
            0, "## [1.2.3]\n\n### Added\n\n- Use the reviewed result.\n", "",
        ))

    def test_precomparison_superseded_note_is_absent_and_loses_unreleased(self) -> None:
        self.add_crate_note(
            "old result", "Use the old result.", crates=("cargo-mend", "cargo-port", "tui_pane"),
            releases=("cargo-mend@Unreleased", "cargo-port@0.9.0", "tui_pane@Unreleased"),
            backfill=True,
        )
        self.supersede_crate_note("old result", "current result", "Use the current result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the current result.\n",
        )
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["current result"],
        })
        selection = self.cli([
            "changelog", "--crate", "cargo-mend", "--version", "Unreleased", "--selection",
        ])
        self.assertEqual(selection.status, 0, selection.stderr)
        selected = cast(ChangelogSelectionJson, json.loads(selection.stdout))
        self.assertEqual(selected["titles"], ["current result"])

        result = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))

        self.assertEqual(result.status, 0, result.stderr)
        self.assertCountEqual(self.releases("old result"), ["cargo-port@0.9.0", "tui_pane@Unreleased"])
        self.assertEqual(self.releases("current result"), ["cargo-mend@1.2.3"])
        self.assertEqual(len(self.release_events()), 1)
        self.assertCountEqual(self.release_events()[0]["titles"], ["old result", "current result"])

    def test_mixed_workspace_stamps_only_crate_with_matching_saved_contents(self) -> None:
        self.add_crate_note("shared result", "Use the shared result.", crates=("cargo-mend", "cargo-port"))
        section = "## [Unreleased]\n\n### Added\n\n- Use the shared result.\n"
        mend_contents = self.save_match("cargo-mend", section)
        port_changelog = self.root / "cargo-port-CHANGELOG.md"
        port_contents = self.state / "release/cargo-port.json"
        _ = port_changelog.write_text("## [Unreleased]\n", encoding="utf-8")
        mismatch = self.cli(self.compare_arguments("cargo-port", port_changelog, contents=port_contents))
        self.assertEqual(mismatch.status, 1, mismatch.stderr)
        self.assertFalse(port_contents.exists())

        stamped = self.cli(self.release_arguments("cargo-mend", "1.2.3", mend_contents))

        self.assertEqual(stamped.status, 0, stamped.stderr)
        self.assertCountEqual(self.releases("shared result"), ["cargo-mend@1.2.3", "cargo-port@Unreleased"])
        self.assertEqual([event["crate"] for event in self.release_events()], ["cargo-mend"])

    def test_partial_publish_keeps_earlier_stamp_and_retry_stamps_remaining_crate(self) -> None:
        self.add_crate_note("shared result", "Use the shared result.", crates=("cargo-mend", "cargo-port"))
        section = "## [Unreleased]\n\n### Added\n\n- Use the shared result.\n"
        mend_contents = self.save_match("cargo-mend", section)
        port_contents = self.save_match("cargo-port", section)
        mend_arguments = self.release_arguments("cargo-mend", "1.2.3", mend_contents)
        first_publish = self.cli(mend_arguments)
        self.assertEqual(first_publish.status, 0, first_publish.stderr)
        self.assertCountEqual(self.releases("shared result"), ["cargo-mend@1.2.3", "cargo-port@Unreleased"])
        self.assertEqual([event["crate"] for event in self.release_events()], ["cargo-mend"])

        # The port publish fails before invoking release. New work must survive its retry.
        self.add_crate_note("next shared result", "Use the next result.", crates=("cargo-mend", "cargo-port"))
        retry = self.cli(mend_arguments)
        self.assertEqual(retry.status, 0, retry.stderr)
        self.assertIn("unchanged", retry.stdout)
        port_publish = self.cli(self.release_arguments("cargo-port", "2.3.4", port_contents))

        self.assertEqual(port_publish.status, 0, port_publish.stderr)
        self.assertCountEqual(self.releases("shared result"), ["cargo-mend@1.2.3", "cargo-port@2.3.4"])
        self.assertCountEqual(self.releases("next shared result"), ["cargo-mend@Unreleased", "cargo-port@Unreleased"])
        self.assertEqual([event["crate"] for event in self.release_events()], ["cargo-mend", "cargo-port"])

    def test_differing_compare_removes_previous_match_file_and_cannot_stamp(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        self.add_crate_note("new result", "Use a newly added result.")
        changelog = self.root / "cargo-mend-CHANGELOG.md"

        mismatch = self.cli(self.compare_arguments("cargo-mend", changelog, contents=contents))

        self.assertEqual(mismatch.status, 1, mismatch.stderr)
        self.assertFalse(contents.exists())
        before = self.vault_snapshot()
        rejected = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))
        self.assertEqual(rejected.status, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.release_events(), [])

    def test_failed_local_compare_removes_previous_saved_contents(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        result = self.cli(self.compare_arguments("cargo-mend", self.root / "missing.md", contents=contents))
        self.assertEqual(result.status, 2)
        self.assertFalse(contents.exists())

    def test_release_rejects_wrong_crate_and_invalid_versions_without_mutation(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        for crate, version in (
            ("cargo-port", "1.2.3"), ("cargo-berth", "1.2.3"),
            ("cargo-mend", "Unreleased"), ("cargo-mend", "1.2"),
            ("cargo-mend", "1.2.3-alpha..1"),
        ):
            with self.subTest(crate=crate, version=version):
                before = self.vault_snapshot()
                events = self.events()
                result = self.cli(self.release_arguments(crate, version, contents))
                self.assertEqual(result.status, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.vault_snapshot(), before)
                self.assertEqual(self.events(), events)

    def test_release_rejects_malformed_snapshot_without_stamps_or_pending_cleanup(self) -> None:
        self.add_crate_note("old result", "Use the old result.")
        self.supersede_crate_note("old result", "reviewed result", "Use the reviewed result.")
        contents = self.root / "invalid-local-contents.json"
        invalid_files = (
            "not JSON", "[]", "{}",
            '{"crate": "cargo-port", "titles": ["reviewed result"]}',
            '{"crate": "cargo-mend", "titles": "reviewed result"}',
            '{"crate": "cargo-mend", "titles": [1]}',
            '{"crate": "cargo-mend", "titles": ["../escape"]}',
        )
        for raw in invalid_files:
            with self.subTest(raw=raw):
                _ = contents.write_text(raw, encoding="utf-8")
                before = self.vault_snapshot()
                events = self.events()
                result = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))
                self.assertEqual(result.status, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
                self.assertEqual(self.vault_snapshot(), before)
                self.assertEqual(self.events(), events)
                self.assertEqual(contents.read_text(encoding="utf-8"), raw)
                self.assert_no_temporary_files()
        self.assertEqual(self.releases("old result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.release_events(), [])

    def test_release_rejects_unsorted_titles_without_stamps_or_pending_cleanup(self) -> None:
        self.add_crate_note("old result", "Use the old result.")
        self.supersede_crate_note("old result", "reviewed result", "Use the reviewed result.")
        contents = self.root / "unsorted-local-contents.json"
        raw = '{"crate": "cargo-mend", "titles": ["reviewed result", "old result"]}'
        _ = contents.write_text(raw, encoding="utf-8")
        before = self.vault_snapshot()
        events = self.events()

        result = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))

        self.assertEqual(result.status, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("reviewed titles must be sorted with no duplicates", result.stderr)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.events(), events)
        self.assertEqual(contents.read_text(encoding="utf-8"), raw)
        self.assertEqual(self.releases("old result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.release_events(), [])
        self.assert_no_temporary_files()

    def test_release_rejects_missing_reviewed_note_without_stamps_or_pending_cleanup(self) -> None:
        self.add_crate_note("old result", "Use the old result.")
        self.supersede_crate_note("old result", "reviewed result", "Use the reviewed result.")
        contents = self.root / "missing-note-contents.json"
        raw = '{"crate": "cargo-mend", "titles": ["reviewed result", "unknown result"]}'
        _ = contents.write_text(raw, encoding="utf-8")
        before = self.vault_snapshot()
        events = self.events()

        result = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))

        self.assertEqual(result.status, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("reviewed note unknown result does not exist", result.stderr)
        self.assertEqual(self.vault_snapshot(), before)
        self.assertEqual(self.events(), events)
        self.assertEqual(contents.read_text(encoding="utf-8"), raw)
        self.assertEqual(self.releases("old result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.releases("reviewed result"), ["cargo-mend@Unreleased"])
        self.assertEqual(self.release_events(), [])
        self.assert_no_temporary_files()

    def test_digest_shows_release_event_and_advances_past_it(self) -> None:
        self.add_crate_note("reviewed result", "Use the reviewed result.")
        contents = self.save_match(
            "cargo-mend", "## [Unreleased]\n\n### Added\n\n- Use the reviewed result.\n",
        )
        first_digest = self.cli(["digest"])
        self.assertEqual(first_digest.status, 0, first_digest.stderr)
        stamped = self.cli(self.release_arguments("cargo-mend", "1.2.3", contents))
        self.assertEqual(stamped.status, 0, stamped.stderr)

        result = self.cli(["digest"])

        self.assertEqual(result, CliResult(
            0, "cargo-liner:\n= cargo-mend@1.2.3 (1 notes)\n"
            + "1 changes since 2026-10-10 12:34 PDT\n", "",
        ))
        self.assertEqual(self.digest_cursor()["line"], len(self.events()))
        repeated = self.cli(["digest"])
        self.assertEqual(repeated.status, 0, repeated.stderr)
        self.assertNotIn("= cargo-mend", repeated.stdout)

    def test_off_host_compare_forwards_one_selection_and_compares_local_path(self) -> None:
        section = "## [Unreleased]\n\n### Added\n\n- Use the remote result.\n"
        reader = RecordingForwardedReader(feature_log.ChangelogSelection(section, ["remote result"]))
        forwarder = RecordingReleaseForwarder()
        changelog = self.root / "caller changelog.md"
        _ = changelog.write_text(section, encoding="utf-8")

        result = self.off_host_cli(self.compare_arguments("cargo-mend", changelog), reader, forwarder)

        self.assertEqual(result, CliResult(0, "match\n", ""))
        self.assertEqual(len(reader.commands), 1)
        self.assertEqual(self.remote_arguments(reader.commands[0]), [
            "changelog", "--crate", "cargo-mend", "--version", "Unreleased", "--selection",
        ])
        self.assertNotIn(str(changelog), reader.commands[0][2])
        self.assertEqual(forwarder.calls, [])
        self.assertFalse(self.vault.exists())

    def test_off_host_compare_reports_notes_after_match_and_differences(self) -> None:
        section = "## [Unreleased]\n\n### Added\n\n- Use the remote result.\n"
        reader = RecordingForwardedReader(feature_log.ChangelogSelection(section, ["remote result"]))
        forwarder = RecordingReleaseForwarder()
        changelog = self.root / "caller changelog.md"
        contents = self.root / "caller-only" / "reviewed.json"
        notes = "\n### Notes\n\n- A caller-only explanation.\n"
        _ = changelog.write_text(section + notes, encoding="utf-8")
        arguments = self.compare_arguments("cargo-mend", changelog, contents=contents)

        matched = self.off_host_cli(arguments, reader, forwarder)

        self.assertEqual(matched, CliResult(0, "match\nnot compared: Notes (1 bullet)\n", ""))
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["remote result"],
        })
        _ = changelog.write_text("## [Unreleased]\n" + notes, encoding="utf-8")

        different = self.off_host_cli(arguments, reader, forwarder)

        self.assertEqual(different, CliResult(
            1, "### Added\nOnly in feature log:\n- Use the remote result.\n"
            + "not compared: Notes (1 bullet)\n", "",
        ))
        self.assertFalse(contents.exists())
        self.assertEqual(len(reader.commands), 2)
        for command in reader.commands:
            self.assertEqual(self.remote_arguments(command), [
                "changelog", "--crate", "cargo-mend", "--version", "Unreleased", "--selection",
            ])
            self.assertNotIn(str(changelog), command[2])
        self.assertEqual(forwarder.calls, [])
        self.assertFalse(self.vault.exists())
        self.assert_no_temporary_files()

    def test_off_host_compare_difference_removes_caller_saved_contents(self) -> None:
        section = "## [Unreleased]\n\n### Fixed\n\n- Repair the remote result.\n"
        reader = RecordingForwardedReader(feature_log.ChangelogSelection(section, ["remote fix"]))
        forwarder = RecordingReleaseForwarder()
        changelog = self.root / "caller CHANGELOG.md"
        _ = changelog.write_text(section, encoding="utf-8")
        contents = self.root / "caller-only" / "reviewed.json"
        arguments = self.compare_arguments("cargo-mend", changelog, contents=contents)
        match = self.off_host_cli(arguments, reader, forwarder)
        self.assertEqual(match, CliResult(0, "match\n", ""))
        self.assertEqual(self.reviewed_contents(contents), {"crate": "cargo-mend", "titles": ["remote fix"]})
        _ = changelog.write_text("## [Unreleased]\n", encoding="utf-8")

        mismatch = self.off_host_cli(arguments, reader, forwarder)

        self.assertEqual(mismatch.status, 1, mismatch.stderr)
        self.assertIn("Repair the remote result.", mismatch.stdout)
        self.assertFalse(contents.exists())
        self.assertEqual(len(reader.commands), 2)
        self.assertEqual(forwarder.calls, [])
        self.assertFalse(self.vault.exists())

    def test_failed_forwarded_read_exits_two_with_remote_error_and_no_saved_file(self) -> None:
        reader = RecordingForwardedReader(feature_log.ForwardFailed(7, "remote vault unavailable\n"))
        forwarder = RecordingReleaseForwarder()
        changelog = self.root / "caller CHANGELOG.md"
        _ = changelog.write_text("## [Unreleased]\n", encoding="utf-8")
        contents = self.root / "previous-match.json"
        _ = contents.write_text('{"crate": "cargo-mend", "titles": ["stale result"]}', encoding="utf-8")

        result = self.off_host_cli(
            self.compare_arguments("cargo-mend", changelog, contents=contents), reader, forwarder,
        )

        self.assertEqual(result.status, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("remote vault unavailable", result.stderr)
        self.assertFalse(contents.exists())
        self.assertEqual(len(reader.commands), 1)
        self.assertEqual(forwarder.calls, [])

    def test_off_host_release_validates_contents_before_forwarding(self) -> None:
        reader = RecordingForwardedReader(feature_log.ForwardFailed(2, "unexpected reader call"))
        forwarder = RecordingReleaseForwarder()
        contents = self.root / "invalid-contents.json"
        invalid_files = (
            "not JSON", "[]", "{}",
            '{"crate": "cargo-port", "titles": []}',
            '{"crate": "cargo-mend", "titles": "a title"}',
            '{"crate": "cargo-mend", "titles": [1]}',
            '{"crate": "cargo-mend", "titles": ["../escape"]}',
        )
        for raw in invalid_files:
            with self.subTest(raw=raw):
                _ = contents.write_text(raw, encoding="utf-8")
                result = self.off_host_cli(
                    self.release_arguments("cargo-mend", "1.2.3", contents), reader, forwarder,
                )
                self.assertEqual(result.status, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
        contents.unlink()
        missing = self.off_host_cli(self.release_arguments("cargo-mend", "1.2.3", contents), reader, forwarder)
        self.assertEqual(missing.status, 2)
        self.assertEqual(reader.commands, [])
        self.assertEqual(forwarder.calls, [])

    def test_off_host_release_sends_validated_json_instead_of_caller_path(self) -> None:
        reader = RecordingForwardedReader(feature_log.ForwardFailed(2, "unexpected reader call"))
        forwarder = RecordingReleaseForwarder()
        contents = self.root / "caller-only.json"
        _ = contents.write_text(
            '{"crate": "cargo-mend", "titles": ["reviewed result"]}', encoding="utf-8",
        )

        result = self.off_host_cli(self.release_arguments("cargo-mend", "1.2.3", contents), reader, forwarder)

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(reader.commands, [])
        self.assertEqual(len(forwarder.calls), 1)
        call = forwarder.calls[0]
        self.assertEqual(call.contents, {"crate": "cargo-mend", "titles": ["reviewed result"]})
        remote = self.remote_arguments(call.command)
        self.assertEqual(remote[:5], ["release", "--crate", "cargo-mend", "--version", "1.2.3"])
        self.assertNotIn("--contents", remote)
        self.assertNotIn(str(contents), call.command[2])
        self.assertFalse(self.vault.exists())

    def test_off_host_compare_save_release_uses_two_separate_filesystems(self) -> None:
        remote_vault = self.root / "vault-host" / "vault"
        remote_state_home = self.root / "vault-host" / "state"
        remote_environment = {
            "FEATURE_LOG_VAULT": str(remote_vault), "XDG_STATE_HOME": str(remote_state_home),
        }
        with patch.dict(os.environ, remote_environment):
            self.add_crate_note("remote reviewed result", "Use the remote reviewed result.")

        section = "## [Unreleased]\n\n### Added\n\n- Use the remote reviewed result.\n"
        changelog = self.root / "calling-host" / "CHANGELOG.md"
        changelog.parent.mkdir(parents=True)
        _ = changelog.write_text(section, encoding="utf-8")
        contents = self.root / "calling-host" / "release" / "cargo-mend.json"
        read_commands: list[list[str]] = []
        release_calls: list[ForwardedReleaseCall] = []

        def read_remote(command: list[str]) -> feature_log.ChangelogSelection | feature_log.ForwardFailed:
            read_commands.append(command.copy())
            arguments = self.remote_arguments(command)
            self.assertEqual(arguments, [
                "changelog", "--crate", "cargo-mend", "--version", "Unreleased", "--selection",
            ])
            self.assertNotIn(str(changelog), command[2])
            self.assertNotIn(str(contents), command[2])
            with (
                patch.dict(os.environ, remote_environment),
                patch.object(feature_log, "vault_dir", return_value=remote_vault),
            ):
                selected = self.cli(arguments)
            self.assertEqual(selected.status, 0, selected.stderr)
            value = cast(ChangelogSelectionJson, json.loads(selected.stdout))
            return feature_log.ChangelogSelection(value["section"], value["titles"])

        def release_remote(command: list[str], reviewed: feature_log.ReviewedReleaseContents) -> int:
            release_calls.append(ForwardedReleaseCall(command.copy(), {
                "crate": reviewed["crate"], "titles": reviewed["titles"].copy(),
            }))
            self.assertTrue(contents.is_file())
            self.assertEqual(list((self.root / "vault-host").rglob("cargo-mend.json")), [])
            arguments = self.remote_arguments(command)
            self.assertNotIn("--contents", arguments)
            self.assertNotIn(str(contents), command[2])
            with (
                patch.dict(os.environ, remote_environment),
                patch.object(feature_log, "vault_dir", return_value=remote_vault),
                patch.object(sys, "stdin", StringIO(json.dumps(reviewed))),
            ):
                return feature_log.main(arguments)

        compared = self.off_host_cli(
            self.compare_arguments("cargo-mend", changelog, contents=contents), read_remote, release_remote,
        )
        self.assertEqual(compared, CliResult(0, "match\n", ""))
        self.assertEqual(self.reviewed_contents(contents), {
            "crate": "cargo-mend", "titles": ["remote reviewed result"],
        })

        stamped = self.off_host_cli(
            self.release_arguments("cargo-mend", "1.2.3", contents), read_remote, release_remote,
        )

        self.assertEqual(stamped.status, 0, stamped.stderr)
        self.assertEqual(len(read_commands), 1)
        self.assertEqual(len(release_calls), 1)
        self.assertFalse(self.vault.exists())
        remote_note = remote_vault / "feature-log/cargo-liner/remote reviewed result.md"
        self.assertEqual(feature_log.parse_note(remote_note)["release"], ["cargo-mend@1.2.3"])
        remote_events = remote_state_home / "claude/feature-log/events.jsonl"
        events = [cast(FeatureLogEvent, json.loads(line)) for line in remote_events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1], {
            "at": EVENT_TIME, "verb": "release", "project": "cargo-liner", "crate": "cargo-mend",
            "version": "1.2.3", "titles": ["remote reviewed result"],
        })

    def test_off_host_release_transport_writes_json_to_stdin(self) -> None:
        contents = self.root / "caller-release.json"
        _ = contents.write_text(
            '{"crate": "cargo-mend", "titles": ["reviewed result"]}', encoding="utf-8",
        )
        commands: list[list[str]] = []
        payloads: list[ReviewedReleaseContents] = []

        def remote_process(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            commands.append(command.copy())
            raw = options.get("input")
            if not isinstance(raw, str):
                self.fail("release transport must send JSON on stdin")
            payloads.append(cast(ReviewedReleaseContents, json.loads(raw)))
            self.assertIs(options.get("text"), True)
            return subprocess.CompletedProcess[str](command, 0)

        ssh_runner = RecordingSshRunner(returncode=99)
        with patch.dict(os.environ, {}, clear=False):
            _ = os.environ.pop("FEATURE_LOG_VAULT")
            with (
                patch.object(socket, "gethostname", return_value="macbook.local"),
                patch.object(feature_log, "vault_dir", return_value=self.vault),
                patch.object(subprocess, "run", side_effect=remote_process),
            ):
                result = self.cli_with_runner(self.release_arguments("cargo-mend", "1.2.3", contents), ssh_runner)

        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(ssh_runner.commands, [])
        self.assertEqual(len(commands), 1)
        self.assertEqual(payloads, [{"crate": "cargo-mend", "titles": ["reviewed result"]}])
        self.assertNotIn("--contents", self.remote_arguments(commands[0]))
        self.assertNotIn(str(contents), commands[0][2])


if __name__ == "__main__":
    _ = unittest.main()
