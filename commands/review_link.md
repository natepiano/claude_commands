---
description: Make clickable review links for files on this machine, including Markdown reports and drafts.
---

**Arguments:** $ARGUMENTS (files; infer them from the conversation when clear).

Run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/review_link.py <file>... [--new]` on the machine with the files. Report any error.

To show edited files, run it again on the same files. Give the same links. Tell the user to refresh the page. Use `--new` only to make separate links. Links expire seven days after the last update.

Before you give each link, fetch it on this machine with `curl -s -o /dev/null -w '%{http_code}' '<URL>'`. Give it only if the result is `200`. Use a clickable Markdown link with the file name. Use only the review-links service. Never give a link from a hand-run server.

Links need Tailscale. They use the Tailscale IPv4 address because the host name does not resolve on its own machine. The path is short so the URL does not wrap in a terminal.

The page renders Markdown. It shows raw HTML, such as `<details>`, as text. GitHub renders that HTML. Keep a draft for GitHub in plain Markdown. Code blocks scroll sideways and do not wrap.
