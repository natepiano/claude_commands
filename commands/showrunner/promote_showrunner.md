---
description: Turn one of this production's running units into the showrunner of its own production, still reporting to this one.
argument-hint: "<unit> <production doc> [check]"
---

# Promote showrunner

**Usage:** `/showrunner:promote_showrunner <unit> <production doc>`, or with
`check` to see whether it would go ahead.

Run this in the showrunner session of a running production. The unit keeps its
conversation and becomes the showrunner of the production the doc describes,
which then reports to yours. User, 2026-10-12: "we need a "promote" command that
turns a unit into a showrunner and closes it and restarts it in the showrunner
account (unless for some reason the accounts happen to be running both roles at
the time)". The other direction, a session becoming a unit, is
`/showrunner:promote_unit`.

First the unit writes the new production's doc in the production format and
commits it. This command writes no doc.

Run, with the sandbox off:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/promote_showrunner.py" <unit> --production <PRODUCTION_DOC> --model <your model id> --effort <your effort level> [--check]
```

Pass your own model id and effort level, as for `/showrunner:add_showrunner`.
Show the script's output. If it exits nonzero, show its message and stop.

What it does:

1. **Checks**, and refuses with the cause, stopping nothing:
   - the unit has no live session under your production;
   - it is mid-turn, or its pane shows a form, prompt or draft;
   - it has a `/unit:delegate` run open, which keeps its workers, so the run finishes first;
   - it has uncommitted work, so it checkpoints first;
   - the doc is missing, uncommitted or not a production doc;
   - the new production already has a showrunner;
   - Claude is not trusted in the unit's worktree on the showrunner account,
     where the session resumes (`/showrunner:add_showrunner` names what
     counts as trusted).
2. **Stops** the unit: it types `/exit` into the unit's pane and waits until its
   Claude is gone, then ends its session on its host, tmux or herdr. Two processes on one session
   corrupt it, so nothing restarts while the old one runs.
3. **Restarts** it with `/showrunner:add_showrunner`'s launcher, resuming the
   same session in the unit's worktree: in its own herdr session named for the
   new production, with no unit mark, on the unit's own account, attached to your production.
4. **Retires** the unit's Units row in your doc, `(retired <date>: promoted to
   showrunner of <slug>)`, and commits it in your checkout. Its branch stays as
   it is; the new production merges to the branch its own doc names.

The last line says whether the account changed: `moved from <unit account> to
the showrunner account`, or `same account, no move`. If the restart fails after
the stop, the output gives the one command that brings the session back as the
unit it was, and the Units row is unchanged.
