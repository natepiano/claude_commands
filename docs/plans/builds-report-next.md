# builds-report — next items

## Each test step's row records the memory charge admission gave its test phase
- **Starts when:** resources-unit's test-phase entry charge lands in admission's reservation (its parked Phase 15 follow-up). Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py` (`test_phase_context`).
- **Need:** every step row records `test_phase_charge` null today, because admission has no test-phase entry-charge field.
- **Completion:** `record.test_phase_context` copies the charge (bytes, history cutoff, form, contributing widths) from the reservation into the row's `test_phase_charge` and computes nothing. Test: a reservation with the field records it for terminal and tee steps; a reservation without it records null.
- **Source phase:** 4c.

## Each build step spends less CPU to write its build-log row
- **Starts when:** any time. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py` (the call `verify.sh` starts for each step).
- **Need:** verify-unit measured 71 ms of CPU each time `verify.sh` starts `record.py`, on every step.
- **Completion:** the call costs less CPU. The phase reports CPU ms per call before and after, both measured the same way verify-unit measured the 71 ms.
- **Source phase:** 6 (showrunner add-on from verify-unit's measurement).

## Admission reads the builds slice's memory from the one shared reader
- **Starts when:** the shared cgroup reader is deployed on main. The showrunner assigns `memory_admit.py`'s owner. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/lint/memory_admit.py` (`sccache_memory`).
- **Need:** `sccache_memory` constructs the builds slice path and separately parses `sccache.service/memory.stat`. The shared cgroup reader already exists.
- **Completion:** `sccache_memory` reads the shared typed slice capture; no second parse of the slice's `memory.stat` remains.
- **Source phase:** 6 (phase review).

## The build log stores each memory reading once each minute
- **Starts when:** any time. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/memory.py`, `scripts/buildlog/sample.py`.
- **Need:** each tick writes the builds slice fields in both the sample row and the memory_snapshot row, so the log grows faster than it needs to.
- **Completion:** the slice fields live only in the sample row; memory_snapshot keeps its old fields; a test pins each row's fields.
- **Source phase:** 7.

## A real build step's row shows measured CPU and IO
- **Starts when:** the installed recorder includes scope-counter capture for CPU. IO verification follows the user's natedev rebuild with scope IO accounting. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py`, `scripts/lint/invoke.sh`.
- **Need:** `invoke.sh` runs the installed recorder, so tests and the copy-based live check could not show a real step's counters.
- **Completion:** one real step on natedev has measured CPU in `scope_resources`; IO is measured after the rebuild.
- **Source phase:** 7.
