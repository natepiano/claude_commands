# Nightly Style Toggle

Toggle the nightly style evaluation on or off.

**Arguments**: `$ARGUMENTS` — `on`, `off`, or empty

## Instructions

The config file is `~/.claude/scripts/nightly/nightly-rust.conf`. The `[style_eval]` section has an `enabled=true` or `enabled=false` line.

1. Read `~/.claude/scripts/nightly/nightly-rust.conf`
2. Determine current state from the `enabled=` line under `[style_eval]`
3. Based on `$ARGUMENTS`:
   - **`on`**: Set `enabled=true`. Tell the user it's enabled.
   - **`off`**: Set `enabled=false`. Tell the user it's disabled.
   - **empty or anything else**: Show the current state and ask the user whether they want to turn it on or off.
4. When changing the value, use the Edit tool to update the line in-place.
