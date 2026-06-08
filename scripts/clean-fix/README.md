# Clean-fix Rust Pipeline

Automated clean-fix clean-build, style evaluation, and style-fix pipeline for all Rust projects under `~/rust/`.

Runs daily at **4:00 AM** via launchd.

## Files

### Orchestration

| File | Purpose |
|------|---------|
| `clean-fix.sh` | Main entry point. For each `[build]` target: clean, build, mend. Then runs style eval + style-fix if the style mode is not `off`. Generates the clean-fix report at the end. |
| `clean-fix.conf` | Configuration. Two opt-in allowlists: `[build]` (clean/build/mend) and `[targets]` (style eval/review/fix). Plus style eval settings (`mode`, `max_new_findings`) and warmup targets. No deny list — nothing runs unless listed. |
| `com.natemccoy.clean-fix.plist` | launchd plist — schedules the clean-fix job at 4:00 AM. |
| `setup.sh` | Idempotent setup script — installs the launchd agent, creates runtime directories. |

### Style Evaluation

| File | Purpose |
|------|---------|
| `style-eval-all.sh` | Runs `/style_eval` on every `[targets]` entry in parallel. Stores pending evaluation markdown in `.history/.pending/<project>.json`. Skips projects with pending findings or a real `_style_fix` worktree so pending JSON cannot be replaced while fixes are awaiting review. |
| `rg-shim.sh` | Timeout shim for `ripgrep`. Bounds every non-interactive `rg` so a path-less search blocked on stdin can't hang forever. See **Reliability guards** below. |

### Style-Fix Worktrees

| File | Purpose |
|------|---------|
| `style-fix-worktrees.sh` | For each project with pending findings: creates a `_style_fix` worktree, exports evaluation markdown to a scratch file under `/private/tmp/claude`, launches the configured style agent to apply fixes (cargo mend, clippy, tests, style review), saves the Fix Summary back into pending JSON, and keeps `EVALUATION.md` out of the worktree. Other linked worktrees are allowed; the primary checkout still must be clean. Can target a single project by name. |

### Warmup

| File | Purpose |
|------|---------|
| `clean-fix-warmup.sh` | Warms up Bevy apps by launching them briefly, polling the BRP endpoint until ready, then shutting them down. Configured via the `[warmup]` section in `clean-fix.conf`. |

### Flowchart Diagram

| File | Purpose |
|------|---------|
| `clean-fix-style-flow.dot` | Graphviz source for the pipeline flowchart. Defines nodes, edges, positions, and cluster membership. |
| `render-flow.py` | Renders the dot file to SVG. Parses cluster definitions from the dot file, runs `neato -n2`, then injects dashed cluster borders and rewrites the SVG viewBox. |
| `clean-fix-style-flow.svg` | Generated output — do not edit by hand. |

## Reliability guards (the rg-hang)

On **2026-06-02** a nightly run wedged for 12+ hours and produced no style-fix
worktrees. Two style-eval agents had issued pipelines like
`rg PATTERN -g '*.rs' | rg -v X | head` where the first `rg` has glob filters
but **no path argument**. With no path, `rg` searches stdin whenever stdin is
not a terminal; claude's Bash tool hands each command an open stdin pipe that
never delivers data and never closes, so that first `rg` blocked on `read()`
forever. The eval stage's serial `wait` then stalled, the parent `clean-fix.sh`
stayed alive, and the launchd trigger's `pgrep` concurrency guard suppressed
every subsequent run all night.

Two layers now prevent a recurrence:

1. **Eval-stage watchdog** (`style-eval-all.sh`). The per-agent wait is
   `wait_or_timeout`, which kills the agent's whole process tree (subshell +
   `claude`/`codex` + any `rg`/`zsh` grandchildren) after `agent_timeout_secs`
   (from `[style_fix]` in `clean-fix.conf`, default 2h). Containment: one hung
   agent can no longer stall the pipeline.

2. **`rg` timeout shim** (`rg-shim.sh`) — the source-level guard. Interactive
   `rg` (stdin is a tty) is a transparent passthrough; non-interactive `rg`
   runs under a watchdog that kills it after `RG_SHIM_TIMEOUT` seconds
   (default 60). A path-less `rg` blocked on a dead pipe dies in seconds; normal
   searches finish in milliseconds and never hit the cap.

   **Activation** is a symlink:

   ```
   ~/.claude/scripts/rg -> clean-fix/rg-shim.sh
   ```

   `~/.claude/scripts` sits ahead of `/opt/homebrew/bin` on PATH (set in
   `.zshrc`), for both the launchd agent's snapshot PATH and interactive shells,
   so the shim wins `rg` resolution everywhere. To deactivate, remove the
   symlink (`rm ~/.claude/scripts/rg`) — real `rg` resolves again immediately.

## Generating the flowchart

Prerequisites: `graphviz` (`brew install graphviz`), Python 3.

```bash
cd ~/.claude/scripts/clean-fix
python3 render-flow.py
```

The render script:
1. Parses cluster membership, labels, and colors from the `.dot` file
2. Runs `neato -n2` to produce an SVG with exact node positioning
3. Injects dashed border rectangles and labels for each `subgraph cluster_*` block
4. Aligns the tops of Phase 1/2/3 clusters
5. Rewrites the SVG `viewBox` to fit all content with uniform padding

To modify the diagram, edit `clean-fix-style-flow.dot` and re-run `python3 render-flow.py`. See the layout guide comment at the top of the dot file for details on adding/removing nodes and phases.

## Pipeline flow

```
Clean-fix Build (4:00 AM)
  │
  ├─ Phase 1: Clean + Rebuild (per project, sequential)
  │    cargo clean → cargo build → cargo clippy → warmup
  │
  ├─ Phase 2: Style Evaluation (per project, parallel)
  │    Load style guide → survey code → carry forward valid findings
  │    → skip any project with pending findings or a _style_fix worktree
  │    → find new violations → store pending evaluation markdown
  │
  ├─ Phase 3: Style-Fix Worktrees (per project, parallel)
  │    Create _style_fix worktree (other linked worktrees allowed if primary is clean)
  │    → export pending evaluation markdown to scratch storage
  │    → Configured style agent applies fixes, runs clippy/tests/style review
  │
  └─ Generate Clean-fix Report

Post-clean-fix (manual):
  /style_fix_review → /merge_from_branch → /delete_a_worktree
```

## Evaluation State

The durable style-eval state is `~/rust/nate_style/.history/.pending/<project>.json`
while a project is waiting for review/fix. When style-fix writes `## Fix Summary`,
that scratch markdown is saved back into the same pending JSON and the JSON
stays in place until the `_style_fix` worktree is reviewed. History rows are
still appended to `~/rust/nate_style/.history/<project>.jsonl` for reporting.
The JSON records:

- `reviewable_unit_total`: how many style-guide units this run could check
- `checked_unit_count`: how many units were disposed by the agent or pre-filter
- `stop_reason`: `budget_reached` or `exhausted`
- `finding_count`: numbered findings currently in the evaluation markdown
- `scratch_exports`: the scratch markdown files freshly exported from pending JSON

The scratch files under `/private/tmp/claude` are phase handoffs, not source of
truth:

| Scratch file | Owner | Cleanup rule |
|--------------|-------|--------------|
| `style_eval_<project>_evaluation.md` | eval agent writes, pending JSON saves | deleted after the eval stage saves pending JSON |
| `style_eval_review_<project>_evaluation.md` | review agent edits a pending export | deleted after the review stage saves pending JSON |
| `style_fix_<project>_evaluation.md` | fix agent appends `## Fix Summary` | kept while the `_style_fix` worktree exists for `/style_fix_review` |

When a new eval run starts, stale eval/review scratch files are removed. A stale
`style_fix_<project>_evaluation.md` is removed only when no real
`~/rust/<project>_style_fix` git worktree owns it; active style-fix scratch files
are preserved so the next eval can avoid duplicating in-flight findings.

If a timing bug leaves a `_style_fix` worktree but pending JSON no longer
contains that worktree's `## Fix Summary`, `/style_fix_review` enters recovered
handoff mode. It writes a salvage markdown file with
`style_history.py recover-evaluation`, preferring the old scratch fix export
when present and otherwise reconstructing the review surface from the latest
history row with finding-like style guideline outcomes. The review must state
that this is an error recovery path and evaluate the current diff against the
recovered style files.
