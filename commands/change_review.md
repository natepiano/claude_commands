---
description: Add a change for the user to check to the top-level showrunner's change review list, show the next change when the user asks for one, and record their verdicts.
---

# Change review

## Bare command

For `/change_review` with no action:

1. Run the page script:

   ```sh
   "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py"
   ```

2. Check the printed link with `curl` on the publishing machine.
3. Give the user only the link, then stop. Say nothing else.

The actions below remain for unit directors and showrunners.

Each top-level showrunner keeps one list of changes waiting for the user's
check, named for it: `~/rust/hanadocs/change-review/change-review - <showrunner>.md`,
such as `change-review - natedev.md`. A child production's rows go to its
root's list. `list_ready` adds checkpoint changes only after they reach `origin/main` and become live.
This command adds other ready changes, shows the list, and records verdicts.

Every action runs:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/change_review.py" <action> …
```

On exit 1 report its one line.

## Find the list

Every action finds the list from its production, written `<production>` below:

- A showrunner passes nothing: the script finds the production from this
  session's update timer, as `/showrunner:proposals` does. A session with no
  timer passes `--production <showrunner name>`.
- A unit director passes `--production <showrunner name>` from the parent showrunner row's `name`.

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

A row requires a change the user can check now, merged where they run it and live after rebuild, restart or deploy.
Until then, add no row.
`add --merged` refuses a hash that is not on `origin/main`.

For a change the user should check that no checkpoint `Change-Review:` line
carries: a showrunner's own change, a fix outside a run, or a change nobody
sees in the app, such as a table's columns or a status line.

```sh
… add <production> --change "<what changed for you>" --check "<how to try it: where and what to type>" --see "<what you see>" [--merged <hash>] [--urgency <level>] [--due <ISO>] [--after C<n>]
```

`--urgency` is `blocks` (work stops until the user checks it), `soon` (with
`--due`), `later` or `user`; leave it out for a normal row. `--after` names a
row that must be checked first.

One row per change, written for a person with no context.
The change description uses one line. The check and result can use several lines.
`--check` names the machine (natedev or the Mac), the session (a
terminal, a showrunner session, a unit session, any Claude session) and what to
type; `--see` is what the person then sees. Example: `--change "The Units table
shows each unit's phase" --check "In a showrunner session on natedev, type
/showrunner:units" --see "Each row has a Phase column"`. The row's How to
check cell is `<how to try it> You see: <what you see>`. Pass `--merged` when a
commit holds the change. It prints the new row's id, such as `C12`.

## list-ready and live

All merge callers use `list_ready` after the final promotion step succeeds, including the Mac pull.
It fetches `origin main` and scans merge commits from 2026-11-06.
It adds each uncovered checkpoint line, including after an interrupted call.
A row covers the same change when its merge contains that checkpoint.
A row without a known merge also covers that change.
A later fix can add a new row with the same words.
A failed fetch prints one error line and adds nothing.
The `append` and `drift` actions are removed.

```sh
… list-ready --repo <checkout> --production <showrunner>
```

A checkpoint line can end with ` | after <step>` when it needs a rebuild, restart or deploy.
The scan prints the merge, the step and the exact `live` command only for uncovered lines.
The session that completes the step runs that command:

```sh
… live --merged <hash> --step <step> --repo <checkout> --production <showrunner>
```

`live` requires both `--merged` and `--step`.
It adds only that merge's lines that wait for the named step, after the merge reaches `origin/main`.
A step with no matching line gives an error.
A repeated call adds nothing.
A hash outside `origin/main` takes the same refusal as `add --merged`.

## edit

To rewrite a row still to review, such as one that lacks where to try it or
what the person sees:

```sh
… edit <production> --row <id> --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
```

Pass the ID as `/next` shows it, such as `C434` or `sdlc/C9`.
The row keeps its id, its Merged cell and its place. It prints the new row.

## show

When the user asks to review changes, follow [Bare command](#bare-command).

The page has Questions, Changes, and Rebuilds tabs with waiting counts.
Questions and Changes show **Showrunner** before their staff-based tabs and show one card at a time.
Each type tab shows its own count. The page has no combined total.
The heading follows the type: Questions to answer, Changes to review, or Rebuilds needed.
An empty showrunner tab shows `No questions for this showrunner.` or `No changes for this showrunner.`
An empty type shows `No questions to answer.` or `No changes to review.`
The selected showrunner tab supplies the count; the card has no separate remaining count.
A question stays with the showrunner whose question list holds it.
An unknown question owner appears under **Owner unavailable**.
Owned question cards show the title before the filed text, choices, and `Asked by <session>`.
They omit the separate Question label and keep `Answer` and `Save answer`.
An **Owner unavailable** card shows `No showrunner owns this question.` without an answer form; the server refuses an answer.
Save answer records the answer and opens the next waiting question.
Skip for now skips a question without saving.
It hides when no other item waits in the selected showrunner tab.
Questions have no Ask showrunner to check or Ask a question controls.
A stale question refuses an answer if another session already answered it.
Saved question answers use the same delivery attempts, startup recovery, and failure alert as changes.
The delivery record stores `saved: false` before the answer saves and `saved: true` after it saves.
A save error clears the delivery intent and releases its claim.
Recovery sends only `saved: true` answers for closed questions.
Recovery clears other intents; a closed question without save confirmation prints `answer save state unknown; not sent`.
The relay follows the asker's current staff route; an answer without a relay goes to the question's showrunner.

Rebuilds counts machines with open needs and shows natedev before the Mac.
Each block tells the user where to run `rebuild`.
Multiple needs share one rebuild; their unchanged reasons appear under `After the rebuild`, newest first.
A closed Details disclosure shows full commits, affected staff names and kinds, and different requesters.
Unknown staff IDs appear as `Unknown session: <id>`.
The Mac block states that updates can take five minutes while the Mac is reachable.
The last line tells the user that requests clear automatically and asks them to refresh after the rebuild.
An empty view shows `No rebuilds needed.`
Rebuilds has no showrunner tabs, card navigation, Skip for now, or answer form.

The Changes tab contains every waiting change.
Each card starts with its 20px title; section labels use 17px text.
One closed Details disclosure holds the ID, date, commit, and kind, with 16px space after the title.
How to check or Proof stays in the main view.
Works and Does not work are the main actions; Ask showrunner to check and Ask a question are secondary actions.
The Does not work button submits `not works` and saves `not works: <text>` when the user supplies text.
An open Production cell holds the unit's fixed staff id or the showrunner's fixed staff id.
The reader follows a unit's current staff parent and displays current names.
Reviewed rows keep the production name recorded at the verdict.
Renames and transfers do not rewrite these cells.
The **Owner unavailable** tab shows an unknown id or a broken staff parent chain.
Verdicts save without sending a message.
This tab offers no question control, and the server refuses question submissions without saving.
Its count covers all page lists, including independent production roots.
Tabs with waiting changes precede empty tabs in alphabetical order and keep their positions until the next page load.
The selected tab draws its full outline inside its edges, including the first and last tabs.
Answers save in the background.
Each successful save opens the next waiting change in the same tab.
Navigation during a save prevents this automatic change.
Skip for now skips a change without saving an answer.
A failed HTTP save keeps the typed text, answer form, and waiting count, and shows `Not saved: <reason>`.
An automatic move after a change save shows **Review saved.** above the next card.
An automatic move after a question save shows **Answer saved.** above the next card.
Manual navigation clears this notice.
A direct link to a saved verdict shows **Saved: <answer>**.
Ask showrunner to check removes the row from every waiting count and Skip for now order when the request saves.
A direct link shows **The showrunner will validate this** without answer buttons.
Ask a question uses the **Comment or question** field.
It removes the row from every waiting count until the showrunner answers.
A direct link shows **The showrunner will answer this** without answer buttons.
The page has no Send feedback control.
Works and Does not work save the text from **Comment or question** as their comment.
The server retains its verdict rules for text that starts with `works` or `fine`.
The server manages delivery; the page shows no delivery notes, links, or resend controls.
The server reads `delivery_tries` and `delivery_pause_seconds` from `config/review_links.conf` when delivery starts.
The initial values are 3 tries and a 5 second pause.
Each round sends to all remaining recipients before one pause; later rounds retry only failed recipients.
A `QUEUED:` result counts as kept and needs no retry.
After all tries fail, the server alerts the tab's showrunner once to pass the saved answer on by hand.
The server resolves every staff recipient ID to its current session for each send.
At startup, the server scans the review lists and resumes pending records with the same retry budget.
It ignores legacy page context and reads the current owner and production document through the same function as the browser.
Recovery skips records that a live request owns.
It ignores legacy failed records and preserves validation ownership and open questions.
The router alerts `unrouted_alert_recipient` once for each unrouted row and reason.
It alerts only for a list in the real change-review folder; a list elsewhere never alerts.
It alerts only from the production staff record; a `STAFF_FILE` override never alerts.
This config key starts at `natedev`.
`unrouted_alert_retry_seconds` starts at 60 seconds and accepts 5 through 3600 seconds.
Both keys are in `config/review_links.conf` and are read when used.
Failed alerts remain pending until the configured pause ends.
A nonblocking lock prevents two renders from sending one alert at once.
The router records acceptance only after the sender accepts or queues the alert.
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
the folder uses the configured scratch root. The row's verdict removes the folder and its owned Cargo outputs.
Cargo stores final binaries in `target_directory` and intermediates in `build_directory`.
Check both output filesystems with `df -h` before a large build.

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
the row's scratch folders and prints `removed <path>` for each.
The Producer cell stays with the row in Reviewed.
Send a returning row and the user's words to its Producer staff id.
Read its current session from the staff record at send time.
An empty Producer cell belongs to the list's showrunner.
The fix returns as a new row.

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
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" answer-question --production <showrunner name> --list <list> --row C<n> --question <id> --text "<answer>"
```

The row returns to the user's count with the question and answer below the change.
The answer and cells can hold several lines; lines that start with `- ` or `1. ` show as lists.
The user can give a verdict or ask another question.
The same question ID and answer text change nothing and exit 0 on a rerun.
The command refuses an old or unknown ID, a different answer, or a row without an open question.

## Never a question

A change for the user to check is never put in `open_for_user` and never
counted among the open questions. It goes on this list, and the user asks for
"change review" when they want it.

### Producer ids

Producer retains the unit's fixed staff id for verdict delivery.
Unknown units leave Producer empty and print one named note.
For `add`, `--repo` selects the directory; the default is `.`.
A showrunner directory or an unowned directory records a production-owned change.
