#!/usr/bin/env python3
"""astra_review.py — Ask gpt-6-astra for a review of one document and print the review.

Usage: astra_review.py <document> [focus...]

The review is one `codex exec` turn on the current workers account, read-only, at reasoning xhigh.
The reviewer's last message is saved in the run folder under /tmp/claude/astra/ and printed when it
finishes. A run that fails prints Codex's error and the account, and exits 1. A missing document
exits 2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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
number), what is wrong or missing, why it matters, and the change you would make.
"""


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


def main(arguments: list[str]) -> int:
    if not arguments or arguments[0] in ("-h", "--help"):
        print("usage: astra_review.py <document> [focus...]", file=sys.stderr)
        return 2
    document = Path(arguments[0]).expanduser().resolve()
    if not document.is_file():
        print(f"astra_review: no document at {arguments[0]}", file=sys.stderr)
        return 2
    focus = " ".join(arguments[1:]).strip()
    label = workers_label()
    if not isinstance(label, str):
        said = tail(label.stderr or label.stdout)
        print(f"astra_review: could not read the workers account (exit {label.returncode}):\n{said}", file=sys.stderr)
        return 1
    folder = run_folder()
    review = folder / "review.md"
    prompt = review_prompt(document, focus)
    _ = (folder / "prompt.md").write_text(prompt, encoding="utf-8")
    log = folder / "codex.log"
    with log.open("w", encoding="utf-8") as output:
        finished = subprocess.run(codex_arguments(label, review, Path.cwd()), input=prompt, stdout=output,
                                  stderr=subprocess.STDOUT, text=True, check=False)
    written = review.read_text(encoding="utf-8").strip() if review.is_file() else ""
    if finished.returncode != 0 or not written:
        why = f"exit {finished.returncode}" if finished.returncode != 0 else "no review was written"
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
