# builds-report — next items

## Each test step's row records the memory charge admission gave its test phase
- **Starts when:** resources-unit's test-phase entry charge lands in admission's reservation (its parked Phase 15 follow-up). Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py` (`test_phase_context`).
- **Need:** every step row records `test_phase_charge` null today, because admission has no test-phase entry-charge field.
- **Completion:** `record.test_phase_context` copies the charge (bytes, history cutoff, form, contributing widths) from the reservation into the row's `test_phase_charge` and computes nothing. Test: a reservation with the field records it for terminal and tee steps; a reservation without it records null.
- **Source phase:** 4c.
