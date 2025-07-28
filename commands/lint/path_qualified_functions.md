# Lint Path-Qualified Functions

Search the codebase for direct function imports and replace them with module imports, then call functions with module qualification.

## What to look for:
- Direct function imports like `use crate::module::function_name`
- Direct function imports like `use super::module::function_name`
- Function calls without module qualification
- **EXCEPTION**: Ignore functions from `std::` (these are commonly imported directly)
- **EXCEPTION**: Ignore trait methods and associated functions

## How to fix:
1. Change `use crate::module::submodule::function_name` to `use crate::module::submodule`
2. Change `use super::module::function_name` to `use super::module`
3. Update function calls from `function_name()` to `submodule::function_name()`
4. For multiple functions from the same module, import the module once

## Examples:
```rust
// Before:
use crate::error::report_to_mcp_error;
use crate::brp_tools::{execute_brp_method, default_port};
use super::support::delete_log_files;

fn example() {
    let result = execute_brp_method(method, params, port).await?;
    let error = report_to_mcp_error(&err);
    delete_log_files(filter)?;
}

// After:
use crate::error;
use crate::brp_tools;
use super::support;

fn example() {
    let result = brp_tools::execute_brp_method(method, params, port).await?;
    let error = error::report_to_mcp_error(&err);
    support::delete_log_files(filter)?;
}
```

## Search command:

**IMPORTANT**: Due to a known issue with Claude Code's Bash tool, piped commands must be wrapped in `bash -c`. Direct pipes (|) will hang indefinitely.

### Find function imports (snake_case identifiers in use statements):
```bash
bash -c "rg '^use.*(crate::|super::).*::[a-z_][a-z0-9_]*;' --type rust"
```

This command finds use statements that import specific functions (snake_case identifiers) rather than modules or types.

### Why this approach:
- In Rust, functions follow snake_case naming convention
- Types (structs, enums, traits) follow PascalCase
- By looking for snake_case imports after `::`, we can identify function imports
- Module imports typically don't end with a snake_case identifier

### What to fix:
Focus on these patterns:
- `use crate::module::function_name;`
- `use super::module::function_name;`
- `use crate::module::{function1, function2, Type};` (keep Type, change functions to module import)

### Special cases:
- If a module has both functions and types imported, split them:
  - Keep type imports as-is: `use crate::module::MyType;`
  - Change function imports to module import: `use crate::module;`
- Constants (UPPER_CASE) can be imported directly or with module qualification (your choice)
- Ignore macro imports (they end with `!` when used)