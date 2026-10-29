# Production format

The shared contract for a **production**: several `/unit:direct` runs building
one deliverable in parallel, coordinated by one session. The commands sit in one
folder per role under `~/.claude/commands/`. These read or write this format and
must not drift from it:

- `/producer:greenlight` — judges whether phased plans should run as a
  production and proposes the units.
- `/producer:to_production` — splits the plans into unit plans and writes the
  production doc.
- `/showrunner:produce` — runs the production from the showrunner session.
- `/showrunner:dailies` — reports the production to the executive producer.
- `/showrunner:report_interval` — changes how often the scheduled updates come.
- `/showrunner:accounts` — shows every Claude and Codex account in one table; sets up a
  Claude folder or swaps the unit account.
- `/unit:direct` — a unit's run; it applies <ProductionUnit/> below.

---

## Words

<GuildHeads>
For Words and <ProductionUnit/> below, production `guilds` has four unit
directors with the exact IDs `designers`, `reviewers`, `showrunners`, `units`,
without `-unit`. The guilds showrunner runs on natedev under sdlc; these heads
are its units, not showrunners, and belong to the units guild. Other unit
names and worker IDs keep their current rules.

A plan headed `> **Standing:** guild-head` runs its report/evidence/tip/reply
loop and daily read instead of a phased build. It dispatches no worker, runs
no phase or gate, and makes no checkpoint or shrink. It may write and publish
its owned guild tips directly. The production row still gives its identity,
plan, worktree and ownership. It owns no assignments, acceptance, model
choices or gates; process proposals go through guilds to sdlc for
guild-process-unit. Ordinary phased and standing style contracts retain their
current behavior.
</GuildHeads>

- **Production** — the whole effort: its units, its merge branch, and its
  production doc.
- **Executive producer** — the user. Every product, scope and irreversible
  choice is theirs, and every report is to them.
- **Producer** — the session that runs the `/producer:` commands: it greenlights
  plans and splits them into a production doc and unit plans.
- **Showrunner** — the session running `/showrunner:produce`. It merges, tests, pushes,
  makes the visual choices, clears waits between units, relays the user's words
  and reports when asked. It writes no implementation code.
- **Unit** — one `/unit:direct` run on one unit plan, in its own worktree and
  branch. A unit is named `<area>-unit`, e.g. `widget-unit`. Its plan, worktree,
  branch, phases, checkpoints and ETA are the unit's.
- **Unit director** — the Claude session that runs a unit: the `/unit:direct`
  session the showrunner launches in its own herdr session. It receives messages,
  decides, writes Work Orders, dispatches workers, runs gates, checkpoints and
  reports. It writes no implementation code.
- **Worker** — a Codex worker thread the unit director dispatches. Implementation
  workers open once per run, continue across phases and repairs in whatever role
  is needed, attempt compaction above `UNIT_COMPACT_ABOVE_TOKENS` but
  continue after a failed attempt, press on after context overflow with
  `Continue where you stopped.`, open anew only when no open worker can take the
  work, and end at `end_session.sh`. Reviews run on the unit's workers, or on the
  production's review pool when its doc names one.
- **Review pool** — a production's reusable reviewers, allocated by a script.
  The first review of each type primes the pool; no unit runs it. Each reviewer
  keeps its own thread of the reviews it did, and no file holds review memory.
- **Reviewer** — a read-only Codex thread in that pool, named `code1`, `ui2`, etc.
- **Template** — a thread holding a type's standing prompt, forked into reviewers
  and never leased.
- **Lease** — one reviewer's assignment to one unit's phase, cleared at phase end.
- **Merge branch** — the branch every unit's checkpoints merge into. cargo-berth
  calls it the trunk. In prose, call it by its branch name, because a unit may be
  named `trunk-unit`.
- **Gate** — a unit phase that cannot start until another unit's checkpoint is on
  the merge branch.
- **Parent, child and root showrunner** — a production's showrunner may report
  to another production's (`/showrunner:attach`). The one at the top of a tree is
  the root: it shows every item that needs the user from the productions below
  it, and the user may still work with any showrunner directly. A child showrunner is never a
  unit: it keeps its own production doc, merge branch and units, and has no row
  in its parent's Units table. Which production reports to which is state on the
  machine, `~/.local/state/showrunner/<child slug>/parent`, and no line in either
  production doc, so either side attaches without a commit in the other's
  checkout. Look it up with `showrunners.py parent|children|root <slug>`.

User, 2026-09-28: production, showrunner and unit were chosen because Hana is a
DCC app and the film words are short.

User, 2026-10-01: the session that runs a unit is its unit director.

---

## Production doc structure

The production doc sits beside the source plan as `<source-stem>-production.md`.

```markdown
# Production — <name>

> **Status: PRODUCTION — <planned | running | wrapped>.** <one line: what it delivers>

## Production Context

- **Formerly:** `<old>`, `<older>` — previous production slugs, newest first, written by
  `/showrunner:rename`; old names keep resolving through it; omit before the first rename
- **Source plans:** <path(s)> — split on <date> by /producer:to_production
- **Repository:** <main checkout path>
- **Merge branch:** `<branch>` — every unit merges here; only the showrunner pushes it
- **Kind:** style — optional; absent means a product production. A style production
  reads `~/.claude/docs/style_production.md`
- **Branch point:** `<branch>` — style productions only: the parent's merge branch
  to sync from and hand back to
- **Parent production:** <path to parent production doc> — style productions only:
  the hand-off destination and the doc used to merge hand-offs while a root
- **Showrunner checkout:** <path, on the merge branch>
- **Showrunner session:** <name> — the showrunner's session name; it also names the production's workspace in the shared herdr session (the slug when absent)
- **Log:** <repo-relative path> — git-excluded; one line per event
- **Updates:** `on demand` (the default, also when the line is missing), or every <N>
  minutes when the user asks for a schedule; each update reports every unit in full; the
  update timer reads N from this line, `on demand` runs no timer and a dailies runs only
  when the user asks, and `/showrunner:report_interval` changes it (user, 2026-10-13: no
  timer unless asked)
- **Merge tests:** <backticked commands run in the showrunner checkout on every merge,
  besides the changed packages' tests, e.g. `./verify.sh test -p app`; text after the
  first unbackticked `(` is commentary>; `—` if none. Each command runs with no shell;
  `~` and `$VAR` expand. Its first word must be on PATH or an executable file in the
  checkout: a package name, a directory or an unset variable is refused before the merge
  starts
- **Capacity:** <cores and memory read by /producer:greenlight, and the unit count it allows>
- **UX guide:** <path to the project's UX rules, e.g. `~/rust/hanadocs/ux`>; omit when
  nothing users see changes
- **Change review:** `~/rust/hanadocs/change-review/change-review - <showrunner>.md`;
  required. One list per top-level showrunner, named for its session name, never the
  production slug (`change-review - natedev.md`); a child production names its root's
  list. `change_review.py` resolves the list from the root showrunner while it runs and
  falls back to this path when it does not, warning when the two differ.
  `merge_checkpoint.py` adds a row per `Change-Review:` line a merged checkpoint names, and
  `/change_review` adds the rest; a missing file is created on its first row with
  frontmatter (`date_created`, `date_modified` as `"[[YYYY-MM-DD]]"`, a `productions`
  list of production doc paths, `showrunner`), and every write moves `date_modified`
  and adds the writing production's doc to `productions`
- **Review pool:** code (or `code, ui`); omit when the production has none
- **Proposals:** <the issues folder the production files its proposals in, e.g.
  `~/rust/hanadocs/issues`>; `/showrunner:proposals` lists every open issue there tagged
  `proposal`; required for a style production, which files held changes there;
  otherwise omit when the production keeps none
- **Improvement queue:** <path to the improvement ledger>; optional; open lines
  still to decide or hand out show as `N projects` in the `For you:` line
- **Efficiency ledger:** <path to a nightly-review style ledger>; optional; its
  proposed and deferred lines show as `N efficiency` in the `For you:`
  line on the status line and in the dailies footer

The showrunner's session name is not in the doc: a name can change, so it is
looked up when needed. `~/.claude/scripts/lib/py
~/.claude/scripts/production/showrunners.py name <slug>` prints it as it is now.
A unit sends to the address `showrunners.py address <slug>` prints, not to the
name: the address is the showrunner's session on this machine, and `SendMessage`
refuses a name that another session of the same account also has.

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| <name>-unit | <plan path> | <path> | <branch> | <app port or —> | <dirs/files> |

The Unit value names the unit everywhere: in the log, the dailies, the waits and every
tool's arguments. A unit's session name is written nowhere, because the user may rename
a session at any time. The unit's herdr session carries the production's
slug and the unit's id as marks, set at launch. To reach a unit, look it up at that moment:
`$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/unit_lookup.py pane <slug> <unit>`
prints its pane, and `unit_lookup.py list <slug>` prints every unit's pane and its
session name now. `<slug>` is this doc's file name without `-production.md`; a former
slug from the Formerly line also works, and a mark set before a rename is found under
the current slug.

To rename a unit, run `/showrunner:rename_unit <old> <new>` from the showrunner session,
between phases. Never edit the Unit, Worktree or Branch cell by hand. The rename stops the unit,
moves its branch, worktree and state, and commits `chore(<slug>): rename unit <old> to <new>` on
the merge branch. Merge history follows that commit, so the unit's past merges still count under
the new id. Then it relaunches the unit and verifies that nothing live still holds the old name.
`nightly-review-unit` keeps its name.

To retire a unit, run `/showrunner:retire_unit <unit>` from the showrunner session. It refuses
while the unit has uncommitted work or a checkpoint not merged. It stops everything the unit owns
(its sessions, Codex workers, reviewers, holds, reservations, Mac claims and processes), marks the row
retired, and removes the worktree and branch. `--dry-run` lists all of it and changes nothing.
The retired note names the director's last session. `/showrunner:resume_unit <unit>` makes the
worktree again at the same path from the merge branch, resumes that session and makes the row live.

A retired unit keeps its row. Its Plan cell begins with `retired`, alone or inside
an opening parenthesis, as in `(retired by the user 2026-10-07, worktree removed)`.
The status script, the dailies, the waits and the stall watch then skip it. The word
anywhere else in the cell changes nothing. With a **Review pool** line, the
showrunner first runs
`~/.claude/scripts/lib/py ~/.claude/scripts/direct/review_pool.py retire --production <slug> --unit <unit>`,
which ends the unit's reviewers and prints their names. One still reviewing is
named on stderr and left in place; run `retire` again once its review ends. A
unit's plan run ending retires nothing, because the unit's next plan keeps
the same reviewer.

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | <unit> phase <N> | <unit> phase <M> | that checkpoint is on the merge branch |

## Close-out

- <production-level work after the last merge: data or saved-state migrations,
  checks on other machines, anything no single unit owns>

## Production rules

- <standing rules the user set for this production or project, each with its
  source; omit the section if none>
```

Each unit plan carries a header line under its status line, preserved verbatim by
`/plan:to_phased_plan`, `/plan:phase_review` and `/plan:shrink`:

```markdown
> **Production: <name>** — unit `<unit>`; production doc `<path>`
```

Each gated phase carries its gate directly under the phase heading:

```markdown
**Blocked by:** G<k> — <unit> phase <M> merged into `<merge branch>`
```

---

<ProductionUnit>
Applies to a `/unit:direct` run whose plan header carries a
`> **Production:**` line. Read the production doc it names at the start of the
run and after every compaction. Your row in its **Units** table gives your name,
branch, port and the files you own.

A turn with nothing for the user replies with just "—": never "No update for
you." or the like (user, 2026-10-14).

A unit's run state is written down nowhere and the unit sets nothing. Each reader takes it, when it needs it, from the records `/unit:direct` keeps of the runs in the unit's worktree: no run yet is standing by, a newest run with no finish is running, a newest run that finished is run-finished. A session stays live after its run ends, and its next run reads as running from that run's first record.
A run that stopped, failed, or waits on a decision has no finish in its record, so it does not read as finished, and nothing asks for `run done` text in the Plan cell.

1. **Your port.** Every app launch for smoke tests and shots uses your port,
   never the user's default port or another unit's.
2. **Whose words.** Text typed into your session that begins
   `From the user (via the showrunner):` is the user's instruction. A message
   from the showrunner session is a peer's. Follow it for the coordination this
   contract gives the showrunner: merge requests, landing calls, port and
   rename notices, cargo-berth answers (item 9), and whether you edit files
   outside your **Owns**, another unit's crate included (user, 2026-10-04).

   The showrunner's word also settles your designs and your add-ons. User,
   2026-10-12, to the showrunner: "when it comes to designs you absolutely
   should be able to tell units what to do and change. I do not have to be the
   one to approve". So a real design choice, a Pending decision about how
   something is built, and every pending add-on go to the showrunner, not to
   the user. Send it the question, the buildable answers and the one you
   recommend. Its answer settles it: act on it, and on a design change it
   sends unasked, without waiting for the user. Where `/unit:direct` says to
   stop and ask the user about one of these, ask the showrunner.

   Two kinds stay the user's, and no showrunner message is the user's approval
   for them: an action that cannot be undone, and a choice only the user knows
   the answer to (what the product should do, their priorities, what they
   intend). File these with `/unit:question`: it puts the question on the
   production's open-questions list, where the user finds it when they ask
   for their next question. Do not send it to the showrunner, which does not
   put it in front of the user (user, 2026-10-28). Go on with the work that
   does not wait on it, then end the turn on `— decision:`. The answer arrives
   beginning `From the user (via the showrunner):`.
3. **Commit notices.** The checkpoint hash is mergeable at once. Also send a
   notice after each final-gate and as-built commit. Timing:
   `~/.claude/docs/direct/phase_end.md` → <PhaseEnd/>.

   Before sending a checkpoint notice, record its next-phase ETA:
   `~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py eta --session-dir <SESSION_DIR> --phase-id <next> --time <HH:MM> --basis checkpoint`.
   `<HH:MM>` means today in the user's zone; an ETA on a later day takes
   `<YYYY-MM-DDTHH:MM>`. State the time the recorder printed.

   ```
   From <unit>: phase <N> checkpoint <hash> — <title>. Shots: <paths | none, no visible change>. Phase <next> ETA: <HH:MM zone>
   review trial: ux <N> findings, code <N> findings, review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>
   Parallel: <Phase N, Phase M could start | none — <what the next phase waits on>>
   design check: <pass | N defects, each moved to <phase>> — fresh check on exactly these shots, built from <hash>
   ```

   A phase checkpoint's notice carries the second line:
   `progress_history.py review-trial`'s output, verbatim
   (`/unit:checkpoint` step 9). `Phase <next> ETA` counts from the code
   checkpoint commit. The `Parallel:` line is
   `phase_table.py parallel --session-dir <SESSION_DIR>`'s output, verbatim.
   `merge_checkpoint.py` refuses `Parallel: unknown`: add the plan's missing
   `**Depends on:**` lines (`/plan:to_phased_plan` → <PhaseNumbering/>) and
   rerun.

   The shrink notice has this form:

   ```
   From <unit>: phase <N> shrink <hash> — plan doc only.
   ```

   When the commit also carries the approved next-items file, the notice ends
   `— plan doc and <next-items path>.` instead. The showrunner merges it like
   any checkpoint whose only paths are the unit's own plan files, with no
   review-ledger row and no CI count. The shrink notice carries no `review trial` or `design check`
   line.

   The as-built notice's title names what moved: each doc created, amended,
   moved or deleted, and each link repointed, this doc's **Units** row
   included. The close-out never waits on a reply.

   A phase that changes what users see, in the app or in any example,
   includes shots from a real window at
   normal size on your port. They show each changed state, including the ones
   the phase fixed. The first set covers every tool, side and state the change
   reaches, so the design check finds the defects in one batch, not one per
   round.

   Such a notice carries the third line: the verdict of the design check
   (`/unit:direct` → <UXReview/>) given on exactly the shots this notice sends
   by a fresh check (the Claude helper, or the pooled UI reviewer once the
   production has adopted it), taken from a build of `<hash>`. A verdict on
   other shots, or on a build from before a later change to what users see, is
   stale: re-shoot and re-judge before sending. Send the notice on a `pass`,
   or with each defect moved to a follow-up phase under item 7, named on the
   line. User rule 2026-10-01 (nightly review): 2 of about 25 notices carried
   a fresh verdict, and 3 holds were caught after the unit had moved on, 187
   minutes in 2.6 days; with a fresh pass the merge followed in 1–2 minutes.

   At plan start and at each <PhaseEnd/>, map which phases depend on which.
   Run each independent phase beside the current one under berth, with its own
   checkpoint (`/showrunner:produce` → Rules, parallel by default). A phase
   better run as its own unit goes to the showrunner as a proposed split
   (phases, files, shared files, merge order), never to the user.
4. **The merge branch is the showrunner's.** Never merge into it or push it.
   Push only your own branch; <PushCheckpoint/> does it at each checkpoint.
   <PeriodicCI/> and <CICleanup/> do not run in a unit; the showrunner runs CI
   on the merge branch.
5. **One more commit kind.** Besides checkpoints, a unit director merges the
   merge branch into the unit's branch at the start of every phase, before
   dispatch (user, 2026-10-16), and also when a gate clears or the showrunner asks:
   `git fetch origin <merge branch> && git merge --no-ff -m "merge(<plan-slug>): <merge branch> at <short hash> — <reason>" origin/<merge branch>`.
   Resolve conflicts on your side, build and test, then continue.
   Standing style units instead use
   `git fetch origin <style merge branch> && git merge --ff-only origin/<style merge branch>`.
   Refill waits for the previous ticket's merge, so no unmerged unit work remains
   at phase start. Report a refused fast-forward to the style showrunner as an
   error; never create a merge commit to work around it.
6. **Gates.** A phase with `**Blocked by:** G<k>` starts only once
   `git merge-base --is-ancestor <gating checkpoint> <merge branch>` succeeds.
   Worktrees share refs, so no fetch is needed. Check it yourself; a message
   saying the gate is clear is not proof. While it fails, end the turn with
   `— blocked: waiting on the showrunner: G<k> (<unit> phase <M> merged)`.
   When it passes, merge the merge branch (item 5), then dispatch. The one
   exception: the showrunner lifts a gate after item 13's test passes without
   it (`From the showrunner: G<k> lifted — …`); dispatch then.
7. **What a phase fixes** (user, 2026-09-28 and 2026-10-01). Reviews, design
   checks and smoke runs keep finding things. Two rules decide which of them
   this phase fixes before its checkpoint.
   - **Its own change only.** A phase fixes defects in what its Work Order
     delivers and regressions it caused. A defect that was there before the
     phase, or lives in another unit's code, is not this phase's: send it to
     the showrunner with its evidence, and the showrunner routes it to the
     owning unit's plan. Never widen a repair round to take it in.
   - **Landing rule.** After the phase's second repair round, or sooner when
     another unit waits on the phase or the showrunner sends a landing call, a
     finding moves to a follow-up phase when all of these hold:
     - it is new this round;
     - it is not a regression from the last fix;
     - the user would not see it in normal use;
     - no waiting unit depends on it;
     - no test or gate fails because of it.

     Finish the round in flight, write the follow-up phase with its own Work
     Order, inserted after this one per `/plan:to_phased_plan` →
     <PhaseNumbering/>, then checkpoint.
   - **Hard landing** (user, 2026-10-01: eleven repair rounds on one phase is
     way too many). From the third repair round on, a finding stays in the
     phase only when it is a regression the phase caused, breaks something
     that worked, or fails a test or gate. Every other finding moves to the
     follow-up phase, however visible, open since round 1 or found this round.
   Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. From repair round 4 onward, do not dispatch a missing target, rule interpretation or acceptance case in this phase unless evidence establishes a phase-caused regression, broken working behavior or test/gate failure. A broad original permission to fix pilot defects does not override this check. A later explicit user or showrunner ruling may keep named work; cite it. Route every other missing criterion to a follow-up Work Order in the owning unit, with its target decision, implementation, tests and acceptance evidence. Do not dispatch a batch until every finding is either retained with evidence or assigned to that follow-up. Split mixed batches; keep their eligible defects here. Never mark an unmet pilot acceptance bar passed because its work was routed.

   Work Orders state the acceptance observation, unit or comparison, and named real-path test or live check for changed behavior. Reuse established targets. Resolve choices through the showrunner before dependent work; do not invent a number to fill the form. A later changed goal updates the order explicitly. (the user, 2026-10-28)

   Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. Before any round 4-or-later dispatch on a repeated finding, require evidence that it is a phase-caused regression, broken working behavior or test/gate failure. Earlier attempts and an open verdict alone are not that evidence. Broad original permission for pilot fixes does not override landing. Cite any later explicit user/showrunner ruling that keeps the named work. Route a repeated finding outside these categories to its owning unit's follow-up Work Order before another repair starts. Keep the finding open and link its checks, previous attempts, due checkpoint and final acceptance result. Split mixed batches and retain all eligible work. If eligibility is unknown, diagnose it rather than silently route or count it as a certain saving.

   For retained work after two completed failed fixes, record the failure, approaches tried, cause diagnosis and next distinguishing test before another approach. Interrupted workers and account moves remain continuations, not completed rejected patches. (the user, 2026-10-28)

   At each fix limit, try something different. When a phase reaches repair round 4 with a finding still open, or a kept finding has failed two fixes, the director writes a short note: the finding, the tries so far and why each failed. It runs `/astra_review` on the note. Astra's approach goes into the next try, or into the follow-up Work Order when the finding moves out (the user, 2026-10-28).

   Use `findings.py route` to move a finding out of the batch and `dispatch --keep F003=<regression|broken|gate|ruling>:<evidence>` for each kept finding from repair round 4. Record Astra's result with `findings.py astra`; it is due at round 4 or two failed fixes, again at each new fix-attempt count. Record `--unavailable` if Astra reports busy or unavailable (the user, 2026-10-28).

   - **One batch per check** (2026-10-01, measured: late repair rounds came
     mostly from one visual defect found live after each fix). Every live
     check or set of shots after a fix covers every view the phase changes,
     and all its findings go to the next round together. Never send one
     defect, fix it, then look again.
   - **A fix that fails twice** (2026-10-01, measured: one bar-fit finding
     failed four times before the fifth attempt held). When the same finding
     is still open after two fixes, send no third worker pass with the same
     approach. First find why the fixes did not hold, write it in the
     attempts log, then dispatch once with an approach that addresses that
     cause. Apply the Astra step above before that next approach (the user, 2026-10-28).

   Before the hard landing, everything else is fixed before the checkpoint. This is packaging under
   `<DecisionEconomy/>`, so it never goes to the user as a question. State it
   in one line.
8. **Changes from other units.** When the showrunner reports that a public item
   was renamed or removed on the merge branch, with sites in your branch or
   worktree, merge the merge branch before your next checkpoint. Fix those
   sites with the named replacement and test. When the showrunner says another
   unit restructured files you have uncommitted edits in, merge the merge branch
   and port your edits into the new layout. Build on the new types, never
   parallel copies of them.
9. **Files you do not own.** Edit another unit's files or hub files only through
   cargo-berth's normal flow. An incursion resolves itself. The showrunner picks
   each overlap answer, and no berth decision waits on the user
   (`/unit:direct` <BerthDecisions/>). Name each such file in your checkpoint
   notice as `also touches <path> (owner <unit>), tested against <owner tip>`.
   Before the notice, on a clean tree, trial-merge the owner's current tip in
   your own worktree (`git merge --no-commit --no-ff <owner tip>`), run the
   tests that cover those files, then `git merge --abort`; never commit the
   trial. Fix every break on your side. A break only the owner's in-flight
   work can fix goes to the owner before the notice: what breaks and who fixes
   it, named on the line. The owner never absorbs your break in a repair
   round. User, 2026-10-06.
   Correcting an as-built doc under `docs/as-built/` that contradicts the
   code is always allowed, in any unit's doc, without asking the user, the
   showrunner or the owner; the checkpoint notice still names the file as
   `also touches`. User, 2026-10-07.
10. **Turn-end lines.** A wait the showrunner can clear names it:
    `— blocked: waiting on the showrunner: <what>`. A wait on another unit is
    one of these: `— blocked: waiting on the showrunner: <unit> <what>`. A wait
    on the user never mentions the showrunner, so the showrunner's status script
    can tell the two apart.
11. **Updates.** Each progress update is the full `/unit:report`, as outside a
    production (user, 2026-10-06). Every time in it is in the user's zone
    (user, 2026-10-04): the recorder reads the zone setting itself, so pass
    no `TZ=`. An older production doc may carry a **User zone** line; nothing
    reads it. When the showrunner asks for `/unit:eta`, run it at once.
12. **Visual choices go to the showrunner.** Wording, spacing, layout, and
    which of two working options looks better are the showrunner's calls, made
    from the **UX guide**. Ask the showrunner, never the user. The showrunner
    alone brings a taste question to the user.
13. **Waiting on another unit** (user, 2026-09-29). Only code you need that
    exists only in another unit's unmerged work blocks you. Reservations, shared
    files and "to avoid conflicts" do not; the showrunner clears those and names
    the cargo-berth commands you run. Before reporting a code block, test it: in
    a scratch worktree, merge the merge branch without that work and run your
    tests. Green means you are not blocked; continue. A real block ends the turn
    with item 10's line. While it lasts, take the work the showrunner names, or:
    parts of your next phase in files nobody holds, your fix built in a scratch
    copy, tests, docs or research.
15. **A failed lint** (user, 2026-10-01: 12 lint runs in a row on an
    unchanged tree failed on the same error; 60 failed lints in one day cost
    about 75 minutes). Lint covers the whole workspace. When it fails, fix
    the error it names, wherever it is in the workspace, then lint once.
    Never re-run lint on a tree that has not changed. A Work Order runs lint
    once after the worker's edits, never once per crate; per crate is right
    only for test (`/unit:direct`). Fix a file you do not
    own through item 9, and name it in your checkpoint notice. Tell your workers
    this in every Work Order that runs lint.
14. **When others wait on you** (user, 2026-09-29). When your checkpoint turns
    the merge branch red, in CI or lint, send a small fix checkpoint at once,
    ahead of your phase work. Checkpoint at your next green
    point, with polish unfinished if need be. If only part of your work is
    needed, checkpoint that part first. A regression never lands: gates pass,
    and the shots are no worse than the merge branch. A fix the showrunner asks
    for comes after that checkpoint, unless the waiting unit needs it. This is
    packaging under `<DecisionEconomy/>`; state it in one line.
16. **Review pool.** With a `**Review pool:**` line in the production doc, send
    every code review through
    `review.sh --pool code --lease <slug>/<unit>/<phase>` with the usual
    positional arguments. The unit's reviewer is permanent: each review
    resumes its thread, and the pool forks a second one only while two reviews
    run at once. `<slug>` is the production doc's file name without
    `-production.md`. A refusal `no primed code template: <cause>` means the
    pool could not prime itself: send that review to an idle worker with
    `--to`, and tell the showrunner the cause once.

17. **Proposals.** A finished proposal you write is a hanadocs issue, filed
    through `/issue` with tag `proposal`, `proposed_by` your unit's name, and
    its five ranking values proposed on a `Proposed ranking:` line. Send the
    showrunner its path and one line; there is no proposals index. When the
    showrunner sends back an approved one, write its work into your plan as
    phases that cite it and add it to the plan's `> **Issues:**` line; your
    run marks it active and its end-of-run as-built pass closes it
    (`/showrunner:proposal`).
18. **Change review.** Every change the user can see or check carries a
    `Change-Review:` line in its checkpoint (`/unit:checkpoint`). A change
    outside a checkpoint goes on the list through `/change_review add`. Never
    ask the user to check something directly.
19. **Builds for the user.** Before a build goes to the user's machine, run
    `assertion_guard.py --base main` in that repository (`/unit:direct`
    <UserBuildGuard/>). Exit 1 or 2 stops the hand-off.
</ProductionUnit>
