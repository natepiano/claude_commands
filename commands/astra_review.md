---
description: Ask a gpt-6-astra Codex reviewer, on the current workers account, to review a document (a proposal, plan, issue or anything else) and bring its review back to you.
---

# astra_review

**Usage:** `/astra_review <document path> [focus text]`

One Codex turn with gpt-6-astra at reasoning xhigh reads the document read-only
and reviews it: clarity, correctness, gaps, risks and what it would change, with
each finding tied to a section or a file and line. The user, showrunners and unit
directors use it the same way. It runs on the current workers account and
changes no file.

## Steps

1. Take the path and the focus from `$ARGUMENTS`: the first word is the path,
   the rest is the focus. With no path, ask for one in one plain line and stop.
   Resolve a relative path from the working directory; if no file is there, say
   so and stop.
2. Run the review as one command, with `dangerouslyDisableSandbox: true` (it
   reads `~/.codex*` and reaches the network):
   `~/.claude/scripts/lib/py ~/.claude/scripts/astra/astra_review.py "<path>" <focus>`
   - **Claude:** run it with `run_in_background: true`, tell the caller in one
     line that the astra review is running, then end the turn or do other work.
     The task notification is the wait. Never poll it, sleep or read its output
     before then. A review at xhigh can take many minutes.
   - **Codex mesh worker:** start it with
     `python3 ~/.claude/scripts/agents/mesh_runs.py start --label "astra review" -- <the command>`
     and end your turn; the result is your next message. Exit 3 means you are
     not a mesh worker: run the command attached in one call instead.
3. When it finishes, read its output once.
   - Exit 0: present the review that follows the heading line, in full. Name
     where it was saved (the `Saved:` line). The review is advice: add your own
     view only where you were asked for it, or where the review is wrong about
     something you can check.
   - Exit 1: the review failed. Report what Codex said and the account it ran
     on. Do not retry with another model or account.
   - Exit 2: the document was missing; say so.

The run folder under `/tmp/claude/astra/` keeps the prompt, Codex's log and the
review.
