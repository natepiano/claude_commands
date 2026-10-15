---
description: Add a change for the user to check to the production's change review list, walk the list when the user asks for "change review", and record their verdicts.
---

# Change review

Every production keeps a list of changes waiting for the user's check: the file
its production doc's **Change review** line names. Merges add a row for each
`Change-Review:` line in a checkpoint. This skill adds the rest, shows the list
when the user asks, and records verdicts. Unit directors and showrunners use it.

Every action runs:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/change_review.py" <action> …
```

On exit 1 report its one line.

## Find the list

`list-path` prints the list's path; pass it as `--list` to every other action.

- A showrunner runs `list-path` alone: it finds the production from this
  session's update timer, as `/showrunner:proposals` does. A session with no
  timer passes `--production <doc>`.
- A unit director runs `list-path --production <doc>`, with the doc its plan
  header's `> **Production:**` line names.

A production doc without a Change review line has no list: say so in one
line. The showrunner adds the line (`~/.claude/docs/production_format.md`). A
missing list file is created on its first row.

## add

For a change the user should check that no checkpoint `Change-Review:` line
carries: a showrunner's own change, a fix outside a run, or a change nobody
sees in the app, such as a table's columns or a status line.

```sh
… add --list <list> --change "<what the user sees change>" --check "<how to check it>" [--merged <hash>]
```

One row per change. Each part is one line in plain words. Pass `--merged` when
a commit holds the change. It prints the new row's id, such as `C12`.

## show

Only when the user asks for "change review". Run `show --list <list>`. It
prints each row to review: its id and change, how to check it, and when it
merged, or `nothing to review`. Walk the rows one at a time, in order. For each,
say what changed and how to check it in plain words, then wait for the user's
verdict in their reply. Never use a form; ask in one plain-text line.

## verdict

A verdict beginning `works` or `fine` lands the row; any other returns it to
its unit. In a Hana or `~/.claude` production, the showrunner first runs the
feature-log step of `/showrunner:produce` → `<CloseChangeReview/>`; a failure
there leaves the row open. Then:

```sh
… verdict --list <list> --row C<n> --verdict "<the user's words>"
```

It moves the row to `## Reviewed` with the verdict and today's date, and prints
`C<n> landed` or `C<n> returns to its unit (merged …)`. Send a returning row,
its change and the user's words to the unit that made it: the merge commit it
names gives the checkpoint, and so the unit. A change outside a unit goes back
to the session that added it. The fix comes back as a new row.

## Never a question

A change for the user to check is never put in `open_for_user` and never
counted among the open questions. It goes on this list, and the user asks for
"change review" when they want it.
