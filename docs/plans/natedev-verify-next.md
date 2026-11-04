# natedev-verify — next items

## Two cargo-liner runs on the Mac at once

**Source:** the user, relayed by builds through the natedev showrunner, 2026-10-29: "allow up to 2 cargo liner cargo things to run on the mac at the same time - right now we just let 1 at a time as i recall - if it already allows more then this is moot". <!-- allow-banned: verbatim user quote -->

**Today:** the Mac takes one claim per repository (`docs/as-built/mac-test-offload.md`), because two runs of one repository share one mirror and one build folder.

**Needed:** a second cargo-liner claim with its own mirror and build folder, kept inside the Mac free-space floor (`free_floor_gib=100`; cargo-liner build budget default 24 GiB). Hana stays at one. The memory rule "max one hana + one cargo-liner job" changes with it. Make each mirror and build folder through copies.py (after Phase 35 in natedev-verify). Reuse the slot allocation and claim adoption code, with no other copy maker. Enable two cargo-liner slots on the Mac with separate mirrors and build folders. Keep Hana at one slot. Test `claim-wait`, `--from-pid`, memory limits, and disk limits for both slots.

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

## Record the end of each Mac hold

**Source:** builds (showrunner), 2026-11-03, settled as `next`, corrected the same day: "The missing pieces are Mac hold END events, not mac_idle.py." Why: hold records have no end, so report-unit can give Mac idle only as an upper bound (0.59 h/day).

**Today:** Mac hold records have a start event but no end event. The producers are `scripts/mac_test/mac_test.py` (`start_hold_recorder`, `record_hold_event`) and `scripts/buildlog/record.py` (`hold_event`).

**Needed:**
1. Find why the end events are missing.
2. Repair the cause at the producer.
3. Test one completed hold end to end: its start and its end are both recorded.

**Order:** after Phases 24 and 27. Track this producer repair separately from the completed Phase 20 work. `mac_test.py` (near lines 1525 and 2491) already requests END events; find why they do not reach the recorded history, and add no second emission path.

## Copy guard: refuse tree-making git calls through every import form

**Source:** phase 18 review 6, finding F037 (blocker), routed under the round-3 landing rule. Astra review: `/tmp/claude/astra/20261103T220532Z-2445924/review.md` item 7.

**Today:** `scripts/copies/copy_guard.py` finds `bulk_run.git(...)` and `copies.git(...)` calls only for some import forms. These forms give no finding:
1. `import scripts.style.bulk_run`, then `scripts.style.bulk_run.git(repo, "clone", "a", "b")`.
2. `from scripts.style import bulk_run as run`, then `run.git(...)`.
3. An import inside a function.

**Needed:** the guard refuses a tree-making git call through each form above, through alias reimports, and through `global` and `nonlocal` bindings. The local-shadowing rule from phase 18 stays.

**Acceptance:** one regression test per binding form, each fails on the old guard; valid shadowed-name cases stay clean; the copies and guard suites pass.

**Due:** the next verify-unit checkpoint after phase 18.

## Name copies.py source and merge policies with semantic types

**Source:** phase 20, 23 and 24 architect review, finding 11, routed out of phase 18 under the round-3 landing rule.

**Today:** the copies API (`scripts/copies/copies.py` near line 539) takes `apply_uncommitted=None` and `merge=None`. These values are a source policy and an integration policy, not missing input.

**Needed:** semantic variants for a committed source versus a worktree source, and for merge versus no merge, with conversion from the CLI flags at the boundary. Callers and tests switch in the same change.

**Acceptance:** copies, copy guard and style suites pass; basedpyright 0/0/0; no bare `None` policy argument remains in the copies API.
