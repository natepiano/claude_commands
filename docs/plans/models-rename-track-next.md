# models-rename-track — next items

## Narrow the subprocess fake in add_unit_test_support

**Status:** next (sdlc, 2026-11-02).

**Problem:** `scripts/production/add_unit_test_support.py` fakes `subprocess.run` for every command that is not git. In phase 3k that fake answered the taplo call from `unit_config`. Phase 3k avoided it with a `run_taplo` alias, but the broad fake stays the cause.

**Change:** the fake answers only the commands it names and raises on any other command.

**Acceptance:** the add_unit test suites pass, and a test shows that an unnamed command raises.

## The fill reports anomalies and healthy changes

**Status:** next (sdlc, 2026-11-02; user rule of 2026-11-02: tools report anomalies and never block on them).

**Problem:** `_fill_live` suppresses its result line and returns 1 when recovery anomalies exist, also after healthy writes.

**Change:** print each recovery anomaly on one line. Continue with the healthy units. Always print the aggregate `changed` or `unchanged` line. Return 0 for reported recovery anomalies.

**Acceptance:** one malformed unit and one repairable unit give the anomaly line, `changed` and exit 0. The rerun gives the anomaly line, `unchanged` and exit 0. The malformed unit stays unchanged. Errors for unreadable staff storage and the spec's safety refusals stay.

**Files:** `scripts/production/staff_sources.py`, `scripts/production/test_staff_sources.py`.

**Constraint:** keep the incomplete-observation output and exit status of `staff.py show --json --live`.
