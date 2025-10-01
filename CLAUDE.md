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
