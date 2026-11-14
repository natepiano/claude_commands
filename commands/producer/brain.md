---
description: Manage brain.
---

# brain

Start brain with `resume brain`.
Use the workers account for Codex and the showrunners account for Claude.
Read the family, model, and effort from `[producers].brain` in `~/.claude/config/models.conf`.
Use the `brain` tab in the `producers` herdr workspace.
Resume the same conversation.

## Transfer

Run `agents transfer brain [--model MODEL --effort EFFORT]` in the session that takes the role.
Give both options when that session's family differs from `[producers].brain`.
The session restarts as brain and receives no prompt.
After the restart, load this command and read the role documents.
Read the old conversation with the printed `catch_up.py` command, then run `send.py pending`.
