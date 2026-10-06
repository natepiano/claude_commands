# Build kill order: the largest build dies first

> **Status: IMPLEMENTATION PLAN — one phase, delegate-ready.** When natedev runs out of memory, earlyoom kills processes from local build steps first, largest first; CI next; the user's sessions last.

> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The Build memory topic's closing condition failed at 06:54 PDT on 2026-10-06. Two hung hana tests in one local `verify.sh test` step held 11.4 GB and 15.5 GB. earlyoom first sent SIGTERM to 26 other processes, including six CI compilers, which failed four jobs in hana CI run 37474349558. The tests were the last two it killed. Investigation by natedev's helper, read only:
- **Why the tests outlasted the compilers.** earlyoom's `--prefer '^(rustc|rustdoc|clippy-driver|cargo-mend|ld[.]mold|cc1|cc1plus)$'` (`/etc/nixos/modules/linux/memory.nix:51-52`) adds 300 to a compiler's score, the weight of about 44 GiB.
- **The scores.** Score is roughly usage‰ of RAM plus swap (98.7 GiB), plus oom_score_adj.
  - The tests ranked 874 and 902 (adj 200).
  - A 2 GB CI rustc ranked 1048 (adj 100, `hana-runners.nix:548`).
  - An 80 MB session rustc ranked 1100 (adj 200).
  - The drkonqi crash processors ranked 1000 (adj 500).
- **Why the builds.slice cap never acted.** The slice's 44G cap (`development.nix:142`) never bound: builds.slice peaked at 37.7 GiB. Before that, zram (10 GiB), sessions (8 GiB) and CI (3.4 GiB) took the machine to earlyoom's 5% line.

## Decisions (showrunner)

- **Order: local build steps first, then CI, then sessions; within each, the largest first.** CI stays at adj 100 (its 2026-10-04 reason holds: a session's build goes before CI). Build steps move to adj 500. With no `--prefer`, a hung 15 GB test scores about 1100 against a 2 GB CI compiler's 750, and a session's rust-analyzer at 200 stays behind every build step of similar size.
- **The adj is set inside the step's scope** in `BUILDLOG_SCOPE_SH` (`scripts/lint/invoke.sh:82`), so every process the step starts inherits it: cargo, rustc, linkers, test binaries. An unprivileged process may raise its own oom_score_adj. Builds outside `invoke.sh` keep 200.
- **natedev, not the unit, drops `--prefer` and rewrites the comment** in `/etc/nixos/modules/linux/memory.nix:25-28`, plus the comment at `hana-runners.nix:542-547`, after this phase reaches `~/.claude` main. The user rebuilds. Each change alone moves the order the right way, so either may land first.

## Delegation Context

- **Project:** `~/.claude`. Work in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`.
- **Project started:** 2026-10-06T14:22:47.217+00:00
- **Stack:** bash (`scripts/lint/invoke.sh`); tests in Python `unittest` beside it (`scripts/lint/test_*.py`).
- **Test:** `(cd scripts/lint && python3 -m unittest discover -q)`; `bash -n scripts/lint/invoke.sh`.
- **Invariants:** the step's exit status and output are unchanged. A failed write to `/proc/self/oom_score_adj` is silent and never stops the step (`invoke.sh`'s callers run `set -euo pipefail`). The Mac, which has no scope, is unchanged.

## Phases

### Phase 1 — Build steps run at oom_score_adj 500 · status: done

#### As-built

`BUILDLOG_SCOPE_SH` in `scripts/lint/invoke.sh` runs `{ echo 500 > /proc/self/oom_score_adj; } 2>/dev/null || true;` after its marker write and before `"$@"`, so every process a build step starts in its builds.slice scope inherits oom_score_adj 500 and earlyoom kills build steps before CI (100) and sessions (200), the largest first; the step's exit status and output are unchanged. The `|| true` keeps a failed write from ending the scope shell when a caller exports `SHELLOPTS=errexit`.

**Files:**
- `scripts/lint/invoke.sh` — the scope string and the comment above it stating the kill order
- `scripts/lint/test_invoke_scope.py` — runs the extracted string under `/bin/sh -c` with fd 3 set as `buildlog_exec` sets it: step and child read 500, exit 7 passes through, a failed write stays silent under exported errexit; the 500 checks skip when the runner already runs at 500 or more
- `docs/as-built/build-memory-admission.md` — the kill-order row and the 2026-10-06 06:54 PDT case

**Gotchas:** compiles the sccache server runs live under `sccache.service`, not under the step, so the scope's 500 never reaches them; they get 500 from `OOMScoreAdjust=500` on that unit in /etc/nixos (`modules/linux/development.nix`), alongside dropping earlyoom's `--prefer` (`modules/linux/memory.nix`).
