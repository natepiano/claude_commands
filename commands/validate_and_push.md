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

**On validation success — pick a push path:**
1. Get current branch: `git branch --show-current`
2. Get default branch: `gh repo view --json defaultBranchRef -q .defaultBranchRef.name` (unsandboxed)
3. If the current branch is NOT the default branch → **direct push path**
4. If the current branch IS the default branch, check whether it requires a PR:
   - `gh api "repos/$(gh repo view --json nameWithOwner -q .nameWithOwner)/rules/branches/$DEFAULT" --jq 'any(.[]; .type=="pull_request")'` (unsandboxed)
   - `false` / empty → **direct push path**
   - `true` → **PR-required path**

**Direct push path:**
- Push the current branch to origin
- Monitor CI in the background using the watch script:
  1. Get the HEAD commit SHA and current branch name
  2. Launch `~/.claude/scripts/validate_and_push/watch_ci.sh <branch> <sha>` via Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`
  3. Tell the user: "Pushed to origin. CI is being watched in the background — I'll report when it finishes."
  4. When the background task notification arrives, read its output and report the final CI status
  5. If CI fails, summarize which jobs/steps failed

**PR-required path (current branch IS default and default requires a PR):**
- List the unpushed commits: `git log --format="%h %s" origin/$DEFAULT..HEAD`
- If there are zero unpushed commits, stop and report "nothing to push"
- Propose a branch name based on the unpushed commits:
  - If all unpushed commits share a conventional-commit prefix (`style:`, `refactor:`, `fix:`, `feat:`, `chore:`, `docs:`), use that prefix plus a slug of the shortest meaningful subject
  - Otherwise, slugify the top (most recent) commit's subject
  - Slug rules: drop `type:` prefix, kebab-case, strip non-alphanumeric, cap at 50 chars, trim trailing `-`
- Present the commits and proposed name to the user. Ask: "Use `<proposed-name>` as the PR branch, or provide a different name?"
- Once the user confirms or provides a name:
  1. `git switch -c <name>` (current HEAD is already at the commits to push; the new branch inherits them)
  2. Reset local default branch back to origin so the commits only live on the new branch: `git branch -f $DEFAULT origin/$DEFAULT`
  3. Push the branch: `git push -u origin <name>` (unsandboxed)
  4. Open the PR: `gh pr create --fill` (unsandboxed) — fills title/body from the commit messages
  5. Capture the PR number from `gh pr view --json number -q .number`
  6. Watch CI: `gh pr checks <pr-number> --watch` (unsandboxed) — blocks until all checks finish
  7. If all checks pass → `gh pr merge <pr-number> --rebase --delete-branch` (unsandboxed)
  8. After the merge succeeds:
     - `git switch $DEFAULT`
     - `git pull --ff-only`
  9. Report: "Merged PR #N as `<merge-sha>`. CI was green before merge."
- If CI fails on the PR:
  - Do NOT merge
  - Leave the branch up so the user can iterate
  - Report which jobs failed with links to the failing runs
