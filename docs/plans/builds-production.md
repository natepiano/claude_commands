# Production — builds

> **Status: PRODUCTION — running.** The build system every session uses on natedev and the Mac: memory admission, the build queue, build holds, slots and disk sweeps; `verify.sh`, its pass records and the Mac test offload; and the build log with its reports (the user, 2026-10-29, via natedev).

## Production Context

- **Source plans:** none yet. The user set up this production on 2026-10-29, via natedev: natedev's builds-unit becomes the builds showrunner and owns three units. /producer:to_production did not split it. Each unit writes its first plan, and its row below then names it. The work starts from builds-unit's as-built docs, listed in each unit's Owns cell.
- **Repository:** `/home/natepiano/.claude`
- **Merge branch:** `builds` — every unit merges here; only the showrunner pushes it
- **Showrunner checkout:** `/home/natepiano/worktrees/claude-natedev-builds` — the builds session's worktree, where `/showrunner:promote_showrunner` resumes it; it moves to `builds` right after the promotion (Launch order)
- **Showrunner session:** builds
- **Log:** `docs/plans/builds-log.md` — untracked: never added or committed (never `git add -A` or `-a`); one line per event
- **Updates:** on demand (user, 2026-10-13: no timer unless asked); each update reports every unit in full
- **Merge tests:** `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` and `basedpyright scripts/buildlog scripts/build_hold scripts/build_queue scripts/build_targets scripts/mac_test scripts/lint scripts/direct` (pass is its `0 errors, 0 warnings` line), run in the showrunner checkout
- **Push:** git
- **Promote:** ~/.claude, mac ~/.claude
- **Capacity:** natedev has 32 cores and 60 GB of memory; three units. The work is Python and shell scripts and docs. The tests run under the merge gate's memory cap. Its workers share natedev's build slots with natedev and sdlc.
- **Change review:** `~/rust/hanadocs/change-review/change-review - natedev.md` — builds is a child of natedev, so it names its root's list
- **Proposals:** `~/rust/hanadocs/issues`

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
| resources-unit | `docs/plans/builds-resources.md` | `/home/natepiano/worktrees/claude-natedev-builds-resources` | `builds-resources` | — | memory admission, the build queue, build holds, build targets and slots, the /tmp and disk sweeps: `docs/as-built/build-memory-admission.md`; `docs/as-built/build-targets.md`; `scripts/lint/memory_gate.sh`, `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`; `scripts/build_hold/`; `commands/build/hold.md`; `scripts/build_targets/`; `config/build_targets.conf`; `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/hooks/test_brp_launch_gate.py`; `scripts/hooks/pre-tool-use-build-hold-guard.sh`, `scripts/hooks/pre-tool-use-build-hold-guard.py`, `scripts/hooks/test_build_hold_guard.py`; `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`; `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`; `scripts/lint/folder_audit.py`, `scripts/lint/test_folder_audit.py`; `config/rust_roots.txt`; `scripts/lib/scratch.py`, `scripts/lib/test_scratch.py`; `scripts/hooks/pre-tool-use-scratch-guard.py`, `scripts/hooks/test_scratch_guard.py`; `scripts/hooks/session-start-temp-root.py`, `scripts/hooks/test_session_temp_root.py`; `scripts/direct/remove_run_scratch.py`, `scripts/direct/test_remove_run_scratch.py`; changes to `/etc/nixos` (steve slots, `builds.slice`, `modules/linux/disk-floor.nix`, the buildlog timers) go through the natedev session |
| report-unit | `docs/as-built/build-memory-admission.md`; `docs/as-built/buildlog-memory-stalls.md`; `docs/as-built/session-roster.md` (run done; as-built) | `/home/natepiano/worktrees/claude-natedev-builds-report` | `builds-report` | — | the build log, the `/build:report` report (formerly `/builds`), and the `/build:queue` and `/build:status` tables: `scripts/buildlog/` (except `port_lint.py`, `mac_idle.py` and their tests, which verify-unit owns, and the one `buildlog hourly` call site, which natedev's screenshot-unit owns); `scripts/build_queue/`; `commands/build/report.md`, `commands/build/mac_report.md`, `commands/build/queue.md`, `commands/build/status.md`; `docs/as-built/build-followups.md`; `docs/as-built/buildlog-memory-stalls.md`; `docs/as-built/buildlog-tests-per-edit-rust-release.md`; `docs/as-built/build-report-session.md` |
| verify-unit | `docs/plans/natedev-verify.md` | `/home/natepiano/worktrees/claude-build-followups-verify` | `natedev-verify` | — | `verify.sh`, the final-pass record and query, the verify cache, lint and test scope, verify speed and the Mac test code: `scripts/direct/verify.sh`; `scripts/direct/test_verify_*.py`; `scripts/direct/final_pass.py`; `scripts/direct/step_record.py`, `scripts/direct/test_step_record.py`; `scripts/direct/local_release_wait.py`; `scripts/direct/verify_saved.py`, `commands/verify_saved.md`; `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/config_values.py`, `scripts/lint/scope.py`, `scripts/lint/nextest_budget.py`, `scripts/lint/port-lint`, `scripts/lint/test_invoke_scope.py`, `scripts/lint/test_nextest_budget.py`, `scripts/lint/test_nextest_width.py`; `config/lint.conf`, `commands/lint_config.md`; `scripts/buildlog/port_lint.py`, `scripts/buildlog/test_port_lint.py`; `scripts/buildlog/mac_idle.py`, `scripts/buildlog/test_mac_idle.py`; `scripts/mac_test/`; `config/mac_test.conf`; `commands/build/mac.md`; `scripts/production/mac_run.sh`; `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py`, `scripts/hooks/test_checkpoint_doc_gate.py`; `docs/as-built/mac-test-offload.md`; `docs/as-built/rustdoc-once-and-final-targets.md`; `docs/as-built/nextest-width-and-lint-scope.md`; also touches `commands/showrunner/produce.md` (hub; one clause in the Mac run rule), `commands/unit/direct.md` (the verify rows), `config/README.md` and `pyrightconfig.json` (one entry each) |

Scope of each unit:

- **resources-unit** — what a build may use and when it starts: memory admission and the memory queue, build holds, build targets and their slots, the cargo wrapper, and the sweeps that keep `/tmp` and the disk under their limits.
- **report-unit** — what the build log records and what its reports show: `/build:report`, `/build:mac_report`, and the `/build:queue` and `/build:status` tables. The user has build report changes coming; they go to this unit.
- **verify-unit** — `verify.sh` and how fast it runs: the final pass, the verify cache, lint and test scope, and the Mac test offload.

## Hub files

| File | Owner unit | Other units that touch it |
| --- | --- | --- |
| `scripts/direct/verify.sh` | verify-unit | resources-unit (memory kill retry, hold wait, cargo token); report-unit (the build log record calls) |
| `scripts/lint/invoke.sh` | verify-unit | resources-unit (the memory gate calls) |
| `config/lint.conf` | verify-unit | resources-unit (the `[sweep]` section) |
| `scripts/lint/memory_admit.py` | resources-unit | report-unit (the queue view that `build_queue.py` reads) |
| `scripts/build_targets/build_targets.py` | resources-unit | report-unit (the `status` table) |
| `scripts/buildlog/record.py` | report-unit | verify-unit, resources-unit (the rows each one writes) |

`commands/showrunner/produce.md`, `settings.json` (hook entries) and `pyrightconfig.json` are hub files in natedev. An edit to one of them goes through cargo-berth's normal flow, and the checkpoint notice names the natedev owner.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |

None yet. When a plan needs another unit's checkpoint, the gate goes here.

## Close-out

- Each unit's as-built doc is in `docs/as-built/`, and the docs it replaces are deleted or amended.
- Tell the natedev showrunner about each change to a natedev hub file once it is on main.
- Send each `/etc/nixos` change a unit needs (slots, `builds.slice`, disk floor, timers) to the natedev session; the user rebuilds.

## Production rules

- **A child of natedev** (the user, 2026-10-29). builds reports to natedev in the showrunner tree: `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py parent builds` prints natedev. The promotion attaches it. If it does not, attach it with `/showrunner:attach`. natedev never merges `builds`; each merged phase goes to main, as below.
- **Launch order** (natedev, 2026-10-29). builds-unit commits this doc on `natedev-builds`, ends its `/unit:direct` run, and natedev merges the doc. Then natedev runs `/showrunner:promote_showrunner builds-unit docs/plans/builds-production.md`. The builds showrunner then creates `builds` from `origin/natedev-builds`, switches its checkout to it and pushes it. Then it starts resources-unit and report-unit with `/showrunner:add_unit <name> --brief "<goal>"`, which writes each stub plan at the path in its Plan cell. Then it asks natedev to transfer verify-unit.
- **Not builds work** (the user, 2026-10-29). Production rename and worker identity belong to natedev's shutdown-unit. A finding there goes to the natedev showrunner, not to a builds unit.
- **Build speed is a top priority** (the user, 2026-10-07: understanding builds deeply and speeding them up ranks among the most important work).
- **Owners in natedev** (2026-10-21, after the sdlc showrunner sent back a checkpoint whose notice named only its own hub owners). A checkpoint that changes a file a natedev unit owns (natedev's Units → Owns, and Hub files) names each owner in its notice: `also touches <paths> (owner <unit>), tested against <owner origin tip>, clean`, after `git merge-tree --write-tree HEAD origin/<owner branch>`. Also trial-merge against `origin/natedev`.
- **No CI on this repo** (natedev, 2026-10-04, for `~/.claude`). `~/.claude` has no workflows, so the Merge tests replace `verify.sh test`, and <CIPoint/>, the smoke launch, the Mac run and `validate_and_push.sh` do not apply.
- **Each merged phase goes to main at once** (natedev, 2026-10-04, for `~/.claude`), because `~/.claude` main is the live configuration. After a merge's tests pass: merge `main` into `builds` if main moved, push `builds`, fast-forward `~/.claude` main to it (`git -C ~/.claude merge --ff-only <sha>`; other sessions' uncommitted files there stay untouched), and push main only when `origin/main..main` held no other session's commits before the fast-forward. Then the Mac pulls: `ssh mac 'cd ~/.claude && git pull --ff-only natedev:/home/natepiano/.claude main'`.
