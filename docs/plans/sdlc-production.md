# Production — sdlc

> **Status: PRODUCTION — running.** Our software development life cycle process: how we prepare design and documentation, how the design and documentation process runs, how showrunners, unit directors and workers share the work to implement it, and the names of things (the user, via the showrunner, 2026-10-20).

## Production Context

- **Source plans:** none yet. The user set up this production on 2026-10-20, via the showrunner, in their own words; /producer:to_production did not split it. Each unit writes its first plan, and its row below then names it.
- **Repository:** `/home/natepiano/.claude`
- **Merge branch:** `sdlc` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-build-followups-sdlc` — the sdlc session's worktree, where `/showrunner:promote_showrunner` resumes it; on `sdlc` from launch
- **Showrunner session:** sdlc
- **Log:** `docs/plans/sdlc-log.md` — untracked: never added or committed (never `git add -A` or `-a`); one line per event
- **Updates:** on demand (user, 2026-10-13: no timer unless asked); each update reports every unit in full
- **Merge tests:** `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` and `basedpyright scripts/direct scripts/production` (pass is its `0 errors, 0 warnings` line), run in the showrunner checkout
- **Push:** git
- **Promote:** ~/.claude, mac ~/.claude
- **Capacity:** natedev has 32 cores and 60 GB of memory; four units. The work is mostly documents and Python scripts, so the load is low. Its seats share natedev's build slots with build-followups.
- **Change review:** `~/rust/hanadocs/change-review/change-review - natedev.md` — sdlc reports to natedev, so new rows go to the root list with Production `sdlc`; rows before 2026-10-29 stay in `change-review - sdlc.md`
- **Proposals:** `~/rust/hanadocs/issues`

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| guild-process-unit | (retired 2026-10-29, worktree removed, session 842c2149-c59b-4abd-934c-ba61d14944ad) `docs/plans/sdlc-guilds.md` | `/home/natepiano/worktrees/claude-build-followups-sdlc-guild-process` | `sdlc-guild-process` | — | `docs/plans/sdlc-guilds.md`; `docs/plans/sdlc-guilds-next.md`; `docs/as-built/guilds.md`; `scripts/astra/astra_roles.py`; `scripts/astra/test_astra_roles.py`; `scripts/guilds/`; `config/guilds/`; `commands/astra_review.md` and `scripts/astra/astra_review.py` with its test (the guild block only; natedev's enh-showrunner owns the rest); `~/rust/nate_style/guilds/` (separate repo: committed on its main by this unit after its phase gates, fast-forward only) |
| phases-unit | `docs/plans/sdlc-phases.md` | `/home/natepiano/worktrees/claude-build-followups-sdlc-phases` | `sdlc-phases` | — | `docs/plans/sdlc-phases.md`; `docs/plans/sdlc-phases-next.md`; `docs/plans/sdlc-phases-design.md`; `scripts/direct/phase_process.py`; `scripts/direct/test_phase_process.py` |
| fix-rounds-unit | `docs/plans/sdlc-fix-rounds.md` | `/home/natepiano/worktrees/claude-build-followups-sdlc-fix-rounds` | `sdlc-fix-rounds` | — | `docs/plans/sdlc-fix-rounds.md`; `docs/plans/sdlc-fix-rounds-next.md`; `docs/plans/sdlc-fix-rounds-study.md`; `scripts/history/`; `commands/history.md` |
| roles-unit | `docs/plans/sdlc-roles.md` | `/home/natepiano/worktrees/claude-build-followups-sdlc-roles` | `sdlc-roles` | — | `docs/plans/sdlc-roles.md`; `docs/plans/sdlc-roles-next.md`; `docs/plans/sdlc-roles-design.md`; `scripts/production/roles_audit.py`; `scripts/production/test_roles_audit.py`; `scripts/production/role_terms.toml` |
| hierarchies-unit | `docs/plans/build-followups-hierarchies.md` | `/home/natepiano/worktrees/claude-build-followups-hierarchies` | `build-followups-hierarchies` | — | `docs/plans/build-followups-hierarchies.md`; `docs/plans/build-followups-hierarchies-next.md`; `docs/plans/build-followups-hierarchies-design.md`; `scripts/hierarchies/` |
| models-unit | `docs/as-built/usage-ledger.md` (run done; the user 2026-10-11 10:02 PDT via hana, start confirmed 10:21 PDT; follow-ups: `docs/plans/build-followups-usage-ledger-next.md`; earlier runs: `docs/as-built/long-lived-workers.md`, `docs/as-built/director-model-study.md`) | `/home/natepiano/worktrees/claude-natedev-models` | `natedev-models` | — | `docs/plans/build-followups-model-study.md`; `docs/as-built/long-lived-workers.md`; `scripts/model_study/`; `docs/as-built/director-model-study-results.md`; `scripts/delegate/` (seat launcher); `scripts/agents/codex_mesh.py`; `commands/unit/direct.md`; `commands/unit/delegate.md`; `docs/production_format.md`; `docs/as-built/usage-ledger.md`; `scripts/usage/`; also touches `review.sh`, `commands/showrunner/produce.md` (hub; the showrunner-start call) and `settings.json` (SessionStart hook; the user's go before main) |
| devtools-unit | `docs/plans/sdlc-devtools-tests.md` | /home/natepiano/worktrees/claude-build-followups-sdlc-devtools | sdlc-devtools | — | idempotency (rerun safety) of tools that change shared state in steps |
| qprobe-unit | standby | /home/natepiano/worktrees/claude-build-followups-sdlc-qprobe | sdlc-qprobe | — | — |

Scope of each unit:

- **guilds-unit** — a persistent, resumable designer role and a persistent, resumable reviewer role. Each can work under a unit director and under a showrunner ("it can and probably should be both"). It also designs a designer guild and a reviewer guild that share tips, tricks and common documentation: designers focus on design skill, reviewers on review skill. The guilds use our messaging system. Every role is a named role. The unit also writes the changes to `/unit:direct` and the showrunner commands.
- **phases-unit** — the phase process: how `/unit:direct` phases run from start to end (Work Orders, reviews, repair rounds, checkpoints, shrink, as-built). Also how design and documentation docs are made before a plan exists.
- **fix-rounds-unit** — finds the phases with the most repair rounds and studies the highest counts first to measure the effect. Then it proposes changes that limit repair rounds and still meet the coding goals ("so we know how to stop ourselves and still get the work done").
- **roles-unit** — the responsibilities of showrunners, unit directors and workers, and the names of things. It works with hierarchies-unit (build-followups) and does not do the same work.

### Held design roles

`scripts/astra/design_roles.py` keeps one held designer and reviewer per design.
State is in `~/.local/state/astra/design-roles/` and `~/.local/state/astra/design-ownership/`.
`/unit:direct` and `/showrunner:produce` use these roles; see [long-lived workers](../as-built/long-lived-workers.md#lasting-roles).

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |
| `commands/unit/direct.md` | phases-unit | guild-process-unit (the designer and reviewer roles); fix-rounds-unit (the repair round rules) |
| `docs/production_format.md` | roles-unit | guild-process-unit (role names); fix-rounds-unit (<ProductionUnit/> item 7) |
| `commands/showrunner/produce.md` | roles-unit | guild-process-unit (the designer and reviewer roles under a showrunner) |

These three files are also hub files in build-followups. An edit to one of them goes through cargo-berth's normal flow, and the checkpoint notice names the build-followups owner.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | hierarchies-unit Phase 4 (measure.py reads wait records and merge receipts) | phases-unit Phase 6 (HI7 wait records and merge receipts; depends on its phase 3) | That checkpoint is on origin/sdlc and ~/.claude main, its notice names the record file and commit, and the file holds at least one wait record and one receipt from a real phase. |

When a plan needs another unit's checkpoint, the gate goes here.

## Close-out

- Send cargo-liner the final guild design and the role names the user chose, so its handler unit shows them. `astra-designer` and `astra-reviewer` stay as the per-design role and census IDs (`docs/as-built/guilds.md`).
- Each unit's as-built doc is in `docs/as-built/`, and the docs it replaces are deleted or amended.
- Tell the natedev showrunner about each change to a build-followups hub file once it is on main.

## Production rules

- **A flat organization** (the user, via the showrunner, 2026-10-20: "I want a flat organization"). sdlc is a top-level production, a peer of build-followups, with no showrunner above it. `showrunners.py parent sdlc` prints no parent. If a promotion attached it to another showrunner, take it out with `/showrunner:attach`.
- **Process redesigns go to the user as questions** (the user, via the showrunner, 2026-10-20: "those should come back to me as questions"). Promotions, unit renames, changes to responsibilities and changes to the process are the user's decisions. The showrunner and the units do not make them alone. A unit sends the question, the answers it can build and the one it recommends to the showrunner, and the showrunner gives it to the user.
- **cargo-liner gets the guild design on every change** (the user, via the showrunner, 2026-10-20). Each time the guild design changes, guilds-unit sends it to cargo-liner, the showrunner of remove-running-section, so that its handler unit shows the design in simple text. cargo-liner owns the `production_tree.py --json` edges for the Astra designer and reviewer records and the `statusline.conf` keys. cargo-liner picks the role names with the user.
- **Coordinate with three units in other productions** (the user, via the showrunner, 2026-10-20):
  - hierarchies-unit (build-followups) designs the best hierarchy for fast delivery within the user's constraints. roles-unit and guilds-unit send it their designs and read its plan before they write theirs. They do not do the same work.
  - fps/frame-time-unit (hana) runs the first Astra designer by hand. guilds-unit reads what it learns before it writes the designer role.
  - experiments-unit (cargo-liner) starts with a hand-resumed Astra thread and `/astra_review`. guilds-unit reads what it learns before it writes the reviewer role.
- **No CI on this repo** (natedev, 2026-10-04, for `~/.claude`). `~/.claude` has no workflows, so the Merge tests replace `verify.sh test`, and <CIPoint/>, the smoke launch, the Mac run and `validate_and_push.sh` do not apply.
- **Each merged phase goes to main at once** (natedev, 2026-10-04, for `~/.claude`), because `~/.claude` main is the live configuration. After a merge's tests pass: merge `main` into `sdlc` if main moved, push `sdlc`, fast-forward `~/.claude` main to it (`git -C ~/.claude merge --ff-only <sha>`; other sessions' uncommitted files there stay untouched), and push main only when `origin/main..main` held no other session's commits before the fast-forward. Then the Mac pulls: `ssh mac 'cd ~/.claude && git pull --ff-only natedev:/home/natepiano/.claude main'`.

## Review decisions

The user's review with the showrunner, 2026-10-21, one item at a time. Each unit gets its answers by message and writes them into its own plan.

### guilds-unit

Model, in the user's words (2026-10-21): the goal of a guild is to help guild members do their jobs better. Showrunners and unit directors share behavior and get guild functionality too, not only designers and reviewers. Heads do not read every member turn: that costs too much. Set events make members write to their guild or head. The user sends all guild feedback through the head. Each skill that starts a member role carries short guidance on how to work with the guild and its head.

| # | Item | Decision |
| --- | --- | --- |
| G1 | Which jobs get a guild (showrunners, unit directors, designers, reviewers; workers?) | Four guilds: showrunners, unit directors, designers, reviewers. Workers have no guild; they get better guidance through their unit director's work orders. |
| G1b | The head's form | Each head is a unit director, one per guild, under one lasting guilds showrunner, not under sdlc. In herdr: a top-level guilds space, the guilds showrunner as the first tab, then the four head unit directors in alphabetical order. This replaces the brief's "each head is a top-level showrunner". |
| G1c | Where the guilds showrunner sits in the showrunner tree | Under sdlc (the user, 2026-10-28: "attach"). sdlc's dailies show guilds as one row; `/next all-below` in sdlc adds its items. Its herdr space stays top level; it keeps its own units, merge branch and `/next`, and still serves every production. Replaces guilds rule 1's "no parent". |
| G2 | What heads watch, since they do not read every turn | Events plus records: a head reads turns when an event fires (G3), and once a day reads the records already kept (repair rounds, time per phase step, review findings); it reads turns only where a record looks bad. |
| G3 | The events that make a member write to its guild or head | The member sends one short message to its head only, never to the whole guild, when: the user corrects it; a phase reaches repair round 4; the same review finding comes back twice; it works around a tool fault or a rule that slowed it; it finds a way that saved real time. The head decides what goes to the whole guild. |
| G3b | Where the heads keep the guild tips | In ~/rust/nate_style: one folder per guild plus one shared folder, one tip per markdown file like the rust rules, and a base folder per guild with Obsidian views by tag like rust_base. The head adds and edits tip files. A member loads its guild's tips when its skill starts. Tips reach both machines through git; the design's copy step and receipts go. Replaces docs/guilds in ~/.claude. |
| G4 | The user's guild feedback goes through the head | Decided by the user's words. The user types in the head's tab in the guilds space, or tells any showrunner, which passes it on. The head writes it as a tip file. |
| G5 | Short guild guidance in each skill that starts a member role | Decided by the user's words. /showrunner:produce, /unit:direct and the designer and reviewer start prompts each carry a short block: who your head is, load your guild's tips, the G3 events, and how to write to the head. |
| G6 | Where the heads live | Settled by G1b: the guilds space in herdr on natedev (showrunner's call: the guild records and the other showrunners are on natedev). |
| G7 | Guild names (was question B) | The top level is named guilds (the showrunner and its herdr space). The four guilds and their head units are named designers, reviewers, showrunners and units (herdr tab order after the guilds showrunner: designers, reviewers, showrunners, units). Goddess names are on hold. Showrunner's call: the tip folders are ~/rust/nate_style/guilds/<guild>/, with the shared tips in ~/rust/nate_style/guilds/common/. |
| G8 | Done work: phase 1 (roles in the tree), phase 2 (design doc) | Revise: keep phase 1; guilds-unit updates the design doc with these decisions. Its lasting-role half stays; its guild half changes. |
| G9 | Next phases: redesign for the new scope, then build | OK: 3 revise the design doc; 4 tip folders and views in nate_style/guilds plus the guild block and G3 events in each starting skill; 5 the guilds production doc, then the top-level guilds showrunner and its four head units, each with its daily record read; 6 lasting designer and reviewer threads. The user's model: sdlc's guild-process unit director owns the guild process; the guilds showrunner runs each guild by that process. The two need different session names. Showrunner's call: sdlc's guilds-unit becomes guild-process-unit (session guild-process). |
| G10 | Where hierarchies-unit's work goes | Move hierarchies-unit into sdlc as its own unit, beside guild-process, with /showrunner:transfer_unit once natedev finishes that command. It keeps its session and reviewed work; guild-process reads its findings. |

### roles-unit

| # | Item | Decision |
| --- | --- | --- |
| R1 | Goal and plan: own who does each duty and what each thing is called; done 1 (name count, duty map), 2 (recommendations); next 3 (build the first approved step), 4 (measure it, propose the next) | OK. |
| R1b | Session names apart from roles (the user, 2026-10-21) | Decided by the user's words. A session's name and its role are separate: many sessions can hold the designer role, and none needs to be called designer. Every session records its role apart from its name. The unit board and cargo-handler show the session name and the role as two columns, side by side, always easy to see. Later this allows more creative session names. roles-unit defines the role record; the unit board's owner and cargo-liner add the column. |
| R2 | Q1 who owns the guild tips | Settled by G3b and G4: each guild head keeps its guild's tips in nate_style/guilds. |
| R3 | Q5 head names versus per-design role names | Settled by G7: heads are named designers, reviewers, showrunners, units; astra-designer and astra-reviewer stay for the per-design threads; goddess names on hold. |
| R4 | Q2 "gate" | A "wait" is one unit waiting on another. A "gate" always means a check that must pass (verification). This replaces roles-unit's recommendation ("gate" for the wait, "verification gate" for the check). |
| R5 | Q3 "reviewer" | Name by the job: design reviewer, code reviewer, UI reviewer, and designer. This replaces roles-unit's "Astra reviewer / pooled reviewer". |
| R5b | Reviewer names across productions (the user, 2026-10-28) | Every shown name carries production · unit · job, for example "sdlc · roles · code reviewer 6", in reports, messages and thread names. The address stays as it is. |
| R6 | Q4 "proposal", "add-on", "change" | Approved as roles-unit recommended (the user, directly with roles-unit, 2026-10-28 13:18 EDT: "yes to all"). |
| R7 | Q6 "orchestrator" | Approved as roles-unit recommended (the user, directly with roles-unit, 2026-10-28 13:18 EDT: "yes to all"). |
| R8 | Q7 "worker" and its other names | Approved as roles-unit recommended (the user, directly with roles-unit, 2026-10-28 13:18 EDT: "yes to all"). "Small-fix helper" does no code work (the 2026-10-28 rule). |
| R9 | Step 1: retire "seat" and add a check | Approved as roles-unit recommended (the user, directly with roles-unit, 2026-10-28 13:18 EDT: "yes to all"). roles-unit builds it as its phase 3. |
| R10 | Q8 "as-built" | Approved as roles-unit recommended (the user, directly with roles-unit, 2026-10-28 13:18 EDT: "yes to all"). |

### fix-rounds-unit

| # | Item | Decision |
| --- | --- | --- |
| F1 | Goal and plan: a true count of repair rounds, a study of the phases with the most, changes that cut rounds and keep the work; done 1-4; next 5 (apply approved changes), 6 (measure them) | OK. The user: the fixes could be far-ranging, so the lasting result is a process that finds them and improves the work. |
| F2 | P1 the director runs unchanged lint and test commands itself | Approved (the user, 2026-10-28). |
| F3 | P2 from round 4, a missing acceptance item becomes a follow-up | Approved as a 30-exposed-phase trial, from round 4 (the user, 2026-10-28). |
| F4 | P3 from round 4, an open finding outside the kept kinds becomes a follow-up || Approved as a 30-phase trial (the user, 2026-10-28). Add (the user): at each fix limit, try something different with an Astra review. When a phase reaches repair round 4 with a finding still open, or a kept finding has failed two fixes, the director runs /astra_review on a short note: the finding, the tries so far and why each failed. Astra's new approach goes into the next try, or into the follow-up Work Order when the finding moves out. |
| F5 | P4 each launch checks the active phase and records planned work apart from repairs || Approved as a 30-phase accuracy trial; a stale or missing phase refuses the launch (the user, 2026-10-28). |
| F6 | P5 each repair prompt carries the accepted checks its edits can break || Approved as a 30-phase trial, for all repairs (the user, 2026-10-28). |

### phases-unit

| # | Item | Decision |
| --- | --- | --- |
| PH1 | Goal and plan: measure how a phase and the doc pipeline run, find where time and tokens go, change one step at a time; done 1 (measure); in 2 (recommend); next 3 (build first step), 4 (measure it) || Approved (the user, 2026-10-28). |
| PH2 | Step 1: tokens first (cheap A) or dispatch time first (C, then cheap A) || Tokens first: cheap A, each step reads only its own rule file (the user, 2026-10-28). |

### hierarchies-unit (moving into sdlc)

| # | Item | Decision |
| --- | --- | --- |
| HI1 | Goal and plan: the organization that ships Hana fastest under the user's constraints; recommends A (repair today's tree) || A, as an experiment (the user, 2026-10-28: "i think of this all as experimental and you should treat it as such"). Each rule, the cargo Hana-cost test included, is a trial with a measure and a review point, and it is dropped if it does not help. |
| HI2 | Q3 must build-followups and sdlc cut a measured Hana cost, or get a fixed share || Revised and approved (the user, 2026-10-28: "all of this work is at the very least transitively supportive... we use natedev as a test bed for ideas for hana"). Every new piece of work in build-followups, sdlc and cargo names its link to Hana, direct or indirect. No measured Hana cost and no fixed share. Record the links; review later which paid off. This softens HI1's cargo rule to the same link rule. |
| HI3 | Q4 the user's daily review budget; may old unreviewed changes close || Part 1, budget: no daily budget (the user, 2026-10-28). The user wants what is being built: a Jev-driven mechanism (scripts/issue_score) that always sorts every item for review by importance, so each review gives the most decision value. Part 2: keep open; no change closes without the user's check (the user: "i was on vacation for 7 days and things accumulated - that's why i want priority sorting"). |
| HI4 | Q5 is the account count fixed || Fixed, with proposals: the account count stays; when records show one more account ships more Hana work, the showrunner brings a proposal with the numbers; cost per phase alone is no case (the user, 2026-10-28). |
| HI5 | Q6 where each newly created showrunner and unit sits in the tree || Dropped (the user, 2026-10-28: does not recall the need). The live showrunner tree is the record; a production doc that disagrees with it is fixed by its owner. |
| HI6 | Q7 push-authorization audit || Approved (the user, 2026-10-28). Each showrunner checks which of its sends to the user pass a failed text check, fixes only paths confirmed as not approved, and records which approved exception each send uses. A change to an exception is its own question. |
| HI7 | Q8 record waits and merge receipts || Approved as a 4-week experiment (the user, 2026-10-28). Each phase records each wait and its cause; each merge leaves a receipt (project, issue, commit, branch, time) that the receiving showrunner checks. After four weeks, hierarchies reports which waits slow Hana. phases-unit builds the records, roles-unit writes the duties; no gate changes. |
| HI8 | Q9 cargo Hana-cost note || Settled by HI2 (showrunner call, 2026-10-28). The note is a link to Hana, direct or indirect, not a measured cost. It is written at a new unit, a later run, added work and a board pull; user orders, demo and herdr work and small fixes start at once. No peer check: a later review checks which links helped Hana. |
| HI9 | Q10 run-end ownership check || Approved with the user's note (2026-10-28): "as we name workers for a unit - we reuse them - unit directors need to rely on building up context and most of our unit directors are very long lived, so must most of their workers be long lived". Workers live as long as their unit director, across runs, and are reused by name. The run-end check ends only workers whose unit director is gone or whose owner cannot be found; active workers and pooled reviewers stay. Each showrunner removes timers nobody asked for. |
| HI10 | Q11 trial B, only under both conditions || Settled by HI2 (showrunner call, 2026-10-28). HI2 ruled out a measured Hana cost, so B's added rule does not start as written. If the HI7 wait records show non-Hana work slowing Hana, hierarchies brings a new proposal. |
| HI11 | Q12 defer C || Dropped (the user, 2026-10-28: "i see no reason for natedev to hand out workers or for their to be a centralized worker mechanism"). C is not a later option; each showrunner keeps starting its own workers. |
