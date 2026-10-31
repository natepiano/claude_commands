# natedev-verify — next items

## Two cargo-liner runs on the Mac at once

**Source:** the user, relayed by builds through the natedev showrunner, 2026-10-29: "allow up to 2 cargo liner cargo things to run on the mac at the same time - right now we just let 1 at a time as i recall - if it already allows more then this is moot". <!-- allow-banned: verbatim user quote -->

**Today:** the Mac takes one claim per repository (`docs/as-built/mac-test-offload.md`), because two runs of one repository share one mirror and one build folder.

**Needed:** a second cargo-liner claim with its own mirror and build folder, kept inside the Mac free-space floor (`free_floor_gib=100`; cargo-liner build budget default 24 GiB). Hana stays at one. The memory rule "max one hana + one cargo-liner job" changes with it. A second claim must also work with the memory-wait handoff: `mac_test.py claim-wait` takes the claim, and `claim --from-pid` (`adopt_wait_claim`) moves the claim at `claim_path(paths, scope)` to the runner. Both read one claim path per repository scope today.

## Pair a literal with an imported crate constant

**Source:** cargo-liner-style showrunner, 2026-10-30 01:1x EDT; ordered by builds 01:1x EDT as the phase right after 6b lands. Its rules (constants-file-organization, import-constants-at-the-top, derive-test-values-from-production-constants) put production constants in a crate constants module, imported by bare name at the top of the file, so most literal-to-constant changes in values tickets use an imported name.

**Today (after 6b):** the assertion guard pairs `contains("x")` with `contains(NAME)` only when NAME is a bare `const` with one unsigned literal in the same file, bound once there. An imported NAME stays as written and the change reports `removed`.

**Needed:** also pair a bare NAME brought in by one explicit top-of-file `use crate::a::b::NAME;` leaf. Any doubt keeps HEAD's behavior (the name stays written):
- no glob, no `as` alias, no `self`/`super` path, no cfg attribute on the `use`;
- the path maps to exactly one file by the standard layout (`src/a/b.rs` or `src/a/b/mod.rs`); a `#[path]` attribute, an inline `mod b { }` on the way, or two candidate files leaves it written;
- one file read; the target is a top-level `const` (not inside an inline module or a fn) with one unsigned literal;
- the 6b bound-once census runs in both files;
- each side of the diff reads its own revision of the target file.

**Acceptance:** run the guard on cargo-liner's real constants-module changes (values checkpoints on origin/style/main in cargo-liner-style-values: 65d9193f with 14 imports, e145b6c0 with 10, 6ccb0c27 with 6, 925989b3 with 4, and db16fb1d, which changes a constants module) and report how many former blocking removals clear, and why each remaining one stays. Regressions for each refusal above.

**Design note:** Astra review of 6b round 4 (/tmp/claude/astra/20261030T051013Z-2591932/review.md, to be copied into the phase brief): a unique textual name is not a proven binding; recognize supported bindings positively and refuse everything else; string/char/bool literals only (numeric types come from context).

## Cut mac_idle.py's CPU per verify step

**Source:** builds (showrunner), 2026-10-30, from the Phase 1 trace: "mac_idle.py costs 122 ms per step, and it is your file, so put it in your next-items list."

**Today:** each `verify.sh` step starts `mac_idle.py`, which costs about 122 ms of CPU (Phase 1 trace, real lint run).

**Needed:** measure its imports and work with `VERIFY_PY_TRACE` and `python -X importtime`, then remove the start or its slow imports where the step does not need them, with every output unchanged. Done when the same trace shows the cost removed or named per cause.

## Refuse a rename proof when a string escape can spell a format capture

**Origin:** binding-rename guard closure review, 2026-10-31 (F002, minor). The rename proof (`prove_binding_rename` in `scripts/direct/assertion_guard.py`) checks each string literal's source text for an inline capture (`{name`). An escape that spells a brace passes: renaming `value` while `println!("\x7bvalue}")` stays gives a clean pass, though Rust reads `\x7b` as `{` and the string still captures the old name.

**Fix as a class:** the proof refuses when any non-raw string or char literal in the enclosing test, on either side, holds a `\x` or `\u` escape. Add `\x7b` and `\u{7b}` cases to `test_binding_rename_refuses_inline_format_captures` in `scripts/direct/test_assertion_guard_revisions.py`.

## Forward named variables to a Mac test run and fetch a named folder back

**Source:** builds (showrunner), 2026-10-31, for hana P34: test fixtures save PNGs when `HANA_TOOLTIP_PLACEMENT_SHOTS` is set. `verify.sh`'s Mac offload forwards no such variable and copies no file back; builds allowed one direct `/build:mac` claimed run for P34 meanwhile.

**Needed:** a Mac test run through `verify.sh test` that names the variables to forward and the folder to fetch back. Each is refused unless named, and the result is recorded like any other test run. One path, no second runner. Joins the `verify.sh test --mac` add-on if that is simpler. Tell hana when it is on main.

**Order:** after the small items, with the `--mac` add-on.
