---
description: Evaluate a Rust project against the style guide and store pending evaluation markdown with top improvements
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments
- `$ARGUMENTS` is `<project-path> [--fix]`:
  - `<project-path>` — absolute path to a Rust project root (must contain a `Cargo.toml`)
  - `--fix` (optional) — after the evaluation completes, launch the style-fix worktree for this project (Step 6). Without `--fix`, the command stops after storing the pending evaluation markdown.

Throughout the rest of this command, `$ARGUMENTS` refers to **just the project path** — strip the `--fix` flag before substituting it into any path or helper invocation below.

## Step 1: Load the full style guide

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --project-root "$ARGUMENTS"
```

This loads the shared style guide plus any repo-local `docs/style/*.md` files, filtered for the project type.

The output ends with a `=== STYLE_CHECKLIST ===` section listing every rule by number and name. Rules may be annotated with `[non-negotiable]`. This is your evaluation order — work through it sequentially.

If you need exact style file paths for citations, run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files --project-root "$ARGUMENTS"
```

## Step 1.5: Use the clean-fix selection helper

If no pending run exists yet (i.e. `next-unit` errors with "No pending run for ..."), initialize one first. The budget is the configured `[style_eval] max_new_findings` in `~/.claude/scripts/clean-fix/clean-fix.conf`; `next-unit` stops once the pending evaluation markdown contains that many numbered findings.

```bash
python3 ~/.claude/scripts/clean-fix/style_history.py start-run --project-root "$ARGUMENTS"
```

The clean-fix (`style-eval-all.sh`) calls `start-run` itself, so this is only needed for ad-hoc agent invocations.

After the pending run exists, record evaluator liveness with the shared heartbeat script. This is not a substitute for `record-unit`; it only says what the agent is currently doing.

```bash
PROJECT_NAME="$(basename "$ARGUMENTS")"
PROJECT_NAME="${PROJECT_NAME%_style_fix}"
RESULTS_FILE="/tmp/style-eval-results-${PROJECT_NAME}.json"
EVAL_PATH="$EVALUATION_PATH"
if [[ -z "$EVAL_PATH" ]]; then
    EVAL_PATH="/tmp/style-eval-${PROJECT_NAME}-evaluation.md"
fi
~/.claude/scripts/clean-fix/style-eval-heartbeat.sh \
    --project "$PROJECT_NAME" \
    --record agent \
    --project-root "$ARGUMENTS" \
    --eval-path "$EVAL_PATH" \
    --results-file "$RESULTS_FILE" \
    --message "agent started evaluation"
```

You must pull evaluation units one at a time from:

```bash
python3 ~/.claude/scripts/clean-fix/style_history.py next-unit --project-root "$ARGUMENTS"
```

The helper returns JSON.

Rules:
- `status=next` means review exactly that returned unit next
- `status=complete` means stop immediately
- `stop_reason=budget_reached` means the pending evaluation markdown has reached the configured numbered-finding cap
- `stop_reason=exhausted` means there are no unseen eligible units left for this run
- `coverage` is the checked/reviewable style-unit count for this run, in `<checked>/<reviewable>` form
- `reviewable_unit_total` is the denominator for the run; use it to distinguish a full sweep from an early stop
- `non_negotiable_guideline_ids` are binding on every unit review and are returned every time
- each unit is exactly one guideline file — its `unit_id` equals the `guideline_id`
- `see_also_guideline_ids` on a unit lists additional guidelines whose content you must consult as context when reviewing this unit — do NOT record results for them

After reviewing a unit, you must record its result immediately with:

```bash
python3 ~/.claude/scripts/clean-fix/style_history.py record-unit \
    --project-root "$ARGUMENTS" \
    --results "$RESULTS_FILE" \
    --eval-path "$EVAL_PATH"
```

**Ordering rule for findings:** if the unit produced a finding, append it under `## Improvements` in the scratch evaluation markdown at `$EVAL_PATH` **before** calling `record-unit`. `record-unit` saves that markdown into `.history/.pending/<project>.json` and refuses to record a finding whose guideline is not present there.

The results path **must** be project-scoped (`/tmp/style-eval-results-<project>.json`). The clean-fix launches up to 4 codex evals in parallel, all writing to `/tmp`; a shared results file would clobber other agents' in-flight results. Always use `$(basename "$ARGUMENTS")` to derive the path so each agent has its own.

The results JSON for a unit with no finding must look like this:

```json
{
  "unit_id": "rust/when-to-split-a-module.md",
  "results": [
    {
      "guideline_id": "rust/when-to-split-a-module.md",
      "outcome": {
        "status": "no_findings"
      }
    }
  ]
}
```

Recording rules:
- use `outcome.status = no_findings` when that guideline produced no finding
- use `outcome.status = finding` when that guideline produced a finding
- pending evaluation markdown is the budget source of truth; `next-unit` counts its numbered findings
- do not review the next unit until the current unit has been recorded

## Step 2: Survey the project

Read the project's `Cargo.toml` at `$ARGUMENTS/Cargo.toml` to understand the project structure (workspace members, dependencies, features).

Then find and read all `.rs` source files under `$ARGUMENTS/src/`, `$ARGUMENTS/examples/`, and under any workspace member `src/` and `examples/` directories. For large projects, prioritize:
- `lib.rs` and `main.rs` files
- Module root files (`mod.rs`)
- Files with the most code

Read enough to form a thorough understanding of the codebase's patterns. Aim for at least 15-20 source files or all files if fewer exist.

## Step 3: Review existing pending evaluation markdown (if present)

Export any already-pending evaluation markdown to the scratch path, if it exists:

```bash
python3 ~/.claude/scripts/clean-fix/style_history.py export-evaluation \
    --project "$PROJECT_NAME" \
    --output "$EVAL_PATH" 2>/dev/null || true
```

If `$EVAL_PATH` exists after that, read it. For each previously listed improvement:
- **Verify** whether the issue still exists in the current code (check the specific files and line numbers cited)
- **Keep** it if the violation is still present (update file paths and line numbers if they've shifted)
- **Remove** it if the code has been fixed

Keep any still-valid findings. They count toward the same pending evaluation numbered-finding cap as newly found issues.

## Step 3.5: Exclude findings already being fixed in a worktree

The active style-fix scratch evaluation path for this project is: `$WORKTREE_EVAL_PATH`

If the line above shows a real filesystem path, check whether that file exists.

If the line above shows the literal string `$WORKTREE_EVAL_PATH` (i.e. no substitution was made, because this command was invoked directly rather than via the clean-fix), derive the scratch path instead: take the project directory name and check `/private/tmp/claude/style_fix_<project>_evaluation.md`. For example, if `$ARGUMENTS` is `~/rust/my_project`, check `/private/tmp/claude/style_fix_my_project_evaluation.md`.

If that file exists, read it. These findings are already being addressed in a style-fix branch. When evaluating in Step 4, **do not re-discover** any finding that matches a style-fix scratch finding by title or by the same style rule applied to the same files. This prevents duplicate work between the primary evaluation and the in-progress worktree fixes.

## Step 4: Evaluate only the selected guideline units

Loop until the helper returns `status=complete`.

For each returned unit:
0. Record an agent heartbeat before reviewing the unit:
   ```bash
   PROJECT_NAME="$(basename "$ARGUMENTS")"
   PROJECT_NAME="${PROJECT_NAME%_style_fix}"
   RESULTS_FILE="/tmp/style-eval-results-${PROJECT_NAME}.json"
   EVAL_PATH="$EVALUATION_PATH"
   if [[ -z "$EVAL_PATH" ]]; then
       EVAL_PATH="/tmp/style-eval-${PROJECT_NAME}-evaluation.md"
   fi
   ~/.claude/scripts/clean-fix/style-eval-heartbeat.sh \
       --project "$PROJECT_NAME" \
       --record agent \
       --project-root "$ARGUMENTS" \
       --eval-path "$EVAL_PATH" \
       --results-file "$RESULTS_FILE" \
       --message "agent reviewing <unit_id>"
   ```
1. Read the full rule content for that selected unit
2. Read the content of any `see_also_guideline_ids` on the unit as review context — apply the selected unit's rule, informed by that context, but do not record findings against the see_also'd guidelines (they get their own review cycle)
3. Re-read the returned `non_negotiable_guideline_ids` and treat them as binding for this unit
3a. **If the unit's frontmatter has `mechanism: clippy` and `mode: auto`,** the rule is clippy-owned. Treat the `lint:` list in the frontmatter as the source of truth: only sites that clippy actually flags for one of those lints may be raised as findings for this unit. If the project's `Cargo.toml` already enables the lint at `warn`/`deny`, the existing `cargo clippy --all-targets` output is sufficient. Otherwise, run `cargo clippy --all-targets -- -W <lint_name>` (one `-W` per lint) and use that output. If clippy is silent project-wide for every lint in the unit's `lint:` list, record `outcome.status = no_findings` and move on. Do not visual-inspect for this category of rule — visual matches that clippy does not fire on are by definition not violations (e.g. `|x| x.method()` reached via `Deref`).
4. Apply the unit's **rule** to the entire project. The unit of exhaustiveness is the rule, not the first example you notice.

   Concretely: derive the abstract pattern the rule prohibits or requires (e.g. "any field/parameter/binding whose name doesn't match the snake_case form of its type", "any raw literal at a call site that should be a named constant"), then search the codebase for every distinct match of that pattern — across all files, all binding positions, all literal kinds the rule covers.

   **Mandatory enumeration artifact.** Before deciding the unit's `outcome.status`, you must produce two written lines for the unit, regardless of whether the outcome is `finding` or `no_findings`:

   - **Surface searched** — one sentence naming the abstract pattern, derived from the rule (not from the first example). If the guideline file has a `### Surface` section, use it as the source of truth and quote it back; do not narrow it.
   - **Search** — the exact rg / LSP / grep command(s) executed against the project, plus the raw match count those commands produced.

   For `outcome.status = finding`, both lines must appear under the finding in the scratch evaluation markdown (see Step 5 template), and `len(Locations)` must equal the post-exception-filter match count. A Locations list shorter than the filtered count means the finding is malformed — expand it to cover every real violation before recording.

   For `outcome.status = no_findings`, both lines must appear in your assistant-visible reasoning before calling `record-unit`, and the post-exception-filter match count must be `0`.

   **Apply the rule's own `Exceptions` clause as a filter, not as commentary.** If the guideline file lists exceptions (init/identity literals, format-macro args, `#[cfg(test)]` blocks, match-arm enum-paired labels, origins like `Vec3::new(0.0, y, 0.0)`, range starts `0..n`, etc.), filter the raw regex matches against those clauses *before* fixing `Locations`. Report both numbers under Search: `N raw matches → M after exception filter`. The Locations list contains only the post-filter set — never raw regex hits with prose explaining "most are exempt." If the post-filter count is `0`, record `no_findings`.

   This artifact is the failure-prevention mechanism. Prose alone has not been enough — past evals have stopped at the first cluster and recorded one-site findings while other sites of the same rule went unlisted. Writing the Surface and Search lines down forces the project-wide enumeration to happen before the outcome is fixed.

   If you spot one match (e.g. `color: ColorMode`), that is a trigger to enumerate the rule across the codebase, not the scope of the finding. The finding's Locations list must include every match of the rule, not just every site of the first example.

   **One guideline = one finding = every site in the project that violates the rule.** Stopping at the first cluster is the failure mode this step exists to prevent.

   Tool precedence — pick the cheapest tool that can answer the question precisely:
   - **Use the LSP tool** for any *semantic* query — anything about types, method signatures, trait implementations, references, or callers. Examples: "find every method whose body never references `self`" (`documentSymbol` per file → inspect signatures), "find every function that takes `&Vec<T>`" (`workspaceSymbol` → `hover` for type), "count the impls of trait `T`" (`findReferences` on the trait declaration), "find every caller of fn `f`" (`prepareCallHierarchy` + `incomingCalls`). LSP answers structurally; ripgrep cannot.
   - **Use ripgrep** for *textual* queries — keywords, attribute strings, identifier patterns. Examples: `pub mod`, `MessageReader<`, `#[reflect(Component)]`, `register_type::<`. The pre_filter system already handles the universally-textual cases; for the rest, ripgrep is fine.
   - **Read source files directly** only when LSP and ripgrep can't express the question or you need to verify a specific site found via the tools above. Never rely on the files you happened to read in Step 2 — they are not exhaustive.
   - LSP availability: claude has the `LSP` tool when `ENABLE_LSP_TOOL=1` is in env; codex has the same coverage via the `mcp-language-server` MCP. If neither is reachable, fall back to ripgrep + AST and document the limitation in the finding.
5. Decide the result for the unit's single guideline:
   - if it has no issue, the result is `outcome.status = no_findings`
   - if it has an issue, append the finding to the scratch evaluation markdown at `$EVAL_PATH`
   - if it matches an in-progress `_style_fix` finding from Step 3.5, do not include it
6. **If this unit produced a finding, append it to `$EVAL_PATH` under `## Improvements` BEFORE recording.** `record-unit` will save `$EVAL_PATH` into pending JSON and refuse to record a finding whose guideline is not present in that markdown.
7. Record the unit immediately with `record-unit` (which now requires `--eval-path`)
8. Record an agent heartbeat after `record-unit`, with a message like `agent recorded <unit_id>: no_findings` or `agent recorded <unit_id>: finding`
9. Then ask the helper for the next unit

Important:
- do not invent your own stopping rule
- keep pulling units until the helper says `budget_reached` or `exhausted`
- `no_findings` units do not change the numbered-finding count in the pending evaluation markdown

## Step 4.5: Verify recorded findings are in the scratch evaluation markdown

After the helper returns `status=complete` (i.e. `budget_reached` or `exhausted`), re-read `$EVAL_PATH` and confirm that every recorded finding appears under `## Improvements` with a matching `**Style file**:` line.

If any recorded finding is missing from `$EVAL_PATH`, append it under `## Improvements` before finalizing. A missing entry here means the pending markdown is out of sync with what was recorded — finalization downstream will mark it as `eval_dropped` in history if you don't fix it now.

This is a backstop on top of the per-unit ordering rule in Step 4 step 6 — `record-unit` already enforces presence at recording time, but recording does not protect against later edits to `$EVAL_PATH` that remove the finding. Verify before continuing.

## Step 5: Write and save the pending evaluation markdown

Write `$EVAL_PATH` with the findings currently allowed by the helper.

If there are **no violations**, write the minimum that signals completion:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]
**Rules checked**: [coverage from the final `next-unit` response, plus stop reason in `<checked>/<reviewable> (<stop_reason>)` form]

## No violations found
```

Otherwise, write:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]
**Rules checked**: [coverage from the final `next-unit` response, plus stop reason in `<checked>/<reviewable> (<stop_reason>)` form]

## Improvements

### 1. [Title]

**Style file**: `[full path from the loader file list]`
**Style rule**: [which rule from the guide]
**Surface searched**: [one sentence naming the abstract pattern the rule covers — e.g. "any string or numeric literal at a call site that names a domain entity (file name, path keyword, target kind, port, threshold)"]
**Search**: `[the exact rg / LSP / grep command(s) actually run]` — **N matches**
**Locations** (every violation found project-wide; count must equal **N** above):
- `path/to/file.rs:42` — [optional brief note about this site, only if it differs materially from the others]
- `path/to/other.rs:15` — [...]
- `path/to/third.rs:88-94` — [...]
**Recommended pattern**: [what it should look like — written once, applies to every location]

### 2. [Title]

[same structure, including Style file]

[...continue numbering for all findings]

```

Do NOT include an "Overall Assessment" section — just list the findings.

After writing the final markdown, save it into pending JSON. The helper also stores the authoritative `checked_unit_count`, `reviewable_unit_total`, `coverage`, and `stop_reason` in pending JSON; do not rely on scratch-file mtimes to infer what ran:

```bash
python3 ~/.claude/scripts/clean-fix/style_history.py save-evaluation \
    --project-root "$ARGUMENTS" \
    --evaluation "$EVAL_PATH"
```

Then record one last agent heartbeat with `--message "agent saved pending evaluation"`.

Requirements for each finding:
- Rank by impact: most violations / most deviation from the guide comes first
- **Locations must enumerate every site that violates the rule project-wide.** Before writing the finding, name the abstract pattern the rule covers (not the first example you found), run a project-wide search for that pattern, and list every match. A finding scoped to one cluster of sites — when other sites elsewhere violate the same rule — is a defective finding, not a partial one. The next clean-fix will re-flag the same guideline instead of clearing it.
- **`Surface searched` and `Search` are required fields** alongside `Locations`. The Search line must contain the literal command(s) you ran (not "I searched") and the resulting match count. `len(Locations)` must equal that match count. A finding without these fields, or with a Locations count that disagrees with the Search count, is malformed.
- If the guideline file contains a `### Surface` section, copy or paraphrase it into the `Surface searched` field — the rule's own surface is the source of truth, not your interpretation of the rule's lead example.
- Be actionable: someone should be able to act on each item without re-reading the style guide
- Only flag things that genuinely violate the style guide — do not invent rules
- Always include the full path to the exact style guide file each finding comes from, using the loader file list (e.g., `~/rust/nate_style/rust/one-use-per-line.md` or `$ARGUMENTS/docs/style/frontend-boundaries.md`)

## Step 6: If `--fix` was passed, launch the style-fix worktree

Skip this step entirely if `--fix` is not in the original arguments — `/style_eval` ends at Step 5. The clean-fix never passes `--fix`, so its behavior is unchanged.

If `--fix` was passed, you are running interactively and the user is waiting on the fix to finish. The fix takes 10–20 minutes; do all of the following without narration in between so the user hits a single "running, you'll be notified" message instead of two.

1. Run `python3 ~/.claude/scripts/clean-fix/style_history.py evaluation-status --project "$(basename "$ARGUMENTS")" --field status`. If it prints `no_findings`, print `nothing to fix` and stop. Do not launch the fix script.

2. Compute the log path deterministically — do not wait to read it from stdout:

   ```
   LOG_PATH=$HOME/.local/logs/clean-fix/style-fix-manual-$(date '+%Y%m%d-%H%M%S').log
   ```

   The manual launcher names its log using `date '+%Y%m%d-%H%M%S'` taken at invocation time, so as long as you compute the same expression in the same shell second the path matches. (You can verify after the fact by reading the launcher's first stdout line.)

3. In a single response, do **both** of the following:

   a. Run the foreground launcher via Bash with `run_in_background: true` + `dangerouslyDisableSandbox: true` (codex needs unsandboxed):

      ```bash
      ~/.claude/scripts/clean-fix/style-fix-manual.sh --foreground "$(basename "$ARGUMENTS")"
      ```

   b. In the same response, invoke `/clean_fix` (the `clean_fix` skill) with arguments `monitor ${LOG_PATH} $(basename "$ARGUMENTS")`. For `style-fix-manual-*.log` paths it arms the sandbox-safe Python helper (`style-fix-monitor.py`) and owns the event-to-update mapping — follow its <StyleFixManualEvents/> reporting.

4. Tell the user once: "fix running, log: `<path>`. I'll surface phases as they arrive and post a final summary when codex finishes." Then **yield** — do not sleep, do not poll, do not re-read the log yourself.

5. When the harness delivers the `run_in_background` completion event for the launcher (the `exec`'d `style-fix-worktrees.sh` returned), the Monitor will already have terminated on its own via the `phase=launcher-exit` sentinel. If for any reason it has not (e.g. the trap did not fire), call `TaskStop` on the Monitor's task id. Then read `/private/tmp/claude/style_fix_<project>_evaluation.md`'s `## Fix Summary` section plus the tail of the manual log. Post a final summary covering: applied/skipped/proposed findings, `cargo mend` status, and clippy status. If the final progress phase was `failed`, surface the `reason=` value first.
