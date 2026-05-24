# Skip Style Fix

Skip or re-enable a workspace member for the **style eval/fix** pass, by
commenting its line in the `[workspace_members]` section of
`~/.claude/scripts/clean-fix/clean-fix.conf`.

This pass reads `[workspace_members]` for the members of a cargo workspace (e.g.
`bevy_diegetic`, `bevy_lagrange`, `fairy_dust`). Clean never reads this section,
so a style skip leaves the clean pass untouched. (Standalone repos are skipped
via `[exclude]` — use `/skip_clean`.)

**Arguments**: `$ARGUMENTS` — one of:

- empty → show which members are currently skipped from style
- `<member> [<member> ...]` → skip those members
- `enable <member> [<member> ...]` → re-enable those members
- `enable-all` (or `reset`) → re-enable every temp-skipped member

## Instructions

Run the helper with the `style` scope and the action matching `$ARGUMENTS`, then
relay its output verbatim. The helper is the single source of truth — do not
edit the conf with Edit/Write.

```bash
python3 ~/.claude/scripts/clean-fix/phase_skip.py style <action> [member ...]
```

Map `$ARGUMENTS` → action:

- empty → `status`
- first token is `enable-all` or `reset` → `enable-all`
- first token is `enable` → `enable` with the remaining tokens
- anything else → `skip` with all tokens

A commented member line is invisible to the conf parser, so the member drops out
of the style eval and fix passes. The helper exits non-zero on an unknown name
or a wrong-scope name (a standalone repo), printing a pointer to `/skip_clean`.
