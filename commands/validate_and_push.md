---
description: Run local CI validation, push to origin, and monitor GitHub CI
---

Run `.github/scripts/validate_ci.sh` from the project root.

**On validation failure:**
- Do NOT push
- Report the validation errors to the user with a clear summary of what failed and why

**On validation success:**
- Push the current branch to origin
- Monitor the GitHub CI workflow run using `gh run watch` on the most recent run for the current branch
- Report the final CI status to the user
- If CI fails, summarize which jobs/steps failed
