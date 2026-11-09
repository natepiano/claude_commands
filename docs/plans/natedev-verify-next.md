# natedev-verify — next items

## Two cargo-liner runs on the Mac at once

**Source:** the user, relayed by builds through the natedev showrunner, 2026-10-29: "allow up to 2 cargo liner cargo things to run on the mac at the same time - right now we just let 1 at a time as i recall - if it already allows more then this is moot". <!-- allow-banned: verbatim user quote -->

**Today:** the Mac takes one claim per repository (`docs/as-built/mac-test-offload.md`), because two runs of one repository share one mirror and one build folder.

**Needed:** a second cargo-liner claim with its own mirror and build folder, kept inside the Mac free-space floor (`free_floor_gib=100`; cargo-liner build budget default 24 GiB). Hana stays at one. The memory rule "max one hana + one cargo-liner job" changes with it. Make each mirror and build folder through copies.py (after Phase 35 in natedev-verify). Reuse the slot allocation and claim adoption code, with no other copy maker. Enable two cargo-liner slots on the Mac with separate mirrors and build folders. Keep Hana at one slot. Test `claim-wait`, `--from-pid`, memory limits, and disk limits for both slots.

## Cut mac_idle.py's CPU per verify step

**Source:** builds (showrunner), 2026-10-30, from the Phase 1 trace: "mac_idle.py costs 122 ms per step, and it is your file, so put it in your next-items list."

**Today:** `verify.sh` starts `mac_idle.py` for Mac retry registration, exclusion and cleanup. `register_mac_wait` (`verify.sh:2068`) limits registration to Mac routes on Linux. The Phase 1 lint trace measured about 122 ms of CPU.

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

## Report Phase 28 metadata items

**Source:** builds (showrunner), 2026-11-05. Report Phase 28 (`eb4210b92`) is on this branch.

**Needed:**
1. `verify.sh` reads the metadata before the first export (`verify.sh:2717-2732`), through `set_gate_metadata` and `load_metadata`.
2. `BUILDLOG_OUTPUT_ROOTS` comes from a `verify_metadata.py` request, in place of the `step_context.py output-roots` call at `verify.sh:725`.
3. `verify.sh test` reads Cargo's folders once. `tree_watch.py:245` takes them from the `BUILDLOG_OUTPUT_ROOTS` export when its evidence matches, else from `read_cargo_output_roots`.
4. Keep `test_metadata_reuse_keeps_the_export_after_a_config_change` and the output-folder tests. Put the before and after time per test run in the notice.

## FixtureHang in test_verify_ssh_targets_retry.py

**Source:** builds (showrunner), 2026-11-05.

**Today:** natest's survey saw the module raise FixtureHang once (659 s, load 55).

**Needed:** find the cause. If it is the same as the retry-clock hang (`988b6690b`, `3c86b5424`), fix it the same way. Send builds the cause when known.

## One busy-mirror loop in sweep.py

**Source:** Phase 35 review, sent to next items by builds 2026-11-05.

**Owner:** whoever holds `scripts/lint/sweep.py` when the builds file freeze lifts. Queue-unit owns the `sweep.py` callers of the floor interfaces once Phase 35 is on GitHub builds.

**Needed:** `_hold_floor_pass` already uses `busy_mirrors` at `scripts/lint/sweep.py:3988`. Verify that loop against F031 and close its Phase 35 ledger route. `MacFloor.leased_mirrors` (`sweep.py:3542`) holds a different value; do not rename it.

## Mirror scans after the sweep.py file freeze

**Source:** Phase 35b review, findings F028 and F029.

**Order:** the first checkpoint after `scripts/lint/sweep.py` leaves the file freeze, together with F031 above.

**Owner:** verify-unit, with queue-unit for the copies and floor interfaces.

**Today:** `copies.py` imports the process scanners from `scripts/lint/tmp_sweep.py`. The Mac floor sweep runs `lsof -F pfn` twice (`copies.scan_mirror_processes`, `sweep.MacFloor.uses`), about 0.23 s more per low-disk sweep.

**Needed:**
1. Move `ProcessUsesKnown`, `ProcessUsesUnavailable`, `process_paths`, `lsof_paths` and `parse_lsof` from `lint/tmp_sweep.py` to `scripts/lib`, and change every caller together. Keep no re-export.
2. Share one Mac `lsof` scan between mirror protection and the approved floor roots. Keep the current rule in `mirror_lease_set`: all candidate leases before the one scan.
3. Keep unavailable-scan protection, unreadable-process protection, path matching and cancellation behavior. Keep the existing scan and floor tests.
4. Put the measured scan count and time before and after in the notice. Close the routes F028 and F029 from the Phase 35b ledger.

## Each verify run starts fewer helper processes

**Source:** Phase 41's profile. The builds showrunner placed it after Phase 41a, 2026-11-06.

**Today:** one `verify.sh` run of a plain package, with the test fixture's fake tools, starts about 300 processes and costs about 8 CPU s. The `strace` count is 38 git, 37 bash, 35 python3, 32 `py`, 19 rm, 12 dirname, 12 mv. The Python starts are 7 `-c`, 7 `tree_watch.py`, 4 `build_folders.py`, 3 `build_targets.py`, 2 `step_record.py`, 2 `mac_idle.py`, 2 `admission_client.py`, and one each of 7 others.

**Needed:**
1. Measure the baseline first, on one real plain-package run: processes by command name, CPU s and wall s. Name the method. The count above comes from fake tools, so it is not this baseline.
2. Remove the starts that the measure shows give no needed answer, largest first. Keep each behavior and its tests.
3. Measure the same run with the same method. Put before and after in the notice.
4. Move three parts of `verify.sh` to functions that the SSH target tests can call. natest 608af02f measured 146.1 CPU s in the four `test_verify_ssh_targets` files. Breakdown: Phase 66 as-built in `docs/plans/builds-natest.md`, on builds-natest at 6f496170f. Builds placed this here, 2026-11-06.
   - An eligibility consumer from `initialize_step_record`. It accepts real eligibility output and starts no heavy target command.
   - A result consumer from `try_mac_test`. It accepts the actual result, status and output, and gives the next step decision of `route_test`. This one decides the most: a stop after a busy result and the queue-age handoff have no narrow function now.
   - The reservation and retry orchestration around `command_reserve`, claim recovery and target selection. It keeps the real admission result and queue sequence.

## A stopped verify call waits for every process of its step

**Source:** natest-unit, 2026-11-06, forced evidence on 5f56b92c4. Builds placed it here, 2026-11-07.

**Today:** `verify.sh` signals the step's process group, then waits only for `RUN_STEP_PID`. The cargo token releases and `verify.sh` exits while a step member (rustc, cargo) can still run. Evidence: `/tmp/claude/unit/2113de4f-9913-485a-9a51-61558bfd81db/phases/73/profile/impl3/cargo-survivor-evidence.jsonl`, a forced SIGSTOP case only.

**Needed:**
1. Measure a live stopped call. Record whether a step member outlives the token release.
2. Only if one does: wait for the whole step process group before the token releases, with a test that fails on the old code.

## The copy guard refuses an unset source or git path inside a program

**Source:** builds (showrunner), 2026-11-08, settled as `next`: "build it only when a real session makes a tree that way. The guard is already large, and no session writes these forms." Found in the phase 41v review; the 41t tree gives the same result.

**Today:** `python3 -c "import shutil;shutil.copytree('$UNSET', '/tmp/o')"` passes, while `cp -r "$UNSET" dest` refuses with `cannot resolve recursive copy source`. `bash -c "$UNSET_DIR/git -C . clone . /tmp/o"` passes, because the unknown-executable check in `_operation` (`scripts/copies/copy_guard.py`) does not skip git options before `clone`; the top-level form refuses with `cannot resolve executable name`.

**Needed:** only when a real session makes a tree in one of these forms. Then both commands refuse, and the copy guard command list and transcript corpus keep their results.
