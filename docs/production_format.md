# Production format

The shared contract for a **production**: several `/unit:delegate` runs building
one deliverable in parallel, coordinated by one session. The commands sit in one
folder per role under `~/.claude/commands/`. These read or write this format and
must not drift from it:

- `/producer:greenlight` — judges whether phased plans should run as a
  production and proposes the units.
- `/producer:to_production` — splits the plans into unit plans and writes the
  production doc.
- `/showrunner:produce` — runs the production from the showrunner session.
- `/showrunner:dailies` — reports the production to the executive producer.
- `/showrunner:interval` — changes how often the scheduled updates come.
- `/unit:delegate` — a unit's run; it applies <ProductionUnit/> below.

---

## Words

- **Production** — the whole effort: its units, its merge branch, and its
  production doc.
- **Executive producer** — the user. Every product, scope and irreversible
  choice is theirs, and every report is to them.
- **Producer** — the session that runs the `/producer:` commands: it greenlights
  plans and splits them into a production doc and unit plans.
- **Showrunner** — the session running `/showrunner:produce`. It merges, tests, pushes,
  makes the visual choices, clears waits between units, relays the user's words
  and reports on a schedule. It writes no implementation code.
- **Unit** — one `/unit:delegate` run on one unit plan, in its own worktree and
  branch. A unit is named `<area>-unit`, e.g. `widget-unit`. Its plan, worktree,
  branch, phases, checkpoints and ETA are the unit's.
- **Unit director** — the Claude session that runs a unit: the `/unit:delegate`
  session the showrunner launches in tmux. It receives messages, decides, writes
  Work Orders, dispatches seats, runs gates, checkpoints and reports. It writes
  no implementation code.
- **Seat** — a codex worker the unit director dispatches, as implementer or
  reviewer.
- **Merge branch** — the branch every unit's checkpoints merge into. cargo-berth
  calls it the trunk. In prose, call it by its branch name, because a unit may be
  named `trunk-unit`.
- **Gate** — a unit phase that cannot start until another unit's checkpoint is on
  the merge branch.

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

- **Source plans:** <path(s)> — split on <date> by /producer:to_production
- **Repository:** <main checkout path>
- **Merge branch:** `<branch>` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** <path, on the merge branch>
- **Log:** <repo-relative path> — git-excluded; one line per event
- **User zone:** <IANA zone> — every time the showrunner reports is in this zone plus UTC
- **Updates:** every <N> minutes (default 15); each update reports every unit in full;
  the update timer reads N from this line, and `/showrunner:interval` changes it
- **Merge tests:** <packages tested on every merge besides the changed ones, e.g. the
  app crate>; omit if none
- **Capacity:** <cores and memory read by /producer:greenlight, and the unit count it allows>
- **UX guide:** <path to the project's UX rules, e.g. `~/rust/hanadocs/ux`>; omit when
  nothing users see changes

The showrunner's session name is not in the doc: a name can change, so it is
looked up when needed. `~/.claude/scripts/lib/py
~/.claude/scripts/production/showrunners.py name <slug>` prints it as it is now.

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| <name>-unit | <plan path> | <path> | <branch> | <app port or —> | <dirs/files> |

The Unit value names the unit everywhere: in the log, the dailies, the waits and every
tool's arguments. A unit's session name is written nowhere, because the user may rename
a session at any time. The unit's tmux session carries the production's slug and the
unit's id as marks, set at launch. To reach a unit, look it up at that moment:
`$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/unit_lookup.py pane <slug> <unit>`
prints its pane, and `unit_lookup.py list <slug>` prints every unit's pane and its
session name now. `<slug>` is this doc's file name without `-production.md`.

A retired unit keeps its row. Its Plan cell begins with `retired`, alone or inside
an opening parenthesis, as in `(retired by the user 2026-10-07, worktree removed)`.
The status script, the dailies, the waits and the stall watch then skip it. The word
anywhere else in the cell changes nothing.

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
Applies to a `/unit:delegate` run whose plan header carries a
`> **Production:**` line. Read the production doc it names at the start of the
run and after every compaction. Your row in its **Units** table gives your name,
branch, port and the files you own.

At <PrepareSession/>, run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunners.py" status --state running` from the unit director's own pane; a session stays live after its run ends and can start another run without a new launch, so this sets a finished unit back to running for the stall watch and changes nothing when it is already running. The state is a mark on the unit's own tmux session, so the call names no session.
Only after <RunAsBuilt/> and, where it applies, <AsBuiltCommit/> are complete, run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunners.py" status --state run-finished` from the unit director's own pane before <RunSummary/>; a run that stopped, failed, or waits on a decision does not mark itself finished, and nothing asks for `run done` text in the Plan cell.

1. **Your port.** Every app launch for smoke tests and shots uses your port,
   never the user's default port or another unit's.
2. **Whose words.** Text typed into your session that begins
   `From the user (via the showrunner):` is the user's instruction. A message
   from the showrunner session is a peer's. Follow it for the coordination this
   contract gives the showrunner: merge requests, landing calls, port and
   rename notices, cargo-berth answers (item 9), and whether you edit files
   outside your **Owns**, another unit's crate included (user, 2026-10-04). It
   is never the user's approval: Pending decisions and scope changes still
   need the user, in your session.
3. **Commit notices.** The checkpoint hash is mergeable at once. Also send a
   notice after each final-gate and as-built commit. Timing:
   `~/.claude/docs/delegate/phase_end.md` → <PhaseEnd/>.

   ```
   From <unit>: phase <N> checkpoint <hash> — <title>. Shots: <paths | none, no visible change>. Phase <next> ETA: <HH:MM zone>
   review trial: ux <N> findings, code <N> findings, review-seat minutes <M>, ux check minutes <U>, ux repair minutes <R>
   design check: <pass | N defects, each moved to <phase>> — fresh helper on exactly these shots, built from <hash>
   ```

   A phase checkpoint's notice carries the second line:
   `progress_history.py review-trial`'s output, verbatim
   (`/unit:checkpoint` step 9). `Phase <next> ETA` counts from the code
   checkpoint commit.

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
   (`/unit:delegate` → <UXReview/>) that a fresh helper gave on exactly the
   shots this notice sends, taken from a build of `<hash>`. A verdict on other
   shots, or on a build from before a later change to what users see, is
   stale: re-shoot and re-judge before sending. Send the notice on a `pass`,
   or with each defect moved to a follow-up phase under item 7, named on the
   line. User rule 2026-10-01 (nightly review): 2 of about 25 notices carried
   a fresh verdict, and 3 holds were caught after the unit had moved on, 187
   minutes in 2.6 days; with a fresh pass the merge followed in 1–2 minutes.

   Under <PhaseEnd/> step 6, start each next phase that can run under berth,
   with its own checkpoint (`/showrunner:produce` → Rules, parallel by default).
4. **The merge branch is the showrunner's.** Never merge into it or push it.
   Push only your own branch; <PushCheckpoint/> does it at each checkpoint.
   <PeriodicCI/> and <CICleanup/> do not run in a unit; the showrunner runs CI
   on the merge branch.
5. **One more commit kind.** Besides checkpoints, a unit director may merge the
   merge branch into the unit's branch, when a gate clears or the showrunner asks:
   `git merge --no-ff -m "merge(<plan-slug>): <merge branch> at <short hash> — <reason>" <merge branch>`.
   Resolve conflicts on your side, build and test, then continue.
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
   - **One batch per check** (2026-10-01, measured: late repair rounds came
     mostly from one visual defect found live after each fix). Every live
     check or set of shots after a fix covers every view the phase changes,
     and all its findings go to the next round together. Never send one
     defect, fix it, then look again.
   - **A fix that fails twice** (2026-10-01, measured: one bar-fit finding
     failed four times before the fifth attempt held). When the same finding
     is still open after two fixes, send no third seat pass with the same
     approach. First find why the fixes did not hold, write it in the
     attempts log, then dispatch once with an approach that addresses that
     cause.

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
   (`/unit:delegate` <BerthDecisions/>). Name each such file in your checkpoint
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
    production (user, 2026-10-06). Run its recorder calls under `TZ=<zone>`, the
    production doc's **User zone**, so every time in it is in that zone (user,
    2026-10-04). When the showrunner asks for `/unit:eta`, run it at once.
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
    once after the seat's edits, never once per crate; per crate is right
    only for test (`/unit:delegate`). Fix a file you do not
    own through item 9, and name it in your checkpoint notice. Tell your seats
    this in every Work Order that runs lint.
14. **When others wait on you** (user, 2026-09-29). When your checkpoint turns
    the merge branch red, in CI or lint, send a small fix checkpoint at once,
    ahead of your phase work. Checkpoint at your next green
    point, with polish unfinished if need be. If only part of your work is
    needed, checkpoint that part first. A regression never lands: gates pass,
    and the shots are no worse than the merge branch. A fix the showrunner asks
    for comes after that checkpoint, unless the waiting unit needs it. This is
    packaging under `<DecisionEconomy/>`; state it in one line.
</ProductionUnit>
