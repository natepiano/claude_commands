#!/usr/bin/env python3
"""Ask gpt-6-astra to review one document.

Usage: astra_review.py [--run-directory <dir> --phase <id>] <document> [focus...]

The review uses the workers account with read-only access and xhigh reasoning.
A managed review publishes its worker before it submits the prompt.
The hook saves a managed reply under /tmp/claude/astra/.
Use --read <reply> --run-directory <run> after the hook reports completion.
A failed review exits 1. A missing document or refused admission exits 2.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.astra import astra_work, design_roles  # noqa: E402
from scripts.lib import unit_runs
from scripts.production import staff, worker
from scripts.direct import progress_history
from scripts.lib.atomic_file import write_text_atomic

MODEL = "gpt-6-astra"
REASONING = "xhigh"
# Lines of Codex's own output a failure prints.
ERROR_TAIL_LINES = 40
SCRIPTS = Path(__file__).resolve().parents[1]
# Tests use a stub accounts command.
ACCOUNTS_COMMAND = os.environ.get("ASTRA_REVIEW_ACCOUNTS") or ""


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


def run_folder() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = unit_runs.ASTRA_RUN_ROOT / f"{stamp}-{os.getpid()}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-ERROR_TAIL_LINES:])


def _managed_review(review: Path, log: Path, target: str) -> int:
    """Run one managed review through assign.sh."""
    run = log.parent
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run([
            "bash", str(SCRIPTS / "direct/assign.sh"), "--role", "review", "--operation", "astra-review",
            "--target", target, "--policy", "astra-reviewer",
            "--new-worker", f"astra-reviewer{os.getpid()}", "--session-dir", str(run), "--cwd", str(Path.cwd()),
            "--prompt", str(run / "prompt.md"), "--reply-file", str(review.resolve()), "--log", str(run / "turn.log"),
            "--model", MODEL, "--effort", REASONING,
        ], stdout=output, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode == 0 and log.read_text(encoding="utf-8").splitlines()[-1:] == ["submitted"]:
        return worker.DispatchSubmission.SUBMITTED
    return result.returncode


def run_review(review: Path, prompt: str, log: Path) -> int:
    """Run each review through its explicit assign.sh target."""
    manager = staff.runtime_id()
    caller = staff.snapshot().by_current_session(manager) if manager else None
    target = "consultation-pane"
    if caller is not None and caller.kind in ("unit", "showrunner"):
        target = "staff-worker"
    write_text_atomic(log.parent / "prompt.md", prompt)
    return _managed_review(review, log, target)


submitted_review = design_roles.submitted_review


def read_review(reply: Path, run: Path) -> int:
    """Read a completed pass after the caller receives its hook message."""
    if not progress_history.reply_completed(run, reply):
        print("astra_review: the review pass is not completed", file=sys.stderr)
        return 1
    print(reply.read_text(encoding="utf-8").strip())
    print(f"\nSaved: {reply}")
    return 0


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
    write_text_atomic(request, f"Review {design.document}.\nFocus: {focus}\n")
    try:
        submitted = design_roles.resume(design.placement.design_id + "/reviewer", request)
        submitted_review(submitted.reply, submitted.run)
        return 0
    except (OSError, ValueError) as error:
        print(f"astra_review: held reviewer busy or unavailable: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1
    finally:
        request.unlink(missing_ok=True)


def main(arguments: list[str]) -> int:
    if len(arguments) == 4 and arguments[0] == "--read" and arguments[2] == "--run-directory":
        try:
            return read_review(Path(arguments[1]).expanduser().resolve(), Path(arguments[3]).expanduser().resolve())
        except (OSError, ValueError, SystemExit) as error:
            print(f"astra_review: {error}", file=sys.stderr)
            return 1
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
    if isinstance(held, design_roles.HeldDesign):
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
    write_text_atomic(folder / "prompt.md", prompt)
    log = folder / "codex.log"
    returncode = run_review(review, prompt, log)
    if returncode is worker.DispatchSubmission.SUBMITTED:
        submitted_review(review, folder)
        return 0
    written = review.read_text(encoding="utf-8").strip() if review.is_file() else ""
    if returncode != 0 or not written:
        why = f"exit {returncode}" if returncode != 0 else "no review was written"
        said = tail(log.read_text(encoding="utf-8"))
        print(f"astra_review: the {MODEL} review of {document} failed on workers account {label} ({why}).",
              f"Codex said:\n{said}", f"Log: {log}", sep="\n", file=sys.stderr)
        if written:
            print(f"Saved: {review}", file=sys.stderr)
        return 2 if returncode == 2 else 1
    print(f"# {MODEL} review of {document} (workers account {label})\n")
    print(written)
    print(f"\nSaved: {review}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
