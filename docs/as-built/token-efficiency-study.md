# Token-efficiency study and the efficiency count

## What it is

The status line's `For you:` row shows how many efficiency proposals wait on the user, as "N efficiency" in its own color. The number comes from the nightly-review ledger at `~/.local/state/nightly-review/ledger.md`. A one-week token-efficiency study (2026-10-12..19, both machines, Claude and Codex) feeds that ledger. The study changed nothing in real work. It mapped where weighted tokens go, priced the fixed cost of every request, measured wasted turn patterns and ran offline replays. It then added twelve ranked proposals to the ledger. Its scripts, data and reports are in `~/.local/state/nightly-review/token-study/`, outside git. The ledger backlog is worked only on demand, with the user, through `/next efficiency`. A proposal is built only after the user approves it.

## How it works

### The For you count (`scripts/production/footer_counts.py`)

- `FooterCounts(changes, proposals, questions, efficiency)` is a NamedTuple, and every field defaults to 0.
- `proposals(doc)` counts `proposals_table.waiting_entries(doc)`, the same own-production rows as `/next proposal`: open proposal issues waiting for approval and proposed plan-backlog lines. Accepted and deferred plan-backlog lines do not count.
- A production doc names the ledger in a `- **Efficiency ledger:** <path>` line (field `EFFICIENCY_LEDGER_FIELD`). `efficiency_ledger(doc)` reads it through `production_field`. A missing field gives `None`. `docs/production_format.md` documents the field, and `build-followups-production.md` sets it.
- `efficiency(doc)` counts the lines that match `EFFICIENCY_LINE`, `^- \d{4}-\d\d-\d\d .*? — (?P<status>.*)$`, when the status starts with `proposed` or `deferred` (`open_efficiency`). The status is the text after the **first** ` — `. A later ` — ` belongs to a `from Also found:` sub-item and does not count. An undated line does not count.
- An absent field, a missing file, a directory, or a file that is not UTF-8 counts 0. The other counts stay as they are.
- `read_counts(doc, *, questions)` fills all four counts. `total(each)` adds them by column across productions.
- `sources(doc)` includes the Efficiency ledger and Improvement queue ledger paths even before the files exist. This makes the status line cache refresh when either ledger appears or changes.
- `counts_line` puts the counts in this order: questions, changes, efficiency, proposals, and leaves out zero counts: `For you: 1 question, 2 changes, 130 efficiency, 2 proposals`. "efficiency" has no plural. Every kind can be shown through `/next`.
- `scripts/hooks/showrunner_footer.py`, `scripts/statusline/counts_row.py` and the dailies footer (`dailies_render.py`) pass `FooterCounts` through without change.
- Tests: `scripts/production/test_footer_counts.py` covers the parser, missing and non-UTF-8 ledgers, sources and render order. `scripts/statusline/test_statusline.py` covers the count end to end in the status line.

### The study layout (`~/.local/state/nightly-review/token-study/`)

- `2-map/`: where the tokens went in the week, by role, model, production, unit, machine, day and token class. It also holds the work yardsticks: checkpoints from git history, findings closed and review rounds. Scripts are in `2-map/claude/work/` (`cells.jsonl`) and `2-map/codex/work/` (`cube.csv`, `git_checkpoints.py`).
- `3-fixed/`: what every request carries before it does any work, per role. Examples are tool schemas, the Artifact schema, the memory index, CLAUDE.md, reminders, and the Codex base instructions and skill catalog. Scripts: `3-fixed/claude/work/` (`transcript_scan.py`, `measure.py`, `analysis.json`) and `3-fixed/codex/work/` (`scan_rollouts.py`, `check_parser.py`).
- `4-turns/`: turn patterns that spend tokens without moving work forward. Examples are wait-only turns, repeated reads, the first request after compaction, skill rereads and notification-only turns. The patterns do not overlap. `scan_patterns.py` and `check_patterns.py` are in each half's `work/`, with a manifest per half.
- `5-experiments/`: five offline replays, one row each with before/after numbers, a weekly projection with a range, a same-work check and the experiment's own cost. The halves are in `claude/claude.md` and `codex/codex.md`.
- `experiments/<name>/`: the setup, results and verification files for each experiment (for example `codex-blocking-waits/results.json`).
- `report.md`: the final report. It has the token map summary, the ranked proposals (change, where and owner, gain with share and range, gain kind, evidence, risk, how to measure again), the experiment results and what the study did not cover.

How gains are labelled:
- **Modeled.** A replay projection. Its range always starts at 0. Only rank 1 has one.
- **Not measurable.** No replay can model the change. The row gives an exposure instead.
- **Exposure.** The tokens the pattern carries today. It is an upper bound and never a gain. Rows that are not measurable are ordered by exposure, and rows with no sized exposure go last.

## Invariants

- Ledger lines are never deleted. An outcome changes the status text on the line's own line.
- Status forms: `proposed`, `deferred`, `accepted <date>, applied (<repo> <sha>)`, `declined`, `done <date>`, `relayed <date>`. A line replaced by a newer proposal reads `superseded by <date> <new title>`. A line whose finding no longer holds after a new measurement reads `gone on re-measure <date>`. Use the gone form only on a like-for-like measurement.
- The gain tag format is `[gain/week: <gain>]`, placed just before ` — <status>`. `candidates.py` reads the tag with `\[gain/week: …\]` and ranks by its leading number: minutes or hours first, then `%` of tokens, then unmeasured. A tag such as `not measurable, up to X% of tokens` starts with no number, so it ranks as unmeasured. Rank 1 reads `0.51983% of tokens, modeled`.
- The For you count equals the number of dated ledger lines whose first status starts with `proposed` or `deferred`. Each new `proposed` line adds one.
- A study run changes nothing in real work. That means no edit to `~/.claude`, settings, skills, commands, CLAUDE.md, memory, hooks or other repos. Reads of transcripts, rollouts and the usage store are free. One `collect.py` pass to bring the store current is allowed.
- Experiments run only in a scratch config home or scratch worktree under `token-study/experiments/`. Each records its own token cost. The budget for all experiments together is 3M weighted tokens.
- Every number names the query or script that made it, its window and its machine coverage. Gains are stated per week, as a share and in weighted tokens, with a low–high range.

## Calibration and gotchas

- The week total is 7,240,515,495.25 weighted tokens: Claude 56.103%, Codex 43.897%. Cache reads are 59.995% of all cost. The largest roles are the Claude director (29.3%), Codex fix (19.5%) and Codex impl (14.6%).
- Yardsticks: 706 checkpoints at 10,255,687.67 each, 2,114 findings closed at 3,425,030.98 each, and 1,136 review passes at 6,373,693.22 each. Per checkpoint, build-followups costs 4.44M and tool-based-ui 24.48M. Nothing explains that 5.51x gap.
- The named Claude fixed parts total 11.39%. The Artifact schema alone is 3.49%, with 5 calls in the week. The Codex retained start blocks total 3.46%. The start residual that the named parts do not explain is 15–30% for Claude and 21–28% for Codex.
- The turn patterns that do not overlap total 2,499,756,881.23 (34.52%). Codex wait-only turns are 12.05% of that total.
- The rank-1 modeled gain is 37,638,356.80/week (0.51983%; 0–37,638,356.80). It comes from collapsing 4,626 wait-run requests to 1,566. The largest exposure is fewer wait turns per fix/impl job, at 749,850,925.14 (10.36%). It overlaps rank 1, so do not add the two.
- No experiment ran live. The observed saving is 0, and the experiments cost 0 model tokens.
- No tokenizer exists on natedev. Text sizes are characters/4. Tool JSON is 0.20 tokens per character. The Artifact schema uses a fitted 10,500 tokens (10,200–10,900).
- `scripts/nightly_review/candidates.py` rewrites `~/rust/hanadocs/nightly improvement list.md` on every run, also when it only prints. `NIGHTLY_REVIEW_NOTE` changes that path. Point the variable at a scratch file before a read-only run.
- Two open-line rules are in use. `candidates.py` treats a line as open when it *ends* `— proposed` or `— deferred`. `footer_counts` reads the *first* status. The two can disagree on a line that has `from Also found:` sub-items.
- The usage store holds only `.claude` checkpoints. Take checkpoints from each production repository's git history.
- Codex logs record no cache writes. A Claude cache write and Codex uncached input are the same kind of cost.
- A Codex thread resumed in a new rollout file must keep the retention its compactions decided. Restoring all start parts overstated cost by 1,272,628 a week.
- A Codex tool output enters input on the request after the call that produced it. A resumed thread replays outputs, so key them by call id and content.
- `fix_pass` is 0 on every review pass. Review rounds cannot be counted from the store, so review-loop cost is only an association.
- The 2026-10-16 no-poll worker rule falls inside the window, and no report splits waits by day. Check whether the polls still happen before you build rank 1.
- Mac coverage is thin (0.007%). Nobody knows if that means low use or missing data.

## Why

- **The director's `direct.md` re-Read after compaction stays.** The reattached copy holds only 260/707 and 236/710 of the required lines. A tail-only Read is safe only with a hash check that the reattached copy matches the file. Without that check, an older copy loses changed lines. Rank 5 proposes the guarded form.
- **Per-checkpoint cost is a description only.** Checkpoints differ in size and kind across productions. `weighted_per_checkpoint` is not a same-work check. A same-work check needs matched tasks or unchanged acceptance outcomes.
- **Exposures are not gains.** A carried cost is not a saving until a change removes it and a measurement shows the removal. The work may also need part of what it carries. An exposure therefore only bounds a gain from above, and its range starts at 0.
- **Gains are "modeled", never "measured".** No live run took place. A replay cannot show timing, cancellation or the next action a worker takes.
- **Codex review compaction limits stay as they are.** No modeled limit beats the observed cost. The Work Order and latest review instruction were available at 0 boundaries under every limit tested.
- **The update-tick line stays open.** The new figure (0.0651%) is a weighted share. The old 24% claim was a share of raw input. A weighted-versus-raw comparison cannot mark a line gone.
- **Older lines are superseded, not deleted.** The ledger is the one record. A superseded line points to its replacement, so the history and the For you count both stay correct.
