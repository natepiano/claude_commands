---
description: Review a diff against the style guide.
---

# Direct — style review

**Usage:** `/unit:style_review [since <ref> [until <ref>]]`

<StyleReview>
The reviewer reads prepared inputs and returns a marked report; it writes
nothing. `scripts/lint/style_review.py` owns switch reading, baseline
validation, diff preparation, Rust applicability and report capture.
`config/review/style.md` is the one reviewer assignment and checklist protocol.

For a phase, use `assign.sh --role review --style-since` under <ReviewPromptContract/>;
the runner prepares input and saves the returned report. The reviewer includes
violations in its normal summary; <Synthesize/> records them in the ledger.
The reviewer does not write the ledger.

For a run by hand, create an empty prompt file and a fresh output directory
outside the reviewed repository,
then run `~/.claude/scripts/lib/py ~/.claude/scripts/lint/style_review.py prepare --working-dir
<repo> --since <ref> --phase-dir <output directory> --prompt <empty prompt>
--directory-file <output directory>/input-directory`. Default `<ref>` is HEAD;
add `--until <ref>` for a committed range, excluding working-tree changes.
No active run is needed. Read the printed result and generated `prompt.md`
in the returned input directory. If it contains an assignment, apply it in
this session, return the marked table and result, and capture that response
with `style_review.py finish --input-dir <returned directory> --result
<saved response> --dispatch-code 0`. Off, No changes and preparation errors
are saved by preparation itself. Never treat an error as a passing review.
</StyleReview>
