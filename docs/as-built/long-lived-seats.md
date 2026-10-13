# Long-lived Codex seats

## What it is

A `/unit:direct` run opens its Codex seats once and keeps them for the rest of the run. Every later phase, repair round, broad review lens, closure review and phase-review architect pass goes to a seat that is already open, with `--to <full-seat-name>`. The exception is a phase on the pool arm of a production with a review pool: its code reviews go to the production's pooled reviewers, which are not seats of the run (`docs/as-built/review-pool.md`). A seat can change role between tasks: a writer becomes a tester, a tester becomes a reviewer, or a writer takes the adversary lens against its own code. Before an open seat gets new work, the launcher compacts its thread if the recorded context is over a threshold. A turn that fills the context window gets the follow-up "Continue where you stopped." When a phase finishes, its turns end but its threads stay open. The threads end only when `end_session.sh` stops the run's app-server. `scripts/whoami/seat_usage.py` checks whether this saves Codex usage. It reads the usage store (`docs/as-built/usage-ledger.md`), compares weighted Codex usage per checkpointed phase before and after the go-live time, and reports account switches per week and credits used per day alongside.

## How it works

### Seat lifecycle in `scripts/agents/codex_mesh.py`

Each run has one detached `codex app-server --listen ws://127.0.0.1:<port>`. Each seat is a named, non-ephemeral thread on it. The verbs:

| Verb | Signature | Behavior |
| --- | --- | --- |
| `start` | `--session-dir --name [--role impl\|test\|fix\|review] [--lens] --cwd --prompt-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--sandbox danger-full-access] [--resident]` | Opens a thread and blocks until its turn ends. `--role` is optional, and roleless threads such as ask-a-friend friends leave it out. A given role, with `--lens` when passed, is appended to `agents.jsonl` for the new thread. |
| `follow` | `--session-dir --to --role (required) [--lens] [--claim-pid] --message-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--cwd] [--read-only] [--image …]` | Runs one more turn on a finished thread. If the old server is gone, it resumes the thread on a new one. `--cwd`, `--read-only` and `--image` apply to that turn only and exist for the review pool; without them the request is the seat's usual one. |
| `prime`, `fork`, `reset` | see `docs/as-built/review-pool.md` | Create a read-only reviewer template with standing instructions, fork a named reviewer from it, and remove every turn from an idle reviewer. Only the review pool calls them. |
| `compact` | `--session-dir --to` | Exits 2 for a seat that is running or busy, a seat with no recorded context size, and a seat whose size is `not_measured_since_compaction` (`size not measured since its last compaction; compact refused`). Otherwise it reads the entry's `revision`, resumes the thread, waits up to 60 s (`COMPACT_IDLE_WAIT_SECS`) for a turn still live on it to end, exits 3 (`COMPACT_TURN_STILL_LIVE`) if one stays live, and calls `thread/compact/start`. It waits until the compaction turn has a `thread/compacted` notice or a `contextCompaction` item, a token-usage update and a `turn/completed`. It then writes the new size under `RosterUnchanged(thread_id, revision)`, so a turn that wrote the entry in between keeps its record. A size above 0 prints `compacted <seat>: <before> -> <after> tokens`; a compaction turn that reports 0 stores `not_measured_since_compaction` and prints `compacted <seat>: <before> tokens, size not measured until its next turn`. A compaction that has not finished in 300 s (`CompactionTimedOut`) is interrupted with `turn/interrupt` and the thread is read until idle, up to the same 60 s: idle is exit 1 with `…; interrupted the compaction turn, the seat is idle and not compacted`, so a follow-up can still be sent; a turn still live is exit 3. |
| `can-follow` | `--session-dir --to --role [--lens] [--claim-pid]` | Refuses a seat that cannot take a follow-up. With `--claim-pid`, it claims the seat under the roster `flock`: it sets `status: starting`, `launcher_pid`, `previous_status`, the new `role`, and `previous_role` (the role the seat held before), and appends the role and lens to the role log `agents.jsonl` (`docs/as-built/usage-ledger.md`). |
| `release-follow` | `--session-dir --to --claim-pid` | Releases a claim that never started a turn. It restores `previous_role`, or removes `role` if the seat had none, returns the seat to its previous status, and appends the restored role to `agents.jsonl`. A follow that did run keeps the role it was claimed with, so after a completed review the seat's role stays `review`. |
| `end` | `--session-dir --to` | Clears the seat's pending queue, writes its end marker and sets the seat's roster `status` to `"ended"`. |
| `stop` | `--session-dir` | Sets every roster seat to `"ended"` and stops the current server and every retired one. |
| `sweep` | `[--root] [--stop]` | Gives a verdict on every app-server listening on `ws://` (see Cleanup). |
| `idle-stop` | `--session-dir --pid --port` | The server's own watcher calls this. It stops the server, or touches its record if something still uses it. |
| `list` | `--session-dir` | Prints the roster. |

`follow` and `start` both send `model` on every `turn/start`. A follow-up therefore runs the configured model of the role the seat was reassigned to, not the model it opened with.

**Roster fields (`mesh_roster.json`).** Each entry has:

- `thread_id`.
- `status`: `starting`, `running`, `done`, `failed`, `waiting_capacity`, `capacity_exhausted`, or the terminal `ended`.
- `role`: `impl`, `test`, `fix` or `review`, or absent for a roleless thread.
- `context_tokens`: `tokenUsage.last.inputTokens` from the turn's latest `thread/tokenUsage/updated`, or `"not_measured_since_compaction"` after a compaction turn that reported 0, until the seat's next turn reports a size.
- `revision`: raised by one on every write inside the roster lock; an entry without one reads as 0. `_update_roster_if_expected` writes only when the entry still matches what the writer expects (still on its thread, absent, ended on its thread, or at an unchanged revision), so a command still at work for an earlier thread, or a compaction that ends late, cannot replace a newer record (`docs/as-built/usage-ledger.md`, Seat roster guards).
- During a claim only: `previous_role` and `previous_status`.
- For a pooled reviewer or its template only: `instructions_file`, whose text is sent again as `developerInstructions` on every resume and fork.

The read-only roster reader `scripts/message/roster.py` (`docs/as-built/session-roster.md`) reads `thread_id`, `status`, `role`, `lens`, `launcher_pid` and `cwd`, and leaves `ended` seats out. A `review` seat with a `lens` field shows as `review (<lens>)`, but no launcher writes `lens` into the mesh roster. `codex_mesh.py --lens` records the lens in `agents.jsonl` only; it also appears in artifact names and in the progress and findings records.

**Overflow press-on.** When a turn ends with `codexErrorInfo: contextWindowExceeded`, `start` or `follow` does three things in the same call:

1. Posts a board status line: `context window filled; sent "Continue where you stopped."`.
2. Starts a continuation turn with that text.
3. Sets a guard flag.

The flag is cleared by any `item/completed` from the seat's own work, but not by a `userMessage` or `contextCompaction` item. A second overflow while the flag is still set stops the seat with `context window filled twice in a row without progress; automatic continuation stopped`. Overflows that have work between them get one continuation each, with no limit.

### Launchers

**`scripts/delegate/implement.sh [--to <seat>] <session_dir> [working_dir] [prompt_file] [task] [role_description] <pass_kind> [pass_activity] [fix_pass] <team_role> [mesh_prefix]`**

- Without `--to`, it opens a new seat named by `seat_name.sh`. After the turn it writes the seat's identity file.
- With `--to`, it follows the open seat after these steps, all under `<session>/impl_seat_claim.lock`:
  1. Checks that the identity file `impl_seat_<team_role>.json` equals `{name, slot, family, model, seat_id}`. `model` is the model configured for the task and `seat_id` is the thread id. A mismatch fails with `has no matching durable seat identity`.
  2. Refuses a seat that another live launcher has claimed (`impl_claim_<slot>.json`) or whose status is `implementing`.
  3. If the roster's `context_tokens` is an integer greater than `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS`, runs `codex_mesh.py compact`; a `not_measured_since_compaction` size skips this step, so the seat gets its follow-up with no second compaction. On success it posts `launcher: compacted …`. On exit 3 (a turn is still live on the thread) it posts `launcher: <seat> still has a live turn after compaction: <detail>; follow-up not sent` and exits 2, naming `codex_mesh.py end --to <seat>` and a fresh seat as the remedy; `review.sh` does the same. On any other failure it posts `launcher: compaction failed for <seat>: <detail>; continuing follow-up` and continues.
  4. Claims the seat with `can-follow --role <pass_kind>`.
- On exit, it releases the claim.
- It reads the threshold from `config/delegate.conf` (`PLAN_DELEGATE_CONFIG` can override the path). A missing, non-numeric or non-positive value exits 2 with an error naming the key.
- A seat assigned a different role keeps its slot as the 9th argument and changes only the task and pass-kind words.

**`scripts/delegate/review.sh [--to <seat> | --slot <new-slot> | --pool code|ui --lease <slug>/<unit>/<phase>] <session_dir> [working_dir] [prompt_file] [task] [role_description] [pass_activity] [pass_index] [lens] [early_ready_file]`**

`lens` must be one of `adversary`, `contract`, `craft` or `ux`. Lens `ux` is the design check: it records no review pass, since the unit director's `UX review` activity carries that time. Artifacts are keyed by pass and lens, never by seat: `review_findings_<N>[_<lens>].txt`, `review_status[_<lens>]`, `review_agent_<N>[_<lens>].log`.

- **`--pool code|ui --lease …`**: reviews on a pooled reviewer of the production, on a read-only turn. `docs/as-built/review-pool.md` describes this path.
- **`--to <seat>`**: reviews on an open seat.
  - It writes one turn record per attempt and marks it `canceled` on TERM, INT or HUP, exiting 143, 130 or 129 (`docs/as-built/review-pool.md`, Turn record). `REVIEW_LEASE=<slug>/<unit>/<phase>` in the environment names the unit and phase in that record.
  - The recorder slot is the seat name's last hyphen segment. It must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`.
  - The family must be Codex with the mesh on.
  - Under `impl_seat_claim.lock`, it compacts when the recorded size is an integer above the threshold (best-effort, one board line), then claims with `can-follow --role review`.
  - The claim happens before any artifact is written, so a refusal for a busy seat cannot overwrite another review's status or findings.
  - It then runs `codex_mesh.py follow --role review`, and releases the claim on exit.
- **`--slot <name>`**: opens a new seat named for its role (`review`, `review2`, `fix2`).
  - The name must match `^[A-Za-z][A-Za-z0-9_]{0,31}$`, so it cannot contain a hyphen.
  - It runs `codex_mesh.py start --role review` with the default `danger-full-access` sandbox.
  - After the turn it writes `impl_seat_<slot>.json` with the configured implementation model of the family (`delegate.<family>` `implementation`, or `impl` if that is not set), not the review model. A later `implement.sh --to` can then give the seat writing or repair work.
  - The seat stays open until `end_session.sh`.
- **Neither flag**: runs the fresh blind reviewer through `agent_exec.sh … readonly`, with no address (`mesh=none`). The skills use this form only for a review outside a run.

**Tree guard (seated and pooled reviews).** Before the turn, `review.sh` records five things:

- `git rev-parse HEAD`
- `git status --porcelain`
- a SHA-256 of `git diff HEAD`
- a SHA-256 over every untracked, non-ignored file's name and content (a symlink's target, for symlinks)
- the changed paths

After the turn it compares the first four. Any difference makes the review status `error`, prints `ERROR: review seat <seat> changed the working tree: <paths>`, posts `blocked` on the board, and keeps the findings file. An early review, which starts while the writer is still running, takes this snapshot when its early-ready sentinel appears.

### Choosing a seat for a review

The rules live in `commands/unit/direct.md` `<LongLivedSeats/>`, `docs/delegate/dual_review.md` (`<TeamReview/>` and `<ReviewPromptContract/>`), `docs/delegate/run_phase_review.md`, `docs/delegate/write_prompt_contract.md` (`<RoleReassignment/>`) and `commands/plan/phase_review.md`:

- Each lens gets a different open seat, preferring one that did not write the files under review. If the other seats are busy, the writer may take the `adversary` lens on its own work.
- Roles move between seats; slots do not. Any open seat may take any role, including in the middle of a phase. Before dispatching, post a board `handoff` that names the slot, the role it leaves and the role it takes. The new prompt has the seat run `board.sh role` before its first tool call.
- If every open seat is busy, open an extra seat named for its role with `--slot` (`review`, then `review2`; `fix2` for repair) and keep it until `end_session.sh`. Never name an extra seat with a bare number.
- Only the adversary lens starts early, and the director records its seat in `EARLY_REVIEW_SLOT`. The closure review and the phase-review architect go to an open seat with `--to`.
- Every review prompt opens with a role switch: "You are slot <slot>, now working as a reviewer. Edit nothing. Read the diff cold as if someone else wrote it. If you wrote any of the code, argue against your own choices as an adversary would." The prompt also gives the concrete `board.sh role … review` command for the seat's first tool call.
- Every prompt carries the Three Gods sentence written out in full: opening, later-phase, repair, review and follow-up prompts alike. It also carries the Type Design Contract, verbatim for a new seat or as the allowed pointer for a follow-up. Compaction can erase earlier prompt text, which is why each prompt repeats them.
- A new seat opens only when no open seat can take the work: the assignment needs more seats, every open seat is busy, or a reusable thread is gone. A replacement under the same name starts fresh and gets an opening prompt.

`scripts/delegate/progress_history.py` gives each extra seat (`review2`, `fix2`, …) its own `Agent N` column after `impl` and `test`, in the order seats first appear. A seat appears when it records a pass or receives a board handoff. The original `review` seat keeps its earlier rule: it shows only while a displayed stage seats it.

### Cleanup

- **Phase end.** `<PhaseCleanup/>` runs `remove_seats.py --session-dir`. That removes Claude seats only and leaves Codex threads open. After a successful Claude session listing, and only under the real delegate root, it runs `codex_mesh.py sweep --stop` (best-effort). A sweep failure prints one line and seat cleanup continues.
- **Run end.** `scripts/delegate/end_session.sh` takes these steps in order:
  1. `remove_seats.py`
  2. `codex_mesh.py stop`, whenever `mesh_roster.json`, `mesh_server.json` or `mesh_retired.json` exists
  3. `sweep --root <delegate root> --stop`
  4. removal of `verify_cache/`
  5. removal of the run's notifier
  6. removal of the run-active marker

  Each step continues after a failure.
- **Sweep.** It first decides who owns each `ws://` app-server: the current server of a run, a retired server of a run, an unowned server whose log names its run, or an unowned server with no log. It then judges whether anything still needs the server:
  - Any connected client means in use.
  - For a current server, a run-active marker or a running seat means in use.
  - A retired server is unused unless a conversation is mid-turn.
  - An unowned server is in use during the record-start window, or while a conversation is mid-turn.
  - Otherwise, a live seat launcher, a server under 30 minutes old, a run folder changed in the last 30 minutes, or a conversation mid-turn means in use.

  It prints `pid  port  verdict  reason  owner` with verdict `in use`, `unused`, `stopped` or `kept`. With `--stop`, it judges a second time under the run folder's server lock before stopping a server. Servers listening on `--listen unix://` are never touched. Each server also has its own watcher, which wakes every 300 s and runs `idle-stop` once the server record has gone 30 minutes untouched. If a server fails to start, its process is terminated and its record dropped.

### Measurement: `scripts/whoami/seat_usage.py`

`seat_usage.py` is a view over the usage store (`docs/as-built/usage-ledger.md`, Seat readout). It reads no rollout, ssh, git or readings file; the hourly collector stores Codex requests from both machines, compactions, edits, checkpoints, account readings and account role changes, and the readout opens the store read-only through `usage_report.open_read_store`.

- **Modes.** `[--db PATH]` with `--before <start> <end> --after <start> <end>`, or `--daily [--t-live <time>]`. In `--daily` mode, the before window is the 7 days before T_live and the after window runs from T_live to now. T_live defaults to `2026-10-10T18:17:00Z` (`DEFAULT_T_LIVE`). `--t-live` is accepted only with `--daily`. A bad window exits 2; a store it cannot open or query exits 1 with one `seat_usage.py:` line.
- **Weights.** Dated per-model weights from `scripts/usage/weights.py`, applied as grouped SQL through `weights.sql_weights`. A window is cut into pieces at each dated weight entry that starts inside it for a model with Codex turns there (`After, from 2026-10-10 21:35Z` for `gpt-6.1-sol`). `Weighting:` heads the report with each weight version used (`<model>[@<start>]: 1 / <cached> / <output>`); `Unweighted:` names each model with no entry and its turn count.
- **Table, per window piece.**
  - threads opened
  - weighted usage
  - share of usage before each thread's first edit, over threads that ever edited
  - compactions and compactions per active thread (a thread with any opening, turn, edit or compaction in the piece)
  - checkpointed phases
  - account switches per week
  - credits used per day
- **Verdict line.** `Weighted usage per checkpointed phase: before X; after Y; up|down N.N%`, or `unchanged` when the change is under 0.05%. It compares the whole before window with the last after piece. A parenthesis notes `different weights: compare roughly`, unweighted turns left out of each side, and `Mac as of <time>` or `Mac never read in full` when the store's Mac Codex source is not `ready`. The `Sources` block (`usage_report.sources`, in UTC) sits above the verdict.
- **Unavailable states.**
  - Switches show `—` when the window has no readings.
  - Credits show `no credit history` or `insufficient credit history`.
  - A window with no checkpoints shows `unavailable`.
  - A zero before-value gives `change unavailable`.
- **Switch counting.** A switch is a change of the account holding the `seats` role (`accounts.CODEX_ROLE`) in the store's `account_role_changes` (`store_switch_history`). Before the stored role history begins, a change of `codex …` account between consecutive readings still counts. The total is scaled to per week over the time the readings cover.
- **Credit counting.** Credits used is the sum, per account, of balance decreases between consecutive `account_readings` rows.
- **Credit readings.** `scripts/whoami/agent_notes.py` stores `credits` with each Codex reading, and the collector copies the readings into `account_readings`. The value is the note's `credit_balance` if it is a finite, non-negative number.
- **Edit detection (`scripts/usage/rollouts.py` `is_edit`).** The collector stores an `edits` row for each record that counts as an edit:
  - an `apply_patch` custom tool call
  - a JSON input whose `command`, `name` or `tool` is `apply_patch`
  - input starting with `apply_patch `
  - `apply_patch(` or `tools.apply_patch(` in code, outside JavaScript string literals and template text, where `${…}` expressions still count as code

### Key files

| File | Role |
| --- | --- |
| `scripts/agents/codex_mesh.py` | Seat threads, roster, compaction, overflow press-on, role claim and restore, sweep |
| `scripts/delegate/implement.sh` | Writer, tester and repair dispatch; `--to` reuse with identity check, claim and compaction |
| `scripts/delegate/review.sh` | Reviews: `--to` reuse, `--slot` new seat, `--pool` pooled reviewer, tree guard, fresh read-only fallback |
| `scripts/delegate/end_session.sh` | Ends the run's seats (`stop`) and sweeps |
| `scripts/delegate/remove_seats.py` | Removes Claude seats at phase end; sweeps after a successful listing |
| `scripts/delegate/progress_history.py` | Agent columns for extra seats |
| `scripts/whoami/seat_usage.py` | Before/after usage report over the usage store |
| `scripts/usage/weights.py` | Dated per-model weights the report applies |
| `scripts/usage/rollouts.py` | `is_edit`, used by the collector |
| `scripts/whoami/agent_notes.py` | `credits` on Codex readings |
| `config/delegate.conf` | `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS=100000` |
| `commands/unit/direct.md` | `<LongLivedSeats/>`, referenced from `<LaunchImplementation/>`, `<FixDispatch/>`, `<PhaseCleanup/>` and `<CompactionContract/>`; early review seat (`EARLY_REVIEW_SLOT`) |
| `docs/delegate/launch_implementation.md` | `codex_mesh.py list`, then `--to` for every slot with an open seat |
| `docs/delegate/dual_review.md`, `docs/delegate/run_phase_review.md`, `commands/plan/phase_review.md` | Review seat selection and `--to`/`--slot` calls |
| `docs/delegate/write_prompt_contract.md` | `<RoleReassignment/>`; Three Gods and Type Design in every prompt |

## Invariants

- Codex seats open once per run and end only at `end_session.sh`. Phase cleanup never ends a Codex thread. A new seat opens only when no open seat can take the work.
- Claude seats keep their per-phase lifecycle; this design covers Codex seats only.
- Only a launcher starts a seat's next turn. A peer message to a finished Codex turn is refused; messages are for questions only.
- Compaction, the seat claim and the identity check run under one lock (`impl_seat_claim.lock`). Compaction is best-effort: a failure posts one board line and never blocks the follow-up.
- A seat's identity file is anchored to the implementation model, whichever launcher opened the seat.
- A seated review claims its seat before writing any artifact, and fails if the working tree changed in any of HEAD, status, tracked diff or untracked content.
- Review artifacts are keyed by pass and lens; the recorder pass is keyed by the slot that ran it (`pool_<reviewer>` for a pooled review), and a review with lens `ux` records none.
- Every prompt to a seat, follow-ups included, repeats the Three Gods sentence and the Type Design Contract, verbatim or by pointer.
- Only the seat's own work items clear the overflow guard; `userMessage` and `contextCompaction` items never do.
- `sweep` treats every doubt as in use, judges a second time under the owner's lock before stopping, and never touches the `unix://` daemon. A sweep failure never stops seat cleanup.
- `seat_usage.py` reads only the usage store and writes nothing. It prints an explicit unavailable state where a value has no data, and its verdict names unweighted turns left out and the Mac's freshness when the Mac was not read in full.
- A roster writer that changes an entry in place advances `revision`; `compact` writes its size only when the revision it read is unchanged.

## Calibration / gotchas

- **Compaction threshold**: 100,000 tokens of a 258,400-token window. The comparison is strictly greater than the threshold, on the roster's last `context_tokens` when it is an integer. The compaction replay (`scripts/usage/compaction_replay.py`, `docs/as-built/usage-ledger.md`) found every higher limit used more weighted tokens, so the threshold stays 100,000.
- **Weights**: dated per model in `scripts/usage/weights.py`. `gpt-5.6-sol` is 1 / 0.1 / 8 for uncached input, cached input and output; `gpt-6.1-sol` is 1 / 0.05 / 5 from 2026-10-10T21:35Z.
- **Idle rule**: 30 minutes (`SERVER_IDLE_SECS = 1800`). The watcher wakes every 300 s.
- **Slot patterns**: `--slot` must match `^[A-Za-z][A-Za-z0-9_]{0,31}$`. A slot derived from `--to` must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`.
- **T_live**: 2026-10-10T18:17:00Z. The before window is the 7 days before it.
- The real app-server emits an `item/completed` for the submitted `userMessage` on every `turn/start`, so the continuation's own message must not count as progress. The fake server in the tests emits it too.
- Compaction is finished at the compaction turn's `turn/completed`, not at `thread/compacted`. Either `thread/compacted` or a `contextCompaction` item identifies that turn.
- A Codex compaction turn reports 0 input tokens, so a launcher's compaction line reads `compacted <seat>: <before> tokens, size not measured until its next turn` and the roster holds `not_measured_since_compaction` until the seat's next turn. Storing 0 would hide the size, and keeping the size from before would compact the seat before every follow-up.
- Real Codex edits arrive as `exec` custom tool calls whose JavaScript calls `tools.apply_patch(`, not as `apply_patch` tool calls.
- A run with no run-active marker, such as ask-a-friend friends or a Codex-hosted director, keeps its server only while there is a client, a launcher, a running seat, or activity in the last 30 minutes. A stopped server comes back on the next use, and its threads resume.
- `--to` takes the slot from the last hyphen segment of the seat name, so `--slot` refuses hyphens.
- A seat opened by `review.sh` must record its identity with the implementation model, not the review model, or `implement.sh --to` refuses it.
- The prior role is restored only when a claim is released before its turn ran. A seat that completed a review keeps `role: review` until its next claim.

## Why

- **Open once.** Before this design, each phase opened new `impl` and `test` threads and every review was a new thread, so every task paid a seat's startup cost. A seat that stays open pays that cost once per run.
- **Compact between tasks.** Startup turned out to be a small share of usage. Over 1,437 natedev threads, usage before the first edit was 19% of uncached input, 4% of cached input and 10% of output: about 6% of weighted usage. Most usage is cached context that long threads read again on every call. A long-lived seat saves its startup but carries its context, so frequent compaction is what keeps the saving.
- **Codex seats only.** The cost being addressed is Codex worker startup. Claude seats are the fallback family and keep their existing lifecycle.
- **Reviews on open seats, without the read-only sandbox.** A review on an open seat runs on that thread's existing sandbox, and a seat may review its own work. The tree guard enforces "edits nothing" in place of the sandbox. A pooled reviewer is not a seat: its turn is read-only and the tree guard still runs after it. The fresh read-only reviewer remains only for reviews outside a run.
- **Verdict per checkpointed phase.** The amount of work changes from day to day, so usage per day would not compare the two windows. Account switches are too rare for a verdict: the baseline had 2 in about five days, too few to show a change within a week. They are reported alongside the verdict.
- **Hyphens refused in `--slot`.** `--to` derives the slot from the seat name, so refusing hyphens is simpler than resolving a hyphenated slot from its recorded identity.
- **Second-overflow error kept.** It stops a seat that fills its context again and again without doing work, so press-on cannot loop forever.
- **`tools.apply_patch(` kept in edit detection.** That call is how real edits appear in rollouts. Detection skips mentions inside strings, so a quoted mention does not count as an edit.
