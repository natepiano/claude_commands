# Clean-fix Rust Pipeline

Automated clean-fix clean-build, style evaluation, and style-fix pipeline for all Rust projects under `~/rust/`.

Runs daily at **4:00 AM** via launchd.

## Files

### Orchestration

| File | Purpose |
|------|---------|
| `clean-fix.sh` | Main entry point. Takes a scope: `clean` (settings back-populate + clean/build/mend + warmup), `style` (eval + review + fix), or `all` (default, both). Emits a clean-fix log that `/clean_fix report` can render on demand. |
| `clean-fix.conf` | Pipeline configuration. Two opt-in allowlists: `[build]` (clean/build/mend) and `[targets]` (style eval/review/fix), plus style quotas, timeouts, project env, and warmup targets. No agent settings live here. No deny list — nothing runs unless listed. |
| `agent-assignments.conf` | Clean-fix stage-to-agent mapping. `[style_eval]`, `[style_eval_review]`, and `[style_fix]` each own `enabled=`, `agent=`, and optional per-stage `model=`/`effort=` overrides. Empty overrides resolve through `~/.claude/config/agents.conf`. |
| `agent_assignments.sh` | Clean-fix Bash helper for loading stage assignments. It delegates model/effort defaults and allowlist validation to `scripts/agents_config.sh`. |
| `com.natemccoy.style-fix.plist` | launchd plist — runs the style scope every 10 minutes (no idle gate). |
| `com.natemccoy.cargo-clean.plist` | launchd plist — runs the clean scope nightly at 4:00 AM (idle-gated). |
| `setup.sh` | Idempotent setup script — installs both launchd agents, creates runtime directories, retires the old pre-split agent. |

### Style Evaluation

| File | Purpose |
|------|---------|
| `style-eval-all.sh` | Runs `/style_eval` on every `[targets]` entry in parallel using the `[style_eval]` assignment. Stores pending evaluation markdown in `.history/.pending/<project>.json`. Skips projects with pending findings or a real `_style_fix` worktree so pending JSON cannot be replaced while fixes are awaiting review. |
| `candidate_generators.py` | Deterministic candidate enumeration for style-eval units. A guideline's `candidates:` frontmatter names a generator kind (regex / toml / Rust-source parse); `next-unit` hands the agent the enumerated sites as a closed list, `record-unit` refuses records that don't disposition every candidate, and zero-candidate units record free like pre_filter skips. Design + audit: `docs/candidate-enumeration-design.md`. Debug via `style_history.py enumerate-candidates`. |
| `rg-shim.sh` | Timeout shim for `ripgrep`. Bounds every non-interactive `rg` so a path-less search blocked on stdin can't hang forever. See **Reliability guards** below. |

### Style-Fix Worktrees

| File | Purpose |
|------|---------|
| `style-fix-worktrees.sh` | For each project with pending findings: creates a `_style_fix` worktree, exports evaluation markdown to a scratch file under `/private/tmp/claude`, launches the `[style_fix]` agent to apply fixes (cargo mend, clippy, tests, style review), then launches a second run of the **same** agent to verify the applied fix against the Fix Summary (correcting mistakes and updating the summary), saves the Fix Summary back into pending JSON, and keeps `EVALUATION.md` out of the worktree. Other linked worktrees are allowed; the primary checkout still must be clean. Can target a single project by name. |

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
cargo-clean job (nightly 4:00 AM, idle-gated) — clean-fix.sh clean
  │
  └─ Clean + Rebuild (per project, sequential)
       cargo clean → cargo build → cargo mend → warmup

style-fix job (every 10 min, no idle gate) — clean-fix.sh style
  │
  ├─ Phase 1: Style Evaluation (per project, parallel)
  │    Load style guide → survey code → carry forward valid findings
  │    → skip any project with pending findings or a _style_fix worktree
  │    → find new violations → store pending evaluation markdown
  │
  ├─ Phase 2: Style Evaluation Review (per project, parallel)
  │    Review pending evaluation markdown with the configured review agent
  │    → save reviewed markdown back into pending JSON
  │
  ├─ Phase 3: Style-Fix Worktrees (per project, parallel)
  │    Create _style_fix worktree (other linked worktrees allowed if primary is clean)
  │    → export pending evaluation markdown to scratch storage
  │    → Pass 1 (apply): configured style agent applies fixes, runs clippy/tests/style review, writes Fix Summary
  │    → Pass 2 (verify): same agent re-checks the fix vs the Fix Summary, corrects mistakes, updates the Fix Summary
  │    → build gate (cargo check) covers both passes; finalize into pending JSON
  │
  └─ Write clean-fix log for on-demand reports

Post-clean-fix (manual):
  /style_fix_review → /merge_branch → /worktree_delete
```

## Evaluation State

The durable style-eval state is `~/rust/nate_style/.history/.pending/<project>.json`
while a project is waiting for review/fix. When style-fix writes `## Fix Summary`,
that scratch markdown is saved back into the same pending JSON and the JSON
stays in place until the `_style_fix` worktree is reviewed. History rows are
appended to `~/rust/nate_style/.history/<project>.jsonl` for local reporting.
The `.history/` directory is local operational state and is not committed.
The JSON records:

- `reviewable_unit_total`: how many style-guide units this run could check
- `checked_unit_count`: how many units were disposed by the agent or pre-filter
- `stop_reason`: `budget_reached`, `quota_reached`, or `exhausted` — the only
  values that allow a run to be finalized into history. An empty stop_reason
  means the run is in progress (or was abandoned); `start-run` resumes it and
  `finalize-no-findings` refuses it (exit 3).
- `finding_count`: numbered findings currently in the evaluation markdown
- `scratch_exports`: the scratch markdown files freshly exported from pending JSON

The scratch files under `/private/tmp/claude` are phase handoffs, not source of
truth:

| Scratch file | Owner | Cleanup rule |
|--------------|-------|--------------|
| `style_eval_<project>_evaluation.md` | eval agent writes, pending JSON saves | deleted after the eval stage saves pending JSON |
| `style_eval_review_<project>_evaluation.md` | review agent edits a pending export | deleted after the review stage saves pending JSON |
| `style_fix_<project>_evaluation.md` | fix agent appends `## Fix Summary`; verify agent updates it and appends `## Fix Verification` | kept while the `_style_fix` worktree exists for `/style_fix_review` |

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
