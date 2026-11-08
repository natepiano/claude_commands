---
description: Resume a retired production unit.
argument-hint: "<unit> [--dry-run]"
---

# Resume unit

Run in the showrunner session. `<unit>` is the unit's id or stem.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/resume_unit.py" <showrunner name> <unit> [--dry-run]
```

Print the script output without changes.

`/showrunner:retire_unit` keeps what a resume needs: the staff row keeps the worktree path in
`RemovedWorktree`, and the parked `.unit.toml` keeps the plan, the branch and the director's last session.

It refuses, and changes nothing, when:
- the row is not retired, or its parked `.unit.toml` names no session;
- no account has that session's transcript under the worktree's project folder: `claude --resume`
  finds a transcript only under the folder where the session started. Every account links the same
  `projects` folder, so a units account that moved since the retire still finds it;
- a folder at the worktree path is not the unit's worktree, or the branch is still here or on
  origin while the worktree is gone and no retired ref is recorded: retire did not finish.
  With a retired ref, an existing branch is accepted only at the saved tip.

Then it:
1. fetches a recorded retired ref, restores the branch and worktree at its saved tip, checks
   worktree HEAD, then deletes the origin ref. With no ref, it uses the merge branch as before;
2. restores `.unit.toml`, sets `PresentWorktree` on the retained row, and resumes that session through `add_unit.py --resume`, on the units account, with the unit's
   name and Remote Control;
3. deletes the parked `.unit.toml` after the running session and its pane `STAFF_ID` are proven.

A rerun after a failure resumes: each step checks whether it is done, and the parked file keeps
the session facts until launch is proven.

`--dry-run` prints each refusal and each step, and changes nothing.

Exit 0 means the unit is resumed. Exit 1 is a refusal or a failed step.
