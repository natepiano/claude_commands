## communication

### word list
- The forbidden-words list lives at `~/rust/nate_style/rust/forbidden-words.md`. It is enforced via `/rust_style` and `/style_eval` (loaded with the style guide), not at session start. Don't use those words in code, comments, or prose.

## decision criteria
Applies to every session when coding and reviewing code. `/plan:delegate` imports the same file, where it defines `<DecisionEconomy/>`.

@~/.claude/docs/decision_criteria.md

## python
- basedpyright (zed's LSP) must report zero errors and zero warnings
- **NEVER** use file-level type ignores (e.g. `# pyright: reportAny=false` at top of file)
- Avoid `Any`: annotate all signatures; use `TypedDict` for dicts with known keys; for stdlib `Any` returns (`json.loads()` etc.), annotate with a `TypedDict`/specific type. Last resort only: line-level `# pyright: ignore[reportAny]` on the specific line. Reference: `~/.claude/scripts/bevy_dependency_check.py`
- **ALWAYS** `uv pip install`, never bare `pip install`

## LSP
- **ALWAYS** prefer LSP tools (go-to-definition, find references, hover types) over grep/glob when working in any language that has LSP support
- Use LSP for finding definitions, references, and type info before resorting to text search

## running long commands (builds, tests, pushes)
- **ALWAYS background long-running commands** with `run_in_background: true`, then **end my turn** — do nothing, or do other unrelated work. The harness sends a `<task-notification>` when the command finishes and re-invokes me with the result. That notification IS the wait mechanism. Don't replace it.
- **NEVER poll a backgrounded command.** No `sleep`/`grep`/`tail` loop, no `for i in $(seq …); do sleep 5; done`, no repeated reads of the output file to detect completion. If I find myself writing a wait loop, that's the bug — delete it and just wait for the notification. Foreground `sleep` is sandbox-blocked anyway.
- **Do NOT foreground-block a long command** (a plain Bash call with a big `timeout`) as the way to "wait" for it. Background it and yield the turn instead. Foreground is for fast commands (seconds) whose output I need inline to proceed.
- After the `<task-notification>` arrives, read the output file once to get the result. Capturing exit code: pipelines mask it — `cargo … | tail` reports `tail`'s exit (always 0), so a real failure looks like success. Use `set -o pipefail`, check `${PIPESTATUS[0]}`, or don't pipe the command whose exit code matters.

## bevy BRP MCP
- when the user says "launch", just launch the app directly — don't try to shut down first. The user has already shut it down.
- when the user says "relaunch", shut down the app first, then launch it.

## working with the user

### iterative problem solving
When iterating on a problem that doesn't resolve within a couple of attempts, **always** create an attempts log in the project memory directory. Log every approach tried — what was changed, the reasoning, and the result. Update the log **before** moving to the next attempt. Inform the user whenever a new entry is added (e.g. "Updated attempts log — attempt #N: ...") so they know progress is being tracked without having to ask.

### renaming code
if you need something renamed such as a type or a function or whatever, the user can use the editor's ability to do a global change very quickly. in such situations, ask the user if they wish to rename the field so it can be done quickly and accurately.

## sandbox

### commands that must run unsandboxed
**ALWAYS** pass `dangerouslyDisableSandbox: true` from the start for every case below. Do NOT try sandboxed first — they always fail. `excludedCommands` never helps: it decides whether an *unsandboxed* run needs approval, not whether a command runs sandboxed. No other setting fixes these.

- **`gh` — any command**: the sandbox network proxy breaks TLS certificate verification (`x509: OSStatus -26276`)
- **git branch-switching and worktree operations** — `checkout`, `merge`, `rebase`, `stash`, `worktree remove`: they rewrite or delete files outside the sandbox's allowed write paths
- **`taplo`**, e.g. `taplo fmt` for auto-fixing: panics under macOS Mach IPC restrictions (`SCDynamicStoreCreate`)
- **`codex`, and any script that launches it** — `style-eval-all.sh`, `style-fix-worktrees.sh`, `clean-fix.sh`: codex needs write access to `~/.codex/sessions`, which the sandbox blocks (`Operation not permitted (os error 1)`). The clean-fix launchd job runs outside Claude Code entirely, so the scripts themselves need no changes — this rule only affects invoking them from a session.
- **builds of crates whose build scripts call Swift Package Manager** — `apple-cf`, `apple-metal`, `screencapturekit`, generally anything wrapping a macOS framework: SwiftPM sandboxes its own manifest compile with `sandbox-exec`, and **macOS sandboxes cannot nest**, so that call fails at `sandbox_apply` and the build script panics.
  - The signature is `sandbox-exec: sandbox_apply: Operation not permitted`, usually buried under a panic that names Swift and never names the sandbox — it reads like a broken dependency. **Treat it as a sandbox failure, never a code defect**: re-run unsandboxed before concluding anything, and never report it as a finding, pin or patch the dependency, or write it into a delegate prompt as a known pre-existing failure.
  - It is intermittent because build-script output caches: once built unsandboxed it stays green until a dependency bump, a toolchain change, or the nightly `cargo clean` makes the scripts re-run.

### editing protected files under `~/.claude` — do NOT use the sandbox override
`~/.claude` is writable, but specific paths are carved back out: `CLAUDE.md`, `settings.json`, and the `skills`/`hooks`/`commands`/`agents` directories. Shell writes to them (`mv`, `>`, `sed -i`) fail with `Operation not permitted`.

- **Use the Edit/Write tools instead** — they route through the permission gate rather than the filesystem sandbox, so the edit lands with no override needed
- This is a deliberate guard on config files, not a proxy or IPC limitation — `dangerouslyDisableSandbox` is the wrong fix here even though it would work
