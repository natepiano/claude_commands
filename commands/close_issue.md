---
description: Close one Hanadocs issue using the vault's closure metadata and automatic backlog ranking.
---

Close exactly one issue in the Hanadocs Obsidian vault. This command works from any project, so always use the fixed absolute paths below and never derive the vault from the current working directory.

## Fixed scope

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues/*.md`
- Rank updater: `/Users/natemccoy/.claude/scripts/prioritize/renumber.py`
- Do not inspect or modify issues outside this vault.
- Do not commit changes.

## Usage

```text
/close_issue [<issue>] [--reason <text> | --no-reason] [--date YYYY-MM-DD | on YYYY-MM-DD]
```

`$ARGUMENTS` may contain the issue selector first, followed by options:

- `<issue>`: one issue path, filename, stem, Obsidian wikilink, title, or unique case-insensitive substring.
- `--reason <text>`: the concise reason the issue is being closed. The value may contain spaces; treat the text through the next recognized option or a final `on YYYY-MM-DD` date phrase as the reason.
- `--no-reason`: explicitly close without a `reason` property. Reject this when `--reason` is also present.
- `--date YYYY-MM-DD`: the closure date. Reject invalid calendar dates.
- `on YYYY-MM-DD`: a natural-language alias for `--date YYYY-MM-DD` when it is the final phrase. Reject an invocation containing both date forms.

Examples:

```text
/close_issue [[network-client-hana-plugin]] --reason implemented
/close_issue issues/networking-reliability-design.md --reason "superseded by the network lanes design"
/close_issue detect worktree deletion --no-reason --date 2026-07-14
/close_issue synchronous screenshots --reason implemented on 2026-07-14
```

## Safe inference

- If `<issue>` is omitted, infer it only when the current conversation unambiguously identifies exactly one open Hanadocs issue, including when the invocation directly follows a discussion or presentation of that issue. Never infer it from file modification time, current working directory, backlog rank, or filesystem order.
- If `--reason` and `--no-reason` are both omitted, infer a reason only from an explicit outcome in the current conversation, such as implemented, fixed, obsolete, duplicate, intentionally declined, superseded, or no action needed. Preserve useful specificity in one concise line. Never assume `implemented` merely because the user invoked this command.
- If the issue cannot be inferred uniquely, ask one concise question and show at most five matching open candidates. Make no changes before the user resolves the target.
- If the reason cannot be inferred reliably, ask one concise question for it. Tell the user they may answer `no reason` to omit the property. Do not make changes until they answer.
- If `--date` is omitted, use today's local date from the system in `America/New_York`, formatted `YYYY-MM-DD`.
- `status` is not a user argument: this command always writes `closed`.
- `date_modified` is not a user argument: set it to the same date as `date_closed`.
- `stage` is not a user argument and has no closing default: preserve its current value unchanged.

## Resolve the issue

1. Read the full `$ARGUMENTS` and separate the recognized options from the issue selector. Reject unknown options and more than one requested issue with a concise usage message.
2. Normalize an Obsidian wikilink by removing `[[`, `]]`, an optional alias after `|`, and an optional `.md` suffix. Preserve an explicitly supplied absolute path.
3. Resolve candidates only under `/Users/natemccoy/rust/hanadocs/issues/`, in this order:
   - exact absolute or `issues/...` path;
   - exact filename or filename stem, case-insensitively;
   - exact title-like stem after treating runs of spaces, hyphens, and underscores as equivalent separators, case-insensitively;
   - unique case-insensitive substring of that normalized filename stem.
4. Resolve the real path and require it to be a regular, non-symlink Markdown file whose real parent is `/Users/natemccoy/rust/hanadocs/issues`. Reject traversal and outside-vault paths.
5. Inspect the file's complete YAML frontmatter before changing it. Require exactly one top-level `status` property.
6. Read the current APFS creation and modification timestamps before changing the file. Require their calendar dates in `America/New_York` to match the existing `date_created` and `date_modified` frontmatter dates. If either differs, stop without writing and report the mismatch; never choose the filesystem or YAML side implicitly.
7. If the resolved issue is already closed, make no changes. Report its existing `date_closed` and `reason`; this command closes issues rather than revising prior closure records.
8. If matching remains ambiguous, show at most five matching open issue paths and ask the user to identify one. Never choose arbitrarily.

## Apply the closure

Once the target, reason choice, and date are resolved, invoking `/close_issue` authorizes the edit; do not ask for another confirmation.

Use `apply_patch` to make the smallest possible edit to the selected issue's top-level YAML frontmatter:

1. Change `status: open` to `status: closed`.
2. Set `date_closed: "[[YYYY-MM-DD]]"` to the resolved closure date. Insert it before `date_created` when it is new.
3. Set `date_modified: "[[YYYY-MM-DD]]"` to the same date.
4. When a reason is supplied or inferred, set a single top-level `reason` property. Keep it to one line, use a plain YAML scalar when safe, and use a correctly escaped double-quoted YAML scalar when punctuation could be interpreted as YAML syntax. Insert a new reason after `project` when practical.
5. With `--no-reason`, remove any existing top-level `reason` so the new closure is recorded without one. This matters for reopened issues that still carry metadata from an earlier closure.
6. Preserve `stage`, all seven prioritization judgment properties, the note body, and every unrelated property and formatting choice.

`apply_patch` preserves the existing inode and APFS creation timestamp but advances the modification timestamp. Immediately after the patch, require the inode and creation timestamp to equal the captured pre-edit values, then set the filesystem modification timestamp to noon on the resolved closure date in `America/New_York` with `/usr/bin/SetFile -m`. Verify that the filesystem calendar dates now match `date_created` and `date_modified`. If any timestamp operation or verification fails, do not run automatic ranking and report the exact mismatch.

Do not directly edit `backlog_score` or `backlog_rank`. After the closure edit succeeds, run:

```bash
/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/renumber.py --apply
```

This removes live score/rank fields from the closed issue and immediately recalculates the positions of every valid open issue. The background watcher may observe the same edit afterward; when the positions are already current, its check makes no further changes.

## Verify and report

1. Re-read the selected issue and verify exactly one `status: closed`, the expected `date_closed`, matching `date_modified`, the resolved reason choice, unchanged `stage`, and no `backlog_score` or `backlog_rank`. Verify again that the filesystem creation and modification calendar dates match `date_created` and `date_modified` after automatic ranking.
2. Run the rank updater again without `--apply` and require it to report no pending mechanical changes. Missing or invalid ratings on other open issues may remain visible for `/prioritize`; they do not invalidate the closure or prevent valid open issues from being ranked.
3. If the issue edit fails, do not run the rank updater. If automatic ranking fails after the issue was closed, leave the valid closure metadata in place, report the exact failure plainly, and do not claim the backlog positions are current.
4. Report the clickable issue path, closure date, recorded reason or `no reason`, and whether automatic ranking was applied and verified.

Never commit the changes.
