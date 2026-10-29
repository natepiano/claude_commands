---
description: Rename a production, move its units between runs, and retire its old merge branch.
argument-hint: "plan <old> <new> | apply <old> <new> | unit <new> <unit> | repair-workers <production> [--dry-run] | finish <new> | checkout <new> [<path>]"
---

# Rename production

Run from the showrunner session. Pass the arguments to:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename.py" <arguments>
```

Show the output. On a nonzero exit, show the refusal and stop. Use this order:

1. `plan <old> <new>` prints the document, timer, state and merge-branch
   changes without writing. The source needs a registered update timer naming
   its production doc. Read every path before applying. The new slug must
   start with an ASCII letter or digit and contain only ASCII letters, digits,
   `_` or `-`; reserved state names and current or former production names refuse.
2. `apply <old> <new>` requires the showrunner checkout clean and on the
   production's declared merge branch. The production's own log and its
   `-recovered` copy may stand untracked; a log named `<old>-log.md` moves to
   `<new>-log.md`. It renames the production doc, records
   Formerly names, updates unit-plan headers, moves the timer and saved state,
   and commits and pushes the merge branch. A merge branch named `<old>` takes
   the name `<new>`; any other merge branch (`main`, `init/catalyst`) is not
   named for the production and keeps its name. Existing destination paths or,
   for a renamed merge branch, an existing `<new>` branch refuse. Apply
   finishing is not the end: unit branches and worktrees still have their old
   names, and a renamed old merge branch stays until `finish`.
3. For each non-retired unit, between its runs, run `unit <new> <unit>` with
   its exact Units-row name. Keep it stopped until the command succeeds.
   The command pushes its new branch, moves and repairs its worktree, moves
   account transcripts and copies trust entries, commits the new Worktree and
   Branch cells, then retires its old branch. It updates the stopped shutdown
   record, run worktree and plan paths, worker launch paths and unit ledger;
   paused workers keep their threads, Codex homes and paused state.
   It prints `shutdown.py restart --only <session-id>`; relaunch the unit
   with that command after `unit` succeeds. With no record it reports that no
   relaunch is available. **Tell the unit director the new
   worktree path printed by the command before it resumes.** `add_unit` writes
   the new herdr production mark at the unit's next launch; existing marks may
   still name the old production.
4. `finish <new>` refuses while any non-retired row names an old-prefixed
   branch or a unit move is unfinished. When apply renamed the merge branch,
   it deletes the old merge branch on origin and locally; a kept merge branch
   is not touched. Then it marks the rename done. Repeating a completed
   `finish` does nothing.
5. `checkout <new> [<path>]` moves the showrunner checkout to `<path>`, by
   default its folder name with the old slug replaced by `<new>`. It stops
   this showrunner session and restarts it in the new folder, so end the turn
   after it prints. After the restart, run the same command again: it pushes
   the merge-branch commit that names the new checkout. If the move fails part
   way, the showrunner restarts in whichever folder holds the checkout; show
   the printed reason.

`unit` and `finish` require `apply` to have finished, through its merge-branch
push. A live Claude, an
unknown Claude state, or unreadable session discovery on either host refuses
a unit move. Retired rows have no live worktree and cannot be moved. A changed
old unit branch tip, occupied destination, unrelated checkout edits, or another
unfinished unit move also refuses. Resolve the reported condition before retrying;
do not delete branches or journal files to bypass it.

Retry a refused or interrupted step by **rerunning the same command**. For
`apply`, keep destination `<new>` fixed; its source argument accepts the old
name, the new name once the journal exists, or a former name. After apply has
registered the new timer, `unit` and `finish` accept the old, new or former
production name. Completed work is retained and the journal resumes the rest.
If trust entries disappear after three two-second checks, the refusal names
the account file; the next launch needs a trust prompt. Retry after resolving
the account's competing writes.

The journal is `~/.local/state/showrunner/<new>/rename.json` (or the configured
showrunner state root). `rename.lock` is internal coordination: leave it to the
script. `rename-history/` holds completed journals archived by later renames;
it is archival state, not a recovery command.

For units moved before worker path repair was added, run
`repair-workers <production> --dry-run`, then `repair-workers <production>`.
It uses the journal's moved units, prints each change or that a unit has nothing
to change, and can be repeated. Dry run changes nothing.

Never rewrite history: existing commit subjects, log lines and transcript
contents keep the old name. Never force-push to carry out a rename.

Implementation and recovery details: `docs/as-built/production-rename.md`.
