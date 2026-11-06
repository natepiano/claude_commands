# models-rename-track — next items

## Narrow the subprocess fake in add_unit_test_support

**Status:** next (sdlc, 2026-11-02).

**Problem:** `scripts/production/add_unit_test_support.py` fakes `subprocess.run` for every command that is not git. In phase 3k that fake answered the taplo call from `unit_config`. Phase 3k avoided it with a `run_taplo` alias, but the broad fake stays the cause.

**Change:** the fake answers only the commands it names and raises on any other command.

**Acceptance:** the add_unit test suites pass, and a test shows that an unnamed command raises.
