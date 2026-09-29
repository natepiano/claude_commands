---
description: Run a production as its showrunner — launch each unit's /unit:delegate session, merge and test every checkpoint on the merge branch, check visible work in screenshots before merging, clear waits between units, relay the user's words, and report each unit's ETA on a schedule.
---

# Produce

The showrunner owns the merge branch, coordination between units, the design
check, and all talk with the user. Units write the code. The showrunner writes
none, and that includes merge conflicts: the unit whose change conflicts
resolves it on its own branch.

**Usage:** `/showrunner:produce <production-doc> [resume]`

Read `~/.claude/docs/production_format.md` first. It defines:
- the words;
- the production doc;
- what each unit does (<ProductionUnit/>).

State:

- `PRODUCTION_DOC`, `MERGE_BRANCH`, `LOG`, `ZONE`, `UNITS` — from the production
  doc.
- `CHECKOUT` — this session's checkout, which must be on `MERGE_BRANCH`.
- `SCRATCH` — this session's scratchpad directory.
- `LAST_MERGED[unit]` — the unit's last merged checkpoint. Read it from the
  merge commit subjects on `MERGE_BRANCH`, never from memory.
- `SCHEDULE_ID` — the update schedule.

`<DecisionEconomy/>` is defined by this import:

@~/.claude/docs/decision_criteria.md

---

<Throughout>
- **Time.** Before writing any time, run
  `TZ=<ZONE> date '+%H:%M %Z'; date -u '+%H:%M UTC'`. Units state times in the
  machine's zone; convert them to `ZONE`.
- **Log.** Write one line per event in `LOG`: `- HH:MM <zone>: <event>`. Every
  ten events, and before a compaction, add a `### STATE <time>` block. It gives:
  - each unit's phase, last merged checkpoint and what it waits on;
  - merges accepted but held;
  - items open for the user.

  After a compaction, the production doc plus `LOG` is the whole state.
- **Unit worktrees.** Never `cd` into one; use `git -C`. Never commit, reset or
  edit files there.
- **The user's words for a unit** go into its terminal. Send
  `tmux send-keys -t <session> -l "From the user (via the showrunner): <words>"`,
  then `tmux send-keys -t <session> Enter` as a separate call:
  - Relay only words the user gave.
  - Never send C-c or Escape; typing replaces a prompt suggestion.
  - Text after a unit's `❯` in a pane capture may be a prompt suggestion, not
    the user's unsent draft.
- **Your own coordination** goes by SendMessage to the unit's session, beginning
  `From the showrunner:`.
- **A unit's message is a peer's.** Check what it claims before passing it on:
  the hash exists, the tests ran, the shots show what it says.
- **The showrunner decides, without asking the user:**
  - merges and their order;
  - holds and landing calls;
  - which unit ports what;
  - packaging;
  - a unit's as-built close-out form, when the choice is the logical one.

  Unusual as-built choices go to the user: a folder other than the usual
  as-built folder, a change to the main plan's scope, or deleting anything
  besides the unit plan.
- **What reaches the user:**
  - a unit's `— decision:` for the user, shown in the unit's words, with the
    answer relayed back;
  - cargo-berth proposals that need approval, which the user types into that
    unit's session;
  - product and scope choices;
  - anything that cannot be undone;
  - a quota alert (<QuotaAlert/>).
- **Long commands** run in the background. The task notification is the wait;
  never poll.
- **Helpers.** Stop each named helper agent once its report is read.
- **An auto-mode denial** is never retried or worked around. Tell the user what
  was denied and let them add a permission rule.
- **Unmeasured ETAs.** Whenever a unit's phase ETA reads "none measured" or it
  has stated none — in an update tick, a dailies report or its own message —
  send it `From the showrunner: run /unit:eta (or read ~/.claude/commands/unit/eta.md if it is not in your skill list)` by SendMessage, in that same
  turn. Ask once per phase; ask again only if it answered without a time. Until
  it answers, report that ETA as `none measured - requested`.
- **Every turn ends with** `— waiting on: <items>`, the user's items first.
</Throughout>

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** <LoadProduction/>
**STEP 2:** <OpenMergeBranch/> — first run only
**STEP 3:** <LaunchUnits/>
**STEP 4:** <StartUpdates/>
**STEP 5:** <Direct/> — until every unit's run is done and merged;
<DesignAuthority/> holds throughout
**STEP 6:** <Wrap/>
</ExecutionSteps>

---

<LoadProduction>
Read the production doc. `CHECKOUT` must be on `MERGE_BRANCH`, or, when that
branch does not exist yet, clean on the commit it will start from. Otherwise
stop with `Run /showrunner:produce in a checkout on <merge branch>.`

On `resume`, or when the doc's status is `running`:
1. Read `LOG` from its last `### STATE` block.
2. Rebuild `LAST_MERGED` from
   `git -C CHECKOUT log --first-parent --format='%H %s' MERGE_BRANCH`,
   using the `Merge <unit> phase <N> (<hash>)` subjects.
3. Check each unit's session with `tmux has-session`.
4. Run <StartUpdates/> again. Schedules end with this session and expire after
   7 days.
</LoadProduction>

---

<OpenMergeBranch>
Only when the doc's status is `planned`:

1. If `MERGE_BRANCH` does not exist, create it in `CHECKOUT` from its current
   commit: `git -C CHECKOUT switch -c <merge branch>`. The production doc
   authorizes this one branch.
2. Set the doc's status to `running`.
3. Commit the production doc and every unit plan as
   `production(<name>): plans for <n> units`, and push `MERGE_BRANCH` with its
   upstream set.
4. Add `LOG` to `$(git -C CHECKOUT rev-parse --git-common-dir)/info/exclude`,
   then create it with a `# Production log — <name>` heading.
</OpenMergeBranch>

---

<LaunchUnits>
For each unit without a live session:

1. **Worktree.** If it is absent, run
   `git -C CHECKOUT worktree add <worktree> -b <branch> <merge branch>`. When the
   repository has `.claude/config/berth.toml`, also set
   `git -C CHECKOUT config branch.<branch>.cargoBerthTarget <merge branch>`, so
   cargo-berth measures the unit against the merge branch.
2. **Session.** Always use detached tmux. The unit then outlives this session,
   runs while the screen is locked, and the showrunner can type into it. When a
   unit is blocked on a full context, type `/compact` into it with
   `send-keys -l`, then `Enter`. First capture the pane to check the block is
   still showing and no compaction is already running, since the user may have
   typed it already.
   - tmux is `command -v tmux`, or else
     `$(nix build --no-link --print-out-paths 'nixpkgs#tmux^out')/bin/tmux`.
   - Start it through `systemd-run --user --scope --unit=<session>`, so it lives
     outside this session's scope.
   - Remove every `CLAUDE_*` variable from its environment. An inherited
     `CLAUDE_CODE_CHILD_SESSION` turns off transcript saving.
   - Launch:
     `tmux new-session -d -s <session> -c <worktree> zsh -ic "ENABLE_TOOL_SEARCH=true command claude --remote-control <session> -n <session> --settings '{\"disableAgentView\": true}' '/unit:delegate <unit plan>'; exec zsh"`
3. **Check.** Log the launch only after the pane shows `/remote-control is
   active`. The mobile session list lags by minutes; trust the pane.
4. **Resume.** To bring back a unit whose session ended, use
   `claude --resume <session-id> --remote-control <session> -n <session>`, which
   keeps its link and its place in the list.

Tell the user one line per unit: its session name, and `tmux attach -t <session>`.
</LaunchUnits>

---

<StartUpdates>
Create a recurring schedule (CronCreate) every N minutes, where N is the
production doc's **Updates** interval (15 when the doc does not give one),
offset from the hour: for 15, `3-59/15 * * * *`. Fill this prompt from the
production doc:

> Scheduled update (every <N> minutes, every unit in full; the user is in
> <zone>). Run `zsh ~/.claude/scripts/production/unit_status.sh <SCRATCH>/unit_status <zone> <sessions…> | cut -c1-400`.
> It checks every unit: its session and Claude are running, anything waiting
> on the user, and its latest step and ETA. Then give the user
> `/showrunner:dailies simple` for every unit and open topic. If the script
> shows SESSION GONE, CLAUDE NOT RUNNING, FORM WAITING, a usage limit, or a
> DECISION for the user, that subject goes first, with `needed:` saying what
> the user must do. Do no other work in this turn, except `/unit:eta` requests,
> merging a unit's checkpoint after viewing its shots, and acting on a BLOCK
> past its limit (`/showrunner:produce` → Dependencies, rule 4).

**Every scheduled update is a `/showrunner:dailies simple` report**, never a
one-unit note: the user sees every unit on every tick, each checked in full.

A `/showrunner:dailies` the user runs takes the next tick's slot: it runs the
script, and then restarts this schedule so the next tick comes N minutes after
that report (`/showrunner:dailies` → Status check and clock).

Log `SCHEDULE_ID`. Then check that this session is on the quota alert list
(<QuotaAlert/>).

Each run of the script does two things:
- It scans every unit for a form or decision waiting on the user.
- It reports the next unit in turn.

A block that names the showrunner or another unit is not a wait on the user.
The script prints it as `BLOCK in <unit>, open <age>: <text>`, the age counted
from the first run that saw it. Clearing it is your job (<Dependencies/>).
</StartUpdates>

---

<Direct>
Turns come from unit messages, update ticks and the user. Handle whatever
arrived:

| Arrival | Action |
| --- | --- |
| a checkpoint notice | <MergeCheckpoint/> |
| an update tick | the schedule prompt only, plus any merge whose shots are viewed and any BLOCK past its limit |
| the user's words for a unit | relay them (<Throughout/>) |
| a unit waiting on another unit | <Dependencies/> |
| a unit blocked on the showrunner | <ClearGate/>, <LandingCall/>, or answer it |
| a unit's decision for the user | show it to the user; relay the answer |
| a quota alert | <QuotaAlert/> |
| the user asks for a status | `/showrunner:dailies`, `simple` unless they name a length |

Merge one checkpoint at a time. A notice that arrives while a merge is testing
waits its turn. When every unit's final checkpoints are merged, go to <Wrap/>.
</Direct>

---

<MergeCheckpoint>
Input: the unit, phase, hash and shots from its notice.

1. **Ancestry.** `git -C CHECKOUT cat-file -e <hash>^{commit}` must succeed.
   `git -C CHECKOUT merge-base --is-ancestor <LAST_MERGED[unit]> <hash>` must
   succeed, so nothing merged before is dropped. A unit's first merge has no
   `LAST_MERGED` and skips this check.
2. **Scope.** `git -C CHECKOUT diff --name-only <merge branch>...<hash>` lists
   the unit's changes. Every path must be:
   - in the unit's **Owns**;
   - one of its hub files;
   - or named in the notice as `also touches`.

   For anything else, ask the unit why, and hold the merge until it answers.
3. **Conflicts.**
   `git -C CHECKOUT merge-tree --write-tree --name-only <merge branch> <hash>`
   exits 1 on a conflict. On a conflict:
   - the unit merges the merge branch, resolves the conflict on its side, and
     sends a new hash;
   - do not merge the old one.
4. **Other units.** For each other unit, compare this change's paths with:
   - its branch, `git -C CHECKOUT diff --name-only <merge branch>...<its branch>`;
   - its uncommitted edits, `git -C <its worktree> status --short`.

   If this change moves, splits or deletes files the other unit has in flight,
   apply <CrossUnitChange/> step 2 before merging. If the overlap is only an
   edit to the same file, tell that unit in one line which file changed under
   it.
5. **New public items.** Each new `pub` item in the diff needs a user in
   production code. One with no consumer goes back to the unit as a finding.
6. **Design check.** A change users can see, in the app or in any example,
   needs shots. Run
   <DesignCheck/>. Send each defect back to the unit with the shot path, the
   rule and the fix, and do not merge. Never ask the user whether a visible
   defect matters.
7. **Merge.** Write the message to `<SCRATCH>/merge_<short hash>.msg`:

   ```
   Merge <unit> phase <N> (<short hash>) into <merge branch>

   <one line: what the phase delivers>

   <the attribution lines this session's commits require>
   ```

   Then run `git -C CHECKOUT merge --no-ff -q -F <msg file> <hash>`.
8. **Test.** In the background, test each package that owns a changed file (its
   nearest `Cargo.toml`), plus the doc's **Merge tests**, one after another:

   ```sh
   log=<SCRATCH>/merge_<short hash>_test.log; : > $log
   for p in <packages>; do
     bash ~/.claude/scripts/delegate/verify.sh test $p >> $log 2>&1
     print -r -- "${p}_EXIT=$?" >> $log
   done
   ```

   When the notification arrives, read the `_EXIT` lines once.
9. **Red.** Rerun each red package once, alone.
   - Green alone, and memory names it as a known load-sensitive flake: log it
     and continue.
   - Otherwise, confirm that `HEAD` is this unpushed merge and undo it with
     `git -C CHECKOUT reset --keep HEAD~1`. Send the unit the failing tests and
     the log path.
10. **Green.** Push with `git -C CHECKOUT push origin <merge branch>`. Never
    force-push. Log it:
    `- HH:MM <zone>: <unit> phase <N> (<hash>) merged as <merge hash>; <packages> green; pushed`.
11. **After the push:**
    - run <ClearGate/> for any gate this checkpoint clears;
    - run <CrossUnitChange/> when the change renamed or removed public items,
      or restructured files;
    - after every fifth merge since the last CI point, run <CIPoint/>.
</MergeCheckpoint>

---

<DesignAuthority>
The showrunner makes the production's visual choices: wording, spacing,
layout, and which of two working options looks better. The bar is the user's:
everything users see looks as professional and polished as it can be. The UX
guide named in the production doc holds the rules that make the bar concrete.

- Units bring visual choices to the showrunner, not to the user.
- When the guide answers a choice, apply it. When it does not and one option
  is clearly more polished, pick that one and state it in one line.
- A choice only the user's taste can settle goes to the user once. Write the
  answer into the guide as a new rule the same turn, with `source:` quoting
  the user, so no one asks again.
- A flaw found in one unit's shots is a flaw to look for in every unit's
  surfaces. Send it to each unit that has it.
</DesignAuthority>

---

<DesignCheck>
Screenshots and the guide never load into the showrunner's own context. For
each checkpoint with shots, spawn a fresh helper agent with this prompt:

> Read `~/.claude/commands/ux_eval.md` and follow it for these shots:
> <paths>. Guide: <UX guide path>. Context: <unit> phase <N> — <what
> changed, and every state the shots must show>. Also apply these production
> rules: <the doc's Production rules that concern looks>. Return only the
> verdict and the table.

Read its verdict, then stop the helper. `pass` lets the merge go on. Send
each defect to the unit. A `no rule` defect is still a defect. If it is a
choice the user's taste must settle, apply <DesignAuthority/>; otherwise add
the rule to the guide.
</DesignCheck>

---

<CIPoint>
Validation needs a clean tree, so merge nothing while it runs. With
`dangerouslyDisableSandbox: true`, run in the background:

```sh
bash ~/.claude/scripts/validate_and_push/validate_and_push.sh \
  --to "<merge branch>" \
  --fix-commit "ci(<name>): validation fixes after <unit> phase <N>"
```

Then watch its run with
`gh run watch <run-id> --repo <repo> --exit-status`, also in the background.
Red CI goes to the unit that owns the failing files. It fixes the failure as
its next checkpoint, and you merge that as usual. Log each point and its
result.
</CIPoint>

---

<ClearGate>
When a merged checkpoint is what a gate waits on, SendMessage the waiting unit:

`From the showrunner: G<k> clear — <unit> phase <M> is on <merge branch> as <merge hash>. Merge <merge branch> and continue.`

The unit checks this in git itself before it continues.

When a gate's test under <Dependencies/> rule 1 passes without the gating
checkpoint, lift the gate instead:

`From the showrunner: G<k> lifted — your tests pass without <unit> phase <M> (<log path>). Continue.`
</ClearGate>

---

<Dependencies>
Apply the user's rules (2026-09-29). A unit waits on another unit only for code
it needs. Every other wait is yours to clear, and fast.

1. **Only missing code blocks a unit.** When a unit reports a wait on another
   unit, name in that turn what it needs:
   - **Code:** a function, fix or behavior that exists only in the other unit's
     unmerged work. Only this is a block. A gate is a code block the producer
     planned.
   - **Files:** cargo-berth reservations or shared files, with no code needed.
     Rule 3 clears it.
   - **Preference:** "to avoid conflicts", "to build on their version". Never a
     block; tell the unit to continue.

   Before accepting a code block, have the waiting unit test it: in a scratch
   worktree, merge `MERGE_BRANCH` without the other unit's work and run its
   tests. Green means it is not blocked: tell it to continue, or lift the gate
   (<ClearGate/>).
2. **The unit waited on lands what is needed now.** Send it:

   `From the showrunner: <waiting unit> waits on your <what>. Checkpoint at your next green point; if only part is needed, checkpoint that part first.`

   It checkpoints with polish unfinished. A regression never lands: gates
   pass, and the shots are no worse than `MERGE_BRANCH`. Polish not yet done is
   not a regression.

   Before any <LandingCall/> or fix-first request, list who waits on that
   checkpoint and for what. Unless a waiting unit needs that fix, the unit
   checkpoints first and fixes after.
3. **File waits are your call.** Take the first option that works, tell each
   unit what to run, and log the call. Each unit runs its own cargo-berth
   commands.
   1. The holding unit checkpoints what is green; merge it, and its
      reservations release.
   2. Release in batches: the holder releases what it is done with, you merge,
      the waiting unit starts on those files, repeat.
   3. Both units work in the same files under a berth ordering; whoever lands
      second resolves the conflict.
   4. The waiting unit moves its work to files nobody holds.
4. **Waits have a limit.** Log each wait when it starts and when it clears:
   - `- HH:MM <zone>: block: <waiting unit> on <unit> (<code | files>: <what>), clears ~HH:MM`
   - `- HH:MM <zone>: block cleared: <waiting unit> on <unit>`

   At 30 minutes past its clear time, or one hour open without movement, act
   under rules 2 and 3 in that turn, an update tick included, and log the call.
   A longer wait needs a logged reason. In the dailies, the waiting unit's
   `update` names the wait with its start and clear times.

   While it waits, give the waiting unit other work: parts of its next phase in
   files nobody holds, its fix built in a scratch copy, tests, docs or research.
5. **Two things still go to the user:** a wait that clears only by changing what
   ships, and approvals that belong in a unit's own session.
</Dependencies>

---

<LandingCall>
Apply the user's rule (2026-09-28). A unit may wait on code in another unit's
phase that has been through more than two repair rounds. First apply
<Dependencies/> rule 2: the unit checkpoints what is green now, before more
repair, unless the waiting unit needs the fix in repair.

Otherwise, read the gating unit's pane and the findings files in its delegate
session directory. If its latest round fixes only new edge cases, as the
landing rule in <ProductionUnit/> defines them, send:

`From the showrunner: <waiting unit> waits on your phase <N> (G<k>). Apply the landing rule: finish this round, move the new edge cases to a follow-up phase, checkpoint.`

Defects users can see are never deferred. Log the call.
</LandingCall>

---

<CrossUnitChange>
1. **Renamed or removed public items.** Search every other unit for uses of
   the old name:
   - its branch, with `git -C CHECKOUT grep -n <old> <branch>`;
   - its worktree, with grep under the source directories.

   Message each unit that has uses. Give the exact sites and the replacement,
   and ask it to merge the merge branch and fix them before its next notice.
   That way its merge stays clean.
2. **Files restructured under another unit's edits.** This applies when one
   unit's change moves, splits or deletes files that another unit has edits in
   flight on:
   1. Merge the restructure, so the new layout is fixed at one commit.
   2. Hold the restructuring unit's later checkpoints that touch those files.
      Accept them but do not merge them until the other unit lands, and log
      each hold.
   3. Tell the other unit to merge the merge branch and port its edits into the
      new layout. It builds on the new types, never parallel copies of them.
   4. When that port is merged, release the held checkpoints. The restructuring
      unit merges the merge branch, reconciles, and sends a new hash. Live
      checks and shots for both run on the combined tree.
</CrossUnitChange>

---

<QuotaAlert>
Units' delegates run on accounts with a weekly usage limit. The notices about
them, how to tell the three kinds apart, and what a receiver does are in
`~/.claude/docs/quota_alerts.md`. Read it at <StartUpdates/> and follow it; this
section adds only what the showrunner role needs.

**The list.** At <StartUpdates/>, check that this session's name, as ListAgents
gives it for "This session is", is in the `notify` list the doc names. The user
keeps the list: if the name is missing, tell them once; never edit the file.

**The units act through you.** They are not on the list, so each notice reaches
them only as your relay (<Throughout/>):
- `Quota alert:` — tell every unit to start no new delegate work on that tool;
  running seats finish and the unit does the rest itself. Hold the alert as one
  item per account, listed first in every `— waiting on:` with the percent left
  and the reset time, until it is acknowledged or restored.
- `Quota alert acknowledged:` — drop the held item. Paused work stays paused.
- `Quota restored:` — tell every unit delegation on that tool can resume, and
  drop the held item.

What to do about the quota itself is the user's call; relay their decision to
every unit.
</QuotaAlert>

---

<Wrap>
When every unit's final-gate and as-built checkpoints are merged:

1. Run a final <CIPoint/>.
2. Work through the production doc's **Close-out** items in order:
   - an item the **Production rules** pre-approve runs as written;
   - any other item that cannot be undone gets the user's OK first;
   - back up user data before migrating it.
3. For each unit, check that its worktree is clean
   (`git -C <worktree> status --short` is empty) and that its branch is merged
   (`git -C CHECKOUT branch --merged <merge branch>` lists it). Then:
   - retire any cargo-berth reservation the worktree still holds;
   - `git -C CHECKOUT worktree remove <worktree>`;
   - `git -C CHECKOUT branch -d <branch>`.

   Leave the tmux sessions; the user closes them.
4. Delete the update schedule.
5. Set the doc's status to `wrapped`, commit it as
   `production(<name>): wrapped`, and push.
6. Report:

   ```markdown
   | Area | Result |
   | --- | --- |
   | Units | <unit>: <n> phases merged, last <hash>; one row per unit |
   | Merge branch | `<branch>` at <hash>, pushed |
   | CI | <last point: green / red → repaired in <hash>> |
   | Close-out | <each item: done / waiting on you> |
   | Next | <what is left for the user, e.g. merging `<merge branch>` into the default branch> |
   ```
</Wrap>

---

## Rules

- The showrunner writes no implementation code, tests or Work Orders in a unit's
  files.
- Only the showrunner pushes the merge branch, and never with force.
- Merge only from a checkpoint notice. Never merge a visible change before
  viewing its shots.
- Never ask the user to review until <DesignCheck/> passed on the shots.
- Never pass on a unit's claim without checking it.
- Updates hold only what the schedule prompt allows.
