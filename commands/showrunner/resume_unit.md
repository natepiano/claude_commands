---
description: Resume one retired unit of this production. It makes the unit's worktree again and resumes the session its director last used.
argument-hint: "<unit> [--dry-run]"
---

# Resume unit

Run from the showrunner session. `<unit>` is the unit's id or its stem.

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/resume_unit.py" <slug> <unit> [--dry-run]
```

When it finishes, print its output exactly as it comes, with nothing added.

`/showrunner:retire_unit` keeps what a resume needs in the Units row: the worktree path, the branch,
the plan, and the director's last session in the note `(retired <date>, worktree removed, session <id>)`.
Unmerged work adds `, ref refs/retired/<staff id>, tip <SHA>` before the closing parenthesis.

It refuses, and changes nothing, when:
- the row is not retired, or its note names no session;
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
3. takes the retired note out of the Plan cell, committed on the merge branch (the next merge push
   takes it out). The Units row is live again. Parked facts go only after the running session
   and its pane `STAFF_ID` are proven.

A rerun after a failure resumes: each step checks whether it is done, and the note goes last,
while the parked file keeps the session facts until launch is proven.

`--dry-run` prints each refusal and each step, and changes nothing.

Exit 0 means the unit is resumed. Exit 1 is a refusal or a failed step.
