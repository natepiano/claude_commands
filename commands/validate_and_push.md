---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

**Run validation:**
- Run `~/.claude/scripts/validate_and_push/validate_ci.sh`
- The script will abort automatically if there are uncommitted changes

**On validation failure:**
- Do NOT push
- Do NOT attempt to fix the errors — stop immediately and report
- Do NOT run taplo fmt, cargo fmt, or any other command to try to resolve the failure
- Do NOT suggest fixes, do NOT apply fixes, do NOT continue to the next step
- Report the validation errors to the user with a clear summary of what step failed and why
- Wait for the user to decide next steps

**On validation success:**
- Push the current branch to origin
- Monitor the GitHub CI workflow run using `gh run watch` on the most recent run for the current branch
- Report the final CI status to the user
- If CI fails, summarize which jobs/steps failed
