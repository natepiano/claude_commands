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

#### Work Order

**Goal:** every verb refuses, before any write, a release list that names one crate both as `Unreleased` and at an exact version, and `check` reports a stored note that holds both.

**Spec:**

- **One rule, in the validator.** `_validate_release_values` refuses a list that holds `X@Unreleased` and an exact `X@<version>`: `release cannot hold both X@Unreleased and X@<version>`, naming the first exact version of X in sorted order. It runs after the per-value checks, so an invalid value, an unpublished crate or a crate missing from `--crate` is still reported first, with today's wording. Every caller gets it:
  - `add --backfill`, `supersede --backfill` and `amend --backfill` with both values in one call are refused before any write. For `supersede` the old note stays live.
  - `validate_note` refuses a stored note that holds both, so `check` prints `<relative path>: release cannot hold both X@Unreleased and X@<version>` and exits 1. No note in the live vault holds both today; the acceptance gate proves it.
  - It replaces the one-call loop in `_run_amend` (1783-1788) and its wording `--release cannot add both …`; delete that loop.
- **A correction against the stored list.** `amend --backfill --release X@<version>` on a note whose `release` holds `X@Unreleased` is refused: `<title> already holds X@Unreleased`. It mirrors the pending mark's refusal `<title> already holds X@<version>` (1778-1781) and sits with it, before the event is journaled. No other flag of the call is applied.
- **Unchanged.** An exact version of another crate on a note pending for X is accepted. A second exact version of X on a note that holds only exact versions of X is accepted: six bevy_brp notes hold one crate at two released versions. The pending mark's two refusals keep their wording. `release` still replaces `X@Unreleased` with the version in one write. A live `add` still marks the note pending for each published crate of `--crate`. A value the note already holds prints `unchanged`.

**Files:**
- `scripts/feature_log/feature_log.py` — `_validate_release_values`, `_run_amend`.
- `scripts/feature_log/test_feature_log.py` — the cases below, and the changed wording in the one-call case at 1498.

**Seats:** `1 writer + 1 tester` — one function and one call site; each rule above is concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`.
- `test` — `scripts/feature_log/test_feature_log.py`: an exact version onto a note pending for the same crate refused with `<title> already holds X@Unreleased`, alone and with `--summary` and `--kind` in the same call, leaving vault, `events.jsonl` and temp files unchanged; the same call for another crate of the note accepted; a second exact version of one crate on a note that holds only an exact version accepted; the one-call `amend` case at 1498 expecting `release cannot hold both hana_lagrange@Unreleased and hana_lagrange@1.2.3` in all four argument orders; `add --backfill` with both values refused with that line and no note, event or temp file; `supersede --backfill` with both values refused and the old note still live; a stored note holding both reported by `check` with its path and that reason (a new fixture beside those at 2047); a list with two exact versions of one crate plus `Unreleased` naming the lower version; a pending note still stamped by `release`.

**Constraints from prior phases:** none. From the earlier feature-log runs (as-built): a refusal test asserts the vault, `events.jsonl` and temp files are unchanged; an `AmendEvent` is journaled before its note is written, so a refusal must come before the event.

**Acceptance gate:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` green; `basedpyright scripts/feature_log` ends `0 errors, 0 warnings, 0 notes`; `python3 scripts/feature_log/feature_log.py check` from the worktree root prints `ok: <n> notes` for the live vault.
