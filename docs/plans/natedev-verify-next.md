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

## Copy guard: refuse tree-making git calls through every import form

**Source:** phase 18 review 6, finding F037 (blocker), routed under the round-3 landing rule. Astra review: `/tmp/claude/astra/20261103T220532Z-2445924/review.md` item 7.

**Today:** `scripts/copies/copy_guard.py` finds `bulk_run.git(...)` and `copies.git(...)` calls only for some import forms. These forms give no finding:
1. `import scripts.style.bulk_run`, then `scripts.style.bulk_run.git(repo, "clone", "a", "b")`.
2. `from scripts.style import bulk_run as run`, then `run.git(...)`.
3. An import inside a function.

**Needed:** the guard refuses a tree-making git call through each form above, through alias reimports, and through `global` and `nonlocal` bindings. The local-shadowing rule from phase 18 stays.

**Acceptance:** one regression test per binding form, each fails on the old guard; valid shadowed-name cases stay clean; the copies and guard suites pass.

**Due:** Phase 35 item 3. Remove this item after its binding-form and valid-shadowing tests pass.

## Name copies.py source and merge policies with semantic types

**Source:** phase 20, 23 and 24 architect review, finding 11, routed out of phase 18 under the round-3 landing rule.

**Today:** `copies.standing` (`scripts/copies/copies.py`) takes `apply_uncommitted=None` and `merge=None`. These values are a source policy and an integration policy, not missing input. Phase 35 adds a third source, the staged tree.

**Needed:** Phase 35 item 7 introduces `CommittedSource`, `WorktreeSource`, `StagedSource`, `NoMerge` and `MergeRevision`. Convert the CLI flags at the boundary, and change callers and tests before queue-unit takes ownership.

**Due:** Phase 35 item 7. Remove this item when that item lands.

**Acceptance:** copies, copy guard and style suites pass; basedpyright 0/0/0; no bare `None` policy argument remains in the copies API.
