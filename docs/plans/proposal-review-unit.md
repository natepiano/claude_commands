# Proposal: a review unit with long-lived reviewer pools

> **Status: BUILT.** The user approved a trial on 2026-10-12 and it shipped;
> `docs/as-built/review-pool.md` is the current design and wins wherever this
> proposal differs. Known differences: the production doc line is
> `**Review pool:**`; a lease ends with the mesh verb `reset`; `follow` takes
> `--read-only`, with no `--sandbox` or `--output-schema` and no JSON findings;
> `allocate` takes `--pass` and `--lens`, not `--kind`; the slot is
> `pool_<reviewer>`; turn records live under the history root's
> `review_turns/`; the report is `review_pool_report.py`, not
> `seat_usage.py --reviews`; the review unit has a worktree; `agent_exec.sh`
> and `sweep` did not change; the UI reviewer's standing prompt holds its
> whole judging procedure, every rule's full text and the production
> repository's design decisions, so a pooled design check reads no rule or
> command file; merge holds count as escapes by unit, phase and plan. Sections 6 to 8 (risks, the trial verdict rule
> and the directors' input) are stated nowhere else. The proposal as written:
> A production starts one review
> unit. Its reviewers are long-lived Codex threads in two pools, code and UI.
> Unit directors send every review to it through one script call. That call gives
> the review to an idle reviewer of the right type, or creates a new reviewer at
> once. No review ever waits. Written 2026-10-10 by a model-study research agent.

## User decisions 2026-10-12

- "for sure i want these to be show-runner specific - so showrunners would have their reviewer pool - somehow - probably through a unit director that does the allocations"
- "script allocates directly for sure - i want no tokens wasted on allocation"
- "yes build this as a trial - but can we trial it on hana? it is our most expensive work and i would like to mitigate it's token usage as soon as possible"

Showrunner calls (packaging), 2026-10-12:
- A new unit in build-followups builds it now, in parallel with the other units.
- The trial runs in the Hana production, not build-followups: Phase A probes, then the code pool switched on in Hana's production (section 7's Phase B moves there), then the review unit director and memory, then the UI pool.
- The comparison split by hash of unit and phase holds until 20 pool reviews give a tokens-per-review reading; if the pool is ahead then, every Hana review goes to the pool and escapes keep being watched to the full verdict.

## Source

The user, 2026-10-10: "should a showrunner instantiate a unit that is dedicated
to reviews - a special kind of unit director that round robins reviews to its
review workers and the other unit directors send reviews to it - then the
reviewers build up review context over time - a production reviewer could either
be a code reviwer or a ui/design reviewer - both would get different context -
such as teh rust style for the code reviewers and the ui style for the ui
reviewers. [...] as many concurrent reviewers as they need and it's okay to sit
idle [...] if we then instantiate reviewers (of aany kind) they could get special
prompts for their workert ype"

Later the same day:
- "i woudl be doing this in intrest of token efficiency and context built up over time"
- "the review unit should not have to think about this - it should have a script
  that it calls to either allocate a review to an existing, or create a new
  worker to take it - so there isn't any ambiguity or waiting"
- "waiting in line won't happen - we'll spawn new reviewers if we don't have enough"

## Success criteria

The user named two. Every choice below is judged by them.

1. **Token efficiency.** The pool spends fewer weighted Codex tokens per review
   than the reviewers a unit opens for itself (uncached input 1, cached input
   0.1, output 8, the weights `seat_usage.py` already uses).
2. **Context built up over time.** Reviews get better as the reviewers gain
   experience: more of what they raise is confirmed, and fewer defects get past
   them.

The two pull against each other. Codex sends a thread's whole context on every
model request, and a review turn makes 15 to 40 requests, so each 10k tokens a
reviewer keeps costs 1k weighted tokens *per request*. A reviewer that keeps
everything it has seen pays for it again on every tool call. The design keeps
what is learned in a small, curated standing prompt, and drops each review's
diff and code when the work for that phase is done.

## Recommendation

Build it, in four phases, behind a measured trial. The key calls, all mine:

- **Allocation is a script, not a session.** `review_pool.py allocate` runs inside
  the requesting unit's own `review.sh`. It picks or creates the reviewer under a
  file lock and returns at once. The review unit's director is never on the path
  of a review: if it is down, every review still runs.
- **The review unit director curates; it does not dispatch.** Its work is the
  pools' standing prompts and their **review memory**: recurring defect classes,
  per-crate idioms and settled rulings, taken from closed findings after each
  merge. This memory is where context builds up over time.
- **A reviewer serves one unit's phase at a time (a lease), then reverts.** The
  broad review and every closure review of that phase go to the same reviewers,
  so a closure reads code already in context. When the phase checkpoints, the
  thread is reverted to the turn right after its standing prompt
  (`thread/revert`). No diff, verdict or unit detail outlives the lease.
- **The standing prompt stays small** (about 7k tokens for code, 6k for UI). It
  carries indexes and pointers, never whole guides.
- **UX verdicts stay fresh, on a long-lived UI reviewer.** Every design-check
  verdict is given right after a revert, so the thread holds no earlier shot.
  What lasts is the rule list and the production's rulings. This meets the
  user's long-lived UI reviewer and the four directors' "fresh verdict" rule
  together.
- **Findings come back into the requesting unit's own session directory**, in
  today's file names, plus a JSON copy. The unit's `review.sh` records the pass
  in the unit's own progress table. The unit director still triages and opens
  ledger rows. No other process writes a unit's ledger or history.

## What exists today (measured 2026-10-10)

- **Fresh reviews:** `review.sh` without `--to` runs `codex exec --ephemeral
  --sandbox read-only` through `agent_exec.sh`, so a fresh review leaves no
  rollout. Its log ends with codex's `tokens used` total. Over the last 8 days,
  37 review logs under `/tmp/claude/delegate/` show these medians:
  - broad-review lens: about 172k–180k (n=11);
  - closure review: about 136k (n=24; mean 258k, max 687k).
- **Volume:** 39 reviews in 5 days on natedev. Peak concurrency was 2,
  median length 9.6 minutes, p90 29 minutes. Closure reviews are two thirds of
  all reviews.
- **Codex's own preamble** is 19k–26k input tokens on a thread's first request,
  before any prompt text (the first-request input of five review and seat
  rollouts). One complete review rollout (2026-10-07) made 15 requests:
  - 48k uncached input, 686k cached input and 8k output;
  - 180k weighted in total.
- **Long-lived seats** (`docs/as-built/long-lived-seats.md`): Phase 1 keeps
  `impl` and `test` open across phases and compacts them above
  `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` (100000). Phase 3 (since shipped) adds
  `review.sh --to <seat>`, so a unit's own seats take its reviews. A writer then
  reviews its own code as an adversary.
- **The app-server already supports what the pool needs** (codex 0.162 schema,
  generated with `codex app-server generate-json-schema`):
  - `turn/start` takes `cwd`, `sandboxPolicy` (`readOnly`) and `outputSchema`
    per turn;
  - `thread/start` and `thread/fork` take `developerInstructions`;
  - `thread/fork` takes `lastTurnId`;
  - `thread/revert` takes `beforeTurnId`.

  On 2026-10-10 no script called any of these; `codex_mesh.py` now does.
- **Guide sizes:**

  | Text | Size |
  | --- | --- |
  | Rust style guide, 53 rules | 105 KB (about 26k tokens) |
  | Its rule headings | 2 KB |
  | `type_design.md` | 1.2 KB |
  | UX guide, 32 rules | 45 KB of text |
  | `load-ux-style.sh --checklist` | 5 KB |

  The full Rust guide in a standing prompt would cost about 2.6k weighted tokens
  per request, or 39k per 15-request review: 22% of a fresh review, every time.

## 1. Structure

```
showrunner ── starts ──> review-unit director (curates memory, primes, keeps health)
                               │ owns
                               ▼
                 pool app-server  (one per production, its own session dir)
                 ├── code template  (standing prompt only; forked to create)
                 ├── code1  code2  code3 …   (leased to a unit's phase, or idle)
                 ├── ui template
                 └── ui1  ui2 …
unit director ── review.sh --pool code|ui ──> review_pool.py allocate ──> reviewer
```

**Words** (added to `production_format.md`):
- **Review unit:** one per production; Units row `review-unit`; it owns no source
  files.
- **Reviewer:** a long-lived thread in a pool, named `<type><n>`: `code1`, `ui2`.
- **Template:** the thread a type's reviewers are forked from. It holds only the
  standing prompt and is never leased.
- **Lease:** a reviewer's assignment to one unit's phase. It ends at that phase's
  checkpoint.

**Allocation** (`review_pool.py allocate --type code|ui --unit <unit> --phase
<phase instance id> --kind adversary|contract|closure|ux`). The rules are
checked in order, under one lock on the pool roster, and the first match wins:

1. An idle reviewer of the type holding this unit's lease for this phase. It
   must not hold the other lens of the same pass, because two lenses must be two
   independent readings.
2. An idle reviewer of the type with no lease. It takes the lease.
3. Otherwise a new reviewer. `thread/fork` of the type's template, named with
   the next number, takes the lease. A fork is one request with no turn, so it
   is ready in seconds.

The script prints the reviewer's name, its thread, and the pool's session
directory, and exits. No rule waits and no rule fails over to a unit's own
seat: when every reviewer is busy, rule 3 creates one (user, 2026-10-10). Idle
reviewers cost nothing and stay for reuse. The pool grows to peak concurrency,
and that number is what the user expected to "settle".

**Lease end.** `review_pool.py end-lease --unit <unit>` runs at the unit's
`<PhaseEnd/>`. It reverts each of the unit's reviewers to the turn after its
standing prompt. A lease with no request for 12 hours is ended by the director's
health check, which covers a unit that stopped.

**Template changes.** When a guide's hash or the review memory changes, the
director primes a new template. It never re-primes a reviewer on the request
path. An idle reviewer whose prime hash is old is retired at its next lease
end, and rule 3 forks a new one from the current template.

**No round robin.** Rule 1 sends a unit's closure review to the reviewer already
holding its phase. With no queue there is no turn order to keep.

## 2. Context per reviewer type

The standing prompt goes in `developerInstructions` at template creation. These
are thread instructions, not history, so compaction does not remove them. Phase
A confirms that on the live server. Compaction drops only lease content. Every
lease opening repeats the role contract and gives the paths again, as Long-lived
seats Phase 1 requires of every follow-up.

| Part | Code reviewer | UI reviewer |
| --- | --- | --- |
| Role | Reviewer; edits nothing. Reads the named worktree at the named head. Re-reads the tree on every review and trusts nothing from memory about its state. | Same, for shots: judges each shot as given, never from an earlier one. |
| Gods and contracts | Three Gods sentence; `type_design.md` verbatim (1.2 KB) | Three Gods sentence |
| Guide | The 53 style-rule headings with each file path (2 KB). The full rule is read only when a hunk touches it. | The UX checklist (`load-ux-style.sh --checklist`, 5 KB) with each rule file's path; `ux_eval.md`'s report format |
| Review memory, repository section | Recurring defect classes, per-crate idioms, settled type and style rulings: at most 4k tokens, each entry with its source and date | The production's look rulings (from `docs/design-decisions.md` and the doc's Production rules) and recurring visual defect classes: at most 3k tokens |
| Output | `outputSchema`: findings (severity, title, file, line, detail, failing case), then the verdict | `outputSchema`: `ux_eval`'s defect table and verdict |

**Sent at lease start, never kept past the lease:**
- the unit's own section of the review memory (its standing rules, such as
  organon's never-edit files and the 9 px floor);
- the worktree, base and head hashes;
- the Work Order section;
- the lens.

**Never kept:** diffs, code read, verdicts, shots, and other units' anything.

**What builds up over time** is the review memory: files under
`~/.local/state/review-pool/<production slug>/memory/` with one repository
section and one section per unit. Only the review unit director writes it.
After each merged checkpoint, it reads that phase's closed findings
(`findings.py status`) and the merge's design-check verdict, and records:
- a class seen twice or more;
- a ruling the unit director or showrunner made;
- a defect that got past review.

When a section reaches its cap, it prunes the least-cited entry. Reviewers never
write memory, so a reviewer's mistaken belief cannot become a standing rule.

## 3. Protocol

### Submitting a review

The unit director calls `review.sh --pool <type>` where today it calls
`review.sh --to <seat>`, with the same positional arguments. `review.sh`:

1. Writes the request beside the prompt: worktree, base hash, head hash (with
   `git add -N` applied so new files show), the Work Order path and heading, the
   lens, and `${SESSION_DIR}`.
2. Calls `review_pool.py allocate` and gets the reviewer and pool session dir.
3. Calls `start-pass` in the unit's own `${SESSION_DIR}` under slot
   `pool:<reviewer>`, as today's launcher does (<PassOwnership/> holds: the
   launcher records its own pass).
4. Runs `codex_mesh.py follow --session-dir <pool dir> --to <reviewer> --cwd
   <worktree> --sandbox read-only --output-schema <type schema>`. The
   compaction step before follow-up work applies unchanged.
5. Writes the reply as `review_findings_<pass>_<lens>.txt` (today's text form,
   so <Synthesize/> is unchanged) plus `review_findings_<pass>_<lens>.json` (the
   `findings.py open` fields: severity, title, file, line, lens, detail). It
   also writes `review_turn_<pass>_<lens>.json` (pool, reviewer, thread id, turn
   id, lease ordinal) for the measurement.
6. Calls `finish-pass`, then `review_pool.py release`, which marks the reviewer
   idle and keeps the lease.

The reviewer writes no file: the read-only sandbox forbids it, and the launcher
writes everything into the requesting unit's directory. The tree-unchanged check
from Long-lived seats Phase 3 stays as a second guard.

### Ledger and progress table

Nothing about triage changes. <Synthesize/> reads the lens files and opens
confirmed rows with `findings.py open --lens …`. The unit director owns the
ledger, and the progress table shows the pass under `pool:code2`. That answers
trunk and widget: the review unit writes nothing into their history.

### Closure reviews

<ClosureReview/>'s prompt is unchanged. Allocation rule 1 sends it to the
reviewer that holds the phase's lease, so the open ids' code is usually still in
context. A closure needs no lens, and either lease holder may take it. When both
are busy, rule 3 creates one; the closure never waits behind a broad review
(trunk's concern). <EarlyReviewArm/> works as today: the armed reviewer is
allocated at arm time and holds its lease.

### UX review

<UXReview/> step 3 calls `review.sh --pool ui` in place of spawning a Claude
helper. The shots go in as `localImage` inputs. Every notice verdict
(<UXReview/> step 6) is judged by a UI reviewer at its standing prompt: allocate
with `--kind ux` always reverts before the turn, so no earlier shot is in
context. Step 5's partial re-judges after a repair may stay on the lease, which
trunk's reply allows.

## 4. Concurrency and latency

- **No queue, by rule.** Every request gets a reviewer in the same call. The
  only added latency is a fork when no reviewer is idle: one request, seconds.
  Today a fresh `codex exec` pays its startup at a similar cost. All four people
  asked ranked queueing first; under the user's rule there is nothing to queue
  for, so no wait limit and no fallback to a unit's seat are needed.
- **A unit is never blocked by the review unit.** Allocation runs in the unit's
  own process. A dead director session changes nothing on the request path.
- **Even service across units.** Nothing waits, so no unit's review delays
  another's.
- **The real ceiling is Codex's account rate limit,** shared with every seat. A
  pool of 14 reviewers adds no load beyond the reviews themselves, which already
  run today as fresh threads. A `429` from the server fails that review with
  `error`, as today; <DualReview/> step 6 already continues on the readings that
  landed.
- **Server load:** one app-server process holds every reviewer thread. Idle
  threads hold no turn. Several units' launchers connect to it at once; the
  roster lock in `codex_mesh.py` already serializes claims.

## 5. What changes, file by file

| File | Change |
| --- | --- |
| `scripts/delegate/review_pool.py` (new) | `allocate`, `release`, `end-lease`, `status`, `health`. Pool roster at `~/.local/state/review-pool/<slug>/pool.json`, one `fcntl` lock. Template ids and prime hashes. |
| `scripts/agents/codex_mesh.py` | `follow` gains `--cwd`, `--sandbox` and `--output-schema` (passed to `turn/start`). `start` gains `--developer-instructions-file`. New `fork --from <seat> --name <new>` and `revert --to <seat> --before-turn <id>`. The server record and roster work from a pool session dir that several units' launchers share, with the lock as today. `sweep` judges a pool server `in use` while its pool marker exists. |
| `scripts/delegate/review.sh` | `--pool <type>`: the six steps above; JSON and turn records beside the text findings. `--to` and fresh paths unchanged. |
| `scripts/agents/agent_exec.sh` | Drop `--ephemeral` for `delegate.review` during the trial, so fresh reviews leave rollouts to measure. |
| `commands/unit/direct.md` | <LongLivedSeats/> and <TeamReview/> seat choice: when the production doc names a review unit, reviews go `--pool code`; <UXReview/> step 3 goes `--pool ui`. <PhaseEnd/> calls `end-lease`. Outside a production, nothing changes. |
| `docs/delegate/dual_review.md` | <DualReview/> step 3: the `--pool` call form. <ReviewPromptContract/>: a pool prompt opens with the lease's role line in place of the seat role switch. Three Gods and Type Design go by pointer, because the standing prompt holds them. |
| `docs/delegate/write_prompt_contract.md` | Items 7 and 9: a pool reviewer's prompt may point to its standing prompt for Type Design and Three Gods. |
| `docs/production_format.md` | Words above; Production Context line `**Review unit:** code, ui` (or `code`); Units row `review-unit` with Owns `—`; <ProductionUnit/> item saying reviews go to the pool. The **Seat** entry's "Reviews still open their own threads" changes. |
| `commands/showrunner/produce.md` | <LaunchUnits/> starts the review unit first when the doc names one. <MergeCheckpoint/> step 6 sends the review unit director one line per merged checkpoint: unit, phase, session dir, design-check verdict. <Wrap/> stops the pool server. |
| `commands/review/direct.md` (new) | The review unit director: prime templates; curate memory after each merge line; health check each hour (server up, stale leases, old prime hashes); give the `seat_usage.py --reviews` report at each dailies. |
| `scripts/whoami/seat_usage.py` | `--reviews`: tokens per review turn (section 7). |
| `scripts/production/add_unit.py` | Accept the `review-unit` row: no worktree, no port; the session runs `/review:direct`. |

## 6. Risks

| Risk | What contains it |
| --- | --- |
| **The cost of standing context per request** (the largest risk) | Prefix tokens are re-billed on every request, so a pool that keeps growing context loses to fresh reviewers without anyone seeing it. Standing prompts are capped (7k code, 6k UI) and leases revert. Section 7's trial ends the pool if the per-review number does not drop. |
| One unit's details in another unit's review | A lease is one unit's. A revert removes the whole lease before the reviewer serves anyone else. Per-unit memory is sent only in that unit's lease. The repository memory holds classes and rulings, never diffs; its writer is the director, who applies that rule. |
| Stale context | The prompt says to re-read the tree each review. A lease never outlives its phase. Memory entries carry a date and source and are pruned when a ruling changes. A changed guide hash retires reviewers at lease end. |
| One point of failure | The request path is the unit's own `review.sh` plus one app-server. A dead server is restarted on the next call, and its threads resume (existing `follow` behavior). If the pool cannot open a thread at all, `review.sh --pool` exits `error`, and the unit director runs today's fresh path, as <DelegateLaunchFailure/> already directs. This applies on failure, not on load. |
| Sandbox reads of every worktree | Codex `readOnly` reads the whole filesystem, so it needs no grant per worktree. It also cannot stop a reviewer reading a sibling unit's tree. All trees are the user's own, so this is a correctness risk, not a privacy one. The prompt names the worktree and head hash. The reviewer's first command is `git -C <worktree> rev-parse HEAD`, and a mismatch reports `error`. |
| Writer independence | An improvement on Phase 3, where a writer can review its own code: a pool reviewer never wrote what it reads. |
| UI reviewer on Codex | Today's design check runs on a Claude helper. Phase D runs the Codex UI reviewer in shadow first and adopts it only if it misses nothing the Claude helper found. |

## 7. Phasing and measurement

**Phase A — probes (one session, no skill text).** On a scratch app-server:
1. Does the first request after `thread/revert` read the standing prompt as
   cached input? Compare with the first request after `thread/fork`. The answer
   decides revert or fork for lease end; the measurement for each is the
   `cached_input_tokens` of that request.
2. Do `developerInstructions` survive `thread/compact/start`?
3. Do per-turn `cwd`, `readOnly` and `outputSchema` work?
4. Can one app-server serve two clients' turns at once?

Also drop `--ephemeral` for reviews, so the baseline starts collecting rollouts.

**Phase B — code pool on build-followups.** `review_pool.py`, the mesh verbs,
`review.sh --pool code`, the `direct.md` and `dual_review.md` text. Trial
assignment: `allocate` sends a phase's reviews to the pool or to the unit's own
seats (Phase 3's `--to`) by a hash of unit and phase. That gives an even mix in
the same window. This is an assignment for comparison, not a fallback.

**Phase C — review unit director, memory, Hana.** `/review:direct`, memory
curation, the production doc, the `produce.md` changes. Roll out to the Hana
production with the same assignment by hash.

**Phase D — UI pool.** Shadow first: for ten design checks, the Codex UI
reviewer judges the same shots as the Claude helper. Its verdict is recorded
but not used. Adopt when it found every defect the helper found.

**Measuring tokens per review.** `seat_usage.py --reviews --since <date>` reads
every `review_turn_*.json` under `/tmp/claude/delegate/` on natedev and the
Mac. It sums the rollout's token counts for that turn id, and prints weighted
tokens per review by arm (fresh, own seat, pool) and kind (broad lens, closure,
ux):
- median and mean;
- the share from cached input, which shows standing-context cost directly;
- for the pool, the lease ordinal, so the warm-closure saving shows apart from
  the startup saving.

Fresh reviews are measured from their rollouts once `--ephemeral` is gone.
`codex exec`'s `tokens used` total does not split cached from uncached input.

**Measuring whether context build-up improves reviews.** Per review, from the
records each unit already keeps:
- **raised:** numbered items in its findings file;
- **confirmed:** ledger rows opened with its lens in that pass;
- **precision:** confirmed ÷ raised;
- **escapes:** defects in the same phase the review should have caught. These
  are rows opened `--caught-by main` only, new defects found by a later closure
  in code the broad review read, merge holds and merge design-check defects
  (`review_regime.py`, regime `pool`), and red CI traced to the phase.

`seat_usage.py --reviews --quality` plots precision and escapes per review
against two measures:
- the memory entry count when the review ran (memory version, stored with the
  turn record);
- the reviewer's review count.

It does the same for the own-seat and fresh arms over the same window.

**Verdict, after 60 pool reviews or 3 weeks, whichever is later:**
- **Keep the pool** when its weighted tokens per review are at most 85% of the
  own-seat arm's, and its escapes per phase are no higher.
- **Keep the memory** when precision or escapes improve with memory version
  (a falling trend across at least four memory versions). Otherwise cut memory
  to the guides and contracts alone. It then costs fewer prefix tokens and buys
  no measured quality.
- **Remove the pool** when it misses the 85% line. Phase 3's own-seat reviews
  stay. The user decides with the report in hand.

The likely sources of a saving, to check against the report:
- a pooled reviewer's first request is cached, saving most of the 19k–26k
  preamble that a fresh reviewer pays uncached, about 18k weighted per review;
- a closure on a warm lease reads less code. Closures are two thirds of reviews.

The likely loss: a pool reviewer is cold on the unit's code, where a Phase 3
own seat is warm. The trial exists to find which effect is larger.

## 8. What the showrunner and directors said

Asked 2026-10-10. All four answered; replies are paraphrased by the coordinator
and attributed by session name.

- **hana** (showrunner): send a request file with "worktree path, base and head
  hashes, files, the Work Order and the guide set". The reviewer writes
  findings.py-schema JSON into a file the unit owns, and "the director keeps
  triage". Keep "the guides, the type-design contract, recurring defect
  classes, per-crate idioms". Forget "each diff, each unit's plan, earlier
  verdicts on a file", and compact between reviews. Worried because
  "checkpoints bunch up after build holds"; wanted a wait limit with a fallback
  to the unit's own seat, and no single pool that can block everyone. UX: a
  fresh helper per checkpoint; a long-lived UI reviewer "may keep the UX rule
  list, but reads each shot fresh".
- **organon** (geometry-material-unit): hand over "base and tip, Work Order
  path, lens list, session dir". The pass goes in organon's progress table
  "with the reviewer's id". Keep "Hana's codebase map, the unit's standing
  rules (never-edit files, ownership, the 9 px floor)" with the style guide and
  type contract. Forget "earlier verdicts on its code, and other units' diffs".
  Fall back to its own seat after about 10 minutes. UX: fresh at each
  checkpoint.
- **widget** (widget-unit): send "base..HEAD plus uncommitted paths", the plan
  heading, the lens and the session dir. Get back "findings.py-ready JSON
  lines" plus one board post. Keep the crate's settled rulings "and its past
  findings with how each closed". Forget "any belief about the tree's current
  state; re-read every time". Warned that "a reviewer warm on another unit is
  cold on this one". Wanted a 15-minute limit and a readable queue position.
  UX: fresh, because "a long-lived eye gets used to the look".
- **trunk** (trunk-unit): the pass shows under slot `review`. Either the review
  unit writes trunk's history, "or sends two lines for trunk to record". Keep
  "crate layout, style and type rulings already made, recurring defect
  classes". Closure reviews "must come first, never behind another unit's
  broad review". Opens its own seat after about 10 minutes of waiting. UX: "a
  fresh verdict on exactly the shots in the notice". A long-lived UI reviewer
  "may hold the guide for repair rounds".

**How the proposal answers them.**
- Queueing, their shared first worry, is gone by the user's rule. Allocation
  creates a reviewer rather than waiting, so wait limits, queue positions and
  closure priority have nothing to act on.
- Findings land in each unit's own files, and triage stays with its director
  (all four).
- The pass is recorded by the unit's own launcher (organon, trunk).
- What to keep became the review memory, and what to forget is removed by the
  lease revert (all four).
- Widget's "warm on another unit is cold on this one" is the reason leases are
  per unit and the trial compares against warm own seats.
- UX verdicts are given from a reverted UI reviewer, which is the fresh eye the
  four asked for. The rule list and rulings persist, as the user asked.
