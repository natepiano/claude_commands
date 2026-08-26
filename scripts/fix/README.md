# Clean-fix Rust Pipeline

Automated style evaluation, evaluation review, and style-fix pipeline for opt-in Rust projects under `~/rust/`.

Runs every 10 minutes via launchd.

## Files

### Orchestration

| File | Purpose |
|------|---------|
| `fix.sh` | Main entry point. Accepts an optional project filter or the literal `run_once`, which forces one evaluation + review + fix pass across all projects. `run_once` overrides only the three stage switches, so normal per-project safety and eligibility skips still apply. Emits a clean-fix log that `/fix report` can render on demand. |
| `fix-usage.sh` | Emits the no-argument `/fix` usage screen as preformatted Markdown with fixed-width, wrapped text blocks. `--json` exposes the same usage, agent, and project data for validation/tools. |
| `fix.conf` | Pipeline configuration. One opt-in allowlist, `[projects]`, plus the optional `[active_checkout]` redirect map (point a project's eval/fix at a worktree while keeping its identity/history), style quotas, timeouts, and project environment. No agent settings live here. No deny list — nothing runs unless listed. |
| `project_add.py` | Adds a project to `[projects]` only. Accepts checkout names, paths under `~/rust`, absolute paths, and `Cargo.toml` paths; workspace members are written as workspace-relative entries so their identity/history key stays the member directory name. |
| `project_rename.py` | Renames a clean-fix project key after a checkout/member path changes. Updates `[projects]`, `[active_checkout]`, and `[project_env]` entries and migrates history JSONL, pending JSON/lock, failure logs, and `.fix-project` markers. Refuses collisions instead of merging histories. |
| `agent-assignments.conf` | Clean-fix stage enablement. `[style_eval]`, `[style_eval_review]`, and `[style_fix]` each own only `enabled=`; family, agent, and effort assignments live under `[fix.<family>]` in `~/.claude/config/agents.conf`. |
| `agent_assignments.sh` | Clean-fix Bash helper for loading the three stage switches and resolving family, agent, and effort for `style_eval`, `style_eval_review`, and `style_fix` through `agents_resolve fix.<stage>`. |
| `com.natemccoy.style-fix.plist` | launchd plist — triggers the pipeline every 10 minutes (no idle gate). |
| `setup.sh` | Idempotent setup script — installs the one launchd agent, creates runtime directories, and retires the old pre-split agent. |

### Style Evaluation

| File | Purpose |
|------|---------|
| `style-eval-all.sh` | Runs `/style_eval` on every `[projects]` entry in parallel using the `[style_eval]` assignment. Stores pending evaluation markdown in `.history/.pending/<project>.json`. Skips projects with pending findings or a real `_style_fix` worktree so pending JSON cannot be replaced while fixes are awaiting review. |
| `candidate_generators.py` | Deterministic candidate enumeration for style-eval units. A guideline's `candidates:` frontmatter names a generator kind (regex / toml / Rust-source parse); `next-unit` hands the agent the enumerated sites as a closed list, `record-unit` refuses records that don't disposition every candidate, and zero-candidate units record free like pre_filter skips. Design + audit: `docs/candidate-enumeration-design.md`. Debug via `style_history.py enumerate-candidates`. |
| `rg-shim.sh` | Retired timeout shim for `ripgrep`. Kept for reference, but not activated on PATH. See **Reliability guards** below. |

### Style-Fix Worktrees

| File | Purpose |
|------|---------|
| `style-fix-worktrees.sh` | For each project with pending findings: creates a `_style_fix` worktree, exports evaluation markdown to a scratch file under `/private/tmp/claude`, launches the `[style_fix]` agent to apply fixes (cargo mend, clippy, tests, style review), then launches a second run of the **same** agent to verify the applied fix against the Fix Summary (correcting mistakes and updating the summary), saves the Fix Summary back into pending JSON, and keeps `EVALUATION.md` out of the worktree. Other linked worktrees are allowed; the source tree the project resolves to must be clean, with the check narrowed to the member subpath for a workspace member. Can target a single project by name. |

### Flowchart Diagram

| File | Purpose |
|------|---------|
| `fix-style-flow.dot` | Graphviz source for the pipeline flowchart. Defines nodes, edges, positions, and cluster membership. |
| `render-flow.py` | Renders the dot file to SVG. Parses cluster definitions from the dot file, runs `neato -n2`, then injects dashed cluster borders and rewrites the SVG viewBox. |
| `fix-style-flow.svg` | Generated output — do not edit by hand. |

## Reliability guards (the rg-hang)

On **2026-06-02** a nightly run wedged for 12+ hours and produced no style-fix
worktrees. Two style-eval agents had issued pipelines like
`rg PATTERN -g '*.rs' | rg -v X | head` where the first `rg` has glob filters
but **no path argument**. With no path, `rg` searches stdin whenever stdin is
not a terminal; claude's Bash tool hands each command an open stdin pipe that
never delivers data and never closes, so that first `rg` blocked on `read()`
forever. The eval stage's serial `wait` then stalled, the parent `fix.sh`
stayed alive, and the launchd trigger's `pgrep` concurrency guard suppressed
every subsequent run all night.

Two layers now prevent a recurrence:

1. **Eval-stage watchdog** (`style-eval-all.sh`). The per-agent wait is
   `wait_or_timeout`, which kills the agent's whole process tree (subshell +
   `claude`/`codex` + any `rg`/`zsh` grandchildren) after `agent_timeout_secs`
   (from `[style_fix]` in `fix.conf`, default 2h). Containment: one hung
   agent can no longer stall the pipeline.

2. **Retired `rg` timeout shim** (`rg-shim.sh`) — this used to be activated by
   a symlink at `~/.claude/scripts/rg` plus a shell PATH entry that put
   `~/.claude/scripts` before the real `rg`. That global PATH shadowing caused
   unrelated command-resolution risk, so the symlink and `.zshrc` PATH export
   were removed. The file remains as incident context only.

## Generating the flowchart

Prerequisites: `graphviz` (`brew install graphviz`), Python 3.

```bash
cd ~/.claude/scripts/fix
python3 render-flow.py
```

The render script:
1. Parses cluster membership, labels, and colors from the `.dot` file
2. Runs `neato -n2` to produce an SVG with exact node positioning
3. Injects dashed border rectangles and labels for each `subgraph cluster_*` block
4. Aligns the tops of Phase 1/2/3 clusters
5. Rewrites the SVG `viewBox` to fit all content with uniform padding

To modify the diagram, edit `fix-style-flow.dot` and re-run `python3 render-flow.py`. See the layout guide comment at the top of the dot file for details on adding/removing nodes and phases.

## Pipeline flow

```
style-fix job (every 10 min, no idle gate) — fix.sh [PROJECT | run_once]
  │
  ├─ STYLE_EVAL_ENABLED? (run_once forces yes)
  │    ├─ no → log SKIP and continue to STYLE_REVIEW_ENABLED
  │    └─ yes → Phase 1: Style Evaluation (per project, parallel)
  │         Load style guide → survey code → carry forward valid findings
  │         → skip any project with pending findings or a _style_fix worktree
  │         → find new violations → store pending evaluation markdown
  │
  ├─ STYLE_REVIEW_ENABLED? (run_once forces yes)
  │    ├─ no → log SKIP and continue to STYLE_FIX_ENABLED
  │    └─ yes → Phase 2: Style Evaluation Review (per project, parallel)
  │         Review pending evaluation markdown with the configured review agent
  │         → save reviewed markdown back into pending JSON
  │
  ├─ STYLE_FIX_ENABLED? (run_once forces yes)
  │    ├─ no → log SKIP and continue to the report activity gate
  │    └─ yes → Phase 3: Style-Fix Worktrees (per project, parallel)
  │         Create _style_fix worktree (other linked worktrees allowed if the resolved
  │         source tree is clean; workspace-member checks use the member subpath)
  │         → export pending evaluation markdown to scratch storage
  │         → Pass 1 (apply): configured style agent applies fixes, runs clippy/tests/style review, writes Fix Summary
  │         → Pass 2 (verify): same agent re-checks the fix vs the Fix Summary, corrects mistakes, updates the Fix Summary
  │         → build gate (cargo check) covers both passes; finalize into pending JSON
  │
  └─ Run log has project result lines?
       ├─ yes → render the clean-fix report
       └─ no → log "Report skipped (no per-project activity this run)"

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
