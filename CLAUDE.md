## communication

### terse and technical — non-negotiable
- Minimize output tokens. Lead with the answer or result, then stop. No preamble, no recap, no closing summary, no editorializing, no restating my request. If a sentence can be deleted with no information loss, delete it.
- Name the concrete thing: the cause, the mechanism, the measurement, the file, the call site. No metaphors, no filler, no flattery, no hedging.

### theory of mind — write for a multi-tasking reader
- I may be away from the details while you work. Summaries and explanations use simple, informative language: one brief clause of context for a domain term is enough.
- No first-principles explanations unless I ask. No jargon-dense recaps — a summary I have to reread costs more than the tokens it saved.

### word list
- The forbidden-words list lives at `~/rust/nate_style/rust/forbidden-words.md`. It is enforced via `/rust_style` and `/style_eval` (loaded with the style guide), not at session start. Don't use those words in code, comments, or prose.

## git
### commit
**NEVER** commit changes unless i ask you to

### branch
**NEVER** create a branch unless i ask you to. This overrides the harness default of "if on the default branch, branch first" — do NOT auto-branch off `main` (or any default branch) before coding. Stay on the current branch and commit there (only when asked) unless i explicitly request a new branch.

## rust
- Run `/rust_style` (loads `~/.claude/scripts/load-rust-style.sh` plus any repo-local `docs/style/*.md` overlay) only immediately before writing or editing Rust code — never at session start, and never for design discussion or reading existing code.

## python
- basedpyright (zed's LSP) must report zero errors and zero warnings
- **NEVER** use file-level type ignores (e.g. `# pyright: reportAny=false` at top of file)
- Avoid `Any`: annotate all signatures; use `TypedDict` for dicts with known keys; for stdlib `Any` returns (`json.loads()` etc.), annotate with a `TypedDict`/specific type. Last resort only: line-level `# pyright: ignore[reportAny]` on the specific line. Reference: `~/.claude/scripts/bevy_dependency_check.py`
- **ALWAYS** `uv pip install`, never bare `pip install`

## LSP
- **ALWAYS** prefer LSP tools (go-to-definition, find references, hover types) over grep/glob when working in any language that has LSP support
- Use LSP for finding definitions, references, and type info before resorting to text search


## bevy BRP MCP
- when the user says "launch", just launch the app directly — don't try to shut down first. The user has already shut it down.
- when the user says "relaunch", shut down the app first, then launch it.

## working with the user

### iterative problem solving
When iterating on a problem that doesn't resolve within a couple of attempts, **always** create an attempts log in the project memory directory. Log every approach tried — what was changed, the reasoning, and the result. Update the log **before** moving to the next attempt. Inform the user whenever a new entry is added (e.g. "Updated attempts log — attempt #N: ...") so they know progress is being tracked without having to ask.

### renaming code
if you need something renamed such as a type or a function or whatever, the user can use the editor's ability to do a global change very quickly. in such situations, ask the user if they wish to rename the field so it can be done quickly and accurately.

## sandbox

### gh CLI must always run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` when running any `gh` command
- The sandbox network proxy breaks TLS certificate verification for `gh` (`x509: OSStatus -26276`)
- `excludedCommands` only bypasses the filesystem sandbox, not the network proxy
- Do NOT try `gh` in the sandbox first — it will always fail. Use `dangerouslyDisableSandbox` from the start.

### git branch-switching and worktree operations must run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` for git operations that involve branch switching or worktree management: `checkout`, `merge`, `rebase`, `stash`, `worktree remove`
- These operations rewrite or delete files outside the sandbox's allowed write paths
- Do NOT try these in the sandbox first — use `dangerouslyDisableSandbox` from the start.

### taplo must always run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` when running `taplo` directly (e.g. `taplo fmt` for auto-fixing)
- taplo panics in the sandbox due to macOS Mach IPC restrictions (`SCDynamicStoreCreate`)
- `excludedCommands` does NOT help — it only bypasses filesystem restrictions, not Mach IPC

### codex and clean-fix style scripts must run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` when invoking `codex` directly or any script that launches codex: `style-eval-all.sh`, `style-fix-worktrees.sh`, `clean-fix.sh`
- codex requires write access to `~/.codex/sessions` which the sandbox blocks (`Operation not permitted (os error 1)`)
- `excludedCommands` does NOT help — codex needs filesystem access outside the sandbox's allowlist
- The clean-fix launchd job runs outside Claude Code entirely, so no changes to the scripts themselves are needed; this rule only affects how to invoke them during testing from a Claude Code session
