# Repair rounds: evidence study

The study covers the **15 phases with the most repair rounds** among the 1,867 phases started in the 60 days to 2026-10-20 (mixed records removed): **260 repair rounds and 271 hours of fix time**. These figures describe those 15 phases, not all phases. In them, failed fixes account for **36% of rounds and 49% of time**; specification gaps account for **17% and 19%**; mechanical work accounts for **15% and 11%**. These rounded figures come from [Totals](#totals); the three mixed records are excluded. Medians and phase/time shares in the round report use reviewed phases only; unreviewed phases remain in its rows and totals.

## Method and selection

Measured on 2026-10-20. The frozen selection command was:

```sh
python3 scripts/history/history.py rounds --since 60d --status all --format json --top 40
```

The historical all-phase JSON summary at selection was: 1,867 phases counted; 222 phases with at least 3 rounds (11.9%) held 79.2% of fix time; 2,255 rounds and 7,587,361 agent-seconds of fixes. Causes were review-ux 18.9%, review-code 61.6%, gate 7.3%, merge 1.9%, live 0.2%, other 10.1%; 39 records were mixed. The selected 15 non-mixed records originally contained **255 rounds**; Phase 3's corrected dispatch join gives this fixed cohort **260 rounds**, still **975,069 agent-seconds**. The three selected mixed records now contain another **139 rounds**, analyzed separately below. Active records can grow while this study runs; a later full-window total is identified separately. The repair verification on 2026-10-20 at 18:28 EDT ran the same command and confirmed the same 15 records, 255 rounds and 975,069 seconds. Its live full-window metadata had grown to 1,868 phases / 2,260 rounds / 7,589,160 seconds; these earlier figures describe the pre-repair join. The corrected full-window capture below is dated 18:51 EDT.

A round is the API's overlapping fix-worker interval, with a matched ledger dispatch counted once. Each dispatch joins only the group containing the first start-ordered fix pass with its number that starts at or after that dispatch. A later matching pass inside an already open group qualifies; a group start before the dispatch alone does not disqualify it. Each dispatch is used once, and an untaken dispatch remains its own zero-time ledger-only round, including repeated ledger numbers. Separate intervals can share one ledger batch number. A ledger-only dispatch has zero measured time. An unfinished start has no measured end and never extends an interval. Time is summed agent time, including concurrent workers; it is not elapsed wall time. All section round numbers below are API interval numbers, not the ledger's dispatch numbers. Each section identifies the run and phase instance, so a finding ID is local to that section.

The 60-day filter selects phases by their recorded start boundary. It then counts the recorded rounds of each selected phase; it does not clip every pass to a 60-day wall-clock interval. Selection sorts by round count, then summed fix time, and removes mixed records before taking 15. The full-window cause table includes mixed and zero-round phase records. Its time allocation sums each API round's `fix_seconds` under its cause.

The Phase 1 cause describes the immediate trigger. The root label is a hand assessment from the cited activity, findings and boundary Work Order. The labels are `spec-gap`, `target-moved`, `out-of-scope`, `fix-failed`, `fix-regressed`, `serial-check`, `mechanical` and `record`. One primary root receives the interval's entire measured time; a multi-worker interval can contain more than one kind of work. These allocations estimate the time associated with each root, not the time spent on each individual defect. A missing acceptance case is marked as an inference when the Work Order alone cannot prove its causal role. New findings after a live check do not by themselves prove a serial-check violation.

Cause priority matters: a matched ledger dispatch is `review-ux` or `review-code` before activity words are considered; then come gate, merge, live and other. Thus a live visual check followed by ledger repair usually counts as review, not live. The low `live` share measures the residual cause category, not the frequency of live checks. Phase 3's temporal join removes the stale reused-number associations that previously hid immediate triggers, notably in rank 3 and mixed record 3.

Every round row names its recorded pass activity or, for a ledger-only interval, its dispatch and covered findings. The Covered field is the corrected API batch association: — means no matched batch. Other finding IDs in the evidence describe independently dated observations or continuity of the work, not an additional API match. The finding catalogs give the recorded titles. Work Orders are read from git before the start where available; the rollout's rebased history and phase 58's late saved order have explicit limitations. `check=ok` is a heuristic result, not proof that all recorded work is a repair or belongs to one task.

| Root | Meaning used in this study |
| --- | --- |
| spec-gap | The start Work Order omitted a required target, visible goal, named test or acceptance case. |
| target-moved | The user or showrunner changed the goal after the phase started. |
| out-of-scope | The defect predated the phase or belonged to another unit; item 7 routes it elsewhere. |
| fix-failed | The same finding remained open after the preceding repair. |
| fix-regressed | A repair introduced the defect repaired next. Direct regression reports are stronger evidence than sequence alone. |
| serial-check | a live check sent one defect, it was fixed, then the next was found (item 7 "one batch per check") |
| mechanical | lint, test update or merge work that follows from a change and needs no judgment |
| record | The interval was not a repair, or its recorded boundary did not identify a completed repair. |

A stated overall goal does not make its correction mechanical. A spec-gap assessment below identifies a particular missing target or test case in the cited start order, not merely a failure to implement the goal. These assessments are inferences about why the repair was needed; they do not prove that adding that test would have prevented it. Several regression attributions also rely on repair sequence and finding reports rather than a source-level replay. Pure verification intervals are record rows, rather than test updates. A check that sends several findings is not serial-check merely because another check follows it.

## Ranked non-mixed records

### 1. Effect views frame the picture and its effect, and no step view cuts a tool

Plan: `info-panels-plan.md`. Run `f205ddd0-5865-4dec-9b85-849e4ffa3dd8`, phase `33b`, instance `deb83eb2-d3f2-485a-a162-8799a8e9c205`. **28 rounds; 20 findings; 33,912 fix seconds (9h25m); 25 rounds past landing.** Start: 2026-10-18T22:41:04.927+00:00.

Start Work Order: `8362bb48d112ba4ac08514c46649a2d18f94f378:docs/hana/info-panels-plan.md:894`, in `/home/natepiano/rust/tool-based-ui-demo`. It specified a 9 px label floor, picture area of at least 15%, both cables in the subject, whole-or-out tools and a named refusal when impossible. The [attempts log](/home/natepiano/.claude/projects/-home-natepiano-rust-hana/memory/attempts-p33b-effect-readability.md) entries 1–17 records the successive rulings and measurements. Entries below refer to that log. This is a changed target and an unsuccessful-repair loop, despite the explicit initial visual measures.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/f205ddd0-5865-4dec-9b85-849e4ffa3dd8.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 561 | Covered: F001, F002, F003, F004. “fixing effect placement under a parent, effect-view tool clearance and cable settling”; “writing regressions for placement, effect-view clearance and cable settling”. Restore parent-relative Transform writes, tool clearance and settling under the stated framing contract (F001–F004); no changed visual target is recorded. The cited start order names effect placement and its fake-BRP tests, but no placement test under a Selection parent (F001). That missing parent/local-world case is the primary gap assessment; the whole batch also corrects explicit whole-tool and settling requirements. |
| 2 | other | fix-regressed | 270 | Covered: —. “keeping the Hidden-cable rule in the new route settle wait”. The route-settle repair had to preserve the existing Hidden-cable rule. |
| 3 | gate | mechanical | 522 | Covered: —. “finishing round 1 regressions, then the final gates”. Finish the regression suite and gates for the same first batch. |
| 4 | review-ux | fix-failed | 1680 | Covered: F002, F005, F006, F007. “fixing effect and step framing”; “writing framing regressions”; “finishing framing regressions and gates”. F002 stayed open after batch 1; the next live check also added F005–F007. |
| 5 | other | fix-failed | 309 | Covered: —. “making effect labels readable in the final view”. The final wider view still made the effect labels too small (attempts 2/2c). |
| 6 | gate | record | 258 | Covered: —. “running final script gates”. Run the gates for the existing framing repair. The activity records gate execution only, with no test update or source repair. |
| 7 | review-ux | target-moved | 632 | Covered: F002, F007. “removing cables from the effect subject”; “writing effect subject regressions”. Showrunner removed both cables from the framed subject (attempts entry 4). |
| 8 | review-ux | target-moved | 643 | Covered: F002, F007. “adding the second effect row”; “writing second-row regressions”. Showrunner added a second effect row and accepted a 9 px planning bar (entry 5). |
| 9 | review-ux | fix-failed | 723 | Covered: F002, F007. “placing effects in the nearest free grid place”; “writing effect grid regressions”. A fixed two-row layout still failed the sixth effect; replace it with a general grid. |
| 10 | gate | mechanical | 152 | Covered: —. “removing the eight-effect test and running gates”. Remove the obsolete eight-effect assertion and run gates. |
| 11 | review-ux | fix-regressed | 836 | Covered: F002, F005, F007. “diagnosing and fixing the Show-step view search”; “writing the Show-step regression, then gates”. F005 reopened after earlier acceptance; radius-only Show search still cut tools (entry 6). |
| 12 | gate | record | 118 | Covered: —. “running the three gates on the frozen script”. Run the frozen-script gates. The activity records gate execution only, with no test update or source repair. |
| 13 | review-ux | fix-failed | 1277 | Covered: F002, F008, F009. “limiting the Show orbit and making the effect planner use the live framing's tools”; “writing regression tests from the live run 5 scene, then the gates”. Planner and live scene used different tool sets; F002 persisted (entries 7–8). |
| 14 | review-code | fix-failed | 390 | Covered: F002, F009. “diagnosing each place's binding rule, then growing the effect grid upward”; “updating grid tests for the upward layout, then the gates”. Swirl still refused every grid place after the prior repair; attempts entry 8 diagnoses the expanding frame and tries an upward grid. That entry does not name a new showrunner ruling. |
| 15 | review-ux | target-moved | 1553 | Covered: F002, F009, F010. “narrowing the whole-or-out set and framing the Show tool band”; “adding the run-6 scene and Show floor tests, then the gates”. Whole-or-out was narrowed to picture, Show and current effect (entry 10). |
| 16 | review-ux | target-moved | 1513 | Covered: F002, F009, F010. “finishing the Show view under the one rule, then the freeze”; “fixing the run-6 fixture and Show test, then the gates”. Showrunner applied the same narrower rule to Show itself (entry 10). |
| 17 | review-ux | fix-failed | 1927 | Covered: F002, F009, F010. “diagnosing the Show refusals and the Swirl text size, then the freeze”; “checking the fake against live run 6, then the gates”; “running the round 11 gates on the frozen script”. Settled labels and planning measures still disagreed; F002/F009/F010 remained open (entry 11). |
| 18 | review-ux | target-moved | 2412 | Covered: F002, F009, F010. “making effect steps continue with the least-broken view, then the freeze”; “updating the run-6 chain test to best-effort, then the gates”. Fallback B allowed best-effort effect views with warnings (entry 12). |
| 19 | review-ux | fix-regressed | 702 | Covered: F002, F009, F010. “framing the turned picture by its own corners, then the freeze”; “fixing stale yaw-0 checks, then the gates”. Turning the picture broke the world-box framing; use oriented corners (entry 12). |
| 20 | gate | record | 1251 | Covered: —. “running the round 13 gates on the frozen script”. Run round-13 gates. The activity records gate execution only, with no test update or source repair. |
| 21 | review-ux | mechanical | 888 | Covered: F002, F009, F010. “fixing the last two tests, then the gates”. Correct the final two stale fixtures and run gates (entry 12). |
| 22 | review-code | fix-regressed | 1230 | Covered: F011, F012, F013. “profiling the Show search, then centring and facing the picture”; “writing regression tests for the three live-run regressions”. Live run 7 found three new regressions: 232 s Show search, off-centre rise and wrong picture facing (entry 13). |
| 23 | review-code | fix-failed | 3619 | Covered: F011, F012, F013. “finishing the Show, centring and facing repairs, then the freeze”. Complete the still-open Show, centring and facing repairs (entry 14). |
| 24 | review-code | fix-regressed | 2068 | Covered: F014. “cutting the effect placement search time”; “adapting the fake and writing the placement-time test”. The new fallback placement took 341 s for Vignette instead of seconds (entry 14). |
| 25 | gate | record | 326 | Covered: —. “running the three gates on the frozen script”. Run gates on the frozen placement-time repair. The activity records gate execution only, with no test update or source repair. |
| 26 | review-code | fix-failed | 2771 | Covered: F015. “sorting and fixing the round 17 test failures”; “sorting stale tests and running the gates”; “updating the stale tests and running the gates”. The frozen-script gates in R25 did not clear F015: 13 failures, 2 errors and 27 warnings (attempts entry 15). R26 continues that failed gate batch, including judgment about stale-by-ruling fixtures versus source defects; the whole interval is not a judgment-free test update. |
| 27 | review-ux | target-moved | 2820 | Covered: F016, F017, F018, F019. “round 19 script repair”; “round 19 tests”; “round 19 tests and gates”. Temporary scale cap 6.0 and warning for sub-9 px labels were ruled while cables fixed the long-lead defect (entries 15–16). |
| 28 | review-ux | fix-failed | 2461 | Covered: F019, F020. “speed up effect search, keep views face-on”; “tests for live-size search time and face-on Swirl”; “check and fix two test measures”. Hue placement remained slow after the prior repair; padded live geometry reproduced hashing cost and Swirl was still off-axis (entry 17). |

Finding titles (the IDs cited above):

- **F001:** Effect placement writes a world pose into a local Transform, ignoring the Selection parent
- **F002:** Effect subject views do not keep other tools whole or out
- **F003:** Effect cable bounds are read before the routes settle
- **F004:** picture_framing_moves hides extra framing requests after arrival
- **F005:** Show step cuts Show and Material at the right edge
- **F006:** Material view crowds the top edge
- **F007:** Effect view shows the cube side without controls
- **F008:** Show step orbits to a steep 45-degree look-down
- **F009:** Swirl lands on the floor in front of Hue rotate's base
- **F010:** Show view is 60% empty floor
- **F011:** Show step takes 231.84 s in the script's own framing search (run 6: 9.2 s)
- **F012:** Into the room puts the picture at the left edge (margins 8/920 px)
- **F013:** Picture does not face the camera: 73 deg off at Show, slanted trapezoid at every yaw-0 effect step
- **F014:** Effect placement search too slow: Vignette 340.77 s, Swirl 21.78 s on the run-6 chain (run 7 placed them in seconds)
- **F015:** Round 17 gates red: 13 failures + 2 errors in effects_demo_test.py, 27 basedpyright warnings in effects_demo.py
- **F016:** Effect scale too large: Swirl placed at scale 9.62, long cable leads never seat (run 8 stops at Swirl)
- **F017:** Show search took 18.8 s live in run 8; ruling says near run 6's 9 s
- **F018:** Posterize grid not up and right of the picture (+2.47/-0.42, bottom level with the picture bottom)
- **F019:** Hue Rotate spawn-to-landing 9.7 s live (run 7: 0.8 s); step 21.3 s
- **F020:** Swirl view is not face-on: picture facing +23.9 deg

### 2. No cell stands empty: an ended command leaves at once, and a drawn cell shows its rows

Plan: `remove-running-section-tile-fixes.md`. Run `03f9b80e-dbe5-47c6-b29e-a7e6587ab721`, phase `22`, instance `ce6ea89f-a674-4477-b86e-7a6b77d4ef0a`. **27 rounds; 46 findings; 201,508 fix seconds (55h58m); 24 rounds past landing.** Start: 2026-10-18T00:43:19.459+00:00.

Start Work Order: `240ceef064057b1fb86b3ceb2311c04ce8416144:docs/cargo-port/remove-running-section-tile-fixes.md:668`, in `/home/natepiano/rust/cargo-liner-tile`. The 4,706-word order named queue limits, live-only queued identities, first-draw row ink, owner lifetime and red-first tests. The [production log](/home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-log.md:2950), especially lines 2950, 2969, 3059, 3115, 3304, 3333, 3373, 3449 and 3468, records later rulings and retained defects. Repeated failures occurred under a detailed visible and timing contract; the record does not establish an absent overall goal.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/03f9b80e-dbe5-47c6-b29e-a7e6587ab721.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 1450 | Covered: F001, F002. “repair round 1”. Update the ended-invocation expectation and correct first-draw account coverage. Start items 5–6 name first-content-row tests, but no multi-account cargo-handler first-draw fixture; F002 exposes that missing caller case. F001 is a test update in the same interval, so this is a whole-batch gap allocation, not a claim that its expectation change needed design work. |
| 2 | review-code | spec-gap | 4189 | Covered: F003, F004, F005, F006, F007, F008, F009, F010, F011, F012, F013, F014, F015, F016. “repair round 2”. F003–F016 correct drawable ownership, lifecycle clocks, queue bookkeeping, test coverage and style against the explicit start contract; a missing overall goal is not shown. Start item 9 names enter/leave timing tests at configured durations, but no test for a second ending while another cell is already fading. F004 is that missing interaction case; F005–F008 also concern interacting lifecycle clocks. Other findings in this interval directly correct stated ownership and drawing contracts. |
| 3 | review-code | fix-failed | 1132 | Covered: F003, F009, F010. “repair round 3”. F003/F009/F010 remained open after the preceding batch. |
| 4 | other | fix-failed | 2373 | Covered: —. “repair round 3”. Continuation of the same unresolved batch-3 findings. |
| 5 | review-ux | fix-regressed | 8417 | Covered: F017, F018, F019, F020, F021. “repair round 4”. F017 explicitly records waiting commands never placed; F018–F021 expose lifecycle breakage. |
| 6 | review-ux | fix-failed | 2710 | Covered: F017, F020, F022. “repair round 5”. F017/F020 were still open; the fade also ended early (F022). |
| 7 | other | fix-failed | 641 | Covered: —. “repair round 5”. Second separated worker interval on batch 5. |
| 8 | review-ux | record | 0 | Covered: F023, F024. Ledger dispatch 7; no matched worker activity. Ledger-only dispatch 7 (F023/F024), with no observed worker interval; retain its zero measured seconds. |
| 9 | review-ux | fix-failed | 2406 | Covered: F017, F020. “repair round 6”. The existing free-row and foot-only failures F017/F020 persisted. |
| 10 | other | fix-failed | 3840 | Covered: —. “repair round 6”. Continuation of the same batch-6 repair. |
| 11 | review-ux | fix-failed | 5099 | Covered: F017, F025, F026, F027. “repair round 7”. F017 remained open and the next capture exposed live-cell erasure F025 and ended-cell growth F027. |
| 12 | review-ux | fix-failed | 6634 | Covered: F017, F025, F026, F027. “repair round 7”. The same batch remained open across another worker interval. |
| 13 | review-ux | fix-failed | 3249 | Covered: F017, F025, F026, F027. “repair round 7”. F025/F027 still remained open after several attempts. |
| 14 | review-ux | fix-failed | 67 | Covered: F025, F027. “repair round 8”. Continue F025/F027 under the next ledger number. |
| 15 | review-ux | fix-failed | 5032 | Covered: F025, F027. “repair round 8b”. The activity names round 8b; the same column/fade findings persisted. |
| 16 | review-ux | fix-failed | 385 | Covered: F025, F027. “repair round 8c”. The activity names round 8c; the same findings persisted again. |
| 17 | review-ux | fix-failed | 2891 | Covered: F025, F027, F028, F029. “repair round 9”. F025/F027 persisted; also viewport panic F028 and lost regression assertions F029. |
| 18 | review-ux | fix-regressed | 4048 | Covered: F025, F027, F030, F031, F032. “repair round 10”. F030 explicitly identifies a round-8 pid-handover regression; F031/F032 add layout and ancestry regressions. |
| 19 | review-ux | fix-failed | 19367 | Covered: F025, F027, F030, F031, F032, F033. “repair round 11”. F025/F027 and the round-8 regression F030 remained in the batch. The later log reiterates the fade clock already specified by start item 9 and adds an ended-before-placement ruling (2950/2969); it does not establish that the whole batch was a changed goal. |
| 20 | review-ux | fix-regressed | 5053 | Covered: F025, F027, F030, F031, F034. “repair round 12”. F034 records early-close/zero-row remnants after the prior fade repair; F030 remained open. |
| 21 | review-ux | fix-failed | 26430 | Covered: F025, F030, F035, F036, F037, F038. “repair round 13”. F025/F030 persisted; the showrunner retained the admission/layout/flicker repairs (log 3115). |
| 22 | review-ux | fix-failed | 24525 | Covered: F025, F030, F035, F036, F037, F038. “repair round 13 (continued)”. Continuation of the same ledger-18 batch, with F025/F030/F036/F037 still open. |
| 23 | review-ux | fix-failed | 36149 | Covered: F030, F036, F037, F039, F040, F041. “repair round 14”. F030/F036/F037 remained open; arrival hit targets and ink clocks F039–F041 were added. |
| 24 | review-ux | fix-regressed | 7691 | Covered: F030, F036, F040, F041, F042, F043. “repair round 15”. F036 and F041 reopened after acceptance; F042 also exposes dropping live cells on failed fit. |
| 25 | review-ux | fix-failed | 14072 | Covered: F030, F036, F037, F042, F044. “per-candidate measurement, startup wave cause, restored owner, frozen ended rows”. F030/F036/F042 stayed open; reopened F037 and a new blank ended row F044 were included. |
| 26 | review-ux | fix-failed | 13658 | Covered: F030, F036, F042, F045, F046. “missing state owner, publication age, elapsed smoothing, full-height packing”. F030/F036 remained open and F042 reopened; log 3449 retains ownership, publication-age and elapsed smoothing work. |
| 27 | other | record | 0 | Covered: —. “missing state owner, publication age, elapsed smoothing, full-height packing”. An unfinished worker start is retained by the API as a zero-time dispatch, not proof of another completed repair. |

Finding titles (the IDs cited above):

- **F001:** reused_pid_scan_delivery_keeps_both_invocations expects the ended invocation tiled
- **F002:** cargo-handler accounts grid loses codex2 on its first draw after drawable admission
- **F003:** A placed cell does not own its drawable proof
- **F004:** A second ending cuts a still-fading cell off mid-departure
- **F005:** A mixed entry and departure runs the departure at 720 ms
- **F006:** A resize lets a resident cell keep a slot below its first drawable allocation (blank body)
- **F007:** A live cell closed for reordering runs on the fade clock
- **F008:** A finished fade starts a fresh 720 ms motion through hidden withdrawal
- **F009:** admitted() checks a different arrangement than sync places
- **F010:** Hidden withdrawal leaves queued summary depths stale
- **F011:** Every frame renders command contents twice
- **F012:** DrawableCell keeps a bare Option title
- **F013:** a_drawn_cell_draws_every_row_it_counts proves one row, not every counted row
- **F014:** Saturation cap runs after shortfalls are served and can push the last cell below the floor
- **F015:** Style review violations (rules 4, 5, 6, 10, 14, 19)
- **F016:** Small cleanups and rewritten-test accounting
- **F017:** Regression: waiting commands never get a cell while free rows stand empty
- **F018:** Ended cells do not shrink while their text fades (item 9)
- **F019:** A taller leaving cell loses its process row mid-fade and leaves a header-only remnant with a counting foot
- **F020:** A cell drew only its foot for 2.4 s under the user's config (case B remains)
- **F021:** Cells admitted after their command ended; a cell tracked a short-lived first process
- **F022:** Ended cells vanish within 0.6 s at full text contrast instead of fading over 3 s
- **F023:** Regression: every cell, live ones included, is erased in one frame ~0.3-0.6 s after the first end in a backlog
- **F024:** Regression: one command drawn twice, the copy without a foot, in settled frames
- **F025:** a column fit erases live cells: d80 13.41 s the whole right column (5 cells, 2 live) is erased in one frame after an ended cell grew 6->12 rows and squeezed the columns
- **F026:** a counted cell draws a blank body: t40 4.24-7.09 s one cell reads content rows 5 then 3 with every body row blank
- **F027:** ended cells grow after their end and are erased early in the fade: t80 p22k06 4->20 rows, d80 p22k10 4->9, t80 p22k13 erased +1.29 s at contrast 13.65
- **F028:** viewport shrink below required minima panics: divider clamp min > max
- **F029:** coverage gaps after round 8: F024 timing truncated, tall-summary departure assert removed, shared check accepts heading-only arrival
- **F030:** shim-to-cargo pid handover draws the command in two cells for one frame (round 8 regression)
- **F031:** one-frame jump to a one-column arrangement drops a live cell with no viewport change
- **F032:** live cell ancestry row goes and comes back (flicker)
- **F033:** release_retargeted_rows sums spans in u16
- **F034:** ended cell closes ahead of its text fade; zero-row sliver after close
- **F035:** live box with zero body rows drawn 0.27-0.58 s
- **F036:** waiting command not placed while free rows fit its minimum
- **F037:** one-frame layout jump with no viewport change
- **F038:** live cell loses a drawn ancestry row and gets it back
- **F039:** Painted free-admission arrivals have no mouse target and no Tab or arrow stop until their queued step starts
- **F040:** A free-admission arrival paints at full ink before its entry clock starts, then fades in from zero
- **F041:** An ended cell that moves restarts its text at blank, fades in, snaps to full ink, then fades out
- **F042:** A failed fit of the retained column assignment can falsely authorize dropping live cells into the summary
- **F043:** A command placed after its end gets a cell that stays blank about 3 s (rule e)
- **F044:** An ended cell grows a blank row under its ancestry row after the end
- **F045:** A restored live invocation continues its old departure and loses its cell at the old fade deadline
- **F046:** Faster scan cadence doubles CPU smoothing speed

### 3. Live pilot under cargo-liner's showrunner

Plan: `build-followups-style.md`. Run `24e20a41-688b-4fff-9f5d-d243a63ee4e2`, phase `17`, instance `de3af83c-e105-4b61-85e0-ea7731204584`. **25 rounds; 10 findings; 6,812 fix seconds (1h53m); 22 rounds past landing.** Start: 2026-10-17T12:25:51.982+00:00.

Start Work Order: `90d4024dfe7d00f99f0d99c3bd7adb00a745f89f:docs/plans/build-followups-style.md:544`, in `/home/natepiano/worktrees/claude-build-followups-style`. It explicitly allowed pilot fixes, required recall of at least 95% on 20 files per family, and measured holds, handoffs and restart behavior. Evidence for the lettered fixes is the [pilot report](/home/natepiano/worktrees/claude-build-followups-style/docs/plans/build-followups-style-pilot.md:313). This broad pilot bundled many distinct defects into one phase. From R5 onward, repeated `fix_pass=1` formerly linked F001/F002 to unrelated lettered fixes. Those stale associations are removed; the activity now determines the gate/other cause, while the pilot report still supports the hand roots.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/24e20a41-688b-4fff-9f5d-d243a63ee4e2.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 644 | Covered: F001, F002. “repairing the bootstrap-retry tip sync”; “writing the retry and reconcile regression tests”. Bootstrap/reconcile rules did not specify that a retry must align already-started workers with the refreshed ledger (pilot Fix A). |
| 2 | review-code | fix-regressed | 1292 | Covered: F004, F005. “repairing the merge-time wake races”; “writing regression tests for the wake races”; “fixing local tier A and E rules”. Fix C added waking waiters; review found two races in that new mechanism (pilot lines 330–342). |
| 3 | review-code | fix-regressed | 672 | Covered: F006. “adding ticket yield”; “writing yield tests”. Fix D made incomplete tickets refuse, but yielded files then could never close (F006). |
| 4 | review-code | fix-regressed | 0 | Covered: F008. Ledger dispatch 4; no matched worker activity. Ledger-only dispatch: the newly added read-window parser missed an attribute before mod (F008). |
| 5 | other | spec-gap | 166 | Covered: —. “fix S: accessors and one-value targets”. Fix S spells out accessors and one-value-per-target after recall misses (pilot 545–553). |
| 6 | other | fix-failed | 73 | Covered: —. “fix T: macros, variants and shadowing”. Fix T addresses recall still below the 95% bar after S; macros/variants/shadowing were missing (567–574). |
| 7 | other | spec-gap | 475 | Covered: —. “keying structure file cells by module”. Fix U corrects structure-cell identity from file to module, an uncovered pilot case (609+). |
| 8 | gate | mechanical | 204 | Covered: —. “updating the planner's pending-pairs test”. Update the pending-pairs assertion after the planner change. |
| 9 | gate | spec-gap | 128 | Covered: —. “writing fix V's ticket text and tests”. Fix V restricts rename targets and accessor interpretation after false positives (598+). |
| 10 | other | spec-gap | 576 | Covered: —. “fix W: single-letter parameters, array elements”. Fix W defines single-letter parameters and array elements, not stated in the ticket (635+). |
| 11 | gate | fix-failed | 59 | Covered: —. “adding the conversion-parameter sentence and its pinned test”. The conversion-parameter miss survived prior ticket wording (Fix X, 655+). |
| 12 | gate | spec-gap | 65 | Covered: —. “adding the two-rule sentence and its pinned test”. Fix Y adds judging a site under every rule it breaks (684+). |
| 13 | gate | spec-gap | 381 | Covered: —. “splitting name-bindings out of the names read ticket, with tests”. Fix Z splits name-bindings into its own read ticket after recall losses (692+). |
| 14 | other | target-moved | 166 | Covered: —. “two judges on the names read ticket”. Fix AA adds two blind judges to the names ticket; this changes the checking mechanism (715+). |
| 15 | gate | spec-gap | 116 | Covered: —. “moving listed-site lookups out of the batch loop, with a test”. Fix AB moves the listed-site reads out of the batch loop (731+). Pilot Fix AB (lines 731–739) measures 220 seconds in 1,318 listed-site reads and introduces a read-count test. The cited pilot order has a recall target but no listed-site read-count or queue-build cost target; this performance acceptance gap is the primary assessment, not a test-only change. |
| 16 | other | spec-gap | 99 | Covered: —. “adding the when-to-split step to modules structure tickets”. Fix AC asks when-to-split for each file, an omitted ticket instruction (740+). |
| 17 | other | spec-gap | 130 | Covered: —. “adding the keep-reasons line”. Fix AD adds keeping every stated reason in comments (754+). |
| 18 | other | fix-failed | 216 | Covered: —. “adding the split-alone sentence”. Fix AE narrows the split instruction after the prior fix bundled four defects (760+). |
| 19 | other | spec-gap | 317 | Covered: —. “adding the held-cell skip”. Fix AF specifies skipping unchanged held cells with open proposals (766+). |
| 20 | other | fix-regressed | 191 | Covered: —. “moving the held-judgment query”. Move the held-judgment query out of repeated selection work. This follows R19/Fix AF, which added the held-cell skip and its held-judgment lookup (pilot lines 766–774); R20 moves that new query out of repeated selection work. The preceding fix is the source of the new repeated-query path; the activity identifies a code repair, not a test update. |
| 21 | other | spec-gap | 240 | Covered: —. “phase 17 fix AG: checkpoint re-stamps own family's verdicts”. Fix AG stamps the current family’s own changed files, a lifecycle case missing earlier (775+). |
| 22 | other | spec-gap | 188 | Covered: —. “writing fix AH”. Fix AH supplies proposed rankings and a root-crate title (783–789). |
| 23 | other | fix-failed | 85 | Covered: —. “repairing fix AH root label”. The same Fix AH still needed its root label corrected; the corrected join removes the previously stale F004/F005 association. |
| 24 | other | fix-regressed | 213 | Covered: —. “writing fix AI”. Fix AI corrects local-main reads that incorporated an unpushed failing parent merge (790–799). |
| 25 | other | fix-failed | 116 | Covered: —. “fix AJ: style proposals read the crate layout once”. The R22/Fix AH ranking backfill is still uncompleted after R23 corrects its root title and R24 fixes a merge-reference issue. Pilot lines 800–811 say repeated 3.7-second layout reads would make that same backfill take about four hours; AJ finishes it in a 19-second dry run. This is substantive continuation of that uncompleted backfill, not a test-only update or proof that R24 caused the slowdown. |

Finding titles (the IDs cited above):

- **F001:** Bootstrap retry leaves workers behind the refreshed ledger
- **F002:** Pilot report marks the queue row pass on unverifiable evidence
- **F003:** fix C: one director row with an empty address fails every wake
- **F004:** merge can miss a waiter being registered
- **F005:** an older wake can erase a newer wait
- **F006:** fix D: a yielded file leaves its cells unjudged, so the gate and checkpoint refuse every ticket that yields
- **F007:** fix K helper breaks the file's naming and line-joining idiom
- **F008:** fix O: a test module with an attribute between cfg(test) and mod gets no window cut
- **F009:** read_windows re-tokenizes the file for every cfg(test) candidate
- **F010:** fix P: a virtual workspace manifest reads as a binary-only crate

### 4. Rollout on both machines: readers, shims, runner services

Plan: `github-runners-next.md`. Run `3c2be97d-180e-425d-a7fe-ba18822feee4`, phase `17`, instance `be3df4fc-2ed3-4005-a57f-23e41cf06d09`. **21 rounds; 7 findings; 32,303 fix seconds (8h58m); 18 rounds past landing.** Start: 2026-09-13T13:20:55.912+00:00.

Recovered Work Order: `4701858b:docs/cargo-tile/github-runners-next.md:559`, in `/home/natepiano/rust/cargo-liner`; the phase-completion diff at `4f255ff9` removes that order. The original `/home/natepiano/rust/cargo-tile-runner` worktree is absent. Available commits were rebased to September 14, after the September 13 start, so the predecessor order is boundary evidence rather than a proven timestamp-exact copy. It specified readers, shims, runner restarts, two-host proof, installation suppression and pre-existing Mac test repairs. Most intervals still cover F001, the broad unperformed-rollout finding. Planned rollout and approval work inflate the repair count.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/3c2be97d-180e-425d-a7fe-ba18822feee4.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | record | 4358 | Covered: F001, F002, F003. “repair round 1: suppression complete on both machines (03 and 04 succeeded); repair seat told and expected to write the Linux upgrade script next; Mac reader upgrade held”. The rollout itself had not been performed (F001); planned deployment was labelled repair. |
| 2 | review-code | record | 858 | Covered: F001. “repair round 2: rewriting the Linux reader upgrade onto release builds”. Rewrite the Linux upgrade script for release builds; F001 is a broad rollout gate. F001 says the planned rollout has not been performed; the recovered order already requires this Linux upgrade script. This is planned rollout preparation under a repair label, not identified lint, test update or merge repair. |
| 3 | review-code | record | 1217 | Covered: F001. “restart step held on a runner configuration change; seats finishing records”. Runner configuration still held the planned restart; no new defect named in activity. |
| 4 | review-code | fix-regressed | 1353 | Covered: F001. “repair round 4: step 06 proof repaired after test found a crash; test and review rechecking”. A test found a crash in the step-06 proof script. |
| 5 | review-code | record | 1478 | Covered: F001. “Step 07 cleared; handed to the user”. Cleared deployment step 07 was handed to the user, not another source repair. |
| 6 | review-code | record | 853 | Covered: F001. “Step 08 cleared; handed to the user”. Cleared deployment step 08 was handed to the user. |
| 7 | review-code | record | 1189 | Covered: F001. “Step 09 draft repaired; awaiting user rebuild”. Repair the step-09 draft before the user rebuild. The recovered order already requires this rollout draft and the user rebuild. No separate defect or changed target is named beyond the broad F001 rollout gate; do not count script preparation as mechanical repair. |
| 8 | review-code | record | 1097 | Covered: F001. “recording natedev rollout completion”. Record Linux rollout completion. |
| 9 | review-code | out-of-scope | 1887 | Covered: F001. “final clean native Mac gates on the test repairs”; “repairing the macOS census and capture failures in cargo-tile source”. Native Mac census/capture failures were pre-existing preflight defects, explicitly carried by the rollout Work Order. |
| 10 | review-code | record | 1868 | Covered: F001. “recording the green Mac test results”. Record the green Mac test result. |
| 11 | review-code | record | 1228 | Covered: F001. “Mac reader upgrade handed to the user”. Hand the planned Mac reader upgrade to the user. |
| 12 | review-code | record | 941 | Covered: F001. “clearing the Mac runner restart script”. Clear the Mac restart script. The recovered order requires the runner restart script; this activity clears that planned deployment step without identifying a separate repair. |
| 13 | review-code | record | 1528 | Covered: F001. “recording the accepted runner restart”. Record the accepted runner restart. |
| 14 | review-code | record | 957 | Covered: F001. “second approval of the Mac capture-shim install script”. Second approval of the capture-shim installation script is not a new repair by itself. |
| 15 | review-code | record | 799 | Covered: F001. “writing and clearing the Mac all-account status script”. Write/check the planned all-account status script. The recovered order requires the all-account proof. Writing and clearing its observation script is planned rollout work under F001, not a test-update finding. |
| 16 | review-code | record | 1214 | Covered: F001. “restoration script approved; writer finishing its own checks”. Check restoration of the automatic-installation suppression window. The activity records approval and checks for the planned suppression-window restoration, with no separate repair identified. |
| 17 | review-code | record | 997 | Covered: F001. “pushing for CI and writing the over-limit refusal script”. Push for CI and prepare the planned over-limit refusal observation. The recovered order requires the over-limit refusal observation; CI/observation preparation is still planned rollout work under F001. |
| 18 | review-code | out-of-scope | 6363 | Covered: F001. “repair round 18: group membership proof, CPU display fix, branch CI fixes, runner rows”; “repairing promoted CPU and CI failures”; “pinning and packaging the Mac proof script”. CPU display/CI fixes and group proof extend the deployment into prior-phase implementation (F001 remains the umbrella). |
| 19 | review-code | out-of-scope | 1129 | Covered: F001, F004. “Round 20: finishing the held window inventory repair and its tests; live minute follows”. F004 repairs the prior desktop watcher’s recurring kdotool invocation. |
| 20 | review-code | out-of-scope | 493 | Covered: F005, F006. “Round 21: capture ticks query only terminal windows; review and live minute follow”. F005/F006 repair prior desktop polling breadth and missing coverage. |
| 21 | review-code | out-of-scope | 496 | Covered: F007. “pacing window position reads while the window is still”. F007 repairs prior per-render-frame window position reads. |

Finding titles (the IDs cited above):

- **F001:** Rollout not performed: readers, shims, and runner services on both machines are still at the pre-phase state
- **F002:** Inventory scripts skip accounts without ~/.cargo and ~/.rustup even when readers exist under ~/cargo/bin or ~/.local/bin
- **F003:** Inventory reports auto_install from raw TOML; a type-invalid config makes cargo-tile fall back to defaults with auto_install true
- **F004:** Linux backdrop still launches kdotool on every one-second capture tick whenever the window is not pinned
- **F005:** Unpinned capture ticks query every desktop window over D-Bus each second
- **F006:** No test pins class-independent kdotool discovery arguments
- **F007:** Window position is read on every render frame while the window is still

### 5. Add or replace, beside Depth

Plan: `catalyst-lfo.md`. Run `1a1f39e6-cf9f-4a5d-ac5e-707755ccd3de`, phase `6`, instance `2fd0f008-d97d-45e3-a586-27687d77ce46`. **18 rounds; 20 findings; 152,906 fix seconds (42h28m); 15 rounds past landing.** Start: 2026-10-11T09:22:09.403+00:00.

Start Work Order: `06c0d33ad3bf976e770fc18c586d66ccb05304e2:docs/plans/init/catalyst-lfo.md:244`, in `/home/natepiano/rust/hana_catalyst-lfo`. It specified Add by default, old Replace behavior, resolved live handles, lock behavior, icon metrics and inherited Add appearance. It did not give a numeric reach-band contrast or color target. F006 remained open over repeated color approaches before a separate capsule held. Four fix-labelled intervals are live verification without an identified code repair.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/1a1f39e6-cf9f-4a5d-ac5e-707755ccd3de.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 34645 | Covered: F001, F002, F003, F004, F005. “repairing the mode default, the switch row and the whole-number sliders”; “repairing the stage drag on a Replace axis”. Restore the specified Add default and resolved mode/switch behavior (F001–F005); these requirements were explicit in the start Work Order. The cited order requires Replace locking and names slider pointer/keyboard/reset/IME tests, but neither its file list nor its tests name Show stage dragging on a Replace-cabled axis. F002 and the second worker activity identify that missing authoring-route test. JSONL 641–642 records 17,373 seconds for mode/switch/slider work and 17,272 for stage drag. The primary gap allocation is therefore an upper bound for a mixed 34,645-second batch; F001 and the other mode/switch requirements were explicit and are not themselves specification gaps. |
| 2 | review-ux | spec-gap | 40620 | Covered: F006, F007, F008, F009, F010. “repairing the band tint, the Depth group spacing, the mode icon and the Material columns”; “writing drawn-pixel tests for the five repairs”; “finishing drawn-pixel tests for the five repairs after the sign-in fault”. The inherited Add-band look had no measured contrast/color target; F006 and proportion/alignment findings start the visual loop. |
| 3 | review-ux | fix-failed | 8875 | Covered: F006, F007, F008, F009, F010, F011, F012. “LFO keeps one size; column alignment test”; “LFO one-size tests and the final gates”. F006–F010 remained open; the implementation also needed constant cube size and a failed alignment fixture. |
| 4 | other | record | 1865 | Covered: —. “second live run and shots”. The fix activity is only a second live run and shots. |
| 5 | review-ux | fix-failed | 7621 | Covered: F006. “redrawing the reach band as a tint over a whole track”. The same reach-band contrast F006 was still open. |
| 6 | other | record | 1383 | Covered: —. “third live run and shots”. The fix activity is only a third live run and shots. |
| 7 | review-ux | fix-failed | 1812 | Covered: F006, F013. “making the reach band a pale blue and fixing the fill test”. The reach-band tint still failed; adjust its color and the pixel test F013. |
| 8 | other | record | 1716 | Covered: —. “fourth live run and shots”. The fix activity is only a fourth live run and shots. |
| 9 | review-ux | fix-failed | 17383 | Covered: F006. “near-white reach band checked through the app's colour path”. F006 remained open through another color-path repair. |
| 10 | review-ux | fix-failed | 2143 | Covered: F006, F014, F015. “light grey reach band through the ordinary colour path”. F006 persisted; pale-band pixel masks and another expected size F014/F015 needed updating. |
| 11 | review-ux | fix-failed | 1697 | Covered: F006. “reach band as a light capsule behind the track”. The same F006 finally required a separate capsule behind the track. |
| 12 | other | record | 1286 | Covered: —. “fifth live run and shots”. The fix activity is only a fifth live run and shots. |
| 13 | review-code | mechanical | 395 | Covered: F016. “updating the slider metrics test for the capsule”. Update the metrics helper to the new capsule shape (F016). |
| 14 | review-code | fix-regressed | 2916 | Covered: F017, F018, F019. “repair round 11: switch reading, effective mode, switch veto”. Rebuilt switch row reseeded readings and mode presentation disagreed with resolution (F017–F019). |
| 15 | review-code | fix-regressed | 705 | Covered: F020. “repair round 12: revert the Power switch change”. F020 explicitly says round 11 changed every tool’s Power semantics; revert it. |
| 16 | merge | mechanical | 4819 | Covered: —. “merge resolution after phase 6”. Resolve the phase-6 integration merge. |
| 17 | merge | mechanical | 10022 | Covered: —. “trial merges after phase 6”. Trial-merge integration work. |
| 18 | merge | mechanical | 13003 | Covered: —. “merge of widget's landed work”. Merge widget’s landed work. |

Finding titles (the IDs cited above):

- **F001:** New effect tools start in Replace instead of Add
- **F002:** Stage drag rewrites a Replace-cabled Show axis
- **F003:** Cabled switch row misses a flip when the final value is unchanged
- **F004:** Unreachable full-depth fallback in authored-mode resolution
- **F005:** Whole-number sliders cannot take Add or Replace from a whole-number cable
- **F006:** Add mode's reach band cannot be told from the slider track
- **F007:** Depth label sits nearer the setting's value than its own slider
- **F008:** Mode icon is smaller than the controls beside it
- **F009:** Replace icon cannot be made out at its size
- **F010:** Material: one cabled row widens both columns
- **F011:** column alignment test fails after the label ink-width change
- **F012:** LFO cube changes size when a cable is plugged into Rate
- **F013:** Rear-panel LFO fill test counts the new blue reach tint as live fill
- **F014:** LFO reservation pixel test masks the knob by brightness and now catches the pale band
- **F015:** Picture-card desktop help draws at 46 px against an expected 48 px after round 7
- **F016:** Jack-controls slider metrics helper expects the band at track height
- **F017:** Rebuilt switch row reseeds its cable reading from the authored input
- **F018:** Modulation mode presentation reads the authored mode, resolution reads the effective one
- **F019:** Authored Off vetoes a locked cabled switch
- **F020:** Round 11 changed every tool's Power semantics and locked the Power row

### 6. Startup: leftovers after the demo

Plan: `tool-based-ui-startup-polish.md`. Run `f93f565a-eef3-4174-ac03-755c36e844f7`, phase `31`, instance `b5546492-b71d-4d39-879b-5a0b340843db`. **20 rounds; 16 findings; 43,336 fix seconds (12h02m); 17 rounds past landing.** Start: 2026-10-15T11:53:20.446+00:00.

Start Work Order: `af797d1a44dccf371f32df31bf88860af732cf3f:docs/hana/tool-based-ui-startup-polish.md:1029`, recovered in `/home/natepiano/rust/tool-based-ui-startup`. It gave nine deferred grid tasks, named pixel tests and required the Phase 21 image to stay unchanged except for the named differences. The floor-low brightness defect was explicitly older than Phase 21 and was part of this new order. Findings show new Solid/Fade pixel defects and repeated F008 repairs; this was not an unspecified request for a new grid.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/f93f565a-eef3-4174-ac03-755c36e844f7.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 13723 | Covered: F001, F002, F003. “restoring the default grid line weight”; “making the grid tests see lost pixels”. Restore the expressly required default grid weight and make the lost-pixel tests fail (F001–F003). The cited order specifies brightness-step and line-weight tests, but no retained-pixel-count assertion for the orbit: F002 says lost pixels remain invisible to its brightness test, and F003 says close rows are skipped. The primary gap is that measurement coverage, not an absent grid-preservation goal; JSONL 59/66 records 5,160 seconds for lost-pixel tests and 8,563 for grid-weight code. The whole-batch gap allocation is an upper bound; F001 also corrects the explicit preservation contract. |
| 2 | gate | fix-regressed | 4305 | Covered: —. “tidying the floor grid code”; “tidying the floor grid tests”. JSONL 68–70 reports F004–F006: the hand-tuned major-width correction and code/test style defects after R1; 85–87 accepts them. This worker predates F007/F008. The faulty width correction follows R1 grid-weight work, supporting the repair-regression assessment, with style work in the same interval. |
| 3 | review-ux | fix-regressed | 0 | Covered: F007, F008. Ledger batch 2 at JSONL 95 covers F007 Fade dashes and F008 Solid moire/glow, opened at 93–94 after the preceding grid changes. Those new pixel defects violate the preservation contract. No fix_pass 2 starts after this dispatch: the tidying workers at 75–76 started earlier, while the next workers at 96–97 use 3. No worker time is observed. |
| 4 | gate | fix-failed | 4517 | Covered: —. “grid dashes and moire”; “grid pixel tests”. F008 remained open after the first grid repair. |
| 5 | gate | fix-failed | 647 | Covered: —. “grid repair apply”; “crossings lint”. Apply the existing grid correction and satisfy crossing lint. F008 remains open after R4; the apply activity continues the same grid correction. Crossing lint is only the concurrent mechanical part. |
| 6 | gate | mechanical | 526 | Covered: —. “lint”. Lint only. |
| 7 | other | fix-failed | 222 | Covered: —. “crossing glow”. F008 still open; correct crossing glow. |
| 8 | review-ux | fix-failed | 0 | Covered: F008. Ledger batch 3 at JSONL 129 covers F008 after its still_open verdict at 128. No matching fix_pass 3 starts afterwards; following workers use 4. The same unresolved Solid defect supports continuation, with no observed worker time. |
| 9 | other | mechanical | 914 | Covered: —. “glow fixture”. Update the glow fixture. |
| 10 | other | fix-failed | 1273 | Covered: —. “Solid coverage fix”. Solid coverage still failed F008. |
| 11 | review-ux | fix-failed | 0 | Covered: F008. Ledger batch 4 at JSONL 142 covers F008 after the still_open verdict at 141. The later worker at 143 uses fix_pass 5, so the dispatch stays separate. This is the same unresolved defect, with no measured worker time. |
| 12 | other | fix-failed | 360 | Covered: —. “Solid edge band”. JSONL 141 leaves F008 still_open and 142 dispatches ledger batch 4 before this worker starts at 143 under fix_pass 5. It continues Solid coverage/glow repair. F009 opens later at 149 and is not a finding repaired by this interval. |
| 13 | review-ux | fix-regressed | 774 | Covered: F008, F009. “fixing the centre-line fade and minor-line beading”. JSONL 148 leaves F008 open; 149 opens F009 and explicitly dates its new beading to ledger round 4, the preceding edge-band work. Batch 5 at 151 covers both. The new beading supports the primary regression root; F008 also continues. |
| 14 | gate | mechanical | 168 | Covered: —. “writing the floor-grid regression tests”. Write the floor-grid regression tests. |
| 15 | gate | mechanical | 814 | Covered: —. “writing the floor-grid regression tests”. Continue those tests. |
| 16 | other | record | 820 | Covered: —. “proving the floor-grid regressions red”. Prove the regression tests fail without the repair. The activity names execution of the already-written tests, not another test update or source repair. |
| 17 | review-ux | fix-regressed | 4886 | Covered: F010. “fixing the Solid grid's density clip”. F010 says the density fade lost mid-floor minors and horizon reach. |
| 18 | review-ux | fix-failed | 653 | Covered: F011. “matching the centre line's fade to its neighbours”. The centre line still ended ahead of its neighbours (F011). |
| 19 | review-code | fix-regressed | 1805 | Covered: F012, F013. “repairing grid blending over the scene and the centre-line test bound”. New max blending erased lines over brighter backgrounds; F013 exposed a permissive test. |
| 20 | review-ux | fix-regressed | 6929 | Covered: F012, F014, F015. “repairing grid compositing order and restoration mask”. New restoration pass changed non-grid antialiasing and blackened tool edges (F014/F015); F012 persisted. |

Finding titles (the IDs cited above):

- **F001:** Fade width floor thins the grid at every pose
- **F002:** Orbit brightness test blind to lost grid pixels
- **F003:** Weight test skips rows closer than 8 px
- **F004:** Major line width correction is a hand-tuned pitch window, not the cause
- **F005:** grid_gizmo style items 1-3 and 5-10
- **F006:** grid_reach test style items 11-17
- **F007:** Fade: major lines break into dashes where minor lines cross
- **F008:** Solid: moire band on the far floor, dashed majors and glow at major crossings
- **F009:** Solid minor lines bead: brightness pulses ~4x every ~56 px (since round 4)
- **F010:** Solid loses mid-floor minors and ends 40-50 px short of the horizon after the density fade
- **F011:** Solid centre line ends about 35 px before its neighbours reach the vanishing point
- **F012:** Max blending composites grid lines against the scene and skybox, so lines vanish over brighter backgrounds and Fade changes
- **F013:** Solid centre-line test accepts a bright plateau and abrupt end (removing sightline_fade_weight still passes)
- **F014:** SMAA restoration pass rewrites non-grid pixels inside its widened quad, dropping scene antialiasing beside Solid strokes
- **F015:** Solid stroke restoration blackens tool-edge antialiasing where grid lines meet a tool
- **F016:** With MSAA on, a later scene resolve can erase the grid composite

### 7. Rate, phase and range on the jack panel, driven by cables

Plan: `catalyst-lfo.md`. Run `1a1f39e6-cf9f-4a5d-ac5e-707755ccd3de`, phase `4`, instance `72ee4786-d9e2-4e56-a82f-a7dbda6acba1`. **16 rounds; 0 findings; 34,281 fix seconds (9h31m); 13 rounds past landing.** Start: 2026-10-10T05:04:11.501+00:00.

Start Work Order: `8e10b53932e4bbc5affa885dba17eb31c3375679:docs/plans/init/catalyst-lfo.md:156`, in `/home/natepiano/rust/hana_catalyst-lfo`. It named six panel rows, cable-driven inputs, continuous controls, old-scene loading and BRP writers. It required visible fit and alignment, but gave no numeric plate-margin or ordinary-number fit target. No ledger findings were opened; the exact pass activities are the available defect evidence.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/1a1f39e6-cf9f-4a5d-ac5e-707755ccd3de.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | other | spec-gap | 2298 | Covered: —. “fixing old-scene loading and the face fit”; “updating the catalog and prompt to the new LFO contract”. Adapt old-scene loading, catalog and prompt to the changed LFO schema. The cited start order names six rows and cable behavior but no numeric face-fit or plate-margin target. The face-fit activity is the gap evidence for this mixed interval; old-scene loading and catalog/prompt changes also occur and are not test-only work. |
| 2 | other | record | 1510 | Covered: —. “taking Phase 4 live shots on 15771”. A fix pass contains only the required live shots. |
| 3 | other | spec-gap | 2440 | Covered: —. “fixing the jack panel's visible defects”. The start Work Order required six rows but gave no numeric face-fit or margin targets; the live check found visible defects. |
| 4 | other | spec-gap | 1877 | Covered: —. “making the jack panel one fitted plate”. The panel needed a single fitted plate, beyond the Work Order’s row-level fixture. |
| 5 | gate | mechanical | 4499 | Covered: —. “fixing two tests the fitted plate broke”. Two tests broke when the fitted plate changed. |
| 6 | other | fix-failed | 817 | Covered: —. “repair round 5: split panel_ports and prove the fitted panel”. Split panel_ports and pin the fitted-panel result. The fitted-panel work from R4 is still being proved after R5 repairs its broken tests. The activity continues that same repair while splitting panel_ports; a source refactor alone is not lint or a test update. |
| 7 | other | spec-gap | 6715 | Covered: —. “repair round 6: plate margins and a clipped value”. No numeric plate margin/value-ink target was given; another fit and clipping adjustment followed. |
| 8 | other | fix-regressed | 1681 | Covered: —. “repair round 7: five regressions from round 6”. The activity explicitly names five regressions from round 6. |
| 9 | gate | mechanical | 537 | Covered: —. “repair round 8: rear-click test samples”. Update rear-click sample points. |
| 10 | gate | fix-failed | 3687 | Covered: —. “making the rear-panel click test stable”. The rear-panel click assertion remained unstable. |
| 11 | other | fix-failed | 1968 | Covered: —. “evening the LFO plate's right margin”. The right margin still needed correction after earlier fit work. |
| 12 | gate | record | 1280 | Covered: —. “testing Phase 4 against organon's latest panel work”. Verify compatibility with organon’s latest panel changes. This activity records compatibility testing, without a test update or source repair. |
| 13 | merge | mechanical | 1405 | Covered: —. “finding why the merged tree shifts the Show label colour”. Resolve changed Show-label color after integration. |
| 14 | other | spec-gap | 606 | Covered: —. “measuring whether ordinary value fields fit the new number format”. The new number format had no explicit ordinary-field fit target; measure it now. |
| 15 | merge | spec-gap | 1461 | Covered: —. “making every LFO number fit its box and finishing the merge copy”. Make LFO values fit and finish the integration copy. R14 had just measured ordinary-field fit without an explicit ordinary-number width target in the cited order. This continues that sizing decision; the merge copy is only one part of the interval. |
| 16 | gate | mechanical | 1500 | Covered: —. “fixing the rear-panel click test on the merged code”. Repair the click test against merged code. |

### 8. Notice fixes: splash word, Frame time at Home, Unfold word, off Screen rim

Plan: `tool-based-ui-startup-polish.md`. Run `e54c80d4-ad05-4d5a-b824-67d79e7f1131`, phase `58`, instance `f6df4742-9014-46ed-85e2-b4e35c5d8e66`. **15 rounds; 4 findings; 126,104 fix seconds (35h01m); 12 rounds past landing.** Start: 2026-10-17T13:47:34.776+00:00.

Start boundary limitation: the last saved plan before the start, `8aea487fbfe6938fa16e0b090237b9012cbd5d5f:docs/hana/tool-based-ui-startup-polish.md`, has no phase 58. The first recovered saved version containing it is `dda378212` on October 20, after the October 17 start; its order begins at line 2089 in `/home/natepiano/rust/tool-based-ui-startup`. It names splash word, Frame time at Home, Unfold word and the off-Screen rim. It also records the later Mac clipping report. Scope assessments against that saved order are provisional, not proof of the exact start-time wording. Annunciator, Screen zoom and Control Panel work are separate from those four named notices.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/e54c80d4-ad05-4d5a-b824-67d79e7f1131.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | gate | fix-failed | 34490 | Covered: —. “fixing the rim on the Solid floor and Frame time's bottom edge”; “making the splash test see the drawn gap and settling the spacing change”; “fitting Frame time to a row edge with no blank band”. The held notice still failed rim/row-edge checks; F001–F004 identify wrong Solid-floor scope, clipping and an unasked spacing change. |
| 2 | merge | mechanical | 7908 | Covered: —. “repairing the Frame time stage after the trunk merge”. Adapt Frame time to the trunk merge. |
| 3 | merge | mechanical | 1573 | Covered: —. “repairing Screen centring after the inspectors merge”. Adapt Screen centring to the inspectors merge. |
| 4 | other | out-of-scope | 14213 | Covered: —. “fixing the smeared Faults text”. Faults text is outside the four subjects in the later saved order; its exact relation to the start-time scope is unverified. |
| 5 | other | out-of-scope | 1288 | Covered: —. “fixing Annunciator rejudge, EnvironmentDrive fixtures and chooser playback”. Update Annunciator/EnvironmentDrive/chooser fixtures after integration. The later recovered order names four notice subjects; Annunciator rejudge and chooser playback are separate behavior repairs, alongside fixture work. As with R4/R7/R10, this scope assessment is provisional because no timestamp-exact start order survives. |
| 6 | other | fix-failed | 16051 | Covered: —. “fixing the Annunciator quiet-frame rejudge”. Quiet-frame rejudge still needed repair after the prior pass. |
| 7 | other | out-of-scope | 1456 | Covered: —. “fixing the Screen jack panel zoom radius”. Screen jack-panel zoom radius is separate from the switched-off rim contrast named in this phase. |
| 8 | gate | mechanical | 1218 | Covered: —. “fixing the 12 wider test failures”. Repair 12 wider-suite failures. |
| 9 | gate | fix-failed | 1305 | Covered: —. “fixing the last three wider test failures”. Repair the last three wider-suite failures. The last three are the wider test failures from R8 that remain after that interval; this is continuation of the same failed test batch. |
| 10 | other | out-of-scope | 1840 | Covered: —. “fixing the Control Panel waits and the floor control”. Control Panel waits and a floor control expand beyond the four notice fixes. |
| 11 | gate | mechanical | 614 | Covered: —. “finishing lint and the full test line”. Finish lint and the full test line. |
| 12 | gate | mechanical | 7956 | Covered: —. “fixing two lint errors, then the full test line”. Clear two remaining lint errors. |
| 13 | gate | record | 9609 | Covered: —. “rerunning lint, then the full test line”. A verification-only rerun of lint and the full test line, with no source repair named; counted as a fix worker interval by the API. |
| 14 | other | fix-failed | 10169 | Covered: —. “fitting Frame time's bottom to its text”. Frame time bottom-to-text fit remained wrong; the later saved Work Order records the Mac clipping report. |
| 15 | other | out-of-scope | 16414 | Covered: —. “making the splash bars only move forward”. Make splash progress monotonic. The activity does not establish which earlier repair introduced the backward movement. The later recovered order names splash-word appearance, not progress-bar direction. This activity repairs a different behavior without identifying an earlier repair that caused it. The missing exact start order makes this scope allocation provisional. |

Finding titles (the IDs cited above):

- **F001:** Switched-off rim also changes on the Solid floor
- **F002:** Frame time bottom does not sit on a row edge
- **F003:** Split connectivity test ignores the shader's child gap
- **F004:** Creation spacing change no work order asks for

### 9. Jack panel polish: Camera jacks, rows and insets

Plan: `hana-organon-design.md`. Run `a702c130-c737-4e4c-99bb-c6524ebaf811`, phase `15`, instance `207917dc-c72d-4d07-b168-6dd006bb126c`. **14 rounds; 23 findings; 59,004 fix seconds; 11 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/a702c130-c737-4e4c-99bb-c6524ebaf811.jsonl), joined by this instance. Start Work Order: `7b0e5bc01e62:docs/hana/hana-organon-design.md:510–555`, in `/home/natepiano/rust/tool-based-ui-geometry-material`. The order sets a closed Viewfinder 9 px floor and drawn-pixel tests, but does not specify saved text-label compatibility across the new enum.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 10147 | Covered: F001, F002, F003, F004, F005, F006, F007, F008, F009, F010, F011, F012, F013, F014, F015, F016, F017. “two seats repairing what review found”; F001: old Screen saves fail after text labels become an enum, alongside F002–F017. Saved-label compatibility is absent from the start order. |
| 2 | review-code | fix-failed | 5447 | Covered: F007, F008. “sizing the closed Viewfinder words and running the unrun drawn tests”; F007: words below 9 px, and F008: incomplete drawn acceptance, remain open after R1. |
| 3 | review-code | fix-failed | 821 | Covered: F007. “the closed Viewfinder card grows as one piece”; F007 still_open after attempt 2, accepted after attempt 3. |
| 4 | review-code | fix-regressed | 2547 | Covered: F018. “eleven closed-card unit tests”; F018 explicitly reports that whole-card scaling fails 11 unit tests. |
| 5 | review-code | mechanical | 18675 | Covered: F019, F020. “testing the centred close Camera On help overlay pixels” / “making a closed Effects assembly selectable”; F019 is titled “Merge: jack row sizing disagrees with widget slider rules” and F020 “Merge: camera card and open assembly tests fail” (JSONL 489–490). This is integration against existing shared sizing/picking contracts, not a new target choice; the ledger priority leaves its immediate cause review-code. |
| 6 | review-code | fix-regressed | 2859 | Covered: F019, F020, F021. “finding why the Camera switch row gets no pointer hit”; F021 explicitly says the repair regressed two Camera title tests; F019/F020 remain. |
| 7 | review-code | fix-failed | 2715 | Covered: F019, F020, F021. “fixing the missing-camera pixel test on the taller jack panel”; F019/F020/F021 still_open. |
| 8 | review-code | fix-failed | 2612 | Covered: F019, F020, F021. “restoring the closed panel's top edge when its caption row goes away”; the F019/F020/F021 batch remains open. |
| 9 | review-code | fix-failed | 1892 | Covered: F019, F020, F021. “finishing the missing-camera test and the wider jack panel test run”; F019/F020 remain; F021 is eventually accepted. |
| 10 | review-code | mechanical | 2578 | Covered: F020, F022. “resolving the merge conflicts and fitting the jack panels to the shared panel style”; F022 names catalyst merge 4c56bba4c; F020 remains. |
| 11 | review-code | mechanical | 12 | Covered: F020, F023. “fitting the jack panels to the merged control sizing”; F023 names catalyst merge 1b19832a1, five conflicts and three picture-test failures. |
| 12 | review-code | fix-failed | 5807 | Covered: F020, F023. Same recorded activity as R11; F020/F023 still_open. |
| 13 | review-code | fix-failed | 1506 | Covered: F020, F023. “fitting the newest merge and the Material group spacing”; F020/F023 still_open again. |
| 14 | review-code | fix-failed | 1386 | Covered: F020, F023. “making the Material jack panel even”; F023 accepted on attempt 4 and F020 on attempt 10. |

Hard landing: R4–R14 concern named failing pixel/slider/Camera tests, repair regressions, or merge conflicts and picture-test failures. These are kept-work cases; the rule permits this tail. Ledger precedence leaves merge work labelled review-code.

### 10. Selection face and glow

Plan: `tool-based-ui-inspectors.md`. Run `8777d55e-0ac2-4586-94c5-c7c309274d22`, phase `10`, instance `a5f11f1e-1f26-46d5-bf61-7dc3fead4d05`. **14 rounds; 14 findings; 31,968 fix seconds; 11 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/8777d55e-0ac2-4586-94c5-c7c309274d22.jsonl), joined by this instance. Start Work Order: `dae29fabc9f9:docs/hana/tool-based-ui-inspectors.md:340–368`, in `/home/natepiano/rust/hana_catalyst-inspectors`. The order names Selection, tool registration and live updates, but no scene-replacement or real startup-order regression test. Findings F001–F004 open at run-file lines 633–641.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | gate | spec-gap | 1296 | Covered: —. “writing Phase 10 review regression tests” / “repairing Phase 10 review findings”; F001: scene replacement loses controls; F002: hovered outline keeps old width; F003: fixtures lack SelectionLook. These lifecycle/setup tests are absent from the order. |
| 2 | gate | mechanical | 1027 | Covered: —. “fixing cable hover test compile errors and running the repair tests”. |
| 3 | review-code | record | 0 | Covered: F001, F002, F003. Ledger batch 1 at JSONL 636 names F001–F003 after their entries at 633–635, but the fix_pass 1 worker started earlier at 629 and finished at 631. No later eligible worker takes it. The record lacks a dispatch-to-worker boundary for this retrospective entry; no additional repair or time is inferred. |
| 4 | gate | mechanical | 1986 | Covered: —. “running lint and the repair tests after the hover test fix”. |
| 5 | review-code | fix-regressed | 3592 | Covered: F004. “making the Selection tool register in the app's plugin order”; F004: real app startup panics on the new environment-tool registration. The causal link is to phase registration; the exact preceding repair contribution is not isolated. |
| 6 | gate | fix-failed | 3025 | Covered: —. “fixing the plugin order test setup”; F004 still_open after its first fix attempt. |
| 7 | review-code | fix-failed | 1665 | Covered: F004. “routing the plugin order test through main's registration”; F004 later accepted on attempt 2. |
| 8 | gate | mechanical | 3586 | Covered: —. “resolving the merge conflicts and running lint and tests”. |
| 9 | gate | mechanical | 2481 | Covered: —. “adding the missing setup, then lint and tests”. |
| 10 | gate | mechanical | 107 | Covered: —. “adding the resource to three fixture modules, then lint and tests”. |
| 11 | gate | mechanical | 2865 | Covered: —. “finishing two fixture edits, then lint and tests”. |
| 12 | gate | mechanical | 168 | Covered: —. “resolving five merge conflicts, then lint and tests”. |
| 13 | gate | mechanical | 4028 | Covered: —. “carrying one setup line into the moved plugin, then lint and tests”. |
| 14 | other | fix-regressed | 6142 | Covered: —. “finding why the ring drag no longer turns the cube”. Lost behavior supports a provisional regression label; the activity does not identify which earlier patch caused it. |

Hard landing: the newly post-landing R4 runs lint/tests; R5–R13 concern the real-app startup panic or compile/lint/fixture/merge test failures. R14 reports lost ring-drag behavior, but does not establish phase ownership or the causal patch, so its eligibility is uncertain.

### 11. Control Panel fold cube

Plan: `tool-based-ui-inspectors.md`. Run `8777d55e-0ac2-4586-94c5-c7c309274d22`, phase `7`, instance `c8684080-72c8-4b8a-a3ac-9bc5cfdfcf0e`. **13 rounds; 16 findings; 28,864 fix seconds; 10 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/8777d55e-0ac2-4586-94c5-c7c309274d22.jsonl), joined by this instance. Start Work Order: `54ae76755c15:docs/hana/tool-based-ui-inspectors.md:246–279`, in `/home/natepiano/rust/hana_catalyst-inspectors`. The order explicitly names reversible 450 ms Pane/Cube/Cross motion, no crossed faces, Faults clearance and pixel-size expectations (lines 258–275). The overall visible contracts were explicit; R1 identifies a missing playback-synchronization test rather than treating their correction as mechanical.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 5685 | Covered: F001, F002, F003, F004, F005, F006, F007, F008, F009. “repairing Phase 7 review findings”; F006: intersecting arms; F007: wrong Cross after reversal; F008: camera/playback desynchronization; F009: unequal margins; F001–F005 concern style/representation. The start order already requires the visible behavior. The cited order specifies synchronized 450 ms camera/fold motion and tests shape/reversal outcomes, but names no sampled camera-versus-fold playback synchronization test. F008 exposes that missing test case; the other visible requirements were explicit and are not relabelled as absent goals. |
| 2 | other | fix-failed | 1051 | Covered: —. “finishing the Phase 7 repairs”; the same F001–F009 batch remains in flight. |
| 3 | review-code | record | 0 | Covered: F010, F011, F012, F013. Ledger-only dispatch, no paired worker, for F010 invisible selected panel, F011 mirrored side-centred Cross, F012 overlap with Frame time and F013 AnchorPose warnings. A new batch alone does not establish a serial live check or a completed patch. |
| 4 | review-code | fix-regressed | 207 | Covered: F014. “repairing the Phase 7 fold reversal”; F014: reversal can reverse an unrelated camera journey. |
| 5 | other | fix-failed | 5648 | Covered: —. Same recorded fold-reversal activity and F014; another worker interval before acceptance. |
| 6 | review-code | spec-gap | 131 | Covered: F015. “repairing the Phase 7 back-face text”; F015: hidden back-face text draws through the front. The visible defect needs correction, but this activity alone does not prove a serial live-check sequence or a prior repair regression. The cited order requires hidden faces to be invisible and unpickable, but its named Pane/shape tests do not require a rendered text-occlusion check. F015 names that missing rendered-text case; a code correction is required, not just a test update. |
| 7 | other | fix-failed | 156 | Covered: —. Same recorded back-face-text activity and open F015. |
| 8 | other | fix-failed | 101 | Covered: —. Same recorded back-face-text activity and open F015. |
| 9 | other | fix-failed | 57 | Covered: —. Same recorded back-face-text activity and open F015. |
| 10 | other | fix-failed | 513 | Covered: —. Same recorded back-face-text activity and open F015. |
| 11 | other | fix-failed | 1689 | Covered: —. Same recorded back-face-text activity and open F015. |
| 12 | other | fix-failed | 4469 | Covered: —. Same recorded back-face-text activity and open F015, ultimately accepted. |
| 13 | review-ux | spec-gap | 9157 | Covered: F016. “repairing the Phase 7 side-face fold”; F016: non-uniform cube-root scale prevents Cross. The cited start order tests side-face rooting and front-facing squares, but has no non-uniform-root-scale case. JSONL 420–442 records one F015 repair/acceptance before F016 at 447; it does not identify F015 as a live-check finding or prove its fix introduced F016. The root is the missing scale test, not an inferred serial live-check violation. |

Hard landing: R4–R5 concern F014: fold reversal reverses an unrelated journey. R6–R13 are visible own-change defects but the cited evidence does not establish the preceding patch or a pre-phase comparison. Their eligibility is uncertain. The seven F015 dispatches include continuations; the ledger records one accepted fix attempt, not seven rejected patches.

### 12. Startup animation: the Unfold tool

Plan: `tool-based-ui-startup-polish.md`. Run `df477c65-1d02-4c11-8810-b61c83db1f2c`, phase `22`, instance `85cfb642-87cf-4e6b-937c-95e4d9bd31b3`. **12 rounds; 34 findings; 87,437 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/df477c65-1d02-4c11-8810-b61c83db1f2c.jsonl), joined by this instance. Start Work Order: `26fdfb72edf6:docs/hana/tool-based-ui-startup-polish.md:751–795`, in `/home/natepiano/rust/tool-based-ui-startup`. The order explicitly requires real creation below 5 ms, correct tests/wiring, bounds, cabled words and whole-word framing. It says no timing test exists to copy (line 793). The 5 ms target was explicit, but choosing its measurement interval remained unresolved and required judgment in the first batch. F001–F012 are at run lines 161–172; F032–F034 at 671 and 684–685.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 8083 | Covered: F001, F002, F003, F004, F005, F006, F007, F008, F009, F010, F011, F012. “repairing the Unfold tool's placement, fold direction and word routing”; F004 times only the pre-build update, F005 uses fabricated bounds, F006 checks CPU normals rather than rendered shading. The goal and test/wiring obligations were explicit; the timing interval needed choosing. The cited order gives the 5 ms goal but explicitly says no timing test exists to copy (line 793), without defining which app updates the interval includes. F004 times the wrong update and chooses the fastest sample. That measurement definition is the primary gap for this mixed code/test batch, not an absent creation-cost goal. |
| 2 | review-code | fix-failed | 38886 | Covered: F002, F004, F007, F008, F009, F010, F013, F014, F015. “font cache warm-up for creation cost” / “fixing gap closing in the square shader”; F002/F004/F007–F010 remain; F004 still_open on attempt 2; F013–F015 are added. |
| 3 | review-code | fix-failed | 3420 | Covered: F002, F004, F016, F017. “measuring Create Unfold's own cost against an idle update”; F002/F004 remain, with F016 staged Home bounds and F017 unbounded regression loop. |
| 4 | review-code | fix-failed | 2858 | Covered: F004. “pinning the Unfold tool's own creation cost”; F004 accepted on attempt 4. |
| 5 | review-ux | spec-gap | 5427 | Covered: F018, F019, F020, F021, F022, F023. “fixing the design-check defects on the Unfold tool”; F018–F023 form one batch: absent body/cable, missing shield bounds, crowded labels and unreadable square-size value. The cited order names the face controls, word shading and whole-word framing tests, but no rendered body/cable visibility test or drawn square-size readout floor. Those missing acceptance checks support the primary gap assessment; six findings in one batch do not establish serial-check. |
| 6 | review-ux | spec-gap | 7009 | Covered: F024, F025, F026, F027, F028. “fitting the Unfold cube to its panel and landing the cable on its jack”; F024–F028 arrive together: cable on floor, oversized case, word/frame collision, displaced gizmo and shield warning. The cited order gives a fixed word offset but no numeric word/case clearance or case-to-panel size target. The primary gap is that fit target; new IDs do not prove the preceding fix introduced these defects, and this is a batched check. |
| 7 | review-code | mechanical | 2429 | Covered: F029, F030. “merge repair: Square size display and word clearance”; F029/F030 explicitly follow catalyst merge 8ccc053b7. |
| 8 | review-code | fix-regressed | 5148 | Covered: F031. “measure and fix the Create Unfold slowdown”; F031 explicitly reports post-merge whole-click cost as high as Create Shape. |
| 9 | other | fix-failed | 8254 | Covered: —. “interleave the Shape and Unfold whole-click samples” / “find why the palette draws no panel background”; whole-click measurement continues; F032 separately names missing palette background after the Phase 22 merge. This interval has mixed work. |
| 10 | other | spec-gap | 3470 | Covered: —. “make the open word clear the case and lie flat”; F033 word crosses the case top rail; F034 open word is not one flat sheet. The cited start order names whole-word framing and a fixed offset, without a drawn case-clearance floor or a planarity acceptance test. Both findings were sent together; no one-defect live-check sequence is shown. |
| 11 | gate | record | 192 | Covered: —. “finish the round10 gates and summary”. Only gate execution and summary work are named; no test update or source repair is recorded. |
| 12 | other | fix-failed | 2261 | Covered: —. “repair round11: the open word draws as one clean sheet”; unresolved flat-sheet case F034 continues. |

The original `/home/natepiano/rust/tool-based-ui-startup-polish` checkout is absent; the historical order is available through the shared git history in the startup checkout named above.

Hard landing: R4, R7 and R8 have budget/test, merge or explicit post-merge slowdown evidence. R11 only executes gates and writes the summary, so it is a record row. R5–R6/R9/R10/R12 are uncertain: fresh visual defects or mixed benchmark/palette work lack a before comparison for every part. Blocker severity alone does not establish retained-category eligibility.

### 13. Twin camera wording, first drag, and live chooser placement

Plan: `tool-based-ui.md`. Run `7b115092-80c3-42c2-9652-2ff222d2c722`, phase `137`, instance `b73e8bda-2516-424e-b466-5cdb7e424a02`. **13 rounds; 10 findings; 76,272 fix seconds; 10 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/7b115092-80c3-42c2-9652-2ff222d2c722.jsonl), joined by this instance. Start Work Order: `a43bbfe13049:docs/hana/tool-based-ui.md:4178–4206`, in `/home/natepiano/rust/tool-based-ui-trunk`. The order requires a contained, reachable crowded long-list chooser that does not cover Open, and separates the deterministic gate from deferred live checks. It gives no minimum usable chooser width, equal-gap target or empty-device-message width cap. The final start is unfinished and has zero observed time.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 720 | Covered: F001, F002, F003, F004. “repairing where the device chooser opens”; F001: no opening room when the card fills the window; F002: unreachable labels in a 12 px chooser. Containment alone does not define usable dimensions. F003/F004 are nits. |
| 2 | review-ux | spec-gap | 3114 | Covered: F005, F006, F007. “repairing the device chooser's width and gaps”; F005: no-camera message widens chooser to 732 px; F006: unequal gaps; F007: crowded chooser covers Screen/Power. The first two lack measured targets. |
| 3 | gate | mechanical | 141 | Covered: —. “running the chooser repair's lint and tests”; no new batch joins this lint/test continuation, so its cause is gate. |
| 4 | gate | fix-failed | 21063 | Covered: —. “fixing three visible defects, then the chooser lint and tests”; the width/gap/coverage work continues after R2, but no new batch joins this interval. |
| 5 | other | fix-failed | 2845 | Covered: —. “fixing the defects the live chooser shots showed”; the activity continues the visible chooser repair. The later JSONL 584–589 verdict accepts F005/F006, leaves F007 still_open and first records F008–F010; those future entries do not join this earlier worker. |
| 6 | other | fix-failed | 19240 | Covered: —. “resuming the chooser repair after the move”; the activity continues the preceding chooser repair. F008–F010 are only recorded later; they are not an attached batch or proof of another failed patch here. |
| 7 | other | fix-failed | 4532 | Covered: —. “repairing the Output chooser and Open alignment”; the visible placement work continues without a matched ledger batch. |
| 8 | other | fix-failed | 4822 | Covered: —. “finishing the device chooser repair”; the corrected join removes the already-accepted F001–F004 association. Continuity of the placement work is inferred from the activity, not a reopened finding. |
| 9 | gate | record | 10344 | Covered: —. “rerunning the device chooser gates”. Only rerunning gates is named; no test update or source repair is recorded. |
| 10 | other | out-of-scope | 6263 | Covered: —. “fixing the rounded box landing and the crowded chooser shot”; rounded-box landing is outside the twin-wording/first-drag/chooser order. The same interval includes in-scope work; its whole time is an upper bound on scope spill. |
| 11 | gate | mechanical | 3188 | Covered: —. “clearing its lint findings”. |
| 12 | review-ux | fix-failed | 0 | Covered: F007, F008, F009, F010. Ledger batch 3 at JSONL 591 covers F007–F010 after F007 remains still_open at 586 and new Output/HUD findings open at 587–589. Every fix_pass 3 worker started earlier; the next unfinished Output/chooser worker uses 0. This is a ledger-only continuation of failed chooser placement, with no observed patch or time. |
| 13 | other | fix-failed | 0 | Covered: —. “fixing the Output card and crowded chooser”; unfinished continuation, no observed finish or invented time. |

Hard landing: R9 only reruns gates and is a record row; R11 clears lint. R4–R8 and R12–R13 are uncertain: repeated placement work and a late ledger-only dispatch do not establish retained regressions. R10 is mixed: rounded-box landing is outside scope, while the crowded-chooser shot is verification; the whole dispatch is uncertain for eligibility. Its 6,263 seconds are an upper bound for outside-scope work.

### 14. The Arrange jacks glyph matches its neighbours in size

Plan: `tool-based-ui.md`. Run `576fc590-6052-460f-a146-43c3ddb433b9`, phase `111`, instance `ba43a6d3-20d9-41bd-862d-5ffd3cc825b0`. **12 rounds; 15 findings; 56,771 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/576fc590-6052-460f-a146-43c3ddb433b9.jsonl), joined by this instance. Start Work Order: `e8f5b8524848:docs/hana/tool-based-ui.md:3475–3516`, in `/home/natepiano/rust/tool-based-ui-trunk`. The order sets the glyph to 24–26 px, a common drawn/hit/framed envelope, tooltip at least 9.5 px and flat dimmed strokes. It does not name repeated-Arrange reparenting or its test, nor a policy for the tall screwdriver's contribution to arranged framing.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | gate | mechanical | 4267 | Covered: —. “fixing the glyph's lint errors and halo test, then shooting it”. This worker starts before F001/F002 open; its activity supports lint/test follow-through, not the later repeated-Arrange finding as its missing-test root. F001/F002 belong to the first ledger batch on the subsequent repair interval. |
| 2 | review-code | fix-failed | 12753 | Covered: F001, F002. “repairing repeated Arrange Jacks”; F001/F002 remain in flight. |
| 3 | other | fix-failed | 3161 | Covered: —. “finishing the repeated Arrange repair”; F001/F002, with no accepted closure between these intervals. |
| 4 | gate | mechanical | 271 | Covered: —. “fixing a test lint and taking the Arrange Jacks shots”. |
| 5 | gate | record | 629 | Covered: —. “running the Arrange tests and taking the Arrange Jacks shots”. Only running tests and taking shots are named; no test update or source repair is recorded. |
| 6 | review-ux | spec-gap | 5074 | Covered: F003, F004. “fixing the Arrange framing and row placement”; JSONL 32–33 sends F003 frame 2.4 times taller than row and F004 earlier row showing through as one batch. The cited glyph order defines its envelope but no arranged-view picture-share/height target or placement policy for repeated rows. Those missing fit/placement criteria support this root; the two findings do not establish serial-check. |
| 7 | review-ux | spec-gap | 5713 | Covered: F005, F006. “fixing the frame margins and the stray body in the arranged view”; JSONL 41–44 accepts F003/F004, then sends F005 margins 35 px horizontally versus 22.6 px vertically and F006 clipped coral slab together. The cited order gives glyph/tooltip dimensions, not an opposite-margin target or a whole-scene-subject rule for the arranged view. This is a batched fit decision, not one defect sent per check. |
| 8 | review-ux | spec-gap | 3162 | Covered: F007, F008. “framing the arranged row closer and centring it”; JSONL 51–54 accepts F005/F006 and sends F007 unreadable jack labels with F008 loose/off-centre view. The cited order gives a 9.5 px tooltip floor, but no arranged jack-label floor or picture-share/centering tolerance. The missing targets support this root; the two-finding batch is not serial-check. |
| 9 | review-ux | spec-gap | 2446 | Covered: F009, F010. “sizing the selection screwdriver on screen and centring the arranged row”; JSONL 62–65 accepts F007/F008 and sends F009 frame 5 px above status with F010 tall screwdriver pushing the row low. The cited order has no screwdriver contribution/size policy for arranged framing or numeric status clearance. The two-finding batch needs those decisions; it does not show serial-check. |
| 10 | review-ux | fix-failed | 3085 | Covered: F010. “sizing the screwdriver on screen like the gizmo”; F010 still_open after attempt 1, accepted on attempt 2. |
| 11 | review-ux | out-of-scope | 14637 | Covered: F011, F012, F013, F014. “turning the screwdriver to face you and lining up the corner controls”; JSONL 83–87 accepts F010 and sends F011 shaft contrast 1.97:1, F012 edge-on 4.7×22.8 px, F013 screwdriver off centre and F014 floating glyph together. The cited glyph order lists rotate files for the shared ray only, not screwdriver material contrast or orientation. This primary scope allocation includes in-scope F014, so 14,637 seconds is an upper bound on outside-scope work. Four findings were batched; none proves serial-check. |
| 12 | review-ux | fix-failed | 1573 | Covered: F012, F013, F015. “laying the screwdriver at an angle, evening the corner insets, and wrapping long rows”; F012/F013 accepted on attempt 2; new F015: six-tool labels below 9 px. |

Hard landing: R4 updates test lint with verification shots; R5 only runs tests/takes shots and is a record row. R6–R12 are successive visual findings and continued F010/F012/F013; exact pre-phase comparisons and causal patches are absent. Their retained-category eligibility is uncertain. The checks batched two to four findings each; this is not evidence of the one-defect serial-check pattern or of a violation of item 7.

### 15. Into the room follows the picture, and each effect step frames its subject

Plan: `info-panels-plan.md`. Run `f205ddd0-5865-4dec-9b85-849e4ffa3dd8`, phase `33`, instance `0661966c-4a96-4c0f-9304-57d36a6701cb`. **12 rounds; 12 findings; 3,591 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/f205ddd0-5865-4dec-9b85-849e4ffa3dd8.jsonl), joined by this instance. Start Work Order: `e74844df81f1:docs/hana/info-panels-plan.md:864–883`, in `/home/natepiano/rust/tool-based-ui-demo`. The order requires even margins and readable effect framing but supplies no numeric picture-share target or opposite-margin ratio. Independent evidence: [attempts log](/home/natepiano/.claude/projects/-home-natepiano-rust-hana/memory/attempts-p33-effect-framing.md), numbered attempts 1–4. Its attempt numbers differ from API intervals; October 19 entries concern later work, not this phase 33 record.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | mechanical | 124 | Covered: F001. “fixing the three proxy-cleanup test expectations”; F001: three cleanup expectations fail after camera-path changes. |
| 2 | review-code | spec-gap | 565 | Covered: F002, F003, F005, F006, F007. “fixing the effect-step margin gate and the follow guard”; F002 compares normalized units rather than pixels, F003 assumes 50 ms, F005 returns 0.9 without proving balance. These measured thresholds are absent; F006/F007 concern helpers/constants. |
| 3 | review-code | fix-failed | 629 | Covered: F002, F003, F006. “opposite-margin gate,100ms follow guard, dead helper removal”; F002/F003/F006 still_open after attempt 1, accepted on attempt 2. |
| 4 | review-ux | spec-gap | 562 | Covered: F008. “fixing posterize framing by panning the fit centre”; F008 picture below 15% after zoom-out balancing. Attempts 1–2 record the shrinking picture and share 0.1432. The start order has no 15% target. |
| 5 | gate | mechanical | 92 | Covered: —. “typing six test values for basedpyright”; the corrected join removes the old F008 association; basedpyright determines the gate cause. |
| 6 | review-ux | spec-gap | 602 | Covered: F010. “measuring the beam by its real points”; F010 rotated local Aabb inflates the subject. Attempts entry 3 measures apex/corners and still gives picture share 0.1476. The cited order has no real-beam-corner measurement test; F008 and F009 were already separate findings before F010 (JSONL 573, 576, 584), so this is not evidence that a live check sent only one defect, fixed it, then found the next. |
| 7 | review-ux | record | 0 | Covered: F011, F012. Ledger-only dispatch for F011: 4 m cable-culling margin, and F012: cut Material tool. Attempt 4 identifies the margin. No worker time is observed; this dispatch alone does not prove a completed repair or serial live-check violation. |
| 8 | review-ux | fix-failed | 794 | Covered: F011, F012. “removing the cable culling margin and the cut tool”; F011/F012 worker starts 22 seconds after the ledger dispatch. This is continuation, not proof of another failed patch. |
| 9 | review-ux | fix-failed | 72 | Covered: F012. “choosing a cut-free follow radius in either direction”; F012 still_open. |
| 10 | review-ux | fix-failed | 0 | Covered: F012. Ledger-only F012 dispatch; no paired worker or inferred time. |
| 11 | review-ux | fix-failed | 0 | Covered: F012. Another ledger-only F012 dispatch; no paired worker or inferred time. |
| 12 | review-ux | fix-failed | 151 | Covered: F012. “choosing a cut-free follow radius in either direction”; F012 accepted on fix attempt 3. |

Hard landing: R5 is basedpyright follow-through. R4 and R6–R12 are uncertain: a new picture-area floor and successive beam/cable/Material framing checks do not establish which preceding patch caused each defect. Three post-landing ledger-only intervals have no measured worker time; nine later intervals are not nine independently rejected patches.

## Mixed records: hand boundary audit

These three records contribute 139 measured rounds, kept outside the 15-record root-cause/time table. A mixed flag is a reason to inspect boundaries, not a finding that every round is spurious. Missing starts cannot be reconstructed from current plan numbers alone.

### Mixed 1. Recorded phase 6: palette highlighting and later work

Plan: `rich-text-plan.md`. Recorded title: “Palette rows highlight the matched lette” (report display, truncated to 40 characters). Run `189487ea-6e3b-48f0-896d-c6f40023c0d2`, recorded phase `6`, instance `6cd97cd6-aeb4-4fff-a4a2-5fffd7096870`. **57 rounds; 7 findings; 415,639 fix seconds; recorded past_landing=54.** Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/189487ea-6e3b-48f0-896d-c6f40023c0d2.jsonl), joined by this instance.

Its only phase start is phase 6 on October 12 at 16:16 UTC; there is no later start or finish. Start Work Order: `42a9d691c9310ff7eca3f982c0d2a606e1e20051:docs/hana_diegetic/rich-text-plan.md:211–263`, in `/home/natepiano/rust/widget-enhancements`: palette matches through Rubric, with named tests. Switches, Material layout, scrolling, picking and info icons are outside that order.

Hand split (corrected interval numbers; mixed-purpose intervals stay whole):

| Work identified in the activities | Rounds | Count |
| --- | --- | ---: |
| Phase 6 / palette; R1/R15 also contain controls work | R1, R2, R14, R15, R28, R30 | 6 |
| Shared field/switch/rear-card/Material work; exact phase unresolved | R3–R13, R16–R17, R19–R27, R29 | 23 |
| Retrospective ledger batch without an eligible worker | R18 | 1 |
| Explicit phase 7 and its scroll indicator | R31–R34 | 4 |
| Explicit phase 14, then text bridge and projected-width/scroll-gate work | R35–R39 | 5 |
| Info icon, with some shared slider/drag work | R40–R53, R55, R57 | 16 |
| Merge integration | R54, R56 | 2 |
| **Total** | | **57** |

Current `rich-text-plan.md` calls phase 7 “Panels scroll like macOS, with a fading indicator”; at the start, phase 7 was the palette footer. The later as-built plan places text-hook/size-gate features under phases 15–16 (lines 427–460) and info icons under phase 17 (462–478). Activity names and features are stronger evidence than retroactively applying current numbers. Phase 18's later number-field heading does not assign earlier batches to it. R47–R48 remain shared work. `record` means the recorded phase cannot supply a defensible repair boundary/root, not that later features required no work.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | record | 21237 | Covered: F001, F002, F003. Concurrent activities mix palette port, rich title box, switch thumb and number-field Enter handling; the matched batch covers only F001–F003 (JSONL 8–12): title centering, saved-Tool test expectations and merge conflicts. Later F004–F007 are not attached here. |
| 2 | gate | fix-failed | 4225 | Covered: —. Activity: saved Tool-row click persistence plus Off-switch band tests. R1 already covers saved-tool click expectations in F002; this activity continues that unresolved persistence repair while also running switch-band tests. |
| 3 | gate | mechanical | 2932 | Covered: —. Activity: update five tests for body-label gap. |
| 4 | gate | mechanical | 756 | Covered: —. Activity: switch tests and clippy. |
| 5 | other | record | 1533 | Covered: —. Activity: Unfold rear-label Text gap, outside the palette order. |
| 6 | other | record | 3042 | Covered: —. Activity: change every rear-jack label gap to a projected rule; no separate start. |
| 7 | other | record | 954 | Covered: —. Activity: project the jack's real silhouette; no phase boundary. |
| 8 | gate | mechanical | 694 | Covered: —. Activity: merge-gate clippy follow-through. |
| 9 | other | record | 20444 | Covered: —. Negative-sign commit, switch-knob OIT depth and caret padding share one recorded dispatch. |
| 10 | other | record | 1340 | Covered: —. Activity: make text and number values one field look. |
| 11 | gate | record | 4127 | Covered: —. Activity: one-click text value and opaque-text OIT depth test. One-click fields and text-depth behavior are mixed without an actual feature start. The test mention does not make the substantive behavior change mechanical, and the missing boundary prevents a phase-specific root. |
| 12 | gate | record | 2461 | Covered: —. Activity says six tests the “branch broke”; the causal patch is unidentified. The missing actual feature boundary and unidentified causal patch prevent a phase-specific root; failure of six tests alone does not establish a judgment-free expectation update. |
| 13 | other | record | 2696 | Covered: —. Activity: right-align Square Size, outside palette highlighting. |
| 14 | other | fix-failed | 4076 | Covered: —. “repairing Phase 6 review findings” continues the recorded repair; no new finding IDs identify a different defect. |
| 15 | gate | record | 15932 | Covered: —. Palette Tab/click tests, Material menu, disabled-match dimming and field-height test share one dispatch. The interval mixes substantive palette/Material/field changes under the missing boundary; tests are not its only work and the original palette order cannot supply all these targets. |
| 16 | other | record | 110 | Covered: —. Activity: LFO plate/top-row heights after field changes; missing phase boundary. |
| 17 | other | record | 2172 | Covered: —. Activity: LFO rear-label height and even padding. |
| 18 | review-code | record | 0 | Covered: F004, F005, F006, F007. Ledger batch 2 at JSONL 79 covers F004–F007, opened at 75–78; all four are accepted at 81–84 about 17 seconds later. No fix_pass 2 starts after dispatch. These retrospective dispatch/acceptance entries supply no worker boundary for an additional observed repair; its time is zero. |
| 19 | other | record | 2500 | Covered: —. Activity: Material popup rows take a full spacing step. |
| 20 | other | record | 4914 | Covered: —. Activity: restore Material card text to 9 px after larger row gaps. |
| 21 | gate | mechanical | 2786 | Covered: —. Activity: drawn-gap pixel test for Material fields/popups. |
| 22 | other | record | 1182 | Covered: —. Activity: button paint clipping and renderer coverage contract. |
| 23 | other | record | 23651 | Covered: —. Activity: rear-panel spacing scale and right number inset. |
| 24 | other | record | 451 | Covered: —. Activity: old spacing assertions and side-by-side card top alignment. The assertions and card alignment share an interval with no actual feature start; the substantive alignment correction is not established as a test-only update. |
| 25 | other | record | 7967 | Covered: —. Activity: card top alignment and LFO margins; no phase boundary. |
| 26 | gate | mechanical | 448 | Covered: —. Activity: two test patches in rear_patch_panel split scratch work. |
| 27 | gate | record | 5965 | Covered: —. Activity: spacing commit lint/tests and Material layout. Material layout and gate work are mixed under the missing boundary; the activity does not identify a test-only or conflict-only repair. |
| 28 | other | record | 9143 | Covered: —. Activity: palette refusal message gets its own line; not the start-time match-highlighting goal. |
| 29 | other | record | 5147 | Covered: —. Activity: validation of the spacing unit. Only validation is recorded, with no test update or source repair. |
| 30 | other | record | 3289 | Covered: —. Activity: same-frame modifier ordering for field keys; no separate start. |
| 31 | other | record | 3227 | Covered: —. Activity explicitly says “Phase 7 review findings”, not phase 6. |
| 32 | other | fix-failed | 2815 | Covered: —. Activity: phase 7 indicator/example continuation after R31. |
| 33 | other | record | 5743 | Covered: —. “verifying Phase 7 repairs”. Only verification is recorded, with no test update or source repair. |
| 34 | other | fix-failed | 5256 | Covered: —. Activity: indicator still needs to stay inside its border. |
| 35 | other | record | 4535 | Covered: —. “Phase 14 review fixes”; later plan numbering cannot recover the exact original order. |
| 36 | gate | mechanical | 8255 | Covered: —. Activity: text test-bridge skips. |
| 37 | other | record | 2222 | Covered: —. Plugin order, hand-built text alpha and guard cost match later splash/text-hook as-built work. |
| 38 | other | record | 6066 | Covered: —. Tilt width, per-camera state, scroll areas and threshold order match later size-gate work. |
| 39 | gate | record | 9128 | Covered: —. Activity: tilt test and two assembly-face test failures. The tests fail during later size-gate work, but no actual phase start or diagnosis separates stale expectations from a substantive regression. |
| 40 | other | spec-gap | 10819 | Covered: —. Info-icon review; later phase 17 as-built records first-shot too-small/high icon and tooltip/title overlap. Further visible sizing/placement judgment was needed; exact start target is not recovered. |
| 41 | gate | mechanical | 24 | Covered: —. Activity: tooltip test must require a visible tooltip. |
| 42 | gate | fix-failed | 5473 | Covered: —. Activity: same visible-tooltip test still being repaired after R41. |
| 43 | other | fix-failed | 4371 | Covered: —. Activity: world-root fallback repair/removal continues tooltip placement. |
| 44 | other | record | 4840 | Covered: —. Activity: named activation types. Choosing activation types is a code/type-design change, not identified lint or a test update. Its original feature boundary is absent. |
| 45 | gate | fix-failed | 17203 | Covered: —. Info-icon size/centre/tooltip placement continues, with three slider test failures. |
| 46 | gate | fix-failed | 7536 | Covered: —. Activity: thumb bounds/helper control kind and tooltip clearance below card text. R45 still repairs info-icon/tooltip placement; R46 continues its unresolved clearance and bounds work. The test/helper part does not make the whole interval mechanical. |
| 47 | other | record | 6752 | Covered: —. Scale-mode jack-panel refit and value-drag states share a dispatch; exact phase unresolved. |
| 48 | other | record | 1917 | Covered: —. Floating-point noise causes card rebuilds; phase ownership unresolved. |
| 49 | other | fix-failed | 10273 | Covered: —. Info-button size and tooltip fallback continue the same placement goal. |
| 50 | other | fix-failed | 989 | Covered: —. Activity: tooltip depth still needs repair. |
| 51 | other | fix-failed | 19309 | Covered: —. Activity: fallback tooltip gap still needs repair. |
| 52 | other | fix-failed | 22151 | Covered: —. Activity: tooltip gap still fails in tilted views. |
| 53 | gate | fix-failed | 50458 | Covered: —. Info-button hover after camera movement and test follow-through; no independently identified new goal. R49–R52 continue the unresolved info-tooltip placement/depth goal; this interval still repairs hover after camera movement, as well as tests. Continuity is inferred from the activities, not a separately recorded failed patch. |
| 54 | merge | mechanical | 14369 | Covered: —. Activity: six catalyst merge conflicts. |
| 55 | gate | mechanical | 35655 | Covered: —. Info-button work against catalyst merge plus failing Mac slider-fill test. |
| 56 | merge | mechanical | 79 | Covered: —. Activity: merge-blocker patch. |
| 57 | other | record | 0 | Covered: —. Overview click hits a rear jack; unfinished start, no observed finish/time. |

Landing check: The recorded past_landing=54 is applied across missing phase boundaries, not 54 extra palette repairs. Gate/merge rows name tests or integration that can qualify if phase-caused. The long info-icon tail is real, but no reliable actual-phase landing ordinal can be reconstructed. All recorded R4–R57 landing positions are therefore marked record in the cross-case check.

### Mixed 2. Recorded phase 20: one long tile phase with an added goal

Plan: `remove-running-section-tile-fixes.md`. Recorded title: “Each reading in one place, and every mar” (report display, truncated to 40 characters). Run `03f9b80e-dbe5-47c6-b29e-a7e6587ab721`, recorded phase `20`, instance `10553e9d-0fcd-4056-ae4b-f7bf9de51a05`. **42 rounds; 75 findings; 99,706 fix seconds; recorded past_landing=39.** Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/03f9b80e-dbe5-47c6-b29e-a7e6587ab721.jsonl), joined by this instance.

Start Work Order: `32b481f6d803a73c2e2b79c96e0c534d0fdd464b:docs/cargo-port/remove-running-section-tile-fixes.md:619–674`, in `/home/natepiano/rust/cargo-liner-tile`. Independent source: [production log](/home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-log.md); row references below are its lines.

The boundary audit does **not** support inventing several named phases here. Phase 19 finishes before phase 20 starts (JSONL 1332–1333, October 13 at 18:48 UTC); phase 20 finishes at 1747 and phase 21 starts at 1750. No activity names another phase number. The mixed flag comes from more than 72 hours of passes. Logical groups, not recovered phase boundaries, are:

| Work group | Rounds | Count |
| --- | --- | ---: |
| Initial reading/mark repairs | R1–R5 | 5 |
| External cargo-mend repair and toast test | R6 | 1 |
| Successive row/column checks | R7–R13 | 7 |
| Grid freeze with added auto-view removal | R14–R22 | 9 |
| Column/gap/readout regressions | R23–R42 | 20 |
| **Total** | | **42** |

The start order already has 15 rules and tests. It explicitly follows phase 19 under hard landing: earlier design findings that were not regressions were moved here. This tail is not explained by an absent visual goal. Whole-dispatch roots below do not divide mixed work/time without evidence.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 4601 | Covered: F001, F002, F003, F004, F005, F006, F007, F008, F009. “phase 20 census findings” / “drawing findings”; F001 shim PID/chdir/flags, F006 descendant count, F007 summary minimum and F008 shared PID expose census/fixture cases beyond the pictured examples, alongside direct rule misses. |
| 2 | review-ux | fix-failed | 4123 | Covered: F001, F007, F010, F011, F012, F013, F014, F015, F016, F017, F018, F019, F020, F021. F001/F007 remain; F010 reserved cut-mark cells, F011 reader fixture, F017/F019 helper pipeline and F021 narrow command join the batch. |
| 3 | review-ux | fix-regressed | 5936 | Covered: F010, F011, F017, F019, F021, F022, F023, F024, F025, F026, F027, F028, F032, F033. F022 six helper tests fail, F023 mend rewrite does not resolve, F024–F028 draw/layout/test regressions. F032 is an older 214×93 moving-pane buffer panic and F033 a lost command tail. Log 742–744 deliberately adds the old panic to avoid a same-file collision; that part is out of scope. |
| 4 | review-ux | fix-regressed | 4029 | Covered: F035, F036, F037. F035 double synchronous demand/requeue, F036 extra passes in every crate, F037 cut subcommand follow the changing draw/measurement path. |
| 5 | review-ux | spec-gap | 3343 | Covered: F038, F039, F040. Next checked build sends F038 slow toast test, F039 readable directory heading cut and F040 glued argument mark together. The start order gives account/cut-mark rules, but no minimum retained directory-heading length; F039 needs that acceptance choice. This mixed-batch primary gap assessment is not evidence of one defect per live check. |
| 6 | review-code | out-of-scope | 1869 | Covered: F038. “fixing cargo-mend's crate:: rewrite under #[path] mounts” plus “repairing the toast-width motion test” (F038). External-tool time is at most this whole interval, not an external-only measurement. |
| 7 | review-ux | spec-gap | 5392 | Covered: F041. Next check sends F041: two child rows repeat the same 83.7% reading (JSONL 1479); activity “reading two rows / full lint”. Start item 15 names the parent/one-child rendering test, not a shared-ownership census fixture for two child rows. That missing test case supports the gap assessment. F041 is still_open at 1485 when F042/F043 are added, so it does not establish a fixed-one-then-next sequence. |
| 8 | review-ux | fix-failed | 2903 | Covered: F041, F042, F043. Undo row-reading change; F041 persists with F042 blocked memory offset and F043 redraw taking 17 frames. |
| 9 | review-ux | spec-gap | 2299 | Covered: F044, F045, F046. Next audit sends F044 ownership, F045 freeze ignores q and F046 lost subcommand together. The cited start order has drawing/census examples but no bounded grid-iteration or quit-responsiveness test. F045 is the missing operational acceptance case; three new IDs alone do not prove a prior patch caused all three or establish serial-check. |
| 10 | review-ux | spec-gap | 4682 | Covered: F047, F048, F049. Nested-reading/state-column check sends F047 ordinary-frame budget, F048 duplicate child reading and F049 long-command memory drop together. Start item 15 names a parent/one-child value test, but no render-cost bound or nested-long-command fixture. The missing budget/combined case supports the primary gap assessment, rather than one-defect dispatches. |
| 11 | review-ux | spec-gap | 2464 | Covered: F050, F051. Frame-budget check sends F050 missing memory and F051 state cut while parent remains. The cited order says empty state must not displace a valued column, but gives no priority between a filled state and a parent column in this constrained layout. F051 exposes that missing choice; the two findings were batched. |
| 12 | review-ux | fix-failed | 1081 | Covered: F051. “drop parent where state”; F051 remains covered. |
| 13 | review-ux | spec-gap | 4666 | Covered: F052, F053. Shared-column fit sends F052 unmarked argument cut and F053 blank state dropping valued columns together. The start order preserves a frame-wide grid and also requires removing an empty slot locally; it does not name an allocation test combining those two tables with argument truncation. The missing combined test is the primary gap assessment; the individual mark/value rules were explicit. |
| 14 | review-ux | target-moved | 3461 | Covered: F052, F053, F054, F055. F054 branch-only grid freeze plus F055 “drop the auto tile view (user)” (JSONL 1541–1542). Log 1172 adds auto-view removal, absent from the start order. |
| 15 | review-ux | fix-failed | 14369 | Covered: F054, F055. “measuring each cell lazily, once per height, and finishing the auto removal”; F054/F055 remain. |
| 16 | gate | mechanical | 128 | Covered: —. “finishing round 15 on the new account: two tests back under their round-13 time, then the gates”; same batch, not a fresh live finding. Log 1262 records account move. |
| 17 | gate | fix-failed | 941 | Covered: —. Remove extra measurement cost against round 13; F054/F055 remain. |
| 18 | gate | record | 115 | Covered: —. Finish measured grid-speed counts and hand back test commands. The activity reports measurements and hands back commands; it identifies no code repair or test update. |
| 19 | gate | record | 86 | Covered: —. Read Mac test times/request gates; same findings, no independent repair evidence. This is reading results/requesting gates, not an identified source repair or test update. |
| 20 | gate | mechanical | 2214 | Covered: —. Cargo-handler changelog, lint and four suites, still F054/F055. Log 1396/1419: timing and gates pass. |
| 21 | review-ux | fix-failed | 1972 | Covered: F054. “repairing the endless row-sharing loop (F054)”; re-shoot freezes after green gates. Log 1422 identifies outer loop and lost round-13 termination check. |
| 22 | gate | mechanical | 1512 | Covered: —. Two drawing tests/gates for F054; log 1441 reports 27/27 freeze checks fixed. |
| 23 | review-ux | fix-regressed | 1984 | Covered: F058, F059. F058 command cut while optional columns remain; F059 shifted runs column. Log 1441 explicitly calls both regressions on the repaired build. |
| 24 | review-ux | spec-gap | 1380 | Covered: F060, F061. Next column check sends F060 stateless parent dropped and F061 Long memory drop together. The cited order supplies empty-state and individual cut rules but no priority/test for stateless ancestry versus valued Long columns under the shared grid. This is that missing combination case, not proof of one defect per check. |
| 25 | other | fix-failed | 2314 | Covered: —. One-place column drop order; F060/F061 remain. |
| 26 | review-ux | fix-regressed | 4444 | Covered: F062, F063, F064. F062 blocked state cut, F063 width-48 progress lost, F064 blank state slot. Log 1463: example order read as filled-state column; ten tests changed to match. Restore main's order/tests. |
| 27 | review-ux | spec-gap | 1846 | Covered: F065. Main comparison finds F065 narrow tables drop duration/runs that main keeps (log 1570). The start order preserves Phase 14 optional columns, but names no main-versus-branch narrow-table duration/runs fixture; that missing compatibility test is the primary gap assessment. The next single-finding sequence is documented in R29. |
| 28 | other | fix-failed | 220 | Covered: —. Follow-through on F065. |
| 29 | review-ux | serial-check | 851 | Covered: F066. Log 1636 and its 11:41 EDT entry record the repaired live re-shoot followed by the width-40 progress/location failure. JSONL 1637/1639 sends only F065; workers repair it at 1641–1645 and 1646 accepts it. Only then 1647 opens F066 and 1648 sends only F066. These are the named single-finding repair/check steps supporting this label; the two-finding batches elsewhere are not counted as serial-check. |
| 30 | other | fix-failed | 376 | Covered: —. F066 continues. |
| 31 | review-ux | spec-gap | 1125 | Covered: F067, F068. Next check sends F067 two frame gap widths and F068 adjacent Long cut marks together. The start order preserves frame-wide offsets but does not settle whether spacing is uniform per table or per whole frame. The later explicit per-table ruling at log 1679/R34 establishes the missing interpretation; this two-finding check is not serial-check. |
| 32 | live | mechanical | 425 | Covered: —. Two explicitly approved one-mark expectations for F067/F068. |
| 33 | review-ux | fix-failed | 1039 | Covered: F067, F068. Main-gap/Long glued-mark repair continues F067/F068. |
| 34 | other | target-moved | 1049 | Covered: —. “main's per-table gap rule”; log 1679 ruling C: do not force tight gaps across state frames; mixed gaps correct where main mixes. Showrunner clarification revises the preceding interpretation, not a new user request. |
| 35 | other | fix-failed | 707 | Covered: —. Cells one gap, summary its own gap; F067/F068 continue. |
| 36 | review-ux | fix-failed | 600 | Covered: F068, F069, F070. F068 remains with F069 blank row and F070 CI runs column missing. |
| 37 | gate | fix-failed | 1031 | Covered: —. Finish F070/gates; F068–F070 still covered. |
| 38 | review-ux | fix-failed | 2484 | Covered: F070, F071. F070 runs remains; F071 width-40 blank summary row drops main's location. |
| 39 | review-ux | spec-gap | 1344 | Covered: F072. Main comparison finds F072 blank CPU where main has a reading (JSONL 1707). Start item 12 tests an unavailable reading as blank, but names no paired test that preserves an available CPU value under this column/width combination. That missing compatibility case is the primary gap assessment, not proof that the preceding CI-runs patch introduced the defect. |
| 40 | review-ux | fix-regressed | 1646 | Covered: F073. JSONL 1707/1709 sends only F072; 1710–1711 repairs it and 1716 accepts it. A fresh shot review at 1718–1719 then reports one regression plus two defects shared with main. F073 cutfoot subcommand loss alone is sent to this phase at 1720–1721; the shared defects are routed to phase 25 (log 2054). Log 2054 explicitly calls F073 a phase-20 regression, supporting this root. The three-defect check does not establish the one-defect serial-check pattern. |
| 41 | review-code | fix-failed | 1451 | Covered: F074, F075. F074/F075 missing CPU readings graded with F072, continuing CPU issue. |
| 42 | other | fix-failed | 1204 | Covered: —. “round 28 follow-up restore blank CPU”; F074/F075 remain. |

Landing check: This is a true long repair tail, not a missing-boundary explanation. Branch-only freezes, failed gates and explicit regressions can stay. F054 survived green tests (log 1422); F058/F059 are explicit new regressions (1441). The old buffer panic in R3, cargo-mend in R6, and user-added auto-view removal in R14 are scope/goal changes, not phase-caused regressions. Main's other defects were routed to phase 25 (2054/2120). The check below preserves uncertain mixed batches instead of treating their entire time as eligible.

### Mixed 3. Recorded phase 38: Beam and later features

Plan: `hana-organon-design.md`. Recorded title: “Show: a Beam switch with a jack” (report display, truncated to 40 characters). Run `8f4c2e88-92c3-44c4-ac27-36ab13c68327`, recorded phase `38`, instance `6253f990-0b9b-4402-ba88-a4d32efa5a56`. **40 rounds; 9 findings; 192,623 fix seconds; recorded past_landing=37.** Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/8f4c2e88-92c3-44c4-ac27-36ab13c68327.jsonl), joined by this instance.

Phase 38 starts October 8 at 06:17 UTC (JSONL 2), finishes October 12 at 08:12 (553), then C24 starts at 08:50 (554). There are no phase 39–46 starts between them. Start Work Order: `9e143d3a56b84cb09734e3471bbe361e4d2571c1:docs/hana/hana-organon-design.md:2211–2241`, in `/home/natepiano/rust/tool-based-ui-geometry-material`: one Beam setting/predicate and three tests, including old-scene default-on loading.

Hand split, checked against the later as-built plan (lines 2001–2145):

| Work identified | Original rounds | Count |
| --- | --- | ---: |
| Phase 38 Beam / old-scene loading | R1, R3 | 2 |
| Phase 37 Material output-row carry-over | R2 | 1 |
| Phase 39 shared panel alpha | R4 | 1 |
| Phase 42 independent effect bodies | R5–R7 | 3 |
| Phase 44 Off dimming | R8–R9 | 2 |
| Phase 41 type/title/help scale and Screen fallback | R10–R25 | 16 |
| Card/case fit, consistent with phase 43 but not numbered | R26–R28 | 3 |
| Shared value width/rear-card placement; boundary unresolved | R30–R33 | 4 |
| Phase 45 one rear-panel reconcile | R34–R36 | 3 |
| Look selection before C24 starts | R38 | 1 |
| Phase 46 top jack panel | R39–R40 | 2 |
| Merge integration | R29, R37 | 2 |
| **Total** | | **40** |

**Corrected ledger association:** F001–F006 open October 12 at 03:31 (JSONL 509–514), with batch 1 at 03:31:57 (517). F007–F009 open at 05:36–05:39, with batch 2 at 05:39:38 (536–540). The corrected join assigns these only to R39 and R40 respectively. R1/R2/R10/R22/R38 now have no covered findings and use activity causes (other/other/gate/gate/other). This removes the previous future-finding associations while preserving all 40 intervals and their agent time. Missing actual-feature starts remain a separate boundary limitation.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | other | record | 2448 | Covered: —. “convert shipped scene” to Beam input; old-scene compatibility is explicit. Attached top-panel findings did not exist yet. The phase-38 order already requires converting the shipped scene. The activity records that planned conversion under a repair label, not lint, a test update or merge work. |
| 2 | other | out-of-scope | 1038 | Covered: —. Fit Material output row under title, matching phase 37 as-built output ordering. Attached F007–F009 did not exist yet. The hand split places this under phase 37 Material-output carry-over, predating the recorded phase-38 Beam order. This is substantive row fitting, not a test update. |
| 3 | other | fix-failed | 1266 | Covered: —. False Beam error on scene load persists after R1 conversion. |
| 4 | other | record | 607 | Covered: —. Activity: screenshots of the heaviest see-through panels, matching phase 39 alpha. It records a visual check, not a repair, and has no phase-39 start or single-defect/fix/next-defect sequence. |
| 5 | other | record | 1153 | Covered: —. Lone effect-tool pick path matches phase 42, outside Beam; original feature boundary/root absent. |
| 6 | other | fix-failed | 10495 | Covered: —. Extend same repair to every standalone effect body. |
| 7 | other | fix-failed | 1975 | Covered: —. Case registration for loaded effects continues effect-body repair. |
| 8 | other | record | 4477 | Covered: —. Off dimming must affect its own tree: phase 44 under the phase-38 record. |
| 9 | gate | record | 4959 | Covered: —. Darker/solid Off appearance and green tests. Off appearance is substantive phase-44 work with no phase-44 start here. Green tests do not turn its color decision into mechanical work. |
| 10 | gate | record | 21400 | Covered: —. Concurrent title lint, stale phase 41 tests, title-padding test and drawn-pixel failures. Later top-panel findings are not evidence here. This interval mixes actual title layout/pixel repairs with lint and stale tests, without a phase-41 start. The missing boundary prevents assigning a phase-specific root to the whole interval. |
| 11 | other | fix-failed | 5017 | Covered: —. Phase 41 drawn-pixel failure persists. |
| 12 | other | fix-failed | 2581 | Covered: —. “Phase 41 repairs” continue. |
| 13 | other | fix-failed | 577 | Covered: —. Diagnose title-bar suite failure. |
| 14 | gate | fix-failed | 5304 | Covered: —. Title-bar layout failures and test fixtures. R11–R13 already continue the failing title-bar suite; layout failures remain open here while fixtures are also updated. |
| 15 | other | fix-failed | 4730 | Covered: —. Title-bar regressions still being repaired. |
| 16 | other | fix-failed | 744 | Covered: —. Same title-bar regression activity continues. |
| 17 | merge | mechanical | 2674 | Covered: —. Land title-bar change on merge branch. |
| 18 | other | fix-failed | 332 | Covered: —. Phase 41 repair on shipped title bar continues. |
| 19 | gate | mechanical | 689 | Covered: —. Activity concerns getting Phase 41 tests to compile after the changed title/plugin setup; classified as a test update, without a new visible target. |
| 20 | other | fix-failed | 12147 | Covered: —. Phase 41 jack-panel/geometry failures persist. |
| 21 | other | record | 1719 | Covered: —. Add Screen view misses Screen; later phase-41 as-built describes camera search/fallback. No phase-41 start or single-defect live-check/fix/next-defect trace exists here, so its original repair boundary/root is not recoverable. |
| 22 | gate | record | 17086 | Covered: —. Portal deletion test under load, Screen pose before framing, tall new Look and Add Screen search share dispatch. Attached findings are from two days later. Substantive Screen placement/search changes share this interval without the actual feature start; the test is only one part of the work. |
| 23 | other | fix-failed | 1275 | Covered: —. Screen pocket closed view continues camera repair. |
| 24 | other | fix-failed | 1386 | Covered: —. Ruled camera fallback continues. |
| 25 | other | fix-failed | 657 | Covered: —. Last-resort camera fallback still needs repair. |
| 26 | other | record | 3639 | Covered: —. Camera/pixel plus card-layout/caption failures resemble phase 43; batch boundary absent. |
| 27 | other | fix-failed | 5766 | Covered: —. Same camera/card failures continue. |
| 28 | other | fix-failed | 1366 | Covered: —. Three card-fit failures persist. |
| 29 | merge | mechanical | 4927 | Covered: —. Catalyst merge. |
| 30 | other | record | 518 | Covered: —. Text measurement through public layout engine; shared work without a start. |
| 31 | other | record | 644 | Covered: —. One width for all value fields; exact phase boundary absent. |
| 32 | merge | record | 22690 | Covered: —. Value width, rear-card clearance of info text and branch merge share dispatch. A merge shares this interval with substantive value-width/clearance work. Its feature boundary is unresolved, so the entire time cannot be called judgment-free merge work. |
| 33 | gate | record | 10320 | Covered: —. Merge gates, full rear-card suite and commit. Only gate execution, a suite run and a commit are identified; no merge conflict repair or test update is named. |
| 34 | other | record | 222 | Covered: —. Activity says “Phase 45 as-built”: documentation/record work, not Beam repair. |
| 35 | other | fix-failed | 296 | Covered: —. Extra rear-panel visits remain, matching phase 45 one-reconcile goal. |
| 36 | gate | mechanical | 1221 | Covered: —. Rear-visit tests must wait for icon font. |
| 37 | merge | record | 5075 | Covered: —. Catalyst merge and seven-character value width. The seven-character field-width decision shares a merge interval without the actual feature boundary; it is not identified as a judgment-free conflict resolution. |
| 38 | other | record | 167 | Covered: —. Look-selection box only on jack panel; C24 starts later. Top-panel F001–F006 were not open yet. |
| 39 | review-ux | record | 11703 | Covered: F001, F002, F003, F004, F005, F006. F001–F006 are sent together for top-panel design defects, followed by build/six shots and cable-plug tests. The phase-46 start order is absent from this record. Neither a phase-specific root nor a one-defect live-check sequence can be recovered from this batch. |
| 40 | review-ux | record | 17333 | Covered: F007, F008, F009. The next top-panel batch sends F007 plate hugs rows, F008 unequal bottom space and F009 white rings together; tests/build/shots follow. With no phase-46 start or source-level before/after, neither a phase-specific root nor a one-defect live-check sequence is established. |

Landing check: The 37 late intervals are not Beam's repair tail. The hand split finds two Beam intervals and later feature/integration work. Top panels have two observed ledger batches; missing starts prevent trustworthy actual-phase ordinals for phases 41–46. All original R4–R40 landing positions are marked record in the cross-case check. Named test/merge/regression work is conditionally eligible; visual provenance still needs source evidence beyond the now-corrected ledger associations.

## Rule check

[ProductionUnit item 7](../production_format.md) keeps only phase-caused regressions, broken working behavior, or test/gate failures from the third repair round onward. The API's `past_landing` counts R4 onward; R3 may still contain kept findings. This check uses API interval numbers, which need not equal ledger numbers. Gate runs and integration intervals can therefore add measured rounds without a new retained finding.

“Supported” below means the evidence names a regression, a broken behavior or a failing test/gate. It does not mean the repair succeeded. “Outside category” means the row concerns a new target or other work without that evidence; a later explicit user/showrunner ruling can supersede the earlier target, so it is not automatically a rule violation. “Uncertain” means the records do not establish provenance or eligibility. “Record” means the row is not evidence of a completed source repair. A repeated open finding alone does not prove it meets the hard-landing category.

| Rank / run / phase | Past landing | Supported R4+ | Outside category R4+ | Uncertain R4+ | Record R4+ |
| --- | ---: | --- | --- | --- | --- |
| 1 / f205ddd0 / 33b | 25 | R10, R11, R19, R21, R22, R23, R24, R26, R28 | R7, R8, R15, R16, R18, R27 | R4, R5, R9, R13, R14, R17 | R6, R12, R20, R25 |
| 2 / 03f9b80e / 22 | 24 | R4, R5, R6, R7, R9, R10, R11, R12, R13, R14, R15, R16, R17, R18, R19, R20, R21, R22, R23, R24, R25, R26 | — | — | R8, R27 |
| 3 / 24e20a41 / 17 | 22 | R4, R8, R15, R20, R24, R25 | R5, R6, R9, R10, R11, R12, R13, R14, R16, R17, R18 | R7, R19, R21, R22, R23 | — |
| 4 / 3c2be97d / 17 | 18 | R4, R9, R18, R19, R20, R21 | — | — | R5, R6, R7, R8, R10, R11, R12, R13, R14, R15, R16, R17 |
| 5 / 1a1f39e6 / 6 | 15 | R7, R10, R13, R14, R15, R16, R17, R18 | — | R5, R9, R11 | R4, R6, R8, R12 |
| 6 / f93f565a / 31 | 17 | R4, R5, R6, R7, R9, R10, R12, R13, R14, R15, R17, R18, R19, R20 | — | R8, R11 | R16 |
| 7 / 1a1f39e6 / 4 | 13 | R5, R6, R8, R9, R10, R13, R15, R16 | — | R4, R7, R11, R14 | R12 |
| 8 / e54c80d4 / 58 | 12 | R8, R9, R11, R12, R14 | — | R4, R5, R6, R7, R10, R15 | R13 |
| 9 / a702c130 / 15 | 11 | R4, R5, R6, R7, R8, R9, R10, R11, R12, R13, R14 | — | — | — |
| 10 / 8777d55e / 10 | 11 | R4, R5, R6, R7, R8, R9, R10, R11, R12, R13 | — | R14 | — |
| 11 / 8777d55e / 7 | 10 | R4, R5 | — | R6, R7, R8, R9, R10, R11, R12, R13 | — |
| 12 / df477c65 / 22 | 9 | R4, R7, R8 | — | R5, R6, R9, R10, R12 | R11 |
| 13 / 7b115092 / 137 | 10 | R11 | — | R4, R5, R6, R7, R8, R10, R12, R13 | R9 |
| 14 / 576fc590 / 111 | 9 | R4 | — | R6, R7, R8, R9, R10, R11, R12 | R5 |
| 15 / f205ddd0 / 33 | 9 | R5 | — | R4, R6, R8, R9, R10, R11, R12 | R7 |

The effect-framing case (rank 1) includes six post-landing target rulings without an independently named kept-category defect. The attempts log supports “target changed”; it does not support calling those changes repeated failures against one fixed contract. Rank 2's cell-lifecycle regressions and failing assertions remain eligible even after many rounds; this is evidence that the rule supplies no numeric stop for such retained defects. Rank 3 contains pilot recall and instruction changes beyond the retained categories, but the Work Order itself authorized pilot fixes. That is a tension between a broad phase order and the landing rule, not evidence that the worker silently expanded its order.

Rank 4 started September 13, before the October 1 hard-landing rule. Its 18 post-landing intervals are a retrospective comparison only. Pre-existing Mac and desktop defects were explicitly carried by its order; this conflicts with the later own-change-only packaging rule, but is not evidence that a September worker ignored an October rule. Rank 5's verification-only intervals and rank 2's ledger-only/unfinished intervals also show why a numeric tail is not by itself a noncompliance count. Rank 6's worker tail names grid regressions and gates that the rule permits to stay; its separate R8/R11 ledger-only continuations do not establish a completed kept-category repair. Rank 7 lacks finding provenance for several fit requests. Rank 8 lacks a timestamp-exact saved order, so its apparent scope expansion stays uncertain.

The same test applied to the mixed records gives a different kind of result:

| Mixed record | Recorded past landing | Supported | Outside original category | Uncertain / mixed work | Unusable actual-phase ordinal | Verification only |
| --- | ---: | --- | --- | --- | --- | --- |
| 189487ea / 6 | 54 | Conditional tests/merges in its section | — | Actual feature ownership unresolved | R4–R57 | Included in unrecoverable ordinals |
| 03f9b80e / 20 | 39 | R4, R21–R23, R26–R28, R32, R39–R42 | R34: new showrunner gap clarification | R5–R17, R20, R24–R25, R29–R31, R33, R35–R38 | — | R18–R19 |
| 8f4c2e88 / 38 | 37 | Conditional tests/merges in its section | — | Actual feature boundaries unresolved | R4–R40 | Included in unrecoverable ordinals |

For tile phase 20, R6 combines an outside cargo-mend repair with an eligible toast test. R14–R20 combine the branch-only freeze/performance/gates with added auto-view removal. R36–R38 combine lost CI runs with less certain row/mark work. These intervals cannot be assigned one eligible/ineligible time split. R34 revises a showrunner interpretation; it is outside the original kept-finding categories, but is an explicit later ruling rather than an unreported worker decision. Missing boundaries in the other two mixed records prevent a numeric claim about actual-phase hard landing.

## Totals

### Primary roots in the 15 non-mixed records

The 260 round rows above sum to **975,069 agent-seconds (270.85 hours)**. The three mixed records and the Astra reference are excluded. Each row's entire time is allocated to one primary root. Counts are sorted by rounds, then time; percentages are rounded independently.

| Primary root | Rounds | Share of rounds | Fix seconds | Fix hours | Share of fix time |
| --- | ---: | ---: | ---: | ---: | ---: |
| fix-failed | 94 | 36.2% | 473,015 | 131.39 | 48.5% |
| spec-gap | 43 | 16.5% | 186,473 | 51.80 | 19.1% |
| mechanical | 39 | 15.0% | 107,662 | 29.91 | 11.0% |
| record | 35 | 13.5% | 53,169 | 14.77 | 5.5% |
| fix-regressed | 30 | 11.5% | 78,532 | 21.81 | 8.1% |
| out-of-scope | 12 | 4.6% | 66,479 | 18.47 | 6.8% |
| target-moved | 7 | 2.7% | 9,739 | 2.71 | 1.0% |
| serial-check | 0 | 0.0% | 0 | 0.00 | 0.0% |
| **Total** | **260** | **100%** | **975,069** | **270.85** | **100%** |

The 94 `fix-failed` intervals include interrupted continuations and failed gate batches; they are not independently rejected patches. Mechanical covers only the stated lint, test-update and merge category. Pure verification and planned rollout rows are record rows. Spec-gap allocations name missing measurement targets or test cases in the individual Work Orders; they remain causal assessments, not proof that an omitted case caused each code mistake.

No normal-record row establishes the required one-defect live-check → fixed → next-defect sequence. All ten former serial-check rows had batched findings or lacked that trace. Zero qualifying rows is a limit of this evidence, not proof that every live check was complete. Mixed tile phase 20 R29 retains serial-check with its explicit single-finding dispatch, acceptance and next-check sequence; it remains outside this table.

Whole-batch allocation particularly limits rank 5 R1 (17,373 seconds of mode/switch work plus 17,272 of stage-drag work) and rank 6 R1 (5,160 test seconds plus 8,563 grid-code seconds): their spec-gap times are upper bounds on the parts tied to the missing tests. Rank 14 R11 allocates 14,637 seconds to outside-scope screwdriver appearance although F014 also repairs the in-scope glyph. Rank 13 R10 likewise allocates 6,263 seconds to outside-scope work although the chooser shot is in scope. These upper bounds and the section uncertainties apply to the time shares.

### Phase 1 causes across the full 60-day window

Corrected capture on **2026-10-20 at 18:51 EDT**: 1,870 phases, 226 phases with at least 3 rounds (12.1%) holding 79.5% of fix time, and 39 mixed records. It contains **2,287 rounds and 7,590,744 agent-seconds**. The fixed cohort remains the 15 records selected in Phase 2; it is not reranked after repair. This full-window table includes mixed and zero-round records, for scale rather than root extrapolation.

Source: the specified JSON report command, with per-cause time sums from the same shared `_events` / `load_phases` start-window/status selection. These sums reconcile with the report metadata. A same-source comparison before and after the join repair gives 2,263 → 2,287 rounds (+24 ledger-only intervals), with identical 7,590,744 agent-seconds. Earlier dated totals also differ through live-record growth; that growth is not attributed to the join repair.

| Phase 1 cause | Rounds | Share of rounds | Fix seconds | Fix hours | Share of fix time |
| --- | ---: | ---: | ---: | ---: | ---: |
| review-code | 1,270 | 55.5% | 2,841,924 | 789.42 | 37.4% |
| other | 357 | 15.6% | 1,508,411 | 419.00 | 19.9% |
| review-ux | 351 | 15.3% | 1,623,091 | 450.86 | 21.4% |
| gate | 255 | 11.1% | 1,286,094 | 357.25 | 16.9% |
| merge | 43 | 1.9% | 311,862 | 86.63 | 4.1% |
| live | 11 | 0.5% | 19,362 | 5.38 | 0.3% |
| **Total** | **2,287** | **100%** | **7,590,744** | **2108.54** | **100%** |

### Dispatch-join correction and constraints for Phase 4

The corrected join leaves every worker interval and its agent time unchanged. The fixed cohort's added rows are rank 6 R3/R8/R11 (ledger batches 2/3/4), rank 10 R3 (batch 1), and rank 13 R12 (batch 3). Each table row cites the dispatch, its timing/number mismatch and its root evidence. Mixed record 1 also adds R18 (batch 2); its later intervals are renumbered. The cohort is still the same 15 phase instances selected in Phase 2, so its headings preserve the original ranks rather than imply a fresh ranking.

Hand roots otherwise remain as in Phase 2. The changed evidence moves rank 6's old R9 → new R12 from fix-regressed to fix-failed: F008 is still open, while F009 has not yet been recorded. Old R10 → new R13 instead receives fix-regressed for the newly recorded F009 beading dated to the preceding edge-band work. Rank 14 R1 changes spec-gap to mechanical because it precedes the repeated-Arrange findings and its own activity names lint/halo-test work. Rank 6 R2 keeps its regression root but now cites the preceding width correction F004 rather than future F007/F008. The new zero-time rows receive roots from their cited record; no worker time is reassigned to them.

Same-source cause changes for downstream API consumers are:

| Cause | Before join repair | After join repair | Change |
| --- | ---: | ---: | ---: |
| review-code | 1,398 | 1,270 | -128 |
| review-ux | 427 | 351 | -76 |
| gate | 164 | 255 | +91 |
| other | 226 | 357 | +131 |
| live | 6 | 11 | +5 |
| merge | 42 | 43 | +1 |

The full-window count increases by 24 ledger-only intervals across 16 phase instances. The study's 15 normal records increase by five and its three mixed records by one; the remaining 18 additions are outside those sections. Phase 38 of run 8f4c2e88 has 40 intervals throughout, with batch 1 attached only to R39 and batch 2 only to R40. Phase 137 of run 7b115092 now has 13 intervals because batch 3 stays ledger-only. These corrections change causes and counts; phase boundaries, measured fix time, API signatures and fields stay unchanged.

The replacement facts for the Phase 4 Work Order's copied constraints are: **15 fixed non-mixed records; 260 rounds; 975,069 agent-seconds (270.85 h)**. Roots by rounds (share of fix time): **fix-failed 94 (48.5%), spec-gap 43 (19.1%), mechanical 39 (11.0%), record 35 (5.5%), fix-regressed 30 (8.1%), out-of-scope 12 (6.8%), target-moved 7 (1.0%), serial-check 0**. These supersede the earlier 255-round counts.

### Cause-word correction and verification

Phase 2's word-list tuning changed only `LIVE_WORDS`: it now also matches `ux review`. Evidence is run `fb5b2c96`, phase 202, instance `8013edcb-6bbb-4771-804d-fc922f3d5dc4`: JSONL line 980 finishes “UX review”, text “CPU pane four states at width 19”, result “2 defects”; line 981 starts “repairing phase202 awaiting row” 59 seconds later; line 982 finishes it. No ledger matches that repair. The change moves exactly that R2, **one round and 2,709 agent-seconds**, from other to live. It changes no sampled record count or cause. Adding screenshot alone matched no further case and was not retained. No API signature or field changed.

The synthetic regression test covers that completed-review/next-repair sequence. Run-record line 982 has `pass_elapsed_seconds=2709`; the API value and the tuning example agree. Phase 2 code checks, before the temporal join repair: 31 unit tests passed; basedpyright reported 0 errors and 0 warnings (its exit 3 is expected here). The second writer's final merge gate passed with history included. Phase 3 repairs the temporal ledger join described in mixed record 3; the word lists and public API fields stay unchanged.

## Reference case outside the run records: Astra design reviews

Source: [experiments-unit design notes](/home/natepiano/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md), sections (b), (c) and (d), dated 2026-10-20. This is cargo-liner's experiment-framework **design**, not code repair. It is excluded from both the 260-round sample and the 60-day run-record cause totals.

| Review / revision | Findings fixed | Root assessment | Evidence |
| --- | ---: | --- | --- |
| 1 | 8 of 8 | target-moved (design scope context) | Tooling item 13 was added from the user during the first draft at 16:45 EDT. The first review found phase-time/stage-time confusion, thread lifetime and replay keys, unsupported comparisons, launch recovery and blinding/control gaps. Revision 1 narrowed unsupported claims. The notes do not attribute every finding to the added item. |
| 2 | 6 of 6 | fix-regressed; target-moved | Section (d) says fixes added receipts, locks and publication generations whose gaps appeared in the next review. Section (b) lists control-policy mismatch, dependent pairs, completion before durable record, unnamed benefit source, no scoring stop gate and a conflicting repeat rule. The director allowed replacement of the retained-reviewer study with a smaller history-command study. |
| 3 | 5 of 5 | fix-regressed; target-moved | The next review found conflicting missing-measurement rules, a withdrawn-result index, missing Mac path, dispatch-dependent pairing and a workload worth only 0.07 s/week. The director ruled that Mac stays in scope and that the first study needs a real recurring workload or an explicit framework-exercise claim. |

All 19 findings were fixed; none was dropped. Counts fell 8 → 6 → 5, but the design did not converge: each repair added mechanisms with new gaps, and the document grew from 568 to 696 lines. The third review still had two high findings. Its final fixes had no further review because the cap was three. The notes support a design repair/target-change cycle; they do not establish a code regression rate or provide agent-time measurements comparable to the run records.

## Proposals

These are process-change questions for the user through the showrunner. None is adopted here. The unit director owns issue filing, ranking values, the showrunner message and the user's answers in Phase 5. Workers file nothing and send no proposal to the user or showrunner. No proposal changes only `scripts/history/`.

The five eligible roots each hold at least 10% of the fixed cohort's 260 rounds. The proposals are ordered by **certain rounds saved in the studied phase**, with ties ordered by root rounds in [Totals](#totals). Proposal numbers follow this revised order. A moved repair earns round credit when the entire original interval leaves the phase under the proposed rule; its work time is **moved**, not saved. **Fix seconds saved** means time spent nowhere. No proposal below proves any such time saving. A record correction alone receives neither round nor time credit.

| Proposal | Root: rounds / seconds in cohort | Certain rounds saved | Fix seconds saved | Recorded seconds moved |
| --- | ---: | ---: | ---: | ---: |
| P1: run command-only follow-through without a repair worker | mechanical: 39 / 107,662 | 8 | 0 | 24,033 |
| P2: route missing acceptance criteria after landing | spec-gap: 43 / 186,473 | 5 | 0 | 1,036 |
| P3: route repeated repairs outside the kept categories | fix-failed: 94 / 473,015 | 3 | 0 | 348 |
| P4: verify phase and activity boundaries | record: 35 / 53,169 | 0 | 0 | 0 |
| P5: preserve accepted behavior touched by a repair | fix-regressed: 30 / 78,532 | 0 | 0 | 0 |

The counted sets do not overlap: P1 uses rank 1 R6/R12/R20/R25, rank 8 R13, rank 10 R4 and rank 13 R3/R9; P2 uses rank 3 R5/R10/R12/R16/R17; P3 uses rank 3 R6/R11/R18. Together they remove **16 original-phase rounds**, move **25,417 recorded agent-seconds**, and prove **0 seconds saved**. Recorded moved time is the work observed in these intervals, not a prediction of future follow-up cost. No other row receives credit. Each receiving Work Order named below is a proposed follow-up for the director to create, not an existing completed phase.

### Evaluation shared by all five questions

Phase 7 of the plan uses the 30 days before the effective main checkpoint and the period after it. Use the same corrected `history.py rounds` API in both windows, including its temporal batch join. Select phase starts, list open and mixed records separately, and compare completed non-mixed phases. Report median rounds, share with at least three rounds and that group's share of fix time over reviewed phases only. Also report summed `past_landing`, rounds and agent-seconds per phase, and Phase 1 cause counts over all selected phases. Hand-label roots with evidence and an independent recount; a review/gate cause is not a root.

Exposure is decided from the Work Order, finding state or handoff **before** the treated step runs, as defined in each Measure below. Keep every exposed phase in the denominator even if it then has zero repairs or zero target-root rounds. Apply the same definition to baseline records. If the necessary before-step evidence is absent, report that exclusion and treat the comparison as inconclusive. Readiness requires 30 completed non-mixed exposed phases after adoption; each window also needs at least 20 completed phases, 10 exposed phases and 12 non-excluded merge design-check rows. Report actual denominators. An insufficient sample gives observations without a pass verdict.

Beside each round target, apply these **work-kept margins**: no increase in mean holds per phase or mean merge design-check defects per phase (margin 0 for each); every moved finding has a linked receiving Work Order, owner, due checkpoint and closure evidence; 100% of follow-ups due in the observation window reach done; and no known defect from a stopped repair reaches the user or merge. Set the due checkpoint at routing, before its result is known. A missing follow-up, unfinished due work, or missing closure/review evidence makes the result **inconclusive, not a pass**. Do not end the observation before later-due follow-ups have results. Routing does not waive an unmet acceptance gate: report the originating pilot or phase as incomplete until its required result is supplied.

Take holds, merge defects, review minutes and UX repair minutes from the fields used by `review_regime.py report`. Bound both windows on `merged`, and report exclusions and samples; its fixed historical regimes are not these windows. Report combined parent-plus-follow-up rounds and agent-seconds, diagnosis, preparation, command execution and review cost as work-kept and cost measures. These figures do not cancel original-phase round credit. State moved seconds separately from seconds spent nowhere. When several changes are adopted together, report the combined effect and leave individual attribution inconclusive unless cases can be separated. Phase 1 cause totals provide scale; they cannot substitute for root or routing evidence.

### P1. Run command-only follow-through without a repair worker — mechanical

**Question:** Should the director run unchanged lint/test commands itself when the handoff has no remaining edit, instead of launching a repair worker to run them?

**Change:** Add to `commands/unit/direct.md`, `<FixDispatch/>`, after its Verification contract:

> Put known fixture/setup updates, lint, tests and merge follow-through in the original repair prompt. After handoff, inspect the remaining action. If it only runs unchanged commands and no edit or judgment is pending, the director runs the existing detached verification workflow; do not dispatch a repair worker. Preserve the commands, output, tree identity and review result. A failing command reopens or retains its finding and goes to the owning worker if an edit is needed. Fixture expectations, source edits and conflict choices remain owner work; a green command does not replace closure review.

**Rounds saved:** **8 rounds; 0 fix seconds saved; 24,033 recorded seconds moved to director verification.** The written rule covers command-only intervals regardless of their primary root. Each row below names only command execution, with no pending edit or judgment; its activity and seconds were checked in the linked run record for that phase instance.

| Rank / round | Seconds moved | Command-only evidence from the study and run record |
| --- | ---: | --- |
| 1 / R6 | 258 | “running final script gates” |
| 1 / R12 | 118 | “running the three gates on the frozen script” |
| 1 / R20 | 1,251 | “running the round 13 gates on the frozen script” |
| 1 / R25 | 326 | “running the three gates on the frozen script” |
| 8 / R13 | 9,609 | “rerunning lint, then the full test line” |
| 10 / R4 | 1,986 | “running lint and the repair tests after the hover test fix” |
| 13 / R3 | 141 | “running the chooser repair's lint and tests” |
| 13 / R9 | 10,344 | “rerunning the device chooser gates” |
| **Total** | **24,033** | **8 complete original-phase repair-worker intervals** |

Sources: rank 1's [run record](/home/natepiano/.local/state/plan-delegate/runs/f205ddd0-5865-4dec-9b85-849e4ffa3dd8.jsonl), phase 33b; rank 8's [run record](/home/natepiano/.local/state/plan-delegate/runs/e54c80d4-ad05-4d5a-b824-67d79e7f1131.jsonl), phase 58; rank 10's [run record](/home/natepiano/.local/state/plan-delegate/runs/8777d55e-0ac2-4586-94c5-c7c309274d22.jsonl), phase 10; rank 13's [run record](/home/natepiano/.local/state/plan-delegate/runs/7b115092-80c3-42c2-9652-2ff222d2c722.jsonl), phase 137. Use each section's instance. Rank 10 R4 and rank 13 R3 keep their mechanical roots; the other six keep their record roots. The proposed executor rule removes all eight repair-worker launches while preserving their commands as director activities. This is an actual dispatch change; the boundary proposal below receives no duplicate credit. Command execution time still occurs, so none is called saved time.

Rank 10 R9–R11 includes missing setup/resource and fixture edits; rank 7 R9 updates test samples. They cannot be sent to a command-only runner and receive zero credit. Rank 6 R6 (“lint”) and rank 8 R11 (“finishing lint and the full test line”) leave pending edits uncertain and also receive zero credit. Shot-only, ledger-only, unfinished or summary/edit/judgment intervals receive no credit.

**Work kept:** The director owns the same eight command sets and records their output and checked tree as required Verification activities of the original repair. There is no finding moved out by those successful command-only intervals and no new follow-up order is needed for command execution. A resulting edit goes to the file owner; if it lies outside landing scope, create “Verification failure: <command and failing case>” in the owning unit with due checkpoint, failure output, test and closure review. Completion requires green results and accepted repair review, not merely a command launch.

**Risk:** A director can misread a handoff and run checks while a fixture still needs changing. Running on the wrong tree can produce false confidence. Require an explicit no-edits handoff and tree identity; send every needed source/expectation decision back to its owner.

**Measure:** Exposure is a phase with a repair handoff whose **written Verification list contains lint or test commands**, identified before deciding whether to launch another worker or run them directly. Include phases where the original worker already completed all commands. Target at least **10% fewer mechanical original-phase repair rounds per exposed phase**; separately report avoided command-only worker rounds, unchanged command cost, moved seconds and any seconds spent nowhere. Beside the target require the shared 0 holds/merge-defect increase, 100% due follow-up closure and no defect escape, with 30 after exposed phases and 20/10/12 per-window samples. Missing handoff, command, follow-up or review evidence is inconclusive. Use `history.py rounds` and the windowed `review_regime.py report` fields for common totals and quality.

**Answers:** Adopt as a 30-exposed-phase trial (**recommended**); adopt with the named change “apply only to lint commands until test execution is audited”; do not adopt and retain repair-worker command continuations.

### P2. Route missing acceptance criteria after landing — spec-gap

**Question:** Should a missing acceptance criterion found after landing become a follow-up Work Order rather than another repair of the current phase, unless a kept-category failure or later explicit ruling requires it to stay?

**Change:** Add to `docs/production_format.md`, item 7, and enforce in `scripts/direct/findings.py` at the finding gate and `commands/unit/direct.md`, `<FixDispatch/>`:

> Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. From repair round 4 onward, do not dispatch a missing target, rule interpretation or acceptance case in this phase unless evidence establishes a phase-caused regression, broken working behavior or test/gate failure. A broad original permission to fix pilot defects does not override this check. A later explicit user or showrunner ruling may keep named work; cite it. Route every other missing criterion to a follow-up Work Order in the owning unit, with its target decision, implementation, tests and acceptance evidence. Do not dispatch a batch until every finding is either retained with evidence or assigned to that follow-up. Split mixed batches; keep their eligible defects here. Never mark an unmet pilot acceptance bar passed because its work was routed.
>
> Work Orders state the acceptance observation, unit or comparison, and named real-path test or live check for changed behavior. Reuse established targets. Resolve choices through the showrunner before dependent work; do not invent a number to fill the form. A later changed goal updates the order explicitly.

**Rounds saved:** **5 rounds; 0 fix seconds saved; 1,036 recorded seconds moved.** The hand application uses five rank 3 spec-gap intervals, with the source-based exclusions below. The primary roots and [Rule check](#rule-check) remain the study's recorded assessments; neither alone proves that the whole interval can leave the phase. The pilot order's broad fix permission is superseded by this proposed gate. The cited pilot entries record no later user/showrunner ruling requiring these complete intervals to remain in phase 17. A target missed in a cold review is not automatically a failed test/gate; supported rows such as rank 3 R15 stay. The complete routed intervals are:

| Rank / round | Seconds moved | Entire work routed and evidence | Proposed receiving Work Order in build-followups-style |
| --- | ---: | --- | --- |
| 3 / R5 | 166 | Fix S: accessors and one-value targets; pilot lines 545–553 add interpretations missing from the ticket. | Pilot criteria: accessors and one value per target |
| 3 / R10 | 576 | Fix W: single-letter parameters and array elements; pilot lines 635 onward define exclusions. | Pilot criteria: parameter and collection exclusions |
| 3 / R12 | 65 | Fix Y: a site judged under every rule it breaks; pilot lines 684 onward add the instruction. | Pilot criteria: overlapping rules |
| 3 / R16 | 99 | Fix AC: when-to-split asked for every file; pilot lines 740 onward add an omitted step. | Pilot criteria: complete split questions |
| 3 / R17 | 130 | Fix AD: preserve every stated comment reason; pilot lines 754 onward say neither rule nor ticket required it. | Pilot criteria: preserve comment reasons |
| **Total** | **1,036** | **5 complete original-phase intervals** | |

Rank 3 R9 and R13 receive **zero credit**, despite their outside-category designation in Rule check. The [pilot report](/home/natepiano/worktrees/claude-build-followups-style/docs/plans/build-followups-style-pilot.md:594) contradicts that designation for whole-interval routing: lines 594–597 say earlier Fix T introduced unsafe shadowing and Fix S allowed another-value lookups as accessors. R9/Fix V repairs those phase-caused defects as well as the rename-target interpretation (598–608), so retained work remains. Lines 678–681 record recall falling from 100/117 before fixes P–X to 63/117 when the expanded name-bindings paragraph diverted checks from other rules; lines 692–697 say R13/Fix Z splits the ticket to repair that loss. That broken previously working behavior also stays under the proposed exception. The 128 seconds in R9 and 381 seconds in R13 cannot certainly leave in full; any partial routing gets no whole-round credit.

Rank 1 R7/R8/R15/R16/R18/R27 has explicit later showrunner target rulings and earns no routing credit. Rank 5 R1 mixes mode/switch and drag work, so its 34,645 seconds earn no credit. Rank 14 R6–R9 has uncertain eligibility and earns no credit. Other missing-test prevention remains unproven.

**Work kept:** Create the five named Work Orders before closing the routing step. Each carries its pilot example/reference sites, the exact ticket decision, pinned tests, rerun recall and false-positive checks, and the original open acceptance requirement. The owning unit implements them; the showrunner settles rule changes, including the comment-reason rule still waiting for the user. Close each only with its commit, passing pinned tests, pilot rerun result and accepted review. Link that result back to the originating pilot. A routing message or renamed finding is not closure. Other discovered defects receive the same receiving-order link; retained failures keep their original checks.

**Risk:** Delaying ticket corrections can leave pilot recall below its required bar. An overly narrow gate can mistake a real regression for a missing criterion. Keep failed acceptance visible, stop any release that requires the missing result, and retain every proven kept-category failure.

**Measure:** Exposure is a phase whose **start Work Order changes visible layout, lifecycle/integration behavior, or pilot rule/acceptance criteria**, identified before implementation. Include it even if preparation prevents every spec-gap round. Target at least **10% fewer spec-gap original-phase rounds per exposed phase**. Report routed rounds, moved seconds, seconds spent nowhere, and follow-up totals separately. Beside the target apply the shared work-kept margins (0 holds/merge-defect increase, 100% due follow-up closure, no defect escape), samples (30 after exposed phases; 20 completed, 10 exposed and 12 reviewed rows per window), and inconclusive-on-missing-evidence rule. Use the corrected `history.py rounds` totals and the windowed `review_regime.py report` fields.

**Answers:** Adopt the gate and acceptance preparation as a 30-exposed-phase trial (**recommended**); adopt with the named change “start routing at round 5 rather than round 4”; do not adopt and keep the existing landing and Work Order rules.

### P3. Route repeated repairs outside the kept categories — fix-failed

**Question:** Should the hard-landing gate route a repeated open finding outside its kept categories instead of treating its earlier repair as permission to keep dispatching it?

**Change:** Add to `docs/production_format.md`, item 7, and enforce in `scripts/direct/findings.py` and `<FixDispatch/>`:

> Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. Before any round 4-or-later dispatch on a repeated finding, require evidence that it is a phase-caused regression, broken working behavior or test/gate failure. Earlier attempts and an open verdict alone are not that evidence. Broad original permission for pilot fixes does not override landing. Cite any later explicit user/showrunner ruling that keeps the named work. Route a repeated finding outside these categories to its owning unit's follow-up Work Order before another repair starts. Keep the finding open and link its checks, previous attempts, due checkpoint and final acceptance result. Split mixed batches and retain all eligible work. If eligibility is unknown, diagnose it rather than silently route or count it as a certain saving.
>
> For retained work after two completed failed fixes, record the failure, approaches tried, cause diagnosis and next distinguishing test before another approach. Interrupted workers and account moves remain continuations, not completed rejected patches.

**Rounds saved:** **3 rounds; 0 fix seconds saved; 348 recorded seconds moved.** Rank 3 R6/R11/R18 are outside-category, whole-interval repeated ticket repairs, with no recorded later explicit ruling keeping their repair in the phase. The original broad pilot permission is superseded by this proposed gate. The evidence and receiving orders are:

| Rank / round | Seconds moved | Entire work routed and evidence | Proposed receiving Work Order in build-followups-style |
| --- | ---: | --- | --- |
| 3 / R6 | 73 | Fix T: macros, variants and shadowing after S misses recall; pilot lines 567–574. A separate showrunner exclusion of one reference site is not a ruling to keep T's repairs here. | Pilot recall: macros, variants and shadowing |
| 3 / R11 | 59 | Fix X: conversion-parameter instruction and pinned test after another recall miss; pilot lines 655 onward. | Pilot recall: conversion parameters |
| 3 / R18 | 216 | Fix AE: split-alone instruction after AC bundles four defects; pilot lines 760 onward. | Pilot recall: one split per finding |
| **Total** | **348** | **3 complete original-phase intervals** | |

No other fix-failed row is counted. Rank 2 R19–R23 repairs retained regressions/test failures. Rank 11 R7–R12 has uncertain provenance and one accepted attempt rather than six rejected patches. Rank 6 R8/R11, rank 13 R12 and rank 15 R10/R11 are zero-time ledger-only continuations; this proposal does not prove that deleting those entries removes work. No round is credited merely because diagnosis might prevent it.

**Work kept:** The three receiving orders retain the open recall/split cases, earlier approaches and exact pilot examples. Require the pinned ticket test, corrected ticket output, pilot rerun against its existing bar and independent acceptance review, with commits and results linked back to the phase. The original pilot remains incomplete until required results arrive. A proven regression or failed gate remains here until its check passes; a mixed batch cannot be counted as wholly removed. Diagnosis and any new eligible defect remain required work, with their costs reported.

**Risk:** Routing can delay a necessary pilot repair or hide a real regression behind a scope label. Eligibility needs recorded evidence and cold review; an unmet required pilot result must still block its dependent release.

**Measure:** Exposure is a phase that **enters a round-4-or-later dispatch decision with a finding still open after a preceding repair**, recorded before the gate decides retention or routing. Keep routed phases and phases with no further repair in the denominator. Target at least **10% fewer fix-failed original-phase rounds per exposed phase**. Count rejected completed fixes separately from continuations and report diagnosis, moved seconds and follow-up results. Beside the target apply the shared 0 holds/merge-defect increase, 100% due follow-up closure, no escaped defect, 30 after exposed phases and 20/10/12 per-window samples; missing eligibility, follow-up or review evidence is inconclusive. Read `history.py rounds` intervals/time and the windowed `review_regime.py report` quality fields.

**Answers:** Adopt as a 30-exposed-phase trial (**recommended**); adopt with the named change “require a cold reviewer to approve every routing decision”; do not adopt and retain the present landing check and two-fix diagnosis rule.

### P4. Verify phase and activity boundaries — record

**Question:** Should every launch verify its Work Order's active phase and distinguish planned work or verification from repair?

**Change:** Add to `commands/unit/direct.md`, `<PassOwnership/>` and the `<FixDispatch/>` pre-dispatch check:

> Before each launch, check the prompt's phase against its active instance and title. Run start-phase and finish-phase at the real boundaries and keep parallel phases separately identified. Do not launch against a stale or absent instance. Classify planned implementation/deployment as implementation and command-only verification as an activity. A fix launch names its defect, failed gate or ledger batch. Correct active state only through supported recorder operations, preserving old evidence; never fabricate starts/finishes or rewrite historic records. Missing historic boundaries stay uncertain in analysis.

**Rounds saved:** **0 rounds; 0 fix seconds saved; 0 seconds moved by this proposal.** No work-removing change fits the record root here: rank 4 R1–R3 remains planned rollout work, rank 1 R6/R12/R20/R25 remains required gate execution, and correcting their labels alone earns no credit here. P1 separately removes the repair-worker launches for those four rows through its executor change; it keeps their gate execution and receives the only round credit. The same applies to all 35 record rows / 53,169 seconds. The three large mixed records remain outside the cohort and add no credit.

**Work kept:** Planned rollout retains its original implementation Work Order and proof; verification retains its command results; real defects retain their ledger checks. Any actual phase transfer gets a receiving Work Order, owner, due checkpoint and closure evidence before the outgoing phase closes. Recovery of missing state cannot stand for completion of deployment or checks.

**Risk:** A stale-state refusal can block a valid parallel launch, while a title-only comparison can accept the wrong instance. Check instance identity, preserve separate active phases and never overwrite history during recovery.

**Measure:** Exposure is a phase with an **incoming launch request for implementation, deployment, verification or repair**, recorded before the boundary check. Include phases whose first check prevents every record error. Target **100% correct phase-instance/activity association and 0 new missing-boundary record intervals; 0 credited work-saving rounds or seconds**. Use the same corrected API in both windows. Beside the accuracy target apply the shared 0 holds/merge-defect increase, 100% due follow-up closure, no defect escape, 30 after exposed phases and 20/10/12 per-window samples. Missing launch, completion or review evidence is inconclusive. Report `history.py rounds` mixed/past-landing measures and the windowed `review_regime.py report` fields without treating a relabelled count as saved repair.

**Answers:** Adopt as a 30-exposed-phase accuracy trial (**recommended**); adopt with the named change “warn and repair state before the next launch rather than refuse the current launch”; do not adopt and retain the present boundary rule.

### P5. Preserve accepted behavior touched by a repair — fix-regressed

**Question:** Should each repair prompt carry accepted checks for behavior its edits can invalidate, and require those checks at closure as well as the new finding's check?

**Change:** Add to `commands/unit/direct.md`, `<FixDispatch/>`, beside regression-test and closure requirements:

> Before editing, list accepted behaviors and checks reached by the repair's planned paths, using earlier verdicts, real callers and the current suite. Carry their commands/live views into Verification beside the new regression check. Preserve assertions unless a cited approved goal change or evidence shows an expectation is wrong. Run the new and affected accepted checks on the same tree. Compare a suspected old defect against the prior accepted tree. Reopen lost accepted behavior with failing evidence. Keep cold closure review for public contracts, registration and cross-batch invariants; route unrelated old defects under item 7.

**Rounds saved:** **0 rounds; 0 fix seconds saved; 0 seconds certainly moved** out of 30 fix-regressed intervals / 78,532 seconds. No safe routing change fits this root: phase-caused regressions must stay, and the rows do not prove that earlier checks would prevent a complete repair interval. Rank 1 R2 loses the Hidden-cable rule, rank 7 R8 reports five regressions from the previous repair, and rank 2 R24 reopens accepted F036/F041. They remain required repairs. Hypothetical prevention is counted as zero.

**Work kept:** Reopen any newly failed accepted behavior and require both its check and the original finding's check to pass before acceptance. A proven old defect goes to “Pre-existing defect: <behavior and failing check>” in its owning unit, with due checkpoint, baseline comparison, implementation, passing result and accepted review. No cohort interval is claimed wholly moved by this proposal. The accepted-check list does not replace cold review or the full changed-view live check.

**Risk:** An incomplete path list can give false confidence, a broad suite can make small repairs expensive, and stale tests can freeze an old goal. Record approved target changes and use real integration/visible checks rather than private helper tests alone.

**Measure:** Exposure is a phase with a repair prompt whose **planned edited paths reach an already accepted behavior or check**, determined from callers, verdicts and tests before the patch. Include phases where those checks prevent all regressions. Target at least **10% fewer fix-regressed original-phase rounds per exposed phase**. Report regression-check and review costs, moved work and any proven seconds spent nowhere separately. Beside the target apply the shared 0 holds/merge-defect increase, 100% due follow-up closure, no escaped defect, 30 after exposed phases and 20/10/12 per-window samples. Missing accepted baseline, follow-up or review evidence is inconclusive. Hand roots determine the regression rate; corrected `history.py rounds` totals and windowed `review_regime.py report` fields give time and merge quality.

**Answers:** Adopt as a 30-exposed-phase trial (**recommended**); adopt with the named change “apply accepted-check lists only to integration, lifecycle and visible-layout repairs”; do not adopt and retain the new-finding regression-test requirement alone.
