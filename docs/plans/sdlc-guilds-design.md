# Lasting Astra roles and the guild process

> **Status: Phase 3 design revision.** The user's G1–G10 decisions of 2026-10-21 replace the former guild design. The lasting-role decisions stand. This document specifies later work; it does not start heads or authorize an implementation phase.

Source: [unit plan](sdlc-guilds.md), Source, Decisions, and Phase 3. G1–G10 and former Q1, Q2, Q5, Q8, Q12, Q14 and Q16 are settled. The director passes this revision to the sdlc showrunner. New process choices still go to the user.

Code citations were checked on 2026-10-21 in this worktree. Cross-unit sources were fetched first: roles-unit `5ebb4dc26b4f1d6d4aefef770365e2f44fc82370`, `docs/plans/sdlc-roles-design.md`, Duty matrix and Term table; phases-unit `fd12c4efda1fe1d8126f9a3f3d3a8c0e6cfec3f7`, `docs/plans/sdlc-phases-design.md`, Map. New paths, types and behavior below are implementation requirements, not claims that they exist.

## Lessons

| Source and time | Lesson and rule retained |
| --- | --- |
| fps/frame-time-unit: `/tmp/claude/delegate/f6cf2c4f-858a-494f-bf69-6133ba872601/design/brief.md` and `designer_replies.md`, reply 1; 2026-10-20 16:30 EDT in the unit plan's account | Berth refused a designer outside a phase reservation. A lasting thread gives no write access; see Roles, file ownership. |
| experiments-unit: `~/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md`, Setup and (a), 2026-10-20 16:40–16:54 EDT | Its run-local config set `impl` and `fix` to `gpt-6-astra:xhigh`; `implement.sh --to` resumed the writer. |
| Same notes, (a), (c), 16:45 EDT | New scope reached a running turn. Roles limits steering to reserved output paths. |
| Same notes, (a)–(d), four revisions 16:40–17:45 EDT; three reviews 17:01–17:36 EDT | Fixes for 8, 6 and 5 findings did not converge. The doc grew from 568 to 696 lines. Cut mechanism without a correctness gain before the last review. Give the reviewer the Key files list. Use short saved rulings; changed scope needs an updated Work Order. |
| `scripts/direct/review_pool.py:257` (`_production`), `:440` (`_repoint`), `:465` (`_repoint_idle`), `:499` (`_choose`); read 2026-10-21 | The pool keeps threads outside run roots and updates standing instructions. It can add a reviewer when existing ones are busy. Reuse storage and mesh patterns; the one-reviewer rule forbids that growth for a design. |
| `commands/message.md:35`, Outcomes and recovery; read 2026-10-21 | A Codex worker between turns takes no peer message. Its holder resumes it. |
| `~/worktrees/claude-build-followups-hierarchies/docs/plans/build-followups-hierarchies-design.md`, C39–C43, Steps item 2 at line 498, Q8 at line 573; read 2026-10-21 | Keep judgment roles across phases and account moves without model downgrades. Its proposed wait and merge records feed G10's daily read. |

## Roles

| Role | Does | Does not do |
| --- | --- | --- |
| Astra designer | Writes the assigned design and revises it in the same thread. Answers each finding with a change or a cited disagreement. | Accept its design, implement the feature, edit unassigned files, direct members, or settle a user process choice. |
| Astra reviewer | Reviews the design and evidence; returns findings; checks responses in the same read-only thread. | Write the design, add reviewers, replace the holder's ruling, or approve for the user. |
| Holder | Briefs, reserves files, dispatches, passes reviews, rules from cited files, and records acceptance and user decisions. | Give authorship back to the user or count a missing review as passed. |

Settled Q1: either a unit director or a showrunner holds the pair, with exactly one holder per design. Unit scope goes to its director; production scope to its showrunner. Both roles have that holder. A head cannot take ownership. This follows roles-unit D51–D52 (`:86–87`). `commands/unit/direct.md:7` currently assigns design to the director; the change separates Astra authorship from direction and acceptance. Showrunner visual authority and screenshot checks remain (`commands/showrunner/produce.md:671`, DesignAuthority; `:698`, DesignCheck).

Keep one designer thread. Once Phase 6 builds held reviews, keep at most one reviewer thread per design across rounds. Phase 3 uses today's fresh `codex exec` per review, as the unit plan's Phase 3 Process requires. This unit requires two or three rounds per design phase. For Phase 3, save each review, revision and holder ruling under `$SESSION_DIR/design/phase3/`. An unresolved defect at the ceiling prevents a pass.

The pool's `code` and `ui` reviewers keep their implementation and visual duties and `review` edge (`scripts/direct/review_pool.py:23`, `ReviewerType`; `:499`, `_choose`). Today's `/astra_review` builds one read-only `codex exec` command with fixed model and effort (`scripts/astra/astra_review.py:26`, constants; `:76`, `codex_arguments`). Phase 6 routes a held document to its sole reviewer. Busy or unavailable means report that state. Without a held design, retain the one-turn case.

### Durable identity, model and access

Settled Q2: keep both Astra roles in a separate store using the mesh lifecycle. Proposed root on the pair's host: `~/.local/state/guilds/roles/`, overridden by `GUILD_ROLES_STATE` in tests. Each immutable UUID `design_id` names a directory with `design.json`, a lock, `mesh/`, and saved prompts and replies. Role IDs are `<design_id>/designer` and `<design_id>/reviewer`. The mesh roster owns thread IDs; its server record supplies the serving `codex_home` (`scripts/agents/codex_mesh.py:226`, `SERVER_FILE`; `:228`, `ROSTER_FILE`). These records outlive run folders. Never copy a live roster into another run.

Settled Q14: one authority on natedev assigns a pair to its holder's host. Both installations pin it in proposed `config/guilds.conf`. Only it writes `~/.local/state/guilds/ownership/` (`GUILD_OWNERSHIP_STATE` for tests). Under a short lock, map `(repository_id, document_path)` to `DesignPlacement(design_id, host, generation)`. Register the repository once with explicit checkout mappings. A URL spelling, new worktree, missing mapping or unreachable host cannot create another identity. Normalize document paths relative to that repository; a moved document keeps an alias.

First open, adoption and holder transfer contact the authority or refuse. Require pair host = holder host before changing ownership. Reserve placement and request ID before creation; the assigned host serializes it under the design lock. Retry the same request. Timeout or uncertain creation never frees the slot. Authority loss blocks ownership changes, but existing roles may resume under local checks. No host transfer or authority failover. Retain ended identities; `open` on `RoleEnded` refuses: "role ended; this design identity cannot be reopened." Inventory both hosts and reserve legacy identities before enabling first opens; incomplete inventory requires recovery. Reuse the SSH transport pattern in `scripts/message/send.py:1096` (`remote`).

`design.json` stores schema, placement, repository, document, area, instance names, UTC creation time, holder, binding and lifecycle. Holder is `UnitHolder(production_slug, unit_name)` or `ShowrunnerHolder(production_slug)`. Resolve its current local session on dispatch and display, using `scripts/production/production_tree.py:95` (`showrunner_sessions`), `:118` (`marked_units`) and `scripts/message/roster.py:1333` (`roster`) with `ThisMachine()` (`:55`). Missing or ambiguous reach prevents dispatch; it does not change the holder.

Use `DesignUnbound` or `DesignBoundToPhase(run_directory, phase, repository, output_paths)`. Use `RoleNotOpened`, `RoleOpen`, `RolePausedForMove`, `RoleNeedsRecovery`, or `RoleEnded`. Convert nullable external fields to these states at the reader boundary (`~/.claude/docs/type_design.md`).

Add `[guild.codex] designer=gpt-6-astra:xhigh`, `reviewer=gpt-6-astra:xhigh`, and `[assignments] guild=codex` to `config/agents.conf`. Resolve through `scripts/agents/agents_config.sh:285` (`agents_resolve`). Missing or invalid rows refuse launch; no hardcoded fallback or speed-based downgrade. During this design phase, retain the run-local `AGENTS_CONFIG_FILE` and `implement.sh --to` route in the unit plan. Phase 6 replaces that route, not the thread.

Separate instructions from access. Proposed `ThreadAccessPolicy` is `PhaseBoundDesigner(binding_generation, launch_sandbox)` or `ReadOnlyReviewer`. Preserve policy on start, follow, account recovery and adoption. Today `scripts/agents/codex_mesh.py:1027` (`_seat_sandbox`) infers read-only access from `instructions_file`; `:1046` (`_resume_params`) reloads that text. Phase 6 removes that inference. Pool templates and forks get explicit read-only policy. Resolve ambiguous legacy records before dispatch.

### Lifecycle and recovery

Proposed `scripts/guilds/roles.py` exposes `open`, `adopt`, `resume`, `rebind`, `end`, `list --json`. Open takes repository, document, role, holder, area, host and writing binding. Adopt also takes source mesh, name and exact thread. Resume takes instance ID and prompt file. Rebind changes holder, names or binding only while idle, with a reason and expected generation.

| Event | Required result |
| --- | --- |
| First open or retry | Validate placement, model, account and binding. Reserve slot and launcher claim under the design lock. Start one thread outside the lock; accept its identity only against that claim. Then write its Astra hold. Duplicate open returns the role or creation state; a dead process alone does not prove creation failed. |
| New scope in a turn | Steer only within current output paths. For a new path, finish the turn, update phase files and reservation, rebind while idle, then resume. |
| Phase or run ends | Finish the turn and clear the binding after checkpoint release. Keep both threads and their saved prompt headers, which retain the phase association. Ordinary run cleanup must not end them. |
| Account move | A saved move marker with no live launcher means `RolePausedForMove`. Accepted `resume-account-move` returns it to `RoleOpen`; failure gives `RoleNeedsRecovery`. Continue busy work; an idle role moves on its next assignment without a synthetic turn. Update its hold's serving home. |
| Holder restarts or transfers | Resolve the same holder's new session. Settled Q16 permits explicit same-host transfer while idle. Rebind fences the former holder; update the census manager. |
| Crash | Reconcile thread and turn before retry; uncertain work needs recovery. A crashed review is not a completed round. Repair missing holds on the next explicit lifecycle action. |
| End | The holder may end `RoleOpen`, `RolePausedForMove` or `RoleNeedsRecovery` after proving no recorded server has a live turn. End the mesh role, release its hold, retain evidence and mark `RoleEnded`. Do not release an unfinished phase reservation. |

Hold changes are repeatable projections of the durable role; failed updates retain identity for repair. Today's `scripts/agents/codex_mesh.py:4308` (`command_end`) marks the roster ended before server contact, even if later cleanup fails. Preserve that behavior for a lost thread.

**Run environment.** `codex_mesh.py:1804` (`ensure_server`, reuse at `:1823`) reuses a healthy same-account server. `_start_server` (`:1855`) calls `_server_environment` (`:1839`) only at launch; `scripts/hooks/pre-tool-use-berth.py:43` (`_seat_session_dir`, `:54`) prefers an existing board folder. Phase 6 records the validated binding generation and resolved holder session with each server. Any mismatch requires parking queued text and releasing the old server by the rules below before dispatch; uncertain release means `RoleNeedsRecovery`. Start its replacement with the current binding's run environment; clear inherited run fields when unbound. This applies to rebind, unbind, holder restart and transfer, even when account and old run folder remain unchanged.

**Unit rename.** Extend `scripts/production/rename_unit.py:436` (`_move_state`) before relaunch to rebind each held design through the role lifecycle. Update the live holder, authority checkout mapping, binding paths and launch cwd; preserve design/thread IDs and mesh aliases. Require idle roles and refresh the server environment as above. A failed step blocks relaunch; retry through the existing rename journal. Extend `:717` (`_state_failures`) to check those live fields. In `scripts/production/unit_rename_sweep.py:70` (`_history`) and `:98` (`sweep`), treat saved guild prompts/replies as history and retained mesh aliases as identity, while still checking live routes. Do not exempt the whole role store.

**Account move.** Extend `scripts/agents/codex_mesh.py:6343` (`retire_workers_servers`) to discover durable meshes and resolve holders without run markers. `scripts/accounts/accounts.py:3344` (`_swap_workers`) selects the validated account and calls retirement. Retain the outgoing server record and home. For busy work, use `codex_mesh.py:6282` (`_interrupt_worker_for_account_move`) to save thread, turn and move marker before interruption. The attached launcher continues it, or `:6128` (`command_resume_account_move`) recovers after launcher loss. Confirm the same thread and release the old writer before new-account submission. Keep the interrupted prompt and access policy. Reconcile a `starting` claim or unknown live state before another dispatch.

For idle work, keep thread and pending text until its next authorized assignment. Before stopping any server, park queued text under the pending lock, using `codex_mesh.py:1545` (`_move_queue_to_pending`) and `:3787` (`_release_other_worker_writers`). Apply this to retirement cleanup too. Failure keeps server evidence and pending text in `RoleNeedsRecovery`. Refuse raw `stop --pause` for held roles: `:4503` (`_pause_roster`) truncates pending text and drops queued text; `:4569` (`command_stop`) deletes server records without move markers.

**Instructions and turn claim.** Extend `codex_mesh.py:1065` (`_claim_follow`) to check holder, idle state, exact thread, roster revision and design generation. Commit prompt pointer, access policy and `starting` claim together under the roster lock. Only that claim submits. Lock order: design, pending, roster, server. Park queued text before taking the server lock. Bounded local app-server queue, live-state and release calls may hold the pending lock, as `:3814–3825` (`_release_other_worker_writers`) does. No SSH, authority call or wait for an LLM turn under these locks. Failed pre-submission claims restore idle; uncertain submission needs recovery.

Held-role `set-instructions` must refuse (`codex_mesh.py:6531`, `command_set_instructions`); keep the pool's setter route (`review_pool.py:465`, `_repoint_idle`). Load tips into each new assignment's prompt. Add strict same-thread resume: `codex_mesh.py:3752` (`_resume_worker_thread`) can now start a replacement after some failures. Held roles instead report `RoleNeedsRecovery`. The holder brings an unrecoverable loss through its showrunner.

**Pending role messages, settled Q12.** Keep one pending record per message under the role directory, with ID, source and text, for active and idle routes. The holder adds it to an authorized assignment; active transport may queue it. Clear it only after that input's turn acceptance is known. On uncertainty keep it and its ID for recovery. `codex_mesh.py:4152` (`command_send`) queues text; `:4082` (`_start_turn`) submits a turn. Successful send means queued, not read. No automatic wake.

### Adoption and file ownership

Settled Q16: adopt an idle thread through explicit ownership transfer. Reserve its source at the authority as `AdoptionReserved`. First open for an inventoried legacy design returns `AdoptionRequired(source)`. The holder may keep its old route until adoption but cannot open a replacement beside it.

1. Verify source thread, name, host, serving home, holder, model, effort, pending text and evidence. Finish its turn. Record an adoption ID and same-host destination. A busy or uncertain source defers transfer.
2. Fence the source as `TransferringRole(adoption_id, destination)` under its roster lock. All old follow, reopen, move, cleanup and `implement.sh --to` routes must honor it and reject stale generations. Release the source writer with `codex_mesh.py:3716` (`_release_worker_thread`); uncertain release keeps `AdoptionNeedsRecovery`.
3. Create the destination for the same thread and mesh name with saved prompt and access policy. Never use thread start or fork. `codex_mesh.py:4458` (`command_reopen`) only changes an entry in its existing roster; it is not a transfer operation.
4. Confirm destination identity and idleness, then commit ownership and a permanent source redirect. Only the destination dispatches, moves or ends it. Preserve pending IDs and evidence. Deduplicate discovery by thread. Fences and redirects must outlive source run deletion.
5. Replace the same thread's hold and record completion. Before ownership commits, undo requires proof of no destination writer. Afterward recover at the destination. Retry the same adoption ID; never open another instance.

The holder prepares exact phase `files`, baseline and reservation before dispatch. Current launch claims phase files in `scripts/direct/implement.sh:389`; `scripts/direct/phase_reservation.py:260` (`attribute`) gives all paths to a sole open phase. Phase 6 checks binding before attribution in `scripts/hooks/pre-tool-use-berth.py:76` (`decide`): normalize paths, resolve symlinks, check both rename paths, and reject the whole patch for stale binding, unlisted paths or unreadable ledger. This guarantee covers `apply_patch` only: `decide` skips other tools at `:85`, and `codex_mesh.py:1039` (`_turn_sandbox_policy`) gives writers full access. Instruct designers to use patches for repository edits. Shell/Python writes remain outside this check. Do not copy reservation state into the role store.

A showrunner-held writer uses an authorized production-design phase with the same reservation contract, without a second director or false unit identity. phases-unit owns that change; include it in the Phase 6 Work Order before dispatch. Until it exists, a showrunner may hold a read-only role but cannot launch an unbound writer. The launcher saves reviewer replies outside the repository.

Extend durable-root discovery in `scripts/message/roster.py:1243` (`_local_roster`), `scripts/agents/mesh_runs.py:105` (`_roster_folders`) and mesh move/cleanup readers. The latter currently lists run and review-pool meshes only. `scripts/direct/end_session.sh:83` removes workers and stops run meshes; it must unbind held roles and honor transfer fences. `scripts/production/retire_unit.py:590` (`retire`) must end a retired unit's roles after their turns finish.

Keep Phase 1 edges and colors (G8). `scripts/astra/astra_roles.py:23` (`RoleRecord`), `:37` (`MeshWorker`), `:45` (`ExecProcess`), `:165` (`_parse_record`) and `:199` (`live_roles`) tie holds to a roster entry or process. Add a `HeldDesignRole` lifetime tied to the durable instance; update the exact-key decoder, writer and reader. Missing run folders cannot end it. `scripts/production/production_tree.py:255` (`astra_edges`) must still emit one edge per thread with manager, member, role, codex_home and busy/idle state. Schema stays 1 (`:296`, `main`). A hold is a census record, not the design or guild authority; removing it cannot delete the design.

## Names

| Thing | Settled name or rule |
| --- | --- |
| Process unit | `guild-process-unit`, under `sdlc`; it owns process changes, not head operations (G9). |
| Ordinary unit | `<area>-unit`; roles-unit Term table `:106`. The four head IDs below are the G7 exception. |
| Designer / reviewer instance | `<production>-<area>-astra-designer` / `<production>-<area>-astra-reviewer` (Q5); for example, `sdlc-guild-process-astra-designer`. |
| Adopted instance | Keep its mesh name as an alias, including `claude-build-followups-sdlc-guilds-design`. A rename never starts a thread. Area identifies the design, not phase or run. |
| Production and guilds | Top-level production `guilds`; guild and head-unit IDs `designers`, `reviewers`, `showrunners`, `units` (G1, G7). |
| Worker wire roles | `astra-designer`, `astra-reviewer`; these name Codex roles only. |

Use Astra designer, Astra reviewer, pooled reviewer, and designers/reviewers guild head in prose. Goddess names stay on hold (G7/Q8). A separate design needs a distinct area, brief and ID. Unit renames use the lifecycle step above; other holder or area name changes need explicit idle rebind.

## Guilds

G1/G3b use existing roles, messages and Git. There is no separate guild member list, group broadcast, publication service, manifest, delivery state or receipt.

### Guilds and heads

G1 defines membership by the role already held: an Astra designer belongs to `designers`, an Astra reviewer to `reviewers`, `/showrunner:produce` to `showrunners`, and `/unit:direct` to `units`. Role records identify held Astra members; the opening prompt identifies a one-turn reviewer. Code/test workers and pooled reviewers have no guild; directors pass tips through better Work Orders. Heads belong to `units`.

G1b/G6: one head per guild is a unit director under the lasting top-level `guilds` showrunner on natedev. herdr space `guilds` has the showrunner in tab 1, followed by the four heads in alphabetical order. Each head's standing plan runs this loop: read a member report or user feedback; check its cited evidence; write or amend a useful tip; reply with the tip path or a reason to decline. Use the Daily read below once a day. Heads run this loop, not phased builds.

The guilds showrunner keeps heads and daily timers running, relays user feedback, and reports their work. Heads own tips, not assignments, acceptance, model choices or gates. A head sends process proposals through guilds to sdlc for guild-process-unit (G9). G10 places hierarchies-unit beside this unit; its records feed the daily read.

Find a head with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --json`: select the single natedev row with production `guilds`, role `unit director`, and unit equal to the guild ID; use its address. These fields come from `scripts/message/roster.py:447` (`_serialized_entry`). Missing or multiple matches mean no send; report the lookup problem to the guilds showrunner, found in the same roster by production `guilds` and role `showrunner`. On natedev only, `showrunners.py address guilds` also resolves it (`scripts/production/showrunners.py:331`, `current_address`, returns a local socket). Before launch or during an outage, retain the report with its source evidence. Never infer addresses from names or cache them across restarts.

### Events

G3 applies to events a member observes. A director detects ledger events for its `units` report and tells an affected Astra member in its next normal brief. The same evidence may inform two guilds.

| Event | Exact trigger and evidence |
| --- | --- |
| User correction | The conversation corrects a prior instruction, result or behavior. After applying it, report the corrected rule and conversation time. New work alone does not trigger a report. |
| Repair round 4 | The director first reads `findings.py gate` with `verdict=dispatch` and `round=4` for a phase instance. `_gate_payload` (`scripts/direct/findings.py:857`) numbers the next batch; `_gate` (`:890`) records it. Re-reading that gate sends nothing new. A converged round-4 result is not this event. |
| Repeated review finding | In one run, normalize title case and spaces. Count matching `finding_opened` titles in different `(phase_instance_id, round)` groups, or a `still_open`/`reopened` verdict after `finding_batch_landed` names that finding. One group counts once across lenses; repeated verdicts for the same finding/round count once. Missing phase/round or repair order means `RepeatNotProven`. Cite both event IDs. Sources: `scripts/direct/findings.py:757` (`_open`), `:802` (`_verdict`), `:995` (`_landed`), `:399` (`_append_event`). |
| Tool or rule workaround | After using a workaround for a tool fault or slow rule, report failure, command or rule, workaround and observed cost. Merely noticing a slow command is not enough. |
| Saved time | After a method saves measured time against a stated prior run or step, report both durations and the change. State when a saving is an estimate; do not call it measured. |

Report at most 100 words in six lines: `Guild <id>: <event> — <short fact>`; `Member: <name>; holder: <address if Codex>`; `Work: <repo, branch, run/phase or design>`; `Evidence: <path:line or review IDs; time>`; `Change: <what worked or needs correction>`; `Result: <cost or saving; limit>`. Name the same source event in a retry. Keep unsent reports with existing phase/design evidence.

Claude members use `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to <head-address> --from <name> --text <report>` or SendMessage. Codex holders relay the worker's reply unchanged. Heads reply through holders under Q12; before Phase 6, retain that text in saved brief/review evidence. Report transport outcomes as returned; a queued report has not reached the head.

### Daily read

G2: one check-in every 1,440 minutes per head, using `scripts/message/notifier.sh` (`:18`, `usage`; `:77`, `schedule`). The guilds showrunner owns four timers, resolves current head sessions, and replaces stale targets after restart. The head records the last covered interval in its daily report; a duplicate check-in does no second read. After downtime, cover the gap once and state its length. This schedule wakes Claude heads only, never Codex members.

Phase 5 adds `scripts/guilds/daily_read.py` and `test_daily_read.py`. The head runs `daily_read.py --guild <id> --since <UTC> --until <UTC>` for the uncovered interval. The script reads local records only; the head requests the same command once on the Mac and combines host-labeled results. An unavailable host or missing file means partial coverage. Display times with `scripts/lib/user_zone.py:42` (`user_zone`); stored stamps stay UTC.

| Input | Reader and limits |
| --- | --- |
| Repair rounds | Reuse `scripts/history/rounds.py:224` (`load_phases`) and `:219` (`phase_rounds`). Join by run and phase-instance ID; ambiguous instances remain unassigned. The reader flags mixed records and counts rounds beyond three (`:283–285`). |
| Step time | Reuse `scripts/direct/phase_process.py:258` (`read_events`), `:293` (`phase_boundaries`), `:341` (`allocated_step_seconds`) and `:334` (`summarize`). Current boundaries use completed recorded phases, which may contain several plan phases. Parallel steps share wall time; uncovered time is not proof of director work. Unmatched intervals stay unknown (`:313`, `step_intervals`). Do not add fix time to this measure. |
| Findings | Read active and archived `findings_state*.json` and run events. Sources: `scripts/direct/findings.py:280` (`_state_path`), `:284` (`_parked_path`), `:303` (`_write_state`), `:399` (`_append_event`). Join verdicts to titles by phase instance and finding ID, then compare across the run; retain source paths. |
| Waits and merge receipts | Read hierarchies-unit Steps item 2 / Q8 when implemented: wait kind, phase, owner, UTC bounds; receipt project, scope, commit, branch and merge time. Missing means `HistoryNotRecorded` or `ReceiptMissing`, never inferred waiting or delivery. This unit adds no writers and does not decide hierarchy Q8. |

`units` reads directors' runs; `showrunners` reads productions' runs and merge/wait records. Astra holds cannot supply history: `scripts/astra/astra_review.py:164–168` (`run_review`) releases them, and `scripts/astra/astra_roles.py:199` (`live_roles`) removes ended holds. From Phase 4, save assignment metadata before dispatch in a first-line `<!-- astra_work {JSON} -->` brief/prompt header: `schema=1`, `role` (`astra-designer` or `astra-reviewer`), `member`, `document`, `assigned_at_utc`, `reply_path`, `work`. The `work.kind` tag selects `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)` or `StandaloneWork`, with no nullable phase fields. The holder supplies explicit run/phase context; copy IDs and history path from `progress_history_state.json` (`scripts/direct/progress_history.py:1234`, `_start_run`; `scripts/direct/findings.py:359`, `_phase_identity`). Reject a supplied phase that does not resolve.

Discover designer briefs at `<run>/design/phase*/round*_brief.md` beneath `scripts/lib/unit_runs.py:44` (`run_roots`); holders save each brief and its `round<N>_reply.md` without overwrite. Discover one-turn reviewer headers at `/tmp/claude/astra/*/prompt.md` (`astra_review.py:90`, `run_folder`; `:185–190`, `main`); add explicit run/phase inputs in Phase 4. `member` is the mesh name or that review folder; `reply_path` names its saved output. Phase 6 saves the same header in `<roles-root>/<design_id>/prompts/*.md` before dispatch and retains it across unbind/end. Read headers only, join their phase IDs to ledgers/history, and filter events to the interval. Deduplicate phase totals. Missing headers mean `MembershipNotRecorded`; missing output leaves the dispatch outcome unknown; standalone reviews have no phase metrics. Removed evidence means partial coverage. Never assign all implementation or pool reviews to Astra; phase totals do not measure an individual's delay.

"Looks bad" means: at least four repair rounds; the repeated-title rule; a mixed phase record; or allocated step time above its p90 in the preceding 14 complete days on that host, with at least 20 samples. The baseline available on 2026-10-21 is phases-unit's cited design `:133–150`: review p90 1,103.6 seconds (647 samples), implementation 1.23 hours (1,373), phase gap 2,767.1 seconds (1,319). These are starting examples; print each read's actual threshold and sample count. Sparse time data gets no speed verdict. Also flag an open recorded wait lasting at least 24 hours, or a claimed merge without its receipt once writers exist. These select evidence, not new stop rules or model changes.

Print host, guild, UTC interval and local times, member/run coverage, counts, repair rounds, repeated titles, step seconds with threshold/sample count, waits, merge receipts, and problems. Each flagged row names its source and phase/turn reference. The head checks that record, then reads only relevant turns. Its existing session report lists causes, tip edits, process proposals and missing evidence, or says no record was flagged.

### Tips

G3b/G4: tips live in `~/rust/nate_style/guilds/designers/`, `reviewers/`, `showrunners/`, `units/`, and `common/`. User feedback comes in the head tab or through any showrunner. Tips cannot change a user rule. A tip for all four guilds goes in common. Its accepting head owns later edits; record it as a `head/<id>` tag. Ask that head to amend a conflicting tip.

Use one file per tip, in the form of `~/rust/nate_style/rust/always-use-nextest.md:1`: YAML front matter, one `##` heading, then short text. Example:

```markdown
---
tags: [guild/common, guild-event, head/units]
date_created: "[[2026-10-21]]"
date_modified: "[[2026-10-21]]"
---
## Include evidence in a guild report
Name the source path, event time and observed result. State any missing evidence.
Source: G3; docs/plans/sdlc-guilds.md, Source, 2026-10-21.
```

Create `guilds/designers_base/`, `reviewers_base/`, `showrunners_base/`, `units_base/`. Each has `events.base` and `tools.base`, filtering by `guild-event` or `guild-tool` and either its `guild/<id>` tag or `guild/common`. Use YAML `views`, `type: table`, `filters` with `file.hasTag(...)`, and columns `file.name`, `tags`, `date_created`, `date_modified`, sorted by name, as in `~/rust/nate_style/rust_base/naming.base:1`. Views index tip files.

Phase 4 seeds five settled tips: `designers/same-thread.md` (same-thread revisions), `reviewers/cite-evidence.md` (cited defects), `showrunners/relay-corrections.md` (G4), `units/report-round-four.md` (G3 trigger), `common/report-evidence.md` (the example, owned by units head). Guild tips carry their guild/head tags and `guild-event`; tools views may start empty. This reviewed setup precedes head launch. Thereafter only the owning head adds or edits tips.

Prepare tip text before changing the checkout. All four heads use one advisory lock at `<git-common-dir>/guild-tips.lock`; skill-start pulls use the same lock on each host. In one bounded command, lock, refuse pre-staged or owned-path edits, pull `--ff-only`, apply the prepared edit, commit owned tip/view paths on `main`, and push. Never stage, stash or reset unrelated edits, including `review-charter.md` and `.obsidian/`. On divergence at pull or push, fetch; only with a clean tracked tree and solely this edit's unpublished commits, rebase those commits onto `origin/main` and retry a normal push. On conflict, abort the rebase, retain the local commit and report to guilds; prepare a revised owned edit before retry. Dirty or foreign unpublished work needs its owner. No force push. Release the lock on failure; never hold it during an LLM turn. Report the commit only after push succeeds.

At skill start, the Claude session or Codex holder uses that lock to pull `nate_style` with `--ff-only` and resolve one commit. Read common and own-guild tips from that commit with Git file reads. On lock or pull failure, report it and use the last local committed tips, stating their commit. With no baseline, restore it before starting.

### Guild block

G5: install this exact block at the start sites in Command changes, replacing only `{guild}`. Link each contract to Events for detection and report form. The Codex holder includes the expanded block and tip bytes in the worker prompt; `scripts/astra/astra_review.py:45` (`review_prompt`) constructs reviewer input separately from the command document.

> Your guild is `{guild}`. Your head is the `{guild}` unit director in the top-level `guilds` production on natedev. At skill start, use the Tips lock/pull rule for `~/rust/nate_style` and load `guilds/common/` and `guilds/{guild}/`; a Codex holder does this for its worker. Report to your head when the user corrects you, a phase needs repair round 4, a finding meets the Events repeat rule, you use a workaround for a tool fault or slow rule, or a method saves measured time. Send one report of at most 100 words, with evidence and result. Resolve the head from `message/roster.py --json` by production `guilds` and unit `{guild}`; send with `message/send.py` or SendMessage. A Codex worker puts the report in its reply for its holder to send. Send only to the head. Never wake an idle Codex member for guild advice.

### Data for cargo-handler

Combine the Astra edges specified in Roles with roster production/unit/address fields (`scripts/message/roster.py:447`, `_serialized_entry`). Heads remain ordinary unit edges under guilds; members keep their real manager. Phase 6 `roles.py list --json` adds local durable roles, holder reach and paused/recovery states without network calls; unknown records produce problems. The director sends these rules to cargo-liner and row changes through sdlc to roles-unit and phases-unit.

## Command changes

Hub owners: `docs/plans/sdlc-production.md:46`, Hub files. All `direct.md` rows are **change requests to phases-unit**, including pointed contracts. Map means the fetched phases-unit Map cited above.

| Contract and owner | Change request / Map row |
| --- | --- |
| `commands/unit/direct.md`, opening before phase selection / StandingPlanRefill — phases-unit | `units` block; head plans use the Guilds loop. See Phase-map differences. |
| Same, introduction / LongLivedWorkers — phases-unit | Astra writes; director briefs and accepts; one lasting pair. Map 6, 9, 11, 18, 22; document row Design doc. |
| Same, CoordinateDelegatedPhaseReservation / DelegatedPhaseReservationContract — phases-unit | Exact writing paths, including showrunner design tasks; new paths need idle rebind. Map 4. |
| Same, PassOwnership / LaunchImplementation — phases-unit | Save baseline/pass and each Astra brief with its work header; use configured same-thread designer and access policy; no tester for this design workflow. Map 5–7. |
| Same, EarlyReviewArm / DualReview — phases-unit | Phase 6 keeps one Astra reviewer thread across rounds; save findings, response and ruling. Map 8–9; document row Review output. |
| Same, Synthesize / FixDispatch — phases-unit | Short cited rulings and same writer; update Work Orders for scope changes. Report round 4 and repeated titles without changing gate outcomes. Map 11–12. |
| Same, PhaseCleanup / FinalGate / RunSummary — phases-unit | Unbind after release; retain roles until explicit end. Map 14, 18, 22. |
| `commands/showrunner/produce.md`, opening before ExecutionSteps / DesignAuthority / DesignCheck — roles-unit | `showrunners` block and production-design holder contract; retain visual checks. |
| `docs/production_format.md`, Words / ProductionUnit — roles-unit | Four unit heads under guilds; names and standing-loop exception; keep worker IDs. |
| `commands/message.md` and `codex/AGENTS-messaging.md`, Outcomes and recovery — guild-process-unit contribution | Holder route from Events and pending messages from Roles. |
| `commands/astra_review.md`, Usage / Steps; `scripts/astra/astra_review.py`, review_prompt / main — guild-process-unit | Phase 4 `reviewers` block, tips and saved work headers with explicit run/phase context; Phase 6 held selection. Map document row Review output. |
| New `config/guilds/designer.md`, Guild section of opening prompt — guild-process-unit | `designers` block; holders include it in Phase 4 briefs; Phase 6 `roles.py` loads it. |

Roles-map differences: D30–D31 (`:65–66`) and Term table Astra rows (`:119–120`) become designers/reviewers unit heads under guilds, with no top-level head or temporary worker-role name (G1b/G7). D32 (`:67`) assigns heads their tips and reports (G3b/G4); add showrunners and units heads (G1). Term table unit (`:106`) has four unsuffixed head IDs (G7). D10–D11, D27–D28 (`:45–46`, `:62–63`) do not require heads to run phases, dispatch workers, shrink or close a build. D12 (`:47`) permits heads to write tips directly (G3b). D51–D52 and pooled-reviewer duties stay unchanged.

Phase-map differences: rows 6, 8–9 use one designer and one reviewer, without a tester or extra lenses for this design workflow. Row 12 resumes the writer with a ruling, not a new Work Order unless scope changes. Row 22 retains lasting roles; row 18 already keeps threads after phases. Design doc names Astra as author. The standing-plan branch after row 22 (`:81–85`) still dispatches tickets today; the four heads use the G1b event/daily loop. Unit plan must allow those standing head plans. Other builds, smoke, reservation release and checkpoint order stay under the Map.

## Steps

The director updates Work Orders/files before dispatch (Map 1–4). Implementation runs checks/reviews (7–13), checkpoints/releases reservations (14–15), then updates later work (16–21). Phases 5 and 6 both depend on Phase 4; Phase 6 may proceed without head launch.

### Phase 4 — Tips and guild blocks

**Changes:** create Tips' five seed files and eight tag views; install the four blocks through the Command changes sites. Add saved Astra work headers and explicit review run/phase inputs from Daily read. Agree hub writers and script/test files before dispatch. Use the Tips lock and Git rule after the showrunner widens this unit's Owns cell to the approved `nate_style` paths (unit plan, Decisions).

**Checks:** parse views and tip front matter; check tags, one heading and four blocks. Exercise generated prompts, phase headers, standalone reviews and invalid phase context with fixtures. Test two head edits plus a skill-start pull, rejected push/rebase, unrelated dirty work, failed pull and missing baseline without real state. Trial-merge hubs against owner tips; run the merge gate. Director live check: a new `/unit:direct` names its head and lists common/units tips; the head need not be running.

**Undo:** restore former prompts/contracts and revert this phase's tip/view commits through Git. Reverted tips reach each host at its next skill-start pull.

### Phase 5 — Guilds production and daily read

**Changes:** add `docs/plans/guilds-production.md` and four `guilds-<guild>.md` standing plans for the loop, duties and tab order. In `scripts/production/add_unit.py:286` (`launch_request`; suffix at `:359`), preserve the four exact G7 IDs only for production `guilds`; ordinary units keep their suffix. Include `test_add_unit.py`; `:381` (`row_is_present`) must find those same IDs on restart. Add the Daily read script, fixtures and lint coverage. After the gate, sdlc starts top-level guilds; guilds starts its heads and four timers.

**Checks:** launch, adopt existing rows, restart and resolve roster heads under each exact G7 ID; other productions keep `<name>-unit`. Populated designer/reviewer fixtures must join saved headers to phase rounds, step times and findings after holds end, and after Phase 6 unbind. Cover standalone work, both hosts, missing evidence, same-round duplicate lenses, post-repair verdicts, ambiguous repeats, parallel steps, sparse baselines and absent hierarchy records. Read no turns and write no history. Run gate/lint. Director prints this run's day for units; after launch, guilds checks tabs, lookup, one report and timers.

**Undo:** guilds stops timers and head loops; retain tips, evidence, docs and unsent reports. Revert reader/contracts as needed. Keep exact-ID launcher support while these head rows exist. Do not delete member roles or end designs.

### Phase 6 — Lasting roles

**Changes:** implement Roles and its listed integrations, including server environment refresh, `rename_unit.py` and `unit_rename_sweep.py`; add `config/guilds.conf` and lint coverage. Keep Daily read's assignment headers in durable prompts. Coordinate the reservation bridge with phases-unit and hubs with roles-unit.

**Checks:** simultaneous two-host opens, aliases, lost acknowledgement, unknown creation, unreachable authority, host mismatch and open-after-end cannot create two pairs. Keep threads through revisions, run end, restart and hold repair. Test busy/idle moves, queue preservation, marker-before-interrupt, old-writer release, paused/recovery states and refused raw pause. Race follows/pending sends in lock order; failed claims submit nothing. Reject stale/unbound/unlisted patch edits, sole-phase bypass, mixed patches, symlink and rename escapes without reservation. Test new-path steering refusal, read-only review, saved interrupted prompts and end from recovery. Rebind A to B with A's folder present and the holder/account unchanged: the next turn and patch check must use B. Rename a unit and checkout, retry the rename, then resume both original threads at the new paths. Verify stale live fields fail while saved prompts/replies and aliases pass.

Adoption fixtures cover both known launch routes, legacy follow, cleanup during transfer, release failure, crashes and retries: one thread, owner and hold; preserved text/evidence; old routes refused. Tests use isolated roots. Run the merge gate and lint. Director live check: adopt an idle designer, end its old run, resume that thread with its binding, and show one Astra edge. A held reviewer must also resume its thread and remain read-only.

**Undo:** disable new open/adoption and finish active turns. Retain placements, fences, durable records, pending text and evidence. Release servers safely, never by raw pause. Keep adopted ownership at its destination unless reverse transfer proves no destination writer. Restore contracts only when they cannot bypass fences. Keep unfinished reservations. At run closeout, convert this design to `docs/as-built/guilds.md` (Map 22; As-built document row).

## Questions for the user

None from this revision.
