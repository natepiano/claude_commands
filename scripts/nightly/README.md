# Nightly Rust Pipeline

Automated nightly clean-build, style evaluation, and style-fix pipeline for all Rust projects under `~/rust/`.

Runs daily at **4:00 AM** via launchd.

## Files

### Orchestration

| File | Purpose |
|------|---------|
| `nightly-rust-clean-build.sh` | Main entry point. For each eligible project: clean, build, clippy. Then runs style eval + style-fix if enabled. Generates the nightly report at the end. |
| `nightly-rust.conf` | Configuration: excluded projects, style eval settings (`enabled`, `max_new_findings`), warmup targets. |
| `com.natemccoy.nightly-rust-clean-build.plist` | launchd plist — schedules the nightly job at 4:00 AM. |
| `setup.sh` | Idempotent setup script — installs the launchd agent, creates runtime directories. |

### Style Evaluation

| File | Purpose |
|------|---------|
| `style-eval-all.sh` | Runs `/style_eval` on all eligible projects in parallel. Produces/updates `EVALUATION.md` in each project root. Checks for an existing `_style_fix` worktree and excludes those findings from re-evaluation. |

### Style-Fix Worktrees

| File | Purpose |
|------|---------|
| `style-fix-worktrees.sh` | For each project with `EVALUATION.md` findings: creates a `_style_fix` worktree, moves `EVALUATION.md` into it, launches Claude to apply fixes (cargo mend, clippy, tests, style review). Can target a single project by name. |

### Warmup

| File | Purpose |
|------|---------|
| `nightly-warmup.sh` | Warms up Bevy apps by launching them briefly, polling the BRP endpoint until ready, then shutting them down. Configured via the `[warmup]` section in `nightly-rust.conf`. |

### Flowchart Diagram

| File | Purpose |
|------|---------|
| `nightly-style-flow.dot` | Graphviz source for the pipeline flowchart. Defines nodes, edges, positions, and cluster membership. |
| `render-flow.py` | Renders the dot file to SVG. Parses cluster definitions from the dot file, runs `neato -n2`, then injects dashed cluster borders and rewrites the SVG viewBox. |
| `nightly-style-flow.svg` | Generated output — do not edit by hand. |

## Generating the flowchart

Prerequisites: `graphviz` (`brew install graphviz`), Python 3.

```bash
cd ~/.claude/scripts/nightly
python3 render-flow.py
```

The render script:
1. Parses cluster membership, labels, and colors from the `.dot` file
2. Runs `neato -n2` to produce an SVG with exact node positioning
3. Injects dashed border rectangles and labels for each `subgraph cluster_*` block
4. Aligns the tops of Phase 1/2/3 clusters
5. Rewrites the SVG `viewBox` to fit all content with uniform padding

To modify the diagram, edit `nightly-style-flow.dot` and re-run `python3 render-flow.py`. See the layout guide comment at the top of the dot file for details on adding/removing nodes and phases.

## Pipeline flow

```
Nightly Build (4:00 AM)
  │
  ├─ Phase 1: Clean + Rebuild (per project, sequential)
  │    cargo clean → cargo build → cargo clippy → warmup
  │
  ├─ Phase 2: Style Evaluation (per project, parallel)
  │    Load style guide → survey code → carry forward valid findings
  │    → check for _style_fix worktree findings (exclude them)
  │    → find new violations → write EVALUATION.md
  │
  ├─ Phase 3: Style-Fix Worktrees (per project, parallel)
  │    Create worktree → mv EVALUATION.md into it
  │    → Claude applies fixes, runs clippy/tests/style review
  │
  └─ Generate Nightly Report

Post-nightly (manual):
  /style_fix_review → /merge_from_branch → /delete_a_worktree
```
