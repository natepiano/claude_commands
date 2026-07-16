---
description: Rank the open issue backlog in the Hanadocs Obsidian vault and update its global and project Base views through a calibrated multi-agent review.
---

Prioritize issues in the Hanadocs Obsidian vault using a repeatable rubric and a team of read-only review agents. The primary agent reconciles their recommendations and is the only judgment writer; local helpers and the watcher may update reviewed inputs or derived fields under the shared lock.

## Fixed scope

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues/*.md`
- Bases: `/Users/natemccoy/rust/hanadocs/*.base`
- Strategic goals: `/Users/natemccoy/rust/hanadocs/prioritization goals.md`
- Never derive the vault from the current working directory.
- Never inspect or modify issues outside this vault.

## Execution workflow

Execute this workflow in order. `/prioritize` always audits the full fixed-scope backlog. Do not commit changes.

Parse `$ARGUMENTS` as an optional spot-check size:

- With no argument, set `spot_check_size = 12`.
- With exactly one base-10 integer from `1` through `12`, use that value. `/prioritize 1` therefore shows one issue in the post-apply spot check.
- Reject extra text, multiple arguments, zero, negative values, and values greater than `12` with the concise usage message `Usage: /prioritize [1-12]`; do not start or mutate a session.
- The spot-check size changes only how many issues are shown after recommendations have been applied. It never narrows the issue inventory, reviewer shards, calibration, application, scoring, or final completeness checks.
- On resume, the current invocation's spot-check size replaces the saved presentation size without invalidating reviewed results or prior writes.

### 1. Establish a resumable review session

- Create an absolute session directory under `/tmp/claude/prioritize/<uuid>/` and create an `IN_PROGRESS` marker before launching reviewers. On a later invocation, resume the newest marked session only when the complete current open-issue path set, goals hash, every row hash, and every cited evidence hash still match it. Compare applied rows with their recorded post-apply review hashes and unchanged or not-yet-applied rows with their discovery review hashes. If any membership or hash differs, mark the old session `stale`, preserve it as history, remove its active marker, and start a fresh complete audit rather than hiding changed work. Never resume a closed session as a substitute for the fresh full audit requested by a new invocation.
- Keep `session.json`, agent prompts, raw findings, normalized findings, reconciled proposals, automatic apply manifests, any user spot-check corrections, and logs in that directory. Record enough state to resume safely after a goal-approval pause, agent failure, or interrupted apply. Store the current `spot_check_size` as presentation state, not as part of any review or evidence hash.
- Maintain an exact per-path ledger derived from the inventory and validated findings. Each open path must be accounted for as `unchanged`, `proposed-unapplied`, or `applied`; never infer completion from a displayed sample or an apply-manifest filename.
- Keep `IN_PROGRESS` outside the vault; do not add temporary review markers to issue frontmatter. Remove `IN_PROGRESS` only at the final completion gate after the ledger has no pending paths. An interruption or application failure leaves the marker in place so the next invocation can resume safely.
- Set the working directory for every agent invocation to `/Users/natemccoy/rust/hanadocs`.
- Run every `agent_exec.sh` invocation outside the primary agent's sandbox, requesting host approval when required; pass `readonly` so each review child remains non-writing.
- Warm the agent registry once with `/bin/bash /Users/natemccoy/.claude/scripts/agents/agent_admin.sh prioritize` outside any sandbox. In a shell that sources `/Users/natemccoy/.claude/scripts/agents/agents_config.sh`, capture `agents_resolve_print prioritize.reviewer` and `agents_resolve_print prioritize.calibrator` in `agent_provenance.txt`; `agents_resolve_print` is a sourced shell function, not a standalone executable.
- Verify that `writer_lock.py`, `runner_lock.py`, `watch_signature.py`, `renumber.py`, `review_hash.py`, `review_manifest.py`, `validate_review.py`, `apply_goals.py`, `apply_ratings.py`, and the watcher source/setup/status files exist and pass their lightweight self-checks. If the watcher is installed, require a healthy last status before writing; diagnose an unhealthy watcher rather than silently adding a second writer.
- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/renumber.py` in its default dry-run mode as a preflight. A report of missing ratings is expected and is review input, not a reason to stop.
- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/review_manifest.py --session-dir <session> --shards 4` once to give the goal reviewers a complete open-issue inventory. The helper emits at most four nonempty shards. If the inventory contains zero open issues, report that result and stop cleanly without launching reviewers or writing the vault. Regenerate the manifest after any approved goal change before issue ratings are reviewed.

### 2. Review the ordered goals

Launch three external CLI agents in one parallel wave through `agent_exec.sh` with the `prioritize.reviewer` registry task and `readonly` mode. Give each a self-contained prompt containing this command's fixed scope and settled rubric, the absolute goals-note path, the complete open-issue inventory, an evidence restriction to that goals note and the issue paths in that inventory, and one distinct lens:

1. Goal coverage — do the goals cover the outcomes evidenced by the open backlog without becoming project labels?
2. Goal exclusivity — can every issue choose the single goal it influences most, and are any goals materially overlapping?
3. Goal order and staleness — does the evidence support the current ordering, and has any goal become obsolete or omitted?

Agents may propose retaining, adding, removing, renaming, or reordering goals, but must cite the goals note or inventory issue evidence by absolute note path with a short paraphrase and must not write files. Reconcile their findings yourself; agent agreement is evidence, not a vote. If there is no supported change, continue without asking the user to reapprove the goals. If there is a supported change, present one concise proposal with the current and proposed ordered list, reasons, and affected issue count; include the proposed definition for every added or renamed goal, and preserve retained definitions unless their edits are also shown. Pause for user approval. After approval, write one JSON goal-approval manifest with exactly `expected_goals_hash` (the discovery-time SHA-256 of the complete note), `evidence_hashes` (every cited inventory issue other than the goals note, using its discovery-time `review_hash`), and `updated_content` (the complete approved UTF-8 note). Invoke `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/apply_goals.py <manifest> --apply`. This saves the approved note and immediately uses `renumber.py`'s calculation code while holding the shared writer lock, preserving the contiguous `N - Name` format. Never edit the goals note outside `apply_goals.py`.

Launch each agent outside the primary agent's sandbox, requesting host approval if required; `readonly` still constrains the child agent. Use absolute arguments:

```bash
/bin/bash /Users/natemccoy/.claude/scripts/agents/agent_exec.sh prioritize.reviewer readonly /Users/natemccoy/rust/hanadocs <prompt> <findings> <log>
```

Launch the complete wave before waiting. Join through the host's supported completion mechanism; if a launch returns a session handle, wait on that handle. Verify every exit status plus a nonempty findings file before continuing. If an invocation fails or returns an empty findings file, inspect its log and relaunch it once; never treat failure as agreement or an empty review, and never synthesize results while any launch remains unresolved.

### 3. Audit every open issue

- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/review_manifest.py --session-dir <session> --shards 4` to enumerate every `status: open` issue, capture a review hash that excludes derived score/rank fields plus a hash of the governing goals note, and create up to four deterministically balanced, nonempty JSONL shards. Verify that the union of shard paths equals the open-issue inventory exactly once.
- Launch one `prioritize.reviewer` agent per emitted shard in one parallel read-only wave. Every prompt must include the absolute path to this command, the approved ordered goals, the complete rubric and scoring definitions, its shard manifest, and the response schema below.
- Each manifest row's `linked_evidence` map is the authoritative resolved-link allowlist and binds every canonical note path to its discovery-time evidence hash. Reviewer evidence may cite only the reviewed issue itself, the canonical goals note, and those explicitly linked in-vault Markdown notes; every finding must cite the reviewed issue itself. `review_manifest.py` resolves Obsidian wikilinks/embeds and Markdown `.md` links, while unresolved, ambiguous, symlinked, non-Markdown, and outside-vault targets are excluded. Add allowed linked notes only when they materially support the judgment. Do not inspect sibling repositories, GitHub, or the internet. Sparse evidence requires conservative ratings; it never licenses invented facts.
- Require one JSON object per assigned issue, in the same path order, with no Markdown wrapper:

```json
{"path":"/absolute/issues/example.md","review_hash":"...","goals_hash":"...","verdict":"unchanged|proposed","current":{},"proposed":{},"evidence":[{"path":"/absolute/vault/note.md","detail":"short paraphrase"}],"reason":"short explanation"}
```

- `proposed` must contain all five judgment properties when the verdict is `proposed`. A missing or invalid current property requires `proposed`; otherwise use it only for a supported change. `unchanged` must still echo all current values so audit completeness can be checked.
- Require explicit evidence for four- or five-star urgency. Rate conservatively where support is weak. Never estimate elapsed time.
- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/validate_review.py reviewer --manifest <shard> --findings <agent-output> --output <normalized-output>` for each shard. It must reject malformed JSONL, Markdown wrappers, missing/duplicate/reordered paths, changed source hashes, current-value drift, invalid domain values, missing evidence, and evidence outside the row allowlist; it also records stable hashes for every cited allowed note. Reject or rerun any shard that does not validate.
- Regenerate the complete inventory in a fresh session subdirectory immediately after all reviewer outputs validate. Require its open path set, goals hash, and every discovery review hash to match the reviewed inventory before calibration; if membership or content changed, regenerate shards and re-review the changed audit rather than silently omitting it.

### 4. Calibrate across shards

After every emitted reviewer output validates, launch two `prioritize.calibrator` agents in one parallel read-only wave:

1. Domain consistency — find scale drift and inconsistent values among comparable project/category issues, especially unsupported alignment, impact, urgency, and effort outliers.
2. Global strategic consistency — compare goals and projects, check mutually exclusive goal choices, detect duplicated issue scope, and challenge ratings whose evidence does not support their relative strength.

Give each calibrator this command, the approved goals, every emitted shard manifest and validated finding file, and the same JSONL amendment schema. Calibrator evidence may cite only the canonical goals note and issue paths present in the complete inventory; arbitrary linked supporting notes are not calibration evidence. Calibrators return only evidence-backed amendments; they do not rewrite the full inventory and do not write vault files. When a calibrator finds no amendments, it must return the single completion object `{"status":"complete","amendments":0}` instead of prose or a zero-byte file. Validate each output with `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/validate_review.py calibrator --manifest <inventory> --findings <agent-output> --output <normalized-output>`; the validator requires that manifest to equal the complete live inventory and converts the explicit completion object to an empty normalized amendment set. Apply the same failure rule as the reviewer wave. Reconcile reviewer and calibrator output yourself against the source notes. Do not average values or use majority voting.

### 5. Apply recommendations and show a spot check

- Exclude every fully audited `unchanged` issue from the apply manifest. Reconcile every evidence-backed reviewer and calibrator amendment into one complete proposed judgment map per changed issue; do not average values or let an agent write the vault.
- Write all reconciled proposals to one immutable JSONL apply manifest. Each line contains exactly the issue path, its discovery-time review and goals hashes, the normalized hashes of every cited evidence note including the reviewed issue itself, and the complete proposed judgment map: `{"path":"/absolute/issues/example.md","review_hash":"...","goals_hash":"...","evidence_hashes":{"/absolute/issues/example.md":"..."},"proposed":{"backlog_goal":"...","backlog_alignment":"...","backlog_impact":"...","backlog_urgency":"...","backlog_effort":"..."}}`.
- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/apply_ratings.py <manifest>` first as a dry-run, then run the same command with `--apply` automatically. Invoking `/prioritize` authorizes these calibrated issue-rating updates; do not pause for per-issue or per-group approval. `apply_ratings.py` must revalidate every review, goals, and evidence hash, write only the five judgment fields, use `renumber.py`'s calculation code before releasing the shared writer lock, and restore the previous files if either the judgment writes or automatic ranking fails.
- Mark a proposed row `applied` only after the complete manifest succeeds. Record each row's post-apply review hash in the ledger. If any row fails validation or application, do not silently skip it: leave the session incomplete, identify the exact path and cause, and resume from fresh evidence rather than claiming a partial pass is complete.
- After the write, regenerate the inventory and run `renumber.py` in dry-run mode. Derive `audited`, `ratings changed`, `valid ranked`, and `needs prioritization` counts from the current ledger and that fresh check; never maintain hand-edited counters.
- Show one optional spot-check table after application. If ratings changed, show up to `spot_check_size` changed issues ordered by their resulting `backlog_rank`; if nothing changed, show the highest-ranked `spot_check_size` open issues. Include `Backlog rank`, `Issue`, `Goal`, `Alignment`, `Impact`, `Urgency`, and `Effort`, plus a compact reason for changed rows. Never use letter-only abbreviations for table headers. Beneath the table, show the complete legend once: every current goal name and definition from `prioritization goals.md`, followed by every value and definition for the four rating factors in this command. Render rating cells as their stored one-to-five-star strings.
- Treat the spot check as inspection, not an approval gate. The full audit and application are complete even if the user does not respond. If the user identifies a disagreement, use the underlying rubric fields rather than manually editing `backlog_score` or `backlog_rank`: accept exact corrections or propose the smallest evidence-backed field correction, record it with current hashes, and apply it through `apply_ratings.py`. A direct correction in the user's next message is an authorized continuation even after the audit session has closed.
- In user-facing progress reports, name the responsible local automation instead of saying "the system": `apply_ratings.py` saves the reviewed ratings and immediately uses `renumber.py`'s calculation code; `renumber.py` recalculates scores and positions; the background watcher reacts to every relevant edit regardless of whether it came from `/prioritize`, Obsidian, or another process. After `/prioritize` writes ratings, the watcher checks the same edits and makes no further changes when the positions are already current; after an edit elsewhere that did not update positions, it applies the needed calculation. Explain that apply failures restore the previous files. Say that automatic ranking was applied or verified. Never use wording such as "I reranked" that implies the primary agent manually chose or assigned ranks, and avoid implementation jargon such as "guarded writer" or "deterministic ranker" unless the user asks for technical detail.
- If there are no proposals, report that the full backlog was audited and no judgment metadata changed; still show the configured spot check and verify automatic ranking.

### 6. Verify and close

- Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/renumber.py` in dry-run mode after the complete apply manifest. Report counts for valid ranked open issues, open issues still needing prioritization, closed issues cleaned of live outputs, and any validation failures.
- Regenerate the current open inventory and verify that every open issue was present in the completed audit even when no change was proposed. Compare applied rows with their recorded post-apply review hashes and all other rows with their discovery hashes; if a new or changed row is not covered, audit it before closing the session.
- Mark a normal future prioritization pass `complete` only when the regenerated open path set exactly equals the audited path set, every path has one validated reviewer result, both calibrators completed, every proposal was applied, every applied row matches its post-apply hash, every unchanged row matches its discovery hash, and the mechanical dry-run reports canonical derived state. Then set `session.json` to `complete` and remove `IN_PROGRESS` before presenting the spot check. Otherwise leave the marker, mark the session `paused` or `stale`, state the precise remaining count, and never claim the pass is done.
- Keep failed or interrupted proposals in session history. Resume only a still-unfinished session whose complete hashes match; a new invocation after session close performs a fresh full audit.
- Summarize approved goal changes, automatically applied issue-rating changes, spot-check corrections, and remaining unranked issues. Do not commit.

## Settled contract

### Canonical ordering

- Store one canonical global ordering in the `backlog_rank` frontmatter property.
- Write `backlog_rank` as an unquoted integer; lower numbers mean higher priority.
- Assign dense ordinal ranks from `1` through the number of eligible issues.
- Use the same global rank in every project Base. Project views are filtered subsets, so gaps in their displayed rank numbers are expected.
- Do not create separate per-project rank properties.

### Workflow split

- `/prioritize` owns judgment: review the current ordered goals and every rubric domain, detect missing or stale ratings, use agents to propose new or changed values, reconcile their evidence, and automatically apply the resulting issue-rating recommendations.
- Audit every open issue on each `/prioritize` invocation, but surface only a configurable post-apply spot check and the count of evidence-backed changes. Do not make the user reapprove unchanged ratings or approve recommendations before application.
- Strategic-goal additions, removals, renames, and reordering still require user approval before the goals note changes. Issue-rating recommendations do not; explicit user spot-check corrections override them and are applied through the same validated path.
- `/prioritize` does not ask the user to review mechanically derived score ties. It may show `backlog_rank` as spot-check context, but no workflow writes a rank directly.
- The automatic watcher owns mechanics after any relevant edit: parse current inputs, recompute `backlog_score`, and densely renumber `backlog_rank`.
- Never let the watcher invent or revise `backlog_goal`, `backlog_alignment`, `backlog_impact`, `backlog_urgency`, or `backlog_effort`.

### Deterministic renumbering

- Create and retain `/Users/natemccoy/.claude/scripts/prioritize/renumber.py` as the mechanical scoring and dense-renumbering tool.
- Hard-code the Hanadocs vault and issue paths in the tool; never derive scope from its current working directory.
- Parse and validate every readable domain value, recompute `backlog_score`, sort eligible issues by the settled ordering rules, and assign contiguous `backlog_rank` values `1..N`.
- Sort by `backlog_score` descending. For equal scores, preserve valid existing relative rank; use file path as the deterministic fallback for the first run or newly tied issues. Never require user adjudication for score ties.
- Make dry-run preview the default. Require an explicit `--apply` flag before changing files.
- Preserve all unrelated frontmatter formatting and note content. Write each changed file atomically, copy its APFS creation time exactly, and preserve its permissions and supported file flags. Request an approximately one-millisecond modification-time shift, verify that the stored timestamp actually changed without crossing the `America/New_York` calendar date, and thereby invalidate Obsidian's metadata cache while `obsidian_knife` continues to see the same `date_modified`; fail before replacement when creation-time or same-date modification-time preservation is unavailable.
- When an open issue has missing or invalid rubric inputs, remove any stale `backlog_score` and `backlog_rank` from that issue, report it as needing prioritization, and continue ranking every valid open issue. Never invent a placeholder rating or freeze valid-issue reranking.
- Refuse to apply when ranks among valid issues would not be unique and contiguous, a file changed after discovery, or any write cannot be completed safely.
- Keep judgment out of this helper. Review agents recommend rubric values, the primary agent reconciles them, and explicit user corrections override them; the tool only validates, scores, and renumbers.
- Keep automated coverage for dry-run, apply, idempotence, malformed-domain rejection, score calculation, dense unique ranks, concurrent-write refusal, exact filesystem creation-time preservation, and same-date modification-time cache invalidation across an atomic replacement.

### Automatic ranking watcher

- Retain the watcher source, setup/status helper, and runner under `/Users/natemccoy/.claude/scripts/prioritize/`. Its installed plist belongs at `/Users/natemccoy/Library/LaunchAgents/com.natemccoy.hanadocs-prioritize.plist`.
- Keep a lightweight watcher daemon alive through launchd and monitor every issue file plus `/Users/natemccoy/rust/hanadocs/prioritization goals.md` so new, modified, renamed, and deleted issues and goal-order changes trigger ranking. Do not depend on launchd `WatchPaths`, which can miss filesystem events.
- Poll stable path/inode/size/mtime/ctime signatures at a sub-second interval and run the semantic snapshot/scorer only after a watched signature changes. Use a separate OS-released runner lock plus pending marker so overlapping save bursts coalesce safely; use `/tmp/hanadocs-prioritize/writer.lock` with another OS-released exclusive lock for actual vault writes. A crash must release either lock without stale-PID cleanup.
- Do not use `git diff` as the change detector. Repository state is not the source of truth for live filesystem events.
- Build a canonical semantic snapshot containing each issue path, eligibility fields, and ranking input fields: `status`, `backlog_goal`, `backlog_alignment`, `backlog_impact`, `backlog_urgency`, and `backlog_effort`.
- Exclude generated `backlog_score` and `backlog_rank` from the input snapshot so the watcher's own renumbering writes cannot create a loop.
- Compare the snapshot with the last successful snapshot in `/Users/natemccoy/Library/Caches/hanadocs-prioritize/`. When inputs are unchanged, still run a mechanical check: exit without writing only when generated score/rank state is canonical, and repair it when it has drifted.
- When valid inputs or eligible membership changed, invoke `renumber.py --apply`, validate the result, then atomically replace the cached snapshot.
- Never advance the cached successful snapshot after a validation or renumbering failure. Record event logs and a concise last-status file under `/tmp/hanadocs-prioritize/`.
- Require every write entry point, including direct `renumber.py --apply`, the watcher, approved-goal application, automatic rating application, and user spot-check corrections, to hold the shared OS writer lock. Never run two write passes concurrently. The OS must release the lock on process exit; do not depend on stale-PID deletion for writer safety.
- Writes made by `renumber.py` change watched file signatures. Immediately verify the semantic snapshot and canonical ranks, then advance the signature baseline without a second debounced pass; schedule another pass only when inputs or generated state changed again.
- Treat body-only changes with unchanged ranking inputs as a harmless no-op. Reassessment of judgment metadata remains an explicit `/prioritize` action.
- A missing or invalid open issue must not block valid open issues from being ranked. Remove its stale score/rank, report it, and leave it visible in the global `Needs prioritization` Base view for the next `/prioritize` audit.
- The watcher installer must accept an incomplete backlog. It may reject malformed global inputs or noncanonical derived state, but it must install when the valid subset is canonical and leave every incomplete issue unranked.

### Eligibility

- Rank every issue whose `status` is `open`.
- Include open issues at every stage, including `backlog`, `active`, and `waiting`.
- Exclude closed issues from the live backlog ordering.
- Retain the current rubric inputs when an issue closes so they remain useful history and make reopening cheap, but remove its live `backlog_score` and `backlog_rank`.

### Governing criterion and rubric metadata

- Make alignment with current strategic goals the governing prioritization criterion.
- Read the canonical ordered goals from `/Users/natemccoy/rust/hanadocs/prioritization goals.md`; do not infer or replace them from the issue corpus.
- Give earlier goals more influence through soft weighting, not absolute precedence. A strongly justified issue for a later goal may outrank a weakly justified issue for an earlier goal.
- Never group the final backlog into rigid goal lanes where every issue for one goal outranks every issue for the next.
- Store the single goal an issue influences most in the scalar `backlog_goal` frontmatter property. Its value must match one readable, numerically prefixed domain value defined in the goals note, for example `"1 - Ship Hana"`.
- Normalize Obsidian wikilinks to their displayed text when comparing goals. For example, treat `1 - Ship [[hana|Hana]]` and `1 - Ship Hana` as the same goal while retaining the goals note's readable links.
- Assign exactly one `backlog_goal` to every eligible issue; do not store multiple goals or a goal list.
- Use `backlog_alignment` to record how strongly the issue advances its selected goal.
- Record each rubric component as its own frontmatter property so Obsidian Bases can filter and sort it independently.
- Store `backlog_alignment`, `backlog_impact`, `backlog_urgency`, and `backlog_effort` as separate YAML text scalars containing exactly one through five `⭐` characters. Accept semantically equivalent plain, single-quoted, and double-quoted YAML serialization because Obsidian may rewrite text properties with quotes.
- Parse and validate the star count for scoring. Treat a missing rubric property as unassessed; there is no zero-star assessed value.
- Store the computed `backlog_score` and `backlog_rank` as unquoted numbers.
- Use the existing `category` property for issue-type questions such as the highest-ranked bug, feature, business, or research issues; do not duplicate issue type in the rubric fields.
- Do not encode the rubric as a list or as `key=value` strings inside one property.
- Define every scale and anchor explicitly in this command so agents apply the metadata consistently.

### Effort sizing

- Store relative effort as a one-to-five-star YAML text value in `backlog_effort`.
- Interpret one through five stars as `XS`, `S`, `M`, `L`, and `XL` respectively.
- Use `backlog_effort` only as a relative effort and complexity estimate.
- Never estimate or display real durations such as hours, days, or weeks.
- Never translate an effort value or t-shirt label into a real duration.

### Score model

- Compute the candidate backlog order with a weighted additive score.
- Combine weighted goal alignment, a soft goal-order bonus, impact, urgency, and a modest subtractive effort penalty.
- Never divide by `backlog_effort`; coarse effort estimates must not make tiny tasks dominate or make an `XL` strategic initiative disappear.
- Parse `A`, `I`, `U`, and `E` as the star counts in `backlog_alignment`, `backlog_impact`, `backlog_urgency`, and `backlog_effort`.
- Parse each goal's 1-based numeric prefix as `goal_position`, require the goals note to use contiguous positions, and compute `goal_bonus = 2 * (goal_count - goal_position)`. The current four goals therefore produce bonuses `6`, `4`, `2`, and `0`.
- Compute `backlog_score = (4 * (A - 1)) + (3 * (I - 1)) + (2 * (U - 1)) - (E - 1) + goal_bonus`.
- Keep these exact weights explicit and centralized in this command.
- Parse the star strings, then write the resulting numeric `backlog_score` and `backlog_rank` together.
- Do not require calculated Base fields for the canonical score or rank. Base formulas may be added later for exploratory views, but they are not a source of truth.

### Rubric definitions

#### `backlog_alignment`

- `⭐` — weak or minimal relationship to the selected goal
- `⭐⭐` — indirectly supports the goal or removes a limited obstacle
- `⭐⭐⭐` — directly advances a meaningful part of the goal
- `⭐⭐⭐⭐` — central work on which substantial goal progress depends
- `⭐⭐⭐⭐⭐` — completing the issue itself delivers a major goal outcome

Assign every eligible issue its closest `backlog_goal`, even when alignment is weak.

#### `backlog_impact`

- `⭐` — small or highly localized benefit
- `⭐⭐` — clear but limited benefit to a narrow workflow or audience
- `⭐⭐⭐` — significant benefit to an important workflow or audience
- `⭐⭐⭐⭐` — major benefit across a core workflow or multiple audiences
- `⭐⭐⭐⭐⭐` — transformative outcome for the product, organization, or ecosystem

Measure the magnitude of the benefit if completed. Do not include urgency or effort in `backlog_impact`.

#### `backlog_urgency`

- `⭐` — can wait; delay has little material cost
- `⭐⭐` — pressure is building; delay slowly raises cost or loses opportunity
- `⭐⭐⭐` — pressing; delay materially worsens the outcome or problem
- `⭐⭐⭐⭐` — time-sensitive; an active commitment, dependency, or opportunity is at risk
- `⭐⭐⭐⭐⭐` — immediate; serious current harm, blockage, or a hard cutoff demands action

Measure cost of delay without estimating a duration. Require cited evidence for four- and five-star urgency; never invent a deadline, commitment, or closing opportunity window.

#### `backlog_effort`

- `⭐` — `XS`: one atomic, tightly scoped action
- `⭐⭐` — `S`: contained work with few touchpoints and a known approach
- `⭐⭐⭐` — `M`: several coordinated steps or touchpoints
- `⭐⭐⭐⭐` — `L`: broad work requiring substantial coordination or integration
- `⭐⭐⭐⭐⭐` — `XL`: a multi-phase initiative, epic, or scope too broad for one task

Measure relative breadth and coordination, never elapsed time. More effort stars mean more work, not a better issue. Flag five-star effort issues as likely decomposition candidates without automatically excluding or demoting them.

### Spot checks and corrections

- Automatically apply the complete reconciled set of issue-rating recommendations after agent review and calibration; do not wait for per-issue approval.
- After application and validation, show up to the invocation's `spot_check_size` as one compact table. Show changed issues first by resulting rank; when no values changed, show the highest-ranked current issues.
- Show each issue as a row with `Backlog rank`, `Issue`, `Goal`, `Alignment`, `Impact`, `Urgency`, and `Effort` columns. Spell out every factor name; never use `A`, `I`, `U`, or `E` as a standalone presentation header.
- Render rating cells as their stored star strings; do not repeat full definitions in every cell.
- Show the complete legend once with every spot-check group: the current goal names and definitions from `prioritization goals.md`, followed by all four factor scales and definitions from this command. Do not shorten the legend to the numeric prefixes or mnemonic labels alone.
- Treat `backlog_rank` as read-only context. If the user disagrees with an ordering, inspect or correct the underlying goal and rubric values, then let `renumber.py` calculate score and position automatically.
- Apply an explicit user correction immediately through `apply_ratings.py` when current hashes still match. If the user describes a disagreement without exact values, propose the smallest underlying correction and ask only for the choice needed to resolve it.
- Never display real-time duration estimates or translate effort labels into durations.
