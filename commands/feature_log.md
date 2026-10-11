---
description: Read the current feature log or its on-request digest
---

Use `~/.claude/scripts/lib/py
~/.claude/scripts/feature_log/feature_log.py`:

- `/feature_log digest` runs `digest` with any supplied `--since YYYY-MM-DD`.
- `/feature_log live <project> [--crate <crate>]` runs `live --project
  <project>` with the remaining filters.

Writers use `<ChangeReviewFeatureLog/>` and `<CloseChangeReview/>` in
`commands/showrunner/produce.md`, `<FeatureLogCloseOut/>` in
`commands/validate_and_push.md`, or the natedev README close-out.
