---
description: Manage a showrunner's staff workers through the shared worker command.
---

# Showrunner workers

Read `~/.claude/commands/unit/worker.md` in full.
Use the supplied arguments: `$ARGUMENTS`.
Run the command from the showrunner's session.
The native session selects the showrunner row.
The command uses the same roles, selector, transcript receipt, and exit codes.
Only worker end and permanent removal delete worker rows.

Assignment returns after submission. End the showrunner turn after any assigned synchronous review.
The turn hook sends the result and reply path to the showrunner.
Read that message before the next workflow step.

An unexpected screen prompt ends no turn, and the director finds it only when it reads the pane.
