# Feature log: an entry is pending or released for a crate, never both

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Refuses every write that would leave a feature log note holding one crate both as pending and at a released version, so no entry prints in two release sections.

> **As-built disposition: amend** — `docs/as-built/feature-log.md`

> **Production: build-followups** — unit `feature-log-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12 12:50 PDT

From the user (via the showrunner), answering whether an add-on that fixes a wrong result is the showrunner's call, with yours settled as refuse on yes: "Yes type the words. But when it comes to designs you absolutely should be able to tell units what to do and change. I do not have to be the one to approve and you should change any skill that says so" ("you" is the showrunner). So: next, refuse it.

The showrunner's assignment, 2026-10-12 13:00 PDT (a peer's coordination, on the user's word above): build the one item of `docs/plans/build-followups-feature-log-pending-next.md` now as a one-phase run: the refusal in `amend --backfill --release` plus its test. That file is consumed by this plan and is deleted with it at the end of the run.

The unit director's call, 2026-10-12: `add --backfill` and `supersede --backfill` accept `--release X@Unreleased X@1.2.3` in one call today and write the same wrong note, so the rule goes in the one function that validates every release list. It is a one-commit revert if the showrunner wants the correction path alone.

## Delegation Context

- **Project:** `~/.claude` config repo plus the feature log in the hanadocs vault. `docs/as-built/feature-log.md` describes the system; read its "Note format", "`feature_log.py`" and "Invariants" sections. Worktree `/home/natepiano/worktrees/claude-build-followups-feature-log-unit`, branch `build-followups-feature-log-unit` (unit `feature-log-unit-unit`); name both in every dispatch.
- **Project started:** 2026-10-12T20:02:56.779+00:00
- **Stack:** Python standard library only; `feature_log.py` stays importable on Python 3.10, and the tests run on natedev's `python3` 3.13.
- **Layout:**
  - `scripts/feature_log/` — `feature_log.py`, `test_feature_log.py` (and `inventory.py`, `test_inventory.py`, not touched here).
  - Vault, natedev: `/home/natepiano/rust/hanadocs/feature-log/{hana,cargo-liner,bevy_brp}/`. This plan only reads it, through `feature_log.py check`.
- **Key files:**
  - `scripts/feature_log/feature_log.py` — `VERSION_RE` 426; `_validate_release_values` 741-755, called for the flags of every verb that takes `--release` and by `validate_note` 784 (line 812) for a stored list; `_new_note` 839 (release flags at 884-889), used by `add`, `supersede` and a landing proposal; `_run_amend` 1686, whose release checks are 1770-1792: the additions are validated at 1770, the pending mark's refusals are 1771-1782, the one-call refusal is 1783-1788, the merge is 1789-1792; `_run_release` 2070 (the stamp); `_run_check` 2239.
  - `scripts/feature_log/test_feature_log.py` — `FeatureLogTests` 314, 110 cases on a temp vault and temp state. Helpers: `cli` 342, `add_arguments` 366, `note` 416, `vault_snapshot` 419, `assert_no_temporary_files` 462. Cases to read first: `test_amend_pending_refusals_leave_notes_and_events_unchanged` 1443, `test_amend_refuses_pending_and_exact_releases_of_the_same_crate_in_one_call` 1498, `test_amend_accepts_pending_and_exact_releases_of_different_crates_in_one_call` 1534, `test_check_reports_each_kind_of_broken_fixture` 2047.
- **Test lanes:** `scripts/feature_log/` → `test_feature_log.py` (beside the code; imports its sibling by bare name).
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` from the worktree root.
- **Lint:** `basedpyright scripts/feature_log`: a pass ends `0 errors, 0 warnings, 0 notes` (it exits 3: `pyrightconfig.json` names a `.venv` no checkout has). Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Invariants:**
  - `feature_log.py` is the only writer of notes; no note is written or edited by hand (as-built "Invariants").
  - Every refusal exits 2 with one stderr line `feature_log: <reason>`, no stdout, and nothing written: no event, no note, no temp file (as-built "`feature_log.py`").
  - `events.jsonl` lines already on disk stay readable, so no event gains or loses a key (as-built "Events").
  - Tests never touch the live vault or state: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, and no test runs `ssh`. Nothing else sets those two variables.
  - Against the live vault only `feature_log.py check` runs here; it reads and writes nothing. No `add`, `amend`, `supersede`, `release` or `changelog --save` runs against it (the unit director's rule for this plan: it changes code, not data).
  - Python: no `Any`, a `TypedDict` per JSON layout, no file-level ignores.
  - Seats run no cargo command, no `verify.sh`, no `nixos-rebuild`, `sudo` or `systemctl` change (the unit director's rule: nothing here needs a build).

## Phases

### Phase 1 — No write can leave an entry both pending and released for one crate  · status: done

#### As-built

- `_validate_release_values` refuses a release list that holds `X@Unreleased` and an exact `X@<version>`: `release cannot hold both X@Unreleased and X@<version>`. It runs after the per-value checks, so an invalid value, an unpublished crate or a crate missing from `--crate` is still reported first. The amend-only one-call loop and its wording `--release cannot add both …` no longer exist.
- Every caller gets it: `add`, `supersede`, `amend` and a landing proposal are refused before any write (the old note of a `supersede` stays live), and `validate_note` refuses a stored note, so `check` prints `<relative path>: release cannot hold both X@Unreleased and X@<version>` and exits 1.
- `_run_amend` refuses an exact version onto a note whose stored `release` holds `X@Unreleased`: `<title> already holds X@Unreleased`, before the event is journaled, beside the pending mark's refusal `<title> already holds X@<version>`. No other flag of the call is applied.
- Accepted: an exact version of another crate on a note pending for X, and a second exact version of X on a note that holds only exact versions of X.

**Files:**
- `scripts/feature_log/feature_log.py` — the rule in `_validate_release_values`; the stored-list refusal in `_run_amend`.
- `scripts/feature_log/test_feature_log.py` — 147 cases: both refusals, the accepted neighbours, three older fixtures that use `--backfill`.

**Gotchas:**
- A new entry without `--backfill` is pending for each published crate of `--crate`, so an exact `--release` of one of them is refused by the shared line; pass `--backfill` to record a released version.
- The version the refusal names is the first exact version of X in string order of the sorted list.

**Ruled out:**
- A separate refusal naming `--backfill` for a new entry: one rule and one wording stays.

