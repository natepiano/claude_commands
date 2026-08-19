---
description: Load the Rust style guide before writing Rust code.
---

Load the Rust style guide before writing Rust code.

Run this exact command with Bash:
```bash
zsh ~/.claude/scripts/rust_style/load-rust-style.sh --scope edit
```

`--scope edit` drops rules tagged `scope: review` — the ones about where code lives (module splitting, submodule naming, where impls go, Cargo.toml deps). They are not actionable while writing a function, and the review commands still load them. If you are restructuring modules rather than writing code, re-run without `--scope` for the full guide.

**IMPORTANT — reading the full output:**
The style guide is ~24KB and will be persisted to a file by the Bash tool. The summary line prints first (file/line counts), followed by all rule content. If the Bash output says "Full output saved to: <path>", you **MUST** use the Read tool on that persisted output file to load the complete style guide. Do NOT skip this step — a 2KB preview is not the full guide.

After loading, confirm the summary line (first line of output). Then proceed with the user's request.
