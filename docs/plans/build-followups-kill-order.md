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
- **Stack:** bash (`scripts/lint/invoke.sh`); tests in Python `unittest` beside it (`scripts/lint/test_*.py`).
- **Test:** `(cd scripts/lint && python3 -m unittest discover -q)`; `bash -n scripts/lint/invoke.sh`.
- **Invariants:** the step's exit status and output are unchanged. A failed write to `/proc/self/oom_score_adj` is silent and never stops the step (`invoke.sh`'s callers run `set -euo pipefail`). The Mac, which has no scope, is unchanged.

## Phases

### Phase 1 — Build steps run at oom_score_adj 500 · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT.

**Goal:** every process a natedev build step starts inside its builds.slice scope carries oom_score_adj 500.

**Spec:**
- In `BUILDLOG_SCOPE_SH`, after the marker write and before `"$@"`: `{ echo 500 > /proc/self/oom_score_adj; } 2>/dev/null;`. Nothing else in the string changes.
- Update the comment block above `BUILDLOG_SCOPE_SH` with one sentence: the scope raises oom_score_adj to 500, so earlyoom kills build steps before CI (100) and sessions (200), the largest first.
- Update `docs/as-built/build-memory-admission.md` where it describes the kill order, with the 2026-10-06 06:54 PDT case in one line.

**Files:** `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py` (new), `docs/as-built/build-memory-admission.md`.

**Seats:** 1 writer + 1 tester.
- `impl`: `scripts/lint/invoke.sh` and the as-built.
- `test`: `scripts/lint/test_invoke_scope.py`, from the Spec alone. It runs the `BUILDLOG_SCOPE_SH` string through `/bin/sh -c` with a temporary marker path, without systemd-run. The step is `sh -c 'cat /proc/self/oom_score_adj'` and prints 500. A child of the step also prints 500. The step's exit status passes through (exit 7 → 7). When the write fails, the step still runs and nothing reaches stderr; the test makes it fail by running the string with `/proc/self/oom_score_adj` replaced by a read-only path, or skips with a reason when it cannot.

**Acceptance gate:**
- Both test commands green.
- Live check (unit director, natedev, after the merge reaches `~/.claude` main): while any real build step runs, `/proc/<its rustc pid>/oom_score_adj` reads 500.
