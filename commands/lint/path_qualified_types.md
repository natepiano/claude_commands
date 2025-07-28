# Lint Path-Qualified Types

Search the codebase for uses of types (structs, enums, traits) that have path qualifiers and replace them with proper use statements at the top of the file.

## What to look for:
- Path-qualified types like `my_module::MyType`, `crate::my_module::MySubmodule::MyType`, `super::MyType`
- Any type name (PascalCase) preceded by a path separator `::`
- Common patterns: `crate::`, `super::`, `self::`, or module paths
- **EXCEPTION**: Ignore paths that begin with `std::` (these are infrequent and typically short)

## How to fix:
1. Add a `use` statement at the top of the file
2. For peer modules (siblings in the same parent), use `use super::peer_module::MyType`
3. For non-peer modules, use `use crate::module::submodule::MyType`
4. Replace the path-qualified usage with just the type name

## Examples:
```rust
// Before:
fn example() {
    let x: my_module::MyStruct = my_module::MyStruct::new();
    let y: crate::other::SomeEnum = crate::other::SomeEnum::Variant;
    let map: std::collections::HashMap<String, i32> = std::collections::HashMap::new(); // OK - std:: paths are allowed
}

// After:
use super::my_module::MyStruct;  // if my_module is a peer
use crate::other::SomeEnum;       // if other is not a peer

fn example() {
    let x: MyStruct = MyStruct::new();
    let y: SomeEnum = SomeEnum::Variant;
    let map: std::collections::HashMap<String, i32> = std::collections::HashMap::new(); // std:: paths remain unchanged
}
```

## Search command:

**IMPORTANT**: Due to a known issue with Claude Code's Bash tool, piped commands must be wrapped in `bash -c`. Direct pipes (|) will hang indefinitely.

### Find all crate:: and super:: paths that violate the lint rule:
```bash
bash -c "rg '(crate::|super::)' --type rust | grep -v ':use '"
```

This command:
1. Uses `rg` to find all instances of `crate::` or `super::` in Rust files
2. Pipes to `grep -v ':use '` to exclude use statements (which are allowed)
3. The pattern `:use ` (with colon prefix) accounts for ripgrep's filename:line format

### Why this approach:
- Ripgrep includes the filename and line number prefix (e.g., `file.rs:42:use crate::...`)
- We exclude lines containing `:use ` to filter out legitimate use statements
- Serde attributes like `deserialize_with = "crate::tool::deserialize_port"` and macro-generated code will appear in results but are legitimate exceptions
- Function calls like `crate::module::function()` are allowed per the rules and can be ignored

### What to fix:
Focus on these patterns in the results:
- Type aliases: `type CallInfoData = crate::response::LocalCallInfo`
- Impl blocks: `impl From<crate::brp_tools::FormatCorrection> for ExtractedValue`
- Function return types: `-> crate::error::Result<T>`
- Type parameters: `Vec<crate::brp_tools::FormatCorrection>`

These are the actual violations that need to be converted to use statements.
