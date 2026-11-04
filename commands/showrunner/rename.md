---
description: Rename a production and its records.
argument-hint: "plan <old> <new> | apply <old> <new> | unit <new> <unit> | checkout <new> [<path>]"
---

# Rename production

Run from the showrunner session. Pass the arguments to:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename.py" <arguments>
```

Show the output. On a nonzero exit, show the error and stop. Use this order:

1. `plan <old> <new>` prints the document, timer, state and merge-branch
   changes. It writes nothing. The source needs a registered update timer that
   names its production doc. Read every path before you apply. The new slug
   starts with an ASCII letter or digit and contains only ASCII letters,
   digits, `_` or `-`. It cannot be a reserved state name or a current
   production name.
2. `apply <old> <new>` writes the staff name first. Then it renames the
   production doc, records the Formerly name, updates the unit-plan headers,
   moves the timer, the saved state, the stall-watch files and the worker
   ledgers, and retargets the phase notes. A merge branch named `<old>` takes
   the name `<new>` with `git branch -m`. Another merge branch (`main`,
   `init/catalyst`) keeps its name. Apply commits the documents, pushes the
   merge branch and sets the herdr tab title. A step that fails prints
   `anomaly: <step>: <reason>`, and the next step runs. Run `apply` again
   until it prints `unchanged`.
3. For each unit that is not retired, run `unit <new> <unit>` with its exact
   Units-row name. `unit` uses the folder move: it stops the unit and its
   workers, moves the worktree, writes the staff worktree and starts them
   again. Then it renames the unit branch with `git branch -m`, commits the
   Worktree and Branch cells and pushes the merge branch. A rerun with nothing
   to change prints `<unit>: unchanged`.
4. `checkout <new> [<path>]` moves the showrunner checkout to `<path>`, by
   default its folder name with the old slug replaced by `<new>`. It stops
   this showrunner session and restarts it in the new folder, so end the turn
   after it prints. After the restart, run the same command again: it pushes
   the merge-branch commit that names the new checkout. If the move fails part
   way, the showrunner restarts in the folder that holds the checkout. Show
   the printed reason.

The production argument of `apply` and `unit` accepts the old, the new or a
former name. Never rewrite history: existing commit subjects, log lines and
transcript contents keep the old name. Never force-push to do a rename.

Implementation details: `docs/as-built/production-rename.md`.
