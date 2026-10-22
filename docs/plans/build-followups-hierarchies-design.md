# Hierarchy for fast Hana delivery

> **Status: DESIGN — phase 1 done: constraints, map and baseline; questions out to the user.**

This document records constraints, role contracts, channels, and a measured baseline. Phase 1 changes no live process.

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
| sdlc | `docs/plans/sdlc-production.md` on branch `build-followups-sdlc` |

| ID | Kind | Constraint and source |
| --- | --- | --- |
| C01 | user | Find the hierarchy that delivers Hana fastest within the user's constraints. Fewer layers alone do not define success. Source: user, 2026-10-20, [unit plan, Source and Decisions](build-followups-hierarchies.md). |
| C02 | user | Keep top-level showrunners as peers. The user wants a flat organization. Source: user, 2026-10-20, `nixos/flat-organization.md`; sdlc, Production rules. |
| C03 | user | The guild heads are not sdlc units. As built, the four heads (`designers`, `reviewers`, `showrunners`, `units`) are unit directors under the top-level `guilds` production, a peer of sdlc (user G1b, `docs/plans/sdlc-production.md`; `docs/as-built/guilds.md`). |
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
| C22 | user | Rank strategic goals as Ship Hana, Find Collaborators, Seek Investors, then Business Viability. Change goals only with explicit approval. Source: `~/rust/hanadocs/prioritization goals.md`, Current goals and Maintenance. |
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
| C40 | user | Keep Codex workers across phases and repair rounds. Reuse their threads after account moves. End run workers at run end. Source: user, 2026-10-10 and 2026-10-16, `config/workers-persist-across-phases.md` and `config/resume-codex-seats-after-move.md`; [production contract, Worker](../production_format.md). |
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
| Guilds showrunner | `docs/plans/guilds-production.md` | Runs the top-level `guilds` production and its four head units | User; peer of other top-level showrunners |
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
| sdlc | sdlc | None | Planned in sdlc production; already listed by tree |
| Guild heads | `designers`, `reviewers`, `showrunners`, `units` | Unit directors under top-level `guilds` | `docs/as-built/guilds.md` |
| Astra designer / reviewer | `astra-designer` / `astra-reviewer` | Held per-design roles under a unit director or showrunner | `docs/as-built/guilds.md` |

The style child is an observed edge, not approval to add more levels. The flat-organization constraint remains the target. Source: current tree; user, 2026-10-20, `nixos/flat-organization.md`.

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

Command: `~/.claude/scripts/lib/py scripts/hierarchies/measure.py baseline`.

Run `20261020T172406`: 2026-10-20 17:24 EDT; duration about 22.6 s. Window: 2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT; end excluded. Snapshot rows use the end time. Source: the impl writer's full output in `/tmp/claude/unit/62f4d2b7-0062-437a-b355-57bb09bfbc9e/phases/1/impl_baseline.txt`. The lines below sum its printed rows. Build, test, and token shares cover natedev only; repository labels are those in the output.

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

Flow to Hana main (recorded phase/issue completion; merges not recorded)
Measure                        Value                             n   Window                                                       Source
Hana phases done               89                                89  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only
Hana phase time                median 634.1 min; p90 2183.5 min  89  2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history run records; elapsed wall time; no merge-to-main receipt; natedev only
Hana wait / user               not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / showrunner         not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / gate               not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
Hana wait / idle               not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  progress_history.py must record wait kind and start/end; activity labels do not prove waits
goal 1 issues moved to active  not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_active; stage/date_modified do not record a transition
goal 1 issues closed           7                                 7   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_closed; date-only precision
goal 1 active to closed        not recorded                      0   2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)  Hanadocs date_active + date_closed; date-only precision

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
Measure                                Value  n   Window                         Source
top 20 goal 1 open issues with a unit  2/20   20  snapshot 2026-10-20 17:24 EDT  backlog-rankings + Hanadocs unit frontmatter; assignment does not prove a running unit
live units naming an open issue        9/41   41  snapshot 2026-10-20 17:24 EDT  production Issue column or open issue unit + showrunner frontmatter
```

## Questions for the user

These are requests for user decisions. Each unit recommendation is a proposal; it changes no current rule.

Q1. Does Hana have a ship date or first public release date, and what must that release include? Unit recommendation: name the date and release scope, or say no date. With no date, improve throughput in goal 1 rank order.

Q2. Is cargo-liner, including cargo-port, cargo-tile, and cargo-berth, a separate product or a supplier of tools to Hana? Unit recommendation: supplier; admit its work against a measured Hana cost.

Q3. Must build-followups and sdlc cut a measured Hana cost, or do they have a guaranteed share of capacity? Unit recommendation: require a measured Hana cost; reserve a fixed small share for broken-tool fixes.

Q4. How much daily time can you give reviews and questions, and may an old unreviewed change close without your check? Unit recommendation: name a daily budget. Close unreviewed changes older than 7 days as accepted, only after you approve this new rule.

Q5. Is the number of Claude and Codex accounts fixed, or may it grow when the measures show a benefit? Unit recommendation: keep it fixed until the measures show a cost per Hana phase.

Source check: Q1 uses `~/rust/hanadocs/prioritization goals.md` and `hana/units-do-not-carry-the-deadline.md`. Neither sets a release date or release scope. Q2 uses remove-running-section, Production Context and Production rules. It defines cargo work but does not settle its product status. Q3 uses build-followups, Capacity and Production rules, and the unit plan, Phase 2 candidate seeds. They do not grant a permanent share. Q4 uses `commands/change_review.md` and `nixos/next-thing-never-auto-advances.md`. They require the user's verdict today; they set no daily budget or age rule. Q5 uses `nixos/account-moves-at-one-percent.md`. It governs account moves, not the total account count.

Dropped the proposed C23 question. `nixos/demo-and-herdr-priority.md` already keeps demo and herdr first until the user says demo is done. That order still applies; this document records no later cancellation.
