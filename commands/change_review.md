---
description: Add a change for the user to check to the top-level showrunner's change review list, show the next change when the user asks for one, and record their verdicts.
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
… add <production> --change "<what changed for you>" --check "<how to try it: where and what to type>" --see "<what you see>" [--merged <hash>] [--urgency <level>] [--due <ISO>] [--after C<n>]
```

`--urgency` is `blocks` (work stops until the user checks it), `soon` (with
`--due`), `later` or `user`; leave it out for a normal row. `--after` names a
row that must be checked first.

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
… edit <production> --row <id> --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
```

Pass the ID as `/next` shows it, such as `C434` or `sdlc/C9`.
The row keeps its id, its Merged cell and its place. It prints the new row.

## show

Only when the user asks for "change review" or "the next change"
(`/next change`). Run `show <production>`. It prints the current row:
its id and change, how to check it, when it merged, and how many rows wait
after it; or `nothing to review`. Say what changed, how to try it and what the
user sees, even if already shown, then wait for the user's verdict in their
reply. Never use a form; ask in one plain-text line.

Show one row per request. After its verdict, stop: never show the next row
until the user asks for it (user, 2026-10-20).

The list keeps the order of `/next` (`commands/next.md`): the row `show`
printed stays first until its verdict. When the user calls a row important,
run `mark-important <production> --row <id>`, with the ID as `/next` shows it,
such as `C434` or `sdlc/C9`.

## A folder for the user to try

When the user needs a folder to try a change in (a repro, a demo crate, a
copy), make it after `add` prints the row's number, never by hand:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/scratch.py" new --task row:<showrunner>-C<n> --name <word> --visible
```

It prints `~/rust/repro/<showrunner>-C<n>-<word>`, where cargo-port lists it.
Name that path in the row's How to check cell (`edit`). `adopt <path>` with the
same options moves a folder that already exists there. Without `--visible`
the folder goes in `/tmp/claude/scratch`. The row's verdict removes both.

## verdict

A verdict that says `works` or `fine` and no word of failure (`not`, `still`,
`broken` and the like) lands the row, such as "okay c17 works"; any other
returns it to its unit. In a Hana or `~/.claude` production, the showrunner first runs the
feature-log step of `/showrunner:produce` → `<CloseChangeReview/>`; a failure
there leaves the row open. Then:

```sh
… verdict <production> --row <id> --verdict "<the user's words>"
```

Pass the ID as `/next` shows it, such as `C434` or `sdlc/C9`.
It moves the row to `## Reviewed` with the verdict and today's date, and prints
`C<n> landed` or `C<n> returns to its unit (merged …)`. Either way it removes
the row's scratch folders and prints `removed <path>` for each. Send a returning row,
its change and the user's words to the unit that made it: the merge commit it
names gives the checkpoint, and so the unit. A change outside a unit goes back
to the session that added it. The fix comes back as a new row.

## Never a question

A change for the user to check is never put in `open_for_user` and never
counted among the open questions. It goes on this list, and the user asks for
"change review" when they want it.
