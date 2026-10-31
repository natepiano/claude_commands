# sdlc assertion guard — next items

- **Snapshot files.** `assertion_guard.py` reads only `.rs` and `.py` files. A
  changed `.snap` file (insta) can make a test pass the same way a weaker
  assertion does: the expected output moves to match the new result. Report a
  changed or deleted `.snap` file, and block it without an `Assertion-Change:`
  ruling. Out of scope for the first phase (showrunner, 2026-10-28).
- **Base-test replay** (showrunner, 2026-10-28; the phase after the guard,
  plan to the showrunner once the guard phase is merged). The pattern check
  cannot see a test weakened through its input, a fixture, a helper, a
  tolerance constant, or code changed to pass only that test. For each test
  the diff changes or deletes, run the base version of that test against the
  new code; a failure needs an `Assertion-Change:` ruling however the test was
  edited. Python: replace the whole test file with its base version in a
  scratch copy. Rust: add the base test function to the same test module
  under a new name, in a scratch copy. A replay that cannot compile or run is
  a "cannot replay" finding that does not block and that the review lists.
  Run only the changed tests, and measure the build cost on the tile case
  first: builds are scarce.
  Limits (showrunner, for the user, 2026-10-28): replay only the tests the
  pattern guard flags (removed, weaker, changed, test removed, turned off),
  never every changed test, so a phase with no flags pays nothing. Run it
  beside the review, not after it, on the warm target dir, never a cold one.
  Acceptance: no added wall time on a phase with no flags. For a flagged
  phase, measure the added time on the tile case and on one ordinary phase,
  and put both numbers in the plan. A replay that adds wall time on the
  critical path does not ship.
