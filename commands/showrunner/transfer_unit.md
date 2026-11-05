---
description: Transfer a unit between productions.
argument-hint: "<unit> <to production doc>"
---

# Transfer unit

Run from the giving showrunner session, when the receiving showrunner asks for the unit. `<unit>` is
the unit's id or its stem. `<to production doc>` is the receiving production's doc. The script lives
at `~/.claude/scripts/production/transfer_unit.py`:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/transfer_unit.py" <plan|apply|verify> <slug> <unit> <to production doc>
```

1. Run `plan`. It writes nothing. It prints `Move <unit> to <production>`, or the reason it cannot
   find the unit or the receiving showrunner.
2. Run `apply`. The unit keeps running. It does these steps:
   1. change the unit's staff parent to the receiving showrunner;
   2. send the unit one message, "Your unit now belongs to production <slug>.", so that its status
      line names the new production. It prints `told <unit>` or `could not tell <unit>: <cause>`;
   3. move the unit's tab to the receiving showrunner's workspace, in its A-Z place among the unit
      tabs.

   It prints `done`. A rerun when the parent is already the receiving showrunner sends no message,
   moves the tab again if it is out of place, and prints `unchanged`. `verify` runs the same steps
   as `apply`. The unit takes the receiving merge branch at its own next merge.
3. Send one message to the receiving showrunner: the unit, its worktree and branch, and that it now
   reports to it. Tell the user in one line.
