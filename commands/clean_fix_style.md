# Clean-fix Style Toggle

Set the clean-fix style agent mode.

**Arguments**: `$ARGUMENTS` — `off`, `claude`, `codex`, `on`, or empty

## Instructions

The config file is `~/.claude/scripts/clean-fix/clean-fix.conf`. The `[style_eval]` section has a `mode=off`, `mode=claude`, or `mode=codex` line.

1. Read `~/.claude/scripts/clean-fix/clean-fix.conf`
2. Determine current state from the `mode=` line under `[style_eval]`
3. Based on `$ARGUMENTS`:
   - **`claude`**: Set `mode=claude`.
   - **`codex`**: Set `mode=codex`.
   - **`off`**: Set `mode=off`.
   - **`on`**: Set `mode=claude`.
   - **empty or anything else**: Show the current mode.
4. When changing the value, use the Edit tool to update the line in-place.
