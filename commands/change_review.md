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

When the user asks to review changes, open the prepared page:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py"
```

1. Check the printed link with `curl` on the publishing machine.
2. Give the link to the user.

The page contains every waiting change, with staff-based showrunner tabs.
Tabs with waiting changes precede empty tabs in alphabetical order and keep their positions until the next page load.
The selected tab draws its full outline inside its edges, including the first and last tabs.
Answers save in the background.
Each successful save opens the next waiting change in the same tab.
Navigation during a save prevents this automatic change.
Next skips a change without saving an answer.
A failed save keeps the answer, failure text, resend control, and waiting count.
Validate yourself advances the page when the request saves.
On a page load, only a sent validation message removes the row from the count and Next order.
Pending and failed validation messages keep the row waiting, with a note and a resend control.
This state survives a reload and a server restart.
A direct link shows **The showrunner will validate this** without answer buttons.
The showrunner still receives the validation message and retains the row in its own `/next` queue.
Send feedback removes the row from the page count only when its verdict saves.
Ask a question uses the one-line feedback field.
Only a sent question message removes the row from the count until the showrunner answers.
Pending and failed question messages keep the row waiting after a page load, with a note and a resend control.
Do not ask for a batch size or take page answers in chat.

The diagnostic command `show <production>` still prints the current row and count.

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

A verdict beginning `works` or `fine`, after optional `okay`/`ok` and a row id
(`c17`), lands the row. Case and later words do not affect the result; any
other start returns it to its unit. In a Hana or `~/.claude` production, the showrunner first runs the
feature-log step of `/showrunner:produce` → `<CloseChangeReview/>`; a failure
there leaves the row open. Then:

```sh
… verdict <production> --row <id> --verdict "<the user's words>"
```

Pass the ID as `/next` shows it, such as `C434` or `sdlc/C9`.
It moves the row to `## Reviewed` with the verdict, today's date and its Merged cell, and prints
`C<n> landed` or `C<n> returns to its unit (merged …)`. Either way it removes
the row's scratch folders and prints `removed <path>` for each. Send a returning row,
its change and the user's words to the unit that made it: the merge commit it
names gives the checkpoint, and so the unit. A change outside a unit goes back
to the session that added it. The fix comes back as a new row.

Rerunning the same verdict prints `already has this verdict`, the first run's
line and the scratch removal lines. A different verdict for a Reviewed row is
refused; it still ends the row's scratch folders.

## Validate

A message `Change review C<n>: the user asks you to validate this` asks the
showrunner of the row's tab to check the change in place of the user:

1. Check the change with evidence: run it, read its output, or look at the screen.
2. If it works, save the verdict
   `works (validated by the showrunner: <evidence>; not seen live: <what>)`.
3. If it does not work, save a verdict that names the fault. The row returns to
   its unit, and the fix comes back as a new row for the user.

Use the `verdict` command above for both. The row stays out of the user's count until the verdict saves.

## Question

1. Read the question and its `Question id` in the change review message.
2. Check the change or ask its unit.
3. Save the answer with this command:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" answer-question --production <doc> --list <list> --row C<n> --question <id> --text "<answer>"
```

The row returns to the user's count with the question and answer below the change.
The user can give a verdict or ask another question.
The same question ID and answer text change nothing and exit 0 on a rerun.
The command refuses an old or unknown ID, a different answer, or a row without an open question.

## Never a question

A change for the user to check is never put in `open_for_user` and never
counted among the open questions. It goes on this list, and the user asks for
"change review" when they want it.
