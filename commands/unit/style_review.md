---
description: Review a caller's diff or the working tree against the style guide.
---

# Direct — style review

**Usage:** `/unit:style_review [since <ref>]`

For a run by hand, apply <StyleReview/> in the current session. The caller
names the reviewed range; a phase passes its baseline as `since <ref>`.
With no range, review the working tree against `HEAD`, including staged,
unstaged, and untracked files. An active `/unit:direct` run is not required.

Everything below is the contract.

---

<StyleReview>
Perform the diff scan and checklist walk inline; do not worker this review.

1. Read the operation switches with:

   ```bash
   bash ~/.claude/scripts/lint/lint_config.sh export
   ```

   On failure, report the error and stop. If `LINT_OP_STYLE_REVIEW=off`, report
   `Style review: Off` and stop; never report an off check as clean. This
   review runs even when mend and clippy found no issues.
2. Set `STYLE_SINCE` to the caller's baseline or `since <ref>` argument,
   defaulting to `HEAD`. Set `STYLE_DIR` anew for this invocation: use the
   caller's named directory (a run supplies its session directory), or run
   `mkdir -p /tmp/claude` followed by
   `STYLE_DIR=$(mktemp -d /tmp/claude/style-review.XXXXXX)` when none is named.
   Create the caller's directory if needed. On directory creation failure,
   report the error and stop. Keep this directory for every later step.

   Validate the baseline and require the tracked diff to succeed before
   appending untracked files or reading additions:

   ```bash
   if ! git rev-parse --verify --quiet "${STYLE_SINCE}^{commit}" > /dev/null; then
     printf 'Style review: invalid baseline %s\n' "${STYLE_SINCE}" >&2
     exit 1
   fi
   if ! git diff "${STYLE_SINCE}" -- > "${STYLE_DIR}/style-review.diff"; then
     printf 'Style review: tracked diff failed for %s\n' "${STYLE_SINCE}" >&2
     exit 1
   fi
   git ls-files --others --exclude-standard -z \
     | xargs -0 -I{} git diff --no-index /dev/null "{}" 2>/dev/null \
     >> "${STYLE_DIR}/style-review.diff"
   grep '^+' "${STYLE_DIR}/style-review.diff" | grep -v '^+++' > "${STYLE_DIR}/style-review-additions.txt"
   ```

   Either validation or tracked-diff failure reports the ref and stops the
   review; never treat that failure as an empty or passing review.
   `git diff <ref>` includes committed, staged, and unstaged work since that
   baseline. Untracked files are always included as all-additions diffs;
   `git diff --no-index` status 1 means differences found. Keep the headers
   in `.diff` for source locations; `-additions.txt` is the reading aid.
   If the additions file is empty, report `No changes to review.` with the
   range and stop.
3. Determine Rust applicability from the combined diff's changed paths,
   including untracked files and deletions: `.rs`, `Cargo.toml`, or `Cargo.lock`.
   With no Rust change, do not load the guide or walk its rules; record
   `Skip (no Rust change)` for the Rust checklist and continue to step 4.
   Otherwise, unless `=== STYLE_CHECKLIST ===` is already in context, load
   the guide:

   ```bash
   zsh ~/.claude/scripts/rust_style/load-rust-style.sh
   ```

   Confirm the script's summary line. Walk every numbered rule in the
   checklist against the additions and present a row for each:

   | # | Rule | Result |
   | --- | --- | --- |
   | 1 | Rule name | Pass / **VIOLATION: description at path:line** / Skip (reason) |

   `Pass` means the additions conform; `VIOLATION` names what violates the
   rule and where; `Skip` gives a short reason the rule does not apply.
4. For the banned-words rule, use the canonical scanner; do not enumerate
   stems, write a patterns file, or build a regex:

   ```bash
   python3 ~/.claude/scripts/hooks/banned_words_lib.py --diff < "${STYLE_DIR}/style-review.diff"
   ```

   Exit 0 is `Pass`; exit 1 is `VIOLATION`. Output reports each added line's
   real `path:line`, including untracked files. Pre-existing lines are not
   scanned. Record the scanner result in the table, including when the Rust
   checklist is skipped. Any other exit is an error; report it and stop.
5. Open every violation in the caller's findings ledger when one is supplied,
   for repair through <FixDispatch/>; otherwise report it in the table.
   Report the reviewed range and violation count, or that the review passed.
</StyleReview>
