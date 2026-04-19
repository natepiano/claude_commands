---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

**Run validation:**
- Run `~/.claude/scripts/validate_and_push/run_validation.sh` with `dangerouslyDisableSandbox: true` — taplo panics under the macOS Mach IPC sandbox, so the entire validation must run unsandboxed
- The script will abort automatically if there are uncommitted changes

**On validation failure — formatting (rustfmt or taplo):**
- If the failed step is `rustfmt` or `taplo`, auto-fix:
  1. Run `cargo +nightly fmt --all` and/or `taplo fmt` (unsandboxed) to apply formatting
  2. Verify the fix worked by re-running the same check command(s) that failed
  3. Commit the changes with message: `style: apply formatting`
  4. Re-run the full validation script from the top
- If validation fails again on a non-formatting step, fall through to the general failure handling below

**On validation failure — `cargo mend` (auto-fixable):**
- If the failed step is `cargo mend` and the warnings indicate they are auto-fixable (e.g. "this warning is auto-fixable with `cargo mend --fix`"):
  1. Run `cargo mend --fix` (unsandboxed) to apply fixes
  2. Verify the fix worked by re-running `cargo mend`
  3. If clean, commit the changes with message: `style: apply cargo mend fixes`
  4. Re-run the full validation script from the top
- If `cargo mend --fix` does not resolve all warnings, fall through to the general failure handling below

**On validation failure — anything else:**
- Do NOT push
- Do NOT attempt to fix the errors — stop immediately and report
- Do NOT suggest fixes, do NOT apply fixes, do NOT continue to the next step
- Report the validation errors to the user with a clear summary of what step failed and why
- Wait for the user to decide next steps

**On validation success:**
- Push the current branch to origin
- Monitor CI in the background using the watch script:
  1. Get the HEAD commit SHA and current branch name
  2. Launch `~/.claude/scripts/validate_and_push/watch_ci.sh <branch> <sha>` via Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`
  3. Tell the user: "Pushed to origin. CI is being watched in the background — I'll report when it finishes."
  4. When the background task notification arrives, read its output and report the final CI status
  5. If CI fails, summarize which jobs/steps failed
