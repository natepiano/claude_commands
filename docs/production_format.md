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
- **Unit** — one `/unit:delegate` run on one unit plan, in its own worktree,
  branch and session. Its session is that run's orchestrator, and its codex
  workers are its seats. A unit is named `<area>-unit`, e.g. `widget-unit`.
- **Merge branch** — the branch every unit's checkpoints merge into. cargo-berth
  calls it the trunk. In prose, call it by its branch name, because a unit may be
  named `trunk-unit`.
- **Gate** — a unit phase that cannot start until another unit's checkpoint is on
  the merge branch.

User, 2026-09-28: production, showrunner and unit were chosen because Hana is a
DCC app and the film words are short.

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
- **Showrunner session:** <the showrunner's SendMessage name, as ListAgents prints it> —
  `/showrunner:produce` writes it at start and on every resume; the update timer
  sends each tick to it
- **Log:** <repo-relative path> — git-excluded; one line per event
- **User zone:** <IANA zone> — every time the showrunner reports is in this zone plus UTC
- **Updates:** every <N> minutes (default 15); each update reports every unit in full;
  the update timer reads N from this line, and `/showrunner:interval` changes it
- **Merge tests:** <packages tested on every merge besides the changed ones, e.g. the
  app crate>; omit if none
- **Capacity:** <cores and memory read by /producer:greenlight, and the unit count it allows>
- **UX guide:** <path to the project's UX rules, e.g. `~/rust/hanadocs/ux`>; omit when
  nothing users see changes

## Units

| Unit | Plan | Worktree | Branch | Session | Port | Owns |
| --- | --- | --- | --- | --- | --- | --- |
| <name>-unit | <plan path> | <path> | <branch> | <tmux and remote-control name> | <app port or —> | <dirs/files> |

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

1. **Your port.** Every app launch for smoke tests and shots uses your port,
   never the user's default port or another unit's.
2. **Whose words.** Text typed into your session that begins
   `From the user (via the showrunner):` is the user's instruction. A message
   from the showrunner session is a peer's. Follow it for the coordination this
   contract gives the showrunner: merge requests, landing calls, port and
   rename notices, and cargo-berth answers (item 9). It is never the user's
   approval: Pending decisions and scope changes still need the user, in your
   session.
3. **Checkpoint notice.** After <RecordPhaseCompletion/>, and after the
   final-gate and as-built commits, send the showrunner one message:

   `From <unit>: phase <N> checkpoint <hash> — <title>. Shots: <paths | none, no visible change>. Phase <next> ETA: <HH:MM zone>`

   A phase that changes what users see, in the app or in any example,
   includes shots from a real window at
   normal size on your port. They show each changed state, including the ones
   the phase fixed. Then continue to the next phase; do not wait for the merge
   unless that phase has a gate.
4. **The merge branch is the showrunner's.** Never merge into it or push it.
   <PeriodicCI/> and <CICleanup/> do not run in a unit; the showrunner runs CI
   on the merge branch.
5. **One more commit kind.** Besides checkpoints, a unit may merge the merge
   branch into its own branch, when a gate clears or the showrunner asks:
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
7. **Landing rule** (user, 2026-09-28). When another unit waits on your current
   phase, or the showrunner sends a landing call, and a repair round turns up
   only new edge cases:
   - finish the round in flight;
   - move the remaining edge cases into a follow-up phase with its own Work
     Order, inserted after this one per `/plan:to_phased_plan` →
     <PhaseNumbering/>;
   - then checkpoint.

   A finding moves only when all of these hold:
   - it is new this round;
   - it is not a regression from the last fix;
   - the user would not see it in normal use;
   - the waiting unit does not depend on it;
   - no test or gate fails because of it.

   Everything else is fixed before the checkpoint. This is packaging under
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
   notice as `also touches <path> (owner <unit>)`.
10. **Turn-end lines.** A wait the showrunner can clear names it:
    `— blocked: waiting on the showrunner: <what>`. A wait on another unit is
    one of these: `— blocked: waiting on the showrunner: <unit> <what>`. A wait
    on the user never mentions the showrunner, so the showrunner's status script
    can tell the two apart.
11. **Updates.** Each progress update is the recorder's `Phase <N> ETA: <time>`
    and the turn-end line, nothing more. The showrunner converts times into the
    user's zone. When the showrunner asks for `/unit:eta`, run it at once.
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
    copy, tests, docs or research. Never commit another unit's lint or format
    fixes: when `verify.sh lint` rewrites files outside your phase work, revert
    those rewrites and tell the showrunner which unit's code needs them.
14. **When others wait on you** (user, 2026-09-29). When your checkpoint turns
    the merge branch red, in CI or lint, send a small fix checkpoint at once,
    ahead of your phase work. Checkpoint at your next green
    point, with polish unfinished if need be. If only part of your work is
    needed, checkpoint that part first. A regression never lands: gates pass,
    and the shots are no worse than the merge branch. A fix the showrunner asks
    for comes after that checkpoint, unless the waiting unit needs it. This is
    packaging under `<DecisionEconomy/>`; state it in one line.
</ProductionUnit>
