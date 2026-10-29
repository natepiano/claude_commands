# Hierarchy for fast Hana delivery

> **Status: DESIGN — the user's review is folded (HI1–HI11, 2026-10-28). A runs as an experiment; Steps 1, 2 and 4 are approved.**

This document records constraints, role contracts, channels, a measured baseline, and proposed steps. It changes no live process.

Source: [unit plan](build-followups-hierarchies.md), Decisions and Phase 1.

## Constraints

Each row is one constraint. `user` means a user rule or standing user contract in the named source. `derived` means an inference from named user words.

Memory references below use these roots:

| Prefix | Memory folder |
| --- | --- |
| `config/` | `~/.claude/projects/-home-natepiano--claude/memory/` |
| `nixos/` | `~/.claude/projects/-etc-nixos/memory/` |
| `hana/` | `~/.claude/projects/-home-natepiano-rust-hana/memory/` |

Production references use these files. They came from `~/.claude/scripts/lib/py scripts/production/showrunners.py list` during this phase.

| Name | Production document |
| --- | --- |
| build-followups | `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md` |
| remove-running-section | `/home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md` |
| cargo-liner-style | `/home/natepiano/rust/cargo-liner-style/docs/cargo-liner-style-production.md` |
| tool-based-ui | `/home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md` |
| sdlc | `/home/natepiano/worktrees/claude-build-followups-sdlc/docs/plans/sdlc-production.md` |
| performance | `/home/natepiano/rust/tool-based-ui-frame-time/docs/hana/performance-production.md` |

| ID | Kind | Constraint and source |
| --- | --- | --- |
| C01 | user | Find the hierarchy that delivers Hana fastest within the user's constraints. Fewer layers alone do not define success. Source: user, 2026-10-20, [unit plan, Source and Decisions](build-followups-hierarchies.md). |
| C02 | user | Keep top-level showrunners as peers. The user wants a flat organization. Source: user, 2026-10-20, `nixos/flat-organization.md`; sdlc, Production rules. |
| C03 | user | The guild heads are not sdlc units. As built, the four heads (`designers`, `reviewers`, `showrunners`, `units`) are unit directors of the `guilds` production. The guilds showrunner is a child of sdlc in the showrunner tree; its herdr space stays top level, and the heads are units of guilds, not of sdlc (user, "attach", 2026-10-28, via the sdlc showrunner; user G1b, `docs/plans/sdlc-production.md`; `docs/as-built/guilds.md`). |
| C04 | user | Ask the user before promotions, unit renames, role changes, or process changes during this redesign. Source: user, 2026-10-20, `nixos/process-redesigns-are-questions.md`; sdlc, Production rules. |
| C05 | user | The user decides real scope changes, irreversible actions, and choices about their priorities, money, licences, or intended use. Source: `~/.claude/docs/decision_criteria.md`, DecisionEconomy; user, 2026-10-12, `hana/tool-design-changes-are-the-users.md`. |
| C06 | user | Decide obvious, reversible choices. Record each self-made call and its risk in one line. Source: `~/.claude/docs/decision_criteria.md`, DecisionEconomy. |
| C07 | user | Correctness comes first. Then rank Simple, Fast, Beautiful, in that order. Work cost does not break a tie. Source: user, 2026-10-01, `~/.claude/docs/decision_criteria.md`, ThreeGods and DecisionEconomy. |
| C08 | user | Showrunners settle unit designs, add-ons, and visual choices within approved scope. C04 still governs this process redesign. Source: user, 2026-10-12, `hana/tool-design-changes-are-the-users.md`; [production contract, items 2 and 12](../production_format.md). |
| C09 | user | Give dailies on demand. Start no report timer unless the user asks. Source: user, 2026-10-10 and 2026-10-13, build-followups, tool-based-ui, and sdlc, Production Context; [production contract, Updates](../production_format.md). |
| C10 | user | Show one change or proposal only when asked. After its verdict, record it and stop. Source: user, 2026-10-20, `nixos/next-thing-never-auto-advances.md`; `commands/next.md`. |
| C11 | user | Keep proposed plans and efficiency work in their queues. Send no reminder; show one item when asked. Source: user, 2026-10-09 and 2026-10-10, `nixos/plan-backlog.md` and `nixos/nightly-proposals-one-at-a-time.md`. |
| C12 | user | Use no form or survey. Ask one plain-text question with a recommendation. Source: user, 2026-10-14, `config/no-forms.md`. |
| C13 | user | Ask a question once in full. Later replies give only the open-question count. Give no count when none remain. Source: user, 2026-10-12 and 2026-10-13, `nixos/open-questions-as-a-count.md`. |
| C14 | user | Start user messages with the situation and required action. They must make sense without session context. Source: user, 2026-10-13, `nixos/user-messages-must-read-cold.md`. |
| C15 | user | Use Simplified Technical English in all prose. Use short active sentences, common words, and one meaning per word. Source: user, 2026-10-18, `config/simplified-technical-english.md`; `~/.claude/CLAUDE.md`, word list. |
| C16 | user | Report times in America/New_York and name the zone. Read the clock first; date times from other days. Source: user, 2026-10-12, `nixos/name-the-time-zone.md`; build-followups and tool-based-ui, Production Context; `scripts/lib/user_zone.py`. |
| C17 | user | Phone pushes name an action only the user can take. Tests and live checks never push. Source: user, 2026-10-05 and 2026-10-10, `nixos/phone-push-must-be-actionable.md`; `config/fyi-push-held-by-review.md`. |
| C18 | user | Send routine peer work to the peer. Give the user only decisions, user-only actions, blocks, and faults they would see. Source: user, 2026-09-09 through 2026-10-16, `nixos/relay-directly-not-via-user.md`. |
| C19 | user | Keep replies short and answer the question asked. A question about why something happened does not authorize a new action. Source: user, 2026-10-08, `nixos/answer-in-a-few-lines.md` and `nixos/a-question-gets-an-answer-not-an-action.md`. |
| C20 | derived | Route unit requests through their showrunner. Inference from the user's 2026-10-12 attention rule in `nixos/relay-directly-not-via-user.md`; applied by hana, 2026-10-18, `hana/unit-asks-go-through-the-showrunner.md`. |
| C21 | user | Leave Remote Control on or off to the user. Send no alert and take no action solely because it is off. Source: user, 2026-10-15, `nixos/act-on-an-alert-a-session-can-do.md`, superseding its earlier example. |
| C22 | user | Rank strategic goals as Ship Hana, Find Collaborators, Seek Investors, then Business Viability. Goal order gives a soft score bonus, not strict priority lanes. Change goals only with explicit approval. Source: `~/rust/hanadocs/prioritization goals.md:9`, Current goals and Maintenance. |
| C23 | user | Give demo and herdr work priority over other work until the user ends that order. Source: user, 2026-10-17, `nixos/demo-and-herdr-priority.md`. |
| C24 | user | Understand builds and speed them up. Use measured evidence for claims about saved time. Source: user, 2026-10-07, `config/build-speed-top-priority.md`. |
| C25 | derived | Share natedev's build capacity across productions; a unit count is not a separate hardware allowance. Inference from the user's scarce-build-resource goal, 2026-10-16, `config/build-report-primary-job.md`; build-followups and sdlc, Capacity. |
| C26 | derived | Respect shared build and memory limits. Inference from the user's 2026-10-16 scarce-build-resource goal, `config/build-report-primary-job.md`; sdlc records 32 cores and 60 GB; `nixos/build-slot-pool-steve.md` records 31 shared slots and a 12 GiB brake. |
| C27 | user | Try a free Mac first for supported tests. Allow one Hana job and one cargo-liner job at once. Source: user, 2026-10-14, `config/mac-offload-idle-and-limits.md`. |
| C28 | user | Mac load and user presence alone do not stop builds. Respect holds and claims; ask the user when a run needs exclusive use. Source: user, 2026-10-10, `hana/mac-builds.md`, Mac builds ignore user presence. |
| C29 | user | A full Mac test pass ends a supported test gate. Use Linux-only routing only for tests that require Linux. Source: user, 2026-10-16, `hana/test-runs-on-either-machine.md`. |
| C30 | user | Only a director asks its showrunner for a timing hold. Workers place no build holds. Ordinary demo runs need no hold. Source: `hana/build-holds.md`, Seats never place build holds; user, 2026-10-19, `hana/demo-runs-need-no-hold.md`. |
| C31 | user | Limit saved run output to a few GB. Read and delete each run before the next. Clean Mac scratch builds after use. Source: `hana/timing-runs-keep-a-few-gb.md`, showrunner rule, 2026-10-04; user, 2026-10-06, `hana/mac-builds.md`, Mac disk is small. |
| C32 | user | Never poll a build, test, or verify run. Long commands detach and send their result to the caller. Source: user, 2026-10-16, `config/never-poll-builds.md`; `commands/unit/direct.md`, WorkerRuns. |
| C33 | user | Use one role-to-account map on natedev and the Mac. natedev manages all director accounts and effort. Source: user, 2026-10-12 and 2026-10-16, `nixos/natedev-owns-unit-account-and-effort.md` and `nixos/role-accounts-same-on-every-machine.md`. |
| C34 | user | A quota reading alone never triggers a pause or move. The next-moves queue controls moves at the user's threshold. Source: user, 2026-10-16 through 2026-10-19, `nixos/account-moves-at-one-percent.md` and `config/showrunner-moves-by-queue.md`. |
| C35 | user | Honor the next queue entry first. A blocked entry permits no fallback move. Read current entries; do not copy old account destinations. Source: user, 2026-10-17, `nixos/account-moves-at-one-percent.md`, The user's design. |
| C36 | user | Run authorized account moves without a new permission question. Check with a director before a restart could stop its worker run. Source: user, 2026-10-15, `config/account-moves-need-no-approval.md`; hana ruling, 2026-10-18, `config/ask-director-before-moving-unit.md`. |
| C37 | user | With Codex credits enabled, keep working at zero weekly allowance. With credits off, use the configured account rule. Source: user, 2026-10-14, `nixos/codex-run-out-is-not-a-block.md`, superseding its older move rule. |
| C38 | user | Do not move quota-blocked Codex work to Claude on a meter reading. Actual refusal and the user's fallback rules govern. Source: user, 2026-10-07 and 2026-10-09, `nixos/codex-run-out-is-not-a-block.md`; `hana/codex-accounts.md`, Check quota before resuming units. |
| C39 | user | Do not downgrade a judgment role from speed or token results alone. The user must try its judgment first. Source: user, 2026-10-06, `config/director-model-quality.md`; directors returned from Sonnet to Opus. |
| C40 | user | Keep Codex workers across phases and repair rounds. Reuse their threads after account moves. A worker lives as long as its unit director, across runs, and is reused by name; a run end ends only workers whose director is gone or whose owner cannot be found (C56, HI9, 2026-10-28, which replaces the older rule to end run workers at run end). Source: user, 2026-10-10, 2026-10-16 and 2026-10-28, `config/workers-persist-across-phases.md` and `config/resume-codex-seats-after-move.md`; [production contract, Worker](../production_format.md). |
| C41 | derived | Keep pooled reviewers for later unit runs; retire them with the unit after active reviews end. Inference from the user's 2026-10-10 request for persistent reviewers, `config/workers-persist-across-phases.md`; implemented by [production contract, unit retirement](../production_format.md). |
| C42 | user | Store only facts that scripts cannot find quickly. Look up live session names, account choices, and run state when needed. Source: user, 2026-10-08, `nixos/store-only-what-cannot-be-looked-up.md`; [production contract, Units](../production_format.md). |
| C43 | derived | Keep this baseline as dated history, never as a live routing list. Inference from the user's 2026-10-08 rule against copied live state, `nixos/store-only-what-cannot-be-looked-up.md`. |
| C44 | user | Run safe independent phases beside one another. Keep their file ownership and checkpoints separate. Source: user, 2026-10-06, `hana/one-phase-at-a-time-per-unit.md`; [production contract, item 3](../production_format.md). |
| C45 | user | Let the showrunner and user change product priority. Give units no deadline-driven cut or drop order. Source: user, 2026-10-08, `hana/units-do-not-carry-the-deadline.md`; reversible packaging still follows DecisionEconomy. |
| C46 | user | Use Codex workers for planned writing. Directors and showrunners coordinate; they write no implementation code. Source: user, 2026-09-30, `hana/plan-work-runs-under-unit-delegate.md`; [production contract, Words](../production_format.md). |
| C47 | derived | Use an in-place helper for a small unowned fix. Inference from the user's 2026-10-08 words, "For small things that seems appropriate", in `nixos/small-fixes-by-subagent-in-main.md`; its size test is interpretation. |
| C48 | user | Route structural design to hierarchies-unit and role contracts to sdlc. Send role or name changes through their showrunners. Source: user, 2026-10-20, sdlc, Unit scopes and Production rules; [unit plan, Scope beside sdlc](build-followups-hierarchies.md). |
| C49 | derived | Keep measurement read-only, with no messages, pushes, or new sessions. Inference from the user's 2026-10-20 request to improve communication; [unit plan, Source and Invariants](build-followups-hierarchies.md) adds the plan-author rule that measurement must add no traffic. |
| C50 | user | Type Python throughout. Use specific JSON types; allow no file-level type ignore or untyped escape. Source: `~/.claude/CLAUDE.md`, python. |
| C51 | user | No ship date. Drive Hana functionality at the fastest rate under good ergonomics, beauty, and a robust sdlc. Q1 answer: "no date - we are focused on simply driving functionality at the fastest rate under the constraints of good ergonomics, beauty, and a robust sdlc". Source: user via the showrunner, 2026-10-20. |
| C52 | user | cargo-liner and its crates supply Hana. Publishing more crates is not a goal. Q2 answer: "they are tools for hana - we have published a couple of them but it's not super important to me that we publish more - as they are becoming more specialized for our work flow". Source: user via the showrunner, 2026-10-20. |
| C53 | user | Treat all of this as experimental. Each rule is a trial with a measure and a review point, and it is dropped if it does not help. HI1: "i think of this all as experimental and you should treat it as such". Source: user via the showrunner, 2026-10-28 (sdlc production, Review decisions). |
| C54 | user | Every new piece of work in build-followups, sdlc and cargo names its link to Hana, direct or indirect. No measured Hana cost and no fixed share. HI2. Source: user via the showrunner, 2026-10-28 (sdlc production, Review decisions). |
| C55 | user | The account count is fixed. A proposal for one more account needs records that it ships more Hana work; cost per phase alone is no case. HI4. Source: user via the showrunner, 2026-10-28 (sdlc production, Review decisions). |
| C56 | user | Workers live as long as their unit director, across runs, and are reused by name. HI9. Source: user via the showrunner, 2026-10-28 (sdlc production, Review decisions). |
| C57 | user | No central worker allocation; each showrunner starts its own workers. HI11. Source: user via the showrunner, 2026-10-28 (sdlc production, Review decisions). |

C02 covers top-level peers. It does not forbid every child production: the user made performance a child of tool-based-ui on 2026-10-20. The live showrunner tree is the record of child edges (HI5); propose no new showrunner, unit, promotion, or edge in this document.

C04 is the specific user exception to C06 and C08 for this redesign. The 2026-10-20 rule does not require approval for measurement or writing. Source: `nixos/process-redesigns-are-questions.md`; [unit plan, Decisions](build-followups-hierarchies.md).

C45 concerns changes to product priority and deadline cuts. C06 covers packaging with the same finished behavior. Source: `hana/units-do-not-carry-the-deadline.md`; `~/.claude/docs/decision_criteria.md`, Packaging of committed work.

Hardware and pool figures in C26 are source records, not a new hardware probe. Production unit counts also do not prove a current capacity ceiling. Source: sdlc and remove-running-section, Capacity; `commands/producer/greenlight.md`, Capacity.

The Mac disk figures and natedev disk floor in older memories are historical. They are not current capacity readings. Source: `hana/mac-builds.md`; `hana/disk-floor-sweeper.md`, Floor size, 2026-10-08.

## Map

### Roles

These rows describe contracts. They do not assign new duties.

| Role | Defining file | Work | Reports to |
| --- | --- | --- | --- |
| Executive producer | `docs/production_format.md`, Words | Sets product, scope, and irreversible choices | No parent; the user |
| Producer | `commands/producer/greenlight.md`; `commands/producer/to_production.md` | Proposes units and writes production plans | User |
| Top-level showrunner | `commands/showrunner/produce.md` | Merges, tests, clears blocks, settles designs, reaches user | User |
| Child showrunner | `docs/production_format.md`, Words | Runs its own production and units | Parent showrunner; user may speak directly |
| Unit director | `commands/unit/direct.md`; `docs/production_format.md`, ProductionUnit | Writes orders, dispatches workers, gates and checkpoints work | Its showrunner |
| Codex worker | `commands/unit/direct.md`, LongLivedWorkers and WorkerRuns | Writes, repairs, tests, or reviews assigned files | Unit director |
| Pooled reviewer | `docs/production_format.md`, Words and item 16 | Reviews read-only in a persistent thread | Unit director through review lease |
| Review template | `docs/production_format.md`, Words | Holds the standing review prompt | Review-pool scripts; never leased |
| Small-fix helper | `nixos/small-fixes-by-subagent-in-main.md` | Writes a small unowned fix in place | Sending showrunner |
| Guilds showrunner | `docs/plans/guilds-production.md` | Runs the `guilds` production and its four head units | sdlc showrunner (child of sdlc since 2026-10-28; its herdr space stays top level) |
| Guild head (`designers`, `reviewers`, `showrunners`, `units`) | `docs/as-built/guilds.md`, Guilds and heads | Unit director that keeps its guild's tips | Guilds showrunner |

`astra-designer` and `astra-reviewer` name the held per-design roles and their census edges, not the heads. Goddess names are on hold. Source: `docs/as-built/guilds.md`, Names.

### Productions

The current edges below come from `~/.claude/scripts/lib/py scripts/production/showrunners.py tree` during this phase. Document presence does not prove a live session.

| Production | Showrunner | Parent shown by tree | State or scope source |
| --- | --- | --- | --- |
| build-followups | natedev | None | build-followups production, Production Context; build and agent tools |
| remove-running-section | cargo-liner | None | remove-running-section production, Production Context; cargo tools |
| cargo-liner-style | cargo-liner-style | remove-running-section | cargo-liner-style production, Kind and Parent production |
| tool-based-ui | hana | None | tool-based-ui production, Production Context; Hana product |
| sdlc | sdlc | None | Running; sdlc production, Production Context |
| performance | fps | tool-based-ui | Created 2026-10-20 after the Baseline; four units; performance production, Production Context and Units |
| Guild heads | `designers`, `reviewers`, `showrunners`, `units` | Unit directors of `guilds` (not of sdlc) | `docs/as-built/guilds.md` |
| Astra designer / reviewer | `astra-designer` / `astra-reviewer` | Held per-design roles under a unit director or showrunner | `docs/as-built/guilds.md` |

The style, performance and guilds child edges remain observed edges. C02 keeps top-level showrunners as peers; The live tree is the record (HI5). The live tree labels performance’s showrunner `performance`; its production document names the session `fps`. This is a source difference, not a rename decision. Sources: live `showrunners.py list` and `tree`, and the production documents read for this fold.

### Channels

The command files in the last column define each path. Script paths are relative to `~/.claude/`.

| Channel | Sender | Receiver | Script or command source |
| --- | --- | --- | --- |
| Native session message | Claude session | Claude peer | `SendMessage`; `commands/message.md` |
| Roster message | Agent or script | Addressed Claude or Codex session, either machine | `scripts/message/roster.py`; `scripts/message/send.py`; `docs/as-built/session-roster.md` |
| `/agent_send` | Session | One resolved live peer | `SendMessage`; `commands/agent_send.md` |
| `/announce` | Session with user instruction | All roles, both machines | `scripts/production/broadcast.py --all`; `commands/announce.md` |
| `/unit:announce` | Session with user instruction | Unit directors | `scripts/production/broadcast.py --units`; `commands/unit/announce.md` |
| `/showrunner:announce` | Session with user instruction | Showrunners | `scripts/production/broadcast.py --showrunners`; `commands/showrunner/announce.md` |
| `/notify_top_level` | Session | Top-level peers; showrunners relay to units | `scripts/message/top_level.py`; `SendMessage`; `commands/notify_top_level.md` |
| `/forest_and_trees_alert` | Invoking session | Other live Claude peers | `ListAgents` and `SendMessage`; `commands/forest_and_trees_alert.md` |
| Worker message | Unit director or peer | Live Codex worker | `scripts/agents/codex_mesh.py send`; `commands/unit/direct.md` |
| Worker board | Worker | Unit director and peer workers | `scripts/direct/board.sh`; `commands/unit/direct.md` |
| Detached run result | Build or test wrapper | Calling worker; director when caller unavailable | `scripts/agents/mesh_runs.py`; `commands/unit/direct.md`, WorkerRuns |
| Phone push | Authorized session or automation | User | `scripts/message/send.py --to user` → `scripts/notify/review.py` → `scripts/notify/pushover.py` |
| `/alert_user` | Session blocked on user | User phone | `scripts/message/send.py`; `commands/alert_user.md` |
| `/nate` | Invoking session | User, now or at requested time | `scripts/message/send.py`; `scripts/message/send_later.py`; `commands/nate.md` |
| Push-review control | User through session | Push review service | `commands/message_review.md`; `scripts/notify/review.py` |
| Showrunner update timer | Requested notifier | Showrunner session | `scripts/message/notifier.sh`; `commands/showrunner/report_interval.md`; on demand by default |
| `/showrunner:dailies` | Showrunner when asked | User | `scripts/production/dailies_input.py`; `dailies_render.py`; `commands/showrunner/dailies.md` |
| Open question | Unit through showrunner | User; durable question list | `scripts/production/update_registration.py questions`; `commands/showrunner/produce.md` |
| Change review | Checkpoint merger or showrunner | User when asked | `scripts/production/change_review.py`; `commands/change_review.md` |
| Proposal issue | Unit through showrunner | User when asked; approved work returns to unit | `scripts/production/proposal_issue.py`; `proposals_table.py`; `commands/showrunner/proposal.md` |
| Plan backlog | Requested plan author | User when asked | `~/.local/state/plan-backlog/ledger.md`; `nixos/plan-backlog.md`; `proposals_table.py` |
| Efficiency ledger | Nightly review | User when asked | `scripts/nightly_review/candidates.py`; `commands/efficiency_next.md` |
| Feature log | Change-review and close-out writers | User when asked | `scripts/feature_log/feature_log.py`; `commands/feature_log.md` |
| `/next` | User through showrunner | One selected review item | `commands/next.md`; change, proposal, efficiency, and question scripts |
| Unit add-ons | Unit director | Showrunner in a production | `commands/unit/add_ons.md`; `commands/plan/phase_review.md` |

`/forest_and_trees_alert` currently uses native Claude discovery. The roster and broadcast scripts cover Codex too. This records the current command; it does not extend it. Source: `commands/forest_and_trees_alert.md`, DiscoverRecipients; `docs/as-built/session-roster.md`, Instructions.

### Ownership beside sdlc

| Unit | Owns | Source |
| --- | --- | --- |
| hierarchies-unit | Structure, priority ownership, work admission, capacity sharing, user contact, and measurement | Unit plan, Scope beside sdlc |
| sdlc roles-unit | Role duties and names | sdlc production, Unit scopes |
| sdlc guild-process-unit | Process changes for the guilds and the lasting designer and reviewer roles | sdlc production, Units |
| sdlc phases-unit | Phase process, design and document preparation | sdlc production, Unit scopes |
| sdlc fix-rounds-unit | Repair-round study and proposed changes | sdlc production, Unit scopes |

## Baseline

Original command: `~/.claude/scripts/lib/py scripts/hierarchies/measure.py baseline`.

Exact-window re-run command: `~/.claude/scripts/lib/py scripts/hierarchies/measure.py baseline --since 2026-10-13T17:24 --until 2026-10-20T17:24`. The date-time form came in Phase 2; its end is excluded.

Run `20261020T172406`: 2026-10-20 17:24 EDT; duration about 22.6 s. Window: 2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT; end excluded. The original capacity and waiting snapshots below are at 2026-10-20 17:24 EDT. The corrected rank snapshot is at 2026-10-20 22:09 EDT; it does not recreate the earlier live state. Source: the impl writer's full output in `/tmp/claude/unit/62f4d2b7-0062-437a-b355-57bb09bfbc9e/phases/1/impl_baseline.txt`. The lines below sum its printed rows. Build, test, and token shares cover natedev only; repository labels are those in the output.

The Phase 2 flow and rank rows below come from the exact-window re-run `20261020T220955`, saved at `/tmp/claude/unit/62f4d2b7-0062-437a-b355-57bb09bfbc9e/phases/2/baseline_fold.txt`. Capacity, resource, and attention rows retain the original report. A re-run of the same window reads different resource values because source stores take later records with earlier stamps. Keep the old resource values for this comparison; do not read the change between reports as a hierarchy effect. The saved re-run holds the later values. The printed old bounds omit seconds; the reports cannot assign each difference to a later write or an edge.

The original `goal 1 issues closed` row counted 7, n=7. The current source has no usable closure date for three cargo-port issues: `phase state lifecycle enum`, `scope map owns its two way invariant`, and `worktree label takes its entry not an index` each have `date_closed: [[a]]`. They cannot be placed in the window now. This source change does not show that the old count was false. The corrected row counts only the dated records now available; it excludes declines and separates projects. A closure naming a commit is a text check, not proof of target-branch delivery.

- Snapshot: Hana has 11/41 units (26.8%) and 66/187 workers (35.3%). Workers cover natedev only.
- Hana build/check row: 14,897.2/18,571.3 recorded minutes (80.2%); n=12439 Hana steps.
- Hana test row: 16,194.6/19,687.5 recorded minutes (82.3%); n=2704 Hana steps.
- Hana token row: 22,585,130,240/54,589,799,547 tokens (41.4%); n=134547 Hana records; includes cache tokens.
- In the window, natedev sent 64 pushes; 16 followed failed reviews (n=64 pushes).
- Snapshot: 315 changes, 161 proposals, 121 efficiency items, and 1 question await the user (n=598 items).
- Four wait kinds, issue activation stamps, and question answer stamps are not recorded (n=0 each).
- Snapshot: 51/187 workers have no recorded cwd; all 51 have no running director. Shares above use the printed rows.

```text
Capacity on Hana
Measure                                                                                                                                      Value                        n       Window                                                       Source
units / build-followups / /home/natepiano/.claude / other                                                                                    18                           18      snapshot 2026-10-20 17:24 EDT                                showrunners + units_table production_units loader
units / cargo-liner-style / /home/natepiano/rust/cargo-liner / other                                                                         4                            4       snapshot 2026-10-20 17:24 EDT                                showrunners + units_table production_units loader
units / remove-running-section / /home/natepiano/rust/cargo-liner / other                                                                    4                            4       snapshot 2026-10-20 17:24 EDT                                showrunners + units_table production_units loader
units / sdlc / /home/natepiano/.claude / other                                                                                               4                            4       snapshot 2026-10-20 17:24 EDT                                showrunners + units_table production_units loader
units / tool-based-ui / /home/natepiano/rust/hana / Hana                                                                                     11                           11      snapshot 2026-10-20 17:24 EDT                                showrunners + units_table production_units loader
workers / worktree not recorded / scope not recorded                                                                                         51; director not running 51  51      snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-berth / other                                                                                     5; director not running 0    5       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-experiments / other                                                                               1; director not running 0    1       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-handler / other                                                                                   2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-style-modules / other                                                                             3; director not running 0    3       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-style-names / other                                                                               2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-style-types / other                                                                               2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-style-values / other                                                                              2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/cargo-liner-tile / other                                                                                      8; director not running 0    8       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/hana_catalyst-cables / Hana                                                                                   3; director not running 0    3       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/hana_catalyst-inspectors / Hana                                                                               12; director not running 0   12      snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/hana_catalyst-lfo / Hana                                                                                      2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/hana_catalyst-research / Hana                                                                                 5; director not running 0    5       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/tool-based-ui-demo / Hana                                                                                     5; director not running 0    5       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/tool-based-ui-frame-time / Hana                                                                               10; director not running 0   10      snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/tool-based-ui-geometry-material / Hana                                                                        12; director not running 7   12      snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/tool-based-ui-startup / Hana                                                                                  4; director not running 0    4       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/tool-based-ui-trunk / Hana                                                                                    6; director not running 2    6       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/rust/widget-enhancements / Hana                                                                                    7; director not running 3    7       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-accounts / other                                                                  5; director not running 0    5       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-hierarchies / other                                                               2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-screenshot / other                                                                4; director not running 0    4       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-sdlc-fix-rounds / other                                                           2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-sdlc-guilds / other                                                               3; director not running 0    3       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-sdlc-phases / other                                                               2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-sdlc-roles / other                                                                2; director not running 0    2       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /home/natepiano/worktrees/claude-build-followups-style / other                                                                     24; director not running 0   24      snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
workers / /tmp/claude/delegate/fc206247-76c0-4607-b268-025299592a62/phases/33/fix / other                                                    1; director not running 1    1       snapshot 2026-10-20 17:24 EDT                                codex_mesh via session roster; natedev only
build/check minutes / cargo-liner                                                                                                            2616.9                       5793    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / cargo-liner                                                                                                                   1312.9                       1476    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / ci_fix                                                                                                                 26.9                         19      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / ci_fix                                                                                                                        54.2                         4       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / clone57                                                                                                                       39.8                         2       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / current-timed                                                                                                                 7.0                          1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / fix                                                                                                                    413.7                        215     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / fix                                                                                                                           447.9                        51      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana                                                                                                                   14897.2                      12439   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana                                                                                                                          16194.6                      2704    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58b                                                                                                            20.3                         12      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58b                                                                                                                   35.7                         4       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58c                                                                                                            14.1                         12      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58c                                                                                                                   16.9                         2       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58f                                                                                                            0.1                          1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58f                                                                                                                   10.7                         1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58i                                                                                                            8.9                          8       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58i                                                                                                                   39.0                         3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58m                                                                                                            18.7                         23      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58m                                                                                                                   69.3                         10      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58t                                                                                                            25.5                         13      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58t                                                                                                                   34.0                         1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58u                                                                                                            1.8                          1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58u                                                                                                                   24.9                         1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_iso58v                                                                                                            1.8                          3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_iso58v                                                                                                                   16.4                         3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / hana_trial34                                                                                                           0.0                          2       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / hana_trial34                                                                                                                  19.7                         2       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / handler_head                                                                                                           0.0                          2       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / handler_head                                                                                                                  1.0                          3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / lfo-gate31-fix                                                                                                         413.8                        324     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / lfo-gate31-fix                                                                                                                962.5                        108     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / lfo-gate31-trunk-compare-fix2                                                                                          0.0                          3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / lfo-gate31-trunk-compare-fix2                                                                                                 34.2                         3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / lfo-readout-fix                                                                                                        21.5                         17      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / lfo-readout-fix                                                                                                               77.0                         10      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / mergeclone                                                                                                             71.2                         45      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / mergeclone                                                                                                                    117.5                        15      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / old-word-metric                                                                                                               12.0                         1       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / p2fix                                                                                                                  18.5                         10      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / p2fix                                                                                                                         25.9                         3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
build/check minutes / repository not recorded                                                                                                0.4                          55      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
test minutes / repository not recorded                                                                                                       134.4                        69      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  buildlog index steps; natedev only; by start time
tokens / /etc/nixos                                                                                                                          1,669,687,637                10384   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/.claude                                                                                                             5,090,445,695                36967   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/rust/bevy_brp                                                                                                       1,988,070                    31      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/rust/cargo-liner                                                                                                    6,226,502,210                41263   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/rust/hana                                                                                                           22,585,130,240               134547  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/rust/hanadocs                                                                                                       90,680,346                   612     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/rust/wesl-order-repro                                                                                               63,286                       3       2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /home/natepiano/scratch/herdrcheck                                                                                                  2,147,724                    43      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /tmp/claude-1000/-home-natepiano-rust-tool-based-ui-geometry-material/11a8e01e-8f31-4338-a81d-007a1289a078/scratchpad/trial_widget  4,182,317                    52      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /tmp/claude/delegate/fc206247-76c0-4607-b268-025299592a62/phases/32/fix                                                             6,780,268                    62      2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /tmp/claude/delegate/fc206247-76c0-4607-b268-025299592a62/phases/33/fix                                                             26,453,132                   254     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /tmp/claude/lfo-gate31-fix                                                                                                          163,284,744                  1202    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / /tmp/claude/lfo-readout-fix                                                                                                         65,204,929                   356     2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only
tokens / cwd has no repository                                                                                                               18,657,248,949               125338  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  usage ledger; input + cache writes + cache reads + output; natedev only

Flow to Hana main (recorded phase/issue completion; local-ref merge commits)
Measure                                                     Value                                                           n   Window                                                       Source
Hana phases done                                            89                                                              89  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only
Hana phase time                                             median 634.1 min; p90 2183.5 min                                89  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only
Hana wait / user                                            not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / showrunner                                      not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / gate                                            not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / idle                                            not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
goal 1 issues moved to active                               not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_active; stage/date_modified do not record a transition
goal 1 issues closed                                        4                                                               4   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_closed; date-only precision; reason declined excluded
goal 1 active to closed                                     not recorded                                                    0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_active + date_closed; date-only precision
goal 1 issues closed / cargo-port / other                   not recorded (0 dated in window; 3 closure dates not recorded)  0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs project + date_closed; date-only precision; reason declined excluded
goal 1 issues closed / hana / Hana                          2                                                               2   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs project + date_closed; date-only precision; reason declined excluded
goal 1 issues closed / hanadocs / other                     2                                                               2   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs project + date_closed; date-only precision; reason declined excluded
goal 1 Hana-project issues closed                           2                                                               2   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs project + date_closed; date-only precision; reason declined excluded
goal 1 declined closures excluded                           0                                                               0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs reason declined + date_closed; date-only precision
goal 1 Hana closures naming a merge commit                  0                                                               2   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs reason naming a merge commit or merge_commit; text only, not checked against Git
Hana closure merge commit / arrange jacks widget            not recorded                                                    1   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs reason naming a merge commit or merge_commit; text only, not checked against Git
Hana closure merge commit / shortcuts and inspect commands  not recorded                                                    1   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs reason naming a merge commit or merge_commit; text only, not checked against Git
Hana merges / origin/init/catalyst                          72                                                              72  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-13                                    3                                                               3   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-14                                    23                                                              23  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-15                                    11                                                              11  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-16                                    4                                                               4   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-17                                    10                                                              10  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-18                                    12                                                              12  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-19                                    5                                                               5   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch
Hana merges / 2026-10-20                                    4                                                               4   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  git log --first-parent --merges origin/init/catalyst; local ref; commit time; no fetch

User attention
Measure                             Value                       n    Window                                                       Source
phone pushes / 2026-10-13           8; after failed review 0    8    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-13 / agent       48                          48   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-13 / automation  78                          78   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-13 / user-bound  8                           8    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-14           3; after failed review 0    3    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-14 / agent       151                         151  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-14 / automation  169                         169  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-14 / user-bound  5                           5    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-15           2; after failed review 1    2    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-15 / agent       503                         503  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-15 / automation  60                          60   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-15 / user-bound  2                           2    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-16           5; after failed review 1    5    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-16 / agent       808                         808  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-16 / automation  157                         157  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-16 / user-bound  5                           5    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-17           3; after failed review 1    3    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-17 / agent       501                         501  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-17 / automation  64                          64   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-17 / user-bound  3                           3    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-18           17; after failed review 2   17   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-18 / agent       636                         636  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-18 / automation  172                         172  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-18 / user-bound  17                          17   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-19           4; after failed review 1    4    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-19 / agent       826                         826  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-19 / automation  166                         166  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-19 / user-bound  4                           4    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
phone pushes / 2026-10-20           22; after failed review 10  22   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  pushover + review log; natedev only
messages / 2026-10-20 / agent       515                         515  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-20 / automation  21                          21   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
messages / 2026-10-20 / user-bound  35                          35   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  message log; kind/route, else sender-name heuristic; natedev only
waiting / changes                   315; oldest 11.7 days       315  snapshot 2026-10-20 17:24 EDT                                change-review / proposal issues / nightly ledger; natedev only
waiting / proposals                 161; oldest 11.7 days       161  snapshot 2026-10-20 17:24 EDT                                change-review / proposal issues / nightly ledger; natedev only
waiting / efficiency items          121; oldest 22.7 days       121  snapshot 2026-10-20 17:24 EDT                                change-review / proposal issues / nightly ledger; natedev only
waiting / questions                 1; oldest 0.1 days          1    snapshot 2026-10-20 17:24 EDT                                showrunner/outstanding/*.json; question since stamps; natedev only
questions answered                  not recorded                0    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  showrunner/outstanding: no answer history or answered_at; producer must record answers; natedev only
question answer time                not recorded                0    2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  showrunner/outstanding: no answer history or answered_at; producer must record answers; natedev only

Rank coverage
Measure                                  Value            n   Window                         Source
top 20 goal 1 open issues with a unit    2/20             20  snapshot 2026-10-20 22:09 EDT  backlog-rankings + open issue links; unit has a running phase or live session
live units naming an open issue          8/44             44  snapshot 2026-10-20 22:09 EDT  open production Issue link or open issue unit + showrunner; running phase or live session
top 20 goal 1 open issues covered score  140/604 (23.2%)  20  snapshot 2026-10-20 22:09 EDT  backlog-rankings score; covered score / all top 20 scores; running phase or live session
```


### Prior-week Hana merges

Source: the measure writer's read-only check in `/tmp/claude/unit/62f4d2b7-0062-437a-b355-57bb09bfbc9e/phases/2/impl_summary_test.txt`. It uses `read_hana_merges(hana_repository([]))`, then `merge_flow` with `window_bounds('2026-10-06T17:24', '2026-10-13T17:24', now)`. Repository: `/home/natepiano/rust/hana`. Git command: `git log --first-parent --merges --format=%H%x09%cI origin/init/catalyst`. Both weeks count first-parent merges on the local ref by commit time, with no fetch. A local ref can be behind the remote.

| Measure | Value | n | Window | Source |
| --- | --- | --- | --- | --- |
| Hana merges / origin/init/catalyst | 324 | 324 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-06 | 14 | 14 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-07 | 55 | 55 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-08 | 89 | 89 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-09 | 33 | 33 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-10 | 36 | 36 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-11 | 26 | 26 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-12 | 40 | 40 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |
| Hana merges / 2026-10-13 | 31 | 31 | 2026-10-06 17:24 EDT to 2026-10-13 17:24 EDT (end excluded) | Local-ref first-parent merge check above |

For the current window, **Flow to Hana main**, `Hana merges / origin/init/catalyst` is 72, n=72. Its daily rows for 2026-10-13 through 2026-10-20 are 3, 23, 11, 4, 10, 12, 5, 4; each row's n equals its count. The first and last days in both weekly lists are partial. The earlier review's current-week daily list is superseded by these checked rows. The fall from 324 to 72 does not explain its cause or establish a hierarchy effect.

In **Flow to Hana main**, `goal 1 Hana-project issues closed` is 2, n=2, and `goal 1 Hana closures naming a merge commit` is 0, n=2. Plan phases deliver most Hana work without an issue link: 72 merges against 2 Hana-project closures in this window. These are different records, not a matched delivery ratio. Receipts and scope checks must connect them before a verified functionality rate is known.

## Candidates

The user reviewed this design on 2026-10-28 (sdlc production, Review decisions HI1–HI11, `origin/sdlc 214bc72e`). A runs as an experiment (HI1). B does not start as written (HI10). C is dropped (HI11). The text below records each candidate's state after that review.

### Rules shared by all candidates

Top-level showrunners stay peers: build-followups, remove-running-section, tool-based-ui and sdlc. guilds is a child of sdlc (2026-10-28). The live showrunner tree is the record of child edges; a production doc that disagrees with it is fixed by its owner (HI5). Propose no new showrunner or unit. Guild heads and the per-design Astra roles follow `docs/as-built/guilds.md`.

Peers read the one global `backlog_rank`. The watcher computes scores and ranks; no workflow writes ranks directly. hana may propose re-ratings through `/prioritize`, with user approval; it does not maintain a separate backlog or command peers. Goal order gives a soft score bonus, not strict lanes. Sources: `commands/prioritize.md:51` and `~/rust/hanadocs/prioritization goals.md:9`. Demo and herdr keep their standing priority under C23. User-ordered work stays admitted under C05.

All candidates share the current slots and memory brake, try a free Mac for supported tests, respect holds, and accept a full supported Mac pass. natedev keeps the role-to-account map and account authority. The account count stays fixed; a showrunner brings a proposal with numbers when records show that one more account ships more Hana work, and cost per phase alone is no case (C55). Workers live as long as their unit director, across runs, and are reused by name (C56). Each showrunner starts its own workers; there is no central worker allocation (C57).

Every rule this design adds is a trial with a measure and a review point, and it is dropped if it does not help (C53). Every new piece of work in build-followups, sdlc and cargo names its link to Hana, direct or indirect; no measured Hana cost and no fixed share (C54).

Success means Hana functionality delivered per week, held to ergonomics, beauty, and a robust sdlc (C51). Count Hana-project closures, excluding declines, with receipts on the target branch, alongside Hana merges on `origin/init/catalyst`. Keep supplier closures separate. A Hana issue has project `hana`, `hana_diegetic`, `hana_conduit`, or `hana_liminal` and concerns code in the Hana repository; `backlog_goal: 1 - Ship Hana` alone does not establish that. Plan phases deliver most Hana work without an issue link: Baseline **Flow to Hana main**, `Hana merges / origin/init/catalyst` has 72, n=72, against `goal 1 Hana-project issues closed` 2, n=2; `goal 1 Hana closures naming a merge commit` is 0, n=2. Neither a merge nor a closed issue has a fixed amount of functionality.

### A — Today's tree, repaired, run as an experiment (chosen, HI1)

**Roles and priority.** Keep current production scopes, directors, and workers. Each showrunner keeps its local selection duty within approved scope and reads the global rank as an input. No new cross-production owner.

**Admission.** A new unit, a later unit run, added work and a standing board pull in build-followups, sdlc and cargo name their link to Hana, direct or indirect, in the existing work note (C54, HI8). Example of an indirect link, in the user's words: "we use natedev as a test bed for ideas for hana". User orders, demo and herdr work and small fixes start at once. No peer checks the link; a later review checks which links helped Hana.

**Capacity and contact.** Use current shared capacity and contact paths. Local showrunners coordinate scarce requests peer to peer. natedev keeps account control. The approved repairs are Steps 1, 2 and 4.

**Review items.** No daily review budget. Items for the user's review are sorted by importance through the Jev-driven score in `scripts/issue_score`, so each review gives the most decision value (HI3). No change closes without the user's check.

### B — Shared rank order and a Hana-cost test (not started, HI10)

B added a shared admission order across peers and a measured Hana-cost test for build-followups and sdlc. HI2 ruled out a measured Hana cost, so B's added rule does not start as written. If the Step 2 wait records show non-Hana work slowing Hana, hierarchies-unit brings a new proposal.

### C — Capacity-led (dropped, HI11)

C made natedev the cross-production owner of slots and new workers. The user dropped it on 2026-10-28: "i see no reason for natedev to hand out workers or for their to be a centralized worker mechanism". C is not a later option.

## Comparison

Use the historical Baseline window: 2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT, end excluded, natedev. Capacity and pending-attention rows keep the 17:24 EDT snapshot; rank rows use the separate 22:09 EDT snapshot. Later snapshots cannot recreate earlier live ownership. Resource rows keep the original values despite later source writes. Each cell below scores the added mechanism against common repairs. A and B both include those repairs and the Hana link rule (C54). C is dropped (HI11), so its column is removed. Expected size is unknown unless a row states an explicit record-completeness or correctness target. The former percentage and phase-count forecasts had no measured basis and are removed. Report window, n, scope, and missing records for each later result.

All candidates keep C02–C03, C05–C32, C34–C44, C46–C50 under the shared rules and approval guards. The table retains differences and risks. A superior over a top-level peer breaks C02/C03 and is out. Taking director account authority from natedev breaks C33 and is out. No core candidate adopts age closure or a new tree edge.

| Measure or constraint; Baseline table and row | A — repaired, run as an experiment (chosen) | B — added shared order and cost test (not started, HI10) |
| --- | --- | --- |
| Capacity: **Capacity on Hana**, all `units /` rows; summary Hana 11/41, Hana row n=11. | No count target; retain approved scope. | No count target; redirect future selection only. Supplier units remain outside Hana repositories. |
| Capacity: same table, all `workers /` rows; summary Hana 66/187; `workers / worktree not recorded / scope not recorded` 51, director not running 51, n=51. | Target zero unexplained entries after ownership checks, not termination of all 51. | Same cleanup; no extra worker target. |
| Resource use: same table, `build/check minutes / hana` 14897.2, n=12439, and all repository rows. | Avoid confirmed duplicate runs; size unknown. | Same repairs. Tool builds occur in other repositories. Shared-load relief can reduce Hana waiting, not directly its build runtime. |
| Resource use: same table, `test minutes / hana` 16194.6, n=2704, and all repository rows. | Preserve gates; duplicate-run saving unknown. | Same as A until a supplier change proves a matched saving. |
| Resource use: same table, `tokens / /home/natepiano/rust/hana` 22,585,130,240, n=134547; all repository rows. | Completed-run cleanup may lower traffic; size unknown. | Added cost notes consume tokens; avoided supplier work may offset them. No per-issue saving is known. |
| Phase flow: **Flow to Hana main**, `Hana phases done` 89, n=89. Daily and weekly phase history is not measured by this report. | Observe only; phase size changes the count. | Same target as A: no separate count target or proven mechanism. |
| Phase flow: same table, `Hana phase time` median 634.1 min, p90 2183.5 min, n=89. | Direction and size unknown. | Direction and size unknown until waits show a cause. |
| Phase flow: same table, four `Hana wait /` rows, each not recorded, n=0. | Record each kind before claims. | Need quota, memory, gate, and user waits plus same-hour load. |
| Issue flow: same table, `goal 1 issues moved to active` and `goal 1 active to closed`, not recorded, n=0 each. | Add activation history; no duration target. | Same missing records; no faster activation claim. |
| Issue flow: same table, `goal 1 issues closed` 4, n=4; `goal 1 Hana-project issues closed` 2, n=2; `goal 1 issues closed / hanadocs / other` 2, n=2; cargo-port dates not recorded (3 missing, n=0); `goal 1 declined closures excluded` 0, n=0; `goal 1 Hana closures naming a merge commit` 0, n=2. | Hana-only deliveries with receipts; supplier results separate. No numerical gain claimed. | Compare only the added rule; effect may be zero or negative. No comparison of all-project closures with Hana deliveries. |
| Issue flow: same table, `Hana merges / origin/init/catalyst` 72, n=72; daily rows 3, 23, 11, 4, 10, 12, 5, 4, n=count each. **Prior-week Hana merges**, same total row 324, n=324, in its preceding seven-day window. | Main outcome alongside Hana-project receipt-backed closures. | Count plan-driven work without requiring issue links; assess scope and quality. |
| Phase flow: quota refusals and Hana refusal minutes; not measured by this report. | Record actual refusals; exclude probe subjects and quota readings. | Benefit requires waits to follow non-Hana use on the same role account. |
| Phase flow: memory-brake wait minutes; not measured by this report. | Record memory/slot brakes separately from merge gates. | Benefit requires same-hour non-Hana memory load to precede waits. |
| Admission overhead and time to first effect; not measured by this report. | Link notes in build-followups, sdlc and cargo add work at the next boundary; no effect before then. | Extra notes for build-followups/sdlc and shared choices; size unknown. Time all checks, deferrals, and time to first affected admission. |
| Attention: **User attention**, all `phone pushes /` rows; summary 64, after failed review 16, n=64. | Zero unauthorized sends target; approved priority-2 and registered texts remain. The 16 are not 16 proven faults. | Same correctness target, with common repairs scored equally. |
| Attention: same table, all agent, automation, and user-bound daily message rows. | End stale exchanges and unrequested timers; size unknown. | Same cleanup; extra coordination may offset savings. |
| Pending attention: same table, `waiting / changes` 315, oldest 11.7 days, n=315. | No removal without a verdict; age may rise. | Same; no admission effect is credited as queue drain. |
| Pending attention: same table, `waiting / proposals` 161, oldest 11.7 days, n=161. | Keep queue; no reduction target. | Optional arrivals may fall, size unknown. |
| Pending attention: same table, `waiting / efficiency items` 121, oldest 22.7 days, n=121. | Keep requested one-at-a-time rule. | Same; no count or age target. |
| Pending attention: same table, `waiting / questions` 1, oldest 0.1 days, n=1. | Ask once, then count. | Added policy questions can raise it. |
| Pending attention: same table, `questions answered` and `question answer time`, each not recorded, n=0. | Add answer history; no speed claim. | Same; cannot promise faster user decisions. |
| Rank coverage: **Rank coverage**, `top 20 goal 1 open issues with a unit` 2/20, n=20; `top 20 goal 1 open issues covered score` 140/604 (23.2%), n=20; snapshot 2026-10-20 22:09 EDT, open issues with live ownership. | Descriptive snapshot; validate current phase ownership. | Follow a fixed starting set through completion. Top 20 are Hana-project issues only Hana's own units can take; suppliers do not fill that gap. |
| Rank coverage: same table, `live units naming an open issue` 8/44, n=44; snapshot 2026-10-20 22:09 EDT. | Validate the issue remains open and owner live. | Same; completed covered work can lower the next moving snapshot. Never a success gate alone. |
| C01 — fastest Hana delivery | Kept: A chosen as an experiment (HI1); no measured B advantage exists. | Kept with risk: extra admission work can delay useful sdlc; test only after evidence. |
| C04 — user decides redesign | Kept: the user approved Steps 1, 2 and 4 (HI6, HI7, HI9). | Not started (HI10); a new proposal goes to the user. |
| C33 — account authority | Kept: natedev remains owner. | Kept: no rank rule overrides map or move queue. |
| C45 — product priority | Kept: no deadline or cut order. | Kept with risk: shared order is a priority duty; user orders and C23 override it. |
| C51 — functionality and robust sdlc | Kept: protect ergonomics, beauty, and robust sdlc. | Kept with risk: cost claims must not suppress robust sdlc; compare functionality, not issue volume. |
| C52 — cargo supplies Hana | Kept: cargo work names its Hana link (HI2, HI8). | Kept: same link rule, not an added B benefit. |

Daily/weekly phase history, daily tokens, and account-refusal records are outside the fold report’s changes. They are not measured here; this does not prove that their source records are absent. Do not use unverified review counts as replacement Baseline rows.

The judge found no established B advantage after Q2: its estimated first-trial difference is about zero, and the small possible steady benefit is unproved. Those are review judgments, not Baseline measurements. A and B can both be limited by the user's review loop, Hana's own merge gate, or Hana's own builds. Do not infer capacity loss from token share or accounts reaching a limit. Record an actual Hana refusal and its duration. No reviewed account-out event proves a Hana refusal; probe subjects must be removed before counts are used.

## Recommendation

The user chose A, as an experiment (HI1, 2026-10-28: "i think of this all as experimental and you should treat it as such").
Repair today's tree with the approved Steps 1, 2 and 4. Each is a trial with its measure and a review point; drop a step that does not help.
Every new piece of work in build-followups, sdlc and cargo names its link to Hana (HI2). Record the links; the Step 5 review checks which ones helped.
Measure Hana functionality under C51: Baseline **Flow to Hana main**, `Hana merges / origin/init/catalyst` 72, n=72; `goal 1 Hana-project issues closed` 2, n=2; `goal 1 Hana closures naming a merge commit` 0, n=2. Step 2 adds the receipts that this count lacks.
B does not start (HI10). If four weeks of Step 2 wait records show non-Hana work slowing Hana, hierarchies-unit brings a new proposal.
C is dropped (HI11).

## Steps

Each step is a trial (C53): it names its measure, its review point and its link to Hana (C54). Every changed duty goes through the showrunners to sdlc's roles-unit; phases-unit owns phase contract text. No step proposes a new showrunner, unit, promotion, or edge. Report the window, covered n, missing n, and scope with every later measure.

1. **Push-authorization audit. Approved (HI6).** Each showrunner checks which of its sends to the user pass a failed text check, fixes only paths confirmed as not approved, and records which approved exception each send uses. Sources: `scripts/notify/review.py`, `scripts/notify/pushover.py` (priority-2 and registered-text exceptions at 316-319), `scripts/message/send.py`, and the admission paths `commands/showrunner/add_unit.md` and `scripts/production/add_unit.py`. A change to an exception is its own question for the user. Measure: Baseline **User attention**, phone-push summary 64 pushes, 16 after failed review, n=64; target zero unauthorized sends, every exception named. **Undo:** revert a faulty patch; keep failed-review records and the approved exception behavior. **Hana link:** indirect; each unauthorized push takes the user's attention from Hana decisions.

2. **Wait records and merge receipts. Approved as a 4-week experiment (HI7).** Each phase records each wait and its cause; each merge leaves a receipt (project, issue, commit, branch, time) that the receiving showrunner checks. phases-unit builds the records; roles-unit writes the duties; no gate changes. Wait kinds: WaitingForQuotaRefusal, WaitingForMemoryOrSlot, WaitingForMergeGate, WaitingForUser, WaitingForSupplier, ReadyWithoutCapacity; missing history is HistoryNotRecorded, not zero; a merge with no receipt is ReceiptMissing. hierarchies-unit adds read-only readers to `scripts/hierarchies/measure.py`. Measure: Baseline **Flow to Hana main**, four `Hana wait /` rows not recorded, n=0 each; `goal 1 Hana closures naming a merge commit` 0, n=2. Target: an explicit record or an explicit missing mark for each new wait and merge. Review point: four weeks after the first records land. **Undo:** stop the new writers, keep their history. **Hana link:** direct; the records show which waits slow Hana phases.

3. **Hana link note at admission. Decided by HI2 and HI8; roles-unit writes the duty.** A new unit, a later run, added work and a board pull in build-followups, sdlc and cargo write their link to Hana, direct or indirect, in the existing work note. User orders, demo and herdr work and small fixes start at once. No peer check. Measure: the share of new work with a recorded link; Step 5 reads the links beside Hana merges and closures. **Undo:** drop the note at the next boundary. **Hana link:** the step records the links themselves.

4. **Run-end ownership check. Approved with the user's rule (HI9).** Workers live as long as their unit director, across runs, and are reused by name. The check ends only workers whose unit director is gone or whose owner cannot be found; active workers and pooled reviewers stay. Each showrunner removes timers nobody asked for. Sources: `scripts/agents/codex_mesh.py`, `scripts/direct/end_session.sh`, `scripts/message/notifier.sh`; the contract text in `commands/unit/direct.md` (<LongLivedWorkers/>) goes to phases-unit, the duty to roles-unit. Measure: Baseline **Capacity on Hana**, `workers / worktree not recorded / scope not recorded` 51, director not running 51, n=51; target zero workers without a known owner. **Undo:** revert the check; resume an ended thread by name if its director was live. **Hana link:** indirect; workers with no owner hold quota and memory on the accounts and machine Hana shares.

   **Step 4 result (Phase 5, 2026-10-28 21:32 EDT, natedev snapshot).** `scripts/hierarchies/worker_owners.py` reads the live roster, the run folders and the daemon threads. `report` names a class for each worker: OwnedLive 185, PooledReviewer 141, DirectorGone 0, OwnerUnknown 0; n=326, no source problems. The Baseline had 51 workers with no known owner (n=51); after recovery the count is 0, which meets the target. `recover` saves owners it finds from launcher files and director messages, and keeps saved owners when their first evidence goes away. `end` previews by default. An applied end acts only on the selected class (DirectorGone or OwnerUnknown), keeps attached, already ended and unrecorded workers, and the first run ends no worker. OwnerUnknown is ended only after recovery fails. Each end sends the expected thread id and an idle-only guard; until models-unit's `codex_mesh.py end` accepts those flags and checks them under its lock, an end is refused and nothing is ended. Fifteen timer registrations have no recorded request; no timer changed. Review point: re-run `report` two weeks after the merge.

5. **Four-week review.** After four weeks of Step 2 records: report which waits slow Hana (HI7), which Hana links helped (HI2), and keep or drop each trial step (HI1). Set quota and memory waits beside same-hour non-Hana load on the same role account and natedev memory. If non-Hana work slows Hana, bring a new proposal (HI10). Roles: hierarchies-unit measures read-only; showrunners give the user one decision. Measure: Baseline **Flow to Hana main** rows above, against the same rows for the four-week window. **Hana link:** direct; the review decides what stays by its effect on Hana.

## Questions for the user

All questions are answered. Q1 and Q2 came through the showrunner on 2026-10-20; Q3–Q12 are the user's review on 2026-10-28 (sdlc production, Review decisions, HI2–HI11).

Q1. **Answered.** Does Hana have a ship date or first public release date, and what must that release include?

> no date - we are focused on simply driving functionality at the fastest rate under the constraints of good ergonomics, beauty, and a robust sdlc

Q2. **Answered.** Is cargo-liner, including cargo-port, cargo-tile, and cargo-berth, a separate product or a supplier of tools to Hana?

> they are tools for hana - we have published a couple of them but it's not super important to me that we publish more - as they are becoming more specialized for our work flow

| Q | Question | Decision |
| --- | --- | --- |
| Q3 | Must build-followups and sdlc cut a measured Hana cost, or get a fixed share? | HI2: neither. Every new piece of work in build-followups, sdlc and cargo names its link to Hana, direct or indirect. The user: "all of this work is at the very least transitively supportive... we use natedev as a test bed for ideas for hana". Record the links; review later which helped. |
| Q4 | The user's daily review budget; may old unreviewed changes close? | HI3: no daily budget; the Jev-driven score (`scripts/issue_score`) sorts every item for review by importance. No change closes without the user's check. |
| Q5 | Is the account count fixed? | HI4: fixed, with proposals when records show one more account ships more Hana work; cost per phase alone is no case. |
| Q6 | Where does each new showrunner and unit sit in the tree? | HI5: dropped. The live showrunner tree is the record. |
| Q7 | Push-authorization audit | HI6: approved. Step 1. |
| Q8 | Wait records and merge receipts | HI7: approved as a 4-week experiment. Step 2. |
| Q9 | Cargo Hana-cost note | HI8: settled by HI2; a link, not a cost. Step 3. |
| Q10 | Run-end ownership check | HI9: approved with the user's rule: "as we name workers for a unit - we reuse them - unit directors need to rely on building up context and most of our unit directors are very long lived, so must most of their workers be long lived". Step 4. |
| Q11 | Trial B under both conditions | HI10: settled by HI2; B does not start as written. |
| Q12 | Defer C | HI11: dropped; no central worker allocation. |
