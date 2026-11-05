# natedev-verify — next items

## Two cargo-liner runs on the Mac at once

**Source:** the user, relayed by builds through the natedev showrunner, 2026-10-29: "allow up to 2 cargo liner cargo things to run on the mac at the same time - right now we just let 1 at a time as i recall - if it already allows more then this is moot". <!-- allow-banned: verbatim user quote -->

**Today:** the Mac takes one claim per repository (`docs/as-built/mac-test-offload.md`), because two runs of one repository share one mirror and one build folder.

**Needed:** a second cargo-liner claim with its own mirror and build folder, kept inside the Mac free-space floor (`free_floor_gib=100`; cargo-liner build budget default 24 GiB). Hana stays at one. The memory rule "max one hana + one cargo-liner job" changes with it. Make each mirror and build folder through copies.py (after Phase 35 in natedev-verify). Reuse the slot allocation and claim adoption code, with no other copy maker. Enable two cargo-liner slots on the Mac with separate mirrors and build folders. Keep Hana at one slot. Test `claim-wait`, `--from-pid`, memory limits, and disk limits for both slots.

## Cut mac_idle.py's CPU per verify step

**Source:** builds (showrunner), 2026-10-30, from the Phase 1 trace: "mac_idle.py costs 122 ms per step, and it is your file, so put it in your next-items list."

**Today:** each `verify.sh` step starts `mac_idle.py`, which costs about 122 ms of CPU (Phase 1 trace, real lint run).

**Needed:** measure its imports and work with `VERIFY_PY_TRACE` and `python -X importtime`, then remove the start or its slow imports where the step does not need them, with every output unchanged. Done when the same trace shows the cost removed or named per cause.

## Record the end of each Mac hold

**Source:** builds (showrunner), 2026-11-03, settled as `next`, corrected the same day: "The missing pieces are Mac hold END events, not mac_idle.py." Why: hold records have no end, so report-unit can give Mac idle only as an upper bound (0.59 h/day).

**Today:** Mac hold records have a start event but no end event. The producers are `scripts/mac_test/mac_test.py` (`start_hold_recorder`, `record_hold_event`) and `scripts/buildlog/record.py` (`hold_event`).

**Needed:**
1. Find why the end events are missing.
2. Repair the cause at the producer.
3. Test one completed hold end to end: its start and its end are both recorded.

**Order:** after Phases 24 and 27. Track this producer repair separately from the completed Phase 20 work. `mac_test.py` (near lines 1525 and 2491) already requests END events; find why they do not reach the recorded history, and add no second emission path.

## Check mend against the active rustc before lint

**Source:** builds (showrunner), 2026-11-04.

**Needed:** before lint runs mend, check that cargo-mend loads against the active rustc (`cargo mend --version` exits 0). On a failure, print one line with the cause (rustc changed since mend was installed) and the repair (`imend` in a cargo-liner worktree), then stop that lint step. Add no pin and no Nix change: natedev and the Mac keep cargo-mend in `~/.cargo/bin` by design (nixos `shell.nix:25-28`).

## Load flake in test_verify_token_mac_step.py

**Source:** builds (showrunner), 2026-11-05.

**Today:** the module failed once at load 59 in natest's gate and passed alone on the rerun.

**Needed:** fix it in the next phase that touches that module, beside the `verify_hold_wait_test_support.py:327` flake fix.

## Reuse the step-record state

**Source:** natest, approved by builds 2026-11-05. Order: first of the three speed items here.

**Today:** each published step state starts its own Python process (`verify.sh:356-358`, `:1925-1934`, `scripts/direct/step_record.py`). That is 3.6 s of process spans per no-repair run and 6.9 s per repair run in `test_verify_lint_judgment.py`.

**Needed:** reuse the state, for a gain of 1-2 s per `verify.sh` call. Measure before and after, and tell the user the build cost change in one line.

## A callable repair decision

**Source:** natest, approved by builds 2026-11-05. Order: second of the three speed items.

**Today:** the decision is inline at `verify.sh:3711-3714`.

**Needed:** give it one callable home. Move the 10-case status table to direct tests, and keep one end-to-end run for each route. This saves about 31 s on `test_verify_lint_judgment.py`. Measure before and after, and tell the user the build cost change in one line.

## Platform column in build_targets.py eligible

**Source:** natest, routed by builds 2026-11-05. Order: third of the three speed items.

**Today:** `verify.sh` starts `build_targets.py platform` again after `eligible` (69 interpreter starts in `test_verify_ssh_targets_routing.py`, about 0.094 s each).

**Needed:** `verify.sh` reads the platform from the `eligible` answer. Change the answer format and every reader together. Keep selection, refusal, fallback and reservation behavior. The estimate is 6.5 s on the routing module and one interpreter start fewer per SSH-target run. Before start, trial-merge origin/builds-resources and take natest Phase 24. On landing, send natest the builds commit.

## Report Phase 28 metadata items

**Source:** builds (showrunner), 2026-11-05. Build on report Phase 28 (`eb4210b92`) once it lands on builds. Its ledger is `/tmp/claude/unit/b090f8f1-cf8e-4d44-8e52-b223d94de9cc/findings_state.json`.

**Needed:**
1. `verify.sh` reads the metadata near line 2697 without an export (F013).
2. `verify.sh` and `verify_metadata.py` export through a `verify_metadata.py` request, in place of the output-roots verb (F014).
3. `verify.sh test` reads Cargo's folders once. `tree_watch.py:245` takes them from the `BUILDLOG_OUTPUT_ROOTS` export when its evidence matches, else from `read_cargo_output_roots`. Put the before and after time per test run in the notice.

## FixtureHang in test_verify_ssh_targets_retry.py

**Source:** builds (showrunner), 2026-11-05.

**Today:** natest's survey saw the module raise FixtureHang once (659 s, load 55).

**Needed:** find the cause. If it is the same as the retry-clock hang (`988b6690b`, `3c86b5424`), fix it the same way. Send builds the cause when known.

## One busy-mirror loop in sweep.py

**Source:** Phase 35 review, sent to next items by builds 2026-11-05.

**Owner:** whoever holds `scripts/lint/sweep.py` when the builds file freeze lifts. Queue-unit owns the `sweep.py` callers of the floor interfaces once Phase 35 is on GitHub builds.

**Needed:** `lint/sweep.py` (near lines 3298, 3321 and 3678-3682) has one busy-mirror `LockHeld` loop in `_hold_floor_pass`. Rename `leased_mirrors` to `busy_mirrors`. Then close its route from the Phase 35 ledger (F031).
