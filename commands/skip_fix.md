# Skip Style Fix

Skip or re-enable a target for the **style eval/fix** pass, by commenting its
line in the `[targets]` allowlist of `~/.claude/scripts/clean-fix/clean-fix.conf`.

`[targets]` is an opt-in allowlist. A target is either a whole directory or a
workspace member (`<dir>/<subpath>`); a member is named by its last path segment
(e.g. `bevy_diegetic`). It is independent of the clean pass's `[build]` list, so
a style skip leaves clean untouched (use `/skip_clean` for that).

**Arguments**: `$ARGUMENTS` — one of:

- empty → show which targets are currently skipped from style
- `<target> [<target> ...]` → skip those targets
- `enable <target> [<target> ...]` → re-enable those targets
- `enable-all` (or `reset`) → re-enable every temp-skipped target

## Instructions

Run the helper with the `style` scope and the action matching `$ARGUMENTS`, then
relay its output verbatim. The helper is the single source of truth — do not
edit the conf with Edit/Write.

```bash
python3 ~/.claude/scripts/clean-fix/phase_skip.py style <action> [target ...]
```

Map `$ARGUMENTS` → action:

- empty → `status`
- first token is `enable-all` or `reset` → `enable-all`
- first token is `enable` → `enable` with the remaining tokens
- anything else → `skip` with all tokens

A commented `[targets]` line is invisible to the conf parser, so the target
drops out of the style eval, review, and fix passes. The helper exits non-zero
on a name with no matching `[targets]` entry.
