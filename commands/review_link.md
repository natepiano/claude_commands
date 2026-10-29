---
description: Make clickable review links for files on this machine, including Markdown reports and drafts.
---

**Arguments:** $ARGUMENTS (files, with optional `--name <label>`; infer the artifact from the conversation when unambiguous).

Run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--name <label>]` on the machine holding the files. Put each returned URL in the reply as a clickable Markdown link named for its file. Report any refusal from the script.

Links are snapshots, expire after seven days, and require Tailscale. To share changed content, run the command again.
