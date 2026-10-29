# Duties and names in the development process

> **Status: DESIGN — phase 2 proposals.** Q2, Q3 and R5b are answered below; other questions remain open. This phase changes no live duty or name. Phase 1 made only the two approved reporting wording fixes.

Scope: `docs/plans/sdlc-roles.md`, Decisions and Phases 1–2. Duties and names belong here. Structure belongs to hierarchies-unit; designer and reviewer contracts belong to guilds-unit; phase steps and the document pipeline belong to phases-unit; repair rules belong to fix-rounds-unit. This document refers to their work and does not replace it.

## Sources

Paths without a prefix are relative to this checkout. These short references keep each matrix row readable:

| Reference | File |
| --- | --- |
| PF | `docs/production_format.md` |
| SR | `commands/showrunner/produce.md` |
| UD | `commands/unit/direct.md` |
| Format | `docs/delegate_plan_format.md` |
| Decisions | `~/.claude/docs/decision_criteria.md` |
| Hierarchy | `d0b07aec:docs/plans/build-followups-hierarchies-design.md` in `/home/natepiano/worktrees/claude-build-followups-hierarchies`, Map → Roles |
| SDLC | `/home/natepiano/worktrees/claude-build-followups-sdlc/docs/plans/sdlc-production.md` |
| Guilds | `docs/as-built/guilds.md` (the shipped guilds and lasting Astra roles; it replaced the guilds-unit plan and design) |
| Phases | `/home/natepiano/worktrees/claude-build-followups-sdlc-phases/docs/plans/sdlc-phases.md` |
| config memory | `~/.claude/projects/-home-natepiano--claude/memory/` |
| nixos memory | `~/.claude/projects/-etc-nixos/memory/` |
| hana memory | `~/.claude/projects/-home-natepiano-rust-hana/memory/` |

Read on 2026-10-20 on natedev. Phase 1 used the on-disk Hierarchy draft because the file was absent from HEAD. Phase 2 uses the committed Map at `d0b07aec`, also merged to main through `83a0ad28`. Its role rows supply the names below; the Map stays in that document. Planned per-design roles D51–D52 come from Guilds, not from the Map.

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
| D17 | Bring a user-only decision or action to the user | Top-level showrunner | Child showrunner asks in its own reply, keeps its own open item and also sends the item directly to its root; a question between productions goes to its parent; Unit director supplies the need to its showrunner | duplicate | SR, opening, `<Throughout>` → Nested showrunners, What reaches the user and Open questions; Hierarchy at `d0b07aec`, Map → Roles → Child showrunner and Channels → Open question; hana memory `unit-asks-go-through-the-showrunner.md`, body and How to apply; PF, `<ProductionUnit>` item 2 |
| D18 | Relay the user's words to a unit | Top-level showrunner | Child showrunner relays to its own units; Unit director alone relays user words to its workers | duplicate | SR, `<Throughout>` → The user's words for a unit director and Nested showrunners; `docs/delegate/write_prompt_contract.md`, `<WritePromptContract>` item 4 and `<PhaseMesh>` |
| D19 | Measure and record a unit phase ETA | Unit director | Top-level showrunner requests a missing ETA and reports the result | duplicate | PF, `<ProductionUnit>` items 3 and 11; `commands/unit/eta.md`, Steps and Answer; SR, `<Throughout>` → Unmeasured ETAs |
| D20 | Give production dailies; choose the default report schedule | Top-level showrunner | Child showrunner reports its own production; Producer writes a conflicting default | conflict | PF, Production Context → Updates; SR, `<StartUpdates>` `:393–394` says 15 minutes when absent; `commands/producer/to_production.md:118` still says every 15 minutes; PF `:100–103` and SR `:412–418` already give the on-demand behavior |
| D21 | Retire a unit and its resources | Top-level showrunner | Child showrunner for its own unit; ending a director run does not retire the unit | duplicate | PF, Units → To retire a unit; `commands/showrunner/retire_unit.md`, opening and numbered resource list |
| D22 | Move a unit director to an account after checking active worker runs | Top-level showrunner | Child showrunner can invoke the move; natedev holds the role map for both machines | conflict | SR, `<PauseForMove>`; `commands/showrunner/move.md`, opening and idle/busy move rules; `commands/showrunner/accounts.md`, natedev role-map rule; config memory `ask-director-before-moving-unit.md`, body requires an answer before moving even an idle director |
| D23 | Rename a unit; decide when it can stop | Top-level showrunner | Executive producer approves the name change during this redesign; a Unit director cannot rename itself | conflict | PF, Units → To rename a unit says between phases; `docs/as-built/unit-rename.md:53` records the user's any-time rule (2026-10-18); `scripts/production/rename_unit.py:77` records stop-at-once (2026-10-20); `commands/showrunner/rename_unit.md`, step 1 implements it; `commands/unit/rename.md` forwards to the showrunner |
| D24 | Place and release a timing build hold | Top-level showrunner | Unit director requests the hold and release; Codex worker and Small-fix helper never place it | duplicate | `commands/build/hold.md`, steps 1 and 4; hana memory `build-holds.md`, Frame-time asks build stop before timing and Seats never place build holds; SR, `<Throughout>` → The showrunner decides |
| D25 | Send an actionable phone push for a unit blocked on the user | Top-level showrunner | Child showrunner for its production; a machine session pushes only its own blocks | one owner | SR, `<Notify>` → Units tell you, and you push; SR, `<Dependencies>` rule 6 |
| D26 | Call an early landing; apply the automatic hard landing | Top-level showrunner for the early landing call; Unit director when another unit waits on the phase | Unit director also applies the landing rule after the second repair round and the hard landing from the third repair round without a call | duplicate | SR, `<LandingCall>`; PF, `<ProductionUnit>` item 7; fix-rounds-unit source check, 2026-10-20; fix-rounds-unit owns changes to these repair rules |
| D27 | Review remaining Work Orders, then shrink the completed phase | Unit director | Codex worker supplies an architect review only when a listed trigger fires | duplicate | UD, `<RunPhaseReview>`; `docs/delegate/run_phase_review.md`, `<RunPhaseReview>` and `<RunPhaseShrink>`; `docs/delegate/phase_end.md`, `<PhaseEnd>` items 3 and 7 |
| D28 | Close the run with as-built documentation | Unit director | None; its scope choice is not a product scope change | duplicate | SR, `<Throughout>` → As-built close-out; UD, `<RunAsBuilt>`; `docs/delegate/final_gate_commit.md`, `<RunAsBuilt>`; `commands/plan/to_as_built.md`, Purpose |
| D29 | Write a small unowned fix in the showrunner checkout | Small-fix helper | Top-level showrunner dispatches, commits, pushes and promotes; existing unit work stays with its unit | one owner | nixos memory `small-fixes-by-subagent-in-main.md`, body and How to apply; PF, Words → Showrunner writes no implementation code |
| D30 | Head the designer guild | `designers` guild head, a unit director in the `guilds` production, which runs under sdlc (G1c) | None; contract in Guilds, Guilds and heads | one owner | SDLC, Guild heads; Guilds, Guilds and heads |
| D31 | Head the reviewer guild | `reviewers` guild head, a unit director in the `guilds` production, which runs under sdlc (G1c) | None; contract in Guilds, Guilds and heads | one owner | SDLC, Guild heads; Guilds, Guilds and heads |
| D32 | Maintain and distribute each guild's shared tips | Each guild head, for its own guild (`tips.py publish` to `~/rust/nate_style/guilds/`) | Members report set events to their head (Guilds, Events); starting skills load tips | one owner | Guilds, Guilds and heads and Tips; SDLC, Scope of each unit → guilds-unit |
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
| D51 | Write and revise a design in the same Codex thread; answer each review finding with a change or a reason to disagree | Astra designer, a held per-design role (`astra-designer`, `scripts/guilds/roles.py`) | Unit director or showrunner holds and briefs the role; the holder passes reviews and judges the result, rather than writing the design; the `designers` head in D30 is separate | one owner | Guilds, Lasting roles |
| D52 | Review a design and return findings for the designer to answer | Astra reviewer, a held per-design role (`astra-reviewer`, `scripts/guilds/roles.py`) | Unit director or showrunner holds the role and passes its review to the designer; the unit director rules on disagreements in the Phase 2 design; the `reviewers` head in D31 is separate; Pooled reviewer in D15 reviews code under the pool contract | one owner | Guilds, Lasting roles (at most one reviewer per design) |
| D53 | A fix that fails twice: find the cause, record it in the attempts log, then dispatch one pass with a new approach | Unit director | Codex worker carries out the new pass; no third pass repeats the failed approach | one owner | PF, `<ProductionUnit>` item 7 → A fix that fails twice; fix-rounds-unit source check, 2026-10-20 |
| D54 | One batch per check: check every changed view after a fix and send all findings to the next round together | Unit director | Codex worker receives the full batch for its file set; the design checker supplies its findings | duplicate | PF, `<ProductionUnit>` item 7 → One batch per check; UD, `<UXReview>` and `<FixDispatch>`; fix-rounds-unit source check, 2026-10-20 |
| D55 | Own change only: send a defect from before the phase to the showrunner for routing | Unit director | Top-level showrunner, or Child showrunner for its production, routes the defect to the owning unit; the phase does not take it into its repair round | one owner | PF, `<ProductionUnit>` item 7 → Its own change only; fix-rounds-unit source check, 2026-10-20 |

Two helpers select recipients, not the actor's role: `commands/showrunner/announce.md` sends the user's words to showrunners, and `commands/unit/announce.md` sends them to unit directors. Their bodies require the invoking session to pass the user's words unchanged. Neither helper gives a worker or director new decision authority. `commands/unit/delegate.md` forwards to `/unit:direct`; `commands/unit/ci.md`, `<PeriodicCI>`, explicitly skips production units because D06 belongs to the showrunner.

The conflicts above record the source instructions. They do not authorize a move, rename, schedule change or repair-rule change. D30–D32 describe the shipped guild heads and tips. D51–D52 describe the shipped per-design roles, at most one designer and one separate reviewer per design; they are distinct from the heads and the review pool. These rows do not change the current implementation-design authority in D08.

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
| worker | role | Dispatched Codex thread that writes, tests, repairs or reviews; PF, Words | seat and Codex seat: UD, `<WorkerRuns>`, `commands/showrunner/rename_unit.md`; delegate: UD, opening, `docs/delegate/write_prompt_contract.md`, `<PhaseMesh>`; subagent also describes the separate Small-fix helper in nixos memory `small-fixes-by-subagent-in-main.md`; Codex worker: UD, `<LongLivedWorkers>` | worker in prose (approved user, sdlc review; phase 3 Work Order, 2026-10-28); retain slot names such as `impl` and `test` as identities |
| shown name | label | Answered R5b: every shown name carries production · unit · job; user decision, relayed by the sdlc showrunner and then the unit director (2026-10-28) | Reports, messages and Codex thread names; the address stays as it is | `production · unit · job`, for example `sdlc · roles · code reviewer 6`; answer recorded only, no source rename in this phase |
| review pool | record | Reusable reviewers allocated by script; PF, Words | — | review pool |
| reviewer | role | Answered Q3: distinguish review duties; user, sdlc review, relayed in phase 3 Work Order (2026-10-28) | Existing pool and held Astra roles remain separate; Guilds, Guilds and heads | design reviewer, code reviewer and UI reviewer; designer for the design role; no source rename in this phase |
| template | record | Thread with standing review instructions; never leased; PF, Words | review template: Hierarchy, Map → Roles | review template in shared prose |
| lease | record | Lasting reviewer assignment to one unit, distinct from ownership of one active review; `scripts/direct/review_pool.py:32`, `UnitLease`, and `:676`, `command_end_lease` (no-op). PF, Words still describes a phase lease and needs correction against this code. | review lease is a proposed qualifier, not a phase lifecycle | review lease in shared prose (proposed); define its lasting unit lifetime |
| merge branch | record | Branch that receives unit checkpoints; PF, Words | trunk: PF, Words, retained as cargo-berth's term; SR, `<Dependencies>` also records a former unit named trunk | merge branch, or the actual branch name where needed |
| gate | step | Answered Q2: a check that must pass; user, sdlc review, relayed in phase 3 Work Order (2026-10-28) | Earlier source meanings: PF, Words; UD, `<FinalGate>` and `<TurnEndGate>` | gate; preserve `gate:` syntax; no source rename in this phase |
| wait | step | Answered Q2: one unit waiting on another; user, sdlc review, relayed in phase 3 Work Order (2026-10-28) | Earlier inter-unit gate wording: PF, Words | wait; no source rename in this phase |
| parent showrunner | role | Showrunner that receives another production's reports; PF, Words | parent: SR, `<Throughout>` → Nested showrunners | parent showrunner when the hierarchy is relevant |
| child showrunner | role | Showrunner with its own production that reports to a parent; PF, Words | lower showrunner: `commands/showrunner/add_showrunner.md`, opening | child showrunner |
| root showrunner | role | Top of an attached production tree; PF, Words | root: SR, `<Throughout>` → Nested showrunners; top-level showrunner: Hierarchy, Map → Roles | root showrunner for a tree; top-level showrunner for a peer without a parent |
| small-fix helper | role | Approved name: a showrunner helper that does no code work; user, sdlc review, relayed in phase 3 Work Order (2026-10-28) | Earlier code-writing helper wording remains in the D29 source inventory pending a separately approved duty correction | small-fix helper; no source duty change in this phase |
| astra-designer | role | Two planned uses: the per-design Codex role writes and revises a design in the same thread (D51; Guilds, Phase 2 → Spec and Roles); the temporary designer guild-head ID names D30 (SDLC, Guild heads) | chief designer: SDLC, Guild heads; Guilds, Decisions → edge role ids and Phase 2 → Names require the head and per-design uses to be named apart | Astra designer for the per-design role; chief designer for the head until cargo-liner settles its name with the user |
| astra-reviewer | role | Two planned uses: the per-design Codex role reviews the design and returns findings (D52; Guilds, Phase 2 → Spec and Roles); the temporary reviewer guild-head ID names D31 (SDLC, Guild heads) | chief reviewer: SDLC, Guild heads; Guilds, Phase 2 → Roles distinguishes the per-design role from pooled code and UI reviewers and one-turn `/astra_review` | Astra reviewer for the per-design role; chief reviewer for the head until cargo-liner settles its name with the user; pooled reviewer for D15 |
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

The examples above describe meanings; some related names identify distinct stages or roles and are not counted as aliases. The index below lists every matching file for each other name in the term inventory, using the audit's path and string filters. The index was checked against `phases/2/measure_facts.md`, Other names, from the checker run on 2026-10-20. Combined counts include Python and shell lower bounds; they are observed matches, not eligible rename totals. The Baseline gives the complete live file set and file counts. Guild-head names and per-design role IDs are planned names, not approved replacements.

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
- F04 — measured: PF, Units → To rename a unit says between phases, contrary to the user's any-time rule in `docs/as-built/unit-rename.md:53` (2026-10-18) and stop-at-once rule at `scripts/production/rename_unit.py:77` (2026-10-20). Correcting PF preserves current behavior; requiring a phase boundary would change policy.
- F05 — measured: D32 has no final shared-tip owner or distribution contract; Guilds, Phase 2 → Guilds requires that design, and SDLC, Scope of each unit assigns its design to guilds-unit.
- F06 — reported: routine peer relays and unit updates repeatedly reached the user; nixos memory `relay-directly-not-via-user.md`, recurrence notes through 2026-10-16; SR, `<Throughout>` → What reaches the user now limits those messages.
- F07 — reported: a unit asked the user for `github-warmup` instead of sending the need to its showrunner; hana memory `unit-asks-go-through-the-showrunner.md`, Why, 2026-10-18; this is the prior event behind D17.
- F08 — reported: a phase-process assignment and rename were treated as packaging; nixos memory `process-redesigns-are-questions.md`, Why, 2026-10-20; SDLC, Production rules keeps duties, names and process changes as user questions.
- F09 — reported: moving an apparently idle director killed a worker's test run; config memory `ask-director-before-moving-unit.md`, Why, 2026-10-18; this is the prior event behind D22.
- F10 — reported: herdr work and questions must go to herdr-unit, and another unit must ask the showrunner before opening that work; config memory `herdr-work-owner.md`, body and How to apply, 2026-10-18; the purpose is to keep that scope with one unit.
- F11 — reported: bare phase numbers did not identify another unit's work to the user; config memory `name-work-not-phase-numbers.md`, Why, user 2026-10-06; `commands/showrunner/dailies.md`, work-naming rules require the change in words.
- F12 — reported: a small fix took about 25 minutes through a unit and repeated merge checks; nixos memory `small-fixes-by-subagent-in-main.md`, Why, user 2026-10-08; its file-count limit and checkout interpretation are the memory author's interpretation, not a user-set threshold.
- F13 — measured: `docs/delegate_plan_format.md`, introduction calls the unit director an orchestrator; UD, opening uses delegate for the writer and `<WorkerRuns>` uses seat; PF, Words calls that writer Worker. These names do not identify one role consistently.
- F14 — measured: PF, Words → Gate means a dependency between units; UD, `<FinalGate>` and `docs/delegate/phase_checks.md`, `<PhaseChecks>` use gate or checks for verification. UD, `<TurnEndGate>` also uses gate for a required user interaction. These are distinct meanings.
- F15 — measured: Guilds, Phase 2 → Names requires the two uses of `astra-designer` and `astra-reviewer` to be named apart; SDLC, Guild heads uses those IDs for top-level heads, while Guilds, Phase 1 uses them for held per-design threads. cargo-liner owns the final head-name choice with the user.
- F16 — measured: `commands/unit/add_ons.md`, `<ConsiderNextItems>` calls follow-up candidates proposals, while `commands/showrunner/proposal.md`, File requires a proposal issue; `commands/change_review.md`, add tracks an implemented change separately. Add-on, proposal and change identify different stages but overlap in prose.
- F17 — measured: `commands/unit/delegate.md`, description keeps the old `/unit:delegate` command; `commands/showrunner/promote_showrunner.md`, step 1 still uses it. `docs/delegate/` paths and state keys are compatibility names, not proof of a live role called delegate.
- F18 — measured: `commands/plan/shrink.md`, Purpose calls the per-phase result an as-built record, while `commands/plan/to_as_built.md`, Purpose calls the feature overview an as-built doc; Format, Document structure uses As-built for the phase section. The short name covers both a phase record and a feature document.

## Questions for the user

These are proposed questions for the showrunner to present. They do not ask for an answer in the worker's turn. Guild questions go through guilds-unit; repair-rule questions go through fix-rounds-unit. F02–F04 need source corrections, but their role owner is already assigned; they are recorded for the later candidate review rather than new ownership questions here.

Q1. For D32, should each guild head own its guild's shared tips and distribution, or should the sdlc showrunner own both; recommend each guild head, with the final route designed by guilds-unit?

Q2. **Answered (user, sdlc review; relayed in the phase 3 Work Order, 2026-10-28):** a `wait` is one unit waiting on another; a `gate` is a check that must pass. This phase records the answer only. Keep the `gate:` status syntax; source changes need a later approved step.

Q3. **Answered (user, sdlc review; relayed in the phase 3 Work Order, 2026-10-28):** name the roles `design reviewer`, `code reviewer`, `UI reviewer` and `designer`. This phase records the answer only; it does not rename sources, role IDs, guild heads or running threads.

R5b. **Answered (user decision, relayed by the sdlc showrunner and then the unit director, 2026-10-28):** every shown name carries `production · unit · job`, for example `sdlc · roles · code reviewer 6`, in reports, messages and Codex thread names. The address stays as it is. This phase records the answer only; it makes no source rename.

Q4. Should `proposal` mean a filed issue and `add-on` an unfiled follow-up candidate, or should both keep `proposal`; recommend the distinct names and keep `change` for an implemented result awaiting review (F16)?

Q5. Should `astra-designer` and `astra-reviewer` remain per-design role IDs (D51–D52) while each guild head (D30–D31) gets its own user-chosen name, or should the head IDs also name per-design threads; recommend separate head names, with cargo-liner settling them with you (F15)?

Q6. Should shared prose use `showrunner` for production coordination and `unit director` for phase coordination, or keep `orchestrator` for both; recommend the two named roles (F13 and the term inventory's shared other name)?

Q7. Should `worker` name a unit-dispatched thread and `small-fix helper` name a helper sent by a showrunner, or keep `delegate`, `seat`, `helper` and `subagent` across both; recommend the scoped names, with compatibility paths and commands reviewed separately (F13; nixos memory `small-fixes-by-subagent-in-main.md`, How to apply)?

Q8. Should `as-built record` name the phase facts and `as-built doc` name the feature overview, or should both use `as-built` alone; recommend the two explicit names (F18)?

Q9. Should a showrunner keep renaming an approved unit at any time, stopping it and its workers at once, or wait for a phase boundary; recommend the current any-time rule? Waiting may reduce interrupted work but delays an authorized rename and changes the user's rule. This new policy alternative has not been answered.

## Candidates

These are alternatives for the user, not new instructions. Q1–Q8 went to the showrunner on 2026-10-20 at 17:50 EDT. No answer has arrived. Each candidate needs approval before it changes a live duty, name or process. A Q reference below marks an additional open dependency, not an assumed answer.

All alternatives keep sdlc, the chief designer and the chief reviewer as top-level peers. None adds a parent, promotes a session or changes a reporting edge. A proposed edge change goes to hierarchies-unit through the showrunner. Guilds-unit designs guild contracts; cargo-liner settles head names with the user. Phases-unit owns phase steps and the document pipeline. Fix-rounds-unit owns repair rules, including D26 and D53–D55. A role's shorter name does not change its authority.

The three gods apply after correctness: Simple, Fast, Beautiful. The specific user rule for this redesign requires questions for duty and name changes. DecisionEconomy still lets the director package approved work, choose ordinary implementation details and record those calls without another user question. These proposals add no report timer, peer relay through the user, or measurement traffic.

### Where duties are written

- **P-A — Owner commands.** State each duty once in its owner's command and tag. Other commands and PF link to that tag. PF Words gives a short role description and links to its duties; it does not repeat the operating rules. A shared repair rule stays in the file fix-rounds-unit chooses; the director command links to it. Keep each current owner in the duplicate rows listed below. Ask separately before resolving a conflict or assigning D32. This keeps a worker's operating rule near the command it follows.
- **P-B — Central duty register.** Put the full duty text, owner and stable duty ID in PF. Commands link to the register instead of restating it. Keep the same owners and the same unresolved choices as P-A. This gives a reader one duty table, but a command needs another document to explain its work. It also makes PF a shared edit point for unrelated duties.

Both candidates cover the duplicate rows D01–D06, D08–D15, D17–D19, D21, D24, D26–D28, D33–D34, D37–D40, D42, D48 and D54. They do not transfer work between those roles. The command source in each row is P-A's proposed home; PF is P-B's home. For D01 and D09, Decisions and the user's production rule remain the authority: link to them instead of rewriting the user's policy. For D26 and D54, submit the proposed links to fix-rounds-unit and phases-unit. For D15, guilds-unit must confirm the distinction from D52. D20, D22, D23 and D32 use the ownership alternatives below.

### Who owns the conflicting or ownerless work

| Candidate | Proposed owner and behavior | Alternative and open decision |
| --- | --- | --- |
| O20-A | Top-level showrunner owns runtime report policy. Producer writes `on demand` as the default. Only a user request starts a timer. Correct SR `:393–394` and `commands/producer/to_production.md:118` against PF `:100–103`; keep SR `:412–418`'s timer setup/stop behavior. | Preferred for D20. The user approves the source correction; it does not authorize a schedule. |
| O20-B | Producer owns a required Updates declaration when it creates a production. Showrunner reads that declaration and reports on demand if it is missing. It starts a timer only after a user request. | Same user rule; more required authoring. D20 remains split between setup and runtime roles, with their separate actions stated. |
| O22-A | Top-level showrunner executes an authorized account move after the Unit director confirms that no worker run is in progress. natedev keeps the shared role-account map. | Preferred for D22; align move instructions with the director-readiness rule. No new permission question for an already authorized move. |
| O22-B | Unit director confirms that no worker run is in progress and invokes the approved move helper for its own run. The showrunner supplies the queued destination; natedev still owns the shared map. | Transfers execution to the director. Needs an explicit user answer and a checked resume path. It does not grant a director power to choose an account outside the authorized queue. |
| O23-A | Top-level showrunner executes the approved rename at any time, mid-phase too, stopping the unit and its Codex workers at once. Correct PF's between-phases wording to match the user rule and helper. | Recommended for D23. Preserve the executor, thread identity and any-time behavior. Approval of a particular new name stays separate. |
| O23-B | Top-level showrunner executes the approved rename only after a phase boundary. The director reports that boundary. | Q9 policy-change alternative, not a source correction. It may reduce interrupted work; it costs a delay and reverses the current any-time rule unless the user changes that rule. Out under the current rule. |
| O32-A | Each guild head owns its guild's shared tips and distribution. Members send observations to the head under the route guilds-unit defines. | Preferred for D32; depends on Q1 and guilds-unit's contract. The heads remain top-level peers. |
| O32-B | sdlc supplies a shared-document editor for both guilds. Each head approves and distributes its own guild's advice. | Q1 alternative: sdlc owns editing, not authority over a head. Guilds-unit must define the edit and distribution duties separately. |

O20–O23 are new source-resolution proposals; no Q1–Q8 answer silently approves them. O32 describes planned work. Neither alternative claims the heads are running. Q9 is the explicit question for O23-B; O23-A preserves the current rule. The new repair rows already have owners, so they need no new assignment proposal. P-A or P-B may remove repeated duty wording; changes to their rules go to fix-rounds-unit.

### How names keep one meaning

- **T-A — Term data is the definition source.** Add a short definition and a stable term ID to `role_terms.toml`. Point `defined_in` at that entry; keep original sources in a separate provenance field. Propose `roles_audit.py render-terms --root .` to render PF Words between fixed markers in stable term-ID order, with fixed newlines and no timestamps. Propose `roles_audit.py check-terms-view --root .` to compare that output without writing and fail on a stale view. Run the comparison in a fixture-backed test discovered by `merge_gate --tests-only`. These commands are new code, not available commands today. Replace competing definitions with links to the rendered table. The same source defines roles, documents, steps and records. A term ID names its meaning; it is not a session name or a transport key. Owner commands still state duties under P-A. Do not remove a second meaning merely to make a count smaller: Q2 and Q5 must first settle the distinct gate and Astra meanings.
- **T-B — PF is the definition source.** Expand PF Words to cover all term kinds. Propose deterministic `roles_audit.py compile-terms --root .` to generate `role_terms.toml` from that section and `roles_audit.py check-terms-data --root .` to reject a stale generated copy in the same test lane. These are new commands; both T choices pay for a generator, validation and a stale-copy test. Commands link to PF for a definition and keep their operating instructions. This is a near-tie with T-A: readable prose is the source, with a Markdown parser instead of TOML parsing and view rendering.
- **N-A — Use scoped names.** Adopt the proposed meanings in the table below, after the relevant answer. Retire an old name only in uses that mean the same thing. Keep identifiers, paths, recorded history and other meanings explicitly separate. Read each match before changing it. Each new name states the role, stage or record it means, as required by the Type Design Contract.
- **N-B — Keep declared synonyms.** Keep the present allowed aliases. Use full role or record names at a shared-file boundary and link to the term definition. Qualify `gate`, `reviewer`, `proposal` and `as-built` where they have distinct meanings. This avoids broad replacements, but leaves more names for a reader to learn. It depends on the same open Q answers where a meaning changes.
- **R-seat — Retire worker `seat`.** Use `worker`; use `Codex worker` only when the agent family matters. Replace `Codex seat` before the shorter phrase so it does not become an accidental name. Keep transport addresses, state keys, file paths and slot identities. Depends on Q7. N-B keeps these prose aliases instead.
- **R-trunk — Retire role prose `trunk`.** Use `merge branch` for the role of the branch, and the actual branch name for a specific ref. Keep cargo-berth's own term and historical unit names when those are the intended meanings. Those are explicit exceptions, not a global substitution. Needs a new user answer; N-B retains the alias.
- **R-command — Retire `/unit:delegate`.** Change command examples and live callers to `/unit:direct`. Only after a compatibility review, remove the forwarding command and update command discovery. Keep `docs/delegate/` paths and script/config identifiers. Needs a separate user answer on removal of the callable alias; Q7 alone is insufficient. N-B keeps the forwarding command and documents its preferred spelling.
- **G-A — Check approved retired uses.** Add a source-only, plural-aware `roles_audit.py check`, called by a test in `scripts/production/test_roles_audit.py` that `merge_gate --tests-only` discovers. Each unit's own gate then fails before the showrunner's merge. Per-file kept-use contexts are new code: today's `without_paths` masks kept strings globally. New entries must name a file, exact context and reason; fixtures must reject a leaked exception in another file or changed context. The check reports each offending path and line and returns failure. It reads no message log and sends nothing. Fixtures cover singulars, plurals, paths, protocol fields, history, unrelated meanings and kept-context drift. Ship it with the first approved retirement. Time the source-only check and review its false failures before the user question goes out; full-baseline time is not its cost.
- **G-B — Report and review only.** Keep the baseline command and have the checkpoint review compare it with the approved target. Add no merge-gate failure. This has less code and no new gate time, but a retired name can return between reviews. It is the enforcement runner-up.

T-A and T-B need a user answer on the definition home. Neither starts a new documentation hierarchy. P, T, N and G are independent choices. Scoped names can use either definition source and either check policy; owner commands can link to either definition home. Mixed choices are valid. N-A and N-B cover every other name in the term table, including an allowed name with no current hit. The choices below are proposed meanings, not replacements already made.

| Defined term and other names | N-A treatment; N-B alternative where it differs | Approval dependency |
| --- | --- | --- |
| executive producer: user | Use executive producer when naming the process role. Keep user for the person and for product users. Do not retire user globally. | New role-label approval |
| showrunner and unit director: orchestrator | Name the actual role in production/phase instructions. For a standalone command, say invoking session without assigning a new role. N-B keeps the broad coordinator word. | Q6; phases-unit for phase text |
| worker: seat, Codex seat, delegate, Codex worker | R-seat changes only the seat names. Review delegate separately from command names and paths. Keep Codex as a family qualifier, not a second duty. N-B retains the aliases. | Q7; R-command is separate |
| reviewer: pooled reviewer | Use pooled reviewer for D15 and Astra reviewer for D52 in shared prose. They remain different roles. | Q3; guilds-unit |
| gate: dependency, verification and required user interaction | Use dependency gate, verification gate and user gate in shared prose; N-B keeps gate with a local definition. Preserve the `gate:` status syntax. | Q2; separate compatibility answer for syntax |
| template: review template; lease: review lease | Use the qualified names in shared prose. They are records with different lifetimes: a template is never leased; the current lease assigns a reviewer to a unit for good, while an active review has separate ownership. Correct PF's phase-only description before choosing a name. N-B keeps the shorter local names with a definition link. | New name approval |
| merge branch: trunk | Apply R-trunk; keep actual refs and cargo-berth terminology. | New name approval |
| parent showrunner: parent; child showrunner: lower showrunner | Use full role names when reporting an existing edge. Parent of a file or process keeps its own meaning. N-B keeps the short edge labels. | New name approval; any edge change goes to hierarchies-unit |
| root showrunner: root, top-level showrunner | Root names the head of an attached tree. Top-level showrunner names a peer without a parent. Do not merge those meanings or replace filesystem root. | New meaning approval; hierarchies-unit |
| production doc: production document | Use production doc for that file. N-B permits the longer spelling. | New name approval |
| unit plan: delegate-ready plan | Use unit plan for a production unit, and phased plan for the general format. Do not rename a standalone plan to a unit plan. | New name approval; phases-unit |
| as-built: as-built overview, as-built doc | Use as-built doc for the feature overview and as-built record for phase facts. N-B permits the short name only when the scope is explicit. | Q8; phases-unit |
| add-on: next item; proposal: candidate, add proposal | Add-on is unfiled follow-up work; proposal is a filed issue; candidate is still a general option in unrelated work. Next item may describe queue order without naming a new record. | Q4; phases-unit for document stages |
| change: change-review row | Change names the implemented result; change-review row names its record. Keep menu action change as a different verb. | Q4 for stage distinction; new record-label approval |
| dailies: scheduled update | Dailies names the report. Scheduled names a user-requested trigger, not the default. N-B keeps the phrase only with that trigger stated. | O20; new name approval |
| checkpoint: code commit, code ready notice, code checkpoint | Name checkpoint commit and checkpoint notice separately where both matter. Code checkpoint may qualify the checkpoint before shrink. N-B keeps the existing labels with links. | New name approval; phases-unit owns steps |
| phase review: plan review | Use phase review for the review of remaining Work Orders. Keep a general plan review distinct when no phase is involved. | New name approval; phases-unit |
| /unit:direct: /unit:delegate | Approve the current-reference migration on its own. Keep the callable alias and historical recognition unless removal is separately approved. | Caller approval, then a separate command-compatibility answer |
| astra-designer: chief designer; astra-reviewer: chief reviewer | Keep per-design roles distinct from the top-level guild heads. Use the temporary head IDs until cargo-liner settles names with the user. No global replacement of either ID. | Q5; Q3 for reviewer distinction; guilds-unit and cargo-liner |
| small-fix helper: subagent, helper | Use small-fix helper only for D29. A research helper or library helper keeps its own meaning. N-B keeps general agent-family names in local context. | Q7 |
| pending decision: Pending decision block | Pending decision names the unresolved choice; block describes its document form. N-B permits both with that distinction. | New record-label approval; phases-unit |
| finding: issue | Use finding for a confirmed review defect. Keep issue for a filed backlog item, and issue as a verb. No global issue replacement. | New record-label approval |
| reservation: phase reservation, delegated phase reservation | Use phase reservation for durable phase authorization. Keep a worker's narrower file claim distinct. N-B keeps the longer qualifier. | New record-label approval; phases-unit |
| ETA: phase ETA | Keep ETA qualified by its target, unit and time zone. Phase ETA is a useful qualifier, not a retired name. | No retirement proposed; approval only if the definition changes |

Work Order, production, producer, unit, review pool, gate, shrink and landing call have no other-name entry in the inventory. Those entries propose no alias retirement by themselves. Gate's distinct meanings still need Q2: N-A proposes dependency gate, verification gate and user gate in shared prose; N-B permits gate with an explicit local meaning. Keep `gate:` protocol syntax under either answer. Landing call and hard landing remain different actions. No naming candidate changes the repair thresholds.

## Comparison

The Phase 1 measures are in the Baseline section; the phase 2 checker re-ran them on 2026-10-20 and they agree. Step 1 adds a read-only plural-aware classification dated 2026-10-20. It does not overwrite the singular-only Baseline. Python and shell counts, including combined totals, are lower bounds: helper arguments and stderr cat heredocs are not fully counted. A lexical hit is not a defect.

The ranking is G-A with N-A first for drift prevention; G-B with N-B is the enforcement runner-up. P-A versus P-B and T-A versus T-B are near-ties on reading. The judge dated 2026-10-20 found `seat` grew from n=4 lines at retirement commit `4ddd7b52` (2026-10-14) to n=27 at the reviewed HEAD, using `git grep -ciw seat`. That line count has a different file set and unit from Names hits. It supports drift as the gain; it does not measure reading or prove G-B would fail its new baseline comparison.

Every candidate is scored on Names, Definitions, unit/session names, sent contacts and duty sources below. `Unchanged` means no direct target. No candidate changes a production or session identity, so all leave the checker's n=2 nonconforming and n=2 shared-name findings in n=5 production docs unchanged. None claims a contact saving: historical roles and request intent are absent from the message log. Its checked window is [2026-10-06T17:54:18.659760-04:00, 2026-10-20T17:54:18.659760-04:00), America/New_York, n=10352 records and n=10144 natedev sent contacts. The measure sends no traffic.

Rule scores are conditional on approval. The columns name each rule without codes. Unit boundaries mean hierarchies-unit owns structure, guilds-unit owns guild contracts, phases-unit owns phase steps and the document pipeline, fix-rounds-unit owns repair rules, and cargo-liner settles guild-head names with the user. A candidate that breaks a current user rule is out. Decision economy permits ordinary implementation and packaging choices; it does not override the explicit duty/name questions. Meaning and language require semantic names under the Type Design Contract and short active sentences under Simplified Technical English.

| Candidate and rank | Names | Definitions | Unit/session names | Sent contacts | Duty sources | Benefit, cost and limit under Simple, Fast, Beautiful | User approval | Flat peers | Decision economy | Unit boundaries | Meaning and language |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P-A; slight preference, reading tie | Unchanged | Unchanged alone | Unchanged | No size known | Each listed duplicate except D16 becomes one operating statement plus links; needs a source measure | Keeps rules nearby; costs link maintenance; reading gain unmeasured | Kept; source moves need approval | Kept | Kept; package approved moves | Kept; owners supply shared edits | Kept; scoped duty IDs |
| P-B; near-tie | Unchanged | Unchanged alone | Unchanged | No size known | Same duplicate target as P-A | One register; shared edit point and command lookup; reading equal until measured | Kept; source moves need approval | Kept | Kept | Kept | Kept; owner per action |
| O20-A; recommended | Unchanged | Unchanged | Unchanged | No size known | Close D20 after all source clauses agree | Keeps runtime owner; correct stale defaults; baseline cannot yet count duty clauses | Kept; new answer | Kept | Kept; no routine timer question | Kept | Kept; report differs from trigger |
| O20-B; alternative | Unchanged | Unchanged | Unchanged | No size known | Measure all source clauses for those two scoped duties | Explicit producer setup and runtime owner; extra authoring; same on-demand default | Kept; new answer | Kept | Kept | Kept | Kept; setup differs from runtime |
| O22-A; recommended | Unchanged | Unchanged | Unchanged | No size known | Close D22 with director readiness and showrunner execution | Preserves executor; requires readiness contact; resume proof still needed | Kept; new answer | Kept | Kept; queued destination stays authorized | Kept; natedev owns shared map | Kept; readiness is a state |
| O22-B; alternative | Unchanged | Unchanged | Unchanged | No size known | Close D22 by approved execution transfer | Local readiness; helper/resume changes; no measured saving | Kept; transfer needs answer | Kept | Kept | Kept; no new reporting edge | Kept; executor differs from chooser |
| O23-A; recommended | Unchanged | Unchanged | Unchanged | No size known | Close D23 by correcting PF to the current any-time rule | Preserves user behavior and showrunner executor; interrupted work resumes on its thread | Kept; source correction needs answer | Kept | Kept; no new timing permission | Kept | Kept; approved name is an input |
| O23-B; out under current rule | Unchanged | Unchanged | Unchanged | No size known | Would impose a boundary instead of correcting PF | May reduce interruptions; delays rename; changes explicit user policy | Needs Q9 policy change | Kept | Kept only after new policy answer | Kept | Kept; timing policy stated |
| O32-A; recommended after guild contract | Unchanged | Unchanged | Unchanged | No size known | D32 gains each head's maintain/distribute contract | Head owns advice; external contract missing; not build-ready | Kept; Q1 | Kept; heads are peers | Kept | Kept; guilds-unit defines contract | Kept; head differs from member |
| O32-B; alternative | Unchanged | Unchanged | Unchanged | No size known | D32 splits editing, approval and distribution | Shared editor; extra hand-off; sdlc gains no authority over heads | Kept; Q1 | Kept | Kept | Kept; guilds-unit decides contract | Kept; editor differs from head |
| T-A; near-tie, slight preference | Unchanged alone | One authority per approved meaning; fix false worker finding separately | Unchanged | Unchanged | Owner choices unchanged | Existing TOML source; new renderer and stale-view test; no measured reading gain | Kept; home and meaning answers | Kept | Kept; rendering details delegated | Kept | Kept; meaning IDs and provenance |
| T-B; near-tie | Unchanged alone | Same authority target as T-A | Unchanged | Unchanged | Owner choices unchanged | Human definition source; new parser and stale-data test; equal generated-copy duty | Kept; same answers | Kept | Kept | Kept | Kept; generated data is a view |
| N-A; recommended for drift | Down only for classified meanings; include source spelling and plurals | Split approved meanings; no false-finding credit | Unchanged | No size known | Owners unchanged | Clear meanings; classification work; mixed P/T/G choices valid | Kept; each named question | Kept | Kept | Kept | Kept; semantic role and lifecycle names |
| N-B; runner-up | Aliases remain allowed; no quota | May split meanings with local qualifiers | Unchanged | No size known | Owners unchanged | Less rewrite work; more allowed spellings; reading equal until measured | Kept; meaning changes still questions | Kept | Kept | Kept | Kept; local meanings explicit |
| R-seat; first rename, conditional on cost check | Step 1: n=66 eligible of n=84 singular/plural hits in n=19 live files; n=18 kept; script share lower bound | Unchanged | Unchanged | No size known | Worker duties unchanged | Narrow classified meaning plus reusable check; checker and gate work included; timing prerequisite remains | Kept; Q7 and retirement policy | Kept | Kept; identities stay | Kept; phases-unit supplies director text | Kept; worker is a role |
| R-trunk; later classification | Observed n=26 in n=8 prose files; eligible subset unknown | Unchanged | Unchanged | No size known | Merge owner unchanged | Role/ref distinction; cargo and history review; not build-ready | Kept; new answer | Kept | Kept | Kept | Kept; role differs from ref |
| R-command; caller phase only | Step 3: n=3 eligible of n=9 in n=6 live files; n=6 kept; script share lower bound | Unchanged | Unchanged | No size known | Director duties unchanged | Corrects stale statements; keeps history parsing and forwarding; removal is separate later work | Kept; caller approval, then separate removal answer | Kept | Kept | Kept; phases-unit owns director contract | Kept; command differs from actor |
| G-A; recommended for drift | Holds the approved target; no reduction alone | Unchanged | Unchanged | Unchanged | Unchanged | Unit gate catches re-adds. Full baseline took 13–31 s under load 154–269 on 32 cores (judge, 2026-10-20); source-only scan 10.5–10.8 s at load 50–54 (2026-10-20); false failures are replayed first in the build phase | Kept; new check policy answer | Kept | Kept; approved rule runs automatically | Kept | Kept; per-file kept contexts are new code |
| G-B; runner-up | Review compares baseline; no automatic rejection | Unchanged | Unchanged | Unchanged | Unchanged | Less code and no added check; drift can escape review; historical drift did not test this comparison | Kept; review policy answer | Kept | Kept | Kept | Kept; no inferred retirement |

The Phase 1 Names file set is n=546 files, with n=2771 other-name hits of n=11308 pair hits (24.5%), a lower bound including scripts. This is neither a defect rate nor a target denominator. Orchestrator's n=18 hits in n=6 prose files are shared between the showrunner and director pairs; do not add them as independent source savings. The old Definitions set is n=36 terms: worker, gate and each Astra ID have n=2 files, and helper has n=0. Worker is a false duplicate: the roster passage is a selection rule. Removing that finding changes classification, not source duplication. Gate and the Astra IDs need meaning splits before an authority count is meaningful. Preserve the old report beside any new authority report.

Before a build, repeat each classification on unchanged sources. Keep the old lexical result beside the plural-aware and scoped results. Freeze kept contexts before edits; do not turn a failed rename into a passing check by adding an exception later. Source-only scan measured 2026-10-20 18:40 EDT: `read_live_files` plus `measure_names` over the n=546 live files and all n=36 terms took 10.5–10.8 s at host load 50–54 on 32 cores; file reading and string extraction took 7.6–7.7 s of that, and a one-term check still pays it. The false-failure replay over the retired-use history is the first task of Step 1's build phase, before any rename; if it reverses the case for G-A, that phase stops and asks again. The first-place ranking includes new matcher, kept-context and gate work.

## Recommendation

Choose N-A with G-A for drift; use P-A and T-A as slight preferences, with O20-A, O22-A, O23-A and O32-A subject to their answers.
P-A versus P-B and T-A versus T-B are near-ties on reading; mixed choices are valid.
Keep the showrunner's current any-time, stop-at-once rename behavior and correct PF, rather than add a phase boundary.
Start with singular and plural worker-role prose, its pinned tests and a source-only check in each unit's tests-only gate.
Keep identities, protocol fields, historical records and quoted command output in memory explicit; the measured source-only scan time goes with the policy question.
Keep guild heads as peers; their contracts and final names stay with their assigned owners.
The runner-up is P-B, T-B, N-B and G-B with the same owner choices: less enforcement; equal reading.
No candidate claims fewer wrong-party asks from records that lack historical roles and intent.
An adversarial review with a neutral judge and an Astra review, both dated 2026-10-20, led to the drift ranking, plural scope, pinned tests, stale-view checks and preservation of any-time renames.

## Steps

Only the numbered steps below have a selected term, exact source set, classified before count and acceptance target. They are proposed Work Orders, not approval to build. Each requires its named user answer and the source owner hand-offs. The director may package already approved work without another question. Later work below is not a numbered build step: it must first acquire its missing source classification, contract or duty measure. That separation avoids copying a programme into one phase.

### 1. Retire singular and plural worker-seat prose with its check

**Needs the user and dependencies:** Q7, R-seat and G-A approval. Keep worker duties and any-time rename behavior. The question carries the measured source-only scan time (Comparison). The build phase first replays a plural-aware prototype over the commits after the retirement (`4ddd7b52..HEAD`, n=21 commits that add the word) and counts real flags against false failures and run time; if that reverses the case for G-A, the phase stops before any rename and asks again. The implementation estimate covers matcher, context data, fixtures and gate test.

**Change files — live prose:** `commands/showrunner/rename_unit.md`; `commands/showrunner/retire_unit.md`; `commands/unit/direct.md`; `docs/production_format.md`; `docs/as-built/agent-registry.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/unit-rename.md`; `docs/delegate/dual_review.md`. Phases-unit supplies the director edit; hub edits use the owning unit's normal flow. Change worker-role labels, not runtime behavior. Treat `Codex seats` and `Codex seat` before the shorter words.

**Change files — scripts:** `scripts/agents/codex_mesh.py`; `scripts/agents/mesh_runs.py`; `scripts/direct/implement.sh`; `scripts/direct/phase_reservation.py`; `scripts/production/rename_unit.py`; `scripts/production/retire_unit.py`; `scripts/shutdown/restart.py`; `scripts/direct/board.sh`; `scripts/direct/review.sh`; `scripts/hooks/pre-tool-use-berth.py`. Change worker-role comments, docstrings, help, printed labels and messages, including `restart.py:1975`, `mesh_runs.py:272–275` and the rename/retire descriptions. The last three files contain role prose missed by string extraction; their edits earn no extra count credit. Keep identifiers, API fields, flags, transport addresses, filenames, placeholders documenting those contracts and historical identities.

**Change files — measure and gate:** `scripts/production/role_terms.toml`; `scripts/production/roles_audit.py`; `scripts/production/test_roles_audit.py`. Add explicit inflected forms for the worker aliases to the data and use those forms in both Names and `check`; do not apply an English plural rule to every command or term. Add per-file kept contexts with reasons, instead of extending today's global masking. In `test_roles_audit.py`, add fixture tests for singular/plural matches, overlap, protocol/history contexts, a context copied into another file, changed kept text and a returning retired use. Add a source-only repository check test in this same discovered test file, so existing `merge_gate --tests-only` runs it in the unit and at merge. No new merge-gate dispatch code is needed. It reads repository sources, not home-state records. Tests that prove behavior must still fail on an eligible re-add.

**Change files — assertions of changed output:** `scripts/shutdown/test_restart.py:435`, `scripts/production/test_rename_unit.py:553`, `scripts/agents/test_mesh_runs.py:183`, and `scripts/production/test_retire_unit.py:214`. Update their role-label expectations with the source output. Keep checks for thread identity, launcher arguments, worker names, refusal behavior and dry-run side effects. The expanded search found the retire dry-run assertion in addition to the first review's pinned tests. Checked `scripts/agents/test_codex_mesh.py` reopen assertions and `scripts/direct/test_implement_launcher.py` claim refusal assertions retain their existing expectations: those strings do not change. Test files stay outside the Names denominator because `read_live_files` skips `test_` scripts.

**Reviewed and kept live files:** `docs/as-built/message-clarity.md:269` is a retirement record preserved at commit `291e1da55`, dated 2026-10-16 by blame; keep its old word as history, not an allowed role label. `docs/as-built/usage-ledger.md:46` and `scripts/usage/collect.py:532–534` document or implement the old `seats` data value migration; keep those exact values. These files belong to the measured source set and are reviewed in this phase, not deferred to a later rename. Raw source matches in `scripts/accounts/worker_names.py`, `scripts/agents/agent_bg.sh`, `scripts/buildlog/index.py`, `scripts/direct/worker_state.py`, `scripts/feature_log/inventory.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/record.py` and `scripts/shutdown/stop.py` are identifiers, stored keys or migration values, not worker-role prose. Keep them. This classification covers every worker-role singular/plural use found by the read-only search over the audit's scanned folders, plus uncounted source prose above; it makes no claim about files the scanner excludes.

**Director-owned memory cleanup:** review agent-read prose under `~/.claude/projects/*/memory/` with the approved names. Correct current role explanations as director-owned cleanup, outside this worker's repository Work Order. Keep dated history, identities and quoted command output as recorded. Memory folders are not in the audit's Names set and earn no count credit. Do not send a question to the user for routine memory maintenance under an already approved naming policy.

**Read-only classification, 2026-10-20:** the following table uses the current audit file filters, extraction and global kept-path masking, then applies `(?<![\w-])seats?(?![\w-])` case-insensitively. It adds only the explicit plural form to today's singular matcher. Each cell is eligible / kept. Every occurrence not listed in the kept contexts below is eligible role prose. Script and combined counts remain lower bounds. Source-only comments/docstrings excluded by the extractor receive no numeric credit.

| Measured live file | Singular eligible / kept (n) | Plural eligible / kept (n) |
| --- | --- | --- |
| `commands/showrunner/rename_unit.md` | 5 / 0 | 2 / 0 |
| `commands/showrunner/retire_unit.md` | 0 / 0 | 2 / 0 |
| `commands/unit/direct.md` | 8 / 0 | 0 / 0 |
| `docs/as-built/agent-registry.md` | 1 / 0 | 1 / 0 |
| `docs/as-built/long-lived-workers.md` | 5 / 0 | 0 / 0 |
| `docs/as-built/message-clarity.md` | 0 / 0 | 0 / 1 |
| `docs/as-built/shutdown-and-restart.md` | 20 / 11 | 7 / 3 |
| `docs/as-built/unit-rename.md` | 1 / 0 | 1 / 0 |
| `docs/as-built/usage-ledger.md` | 0 / 0 | 0 / 1 |
| `docs/delegate/dual_review.md` | 1 / 0 | 0 / 0 |
| `docs/production_format.md` | 0 / 0 | 1 / 0 |
| Prose subtotal, n=11 files | 41 / 11 | 14 / 5 |
| `scripts/agents/codex_mesh.py` | 2 / 0 | 0 / 0 |
| `scripts/agents/mesh_runs.py` | 1 / 0 | 0 / 0 |
| `scripts/direct/implement.sh` | 1 / 0 | 0 / 0 |
| `scripts/direct/phase_reservation.py` | 0 / 0 | 1 / 0 |
| `scripts/production/rename_unit.py` | 1 / 0 | 2 / 0 |
| `scripts/production/retire_unit.py` | 0 / 0 | 2 / 0 |
| `scripts/shutdown/restart.py` | 1 / 0 | 0 / 0 |
| `scripts/usage/collect.py` | 0 / 0 | 0 / 2 |
| Script subtotal, n=8 files | 6 / 0 | 5 / 2 |
| Total, n=19 measured live files | 47 / 11 | 19 / 7 |

**Kept contexts:**

- Stored field: `docs/as-built/shutdown-and-restart.md:116`, literal `seat` (n=1 singular).
- Command/output placeholders: that file `:120`, `--to <seat>`, `reopened <seat>`, printed `--to <seat>` and `codex_mesh: <seat>: <reason>` (n=4 singular); `:143`, `<seat> (<role>, was working|idle): <command>` (n=1 singular). These document protocol arguments; ordinary role prose and the newly changed live message quotation change together.
- Filename placeholders: that file `:116`, `<run_dir>/<seat>.resume-prompt.md`, and `:122`, `<seat>.account-move.json` (n=2 singular). Today's masking leaves those placeholders partly visible; keep that fact in the lexical report.
- Dated probe history: that file `:290`, the 2026-10-16 run narrative (n=3 singular, n=3 plural). Keep its recorded wording and worker identity. Unlike the prior classification, this classification retains the full dated record, not just its identity.
- Retirement history: `docs/as-built/message-clarity.md:269` at `291e1da55`, dated 2026-10-16 (n=1 plural).
- Legacy data value: `docs/as-built/usage-ledger.md:46` (n=1 plural) and `scripts/usage/collect.py:532–534` SQL migration values (n=2 plural). They remain inputs the migration must recognize, not names for current workers.

**Before and target:** singular n=58 reconciles with the checker's n=58 exactly; plural adds n=26, giving n=84 in n=19 live files. Eligible n=66 comprises prose n=55 and script n=11; kept n=18 comprises prose n=16 and script n=2. Singular eligible is n=47 and plural eligible n=19. With the same extraction the lexical result should become singular n=11 plus plural n=7, total n=18, while scoped eligible uses become zero. Phase 4's half-size test uses n=66 as the expected reduction. Re-run this classification before the build Work Order; source drift requires a new frozen classification. Do not claim adding plural detection or retaining more history as a rename saving. Keep the singular-only before/after report as well as the expanded report. `Codex seat` and `Codex seats` overlap these matches and earn no extra credit. Definitions, production names and sent contacts have no target here.

**Why first:** this narrow role meaning has a classified plural-aware pool and a documented drift event after retirement. G-A adds matcher, context data, fixture and integration work, but later approved retirements reuse it. The judge supports drift prevention, not a measured work rate. The ranking remains conditional on the false-failure replay at the start of the build phase; a prose count alone does not rank implementation effort. This keeps the first phase measurable without pretending the other broad aliases are classified defects.

**Undo:** revert role prose and pinned expectations with the inflection/retirement entries and check test. Preserve identities and migration inputs. Keep the before records. No live session, transport key, file path or thread moves during this rename or its undo.

### 2. Name the coordinator by its actual role

**Needs the user and dependencies:** Q6 and approval of N-A for orchestrator; Step 1's context check is available. Phases-unit supplies its command and document-pipeline edits. No role authority changes.

**Change files:** `commands/plan/phase_review.md`, `commands/plan/shrink.md`, `commands/plan/to_as_built.md`, `commands/plan/to_phased_plan.md`, `commands/team_review.md`, `docs/delegate_plan_format.md`, `scripts/production/role_terms.toml`, `scripts/production/test_roles_audit.py`. Use the actual showrunner or unit director in production instructions; use invoking session for standalone execution. The policy fixture covers this term and does not alter protocol names. The source search found no pinned output test for these prose files.

**Before and acceptance:** the checker facts give orchestrator n=18 in n=6 prose files for each role pair. All broad coordinator references in that set become the scoped role or invoking session. Target no eligible orchestrator use; each pair falls by its observed n=18. These are the same source occurrences, not independent savings. Freeze a new before classification if history or source drift adds a retained use. Other measures have no target.

**Undo:** restore that source set and its retirement entry together; preserve Step 1's independently approved worker policy.

### 3. Correct current command references and keep compatibility

**Needs the user and dependencies:** approve the R-command current-reference migration only. Q7 does not authorize alias removal. Step 1 supplies the scoped check. The forwarding command, historical command recognition and thread records remain valid. Removal is later work requiring a separate answer.

**Change files:** `CLAUDE.md:7`, `commands/showrunner/promote_showrunner.md:36`, `scripts/production/promote_showrunner.py:100`, `scripts/production/test_promote_showrunner.py:210`, `scripts/production/role_terms.toml`, `scripts/production/test_roles_audit.py`. Correct CLAUDE.md's wrong statement: `/unit:direct` imports decision criteria at `commands/unit/direct.md:673`; the alias merely forwards. Correct the current command name in the promotion instruction/refusal and its expected text, retaining the active-run refusal and identity/argument checks. No alias is deleted.

**Reviewed and kept files:** `docs/as-built/agent-registry.md:315,432` documents forwarding and discovery behavior; `docs/as-built/director-model-study.md:27,108` describes old transcript recognition and historical study results; `scripts/model_study/turns.py:17` must recognize historical `/unit:delegate` records. `commands/unit/delegate.md` stays as the forwarding control file. Checked `scripts/model_study/test_turns.py:257,269,479` and `scripts/model_study/test_rerun.py:182,274` fixtures retain old transcript inputs; do not convert historical recognition tests to current callers.

**Before and acceptance, read-only classification 2026-10-20:** the checker's alias pool has n=9 hits in n=6 live files. The current-reference uses above give n=3 eligible (prose n=2, script n=1); kept n=6 (prose n=5, script n=1). The kept prose consists of agent-registry n=3 and director-model-study n=2; the kept script match is historical command recognition. Target no eligible old current reference and lexical n=6 kept in the unchanged measured set. Script/combined counts are lower bounds. Test files are change/check files outside that denominator. Prove the live-run refusal still fires, forwarding preserves arguments, and old and new transcript commands remain recognized. Definitions, session names and sent contacts have no target.

**Undo:** restore these current-reference statements, refusal expectation and scoped policy together. The forwarding command and history parser never need removal or restoration in this phase.

## Later work

This list replaces the former repeated-duty and per-term programmes, including former Steps 9–11. None is a build Work Order. Before promoting one item to a numbered step, select exactly one duty or term, paste all its exact change and kept files, name dependencies, freeze a before classification and state an acceptance target and undo. No measured live file for that selected rename may wait for another slice. Test expectations of changed strings belong in its change set even though test files earn no Names credit. An item with an unknown eligible size or a missing external contract stays here.

### Merge-branch prose (former Step 4)

R-trunk needs a new user name answer and a classification of trunk n=26 in n=8 prose files from the checker facts: `commands/berth_fix.md`, `commands/plan/to_as_built.md`, `commands/showrunner/dailies.md`, `commands/showrunner/produce.md`, `docs/as-built/director-model-study-results.md`, `docs/as-built/director-model-study.md`, `docs/as-built/review-pool.md`, `docs/production_format.md`. Preserve actual refs, cargo-berth meaning and history. An eligible count is not yet recorded, so this is not phase-ready. Its later target is zero eligible retired role use; undo restores only role prose and its policy, never branch refs.

### Definition authority and worker classification (former Step 5)

T-A or T-B needs its own user answer and the deterministic generator/stale-copy test named in Candidates. Handle one approved meaning in each later build phase. Proposed control files are `scripts/production/role_terms.toml`, `scripts/production/roles_audit.py`, `scripts/production/test_roles_audit.py` and `docs/production_format.md`; select the term's source files before dispatch.

The worker finding is a false duplicate, not a removal target. `docs/as-built/session-roster.md:60` specifies process checks, status conversion, cwd choice and director links. Keep all that implementation rule; add a link to the general worker definition. Correct Definitions classification with fixtures that distinguish role selection from a competing definition, retaining the old result beside the corrected one. The observed worker n=2-file finding in the n=36-term set must disappear for the correct reason; it earns no reduction in actual repeated duties or definitions. The helper still has n=0 defining files, but Q7 must settle its scope before adding one. These are separate term/classification tasks, not one combined definition move. Undo restores authority links and schema with the affected term; never deletes the roster selection rule.

### Reporting default and duty-source measurement (former Step 6)

O20-A is recommended; O20-B is the setup/runtime alternative. Ask the user about the source resolution and P-A before changing it. The checked sources are `docs/production_format.md:100–103` (on demand when absent), `commands/showrunner/produce.md:393–394` (15 minutes when absent), `commands/showrunner/produce.md:412–418` (timer creation/stop, already agrees), and `commands/producer/to_production.md:118` (every 15 minutes, contrary to PF). Correct the producer line and SR's introductory absent-default clause against PF. Keep SR's actual setup/stop behavior; it is in the checked acceptance set, not a policy conflict to remove.

Before dispatch, add a typed duty-source measure proposal using `scripts/production/role_terms.toml`, `scripts/production/roles_audit.py` and `scripts/production/test_roles_audit.py`, recording every cited clause, owner and reference. The current baseline does not count D20's source clauses. Print that new before set before fixing a numeric target. The later acceptance set must cover every source clause for agreement with the approved runtime owner and on-demand default, with no clause dropped. A matrix relabel is not a measure. Undo restores the source clauses and registry together and starts no timer.

### Account-move readiness (former Step 7)

O22-A keeps the showrunner executor after the director confirms no worker run is active. O22-B transfers execution and needs its own answer. Checked change candidates are `commands/showrunner/produce.md`, `commands/showrunner/move.md` and `commands/showrunner/accounts.md`; the read-only source is `~/.claude/projects/-home-natepiano--claude/memory/ask-director-before-moving-unit.md`. Keep natedev's shared role map. This item depends on the duty-source measure, its exact before clause count and a separate O22 answer. Target agreement across D22's cited clauses, plus functional pause/resume proof; no measured contact saving is claimed. Undo restores the instructions and registry, not a live account move.

### Any-time unit rename (former Step 8)

Recommend O23-A: keep the showrunner executor and the current any-time, stop-at-once rule. `docs/as-built/unit-rename.md:53` records the user's 2026-10-18 rule; `scripts/production/rename_unit.py:77` records immediate stopping (2026-10-20). The source correction is PF's between-phases phrase, not the helper. Checked files are `docs/production_format.md:158–161`, `commands/showrunner/rename_unit.md:8,22–30`, `commands/unit/rename.md`, the as-built and the helper. Only PF timing text and the later duty registry need change for O23-A; keep other behavior and test assertions. This needs the source-resolution answer and the duty measure's full before set; no numeric clause size has been measured. Target all D23 clauses agreeing with the existing any-time behavior. A specific name approval is separate. O23-B is a Q9 policy proposal: waiting may reduce interruption, but delays the rename and reverses current permission. It is out without a new answer. Undo restores only the PF/source-registry correction, never renames a live unit.

### One selected duplicate duty at a time (former Step 9)

P-A and P-B cover D01–D06, D08–D15, D17–D19, D21, D24, D26–D28, D33–D34, D37–D40, D42, D48 and D54. Select one row and its exact cited files from the Duty matrix, not this whole programme, after the source-policy answer. D16 already has one owner and earns no duplicate-removal target; its source remains available for other rows. A later Work Order must state the before operating-statement count, the one chosen authority and valid linked references. A genuinely different scoped action stays separate rather than being deleted. D01/D09 link to the user's authority; D15 needs guilds-unit's role check; D26/D54 go through fix-rounds-unit and phases-unit. D53/D55 keep their current rules. This item cannot use an unmeasured aggregate count as acceptance. Undo restores only the selected row's statements and links.

### Guild meanings and tips (former Step 10)

Q1 and Q5 remain open; Q3 is answered above (2026-10-28). O32-A is recommended, subject to guilds-unit's actual contract; O32-B is the editor alternative. Sources are `docs/as-built/guilds.md` and `/home/natepiano/worktrees/claude-build-followups-sdlc/docs/plans/sdlc-production.md`. Each Astra ID has n=2 definition sources in the checked n=36-term set, but head and per-design meanings must be approved separately before a target can be stated. The heads shipped as `designers`, `reviewers`, `showrunners` and `units`, and `astra-designer` / `astra-reviewer` now name only the per-design roles and census edges. The shared-tips contract shipped in Guilds, Tips: each head owns its guild's tip files and publishes them with `tips.py publish`. Undo of a later approved meaning/contract change must be coordinated with those owners and must not rename a running head.

### Alias removal, meanings and remaining source pools (former Step 11)

Removing `/unit:delegate` is distinct from Step 3's current-reference migration. It needs a compatibility answer, discovery checks, exact removal/control files and a before measure before it can become a Work Order. Keeping forwarding is a buildable answer. Preserve historical transcript recognition under either answer.

Q2 covers dependency, verification and required user-interaction meanings of gate. Checked defining files are `docs/production_format.md` and `commands/unit/direct.md`; the latter's `<TurnEndGate>` meaning is not a verification check. Keep `gate:` status syntax unless compatibility is separately approved. Q4 covers filed proposal, unfiled add-on and implemented change using `commands/unit/add_ons.md`, `commands/showrunner/proposal.md` and `commands/change_review.md`. Q8 covers phase as-built record and feature as-built doc; phases-unit owns the pipeline. Each later term phase must classify its complete source-spelling set, not infer a file list from its proposed target spelling.

That rule includes defined names: template, lease and reviewer are current source spellings, even though review template and review lease have no observed alias hits. The following source pools were re-counted read-only on 2026-10-20 with the current audit filters and exact singular matcher. They are search sets, not approved replacement lists or eligible counts. Python/shell and combined counts are lower bounds. Before a Work Order, inspect every meaning, include plural forms, classify protocol/history and pin changed output tests. Add the chosen definition source and control files only after the authority policy is approved. Broad templates unrelated to review, machine keys, active-review ownership and other reviewer roles must not be renamed together.


<details>
<summary>template source spelling: n=84 hits in n=23 live files</summary>

| Source file | Hits (n) |
| --- | --- |
| `commands/bevy_migration_plan.md` | 2 |
| `commands/issue.md` | 3 |
| `commands/nate.md` | 6 |
| `commands/plan/phase_review.md` | 1 |
| `commands/showrunner/dailies.md` | 2 |
| `commands/unit/direct.md` | 2 |
| `docs/as-built/feature-log.md` | 1 |
| `docs/as-built/long-lived-workers.md` | 3 |
| `docs/as-built/message-clarity.md` | 7 |
| `docs/as-built/nextest-width-and-lint-scope.md` | 1 |
| `docs/as-built/review-pool-probes.md` | 9 |
| `docs/as-built/review-pool.md` | 22 |
| `docs/delegate_footprint_review.md` | 2 |
| `docs/production_format.md` | 2 |
| `docs/style_production.md` | 1 |
| `docs/user_facing_explanation.md` | 2 |
| `scripts/agents/codex_mesh.py` | 2 |
| `scripts/direct/review_pool.py` | 4 |
| `scripts/make_a_worktree/copy_settings_local.sh` | 2 |
| `scripts/message/send.py` | 4 |
| `scripts/new_rust_project/rust_generate.sh` | 3 |
| `scripts/notify/pushover.py` | 2 |
| `scripts/production/dailies_render.py` | 1 |

</details>

<details>
<summary>lease source spelling: n=25 hits in n=6 live files</summary>

| Source file | Hits (n) |
| --- | --- |
| `docs/as-built/review-pool-probes.md` | 1 |
| `docs/as-built/review-pool.md` | 17 |
| `docs/as-built/showrunner-automation.md` | 3 |
| `docs/production_format.md` | 1 |
| `scripts/direct/review_pool.py` | 1 |
| `scripts/direct/review_pool_report.py` | 2 |

</details>

<details>
<summary>reviewer source spelling: n=252 hits in n=40 live files</summary>

| Source file | Hits (n) |
| --- | --- |
| `commands/api_review.md` | 6 |
| `commands/astra_review.md` | 1 |
| `commands/commit_prep.md` | 1 |
| `commands/module_review.md` | 8 |
| `commands/plan/phase_review.md` | 2 |
| `commands/plan/shrink.md` | 1 |
| `commands/prioritize.md` | 1 |
| `commands/showrunner/produce.md` | 1 |
| `commands/showrunner/retire_unit.md` | 1 |
| `commands/unit/direct.md` | 23 |
| `commands/unit/report.md` | 3 |
| `docs/as-built/agent-registry.md` | 6 |
| `docs/as-built/herdr-trial.md` | 1 |
| `docs/as-built/long-lived-workers.md` | 14 |
| `docs/as-built/message-clarity.md` | 21 |
| `docs/as-built/plan-delegate-progress-history.md` | 3 |
| `docs/as-built/review-pool.md` | 85 |
| `docs/as-built/review-regime-trial.md` | 11 |
| `docs/as-built/session-roster.md` | 7 |
| `docs/as-built/showrunner-automation.md` | 3 |
| `docs/as-built/unit-rename.md` | 1 |
| `docs/as-built/usage-ledger.md` | 1 |
| `docs/decision_criteria.md` | 2 |
| `docs/delegate/dual_review.md` | 14 |
| `docs/delegate/launch_implementation.md` | 1 |
| `docs/delegate/phase_end.md` | 1 |
| `docs/delegate/write_prompt_contract.md` | 2 |
| `docs/delegate_footprint_review.md` | 3 |
| `docs/delegate_plan_format.md` | 1 |
| `docs/production_format.md` | 6 |
| `docs/quota_alerts.md` | 1 |
| `docs/user_facing_explanation.md` | 1 |
| `scripts/agents/codex_mesh.py` | 3 |
| `scripts/direct/progress_history.py` | 3 |
| `scripts/direct/review_pool.py` | 1 |
| `scripts/message/roster.py` | 2 |
| `scripts/notify/review.py` | 3 |
| `scripts/production/rename_unit.py` | 1 |
| `scripts/production/retire_unit.py` | 3 |
| `scripts/production/review_regime.py` | 3 |

</details>

For lease, first correct PF's phase-only definition against `scripts/direct/review_pool.py:32,676`: the unit assignment lasts, while claiming/releasing an active review changes process ownership. Keep the `--lease` API and no-op compatibility entry unless an independent compatibility answer changes them. For reviewer, classify pool, per-design Astra, blind, one-turn and other review roles before Q3 or Q5 selects a name. For template, classify review instructions apart from message, project and output templates. A proposed target with no hit is not a source inventory.

All other aliases and related terms stay proposed under the N-A treatment table, with their Other-name file index as one input, not a sufficient source set. Add defined-name and plural matches whenever those spellings would change. Include zero-hit policy entries only with a measured definition task or keep them unbuilt. Every later rename needs a frozen eligible size, kept contexts and an undo of that term's prose/policy together. Meaning IDs and inventory versions must stay beside before/after Definitions; adding a meaning or changing a parser is not source cleanup by itself. No aggregate promise of fewer production names or contacts is made.

After each approved numbered phase, run `~/.claude/scripts/lib/py scripts/production/roles_audit.py baseline`. Record its term-set version, file set, count and window beside the before result. Keep any contact comparison on one fixed window. Run the new source-only check through the unit's tests-only gate after its policy is approved. A step is complete only when its stated measure moves and the functional checks pass. Source-only tests must not read real home-state records or send messages.

## Role record

R1b and R5b, built in phase 5. A session's name and its role are separate. Every roster record carries its role in its own fields, apart from its name. Each shown name has the form production · unit · job. The address does not change.

**Three meanings of role.** They stay apart, and each keeps its routing and permission meaning.

| Meaning | Where it lives | Values | Shown job |
| --- | --- | --- | --- |
| Organizational role | `RosterEntry.role` in `scripts/message/roster.py` | `showrunner`, `unit director`, `worker`, `freestanding`, `astra-designer`, `astra-reviewer` | `showrunner`, `unit director`; `astra-designer` → `designer`; `astra-reviewer` → `design reviewer`; a worker takes its job from its assignment; `freestanding` has no job |
| Worker assignment | `role` in each `mesh_roster.json` record | `impl`, `test`, `fix`, `review`; a record with no `role` is unrecorded; any other value is unreadable | `impl`, `test`, `fix` → `worker`; `review` → `code reviewer`; unrecorded or unreadable → no job, with the reason |
| Guild responsibility | guild records | unchanged | not shown in this phase |

An Astra held role keeps its own assignment state; it is not a worker assignment. A review-pool reviewer named `<type><n>` (`code6`, `ui2`) has the job `code reviewer` or `UI reviewer` with the number `n`, and the unit of its current lease. The roster finds pool reviewers itself: each has the address `worker:review-pool/<production>/<run>/<reviewer>` and its director is the unit director that holds the lease. The name never supplies a role: a record without a role keeps its explicit state.

**Shown name.** Parts join with ` · `. The production part is the production slug. The unit part is the unit name without its `-unit` suffix. The job part is the job word, then the slot or the number that tells several holders apart. An absent part is left out. A session with no production, no unit and no job shows its address.

| Record | Shown name |
| --- | --- |
| sdlc pool reviewer `code6`, leased to roles-unit | `sdlc · roles · code reviewer 6` |
| roles-unit director | `sdlc · roles · unit director` |
| roles-unit worker in slot `test` | `sdlc · roles · worker test` |
| roles-unit worker in slot `impl`, doing review | `sdlc · roles · code reviewer impl` |
| sdlc showrunner | `sdlc · showrunner` |
| Astra designer held for a roles-unit design | `sdlc · roles · designer` |
| freestanding session | its address, such as `session:<session id>` |

**Where it shows.** Codex thread titles (set again at each turn, so a lease move or role change shows at the next turn), the roster's own output, broadcast recipient lines and the sender a reader sees. A broadcast recipient line reads `<shown name> [<address>] - <outcome>`; the address shows only when it differs from the shown name, and there is no separate role column. `send.py` reads the shown sender once, on the sending machine, from the same roster read that finds the recipient. Relay, Codex and daemon deliveries then carry a `Shown sender: <name>` line after the first line of the body. A user push does not change. Cost: a send to a socket or a named machine reads the roster once more, about 1 s (319 entries). `roster.py --json` adds the shown name and keeps every existing field and value. Addresses, `--to` and `--from` values, sockets, thread ids and the stored fields `seat` and `codex_seats` stay as they are.

**Column owners.** The unit board's owner and cargo-liner show the session name and the shown job as two columns. They read both from `roster.py --json`. Sending them this definition does not count as their change landing.

## Next steps

The adversarial judge and Astra review dated 2026-10-20 are folded into Candidates, Comparison, Recommendation and Steps. The director checked these repairs, recorded the source-only scan time, and sends the showrunner the Recommendation and Step 1 as a question with buildable answers and the recommended answer. The false-failure replay runs first in Step 1's build phase. Use only a numbered phase-ready step for a build Work Order; promote later work only after its exact files, dependencies and before/target are complete. No open question or later candidate is approved by accepting another step.

## Step log

### Step 1 freeze — 2026-10-28, phase 3

Source tree before the first rename: `2bd53a04b4c42ac7c1b4f0f72c8498e8b01a9f7b`; the pre-existing edit to `docs/plans/sdlc-roles.md` is outside this worker's file set. Read-only classification uses the current `roles_audit.read_live_files(Path('.'))` extraction and `without_paths(text, tuple(name for term in terms for name in term.kept))`, followed by `re.finditer(r'(?<![\w-])seats?(?![\w-])', text, re.I)`. Command: `python3 - <<'PY'` importing `roles_audit` from `scripts/production`, loading `scripts/production/role_terms.toml` with `parse_terms`, and applying those calls to each returned live file. This reads source only and starts no session or message. The extracted file set is n=563 live files; read issues n=0. Files without a match are omitted from the table. Each cell is eligible / kept; overlapping `Codex seat` and `Codex seats` earn no extra credit.

Before baseline recorded by the unit director at 13:37 EDT on 2026-10-28: `python3 scripts/production/roles_audit.py --root /tmp/claude-1000/-home-natepiano-worktrees-claude-build-followups-sdlc-roles/a58b133c-41b8-408d-967e-e41b631ab545/scratchpad/before_tree --since 2026-10-14 --until 2026-10-27 baseline`. The root is a `git archive` export of pre-change tree `2bd53a04b4c42ac7c1b4f0f72c8498e8b01a9f7b`. Source set: n=563 live files. Names reports worker / `seat`: n=69 in n=14 files; worker / `Codex seat`: n=9 in n=4 files. These alias counts overlap and are not added. The output is `/tmp/claude/unit/d739a3b3-2f5e-42d2-a9c8-d79b5de75d2a/phases/3/before_baseline.md`; source issues n=0. Its message window is [2026-10-14T00:00:00-04:00, 2026-10-28T00:00:00-04:00), labelled America/Curacao in that output. This is the director’s recorded before baseline, separate from the plural-aware source classification below.

| Measured live file | Singular eligible / kept (n) | Plural eligible / kept (n) |
| --- | --- | --- |
| `commands/showrunner/rename_unit.md` | 5 / 0 | 2 / 0 |
| `commands/showrunner/retire_unit.md` | 0 / 0 | 2 / 0 |
| `commands/showrunner/transfer_unit.md` | 0 / 0 | 1 / 0 |
| `commands/unit/direct.md` | 8 / 0 | 0 / 0 |
| `docs/as-built/agent-registry.md` | 1 / 0 | 1 / 0 |
| `docs/as-built/build-memory-admission.md` | 1 / 1 | 0 / 0 |
| `docs/as-built/guilds.md` | 1 / 0 | 0 / 0 |
| `docs/as-built/long-lived-workers.md` | 5 / 0 | 0 / 0 |
| `docs/as-built/message-clarity.md` | 0 / 0 | 0 / 1 |
| `docs/as-built/shutdown-and-restart.md` | 25 / 14 | 7 / 3 |
| `docs/as-built/unit-rename.md` | 1 / 0 | 2 / 0 |
| `docs/as-built/usage-ledger.md` | 0 / 0 | 0 / 1 |
| `docs/delegate/dual_review.md` | 1 / 0 | 0 / 0 |
| `docs/production_format.md` | 0 / 0 | 1 / 0 |
| Prose subtotal, n=14 matching files | 48 / 15 | 16 / 5 |
| `scripts/agents/codex_mesh.py` | 2 / 0 | 0 / 0 |
| `scripts/agents/mesh_runs.py` | 1 / 0 | 0 / 0 |
| `scripts/direct/implement.sh` | 1 / 0 | 0 / 0 |
| `scripts/direct/phase_reservation.py` | 0 / 0 | 1 / 0 |
| `scripts/production/rename_unit.py` | 1 / 0 | 2 / 0 |
| `scripts/production/retire_unit.py` | 0 / 0 | 2 / 0 |
| `scripts/production/transfer_unit.py` | 0 / 0 | 1 / 0 |
| `scripts/shutdown/restart.py` | 1 / 0 | 0 / 0 |
| `scripts/usage/collect.py` | 0 / 0 | 0 / 2 |
| Script subtotal, n=9 matching files | 6 / 0 | 6 / 2 |
| Total, n=23 matching live files | 54 / 15 | 22 / 7 |

Singular-only before n=69; plural before n=29; combined before n=98. Eligible n=76 (prose n=64, script n=12); kept n=22. Complete retirement target: singular n=15, plural n=7, combined n=22. With the n=8 director-owned singular uses pending and the n=1 singular at the absent replacement path pending, this writer's expected reduction is n=67 (singular n=45, plural n=22); interim lexical target n=31 (singular n=24, plural n=7). Kept history is not a rename saving. Script counts remain lower bounds: comments, docstrings and some f-string text excluded by extraction earn no extra count credit.

Kept contexts, with reasons:

- `docs/as-built/shutdown-and-restart.md`: literal stored field `seat` (n=1 singular); all `<seat>` placeholders (n=10 singular after existing path masking, including filename fragments, reopen arguments/output and idle/working resume command examples) document unchanged protocol or filename contracts. Keep the exact source spelling and its file scope. Keep the full dated 2026-10-16 resume-workers probe paragraph (n=3 singular, n=3 plural), including recorded output and worker identity. Total n=14 singular, n=3 plural. Plain role prose and the live `Each seat continues…` quotation are eligible.
- `docs/as-built/build-memory-admission.md`: the sentence beginning `Before this, a seat started one lint run three times` through `build time.` (n=1 singular) records the dated 2026-10-21 hana_catalyst-inspectors incident. Keep the record; the preceding current-role explanation changes.
- `docs/as-built/message-clarity.md`: the retirement record at `291e1da55`, dated 2026-10-16 by blame (n=1 plural).
- `docs/as-built/usage-ledger.md`: the old stored `seats` migration value (n=1 plural); `scripts/usage/collect.py`: the two SQL migration input values (n=2 plural). They remain recognized input values.
- Identifiers, API and JSON fields (`seat`, `codex_seats`, `PausedSeat`), flags, transport addresses, filenames and placeholders documenting them remain unchanged. Raw source uses in `scripts/accounts/worker_names.py`, `scripts/agents/agent_bg.sh`, `scripts/buildlog/index.py`, `scripts/direct/worker_state.py`, `scripts/feature_log/inventory.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/record.py` and `scripts/shutdown/stop.py` are contracts or migration values, not eligible role prose.
- Temporary check exceptions at opening: `commands/unit/direct.md` had n=8 eligible singular uses pending phases-unit ownership. Repair round 1 refreshes that count to n=9 and the exact lines below. The old-path `dual_review.md` mismatch at opening is resolved in repair round 1. Only the director-owned request has authorized exact-context check exceptions; do not mask files wholesale.

Pre-edit dependencies read: `commands/showrunner/produce.md` `<Guild>`; `docs/production_format.md` `<GuildHeads>` (there is no `<Guild>` tag in that file); hierarchies Map change via `git -C /home/natepiano/worktrees/claude-build-followups-hierarchies show d14a7368 -- docs/plans/build-followups-hierarchies-design.md`. The Map distinguishes four guild heads from held per-design Astra roles. `produce.md` has no eligible worker-seat spelling; no edit is needed there. The 2026-10-20 replay stands by the Work Order amendment; it is not repeated.

Replay lesson: when a retirement leaves a live key, value or flag, add that contract to the per-file kept list with its reason in the same change. A kept context copied to another file or changed text must require fresh classification.

### Director-owned change requests — refreshed at `8b3b83bd`

**Applied (2026-10-28):** phases-unit is paused, so by the showrunner's call the unit director applied the 9 replacements below in `commands/unit/direct.md` in the Step 1 checkpoint and removed the 8 pending `direct.md` kept contexts from `role_terms.toml`. No eligible use remains pending; phases-unit receives an also-touches line.

Exact whole-line replacements below match the merged source byte for byte, including leading spaces. These change n=9 singular uses on n=8 lines and no duties. Line 863 is new in the merged tree; line 886 also has new upstream text.

Line 374:

```text
OLD: launcher is attached carries the result to you: dispatch that seat
NEW: launcher is attached carries the result to you: dispatch that worker
```

Line 474:

```text
OLD: never open a new seat for a role a paused seat holds. A forced stop without this
NEW: never open a new worker for a role a paused worker holds. A forced stop without this
```

Line 863:

```text
OLD: | First touch to `Active` | The Codex seat hook, or `phase_reservation.py record-active` at <CheckpointCommit/> step 5 |
NEW: | First touch to `Active` | The Codex worker hook, or `phase_reservation.py record-active` at <CheckpointCommit/> step 5 |
```

Line 886:

```text
OLD: | `Active` | A clear check that acquired or already held the scopes: the Codex seat hook, or the checkpoint backstop for a first touch no hook recorded. A later clear check that names another reservation of the same phase replaces it, as after berth released the recorded one. `repair` may restore it from berth's session map only after the board confirms it is active | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
NEW: | `Active` | A clear check that acquired or already held the scopes: the Codex worker hook, or the checkpoint backstop for a first touch no hook recorded. A later clear check that names another reservation of the same phase replaces it, as after berth released the recorded one. `repair` may restore it from berth's session map only after the board confirms it is active | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
```

Line 954:

```text
OLD:    two open, it is how a seat's edit finds its phase, so keep each path in
NEW:    two open, it is how a worker's edit finds its phase, so keep each path in
```

Line 955:

```text
OLD:    exactly one phase's list and add a path a seat's refusal names. Then invoke
NEW:    exactly one phase's list and add a path a worker's refusal names. Then invoke
```

Line 973:

```text
OLD:    atomically acquires exact `file:` scopes before the edit. A Codex seat's
NEW:    atomically acquires exact `file:` scopes before the edit. A Codex worker's
```

Line 990:

```text
OLD:    leaves the pending state intact; a seat reports the refusal to you, and you
NEW:    leaves the pending state intact; a worker reports the refusal to you, and you
```

The former `docs/delegate/dual_review.md` path is now `docs/direct/dual_review.md`. F001 is in this writer’s file set; the line 38 replacement is applied in repair round 1, so no routing exception is needed.

### Source classification after the opening writer edits — `2bd53a04`

Read-only command (run from the worktree root; no tests, state reads or session starts):

```sh
python3 - <<'PY'
import re
import sys
from pathlib import Path
sys.path.insert(0, 'scripts/production')
import roles_audit as audit
files, issues = audit.read_live_files(Path('.'))
pattern = re.compile(r'(?<![\w-])seats?(?![\w-])', re.I)
kept_paths = ('docs/delegate/', 'scripts/direct/', 'scripts/delegate/')
totals = [0, 0]
for file in files:
    text = audit.without_paths(file.text, kept_paths)
    counts = [sum(match.group().lower() == word
                  for match in pattern.finditer(text))
              for word in ('seat', 'seats')]
    if any(counts):
        print(file.path, *counts)
    totals = [left + right for left, right in zip(totals, counts)]
print('SOURCE CLASSIFICATION AFTER', len(files), 'files', totals,
      'total', sum(totals), 'issues', issues)
PY
```

The same extraction and original global kept-path masking give the following after counts, over n=563 live files, read issues n=0. The before classification used the same calls, then divided each match by the frozen per-file kept contexts above. This preserves comparability while the test worker adds scoped check exceptions.

| Remaining measured live file | Singular (n) | Plural (n) | Reason |
| --- | --- | --- | --- |
| `commands/unit/direct.md` | 8 | 0 | Eligible; pending phases-unit request above |
| `docs/as-built/build-memory-admission.md` | 1 | 0 | Dated incident kept |
| `docs/as-built/message-clarity.md` | 0 | 1 | Dated retirement kept |
| `docs/as-built/shutdown-and-restart.md` | 14 | 3 | Contract placeholders, stored field and dated probe kept |
| `docs/as-built/usage-ledger.md` | 0 | 1 | Migration value kept |
| `docs/delegate/dual_review.md` | 1 | 0 | Eligible; assigned replacement path absent |
| `scripts/usage/collect.py` | 0 | 2 | Migration values kept |
| Total, n=7 matching live files | 24 | 7 | n=31 combined |

Measured writer reduction: singular n=45 (69 to 24), plural n=22 (29 to 7), combined n=67 (98 to 31). Remaining eligible uses n=9, outside the writer's editable paths. Remaining kept uses n=22. The new plural matcher and kept history earn no rename credit. Source-only comment/docstring changes have no numeric credit. No identifiers, API/JSON fields, flags, transport addresses, filenames or historical identities changed.

The after baseline ran once the build hold ended; see **After measure** at the end of this Step log.

Execution record: the opening slot did not run verification under the build hold; the director supplied the before baseline above. The opening path mismatch is resolved by the upstream move to `docs/direct/`. Installed `board.sh post ... handoff` requires a machine-readable role prefix, so handoffs use `role=impl`. The replay is not repeated, per the amendment. No source rename for Q2/Q3 and no duty change were made.

### Repair round 1 — merged tree `8b3b83bd`

The director fast-forwarded to `8b3b83bd` and reapplied both slots' changes. Each of this writer's eight conflict blocks in six files first takes upstream's content; worker-role wording is then reapplied. This retains `UNIT_RUN_DIR`, `TMPDIR`, dev-shell behavior, durable model identity, the shared pause deadline and `_exit_words` output formatting.

The same read-only method uses n=596 live files, read issues n=0. For a comparable merged before set, `git archive HEAD` is read in memory with `tarfile`; each member passes the same folder, extension, test/fixture and plan filters as `read_live_files`. Scripts use `python_prose(text).text` or `shell_prose(text).text`; every file then uses `without_paths(text, ('docs/delegate/', 'scripts/direct/', 'scripts/delegate/'))` and `re.finditer(r'(?<![\w-])seats?(?![\w-])', text, re.I)`. No checkout, state source, build or test is involved. This measures the merged source before the role edits rather than mixing conflict markers or partially reapplied edits into its denominator. Cells remain eligible / kept. Kept contexts and reasons are the same as the opening freeze: stored field and placeholders, dated probe and incident, dated retirement, and migration input values. `Codex seat(s)` overlaps receive no extra credit.

| Merged before live file | Singular eligible / kept (n) | Plural eligible / kept (n) |
| --- | --- | --- |
| `commands/showrunner/rename_unit.md` | 5 / 0 | 2 / 0 |
| `commands/showrunner/retire_unit.md` | 0 / 0 | 2 / 0 |
| `commands/showrunner/transfer_unit.md` | 0 / 0 | 1 / 0 |
| `commands/unit/direct.md` | 9 / 0 | 0 / 0 |
| `docs/as-built/agent-registry.md` | 1 / 0 | 1 / 0 |
| `docs/as-built/build-memory-admission.md` | 1 / 1 | 0 / 0 |
| `docs/as-built/guilds.md` | 1 / 0 | 0 / 0 |
| `docs/as-built/long-lived-workers.md` | 6 / 0 | 0 / 0 |
| `docs/as-built/message-clarity.md` | 0 / 0 | 0 / 1 |
| `docs/as-built/rustdoc-once-and-final-targets.md` | 2 / 0 | 0 / 0 |
| `docs/as-built/shutdown-and-restart.md` | 26 / 14 | 8 / 3 |
| `docs/as-built/unit-rename.md` | 1 / 0 | 2 / 0 |
| `docs/as-built/usage-ledger.md` | 0 / 0 | 0 / 1 |
| `docs/direct/dual_review.md` | 1 / 0 | 0 / 0 |
| `docs/production_format.md` | 0 / 0 | 1 / 0 |
| `scripts/agents/codex_mesh.py` | 2 / 0 | 0 / 0 |
| `scripts/agents/mesh_runs.py` | 1 / 0 | 0 / 0 |
| `scripts/direct/implement.sh` | 1 / 0 | 0 / 0 |
| `scripts/direct/phase_reservation.py` | 0 / 0 | 1 / 0 |
| `scripts/production/rename_unit.py` | 1 / 0 | 2 / 0 |
| `scripts/production/retire_unit.py` | 0 / 0 | 2 / 0 |
| `scripts/production/transfer_unit.py` | 0 / 0 | 1 / 0 |
| `scripts/shutdown/restart.py` | 1 / 0 | 0 / 0 |
| `scripts/usage/collect.py` | 0 / 0 | 0 / 2 |
| Total, n=24 matching live files | 59 / 15 | 23 / 7 |

Merged before: singular n=74, plural n=30, combined n=104; eligible n=82, kept n=22. Compared with the opening tree's n=98, source drift adds n=6 eligible uses: direct.md n=1 singular, long-lived-workers.md n=1 singular, rustdoc-once-and-final-targets.md n=2 singular, and shutdown-and-restart.md n=1 singular and n=1 plural. The moved dual_review.md use keeps its original count. The whole-file scan after selecting upstream conflict blocks, before this repair's wording edits, reports n=45 (singular n=37, plural n=8) in n=10 matching files; that intermediate count includes opening edits already reapplied and is not a new retirement baseline.

New or newly found eligible uses outside this writer's files were routed to the unit director, who applied the replacements below in F006. The rustdoc paragraphs explain current build policy with an undated old role label; they are not dated incident records. Exact whole-line replacements follow. The README and inventory docstring are outside Names extraction, so their edits receive no count credit.

`docs/as-built/rustdoc-once-and-final-targets.md:5`:

```text
OLD: Rustdoc runs once per phase, as the unit director's last command before a Rust checkpoint commit. It does not run in every `verify.sh lint` call. A PreToolUse hook refuses a checkpoint commit in a Cargo repository unless a passing rustdoc run exists for the exact tree that the commit writes, so the check cannot be skipped. `lint.conf` cannot switch it off. `verify.sh final` type-checks every example once with `cargo check --all-targets`. Its nextest build no longer builds and links the examples again. A scan before any build refuses an example that holds a test or sets `test = true`, so no test is lost. The reason is fewer and shorter builds with the same guarantees: rustdoc used much seat time on each lint call and caught few errors, and the second example build in `final` added work but no checks.
NEW: Rustdoc runs once per phase, as the unit director's last command before a Rust checkpoint commit. It does not run in every `verify.sh lint` call. A PreToolUse hook refuses a checkpoint commit in a Cargo repository unless a passing rustdoc run exists for the exact tree that the commit writes, so the check cannot be skipped. `lint.conf` cannot switch it off. `verify.sh final` type-checks every example once with `cargo check --all-targets`. Its nextest build no longer builds and links the examples again. A scan before any build refuses an example that holds a test or sets `test = true`, so no test is lost. The reason is fewer and shorter builds with the same guarantees: rustdoc used much worker time on each lint call and caught few errors, and the second example build in `final` added work but no checks.
```

`docs/as-built/rustdoc-once-and-final-targets.md:111`:

```text
OLD: Rustdoc ran in every `verify.sh lint` call. It used much seat time and caught few errors. One run per phase, at the checkpoint, keeps the check and removes the repeats. The user required that it be impossible to skip rustdoc in a phase, so a hook enforces it, and the record is keyed to the exact committed tree. An edit after the doc run, or a file left out of the commit, makes the record not match.
NEW: Rustdoc ran in every `verify.sh lint` call. It used much worker time and caught few errors. One run per phase, at the checkpoint, keeps the check and removes the repeats. The user required that it be impossible to skip rustdoc in a phase, so a hook enforces it, and the record is keyed to the exact committed tree. An edit after the doc run, or a file left out of the commit, makes the record not match.
```

`scripts/berth/install/README.md:11`:

```text
OLD: A unit's Codex seats edit through `apply_patch`, which none of these see.
NEW: A unit's Codex workers edit through `apply_patch`, which none of these see.
```

`scripts/berth/install/README.md:12`:

```text
OLD: `scripts/hooks/pre-tool-use-berth.py` is their `PreToolUse` check: for a seat
NEW: `scripts/hooks/pre-tool-use-berth.py` is their `PreToolUse` check: for a worker
```

`scripts/shutdown/inventory.py:888`:

```text
OLD:     """An earlier stop paused every seat, and the director has written no board line since.
NEW:     """An earlier stop paused every worker, and the director has written no board line since.
```

The `<seat>.resume-prompt.md` placeholder in `scripts/shutdown/inventory.py:890` remains a filename contract. Only the preceding worker-role sentence is eligible. The new direct.md line 863 is included in the refreshed owner request above; its nine uses remain eligible and temporarily excepted by exact OLD lines until phases-unit applies them. The unit director applied F006: both rustdoc replacements, both README replacements and the inventory docstring replacement above. The rustdoc uses no longer remain in the source result; no kept exception was added for them.

### Repair round 1 source result

The read-only source classification command shown above, run after repair with the original path masking, reads n=596 live files with n=0 issues. After the unit director applied F006, removing the two rustdoc uses from that result gives:

| Remaining measured live file | Singular eligible / kept (n) | Plural eligible / kept (n) |
| --- | --- | --- |
| `commands/unit/direct.md` | 9 / 0 | 0 / 0 |
| `docs/as-built/build-memory-admission.md` | 0 / 1 | 0 / 0 |
| `docs/as-built/message-clarity.md` | 0 / 0 | 0 / 1 |
| `docs/as-built/shutdown-and-restart.md` | 0 / 14 | 0 / 3 |
| `docs/as-built/usage-ledger.md` | 0 / 0 | 0 / 1 |
| `scripts/usage/collect.py` | 0 / 0 | 0 / 2 |
| Total, n=6 matching live files | 9 / 15 | 0 / 7 |

Singular n=24; plural n=7; combined n=31. On the same merged source set, the rename reduction is singular n=50 (74 to 24), plural n=23 (30 to 7), combined n=73 (104 to 31). Remaining eligible n=9 are all in direct.md. Kept n=22 remains unchanged. The writer repair and director F006 edits remove n=14 measured occurrences from the partially reapplied tree (45 to 31); that intermediate change is not added to the full rename reduction. Source comments/docstrings and the routed README receive no extra count credit.

F001 is fixed at `docs/direct/dual_review.md:38`. F003 is fixed by the director's before-baseline record above. F005 is fixed by re-matching all eight OLD lines to the merged read-only direct.md and sending their exact text to test for its kept contexts. The upstream changes within all six conflict files remain; every conflict marker in those files is removed. No source identifiers, stored fields, flags, transport addresses, filenames or historical records change.

Repair verification is deferred: hold status names build-speed-unit since 13:37 EDT, with release ETA 15:00 EDT. The assigned basedpyright, shell syntax and after-baseline commands were not started. No test or merge gate ran in this slot.

Repair follow-up: the extraction API now returns `SourceProse`; the reproducible merged-before method above uses `python_prose(text).text` / `shell_prose(text).text`. Names text and the classification method stay the same. The final eight direct.md OLD lines were sent again to test with exact leading spaces. R5b is recorded as answered in the term table and Questions from the unit director’s relay of the sdlc showrunner’s 2026-10-28 user decision; this adds no live rename.

### After measure — merged tree `8b3b83bd` with all repairs

Command: `python3 scripts/production/roles_audit.py --root . --since 2026-10-14 --until 2026-10-27 baseline`, run at 14:25 EDT on 2026-10-28 after the build hold ended, over n=596 live files, exit 0. The test slot ran it after the measure fix for unreadable Python tokens.

| Worker alias | Uses (n) | Files (n) |
| --- | --- | --- |
| `seat` | 15 | 2 |
| `seats` | 7 | 4 |
| `Codex seat` | 1 | 1 |
| `Codex seats` | 0 | 0 |

Singular n=15 and plural n=7 match the kept target of the opening freeze (n=22 combined). Every remaining use is a kept context with its reason in `role_terms.toml`; eligible uses remaining n=0, `commands/unit/direct.md` included. The before measure on the `2bd53a04` export read `seat` n=69 and `Codex seat` n=9. The `Codex seat` row overlaps the `seat` row and earns no extra count. The repository check `test_repository_sources_have_no_unkept_retired_names` passes on this tree.

### Retired-name check timing — 2026-10-28

The check now selects candidate files before full extraction, splits each Python file's source lines once, and reads and extracts the files of a full-tree scan in 8 processes (results keep the sorted source order). On natedev, on the full tree at merged HEAD `35fb3d20`, with the output byte-for-byte the same as before:

| Command | Before (wall, 1-minute load) | Candidate selection only (wall, 1-minute load) | With 8 processes (wall, 1-minute load; 3 runs) |
| --- | --- | --- | --- |
| `python3 scripts/production/roles_audit.py --root . check` | 39.8 s, 78.2 | 2.5 s, 24.0 | 1.28 s, 1.10 s, 1.16 s at 67–69 |
| `python3 -m unittest discover -s scripts/production -p 'test_roles_audit.py'` (31 tests) | not measured alone (about 38 s for the check inside it) | 4.1 s, 22.7; 8.4 s, 38–44 | 1.68 s, 1.50 s, 1.69 s at 67 |

Times depend on load. With candidate selection only, one suite run took 8.4 s at load 38–44, so the review asked for more margin. With 8 processes, runs at load 87–102 took 2.5–4.4 s for the check and 3.7–5.6 s for the suite. The merge gate cost is measured in real gates later.
