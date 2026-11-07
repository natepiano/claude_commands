#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Prepare immutable style inputs and capture a read-only reviewer's result."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Literal, cast, final

from scripts.lib import build_folders
from scripts.lib.atomic_file import write_text_atomic
from scripts.agents import worker_lifecycle
from scripts.message import sessions

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
BEGIN = "<!-- phase-style-review:start -->"
END = "<!-- phase-style-review:end -->"


@dataclass(frozen=True)
class WorkingTree:
    """Include staged, unstaged and untracked work at preparation time."""


@dataclass(frozen=True)
class CommitEnd:
    reference: str


ReviewEnd = WorkingTree | CommitEnd
WORKING_TREE = WorkingTree()


@dataclass(frozen=True)
class ReviewInputs:
    baseline: str
    end: ReviewEnd
    diff: Path
    additions: Path
    paths: tuple[str, ...]
    rust_changed: bool


@dataclass(frozen=True)
class Ready:
    inputs: ReviewInputs


@dataclass(frozen=True)
class Finished:
    result: Literal["Off", "No changes to review"]


@dataclass(frozen=True)
class PreparationError:
    cause: str


Preparation = Ready | Finished | PreparationError


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)


def require_success(result: subprocess.CompletedProcess[bytes], operation: str) -> bytes:
    if result.returncode:
        raise ValueError(f"{operation}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def validate_output(root: Path, output: Path) -> None:
    repository = Path(require_success(git(root, "rev-parse", "--show-toplevel"),
                                      "repository root").decode().strip()).resolve()
    if output.resolve().is_relative_to(repository):
        raise ValueError(f"style output must be outside the reviewed repository: {output}")


class StyleReviewBusy(ValueError):
    """A live or unconfirmed launcher still owns the phase's style assignment."""


def prepare(root: Path, baseline: str, directory: Path, end: ReviewEnd = WORKING_TREE) -> Preparation:
    """Reject self-input, then read the switch before validating the review range."""
    try:
        validate_output(root, directory)
        switches = subprocess.run(["bash", str(ROOT / "scripts/lint/lint_config.sh"), "export"],
                                  capture_output=True, text=True)
        if switches.returncode:
            raise ValueError(f"lint switches: {switches.stderr.strip()}")
        values = dict(line.split("=", 1) for line in switches.stdout.splitlines() if "=" in line)
        if values.get("LINT_OP_STYLE_REVIEW") == "off":
            return Finished("Off")
        if values.get("LINT_OP_STYLE_REVIEW") != "on":
            raise ValueError("lint switches: missing LINT_OP_STYLE_REVIEW")
        commit = require_success(git(root, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}"),
                                 f"invalid baseline {baseline}").decode().strip()
        endpoints = [commit]
        if isinstance(end, CommitEnd):
            end = CommitEnd(require_success(git(root, "rev-parse", "--verify", "--end-of-options",
                                               end.reference + "^{commit}"),
                                           f"invalid end commit {end.reference}").decode().strip())
            endpoints.append(end.reference)
        diff = require_success(git(root, "diff", "--no-ext-diff", "--no-textconv", *endpoints, "--"),
                               f"tracked diff failed for {baseline}")
        names = require_success(git(root, "diff", "--name-only", "-z", *endpoints, "--"), "changed paths")
        untracked = (require_success(git(root, "ls-files", "--others", "--exclude-standard", "-z"),
                                     "untracked paths") if isinstance(end, WorkingTree) else b"")
        paths = tuple(dict.fromkeys(name.decode(errors="surrogateescape") for name in
                                   (names + untracked).split(b"\0") if name))
        for name in untracked.split(b"\0"):
            if not name:
                continue
            result = git(root, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", "/dev/null",
                         name.decode(errors="surrogateescape"))
            if result.returncode not in (0, 1):
                raise ValueError(f"untracked diff failed: {result.stderr.decode(errors='replace')}")
            diff += result.stdout
        content = diff.decode(errors="replace")
        additions = "".join(line for line in content.splitlines(keepends=True)
                            if line.startswith("+") and not line.startswith("+++"))
        directory.mkdir(parents=True, exist_ok=True)
        diff_path, additions_path = directory / "style-review.diff", directory / "style-review-additions.txt"
        write_text_atomic(diff_path, content)
        write_text_atomic(additions_path, additions)
        if not diff:
            return Finished("No changes to review")
        rust = any(Path(name).suffix == ".rs" or Path(name).name in ("Cargo.toml", "Cargo.lock") for name in paths)
        return Ready(ReviewInputs(commit, end, diff_path, additions_path, paths, rust))
    except (OSError, ValueError) as error:
        return PreparationError(str(error))


def publish(directory: Path, state: Literal["owed", "complete"], report: str, *, in_flight: bool = False) -> None:
    """Publish pass and phase evidence while the caller holds the phase state lock."""
    write_text_atomic(directory / "report.md", report)
    write_text_atomic(directory.parent.parent / "phase_style_review.md", report)
    write_text_atomic(directory.parent.parent / "style_review_state.json", json.dumps({
        "state": state, "input_directory": str(directory),
        "report": str(directory.parent.parent / "phase_style_review.md"),
        "baseline": (directory / "baseline").read_text(),
        "until": (directory / "until").read_text(),
        "in_flight": in_flight,
        "owner": json.loads((directory / "owner.json").read_text()),
    }, indent=2) + "\n")


def start(root: Path, baseline: str, phase_dir: Path, prompt: Path, pass_name: str,
          end: ReviewEnd = WORKING_TREE, *, owner_pid: int = 0) -> Path:
    """Claim and prepare under the same phase lock used to capture the result."""
    validate_output(root, phase_dir)
    pid = owner_pid or os.getpid()
    started = sessions.process_start(pid)
    if not started:
        raise ValueError(f"cannot identify style launcher {pid}")
    with worker_lifecycle.locked_state(phase_dir.resolve() / "style_review_state.json") as saved:
        state = saved.read_object()
        if state.get("in_flight"):
            owner = state.get("owner")
            if not isinstance(owner, dict):
                raise StyleReviewBusy("style review in flight with unreadable owner; inspect the phase state")
            owner = cast(dict[str, object], owner)
            previous_pid, previous_start = owner.get("pid"), owner.get("started")
            if not isinstance(previous_pid, int) or not isinstance(previous_start, str):
                raise StyleReviewBusy("style review in flight with unreadable owner; inspect the phase state")
            if not isinstance(sessions.process_liveness(previous_pid, previous_start), sessions.SessionNotLive):
                raise StyleReviewBusy(f"style review in flight: {owner.get('dispatch')} " +
                                      f"(launcher {previous_pid}); finish or cancel that dispatch first")
        parent = phase_dir.resolve() / "style"
        parent.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", pass_name) or "pass"
        directory = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
            build_folders.FOLDER_PREFIXES["style-review"].format(owner=safe_name), build_folders.ExplicitBuildParent(parent)))
        write_text_atomic(directory / "owner.json", json.dumps({
            "pid": pid, "started": started, "dispatch": pass_name,
        }))
        write_text_atomic(directory / "baseline", baseline)
        write_text_atomic(directory / "until", end.reference if isinstance(end, CommitEnd) else "working tree")
        # A stop anywhere after allocation keeps this attempt owed.
        publish(directory, "owed", "Style review: Error\nPreparation or dispatch did not finish.\n", in_flight=True)
        result = prepare(root, baseline, directory, end)
        assignment = ""
        if isinstance(result, Ready):
            inputs = result.inputs
            write_text_atomic(directory / "baseline", inputs.baseline)
            write_text_atomic(directory / "until", inputs.end.reference if isinstance(inputs.end, CommitEnd) else "working tree")
            assignment = Template((ROOT / "config/review/style.md").read_text()).substitute(
                range=f"{inputs.baseline}..{(directory / 'until').read_text()}",
                scripts=str(SCRIPTS), diff=str(inputs.diff), additions=str(inputs.additions),
                rust="yes" if inputs.rust_changed else "no", begin=BEGIN, end=END)
            print(f"Rust change {'yes' if inputs.rust_changed else 'no'}: {', '.join(inputs.paths)}")
            print(f"Diff: {inputs.diff}\nAdditions: {inputs.additions}")
        elif isinstance(result, Finished):
            publish(directory, "complete", f"Style review: {result.result}\nRange: {baseline}..{(directory / 'until').read_text()}\n")
            print(result.result)
        else:
            publish(directory, "owed", f"Style review: Error\n{result.cause}\n")
            print(f"Style review: Error: {result.cause}", file=sys.stderr)
        write_text_atomic(directory / "assignment.md", assignment)
        write_text_atomic(directory / "prompt.md", prompt.read_text() + ("\n" + assignment if assignment else ""))
        print(f"Style report: {phase_dir.resolve() / 'phase_style_review.md'}")
        return directory


def finish(directory: Path, result_file: Path, dispatch_code: int) -> None:
    """Only a successful dispatch with one valid marked result settles an assignment."""
    if not (directory / "assignment.md").read_text():
        return  # Off, empty input, or preparation error was already recorded.
    state_file = directory.parent.parent / "style_review_state.json"
    with worker_lifecycle.locked_state(state_file) as saved:
        state = saved.read_object()
        if state.get("input_directory") != str(directory):
            return  # A later retry owns the phase state; keep its result.
        report = "Style review: Error\nDispatch failed or was canceled.\n"
        complete = False
        if dispatch_code == 0:
            text = result_file.read_text() if result_file.exists() else ""
            if text.count(BEGIN) == 1 and text.count(END) == 1 and text.index(BEGIN) < text.index(END):
                report = text.split(BEGIN, 1)[1].split(END, 1)[0].strip() + "\n"
                outcomes = re.findall(r"^Style review: (.+?)\.?$", report, re.MULTILINE)
                complete = len(outcomes) == 1 and outcomes[0] in ("Pass", "Findings", "Off", "No changes to review")
            else:
                report = "Style review: Error\nMissing or ambiguous report markers.\n"
        publish(directory, "complete" if complete else "owed",
                f"Range: {(directory / 'baseline').read_text()}..{(directory / 'until').read_text()}\n{report}")


@final
class Arguments(argparse.Namespace):
    command: str = ""
    working_dir: str = ""
    since: str = "HEAD"
    until: str = ""
    phase_dir: str = ""
    prompt: str = ""
    pass_name: str = "pass"
    directory_file: str = ""
    input_dir: str = ""
    result: str = ""
    dispatch_code: int = 1
    owner_pid: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("prepare")
    _ = begin.add_argument("--working-dir", default=str(Path.cwd()))
    _ = begin.add_argument("--since", default="HEAD")
    _ = begin.add_argument("--until", default="")
    for name in ("phase-dir", "prompt", "directory-file"):
        _ = begin.add_argument("--" + name, required=True)
    _ = begin.add_argument("--pass-name", default="pass")
    _ = begin.add_argument("--owner-pid", type=int, default=0)
    end = sub.add_parser("finish")
    for name in ("input-dir", "result"):
        _ = end.add_argument("--" + name, required=True)
    _ = end.add_argument("--dispatch-code", type=int, required=True)
    args = parser.parse_args(namespace=Arguments())
    if args.command == "prepare":
        try:
            directory = start(Path(args.working_dir).resolve(), args.since, Path(args.phase_dir),
                              Path(args.prompt), args.pass_name,
                              CommitEnd(args.until) if args.until else WORKING_TREE, owner_pid=args.owner_pid)
        except ValueError as error:
            print(f"style_review: {error}", file=sys.stderr)
            return 2
        write_text_atomic(Path(args.directory_file), str(directory) + "\n")
    else:
        finish(Path(args.input_dir), Path(args.result), args.dispatch_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
