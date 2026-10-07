# Director model study

## What it is

`scripts/model_study/` is a read-only Python 3.13 standard-library tool that measures unit directors' turns before and after the 2026-10-06 switch from Opus 5.5 xhigh to Sonnet 5.5 xhigh. It answers one question: which model should be the default director model for enh-showrunner. It reads Claude Code transcripts and the run history, writes working files under `~/.local/state/model-study/`, and generates the results in `docs/as-built/director-model-study-results.md` (readable report) and `docs/as-built/director-model-study-results.json` (numbers only). Both results files are generated; the next `results` run replaces a hand edit.

## How it works

The stages pass data through files in the state directory, so each command can run on its own: `extract` -> `compare` and `phases` -> `report` / `results`. Imports are flat (`from compare import ...`); `pyrightconfig.json` has an `executionEnvironments` entry rooted at `scripts/model_study`.

| Module | Holds |
| --- | --- |
| `turns.py` | `RosterEntry`, `Turn`, `Compaction`, `Dropped`, `SessionRead`, `load_roster`, `session_ids`, `director_boundary`, `prompt_kind`, `read_session`, `director_turns`, `recompute_switch_turns` |
| `model_study.py` | the hub: argument parser, `extract` (carry-forward, migration), `atomic_text`, dispatch of every subcommand |
| `stats.py` | `PRICES`, `percentile`, `bootstrap_diff`, `bootstrap_net`, `request_cost`, `label_difference` |
| `compare.py` | filters, arms, classes, baseline, `control`, `net_seconds`, compactions, `compare()`, `markdown()`; the `ComparisonReport` TypedDicts |
| `phases.py` | run-history join, `PhaseRow`, per-metric comparisons, `phases()`, `markdown()` |
| `report.py` | `render`, `message`, `verdict`, `recommendation_line`, `ready`, history (`record_history`, `since_lines`) |
| `results.py` | `render` of the results markdown, `compact_json`, `document_paths`, `MAX_JSON_BYTES`, `METHOD` |
| `roster.json` | the twelve directors: `name`, `session_ids`, `switched_by_pdt`, `production`, `director_from_pdt` |

### Extraction

- `read_session(path, name, director_from) -> SessionRead` groups non-sidechain `assistant` records by `requestId` (the record `uuid` when absent). One API request is one turn. A request runs from its first block to its last block's timestamp; usage takes the largest value per field across its blocks; cache writes split into `write_1h` and `write_5m` (any remainder of `cache_creation_input_tokens` counts as `write_5m`); `stop` is the last block's `stop_reason` when `tool_use` or `end_turn`, else `other`. Sidechain (subagent) records are counted and never measured.
- `seconds` is the last block's timestamp minus the trigger, the nearest earlier non-`assistant` record stamped at or before the first block. Tool and person wait time falls outside it. `seconds` is null, counted `no_trigger`, `negative` or `over_limit` (gap over `OVER_LIMIT_SECONDS = 1800`), and never clipped; the row stays because its tokens are real.
- `prompt_kind` reads the nearest earlier `user` record: a `tool_result` item is `continuation`; `isCompactSummary` is `compact`; string content takes its `turnOrigin` (`human`, `peer`, `task_notification`); anything else is `other`.
- `director_boundary` is the first of: roster `director_from_pdt`; the first `user` record containing `<command-name>/unit:delegate</command-name>`; the first `human` or `peer` prompt containing `/unit:delegate`; else the first request (`director_from` null, `whole_session` true). Earlier requests count `before_director`. A `<synthetic>` reply is not a request: counted `synthetic`, and it never changes the previous model, `after_compact` or the compaction counter.
- `Turn` (frozen; `to_json()` is a `turns.jsonl` row) holds identifiers, ISO UTC times, `seconds`, `model`, `effort` (`perTurnEffort`, else `unknown`), `speed` (`usage.speed`, else `unknown`), `stop`, `kind`, the token counts (`input`, `output`, `thinking`, `cache_read`, `write_5m`, `write_1h`, `context`), `switch_turn` and `after_compact` (1 to 10 for the first ten requests after each `compact_boundary`). `Compaction` rows carry `trigger`, `pre_tokens`, `post_tokens`, `duration_ms` (nullable) and the model of the last request before it. `Dropped` has six counts: `before_director`, `sidechain`, `no_trigger`, `negative`, `over_limit`, `synthetic`.
- `switch_turn` is true for a director's first request and the first request after any model change (the cache rebuild). Only `recompute_switch_turns(rows)` decides it, over all rows of one director sorted by `started`, so carried and live rows combine correctly.
- `extract(roster, projects_dir, registry_dir, state_dir, verbose=True, now=None)` finds each id's transcript by globbing `<projects-dir>/*/<id>.jsonl` (never `<id>/subagents/`). `session_ids(entry, registry_dir)` adds the id that `<registry-dir>/*.json` (`name`, `sessionId`) names now, because a restarted director keeps its name and gets a new id; ids recorded in the previous `extract.json` `session_states` stay joined after the registry stops listing them.
- Carry-forward: an id whose transcript is gone keeps its rows (copied from the previous `turns.jsonl` and `compactions.jsonl`) and its previous `session_states` entry, marked `transcript_gone`; a transcript that exists is always re-read. An `extract.json` without `session_states` migrates on the next run; the residual legacy drop counts go to the first carried id.
- `extract.json` is `{extracted, sessions: [per-director summary], session_states: {id: {director, requests, dropped, transcript_gone, director_from, whole_session}}}`.

### Comparison

- `compare(state_dir) -> ComparisonReport` reads `turns.jsonl` and `compactions.jsonl` only (directors come from the rows; `extract.json` is never read), writes `compare.json` whole, prints nothing and returns the data. `markdown(result)` renders it; the `compare` subcommand prints that.
- Arms are by `message.model`: `OPUS = "claude-opus-5-5"`, `SONNET = "claude-sonnet-5-5"`; any other model counts as `other`. `eligible_turns` drops `switch_turn` requests, then effort other than `xhigh`, then speed other than `standard`, counting each request once per director and arm.
- Director statuses: `switched` (both arms), `control candidate` (Opus only), `Sonnet-only`, `no eligible requests`; the pooled row (`Pooled switched`, switched directors only) sits in `ComparisonReport["pooled"]`.
- Classes: `continuation/tool_use`, `continuation/end_turn`, `prompt`; `continuation` is the first two pooled and is the headline. Per (director, arm, class): `n`, `timed_n`, seconds median/p25/p75/p90, median output, thinking, fresh input (`input + write_5m + write_1h`), cache read and context tokens, median output tokens per second (requests of at least 0.5 s), mean `request_cost`. `prompt` (every other request) reports tokens only.
- Baseline: a switched director's Opus arm is its last `BASELINE_REQUESTS = 300` filtered Opus requests before its first Sonnet request; `opus_all` sits beside it.
- `compare.json` consumers read the status strings exactly as spelled, `counts`, `drops`, `classes.<arm>.<class>`, `differences` (`seconds`, `output`, `cost`, `net_seconds`), `control`, `compactions` and `pooled`; renaming any of them breaks `report.py` and `results.py`. No pooled compaction row exists.
- Differences are Sonnet minus Opus: median continuation `seconds`, median `output`, mean `cost`, each a `DifferenceEstimate {value, low, high, label, opus_n, sonnet_n}` from `bootstrap_diff(a, b, stat, resamples=2000, seed=1)` (95% percentile interval). `label_difference` returns `too few` below 30 requests in either arm, else `faster`/`slower` (seconds) or `lower`/`higher` (tokens, cost) when the interval lies below/above zero, else `no measurable difference`.
- Cost: `request_cost(turn)` at `PRICES` (USD per million tokens, standard speed): Opus input 4, output 20, write 5 min 5, write 1 h 8, read 0.20; Sonnet 2, 10, 2.50, 4, 0.20. Thinking tokens are a subset of output and never added. An unpriced model costs 0.
- Control: a director qualifies only when its status is `control candidate` and its transcripts hold no Sonnet request at all (the effort and speed filters do not rescue it). The boundary T is the median of the switched directors' first Sonnet request times, taken from the rows. Its before side is the continuation requests among its last 300 filtered Opus requests before T; its after side is its filtered Opus continuation requests from T on; each side needs 30 timed requests. `control.directors` lists each (`difference` before to after); `control.pooled` (`PooledControl`) holds `before_n`, `after_n`, `before_median`, `after_median`, `change_seconds` (counts 0 and nulls when no director qualifies).
- `net_seconds`, a `DifferenceEstimate` on each switched director's `differences` and on the pooled row, is `(Sonnet median - Opus baseline median) - (pooled control after - pooled control before)`, from `bootstrap_net(opus, sonnet, control_before, control_after, stat, resamples=2000, seed=1)`, which resamples all four samples at their own sizes from one seeded generator. Labels: `no control`, `too few` (an arm empty or under 30), `faster`, `slower`, `no measurable difference`. The raw `seconds` estimate is unchanged beside it.
- Compactions, per (director, arm) keyed by the model before each compaction: per 100 requests; per active hour (the arm's request span with gaps over 15 minutes removed); median `pre_tokens` and `duration_ms`; `seconds_per_active_hour` (null when any attributed compaction lacks `duration_ms`); and the ratio of mean seconds and of mean cache-write tokens for requests with `after_compact` 1 to 10 against the arm's others.

### Phase table

- `phases(state_dir, runs_dir, roster, registry_dir) -> PhaseReport` writes `phases.json` (`{phases, comparisons, drops}`). It reads `~/.local/state/plan-delegate/runs/*.jsonl`; `run_started.main_agent.session_id` picks the director (ids from `session_states`, then roster, then registry). Each completed phase (`phase_started` paired with a `phase_finished` of status `completed`) takes every request of that director with `started <= at < ended`, unfiltered; `phase_arm` names it `opus`, `sonnet` or `mixed` from the requests' models.
- `PhaseRow` carries `requests`, `director_seconds`, `cost_usd`, `output_tokens`, `repair_rounds` (the highest `round` of the phase's `finding_batch_dispatched` events), `phase_elapsed_seconds`, the recorded size `work_order_words`, and per-1,000-word rates `requests_per_1000_words`, `seconds_per_1000_words`, `cost_per_1000_words`. A size is null when unrecorded or invalid; a recorded 0 stays 0; rates are null for a null or 0 size.
- `comparisons` has one entry per roster director with `switched_by_pdt` set, then `Pooled switched`. Four metrics (the three rates and `repair_rounds`) each map to Sonnet minus Opus medians. With 8 or more phases per arm the interval comes from `bootstrap_diff` and the label is `fewer`, `more` or `no measurable difference`; otherwise `too few phases (n=<a> against <b>)` with null value and bounds. Rate metrics skip rows with null rates, so their n is smaller than the `repair_rounds` n.
- `drops` is `{stopped, errored, empty}`: finish status `stopped`; any status other than `completed` or `stopped`; completed phases with no request of the director.

### Report and verdict

- `report.py` renders from `compare.json`, `phases.json` and `extract.json`. `render(comparison, phases, extracted, now, final=False, history=NO_HISTORY)` writes, in order: the user's measure, "Since the last run" (only when history is recorded), Verdict, per-director table, pooled turn classes, control, compactions, work per phase, Limits. The title says `INTERIM` or `FINAL` with the PDT time.
- The user's measure is one line per switched director plus `Pooled switched`: median continuation seconds Opus A -> Sonnet B with the raw percent (from the two `continuation` medians, never `differences.seconds.value`), then `net of the clock <±x.xx> s (<±y.y%> of the Opus median), 95% interval [low, high] s — <label>`, or `net of the clock n/a (...)`.
- `verdict(comparison, phases)` reads four pooled labels: `net_seconds`, `cost`, phase `requests_per_1000_words` and `repair_rounds`. Rule: `opus` when net time reads `slower` or requests or repair rounds read `more`; else `sonnet` when cost reads `lower` or net time reads `faster`; else `sonnet stays (no measurable difference; evidence thin)`. `too few`, `too few phases` and `no control` are no evidence. Raw seconds, output tokens and compactions (the pooled rate, `compaction_rate`, weighted by active hours) are printed in the verdict block and do not enter the rule. `recommendation_line` repeats the rule as a sentence; the block's last line begins `Default for enh-showrunner` and ends `opus` or `sonnet`.
- `message(...)` is the short message: first line `From model-study-unit: <INTERIM|FINAL> director model study — <verdict>.`, then the measure lines, the pooled "Since the last run" line, the verdict block, the director table, a Limits line and the report path. It stays at 60 lines or fewer: director table rows are cut first, then per-director measure lines (the pooled line stays), ending `… N more directors in report.md`.
- `ready(comparison, now=None) -> (bool, lines)`: the gate holds when at least 4 `switched` directors have `classes.sonnet.continuation.n >= 150`, or `now >= SAMPLE_DEADLINE_PDT` (2026-10-07 10:30 PDT).
- History: each `report` and `results` run writes one `HistoryRun` line to `history.jsonl`: `{at (UTC ISO), mode, recommendation, pooled, directors[]}` where each measure holds the Sonnet continuation n, the two medians, `change_percent` and `net_change_percent` (net seconds over the Opus median; null when not netted; older lines without the key read as not netted). Types: `NotRecorded`, `FirstRun(current)`, `FollowingRun(current, previous)`; `record_history(path, current, save=True)` compares with the latest line whose `at` differs.
- `results.py`: `render` writes `Sample as of <PDT>`, Question, Data, Method (`METHOD`), the user's measure, per-director comparison, Verdict, Sample gate (from `ready`), Learning (one line per measure; the time line gives net label, raw and net values), Limits, Since the last run, How to re-run. `compact_json` writes `{compare, phases, history}` with `separators=(",", ":")`, `sort_keys=True`, the latest 20 history lines, no per-request rows.

### Commands

`python3 scripts/model_study/model_study.py <subcommand>`. Path flags default as follows: `--state-dir` is `MODEL_STUDY_STATE`, else `~/.local/state/model-study`; `--projects-dir` `~/.claude/projects`; `--registry-dir` `~/.claude/sessions`; `--runs-dir` `~/.local/state/plan-delegate/runs`; `--roster` `scripts/model_study/roster.json`. `--now <ISO time>` (a naive time reads as PDT) replaces the wall clock everywhere: `extracted`, the history `at`, the report title, the deadline test.

| Subcommand | Flags | Does |
| --- | --- | --- |
| `extract` | `--state-dir --projects-dir --registry-dir --roster --now` | writes `turns.jsonl`, `compactions.jsonl`, `extract.json`; prints one table row per roster name (requests by model and effort, first Sonnet PDT beside `switched_by_pdt`, drop counts, `carried N`) |
| `compare` | `--state-dir` | writes `compare.json`; prints the comparison markdown |
| `phases` | `--state-dir --runs-dir --registry-dir --roster` | writes `phases.json`; prints the phase markdown |
| `report` | `--interim` (default) or `--final`, `--message`, `--no-extract`, `--no-history`, `--now`, and the five path flags | extract (unless `--no-extract`, which needs an existing `extract.json`), compare, phases, history line, then `report.md`; prints the path, or the short message with `--message` |
| `results` | `--message`, `--no-extract`, `--no-history`, `--now`, `--docs-dir` (default `docs/as-built` of the repository holding the tool), and the five path flags | one final run, then the two results documents; exits 4 and writes neither when the compact JSON reaches `MAX_JSON_BYTES = 300_000` (`history.jsonl` and `report.md` are already written) |
| `ready` | `--now`, `--state-dir --projects-dir --registry-dir --roster` | runs extract and compare, prints the deadline, each switched director's filtered Sonnet continuation count and `G1: ready\|waiting (...)`; exits 0 when the gate holds, else 3 |

State directory files: `turns.jsonl`, `compactions.jsonl`, `extract.json`, `compare.json`, `phases.json`, `report.md`, `history.jsonl`. Every output is written whole through a temporary file and `os.replace`.

## Invariants

- Transcripts, run history and the registry of running agents (`~/.claude/sessions`) are read only. Writes go only to the state directory and, for `results`, the docs directory.
- Both arms use effort `xhigh` and speed `standard`; the model is the only difference. The first request after a model change is dropped from every statistic and counted.
- The transcript's `message.model` fixes each switch. `switched_by_pdt` is a "by" time (the first Sonnet request can precede it) and never sets the arms or the control boundary T.
- Baseline = the last 300 filtered Opus requests before the first Sonnet request. The Opus arm is a fixed window, so a re-run moves only the Sonnet arm and what depends on it.
- Floors: 30 requests per arm for any `faster`/`slower`/`lower`/`higher` label; 30 timed continuation requests on each side of T for a control; 8 phases per arm for a phase label; a smaller arm reads `too few` or `too few phases`.
- Control membership: no Sonnet request in the transcripts at all; one pooled control serves every row, with one boundary T.
- Determinism: one input and one `--now` give byte-identical output files from every command, `history.jsonl` included. The bootstrap is seeded; history lines are keyed by `at`.
- Privacy: no prompt text, tool argument, tool result or file path from a transcript reaches an output file or the screen; rows hold numbers and allowlisted fields. Tests assert it (`test_turns.py`, `test_rerun.py`).
- Recommendation: harm evidence (net time `slower`, or requests per 1,000 words or repair rounds `more`) gives `opus`; else `lower` cost per request or `faster` net time gives `sonnet`; else `sonnet stays`.
- Gate G1: at least 4 switched directors with 150 filtered Sonnet continuation requests each, or `SAMPLE_DEADLINE_PDT`, whichever comes first; `ready` exits 0 or 3. It is advisory: no command refuses to run before it holds.
- Every time in an output is PDT (`America/Los_Angeles`); transcripts and run history are UTC. Costs are API-equivalent at `PRICES`, not subscription weights.
- Python is typed throughout with no `Any` and no file-level type ignores; every JSON record is a `TypedDict` or dataclass. Unavailable values are `None` and JSON null (rendered `—`), not named variants.
- Tests build synthetic transcripts in temporary directories; they never read a real transcript or write the real state directory.
- The study covers natedev's directors only.

## Calibration and gotchas

- The constants are literals in several places. 30: `stats.label_difference` and the control floor in `compare.py`. 150 and 4: `report.ready` and again in the clause logic of `results.render`. 300: `BASELINE_REQUESTS`, plus the text of `METHOD` and `report.limits`. 2,000 and seed 1: the bootstrap defaults plus `METHOD`. Also literal: the 900 s active-hour gap, the 0.5 s output-rate floor, 60 message lines, 20 history lines in the `.json`. A change edits every spot and its text.
- The baseline window rule is written three times in `compare.py`: `director_result`, the pooled baseline in `compare`, and the per-director input to `net_seconds`. Change all three together. The control's before side is a fourth, windowed at T; without the 300-request window it holds weeks of drift.
- The recommendation rule is written twice, in `verdict` and `recommendation_line`.
- A same-`--now` repeat replaces its own history line (same `at`); a different `--now`, or the wall clock, appends one. `history.jsonl` keeps every line; the `.json` keeps 20.
- `--no-extract` makes `report` and `results` reuse the extract outputs already in the state directory. Live transcripts grow between runs, so a repeat is byte-identical only with `--no-extract`; on fixtures both are. `ready` always extracts.
- `build-report`'s Opus arm is 7 requests: its net label is `too few`, but the raw percent line prints (+253.3%) without a label and is not evidence.
- The sample keeps moving while directors work: counts, medians and labels change between runs. The Sample gate section and `ready` agree only at one run's instant; "Since the last run" shows what moved.
- Phase "switched" comes from the roster's `switched_by_pdt`; the comparison status `switched` comes from the rows. They can differ, so a director can have a table row and no work line, or the reverse.
- Some phase starts record no size, so their rates are null; a phase that starts and never finishes, or whose interval is unreadable, appears in no count.
- `trunk` and `organon` drop many requests as `before_director` because their start is found through `/unit:delegate` in a human prompt.
- `results` at 300,000 JSON bytes exits 4; compact encoding keeps `compare` plus `phases` near 150 KB (default indentation is about 207 KB). Any line added to `message` counts toward its 60.
- A legacy migration attributes residual drop counts to the first carried id; with several carried ids the split is an attribution, not a measurement. `extract` never reads `<id>/subagents/*.jsonl`.
- `basedpyright scripts/model_study` exits 3 in every checkout; judge it by the last line, `0 errors, 0 warnings, 0 notes`.

## Why

- Continuation turns are the headline because the user's measure is turn time for what the director does, not what it waits on. A request-to-reply span excludes tool and person run time; a continuation turn starts at a tool result, so no person is in the wait. A prompt turn's stamp can precede the model's start (queued prompts), so prompt turns carry token counts only.
- Effort is filtered because a `/model` switch resets effort to medium; with effort and speed held, the model is the only variable. The first request after a change rebuilds the cache and is excluded for the same reason.
- The raw before-and-after credited Sonnet with the evening clock: directors that never switched also got faster over the same hours (pooled raw -15.2% for the switched directors, -13.6% for the control). The difference-in-differences nets that change out, so a time verdict reflects the model. One pooled control serves every row because a single director's concurrent Opus window holds too few requests for a stable control. A control must be Opus throughout, so any Sonnet request, even one the filters drop, excludes the director.
- Cost per request decides the recommendation when time is a draw. Sonnet's price columns are lower, and the tool prices measured tokens rather than assuming identical tokens, since higher output could offset the price; with no evidence of harm on time, turns or repair rounds, a lower measured cost per request keeps Sonnet. The rule is mechanical and printed with its inputs so each run's answer traces to labels.
- More turns or more repair rounds for the same work are harm even when each turn is fast, so work per phase is normalized by recorded task size (`work_order_words`, per 1,000 words) and compared per director and pooled.
- Reruns rebuild everything whole from live files, with a fixed Opus window, carried rows for deleted transcripts and a history line per run, because the sample grows after the first answer and a later run must show what moved.
- Ruled out: reading `extract.json` in `compare` (not an input, goes stale); a pooled compaction row in `compare.json` (derived from the switched directors' rows); every earlier Opus request as the control's before side (measures drift); netting each director against its own concurrent Opus window (too few requests); filtering phase requests by effort, speed or `switch_turn`; counting started-but-unfinished phases; a length cap on `report.md` (only `message` is capped); named variants for unavailable values in place of `None`; changing the cost and compaction lines (they do not depend on the clock); joining ledger holds (a unit's phase ids repeat across its documents); measuring the Mac; editing `config/agents.conf` (enh-showrunner owns the director model setting and takes this result as its default).

## How to re-run

- `python3 scripts/model_study/model_study.py results` extracts the live transcripts, compares, joins the phase table, adds a history line, and replaces `docs/as-built/director-model-study-results.md` and `.json` plus `~/.local/state/model-study/report.md`. Add `--message` for the short message, `--no-extract` to reuse extracted rows, `--now 2026-10-07T10:30` to pin the clock, `--no-history` to skip the history line.
- `python3 scripts/model_study/model_study.py report --interim` writes only `report.md` and a history line; `ready` prints the gate state and exits 0 or 3.
- `python3 -m unittest discover -s scripts/model_study -p 'test_*.py'` from the repository root runs the tests (`test_turns`, `test_stats`, `test_compare`, `test_phases`, `test_report`, `test_rerun`); it prints a pass count and touches no real transcript or state.
- `basedpyright scripts/model_study` type-checks the tool; read its closing counts, not its exit status.

## Result at the time of writing

- Sample of 2026-10-06 20:44 PDT: pooled median continuation seconds Opus 5.41, Sonnet 4.59, raw -15.2%; the never-switched control moved -13.6% over the same hours.
- Net of the clock -0.11 s with 95% interval [-0.53, 0.46] s, so time reads no measurable difference.
- Recommendation `sonnet`, because cost per request is lower.
- Later runs supersede this; the generated `docs/as-built/director-model-study-results.md` holds the current numbers.
