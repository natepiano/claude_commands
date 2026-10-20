# Repair rounds: evidence study

## Method and selection

Measured on 2026-10-20. The frozen selection command was:

```sh
python3 scripts/history/history.py rounds --since 60d --status all --format json --top 40
```

The JSON summary at selection was: 1,867 phases counted; median 0 rounds; 222 phases with at least 3 rounds (11.9%) held 79.2% of fix time; 2,255 rounds and 7,587,361 agent-seconds of fixes. Causes were review-ux 18.9%, review-code 61.6%, gate 7.3%, merge 1.9%, live 0.2%, other 10.1%; 39 records were mixed. The largest 15 non-mixed records contain **255 rounds**. The three largest mixed records contain another **138 rounds**, analyzed separately below. Active records can grow while this study runs; a later full-window total is identified separately. The repair verification on 2026-10-20 at 18:28 EDT ran the same command and confirmed the same 15 records, 255 rounds and 975,069 seconds. Its live full-window metadata had grown to 1,868 phases / 2,260 rounds / 7,589,160 seconds; the dated totals below are frozen captures.

A round is the API's overlapping fix-worker interval, with a matched ledger dispatch counted once. Separate intervals can share one ledger batch number. A ledger-only dispatch has zero measured time. An unfinished start has no measured end and never extends an interval. Time is summed agent time, including concurrent workers; it is not elapsed wall time. All section round numbers below are API interval numbers, not the ledger's dispatch numbers. Each section identifies the run and phase instance, so a finding ID is local to that section.

The 60-day filter selects phases by their recorded start boundary. It then counts the recorded rounds of each selected phase; it does not clip every pass to a 60-day wall-clock interval. Selection sorts by round count, then summed fix time, and removes mixed records before taking 15. The full-window cause table includes mixed and zero-round phase records. Its time allocation sums each API round's `fix_seconds` under its cause.

The Phase 1 cause describes the immediate trigger. The root label is a hand assessment from the cited activity, findings and boundary Work Order. The labels are `spec-gap`, `target-moved`, `out-of-scope`, `fix-failed`, `fix-regressed`, `serial-check`, `mechanical` and `record`. One primary root receives the interval's entire measured time; a multi-worker interval can contain more than one kind of work. These allocations estimate the time associated with each root, not the time spent on each individual defect. A missing acceptance case is marked as an inference when the Work Order alone cannot prove its causal role. New findings after a live check do not by themselves prove a serial-check violation.

Cause priority matters: a matched ledger dispatch is `review-ux` or `review-code` before activity words are considered; then come gate, merge, live and other. Thus a live visual check followed by ledger repair usually counts as review, not live. The low `live` share measures the residual cause category, not the frequency of live checks. It also means stale reused ledger numbers can hide the immediate trigger, as in rank 3.

Every round row names its recorded pass activity or, for a ledger-only interval, its dispatch and covered findings. The finding catalogs give the recorded titles. Work Orders are read from git before the start where available; the rollout's rebased history and phase 58's late saved order have explicit limitations. `check=ok` is a heuristic result, not proof that all recorded work is a repair or belongs to one task.

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
| 1 | review-code | spec-gap | 561 | “fixing effect placement under a parent, effect-view tool clearance and cable settling”; “writing regressions for placement, effect-view clearance and cable settling”. Covered: F001, F002, F003, F004. Restore parent-relative Transform writes, tool clearance and settling under the stated framing contract (F001–F004); no changed visual target is recorded. The cited start order names effect placement and its fake-BRP tests, but no placement test under a Selection parent (F001). That missing parent/local-world case is the primary gap assessment; the whole batch also corrects explicit whole-tool and settling requirements. |
| 2 | review-code | fix-regressed | 270 | “keeping the Hidden-cable rule in the new route settle wait”. Covered: F001, F002, F003, F004. The route-settle repair had to preserve the existing Hidden-cable rule. |
| 3 | review-code | mechanical | 522 | “finishing round 1 regressions, then the final gates”. Covered: F001, F002, F003, F004. Finish the regression suite and gates for the same first batch. |
| 4 | review-ux | fix-failed | 1680 | “fixing effect and step framing”; “writing framing regressions”; “finishing framing regressions and gates”. Covered: F002, F005, F006, F007. F002 stayed open after batch 1; the next live check also added F005–F007. |
| 5 | review-ux | fix-failed | 309 | “making effect labels readable in the final view”. Covered: F002, F005, F006, F007. The final wider view still made the effect labels too small (attempts 2/2c). |
| 6 | review-ux | record | 258 | “running final script gates”. Covered: F002, F005, F006, F007. Run the gates for the existing framing repair. The activity records gate execution only, with no test update or source repair. |
| 7 | review-ux | target-moved | 632 | “removing cables from the effect subject”; “writing effect subject regressions”. Covered: F002, F007. Showrunner removed both cables from the framed subject (attempts entry 4). |
| 8 | review-ux | target-moved | 643 | “adding the second effect row”; “writing second-row regressions”. Covered: F002, F007. Showrunner added a second effect row and accepted a 9 px planning bar (entry 5). |
| 9 | review-ux | fix-failed | 723 | “placing effects in the nearest free grid place”; “writing effect grid regressions”. Covered: F002, F007. A fixed two-row layout still failed the sixth effect; replace it with a general grid. |
| 10 | review-ux | mechanical | 152 | “removing the eight-effect test and running gates”. Covered: F002, F007. Remove the obsolete eight-effect assertion and run gates. |
| 11 | review-ux | fix-regressed | 836 | “diagnosing and fixing the Show-step view search”; “writing the Show-step regression, then gates”. Covered: F002, F005, F007. F005 reopened after earlier acceptance; radius-only Show search still cut tools (entry 6). |
| 12 | review-ux | record | 118 | “running the three gates on the frozen script”. Covered: F002, F005, F007. Run the frozen-script gates. The activity records gate execution only, with no test update or source repair. |
| 13 | review-ux | fix-failed | 1277 | “limiting the Show orbit and making the effect planner use the live framing's tools”; “writing regression tests from the live run 5 scene, then the gates”. Covered: F002, F008, F009. Planner and live scene used different tool sets; F002 persisted (entries 7–8). |
| 14 | review-code | fix-failed | 390 | “diagnosing each place's binding rule, then growing the effect grid upward”; “updating grid tests for the upward layout, then the gates”. Covered: F002, F009. Swirl still refused every grid place after the prior repair; attempts entry 8 diagnoses the expanding frame and tries an upward grid. That entry does not name a new showrunner ruling. |
| 15 | review-ux | target-moved | 1553 | “narrowing the whole-or-out set and framing the Show tool band”; “adding the run-6 scene and Show floor tests, then the gates”. Covered: F002, F009, F010. Whole-or-out was narrowed to picture, Show and current effect (entry 10). |
| 16 | review-ux | target-moved | 1513 | “finishing the Show view under the one rule, then the freeze”; “fixing the run-6 fixture and Show test, then the gates”. Covered: F002, F009, F010. Showrunner applied the same narrower rule to Show itself (entry 10). |
| 17 | review-ux | fix-failed | 1927 | “diagnosing the Show refusals and the Swirl text size, then the freeze”; “checking the fake against live run 6, then the gates”; “running the round 11 gates on the frozen script”. Covered: F002, F009, F010. Settled labels and planning measures still disagreed; F002/F009/F010 remained open (entry 11). |
| 18 | review-ux | target-moved | 2412 | “making effect steps continue with the least-broken view, then the freeze”; “updating the run-6 chain test to best-effort, then the gates”. Covered: F002, F009, F010. Fallback B allowed best-effort effect views with warnings (entry 12). |
| 19 | review-ux | fix-regressed | 702 | “framing the turned picture by its own corners, then the freeze”; “fixing stale yaw-0 checks, then the gates”. Covered: F002, F009, F010. Turning the picture broke the world-box framing; use oriented corners (entry 12). |
| 20 | review-ux | record | 1251 | “running the round 13 gates on the frozen script”. Covered: F002, F009, F010. Run round-13 gates. The activity records gate execution only, with no test update or source repair. |
| 21 | review-ux | mechanical | 888 | “fixing the last two tests, then the gates”. Covered: F002, F009, F010. Correct the final two stale fixtures and run gates (entry 12). |
| 22 | review-code | fix-regressed | 1230 | “profiling the Show search, then centring and facing the picture”; “writing regression tests for the three live-run regressions”. Covered: F011, F012, F013. Live run 7 found three new regressions: 232 s Show search, off-centre rise and wrong picture facing (entry 13). |
| 23 | review-code | fix-failed | 3619 | “finishing the Show, centring and facing repairs, then the freeze”. Covered: F011, F012, F013. Complete the still-open Show, centring and facing repairs (entry 14). |
| 24 | review-code | fix-regressed | 2068 | “cutting the effect placement search time”; “adapting the fake and writing the placement-time test”. Covered: F014. The new fallback placement took 341 s for Vignette instead of seconds (entry 14). |
| 25 | review-code | record | 326 | “running the three gates on the frozen script”. Covered: F014. Run gates on the frozen placement-time repair. The activity records gate execution only, with no test update or source repair. |
| 26 | review-code | fix-failed | 2771 | “sorting and fixing the round 17 test failures”; “sorting stale tests and running the gates”; “updating the stale tests and running the gates”. Covered: F015. The frozen-script gates in R25 did not clear F015: 13 failures, 2 errors and 27 warnings (attempts entry 15). R26 continues that failed gate batch, including judgment about stale-by-ruling fixtures versus source defects; the whole interval is not a judgment-free test update. |
| 27 | review-ux | target-moved | 2820 | “round 19 script repair”; “round 19 tests”; “round 19 tests and gates”. Covered: F016, F017, F018, F019. Temporary scale cap 6.0 and warning for sub-9 px labels were ruled while cables fixed the long-lead defect (entries 15–16). |
| 28 | review-ux | fix-failed | 2461 | “speed up effect search, keep views face-on”; “tests for live-size search time and face-on Swirl”; “check and fix two test measures”. Covered: F019, F020. Hue placement remained slow after the prior repair; padded live geometry reproduced hashing cost and Swirl was still off-axis (entry 17). |

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
| 1 | review-code | spec-gap | 1450 | “repair round 1”. Covered: F001, F002. Update the ended-invocation expectation and correct first-draw account coverage. Start items 5–6 name first-content-row tests, but no multi-account cargo-handler first-draw fixture; F002 exposes that missing caller case. F001 is a test update in the same interval, so this is a whole-batch gap allocation, not a claim that its expectation change needed design work. |
| 2 | review-code | spec-gap | 4189 | “repair round 2”. Covered: F003, F004, F005, F006, F007, F008, F009, F010, F011, F012, F013, F014, F015, F016. F003–F016 correct drawable ownership, lifecycle clocks, queue bookkeeping, test coverage and style against the explicit start contract; a missing overall goal is not shown. Start item 9 names enter/leave timing tests at configured durations, but no test for a second ending while another cell is already fading. F004 is that missing interaction case; F005–F008 also concern interacting lifecycle clocks. Other findings in this interval directly correct stated ownership and drawing contracts. |
| 3 | review-code | fix-failed | 1132 | “repair round 3”. Covered: F003, F009, F010. F003/F009/F010 remained open after the preceding batch. |
| 4 | review-code | fix-failed | 2373 | “repair round 3”. Covered: F003, F009, F010. Continuation of the same unresolved batch-3 findings. |
| 5 | review-ux | fix-regressed | 8417 | “repair round 4”. Covered: F017, F018, F019, F020, F021. F017 explicitly records waiting commands never placed; F018–F021 expose lifecycle breakage. |
| 6 | review-ux | fix-failed | 2710 | “repair round 5”. Covered: F017, F020, F022. F017/F020 were still open; the fade also ended early (F022). |
| 7 | review-ux | fix-failed | 641 | “repair round 5”. Covered: F017, F020, F022. Second separated worker interval on batch 5. |
| 8 | review-ux | record | 0 | Ledger dispatch 7; no matched worker activity. Covered: F023, F024. Ledger-only dispatch 7 (F023/F024), with no observed worker interval; retain its zero measured seconds. |
| 9 | review-ux | fix-failed | 2406 | “repair round 6”. Covered: F017, F020. The existing free-row and foot-only failures F017/F020 persisted. |
| 10 | review-ux | fix-failed | 3840 | “repair round 6”. Covered: F017, F020. Continuation of the same batch-6 repair. |
| 11 | review-ux | fix-failed | 5099 | “repair round 7”. Covered: F017, F025, F026, F027. F017 remained open and the next capture exposed live-cell erasure F025 and ended-cell growth F027. |
| 12 | review-ux | fix-failed | 6634 | “repair round 7”. Covered: F017, F025, F026, F027. The same batch remained open across another worker interval. |
| 13 | review-ux | fix-failed | 3249 | “repair round 7”. Covered: F017, F025, F026, F027. F025/F027 still remained open after several attempts. |
| 14 | review-ux | fix-failed | 67 | “repair round 8”. Covered: F025, F027. Continue F025/F027 under the next ledger number. |
| 15 | review-ux | fix-failed | 5032 | “repair round 8b”. Covered: F025, F027. The activity names round 8b; the same column/fade findings persisted. |
| 16 | review-ux | fix-failed | 385 | “repair round 8c”. Covered: F025, F027. The activity names round 8c; the same findings persisted again. |
| 17 | review-ux | fix-failed | 2891 | “repair round 9”. Covered: F025, F027, F028, F029. F025/F027 persisted; also viewport panic F028 and lost regression assertions F029. |
| 18 | review-ux | fix-regressed | 4048 | “repair round 10”. Covered: F025, F027, F030, F031, F032. F030 explicitly identifies a round-8 pid-handover regression; F031/F032 add layout and ancestry regressions. |
| 19 | review-ux | fix-failed | 19367 | “repair round 11”. Covered: F025, F027, F030, F031, F032, F033. F025/F027 and the round-8 regression F030 remained in the batch. The later log reiterates the fade clock already specified by start item 9 and adds an ended-before-placement ruling (2950/2969); it does not establish that the whole batch was a changed goal. |
| 20 | review-ux | fix-regressed | 5053 | “repair round 12”. Covered: F025, F027, F030, F031, F034. F034 records early-close/zero-row remnants after the prior fade repair; F030 remained open. |
| 21 | review-ux | fix-failed | 26430 | “repair round 13”. Covered: F025, F030, F035, F036, F037, F038. F025/F030 persisted; the showrunner retained the admission/layout/flicker repairs (log 3115). |
| 22 | review-ux | fix-failed | 24525 | “repair round 13 (continued)”. Covered: F025, F030, F035, F036, F037, F038. Continuation of the same ledger-18 batch, with F025/F030/F036/F037 still open. |
| 23 | review-ux | fix-failed | 36149 | “repair round 14”. Covered: F030, F036, F037, F039, F040, F041. F030/F036/F037 remained open; arrival hit targets and ink clocks F039–F041 were added. |
| 24 | review-ux | fix-regressed | 7691 | “repair round 15”. Covered: F030, F036, F040, F041, F042, F043. F036 and F041 reopened after acceptance; F042 also exposes dropping live cells on failed fit. |
| 25 | review-ux | fix-failed | 14072 | “per-candidate measurement, startup wave cause, restored owner, frozen ended rows”. Covered: F030, F036, F037, F042, F044. F030/F036/F042 stayed open; reopened F037 and a new blank ended row F044 were included. |
| 26 | review-ux | fix-failed | 13658 | “missing state owner, publication age, elapsed smoothing, full-height packing”. Covered: F030, F036, F042, F045, F046. F030/F036 remained open and F042 reopened; log 3449 retains ownership, publication-age and elapsed smoothing work. |
| 27 | review-ux | record | 0 | “missing state owner, publication age, elapsed smoothing, full-height packing”. Covered: F030, F036, F042, F045, F046. An unfinished worker start is retained by the API as a zero-time dispatch, not proof of another completed repair. |

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

Start Work Order: `90d4024dfe7d00f99f0d99c3bd7adb00a745f89f:docs/plans/build-followups-style.md:544`, in `/home/natepiano/worktrees/claude-build-followups-style`. It explicitly allowed pilot fixes, required recall of at least 95% on 20 files per family, and measured holds, handoffs and restart behavior. Evidence for the lettered fixes is the [pilot report](/home/natepiano/worktrees/claude-build-followups-style/docs/plans/build-followups-style-pilot.md:313). This broad pilot bundled many distinct defects into one phase. From R5 onward, repeated `fix_pass=1` links F001/F002 to unrelated lettered fixes; the API's `review-code` cause is preserved, but those covered IDs are stale evidence of the specific defect.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/24e20a41-688b-4fff-9f5d-d243a63ee4e2.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 644 | “repairing the bootstrap-retry tip sync”; “writing the retry and reconcile regression tests”. Covered: F001, F002. Bootstrap/reconcile rules did not specify that a retry must align already-started workers with the refreshed ledger (pilot Fix A). |
| 2 | review-code | fix-regressed | 1292 | “repairing the merge-time wake races”; “writing regression tests for the wake races”; “fixing local tier A and E rules”. Covered: F001, F002, F004, F005. Fix C added waking waiters; review found two races in that new mechanism (pilot lines 330–342). |
| 3 | review-code | fix-regressed | 672 | “adding ticket yield”; “writing yield tests”. Covered: F006. Fix D made incomplete tickets refuse, but yielded files then could never close (F006). |
| 4 | review-code | fix-regressed | 0 | Ledger dispatch 4; no matched worker activity. Covered: F008. Ledger-only dispatch: the newly added read-window parser missed an attribute before mod (F008). |
| 5 | review-code | spec-gap | 166 | “fix S: accessors and one-value targets”. Covered: F001, F002. Fix S spells out accessors and one-value-per-target after recall misses (pilot 545–553). |
| 6 | review-code | fix-failed | 73 | “fix T: macros, variants and shadowing”. Covered: F001, F002. Fix T addresses recall still below the 95% bar after S; macros/variants/shadowing were missing (567–574). |
| 7 | review-code | spec-gap | 475 | “keying structure file cells by module”. Covered: F001, F002. Fix U corrects structure-cell identity from file to module, an uncovered pilot case (609+). |
| 8 | review-code | mechanical | 204 | “updating the planner's pending-pairs test”. Covered: F001, F002. Update the pending-pairs assertion after the planner change. |
| 9 | review-code | spec-gap | 128 | “writing fix V's ticket text and tests”. Covered: F001, F002. Fix V restricts rename targets and accessor interpretation after false positives (598+). |
| 10 | review-code | spec-gap | 576 | “fix W: single-letter parameters, array elements”. Covered: F001, F002. Fix W defines single-letter parameters and array elements, not stated in the ticket (635+). |
| 11 | review-code | fix-failed | 59 | “adding the conversion-parameter sentence and its pinned test”. Covered: F001, F002. The conversion-parameter miss survived prior ticket wording (Fix X, 655+). |
| 12 | review-code | spec-gap | 65 | “adding the two-rule sentence and its pinned test”. Covered: F001, F002. Fix Y adds judging a site under every rule it breaks (684+). |
| 13 | review-code | spec-gap | 381 | “splitting name-bindings out of the names read ticket, with tests”. Covered: F001, F002. Fix Z splits name-bindings into its own read ticket after recall losses (692+). |
| 14 | review-code | target-moved | 166 | “two judges on the names read ticket”. Covered: F001, F002. Fix AA adds two blind judges to the names ticket; this changes the checking mechanism (715+). |
| 15 | review-code | spec-gap | 116 | “moving listed-site lookups out of the batch loop, with a test”. Covered: F001, F002. Fix AB moves the listed-site reads out of the batch loop (731+). Pilot Fix AB (lines 731–739) measures 220 seconds in 1,318 listed-site reads and introduces a read-count test. The cited pilot order has a recall target but no listed-site read-count or queue-build cost target; this performance acceptance gap is the primary assessment, not a test-only change. |
| 16 | review-code | spec-gap | 99 | “adding the when-to-split step to modules structure tickets”. Covered: F001, F002. Fix AC asks when-to-split for each file, an omitted ticket instruction (740+). |
| 17 | review-code | spec-gap | 130 | “adding the keep-reasons line”. Covered: F001, F002. Fix AD adds keeping every stated reason in comments (754+). |
| 18 | review-code | fix-failed | 216 | “adding the split-alone sentence”. Covered: F001, F002. Fix AE narrows the split instruction after the prior fix bundled four defects (760+). |
| 19 | review-code | spec-gap | 317 | “adding the held-cell skip”. Covered: F001, F002. Fix AF specifies skipping unchanged held cells with open proposals (766+). |
| 20 | review-code | fix-regressed | 191 | “moving the held-judgment query”. Covered: F001, F002. Move the held-judgment query out of repeated selection work. This follows R19/Fix AF, which added the held-cell skip and its held-judgment lookup (pilot lines 766–774); R20 moves that new query out of repeated selection work. The preceding fix is the source of the new repeated-query path; the activity identifies a code repair, not a test update. |
| 21 | review-code | spec-gap | 240 | “phase 17 fix AG: checkpoint re-stamps own family's verdicts”. Covered: F001, F002. Fix AG stamps the current family’s own changed files, a lifecycle case missing earlier (775+). |
| 22 | review-code | spec-gap | 188 | “writing fix AH”. Covered: F001, F002. Fix AH supplies proposed rankings and a root-crate title (783–789). |
| 23 | review-code | fix-failed | 85 | “repairing fix AH root label”. Covered: F004, F005. The same Fix AH still needed its root label corrected; the attached F004/F005 are stale reused numbers. |
| 24 | review-code | fix-regressed | 213 | “writing fix AI”. Covered: F001, F002. Fix AI corrects local-main reads that incorporated an unpushed failing parent merge (790–799). |
| 25 | review-code | fix-failed | 116 | “fix AJ: style proposals read the crate layout once”. The R22/Fix AH ranking backfill is still uncompleted after R23 corrects its root title and R24 fixes a merge-reference issue. Pilot lines 800–811 say repeated 3.7-second layout reads would make that same backfill take about four hours; AJ finishes it in a 19-second dry run. This is substantive continuation of that uncompleted backfill, not a test-only update or proof that R24 caused the slowdown. |

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
| 1 | review-code | record | 4358 | “repair round 1: suppression complete on both machines (03 and 04 succeeded); repair seat told and expected to write the Linux upgrade script next; Mac reader upgrade held”. Covered: F001, F002, F003. The rollout itself had not been performed (F001); planned deployment was labelled repair. |
| 2 | review-code | record | 858 | “repair round 2: rewriting the Linux reader upgrade onto release builds”. Covered: F001. Rewrite the Linux upgrade script for release builds; F001 is a broad rollout gate. F001 says the planned rollout has not been performed; the recovered order already requires this Linux upgrade script. This is planned rollout preparation under a repair label, not identified lint, test update or merge repair. |
| 3 | review-code | record | 1217 | “restart step held on a runner configuration change; seats finishing records”. Covered: F001. Runner configuration still held the planned restart; no new defect named in activity. |
| 4 | review-code | fix-regressed | 1353 | “repair round 4: step 06 proof repaired after test found a crash; test and review rechecking”. Covered: F001. A test found a crash in the step-06 proof script. |
| 5 | review-code | record | 1478 | “Step 07 cleared; handed to the user”. Covered: F001. Cleared deployment step 07 was handed to the user, not another source repair. |
| 6 | review-code | record | 853 | “Step 08 cleared; handed to the user”. Covered: F001. Cleared deployment step 08 was handed to the user. |
| 7 | review-code | record | 1189 | “Step 09 draft repaired; awaiting user rebuild”. Covered: F001. Repair the step-09 draft before the user rebuild. The recovered order already requires this rollout draft and the user rebuild. No separate defect or changed target is named beyond the broad F001 rollout gate; do not count script preparation as mechanical repair. |
| 8 | review-code | record | 1097 | “recording natedev rollout completion”. Covered: F001. Record Linux rollout completion. |
| 9 | review-code | out-of-scope | 1887 | “final clean native Mac gates on the test repairs”; “repairing the macOS census and capture failures in cargo-tile source”. Covered: F001. Native Mac census/capture failures were pre-existing preflight defects, explicitly carried by the rollout Work Order. |
| 10 | review-code | record | 1868 | “recording the green Mac test results”. Covered: F001. Record the green Mac test result. |
| 11 | review-code | record | 1228 | “Mac reader upgrade handed to the user”. Covered: F001. Hand the planned Mac reader upgrade to the user. |
| 12 | review-code | record | 941 | “clearing the Mac runner restart script”. Covered: F001. Clear the Mac restart script. The recovered order requires the runner restart script; this activity clears that planned deployment step without identifying a separate repair. |
| 13 | review-code | record | 1528 | “recording the accepted runner restart”. Covered: F001. Record the accepted runner restart. |
| 14 | review-code | record | 957 | “second approval of the Mac capture-shim install script”. Covered: F001. Second approval of the capture-shim installation script is not a new repair by itself. |
| 15 | review-code | record | 799 | “writing and clearing the Mac all-account status script”. Covered: F001. Write/check the planned all-account status script. The recovered order requires the all-account proof. Writing and clearing its observation script is planned rollout work under F001, not a test-update finding. |
| 16 | review-code | record | 1214 | “restoration script approved; writer finishing its own checks”. Covered: F001. Check restoration of the automatic-installation suppression window. The activity records approval and checks for the planned suppression-window restoration, with no separate repair identified. |
| 17 | review-code | record | 997 | “pushing for CI and writing the over-limit refusal script”. Covered: F001. Push for CI and prepare the planned over-limit refusal observation. The recovered order requires the over-limit refusal observation; CI/observation preparation is still planned rollout work under F001. |
| 18 | review-code | out-of-scope | 6363 | “repair round 18: group membership proof, CPU display fix, branch CI fixes, runner rows”; “repairing promoted CPU and CI failures”; “pinning and packaging the Mac proof script”. Covered: F001. CPU display/CI fixes and group proof extend the deployment into prior-phase implementation (F001 remains the umbrella). |
| 19 | review-code | out-of-scope | 1129 | “Round 20: finishing the held window inventory repair and its tests; live minute follows”. Covered: F001, F004. F004 repairs the prior desktop watcher’s recurring kdotool invocation. |
| 20 | review-code | out-of-scope | 493 | “Round 21: capture ticks query only terminal windows; review and live minute follow”. Covered: F005, F006. F005/F006 repair prior desktop polling breadth and missing coverage. |
| 21 | review-code | out-of-scope | 496 | “pacing window position reads while the window is still”. Covered: F007. F007 repairs prior per-render-frame window position reads. |

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
| 1 | review-code | spec-gap | 34645 | “repairing the mode default, the switch row and the whole-number sliders”; “repairing the stage drag on a Replace axis”. Covered: F001, F002, F003, F004, F005. Restore the specified Add default and resolved mode/switch behavior (F001–F005); these requirements were explicit in the start Work Order. The cited order requires Replace locking and names slider pointer/keyboard/reset/IME tests, but neither its file list nor its tests name Show stage dragging on a Replace-cabled axis. F002 and the second worker activity identify that missing authoring-route test. JSONL 641–642 records 17,373 seconds for mode/switch/slider work and 17,272 for stage drag. The primary gap allocation is therefore an upper bound for a mixed 34,645-second batch; F001 and the other mode/switch requirements were explicit and are not themselves specification gaps. |
| 2 | review-ux | spec-gap | 40620 | “repairing the band tint, the Depth group spacing, the mode icon and the Material columns”; “writing drawn-pixel tests for the five repairs”; “finishing drawn-pixel tests for the five repairs after the sign-in fault”. Covered: F006, F007, F008, F009, F010. The inherited Add-band look had no measured contrast/color target; F006 and proportion/alignment findings start the visual loop. |
| 3 | review-ux | fix-failed | 8875 | “LFO keeps one size; column alignment test”; “LFO one-size tests and the final gates”. Covered: F006, F007, F008, F009, F010, F011, F012. F006–F010 remained open; the implementation also needed constant cube size and a failed alignment fixture. |
| 4 | review-ux | record | 1865 | “second live run and shots”. Covered: F006, F007, F008, F009, F010, F011, F012. The fix activity is only a second live run and shots. |
| 5 | review-ux | fix-failed | 7621 | “redrawing the reach band as a tint over a whole track”. Covered: F006. The same reach-band contrast F006 was still open. |
| 6 | review-ux | record | 1383 | “third live run and shots”. Covered: F006. The fix activity is only a third live run and shots. |
| 7 | review-ux | fix-failed | 1812 | “making the reach band a pale blue and fixing the fill test”. Covered: F006, F013. The reach-band tint still failed; adjust its color and the pixel test F013. |
| 8 | review-ux | record | 1716 | “fourth live run and shots”. Covered: F006, F013. The fix activity is only a fourth live run and shots. |
| 9 | review-ux | fix-failed | 17383 | “near-white reach band checked through the app's colour path”. Covered: F006. F006 remained open through another color-path repair. |
| 10 | review-ux | fix-failed | 2143 | “light grey reach band through the ordinary colour path”. Covered: F006, F014, F015. F006 persisted; pale-band pixel masks and another expected size F014/F015 needed updating. |
| 11 | review-ux | fix-failed | 1697 | “reach band as a light capsule behind the track”. Covered: F006. The same F006 finally required a separate capsule behind the track. |
| 12 | review-ux | record | 1286 | “fifth live run and shots”. Covered: F006. The fix activity is only a fifth live run and shots. |
| 13 | review-code | mechanical | 395 | “updating the slider metrics test for the capsule”. Covered: F016. Update the metrics helper to the new capsule shape (F016). |
| 14 | review-code | fix-regressed | 2916 | “repair round 11: switch reading, effective mode, switch veto”. Covered: F017, F018, F019. Rebuilt switch row reseeded readings and mode presentation disagreed with resolution (F017–F019). |
| 15 | review-code | fix-regressed | 705 | “repair round 12: revert the Power switch change”. Covered: F020. F020 explicitly says round 11 changed every tool’s Power semantics; revert it. |
| 16 | merge | mechanical | 4819 | “merge resolution after phase 6”. Resolve the phase-6 integration merge. |
| 17 | merge | mechanical | 10022 | “trial merges after phase 6”. Trial-merge integration work. |
| 18 | merge | mechanical | 13003 | “merge of widget's landed work”. Merge widget’s landed work. |

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

Plan: `tool-based-ui-startup-polish.md`. Run `f93f565a-eef3-4174-ac03-755c36e844f7`, phase `31`, instance `b5546492-b71d-4d39-879b-5a0b340843db`. **17 rounds; 16 findings; 43,336 fix seconds (12h02m); 14 rounds past landing.** Start: 2026-10-15T11:53:20.446+00:00.

Start Work Order: `af797d1a44dccf371f32df31bf88860af732cf3f:docs/hana/tool-based-ui-startup-polish.md:1029`, recovered in `/home/natepiano/rust/tool-based-ui-startup`. It gave nine deferred grid tasks, named pixel tests and required the Phase 21 image to stay unchanged except for the named differences. The floor-low brightness defect was explicitly older than Phase 21 and was part of this new order. Findings show new Solid/Fade pixel defects and repeated F008 repairs; this was not an unspecified request for a new grid.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/f93f565a-eef3-4174-ac03-755c36e844f7.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 13723 | “restoring the default grid line weight”; “making the grid tests see lost pixels”. Covered: F001, F002, F003. Restore the expressly required default grid weight and make the lost-pixel tests fail (F001–F003). The cited order specifies brightness-step and line-weight tests, but no retained-pixel-count assertion for the orbit: F002 says lost pixels remain invisible to its brightness test, and F003 says close rows are skipped. The primary gap is that measurement coverage, not an absent grid-preservation goal; JSONL 59/66 records 5,160 seconds for lost-pixel tests and 8,563 for grid-weight code. The whole-batch gap allocation is an upper bound; F001 also corrects the explicit preservation contract. |
| 2 | review-ux | fix-regressed | 4305 | “tidying the floor grid code”; “tidying the floor grid tests”. Covered: F007, F008. The preservation contract was broken: new Fade dashes and Solid moire/glow (F007/F008). |
| 3 | review-ux | fix-failed | 4517 | “grid dashes and moire”; “grid pixel tests”. Covered: F008. F008 remained open after the first grid repair. |
| 4 | review-ux | fix-failed | 647 | “grid repair apply”; “crossings lint”. Covered: F008. Apply the existing grid correction and satisfy crossing lint. F008 remains open after R3; the apply activity continues the same grid correction. Crossing lint is only the concurrent mechanical part. |
| 5 | review-ux | mechanical | 526 | “lint”. Covered: F008. Lint only. |
| 6 | review-ux | fix-failed | 222 | “crossing glow”. Covered: F008. F008 still open; correct crossing glow. |
| 7 | review-ux | mechanical | 914 | “glow fixture”. Covered: F008. Update the glow fixture. |
| 8 | review-ux | fix-failed | 1273 | “Solid coverage fix”. Covered: F008. Solid coverage still failed F008. |
| 9 | review-ux | fix-regressed | 360 | “Solid edge band”. Covered: F008, F009. F009 explicitly dates new minor-line beading to round 4. |
| 10 | review-ux | fix-failed | 774 | “fixing the centre-line fade and minor-line beading”. Covered: F008, F009. The same centre fade and beading remain in the batch. |
| 11 | review-ux | mechanical | 168 | “writing the floor-grid regression tests”. Covered: F008, F009. Write the floor-grid regression tests. |
| 12 | review-ux | mechanical | 814 | “writing the floor-grid regression tests”. Covered: F008, F009. Continue those tests. |
| 13 | review-ux | record | 820 | “proving the floor-grid regressions red”. Covered: F008, F009. Prove the regression tests fail without the repair. The activity names execution of the already-written tests, not another test update or source repair. |
| 14 | review-ux | fix-regressed | 4886 | “fixing the Solid grid's density clip”. Covered: F010. F010 says the density fade lost mid-floor minors and horizon reach. |
| 15 | review-ux | fix-failed | 653 | “matching the centre line's fade to its neighbours”. Covered: F011. The centre line still ended ahead of its neighbours (F011). |
| 16 | review-code | fix-regressed | 1805 | “repairing grid blending over the scene and the centre-line test bound”. Covered: F012, F013. New max blending erased lines over brighter backgrounds; F013 exposed a permissive test. |
| 17 | review-ux | fix-regressed | 6929 | “repairing grid compositing order and restoration mask”. Covered: F012, F014, F015. New restoration pass changed non-grid antialiasing and blackened tool edges (F014/F015); F012 persisted. |

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
| 1 | other | spec-gap | 2298 | “fixing old-scene loading and the face fit”; “updating the catalog and prompt to the new LFO contract”. Adapt old-scene loading, catalog and prompt to the changed LFO schema. The cited start order names six rows and cable behavior but no numeric face-fit or plate-margin target. The face-fit activity is the gap evidence for this mixed interval; old-scene loading and catalog/prompt changes also occur and are not test-only work. |
| 2 | other | record | 1510 | “taking Phase 4 live shots on 15771”. A fix pass contains only the required live shots. |
| 3 | other | spec-gap | 2440 | “fixing the jack panel's visible defects”. The start Work Order required six rows but gave no numeric face-fit or margin targets; the live check found visible defects. |
| 4 | other | spec-gap | 1877 | “making the jack panel one fitted plate”. The panel needed a single fitted plate, beyond the Work Order’s row-level fixture. |
| 5 | gate | mechanical | 4499 | “fixing two tests the fitted plate broke”. Two tests broke when the fitted plate changed. |
| 6 | other | fix-failed | 817 | “repair round 5: split panel_ports and prove the fitted panel”. Split panel_ports and pin the fitted-panel result. The fitted-panel work from R4 is still being proved after R5 repairs its broken tests. The activity continues that same repair while splitting panel_ports; a source refactor alone is not lint or a test update. |
| 7 | other | spec-gap | 6715 | “repair round 6: plate margins and a clipped value”. No numeric plate margin/value-ink target was given; another fit and clipping adjustment followed. |
| 8 | other | fix-regressed | 1681 | “repair round 7: five regressions from round 6”. The activity explicitly names five regressions from round 6. |
| 9 | gate | mechanical | 537 | “repair round 8: rear-click test samples”. Update rear-click sample points. |
| 10 | gate | fix-failed | 3687 | “making the rear-panel click test stable”. The rear-panel click assertion remained unstable. |
| 11 | other | fix-failed | 1968 | “evening the LFO plate's right margin”. The right margin still needed correction after earlier fit work. |
| 12 | gate | record | 1280 | “testing Phase 4 against organon's latest panel work”. Verify compatibility with organon’s latest panel changes. This activity records compatibility testing, without a test update or source repair. |
| 13 | merge | mechanical | 1405 | “finding why the merged tree shifts the Show label colour”. Resolve changed Show-label color after integration. |
| 14 | other | spec-gap | 606 | “measuring whether ordinary value fields fit the new number format”. The new number format had no explicit ordinary-field fit target; measure it now. |
| 15 | merge | spec-gap | 1461 | “making every LFO number fit its box and finishing the merge copy”. Make LFO values fit and finish the integration copy. R14 had just measured ordinary-field fit without an explicit ordinary-number width target in the cited order. This continues that sizing decision; the merge copy is only one part of the interval. |
| 16 | gate | mechanical | 1500 | “fixing the rear-panel click test on the merged code”. Repair the click test against merged code. |

### 8. Notice fixes: splash word, Frame time at Home, Unfold word, off Screen rim

Plan: `tool-based-ui-startup-polish.md`. Run `e54c80d4-ad05-4d5a-b824-67d79e7f1131`, phase `58`, instance `f6df4742-9014-46ed-85e2-b4e35c5d8e66`. **15 rounds; 4 findings; 126,104 fix seconds (35h01m); 12 rounds past landing.** Start: 2026-10-17T13:47:34.776+00:00.

Start boundary limitation: the last saved plan before the start, `8aea487fbfe6938fa16e0b090237b9012cbd5d5f:docs/hana/tool-based-ui-startup-polish.md`, has no phase 58. The first recovered saved version containing it is `dda378212` on October 20, after the October 17 start; its order begins at line 2089 in `/home/natepiano/rust/tool-based-ui-startup`. It names splash word, Frame time at Home, Unfold word and the off-Screen rim. It also records the later Mac clipping report. Scope assessments against that saved order are provisional, not proof of the exact start-time wording. Annunciator, Screen zoom and Control Panel work are separate from those four named notices.

Round and finding source: [run record](/home/natepiano/.local/state/plan-delegate/runs/e54c80d4-ad05-4d5a-b824-67d79e7f1131.jsonl); select the instance above. Activities below are recorded text, not reconstructed commands.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | gate | fix-failed | 34490 | “fixing the rim on the Solid floor and Frame time's bottom edge”; “making the splash test see the drawn gap and settling the spacing change”; “fitting Frame time to a row edge with no blank band”. The held notice still failed rim/row-edge checks; F001–F004 identify wrong Solid-floor scope, clipping and an unasked spacing change. |
| 2 | merge | mechanical | 7908 | “repairing the Frame time stage after the trunk merge”. Adapt Frame time to the trunk merge. |
| 3 | merge | mechanical | 1573 | “repairing Screen centring after the inspectors merge”. Adapt Screen centring to the inspectors merge. |
| 4 | other | out-of-scope | 14213 | “fixing the smeared Faults text”. Faults text is outside the four subjects in the later saved order; its exact relation to the start-time scope is unverified. |
| 5 | other | out-of-scope | 1288 | “fixing Annunciator rejudge, EnvironmentDrive fixtures and chooser playback”. Update Annunciator/EnvironmentDrive/chooser fixtures after integration. The later recovered order names four notice subjects; Annunciator rejudge and chooser playback are separate behavior repairs, alongside fixture work. As with R4/R7/R10, this scope assessment is provisional because no timestamp-exact start order survives. |
| 6 | other | fix-failed | 16051 | “fixing the Annunciator quiet-frame rejudge”. Quiet-frame rejudge still needed repair after the prior pass. |
| 7 | other | out-of-scope | 1456 | “fixing the Screen jack panel zoom radius”. Screen jack-panel zoom radius is separate from the switched-off rim contrast named in this phase. |
| 8 | gate | mechanical | 1218 | “fixing the 12 wider test failures”. Repair 12 wider-suite failures. |
| 9 | gate | fix-failed | 1305 | “fixing the last three wider test failures”. Repair the last three wider-suite failures. The last three are the wider test failures from R8 that remain after that interval; this is continuation of the same failed test batch. |
| 10 | other | out-of-scope | 1840 | “fixing the Control Panel waits and the floor control”. Control Panel waits and a floor control expand beyond the four notice fixes. |
| 11 | gate | mechanical | 614 | “finishing lint and the full test line”. Finish lint and the full test line. |
| 12 | gate | mechanical | 7956 | “fixing two lint errors, then the full test line”. Clear two remaining lint errors. |
| 13 | gate | record | 9609 | “rerunning lint, then the full test line”. A verification-only rerun of lint and the full test line, with no source repair named; counted as a fix worker interval by the API. |
| 14 | other | fix-failed | 10169 | “fitting Frame time's bottom to its text”. Frame time bottom-to-text fit remained wrong; the later saved Work Order records the Mac clipping report. |
| 15 | other | out-of-scope | 16414 | “making the splash bars only move forward”. Make splash progress monotonic. The activity does not establish which earlier repair introduced the backward movement. The later recovered order names splash-word appearance, not progress-bar direction. This activity repairs a different behavior without identifying an earlier repair that caused it. The missing exact start order makes this scope allocation provisional. |

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
| 1 | review-code | spec-gap | 10147 | “two seats repairing what review found”; F001: old Screen saves fail after text labels become an enum, alongside F002–F017. Saved-label compatibility is absent from the start order. |
| 2 | review-code | fix-failed | 5447 | “sizing the closed Viewfinder words and running the unrun drawn tests”; F007: words below 9 px, and F008: incomplete drawn acceptance, remain open after R1. |
| 3 | review-code | fix-failed | 821 | “the closed Viewfinder card grows as one piece”; F007 still_open after attempt 2, accepted after attempt 3. |
| 4 | review-code | fix-regressed | 2547 | “eleven closed-card unit tests”; F018 explicitly reports that whole-card scaling fails 11 unit tests. |
| 5 | review-code | mechanical | 18675 | “testing the centred close Camera On help overlay pixels” / “making a closed Effects assembly selectable”; F019 is titled “Merge: jack row sizing disagrees with widget slider rules” and F020 “Merge: camera card and open assembly tests fail” (JSONL 489–490). This is integration against existing shared sizing/picking contracts, not a new target choice; the ledger priority leaves its immediate cause review-code. |
| 6 | review-code | fix-regressed | 2859 | “finding why the Camera switch row gets no pointer hit”; F021 explicitly says the repair regressed two Camera title tests; F019/F020 remain. |
| 7 | review-code | fix-failed | 2715 | “fixing the missing-camera pixel test on the taller jack panel”; F019/F020/F021 still_open. |
| 8 | review-code | fix-failed | 2612 | “restoring the closed panel's top edge when its caption row goes away”; the F019/F020/F021 batch remains open. |
| 9 | review-code | fix-failed | 1892 | “finishing the missing-camera test and the wider jack panel test run”; F019/F020 remain; F021 is eventually accepted. |
| 10 | review-code | mechanical | 2578 | “resolving the merge conflicts and fitting the jack panels to the shared panel style”; F022 names catalyst merge 4c56bba4c; F020 remains. |
| 11 | review-code | mechanical | 12 | “fitting the jack panels to the merged control sizing”; F023 names catalyst merge 1b19832a1, five conflicts and three picture-test failures. |
| 12 | review-code | fix-failed | 5807 | Same recorded activity as R11; F020/F023 still_open. |
| 13 | review-code | fix-failed | 1506 | “fitting the newest merge and the Material group spacing”; F020/F023 still_open again. |
| 14 | review-code | fix-failed | 1386 | “making the Material jack panel even”; F023 accepted on attempt 4 and F020 on attempt 10. |

Hard landing: R4–R14 concern named failing pixel/slider/Camera tests, repair regressions, or merge conflicts and picture-test failures. These are kept-work cases; the rule permits this tail. Ledger precedence leaves merge work labelled review-code.

### 10. Selection face and glow

Plan: `tool-based-ui-inspectors.md`. Run `8777d55e-0ac2-4586-94c5-c7c309274d22`, phase `10`, instance `a5f11f1e-1f26-46d5-bf61-7dc3fead4d05`. **13 rounds; 14 findings; 31,968 fix seconds; 10 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/8777d55e-0ac2-4586-94c5-c7c309274d22.jsonl), joined by this instance. Start Work Order: `dae29fabc9f9:docs/hana/tool-based-ui-inspectors.md:340–368`, in `/home/natepiano/rust/hana_catalyst-inspectors`. The order names Selection, tool registration and live updates, but no scene-replacement or real startup-order regression test. Findings F001–F004 open at run-file lines 633–641.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 1296 | “writing Phase 10 review regression tests” / “repairing Phase 10 review findings”; F001: scene replacement loses controls; F002: hovered outline keeps old width; F003: fixtures lack SelectionLook. These lifecycle/setup tests are absent from the order. |
| 2 | gate | mechanical | 1027 | “fixing cable hover test compile errors and running the repair tests”. |
| 3 | gate | mechanical | 1986 | “running lint and the repair tests after the hover test fix”. |
| 4 | review-code | fix-regressed | 3592 | “making the Selection tool register in the app's plugin order”; F004: real app startup panics on the new environment-tool registration. The causal link is to phase registration; the exact preceding repair contribution is not isolated. |
| 5 | review-code | fix-failed | 3025 | “fixing the plugin order test setup”; F004 still_open after its first fix attempt. |
| 6 | review-code | fix-failed | 1665 | “routing the plugin order test through main's registration”; F004 later accepted on attempt 2. |
| 7 | gate | mechanical | 3586 | “resolving the merge conflicts and running lint and tests”. |
| 8 | gate | mechanical | 2481 | “adding the missing setup, then lint and tests”. |
| 9 | gate | mechanical | 107 | “adding the resource to three fixture modules, then lint and tests”. |
| 10 | gate | mechanical | 2865 | “finishing two fixture edits, then lint and tests”. |
| 11 | gate | mechanical | 168 | “resolving five merge conflicts, then lint and tests”. |
| 12 | gate | mechanical | 4028 | “carrying one setup line into the moved plugin, then lint and tests”. |
| 13 | other | fix-regressed | 6142 | “finding why the ring drag no longer turns the cube”. Lost behavior supports a provisional regression label; the activity does not identify which earlier patch caused it. |

Hard landing: R4–R12 concern the real-app startup panic or compile/lint/fixture/merge test failures. R13 reports lost ring-drag behavior, but does not establish phase ownership or the causal patch, so its eligibility is uncertain.

### 11. Control Panel fold cube

Plan: `tool-based-ui-inspectors.md`. Run `8777d55e-0ac2-4586-94c5-c7c309274d22`, phase `7`, instance `c8684080-72c8-4b8a-a3ac-9bc5cfdfcf0e`. **13 rounds; 16 findings; 28,864 fix seconds; 10 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/8777d55e-0ac2-4586-94c5-c7c309274d22.jsonl), joined by this instance. Start Work Order: `54ae76755c15:docs/hana/tool-based-ui-inspectors.md:246–279`, in `/home/natepiano/rust/hana_catalyst-inspectors`. The order explicitly names reversible 450 ms Pane/Cube/Cross motion, no crossed faces, Faults clearance and pixel-size expectations (lines 258–275). The overall visible contracts were explicit; R1 identifies a missing playback-synchronization test rather than treating their correction as mechanical.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 5685 | “repairing Phase 7 review findings”; F006: intersecting arms; F007: wrong Cross after reversal; F008: camera/playback desynchronization; F009: unequal margins; F001–F005 concern style/representation. The start order already requires the visible behavior. The cited order specifies synchronized 450 ms camera/fold motion and tests shape/reversal outcomes, but names no sampled camera-versus-fold playback synchronization test. F008 exposes that missing test case; the other visible requirements were explicit and are not relabelled as absent goals. |
| 2 | review-code | fix-failed | 1051 | “finishing the Phase 7 repairs”; the same F001–F009 batch remains in flight. |
| 3 | review-code | record | 0 | Ledger-only dispatch, no paired worker, for F010 invisible selected panel, F011 mirrored side-centred Cross, F012 overlap with Frame time and F013 AnchorPose warnings. A new batch alone does not establish a serial live check or a completed patch. |
| 4 | review-code | fix-regressed | 207 | “repairing the Phase 7 fold reversal”; F014: reversal can reverse an unrelated camera journey. |
| 5 | review-code | fix-failed | 5648 | Same recorded fold-reversal activity and F014; another worker interval before acceptance. |
| 6 | review-code | spec-gap | 131 | “repairing the Phase 7 back-face text”; F015: hidden back-face text draws through the front. The visible defect needs correction, but this activity alone does not prove a serial live-check sequence or a prior repair regression. The cited order requires hidden faces to be invisible and unpickable, but its named Pane/shape tests do not require a rendered text-occlusion check. F015 names that missing rendered-text case; a code correction is required, not just a test update. |
| 7 | review-code | fix-failed | 156 | Same recorded back-face-text activity and open F015. |
| 8 | review-code | fix-failed | 101 | Same recorded back-face-text activity and open F015. |
| 9 | review-code | fix-failed | 57 | Same recorded back-face-text activity and open F015. |
| 10 | review-code | fix-failed | 513 | Same recorded back-face-text activity and open F015. |
| 11 | review-code | fix-failed | 1689 | Same recorded back-face-text activity and open F015. |
| 12 | review-code | fix-failed | 4469 | Same recorded back-face-text activity and open F015, ultimately accepted. |
| 13 | review-ux | spec-gap | 9157 | “repairing the Phase 7 side-face fold”; F016: non-uniform cube-root scale prevents Cross. The cited start order tests side-face rooting and front-facing squares, but has no non-uniform-root-scale case. JSONL 420–442 records one F015 repair/acceptance before F016 at 447; it does not identify F015 as a live-check finding or prove its fix introduced F016. The root is the missing scale test, not an inferred serial live-check violation. |

Hard landing: R4–R5 concern F014: fold reversal reverses an unrelated journey. R6–R13 are visible own-change defects but the cited evidence does not establish the preceding patch or a pre-phase comparison. Their eligibility is uncertain. The seven F015 dispatches include continuations; the ledger records one accepted fix attempt, not seven rejected patches.

### 12. Startup animation: the Unfold tool

Plan: `tool-based-ui-startup-polish.md`. Run `df477c65-1d02-4c11-8810-b61c83db1f2c`, phase `22`, instance `85cfb642-87cf-4e6b-937c-95e4d9bd31b3`. **12 rounds; 34 findings; 87,437 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/df477c65-1d02-4c11-8810-b61c83db1f2c.jsonl), joined by this instance. Start Work Order: `26fdfb72edf6:docs/hana/tool-based-ui-startup-polish.md:751–795`, in `/home/natepiano/rust/tool-based-ui-startup`. The order explicitly requires real creation below 5 ms, correct tests/wiring, bounds, cabled words and whole-word framing. It says no timing test exists to copy (line 793). The 5 ms target was explicit, but choosing its measurement interval remained unresolved and required judgment in the first batch. F001–F012 are at run lines 161–172; F032–F034 at 671 and 684–685.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 8083 | “repairing the Unfold tool's placement, fold direction and word routing”; F004 times only the pre-build update, F005 uses fabricated bounds, F006 checks CPU normals rather than rendered shading. The goal and test/wiring obligations were explicit; the timing interval needed choosing. The cited order gives the 5 ms goal but explicitly says no timing test exists to copy (line 793), without defining which app updates the interval includes. F004 times the wrong update and chooses the fastest sample. That measurement definition is the primary gap for this mixed code/test batch, not an absent creation-cost goal. |
| 2 | review-code | fix-failed | 38886 | “font cache warm-up for creation cost” / “fixing gap closing in the square shader”; F002/F004/F007–F010 remain; F004 still_open on attempt 2; F013–F015 are added. |
| 3 | review-code | fix-failed | 3420 | “measuring Create Unfold's own cost against an idle update”; F002/F004 remain, with F016 staged Home bounds and F017 unbounded regression loop. |
| 4 | review-code | fix-failed | 2858 | “pinning the Unfold tool's own creation cost”; F004 accepted on attempt 4. |
| 5 | review-ux | spec-gap | 5427 | “fixing the design-check defects on the Unfold tool”; F018–F023 form one batch: absent body/cable, missing shield bounds, crowded labels and unreadable square-size value. The cited order names the face controls, word shading and whole-word framing tests, but no rendered body/cable visibility test or drawn square-size readout floor. Those missing acceptance checks support the primary gap assessment; six findings in one batch do not establish serial-check. |
| 6 | review-ux | spec-gap | 7009 | “fitting the Unfold cube to its panel and landing the cable on its jack”; F024–F028 arrive together: cable on floor, oversized case, word/frame collision, displaced gizmo and shield warning. The cited order gives a fixed word offset but no numeric word/case clearance or case-to-panel size target. The primary gap is that fit target; new IDs do not prove the preceding fix introduced these defects, and this is a batched check. |
| 7 | review-code | mechanical | 2429 | “merge repair: Square size display and word clearance”; F029/F030 explicitly follow catalyst merge 8ccc053b7. |
| 8 | review-code | fix-regressed | 5148 | “measure and fix the Create Unfold slowdown”; F031 explicitly reports post-merge whole-click cost as high as Create Shape. |
| 9 | other | fix-failed | 8254 | “interleave the Shape and Unfold whole-click samples” / “find why the palette draws no panel background”; whole-click measurement continues; F032 separately names missing palette background after the Phase 22 merge. This interval has mixed work. |
| 10 | other | spec-gap | 3470 | “make the open word clear the case and lie flat”; F033 word crosses the case top rail; F034 open word is not one flat sheet. The cited start order names whole-word framing and a fixed offset, without a drawn case-clearance floor or a planarity acceptance test. Both findings were sent together; no one-defect live-check sequence is shown. |
| 11 | gate | record | 192 | “finish the round10 gates and summary”. Only gate execution and summary work are named; no test update or source repair is recorded. |
| 12 | other | fix-failed | 2261 | “repair round11: the open word draws as one clean sheet”; unresolved flat-sheet case F034 continues. |

The original `/home/natepiano/rust/tool-based-ui-startup-polish` checkout is absent; the historical order is available through the shared git history in the startup checkout named above.

Hard landing: R4, R7 and R8 have budget/test, merge or explicit post-merge slowdown evidence. R11 only executes gates and writes the summary, so it is a record row. R5–R6/R9/R10/R12 are uncertain: fresh visual defects or mixed benchmark/palette work lack a before comparison for every part. Blocker severity alone does not establish retained-category eligibility.

### 13. Twin camera wording, first drag, and live chooser placement

Plan: `tool-based-ui.md`. Run `7b115092-80c3-42c2-9652-2ff222d2c722`, phase `137`, instance `b73e8bda-2516-424e-b466-5cdb7e424a02`. **12 rounds; 10 findings; 76,272 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/7b115092-80c3-42c2-9652-2ff222d2c722.jsonl), joined by this instance. Start Work Order: `a43bbfe13049:docs/hana/tool-based-ui.md:4178–4206`, in `/home/natepiano/rust/tool-based-ui-trunk`. The order requires a contained, reachable crowded long-list chooser that does not cover Open, and separates the deterministic gate from deferred live checks. It gives no minimum usable chooser width, equal-gap target or empty-device-message width cap. The final start is unfinished and has zero observed time.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 720 | “repairing where the device chooser opens”; F001: no opening room when the card fills the window; F002: unreachable labels in a 12 px chooser. Containment alone does not define usable dimensions. F003/F004 are nits. |
| 2 | review-ux | spec-gap | 3114 | “repairing the device chooser's width and gaps”; F005: no-camera message widens chooser to 732 px; F006: unequal gaps; F007: crowded chooser covers Screen/Power. The first two lack measured targets. |
| 3 | review-ux | mechanical | 141 | “running the chooser repair's lint and tests”; ledger priority leaves cause review-ux under F005–F007. |
| 4 | review-ux | fix-failed | 21063 | “fixing three visible defects, then the chooser lint and tests”; same F005–F007 batch. |
| 5 | review-ux | fix-failed | 2845 | “fixing the defects the live chooser shots showed”; F007 still_open after attempt 1; F008–F010 add Output whitespace/alignment and HUD overlap. |
| 6 | review-ux | fix-failed | 19240 | “resuming the chooser repair after the move”; same F007–F010 batch; continuation does not prove a separate failed patch. |
| 7 | other | fix-failed | 4532 | “repairing the Output chooser and Open alignment”; the visible placement work continues without a matched ledger batch. |
| 8 | review-code | fix-failed | 4822 | “finishing the device chooser repair”; reused fix_pass attaches already-accepted F001–F004. That association does not prove reopening; continuity is inferred from the activity. |
| 9 | gate | record | 10344 | “rerunning the device chooser gates”. Only rerunning gates is named; no test update or source repair is recorded. |
| 10 | other | out-of-scope | 6263 | “fixing the rounded box landing and the crowded chooser shot”; rounded-box landing is outside the twin-wording/first-drag/chooser order. The same interval includes in-scope work; its whole time is an upper bound on scope spill. |
| 11 | gate | mechanical | 3188 | “clearing its lint findings”. |
| 12 | other | fix-failed | 0 | “fixing the Output card and crowded chooser”; unfinished continuation, no observed finish or invented time. |

Hard landing: R9 only reruns gates and is a record row; R11 clears lint. R4–R8 and R12 are uncertain: repeated chooser findings and reused ledger numbers do not prove retained regressions. R10 is mixed: rounded-box landing is outside scope, while the crowded-chooser shot is verification; the whole dispatch is uncertain for eligibility. Its 6,263 seconds are an upper bound for outside-scope work.

### 14. The Arrange jacks glyph matches its neighbours in size

Plan: `tool-based-ui.md`. Run `576fc590-6052-460f-a146-43c3ddb433b9`, phase `111`, instance `ba43a6d3-20d9-41bd-862d-5ffd3cc825b0`. **12 rounds; 15 findings; 56,771 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/576fc590-6052-460f-a146-43c3ddb433b9.jsonl), joined by this instance. Start Work Order: `e8f5b8524848:docs/hana/tool-based-ui.md:3475–3516`, in `/home/natepiano/rust/tool-based-ui-trunk`. The order sets the glyph to 24–26 px, a common drawn/hit/framed envelope, tooltip at least 9.5 px and flat dimmed strokes. It does not name repeated-Arrange reparenting or its test, nor a policy for the tall screwdriver's contribution to arranged framing.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | spec-gap | 4267 | “fixing the glyph's lint errors and halo test, then shooting it”; F001: repeated Arrange detaches the glyph; F002: envelope-selection test never calls select_jack_targets. Repeated-operation proof is absent from the order's test list. |
| 2 | review-code | fix-failed | 12753 | “repairing repeated Arrange Jacks”; F001/F002 remain in flight. |
| 3 | review-code | fix-failed | 3161 | “finishing the repeated Arrange repair”; F001/F002, with no accepted closure between these intervals. |
| 4 | review-code | mechanical | 271 | “fixing a test lint and taking the Arrange Jacks shots”. |
| 5 | review-code | record | 629 | “running the Arrange tests and taking the Arrange Jacks shots”. Only running tests and taking shots are named; no test update or source repair is recorded. |
| 6 | review-ux | spec-gap | 5074 | “fixing the Arrange framing and row placement”; JSONL 32–33 sends F003 frame 2.4 times taller than row and F004 earlier row showing through as one batch. The cited glyph order defines its envelope but no arranged-view picture-share/height target or placement policy for repeated rows. Those missing fit/placement criteria support this root; the two findings do not establish serial-check. |
| 7 | review-ux | spec-gap | 5713 | “fixing the frame margins and the stray body in the arranged view”; JSONL 41–44 accepts F003/F004, then sends F005 margins 35 px horizontally versus 22.6 px vertically and F006 clipped coral slab together. The cited order gives glyph/tooltip dimensions, not an opposite-margin target or a whole-scene-subject rule for the arranged view. This is a batched fit decision, not one defect sent per check. |
| 8 | review-ux | spec-gap | 3162 | “framing the arranged row closer and centring it”; JSONL 51–54 accepts F005/F006 and sends F007 unreadable jack labels with F008 loose/off-centre view. The cited order gives a 9.5 px tooltip floor, but no arranged jack-label floor or picture-share/centering tolerance. The missing targets support this root; the two-finding batch is not serial-check. |
| 9 | review-ux | spec-gap | 2446 | “sizing the selection screwdriver on screen and centring the arranged row”; JSONL 62–65 accepts F007/F008 and sends F009 frame 5 px above status with F010 tall screwdriver pushing the row low. The cited order has no screwdriver contribution/size policy for arranged framing or numeric status clearance. The two-finding batch needs those decisions; it does not show serial-check. |
| 10 | review-ux | fix-failed | 3085 | “sizing the screwdriver on screen like the gizmo”; F010 still_open after attempt 1, accepted on attempt 2. |
| 11 | review-ux | out-of-scope | 14637 | “turning the screwdriver to face you and lining up the corner controls”; JSONL 83–87 accepts F010 and sends F011 shaft contrast 1.97:1, F012 edge-on 4.7×22.8 px, F013 screwdriver off centre and F014 floating glyph together. The cited glyph order lists rotate files for the shared ray only, not screwdriver material contrast or orientation. This primary scope allocation includes in-scope F014, so 14,637 seconds is an upper bound on outside-scope work. Four findings were batched; none proves serial-check. |
| 12 | review-ux | fix-failed | 1573 | “laying the screwdriver at an angle, evening the corner insets, and wrapping long rows”; F012/F013 accepted on attempt 2; new F015: six-tool labels below 9 px. |

Hard landing: R4 updates test lint with verification shots; R5 only runs tests/takes shots and is a record row. R6–R12 are successive visual findings and continued F010/F012/F013; exact pre-phase comparisons and causal patches are absent. Their retained-category eligibility is uncertain. The checks batched two to four findings each; this is not evidence of the one-defect serial-check pattern or of a violation of item 7.

### 15. Into the room follows the picture, and each effect step frames its subject

Plan: `info-panels-plan.md`. Run `f205ddd0-5865-4dec-9b85-849e4ffa3dd8`, phase `33`, instance `0661966c-4a96-4c0f-9304-57d36a6701cb`. **12 rounds; 12 findings; 3,591 fix seconds; 9 rounds past landing.**

Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/f205ddd0-5865-4dec-9b85-849e4ffa3dd8.jsonl), joined by this instance. Start Work Order: `e74844df81f1:docs/hana/info-panels-plan.md:864–883`, in `/home/natepiano/rust/tool-based-ui-demo`. The order requires even margins and readable effect framing but supplies no numeric picture-share target or opposite-margin ratio. Independent evidence: [attempts log](/home/natepiano/.claude/projects/-home-natepiano-rust-hana/memory/attempts-p33-effect-framing.md), numbered attempts 1–4. Its attempt numbers differ from API intervals; October 19 entries concern later work, not this phase 33 record.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | mechanical | 124 | “fixing the three proxy-cleanup test expectations”; F001: three cleanup expectations fail after camera-path changes. |
| 2 | review-code | spec-gap | 565 | “fixing the effect-step margin gate and the follow guard”; F002 compares normalized units rather than pixels, F003 assumes 50 ms, F005 returns 0.9 without proving balance. These measured thresholds are absent; F006/F007 concern helpers/constants. |
| 3 | review-code | fix-failed | 629 | “opposite-margin gate,100ms follow guard, dead helper removal”; F002/F003/F006 still_open after attempt 1, accepted on attempt 2. |
| 4 | review-ux | spec-gap | 562 | “fixing posterize framing by panning the fit centre”; F008 picture below 15% after zoom-out balancing. Attempts 1–2 record the shrinking picture and share 0.1432. The start order has no 15% target. |
| 5 | review-ux | mechanical | 92 | “typing six test values for basedpyright”; same F008 ledger association. |
| 6 | review-ux | spec-gap | 602 | “measuring the beam by its real points”; F010 rotated local Aabb inflates the subject. Attempts entry 3 measures apex/corners and still gives picture share 0.1476. The cited order has no real-beam-corner measurement test; F008 and F009 were already separate findings before F010 (JSONL 573, 576, 584), so this is not evidence that a live check sent only one defect, fixed it, then found the next. |
| 7 | review-ux | record | 0 | Ledger-only dispatch for F011: 4 m cable-culling margin, and F012: cut Material tool. Attempt 4 identifies the margin. No worker time is observed; this dispatch alone does not prove a completed repair or serial live-check violation. |
| 8 | review-ux | fix-failed | 794 | “removing the cable culling margin and the cut tool”; F011/F012 worker starts 22 seconds after the ledger dispatch. This is continuation, not proof of another failed patch. |
| 9 | review-ux | fix-failed | 72 | “choosing a cut-free follow radius in either direction”; F012 still_open. |
| 10 | review-ux | fix-failed | 0 | Ledger-only F012 dispatch; no paired worker or inferred time. |
| 11 | review-ux | fix-failed | 0 | Another ledger-only F012 dispatch; no paired worker or inferred time. |
| 12 | review-ux | fix-failed | 151 | “choosing a cut-free follow radius in either direction”; F012 accepted on fix attempt 3. |

Hard landing: R5 is basedpyright follow-through. R4 and R6–R12 are uncertain: a new picture-area floor and successive beam/cable/Material framing checks do not establish which preceding patch caused each defect. Three post-landing ledger-only intervals have no measured worker time; nine later intervals are not nine independently rejected patches.

## Mixed records: hand boundary audit

These three records contribute 138 measured rounds, kept outside the 15-record root-cause/time table. A mixed flag is a reason to inspect boundaries, not a finding that every round is spurious. Missing starts cannot be reconstructed from current plan numbers alone.

### Mixed 1. Recorded phase 6: palette highlighting and later work

Plan: `rich-text-plan.md`. Recorded title: “Palette rows highlight the matched lette” (report display, truncated to 40 characters). Run `189487ea-6e3b-48f0-896d-c6f40023c0d2`, recorded phase `6`, instance `6cd97cd6-aeb4-4fff-a4a2-5fffd7096870`. **56 rounds; 7 findings; 415,639 fix seconds; recorded past_landing=53.** Source: [run record](/home/natepiano/.local/state/plan-delegate/runs/189487ea-6e3b-48f0-896d-c6f40023c0d2.jsonl), joined by this instance.

Its only phase start is phase 6 on October 12 at 16:16 UTC; there is no later start or finish. Start Work Order: `42a9d691c9310ff7eca3f982c0d2a606e1e20051:docs/hana_diegetic/rich-text-plan.md:211–263`, in `/home/natepiano/rust/widget-enhancements`: palette matches through Rubric, with named tests. Switches, Material layout, scrolling, picking and info icons are outside that order.

Hand split (original interval numbers; mixed-purpose intervals stay whole):

| Work identified in the activities | Rounds | Count |
| --- | --- | ---: |
| Phase 6 / palette; R1/R15 also contain controls work | R1, R2, R14, R15, R27, R29 | 6 |
| Shared field/switch/rear-card/Material work; exact phase unresolved | R3–R13, R16–R26, R28 | 23 |
| Explicit phase 7 and its scroll indicator | R30–R33 | 4 |
| Explicit phase 14, then text bridge and projected-width/scroll-gate work | R34–R38 | 5 |
| Info icon, with some shared slider/drag work | R39–R52, R54, R56 | 16 |
| Merge integration | R53, R55 | 2 |
| **Total** | | **56** |

Current `rich-text-plan.md` calls phase 7 “Panels scroll like macOS, with a fading indicator”; at the start, phase 7 was the palette footer. The later as-built plan places text-hook/size-gate features under phases 15–16 (lines 427–460) and info icons under phase 17 (462–478). Activity names and features are stronger evidence than retroactively applying current numbers. Phase 18's later number-field heading does not assign earlier batches to it. R46–R47 remain shared work. `record` means the recorded phase cannot supply a defensible repair boundary/root, not that later features required no work.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-code | record | 21237 | Concurrent activities mix palette port, rich title box, switch thumb and number-field Enter handling; F001–F007 include saved-tool click persistence and controls outside palette highlighting. |
| 2 | gate | fix-failed | 4225 | Activity: saved Tool-row click persistence plus Off-switch band tests. R1 already covers saved-tool click persistence in F001–F007; this activity continues that unresolved persistence repair while also running switch-band tests. |
| 3 | gate | mechanical | 2932 | Activity: update five tests for body-label gap. |
| 4 | gate | mechanical | 756 | Activity: switch tests and clippy. |
| 5 | other | record | 1533 | Activity: Unfold rear-label Text gap, outside the palette order. |
| 6 | other | record | 3042 | Activity: change every rear-jack label gap to a projected rule; no separate start. |
| 7 | other | record | 954 | Activity: project the jack's real silhouette; no phase boundary. |
| 8 | gate | mechanical | 694 | Activity: merge-gate clippy follow-through. |
| 9 | other | record | 20444 | Negative-sign commit, switch-knob OIT depth and caret padding share one recorded dispatch. |
| 10 | other | record | 1340 | Activity: make text and number values one field look. |
| 11 | gate | record | 4127 | Activity: one-click text value and opaque-text OIT depth test. One-click fields and text-depth behavior are mixed without an actual feature start. The test mention does not make the substantive behavior change mechanical, and the missing boundary prevents a phase-specific root. |
| 12 | gate | record | 2461 | Activity says six tests the “branch broke”; the causal patch is unidentified. The missing actual feature boundary and unidentified causal patch prevent a phase-specific root; failure of six tests alone does not establish a judgment-free expectation update. |
| 13 | other | record | 2696 | Activity: right-align Square Size, outside palette highlighting. |
| 14 | other | fix-failed | 4076 | “repairing Phase 6 review findings” continues the recorded repair; no new finding IDs identify a different defect. |
| 15 | gate | record | 15932 | Palette Tab/click tests, Material menu, disabled-match dimming and field-height test share one dispatch. The interval mixes substantive palette/Material/field changes under the missing boundary; tests are not its only work and the original palette order cannot supply all these targets. |
| 16 | other | record | 110 | Activity: LFO plate/top-row heights after field changes; missing phase boundary. |
| 17 | other | record | 2172 | Activity: LFO rear-label height and even padding. |
| 18 | other | record | 2500 | Activity: Material popup rows take a full spacing step. |
| 19 | other | record | 4914 | Activity: restore Material card text to 9 px after larger row gaps. |
| 20 | gate | mechanical | 2786 | Activity: drawn-gap pixel test for Material fields/popups. |
| 21 | other | record | 1182 | Activity: button paint clipping and renderer coverage contract. |
| 22 | other | record | 23651 | Activity: rear-panel spacing scale and right number inset. |
| 23 | other | record | 451 | Activity: old spacing assertions and side-by-side card top alignment. The assertions and card alignment share an interval with no actual feature start; the substantive alignment correction is not established as a test-only update. |
| 24 | other | record | 7967 | Activity: card top alignment and LFO margins; no phase boundary. |
| 25 | gate | mechanical | 448 | Activity: two test patches in rear_patch_panel split scratch work. |
| 26 | gate | record | 5965 | Activity: spacing commit lint/tests and Material layout. Material layout and gate work are mixed under the missing boundary; the activity does not identify a test-only or conflict-only repair. |
| 27 | other | record | 9143 | Activity: palette refusal message gets its own line; not the start-time match-highlighting goal. |
| 28 | other | record | 5147 | Activity: validation of the spacing unit. Only validation is recorded, with no test update or source repair. |
| 29 | other | record | 3289 | Activity: same-frame modifier ordering for field keys; no separate start. |
| 30 | other | record | 3227 | Activity explicitly says “Phase 7 review findings”, not phase 6. |
| 31 | other | fix-failed | 2815 | Activity: phase 7 indicator/example continuation after R30. |
| 32 | other | record | 5743 | “verifying Phase 7 repairs”. Only verification is recorded, with no test update or source repair. |
| 33 | other | fix-failed | 5256 | Activity: indicator still needs to stay inside its border. |
| 34 | other | record | 4535 | “Phase 14 review fixes”; later plan numbering cannot recover the exact original order. |
| 35 | gate | mechanical | 8255 | Activity: text test-bridge skips. |
| 36 | other | record | 2222 | Plugin order, hand-built text alpha and guard cost match later splash/text-hook as-built work. |
| 37 | other | record | 6066 | Tilt width, per-camera state, scroll areas and threshold order match later size-gate work. |
| 38 | gate | record | 9128 | Activity: tilt test and two assembly-face test failures. The tests fail during later size-gate work, but no actual phase start or diagnosis separates stale expectations from a substantive regression. |
| 39 | other | spec-gap | 10819 | Info-icon review; later phase 17 as-built records first-shot too-small/high icon and tooltip/title overlap. Further visible sizing/placement judgment was needed; exact start target is not recovered. |
| 40 | gate | mechanical | 24 | Activity: tooltip test must require a visible tooltip. |
| 41 | gate | fix-failed | 5473 | Activity: same visible-tooltip test still being repaired after R40. |
| 42 | other | fix-failed | 4371 | Activity: world-root fallback repair/removal continues tooltip placement. |
| 43 | other | record | 4840 | Activity: named activation types. Choosing activation types is a code/type-design change, not identified lint or a test update. Its original feature boundary is absent. |
| 44 | gate | fix-failed | 17203 | Info-icon size/centre/tooltip placement continues, with three slider test failures. |
| 45 | gate | fix-failed | 7536 | Activity: thumb bounds/helper control kind and tooltip clearance below card text. R44 still repairs info-icon/tooltip placement; R45 continues its unresolved clearance and bounds work. The test/helper part does not make the whole interval mechanical. |
| 46 | other | record | 6752 | Scale-mode jack-panel refit and value-drag states share a dispatch; exact phase unresolved. |
| 47 | other | record | 1917 | Floating-point noise causes card rebuilds; phase ownership unresolved. |
| 48 | other | fix-failed | 10273 | Info-button size and tooltip fallback continue the same placement goal. |
| 49 | other | fix-failed | 989 | Activity: tooltip depth still needs repair. |
| 50 | other | fix-failed | 19309 | Activity: fallback tooltip gap still needs repair. |
| 51 | other | fix-failed | 22151 | Activity: tooltip gap still fails in tilted views. |
| 52 | gate | fix-failed | 50458 | Info-button hover after camera movement and test follow-through; no independently identified new goal. R48–R51 continue the unresolved info-tooltip placement/depth goal; this interval still repairs hover after camera movement, as well as tests. Continuity is inferred from the activities, not a separately recorded failed patch. |
| 53 | merge | mechanical | 14369 | Activity: six catalyst merge conflicts. |
| 54 | gate | mechanical | 35655 | Info-button work against catalyst merge plus failing Mac slider-fill test. |
| 55 | merge | mechanical | 79 | Activity: merge-blocker patch. |
| 56 | other | record | 0 | Overview click hits a rear jack; unfinished start, no observed finish/time. |

Landing check: The recorded past_landing=53 is applied across missing phase boundaries, not 53 extra palette repairs. Gate/merge rows name tests or integration that can qualify if phase-caused. The long info-icon tail is real, but no reliable actual-phase landing ordinal can be reconstructed. All recorded R4–R56 landing positions are therefore marked record in the cross-case check.

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
| 1 | review-code | spec-gap | 4601 | “phase 20 census findings” / “drawing findings”; F001 shim PID/chdir/flags, F006 descendant count, F007 summary minimum and F008 shared PID expose census/fixture cases beyond the pictured examples, alongside direct rule misses. |
| 2 | review-ux | fix-failed | 4123 | F001/F007 remain; F010 reserved cut-mark cells, F011 reader fixture, F017/F019 helper pipeline and F021 narrow command join the batch. |
| 3 | review-ux | fix-regressed | 5936 | F022 six helper tests fail, F023 mend rewrite does not resolve, F024–F028 draw/layout/test regressions. F032 is an older 214×93 moving-pane buffer panic and F033 a lost command tail. Log 742–744 deliberately adds the old panic to avoid a same-file collision; that part is out of scope. |
| 4 | review-ux | fix-regressed | 4029 | F035 double synchronous demand/requeue, F036 extra passes in every crate, F037 cut subcommand follow the changing draw/measurement path. |
| 5 | review-ux | spec-gap | 3343 | Next checked build sends F038 slow toast test, F039 readable directory heading cut and F040 glued argument mark together. The start order gives account/cut-mark rules, but no minimum retained directory-heading length; F039 needs that acceptance choice. This mixed-batch primary gap assessment is not evidence of one defect per live check. |
| 6 | review-code | out-of-scope | 1869 | “fixing cargo-mend's crate:: rewrite under #[path] mounts” plus “repairing the toast-width motion test” (F038). External-tool time is at most this whole interval, not an external-only measurement. |
| 7 | review-ux | spec-gap | 5392 | Next check sends F041: two child rows repeat the same 83.7% reading (JSONL 1479); activity “reading two rows / full lint”. Start item 15 names the parent/one-child rendering test, not a shared-ownership census fixture for two child rows. That missing test case supports the gap assessment. F041 is still_open at 1485 when F042/F043 are added, so it does not establish a fixed-one-then-next sequence. |
| 8 | review-ux | fix-failed | 2903 | Undo row-reading change; F041 persists with F042 blocked memory offset and F043 redraw taking 17 frames. |
| 9 | review-ux | spec-gap | 2299 | Next audit sends F044 ownership, F045 freeze ignores q and F046 lost subcommand together. The cited start order has drawing/census examples but no bounded grid-iteration or quit-responsiveness test. F045 is the missing operational acceptance case; three new IDs alone do not prove a prior patch caused all three or establish serial-check. |
| 10 | review-ux | spec-gap | 4682 | Nested-reading/state-column check sends F047 ordinary-frame budget, F048 duplicate child reading and F049 long-command memory drop together. Start item 15 names a parent/one-child value test, but no render-cost bound or nested-long-command fixture. The missing budget/combined case supports the primary gap assessment, rather than one-defect dispatches. |
| 11 | review-ux | spec-gap | 2464 | Frame-budget check sends F050 missing memory and F051 state cut while parent remains. The cited order says empty state must not displace a valued column, but gives no priority between a filled state and a parent column in this constrained layout. F051 exposes that missing choice; the two findings were batched. |
| 12 | review-ux | fix-failed | 1081 | “drop parent where state”; F051 remains covered. |
| 13 | review-ux | spec-gap | 4666 | Shared-column fit sends F052 unmarked argument cut and F053 blank state dropping valued columns together. The start order preserves a frame-wide grid and also requires removing an empty slot locally; it does not name an allocation test combining those two tables with argument truncation. The missing combined test is the primary gap assessment; the individual mark/value rules were explicit. |
| 14 | review-ux | target-moved | 3461 | F054 branch-only grid freeze plus F055 “drop the auto tile view (user)” (JSONL 1541–1542). Log 1172 adds auto-view removal, absent from the start order. |
| 15 | review-ux | fix-failed | 14369 | “measuring each cell lazily, once per height, and finishing the auto removal”; F054/F055 remain. |
| 16 | review-ux | mechanical | 128 | “finishing round 15 on the new account: two tests back under their round-13 time, then the gates”; same batch, not a fresh live finding. Log 1262 records account move. |
| 17 | review-ux | fix-failed | 941 | Remove extra measurement cost against round 13; F054/F055 remain. |
| 18 | review-ux | record | 115 | Finish measured grid-speed counts and hand back test commands. The activity reports measurements and hands back commands; it identifies no code repair or test update. |
| 19 | review-ux | record | 86 | Read Mac test times/request gates; same findings, no independent repair evidence. This is reading results/requesting gates, not an identified source repair or test update. |
| 20 | review-ux | mechanical | 2214 | Cargo-handler changelog, lint and four suites, still F054/F055. Log 1396/1419: timing and gates pass. |
| 21 | review-ux | fix-failed | 1972 | “repairing the endless row-sharing loop (F054)”; re-shoot freezes after green gates. Log 1422 identifies outer loop and lost round-13 termination check. |
| 22 | review-ux | mechanical | 1512 | Two drawing tests/gates for F054; log 1441 reports 27/27 freeze checks fixed. |
| 23 | review-ux | fix-regressed | 1984 | F058 command cut while optional columns remain; F059 shifted runs column. Log 1441 explicitly calls both regressions on the repaired build. |
| 24 | review-ux | spec-gap | 1380 | Next column check sends F060 stateless parent dropped and F061 Long memory drop together. The cited order supplies empty-state and individual cut rules but no priority/test for stateless ancestry versus valued Long columns under the shared grid. This is that missing combination case, not proof of one defect per check. |
| 25 | review-ux | fix-failed | 2314 | One-place column drop order; F060/F061 remain. |
| 26 | review-ux | fix-regressed | 4444 | F062 blocked state cut, F063 width-48 progress lost, F064 blank state slot. Log 1463: example order read as filled-state column; ten tests changed to match. Restore main's order/tests. |
| 27 | review-ux | spec-gap | 1846 | Main comparison finds F065 narrow tables drop duration/runs that main keeps (log 1570). The start order preserves Phase 14 optional columns, but names no main-versus-branch narrow-table duration/runs fixture; that missing compatibility test is the primary gap assessment. The next single-finding sequence is documented in R29. |
| 28 | review-ux | fix-failed | 220 | Follow-through on F065. |
| 29 | review-ux | serial-check | 851 | Log 1636 and its 11:41 EDT entry record the repaired live re-shoot followed by the width-40 progress/location failure. JSONL 1637/1639 sends only F065; workers repair it at 1641–1645 and 1646 accepts it. Only then 1647 opens F066 and 1648 sends only F066. These are the named single-finding repair/check steps supporting this label; the two-finding batches elsewhere are not counted as serial-check. |
| 30 | review-ux | fix-failed | 376 | F066 continues. |
| 31 | review-ux | spec-gap | 1125 | Next check sends F067 two frame gap widths and F068 adjacent Long cut marks together. The start order preserves frame-wide offsets but does not settle whether spacing is uniform per table or per whole frame. The later explicit per-table ruling at log 1679/R34 establishes the missing interpretation; this two-finding check is not serial-check. |
| 32 | review-ux | mechanical | 425 | Two explicitly approved one-mark expectations for F067/F068. |
| 33 | review-ux | fix-failed | 1039 | Main-gap/Long glued-mark repair continues F067/F068. |
| 34 | review-ux | target-moved | 1049 | “main's per-table gap rule”; log 1679 ruling C: do not force tight gaps across state frames; mixed gaps correct where main mixes. Showrunner clarification revises the preceding interpretation, not a new user request. |
| 35 | review-ux | fix-failed | 707 | Cells one gap, summary its own gap; F067/F068 continue. |
| 36 | review-ux | fix-failed | 600 | F068 remains with F069 blank row and F070 CI runs column missing. |
| 37 | review-ux | fix-failed | 1031 | Finish F070/gates; F068–F070 still covered. |
| 38 | review-ux | fix-failed | 2484 | F070 runs remains; F071 width-40 blank summary row drops main's location. |
| 39 | review-ux | spec-gap | 1344 | Main comparison finds F072 blank CPU where main has a reading (JSONL 1707). Start item 12 tests an unavailable reading as blank, but names no paired test that preserves an available CPU value under this column/width combination. That missing compatibility case is the primary gap assessment, not proof that the preceding CI-runs patch introduced the defect. |
| 40 | review-ux | fix-regressed | 1646 | JSONL 1707/1709 sends only F072; 1710–1711 repairs it and 1716 accepts it. A fresh shot review at 1718–1719 then reports one regression plus two defects shared with main. F073 cutfoot subcommand loss alone is sent to this phase at 1720–1721; the shared defects are routed to phase 25 (log 2054). Log 2054 explicitly calls F073 a phase-20 regression, supporting this root. The three-defect check does not establish the one-defect serial-check pattern. |
| 41 | review-code | fix-failed | 1451 | F074/F075 missing CPU readings graded with F072, continuing CPU issue. |
| 42 | review-code | fix-failed | 1204 | “round 28 follow-up restore blank CPU”; F074/F075 remain. |

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

**Ledger association limitation:** F001–F006 open October 12 at 03:31 (509–514), with ledger round 1 at 03:31:57 (517). F007–F009 open at 05:36–05:39, ledger round 2 at 05:39:38 (536–540). These are top-panel findings. Reused `fix_pass=1/2` also attaches them to October 8 R1/R2, October 10 R10/R22, and R38 before the findings existed. Actual top-panel work is R39/R40. The table preserves API causes, but does not use future findings as proof of earlier root causes. A temporal join change is outside word-list-only tuning.

| Round | Cause | Root | Fix seconds | Evidence and assessment |
| --- | --- | --- | ---: | --- |
| 1 | review-ux | record | 2448 | “convert shipped scene” to Beam input; old-scene compatibility is explicit. Attached top-panel findings did not exist yet. The phase-38 order already requires converting the shipped scene. The activity records that planned conversion under a repair label, not lint, a test update or merge work. |
| 2 | review-ux | out-of-scope | 1038 | Fit Material output row under title, matching phase 37 as-built output ordering. Attached F007–F009 did not exist yet. The hand split places this under phase 37 Material-output carry-over, predating the recorded phase-38 Beam order. This is substantive row fitting, not a test update. |
| 3 | other | fix-failed | 1266 | False Beam error on scene load persists after R1 conversion. |
| 4 | other | record | 607 | Activity: screenshots of the heaviest see-through panels, matching phase 39 alpha. It records a visual check, not a repair, and has no phase-39 start or single-defect/fix/next-defect sequence. |
| 5 | other | record | 1153 | Lone effect-tool pick path matches phase 42, outside Beam; original feature boundary/root absent. |
| 6 | other | fix-failed | 10495 | Extend same repair to every standalone effect body. |
| 7 | other | fix-failed | 1975 | Case registration for loaded effects continues effect-body repair. |
| 8 | other | record | 4477 | Off dimming must affect its own tree: phase 44 under the phase-38 record. |
| 9 | gate | record | 4959 | Darker/solid Off appearance and green tests. Off appearance is substantive phase-44 work with no phase-44 start here. Green tests do not turn its color decision into mechanical work. |
| 10 | review-ux | record | 21400 | Concurrent title lint, stale phase 41 tests, title-padding test and drawn-pixel failures. Later top-panel findings are not evidence here. This interval mixes actual title layout/pixel repairs with lint and stale tests, without a phase-41 start. The missing boundary prevents assigning a phase-specific root to the whole interval. |
| 11 | other | fix-failed | 5017 | Phase 41 drawn-pixel failure persists. |
| 12 | other | fix-failed | 2581 | “Phase 41 repairs” continue. |
| 13 | other | fix-failed | 577 | Diagnose title-bar suite failure. |
| 14 | gate | fix-failed | 5304 | Title-bar layout failures and test fixtures. R11–R13 already continue the failing title-bar suite; layout failures remain open here while fixtures are also updated. |
| 15 | other | fix-failed | 4730 | Title-bar regressions still being repaired. |
| 16 | other | fix-failed | 744 | Same title-bar regression activity continues. |
| 17 | merge | mechanical | 2674 | Land title-bar change on merge branch. |
| 18 | other | fix-failed | 332 | Phase 41 repair on shipped title bar continues. |
| 19 | gate | mechanical | 689 | Activity concerns getting Phase 41 tests to compile after the changed title/plugin setup; classified as a test update, without a new visible target. |
| 20 | other | fix-failed | 12147 | Phase 41 jack-panel/geometry failures persist. |
| 21 | other | record | 1719 | Add Screen view misses Screen; later phase-41 as-built describes camera search/fallback. No phase-41 start or single-defect live-check/fix/next-defect trace exists here, so its original repair boundary/root is not recoverable. |
| 22 | review-ux | record | 17086 | Portal deletion test under load, Screen pose before framing, tall new Look and Add Screen search share dispatch. Attached findings are from two days later. Substantive Screen placement/search changes share this interval without the actual feature start; the test is only one part of the work. |
| 23 | other | fix-failed | 1275 | Screen pocket closed view continues camera repair. |
| 24 | other | fix-failed | 1386 | Ruled camera fallback continues. |
| 25 | other | fix-failed | 657 | Last-resort camera fallback still needs repair. |
| 26 | other | record | 3639 | Camera/pixel plus card-layout/caption failures resemble phase 43; batch boundary absent. |
| 27 | other | fix-failed | 5766 | Same camera/card failures continue. |
| 28 | other | fix-failed | 1366 | Three card-fit failures persist. |
| 29 | merge | mechanical | 4927 | Catalyst merge. |
| 30 | other | record | 518 | Text measurement through public layout engine; shared work without a start. |
| 31 | other | record | 644 | One width for all value fields; exact phase boundary absent. |
| 32 | merge | record | 22690 | Value width, rear-card clearance of info text and branch merge share dispatch. A merge shares this interval with substantive value-width/clearance work. Its feature boundary is unresolved, so the entire time cannot be called judgment-free merge work. |
| 33 | gate | record | 10320 | Merge gates, full rear-card suite and commit. Only gate execution, a suite run and a commit are identified; no merge conflict repair or test update is named. |
| 34 | other | record | 222 | Activity says “Phase 45 as-built”: documentation/record work, not Beam repair. |
| 35 | other | fix-failed | 296 | Extra rear-panel visits remain, matching phase 45 one-reconcile goal. |
| 36 | gate | mechanical | 1221 | Rear-visit tests must wait for icon font. |
| 37 | merge | record | 5075 | Catalyst merge and seven-character value width. The seven-character field-width decision shares a merge interval without the actual feature boundary; it is not identified as a judgment-free conflict resolution. |
| 38 | review-ux | record | 167 | Look-selection box only on jack panel; C24 starts later. Top-panel F001–F006 were not open yet. |
| 39 | review-ux | record | 11703 | F001–F006 are sent together for top-panel design defects, followed by build/six shots and cable-plug tests. The phase-46 start order is absent from this record. Neither a phase-specific root nor a one-defect live-check sequence can be recovered from this batch. |
| 40 | review-ux | record | 17333 | The next top-panel batch sends F007 plate hugs rows, F008 unequal bottom space and F009 white rings together; tests/build/shots follow. With no phase-46 start or source-level before/after, neither a phase-specific root nor a one-defect live-check sequence is established. |

Landing check: The 37 late intervals are not Beam's repair tail. The hand split finds two Beam intervals and later feature/integration work. Top panels have two observed ledger batches; missing starts prevent trustworthy actual-phase ordinals for phases 41–46. All original R4–R40 landing positions are marked record in the cross-case check. Named test/merge/regression work is conditionally eligible; visual provenance is not proved by the misjoined ledger.

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
| 6 / f93f565a / 31 | 14 | R4, R5, R6, R7, R8, R9, R10, R11, R12, R14, R15, R16, R17 | — | — | R13 |
| 7 / 1a1f39e6 / 4 | 13 | R5, R6, R8, R9, R10, R13, R15, R16 | — | R4, R7, R11, R14 | R12 |
| 8 / e54c80d4 / 58 | 12 | R8, R9, R11, R12, R14 | — | R4, R5, R6, R7, R10, R15 | R13 |
| 9 / a702c130 / 15 | 11 | R4, R5, R6, R7, R8, R9, R10, R11, R12, R13, R14 | — | — | — |
| 10 / 8777d55e / 10 | 10 | R4, R5, R6, R7, R8, R9, R10, R11, R12 | — | R13 | — |
| 11 / 8777d55e / 7 | 10 | R4, R5 | — | R6, R7, R8, R9, R10, R11, R12, R13 | — |
| 12 / df477c65 / 22 | 9 | R4, R7, R8 | — | R5, R6, R9, R10, R12 | R11 |
| 13 / 7b115092 / 137 | 9 | R11 | — | R4, R5, R6, R7, R8, R10, R12 | R9 |
| 14 / 576fc590 / 111 | 9 | R4 | — | R6, R7, R8, R9, R10, R11, R12 | R5 |
| 15 / f205ddd0 / 33 | 9 | R5 | — | R4, R6, R8, R9, R10, R11, R12 | R7 |

The effect-framing case (rank 1) includes six post-landing target rulings without an independently named kept-category defect. The attempts log supports “target changed”; it does not support calling those changes repeated failures against one fixed contract. Rank 2's cell-lifecycle regressions and failing assertions remain eligible even after many rounds; this is evidence that the rule supplies no numeric stop for such retained defects. Rank 3 contains pilot recall and instruction changes beyond the retained categories, but the Work Order itself authorized pilot fixes. That is a tension between a broad phase order and the landing rule, not evidence that the worker silently expanded its order.

Rank 4 started September 13, before the October 1 hard-landing rule. Its 18 post-landing intervals are a retrospective comparison only. Pre-existing Mac and desktop defects were explicitly carried by its order; this conflicts with the later own-change-only packaging rule, but is not evidence that a September worker ignored an October rule. Rank 5's verification-only intervals and rank 2's ledger-only/unfinished intervals also show why a numeric tail is not by itself a noncompliance count. Rank 6's post-landing tail consists of named grid regressions and gates that the rule permits to stay. Rank 7 lacks finding provenance for several fit requests. Rank 8 lacks a timestamp-exact saved order, so its apparent scope expansion stays uncertain.

The same test applied to the mixed records gives a different kind of result:

| Mixed record | Recorded past landing | Supported | Outside original category | Uncertain / mixed work | Unusable actual-phase ordinal | Verification only |
| --- | ---: | --- | --- | --- | --- | --- |
| 189487ea / 6 | 53 | Conditional tests/merges in its section | — | Actual feature ownership unresolved | R4–R56 | Included in unrecoverable ordinals |
| 03f9b80e / 20 | 39 | R4, R21–R23, R26–R28, R32, R39–R42 | R34: new showrunner gap clarification | R5–R17, R20, R24–R25, R29–R31, R33, R35–R38 | — | R18–R19 |
| 8f4c2e88 / 38 | 37 | Conditional tests/merges in its section | — | Feature boundaries and ledger associations unresolved | R4–R40 | Included in unrecoverable ordinals |

For tile phase 20, R6 combines an outside cargo-mend repair with an eligible toast test. R14–R20 combine the branch-only freeze/performance/gates with added auto-view removal. R36–R38 combine lost CI runs with less certain row/mark work. These intervals cannot be assigned one eligible/ineligible time split. R34 revises a showrunner interpretation; it is outside the original kept-finding categories, but is an explicit later ruling rather than an unreported worker decision. Missing boundaries in the other two mixed records prevent a numeric claim about actual-phase hard landing.

## Totals

### Primary roots in the 15 non-mixed records

The 255 round rows above sum to **975,069 agent-seconds (270.85 hours)**. The three mixed records and the Astra reference are excluded. Each row's entire time is allocated to one primary root. Counts are sorted by rounds, then time; percentages are rounded independently.

| Primary root | Rounds | Share of rounds | Fix seconds | Fix hours | Share of fix time |
| --- | ---: | ---: | ---: | ---: | ---: |
| fix-failed | 91 | 35.7% | 473,429 | 131.51 | 48.6% |
| spec-gap | 44 | 17.3% | 190,740 | 52.98 | 19.6% |
| mechanical | 38 | 14.9% | 103,395 | 28.72 | 10.6% |
| record | 34 | 13.3% | 53,169 | 14.77 | 5.5% |
| fix-regressed | 29 | 11.4% | 78,118 | 21.70 | 8.0% |
| out-of-scope | 12 | 4.7% | 66,479 | 18.47 | 6.8% |
| target-moved | 7 | 2.7% | 9,739 | 2.71 | 1.0% |
| serial-check | 0 | 0.0% | 0 | 0.00 | 0.0% |
| **Total** | **255** | **100%** | **975,069** | **270.85** | **100%** |

The 91 `fix-failed` intervals include interrupted continuations and failed gate batches; they are not independently rejected patches. Mechanical covers only the stated lint, test-update and merge category. Pure verification and planned rollout rows are record rows. Spec-gap allocations name missing measurement targets or test cases in the individual Work Orders; they remain causal assessments, not proof that an omitted case caused each code mistake.

No normal-record row establishes the required one-defect live-check → fixed → next-defect sequence. All ten former serial-check rows had batched findings or lacked that trace. Zero qualifying rows is a limit of this evidence, not proof that every live check was complete. Mixed tile phase 20 R29 retains serial-check with its explicit single-finding dispatch, acceptance and next-check sequence; it remains outside this table.

Whole-batch allocation particularly limits rank 5 R1 (17,373 seconds of mode/switch work plus 17,272 of stage-drag work) and rank 6 R1 (5,160 test seconds plus 8,563 grid-code seconds): their spec-gap times are upper bounds on the parts tied to the missing tests. Rank 14 R11 allocates 14,637 seconds to outside-scope screwdriver appearance although F014 also repairs the in-scope glyph. Rank 13 R10 likewise allocates 6,263 seconds to outside-scope work although the chooser shot is in scope. These upper bounds and the section uncertainties apply to the time shares.

### Phase 1 causes across the full 60-day window

Later capture on **2026-10-20 at 18:06 EDT**, after tuning, has 1,867 phases, median 0 rounds, 222 phases with at least 3 rounds (11.9%) holding 79.2% of fix time, and 39 mixed records. It contains **2,257 rounds and 7,588,091 agent-seconds**. The initial selection had 2,255 rounds and 7,587,361 seconds: live records added two rounds and 730 seconds elsewhere; the sampled 15 counts and times did not change. This table includes all selected phase records, including mixed ones. It is for scale, not a root-cause extrapolation from the top 15.

Source: the same report command and shared `_events` / `load_phases` records, with the same start-window/status filters. Per-cause time sums use the shared records and reconcile with the independently checked report metadata. This table keeps its dated 18:06 capture. Root reassessment changes no API cause or recorded seconds, so its values are unchanged; correcting the tuning example to 2,709 seconds does not change these independently summed API values.

| Phase 1 cause | Rounds | Share of rounds | Fix seconds | Fix hours | Share of fix time |
| --- | ---: | ---: | ---: | ---: | ---: |
| review-code | 1,392 | 61.7% | 3,270,561 | 908.49 | 43.1% |
| review-ux | 427 | 18.9% | 2,002,693 | 556.30 | 26.4% |
| other | 226 | 10.0% | 1,062,728 | 295.20 | 14.0% |
| gate | 164 | 7.3% | 939,197 | 260.89 | 12.4% |
| merge | 42 | 1.9% | 300,384 | 83.44 | 4.0% |
| live | 6 | 0.3% | 12,528 | 3.48 | 0.2% |
| **Total** | **2,257** | **100%** | **7,588,091** | **2107.80** | **100%** |

### Cause-word correction and verification

Only `LIVE_WORDS` changed: it now also matches `ux review`. Evidence is run `fb5b2c96`, phase 202, instance `8013edcb-6bbb-4771-804d-fc922f3d5dc4`: JSONL line 980 finishes “UX review”, text “CPU pane four states at width 19”, result “2 defects”; line 981 starts “repairing phase202 awaiting row” 59 seconds later; line 982 finishes it. No ledger matches that repair. The change moves exactly that R2, **one round and 2,709 agent-seconds**, from other to live. It changes no sampled record count or cause. Adding screenshot alone matched no further case and was not retained. No API signature or field changed.

The synthetic regression test covers that completed-review/next-repair sequence. Run-record line 982 has `pass_elapsed_seconds=2709`; the API value and the tuning example agree. Phase 2 code checks, before this document-only repair: 31 unit tests passed; basedpyright reported 0 errors and 0 warnings (its exit 3 is expected here). The second writer's final merge gate passed with history included. The remaining temporal ledger-join problem described in mixed record 3 is a measurement limitation; word-list tuning does not repair it.

## Reference case outside the run records: Astra design reviews

Source: [experiments-unit design notes](/home/natepiano/rust/hanadocs/showrunners/cargo-liner/experiments/astra-design-notes.md), sections (b), (c) and (d), dated 2026-10-20. This is cargo-liner's experiment-framework **design**, not code repair. It is excluded from both the 255-round sample and the 60-day run-record cause totals.

| Review / revision | Findings fixed | Root assessment | Evidence |
| --- | ---: | --- | --- |
| 1 | 8 of 8 | target-moved (design scope context) | Tooling item 13 was added from the user during the first draft at 16:45 EDT. The first review found phase-time/stage-time confusion, thread lifetime and replay keys, unsupported comparisons, launch recovery and blinding/control gaps. Revision 1 narrowed unsupported claims. The notes do not attribute every finding to the added item. |
| 2 | 6 of 6 | fix-regressed; target-moved | Section (d) says fixes added receipts, locks and publication generations whose gaps appeared in the next review. Section (b) lists control-policy mismatch, dependent pairs, completion before durable record, unnamed benefit source, no scoring stop gate and a conflicting repeat rule. The director allowed replacement of the retained-reviewer study with a smaller history-command study. |
| 3 | 5 of 5 | fix-regressed; target-moved | The next review found conflicting missing-measurement rules, a withdrawn-result index, missing Mac path, dispatch-dependent pairing and a workload worth only 0.07 s/week. The director ruled that Mac stays in scope and that the first study needs a real recurring workload or an explicit framework-exercise claim. |

All 19 findings were fixed; none was dropped. Counts fell 8 → 6 → 5, but the design did not converge: each repair added mechanisms with new gaps, and the document grew from 568 to 696 lines. The third review still had two high findings. Its final fixes had no further review because the cap was three. The notes support a design repair/target-change cycle; they do not establish a code regression rate or provide agent-time measurements comparable to the run records.
