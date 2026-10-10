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

### Phase 2 — Measure: Codex usage per phase, credits and account switches, before and after; overflow guard fix · status: done

#### As-built

- `scripts/whoami/seat_usage.py --before <start> <end> --after <start> <end>`, or `--daily [--t-live <time>]` (the 7 days before T_live 2026-10-10T18:17:00Z against T_live to now), reads Codex rollouts on natedev and the Mac (`natemccoy@mac`, by `ssh`). Per window it prints threads opened, weighted Codex usage (uncached input 1 · cached input 0.1 · output 8, named in the output), the share spent before each thread's first edit, compactions, and phases checkpointed (`checkpoint(` commits on all branches of `~/.claude` and the repositories the production rows name). The verdict line is weighted usage per checkpointed phase, before and after, with the change in percent; beside it are Codex account switches per week (readings log, plus the baseline copy in `~/.local/state/usage-model/seats-baseline/`) and credits used per day.
- Missing data prints an explicit unavailable state (Mac unreachable, no switch history, insufficient credit history) instead of a number; these states appear in the daily update text.
- `agent_notes.py` records `credits` with each Codex reading.
- The overflow guard in `codex_mesh.py` counts only the seat's own work items as progress — not `userMessage`, not `contextCompaction` — so a second overflow with no work between is an error on a real server.
- `codex_mesh.py sweep [--stop]` judges every `codex app-server --listen ws://` process by ownership (current, retired, unowned with or without a log): connected clients first, then the run-active marker, running seats, launchers, age and quiet time, then live conversations. It prints a verdict per server (`in use`, `unused`, `stopped`, `kept`), takes a second look under the owner's lock before stopping, and never touches the `--listen unix://` daemon. A failed server start terminates its process and drops its record.
- `remove_seats.py` runs `sweep --stop` after a successful session listing; `end_session.sh` runs it after `stop`. A sweep failure never stops seat cleanup.

**Files:**
- `scripts/whoami/seat_usage.py` — the before/after report and `--daily`
- `scripts/whoami/test_seat_usage.py` — report tests, unavailable states included
- `scripts/whoami/agent_notes.py`, `scripts/whoami/test_agent_notes.py` — `credits` field
- `scripts/agents/codex_mesh.py` — overflow guard, `sweep`, failed-start cleanup
- `scripts/agents/test_codex_mesh.py` — the fake server emits the submitted `userMessage` `item/completed` on every `turn/start`, as the real app-server does; guard regression; sweep verdict tests
- `scripts/delegate/remove_seats.py`, `scripts/delegate/end_session.sh` (and `test_remove_seats.py`, `test_end_session.py`) — sweep callers

**Binds later work:** **Reviews go to open seats** narrows `seat_usage.py` `is_edit` so a quoted mention of `tools.apply_patch(` is not an edit, keeping the pattern for real `exec` calls.

**Gotchas:** real Codex edits arrive as `exec` custom tool calls whose JavaScript calls `tools.apply_patch(`, not as `apply_patch` tool calls; edit detection must match that call. A run with no run-active marker (ask-a-friend friends, a Codex-hosted director) keeps its server only while a client, launcher, running seat or under 30 minutes of activity exists; a stopped server comes back on the next use and its threads resume.

**Ruled out:** dropping the `tools.apply_patch(` pattern from edit detection — it is how real edits are logged.

### Phase 3 — Reviews go to open seats · status: done

#### Work Order

**Goal:** a phase's broad review lenses and its closure reviews run on the seats already open, in a reviewing role, instead of new reviewer threads.

**Spec:**
- `review.sh --to <seat>` sends the review prompt as a follow-up to an open seat (compacting first, as Phase 1 built), records the pass under that seat, and writes the same findings files as today. After the turn, the working tree must be unchanged (`git status --porcelain` and the diff hash before and after); a change fails the review with `error` and names the seat.
- Without `--to`, `review.sh` works as today; a run with no open seat (a solo review outside a run) still uses it.
- The review prompt (<ReviewPromptContract/>, <BroadReviewPrompt/>, <ClosureReview/>) gains a role-switch opening: the seat is now a reviewer, edits nothing, reads the diff cold as if someone else wrote it, and, when it wrote the code, argues against its own choices as an adversary would.
- `dual_review.md`, `run_phase_review.md` and `direct.md` (<DualReview/>, <EarlyReviewArm/>, <FixDispatch/>'s closure review) say which open seat takes which lens: the seat that did not write a file reviews it when one exists; the writer takes the adversary lens on its own work when the other seat is busy. The handoff post per <RoleReassignment/> names the move.
- Seat management (user, 2026-10-10): reviewers are long-lived like writers. Moving any open seat to another role is allowed, mid-phase included, because all of a unit's seats work in the same area; `RoleReassignment` and <LongLivedSeats/> say so and drop any wording that keeps a busy seat from being reassigned. An extra seat when every open one is busy is allowed and kept for reuse (an idle seat costs no context). The unit director names each seat for its role — a new seat's slot name says what it does (`review`, `fix2`), never a bare number — and `direct.md` says so.
- Carried from Phase 2's closure (minor): `seat_usage.py` `is_edit` counts a quoted mention such as `rg -F 'tools.apply_patch(' scripts` as an edit. Real Codex edits arrive as an `exec` custom tool call whose JavaScript calls `tools.apply_patch(` (647 of the last 30 rollouts' calls opened `const patch = "***`), so the pattern stays and is narrowed to a call outside a string literal. Tests pin the real `exec` shape as an edit and the quoted mention as not one; each must fail on the current code.

**Files:**
- `scripts/delegate/review.sh`
- `scripts/delegate/test_review_launcher.py`
- `commands/unit/direct.md`
- `docs/delegate/dual_review.md`
- `docs/delegate/run_phase_review.md`
- `docs/delegate/write_prompt_contract.md`
- `scripts/whoami/seat_usage.py`
- `scripts/whoami/test_seat_usage.py`

**Seats:** `2 writers` — `impl` writes `review.sh` and its tests; `test` writes the skill text and the `is_edit` repair with its tests.

**Constraints from prior phases:** Phase 1: `codex_mesh.py compact` waits for the compaction turn's `turn/completed` and refuses a running seat; `implement.sh --to` compacts under the seat-claim lock above `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` (`config/delegate.conf`), best-effort; the roster records `role` (optional; roleless threads such as friends omit it), `context_tokens` and status `ended`; `can-follow` saves the prior role and a released claim restores it. Every prompt to a seat, follow-ups included, carries the inline Three Gods sentence and the Type Design Contract or its pointer (`write_prompt_contract.md` sections 7 and 9). A replacement thread under the same seat name gets an opening prompt.

**Acceptance gate:** the verification lines green; a live check in a scratch session: an open seat takes a review with `review.sh --to`, writes findings, and a second review whose seat edits a scratch file fails with `error`.
