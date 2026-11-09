# Accounts and machine-local settings

The account tools separate provider identity from shared configuration and conversations.
Claude groups and Codex workers select numbered accounts through one role map.
Both machines use that map, but each machine keeps its own sign-in state.
Account moves preserve conversations and worker threads.
Git filters keep machine-local settings outside promoted configuration.

The [account guide](../accounts.md) describes commands and repairs.
This document describes the implementation and its constraints.

## Ownership and data flow

| Module | Responsibility |
| --- | --- |
| `scripts/accounts/accounts.py` | Account checks, preparation, launch selection, moves, trust, updates, and status. |
| `scripts/accounts/role_map.py` | The shared role map, its transport, cache, locks, and change history. |
| `scripts/accounts/next_moves.py` | Ordered moves, holds, positional edits, and validation. |
| `scripts/accounts/refusal_watch.py` | Refusal detection, login renewal, queued moves, and automatic group moves. |
| `scripts/accounts/refusals.py` | Provider refusals and the freshness of usage readings. |
| `scripts/accounts/moves.py` | Move outcomes, waits, assignment routing, and replay evidence. |
| `scripts/accounts/unit_watchdog.py` | Recovery from pane launch records and fresh session liveness. |
| `scripts/accounts/outage_alerts.py` | Durable alert claims for retained record-driven restart paths. |
| `scripts/whoami/agent_accounts.py` | Account identity, Codex homes, worker selection, and credit policy. |
| `scripts/whoami/note_file.py` | Numbered account notes and the shared notes-folder resolver. |
| `scripts/settings/settings_local_keys.sh` | Settings classification, local snapshots, and Git filter transformations. |
| `scripts/settings/local_key_notices.py` | Deferred notices for new machine-local keys. |
| `scripts/lib/conf.py` | Validated configuration reads and recovery. |

Shared whoami modules use canonical `scripts.whoami` imports.
Loading the same module under two names creates distinct runtime types and breaks state checks across callers.

The agent-notes job runs notes, quota alerts, pacing, refusal moves, and unit recovery as separate parts.
`scripts/maintenance/agent_notes_job.py` owns that job.
`config/agent_notes.toml` supplies its schedules and process limits.
Herdr layout saves and detection rules belong to herdr-upkeep, with `config/herdr_upkeep.toml`.
Neither job collects recovery snapshots or a desktop session note.

## Account identity and storage

Claude account names have the form `claudeN`.
Codex account names have the form `codexN`.
The number identifies an account note, not necessarily the suffix of its current Codex home.
Legacy spaced labels remain valid input, but output uses compact labels.

`~/.claude` and `~/.codex` are shared configuration and conversation stores.
They are not the selected account after cutover.
Numbered homes retain private sign-in and runtime state.
Claude numbered homes link shared commands, skills, settings, transcripts, memory, and the session registry to `~/.claude`.
Codex homes link shared configuration, instructions, skills, session files, and history to `~/.codex`.
Codex authentication, app-server state, caches, logs, and SQLite databases remain private.
SQLite companion files remain private with their database.

Shared transcripts and registry paths let a conversation resume under another account.
The account homes use links rather than Git worktrees, so commands and hooks use the same promoted scripts.
Account checks detect broken or replaced shared links.
Preparation repairs safe shared links before launch.
It refuses an unknown conflicting file rather than deleting private data.

`NumberedAccountLabel` validates the tool and positive account number.
`NamedAccount` carries a Claude label and directory.
`CodexWorkersAccount` carries a Codex label and resolved home.
`PreparedAccount` carries the launch environment and variables to remove.
`AccountRefused` carries the reason a launch cannot proceed.
Directory checks distinguish logged-out, wrong-account, absent, unreadable, and broken-link states.
An unreadable identity is never an absent identity.

Codex identity resolution uses the account note's identity mapping.
It handles numbered homes and a legacy `~/.codex` home through the same resolver.
Status counts use that resolver too.
Setup and account checks detect duplicate sign-ins across homes.
Cutover journals preserve interrupted identity moves and support an explicit undo.
Setup waits for Codex to answer `initialize` before it reports success.
Shared session indexing can require up to twenty minutes.
The [trust guide](../accounts.md#trusted-worktrees) describes configured project roots and guarded trust-file updates.

## Role map and launch selection

Natedev owns the role map for both machines.
The map contains the Claude groups `units` and `showrunners`, the Codex `workers` account, and an optional timed Claude worker route.
Both Claude groups may select the same account.
An absent selection stays unconfigured until an explicit move selects it.
Status, launch, and preview reads never create a selection.

| State | Location |
| --- | --- |
| Claude groups | `~/.local/state/accounts/groups.json` |
| Worker selection | `~/.claude/config/accounts.json` |
| Role change history | `~/.local/state/accounts/roles.jsonl` |
| Peer role-map cache | `~/.local/state/accounts/role_map_cache.json` |
| Ordered account moves | `~/.local/state/accounts/next_moves.json` |

All role readers and writers use `role_map.py`.
A command reuses one map observation.
Mac writes use the shared SSH transport to natedev.
The authority locks the selection and history together.
A failed transaction restores the prior selection and history length.
A failed peer write never creates an independent local choice.

`RoleMap` carries selections and provenance.
`CurrentRoleMap`, `CachedAfterFetchFailure`, and `ExplicitStoredRoleMap` distinguish fresh, degraded, and stored observations.
A failed peer read may use a valid cache, with its observation time and failure shown.
Without a usable map, the operation refuses.
No reader invents a replacement account.

`account_for_group_launch()` resolves unit and showrunner launches.
Explicit account or directory arguments must agree with that selection.
A checked unit recovery can use its source account when the selected destination cannot launch.
The recovery records that fallback reason.
Unscoped Claude launches retain a valid caller account; otherwise, they use the configured showrunner account.
An active timed Claude worker route takes precedence for a worker launch.

`GroupNotConfigured`, `UnitsResetSoonest`, `ShowrunnersMostLeft`, and `GroupNoRoom` preserve the selection result's meaning.
Readings rank eligible destinations; they do not silently replace a configured launch account.
Units prefer the earliest weekly reset, then the most weekly allowance left.
Showrunners prefer the most weekly allowance left.
Labels provide a stable final ordering.
Recorded provider refusals block a destination until the refusal resolves.
Codex selection also checks allowance and the account's `use_credits` policy.

`_exec_codex_account()` serves both `codex` and numbered Codex aliases.
It recognizes `update` before account admission.
The local update stays attached to the terminal and uses `accounts_home() / ".codex"`.
Version reads are captured.
The peer command directs update output to stderr and leaves only the version on stdout.
The caller discards peer stderr and rejects an empty version.
Each machine gets one summary line; success requires both updates and version reads.

The [launch environment](nix-shell-launch.md) describes shared Nix setup and account environment preparation.

## Moves, refusals, and workers

Account moves use the shared stop and resume API.
Each session attempt writes a request and one confirmed or failed outcome to `~/.local/state/accounts/moves.jsonl`.
Each attempt has its own move ID and cause.
A session already on the target writes nothing.
The [shutdown design](shutdown-and-restart.md) owns process identity, pane reuse, prompt delivery, and recovery details.
All selected stops finish before the batch resumes any session.
Each session resumes its own conversation on the target account.
A selected caller delegates the batch to a detached process before any signal.
The notes timer delegates its moves through the same boundary before its part deadline can kill them.
Requests use each session's observed process account, including members outside their selected group account.
Blocked notices check the queue head and target again before delivery.
Quota recipients use one session address for both names and IDs.
Workers keep their threads when their Claude owner moves.

A group move validates the destination before changing the selection or stopping members.
It selects the account for future launches and moves live off-target members.
An empty group can still select its account.
Each member has an independent result.
A single-session move refuses a grouped session and names the group operation.
The move path creates no ready mark, conductor, follow command, or cancellation step.

`TranscriptRefusal`, `ProofRefusal`, and `NoSessionRefusal` distinguish provider evidence from a test assumption and absence.
Proof evidence stays with its selected session.
The watcher saves the first refusal observation and suppresses repeated actions for that entry.
Known short five-hour resets can wait instead of moving.
The default wait boundary is strictly less than 20 minutes.
An unknown reset or the exact boundary does not qualify for that wait.
Failed continuation delivery retains the retry state.

Queued moves use group-local positions and stable internal identities.
Only the head of each group can run.
An unavailable target remains queued and blocks later entries.
A changed head loses its old hold and notice state.
`FixedHold` carries a duration; `HalfPointHold` requests a pace-based duration.
`ResolvedHold` keeps the duration and its source.
The half-point estimate uses the selected account's history, including other groups that share it.
The default estimate is 40 minutes when no usable pace exists.
The duration stays with the started hold rather than changing on each pass.

A successful selection and journal entry precede removal of the queued entry.
Replay checks retain a blocked entry when history is unreadable.
An incomplete final journal row is distinct from corruption between complete rows.
Queue edits and execution share locks on natedev.
The Mac forwards edits instead of maintaining a second queue.

Without a queued workers move, provider refusal or unavailable included usage can select another Codex account.
Credit policy determines whether an exhausted account can continue.
Worker assignment fallback to Claude remains separate from the selected Codex account.
Routing returns only when the selected Codex account can work and its refusal has resolved.
Pane workers move through their dispatch lock, fresh pane evidence, `restart.stop_row`, and `worker_panes.start_or_resume`.
They keep the same conversation and reuse a stopped pane.
Only an open pass receives resume text through the shared target check.
`config/shutdown.conf` sets the stop deadline from `STOP_GRACE_SECONDS`, `KILL_GRACE_SECONDS`, and `MOVE_STOP_MARGIN_SECONDS`.
The [quota guide](../quota_alerts.md) owns phone episodes, forecasts, routing notices, and delivery retries.

Login renewal uses named success and failure results.
The watcher rate-limits attempts and records their reason.
A failed renewal remains visible; it does not convert a stale reading into a fresh reading.

### Worker move results

`accounts.py move workers <target>` stops same-family workers and leaves their restart pending.
`assign.sh` cannot yet carry both the fixed staff ID and the destination account for this operation.
The result names the target account: `stopped, resume pending on <account>: <reason>`.

The preview uses the same family decision before it takes a row lock or stops a source.
A same-family worker prints `<name> (<id>): would stop; restart on <target> stays pending`.
A cross-family worker prints `<name> (<id>): not moved: cannot restart on <target> through assign.sh`.
Unavailable family evidence also refuses the restart.
The real refusal leaves the source running.

Source release preserves each stop reason and gives the next action.
The [family handoff](long-lived-workers.md#family-handoff-and-retry) defines its named results and native source evidence.
A session move's opening message selects only its [own worker children](shutdown-and-restart.md#worker-resume-results).

## Recovery and alert ownership

The unit watchdog reads current names, parents, sessions, and folders from staff.
It reads the account from the pane launch record and checks current process liveness.
It saves pending jobs before it starts detached recovery services.
Each child resolves the current staff session before resume.
Historical jobs remain keyed by session id.
Account notes keep historical rows without using them as current counts.
A failed roster read reports one anomaly and sets the derived count to zero.
Recovery keeps its grace period, dispatch limit, and result until a fresh observation confirms the session state.
The watchdog sends no phone push, showrunner notice, or message to a live session.

Retained record-driven restart paths share a durable alert ledger.
The [shutdown design](shutdown-and-restart.md#outage-alert-ledger) describes claims, delivery, leases, machine ownership, and observation-scoped clearing.
The ledger survives shutdown-record archival.
Claim ownership prevents concurrent sends and stale updates.
A second outage or five-hour delivery caller prints `already claimed by <id>` and does not repeat delivery.
Its delivered state means a successful push, not an unfinished send claim.

Config publication uses a blocking `fcntl.flock` with no time limit or retry settings.
The kernel releases the lock when its holder exits.
Usage and renewal schedules keep separate claims.
A second schedule caller prints `already claimed by <id>` and exits with code 0.

## Status, counts, and notes

`StatusReading` separates stored facts from their display.
`StatusDocument` carries account rows, problems, and session input paths.
Named problem causes and repair steps preserve the subject, fault, evidence, and action.
Status attributes a session to its account independently of the current role selection.
This preserves visible off-target members during a move.

Local role counts read the registry, staff, pane launches, and worker records without writing state.
`session_state.classify` supplies the shared unit status for the accounts board and the units table.
An exit record takes precedence over a pause record.
A missing live session gives `exited`; live activity gives `busy` or `idle`.
The workers role retains only `busy` and `idle`.
`session-counts-read` prints the same live count document without publishing a file.
Peer counts use the shared role-map transport in a detached fetch.
One claim prevents concurrent fetches.
The normal status path never waits for that fetch.
The cache keeps each machine's counts separate and rejects copies older than ten minutes.
Canonical `mac` and `natedev` names identify observations independently of a desktop hostname.
The board sums available machine counts and identifies an unavailable peer.

`status --stored` reads saved usage, role maps, and peer counts.
It does not wait for the detached counts fetch.
Local session counts still use current local registry evidence.
The [board design](unit-board.md) owns the display contract and refresh lifecycle.

Account notes use numbered filenames.
Each note shows its roles and live session count.
Existing session rows remain as history and supply no current staff facts.
Unchanged notes are not rewritten.
Usage reads retain account identity and reading outcome; a failed read never becomes a zero-percent reading.
Recent usage can be reused, but low quota, resets, queued moves, and identity changes require fresh checks.
One Codex app-server serves each home within a pass.

## Machine-local settings and promotion

The settings clean filter classifies top-level keys against `git show HEAD:settings.json`.
The fixed local list contains `model`, `effortLevel`, `modelSettings`, `extraKnownMarketplaces`, `theme`, and `voice`.
A new key absent from the committed baseline is also local.
An unreadable or invalid baseline refuses the operation; it never means an empty baseline.
The hook and plugin mergers preserve their shared portions and known local additions separately.

The clean filter saves local values in `settings.local-keys.json` and omits them from Git's shared representation.
It captures stdin once, so snapshot and clean use the same document.
An empty snapshot retains saved keys that remain local.
Smudge restores saved local values without replacing incoming shared keys.
A key that arrives through committed settings becomes shared unless the fixed list keeps it local.
Unknown keys on one machine therefore do not block promotion on another machine.

`NoticeHistory` stores `pending` or `accepted` per machine and key in `settings.local-key-notices.json`.
The filter records a pending notice but sends nothing.
The normal refresh sends the notice later to the destination in `config/settings.conf`.
Only the machine and key name enter the notice; the setting value stays local.
A stable sender key suppresses duplicates.
Successful, skipped, or durably queued delivery marks acceptance.
A failed send leaves the notice pending.
The sender holds no ledger lock during delivery.

The index refresh hashes one file copy before and after cleaning.
It refreshes only when the clean hash matches the staged blob.
The guarded refresh checks the same input and staged hashes under Git's index lock.
It preserves concurrent edits, staged content, unmerged entries, and executable-mode changes.
A clean failure stops refresh and writes its diagnostic to the local error log.
Promotion never hides an unrelated dirty setting.

## Runtime configuration and constraints

`config/accounts.conf` supplies account timeouts, ages, limits, sizes, holds, leases, and path roots.
Each pass takes one `conf.read("accounts")` and shares its `ConfigRead` with its helpers.
The read supplies `whole`, `number`, and `text` methods.
Session loops reuse a value read for that call instead of reading configuration for each session.
Paths with `~` expand through `HOME`, so scratch homes remain isolated.
A configuration change needs no build.

`CONF_DIR` selects an authoritative configuration folder.
Without that override, an absent file or key can use the repository's matching `config/<stem>.conf`.
That default lookup precedes Git recovery.
An invalid present value still uses validated Git recovery, rather than an unrelated repository default.
Explicit folders have no repository fallback.
`ConfigRead.origin(key, convert)` applies the same rules and distinguishes live, recovered, and unavailable values.
The [shared configuration contract](showrunner-automation.md#shared-runtime-configuration) defines reads, limits, and publication.

`note_file.notes_dir()` gives `AGENT_NOTES_DIR` and an explicit `AGENTS_DIR` override precedence.
Its private cached helper reads `notes_directory` at most once per process.
Account creation, identity resolution, and missing-note instructions use that same helper.
A new process sees a changed notes-folder setting.

The default usage age is ten minutes.
Codex alert readings use the same `usage_stale_minutes` setting.
The quota relay limit defaults to 40 seconds, and the registry editor limit defaults to 30 seconds.
Account commands and Claude usage HTTP requests each default to 15 seconds.
The Codex account server has three seconds to stop after termination.
The role-map transport has a five-second connection limit and a twenty-second command limit.
The outage claim lease is fifteen minutes, longer than the normal send deadline.
These values live in configuration; their values are not independent code defaults.
Maintenance schedules and recovery dispatch limits remain in their owning TOML configuration files.

Account and role changes preserve these boundaries:

1. Keep private identity files outside shared links.
2. Use the authoritative role map for every machine.
3. Keep unknown evidence distinct from absence or exhausted allowance.
4. Validate the destination before a destructive move step.
5. Preserve conversation identity and worker threads across account changes.
6. Keep status reads independent of the background counts fetch.
7. Mark an alert delivered only after successful delivery.
8. Clear only alerts that precede the observation of a live session.
9. Preserve shared settings and concurrent edits during local-key filtering.
