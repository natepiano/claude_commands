Sample as of 2026-10-06 20:44 PDT

## Question

Do Sonnet directors keep their turn time, tokens and work per phase against Opus?

## Data

Roster: 12 directors; 12 with measured requests.
Filters: xhigh, standard speed; 23197 extract drops; Opus baseline last 300 filtered requests per director.
Counts: 1575 pooled Opus continuation requests; 2138 pooled Sonnet continuation requests.

## Method

- One request is one director-thread turn; native subagents are excluded.
- The headline is the continuation turn triggered by a tool result; prompt turns contribute tokens only.
- Both arms use xhigh effort and standard speed; filter drops are counted.
- The transcript model fixes each switch; the first request after a model change is excluded.
- The Opus baseline is the last 300 filtered requests before Sonnet; concurrent Opus directors form the clock control.
- Median differences use 2,000 bootstrap resamples at seed 1; an arm below 30 requests reads too few.
- Harm on time, turns or repair rounds recommends Opus; otherwise Sonnet stays.
- G1 holds at four switched directors with 150 filtered Sonnet continuation requests each, or at the deadline.
- Each run rebuilds its outputs; missing transcripts retain measured rows and reports record what moved.
- The scope is natedev; ledger holds and subscription weights are outside this measure.

## The user's measure

screenshot: median continuation-turn seconds Opus 6.54 → Sonnet 5.28 (-19.3%); net of the clock -0.55 s (-8.3% of the Opus median), 95% interval [-1.40, 0.37] s — no measurable difference
widget: median continuation-turn seconds Opus 4.48 → Sonnet 3.53 (-21.2%); net of the clock -0.23 s (-5.2% of the Opus median), 95% interval [-0.79, 0.57] s — no measurable difference
hook: median continuation-turn seconds Opus 4.46 → Sonnet 4.20 (-5.7%); net of the clock +0.46 s (+10.3% of the Opus median), 95% interval [-0.38, 1.16] s — no measurable difference
enh-showrunner: median continuation-turn seconds Opus 6.80 → Sonnet 5.48 (-19.4%); net of the clock -0.61 s (-8.9% of the Opus median), 95% interval [-1.52, 0.30] s — no measurable difference
cache-evict: median continuation-turn seconds Opus 6.58 → Sonnet 3.27 (-50.2%); net of the clock -2.59 s (-39.4% of the Opus median), 95% interval [-3.54, -1.47] s — faster
mul_add: median continuation-turn seconds Opus 4.37 → Sonnet 3.93 (-10.1%); net of the clock +0.27 s (+6.3% of the Opus median), 95% interval [-0.61, 1.18] s — no measurable difference
build-report: median continuation-turn seconds Opus 1.49 → Sonnet 5.28 (+253.3%); net of the clock +4.50 s (+301.2% of the Opus median), 95% interval [1.54, 6.09] s — too few
Pooled switched: median continuation-turn seconds Opus 5.41 → Sonnet 4.59 (-15.2%); net of the clock -0.11 s (-2.0% of the Opus median), 95% interval [-0.53, 0.46] s — no measurable difference

## Per-director comparison

| Director | Opus n | Sonnet n | First Sonnet request PDT | Continuation median s O → S | Raw Δ seconds and label | Output median O → S | Mean cost USD O → S | Repair rounds median O → S |
| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |
| screenshot | 272 | 505 | 2026-10-06 17:00 PDT | 6.54 → 5.28 | -1.26 (faster) | 437.00 → 548.00 | 0.07138 → 0.06043 | 2.00 → 1.00 |
| widget | 300 | 290 | 2026-10-06 17:01 PDT | 4.48 → 3.53 | -0.95 (faster) | 425.00 → 462.00 | 0.07584 → 0.06578 | 1.00 → — |
| hook | 300 | 334 | 2026-10-06 17:17 PDT | 4.46 → 4.20 | -0.26 (no measurable difference) | 404.00 → 458.00 | 0.06984 → 0.05542 | 2.00 → 0.00 |
| enh-showrunner | 300 | 713 | 2026-10-06 17:22 PDT | 6.80 → 5.48 | -1.32 (faster) | 446.00 → 514.50 | 0.07268 → 0.06220 | 1.00 → 0.00 |
| cache-evict | 271 | 185 | 2026-10-06 17:18 PDT | 6.58 → 3.27 | -3.30 (faster) | 391.00 → 429.50 | 0.07481 → 0.05341 | 1.00 → 0.00 |
| mul_add | 300 | 255 | 2026-10-06 17:35 PDT | 4.37 → 3.93 | -0.44 (no measurable difference) | 456.00 → 557.00 | 0.07204 → 0.06081 | 1.00 → 1.00 |
| build-report | 7 | 102 | 2026-10-06 17:01 PDT | 1.49 → 5.28 | 3.79 (too few) | 284.00 → 545.00 | 0.05917 → 0.05223 | — → — |
| model-study | 0 | 729 | 2026-10-06 17:28 PDT | — → 3.45 | — | — → 493.00 | — → 0.06062 | — → — |
| startup | 300 | 0 | — | 4.22 → — | — | 448.00 → — | 0.07046 → — | — → — |
| fps | 300 | 0 | — | 6.28 → — | — | 396.50 → — | 0.06685 → — | — → — |
| trunk | 300 | 0 | — | 4.58 → — | — | 444.00 → — | 0.06925 → — | — → — |
| organon | 300 | 0 | — | 3.83 → — | — | 361.00 → — | 0.07060 → — | — → — |

## Verdict

time: pooled continuation seconds no measurable difference net of the clock (raw faster)
tokens: output tokens higher; cost per request lower
work: requests per 1,000 Work Order words no measurable difference; repair rounds no measurable difference
compaction: Sonnet / Opus 1.63× (Opus 115.32 s/active h; Sonnet 187.57 s/active h)
Recommendation: sonnet — cost per request is lower.
Default for enh-showrunner Phase 6: sonnet

## Sample gate

Deadline: 2026-10-07 10:30 PDT
screenshot: 457 filtered Sonnet continuation requests
widget: 225 filtered Sonnet continuation requests
hook: 299 filtered Sonnet continuation requests
enh-showrunner: 672 filtered Sonnet continuation requests
cache-evict: 170 filtered Sonnet continuation requests
mul_add: 222 filtered Sonnet continuation requests
build-report: 93 filtered Sonnet continuation requests
G1: ready (6 switched directors at 150; deadline pending)
G1 holds: yes; clause: four directors at 150.

## Learning

Time: median continuation seconds no measurable difference net of the clock (Opus 5.41; Sonnet 4.59; raw -15.2%; net -0.11 s, interval [-0.53, 0.46]).
Tokens and cost: median output tokens per request higher (Opus 426.00; Sonnet 509.50); mean cost in dollars per request lower (Opus 0.07270; Sonnet 0.06005).
Turns and repair rounds: requests per 1,000 words no measurable difference (Opus 64.85; Sonnet 43.16); repair rounds per phase no measurable difference (Opus 1.00; Sonnet 0.00).
Compactions: seconds per active hour, Opus 115.32; Sonnet 187.57.
Control: startup no measurable difference (before 4.35; after 4.26); fps no measurable difference (before 6.76; after 6.39); trunk no measurable difference (before 4.96; after 4.34); organon faster (before 4.88; after 4.01).

## Limits

Requests are filtered xhigh, standard-speed requests; Opus uses each director's last 300 before Sonnet. Sample sizes and drops follow.
screenshot: Opus all 272, baseline 272, Sonnet 505, other-model 0; status switched.
screenshot extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 0.
screenshot opus filter drops: switch turn 1, effort 0, speed 0.
screenshot sonnet filter drops: switch turn 1, effort 0, speed 0.
screenshot other filter drops: switch turn 0, effort 0, speed 0.
widget: Opus all 14973, baseline 300, Sonnet 290, other-model 0; status switched.
widget extract drops: before_director 3803, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 9.
widget opus filter drops: switch turn 1, effort 0, speed 0.
widget sonnet filter drops: switch turn 1, effort 10, speed 0.
widget other filter drops: switch turn 0, effort 0, speed 0.
hook: Opus all 1520, baseline 300, Sonnet 334, other-model 0; status switched.
hook extract drops: before_director 4070, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 5.
hook opus filter drops: switch turn 1, effort 0, speed 0.
hook sonnet filter drops: switch turn 1, effort 7, speed 0.
hook other filter drops: switch turn 0, effort 0, speed 0.
enh-showrunner: Opus all 446, baseline 300, Sonnet 713, other-model 0; status switched.
enh-showrunner extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 0.
enh-showrunner opus filter drops: switch turn 1, effort 0, speed 0.
enh-showrunner sonnet filter drops: switch turn 1, effort 0, speed 0.
enh-showrunner other filter drops: switch turn 0, effort 0, speed 0.
cache-evict: Opus all 271, baseline 271, Sonnet 185, other-model 0; status switched.
cache-evict extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 0.
cache-evict opus filter drops: switch turn 1, effort 0, speed 0.
cache-evict sonnet filter drops: switch turn 1, effort 1, speed 0.
cache-evict other filter drops: switch turn 0, effort 0, speed 0.
mul_add: Opus all 754, baseline 300, Sonnet 255, other-model 0; status switched.
mul_add extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 4.
mul_add opus filter drops: switch turn 1, effort 0, speed 0.
mul_add sonnet filter drops: switch turn 1, effort 0, speed 0.
mul_add other filter drops: switch turn 0, effort 0, speed 0.
build-report: Opus all 7, baseline 7, Sonnet 102, other-model 0; status switched.
build-report extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 0.
build-report opus filter drops: switch turn 1, effort 0, speed 0.
build-report sonnet filter drops: switch turn 1, effort 1, speed 0.
build-report other filter drops: switch turn 0, effort 0, speed 0.
model-study: Opus all 0, baseline 0, Sonnet 729, other-model 0; status Sonnet-only.
model-study extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 0.
model-study opus filter drops: switch turn 0, effort 0, speed 0.
model-study sonnet filter drops: switch turn 1, effort 0, speed 0.
model-study other filter drops: switch turn 0, effort 0, speed 0.
model-study is Sonnet-only; there is no Opus baseline.
startup: Opus all 8590, baseline 300, Sonnet 0, other-model 0; status control candidate.
startup extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 3.
startup opus filter drops: switch turn 1, effort 0, speed 1.
startup sonnet filter drops: switch turn 0, effort 0, speed 0.
startup other filter drops: switch turn 0, effort 0, speed 0.
fps: Opus all 7046, baseline 300, Sonnet 0, other-model 0; status control candidate.
fps extract drops: before_director 0, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 6.
fps opus filter drops: switch turn 1, effort 0, speed 1.
fps sonnet filter drops: switch turn 0, effort 0, speed 0.
fps other filter drops: switch turn 0, effort 0, speed 0.
trunk: Opus all 11748, baseline 300, Sonnet 0, other-model 0; status control candidate.
trunk extract drops: before_director 8123, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 10.
trunk opus filter drops: switch turn 1, effort 0, speed 0.
trunk sonnet filter drops: switch turn 0, effort 0, speed 0.
trunk other filter drops: switch turn 0, effort 0, speed 0.
organon: Opus all 14155, baseline 300, Sonnet 0, other-model 0; status control candidate.
organon extract drops: before_director 7150, sidechain 0, no_trigger 0, negative 0, over_limit 0, synthetic 14.
organon opus filter drops: switch turn 1, effort 0, speed 2.
organon sonnet filter drops: switch turn 0, effort 0, speed 0.
organon other filter drops: switch turn 0, effort 0, speed 0.
Pooled work comparison: 56 Opus phases and 11 Sonnet phases for repair rounds; 44 and 10 with recorded Work Order size for per-1,000-word rates.
Phase drops: stopped 5, errored 2, no request 75.
A phase with no recorded Work Order size has no per-1,000-word rate (—).
Clock control boundary 2026-10-06 17:17 PDT: 4 director(s) qualified; 1029 continuation requests before and 1104 after.
The study covers natedev only.
Costs are API-equivalent estimates, not subscription weights.
Holds and merge defects in the review ledger are not joined; decision quality beyond repair rounds is not measured.

## Since the last run

Previous run: 2026-10-06 20:18 PDT (final).
screenshot: Sonnet continuation n 388 → 457; median change -17.02% → -19.27%.
widget: Sonnet continuation n 202 → 225; median change -20.10% → -21.17%.
hook: Sonnet continuation n 299 → 299; median change -5.73% → -5.73%.
enh-showrunner: Sonnet continuation n 562 → 672; median change -15.27% → -19.45%.
cache-evict: Sonnet continuation n 170 → 170; median change -50.24% → -50.24%.
mul_add: Sonnet continuation n 221 → 222; median change -10.44% → -10.06%.
build-report: Sonnet continuation n 93 → 93; median change 253.35% → 253.35%.
model-study: Sonnet continuation n 558 → 657; median change —% → —%.
startup: Sonnet continuation n 0 → 0; median change —% → —%.
fps: Sonnet continuation n 0 → 0; median change —% → —%.
trunk: Sonnet continuation n 0 → 0; median change —% → —%.
organon: Sonnet continuation n 0 → 0; median change —% → —%.
Pooled switched: median change then -12.89%, now -15.16%.
recommendation unchanged

## How to re-run

Run `python3 scripts/model_study/model_study.py results`.
Per-request rows live in `~/.local/state/model-study/turns.jsonl`, not in git.
