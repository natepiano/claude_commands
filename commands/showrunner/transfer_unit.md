---
description: Move one unit of this production to another production. The unit keeps its session, worktree, branch and commits; it relaunches under the new showrunner.
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
   - the unit is not in this production's Units table, is reserved, or is retired;
   - the receiving Units table already has the unit;
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
   3. commit the Units row into the receiving doc, on the receiving merge branch;
   4. commit the Units row out of this doc, on this merge branch. The row moves; it is not marked retired;
   5. move the stall-watch stretch to the receiving production, retire this pool's reviewers of the
      unit, and forget its old pane;
   6. point the unit's shutdown record at the receiving production and doc;
   7. relaunch the same session. Its marks name the receiving production;
   8. verify.

   The doc commits are not pushed. Each goes out with its showrunner's next merge push. The unit
   takes the receiving merge branch at its own next merge.
   - **Exit 2** means the unit was still stopping after an hour. Find out why its stop holds, then rerun `apply`.
   - **Exit 1** names the step that refused. Fix the cause and rerun `apply`.
3. Verify fails when a doc, the plan header, the stall-watch stretch, the shutdown record, the
   unit's marks or this pool still name this production. A failure leaves the journal open. Then run `verify` alone.
4. When the transfer is done, the unit restarts. Its first prompt names its new production doc, merge
   branch and showrunner. Send one message to the receiving showrunner: the unit, its worktree and
   branch, and that it now reports to it. Tell the user in one line.
