# Duties and names in the development process

> **Status: DESIGN — phase 1 map and baseline.** Recommendations await the user's answer. The two approved reporting wording fixes are the only live contract changes in this phase.

Scope: `docs/plans/sdlc-roles.md`, Decisions and Phase 1. Duties and names belong here. Structure belongs to hierarchies-unit; designer and reviewer contracts belong to guilds-unit; phase steps and the document pipeline belong to phases-unit; repair rules belong to fix-rounds-unit. This document refers to their work and does not replace it.

## Sources

Paths without a prefix are relative to this checkout. These short references keep each matrix row readable:

| Reference | File |
| --- | --- |
| PF | `docs/production_format.md` |
| SR | `commands/showrunner/produce.md` |
| UD | `commands/unit/direct.md` |
| Format | `docs/delegate_plan_format.md` |
| Decisions | `~/.claude/docs/decision_criteria.md` |
| Hierarchy | `/home/natepiano/worktrees/claude-build-followups-hierarchies/docs/plans/build-followups-hierarchies-design.md`, Map → Roles |
| SDLC | `/home/natepiano/worktrees/claude-build-followups-sdlc/docs/plans/sdlc-production.md` |
| Guilds | `/home/natepiano/worktrees/claude-build-followups-sdlc-guilds/docs/plans/sdlc-guilds.md` |
| Phases | `/home/natepiano/worktrees/claude-build-followups-sdlc-phases/docs/plans/sdlc-phases.md` |
| config memory | `~/.claude/projects/-home-natepiano--claude/memory/` |
| nixos memory | `~/.claude/projects/-etc-nixos/memory/` |
| hana memory | `~/.claude/projects/-home-natepiano-rust-hana/memory/` |

Read on 2026-10-20 on natedev. The requested `git show HEAD:docs/plans/build-followups-hierarchies-design.md` failed because the file was not in HEAD. The Hierarchy reference is its on-disk draft, read without changes. The role names below use its row names. The Map stays in that document.

Repair source check, 2026-10-20: Guilds HEAD is `95eb9cda042cead9f09dcdab29025f314f22d83f` (`plan(sdlc-guilds): phase 2 runs beside phase 1 (showrunner approval)`). Its committed Phase 2 Spec and Roles text match the working plan read here. That plan defines the planned per-design roles in D51–D52; it leaves their full contracts and names to guilds-unit and the user.

## Duty matrix

`one owner` means the sources assign the scoped duty once. `duplicate` means two sources give that duty to the same role in different words. `conflict` means the sources give different instructions or owners. `no owner` means the cited sources leave the assignment open. A script performs an action for the named role; it does not become a second owner. Top-level showrunner and Child showrunner share the showrunner contract within their own productions.

| ID | Duty | Role that does it | Other role also named; scope | State | Sources: file and tag or section |
| --- | --- | --- | --- | --- | --- |
| D01 | Set product scope and approve irreversible choices | Executive producer | None; showrunners carry the question | duplicate | PF, Words → Executive producer and `<ProductionUnit>` item 2; Decisions, `<DecisionEconomy>`; SDLC, Production rules |
| D02 | Judge whether plans need a production; propose units, hub owners and gates | Producer | Executive producer decides a change in what ships | duplicate | PF, Words → Producer; `commands/producer/greenlight.md`, `<FormUnits>`, `<Capacity>` and `<Verdict>` |
| D03 | Split approved plans and write the production doc | Producer | None | duplicate | PF, Words → Producer; `commands/producer/to_production.md`, `<WriteUnitPlans>` and `<WriteProductionDoc>` |
| D04 | Merge a unit checkpoint into the merge branch | Top-level showrunner | Child showrunner for its own production; Unit director resolves conflicts on the unit branch | duplicate | PF, `<ProductionUnit>` items 4–5; SR, opening and `<MergeCheckpoint>` |
| D05 | Push a unit checkpoint branch | Unit director | None; workers do not checkpoint | duplicate | PF, `<ProductionUnit>` item 4; `commands/unit/checkpoint.md`, `<PushCheckpoint>` |
| D06 | Test and push the merge branch | Top-level showrunner | Child showrunner for its own production | duplicate | PF, Production Context → Merge branch and `<ProductionUnit>` item 4; SR, `<CIPoint>` and `<MergeCheckpoint>` |
| D07 | Promote the tested merge commit to main | Top-level showrunner | Child showrunner for its own production | one owner | SR, `<PromoteMain>` |
| D08 | Settle an implementation design or add-on within approved scope | Top-level showrunner | Child showrunner for its own units; Executive producer keeps product, scope and irreversible choices | duplicate | PF, `<ProductionUnit>` item 2; SR, `<Throughout>` → Designs and add-ons; `commands/unit/add_ons.md`, `<ReviewPendingAddOns>` |
| D09 | Approve a change to a role, name or process during this redesign | Executive producer | Top-level showrunner presents the question; Unit director supplies buildable answers | duplicate | SDLC, Production rules → Process redesigns go to the user as questions; nixos memory `process-redesigns-are-questions.md`, body and How to apply |
| D10 | Assemble and validate a phase Work Order before dispatch | Unit director | Producer preserves and splits source Work Orders at production creation | duplicate | UD, `<ComposeWorkOrder>`; `docs/delegate/compose_work_order.md`, items 1 and 4–5 and Delegate-ready fast path; PF, Words → Unit director |
| D11 | Dispatch workers and assign each slot a role and file set | Unit director | None | duplicate | UD, `<LongLivedWorkers>` and `<LaunchImplementation>`; `docs/delegate/launch_implementation.md`, `<LaunchImplementation>` items 3–5; `docs/delegate/write_prompt_contract.md`, `<PhaseTeam>` and `<TeamFilePartition>` |
| D12 | Write implementation, tests and repairs for assigned phase files | Codex worker | Small-fix helper handles separate small unowned fixes; Unit director has the stated doc-only or trivial post-review exception | duplicate | PF, Words → Worker; UD, opening and `<CoreContract>`; `docs/delegate/write_prompt_contract.md`, `<TeamFilePartition>` |
| D13 | Run assigned checks and read their output | Codex worker | Unit director owns acceptance, live smoke and final gate; these are separate checks | duplicate | `docs/delegate/phase_checks.md`, `<PhaseChecks>`; `docs/delegate/launch_implementation.md`, `<LaunchImplementation>` item 8; UD, `<RunApplicationSmokeTest>` and `<FinalGate>` |
| D14 | Run the main phase review, including style | Unit director | Codex worker and Pooled reviewer supply separate read-only reviews; a standalone style review stays with its invoking session | duplicate | `docs/delegate/dual_review.md`, `<DualReview>` item 4; `docs/delegate/phase_checks.md`, `<PhaseChecks>`; `commands/unit/style_review.md`, opening and `<StyleReview>` |
| D15 | Review code in a production with a review pool | Pooled reviewer | Codex worker is the explicit fallback if the pool cannot prime | duplicate | PF, Words → Reviewer and `<ProductionUnit>` item 16; UD, `<LongLivedWorkers>` |
| D16 | Hold and supply the standing review prompt | Review template | None; pool scripts allocate leases and never lease the template | one owner | PF, Words → Template, Review pool and Lease |
| D17 | Bring a user-only decision or action to the user | Top-level showrunner | Child showrunner sends an item through its parent and keeps its own item; Unit director supplies the need | duplicate | SR, opening, `<Throughout>` → Nested showrunners, What reaches the user and Open questions; hana memory `unit-asks-go-through-the-showrunner.md`, body and How to apply; PF, `<ProductionUnit>` item 2 |
| D18 | Relay the user's words to a unit | Top-level showrunner | Child showrunner relays to its own units; Unit director alone relays user words to its workers | duplicate | SR, `<Throughout>` → The user's words for a unit director and Nested showrunners; `docs/delegate/write_prompt_contract.md`, `<WritePromptContract>` item 4 and `<PhaseMesh>` |
| D19 | Measure and record a unit phase ETA | Unit director | Top-level showrunner requests a missing ETA and reports the result | duplicate | PF, `<ProductionUnit>` items 3 and 11; `commands/unit/eta.md`, Steps and Answer; SR, `<Throughout>` → Unmeasured ETAs |
| D20 | Give production dailies; choose the default report schedule | Top-level showrunner | Child showrunner reports its own production; Producer writes a conflicting default | conflict | PF, Production Context → Updates; SR, `<StartUpdates>` says both 15 minutes when absent and on demand when absent; `commands/producer/to_production.md`, `<WriteProductionDoc>` → Updates still says every 15 minutes |
| D21 | Retire a unit and its resources | Top-level showrunner | Child showrunner for its own unit; ending a director run does not retire the unit | duplicate | PF, Units → To retire a unit; `commands/showrunner/retire_unit.md`, opening and numbered resource list |
| D22 | Move a unit director to an account after checking active worker runs | Top-level showrunner | Child showrunner can invoke the move; natedev holds the role map for both machines | conflict | SR, `<PauseForMove>`; `commands/showrunner/move.md`, opening and idle/busy move rules; `commands/showrunner/accounts.md`, natedev role-map rule; config memory `ask-director-before-moving-unit.md`, body requires an answer before moving even an idle director |
| D23 | Rename a unit; decide when it can stop | Top-level showrunner | Executive producer approves the name change during this redesign; a Unit director cannot rename itself | conflict | PF, Units → To rename a unit says between phases; `commands/showrunner/rename_unit.md`, opening and step 1 allow mid-phase and immediate stop; `commands/unit/rename.md`, opening forwards to the showrunner; SDLC, Production rules |
| D24 | Place and release a timing build hold | Top-level showrunner | Unit director requests the hold and release; Codex worker and Small-fix helper never place it | duplicate | `commands/build/hold.md`, steps 1 and 4; hana memory `build-holds.md`, Frame-time asks build stop before timing and Seats never place build holds; SR, `<Throughout>` → The showrunner decides |
| D25 | Send an actionable phone push for a unit blocked on the user | Top-level showrunner | Child showrunner for its production; a machine session pushes only its own blocks | one owner | SR, `<Notify>` → Units tell you, and you push; SR, `<Dependencies>` rule 6 |
| D26 | Call an early landing; apply the automatic hard landing | Top-level showrunner for the early call | Unit director applies the hard landing from the third repair round without a call | duplicate | SR, `<LandingCall>`; PF, `<ProductionUnit>` item 7; fix-rounds-unit owns changes to these repair rules |
| D27 | Review remaining Work Orders, then shrink the completed phase | Unit director | Codex worker supplies an architect review only when a listed trigger fires | duplicate | UD, `<RunPhaseReview>`; `docs/delegate/run_phase_review.md`, `<RunPhaseReview>` and `<RunPhaseShrink>`; `docs/delegate/phase_end.md`, `<PhaseEnd>` items 3 and 7 |
| D28 | Close the run with as-built documentation | Unit director | None; its scope choice is not a product scope change | duplicate | SR, `<Throughout>` → As-built close-out; UD, `<RunAsBuilt>`; `docs/delegate/final_gate_commit.md`, `<RunAsBuilt>`; `commands/plan/to_as_built.md`, Purpose |
| D29 | Write a small unowned fix in the showrunner checkout | Small-fix helper | Top-level showrunner dispatches, commits, pushes and promotes; existing unit work stays with its unit | one owner | nixos memory `small-fixes-by-subagent-in-main.md`, body and How to apply; PF, Words → Showrunner writes no implementation code |
| D30 | Head the designer guild | Chief designer, planned (`astra-designer`) | None; detailed contract belongs to guilds-unit | one owner | SDLC, Guild heads; Guilds, Phase 2 → Roles and Guilds |
| D31 | Head the reviewer guild | Chief reviewer, planned (`astra-reviewer`) | None; detailed contract belongs to guilds-unit | one owner | SDLC, Guild heads; Guilds, Phase 2 → Roles and Guilds |
| D32 | Maintain and distribute each guild's shared tips | Not yet assigned | Chief designer, planned and Chief reviewer, planned are candidates; the member-to-head route is still to be designed | no owner | Guilds, Phase 2 → Guilds asks for the head's duties, shared documents and distribution path; SDLC, Scope of each unit → guilds-unit |
| D33 | Launch a unit director or resume a retired unit | Top-level showrunner | Child showrunner for its own units | duplicate | SR, `<LaunchUnits>`; `commands/showrunner/add_unit.md`, opening; `commands/showrunner/resume_unit.md`, opening and steps 1–3 |
| D34 | Launch or attach a child showrunner | Top-level showrunner | Child showrunner may hold a lower production under the existing contract; Executive producer keeps the flat-organization redesign choice | duplicate | SR, `<Throughout>` → Nested showrunners; `commands/showrunner/add_showrunner.md`, opening; `commands/showrunner/attach.md`, opening; SDLC, Production rules → A flat organization |
| D35 | Rename a production and move its unit worktrees between runs | Top-level showrunner | Child showrunner for its own production; Executive producer approves a new name during this redesign | one owner | `commands/showrunner/rename.md`, opening and steps 1–4; SDLC, Production rules |
| D36 | Set unit director effort and role-account selections | Top-level showrunner | Child showrunner can invoke the commands; natedev manages the shared role map, not a separate map per production | one owner | `commands/showrunner/effort.md`, opening; `commands/showrunner/accounts.md`, natedev role-map rule; config memory `ask-director-before-moving-unit.md`, How to apply bounds restarts |
| D37 | Route a cross-unit API rename or a file restructure | Top-level showrunner | Unit director ports and tests its own branch | duplicate | SR, `<CrossUnitChange>`; PF, `<ProductionUnit>` item 8 |
| D38 | Clear a dependency gate after a checkpoint merges | Top-level showrunner | Unit director checks the gate before dispatch | duplicate | SR, `<ClearGate>`; PF, `<ProductionUnit>` item 6 |
| D39 | File a finished proposal and start its approved work | Unit director | Top-level showrunner records the user's verdict and returns approved work; Executive producer approves product or process scope | duplicate | PF, `<ProductionUnit>` item 17; `commands/showrunner/proposal.md`, File, Approve and Start and close |
| D40 | Record an implemented change for the user's later review | Top-level showrunner | Unit director supplies checkpoint Change-Review lines; Executive producer gives the verdict | duplicate | SR, `<ChangeReviewFeatureLog>` and `<CloseChangeReview>`; `commands/change_review.md`, add and show |
| D41 | Give a phase briefing, phase report and ETA breakdown | Unit director | Top-level showrunner requests them and uses the result in dailies | one owner | `commands/unit/brief.md`, opening; `commands/unit/phase_report.md`, opening; `commands/unit/eta_breakdown.md`, Steps |
| D42 | Give the unit's current status report | Unit director | Top-level showrunner requests it; reports have no timer | duplicate | PF, `<ProductionUnit>` item 11; `commands/unit/report.md`, opening and `<ProgressReport>` |
| D43 | List live units, phases and work | Top-level showrunner | Child showrunner lists its production; the unit-board command currently only names its required plugin | one owner | `commands/showrunner/units.md`, opening; `commands/showrunner/unit_board.md`, body |
| D44 | List open proposals, plan backlog and pending add-ons | Top-level showrunner | Child showrunner lists its production; a listed add-on still belongs to the showrunner under D08 | one owner | `commands/showrunner/proposals.md`, opening, source list and State |
| D45 | List measured nightly improvement candidates | Top-level showrunner | Child showrunner may run it; the ledger lives on natedev | one owner | `commands/showrunner/nightly.md`, opening and final paragraph |
| D46 | Report style coverage and held changes | Top-level showrunner | Child showrunner of the style production also reports; this command starts no style work | one owner | `commands/showrunner/style_report.md`, opening and final paragraph |
| D47 | Track an upgrade and record user approval of dependency pins | Top-level showrunner | Executive producer gives the per-crate approval; Child showrunner tracks its production | one owner | `commands/showrunner/bevy_upgrade.md`, opening and approve/decline rules |
| D48 | Change a report timer when the user requests a schedule | Top-level showrunner | Child showrunner for its own production; Executive producer requests the interval | duplicate | PF, Production Context → Updates; SR, `<StartUpdates>`; `commands/showrunner/report_interval.md`, opening and step 1 |
| D49 | Promote a session to unit director or a unit to showrunner | Top-level showrunner | Child showrunner may invoke the existing route; Executive producer approves a promotion during this redesign | one owner | `commands/showrunner/promote_unit.md`, Usage and steps 1–7; `commands/showrunner/promote_showrunner.md`, opening and What it does; SDLC, Production rules |
| D50 | Start the repository's standing style production | Top-level showrunner | Child showrunner can be its parent under the current contract; structure changes go to hierarchies-unit and the user | one owner | `commands/showrunner/add_style.md`, opening; SDLC, Production rules and Scope of each unit |
| D51 | Write and revise a design in the same Codex thread; answer each review finding with a change or a reason to disagree | Astra designer, planned per-design role (`astra-designer`) | Unit director or showrunner holds and briefs the role; the holder passes reviews and judges the result, rather than writing the design; Chief designer, planned is the separate guild head in D30 | one owner | Guilds, Source → 2026-10-20 16:19 EDT; Phase 2 → Spec first bullet and Roles (working-plan lines 137 and 140; committed version above) |
| D52 | Review a design and return findings for the designer to answer | Astra reviewer, planned per-design role (`astra-reviewer`) | Unit director or showrunner holds the role and passes its review to the designer; the unit director rules on disagreements in the Phase 2 design; Chief reviewer, planned is the separate guild head in D31; Pooled reviewer in D15 reviews code under the pool contract | one owner | Guilds, Source → 2026-10-20 16:19 EDT (optional separate reviewer, at most one per design); Phase 2 → Spec first bullet, Roles and Workers (working-plan lines 137, 140 and 152; committed version above) |

Two helpers select recipients, not the actor's role: `commands/showrunner/announce.md` sends the user's words to showrunners, and `commands/unit/announce.md` sends them to unit directors. Their bodies require the invoking session to pass the user's words unchanged. Neither helper gives a worker or director new decision authority. `commands/unit/delegate.md` forwards to `/unit:direct`; `commands/unit/ci.md`, `<PeriodicCI>`, explicitly skips production units because D06 belongs to the showrunner.

The conflicts above record the source instructions. They do not authorize a move, rename, schedule change or repair-rule change. D30–D32 describe planned guild work. D51–D52 describe the planned per-design roles, at most one designer and one separate reviewer per design; they are distinct from the heads and the review pool. These rows do not claim those sessions are live or change the current implementation-design authority in D08.

## Term table

The machine-readable inventory is `scripts/production/role_terms.toml`. The table below gives meanings and source uses. A recommendation is proposed wording only. Paths and public command compatibility names remain separate from prose changes. Names with no observed alternative keep `—`; this means no alternative was found in the cited role files, not a zero over all files.

| Term | Kind | Definition and source | Other names and source uses | Recommended term |
| --- | --- | --- | --- | --- |
| production | record | Whole effort, its units, merge branch and production doc; PF, Words | — | production for the effort; production doc for its file |
| executive producer | role | The user who owns product, scope and irreversible choices; PF, Words | user: PF, Words; SR, `<Throughout>` | user in user-facing text; executive producer in the term table |
| producer | role | Session that judges and splits plans; PF, Words | — | producer |
| showrunner | role | Session that coordinates, merges, tests, settles designs and reports; PF, Words | orchestrator is a broader coordinator name: Format, introduction, specifically for a unit director | showrunner for a production coordinator |
| unit | record | One plan-backed run with its worktree and branch; PF, Words | — | unit; `<area>-unit` for its ID |
| unit director | role | Session that orders, dispatches, gates and checkpoints unit work; PF, Words | orchestrator: Format, introduction; `/unit:direct` names its command, not a worker | unit director |
| worker | role | Dispatched Codex thread that writes, tests, repairs or reviews; PF, Words | seat and Codex seat: UD, `<WorkerRuns>`, `commands/showrunner/rename_unit.md`; delegate: UD, opening, `docs/delegate/write_prompt_contract.md`, `<PhaseMesh>`; subagent also describes the separate Small-fix helper in nixos memory `small-fixes-by-subagent-in-main.md`; Codex worker: UD, `<LongLivedWorkers>` | worker in prose; retain slot names such as `impl` and `test` as identities |
| review pool | record | Reusable reviewers allocated by script; PF, Words | — | review pool |
| reviewer | role | Read-only Codex thread in the pool; PF, Words | pooled reviewer: Hierarchy, Map → Roles; Astra reviewer is a distinct planned role: Guilds, Phase 2 → Roles | pooled reviewer in shared prose; ask about the distinct design role |
| template | record | Thread with standing review instructions; never leased; PF, Words | review template: Hierarchy, Map → Roles | review template in shared prose |
| lease | record | A reviewer's assignment to a unit phase; PF, Words | — | review lease in shared prose |
| merge branch | record | Branch that receives unit checkpoints; PF, Words | trunk: PF, Words, retained as cargo-berth's term; SR, `<Dependencies>` also records a former unit named trunk | merge branch, or the actual branch name where needed |
| gate | step | Dependency that blocks a phase until another checkpoint merges; PF, Words | verification gate is a different use: UD, `<FinalGate>`; checks: `docs/delegate/phase_checks.md`, `<PhaseChecks>` | dependency gate for coordination; verification gate for checks; ask before changing |
| parent showrunner | role | Showrunner that receives another production's reports; PF, Words | parent: SR, `<Throughout>` → Nested showrunners | parent showrunner when the hierarchy is relevant |
| child showrunner | role | Showrunner with its own production that reports to a parent; PF, Words | lower showrunner: `commands/showrunner/add_showrunner.md`, opening | child showrunner |
| root showrunner | role | Top of an attached production tree; PF, Words | root: SR, `<Throughout>` → Nested showrunners; top-level showrunner: Hierarchy, Map → Roles | root showrunner for a tree; top-level showrunner for a peer without a parent |
| small-fix helper | role | Helper that writes a small unowned fix in place; nixos memory `small-fixes-by-subagent-in-main.md`, How to apply; no defining live role file | subagent and helper: that memory; helper is broader in UD, `<PhaseCleanup>` | small-fix helper; definition still needs a live owner file |
| astra-designer | role | Two planned uses: the per-design Codex role writes and revises a design in the same thread (D51; Guilds, Phase 2 → Spec and Roles); the temporary top-level designer guild-head ID names D30 (SDLC, Guild heads) | chief designer: SDLC, Guild heads; Guilds, Decisions → edge role ids and Phase 2 → Names require the head and per-design uses to be named apart | Astra designer for the per-design role; chief designer for the head until cargo-liner settles its name with the user |
| astra-reviewer | role | Two planned uses: the per-design Codex role reviews the design and returns findings (D52; Guilds, Phase 2 → Spec and Roles); the temporary top-level reviewer guild-head ID names D31 (SDLC, Guild heads) | chief reviewer: SDLC, Guild heads; Guilds, Phase 2 → Roles distinguishes the per-design role from pooled code and UI reviewers and one-turn `/astra_review` | Astra reviewer for the per-design role; chief reviewer for the head until cargo-liner settles its name with the user; pooled reviewer for D15 |
| Work Order | document | Self-contained dispatch specification against named files and Delegation Context; Format, Document structure and Rules 2–3 | work order: `docs/delegate/compose_work_order.md`, title; case variants count together | Work Order |
| production doc | document | Source of a production's units, branches, gates and rules; PF, Production doc structure | production document is listed in the term inventory; its measured live files are indexed below, including a zero if none use that phrase | production doc for the file |
| unit plan | document | Phased plan for one unit; PF, Words → Unit; `commands/producer/to_production.md`, `<WriteUnitPlans>` | delegate-ready plan: Format, introduction; that term also covers a standalone run | unit plan in a production; phased plan for the general format |
| as-built | document | Overview of the feature that shipped; `commands/plan/to_as_built.md`, Purpose | as-built overview: Format, introduction; as-built record: `commands/plan/shrink.md`, Purpose, is the shorter per-phase record; as-built doc: `commands/plan/to_as_built.md`, Purpose | as-built doc for the overview; as-built record for a phase |
| add-on | record | Optional follow-up not required to finish current work; `commands/unit/add_ons.md`, `<ConsiderNextItems>` and `<ReviewPendingAddOns>` | next item: same file; proposal: `<ConsiderNextItems>` also calls unfiled candidates proposals | add-on before approval or filing; retain the next-items file as a container |
| proposal | record | Finished proposal filed as an issue for a decision; `commands/showrunner/proposal.md`, File | candidate and add proposal: `commands/unit/add_ons.md`, `<ConsiderNextItems>`, also cover unfiled work | proposal for a filed issue; ask before narrowing the shared term |
| change | record | Implemented result queued for the user's check, with ID C<n>; `commands/change_review.md`, add | change-review row: same file; proposal P<n> is a separate record: `commands/showrunner/proposal.md`, opening | change for the result; change-review row for its record |
| dailies | document | Production report covering units and what needs the user; `commands/showrunner/dailies.md`, opening and Output | scheduled update: SR, `<StartUpdates>`; this names a trigger as well as its report | dailies for the report; on demand unless the user requests a timer |
| checkpoint | step | Commit and notice for a gated unit phase; `commands/unit/checkpoint.md`, `<CheckpointCommit>`; `docs/delegate/phase_end.md`, `<PhaseEnd>` | code commit and code ready notice: `<PhaseEnd>` items 1–2; code checkpoint: `commands/unit/eta_breakdown.md`, Steps | checkpoint; distinguish its commit from its notice |
| shrink | step | Replace a completed Work Order with the shipped facts; `commands/plan/shrink.md`, Purpose | closeout: same file, `--closeout` is a specific mode; shrink commit: `commands/unit/checkpoint.md`, `<ShrinkCommit>`, is its separate commit | shrink for the rewrite; shrink commit for the commit |
| phase review | step | Review the completed work's effect on remaining Work Orders; `docs/delegate/run_phase_review.md`, `<RunPhaseReview>` | plan review: `docs/delegate/phase_end.md`, `<PhaseEnd>` item 3; architect review is one conditional part: `<RunPhaseReview>` | phase review |
| landing call | step | Showrunner instruction to finish the round and defer eligible findings; SR, `<LandingCall>` | hard landing is a separate automatic rule: PF, `<ProductionUnit>` item 7 | landing call for the instruction; hard landing for the automatic rule |
| pending decision | record | Unresolved user choice in a Work Order that blocks dispatch until settled; Format, Pending decisions | Pending decision block: `docs/delegate/compose_work_order.md`, item 1 | pending decision |
| finding | record | Confirmed review defect tracked for closure; UD, `<FindingsLedger>` | issue: `docs/delegate/dual_review.md`, `<ClosureReview>`, also used for review findings; proposal issues are separate | finding for a review defect |
| reservation | record | Durable phase edit authorization kept through checkpoint release; UD, `<DelegatedPhaseReservationContract>` | phase reservation and delegated phase reservation: same tag; file claim: `docs/delegate/write_prompt_contract.md`, `<TeamFilePartition>`, is a narrower file scope | phase reservation; file claim for the narrower scope |
| ETA | record | Measured expected finish time with its basis; `commands/unit/eta.md`, Steps and Answer | phase ETA: same file; next-phase ETA: PF, `<ProductionUnit>` item 3, has a different target | ETA with the unit, target and time zone |
| /unit:direct | step | Command that runs a unit plan and dispatches workers; UD, opening | /unit:delegate: `commands/unit/delegate.md`, description and `commands/showrunner/promote_showrunner.md`, Checks | /unit:direct; keep compatibility paths separate |

The examples above describe meanings; some related names identify distinct stages or roles and are not counted as aliases. The index below lists every matching file for each other name in the term inventory, using the audit's path and string filters. The Baseline gives the complete live file set and file counts. Guild-head names and per-design role IDs are planned names, not approved replacements.

<details>
<summary>Other-name file index (n=546 live files; paths and script strings use the audit filters)</summary>

| Defined term | Other name | Hits (n) | Files (n) | Every matching file |
| --- | --- | --- | --- | --- |
| executive producer | user | 1484 | 189 | `CLAUDE.md`; `commands/ack.md`; `commands/add_banned_word.md`; `commands/add_to_fmt_allowlist.md`; `commands/adhoc_review.md`; `commands/adversarial.md`; `commands/agent_send.md`; `commands/alert_user.md`; `commands/announce.md`; `commands/api_review.md`; `commands/apply_example_layout.md`; `commands/ask_a_friend.md`; `commands/astra_review.md`; `commands/banned_word_analysis.md`; `commands/banned_word_hook.md`; `commands/berth_fix.md`; `commands/bevy_migration_plan.md`; `commands/build/hold.md`; `commands/build/report.md`; `commands/change_review.md`; `commands/changelog.md`; `commands/close_issue.md`; `commands/codex_winddown.md`; `commands/commit_prep.md`; `commands/efficiency_next.md`; `commands/exclude.md`; `commands/exclude_from_merge.md`; `commands/focused_eval.md`; `commands/forest_and_trees.md`; `commands/forest_and_trees_alert.md`; `commands/hana_shot.md`; `commands/idle_timeout.md`; `commands/implement_issue.md`; `commands/improve_decisions.md`; `commands/issue.md`; `commands/make_a_worktree.md`; `commands/mend_fix.md`; `commands/merge_branch.md`; `commands/message.md`; `commands/message_review.md`; `commands/message_style.md`; `commands/module_review.md`; `commands/nate.md`; `commands/new_account.md`; `commands/new_rust_project.md`; `commands/new_style.md`; `commands/next.md`; `commands/nightly_review.md`; `commands/notify_top_level.md`; `commands/orphans.md`; `commands/plan/phase_review.md`; `commands/plan/shrink.md`; `commands/plan/to_as_built.md`; `commands/plan/to_phased_plan.md`; `commands/pr_from_branch.md`; `commands/prioritize.md`; `commands/producer/greenlight.md`; `commands/producer/to_production.md`; `commands/quota_refresh.md`; `commands/release.md`; `commands/revert_banned_count.md`; `commands/rust_style.md`; `commands/screen_record.md`; `commands/shot_report.md`; `commands/showrunner/accounts.md`; `commands/showrunner/add_showrunner.md`; `commands/showrunner/add_unit.md`; `commands/showrunner/announce.md`; `commands/showrunner/attach.md`; `commands/showrunner/bevy_upgrade.md`; `commands/showrunner/dailies.md`; `commands/showrunner/move.md`; `commands/showrunner/produce.md`; `commands/showrunner/promote_showrunner.md`; `commands/showrunner/promote_unit.md`; `commands/showrunner/proposal.md`; `commands/showrunner/proposals.md`; `commands/showrunner/rename_unit.md`; `commands/showrunner/report_interval.md`; `commands/showrunner/style_report.md`; `commands/shutdown.md`; `commands/team_review.md`; `commands/unit/add_ons.md`; `commands/unit/announce.md`; `commands/unit/brief.md`; `commands/unit/checkpoint.md`; `commands/unit/ci.md`; `commands/unit/direct.md`; `commands/unit/eta.md`; `commands/unit/eta_breakdown.md`; `commands/unit/phase_report.md`; `commands/unit/report.md`; `commands/ux_eval.md`; `commands/validate_and_push.md`; `commands/watcher.md`; `commands/worktree_delete.md`; `docs/accounts.md`; `docs/as-built/agent-registry.md`; `docs/as-built/bevy-upgrade.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/as-built/build-followups-mul-add.md`; `docs/as-built/build-followups.md`; `docs/as-built/build-memory-admission.md`; `docs/as-built/build-report-session.md`; `docs/as-built/build-targets.md`; `docs/as-built/buildlog-memory-stalls.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/conversation-pause.md`; `docs/as-built/director-model-study-results.md`; `docs/as-built/director-model-study.md`; `docs/as-built/feature-log.md`; `docs/as-built/herdr-adoption.md`; `docs/as-built/herdr-restore.md`; `docs/as-built/herdr-trial.md`; `docs/as-built/mac-test-offload.md`; `docs/as-built/merge-gate-and-test-speed.md`; `docs/as-built/message-clarity.md`; `docs/as-built/nextest-width-and-lint-scope.md`; `docs/as-built/phase-tables.md`; `docs/as-built/plan-delegate-progress-history.md`; `docs/as-built/review-pool-probes.md`; `docs/as-built/review-pool.md`; `docs/as-built/review-regime-trial.md`; `docs/as-built/screen-record.md`; `docs/as-built/session-notifier.md`; `docs/as-built/session-roster.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/token-efficiency-study.md`; `docs/as-built/unit-board.md`; `docs/as-built/unit-rename.md`; `docs/as-built/usage-ledger.md`; `docs/decision_criteria.md`; `docs/delegate/compose_work_order.md`; `docs/delegate/dual_review.md`; `docs/delegate/final_gate_commit.md`; `docs/delegate/phase_end.md`; `docs/delegate/run_phase_review.md`; `docs/delegate/write_prompt_contract.md`; `docs/delegate_footprint_review.md`; `docs/delegate_plan_format.md`; `docs/explain_on_demand.md`; `docs/production_format.md`; `docs/quota_alerts.md`; `docs/style_production.md`; `docs/user_facing_explanation.md`; `scripts/bevy_upgrade/upgrade_report.py`; `scripts/build_hold/build_hold.py`; `scripts/build_hold/override_guard.sh`; `scripts/buildlog/index.py`; `scripts/buildlog/mac_report.py`; `scripts/buildlog/report.py`; `scripts/buildlog/rust_release.py`; `scripts/claude_to_codex/sync.py`; `scripts/direct/review_pool_report.py`; `scripts/direct/verify.sh`; `scripts/hana_shot/hana_shot.py`; `scripts/herdr/herdr.py`; `scripts/herdr/herdr_sessions.py`; `scripts/hooks/conversation_pause.py`; `scripts/hooks/post-tool-use-banned-words.py`; `scripts/hooks/pre-tool-use-push-gate.py`; `scripts/hooks/pre-tool-use-tool-output-read.py`; `scripts/hooks/stop-direct-continue.py`; `scripts/lib/urgency.py`; `scripts/mac_test/mac_test.py`; `scripts/message/escalate.py`; `scripts/message/send.py`; `scripts/model_study/report.py`; `scripts/model_study/results.py`; `scripts/notify/reply_asks.py`; `scripts/notify/review.py`; `scripts/notify/user_action.py`; `scripts/prioritize/install_watcher.sh`; `scripts/production/add_unit.py`; `scripts/production/codex_winddown.py`; `scripts/production/dailies_input.py`; `scripts/production/dailies_render.py`; `scripts/production/fake_showrunner.py`; `scripts/production/proposal_issue.py`; `scripts/production/review_regime.py`; `scripts/production/roles_audit.py`; `scripts/production/showrunner_move.py`; `scripts/production/showrunners.py`; `scripts/production/update_registration.py`; `scripts/shutdown/restart.py`; `scripts/shutdown/settle.py`; `scripts/whoami/five_hour.py`; `scripts/whoami/quota_alert.py` |
| showrunner | orchestrator | 18 | 6 | `commands/plan/phase_review.md`; `commands/plan/shrink.md`; `commands/plan/to_as_built.md`; `commands/plan/to_phased_plan.md`; `commands/team_review.md`; `docs/delegate_plan_format.md` |
| unit director | orchestrator | 18 | 6 | `commands/plan/phase_review.md`; `commands/plan/shrink.md`; `commands/plan/to_as_built.md`; `commands/plan/to_phased_plan.md`; `commands/team_review.md`; `docs/delegate_plan_format.md` |
| worker | seat | 58 | 12 | `commands/showrunner/rename_unit.md`; `commands/unit/direct.md`; `docs/as-built/agent-registry.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/unit-rename.md`; `docs/delegate/dual_review.md`; `scripts/agents/codex_mesh.py`; `scripts/agents/mesh_runs.py`; `scripts/direct/implement.sh`; `scripts/production/rename_unit.py`; `scripts/shutdown/restart.py` |
| worker | Codex seat | 9 | 4 | `commands/showrunner/rename_unit.md`; `commands/unit/direct.md`; `docs/as-built/shutdown-and-restart.md`; `scripts/production/rename_unit.py` |
| worker | delegate | 177 | 41 | `CLAUDE.md`; `commands/agent.md`; `commands/history.md`; `commands/lint_config.md`; `commands/nightly_review.md`; `commands/plan/phase_review.md`; `commands/plan/shrink.md`; `commands/plan/to_phased_plan.md`; `commands/producer/to_production.md`; `commands/showrunner/produce.md`; `commands/showrunner/promote_showrunner.md`; `commands/unit/direct.md`; `commands/unit/style_review.md`; `commands/watcher.md`; `docs/as-built/agent-registry.md`; `docs/as-built/build-followups-mul-add.md`; `docs/as-built/build-followups.md`; `docs/as-built/build-memory-admission.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/director-model-study.md`; `docs/as-built/herdr-trial.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/merge-gate-and-test-speed.md`; `docs/as-built/message-clarity.md`; `docs/as-built/plan-delegate-progress-history.md`; `docs/as-built/review-pool.md`; `docs/as-built/session-notifier.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/usage-ledger.md`; `docs/delegate/launch_implementation.md`; `docs/delegate/run_phase_review.md`; `docs/delegate/write_prompt_contract.md`; `docs/delegate_footprint_review.md`; `docs/delegate_plan_format.md`; `scripts/agents/codex_mesh.py`; `scripts/buildlog/index.py`; `scripts/direct/findings.py`; `scripts/hooks/session-start-delegate-resume.py`; `scripts/message/roster.py`; `scripts/model_study/turns.py`; `scripts/production/promote_showrunner.py` |
| worker | Codex worker | 36 | 21 | `commands/announce.md`; `commands/hana_shot.md`; `commands/message.md`; `commands/unit/direct.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/as-built/build-followups-mul-add.md`; `docs/as-built/build-targets.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/mac-test-offload.md`; `docs/as-built/plan-delegate-progress-history.md`; `docs/as-built/session-roster.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/usage-ledger.md`; `docs/delegate/write_prompt_contract.md`; `docs/production_format.md`; `scripts/buildlog/port_lint.py`; `scripts/message/send.py`; `scripts/shutdown/settle.py`; `scripts/usage/waste_watch.py` |
| reviewer | pooled reviewer | 7 | 4 | `docs/as-built/long-lived-workers.md`; `docs/as-built/review-pool.md`; `docs/delegate/dual_review.md`; `docs/delegate/phase_end.md` |
| template | review template | 0 | 0 | none |
| lease | review lease | 0 | 0 | none |
| merge branch | trunk | 26 | 8 | `commands/berth_fix.md`; `commands/plan/to_as_built.md`; `commands/showrunner/dailies.md`; `commands/showrunner/produce.md`; `docs/as-built/director-model-study-results.md`; `docs/as-built/director-model-study.md`; `docs/as-built/review-pool.md`; `docs/production_format.md` |
| parent showrunner | parent | 117 | 36 | `commands/close_issue.md`; `commands/implement_issue.md`; `commands/module_review.md`; `commands/plan/to_phased_plan.md`; `commands/producer/to_production.md`; `commands/release.md`; `commands/showrunner/add_showrunner.md`; `commands/showrunner/add_style.md`; `commands/showrunner/attach.md`; `commands/showrunner/produce.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/as-built/build-memory-admission.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/feature-log.md`; `docs/as-built/herdr-adoption.md`; `docs/as-built/herdr-restore.md`; `docs/as-built/phase-tables.md`; `docs/as-built/session-roster.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/unit-board.md`; `docs/as-built/usage-ledger.md`; `docs/production_format.md`; `docs/style_production.md`; `scripts/agents/codex_mesh.py`; `scripts/buildlog/index.py`; `scripts/hana_shot/hana_shot.py`; `scripts/notify/pushover.py`; `scripts/production/merge_checkpoint.py`; `scripts/production/production_lifecycle.py`; `scripts/production/rename.py`; `scripts/production/showrunners.py`; `scripts/style/add_style.py`; `scripts/style/candidate_generators.py`; `scripts/style/style_plan.py`; `scripts/style/style_proposals.py` |
| child showrunner | lower showrunner | 13 | 4 | `commands/showrunner/add_showrunner.md`; `commands/showrunner/produce.md`; `docs/as-built/herdr-adoption.md`; `scripts/production/dailies_render.py` |
| root showrunner | root | 256 | 72 | `commands/add_banned_word.md`; `commands/adhoc_review.md`; `commands/api_review.md`; `commands/bevy_migration_plan.md`; `commands/change_review.md`; `commands/changelog.md`; `commands/focused_eval.md`; `commands/hana_shot.md`; `commands/implement_issue.md`; `commands/issue.md`; `commands/mend_fix.md`; `commands/module_review.md`; `commands/new_rust_project.md`; `commands/new_style.md`; `commands/plan/to_as_built.md`; `commands/producer/to_production.md`; `commands/release.md`; `commands/showrunner/attach.md`; `commands/showrunner/dailies.md`; `commands/showrunner/produce.md`; `commands/showrunner/rename.md`; `commands/team_review.md`; `commands/unit/direct.md`; `commands/validate_and_push.md`; `commands/watcher.md`; `docs/as-built/agent-registry.md`; `docs/as-built/bevy-upgrade.md`; `docs/as-built/build-followups.md`; `docs/as-built/build-memory-admission.md`; `docs/as-built/build-targets.md`; `docs/as-built/buildlog-memory-stalls.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/director-model-study.md`; `docs/as-built/feature-log.md`; `docs/as-built/herdr-adoption.md`; `docs/as-built/herdr-trial.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/merge-gate-and-test-speed.md`; `docs/as-built/nextest-width-and-lint-scope.md`; `docs/as-built/phase-checks.md`; `docs/as-built/phase-tables.md`; `docs/as-built/production-rename.md`; `docs/as-built/review-pool-probes.md`; `docs/as-built/review-pool.md`; `docs/as-built/session-notifier.md`; `docs/as-built/session-roster.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/unit-board.md`; `docs/as-built/unit-rename.md`; `docs/production_format.md`; `docs/style_production.md`; `scripts/agents/codex_mesh.py`; `scripts/berth/work_order.py`; `scripts/buildlog/index.py`; `scripts/claude_to_codex/sync.py`; `scripts/direct/remove_workers.py`; `scripts/direct/review_pool_report.py`; `scripts/hana_shot/hana_shot.py`; `scripts/herdr/herdr.py`; `scripts/lib/unit_runs.py`; `scripts/new_rust_project/rust_generate.sh`; `scripts/production/dailies_render.py`; `scripts/production/merge_checkpoint.py`; `scripts/production/production_lifecycle.py`; `scripts/production/roles_audit.py`; `scripts/production/showrunners.py`; `scripts/production/unit_run.py`; `scripts/shot_report/shot_report.py`; `scripts/style/style_plan.py`; `scripts/style/style_sync.py`; `scripts/usage/agents_log.py` |
| root showrunner | top-level showrunner | 7 | 5 | `commands/change_review.md`; `commands/next.md`; `commands/producer/to_production.md`; `docs/as-built/showrunner-automation.md`; `docs/production_format.md` |
| production doc | production document | 2 | 1 | `scripts/direct/phase_table.py` |
| unit plan | delegate-ready plan | 7 | 6 | `commands/plan/phase_review.md`; `commands/plan/to_as_built.md`; `commands/plan/to_phased_plan.md`; `commands/producer/to_production.md`; `docs/delegate/compose_work_order.md`; `docs/delegate_plan_format.md` |
| as-built | as-built overview | 3 | 2 | `commands/plan/to_as_built.md`; `docs/delegate_plan_format.md` |
| as-built | as-built doc | 24 | 11 | `commands/close_issue.md`; `commands/plan/to_as_built.md`; `commands/showrunner/produce.md`; `commands/showrunner/proposal.md`; `commands/unit/direct.md`; `docs/as-built/conversation-pause.md`; `docs/as-built/unit-board.md`; `docs/delegate/final_gate_commit.md`; `docs/delegate_plan_format.md`; `docs/production_format.md`; `scripts/production/proposal_issue.py` |
| add-on | next item | 4 | 4 | `commands/adhoc_review.md`; `commands/plan/phase_review.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/delegate/run_phase_review.md` |
| proposal | candidate | 61 | 35 | `commands/add_banned_word.md`; `commands/commit_prep.md`; `commands/efficiency_next.md`; `commands/implement_issue.md`; `commands/issue.md`; `commands/next.md`; `commands/nightly_review.md`; `commands/plan/phase_review.md`; `commands/producer/greenlight.md`; `commands/release.md`; `commands/showrunner/nightly.md`; `commands/showrunner/produce.md`; `commands/showrunner/proposal.md`; `commands/unit/add_ons.md`; `commands/unit/direct.md`; `commands/ux_eval.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/as-built/build-targets.md`; `docs/as-built/director-model-study-results.md`; `docs/as-built/director-model-study.md`; `docs/as-built/mac-test-offload.md`; `docs/as-built/phase-tables.md`; `docs/as-built/review-pool.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/usage-ledger.md`; `docs/quota_alerts.md`; `scripts/direct/progress_history.py`; `scripts/model_study/compare.py`; `scripts/model_study/report.py`; `scripts/model_study/results.py`; `scripts/nightly_review/candidates.py`; `scripts/shot_report/shot_report.py`; `scripts/style/style_ledger.py`; `scripts/style/style_plan.py` |
| proposal | add proposal | 0 | 0 | none |
| change | change-review row | 1 | 1 | `docs/as-built/showrunner-automation.md` |
| dailies | scheduled update | 9 | 3 | `commands/showrunner/produce.md`; `docs/as-built/session-notifier.md`; `docs/as-built/showrunner-automation.md` |
| checkpoint | code commit | 13 | 6 | `commands/plan/shrink.md`; `commands/unit/checkpoint.md`; `commands/unit/direct.md`; `docs/as-built/showrunner-automation.md`; `docs/delegate/phase_end.md`; `docs/delegate_plan_format.md` |
| checkpoint | code ready notice | 1 | 1 | `docs/delegate/phase_end.md` |
| checkpoint | code checkpoint | 18 | 7 | `commands/plan/phase_review.md`; `commands/showrunner/produce.md`; `commands/unit/checkpoint.md`; `commands/unit/eta_breakdown.md`; `docs/as-built/showrunner-automation.md`; `scripts/production/merge_checkpoint.py`; `scripts/production/production_lifecycle.py` |
| phase review | plan review | 5 | 5 | `commands/unit/checkpoint.md`; `docs/as-built/phase-checks.md`; `docs/as-built/showrunner-automation.md`; `docs/delegate/phase_end.md`; `scripts/direct/progress_history.py` |
| /unit:direct | /unit:delegate | 9 | 6 | `CLAUDE.md`; `commands/showrunner/promote_showrunner.md`; `docs/as-built/agent-registry.md`; `docs/as-built/director-model-study.md`; `scripts/model_study/turns.py`; `scripts/production/promote_showrunner.py` |
| astra-designer | chief designer | 0 | 0 | none |
| astra-reviewer | chief reviewer | 0 | 0 | none |
| small-fix helper | subagent | 57 | 10 | `commands/bevy_migration_plan.md`; `commands/plan/phase_review.md`; `commands/plan/shrink.md`; `commands/plan/to_as_built.md`; `commands/plan/to_phased_plan.md`; `docs/accounts.md`; `docs/as-built/build-memory-admission.md`; `docs/as-built/director-model-study.md`; `docs/as-built/usage-ledger.md`; `scripts/bevy_migration_plan/bevy_migration_get_tranche.py` |
| small-fix helper | helper | 58 | 24 | `CLAUDE.md`; `commands/apply_example_layout.md`; `commands/commit_prep.md`; `commands/focused_eval.md`; `commands/idle_timeout.md`; `commands/prioritize.md`; `commands/screen_record.md`; `commands/showrunner/dailies.md`; `commands/showrunner/produce.md`; `commands/unit/direct.md`; `commands/ux_eval.md`; `docs/as-built/build-targets.md`; `docs/as-built/herdr-trial.md`; `docs/as-built/merge-gate-and-test-speed.md`; `docs/as-built/message-clarity.md`; `docs/as-built/nextest-width-and-lint-scope.md`; `docs/as-built/review-pool.md`; `docs/as-built/screen-record.md`; `docs/as-built/session-roster.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/delegate/write_prompt_contract.md`; `docs/production_format.md`; `scripts/production/roles_audit.py` |
| pending decision | Pending decision block | 0 | 0 | none |
| finding | issue | 250 | 45 | `commands/adhoc_review.md`; `commands/api_review.md`; `commands/apply_example_layout.md`; `commands/astra_review.md`; `commands/berth_fix.md`; `commands/changelog.md`; `commands/close_issue.md`; `commands/forest_and_trees.md`; `commands/implement_issue.md`; `commands/issue.md`; `commands/module_review.md`; `commands/next.md`; `commands/plan/phase_review.md`; `commands/prioritize.md`; `commands/showrunner/proposal.md`; `commands/showrunner/proposals.md`; `commands/team_review.md`; `commands/unit/direct.md`; `docs/as-built/bevy-upgrade.md`; `docs/as-built/build-followups-fn-length-hook.md`; `docs/as-built/feature-log.md`; `docs/as-built/herdr-adoption.md`; `docs/as-built/herdr-restore.md`; `docs/as-built/herdr-trial.md`; `docs/as-built/review-pool.md`; `docs/as-built/showrunner-automation.md`; `docs/as-built/shutdown-and-restart.md`; `docs/delegate/final_gate_commit.md`; `docs/delegate_plan_format.md`; `docs/production_format.md`; `docs/style_production.md`; `docs/user_facing_explanation.md`; `scripts/bevy_migration_plan/bevy_migration_dependency_check.py`; `scripts/bevy_upgrade/upgrade_record.py`; `scripts/hooks/post-tool-use-basedpyright.py`; `scripts/prioritize/renumber.py`; `scripts/prioritize/snapshot.py`; `scripts/prioritize/watch_signature.py`; `scripts/production/proposal_issue.py`; `scripts/production/roles_audit.py`; `scripts/shutdown/restart.py`; `scripts/shutdown/settle.py`; `scripts/style/style_ledger.py`; `scripts/style/style_plan.py`; `scripts/style/style_proposals.py` |
| reservation | phase reservation | 15 | 3 | `commands/unit/direct.md`; `docs/delegate/final_gate_commit.md`; `scripts/direct/phase_reservation.py` |
| reservation | delegated phase reservation | 0 | 0 | none |
| ETA | phase ETA | 8 | 4 | `commands/showrunner/dailies.md`; `commands/showrunner/produce.md`; `commands/unit/eta.md`; `commands/unit/eta_breakdown.md` |

</details>

## Baseline

Command from this checkout: `~/.claude/scripts/lib/py scripts/production/roles_audit.py baseline`. This slot ran it once, detached; exit 0. The complete output follows. The live file set is 546 files under `commands/`, `docs/` except `docs/plans/`, `skills/`, `CLAUDE.md` and user-facing script strings; tests and fixtures are excluded. The message window is [2026-10-06T17:40:37.360928-04:00, 2026-10-20T17:40:37.360928-04:00) America/New_York. Production and message records are natedev only. `change` has one defining file, `commands/change_review.md`; its other meanings in `commands/changelog.md` and `commands/commit_prep.md` are excluded, so it is absent from the Definitions findings.

<details>
<summary>Complete baseline output</summary>

```text
Repository: /home/natepiano/worktrees/claude-build-followups-sdlc-roles
Roles baseline
Root file set: commands/; docs/ except docs/plans/; skills/; CLAUDE.md; scripts/ strings
Window: [2026-10-06T17:40:37.360928-04:00, 2026-10-20T17:40:37.360928-04:00) America/New_York
Static script strings; no execution or expansion. Tests and fixtures excluded.
Python references use lexical scope; helper call arguments are not followed into parameters.
Names use exact phrases, ignoring case; plurals count only when listed.
Each term/name pair is a separate count; shared other names count for each term.
Production and message sources: natedev only.

Names
Term                Name                         Form     Hits (n)  Files (n)  File set
production          production                   defined  761       105        live: n=546 files
executive producer  executive producer           defined  4         2          live: n=546 files
executive producer  user                         other    1484      189        live: n=546 files
producer            producer                     defined  23        8          live: n=546 files
showrunner          showrunner                   defined  824       118        live: n=546 files
showrunner          orchestrator                 other    18        6          live: n=546 files
unit                unit                         defined  2044      165        live: n=546 files
unit director       unit director                defined  247       57         live: n=546 files
unit director       orchestrator                 other    18        6          live: n=546 files
worker              worker                       defined  779       81         live: n=546 files
worker              seat                         other    58        12         live: n=546 files
worker              Codex seat                   other    9         4          live: n=546 files
worker              delegate                     other    177       41         live: n=546 files
worker              Codex worker                 other    36        21         live: n=546 files
review pool         review pool                  defined  35        12         live: n=546 files
reviewer            reviewer                     defined  252       40         live: n=546 files
reviewer            pooled reviewer              other    7         4          live: n=546 files
template            template                     defined  84        23         live: n=546 files
template            review template              other    0         0          live: n=546 files
lease               lease                        defined  25        6          live: n=546 files
lease               review lease                 other    0         0          live: n=546 files
merge branch        merge branch                 defined  159       30         live: n=546 files
merge branch        trunk                        other    26        8          live: n=546 files
gate                gate                         defined  420       78         live: n=546 files
parent showrunner   parent showrunner            defined  0         0          live: n=546 files
parent showrunner   parent                       other    117       36         live: n=546 files
child showrunner    child showrunner             defined  7         5          live: n=546 files
child showrunner    lower showrunner             other    13        4          live: n=546 files
root showrunner     root showrunner              defined  3         2          live: n=546 files
root showrunner     root                         other    256       72         live: n=546 files
root showrunner     top-level showrunner         other    7         5          live: n=546 files
production doc      production doc               defined  160       51         live: n=546 files
production doc      production document          other    2         1          live: n=546 files
unit plan           unit plan                    defined  11        6          live: n=546 files
unit plan           delegate-ready plan          other    7         6          live: n=546 files
Work Order          Work Order                   defined  159       32         live: n=546 files
as-built            as-built                     defined  140       29         live: n=546 files
as-built            as-built overview            other    3         2          live: n=546 files
as-built            as-built doc                 other    24        11         live: n=546 files
add-on              add-on                       defined  15        8          live: n=546 files
add-on              next item                    other    4         4          live: n=546 files
proposal            proposal                     defined  194       35         live: n=546 files
proposal            candidate                    other    61        35         live: n=546 files
proposal            add proposal                 other    0         0          live: n=546 files
change              change                       defined  576       132        live: n=546 files
change              change-review row            other    1         1          live: n=546 files
dailies             dailies                      defined  150       22         live: n=546 files
dailies             scheduled update             other    9         3          live: n=546 files
checkpoint          checkpoint                   defined  339       53         live: n=546 files
checkpoint          code commit                  other    13        6          live: n=546 files
checkpoint          code ready notice            other    1         1          live: n=546 files
checkpoint          code checkpoint              other    18        7          live: n=546 files
shrink              shrink                       defined  101       19         live: n=546 files
phase review        phase review                 defined  14        9          live: n=546 files
phase review        plan review                  other    5         5          live: n=546 files
landing call        landing call                 defined  1         1          live: n=546 files
/unit:direct        /unit:direct                 defined  163       56         live: n=546 files
/unit:direct        /unit:delegate               other    9         6          live: n=546 files
astra-designer      astra-designer               defined  0         0          live: n=546 files
astra-designer      chief designer               other    0         0          live: n=546 files
astra-reviewer      astra-reviewer               defined  0         0          live: n=546 files
astra-reviewer      chief reviewer               other    0         0          live: n=546 files
small-fix helper    small-fix helper             defined  0         0          live: n=546 files
small-fix helper    subagent                     other    57        10         live: n=546 files
small-fix helper    helper                       other    58        24         live: n=546 files
pending decision    pending decision             defined  25        13         live: n=546 files
pending decision    Pending decision block       other    0         0          live: n=546 files
finding             finding                      defined  353       49         live: n=546 files
finding             issue                        other    250       45         live: n=546 files
reservation         reservation                  defined  120       25         live: n=546 files
reservation         phase reservation            other    15        3          live: n=546 files
reservation         delegated phase reservation  other    0         0          live: n=546 files
ETA                 ETA                          defined  349       26         live: n=546 files
ETA                 phase ETA                    other    8         4          live: n=546 files
TOTAL other / all   2771 / 11308                 24.5%    2771      —          live: n=546 files

Definitions (declared sources plus explicit **Name** — definitions; other-meaning files excluded)
Term              Finding         Files                                                                                                                  Files (n)  File set
worker            multiple files  docs/as-built/session-roster.md; docs/production_format.md                                                             2          live + declared: n=36 terms
gate              multiple files  commands/unit/direct.md; docs/production_format.md                                                                     2          live + declared: n=36 terms
astra-designer    multiple files  /home/natepiano/worktrees/claude-build-followups-sdlc-guilds/docs/plans/sdlc-guilds.md; docs/plans/sdlc-production.md  2          live + declared: n=36 terms
astra-reviewer    multiple files  /home/natepiano/worktrees/claude-build-followups-sdlc-guilds/docs/plans/sdlc-guilds.md; docs/plans/sdlc-production.md  2          live + declared: n=36 terms
small-fix helper  no defined_in   none                                                                                                                   0          live + declared: n=36 terms

Unit and session names — natedev only; all Units rows, including retired rows
Finding      Name           Productions                                             Files                                                                                                                                                                                                                                                             Docs (n)  File set
unit format  mul_add-unit   build-followups                                         /home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md                                                                                                                                                                   1         productions: n=5 docs
unit format  cargo-tile     remove-running-section                                  /home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md                                                                                                                                                                             1         productions: n=5 docs
shared name  notifier-unit  build-followups; tool-based-ui                          /home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md; /home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md                                                                                         2         productions: n=5 docs
shared name  review-unit    build-followups; remove-running-section; tool-based-ui  /home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md; /home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md; /home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md  3         productions: n=5 docs

Wrong-party asks — natedev only; sent contacts, request intent not recorded
Sender                                         Director to user (n)  Director to other showrunner (n)  Role unknown (n)    Production unknown (n)  Sent (n)  Window
acct-probe                                     0                     0                                 not recorded: 1     0                       1         window
add_unit                                       0                     0                                 not recorded: 2     0                       2         window
bevy-upgrade                                   0                     0                                 not recorded: 6     0                       6         window
bevy020-watch                                  0                     0                                 not recorded: 18    0                       18        window
build-report-unit                              0                     0                                 not recorded: 86    0                       86        window
cargo-liner-berth-parallel-impl                0                     0                                 not recorded: 24    0                       24        window
cargo-liner-berth-parallel-impl2               0                     0                                 not recorded: 3     0                       3         window
cargo-liner-berth-parallel-test                0                     0                                 not recorded: 13    0                       13        window
cargo-liner-handler-enh-impl                   0                     0                                 not recorded: 57    0                       57        window
cargo-liner-handler-enh-test                   0                     0                                 not recorded: 11    0                       11        window
cargo-liner-style                              0                     0                                 not recorded: 356   0                       356       window
cargo-liner-style-modules-impl                 0                     0                                 not recorded: 2     0                       2         window
cargo-liner-style-modules-test                 0                     0                                 not recorded: 1     0                       1         window
cargo-liner-style-types-test                   0                     0                                 not recorded: 1     0                       1         window
claude-build-followups-herdr-trial-fix         0                     0                                 not recorded: 7     0                       7         window
claude-build-followups-herdr-trial-fix2        0                     0                                 not recorded: 1     0                       1         window
claude-build-followups-herdr-trial-test        0                     0                                 not recorded: 2     0                       2         window
claude-build-followups-mul-add-impl            0                     0                                 not recorded: 5     0                       5         window
claude-build-followups-style-impl              0                     0                                 not recorded: 5     0                       5         window
claude-build-followups-style-impl2             0                     0                                 not recorded: 1     0                       1         window
claude-build-followups-style-test              0                     0                                 not recorded: 2     0                       2         window
claude-build-followups-two-accounts-impl       0                     0                                 not recorded: 28    0                       28        window
claude-build-followups-two-accounts-test       0                     0                                 not recorded: 19    0                       19        window
codex capacity                                 0                     0                                 not recorded: 1     0                       1         window
codex-seats                                    0                     0                                 not recorded: 8     0                       8         window
codex-sign-in                                  0                     0                                 not recorded: 10    0                       10        window
codex-winddown                                 0                     0                                 not recorded: 20    0                       20        window
codex-workers                                  0                     0                                 not recorded: 8     0                       8         window
conversation-pause                             0                     0                                 not recorded: 1147  0                       1147      window
delegate-020d630c-df93-427c-b02c-5807a0a826a9  0                     0                                 not recorded: 19    0                       19        window
delegate-03f9b80e-dbe5-47c6-b29e-a7e6587ab721  0                     0                                 not recorded: 30    0                       30        window
delegate-08505f2b-440e-47d6-9096-a0f1da508f9f  0                     0                                 not recorded: 3     0                       3         window
delegate-0f84824c-34da-4f18-9e29-1fd4a71f8a7c  0                     0                                 not recorded: 12    0                       12        window
delegate-12757803-5169-4aa4-801b-13313a8ad8de  0                     0                                 not recorded: 4     0                       4         window
delegate-19dbaec7-7cc3-459e-98c9-8d2b97e6f2f7  0                     0                                 not recorded: 23    0                       23        window
delegate-2426301b-4a1c-46ba-aee8-10b4646684fc  0                     0                                 not recorded: 11    0                       11        window
delegate-25d2bbf7-0df4-48e0-96b0-9550d1faee23  0                     0                                 not recorded: 1     0                       1         window
delegate-267796d6-5cb2-4c02-84ff-d0c9671bcb6e  0                     0                                 not recorded: 1     0                       1         window
delegate-361a1405-84ee-4368-92b7-4ee936e3a6d0  0                     0                                 not recorded: 2     0                       2         window
delegate-39d29d76-1b9a-42ed-bf7d-b931afddbc96  0                     0                                 not recorded: 2     0                       2         window
delegate-3b67e19c-5dea-4a82-92a5-35812917e3b2  0                     0                                 not recorded: 22    0                       22        window
delegate-3d1e70e5-f0a0-4be9-9f85-37a7ef43e4b9  0                     0                                 not recorded: 2     0                       2         window
delegate-40986759-79ca-42db-9dca-333ca7d8176f  0                     0                                 not recorded: 4     0                       4         window
delegate-43da9458-88a0-4fa5-858a-268e0a628361  0                     0                                 not recorded: 28    0                       28        window
delegate-4a878237-614e-4a17-9d09-973c5b3f0481  0                     0                                 not recorded: 4     0                       4         window
delegate-4f7e11a0-ddf9-44ef-8375-b46890c78e2e  0                     0                                 not recorded: 5     0                       5         window
delegate-5aa5a840-f811-4de6-bfc2-0dd88cf00674  0                     0                                 not recorded: 10    0                       10        window
delegate-5d3494d9-53c5-40b8-91fa-6aa859be35b2  0                     0                                 not recorded: 22    0                       22        window
delegate-5ea59d53-eafd-43b1-916f-b43d57a9a915  0                     0                                 not recorded: 51    0                       51        window
delegate-6062c984-0ae5-49d2-a4e4-55b03a5a2188  0                     0                                 not recorded: 7     0                       7         window
delegate-61cc30d4-b1a1-4223-a157-6c707fdfca67  0                     0                                 not recorded: 56    0                       56        window
delegate-62b3d9fb-1941-4d64-9e2a-feff681e1f3e  0                     0                                 not recorded: 10    0                       10        window
delegate-64b259cd-5f19-45d1-8ca4-74333db656aa  0                     0                                 not recorded: 90    0                       90        window
delegate-6a6430e9-bfa9-4146-8645-a551ea8cb1e4  0                     0                                 not recorded: 2     0                       2         window
delegate-6ad5ac5d-0a0a-4663-94d7-e43c657b84a5  0                     0                                 not recorded: 4     0                       4         window
delegate-70cb2c04-c877-44a5-b737-d5dff99e9c2b  0                     0                                 not recorded: 2     0                       2         window
delegate-73457307-85e0-4b3c-965b-91d8ec00f251  0                     0                                 not recorded: 1     0                       1         window
delegate-77703ad9-d2a1-4610-b66b-f987b0725677  0                     0                                 not recorded: 49    0                       49        window
delegate-78b7ab7e-ebf0-4baa-bfda-29a8c93f5914  0                     0                                 not recorded: 5     0                       5         window
delegate-7b115092-80c3-42c2-9652-2ff222d2c722  0                     0                                 not recorded: 1     0                       1         window
delegate-7ccc56fd-9926-49ce-9014-3332974aad76  0                     0                                 not recorded: 47    0                       47        window
delegate-833eaeaf-438e-4915-8332-40d83a30ad79  0                     0                                 not recorded: 3     0                       3         window
delegate-854c582c-e6eb-4d95-a750-659035c5fd66  0                     0                                 not recorded: 1     0                       1         window
delegate-935bc85e-9d4c-44ad-984b-f78b29127059  0                     0                                 not recorded: 4     0                       4         window
delegate-a6821059-ef6a-431c-8a7a-3c5c33012203  0                     0                                 not recorded: 4     0                       4         window
delegate-a702c130-c737-4e4c-99bb-c6524ebaf811  0                     0                                 not recorded: 88    0                       88        window
delegate-a96772a3-2bc9-4a19-b710-1a8719efaa7d  0                     0                                 not recorded: 10    0                       10        window
delegate-ae29f789-b826-4326-b294-a5810bb6e127  0                     0                                 not recorded: 10    0                       10        window
delegate-b6d7dacc-c832-478e-8659-f45d7988848e  0                     0                                 not recorded: 5     0                       5         window
delegate-bdc0d8c0-54f1-4e45-a63a-f9afdb5704f1  0                     0                                 not recorded: 5     0                       5         window
delegate-c2d79f66-72ea-40e9-a9ee-13a51a42bec2  0                     0                                 not recorded: 7     0                       7         window
delegate-c7c4abcf-9b3a-4543-92ab-a28295646ca3  0                     0                                 not recorded: 27    0                       27        window
delegate-d51d9b1c-d802-4070-a394-48816796da73  0                     0                                 not recorded: 7     0                       7         window
delegate-dd07f934-939e-4581-951a-96690a0a33de  0                     0                                 not recorded: 14    0                       14        window
delegate-df477c65-1d02-4c11-8810-b61c83db1f2c  0                     0                                 not recorded: 41    0                       41        window
delegate-e41a69e0-98c1-4b1f-a6ef-656b6fa0d2e4  0                     0                                 not recorded: 22    0                       22        window
delegate-ee9ef9e2-6596-4ef8-a9db-8c89d362f093  0                     0                                 not recorded: 85    0                       85        window
delegate-f0ad402c-96a6-49e5-baef-1f6bda7dcaca  0                     0                                 not recorded: 16    0                       16        window
delegate-f6cf2c4f-858a-494f-bf69-6133ba872601  0                     0                                 not recorded: 69    0                       69        window
delegate-f80cae0a-9b69-4fa7-a0b3-a44b032347b6  0                     0                                 not recorded: 1     0                       1         window
delegate-f8fd45af-016b-4de6-a608-634cb3c66c51  0                     0                                 not recorded: 5     0                       5         window
delegate-f9fc3663-52fa-443f-b0a1-29c568bb97b7  0                     0                                 not recorded: 11    0                       11        window
delegate-fcc29db9-0f75-4fc1-bc90-0d6f21d0530c  0                     0                                 not recorded: 36    0                       36        window
diagrams                                       0                     0                                 not recorded: 2     0                       2         window
disk_floor                                     0                     0                                 not recorded: 115   0                       115       window
escalate                                       0                     0                                 not recorded: 4     0                       4         window
flake-fix (impl)                               0                     0                                 not recorded: 3     0                       3         window
frame                                          0                     0                                 not recorded: 4     0                       4         window
gates and triggers                             0                     0                                 not recorded: 2     0                       2         window
gesture                                        0                     0                                 not recorded: 1     0                       1         window
hana                                           0                     0                                 not recorded: 425   0                       425       window
hana_catalyst-cables-impl                      0                     0                                 not recorded: 2     0                       2         window
hana_catalyst-inspectors-impl                  0                     0                                 not recorded: 3     0                       3         window
hana_catalyst-inspectors-impl3                 0                     0                                 not recorded: 1     0                       1         window
hana_catalyst-inspectors-impl5                 0                     0                                 not recorded: 3     0                       3         window
hana_catalyst-lfo-test                         0                     0                                 not recorded: 54    0                       54        window
hana_catalyst-research-impl                    0                     0                                 not recorded: 8     0                       8         window
hanadocs                                       0                     0                                 not recorded: 2     0                       2         window
handler-enh-unit                               0                     0                                 not recorded: 15    0                       15        window
handler-unit                                   0                     0                                 not recorded: 5     0                       5         window
herdr-trial-unit                               0                     0                                 not recorded: 115   0                       115       window
issue manager                                  0                     0                                 not recorded: 1     0                       1         window
launcher                                       0                     0                                 not recorded: 1     0                       1         window
lfo                                            0                     0                                 not recorded: 1     0                       1         window
lfo-p4-fix                                     0                     0                                 not recorded: 207   0                       207       window
lfo-unit                                       0                     0                                 not recorded: 1     0                       1         window
mac-test                                       0                     0                                 not recorded: 21    0                       21        window
mat-fix2                                       0                     0                                 not recorded: 10    0                       10        window
mat-fix3                                       0                     0                                 not recorded: 3     0                       3         window
mat-test                                       0                     0                                 not recorded: 2     0                       2         window
merge-gate-unit                                0                     0                                 not recorded: 312   0                       312       window
mesh-runs                                      0                     0                                 not recorded: 16    0                       16        window
model-study                                    0                     0                                 not recorded: 1     0                       1         window
model-study-unit                               0                     0                                 not recorded: 121   0                       121       window
models-unit                                    0                     0                                 not recorded: 1     0                       1         window
modules                                        0                     0                                 not recorded: 330   0                       330       window
modules-unit                                   0                     0                                 not recorded: 13    0                       13        window
mul-add-first-fire                             0                     0                                 not recorded: 1     0                       1         window
mul-add-index-call                             0                     0                                 not recorded: 1     0                       1         window
mul-add-open-call                              0                     0                                 not recorded: 1     0                       1         window
mul-add-remeasure                              0                     0                                 not recorded: 1     0                       1         window
mul-add-rounds-remeasure                       0                     0                                 not recorded: 1     0                       1         window
mul_add                                        0                     0                                 not recorded: 1     0                       1         window
mul_add-unit                                   0                     0                                 not recorded: 17    0                       17        window
natedev                                        0                     0                                 not recorded: 1532  0                       1532      window
natemccoy-67                                   0                     0                                 not recorded: 1     0                       1         window
nightly-review timer                           0                     0                                 not recorded: 5     0                       5         window
organon-fix2                                   0                     0                                 not recorded: 1     0                       1         window
organon-frame                                  0                     0                                 not recorded: 1     0                       1         window
organon-offset                                 0                     0                                 not recorded: 3     0                       3         window
organon-seed                                   0                     0                                 not recorded: 5     0                       5         window
organon-t72                                    0                     0                                 not recorded: 12    0                       12        window
organon-w19                                    0                     0                                 not recorded: 3     0                       3         window
organon-w74                                    0                     0                                 not recorded: 12    0                       12        window
p41cam-test                                    0                     0                                 not recorded: 6     0                       6         window
p45-impl                                       0                     0                                 not recorded: 3     0                       3         window
pause-probe-sender                             0                     0                                 not recorded: 1     0                       1         window
pm-check2                                      0                     0                                 not recorded: 1     0                       1         window
pm-origin                                      0                     0                                 not recorded: 2     0                       2         window
pm-trial                                       0                     0                                 not recorded: 7     0                       7         window
push template review                           0                     0                                 not recorded: 5     0                       5         window
quota_alert                                    0                     0                                 not recorded: 313   0                       313       window
remove-running-section                         0                     0                                 not recorded: 79    0                       79        window
report-builds                                  0                     0                                 not recorded: 54    0                       54        window
research-unit                                  0                     0                                 not recorded: 12    0                       12        window
review-health-tool-based-ui                    0                     0                                 not recorded: 81    0                       81        window
rwprobe                                        0                     0                                 not recorded: 1     0                       1         window
screenshot-unit                                0                     0                                 not recorded: 21    0                       21        window
seat-count-timer                               0                     0                                 not recorded: 1     0                       1         window
send.py in accounts                            0                     0                                 not recorded: 2     0                       2         window
send.py in berth-parallel                      0                     0                                 not recorded: 56    0                       56        window
send.py in cables                              0                     0                                 not recorded: 1     0                       1         window
send.py in cargo-liner                         0                     0                                 not recorded: 14    0                       14        window
send.py in demo                                0                     0                                 not recorded: 192   0                       192       window
send.py in fps                                 0                     0                                 not recorded: 7     0                       7         window
send.py in hana                                0                     0                                 not recorded: 534   0                       534       window
send.py in hanadocs                            0                     0                                 not recorded: 1     0                       1         window
send.py in natedev                             0                     0                                 not recorded: 45    0                       45        window
send.py in nested-g2                           0                     0                                 not recorded: 1     0                       1         window
send.py in research                            0                     0                                 not recorded: 34    0                       34        window
send.py in showrunner                          0                     0                                 not recorded: 6     0                       6         window
send.py in startup                             0                     0                                 not recorded: 54    0                       54        window
send.py in startup-polish                      0                     0                                 not recorded: 1     0                       1         window
send.py in tile-fixes                          0                     0                                 not recorded: 51    0                       51        window
send.py in widget                              0                     0                                 not recorded: 14    0                       14        window
showrunner-timer-build-followups               0                     0                                 not recorded: 56    0                       56        window
showrunner-timer-hanadocs-info                 0                     0                                 not recorded: 21    0                       21        window
showrunner-timer-remove-running-section        0                     0                                 not recorded: 99    0                       99        window
showrunner-timer-tool-based-ui                 0                     0                                 not recorded: 83    0                       83        window
showrunners                                    0                     0                                 not recorded: 14    0                       14        window
shutdown                                       0                     0                                 not recorded: 781   0                       781       window
shutdown-unit                                  0                     0                                 not recorded: 35    0                       35        window
shutdown-unit live check                       0                     0                                 not recorded: 9     0                       9         window
slot arrange                                   0                     0                                 not recorded: 1     0                       1         window
stall-watch                                    0                     0                                 not recorded: 326   0                       326       window
startup                                        0                     0                                 not recorded: 84    0                       84        window
style-unit                                     0                     0                                 not recorded: 275   0                       275       window
style_sync                                     0                     0                                 not recorded: 1     0                       1         window
sweep                                          0                     0                                 not recorded: 1     0                       1         window
test7                                          0                     0                                 not recorded: 1     0                       1         window
tile-fixes-unit                                0                     0                                 not recorded: 9     0                       9         window
tile-unit                                      0                     0                                 not recorded: 3     0                       3         window
tmux-names                                     0                     0                                 not recorded: 14    0                       14        window
tool-based-ui-demo-fix                         0                     0                                 not recorded: 1     0                       1         window
tool-based-ui-demo-fix2                        0                     0                                 not recorded: 2     0                       2         window
tool-based-ui-demo-impl                        0                     0                                 not recorded: 67    0                       67        window
tool-based-ui-demo-impl2                       0                     0                                 not recorded: 56    0                       56        window
tool-based-ui-demo-review                      0                     0                                 not recorded: 16    0                       16        window
tool-based-ui-demo-test                        0                     0                                 not recorded: 33    0                       33        window
tool-based-ui-frame-time-impl                  0                     0                                 not recorded: 6     0                       6         window
tool-based-ui-frame-time-test                  0                     0                                 not recorded: 3     0                       3         window
tool-based-ui-geometry-material-fix            0                     0                                 not recorded: 2     0                       2         window
tool-based-ui-startup-panels                   0                     0                                 not recorded: 13    0                       13        window
tool-based-ui-startup-polish-fix2              0                     0                                 not recorded: 3     0                       3         window
tool-based-ui-startup-polish-impl              0                     0                                 not recorded: 1     0                       1         window
tool-based-ui-startup-polish-impl5             0                     0                                 not recorded: 3     0                       3         window
tool-based-ui-startup-polish-impl7             0                     0                                 not recorded: 25    0                       25        window
tool-based-ui-startup-polish-test              0                     0                                 not recorded: 1     0                       1         window
tool-based-ui-startup-polish-test7             0                     0                                 not recorded: 25    0                       25        window
tool-based-ui-trunk-fix                        0                     0                                 not recorded: 1     0                       1         window
tool-based-ui-trunk-fix2                       0                     0                                 not recorded: 3     0                       3         window
tool-based-ui-trunk-gesture                    0                     0                                 not recorded: 14    0                       14        window
tool-based-ui-trunk-r10-fix                    0                     0                                 not recorded: 3     0                       3         window
tool-based-ui-trunk-select                     0                     0                                 not recorded: 1     0                       1         window
two-accounts                                   0                     0                                 not recorded: 34    0                       34        window
usage-waste-watch                              0                     0                                 not recorded: 3     0                       3         window
widget-enhancements-fix                        0                     0                                 not recorded: 26    0                       26        window
widget-enhancements-fix2                       0                     0                                 not recorded: 79    0                       79        window
widget-enhancements-icon                       0                     0                                 not recorded: 6     0                       6         window
widget-enhancements-impl                       0                     0                                 not recorded: 14    0                       14        window
widget-enhancements-oit                        0                     0                                 not recorded: 1     0                       1         window
widget-enhancements-pick                       0                     0                                 not recorded: 1     0                       1         window
widget-enhancements-test                       0                     0                                 not recorded: 38    0                       38        window
widget-enhancements-thumb                      0                     0                                 not recorded: 3     0                       3         window

Message window: [2026-10-06T17:40:37.360928-04:00, 2026-10-20T17:40:37.360928-04:00) America/New_York
Source: ~/.local/state/message/log.jsonl (read)
Window records n=10353; natedev sent n=10146; other-machine records excluded n=0; machine not recorded n=0; invalid lines in whole log n=0.
Role source: scripts/message/roster.py --json --here (current roles, not historical roles). Historical role/production fields are not recorded in the current log schema.
Current names cannot prove past roles.
Recorded zeroes cover only known roles. Unknown counts do not prove absence of wrong-party asks.

Source issues
Source issue  Issues (n)
none          0
```

</details>

Measurement limits: phrase counts identify files to read. They do not prove that each use has the defined meaning. Common words such as `root`, `parent`, `candidate` and `issue` have uses outside these role contracts. A name listed for two terms contributes to each term's count. Read the file before proposing a replacement. Definition findings use the declared sources and explicit bold definition lines; they do not inspect all prose for competing definitions. Production names describe the listed documents, not proof that every session is live. Missing historical roles in the message log cannot be supplied from today's session names. Sources: `scripts/production/roles_audit.py`, `measure_names`, `measure_definitions`, `measure_production_names`, `measure_wrong_party` and `report`.

## Friction

Each line is `measured` for a directly checked source or script result, or `reported` for a prior event in a memory. A reported event is not a new count from the message log.

- F01 — measured: PF, Words and SR, description said reports come on a schedule, while PF, Production Context → Updates and SR, `<StartUpdates>` default to on demand; Phase 1 fixes only those two sentences, as approved in `docs/plans/sdlc-roles.md`, Phase 1 → Wording fix.
- F02 — measured: D20 remains open because `commands/producer/to_production.md`, `<WriteProductionDoc>` → Updates writes every 15 minutes, and SR, `<StartUpdates>` opens with 15 minutes when absent but later says on demand when absent; PF and SDLC, Production Context → Updates say on demand; these lines are outside the approved wording fix.
- F03 — measured: D22 has two restart checks: `commands/showrunner/move.md`, idle/busy move rules let an idle member stop without a pause, while config memory `ask-director-before-moving-unit.md`, body requires the director's answer because idle does not prove no worker run.
- F04 — measured: D23 has two rename timings: PF, Units → To rename a unit says between phases; `commands/showrunner/rename_unit.md`, opening and step 1 allow an immediate mid-phase stop.
- F05 — measured: D32 has no final shared-tip owner or distribution contract; Guilds, Phase 2 → Guilds requires that design, and SDLC, Scope of each unit assigns its design to guilds-unit.
- F06 — reported: routine peer relays and unit updates repeatedly reached the user; nixos memory `relay-directly-not-via-user.md`, recurrence notes through 2026-10-16; SR, `<Throughout>` → What reaches the user now limits those messages.
- F07 — reported: a unit asked the user for `github-warmup` instead of sending the need to its showrunner; hana memory `unit-asks-go-through-the-showrunner.md`, Why, 2026-10-18; this is the prior event behind D17.
- F08 — reported: a phase-process assignment and rename were treated as packaging; nixos memory `process-redesigns-are-questions.md`, Why, 2026-10-20; SDLC, Production rules keeps duties, names and process changes as user questions.
- F09 — reported: moving an apparently idle director killed a worker's test run; config memory `ask-director-before-moving-unit.md`, Why, 2026-10-18; this is the prior event behind D22.
- F10 — reported: herdr work and questions must go to herdr-unit, and another unit must ask the showrunner before opening that work; config memory `herdr-work-owner.md`, body and How to apply, 2026-10-18; the purpose is to keep that scope with one unit.
- F11 — reported: bare phase numbers did not identify another unit's work to the user; config memory `name-work-not-phase-numbers.md`, Why, user 2026-10-06; `commands/showrunner/dailies.md`, work-naming rules require the change in words.
- F12 — reported: a small fix took about 25 minutes through a unit and repeated merge checks; nixos memory `small-fixes-by-subagent-in-main.md`, Why, user 2026-10-08; its file-count limit and checkout interpretation are the memory author's interpretation, not a user-set threshold.
- F13 — measured: `docs/delegate_plan_format.md`, introduction calls the unit director an orchestrator; UD, opening uses delegate for the writer and `<WorkerRuns>` uses seat; PF, Words calls that writer Worker. These names do not identify one role consistently.
- F14 — measured: PF, Words → Gate means a dependency between units; UD, `<FinalGate>` and `docs/delegate/phase_checks.md`, `<PhaseChecks>` use gate or checks for verification. One word has two current uses.
- F15 — measured: Guilds, Phase 2 → Names requires the two uses of `astra-designer` and `astra-reviewer` to be named apart; SDLC, Guild heads uses those IDs for top-level heads, while Guilds, Phase 1 uses them for held per-design threads. cargo-liner owns the final head-name choice with the user.
- F16 — measured: `commands/unit/add_ons.md`, `<ConsiderNextItems>` calls follow-up candidates proposals, while `commands/showrunner/proposal.md`, File requires a proposal issue; `commands/change_review.md`, add tracks an implemented change separately. Add-on, proposal and change identify different stages but overlap in prose.
- F17 — measured: `commands/unit/delegate.md`, description keeps the old `/unit:delegate` command; `commands/showrunner/promote_showrunner.md`, step 1 still uses it. `docs/delegate/` paths and state keys are compatibility names, not proof of a live role called delegate.
- F18 — measured: `commands/plan/shrink.md`, Purpose calls the per-phase result an as-built record, while `commands/plan/to_as_built.md`, Purpose calls the feature overview an as-built doc; Format, Document structure uses As-built for the phase section. The short name covers both a phase record and a feature document.

## Questions for the user

These are proposed questions for the showrunner to present. They do not ask for an answer in the worker's turn. Guild questions go through guilds-unit; repair-rule questions go through fix-rounds-unit. F02–F04 need source corrections, but their role owner is already assigned; they are recorded for the later candidate review rather than new ownership questions here.

Q1. For D32, should each guild head own its guild's shared tips and distribution, or should the sdlc showrunner own both; recommend each guild head, with the final route designed by guilds-unit?

Q2. Should `gate` name only a dependency between units, with `verification gate` for a check, or should both keep `gate`; recommend the two distinct terms (F14)?

Q3. Should `reviewer` alone mean a pooled code reviewer (D15), with `Astra reviewer` for the planned per-design reviewer (D52), or should both keep the same name; recommend explicit `pooled reviewer` and `Astra reviewer` in shared prose, with guilds-unit defining the full design-review contract (PF, Words → Reviewer; Guilds, Phase 2 → Roles)?

Q4. Should `proposal` mean a filed issue and `add-on` an unfiled follow-up candidate, or should both keep `proposal`; recommend the distinct names and keep `change` for an implemented result awaiting review (F16)?

Q5. Should `astra-designer` and `astra-reviewer` remain per-design role IDs (D51–D52) while each guild head (D30–D31) gets its own user-chosen name, or should the head IDs also name per-design threads; recommend separate head names, with cargo-liner settling them with you (F15)?

Q6. Should shared prose use `showrunner` for production coordination and `unit director` for phase coordination, or keep `orchestrator` for both; recommend the two named roles (F13 and the term inventory's shared other name)?

Q7. Should `worker` name a unit-dispatched thread and `small-fix helper` name a helper sent by a showrunner, or keep `delegate`, `seat`, `helper` and `subagent` across both; recommend the scoped names, with compatibility paths and commands reviewed separately (F13; nixos memory `small-fixes-by-subagent-in-main.md`, How to apply)?

Q8. Should `as-built record` name the phase facts and `as-built doc` name the feature overview, or should both use `as-built` alone; recommend the two explicit names (F18)?

## Next steps

Phase 2 of `docs/plans/sdlc-roles.md` compares candidates for duty definitions, ownership and names and states which baseline measure each step must move. No candidate is approved by this map. Later phases change one approved step and rerun the measure; a step is complete only when its before and after counts show the intended change. The approved wording fix above does not retire any alias or change any role.
