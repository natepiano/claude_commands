# Production — build-followups

> **Status: PRODUCTION — running.** The five follow-ups from the user's 2026-10-04 adhoc review: cheaper test builds, and a build report that shows temp-folder builds, memory stalls and tests per edit, plus cancelling a superseded CI run, and a disk usage table (added by the user 2026-10-04); then builds that fit in memory, so earlyoom stops firing (the user, 2026-10-04). Since 2026-10-06: a shared screen recording skill, `/unit:report` timers that never go missing, Codex seats that survive a busy model, then an edit hook that stops long Rust functions as they are written, and fast fused multiply-add on natedev with a hook that has agents write it (the user, 2026-10-06).

## Production Context

- **Formerly:** `build-followups`
- **Source plans:** `docs/plans/build-followups.md` (now `docs/as-built/build-followups.md`), `docs/plans/build-followups-stalls.md` (now `docs/as-built/buildlog-memory-stalls.md`), `docs/plans/build-followups-ratio.md` (now `docs/as-built/buildlog-tests-per-edit-rust-release.md`), `docs/plans/build-followups-memory.md` (the user, 2026-10-04 11:46 PDT; now `docs/as-built/build-memory-admission.md`), `docs/plans/build-followups-notifier.md` (now `docs/as-built/validate-and-push-cancel-prior.md`), `docs/plans/build-followups-screen-record.md` (the user, 2026-10-06; now `docs/as-built/screen-record.md`), `docs/as-built/build-followups-fn-length-hook.md (run done; as-built 4ab5e4c)` (the user, 2026-10-06), `docs/plans/build-followups-mul-add.md` (now `docs/as-built/build-followups-mul-add.md`) (the user, 2026-10-06 08:2x PDT), `docs/plans/build-followups-screenshot.md` and `docs/plans/build-followups-enh-showrunner.md` (the user, 2026-10-06; now `docs/as-built/showrunner-automation.md`), and `docs/plans/build-followups-kill-order.md` (natedev, 2026-10-06, after earlyoom killed CI compilers before a hung 15 GB test; now `docs/as-built/build-memory-admission.md`), and `docs/plans/build-followups-test-speed.md` (the user, 2026-10-13; now `docs/as-built/merge-gate-and-test-speed.md`) — written 2026-10-04 by natedev from the adhoc review's five follow-up tasks (user: "launch it as a plan for a unit director to execute"; "you must use /showrunner:produce for this and start running dailies")
- **Repository:** `/home/natepiano/.claude`
- **Merge branch:** `natedev` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-natedev-trunk`
- **Showrunner session:** natedev
- **Log:** `docs/plans/natedev-log.md` — git-excluded; one line per event
- **Change review:** `~/rust/hanadocs/change-review/change-review - natedev.md` — changes for the user to check, read only when the user asks; never open questions
- **Proposals:** `~/rust/hanadocs/issues`
- **User zone:** America/Curacao (AST) — every time the showrunner reports is in this zone only (user, 2026-10-21: "My time zone is Curacao- I think that's ast. Please adjust"; EDT from 2026-10-12, PDT from 2026-10-04). user_zone.conf is the setting; this line only records it
- **Updates:** on demand (user, 2026-10-10: dailies only when the user asks); each dailies lists every unit; a simple dailies gives each waiting, idle unit one line
- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` and `basedpyright scripts/buildlog` (exits 0 when clean, with `0 errors, 0 warnings, 0 notes`), run in the showrunner checkout
- **Push:** git
- **Promote:** ~/.claude, mac ~/.claude
- **Capacity:** five units; `report.py` is shared, and whoever lands second resolves conflicts; its seats share natedev's build slots (steve)
- **Improvement queue:** ~/.local/state/plan-backlog/ledger.md
- **Efficiency ledger:** ~/.local/state/nightly-review/ledger.md

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |
| `settings.json` | stalls-unit | mul_add-unit (mul_add PostToolUse handler after fn-length) |
| `scripts/hooks/codex_hooks.py` | mul_add-unit (both fn-length and mul_add Codex handlers) | — |
| `scripts/hooks/fn_length_lib.py` | stalls-unit | mul_add-unit (shared scanner, edit parser, lint reader and multiply-add target-root helper) |
| `commands/showrunner/produce.md` | stalls-unit | enh-showrunner-unit (after stalls-unit Phase 6 merges); feature-log-unit (<ChangeReviewFeatureLog/> and <CloseChangeReview/>) |
| `commands/showrunner/promote_unit.md` | stalls-unit | enh-showrunner-unit (after stalls-unit Phase 6 merges) |
| `scripts/production/showrunners.py` | stalls-unit | enh-showrunner-unit (after stalls-unit Phase 6 merges) |
| `scripts/production/stall_watch.py` | stalls-unit | enh-showrunner-unit (after stalls-unit Phase 6 merges) |

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | enh-showrunner-unit Phase 4 | stalls-unit Phase 6 | cleared 16:00 PDT 2026-10-06: 966b814 merged, enh-showrunner told |

## Close-out

- followups-unit Phase 1 on main: mark `~/.local/state/nightly-review/ledger.md` line 15 accepted with the main sha — done (0bbccc0).
- stalls-unit Phase 1 and followups-unit Phase 3 on main: natedev adds the `buildlog sample` (60 s) and `buildlog disk` (10 min) timers to `/etc/nixos`; the user rebuilds — done (nixos d2b7778, rebuilt 11:55 PDT).
- notifier-unit Phase 1 on main: natedev tells the tool-based-ui showrunner about `validate_and_push.sh --cancel-prior` and the rule in `produce.md` — done 10:52 PDT.
- Delete `/etc/nixos/adhoc_review_2026-10-04.md` — done.

## Production rules

- **No CI on this repo** (natedev, 2026-10-04). `~/.claude` has no workflows, so the Merge tests replace `verify.sh test`, and <CIPoint/>, the smoke launch, the Mac run and `validate_and_push.sh` do not apply.
- **Each merged phase goes to main at once** (natedev, 2026-10-04), since every phase is useful alone and `~/.claude` main is the live configuration. After a merge's tests pass: merge `main` into the **Merge branch** if main moved, push the **Merge branch**, fast-forward `~/.claude` main to it (`git -C ~/.claude merge --ff-only <sha>`; other sessions' uncommitted files there stay untouched), and push main only when `origin/main..main` held no other session's commits before the fast-forward. Then the Mac pulls: `ssh mac 'cd ~/.claude && git pull --ff-only natedev:/home/natepiano/.claude main'`.
