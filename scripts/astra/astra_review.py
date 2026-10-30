#!/usr/bin/env python3
"""astra_review.py — Ask gpt-6-astra for a review of one document and print the review.

Usage: astra_review.py [--run-directory <dir> --phase <id>] <document> [focus...]

The review is one `codex exec` turn on the current workers account, read-only, at reasoning xhigh.
The reviewer's last message is saved in the run folder under /tmp/claude/astra/ and printed when it
finishes. A run that fails prints Codex's error and the account, and exits 1. A missing document
exits 2.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_roles  # noqa: E402
from scripts.astra import astra_work, design_roles  # noqa: E402

MODEL = "gpt-6-astra"
REASONING = "xhigh"
# The reviewer's thread compacts above this many tokens.
COMPACT_ABOVE_TOKENS = 200_000
# Lines of Codex's own output a failure prints.
ERROR_TAIL_LINES = 40
SCRIPTS = Path(__file__).resolve().parents[1]
# Tests point these at a stub and a temporary folder.
ACCOUNTS_COMMAND = os.environ.get("ASTRA_REVIEW_ACCOUNTS") or ""
RUNS_ROOT = Path(os.environ.get("ASTRA_REVIEW_DIR") or "/tmp/claude/astra")
THREAD_HEADER_TIMEOUT = 30.0


def accounts_command() -> list[str]:
    if ACCOUNTS_COMMAND:
        return [ACCOUNTS_COMMAND]
    return [str(SCRIPTS / "lib" / "py"), str(SCRIPTS / "accounts" / "accounts.py")]


def review_prompt(document: Path, focus: str) -> str:
    asked = f"\nThe caller asks you to focus on: {focus}\n" if focus else ""
    return f"""You are reviewing one document for the person or agent who asked: {document}
{asked}
Read the document in full. Read the files it names where they bear on what it claims, and check
those claims against them.

Review it for:
1. Clarity: could a reader who has not seen it before act on it?
2. Correctness: is what it says true of the code, files and facts it names?
3. Gaps: what does the work need that it leaves out?
4. Risks: what could go wrong if it is followed as written?
5. What you would change.

Judge correctness first, then simplicity, then speed, then polish.

Only read. Do not build, run tests, or change, create or delete any file.

Your final message is the review. Start with a short verdict paragraph. Then list findings, most
important first. Each finding names where it is (the section heading, or a file path with a line
number), what is wrong or missing, why it matters, and the change you would make."""


def workers_label() -> str | subprocess.CompletedProcess[str]:
    """The workers account's label, or the failed lookup."""
    found = subprocess.run([*accounts_command(), "role", "workers"], capture_output=True, text=True, check=False)
    label = found.stdout.strip()
    return label if found.returncode == 0 and label else found


def codex_arguments(label: str, review: Path, working_dir: Path) -> list[str]:
    return [*accounts_command(), "exec", label, "exec",
            "-m", MODEL,
            "-c", f'model_reasoning_effort="{REASONING}"',
            "-c", f"model_auto_compact_token_limit={COMPACT_ABOVE_TOKENS}",
            "-c", 'approval_policy="never"',
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--color", "never",
            "-C", str(working_dir),
            "-o", str(review),
            "-"]


def run_folder() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS_ROOT / f"{stamp}-{os.getpid()}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-ERROR_TAIL_LINES:])


def workers_home() -> str:
    """Read the workers account's CODEX_HOME without evaluating shell text."""
    found = subprocess.run([*accounts_command(), "env", "workers"], capture_output=True, text=True, check=False)
    if found.returncode == 0:
        for assignment in shlex.split(found.stdout):
            if assignment.startswith("CODEX_HOME="):
                return assignment.removeprefix("CODEX_HOME=")
    raise ValueError("could not read CODEX_HOME from accounts env workers")


def review_thread(process: subprocess.Popen[str], log: Path) -> str:
    """Read Codex's header for up to 30 seconds, or until the process ends."""
    deadline = time.monotonic() + THREAD_HEADER_TIMEOUT
    header = ""
    with log.open(encoding="utf-8", errors="replace") as output:
        while True:
            header += output.read()
            match = re.search(r"^session id:[ \t]*(\S+)[ \t]*\r?\n", header, re.MULTILINE)
            if match:
                return match[1]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ""
            try:
                _ = process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
            header += output.read()
            match = re.search(r"^session id:[ \t]*(\S+)[ \t]*$", header, re.MULTILINE)
            return match[1] if match else ""


def run_review(label: str, review: Path, prompt: str, log: Path) -> int:
    """Keep the reviewer role only during this Codex process."""
    manager = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    codex_home = ""
    if manager:
        try:
            codex_home = workers_home()
        except (OSError, ValueError) as error:
            print(f"astra_review: reviewer role unavailable: {error}", file=sys.stderr)
    held_thread = ""
    with log.open("w", encoding="utf-8") as output, subprocess.Popen(
        codex_arguments(label, review, Path.cwd()), stdin=subprocess.PIPE, stdout=output,
        stderr=subprocess.STDOUT, text=True,
    ) as process:
        try:
            if process.stdin is not None:
                try:
                    _ = process.stdin.write(prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            if manager and codex_home:
                thread = review_thread(process, log)
                if thread:
                    try:
                        _ = astra_roles.hold("reviewer", thread, codex_home, pid=process.pid, manager=manager)
                        held_thread = thread
                    except (OSError, ValueError) as error:
                        print(f"astra_review: reviewer role unavailable: {error}", file=sys.stderr)
            return process.wait()
        finally:
            if held_thread:
                try:
                    astra_roles.release(held_thread)
                except OSError as error:
                    print(f"astra_review: could not release reviewer role: {error}", file=sys.stderr)


def held_review(design: design_roles.HeldDesign, focus: str, work: astra_work.PhaseWork | astra_work.StandaloneWork) -> int:
    reviewer = next(instance for instance in design.roles if instance.role == "reviewer")
    if not isinstance(reviewer.lifecycle, (design_roles.RoleOpen, design_roles.RolePausedForMove)):
        print(f"astra_review: held reviewer unavailable: {type(reviewer.lifecycle).__name__}", file=sys.stderr)
        return 1
    if isinstance(work, astra_work.PhaseWork):
        if (not isinstance(design.binding, design_roles.DesignBoundToPhase)
                or Path(design.binding.run_directory) != Path(work.run_directory)
                or design.binding.phase != work.phase_id):
            print("astra_review: held reviewer needs an idle rebind to the supplied phase", file=sys.stderr)
            return 1
    folder = design_roles.design_directory(design.placement.design_id)
    request = folder / f"review-request-{os.getpid()}-{time.time_ns()}.txt"
    with request.open("x", encoding="utf-8") as output:
        _ = output.write(f"Review {design.document}.\nFocus: {focus}\n")
    try:
        reply = design_roles.resume(design.placement.design_id + "/reviewer", request)
        if not reply.is_file():
            raise design_roles.RoleRefused("held reviewer returned no saved reply")
        print(f"# {MODEL} review of {design.document} (held reviewer)\n")
        print(reply.read_text(encoding="utf-8").strip())
        print(f"\nSaved: {reply}")
        return 0
    except (OSError, ValueError) as error:
        print(f"astra_review: held reviewer busy or unavailable: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1
    finally:
        request.unlink(missing_ok=True)


def main(arguments: list[str]) -> int:
    usage = "usage: astra_review.py [--run-directory <dir> --phase <id>] <document> [focus...]"
    if not arguments or arguments[0] in ("-h", "--help"):
        print(usage, file=sys.stderr)
        return 2
    options: dict[str, str] = {}
    while arguments and arguments[0].startswith("--"):
        option = arguments[0]
        if option not in ("--run-directory", "--phase") or option in options or len(arguments) < 2:
            print(f"astra_review: {usage}", file=sys.stderr)
            return 2
        options[option] = arguments[1]
        arguments = arguments[2:]
    if not arguments or (options and (set(options) != {"--run-directory", "--phase"} or not all(options.values()))):
        print(f"astra_review: supply --run-directory and --phase together; {usage}", file=sys.stderr)
        return 2
    try:
        work = astra_work.phase_work(options["--run-directory"], options["--phase"]) if options else astra_work.StandaloneWork()
    except ValueError as error:
        print(f"astra_review: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 2
    document = Path(arguments[0]).expanduser().resolve()
    if not document.is_file():
        print(f"astra_review: no document at {arguments[0]}", file=sys.stderr)
        return 2
    focus = " ".join(arguments[1:]).strip()
    try:
        held = design_roles.held_document(document)
    except (OSError, ValueError) as error:
        print(f"astra_review: held reviewer unavailable: {error}", file=sys.stderr)
        return 1
    if held is not None:
        return held_review(held, focus, work)
    label = workers_label()
    if not isinstance(label, str):
        said = tail(label.stderr or label.stdout)
        print(f"astra_review: could not read the workers account (exit {label.returncode}):\n{said}", file=sys.stderr)
        return 1
    folder = run_folder()
    review = folder / "review.md"
    assignment = astra_work.AstraWork("astra-reviewer", str(folder), str(document),
                                     datetime.now(timezone.utc).isoformat(), str(review), work)
    prompt = astra_work.header_line(assignment) + "\n" + review_prompt(document, focus)
    _ = (folder / "prompt.md").write_text(prompt, encoding="utf-8")
    log = folder / "codex.log"
    returncode = run_review(label, review, prompt, log)
    written = review.read_text(encoding="utf-8").strip() if review.is_file() else ""
    if returncode != 0 or not written:
        why = f"exit {returncode}" if returncode != 0 else "no review was written"
        said = tail(log.read_text(encoding="utf-8"))
        print(f"astra_review: the {MODEL} review of {document} failed on workers account {label} ({why}).",
              f"Codex said:\n{said}", f"Log: {log}", sep="\n", file=sys.stderr)
        return 1
    print(f"# {MODEL} review of {document} (workers account {label})\n")
    print(written)
    print(f"\nSaved: {review}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
