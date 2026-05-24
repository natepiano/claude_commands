# Skip Clean

Skip or re-enable a project for the **clean + build** pass, by editing the
`[exclude]` section of `~/.claude/scripts/clean-fix/clean-fix.conf`.

The clean pass operates on top-level repo directories under `~/rust`. Standalone
repos share `[exclude]` with the style pass, so a clean skip also removes a
standalone repo from style. (Workspace members are not handled here — use
`/skip_fix`.)

**Arguments**: `$ARGUMENTS` — one of:

- empty → show which repos are currently skipped from clean
- `<repo> [<repo> ...]` → skip those repos
- `enable <repo> [<repo> ...]` → re-enable those repos
- `enable-all` (or `reset`) → re-enable every temp-skipped repo

## Instructions

Run the helper with the `clean` scope and the action matching `$ARGUMENTS`, then
relay its output verbatim. The helper is the single source of truth — do not
edit the conf with Edit/Write.

```bash
python3 ~/.claude/scripts/clean-fix/phase_skip.py clean <action> [repo ...]
```

Map `$ARGUMENTS` → action:

- empty → `status`
- first token is `enable-all` or `reset` → `enable-all`
- first token is `enable` → `enable` with the remaining tokens
- anything else → `skip` with all tokens

The helper tags its edits so `enable-all` only reverses temp skips and never
touches permanent `[exclude]` entries. It exits non-zero on an unknown repo or a
wrong-scope name (a workspace member), printing a pointer to `/skip_fix`.
