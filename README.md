# Claude Commands

Custom /command prompts, hooks, and scripts for Claude Code.

I iterate on these almost every day to get them to work right for me - feel free to make suggestions, or create a PR!

## Key Components

### Style Enforcement
- **rust_style / local_style / succinct_style** - Load shared and repo-local style guides
- **style_eval / focused_eval / style_fix_review** - Evaluate code against the style guide and review fixes

### Clean-Fix Automation
- **clean_fix** - Pipeline that evaluates and fixes style across projects; runs on a launchd schedule (`scripts/clean-fix/`)

### Review & Planning
- **module_review** - Multi-agent module-structure evaluation
- **adhoc_review / phase_review / team_review** - Walk findings with the user, retrospect plan phases
- **ask_a_friend** - Get a second opinion from codex

### Release & CI
- **release** - Unified release pipeline (version bump, changelog, publish, GitHub release)
- **validate_and_push** - Local CI validation, push, and CI monitoring

### Worktree Management
- **make_a_worktree / worktree_delete** - Create and clean up git worktrees
- **pr_from_branch / merge_branch** - PRs and safe merges

### Hooks (`scripts/hooks/`)
- Auto cargo check on Rust edits, basedpyright on Python edits
- Random acknowledgements - ymmv

## Configuration
- `settings.json` - hooks, permissions, sandbox config

## Finally
There's more stuff you can check out. I use this as my "User" level config in ~/.claude - pick and choose what you adopt, since Claude makes it available in every project. Some of it is Rust-specific; swap that out to suit your use case.
