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

#### As-built

- `ProjectSpec.checkouts: tuple[Path, ...]` lists each project's local checkouts on natedev, in lookup order: hana → `~/rust/hana`, `~/rust/hana_catalyst`; natedev → `/etc/nixos`; cargo-liner, bevy_brp, obsidian_knife, nateroids → `~/rust/<name>`; claude → `~/.claude`.
- `_commit_date(project: ProjectSpec, sha: str) -> str` runs `git -C <checkout> show -s --format=%cI <sha>` with no shell in each checkout in order. The first exit 0 gives the committer date, converted to `user_zone()` as `YYYY-MM-DD`. A missing checkout, a non-zero exit or an `OSError` is a miss.
- `_new_note` takes `PROJECTS[<project>]` at call time. An omitted `--completed` takes `_commit_date` of the canonical link's sha; a given `--completed` runs no git. `date_created` and `date_modified` stay `today()`. `add`, `supersede` and `propose` all go through it; `propose` saves the resolved date, so `add --review` and `supersede --review` land that date.
- Refusals, before anything is written: `cannot date commit <sha>: no checkout of <repo> holds it; pass --completed`, and `cannot date commit <sha>: invalid committer date in <checkout>; pass --completed` when git output does not parse.

**Files:**
- `scripts/feature_log/feature_log.py` — `ProjectSpec.checkouts`, `PROJECTS`, `_commit_date`, the default in `_new_note`.
- `scripts/feature_log/test_feature_log.py` — `temporary_checkout` builds temp git repos with fixed committer dates and no global or system git config; every project points at one shared temp checkout in `setUp`; cases cover zone conversion, an explicit date with no git, the second checkout, the refusal with no writes, `supersede`, and `propose` with landing.

**Gotchas:**
- A test that omits `--completed` needs a link to a real commit in a temp checkout; a made-up sha is refused.
- `basedpyright scripts/feature_log` exits 3 in a worktree with no `.venv`; the result line (`0 errors, 0 warnings, 0 notes`) is the gate.
