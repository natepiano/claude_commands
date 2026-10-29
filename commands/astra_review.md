---
description: Ask a gpt-6-astra Codex reviewer, on the current workers account, to review a document (a proposal, plan, issue or anything else) and bring its review back to you.
---

# astra_review

<Guild>

Your guild is `reviewers`. Your head is the `reviewers` unit director in the `guilds` production on natedev. At skill start, use the Tips lock/pull rule for `~/rust/nate_style` and load `guilds/common/` and `guilds/reviewers/`; a Codex holder does this for its worker. Report to your head when the user corrects you, a phase needs repair round 4, a finding meets the Events repeat rule, you use a workaround for a tool fault or slow rule, or a method saves measured time. Send one report of at most 100 words, with evidence and result. Resolve the head from `message/roster.py --json` by production `guilds` and unit `reviewers`; send with `message/send.py` or SendMessage. A Codex worker puts the report in its reply for its holder to send. Send only to the head. Never wake an idle Codex member for guild advice.

Load tips: ~/.claude/scripts/lib/py ~/.claude/scripts/guilds/tips.py load --guild reviewers

Use Events in `~/.claude/docs/as-built/guilds.md` for report detection and form.

The script loads the reviewers block and tip text into each prompt. A Codex
holder relays a guild report in the reply unchanged.
</Guild>

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
   A held document uses its sole reviewer in the same thread. Busy or unavailable
   means report that state; do not start another reviewer. Without a held design,
   the script keeps its one-turn read-only review.
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
   - Exit 2: report the `astra_review:` error line. A phase or tips-baseline
     error is that error, not a missing document.

The run folder under `/tmp/claude/astra/` keeps the prompt, Codex's log and the
review.
