## writing style
- **NEVER** use the word "honest" (or "honestly", "more honest", "more honestly", "to be honest", "in all honesty", "the honest X", "an honest Y", etc.) in output to the user, in code, in commit messages, in design docs, or anywhere else. **This is a permanent, non-negotiable, repeat-offense rule.** It has been violated across multiple sessions in multiple projects despite explicit memories and CLAUDE.md entries banning it. The user's word: "forever."
  - **Reminder counter: 10.** Increment this number every time the user has to ask again. Each increment is evidence that the pre-send check below failed; treat a rising counter as a serious defect, not a casual tally.
  - Drop it entirely. Do not substitute "truthfully", "frankly", "candidly", or any near-synonym that does the same rhetorical work.
  - Applying the word to a *thing* (an API, a design, a list, an abstraction) is just as banned as applying it to a claim. "The abstraction is honest", "an honest list", "the design is more honest" are all violations.
  - The word smuggles in a virtue claim ("look how truthful this is") and implies the alternative is dishonest. Both are wrong moves.
  - **Pre-send check:** before sending any reply, scan the draft for the substring `honest`. If present, identify which of {direct, explicit, one-to-one, single-source-of-truth, simple, accurate} you actually mean and substitute the precise word. If none of those fit, the sentence is not making a real claim — delete it.
  - When the impulse appears mid-draft, delete the whole sentence and rewrite from scratch. Do not try to surgically swap one word; the underlying intent was almost always self-praise that needs to be removed wholesale.
- **NEVER** use the word "shape" (or "shaped", "shapes", "reshape", "reshaping", etc.) in output to the user, in code, or in identifiers. It is nails on a chalkboard. Name the actual concrete artifact instead:
  - If it is a **function**, say "function" (and name it).
  - If it is a **pattern** (in the design / convention sense), say "pattern".
  - If it is a **struct** or **enum**, say "struct" or "enum" (and name it).
  - If it is a **function with specific arguments**, say "function signature" and write the signature.
  - If it is a **trait**, say "trait".
  - If it is a **type**, say "type" and name the type.
  Never substitute "form" or "structure" — same hedge with different letters. State what the thing actually is.
- **NEVER** use the word "carve" (or "carving", "carved", "carve-out", "carve out", etc.) in output to the user, in code, or in identifiers. It is metaphor filler that hides what the change actually does. Name the concrete operation instead:
  - **Extract** a type / module / subsystem from a larger one (when the body of code is moved into a new home).
  - **Split** A into B and C (when one thing becomes two).
  - **Move** field X from A to B (when a single field relocates).
  - **Refactor** A into B (when behavior is preserved but the structure changes).
  - **Introduce** type T (when the change is purely additive).
  Pick the verb that names what's happening. Never substitute "sculpt", "tease apart", or other artisanal-craft metaphors — same hedge.

## git
### commit
**NEVER** commit changes unless i ask you to

## rust
- Before writing Rust code, run `/nate_style`, which uses `~/.claude/scripts/load-rust-style.sh` to load the shared style guide plus any repo-local `docs/style/*.md` overlay

## python

<python_requirements>

### type safety with basedpyright
- our editor, zed, uses basedpyright as the lsp for python
- **CRITICAL**: ALL errors and warnings must be fixed - zero tolerance
- **NEVER** use file-level type ignores (e.g., `# pyright: reportAny=false` at top of file)
- **ALWAYS** provide explicit type definitions to avoid `Any` types

### avoiding Any types
1. **First priority**: Create proper type definitions
   - Use `TypedDict` for dictionary structures with known keys
   - Use specific types instead of generic containers
   - Add type annotations to all function signatures

2. **Standard library Any types**: When stdlib functions return `Any` (e.g., `json.loads()`, `urllib.request.urlopen()`):
   - Create `TypedDict` definitions for expected structures
   - Cast to specific types with annotations
   - Use type assertions where the type is known at runtime

3. **Last resort only**: If `Any` is genuinely unavoidable from stdlib, use line-level suppression:
   ```python
   data: MyType = json.loads(response.read())  # pyright: ignore[reportAny]
   ```
   - Use `# pyright: ignore[reportAny]` on the specific line only
   - Only use after exhausting all other type-safe approaches
   - Each suppression should be justified by unavoidable stdlib limitations

### example from bevy_dependency_check.py
See `~/.claude/scripts/bevy_dependency_check.py` for reference implementation with:
- `TypedDict` definitions for API responses
- Line-level suppressions only where stdlib returns `Any`
- Zero errors, zero warnings achieved

</python_requirements>

### package management
- **ALWAYS** use `uv` instead of `pip` for installing Python packages

<incorrect>
```bash
pip install some-package
```
</incorrect>

<correct>
```bash
uv pip install some-package
```
</correct>

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

## working with files

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

### codex and nightly style scripts must run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` when invoking `codex` directly or any script that launches codex: `style-eval-all.sh`, `style-fix-worktrees.sh`, `nightly-rust-clean-build.sh`
- codex requires write access to `~/.codex/sessions` which the sandbox blocks (`Operation not permitted (os error 1)`)
- `excludedCommands` does NOT help — codex needs filesystem access outside the sandbox's allowlist
- The nightly launchd job runs outside Claude Code entirely, so no changes to the scripts themselves are needed; this rule only affects how to invoke them during testing from a Claude Code session
