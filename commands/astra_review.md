---
description: Review a document with gpt-6-astra.
---

# astra_review

One Codex turn with gpt-6-astra at reasoning xhigh reads the document read-only
and reviews it: clarity, correctness, gaps, risks and what it would change, with
each finding tied to a section or a file and line. The user, showrunners and unit
directors use it the same way. It runs on the current workers account and
changes no file.

**Usage:** `/astra_review [--run-directory <dir> --phase <id>] <document path> [focus text]`

## Steps

1. Consume `--run-directory <dir> --phase <id>` before reading the document;
   pass both for phase work, neither for standalone work. The next word is the
   path and the rest is focus. With no path, ask for one in one plain line and stop.
   Resolve a relative path from the working directory; if no file is there, say
   so and stop.
2. Run the review as one command, with `dangerouslyDisableSandbox: true` (it
   reads `~/.codex*` and reaches the network):
   `~/.claude/scripts/lib/py ~/.claude/scripts/astra/astra_review.py [--run-directory <dir> --phase <id>] "<path>" <focus>`
   A held document uses its sole reviewer pane under the unit or showrunner.
   The held role uses `worker.py` with the explicit `astra-reviewer` role and keeps its native conversation.
   Report a busy or unavailable reviewer; do not start a replacement.
   Without a held document, a unit or showrunner review starts through `assign.sh --role review --operation astra-review`.
   The launcher uses the review run's transport pin.
   If worker end fails, its staff row remains visible in `worker.py list`.
   The exec hold ends with the launcher process.
   A review outside the staff hierarchy uses `assign.sh --target consultation-pane --operation astra-review`.
   Its consultation pane uses Codex `gpt-6-astra` at `xhigh` with read-only access and approval `never`.
   The launcher reads the review from the transcript, then closes the pane.
   - **Claude:** run it with `run_in_background: true`, tell the caller in one
     line that the astra review is running, then end the turn or do other work.
     The task notification is the wait. Never poll it, sleep or read its output
     before then. A review at xhigh can take many minutes.
   - **Pane worker:** start it with
     `~/.claude/scripts/lib/py ~/.claude/scripts/agents/mesh_runs.py start --label "astra review" -- <the command>`
     End your turn; the result arrives as your next message.
     Exit 3 means the caller is not a worker; run attached in one call only for that exit.
     A live twin takes priority over `config/mesh_runs.conf` `LIVE_RUN_LIMIT`.
     Exit 4 refuses a new run at that limit and names the live runs.
     Wait for a run's result, or put more files in one run. Never run attached after exit 4.
3. When it finishes, read its output once.
   - Exit 0: present the review that follows the heading line, in full. Name
     where it was saved (the `Saved:` line). The review is advice: add your own
     view only where you were asked for it, or where the review is wrong about
     something you can check.
   - Exit 1: the review failed. Report what Codex said and the account it ran
     on. Do not retry with another model or account.
   - Exit 2: report the `astra_review:` error line. A phase or tips-baseline
     error is that error, not a missing document.

The run folder under `/tmp/claude/astra/` keeps the prompt, Codex's log and the
review.
