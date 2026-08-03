---
description: Delegate coding work to a configured CLI agent — the main agent orchestrates and the delegate agent codes. Each phase runs implement → dual blind review → auto-routed fixes → phase review → checkpoint commit. Phased plans run automatically by default; pass `verbose` for a pre-phase explanation and approval gate before every phase, or `single` for one phase with no commit.
---

# Delegate

**Purpose:** The main agent (the session running this command) owns design and orchestration; the configured delegate agent does all coding. This command composes an implementation work order, dispatches it, runs a dual review (a fresh blind delegate session + the main agent's own analysis), synthesizes the results, and routes fixes back to the delegate agent.

**Usage:** `/plan:delegate [plan-doc-path] [phase N] [single|verbose] [auto next N phases|auto through phase X] [free-text instructions]`

**Arguments** — all optional; `single` and `verbose` are mutually exclusive:
- A path to a design/plan/implementation doc → the work spec
- A phase/section identifier (e.g. `phase 3`, `## Migration` section name) → the starting phase
- `single` → run exactly one phase, then stop (no checkpoint commit, no auto-continue)
- `verbose` → before every phase, explain why it exists, the work it will do,
  and the important types/APIs it will introduce or change; wait for explicit
  approval, then run and checkpoint that phase, output only its reviewed result,
  and wait for `continue` before showing the next phase's briefing
- `auto next N phases` / `auto through phase X` → with `verbose`, temporarily
  auto-run a bounded phase range and then restore the verbose approval gate
- Free text → direct instructions, or amendments/narrowing on top of the doc
- Empty → infer the work from the current conversation (the design just discussed is the work)

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSession)
WORKING_DIR = current project directory (often a worktree checkout, sometimes main — use it as-is; never create a worktree or switch branches)
REVIEW_PASS = 0 (review dispatches this phase; resets in <NextPhase/>)
FINDINGS = the per-phase ledger owned by `findings.py` — see <FindingsLedger/>.
It, not a counter, decides whether another fix round runs.
IMPLEMENTATION_TASK = implementation
APPLICATION_SMOKE_RESULT = not_run (resets in <NextPhase/>)
MODE = single when `single` was passed or the work is not phased; verbose when
`verbose` was passed for a phased plan; otherwise loop
AUTO_WINDOW = none (verbose mode only; may become `next N` or `through <phase>`
at a <VerbosePrePhaseGate/>, then returns to none automatically)

---

## Background wait invariant

This is a hard control-flow rule for every implementation, review, and fix-pass
launcher in this workflow:

- When a background launch returns a task or terminal handle, immediately attach
  the environment's background-wait mechanism to that handle and keep the
  current primary-agent turn open until completion notification arrives.
- Never send a final response, yield the turn back to the user, or substitute
  status-file/process polling while any delegated terminal is still active.
- `run_in_background: true` permits the main agent to do the concurrent work
  named by this workflow; it does not permit the main agent to end its turn.
- When phases or reviews launch in parallel, retain every handle and wait on all
  of them in the same turn. Process each completion while remaining waits stay
  attached. Continue the workflow immediately after the final completion.
- **Dual-review preemption:** <DualReview/> may cancel a still-running blind
  review only when the main agent has already confirmed a substantial,
  unambiguous correction from the Work Order. First read any streamed reviewer
  log once, then cancel its handle through the environment's cancellation
  mechanism and remain attached until cancellation settles. Dispatch the fix
  only after that; never edit the diff while the old-diff reviewer is active.

Context compaction does not relax this invariant: resume the same active waits
and control flow after compaction.

---

## Context and compaction — never stall for it

Compaction is a normal, expected event in a long delegate run. It is designed
into this workflow, not an emergency.

**Do not write or refresh a handoff doc before the environment's context-usage
hook asks for one.** Speculative handoff maintenance burns tokens on every
meaningful change and buys nothing; the hook fires at its threshold and that is
the trigger. When it does fire, write the doc into the repo (not the session
scratchpad, which does not survive) and follow the hook's own content list. Add
exactly one thing it has no reason to ask for: **the authorization state** — the
mode this run is in, whether a bounded auto window is open and how many phases
remain in it, and what the last gate actually approved. Everything else the hook
already covers; do not pad the doc. Getting authorization wrong after compaction
means dispatching a phase the user never approved, which is why it is worth the
line. Exclude the doc from `add -N` and from <CheckpointCommit/>; delete it once
the phase is committed and its content is consumed.

**Never stall for compaction.** An approaching context limit is not a reason to
stop, pause, wrap up early, ask the user whether to continue, or hold a dispatch
until after compaction — and it is not something to announce. Write the doc when
the hook asks, then proceed directly to the next action and let auto-compaction
fire on its own schedule. That is the intended path; interrupting it is strictly
slower. Even a missing or hastily written handoff is not grounds to stop —
auto-compaction plus the conversation summary generally carries the run forward
correctly on its own.

The mechanism behind that rule: **auto-compaction fires on the next request,
never mid-turn.** Ending the turn near the limit is therefore the one action that
guarantees compaction never runs — the run stalls waiting for a user who has no
reason to expect it. A Stop hook enforces this while a run is active: end the
turn past the handoff threshold and it refuses the stop and sends you straight
back in. It blocks only once, so a genuine wait on a user decision (a verbose
gate, a pending decision, conflicting reviews, a `stop` gate verdict) survives —
restate the decision in one line and end the turn again.

After compaction, before taking any further workflow action: **re-read this
command file in full** (`~/.claude/commands/plan/delegate.md`) so the workflow is
fresh and complete rather than reconstructed from a summary — a summarized
workflow silently drops rules, and the ones it drops are the ones that were not
firing at the moment of compaction. Then read the handoff back, resume the same
active waits and control flow, and delete the handoff when the phase it describes
is committed.

A SessionStart hook restates that paragraph into the freshly compacted context
while a run is active, since this file is one of the things summarization can
drop. Treat the two as the same instruction, not as a second one.

---

<FindingsLedger>
The fix loop is bounded by convergence, not by a counter. A counter punished a
phase with eight real defects exactly as hard as one whose reviews kept
re-litigating settled ground. `~/.claude/scripts/delegate/findings.py` owns the
per-phase ledger and makes the decision:

| Command | Use |
| --- | --- |
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <delegate\|main\|both> [--detail <d>]` | Record one confirmed issue after <Synthesize/> merges the reviews. Prints its permanent id (`F001`). |
| `status` | The whole ledger as JSON — the closure review prompt is built from it. |
| `gate` | Returns `converged`, `dispatch`, or `stop`, plus the `batch` a fix round must cover and a `stop_reason`. |
| `dispatch --covers F001,F002,…` | Record the fix round. Refuses a partial batch and refuses when `gate` did not say `dispatch`. |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | Record the closure review's outcome per finding. |

Every command takes `--session-dir "${SESSION_DIR}"`. The ledger resets itself
when `start-phase` moves to a new phase.

Three rules the script enforces, so the main agent never rules on them:

- **One batch per round.** A fix round repairs every gating open finding
  together, by root cause. `dispatch` rejects a subset.
- **Severity narrows.** Blockers and minors gate the first fix round; blockers
  alone gate every later one. Nits never gate — they belong in the phase
  retrospective, not in the loop. A phase ships when it is correct, not when it
  is spotless.
- **Convergence, not a count.** A run grinding through eight rounds of real
  defects is never interrupted. `gate` returns `stop` only when a finding fails
  to close twice, a finding reopens twice after being accepted, the gating open
  count fails to decrease across two consecutive rounds, or a ten-round runaway
  backstop trips.

Record findings only after <Synthesize/> has confirmed them — a finding refuted
by reading the code never enters the ledger.
</FindingsLedger>

---

## Delegate heartbeat

Every dispatch in a session — implementation, blind review, fix passes,
escalations — shares one `${SESSION_DIR}/heartbeat.log`, so a single read shows
the whole session's timeline. Beat lines: `<ISO time> [wrapper|agent]
<message>`.

- **Header block** — each launcher (`implement.sh`, `review.sh`) opens its run
  with `---- <ISO time> [<role> (<family>/<model>:<effort>)] ----`
  (an empty resolved effort shows as `unset`, never silently) followed by
  the responsibility text the main agent passed as the launcher's 5th
  argument. Its first line always names the plan and phase:
  `<plan-doc filename> — phase: <phase identifier>` (work without a plan doc
  uses `adhoc — <short scope name>` instead). Then 1-2 lines saying what this
  specific run is responsible for (e.g. `implement the parser Work Order` or
  `fix pass 2 — clippy findings from the dual review`). Always pass it; the
  scripts' fallback text is generic.
- `[wrapper]` lines come from the launcher (`heartbeat_watch.sh`): one beat
  every 60s while the delegate process is alive, tagged with the role and
  carrying an activity digest decoded from the delegate's own streamed log —
  the latest tool call (`Bash: cargo nextest run`), narration line, or output
  line. They prove liveness AND name what the process is doing, even
  mid-blocking-command and even for dispatches that cannot write `[agent]`
  lines.
- `[agent]` lines are the delegate's own narration, written via
  `~/.claude/scripts/agents/heartbeat.sh` immediately before each new activity
  ("implementing the parser changes", "running clippy for verification"). The
  delegate cannot write during a blocking command, so an agent line's age
  measures how long the last-named activity has been running — it is not
  staleness.
- **Handoff rule:** a delegate only writes `[agent]` lines if its prompt names
  the concrete heartbeat file path — delegates have no `${SESSION_DIR}`
  variable. Every write-mode prompt (work order and fix prompts) must carry the
  heartbeat paragraph with the path substituted. Review prompts must NOT carry
  the file path — reviewers cannot write it (codex read-only runs under an OS
  sandbox that fails every write; claude plan mode refuses mutating commands
  even when explicitly allowlisted — verified empirically). Review prompts
  instead carry the **narration instruction**: the reviewer emits one short
  line of output text before each activity, which streams into its log and
  reaches heartbeat.log through the `[wrapper]` digest within one beat. No
  dispatch is ever dark.

Reading rules — the **Background wait invariant** stands unchanged:

- Read the file on demand, as a single read — and read only the tail (last
  ~40 lines, e.g. `tail -n 40`), not the whole file: a long multi-phase
  session accumulates hundreds of lines. Never in a wait loop, and never as
  a completion signal; the background task notification remains the only wait
  mechanism. The progress narration below is the one sanctioned periodic read,
  it runs at the interval <ProgressMonitor/> owns, and it is driven by monitor
  events, not by a loop the main agent writes into its own turn.
- Read it when: the user interjects during a wait and asks what is happening
  (report the last few lines in real words); when resuming after context
  compaction (one read to re-establish where the delegate is); or when a
  delegate has run far longer than its scope suggests (one staleness check).
- A user-requested progress check reads both evidence channels: the heartbeat
  tail for what the delegate says it is doing, and the worktree snapshot defined
  under **Progress narration while waiting** for what implementation has actually
  appeared. Never answer from the heartbeat alone when a live diff is available.
- Interpretation: fresh `[wrapper]` lines + an old `[agent]` line mean the
  delegate is alive and has been in the named activity that long — the wrapper
  digest shows what it is actually doing meanwhile; flag it to the user only
  if that duration is implausible for the activity. `[wrapper]` lines older
  than ~150s (2.5x the 60s cadence) mean the delegate process is dead —
  expect the task notification imminently; do not act before it arrives.
  Read entries below the most recent header block as belonging to that run.

---

## Progress narration while waiting

A delegate dispatch runs for many minutes. The user must not have to ask what is
happening. Every dispatch — implementation, blind review, fix pass, escalation,
and <RunPhaseReview/>'s architect review of the remaining phases — narrates its
progress to the user for as long as it runs, at the interval <ProgressMonitor/>
owns.

The list is exhaustive by construction, not by enumeration: **every dispatch this
command makes goes through a launcher in `~/.claude/scripts/delegate/`, and every
launcher writes a status file and streams heartbeat beats.** If a step of this
workflow ever hands work to another agent by some other mechanism, that is the
defect — route it through a launcher instead of accepting an unnarratable gap.
The Agent tool in particular gives no readable liveness signal: its only output
is a full transcript too large to read, so a subagent dispatch goes dark until it
finishes and the user is left guessing at the same point in every phase.

**Durable progress history.** Every main-agent implementation of this command —
Claude or Codex — writes the same schema through
`~/.claude/scripts/delegate/progress_history.py`. The durable store is
`~/.local/state/plan-delegate/runs/<run-id>.jsonl`: one append-only event stream
per delegation run, all under one aggregation root. `${SESSION_DIR}` retains
only the live state cache. The recorder locks each session state transition and
uses a file lock plus fsync for every event, so simultaneous agents cannot race
the cache, interleave events, or lose rows.

The event stream contains run, phase, pass, progress, and completion events.
Every row carries the worktree name, branch, working directory, plan doc,
phase identifier/title, project/phase/pass timestamps, pass kind and fix count,
current activity, independent project and phase percentages, each percentage's
unchanged duration, the phase's raw and suggested percentages, the decision
source (`raw`, `calibrated`, or `override`), any override reason, the historical
bias and resulting adjustment, and both agent identities. The recorder detects the main agent's family,
session, model, and effort from the active Claude or Codex transcript. The
launchers supply the called agent's resolved task, family, model, and effort from
`config/agents.conf`; never infer either identity from defaults.

**Capture the pre-dispatch worktree baseline once per phase.** Immediately before
the phase's first implementation launcher, write `git status --short` from
`${WORKING_DIR}` to `${SESSION_DIR}/progress_baseline_status`. This records the
plan doc, handoff, or user-owned paths that were already dirty and prevents later
progress reports from claiming them as delegate work. Keep that original phase
baseline through reviews and fix passes; do not overwrite it after implementation
starts. `progress_history.py start-phase` resets the phase clock and phase
percentage state without resetting the project percentage state. The modified
launchers start and finish the `impl`, `review`, `arch`, and `fix N` pass clocks;
a canceled or completed pass never lends its elapsed time to the next pass.

<ProgressMonitor>
**This block is the sole owner of the progress-update interval.** Nowhere else
in this command names a cadence — not in prose, not in a gate briefing, not in a
dispatch announcement. Every other mention refers here.

**The interval is `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` in
`~/.claude/config/timings.conf`, and it is the only source.** Read that file
before telling the user anything about how often updates arrive, and quote the
value it holds — converted to whatever unit reads naturally, but derived from
the file every time, never recalled. If the file is missing or its value is not
a positive integer, say that updates arrive periodically and name no number:
the loop's own numeric guard below exists to keep `sleep` from failing, not to
be reported as a cadence.

**Arm the monitor in the same turn as the dispatch**, immediately after the
launcher returns its handle and before settling into the **Background wait
invariant**. Both launchers write a status file whose value ends in `ing` while
the delegate is alive (`impl_status` → `implementing`, `review_status` →
`reviewing`), so one command serves either. The architect review dispatches
through `review.sh` as well, so it watches `review_status` like any other review:

```
Monitor({
  command: 'S="${SESSION_DIR}/<impl|review>_status"; C="$HOME/.claude/config/timings.conf"; while :; do ' +
           'unset PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS; test ! -r "$C" || . "$C"; ' +
           'I="$PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS"; ' +
           'case "$I" in ""|*[!0-9]*|0) I=120 ;; esac; sleep "$I"; ' +
           'case "$(cat "$S" 2>/dev/null)" in *ing) ;; *) break ;; esac; ' +
           'echo "--- $(date +%H:%M:%S)"; tail -n 6 "${SESSION_DIR}/heartbeat.log"; done',
  description: '<phase> delegate progress',
  persistent: true,
})
```

The loop unsets the variable and re-sources the config before every sleep, so an
edit to `timings.conf` takes effect on the next tick and a config that stops
being readable cannot leave a stale value in place. The `case` guard is a shell
safety net for a missing or malformed setting; it is not a documented default
and is never quoted to the user.

`Monitor` above names the capability, not a required tool spelling. If the
environment has no first-class persistent monitor primitive, launch that exact
loop immediately in its own background terminal and retain its handle separately
from the delegate terminal. The monitor terminal owns the configured interval;
do not approximate it by polling the delegate terminal on some shorter rhythm of
your own, and do not wait for the primary agent to enter a generic "waiting for
background terminal" state before launching it. The primary agent remains
attached to the delegate terminal independently under the Background wait
invariant.

Substitute the real `${SESSION_DIR}` and the right status file. The loop exits on
its own when the status flips to `implemented` / `reviewed` / `error`, so no
monitor outlives its dispatch; still call `TaskStop` on the monitor handle if the
dispatch ends some other way (cancelled, preempted by <DualReview/>).
</ProgressMonitor>

**Worktree evidence is mandatory for every progress narration.** Immediately
before writing a scheduled progress update or answering a user-requested status
check, run these read-only commands in `${WORKING_DIR}`:

```
git status --short
git diff --stat
```

Compare the current paths with `${SESSION_DIR}/progress_baseline_status` and
identify which source, test, example, or documentation areas appeared after the
dispatch. An untracked path is real progress even though ordinary `git diff
--stat` omits it; take its name from `git status --short`. Never use `git add -N`
for progress reporting, and never modify the index or worktree to improve the
snapshot. The snapshot is evidence that editing exists, not evidence that the
edit is correct, complete, or owned by a finished task.

Every progress narration combines both channels:

- **Current activity** from the newest heartbeat entries.
- **Delivered work so far** from post-baseline changed paths and diff scope,
  grouped into behavior-relevant areas rather than pasted filenames.
- **What remains** from the Work Order and verification list.
- **Rough completion estimate** scored from all three.

If the heartbeat still describes reading while source files have appeared, say
that implementation is present and name its areas; do not report that the run is
"still reading" or in its "initial pass." If the configured monitor interval is
longer than another environment rule permits between user updates, capture the
same fresh worktree evidence before each interim narration instead of
extrapolating from the last monitor event. Never infer unchanged work from a
silent launcher terminal: launchers normally emit no stdout while the delegate
runs.

**When a monitor event lands, write the two-section progress header below, then one
or two sentences of ordinary English** — what the delegate is doing right now,
what has materially appeared in the worktree, and what remains. This is
the same translation standard as <Synthesize/>: the reader has not seen the code,
the plan, or the log. Turn `[agent] rerunning the mimesis test gate` into "still
running the image-tool tests; the implementation is present and verification is
underway." Never paste raw heartbeat lines, never quote a file path, never emit a
bare timestamp.
(The heartbeat log path is given once at dispatch — see <ComposeWorkOrder/> step
3 — and is not repeated in these updates.)
If nothing has changed since the last narration, say that in one short sentence
rather than repeating the previous wording verbatim.

The two sections are mandatory for every scheduled update and every
user-requested progress check. Project information comes first; phase and pass
information comes after one blank line:

```
**<worktree-name> - <branch>**
**<project-percent>% complete - elapsed <project-duration>**

**Phase <phase identifier>: <phase title>**
**<phase-percent>% complete - elapsed <phase-duration>**
**<Impl|Review|Arch|Fix N> - <current activity> - elapsed <pass-duration>**
```

When either percentage is reported consecutively, append its own unchanged
duration to that percentage's row:

```
**<percent>% complete - elapsed <duration> - unchanged <duration>**
```

For work without a phased plan, use `ad hoc` as the phase identifier and a short
scope name as its title and use the same assessment for project and phase.
Project elapsed is current time minus the plan's memorialized `Project started`
value and does not reset between phases, reviews, fixes, or later delegation
sessions. Phase elapsed resets at the phase's first implementation dispatch.
Pass elapsed resets at every called-agent dispatch. Project and phase unchanged
timers are independent: changing one assessment resets only its row. Every
duration is zero-padded `MM:SS` under one hour and `HH:MM:SS` at one hour or
longer; total hours may exceed 23.

Do not hand-format or independently time these lines. Immediately before each
progress report:

1. Derive `${PROJECT_RAW_PERCENT}` from the whole plan's completed and remaining
   work, including the current phase's contribution. Derive
   `${PHASE_RAW_PERCENT}` from the current phase's live evidence and work list
   using the estimation rules below. These are separate assessments; never copy
   one into the other merely because only one changed since the last report.
2. Run:
   `python3 ~/.claude/scripts/delegate/progress_history.py calibrate --session-dir "${SESSION_DIR}" --candidate-percent "${PHASE_RAW_PERCENT}"`
3. Read its JSON. When `apply_suggestion` is true, use `suggested_percent` as
   `${PHASE_REPORTED_PERCENT}` unless countable current evidence proves that value
   wrong. With fewer than `minimum_samples`, keep
   `${PHASE_REPORTED_PERCENT} = ${PHASE_RAW_PERCENT}`. Keep
   `${PROJECT_REPORTED_PERCENT} = ${PROJECT_RAW_PERCENT}`. The recorder derives
   the phase decision source: `raw` when no suggestion applies, `calibrated`
   when the suggestion is used, and `override` for any other reported value.
4. Run:
   `python3 ~/.claude/scripts/delegate/progress_history.py progress --session-dir "${SESSION_DIR}" --project-raw-percent "${PROJECT_RAW_PERCENT}" --project-percent "${PROJECT_REPORTED_PERCENT}" --phase-raw-percent "${PHASE_RAW_PERCENT}" --phase-percent "${PHASE_REPORTED_PERCENT}" --cap-stage "<stage>" --activity "<current activity>" [--phase-override-reason "<specific current evidence>"]`
   Include `--phase-override-reason` exactly when rejecting the calibrated phase
   value. State the countable worktree, heartbeat, or verification evidence that
   justified the choice; generic disagreement is not a reason. The script
   rejects a missing reason and also rejects a reason when no override occurred.

   `--cap-stage` is required, and it names the last gate this phase has actually
   passed — not how far along it feels. A percentage is an estimate; a stage is a
   fact, so the script clamps the estimate to the stage's ceiling:

   | `--cap-stage` | The phase has reached | Phase cap |
   |---|---|---|
   | `implementation` | implement.sh dispatched or running; no review yet | 75 |
   | `initial_review` | the broad <DualReview/> is running or just merged | 85 |
   | `open_findings` | the ledger holds gating open findings — fix rounds and closure reviews | 90 |
   | `closure` | `findings.py gate` returned `converged`; smoke test / phase review | 95 |
   | `checkpoint` | phase review done, committing the checkpoint | 98 |
   | `complete` | the checkpoint commit landed | 100 |

   Project percent is clamped to 99 at every stage except `complete`. Going
   backwards is normal and correct: a phase reporting 92 at `closure` that
   reopens findings drops to 90 at `open_findings`.
5. Copy the script's two-section Markdown header exactly, then add the status prose.

`calibrate` uses completed phases only. It compares the report timestamp with
the phase's actual finish time, measures how long that percentage remained
unchanged, and computes raw-vs-reported temporal bias. It prefers matching main
and called model/effort plus pass kind when at least five comparable percentage
streaks for the raw estimate exist, then falls back through called-agent/pass,
pass-only, and global evidence. `aggregate [--percent N]` groups on the raw
estimate and emits the underlying statistics, including median and tail
unchanged durations, remaining time, percentage bias, and raw-versus-calibrated
absolute error. It separately reports suggested-versus-reported accuracy and
decision-source counts, so overrides can be evaluated instead of silently
folded into the calibration result. This is advisory calibration, not a
replacement for current worktree and verification evidence.

**Estimating percent complete.** The main agent wrote the prompt, so it knows the
whole work list: the number of issues or files to change, and the exact
verification lines the delegate must run. Score against that list, not against
elapsed time.

- Reading the style guide, reading source, and locating the change sites is the
  first stretch — under 20% only while the worktree snapshot confirms that no
  implementation has appeared.
- Editing is the middle. With a known issue count, weight by issues finished:
  two of four done is about half of the editing stretch. Use changed areas as
  evidence that an issue has started, then use heartbeat/check output to decide
  whether it is merely drafted or working.
- Verification is the last stretch and its steps are countable. Each
  `verify.sh` line that has passed is a fixed share of it; the last line
  finishing means the delegate is writing its report — 90%+.
- A blind review has no edit stretch: reading and cross-checking is nearly all
  of it, so hold the estimate low and moving (30%, 55%, 70%) until findings are
  being written.
- The architect review of the remaining phases has no edit stretch either, and
  its work list is known exactly: the number of remaining phases, each of which
  it reads and then verifies against real code. Score by phases covered. Its
  tail is the variable part — heavy drift means writing replacement Work Order
  text for each finding, so say the estimate is uncertain when findings start
  appearing early.

Give one number, round it hard (10%, 25%, 50%, 80%), and never present it as
precise. If the delegate is doing something the work list did not anticipate,
say the estimate is uncertain and why in the prose after the required header.
Never omit the header because the estimate is uncertain. An unexplained
percentage that stalls or moves backwards is worse than an explicitly qualified
estimate.

Interpretation carries over from the heartbeat reading rules: fresh `[wrapper]`
lines with an old `[agent]` line mean the delegate is alive and has been in that
one activity the whole time — say so, and flag it only when that duration is
implausible for the named activity.

Two things this narration never does: it never ends the turn, and it never
substitutes for the completion notification. The **Background wait invariant**
stands unchanged — the monitor is a side channel for the user's benefit, not a
wait mechanism.

**Yield to the user.** If the user gives the main agent something else to do
during a wait, that work takes precedence: do it, and let the narration lapse
while it is in progress rather than interleaving status lines into the middle of
it. Resume narrating once that work is done and the delegate is still running. If
the user says to stop the updates, `TaskStop` the monitor and do not re-arm it
for the rest of the run.

---

<UserFacingText>
**Applies to every turn this command shows the user** — briefings, review
results, decisions, choice lines, progress narration.

**Read `~/.claude/docs/user_facing_explanation.md` and follow it.** It owns the
principle the rest of this command's presentation rules derive from — you do the
reconstruction, not the user — plus the build order, naming, banned vocabulary,
the comprehension gate, which decisions are worth the user's attention, and the
choice-line format. `/adhoc_review` shares the same file, so the two cannot
drift.
</UserFacingText>

---

<TypeDesignContract>
**Applies to every implementation, blind-review, fix-pass, and escalation
delegate call, and to the main agent's own review.**

Read `~/.claude/docs/type_design.md` and follow it. Copy its complete contents
verbatim into every delegate prompt under `## Type Design Contract`; a role
does not inherit this contract from an earlier call. Implementers and fixers
must apply it to the code they write. Reviewers must actively check the diff
for violations and report them as design findings. Escalation does not relax
it.
</TypeDesignContract>

---

<ExplainOnDemand>
**Trigger:** the user says they do not understand, asks what something means,
asks for a reframe, or answers a gate with confusion instead of a control. This
fires at any gate — pending decision, <VerbosePrePhaseGate/>, <Synthesize/>,
<VerbosePostPhaseReport/> — and preserves that gate. Explaining never authorizes
anything.

**Read `~/.claude/docs/explain_on_demand.md` and follow it.** It owns the
method: rebuild from the bottom, stay technical, name real signatures read from
real source, and put a short code example under every mechanism — problem code
before fix code. `/plan:phase_review` shares the same file, so the two commands
cannot drift.

This is the one place the terse default is wrong. Do not compress and do not
re-emit the same summary with softer words. Afterwards, restate the pending
question and wait.
</ExplainOnDemand>

---

## Delegate verification (Rust)

Delegates never compose cargo commands. Every build/test/lint a delegate runs
is an exact line from its prompt invoking
`~/.claude/scripts/delegate/verify.sh` — the script owns all flags, target
selection, and the nextest fallback, so there is no scope choice to get wrong
(Cargo compiles a package's examples even under `-p <pkg>`; the script pins
explicit `--lib`/`--bins` targets from cargo metadata).

| Intent | Line |
| --- | --- |
| compile feedback while coding | `bash ~/.claude/scripts/delegate/verify.sh check <package>` |
| unit tests (phase gate) | `bash ~/.claude/scripts/delegate/verify.sh test <package>` |
| one integration test target | `bash ~/.claude/scripts/delegate/verify.sh test <package> <int_test>` |
| format + scoped lint (phase gate) | `bash ~/.claude/scripts/delegate/verify.sh lint <package>` |
| format only (checkpoint backstop) | `bash ~/.claude/scripts/delegate/verify.sh fmt <package>` — <CheckpointCommit/>, not phase delegates |
| one changed example | `bash ~/.claude/scripts/delegate/verify.sh example <package> <name>` |
| full workspace gate | `verify.sh final` — <FinalGate/> only, never a phase delegate |

- Work orders and fix prompts list the applicable lines verbatim with
  `<package>` filled in; the delegate runs exactly those, nothing more. An
  unrequested `cargo check --all-targets` or example build is a Work Order
  violation, not diligence.
- **Every write-mode prompt must state that the delegate may not report back
  until each verification command has exited and it has read the output.**
  Without that sentence a delegate will background its own verification and
  return a summary claiming success — observed returning the single line "Test
  verification running in background" for a change that failed lint. The main
  agent's own gate run is what catches this; never accept the summary's word for
  a passing gate.
- `example` lines appear only in phases whose **Files** touch that example;
  integration-test lines only in phases that own that test.
- `check` is compile feedback, never a gate entry. Every package in the gate
  gets `test` + `lint`. A package listed as `check` only is a hole: it proves
  the code builds while its tests go unrun.
- **Never write a "known pre-existing failure" caveat into a delegate prompt
  without having proven it on a clean tree first.** Telling the delegate which
  failure to disregard makes its confirmation circular: it reports back the
  conclusion the prompt handed it, and a real regression introduced by the
  phase wears the exemption. If a test is believed to fail before the phase
  starts, verify it by running that test on the pre-phase tree — `git stash` is
  prohibited, so check out the base commit in a scratch worktree, or run the
  single test by name before dispatching. If it passes there, it is not
  pre-existing and the prompt must say nothing about it.
- A failure that is environmental rather than code — no GPU, no display, a
  missing service, a blocked socket — is a finding to surface to the user, not
  a caveat to feed the delegate. The main agent's own shell may be sandboxed
  when the delegate's is not (or the reverse), so "it fails for me" is not
  evidence about the delegate's environment. Re-run the failing test with
  `dangerouslyDisableSandbox: true` before concluding anything: a sandboxed
  macOS shell has no GPU adapter, so every test that builds a real render
  device panics with a driver error that looks nothing like a sandbox problem.
- The gate covers every package the phase **modifies**, which is not the same as
  the packages its **Files** list names. A changed trait signature, public API,
  registration path, or plugin wiring reaches callers the plan never listed —
  most often the top-level application crate, which the Work Order rarely names
  because the change there is one or two lines. Trace the blast radius when
  composing the gate and add those packages. This is a recurring escape route
  for regressions: the delegate edits a caller to keep the build green, and
  nothing ever runs that caller's tests.
- Everything workspace-wide — `--all-targets`, all examples, the full `clippy`
  skill — happens once in <FinalGate/> when the plan is exhausted, not per
  phase.
- Non-Rust projects: the Work Order lists the project's exact commands instead;
  the run-only-what-is-listed rule is identical.

---

## Multi-phase modes

When the work comes from a phased plan doc, auto/loop mode is the default: run the
named phase (or the first `todo` phase if none was named), then continue
phase-after-phase until the plan is exhausted or a blocking decision stops the
run. `verbose` opts into a checkpointed human gate before every phase, including
the selected starting phase; `single` opts out of multi-phase execution and
checkpoint commits entirely.

### Verbose mode

`verbose` does not authorize the selected phase immediately. After assembling
the selected phase's work order, run <VerbosePrePhaseGate/> before any delegate
dispatch and wait. Approval authorizes exactly that phase's implementation,
dual review, fixes, phase review, and checkpoint commit. After completion, run
<VerbosePostPhaseReport/> and, outside a bounded-auto window, wait at
<VerbosePostPhaseGate/>. The post-phase report contains only the completed
phase's reviewed summary. `continue` advances only far enough to assemble and
show the next phase's <VerbosePrePhaseGate/>; it never authorizes that phase.
Even a completely correct phase never authorizes the next phase.

At the gate, accept these controls:

- `proceed` or `approved` — run only the phase currently described,
  then return to another verbose gate before the next phase.
- `auto next N phases` — run the currently described phase and enough
  subsequent phases to total the positive integer N automatically, then return
  to verbose mode after the Nth checkpoint.
- `auto through phase X` — run the currently described phase through X inclusive, then
  return to verbose mode after X's checkpoint.
- `stop` — emit <RunSummary/> and end without starting the described phase.

The user may include amendments with `proceed`; append the remaining text to the
current phase's assembled implementation prompt before dispatch. Post-phase
`continue` never amends or dispatches a phase. Validate an auto-window target
before dispatch: it must identify the current or a later todo phase in the current plan. Plan
exhaustion, a blocking decision, or an error still ends/stops the window early.
An auto window changes only phase-to-phase advancement; every implementation,
review, fix, phase-review, and checkpoint rule remains active.

**An auto window removes the stops, not the explanations.** The verbose briefing
is the reason the user chose verbose; it survives the window. The window changes
only *when* the briefings are delivered — instead of one before each phase, all
of them **up front**, before the first dispatch. On accepting `auto next N
phases` or `auto through phase X`:

1. Resolve the exact phase list the window covers.
2. Brief **every** phase in that list, in order, to the standard
   <VerbosePrePhaseGate/> requires for a single phase — why it exists, the work
   it will do, the important types/APIs it introduces or changes. A phase whose
   Work Order has not been read yet is not briefable; read it now, not later.
3. Take **one** approval on the whole batch, then run the window uninterrupted.

Never treat a reply to a compressed summary row — a `Recommended next step`
table cell, a one-line phase title — as informed approval for a window or a
phase. If an auto control arrives before its batch briefing exists (attached to
the initial invocation, or given at a gate that described only the current
phase), deliver the batch briefing and re-gate before the first dispatch.

The same bounded-auto controls may accompany the initial `verbose` invocation.
There, the selected starting phase counts as the first `auto next N` phase, and
`auto through phase X` includes both the selected phase and X. Examples:

```
/plan:delegate docs/hana/tool-graph.md phase 1 verbose
/plan:delegate docs/hana/tool-graph.md phase 1 verbose auto through phase 13
```

The second form still owes the user <AutoWindowBatchBriefing/> for phases 1–13
before phase 1 is dispatched. An auto control on the invocation line is a
statement about stopping, not a waiver of the briefing.

**Commit authorization.** Invoking this command in loop mode IS the user's
explicit request for checkpoint commits. In verbose mode, approval at
<VerbosePrePhaseGate/> authorizes exactly one implementation and checkpoint, or
the phases named by an explicit bounded-auto control. Each completed phase gets
exactly one commit via <CheckpointCommit/>, never a push, no other commits. This
is the sole exception to the global never-commit rule.

**Dirty-tree guard.** Before the first dispatch in loop or verbose mode, run
`git status --short` in ${WORKING_DIR}. If the tree already has uncommitted
changes, STOP and ask the user how to proceed — a checkpoint must contain one
phase's work and nothing else. Exception: if the selected phased plan document
is the only dirty path, continue without asking. Treat its existing changes as
part of the delegation run and include them in the first phase's checkpoint
commit with that phase's implementation and phase-review updates.

**Blocking vs. deferrable decisions.** Whenever a user decision surfaces (from
reviews, fix passes, or phase review):
- It is purely a sequencing defect — the plan already specifies the required
  behavior and acceptance criteria, but correct work must move, merge, or run
  earlier/later so a phase is usable, testable, or lint-clean → do not treat it
  as a user decision. Edit the phased plan to resequence the existing work,
  preserve every requirement and test owner, recompose the affected Work
  Order, and continue automatically. The main agent may merge phases, split a
  phase, or renumber later integer phases when that is the smallest coherent
  correction. Report the resequencing in one line.
- It blocks the *current* phase's correctness or acceptance gate → STOP and
  present it.
- It affects only later phases and the current phase can complete safely
  without it → write a `**Pending decision:**` block (format:
  `~/.claude/docs/delegate_plan_format.md`) into the affected phase's Work
  Order, tell the user in one line that it was deferred, and continue. The
  pre-dispatch check in <ComposeWorkOrder/> stops either multi-phase mode when
  that phase actually comes up.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSession/>
**STEP 2:** Execute <ComposeWorkOrder/> (starts with the pending-decision pre-dispatch check)
**STEP 3:** In verbose mode with no active auto window, execute
<VerbosePrePhaseGate/> and wait
**STEP 4:** Execute <SelectTask/>
**STEP 5:** Execute <LaunchImplementation/>
**STEP 6:** Execute <DualReview/>
**STEP 7:** Execute <Synthesize/>
**STEP 8:** Execute <RunApplicationSmokeTest/>
**STEP 9:** Execute <RunPhaseReview/> (required for phased plans; pass `auto` in either multi-phase mode)
**STEP 10:** Execute <CheckpointCommit/> (loop and verbose modes only)
**STEP 11:** Execute <RecordPhaseCompletion/>
**STEP 12:** In verbose mode execute <VerbosePostPhaseReport/>
**STEP 13:** In verbose mode execute <VerbosePostPhaseGate/> when applicable
**STEP 14:** Execute <NextPhase/> (loop and verbose modes only) — auto-continues,
returns to STEP 2 for the next pre-phase gate, or ends with <RunSummary/>
</ExecutionSteps>

---

<PrepareSession>
**Goal:** Create a clean session directory.

1. Run: `bash ~/.claude/scripts/delegate/prepare_session.sh` using Bash with `dangerouslyDisableSandbox: true`
2. **Capture ${SESSION_DIR}** from the last line of output (format: `Session ready at <path>`)
3. Store the current project directory as ${WORKING_DIR}
4. Record the current epoch time in
   `${SESSION_DIR}/progress_project_started_at`. This is the fallback start for
   ad hoc work and plans with no Git history. <ComposeWorkOrder/> replaces it
   with the plan's memorialized project start when one exists or can be derived.

The same script writes a run-active marker for this Claude session. While it
exists, the Stop hook refuses to let the turn end near the auto-compaction
threshold (see **Context and compaction**). <RunSummary/> clears it.
</PrepareSession>

---

<ComposeWorkOrder>
**Goal:** Write an implementation prompt that lets the delegate agent implement without ambiguity or questions.

0. **Pre-dispatch check (phased plans).** Scan the target phase's Work Order for
   `**Pending decision:**` blocks. If any exist, STOP: present each block to the
   user (it already carries the decision template — problem, current state,
   recommendation), wait for the resolution, edit the resolved outcome into the
   Work Order (Spec / Files / Acceptance gate, then delete the block), and only
   then proceed. Never dispatch a phase carrying an unresolved pending decision.
   A block written months ago may name code that has since shipped — read what
   it cites before presenting it, and say so when the gap turns out narrower
   than the block assumes. If the user does not follow the decision, execute
   <ExplainOnDemand/>; a decision the user cannot restate is not resolved.

1. Parse $ARGUMENTS into: doc path (if any), phase/section (if any), `single`
   token, `verbose` token, optional bounded-auto control (`auto next N phases`
   or `auto through phase X`), and free-text instructions. Remove recognized
   controls from the free text. Parse the complete bounded-auto phrase before
   interpreting standalone `phase X`, because `auto through phase X` contains a
   phase token that is not the starting phase. If both `single` and `verbose`
   appear, or if a bounded-auto control appears without `verbose`, STOP and ask which execution
   contract the user wants. Require a positive N. If all arguments are empty,
   infer the work from the conversation.

1a. **Resolve project start once.** If a plan doc was given, read its
    `## Delegation Context` and look for `- **Project started:** <ISO-8601>`.
    Validate and convert an existing value to epoch seconds, then write it to
    `${SESSION_DIR}/progress_project_started_at`; never query Git when the field
    exists. If the field is absent, run one Git-history lookup for the oldest
    visible commit that touched the plan doc:

    `git log --follow --format='%H %cI' -- "<plan-doc>"`

    Take the last non-empty entry, add its ISO-8601 commit timestamp as the
    `Project started` bullet immediately after the `Project` bullet, and write
    the corresponding epoch time to the session file. The plan field is the
    authority from then on; later phases and later `/plan:delegate` sessions
    read it without checking Git again. If the plan has no Git history, use the
    session-start timestamp from <PrepareSession/> and still write its ISO-8601
    value into the plan so the failed lookup is never repeated. A malformed
    existing field is an error; do not silently replace it.

1b. **Initialize durable progress history once.** After 1a has resolved the
    project timestamp, run:

    `python3 ~/.claude/scripts/delegate/progress_history.py start-run --session-dir "${SESSION_DIR}" --working-dir "${WORKING_DIR}" --project-started-at "$(cat "${SESSION_DIR}/progress_project_started_at")" [--plan-doc "<plan-doc>"]`

    Include `--plan-doc` only when one exists. The recorder captures the
    working-directory basename, branch, run id, and the active main agent's
    family/session/model/effort. It is idempotent when later phases return to
    <ComposeWorkOrder/> with the same `${SESSION_DIR}`. The recorder fails when
    identity detection cannot find an exact model/effort; STOP instead of
    supplying guessed values.

2. If a doc path was given, Read it. Decide whether it is a **delegate-ready plan** (per `~/.claude/docs/delegate_plan_format.md`): it has a `## Delegation Context` section **and** the target phase has a `#### Work Order`. Branch:

   `verbose` requires a phased delegate-ready plan. If the target has no phased
   Work Orders, STOP and explain that there is no next-phase boundary to gate;
   offer `single` for that work instead.

   Validate an initial bounded-auto range against the plan before dispatch. For
   `auto next N phases`, set `AUTO_WINDOW = next N` and count the selected phase
   as the first. For `auto through phase X`, require X to be the selected or a
   later todo phase and set `AUTO_WINDOW = through X`.

**FAST PATH — delegate-ready plan. Assemble; do NOT research the codebase.**
`/plan:to_phased_plan` already paid the research cost and baked it into the doc. Build `${SESSION_DIR}/implementation_prompt.md` by copy-and-assemble:
- **Project Context** = the doc's `## Delegation Context` block verbatim + the target phase's **Constraints from prior phases**.
- **Work Specification** = the target phase's **Goal**, **Spec**, and **Files** verbatim + any free-text the user added on the command line.
- **Type Design Contract** = `~/.claude/docs/type_design.md` verbatim.
- **Style Requirements** = the standard block (see fallback template), included only if Delegation Context names a **Style** line.
- **Verification** = Delegation Context **Build / Test / Lint / Run / Smoke**
  entries that exist + the phase **Acceptance gate**, translated into
  `verify.sh` lines per **Delegate verification (Rust)**. Older plans carry raw
  cargo commands — never pass those through: a bare `cargo nextest run` or any
  `--all-targets`/example-building command becomes its scoped `verify.sh`
  equivalent, and a **Lint** line naming the `clippy` skill becomes
  `verify.sh lint <package>` (the full `clippy` skill runs once in
  <FinalGate/>, not per phase). Note the translation in one line. The main
  agent still owns the mandatory live application smoke test in
  <RunApplicationSmokeTest/>; delegate verification never substitutes for it.

Prepend the boilerplate header (the first two paragraphs of the template below, with the concrete ${SESSION_DIR} path substituted into the heartbeat command) and write the file with the **Write tool**. Do not open codebase files to fill gaps — if a needed fact is absent, the *plan* is at fault: name the gap in one line, proceed with what the doc gives, and let the review catch the rest. This path should cost a few thousand tokens, not tens of thousands.

**FALLBACK PATH — no Work Order (free-text, conversation-inferred, or a pre-`/plan:to_phased_plan` doc).**
Research and compose. If a doc path was given, extract the applicable phase/section. Write `${SESSION_DIR}/implementation_prompt.md` with the **Write tool** (NOT Bash heredoc) using this template:

```
You are implementing a code change. Write the code. Make the changes directly
in the codebase. Do not ask questions — implement the spec below.
Do NOT commit. Do NOT create branches. Do NOT touch files outside this task's scope.
After making all changes, summarize what you did: which files you
created/modified and why, and any deviations from the spec with reasons.

Heartbeat: immediately before each new activity (reading code, editing a file,
running build/lint/tests), run
  bash ~/.claude/scripts/agents/heartbeat.sh <SESSION_DIR>/heartbeat.log agent "<what you are about to do>"
with a short present-tense phrase of real words naming the activity (e.g.
"implementing the parser changes", "running clippy for verification"). One
line per activity change — err on the side of too many, not too few; a long
gap reads as a hang. Never read the heartbeat file — it is for the
orchestrator only.

## Project Context

[Project description, tech stack, relevant directory structure, key file paths
the delegate agent will need to read or modify. For a phased plan: one-line summaries of
already-completed phases and any retrospective facts that constrain this phase.]

## Work Specification

[The applicable plan section quoted VERBATIM, plus the user's free-text
instructions. If inferred from conversation: a concrete, complete spec —
files to create/modify, the approach, types/APIs/patterns to follow,
edge cases and constraints discussed.]

## Type Design Contract

[Complete contents of ~/.claude/docs/type_design.md, copied verbatim.]

## Style Requirements   ← include this section only for Rust work

Before writing any code, run:
  zsh ~/.claude/scripts/rust_style/load-rust-style.sh --scope edit --project-root <WORKING_DIR>
Read every style file marked [non-negotiable] in the loaded checklist and any
guideline files relevant to the code you are changing (full paths are shown in
the checklist, e.g. ~/rust/nate_style/rust/<rule>.md or repo-local docs/style/*.md).
Follow them in all code you write.

## Verification

Run ONLY the commands listed below. Verification scope is not your choice:
do not add flags, do not widen targets, do not invoke cargo directly. If a
listed command fails in a way the spec does not explain, report it in your
summary rather than inventing a broader check.

One exception, and only this one: if you modify a file in a package that has no
`test` line below, run `bash ~/.claude/scripts/delegate/verify.sh test <that
package>` as well and name it in your summary. Editing a caller to keep the
build compiling — a trait signature change, a new registration, a plugin
list — is exactly the case where that package's tests are most likely to break
and least likely to be listed. Widening a package already covered below is
still a violation; this covers packages the gate missed entirely.

While coding (as often as useful):
  bash ~/.claude/scripts/delegate/verify.sh check <package>
Before summarizing (the phase gate):
  bash ~/.claude/scripts/delegate/verify.sh test <package>
  bash ~/.claude/scripts/delegate/verify.sh lint <package>
[ONLY if this phase's Files touch an example, add:
  bash ~/.claude/scripts/delegate/verify.sh example <package> <name>]
[ONLY if this phase owns a named integration test, add:
  bash ~/.claude/scripts/delegate/verify.sh test <package> <integration_test>]
[Non-Rust projects: replace the lines above with the project's exact
commands — the run-only-what-is-listed rule still applies.]
```

**Key principles (fallback):**
- Quote the plan section verbatim — do not paraphrase the spec
- Be specific enough that the delegate agent never has to guess; it cannot ask questions
- Point to files the delegate agent can read itself rather than dumping file contents
- Include the no-commit / no-branch rules verbatim — the delegate agent must leave the tree dirty for review
- Include the heartbeat paragraph with the concrete ${SESSION_DIR} path substituted — the delegate has no ${SESSION_DIR} variable
- Verification lines are `verify.sh` invocations with `<package>` filled in — never raw cargo commands (see **Delegate verification (Rust)**)

3. In single/loop mode or a verbose bounded-auto window, tell the user what is
   being dispatched, the prompt path, and the heartbeat log path:
   `Dispatching <scope summary> to the delegate agent — prompt at ${SESSION_DIR}/implementation_prompt.md`
   `Heartbeat: ${SESSION_DIR}/heartbeat.log`
   The heartbeat path is always given so the user can watch the delegate
   directly (`tail -f`) instead of relying on the periodic narration. Emit it
   once per dispatch, at dispatch — including fix passes, escalations, and the
   blind review, which share the same file.
   In verbose mode with no active auto window, do not announce dispatch yet;
   <VerbosePrePhaseGate/> explains the assembled phase and waits for approval —
   the same two lines are emitted after approval, when the dispatch actually
   goes out.

   If this was the fast path, add: `(assembled from <plan>'s Phase N Work Order — no research)`.
</ComposeWorkOrder>

---

<VerbosePrePhaseGate>
**Verbose mode only, before every phase when no bounded-auto window is active.**

**Goal:** Explain the phase before any delegate work starts, including the
load-bearing types and APIs the plan expects, then wait for explicit approval.

Build the briefing only from the target phase's Work Order and Delegation
Context. Do not research the codebase on the delegate-ready fast path, invent
types the plan does not name, or describe planned behavior as already working.
Use the assembled implementation prompt to ensure any command-line amendments
are reflected.

```
## Phase N ready — <phase title>

### Why this phase exists
[The point of the phase in 2-4 sentences: what capability or foundation it adds,
what later work depends on it, and what remains deliberately outside it.]

### Work to be done
[A behavior-focused summary of the planned implementation, including the main
state transitions, ownership boundaries, and user-visible effect.]

### Important types and APIs this phase will introduce or change
| Type / trait / API | Status | Planned role | How it will work with the rest of the system |
| --- | --- | --- | --- |
[Only load-bearing types, traits, resources, enums, events, and public methods
explicitly named by the Work Order. Explain expected ownership, inputs/outputs,
lifecycle, and persistence/runtime boundaries where specified. If the Work
Order names none, say "No new load-bearing types or APIs are specified for this
phase" instead of manufacturing entries.

**Status** is exactly one of `New`, `Existing - Changes`, or
`Existing - No Changes`, derived from the Work Order alone — its Spec wording
and Files list say what the phase creates versus modifies; a type the phase
only calls, stores, or reads is `Existing - No Changes`. Never open codebase
files to settle this on the delegate-ready fast path. If the Work Order is
genuinely ambiguous about a row, write `Existing - Changes (unconfirmed)` or
`New (unconfirmed)` rather than guessing silently, and name the ambiguity in
one line under the table.]

### Files and verification
[Name the files or modules that will change, the acceptance gate, and meaningful
build/test/lint checks.]
```

Then ask exactly one authorization question and wait:

`Start Phase N? Reply \`proceed\` to run only this phase, \`auto next N phases\`,
\`auto through phase X\`, or \`stop\`.`

- `proceed` or `approved`: keep `AUTO_WINDOW = none`; append any
  trailing text to `${SESSION_DIR}/implementation_prompt.md` as current-phase
  free-text instructions; announce the dispatch and continue to <SelectTask/>.
- `auto next N phases`: require positive N, set `AUTO_WINDOW = next N`, announce
  the inclusive phase range, then execute <AutoWindowBatchBriefing/> before any
  dispatch. The current phase counts as one.
- `auto through phase X`: require X to be the current or a later todo phase, set
  `AUTO_WINDOW = through X`, announce the inclusive range, then execute
  <AutoWindowBatchBriefing/> before any dispatch.
- `stop`: emit <RunSummary/> with `user stopped before phase N` and end.
- `continue`: this control is reserved for <VerbosePostPhaseGate/> and does not
  authorize implementation; preserve this gate and ask for `proceed`.
- A question or discussion without an explicit authorization control does not
  authorize the phase. Answer it, preserve the gate, and ask again. If the
  question is that the briefing did not land, execute <ExplainOnDemand/>.
</VerbosePrePhaseGate>

---

<AutoWindowBatchBriefing>
**Verbose mode only, when a bounded-auto window opens — before the window's
first dispatch.**

**Goal:** The user is authorizing several phases at once and will not be asked
again until the window closes. Give them, up front, everything they would have
been told phase by phase.

An auto window suppresses the *stops*, never the *explanations*. Skipping the
briefing because "auto means go" is the failure this block exists to prevent,
and so is deferring the briefing until after dispatch.

1. **Resolve the window's phase list** from `AUTO_WINDOW` and the plan's `todo`
   phases: the current phase plus each subsequent one the window covers.
2. **Read each of those phases' Work Orders now.** A phase that has not been
   read cannot be briefed, and reading it later is too late — the user has
   already approved by then. This is also the moment any `**Pending decision:**`
   block inside a windowed phase surfaces: name it in the briefing rather than
   letting the window run into it blind.
3. **Emit one briefing per phase, in order, in a single turn**, each using the
   <VerbosePrePhaseGate/> template above (`## Phase N ready` → Why → Work →
   types/APIs table → Files and verification). Do not compress a phase to a
   table row or a sentence; the window is exactly when the user has the least
   opportunity to ask.
4. **Ask once, for the whole batch, and wait:**

   `Run phases <list> without stopping? Reply \`proceed\` to authorize all of
   them, \`proceed phase N\` to authorize only phase N and re-gate after it, or
   \`stop\`.`

- `proceed` / `approved`: dispatch the first phase and run the window to its end
  without further gates.
- `proceed phase N` or any narrowing: set `AUTO_WINDOW = none` (or the narrowed
  range), and continue with only what was authorized.
- `stop`: emit <RunSummary/> and end.
- A question, or an answer that shows a phase did not land, preserves the batch
  gate: answer it (via <ExplainOnDemand/> when the briefing itself missed),
  then ask again. Discussion is never authorization.

**Applies to a window opened at the initial invocation too** — `/plan:delegate
<doc> phase 1 verbose auto through phase 13` runs this block before phase 1's
dispatch, not a single-phase gate.

**If a window was already opened without this briefing**, deliver it at the next
point the run reaches — before the next dispatch inside the window — and say
plainly that the earlier approval was taken on less information than it should
have been.
</AutoWindowBatchBriefing>

---

<SelectTask>
Choose the implementation task deliberately; do not scan the prompt for
keywords:

- `implementation` — the default for ordinary feature work.
- `escalation` — use when the Work Order contains genuinely ambiguous
  architecture, numerical/transform mathematics, or a prior behavioral attempt
  failed review.

State the selected task in the dispatch update. `~/.claude/config/agents.conf`
owns both agent and effort in its `[delegate.<family>]` rows; switch the active
delegate family with `/agent`.
</SelectTask>

---

<LaunchImplementation>
**Goal:** Run the delegate agent and wait for completion.

1. Before the phase's first implementation launcher, run `git status --short`
   in `${WORKING_DIR}` and write its output to
   `${SESSION_DIR}/progress_baseline_status`. Do this exactly once for the phase;
   fix passes keep the original baseline. Then run
   `python3 ~/.claude/scripts/delegate/progress_history.py start-phase --session-dir "${SESSION_DIR}" --phase-id "<identifier>" --phase-title "<title>" --work-order-file "${SESSION_DIR}/implementation_prompt.md"`.
   Use `ad hoc` plus a short scope title when there is no phased plan. This call
   is idempotent for the already-active phase. `--work-order-file` records the
   Work Order's size (lines, words, distinct file targets, top-level bullets)
   into the `phase_started` event. Nothing enforces a threshold on it; the
   numbers accumulate so a later release can tell what size of phase actually
   converges. Pass the phase's original prompt, never a fix-round prompt.
2. Set `${PASS_KIND}` to `arch` when
   `${IMPLEMENTATION_TASK} = escalation`, otherwise `impl`.
3. Run `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/implementation_prompt.md" "${IMPLEMENTATION_TASK}" "<responsibility>" "${PASS_KIND}" "<current pass activity>"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true` — `<responsibility>` starts with the plan/phase line (`<plan-doc filename> — phase: <identifier>`, or `adhoc — <scope>` without a plan doc), then 1-2 lines naming what this run implements (the Work Order's goal in a few words). `<current pass activity>` is a short present-participle description suitable for the header, e.g. `implementing retry recovery`. The launcher records the pass start, called agent identity, and completion/error automatically.
4. Inform the user: "The delegate agent is implementing... (heartbeat: ${SESSION_DIR}/heartbeat.log)"
5. Arm the progress monitor over `impl_status` per <ProgressMonitor/>, in this
   same turn.
6. Apply the **Background wait invariant**: keep this turn visibly attached to
   the returned handle and wait for the background task notification. Do NOT
   poll status files or end the turn.
7. When it arrives, read ${SESSION_DIR}/impl_status:
   - **"implemented":** Read ${SESSION_DIR}/impl_summary.txt → ${IMPL_SUMMARY}. Continue.
   - **"error":** Read ${SESSION_DIR}/impl_agent.log, show the user the error, stop.
</LaunchImplementation>

---

<DualReview>
**Goal:** Two independent reviews of the diff — a fresh blind delegate session and the main agent's own — running concurrently.

**Step 0 — pick the review kind.** Increment `${REVIEW_PASS}`.

- `${REVIEW_PASS} = 1` — the phase's **broad review**. Whole diff, whole spec,
  the five general questions. Steps 1-5 below as written.
- `${REVIEW_PASS} > 1` — a **closure review** of one repair. Use
  <ClosureReview/> instead of Step 2's template. Everything else in this block
  (the untracked sweep, the launch, the main agent's own pass, collection)
  is unchanged.

A closure review is not a second audit of the phase. Re-running the broad review
after every repair is what kept runs from converging: a fresh blind reviewer,
handed a larger diff each time with no record of what was already accepted,
always returns something.

**Step 1 — Capture the diff:**

**Before capturing anything, run `git status --short` in ${WORKING_DIR} and `git add -N` every untracked file the phase created.** A delegate that creates a new source file leaves it untracked, and `git diff` does not show untracked files at all — so the phase's largest new file is routinely the one missing from the review. Intent-to-add makes it appear in every subsequent `git diff` without staging its contents.

Exclude from `add -N`: files the phase did not create (pre-existing untracked scratch), and orchestrator-owned files such as handoff docs. Everything else the phase produced gets tracked now — it also has to be in <CheckpointCommit/> later, so this is not extra work.

Then run `git diff` and `git status --short`. Verify the diff now contains every new file by name; if a file the delegate's summary claims to have created is absent from the diff, stop and resolve that before dispatching any review.

This rule applies to **every** review dispatch, not just the first: every <ClosureReview/> after a fix round too. Fix rounds create new files, and a closure review that never sees them reports the repair clean.

**Never hand a reviewer a `git diff <ref>` command as the definition of the change** unless every new file is already tracked. If a review prompt names a diff command, the untracked sweep above must have run first — otherwise the reviewer silently reviews a subset and reports clean.

**Step 2 — Compose the review prompt.** Write ${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md using the **Write tool**:

```
You are reviewing a code change you did not write. You have the specification
it was implemented from and the full diff. Review independently and critically.
You have read-only access to the codebase — read surrounding code as needed.

Do NOT re-run the verification the implementer already ran — its exact
build/test/lint gate is listed in the specification below and it passed.
Repeating it proves nothing and burns most of the review on compilation.
Reading code and reasoning about it is the only thing this review contributes,
so spend your time there.

The exception is real: if you suspect this change breaks something the listed
gate does NOT cover — another crate's tests, a target outside its scope — run
that one specific check. Gaps in the gate are exactly what a reviewer is
positioned to catch. Name the command you ran in the finding.

Narrate as you go: before each new activity (reading the diff, opening a
surrounding file, checking a spec section, composing findings), output one
short present-tense line of plain text naming it, e.g. "checking error
handling in parser.rs against spec section 3". These lines stream to a
liveness monitor; a long silent stretch reads as a hang. Narrate every
activity change — err on the side of too many lines, not too few.

Report findings as a numbered list. Each finding: one-line title, 1-3 sentence
body naming file and line, and a severity tag:
- blocker  — wrong behavior, spec violation, or missing required work
- minor    — works but has a real defect (error handling, edge case, quality)
- nit      — style/polish only

End with a one-line verdict: APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES.
If you find nothing, say so explicitly — do not invent findings.

## Specification

[The same work spec sent to the implementer — verbatim]

## Type Design Contract

[Complete contents of ~/.claude/docs/type_design.md, copied verbatim.]

## Diff

[git diff output + contents of new untracked files]

## Review Questions

1. Does the implementation match the specification — complete and correct?
2. Any bugs, missed edge cases, or broken error handling?
3. Anything implemented that the spec did not ask for?
4. Does it fit the existing codebase's patterns?
5. Can every domain type be understood from its name, and has each bare
   `Option<T>` in an owned API been replaced or justified by an external API
   boundary and converted there?
```

**BLINDNESS RULE:** the review prompt must NOT contain ${IMPL_SUMMARY} or any hint of what the implementer claims it did. Spec + diff only.

**Step 3 — Launch the delegate review:**
Run `bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md" review "<responsibility>" "<current review activity>" "${REVIEW_PASS}"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true` — `<responsibility>` starts with the same plan/phase line as the implementation dispatch, then 1-2 lines naming what is under review (e.g. `tool-graph.md — phase: 3` newline `Blind review of the diff against its Work Order; no implementer summary provided`). `<current review activity>` starts as a concise phrase such as `reviewing the retry recovery diff`; later progress reports replace it with the newest heartbeat activity. The launcher records the review pass and called agent identity automatically.

Retain the returned handle and arm the progress monitor over `review_status`
per <ProgressMonitor/>. The main agent performs
Step 4 while the review runs, then applies the **Background wait invariant** until
that handle completes. Narration during Step 4 is unnecessary — the main agent is
visibly working — so let it lapse there and resume once Step 4 is done and the
only remaining activity is the wait.

**Step 4 — the main agent's own review, while the delegate agent reviews:**
(The main agent MAY read ${IMPL_SUMMARY} — only the delegate reviewer is blind.)
1. Read every changed file (or changed sections for large files)
2. Verify against the spec: correctness, completeness, nothing extra
3. Check codebase consistency and — for Rust — style-guide conformance
4. Note where ${IMPL_SUMMARY}'s claims diverge from what the diff actually shows
5. Record your own findings with the same severity scale

**Step 4a — preempt an obsolete blind review.** Do this as soon as Step 4
confirms a substantial defect whose correct repair is already unambiguous from
the Work Order; do not wait merely to collect a second opinion. A defect is
substantial when it is wrong behavior, a specification violation, missing
required work, or a non-trivial change to logic or error handling. It is not a
style issue, a documentation-only change, a one-line mechanical correction, or
anything whose intended behavior remains unclear.

1. If the blind reviewer has already completed, use its findings normally and
   continue to Step 5.
2. If it is still active, read `${SESSION_DIR}/review_agent.log` once. Preserve
   any completed observations that bear on the confirmed defect as
   `${PARTIAL_AGENT_REVIEW}`; an absent, partial, or silent log is not an
   approval and does not delay the repair.
3. Cancel the blind-review handle with the environment's background-task
   cancellation mechanism. Stay attached until cancellation completion is
   reported; cancellation replaces the ordinary completion wait for this one
   review, never the same-turn wait requirement. Then run
   `python3 ~/.claude/scripts/delegate/progress_history.py finish-pass --session-dir "${SESSION_DIR}" --status canceled`; a forcibly canceled launcher cannot record its own ending.
4. Record the confirmed defect with `findings.py open`, run `findings.py
   gate` and then `findings.py dispatch --covers <its id>`, compose a normal
   delegate fix prompt for it (including any useful `${PARTIAL_AGENT_REVIEW}`
   evidence), and dispatch it immediately using the auto fix round rules in
   <Synthesize/>. Preempt at most once per phase: a second obsolete-review
   cancellation means the broad review is never completing, so let it finish.
   Tell the user in one line that the blind review was canceled because the
   confirmed defect already requires this fix.
5. After the fix returns, run a fresh <DualReview/> of the new diff. Do not
   synthesize the canceled review as a verdict and do not use it to qualify for
   the direct-fix exception.

**Step 5 — Collect:** when the background task notification arrives, read ${SESSION_DIR}/review_status:
- **"reviewed":** Read ${SESSION_DIR}/review_findings.txt → ${AGENT_REVIEW}
- **"error":** Read ${SESSION_DIR}/review_agent.log, tell the user the delegate review failed, and proceed on the main agent's review alone (say so explicitly).

Both paths are the current pass's artifacts — `review.sh` writes
`review_findings_${REVIEW_PASS}.txt` and `review_agent_${REVIEW_PASS}.log` and
points the unnumbered names at them, so every earlier round stays readable.
</DualReview>

---

<ClosureReview>
**Replaces <DualReview/> Step 2's template whenever `${REVIEW_PASS} > 1`.**

Run `python3 ~/.claude/scripts/delegate/findings.py status --session-dir
"${SESSION_DIR}"` and build the prompt from it. Write
${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md:

```
You are checking one repair. A previous review of this change produced the
findings listed below and a delegate has just repaired them. Your job is to
decide whether each one is actually fixed and whether the repair broke
anything adjacent. This is not a fresh audit of the whole change.

You have read-only access to the codebase. Do not re-run the verification the
implementer already ran.

Narrate as you go: before each new activity, output one short present-tense
line of plain text naming it. These lines stream to a liveness monitor.

## Findings under repair

[One block per open finding from `findings.py status`: its id, severity,
file:line, and title. Verbatim — keep the ids.]

## What the repair touched

[The files the fix prompt named, plus any path that appeared in
`git status --short` after the fix that was not in the pre-fix snapshot.]

## Diff of the repair

[git diff limited to the touched paths + contents of any new untracked file.]

## Answer exactly two questions

1. For EACH finding id above, is it actually fixed? Answer per id with one of:
   FIXED, NOT FIXED, or UNCLEAR — and one sentence of evidence naming the
   file and line you read.
2. Does the repair break a caller, consumer, state transition, or invariant of
   the symbols it changed? Name each one you checked and whether it holds.

Then stop. Do NOT audit parts of the change outside the touched paths and do
NOT report style, polish, or design observations about already-reviewed code.
If you believe something outside the touched paths is now broken, you may
report it only by quoting the specific hunk of THIS repair that breaks it and
naming what it invalidates. A general concern without that hunk is not a
finding here.
```

**The prompt carries no `## Review Questions` section and no Type Design
Contract.** Those belong to the broad review; repeating them is what turns a
closure check back into a full audit.

**Record the verdicts.** For each id the closure review answered, and only after
the main agent's own pass agrees:

- FIXED, confirmed → `findings.py verdict --id <id> --state accepted`
- NOT FIXED or UNCLEAR → `findings.py verdict --id <id> --state still_open`
- An accepted finding the reviewer invalidated with a quoted hunk →
  `findings.py verdict --id <id> --state reopened --evidence "<the hunk and
  the dependency it invalidates>"`

New problems the repair introduced are new findings: `findings.py open`. Then
continue to <Synthesize/>, which gates the next round.
</ClosureReview>

---

<Synthesize>
**Goal:** Merge both reviews and present one verdict.

1. Merge ${AGENT_REVIEW} with your own findings. Dedupe — one entry per real issue, tagged with who caught it (delegate / main agent / both). Discard delegate findings you can refute by reading the code; say which and why.

**TRANSLATE — do not pass reviewer vocabulary through.** <UserFacingText/>
applies in full here. The user has not read the plan, the diff, or the two
reviews, so its banned-vocabulary rule also covers the reviewers' own finding
numbers and titles: name the real problem, not what a reviewer called it. If the
user says a finding did not land, execute <ExplainOnDemand/> before re-offering
the choices.

2. Present in two layers — readable summary first, technical reference second:

```
## Delegation Result

### Where things stand
[2-4 sentences, no jargon: what the delegate agent actually built (what it does, not the type
names), whether anything visibly changed yet, and that the important parts work
/ pass tests. Written for someone who has not seen the code.]

### What's left
[One numbered item per confirmed issue. For EACH, with no jargon:
- A title naming the real problem (not the reviewer's title).
- 1-2 sentences: what the actual behavior or risk is, what breaks or could break.
- How much it matters: does it happen in normal use, is it a rare fallback, or
  is the code already correct and this only guards against a future edit?
- Cost to fix: cheap vs. involved, and any hard limit (e.g. "can only be written
  correctly, not proven here, because it needs a real screen").
If there are no issues, say so in one sentence and skip the table below.]

### Reference (file/line for the fix pass)
| # | Severity | File:line | Problem (technical) | Caught by |

### Reviewer disagreements (if any)
[Where the delegate review and yours diverge — give your take without jargon, don't
manufacture consensus.]
```

The numbered items in **What's left** and the rows in **Reference** must use the
same numbers so the user can cross-walk if they want detail.

3. **DIRECT-FIX EXCEPTION (post-review only).** Before offering choices, check
whether every remaining confirmed issue is one of:
- a **documentation-only update** (doc comments, markdown, plan docs — no code
  behavior change), or
- a **trivial change** — a fix so small and mechanical that dispatching a delegate
  session would cost more than the fix itself (a one-line correction, a typo, a
  rename already agreed on). Not trivial: anything touching logic, error
  handling, or more than a couple of lines.

If ALL remaining issues qualify AND both reviews agree on them (the delegate
reviewer flagged it or its review is consistent with it, and the main agent's
own review confirms it), the main agent applies the fixes directly — do NOT ask the user, do NOT
dispatch a fix pass to the delegate agent. Then tell the user in one or two sentences exactly
what was changed and why it qualified (doc-only / trivial). Skip the choice
menu and continue to <RunApplicationSmokeTest/>.

If even one remaining issue is substantial, or the reviews disagree, the
exception does not apply — everything routes through the normal choice menu
below. This exception is only available after <DualReview/> has run; it never
applies to the initial implementation.

4. **AUTO-ROUTE.** Otherwise (confirmed blocker or minor issues remain), route
without asking:

   **Only real choices reach the user** — before routing anything to STOP, apply
   <UserFacingText/>'s three tests to every option you would present. An option
   that contradicts the phase's own structure — an acceptance line asserting a
   guarantee the phase has no code path to deliver, a test for a boundary that
   does not exist yet — is not buildable. Correct the plan document yourself,
   move the guarantee to the phase that can enforce it, tell the user in one line
   what you corrected, and continue. Same standard as the resequencing rule: this
   is a correction, not a decision, so long as it preserves product behavior,
   public API, scope, invariants, and required verification.

   **Defer first** — if an issue is really a decision that affects only later
   phases and the current phase's acceptance gate passes without it, apply the
   blocking-vs-deferrable rule (see Multi-phase modes): record it as a
   `**Pending decision:**` block on the affected phase, tell the user in one
   line, and drop it from this phase's issue list.

   **Resequence before deciding** — if the remaining issue changes only when
   already-specified work happens, apply the pure-sequencing rule from Loop
   mode immediately. Update the plan document, including phase numbering,
   dependencies, Files, Acceptance gates, and test ownership where affected;
   do not ask the user. Re-evaluate the current diff against the revised Work
   Order, then auto-dispatch whatever additional implementation or correction
   is needed for that revised phase. A sequencing change is pure only when it
   preserves product behavior, public API, scope, invariants, and required
   verification. If any of those must change, it remains a real decision and
   follows the normal blocking/deferral rules.

   **Record, then let the ledger decide.** `findings.py open` every remaining
   confirmed issue (see <FindingsLedger/>) — one call per issue, with the
   severity the merged reviews settled on. Then run
   `python3 ~/.claude/scripts/delegate/findings.py gate --session-dir
   "${SESSION_DIR}"` and follow its verdict. Do not decide this yourself, and
   do not count rounds:

   - `converged` — no gating finding is open. Report the non-gating leftovers
     in one line for the retrospective and continue to
     <RunApplicationSmokeTest/>.
   - `dispatch` — run the **auto fix round** below over the whole `batch`.
   - `stop` — go to **STOP** below and give the user `stop_reason` in plain
     words.

   **Auto fix round** — when `gate` said `dispatch` and every issue in the
   batch has an unambiguous correct fix (the spec answers it and the two
   reviews do not conflict on intended behavior): set ${FIX_ROUND} to the
   gate's `round`, write
   ${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md covering **every** id in the
   batch — a fix prompt that repairs a subset is rejected at `dispatch`, and
   the batch is meant to be repaired together by root cause (same structure as the work order,
   spec = the confirmed issues table with file/line specifics, same no-commit
   rules, heartbeat instruction, the complete `## Type Design Contract` copied
   verbatim from `~/.claude/docs/type_design.md`, and style requirements;
   verification = only
   the `verify.sh` lines the confirmed issues implicate — typically `check` +
   `test`, adding `lint` only when lint findings are being fixed, never the
   whole phase gate), select `${FIX_TASK}` — `mechanical` only
   when every confirmed issue is documentation, formatting, lint guidance, a
   trivial rename, or an equivalently behavior-preserving edit; `escalation`
   when review found incorrect behavior, numerical/transform math, unresolved
   architecture, or a prior fix failed; otherwise `implementation` — then run
   `python3 ~/.claude/scripts/delegate/findings.py dispatch --session-dir "${SESSION_DIR}" --covers "<every batch id, comma-separated>"` — this
   records the round and refuses an incomplete batch, so run it before the
   launcher, not after — then run
   `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" "${FIX_TASK}" "<responsibility>" fix "<current fix activity>" "${FIX_ROUND}"`
   (background, unsandboxed) — `<responsibility>` starts with the plan/phase
   line, then 1-2 lines naming the fix round and the confirmed issues it
   addresses (e.g. `tool-graph.md — phase: 3` newline `Fix round 2 — restore
   the error path dropped from the parser; both reviews flagged it`).
   `<current fix activity>` is a short phrase such as `restoring parser error
   handling`; the launcher records `fix ${FIX_ROUND}`, called agent identity,
   and the pass outcome automatically. Tell
   the user in one line what is being fixed, and arm the progress monitor over
   `impl_status` per <ProgressMonitor/>.
   Then re-execute <DualReview/> — which, at `${REVIEW_PASS} > 1`, is
   <ClosureReview/> over this repair, not another audit of the phase — and
   return here.

   **STOP** — when any remaining issue needs a design decision the plan does
   not answer *and that has at least two buildable answers*, when the two
   reviews conflict on *intended behavior* (not just severity), or when `gate`
   returned `stop`. Present the
   two-layer result above plus the choices — each option one sentence, no
   jargon, with a recommendation and the reason for it:

```
Your choice:

1. One more delegate fix pass — [name what gets fixed and the cost].
   ([Recommended / not] because [reason].)
2. Stop here — the parts that matter work; the leftover items become written-down
   todos for later.
3. Talk through any item first.
```

   Do not surface internal bookkeeping (finding ids, round numbers) as the
   headline. When `gate` returned `stop`, option 1 must say in plain words what
   stopped converging — that the same problem survived two repairs, that a fix
   undid an earlier one, or that the list stopped shrinking. That is the whole
   reason the user is being asked. **Wait for the user.**

   - **1:** An explicit user choice overrides the gate — dispatch as in the
     auto fix round above, running `findings.py dispatch` first.
   - **2:** Continue to <RunApplicationSmokeTest/>.
   - **3:** Discuss; afterwards re-offer the options.

If there are no issues (or nits only), state that and continue to
<RunApplicationSmokeTest/>.

**RULE:** The main agent does not write or edit implementation code in this command unless the user explicitly says so. All fixes route to the delegate agent by default. Sole exception: the direct-fix exception above (doc-only or trivial post-review fixes both reviews agree on).
</Synthesize>

---

<RunApplicationSmokeTest>
**Required after every phase, after review fixes and before phase review or a
checkpoint.**

**Goal:** Demonstrate that the repository's runnable product still starts and
that the runtime behavior added or changed by the phase works without a panic,
fatal error, or immediate shutdown.

1. Determine the runnable target and command from, in order: the Delegation
   Context's **Run** or **Smoke** entry, the phase Acceptance gate, repository
   instructions, and the relevant package manifest. This narrow inspection is
   required even on the delegate-ready fast path. Do not treat a successful
   build, test binary, static example build, or delegate claim as an application
   smoke test.
2. Launch the real application or executable directly from ${WORKING_DIR} with
   backtraces and useful runtime logging enabled. Keep the process attached and
   capture its output. For a repository with a primary application, run that
   application after every phase, including library-only phases. If the
   repository genuinely has no runnable product, run the closest executable or
   example that integrates the changed code and record why it is the applicable
   target.
3. Exercise the runtime path added or changed by the phase. Merely reaching the
   first frame is sufficient only when the phase has no changed runtime behavior
   to invoke. For GUI, input, hardware, networking, persistence, or other
   interactive work, perform the relevant action and continue the application
   long enough to observe its result. Automate the action when safe. If the
   environment cannot perform a required real interaction, keep the bounded
   smoke run attached, ask the user to perform that exact action, and wait. Do
   not report a pass with an unexercised runtime path.
4. Close the application cleanly after the exercised behavior remains stable.
   Set ${APPLICATION_SMOKE_RESULT} to a concise record of the command, exercised
   behavior, and observed result.
5. A panic, fatal log, unexpected exit, or incorrect exercised behavior is a
   blocker for the current phase. Capture the backtrace and relevant logs, route
   the confirmed issue through the same automatic delegate fix logic in
   <Synthesize/>, then rerun <DualReview/>, <Synthesize/>, and this smoke test.
   Never run <RunPhaseReview/>, <CheckpointCommit/>, or a verbose completion
   report until the smoke test passes.
6. If no applicable executable can be found or a required interaction cannot be
   performed by either the main agent or the user, STOP with the phase
   incomplete. Environment limits are not a successful application smoke test.
</RunApplicationSmokeTest>

---

<RunPhaseReview>
**Only when the work came from a phased plan doc.** A phase review is mandatory — do not ask, do not offer to skip.

Tell the user in one line: `Phased plan — running /plan:phase_review to update <plan doc> (retrospective + remaining-phase re-evaluation).` Then invoke the `plan:phase_review` skill immediately — **in loop or verbose mode pass `auto`**, so user decisions are deferred into the affected Work Orders as `**Pending decision:**` blocks instead of asked inline; either multi-phase mode stops for them at that phase's pre-dispatch check. When writing the retrospective, include relevant facts from ${AGENT_REVIEW} and the fix passes (e.g. what the blind reviewer caught, what deviated from spec).

**Pass this run's `${SESSION_DIR}` and `${WORKING_DIR}` down.** That command's
architect review dispatches through `review.sh` into the same session directory,
at a pass index one past this phase's last code review, so its findings file
never overwrites a review's and its progress narrates from the same heartbeat
log. It must not create a session of its own.

If the work was not from a phased plan, skip this step silently and continue to
<RecordPhaseCompletion/>.
</RunPhaseReview>

---

<CheckpointCommit>
**Loop and verbose modes only** — `single` skips this step without committing.

1. Confirm ${APPLICATION_SMOKE_RESULT} records a passing live application run
   that exercised the current phase's changed runtime path. If it is `not_run`,
   incomplete, or failed, STOP; do not commit.
2. Run `git status --short` in ${WORKING_DIR} and confirm the changes are this
   phase's implementation plus the plan doc. Anything unexpected → STOP and ask.
3. Run `bash ~/.claude/scripts/delegate/verify.sh fmt <package>` for each
   package the phase touched — fix passes skip `lint` (the only formatting
   phase-gate step), so formatting must be re-proven here. Formatting-only
   changes join the checkpoint commit.
4. Edit the phase's status line in the plan doc to `status: done`. Never record
   the commit hash in the plan doc — the commit does not exist yet at this
   point, and amending afterwards to add one only writes a hash the amend
   itself invalidates.
5. Stage everything and commit with this message shape:

   ```
   checkpoint(<plan-slug>): phase N — <phase title>

   <one line: what the phase built>

   Claude-Session: <session url>
   ```

6. Report one line: `Checkpoint <short hash> — phase N: <title>.`

Never push. Never commit anything outside this step.
</CheckpointCommit>

---

<RecordPhaseCompletion>
After the smoke test, phase review when applicable, and checkpoint when
applicable have all succeeded, run:

`python3 ~/.claude/scripts/delegate/progress_history.py finish-phase --session-dir "${SESSION_DIR}" --status completed`

This finish timestamp is the outcome used to score every earlier percentage in
the phase. Never record `completed` before all required phase gates pass.

In `single` mode, then run
`python3 ~/.claude/scripts/delegate/progress_history.py finish-run --session-dir "${SESSION_DIR}" --status completed`, run
`bash ~/.claude/scripts/delegate/end_session.sh`, and end. Loop and verbose modes
continue to their normal report/gate/next-phase path.
</RecordPhaseCompletion>

---

<VerbosePostPhaseReport>
**Verbose mode only, after every completed phase, including phases inside a
bounded-auto window.**

**Goal:** Output only what the reviewed phase actually delivered. Do not include
the next phase's purpose, planned work, types, files, verification, or any other
pre-phase briefing content.

Build this summary from the phase Work Order, the reviewed diff captured in
<DualReview/>, ${IMPL_SUMMARY}, accepted fixes, the phase retrospective, and the
checkpoint. The diff and review are authoritative when an implementation detail
differs from the original plan. Do not merely repeat the Work Order or the
delegate's claims.

```
## Phase N complete — <phase title>

### Why this phase exists
[The point of the phase in 2-4 sentences: what capability or foundation it adds,
what later work depends on it, and what remains deliberately outside it.]

### What now works
[A concise behavior-focused summary of the reviewed implementation.]

### Important types and APIs
| Type / trait / API | Status | Role | How it works with the rest of the system |
| --- | --- | --- | --- |
[Only load-bearing new or materially changed types, traits, resources, enums,
and public methods. Explain ownership, inputs/outputs, important lifecycle, and
persistence/runtime boundaries where relevant. If none were introduced, say
"No new load-bearing types in this phase" instead of manufacturing entries.

**Status** is exactly one of `New`, `Existing - Changes`, or
`Existing - No Changes`, read off the reviewed diff — the diff is authoritative
over the Work Order's expectation, so a type the pre-phase briefing called
`New` that landed as a change to an existing type is reported as
`Existing - Changes`, and the difference is worth one line. Include an
`Existing - No Changes` row only when that untouched type is needed to
understand the phase's change.]

### Verification and review
[Acceptance gate result, meaningful tests/lint, review outcome, fixes, and the
mandatory live application smoke command, exercised behavior, and result from
${APPLICATION_SMOKE_RESULT}.]

**Checkpoint:** `<short hash>`

```

Do not include the next phase's briefing or ask whether to start it in this
report. <VerbosePostPhaseGate/> separately waits for `continue` before the next
briefing when no bounded-auto window is active.
</VerbosePostPhaseReport>

---

<VerbosePostPhaseGate>
**Verbose mode only, after <VerbosePostPhaseReport/>.**

If no `todo` phase remains, skip this gate and let <NextPhase/> finish with
<RunSummary/>. If a bounded-auto window is active, skip this gate and let
<NextPhase/> continue or end the window according to its existing rules.

Otherwise, show exactly this control line after the completed-phase summary and
wait:

`Reply \`continue\` when you are ready to review the next phase's pre-phase briefing, or \`stop\` to end the run.`

- `continue` — advance to <NextPhase/>. This authorizes only composing and
  displaying the next phase's <VerbosePrePhaseGate/>; it does not authorize a
  delegate dispatch, implementation, review, fix, phase review, or checkpoint.
- `stop` — emit <RunSummary/> with `user stopped after phase N` and end without
  composing the next phase's briefing.
- `proceed`, `approved`, a question, or discussion without `continue` does not
  advance. Answer any discussion using only the completed phase's report and
  preserve this gate. If the report did not land, execute <ExplainOnDemand/>.
</VerbosePostPhaseGate>

---

<NextPhase>
**Loop and verbose modes only.**

1. Find the next `todo` phase in the plan. If none remains, run <FinalGate/>,
   then <RunSummary/>, and end. The final verbose phase already received
   <VerbosePostPhaseReport/>.
2. Reset ${REVIEW_PASS} = 0, ${IMPLEMENTATION_TASK} = implementation, and
   ${APPLICATION_SMOKE_RESULT} = not_run.
3. If MODE = loop, announce `Continuing to phase N — <title>.` and loop to
   <ComposeWorkOrder/> (STEP 2).
4. If MODE = verbose and `AUTO_WINDOW = none`, announce
   `Preparing the Phase N briefing — <title>.` and loop to STEP 2.
   <VerbosePrePhaseGate/> waits before dispatch.
5. If MODE = verbose and `AUTO_WINDOW = next N`, decrement N for the phase just
   completed. If N is now zero, clear the window, announce the next phase's
   briefing, and loop to STEP 2. Otherwise announce the next phase and loop to
   STEP 2; STEP 3 skips the gate while the window remains active.
6. If MODE = verbose and `AUTO_WINDOW = through X`, compare the phase just
   completed with X. If it is X, clear the window, announce the next phase's
   briefing, and loop to STEP 2. Otherwise announce the next phase and loop to
   STEP 2; STEP 3 skips the gate while the window remains active.

Every path back to STEP 2 still runs the pending-decision pre-dispatch check.
</NextPhase>

---

<FinalGate>
**Loop and verbose modes, once, when the plan is exhausted — before
<RunSummary/>.** Per-phase gates were deliberately scoped (see **Delegate
verification (Rust)**); this is the single full-breadth pass.

1. Run `bash ~/.claude/scripts/delegate/verify.sh final` with
   `run_in_background: true` — workspace `fmt --check`, `--all-targets` check
   (the only time every example builds), and the full test suite — and apply
   the **Background wait invariant**.
2. Rust plans: run the `clippy` skill with `auto-proceed` (main agent, inline).
3. Failures route like review findings: compose a fix prompt scoped to the
   failures. Before the first such dispatch, capture a new
   `${SESSION_DIR}/progress_baseline_status`, run `start-phase` with phase ID
   `final` and title `Final verification`, and reset `${REVIEW_PASS}` to zero.
   `start-phase` resets the findings ledger with it. Then dispatch per the
   <Synthesize/> auto fix round rules and rerun this gate. The convergence test
   applies to this final-verification phase independently.
4. Once the entire gate is green, if the synthetic `final` phase was started,
   record it with `finish-phase --status completed`. Its completion timestamp
   scores the progress estimates made during final-gate fixes.
5. Record the outcome for <RunSummary/>'s **Final gate** line.

Single mode and non-plan-complete endings (user stop, blocking stop, error)
skip this gate — say so in the summary; the tree may not be workspace-clean.
</FinalGate>

---

<RunSummary>
Emitted whenever a multi-phase run ends — plan exhausted, verbose user stop,
blocking stop, or error.

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Final gate:** [green / green after N fix rounds / skipped — <reason>]
**Deferred decisions still open:** [one line each, naming the phase that owns it — or "none"]
**Why the run stopped:** [plan complete / user stopped before phase N / pending
decision on phase N / phase N stopped converging: <reason> / delegate error]
```

Same translation rules as <Synthesize/>: no reviewer vocabulary, no bare codes —
every line must stand on its own for a reader who has not seen the plan.

After emitting the summary, record the durable run outcome:

- plan complete → `python3 ~/.claude/scripts/delegate/progress_history.py finish-run --session-dir "${SESSION_DIR}" --status completed`
- user stop, pending decision, or a `stop` gate verdict → the same command with `--status stopped`
- delegate or environment error → the same command with `--status error`

`finish-run` automatically closes an active pass/phase as incomplete, so
partial runs remain measurable without being included in completed-phase
calibration. Then run `bash ~/.claude/scripts/delegate/end_session.sh`
to clear the run-active marker. This is mandatory on every ending, including the
ones that stop early — leaving it set keeps the Stop hook pushing later,
unrelated turns to continue a run that is over.
</RunSummary>

---

## Rules

- ${WORKING_DIR} is whatever the current project directory is — often a worktree checkout. Never create a worktree or switch branches. The only commits are <CheckpointCommit/> checkpoints in loop or verbose mode — one per completed phase.
- **Verify a blocker before naming one** — <UserFacingText/>'s three tests decide what may appear in a gate briefing. A stale status doc, an unpushed branch, or a decision the plan already recommends fails them: read the doc's claim against the actual tree, push the branch, follow the plan's recommendation.
- **An unpushed branch is NEVER a blocker.** When a phase's work needs a commit reachable from the remote — a `git = "…", rev = "…"` pin, a cross-repo consumer, a CI run — pushing the working branch is a mechanical step of that phase. Push it, compute the rev, continue. Do not list it in a <VerbosePrePhaseGate/> briefing, do not raise it as a prerequisite, do not ask.
- All delegate-launching scripts run with `dangerouslyDisableSandbox: true` and `run_in_background: true`.
- **Every script in `~/.claude/scripts/delegate/` runs with `dangerouslyDisableSandbox: true`**, launcher or not — `prepare_session.sh`, `implement.sh`, `review.sh`, `verify.sh`, `findings.py`, `progress_history.py`. They write to `~/.local/state/plan-delegate/`, which the sandbox denies. Do not try sandboxed first: `findings.py` mutates the session ledger *before* it appends to the durable store, so a sandboxed call half-applies — the round is recorded, the history event is lost, and the retry then refuses the round as already dispatched. Foreground bookkeeping calls (`findings.py`, `progress_history.py`) take `dangerouslyDisableSandbox: true` alone, without `run_in_background`.
- **Tooling mechanics never reach the user.** Sandbox flags, script names, ledger internals, status files, and the recovery for a half-applied call are the main agent's business. They do not appear in progress narration, gate briefings, <Synthesize/> summaries, or post-phase reports — not as an aside, not as a "process note". The user asked for a phase of work; report the work. If a tooling failure genuinely changes what the user gets, say what changed in terms of the work, not the tool.
- The **Background wait invariant** is mandatory. No active delegate terminal may outlive the primary-agent turn that launched it.
- A confirmed substantial, spec-defined defect found during the main side of
  <DualReview/> preempts a still-running blind review: read its partial log if
  available, cancel and wait for cancellation, delegate the repair immediately,
  then run a fresh dual review of the repaired diff.
- `${SESSION_DIR}/heartbeat.log` is for on-demand status only (see **Delegate heartbeat**): a single read when the user asks what is happening, once after compaction, or one staleness check on an overdue delegate — never a wait loop, never a completion signal.
- The delegate reviewer is always a fresh session and always blind to the implementer's summary.
- Delegate launchers record task, family, model, effort, pass timing, and pass
  outcome in both the session directory and durable progress history. Never
  rely on an empty effort silently becoming `xhigh`.
- Select `escalation` from the actual Work Order or review outcome, never keyword matching.
- The main agent orchestrates and reviews; the delegate agent codes. The main agent touches implementation code only on explicit user instruction — except post-review doc-only or trivial fixes that both reviews agree on (see the direct-fix exception in <Synthesize>), which the main agent applies itself and reports.
- Every delegate dispatch arms the <ProgressMonitor/> and narrates what
  the delegate is doing with the mandatory project section followed by the
  phase/pass section, then the ordinary-English current status defined under
  **Progress narration while waiting**, until it finishes
  (**Progress narration while waiting**). Other user work takes precedence over
  the narration; the narration never replaces the completion notification.
- Any signal that the user does not understand — at any gate — triggers
  <ExplainOnDemand/>: rebuild from the bottom, stay technical, put a short code
  example under every mechanism, and preserve the gate. Terseness is the default
  everywhere else; here it is the defect.
- The fix loop is bounded by convergence, not a counter (<FindingsLedger/>).
  `findings.py gate` decides `converged` / `dispatch` / `stop`; the main agent
  never rules on it. One batch per round, blockers-only gating after round 1,
  and a stop when a finding fails to close twice, reopens twice, or the gating
  open count stops falling. An explicit user choice overrides a `stop`.
- Auto/loop mode stops only for: an unresolved `**Pending decision:**` on the phase being dispatched, a fix that needs a design decision the plan does not answer, reviews conflicting on intended behavior, a `stop` verdict from `findings.py gate`, or a delegate/environment error. Everything else auto-routes or defers.
- Verbose mode has all of those stops plus a mandatory <VerbosePrePhaseGate/>
  before every phase outside an active bounded-auto window, a
  <VerbosePostPhaseReport/> after every completed phase, and a separate
  <VerbosePostPhaseGate/> that waits for `continue` before showing the next
  briefing. A successful phase and the post-phase `continue` never imply
  authorization for the next implementation.
- Delegate verification is `verify.sh` lines only (see **Delegate verification (Rust)**): work orders and fix prompts never contain raw cargo commands, and a delegate running one — or any unrequested check — is a Work Order violation. The full `clippy` skill runs only in <FinalGate/>, with `auto-proceed`.
- Every phase must pass <RunApplicationSmokeTest/> before phase review,
  checkpoint, or completion reporting. The main agent must run the actual
  product and exercise the phase's changed runtime path; builds, automated
  tests, and an untested startup screen do not satisfy this gate.
- Every workflow exit records `finish-run` before `end_session.sh`; completed
  phases feed calibration, while stopped/error phases remain audit evidence but
  never train percentage suggestions.
