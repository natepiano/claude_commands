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

It refuses, and changes nothing, when:
- the row is not retired, or its note names no session;
- no account has that session's transcript under the worktree's project folder: `claude --resume`
  finds a transcript only under the folder where the session started. Every account links the same
  `projects` folder, so a units account that moved since the retire still finds it;
- a folder at the worktree path is not the unit's worktree, or the branch is still here or on
  origin while the worktree is gone: the retire did not finish.

Then it:
1. makes the worktree again at the same path, on the unit's branch, from the merge branch;
2. resumes that session through `add_unit.py --resume`, on the units account, with the unit's
   name and Remote Control;
3. takes the retired note out of the Plan cell, committed on the merge branch (the next merge push
   takes it out). The Units row is live again.

A rerun after a failure resumes: each step checks whether it is done, and the note goes last,
because it holds the session id.

`--dry-run` prints each refusal and each step, and changes nothing.

Exit 0 means the unit is resumed. Exit 1 is a refusal or a failed step.
