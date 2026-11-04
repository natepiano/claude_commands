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

1. Run `plan`. It writes nothing. It either refuses or prints every step. It refuses when:
   - the unit's staff parent is not this production's showrunner, or the unit is retired;
   - the unit is on the Mac, or the receiving production is in another repository;
   - a merge is in progress in the unit's worktree or in either showrunner checkout;
   - the unit branch holds commits of this merge branch that the receiving merge branch does not
     have, because they would move with the unit;
   - a rename or another transfer is not finished in either production.

   A running unit is not a refusal: apply stops it itself, at step 1, as a rename does.
2. Run `apply` once, in the background. It waits for the unit to stop and for the relaunched unit to
   come up. It journals each step, so a rerun after a refusal resumes from the step that stopped. The steps are:
   1. stop the unit through the shutdown path and pause its Codex workers; each keeps its thread, and the unit's worker ledger moves to the receiving production;
   2. commit the plan's `> **Production:**` header on the unit branch, so that it names the receiving
      production, and push the branch;
   3. move the stall-watch stretch to the receiving production and forget its old pane;
   4. update the unit's staff parent;
   5. relaunch the same session. Its marks name the receiving production;
   6. verify.

   The unit takes the receiving merge branch at its own next merge.
   - **Exit 2** means the unit was still stopping after an hour. Find out why its stop holds, then rerun `apply`.
   - **Exit 1** names the step that refused. Fix the cause and rerun `apply`.
3. Verify fails when the plan header, the stall-watch stretch, the staff row, or the
   unit's marks still name this production. A failure leaves the journal open. Then run `verify` alone.
4. When the transfer is done, the unit resumes its current session. Restart sends no work prompt. Send one message to the receiving showrunner: the unit, its worktree and
   branch, and that it now reports to it. Tell the user in one line.
