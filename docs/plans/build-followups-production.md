# Production — build-followups

> **Status: PRODUCTION — running.** The five follow-ups from the user's 2026-10-04 adhoc review: cheaper test builds, and a build report that shows temp-folder builds, memory stalls and tests per edit, plus cancelling a superseded CI run, and a disk usage table (added by the user 2026-10-04).

## Production Context

- **Source plans:** `docs/plans/build-followups.md`, `docs/plans/build-followups-stalls.md`, `docs/plans/build-followups-ratio.md`, and `docs/plans/build-followups-notifier.md` (now `docs/as-built/validate-and-push-cancel-prior.md`) — written 2026-10-04 by natedev from the adhoc review's five follow-up tasks (user: "launch it as a plan for a unit director to execute"; "you must use /showrunner:produce for this and start running dailies")
- **Repository:** `/home/natepiano/.claude`
- **Merge branch:** `build-followups` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-build-followups-trunk`
- **Showrunner session:** natedev
- **Log:** `docs/plans/build-followups-log.md` — git-excluded; one line per event
- **User zone:** America/Los_Angeles — every time the showrunner reports is in this zone only (user, 2026-10-04: "operate in PDT going forward")
- **Updates:** every 30 minutes; each update reports every unit in full
- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` and `basedpyright scripts/buildlog` (pass is its `0 errors, 0 warnings` line: it exits 3 because pyrightconfig names a `.venv` no checkout has), run in the showrunner checkout
- **Capacity:** four units; `report.py` is shared, and whoever lands second resolves conflicts; its seats share natedev's build slots (steve)

## Units

| Unit | Plan | Worktree | Branch | Session | Port | Owns |
| --- | --- | --- | --- | --- | --- | --- |
| followups-unit | `docs/plans/build-followups.md` | `/home/natepiano/worktrees/claude-build-followups` | `build-followups-unit` | `build-followups` | — | `scripts/delegate/verify.sh`, `scripts/buildlog/`, `commands/unit/delegate.md` (the `--filter` row) |
| notifier-unit | `docs/as-built/validate-and-push-cancel-prior.md` (run done; as-built merged as 8772951) | `/home/natepiano/worktrees/claude-build-followups-notifier` | `build-followups-notifier` | `session-notifier` (resumed in `~/.claude`, the directory its session began in) | — | `scripts/validate_and_push/`, `commands/showrunner/produce.md` (the cancel-prior rule); promoted from tool-based-ui by the user 2026-10-04 |
| stalls-unit | `docs/plans/build-followups-stalls.md` | `/home/natepiano/worktrees/claude-build-followups-stalls` | `build-followups-stalls` | `nightly-config` (resumed in `/etc/nixos`) | — | memory stall recording and its report section; promoted by the user 2026-10-04 |
| ratio-unit | `docs/plans/build-followups-ratio.md` | `/home/natepiano/worktrees/claude-build-followups-ratio` | `build-followups-ratio` | `nightly-rust` (resumed in `~/rust`) | — | the tests-per-edit report section; promoted by the user 2026-10-04 |

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |

## Close-out

- followups-unit Phase 1 on main: mark `~/.local/state/nightly-review/ledger.md` line 15 accepted with the main sha — done (0bbccc0).
- stalls-unit Phase 1 on main: natedev adds the `buildlog sample` timer the unit names to `/etc/nixos` (commit, push); the user rebuilds.
- notifier-unit Phase 1 on main: natedev tells the tool-based-ui showrunner about `validate_and_push.sh --cancel-prior` and the rule in `produce.md` — done 10:52 PDT.
- Delete `/etc/nixos/adhoc_review_2026-10-04.md` — done.

## Production rules

- **No CI on this repo** (natedev, 2026-10-04). `~/.claude` has no workflows, so the Merge tests replace `verify.sh test`, and <CIPoint/>, the smoke launch, the Mac run and `validate_and_push.sh` do not apply.
- **Each merged phase goes to main at once** (natedev, 2026-10-04), since every phase is useful alone and `~/.claude` main is the live configuration. After a merge's tests pass: merge `main` into `build-followups` if main moved, push `build-followups`, fast-forward `~/.claude` main to it (`git -C ~/.claude merge --ff-only <sha>`; other sessions' uncommitted files there stay untouched), and push main only when `origin/main..main` held no other session's commits before the fast-forward. Then the Mac pulls: `ssh mac 'cd ~/.claude && git pull --ff-only natedev:/home/natepiano/.claude main'`.
