## git
### commit
**NEVER** commit changes unless i ask you to

## rust

### variables should be used within a format string:

<incorrect>
```rust
details:   Some(format!("Element index: {}", index)),
```
</incorrect>

<correct>
```rust
details:   Some(format!("Element index: {index}")),
```
</correct>

### edition
- we are using rust edition 2024 now

### dead code
- **IMPORTANT** never add #[allow(dead_code)] to fix a warning. if there already is #[allow(dead_code)] in the code base, it's because I put it there so you can leave it alone. But you never add it.

### pub mod use
- never use pub mod, always use mod with pub use statements

<incorrect>
```rust
pub mod some_module;
```
</incorrect>

<correct>
```rust
mod some_module;
pub use some_module::{SomeOtherType, SomeType};
```
</correct>

### always use nextest
always use `cargo nextest run`

<incorrect>
```bash
cargo test
```
</incorrect>

<correct>
```bash
cargo nextest run
```
</correct>

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

### use $TMPDIR rather than /tmp
don't use /tmp directory when you need temporary file processing, use $TEMPDIR which will automatically clean up temporary files

## sandbox

### gh CLI must always run unsandboxed
- **ALWAYS** use `dangerouslyDisableSandbox: true` when running any `gh` command
- The sandbox network proxy breaks TLS certificate verification for `gh` (`x509: OSStatus -26276`)
- `excludedCommands` only bypasses the filesystem sandbox, not the network proxy
- Do NOT try `gh` in the sandbox first — it will always fail. Use `dangerouslyDisableSandbox` from the start.

## bash commands

- when we use types i never want to see inline module paths, use them at the top of the file and not inline in the code
- imports always go at the top of the file
- never consolidate rust imports - we want them one-per-line