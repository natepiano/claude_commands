# Claude Commands

Custom /command prompres for Claude Code.

Try it out and let me know if you like it or have suggestions to change it.

I iterate on these almost every day to get them to work right for me - feel free to make Suggestions, or create a PR!


## Key Components

### Review Commands
- **code_review** - Review code changes (commits, tags, diffs)
- **design_review** - Evaluate implementation plans
- **alignment_review** - Verify implementation matches plans

there is some rust specific stuff in the reviews so if you try this out you may want to change some of that to suit your specific use case as I haven't yet devised how to swap out "language support". It's totally doable I just haven't gotten around to it yet.
### Development Tools
- **make_a_plan** - Generate implementation plans
- **commit_prep** - Prepare git commits
- **clippy** - Run Rust linting

### Automation
- **post-tool-use-cargo-check.sh** - Auto-runs cargo check on Rust edits
- **post-tool-use-random-ack.sh** - Random acknowledgements - ymmv

### Worktree Management
- **make_a_worktree** - Create git worktrees
- **delete_a_worktree** - Clean it up
- **pr_from_worktree** - Create PRs from it

## Configuration
- `settings.json` - my hook configurations and preferences

## Finally
There's more stuff you can check out. Try it out in a project branch. I use this as my "User" level with Claude - in the ~/.claude directory and probably if you like any of it you should pick and choose what you push to your user level - as Claude makes that available in every project.
