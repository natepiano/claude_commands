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

## working with the user

### renaming code
if you need something renamed such as a type or a function or whatever, the user can use the editor's ability to do a global change very quickly. in such situations, ask the user if they wish to rename the field so it can be done quickly and accurately.

## working with files

### use $TMPDIR rather than /tmp
don't use /tmp directory when you need temporary file processing, use $TEMPDIR which will automatically clean up temporary files

## bash commands

### never use cat with heredoc in combined commands
**CRITICAL**: NEVER use `cat > file << 'EOF'` patterns combined with other commands in a single Bash call.
This breaks up command execution requiring permission gates.

<incorrect>
```bash
cat > /tmp/test.rs << 'EOF'
fn main() {}
EOF
rustc /tmp/test.rs -o /tmp/test && /tmp/test
```
</incorrect>

<correct>
```bash
# Use Write tool for file creation, then separate Bash command for compilation
# Or better: avoid creating test files altogether during reviews - just analyze the code logic
```
</correct>
- when we use types i never want to see inline module paths, use them at the top of the file and not inline in the code