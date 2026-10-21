# Feature log: a note's completed date defaults to its commit's date

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A new feature log note given `--link` and no `--completed` takes the linked commit's date, not the day it was logged.

> **As-built disposition: amend** — `docs/as-built/feature-log.md`

> **Production: build-followups** — unit `feature-log-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The showrunner's assignment, 2026-10-21 02:05 EDT: "When feature_log.py gets no completed date, it uses the logging date, so nine claude feature notes for commits from 10-15 show 10-16, 10-19 or 10-20. With a --link commit, the completed date should default to that commit's date (the merge date). Fix it with a test, and send a checkpoint."

The unit director's calls, 2026-10-21:
- The date comes from the project's local checkouts on natedev, where every write runs (a forwarded call runs natedev's `~/.claude`). It is the commit's committer date, in the user's zone. For a merge commit that is the merge date. Local git needs no network and no GitHub sign-in.
- When no checkout holds the commit, the write is refused and names `--completed`. A silent fallback to today is the defect itself.
- The nine wrong notes are data in the live vault. This plan changes code only. After the fix is on `~/.claude` main, the unit offers the showrunner the nine `amend --backfill` corrections.

## Delegation Context

- **Project:** `~/.claude` config repo plus the feature log in the hanadocs vault. `docs/as-built/feature-log.md` describes the system; read its "Note format", "`feature_log.py`" and "Invariants" sections. Worktree `/home/natepiano/worktrees/claude-build-followups-feature-log`, branch `build-followups-feature-log` (unit `feature-log-unit`); name both in every dispatch.
- **Project started:** 2026-10-21T06:09:06.134+00:00
- **Stack:** Python standard library only; `feature_log.py` stays importable on Python 3.10, and the tests run on natedev's `python3` 3.13.
- **Key files:**
  - `scripts/feature_log/feature_log.py` — `ProjectSpec` 25 and `PROJECTS` 31-113; `today()` 343; `_canonical_link` 769 (a link is `https://github.com/<repo>/commit/<40-hex>`); `_new_note` (the default is `completed = … or today()` at 884), used by `add`, `supersede` and `propose`; `_run_propose` 1574 keeps a saved `completed` when the flag is omitted; `user_zone()` is imported from `scripts/lib/user_zone.py`.
  - `scripts/feature_log/test_feature_log.py` — `FeatureLogTests`, 117 cases on a temp vault and temp state. Helpers `cli`, `add_arguments`, `note`, `vault_snapshot`, `assert_no_temporary_files`.
- **Local checkouts on natedev (origin in brackets):** hana → `~/rust/hana`, then `~/rust/hana_catalyst` [both `natepiano/hana`; separate clones]; natedev → `/etc/nixos`; cargo-liner, bevy_brp, obsidian_knife, nateroids → `~/rust/<name>`; claude → `~/.claude` [`natepiano/claude_commands`; its worktrees share objects].
- **Test:** `python3 -m unittest discover -s scripts/feature_log -p 'test_*.py'` from the worktree root.
- **Lint:** `basedpyright scripts/feature_log`: a pass ends `0 errors, 0 warnings, 0 notes`. Lint once after the edits; on failure fix what it names, then lint once.
- **Invariants:**
  - Every refusal exits 2 with one stderr line `feature_log: <reason>`, no stdout, and nothing written: no event, no note, no temp file.
  - `events.jsonl` lines already on disk stay readable; no event or proposal gains or loses a key.
  - Tests never touch the live vault, the live state or a real checkout: `FEATURE_LOG_VAULT` and `XDG_STATE_HOME` point at temp dirs, the checkouts at temp git repos, and no test runs `ssh` or reaches the network.
  - Against the live vault only `feature_log.py check` runs here. No `add`, `amend`, `supersede`, `propose`, `release` or `changelog --save` runs against it.
  - Python: no `Any`, no file-level ignores.

## Phases

### Phase 1 — A note's completed date defaults to its commit's date  · status: done

#### Work Order

**Worktree and branch:** `/home/natepiano/worktrees/claude-build-followups-feature-log`, branch `build-followups-feature-log`.

**Goal:** `add`, `supersede` and `propose` without `--completed` set `completed_date` to the linked commit's committer date in the user's zone.

**Spec:**

- **Where each project's commits live.** `ProjectSpec` gains `checkouts: tuple[Path, ...]`, filled from the list in Delegation Context, in that order.
- **The lookup.** One function takes the project and the canonical link's 40-hex sha. For each checkout in order, it runs `git -C <checkout> show -s --format=%cI <sha>` without a shell. The first checkout that exits 0 gives the date: parse the ISO time, convert it to `user_zone()`, and return its `YYYY-MM-DD`. A missing checkout counts as a miss.
- **The default.** In `_new_note`, an omitted `--completed` takes the lookup's date in place of `today()`. A given `--completed` runs no git and stays as it is. `date_created` and `date_modified` stay `today()`.
- **The refusal.** When every checkout misses, the write is refused before anything is written: `cannot date commit <sha>: no checkout of <repo> holds it; pass --completed`.
- **Unchanged.** `propose` keeps an open proposal's saved `completed` when the flag is omitted, and runs no lookup then. A repeat whose `--completed` was omitted still compares apart from `completed_date`. `amend --backfill` still takes `--link` and `--completed` together.

**Files:**
- `scripts/feature_log/feature_log.py` — `ProjectSpec`, `PROJECTS`, `_new_note`, one new lookup function.
- `scripts/feature_log/test_feature_log.py` — the cases below. Existing cases that omit `--completed` must point their project at a temp checkout that holds their link, through one shared helper.

**Seats:** `1 writer + 1 tester` — one function and one call site, each rule concrete enough to test first.
- `impl` — `scripts/feature_log/feature_log.py`.
- `test` — `scripts/feature_log/test_feature_log.py`, with temp git repos whose commits carry a fixed `GIT_COMMITTER_DATE`:
  - `add` without `--completed` takes the commit's date; a committer time of 01:30 UTC dates to the day before in the user's zone.
  - `add` with `--completed` keeps that date and runs no git.
  - the second checkout of a project supplies the date when the first lacks the commit, or does not exist.
  - no checkout holds the commit: exit 2, the reason line, and no note, event, proposal or temp file.
  - `supersede` and `propose` without `--completed` take the commit's date; `add --review` lands a proposal with the date `propose` saved.

**Constraints from prior phases:** none. From the earlier feature-log runs (as-built): a refusal test asserts the vault, `events.jsonl` and temp files are unchanged.

**Acceptance gate:** the test command green; `basedpyright scripts/feature_log` ends `0 errors, 0 warnings, 0 notes`; `python3 scripts/feature_log/feature_log.py check` from the worktree root prints `ok: <n> notes` for the live vault.
