---
description: Add a change for the user to check to the top-level showrunner's change review list, walk the list when the user asks for "change review", and record their verdicts.
---

# Change review

Each top-level showrunner keeps one list of changes waiting for the user's
check, named for it: `~/rust/hanadocs/change-review/change-review - <showrunner>.md`,
such as `change-review - natedev.md`. A child production's rows go to its
root's list. Merges add a row for each
`Change-Review:` line in a checkpoint. This skill adds the rest, shows the list
when the user asks, and records verdicts. Unit directors and showrunners use it.

Every action runs:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/change_review.py" <action> …
```

On exit 1 report its one line.

## Find the list

Every action finds the list from its production, written `<production>` below:

- A showrunner passes nothing: the script finds the production from this
  session's update timer, as `/showrunner:proposals` does. A session with no
  timer passes `--production <doc>`.
- A unit director passes `--production <doc>`, with the doc its plan header's
  `> **Production:**` line names.

The list is named for the production's top-level showrunner while it runs, and
otherwise is the path the doc's **Change review** line names. `list-path`
prints it and `showrunner` prints the name. A production doc without a Change
review line has no list: say so in one line. The showrunner adds the line
(`~/.claude/docs/production_format.md`). A missing list file is created on its
first row, with its frontmatter.

Every write sets the list's `date_modified` and adds the production's doc to
its `productions`, so the list's frontmatter names every production it holds.

Every write commits the list itself when it sits in a git repository, so never
commit it by hand. A `written but not committed` warning needs nothing: the
write stands and the next write commits it. The vault's Templater must exclude
the change-review folder, since its creation trigger rewrites a new list within
half a second and can drop a row; the vault owner sets Excluded folders →
`change-review`.

## add

For a change the user should check that no checkpoint `Change-Review:` line
carries: a showrunner's own change, a fix outside a run, or a change nobody
sees in the app, such as a table's columns or a status line.

```sh
… add <production> --change "<what changed for you>" --check "<how to try it: where and what to type>" --see "<what you see>" [--merged <hash>]
```

One row per change, written for a person with no context. Each part is one
line. `--check` names the machine (natedev or the Mac), the session (a
terminal, a showrunner session, a unit session, any Claude session) and what to
type; `--see` is what the person then sees. Example: `--change "The unit board
shows each unit's phase" --check "In a showrunner session on natedev, type
/showrunner:unit_board" --see "Each row has a Phase column"`. The row's How to
check cell is `<how to try it> You see: <what you see>`. Pass `--merged` when a
commit holds the change. It prints the new row's id, such as `C12`.

## edit

To rewrite a row still to review, such as one that lacks where to try it or
what the person sees:

```sh
… edit <production> --row C<n> --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
```

The row keeps its id, its Merged cell and its place. It prints the new row.

## show

Only when the user asks for "change review". Run `show <production>`. It
prints each row to review: its id and change, how to check it, and when it
merged, or `nothing to review`. Walk the rows one at a time, in order. For each,
say what changed, how to try it and what the user sees, then wait for the user's
verdict in their reply. Never use a form; ask in one plain-text line.

A request for "the next change" means the user is returning to the terminal:
show the current unconfirmed row in full, even if already shown. Never show a
later row until the current one has a verdict (user, 2026-10-16).

## verdict

A verdict beginning `works` or `fine` lands the row; any other returns it to
its unit. In a Hana or `~/.claude` production, the showrunner first runs the
feature-log step of `/showrunner:produce` → `<CloseChangeReview/>`; a failure
there leaves the row open. Then:

```sh
… verdict <production> --row C<n> --verdict "<the user's words>"
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
