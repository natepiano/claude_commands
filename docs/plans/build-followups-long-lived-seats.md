# Long-lived seats: Codex workers stay open for the whole run

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A unit director opens its Codex seats once per run and hands every later phase, repair and review to the seats already open, switching their roles as the work needs, compacting them between tasks, and ending them only when the run ends. Then it measures whether Codex credits and weekly usage drop.

> **Production: build-followups** — unit `model-study-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-10: "i think it's probably likely that we spend a lot of tokens starting and stopping our codex workers while they come up to speed. i think we should move immediately to a model where all unit directors keep their workers alive and re-use them - assigning the same task/role to the same unit when feasible but also allwing them to swithc roles if they need a tester to become a reviewer - or asking a model to become an adversary to itself - i want you to work with model-study to roll this out as soon as feasible and then i want model study to incorporate measuring whether we slow down our credit usage and whether we slow down our overall weekly usage (based on how often we have to swtich codex accounts) - workers need to be long lived so they don't pay startup costs anymore. when they run out of context the unit director can just message them to pres on so they automatically compact - they have relatively limited capacity anyway so frequent compacts is a good thing. and we need to update our unit director direct skill to make sure that this is clear - and any other skill area that needs to be updated"

natedev, 2026-10-10: rollout phases first, measurement last; measurement must not hold the rollout; take the baseline from what is already logged, before the go-live commit; seats write the code; once merged, natedev tells every unit director on natedev and the Mac.

## What exists today (checked 2026-10-10 by the plan author)

- **Codex seats are threads on one app-server per run** (`scripts/agents/codex_mesh.py`). `start` opens a named thread and blocks until its turn ends; `follow` runs one more turn on a finished thread (resuming it on a new server if the old one is gone); `end` finishes a resident thread; `stop` stops the run's server. `implement.sh --to <seat>` already sends a follow-up to an open seat through `follow`, guarded by `can-follow` / `release-follow`.
- **Threads already outlive their phase, but nothing reuses them.** Each phase's <LaunchImplementation/> (`docs/delegate/launch_implementation.md`) runs `implement.sh` without `--to`, so it opens two new threads (`impl`, `test`). `remove_seats.py` at phase end removes Claude seats only; Codex threads die with the server at `end_session.sh`. Follow-ups reach an open seat only inside a repair round (<FixDispatch/> in `commands/unit/direct.md`).
- **Every review is a new thread.** `scripts/delegate/review.sh` launches a blind reviewer: a new read-only session with no peers and no address, one per lens (adversary, contract, craft) and one per closure review.
- **Compaction.** Codex compacts a thread on its own when its context fills (seen in rollouts: a seat's context fell from 233k to 43k tokens mid-run). The app-server also takes `thread/compact/start` (codex 0.162.0 schema), which no script calls yet.
- **Startup is a small share of Codex usage.** Over 2026-10-01..10, natedev's 1,437 Codex threads (`~/.codex/sessions/2026/10`) spent, before their first edit: 19% of uncached input, 4% of cached input and 10% of output. Weighting uncached 1 : cached 0.1 : output 8, that is about 6% of Codex usage. Most usage is cached context re-read on every call by long threads, so a long-lived seat saves its startup but carries its context; frequent compaction is what keeps that cost down.
- **Baseline already on disk.** `~/.local/state/agent-notes/readings.jsonl` (logged-in account every 2 minutes, kept 8 days) shows 2 Codex account switches between 2026-10-05 19:25 and 2026-10-10 16:18 UTC; a copy is at `~/.local/state/usage-model/seats-baseline/readings-2026-10-10.jsonl`, with the startup script `startup2.py` beside it. Codex rollouts keep full history on both machines. The credit balance is written only to `~/rust/hanadocs/agents/codex *.md` (`credit_balance`), with no history.

## Decisions (unit director)

- **A run opens its seats once.** The first phase opens `impl` and `test` as today; every later phase, repair and review goes to an open seat with `--to`. A new seat opens only when no open seat can take the work: the seat count the Work Order's `Seats:` field asks for is higher than the open count, or a seat's thread is gone. Seats end at `end_session.sh`, as today.
- **Same role when it fits, any role when needed.** A phase's writer stays the writer and its tester the tester when the next Work Order splits the same way; otherwise the unit director reassigns by the existing <RoleReassignment/> handoff. A tester becomes a reviewer, and a writer may review its own work as an adversary to itself (user).
- **Reviews go to open seats.** A broad review's lenses and a closure review are sent to open seats in a reviewing role. The review prompt keeps the blind-reviewer contract that can be kept: the diff, the Work Order and the instruction to read cold. A seat in a reviewing role edits nothing; the launcher checks the tree is unchanged after the turn and fails the review if it is not. The read-only sandbox goes away for these reviews (user's call: a seat may review its own work).
- **Compact between tasks, and on overflow press on.** Before handing an open seat new work, the launcher compacts the thread when its last context is over `COMPACT_ABOVE_TOKENS` (start at 100,000 of the 258,400 window; Phase 1 records the number used). A turn that ends because the context filled is followed at once with "Continue where you stopped." (user). Both are logged on the board.
- **Codex seats only.** Claude seats keep today's lifecycle; the request names Codex workers, and Claude seats are the fallback family.
- **Measurement uses usage per checkpointed phase**, not per day, because the amount of work changes day to day. Account switches are reported, but two in five days is too few to show a change in one week, so the weighted Codex usage per phase is the verdict and switches are context.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills and scripts. This plan makes seats outlive their phase (Phase 1), measures the effect (Phase 2), and sends reviews to open seats (Phase 3). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-model-study` on branch `build-followups-model-study` (unit `model-study-unit` of production `build-followups`).
- **Project started:** 2026-10-10T16:20:12+00:00
- **Stack:** Python 3.13 standard library, bash, zsh; codex-cli 0.162.0 app-server.
- **Layout:**
  - `scripts/agents/codex_mesh.py` — `compact` verb; context size on the roster; press-on after overflow (Phase 1)
  - `scripts/delegate/implement.sh` — reuse an open seat for a new phase; compact before new work (Phase 1)
  - `scripts/delegate/review.sh` — `--to <seat>` review on an open seat, tree-unchanged check (Phase 3)
  - `commands/unit/direct.md`, `commands/unit/delegate.md`, `commands/unit/report.md`, `docs/delegate/launch_implementation.md`, `docs/delegate/phase_end.md`, `docs/delegate/write_prompt_contract.md`, `docs/delegate/dual_review.md`, `docs/delegate/run_phase_review.md`, `docs/production_format.md` — the skill text (Phases 1 and 2)
  - `scripts/whoami/seat_usage.py` — the measurement (Phase 2, new)
- **Verification:** `python3 -m unittest discover -s scripts/agents -p 'test_*.py'`, `python3 -m unittest discover -s scripts/delegate -p 'test_*.py'`, `python3 -m unittest discover -s scripts/whoami -p 'test_*.py'`; basedpyright 0 errors, 0 warnings, 0 notes on every changed Python file (it exits 3 on the missing `.venv` notice, which is expected); `bash -n` on changed shell scripts.
- **Live checks** run a scratch session directory under the scratchpad with one small Codex thread; never a real unit's session, never `--to user`.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 2 (verdict run only) | Phase 1 merged to main and natedev has told every unit director on natedev and the Mac (T_live) | 7 days after T_live |

## Phases

### Phase 1 — Seats stay open from phase to phase and compact between tasks · status: done

#### As-built

- Codex seats stay open from phase to phase until `end_session.sh`. When the run has open `impl` and `test` seats (`codex_mesh.py list`), the next phase's prompt goes to each with `implement.sh --to <full seat name>`; a new seat opens only for a slot with no open seat. The prompt says the seat is continuing in a new phase, names its role, and says its earlier work is committed.
- `codex_mesh.py compact --session-dir <dir> --to <seat>` refuses a running seat, calls `thread/compact/start` on a finished one, waits for the compaction turn's `turn/completed`, and prints `compacted <seat>: <before> -> <after> tokens`.
- `implement.sh --to <seat>` compacts the seat first, under the seat-claim lock and best-effort, when its roster `context_tokens` exceeds `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` (`config/delegate.conf`, `100000`; missing or non-positive is an error naming the key), then follows; the board gets one line with the sizes.
- A turn that ends on `contextWindowExceeded` is pressed on once, inside the same `start` or `follow`, with "Continue where you stopped."; a second overflow with no progress is an error.
- Roster fields: `role` (`impl` | `test` | `fix` | `review`; optional — roleless threads such as ask-a-friend friends omit it; `--role` is optional), `context_tokens` (`last_token_usage.input_tokens` from the turn's final token count), and `status: "ended"` written by `end` for its seat and by `stop` for every seat on the run's servers (`running` and `done` keep their meanings). `can-follow` saves the prior role and a released claim restores it.
- `end_session.sh` runs `stop` whenever a roster or server record exists.
- `direct.md` holds the <LongLivedSeats/> contract (open once, same role when it fits, compaction, press-on, a new seat only when none can take the work), referenced from <LaunchImplementation/>, <FixDispatch/>, <PhaseCleanup/> and <CompactionContract/>. `write_prompt_contract.md` requires every prompt to carry the Three Gods inline and Type Design verbatim, or by pointer in follow-ups. `report.md`'s seat table names the phase a seat opened in when it is older than the current phase.

**Files:**
- `scripts/agents/codex_mesh.py` — `compact` verb, roster fields, overflow press-on, role save/restore
- `scripts/agents/test_codex_mesh.py` — tests for the above
- `scripts/delegate/implement.sh` — `--to` compaction above the threshold
- `scripts/delegate/test_implement_launcher.py` — launcher tests
- `scripts/delegate/end_session.sh` — `stop` whenever a roster or server record exists
- `scripts/delegate/test_end_session.py` — end-session tests
- `config/delegate.conf` — `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS=100000`
- `commands/unit/direct.md`, `commands/unit/delegate.md`, `commands/unit/report.md`, `docs/delegate/launch_implementation.md`, `docs/delegate/phase_end.md`, `docs/delegate/write_prompt_contract.md`, `docs/production_format.md` — seat-life text for long-lived seats

**Binds later work:** `shutdown-unit`'s read-only roster reader (`scripts/message/roster.py`) consumes `role`, `context_tokens` and status `ended`. The review-reuse phase adds `lens` while `role` is `review` and inherits the role, claim and compaction rules above. **Measure: Codex usage per phase, credits and account switches, before and after; overflow guard fix** owns the overflow guard repair with its regression.

**Gotchas:** the overflow guard is cleared by the continuation's own `userMessage` item, so the second-overflow error never fires live. Compaction completes on `turn/completed`, not `thread/compacted`.

**Ruled out:** dropping the second-overflow error — kept as a loop guard.

### Phase 2 — Measure: Codex usage per phase, credits and account switches, before and after; overflow guard fix · status: todo

#### Work Order

**Blocked by:** G1, for the verdict run only (T_live 2026-10-10T18:17:00Z, natedev's go-live message to every unit director). The script and the credit logging are built right after Phase 1, ahead of the review phase, so the daily updates the user asked for (2026-10-10: "i want to get updates at least daily over the next 7 days") use it from go-live; the unit director runs it every day of the 7.

**Goal:** a report says whether Codex usage per checkpointed phase fell after T_live, and by how much, with credits and account switches beside it.

**Spec:**
- `scripts/whoami/seat_usage.py --before <start> <T_live> --after <T_live> <end>` reads Codex rollouts on natedev and the Mac (`natemccoy@mac`, by `ssh`), and for each window prints: threads opened; weighted Codex usage (uncached 1 : cached 0.1 : output 8, the ratio of OpenAI's published prices for the seat model, read on the day and named in the output); the share spent before each thread's first edit; compactions; and phases checkpointed (commits whose subject starts `checkpoint(` on all branches of `~/.claude` and of the repositories the production rows name). The verdict line is weighted usage per checkpointed phase, before and after, with the change in percent.
- Codex account switches per week from the readings log, with the baseline copy in `~/.local/state/usage-model/seats-baseline/` for the days the live log has dropped.
- Credits: `agent_notes.py` adds `credits` to each Codex reading from this phase on; the report prints credits used per day where the log has them, and says the before window had no credit history.
- natedev gets the verdict line and the table; NOTES.md gets the same.
- **No orphaned app-servers (showrunner, 2026-10-10).** On natedev, 5 `codex app-server` processes up 1–2 days are named by no `mesh_server.json`, and the idle stop never reaches them. (1) Replacing a run's `mesh_server.json` (the `retrying on a new app-server` path, a sign-in retirement, or any new server for a run) stops the server the old record named, or lists it in `RETIRED_FILE` so the idle watcher stops it once its turns finish; it never leaves it running unowned. (2) `codex_mesh.py sweep --stop` also stops a `codex app-server --listen ws://` that no record under the sweep root names, and a recorded server whose run has ended (no run-active marker under `active/` names its folder and no roster seat is `running`), keeping the existing two-looks rule and never touching the `--listen unix://` daemon or a server another live record names. Something already scheduled runs it (find the existing sweep caller; if none runs it periodically, run it from `remove_seats.py` at phase end and from `end_session.sh`). (3) After the merge to main, a `sweep --stop` run clears the 5 orphans and the ended runs among the recorded servers; the unit director runs it and reports the counts to natedev.
- **Overflow guard fix (carried from Phase 1's closure review).** `codex_mesh.py` clears the one-continuation overflow guard on any non-compaction `item/completed` (around line 1944), and the continuation's own `userMessage` item is one, so on a real server a second overflow with no work between is pressed on forever. Count only the seat's own work items as progress (not `userMessage`, not `contextCompaction`). Make the fake server in `test_codex_mesh.py` emit the submitted `userMessage` `item/completed` on every `turn/start`, as the real app-server does, and add a regression that fails on the old condition.

**Files:**
- `scripts/agents/codex_mesh.py`
- `scripts/agents/test_codex_mesh.py`
- `scripts/whoami/seat_usage.py`
- `scripts/whoami/test_seat_usage.py`
- `scripts/whoami/agent_notes.py`
- `scripts/whoami/test_agent_notes.py`

**Seats:** `2 writers` — `impl` fixes the overflow guard in `codex_mesh.py` and its tests, then reviews the report's tests; `test` writes `seat_usage.py`, the `agent_notes.py` credits field, and their tests.

**Constraints from prior phases:** Phase 1: compaction is counted from the rollouts (`thread/compacted` / `contextCompaction` items), not the board; `implement.sh` posts a board line when it compacts a seat; the roster's `context_tokens` holds each seat's last known context size.

**Acceptance gate:** the verification lines green; the report over the real windows exits 0 and prints every column.

### Phase 3 — Reviews go to open seats · status: todo

#### Work Order

**Goal:** a phase's broad review lenses and its closure reviews run on the seats already open, in a reviewing role, instead of new reviewer threads.

**Spec:**
- `review.sh --to <seat>` sends the review prompt as a follow-up to an open seat (compacting first, as Phase 1 built), records the pass under that seat, and writes the same findings files as today. After the turn, the working tree must be unchanged (`git status --porcelain` and the diff hash before and after); a change fails the review with `error` and names the seat.
- Without `--to`, `review.sh` works as today; a run with no open seat (a solo review outside a run) still uses it.
- The review prompt (<ReviewPromptContract/>, <BroadReviewPrompt/>, <ClosureReview/>) gains a role-switch opening: the seat is now a reviewer, edits nothing, reads the diff cold as if someone else wrote it, and, when it wrote the code, argues against its own choices as an adversary would.
- `dual_review.md`, `run_phase_review.md` and `direct.md` (<DualReview/>, <EarlyReviewArm/>, <FixDispatch/>'s closure review) say which open seat takes which lens: the seat that did not write a file reviews it when one exists; the writer takes the adversary lens on its own work when the other seat is busy. The handoff post per <RoleReassignment/> names the move.

**Files:**
- `scripts/delegate/review.sh`
- `scripts/delegate/test_review_launcher.py`
- `commands/unit/direct.md`
- `docs/delegate/dual_review.md`
- `docs/delegate/run_phase_review.md`
- `docs/delegate/write_prompt_contract.md`

**Seats:** `2 writers` — `impl` writes `review.sh` and its tests; `test` writes the skill text.

**Constraints from prior phases:** Phase 1: `codex_mesh.py compact` waits for the compaction turn's `turn/completed` and refuses a running seat; `implement.sh --to` compacts under the seat-claim lock above `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` (`config/delegate.conf`), best-effort; the roster records `role` (optional; roleless threads such as friends omit it), `context_tokens` and status `ended`; `can-follow` saves the prior role and a released claim restores it. Every prompt to a seat, follow-ups included, carries the inline Three Gods sentence and the Type Design Contract or its pointer (`write_prompt_contract.md` sections 7 and 9). A replacement thread under the same seat name gets an opening prompt.

**Acceptance gate:** the verification lines green; a live check in a scratch session: an open seat takes a review with `review.sh --to`, writes findings, and a second review whose seat edits a scratch file fails with `error`.
