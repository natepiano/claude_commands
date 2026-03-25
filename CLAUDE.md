## git
### commit
**NEVER** commit changes unless i ask you to

## rust
- Before writing Rust code, run `/rust-style` to load the full style guide from `~/rust/hanadocs/topics/developer/rust/`

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

### git branch-switching operations must run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` for git operations that involve branch switching: `checkout`, `merge`, `rebase`, `stash`
- These operations rewrite files under `.claude/` which the sandbox write restrictions block
- Do NOT try these in the sandbox first — use `dangerouslyDisableSandbox` from the start.

