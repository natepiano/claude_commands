# Lasting designers, Astra reviewers, and their guilds

> **Status: DESIGN — round 3 and adversarial fixes.** This document proposes the roles and process. It authorizes no role change, rename, head launch, or implementation phase.

Source: [unit plan, Source and Decisions](sdlc-guilds.md), user instructions of 2026-10-20 16:19 and 16:47 EDT; [production plan, Guild heads and Production rules](sdlc-production.md). The unit director carries the questions to the sdlc showrunner. The designer does not ask the user directly.

## Lessons

Sources were read on 2026-10-20. Times below distinguish an event from the time its record was read. Absolute paths are external evidence, not files this unit owns.

| Evidence and time | Lesson | Effect on this design |
| --- | --- | --- |
| fps/frame-time-unit: `/tmp/claude/delegate/f6cf2c4f-858a-494f-bf69-6133ba872601/design/brief.md` and `designer_replies.md`, reply 1; event at 16:30 EDT as recorded in `docs/plans/sdlc-guilds.md`, What exists today | `fps-metrics-designer` drafted the design but could not save it. Berth said the file belonged to neither open phase. The reply reports no changed files. | A lasting thread does not grant write access. Bind each writing turn to an authorized phase and its exact output path before launch. Keep the thread after that phase releases its files. |
| experiments-unit: `~/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md`, Setup and (a), first draft 16:40–16:54 EDT | A run-local `agents_astra.conf` set `impl` and `fix` to `gpt-6-astra:xhigh`; `implement.sh --to cargo-liner-experiments-impl` resumes the writer. | Use that route for this design phase. The lasting-role launcher must later resolve its own named tasks from the shared registry. |
| Same experiments file, (a) and (c), 16:45 EDT | The user added item 13 after launch. The director steered the added tooling scope into the running turn. | Steer within existing output paths. A new path waits for turn completion, phase and binding updates, then same-thread resume. |
| Same experiments file, (a)–(d), four designer revisions 16:40–17:45 EDT; three reviews 17:01–17:36 EDT; read at 17:53 EDT | All 8, 6, then 5 findings were reported fixed, but findings did not converge. Each fix added mechanism and further gaps; the document grew from 568 to 696 lines. Final fixes had no further review. Setup used fresh reviewers, so it does not prove same-thread review. | Before the last review, cut or narrow mechanism. Give the reviewer the actual Key files list. Disagree, with evidence, when a finding adds a concept without a correctness gain; do not require a quota of disagreements. |
| Same experiments file, (c)–(d), rulings during rounds 2–3; final revision ended 17:45 EDT | The holder supplied one fact check, one scope permission and two rulings, including keeping both machines in scope. | Use a short ruling message saved with the round, not a new Work Order for every revision. Changed scope or file ownership still needs the holder's authorization. Check both machines and distinguish reported fixes from reviewed convergence. |
| `scripts/direct/review_pool.py`, `ProductionPool`, `_choose`, `_repoint`, and `_repoint_idle`; read at 17:03 EDT | The production pool stores threads outside run roots, leases them to units, and updates idle reviewers through `set-instructions`. It can fork another reviewer when the unit's reviewers are busy. | Reuse the durable-store and prompt-update patterns. The one-reviewer-per-design rule forbids the pool's automatic extra-reviewer behavior for Astra design review. |
| `commands/message.md`, Outcomes and recovery; `codex/AGENTS-messaging.md`, Outcomes and recovery; read at 17:03 EDT | A Codex worker between turns cannot receive a peer message. Its holder must resume it. | Guild membership must name the holder independently of a live worker address. Delivery to the holder is not yet delivery to the member. |
| `/home/natepiano/worktrees/claude-build-followups-hierarchies/docs/plans/build-followups-hierarchies-design.md`, Constraints C02–C04, C39–C43 and Map; baseline dated 16:55 EDT | Heads are peers, process changes are user decisions, judgment roles cannot be downgraded from speed or token measures, and pooled reviewers outlive runs. Live routing facts should be looked up. | A guild adds shared practice, not a second work manager. Store stable ownership and resolve the current holder session. Keep Astra threads across runs and account moves. |

Roles-unit's remote source is now available: `origin/sdlc-roles` at `b55c5781edf6096631ac72e831f8a22affca26b9`, read at 17:53 EDT. `docs/plans/sdlc-roles.md`, Decisions, and `docs/plans/sdlc-roles-design.md`, Duty matrix and Term table, name chief designer (D30), chief reviewer (D31), Astra designer (D51), Astra reviewer (D52), and pooled reviewer (D15). This design follows those names and holders. D32 leaves tips ownership unassigned; roles-unit Q1, Q3 and Q5 are already with the user, as the director reports. This document refers to those decisions and does not ask them again.

## Roles

### Duties and authority

| Role | Does | Does not do | Holder and lifetime |
| --- | --- | --- | --- |
| Astra designer | Reads the brief and sources; writes the assigned design; revises it in the same thread; answers each review finding with a change or a cited disagreement; records questions and tradeoffs. | Approve its own design, implement the feature, change another file, direct other members, or settle a user process choice. | One unit director or one showrunner holds it. The role and thread outlive a phase or run until the holder ends the design role. |
| Astra reviewer | Reads the design and its evidence; returns ordered findings with file or section citations; checks each response in the same thread. | Write the design, open another reviewer, replace the holder's verdict, or grant approval on the user's behalf. | The same holder as the designer. It has a separate read-only thread and the same durable lifetime rule. |
| Holder | Briefs and dispatches; assigns files; passes each review; rules on cited disagreements; records the acceptance verdict and user decisions. | Hand the designer's writing back to the user, count a missing review as passed, or change the user's one-of-each limit. | A unit director for a unit design; a showrunner for a production design. Exactly one holder is active per design. |

These D51–D52 duties follow roles-unit's map; D30–D31 belong to heads and D32 remains undecided. Recommendation Q1 permits both holder kinds: unit scope goes to its director, production scope to its showrunner. One pair has one holder; an approved transfer changes that holder while idle. A head cannot transfer it.

The current `commands/unit/direct.md` introduction says the unit director owns design. Proposed wording separates **design authorship**, assigned to Astra, from **design direction and acceptance**, held by the director. `commands/showrunner/produce.md`, DesignAuthority and DesignCheck, separately govern visual choices and screenshot checks. A design reviewer does not replace those checks.

Per design, keep exactly one designer and at most one Astra reviewer. The user's source rule requires at least two review rounds and saved rounds. This unit's Phase 2 adds a maximum of three. A round includes review, the designer's answers, and the holder's ruling. Here, unresolved findings at round three remain explicit and the phase does not pass; there is no automatic fourth round or second pair. This document does not extend the three-round ceiling to other designs. Their briefs must retain the user's minimum and specify any separately approved stopping rule. Source: `docs/plans/sdlc-guilds.md`, Source, Decisions, and Phase 2.

The pool's `code` and `ui` reviewers remain implementation and visual reviewers with their existing `review` edge and names. The Astra reviewer judges the design and its revisions. `scripts/direct/review_pool.py`, `ReviewerType` and `_choose`, currently only support `code` and `ui` and allow growth for concurrent work. `scripts/astra/astra_review.py` currently starts a read-only `codex exec` turn, with hardcoded model and effort; it has no resume argument. Phase 3 should let `/astra_review <document> <focus>` find and resume the unique held reviewer for that document. If there is no held design, retain a clearly identified one-turn review. If the held reviewer is busy or unavailable, report that state; never fall back to a new thread.

### Durable identity and model

Recommendation Q2: use a separate store for both Astra roles and share mesh primitives with the review pool. Proposed root on the host that runs the pair: `~/.local/state/guilds/roles/`, overridden by `GUILD_ROLES_STATE` for tests. A design has an immutable `design_id`, a UUID assigned once, and a directory `<design_id>/`. It holds `design.json`, a lock, one `mesh/` directory for the pair, and saved prompts and replies. The mesh roster is authoritative for thread IDs; the server record supplies the serving `codex_home`. Keep these files outside `/tmp/claude/unit/` and `/tmp/claude/delegate/`. Do not copy a live mesh roster into a new run.

Recommendation Q14: use **one ownership authority on natedev**, while a pair runs on its holder's host, natedev or the Mac. Both installations pin that authority in the proposed `config/guilds.conf`; an absent or conflicting setting refuses an open. Only natedev writes `~/.local/state/guilds/ownership/` (`GUILD_OWNERSHIP_STATE` in fixtures). Its short creation lock protects the index from `(repository_id, document_path)` to `DesignPlacement(design_id, host, generation)`. The repository ID is registered once at this authority, with explicit local checkout mappings on each machine. A URL spelling, worktree path, missing mapping, or unreachable host cannot create a new repository identity. The document path is normalized relative to that repository; moves retain an alias to the same design ID.

First open, adoption and holder transfer reach that authority by SSH or refuse. Require pair host = holder host at open, adoption and transfer; reject a mismatch before changing ownership. Reserve design ID, host and request ID under its lock before creation; the assigned host serializes creation under its design lock. Retries use the same placement; timeout never frees it. Unknown creation blocks another start. Authority loss prevents ownership changes, but existing roles can resume under local holder/turn checks. Account moves stay on that host; no host transfer or authority failover is supported. Retain ended identities; `open` cannot reuse them. Inventory both machines and reserve existing designs before enabling first opens; incomplete inventory or lost records require recovery. Transport patterns: `scripts/message/send.py`, `remote`; `scripts/production/broadcast.py`, `_remote_broadcast`. Internal tasks stay `impl` and `review` (`scripts/agents/codex_mesh.py`, `WORKER_ROLES`); Astra holds supply census roles.

`design.json` records schema version, the authority-issued placement, repository ID, document path, area name, role instance names, creation time, holder identity, phase binding, and lifecycle. A role instance ID is `<design_id>/designer` or `<design_id>/reviewer`; names can change without changing these IDs. Holder identity is `UnitHolder(production_slug, unit_name)` or `ShowrunnerHolder(production_slug)`. Resolve its current local Claude session when dispatching, displaying, or sending. Lookup sources: `scripts/lib/unit_runs.py`, `scripts/production/production_tree.py` (`marked_units`, `showrunner_sessions`), and `scripts/message/roster.py` with `ThisMachine()`. A missing or ambiguous holder prevents dispatch; it does not transfer ownership to a similarly named session.

Use named domain states: `DesignUnbound` and `DesignBoundToPhase(run_directory, phase, repository, output_paths)`; `RoleNotOpened`, `RoleOpen`, `RolePausedForMove`, `RoleNeedsRecovery`, and `RoleEnded`; `HeadNotStarted`. Decode optional external fields into these states at the boundary. Do not give domain code a set of nullable strings and ask it to infer what is legal. The Phase 1 wire record still keeps its agreed nullable fields; this design does not change that consumer contract.

Separate standing text from access policy. Proposed `ThreadAccessPolicy` has `PhaseBoundDesigner(binding_generation, launch_sandbox)` and `ReadOnlyReviewer` for these roles. The former retains the authorized writing sandbox and requires a valid berth binding; the latter always selects read-only access. A tips update changes neither policy. Persist the policy with the mesh role and enforce it on start, follow, reopen, account recovery, and adoption. Today `scripts/agents/codex_mesh.py`, `_seat_sandbox`, treats any `instructions_file` as a read-only reviewer. Phase 3 must remove that inference and preserve explicit policy through roster updates and `_resume_params`. Existing review-pool templates and forks must receive `ReadOnlyReviewer`; an ambiguous legacy record must be resolved before dispatch, never defaulted to write access. Instructions cannot grant a writer permission or remove its authorized write access. Apply the semantic-state rules in `~/.claude/docs/type_design.md` to these types and to all recovery records below.

Proposed `config/agents.conf` rows: `[guild.codex] designer=gpt-6-astra:xhigh`, `reviewer=gpt-6-astra:xhigh`, and assignment `guild=codex`. Resolve through `scripts/agents/agents_config.sh`, never hardcode or fall back. Invalid assignments or missing catalog rows refuse launch; `scripts/agents/sync_codex_catalog.sh` owns catalog updates. Verify the required model and effort before launch. Hierarchy constraint C39 forbids downgrades for speed or tokens.

### Lifecycle and recovery

Proposed entry point: `scripts/guilds/roles.py`, with `open`, `adopt`, `resume`, `rebind`, `end`, and `list --json`. `open` takes repository ID, document, role, holder, area, host and writing phase binding; it returns the existing instance unless `RoleEnded`. Then it refuses: "role ended; this design identity cannot be reopened." `adopt` also requires source mesh, worker name and exact thread. `resume` takes instance ID and prompt file. `rebind` changes holder or binding while idle, with a reason and generation check. Account moves use the existing workers-account lifecycle below; there is no separate guild pause command or Codex client.

| Event | Required transition and result |
| --- | --- |
| First open | Get the authority's placement; validate the brief, binding, model and account. Under that host's design lock, reserve the role slot and launcher claim, then release the lock before starting one named thread. Commit its identity only against the same claim and generation, then publish the Phase 1 hold. Membership derives from the role. |
| Another open while creating or running | Return the existing role or an in-progress result. Never fork to satisfy a duplicate request. A process ID alone cannot prove that a timed-out thread creation failed; recover the roster before retrying. |
| Revision | Verify the placement, existing thread, holder and binding. Use the proposed atomic instruction-and-turn operation below, then `codex_mesh.py follow --to` with that claim. Preserve the explicit access policy. |
| New scope during a turn | The holder sends or steers authorized scope within the binding's existing output paths. A new path waits for turn completion; update the phase's files and reservation, `rebind` while idle, then resume the same thread. Guild messages cannot authorize scope. |
| Phase or run ends | Finish the turn, clear the phase binding after the phase's normal checkpoint release, and retain both threads. Ordinary `end_session.sh` cleanup must not end the durable mesh. |
| Account move | Use `scripts/accounts/accounts.py`, `_swap_workers`, and the mesh's `retire_workers_servers`. A saved move marker with no live launcher enters `RolePausedForMove`; accepted `resume-account-move` returns it to `RoleOpen`, failure to `RoleNeedsRecovery`. Busy roles continue the interrupted assignment; idle roles move on their next assignment without a synthetic continuation. Refresh each Astra hold when its serving home changes. |
| Holder restarts or changes | Resolve the stable holder to its current session. An approved same-host transfer uses `rebind` while idle and fences dispatch under the old identity. Retire an old-environment server with its evidence and queued text preserved, using the same safe release rules below; the next assignment starts with the new holder environment. Retain thread IDs; update the census manager. |
| Server or launcher crashes | Keep durable roles. With a saved move marker and no live launcher, use `RolePausedForMove`; otherwise mark uncertain work `RoleNeedsRecovery`. Reconcile the recorded thread and turn before resuming. Do not count a crashed review as a round. The role launcher repairs missing Phase 1 holds on its next explicit lifecycle action. |
| End | The holder may end `RoleOpen`, `RolePausedForMove` or `RoleNeedsRecovery` when no recorded server has a live turn. End its mesh thread, release its Astra hold, and mark `RoleEnded`. Retain design evidence. Role end does not release an unfinished phase's berth reservation. Source: `codex_mesh.py`, `command_end`, marks the roster ended before server contact. |

Lock the design record for short state changes; use its generation and mesh claim to reject stale completions. Never hold a lock across an LLM turn. Hold and release are idempotent projections. Retain failed projection identity for recovery on the same instance; never open a duplicate.

**Account-switch sequence.** Extend `codex_mesh.py`, `retire_workers_servers`, to discover durable meshes and resolve their holders without run markers. After `_swap_workers` selects the validated target, retain the outgoing server record in the retired-server list, including its serving home. For a busy role, `_interrupt_worker_for_account_move` reads the live turn and writes its thread/turn/move marker **before** interruption; the attached launcher continues it, or `resume-account-move` recovers that marker after launcher loss. Confirm the same thread and the old writer's release before accepting a continuation on the new account. Preserve that assignment's instructions and access policy. A `starting` claim or unknown live state must be reconciled before interruption or another dispatch, not treated as idle.

For an idle role, keep its thread and pending text; no move marker or continuation is needed. Its next authorized `resume` releases the old writer and moves the same thread. Before any outgoing server is stopped, park its queued text with `_move_queue_to_pending` under the existing pending lock; `_release_other_worker_writers` supplies that pattern. Require this preservation in retirement cleanup too, not only in follow. Keep guild message IDs and receipts through that transfer; transport acceptance is not evidence that queued text survived. Failure retains the server evidence and pending text as `RoleNeedsRecovery`. Held roles must refuse raw `stop --pause`: today's `_pause_roster` truncates pending text and drops the server queue, while `command_stop` deletes server records without creating move markers. No second move journal is proposed.

**Instruction and turn transaction.** Extend `codex_mesh.py`, `_claim_follow`, to validate holder, idle state, expected thread, roster revision and design generation, then commit the immutable instruction pointer, access policy and `starting` claim under the roster lock. Only that claim submits. All held-role entry points enforce these checks; a losing follow submits nothing. Lock order is publication, design, pending, roster, server; park queued text before taking the server lock. No network under locks. Compare the accepted publication sequence at the claim. Later publications apply to the next assignment.

Held Astra roles install instructions only in that transaction; direct `set-instructions` refuses them. Keep the pool's existing setter route in `scripts/direct/review_pool.py`, `_repoint_idle`. Record `InstructionsInstalled(revision, prompt_hash)` after commit, not as evidence of consumption. A failed claim before submission restores idle state; uncertain submission requires recovery.

`command_resume_account_move` currently creates its own `starting` claim and continuation prompt; Phase 3 must carry the saved revision and access policy through that path. An unknown boundary is `RoleNeedsRecovery`.

Phase 3 installs roles without guild dispatch. Phase 4 derives membership from those records and enables dispatch after baseline publication.

Existing primitives are in `scripts/agents/codex_mesh.py`: `start`, `follow`, `reopen`, `resume-account-move`, and `end`. Its `_resume_worker_thread` can currently start a replacement thread after some resume failures. Phase 3 must add a strict same-thread mode for held Astra roles. A resume refusal becomes `RoleNeedsRecovery`. The holder brings an unrecoverable loss back through the showrunner; it cannot silently claim the replacement is the original designer. `scripts/accounts/accounts.py` supplies the current workers account. No account name is stored as a permanent routing choice.

### Adoption of an existing designer or reviewer

Recommendation Q16: adopt an idle thread explicitly. The authority reserves its source identity as `AdoptionReserved` before destination creation. `open` on an inventoried legacy design returns `AdoptionRequired(source)`; the holder may retain the old route but cannot open a replacement beside it. Lessons name both starting cases: `fps-metrics-designer` and `cargo-liner-experiments-impl`.

1. Verify source thread, mesh name, serving home, holder, model, effort, pending messages and saved evidence. Finish the active turn and establish a valid output reservation before the next writing turn. Record the same-host destination placement and an adoption ID. A busy or uncertain source defers adoption without changing its current assignment.
2. Fence the source as `TransferringRole(adoption_id, destination)` under its roster lock. Phase 3 must make `implement.sh --to`, mesh follow/reopen, account moves, `end_session.sh` and sweep honor it and reject stale launcher generations. Retain the source until transfer settles; unrelated workers continue.
3. Prove idleness and release the source writer using `codex_mesh.py`, `_release_worker_thread`; otherwise retain the fence as `AdoptionNeedsRecovery`. Create a destination entry for the **same** thread, mesh name, host and serving account, with saved instructions and explicit access policy. Never copy a live roster or use `thread/start` or `thread/fork`. Current `command_reopen` does not transfer entries or reopen finished runs.
4. Confirm destination identity and idleness, then commit `AdoptionComplete` and a permanent source redirect. Only the destination dispatches, ends or moves the thread. Discovery deduplicates by thread ID; old addresses return the destination. Cleanup skips the transferred entry and retains other workers. Preserve evidence references and pending message IDs without claiming delivery.
5. Repoint the same-thread Phase 1 hold atomically and record completion. Repair a failed hold at the destination. Before ownership commits, rollback requires proof of no destination writer; afterward recover only at the destination. Retrying the adoption ID cannot create another instance.

The new fences and redirects must survive source run deletion and be referenced by the authority and durable role record. A failure to make discovery and cleanup honor them blocks adoption. This design does not claim that today's mesh supports adoption without those changes. Sources for the present limits: `scripts/agents/codex_mesh.py`, `_claim_follow`, `_release_worker_thread`, `command_reopen`, `command_resume_account_move`; `scripts/direct/end_session.sh`; `scripts/agents/mesh_runs.py`, `_roster_folders`.

### File ownership and integration

The role lifetime and the file reservation lifetime are separate. Before each writing turn the holder prepares the authorized phase's `files` list, baseline, and `delegated_phase_reservation_state.json` through the existing reservation contract. The designer's output belongs to exactly one open phase. The reviewer writes no repository files; the launcher saves its reply as review evidence. Sources: `commands/unit/direct.md`, DelegatedPhaseReservationContract and CoordinateDelegatedPhaseReservation; `scripts/direct/phase_reservation.py`; `scripts/direct/implement.sh`, pre-dispatch claim.

A showrunner-held designer uses the same phase reservation contract for an authorized production design, with the showrunner as holder. It does not pretend to be a unit or acquire a second director. phases-unit must approve the binding of that design task to its phase machinery before Phase 3 is dispatched. Until this exists, a showrunner can hold a read-only role but cannot launch an unbound writing turn.

The hook must resolve the durable thread and generation to its current `DesignBoundToPhase`, refusing missing or stale bindings. Before attribution or reservation, validate **every edited path** against that binding's exact `output_paths`: normalize repository-relative paths, resolve symlinks, reject escapes and unlisted files, and check both paths of a rename. Reject the whole patch on any failure, even with one open phase or an unreadable berth ledger. Then apply the existing overlap and checkpoint rules to the bound phase. `scripts/direct/phase_reservation.py`, `attribute`, assigns every supplied path to a sole open phase; it cannot enforce this limit. `scripts/hooks/pre-tool-use-berth.py`, `decide`, needs the check before that call. Its run/active-marker fallback and `codex_mesh.py`, `_server_environment`, do not identify a durable role's phase; do not use them as a substitute or copy reservation state into the role store.

Durable meshes also need discovery in `scripts/message/roster.py`, `scripts/agents/mesh_runs.py`, and `scripts/agents/codex_mesh.py` account-move and server-cleanup readers. `mesh_runs.py`, `_roster_folders`, currently lists run roots and review-pool meshes only. Add the new root to the appropriate readers, with fixture overrides; do not put it under a run root just to make discovery work. Extend `scripts/direct/end_session.sh` to unbind held designs without ending their threads, and the unit-retirement route to end them explicitly. Phase 3's file list must include these integration changes and their focused tests before dispatch. The launcher alone cannot provide the promised lifetime.

The Phase 1 hold is a census projection, not the lasting role store or a guild membership. Preserve `~/.local/state/astra/roles/<thread_id>.json`, `ASTRA_ROLES_STATE`, and `production_tree.py --json` schema 1. `astra_roles.py live_roles()` can remove an ended hold; this must never delete the durable design. Keep one ownership edge per thread with `manager`, `member`, `role`, `codex_home`, and `state`. Guild membership adds no second manager edge. Sources: `docs/plans/sdlc-guilds.md`, Phase 1; `scripts/astra/astra_roles.py`; handler Phase 21 at `~/rust/cargo-liner-handler/docs/cargo-port/remove-running-section-handler-footer-colors.md`.

## Names

| Thing | Proposed rule | Example or distinction |
| --- | --- | --- |
| Unit | Keep the existing `<area>-unit`. | `guilds-unit`; source: `docs/production_format.md`, Words. |
| Designer instance | `<production>-<area>-astra-designer` | `sdlc-guilds-astra-designer` |
| Astra reviewer instance | `<production>-<area>-astra-reviewer` | `sdlc-guilds-astra-reviewer` |
| Guild | `<discipline>-guild` | `design-guild`, `review-guild` |
| Chief designer; chief reviewer | User-chosen goddess names, settled through cargo-liner. | D30–D31; temporary head IDs `astra-designer` and `astra-reviewer`. No goddess name is selected here. |

The area names a design, not a run number or phase. Revisions retain the name. A separate concurrent design gets a distinct area in its brief. An existing hand-run worker keeps its current mesh name through the explicit `adopt` operation in Roles; the durable role may have a new display name only after the naming decision. An approved owner or area rename updates display names and stable-holder lookup together; it does not create another thread.

There are two uses of `astra-designer` and `astra-reviewer`: **Codex edge role IDs** for D51–D52, and **temporary head IDs** for D30–D31. A head is not an Astra worker edge. Keep `role`, `guild_id`, and `head` separate. Use **pooled reviewer**, **Astra reviewer**, **chief designer**, and **chief reviewer** in shared prose, following roles-unit Q3's recommendation. Handler Phase 21 keeps the wire role `review` for the pool and the Astra display words `astra designer` and `astra reviewer`; display spelling does not change the prose duty owner.

Under roles-unit Q5's recommended separation, `docs/plans/sdlc-production.md`, Close-out, replaces **head IDs and labels** only. Keep worker wire IDs and their Phase 1 color keys; a different user answer requires coordination with cargo-liner before any wire change. Q8 concerns naming time only.

## Guilds

### Membership and heads

Derive local member maps, keyed by role instance ID, from the durable role records in Roles: each successfully opened, not-ended designer belongs to `design-guild`; each such Astra reviewer belongs to `review-guild`. Q4 recommends this scope. No separate member file, join/leave command, enrollment intent or repair state is needed. Idle time, run completion and account moves retain the role and thus membership; explicit role end removes it. Unit retirement ends its roles after active turns finish. Unreadable records produce a census problem, never a guessed end or a duplicate on another host.

Proposed `scripts/guilds/guilds.py` provides `list --json` and `publish-instructions`. Read thread, account, name, holder and activity from role and mesh records; read desired instructions from accepted publications. This version reports `HeadNotStarted`; it adds no head file or head-discovery mechanism. Heads are separate from member maps and the per-design reviewer limit.

Recommendation Q9: start heads after the approved role and guild mechanisms pass their gates; Q8 separates naming time. Each head is a top-level showrunner and peer of sdlc. This unit neither starts nor runs them. Until then, holders retain tips with design evidence and documents stay at the initial approved revision. Use `HeadNotStarted`; initial publication is the director's bounded rollout task below.

Roles-unit Q1 already proposes that each head maintain and distribute its guild's tips (D32); this design recommends that answer without assigning it in advance. Q10 then selects one owner for `common.md`, consulting the other head. If roles-unit Q1 instead assigns both guilds to sdlc, sdlc owns all three documents and Q10 is unnecessary. Heads assess advice and report recurring defects; they do not redirect product work, accept designs, change models, or approve process rules. Findings return to guilds-unit through sdlc; process decisions go to the user. Sources: `docs/plans/sdlc-production.md`, Guild heads and Production rules; hierarchy design C02–C04.

### Shared documents and tips

Proposed documents are `docs/guilds/common.md` for shared practice, `design.md` for design, and `review.md` for design review. A tip states the problem, example, source file and time, result, and limits. The member sends it through existing messaging to the owner selected by roles-unit Q1, naming its holder. If that owner has not started, the holder retains it. Advice takes effect only after the owner's document change lands through normal review.

Recommendation Q15: publish landed tips to both machines, including empty guilds. The authority stores publications at `~/.local/state/guilds/publications/` (`GUILD_PUBLICATIONS_STATE` in fixtures). The document owner's `publish-instructions` request identifies the landed commit and changed document. A common change targets both guilds on both hosts; a guild-only change targets that guild on both hosts. Freeze targets before transfer; later members inherit the accepted revision.

**Initial publication:** an earlier document-only checkpoint lands the three approved baseline documents, as Steps specifies. Phase 4's guilds-unit director publishes that existing commit through ordinary `publish-instructions`, targeting both guilds on both machines. Only after all targets acknowledge does guild dispatch enable and the Phase 4 live gate run; its implementation checkpoint follows that gate. Both heads may remain `HeadNotStarted`. This bounded rollout duty grants no continuing editorial authority. Partial publication uses ordinary retry, with dispatch disabled.

Each immutable manifest contains a publication ID, increasing sequence, source commit, affected guild IDs, target host IDs, and the SHA-256 hash and byte length of each common and guild file. A guild revision hashes a canonical manifest of the common and guild blob hashes, not an ambiguous concatenation. For a common change, both guild revisions use the same common blob and the guild texts at the selected landed commit. Compare competing head requests with the authority's last source revision and reject an unintended reversion. Keep only one incomplete publication: do not issue a newer one until all targets of the earlier one acknowledge. This delays later publication during a host outage but avoids ambiguous superseded receipts. Transfer the manifest and exact UTF-8 file bytes with a structured remote command, using the SSH pattern in `scripts/message/send.py`, `remote`. Do not fetch whatever happens to be on the destination's branch or infer delivery from a broadcast notice.

On each host, verify lengths and hashes, save immutable blobs, then atomically replace one `accepted_publications.json` map from guild ID to manifest revision. A common change updates both entries together; census reads the map once. The receiver acknowledges `(publication_id, host, affected guild revisions)` only after that durable commit. Retry a lost acknowledgement with the same ID. Older sequences cannot overwrite newer ones; hash conflicts retain the prior map and report an error. Tests use isolated roots and fake transport.

Use `PublicationPending(target_receipts)` and `PublicationComplete(target_receipts)`; targets are `TargetPending(reason)` or `TargetAccepted(revisions)`. The publisher reports partial transfer with exit 1 and pending targets; all-target success requires every receipt. Neither means members installed or read the text. Retain incomplete publications and retry explicitly with `publish-instructions --retry <publication_id>`. To keep version 1 small, resume performs no publication fetch and census exposes only local acceptance. Global receipts stay at the authority and in the publish result; do not replicate receipt caches. Members use the verified local baseline and subsequent locally accepted revisions, without claiming global freshness.

Recommendation Q11 applies locally accepted instructions before the next **new assignment**, without forced turns. The role launcher composes fixed duties, the current task contract, and the exact verified common and guild blobs. It records both the guild revision and the composed-prompt hash. Guild text must not replace file limits, user rules, the Type Design Contract, or Three Gods. The holder resolves a detected conflict before dispatch. Once a publication is accepted on the host, a new assignment cannot bypass that pending revision; an unfinished assignment keeps its prior instructions through recovery as Roles specifies.

The atomic instruction-and-turn claim in Roles installs the revision at dispatch. Until then, census reports it pending. Retain immutable prompts while threads or recovery records refer to them. This adapts the pool's prompt-update pattern without its idle setter: `scripts/direct/review_pool.py`, `_repoint_idle`; mesh changes: `_seat_sandbox`, `_claim_follow`, and `command_set_instructions`.

### Messages and members between turns

Proposed syntax is `broadcast.py --from <sender> --guild design-guild <text>` or `--guild review-guild <text>`. Add `GuildMembers(guild_id)` beside `WorkersOfUnit` in `scripts/production/broadcast.py`; update parsing, selection, `ending`, `_group_name`, and `_remote_arguments` together. Preserve sender exclusion, last-selected-text behavior, and `--here` on both machines. Use the same role-derived member map as census, never name suffixes.

Recommendation Q12 uses one durable pending record per member under its role directory for **both active and idle routes**. Save message ID, guild, member, source and body before transport. With a live address, send through `scripts/message/send.py`; otherwise notify the holder. A refused active send uses the holder route with the same ID. Notices may combine members but never combine their receipts. The holder alone resumes an idle member; an active launcher carries queued IDs into its next turn submission.

Use `PendingTurnAcceptance` until `DeliveredToMember(thread_id, turn_id)` is confirmed. A successful send reports **queued**, not delivered: `scripts/message/send.py`, `codex`, reports success when `codex_mesh.py`, `command_send`, only saves pending text. Both routes associate IDs with submitted input and mark delivery after mesh turn acceptance (`_start_turn`, including `_move_resident`). A holder notice is routing evidence only. Uncertain acceptance retains the record for recovery; repeated IDs remain recognizable. This proves submission, not reading or action. Guild text grants no authority; the holder controls assignments, as `commands/message.md` and `codex/AGENTS-messaging.md` require.

Guild broadcast reports queued messages, holder notices and confirmed turn acceptances separately; generic groups keep their current behavior. Missing holders and send failures retain pending records with reasons. Role end reports undelivered records to the holder and retains them. No automatic wake, replacement thread, user push, timer or polling loop.

### Simple text data for cargo-handler

Proposed single census command: `python3 scripts/guilds/guilds.py list --json`. It reads local files only, never prompts, performs no network call, and does not mutate records. Resolve holders with `scripts/message/roster.py` using `ThisMachine()`, never its CLI's `AllMachines()` default. Run once per machine, as with `scripts/production/production_tree.py --json`. Return readable records even if another fails; emit each problem on stderr and in JSON `problems`. Exit 1 means partial data, 0 complete data, and 2 invalid arguments. An empty valid guild remains present with an empty members list.

| JSON field | Meaning |
| --- | --- |
| `schema`, `machine`, `observed_at`, `problems` | Version 1; source machine; UTC read time; structured `{path, reason}` failures. |
| `guilds[].guild_id`, `display_name` | Stable guild ID and user-facing guild name. |
| `guilds[].head` | `{kind: "not_started"}`; head launch and discovery are outside this version. |
| `guilds[].documents` | Common and guild paths and blob hashes; locally accepted publication ID, sequence and desired guild revision. |
| `members[].instance_id`, `design_id`, `name`, `role` | Stable instance and design IDs; display name; `astra-designer` or `astra-reviewer`. Members are nested under their guild. |
| `members[].thread_id`, `codex_home`, `machine` | Current recorded thread and serving home for census lookup; host identity. Omit an unreadable member and identify it in `problems`; do not emit a guessed ID. |
| `members[].holder` | Tagged unit or showrunner identity and local reach: `available` with session address, or `unavailable` with reason. Missing reach does not remove membership. |
| `members[].activity` | `busy` or `idle` agrees with the Phase 1 hold when available; `paused` derives from `RolePausedForMove`, `needs_recovery` from `RoleNeedsRecovery`, each with reason. |
| `members[].instructions` | Desired accepted revision and last dispatch's installed revision; `current` or `pending` compares them, without claiming the member read the text. |
| `members[].pending_messages` | Number of undelivered member messages. |

Example rendering: `design-guild · head not started`, then `sdlc-guilds-astra-designer · idle · holder guilds-unit · tips pending`. Membership is a second view of the same thread, not another hierarchy parent. A paused member can remain in the guild view while its Phase 1 hold is absent. The handler must retain partial-data warnings and join members by machine and thread ID, not display name. Keep the command comfortably below one second with fixture scale tests and no per-member subprocess; this is a proposed gate, not a measured result.

Every change to this section goes from the unit director to cargo-liner, whose address is resolved with `showrunners.py address remove-running-section`. The director also sends the document through sdlc to hierarchies-unit and roles-unit. The writer communicates through its board only. Source: `docs/plans/sdlc-production.md`, Production rules; current phase assignment.

## Command changes

These are change requests after user decisions, not authority to edit hub files. Owners: `docs/plans/sdlc-production.md`, Hub files. Map references below use `origin/sdlc-phases` at `e66bfe6036bbc75829a192a356f87263949e8644`, `docs/plans/sdlc-phases-design.md`, Map: 22 numbered phase rows and seven document rows named by kind. phases-unit owns every direct.md request, including its pointed contracts.

| File and section | Proposed statement |
| --- | --- |
| `commands/unit/direct.md`, introduction — request to phases-unit; Map 6, 9, 11; document row Design doc | Assign authorship to Astra; the director briefs, routes reviews, rules and accepts. |
| Same file, LongLivedWorkers — request to phases-unit; Map 6, 12, 18, 22 | Keep one pair per design across phases and runs; never replace it because busy or unavailable. |
| Same file, CoordinateDelegatedPhaseReservation and DelegatedPhaseReservationContract — request to phases-unit; Map 4 | Bind each writing turn to exact phase outputs; new paths require idle rebind. |
| Same file, PassOwnership and LaunchImplementation pointer — request to phases-unit; Map 5–7 | Record the normal baseline and pass; resume the registry-configured designer with its binding and access policy; no tester for this design workflow. |
| Same file, EarlyReviewArm and DualReview pointer — request to phases-unit; Map 8–9; document row Review output | Use the same sole Astra reviewer and save each round; do not open extra lenses for this design workflow. |
| Same file, Synthesize and FixDispatch — request to phases-unit; Map 11–12 | The director rules briefly from cited files and resumes the same designer; update the Work Order when scope changes. |
| Same file, PhaseCleanup, FinalGate and RunSummary — request to phases-unit; Map 14, 18, 22 | Clear binding after checkpoint release; retain the durable pair at run end until explicit role end. |
| `commands/showrunner/produce.md`, DesignAuthority and DesignCheck; owner roles-unit | The showrunner may hold an Astra pair for a production design under the same file and review rules; it keeps visual authority, screenshot checks, and the user's process decisions. |
| `docs/production_format.md`, Words and ProductionUnit; owner roles-unit | Define the Astra roles, chief designer, chief reviewer, holder, and guild against D30–D32/D51–D52; use the answers to roles-unit Q1/Q3/Q5 and keep the pool's `review` wire role. |
| `commands/message.md`, Finding sessions and Outcomes and recovery | Add the guild flag and the exact rules for pending member delivery through a holder, preserving refusal and receipt meanings. |
| `codex/AGENTS-messaging.md`, Send groups and Outcomes and recovery | Insert the same guild flag and holder-delivery text word for word as in `commands/message.md`. |

Also amend `commands/astra_review.md`, Usage and Steps (Map document row Review output), for held-thread selection, busy refusal and the one-turn case. Guild announcements use only `broadcast.py --guild`.

- Map 6 departure: this design workflow dispatches one designer and no tester, because the user's rule allows one designer and one reviewer.
- Map 8–9 departure: design reviews use the sole lasting Astra reviewer across rounds, not additional early or dual reviewers; implementation review remains unchanged.
- Map 12 departure: design revisions return to the same designer with a brief ruling; scope changes still update the Work Order.
- Map 22 departure: durable Astra roles survive run closeout, unlike run-scoped workers; Map 18 already retains threads at phase end.
- Map document row Design doc refinement: its generic task author becomes the held Astra designer; Review output retains saved findings and holder rulings. Map 10/13 visual checks and Map 14 checkpoint ordering are unchanged.

## Choices

These are proposed answers, not approved changes. The specific process-redesign rule in `docs/plans/sdlc-production.md` requires the user questions despite the general reversible-choice rule in `~/.claude/docs/decision_criteria.md`.

| ID | Buildable answers and cost | Recommendation |
| --- | --- | --- |
| Q1 — holder | Unit only: production designs need a unit. Showrunner only: unit revisions depend on the showrunner. Both, one holder per design: requires stable holder resolution and transfer rules. | Both; put the pair with the owner of the design's scope. |
| Q2 — reviewer store | Add a design type to the review pool: reuse storage but change unit leases, showrunner ownership, and automatic growth. Separate Astra store: add discovery and lifecycle integration while leaving pool semantics intact. | Separate store for both Astra roles; reuse mesh and prompt primitives. |
| Q4 — membership | Held Astra designers and reviewers only: narrow initial scope; pool lessons need a holder to pass them on. Include pool `code`/`ui` reviewers: needs membership for leased reviewers and separate rules for their instructions and lifecycle. | Held Astra roles first; add pool membership only after a later approved design. |
| Q5 — instance names | `<production>-<area>-astra-designer` and `-astra-reviewer`: longer but distinguish productions. `<area>-astra-designer` and `-astra-reviewer`: shorter but can collide across productions. | Include production and area; preserve adopted mesh names as aliases. |
| Q6 — guild names | `design-guild` and `review-guild`: concise discipline names. `astra-design-guild` and `astra-review-guild`: explicit current model family but harder to extend if membership later grows. | `design-guild` and `review-guild`. |
| Q7 — reference only | Roles-unit Q5 already asks whether head names and per-design IDs are separate. Sharing names saves labels but confuses duties and could require a handler migration. | Use that answer; recommend separate head names and unchanged wire IDs and colors. Do not ask again. |
| Q8 — head naming time | Ask cargo-liner to settle goddess names with the user now: a decision before the mechanism is ready. Settle names after the gates: delays that decision but keeps placeholders longer. | Settle names now through cargo-liner; choose no goddess in this document. |
| Q9 — head launch | Start now: heads manage members by hand before the mechanism exists. Start after Phases 3–4 pass: holders retain tips until then. | Start after Phases 3–4; this unit does not launch the heads. |
| Q10 — common-document owner | If roles-unit Q1 assigns tips to heads: chief designer owns common text, consulting chief reviewer, or vice versa. Both give one author; emphasis differs. If Q1 selects sdlc, no further owner choice is needed. | Chief designer, consulting chief reviewer, conditional on heads owning tips. |
| Q11 — instruction timing | Apply accepted tips before the next new assignment: tracks desired/applied revisions without extra turns. Resume idle members immediately and interrupt busy members at a safe point: faster uptake but extra turns and disrupted assignments. | Next new assignment; interrupted assignment recovery retains its prior instructions. |
| Q12 — member delivery | Pending records for both routes, with idle holder notices: receipt work, but retains delivery intent. Direct-only send: less work, but no idle delivery or confirmed turn acceptance. | One pending record and turn-acceptance rule for active and idle members; no automatic wake. |
| Q14 — machine ownership | One natedev registry places each pair on its holder's host: preserves Mac execution, but ownership changes need natedev. Keep holders and pairs on natedev: simpler placement, but no Mac execution and all turns depend on natedev. | Single natedev registry; pair and holder share a fixed host, with no failover or host transfer. |
| Q15 — publication scope | Default to both machines with exact-content transfer and acknowledgements: extra transport and retry state; an unavailable host delays later publications. Require separate manual publication on each host: less automation, but the head must track and finish both targets by hand. | Publish to both; report partial success until every intended host acknowledges. |
| Q16 — existing threads | Adopt idle threads with ownership transfer and source fences: implementation and recovery work, but retains history outside run cleanup. Leave them on current launchers until each design ends: less migration work, but lasting lifecycle benefits are deferred; keep their slots reserved against duplicates. | Adopt the same thread when idle; no fresh replacement. |

The one-of-each limit, same-thread revision, model and effort, and peer heads are source constraints. Roles-unit Q1/Q3/Q5 are dependencies, not approvals; local Q7 refers to its Q5. Local Q3 and Q13 are removed with the head file and wrapper. The three-round ceiling stays local to this unit. Command text follows the answers without another bundled approval.

## Steps

Map references use the source in Command changes. Each checkpoint follows its own review and checks (Map 7–13), then commit/release and notice (14–15); cleanup, plan updates and next-phase selection follow 16–21. The director updates pending Work Orders before dispatch (Map 1–4; document rows Phased plan and Unit plan).

### Phase 3 — Lasting roles

Waits on Q1, Q2, Q5, Q14, Q16 and roles-unit Q3/Q5 (Map 1–3). Implement the role store, registry, authority, held-review route, lifecycle, access policy, atomic claim, binding, adoption and holds (Map 4–7). Add `config/guilds.conf`, mesh, account, hook, roster, detached-result, cleanup, retirement, transport and `commands/astra_review.md` integrations to the Work Order, with tests and the `scripts/guilds` pyright entry. Coordinate the reservation bridge with phases-unit and hub changes with roles-unit; preserve Phase 1 wire IDs.

Checks (Map 7–13): prove one pair across simultaneous two-host opens, lost acknowledgement, aliases and unreachable authority; reject holder/pair host mismatch at open or transfer and `open` after end. Preserve threads across revisions/run end, holder restart and incomplete holds. Exercise busy and idle moves **without an existing marker**, preserving queued text: mark before interrupt, release the old writer, continue interrupted work, move idle work on its next assignment. Cover paused-state entry/recovery, racing claims and failure before marker/acceptance; raw `stop --pause` refuses held roles.

Check bound edits and refusal of unbound, stale or unlisted paths, including a sole open phase, mixed patches, symlink escape and rename destination; refusal acquires no reservation and writes nothing. Prove new-path steering cannot widen an active binding; idle rebind permits the same thread's next turn. Race follows, publication and pending sends in the stated lock order; reject held-role setters, stale generations and uncertain submissions. Preserve access and interrupted prompts; rejected claims start no turn. Check end from `RoleNeedsRecovery`: a lost-thread fixture must leave the roster ended even if server cleanup returns 1.

Adoption fixtures cover both known launches, concurrent legacy follow, cleanup during transfer, release failure, crash boundaries and retries: same thread/name, one owner/hold, preserved messages/evidence, all old dispatches refused. Tests use isolated fixtures. Director live gate (Map 10): adopt an idle designer, finish its old run, resume the same thread through the new role and show one Astra edge.

Undo: disable new dispatch/adoption, finish active turns, and retain placements, transfer fences, durable stores and evidence. Use the safe server-release rules above, never raw `stop --pause`. For an adopted role, retain destination ownership or reverse transfer only after proving it has no writer. Restore old command text only when it cannot bypass fences. Preserve unfinished reservations; never replace a thread silently.

### Phase 4 — Guild membership, shared text, and delivery

Waits on Q4, Q6, Q9, Q10 when applicable, Q11, Q12, Q15, roles-unit Q1, and Phase 3 (Map 1–3). **First, insert a document-only predecessor checkpoint** for approved `docs/guilds/common.md`, `design.md` and `review.md`. Give it ordinary authorization, reservation, review and document checks (Map 1–13), then checkpoint and land it (14–15 and the production merge gate). Its acceptance requires no guild publication or live member. This splits the unit plan's Phase 4 files before dispatch; no exception to `docs/delegate/phase_end.md` is proposed.

Then implement derived membership, publication records, census, broadcast, receipts and dispatch integration (Map 4–7). Remove join/leave requirements from the unit plan's Phase 3–4 Work Orders; include needed roster/transport files. Change `scripts/production/codex_winddown.py` only if its broadcast interface changes. The director publishes the predecessor's existing commit, confirms both guilds on both hosts, then enables guild dispatch and runs the live gate (Map 10). Checkpoint Phase 4 implementation only after that gate (Map 14). Send cargo-liner the schema/sample before consumer work.

Checks (Map 7–13): no-manifest baseline with absent heads, partial transfer blocking dispatch, ordinary retry enabling first resume; both holders/hosts, overlapping groups, missing holders, unreadable roles and end/move recovery. Active and idle messages remain pending until accepted input; test launcher death after queued send, refused-send fallback and uncertain acceptance. Derive empty/new/ended membership without join/leave. Common publication changes both guilds; guild-only leaves the other unchanged. Test unavailable Mac, lost receipt, wrong hash, stale/competing publication and crash before local commit. Prompts use accepted bytes; partial transfer never reports completion. Measure local census without network. Director live gate: send to an idle designer through its holder and confirm acceptance on its next turn.

Undo: before publication, revert baseline documents through an ordinary reviewed checkpoint. After publication, restore earlier approved tips only with a higher-sequence publication. Disable new publication/group selection but retain receipts, pending messages, manifests and prompts; keep ordinary roles usable. Never report pending work delivered. Head launch remains separate after the gates; Q8 names heads, Q9 starts them.

### Phase 5 — Published contracts

Waits on Q5/Q6/Q8, roles-unit Q1/Q3/Q5, and Phase 3–4 decisions/gates (Map 1–3). Submit direct.md requests to phases-unit at the rows in Command changes; coordinate the other four contracts with their owners (Map 4–7). Use implemented verbs and paths, identical guild text in both messaging documents, and no announcement wrapper. Trial-merge hubs and send final contracts to cargo-liner.

Verify the merge gate, documented verbs and hub trial merges (Map 7–13). Undo restores prior command text while preserving recovery records and ownership fences. At run closeout (Map 22; document row As-built), the director converts this design to `docs/as-built/guilds.md`.

### Phase 2 evidence still required

This is not a phase pass. Reviews are saved at `/tmp/claude/unit/c9b1f556-60d5-466f-aa72-00e2db13d96f/design/round<N>_review.md`, N=1–3; `adversarial_report.md` records five defects and eight safe cuts, applied here. Round 3's checkpoint, steering and receipt fixes remain. The director saves the final document, reply and ruling; the final merge gate follows (Map document row Review output; phase rows 9–14). No further review round or tester is authorized. Record reviewer-thread continuity: today's `scripts/astra/astra_review.py` starts fresh threads, so its invocations alone do not prove the user's one-reviewer rule.

Experiments (a)–(d) and the fps folder were read again for this revision: no new lesson or saved fps review round was present. Lessons retain their sources and times. The phases-unit Map is now cited in Command changes; roles-unit Q1/Q3/Q5 remain pending. Report gate evidence separately from proposed implementation guarantees.

## Questions for the user

Already with you through roles-unit: Q1 (tips ownership), Q3 (reviewer terms), and Q5 (head names versus per-design IDs); use those answers without asking again. Local numbers stay stable: Q7 is a reference; Q3 and Q13 were removed with the head file and wrapper.

Q1. Should lasting designers and Astra reviewers be held by unit directors, showrunners, or both? Recommended: both, with one holder per design; unit scope stays with the director and production scope with the showrunner.

Q2. Should the lasting Astra reviewer use the existing review pool or a separate store shared with the designer? Recommended: a separate Astra store that reuses the mesh lifecycle.

Q4. Should the review guild begin with held Astra reviewers only or include the code and UI pool reviewers? Recommended: held Astra reviewers only for the first version.

Q5. Should new role instances use `<production>-<area>-astra-designer` and `<production>-<area>-astra-reviewer`, or omit the production prefix? Recommended: include the production prefix.

Q6. Should the guilds be named `design-guild` and `review-guild`, or `astra-design-guild` and `astra-review-guild`? Recommended: `design-guild` and `review-guild`.

Q7. Reference only: roles-unit Q5 already covers separate head names and per-design IDs; use its answer here, with unchanged worker wire IDs and color keys recommended.

Q8. Should cargo-liner settle goddess names for the chief designer and chief reviewer with you now or after the role and guild mechanisms pass their gates? Recommended: settle the names now.

Q9. Should the two guild heads start now or after the lasting-role and guild-membership mechanisms pass their gates? Recommended: start after those gates, in Phases 3 and 4.

Q10. If roles-unit Q1 assigns tips to the heads, should the chief designer or chief reviewer own their common document, consulting the other? Recommended: chief designer; omit this choice if Q1 assigns all tips to sdlc.

Q11. Should accepted tips reach members at their next new assignment, or trigger extra turns and interrupt active work for earlier uptake? Recommended: the next new assignment; preserve the prior prompt while an interrupted assignment recovers.

Q12. Should guild messages retain pending records until turn acceptance, using holders for idle members, or use direct-only send with sender retries? Recommended: pending records for both active and idle routes, with holder delivery when needed.

Q14. Should one natedev registry place each pair on its holder's host, or should holders and pairs all run on natedev? Recommended: one natedev registry; each pair and holder share one fixed host.

Q15. Should each accepted tips revision be sent to both machines with delivery receipts, or require a separate manual publication on each machine? Recommended: send to both and report incomplete delivery until both acknowledge.

Q16. Should existing designers be adopted into the lasting store on the same thread when idle, or stay on their current launchers until their designs end? Recommended: adopt them with an explicit ownership transfer; never open a second designer beside them.
