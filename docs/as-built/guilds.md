# Guilds and lasting Astra roles

> **Status: as-built.** This doc covers the four guilds, their heads, tips, events and daily read, and the lasting per-design Astra designer and reviewer that a unit director or showrunner holds.

## What it is

Four guilds help members do their jobs better: `designers`, `reviewers`, `showrunners` and `units`. A session belongs to a guild by the role it already holds; there is no member list. Each guild has one head, a unit director with the guild's exact ID under the top-level `guilds` production on natedev. Heads keep tips, one Markdown file per tip, in `~/rust/nate_style/guilds/`, and every starting skill loads its guild's tips at skill start. Heads do not read every member turn. Set events make a member send one short report to its head, and once a day each head reads the records that already exist. Beside the guilds, each design has at most one lasting Astra designer and one lasting Astra reviewer, both `gpt-6-astra:xhigh` Codex threads. One unit director or one showrunner holds the pair. The pair keeps the same threads across review rounds, phases, runs, restarts and account moves, so a design is written and reviewed in one thread each instead of by a fresh turn each round.

## How it works

### Guilds and heads

Membership comes from the held role:

| Guild | Members | Start site |
| --- | --- | --- |
| `designers` | Astra designers | `config/guilds/designer.md` (designer opening prompt) |
| `reviewers` | Astra reviewers, held or one-turn | `commands/astra_review.md`; `scripts/astra/astra_review.py` `review_prompt` |
| `showrunners` | `/showrunner:produce` sessions | `commands/showrunner/produce.md` `<Guild>` |
| `units` | `/unit:direct` sessions, the four heads included | `commands/unit/direct.md` `<Guild>` |

Codex code and test workers and pooled `code`/`ui` reviewers have no guild. Their directors pass tips on through better Work Orders.

- **Production.** `docs/plans/guilds-production.md` defines the top-level `guilds` production on natedev with no parent (merge branch `guilds`, showrunner checkout `/home/natepiano/worktrees/claude-guilds-trunk`). herdr space `guilds` has the showrunner in tab 1, then `designers`, `reviewers`, `showrunners`, `units`. Each head is launched from its Units row with `/showrunner:add_unit <id> --plan <Plan cell>`.
- **Head IDs.** `scripts/production/add_unit.py` `launch_request` keeps a name as the unit ID when a Units row in that production already has that exact `Unit` cell; otherwise it adds `-unit`. The guilds production doc lists `designers`, `reviewers`, `showrunners`, `units`, so the heads keep those IDs. `docs/production_format.md` `<GuildHeads>` states the same rule.
- **Head lookup.** Run `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --json` and take the one natedev entry with production `guilds`, role `unit director` and unit equal to the guild ID. Missing or several matches mean no send; report the lookup problem to the guilds showrunner (same roster, production `guilds`, role `showrunner`). Never infer an address from a name or cache it across restarts.
- **Standing loop.** Each head runs `docs/plans/guilds-<guild>.md`, headed `> **Standing:** guild-head`. `commands/unit/direct.md` `<GuildHeadStanding>` takes precedence at startup, restart, compaction, phase selection and `<StandingPlanRefill>` entry. The loop: read a member report or user feedback, check its cited evidence, publish an owned tip with `tips.py publish`, reply with the tip path and pushed commit or a reason to decline. It runs no refill, phase, worker, checkpoint, shrink, FinalGate or RunSummary. An idle head ends each turn with `— holding: waiting on the next guild report or daily check-in`.
- **Duties.** Heads own tips, not assignments, acceptance, model choices or gates. A process proposal goes from the head through guilds to sdlc for `guild-process-unit`. No tip changes a user rule. The guilds showrunner relays the user's guild feedback (from any showrunner, per `produce.md` `<Guild>`) to that guild's head.
- **Daily timers.** The guilds showrunner owns four `scripts/message/notifier.sh` timers, `guilds-<guild>-daily`, created with `new … --every 1440 --to session:<id> --from guilds`. After a head restart it stops and removes that head's stale timer, resolves the current session and creates a new one. Timers wake Claude heads only, never Codex members.

### Events

A member observes these events and reports to its head. A director detects ledger events for its `units` report and tells an affected Astra member in its next normal brief. The same evidence may feed two guilds.

| Event | Trigger and evidence |
| --- | --- |
| User correction | The conversation corrects an earlier instruction, result or behavior. After applying it, report the corrected rule and the conversation time. New work alone is not a correction. |
| Repair round 4 | The director first reads `findings.py gate` with `verdict=dispatch` and `round=4` for a phase instance. A second read of that gate sends nothing. A round-4 result that converged is not this event. The gate outcome does not change. |
| Repeated review finding | In one run, normalize title case and spaces. Count matching `finding_opened` titles in different `(phase_instance_id, round)` groups, or a `still_open`/`reopened` verdict after a `finding_batch_landed` that names that finding. One group counts once across lenses; one verdict counts once per finding and round. Missing phase, round or repair order gives `RepeatNotProven`. Cite both event IDs. |
| Tool or rule workaround | After a workaround for a tool fault or a slow rule, report the failure, the command or rule, the workaround and the observed cost. Only noticing a slow command is not enough. |
| Saved time | After a method saves measured time against a named earlier run or step, report both durations and the change. Label an estimate as an estimate. |

**Report form.** At most 100 words, six lines:

```
Guild <id>: <event> — <short fact>
Member: <name>; holder: <address if Codex>
Work: <repo, branch, run/phase or design>
Evidence: <path:line or review IDs; time>
Change: <what worked or needs correction>
Result: <cost or saving; limit>
```

A retry names the same source event. Keep an unsent report with its phase or design evidence. Claude members send with `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to <head-address> --from <name> --text <report>` or SendMessage. A Codex worker puts the report in its reply; its holder relays it unchanged. Heads reply to a Codex member through its holder. A queued report has not reached the head; report the transport outcome as returned.

### Guild block

`scripts/guilds/tips.py` `GUILD_BLOCK` holds the block; `guild_block(guild)` replaces `{guild}` and refuses an unknown guild:

> Your guild is `{guild}`. Your head is the `{guild}` unit director in the top-level `guilds` production on natedev. At skill start, use the Tips lock/pull rule for `~/rust/nate_style` and load `guilds/common/` and `guilds/{guild}/`; a Codex holder does this for its worker. Report to your head when the user corrects you, a phase needs repair round 4, a finding meets the Events repeat rule, you use a workaround for a tool fault or slow rule, or a method saves measured time. Send one report of at most 100 words, with evidence and result. Resolve the head from `message/roster.py --json` by production `guilds` and unit `{guild}`; send with `message/send.py` or SendMessage. A Codex worker puts the report in its reply for its holder to send. Send only to the head. Never wake an idle Codex member for guild advice.

Each start site has one `<Guild>` section (or `## Guild` in a prompt) with the expanded block, the line `Load tips: ~/.claude/scripts/lib/py ~/.claude/scripts/guilds/tips.py load --guild <id>`, and a pointer to Events for detection and form. The sites are `commands/unit/direct.md`, `commands/showrunner/produce.md`, `commands/astra_review.md`, `config/guilds/designer.md`, and `scripts/astra/astra_review.py` `review_prompt`, which appends the block, the load line, the pointer and `tips.load("reviewers")` to every reviewer prompt. The four standing plans `docs/plans/guilds-<guild>.md` also point to Events. `direct.md` `<Guild>` adds the round-4 and repeat rules above and the designer brief rules (see Astra work header).

### Tips

- **Folders.** `~/rust/nate_style` (own GitHub repo, branch `main`) holds `guilds/designers/`, `reviewers/`, `showrunners/`, `units/` and `common/`. A tip for all four guilds goes in `common/`.
- **Tip file.** YAML front matter with `tags: [guild/<id>, guild-event, head/<id>]` (or `guild-tool`; `guild/common` for a common tip), `date_created` and `date_modified` as `"[[YYYY-MM-DD]]"`; then one `##` heading, short text and a `Source:` line. The `head/<id>` tag names the owning head. A common tip has exactly one `head/` tag.
- **Base views.** `guilds/<id>_base/events.base` and `tools.base` (eight files): one YAML `views` entry, `type: table`, `filters: and:` with `file.hasTag("guild-event")` or `file.hasTag("guild-tool")` and `or:` of `guild/<id>` / `guild/common`; columns `file.name`, `tags`, `date_created`, `date_modified`, sorted by `file.name`.
- **Lock.** Private `_lock(repo, timeout)` takes `fcntl.flock` on `<git-common-dir>/guild-tips.lock`, polling every 50 ms up to a monotonic deadline. `GUILD_TIPS_REPO` overrides the repo path in tests.
- **`load(guild) -> str`.** Under the lock with `LOAD_LOCK_TIMEOUT` (10 s), runs `git pull --ff-only` once, then prints `commit <sha>` and, for each `guilds/common/*.md` and `guilds/<guild>/*.md` at that commit, `## guilds/<path>` and the file text read with `git show`. A lock or pull failure prints one `tips:` stderr line and uses the local commit. No commit or no `guilds/` at `HEAD` raises `BaselineMissing` (a `ValueError`).
- **`publish(head, folder) -> str`.** Under the lock with `GIT_TIMEOUT` (60 s): requires branch `main`; reads the prepared folder (paths relative to `guilds/`); refuses symlinks and paths that leave the checkout; allows only `<head>/`, `<head>_base/`, or a `common/` file whose only `head/` tag is `head/<head>` in both the prepared and existing file (YAML comments ignored). It refuses a staged index, an uncommitted or ignored target (`status --porcelain --ignored --untracked-files=all`) and foreign unpublished commits. It pulls `--ff-only`, checks again, writes, commits only the prepared paths (`Update <head> guild tips`) and pushes `origin main`. On a pull or push divergence it fetches, refuses dirty tracked work or foreign commits, rebases only its own commit onto `origin/main` and retries one push. On a rebase conflict it aborts and keeps the local commit. It prints the commit only after the push succeeds.
- **CLI.** `tips.py load --guild <id>`; `tips.py publish --head <id> --from <folder>`. Exit 2 for `BaselineMissing`, exit 1 for every other refusal, one `tips:` line on stderr.

### Daily read

`scripts/guilds/daily_read.py --guild <id> --since <UTC> --until <UTC> [--json]` prints one host's local evidence for the interval. It reads no turns, writes no history and contacts no other host. `read_daily(guild, since, until, sources) -> dict[str, object]` raises `ValueError` (CLI exit 2) for an unknown guild, a non-UTC time or an empty interval. The head runs it on natedev and asks the Mac's roster session to run it once, then combines the host-labelled results in a git-excluded `daily-report.md` at its worktree root, with each covered UTC interval. A duplicate check-in does no second read; after downtime the head covers the gap once and states its length.

**Sources.** Frozen `LocalSources(run_roots, history_root, astra_root, roles_root, host)`; `configured()` reads:

| Variable | Default |
| --- | --- |
| `GUILD_DAILY_RUN_ROOTS` (`os.pathsep` list) | `unit_runs.run_roots()` |
| `GUILD_DAILY_HISTORY_ROOT` | `unit_runs.history_root()/runs`: `$UNIT_HISTORY_DIR`, else `~/.local/state/unit-history`, else `~/.local/state/plan-delegate` before the move |
| `GUILD_DAILY_ASTRA_ROOT` | `$ASTRA_REVIEW_DIR`, else `/tmp/claude/astra` |
| `GUILD_DAILY_ROLES_ROOT` | `$GUILD_ROLES_STATE`, else `~/.local/state/guilds/roles` |
| `GUILD_DAILY_HOST` | `socket.gethostname()` |

**Joins.** For `designers` and `reviewers` it reads `astra_work` headers from `<run>/design/phase*/round*_brief.md`, `<astra_root>/*/prompt.md` and `<roles_root>/*/prompts/*.md`, keeps the matching role, and requires absolute `document` and `run_directory`. It joins headers on `run_id` and `phase_instance_id` to progress-history ledgers (`rounds.load_phases`, `rounds.phase_rounds`), `phase_process` step allocation, and `findings_state*.json` titles. `units` selects runs whose events carry `skill == progress_history.SKILL_NAME` (`plan-delegate`) and names the member from `run_started.main_agent.session_id`. `showrunners` gives no phase rows and one `HistoryNotRecorded` problem with source `WriterNotRecorded`, because no showrunner writes run history.

**Flags.** `RepairRoundsBeyondThree` (four or more repair rounds), `MixedPhaseRecord`, `SlowStep`, `RepeatedTitle`. `repeated_titles` applies the Events repeat rule and returns `RepeatedTitle` or `RepeatNotProven` rows with both event IDs and sources. `SlowStep` needs at least 20 baseline samples and seconds above the p90 of the 14 UTC days that end at 00:00 UTC on the `since` date. Fewer samples give `InsufficientSamples`; `director time` prints as `unattributed wall` with `CoverageUnknown` and gets no speed verdict.

**Problems** keep named states and make coverage partial: `EvidenceUnavailable`, `EvidenceInvalid`, `HistoryNotRecorded`, `HistoryInvalid`, `RunAmbiguous`, `PhaseUnassigned`, `FindingsNotRecorded`, `HeaderInvalid`, `MembershipNotRecorded`. An assignment row has `OutputRecorded` when its `reply_path` exists, else `OutcomeUnknown`. Waits and merge receipts are one fixed row each, `HistoryNotRecorded` / `ReceiptMissing` with source `WriterNotRecorded`, until hierarchy writers exist. A guild with no member in the interval gets `MembershipNotRecorded` naming its read roots. Output shows host, guild, UTC and local interval (`user_zone`), coverage, counts, assignments, phases, repeats, waits, receipts, flagged rows and problems. Phase totals are shared context, not one member's delay.

### Astra work header

`scripts/guilds/astra_work.py` saves assignment identity before dispatch as the first line of a brief or prompt: `<!-- astra_work {JSON} -->`.

- Frozen types: `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)` (`kind="phase"`), `StandaloneWork()` (`kind="standalone"`), `AstraWork(role, member, document, assigned_at_utc, reply_path, work)` (`schema=1`, role `astra-designer` | `astra-reviewer`), and read results `HeaderMissing(path)`, `HeaderInvalid(path, reason)`.
- `phase_work(run_directory, phase_id) -> PhaseWork` reads `progress_history_state.json`: `open_phases` plus the `phase` when its status is `active`. Exactly one match or `ValueError`.
- `header_line(work) -> str`; `read_header(path)` checks the exact key sets, `schema` 1, the role and a UTC `assigned_at_utc`.
- CLI `astra_work.py brief --run-directory --phase --member --document --round --text-file` writes `<run>/design/phase<id>/round<N>_brief.md` by exclusive create, with `reply_path` `round<N>_reply.md`; it refuses (exit 2) when that reply exists. Holders use it only for a designer that `roles.py` does not hold, and save the reply there without overwrite.
- `astra_review.py [--run-directory <dir> --phase <id>] <document> [focus]` takes both flags or neither. A one-turn review writes the header as the first line of `/tmp/claude/astra/<stamp>-<pid>/prompt.md`, with `member` the run folder and `reply_path` its `review.md`. Exit 2 also covers an unresolved phase and `BaselineMissing`; exit 1 covers a tips read error.

### Lasting roles

`scripts/guilds/roles.py` keeps one designer and one reviewer slot per design.

**Store.** Root `GUILD_ROLES_STATE`, else `~/.local/state/guilds/roles/`, on the holder's host. One directory per UUID `design_id`:

| Path | Holds |
| --- | --- |
| `design.json` | schema 1, placement, `repository_id`, checkout (`repository`), absolute `document`, area, `created_at_utc`, tagged holder and binding, both role slots (`role`, `name`, tagged lifecycle, `assignment_claim`) |
| `lock` | the design lock (`fcntl.flock`) |
| `mesh/` | the role's mesh: `mesh_roster.json`, `mesh_server.json`, `guild_context.json`, `serving_home.json`, `<name>.pending.json`, `<name>.account-move.json` |
| `prompts/<role>-<stamp>-<uuid>.md` | each headed prompt, exclusive create, with a `.messages.json` list of included pending IDs |
| `replies/<role>-<stamp>-<uuid>.md` | each reply, at the header's `reply_path` |
| `pending/<role>/<id>.json` | one `PendingMessage(id, source, text)` per queued message |
| `transition.json` | an incomplete rebind, unit rename, explicit end or adoption |
| `transfer.json`, `adoption.json` | the adoption fence and its completion record |

Role IDs are `<design_id>/designer` and `<design_id>/reviewer`. Instance names are `<production>-<area>-astra-designer` / `-astra-reviewer`; an adopted role keeps its old mesh name.

**Types.** Frozen dataclasses, tagged by `kind` at the JSON boundary (`../type_design.md`): `UnitHolder(production_slug, unit_name)` | `ShowrunnerHolder(production_slug)`; `DesignUnbound` | `DesignBoundToPhase(run_directory, phase, repository, output_paths)`; `PhaseBoundDesigner(binding_generation, launch_sandbox)` | `ReadOnlyReviewer` (`ThreadAccessPolicy`); `RoleNotOpened(claim)` | `RoleOpen(thread_id)` | `RolePausedForMove(thread_id)` | `RoleNeedsRecovery(thread_id, reason)` | `RoleEnded(thread_id)`; `DesignPlacement(design_id, host, generation)`; `RoleInstance`, `HeldDesign`, `PendingMessage`, `HolderReach(session_id)` | `HolderUnavailable(reason)`. Refusals raise `RoleRefused(ValueError)`.

**CLI** (exit 2 and one `roles:` stderr line on any refusal):

| Verb | Function | Does |
| --- | --- | --- |
| `open --repository <id> --document --role --production [--unit] --area [--run-directory --phase --checkout --output-path …]` | `open_role` | Reserve placement, create the slot, start one thread. `--repository` is the registered repository ID; the checkout comes from the authority. |
| `adopt … --source-mesh --name --thread-id --adoption-id` | `adopt` | Move an existing idle thread into the store. |
| `resume <instance> --prompt-file` | `resume` | Build a headed prompt, claim and dispatch one turn, print the reply path. |
| `recover <instance>` | `recover` | Prove the recorded thread idle and clear an uncertain claim. |
| `rebind <design> --expected-generation --reason [--unbind / --run-directory --phase --output-path / --production --unit / --checkout --document]` | `rebind` | Change holder, binding, checkout or document while idle; advances the generation. |
| `end <instance>` | `end` | End one role. When no role of the design stays open, it also stops the design mesh server and writes `end-server-release.json`; a failed stop leaves the role `RoleNeedsRecovery`. |
| `unbind-run <run>` | `unbind_run` | Unbind every design bound to that run. |
| `list --json` | `list_roles` | Local roles, holder reach, lifecycle (with `transition.json`, move marker and failed status mapped to recovery/paused states), and problems; no network calls. |
| `register --repository --host --checkout [--inventory-complete]` | `register_repository` | Register a repository with the authority. |
| `authority <operation>` | `_authority_local` | Run one authority operation from JSON on stdin; refuses on a host that is not the authority. |

Other public functions: `queue_message`, `pending_messages`, `held_document`, `end_holder`, `check_patch`, `rename_holder`, `rename_failures`, `move_holder`, `holder_failures`, `transfer_route`, `durable_meshes`, `resolve_holder`, `resolve_model`, `access_policy`, `refresh_census`, `authority_request`.

**Ownership authority.** `config/guilds.conf` pins `[ownership] host=natedev` (override file: `GUILD_CONFIG_FILE`; local host: `GUILD_ROLES_HOST`, else the short hostname). `authority_request` runs locally on that host, else over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> … roles.py authority <op>` with a 30 s limit; failure is `RoleRefused('ownership authority unavailable: …')`. Only the authority writes `GUILD_OWNERSHIP_STATE`, else `~/.local/state/guilds/ownership/`: `repositories.json` (ID to per-host absolute checkouts and `inventory_complete`) and `placements/<sha256(repository_id \0 relative document)>.json`. Operations: `register`, `reserve`, `lookup`, `advance` (generation +1 on the placement and its aliases), `checkout` (move a host's checkout mapping), `alias`, `legacy` (record `adoption_required`), `adopt-reserve`, `adopt-commit`. Every operation except `register` refuses until `inventory_complete` is true, and refuses a host with no explicit checkout mapping or a document outside it. A placement on another host refuses: the pair always lives on its holder's host. `[roles] new_open=false` makes `open_role` and `adopt` refuse and keeps every existing identity.

**Authorization.** `_authorize` requires placement host = local host, a live holder from `resolve_holder` (unit holder: `unit_lookup.every_marked_unit` with a `LiveClaude`; showrunner holder: `production_tree.showrunner_sessions` and `sessions.running_sessions`), and `CLAUDE_CODE_SESSION_ID` equal to that session. A missing or ambiguous holder blocks dispatch but does not change the holder.

**Models.** `resolve_model(role)` sources `scripts/agents/agents_config.sh` and runs `agents_resolve "guild.<role>"`; the family must be `codex`. `config/agents.conf` has `[assignments] guild=codex` and `[guild.codex] designer=gpt-6-astra:xhigh`, `reviewer=gpt-6-astra:xhigh`. A missing or invalid row refuses; there is no fallback model.

**Prompts.** A designer prompt is `config/guilds/designer.md` + `tips.load("designers")` + `Use apply_patch for all repository edits. Edit only the bound output paths.` A reviewer prompt is `astra_review.review_prompt(document, "")`. `_prompt` writes the `astra_work.header_line` first (`PhaseWork` from the binding, else `StandaloneWork`), then the instructions, the holder's text and each pending message as `Pending message <id> from <source>: <text>`.

**Access.** The roster entry's `access_policy` decides the sandbox (`codex_mesh.py` `_seat_sandbox`, `_resume_params`): `ReadOnlyReviewer` gives `read-only`; `PhaseBoundDesigner` gives its `launch_sandbox` (`danger-full-access`). A legacy entry with `instructions_file` and no policy refuses with "recovery required". A designer must be bound; `_validate_binding` checks the phase resolves, the output paths stay inside the repository and are listed in the open phase's `files`, and the reservation ledger reads.

**Lifecycle as built.**

| Event | Result |
| --- | --- |
| First open or retry | Authority `reserve`, then under the design lock create `design.json` or reuse it (another holder refuses). Validate binding, resolve the model, save the prompt, write `RoleNotOpened(claim)` and `guild_context.json`. Outside the lock, `codex_mesh.py prime --held-design --held-role` starts the thread; prime refuses without the holder's claim and writes `<role>.creation-claimed` by exclusive create. Under the lock again, accept the thread only if the claim is unchanged, set `RoleOpen`, then write the census hold. A retry with a claim or an open role returns the current design. A failed launch keeps the claim. `open` on `RoleEnded` refuses: `role ended; this design identity cannot be reopened.` |
| Assignment | `resume`: recover first if the role needs recovery or has a claim; under the design lock check `RoleOpen`/`RolePausedForMove`, no `transition.json`, idle, binding valid. Save the prompt, then under design and roster locks record `authorized_prompt`, `authorized_generation`, `authorized_holder` and `access_policy` and set `assignment_claim`. Dispatch with `codex_mesh.py follow --role impl|review --message-file <prompt> --summary-file <reply>`. Success clears the claim and deletes the included pending files; failure sets `RoleNeedsRecovery` and keeps pending input. |
| New scope during a turn | Not steerable. Finish the turn, update phase files and reservation, `rebind` while idle, then resume. |
| Phase or run ends | `end_session.sh` runs `roles.py unbind-run <run>` per run before it removes workers and stops run meshes; a failure stops the script. Threads, prompts and replies stay. |
| Rebind or holder transfer | Same host only, while idle, with a reason and the expected generation. Write `transition.json`, release servers (`_release_held_server`), authority `advance`, save the new holder/binding/checkout/document and generation, remove `transition.json`, refresh `guild_context.json` and the census holds. |
| Account move | `retire_workers_servers` adds `roles.durable_meshes()` and takes the holder from `guild_context.json`. A running role gets the move marker before interruption (`_interrupt_worker_for_account_move`); an idle one has its queue parked and its thread released. `command_resume_account_move` reads the saved header and writes to its `reply_path`. `list` shows `RolePausedForMove` while the marker exists. `accounts.py` `_swap_workers` needs no held-role code. |
| Crash or uncertainty | `recover` refuses while the launcher pid lives, resumes the recorded thread on the mesh server, requires the same thread ID and `ThreadIdle`, then sets `RoleOpen` and clears the claim. A resume that cannot keep the same thread gives `RoleNeedsRecovery`; no replacement thread starts. |
| End | Holder only, idle, not during an adoption. Write `transition.json`, run `codex_mesh.py end`, then require `held_thread_end_confirmed(session_dir, thread_id)`: every recorded live server shows the thread idle or unloaded. Unconfirmed gives `RoleNeedsRecovery` with the roster end marker kept. Confirmed sets `RoleEnded` and releases the census hold. Evidence stays. |
| Retire | `retire_unit.py` `retire` calls `roles.end_holder(production, unit)` after the unit's sessions stop; only the production showrunner's session may run it; each open role must be idle. A failure leaves the unit not retired. |

**Pending messages.** `codex_mesh.py send` to a held entry calls `roles.queue_message` and prints `queued for <name>; holder adds it to the next authorized assignment`. `mesh_runs.py` queues a held role's detached result with a stable `uuid5` ID and source `mesh_runs:<run_id>`, following a `transfer_route` to the adoption destination. Nothing wakes the role. `commands/message.md` and `codex/AGENTS-messaging.md` `<HeldRoleMessages>` state the same rules for senders.

**Mesh refusals for held entries** (`codex_mesh.py`): `start`, `prime` on an existing held entry, `fork` from one, `reopen`, raw `stop --pause` (`_pause_roster`), raw `stop`, and `set-instructions` refuse with a pointer to `roles.py`. `follow` refuses a message file other than the `authorized_prompt`, except an account-move continuation. `implement.sh --to` refuses a held or fenced entry.

**Turn claim and lock order.** `_claim_follow` runs inside `_design_claim_lock` (design `lock`, then the pending file), then the roster flock: design, pending, roster, server. `_validate_held_claim` checks the role is open on the same thread, the caller is the live holder, the authorized generation and holder match, `assignment_claim` equals `authorized_prompt`, and no `transition.json` exists. It then sets `instructions_file` and `access_policy`. The claim pops `authorized_prompt`, so one authorization starts one turn. A stale launcher claim on a held entry is not taken over.

**Server environment.** `_guild_context(session_dir)` rebuilds `guild_context.json` from `design.json` and the live holder. `ensure_server` compares it with the server record's `guild_context` (and home, compact limit, sign-in); any difference runs `_release_held_server` first. That function saves the serving home, parks each queue (`_move_queue_to_pending`), requires `ThreadIdle`, releases the thread (`_release_worker_thread`) and reaps the server; uncertainty raises `RoleNeedsRecovery`. `_server_environment` for a durable mesh drops `UNIT_*`, `PLAN_DELEGATE_*`, `CLAUDE_CODE_SESSION_ID`, `GUILD_DESIGN_ID` and `GUILD_BINDING_GENERATION` from the caller and sets them from the context: holder session, design ID, generation, and `PLAN_DELEGATE_BOARD_DIR` only when bound.

**File ownership.** `scripts/hooks/pre-tool-use-berth.py` checks `apply_patch` only. With `GUILD_DESIGN_ID` set, it calls `roles.check_patch(design_id, GUILD_BINDING_GENERATION, cwd, paths)` before phase attribution. It rejects the whole patch for `transition.json`, a stale generation, a holder mismatch, an unbound designer, any path (rename sources included, symlinks resolved) outside the bound output paths or the repository, or an unreadable ledger. The binding's run directory then drives attribution, and an unreadable reservation ledger refuses instead of failing open.

**Adoption.** `adopt` needs the authority's `adoption_required` source (`thread_id`, `name`, `mesh`, holder) to match. Steps: first the checks that change no authority state: the binding, the configured model, and the source entry, which must be idle on that thread (a `paused` entry counts as idle when `paused_from` is not busy). The source model and effort come from the roster entry; when it has none, from that thread's launch record (`impl_worker_<slot>.json` with the same `worker_id`, then the `phases/*/impl_agent_<slot>` file whose log names the worker and thread), and last from the source server's `thread/read` reply. Missing, conflicting or different values refuse with `AdoptionRequired:`. A refused adopt therefore leaves no `AdoptionReserved` claim. Then `adopt-reserve`; check the source entry again under its roster lock; write `transition.json` and `transfer.json` and set `transferring_role` on the source entry under its roster lock; on the source server park the queue, require `ThreadIdle` and release the writer; write the destination roster entry (same thread and name, saved prompt, access policy) and carry pending text; `confirm_adoption` resumes the same thread on the destination and requires `ThreadIdle`; set `role_redirect` on the source; write `adoption.json`; `adopt-commit`; write the hold. A retry with the same adoption ID completes from `adoption.json`. `_role_fence` and `transfer_route` make every old route refuse (`TransferringRole: use the destination holder route`), also after the source run folder is gone.

**Unit rename.** `rename_unit.py` `_move_state` calls `roles.rename_holder(slug, old, new, old_worktree, new_worktree)` before relaunch: idle required, authority `checkout` then `advance`, holder, checkout, document and binding paths moved, generation +1, `guild_context.json` rewritten, roster `cwd` set and `authorized_*` cleared. A failure raises `RenameRefused`; the rename journal retries it. `_state_failures` adds `roles.rename_failures`, which flags an old holder, old checkout in live fields or roster `cwd`, or a stale context. `unit_rename_sweep.py` `_history` treats `prompts/`, `replies/`, `adoption.json` and `transfer.json` as history, and `sweep` ignores role `name` and `aliases` in `design.json` and `aliases` in the roster as identity.

**Unit transfer and production rename.** `rename_holder` is `move_holder(old_holder, new_holder, old_checkout, new_checkout, reason=...)` for one unit. `move_holder` moves every design held by `old_holder` to `new_holder`; when the checkout does not change, it keeps the repository, document, binding paths and roster `cwd`, and skips the `checkout` request. It writes `transition.json`, releases the servers, bumps the generation and clears `authorized_*`; a second call changes nothing. `holder_failures(old_holder, old_checkout='')` flags an old holder, a stale context and, with a checkout, the old checkout. `transfer_unit.py` `_move_state` moves `UnitHolder(from, unit)` to `UnitHolder(to, unit)` and `verify_failures` adds `holder_failures`. Production `rename.py` moves `ShowrunnerHolder(old)` to `ShowrunnerHolder(new)` in apply step 4, and each `UnitHolder(old, unit)` with its worktree paths in unit step 3. Role names keep the old slug: they are thread aliases, kept as history. A failure raises `RenameRefused`.

### Census for cargo-handler

- `scripts/astra/astra_roles.py` writes one file per thread, `~/.local/state/astra/roles/<thread id>.json` (`ASTRA_ROLES_STATE`), by temp file and rename. `RoleRecord` keys are exactly `role`, `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, plus `design_id` and `role_id` for a durable hold. In memory `HeldRole`/`LiveRole` carry `lifetime: MeshWorker(session_dir, worker) | ExecProcess(pid) | HeldDesignRole(design_id, role_id)`.
- `hold(role, thread_id, codex_home, *, session_dir, worker, pid, manager, design_id, role_id) -> HeldRole`, `release(thread_id)`, `live_roles() -> list[LiveRole]`; CLI `hold`, `release`, `list [--json]`, exit 2 on a refusal.
- `live_roles` removes a mesh hold whose run folder, roster worker or thread is gone or whose status is `ended`/`failed`, and an exec hold whose pid is gone. A `HeldDesignRole` hold ends only when the durable role is `RoleEnded`; a missing run folder cannot end it. Its `manager` is refreshed to the current holder session. `busy` means status `starting`, `running` or `waiting_capacity` (or a live exec pid). A bad record is one stderr line; other records still return.
- `astra_review.py` `run_review` holds an `ExecProcess` reviewer during its one-turn `codex exec` when `CLAUDE_CODE_SESSION_ID` is set, after reading `session id:` from `codex.log` (30 s), with `codex_home` from `accounts.py env workers`, and releases in `finally`. For a document with a held design (`roles.held_document`), `held_review` sends it to the sole reviewer through `roles.resume`; busy or unavailable exits 1 and starts no other reviewer.
- `scripts/production/production_tree.py --json` keeps schema 1 and adds one `AstraEdge` (`manager`, `member` = thread ID, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) per live hold, and drops the Codex worker edge for that thread.
- `scripts/message/roster.py` `_durable_roles` adds one Codex entry per open held role (role `astra-designer` / `astra-reviewer`, reach `CannotReceiveMessages`, director or showrunner from the holder), skipping fenced threads. `scripts/agents/mesh_runs.py` `_roster_folders` lists durable meshes.
- Colors: `config/statusline.conf` `role.astra-designer=mauve`, `role.astra-reviewer=bright-green`.

### Names

| Thing | Name |
| --- | --- |
| Top-level production | `guilds` |
| Guilds and head-unit IDs | `designers`, `reviewers`, `showrunners`, `units` (no `-unit`) |
| Process unit | `guild-process-unit`, under `sdlc`; owns process changes, not head operations |
| Designer / reviewer instance | `<production>-<area>-astra-designer` / `-astra-reviewer` |
| Adopted instance | keeps its old mesh name |
| Worker wire roles, census roles | `astra-designer`, `astra-reviewer` |
| Daily timers | `guilds-<guild>-daily` |

In prose use Astra designer, Astra reviewer, pooled reviewer, and designers/reviewers guild head. Goddess names are on hold. A separate design needs its own area, brief and ID.

## Invariants

- One designer and at most one reviewer per design, one holder per design, and the pair lives on the holder's host. No host transfer and no authority failover.
- A lost or uncertain thread becomes `RoleNeedsRecovery`; nothing starts a replacement thread, forks a held thread or reopens an ended identity.
- Held roles change only through `roles.py`. Every mesh route that can start, follow, pause, stop, reopen, fork or re-instruct a thread checks `held_design_id` and `_role_fence`. A new route must do the same.
- Lock order is design, pending, roster, server. No SSH, authority call or LLM turn runs under these locks; `authority_request` runs after design and mesh locks are released.
- Park queued text before any server stop or release. Clear a pending message only after its turn is accepted; keep it and its ID on uncertainty.
- Access comes from the roster `access_policy`, never from the instructions text. Reviews stay `ReadOnlyReviewer`.
- The berth check covers `apply_patch` only. Designers edit by patch; shell or Python writes are outside the check. Do not copy reservation state into the role store.
- Role records outlive run folders. Ordinary run cleanup unbinds; it never ends a held role or stops a durable mesh.
- A census hold is a projection of the durable role, repeatable on the next lifecycle action. Removing it cannot delete a design. `AstraEdge` fields and `production_tree.py` schema 1 stay; cargo-handler reads them.
- `production_tree.py --json` reads only local records, never prompts, answers well under a second, and drops only the edge it cannot read, with a stderr line.
- A showrunner may hold a read-only role but cannot launch an unbound writer, because no production-design phase exists yet. A writer needs an authorized phase with exact output paths, baseline and reservation.
- Heads publish only through `tips.py publish`. Tips never change a user rule. No guild member list, broadcast group, manifest or receipt exists; do not add one.
- The daily read selects evidence; it adds no stop rule or model change, reads no turns and infers no waiting, delivery or speed verdict from absent records.
- Tests use fixtures and environment overrides (`GUILD_ROLES_STATE`, `GUILD_OWNERSHIP_STATE`, `GUILD_CONFIG_FILE`, `GUILD_ROLES_HOST`, `GUILD_TIPS_REPO`, `GUILD_DAILY_*`, `ASTRA_ROLES_STATE`, `ASTRA_REVIEW_DIR`), never real state or a live Codex.
- Stored stamps are UTC; user-facing times use `scripts/lib/user_zone.py`.

## Calibration / gotchas

| Value | Where |
| --- | --- |
| `LOAD_LOCK_TIMEOUT` 10 s; `GIT_TIMEOUT` 60 s (publish lock and each Git command) | `tips.py` |
| `THREAD_HEADER_TIMEOUT` 30 s to read `session id:` from `codex.log` | `astra_review.py` |
| Authority SSH: `ConnectTimeout=10`, 30 s run limit; `resolve_model` 15 s | `roles.py` |
| Daily timers every 1,440 minutes | `guilds-production.md` |
| `SlowStep`: p90 of the 14 UTC days before `since`, 20 samples minimum | `daily_read.py` `phase_rows` |
| `RepairRoundsBeyondThree` at four or more rounds; the Repair round 4 event at the first round-4 dispatch | `daily_read.py`, `direct.md` |
| `THREAD_RELEASE_TIMEOUT_SECS` 20 s for the unloaded-thread check in `held_thread_end_confirmed` | `codex_mesh.py` |

- The server context comparison uses JSON-normalized values. A tuple `output_paths` against the saved JSON list never matches, and the release then takes the pending lock a second time. Keep `_guild_context` passing through `json.dumps`/`_loads`.
- The adopt commit writes `adoption.json` before it removes `adoption_required`, so a same-ID retry completes after a failure at any step.
- Account-move recovery checks the saved header before it claims the roster entry.
- The first `register` for a repository must carry every host's checkout: a later `register` with a different mapping refuses. The `register` CLI takes one host; use `register_repository` for two, or the authority `checkout` operation to move one. `alias` and `legacy` are reached only through `roles.py authority <op>`.
- Scripts import as `from scripts.<folder> import <module>` after `sys.path.insert(0, <repo root>)`. `roles.py` imports `codex_mesh` and `astra_roles` through `importlib` at the call to avoid an import cycle; do not name a module function `open`.
- A new script folder runs in the merge gate only when listed in `scripts/production/merge_gate.py` `TEST_SUITES`; `test_merge_gate.py` `EXPECTED_SUITES` changes with it. `pyrightconfig.json` gives `scripts/guilds` its extraPaths.
- basedpyright exits 3 for the missing `.venv`; only its `0 errors, 0 warnings, 0 notes` footer counts. A test helper named `run` hides `unittest.TestCase.run`. The standard library has no YAML parser; `tips.py` reads front-matter tags by line.
- `git status --porcelain` omits ignored files unless given `--ignored`.
- Every `/astra_review` pulls `~/rust/nate_style`, so a `guilds/` change reaches `origin/main` before `~/.claude` merges. The `nate_style` working tree holds unowned edits (`review-charter.md`, `.obsidian/`, untracked rule files); commit exact paths, pull `--ff-only` first, push fast-forward only.
- Every progress-history event has `skill` `plan-delegate`, and no showrunner writes run history, so `showrunners` reads give no phase rows. A headerless `/tmp/claude/astra/*/prompt.md` older than `since` is skipped; a newer one gives `MembershipNotRecorded`.

## Why

- **One store outside run roots.** A run folder ends with its run, and a `MeshWorker` hold ends with its folder. A design outlives several phases and runs, so its threads, prompts, replies and pending text live in `~/.local/state/guilds/roles/`, and the census uses a `HeldDesignRole` lifetime tied to that store. The review pool showed the pattern of lasting threads outside run roots. Copying a live roster into another run, or putting a design type inside the review pool, would give one thread two owners.
- **One designer and at most one reviewer per design.** The user's rule: one Astra writes and revises in the same thread; one Astra reviews in one thread. A fresh `codex exec` per round cannot prove one reviewer per design, and a reviewer pool that grows when busy breaks the rule, so a busy reviewer is reported, not replaced. One thread per role keeps the context and the cited rulings across rounds.
- **One natedev authority with explicit checkout mappings.** A URL spelling, a new worktree or an unreachable host must not make a second identity for the same document. Reservation before creation and retries with the same request ID mean an uncertain launch never frees the slot.
- **Heads read records, not every turn.** Reading every member turn costs too much. Set events make the member write, and the daily read uses records already kept (rounds, step time, findings, saved headers), so a head reads turns only where a record looks bad.
- **Tips in Git, one file per tip.** Git already reaches both machines, gives history and needs no copy step, manifest, receipt or delivery state. One file per tip, like the Rust rules, lets Obsidian views index by tag and lets the `head/` tag name one owner. A blocking lock with a deadline lets a publish wait out a load instead of failing at once.
- **No separate member list.** Membership comes from the role a session already holds (skill, role record, prompt header). A second list would drift from the records; join/leave verbs and a guild broadcast add surface with no consumer.
- **Raw pause refused for held roles.** `stop --pause` truncates pending text and drops queued text, and raw `stop` deletes server records without move markers. Held roles release servers only through `_release_held_server`, which parks queues and proves idle first.
- **Patch-only write check.** The berth hook sees `apply_patch` paths before the edit; other tools give no path list, and writers run with full sandbox access. So the guarantee covers patches, and the designer prompt tells designers to edit only by patch.
- **Explicit access policy.** Access must not follow from prompt text. The mesh once inferred read-only access from `instructions_file`; a typed `ThreadAccessPolicy` on the roster entry now keeps access through start, follow, account recovery and adoption. `roster_access_policy` in `codex_mesh.py` is the one reader. An entry from before the policy existed gets its policy by what it is: with `instructions_file` and no `held_design_id` it is a reviewer template or fork (pool or `review.sh --slot`) and gets `ReadOnlyReviewer`; with `held_design_id` it is a damaged held role and refuses, naming `roles.py recover`; with neither it is a worker and keeps its launch sandbox. Each locked roster load stores the reviewer policy on every such entry in the file, so the first write after install migrates a whole pool; a read without the lock writes nothing.
- **Short cited rulings.** Revisions that answered every finding with more mechanism grew a design without a correctness gain. Holders resume the same writer with short rulings that cite files, and change the Work Order only when scope changes.
