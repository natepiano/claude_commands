# Skip Clean

Skip or re-enable a directory for the **clean + build** pass, by commenting its
line in the `[build]` allowlist of `~/.claude/scripts/clean-fix/clean-fix.conf`.

`[build]` is an opt-in allowlist, so "skip" comments the entry out and "enable"
uncomments it. It is independent of the style pass's `[targets]` list — skipping
a directory from clean has no effect on style (use `/skip_fix` for that).

**Arguments**: `$ARGUMENTS` — one of:

- empty → show which directories are currently skipped from clean
- `<dir> [<dir> ...]` → skip those directories
- `enable <dir> [<dir> ...]` → re-enable those directories
- `enable-all` (or `reset`) → re-enable every temp-skipped directory

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

The helper tags its edits with `#CLEAN_FIX_SKIP#` so `enable-all` only reverses
temp skips and never touches plain doc comments. It exits non-zero on a name
with no matching `[build]` entry.
