# models-rename-track — next items

## Narrow the subprocess fake in add_unit_test_support

**Status:** next (sdlc, 2026-11-02).

**Problem:** `scripts/production/add_unit_test_support.py` fakes `subprocess.run` for every command that is not git. In phase 3k that fake answered the taplo call from `unit_config`. Phase 3k avoided it with a `run_taplo` alias, but the broad fake stays the cause.

**Change:** the fake answers only the commands it names and raises on any other command.

**Acceptance:** the add_unit test suites pass, and a test shows that an unnamed command raises.

## The fill reports problems and never blocks on them

**Status:** next (sdlc, 2026-11-02; user rule of 2026-11-02: tools report anomalies and never block on them).

**Problem:** `staff_sources.py fill` exits 1 and prints no `changed` or `unchanged` line when any unit has a recovery error. Healthy units get no report.

**Change:** the fill prints each problem as one line, prints `changed` or `unchanged` for the healthy units, and exits 0 on a rerun.

**Acceptance:** with one unit in error, a fill prints that unit's problem line and `changed`; a rerun prints the same problem line and `unchanged`, and exits 0.
