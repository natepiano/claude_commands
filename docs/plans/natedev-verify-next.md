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
